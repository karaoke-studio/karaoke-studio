"""QGraphicsScene-based subtitle preview path.

This preview widget keeps the public surface of ``PreviewCanvas`` while moving
video presentation onto Qt's native multimedia item. The background video is
handled by ``QGraphicsVideoItem`` and the subtitles are painted by a transparent
``QGraphicsItem`` on top, still using the shared QPainter renderer.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Optional

from PyQt6.QtCore import QRectF, QSizeF, Qt, QUrl, pyqtSignal as Signal
from PyQt6.QtGui import QBrush, QColor, QImage, QPainter
from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer
from PyQt6.QtMultimediaWidgets import QGraphicsVideoItem
from PyQt6.QtWidgets import (
    QGraphicsItem,
    QGraphicsScene,
    QGraphicsView,
    QWidget,
)

from krok_helper.subtitle_render.engine.painter import frame_vertical_bounds, paint_frame_to_painter
from krok_helper.subtitle_render.frontend.preview_async import (
    AsyncSubtitleRenderer,
    NativeAsyncSubtitleRenderer,
    async_preview_enabled,
    native_preview_enabled,
)
from krok_helper.subtitle_render.frontend.preview_media import qt_playback_source
from krok_helper.subtitle_render.frontend.theme import palette, stage_bg, themed
from krok_helper.subtitle_render.models import Style, TimingTrack


_VIDEO_SEEK_TOLERANCE_MS = 80
"""Small playback drift allowed before forcing the preview video position."""

_ASYNC_PLAYBACK_STALE_TOLERANCE_MS = 120
"""Late subtitle frames accepted while video playback is advancing."""

_VIDEO_EDGE_OVERSCAN_PX = 4
"""Small scene-space bleed to cover native video edge underdraw while playing."""

class SubtitleGraphicsItem(QGraphicsItem):
    """Transparent subtitle layer placed above the video item."""

    def __init__(
        self,
        width: int,
        height: int,
        parent: Optional[QGraphicsItem] = None,
        on_painted: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self._width = max(int(width), 1)
        self._height = max(int(height), 1)
        self._track: Optional[TimingTrack] = None
        self._style: Style = Style()
        self._t_ms: int = 0
        self._on_painted = on_painted
        # 异步预览（§9 A4 解耦）：开启后 paint() 只 blit worker 渲染好的位图。
        self._async_mode: bool = False
        self._async_image: Optional[QImage] = None
        # FPS 只统计「新字幕帧」：同步路径按 t 去重（过滤视频区重绘触发的同 t 重栅），
        # 异步路径在 set_async_image（worker 产出新帧）处计数，不计每次 blit 重绘。
        self._last_painted_t: Optional[int] = None

    # ------------------------------------------------------------------ Qt API

    def boundingRect(self) -> QRectF:  # noqa: N802
        return QRectF(0, 0, self._width, self._height)

    def paint(self, painter: QPainter, option, widget: Optional[QWidget] = None) -> None:  # noqa: N802, ARG002
        if self._async_mode:
            # 异步路径：只 blit worker 渲染好的最新位图（廉价），不在 GUI 线程栅格化。
            # 不在此计 FPS：同一帧可能因视频区重绘被重复 blit；新帧计数在 set_async_image。
            if self._async_image is not None and not self._async_image.isNull():
                painter.drawImage(0, 0, self._async_image)
            return
        if self._track is None:
            return
        self._paint_subtitles(painter)
        # 同步路径：仅当时间推进（新内容）才计为一次字幕渲染，过滤视频重绘触发的同 t 重栅。
        if self._on_painted is not None and self._t_ms != self._last_painted_t:
            self._last_painted_t = self._t_ms
            self._on_painted()

    def set_async_mode(self, enabled: bool) -> None:
        self._async_mode = bool(enabled)

    def set_async_image(self, image: QImage) -> None:
        """GUI 线程：收到 worker 渲染好的帧 → 存最新 + 触发一次廉价 blit 重绘。"""
        self._async_image = image
        # worker 产出一帧新字幕 = 一次真实的字幕预览渲染（FPS 的真实来源，不含重 blit）。
        if self._on_painted is not None:
            self._on_painted()
        self.update()

    def clear_async_image(self) -> None:
        self._async_image = None
        self.update()

    # ------------------------------------------------------------------ public

    def set_track(self, track: Optional[TimingTrack]) -> None:
        self._track = track
        self.update()

    def set_style(self, style: Style) -> None:
        self._style = style
        self.update()

    def set_time(self, t_ms: int) -> None:
        if t_ms == self._t_ms:
            return
        if self._async_mode:
            # 异步模式下不在 GUI 线程算脏区/栅格化；由 view 投递 worker，帧到达时再 blit。
            self._t_ms = t_ms
            return
        old_dirty = self._dirty_rect_for_time(self._t_ms)
        self._t_ms = t_ms
        new_dirty = self._dirty_rect_for_time(self._t_ms)
        if old_dirty is None or new_dirty is None:
            self.update()
        else:
            self.update(old_dirty.united(new_dirty).adjusted(0, -2, 0, 2))

    def set_output_size(self, width: int, height: int) -> None:
        w = max(int(width), 1)
        h = max(int(height), 1)
        if (w, h) == (self._width, self._height):
            return
        self.prepareGeometryChange()
        self._width = w
        self._height = h
        self.update()

    @property
    def current_time_ms(self) -> int:
        return self._t_ms

    def _dirty_rect_for_time(self, t_ms: int) -> QRectF | None:
        bounds = frame_vertical_bounds(self._width, self._height, self._track, t_ms, self._style)
        if bounds is None:
            return None
        top, bottom = bounds
        top = max(0, min(top, self._height - 1))
        bottom = max(top, min(bottom, self._height - 1))
        return QRectF(0.0, float(top), float(self._width), float(bottom - top + 1))

    def _paint_subtitles(self, painter: QPainter) -> None:
        paint_frame_to_painter(
            painter,
            self._width,
            self._height,
            self._track,
            self._t_ms,
            self._style,
        )


class PreviewGraphicsView(QGraphicsView):
    """Graphics View preview with native video plus a QPainter subtitle layer."""

    framePainted = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("PreviewGraphicsView")
        self.destroyed.connect(lambda: self._stop_async_renderer())
        self.setMinimumHeight(240)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFrameShape(QGraphicsView.Shape.NoFrame)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        # QGraphicsView 默认 acceptDrops=True，会吞掉拖拽事件，导致预览被填充后无法
        # 再往播放区拖入新视频。关掉它（连同 viewport），让拖拽冒泡到外层 DropPanel。
        self.setAcceptDrops(False)
        self.viewport().setAcceptDrops(False)

        self._output_w = 1920
        self._output_h = 1080

        scene = QGraphicsScene(self)
        scene.setSceneRect(0, 0, self._output_w, self._output_h)
        self.setScene(scene)
        self._scene = scene

        # 视图外框、视图背景、场景背景三者全部用同一个舞台底色——否则视频四周会露出
        # 一圈深浅不一的细黑边（场景底色 #101010 与外框 stage_bg 撞色）。随主题刷新。
        def _stage_style() -> str:
            color = stage_bg()
            scene.setBackgroundBrush(QBrush(QColor(color)))
            self.setBackgroundBrush(QBrush(QColor(color)))
            return (
                f"#PreviewGraphicsView {{ background: {color}; "
                "border: 0; border-radius: 0; }}"
            )

        themed(self, _stage_style)

        self._video_item = QGraphicsVideoItem()
        self._video_item.setAspectRatioMode(Qt.AspectRatioMode.KeepAspectRatioByExpanding)
        self._video_item.setZValue(0)
        scene.addItem(self._video_item)
        self._fit_video_item_to_scene()

        self._subtitle_item = SubtitleGraphicsItem(
            self._output_w,
            self._output_h,
            on_painted=self.framePainted.emit,
        )
        self._subtitle_item.setZValue(10)
        scene.addItem(self._subtitle_item)

        self._video_path: Optional[Path] = None
        self._video_playing: bool = False
        self._t_ms: int = 0
        self._video_player: Optional[QMediaPlayer] = None
        self._video_audio_out: Optional[QAudioOutput] = None
        # 单播放器统一（步骤2，§10.9）：use_external_player 后视频由共享 controller 驱动，
        # 本视图不再自建/seek 视频 player；set_time 只驱动字幕层。None → 旧自建路径不变。
        self._external_player = None

        # 把字幕栅格化搬到工作线程，GUI 线程只 blit → 主呈现循环不再被
        # 单帧 14ms paint 阻塞（§9 A4 解耦）。默认开，env KROK_SUBTITLE_ASYNC_PREVIEW=0 回退。
        self._async_renderer: Optional[AsyncSubtitleRenderer] = None
        if async_preview_enabled():
            renderer_cls = NativeAsyncSubtitleRenderer if native_preview_enabled() else AsyncSubtitleRenderer
            self._async_renderer = renderer_cls(self._output_w, self._output_h, self)
            self._async_renderer.frame_ready.connect(
                self._on_async_frame, Qt.ConnectionType.QueuedConnection
            )
            self._subtitle_item.set_async_mode(True)
            self._refresh_async_target()

    # ------------------------------------------------------------------ Qt API

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        self._fit_scene_to_view()
        self._refresh_async_target()

    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        self._fit_scene_to_view()
        self._refresh_async_target()

    def closeEvent(self, event):  # noqa: N802
        self._stop_async_renderer()
        super().closeEvent(event)

    # ------------------------------------------------------------------ public

    def set_track(self, track: Optional[TimingTrack]) -> None:
        self._subtitle_item.set_track(track)
        self._subtitle_item.clear_async_image()
        self._refresh_async_state()

    def set_style(self, style: Style) -> None:
        self._subtitle_item.set_style(style)
        self._subtitle_item.clear_async_image()
        self._refresh_async_state()

    def set_time(self, t_ms: int) -> None:
        self._t_ms = t_ms
        if self._async_renderer is not None:
            self._async_renderer.request(t_ms)
        else:
            self._subtitle_item.set_time(t_ms)
        self._sync_video_position(force=not self._video_playing)

    def _refresh_async_state(self) -> None:
        if self._async_renderer is None:
            return
        self._async_renderer.set_state(self._track, self._style)
        self._async_renderer.request(self._t_ms)

    def _on_async_frame(self, image: QImage, t_ms: int) -> None:
        if int(t_ms) != int(self._t_ms):
            tolerance = _ASYNC_PLAYBACK_STALE_TOLERANCE_MS if self._video_playing else 0
            if tolerance <= 0 or abs(int(t_ms) - int(self._t_ms)) > tolerance:
                return
        self._subtitle_item.set_async_image(image)

    def set_output_size(self, width: int, height: int) -> None:
        w = max(int(width), 1)
        h = max(int(height), 1)
        if (w, h) == (self._output_w, self._output_h):
            return
        self._output_w = w
        self._output_h = h
        self._scene.setSceneRect(0, 0, w, h)
        self._fit_video_item_to_scene()
        self._subtitle_item.set_output_size(w, h)
        self._fit_scene_to_view()
        self._refresh_async_target()

    def _fit_video_item_to_scene(self) -> None:
        overscan = _VIDEO_EDGE_OVERSCAN_PX
        self._video_item.setPos(-overscan, -overscan)
        self._video_item.setSize(
            QSizeF(
                self._output_w + overscan * 2,
                self._output_h + overscan * 2,
            )
        )

    def _fit_scene_to_view(self) -> None:
        self.fitInView(
            self._scene.sceneRect(),
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
        )

    def _scene_device_pixel_ratio(self) -> float:
        # DPR-aware 渲染（af1ad4e）：worker 直接按 viewport 设备倍率（DPR × scene→viewport
        # 缩放）栅格化，GUI 等倍 blit，省掉 1920×1080→viewport device 的 smooth-scale。
        # KROK_SUBTITLE_PREVIEW_DPR_AWARE=0 回退到旧路径（worker 渲 logical、GUI 缩放），用于 A/B。
        if os.environ.get("KROK_SUBTITLE_PREVIEW_DPR_AWARE", "1").strip().lower() in (
            "0",
            "false",
            "no",
            "off",
        ):
            return 1.0
        viewport = self.viewport()
        dpr = viewport.devicePixelRatioF() if viewport is not None else self.devicePixelRatioF()
        scene_scale = abs(self.transform().m11()) or 1.0
        return max(float(dpr or 1.0) * float(scene_scale), 0.01)

    def _refresh_async_target(self) -> None:
        if self._async_renderer is None:
            return
        self._async_renderer.set_render_target(
            self._output_w,
            self._output_h,
            self._scene_device_pixel_ratio(),
        )
        self._async_renderer.request(self._t_ms)

    def _stop_async_renderer(self) -> None:
        renderer = self._async_renderer
        if renderer is None:
            return
        self._async_renderer = None
        renderer.stop()

    def use_external_player(self, controller) -> None:
        """单播放器统一：视频输出接到共享 controller，本视图不再自建/驱动视频 player。"""
        self._external_player = controller
        controller.set_video_output(self._video_item)

    def set_video_source(self, path: Optional[Path]) -> None:
        if self._external_player is not None:
            # 视频由共享 controller 驱动；本视图只记录路径，不创建/操作自己的 player。
            self._video_path = path
            return
        if self._video_player is not None:
            self._video_player.pause()
        self._video_path = path
        if path is None:
            if self._video_player is not None:
                self._video_player.setSource(QUrl())
            return
        if not path.is_file():
            return
        playback_path = qt_playback_source(path)
        player = self._ensure_video_player()
        player.setSource(QUrl.fromLocalFile(str(playback_path)))
        player.setPosition(self._t_ms)
        if self._video_playing:
            player.play()

    def set_playing(self, playing: bool) -> None:
        self._video_playing = playing
        if self._async_renderer is not None and hasattr(self._async_renderer, "set_playing"):
            self._async_renderer.set_playing(playing)
        if self._external_player is not None:
            # 播放由 TransportBar 驱动共享 controller；本视图不操作播放器。
            return
        if self._video_path is None or self._video_player is None:
            return
        if playing:
            self._sync_video_position(force=True)
            self._video_player.play()
        else:
            self._video_player.pause()
            self._sync_video_position(force=True)

    @property
    def has_video_source(self) -> bool:
        return self._video_path is not None

    @property
    def current_time_ms(self) -> int:
        return self._t_ms

    # ------------------------------------------------------------------ compat shim

    @property
    def _track(self) -> Optional[TimingTrack]:
        return self._subtitle_item._track  # noqa: SLF001

    @_track.setter
    def _track(self, track: Optional[TimingTrack]) -> None:
        self.set_track(track)

    @property
    def _style(self) -> Style:
        return self._subtitle_item._style  # noqa: SLF001

    @_style.setter
    def _style(self, style: Style) -> None:
        self.set_style(style)

    @property
    def _output_width(self) -> int:
        return self._output_w

    @_output_width.setter
    def _output_width(self, width: int) -> None:
        self.set_output_size(width, self._output_h)

    @property
    def _output_height(self) -> int:
        return self._output_h

    @_output_height.setter
    def _output_height(self, height: int) -> None:
        self.set_output_size(self._output_w, height)

    @property
    def current_video_frame(self):
        return None

    # ------------------------------------------------------------------ internal

    def _sync_video_position(self, *, force: bool = False) -> None:
        if self._external_player is not None:
            # 视频 seek 由 TransportBar→controller 统一驱动，本视图不再各自 seek。
            return
        if self._video_path is None or self._video_player is None:
            return
        current = self._video_player.position()
        if force or abs(current - self._t_ms) > _VIDEO_SEEK_TOLERANCE_MS:
            self._video_player.setPosition(self._t_ms)

    def _ensure_video_player(self) -> QMediaPlayer:
        if self._video_player is not None:
            return self._video_player
        self._video_player = QMediaPlayer(self)
        self._video_player.setVideoOutput(self._video_item)
        self._video_audio_out = QAudioOutput(self)
        self._video_audio_out.setVolume(0.0)
        self._video_player.setAudioOutput(self._video_audio_out)
        return self._video_player
