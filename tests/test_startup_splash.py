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
