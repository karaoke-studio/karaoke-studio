"""Tests for A5/A6 subtitle style controls."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QPoint, QPointF, Qt  # noqa: E402
from PyQt6.QtGui import QWheelEvent  # noqa: E402
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
        dual_line_layout=False,
        line_horizontal_layout="center",
        line_gap_px=66,
        upper_line_left_margin_px=77,
        lower_line_right_margin_px=88,
        line_lead_in_ms=900,
        line_tail_ms=1100,
        line_lane_gap_ms=250,
        line_max_hold_ms=9000,
        ruby_font_size_px=30,
        ruby_color="#223344",
        ruby_gap_px=9,
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
    assert not panel._dual_line_check.isChecked()
    assert panel._horizontal_layout_combo.currentData() == "center"
    assert panel._line_gap_spin.value() == 66
    assert panel._upper_left_spin.value() == 77
    assert panel._lower_right_spin.value() == 88
    assert panel._line_lead_spin.value() == 900
    assert panel._line_tail_spin.value() == 1100
    assert panel._line_lane_gap_spin.value() == 250
    assert panel._line_max_hold_spin.value() == 9000
    assert panel._ruby_font_size_spin.value() == 30
    assert panel._ruby_color_btn.color == "#223344"
    assert panel._ruby_gap_spin.value() == 9


def test_style_defaults_match_nicokara_layout_baseline():
    style = Style()

    assert style.font_family == "UD Digi Kyokasho N-B"
    assert style.font_size_px == 100
    assert style.font_weight == 400
    assert style.ruby_font_size_px == 35
    assert style.ruby_gap_px == 4
    assert style.line_y_position == "bottom"
    assert style.line_y_margin_px == 80
    assert style.dual_line_layout is True
    assert style.line_horizontal_layout == "asymmetric"
    assert style.line_gap_px == 90
    assert style.stroke_width_px == 9
    assert style.shadow_offset_x == 0
    assert style.shadow_offset_y == 1
    assert style.upper_line_left_margin_px == 50
    assert style.lower_line_right_margin_px == 50
    assert style.line_lead_in_ms == 1800
    assert style.line_tail_ms == 1000
    assert style.line_lane_gap_ms == 300
    assert style.line_continuity_snap_ms == 800
    assert style.line_pair_second_delay_ms == 3000
    assert style.line_max_hold_ms == 12_000


def test_property_panel_subtitle_page_has_no_horizontal_scroll(qapp):
    panel = PropertyPanel()
    subtitle_page = panel.widget(1)

    assert subtitle_page.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    assert panel._font_combo.minimumWidth() == 0
    assert panel._font_size_spin.minimumWidth() == 0


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


def test_property_panel_ruby_controls_emit_style(qapp):
    panel = PropertyPanel()
    emitted: list[Style] = []
    panel.styleChanged.connect(emitted.append)

    panel._ruby_font_size_spin.setValue(34)
    panel._ruby_gap_spin.setValue(11)
    panel._set_color("ruby_color", "#00aa77")

    assert emitted[-1].ruby_font_size_px == 34
    assert emitted[-1].ruby_gap_px == 11
    assert emitted[-1].ruby_color == "#00AA77"
    assert panel._ruby_color_btn.color == "#00AA77"


def test_property_panel_layout_controls_emit_style(qapp):
    panel = PropertyPanel()
    emitted: list[Style] = []
    panel.styleChanged.connect(emitted.append)

    panel._dual_line_check.setChecked(False)
    panel._horizontal_layout_combo.setCurrentIndex(
        panel._horizontal_layout_combo.findData("center")
    )
    panel._line_margin_spin.setValue(123)
    panel._line_gap_spin.setValue(70)
    panel._upper_left_spin.setValue(31)
    panel._lower_right_spin.setValue(42)

    assert emitted[-1].dual_line_layout is False
    assert emitted[-1].line_horizontal_layout == "center"
    assert emitted[-1].line_y_margin_px == 123
    assert emitted[-1].line_gap_px == 70
    assert emitted[-1].upper_line_left_margin_px == 31
    assert emitted[-1].lower_line_right_margin_px == 42


def test_property_panel_timing_controls_emit_style(qapp):
    panel = PropertyPanel()
    emitted: list[Style] = []
    panel.styleChanged.connect(emitted.append)

    panel._line_lead_spin.setValue(1500)
    panel._line_tail_spin.setValue(1200)
    panel._line_lane_gap_spin.setValue(450)
    panel._line_max_hold_spin.setValue(8000)

    assert emitted[-1].line_lead_in_ms == 1500
    assert emitted[-1].line_tail_ms == 1200
    assert emitted[-1].line_lane_gap_ms == 450
    assert emitted[-1].line_max_hold_ms == 8000


def test_wheel_changes_spinbox_only_when_focused(qapp):
    panel = PropertyPanel()
    panel.show()
    spin = panel._font_size_spin
    assert spin.focusPolicy() == Qt.FocusPolicy.StrongFocus
    assert spin.lineEdit().focusPolicy() == Qt.FocusPolicy.StrongFocus
    spin.setValue(100)
    panel.setFocus()
    spin.clearFocus()
    qapp.processEvents()

    unfocused_event = _wheel_event(spin)
    QApplication.sendEvent(spin, unfocused_event)
    assert spin.value() == 100
    assert not unfocused_event.isAccepted()

    spin.setFocus(Qt.FocusReason.MouseFocusReason)
    qapp.processEvents()
    focused_event = _wheel_event(spin)
    QApplication.sendEvent(spin, focused_event)
    assert spin.value() != 100


def test_unfocused_wheel_does_not_change_combo(qapp):
    panel = PropertyPanel()
    panel.show()
    combo = panel._font_weight_combo
    assert combo.focusPolicy() == Qt.FocusPolicy.StrongFocus
    combo.setCurrentIndex(combo.findData(400))
    panel.setFocus()
    combo.clearFocus()
    qapp.processEvents()

    unfocused_event = _wheel_event(combo)
    QApplication.sendEvent(combo, unfocused_event)
    assert combo.currentData() == 400
    assert not unfocused_event.isAccepted()


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
    win._property_panel._ruby_font_size_spin.setValue(28)
    win._property_panel._line_gap_spin.setValue(77)

    assert win._style.font_size_px == 96
    assert win._style.fill_color == "#00AAEE"
    assert win._style.ruby_font_size_px == 28
    assert win._style.line_gap_px == 77
    assert win._preview_panel.canvas._style.font_size_px == 96
    assert win._preview_panel.canvas._style.fill_color == "#00AAEE"
    assert win._preview_panel.canvas._style.ruby_font_size_px == 28
    assert win._preview_panel.canvas._style.line_gap_px == 77


def _wheel_event(widget, delta: int = 120) -> QWheelEvent:
    center = QPointF(widget.rect().center())
    global_center = QPointF(widget.mapToGlobal(widget.rect().center()))
    return QWheelEvent(
        center,
        global_center,
        QPoint(0, 0),
        QPoint(0, delta),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.ScrollUpdate,
        False,
    )
