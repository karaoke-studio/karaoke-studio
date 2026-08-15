"""NicoKaraMaker3-compatible system font discovery and name resolution.

The primary record source is SUG's process-level font cache (EMBEDDING §8):
``localized_alias_map()`` localizes family names via a pure-stdlib font-file
scan that ``prewarm_async`` keeps warm in a background thread, and the Qt
family snapshot is cached the same way.  Building the catalog from those
caches is pure dict work - the DirectWrite COM walk it replaces enumerates
every family and face synchronously and blocks the UI thread for tens of
seconds on machines with large font libraries (first project load / first
font combo open).  DirectWrite remains as the fallback when the cached scan
yields nothing.
"""

from __future__ import annotations

import ctypes
import logging
import sys
import uuid
from ctypes import wintypes
from dataclasses import dataclass, field, replace
from functools import cmp_to_key, lru_cache
from types import MappingProxyType
from typing import Callable, Mapping, Sequence

from PyQt6.QtGui import QFont, QFontDatabase, QFontInfo, QGuiApplication

from krok_helper.subtitle_render.models import Style, SubtitleStyleScheme


log = logging.getLogger(__name__)

_DWRITE_FACTORY_TYPE_SHARED = 0
_DWRITE_FONT_STYLE_NORMAL = 0
_IID_IDWRITE_FACTORY = "B859EE5A-D838-4B5B-A2E8-1ADC7D93DB48"
_N3_DEFAULT_FONT_CANDIDATES = ("HGP明朝E", "游明朝", "ＭＳ Ｐ明朝")
_OPTIONAL_FONT_FIELDS = (
    "font_family_latin",
    "ruby_font_family",
    "ruby_font_family_latin",
)
_OPTIONAL_FONT_WEIGHT_FIELDS = {
    "font_family_latin": "latin_font_weight",
    "ruby_font_family": "ruby_font_weight",
    "ruby_font_family_latin": "ruby_latin_font_weight",
}


@dataclass(frozen=True)
class N3FontCatalog:
    """Canonical N3 display names and case-insensitive localized aliases."""

    families: tuple[str, ...]
    aliases: Mapping[str, str]
    authoritative: bool
    family_aliases: Mapping[str, tuple[str, ...]] = field(
        default_factory=lambda: MappingProxyType({})
    )
    qt_families: Mapping[str, str] = field(
        default_factory=lambda: MappingProxyType({})
    )

    def canonicalize(self, name: str) -> str | None:
        return self.aliases.get(str(name or "").strip().casefold())

    def aliases_for(self, name: str) -> tuple[str, ...]:
        canonical = self.canonicalize(name)
        if canonical is None:
            return ()
        return self.family_aliases.get(canonical.casefold(), (canonical,))

    def qt_family(self, name: str) -> str:
        """Return the spelling Qt exposes for an N3/localized family name."""

        requested = str(name or "").strip()
        canonical = self.canonicalize(requested)
        if canonical is None:
            return requested
        return self.qt_families.get(canonical.casefold(), canonical)


@dataclass(frozen=True)
class _FamilyRecord:
    """DirectWrite family data reduced to the values N3 consults."""

    names: tuple[tuple[str, str], ...]
    styles: tuple[int, ...]


@dataclass(frozen=True)
class _CatalogEntry:
    canonical_name: str
    has_japanese_name: bool
    aliases: tuple[str, ...]


