"""Fluent dialog helpers for the subtitle-render frontend.

Use qfluentwidgets controls inside ordinary top-level ``QDialog`` windows so
standalone and embedded modes both keep correct focus, modality, and theme.
"""

from __future__ import annotations

from fractions import Fraction
from math import isfinite
from typing import Callable, Mapping, Optional, Sequence

from PyQt6.QtCore import Qt, QTimer, pyqtSignal as Signal
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCompleter,
    QDialog,
    QHBoxLayout,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CheckBox,
    ComboBox,
    Dialog,
    EditableComboBox,
    LineEdit,
    ListWidget as FluentListWidget,
    PrimaryPushButton,
    PushButton,
    SpinBox,
)
from krok_helper.qfluent_compat import ModelessDialog, exec_modeless_dialog, show_modeless_dialog
from krok_helper.subtitle_render.domain.background import (
    IMAGE_SEQUENCE_FPS_PRESETS,
    normalize_sequence_fps,
)


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


def fluent_question_with_checkbox(
    parent: Optional[QWidget],
    title: str,
    content: str,
    checkbox_text: str,
    *,
    yes_text: str = "确定",
    no_text: str = "取消",
    default_cancel: bool = False,
) -> tuple[bool, bool]:
    """Ask a yes/no question plus one inline checkbox option.

    Returns ``(confirmed, checked)``. The checkbox lives under the message text
    and is read after the dialog closes, so callers can offer "don't ask again"
    style choices without persisting them.
    """
    dialog = FluentMessageDialog(title, content, parent)
    dialog.yesButton.setText(yes_text)
    dialog.cancelButton.setText(no_text)
    checkbox = CheckBox(checkbox_text, dialog)
    dialog.textLayout.addWidget(checkbox, 0, Qt.AlignmentFlag.AlignLeft)
    # 加入控件后布局需要重新激活一次，Dialog 的 SetMinimumSize 约束才会把
    # 固定尺寸长到能容下勾选框（qfluentwidgets 不会自动 propagate）。
    dialog.textLayout.activate()
    if default_cancel:
        dialog.cancelButton.setFocus()
    confirmed = bool(dialog.exec())
    return confirmed, checkbox.isChecked()


def fluent_role_delete_dialog(
    parent: Optional[QWidget],
    name: str,
    target_options: Sequence[str],
    *,
    checkbox_text: str = "本次运行内不再提醒",
    yes_text: str = "删除",
    no_text: str = "取消",
) -> tuple[bool, Optional[str], bool]:
    """删除角色确认框：选择把引用迁移到另一角色，或回归全局默认。

    返回 ``(confirmed, target, dont_ask)``：``target`` 为 ``None`` 表示回归
    全局默认，否则为迁移目标角色名；``target_options`` 里与被删角色同名的
    项会被过滤。「本次运行内不再提醒」是否记忆由调用方决定（本函数不落盘）。

    正文沿用原确认框的两行文案，第二行随迁移目标变化（默认项与原文案一致）。
    """

    def message(target: Optional[str]) -> str:
        if target is None:
            return (
                f"确定删除角色「{name}」吗？\n"
                "引用该角色的歌词、标题与导唱符会回到默认样式。"
            )
        return (
            f"确定删除角色「{name}」吗？\n"
            f"引用该角色的歌词、标题与导唱符会迁移到「{target}」。"
        )

    # 首项恒为「回归全局默认」（target=None）；其余为候选迁移目标，
    # 被删角色自己不能做目标。
    targets: list[Optional[str]] = [None]
    seen: set[str] = {str(name)}
    for role in target_options:
        role = str(role).strip()
        if not role or role in seen:
            continue
        seen.add(role)
        targets.append(role)

    dialog = FluentMessageDialog("删除角色", message(None), parent)
    dialog.yesButton.setText(yes_text)
    dialog.cancelButton.setText(no_text)

    row = QHBoxLayout()
    row.addWidget(BodyLabel("迁移到：", dialog))
    combo = ComboBox(dialog)
    combo.addItem("回归全局默认")
    for role in targets[1:]:
        combo.addItem(role)
    row.addWidget(combo, 1)
    checkbox = CheckBox(checkbox_text, dialog)
    dialog.textLayout.addLayout(row)
    dialog.textLayout.addWidget(checkbox, 0, Qt.AlignmentFlag.AlignLeft)

    def current_target() -> Optional[str]:
        index = combo.currentIndex()
        return targets[index] if 0 <= index < len(targets) else None

    def sync_message(*_args) -> None:
        dialog.content = message(current_target())
        dialog._adjustText()

    combo.currentIndexChanged.connect(sync_message)
    # Dialog 构造时 setFixedSize(240, 192)，之后只会随布局最小尺寸长大、
    # 不会缩回；宽度由正文最长的一行决定。先按最长的一种文案撑开并锁定，
    # 再换回当前选项的文案：切换迁移目标时正文不会被裁，弹窗宽度也与
    # 原来的两行确认框一致，而不是卡在 240 把下拉挤成一条。
    metrics = dialog.contentLabel.fontMetrics()
    dialog.content = max(
        (message(target) for target in targets),
        key=lambda text: max(
            metrics.horizontalAdvance(line) for line in text.split("\n")
        ),
    )
    dialog._adjustText()
    dialog.textLayout.activate()
    dialog.setFixedSize(dialog.size())
    sync_message()
    dialog.cancelButton.setFocus()
    confirmed = bool(dialog.exec())
    return confirmed, current_target(), checkbox.isChecked()


