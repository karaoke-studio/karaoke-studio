"""Preview-window visibility state independent from the main subtitle window."""

from __future__ import annotations

from collections.abc import Callable, MutableMapping
from typing import Any

from krok_helper.subtitle_render.engine.timing.timeline import track_duration_ms
from krok_helper.subtitle_render.frontend.preview.preview_async import (
    normalize_preview_quality,
)


class PreviewDurationController:
    """Resolve and publish the shared preview timeline duration."""

    @staticmethod
    def resolve_duration_ms(
        tracks: Any,
        *,
        video_info: Any = None,
        audio_info: Any = None,
    ) -> int:
        candidates = [track_duration_ms(track) for track in tracks]
        for media_info in (video_info, audio_info):
            if media_info is not None and media_info.duration > 0:
                candidates.append(int(media_info.duration * 1000))
        return max(candidates, default=0)

    def refresh(
        self,
        *,
        tracks: Any,
        video_info: Any,
        audio_info: Any,
        tracks_view: Any,
        preview_panel: Any,
        transport_bar: Any,
    ) -> int:
        duration = self.resolve_duration_ms(
            tracks,
            video_info=video_info,
            audio_info=audio_info,
        )
        tracks_view.set_duration(duration)
        preview_panel.set_duration(duration)
        if duration > 0:
            transport_bar.set_duration(duration)
        return duration


class PreviewPreferenceController:
    """Coordinate preview-only preferences without owning concrete UI widgets."""

    def __init__(
        self,
        *,
        preview_panel: Any,
        gpu_checkbox: Any,
        local_output_preferences: MutableMapping[str, Any],
        save_persisted_state: Callable[[], None],
        warn_gpu_unavailable: Callable[[], None],
        warn_gpu_fallback: Callable[[str], None],
    ) -> None:
        self._preview_panel = preview_panel
        self._gpu_checkbox = gpu_checkbox
        self._local_output_preferences = local_output_preferences
        self._save_persisted_state = save_persisted_state
        self._warn_gpu_unavailable = warn_gpu_unavailable
        self._warn_gpu_fallback = warn_gpu_fallback

    def apply_gpu_enabled(self, enabled: bool) -> None:
        """Apply a GPU request, reverting the toggle when unsupported."""
        if not self._preview_panel.set_gpu_preview_enabled(bool(enabled)):
            blocked = self._gpu_checkbox.blockSignals(True)
            try:
                self._gpu_checkbox.setChecked(False)
            finally:
                self._gpu_checkbox.blockSignals(blocked)
            self._warn_gpu_unavailable()
        self._save_persisted_state()

    def apply_quality(self, quality: object) -> str:
        """Apply and persist a normalized preview-only raster quality."""
        normalized = normalize_preview_quality(quality)
        self._preview_panel.set_preview_quality(normalized)
        self._local_output_preferences["preview_quality"] = normalized
        self._save_persisted_state()
        return normalized

    def report_gpu_fallback(self, message: object) -> None:
        """Forward a runtime GPU fallback through the host notification boundary."""
        self._warn_gpu_fallback(str(message))


class PreviewWindowController:
    """Own preview request, context hiding, pause, and restore semantics."""

    def __init__(self) -> None:
        self.requested = False
        self.reposition_on_next_show = True
        # 预览窗生命周期（切页 / 关窗 / 后端重启）里播放头可能被别处的复位
        # 逻辑清零；记住离开时的位置，重新出现时补回去。
        self._resume_position_ms = 0

    @staticmethod
    def context_allowed(
        *,
        host_visible: bool,
        preview_tab_active: bool,
        exporting: bool,
    ) -> bool:
        return bool(host_visible and preview_tab_active and not exporting)

    def hide(self, preview_window: Any, transport: Any) -> None:
        transport.pause()
        self.remember_position(transport)
        if preview_window.isVisible():
            preview_window.hide()

    def remember_position(self, transport: Any) -> None:
        """Capture the preview playhead before a lifecycle hide or close."""
        getter = getattr(transport, "current_time_ms", None)
        if getter is None:
            return
        try:
            self._resume_position_ms = max(int(getter), 0)
        except (TypeError, ValueError):
            return

    def restore_position(self, transport: Any) -> bool:
        """Re-seek a transport that a lifecycle reset zeroed while hidden.

        Only an explicit zero is treated as a reset: a non-zero playhead means
        the position survived (or the user moved it), so it is left alone.
        """
        remembered = self._resume_position_ms
        if remembered <= 0:
            return False
        getter = getattr(transport, "current_time_ms", None)
        setter = getattr(transport, "set_time", None)
        if getter is None or setter is None:
            return False
        try:
            current = int(getter)
        except (TypeError, ValueError):
            return False
        if current != 0:
            return False
        setter(remembered)
        return True

    def forget_position(self) -> None:
        """Drop the remembered playhead when the timeline itself changes."""
        self._resume_position_ms = 0

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
        self.restore_position(transport)
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
