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


__all__ = ["ProgressReporter", "render_progress_scope", "report_render_progress"]