def _build_catalog(
    records: Sequence[_FamilyRecord],
    *,
    compare: Callable[[str, str], int],
    qt_families: Sequence[str] = (),
) -> N3FontCatalog:
    merged: dict[str, _CatalogEntry] = {}
    for record in records:
        if _DWRITE_FONT_STYLE_NORMAL not in record.styles or not record.names:
            continue
        japanese_name = next(
            (name for locale, name in record.names if locale.casefold() == "ja-jp"),
            None,
        )
        canonical = japanese_name or record.names[0][1]
        if not canonical:
            continue
        aliases = tuple(name for _locale, name in record.names if name)
        key = canonical.casefold()
        existing = merged.get(key)
        if existing is None:
            merged[key] = _CatalogEntry(
                canonical_name=canonical,
                has_japanese_name=japanese_name is not None,
                aliases=aliases,
            )
        else:
            # Parallel installs (per-user + per-machine) expose the same family
            # twice; merge the alias sets instead of listing the family twice.
            merged[key] = _CatalogEntry(
                canonical_name=existing.canonical_name,
                has_japanese_name=existing.has_japanese_name,
                aliases=tuple(dict.fromkeys((*existing.aliases, *aliases))),
            )
    entries: list[_CatalogEntry] = list(merged.values())

    # Qt enumerates fonts the DirectWrite system collection can miss (fonts
    # installed after app start, per-user installs).  Follow the SUG font
    # picker's contract: every family QFontDatabase can resolve must stay
    # selectable in the font combo.
    covered = {
        alias.casefold()
        for entry in entries
        for alias in (entry.canonical_name, *entry.aliases)
    }
    for qt_name in qt_families:
        key = str(qt_name or "").casefold()
        if not key or key in covered:
            continue
        entries.append(_CatalogEntry(str(qt_name), False, (str(qt_name),)))
        covered.add(key)

    def compare_entries(left: _CatalogEntry, right: _CatalogEntry) -> int:
        if left.has_japanese_name != right.has_japanese_name:
            return -1 if left.has_japanese_name else 1
        return compare(left.canonical_name, right.canonical_name)

    entries.sort(key=cmp_to_key(compare_entries))
    aliases: dict[str, str] = {}
    family_aliases: dict[str, tuple[str, ...]] = {}
    resolved_qt_families: dict[str, str] = {}
    qt_names = {name.casefold(): name for name in qt_families if name}
    for entry in entries:
        aliases.setdefault(entry.canonical_name.casefold(), entry.canonical_name)
        for alias in entry.aliases:
            aliases.setdefault(alias.casefold(), entry.canonical_name)
        canonical_key = entry.canonical_name.casefold()
        ordered_aliases = tuple(
            dict.fromkeys((entry.canonical_name, *entry.aliases))
        )
        family_aliases[canonical_key] = ordered_aliases
        for alias in ordered_aliases:
            qt_name = qt_names.get(alias.casefold())
            if qt_name is not None:
                resolved_qt_families[canonical_key] = qt_name
                break
    return N3FontCatalog(
        families=tuple(entry.canonical_name for entry in entries),
        aliases=MappingProxyType(aliases),
        authoritative=True,
        family_aliases=MappingProxyType(family_aliases),
        qt_families=MappingProxyType(resolved_qt_families),
    )


class _Guid(ctypes.Structure):
    _fields_ = (
        ("data1", ctypes.c_uint32),
        ("data2", ctypes.c_uint16),
        ("data3", ctypes.c_uint16),
        ("data4", ctypes.c_ubyte * 8),
    )


def _guid(value: str) -> _Guid:
    return _Guid.from_buffer_copy(uuid.UUID(value).bytes_le)


def _com_method(pointer, index: int, restype, *argtypes):
    winfunctype = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)
    address = ctypes.cast(
        pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))
    ).contents[index]
    return winfunctype(restype, ctypes.c_void_p, *argtypes)(address)


def _release(pointer) -> None:
    if pointer:
        _com_method(pointer, 2, ctypes.c_ulong)(pointer)


def _check_hresult(value: int) -> None:
    if value < 0:
        raise OSError(f"DirectWrite HRESULT 0x{value & 0xFFFFFFFF:08X}")


def _localized_string(strings, index: int, *, locale_name: bool) -> str:
    length = ctypes.c_uint32()
    length_method = 5 if locale_name else 7
    value_method = 6 if locale_name else 8
    _check_hresult(
        _com_method(
            strings,
            length_method,
            ctypes.c_long,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32),
        )(strings, index, ctypes.byref(length))
    )
    buffer = ctypes.create_unicode_buffer(length.value + 1)
    _check_hresult(
        _com_method(
            strings,
            value_method,
            ctypes.c_long,
            ctypes.c_uint32,
            wintypes.LPWSTR,
            ctypes.c_uint32,
        )(strings, index, buffer, length.value + 1)
    )
    return buffer.value


