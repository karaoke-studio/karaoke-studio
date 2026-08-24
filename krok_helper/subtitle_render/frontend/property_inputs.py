"""Input controls shared by subtitle property pages."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any, Optional

from PyQt6.QtCore import QRegularExpression, QSize, Qt, QTimer, pyqtSignal as Signal
from PyQt6.QtGui import QFont, QRegularExpressionValidator
from PyQt6.QtWidgets import QSizePolicy, QStackedWidget, QWidget
from qfluentwidgets import (
    ComboBox as FluentComboBox,
    LineEdit as FluentLineEdit,
    PlainTextEdit as FluentPlainTextEdit,
    SpinBox as FluentSpinBox,
)

from krok_helper.subtitle_render.n3_font_catalog import (
    canonicalize_n3_font_family,
    n3_font_families,
)
from krok_helper.subtitle_render.timecode import format_timecode_ms, parse_timecode_ms


_TIMECODE_PATTERN = QRegularExpression(
    r"\d{0,4}(:\d{1,2}){0,2}([.,]\d{0,3})?"
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


class NoWheelSpinBox(FluentSpinBox):
    """Ignore wheel input so scrolling a page cannot change the value."""

    def wheelEvent(self, event) -> None:  # noqa: N802 - Qt API
        event.ignore()


class TimecodeEdit(FluentLineEdit):
    """Single timecode input exposing an integer-millisecond value contract."""

    valueChanged = Signal(int)

    def __init__(
        self,
        minimum: int,
        maximum: int,
        parent: Optional[QWidget] = None,
        *,
        commit_delay_ms: int = 200,
    ) -> None:
        super().__init__(parent)
        if minimum < 0:
            raise ValueError("_TimecodeEdit 只支持非负范围")
        if maximum < minimum:
            raise ValueError("_TimecodeEdit 的 maximum 不能小于 minimum")
        self._minimum = int(minimum)
        self._maximum = int(maximum)
        self._value = self._minimum

        self.setValidator(QRegularExpressionValidator(_TIMECODE_PATTERN))
        self.setPlaceholderText("分:秒.毫秒")
        self.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self.setMinimumWidth(0)
        self.setFixedHeight(32)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.setToolTip(
            "时间格式「分:秒.毫秒」，如 1:23.450；直接输入数字按秒计"
            "（90 = 90 秒），也接受 时:分:秒。回车或点击别处后自动规范化。"
            "聚焦时滚轮 / 上下方向键 ±1 秒，按住 Ctrl ±10 毫秒。"
        )

        self._commit_timer = QTimer(self)
        self._commit_timer.setSingleShot(True)
        self._commit_timer.setInterval(int(commit_delay_ms))
        self._commit_timer.timeout.connect(self._commit_typing)
        self.textEdited.connect(lambda _text: self._commit_timer.start())
        self.editingFinished.connect(self._flush_edit)
        self._apply_text(self._value)

    def value(self) -> int:
        return self._value

    def setValue(self, value: int) -> None:  # noqa: N802 - Qt API
        clamped = self._clamp(value)
        changed = clamped != self._value
        self._value = clamped
        if changed or parse_timecode_ms(self.text()) != clamped:
            self._apply_text(clamped)
        if changed:
            self.valueChanged.emit(clamped)

    def minimum(self) -> int:
        return self._minimum

    def maximum(self) -> int:
        return self._maximum

    def submit_text(self, text: str) -> bool:
        self.setText(text)
        return self._flush_edit()

    def stepBy(self, steps: int, fine: bool = False) -> None:  # noqa: N802
        self._apply_value(self._value + steps * (10 if fine else 1000))

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt API
        if event.key() in (Qt.Key.Key_Up, Qt.Key.Key_Down):
            steps = 1 if event.key() == Qt.Key.Key_Up else -1
            self.stepBy(
                steps,
                fine=bool(
                    event.modifiers() & Qt.KeyboardModifier.ControlModifier
                ),
            )
            event.accept()
            return
        super().keyPressEvent(event)

    def wheelEvent(self, event) -> None:  # noqa: N802 - Qt API
        if not self.hasFocus():
            event.ignore()
            return
        delta = event.angleDelta().y()
        if event.inverted():
            delta = -delta
        if delta:
            steps = int(delta / 120) or (1 if delta > 0 else -1)
            self.stepBy(
                steps,
                fine=bool(
                    event.modifiers() & Qt.KeyboardModifier.ControlModifier
                ),
            )
            event.accept()
            return
        super().wheelEvent(event)

    def _clamp(self, value: Any) -> int:
        return int(max(self._minimum, min(self._maximum, int(value))))

    def _commit_typing(self) -> None:
        parsed = parse_timecode_ms(self.text())
        if parsed is None:
            return
        clamped = self._clamp(parsed)
        if clamped != self._value:
            self._value = clamped
            self.valueChanged.emit(clamped)

    def _flush_edit(self) -> bool:
        self._commit_timer.stop()
        parsed = parse_timecode_ms(self.text())
        if parsed is None:
            self._apply_text(self._value)
            return False
        self._apply_value(self._clamp(parsed))
        return True

    def _apply_value(self, value: int) -> None:
        clamped = self._clamp(value)
        changed = clamped != self._value
        self._value = clamped
        self._apply_text(clamped)
        if changed:
            self.valueChanged.emit(clamped)

    def _apply_text(self, value: int) -> None:
        offset = len(self.text()) - self.cursorPosition()
        self.setText(format_timecode_ms(value))
        self.setCursorPosition(max(0, len(self.text()) - offset))


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
