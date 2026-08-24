"""Focused contracts for preview-window visibility orchestration."""

from __future__ import annotations

from krok_helper.subtitle_render.frontend.preview_controller import (
    PreviewWindowController,
)


class _PreviewWindow:
    def __init__(self) -> None:
        self.visible = False
        self.collapsed = False
        self.calls: list[str] = []

    def isVisible(self) -> bool:
        return self.visible

    def show_near_workspace(self) -> None:
        self.calls.append("show_near_workspace")
        self.visible = True

    def show(self) -> None:
        self.calls.append("show")
        self.visible = True

    def show_controls(self) -> None:
        self.calls.append("show_controls")

    def hide(self) -> None:
        self.calls.append("hide")
        self.visible = False

    def is_collapsed(self) -> bool:
        return self.collapsed

    def _restore_from_collapsed(self) -> None:
        self.calls.append("restore")
        self.collapsed = False

    def raise_(self) -> None:
        self.calls.append("raise")

    def activateWindow(self) -> None:
        self.calls.append("activate")


class _Transport:
    def __init__(self) -> None:
        self.pauses = 0

    def pause(self) -> None:
        self.pauses += 1


def test_preview_controller_defers_request_until_context_is_allowed() -> None:
    controller = PreviewWindowController()
    window = _PreviewWindow()
    transport = _Transport()

    controller.request(window, transport, context_allowed=False)

    assert controller.requested is True
    assert window.visible is False
    assert transport.pauses == 1

    controller.sync(window, transport, context_allowed=True)

    assert window.visible is True
    assert window.calls == ["show_near_workspace"]
    assert controller.reposition_on_next_show is False


def test_preview_controller_hides_and_restores_without_repositioning() -> None:
    controller = PreviewWindowController()
    window = _PreviewWindow()
    transport = _Transport()
    controller.request(window, transport, context_allowed=True)

    controller.sync(window, transport, context_allowed=False)
    controller.sync(window, transport, context_allowed=True)

    assert transport.pauses == 1
    assert window.calls == [
        "show_near_workspace",
        "hide",
        "show",
        "show_controls",
    ]


def test_preview_controller_user_close_clears_request_and_resets_position() -> None:
    controller = PreviewWindowController()
    controller.requested = True
    controller.reposition_on_next_show = False

    controller.user_closed()

    assert controller.requested is False
    assert controller.reposition_on_next_show is True


def test_preview_controller_restores_collapsed_window_before_activation() -> None:
    controller = PreviewWindowController()
    window = _PreviewWindow()
    window.visible = True
    window.collapsed = True
    transport = _Transport()

    controller.show_and_activate(window, transport, context_allowed=True)

    assert controller.requested is True
    assert window.calls == ["restore", "raise", "activate"]


def test_preview_context_contract_rejects_hidden_export_or_non_preview() -> None:
    allowed = PreviewWindowController.context_allowed

    assert allowed(host_visible=True, preview_tab_active=True, exporting=False) is True
    assert allowed(host_visible=False, preview_tab_active=True, exporting=False) is False
    assert allowed(host_visible=True, preview_tab_active=False, exporting=False) is False
    assert allowed(host_visible=True, preview_tab_active=True, exporting=True) is False
