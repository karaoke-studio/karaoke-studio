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

import math
import os
import threading
import time
import uuid
from collections import OrderedDict, deque
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
    """实验开关：显式 ``KROK_SUBTITLE_NATIVE_RENDER=1`` 才启用 native 预览，默认关闭。

    2026-07-19 起从硬关闭恢复为 env opt-in，用于验证修复后的调度器
    （见 GPU 计划文档 §2.5 与 G2 调度硬性要求）；产品 UI 不暴露该开关。
    """
    return _env_enabled("KROK_SUBTITLE_NATIVE_RENDER", "0")


def gpu_preview_enabled() -> bool:
    """G2 developer opt-in; never enabled by product settings or by default."""
    return _env_enabled("KROK_SUBTITLE_GPU_PREVIEW", "0")


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
            # store() 已复制一份私有拷贝；pop 后缓存不再持有引用，直接移交即可。
            return self._images.pop(self._key(t_ms), None)

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
        self._extra_tracks: list[TimingTrack] = []
        self._pending_t: Optional[int] = None
        self._rendering = False
        self._stopping = False

    @Slot(object, object, object)
    def set_state(
        self,
        track: Optional[TimingTrack],
        style: Optional[Style],
        extra_tracks: object = None,
    ) -> None:
        self._track = track
        self._style = style
        self._extra_tracks = list(extra_tracks) if isinstance(extra_tracks, (list, tuple)) else []

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
        extra_tracks = self._extra_tracks
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
                paint_frame_to_painter(
                    painter, logical_w, logical_h, track, int(t_ms), style, extra_tracks
                )
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
    _state_changed = Signal(object, object, object)
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

    def set_state(
        self,
        track: Optional[TimingTrack],
        style: Optional[Style],
        extra_tracks: Optional[list[TimingTrack]] = None,
    ) -> None:
        if self._stopped:
            return
        self._track = track
        self._style = style
        self._state_changed.emit(track, style, list(extra_tracks or ()))

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


