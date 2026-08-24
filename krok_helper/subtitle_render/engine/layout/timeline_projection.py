"""Project renderer timing semantics onto the passive timeline UI.

The timeline consumes character visibility intervals, not Painter's font,
Ruby, geometry, or wipe implementation details.  This narrow adapter preserves
the existing Utopia calculation while those semantics are moved out of the CPU
renderer incrementally.
"""

from __future__ import annotations

from collections.abc import Sequence

from PyQt6.QtGui import QFontMetrics

from krok_helper.subtitle_render.engine.timeline import compute_char_intervals
from krok_helper.subtitle_render.engine.text import (
    build_font,
    build_latin_font,
    char_layout_width,
)
from krok_helper.subtitle_render.timing import (
    RubyAnnotation,
    TimingLine,
)
from krok_helper.subtitle_render.models import (
    Style,
    effective_karaoke_animation,
)


def source_char_intervals(
    line: TimingLine,
    end_ms: int,
) -> tuple[tuple[int, int], ...]:
    """Project authored character timing into passive timeline cells."""

    intervals: list[tuple[int, int]] = []
    for index, char in enumerate(line.chars):
        next_ms = (
            line.chars[index + 1].start_ms
            if index + 1 < len(line.chars)
            else end_ms
        )
        if char.pause_release_ms is not None:
            next_ms = min(next_ms, char.pause_release_ms)
        intervals.append((char.start_ms, max(next_ms, char.start_ms)))
    return tuple(intervals)


def resolve_utopia_visual_intervals(
    line: TimingLine,
    end_ms: int,
    style: Style,
    rubies: Sequence[RubyAnnotation],
) -> tuple[tuple[int, int], ...] | None:
    """Return Painter-equivalent Utopia cell windows when they differ.

    ``None`` means the timeline should retain its ordinary source intervals.
    The returned tuple has one interval for every character in ``line``.
    """

    if effective_karaoke_animation(style) != "utopia" or not rubies:
        return None

    # Preserve TimelineView's historical lazy dependency on the renderer.  A
    # normal timeline import must not initialize the Painter/cache stack unless
    # Utopia's visual timing projection is actually requested.
    from krok_helper.subtitle_render.engine.painter import (
        _active_rubies_for_line,
        _char_left_positions,
        _letter_spacing,
        _resolve_char_ruby_groups,
        _style_for_line,
        _utopia_wipe_window_for_index,
    )

    line_style = _style_for_line(style, line)
    active_rubies = _active_rubies_for_line(list(rubies), line)
    if not active_rubies:
        return None
    font = build_font(line_style)
    metrics = QFontMetrics(font)
    latin_font = build_latin_font(line_style)
    latin_metrics = QFontMetrics(latin_font)
    char_widths = [
        char_layout_width(
            char.text,
            font,
            metrics,
            latin_metrics,
            None,
            line_style,
        )
        for char in line.chars
    ]
    intervals = compute_char_intervals(line, char_widths)
    char_lefts = _char_left_positions(
        char_widths,
        0,
        line_style.right_to_left,
        _letter_spacing(line_style),
        n3_no_backtracking=line_style.layout_semantics == "n3_1074",
    )
    char_x_ranges = [
        (left, left + width) for left, width in zip(char_lefts, char_widths)
    ]
    groups = _resolve_char_ruby_groups(active_rubies, line, intervals)
    if not groups:
        return None

    visual_intervals: list[tuple[int, int]] = []
    changed = False
    raw_intervals = source_char_intervals(line, end_ms)
    for index, (fallback_start, fallback_end) in enumerate(raw_intervals):
        start_ms, cell_end_ms = _utopia_wipe_window_for_index(
            line,
            intervals,
            char_x_ranges,
            groups,
            index,
            line_style,
            fallback_start=fallback_start,
            fallback_end=fallback_end,
        )
        if (start_ms, cell_end_ms) != (fallback_start, fallback_end):
            changed = True
        visual_intervals.append((start_ms, max(start_ms, cell_end_ms)))
    return tuple(visual_intervals) if changed else None


__all__ = ["resolve_utopia_visual_intervals", "source_char_intervals"]
