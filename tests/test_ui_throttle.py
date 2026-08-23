"""工作台前后台 UI 节流（krok_helper.background_throttle）的单元测试。

判定口径：只看 widget/窗口可见性，不看焦点——窗口可见但失焦仍算前台
（分屏看预览场景）；窗口最小化/隐藏或页面切走才算后台。
"""

from __future__ import annotations

import pytest
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication, QWidget

from krok_helper.background_throttle import (
    UiActivityGuard,
    background_throttle,
    ui_active,
)


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def throttle(app):
    return background_throttle()


def _process(app):
    app.processEvents()


# ── BackgroundThrottle：单例 / is_visible / 广播 ───────────────────────


def test_singleton(app):
    assert background_throttle() is background_throttle()


def test_is_visible_scans_top_level_windows(app, throttle, monkeypatch):
    first, second = QWidget(), QWidget()
    monkeypatch.setattr(
        throttle._app, "topLevelWidgets", lambda: [first, second]
    )
    assert throttle.is_visible is False  # 全部隐藏

    first.show()
    _process(app)
    assert throttle.is_visible is True  # 任意一个可见即前台

    first.hide()
    _process(app)
    monkeypatch.setattr(second, "isMinimized", lambda: True)
    second.show()
    _process(app)
    assert throttle.is_visible is False  # 可见但最小化 = 后台

    first.deleteLater()
    second.deleteLater()


def test_visibility_signal_emitted_on_show_hide(app, throttle):
    received = []
    throttle.visibility_maybe_changed.connect(lambda: received.append(1))
    win = QWidget()
    try:
        win.show()
        _process(app)
        shown = len(received)
        win.hide()
        _process(app)
        hidden = len(received)
    finally:
        win.deleteLater()
    assert shown >= 1
    assert hidden > shown


# ── ui_active：widget 自身可见 + 顶层窗口未最小化 ─────────────────────


def test_ui_active_follows_widget_and_window_state(app):
    win = QWidget()
    child = QWidget(win)
    try:
        win.show()
        _process(app)
        assert ui_active(child) is True

        child.hide()  # 模拟页面被切走
        assert ui_active(child) is False
        child.show()

        win.hide()  # 窗口严格隐藏
        assert ui_active(child) is False
        win.show()
        _process(app)

        win.isMinimized = lambda: True  # 模拟窗口最小化
        assert ui_active(child) is False
    finally:
        win.deleteLater()


# ── UiActivityGuard：暂停 / 恢复 / 幂等 / 业务启停 ─────────────────────


def test_guard_pauses_and_resumes_timer(app, throttle):
    win = QWidget()
    win.show()
    _process(app)
    timer = QTimer(win)
    timer.setInterval(100)
    timer.start()
    resumes = []
    try:
        guard = UiActivityGuard(win)
        guard.manage(timer, on_resume=lambda: resumes.append(1))
        assert timer.isActive()  # 可见时保持全速

        win.hide()
        _process(app)
        assert not timer.isActive()  # 隐藏 → 暂停

        win.show()
        _process(app)
        assert timer.isActive()  # 恢复 → 重启
        assert len(resumes) == 1  # 恢复回调恰好一次（立即补刷新）
    finally:
        win.deleteLater()


def test_guard_idempotent_on_repeated_broadcasts(app, throttle):
    win = QWidget()
    win.show()
    _process(app)
    timer = QTimer(win)
    timer.start()
    resumes = []
    try:
        guard = UiActivityGuard(win)
        guard.manage(timer, on_resume=lambda: resumes.append(1))
        win.hide()
        _process(app)
        throttle.visibility_maybe_changed.emit()  # 重复广播
        assert not timer.isActive()

        win.show()
        _process(app)
        count = len(resumes)
        assert count == 1
        throttle.visibility_maybe_changed.emit()  # 已在跑，不重复动作
        assert timer.isActive()
        assert len(resumes) == count
    finally:
        win.deleteLater()


def test_guard_business_stop_prevents_resume(app, throttle):
    """任务结束（set_desired(False)）后，可见性恢复不得重启定时器。"""
    win = QWidget()
    win.show()
    _process(app)
    timer = QTimer(win)
    timer.start()
    resumes = []
    try:
        guard = UiActivityGuard(win)
        entry = guard.manage(timer, on_resume=lambda: resumes.append(1))
        entry.set_desired(False)
        assert not timer.isActive()

        win.hide()
        _process(app)
        win.show()
        _process(app)
        assert not timer.isActive()
        assert resumes == []
    finally:
        win.deleteLater()


def test_manage_stops_running_timer_on_hidden_widget_immediately(app):
    """接管已启动的定时器时必须当场按可见性校正。

    FPS 定时器是「先 start、后 manage」路径：widget 仍隐藏时 manage()
    不能等下一次 Show/Hide 事件才停。
    """
    win = QWidget()  # 从未 show
    timer = QTimer(win)
    timer.setInterval(100)
    timer.start()
    try:
        guard = UiActivityGuard(win)
        guard.manage(timer)
        assert not timer.isActive()  # 接管即停，无事件也生效

        win.show()
        _process(app)
        assert timer.isActive()  # 广播后恢复
    finally:
        win.deleteLater()


def test_on_visibility_callback_invoked_on_broadcast(app, throttle):
    calls = []
    win = QWidget()
    try:
        guard = UiActivityGuard(win)
        guard.on_visibility(lambda: calls.append(win.isVisible()))
        win.show()
        _process(app)
        win.hide()
        _process(app)
    finally:
        win.deleteLater()
    # show/hide 可能各自触发多次广播（Show + WindowStateChange 等），
    # 只要求可见与不可见两种状态都被通知到。
    assert True in calls
    assert False in calls
    assert calls[-1] is False


