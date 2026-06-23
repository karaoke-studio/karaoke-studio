"""QGraphicsScene-based subtitle preview path.

This preview widget keeps the public surface of ``PreviewCanvas`` while moving
video presentation onto Qt's native multimedia item. The background video is
handled by ``QGraphicsVideoItem`` and the subtitles are painted by a transparent
``QGraphicsItem`` on top, still using the shared QPainter renderer.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from PyQt6.QtCore import QRectF, QSizeF, Qt, QUrl, pyqtSignal as Signal
from PyQt6.QtGui import QBrush, QColor, QPainter
from PyQt6.QtGui import QImage
from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer
from PyQt6.QtMultimediaWidgets import QGraphicsVideoItem
from PyQt6.QtWidgets import (
    QGraphicsItem,
    QGraphicsScene,
    QGraphicsView,
    QWidget,
)

from krok_helper.subtitle_render.engine.painter import frame_vertical_bounds, paint_frame_to_painter
from krok_helper.subtitle_render.frontend.preview_media import qt_playback_source
from krok_helper.subtitle_render.frontend.theme import palette, stage_bg, themed
from krok_helper.subtitle_render.models import Style, TimingTrack


_VIDEO_SEEK_TOLERANCE_MS = 80
"""Small playback drift allowed before forcing the preview video position."""

_VIDEO_EDGE_OVERSCAN_PX = 4
"""Small scene-space bleed to cover native video edge underdraw while playing."""


def _normalize_preview_scale(scale: float) -> float:
    try:
        value = float(scale)
    except (TypeError, ValueError):
        return 1.0
    if value <= 0.30:
        return 0.25
    if value <= 0.75:
        return 0.5
    return 1.0


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
        self._preview_scale: float = 1.0
        self._on_painted = on_painted

    # ------------------------------------------------------------------ Qt API

    def boundingRect(self) -> QRectF:  # noqa: N802
        return QRectF(0, 0, self._width, self._height)

    def paint(self, painter: QPainter, option, widget: Optional[QWidget] = None) -> None:  # noqa: N802, ARG002
        if self._track is None:
            return
        self._paint_subtitles(painter)
        if self._on_painted is not None:
            self._on_painted()

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

    def set_preview_scale(self, scale: float) -> None:
        self._preview_scale = _normalize_preview_scale(scale)
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
        if self._preview_scale >= 0.999:
            paint_frame_to_painter(
                painter,
                self._width,
                self._height,
                self._track,
                self._t_ms,
                self._style,
            )
            return
        low_w = max(int(round(self._width * self._preview_scale)), 1)
        low_h = max(int(round(self._height * self._preview_scale)), 1)
        layer = QImage(low_w, low_h, QImage.Format.Format_ARGB32_Premultiplied)
        layer.fill(QColor(0, 0, 0, 0))
        layer_painter = QPainter(layer)
        try:
            layer_painter.scale(self._preview_scale, self._preview_scale)
            paint_frame_to_painter(
                layer_painter,
                self._width,
                self._height,
                self._track,
                self._t_ms,
                self._style,
            )
        finally:
            layer_painter.end()
        painter.drawImage(QRectF(0, 0, self._width, self._height), layer)


class PreviewGraphicsView(QGraphicsView):
    """Graphics View preview with native video plus a QPainter subtitle layer."""

    framePainted = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("PreviewGraphicsView")
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

    # ------------------------------------------------------------------ Qt API

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        self._fit_scene_to_view()

    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        self._fit_scene_to_view()

    # ------------------------------------------------------------------ public

    def set_track(self, track: Optional[TimingTrack]) -> None:
        self._subtitle_item.set_track(track)

    def set_style(self, style: Style) -> None:
        self._subtitle_item.set_style(style)

    def set_time(self, t_ms: int) -> None:
        self._t_ms = t_ms
        self._subtitle_item.set_time(t_ms)
        self._sync_video_position(force=not self._video_playing)

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

    def set_preview_scale(self, scale: float) -> None:
        self._subtitle_item.set_preview_scale(scale)

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

    def set_video_source(self, path: Optional[Path]) -> None:
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
