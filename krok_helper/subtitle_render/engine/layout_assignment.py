"""Project-model operations for assigning subtitle layouts to pages."""

from __future__ import annotations

from typing import Optional

from krok_helper.subtitle_render.engine.line_style import (
    lane_count as _lane_count,
    layout_style_for_line as _layout_style_for_line,
    row_count_resolver as _row_count_resolver,
)

from krok_helper.subtitle_render.engine.page_plan import (
    resolve_page_plan,
    set_pages_layout,
    use_default_layouts,
)
from krok_helper.subtitle_render.engine.timeline import assign_lanes
from krok_helper.subtitle_render.timing import (
    TimingLine,
    TimingTrack,
)
from krok_helper.subtitle_render.models import Style, layout_capacity, layout_id_for_index


def apply_layout_to_page(
    track: TimingTrack,
    style: Style,
    track_line_index: int,
    layout_index: int,
) -> list[int]:
    """Apply one layout to every line on the selected line's page."""
    if track.page_plan is not None:
        resolved = resolve_page_plan(track, style)
        resolved_line = resolved.line_for_track_index(track_line_index)
        if resolved_line is None:
            return []
        page = resolved.pages[resolved_line.global_page_index]
        layout_id = layout_id_for_index(style, int(layout_index))
        if layout_capacity(style, layout_id) < page.line_count:
            return []
        changed = set_pages_layout(
            track, style, [resolved_line.global_page_index], layout_id
        )
        return list(page.track_line_indices) if changed else []

    render_positions = [
        index
        for index, line in enumerate(track.lines)
        if not line.is_blank and line.chars
    ]
    render_lines = [track.lines[index] for index in render_positions]
    try:
        render_index = render_positions.index(track_line_index)
    except ValueError:
        return []
    _lanes, page_starts, page_rows = assign_lanes(
        render_lines,
        _lane_count(style),
        _row_count_resolver(style),
        section_gap_ms=style.section_gap_ms,
    )
    page_start = page_starts[render_index]
    page_end = min(page_start + page_rows[render_index], len(render_lines))
    affected: list[int] = []
    for index in range(page_start, page_end):
        line = render_lines[index]
        if line.layout_index != layout_index:
            line.layout_index = layout_index
            affected.append(render_positions[index])
    return affected


def assign_layout_to_all(
    track: TimingTrack, layout_index: int, style: Optional[Style] = None
) -> bool:
    """Apply one layout to every renderable line."""
    if track.page_plan is not None:
        if style is None:
            return False
        resolved = resolve_page_plan(track, style)
        layout_id = layout_id_for_index(style, int(layout_index))
        if any(
            layout_capacity(style, layout_id) < page.line_count
            for page in resolved.pages
        ):
            return False
        return set_pages_layout(
            track,
            style,
            [page.global_page_index for page in resolved.pages],
            layout_id,
        )
    changed = False
    for line in track.lines:
        if line.is_blank or not line.chars:
            continue
        if line.layout_index != layout_index:
            line.layout_index = layout_index
            changed = True
    return changed


def auto_assign_layouts_by_page(track: TimingTrack, style: Style) -> bool:
    """Choose the first compatible layout for each page's line count."""
    if track.page_plan is not None:
        return use_default_layouts(track, style)

    row_counts = [max(len(style.line_alignments), 1)] + [
        max(len(layout.line_alignments), 1) for layout in style.layouts
    ]
    pick_cache: dict[int, int] = {}

    def pick(page_lines: int) -> int:
        if page_lines in pick_cache:
            return pick_cache[page_lines]
        choice = 0
        for wanted in range(page_lines, 0, -1):
            found = next(
                (
                    index
                    for index, count in enumerate(row_counts)
                    if count == wanted
                ),
                None,
            )
            if found is not None:
                choice = found
                break
        pick_cache[page_lines] = choice
        return choice

    render_lines = [line for line in track.lines if not line.is_blank and line.chars]
    if not render_lines:
        return False
    _lanes, page_starts, _page_rows = assign_lanes(
        render_lines,
        _lane_count(style),
        _row_count_resolver(style),
        section_gap_ms=style.section_gap_ms,
    )
    pages: dict[int, list[TimingLine]] = {}
    for line, page_start in zip(render_lines, page_starts):
        pages.setdefault(page_start, []).append(line)

    changed = False
    for page in pages.values():
        index = pick(len(page))
        for line in page:
            if line.layout_index != index:
                line.layout_index = index
                changed = True
    return changed
