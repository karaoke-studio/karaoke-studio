"""Projection of resolved subtitle display windows into stable schedules."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from typing import Protocol

from krok_helper.subtitle_render.engine.layout.line.style import (
    line_end_ms,
    line_start_ms,
    style_for_line_display_window,
)
from krok_helper.subtitle_render.engine.layout.display.signal import (
    signal_head_context,
    signal_lead_in_ms,
)
from krok_helper.subtitle_render.engine.layout.page.placement import (
    LineVisualBand,
    bands_require_separation,
)
from krok_helper.subtitle_render.engine.timing.timeline import (
    DisplayLine,
    apply_display_overrides,
)
from krok_helper.subtitle_render.domain.models import Style
from krok_helper.subtitle_render.domain.timing import TimingTrack


DisplayWindows = dict[int, tuple[int, int]]
DisplaySchedule = dict[int, tuple[int, int, int]]
SyncCollisionBands = dict[int, tuple[LineVisualBand, float]]
"""Per-line static ink geometry keyed by position in the display-line list."""


def collision_time_window_name(style: Style) -> str:
    """Return the collision window convention this style's guard runs on."""

    return "stable" if style.allow_entry_exit_animation_overlap else "display"


def display_line_static_collision_window(
    display_line: DisplayLine,
    style: Style,
) -> tuple[int, int]:
    """Return the non-animation interval used for page collisions."""

    line_style = style_for_line_display_window(
        style,
        display_line.line,
        display_line.display_start_ms,
        display_line.display_end_ms,
    )
    start = int(display_line.display_start_ms)
    end = int(display_line.display_end_ms)
    if line_style.entry_anim != "none":
        start += max(int(line_style.entry_lead_ms), 0)
    if line_style.exit_anim != "none":
        end -= max(int(line_style.exit_fade_ms), 0)
    return start, max(start, end)


def display_line_collision_time_window(
    display_line: DisplayLine,
    style: Style,
    *,
    time_window: str,
) -> tuple[int, int]:
    """Resolve the selected full-display or stable collision interval."""

    if time_window == "display":
        start = int(display_line.display_start_ms)
        end = int(display_line.display_end_ms)
        return start, max(start, end)
    if time_window != "stable":
        raise ValueError(f"Unsupported collision time window: {time_window}")
    return display_line_static_collision_window(display_line, style)


class DisplayLinesResolver(Protocol):
    def __call__(
        self,
        track: TimingTrack,
        style: Style,
        *,
        logical_w: int | None = None,
        logical_h: int | None = None,
    ) -> list[DisplayLine]: ...


@dataclass(frozen=True)
class DisplayScheduleResolvers:
    """Capabilities required to project a style's resolved display lines."""

    display_lines: DisplayLinesResolver


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


def single_visible_display_line(
    track: TimingTrack,
    t_ms: int,
    style: Style,
) -> DisplayLine | None:
    """Select the live line, or the latest lead/tail line, in single-line mode."""

    best_live: DisplayLine | None = None
    best_lead_or_tail: DisplayLine | None = None
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
        sing_start = line_start_ms(line)
        sing_end = line_end_ms(line)
        display_start = max(sing_start - line_lead, 0)
        display_end = sing_end + tail
        display_start, display_end = apply_display_overrides(
            line,
            display_start,
            display_end,
        )
        display_line = DisplayLine(
            line=line,
            lane=0,
            display_start_ms=display_start,
            display_end_ms=display_end,
        )
        if sing_start <= t_ms < sing_end:
            if best_live is None or sing_start >= line_start_ms(best_live.line):
                best_live = display_line
        elif display_start <= t_ms < display_end:
            if (
                best_lead_or_tail is None
                or sing_start >= line_start_ms(best_lead_or_tail.line)
            ):
                best_lead_or_tail = display_line
    return best_live or best_lead_or_tail


