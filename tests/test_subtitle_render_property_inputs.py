"""Focused contracts for shared subtitle property input controls."""

from __future__ import annotations

from PyQt6.QtCore import QSize
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import QWidget

from krok_helper.subtitle_render.frontend.property_inputs import (
    DynamicStackedWidget,
    GrowingPlainTextEdit,
    WheelFocusedComboBox,
    WheelFocusedFontComboBox,
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
