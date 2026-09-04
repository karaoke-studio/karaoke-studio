"""Opt-in progress reporting for whole-track layout rebuilds.

预览 worker 在整轨重排 / 场景重建区间挂一个 reporter，引擎按真实刻度
（stage, done, total）逐行上报，GUI 侧据此显示百分比进度。未挂 reporter
时每次上报只是一次 thread-local 属性查找的 no-op，渲染热路径零开销。

刻度语义：

- ``done / total`` 恒在 ``[0, 1]`` 区间内单调推进到 1；
- 同一次重排内可能交错出现多个 stage（页偏移解析内部会复用显示窗口解析），
  由 worker 侧负责按 stage 加权合成总百分比并对结果做单调保护；
- stage 键是引擎内部标识（display / page_offsets / lines），用户可见的
  中文文案在 worker 侧映射。
"""

from __future__ import annotations

from contextlib import contextmanager
from threading import local as thread_local
from typing import Callable


_REPORTER = thread_local()

ProgressReporter = Callable[[str, int, int], None]


@contextmanager
def render_progress_scope(reporter: ProgressReporter | None):
    """挂载当前线程的进度 reporter（可重入，退出时恢复上一层）。"""

    previous = getattr(_REPORTER, "reporter", None)
    _REPORTER.reporter = reporter
    try:
        yield
    finally:
        _REPORTER.reporter = previous


def report_render_progress(stage: str, done: int, total: int) -> None:
    reporter = getattr(_REPORTER, "reporter", None)
    if reporter is not None:
        reporter(stage, int(done), int(total))


def set_display_phase_head(head: float, total: float) -> None:
    """登记 display 阶段当前所处的步骤槽位（head = 已完成步骤数）。

    ``resolve_display_lines`` 的 driver 在每个多趟步骤前登记；步骤内部的
    实测循环（``measure_collision_bands``）据此把逐行进度折算成
    ``head + 行比例`` 的总刻度，使 display 阶段逐行连续推进而非每趟一跳。
    """

    _REPORTER.display_phase_head = (float(head), float(total))


def clear_display_phase_head() -> None:
    _REPORTER.display_phase_head = None


def report_display_measure_progress(done: int, total: int) -> None:
    """display 阶段内一次实测调用的逐行进度，折算进当前步骤槽位。

    同一槽位内可能有多次实测调用（趟内复测）：每次都从槽位内 0 起算，
    worker 侧的单调保护会忽略后续调用的回退刻度——后续调用期间进度
    停在槽位顶，偏差上界为一个步骤槽（总刻度的 1/7）。
    """

    reporter = getattr(_REPORTER, "reporter", None)
    if reporter is None:
        return
    head = getattr(_REPORTER, "display_phase_head", None)
    if head is None or total <= 0:
        return
    head_value, head_total = head
    fraction = min(max(float(done) / float(total), 0.0), 1.0)
    reporter("display", head_value + fraction, head_total)


__all__ = [
    "ProgressReporter",
    "clear_display_phase_head",
    "render_progress_scope",
    "report_display_measure_progress",
    "report_render_progress",
    "set_display_phase_head",
]
