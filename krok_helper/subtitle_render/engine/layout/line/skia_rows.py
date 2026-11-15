"""Source/layout row frames from explicit canonical font metrics.

Only used text slots contribute; invisible peer pages still share the frame.
The baseline of each glyph stays independent of this outer row box.
"""
from dataclasses import dataclass
import math
import hashlib

from ...style.style_semantics import style_for_role_in_layout
from ...text.font_slots import font_style_for_cell, main_script_stroke_style, is_n3_latin_text
from ...ruby.style_semantics import ruby_stroke_width


@dataclass(frozen=True)
class RowFrame:
    ascent: int
    descent: int
    ruby_extra: int
    reference_identity: str = ''


def fixed_row_frame(style, metrics):
    # Match the integer font-box adapter, never round glyph ink or positions.
    ascent = math.floor(max(metrics['ascent'], 0)+.5)
    descent = math.floor(max(metrics['descent'], 0)+.5)
    edge = max(int(style.stroke_width_px), 0)
    if style.layout_semantics == 'n3_1074':
        size = max(int(style.font_size_px), 1)
        down = size*descent//max(ascent+descent, 1)+edge//2
        return RowFrame(size+edge-down, down, 0)
    edge2 = max(int(style.stroke2_width_px), 0) if style.stroke2_enabled else 0
    pad = math.ceil((edge+edge2)/2)
    extra = max(int(style.ruby_gap_px)+max(int(style.ruby_font_size_px), 1)+ruby_stroke_width(style), 0)
    return RowFrame(ascent+pad, descent+pad, extra)


def source_row_frames(track, style, styles, measured, *, resolve_frames=None):
    """Retain default-only fractional placement, like the D2D protocol edge.

    Independent Latin or role/singer sources use source-wide integer frames.
    There is no Qt family alias discovery at this canonical boundary.
    """
    latin, _ = font_style_for_cell(style, 'a')
    independent = bool(str(style.font_family_latin or '').strip()) and (
        (latin.font_family, latin.font_size_px, latin.font_weight, latin.italic)
        != (style.font_family, style.font_size_px, style.font_weight, style.italic)
        or int(style.latin_font_stretch_pct) != 100)
    enabled, boxes, seen, defaults = set(), {}, set(), {}
    for line, appearance, metrics in zip(track.lines, styles, measured, strict=True):
        layout = int(line.layout_index or 0)
        defaults.setdefault(layout, (appearance, metrics.baseline_metrics))
        if appearance.lower_ruby_enabled:
            enabled.add(layout)
        for char, cell in zip(line.chars, metrics.cells, strict=True):
            if char.vector_glyph is not None or not char.text.strip():
                continue
            if line.singer_id is not None or char.role_label:
                enabled.add(layout)
            if line.is_blank:
                continue
            key = (layout, line.singer_id, char.role_label, is_n3_latin_text(char.text))
            if key in seen:
                continue
            seen.add(key)
            slot = main_script_stroke_style(style_for_role_in_layout(appearance, char.role_label), char.text)
            slot, _ = font_style_for_cell(slot, char.text)
            boxes.setdefault(layout, []).append((slot, cell))
    requests, indices = [], {}
    for layout, default in defaults.items():
        if not independent and layout not in enabled:
            continue
        used = boxes.get(layout) or [default]
        indices[layout] = range(len(requests), len(requests)+len(used))
        requests.extend(used)
    resolved = (resolve_frames(requests) if resolve_frames is not None
                else [fixed_row_frame(s, m) for s, m in requests])
    if len(resolved) != len(requests):
        raise ValueError('Skia 源行框参考度量数量失配')
    frames = {}
    for layout, offsets in indices.items():
        used = [resolved[i] for i in offsets]
        identities = sorted({b.reference_identity for b in used if b.reference_identity})
        identity = hashlib.sha256(repr(identities).encode()).hexdigest() if identities else ''
        frames[layout] = RowFrame(max(b.ascent for b in used), max(b.descent for b in used),
                                  max(b.ruby_extra for b in used), identity)
    return tuple(frames.get(int(line.layout_index or 0)) for line in track.lines)
