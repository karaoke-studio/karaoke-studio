"""Off-thread project task execution (no progress windows)."""

from __future__ import annotations

import threading
import time

import pytest
from PyQt6.QtCore import QEventLoop, QTimer
from PyQt6.QtWidgets import QApplication

from krok_helper.subtitle_render.frontend.project import tasks
from krok_helper.subtitle_render.frontend.project.tasks import (
    ProjectIoGuard,
    ProjectTaskCanceled,
    ProjectTaskRunner,
    ProjectTaskScope,
)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _pump(qapp: QApplication, predicate, timeout_s: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    return bool(predicate())


def test_run_blocking_runs_work_off_the_gui_thread(qapp):
    runner = ProjectTaskRunner()
    gui_thread = threading.get_ident()
    seen: dict[str, int] = {}

    def work():
        seen["thread"] = threading.get_ident()
        return 42

    try:
        assert runner.run_blocking(work) == 42
        assert seen["thread"] != gui_thread
    finally:
        runner.shutdown()


def test_run_blocking_propagates_worker_errors(qapp):
    runner = ProjectTaskRunner()

    def work():
        raise OSError("读取失败")

    try:
        with pytest.raises(OSError, match="读取失败"):
            runner.run_blocking(work)
    finally:
        runner.shutdown()


def test_run_blocking_cancel_raises_and_discards_late_result(qapp):
    runner = ProjectTaskRunner()
    scope = ProjectTaskScope()

    def cancel_soon() -> None:
        runner.guard.request_cancel()

    def slow():
        time.sleep(0.2)
        return "late"

    QTimer.singleShot(10, cancel_soon)
    try:
        with pytest.raises(ProjectTaskCanceled):
            runner.run_blocking(slow, scope=scope)
        assert scope.canceled
        # 结果被丢弃后回调不再执行；作用域清理后可以继续正常使用。
        runner.run_blocking(lambda: 1)
    finally:
        runner.shutdown()


def test_cancelled_scope_short_circuits_before_starting_work(qapp):
    runner = ProjectTaskRunner()
    scope = ProjectTaskScope()
    scope.cancel()
    called: list[bool] = []
    try:
        with pytest.raises(ProjectTaskCanceled):
            runner.run_blocking(lambda: called.append(True), scope=scope)
        assert called == []
    finally:
        runner.shutdown()


def test_submit_delivers_callbacks_on_the_gui_thread(qapp):
    runner = ProjectTaskRunner()
    gui_thread = threading.get_ident()
    done: list[tuple[object, int]] = []
    errors: list[BaseException] = []
    try:
        runner.submit(
            lambda: 7,
            on_done=lambda value: done.append((value, threading.get_ident())),
        )
        assert _pump(qapp, lambda: done)
        assert done == [(7, gui_thread)]

        runner.submit(lambda: 1 / 0, on_error=lambda exc: errors.append(exc))
        assert _pump(qapp, lambda: errors)
        assert isinstance(errors[0], ZeroDivisionError)
    finally:
        runner.shutdown()


def test_blocking_work_never_creates_a_progress_window(qapp):
    """进度只走页面既有的 StateToolTip/InfoBar：后台 I/O 不得新建任何窗口。"""
    runner = ProjectTaskRunner()
    before = set(QApplication.topLevelWidgets())
    try:
        assert runner.run_blocking(lambda: 1) == 1
        assert set(QApplication.topLevelWidgets()) - before == set()
        assert not hasattr(runner.guard, "_dialog")
    finally:
        runner.shutdown()


def test_slow_work_never_creates_a_progress_window(qapp):
    runner = ProjectTaskRunner()

    def slow():
        time.sleep(0.3)
        return "ok"

    before = set(QApplication.topLevelWidgets())
    try:
        assert runner.run_blocking(slow, label="读取工程文件") == "ok"
        assert set(QApplication.topLevelWidgets()) - before == set()
        assert runner.run_blocking(lambda: 2) == 2
        assert set(QApplication.topLevelWidgets()) - before == set()
        assert not hasattr(runner.guard, "_dialog")
    finally:
        runner.shutdown()


def test_guard_cancels_every_nested_scope(qapp):
    guard = ProjectIoGuard()
    scope = ProjectTaskScope()
    loop_holder: list[object] = []

    class _Loop:
        def quit(self) -> None:
            loop_holder.append(self)

    guard.begin("一", scope, _Loop())
    guard.begin("二", scope, None)
    guard.request_cancel()
    assert scope.canceled
    assert loop_holder
    guard.end(None)
    guard.end()
    guard.close_now()


def test_guard_cancel_without_scope_is_a_no_op(qapp):
    guard = ProjectIoGuard()
    guard.begin("只读")
    guard.request_cancel()
    guard.end()
    guard.close_now()


def test_shutdown_releases_a_waiting_blocking_call(qapp):
    """关窗/宿主强退时，等在嵌套事件循环里的 run_blocking 必须能立刻退出。"""
    runner = ProjectTaskRunner()
    entered = threading.Event()

    def slow():
        entered.set()
        time.sleep(1.0)
        return "late"

    QTimer.singleShot(50, runner.shutdown)
    started = time.monotonic()
    try:
        with pytest.raises(ProjectTaskCanceled):
            runner.run_blocking(slow)
    finally:
        elapsed = time.monotonic() - started
        assert runner.closed
    assert entered.is_set()
    assert elapsed < 0.9, f"shutdown 未及时唤醒等待者（{elapsed:.2f}s）"


def test_run_blocking_timeout_raises_instead_of_hanging(qapp):
    runner = ProjectTaskRunner()
    release = threading.Event()
    try:
        with pytest.raises(tasks.ProjectTaskTimeout):
            runner.run_blocking(
                lambda: release.wait(5.0),
                timeout_s=0.15,
            )
        # 超时只丢弃等待，不毒化执行器：后续任务照常完成。
        assert runner.run_blocking(lambda: 3) == 3
    finally:
        release.set()
        runner.shutdown()


def test_submit_timeout_notifies_error_callback(qapp):
    runner = ProjectTaskRunner()
    release = threading.Event()
    errors: list[BaseException] = []
    try:
        runner.submit(
            lambda: release.wait(5.0),
            on_error=lambda exc: errors.append(exc),
            timeout_s=0.1,
        )
        assert _pump(qapp, lambda: errors)
        assert isinstance(errors[0], tasks.ProjectTaskTimeout)
    finally:
        release.set()
        runner.shutdown()


def test_pool_never_strands_a_task_at_the_idle_boundary(qapp, monkeypatch):
    """空闲 worker 退出与 enqueue 交错时任务不得丢失（提交永远有人消费）。"""
    monkeypatch.setattr(tasks._DaemonTaskPool, "IDLE_EXIT_S", 0.02)
    runner = ProjectTaskRunner()
    try:
        for _round in range(6):
            done: list[int] = []
            assert runner.submit(lambda: done.append(1)) is not None
            assert _pump(qapp, lambda: done), "任务在空闲边界被饿死"
            # 等 idle worker 真正跑到超时退出窗口附近。
            time.sleep(0.03)
    finally:
        runner.shutdown()


def test_nested_blocking_waits_create_no_windows(qapp):
    """嵌套 run_blocking 也不能创建进度窗口（页面只保留既有 StateToolTip）。"""
    runner = ProjectTaskRunner()

    def slow():
        time.sleep(0.3)

    before = set(QApplication.topLevelWidgets())
    try:
        runner.run_blocking(slow, label="第一段")
        for index in range(3):
            runner.run_blocking(lambda: time.sleep(0.25), label=f"第{index}段")
        assert set(QApplication.topLevelWidgets()) - before == set()
        assert not hasattr(runner.guard, "_dialog")
    finally:
        runner.shutdown()


def test_stage_loop_is_released_by_close_now(qapp):
    """_run_project_steps 用的 stage loop 也要能被窗口关闭路径唤醒。"""
    guard = ProjectIoGuard()
    loop = QEventLoop()
    guard.begin("装配")
    guard.register_stage_loop(loop)
    QTimer.singleShot(30, guard.close_now)
    started = time.monotonic()
    loop.exec()
    guard.unregister_stage_loop(loop)
    guard.end()
    assert time.monotonic() - started < 1.0
