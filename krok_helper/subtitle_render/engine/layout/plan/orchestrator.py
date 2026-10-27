"""Orchestrate a shared layout plan through explicit resolver contracts."""

from __future__ import annotations

from dataclasses import dataclass, replace
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
)
from krok_helper.subtitle_render.engine.timing.timeline import DisplayLine
from krok_helper.subtitle_render.engine.value_signature import (
    lyric_layout_style_signature,
    track_signature_for_windows,
    value_signature,
)
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


def _rebind_plan_line_styles(
    plan: TrackLayoutPlan,
    track: TimingTrack,
    style: Style,
) -> TrackLayoutPlan:
    """把缓存 plan 里逐行携带的样式引用重绑到当前 ``style``。

    plan 的 key 是颜色剔除版布局签名（颜色编辑时几何部分照常复用），
    但 :class:`LineLayoutPlan` 的 ``layout_style`` / ``animation_style``
    是解析后的完整样式引用，携带颜色与角色叠加——不重绑会让换色命中
    旧 plan、按旧样式绘制。逐行重跑 :func:`style_for_line` 系列只是
    dataclass 级替换，远便宜于整轨重排；样式未变时返回原 plan，保持
    paint-scope 复用测试所依赖的对象同一性。
    """

    if not plan.lines:
        return plan
    with layout_pass():
        section_edge_context(track, style)
        new_lines = []
        changed = False
        for line_plan in plan.lines:
            index = line_plan.track_index
            if index < 0 or index >= len(track.lines):
                new_lines.append(line_plan)
                continue
            line = track.lines[index]
            layout_style = style_for_line(style, line)
            animation_style = style_for_line_display_window(
                style,
                line,
                line_plan.display_start_ms,
                line_plan.display_end_ms,
            )
            if line_plan.displace_exit_ms is not None:
                # 被顶掉行的退场动画时长覆写必须在窗口钳制之后重应用，
                # 否则缓存命中重绑会把 exit_fade 压回钳制值。
                animation_style = replace(
                    animation_style,
                    exit_fade_ms=line_plan.displace_exit_ms,
                )
            # style_for_line 每次调用都 replace 出新对象，按值比较判断
            # 是否真的变化：未变时保持 plan 对象同一性（缓存复用方依赖）。
            if (
                layout_style != line_plan.layout_style
                or animation_style != line_plan.animation_style
            ):
                changed = True
                line_plan = replace(
                    line_plan,
                    layout_style=layout_style,
                    animation_style=animation_style,
                )
            new_lines.append(line_plan)
    if not changed:
        return plan
    return replace(plan, lines=tuple(new_lines))


def resolve_track_layout_plan(
    track: TimingTrack,
    style: Style,
    resolvers: LayoutPlanResolvers,
    *,
    logical_w: int | None = None,
    logical_h: int | None = None,
    use_cache: bool = True,
) -> TrackLayoutPlan:
    """Resolve frame-independent line semantics for CPU and GPU consumers.

    ``use_cache=False`` 表示强制全量重排（时间/布局等变化，绕过缓存直建），
    结果仍写回缓存供后续局部复用；``use_cache=True`` 时按 (轨道值签名,
    歌词布局样式签名) 命中复用——签名不匹配自动回退重建，因此局部复用
    永远是签名校验过的，调用方的 scope 只是性能提示而非正确性依据。
    """
    cache_key = (
        logical_w,
        logical_h,
        id(track),
        # 窗口语义归一化签名（P5）：逐行动画「类型互换（时长不变）」不再
        # 打布局计划缓存——计划几何不变，逐行样式由 _rebind_plan_line_styles
        # 从当前轨道重解析。none↔非none 与时长变化仍全量重排。
        track_signature_for_windows(track),
        lyric_layout_style_signature(style),
    )
    if use_cache and layout_cache_enabled():
        cached = cached_track_layout_plan(cache_key)
        if cached is not None:
            rebound = _rebind_plan_line_styles(cached, track, style)
            if rebound is not cached:
                # 重绑结果必须写回缓存：否则任何 paint-only 编辑之后，每帧都会
                # 重新 rebind 出一批新的 LineLayoutPlan 对象，挂在行计划上的
                # 缓存（如退场尾奏淘汰界 painter._particle_tail_cache）每帧被
                # 击穿，退化为「每行每帧重跑一次整行粒子规划」。
                # 写回值仍满足缓存键：键只含布局签名，而逐行样式本来就是缓存
                # 命中后按当前样式重解析的结果。
                store_track_layout_plan(cache_key, track, style, rebound)
            return rebound

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
        animation_styles: list = []
        for index, line in enumerate(track.lines):
            rendered = render_line_with_guide_symbols(line)
            render_lines.append(rendered)
            layout_styles.append(style_for_line(style, line))
            resolved_intervals.append(resolved_char_intervals_for_line(rendered, style))
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
        )
    if layout_cache_enabled():
        # Retain the mutable owners because the key contains track identity.
        store_track_layout_plan(cache_key, track, style, plan)
    return plan


__all__ = ["LayoutPlanResolvers", "resolve_track_layout_plan"]
