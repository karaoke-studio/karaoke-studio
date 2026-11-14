"""Single-lane placement from canonical metrics; no Qt measurement or paging.

Legacy anchors its ink union and reserves the style's ruby allowance even on
rubyless lines. N3 anchors the cell grid and uses an integer em-height box.
"""
from dataclasses import dataclass
import math
from .alignment import lane_alignment, lane_offset_x, row_layout_params


@dataclass(frozen=True)
class LinePosition:
    offset_x: float
    baseline_y: float


def place_single_line(item, metrics, *, width, height, ink_left, ink_right, layout_width):
    style = item.layout_style
    mode = style.line_horizontal_layout
    if style.dual_line_layout or mode not in {'center', 'asymmetric', 'per_row'}:
        raise ValueError('Skia 当前定位仅支持单行布局')
    if style.line_y_position not in {'top', 'center', 'bottom'}:
        raise ValueError('Skia 行纵向位置无效')
    if style.layout_semantics == 'legacy':
        left, right = ink_left, ink_right
        ascent, descent = float(metrics['ascent']), float(metrics['descent'])
        # Main outlines are gated off here, so an inherited ruby stroke is 0.
        extra = max(style.ruby_gap_px + max(int(style.ruby_font_size_px), 1)
                    + max(int(style.ruby_stroke_width_px or 0), 0), 0)
    elif style.layout_semantics == 'n3_1074':
        left, right = 0, max(layout_width, 1)
        size = max(int(style.font_size_px), 1)
        descent = math.floor(size * max(float(metrics['descent']), 0)
                             / max(float(metrics['ascent']) + float(metrics['descent']), 1))
        ascent, extra = size - descent, 0
    else:
        raise ValueError('Skia 不支持此布局语义')
    row_align, row_x, row_y = row_layout_params(style, item.lane)
    margin = style.horizontal_margin_px
    x_extra, y_extra = 0, 0
    if mode == 'per_row':
        y_extra = row_y
    if item.center_override or mode == 'center':
        align = 'center'
    elif mode == 'per_row':
        align, margin, x_extra = row_align, 0, row_x
    else:
        # Use page metadata from the shared plan, never the current visible text.
        if not style.line_alignments:
            raise ValueError('Skia 非对称布局至少需要一个对齐项')
        align = lane_alignment(style, item.lane, item.page_line_count)
    if align == 'left':
        x = margin - left
    elif align == 'right':
        x = width - margin - right
    elif align == 'center':
        x = (width - left - right) / 2
    else:
        raise ValueError('Skia 行水平对齐无效')
    if style.line_y_position == 'top':
        baseline = style.line_y_margin_px + extra + ascent
    elif style.line_y_position == 'center':
        baseline = (height - (ascent + descent + extra)) / 2 + extra + ascent
    else:
        baseline = height - style.line_y_margin_px - descent
    # Single-lane authored X offsets use slot 0; the alignment can consume a
    # bottom short-page tail slot. This is the existing GPU projection contract.
    return LinePosition(x + x_extra + lane_offset_x(style, item.lane), baseline + y_extra)
