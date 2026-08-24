"""Focused construction contracts for the subtitle effects-property page."""

from __future__ import annotations

from krok_helper.subtitle_render.frontend.property_effects_page import (
    EffectsPropertyPageBuilder,
)


class _Host:
    def __init__(self) -> None:
        self.updates: list[dict[str, object]] = []

    def _update_style(self, **changes) -> None:
        self.updates.append(changes)


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
