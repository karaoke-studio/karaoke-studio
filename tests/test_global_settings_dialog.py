from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QScrollArea, QWidget

from krok_helper import gui_qt
from krok_helper.global_settings import page as settings_page
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
    captured: dict[str, settings_page.QDialog] = {}

    monkeypatch.setattr(
        settings_page,
        "ensure_updater_settings",
        lambda _settings: UpdaterSettings(),
    )

    def capture_dialog(dialog: settings_page.QDialog) -> int:
        captured["dialog"] = dialog
        dialog.show()
        app.processEvents()
        return 0

    monkeypatch.setattr(settings_page.ModelessDialog, "exec", capture_dialog)

    host = _SettingsHost()
    gui_qt.KrokHelperQtApp._open_global_settings_window(host)

    return app, captured["dialog"], host


def test_global_settings_actions_stay_outside_scroll_area(monkeypatch) -> None:
    _app, dialog, host = _open_global_settings_dialog(monkeypatch)
    scroll_areas = dialog.findChildren(QScrollArea)
    buttons = {button.text(): button for button in dialog.findChildren(settings_page.QPushButton)}

    assert scroll_areas
    save_button = buttons["保存设置"]
    close_button = buttons["关闭"]

    for scroll in scroll_areas:
        assert not scroll.isAncestorOf(save_button)
        assert not scroll.isAncestorOf(close_button)

    settings_stack = dialog.findChild(settings_page.QStackedWidget)
    assert settings_stack is not None
    stack_top = settings_stack.mapTo(dialog, settings_stack.rect().topLeft()).y()
    save_center = save_button.mapTo(dialog, save_button.rect().center())
    close_center = close_button.mapTo(dialog, close_button.rect().center())
    assert save_center.y() < stack_top
    assert save_center.x() > dialog.width() / 2
    assert save_center.x() < close_center.x()

    dialog.close()
    host.close()


def test_application_update_controls_use_setting_cards(monkeypatch) -> None:
    app, dialog, host = _open_global_settings_dialog(monkeypatch)
    settings_stack = dialog.findChild(settings_page.QStackedWidget)
    assert settings_stack is not None
    settings_stack.setCurrentIndex(2)
    app.processEvents()

    update_group = next(
        group
        for group in dialog.findChildren(settings_page.SettingCardGroup)
        if group.titleLabel.text() == "应用更新"
    )
    # ExpandLayout.count() 不报告 widget 数量，改为按创建顺序遍历子 SettingCard。
    cards = update_group.findChildren(settings_page.SettingCard)
    assert [card.titleLabel.text() for card in cards] == [
        "启用工作台自动更新",
        "启动时静默检查更新",
        "启动检查间隔",
        "更新源优先级",
        "立即检查更新",
    ]

    # 两个开关卡使用 SwitchButton；间隔 / 顺序 / 立即检查卡保留原有控件。
    assert len(update_group.findChildren(settings_page.SwitchButton)) == 2

    buttons = {button.text() for button in update_group.findChildren(settings_page.QPushButton)}
    assert {"编辑顺序", "检查更新"} <= buttons

    interval_edits = [
        edit
        for edit in update_group.findChildren(settings_page.QLineEdit)
        if edit.text() == str(UpdaterSettings().min_check_interval_hours)
    ]
    assert interval_edits

    dialog.close()
    host.close()


def test_using_system_path_saves_empty_ffmpeg_directory(monkeypatch) -> None:
    app, dialog, host = _open_global_settings_dialog(monkeypatch)
    monkeypatch.setattr(UpdaterSettings, "save", lambda self, settings: None)
    buttons = {button.text(): button for button in dialog.findChildren(settings_page.QPushButton)}

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


def test_update_source_order_dialog_is_mask_free_and_modeless() -> None:
    app = QApplication.instance() or QApplication([])
    host = QWidget()
    dialog = settings_page.UpdateSourceOrderDialog(["github", "ghproxy"], host)

    assert isinstance(dialog, settings_page.ModelessDialog)
    assert not dialog.isModal()
    assert dialog.windowModality() == Qt.WindowModality.NonModal
    assert host.isEnabled()

    dialog.close()
    host.close()
    app.processEvents()
