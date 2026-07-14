"""Tests for the export monitor's preview sizing and DPR handling."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QSize  # noqa: E402
from PyQt6.QtGui import QImage, QPixmap  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from krok_helper.subtitle_render.frontend.main_window import (  # noqa: E402
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


def test_export_monitor_displays_frame_at_active_screen_dpr(qapp):
    view = _ExportMonitorView()
    view.resize(489, 275)
    image = QImage(1920, 1080, QImage.Format.Format_ARGB32)
    image.fill(0xFF336699)

    view.set_frame(image)

    pixmap = view.pixmap()
    assert pixmap is not None
    assert pixmap.devicePixelRatioF() == pytest.approx(view.devicePixelRatioF())
