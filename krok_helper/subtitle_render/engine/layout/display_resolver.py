"""Display-line resolution orchestration independent from concrete rendering."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Hashable
from dataclasses import dataclass, replace

from krok_helper.subtitle_render.engine.layout.layout_diagnostics import (
    TimingCollisionAdjustment,
)
from krok_helper.subtitle_render.engine.layout.display_schedule import (
    apply_constrained_page_sync,
)
from krok_helper.subtitle_render.engine.layout.line.style import (
    auto_entry_reserve_resolver,
    auto_exit_reserve_resolver,
    bottom_align_resolver,
    entry_animation_resolver,
    exit_animation_resolver,
    lane_count,
    line_end_ms,
    line_start_ms,
    row_count_resolver,
    vertical_position_resolver,
)
from krok_helper.subtitle_render.engine.layout.page.placement import (
    LineVisualBand,
    bands_require_separation,
)
from krok_helper.subtitle_render.engine.layout.signal_semantics import (
    signal_head_context,
    signal_lead_in_ms,
)
from krok_helper.subtitle_render.engine.timing.timeline import DisplayLine
from krok_helper.subtitle_render.engine.timing.show_time import protect_time_ms
from krok_helper.subtitle_render.engine.value_signature import value_signature
from krok_helper.subtitle_render.models import Style
from krok_helper.subtitle_render.timing import TimingLine, TimingTrack


DisplayLines = list[DisplayLine]
CollisionPairs = tuple[tuple[int, int], ...]
MeasuredCollisionBand = tuple[int, Hashable, LineVisualBand, int]
MeasuredCollisionBands = list[MeasuredCollisionBand]


class DisplayResolutionCache:
    """Bounded LRU cache that retains each display line's track owner."""

    def __init__(self, max_items: int = 24) -> None:
        self._max_items = max(int(max_items), 1)
        self._entries: OrderedDict[
            Hashable, tuple[object, tuple[DisplayLine, ...]]
        ] = OrderedDict()

    def get(self, key: Hashable) -> DisplayLines | None:
        cached = self._entries.get(key)
        if cached is None:
            return None
        self._entries.move_to_end(key)
        return list(cached[1])

    def put(
        self,
        key: Hashable,
        owner: object,
        display_lines: DisplayLines,
    ) -> None:
        self._entries[key] = (owner, tuple(display_lines))
        self._entries.move_to_end(key)
        while len(self._entries) > self._max_items:
            self._entries.popitem(last=False)

    def clear(self) -> None:
        self._entries.clear()


_DISPLAY_LINE_RESOLUTION_CACHE = DisplayResolutionCache(max_items=24)


def cached_display_line_resolution(key: Hashable) -> DisplayLines | None:
    return _DISPLAY_LINE_RESOLUTION_CACHE.get(key)


def store_display_line_resolution(
    key: Hashable,
    owner: object,
    display_lines: DisplayLines,
) -> None:
    _DISPLAY_LINE_RESOLUTION_CACHE.put(key, owner, display_lines)


def clear_display_line_resolution_cache() -> None:
    _DISPLAY_LINE_RESOLUTION_CACHE.clear()


@dataclass(frozen=True)
class DisplayResolutionPorts:
    """Concrete geometry and timing operations required by the resolver."""

    compute: Callable[..., DisplayLines]
    resolve_timing: Callable[[DisplayLines, bool], DisplayLines]
    collision_pairs: Callable[[DisplayLines], CollisionPairs]
    secondary_collision_pairs: Callable[[DisplayLines], CollisionPairs]
    fill_section_time: Callable[[DisplayLines], DisplayLines]
    apply_animation_guard: Callable[[DisplayLines, bool], DisplayLines]


@dataclass(frozen=True)
class StyleDisplayResolutionPorts:
    """Backend factory for one concrete canvas display-resolution pass."""

    build: Callable[[int, int, dict[str, object]], DisplayResolutionPorts]


