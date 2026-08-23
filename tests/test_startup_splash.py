from __future__ import annotations

import importlib
import importlib.util
import os
from pathlib import Path
import subprocess
import sys

import pytest

from PyQt6.QtCore import QPoint, QSize, Qt
from PyQt6.QtGui import QCursor
from PyQt6.QtWidgets import QApplication, QLabel, QWidget

from krok_helper.config import APP_VERSION, APP_WINDOW_TITLE


def test_startup_splash_module_exposes_window_class() -> None:
    spec = importlib.util.find_spec("krok_helper.startup_splash")

    assert spec is not None
    module = importlib.import_module("krok_helper.startup_splash")
    assert getattr(module, "StartupSplashWindow", None) is not None


def test_startup_splash_is_a_qt_widget() -> None:
    from krok_helper.startup_splash import StartupSplashWindow

    assert issubclass(StartupSplashWindow, QWidget)


def test_startup_splash_uses_compact_frameless_window() -> None:
    from krok_helper.startup_splash import StartupSplashWindow

    window = StartupSplashWindow()

    assert window.size() == QSize(400, 400)
    assert window.windowFlags() & Qt.WindowType.FramelessWindowHint
    assert not window.windowFlags() & Qt.WindowType.WindowStaysOnTopHint


def test_startup_splash_displays_workbench_branding_and_start_image() -> None:
    from krok_helper.startup_splash import STARTUP_IMAGE_PATH, StartupSplashWindow

    window = StartupSplashWindow()
    title = window.findChild(QLabel, "startupTitle")
    status = window.findChild(QLabel, "startupStatus")

    assert title is not None
    assert title.text() == f"{APP_WINDOW_TITLE} · v{APP_VERSION}"
    assert status is not None
    assert status.text() == "正在加载..."
    assert STARTUP_IMAGE_PATH == Path(__file__).parents[1] / "krok_helper" / "assets" / "logo" / "start.jpg"
    assert window.background_path == STARTUP_IMAGE_PATH
    assert not window.background_pixmap.isNull()
    assert window.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)


def test_startup_title_fits_inside_the_splash() -> None:
    from krok_helper.startup_splash import StartupSplashWindow

    window = StartupSplashWindow()
    window.show()
    QApplication.processEvents()
    title = window.findChild(QLabel, "startupTitle")

    assert title is not None
    assert title.fontMetrics().horizontalAdvance(title.text()) <= title.width()


def test_startup_screen_selector_is_available() -> None:
    module = importlib.import_module("krok_helper.startup_splash")

    assert getattr(module, "select_startup_screen", None) is not None


def test_startup_screen_selector_prefers_screen_under_cursor(monkeypatch) -> None:
    from krok_helper.startup_splash import select_startup_screen

    cursor_position = QPoint(1800, 240)
    cursor_screen = object()
    primary_screen = object()
    monkeypatch.setattr(QCursor, "pos", staticmethod(lambda: cursor_position))
    monkeypatch.setattr(
        QApplication,
        "screenAt",
        staticmethod(lambda position: cursor_screen if position == cursor_position else None),
    )
    monkeypatch.setattr(QApplication, "primaryScreen", staticmethod(lambda: primary_screen))

    assert select_startup_screen() is cursor_screen


def test_cli_import_does_not_eagerly_load_main_window_module() -> None:
    env = os.environ.copy()
    env["QT_QPA_PLATFORM"] = "offscreen"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import krok_helper.cli; "
                "print('krok_helper.gui_qt' in sys.modules)"
            ),
        ],
        cwd=Path(__file__).parents[1],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.strip().splitlines()[-1] == "False"


def test_startup_splash_set_progress_updates_status_text_and_shows_bar() -> None:
    from krok_helper.startup_splash import StartupSplashWindow

    window = StartupSplashWindow()
    assert window.progress_bar.isHidden()

    window.set_progress(42, "正在加载波形对齐模块")

    assert window.status_label.text() == "正在加载波形对齐模块... (42.0%)"
    assert not window.progress_bar.isHidden()


def test_startup_splash_set_progress_clamps_percent_into_range() -> None:
    from krok_helper.startup_splash import StartupSplashWindow

    window = StartupSplashWindow()
    window.set_progress(-5, "正在加载")

    assert window.status_label.text().endswith("(0.0%)")

    window.set_progress(120)

    assert window.status_label.text().endswith("(100.0%)")


def test_startup_splash_set_progress_never_regresses_display() -> None:
    """进度只增不减：回调回撤时保持当前显示值。"""
    from krok_helper.startup_splash import StartupSplashWindow

    window = StartupSplashWindow()
    window.set_progress(58, "正在加载")
    window.set_progress(10)

    assert window.status_label.text().endswith("(58.0%)")


def test_startup_splash_set_progress_without_stage_keeps_previous_stage() -> None:
    from krok_helper.startup_splash import StartupSplashWindow

    window = StartupSplashWindow()
    window.set_progress(30, "正在加载歌词打轴模块")
    window.set_progress(60)

    assert window.status_label.text() == "正在加载歌词打轴模块... (60.0%)"


