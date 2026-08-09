"""波形对齐页的波形画布。

从 ``gui_qt`` 原样搬出：自绘双轨波形、播放头、拖拽调偏移、缩放与选区。
不持有宿主引用，状态全部通过属性写入、变化通过信号抛出。
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal as Signal
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PyQt6.QtWidgets import QSizePolicy, QWidget

from krok_helper.audio_alignment import WaveformData, format_offset
from krok_helper.settings import ALIGN_TARGET_AUDIO, ALIGN_TARGET_VIDEO

__all__ = ["WaveformView"]


class WaveformView(QWidget):
    playheadChanged = Signal(float)
    offsetChanged = Signal(float)
    trimChanged = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.video_waveform: WaveformData | None = None
        self.audio_waveform: WaveformData | None = None
        self.target_track = ALIGN_TARGET_VIDEO
        self.offset_seconds = 0.0
        self.playhead_seconds = 0.0
        self.view_start_seconds = 0.0
        self.trim_end_seconds: float | None = None
        self.pixels_per_second = 120.0
        self.drag_mode = "offset"
        self._drag_kind = ""
        self._drag_start_x = 0.0
        self._drag_start_offset = 0.0
        self._drag_start_view = 0.0
        self.track_label_width = 190
        self.right_reserved_width = 58
        self._auto_fit_view = True
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumHeight(280)
        self.setFocusPolicy(Qt.FocusPolicy.WheelFocus)
        self.setMouseTracking(True)

    def clear(self) -> None:
        self.video_waveform = None
        self.audio_waveform = None
        self.offset_seconds = 0.0
        self.playhead_seconds = 0.0
        self.view_start_seconds = 0.0
        self.trim_end_seconds = None
        self._auto_fit_view = True
        self.update()

    def set_waveforms(self, *, video_waveform: WaveformData, audio_waveform: WaveformData) -> None:
        self.video_waveform = video_waveform
        self.audio_waveform = audio_waveform
        self.offset_seconds = 0.0
        self.playhead_seconds = 0.0
        self.view_start_seconds = 0.0
        self.trim_end_seconds = None
        self._auto_fit_view = True
        self.fit_to_waveforms()
        self.update()

    def set_target_track(self, target_track: str) -> None:
        self.target_track = target_track
        self.set_offset(0.0)

    def set_drag_mode(self, mode: str) -> None:
        self.drag_mode = mode if mode in {"offset", "pan"} else "offset"

    def set_offset(self, seconds: float) -> None:
        self.offset_seconds = seconds
        self.offsetChanged.emit(seconds)
        self.update()

    def nudge_offset(self, delta_seconds: float) -> None:
        self.set_offset(self.offset_seconds + delta_seconds)

    def set_playhead(self, seconds: float, *, keep_visible: bool = False) -> None:
        self.playhead_seconds = max(0.0, seconds)
        if keep_visible:
            self._ensure_visible(self.playhead_seconds)
        self.playheadChanged.emit(self.playhead_seconds)
        self.update()

    def set_trim_end(self, seconds: float) -> None:
        self.trim_end_seconds = max(0.0, seconds)
        self.trimChanged.emit(self.trim_end_seconds)
        self.update()

    def clear_trim_end(self) -> None:
        self.trim_end_seconds = None
        self.trimChanged.emit(None)
        self.update()

    def set_zoom(self, pixels_per_second: float) -> None:
        self._auto_fit_view = False
        self._zoom_to(pixels_per_second, self._playhead_anchor_x())

    def fit_to_waveforms(self) -> None:
        if not self.video_waveform or not self.audio_waveform:
            return
        _plot_left, plot_width = self._plot_bounds()
        usable_width = max(1.0, plot_width - 8.0)
        self.pixels_per_second = max(0.5, min(1200.0, usable_width / 15.0))
        self.view_start_seconds = 0.0
        self._auto_fit_view = True
        self.update()

    def reset_view(self) -> None:
        self.fit_to_waveforms()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if self._auto_fit_view and self.video_waveform and self.audio_waveform:
            self.fit_to_waveforms()

    def jump_to_end(self) -> None:
        if not self.video_waveform or not self.audio_waveform:
            return
        visible_seconds = self._visible_seconds()
        video_end = self.video_waveform.duration + (self.offset_seconds if self.target_track == ALIGN_TARGET_VIDEO else 0.0)
        audio_end = self.audio_waveform.duration + (self.offset_seconds if self.target_track == ALIGN_TARGET_AUDIO else 0.0)
        timeline_end = max(video_end, audio_end)
        self.view_start_seconds = max(0.0, timeline_end - visible_seconds * (2 / 3))
        self.update()

    def source_starts(self) -> tuple[float, float]:
        video_offset = self.offset_seconds if self.target_track == ALIGN_TARGET_VIDEO else 0.0
        audio_offset = self.offset_seconds if self.target_track == ALIGN_TARGET_AUDIO else 0.0
        timeline_start = max(self.view_start_seconds, video_offset, audio_offset, 0.0)
        return max(0.0, timeline_start - video_offset), max(0.0, timeline_start - audio_offset)

    def wheelEvent(self, event) -> None:  # noqa: N802
        if not self.video_waveform or not self.audio_waveform:
            event.ignore()
            return
        delta = event.angleDelta().y()
        if delta == 0:
            event.ignore()
            return
        factor = 1.15 if delta > 0 else (1 / 1.15)
        self._auto_fit_view = False
        self._zoom_to(self.pixels_per_second * factor, self._playhead_anchor_x())
        event.accept()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if not self.video_waveform or not self.audio_waveform:
            return
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_start_x = event.position().x()
            self._drag_start_offset = self.offset_seconds
            self._drag_start_view = self.view_start_seconds
            if event.position().y() <= 24 and event.position().x() >= self.track_label_width:
                self._drag_kind = "playhead"
                self._set_playhead_from_x(event.position().x())
            elif event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                self._drag_kind = "playhead"
                self._set_playhead_from_x(event.position().x())
            elif self.drag_mode == "pan":
                self._drag_kind = "pan"
            else:
                self._drag_kind = "offset"

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._drag_kind == "playhead":
            self._set_playhead_from_x(event.position().x())
            return
        if self._drag_kind == "pan":
            delta_seconds = (event.position().x() - self._drag_start_x) / self.pixels_per_second
            self.view_start_seconds = max(0.0, self._drag_start_view - delta_seconds)
            self.update()
            return
        if self._drag_kind == "offset":
            delta_seconds = (event.position().x() - self._drag_start_x) / self.pixels_per_second
            self.set_offset(self._drag_start_offset + delta_seconds)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_kind = ""

    def paintEvent(self, event) -> None:  # noqa: N802
        from krok_helper.theme_workbench import palette

        p = palette()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        painter.fillRect(self.rect(), QColor(p.card_bg))
        if self._auto_fit_view and self.video_waveform and self.audio_waveform:
            _plot_left, plot_width = self._plot_bounds()
            fitted = max(0.5, min(1200.0, max(1.0, plot_width - 8.0) / 15.0))
            if abs(fitted - self.pixels_per_second) > 0.01:
                self.pixels_per_second = fitted
                self.view_start_seconds = 0.0

        if not self.video_waveform or not self.audio_waveform:
            return

        outer_rect = self.rect().adjusted(0, 0, -self.right_reserved_width, -1)
        label_width = self.track_label_width

        ruler_rect = outer_rect.adjusted(label_width, 0, 0, -(outer_rect.height() - 24))
        painter.setPen(QColor(p.card_bg))
        painter.drawLine(ruler_rect.left() + 1, ruler_rect.top(), ruler_rect.right() - 1, ruler_rect.top())
        painter.setPen(QColor(p.table_border))
        painter.drawLine(ruler_rect.left(), ruler_rect.top() + 1, ruler_rect.left(), ruler_rect.bottom())
        painter.drawLine(ruler_rect.right(), ruler_rect.top() + 1, ruler_rect.right(), ruler_rect.bottom())
        painter.drawLine(ruler_rect.left(), ruler_rect.bottom(), ruler_rect.right(), ruler_rect.bottom())

        content_rect = outer_rect.adjusted(0, 24, 0, 0)
        track_gap = 0
        track_height = max(68, int((content_rect.height() - track_gap) / 2))
        video_label_rect = content_rect.adjusted(0, 0, -(content_rect.width() - label_width), -(content_rect.height() - track_height))
        video_rect = content_rect.adjusted(label_width, 0, 0, -(content_rect.height() - track_height))
        audio_label_rect = content_rect.adjusted(0, track_height + track_gap, -(content_rect.width() - label_width), 0)
        audio_rect = content_rect.adjusted(label_width, track_height + track_gap, 0, 0)

        self._draw_ruler(painter, ruler_rect)
        video_offset = self.offset_seconds if self.target_track == ALIGN_TARGET_VIDEO else 0.0
        audio_offset = self.offset_seconds if self.target_track == ALIGN_TARGET_AUDIO else 0.0
        self._draw_label_block(
            painter,
            video_label_rect,
            "字幕视频音轨",
            format_offset(video_offset) if self.target_track == ALIGN_TARGET_VIDEO else "",
            QColor("#F04452"),
        )
        self._draw_label_block(
            painter,
            audio_label_rect,
            "原唱音源",
            format_offset(audio_offset) if self.target_track == ALIGN_TARGET_AUDIO else "",
            QColor("#2F6BFF"),
        )

        self._draw_track(
            painter,
            video_rect,
            self.video_waveform,
            QColor("#F04452"),
            self.offset_seconds if self.target_track == ALIGN_TARGET_VIDEO else 0.0,
        )
        self._draw_track(
            painter,
            audio_rect,
            self.audio_waveform,
            QColor("#2F6BFF"),
            self.offset_seconds if self.target_track == ALIGN_TARGET_AUDIO else 0.0,
        )

        playhead_x = self._time_to_x(self.playhead_seconds, video_rect.left())
        painter.setPen(QPen(QColor("#F04452"), 2))
        painter.drawLine(int(playhead_x), ruler_rect.top(), int(playhead_x), audio_rect.bottom())

        if self.trim_end_seconds is not None:
            trim_x = self._time_to_x(self.trim_end_seconds, video_rect.left())
            painter.setPen(QPen(QColor("#eab308"), 2, Qt.PenStyle.DashLine))
            painter.drawLine(int(trim_x), video_rect.top(), int(trim_x), video_rect.bottom())

    def _draw_track(
        self,
        painter: QPainter,
        rect,
        waveform: WaveformData,
        color: QColor,
        track_offset: float,
    ) -> None:
        from krok_helper.theme_workbench import palette

        p = palette()
        painter.fillRect(rect, QColor(p.input_bg))
        painter.setPen(QColor(p.table_border))
        painter.drawRect(rect)

        center_y = rect.center().y()
        painter.setPen(QPen(QColor(p.table_row_border), 1))
        painter.drawLine(rect.left() + 1, center_y, rect.right() - 1, center_y)

        painter.setPen(QPen(color, 1))
        usable_height = max(12.0, rect.height() * 0.35)
        for x in range(rect.left() + 1, rect.right(), 2):
            absolute_time = self.view_start_seconds + ((x - rect.left()) / self.pixels_per_second)
            source_time = absolute_time - track_offset
            if source_time < 0 or source_time >= waveform.duration:
                continue
            index = int(source_time * waveform.peaks_per_second)
            if index < 0 or index >= len(waveform.peaks):
                continue
            amplitude = waveform.peaks[index]
            top = center_y - int(amplitude * usable_height)
            bottom = center_y + int(amplitude * usable_height)
            painter.drawLine(x, top, x, bottom)

        track_end_seconds = waveform.duration + track_offset
        end_x = self._time_to_x(track_end_seconds, rect.left())
        if rect.left() < end_x < rect.right():
            end_x_int = int(end_x)
            painter.fillRect(
                end_x_int + 1,
                rect.top() + 1,
                rect.right() - end_x_int - 1,
                rect.height() - 2,
                QColor(p.panel_bg),
            )
            painter.setPen(QPen(QColor(p.text_hint), 1, Qt.PenStyle.DashLine))
            painter.drawLine(end_x_int, rect.top() + 1, end_x_int, rect.bottom() - 1)
            painter.setPen(QColor(p.text_hint))
            painter.setFont(QFont("Microsoft YaHei UI", 8))
            painter.drawText(end_x_int + 4, rect.top() + 12, "结束")

    def _draw_label_block(self, painter: QPainter, rect, title: str, offset_text: str, title_color: QColor) -> None:
        from krok_helper.theme_workbench import palette

        painter.fillRect(rect, QColor(palette().card_bg))
        text_rect = rect.adjusted(10, 8, -10, -8)
        title_font = QFont("Microsoft YaHei UI", 11)
        title_font.setBold(True)
        title_metrics = QFontMetrics(title_font)
        offset_font = QFont("Microsoft YaHei UI", 11)
        offset_font.setBold(True)
        offset_metrics = QFontMetrics(offset_font)
        line_gap = 4 if offset_text else 0
        content_height = title_metrics.height() + line_gap + (offset_metrics.height() if offset_text else 0)
        start_y = text_rect.top() + max(0, (text_rect.height() - content_height) // 2)
        painter.setFont(title_font)
        painter.setPen(title_color)
        title_display = title_metrics.elidedText(title, Qt.TextElideMode.ElideRight, text_rect.width())
        painter.drawText(
            text_rect.left(),
            start_y,
            text_rect.width(),
            title_metrics.height(),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            title_display,
        )
        if offset_text:
            painter.setFont(offset_font)
            offset_display = offset_metrics.elidedText(offset_text, Qt.TextElideMode.ElideRight, text_rect.width())
            painter.drawText(
                text_rect.left(),
                start_y + title_metrics.height() + line_gap,
                text_rect.width(),
                offset_metrics.height(),
                int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                offset_display,
            )

    def _draw_ruler(self, painter: QPainter, rect) -> None:
        visible_seconds = self._visible_seconds()
        min_label_spacing_px = 72.0
        step = 1.0
        for candidate in (0.1, 0.2, 0.5, 1.0, 2.0, 5.0, 10.0, 15.0, 30.0, 60.0, 120.0, 300.0, 600.0):
            if candidate * self.pixels_per_second >= min_label_spacing_px:
                step = candidate
                break
            step = candidate
        painter.setPen(QColor("#94a3b8"))
        start_tick = int(self.view_start_seconds // step)
        end_tick = int((self.view_start_seconds + visible_seconds) // step) + 1
        for tick in range(start_tick, end_tick):
            tick_seconds = tick * step
            x = self._time_to_x(tick_seconds, rect.left())
            if x < rect.left() or x > rect.right():
                continue
            painter.drawLine(int(x), rect.bottom() - 6, int(x), rect.bottom())
            label = f"{tick_seconds:.1f}s" if step < 10 else f"{tick_seconds:.0f}s"
            painter.drawText(int(x) + 2, rect.top() + 14, label)

    def _plot_bounds(self) -> tuple[float, float]:
        plot_left = float(self.track_label_width)
        plot_width = max(1.0, float(self.width() - self.track_label_width - self.right_reserved_width - 1))
        return plot_left, plot_width

    def _zoom_to(self, pixels_per_second: float, anchor_x: float) -> None:
        plot_left, plot_width = self._plot_bounds()
        anchor_x = min(plot_left + plot_width, max(plot_left, anchor_x))
        old_pixels_per_second = max(1.0, self.pixels_per_second)
        anchor_seconds = self.view_start_seconds + (anchor_x - plot_left) / old_pixels_per_second
        self.pixels_per_second = max(0.5, min(1200.0, pixels_per_second))
        self.view_start_seconds = max(0.0, anchor_seconds - (anchor_x - plot_left) / self.pixels_per_second)
        self.update()

    def _playhead_anchor_x(self) -> float:
        plot_left, plot_width = self._plot_bounds()
        return min(plot_left + plot_width, max(plot_left, self._time_to_x(self.playhead_seconds, plot_left)))

    def _visible_seconds(self) -> float:
        _plot_left, plot_width = self._plot_bounds()
        return max(1.0, plot_width / self.pixels_per_second)

    def _ensure_visible(self, seconds: float) -> None:
        visible_seconds = self._visible_seconds()
        if seconds < self.view_start_seconds:
            self.view_start_seconds = max(0.0, seconds - visible_seconds * 0.1)
        elif seconds > self.view_start_seconds + visible_seconds:
            self.view_start_seconds = max(0.0, seconds - visible_seconds * 0.9)

    def _time_to_x(self, seconds: float, left_edge: int) -> float:
        return left_edge + (seconds - self.view_start_seconds) * self.pixels_per_second

    def _set_playhead_from_x(self, x_pos: float) -> None:
        rect_left, rect_width = self._plot_bounds()
        clamped_x = min(rect_left + rect_width, max(rect_left, x_pos))
        time_pos = self.view_start_seconds + (clamped_x - rect_left) / self.pixels_per_second
        self.set_playhead(time_pos)
