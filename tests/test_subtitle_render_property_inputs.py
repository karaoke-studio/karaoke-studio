"""Focused contracts for shared subtitle property input controls."""

from __future__ import annotations

from PyQt6.QtCore import QSize
from PyQt6.QtWidgets import QWidget

from krok_helper.subtitle_render.frontend.property_inputs import (
    DynamicStackedWidget,
    GrowingPlainTextEdit,
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