def _localized_names(strings) -> tuple[tuple[str, str], ...]:
    count = _com_method(strings, 3, ctypes.c_uint32)(strings)
    return tuple(
        (
            _localized_string(strings, index, locale_name=True),
            _localized_string(strings, index, locale_name=False),
        )
        for index in range(count)
    )


def _directwrite_records() -> list[_FamilyRecord]:
    if sys.platform != "win32":
        raise OSError("DirectWrite is only available on Windows")

    dwrite = ctypes.WinDLL("dwrite.dll")
    create_factory = dwrite.DWriteCreateFactory
    create_factory.argtypes = (
        ctypes.c_int,
        ctypes.POINTER(_Guid),
        ctypes.POINTER(ctypes.c_void_p),
    )
    create_factory.restype = ctypes.c_long

    iid = _guid(_IID_IDWRITE_FACTORY)
    factory = ctypes.c_void_p()
    collection = ctypes.c_void_p()
    _check_hresult(
        create_factory(
            _DWRITE_FACTORY_TYPE_SHARED, ctypes.byref(iid), ctypes.byref(factory)
        )
    )
    try:
        _check_hresult(
            _com_method(
                factory,
                3,
                ctypes.c_long,
                ctypes.POINTER(ctypes.c_void_p),
                wintypes.BOOL,
            )(factory, ctypes.byref(collection), False)
        )
        records: list[_FamilyRecord] = []
        family_count = _com_method(collection, 3, ctypes.c_uint32)(collection)
        for family_index in range(family_count):
            family = ctypes.c_void_p()
            _check_hresult(
                _com_method(
                    collection,
                    4,
                    ctypes.c_long,
                    ctypes.c_uint32,
                    ctypes.POINTER(ctypes.c_void_p),
                )(collection, family_index, ctypes.byref(family))
            )
            try:
                names_object = ctypes.c_void_p()
                _check_hresult(
                    _com_method(
                        family,
                        6,
                        ctypes.c_long,
                        ctypes.POINTER(ctypes.c_void_p),
                    )(family, ctypes.byref(names_object))
                )
                try:
                    names = _localized_names(names_object)
                finally:
                    _release(names_object)

                styles: list[int] = []
                font_count = _com_method(family, 4, ctypes.c_uint32)(family)
                for font_index in range(font_count):
                    font = ctypes.c_void_p()
                    _check_hresult(
                        _com_method(
                            family,
                            5,
                            ctypes.c_long,
                            ctypes.c_uint32,
                            ctypes.POINTER(ctypes.c_void_p),
                        )(family, font_index, ctypes.byref(font))
                    )
                    try:
                        styles.append(_com_method(font, 6, ctypes.c_int)(font))
                    finally:
                        _release(font)
                records.append(_FamilyRecord(names=names, styles=tuple(styles)))
            finally:
                _release(family)
        return records
    finally:
        _release(collection)
        _release(factory)


def _compare_ja_jp(left: str, right: str) -> int:
    compare_string = ctypes.WinDLL("kernel32.dll").CompareStringEx
    compare_string.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.LPCWSTR,
        ctypes.c_int,
        wintypes.LPCWSTR,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_ssize_t,
    )
    compare_string.restype = ctypes.c_int
    result = compare_string("ja-JP", 0, left, -1, right, -1, None, None, 0)
    if result == 0:
        raise OSError(ctypes.get_last_error(), "CompareStringEx failed")
    return result - 2


# SUG's font-name scan reports Windows language IDs; catalog semantics only
# distinguish the Japanese locale when picking the canonical display name,
# every other entry is an opaque alias.
_LANGID_LOCALE = {
    0x0411: "ja-jp",
    0x0412: "ko-kr",
    0x0804: "zh-cn",
    0x0404: "zh-tw",
    0x0C04: "zh-hk",
    0x1004: "zh-sg",
    0x0409: "en-us",
}


