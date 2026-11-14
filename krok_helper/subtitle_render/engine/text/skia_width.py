"""Map canonical font metrics to the existing integer lyric-cell box policy."""
import math
from krok_helper.subtitle_render.engine.layout.line.width_policy import (
    nicokara_layout_width, nicokara_char_geometry_left_offset,
)


def cell_geometry(text, metrics, style):
    size = max(int(style.font_size_px), 1)
    space = max(10, min(int(style.space_width_percent), 100))
    left, top, right, bottom = metrics['ink']
    if text == ' ':
        return size * space // 100, 0.0
    if right <= left or bottom <= top:
        return size * space * 25 // 100 // 10, 0.0
    values = [metrics[k] for k in ('design_advance', 'design_left_bearing', 'design_right_bearing')]
    if not all(math.isfinite(v) for v in values):
        raise ValueError('Skia 字体设计度量包含非有限值')
    advance, bearing_left, bearing_right = (int(v) for v in values)
    width = nicokara_layout_width(int(right - left), advance, bearing_left, bearing_right,
                                  edge_size=0, allow_biting=style.allow_biting)
    offset = -left + nicokara_char_geometry_left_offset(int(right - left), advance, bearing_left,
                                                       allow_biting=style.allow_biting)
    return width, float(offset)