def test_startup_splash_percent_creeps_forward_between_milestones() -> None:
    """里程碑之间数字以随机步长持续爬行，状态行带一位小数。"""
    import re

    from krok_helper.startup_splash import StartupSplashWindow

    window = StartupSplashWindow()
    window.set_progress(58, "正在加载歌词打轴模块")

    values = [window.progress_bar.value]
    for _ in range(40):
        window._creep_tick()
        values.append(window.progress_bar.value)

    assert values == sorted(values), f"爬行必须只增不减：{values}"
    assert values[-1] > values[0]
    # 爬行余量：最多超过最近锚点 _CREEP_HEADROOM。
    assert values[-1] <= 58 + window._CREEP_HEADROOM + 1e-6
    assert re.fullmatch(r"正在加载歌词打轴模块\.\.\. \(\d+\.\d%\)", window.status_label.text())


def test_startup_splash_creep_catches_up_after_blocked_time(monkeypatch) -> None:
    """主线程阻塞后恢复时，一次 tick 按墙钟补齐欠下的步数。"""
    from krok_helper import startup_splash
    from krok_helper.startup_splash import StartupSplashWindow

    monkeypatch.setattr(
        startup_splash.random, "uniform", lambda low, high: (low + high) / 2
    )
    clock = {"now": 0.0}
    monkeypatch.setattr(startup_splash.time, "monotonic", lambda: clock["now"])

    window = StartupSplashWindow()
    window.set_progress(58, "正在加载歌词打轴模块")

    clock["now"] = 3.0  # 模拟主线程被阻塞 3 秒
    window._creep_tick()

    value = window.progress_bar.value
    assert value > 60.0, f"应补偿约 30 个步长，而不是单步：{value}"
    assert value <= 58 + window._CREEP_HEADROOM + 1e-6


def test_startup_splash_creep_respects_ceiling_and_stops_at_completion(monkeypatch) -> None:
    from krok_helper import startup_splash
    from krok_helper.startup_splash import StartupSplashWindow

    # 固定步长取区间中点，保证 500 tick 后的渐近位置是确定值。
    monkeypatch.setattr(
        startup_splash.random, "uniform", lambda low, high: (low + high) / 2
    )

    window = StartupSplashWindow()
    window.set_progress(96, "正在加载")
    for _ in range(500):
        window._creep_tick()

    assert 96.0 < window.progress_bar.value <= window._CREEP_CEILING + 1e-6

    window.set_progress(100, "启动完成")
    assert not window._creep_timer.isActive()


def test_startup_splash_anchor_below_current_display_keeps_display() -> None:
    """爬行已超过新锚点时进度不回退，只切换阶段文案。"""
    from krok_helper.startup_splash import StartupSplashWindow

    window = StartupSplashWindow()
    window.set_progress(58, "正在加载歌词打轴模块")
    window._display(63.0)

    window.set_progress(60, "正在调整")

    assert window.progress_bar.value >= 63.0
    assert window.status_label.text().startswith("正在调整... (6")


def test_workbench_construction_reports_startup_progress(monkeypatch, tmp_path: Path) -> None:
    """构造期间逐模块上报进度（cli.run_gui 注入的 splash 回调契约）。"""
    monkeypatch.setenv("KARAOKE_STUDIO_SETTINGS_DIR", str(tmp_path))
    monkeypatch.delenv("KARAOKE_STUDIO_SETTINGS_APP_NAME", raising=False)

    from krok_helper import gui_qt

    app = QApplication.instance() or QApplication([])
    events: list[tuple[int, str]] = []
    window = gui_qt.KrokHelperQtApp(
        startup_progress=lambda pct, stage: events.append((pct, stage))
    )
    try:
        assert events, "构造期间没有任何启动进度上报"
        percents = [pct for pct, _ in events]
        assert percents == sorted(percents), f"进度百分比必须单调递增：{events}"
        # 100% 由外壳在主窗口 show 之后报告，构造期不应提前打满。
        assert percents[-1] < 100
        stages = "".join(stage for _, stage in events)
        for module in (
            "视频下载",
            "波形对齐",
            "音频处理",
            "歌词检索",
            "歌词打轴",
            "字幕渲染",
            "Hi-Res",
        ):
            assert module in stages, f"缺少模块「{module}」的加载上报：{events}"
    finally:
        window.close()
        window.deleteLater()
        app.processEvents()


@pytest.mark.parametrize(
    ("script_path", "packaged_start_image"),
    [
        ("scripts/build_windows.bat", r"krok_helper\assets\logo\start.jpg"),
        ("scripts/build_macos.command", "krok_helper/assets/logo/start.jpg"),
    ],
)
def test_packaged_app_requires_start_image(
    script_path: str,
    packaged_start_image: str,
) -> None:
    script = (Path(__file__).parents[1] / script_path).read_text(encoding="utf-8")

    assert packaged_start_image in script