def _sug_alias_records() -> list[_FamilyRecord]:
    """Localized-name records synthesized from SUG's cached font-file scan.

    The scan (registry ∪ font directories, OpenType ``name`` tables) is
    prewarmed into a process-level cache by ``font_cache.prewarm_async`` at
    startup, so this is dict work instead of the DirectWrite COM walk.
    Name tables carry no style information, so every record claims the
    normal face: ``_build_catalog`` merges every Qt-visible family back in
    regardless of styles, so selectable families are unchanged.
    """

    try:
        from strange_uta_game.frontend.font_names import localized_alias_map
    except Exception:
        return []
    records: list[_FamilyRecord] = []
    for english, natives in localized_alias_map().items():
        names: list[tuple[str, str]] = [("en-us", english)]
        for langid, native in natives.items():
            locale = _LANGID_LOCALE.get(langid, f"lang-{langid:04x}")
            names.append((locale, native))
        records.append(
            _FamilyRecord(names=tuple(names), styles=(_DWRITE_FONT_STYLE_NORMAL,))
        )
    return records


def _sug_installed_families() -> tuple[str, ...]:
    """Qt family snapshot from SUG's process-level cache (EMBEDDING §8)."""

    try:
        from PyQt6.QtGui import QGuiApplication

        from strange_uta_game.frontend import font_cache

        if QGuiApplication.instance() is None:
            # Never build the snapshot without a live application: it would
            # cache the empty pre-GUI database for the rest of the process.
            return ()
        return tuple(font_cache.installed_families())
    except Exception:
        return ()


def _qt_families_for_catalog(qt_available: bool) -> tuple[str, ...]:
    """Qt-visible families, preferring the prewarmed SUG snapshot."""

    if not qt_available:
        return ()
    cached = _sug_installed_families()
    if cached:
        return cached
    return tuple(QFontDatabase.families())


def installed_qt_font_families() -> tuple[str, ...]:
    """Public wrapper for Qt-visible families with the SUG snapshot preferred."""

    return _qt_families_for_catalog(True)


def _qt_fallback_catalog(*, qt_available: bool = True) -> N3FontCatalog:
    families = tuple(QFontDatabase.families()) if qt_available else ()
    if qt_available and not families:
        log.warning("Qt 字体目录为空：未检测到任何系统字体")
    aliases = {name.casefold(): name for name in families}
    return N3FontCatalog(
        families=families,
        aliases=MappingProxyType(aliases),
        authoritative=False,
        family_aliases=MappingProxyType(
            {name.casefold(): (name,) for name in families}
        ),
        qt_families=MappingProxyType(aliases),
    )


def _qt_application_cache_key() -> int:
    """Distinguish pre-QApplication discovery from a live Qt font database."""
    application = QGuiApplication.instance()
    if application is None or not isinstance(application, QGuiApplication):
        return 0
    return id(application)


@lru_cache(maxsize=4)
def _get_n3_font_catalog(qt_application_key: int) -> N3FontCatalog:
    qt_available = qt_application_key != 0
    if sys.platform == "win32":
        # Primary source: SUG's process-level font cache, prewarmed at
        # startup - keeps the first catalog build off the critical path of
        # project load / first font combo open.
        try:
            sug_records = _sug_alias_records()
        except Exception:
            sug_records = []
        if sug_records:
            qt_families = _qt_families_for_catalog(qt_available)
            if qt_families:
                return _build_catalog(
                    sug_records,
                    compare=_compare_ja_jp,
                    qt_families=qt_families,
                )
            # Without a populated Qt font database (pre-GUI canonicalization,
            # offscreen sessions) the alias map alone misses English-only
            # families; fall through to the full DirectWrite walk, which is
            # Qt-independent.  Real GUI sessions - the slow-library case the
            # cache exists for - always take the branch above.
        try:
            records = _directwrite_records()
            if records:
                return _build_catalog(
                    records,
                    compare=_compare_ja_jp,
                    qt_families=_qt_families_for_catalog(qt_available),
                )
            # A damaged/disabled Windows font cache service can yield an empty
            # DirectWrite collection without any HRESULT failure.  Treat that
            # as "no catalog" instead of freezing an authoritative empty list.
            log.warning("DirectWrite 字体目录为空，已回退 Qt 字体目录")
        except Exception:
            # Hand-written COM calls can raise anything on locked-down or
            # damaged font stacks; the Qt (GDI) catalog is the safety net.
            log.exception("DirectWrite 字体目录枚举失败，已回退 Qt 字体目录")
    return _qt_fallback_catalog(qt_available=qt_available)


