"""Frame-independent style semantics for one subtitle timing line."""

from __future__ import annotations

from dataclasses import replace
from typing import Callable

from krok_helper.subtitle_render.engine.layout.layout_context import _LAYOUT_PASS
from krok_helper.subtitle_render.engine.timing.show_time import (
    MIN_AUTO_ENTRY_ANIMATION_MS,
    MIN_AUTO_EXIT_ANIMATION_MS,
)
from krok_helper.subtitle_render.engine.style.style_semantics import style_scheme_changes
from krok_helper.subtitle_render.models import (
    LYRICS_LAYOUT_FIELDS,
    Style,
    style_with_line_animation,
)
from krok_helper.subtitle_render.timing import (
    TimingLine,
    timing_line_start_ms,
)


def line_start_ms(line: TimingLine) -> int:
    return timing_line_start_ms(line)


def line_end_ms(line: TimingLine) -> int:
    if line.end_ms is not None:
        return line.end_ms
    return line.chars[-1].start_ms + 1000 if line.chars else 0


def lane_count(style: Style) -> int:
    """Return the configured page row count; single-line mode always uses one."""
    if not style.dual_line_layout:
        return 1
    return max(len(style.line_alignments), 1)


def layout_style_for_line(style: Style, line: TimingLine) -> Style:
    """Apply the layout definition referenced by one line to the base style."""
    index = int(getattr(line, "layout_index", 0) or 0)
    if index <= 0 or index > len(style.layouts):
        return style
    layout = style.layouts[index - 1]
    changes = {
        name: value
        for name in LYRICS_LAYOUT_FIELDS
        if (value := getattr(layout, name)) is not None
    }
    return replace(style, **changes)


def row_count_resolver(
    style: Style,
) -> Callable[[TimingLine], int] | None:
    """Build the timeline callback for per-layout page row counts."""
    if not style.layouts:
        return None
    return lambda line: lane_count(layout_style_for_line(style, line))


def bottom_align_resolver(
    style: Style,
) -> Callable[[TimingLine], bool] | None:
    if style.vertical:
        return None
    return lambda line: layout_style_for_line(style, line).line_y_position == "bottom"


def vertical_position_resolver(
    style: Style,
) -> Callable[[TimingLine], str] | None:
    if style.vertical:
        return None
    return lambda line: layout_style_for_line(style, line).line_y_position


def auto_entry_reserve_ms(style: Style, line: TimingLine) -> int:
    """Return the automatic pre-wipe reserve for this line's entry animation."""

    line_style = style_for_line(style, line)
    duration = max(int(line_style.entry_lead_ms), 0)
    if line_style.entry_anim == "none" or duration <= 0:
        return 0
    return min(duration, MIN_AUTO_ENTRY_ANIMATION_MS)


def auto_entry_reserve_resolver(style: Style) -> Callable[[TimingLine], int]:
    return lambda line: auto_entry_reserve_ms(style, line)


def auto_exit_reserve_ms(style: Style, line: TimingLine) -> int:
    """Keep a short automatic exit visible; explicit shorter values win."""

    line_style = style_for_line(style, line)
    duration = max(int(line_style.exit_fade_ms), 0)
    if line_style.exit_anim == "none" or duration <= 0:
        return 0
    return min(duration, MIN_AUTO_EXIT_ANIMATION_MS)


def auto_exit_reserve_resolver(style: Style) -> Callable[[TimingLine], int]:
    return lambda line: auto_exit_reserve_ms(style, line)


def entry_animation_ms(style: Style, line: TimingLine) -> int:
    line_style = style_for_line(style, line)
    if line_style.entry_anim == "none":
        return 0
    return max(int(line_style.entry_lead_ms), 0)


def entry_animation_resolver(style: Style) -> Callable[[TimingLine], int]:
    return lambda line: entry_animation_ms(style, line)


def exit_animation_ms(style: Style, line: TimingLine) -> int:
    line_style = style_for_line(style, line)
    if line_style.exit_anim == "none":
        return 0
    return max(int(line_style.exit_fade_ms), 0)


def exit_animation_resolver(style: Style) -> Callable[[TimingLine], int]:
    return lambda line: exit_animation_ms(style, line)


def style_for_line(style: Style, line: TimingLine) -> Style:
    """Resolve layout, singer and animation overrides for one timing line."""
    cache = getattr(_LAYOUT_PASS, "line_styles", None)
    if cache is None:
        return _style_for_line_uncached(style, line)
    override = line.animation_override
    cache_key = (
        id(style),
        int(getattr(line, "layout_index", 0) or 0),
        line.singer_id,
        None
        if override is None
        else (
            override.entry_anim,
            int(override.entry_duration_ms),
            override.exit_anim,
            int(override.exit_duration_ms),
            override.karaoke_anim,
        ),
    )
    hit = cache.get(cache_key)
    if hit is not None:
        return hit
    resolved = _style_for_line_uncached(style, line)
    cache[cache_key] = resolved
    # The cache key contains id(style), so retain the owner for the pass.
    _LAYOUT_PASS.styles.append(style)
    return resolved


def _style_for_line_uncached(style: Style, line: TimingLine) -> Style:
    layout_style = layout_style_for_line(style, line)
    if line.singer_id is not None:
        scheme = style.singer_style_overrides.get(line.singer_id)
        if scheme is not None:
            changes = style_scheme_changes(scheme)
            if changes:
                style = replace(style, **changes)
    style = replace(
        style,
        **{name: getattr(layout_style, name) for name in LYRICS_LAYOUT_FIELDS},
    )
    return style_with_line_animation(style, line)


def style_for_line_display_window(
    style: Style,
    line: TimingLine,
    display_start_ms: int | None,
    display_end_ms: int | None,
) -> Style:
    """Clamp line animation durations to its resolved display margins."""
    line_style = style_for_line(style, line)
    start = line_start_ms(line) if display_start_ms is None else int(display_start_ms)
    end = line_end_ms(line) if display_end_ms is None else int(display_end_ms)
    entry_available = max(line_start_ms(line) - start, 0)
    exit_available = max(end - line_end_ms(line), 0)
    return replace(
        line_style,
        entry_lead_ms=min(max(int(line_style.entry_lead_ms), 0), entry_available),
        exit_fade_ms=min(max(int(line_style.exit_fade_ms), 0), exit_available),
    )


__all__ = [
    "auto_entry_reserve_ms",
    "auto_entry_reserve_resolver",
    "auto_exit_reserve_ms",
    "auto_exit_reserve_resolver",
    "bottom_align_resolver",
    "entry_animation_ms",
    "entry_animation_resolver",
    "exit_animation_ms",
    "exit_animation_resolver",
    "lane_count",
    "layout_style_for_line",
    "line_end_ms",
    "line_start_ms",
    "row_count_resolver",
    "style_for_line",
    "style_for_line_display_window",
    "vertical_position_resolver",
]