#: 会话级「本次运行不再提示」标记（按用途分键；不落盘，重启即恢复询问）。
_DONT_ASK_SESSION_FLAGS: dict[str, bool] = {}


def dont_ask_active(key: str) -> bool:
    """该用途是否已被用户勾选「本次运行不再提示」。"""

    return bool(_DONT_ASK_SESSION_FLAGS.get(key))


def reset_dont_ask_flags() -> None:
    """清空会话标记（测试与「恢复默认偏好」用）。"""

    _DONT_ASK_SESSION_FLAGS.clear()


def fluent_confirm_dont_ask(
    parent: Optional[QWidget],
    title: str,
    content: str,
    *,
    key: str,
    checkbox_text: str = "本次运行不再提示",
    yes_text: str = "确定",
    no_text: str = "取消",
    default_cancel: bool = True,
) -> bool:
    """确认框 + 「本次运行不再提示」勾选（会话级生效）。

    与 :func:`fluent_question_with_checkbox` 的区别：本函数自己记住勾选，
    同一 ``key`` 后续直接返回 True，不再弹窗。删除类二次确认用它。
    """

    if dont_ask_active(key):
        return True
    confirmed, dont_ask = fluent_question_with_checkbox(
        parent,
        title,
        content,
        checkbox_text,
        yes_text=yes_text,
        no_text=no_text,
        default_cancel=default_cancel,
    )
    if confirmed and dont_ask:
        _DONT_ASK_SESSION_FLAGS[key] = True
    return confirmed


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


def format_sequence_fps_text(value: int | float) -> str:
    """回显文本：小数表示紧凑时按小数（60、0.5、0.25），冗长时按分数（1/30）。"""
    if isinstance(value, int):
        return str(value)
    decimal_text = repr(float(value))
    if len(decimal_text) <= 6:
        return decimal_text
    fraction_text = str(Fraction(decimal_text).limit_denominator(1_000_000))
    if len(fraction_text) < len(decimal_text):
        return fraction_text
    return decimal_text


def parse_sequence_fps_text(
    text: str,
    *,
    minimum: float = 0.001,
    maximum: float = 240.0,
) -> Optional[int | float]:
    """解析帧率文本：小数（0.5）或分数（1/30）；非法/超范围返回 ``None``。"""
    raw = text.strip()
    if not raw:
        return None
    try:
        # ``Fraction`` 原生接受 "24"、"0.5"、"1/30" 三类写法且全程精确。
        value = Fraction(raw)
        fps = float(value)
    except (ValueError, ZeroDivisionError, OverflowError):
        return None
    if not isfinite(fps) or fps <= 0:
        return None
    # 边界经 repr 转 Fraction：二进制浮点 0.001 略大于 1/1000，直接
    # Fraction(0.001) 会把合法的下限值 "0.001" 误判为超范围。
    if not (Fraction(repr(float(minimum))) <= value <= Fraction(repr(float(maximum)))):
        return None
    return int(fps) if fps.is_integer() else fps


