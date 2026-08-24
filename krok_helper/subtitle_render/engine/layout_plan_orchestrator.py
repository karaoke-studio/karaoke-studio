"""Orchestrate a shared layout plan through explicit resolver contracts."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from krok_helper.subtitle_render.engine.display_schedule import (
    display_schedule_from_items,
    single_line_display_schedule,
)
from krok_helper.subtitle_render.engine.guide_semantics import (
    render_line_with_guide_symbols,
)
from krok_helper.subtitle_render.engine.layout_plan import (
    LayoutOffsetWindow,
    TrackLayoutPlan,
)
from krok_helper.subtitle_render.engine.layout_plan_builder import (
    assemble_track_layout_plan,
)
from krok_helper.subtitle_render.engine.layout_plan_cache import (
    cached_track_layout_plan,
    store_track_layout_plan,
)
from krok_helper.subtitle_render.engine.line_style import (
    style_for_line,
    style_for_line_display_window,
)
from krok_helper.subtitle_render.engine.signal_semantics import (
    display_style_for_signal_window,
)
from krok_helper.subtitle_render.engine.timeline import DisplayLine
from krok_helper.subtitle_render.engine.value_signature import value_signature
from krok_helper.subtitle_render.models import Style
from krok_helper.subtitle_render.timing import TimingLine, TimingTrack


class DisplayLinesResolver(Protocol):
    def __call__(
        self,
        track: TimingTrack,
        style: Style,
        *,
        logical_w: int | None = None,
        logical_h: int | None = None,
    ) -> list[DisplayLine]: ...


class PageOffsetWindowsResolver(Protocol):
    def __call__(
        self,
        logical_w: int,
        logical_h: int,
        track: TimingTrack,
        style: Style,
    ) -> dict[int, tuple[LayoutOffsetWindow, ...]]: ...


class CharIntervalsResolver(Protocol):
    def __call__(
        self,
        line: TimingLine,
        style: Style,
    ) -> list[tuple[int, int]]: ...


class GuideAnchorBoundsResolver(Protocol):
    def __call__(
        self,
        track: TimingTrack,
        line: TimingLine,
        style: Style,
    ) -> tuple[float, float] | None: ...


@dataclass(frozen=True)
class LayoutPlanResolvers:
    """Concrete capabilities required to resolve one immutable track plan."""

    display_lines: DisplayLinesResolver
    page_offset_windows: PageOffsetWindowsResolver
    char_intervals: CharIntervalsResolver
    guide_anchor_bounds: GuideAnchorBoundsResolver
    cache_enabled: Callable[[], bool]


def resolve_track_layout_plan(
    track: TimingTrack,
    style: Style,
    resolvers: LayoutPlanResolvers,
    *,
    logical_w: int | None = None,
    logical_h: int | None = None,
) -> TrackLayoutPlan:
    """Resolve frame-independent line semantics for CPU and GPU consumers."""
    cache_key = (
        logical_w,
        logical_h,
        id(track),
        value_signature(track),
        value_signature(style),
    )
    if resolvers.cache_enabled():
        cached = cached_track_layout_plan(cache_key)
        if cached is not None:
            return cached

    display_style = display_style_for_signal_window(style)
    display_items: list[DisplayLine] = []
    if display_style.dual_line_layout:
        display_items = resolvers.display_lines(
            track,
            display_style,
            logical_w=logical_w,
            logical_h=logical_h,
        )
        schedule = display_schedule_from_items(track, display_items)
    else:
        schedule = single_line_display_schedule(track, display_style)
    page_offset_windows = (
        resolvers.page_offset_windows(
            max(int(logical_w), 1),
            max(int(logical_h), 1),
            track,
            display_style,
        )
        if logical_w is not None and logical_h is not None
        else {}
    )
    render_lines = [render_line_with_guide_symbols(line) for line in track.lines]
    layout_styles = [style_for_line(style, line) for line in track.lines]
    resolved_intervals = [
        resolvers.char_intervals(line, style) for line in render_lines
    ]
    guide_anchor_bounds = [
        resolvers.guide_anchor_bounds(track, line, style) for line in track.lines
    ]
    animation_styles = [
        style_for_line_display_window(
            style,
            line,
            schedule[index][1] if index in schedule else None,
            schedule[index][2] if index in schedule else None,
        )
        for index, line in enumerate(track.lines)
    ]

    plan = assemble_track_layout_plan(
        track,
        style,
        logical_w=logical_w,
        logical_h=logical_h,
        display_items=display_items,
        schedule=schedule,
        page_offset_windows=page_offset_windows,
        render_lines=render_lines,
        layout_styles=layout_styles,
        animation_styles=animation_styles,
        resolved_intervals=resolved_intervals,
        guide_anchor_bounds=guide_anchor_bounds,
    )
    if resolvers.cache_enabled():
        # Retain the mutable owners because the key contains track identity.
        store_track_layout_plan(cache_key, track, style, plan)
    return plan


__all__ = ["LayoutPlanResolvers", "resolve_track_layout_plan"]
