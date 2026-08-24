"""Focused construction contracts for the subtitle effects-property page."""

from __future__ import annotations

from types import SimpleNamespace

from PyQt6.QtWidgets import QPushButton

from krok_helper.subtitle_render.frontend.property_effects_page import (
    EffectsPropertyPageBuilder,
)


class _Host:
    def __init__(self) -> None:
        self.updates: list[dict[str, object]] = []
        self.visibility_syncs = 0
        self._style = SimpleNamespace(
            volume_fill_color="#101010",
            volume_stroke_color="#202020",
            volume_overlay_fill_color="#303030",
            volume_overlay_stroke_color="#404040",
            lit_fill_color="#505050",
            lit_stroke_color="#606060",
        )

    def _update_style(self, **changes) -> None:
        self.updates.append(changes)

    def _color_button(self, field: str, color: str):
        button = QPushButton(color)
        button.setObjectName(field)
        return button

    def _sync_lit_style_visibility(self) -> None:
        self.visibility_syncs += 1


def test_effects_animation_builder_preserves_options_and_layout(qapp) -> None:
    host = _Host()
    section = EffectsPropertyPageBuilder(host).make_animation_section()

    assert section.header.text() == "入退场动画"
    assert host._animation_grid._max_columns == 2
    assert host._entry_anim_combo.count() == 8
    assert host._entry_anim_combo.itemData(5) == "char_drip"
    assert host._exit_anim_combo.count() == 8
    assert host._exit_anim_combo.itemData(2) == "slide_out"
    assert host._entry_lead_spin.maximum() == 3000
    assert host._exit_fade_spin.maximum() == 3000
    assert host._entry_lead_spin.toolTip() == "入场动画时长"
    assert host._exit_fade_spin.toolTip() == "退场动画时长"
    assert host._karaoke_anim_combo.count() == 2


def test_effects_animation_builder_routes_controls_to_style_fields(qapp) -> None:
    host = _Host()
    EffectsPropertyPageBuilder(host).make_animation_section()

    host._entry_anim_combo.setCurrentIndex(1)
    host._entry_lead_spin.setValue(250)
    host._exit_anim_combo.setCurrentIndex(2)
    host._exit_fade_spin.setValue(300)
    host._karaoke_anim_combo.setCurrentIndex(1)

    assert host.updates == [
        {"entry_anim": "fade"},
        {"entry_lead_ms": 250},
        {"exit_anim": "slide_out"},
        {"exit_fade_ms": 300},
        {"karaoke_anim": "utopia"},
    ]


def test_effects_lit_builder_preserves_groups_ranges_and_initial_state(qapp) -> None:
    host = _Host()
    section = EffectsPropertyPageBuilder(host).make_lit_section()

    assert section.header.text() == "指示灯"
    assert section.header_switch is host._lit_enabled_switch
    assert not section.is_expanded()
    assert list(host._lit_group_grids) == [
        "通用",
        "音量柱 · 布局",
        "音量柱 · 动画",
        "音量柱 · 颜色",
        "形状灯 · 布局",
        "形状灯 · 外观",
        "形状灯 · 转场",
    ]
    assert len(host._lit_volume_groups) == 3
    assert len(host._lit_shape_groups) == 3
    assert host._lit_style_combo.count() == 4
    assert host._volume_column_count_spin.maximum() == 16
    assert host._lit_transition_angle_spin.minimum() == -360
    assert host._lit_transition_distance_spin.maximum() == 800
    assert host._volume_fill_btn.objectName() == "volume_fill_color"
    assert host._lit_stroke_btn.objectName() == "lit_stroke_color"
    assert host.visibility_syncs == 1


def test_effects_lit_builder_routes_transformed_values(qapp) -> None:
    host = _Host()
    EffectsPropertyPageBuilder(host).make_lit_section()

    host._lit_enabled_switch.setChecked(True)
    host._volume_ratio_spin.setValue(3)
    host._volume_flash_duration_spin.setValue(25)
    host._lit_transition_mode_combo.setCurrentIndex(2)
    host._lit_shadow_check.setChecked(True)

    assert host.updates == [
        {"lit_enabled": True},
        {"volume_ratio": 3.0},
        {"volume_flash_duration_ratio": 0.25},
        {"lit_transition_mode": "slide"},
        {"lit_shadow": True},
    ]
