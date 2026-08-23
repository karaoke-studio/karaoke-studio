from __future__ import annotations

import logging
import sys
from types import SimpleNamespace

from PyQt6.QtCore import Qt

from krok_helper.updater.progress_window import UpdateProgressWindow
from krok_helper.updater_app import build_updater
from krok_helper.updater_app import diagnostics
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
    assert record.getMessage() == "Lin-K Lyrics Updater 启动"


def test_workbench_update_progress_window_is_not_always_on_top(qapp) -> None:
    parent = UpdateProgressWindow()
    window = UpdateProgressWindow(parent)

    assert window.windowFlags() & Qt.WindowType.FramelessWindowHint
    assert window.windowFlags() & Qt.WindowType.Window
    assert not window.windowFlags() & Qt.WindowType.WindowStaysOnTopHint
    assert window.parentWidget() is parent


def test_reused_sug_updater_window_is_not_always_on_top(qapp) -> None:
    workbench_updater._enable_gui()
    from updater_app.gui import _UpdaterWindow

    window = _UpdaterWindow("Karaoke Studio Updater")

    assert window.windowFlags() & Qt.WindowType.FramelessWindowHint
    assert not window.windowFlags() & Qt.WindowType.WindowStaysOnTopHint
    assert getattr(type(window), "_workbench_foreground_patch", False)


def test_failed_window_exposes_diagnostic_folder_and_copy_actions(
    qapp, tmp_path
) -> None:
    workbench_updater._enable_gui()
    from updater_app.gui import _UpdaterWindow

    args = SimpleNamespace(
        pid=1,
        target_version="4.2.6.9",
        target_tag="v4.2.6.9",
        app_dir=tmp_path / "app",
        app_exe="Lin-K Lyrics.exe",
        internal_name="_internal",
    )
    diagnostics.begin_session(args, root=tmp_path / "diagnostics")
    bundle = diagnostics.persist_failure("test_failure")
    assert bundle is not None
    window = _UpdaterWindow("Lin-K Lyrics Updater")

    workbench_updater._show_diagnostic_actions(window)

    assert window._workbench_open_diagnostics.text() == "打开诊断目录"
    assert window._workbench_copy_diagnostics.text() == "复制诊断路径"
    window._workbench_copy_diagnostics.click()
    assert qapp.clipboard().text() == str(bundle)
