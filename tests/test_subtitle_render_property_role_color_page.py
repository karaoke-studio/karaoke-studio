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
    assert host._color_layer_combo.count() == 4
    assert host._color_layer_pill.current() == "text"
    assert host._fill_mode_combo.count() == 5
    assert host._fill_mode_pill.current() == "solid"
    assert host._fill_editor_stack.count() == 4
    assert host._decoration_type_combo.count() == 3
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
