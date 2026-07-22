from __future__ import annotations

import logging
import sys

from PyQt6.QtCore import Qt

from krok_helper.updater.progress_window import UpdateProgressWindow
from krok_helper.updater_app import build_updater
from krok_helper.updater_app import main as workbench_updater


def test_updater_is_built_as_windowed_gui() -> None:
    args = build_updater.pyinstaller_args()

    assert "--windowed" in args
    assert "--console" not in args
    assert "--hidden-import=updater_app.gui" in args
    assert "--hidden-import=PyQt6.QtWidgets" in args
    assert "--hidden-import=qfluentwidgets" in args
    assert "--exclude-module=PyQt6" not in args
    assert "--exclude-module=qfluentwidgets" not in args


def test_workbench_registers_sug_gui_import_alias() -> None:
    previous = sys.modules.pop("gui", None)
    try:
        workbench_updater._enable_gui()
        from updater_app import gui as updater_gui

        assert sys.modules["gui"] is updater_gui
    finally:
        sys.modules.pop("gui", None)
        if previous is not None:
            sys.modules["gui"] = previous


def test_workbench_rebrands_sug_updater_log_messages() -> None:
    record = logging.LogRecord(
        "sug.updater",
        logging.INFO,
        __file__,
        1,
        "StrangeUtaGame Updater 启动",
        (),
        None,
    )

    assert workbench_updater._WorkbenchProductFilter().filter(record)
    assert record.getMessage() == "Karaoke Studio Updater 启动"


def test_workbench_update_progress_window_is_not_always_on_top(qapp) -> None:
    window = UpdateProgressWindow()

    assert window.windowFlags() & Qt.WindowType.FramelessWindowHint
    assert not window.windowFlags() & Qt.WindowType.WindowStaysOnTopHint


def test_reused_sug_updater_window_is_not_always_on_top(qapp) -> None:
    workbench_updater._enable_gui()
    from updater_app.gui import _UpdaterWindow

    window = _UpdaterWindow("Karaoke Studio Updater")

    assert window.windowFlags() & Qt.WindowType.FramelessWindowHint
    assert not window.windowFlags() & Qt.WindowType.WindowStaysOnTopHint
