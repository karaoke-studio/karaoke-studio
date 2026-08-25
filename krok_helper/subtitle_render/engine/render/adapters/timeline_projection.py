"""Project renderer timing semantics onto the passive timeline UI.

The timeline consumes character visibility intervals, not Painter's caches or
paint orchestration. This adapter projects the shared ruby, text, and layout
contracts without importing the CPU renderer.
"""

from __future__ import annotations

from collections.abc import Sequence

from PyQt6.QtGui import QFontMetrics

from krok_helper.subtitle_render.engine.timing.timeline import compute_char_intervals
from krok_helper.subtitle_render.engine.text import (
    build_font,
    build_latin_font,
    char_left_positions,
    char_layout_width,
    letter_spacing,
)
from krok_helper.subtitle_render.engine.ruby import (
    active_rubies_for_line,
    resolve_char_ruby_groups,
    utopia_wipe_window_for_index,
)
from krok_helper.subtitle_render.engine.layout.line.style import style_for_line
from krok_helper.subtitle_render.domain.timing import (
    RubyAnnotation,
    TimingLine,
)
from krok_helper.subtitle_render.domain.models import (
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

    line_style = style_for_line(style, line)
    active_rubies = active_rubies_for_line(list(rubies), line)
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
    char_lefts = char_left_positions(
        char_widths,
        0,
        line_style.right_to_left,
        letter_spacing(line_style),
        n3_no_backtracking=line_style.layout_semantics == "n3_1074",
    )
    char_x_ranges = [
        (left, left + width) for left, width in zip(char_lefts, char_widths)
    ]
    groups = resolve_char_ruby_groups(active_rubies, line, intervals)
    if not groups:
        return None

    visual_intervals: list[tuple[int, int]] = []
    changed = False
    raw_intervals = source_char_intervals(line, end_ms)
    for index, (fallback_start, fallback_end) in enumerate(raw_intervals):
        start_ms, cell_end_ms = utopia_wipe_window_for_index(
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
