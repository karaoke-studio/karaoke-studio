"""中央预览区（视频拖入 + 字幕实时渲染画布 + 时间码 / 播放滑块）。

A4 / A7：

- :class:`PreviewPanel`：DropPanel 接受视频；同时也是字幕画布容器，加载字幕
  即翻到 "populated" 状态（DropPanel 双态用 :meth:`set_track` 主动切换）
- :class:`PreviewCanvas`：``paintEvent`` 调 :func:`paint_frame` 把当前时间
  的活跃行画到 widget 上
- :class:`TransportBar`：播放 / 暂停按钮 + 时间码 + ``QSlider``。播放时优先
  走 ``QMediaPlayer``（已加载音频时），否则退化为 ``QTimer`` 视觉 tick；emit
  :pyattr:`timeChanged` 同步给画布

后续 A8/B7 之后接入 ``QMediaPlayer.setVideoSink`` 把视频帧画到画布底层。
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PyQt6.QtCore import (
    QElapsedTimer,
    QSize,
    Qt,
    QTimer,
    QUrl,
    pyqtSignal as Signal,
)
from PyQt6.QtGui import QColor, QImage, QPainter
from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoSink
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QSlider,
    QToolButton,
    QWidget,
)

from krok_helper.subtitle_render.engine.painter import paint_frame
from krok_helper.subtitle_render.frontend.drop_panel import DropPanel
from krok_helper.subtitle_render.frontend.theme import palette, themed
from krok_helper.subtitle_render.models import Style, TimingTrack


PREVIEW_BG = QColor("#101010")
"""画布默认深色背景（A7 接入视频后这里换成视频帧）。"""

_TICK_INTERVAL_MS = 33
"""无音频时的视觉 tick 间隔（约 30fps）。"""

_VIDEO_SEEK_TOLERANCE_MS = 80
"""视频预览播放器允许的轻微漂移，超过后按播放条时间校正。"""


class PreviewCanvas(QWidget):
    """字幕预览画布：原地 ``paint_frame`` 重绘当前时刻活跃行。"""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(240)
        self._track: Optional[TimingTrack] = None
        self._style: Style = Style()
        self._t_ms: int = 0
        self._video_path: Optional[Path] = None
        self._video_image: Optional[QImage] = None
        self._video_playing: bool = False
        self._video_sink: Optional[QVideoSink] = None
        self._video_player: Optional[QMediaPlayer] = None
        self._video_audio_out: Optional[QAudioOutput] = None

        themed(
            self,
            lambda: (
                f"PreviewCanvas {{ background: {palette().preview_bg}; "
                f"border: 1px solid {palette().preview_border}; "
                f"border-radius: 6px; }}"
            ),
        )

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
        self._t_ms = t_ms
        self._sync_video_position(force=not self._video_playing)
        self.update()

    @property
    def current_time_ms(self) -> int:
        return self._t_ms

    def set_video_source(self, path: Optional[Path]) -> None:
        """Load / clear the background video used by the preview canvas."""
        if self._video_player is not None:
            self._video_player.pause()
        self._video_path = path
        self._video_image = None
        if path is None:
            if self._video_player is not None:
                self._video_player.setSource(QUrl())
            self.update()
            return
        if not path.is_file():
            self.update()
            return
        player = self._ensure_video_player()
        player.setSource(QUrl.fromLocalFile(str(path)))
        player.setPosition(self._t_ms)
        if self._video_playing:
            player.play()
        self.update()

    def set_playing(self, playing: bool) -> None:
        """Mirror the transport state into the silent video preview player."""
        self._video_playing = playing
        if self._video_path is None:
            return
        if playing:
            self._sync_video_position(force=True)
            if self._video_player is not None:
                self._video_player.play()
        else:
            if self._video_player is not None:
                self._video_player.pause()
            self._sync_video_position(force=True)

    @property
    def has_video_source(self) -> bool:
        return self._video_path is not None

    @property
    def current_video_frame(self) -> Optional[QImage]:
        return self._video_image

    # ------------------------------------------------------------------ paint

    def paintEvent(self, event):  # noqa: N802 — Qt API
        w = max(self.width(), 1)
        h = max(self.height(), 1)
        # 先离屏渲染到 QImage，再 blit 到 widget——保持渲染路径与导出管线一致
        image = QImage(w, h, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(PREVIEW_BG)
        self._paint_background_video(image)
        paint_frame(image, self._track, self._t_ms, self._style)
        painter = QPainter(self)
        try:
            painter.drawImage(0, 0, image)
        finally:
            painter.end()

    # ------------------------------------------------------------------ video

    def _on_video_frame_changed(self, frame) -> None:
        if not frame.isValid():
            return
        image = frame.toImage()
        if image.isNull():
            return
        self._video_image = image.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
        self.update()

    def _sync_video_position(self, *, force: bool = False) -> None:
        if self._video_path is None or self._video_player is None:
            return
        current = self._video_player.position()
        if force or abs(current - self._t_ms) > _VIDEO_SEEK_TOLERANCE_MS:
            self._video_player.setPosition(self._t_ms)

    def _ensure_video_player(self) -> QMediaPlayer:
        if self._video_player is not None:
            return self._video_player
        self._video_sink = QVideoSink(self)
        self._video_sink.videoFrameChanged.connect(self._on_video_frame_changed)
        self._video_player = QMediaPlayer(self)
        self._video_player.setVideoSink(self._video_sink)
        self._video_audio_out = QAudioOutput(self)
        self._video_audio_out.setVolume(0.0)
        self._video_player.setAudioOutput(self._video_audio_out)
        return self._video_player

    def _paint_background_video(self, target: QImage) -> None:
        if self._video_image is None or self._video_image.isNull():
            return
        frame = self._video_image.scaled(
            QSize(target.width(), target.height()),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        x = (target.width() - frame.width()) // 2
        y = (target.height() - frame.height()) // 2
        painter = QPainter(target)
        try:
            painter.drawImage(x, y, frame)
        finally:
            painter.end()


class PreviewPanel(DropPanel):
    """预览面板：空态拖入视频 / populated 后显示画布。"""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(
            extensions={".mp4", ".mkv", ".mov", ".webm", ".avi", ".flv"},
            empty_title="拖入背景视频",
            empty_hint="支持 .mp4 / .mkv / .mov / .webm 等\n或点击此处选择\n\n（仅加载字幕也可直接预览）",
            empty_icon="🎬",
            parent=parent,
        )
        self._canvas = PreviewCanvas()
        self.set_content(self._canvas)

    # ------------------------------------------------------------------ public

    def set_track(self, track: Optional[TimingTrack]) -> None:
        """加载字幕后调：切到 populated 状态并把 track 喂给画布。"""
        self._canvas.set_track(track)
        if track is not None and track.lines:
            self.set_populated(True)

    def set_time(self, t_ms: int) -> None:
        self._canvas.set_time(t_ms)

    def set_style(self, style: Style) -> None:
        self._canvas.set_style(style)

    def set_video_source(self, path: Optional[Path]) -> None:
        self._canvas.set_video_source(path)
        if path is not None:
            self.set_populated(True)

    def set_playing(self, playing: bool) -> None:
        self._canvas.set_playing(playing)

    @property
    def canvas(self) -> PreviewCanvas:
        return self._canvas


class TransportBar(QWidget):
    """播放控件 + 时间码 + 进度条。

    播放路径：
    - **有音频** → ``QMediaPlayer`` 推音频流，``positionChanged`` 反馈滑块位置
    - **无音频** → 内部 ``QTimer`` 30fps tick，根据 ``QElapsedTimer`` 累加时间

    任意一条路径推进 → 同步刷新滑块 → emit :pyattr:`timeChanged`，画布订阅
    重绘。用户拖滑块也走同一条路径回写到 player / 重置 tick 锚点。
    """

    timeChanged = Signal(int)
    """滑块拖动 / 程序设值 / 播放推进时 emit 当前时间（毫秒）。"""

    playbackStateChanged = Signal(bool)
    """播放 / 暂停状态变化时 emit，供视频预览层同步静音视频播放器。"""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("TransportBar")
        self.setFixedHeight(44)
        themed(
            self,
            lambda: (
                f"#TransportBar {{ background: transparent; "
                f"border-top: 1px solid {palette().card_border}; }}"
            ),
        )

        # ── 子控件 ─────────────────────────────────────────────────
        self._play_btn = QToolButton(self)
        self._play_btn.setText("▶")
        self._play_btn.setToolTip("播放 / 暂停 (Space)")
        self._play_btn.setFixedSize(32, 32)
        self._play_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        themed(
            self._play_btn,
            lambda: (
                f"""
                QToolButton {{
                    background: {palette().secondary_button_bg};
                    color: {palette().text_primary};
                    border: 1px solid {palette().secondary_button_border};
                    border-radius: 6px;
                    font-size: 12pt;
                }}
                QToolButton:hover {{
                    background: {palette().secondary_button_hover_bg};
                    border-color: {palette().secondary_button_hover_border};
                }}
                QToolButton:pressed {{
                    background: {palette().secondary_button_pressed_bg};
                }}
                """
            ),
        )
        self._play_btn.clicked.connect(self.toggle_play)

        self._slider = QSlider(Qt.Orientation.Horizontal, self)
        self._slider.setMinimum(0)
        self._slider.setMaximum(60_000)
        self._slider.setValue(0)
        self._slider.setSingleStep(50)
        self._slider.setPageStep(1000)
        self._slider.valueChanged.connect(self._on_slider_changed)

        self._timecode = QLabel("00:00.00", self)
        themed(
            self._timecode,
            lambda: (
                f"color: {palette().text_primary}; "
                f'font-family: "Consolas", "Courier New", monospace; '
                f"font-size: 10pt;"
            ),
        )
        self._timecode.setFixedWidth(80)
        self._timecode.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        # ── 布局 ───────────────────────────────────────────────────
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 6, 12, 6)
        layout.setSpacing(10)
        layout.addWidget(self._play_btn)
        layout.addWidget(self._slider, 1)
        layout.addWidget(self._timecode)

        # ── 音频播放（QMediaPlayer） ────────────────────────────────
        self._player = QMediaPlayer(self)
        self._audio_out = QAudioOutput(self)
        self._player.setAudioOutput(self._audio_out)
        self._has_audio = False
        self._player.positionChanged.connect(self._on_player_position)
        self._player.playbackStateChanged.connect(self._on_player_state_changed)

        # ── 无音频时的视觉 tick ─────────────────────────────────────
        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(_TICK_INTERVAL_MS)
        self._tick_timer.timeout.connect(self._on_tick)
        self._tick_anchor_ms: int = 0
        self._tick_anchor_real = QElapsedTimer()

        # 抑制 player ↔ slider 反馈环
        self._suppress_seek: bool = False
        self._playing_state: bool = False

    # ------------------------------------------------------------------ public API

    def set_duration(self, ms: int) -> None:
        """设置时间轴总长（毫秒），决定滑块最大值。"""
        ms = max(ms, 1000)
        self._slider.setMaximum(ms)

    def set_time(self, ms: int) -> None:
        """程序设置当前时间，会触发 :pyattr:`timeChanged`。"""
        ms = max(0, min(ms, self._slider.maximum()))
        if ms == self._slider.value():
            self._update_timecode(ms)
            return
        self._slider.setValue(ms)  # 触发 valueChanged → timeChanged

    def set_audio_source(self, path: Optional[Path]) -> None:
        """喂音频文件给 ``QMediaPlayer``；``None`` 清空。"""
        was_playing = self.is_playing()
        self.pause()
        if path is None:
            self._player.setSource(QUrl())
            self._has_audio = False
            return
        self._player.setSource(QUrl.fromLocalFile(str(path)))
        self._has_audio = True
        # 切音源后回到 0 而不是续播旧位置
        self._player.setPosition(0)
        if was_playing:
            self.play()

    def play(self) -> None:
        """开始播放。有音频用 ``QMediaPlayer``；无音频走视觉 tick。"""
        if self._has_audio:
            self._player.setPosition(self._slider.value())
            self._player.play()
        else:
            self._tick_anchor_ms = self._slider.value()
            self._tick_anchor_real.start()
            self._tick_timer.start()
        self._update_play_button(True)

    def pause(self) -> None:
        """暂停。"""
        if self._has_audio:
            self._player.pause()
        self._tick_timer.stop()
        self._update_play_button(False)

    def toggle_play(self) -> None:
        if self.is_playing():
            self.pause()
        else:
            self.play()

    def is_playing(self) -> bool:
        if self._has_audio:
            return (
                self._player.playbackState()
                == QMediaPlayer.PlaybackState.PlayingState
            )
        return self._tick_timer.isActive()

    @property
    def current_time_ms(self) -> int:
        return self._slider.value()

    # ------------------------------------------------------------------ events

    def _on_slider_changed(self, value: int) -> None:
        self._update_timecode(value)
        self.timeChanged.emit(value)
        if self._suppress_seek:
            return
        # 用户拖动 / 外部 set_time → 同步给 player / tick 锚点
        if self._has_audio:
            self._player.setPosition(value)
        if self._tick_timer.isActive():
            self._tick_anchor_ms = value
            self._tick_anchor_real.restart()

    def _on_player_position(self, ms: int) -> None:
        if not self._has_audio:
            return
        # 反馈到滑块，但抑制下游回写 player（避免循环 seek）
        self._set_slider_silently(ms)

    def _on_player_state_changed(self, state) -> None:
        if state == QMediaPlayer.PlaybackState.StoppedState and self._has_audio:
            # 音频自然结束 → 复位按钮
            self._update_play_button(False)

    def _on_tick(self) -> None:
        elapsed = self._tick_anchor_real.elapsed()
        target = self._tick_anchor_ms + int(elapsed)
        if target >= self._slider.maximum():
            target = self._slider.maximum()
            self._set_slider_silently(target)
            self.pause()
            return
        self._set_slider_silently(target)

    # ------------------------------------------------------------------ helpers

    def _set_slider_silently(self, ms: int) -> None:
        """更新滑块位置但不让 _on_slider_changed 反向 seek 来源。"""
        self._suppress_seek = True
        try:
            self._slider.setValue(ms)
        finally:
            self._suppress_seek = False

    def _update_timecode(self, ms: int) -> None:
        total_cs = ms // 10
        minutes = total_cs // 6000
        seconds = (total_cs % 6000) // 100
        centiseconds = total_cs % 100
        self._timecode.setText(f"{minutes:02d}:{seconds:02d}.{centiseconds:02d}")

    def _update_play_button(self, playing: bool) -> None:
        if playing != self._playing_state:
            self._playing_state = playing
            self.playbackStateChanged.emit(playing)
        self._play_btn.setText("⏸" if playing else "▶")
