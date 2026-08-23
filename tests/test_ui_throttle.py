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
