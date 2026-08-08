from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QSplitter
from qfluentwidgets import ComboBox
from qfluentwidgets.components.widgets.combo_box import ComboBoxMenu

from krok_helper.settings import AppSettings
from krok_helper.video_download.video_download_page import VIDEO_INFO_COLLAPSED_HEIGHT, VideoDownloadPage


def test_format_selector_uses_native_fluent_combo(monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(VideoDownloadPage, "_refresh_cookie_status", lambda _self: None)
    monkeypatch.setattr(VideoDownloadPage, "_refresh_youtube_cookie_status", lambda _self: None)
    monkeypatch.setattr(VideoDownloadPage, "_ensure_qr_login", lambda _self: None)

    page = VideoDownloadPage(AppSettings(), lambda: None)
    try:
        assert type(page.format_combo) is ComboBox
        assert type(page.format_combo._createComboMenu()) is ComboBoxMenu
    finally:
        page.close()
        page.deleteLater()
        app.processEvents()


def test_download_workspace_panels_use_buttons_for_safe_collapsing(monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(VideoDownloadPage, "_refresh_cookie_status", lambda _self: None)
    monkeypatch.setattr(VideoDownloadPage, "_refresh_youtube_cookie_status", lambda _self: None)
    monkeypatch.setattr(VideoDownloadPage, "_ensure_qr_login", lambda _self: None)

    page = VideoDownloadPage(AppSettings(), lambda: None)
    try:
        assert isinstance(page.main_splitter, QSplitter)
        assert page.main_splitter.orientation() == Qt.Orientation.Horizontal
        assert page.main_splitter.count() == 2
        assert not page.main_splitter.childrenCollapsible()

        assert isinstance(page.content_splitter, QSplitter)
        assert page.content_splitter.orientation() == Qt.Orientation.Vertical
        assert page.content_splitter.count() == 3
        assert not page.content_splitter.childrenCollapsible()

        page.resize(1480, 900)
        page.show()
        app.processEvents()

        page.main_splitter.setSizes([0, 1400])
        page.content_splitter.setSizes([0, 0, 800])
        app.processEvents()
        assert page.main_splitter.sizes()[0] > 0
        assert all(size > 0 for size in page.content_splitter.sizes())

        page._panel_collapse_buttons["input"].click()
        page._panel_collapse_buttons["info"].click()
        app.processEvents()
        assert page._collapsed_panels == {"input", "info"}
        assert page.link_input.isHidden()
        assert page.video_details_stack.isHidden()
        assert page.content_splitter.widget(0).height() == VIDEO_INFO_COLLAPSED_HEIGHT
        assert page.content_splitter.widget(1).height() == VIDEO_INFO_COLLAPSED_HEIGHT
        assert not page.download_table.isHidden()
        assert not page._panel_collapse_buttons["download"].isEnabled()

        page._panel_collapse_buttons["download"].click()
        assert page._collapsed_panels == {"input", "info"}

        page._panel_collapse_buttons["input"].click()
        app.processEvents()
        assert page._collapsed_panels == {"info"}
        assert not page.link_input.isHidden()
        assert page._panel_collapse_buttons["download"].isEnabled()

        page._panel_collapse_buttons["download"].click()
        app.processEvents()
        assert page._collapsed_panels == {"info", "download"}
        info_height = page.content_splitter.widget(1).height()
        download_height = page.content_splitter.widget(2).height()
        assert info_height == VIDEO_INFO_COLLAPSED_HEIGHT
        assert download_height == info_height

        input_card = page.content_splitter.widget(0)
        info_card = page.content_splitter.widget(1)
        assert input_card.maximumHeight() > input_card.minimumHeight()
        assert info_card.maximumHeight() == info_card.minimumHeight()
        assert page.link_input.maximumHeight() > page.link_input.minimumHeight()
        assert page.task_switch_combo.maximumWidth() > page.task_switch_combo.minimumWidth()
    finally:
        page.close()
        page.deleteLater()
        app.processEvents()
