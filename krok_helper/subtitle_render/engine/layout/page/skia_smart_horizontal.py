"""Canonical SmartHorizon arithmetic and configuration-scoped page statistics.

The output is a secondary logical X translation, before authored line offsets.
Page membership comes from the shared pager; visibility never filters siblings.
"""
import math
from ..line.alignment import lane_alignment, row_layout_params
from .pagination import renderable_page_map, line_center_override
from ...style.style_semantics import style_for_role_in_layout


def smart_horizontal_shift(mode, *, width, own_width, own_font, alignment, margin, maxima, page_head_font):
    if mode == 'none' or alignment == 'center':
        return 0
    if mode == 'center_position':
        threshold = math.floor(width / 2 + own_font / 2 - own_width)
        if threshold <= margin:
            return 0
        if alignment == 'right':
            return math.floor(width / 2 - own_font / 2) - (width - margin - own_width)
        return threshold - margin
    if mode != 'equal_margins':
        raise ValueError('Skia 智能水平布局模式无效')
    if maxima['left'] <= 0 or maxima['right'] <= 0:
        return 0
    slack = width - margin * 2 - sum(maxima.values()) + page_head_font
    if slack <= 0:
        return 0
    half = math.floor(slack / 2)
    return -half if alignment == 'right' else half


class SmartHorizontalPages:
    def __init__(self, track, style, styles, locals, *, anchors=None):
        self.track, self.styles, self.locals = track, tuple(styles), locals
        self.anchors = anchors
        self.indices = {id(line): i for i, line in enumerate(track.lines)}
        self.pages = renderable_page_map(track, style)
        self.lanes = {}
        self.maxima = {}
        self.fonts = tuple(max(int(style_for_role_in_layout(s, l.chars[0].role_label).font_size_px), 1)
                           if l.chars else max(int(s.font_size_px), 1)
                           for l, s in zip(track.lines, styles, strict=True))

    def set_lanes(self, items):
        lanes = {id(item.line): item.lane for item in items}
        if lanes == self.lanes:
            return False
        self.lanes = lanes
        self.maxima.clear()
        return True

    def anchor_width(self, index):
        if self.anchors is not None:
            left, right = self.anchors[index]
            return max(right - left, 1)
        local, style = self.locals[index], self.styles[index]
        if style.layout_semantics == 'n3_1074' or local.ink is None:
            return max(local.layout_width, 1)
        return max(local.ink[2] - local.ink[0], 1)

    def alignment(self, line, natural_lane, count):
        i = self.indices[id(line)]
        style = self.styles[i]
        lane = self.lanes.get(id(line), natural_lane)
        if line_center_override(self.track, line, style) or style.line_horizontal_layout == 'center':
            return 'center'
        if style.line_horizontal_layout == 'per_row':
            return row_layout_params(style, lane)[0]
        return lane_alignment(style, lane, count)

    def shift(self, item, width):
        style = item.layout_style
        if not style.dual_line_layout or style.line_horizontal_layout != 'asymmetric' or item.center_override:
            return 0
        page = self.pages.get(id(item.line))
        if not page or style.smart_horizontal == 'none':
            return 0
        i = self.indices[id(item.line)]
        alignment = lane_alignment(style, item.lane, item.page_line_count)
        head = id(page[0][0])
        if head not in self.maxima:
            maxima = {'left': 0, 'center': 0, 'right': 0}
            for line, lane in page:
                align = self.alignment(line, lane, len(page))
                if align not in maxima:
                    raise ValueError('Skia 行水平对齐无效')
                maxima[align] = max(maxima[align], self.anchor_width(self.indices[id(line)]))
            self.maxima[head] = maxima
        return smart_horizontal_shift(style.smart_horizontal, width=width,
            own_width=self.anchor_width(i), own_font=self.fonts[i], alignment=alignment,
            margin=style.horizontal_margin_px, maxima=self.maxima[head],
            page_head_font=self.fonts[self.indices[head]])
