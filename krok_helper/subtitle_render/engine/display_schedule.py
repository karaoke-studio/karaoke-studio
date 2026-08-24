"""Projection of resolved subtitle display windows into stable schedules."""

from __future__ import annotations

from collections.abc import Iterable

from krok_helper.subtitle_render.engine.line_style import line_end_ms, line_start_ms
from krok_helper.subtitle_render.engine.signal_semantics import (
    signal_head_context,
    signal_lead_in_ms,
)
from krok_helper.subtitle_render.engine.timeline import (
    DisplayLine,
    apply_display_overrides,
)
from krok_helper.subtitle_render.models import Style
from krok_helper.subtitle_render.timing import TimingTrack


DisplayWindows = dict[int, tuple[int, int]]
DisplaySchedule = dict[int, tuple[int, int, int]]


def single_line_display_windows(
    track: TimingTrack,
    style: Style,
) -> DisplayWindows:
    """Resolve display windows for renderable lines in single-line mode."""
    windows: DisplayWindows = {}
    lead = max(style.line_lead_in_ms, 0)
    tail = max(style.line_tail_ms, 0)
    signal_heads = signal_head_context(track, style)
    signal_lead = signal_lead_in_ms(style) if signal_heads is not None else 0
    for index, line in enumerate(track.lines):
        if line.is_blank or not line.chars:
            continue
        line_lead = (
            max(lead, signal_lead)
            if signal_heads is not None and index in signal_heads
            else lead
        )
        display_start = max(line_start_ms(line) - line_lead, 0)
        display_end = line_end_ms(line) + tail
        windows[index] = apply_display_overrides(line, display_start, display_end)
    return windows


def display_windows_from_items(
    track: TimingTrack,
    items: Iterable[DisplayLine],
) -> DisplayWindows:
    """Project resolved dual-line items to source-line display windows."""
    index_of = {id(line): index for index, line in enumerate(track.lines)}
    return {
        index_of[id(item.line)]: (
            int(item.display_start_ms),
            int(item.display_end_ms),
        )
        for item in items
        if id(item.line) in index_of
    }


def display_schedule_from_items(
    track: TimingTrack,
    items: Iterable[DisplayLine],
) -> DisplaySchedule:
    """Project resolved dual-line items to source-line lane/window schedules."""
    index_of = {id(line): index for index, line in enumerate(track.lines)}
    return {
        index_of[id(item.line)]: (
            int(item.lane),
            int(item.display_start_ms),
            int(item.display_end_ms),
        )
        for item in items
        if id(item.line) in index_of
    }


def single_line_display_schedule(
    track: TimingTrack,
    style: Style,
) -> DisplaySchedule:
    return {
        index: (0, start, end)
        for index, (start, end) in single_line_display_windows(track, style).items()
    }


__all__ = [
    "DisplaySchedule",
    "DisplayWindows",
    "display_schedule_from_items",
    "display_windows_from_items",
    "single_line_display_schedule",
    "single_line_display_windows",
]
