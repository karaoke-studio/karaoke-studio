"""Content-addressed OpenType snapshots of the face Qt actually selected."""
from __future__ import annotations

import hashlib
from array import array
import os
from pathlib import Path
import struct
import sys
import tempfile
import threading

from PyQt6.QtGui import QRawFont

_LOCK = threading.RLock()
_CACHE: dict[tuple, str] = {}
_PHYSICAL_CACHE: dict[bytes, str] = {}


class FontResourceError(ValueError):
    """A selected Qt face cannot be transferred to the native renderer."""

# OpenType and AAT tables relevant to outlines, variation, metrics and shaping.
_TAGS = "head hhea maxp OS/2 hmtx cmap loca glyf name post cvt fpgm prep gasp CFF CFF2 VORG vhea vmtx BASE GDEF GPOS GSUB JSTF MATH fvar gvar avar cvar HVAR VVAR MVAR STAT COLR CPAL CBDT CBLC EBDT EBLC EBSC sbix SVG kern kerx morx mort ankr bsln feat lcar opbd prop trak acnt fdsc fmtx just Zapf hdmx LTSH VDMX PCLT Glat Gloc Silf Sill Feat".split()
_TAGS = [tag.ljust(4) for tag in _TAGS]


def clear_resource_cache() -> None:
    """Forget face mappings after registry changes; active sidecars keep files."""
    with _LOCK:
        _CACHE.clear()
        _PHYSICAL_CACHE.clear()


def snapshot_raw_font(raw: QRawFont, key: tuple) -> str:
    """Export original tables, without instancing or renumbering glyphs.

    A TTC face becomes a standalone face at index zero. Content addressing
    makes workers reuse the same file, including application-only Qt fonts.
    """
    with _LOCK:
        cached = _CACHE.get(key)
        if cached and Path(cached).is_file():
            return cached
    if not raw.isValid():
        raise ValueError("Cannot snapshot an invalid Qt font face")
    # Metadata/checkSumAdjustment is not a content identity: patched fonts
    # can retain it while changing outlines, metrics or variation tables.
    tables = {tag: bytes(raw.fontTable(tag)) for tag in _TAGS}
    tables = {tag: data for tag, data in tables.items() if data}
    fingerprint = hashlib.sha256()
    for tag, data in sorted(tables.items()):
        fingerprint.update(tag.encode("ascii"))
        fingerprint.update(struct.pack(">I", len(data)))
        fingerprint.update(data)
    identity = fingerprint.digest()
    with _LOCK:
        existing = _PHYSICAL_CACHE.get(identity)
        if existing and Path(existing).is_file():
            _CACHE[key] = existing
            return existing
    if "head" not in tables or "cmap" not in tables:
        raise ValueError("Qt font face has no usable OpenType tables")
    # Do not manufacture a seemingly valid SFNT from truncated Qt tables:
    # writing checkSumAdjustment into a short head can overwrite the next
    # table, handing a corrupt resource to DirectWrite in the sidecar.
    for tag, minimum in (("head", 54), ("cmap", 4), ("maxp", 6), ("hhea", 36)):
        if len(tables.get(tag, b"")) < minimum:
            raise ValueError(f"Qt font face has a truncated or missing {tag} table")
    head = bytearray(tables["head"])
    head[8:12] = b"\0" * 4
    tables["head"] = bytes(head)
    count = len(tables)
    power = 1 << (count.bit_length() - 1)
    header = struct.pack(">4s4H", b"OTTO" if "CFF " in tables or "CFF2" in tables else b"\0\1\0\0",
                         count, power * 16, power.bit_length() - 1, count * 16 - power * 16)
    directory = bytearray()
    body = bytearray()
    offset = 12 + count * 16
    head_offset = 0
    def checksum(data: bytes) -> int:
        padded = data + b"\0" * (-len(data) % 4)
        words = array("I", padded)
        if sys.byteorder == "little":
            words.byteswap()
        return sum(words) & 0xFFFFFFFF
    for tag, data in sorted(tables.items()):
        directory += struct.pack(">4s3I", tag.encode("ascii"), checksum(data), offset, len(data))
        if tag == "head":
            head_offset = offset
        padded = data + b"\0" * (-len(data) % 4)
        body += padded
        offset += len(padded)
    output = bytearray(header + directory + body)
    struct.pack_into(">I", output, head_offset + 8, (0xB1B0AFBA - checksum(output)) & 0xFFFFFFFF)
    digest = hashlib.sha256(output).hexdigest()
    directory_path = Path(tempfile.gettempdir()) / "LinKFontResources"
    directory_path.mkdir(parents=True, exist_ok=True)
    destination = directory_path / (digest + ".otf")
    with _LOCK:
        if not destination.is_file():
            with tempfile.NamedTemporaryFile(dir=directory_path, delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(output)
            try:
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)
        _CACHE[key] = str(destination)
        _PHYSICAL_CACHE[identity] = str(destination)
    return str(destination)