def resolve_display_windows(
    track: TimingTrack,
    style: Style,
    resolvers: DisplayScheduleResolvers,
    *,
    logical_w: int | None = None,
    logical_h: int | None = None,
) -> DisplayWindows:
    """Project either dual-line resolution or single-line authored windows."""

    if not style.dual_line_layout:
        return single_line_display_windows(track, style)
    items = resolvers.display_lines(
        track,
        style,
        logical_w=logical_w,
        logical_h=logical_h,
    )
    return display_windows_from_items(track, items)


def resolve_display_schedule(
    track: TimingTrack,
    style: Style,
    resolvers: DisplayScheduleResolvers,
    *,
    logical_w: int | None = None,
    logical_h: int | None = None,
) -> DisplaySchedule:
    """Project line lanes and windows without knowing the render backend."""

    if not style.dual_line_layout:
        return single_line_display_schedule(track, style)
    items = resolvers.display_lines(
        track,
        style,
        logical_w=logical_w,
        logical_h=logical_h,
    )
    return display_schedule_from_items(track, items)


def resolve_visible_display_lines(
    track: TimingTrack,
    t_ms: int,
    style: Style,
    resolvers: DisplayScheduleResolvers,
    *,
    logical_w: int | None = None,
    logical_h: int | None = None,
) -> list[DisplayLine]:
    """Return visible display lines through one single/dual-line boundary."""

    if not style.dual_line_layout:
        display_line = single_visible_display_line(track, t_ms, style)
        return [] if display_line is None else [display_line]
    return [
        item
        for item in resolvers.display_lines(
            track,
            style,
            logical_w=logical_w,
            logical_h=logical_h,
        )
        if item.display_start_ms <= t_ms < item.display_end_ms
    ]


def extend_page_display_boundary(
    display_lines: list[DisplayLine],
    indices: tuple[int, ...],
    *,
    start_ms: int | None = None,
    end_ms: int | None = None,
) -> list[DisplayLine]:
    """Extend automatic page members toward a shared display boundary."""

    changed = list(display_lines)
    for index in indices:
        item = changed[index]
        changed[index] = replace(
            item,
            display_start_ms=(
                item.display_start_ms
                if start_ms is None
                else min(int(item.display_start_ms), int(start_ms))
            ),
            display_end_ms=(
                item.display_end_ms
                if end_ms is None
                else max(int(item.display_end_ms), int(end_ms))
            ),
        )
    return changed


def _entry_floor_ms(
    resolved: list[DisplayLine],
    index: int,
    page_id: tuple[int, int],
    style: Style,
    collision_bands: SyncCollisionBands | None,
    time_window: str,
) -> int | None:
    """Earliest entry for one line that leaves every earlier page untouched.

    Synchronization may only spend room that is already free.  The floor is the
    same separation the collision guard would demand -- an earlier page's
    resolved exit plus ``IntervalTime`` at a shared screen position -- so a page
    that cannot reach its shared entry instant stops here instead of buying the
    difference from the previous line's exit.
    """

    incoming = resolved[index]
    lane_gap = max(int(style.line_lane_gap_ms), 0)

    def band_of(position: int) -> LineVisualBand | None:
        if collision_bands is None:
            return None
        entry = collision_bands.get(position)
        return None if entry is None else entry[0]

    incoming_band = band_of(index)
    floor: int | None = None
    for other_index in range(index):
        other = resolved[other_index]
        if (int(other.section_index), int(other.page_index)) == page_id:
            continue
        same_lane = int(other.lane) == int(incoming.lane)
        if not same_lane:
            other_band = band_of(other_index)
            if incoming_band is None or other_band is None:
                # Without measured geometry only a shared lane is a known
                # conflict; assuming more would suppress legitimate sync.
                continue
            if not bands_require_separation(incoming_band, other_band, 0.0):
                continue
        candidate = (
            int(
                display_line_collision_time_window(
                    other, style, time_window=time_window
                )[1]
            )
            + (lane_gap if same_lane else 0)
        )
        if floor is None or candidate > floor:
            floor = candidate
    if floor is None:
        return None
    # The floor is expressed in collision-window time; shift it back onto the
    # display window the caller actually writes.
    collision_start = int(
        display_line_collision_time_window(
            incoming, style, time_window=time_window
        )[0]
    )
    return floor - (collision_start - int(incoming.display_start_ms))


