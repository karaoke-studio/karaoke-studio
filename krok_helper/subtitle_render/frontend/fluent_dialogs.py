"""Fluent dialog helpers for the subtitle-render frontend.

Use qfluentwidgets controls inside ordinary top-level ``QDialog`` windows so
standalone and embedded modes both keep correct focus, modality, and theme.
"""

from __future__ import annotations

from typing import Callable, Mapping, Optional, Sequence

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor, QPainter
from PyQt6.QtWidgets import QApplication, QDialog, QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    Dialog,
    EditableComboBox,
    LineEdit,
    PrimaryPushButton,
    PushButton,
    SpinBox,
)


class _DimOverlay(QWidget):
    """Non-interactive dim layer below a top-level Fluent dialog."""

    def __init__(self, anchor_window: QWidget) -> None:
        super().__init__(anchor_window)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setGeometry(0, 0, anchor_window.width(), anchor_window.height())

    def paintEvent(self, event) -> None:  # noqa: N802, ARG002
        painter = QPainter(self)
        try:
            painter.fillRect(self.rect(), QColor(0, 0, 0, 96))
        finally:
            painter.end()


def _resolve_window(parent: Optional[QWidget]) -> Optional[QWidget]:
    if parent is not None:
        window = parent.window()
        if window is not None:
            return window
    app = QApplication.instance()
    if app is not None:
        active = app.activeWindow()
        if active is not None:
            return active
        for window in app.topLevelWidgets():
            if window.isVisible():
                return window
    return parent


class FluentMessageDialog(Dialog):
    """Clickable Fluent message dialog that also works in embedded mode."""

    def __init__(self, title: str, content: str, parent: Optional[QWidget] = None):
        anchor = _resolve_window(parent)
        super().__init__(title, content, anchor)
        self.setTitleBarVisible(False)
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self._anchor_window = anchor
        self._dim: Optional[_DimOverlay] = None

    def _ensure_active(self) -> None:
        self.raise_()
        self.activateWindow()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._ensure_active()
        QTimer.singleShot(0, self._ensure_active)

    def exec(self) -> int:
        anchor = self._anchor_window
        if anchor is not None and anchor.isVisible():
            self._dim = _DimOverlay(anchor)
            self._dim.show()
            self._dim.raise_()
        try:
            return super().exec()
        finally:
            if self._dim is not None:
                self._dim.hide()
                self._dim.deleteLater()
                self._dim = None


def fluent_info(
    parent: Optional[QWidget],
    title: str,
    content: str,
    *,
    ok_text: str = "确定",
    copyable: bool = False,
) -> None:
    """Show a single-button Fluent message dialog."""
    dialog = FluentMessageDialog(title, content, parent)
    dialog.yesButton.setText(ok_text)
    dialog.hideCancelButton()
    if copyable:
        dialog.setContentCopyable(True)
    dialog.exec()


# qfluentwidgets Dialog has no severity icon area. Keep semantic aliases so
# callers remain explicit and future styling can differentiate them centrally.
fluent_warning = fluent_info
fluent_error = fluent_info


def fluent_question(
    parent: Optional[QWidget],
    title: str,
    content: str,
    *,
    yes_text: str = "确定",
    no_text: str = "取消",
    default_cancel: bool = False,
) -> bool:
    dialog = FluentMessageDialog(title, content, parent)
    dialog.yesButton.setText(yes_text)
    dialog.cancelButton.setText(no_text)
    if default_cancel:
        dialog.cancelButton.setFocus()
    return bool(dialog.exec())


def fluent_choice(
    parent: Optional[QWidget],
    title: str,
    content: str,
    buttons: Sequence[str],
    *,
    default: int = 0,
    sticky: Optional[Mapping[int, Callable[[], None]]] = None,
) -> int:
    """Return the clicked button index, or ``-1`` when closed externally.

    ``sticky`` maps a button index to a handler that runs in place and leaves
    the dialog open, so the user can act on it and still pick another button
    afterwards. Sticky buttons never become the return value.
    """
    if len(buttons) < 2:
        raise ValueError("fluent_choice requires at least two buttons")
    dialog = FluentMessageDialog(title, content, parent)
    handlers = dict(sticky or {})
    selected = {"index": -1}

    def choose(index: int) -> None:
        selected["index"] = index

    def make_sticky(button, handler: Callable[[], None]) -> None:
        # yes/cancel 默认被 qfluentwidgets 接到 accept/reject，先摘掉才能不关闭。
        try:
            button.clicked.disconnect()
        except TypeError:
            pass
        button.clicked.connect(lambda _checked=False, run=handler: run())

    ordered = [dialog.yesButton]
    dialog.yesButton.setText(buttons[0])
    if 0 in handlers:
        make_sticky(dialog.yesButton, handlers[0])
    else:
        dialog.yesButton.clicked.connect(lambda: choose(0))
    for index in range(1, len(buttons) - 1):
        button = PushButton(buttons[index], dialog.buttonGroup)
        button.setAttribute(Qt.WidgetAttribute.WA_LayoutUsesWidgetRect)
        handler = handlers.get(index)
        if handler is None:
            button.clicked.connect(
                lambda _checked=False, value=index: (choose(value), dialog.accept())
            )
        else:
            button.clicked.connect(lambda _checked=False, run=handler: run())
        dialog.buttonLayout.insertWidget(
            dialog.buttonLayout.count() - 1,
            button,
            1,
            Qt.AlignmentFlag.AlignVCenter,
        )
        ordered.append(button)
    last = len(buttons) - 1
    dialog.cancelButton.setText(buttons[last])
    if last in handlers:
        make_sticky(dialog.cancelButton, handlers[last])
    else:
        dialog.cancelButton.clicked.connect(lambda: choose(last))
    ordered.append(dialog.cancelButton)
    if 0 <= default < len(ordered):
        ordered[default].setFocus()
    dialog.exec()
    return selected["index"]


