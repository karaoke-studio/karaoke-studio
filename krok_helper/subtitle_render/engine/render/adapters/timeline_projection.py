"""Project renderer timing semantics onto the passive timeline UI.

The timeline consumes character visibility intervals, not Painter's caches or
paint orchestration. This adapter projects the shared ruby, text, and layout
contracts without importing the CPU renderer.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from PyQt6.QtGui import QFontMetrics

from krok_helper.subtitle_render.engine.timing.timeline import DisplayLine, compute_char_intervals
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
from krok_helper.subtitle_render.engine.layout.page.plan import resolve_page_plan
from krok_helper.subtitle_render.engine.layout.display.schedule import (
    DisplayLinesResolver,
    display_windows_from_items,
    single_line_display_windows,
)
from krok_helper.subtitle_render.domain.timing import (
    RubyAnnotation,
    TimingLine,
    TimingTrack,
)
from krok_helper.subtitle_render.domain.models import (
    Style,
    effective_karaoke_animation,
    layout_display_name,
    layout_capacity,
    layout_id_for_index,
    style_for_track,
)


@dataclass(frozen=True)
class TimelineRow:
    """Final display row, not the lyric's source index or source track."""

    row: int  # zero-based, as in DisplayLine.lane
    count: int
    layout_id: str
    layout_name: str

    @property
    def label(self) -> str:
        return f"{self.row + 1}/{self.count}"


@dataclass(frozen=True)
class TimelineDisplay:
    windows: dict[int, tuple[int, int]]
    rows: dict[int, TimelineRow]


def timeline_display_for_style(
    track: TimingTrack,
    style: Style,
    *,
    display_lines: DisplayLinesResolver,
    logical_w: int | None = None,
    logical_h: int | None = None,
) -> TimelineDisplay:
    """Resolve windows and row badges from the same render schedule once.

    Keep untruncated authored windows for editing; use the final lane (including
    ForceBottom) for colors. No pagination or row assignment happens in the UI.
    """
    style = style_for_track(style, track)
    if not style.dual_line_layout:
        windows = single_line_display_windows(track, style)
        return TimelineDisplay(
            windows,
            {index: TimelineRow(0, 1, "default", "单行布局") for index in windows},
        )
    items = display_lines(track, style, logical_w=logical_w, logical_h=logical_h)
    index_of = {id(line): index for index, line in enumerate(track.lines)}
    authored_layouts = (
        {
            line.track_line_index: line.layout_id
            for line in resolve_page_plan(track, style).lines
        }
        if track.page_plan is not None else {}
    )
    # Page-head layout governs the page, including legacy mixed-layout inputs.
    heads: dict[tuple[int, int], DisplayLine] = {}
    rows: dict[int, TimelineRow] = {}
    for item in items:
        index = index_of.get(id(item.line))
        if index is None:
            continue
        head = heads.setdefault((item.section_index, item.page_index), item)
        layout_id = authored_layouts.get(
            index, layout_id_for_index(style, head.line.layout_index)
        )
        rows[index] = TimelineRow(
            item.lane, max(layout_capacity(style, layout_id), item.lane + 1),
            layout_id, layout_display_name(style, layout_id),
        )
    return TimelineDisplay(display_windows_from_items(track, items), rows)


def source_char_intervals(
    line: TimingLine,
    end_ms: int,
) -> tuple[tuple[int, int], ...]:
    """Project authored character timing into passive timeline cells."""

    # 整行逆序的行在源加载入口已被镜像理顺为顺序（见
    # normalize_reversed_wipe_lines），这里按普通行投影即可。
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


__all__ = [
    "TimelineDisplay", "TimelineRow", "timeline_display_for_style",
    "resolve_utopia_visual_intervals", "source_char_intervals",
]
