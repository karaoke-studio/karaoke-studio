"""Focused contracts for responsive property-page layout primitives."""

from __future__ import annotations

from PyQt6.QtCore import QSize, Qt
from PyQt6.QtWidgets import QLabel, QLineEdit, QSizePolicy, QWidget

from krok_helper.subtitle_render.frontend.property_layout import (
    ResponsiveFieldGrid,
    ResponsivePropertyPair,
    ResponsiveRoleHeader,
    compact_property_control,
    inline_property_section,
    plain_property_card,
    property_field,
    property_section,
    property_section_pair,
)


def test_property_field_preserves_label_control_and_style_hooks(qapp) -> None:
    control = QLineEdit()

    field = property_field("字体", control)

    assert field.objectName() == "SubtitlePropertyField"
    assert field.findChild(QLabel).text() == "字体"
    assert control.parentWidget() is field


def test_compact_property_control_preserves_size_focus_and_width_policy(qapp) -> None:
    control = QLineEdit()

    compact_property_control(control)

    assert control.height() == 32
    assert control.minimumWidth() == 0
    assert control.focusPolicy() == Qt.FocusPolicy.StrongFocus
    assert control.sizePolicy().horizontalPolicy() == QSizePolicy.Policy.Ignored
    assert control.sizePolicy().verticalPolicy() == QSizePolicy.Policy.Fixed


def test_property_card_builders_preserve_structure_and_style_hooks(qapp) -> None:
    section, section_layout = property_section("标题", switch=True)
    plain, plain_layout = plain_property_card()
    inline, inline_layout = inline_property_section("字体")

    assert section.objectName() == "SubtitlePropertySection"
    assert section.header.text() == "标题"
    assert section.header_switch is not None
    assert section_layout is section.content_layout
    assert plain.objectName() == "SubtitlePropertyCard"
    assert plain_layout.contentsMargins().left() == 12
    assert inline_layout.contentsMargins().left() == 0
    assert inline.findChild(QLabel, "SubtitlePropertySubheading").text() == "字体"


def test_responsive_field_grid_reflows_between_one_and_three_columns(qapp) -> None:
    grid = ResponsiveFieldGrid(min_column_width=100, max_columns=3)
    for index in range(3):
        grid.add_widget(QWidget())

    grid.resize(350, 100)
    grid._relayout()
    assert grid._columns == 3

    grid.resize(90, 100)
    grid._relayout()
    assert grid._columns == 1


def test_responsive_property_pair_uses_child_hints_as_breakpoint(qapp) -> None:
    pair = ResponsivePropertyPair(min_side_width=100)
    pair.set_widgets(QWidget(), None, QWidget())

    pair.resize(500, 100)
    pair._sync_direction()
    assert pair.is_stacked() is False

    pair.resize(100, 100)
    pair._sync_direction()
    assert pair.is_stacked() is True


def test_property_section_pair_preserves_standard_breakpoint_and_spacing(qapp) -> None:
    first = QWidget()
    second = QWidget()

    pair = property_section_pair(first, second)

    assert pair._first is first
    assert pair._divider is None
    assert pair._second is second
    assert pair._min_side_width == 270
    assert pair._layout.spacing() == 10
    assert first.sizePolicy().horizontalPolicy() == QSizePolicy.Policy.Expanding
    assert first.sizePolicy().verticalPolicy() == QSizePolicy.Policy.Preferred
    assert second.sizePolicy().horizontalPolicy() == QSizePolicy.Policy.Expanding
    assert second.sizePolicy().verticalPolicy() == QSizePolicy.Policy.Preferred


def test_responsive_role_header_stacks_when_navigation_and_preview_do_not_fit(qapp) -> None:
    class HintWidget(QWidget):
        def __init__(self, width: int) -> None:
            super().__init__()
            self._hint = QSize(width, 30)

        def sizeHint(self) -> QSize:  # noqa: N802
            return self._hint

    header = ResponsiveRoleHeader()
    navigation = HintWidget(200)
    preview = HintWidget(120)
    header.set_widgets(navigation, preview)

    header.resize(500, 100)
    header._sync_direction()
    assert header.is_stacked() is False

    header.resize(200, 100)
    header._sync_direction()
    assert header.is_stacked() is True