class GpuAsyncSubtitleRenderer(QObject):
    """Bounded latest-wins G2 preview scheduler for the Direct2D backend.

    There is exactly one in-flight synchronous sidecar request and at most one
    pending request. A pending timestamp can be replaced, never appended. One
    speculative frame may occupy that pending slot while playback is active.
    """

    frame_ready = Signal(QImage, int)

    _STALE_TOLERANCE_MS = 120

    def __init__(self, width: int, height: int, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._logical_w = max(int(width), 1)
        self._logical_h = max(int(height), 1)
        self._device_pixel_ratio = 1.0
        self._track: Optional[TimingTrack] = None
        self._style: Optional[Style] = None
        self._extra_tracks: list[TimingTrack] = []
        self._generation = 0
        self._request_serial = 0
        self._latest_t: Optional[int] = None
        self._pending: Optional[tuple[int, int, bool, float]] = None
        self._needs_configure = True
        self._playing = False
        self._stopped = False
        self._renderer_failed = False
        self._retry_after = 0.0
        self._force_warp = _env_enabled("KROK_SUBTITLE_GPU_FORCE_WARP", "0")
        self._lookahead_frames = _env_int(
            "KROK_SUBTITLE_GPU_LOOKAHEAD_FRAMES", 1, minimum=0
        )
        self._frame_cache = NativePreviewFrameCache(max(self._lookahead_frames + 1, 1))
        self._renderer: Optional[NativeRendererProcess] = None
        self._reader: Optional[SharedFrameRingReader] = None
        self._shm_key = f"krok-gpu-preview-{os.getpid()}-{uuid.uuid4().hex}"
        self._frame_index = 0
        self._condition = threading.Condition()
        self._stats_lock = threading.Lock()
        self._stats = {
            "requests": 0,
            "pending_replaced": 0,
            "frames_emitted": 0,
            "future_frames_cached": 0,
            "stale_frames_dropped": 0,
            "configure_count": 0,
            "renderer_failures": 0,
            "renderer_restarts": 0,
            "fallback_frames": 0,
            "max_pending": 0,
        }
        self._timings: dict[str, deque[float]] = {
            "render_ms": deque(maxlen=4096),
            "readback_ms": deque(maxlen=4096),
            "roundtrip_ms": deque(maxlen=4096),
            "ready_latency_ms": deque(maxlen=4096),
        }
        self._thread = threading.Thread(
            target=self._run,
            name="subtitle-preview-gpu-render",
            daemon=True,
        )
        self._thread.start()

    def set_state(
        self,
        track: Optional[TimingTrack],
        style: Optional[Style],
        extra_tracks: Optional[list[TimingTrack]] = None,
    ) -> None:
        with self._condition:
            if self._stopped:
                return
            self._track = track
            self._style = style
            self._extra_tracks = list(extra_tracks or ())
            self._generation += 1
            self._needs_configure = True
            self._pending = None
            self._frame_cache.clear()
            self._condition.notify_all()

    def set_size(self, width: int, height: int) -> None:
        self.set_render_target(width, height, self._device_pixel_ratio)

    def set_render_target(
        self,
        width: int,
        height: int,
        device_pixel_ratio: float = 1.0,
    ) -> None:
        with self._condition:
            if self._stopped:
                return
            target = (
                max(int(width), 1),
                max(int(height), 1),
                max(float(device_pixel_ratio or 1.0), 0.01),
            )
            if target != (self._logical_w, self._logical_h, self._device_pixel_ratio):
                self._logical_w, self._logical_h, self._device_pixel_ratio = target
                self._generation += 1
                self._needs_configure = True
                self._pending = None
                self._frame_cache.clear()
                # QSharedMemory cannot resize an existing named segment. Give
                # each render-target generation its own key while preserving
                # the sidecar/GPU device across ordinary frame requests.
                self._shm_key = f"krok-gpu-preview-{os.getpid()}-{uuid.uuid4().hex}"
            self._condition.notify_all()

    def request(self, t_ms: int) -> None:
        requested_t = int(t_ms)
        cached = self._frame_cache.take(requested_t)
        with self._condition:
            if self._stopped:
                return
            self._request_serial += 1
            serial = self._request_serial
            self._latest_t = requested_t
            self._note("requests")
            if cached is not None:
                self._note("frames_emitted")
                self.frame_ready.emit(cached, requested_t)
                if self._playing and self._lookahead_frames > 0:
                    self._replace_pending_locked(
                        self._next_frame_timestamp(requested_t), serial, True
                    )
            else:
                self._replace_pending_locked(requested_t, serial, False)
            self._condition.notify()

    def set_playing(self, playing: bool) -> None:
        with self._condition:
            self._playing = bool(playing)
            if not self._playing and self._pending is not None and self._pending[2]:
                self._pending = None
            self._condition.notify_all()

    def stop(self) -> None:
        with self._condition:
            if self._stopped:
                return
            self._stopped = True
            self._pending = None
            self._condition.notify_all()
        self._thread.join(timeout=3.0)

    def _replace_pending_locked(self, t_ms: int, serial: int, speculative: bool) -> None:
        if self._pending is not None:
            self._note("pending_replaced")
        self._pending = (int(t_ms), int(serial), bool(speculative), time.monotonic())
        self._note_max_pending(1)

    def _take_next_request(self):
        with self._condition:
            while not self._stopped and self._pending is None:
                self._condition.wait()
            if self._stopped:
                return None
            t_ms, serial, speculative, submitted_at = self._pending
            self._pending = None
            needs_configure = self._needs_configure
            self._needs_configure = False
            return (
                self._track,
                self._style,
                list(self._extra_tracks),
                self._logical_w,
                self._logical_h,
                self._device_pixel_ratio,
                t_ms,
                serial,
                speculative,
                self._generation,
                needs_configure,
                self._shm_key,
                submitted_at,
            )

    def _run(self) -> None:
        try:
            while True:
                snapshot = self._take_next_request()
                if snapshot is None:
                    return
                (
                    track,
                    style,
                    extra_tracks,
                    width,
                    height,
                    dpr,
                    t_ms,
                    serial,
                    speculative,
                    generation,
                    needs_configure,
                    shm_key,
                    submitted_at,
                ) = snapshot
                if track is None or style is None:
                    continue
                if self._renderer_failed:
                    if time.monotonic() < self._retry_after:
                        if not speculative:
                            self._emit_python_fallback(
                                track, style, extra_tracks, width, height, dpr, t_ms, generation
                            )
                        continue
                    self._renderer_failed = False
                    needs_configure = True
                    self._note("renderer_restarts")
                work_started = time.monotonic()
                try:
                    renderer = self._ensure_renderer()
                    if needs_configure:
                        renderer.configure_gpu(
                            track,
                            style,
                            width=width,
                            height=height,
                            fps=60,
                            dpr=dpr,
                            force_warp=self._force_warp,
                            extra_tracks=extra_tracks,
                        )
                        self._note("configure_count")
                    event = renderer.render_gpu_frame(
                        t_ms,
                        force_warp=self._force_warp,
                        generation=generation,
                        frame_index=self._frame_index,
                        shm_key=shm_key,
                        include_checksum=False,
                        readback_bands=True,
                    )
                    self._frame_index += 1
                    event_key = str(event.get("shm_key") or "")
                    if self._reader is None or self._reader.shm_key != event_key:
                        if self._reader is not None:
                            self._reader.close()
                        self._reader = SharedFrameRingReader.from_event(event)
                    image = self._reader.read_qimage(event)
                    image.setDevicePixelRatio(dpr)
                    completed_at = time.monotonic()
                    self._record_timing("roundtrip_ms", (completed_at - work_started) * 1000.0)
                    self._record_event_timing("render_ms", event.get("render_ms"))
                    self._record_event_timing("readback_ms", event.get("readback_ms"))
                    if not speculative:
                        self._record_timing(
                            "ready_latency_ms", (completed_at - submitted_at) * 1000.0
                        )
                    if speculative:
                        self._cache_speculative(image, t_ms, generation)
                    elif self._may_emit(t_ms, generation):
                        self._note("frames_emitted")
                        self.frame_ready.emit(image, int(t_ms))
                        self._schedule_lookahead(t_ms, serial, generation)
                    else:
                        self._note("stale_frames_dropped")
                except (NativeRendererError, RuntimeError) as exc:
                    if _env_enabled("KROK_SUBTITLE_NATIVE_DEBUG_FAILURES", "0"):
                        print(f"GPU preview failed: {exc}")
                    self._renderer_failed = True
                    self._retry_after = time.monotonic() + 1.0
                    with self._condition:
                        self._needs_configure = True
                    self._note("renderer_failures")
                    self._close_renderer()
                    if not speculative:
                        self._emit_python_fallback(
                            track, style, extra_tracks, width, height, dpr, t_ms, generation
                        )
        finally:
            self._close_renderer()

    def _ensure_renderer(self) -> NativeRendererProcess:
        if self._renderer is None:
            self._renderer = NativeRendererProcess(
                response_timeout_s=2.0,
                close_timeout_s=1.0,
            )
            self._renderer.start()
        return self._renderer

    def _close_renderer(self) -> None:
        if self._reader is not None:
            self._reader.close()
            self._reader = None
        if self._renderer is not None:
            self._renderer.close()
            self._renderer = None

    def _may_emit(self, t_ms: int, generation: int) -> bool:
        with self._condition:
            if self._stopped or generation != self._generation or self._latest_t is None:
                return False
            tolerance = self._STALE_TOLERANCE_MS if self._playing else 0
            return abs(int(t_ms) - int(self._latest_t)) <= tolerance

    def _cache_speculative(self, image: QImage, t_ms: int, generation: int) -> None:
        with self._condition:
            if self._stopped or generation != self._generation or self._latest_t is None:
                self._note("stale_frames_dropped")
                return
            if self._frame_cache.key_for(t_ms) < self._frame_cache.key_for(self._latest_t):
                self._note("stale_frames_dropped")
                return
        self._frame_cache.store(t_ms, image)
        self._note("future_frames_cached")

    def _schedule_lookahead(self, t_ms: int, serial: int, generation: int) -> None:
        with self._condition:
            if (
                self._stopped
                or not self._playing
                or self._lookahead_frames <= 0
                or generation != self._generation
                or self._pending is not None
                or serial != self._request_serial
            ):
                return
            self._pending = (
                self._next_frame_timestamp(t_ms),
                serial,
                True,
                time.monotonic(),
            )
            self._note_max_pending(1)
            self._condition.notify()

    @staticmethod
    def _next_frame_timestamp(t_ms: int) -> int:
        return int(round(int(t_ms) + 1000.0 / 60.0))

    def _emit_python_fallback(
        self,
        track: TimingTrack,
        style: Style,
        extra_tracks: list[TimingTrack],
        width: int,
        height: int,
        dpr: float,
        t_ms: int,
        generation: int,
    ) -> None:
        if not self._may_emit(t_ms, generation):
            self._note("stale_frames_dropped")
            return
        physical_w, physical_h, dpr = preview_render_target_size(width, height, dpr)
        image = QImage(physical_w, physical_h, QImage.Format.Format_ARGB32_Premultiplied)
        image.setDevicePixelRatio(dpr)
        image.fill(0)
        painter = QPainter(image)
        try:
            paint_frame_to_painter(
                painter, width, height, track, int(t_ms), style, extra_tracks
            )
        finally:
            painter.end()
        if self._may_emit(t_ms, generation):
            self._note("fallback_frames")
            self._note("frames_emitted")
            self.frame_ready.emit(image, int(t_ms))

    def _note(self, key: str) -> None:
        with self._stats_lock:
            self._stats[key] += 1

    def _note_max_pending(self, value: int) -> None:
        with self._stats_lock:
            self._stats["max_pending"] = max(self._stats["max_pending"], int(value))

    def _record_event_timing(self, key: str, value: object) -> None:
        try:
            normalized = float(value)
        except (TypeError, ValueError):
            return
        self._record_timing(key, normalized)

    def _record_timing(self, key: str, value: float) -> None:
        with self._stats_lock:
            self._timings[key].append(max(float(value), 0.0))

    def stats_snapshot(self) -> dict[str, int]:
        with self._stats_lock:
            return dict(self._stats)

    def timing_snapshot(self) -> dict[str, dict[str, float | int]]:
        with self._stats_lock:
            result: dict[str, dict[str, float | int]] = {}
            for key, samples in self._timings.items():
                values = sorted(samples)
                if not values:
                    result[key] = {"count": 0, "mean": 0.0, "p95": 0.0, "max": 0.0}
                    continue
                p95_index = min(
                    max(math.ceil(len(values) * 0.95) - 1, 0),
                    len(values) - 1,
                )
                result[key] = {
                    "count": len(values),
                    "mean": sum(values) / len(values),
                    "p95": values[p95_index],
                    "max": values[-1],
                }
            return result


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
        self._shm_key = ""
        self._renderer_failed = False
        self._playing = False
        self._last_t: Optional[int] = None
        self._fps = 60
        self._lookahead_frames = _env_int(
            "KROK_SUBTITLE_NATIVE_LOOKAHEAD_FRAMES",
            6,
            minimum=0,
        )
        # 自适应前瞻（G2 硬性要求 6"失控自愈"）：range 端到端耗时超过前瞻窗口时收缩，
        # 恢复后逐步回涨到配置值；仅 worker 线程读写。
        self._effective_lookahead = self._lookahead_frames
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

    def set_state(
        self,
        track: Optional[TimingTrack],
        style: Optional[Style],
        extra_tracks: Optional[list[TimingTrack]] = None,  # noqa: ARG002 — native 预览暂不支持副轨
    ) -> None:
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
            self._purge_stale_waiting_locked(requested_key)
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
                dpr,
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
                self._emit_python_fallback(track, style, width, height, dpr, t_ms, generation)
                continue
            try:
                self._render_native(
                    track,
                    style,
                    width=width,
                    height=height,
                    dpr=dpr,
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
                self._emit_python_fallback(track, style, width, height, dpr, t_ms, generation)

    def _take_next_request(
        self,
    ) -> tuple[TimingTrack | None, Style | None, int, int, float, int, int, bool, bool, bool, bool] | None:
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
                self._device_pixel_ratio,
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
        dpr: float,
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
            lookahead_frames=self._effective_lookahead,
            include_current=not skip_current,
        )
        # 调度硬性要求（GPU 计划 §2.5 / G2）：
        # 1. 不回灌积压——过期的 waiting 请求绝不加入新 range 重新渲染；
        # 3. 单次在途帧数 ≤ ring 槽数，杜绝发射端覆写未读槽。
        timestamps = timestamps[: self._ring_slots]
        if not timestamps:
            return
        range_started = time.monotonic()
        with self._process_lock:
            if restart_renderer and self._renderer is not None:
                self._renderer.close()
                self._renderer = None
                needs_configure = True
            renderer_was_missing = self._renderer is None
            renderer = self._ensure_renderer()
            if renderer_was_missing or needs_configure:
                # dpr 让 native 按显示分辨率光栅化（布局仍在逻辑坐标系），
                # 与 Python 预览路径一致；4K 工程预览不再渲染全分辨率帧。
                renderer.configure(track, style, width=width, height=height, fps=60, dpr=dpr)
            # 资源常驻（G2 硬性要求 4）：shm_key 与 renderer 同生命周期，
            # sidecar 端据此跨 range 复用同一块 ring，不再逐 range 重建。
            shm_key = self._shm_key
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
                                image.setDevicePixelRatio(dpr)
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
                            elif self._is_behind_latest_request(slot.t_ms):
                                # 早于最新请求的帧没有未来消费者，缓存只会污染 LRU。
                                self._stats.note_stale_frame_dropped()
                            else:
                                self._stats.note_future_frame_cached()
                                self._frame_cache.store(slot.t_ms, image)
                        else:
                            self._stats.note_stale_frame_dropped()
                    elif event.get("event") == "range_done":
                        self._stats.note_range_done_event()
                        self._adapt_lookahead(
                            (time.monotonic() - range_started) * 1000.0, playing=playing
                        )
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
            self._shm_key = f"krok-preview-{os.getpid()}-{uuid.uuid4().hex}"
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
        dpr: float,
        t_ms: int,
        generation: int,
    ) -> None:
        if not self._is_current_generation(generation):
            return
        physical_w, physical_h, dpr = preview_render_target_size(width, height, dpr)
        image = QImage(physical_w, physical_h, QImage.Format.Format_ARGB32_Premultiplied)
        image.setDevicePixelRatio(dpr)
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

    def _purge_stale_waiting_locked(self, current_key: int) -> None:
        """丢弃早于当前帧桶的未兑现请求（G2 硬性要求 1"积压有上限"）。

        过期请求既不回灌新 range 重新渲染，也不再等待兑现——它们对应的画面
        已经过时，唯一正确的结局是作为丢帧统计掉。同帧桶内的毫秒抖动兑现
        （1033ms/1034ms）不受影响。
        """
        stale_keys = [key for key in self._waiting_request_by_key if key < current_key]
        for key in stale_keys:
            del self._waiting_request_by_key[key]
            self._stats.note_stale_frame_dropped()

    def _is_behind_latest_request(self, t_ms: int) -> bool:
        with self._condition:
            latest_t = self._last_t
        if latest_t is None:
            return False
        return self._frame_cache.key_for(int(t_ms)) < self._frame_cache.key_for(int(latest_t))

    def _adapt_lookahead(self, elapsed_ms: float, *, playing: bool) -> None:
        """按 range 端到端耗时收缩/恢复前瞻（G2 硬性要求 6"失控自愈"）。

        range 耗时超过前瞻窗口意味着产出注定过期（§2.5 死亡螺旋的临界条件），
        此时对半收缩前瞻，最低退化为纯 latest-wins 单帧；耗时回落后逐步涨回配置值。
        """
        if not playing or self._lookahead_frames <= 0:
            return
        frame_ms = max(1000.0 / max(self._fps, 1), 1.0)
        window_ms = frame_ms * (self._effective_lookahead + 1)
        if elapsed_ms > window_ms:
            self._effective_lookahead = self._effective_lookahead // 2
        elif elapsed_ms < window_ms * 0.5 and self._effective_lookahead < self._lookahead_frames:
            self._effective_lookahead += 1

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