def apply_constrained_page_sync(
    display_lines: list[DisplayLine],
    style: Style,
    *,
    collision_bands: SyncCollisionBands | None = None,
    enforce_inter_page_gap: bool = True,
) -> list[DisplayLine]:
    """Apply configured entry/ending synchronization within section bounds.

    Each automatic line extends toward its page's shared boundary as far as the
    neighbouring pages allow.  Entry synchronization is clamped up front against
    the earlier pages' resolved exits, so a page that cannot reach the shared
    instant simply stops at that limit; it never hands the collision guard an
    overdraft to settle out of a neighbour's window.  Ending synchronization
    stays guard-mediated: the extended line is itself the outgoing side there,
    so the guard already rolls its own grant back before touching anything else.
    """

    if not display_lines or not (style.sync_entry or style.sync_ending):
        return display_lines
    baseline = list(display_lines)
    page_order: list[tuple[int, int]] = []
    page_indices: dict[tuple[int, int], list[int]] = {}
    for index, item in enumerate(baseline):
        page_id = (int(item.section_index), int(item.page_index))
        if page_id not in page_indices:
            page_order.append(page_id)
            page_indices[page_id] = []
        page_indices[page_id].append(index)

    resolved = list(baseline)
    constrain_entry = bool(enforce_inter_page_gap) and not bool(
        style.allow_inter_page_line_overlap
    )
    time_window = collision_time_window_name(style)
    first_page_by_section: dict[int, tuple[int, int]] = {}
    last_page_by_section: dict[int, tuple[int, int]] = {}
    for page_id in page_order:
        section_index = page_id[0]
        first_page_by_section.setdefault(section_index, page_id)
        last_page_by_section[section_index] = page_id
    for page_id in page_order:
        indices = tuple(page_indices[page_id])
        sync_entry_here = style.sync_entry and (
            style.sync_each_page
            or first_page_by_section.get(page_id[0]) == page_id
        )
        sync_ending_here = style.sync_ending and (
            style.sync_each_page
            or last_page_by_section.get(page_id[0]) == page_id
        )
        if sync_entry_here:
            automatic = tuple(
                index
                for index in indices
                if resolved[index].line.display_start_override_ms is None
            )
            if automatic:
                page_target = min(
                    int(resolved[index].display_start_ms) for index in indices
                )
                for index in automatic:
                    floor = (
                        None
                        if not constrain_entry
                        else _entry_floor_ms(
                            resolved,
                            index,
                            page_id,
                            style,
                            collision_bands,
                            time_window,
                        )
                    )
                    resolved = extend_page_display_boundary(
                        resolved,
                        (index,),
                        start_ms=(
                            page_target
                            if floor is None
                            else max(page_target, floor)
                        ),
                    )

        if sync_ending_here:
            automatic = tuple(
                index
                for index in indices
                if resolved[index].line.display_end_override_ms is None
            )
            if automatic:
                page_target = max(
                    int(resolved[index].display_end_ms) for index in indices
                )
                resolved = extend_page_display_boundary(
                    resolved,
                    automatic,
                    end_ms=page_target,
                )
    return resolved


__all__ = [
    "DisplaySchedule",
    "SyncCollisionBands",
    "DisplayScheduleResolvers",
    "DisplayWindows",
    "apply_constrained_page_sync",
    "collision_time_window_name",
    "display_line_collision_time_window",
    "display_line_static_collision_window",
    "display_schedule_from_items",
    "display_windows_from_items",
    "extend_page_display_boundary",
    "resolve_display_schedule",
    "resolve_visible_display_lines",
    "resolve_display_windows",
    "single_line_display_schedule",
    "single_line_display_windows",
    "single_visible_display_line",
]
