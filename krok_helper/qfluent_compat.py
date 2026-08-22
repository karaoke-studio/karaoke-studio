"""Host-side compatibility fixes for PyQt6-Fluent-Widgets."""

from __future__ import annotations

from typing import Callable

from PyQt6.QtCore import QEventLoop, Qt, QTimer
from PyQt6.QtWidgets import QDialog, QWidget
from qfluentwidgets import Dialog


_PATCH_MARKER = "_krok_menu_lifetime_safe"
_TOOLTIP_PATCH_MARKER = "_krok_parentless_tooltip"
_TOOLTIP_SHADOW_PATCH_MARKER = "_krok_slim_tooltip_shadow"
_FLUENT_TOOLTIP_FILTER_ATTRIBUTE = "_strange_uta_game_fluent_tooltip_filter"
_manual_tooltips = {}
_modeless_dialogs: set[QDialog] = set()


def _prepare_modeless_dialog(dialog: QDialog) -> None:
    """Keep a dialog top-level and interactive without disabling the workbench."""

    dialog.setModal(False)
    dialog.setWindowModality(Qt.WindowModality.NonModal)
    dialog.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)


def show_modeless_dialog(dialog: QDialog) -> QDialog:
    """Show a dialog without a mask and retain it until the user closes it."""

    _prepare_modeless_dialog(dialog)
    _modeless_dialogs.add(dialog)

    def release(*_args) -> None:
        _modeless_dialogs.discard(dialog)

    def release_finished(*_args) -> None:
        release()
        dialog.deleteLater()

    dialog.finished.connect(release_finished)
    dialog.destroyed.connect(release)
    dialog.show()
    dialog.raise_()
    dialog.activateWindow()
    return dialog


def exec_modeless_dialog(dialog: QDialog) -> int:
    """Wait for a modeless dialog while keeping the whole workbench usable.

    This preserves the return-value contract of the old ``QDialog.exec()``
    call sites without enabling Qt modality or a Fluent mask.  The nested event
    loop only pauses the caller; other pages and the main window keep receiving
    input.
    """

    _prepare_modeless_dialog(dialog)
    loop = QEventLoop()
    result = int(QDialog.DialogCode.Rejected)
    finished = False

    def finish(code: int = int(QDialog.DialogCode.Rejected)) -> None:
        nonlocal result, finished
        result = int(code)
        finished = True
        if loop.isRunning():
            loop.quit()

    dialog.finished.connect(finish)
    dialog.destroyed.connect(loop.quit)
    dialog.show()
    dialog.raise_()
    dialog.activateWindow()
    if dialog.isVisible() and not finished:
        loop.exec()
    return result


