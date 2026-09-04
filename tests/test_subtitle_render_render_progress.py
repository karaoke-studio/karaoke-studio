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
from krok_helper.subtitle_render.engine.layout.plan.orchestrator import (  # noqa: E402
    LayoutPlanResolvers,
    resolve_track_layout_plan,
)
from krok_helper.subtitle_render.engine.layout.plan.cache import (  # noqa: E402
    clear_track_layout_plan_cache,
)
from krok_helper.subtitle_render.engine.render_progress import (  # noqa: E402
    render_progress_scope,
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
        page_offset_windows=lambda logical_w, logical_h, track, style: {},
    )


def test_scope_reports_line_stage_progress():
    clear_track_layout_plan_cache()
    events: list[tuple[str, int, int]] = []

    with render_progress_scope(lambda stage, done, total: events.append((stage, done, total))):
        resolve_track_layout_plan(_track(3), Style(), _resolvers())

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
