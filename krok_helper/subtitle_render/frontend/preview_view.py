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

import os
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import (
    QElapsedTimer,
    QRectF,
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

from krok_helper.subtitle_render.engine.painter import paint_frame_to_painter
from krok_helper.subtitle_render.frontend.drop_panel import DropPanel
from krok_helper.subtitle_render.frontend.preview_media import qt_playback_source
from krok_helper.subtitle_render.frontend.theme import palette, stage_bg, themed
from krok_helper.subtitle_render.models import Style, TimingTrack


PREVIEW_BG = QColor("#101010")
"""画布默认深色背景（A7 接入视频后这里换成视频帧）。"""

_DEFAULT_PREVIEW_FPS = 60
_TICK_INTERVAL_MS = int(1000 / _DEFAULT_PREVIEW_FPS)
"""默认 tick / 位置轮询间隔（~60Hz，对齐主流显示器 vsync）。

历史上这里曾是 8ms（120Hz）来追求"丝滑"，但实测：

- Qt 的 ``QWidget.update()`` 会自动 coalesce 到下一次 paintEvent，超过 vsync
  的额外 tick 全部被合并丢弃——重绘并不会变快
- 但 Python 端 ``elapsed → setValue → valueChanged → timeChanged → set_time``
  那条 signal-slot 链每次 tick 都要完整走一遍；125Hz 比 60Hz 多一倍纯
  Python 开销，挤占了 paintEvent 自己的时间
- 现在固定 16ms：保证 60Hz 时间精度（卡拉ok 填色单帧间隔），同时把时钟
  路径的 Python 开销直接砍半

无音频路径与有音频路径共用此常量。有音频路径下 QMediaPlayer 本身的
``positionChanged`` 粒度约 100ms，所以仍然需要这一档高频轮询配 elapsed
时钟做插值；只是不再过度采样。"""

_VIDEO_SEEK_TOLERANCE_MS = 80
"""视频预览播放器允许的轻微漂移，超过后按播放条时间校正。"""

# 音频锚定时钟（KROK_SUBTITLE_AUDIO_CLOCK，默认开，=0 回退纯墙钟）：播放时把墙钟插值出来的
# UI 时间周期性向 QMediaPlayer.position()（音频真实播放位置）收敛，使字幕/视频跟随**音频**
# 而非自走墙钟——根治「字幕跑在音频前 / 音画失步」（§10 诉求 #3）。当未来统一成单播放器时，
# 这套收敛逻辑原样复用（只是 position 来自那个唯一播放器）。
_AUDIO_CLOCK_RESYNC_MS = 250
"""偏差 > 此值（如卡顿 / seek 后）→ 直接吸附到音频位置。"""
_AUDIO_CLOCK_DEADBAND_MS = 30
"""偏差 ≤ 此值视为正常抖动，不纠（避免来回纠偏自激抖动）。"""
_AUDIO_CLOCK_GAIN = 0.1
"""中等偏差按比例缓慢收敛（每 tick 纠 10%），不产生可见跳变。"""


def _audio_clock_enabled() -> bool:
    return os.environ.get("KROK_SUBTITLE_AUDIO_CLOCK", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def _audio_clock_anchor_correction(target_ms: int, audio_pos_ms: int) -> int:
    """让墙钟外推时间向音频真实位置收敛时，应施加到锚点的校正量（ms）。

    纯函数（便于无 Qt 单测）。``drift = 音频位置 − 当前墙钟外推时间``：
    - ``|drift| ≤ deadband``：返回 0（正常抖动不纠）；
    - ``|drift| > resync``：返回整个 drift（大偏差直接吸附，如卡顿 / seek 后）；
    - 其间：返回 drift 的一个小比例（缓慢收敛，无可见跳变）。

    常见的「字幕跑在音频前」= 墙钟比音频快 → ``drift < 0`` 小负值 → 轻微回拉，平滑消除。
    """
    drift = audio_pos_ms - target_ms
    if abs(drift) <= _AUDIO_CLOCK_DEADBAND_MS:
        return 0
    if abs(drift) > _AUDIO_CLOCK_RESYNC_MS:
        return drift
    return int(drift * _AUDIO_CLOCK_GAIN)


class PreviewCanvas(QWidget):
    """字幕预览画布：原地 ``paint_frame`` 重绘当前时刻活跃行。"""

    framePainted = Signal()
    """预览字幕层完成一次实际 paint，用于统计实时 FPS。"""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(240)
        self._track: Optional[TimingTrack] = None
        self._style: Style = Style()
        self._t_ms: int = 0
        self._video_path: Optional[Path] = None
        self._video_image: Optional[QImage] = None
        self._scaled_video_image: Optional[QImage] = None
        self._scaled_video_key: Optional[tuple[int, int, int, int]] = None
        self._video_playing: bool = False
        self._video_sink: Optional[QVideoSink] = None
        self._video_player: Optional[QMediaPlayer] = None
        self._video_audio_out: Optional[QAudioOutput] = None
        self._output_width: int = 1920
        self._output_height: int = 1080

        themed(
            self,
            lambda: (
                f"PreviewCanvas {{ background: {stage_bg()}; "
                "border: 0; "
                "border-radius: 0; }}"
            ),
        )

    # ------------------------------------------------------------------ public

    def set_track(self, track: Optional[TimingTrack]) -> None:
        self._track = track
        self.update()

    def set_style(self, style: Style) -> None:
        self._style = style
        self.update()

    def set_output_size(self, width: int, height: int) -> None:
        self._output_width = max(int(width), 1)
        self._output_height = max(int(height), 1)
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
        self._scaled_video_image = None
        self._scaled_video_key = None
        if path is None:
            if self._video_player is not None:
                self._video_player.setSource(QUrl())
            self.update()
            return
        if not path.is_file():
            self.update()
            return
        playback_path = qt_playback_source(path)
        player = self._ensure_video_player()
        player.setSource(QUrl.fromLocalFile(str(playback_path)))
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
        dpr = self.devicePixelRatioF() or 1.0
        logical_w = max(self.width(), 1)
        logical_h = max(self.height(), 1)
        painter = QPainter(self)
        try:
            painter.fillRect(self.rect(), PREVIEW_BG)
            target = self._paint_background_video(painter, logical_w, logical_h, dpr)
            if target is None:
                target = self._fit_output_rect(logical_w, logical_h)
            x, y, target_w, target_h = target
            painter.save()
            try:
                painter.setClipRect(QRectF(x, y, target_w, target_h))
                painter.translate(x, y)
                painter.scale(
                    target_w / self._output_width,
                    target_h / self._output_height,
                )
                self._paint_subtitles(painter)
            finally:
                painter.restore()
        finally:
            painter.end()
        self.framePainted.emit()

    def _paint_subtitles(self, painter: QPainter) -> None:
        paint_frame_to_painter(
            painter,
            self._output_width,
            self._output_height,
            self._track,
            self._t_ms,
            self._style,
        )

    # ------------------------------------------------------------------ video

    def _on_video_frame_changed(self, frame) -> None:
        if not frame.isValid():
            return
        image = frame.toImage()
        if image.isNull():
            return
        self._video_image = image.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
        self._scaled_video_image = None
        self._scaled_video_key = None
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

    def _scaled_background_video(
        self,
        logical_w: int,
        logical_h: int,
        dpr: float,
    ) -> Optional[QImage]:
        if self._video_image is None or self._video_image.isNull():
            return None
        phys_w = max(int(round(logical_w * dpr)), 1)
        phys_h = max(int(round(logical_h * dpr)), 1)
        # 缓存 DPR-aware 视频帧：物理尺寸 = 逻辑 × dpr。
        # 直接按物理像素缩放视频帧 → 物理分辨率渲染 → 不糊；最后用物理坐标
        # 绘制（painter.drawImage 在 dpr-aware image 上默认是逻辑坐标系，
        # 这里把 frame 自身也 setDevicePixelRatio 同步，绘制时就按逻辑落点）。
        dpr_key = int(round(dpr * 1000))
        cache_key = (
            int(self._video_image.cacheKey()),
            phys_w,
            phys_h,
            dpr_key,
        )
        frame = self._scaled_video_image
        if frame is None or self._scaled_video_key != cache_key:
            frame = self._video_image.scaled(
                QSize(phys_w, phys_h),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            frame.setDevicePixelRatio(dpr)
            self._scaled_video_image = frame
            self._scaled_video_key = cache_key
        return frame

    def _paint_background_video(
        self,
        painter: QPainter,
        logical_w: int,
        logical_h: int,
        dpr: float,
    ) -> Optional[tuple[int, int, int, int]]:
        frame = self._scaled_background_video(logical_w, logical_h, dpr)
        if frame is None:
            return None
        # 居中：用逻辑坐标
        logical_frame_w = int(round(frame.width() / dpr))
        logical_frame_h = int(round(frame.height() / dpr))
        x = (logical_w - logical_frame_w) // 2
        y = (logical_h - logical_frame_h) // 2
        painter.drawImage(x, y, frame)
        return (x, y, logical_frame_w, logical_frame_h)

    def _fit_output_rect(self, logical_w: int, logical_h: int) -> tuple[int, int, int, int]:
        output_aspect = self._output_width / self._output_height
        widget_aspect = logical_w / logical_h
        if widget_aspect >= output_aspect:
            target_h = logical_h
            target_w = int(round(target_h * output_aspect))
        else:
            target_w = logical_w
            target_h = int(round(target_w / output_aspect))
        x = (logical_w - target_w) // 2
        y = (logical_h - target_h) // 2
        return (x, y, max(target_w, 1), max(target_h, 1))


def _use_graphics_preview() -> bool:
    """默认走档2 ``QGraphicsView`` 路径——vsync 对齐 + 视频走 Qt 原生 GPU。

    设环境变量 ``KROK_SUBTITLE_PREVIEW=raster`` 可强制回退到旧版手画式
    :class:`PreviewCanvas`，便于 A/B 对比或回退。
    """
    return os.environ.get("KROK_SUBTITLE_PREVIEW", "graphics").lower() != "raster"


class PreviewPanel(DropPanel):
    """预览面板：空态拖入视频 / populated 后显示画布。

    内部 canvas 默认是 :class:`PreviewGraphicsView`（档2 QGraphicsView 路径）。
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(
            extensions={".mp4", ".mkv", ".mov", ".webm", ".avi", ".flv"},
            empty_title="拖入背景视频",
            empty_hint="支持 .mp4 / .mkv / .mov / .webm 等\n或点击此处选择\n\n（仅加载字幕也可直接预览）",
            empty_icon="🎬",
            parent=parent,
        )
        if _use_graphics_preview():
            # 延迟 import：QGraphicsVideoItem 依赖 QtMultimediaWidgets，
            # 测试 raster 路径时不需要加载。
            from krok_helper.subtitle_render.frontend.preview_graphics import (
                PreviewGraphicsView,
            )
            self._canvas = PreviewGraphicsView()
        else:
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

    def set_output_size(self, width: int, height: int) -> None:
        self._canvas.set_output_size(width, height)

    def set_video_source(self, path: Optional[Path]) -> None:
        self._canvas.set_video_source(path)
        if path is not None:
            self.set_populated(True)

    def set_playing(self, playing: bool) -> None:
        self._canvas.set_playing(playing)

    def use_external_player(self, controller) -> bool:
        """单播放器统一：把画布视频输出接到共享 controller。返回是否成功接上。

        当前仅 graphics 画布支持；raster 回退暂不支持（返回 False，调用方据此回退旧路径）。
        """
        canvas = self._canvas
        if hasattr(canvas, "use_external_player"):
            canvas.use_external_player(controller)
            return True
        return False

    @property
    def canvas(self) -> PreviewCanvas:
        return self._canvas


class TransportBar(QWidget):
    """播放控件 + 时间码 + 进度条。

    播放路径：
    - **有音频** → ``QMediaPlayer`` 推音频流，``positionChanged`` 反馈滑块位置
    - **无音频** → 内部 ``QTimer`` 视觉 tick，根据 ``QElapsedTimer`` 累加时间

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

        self._fps_label = QLabel("FPS --", self)
        themed(
            self._fps_label,
            lambda: (
                f"color: {palette().text_secondary}; "
                f'font-family: "Consolas", "Courier New", monospace; '
                f"font-size: 9.5pt;"
            ),
        )
        self._fps_label.setFixedWidth(58)
        self._fps_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        # ── 布局 ───────────────────────────────────────────────────
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 6, 12, 6)
        layout.setSpacing(10)
        layout.addWidget(self._play_btn)
        layout.addWidget(self._slider, 1)
        layout.addWidget(self._timecode)
        layout.addWidget(self._fps_label)

        # ── 音频播放（QMediaPlayer，真实非空音频文件才懒创建） ────────
        self._player: Optional[QMediaPlayer] = None
        self._audio_out: Optional[QAudioOutput] = None
        self._has_audio = False
        # 单播放器统一（步骤2，§10.9）：attach_playback_controller 后用共享 controller
        # 取代自建音频 player（同一个播放器同时驱动音视频）。controller=None → 旧路径完全不变。
        self._controller = None

        # ── 无音频时的视觉 tick ─────────────────────────────────────
        self._preview_fps = _DEFAULT_PREVIEW_FPS
        self._tick_timer = QTimer(self)
        self._tick_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._tick_timer.setInterval(_TICK_INTERVAL_MS)
        self._tick_timer.timeout.connect(self._on_tick)
        self._tick_anchor_ms: int = 0
        self._tick_anchor_real = QElapsedTimer()

        # ── 有音频时的预览主时钟 ─────────────────────────────────
        # QMediaPlayer.position()/positionChanged 在 Windows 后端上经常只有几十
        # ms 甚至 100ms 粒度。字幕填色要稳定接近预览帧率，所以播放期用
        # QElapsedTimer 推进 UI 时间，QMediaPlayer 只负责音频输出。
        self._position_poll_timer = QTimer(self)
        self._position_poll_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._position_poll_timer.setInterval(_TICK_INTERVAL_MS)
        self._position_poll_timer.timeout.connect(self._on_audio_clock_tick)

        # 抑制 player ↔ slider 反馈环
        self._suppress_seek: bool = False
        self._playing_state: bool = False
        self._fps_timer = QElapsedTimer()
        self._fps_window_frames = 0
        self._fps_timer.start()

    # ------------------------------------------------------------------ public API

    def set_duration(self, ms: int) -> None:
        """设置时间轴总长（毫秒），决定滑块最大值。"""
        ms = max(ms, 1000)
        self._slider.setMaximum(ms)

    def set_preview_fps(self, fps: int) -> None:
        """设置预览播放时钟帧率；当前只允许 60 / 120fps。"""
        normalized = 120 if int(fps) == 120 else 60
        self._preview_fps = normalized
        interval = max(1, int(1000 / normalized))
        self._tick_timer.setInterval(interval)
        self._position_poll_timer.setInterval(interval)

    def set_time(self, ms: int) -> None:
        """程序设置当前时间，会触发 :pyattr:`timeChanged`。"""
        ms = max(0, min(ms, self._slider.maximum()))
        if ms == self._slider.value():
            self._update_timecode(ms)
            return
        self._slider.setValue(ms)  # 触发 valueChanged → timeChanged

    def attach_playback_controller(self, controller) -> None:
        """单播放器统一（步骤2）：用共享 PlaybackController 取代自建音频 player。

        attach 后 set_audio_source / play / pause / seek / 时钟都走 controller（同一个
        播放器同时驱动音视频）；不 attach（controller=None）时旧的自建音频 player 路径完全不变。
        """
        self._controller = controller

    def _use_controller(self) -> bool:
        return self._controller is not None and self._controller.has_media()

    def set_audio_source(self, path: Optional[Path]) -> None:
        """喂音频文件给播放器；``None`` 清空。attach controller 后交给共享 controller。"""
        was_playing = self.is_playing()
        self.pause()
        if self._controller is not None:
            self._controller.set_media(path)
            if was_playing and self._controller.has_media():
                self.play()
            return
        if path is None:
            if self._player is not None:
                self._player.setSource(QUrl())
            self._has_audio = False
            return
        if not _is_real_media_file(path):
            if self._player is not None:
                self._player.setSource(QUrl())
            self._has_audio = False
            return
        playback_path = qt_playback_source(path)
        player = self._ensure_audio_player()
        player.setSource(QUrl.fromLocalFile(str(playback_path)))
        self._has_audio = True
        # 切音源后回到 0 而不是续播旧位置
        player.setPosition(0)
        if was_playing:
            self.play()

    def play(self) -> None:
        """开始播放。有音频用 ``QMediaPlayer``；无音频走视觉 tick。"""
        self._tick_anchor_ms = self._slider.value()
        self._tick_anchor_real.start()
        if self._use_controller():
            self._controller.seek(self._tick_anchor_ms)
            self._controller.play()
            self._position_poll_timer.start()
        elif self._has_audio:
            player = self._ensure_audio_player()
            player.setPosition(self._tick_anchor_ms)
            player.play()
            self._position_poll_timer.start()
        else:
            self._tick_timer.start()
        self._update_play_button(True)

    def pause(self) -> None:
        """暂停。"""
        if self._use_controller():
            self._controller.pause()
        elif self._has_audio and self._player is not None:
            self._player.pause()
        self._tick_timer.stop()
        self._position_poll_timer.stop()
        self._update_play_button(False)

    def toggle_play(self) -> None:
        if self.is_playing():
            self.pause()
        else:
            self.play()

    def is_playing(self) -> bool:
        if self._use_controller():
            return self._controller.is_playing()
        if self._has_audio and self._player is not None:
            return (
                self._player.playbackState()
                == QMediaPlayer.PlaybackState.PlayingState
            )
        return self._tick_timer.isActive()

    @property
    def current_time_ms(self) -> int:
        return self._slider.value()

    def note_preview_frame_painted(self) -> None:
        """Record one real preview paint and refresh the displayed FPS."""
        if not self._fps_timer.isValid():
            self._fps_timer.start()
        self._fps_window_frames += 1
        elapsed = self._fps_timer.elapsed()
        if elapsed < 500:
            return
        fps = self._fps_window_frames * 1000.0 / max(elapsed, 1)
        self._fps_label.setText(f"FPS {fps:02.0f}")
        self._fps_window_frames = 0
        self._fps_timer.restart()

    # ------------------------------------------------------------------ events

    def _on_slider_changed(self, value: int) -> None:
        self._update_timecode(value)
        self.timeChanged.emit(value)
        if self._suppress_seek:
            return
        # 用户拖动 / 外部 set_time → 同步给 player / tick 锚点
        if self._use_controller():
            self._controller.seek(value)
        elif self._has_audio and self._player is not None:
            self._player.setPosition(value)
        if self._tick_timer.isActive() or self._position_poll_timer.isActive():
            self._tick_anchor_ms = value
            self._tick_anchor_real.restart()

    def _on_player_position(self, ms: int) -> None:
        if not self._has_audio:
            return
        if self._position_poll_timer.isActive():
            return
        # 反馈到滑块，但抑制下游回写 player（避免循环 seek）
        self._set_slider_silently(ms)

    def _on_player_state_changed(self, state) -> None:
        if state == QMediaPlayer.PlaybackState.StoppedState and self._has_audio:
            # 音频自然结束 → 停轮询 + 复位按钮
            self._position_poll_timer.stop()
            self._update_play_button(False)

    def _on_audio_clock_tick(self) -> None:
        if self._use_controller():
            audio_pos_source = self._controller.position()
        elif self._player is not None and self._has_audio:
            audio_pos_source = self._player.position()
        else:
            return
        elapsed = self._tick_anchor_real.elapsed()
        target = self._tick_anchor_ms + int(elapsed)
        # 向音频真实播放位置收敛（默认开）：字幕/视频跟随音频，不自走墙钟 → 不会跑在音频前。
        if _audio_clock_enabled():
            audio_pos = audio_pos_source
            if audio_pos > 0:
                correction = _audio_clock_anchor_correction(target, audio_pos)
                if correction:
                    self._tick_anchor_ms += correction
                    target += correction
        if target >= self._slider.maximum():
            target = self._slider.maximum()
            self._set_slider_silently(target)
            self.pause()
            return
        self._set_slider_silently(target)

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

    def _ensure_audio_player(self) -> QMediaPlayer:
        if self._player is not None:
            return self._player
        self._player = QMediaPlayer(self)
        self._audio_out = QAudioOutput(self)
        self._player.setAudioOutput(self._audio_out)
        self._player.positionChanged.connect(self._on_player_position)
        self._player.playbackStateChanged.connect(self._on_player_state_changed)
        return self._player


def _is_real_media_file(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False
