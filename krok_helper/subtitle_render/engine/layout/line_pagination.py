"""Frame-independent page membership semantics for subtitle timing lines."""

from __future__ import annotations

from krok_helper.subtitle_render.engine.layout.layout_context import _LAYOUT_PASS
from krok_helper.subtitle_render.engine.layout.line_style import lane_count, row_count_resolver
from krok_helper.subtitle_render.engine.timeline import assign_lanes
from krok_helper.subtitle_render.models import Style
from krok_helper.subtitle_render.timing import TimingLine, TimingTrack


def _page_lines_style_key(style: Style) -> tuple:
    """Return only style values that can change renderable page membership."""
    return (
        bool(style.dual_line_layout),
        len(style.line_alignments or ()),
        int(style.section_gap_ms),
        tuple(len(layout.line_alignments or ()) for layout in style.layouts),
    )


def renderable_page_map(
    track: TimingTrack, style: Style
) -> dict[int, tuple[tuple[TimingLine, int], ...]]:
    """Map each renderable line identity to the lines and lanes on its page."""
    cache = getattr(_LAYOUT_PASS, "page_maps", None)
    cache_key = (id(track), _page_lines_style_key(style)) if cache is not None else None
    if cache is not None:
        hit = cache.get(cache_key)
        if hit is not None:
            return hit
    render_lines = [item for item in track.lines if not item.is_blank and item.chars]
    lanes, page_starts, page_rows = assign_lanes(
        render_lines,
        lane_count(style),
        row_count_resolver(style),
        section_gap_ms=style.section_gap_ms,
    )
    page_map: dict[int, tuple[tuple[TimingLine, int], ...]] = {}
    for index, item in enumerate(render_lines):
        page_start = page_starts[index]
        page_end = min(page_start + page_rows[index], len(render_lines))
        page_map[id(item)] = tuple(
            (render_lines[i], lanes[i]) for i in range(page_start, page_end)
        )
    if cache is not None:
        # The key contains id(track), so retain the owner for the pass.
        cache[cache_key] = page_map
        _LAYOUT_PASS.tracks.append(track)
    return page_map


def renderable_page_lines(
    track: TimingTrack,
    line: TimingLine,
    style: Style,
) -> list[tuple[TimingLine, int]] | None:
    """Return the renderable lines and lanes sharing one line's page."""
    page = renderable_page_map(track, style).get(id(line))
    return None if page is None else list(page)


def line_center_override(track: TimingTrack, line: TimingLine, style: Style) -> bool:
    """Return whether N3 SmartHorizon centers a single-line page."""
    if style.smart_horizontal == "none":
        return False
    if not style.dual_line_layout or style.line_horizontal_layout != "asymmetric":
        return False
    page = renderable_page_lines(track, line, style)
    return page is not None and len(page) == 1


__all__ = [
    "line_center_override",
    "renderable_page_lines",
    "renderable_page_map",
]