def display_line_compute_kwargs(style: Style) -> dict[str, object]:
    """Build the frame-independent timeline configuration for one style."""

    return {
        "lead_in_ms": style.line_lead_in_ms,
        "tail_ms": style.line_tail_ms,
        "lane_gap_ms": style.line_lane_gap_ms,
        "section_gap_ms": style.section_gap_ms,
        "sync_entry": style.sync_entry,
        "sync_ending": style.sync_ending,
        "sync_each_page": style.sync_each_page,
        "auto_fill_section_time": style.auto_fill_section_time,
        "section_ending_mode": style.section_ending_mode,
        "protect_ms": protect_time_ms(
            style.line_lead_in_ms,
            style.line_tail_ms,
            style.line_protect_ms,
        ),
        "lane_count": lane_count(style),
        "row_count_of": row_count_resolver(style),
        "bottom_align_of": bottom_align_resolver(style),
        "vertical_position_of": vertical_position_resolver(style),
        "auto_entry_reserve_ms_of": auto_entry_reserve_resolver(style),
        "auto_exit_reserve_ms_of": auto_exit_reserve_resolver(style),
        "entry_animation_ms_of": entry_animation_resolver(style),
        "exit_animation_ms_of": exit_animation_resolver(style),
    }


@dataclass(frozen=True)
class AnimationGuardPorts:
    """Geometry and animation measurements needed by the timing guard."""

    entry_animation_ms: Callable[[TimingLine], int]
    exit_animation_ms: Callable[[TimingLine], int]
    measure: Callable[[DisplayLines, str], MeasuredCollisionBands]
    retime: Callable[
        [MeasuredCollisionBands, DisplayLines, tuple[int, ...], str],
        MeasuredCollisionBands | None,
    ]


