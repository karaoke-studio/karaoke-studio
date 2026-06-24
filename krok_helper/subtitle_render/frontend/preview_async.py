"""Off-GUI-thread subtitle rasterisation for the preview (experimental).

Background (§9 A4 诊断)：预览预览的真实帧率天花板**不是单帧光栅化成本本身**，而是
字幕 paint 在 GUI 主线程上与视频呈现循环**串行**——单帧 14–20ms 的矢量/glow 栅格化
直接加进每帧周期，把 60Hz 的呈现循环拖到 ~30–35Hz（`--no-subtitle` 时循环可跑满 60）。

本模块把字幕栅格化搬到**独立工作线程**：worker 渲染进 ``QImage``，GUI 线程的
``SubtitleGraphicsItem.paint`` 只做一次廉价 blit。主循环不再被 14ms 阻塞 → 呈现回到
~60Hz；字幕内容按 worker 产出速率刷新（latest-wins 合并，丢弃过期请求）。

默认关闭，env ``KROK_SUBTITLE_ASYNC_PREVIEW=1`` 开启（实验性；导出路径不受影响）。
"""

from __future__ import annotations

import os
import threading
from typing import Optional

from PyQt6.QtCore import QObject, pyqtSignal as Signal
from PyQt6.QtGui import QImage, QPainter

from krok_helper.subtitle_render.engine.painter import paint_frame_to_painter
from krok_helper.subtitle_render.models import Style, TimingTrack


def async_preview_enabled() -> bool:
    return os.environ.get("KROK_SUBTITLE_ASYNC_PREVIEW", "0").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


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


class AsyncSubtitleRenderer(QObject):
    """Renders subtitle frames on a worker thread; emits :pyattr:`frame_ready`.

    协议：GUI 线程通过 :meth:`set_state` / :meth:`set_size` 更新轨道/样式/尺寸，
    通过 :meth:`request` 投递目标时间（latest-wins 合并）。worker 渲染完成后从工作
    线程 emit ``frame_ready(QImage, t_ms)``——接收方须用 ``QueuedConnection`` 接到
    GUI 线程槽（QImage 跨线程经队列连接复制句柄，安全）。
    """

    frame_ready = Signal(QImage, int)

    def __init__(self, width: int, height: int, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._logical_w = max(int(width), 1)
        self._logical_h = max(int(height), 1)
        self._device_pixel_ratio = 1.0
        self._track: Optional[TimingTrack] = None
        self._style: Optional[Style] = None
        self._pending_t: Optional[int] = None
        self._running = True
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._thread = threading.Thread(
            target=self._loop, name="subtitle-preview-render", daemon=True
        )
        self._thread.start()

    # ------------------------------------------------------------------ GUI API

    def set_state(self, track: Optional[TimingTrack], style: Optional[Style]) -> None:
        with self._lock:
            self._track = track
            self._style = style

    def set_size(self, width: int, height: int) -> None:
        self.set_render_target(width, height, self._device_pixel_ratio)

    def set_render_target(self, width: int, height: int, device_pixel_ratio: float = 1.0) -> None:
        with self._lock:
            self._logical_w = max(int(width), 1)
            self._logical_h = max(int(height), 1)
            self._device_pixel_ratio = max(float(device_pixel_ratio or 1.0), 0.01)

    def request(self, t_ms: int) -> None:
        """投递一帧渲染请求；只保留最新 t（合并掉过期请求）。"""
        with self._lock:
            self._pending_t = int(t_ms)
            self._cond.notify()

    def stop(self) -> None:
        with self._lock:
            self._running = False
            self._cond.notify()
        self._thread.join(timeout=2.0)

    # ------------------------------------------------------------------ worker

    def _loop(self) -> None:
        while True:
            with self._lock:
                while self._running and self._pending_t is None:
                    self._cond.wait()
                if not self._running:
                    return
                t_ms = self._pending_t
                self._pending_t = None
                track = self._track
                style = self._style
                logical_w = self._logical_w
                logical_h = self._logical_h
                dpr = self._device_pixel_ratio
            if track is None or style is None:
                continue
            physical_w, physical_h, dpr = preview_render_target_size(logical_w, logical_h, dpr)
            image = QImage(physical_w, physical_h, QImage.Format.Format_ARGB32_Premultiplied)
            image.setDevicePixelRatio(dpr)
            image.fill(0)
            painter = QPainter(image)
            try:
                paint_frame_to_painter(painter, logical_w, logical_h, track, int(t_ms), style)
            finally:
                painter.end()
            # 从工作线程 emit；接收方 QueuedConnection → 在 GUI 线程交付。
            self.frame_ready.emit(image, int(t_ms))
