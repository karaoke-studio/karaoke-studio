"""Focused contracts for preview-window visibility orchestration."""

from __future__ import annotations

from krok_helper.subtitle_render.frontend.preview_controller import (
    PreviewPreferenceController,
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


class _PreviewPanel:
    def __init__(self, *, gpu_supported: bool = True) -> None:
        self.gpu_supported = gpu_supported
        self.gpu_requests: list[bool] = []
        self.quality_requests: list[str] = []

    def set_gpu_preview_enabled(self, enabled: bool) -> bool:
        self.gpu_requests.append(enabled)
        return self.gpu_supported

    def set_preview_quality(self, quality: str) -> None:
        self.quality_requests.append(quality)


class _CheckBox:
    def __init__(self) -> None:
        self.checked = True
        self.signals_blocked = False
        self.block_calls: list[bool] = []

    def blockSignals(self, blocked: bool) -> bool:
        previous = self.signals_blocked
        self.signals_blocked = blocked
        self.block_calls.append(blocked)
        return previous

    def setChecked(self, checked: bool) -> None:
        self.checked = checked


def _preference_controller(*, gpu_supported: bool = True):
    panel = _PreviewPanel(gpu_supported=gpu_supported)
    checkbox = _CheckBox()
    preferences: dict[str, object] = {}
    events: list[object] = []
    controller = PreviewPreferenceController(
        preview_panel=panel,
        gpu_checkbox=checkbox,
        local_output_preferences=preferences,
        save_persisted_state=lambda: events.append("save"),
        warn_gpu_unavailable=lambda: events.append("unavailable"),
        warn_gpu_fallback=lambda message: events.append(("fallback", message)),
    )
    return controller, panel, checkbox, preferences, events


def test_preview_preferences_apply_supported_gpu_request_and_persist() -> None:
    controller, panel, checkbox, _preferences, events = _preference_controller()

    controller.apply_gpu_enabled(True)

    assert panel.gpu_requests == [True]
    assert checkbox.checked is True
    assert checkbox.block_calls == []
    assert events == ["save"]


def test_preview_preferences_revert_unsupported_gpu_request_without_signal() -> None:
    controller, panel, checkbox, _preferences, events = _preference_controller(
        gpu_supported=False
    )

    controller.apply_gpu_enabled(True)

    assert panel.gpu_requests == [True]
    assert checkbox.checked is False
    assert checkbox.block_calls == [True, False]
    assert checkbox.signals_blocked is False
    assert events == ["unavailable", "save"]


def test_preview_preferences_normalize_quality_and_keep_it_local() -> None:
    controller, panel, _checkbox, preferences, events = _preference_controller()

    normalized = controller.apply_quality("unknown")

    assert normalized == "high"
    assert panel.quality_requests == ["high"]
    assert preferences == {"preview_quality": "high"}
    assert events == ["save"]


def test_preview_preferences_forward_runtime_fallback_message() -> None:
    controller, _panel, _checkbox, _preferences, events = _preference_controller()

    controller.report_gpu_fallback(123)

    assert events == [("fallback", "123")]


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
