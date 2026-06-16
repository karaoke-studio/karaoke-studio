"""中央预览区（视频拖入 + 字幕实时渲染画布 + 时间码 / 滑块）。

A4 阶段：

- :class:`PreviewPanel`：DropPanel 接受视频；同时也是字幕画布容器，加载字幕
  即翻到 "populated" 状态（DropPanel 双态用 :meth:`set_track` 主动切换）
- :class:`PreviewCanvas`：``paintEvent`` 调 :func:`paint_frame` 把当前时间
  的活跃行画到 widget 上
- :class:`TransportBar`：``QSlider`` 拖时间 + 时间码标签，emit :pyattr:`timeChanged`

A7 之后这里会接入 ``QMediaPlayer.setVideoSink`` 把视频帧画到画布底层；A4 阶段
画布背景是统一深色（在视频载入之前也能预览字幕动效）。
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt, pyqtSignal as Signal
from PyQt6.QtGui import QColor, QImage, QPainter
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from krok_helper.subtitle_render.engine.painter import paint_frame
from krok_helper.subtitle_render.frontend.drop_panel import DropPanel
from krok_helper.subtitle_render.models import Style, TimingTrack
from krok_helper.theme_workbench import palette, themed


PREVIEW_BG = QColor("#101010")
"""画布默认深色背景（A7 接入视频后这里换成视频帧）。"""


class PreviewCanvas(QWidget):
    """字幕预览画布：原地 ``paint_frame`` 重绘当前时刻活跃行。"""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(240)
        self._track: Optional[TimingTrack] = None
        self._style: Style = Style()
        self._t_ms: int = 0
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
        self.update()

    @property
    def current_time_ms(self) -> int:
        return self._t_ms

    # ------------------------------------------------------------------ paint

    def paintEvent(self, event):  # noqa: N802 — Qt API
        w = max(self.width(), 1)
        h = max(self.height(), 1)
        # 先离屏渲染到 QImage，再 blit 到 widget——保持渲染路径与导出管线一致
        image = QImage(w, h, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(PREVIEW_BG)
        paint_frame(image, self._track, self._t_ms, self._style)
        painter = QPainter(self)
        try:
            painter.drawImage(0, 0, image)
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

    @property
    def canvas(self) -> PreviewCanvas:
        return self._canvas


class TransportBar(QWidget):
    """播放控件 + 时间码 + 进度条（A4 / A7 用，播放循环 P1 接入）。"""

    timeChanged = Signal(int)
    """滑块拖动 / 程序设值时 emit 当前时间（毫秒）。"""

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
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 6, 12, 6)
        layout.setSpacing(10)

        self._slider = QSlider(Qt.Orientation.Horizontal, self)
        self._slider.setMinimum(0)
        self._slider.setMaximum(60_000)
        self._slider.setValue(0)
        self._slider.setSingleStep(50)
        self._slider.setPageStep(1000)
        self._slider.valueChanged.connect(self._on_slider_changed)
        layout.addWidget(self._slider, 1)

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
        layout.addWidget(self._timecode)

    # ------------------------------------------------------------------ public

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

    @property
    def current_time_ms(self) -> int:
        return self._slider.value()

    # ------------------------------------------------------------------ events

    def _on_slider_changed(self, value: int) -> None:
        self._update_timecode(value)
        self.timeChanged.emit(value)

    def _update_timecode(self, ms: int) -> None:
        total_cs = ms // 10
        minutes = total_cs // 6000
        seconds = (total_cs % 6000) // 100
        centiseconds = total_cs % 100
        self._timecode.setText(f"{minutes:02d}:{seconds:02d}.{centiseconds:02d}")
