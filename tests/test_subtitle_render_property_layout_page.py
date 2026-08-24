"""Focused construction contracts for the subtitle layout-property page."""

from __future__ import annotations

from krok_helper.subtitle_render.frontend.property_layout_page import (
    LayoutPropertyPageBuilder,
)


class _Host:
    def __init__(self) -> None:
        self.updates: list[dict[str, object]] = []

    def _update_style(self, **changes) -> None:
        self.updates.append(changes)


def test_layout_viewport_builder_preserves_options_and_ranges(qapp) -> None:
    host = _Host()
    section = LayoutPropertyPageBuilder(host).make_viewport_section()

    assert section.header.text() == "视图"
    assert host._viewport_align_combo.count() == 9
    assert host._viewport_align_combo.itemData(4) == "center"
    assert host._viewport_x_spin.minimum() == -4000
    assert host._viewport_y_spin.maximum() == 4000
    assert host._viewport_scale_spin.minimum() == 10
    assert host._viewport_scale_spin.maximum() == 400
    assert host._viewport_rotation_spin.minimum() == -180
    assert host._viewport_rotation_spin.maximum() == 180


def test_layout_viewport_builder_routes_controls_to_style_fields(qapp) -> None:
    host = _Host()
    LayoutPropertyPageBuilder(host).make_viewport_section()

    host._viewport_align_combo.setCurrentIndex(4)
    host._viewport_x_spin.setValue(20)
    host._viewport_scale_spin.setValue(125)
    host._viewport_rotation_spin.setValue(-15)

    assert host.updates == [
        {"viewport_align": "center"},
        {"viewport_offset_x": 20},
        {"viewport_scale_pct": 125},
        {"viewport_rotation_deg": -15},
    ]