class ModelessDialog(QDialog):
    """QDialog whose synchronous API does not disable the workbench."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        _prepare_modeless_dialog(self)

    def exec(self) -> int:
        return exec_modeless_dialog(self)


class HostFluentMessageDialog(Dialog):
    """Modeless Fluent dialog for pages embedded in the workbench window.

    ``qfluentwidgets.MessageBox`` is itself a child-sized mask dialog.  In the
    workbench's stacked-page hierarchy that mask can remain above its content
    and consume mouse input.  A real top-level ``Dialog`` without Qt modality
    or a dim layer keeps both the dialog and the rest of the workbench usable.
    """

    def __init__(self, title: str, content: str, parent=None) -> None:
        anchor = resolve_fluent_dialog_parent(parent)
        super().__init__(title, content, anchor)
        self.setTitleBarVisible(False)
        _prepare_modeless_dialog(self)

    def _ensure_active(self) -> None:
        self.raise_()
        self.activateWindow()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._ensure_active()
        QTimer.singleShot(0, self._ensure_active)

    def exec(self) -> int:
        return exec_modeless_dialog(self)


def apply_qfluent_menu_lifetime_patch() -> None:
    """Ignore animation frames delivered after a Fluent menu was destroyed.

    PyQt6-Fluent-Widgets 1.11.2 makes combo menus ``WA_DeleteOnClose`` while
    their popup ``QPropertyAnimation`` can still emit ``valueChanged``.  Fast
    clicks can therefore call ``_updateMenuViewport`` with an already deleted
    ``MenuActionListWidget`` and terminate the application from the Qt slot.
    """

    from PyQt6 import sip
    from qfluentwidgets.components.widgets.menu import MenuAnimationManager

    current: Callable = MenuAnimationManager._updateMenuViewport
    if getattr(current, _PATCH_MARKER, False):
        return

    original = current

    def _update_menu_viewport_if_alive(self) -> None:
        menu = getattr(self, "menu", None)
        if menu is None or sip.isdeleted(menu):
            return
        view = getattr(menu, "view", None)
        if view is None or sip.isdeleted(view):
            return
        try:
            original(self)
        except RuntimeError:
            # Qt can delete the viewport between the checks above and the
            # original method's two C++ calls.  Suppress only that race.
            if sip.isdeleted(menu) or sip.isdeleted(view):
                return
            viewport = getattr(view, "viewport", lambda: None)()
            if viewport is None or sip.isdeleted(viewport):
                return
            raise

    setattr(_update_menu_viewport_if_alive, _PATCH_MARKER, True)
    setattr(_update_menu_viewport_if_alive, "_krok_original", original)
    MenuAnimationManager._updateMenuViewport = _update_menu_viewport_if_alive


def apply_qfluent_tooltip_parent_patch() -> None:
    """Keep Fluent tooltips out of host-level ``QWidget`` stylesheet cascades.

    qfluentwidgets parents ``ToolTipFilter`` popups to ``parent().window()``.
    The workbench stylesheet intentionally lives on the main window, so broad
    host selectors can cascade into these transient popup widgets and paint an
    extra background around the Fluent tooltip card.  Parentless tooltips keep
    qfluentwidgets' own transparent top-level popup behavior intact.
    """

    from qfluentwidgets import ToolTip
    from qfluentwidgets.components.widgets.tool_tip import ToolTipFilter

    current_init: Callable = ToolTip.__init__
    if not getattr(current_init, _TOOLTIP_SHADOW_PATCH_MARKER, False):
        original_init = current_init

        def _slim_tooltip_init(self, *args, **kwargs):
            original_init(self, *args, **kwargs)
            self.layout().setContentsMargins(1, 1, 1, 2)
            self.shadowEffect.setBlurRadius(2)
            self.shadowEffect.setOffset(0, 1)

        setattr(_slim_tooltip_init, _TOOLTIP_SHADOW_PATCH_MARKER, True)
        setattr(_slim_tooltip_init, "_krok_original", original_init)
        ToolTip.__init__ = _slim_tooltip_init

    current: Callable = ToolTipFilter._createToolTip
    if getattr(current, _TOOLTIP_PATCH_MARKER, False):
        return

    def _create_parentless_tooltip(self):
        return ToolTip(self.parent().toolTip())

    setattr(_create_parentless_tooltip, _TOOLTIP_PATCH_MARKER, True)
    setattr(_create_parentless_tooltip, "_krok_original", current)
    ToolTipFilter._createToolTip = _create_parentless_tooltip


def install_fluent_tooltip(widget, show_delay: int = 300, position=None):
    """Install one independently timed Fluent tooltip filter on ``widget``.

    The attribute name intentionally matches SUG's application-wide manager.
    Whichever side installs first therefore owns the sole filter, avoiding a
    second timer when the embedded editor initializes later.
    """

    from qfluentwidgets import ToolTipFilter, ToolTipPosition

    apply_qfluent_tooltip_parent_patch()
    if position is None:
        position = ToolTipPosition.TOP
    existing = getattr(widget, _FLUENT_TOOLTIP_FILTER_ATTRIBUTE, None)
    if existing is not None:
        existing.setToolTipDelay(show_delay)
        existing.position = position
        return existing
    tooltip_filter = ToolTipFilter(widget, show_delay, position)
    widget.installEventFilter(tooltip_filter)
    setattr(widget, _FLUENT_TOOLTIP_FILTER_ATTRIBUTE, tooltip_filter)
    return tooltip_filter


def show_fluent_tooltip(
    text: str,
    *,
    parent=None,
    global_pos=None,
    duration: int = 1600,
    position=None,
) -> None:
    """Show a short qfluentwidgets tooltip, replacing native ``QToolTip``."""

    if not text:
        hide_fluent_tooltip(parent=parent)
        return

    from qfluentwidgets import ToolTip, ToolTipPosition

    apply_qfluent_tooltip_parent_patch()
    key = id(parent) if parent is not None else 0
    tooltip = _manual_tooltips.get(key)
    if tooltip is None:
        tooltip = ToolTip(text)
        _manual_tooltips[key] = tooltip
        tooltip.destroyed.connect(
            lambda _obj=None, tooltip_key=key: _manual_tooltips.pop(tooltip_key, None)
        )
        if parent is not None:
            try:
                parent.destroyed.connect(
                    lambda _obj=None, tooltip_key=key: _manual_tooltips.pop(tooltip_key, None)
                )
            except RuntimeError:
                _manual_tooltips.pop(key, None)
                return
    else:
        tooltip.setText(text)
    tooltip.setDuration(duration)

    if global_pos is not None:
        tooltip.move(global_pos)
    elif parent is not None:
        tooltip.adjustPos(parent, position or ToolTipPosition.TOP)
    tooltip.show()


def hide_fluent_tooltip(*, parent=None) -> None:
    """Hide a tooltip shown by :func:`show_fluent_tooltip`."""

    key = id(parent) if parent is not None else 0
    tooltip = _manual_tooltips.get(key)
    if tooltip is not None:
        try:
            tooltip.hide()
        except RuntimeError:
            _manual_tooltips.pop(key, None)


def resolve_fluent_dialog_parent(parent):
    """把控件解析成它的顶层窗口，供遮罩式 Fluent 对话框使用。

    ``MessageBox`` / ``MessageBoxBase`` 都继承 ``MaskDialogBase``：遮罩只覆盖传入
    的 parent。直接传内层页面（比如 QStackedWidget 里的某一页）的话，遮罩盖不住
    导航栏，视觉上像是弹窗漏在半个界面上；而且 parent 为 ``None`` 时构造会直接崩
    （内部要访问 ``parent.width()``）。这里统一升到顶层窗口，并对 None 兜底。
    """

    from PyQt6.QtWidgets import QApplication

    if parent is not None:
        try:
            window = parent.window()
            if window is not None:
                return window
        except (AttributeError, RuntimeError):
            pass

    app = QApplication.instance()
    if app is None:
        return parent
    active = app.activeWindow()
    if active is not None:
        return active
    return next((widget for widget in app.topLevelWidgets() if widget.isVisible()), parent)


def show_fluent_info(parent, text: str, *, title: str = "", yes_text: str = "确定") -> None:
    """以 Fluent 风格弹一个只有确认按钮的提示框（替代 ``QMessageBox.information``）。"""

    from krok_helper.config import APP_TITLE

    box = HostFluentMessageDialog(title or APP_TITLE, text, parent)
    box.yesButton.setText(yes_text)
    box.cancelButton.hide()
    show_modeless_dialog(box)


def show_fluent_error(parent, text: str, *, title: str = "", yes_text: str = "确定") -> None:
    """以 Fluent 风格弹一个报错框（替代 ``QMessageBox.critical``）。

    走 :func:`show_modeless_dialog`：**不加遮罩、不开 Qt 模态、也不起嵌套事件
    循环**。带遮罩的 ``MessageBox`` 在工作台的堆叠页层级里会盖住内容并吃掉鼠标
    事件，整个界面看着就像卡死了；嵌套循环则会让调用方停在原地。报错本身只是
    告知，调用处一般紧接着 ``return``，不需要等用户点确认。
    """

    from krok_helper.config import APP_TITLE

    box = HostFluentMessageDialog(title or APP_TITLE, text, parent)
    box.yesButton.setText(yes_text)
    box.cancelButton.hide()
    show_modeless_dialog(box)


def show_fluent_warning(parent, text: str, *, title: str = "", yes_text: str = "确定") -> None:
    """以 Fluent 风格弹一个警告框（替代 ``QMessageBox.warning``）。"""

    show_fluent_error(parent, text, title=title, yes_text=yes_text)


def ask_fluent_confirm(
    parent,
    text: str,
    *,
    title: str = "",
    yes_text: str,
    cancel_text: str = "取消",
) -> bool:
    """以 Fluent 风格弹确认框（替代 ``QMessageBox.question``）；点 ``yes_text`` 返回 True。"""

    from krok_helper.config import APP_TITLE

    box = HostFluentMessageDialog(title or APP_TITLE, text, parent)
    box.yesButton.setText(yes_text)
    box.cancelButton.setText(cancel_text)
    return bool(box.exec())
