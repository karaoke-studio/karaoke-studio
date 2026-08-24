"""Input controls shared by subtitle property pages."""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import QSize, Qt, pyqtSignal as Signal
from PyQt6.QtWidgets import QSizePolicy, QStackedWidget, QWidget
from qfluentwidgets import PlainTextEdit as FluentPlainTextEdit


class GrowingPlainTextEdit(FluentPlainTextEdit):
    """Multiline editor whose height follows its paragraph count."""

    editingFinished = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setLineWrapMode(FluentPlainTextEdit.LineWrapMode.WidgetWidth)
        self.textChanged.connect(self._adjust_height)
        self._adjust_height()

    def _adjust_height(self) -> None:
        blocks = max(1, self.document().blockCount())
        line_height = self.fontMetrics().lineSpacing()
        frame = int(self.frameWidth()) * 2
        margins = self.contentsMargins()
        doc_margin = int(self.document().documentMargin()) * 2
        height = (
            blocks * line_height
            + frame
            + margins.top()
            + margins.bottom()
            + doc_margin
            + 4
        )
        self.setFixedHeight(max(32, height))

    def wheelEvent(self, event):  # noqa: N802 - Qt API
        if not self.hasFocus():
            event.ignore()
            return
        super().wheelEvent(event)

    def focusOutEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().focusOutEvent(event)
        self.editingFinished.emit()


class DynamicStackedWidget(QStackedWidget):
    """Report the current page height instead of the tallest page height."""

    def sizeHint(self) -> QSize:  # noqa: N802
        widget = self.currentWidget()
        return widget.sizeHint() if widget is not None else super().sizeHint()

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        widget = self.currentWidget()
        return (
            widget.minimumSizeHint()
            if widget is not None
            else super().minimumSizeHint()
        )
