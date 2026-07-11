"""Host-side compatibility fixes for PyQt6-Fluent-Widgets."""

from __future__ import annotations

from typing import Callable


_PATCH_MARKER = "_krok_menu_lifetime_safe"


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

