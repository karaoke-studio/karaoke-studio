"""Focused construction contracts for the subtitle layout-property page."""

from __future__ import annotations

from PyQt6.QtCore import pyqtSignal as Signal
from PyQt6.QtWidgets import QVBoxLayout, QWidget

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

    def _make_layout_navigation(self, parent):
        return QWidget(parent)

    def _make_layout_assignment_actions(self, parent):
        return QWidget(parent)

    def _on_line_position_changed(self, _value="") -> None:
        pass

    def _on_horizontal_margin_changed(self, value: int) -> None:
        self.layout_updates.append({"horizontal_margin_px": value})

    def _make_smart_horizontal_field(self, parent):
        return QWidget(parent)

    def _make_character_layout_group(self, parent):
        return QWidget(parent)

    def _make_line_alignments_box(self, parent):
        return QWidget(parent)


class _GlyphSegment(QWidget):
    valueChanged = Signal(str)

    def __init__(self, options, parent=None) -> None:
        super().__init__(parent)
        self.options = tuple(options)
        self.value = ""

    def setValue(self, value: str) -> None:
        self.value = value


class _SchematicBoard(QWidget):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(args[4])
        self.parts = args
        self.slots = kwargs


def _plain_card():
    card = QWidget()
    return card, QVBoxLayout(card)


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


def test_layout_ruby_builder_preserves_ranges_options_and_inline_form(qapp) -> None:
    host = _Host()
    builder = LayoutPropertyPageBuilder(host)
    section = builder.make_ruby_section()

    assert section.header.text() == "注音"
    assert host._ruby_gap_spin.minimum() == -16_384
    assert host._ruby_interval_spin.maximum() == 16_384
    assert host._ruby_alignment_combo.count() == 3
    assert host._ruby_alignment_combo.itemData(2) == "equal_space"
    assert "下限" in host._ruby_interval_spin.toolTip()

    inline = builder.make_ruby_section(inline=True)
    assert not hasattr(inline, "header")


def test_layout_ruby_builder_routes_controls_to_layout_fields(qapp) -> None:
    host = _Host()
    LayoutPropertyPageBuilder(host).make_ruby_section()

    host._ruby_gap_spin.setValue(12)
    host._ruby_interval_spin.setValue(-3)
    host._ruby_alignment_combo.setCurrentIndex(2)

    assert host.layout_updates == [
        {"ruby_gap_px": 12},
        {"ruby_interval_px": -3},
        {"ruby_alignment": "equal_space"},
    ]


def _row_builder(host: _Host) -> LayoutPropertyPageBuilder:
    return LayoutPropertyPageBuilder(
        host,
        plain_card_factory=_plain_card,
        glyph_segment_factory=_GlyphSegment,
        layout_schematic_factory=QWidget,
        schematic_board_factory=_SchematicBoard,
    )


def test_layout_row_builder_preserves_schematic_slots_and_ranges(qapp) -> None:
    host = _Host()
    section = _row_builder(host).make_row_structure_section()

    assert section is host._layout_section
    assert host._line_position_seg.value == "bottom"
    assert len(host._line_position_seg.options) == 3
    assert host._horizontal_margin_spin.minimum() == -16_384
    assert host._line_margin_spin.maximum() == 16_384
    assert host._layout_schematic.width() == round(150 * 16 / 9)
    assert host._vertical_margin_label.text() == "下余白"
    assert host._vertical_margin_field.sizePolicy().retainSizeWhenHidden()
    assert host._schematic_board.slots["bottom_right"] is host._allow_biting_check


def test_layout_row_builder_routes_margin_and_biting_changes(qapp) -> None:
    host = _Host()
    _row_builder(host).make_row_structure_section()

    host._horizontal_margin_spin.setValue(24)
    host._line_margin_spin.setValue(36)
    host._allow_biting_check.setChecked(True)

    assert host.layout_updates == [
        {"horizontal_margin_px": 24},
        {"line_y_margin_px": 36},
        {"allow_biting": True},
    ]
