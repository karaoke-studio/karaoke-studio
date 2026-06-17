"""Tests for A5/A6 subtitle style controls."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from krok_helper.subtitle_render.frontend import main_window as mw  # noqa: E402
from krok_helper.subtitle_render.frontend.property_panel import (  # noqa: E402
    ColorButton,
    PropertyPanel,
)
from krok_helper.subtitle_render.models import Style  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def test_property_panel_set_style_populates_controls(qapp):
    panel = PropertyPanel()
    style = Style(
        font_family="Microsoft YaHei UI",
        font_size_px=72,
        font_weight=900,
        italic=True,
        base_color="#102030",
        fill_color="#405060",
        stroke_color="#708090",
        stroke_width_px=8,
        shadow_color="#A0B0C0",
        shadow_offset_x=3,
        shadow_offset_y=4,
        line_y_position="top",
        line_y_margin_px=120,
    )

    panel.set_style(style)

    assert panel.style == style
    assert panel._font_size_spin.value() == 72
    assert panel._font_weight_combo.currentData() == 900
    assert panel._italic_check.isChecked()
    assert panel._base_color_btn.color == "#102030"
    assert panel._fill_color_btn.color == "#405060"
    assert panel._stroke_color_btn.color == "#708090"
    assert panel._stroke_width_spin.value() == 8
    assert panel._shadow_color_btn.color == "#A0B0C0"
    assert panel._shadow_x_spin.value() == 3
    assert panel._shadow_y_spin.value() == 4
    assert panel._line_position_combo.currentData() == "top"
    assert panel._line_margin_spin.value() == 120


def test_property_panel_font_controls_emit_style(qapp):
    panel = PropertyPanel()
    emitted: list[Style] = []
    panel.styleChanged.connect(emitted.append)

    panel._font_size_spin.setValue(88)
    panel._font_weight_combo.setCurrentIndex(panel._font_weight_combo.findData(500))
    panel._italic_check.setChecked(True)

    assert emitted[-1].font_size_px == 88
    assert emitted[-1].font_weight == 500
    assert emitted[-1].italic is True


def test_property_panel_color_controls_emit_normalized_style(qapp):
    panel = PropertyPanel()
    emitted: list[Style] = []
    panel.styleChanged.connect(emitted.append)

    panel._set_color("fill_color", "#123abc")
    panel._set_color("stroke_color", "not-a-color")

    assert emitted[-1].fill_color == "#123ABC"
    assert emitted[-1].stroke_color == "#222222"
    assert panel._fill_color_btn.color == "#123ABC"
    assert panel._stroke_color_btn.color == "#222222"


def test_color_button_updates_text_and_color(qapp):
    button = ColorButton("#abcdef")
    assert button.color == "#ABCDEF"
    assert button.text() == "#ABCDEF"
    button.set_color("#010203")
    assert button.color == "#010203"
    assert button.text() == "#010203"


def test_main_window_style_panel_updates_preview(qapp, monkeypatch):
    monkeypatch.setattr(mw.QMessageBox, "critical", lambda *a, **k: None)
    monkeypatch.setattr(mw.QMessageBox, "warning", lambda *a, **k: None)
    monkeypatch.setattr(
        mw.SubtitleRenderWindow,
        "_resolve_ffprobe_path",
        lambda self: "ffprobe",
    )
    win = mw.SubtitleRenderWindow(embedded=False)

    win._property_panel._font_size_spin.setValue(96)
    win._property_panel._set_color("fill_color", "#00aaee")

    assert win._style.font_size_px == 96
    assert win._style.fill_color == "#00AAEE"
    assert win._preview_panel.canvas._style.font_size_px == 96
    assert win._preview_panel.canvas._style.fill_color == "#00AAEE"
