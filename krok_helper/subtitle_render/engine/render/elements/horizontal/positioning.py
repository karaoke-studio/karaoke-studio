"""Horizontal lane alignment and line-origin policies."""

from __future__ import annotations

from krok_helper.subtitle_render.domain.models import Style
from krok_helper.subtitle_render.domain.timing import TimingLine, TimingTrack
from krok_helper.subtitle_render.engine.layout.page.pagination import (
    renderable_page_lines,
)


def lane_alignment(
    style: Style,
    lane: int | None,
    page_line_count: int | None = None,
) -> str:
    """Resolve N3's alignment slot for a page lane."""
    alignments = style.line_alignments or ["left"]
    count = len(alignments)
    index = 0 if lane is None else max(int(lane), 0)
    if (
        page_line_count is not None
        and style.line_y_position == "bottom"
        and 0 < int(page_line_count) < count
    ):
        index = max(count - int(page_line_count) + index, 0)
    return alignments[min(index, count - 1)]


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
