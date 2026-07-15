from __future__ import annotations

from PyQt6.QtWidgets import QApplication, QScrollArea, QWidget

from krok_helper import gui_qt
from krok_helper.settings import AppSettings
from krok_helper.updater.settings import UpdaterSettings


class _SettingsHost(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.settings = AppSettings()
        self.ffmpeg_dir_text = ""

    def _install_single_click_combo_behavior(self, _combo: QWidget) -> None:
        pass

    def set_ffmpeg_dir(self, path) -> None:
        gui_qt.KrokHelperQtApp.set_ffmpeg_dir(self, path)

    def _sync_ffmpeg_labels(self) -> None:
        pass

    def _sync_lyrics_timing_host_paths(self) -> None:
        pass


def _open_global_settings_dialog(monkeypatch):
    app = QApplication.instance() or QApplication([])
    captured: dict[str, gui_qt.QDialog] = {}

    monkeypatch.setattr(
        gui_qt,
        "ensure_updater_settings",
        lambda _settings: UpdaterSettings(),
    )

    def capture_dialog(dialog: gui_qt.QDialog) -> int:
        captured["dialog"] = dialog
        dialog.show()
        app.processEvents()
        return 0

    monkeypatch.setattr(gui_qt.QDialog, "exec", capture_dialog)

    host = _SettingsHost()
    gui_qt.KrokHelperQtApp._open_global_settings_window(host)

    return app, captured["dialog"], host


def test_global_settings_actions_stay_outside_scroll_area(monkeypatch) -> None:
    _app, dialog, host = _open_global_settings_dialog(monkeypatch)
    scroll = dialog.findChild(QScrollArea)
    buttons = {button.text(): button for button in dialog.findChildren(gui_qt.QPushButton)}

    assert scroll is not None
    save_button = buttons["保存设置"]
    close_button = buttons["关闭"]

    assert not scroll.isAncestorOf(save_button)
    assert not scroll.isAncestorOf(close_button)

    save_center = save_button.mapTo(dialog, save_button.rect().center())
    close_center = close_button.mapTo(dialog, close_button.rect().center())
    assert save_center.y() < scroll.geometry().top()
    assert save_center.x() > dialog.width() / 2
    assert save_center.x() < close_center.x()

    dialog.close()
    host.close()


def test_application_update_controls_use_compact_rows(monkeypatch) -> None:
    app, dialog, host = _open_global_settings_dialog(monkeypatch)
    settings_stack = dialog.findChild(gui_qt.QStackedWidget)
    assert settings_stack is not None
    settings_stack.setCurrentIndex(2)
    app.processEvents()

    update_title = next(
        label for label in dialog.findChildren(gui_qt.QLabel) if label.text() == "应用更新"
    )
    update_panel = update_title.parentWidget()
    update_layout = update_panel.layout()
    checkboxes = {
        checkbox.text(): checkbox
        for checkbox in update_panel.findChildren(gui_qt.QCheckBox)
    }
    labels = {
        label.text(): label
        for label in update_panel.findChildren(gui_qt.QLabel)
    }

    def grid_row(widget: QWidget) -> int:
        index = update_layout.indexOf(widget)
        row, _column, _row_span, _column_span = update_layout.getItemPosition(index)
        return row

    assert grid_row(checkboxes["启用工作台自动更新"]) == 1
    assert grid_row(checkboxes["启动时静默检查更新"]) == 1
    assert grid_row(labels["启动检查间隔"]) == 2
    assert grid_row(labels["更新源优先级"]) == 3
    assert grid_row(labels["立即检查更新"]) == 4

    dialog.close()
    host.close()


def test_using_system_path_saves_empty_ffmpeg_directory(monkeypatch) -> None:
    app, dialog, host = _open_global_settings_dialog(monkeypatch)
    monkeypatch.setattr(UpdaterSettings, "save", lambda self, settings: None)
    buttons = {button.text(): button for button in dialog.findChildren(gui_qt.QPushButton)}

    buttons["使用系统 PATH"].click()
    buttons["保存设置"].click()
    app.processEvents()

    assert host.ffmpeg_dir_text == ""
    assert host.settings.ffmpeg_dir == ""

    dialog.close()
    host.close()


def test_set_ffmpeg_dir_accepts_none_as_system_path() -> None:
    host = _SettingsHost()

    host.set_ffmpeg_dir(None)

    assert host.ffmpeg_dir_text == ""
    host.close()
