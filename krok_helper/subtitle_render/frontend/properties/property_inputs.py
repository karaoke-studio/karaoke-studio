"""Input controls shared by subtitle property pages."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any, Optional

from PyQt6.QtCore import QRegularExpression, QSize, Qt, QTimer, pyqtSignal as Signal
from PyQt6.QtGui import QFont, QRegularExpressionValidator, QValidator
from PyQt6.QtWidgets import QSizePolicy, QStackedWidget, QStyle, QWidget
from qfluentwidgets import (
    ComboBox as FluentComboBox,
    DoubleSpinBox as FluentDoubleSpinBox,
    LineEdit as FluentLineEdit,
    PlainTextEdit as FluentPlainTextEdit,
    SpinBox as FluentSpinBox,
)

from krok_helper.subtitle_render.n3.font_catalog import (
    canonicalize_n3_font_family,
    n3_font_families,
)
from krok_helper.subtitle_render.engine.timing.timecode import format_timecode_ms, parse_timecode_ms


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


class UnitProtectedSpinBoxMixin:
    """Keep spin-box prefixes and suffixes outside editable selections."""

    def _install_debounced_keyboard_commit(self, commit_delay_ms: int = 200) -> None:
        self.setKeyboardTracking(False)
        self._keyboard_commit_pending = False
        self._keyboard_commit_timer = QTimer(self)
        self._keyboard_commit_timer.setSingleShot(True)
        self._keyboard_commit_timer.setInterval(int(commit_delay_ms))
        self._keyboard_commit_timer.timeout.connect(self._commit_keyboard_edit)
        self.lineEdit().textEdited.connect(self._queue_keyboard_commit)
        self.editingFinished.connect(self._flush_keyboard_edit)

    def _queue_keyboard_commit(self, _text: str) -> None:
        self._keyboard_commit_pending = True
        self._keyboard_commit_timer.start()

    def _commit_keyboard_edit(self) -> None:
        if not self._keyboard_commit_pending:
            return
        editor = self.lineEdit()
        text = editor.text()
        state, _fixed, _pos = self.validate(text, editor.cursorPosition())
        if state != QValidator.State.Acceptable:
            return
        self._keyboard_commit_timer.stop()
        self._keyboard_commit_pending = False
        cursor = editor.cursorPosition()
        selection_start = editor.selectionStart()
        selection_length = len(editor.selectedText())
        self.interpretText()
        if editor.text() != text:
            self._restore_editor_text(
                text,
                cursor,
                selection_start,
                selection_length,
            )

    def _flush_keyboard_edit(self) -> None:
        self._keyboard_commit_timer.stop()
        self._keyboard_commit_pending = False
        self.interpretText()

    def _restore_editor_text(
        self,
        text: str,
        cursor: int,
        selection_start: int,
        selection_length: int,
    ) -> None:
        editor = self.lineEdit()
        self._protecting_unit_selection = True
        try:
            editor.setText(text)
            if selection_start >= 0 and selection_length > 0:
                editor.setSelection(selection_start, selection_length)
            else:
                editor.setCursorPosition(min(cursor, len(text)))
        finally:
            self._protecting_unit_selection = False

    def _install_unit_selection_guard(self) -> None:
        self._protecting_unit_selection = False
        editor = self.lineEdit()
        editor.selectionChanged.connect(self._keep_units_out_of_selection)
        editor.cursorPositionChanged.connect(self._keep_cursor_out_of_units)

    def _editable_text_bounds(self) -> tuple[int, int]:
        editor_text = self.lineEdit().text()
        prefix = self.prefix()
        suffix = self.suffix()
        start = len(prefix) if prefix and editor_text.startswith(prefix) else 0
        end = (
            len(editor_text) - len(suffix)
            if suffix and editor_text.endswith(suffix)
            else len(editor_text)
        )
        return start, max(start, end)

    def _keep_units_out_of_selection(self) -> None:
        if self._protecting_unit_selection:
            return
        editor = self.lineEdit()
        selection_start = editor.selectionStart()
        if selection_start < 0:
            return
        selection_end = selection_start + len(editor.selectedText())
        value_start, value_end = self._editable_text_bounds()
        protected_start = max(selection_start, value_start)
        protected_end = min(selection_end, value_end)
        if (
            protected_start == selection_start
            and protected_end == selection_end
        ):
            return

        self._protecting_unit_selection = True
        try:
            if protected_start >= protected_end:
                editor.deselect()
                editor.setCursorPosition(
                    value_start if selection_end <= value_start else value_end
                )
            elif editor.cursorPosition() <= selection_start:
                editor.setSelection(
                    protected_end,
                    protected_start - protected_end,
                )
            else:
                editor.setSelection(
                    protected_start,
                    protected_end - protected_start,
                )
        finally:
            self._protecting_unit_selection = False

    def _keep_cursor_out_of_units(self, _old: int, current: int) -> None:
        if self._protecting_unit_selection:
            return
        editor = self.lineEdit()
        if editor.hasSelectedText():
            return
        value_start, value_end = self._editable_text_bounds()
        protected_position = min(max(current, value_start), value_end)
        if protected_position == current:
            return
        self._protecting_unit_selection = True
        try:
            editor.setCursorPosition(protected_position)
        finally:
            self._protecting_unit_selection = False


class WheelFocusedSpinBox(UnitProtectedSpinBoxMixin, FluentSpinBox):
    """Direct-entry integer spin box with focused wheel input."""

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        *,
        commit_delay_ms: int = 200,
    ) -> None:
        super().__init__(parent)
        self.setSymbolVisible(False)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.lineEdit().setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.lineEdit().textChanged.connect(self._sync_text_minimum)
        self.valueChanged.connect(
            lambda _value: QTimer.singleShot(0, self._sync_text_minimum)
        )
        self._install_debounced_keyboard_commit(commit_delay_ms)
        self._install_unit_selection_guard()

    def showEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().showEvent(event)
        self._sync_text_minimum()

    def _sync_text_minimum(self) -> None:
        style = self.style()
        text_width = self.lineEdit().fontMetrics().horizontalAdvance(
            self.lineEdit().text()
        )
        text_chrome = (
            2 * style.pixelMetric(QStyle.PixelMetric.PM_LayoutLeftMargin)
            + 2 * style.pixelMetric(QStyle.PixelMetric.PM_FocusFrameHMargin)
        )
        self.setMinimumWidth(max(text_width + text_chrome, 1))

    def wheelEvent(self, event):  # noqa: N802 - Qt API
        if not (self.hasFocus() or self.lineEdit().hasFocus()):
            event.ignore()
            return
        delta = event.angleDelta().y()
        if event.inverted():
            delta = -delta
        if delta:
            steps = int(delta / 120) or (1 if delta > 0 else -1)
            self.stepBy(steps)
            event.accept()
            return
        super().wheelEvent(event)


class WheelFocusedDoubleSpinBox(UnitProtectedSpinBoxMixin, FluentDoubleSpinBox):
    """Floating-point counterpart for exact property values."""

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        *,
        commit_delay_ms: int = 200,
    ) -> None:
        super().__init__(parent)
        self.setSymbolVisible(False)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.lineEdit().setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.lineEdit().textChanged.connect(self._sync_text_minimum)
        self.valueChanged.connect(
            lambda _value: QTimer.singleShot(0, self._sync_text_minimum)
        )
        self._install_debounced_keyboard_commit(commit_delay_ms)
        self._install_unit_selection_guard()

    def showEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().showEvent(event)
        self._sync_text_minimum()

    def _sync_text_minimum(self) -> None:
        style = self.style()
        text_width = self.lineEdit().fontMetrics().horizontalAdvance(
            self.lineEdit().text()
        )
        text_chrome = (
            2 * style.pixelMetric(QStyle.PixelMetric.PM_LayoutLeftMargin)
            + 2 * style.pixelMetric(QStyle.PixelMetric.PM_FocusFrameHMargin)
        )
        self.setMinimumWidth(max(text_width + text_chrome, 1))

    def wheelEvent(self, event):  # noqa: N802 - Qt API
        if not (self.hasFocus() or self.lineEdit().hasFocus()):
            event.ignore()
            return
        delta = event.angleDelta().y()
        if event.inverted():
            delta = -delta
        if delta:
            steps = int(delta / 120) or (1 if delta > 0 else -1)
            self.stepBy(steps)
            event.accept()
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