def get_n3_font_catalog() -> N3FontCatalog:
    """Return a catalog scoped to the current QApplication lifecycle.

    Import/load helpers may canonicalize project fonts before the GUI exists.
    That pre-Qt catalog must not freeze an empty ``QFontDatabase`` mapping for
    the later Painter session.
    """
    return _get_n3_font_catalog(_qt_application_cache_key())


def n3_font_families() -> tuple[str, ...]:
    return get_n3_font_catalog().families


def canonicalize_n3_font_family(name: str) -> str | None:
    return get_n3_font_catalog().canonicalize(name)


@lru_cache(maxsize=1024)
def _resolve_qt_font_family_cached(name: str, qt_application_key: int) -> str:
    """Resolve a saved/display N3 name to the current Qt platform spelling.

    DirectWrite exposes all localized family names while Qt may expose only the
    spelling preferred by the current Windows locale.  The catalog performs the
    cheap alias lookup.  ``QFontInfo`` is used only once per requested family as
    a defensive check and its result is cached.
    """

    catalog = get_n3_font_catalog()
    requested = str(name or "").strip()
    candidate = catalog.qt_family(requested)
    if not candidate or qt_application_key == 0:
        return candidate

    try:
        actual = QFontInfo(QFont(candidate)).family().strip()
    except (RuntimeError, TypeError, ValueError):
        return candidate
    if not actual:
        return candidate

    aliases = catalog.aliases_for(requested)
    known_aliases = {alias.casefold() for alias in aliases}
    if actual.casefold() in known_aliases:
        return actual
    # Qt application fonts can be registered after the DirectWrite/Qt catalog
    # was built.  Probe the other localized spellings before accepting a stale
    # catalog mapping; this keeps English/Japanese family aliases equivalent
    # without rebuilding the expensive DirectWrite catalog for every frame.
    for alias in aliases:
        if alias.casefold() == candidate.casefold():
            continue
        try:
            alias_actual = QFontInfo(QFont(alias)).family().strip()
        except (RuntimeError, TypeError, ValueError):
            continue
        if alias_actual.casefold() in known_aliases:
            return alias_actual
    if catalog.canonicalize(requested) is not None:
        log.warning(
            "Qt 字体回退：请求 %r（解析为 %r），实际匹配 %r",
            requested,
            candidate,
            actual,
        )
    return candidate


def resolve_qt_font_family(name: str) -> str:
    """Resolve one family without sharing pre-GUI results with a live GUI."""
    return _resolve_qt_font_family_cached(
        str(name or ""),
        _qt_application_cache_key(),
    )


# Preserve the cache controls used by diagnostics/tests while keeping the
# QApplication identity in the actual cache key.
resolve_qt_font_family.cache_clear = _resolve_qt_font_family_cached.cache_clear  # type: ignore[attr-defined]
resolve_qt_font_family.cache_info = _resolve_qt_font_family_cached.cache_info  # type: ignore[attr-defined]


def invalidate_n3_font_caches() -> None:
    """Invalidate caches after the process-wide Qt font registry changes."""

    _get_n3_font_catalog.cache_clear()
    _resolve_qt_font_family_cached.cache_clear()
    try:
        from strange_uta_game.frontend import font_cache

        # Qt registry changes do not alter font files on disk, so the
        # expensive localized-name scan is kept (EMBEDDING §8).
        font_cache.invalidate(clear_alias_map=False)
    except Exception:
        pass


def _n3_default_family(catalog: N3FontCatalog, fallback: str) -> str:
    for candidate in _N3_DEFAULT_FONT_CANDIDATES:
        canonical = catalog.canonicalize(candidate)
        if canonical is not None:
            return canonical
    return catalog.families[0] if catalog.families else fallback


