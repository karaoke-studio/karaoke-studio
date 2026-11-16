"""Verified localized family aliases for explicit Skia font resources.

The shared capability layer may report ``missing_family`` for an unlisted
localized spelling. Skia verifies the platform match against its own ``name``
table, or lazily indexes the actual Qt families' name tables when the platform
matcher omits that spelling too. A substitute without that name is rejected.

Verification runs on the GUI thread; workers only read the decision recorded
for the current font generation. Shared CPU/D2D family resolution is unchanged.
"""
import struct
import threading

from krok_helper.subtitle_render.engine.text.font_capabilities import capabilities_generation
from krok_helper.subtitle_render.engine.text.font_weight import resolve_font_instance

_FAMILY_NAME_IDS = {1, 16}  # Family and typographic family, every language.
_LOCK = threading.Lock()
_ALIASES = {}  # {family: canonical family or None}, current generation only.
_GENERATION = None
_FAMILY_INDEX = None  # Built lazily on the GUI thread for an unmatched spelling.


def family_names(table: bytes) -> set:
    """Decoded family-name records of an OpenType ``name`` table."""
    if len(table) < 6:
        return set()
    _, count, storage = struct.unpack_from('>3H', table)
    names = set()
    for index in range(count):
        offset = 6 + index*12
        if offset + 12 > len(table):
            break
        platform, encoding, _, name_id, length, start = struct.unpack_from('>6H', table, offset)
        data = table[storage+start:storage+start+length]
        if name_id not in _FAMILY_NAME_IDS or len(data) != length:
            continue
        codec = 'mac_roman' if platform == 1 and encoding == 0 else 'utf-16-be' if platform in (0, 3) else None
        if codec is not None:
            names.add(data.decode(codec, errors='replace'))
    return names


def _current():
    global _GENERATION, _FAMILY_INDEX
    generation = capabilities_generation()
    if generation != _GENERATION:
        _ALIASES.clear()
        _FAMILY_INDEX = None
        _GENERATION = generation


def _indexed_alias(family):
    """GUI-only name-table index when the platform matcher omits aliases.

    CoreText need not match names outside its current locale. Enumerate the
    current Qt families once per font generation, reading their actual face
    tables; never scan files or ask a worker to create a font engine.
    """
    global _FAMILY_INDEX
    from PyQt6.QtGui import QFont, QFontDatabase, QRawFont
    with _LOCK:
        _current()
        generation, index = _GENERATION, _FAMILY_INDEX
    if index is None:
        index = {}
        for canonical in QFontDatabase.families():
            raw = QRawFont.fromFont(QFont(canonical))
            if not raw.isValid():
                continue
            names = family_names(bytes(raw.fontTable('name')))
            if canonical.casefold() not in {name.casefold() for name in names}:
                continue  # An unrelated platform substitute is not this family.
            for name in names:
                index.setdefault(name.casefold(), canonical)
        with _LOCK:
            _current()
            if generation != _GENERATION:
                return None  # Registry moved while this index was being built.
            _FAMILY_INDEX = index
    return index.get(family.casefold())


def verify_alias(family: str):
    """GUI thread: Qt's matched family when its face really carries ``family``."""
    from PyQt6.QtGui import QFont, QFontInfo, QRawFont
    font = QFont(family)
    matched = QFontInfo(font).family()
    raw = QRawFont.fromFont(font)
    alias = None
    if matched and matched != family and raw.isValid():
        wanted = family.casefold()
        if any(name.casefold() == wanted for name in family_names(bytes(raw.fontTable('name')))):
            alias = matched
    if alias is None:
        alias = _indexed_alias(family)
    with _LOCK:
        _current()
        _ALIASES[family] = alias
    return alias


def prepared_alias(family: str):
    """Worker-safe lookup of a GUI-verified alias for the current generation."""
    with _LOCK:
        _current()
        return _ALIASES.get(family)


def missing_family(instance) -> bool:
    return instance is not None and instance.reason == 'missing_family' and not instance.resource_path


def gui_font_instance(family: str, weight: int, italic: bool, *, resolve=None):
    """GUI thread: the shared decision, or the decision for a verified alias.

    ``resolve`` is the caller's discovery seam (default: the shared resolver).
    """
    resolve = resolve or resolve_font_instance
    instance = resolve(family, weight, italic=italic)
    if missing_family(instance) and (alias := verify_alias(family)):
        instance = resolve(alias, weight, italic=italic)
    return instance
