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
from collections import OrderedDict
from typing import Optional

from PyQt6.QtCore import QObject, QThread, QTimer, pyqtSignal as Signal, pyqtSlot as Slot
from PyQt6.QtGui import QImage, QPainter

from krok_helper.subtitle_render.engine.painter import paint_frame_to_painter
from krok_helper.subtitle_render.models import Style, TimingTrack


def async_preview_enabled() -> bool:
    return os.environ.get("KROK_SUBTITLE_ASYNC_PREVIEW", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


# 预览输出帧缓存（P-A，见 docs/字幕渲染-预渲染帧缓存方案评估.md §3.2）：把渲染好的字幕帧
# 按「帧索引」存起来，重播 / seek / 暂停后逐帧 scrub 命中即取（这些帧现在每次都重渲）。
# 仅服务异步路径；导出不受影响（导出每帧只渲一次，无可缓存性，见评估 §8）。命中帧与重渲
# 帧逐像素相同（同一 canonical t_ms → 同一渲染结果）。**默认关**：分批落地、真机 A/B 后再转默认开。
_PREVIEW_CACHE_FPS = 60
"""帧缓存量子化网格（fps）：请求时间吸附到此网格的帧边界，使连续墙钟时间可命中同一帧。"""

_PREVIEW_CACHE_MAX_FRAMES = 64
"""环形上界（帧数）。device 全幅约 13MB/帧 → 64 帧约 832MB；可经 env 调小。"""


def frame_cache_enabled() -> bool:
    """P-A 预览帧缓存开关（默认关，``KROK_SUBTITLE_PREVIEW_FRAME_CACHE=1`` 开启）。"""
    return os.environ.get("KROK_SUBTITLE_PREVIEW_FRAME_CACHE", "0").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _frame_cache_max_frames() -> int:
    raw = os.environ.get("KROK_SUBTITLE_PREVIEW_FRAME_CACHE_MAX")
    if raw is not None and raw.strip():
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    return _PREVIEW_CACHE_MAX_FRAMES


def preview_frame_index(t_ms: int, fps: int = _PREVIEW_CACHE_FPS) -> int:
    """把时间量子化到帧网格索引（纯函数，便于单测）。"""
    fps = max(int(fps), 1)
    return int(round(int(t_ms) * fps / 1000.0))


def preview_frame_canonical_ms(frame_index: int, fps: int = _PREVIEW_CACHE_FPS) -> int:
    """帧索引 → 该帧的规范 t_ms（与 :func:`preview_frame_index` 互逆，渲染用此 t 保证可复现）。"""
    fps = max(int(fps), 1)
    return int(round(int(frame_index) * 1000.0 / fps))


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
        # P-A 帧缓存（仅 GUI 线程访问：request / _on_worker_frame 都在 GUI 线程跑 → 无需锁）。
        self._frame_cache: "OrderedDict[int, QImage]" = OrderedDict()
        self._thread = QThread(self)
        self._thread.setObjectName("subtitle-preview-render")
        self._worker = _AsyncSubtitleWorker(self._logical_w, self._logical_h)
        self._worker.moveToThread(self._thread)
        self._state_changed.connect(self._worker.set_state)
        self._target_changed.connect(self._worker.set_render_target)
        self._frame_requested.connect(self._worker.request)
        # worker 渲完先经本对象（GUI 线程，AutoConnection 跨线程→队列）写入缓存再上抛。
        self._worker.frame_ready.connect(self._on_worker_frame)
        self._worker.finished.connect(self._thread.quit)
        self._thread.finished.connect(self._worker.deleteLater)
        self._thread.start()

    def _clear_frame_cache(self) -> None:
        self._frame_cache.clear()

    def _on_worker_frame(self, image: QImage, t_ms: int) -> None:
        # GUI 线程：worker 产出新帧 → 按帧索引存入缓存（有界 LRU）再上抛给视图。
        if frame_cache_enabled():
            index = preview_frame_index(t_ms)
            self._frame_cache[index] = image
            self._frame_cache.move_to_end(index)
            while len(self._frame_cache) > _frame_cache_max_frames():
                self._frame_cache.popitem(last=False)
        self.frame_ready.emit(image, t_ms)

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
        # 轨道/样式变 → 已缓存帧作废（与 LayerCache.clear 同范式）。
        self._clear_frame_cache()
        self._state_changed.emit(track, style)

    def set_size(self, width: int, height: int) -> None:
        self.set_render_target(width, height, self._device_pixel_ratio)

    def set_render_target(self, width: int, height: int, device_pixel_ratio: float = 1.0) -> None:
        if self._stopped:
            return
        self._logical_w = max(int(width), 1)
        self._logical_h = max(int(height), 1)
        self._device_pixel_ratio = max(float(device_pixel_ratio or 1.0), 0.01)
        # 渲染尺寸/DPR 变 → 已缓存帧分辨率不再匹配，作废。
        self._clear_frame_cache()
        self._target_changed.emit(self._logical_w, self._logical_h, self._device_pixel_ratio)

    def request(self, t_ms: int) -> None:
        """投递一帧渲染请求；只保留最新 t（合并掉过期请求）。

        P-A：开启帧缓存时，先把 t 吸附到帧网格并查缓存——命中即直接上抛缓存帧（不投 worker）；
        未命中则按规范帧 t 投 worker（渲完由 :meth:`_on_worker_frame` 入缓存）。
        """
        if self._stopped:
            return
        if frame_cache_enabled():
            index = preview_frame_index(int(t_ms))
            cached = self._frame_cache.get(index)
            if cached is not None:
                self._frame_cache.move_to_end(index)
                self.frame_ready.emit(cached, preview_frame_canonical_ms(index))
                return
            self._frame_requested.emit(preview_frame_canonical_ms(index))
            return
        self._frame_requested.emit(int(t_ms))

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