def apply_animation_time_guard(
    style: Style,
    display_lines: DisplayLines,
    ports: AnimationGuardPorts,
    *,
    enforce_inter_page_gap: bool,
    adjustments: list[TimingCollisionAdjustment] | None = None,
) -> DisplayLines:
    """Restore animation windows and enforce measured collision separation."""

    if not display_lines:
        return display_lines

    guarded = list(display_lines)
    changed = False
    entry_durations: list[int] = []
    line_starts: list[int] = []
    line_ends: list[int] = []
    for index, item in enumerate(guarded):
        entry_duration = ports.entry_animation_ms(item.line)
        exit_duration = ports.exit_animation_ms(item.line)
        entry_durations.append(entry_duration)
        line_start = line_start_ms(item.line)
        line_end = line_end_ms(item.line)
        line_starts.append(line_start)
        line_ends.append(line_end)

        start = int(item.display_start_ms)
        end = int(item.display_end_ms)
        if item.line.display_start_override_ms is None and entry_duration > 0:
            start = min(start, max(line_start - entry_duration, 0))
        if item.line.display_end_override_ms is None and exit_duration > 0:
            end = max(end, line_end + exit_duration)
        if start != item.display_start_ms or end != item.display_end_ms:
            guarded[index] = replace(
                item,
                display_start_ms=start,
                display_end_ms=max(start, end),
            )
            changed = True

    if not enforce_inter_page_gap or style.allow_inter_page_line_overlap:
        return guarded if changed else display_lines

    time_window = (
        "stable" if style.allow_entry_exit_animation_overlap else "display"
    )
    measured = ports.measure(guarded, time_window)
    for _pass in range(max(len(guarded) * 3, 1)):
        adjusted = False
        changed_index: int | None = None
        for incoming_pos, (
            incoming_index,
            incoming_page,
            incoming_band,
            _incoming_gap,
        ) in enumerate(measured):
            incoming = guarded[incoming_index]
            for previous_index, previous_page, previous_band, _previous_gap in measured[
                :incoming_pos
            ]:
                if previous_page == incoming_page:
                    continue
                previous = guarded[previous_index]
                same_lane = int(previous.lane) == int(incoming.lane)
                if (
                    not same_lane
                    and not bands_require_separation(
                        incoming_band,
                        previous_band,
                        0.0,
                    )
                ):
                    continue
                required_gap = (
                    max(int(style.line_lane_gap_ms), 0) if same_lane else 0
                )
                required_start = int(previous_band.display_end_ms) + required_gap
                if int(incoming_band.display_start_ms) >= required_start:
                    continue
                overlap_ms = required_start - int(incoming_band.display_start_ms)

                if previous.line.display_end_override_ms is None:
                    if time_window == "stable":
                        stable_tail = max(
                            int(previous_band.display_end_ms)
                            - line_ends[previous_index],
                            0,
                        )
                    else:
                        stable_tail = max(
                            int(previous.display_end_ms)
                            - ports.exit_animation_ms(previous.line)
                            - line_ends[previous_index],
                            0,
                        )
                    delta = min(overlap_ms, stable_tail)
                    new_end = int(previous.display_end_ms) - delta
                    if new_end < previous.display_end_ms:
                        if adjustments is not None:
                            adjustments.append(
                                TimingCollisionAdjustment(
                                    previous_index=previous_index,
                                    incoming_index=incoming_index,
                                    boundary="exit",
                                    before_ms=int(previous.display_end_ms),
                                    after_ms=int(new_end),
                                )
                            )
                        guarded[previous_index] = replace(
                            previous,
                            display_end_ms=max(
                                int(previous.display_start_ms),
                                new_end,
                            ),
                        )
                        adjusted = True
                        changed_index = previous_index
                        changed = True
                        break

                if time_window == "stable":
                    stable_lead = max(
                        line_starts[incoming_index]
                        - int(incoming_band.display_start_ms),
                        0,
                    )
                else:
                    stable_lead = max(
                        line_starts[incoming_index]
                        - entry_durations[incoming_index]
                        - int(incoming.display_start_ms),
                        0,
                    )
                if incoming.line.display_start_override_ms is None:
                    delta = min(overlap_ms, stable_lead)
                    new_start = int(incoming.display_start_ms) + delta
                    latest_entry_start = max(
                        line_starts[incoming_index]
                        - entry_durations[incoming_index],
                        0,
                    )
                    new_start = min(new_start, latest_entry_start)
                    if new_start != incoming.display_start_ms:
                        if adjustments is not None:
                            adjustments.append(
                                TimingCollisionAdjustment(
                                    previous_index=previous_index,
                                    incoming_index=incoming_index,
                                    boundary="entry",
                                    before_ms=int(incoming.display_start_ms),
                                    after_ms=int(new_start),
                                )
                            )
                        guarded[incoming_index] = replace(
                            incoming,
                            display_start_ms=new_start,
                        )
                        adjusted = True
                        changed_index = incoming_index
                        changed = True
                        break
            if adjusted:
                break
        if not adjusted:
            break
        if changed_index is not None:
            retimed = ports.retime(
                measured,
                guarded,
                (changed_index,),
                time_window,
            )
            measured = (
                retimed
                if retimed is not None
                else ports.measure(guarded, time_window)
            )
    return guarded if changed else display_lines


def resolve_display_timing(
    style: Style,
    display_lines: DisplayLines,
    animation_ports: AnimationGuardPorts,
    *,
    enforce_inter_page_gap: bool,
    adjustments: list[TimingCollisionAdjustment] | None = None,
) -> DisplayLines:
    """Apply page synchronization before measured animation-window guarding."""

    synchronized = apply_constrained_page_sync(display_lines, style)
    return apply_animation_time_guard(
        style,
        synchronized,
        animation_ports,
        enforce_inter_page_gap=enforce_inter_page_gap,
        adjustments=adjustments,
    )


