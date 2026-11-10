"""主窗口几何与最大化/全屏状态要活过一次重启。

启动路径 ``showEvent → _apply_startup_window_geometry``：优先恢复持久化几何，
无记录/记录脱屏回落默认居中。退出最大化回到记住的窗口化矩形而不是硬编码
1480×960 居中 —— 落点仍由工作台显式 ``setGeometry``（延续 c2a8fe85「恢复后
落到屏幕右侧」的修复方式），只是矩形来源从常量换成记忆。

断言注意：恢复路径会把几何夹进当前屏幕可用区，offscreen 平台的屏幕可能只有
800×600，所以期望值要按 ``screen().availableGeometry()`` 现算，不能写死。

另外 Windows 真机上进入最大化/全屏时，resize/move 会先于 WindowStateChange
送达，几何采样因此延后到事件 settle 之后（见 gui_qt ``_track_normal_geometry``
注释）——相关回归用例在文末。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QSize, Qt  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from krok_helper.config import WINDOW_MIN_HEIGHT, WINDOW_MIN_WIDTH  # noqa: E402
from krok_helper.settings import (  # noqa: E402
    get_settings_path,
    load_app_settings,
    save_app_settings,
)

#: 模拟用户摆出的窗口化几何（原点在所有常见屏幕尺寸下都落在可用区内，
#: 夹取只可能收尺寸、不动原点，便于断言）。
USER_X, USER_Y, USER_W, USER_H = 180, 140, 1300, 880


@pytest.fixture
def workbench(monkeypatch, tmp_path: Path):
    """干净配置目录里起来的工作台（与 test_module_settings_persist 同模式）。"""
    monkeypatch.setenv("KARAOKE_STUDIO_SETTINGS_DIR", str(tmp_path))
    monkeypatch.delenv("KARAOKE_STUDIO_SETTINGS_APP_NAME", raising=False)

    from krok_helper import gui_qt

    app = QApplication.instance() or QApplication([])
    window = gui_qt.KrokHelperQtApp()
    yield window
    window.close()
    window.deleteLater()
    app.processEvents()


def _on_disk() -> dict:
    return json.loads(Path(get_settings_path()).read_text(encoding="utf-8"))


def _simulate_user_sizing(window) -> None:
    """窗口进入「已显示」状态并摆到用户选择的几何。

    隐藏的顶层窗口收不到 move/resize 事件，必须先 show()（真实流程里窗口
    也总是已显示的）；标志位先置 True，showEvent 就不会另排一次启动恢复。
    """
    window._startup_geometry_applied = True
    window.show()
    window.setGeometry(USER_X, USER_Y, USER_W, USER_H)
    QApplication.processEvents()


def _clamped_size(window) -> tuple[int, int]:
    """按恢复逻辑同规则计算期望尺寸：夹进可用区、不低于最低可用最小值。"""
    available = window.screen().availableGeometry()
    min_w = min(WINDOW_MIN_WIDTH, available.width())
    min_h = min(WINDOW_MIN_HEIGHT, available.height())
    return (
        max(min_w, min(USER_W, available.width())),
        max(min_h, min(USER_H, available.height())),
    )


# ── 纯设置层：序列化与校验 ──────────────────────────────────


def test_settings_roundtrip_window_geometry(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("KARAOKE_STUDIO_SETTINGS_DIR", str(tmp_path))
    monkeypatch.delenv("KARAOKE_STUDIO_SETTINGS_APP_NAME", raising=False)
    settings = load_app_settings()
    settings.window_geometry = [-1920, 300, 1480, 960]  # 负坐标：副屏在主屏左侧
    settings.window_maximized = True
    save_app_settings(settings)

    reloaded = load_app_settings()
    assert reloaded.window_geometry == [-1920, 300, 1480, 960]
    assert reloaded.window_maximized is True
    assert reloaded.window_fullscreen is False


@pytest.mark.parametrize(
    "bad",
    [
        [],
        [100, 100],
        [100, 100, 800],  # 少一段
        [100, 100, 800, 600, 1],  # 多一段
        [100, 100, 0, 600],
        [100, 100, 800, -5],
        [100, "x", 800, 600],
        [100, True, 800, 600],
        "100,100,800,600",
        {"x": 100, "y": 100, "w": 800, "h": 600},
    ],
)
def test_settings_reject_invalid_window_geometry(tmp_path, monkeypatch, bad) -> None:
    monkeypatch.setenv("KARAOKE_STUDIO_SETTINGS_DIR", str(tmp_path))
    monkeypatch.delenv("KARAOKE_STUDIO_SETTINGS_APP_NAME", raising=False)
    settings = load_app_settings()
    settings.window_geometry = bad
    save_app_settings(settings)
    assert load_app_settings().window_geometry == []


# ── GUI 层：保存与恢复 ─────────────────────────────────────


def test_geometry_and_maximized_survive_a_restart(monkeypatch, tmp_path: Path) -> None:
    """端到端：关掉一个窗口，再起一个，几何与最大化状态都还在。"""
    monkeypatch.setenv("KARAOKE_STUDIO_SETTINGS_DIR", str(tmp_path))
    monkeypatch.delenv("KARAOKE_STUDIO_SETTINGS_APP_NAME", raising=False)
    app = QApplication.instance() or QApplication([])

    from krok_helper import gui_qt

    first = gui_qt.KrokHelperQtApp()
    _simulate_user_sizing(first)
    first.setWindowState(Qt.WindowState.WindowMaximized)
    QApplication.processEvents()
    first._save_all_settings()
    disk = _on_disk()
    assert disk["window_geometry"] == [USER_X, USER_Y, USER_W, USER_H]
    assert disk["window_maximized"] is True
    assert disk["window_fullscreen"] is False
    first.close()
    first.deleteLater()
    app.processEvents()

    second = gui_qt.KrokHelperQtApp()
    # 真实路径里这一步由 showEvent 的 singleShot(0) 触发，这里直接调用等价物。
    second._apply_startup_window_geometry()
    assert bool(second.windowState() & Qt.WindowState.WindowMaximized)
    assert second._last_normal_geometry[0:2] == [USER_X, USER_Y]
    assert tuple(second._last_normal_geometry[2:]) == _clamped_size(second)
    second.close()
    second.deleteLater()
    app.processEvents()


def test_startup_restores_windowed_geometry(workbench) -> None:
    workbench.settings.window_geometry = [USER_X, USER_Y, USER_W, USER_H]
    workbench.settings.window_maximized = False
    workbench._apply_startup_window_geometry()
    geo = workbench.geometry()
    assert (geo.x(), geo.y()) == (USER_X, USER_Y)
    assert (geo.width(), geo.height()) == _clamped_size(workbench)


def test_startup_falls_back_when_geometry_off_screen(workbench) -> None:
    """换显示器布局后记录可能完全脱屏：回落默认居中而不是把窗口开到屏外。"""
    workbench.settings.window_geometry = [50000, 50000, USER_W, USER_H]
    workbench._apply_startup_window_geometry()
    geo = workbench.geometry()
    available = workbench.screen().availableGeometry()
    assert (geo.x(), geo.y()) != (50000, 50000)
    assert geo.width() <= available.width()
    assert geo.height() <= available.height()
    assert available.intersects(geo)


def test_unmaximize_returns_to_remembered_geometry(workbench) -> None:
    _simulate_user_sizing(workbench)
    workbench.setWindowState(Qt.WindowState.WindowMaximized)
    QApplication.processEvents()
    assert workbench._last_normal_geometry[:2] == [USER_X, USER_Y]

    workbench.setWindowState(Qt.WindowState.WindowNoState)
    QApplication.processEvents()  # 跑掉 changeEvent 排的 singleShot(0) 恢复
    geo = workbench.geometry()
    assert (geo.x(), geo.y()) == (USER_X, USER_Y)
    assert (geo.width(), geo.height()) == _clamped_size(workbench)


def test_save_before_first_show_does_not_clobber_loaded_geometry(workbench) -> None:
    """页面构建期的保存（窗口未显示）不许把盘上的几何冲掉。"""
    workbench.settings.window_geometry = [USER_X, USER_Y, USER_W, USER_H]
    workbench.settings.window_maximized = True
    # 模拟 _build_ui 期间的保存：startup_geometry_applied 还是 False。
    workbench._startup_geometry_applied = False
    workbench.setGeometry(0, 0, 1480, 960)
    QApplication.processEvents()
    workbench._save_all_settings()

    disk = _on_disk()
    assert disk["window_geometry"] == [USER_X, USER_Y, USER_W, USER_H]
    assert disk["window_maximized"] is True


def test_min_size_tracks_screen_when_restoring(workbench) -> None:
    """小屏幕上最低尺寸要让位于屏幕可用区，恢复的几何才能真的落地。"""
    workbench.settings.window_geometry = [USER_X, USER_Y, USER_W, USER_H]
    workbench._apply_startup_window_geometry()
    available = workbench.screen().availableGeometry()
    assert workbench.minimumSize() == QSize(
        min(WINDOW_MIN_WIDTH, available.width()), min(WINDOW_MIN_HEIGHT, available.height())
    )


# ── 回归：Windows 事件顺序造成的最大化几何污染 ───────────────
#
# 真机上 PostMessage SC_MAXIMIZE 后事件顺序是 resize → move → changeEvent：
# 带全屏矩形的 resize 到达时 windowState() 还是 NoState。同步采样会把满屏
# 矩形记进 _last_normal_geometry，之后每次退出最大化都恢复成满屏大小并
# 持久化（用户报的「最大化再还原，窗口越还原越大」）。修复是采样延后到
# 事件 settle 之后，这里在逻辑层重放同一顺序。


def test_maximize_race_does_not_pollute_remembered_geometry(workbench) -> None:
    """状态翻转先于 settle 的 resize，不得把满屏矩形记成窗口化几何。"""
    _simulate_user_sizing(workbench)
    assert workbench._last_normal_geometry == [USER_X, USER_Y, USER_W, USER_H]

    # 重放真机顺序：先来一个「满屏矩形」的 resize（此刻 windowState 仍是
    # NoState，采样被排队），同一批事件里状态才翻成 Maximized。
    available = workbench.screen().availableGeometry()
    workbench.setGeometry(available.x(), available.y(), available.width(), available.height())
    workbench.setWindowState(Qt.WindowState.WindowMaximized)
    QApplication.processEvents()  # 跑掉 _track_normal_geometry 排的 singleShot(0)

    # settle 之后守卫判到最大化：不落值，记忆仍是用户摆的窗口化矩形。
    assert workbench._last_normal_geometry == [USER_X, USER_Y, USER_W, USER_H]


def test_windowed_resize_still_updates_remembered_geometry(workbench) -> None:
    """正向对照：窗口化状态下的 resize 在 settle 后仍要更新记忆矩形，
    防止延迟采样把跟踪整个弄丢。"""
    _simulate_user_sizing(workbench)
    workbench.setGeometry(USER_X + 40, USER_Y + 30, USER_W - 60, USER_H - 50)
    QApplication.processEvents()
    assert workbench._last_normal_geometry == [USER_X + 40, USER_Y + 30, USER_W - 60, USER_H - 50]
