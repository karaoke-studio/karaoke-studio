"""Input controls shared by subtitle property pages."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any, Optional

from PyQt6.QtCore import (
    QEvent,
    QPoint,
    QPropertyAnimation,
    QRegularExpression,
    QSize,
    Qt,
    QTimer,
    pyqtSignal as Signal,
)
from PyQt6.QtGui import (
    QAction,
    QFont,
    QRegularExpressionValidator,
    QValidator,
)
from PyQt6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QSizePolicy,
    QStackedWidget,
    QStyle,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    ComboBox as FluentComboBox,
    DoubleSpinBox as FluentDoubleSpinBox,
    LineEdit as FluentLineEdit,
    PlainTextEdit as FluentPlainTextEdit,
    Slider,
    SpinBox as FluentSpinBox,
)
from qfluentwidgets.components.widgets.combo_box import ComboBoxMenu
from qfluentwidgets.components.widgets.menu import (
    MenuAnimationManager,
    MenuAnimationType,
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
    """Report the current page height instead of the tallest page height.

    ``stable_width`` optionally pins the preferred width across pages.  The
    fill editor uses it because the horizontal-gradient page asks for a much
    wider layout than solid/vertical/image; a mode-dependent width would
    change the surrounding colour/font pair's breakpoint and stretch as soon
    as the user switches fill mode.
    """

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        *,
        stable_width: Optional[int] = None,
    ) -> None:
        super().__init__(parent)
        self._stable_width = (
            None if stable_width is None else max(1, int(stable_width))
        )

    def set_stable_width(self, width: int) -> None:
        stable_width = max(1, int(width))
        if stable_width == self._stable_width:
            return
        self._stable_width = stable_width
        self.updateGeometry()

    def sizeHint(self) -> QSize:  # noqa: N802
        widget = self.currentWidget()
        hint = widget.sizeHint() if widget is not None else super().sizeHint()
        if self._stable_width is not None:
            return QSize(self._stable_width, hint.height())
        return hint

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        widget = self.currentWidget()
        hint = (
            widget.minimumSizeHint()
            if widget is not None
            else super().minimumSizeHint()
        )
        if self._stable_width is not None:
            # The stable width is the widest page minimum, so every page
            # still fits; keep the current page's hard floor so a forced
            # narrow layout can still shrink instead of clipping.
            return QSize(
                min(self._stable_width, max(0, hint.width())),
                hint.height(),
            )
        return hint


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
        self.setMinimumWidth(190)
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


def _widget_is_within(widget: Optional[QWidget], ancestor: QWidget) -> bool:
    """Return True when ``widget`` is ``ancestor`` itself or one of its children."""
    while widget is not None:
        if widget is ancestor:
            return True
        widget = widget.parentWidget()
    return False


class _FontMenuSearchEdit(FluentLineEdit):
    """Filter box pinned above the font popup list; navigation keys go to the list."""

    def __init__(self, menu: _FilterableFontMenu) -> None:
        super().__init__(menu)
        self._menu = menu
        self.setPlaceholderText("输入以筛选字体")
        self.setClearButtonEnabled(True)
        self.setFixedHeight(33)

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt API
        key = event.key()
        if key in (Qt.Key.Key_Down, Qt.Key.Key_Up):
            self._menu.move_selection(1 if key == Qt.Key.Key_Down else -1)
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._menu.activate_current_item()
        elif key == Qt.Key.Key_Escape:
            self._menu.close()
        else:
            super().keyPressEvent(event)


class _FilterableFontMenu(ComboBoxMenu):
    """Font popup with a search strip pinned above the scrollable list.

    The filter box lives in the list viewport's top margin, so it never
    scrolls away — not even when the popup opens scrolled to the current
    item deep inside a long catalog.  List row 0 is the no-match hint and
    the font actions start at ``_FIRST_ITEM_ROW``; their combo item index
    never shifts, so hiding rows keeps the action-to-item mapping intact.
    """

    _SEARCH_TOP_INSET = 4
    _SEARCH_SIDE_INSET = 12
    _SEARCH_GAP = 6
    _EMPTY_HINT_ROW = 0
    _FIRST_ITEM_ROW = 1

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent=parent)
        self._default_row = -1
        self._exec_pos: Optional[QPoint] = None
        self._ani_type = MenuAnimationType.DROP_DOWN
        self._search = _FontMenuSearchEdit(self)
        # 挂在 view（而非 viewport）上：viewport 的子控件会随 contents scroll
        # 一起被挪动，只有 view 层的子控件才能钉死在 viewportMargins 预留的
        # 顶部条带里，列表无论滚到哪里搜索框都不动。
        self._search.setParent(self.view)
        self._search.raise_()
        self._search.show()
        self.view.setViewportMargins(
            0,
            self._SEARCH_TOP_INSET + self._search.height() + self._SEARCH_GAP,
            0,
            6,
        )
        self._empty_hint = BodyLabel("未找到匹配的字体", self)
        self.addWidget(self._empty_hint, selectable=False)
        self.view.item(self._EMPTY_HINT_ROW).setHidden(True)
        self._search.textChanged.connect(self._apply_filter)
        # 聚焦定时器只在构造时建一个：菜单会跨打开缓存复用，exec 里现建
        # 会给 search 堆一堆用完即弃的 QTimer 子对象。
        # 定时器挂在 search 之下：菜单先关再触发时定时器随其销毁，回调不会
        # 摸到已删除的 C++ 对象（PyQt6 没有 QPointer 可用）。
        self._focus_timer = QTimer(self._search)
        self._focus_timer.setSingleShot(True)
        self._focus_timer.timeout.connect(self._focus_search)
        # Windows 的输入法（IME）不会附加到 Qt::Popup 类窗口上，筛选框因此
        # 只收得到直接按键（英文），中文等需要输入法组合的文字根本打不进
        # 来。换成 Tool 类窗口换回输入法支持；失去的「点外部自动关闭」由
        # 应用级按下监听（eventFilter）与失活关闭（event）补上。
        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.NoDropShadowWindowHint
        )

    def action_for_item(self, index: int) -> QAction:
        """Return the menu action bound to combo item ``index``."""
        return self.menuActions()[self._FIRST_ITEM_ROW + index]

    def _hasItemIcon(self) -> bool:  # noqa: N802 - qfluentwidgets hook
        """Font rows never carry icons; short-circuit the per-add icon scan.

        上游 _adjustItemText 每加一条都全量扫描 ``_actions`` 的图标
        （O(N²)，500+ 字体约 80ms）。若上游改名后不再调到这里，只会
        退回慢路径，行为不变。
        """
        return False

    def add_font_actions(self, actions: Iterable[QAction]) -> None:
        """Populate font rows in one pass, adjusting the layout once at the end.

        上游 RoundMenu.addAction 每条都做一次全列表 adjustSize 与图标扫描
        （各是 O(N²)，500+ 字体一次填充约 210ms，即打开瞬间的卡顿）。
        上游私有方法不可用时逐条回退到公开 addAction（慢但正确）。
        """
        create_item = getattr(self, "_createActionItem", None)
        if not callable(create_item):
            for action in actions:
                self.addAction(action)
            return
        view = self.view
        view.setUpdatesEnabled(False)
        # view.addItem 每条末尾也会全量 adjustSize（同样是 O(N²)）：填充期间
        # 把实例上的 adjustSize 短路掉，结束后统一调一次真实实现。若上游
        # 不再调用它，这里的短路自然失效，只是退回慢路径。
        real_adjust_size = view.adjustSize
        view.adjustSize = lambda *args, **kwargs: None
        try:
            for action in actions:
                view.addItem(create_item(action))
        finally:
            del view.adjustSize
            view.setUpdatesEnabled(True)
        real_adjust_size()

    def set_default_item(self, index: int) -> None:
        """Highlight the row bound to combo item ``index`` on open."""
        self._default_row = self._FIRST_ITEM_ROW + index
        self.view.setCurrentRow(self._default_row)

    def move_selection(self, offset: int) -> None:
        """Move the highlighted row among visible (non-filtered) rows."""
        rows = self._visible_item_rows()
        if not rows:
            return
        view = self.view
        try:
            pos = rows.index(view.currentRow())
        except ValueError:
            pos = 0 if offset > 0 else len(rows) - 1
        view.setCurrentRow(rows[(pos + offset) % len(rows)])

    def activate_current_item(self) -> None:
        """Commit the highlighted (or first visible) row like a mouse click."""
        rows = self._visible_item_rows()
        if not rows:
            return
        view = self.view
        row = view.currentRow() if view.currentRow() in rows else rows[0]
        action = view.item(row).data(Qt.ItemDataRole.UserRole)
        if action is None or not action.isEnabled():
            return
        self.close()
        action.trigger()

    def exec(  # noqa: N802 - qfluentwidgets API
        self,
        pos: QPoint,
        ani: bool = True,
        aniType: MenuAnimationType = MenuAnimationType.DROP_DOWN,
    ) -> None:
        self._exec_pos = QPoint(pos)
        self._ani_type = aniType
        # 菜单实例跨打开复用：上一次的筛选文字与隐藏行必须先复位，否则
        # 重开的列表带着旧过滤状态。clear 会经 textChanged 触发复位。
        if self._search.text():
            self._search.clear()
        self._fit_view(pos, aniType)
        self.aniManager = MenuAnimationManager.make(self, aniType)
        # 不播滑动动画：仅借用方向管理器计算终点。availableViewSize 与
        # _endPosition 都随 DROP_DOWN / PULL_UP 变化（NONE 会按全屏高度
        # 算尺寸、按下拉语义锚定，弹层又会高又窜位），动画本身不启动、
        # 直接就位即可。
        self.move(self.aniManager._endPosition(pos))
        self.show()
        # 首次 show 之前 viewport 的 resize 事件是挂起的，此刻读到的几何
        # 还是布局前的旧值，show 后必须重新放置一次。
        self._place_search()
        # Tool 窗口没有 Qt::Popup 的系统级「点外部关闭」，挂一个应用级
        # 按下监听补上；隐藏时（hideEvent）移除，销毁时 Qt 也会自动注销。
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)
        self._focus_timer.start(0)

    def event(self, e) -> bool:  # noqa: N802 - Qt API
        # 切到别的程序 / 别的窗口（Alt+Tab、点击其他应用）时收起菜单；
        # 应用内点击由 eventFilter 的按下监听处理。
        if e.type() == QEvent.Type.WindowDeactivate:
            self.close()
        return super().event(e)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 - Qt API
        # 真实鼠标按下会先以 QWindow（顶层窗口本身）为接收者过一遍应用级
        # 过滤器，随后的子控件分发才带上具体 QWidget。窗口阶段拿不到控件、
        # 一律不判定；否则菜单自家窗口上的任何按下（含滚动条、列表行）都会
        # 被误判为「点在外部」而收起。
        if (
            event.type() == QEvent.Type.MouseButtonPress
            and self.isVisible()
            and isinstance(obj, QWidget)
        ):
            if not _widget_is_within(obj, self):
                self._close_for_outside_press(obj)
        return super().eventFilter(obj, event)

    def hideEvent(self, event) -> None:  # noqa: N802 - Qt API
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
        super().hideEvent(event)

    def _close_for_outside_press(self, pressed: Optional[QWidget]) -> None:
        """Close for a press outside the menu.

        A press on the owner combo additionally arms the reopen guard: the
        pending release would run ``_toggleComboMenu`` and instantly reopen
        the menu otherwise.
        """
        combo = self.parent()
        on_combo = isinstance(
            combo, WheelFocusedFontComboBox
        ) and _widget_is_within(pressed, combo)
        self.close()
        if on_combo:
            # close() 的 closedSignal 在 win32 上按光标位置决定是否保留
            # dropMenu（此时光标仍在组合框上会保留）；统一清掉并立牌，让
            # 随后的 release toggle 不再把菜单弹回来。
            combo.dropMenu = None
            combo._suppress_combo_reopen = True

    def _focus_search(self) -> None:
        """Re-pin the filter box once layout settles, then take keyboard focus."""
        self._place_search()
        # Tool 窗口必须先成为活动窗口，IME 才会挂到筛选框上；只 setFocus
        # 不足以激活输入法。
        self.activateWindow()
        self._search.setFocus()

    def _visible_item_rows(self) -> list[int]:
        view = self.view
        return [
            row
            for row in range(self._FIRST_ITEM_ROW, view.count())
            if not view.item(row).isHidden()
        ]

    def _apply_filter(self, text: str) -> None:
        view = self.view
        needle = text.strip().casefold()
        matches = 0
        for row in range(self._FIRST_ITEM_ROW, view.count()):
            item = view.item(row)
            hidden = bool(needle) and needle not in item.text().casefold()
            item.setHidden(hidden)
            if not hidden:
                matches += 1
        view.item(self._EMPTY_HINT_ROW).setHidden(matches > 0)
        rows = self._visible_item_rows()
        if needle and rows:
            view.scrollToTop()
            view.setCurrentRow(rows[0])
        else:
            view.setCurrentRow(self._default_row)
            if 0 <= self._default_row < view.count():
                view.scrollToItem(view.item(self._default_row))
        self._fit_view(self._exec_pos, self._ani_type)
        self._keep_anchored()

    def _fit_view(self, pos: Optional[QPoint], ani_type: MenuAnimationType) -> None:
        """Resize the list to visible rows only, mirroring adjustSize minus hidden rows."""
        view = self.view
        margins = view.viewportMargins()
        width_limit, height_limit = MenuAnimationManager.make(
            self, ani_type
        ).availableViewSize(pos)

        content_width = 0
        for row in range(self._FIRST_ITEM_ROW, view.count()):
            item = view.item(row)
            if not item.isHidden():
                content_width = max(content_width, item.sizeHint().width(), 1)
        view_width = max(
            min(width_limit, content_width + margins.left() + margins.right() + 2),
            view.minimumWidth(),
        )

        rows_height = 0
        for row in range(view.count()):
            item = view.item(row)
            if not item.isHidden():
                rows_height += max(1, item.sizeHint().height())
        view_height = min(
            height_limit, rows_height + margins.top() + margins.bottom() + 3
        )
        if view.maxVisibleItems() > 0:
            view_height = min(
                view_height,
                view.maxVisibleItems() * self.itemHeight
                + margins.top()
                + margins.bottom()
                + 3,
            )
        view.setFixedSize(QSize(view_width, view_height))
        self.adjustSize()
        self._place_search()

    def _place_search(self) -> None:
        """Pin the filter box in the top margin strip above the scrolling list."""
        viewport = self.view.viewport()
        self._search.setGeometry(
            viewport.x() + self._SEARCH_SIDE_INSET,
            viewport.y()
            - self.view.viewportMargins().top()
            + self._SEARCH_TOP_INSET,
            max(viewport.width() - 2 * self._SEARCH_SIDE_INSET, 1),
            self._search.height(),
        )

    def _keep_anchored(self) -> None:
        """Keep the open popup attached to its anchor after a filter resize."""
        if not self.isVisible() or self._exec_pos is None or self.aniManager is None:
            return
        if (
            self.aniManager.ani.state()
            == QPropertyAnimation.State.Running
        ):
            return
        self.move(self.aniManager._endPosition(self._exec_pos))


class WheelFocusedFontComboBox(WheelFocusedComboBox):
    """Fluent font picker preserving QFontComboBox's small public contract.

    Long catalogs open with a search box on top of the popup for live
    filtering; short catalogs keep the plain combo popup.
    """

    currentFontChanged = Signal(QFont)

    #: 弹层条目数达到该值才启用筛选框，避免短列表出现无意义的输入框。
    filter_min_items = 12

    #: 后台预热的起始延迟与相邻字体槽之间的错峰间隔（毫秒）。
    _FONT_MENU_WARMUP_BASE_MS = 500
    _FONT_MENU_WARMUP_STAGGER_MS = 150
    _font_menu_warmup_index = 0

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
        # 菜单因「点击组合框」而关闭时置位：同一次点击的 release 会再走
        # _showComboMenu，置位时吞掉这一次，避免菜单被立刻重新弹开。
        self._suppress_combo_reopen = False
        # 长目录筛选菜单跨打开复用（每次重建 500+ 条目要花约 200ms，
        # 是打开瞬间卡顿的主因）；短目录仍走上游的一次性菜单。
        self._cached_font_menu: Optional[_FilterableFontMenu] = None
        self.addItems(tuple(font_families_provider()))
        self.currentIndexChanged.connect(
            lambda _index: self.currentFontChanged.emit(self.currentFont())
        )
        if self.count() >= self.filter_min_items:
            self.setToolTip("展开后可在顶部输入框输入关键字筛选字体")
            self._schedule_font_menu_warmup()

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
            # 字体族未变化时不再补发 currentFontChanged：下游会因此
            # 无差别刷新四组字重下拉（每次改字号都触发，全部在 UI 线程）。
            return
        self.setCurrentIndex(index)

    def _createComboMenu(self):  # noqa: N802 - qfluentwidgets hook
        if self.count() < self.filter_min_items:
            return super()._createComboMenu()
        cached = self._cached_font_menu
        # menuActions() 还包含空提示行（addWidget 也进 _actions），要多算一行
        if (
            cached is not None
            and len(cached.menuActions()) == len(self.items) + cached._FIRST_ITEM_ROW
        ):
            return cached
        if cached is not None:
            # 条目数对不上（理论上构造完成后不会发生）：旧菜单整只换新
            cached.deleteLater()
        self._cached_font_menu = _FilterableFontMenu(self)
        return self._cached_font_menu

    def _ensure_menu_populated(self, menu) -> None:
        """Populate ``menu`` with the catalog rows unless it already has them."""
        row_offset = (
            menu._FIRST_ITEM_ROW if isinstance(menu, _FilterableFontMenu) else 0
        )
        if len(menu.menuActions()) == len(self.items) + row_offset:
            return
        actions = [
            QAction(
                item.icon,
                item.text,
                triggered=lambda _checked, index=i: self._onItemClicked(index),
            )
            for i, item in enumerate(self.items)
        ]
        for action, item in zip(actions, self.items):
            action.setEnabled(item.isEnabled)
        if isinstance(menu, _FilterableFontMenu):
            menu.add_font_actions(actions)
        else:
            for action in actions:
                menu.addAction(action)

    def _schedule_font_menu_warmup(self) -> None:
        """后台预热缓存菜单，让第一次点开也不用现建 500+ 条目。

        定时器挂在本控件之下（控件销毁即取消）；多个字体槽错峰预热，
        避免同时各花几十 ms 把界面卡一下。
        """
        delay_ms = self._FONT_MENU_WARMUP_BASE_MS + (
            type(self)._font_menu_warmup_index * self._FONT_MENU_WARMUP_STAGGER_MS
        )
        type(self)._font_menu_warmup_index += 1
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(self._warm_up_font_menu)
        timer.start(delay_ms)

    def _warm_up_font_menu(self) -> None:
        if self._cached_font_menu is not None:
            return  # 用户已抢先点开过，菜单已在缓存里
        menu = self._createComboMenu()
        self._ensure_menu_populated(menu)

    def _showComboMenu(self) -> None:  # noqa: N802 - qfluentwidgets hook
        """Open the popup, offsetting the default action past the filter rows."""
        if self._suppress_combo_reopen:
            self._suppress_combo_reopen = False
            return
        if not self.items:
            return
        menu = self._createComboMenu()
        self._ensure_menu_populated(menu)

        if menu.view.width() < self.width():
            menu.view.setMinimumWidth(self.width())
            menu.adjustSize()
        menu.setMaxVisibleItems(self.maxVisibleItems())
        if not isinstance(menu, _FilterableFontMenu):
            menu.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        # 缓存菜单会重复走到这里：先断开再连接，保证 closedSignal 只挂一条
        try:
            menu.closedSignal.disconnect(self._onDropMenuClosed)
        except TypeError:
            pass
        menu.closedSignal.connect(self._onDropMenuClosed)
        self.dropMenu = menu

        if 0 <= self.currentIndex() < len(self.items):
            if isinstance(menu, _FilterableFontMenu):
                menu.set_default_item(self.currentIndex())
            else:
                menu.setDefaultAction(menu.actions()[self.currentIndex()])

        # determine the animation type by choosing the maximum height of view
        x = -menu.width()//2 + menu.layout().contentsMargins().left() + self.width()//2
        pd = self.mapToGlobal(QPoint(x, self.height()))
        hd = menu.view.heightForAnimation(pd, MenuAnimationType.DROP_DOWN)

        pu = self.mapToGlobal(QPoint(x, 0))
        hu = menu.view.heightForAnimation(pu, MenuAnimationType.PULL_UP)

        # 弹出方向沿用 DROP_DOWN / PULL_UP 的取舍（含尺寸与落位语义）；
        # 长目录弹层自己不播动画（见 _FilterableFontMenu.exec）。
        if hd >= hu:
            menu.exec(pd, aniType=MenuAnimationType.DROP_DOWN)
        else:
            menu.exec(pu, aniType=MenuAnimationType.PULL_UP)

class _CommitOnReleaseSlider(Slider):
    """Fluent slider with drag-commit-on-release semantics and no wheel input.

    qfluentwidgets 的 Slider 重写了鼠标交互：拖拽的每一帧都直接 ``setValue``
    并发出 ``valueChanged``（与 QSlider 的 ``tracking`` 无关），释放信号也只
    在把手上按起才发。这里接管按压状态，恢复标准滑块语义：
    - 按下即进入 ``sliderDown`` 态，期间值变化只刷新显示，不提交；
    - 释放（含槽点击跳变）统一补发 ``sliderReleased``，由宿主提交一次；
    - 滚轮/触摸板滚动不调值——面板内极易误触，事件交回外层滚动区。
    """

    def wheelEvent(self, event) -> None:  # noqa: N802 - Qt API
        event.ignore()

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt API
        self.setSliderDown(True)
        super().mousePressEvent(event)
        event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().mouseMoveEvent(event)
        event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().mouseReleaseEvent(event)
        if self.isSliderDown():
            self.setSliderDown(False)
            self.sliderReleased.emit()
        event.accept()


class CanvasSliderSpinBox(QWidget):
    """Compact slider paired with an unrestricted precise integer editor.

    The editor owns the real value and keeps its original hard range. The
    slider is only a canvas-scaled visual/dragging range, so out-of-range
    values remain editable while the thumb rests at the nearest endpoint.
    """

    valueChanged = Signal(int)

    def __init__(
        self,
        spin: WheelFocusedSpinBox,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._spin = spin
        self._slider = _CommitOnReleaseSlider(Qt.Orientation.Horizontal, self)
        self._slider.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._slider.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self._spin.setParent(self)
        # 工厂（timing_spin 等）可能给 spin 施加 compact_property_control 的
        # Ignored 水平策略——那是给面板网格用的；放进复合控件的 hbox 里会让
        # 布局把数值框挤出控件边界，这里恢复常规策略。
        self._spin.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed
        )
        # 只设上限：WheelFocusedSpinBox 会按文本内容自适应最小宽度，
        # setFixedWidth 会和 _sync_text_minimum 的 setMinimumWidth 互相覆盖。
        self._spin.setMaximumWidth(92)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addWidget(self._slider, 1)
        layout.addWidget(self._spin, 0)
        # setParent() 隐藏了 spin；不重新 show 的话父控件显示时数值框
        # 会一直保持显式隐藏，只剩滑块可见。
        self._spin.show()

        self.setMinimumWidth(190)
        self.setFixedHeight(self._spin.height())
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        # 拖拽期间只刷新数值显示，松手才提交一次（见 _CommitOnReleaseSlider）；
        # 逐帧提交会在拖动时反复重配 native 渲染器，曾导致 GPU 回退与崩溃。
        self._committed_value = int(self._spin.value())
        self._slider.valueChanged.connect(self._on_slider_value_changed)
        self._slider.sliderMoved.connect(self._on_slider_moved)
        self._slider.sliderReleased.connect(self._commit_slider_value)
        self._spin.valueChanged.connect(self._on_spin_value_changed)

    def value(self) -> int:
        return int(self._spin.value())

    def showEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().showEvent(event)
        self._force_layout_distribution()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(event)
        self._force_layout_distribution()

    def _force_layout_distribution(self) -> None:
        # 控件在隐藏或更宽尺寸阶段（面板默认 640px）布局过一次后，收缩到
        # 真实宽度时 hbox 不会按新宽度重算，数值框会被排到控件外。这里
        # 绕过脏标记，在显示/尺寸变化时直接按当前矩形强制重分配。
        layout = self.layout()
        if layout is not None:
            layout.setGeometry(self.rect())

    def setValue(self, value: int) -> None:  # noqa: N802 - Qt-style compatibility
        self._spin.setValue(int(value))
        self._sync_slider()

    def set_slider_range(self, minimum: int, maximum: int) -> None:
        minimum = int(minimum)
        maximum = max(int(maximum), minimum)
        blocked = self._slider.blockSignals(True)
        try:
            self._slider.setRange(minimum, maximum)
            self._slider.setValue(max(min(self.value(), maximum), minimum))
        finally:
            self._slider.blockSignals(blocked)
        self._sync_handle_position()

    def slider_range(self) -> tuple[int, int]:
        return self._slider.minimum(), self._slider.maximum()

    def input_range(self) -> tuple[int, int]:
        return self._spin.minimum(), self._spin.maximum()

    def minimum(self) -> int:
        return int(self._spin.minimum())

    def maximum(self) -> int:
        return int(self._spin.maximum())

    def setToolTip(self, text: str) -> None:  # noqa: N802 - Qt API
        super().setToolTip(text)
        self._slider.setToolTip(text)
        self._spin.setToolTip(text)

    def _on_slider_value_changed(self, _value: int) -> None:
        # 拖拽/按压期间（sliderDown）只刷新显示不提交；键盘等非按压值变化
        # 没有释放信号，立即提交。
        if not self._slider.isSliderDown():
            self._commit_slider_value()
    def _on_slider_moved(self, value: int) -> None:
        blocked = self._spin.blockSignals(True)
        try:
            self._spin.setValue(int(value))
        finally:
            self._spin.blockSignals(blocked)

    def _commit_slider_value(self) -> None:
        value = int(self._slider.value())
        blocked = self._spin.blockSignals(True)
        try:
            self._spin.setValue(value)
        finally:
            self._spin.blockSignals(blocked)
        if value != self._committed_value:
            self._committed_value = value
            self.valueChanged.emit(value)

    def _on_spin_value_changed(self, value: int) -> None:
        self._committed_value = int(value)
        self._sync_slider()
        self.valueChanged.emit(int(value))

    def _sync_slider(self) -> None:
        minimum, maximum = self.slider_range()
        clamped = max(min(self.value(), maximum), minimum)
        blocked = self._slider.blockSignals(True)
        try:
            self._slider.setValue(clamped)
        finally:
            self._slider.blockSignals(blocked)
        self._sync_handle_position()

    def _sync_handle_position(self) -> None:
        # qfluentwidgets 的把手是子控件，位置只随 valueChanged 信号刷新；
        # 阻塞信号更新值后必须手动补一次，否则输入数值时把手不跟随。
        adjust = getattr(self._slider, "_adjustHandlePos", None)
        if callable(adjust):
            adjust()
