"""Latest-request Skia preview using the existing G5 shared-memory transport."""
from __future__ import annotations

import threading
import uuid

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtGui import QImage

from krok_helper.subtitle_render.native.backend import SharedFrameRingReader, NativeRendererError
from krok_helper.subtitle_render.native.skia_backend import SkiaRendererProcess, SkiaSelection


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
        self._stopped = False
        self._renderer = None
        self.current_backend_mode = None
        self.backend_label = "Skia 准备中"
        self._thread = threading.Thread(target=self._run, name="subtitle-preview-skia", daemon=True)
        self._thread.start()

    def set_state(self, track, style, extra_tracks=None, *, duration_ms=None, relayout_scope=None, roll_shift_ms=0):
        with self._condition:
            self._state = (track, style, list(extra_tracks or []), duration_ms, relayout_scope, roll_shift_ms)
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
                self._pending = int(t_ms)
                self._condition.notify()

    def stop(self):
        with self._condition:
            self._stopped = True
            self._condition.notify_all()
            renderer = self._renderer
        if renderer is not None:
            renderer.abort(reason="Skia preview stopped")
        self._thread.join(timeout=3)

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
                        valid = not self._stopped and version == self._version
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
                    failed_version = version
                    if valid:
                        self.backend_label = "Skia 错误"
                        self.renderError.emit("Skia 字幕渲染失败：" + str(exc))
        finally:
            if renderer is not None:
                renderer.close()
            with self._condition:
                self._renderer = None
