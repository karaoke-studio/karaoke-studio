"""Focused contracts for role color-page composition."""

from pathlib import Path

from PyQt6.QtWidgets import QToolButton, QWidget

from krok_helper.subtitle_render.frontend.properties.roles.color import (
    RoleColorPropertyPageBuilder,
)


class _AnchoredAction(QToolButton):
    def __init__(self, _panel, _first, _second, parent=None) -> None:
        super().__init__(parent)


class _Host:
    def __init__(self) -> None:
        self.fill_updates = []
        self.decoration_updates = []

    def _on_color_subject_changed(self):
        pass

    def _on_color_target_combo_changed(self):
        pass

    def _on_color_state_tab_changed(self, _state):
        pass

    def _on_color_subject_tab_changed(self, _subject):
        pass

    def _swap_karaoke_color_states(self):
        pass

    def _on_color_layer_pill_changed(self, _layer):
        pass

    def _update_current_fill(self, **changes):
        self.fill_updates.append(changes)

    def _update_shared_decoration(self, **changes):
        self.decoration_updates.append(changes)

    def _on_decoration_sync_toggled(self, _checked):
        pass

    def _make_solid_fill_page(self):
        return QWidget()

    def _make_gradient_fill_page(self):
        return QWidget()

    def _make_split_fill_page(self):
        return QWidget()

    def _make_image_fill_page(self):
        return QWidget()

    def _on_ruby_colors_follow_main_toggled(self, _checked):
        pass

    def _apply_main_colors_to_ruby(self):
        pass

    def _set_ruby_color_controls_visible(self, _visible):
        pass


def _builder(host: _Host) -> RoleColorPropertyPageBuilder:
    return RoleColorPropertyPageBuilder(
        host,
        anchored_action_factory=_AnchoredAction,
        color_state_swap_icon=Path("missing.svg"),
        fill_mode_icons_provider=dict,
    )


def test_role_color_builder_preserves_tabs_layers_and_editor_order(qapp) -> None:
    host = _Host()
    section = _builder(host).make_section()

    assert section.header.text() == "颜色"
    assert host._color_state_combo.currentData() == "after"
    assert host._color_subject_combo.count() == 2
    assert host._color_layer_combo.count() == 5
    assert host._color_layer_pill.current() == "text"
    assert host._fill_mode_combo.count() == 6
    assert host._fill_mode_pill.current() == "solid"
    assert list(host._fill_mode_pill._buttons.keys()) == [
        "solid",
        "gradient_linear",
        "gradient_radial",
        "split_vertical",
        "image",
    ]
    assert host._fill_editor_stack.count() == 4
    assert host._decoration_type_combo.count() == 3
    assert host._shadow_enabled_check.text() == "阴影"
    assert host._glow_enabled_check.text() == "发光"
    assert host._ruby_colors_follow_main_check.isChecked()


def test_role_color_builder_routes_fill_and_decoration_controls(qapp) -> None:
    host = _Host()
    _builder(host).make_section()

    host._fill_mode_combo.setCurrentIndex(1)
    host._shadow_x_spin.setValue(4)
    host._glow_before_radius_spin.setValue(8)

    assert host.fill_updates == [{"mode": "gradient_horizontal"}]
    assert host.decoration_updates == [
        {"shadow_offset_x": 4},
        {"glow_before_radius_px": 8},
    ]


def test_gradient_upgrade_paint_serialization_roundtrip_and_legacy_omission() -> None:
    from krok_helper.subtitle_render.domain.paint import (
        PaintFill,
        effective_gradient_angle_deg,
    )
    from krok_helper.subtitle_render.serialization.paint import (
        paint_fill_from_dict,
        paint_fill_to_dict,
    )

    legacy = PaintFill(mode="gradient_vertical", start_color="#FF0000", end_color="#0000FF")
    legacy_payload = paint_fill_to_dict(legacy)
    assert "gradient_angle_deg" not in legacy_payload
    assert "gradient_space" not in legacy_payload
    assert "radial_center_x_pct" not in legacy_payload
    assert effective_gradient_angle_deg(legacy) == 90.0
    assert effective_gradient_angle_deg(PaintFill(mode="gradient_horizontal")) == 0.0

    angled = PaintFill(
        mode="gradient_vertical",
        start_color="#FF0000",
        end_color="#00FF00",
        gradient_angle_deg=135.0,
        gradient_space="glyph",
    )
    restored_angled = paint_fill_from_dict(paint_fill_to_dict(angled), fallback="#FFFFFF")
    assert restored_angled.gradient_angle_deg == 135.0
    assert restored_angled.gradient_space == "glyph"
    assert effective_gradient_angle_deg(restored_angled) == 135.0

    radial = PaintFill(
        mode="gradient_radial",
        start_color="#FFFFFF",
        end_color="#112233",
        gradient_space="canvas",
        radial_center_x_pct=30.0,
        radial_center_y_pct=70.0,
        radial_radius_x_pct=120.0,
        radial_radius_y_pct=45.0,
    )
    restored_radial = paint_fill_from_dict(paint_fill_to_dict(radial), fallback="#FFFFFF")
    assert restored_radial.mode == "gradient_radial"
    assert restored_radial.gradient_space == "canvas"
    assert restored_radial.radial_center_x_pct == 30.0
    assert restored_radial.radial_center_y_pct == 70.0
    assert restored_radial.radial_radius_x_pct == 120.0
    assert restored_radial.radial_radius_y_pct == 45.0


