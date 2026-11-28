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


def _resource_directory() -> Path:
    """Resolve a persistent local cache directory for font snapshots.

    Avoid ``%TEMP%`` by default so OS/cleaner temp sweeps do not delete live
    snapshots mid-session; fall back to the app temp dir if local cache is
    unwritable (e.g. restricted sandbox).
    """
    from krok_helper.app_paths import cache_dir, temp_dir

    preferred = cache_dir("font-resources")
    try:
        preferred.mkdir(parents=True, exist_ok=True)
        return preferred
    except OSError:
        fallback = temp_dir("font-resources")
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


def clear_resource_cache() -> None:
    """Forget face mappings after registry changes; active sidecars keep files."""
    with _LOCK:
        _CACHE.clear()
        _PHYSICAL_CACHE.clear()


_LEGACY_CJK_CMAP_CODECS: dict[tuple[int, int], str] = {
    (3, 2): "cp932",   # Windows Shift-JIS (legacy Japanese fonts)
    (3, 3): "gbk",     # Windows PRC GB2312/GBK
    (3, 4): "big5",    # Windows Big5
    (3, 5): "cp949",   # Windows Wansung
}


def _parse_cmap_format4(subtable: bytes) -> dict[int, int]:
    """Decode a TrueType cmap format-4 subtable into ``{code: glyph_id}``."""
    if len(subtable) < 14:
        return {}
    fmt, length = struct.unpack_from(">HH", subtable, 0)
    if fmt != 4 or length > len(subtable) or length < 16:
        return {}
    seg_count_x2 = struct.unpack_from(">H", subtable, 6)[0]
    seg_count = seg_count_x2 // 2
    if seg_count <= 0:
        return {}
    end_off = 14
    start_off = end_off + seg_count_x2 + 2
    delta_off = start_off + seg_count_x2
    range_off = delta_off + seg_count_x2
    if range_off + seg_count_x2 > length:
        return {}
    mapping: dict[int, int] = {}
    for seg in range(seg_count):
        end_code = struct.unpack_from(">H", subtable, end_off + seg * 2)[0]
        start_code = struct.unpack_from(">H", subtable, start_off + seg * 2)[0]
        if start_code == 0xFFFF and end_code == 0xFFFF:
            break
        if start_code > end_code:
            continue
        id_delta = struct.unpack_from(">h", subtable, delta_off + seg * 2)[0]
        id_range_pos = range_off + seg * 2
        id_range_offset = struct.unpack_from(">H", subtable, id_range_pos)[0]
        for code in range(start_code, end_code + 1):
            if id_range_offset == 0:
                gid = (code + id_delta) & 0xFFFF
            else:
                glyph_pos = id_range_pos + id_range_offset + (code - start_code) * 2
                if glyph_pos + 2 > length:
                    continue
                raw_gid = struct.unpack_from(">H", subtable, glyph_pos)[0]
                gid = ((raw_gid + id_delta) & 0xFFFF) if raw_gid != 0 else 0
            if gid != 0:
                mapping[code] = gid
    return mapping


def _build_cmap_format4(unicode_to_gid: dict[int, int]) -> bytes:
    """Serialize a standard BMP Unicode cmap format-4 subtable."""
    items = sorted(
        (cp, gid) for cp, gid in unicode_to_gid.items() if 0 <= cp <= 0xFFFE and 0 < gid <= 0xFFFF
    )
    # Group contiguous codepoints; split runs when delta breaks to keep subtable compact.
    segments: list[tuple[int, int, int, list[int]]] = []
    idx = 0
    n = len(items)
    while idx < n:
        start_cp, first_gid = items[idx]
        end_cp = start_cp
        delta = (first_gid - start_cp) & 0xFFFF
        constant_delta = True
        gids = [first_gid]
        idx += 1
        while idx < n and items[idx][0] == end_cp + 1:
            cp, gid = items[idx]
            if ((gid - cp) & 0xFFFF) != delta:
                constant_delta = False
            gids.append(gid)
            end_cp = cp
            idx += 1
        if constant_delta:
            segments.append((start_cp, end_cp, delta, []))
        else:
            segments.append((start_cp, end_cp, 0, gids))
    # Required sentinel segment.
    segments.append((0xFFFF, 0xFFFF, 1, []))

    seg_count = len(segments)
    seg_count_x2 = seg_count * 2
    entry_selector = (seg_count.bit_length() - 1) if seg_count > 0 else 0
    search_range = (1 << entry_selector) * 2
    range_shift = seg_count_x2 - search_range

    end_codes = bytearray()
    start_codes = bytearray()
    id_deltas = bytearray()
    id_range_offsets = bytearray()
    glyph_id_array = bytearray()

    for seg_i, (start_cp, end_cp, delta, gids) in enumerate(segments):
        end_codes += struct.pack(">H", end_cp)
        start_codes += struct.pack(">H", start_cp)
        if not gids:
            signed_delta = delta - 0x10000 if delta >= 0x8000 else delta
            id_deltas += struct.pack(">h", signed_delta)
            id_range_offsets += struct.pack(">H", 0)
        else:
            id_deltas += struct.pack(">h", 0)
            # Distance in bytes from this idRangeOffset entry to its first glyphIdArray entry.
            offset_bytes = (seg_count - seg_i) * 2 + len(glyph_id_array)
            id_range_offsets += struct.pack(">H", offset_bytes)
            for gid in gids:
                glyph_id_array += struct.pack(">H", gid)

    body = (
        bytes(end_codes)
        + struct.pack(">H", 0)  # reservedPad
        + bytes(start_codes)
        + bytes(id_deltas)
        + bytes(id_range_offsets)
        + bytes(glyph_id_array)
    )
    length = 14 + len(body)
    if length > 0xFFFF:
        return b""
    header = struct.pack(">7H", 4, length, 0, seg_count_x2, search_range, entry_selector, range_shift)
    return header + body


