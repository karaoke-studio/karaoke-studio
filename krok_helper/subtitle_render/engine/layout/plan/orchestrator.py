"""Orchestrate a shared layout plan through explicit resolver contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from krok_helper.subtitle_render.engine.layout.display.schedule import (
    display_schedule_from_items,
    single_line_display_schedule,
)
from krok_helper.subtitle_render.engine.guide import (
    render_line_with_guide_symbols,
)
from krok_helper.subtitle_render.engine.layout.plan.model import (
    LayoutOffsetWindow,
    TrackLayoutPlan,
)
from krok_helper.subtitle_render.engine.layout.plan.builder import (
    assemble_track_layout_plan,
)
from krok_helper.subtitle_render.engine.layout.plan.cache import (
    cached_track_layout_plan,
    layout_cache_enabled,
    store_track_layout_plan,
)
from krok_helper.subtitle_render.engine.layout.line.style import (
    style_for_line,
    style_for_line_display_window,
)
from krok_helper.subtitle_render.engine.layout.layout_context import layout_pass
from krok_helper.subtitle_render.engine.render_progress import report_render_progress
from krok_helper.subtitle_render.engine.layout.display.section_edges import (
    section_edge_context,
)
from krok_helper.subtitle_render.engine.layout.display.signal import (
    display_style_for_signal_window,
)
from krok_helper.subtitle_render.engine.layout.line.qt_geometry import (
    resolved_char_intervals_for_line,
    resolved_guide_anchor_bounds_for_line,
)
from krok_helper.subtitle_render.engine.timing.timeline import DisplayLine
from krok_helper.subtitle_render.engine.value_signature import value_signature
from krok_helper.subtitle_render.domain.models import Style
from krok_helper.subtitle_render.domain.timing import TimingTrack


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


@dataclass(frozen=True)
class LayoutPlanResolvers:
    """Concrete capabilities required to resolve one immutable track plan."""

    display_lines: DisplayLinesResolver
    page_offset_windows: PageOffsetWindowsResolver


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
    if layout_cache_enabled():
        cached = cached_track_layout_plan(cache_key)
        if cached is not None:
            return cached

    # 段首/段尾页标记必须先于逐行样式解析注册；包一层 layout_pass 保证
    # 无外层 pass 的调用方（IR 构建等）也能拿到同样的替换结果。
    with layout_pass():
        section_edge_context(track, style)
        display_style = display_style_for_signal_window(style)
        display_items: list[DisplayLine] = []
        if display_style.dual_line_layout:
            display_items = resolvers.display_lines(
                track,
                display_style,
                logical_w=logical_w,
                logical_h=logical_h,
            )
            # 显示窗口解析内部的分阶段刻度在此收口，保证该阶段必然到达满值。
            report_render_progress("display", 1, 1)
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
        # 页偏移解析内部的逐测量行刻度在此收口；单行 / 允许跨页重叠模式解析
        # 直接返回空字典（没有做实际工作），不空跳一格进度。
        if page_offset_windows:
            report_render_progress("page_offsets", 1, 1)
        line_total = max(len(track.lines), 1)
        report_render_progress("lines", 0, line_total)
        render_lines: list = []
        layout_styles: list = []
        resolved_intervals: list = []
        guide_anchor_bounds: list = []
        animation_styles: list = []
        for index, line in enumerate(track.lines):
            rendered = render_line_with_guide_symbols(line)
            render_lines.append(rendered)
            layout_styles.append(style_for_line(style, line))
            resolved_intervals.append(resolved_char_intervals_for_line(rendered, style))
            guide_anchor_bounds.append(
                resolved_guide_anchor_bounds_for_line(track, line, style)
            )
            animation_styles.append(
                style_for_line_display_window(
                    style,
                    line,
                    schedule[index][1] if index in schedule else None,
                    schedule[index][2] if index in schedule else None,
                )
            )
            report_render_progress("lines", index + 1, line_total)

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
    if layout_cache_enabled():
        # Retain the mutable owners because the key contains track identity.
        store_track_layout_plan(cache_key, track, style, plan)
    return plan


__all__ = ["LayoutPlanResolvers", "resolve_track_layout_plan"]