def fluent_button_row(
    dialog: QDialog,
    *,
    ok_text: str = "确定",
    cancel_text: str = "取消",
) -> tuple[QHBoxLayout, PrimaryPushButton, PushButton]:
    row = QHBoxLayout()
    row.addStretch(1)
    ok_button = PrimaryPushButton(ok_text, dialog)
    cancel_button = PushButton(cancel_text, dialog)
    ok_button.clicked.connect(dialog.accept)
    cancel_button.clicked.connect(dialog.reject)
    row.addWidget(ok_button)
    row.addWidget(cancel_button)
    return row, ok_button, cancel_button


class FluentTextInputDialog(QDialog):
    """Small Fluent text or editable-choice input dialog."""

    def __init__(
        self,
        title: str,
        label: str,
        *,
        text: str = "",
        choices: Optional[Sequence[str]] = None,
        placeholder: str = "",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(_resolve_window(parent))
        self.setWindowTitle(title)
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setMinimumWidth(400)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(10)
        layout.addWidget(BodyLabel(label, self))
        if choices is None:
            control = LineEdit(self)
            control.setText(text)
            control.setPlaceholderText(placeholder)
            control.selectAll()
        else:
            combo = EditableComboBox(self)
            combo.setClearButtonEnabled(True)
            combo.setPlaceholderText(placeholder)
            for choice in choices:
                if choice:
                    combo.addItem(str(choice))
            combo.setText(text)
            control = combo
        self.control = control
        layout.addWidget(control)
        button_row, self.ok_button, _cancel_button = fluent_button_row(self)
        layout.addLayout(button_row)

    def value(self) -> str:
        if isinstance(self.control, EditableComboBox):
            return self.control.text().strip()
        return self.control.text().strip()


class FluentIntInputDialog(QDialog):
    """Small Fluent integer input dialog backed by a qfluentwidgets SpinBox."""

    def __init__(
        self,
        title: str,
        label: str,
        *,
        value: int = 0,
        minimum: int = -2_147_483_648,
        maximum: int = 2_147_483_647,
        step: int = 1,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(_resolve_window(parent))
        self.setWindowTitle(title)
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setMinimumWidth(400)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(10)
        layout.addWidget(BodyLabel(label, self))
        self.control = SpinBox(self)
        self.control.setRange(int(minimum), int(maximum))
        self.control.setSingleStep(max(int(step), 1))
        self.control.setValue(int(value))
        self.control.selectAll()
        layout.addWidget(self.control)
        button_row, self.ok_button, _cancel_button = fluent_button_row(self)
        layout.addLayout(button_row)

    def value(self) -> int:
        return int(self.control.value())


def fluent_get_text(
    parent: Optional[QWidget],
    title: str,
    label: str,
    *,
    text: str = "",
    placeholder: str = "",
) -> tuple[str, bool]:
    dialog = FluentTextInputDialog(
        title, label, text=text, placeholder=placeholder, parent=parent
    )
    accepted = dialog.exec() == QDialog.DialogCode.Accepted
    return dialog.value(), accepted


def fluent_get_int(
    parent: Optional[QWidget],
    title: str,
    label: str,
    *,
    value: int = 0,
    minimum: int = -2_147_483_648,
    maximum: int = 2_147_483_647,
    step: int = 1,
) -> tuple[int, bool]:
    dialog = FluentIntInputDialog(
        title,
        label,
        value=value,
        minimum=minimum,
        maximum=maximum,
        step=step,
        parent=parent,
    )
    accepted = dialog.exec() == QDialog.DialogCode.Accepted
    return dialog.value(), accepted


def fluent_get_editable_choice(
    parent: Optional[QWidget],
    title: str,
    label: str,
    choices: Sequence[str],
    *,
    text: str = "",
    placeholder: str = "",
) -> tuple[str, bool]:
    dialog = FluentTextInputDialog(
        title,
        label,
        text=text,
        choices=choices,
        placeholder=placeholder,
        parent=parent,
    )
    accepted = dialog.exec() == QDialog.DialogCode.Accepted
    return dialog.value(), accepted
