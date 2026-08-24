"""Input controls shared by subtitle property pages."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Optional

from PyQt6.QtCore import QSize, Qt, pyqtSignal as Signal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import QSizePolicy, QStackedWidget, QWidget
from qfluentwidgets import (
    ComboBox as FluentComboBox,
    PlainTextEdit as FluentPlainTextEdit,
)

from krok_helper.subtitle_render.n3_font_catalog import (
    canonicalize_n3_font_family,
    n3_font_families,
)


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


class WheelFocusedComboBox(FluentComboBox):
    """Avoid accidental option changes while scrolling a property page."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def addItem(self, text: str, userData=None) -> None:  # noqa: N802 - Qt API
        super().addItem(text, userData=userData)

    def wheelEvent(self, event):  # noqa: N802 - Qt API
        if not self.hasFocus():
            event.ignore()
            return
        super().wheelEvent(event)


class WheelFocusedFontComboBox(WheelFocusedComboBox):
    """Fluent font picker preserving QFontComboBox's small public contract."""

    currentFontChanged = Signal(QFont)

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        *,
        font_families_provider: Callable[[], Iterable[str]] = n3_font_families,
        canonicalize_family: Callable[[str], Optional[str]] = (
            canonicalize_n3_font_family
        ),
    ) -> None:
        super().__init__(parent)
        self._canonicalize_family = canonicalize_family
        self._inheritance_label: Optional[str] = None
        self.addItems(tuple(font_families_provider()))
        self.currentIndexChanged.connect(
            lambda _index: self.currentFontChanged.emit(self.currentFont())
        )

    def enable_inheritance(self, label: str) -> None:
        """Add an explicit N3-style zero slot before installed families."""
        if self._inheritance_label is not None:
            return
        self._inheritance_label = str(label)
        self.insertItem(0, self._inheritance_label, 0)

    def is_inherited(self) -> bool:
        return self._inheritance_label is not None and self.currentIndex() == 0

    def setInherited(self) -> None:  # noqa: N802 - Qt-style helper
        if self._inheritance_label is not None:
            self.setCurrentIndex(0)

    def currentFont(self) -> QFont:  # noqa: N802 - QFontComboBox compatibility
        return QFont(self.currentText())

    def setCurrentFont(self, font: QFont) -> None:  # noqa: N802
        family = self._canonicalize_family(font.family())
        index = self.findText(family) if family is not None else -1
        if index < 0:
            if self._inheritance_label is not None:
                self.setInherited()
            return
        if index == self.currentIndex():
            self.currentFontChanged.emit(self.currentFont())
            return
        self.setCurrentIndex(index)
