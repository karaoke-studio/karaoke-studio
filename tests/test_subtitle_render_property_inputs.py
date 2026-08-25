"""Focused contracts for shared subtitle property input controls."""

from __future__ import annotations

from PyQt6.QtCore import QSize
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import QWidget

from krok_helper.subtitle_render.frontend.properties.controls.inputs import (
    DynamicStackedWidget,
    GrowingPlainTextEdit,
    NoWheelSpinBox,
    TimecodeEdit,
    WheelFocusedComboBox,
    WheelFocusedDoubleSpinBox,
    WheelFocusedFontComboBox,
    WheelFocusedSpinBox,
)


def test_growing_property_text_edit_increases_height_for_new_paragraphs(qapp) -> None:
    editor = GrowingPlainTextEdit()
    initial_height = editor.height()

    editor.setPlainText("第一行\n第二行\n第三行")

    assert editor.document().blockCount() == 3
    assert editor.height() > initial_height


def test_dynamic_property_stack_reports_only_current_page_hints(qapp) -> None:
    class HintPage(QWidget):
        def __init__(self, hint: QSize, minimum: QSize) -> None:
            super().__init__()
            self._hint = hint
            self._minimum = minimum

        def sizeHint(self) -> QSize:  # noqa: N802
            return self._hint

        def minimumSizeHint(self) -> QSize:  # noqa: N802
            return self._minimum

    stack = DynamicStackedWidget()
    first = HintPage(QSize(100, 40), QSize(80, 30))
    second = HintPage(QSize(300, 200), QSize(200, 120))
    stack.addWidget(first)
    stack.addWidget(second)

    stack.setCurrentWidget(first)
    assert stack.sizeHint() == QSize(100, 40)
    assert stack.minimumSizeHint() == QSize(80, 30)

    stack.setCurrentWidget(second)
    assert stack.sizeHint() == QSize(300, 200)
    assert stack.minimumSizeHint() == QSize(200, 120)


def test_wheel_focused_property_combo_preserves_positional_user_data(qapp) -> None:
    combo = WheelFocusedComboBox()

    combo.addItem("布局", 3)

    assert combo.itemText(0) == "布局"
    assert combo.itemData(0) == 3


def test_no_wheel_property_spin_ignores_page_scroll_input(qapp) -> None:
    class WheelEvent:
        ignored = False

        def ignore(self) -> None:
            self.ignored = True

    event = WheelEvent()
    spin = NoWheelSpinBox()

    spin.wheelEvent(event)

    assert event.ignored is True


def test_property_timecode_input_preserves_value_format_clamp_and_step(qapp) -> None:
    edit = TimecodeEdit(0, 10_000)
    changes: list[int] = []
    edit.valueChanged.connect(changes.append)

    assert edit.submit_text("3.5") is True
    assert edit.value() == 3_500
    assert edit.text() == "0:03.500"

    edit.stepBy(1)
    edit.stepBy(-1, fine=True)
    assert edit.value() == 4_490

    assert edit.submit_text("15") is True
    assert edit.value() == 10_000
    assert edit.text() == "0:10.000"
    assert changes == [3_500, 4_500, 4_490, 10_000]


def test_property_timecode_input_restores_invalid_partial_text(qapp) -> None:
    edit = TimecodeEdit(0, 10_000)
    edit.setValue(2_500)

    assert edit.submit_text("1:") is False
    assert edit.value() == 2_500
    assert edit.text() == "0:02.500"


def test_property_spin_inputs_keep_units_out_of_selection(qapp) -> None:
    spin = WheelFocusedSpinBox()
    spin.setRange(0, 200)
    spin.setSuffix(" px")
    spin.setValue(75)
    editor = spin.lineEdit()

    editor.setSelection(0, len(editor.text()))
    qapp.processEvents()

    assert editor.selectedText() == "75"


def test_property_double_spin_input_preserves_typed_text_on_commit(qapp) -> None:
    spin = WheelFocusedDoubleSpinBox(commit_delay_ms=1)
    spin.setRange(0.0, 100.0)
    spin.setDecimals(3)
    spin.show()
    editor = spin.lineEdit()
    editor.setFocus()
    editor.selectAll()
    editor.setText("12.5")
    editor.setCursorPosition(len(editor.text()))
    spin._keyboard_commit_pending = True

    spin._commit_keyboard_edit()

    assert spin.value() == 12.5
    assert editor.text() == "12.5"
    assert editor.cursorPosition() == len("12.5")


def test_property_font_combo_uses_injected_catalog_and_canonicalizer(qapp) -> None:
    combo = WheelFocusedFontComboBox(
        font_families_provider=lambda: ("Canonical Font",),
        canonicalize_family=lambda name: (
            "Canonical Font" if name == "Saved Alias" else None
        ),
    )
    combo.enable_inheritance("跟随主文字（0）")

    combo.setCurrentFont(QFont("Saved Alias"))
    assert combo.currentText() == "Canonical Font"

    combo.setCurrentFont(QFont("Missing Font"))
    assert combo.is_inherited() is True
