"""Off-GUI-thread subtitle rasterisation for the preview (experimental).

Background (§9 A4 诊断)：预览预览的真实帧率天花板**不是单帧光栅化成本本身**，而是
字幕 paint 在 GUI 主线程上与视频呈现循环**串行**——单帧 14–20ms 的矢量/glow 栅格化
直接加进每帧周期，把 60Hz 的呈现循环拖到 ~30–35Hz（`--no-subtitle` 时循环可跑满 60）。

本模块把字幕栅格化搬到**独立工作线程**：worker 渲染进 ``QImage``，GUI 线程的
``SubtitleGraphicsItem.paint`` 只做一次廉价 blit。主循环不再被 14ms 阻塞 → 呈现回到
~60Hz；字幕内容按 worker 产出速率刷新（latest-wins 合并，丢弃过期请求）。

默认开启；env ``KROK_SUBTITLE_ASYNC_PREVIEW=0`` 可回退同步预览（导出路径不受影响）。
"""

from __future__ import annotations

import os
import threading
import uuid
from collections import OrderedDict
from typing import Optional

from PyQt6.QtCore import QObject, QThread, QTimer, pyqtSignal as Signal, pyqtSlot as Slot
from PyQt6.QtGui import QImage, QPainter

from krok_helper.subtitle_render.engine.painter import paint_frame_to_painter
from krok_helper.subtitle_render.models import Style, TimingTrack
from krok_helper.subtitle_render.native_backend import (
    NativeRendererError,
    NativeRendererProcess,
    SharedFrameRingReader,
)


def _env_enabled(name: str, default: str) -> bool:
    return os.environ.get(name, default).strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    try:
        value = int(os.environ.get(name, str(default)) or default)
    except (TypeError, ValueError):
        value = default
    return max(int(value), int(minimum))


def _default_native_preview_threads() -> int:
    return min(max(os.cpu_count() or 4, 1), 6)


def async_preview_enabled() -> bool:
    return _env_enabled("KROK_SUBTITLE_ASYNC_PREVIEW", "1")


def native_preview_enabled() -> bool:
    """Native preview is temporarily hard-disabled for layout consistency."""
    return False


def native_preview_timestamps(
    t_ms: int,
    *,
    playing: bool,
    fps: int,
    lookahead_frames: int,
    include_current: bool = True,
) -> list[int]:
    """Return current frame plus optional playback look-ahead timestamps."""
    current = int(t_ms)
    if not playing:
        return [current] if include_current else []
    normalized_fps = max(int(fps), 1)
    frame_ms = 1000.0 / normalized_fps
    count = max(int(lookahead_frames), 0)
    start_offset = 0 if include_current else 1
    timestamps = [
        int(round(current + frame_ms * offset))
        for offset in range(start_offset, count + 1)
    ]
    return list(dict.fromkeys(timestamps))


class NativePreviewFrameCache:
    """Small thread-safe QImage cache for native preview look-ahead frames."""

    def __init__(self, max_frames: int, fps: int = 60) -> None:
        self._max_frames = max(int(max_frames), 1)
        self._fps = max(int(fps), 1)
        self._images: OrderedDict[int, QImage] = OrderedDict()
        self._lock = threading.Lock()

    def _key(self, t_ms: int) -> int:
        return int(round(int(t_ms) * self._fps / 1000.0))

    def key_for(self, t_ms: int) -> int:
        return self._key(t_ms)

    def store(self, t_ms: int, image: QImage) -> None:
        copied = image.copy()
        with self._lock:
            key = self._key(t_ms)
            self._images.pop(key, None)
            self._images[key] = copied
            while len(self._images) > self._max_frames:
                self._images.popitem(last=False)

    def take(self, t_ms: int) -> Optional[QImage]:
        with self._lock:
            image = self._images.pop(self._key(t_ms), None)
        if image is None:
            return None
        return image.copy()

    def clear(self) -> None:
        with self._lock:
            self._images.clear()


