"""Preview-window visibility state independent from the main subtitle window."""

from __future__ import annotations

from typing import Any


class PreviewWindowController:
    """Own preview request, context hiding, pause, and restore semantics."""

    def __init__(self) -> None:
        self.requested = False
        self.reposition_on_next_show = True

    @staticmethod
    def context_allowed(
        *,
        host_visible: bool,
        preview_tab_active: bool,
        exporting: bool,
    ) -> bool:
        return bool(host_visible and preview_tab_active and not exporting)

    @staticmethod
    def hide(preview_window: Any, transport: Any) -> None:
        transport.pause()
        if preview_window.isVisible():
            preview_window.hide()

    def sync(
        self,
        preview_window: Any,
        transport: Any,
        *,
        context_allowed: bool,
    ) -> None:
        should_show = self.requested and bool(context_allowed)
        if not should_show:
            self.hide(preview_window, transport)
            return
        if preview_window.isVisible():
            return
        if self.reposition_on_next_show:
            preview_window.show_near_workspace()
            self.reposition_on_next_show = False
        else:
            preview_window.show()
            preview_window.show_controls()

    def request(
        self,
        preview_window: Any,
        transport: Any,
        *,
        context_allowed: bool,
    ) -> None:
        self.requested = True
        self.sync(
            preview_window,
            transport,
            context_allowed=context_allowed,
        )

    def user_closed(self) -> None:
        self.requested = False
        self.reposition_on_next_show = True

    def show_and_activate(
        self,
        preview_window: Any,
        transport: Any,
        *,
        context_allowed: bool,
    ) -> None:
        self.request(
            preview_window,
            transport,
            context_allowed=context_allowed,
        )
        self.activate_visible(preview_window)

    @staticmethod
    def activate_visible(preview_window: Any) -> None:
        """Restore and activate an already requested visible preview window."""
        if not preview_window.isVisible():
            return
        if preview_window.is_collapsed():
            preview_window._restore_from_collapsed()
        preview_window.raise_()
        preview_window.activateWindow()
