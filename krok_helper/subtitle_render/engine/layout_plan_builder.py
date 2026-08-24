"""Pure assembly of resolved subtitle semantics into an immutable layout plan."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from krok_helper.subtitle_render.engine.layout_plan import (
    LayoutOffsetWindow,
    LineLayoutPlan,
    TrackLayoutPlan,
)
from krok_helper.subtitle_render.engine.line_pagination import line_center_override
from krok_helper.subtitle_render.engine.line_style import (
    lane_count,
    row_count_resolver,
    style_for_line,
)
from krok_helper.subtitle_render.engine.page_plan import resolve_page_plan
from krok_helper.subtitle_render.engine.timeline import DisplayLine, assign_lanes
from krok_helper.subtitle_render.models import Style
from krok_helper.subtitle_render.timing import TimingLine, TimingTrack


DisplaySchedule = Mapping[int, tuple[int, int | None, int | None]]


def assemble_track_layout_plan(
    track: TimingTrack,
    style: Style,
    *,
    logical_w: int | None,
    logical_h: int | None,
    display_items: Sequence[DisplayLine],
    schedule: DisplaySchedule,
    page_offset_windows: Mapping[int, Sequence[LayoutOffsetWindow]],
    render_lines: Sequence[TimingLine],
    layout_styles: Sequence[Style],
    animation_styles: Sequence[Style],
    resolved_intervals: Sequence[Sequence[tuple[int, int]]],
    guide_anchor_bounds: Sequence[tuple[float, float] | None],
) -> TrackLayoutPlan:
    """Assemble already-resolved timing and geometry into one shared plan."""
    renderable_lines = [
        (index, line)
        for index, line in enumerate(track.lines)
        if not line.is_blank and line.chars
    ]
    lanes, lane_page_starts, lane_page_rows = assign_lanes(
        [line for _, line in renderable_lines],
        lane_count(style),
        row_count_resolver(style),
        section_gap_ms=style.section_gap_ms,
    )
    page_line_counts = {
        track_index: lane_page_rows[render_index]
        for render_index, (track_index, _) in enumerate(renderable_lines)
    }
    authored_lanes = {
        track_index: lanes[render_index]
        for render_index, (track_index, _) in enumerate(renderable_lines)
    }
    if track.page_plan is not None:
        resolved_plan = resolve_page_plan(track, style)
        page_indices = {
            item.track_line_index: item.global_page_index
            for item in resolved_plan.lines
        }
        section_indices = {
            item.track_line_index: item.section_index
            for item in resolved_plan.lines
        }
        page_line_counts = {
            item.track_line_index: item.page_line_count
            for item in resolved_plan.lines
        }
        authored_lanes = {
            item.track_line_index: item.lane for item in resolved_plan.lines
        }
    else:
        page_indices = {
            track_index: lane_page_starts[render_index]
            for render_index, (track_index, _) in enumerate(renderable_lines)
        }
        section_indices: dict[int, int] = {}
        renderable_only = [line for _, line in renderable_lines]
        for render_index, (track_index, _line) in enumerate(renderable_lines):
            page_start = lane_page_starts[render_index]
            page_rows = lane_page_rows[render_index]
            page_head = renderable_only[page_start]
            page_style = style_for_line(style, page_head)
            configured_rows = lane_count(page_style)
            if page_rows >= configured_rows:
                continue
            if page_style.line_y_position == "bottom":
                authored_lanes[track_index] += configured_rows - page_rows
            elif page_style.line_y_position == "center":
                authored_lanes[track_index] += max(
                    (configured_rows - page_rows + 1) // 2,
                    0,
                )

    index_of = {id(line): index for index, line in enumerate(track.lines)}
    display_page_metadata = {
        index_of[id(item.line)]: (
            int(item.page_index),
            int(item.page_line_count),
            int(item.section_index),
        )
        for item in display_items
        if id(item.line) in index_of
    }

    plans = []
    for index, line in enumerate(track.lines):
        lane, display_start, display_end = schedule.get(index, (0, None, None))
        display_page_index, display_page_line_count, display_section_index = (
            display_page_metadata.get(
                index,
                (
                    page_indices.get(index, -1),
                    page_line_counts.get(index, 0),
                    section_indices.get(index, -1),
                ),
            )
        )
        plans.append(
            LineLayoutPlan(
                track_index=index,
                line=line,
                render_line=render_lines[index],
                layout_style=layout_styles[index],
                animation_style=animation_styles[index],
                resolved_intervals=tuple(resolved_intervals[index]),
                guide_anchor_bounds=guide_anchor_bounds[index],
                page_index=page_indices.get(index, -1),
                page_line_count=page_line_counts.get(index, 0),
                section_index=section_indices.get(index, -1),
                display_page_index=display_page_index,
                display_page_line_count=display_page_line_count,
                display_section_index=display_section_index,
                lane=lane,
                layout_lane=authored_lanes.get(index, lane),
                display_start_ms=display_start,
                display_end_ms=display_end,
                center_override=line_center_override(
                    track,
                    line,
                    layout_styles[index],
                ),
                layout_offset_windows=tuple(page_offset_windows.get(index, ())),
            )
        )
    return TrackLayoutPlan(
        layout_semantics=style.layout_semantics,
        logical_width=logical_w,
        logical_height=logical_h,
        lines=tuple(plans),
    )


__all__ = ["assemble_track_layout_plan"]
