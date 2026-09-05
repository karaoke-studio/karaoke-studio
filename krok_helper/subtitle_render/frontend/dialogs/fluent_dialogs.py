"""Fluent dialog helpers for the subtitle-render frontend.

Use qfluentwidgets controls inside ordinary top-level ``QDialog`` windows so
standalone and embedded modes both keep correct focus, modality, and theme.
"""

from __future__ import annotations

from typing import Callable, Mapping, Optional, Sequence

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDialog,
    QHBoxLayout,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    Dialog,
    EditableComboBox,
    LineEdit,
    ListWidget as FluentListWidget,
    PrimaryPushButton,
    PushButton,
    SpinBox,
)
from krok_helper.qfluent_compat import ModelessDialog, exec_modeless_dialog, show_modeless_dialog


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
    """Mask-free Fluent message dialog that also works in embedded mode."""

    def __init__(self, title: str, content: str, parent: Optional[QWidget] = None):
        anchor = _resolve_window(parent)
        super().__init__(title, content, anchor)
        self.setTitleBarVisible(False)
        self.setModal(False)
        self.setWindowModality(Qt.WindowModality.NonModal)

    def _ensure_active(self) -> None:
        self.raise_()
        self.activateWindow()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._ensure_active()
        QTimer.singleShot(0, self._ensure_active)

    def exec(self) -> int:
        return exec_modeless_dialog(self)


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
    show_modeless_dialog(dialog)


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


class FluentTextInputDialog(ModelessDialog):
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
        self.setWindowModality(Qt.WindowModality.NonModal)
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


class FluentIntInputDialog(ModelessDialog):
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
        self.setWindowModality(Qt.WindowModality.NonModal)
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


