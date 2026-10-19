"""Focused contracts for responsive property-page layout primitives."""

from __future__ import annotations

from PyQt6.QtCore import QSize, Qt
from PyQt6.QtWidgets import QLabel, QLineEdit, QSizePolicy, QWidget

from krok_helper.subtitle_render.frontend.properties.controls.layout import (
    ResponsiveControlPair,
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


def test_responsive_control_pair_breaks_on_minimum_not_preferred_width(qapp) -> None:
    class HintWidget(QWidget):
        def __init__(self, minimum: int, preferred: int) -> None:
            super().__init__()
            self._minimum = minimum
            self._preferred = preferred

        def minimumSizeHint(self) -> QSize:  # noqa: N802
            return QSize(self._minimum, 30)

        def sizeHint(self) -> QSize:  # noqa: N802
            return QSize(self._preferred, 30)

    pair = ResponsiveControlPair()
    pair.set_widgets(HintWidget(170, 330), HintWidget(170, 330))

    # 输入类控件的首选宽度（330）远大于实际可用宽度，断点必须只看两侧
    # 最小宽度：并排 170+170+4 = 344 够用就不竖排。
    assert pair._layout.spacing() == 4
    assert pair.minimumSizeHint().width() == 170

    pair.resize(400, 100)
    pair._sync_direction()
    assert pair.is_stacked() is False

    pair.resize(300, 100)
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


def test_responsive_role_header_can_shrink_below_side_by_side_sum(qapp) -> None:
    class HintWidget(QWidget):
        def __init__(self, width: int) -> None:
            super().__init__()
            self._hint = QSize(width, 30)

        def minimumSizeHint(self) -> QSize:  # noqa: N802
            return self._hint

        def sizeHint(self) -> QSize:  # noqa: N802
            return self._hint

    header = ResponsiveRoleHeader()
    navigation = HintWidget(418)
    preview = HintWidget(120)
    header.set_widgets(navigation, preview)

    # 父布局先看到的是“最宽子项”，而不是 418+120+间距；否则它永远
    # 缩不到断点以下，预览只能被裁掉。
    assert header.minimumSizeHint().width() == 418

    header.resize(450, 200)
    header._sync_direction()
    header.layout().activate()
    assert header.is_stacked() is True
    assert navigation.width() <= 450
    assert preview.width() <= 450


def test_responsive_property_pair_does_not_unstack_on_small_width_jitter(qapp) -> None:
    class HintWidget(QWidget):
        def minimumSizeHint(self) -> QSize:  # noqa: N802
            return QSize(300, 30)

        def sizeHint(self) -> QSize:  # noqa: N802
            return QSize(300, 30)

    pair = ResponsivePropertyPair()
    pair.set_widgets(HintWidget(), None, HintWidget())
    required = pair.horizontal_width_hint()

    pair.resize(required, 100)
    pair._sync_direction()
    assert pair.is_stacked() is False

    # A scrollbar / hover jitter of a few pixels must not make a stacked pair
    # jump back to side-by-side; it must clear the 24px unstack band first.
    pair.resize(required - 12, 100)
    pair._sync_direction()
    assert pair.is_stacked() is True

    pair.resize(required + 4, 100)
    pair._sync_direction()
    assert pair.is_stacked() is True

    pair.resize(required + 12, 100)
    pair._sync_direction()
    assert pair.is_stacked() is False


def test_responsive_role_header_does_not_unstack_on_small_width_jitter(qapp) -> None:
    class HintWidget(QWidget):
        def __init__(self, width: int) -> None:
            super().__init__()
            self._hint = QSize(width, 30)

        def minimumSizeHint(self) -> QSize:  # noqa: N802
            return self._hint

        def sizeHint(self) -> QSize:  # noqa: N802
            return self._hint

    header = ResponsiveRoleHeader()
    header.set_widgets(HintWidget(200), HintWidget(120))
    required = 200 + 120 + header.layout().spacing()

    header.resize(required, 100)
    header._sync_direction()
    assert header.is_stacked() is False

    header.resize(required - 12, 100)
    header._sync_direction()
    assert header.is_stacked() is True

    header.resize(required + 12, 100)
    header._sync_direction()
    assert header.is_stacked() is True

    header.resize(required + 30, 100)
    header._sync_direction()
    assert header.is_stacked() is False


def test_responsive_property_pair_allocates_side_widths_by_preferred(qapp) -> None:
    class HintWidget(QWidget):
        def __init__(self, minimum: int, preferred: int) -> None:
            super().__init__()
            self._minimum = minimum
            self._preferred = preferred

        def minimumSizeHint(self) -> QSize:  # noqa: N802
            return QSize(self._minimum, 30)

        def sizeHint(self) -> QSize:  # noqa: N802
            return QSize(self._preferred, 30)

    pair = ResponsivePropertyPair()
    first = HintWidget(279, 324)
    second = HintWidget(253, 253)
    pair.set_widgets(first, None, second)
    pair.resize(600, 100)
    pair._sync_direction()
    pair.layout().activate()

    assert pair.is_stacked() is False
    # 等分 stretch 会给 first 约 294px，低于它的 preferred 324，导致右侧
    # 被裁；按 preferred 分配后两张卡都拿到自己的完整宽度。
    assert first.width() >= 324
    assert second.width() >= 253
