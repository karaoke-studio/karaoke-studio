"""Map canonical font metrics to the existing integer lyric-cell box policy."""
import math
from krok_helper.subtitle_render.engine.layout.line.width_policy import (
    nicokara_layout_width, nicokara_char_geometry_left_offset,
)


def cell_geometry(text, metrics, style, *, edge_size=0):
    size = max(int(style.font_size_px), 1)
    space = max(10, min(int(style.space_width_percent), 100))
    left, top, right, bottom = metrics['ink']
    if text == ' ':
        return size * space // 100, 0.0
    if right <= left or bottom <= top:
        return size * space * 25 // 100 // 10 + max(int(edge_size), 0), 0.0
    values = [metrics[k] for k in ('design_advance', 'design_left_bearing', 'design_right_bearing')]
    if not all(math.isfinite(v) for v in values):
        raise ValueError('Skia 字体设计度量包含非有限值')
    advance, bearing_left, bearing_right = (int(v) for v in values)
    width = nicokara_layout_width(int(right - left), advance, bearing_left, bearing_right,
                                  edge_size=edge_size, allow_biting=style.allow_biting)
    # The outline contributes an integer inset in reference coordinates,
    # before the final geometry is scaled for the target DPR.
    offset = -left + nicokara_char_geometry_left_offset(
        int(right - left), advance, bearing_left, allow_biting=style.allow_biting,
    ) + max(int(edge_size), 0) // 2
    return width, float(offset)


def _qt_round(value):
    # QFixed 26.6 storage, then qRound (half up), as QFontMetrics reports.
    return math.floor(round(value * 64) / 64 + 0.5)


def interval_width(text, metrics, style, *, edge_size=0):
    """Shared-span timing weight of the existing GPU path.

    The GPU draws the canonical cell box above, but its shared-span clocks are
    weighted by Painter's integer metrics: rounded pixel advance and, for a
    single character, rounded pixel side bearings (a string falls back to
    truncated ink-box bearings). Exact design proportions differ by about a
    pixel per cell and move every boundary inside a shared span.
    """
    size = max(int(style.font_size_px), 1)
    space = max(10, min(int(style.space_width_percent), 100))
    left, top, right, bottom = metrics['ink']
    if text == ' ':
        return size * space // 100
    if right <= left or bottom <= top:
        return size * space * 25 // 100 // 10 + max(int(edge_size), 0)
    values = [metrics[k] for k in ('advance', 'design_advance', 'design_left_bearing', 'design_right_bearing')]
    if not all(math.isfinite(v) for v in values):
        raise ValueError('Skia 字体设计度量包含非有限值')
    advance_px, design_advance, design_left, design_right = values
    advance = _qt_round(advance_px)
    if len(text) == 1 and design_advance > 0:
        scale = advance_px / design_advance
        bearing_left, bearing_right = _qt_round(design_left * scale), _qt_round(design_right * scale)
    else:
        bearing_left, bearing_right = int(left), int(advance - right)
    return nicokara_layout_width(int(right - left), advance, bearing_left, bearing_right,
                                 edge_size=edge_size, allow_biting=style.allow_biting)
