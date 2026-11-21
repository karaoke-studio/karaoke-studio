"""Latest-request Skia preview using the existing G5 shared-memory transport."""
from __future__ import annotations

import threading
import uuid

from PyQt6.QtCore import QObject, QTimer, pyqtSignal
from PyQt6.QtGui import QGuiApplication, QImage

from krok_helper.subtitle_render.native.backend import SharedFrameRingReader, NativeRendererError
from krok_helper.subtitle_render.native.skia_backend import SkiaRendererProcess, SkiaSelection
from krok_helper.subtitle_render.native.skia_fonts import SkiaFontNotPreparedError
from krok_helper.subtitle_render.engine.text.font_capabilities import capabilities_generation


class SkiaAsyncSubtitleRenderer(QObject):
    frame_ready = pyqtSignal(QImage, int)
    renderProgress = pyqtSignal(int, str)
    backendModeChanged = pyqtSignal(str)
    renderError = pyqtSignal(str)
    supports_font_preparation_steps = True

    def __init__(self, width, height, parent=None, *, gpu_enabled=True):
        super().__init__(parent)
        self.selection = SkiaSelection.from_environment(gpu_enabled=gpu_enabled)
        self._condition = threading.Condition()
        self._target = (int(width), int(height), 1.0)
        self._state = None
        self._version = 0
        self._pending = None
        self._last_request = None
        self._prepared_generation = None  # Font generation the state was GUI-prepared at.
        self._font_change_pending = False  # A fontDatabaseChanged re-preparation is queued.
        self._stopped = False
        self._renderer = None
        self.current_backend_mode = None
        self.backend_label = "Skia 准备中"
        self._thread = threading.Thread(target=self._run, name="subtitle-preview-skia", daemon=True)
        self._thread.start()
        # Workers only read GUI-prepared font resources. A registry change
        # makes them stale even when track/style objects are unchanged.
        # Owned, coalescing timer: a burst of registry changes prepares once,
        # after the font-capability cache clear (another direct slot) has run,
        # and nothing fires once this renderer is stopped or destroyed.
        self._font_timer = QTimer(self)
        self._font_timer.setSingleShot(True)
        self._font_timer.setInterval(0)
        self._font_timer.timeout.connect(self._reprepare_fonts)
        app = QGuiApplication.instance()
        if app is not None:
            app.fontDatabaseChanged.connect(self._font_database_changed)

    def _font_database_changed(self):
        with self._condition:
            if self._stopped:
                return
            self._font_change_pending = True
        self._font_timer.start()

    def _reprepare_fonts(self):
        from krok_helper.subtitle_render.engine.text.font_preparation import prepare_fonts
        with self._condition:
            state, stopped = self._state, self._stopped
            if stopped or not state or state[0] is None or state[1] is None:
                self._font_change_pending = False
                return
        try:
            # GUI thread and synchronous: no set_state can interleave here.
            prepare_fonts(state[0], state[1], state[2])
        finally:
            with self._condition:
                self._font_change_pending = False
        with self._condition:
            if self._stopped:
                return
            # Always publish a new version: this also covers a state the host
            # committed while the registry changed during its own preparation.
            self._prepared_generation = capabilities_generation()
            self._version += 1
            if self._last_request is not None and self._pending is None:
                self._pending = self._last_request
            self._condition.notify()

    def set_state(self, track, style, extra_tracks=None, *, duration_ms=None, relayout_scope=None, roll_shift_ms=0):
        with self._condition:
            self._state = (track, style, list(extra_tracks or []), duration_ms, relayout_scope, roll_shift_ms)
            self._prepared_generation = capabilities_generation()  # Owners prepare before set_state.
            self._version += 1

    def set_render_target(self, width, height, device_pixel_ratio=1.0):
        target = (max(int(width), 1), max(int(height), 1), max(float(device_pixel_ratio), 0.1))
        with self._condition:
            if target != self._target:
                self._target = target
                self._version += 1

    def set_size(self, width, height):
        self.set_render_target(width, height, self._target[2])

    def set_playing(self, playing):
        pass  # Timing remains authoritative in the existing player/request clock.

    def request(self, t_ms):
        with self._condition:
            if not self._stopped:
                self._pending = self._last_request = int(t_ms)
                self._condition.notify()

    def stop(self):
        app = QGuiApplication.instance()
        if app is not None:
            try:
                app.fontDatabaseChanged.disconnect(self._font_database_changed)
            except TypeError:
                pass
        self._font_timer.stop()
        # The thread can finish native cleanup after its old view is closed.
        self.setParent(None)
        with self._condition:
            self._stopped = True
            self._state = self._pending = self._last_request = None
            self._condition.notify_all()
            renderer = self._renderer
        if renderer is not None:
            renderer.abort(reason="Skia preview stopped")
        self._thread.join(timeout=3)
        if not self._thread.is_alive():
            self.deleteLater()

    def _run(self):
        renderer = None
        configured_version = -1
        failed_version = -1
        generation = 0
        try:
            while True:
                with self._condition:
                    self._condition.wait_for(lambda: self._stopped or self._pending is not None)
                    if self._stopped:
                        return
                    t_ms, self._pending = self._pending, None
                    state, target, version = self._state, self._target, self._version
                    font_generation = self._prepared_generation
                if not state or state[0] is None or state[1] is None or version == failed_version:
                    continue
                try:
                    if renderer is None:
                        renderer = SkiaRendererProcess(selection=self.selection, response_timeout_s=5)
                        with self._condition:
                            if self._stopped:
                                return
                            self._renderer = renderer
                        renderer.start()
                    if configured_version != version:
                        track, style, extras, duration, scope, roll_shift = state
                        self.renderProgress.emit(0, "Skia 字幕准备")
                        renderer.configure(track, style, width=target[0], height=target[1], dpr=target[2], fps=60,
                                           extra_tracks=extras, duration_ms=duration, relayout_scope=scope,
                                           roll_shift_ms=roll_shift or 0)
                        configured_version = version
                        info = renderer.skia_info
                        self.current_backend_mode = "gpu" if info["execution_target"] == "gpu" else "cpu"
                        self.backend_label = "Skia " + (info["api"].capitalize() + " GPU" if self.current_backend_mode == "gpu" else "Raster CPU")
                        if info.get("fallback_reason"):
                            self.renderProgress.emit(100, "Skia GPU 初始化失败，已使用 Skia Raster：" + info["fallback_reason"])
                        self.backendModeChanged.emit(self.current_backend_mode)
                    generation += 1
                    renderer.start_render_range([t_ms], generation=generation, threads=1,
                                                shm_key="krok-skia-preview-" + uuid.uuid4().hex, ring_slots=1)
                    image = None
                    while True:
                        event = renderer.read_event()
                        if not event.get("ok"):
                            raise NativeRendererError(str(event.get("error") or event))
                        if event.get("generation") != generation:
                            continue
                        if event.get("event") == "frame_ready":
                            with SharedFrameRingReader.from_event(event) as reader:
                                slot = reader.read_frame(event)
                                image = QImage(slot.payload, slot.width, slot.height, slot.stride, QImage.Format.Format_RGBA8888).copy()
                                image.setDevicePixelRatio(target[2])
                        elif event.get("event") == "range_done":
                            break
                    if image is None:
                        raise NativeRendererError("Skia 字幕帧缺失")
                    with self._condition:
                        valid = (not self._stopped and version == self._version
                                 and not self._font_change_pending
                                 and font_generation == capabilities_generation())
                    if valid:
                        image.setText("preview_backend", "skia_" + self.current_backend_mode)
                        self.frame_ready.emit(image, t_ms)
                        self.renderProgress.emit(100, "Skia 字幕已更新")
                except Exception as exc:
                    if renderer is not None:
                        renderer.close()
                    renderer = None
                    with self._condition:
                        self._renderer = None
                        valid = not self._stopped and version == self._version
                        # The registry changed after GUI preparation: the
                        # fontDatabaseChanged re-preparation supersedes this.
                        superseded = isinstance(exc, SkiaFontNotPreparedError) and (
                            self._font_change_pending
                            or capabilities_generation() != self._prepared_generation)
                    failed_version = version
                    if superseded:
                        continue
                    if valid:
                        self.backend_label = "Skia 错误"
                        self.renderError.emit("Skia 字幕渲染失败：" + str(exc))
        finally:
            try:
                if renderer is not None:
                    renderer.close()
            finally:
                with self._condition:
                    self._renderer = None
                    if self._stopped:
                        # A worker may outlive stop() while it finishes native cleanup.
                        self.deleteLater()