def _canonicalize_family_with_weight(
    catalog: N3FontCatalog, name: str, weight: int | None
) -> str | None:
    """Resolve physical bold/regular families saved by N3 as family + face."""

    canonical = catalog.canonicalize(name)
    if canonical is not None:
        return canonical
    requested = str(name or "").strip()
    if not requested:
        return None
    suffix = "-B" if weight is not None and int(weight) >= 600 else "-R"
    return catalog.canonicalize(f"{requested}{suffix}")


def normalize_scheme_font_families(
    scheme: SubtitleStyleScheme,
    catalog: N3FontCatalog | None = None,
) -> tuple[SubtitleStyleScheme, bool]:
    """Canonicalize one saved scheme without mutating the source object."""

    catalog = catalog or get_n3_font_catalog()
    if not catalog.authoritative:
        return scheme, False

    changes: dict[str, str | None] = {}
    if scheme.font_family:
        canonical = _canonicalize_family_with_weight(
            catalog, scheme.font_family, scheme.font_weight
        )
        normalized_root = canonical or (
            _n3_default_family(catalog, scheme.font_family)
            if scheme.n3_font_inheritance
            else None
        )
        if normalized_root != scheme.font_family:
            changes["font_family"] = normalized_root
    for field in _OPTIONAL_FONT_FIELDS:
        value = getattr(scheme, field)
        if value:
            canonical = _canonicalize_family_with_weight(
                catalog, value, getattr(scheme, _OPTIONAL_FONT_WEIGHT_FIELDS[field])
            )
            if canonical != value:
                changes[field] = canonical

    if not changes:
        return scheme, False
    return replace(scheme, **changes), True


def normalize_style_font_families(
    style: Style,
    catalog: N3FontCatalog | None = None,
) -> tuple[Style, bool]:
    """Canonicalize every font-family field reachable from a ``Style``."""

    catalog = catalog or get_n3_font_catalog()
    if not catalog.authoritative:
        return style, False

    changes: dict[str, object] = {}
    root = _canonicalize_family_with_weight(
        catalog, style.font_family, style.font_weight
    ) or _n3_default_family(
        catalog, style.font_family
    )
    if root != style.font_family:
        changes["font_family"] = root
    for field in _OPTIONAL_FONT_FIELDS:
        value = getattr(style, field)
        if value:
            canonical = _canonicalize_family_with_weight(
                catalog, value, getattr(style, _OPTIONAL_FONT_WEIGHT_FIELDS[field])
            )
            if canonical != value:
                changes[field] = canonical

    singer_schemes: dict[int, SubtitleStyleScheme] = {}
    singer_changed = False
    for key, scheme in style.singer_style_overrides.items():
        normalized, changed = normalize_scheme_font_families(scheme, catalog)
        singer_schemes[key] = normalized
        singer_changed |= changed
    if singer_changed:
        changes["singer_style_overrides"] = singer_schemes

    custom_schemes: dict[str, SubtitleStyleScheme] = {}
    custom_changed = False
    for name, scheme in style.custom_style_schemes.items():
        normalized, changed = normalize_scheme_font_families(scheme, catalog)
        custom_schemes[name] = normalized
        custom_changed |= changed
    if custom_changed:
        changes["custom_style_schemes"] = custom_schemes

    title = style.title_overlay
    if title is not None:
        title_changes: dict[str, str | None] = {}
        title_root = _canonicalize_family_with_weight(
            catalog, title.font_family, title.font_weight
        ) or _n3_default_family(
            catalog, title.font_family
        )
        if title_root != title.font_family:
            title_changes["font_family"] = title_root
        if title.font_family_latin:
            title_latin = _canonicalize_family_with_weight(
                catalog, title.font_family_latin, title.font_weight
            )
            if title_latin != title.font_family_latin:
                title_changes["font_family_latin"] = title_latin
        if title_changes:
            changes["title_overlay"] = replace(title, **title_changes)

    if not changes:
        return style, False
    return replace(style, **changes), True
