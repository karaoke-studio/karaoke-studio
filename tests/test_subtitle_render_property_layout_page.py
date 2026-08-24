"""Focused construction contracts for the subtitle layout-property page."""

from __future__ import annotations

from krok_helper.subtitle_render.frontend.property_layout_page import (
    LayoutPropertyPageBuilder,
)


class _Host:
    def __init__(self) -> None:
        self.updates: list[dict[str, object]] = []
        self.layout_updates: list[dict[str, object]] = []

    def _update_style(self, **changes) -> None:
        self.updates.append(changes)

    def _update_layout_field(self, **changes) -> None:
        self.layout_updates.append(changes)


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


def test_layout_vertical_builder_preserves_ranges_and_compact_controls(qapp) -> None:
    host = _Host()
    section = LayoutPropertyPageBuilder(host).make_vertical_section()

    assert section.header.text() == "垂直与方向"
    assert host._line_gap_spin.minimum() == -16_384
    assert host._line_gap_spin.maximum() == 16_384
    assert host._line_gap_spin.width() == 120
    assert host._line_gap_spin.sizePolicy().horizontalPolicy().name == "Fixed"
    assert host._vertical_check.text() == "竖排"
    assert host._rtl_check.text() == "从右到左"
    assert host._allow_inter_page_line_overlap_check.text() == "启用行间重叠"
    assert "250 ms" in host._allow_inter_page_line_overlap_check.toolTip()


def test_layout_vertical_builder_routes_layout_and_style_fields(qapp) -> None:
    host = _Host()
    LayoutPropertyPageBuilder(host).make_vertical_section()

    host._line_gap_spin.setValue(-12)
    host._vertical_check.setChecked(True)
    host._rtl_check.setChecked(True)
    host._allow_inter_page_line_overlap_check.setChecked(True)

    assert host.layout_updates == [{"line_gap_px": -12}]
    assert host.updates == [
        {"vertical": True},
        {"right_to_left": True},
        {"allow_inter_page_line_overlap": True},
    ]