def _ensure_unicode_cmap(tables: dict[str, bytes]) -> None:
    """Synthesize a Unicode BMP (3, 1) cmap subtable for legacy Shift-JIS/GBK/Big5/Wansung fonts.

    HarfBuzz's OpenType cmap lookup only reads Unicode subtables (platform 0 or
    (3, 1)/(3, 10)), whereas Qt/DirectWrite also read Windows Shift-JIS (3, 2)
    and other legacy CJK cmaps.  Synthesizing a (3, 1) format-4 subtable inside
    the exported snapshot lets Skia render those legacy fonts natively with
    their real glyphs.
    """
    cmap = tables.get("cmap")
    if not cmap or len(cmap) < 4:
        return
    version, num_tables = struct.unpack_from(">HH", cmap, 0)
    if num_tables <= 0 or 4 + num_tables * 8 > len(cmap):
        return
    records: list[tuple[int, int, int]] = []
    has_unicode = False
    legacy_candidate: tuple[int, int, int, str] | None = None
    for i in range(num_tables):
        pid, eid, offset = struct.unpack_from(">HHI", cmap, 4 + i * 8)
        records.append((pid, eid, offset))
        if pid == 0 or (pid == 3 and eid in (1, 10)):
            has_unicode = True
        elif legacy_candidate is None and (pid, eid) in _LEGACY_CJK_CMAP_CODECS:
            legacy_candidate = (pid, eid, offset, _LEGACY_CJK_CMAP_CODECS[(pid, eid)])
    if has_unicode or legacy_candidate is None:
        return
    _pid, _eid, sub_offset, codec = legacy_candidate
    if sub_offset >= len(cmap):
        return
    legacy_map = _parse_cmap_format4(cmap[sub_offset:])
    if not legacy_map:
        return
    unicode_map: dict[int, int] = {}
    for code, gid in legacy_map.items():
        raw_bytes = bytes([code]) if code <= 0xFF else bytes([(code >> 8) & 0xFF, code & 0xFF])
        try:
            decoded = raw_bytes.decode(codec)
        except UnicodeDecodeError:
            continue
        if len(decoded) != 1:
            continue
        cp = ord(decoded)
        if cp <= 0xFFFE and cp not in unicode_map:
            unicode_map[cp] = gid
    if not unicode_map:
        return
    new_subtable = _build_cmap_format4(unicode_map)
    if not new_subtable:
        return
    # Append the new (3, 1) subtable at the end of the existing cmap payload and insert its record sorted.
    shifted_records: list[tuple[int, int, int]] = [
        (pid, eid, offset + 8) for pid, eid, offset in records
    ]
    new_sub_offset = len(cmap) + 8
    shifted_records.append((3, 1, new_sub_offset))
    shifted_records.sort(key=lambda item: (item[0], item[1]))
    new_cmap = bytearray(struct.pack(">HH", version, len(shifted_records)))
    for pid, eid, offset in shifted_records:
        new_cmap += struct.pack(">HHI", pid, eid, offset)
    new_cmap += cmap[4 + num_tables * 8 :]
    new_cmap += new_subtable
    tables["cmap"] = bytes(new_cmap)


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
    _ensure_unicode_cmap(tables)
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
    directory_path = _resource_directory()
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