class NativePreviewStats:
    """Thread-safe counters for native preview scheduler diagnostics."""

    _COUNTERS = (
        "cache_hits",
        "cache_misses",
        "future_frames_cached",
        "stale_frames_dropped",
        "generations_cancelled",
        "native_generation_cancelled_events",
        "range_done_events",
        "native_renderer_failures",
    )

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values = {key: 0 for key in self._COUNTERS}

    def note_cache_hit(self) -> None:
        self._increment("cache_hits")

    def note_cache_miss(self) -> None:
        self._increment("cache_misses")

    def note_future_frame_cached(self) -> None:
        self._increment("future_frames_cached")

    def note_stale_frame_dropped(self) -> None:
        self._increment("stale_frames_dropped")

    def note_generation_cancelled(self) -> None:
        self._increment("generations_cancelled")

    def note_native_generation_cancelled_event(self) -> None:
        self._increment("native_generation_cancelled_events")

    def note_range_done_event(self) -> None:
        self._increment("range_done_events")

    def note_native_renderer_failure(self) -> None:
        self._increment("native_renderer_failures")

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._values)

    def _increment(self, key: str) -> None:
        with self._lock:
            self._values[key] += 1


def preview_render_target_size(
    logical_width: int,
    logical_height: int,
    device_pixel_ratio: float,
) -> tuple[int, int, float]:
    """Return physical image size + normalized DPR for async preview rendering."""
    logical_w = max(int(logical_width), 1)
    logical_h = max(int(logical_height), 1)
    dpr = max(float(device_pixel_ratio or 1.0), 0.01)
    return (
        max(int(round(logical_w * dpr)), 1),
        max(int(round(logical_h * dpr)), 1),
        dpr,
    )


class _AsyncSubtitleWorker(QObject):
    """Qt-thread resident worker that rasterises latest-wins subtitle requests."""

    frame_ready = Signal(QImage, int)
    finished = Signal()

    def __init__(self, width: int, height: int) -> None:
        super().__init__()
        self._logical_w = max(int(width), 1)
        self._logical_h = max(int(height), 1)
        self._device_pixel_ratio = 1.0
        self._track: Optional[TimingTrack] = None
        self._style: Optional[Style] = None
        self._pending_t: Optional[int] = None
        self._rendering = False
        self._stopping = False

    @Slot(object, object)
    def set_state(self, track: Optional[TimingTrack], style: Optional[Style]) -> None:
        self._track = track
        self._style = style

    @Slot(int, int, float)
    def set_render_target(self, width: int, height: int, device_pixel_ratio: float = 1.0) -> None:
        self._logical_w = max(int(width), 1)
        self._logical_h = max(int(height), 1)
        self._device_pixel_ratio = max(float(device_pixel_ratio or 1.0), 0.01)

    @Slot(int)
    def request(self, t_ms: int) -> None:
        self._pending_t = int(t_ms)
        if not self._rendering:
            QTimer.singleShot(0, self._render_pending)

    @Slot()
    def stop(self) -> None:
        self._stopping = True
        if not self._rendering:
            self.finished.emit()

    def _render_pending(self) -> None:
        if self._stopping:
            self.finished.emit()
            return
        if self._pending_t is None:
            return
        t_ms = self._pending_t
        self._pending_t = None
        track = self._track
        style = self._style
        logical_w = self._logical_w
        logical_h = self._logical_h
        dpr = self._device_pixel_ratio
        if track is None or style is None:
            return

        self._rendering = True
        try:
            physical_w, physical_h, dpr = preview_render_target_size(logical_w, logical_h, dpr)
            image = QImage(physical_w, physical_h, QImage.Format.Format_ARGB32_Premultiplied)
            image.setDevicePixelRatio(dpr)
            image.fill(0)
            painter = QPainter(image)
            try:
                paint_frame_to_painter(painter, logical_w, logical_h, track, int(t_ms), style)
            finally:
                painter.end()
            self.frame_ready.emit(image, int(t_ms))
        finally:
            self._rendering = False

        if self._stopping:
            self.finished.emit()
        elif self._pending_t is not None:
            QTimer.singleShot(0, self._render_pending)