class _RowToggleListWidget(FluentListWidget):
    """整行可点的勾选列表：点名称等任意位置都能切换勾选状态。

    指示器区域的点击由 Qt 自己切换，这里不能重复切。区分方式不靠几何
    计算（各样式下指示器的位置和大小不一样），而是记住**按下时**该行的
    勾选状态：松开时若状态已被 Qt 改过，说明点在了指示器上。
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._pressed_check_state: Optional[Qt.CheckState] = None

    def mousePressEvent(self, event) -> None:  # noqa: N802
        item = self.itemAt(event.position().toPoint())
        self._pressed_check_state = (
            item.checkState() if item is not None else None
        )
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        item = self.itemAt(event.position().toPoint())
        pressed_state = self._pressed_check_state
        super().mouseReleaseEvent(event)
        if event.button() != Qt.MouseButton.LeftButton or item is None:
            return
        if not item.flags() & Qt.ItemFlag.ItemIsUserCheckable:
            return
        if pressed_state is not None and item.checkState() != pressed_state:
            return  # 指示器上的点击，Qt 已经切换过一次
        item.setCheckState(
            Qt.CheckState.Unchecked
            if item.checkState() == Qt.CheckState.Checked
            else Qt.CheckState.Checked
        )


class FluentMultiChoiceDialog(ModelessDialog):
    """Fluent dialog returning the subset of offered choices the user ticked.

    The choice list defaults to all-checked, and the primary button always
    reads how many entries will be acted on (and disables itself at zero), so
    confirming an empty or unintended batch is hard to do by accident.
    """

    #: 列表里最多直接铺开这么多行，超出的部分靠内部滚动。
    _MAX_VISIBLE_ROWS = 8

    def __init__(
        self,
        title: str,
        label: str,
        choices: Sequence[str],
        *,
        parent: Optional[QWidget] = None,
        checked: Optional[Sequence[bool]] = None,
        icons: Optional[Sequence[Optional[QIcon]]] = None,
        hint: Optional[str] = None,
        ok_text: str = "应用",
        select_all_text: str = "全选",
        clear_text: str = "全部取消",
    ) -> None:
        super().__init__(_resolve_window(parent))
        self.setWindowTitle(title)
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.setMinimumWidth(420)
        self._ok_text = str(ok_text)
        self._choices = [str(choice) for choice in choices]

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(10)
        prompt = BodyLabel(label, self)
        prompt.setWordWrap(True)
        layout.addWidget(prompt)
        if hint:
            hint_label = CaptionLabel(hint, self)
            hint_label.setWordWrap(True)
            layout.addWidget(hint_label)

        self._list = _RowToggleListWidget(self)
        self._list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._list.setVerticalScrollMode(
            QAbstractItemView.ScrollMode.ScrollPerPixel
        )
        for index, name in enumerate(self._choices):
            item = QListWidgetItem(name)
            item.setFlags(
                Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable
            )
            want = True
            if checked is not None and index < len(checked):
                want = bool(checked[index])
            item.setCheckState(
                Qt.CheckState.Checked if want else Qt.CheckState.Unchecked
            )
            if icons is not None and index < len(icons) and icons[index] is not None:
                item.setIcon(icons[index])
            self._list.addItem(item)
        if self._list.count():
            row_height = max(self._list.sizeHintForRow(0), 24)
            visible = min(self._list.count(), self._MAX_VISIBLE_ROWS)
            self._list.setFixedHeight(row_height * visible + 10)
        self._list.itemChanged.connect(lambda _item: self._sync_footer())
        layout.addWidget(self._list, 1)

        selection_row = QHBoxLayout()
        selection_row.setContentsMargins(0, 0, 0, 0)
        self._select_all_button = PushButton(select_all_text, self)
        self._select_all_button.clicked.connect(
            lambda _checked=False: self._set_all_checked(True)
        )
        self._clear_button = PushButton(clear_text, self)
        self._clear_button.clicked.connect(
            lambda _checked=False: self._set_all_checked(False)
        )
        self._selection_stats = CaptionLabel("", self)
        selection_row.addWidget(self._select_all_button)
        selection_row.addWidget(self._clear_button)
        selection_row.addStretch(1)
        selection_row.addWidget(self._selection_stats)
        layout.addLayout(selection_row)

        button_row, self.ok_button, _cancel_button = fluent_button_row(self)
        layout.addLayout(button_row)
        self._sync_footer()

    def _set_all_checked(self, checked: bool) -> None:
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        for index in range(self._list.count()):
            self._list.item(index).setCheckState(state)

    def _checked_names(self) -> list[str]:
        return [
            self._list.item(index).text()
            for index in range(self._list.count())
            if self._list.item(index).checkState() == Qt.CheckState.Checked
        ]

    def _sync_footer(self, *_args) -> None:
        selected = len(self._checked_names())
        total = self._list.count()
        self._selection_stats.setText(f"已选 {selected}/{total}")
        self.ok_button.setText(f"{self._ok_text}（{selected}）")
        self.ok_button.setEnabled(selected > 0)

    def set_checked_names(self, names: Sequence[str]) -> None:
        """Tick the items whose text appears in ``names`` and only those."""
        wanted = set(names)
        for index in range(self._list.count()):
            item = self._list.item(index)
            item.setCheckState(
                Qt.CheckState.Checked if item.text() in wanted else Qt.CheckState.Unchecked
            )
        self._sync_footer()

    def value(self) -> list[str]:
        return self._checked_names()


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
    accepted = exec_modeless_dialog(dialog) == QDialog.DialogCode.Accepted
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
    accepted = exec_modeless_dialog(dialog) == QDialog.DialogCode.Accepted
    return dialog.value(), accepted


def fluent_get_multiple(
    parent: Optional[QWidget],
    title: str,
    label: str,
    choices: Sequence[str],
    *,
    checked: Optional[Sequence[bool]] = None,
    icons: Optional[Sequence[Optional[QIcon]]] = None,
    hint: Optional[str] = None,
    ok_text: str = "应用",
) -> tuple[list[str], bool]:
    """Ask for a subset of ``choices``; empty list + ``False`` means cancelled."""
    dialog = FluentMultiChoiceDialog(
        title,
        label,
        choices,
        parent=parent,
        checked=checked,
        icons=icons,
        hint=hint,
        ok_text=ok_text,
    )
    accepted = exec_modeless_dialog(dialog) == QDialog.DialogCode.Accepted
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
    accepted = exec_modeless_dialog(dialog) == QDialog.DialogCode.Accepted
    return dialog.value(), accepted
