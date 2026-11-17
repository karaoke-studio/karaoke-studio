"""Pure authored lane alignment/offset selection shared by layout adapters."""
from __future__ import annotations
from krok_helper.subtitle_render.domain.models import Style


def lane_slot(
    style: Style,
    lane: int | None,
    page_line_count: int | None = None,
) -> int:
    """Resolve the shared alignment/offset slot, including bottom short pages."""
    alignments = style.line_alignments or ["left"]
    count = len(alignments)
    index = 0 if lane is None else max(int(lane), 0)
    # An unforced single-line page keeps its natural T1 row. Its horizontal
    # alignment and offset must use that same row rather than the bottom slot.
    natural_single = (
        page_line_count == 1 and style.dual_line_layout
        and not style.vertical and not style.force_top_bottom_n3
    )
    if (
        page_line_count is not None
        and style.line_y_position == "bottom"
        and 0 < int(page_line_count) < count
        and not natural_single
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


def row_layout_params(style: Style, lane: int | None) -> tuple[str, int, int]:
    if lane == 1:
        return style.row2_align, style.row2_offset_x, style.row2_offset_y
    return style.row1_align, style.row1_offset_x, style.row1_offset_y