class FilterableFpsCombo(EditableComboBox):
    """可输入并即时筛选预设的图片序列源帧率选择框。

    三种输入等效：点预设项、直接输小数（0.5）、输分数（1/30）。输入内容
    即时过滤预设下拉（包含匹配）；非法或超范围的文本在回车/失焦时回退到
    上一个合法值，自定义值不会被加入预设列表。
    """

    fpsValidated = Signal(object)

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        *,
        presets: Sequence[int | float] = IMAGE_SEQUENCE_FPS_PRESETS,
        value: int | float = 60,
        minimum: float = 0.001,
        maximum: float = 240.0,
        commit_delay_ms: int = 200,
    ) -> None:
        super().__init__(parent)
        self._minimum = float(minimum)
        self._maximum = float(maximum)
        self._value = self._bounded(value)
        self._syncing = False

        self.setClearButtonEnabled(True)
        self.setPlaceholderText("预设或直接输入，如 60 / 0.5 / 1/30")
        for preset in presets:
            fps = normalize_sequence_fps(preset)
            if self.findText(format_sequence_fps_text(fps)) < 0:
                self.addItem(format_sequence_fps_text(fps))

        completer = QCompleter([self.itemText(i) for i in range(self.count())], self)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setMaxVisibleItems(10)
        self.setCompleter(completer)

        self._commit_timer = QTimer(self)
        self._commit_timer.setSingleShot(True)
        self._commit_timer.setInterval(int(commit_delay_ms))
        self._commit_timer.timeout.connect(self._commit_typing)
        # currentTextChanged 覆盖三条路径：键盘输入、下拉/补全选预设、
        # 程序设值（后者由 _syncing 挡掉）。
        self.currentTextChanged.connect(
            lambda _text: self._commit_timer.start() if not self._syncing else None
        )
        self.editingFinished.connect(self._flush_edit)
        self._apply_value(self._value)

    def value(self) -> int | float:
        return self._value

    def _bounded(self, value: int | float) -> int | float:
        """程序设值统一夹到 [minimum, maximum]（工程/N3 里带来的越界值）。"""
        fps = min(max(float(normalize_sequence_fps(value)), self._minimum), self._maximum)
        return int(fps) if fps.is_integer() else fps

    def setValue(self, value: int | float) -> None:  # noqa: N802 - Qt API
        # 程序设值不发射，供宿主同步回显使用（仿 TimecodeEdit 的宿主 _syncing 约定）。
        bounded = self._bounded(value)
        was = self._syncing
        self._syncing = True
        try:
            self._value = bounded
            self._apply_text(bounded)
        finally:
            self._syncing = was

    def submit_text(self, text: str) -> bool:
        """直接提交一段文本；合法返回 True 并生效，非法回退返回 False。"""
        self.setText(text)
        return self._flush_edit()

    def _commit_typing(self) -> None:
        if self._syncing:
            return
        parsed = parse_sequence_fps_text(
            self.text(), minimum=self._minimum, maximum=self._maximum
        )
        if parsed is not None and parsed != self._value:
            self._value = parsed
            self.fpsValidated.emit(parsed)

    def _flush_edit(self) -> bool:
        self._commit_timer.stop()
        if self._syncing:
            return False
        parsed = parse_sequence_fps_text(
            self.text(), minimum=self._minimum, maximum=self._maximum
        )
        if parsed is None:
            self._apply_text(self._value)
            return False
        self._apply_value(parsed)
        return True

    def _apply_value(self, value: int | float) -> None:
        changed = value != self._value
        self._value = value
        self._apply_text(value)
        if changed and not self._syncing:
            self.fpsValidated.emit(value)

    def _apply_text(self, value: int | float) -> None:
        was = self._syncing
        self._syncing = True
        try:
            self.setText(format_sequence_fps_text(value))
        finally:
            self._syncing = was

    def _onReturnPressed(self) -> None:
        # 覆写基类：不把自定义文本 addItem 进预设列表，改为校验提交。
        self._flush_edit()

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt API
        # 非法文本上按回车：吞掉按键，避免冒泡触发对话框的默认「确定」
        # 按钮带着旧值静默关窗（此时 _flush_edit 已把文本回退）。
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if not self._flush_edit():
                event.accept()
                return
        super().keyPressEvent(event)


class FluentFpsInputDialog(ModelessDialog):
    """Small Fluent fps input dialog with a filterable preset combo."""

    def __init__(
        self,
        title: str,
        label: str,
        *,
        value: int | float = 60,
        presets: Sequence[int | float] = IMAGE_SEQUENCE_FPS_PRESETS,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(_resolve_window(parent))
        self.setWindowTitle(title)
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.setMinimumWidth(400)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(10)
        label_widget = BodyLabel(label, self)
        label_widget.setWordWrap(True)
        layout.addWidget(label_widget)
        self.control = FilterableFpsCombo(self, presets=presets, value=value)
        self.control.selectAll()
        layout.addWidget(self.control)
        button_row, self.ok_button, _cancel_button = fluent_button_row(self)
        layout.addLayout(button_row)

    def value(self) -> int | float:
        return self.control.value()


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


def fluent_get_fps(
    parent: Optional[QWidget],
    title: str,
    label: str,
    *,
    value: int | float = 60,
    presets: Sequence[int | float] = IMAGE_SEQUENCE_FPS_PRESETS,
) -> tuple[int | float, bool]:
    """Ask for an image-sequence source fps via the filterable preset combo."""
    dialog = FluentFpsInputDialog(
        title, label, value=value, presets=presets, parent=parent
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
