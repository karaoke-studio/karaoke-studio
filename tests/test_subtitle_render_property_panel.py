"""Tests for A5/A6 subtitle style controls."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEvent, QPoint, QPointF, Qt  # noqa: E402
from PyQt6.QtGui import QMouseEvent, QWheelEvent  # noqa: E402
from PyQt6.QtWidgets import QApplication, QInputDialog  # noqa: E402

from krok_helper.subtitle_render.frontend import main_window as mw  # noqa: E402
from krok_helper.subtitle_render.frontend.property_panel import (  # noqa: E402
    ColorButton,
    PropertyPanel,
    ScreenSettings,
)
from krok_helper.subtitle_render.models import (  # noqa: E402
    KaraokeColors,
    KaraokeColorState,
    PaintFill,
    SubtitleStyleScheme,
    Style,
    style_from_dict,
    style_to_dict,
)


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
        fill_gradient_enabled=True,
        fill_gradient_start_color="#111111",
        fill_gradient_end_color="#EEEEEE",
        fill_gradient_angle_deg=45,
        stroke_color="#708090",
        stroke_width_px=8,
        shadow_color="#A0B0C0",
        shadow_offset_x=3,
        shadow_offset_y=4,
        viewport_align="bottom_right",
        viewport_offset_x=-120,
        viewport_offset_y=60,
        viewport_scale_pct=150,
        viewport_rotation_deg=-30,
        line_y_position="top",
        line_y_margin_px=120,
        dual_line_layout=False,
        right_to_left=True,
        vertical=True,
        line_horizontal_layout="per_row",
        line_gap_px=66,
        upper_line_left_margin_px=77,
        lower_line_right_margin_px=88,
        row1_align="center",
        row1_offset_x=11,
        row1_offset_y=-22,
        row2_align="left",
        row2_offset_x=33,
        row2_offset_y=44,
        line_lead_in_ms=900,
        line_tail_ms=1100,
        timing_offset_ms=-120,
        section_gap_ms=5000,
        sync_ending=True,
        section_ending_mode="clear",
        line_lane_gap_ms=250,
        line_max_hold_ms=9000,
        entry_anim="utopia",
        entry_lead_ms=450,
        exit_anim="char_fade",
        exit_fade_ms=650,
        lit_enabled=True,
        lit_style="rounded",
        lit_number=2,
        lit_size=36,
        lit_offset_x=90,
        lit_offset_y=70,
        lit_tracking=14,
        lit_fill_color="#333333",
        lit1_fill_color="#112233",
        lit2_fill_color="#445566",
        lit3_fill_color="#778899",
        lit_stroke_color="#AABBCC",
        lit_stroke_width=5,
        lit_stroke_soften=3,
        lit_opacity_pct=80,
        lit_edge_brightness_pct=45,
        lit_shadow=True,
        lit_time_offset_ms=-300,
        lit_waiting_time_ms=200,
        lit_transition_mode="slide",
        lit_transition_ratio_pct=30,
        lit_transition_angle_deg=45,
        lit_transition_distance=24,
        signals_duration_ms=1800,
        volume_size=64,
        volume_offset_x=12,
        volume_offset_y=-8,
        volume_column_width=10,
        volume_column_count=5,
        volume_column_spacing=3,
        volume_align=2,
        volume_ratio=4.0,
        volume_fill_color="#010203",
        volume_stroke_color="#040506",
        volume_overlay_fill_color="#070809",
        volume_overlay_stroke_color="#0A0B0C",
        volume_flash_times=6,
        volume_flash_duration_ratio=0.75,
        volume_transition_ratio_pct=55,
        ruby_font_size_px=30,
        ruby_color="#223344",
        ruby_gap_px=9,
    )

    panel.set_style(style)

    assert panel.subtitle_style == style
    assert panel._font_size_spin.value() == 72
    assert panel._font_weight_combo.currentData() == 900
    assert panel._italic_check.isChecked()
    assert panel._color_state_combo.currentData() == "after"
    assert panel._color_layer_combo.currentData() == "text"
    assert panel._fill_mode_combo.currentData() == "gradient_horizontal"
    assert panel._paint_gradient_start_btn.color == "#111111"
    assert panel._paint_gradient_end_btn.color == "#EEEEEE"
    panel._color_state_combo.setCurrentIndex(panel._color_state_combo.findData("before"))
    assert panel._paint_solid_btn.color == "#102030"
    assert panel._stroke_width_spin.value() == 8
    assert panel._shadow_x_spin.value() == 3
    assert panel._shadow_y_spin.value() == 4
    assert panel._viewport_align_combo.currentData() == "bottom_right"
    assert panel._viewport_x_spin.value() == -120
    assert panel._viewport_y_spin.value() == 60
    assert panel._viewport_scale_spin.value() == 150
    assert panel._viewport_rotation_spin.value() == -30
    assert panel._line_position_combo.currentData() == "top"
    assert panel._line_margin_spin.value() == 120
    assert not panel._dual_line_check.isChecked()
    assert panel._rtl_check.isChecked()
    assert panel._vertical_check.isChecked()
    assert panel._horizontal_layout_combo.currentData() == "per_row"
    assert panel._line_gap_spin.value() == 66
    assert panel._upper_left_spin.value() == 77
    assert panel._lower_right_spin.value() == 88
    assert panel._row1_align_combo.currentData() == "center"
    assert panel._row1_x_spin.value() == 11
    assert panel._row1_y_spin.value() == -22
    assert panel._row2_align_combo.currentData() == "left"
    assert panel._row2_x_spin.value() == 33
    assert panel._row2_y_spin.value() == 44
    assert panel._per_row_box.isEnabled()
    assert panel._line_lead_spin.value() == 900
    assert panel._line_tail_spin.value() == 1100
    assert panel._line_offset_spin.value() == -120
    assert panel._section_gap_spin.value() == 5000
    assert panel._sync_ending_check.isChecked()
    assert panel._section_ending_combo.currentData() == "clear"
    assert panel._entry_anim_combo.currentData() == "utopia"
    assert panel._entry_lead_spin.value() == 450
    assert panel._exit_anim_combo.currentData() == "char_fade"
    assert panel._exit_fade_spin.value() == 650
    assert panel._lit_enabled_check.isChecked()
    assert panel._lit_style_combo.currentData() == "rounded"
    assert panel._lit_number_spin.value() == 2
    assert panel._lit_size_spin.value() == 36
    assert panel._lit_x_spin.value() == 90
    assert panel._lit_y_spin.value() == 70
    assert panel._lit_tracking_spin.value() == 14
    assert panel._lit_fill_btn.color == "#333333"
    assert panel._lit_stroke_btn.color == "#AABBCC"
    assert panel._lit_stroke_width_spin.value() == 5
    assert panel._lit_stroke_soften_spin.value() == 3
    assert panel._lit_opacity_spin.value() == 80
    assert panel._lit_edge_brightness_spin.value() == 45
    assert panel._lit_shadow_check.isChecked()
    assert panel._lit_time_offset_spin.value() == -300
    assert panel._lit_waiting_time_spin.value() == 200
    assert panel._lit_transition_mode_combo.currentData() == "slide"
    assert panel._lit_transition_ratio_spin.value() == 30
    assert panel._lit_transition_angle_spin.value() == 45
    assert panel._lit_transition_distance_spin.value() == 24
    assert panel._lit_duration_spin.value() == 1800
    assert panel._volume_size_spin.value() == 64
    assert panel._volume_x_spin.value() == 12
    assert panel._volume_y_spin.value() == -8
    assert panel._volume_column_width_spin.value() == 10
    assert panel._volume_column_count_spin.value() == 5
    assert panel._volume_column_spacing_spin.value() == 3
    assert panel._volume_ratio_spin.value() == 4
    assert panel._volume_align_combo.currentData() == 2
    assert panel._volume_flash_times_spin.value() == 6
    assert panel._volume_flash_duration_spin.value() == 75
    assert panel._volume_transition_ratio_spin.value() == 55
    assert panel._volume_fill_btn.color == "#010203"
    assert panel._volume_stroke_btn.color == "#040506"
    assert panel._volume_overlay_fill_btn.color == "#070809"
    assert panel._volume_overlay_stroke_btn.color == "#0A0B0C"
    assert panel._ruby_font_size_spin.value() == 30
    assert panel._ruby_color_btn.color == "#223344"
    assert panel._ruby_gap_spin.value() == 9


def test_property_panel_does_not_shadow_qwidget_style(qapp):
    panel = PropertyPanel()

    qt_style = panel.style()
    qt_style.unpolish(panel)
    qt_style.polish(panel)

    assert panel.subtitle_style == panel._style


def test_style_defaults_match_nicokara_layout_baseline():
    style = Style()

    assert style.font_family == "UD Digi Kyokasho N-B"
    assert style.font_size_px == 100
    assert style.font_weight == 400
    assert style.fill_gradient_enabled is False
    assert style.fill_gradient_start_color == "#FF5A6F"
    assert style.fill_gradient_end_color == "#0055FF"
    assert style.fill_gradient_angle_deg == 0
    assert style.ruby_font_size_px == 35
    assert style.ruby_gap_px == 4
    assert style.viewport_align == "center"
    assert style.viewport_offset_x == 0
    assert style.viewport_offset_y == 0
    assert style.viewport_scale_pct == 100
    assert style.viewport_rotation_deg == 0
    assert style.line_y_position == "bottom"
    assert style.line_y_margin_px == 80
    assert style.dual_line_layout is True
    assert style.line_horizontal_layout == "asymmetric"
    assert style.right_to_left is False
    assert style.vertical is False
    assert style.row1_align == "left"
    assert style.row1_offset_x == 50
    assert style.row1_offset_y == 0
    assert style.row2_align == "right"
    assert style.row2_offset_x == -50
    assert style.row2_offset_y == 0
    assert style.line_gap_px == 90
    assert style.stroke_width_px == 9
    assert style.stroke2_width_px == 0
    assert style.decoration_kind == "shadow"
    assert style.glow_radius_px == 10
    assert style.shadow_offset_x == 0
    assert style.shadow_offset_y == 1
    assert style.upper_line_left_margin_px == 50
    assert style.lower_line_right_margin_px == 50
    assert style.line_lead_in_ms == 1800
    assert style.line_tail_ms == 1000
    assert style.timing_offset_ms == 0
    assert style.section_gap_ms == 4000
    assert style.sync_ending is False
    assert style.section_ending_mode == "hold"
    assert style.line_lane_gap_ms == 300
    assert style.line_continuity_snap_ms == 800
    assert style.line_pair_second_delay_ms == 3000
    assert style.line_max_hold_ms == 12_000
    assert style.entry_anim == "none"
    assert style.entry_lead_ms == 300
    assert style.exit_anim == "none"
    assert style.exit_fade_ms == 300
    assert style.lit_enabled is False
    assert style.lit_style == "volume"
    assert style.lit_number == 4
    assert style.lit_size == 32
    assert style.lit_offset_x == 0
    assert style.lit_offset_y == -24
    assert style.lit_tracking == 0
    assert style.lit_fill_color == "#0000FF"
    assert style.lit1_fill_color == "#FF0000"
    assert style.lit2_fill_color == "#FFFF00"
    assert style.lit3_fill_color == "#00FF00"
    assert style.lit_stroke_color == "#FFFFFF"
    assert style.lit_stroke_width == 2
    assert style.lit_stroke_soften == 0
    assert style.lit_opacity_pct == 100
    assert style.lit_edge_brightness_pct == 60
    assert style.lit_shadow is True
    assert style.lit_time_offset_ms == 0
    assert style.lit_waiting_time_ms == 0
    assert style.lit_transition_mode == "fade"
    assert style.lit_transition_ratio_pct == 67
    assert style.lit_transition_angle_deg == 0
    assert style.lit_transition_distance == 0
    assert style.signals_duration_ms == 4000
    assert style.volume_size == 48
    assert style.volume_offset_x == 0
    assert style.volume_offset_y == 0
    assert style.volume_column_width == 12
    assert style.volume_column_count == 4
    assert style.volume_column_spacing == 0
    assert style.volume_align == 1
    assert style.volume_ratio == 3.0
    assert style.volume_fill_color == "#FFFFFF"
    assert style.volume_stroke_color == "#0000FF"
    assert style.volume_overlay_fill_color == "#0000FF"
    assert style.volume_overlay_stroke_color == "#FFFFFF"
    assert style.volume_flash_times == 3
    assert style.volume_flash_duration_ratio == 1.0
    assert style.volume_transition_ratio_pct == 67


def test_property_panel_subtitle_page_has_no_horizontal_scroll(qapp):
    panel = PropertyPanel()
    basic_page = panel.widget(0)
    subtitle_page = panel.widget(1)

    assert basic_page.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    assert subtitle_page.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    assert panel._font_combo.minimumWidth() == 0
    assert panel._font_size_spin.minimumWidth() == 0
    assert panel._line_margin_spin.parentWidget() is not panel._font_size_spin.parentWidget()
    assert panel._singer_combo.parentWidget() is not panel._line_margin_spin.parentWidget()
    subtitle_layout = subtitle_page.widget().layout()
    first_section = subtitle_layout.itemAt(0).widget()
    assert first_section.layout().itemAt(0).widget().text() == "配色方案"


def test_property_panel_sections_are_collapsible(qapp):
    panel = PropertyPanel()
    subtitle_page = panel.widget(1)
    subtitle_layout = subtitle_page.widget().layout()
    first_section = subtitle_layout.itemAt(0).widget()
    header = first_section.layout().itemAt(0).widget()
    content = first_section.layout().itemAt(1).widget()

    assert header.text() == "配色方案"
    assert not content.isHidden()

    header.click()
    assert content.isHidden()
    assert header.arrowType() == Qt.ArrowType.RightArrow

    header.click()
    assert not content.isHidden()
    assert header.arrowType() == Qt.ArrowType.DownArrow


def test_property_panel_screen_preset_emits_settings(qapp):
    panel = PropertyPanel()
    emitted: list[ScreenSettings] = []
    panel.screenChanged.connect(emitted.append)

    panel._screen_preset_combo.setCurrentIndex(
        panel._screen_preset_combo.findData("hdv_1080")
    )

    assert emitted[-1] == ScreenSettings(
        preset_key="hdv_1080",
        par="4:3",
        width=1440,
        height=1080,
        fps=60,
    )
    assert panel._screen_par_combo.currentData() == "4:3"
    assert panel._screen_width_spin.value() == 1440
    assert panel._screen_height_spin.value() == 1080

    panel._screen_width_spin.setValue(1500)
    assert emitted[-1].preset_key == "custom"
    assert emitted[-1].width == 1500


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
    assert emitted[-1].karaoke_colors.after.text.color == "#123ABC"
    assert emitted[-1].karaoke_colors.after.stroke.color == "#222222"
    assert panel._paint_solid_btn.color == "#123ABC"


def test_property_panel_gradient_controls_emit_style(qapp):
    panel = PropertyPanel()
    emitted: list[Style] = []
    panel.styleChanged.connect(emitted.append)

    panel._fill_mode_combo.setCurrentIndex(
        panel._fill_mode_combo.findData("gradient_vertical")
    )
    panel._update_current_fill(start_color="#00AAEE")
    panel._update_current_fill(end_color="#FFCC00")

    fill = emitted[-1].karaoke_colors.after.text
    assert fill.mode == "gradient_vertical"
    assert fill.start_color == "#00AAEE"
    assert fill.end_color == "#FFCC00"
    assert panel._paint_gradient_start_btn.color == "#00AAEE"
    assert panel._paint_gradient_end_btn.color == "#FFCC00"


def test_property_panel_gradient_stop_editor_emits_style(qapp):
    panel = PropertyPanel()
    emitted: list[Style] = []
    panel.styleChanged.connect(emitted.append)

    panel._fill_mode_combo.setCurrentIndex(
        panel._fill_mode_combo.findData("gradient_horizontal")
    )
    panel._gradient_editor.add_stop(50, "#808080")
    panel._gradient_stop_position_spin.setValue(60)
    panel._gradient_editor.set_selected_color("#336699")

    fill = emitted[-1].karaoke_colors.after.text
    assert fill.mode == "gradient_horizontal"
    assert (60, "#336699") in fill.gradient_stops
    assert fill.start_color == "#FF5A6F"
    assert fill.end_color == "#FF5A6F"


def test_property_panel_gradient_bar_click_adds_stop(qapp):
    panel = PropertyPanel()
    emitted: list[Style] = []
    panel.styleChanged.connect(emitted.append)
    panel._fill_mode_combo.setCurrentIndex(
        panel._fill_mode_combo.findData("gradient_horizontal")
    )

    editor = panel._gradient_editor
    editor.resize(240, editor.sizeHint().height())
    point = editor._bar_rect().center()  # noqa: SLF001
    event = QMouseEvent(
        QEvent.Type.MouseButtonPress,
        point,
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )

    editor.mousePressEvent(event)

    fill = emitted[-1].karaoke_colors.after.text
    assert any(position == 50 for position, _color in fill.gradient_stops)


def test_property_panel_gradient_endpoint_stops_cannot_be_deleted(qapp):
    panel = PropertyPanel()
    panel._fill_mode_combo.setCurrentIndex(
        panel._fill_mode_combo.findData("gradient_horizontal")
    )
    editor = panel._gradient_editor

    editor._selected = 0  # noqa: SLF001
    editor.delete_selected_stop()
    editor._selected = len(editor._stops) - 1  # noqa: SLF001
    editor.delete_selected_stop()

    assert editor._stops[0][0] == 0  # noqa: SLF001
    assert editor._stops[-1][0] == 100  # noqa: SLF001


def test_property_panel_dragging_endpoint_creates_mergeable_stop(qapp):
    panel = PropertyPanel()
    panel._fill_mode_combo.setCurrentIndex(
        panel._fill_mode_combo.findData("gradient_horizontal")
    )
    editor = panel._gradient_editor
    editor.resize(240, editor.sizeHint().height())

    start = editor._marker_center(0)  # noqa: SLF001
    middle = editor._bar_rect().center()  # noqa: SLF001
    editor.mousePressEvent(
        QMouseEvent(
            QEvent.Type.MouseButtonPress,
            start,
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
    )
    editor.mouseMoveEvent(
        QMouseEvent(
            QEvent.Type.MouseMove,
            middle,
            Qt.MouseButton.NoButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
    )

    assert [position for position, _color in editor._stops] == [0, 50, 100]  # noqa: SLF001

    editor.mouseMoveEvent(
        QMouseEvent(
            QEvent.Type.MouseMove,
            start,
            Qt.MouseButton.NoButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
    )

    assert [position for position, _color in editor._stops] == [0, 100]  # noqa: SLF001


def test_property_panel_fill_editor_height_follows_current_page(qapp):
    panel = PropertyPanel()

    panel._fill_mode_combo.setCurrentIndex(
        panel._fill_mode_combo.findData("gradient_vertical")
    )
    gradient_height = panel._fill_editor_stack.sizeHint().height()
    panel._fill_mode_combo.setCurrentIndex(panel._fill_mode_combo.findData("solid"))
    solid_height = panel._fill_editor_stack.sizeHint().height()

    assert solid_height < gradient_height


def test_property_panel_split_and_image_fill_controls_emit_style(qapp):
    panel = PropertyPanel()
    emitted: list[Style] = []
    panel.styleChanged.connect(emitted.append)

    panel._fill_mode_combo.setCurrentIndex(panel._fill_mode_combo.findData("split_vertical"))
    panel._update_current_fill(split_top_color="#111111")
    panel._update_current_fill(split_bottom_color="#EEEEEE")
    panel._paint_split_position_spin.setValue(42)

    split = emitted[-1].karaoke_colors.after.text
    assert split.mode == "split_vertical"
    assert split.split_top_color == "#111111"
    assert split.split_bottom_color == "#EEEEEE"
    assert split.split_position_pct == 42

    panel._fill_mode_combo.setCurrentIndex(panel._fill_mode_combo.findData("image"))
    panel._paint_image_path_edit.setText(r"D:\cover.png")
    panel._paint_image_path_edit.editingFinished.emit()
    panel._paint_image_scale_spin.setValue(150)

    image = emitted[-1].karaoke_colors.after.text
    assert image.mode == "image"
    assert image.image_path == r"D:\cover.png"
    assert image.image_scale_pct == 150


def test_style_serialization_preserves_complex_fills_and_schemes(tmp_path):
    image_path = str(tmp_path / "texture.png")
    fill = PaintFill(
        mode="image",
        color="#112233",
        start_color="#112233",
        end_color="#445566",
        gradient_stops=[(0, "#112233"), (40, "#778899"), (100, "#445566")],
        split_top_color="#112233",
        split_bottom_color="#445566",
        split_position_pct=35,
        image_path=image_path,
        image_scale_pct=175,
    )
    scheme = SubtitleStyleScheme(
        font_size_px=88,
        fill_color="#112233",
        karaoke_colors=KaraokeColors(after=KaraokeColorState(text=fill)),
    )
    style = Style(
        entry_anim="utopia",
        entry_lead_ms=500,
        exit_anim="char_fade",
        exit_fade_ms=700,
        lit_enabled=True,
        lit_style="square",
        lit_number=2,
        lit_size=40,
        lit_offset_x=80,
        lit_offset_y=70,
        lit_tracking=12,
        lit_fill_color="#222222",
        lit1_fill_color="#FF0000",
        lit2_fill_color="#00FF00",
        lit3_fill_color="#0000FF",
        lit_stroke_color="#FFFFFF",
        lit_stroke_width=4,
        lit_stroke_soften=2,
        lit_opacity_pct=75,
        lit_edge_brightness_pct=60,
        lit_shadow=True,
        lit_time_offset_ms=-250,
        lit_waiting_time_ms=100,
        lit_transition_mode="fade",
        lit_transition_ratio_pct=25,
        lit_transition_angle_deg=-30,
        lit_transition_distance=16,
        signals_duration_ms=1500,
        volume_size=54,
        volume_offset_x=8,
        volume_offset_y=-6,
        volume_column_width=9,
        volume_column_count=6,
        volume_column_spacing=2,
        volume_align=2,
        volume_ratio=5.0,
        volume_fill_color="#101112",
        volume_stroke_color="#131415",
        volume_overlay_fill_color="#161718",
        volume_overlay_stroke_color="#191A1B",
        volume_flash_times=4,
        volume_flash_duration_ratio=0.5,
        volume_transition_ratio_pct=44,
        singer_style_overrides={2: scheme},
        custom_style_schemes={"图像方案": scheme},
    )

    restored = style_from_dict(style_to_dict(style))

    assert restored.entry_anim == "utopia"
    assert restored.exit_anim == "char_fade"
    assert restored.lit_enabled is True
    assert restored.lit_style == "square"
    assert restored.lit_number == 2
    assert restored.lit_size == 40
    assert restored.lit_offset_x == 80
    assert restored.lit_offset_y == 70
    assert restored.lit_tracking == 12
    assert restored.lit_fill_color == "#222222"
    assert restored.lit1_fill_color == "#FF0000"
    assert restored.lit2_fill_color == "#00FF00"
    assert restored.lit3_fill_color == "#0000FF"
    assert restored.lit_stroke_color == "#FFFFFF"
    assert restored.lit_stroke_width == 4
    assert restored.lit_stroke_soften == 2
    assert restored.lit_opacity_pct == 75
    assert restored.lit_edge_brightness_pct == 60
    assert restored.lit_shadow is True
    assert restored.lit_time_offset_ms == -250
    assert restored.lit_waiting_time_ms == 100
    assert restored.lit_transition_mode == "fade"
    assert restored.lit_transition_ratio_pct == 25
    assert restored.lit_transition_angle_deg == -30
    assert restored.lit_transition_distance == 16
    assert restored.signals_duration_ms == 1500
    assert restored.volume_size == 54
    assert restored.volume_offset_x == 8
    assert restored.volume_offset_y == -6
    assert restored.volume_column_width == 9
    assert restored.volume_column_count == 6
    assert restored.volume_column_spacing == 2
    assert restored.volume_align == 2
    assert restored.volume_ratio == 5.0
    assert restored.volume_fill_color == "#101112"
    assert restored.volume_stroke_color == "#131415"
    assert restored.volume_overlay_fill_color == "#161718"
    assert restored.volume_overlay_stroke_color == "#191A1B"
    assert restored.volume_flash_times == 4
    assert restored.volume_flash_duration_ratio == 0.5
    assert restored.volume_transition_ratio_pct == 44
    assert restored.singer_style_overrides[2].karaoke_colors.after.text.image_path == image_path
    assert restored.singer_style_overrides[2].karaoke_colors.after.text.image_scale_pct == 175
    assert restored.custom_style_schemes["图像方案"].karaoke_colors.after.text.mode == "image"


def test_property_panel_decoration_controls_visibility_and_emit_style(qapp):
    panel = PropertyPanel()
    emitted: list[Style] = []
    panel.styleChanged.connect(emitted.append)

    assert panel._decoration_type_field.isHidden()

    panel._color_layer_combo.setCurrentIndex(panel._color_layer_combo.findData("shadow"))
    assert not panel._decoration_type_field.isHidden()
    assert not panel._shadow_x_field.isHidden()
    assert not panel._shadow_y_field.isHidden()

    panel._decoration_type_combo.setCurrentIndex(
        panel._decoration_type_combo.findData("glow")
    )
    assert emitted[-1].decoration_kind == "glow"
    assert panel._shadow_x_field.isHidden()
    assert panel._shadow_y_field.isHidden()
    assert not panel._glow_radius_field.isHidden()

    panel._glow_radius_spin.setValue(28)
    assert emitted[-1].glow_radius_px == 28

    panel._decoration_type_combo.setCurrentIndex(
        panel._decoration_type_combo.findData("shadow")
    )
    assert emitted[-1].decoration_kind == "shadow"
    assert not panel._shadow_x_field.isHidden()
    assert not panel._shadow_y_field.isHidden()
    assert panel._glow_radius_field.isHidden()


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

    panel._rtl_check.setChecked(True)
    panel._vertical_check.setChecked(True)

    assert emitted[-1].dual_line_layout is False
    assert emitted[-1].right_to_left is True
    assert emitted[-1].vertical is True
    assert emitted[-1].line_horizontal_layout == "center"
    assert emitted[-1].line_y_margin_px == 123
    assert emitted[-1].line_gap_px == 70
    assert emitted[-1].upper_line_left_margin_px == 31
    assert emitted[-1].lower_line_right_margin_px == 42


def test_property_panel_per_row_controls_emit_style(qapp):
    panel = PropertyPanel()
    emitted: list[Style] = []
    panel.styleChanged.connect(emitted.append)

    panel._horizontal_layout_combo.setCurrentIndex(
        panel._horizontal_layout_combo.findData("per_row")
    )
    assert panel._per_row_box.isEnabled()

    panel._row1_align_combo.setCurrentIndex(
        panel._row1_align_combo.findData("center")
    )
    panel._row1_x_spin.setValue(120)
    panel._row1_y_spin.setValue(-15)
    panel._row2_align_combo.setCurrentIndex(panel._row2_align_combo.findData("left"))
    panel._row2_x_spin.setValue(-60)
    panel._row2_y_spin.setValue(25)

    assert emitted[-1].line_horizontal_layout == "per_row"
    assert emitted[-1].row1_align == "center"
    assert emitted[-1].row1_offset_x == 120
    assert emitted[-1].row1_offset_y == -15
    assert emitted[-1].row2_align == "left"
    assert emitted[-1].row2_offset_x == -60
    assert emitted[-1].row2_offset_y == 25


def test_property_panel_per_row_box_disabled_for_other_layouts(qapp):
    panel = PropertyPanel()
    # 默认 asymmetric → 逐行控件禁用
    assert not panel._per_row_box.isEnabled()
    panel._horizontal_layout_combo.setCurrentIndex(
        panel._horizontal_layout_combo.findData("per_row")
    )
    assert panel._per_row_box.isEnabled()
    panel._horizontal_layout_combo.setCurrentIndex(
        panel._horizontal_layout_combo.findData("center")
    )
    assert not panel._per_row_box.isEnabled()


def test_property_panel_viewport_controls_emit_style(qapp):
    panel = PropertyPanel()
    emitted: list[Style] = []
    panel.styleChanged.connect(emitted.append)

    panel._viewport_align_combo.setCurrentIndex(
        panel._viewport_align_combo.findData("top_left")
    )
    panel._viewport_x_spin.setValue(-80)
    panel._viewport_y_spin.setValue(45)
    panel._viewport_scale_spin.setValue(120)
    panel._viewport_rotation_spin.setValue(15)

    assert emitted[-1].viewport_align == "top_left"
    assert emitted[-1].viewport_offset_x == -80
    assert emitted[-1].viewport_offset_y == 45
    assert emitted[-1].viewport_scale_pct == 120
    assert emitted[-1].viewport_rotation_deg == 15


def test_property_panel_timing_controls_emit_style(qapp):
    panel = PropertyPanel()
    emitted: list[Style] = []
    panel.styleChanged.connect(emitted.append)

    panel._line_lead_spin.setValue(1500)
    panel._line_tail_spin.setValue(1200)
    panel._line_offset_spin.setValue(-250)
    panel._section_gap_spin.setValue(6000)
    panel._section_ending_combo.setCurrentIndex(
        panel._section_ending_combo.findData("clear")
    )
    panel._sync_ending_check.setChecked(True)

    assert emitted[-1].line_lead_in_ms == 1500
    assert emitted[-1].line_tail_ms == 1200
    assert emitted[-1].timing_offset_ms == -250
    assert emitted[-1].section_gap_ms == 6000
    assert emitted[-1].section_ending_mode == "clear"
    assert emitted[-1].sync_ending is True


def test_property_panel_animation_controls_emit_style(qapp):
    panel = PropertyPanel()
    emitted: list[Style] = []
    panel.styleChanged.connect(emitted.append)

    panel._entry_anim_combo.setCurrentIndex(
        panel._entry_anim_combo.findData("char_fade")
    )
    panel._entry_lead_spin.setValue(700)
    panel._exit_anim_combo.setCurrentIndex(panel._exit_anim_combo.findData("utopia"))
    panel._exit_fade_spin.setValue(900)

    assert emitted[-1].entry_anim == "char_fade"
    assert emitted[-1].entry_lead_ms == 700
    assert emitted[-1].exit_anim == "utopia"
    assert emitted[-1].exit_fade_ms == 900


def test_property_panel_lit_controls_emit_style(qapp):
    panel = PropertyPanel()
    emitted: list[Style] = []
    panel.styleChanged.connect(emitted.append)

    panel._lit_enabled_check.setChecked(True)
    panel._lit_style_combo.setCurrentIndex(panel._lit_style_combo.findData("square"))
    panel._lit_number_spin.setValue(2)
    panel._lit_size_spin.setValue(44)
    panel._lit_x_spin.setValue(120)
    panel._lit_y_spin.setValue(64)
    panel._lit_tracking_spin.setValue(18)
    panel._lit_duration_spin.setValue(1700)
    panel._lit_time_offset_spin.setValue(-400)
    panel._lit_stroke_width_spin.setValue(6)
    panel._lit_stroke_soften_spin.setValue(4)
    panel._lit_opacity_spin.setValue(70)
    panel._lit_edge_brightness_spin.setValue(50)
    panel._lit_shadow_check.setChecked(True)
    panel._lit_waiting_time_spin.setValue(250)
    panel._lit_transition_mode_combo.setCurrentIndex(
        panel._lit_transition_mode_combo.findData("fade")
    )
    panel._lit_transition_ratio_spin.setValue(20)
    panel._lit_transition_angle_spin.setValue(90)
    panel._lit_transition_distance_spin.setValue(30)
    panel._volume_size_spin.setValue(72)
    panel._volume_x_spin.setValue(24)
    panel._volume_y_spin.setValue(-12)
    panel._volume_column_width_spin.setValue(11)
    panel._volume_column_count_spin.setValue(5)
    panel._volume_column_spacing_spin.setValue(4)
    panel._volume_ratio_spin.setValue(6)
    panel._volume_align_combo.setCurrentIndex(panel._volume_align_combo.findData(2))
    panel._volume_flash_times_spin.setValue(5)
    panel._volume_flash_duration_spin.setValue(40)
    panel._volume_transition_ratio_spin.setValue(58)
    panel._set_color("lit_fill_color", "#111111")
    panel._set_color("lit_stroke_color", "#eeeeee")
    panel._set_color("volume_fill_color", "#112244")
    panel._set_color("volume_stroke_color", "#223355")
    panel._set_color("volume_overlay_fill_color", "#334466")
    panel._set_color("volume_overlay_stroke_color", "#445577")

    assert emitted[-1].lit_enabled is True
    assert emitted[-1].lit_style == "square"
    assert emitted[-1].lit_number == 2
    assert emitted[-1].lit_size == 44
    assert emitted[-1].lit_offset_x == 120
    assert emitted[-1].lit_offset_y == 64
    assert emitted[-1].lit_tracking == 18
    assert emitted[-1].signals_duration_ms == 1700
    assert emitted[-1].lit_time_offset_ms == -400
    assert emitted[-1].lit_stroke_width == 6
    assert emitted[-1].lit_stroke_soften == 4
    assert emitted[-1].lit_opacity_pct == 70
    assert emitted[-1].lit_edge_brightness_pct == 50
    assert emitted[-1].lit_shadow is True
    assert emitted[-1].lit_waiting_time_ms == 250
    assert emitted[-1].lit_transition_mode == "fade"
    assert emitted[-1].lit_transition_ratio_pct == 20
    assert emitted[-1].lit_transition_angle_deg == 90
    assert emitted[-1].lit_transition_distance == 30
    assert emitted[-1].lit_fill_color == "#111111"
    assert emitted[-1].lit_stroke_color == "#EEEEEE"
    assert emitted[-1].volume_size == 72
    assert emitted[-1].volume_offset_x == 24
    assert emitted[-1].volume_offset_y == -12
    assert emitted[-1].volume_column_width == 11
    assert emitted[-1].volume_column_count == 5
    assert emitted[-1].volume_column_spacing == 4
    assert emitted[-1].volume_align == 2
    assert emitted[-1].volume_ratio == 6.0
    assert emitted[-1].volume_flash_times == 5
    assert emitted[-1].volume_flash_duration_ratio == 0.4
    assert emitted[-1].volume_transition_ratio_pct == 58
    assert emitted[-1].volume_fill_color == "#112244"
    assert emitted[-1].volume_stroke_color == "#223355"
    assert emitted[-1].volume_overlay_fill_color == "#334466"
    assert emitted[-1].volume_overlay_stroke_color == "#445577"


def test_property_panel_singer_scheme_controls_emit_style(qapp):
    panel = PropertyPanel()
    panel.set_singers([(0, "A"), (1, "B")])
    emitted: list[Style] = []
    panel.styleChanged.connect(emitted.append)

    panel._singer_combo.setCurrentIndex(panel._singer_combo.findData("singer:1"))
    panel._font_size_spin.setValue(88)
    panel._set_color("fill_color", "#00aaee")
    panel._fill_mode_combo.setCurrentIndex(
        panel._fill_mode_combo.findData("gradient_horizontal")
    )
    panel._update_current_fill(start_color="#00AAEE")
    panel._update_current_fill(end_color="#FFCC00")
    panel._set_color("base_color", "#112233")
    panel._ruby_gap_spin.setValue(8)

    scheme = emitted[-1].singer_style_overrides[1]
    assert scheme.font_size_px == 88
    assert scheme.fill_color == "#00AAEE"
    assert scheme.karaoke_colors.after.text.mode == "gradient_horizontal"
    assert scheme.karaoke_colors.after.text.start_color == "#00AAEE"
    assert scheme.karaoke_colors.after.text.end_color == "#FFCC00"
    assert scheme.base_color == "#112233"
    assert scheme.karaoke_colors.before.text.color == "#112233"
    assert scheme.ruby_gap_px == 8
    assert panel._paint_gradient_start_btn.color == "#00AAEE"


def test_property_panel_singer_scheme_switches_subtitle_controls(qapp):
    panel = PropertyPanel()
    panel.set_singers([(0, "A")])
    style = Style(
        singer_style_overrides={
            0: SubtitleStyleScheme(
                font_size_px=72,
                font_weight=700,
                fill_color="#0088ff",
                fill_gradient_enabled=True,
                fill_gradient_start_color="#0088ff",
                fill_gradient_end_color="#ffcc00",
                fill_gradient_angle_deg=270,
                ruby_color="#00ff88",
                ruby_gap_px=12,
            )
        }
    )

    panel.set_style(style)
    panel._singer_combo.setCurrentIndex(panel._singer_combo.findData("singer:0"))

    assert panel._font_size_spin.value() == 72
    assert panel._font_weight_combo.currentData() == 700
    assert panel._fill_mode_combo.currentData() == "gradient_vertical"
    assert panel._paint_gradient_start_btn.color == "#0088FF"
    assert panel._paint_gradient_end_btn.color == "#FFCC00"
    assert panel._ruby_color_btn.color == "#00FF88"
    assert panel._ruby_gap_spin.value() == 12

    panel._singer_combo.setCurrentIndex(panel._singer_combo.findData("global"))
    assert panel._font_size_spin.value() == style.font_size_px
    assert panel._paint_solid_btn.color == style.fill_color


def test_property_panel_can_add_custom_scheme(qapp):
    panel = PropertyPanel()
    emitted: list[Style] = []
    panel.styleChanged.connect(emitted.append)

    panel._set_color("fill_color", "#123456")
    panel._add_custom_scheme("蓝色方案")

    assert "蓝色方案" in panel.subtitle_style.custom_style_schemes
    assert emitted[-1].custom_style_schemes["蓝色方案"].fill_color == "#123456"
    assert panel._singer_combo.currentData() == "custom:蓝色方案"

    panel._font_size_spin.setValue(77)
    assert emitted[-1].custom_style_schemes["蓝色方案"].font_size_px == 77


def test_property_panel_scheme_selection_emits_current_key(qapp):
    panel = PropertyPanel()
    panel._add_custom_scheme("图像方案")
    emitted: list[str] = []
    panel.schemeSelectionChanged.connect(emitted.append)

    panel._singer_combo.setCurrentIndex(panel._singer_combo.findData("global"))
    panel._singer_combo.setCurrentIndex(panel._singer_combo.findData("custom:图像方案"))

    assert emitted[-1] == "custom:图像方案"


def test_property_panel_add_scheme_button_ignores_clicked_checked_arg(qapp, monkeypatch):
    panel = PropertyPanel()
    monkeypatch.setattr(
        QInputDialog,
        "getText",
        lambda *args, **kwargs: ("按钮方案", True),
    )

    panel._add_scheme_button.clicked.emit(False)

    assert "按钮方案" in panel.subtitle_style.custom_style_schemes
    assert panel._singer_combo.currentData() == "custom:按钮方案"


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
    win._property_panel.set_singers([(0, "A")])
    win._property_panel._singer_combo.setCurrentIndex(
        win._property_panel._singer_combo.findData("singer:0")
    )
    win._property_panel._set_color("fill_color", "#ffcc00")

    assert win._style.font_size_px == 96
    assert win._style.fill_color == "#00AAEE"
    assert win._style.ruby_font_size_px == 28
    assert win._style.line_gap_px == 77
    assert win._style.singer_style_overrides[0].fill_color == "#FFCC00"
    assert win._preview_panel.canvas._style.font_size_px == 96
    assert win._preview_panel.canvas._style.fill_color == "#00AAEE"
    assert win._preview_panel.canvas._style.ruby_font_size_px == 28
    assert win._preview_panel.canvas._style.line_gap_px == 77
    assert win._preview_panel.canvas._style.singer_style_overrides[0].fill_color == "#FFCC00"


def test_main_window_screen_panel_updates_export_and_persists(qapp, monkeypatch):
    monkeypatch.setattr(mw.QMessageBox, "critical", lambda *a, **k: None)
    monkeypatch.setattr(mw.QMessageBox, "warning", lambda *a, **k: None)

    class FakeSettingsProvider:
        def __init__(self):
            self.data = {}

        def load(self):
            return dict(self.data)

        def save(self, data):
            self.data = dict(data)

    provider = FakeSettingsProvider()
    win = mw.SubtitleRenderWindow(embedded=True, settings_provider=provider)

    win._property_panel._screen_preset_combo.setCurrentIndex(
        win._property_panel._screen_preset_combo.findData("uhd_4k")
    )

    assert win._export_width_spin.value() == 3840
    assert win._export_height_spin.value() == 2160
    assert win._export_fps_combo.currentData() == 60
    assert win._preview_panel.canvas._output_width == 3840
    assert win._preview_panel.canvas._output_height == 2160
    assert provider.data["screen"] == {
        "preset_key": "uhd_4k",
        "par": "1:1",
        "width": 3840,
        "height": 2160,
        "fps": 60,
    }

    win._export_width_spin.setValue(4000)
    assert win._property_panel.screen_settings.preset_key == "custom"
    assert win._property_panel._screen_width_spin.value() == 4000
    assert provider.data["screen"]["preset_key"] == "custom"
    assert provider.data["screen"]["width"] == 4000

    win._export_fps_combo.setCurrentIndex(win._export_fps_combo.findData(120))
    assert win._property_panel.screen_settings.fps == 120
    assert win._property_panel._screen_fps_combo.currentData() == 120
    assert win._transport_bar._tick_timer.interval() == 8
    assert win._transport_bar._position_poll_timer.interval() == 8
    assert provider.data["screen"]["fps"] == 120


def test_main_window_persists_style_and_selected_scheme(qapp, monkeypatch):
    monkeypatch.setattr(mw.QMessageBox, "critical", lambda *a, **k: None)
    monkeypatch.setattr(mw.QMessageBox, "warning", lambda *a, **k: None)
    initial_style = Style(
        custom_style_schemes={
            "图像方案": SubtitleStyleScheme(
                karaoke_colors=KaraokeColors(
                    after=KaraokeColorState(
                        text=PaintFill(
                            mode="image",
                            image_path=r"D:\cover.png",
                            image_scale_pct=150,
                        )
                    )
                )
            )
        }
    )

    class FakeSettingsProvider:
        def __init__(self):
            self.data = {
                "style": style_to_dict(initial_style),
                "selected_scheme_key": "custom:图像方案",
            }

        def load(self):
            return dict(self.data)

        def save(self, data):
            self.data = dict(data)

    provider = FakeSettingsProvider()
    win = mw.SubtitleRenderWindow(embedded=True, settings_provider=provider)

    assert win._property_panel.current_scheme_key() == "custom:图像方案"
    assert win._style.custom_style_schemes["图像方案"].karaoke_colors.after.text.image_path == r"D:\cover.png"

    win._property_panel._paint_image_scale_spin.setValue(175)
    win._property_panel.set_current_scheme_key("global")

    saved_style = style_from_dict(provider.data["style"])
    assert saved_style.custom_style_schemes["图像方案"].karaoke_colors.after.text.image_scale_pct == 175
    assert provider.data["selected_scheme_key"] == "global"


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