def resolve_display_lines(
    *,
    avoid_collisions: bool,
    auto_fill_section_time: bool,
    ports: DisplayResolutionPorts,
) -> DisplayLines:
    """Run the stable multi-pass display-line resolution policy.

    The caller supplies rendering-specific measurement operations.  This module
    owns only the ordering and data flow between those operations, so layout
    policy no longer depends on the Painter implementation.
    """

    ideal = ports.compute(
        adjust_same_position=False,
        dynamic_single_page_reflow=not avoid_collisions,
        independent_line_entry=True,
    )
    resolved = ideal
    timing_resolved = False
    if avoid_collisions:
        # ForceBottom inspects the schedule only after automatic timing has
        # separated pages; measuring the raw lead/tail windows latches stale
        # spatial reflow decisions.
        resolved = ports.resolve_timing(resolved, True)
        timing_resolved = True
        force_bottom_pairs = ports.collision_pairs(resolved)
        if force_bottom_pairs:
            resolved = ports.compute(
                adjust_same_position=False,
                force_bottom_pairs=force_bottom_pairs,
                dynamic_single_page_reflow=True,
                independent_line_entry=True,
            )
            resolved = ports.resolve_timing(resolved, True)
        squeeze_pairs = ports.collision_pairs(resolved)
        if squeeze_pairs:
            resolved = ports.compute(
                adjust_same_position=False,
                squeeze_pairs=squeeze_pairs,
                force_bottom_pairs=force_bottom_pairs,
                dynamic_single_page_reflow=True,
                independent_line_entry=True,
            )
            resolved = ports.resolve_timing(resolved, True)
        secondary_pairs = ports.secondary_collision_pairs(resolved)
        if secondary_pairs:
            combined_pairs = tuple(dict.fromkeys((*squeeze_pairs, *secondary_pairs)))
            resolved = ports.compute(
                adjust_same_position=False,
                squeeze_pairs=combined_pairs,
                force_bottom_pairs=force_bottom_pairs,
                dynamic_single_page_reflow=True,
                independent_line_entry=True,
            )
            timing_resolved = False
    if not timing_resolved:
        resolved = ports.resolve_timing(resolved, avoid_collisions)
    # Geometry-dependent section filling is the final layout pass.  It may
    # extend windows, so restore the animation guard without changing geometry.
    if auto_fill_section_time:
        filled = ports.fill_section_time(resolved)
        if filled != resolved:
            resolved = ports.apply_animation_guard(filled, avoid_collisions)
    return resolved


def resolve_display_lines_for_style(
    track: TimingTrack,
    style: Style,
    compute_kwargs: dict[str, object],
    ports: StyleDisplayResolutionPorts,
    *,
    logical_w: int | None = None,
    logical_h: int | None = None,
) -> DisplayLines:
    """Resolve and cache one style's display lines on a normalized canvas."""

    base_kwargs = {
        **compute_kwargs,
        "sync_entry": False,
        "sync_ending": False,
        "auto_fill_section_time": False,
    }
    signal_heads = signal_head_context(track, style)
    if signal_heads is not None:
        base_kwargs["signal_head_indexes"] = signal_heads
        base_kwargs["signal_lead_ms"] = signal_lead_in_ms(style)
    if logical_w is None or logical_h is None:
        default_h = max(int(style.layout_reference_height), 1)
        default_w = max(int(round(default_h * 16 / 9)), 1)
        logical_w = default_w if logical_w is None else logical_w
        logical_h = default_h if logical_h is None else logical_h
    logical_w = max(int(logical_w), 1)
    logical_h = max(int(logical_h), 1)
    cache_key = (
        logical_w,
        logical_h,
        id(track),
        value_signature(track),
        value_signature(style),
    )
    cached = cached_display_line_resolution(cache_key)
    if cached is not None:
        return cached
    resolved = resolve_display_lines(
        avoid_collisions=not style.allow_inter_page_line_overlap,
        auto_fill_section_time=style.auto_fill_section_time,
        ports=ports.build(logical_w, logical_h, base_kwargs),
    )
    store_display_line_resolution(cache_key, track, resolved)
    return resolved


__all__ = [
    "AnimationGuardPorts",
    "DisplayResolutionCache",
    "DisplayResolutionPorts",
    "StyleDisplayResolutionPorts",
    "apply_animation_time_guard",
    "cached_display_line_resolution",
    "clear_display_line_resolution_cache",
    "display_line_compute_kwargs",
    "resolve_display_lines",
    "resolve_display_lines_for_style",
    "resolve_display_timing",
    "store_display_line_resolution",
]
