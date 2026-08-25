"""Tests for the export monitor's preview sizing and DPR handling."""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QPoint, QSize  # noqa: E402
from PyQt6.QtGui import QImage, QPixmap  # noqa: E402
from PyQt6.QtTest import QSignalSpy  # noqa: E402
from PyQt6.QtWidgets import QApplication, QWidget  # noqa: E402

from krok_helper.subtitle_render.frontend.main_window import (  # noqa: E402
    SubtitleRenderWindow,
    _AspectRatioBox,
    _ExportMonitorView,
    _export_preview_width,
    _physical_preview_size,
    _scaled_preview_pixmap,
)
from krok_helper.subtitle_render.frontend.workflow.export_view import (  # noqa: E402
    ExportWorkspaceView,
)


class _SettingsProvider:
    def __init__(self):
        self.data = {}

    def load(self):
        return dict(self.data)

    def save(self, data):
        self.data = dict(data)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.mark.parametrize(
    ("view_size", "dpr", "output_size", "expected"),
    [
        (QSize(566, 275), 1.0, QSize(1920, 1080), 489),
        (QSize(566, 275), 1.25, QSize(1920, 1080), 611),
        (QSize(), 1.0, QSize(1920, 1080), 640),
        (QSize(566, 275), 0.0, QSize(1920, 1080), 640),
        (QSize(2000, 1200), 2.0, QSize(1920, 1080), 1920),
        (QSize(100, 100), 1.0, QSize(160, 90), 160),
    ],
)
def test_export_preview_width_matches_fitted_physical_pixels(
    view_size, dpr, output_size, expected
):
    assert (
        _export_preview_width(
            view_size,
            dpr,
            output_size.width(),
            output_size.height(),
        )
        == expected
    )


def test_physical_preview_size_scales_both_dimensions():
    assert _physical_preview_size(QSize(489, 275), 1.25) == QSize(611, 344)


def test_scaled_preview_pixmap_preserves_physical_pixels_and_dpr(qapp):
    image = QImage(1920, 1080, QImage.Format.Format_ARGB32)
    image.fill(0xFF336699)

    pixmap = _scaled_preview_pixmap(QPixmap.fromImage(image), QSize(489, 275), 1.25)

    assert pixmap.devicePixelRatioF() == pytest.approx(1.25)
    assert pixmap.size() == QSize(611, 344)


def test_scaled_preview_pixmap_crops_rounding_mismatch_to_fill_stage(qapp):
    frame = QPixmap(640, 362)  # ffmpeg ``-2`` can round the calculated height
    frame.fill(0xFF336699)

    pixmap = _scaled_preview_pixmap(frame, QSize(533, 300), 1.0)

    assert pixmap.size() == QSize(533, 300)


def test_export_monitor_displays_frame_at_active_screen_dpr(qapp):
    view = _ExportMonitorView()
    view.resize(489, 275)
    image = QImage(1920, 1080, QImage.Format.Format_ARGB32)
    image.fill(0xFF336699)

    view.set_frame(image)

    pixmap = view.pixmap()
    assert pixmap is not None
    assert pixmap.devicePixelRatioF() == pytest.approx(view.devicePixelRatioF())


def test_aspect_ratio_box_can_switch_to_export_ratio(qapp):
    child = QWidget()
    frame = _AspectRatioBox(child)
    frame.resize(1000, 700)
    frame.show()
    qapp.processEvents()

    frame.set_aspect_ratio(1440, 1080)
    qapp.processEvents()

    geometry = child.geometry()
    assert geometry.size() == QSize(933, 700)
    assert geometry.x() == pytest.approx(33, abs=1)
    assert geometry.y() == 0
    assert geometry.width() / geometry.height() == pytest.approx(4 / 3, rel=0.002)


def test_sync_preview_output_size_updates_export_monitor_ratio():
    calls: list[tuple[object, ...]] = []
    host = SimpleNamespace(
        _preview_panel=SimpleNamespace(
            set_output_size=lambda width, height: calls.append(("preview", width, height))
        ),
        _export_monitor_frame=SimpleNamespace(
            set_aspect_ratio=lambda width, height: calls.append(("monitor", width, height))
        ),
        _sync_export_monitor_card_size=lambda width, height: calls.append(
            ("card", width, height)
        ),
        _export_width_spin=SimpleNamespace(value=lambda: 1440),
        _export_height_spin=SimpleNamespace(value=lambda: 1080),
    )

    SubtitleRenderWindow._sync_preview_output_size(host)

    assert calls == [
        ("preview", 1440, 1080),
        ("monitor", 1440, 1080),
        ("card", 1440, 1080),
    ]


