"""Horizontal lane alignment and line-origin policies."""

from __future__ import annotations

from PyQt6.QtGui import QFontMetrics

from krok_helper.subtitle_render.domain.models import Style
from krok_helper.subtitle_render.domain.timing import (
    RubyAnnotation,
    TimingLine,
    TimingTrack,
)
from krok_helper.subtitle_render.engine.guide import (
    render_line_with_guide_symbols,
    vector_glyph_width,
)
from krok_helper.subtitle_render.engine.layout.layout_context import _LAYOUT_PASS
from krok_helper.subtitle_render.engine.layout.line.geometry import (
    line_has_role_labels,
)
from krok_helper.subtitle_render.engine.layout.line.style import style_for_line
from krok_helper.subtitle_render.engine.layout.page.pagination import (
    line_center_override,
    renderable_page_lines,
)
from krok_helper.subtitle_render.engine.render_progress import yield_to_gui
from krok_helper.subtitle_render.engine.ruby import (
    active_rubies_for_line,
    ruby_char_gaps,
)
from krok_helper.subtitle_render.engine.text import (
    build_font,
    build_latin_font,
    build_role_text_layout,
    char_layout_width,
    line_text_width,
    make_font_for,
    role_char_geometry_by_index,
    style_for_role_in_layout,
)


def lane_slot(
    style: Style,
    lane: int | None,
    page_line_count: int | None = None,
) -> int:
    """Resolve the shared alignment/offset slot, including bottom short pages."""
    alignments = style.line_alignments or ["left"]
    count = len(alignments)
    index = 0 if lane is None else max(int(lane), 0)
    if (
        page_line_count is not None
        and style.line_y_position == "bottom"
        and 0 < int(page_line_count) < count
    ):
        index = max(count - int(page_line_count) + index, 0)
    return min(index, count - 1)


def lane_alignment(
    style: Style, lane: int | None, page_line_count: int | None = None,
) -> str:
    return (style.line_alignments or ["left"])[lane_slot(style, lane, page_line_count)]


def lane_offset_x(
    style: Style, lane: int | None, page_line_count: int | None = None,
) -> int:
    index = lane_slot(style, lane, page_line_count)
    return int(style.line_offsets_x[index]) if index < len(style.line_offsets_x) else 0


def line_lane_alignment(
    track: TimingTrack,
    line: TimingLine,
    style: Style,
    lane: int | None,
) -> str:
    """Resolve one line's alignment, including bottom-anchored short pages."""
    if not bottom_short_page_alignment(style):
        return lane_alignment(style, lane)
    page = renderable_page_lines(track, line, style)
    return lane_alignment(style, lane, len(page) if page else None)


def bottom_short_page_alignment(style: Style) -> bool:
    """Return whether short pages consume alignment slots from the bottom."""
    return (
        style.dual_line_layout
        and style.line_horizontal_layout == "asymmetric"
        and style.line_y_position == "bottom"
        and len(style.line_alignments or []) > 1
    )


def layout_page_lines(
    track: TimingTrack,
    line: TimingLine,
    style: Style,
) -> list[tuple[TimingLine, int]] | None:
    """Return page members only when horizontal policies need them."""
    if not style.dual_line_layout or style.line_horizontal_layout != "asymmetric":
        return None
    needs_smart = style.smart_horizontal != "none" and not style.vertical
    if not needs_smart and not bottom_short_page_alignment(style):
        return None
    return renderable_page_lines(track, line, style)


def resolve_line_x(
    img_w: int,
    total_w: int,
    style: Style,
    lane: int | None,
    *,
    center_override: bool = False,
    page_line_count: int | None = None,
) -> int:
    if center_override:
        return (img_w - total_w) // 2
    if style.line_horizontal_layout == "per_row":
        align, offset_x, _ = row_layout_params(style, lane)
        return aligned_x0(img_w, total_w, align) + offset_x
    if style.line_horizontal_layout == "center":
        return (img_w - total_w) // 2
    if style.dual_line_layout and lane is not None:
        align = lane_alignment(style, lane, page_line_count)
        margin = style.horizontal_margin_px
        if align == "left":
            return margin
        if align == "right":
            return img_w - margin - total_w
        return (img_w - total_w) // 2
    return (img_w - total_w) // 2


def aligned_x0(img_w: int, total_w: int, align: str) -> int:
    if align == "center":
        return (img_w - total_w) // 2
    if align == "right":
        return img_w - total_w
    return 0


def row_layout_params(style: Style, lane: int | None) -> tuple[str, int, int]:
    if lane == 1:
        return style.row2_align, style.row2_offset_x, style.row2_offset_y
    return style.row1_align, style.row1_offset_x, style.row1_offset_y


def line_total_width(
    line: TimingLine,
    style: Style,
    rubies: list[RubyAnnotation] | None = None,
    *,
    track: TimingTrack | None = None,
) -> int:
    """Measure the N3 horizontal line box, optionally including ruby.

    同一 :func:`layout_pass` 区间内按入参身份缓存：SmartHorizon 的页级
    宽度盘点、余白诊断与碰撞测量会在一次区间里对同一行反复测量，字符级
    ``_CHAR_GLYPH_CACHE`` 只省 path 构造，省不掉逐字符的 Python 循环。
    区间契约保证 track/style/rubies 不可变，身份键即语义键。
    """

    cache = getattr(_LAYOUT_PASS, "line_widths", None)
    if cache is None:
        return _line_total_width_uncached(line, style, rubies, track=track)
    key = (id(line), id(style), id(rubies), id(track))
    hit = cache.get(key)
    if hit is None:
        hit = _line_total_width_uncached(line, style, rubies, track=track)
        cache[key] = hit
        # 键里有 id()：存住入参，避免回收后地址被复用。
        _LAYOUT_PASS.lines.append(line)
        _LAYOUT_PASS.styles.append(style)
        _LAYOUT_PASS.ruby_lists.append(rubies)
        if track is not None:
            _LAYOUT_PASS.tracks.append(track)
    return hit


