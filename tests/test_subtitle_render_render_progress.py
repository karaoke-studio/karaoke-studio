"""整轨重排的进度上报原语（render_progress_scope / report_render_progress）。

预览 worker 在重排区间挂 reporter，引擎按真实刻度（stage, done, total）逐行上报，
GUI 侧据此显示百分比徽标；未挂 reporter 时上报必须是零开销 no-op、结果不受影响。
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from krok_helper.subtitle_render.domain.models import (  # noqa: E402
    Style,
    TimingChar,
    TimingLine,
    TimingTrack,
)
from krok_helper.subtitle_render.engine.layout.display.resolver import (  # noqa: E402
    DisplayResolutionPorts,
    resolve_display_lines,
)
from krok_helper.subtitle_render.engine.layout.plan.orchestrator import (  # noqa: E402
    LayoutPlanResolvers,
    resolve_track_layout_plan,
)
from krok_helper.subtitle_render.engine.layout.plan.cache import (  # noqa: E402
    clear_track_layout_plan_cache,
)
from krok_helper.subtitle_render.engine.render_progress import (  # noqa: E402
    clear_display_phase_head,
    render_progress_scope,
    report_display_measure_progress,
    set_display_phase_head,
)


def _track(line_count: int = 3) -> TimingTrack:
    lines = []
    for index in range(line_count):
        start = index * 2_000
        lines.append(
            TimingLine(
                chars=[TimingChar(text, start + i * 300) for i, text in enumerate("テスト")],
                end_ms=start + 1_500,
            )
        )
    return TimingTrack(lines=lines)


def _resolvers():
    return LayoutPlanResolvers(
        display_lines=lambda track, style, *, logical_w=None, logical_h=None: [],
        # 返回非空 dict：页偏移收口边界只在实际做了解析工作时上报。
        page_offset_windows=lambda logical_w, logical_h, track, style: {0: ()},
    )


def test_scope_reports_line_stage_progress():
    clear_track_layout_plan_cache()
    events: list[tuple[str, int, int]] = []

    with render_progress_scope(lambda stage, done, total: events.append((stage, done, total))):
        resolve_track_layout_plan(_track(3), Style(), _resolvers(), logical_w=1920, logical_h=1080)

    lines_events = [event for event in events if event[0] == "lines"]
    assert lines_events[0] == ("lines", 0, 3)
    assert lines_events[-1] == ("lines", 3, 3)
    dones = [event[1] for event in lines_events]
    assert dones == sorted(dones)
    # 两个解析器调用的收口边界必然到达满值。
    assert ("display", 1, 1) in events
    assert ("page_offsets", 1, 1) in events


def test_without_scope_render_is_silent_and_unchanged():
    clear_track_layout_plan_cache()
    # 未挂 reporter：不抛异常、计划照常产出（cache key 含 track id，两次互不干扰）。
    plan = resolve_track_layout_plan(_track(2), Style(), _resolvers())
    assert plan is not None
    assert len(plan.lines) == 2


def test_scope_restores_previous_reporter_on_exit():
    outer: list[tuple[str, int, int]] = []
    inner: list[tuple[str, int, int]] = []

    with render_progress_scope(lambda *event: outer.append(event)):
        with render_progress_scope(lambda *event: inner.append(event)):
            resolve_track_layout_plan(_track(1), Style(), _resolvers())
        # 内层退出后恢复外层：继续上报进 outer。
        resolve_track_layout_plan(_track(2), Style(), _resolvers())

    assert inner
    assert outer


def test_display_measure_progress_folds_into_phase_slot():
    events: list[tuple[str, float, float]] = []

    with render_progress_scope(lambda stage, done, total: events.append((stage, done, total))):
        # 未登记槽位：不发射。
        report_display_measure_progress(1, 4)
        assert not events

        set_display_phase_head(2, 7)
        report_display_measure_progress(1, 4)
        report_display_measure_progress(4, 4)
        assert events == [("display", 2.25, 7), ("display", 3.0, 7)]

        clear_display_phase_head()
        report_display_measure_progress(2, 4)
        # 槽位已清理：不再产生新事件。
        assert len(events) == 2


def test_resolve_display_lines_reports_measure_ticks_and_clears_slot():
    events: list[tuple[str, float, float]] = []

    def measuring_compute(**kwargs):
        for index in range(4):
            report_display_measure_progress(index, 4)
        return []

    def measuring_resolve_timing(items, enforce_gap):
        for index in range(4):
            report_display_measure_progress(index, 4)
        return items

    ports = DisplayResolutionPorts(
        compute=measuring_compute,
        resolve_timing=measuring_resolve_timing,
        collision_pairs=lambda items: (),
        secondary_collision_pairs=lambda items: (),
        fill_section_time=lambda items: items,
        apply_animation_guard=lambda items, avoid: items,
    )

    with render_progress_scope(lambda stage, done, total: events.append((stage, done, total))):
        resolved = resolve_display_lines(
            avoid_collisions=False,
            auto_fill_section_time=False,
            ports=ports,
        )
    assert resolved == []

    display_events = [event for event in events if event[0] == "display"]
    # avoid_collisions=False：只有 ideal compute（槽位 0）与收尾 resolve_timing
    # （槽位 5）；逐行折算 + 步骤边界全部落在 7 格总刻度内且单调。
    assert ("display", 0.0, 7) in display_events
    assert ("display", 0.75, 7) in display_events
    assert ("display", 1.0, 7) in display_events
    assert ("display", 5.0, 7) in display_events
    assert ("display", 5.75, 7) in display_events
    assert ("display", 7.0, 7) in display_events
    fractions = [done / total for _stage, done, total in display_events]
    max_fraction = 0.0
    for fraction in fractions:
        # 允许步骤边界持平，但不允许任何回退。
        assert fraction >= max_fraction
        max_fraction = max(max_fraction, fraction)

    # driver 退出后槽位已清理：脱离解析流程的实测调用不再发射。
    after: list[tuple[str, float, float]] = []
    with render_progress_scope(lambda *event: after.append(event)):
        report_display_measure_progress(1, 4)
    assert after == []