def test_export_monitor_matches_settings_height_and_uses_card_width(qapp):
    window = SubtitleRenderWindow(embedded=True)
    try:
        window.resize(1280, 800)
        window._stack.setCurrentWidget(window._export_tab)
        window.show()
        qapp.processEvents()

        settings_column = window.findChild(QWidget, "SrExportSettingsCol")
        assert settings_column is not None
        monitor_card = window._export_monitor_card
        assert monitor_card.height() == pytest.approx(
            settings_column.sizeHint().height(), abs=1
        )
        frame_width = window._export_monitor_frame.width()
        view_geometry = window._export_monitor_view.geometry()
        assert view_geometry.width() >= frame_width * 0.95
        assert view_geometry.width() / view_geometry.height() == pytest.approx(
            16 / 9, rel=0.005
        )
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_export_workspace_reports_user_actions_through_its_contract(qapp):
    view = ExportWorkspaceView(
        fps_options=(60, 120),
        render_worker_options=(0, 4, 8, 12, 16),
        gpu_preview_checked=True,
        gpu_controls_visible=True,
    )
    try:
        spies = {
            "location": QSignalSpy(view.locationSettingsRequested),
            "directory": QSignalSpy(view.directoryEditingFinished),
            "browse": QSignalSpy(view.browseRequested),
            "encoder": QSignalSpy(view.encoderChanged),
            "codec": QSignalSpy(view.codecChanged),
            "start": QSignalSpy(view.startRequested),
            "stop": QSignalSpy(view.stopRequested),
        }
        controls = view.controls

        controls.location_settings_button.click()
        controls.directory_edit.editingFinished.emit()
        controls.browse_button.click()
        controls.encoder_combo.setCurrentIndex(1)
        controls.codec_combo.setCurrentIndex(1)
        controls.start_button.click()
        controls.stop_button.setEnabled(True)
        controls.stop_button.click()

        assert {name: len(spy) for name, spy in spies.items()} == {
            name: 1 for name in spies
        }
    finally:
        view.close()
        view.deleteLater()
        qapp.processEvents()


def test_export_workspace_actions_reach_window_coordinator(qapp, monkeypatch):
    calls = []
    handlers = {
        "_open_export_location_settings": "location",
        "_on_export_directory_edited": "directory",
        "_browse_export_output": "browse",
        "_start_render_export": "start",
        "_stop_render_export": "stop",
    }
    for method_name, call_name in handlers.items():
        monkeypatch.setattr(
            SubtitleRenderWindow,
            method_name,
            lambda self, name=call_name: calls.append(name),
        )

    window = SubtitleRenderWindow(
        embedded=True,
        settings_provider=_SettingsProvider(),
    )
    try:
        window._export_location_settings_button.click()
        window._export_dir_edit.editingFinished.emit()
        window._export_browse_button.click()
        window._export_start_button.click()
        window._export_stop_button.setEnabled(True)
        window._export_stop_button.click()

        assert calls == ["location", "directory", "browse", "start", "stop"]
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_export_page_omits_title_block_and_initial_status(qapp):
    window = SubtitleRenderWindow(embedded=True, settings_provider=_SettingsProvider())
    try:
        assert not hasattr(window, "_export_title_label")
        assert not hasattr(window, "_export_caption_label")
        assert window._export_status_label.text() == ""
        assert (
            f"{window._export_width_spin.value()}×{window._export_height_spin.value()}"
            f" @ {window._export_fps_value()}fps"
            in window._export_format_label.text()
        )
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_export_format_label_tracks_idle_screen_controls_without_starting_export(qapp):
    window = SubtitleRenderWindow(embedded=True, settings_provider=_SettingsProvider())
    try:
        window._export_width_spin.setValue(3840)
        window._export_height_spin.setValue(2160)
        fps_index = window._export_fps_combo.findData(120)
        assert fps_index >= 0
        window._export_fps_combo.setCurrentIndex(fps_index)
        qapp.processEvents()

        assert window._export_start_button.isEnabled()
        assert "3840×2160 @ 120fps" in window._export_format_label.text()
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_export_format_label_keeps_active_job_snapshot(qapp):
    window = SubtitleRenderWindow(embedded=True, settings_provider=_SettingsProvider())
    try:
        window._export_start_button.setEnabled(False)
        window._export_format_label.setText(
            "输出格式: MP4 · H.264 (AVC) · 1920×1080 @ 60fps"
        )

        window._export_width_spin.setValue(3840)
        window._export_height_spin.setValue(2160)
        qapp.processEvents()

        assert "1920×1080 @ 60fps" in window._export_format_label.text()
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_export_cards_are_vertically_centered_above_actions(qapp):
    window = SubtitleRenderWindow(embedded=True)
    try:
        window.resize(1280, 800)
        window._stack.setCurrentWidget(window._export_tab)
        window.show()
        qapp.processEvents()

        column = window.findChild(QWidget, "SrExportColumn")
        assert column is not None
        settings_top = window._export_settings_col.mapTo(column, QPoint()).y()
        monitor_top = window._export_monitor_card.mapTo(column, QPoint()).y()
        progress_top = window._export_progress.mapTo(column, QPoint()).y()
        gap_below_cards = progress_top - (
            settings_top + window._export_settings_col.height()
        )

        assert settings_top >= 40
        assert monitor_top == settings_top
        assert gap_below_cards == pytest.approx(settings_top, abs=16)
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()
