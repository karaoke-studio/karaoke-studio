from __future__ import annotations

from PyQt6.QtWidgets import QApplication
from qfluentwidgets import ComboBox
from qfluentwidgets.components.widgets.combo_box import ComboBoxMenu

from krok_helper.settings import AppSettings
from krok_helper.video_download.video_download_page import VideoDownloadPage


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
