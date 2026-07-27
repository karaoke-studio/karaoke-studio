"""Host-side compatibility fixes for PyQt6-Fluent-Widgets."""

from __future__ import annotations

from typing import Callable


_PATCH_MARKER = "_krok_menu_lifetime_safe"
_TOOLTIP_PATCH_MARKER = "_krok_parentless_tooltip"
_TOOLTIP_SHADOW_PATCH_MARKER = "_krok_slim_tooltip_shadow"
_manual_tooltips = {}


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


def install_fluent_tooltip(widget, show_delay: int = 300, position=None) -> None:
    """Install qfluentwidgets' themed tooltip filter on a widget."""

    from qfluentwidgets import ToolTipFilter, ToolTipPosition

    apply_qfluent_tooltip_parent_patch()
    if position is None:
        position = ToolTipPosition.TOP
    widget.installEventFilter(ToolTipFilter(widget, show_delay, position))


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
