"""Frame-time projections of an immutable shared subtitle layout plan."""

from __future__ import annotations

from krok_helper.subtitle_render.engine.layout_plan import TrackLayoutPlan
from krok_helper.subtitle_render.engine.timeline import DisplayLine


def visible_lines_from_layout_plan(
    plan: TrackLayoutPlan,
    t_ms: int,
) -> list[DisplayLine]:
    """Project planned line windows into the display lines visible now."""
    return [
        DisplayLine(
            line=item.line,
            lane=item.lane,
            display_start_ms=item.display_start_ms,
            display_end_ms=item.display_end_ms,
            section_index=item.display_section_index,
            page_index=item.display_page_index,
            page_line_count=item.display_page_line_count,
        )
        for item in plan.lines
        if item.display_start_ms is not None
        and item.display_end_ms is not None
        and item.display_start_ms <= t_ms < item.display_end_ms
    ]


def active_page_offsets_from_layout_plan(
    plan: TrackLayoutPlan,
    t_ms: int,
) -> dict[int, tuple[float, float]]:
    """Select each source line's page-offset window active now."""
    resolved: dict[int, tuple[float, float]] = {}
    for item in plan.lines:
        selected = next(
            (
                window
                for window in item.layout_offset_windows
                if window[0] <= int(t_ms) < window[1]
            ),
            None,
        )
        if selected is not None:
            resolved[item.track_index] = (selected[2], selected[3])
    return resolved


__all__ = [
    "active_page_offsets_from_layout_plan",
    "visible_lines_from_layout_plan",
]