def test_property_panel_gradient_angle_radial_space_and_font_preview(qapp) -> None:
    from dataclasses import replace
    from krok_helper.subtitle_render.domain.paint import PaintFill
    from krok_helper.subtitle_render.frontend.properties.controls.font_preview import (
        _FontSampleCanvas,
    )
    from krok_helper.subtitle_render.frontend.properties.property_panel import (
        PropertyPanel,
    )

    panel = PropertyPanel()
    panel._fill_mode_pill._buttons["gradient_linear"].click()
    assert panel._fill_mode_pill.current() == "gradient_linear"
    assert panel._gradient_linear_options_row.isHidden() is False
    assert panel._gradient_radial_options_grid.isHidden() is True
    assert [
        panel._gradient_space_combo.itemData(i)
        for i in range(panel._gradient_space_combo.count())
    ] == ["line", "glyph"]
    assert panel._gradient_angle_h_btn.width() >= 30
    assert panel._gradient_angle_v_btn.width() >= 30

    panel._gradient_angle_spin.setValue(45.0)
    panel._gradient_space_combo.setCurrentIndex(
        panel._gradient_space_combo.findData("glyph")
    )
    fill = panel._current_paint_fill()
    assert fill.gradient_angle_deg == 45.0
    assert fill.gradient_space == "glyph"

    panel._gradient_angle_h_btn.click()
    fill = panel._current_paint_fill()
    assert fill.mode == "gradient_horizontal"
    assert fill.gradient_angle_deg is None
    assert panel._fill_mode_pill.current() == "gradient_linear"

    panel._fill_mode_pill._buttons["gradient_radial"].click()
    assert panel._gradient_linear_options_row.isHidden() is True
    assert panel._gradient_radial_options_grid.isHidden() is False
    assert not hasattr(panel, "_radial_center_x_spin")
    assert [
        panel._radial_space_combo.itemData(i)
        for i in range(panel._radial_space_combo.count())
    ] == ["line", "glyph"]
    panel._radial_pad.geometryChanged.emit(25.0, 35.0, 80.0, 110.0)
    panel._radial_space_combo.setCurrentIndex(
        panel._radial_space_combo.findData("glyph")
    )
    fill = panel._current_paint_fill()
    assert fill.mode == "gradient_radial"
    assert fill.radial_center_x_pct == 25.0
    assert fill.radial_center_y_pct == 35.0
    assert fill.radial_radius_x_pct == 80.0
    assert fill.radial_radius_y_pct == 110.0
    assert fill.gradient_space == "glyph"

    # Out-of-box center (-25%, 125%) and large radius (180%) auto-fit all handles inside the pad.
    panel._radial_pad.resize(260, 104)
    panel._radial_pad.set_geometry_values(-25.0, 125.0, 180.0, 150.0)
    pad_rect = panel._radial_pad._pad_rect()
    for pt in (
        panel._radial_pad._center_point(),
        panel._radial_pad._rx_handle_point(),
        panel._radial_pad._ry_handle_point(),
    ):
        assert pad_rect.contains(pt)

    panel._radial_reset_btn.click()
    fill = panel._current_paint_fill()
    assert (
        fill.radial_center_x_pct,
        fill.radial_center_y_pct,
        fill.radial_radius_x_pct,
        fill.radial_radius_y_pct,
    ) == (50.0, 50.0, 50.0, 50.0)

    panel._fill_mode_pill._buttons["split_vertical"].click()
    assert [
        panel._split_space_combo.itemData(i)
        for i in range(panel._split_space_combo.count())
    ] == ["line", "glyph"]
    panel._split_angle_spin.setValue(30.0)
    panel._split_space_combo.setCurrentIndex(
        panel._split_space_combo.findData("glyph")
    )
    fill = panel._current_paint_fill()
    assert fill.mode == "split_vertical"
    assert fill.gradient_angle_deg == 30.0
    assert fill.gradient_space == "glyph"

    # Font preview renders distinct non-empty images for radial, angled, and line-vs-glyph fills.
    base_style = panel.subtitle_style
    imgs = []
    for sample_fill in (
        PaintFill(mode="gradient_vertical", start_color="#FF0000", end_color="#0000FF", gradient_space="line"),
        PaintFill(mode="gradient_vertical", start_color="#FF0000", end_color="#0000FF", gradient_space="glyph"),
        PaintFill(mode="gradient_vertical", start_color="#FF0000", end_color="#0000FF", gradient_angle_deg=45.0),
        PaintFill(mode="gradient_radial", start_color="#FFFFFF", end_color="#0000FF"),
        PaintFill(mode="split_vertical", split_stops=[(0, "#FF0000"), (50, "#00FF00")], gradient_angle_deg=45.0),
    ):
        colors = replace(
            base_style.karaoke_colors,
            after=replace(base_style.karaoke_colors.after, text=sample_fill),
        )
        img = _FontSampleCanvas._render_sample_image(
            replace(base_style, karaoke_colors=colors, ruby_colors_follow_main=True),
            "japanese",
        )
        assert not img.isNull()
        imgs.append(img)
    assert imgs[0] != imgs[1]
    assert imgs[0] != imgs[2]
    assert imgs[0] != imgs[3]
    assert imgs[3] != imgs[4]
    panel.close()
    panel.deleteLater()