def _line_total_width_uncached(
    line: TimingLine,
    style: Style,
    rubies: list[RubyAnnotation] | None = None,
    *,
    track: TimingTrack | None = None,
) -> int:
    source_line = line
    line = render_line_with_guide_symbols(line)
    if line_has_role_labels(line):
        role_layout = build_role_text_layout(line, style, x0=0, baseline_y=0)
        char_widths, _ranges = role_char_geometry_by_index(line, role_layout)
        text_width = role_layout.total_width
    else:
        font = build_font(style)
        metrics = QFontMetrics(font)
        latin_font = build_latin_font(style)
        font_for = make_font_for(style, font, latin_font)
        latin_metrics = QFontMetrics(latin_font) if font_for is not None else metrics
        char_widths = []
        for char in line.chars:
            # 逐字符测量是余白诊断整轨重算的热点循环；节流后的让出保证
            # 后台线程长段执行不饿死 GUI 线程（与 value_signature 同口径）。
            yield_to_gui()
            char_widths.append(
                vector_glyph_width(
                    char.vector_glyph,
                    style_for_role_in_layout(style, char.role_label),
                )
                if char.vector_glyph is not None
                else char_layout_width(
                    char.text,
                    font,
                    metrics,
                    latin_metrics,
                    font_for,
                    style,
                )
            )
        text_width = line_text_width(char_widths, style)
    left_ext = right_ext = 0
    gap_total = 0
    if rubies:
        active = active_rubies_for_line(rubies, source_line)
        if active:
            gaps, ruby_left, ruby_right = ruby_char_gaps(
                line,
                char_widths,
                active,
                style,
            )
            gap_total = sum(gaps)
            left_ext = ruby_left
            right_ext = ruby_right
    if track is not None:
        from krok_helper.subtitle_render.engine.render.elements.signal import (
            inline_volume_geometry,
        )
        inline = inline_volume_geometry(track, source_line, style)
        if inline is not None:
            left_ext = max(left_ext, inline[1].group_width)
    return max(
        int(round(text_width + gap_total + left_ext + right_ext)),
        1,
    )


def smart_horizontal_dx(
    img_w: int,
    total_w: int,
    track: TimingTrack,
    line: TimingLine,
    style: Style,
    lane: int | None,
    *,
    center_override: bool,
    page: list[tuple[TimingLine, int]] | None = None,
) -> int:
    """Resolve N3 SmartHorizon's secondary horizontal displacement."""
    mode = style.smart_horizontal
    if mode == "none" or style.vertical or center_override:
        return 0
    if not style.dual_line_layout or style.line_horizontal_layout != "asymmetric":
        return 0
    if page is None:
        page = renderable_page_lines(track, line, style)
    page_rows = len(page) if page else None
    own_align = lane_alignment(style, lane, page_rows)
    if own_align == "center":
        return 0
    margin = style.horizontal_margin_px
    font = n3_smart_font_size(line, style)
    base_x = resolve_line_x(
        img_w,
        total_w,
        style,
        lane,
        center_override=False,
        page_line_count=page_rows,
    )
    if page is not None and len(page) <= 1:
        return (img_w - total_w) // 2 - base_x

    if mode == "center_position":
        threshold = img_w // 2 + font // 2 - total_w
        if threshold <= margin:
            return 0
        if own_align == "right":
            return (img_w // 2 - font // 2) - base_x
        return threshold - base_x

    if page is None:
        return 0
    if page:
        page_head, _page_head_lane = page[0]
        font = n3_smart_font_size(
            page_head,
            style_for_line(style, page_head),
        )
    max_widths = {"left": 0, "center": 0, "right": 0}
    for page_line, page_lane in page:
        page_style = style_for_line(style, page_line)
        if line_center_override(track, page_line, page_style):
            align = "center"
        else:
            align = lane_alignment(page_style, page_lane, page_rows)
        width = (
            total_w
            if page_line is line
            else line_total_width(page_line, page_style, track.rubies, track=track)
        )
        max_widths[align] = max(max_widths[align], width)
    if max_widths["left"] == 0 or max_widths["right"] == 0:
        return 0
    slack = (
        img_w
        - margin * 2
        - max_widths["left"]
        - max_widths["center"]
        - max_widths["right"]
        + font
    )
    if slack <= 0:
        return 0
    return -(slack // 2) if own_align == "right" else slack // 2


def n3_smart_font_size(line: TimingLine, style: Style) -> int:
    """Return the first rendered character's Japanese font-slot size."""
    render_line = render_line_with_guide_symbols(line)
    if not render_line.chars:
        return max(int(style.font_size_px), 1)
    first_style = style_for_role_in_layout(
        style,
        render_line.chars[0].role_label,
    )
    return max(int(first_style.font_size_px), 1)


def resolve_line_x_smart(
    img_w: int,
    total_w: int,
    track: TimingTrack,
    line: TimingLine,
    style: Style,
    lane: int | None,
    *,
    center_override: bool = False,
) -> int:
    page = layout_page_lines(track, line, style)
    x = resolve_line_x(
        img_w,
        total_w,
        style,
        lane,
        center_override=center_override,
        page_line_count=len(page) if page else None,
    )
    offset = lane_offset_x(style, lane, len(page) if page else None)
    return x + offset + smart_horizontal_dx(
        img_w,
        total_w,
        track,
        line,
        style,
        lane,
        center_override=center_override,
        page=page,
    )