def test_guard_defers_start_while_hidden(app, throttle):
    """隐藏期间任务开始（set_desired(True)）：等恢复可见才真正启动。"""
    win = QWidget()
    timer = QTimer(win)
    timer.setInterval(100)
    resumes = []
    try:
        guard = UiActivityGuard(win)
        entry = guard.manage(timer, on_resume=lambda: resumes.append(1))
        entry.set_desired(True)
        assert not timer.isActive()  # 窗口从未显示 → 不启动

        win.show()
        _process(app)
        assert timer.isActive()
        assert len(resumes) == 1
    finally:
        win.deleteLater()


# ── CurrentTaskPanel：空闲动画 bug 回归 + 面板级节流 ───────────────────


def test_current_task_panel_idle_busy_bar_not_running(app):
    from krok_helper.audio_processing.separation.widgets import CurrentTaskPanel

    panel = CurrentTaskPanel()
    try:
        # qfluentwidgets IndeterminateProgressBar 构造即启动无限动画；
        # 空闲面板不得空转（本次节流修掉的常驻 CPU 消耗点）。
        assert not panel._busy_bar.isStarted()
        assert not panel._elapsed_timer.isActive()

        panel.start("测试任务")
        _process(app)
        assert panel._busy_bar.isStarted()
        assert panel._elapsed_timer.isActive()

        panel.hide()  # 模拟页面切走 / 窗口隐藏
        _process(app)
        assert not panel._busy_bar.isStarted()
        assert not panel._elapsed_timer.isActive()

        panel.show()
        _process(app)
        assert panel._busy_bar.isStarted()
        assert panel._elapsed_timer.isActive()

        panel.stop()  # 任务结束：即使可见也不再跑
        assert not panel._busy_bar.isStarted()
        assert not panel._elapsed_timer.isActive()
    finally:
        panel.deleteLater()


def test_current_task_panel_determinate_stage_stops_busy_bar(app):
    """下载/处理阶段显示确定性进度条，忙碌条动画应停而不是后台空转。"""
    from krok_helper.audio_processing.separation.backend import TaskProgress
    from krok_helper.audio_processing.separation.widgets import CurrentTaskPanel

    panel = CurrentTaskPanel()
    try:
        panel.show()
        _process(app)
        panel.start("测试任务")
        assert panel._busy_bar.isStarted()

        progress = TaskProgress(
            stage_index=1,
            show_download=True,
            download_done=10,
            download_total=100,
        )
        panel.update_progress(progress)
        assert not panel._busy_bar.isStarted()  # 确定性阶段动画停止

        progress = TaskProgress(stage_index=2)
        panel.update_progress(progress)
        assert panel._busy_bar.isStarted()  # 回到不确定阶段恢复动画
    finally:
        panel.deleteLater()


# ── 导出逐帧 UI 更新：隐藏攒最新值，恢复可见重放 ──────────────────────


@pytest.fixture
def render_window(qapp, monkeypatch):
    from krok_helper.subtitle_render.frontend import main_window as mw

    monkeypatch.setattr(mw, "fluent_error", lambda *a, **k: None)
    monkeypatch.setattr(mw, "fluent_warning", lambda *a, **k: None)
    monkeypatch.setattr(
        mw.SubtitleRenderWindow,
        "_resolve_ffprobe_path",
        lambda self: "ffprobe",
    )
    window = mw.SubtitleRenderWindow(embedded=False)
    yield window
    # 必须跑一次事件循环让 deleteLater 真正销毁窗口树，
    # 否则合跑时 USER 句柄累积会拖垮后续用例的定时器注册。
    window.hide()
    window.close()
    window.deleteLater()
    qapp.processEvents()


def test_export_progress_defers_updates_while_hidden(qapp, render_window):
    """隐藏攒最新值 → 恢复重放 → 再隐藏 → 收尾清理，单窗口走完整场景。"""
    win = render_window
    # 隐藏期间 worker 全速：UI 只攒最新一帧数据
    win._on_render_progress(10, 100)
    assert win._export_pending_progress == (10, 100)
    assert win._export_progress.value() == 0
    win._on_render_log("阶段日志")
    assert win._export_pending_log == "阶段日志"

    # 恢复可见：广播触发 flush，重放到 UI
    win.show()
    qapp.processEvents()
    assert win._export_pending_progress is None
    assert win._export_pending_log is None
    assert win._export_progress.value() == 10
    assert win._export_progress.maximum() == 100
    assert win._export_status_label.text() == "阶段日志"

    # 可见时直接更新，不攒
    win._on_render_progress(30, 100)
    assert win._export_pending_progress is None
    assert win._export_progress.value() == 30
    assert "30/100" in win._export_status_label.text()

    # 再次隐藏 → 重新进入攒模式，UI 保持旧值
    win.hide()
    qapp.processEvents()
    win._on_render_progress(40, 100)
    assert win._export_pending_progress == (40, 100)
    assert win._export_progress.value() == 30


def test_export_finish_clears_pending(qapp, render_window):
    win = render_window
    win._on_render_progress(50, 100)
    assert win._export_pending_progress == (50, 100)
    win._stop_export_preview_polling()  # 三个 finish 路径共用的收尾
    assert win._export_pending_progress is None
    assert win._export_pending_log is None
    # 收尾后恢复可见不得重放旧进度
    win.show()
    qapp.processEvents()
    assert win._export_progress.value() == 0
