"""Tests for the export monitor's preview sizing and DPR handling."""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QPoint, QSize  # noqa: E402
from PyQt6.QtGui import QImage, QPixmap  # noqa: E402
from PyQt6.QtWidgets import QApplication, QWidget  # noqa: E402

from krok_helper.subtitle_render.frontend.main_window import (  # noqa: E402
    SubtitleRenderWindow,
    _AspectRatioBox,
    _ExportMonitorView,
    _export_preview_width,
    _physical_preview_size,
    _scaled_preview_pixmap,
)


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


def test_export_page_omits_title_block_and_initial_status(qapp):
    window = SubtitleRenderWindow(embedded=True)
    try:
        assert not hasattr(window, "_export_title_label")
        assert not hasattr(window, "_export_caption_label")
        assert window._export_status_label.text() == ""
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