class AsyncSubtitleRenderer(QObject):
    """Renders subtitle frames on a worker thread; emits :pyattr:`frame_ready`.

    协议：GUI 线程通过 :meth:`set_state` / :meth:`set_size` 更新轨道/样式/尺寸，
    通过 :meth:`request` 投递目标时间（latest-wins 合并）。worker 渲染完成后从工作
    线程 emit ``frame_ready(QImage, t_ms)``——接收方须用 ``QueuedConnection`` 接到
    GUI 线程槽（QImage 跨线程经队列连接复制句柄，安全）。内部使用 ``QThread``，
    避免 Qt 图形对象在普通 Python 线程里启动 ``QBasicTimer``。
    """

    frame_ready = Signal(QImage, int)
    _state_changed = Signal(object, object)
    _target_changed = Signal(int, int, float)
    _frame_requested = Signal(int)

    def __init__(self, width: int, height: int, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._logical_w = max(int(width), 1)
        self._logical_h = max(int(height), 1)
        self._device_pixel_ratio = 1.0
        self._track: Optional[TimingTrack] = None
        self._style: Optional[Style] = None
        self._stopped = False
        self._thread = QThread(self)
        self._thread.setObjectName("subtitle-preview-render")
        self._worker = _AsyncSubtitleWorker(self._logical_w, self._logical_h)
        self._worker.moveToThread(self._thread)
        self._state_changed.connect(self._worker.set_state)
        self._target_changed.connect(self._worker.set_render_target)
        self._frame_requested.connect(self._worker.request)
        self._worker.frame_ready.connect(self.frame_ready)
        self._worker.finished.connect(self._thread.quit)
        self._thread.finished.connect(self._worker.deleteLater)
        self._thread.start()

    # ------------------------------------------------------------------ GUI API

    def __del__(self) -> None:
        try:
            self.stop()
        except RuntimeError:
            pass

    def set_state(self, track: Optional[TimingTrack], style: Optional[Style]) -> None:
        if self._stopped:
            return
        self._track = track
        self._style = style
        self._state_changed.emit(track, style)

    def set_size(self, width: int, height: int) -> None:
        self.set_render_target(width, height, self._device_pixel_ratio)

    def set_render_target(self, width: int, height: int, device_pixel_ratio: float = 1.0) -> None:
        if self._stopped:
            return
        self._logical_w = max(int(width), 1)
        self._logical_h = max(int(height), 1)
        self._device_pixel_ratio = max(float(device_pixel_ratio or 1.0), 0.01)
        self._target_changed.emit(self._logical_w, self._logical_h, self._device_pixel_ratio)

    def request(self, t_ms: int) -> None:
        """投递一帧渲染请求；只保留最新 t（合并掉过期请求）。"""
        if self._stopped:
            return
        self._frame_requested.emit(int(t_ms))

    def set_playing(self, playing: bool) -> None:  # noqa: ARG002
        """Playback state hook kept for API symmetry with the native preview path."""
        return

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        try:
            self._worker._stopping = True  # noqa: SLF001
        except RuntimeError:
            pass
        self._thread.quit()
        if not self._thread.wait(2000):
            self._thread.quit()
            self._thread.wait(1000)


class NativeAsyncSubtitleRenderer(QObject):
    """Preview renderer backed by the native sidecar shared-memory range path."""

    frame_ready = Signal(QImage, int)

    def __init__(self, width: int, height: int, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._logical_w = max(int(width), 1)
        self._logical_h = max(int(height), 1)
        self._device_pixel_ratio = 1.0
        self._track: Optional[TimingTrack] = None
        self._style: Optional[Style] = None
        self._generation = 0
        self._active_generation: Optional[int] = None
        self._pending_t: Optional[int] = None
        self._pending_skip_current = False
        self._stopped = False
        self._needs_configure = True
        self._restart_renderer = False
        self._renderer: Optional[NativeRendererProcess] = None
        self._renderer_failed = False
        self._playing = False
        self._last_t: Optional[int] = None
        self._fps = 60
        self._lookahead_frames = _env_int(
            "KROK_SUBTITLE_NATIVE_LOOKAHEAD_FRAMES",
            6,
            minimum=0,
        )
        self._threads = _env_int(
            "KROK_SUBTITLE_NATIVE_THREADS",
            _default_native_preview_threads(),
            minimum=1,
        )
        self._ring_slots = max(
            _env_int(
                "KROK_SUBTITLE_NATIVE_RING_SLOTS",
                self._lookahead_frames + 2,
                minimum=1,
            ),
            self._lookahead_frames + 2,
        )
        self._frame_cache = NativePreviewFrameCache(self._lookahead_frames + 1)
        self._waiting_request_by_key: dict[int, int] = {}
        self._emitted_request_keys: set[int] = set()
        self._stats = NativePreviewStats()
        self._condition = threading.Condition()
        self._process_lock = threading.Lock()
        self._thread = threading.Thread(
            target=self._run,
            name="subtitle-preview-native-render",
            daemon=True,
        )
        self._thread.start()

    def set_state(self, track: Optional[TimingTrack], style: Optional[Style]) -> None:
        with self._condition:
            if self._stopped:
                return
            self._track = track
            self._style = style
            self._advance_generation_locked()
            self._needs_configure = True
            self._frame_cache.clear()
            self._waiting_request_by_key.clear()
            self._emitted_request_keys.clear()
            self._condition.notify()

    def set_size(self, width: int, height: int) -> None:
        self.set_render_target(width, height, self._device_pixel_ratio)

    def set_render_target(self, width: int, height: int, device_pixel_ratio: float = 1.0) -> None:
        with self._condition:
            if self._stopped:
                return
            w = max(int(width), 1)
            h = max(int(height), 1)
            dpr = max(float(device_pixel_ratio or 1.0), 0.01)
            if (w, h, dpr) != (self._logical_w, self._logical_h, self._device_pixel_ratio):
                self._needs_configure = True
                self._restart_renderer = True
                self._advance_generation_locked()
                self._frame_cache.clear()
                self._waiting_request_by_key.clear()
                self._emitted_request_keys.clear()
            self._logical_w = w
            self._logical_h = h
            self._device_pixel_ratio = dpr
            self._condition.notify()

    def request(self, t_ms: int) -> None:
        requested_t = int(t_ms)
        requested_key = self._frame_cache.key_for(requested_t)
        cached = self._frame_cache.take(requested_t)
        if cached is not None:
            self._stats.note_cache_hit()
            with self._condition:
                self._emitted_request_keys.add(requested_key)
            self.frame_ready.emit(cached, requested_t)
        else:
            self._stats.note_cache_miss()
        with self._condition:
            if self._stopped:
                return
            if self._should_advance_generation_for_request_locked(requested_t):
                self._advance_generation_locked()
                self._waiting_request_by_key.clear()
                self._emitted_request_keys.clear()
            self._last_t = requested_t
            self._pending_t = self._last_t
            self._pending_skip_current = cached is not None
            if cached is None:
                self._waiting_request_by_key[requested_key] = requested_t
            self._condition.notify()

    def set_playing(self, playing: bool) -> None:
        with self._condition:
            if self._stopped:
                return
            normalized = bool(playing)
            if self._playing == normalized:
                return
            self._playing = normalized
            if self._last_t is not None:
                self._advance_generation_locked()
                self._waiting_request_by_key.clear()
                self._emitted_request_keys.clear()
                self._pending_t = self._last_t
                self._pending_skip_current = False
                self._waiting_request_by_key[self._frame_cache.key_for(self._last_t)] = self._last_t
                self._condition.notify()

    def stop(self) -> None:
        with self._condition:
            if self._stopped:
                return
            self._stopped = True
            self._condition.notify_all()
        with self._process_lock:
            if self._renderer is not None:
                self._renderer.close()
                self._renderer = None
        self._thread.join(timeout=2.0)

    def _run(self) -> None:
        while True:
            snapshot = self._take_next_request()
            if snapshot is None:
                return
            (
                track,
                style,
                width,
                height,
                t_ms,
                generation,
                needs_configure,
                restart_renderer,
                playing,
                skip_current,
            ) = snapshot
            if track is None or style is None:
                continue
            if self._renderer_failed:
                self._emit_python_fallback(track, style, width, height, t_ms, generation)
                continue
            try:
                self._render_native(
                    track,
                    style,
                    width=width,
                    height=height,
                    t_ms=t_ms,
                    generation=generation,
                    needs_configure=needs_configure,
                    restart_renderer=restart_renderer,
                    playing=playing,
                    skip_current=skip_current,
                )
            except NativeRendererError as exc:
                self._stats.note_native_renderer_failure()
                if _env_enabled("KROK_SUBTITLE_NATIVE_DEBUG_FAILURES", "0"):
                    print(f"native preview failed: {exc}")
                self._renderer_failed = True
                self._close_renderer()
                self._emit_python_fallback(track, style, width, height, t_ms, generation)

    def _take_next_request(
        self,
    ) -> tuple[TimingTrack | None, Style | None, int, int, int, int, bool, bool, bool, bool] | None:
        with self._condition:
            while not self._stopped and self._pending_t is None:
                self._condition.wait()
            if self._stopped:
                return None
            t_ms = int(self._pending_t or 0)
            skip_current = self._pending_skip_current
            self._pending_t = None
            self._pending_skip_current = False
            needs_configure = self._needs_configure
            self._needs_configure = False
            restart_renderer = self._restart_renderer
            self._restart_renderer = False
            return (
                self._track,
                self._style,
                self._logical_w,
                self._logical_h,
                t_ms,
                self._generation,
                needs_configure,
                restart_renderer,
                self._playing,
                skip_current,
            )

    def _render_native(
        self,
        track: TimingTrack,
        style: Style,
        *,
        width: int,
        height: int,
        t_ms: int,
        generation: int,
        needs_configure: bool,
        restart_renderer: bool,
        playing: bool,
        skip_current: bool,
    ) -> None:
        timestamps = native_preview_timestamps(
            t_ms,
            playing=playing,
            fps=self._fps,
            lookahead_frames=self._lookahead_frames,
            include_current=not skip_current,
        )
        timestamps = self._include_waiting_timestamps(timestamps, t_ms=t_ms)
        if not timestamps:
            return
        with self._process_lock:
            if restart_renderer and self._renderer is not None:
                self._renderer.close()
                self._renderer = None
                needs_configure = True
            renderer_was_missing = self._renderer is None
            renderer = self._ensure_renderer()
            if renderer_was_missing or needs_configure:
                renderer.configure(track, style, width=width, height=height, fps=60)
            shm_key = f"krok-preview-{os.getpid()}-{uuid.uuid4().hex}"
            with self._condition:
                if not self._stopped and self._generation == generation:
                    self._active_generation = generation
            try:
                reader: Optional[SharedFrameRingReader] = None
                renderer.start_render_range(
                    timestamps,
                    generation=generation,
                    threads=self._threads,
                    shm_key=shm_key,
                    ring_slots=self._ring_slots,
                )
                while True:
                    event = renderer.read_event()
                    if event.get("event") == "frame_ready":
                        if self._is_current_generation(generation):
                            try:
                                event_key = str(event.get("shm_key") or "")
                                if reader is None or reader.shm_key != event_key:
                                    if reader is not None:
                                        reader.close()
                                    reader = SharedFrameRingReader.from_event(event)
                                slot = reader.read_frame(event)
                                image = slot.to_qimage()
                            except NativeRendererError:
                                self._stats.note_stale_frame_dropped()
                                continue
                            requested_t = self._take_waiting_request_for_slot(slot.t_ms)
                            if requested_t is not None:
                                self.frame_ready.emit(image, requested_t)
                            elif int(slot.t_ms) == int(t_ms) and self._mark_emitted_if_new(slot.t_ms):
                                self.frame_ready.emit(image, slot.t_ms)
                            elif self._was_emitted(slot.t_ms):
                                continue
                            else:
                                self._stats.note_future_frame_cached()
                                self._frame_cache.store(slot.t_ms, image)
                        else:
                            self._stats.note_stale_frame_dropped()
                    elif event.get("event") == "range_done":
                        self._stats.note_range_done_event()
                        return
                    elif event.get("event") == "generation_cancelled":
                        self._stats.note_native_generation_cancelled_event()
                        continue
            finally:
                if reader is not None:
                    reader.close()
                with self._condition:
                    if self._active_generation == generation:
                        self._active_generation = None

    def _ensure_renderer(self) -> NativeRendererProcess:
        if self._renderer is None:
            self._renderer = NativeRendererProcess(response_timeout_s=2.0, close_timeout_s=1.0)
            self._renderer.start()
            self._needs_configure = True
        return self._renderer

    def _close_renderer(self) -> None:
        with self._process_lock:
            if self._renderer is not None:
                self._renderer.close()
                self._renderer = None

    def _emit_python_fallback(
        self,
        track: TimingTrack,
        style: Style,
        width: int,
        height: int,
        t_ms: int,
        generation: int,
    ) -> None:
        if not self._is_current_generation(generation):
            return
        image = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(0)
        painter = QPainter(image)
        try:
            paint_frame_to_painter(painter, width, height, track, int(t_ms), style)
        finally:
            painter.end()
        if self._is_current_generation(generation):
            self.frame_ready.emit(image, int(t_ms))

    def _is_current_generation(self, generation: int) -> bool:
        with self._condition:
            return not self._stopped and int(generation) == self._generation

    def _take_waiting_request_for_slot(self, t_ms: int) -> Optional[int]:
        key = self._frame_cache.key_for(int(t_ms))
        with self._condition:
            requested_t = self._waiting_request_by_key.pop(key, None)
            if requested_t is None or key in self._emitted_request_keys:
                return None
            self._emitted_request_keys.add(key)
            return requested_t

    def _mark_emitted_if_new(self, t_ms: int) -> bool:
        key = self._frame_cache.key_for(int(t_ms))
        with self._condition:
            if key in self._emitted_request_keys:
                return False
            self._emitted_request_keys.add(key)
            return True

    def _was_emitted(self, t_ms: int) -> bool:
        key = self._frame_cache.key_for(int(t_ms))
        with self._condition:
            return key in self._emitted_request_keys

    def _include_waiting_timestamps(self, timestamps: list[int], *, t_ms: int) -> list[int]:
        if not self._playing:
            return timestamps
        with self._condition:
            waiting = sorted(
                int(value)
                for key, value in self._waiting_request_by_key.items()
                if key not in self._emitted_request_keys and int(value) <= int(t_ms)
            )
        return list(dict.fromkeys([*waiting, *timestamps]))

    def _should_advance_generation_for_request_locked(self, requested_t: int) -> bool:
        if not self._playing:
            return True
        if self._last_t is None:
            return False
        frame_ms = max(1000.0 / max(self._fps, 1), 1.0)
        delta = int(requested_t) - int(self._last_t)
        if delta < -frame_ms:
            return True
        lookahead_window_ms = frame_ms * max(self._lookahead_frames + 1, 1)
        return delta > lookahead_window_ms

    def _advance_generation_locked(self) -> None:
        active_generation = self._active_generation
        self._generation += 1
        if active_generation is None:
            return
        renderer = self._renderer
        if renderer is None:
            return
        try:
            renderer.send_cancel_generation(active_generation)
            self._stats.note_generation_cancelled()
        except NativeRendererError:
            self._renderer_failed = True

    def stats_snapshot(self) -> dict[str, int]:
        return self._stats.snapshot()
