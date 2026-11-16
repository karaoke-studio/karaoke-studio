"""Verified localized family aliases for explicit Skia font resources.

Qt's platform matcher can resolve a localized family spelling (for example a
Japanese name record of an application font under fontconfig) although
``QFontDatabase.families()`` only lists the English name. The shared
capability layer then reports ``missing_family``. Skia accepts Qt's match only
when the matched face's own ``name`` table lists the requested spelling as a
family name, so a platform fallback substitute is never exported.

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
    global _GENERATION
    generation = capabilities_generation()
    if generation != _GENERATION:
        _ALIASES.clear()
        _GENERATION = generation


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
