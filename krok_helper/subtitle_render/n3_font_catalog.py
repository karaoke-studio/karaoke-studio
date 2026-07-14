"""NicoKaraMaker3-compatible system font discovery and name resolution."""

from __future__ import annotations

import ctypes
import logging
import sys
import uuid
from ctypes import wintypes
from dataclasses import dataclass, replace
from functools import cmp_to_key, lru_cache
from types import MappingProxyType
from typing import Callable, Mapping, Sequence

from PyQt6.QtGui import QFontDatabase

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


@dataclass(frozen=True)
class N3FontCatalog:
    """Canonical N3 display names and case-insensitive localized aliases."""

    families: tuple[str, ...]
    aliases: Mapping[str, str]
    authoritative: bool

    def canonicalize(self, name: str) -> str | None:
        return self.aliases.get(str(name or "").strip().casefold())


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
) -> N3FontCatalog:
    entries: list[_CatalogEntry] = []
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
        entries.append(
            _CatalogEntry(
                canonical_name=canonical,
                has_japanese_name=japanese_name is not None,
                aliases=tuple(name for _locale, name in record.names if name),
            )
        )

    def compare_entries(left: _CatalogEntry, right: _CatalogEntry) -> int:
        if left.has_japanese_name != right.has_japanese_name:
            return -1 if left.has_japanese_name else 1
        return compare(left.canonical_name, right.canonical_name)

    entries.sort(key=cmp_to_key(compare_entries))
    aliases: dict[str, str] = {}
    for entry in entries:
        aliases.setdefault(entry.canonical_name.casefold(), entry.canonical_name)
        for alias in entry.aliases:
            aliases.setdefault(alias.casefold(), entry.canonical_name)
    return N3FontCatalog(
        families=tuple(entry.canonical_name for entry in entries),
        aliases=MappingProxyType(aliases),
        authoritative=True,
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


def _qt_fallback_catalog() -> N3FontCatalog:
    families = tuple(QFontDatabase.families())
    return N3FontCatalog(
        families=families,
        aliases=MappingProxyType({name.casefold(): name for name in families}),
        authoritative=False,
    )


@lru_cache(maxsize=1)
def get_n3_font_catalog() -> N3FontCatalog:
    if sys.platform == "win32":
        try:
            return _build_catalog(_directwrite_records(), compare=_compare_ja_jp)
        except (OSError, RuntimeError, TypeError, ValueError):
            log.exception("DirectWrite 字体目录枚举失败，已回退 Qt 字体目录")
    return _qt_fallback_catalog()


def n3_font_families() -> tuple[str, ...]:
    return get_n3_font_catalog().families


def canonicalize_n3_font_family(name: str) -> str | None:
    return get_n3_font_catalog().canonicalize(name)


def _n3_default_family(catalog: N3FontCatalog, fallback: str) -> str:
    for candidate in _N3_DEFAULT_FONT_CANDIDATES:
        canonical = catalog.canonicalize(candidate)
        if canonical is not None:
            return canonical
    return catalog.families[0] if catalog.families else fallback


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
        canonical = catalog.canonicalize(scheme.font_family)
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
            canonical = catalog.canonicalize(value)
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
    root = catalog.canonicalize(style.font_family) or _n3_default_family(
        catalog, style.font_family
    )
    if root != style.font_family:
        changes["font_family"] = root
    for field in _OPTIONAL_FONT_FIELDS:
        value = getattr(style, field)
        if value:
            canonical = catalog.canonicalize(value)
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
        title_root = catalog.canonicalize(title.font_family) or _n3_default_family(
            catalog, title.font_family
        )
        if title_root != title.font_family:
            title_changes["font_family"] = title_root
        if title.font_family_latin:
            title_latin = catalog.canonicalize(title.font_family_latin)
            if title_latin != title.font_family_latin:
                title_changes["font_family_latin"] = title_latin
        if title_changes:
            changes["title_overlay"] = replace(title, **title_changes)

    if not changes:
        return style, False
    return replace(style, **changes), True
