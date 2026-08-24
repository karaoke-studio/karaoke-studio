"""Focused construction contracts for the subtitle background-property page."""

from __future__ import annotations

from krok_helper.subtitle_render.frontend.property_background_page import (
    BackgroundPropertyPageBuilder,
)


class _Host:
    def __init__(self) -> None:
        self.screen_changes = 0

    def _on_panel_screen_size_changed(self, *_args) -> None:
        self.screen_changes += 1


def test_background_screen_size_builder_preserves_control_contracts(qapp) -> None:
    host = _Host()
    section = BackgroundPropertyPageBuilder(host).make_screen_size_section()

    assert section.header.text() == "画面尺寸"
    assert host._screen_size_width_spin.minimum() == 160
    assert host._screen_size_width_spin.maximum() == 7680
    assert host._screen_size_width_spin.value() == 1920
    assert host._screen_size_width_spin.singleStep() == 2
    assert not host._screen_size_width_spin.keyboardTracking()
    assert host._screen_size_height_spin.minimum() == 90
    assert host._screen_size_height_spin.maximum() == 4320
    assert host._screen_size_height_spin.value() == 1080
    assert host._screen_size_fps_combo.count() == 2
    assert host._screen_size_fps_combo.itemData(0) == 60
    assert host._screen_size_fps_combo.itemData(1) == 120


def test_background_screen_size_builder_routes_each_change_to_host(qapp) -> None:
    host = _Host()
    BackgroundPropertyPageBuilder(host).make_screen_size_section()

    host._screen_size_width_spin.setValue(1280)
    host._screen_size_height_spin.setValue(720)
    host._screen_size_fps_combo.setCurrentIndex(1)

    assert host.screen_changes == 3
