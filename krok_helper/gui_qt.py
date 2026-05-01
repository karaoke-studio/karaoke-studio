from __future__ import annotations

import subprocess
import time
from pathlib import Path
from string import Formatter
from typing import Callable

from PySide6.QtCore import QEvent, QThread, QTimer, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QIcon, QKeySequence, QPainter, QPen, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QProgressBar,
    QRadioButton,
    QScrollArea,
    QSlider,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from krok_helper.audio_alignment import (
    DEFAULT_ALIGNED_AUDIO_NAME_TEMPLATE,
    DEFAULT_ALIGNED_VIDEO_NAME_TEMPLATE,
    ENCODE_MODE_HARDWARE,
    ENCODE_MODE_SOFTWARE,
    LEAD_FILL_BLACK,
    LEAD_FILL_FREEZE,
    LEAD_FILL_WHITE,
    AutoAlignResult,
    WaveformData,
    export_aligned_audio,
    export_aligned_video,
    estimate_waveform_alignment,
    extract_waveform,
    format_offset,
    start_alignment_preview,
)
from krok_helper.config import (
    APP_NAME,
    APP_TITLE,
    WINDOW_HEIGHT,
    WINDOW_MIN_HEIGHT,
    WINDOW_MIN_WIDTH,
    WINDOW_WIDTH,
)
from krok_helper.errors import ExportCancelled, ProcessingError
from krok_helper.ffmpeg import find_tool, probe_media, terminate_process
from krok_helper.lyrics import (
    DEFAULT_LYRICS_SEARCH_LIMIT,
    DEFAULT_LYRICS_PROVIDER_IDS,
    LYRICS_PREVIEW_LINE,
    LYRICS_PREVIEW_VERBATIM,
    LyricsPreview,
    LyricsSearchBatch,
    LyricsSearchCandidate,
    LyricsSearchService,
    build_lyrics_preview,
)
from krok_helper.pipeline import (
    DEFAULT_OFF_NAME_TEMPLATE,
    DEFAULT_ON_NAME_TEMPLATE,
    OUTPUT_NAME_MODE_FIXED,
    OUTPUT_NAME_MODE_TEMPLATE,
    OUTPUT_NAME_MODE_VIDEO_NAME,
    resolve_output_dir,
    run_pipeline,
    validate_output_name_template,
)
from krok_helper.settings import load_app_settings, save_app_settings
from krok_helper.windows import set_explicit_app_user_model_id


ALIGN_TARGET_VIDEO = "video"
ALIGN_TARGET_AUDIO = "audio"
VIDEO_EXTENSIONS = {".mkv", ".mp4", ".mov", ".avi"}
AUDIO_EXTENSIONS = {".flac", ".wav", ".mp3", ".m4a", ".aac", ".ape", ".alac", ".mkv"}
HIRES_AUDIO_EXTENSIONS = AUDIO_EXTENSIONS | {".mp4"}
ALIGN_AUDIO_EXTENSIONS = AUDIO_EXTENSIONS | {".mp4"}
WINDOWS_INVALID_FILENAME_CHARS = '<>:"/\\|?*'
ALIGNMENT_TEMPLATE_FORMATTER = Formatter()
FFMPEG_DIR_PLACEHOLDER = "未设置，将优先使用系统 PATH 中的 ffmpeg"

APP_LOGO_PATH = Path(__file__).resolve().parent / "assets" / "logo" / "logo.jpg"
TASKBAR_LOGO_PATH = Path(__file__).resolve().parent / "assets" / "logo" / "logo2.png"


def open_in_explorer(path: Path) -> None:
    subprocess.Popen(["explorer", str(path)])


def load_app_icon() -> QIcon | None:
    if not APP_LOGO_PATH.exists():
        return None
    icon = QIcon(str(APP_LOGO_PATH))
    return None if icon.isNull() else icon


def load_taskbar_icon() -> QIcon | None:
    if TASKBAR_LOGO_PATH.exists():
        icon = QIcon(str(TASKBAR_LOGO_PATH))
        if not icon.isNull():
            return icon
    return load_app_icon()


def apply_safe_label_metrics(
    label: QLabel,
    font: QFont,
    *,
    top_padding: int = 3,
    bottom_padding: int = 2,
) -> None:
    margins = label.contentsMargins()
    label.setContentsMargins(margins.left(), top_padding, margins.right(), bottom_padding)
    label.setMinimumHeight(QFontMetrics(font).height() + top_padding + bottom_padding)


def build_lyrics_ui_font(*, point_size: float = 10.5, bold: bool = False) -> QFont:
    font = QFont()
    font.setFamilies(
        [
            "Yu Gothic UI",
            "Meiryo UI",
            "Meiryo",
            "Segoe UI",
            "Microsoft YaHei UI",
        ]
    )
    font.setPointSizeF(point_size)
    font.setStyleStrategy(QFont.StyleStrategy.PreferDefault)
    if bold:
        font.setBold(True)
    return font


def format_media_duration(seconds: float | None) -> str:
    if seconds is None or seconds <= 0:
        return "时长未知"

    minutes = int(seconds // 60)
    remainder = seconds - minutes * 60
    if minutes:
        return f"{minutes}:{remainder:06.3f}"
    return f"{seconds:.3f}s"


class DropZoneCard(QFrame):
    pathChanged = Signal(Path)
    browseRequested = Signal()

    def __init__(
        self,
        *,
        title: str,
        hint: str,
        extensions: set[str],
        min_height: int = 220,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.extensions = {ext.lower() for ext in extensions}
        self.path: Path | None = None
        self._hovered = False
        self._drag_state = "idle"
        self._default_action_text = "点击选择文件，或直接拖进这个区域"

        self.setObjectName("DropZoneCard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAcceptDrops(True)
        self.setMinimumHeight(min_height)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(8)

        self.title_label = QLabel(title)
        self.title_label.setObjectName("DropZoneTitle")
        self.title_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        title_font = QFont("Microsoft YaHei UI", 12)
        title_font.setBold(True)
        apply_safe_label_metrics(self.title_label, title_font)

        self.hint_label = QLabel(hint)
        self.hint_label.setObjectName("DropZoneHint")
        self.hint_label.setWordWrap(True)
        self.hint_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        self.path_label = QLabel("未选择文件")
        self.path_label.setObjectName("DropZonePath")
        self.path_label.setWordWrap(True)
        self.path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.path_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        self.action_label = QLabel(self._default_action_text)
        self.action_label.setObjectName("DropZoneAction")
        self.action_label.setWordWrap(True)
        self.action_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        layout.addWidget(self.title_label)
        layout.addWidget(self.hint_label)
        layout.addStretch(1)
        layout.addWidget(self.path_label)
        layout.addWidget(self.action_label)
        self._refresh_style()

    def accepts(self, path: Path) -> bool:
        return path.is_file() and path.suffix.lower() in self.extensions

    def set_path(self, path: Path) -> None:
        self.path = path
        self.path_label.setText(str(path))
        self._drag_state = "idle"
        self._refresh_style()

    def clear_path(self) -> None:
        self.path = None
        self.path_label.setText("未选择文件")
        self._drag_state = "idle"
        self._refresh_style()

    def enterEvent(self, event) -> None:  # noqa: N802
        self._hovered = True
        self._refresh_style()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hovered = False
        self._refresh_style()
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.browseRequested.emit()
            event.accept()
            return
        super().mousePressEvent(event)

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        urls = event.mimeData().urls()
        if not urls:
            self._drag_state = "reject"
            self._refresh_style()
            event.ignore()
            return
        path = Path(urls[0].toLocalFile()).expanduser()
        if self.accepts(path):
            self._drag_state = "accept"
            self._refresh_style()
            event.acceptProposedAction()
            return
        self._drag_state = "reject"
        self._refresh_style()
        event.ignore()

    def dragLeaveEvent(self, event) -> None:  # noqa: N802
        self._drag_state = "idle"
        self._refresh_style()
        super().dragLeaveEvent(event)

    def dropEvent(self, event) -> None:  # noqa: N802
        urls = event.mimeData().urls()
        if not urls:
            self._drag_state = "idle"
            self._refresh_style()
            event.ignore()
            return
        path = Path(urls[0].toLocalFile()).expanduser()
        if not self.accepts(path):
            self._drag_state = "reject"
            self._refresh_style()
            event.ignore()
            return
        self.set_path(path)
        self.pathChanged.emit(path)
        event.acceptProposedAction()

    def _refresh_style(self) -> None:
        border_width = 1
        if self._drag_state == "accept":
            background = "#dbeafe"
            border = "#2563eb"
            accent = "#1d4ed8"
            border_width = 2
            action_text = "松开鼠标即可导入这个文件"
        elif self._drag_state == "reject":
            background = "#fef2f2"
            border = "#ef4444"
            accent = "#b91c1c"
            border_width = 2
            action_text = "这个文件类型不支持，请换一个文件"
        elif self.path is not None:
            background = "#ecfdf3"
            border = "#3aa76d"
            accent = "#177245"
            action_text = self._default_action_text
        elif self._hovered:
            background = "#eef4ff"
            border = "#8aa8f8"
            accent = "#2f6fed"
            action_text = self._default_action_text
        else:
            background = "#f6f8fb"
            border = "#d5dce6"
            accent = "#2f6fed"
            action_text = self._default_action_text

        self.action_label.setText(action_text)

        self.setStyleSheet(
            f"""
            QFrame#DropZoneCard {{
                background: {background};
                border: {border_width}px solid {border};
            }}
            QLabel#DropZoneTitle {{
                background: transparent;
                border: 0;
                color: #1f2937;
                font-family: "Microsoft YaHei UI";
                font-size: 12pt;
                font-weight: 700;
            }}
            QLabel#DropZoneHint {{
                background: transparent;
                border: 0;
                color: #5b6677;
                font-family: "Microsoft YaHei UI";
                font-size: 10pt;
            }}
            QLabel#DropZonePath {{
                background: transparent;
                border: 0;
                color: #111827;
                font-family: "Consolas";
                font-size: 10pt;
            }}
            QLabel#DropZoneAction {{
                background: transparent;
                border: 0;
                color: {accent};
                font-family: "Microsoft YaHei UI";
                font-size: 10pt;
                font-weight: 700;
            }}
            """
        )


class BackgroundTask(QThread):
    log_message = Signal(str)
    task_succeeded = Signal(object)
    task_failed = Signal(str)

    def __init__(self, runner: Callable[[Callable[[str], None]], object]) -> None:
        super().__init__()
        self._runner = runner

    def run(self) -> None:  # noqa: D401
        try:
            result = self._runner(self.log_message.emit)
        except Exception as exc:  # noqa: BLE001
            self.task_failed.emit(str(exc))
            return
        self.task_succeeded.emit(result)


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
        self.track_label_width = 188
        self.setMinimumHeight(280)
        self.setMouseTracking(True)

    def clear(self) -> None:
        self.video_waveform = None
        self.audio_waveform = None
        self.offset_seconds = 0.0
        self.playhead_seconds = 0.0
        self.view_start_seconds = 0.0
        self.trim_end_seconds = None
        self.update()

    def set_waveforms(self, *, video_waveform: WaveformData, audio_waveform: WaveformData) -> None:
        self.video_waveform = video_waveform
        self.audio_waveform = audio_waveform
        self.offset_seconds = 0.0
        self.playhead_seconds = 0.0
        self.view_start_seconds = 0.0
        self.trim_end_seconds = None
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
        self._zoom_to(pixels_per_second, self._playhead_anchor_x())

    def reset_view(self) -> None:
        self.pixels_per_second = 120.0
        self.view_start_seconds = 0.0
        self.update()

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
            return
        delta = event.angleDelta().y()
        if delta == 0:
            return
        factor = 1.15 if delta > 0 else (1 / 1.15)
        self._zoom_to(self.pixels_per_second * factor, self._playhead_anchor_x())

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
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        painter.fillRect(self.rect(), QColor("#ffffff"))

        if not self.video_waveform or not self.audio_waveform:
            painter.setPen(QColor("#6b7280"))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "生成波形后会在这里显示对齐视图")
            return

        outer_rect = self.rect().adjusted(0, 0, -1, -1)
        label_width = self.track_label_width
        painter.setPen(QColor("#d5dce6"))
        painter.drawRect(outer_rect)

        ruler_rect = outer_rect.adjusted(label_width, 0, 0, -(outer_rect.height() - 24))
        painter.fillRect(ruler_rect, QColor("#fafbfc"))
        painter.setPen(QColor("#cfd7e2"))
        painter.drawRect(ruler_rect)

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
        video_title = "字幕视频音轨" + (f" {format_offset(video_offset)}" if abs(video_offset) >= 0.0005 else "")
        audio_title = "原唱音源" + (f" {format_offset(audio_offset)}" if abs(audio_offset) >= 0.0005 else "")
        self._draw_label_block(painter, video_label_rect, video_title, format_media_duration(self.video_waveform.duration))
        self._draw_label_block(painter, audio_label_rect, audio_title, format_media_duration(self.audio_waveform.duration))

        self._draw_track(
            painter,
            video_rect,
            self.video_waveform,
            QColor("#2f6fed"),
            self.offset_seconds if self.target_track == ALIGN_TARGET_VIDEO else 0.0,
        )
        self._draw_track(
            painter,
            audio_rect,
            self.audio_waveform,
            QColor("#177245"),
            self.offset_seconds if self.target_track == ALIGN_TARGET_AUDIO else 0.0,
        )

        playhead_x = self._time_to_x(self.playhead_seconds, video_rect.left())
        painter.setPen(QPen(QColor("#f43f5e"), 2))
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
        painter.fillRect(rect, QColor("#ffffff"))
        painter.setPen(QColor("#d5dce6"))
        painter.drawRect(rect)

        center_y = rect.center().y()
        painter.setPen(QPen(QColor("#e5e7eb"), 1))
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
            painter.fillRect(end_x_int + 1, rect.top() + 1, rect.right() - end_x_int - 1, rect.height() - 2, QColor("#f8fafc"))
            painter.setPen(QPen(QColor("#94a3b8"), 1, Qt.PenStyle.DashLine))
            painter.drawLine(end_x_int, rect.top() + 1, end_x_int, rect.bottom() - 1)
            painter.setPen(QColor("#94a3b8"))
            painter.setFont(QFont("Microsoft YaHei UI", 8))
            painter.drawText(end_x_int + 4, rect.top() + 12, "结束")

    def _draw_label_block(self, painter: QPainter, rect, title: str, duration_text: str) -> None:
        painter.fillRect(rect, QColor("#ffffff"))
        painter.setPen(QColor("#d5dce6"))
        painter.drawRect(rect)
        text_rect = rect.adjusted(10, 10, -8, -8)
        title_font = QFont("Microsoft YaHei UI", 10)
        title_font.setBold(True)
        painter.setFont(title_font)
        painter.setPen(QColor("#111827"))
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWordWrap, title)
        subtitle_font = QFont("Microsoft YaHei UI", 9)
        painter.setFont(subtitle_font)
        painter.setPen(QColor("#6b7280"))
        painter.drawText(
            text_rect.adjusted(0, 28, 0, 0),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWordWrap,
            f"总时长 {duration_text}",
        )

    def _draw_ruler(self, painter: QPainter, rect) -> None:
        visible_seconds = self._visible_seconds()
        step = 5.0 if visible_seconds > 20 else 1.0
        painter.setPen(QColor("#94a3b8"))
        start_tick = int(self.view_start_seconds // step)
        end_tick = int((self.view_start_seconds + visible_seconds) // step) + 1
        for tick in range(start_tick, end_tick):
            tick_seconds = tick * step
            x = self._time_to_x(tick_seconds, rect.left())
            if x < rect.left() or x > rect.right():
                continue
            painter.drawLine(int(x), rect.bottom() - 6, int(x), rect.bottom())
            painter.drawText(int(x) + 2, rect.top() + 14, f"{tick_seconds:.1f}s")

    def _plot_bounds(self) -> tuple[float, float]:
        plot_left = float(self.track_label_width)
        plot_width = max(1.0, float(self.width() - self.track_label_width - 1))
        return plot_left, plot_width

    def _zoom_to(self, pixels_per_second: float, anchor_x: float) -> None:
        plot_left, plot_width = self._plot_bounds()
        anchor_x = min(plot_left + plot_width, max(plot_left, anchor_x))
        old_pixels_per_second = max(1.0, self.pixels_per_second)
        anchor_seconds = self.view_start_seconds + (anchor_x - plot_left) / old_pixels_per_second
        self.pixels_per_second = max(20.0, min(1200.0, pixels_per_second))
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


class KrokHelperQtApp(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.settings = load_app_settings()
        self.hires_task: BackgroundTask | None = None
        self.lyrics_search_task: BackgroundTask | None = None
        self.lyrics_fetch_task: BackgroundTask | None = None
        self.align_analysis_task: BackgroundTask | None = None
        self.align_auto_task: BackgroundTask | None = None
        self.align_export_task: BackgroundTask | None = None
        self.lyrics_search_service = LyricsSearchService()
        self.lyrics_search_results: list[LyricsSearchCandidate] = []
        self.lyrics_pending_results: list[LyricsSearchCandidate] = []
        self.lyrics_selected_candidate: LyricsSearchCandidate | None = None
        self.lyrics_search_keyword = ""
        self.lyrics_search_provider_ids: tuple[str, ...] = DEFAULT_LYRICS_PROVIDER_IDS
        self.lyrics_next_provider_pages: dict[str, int] = {}
        self.lyrics_has_more_results = False
        self._lyrics_loading_more = False
        self._lyrics_loading_key = ""
        self.align_preview_process = None
        self.align_preview_started_at = 0.0
        self.align_preview_start_seconds = 0.0
        self._align_export_cancel_requested = False
        self._align_export_process: subprocess.Popen | None = None
        self._align_export_expected_outputs: list[Path] = []
        self._align_export_completed_outputs: list[Path] = []
        self.active_module = "lyrics"
        self._loading_settings_into_ui = False

        self.output_name_mode_value = OUTPUT_NAME_MODE_FIXED
        self.on_name_template_value = DEFAULT_ON_NAME_TEMPLATE
        self.off_name_template_value = DEFAULT_OFF_NAME_TEMPLATE
        self.align_video_name_template_value = DEFAULT_ALIGNED_VIDEO_NAME_TEMPLATE
        self.align_audio_name_template_value = DEFAULT_ALIGNED_AUDIO_NAME_TEMPLATE
        self.ffmpeg_dir_text = ""
        self._align_lead_fill_selection = LEAD_FILL_BLACK
        self._align_encode_selection = ENCODE_MODE_SOFTWARE
        self._media_duration_cache: dict[Path, str] = {}
        self._suppress_preview_seek_restart = False
        self._restoring_from_maximized = False
        self._startup_geometry_applied = False
        self.align_control_panel: QFrame | None = None
        self.align_open_output_button: QPushButton | None = None
        self.align_clear_button: QPushButton | None = None
        self.align_jump_to_end_button: QPushButton | None = None
        self.align_reset_view_button: QPushButton | None = None

        self.setWindowTitle(APP_TITLE)
        app_icon = load_app_icon()
        if app_icon is not None:
            self.setWindowIcon(app_icon)
        self.resize(WINDOW_WIDTH, WINDOW_MIN_HEIGHT)
        self.setMinimumSize(WINDOW_MIN_WIDTH, WINDOW_MIN_HEIGHT)

        self._apply_styles()
        self._build_ui()
        self._load_settings_into_ui()
        self._bind_shortcuts()

        self.preview_timer = QTimer(self)
        self.preview_timer.setInterval(300)
        self.preview_timer.timeout.connect(self._poll_alignment_preview)

    def showEvent(self, event) -> None:  # noqa: N802
        if not self._startup_geometry_applied:
            self._startup_geometry_applied = True
            QTimer.singleShot(0, self._apply_startup_window_geometry)
        super().showEvent(event)

    def changeEvent(self, event) -> None:  # noqa: N802
        if event.type() == QEvent.Type.WindowStateChange:
            old_state = event.oldState() if hasattr(event, "oldState") else Qt.WindowState.WindowNoState
            current_state = self.windowState()
            was_large = bool(
                old_state & (Qt.WindowState.WindowMaximized | Qt.WindowState.WindowFullScreen)
            )
            is_large = bool(
                current_state & (Qt.WindowState.WindowMaximized | Qt.WindowState.WindowFullScreen)
            )
            if was_large and not is_large and not self._restoring_from_maximized:
                self._restoring_from_maximized = True
                QTimer.singleShot(0, self._restore_windowed_geometry_centered)
        super().changeEvent(event)

    def _apply_startup_window_geometry(self) -> None:
        screen = self.screen() or QApplication.primaryScreen()
        if screen is None:
            return
        available = screen.availableGeometry()
        target_width = min(
            max(WINDOW_MIN_WIDTH, WINDOW_WIDTH),
            max(WINDOW_MIN_WIDTH, available.width()),
        )
        target_height = min(WINDOW_MIN_HEIGHT, available.height())
        left = available.x() + max(0, (available.width() - target_width) // 2)
        top = available.y() + max(0, (available.height() - target_height) // 2)
        self.setGeometry(left, top, target_width, target_height)

    def _restore_windowed_geometry_centered(self) -> None:
        try:
            screen = self.screen() or QApplication.primaryScreen()
            if screen is None:
                return
            available = screen.availableGeometry()
            target_width = min(
                max(WINDOW_MIN_WIDTH, WINDOW_WIDTH),
                max(WINDOW_MIN_WIDTH, available.width()),
            )
            target_height = min(
                WINDOW_MIN_HEIGHT,
                max(WINDOW_MIN_HEIGHT, available.height()),
            )
            left = available.x() + max(0, (available.width() - target_width) // 2)
            top = available.y() + max(0, (available.height() - target_height) // 2)
            self.setGeometry(left, top, target_width, target_height)
        finally:
            self._restoring_from_maximized = False

    def _apply_styles(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow, QWidget {
                background: #eef2f7;
                color: #1f2937;
                font-family: "Microsoft YaHei UI";
                font-size: 11pt;
            }
            QLabel {
                background: transparent;
            }
            QFrame#Sidebar {
                background: #111827;
            }
            QLabel#SidebarTitle {
                background: #111827;
                color: #ffffff;
                font-size: 15pt;
                font-weight: 700;
                padding: 18px 16px;
            }
            QFrame#WhitePanel, QFrame#ControlPanel {
                background: #ffffff;
                border: 1px solid #d5dce6;
            }
            QFrame#TrimRow {
                background: transparent;
                border: 0;
            }
            QLabel#PanelTitle {
                background: transparent;
                color: #111827;
                font-size: 12pt;
                font-weight: 700;
            }
            QPlainTextEdit#LogText {
                background: #ffffff;
                border: 0;
                color: #1f2937;
                font-family: "Consolas";
                font-size: 10pt;
            }
            QPlainTextEdit#LyricsPreviewText {
                background: #ffffff;
                border: 0;
                color: #1f2937;
                font-size: 11pt;
                padding: 2px 0;
            }
            QTableWidget {
                background: #ffffff;
                alternate-background-color: #f8fafc;
                border: 1px solid #d5dce6;
                gridline-color: #d5dce6;
                selection-background-color: #d6d9df;
                selection-color: #111827;
            }
            QTableWidget::item {
                padding: 4px 6px;
                border: 0;
            }
            QTableWidget::item:selected {
                background: #d6d9df;
                color: #111827;
            }
            QTableWidget::item:focus {
                outline: none;
                border: 0;
            }
            QTableWidget::item:selected:focus {
                outline: none;
                border: 0;
            }
            QTableWidget#LyricsResultsTable {
                background: #ffffff;
                alternate-background-color: #ffffff;
                border: 1px solid rgba(203, 213, 225, 0.8);
                gridline-color: transparent;
                selection-background-color: transparent;
                selection-color: #111827;
                outline: 0;
            }
            QTableWidget#LyricsResultsTable::item {
                padding: 9px 10px;
                border: 0;
                border-bottom: 1px solid rgba(203, 213, 225, 0.45);
            }
            QTableWidget#LyricsResultsTable::item:hover {
                background: #eef2f7;
            }
            QTableWidget#LyricsResultsTable::item:selected {
                background: #dde3ea;
                color: #111827;
            }
            QTableWidget#LyricsResultsTable::item:selected:hover {
                background: #d7dee7;
            }
            QTableWidget#LyricsResultsTable QHeaderView::section {
                background: #ffffff;
                color: #111827;
                border: 0;
                border-bottom: 1px solid rgba(203, 213, 225, 0.6);
                border-right: 1px solid rgba(203, 213, 225, 0.45);
                padding: 8px 8px 10px 8px;
                font-weight: 700;
            }
            QHeaderView::section {
                background: #eef2f7;
                color: #111827;
                border: 0;
                border-right: 1px solid #d5dce6;
                border-bottom: 1px solid #d5dce6;
                padding: 6px 8px;
                font-weight: 700;
            }
            QPushButton {
                background: #f8fafc;
                border: 1px solid #d5dce6;
                border-radius: 6px;
                color: #111827;
                padding: 10px 14px;
                font-size: 10pt;
                font-weight: 700;
            }
            QPushButton:hover {
                background: #eef4ff;
                border-color: #8aa8f8;
            }
            QPushButton:pressed {
                background: #dbeafe;
                border-color: #2f6fed;
            }
            QPushButton:disabled {
                background: #e5e7eb;
                border-color: #cbd5e1;
                color: #94a3b8;
            }
            QPushButton[compact="true"] {
                padding: 3px 8px;
                font-size: 10pt;
            }
            QProgressBar {
                border: 0;
                background: #dbe4ee;
                min-height: 10px;
                max-height: 10px;
            }
            QProgressBar::chunk {
                background: #2563eb;
            }
            QRadioButton, QCheckBox {
                background: transparent;
                spacing: 4px;
            }
            QRadioButton:disabled, QCheckBox:disabled, QLabel:disabled {
                color: #94a3b8;
            }
            QRadioButton::indicator {
                width: 18px;
                height: 18px;
                border: 2px solid #94a3b8;
                border-radius: 9px;
                background: #ffffff;
            }
            QCheckBox::indicator {
                width: 18px;
                height: 18px;
                border: 2px solid #94a3b8;
                border-radius: 9px;
                background: #ffffff;
            }
            QRadioButton::indicator:disabled, QCheckBox::indicator:disabled {
                border-color: #cbd5e1;
                background: #e5e7eb;
            }
            QRadioButton::indicator:checked:disabled, QCheckBox::indicator:checked:disabled {
                border-color: #cbd5e1;
                background: #d1d5db;
            }
            QRadioButton::indicator:hover, QCheckBox::indicator:hover {
                border-color: #60a5fa;
            }
            QRadioButton::indicator:checked {
                border: 2px solid #2563eb;
                border-radius: 9px;
                background: qradialgradient(
                    cx: 0.5, cy: 0.5, radius: 0.55, fx: 0.5, fy: 0.5,
                    stop: 0 #2563eb,
                    stop: 0.34 #2563eb,
                    stop: 0.35 #ffffff,
                    stop: 1 #ffffff
                );
            }
            QCheckBox::indicator:checked {
                border: 2px solid #2563eb;
                border-radius: 9px;
                background: qradialgradient(
                    cx: 0.5, cy: 0.5, radius: 0.55, fx: 0.5, fy: 0.5,
                    stop: 0 #2563eb,
                    stop: 0.34 #2563eb,
                    stop: 0.35 #ffffff,
                    stop: 1 #ffffff
                );
            }
            QLineEdit, QComboBox, QDoubleSpinBox {
                background: #ffffff;
                border: 1px solid #c8d0da;
                padding: 6px 8px;
            }
            QScrollBar:vertical {
                background: #e2e8f0;
                border-left: 1px solid #cbd5e1;
                width: 14px;
                margin: 0;
            }
            QScrollBar:horizontal {
                background: #e2e8f0;
                border-top: 1px solid #cbd5e1;
                height: 14px;
                margin: 0;
            }
            QScrollBar::handle:vertical {
                background: #94a3b8;
                border-radius: 6px;
                min-height: 48px;
                margin: 2px;
            }
            QScrollBar::handle:horizontal {
                background: #94a3b8;
                border-radius: 6px;
                min-width: 48px;
                margin: 2px;
            }
            QScrollBar::handle:vertical:hover {
                background: #64748b;
            }
            QScrollBar::handle:horizontal:hover {
                background: #64748b;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0;
                background: transparent;
                border: 0;
            }
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
                width: 0;
                background: transparent;
                border: 0;
            }
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
                background: transparent;
            }
            QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {
                background: transparent;
            }
            """
        )

    def _build_ui(self) -> None:
        central = QWidget()
        shell = QHBoxLayout(central)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)

        sidebar = QFrame()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(200)
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(0, 0, 0, 0)
        sidebar_layout.setSpacing(0)

        title = QLabel(APP_NAME)
        title.setObjectName("SidebarTitle")
        sidebar_layout.addWidget(title)

        self.module_buttons: dict[str, QPushButton] = {}
        self._build_module_button(sidebar_layout, "lyrics", "歌词检索")
        self._build_module_button(sidebar_layout, "align", "波形对齐")
        self._build_module_button(sidebar_layout, "hires", "Hi-Res 生成")
        sidebar_layout.addStretch(1)

        self.page_stack = QStackedWidget()
        self.lyrics_page = self._build_lyrics_page()
        self.align_page = self._build_alignment_page()
        self.hires_page = self._build_hires_page()
        self.module_pages = {
            "lyrics": self.lyrics_page,
            "align": self.align_page,
            "hires": self.hires_page,
        }
        self.page_stack.addWidget(self.lyrics_page)
        self.page_stack.addWidget(self.align_page)
        self.page_stack.addWidget(self.hires_page)

        shell.addWidget(sidebar)
        shell.addWidget(self.page_stack, 1)
        self.setCentralWidget(central)
        self._show_module("lyrics")

    def _build_module_button(self, layout: QVBoxLayout, module_id: str, label: str) -> None:
        button = QPushButton(label)
        button.setFlat(True)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.clicked.connect(lambda checked=False, module_id=module_id: self._show_module(module_id))
        layout.addWidget(button)
        self.module_buttons[module_id] = button

    def _show_module(self, module_id: str) -> None:
        self.active_module = module_id
        self.page_stack.setCurrentWidget(self.module_pages[module_id])
        for current_id, button in self.module_buttons.items():
            if current_id == module_id:
                button.setStyleSheet(
                    """
                    QPushButton {
                        background: #2563eb;
                        color: #ffffff;
                        border: 0;
                        padding: 14px 18px;
                        text-align: left;
                        font-size: 11pt;
                        font-weight: 700;
                    }
                    QPushButton:hover { background: #1d4ed8; }
                    QPushButton:pressed { background: #1e40af; }
                    """
                )
            else:
                button.setStyleSheet(
                    """
                    QPushButton {
                        background: #111827;
                        color: #d1d5db;
                        border: 0;
                        padding: 14px 18px;
                        text-align: left;
                        font-size: 11pt;
                        font-weight: 700;
                    }
                    QPushButton:hover { background: #1f2937; color: #ffffff; }
                    QPushButton:pressed { background: #0f172a; color: #ffffff; }
                    """
                )

    def _bind_shortcuts(self) -> None:
        self.shortcut_space = QShortcut(QKeySequence(Qt.Key.Key_Space), self)
        self.shortcut_space.activated.connect(self._handle_align_space_shortcut)
        self.shortcut_export = QShortcut(QKeySequence("Ctrl+S"), self)
        self.shortcut_export.activated.connect(self._handle_align_export_shortcut)
        self.shortcut_auto = QShortcut(QKeySequence("Ctrl+D"), self)
        self.shortcut_auto.activated.connect(self._handle_align_auto_shortcut)
        self.shortcut_drag_mode = QShortcut(QKeySequence("Alt+V"), self)
        self.shortcut_drag_mode.activated.connect(self._handle_align_drag_mode_shortcut)

    def _focused_widget_is_text_input(self) -> bool:
        widget = QApplication.focusWidget()
        return isinstance(widget, (QLineEdit, QPlainTextEdit, QComboBox, QDoubleSpinBox))

    def _handle_align_space_shortcut(self) -> None:
        if self.active_module != "align" or self._focused_widget_is_text_input():
            return
        if self.align_preview_process is not None and self.align_preview_process.is_running():
            self._stop_alignment_preview()
            return
        if self.waveform_view.video_waveform is not None and self.waveform_view.audio_waveform is not None:
            self._start_alignment_preview()
        else:
            self._start_alignment_analysis()

    def _handle_align_export_shortcut(self) -> None:
        if self.active_module != "align" or self._focused_widget_is_text_input():
            return
        self._start_aligned_export()

    def _handle_align_auto_shortcut(self) -> None:
        if self.active_module != "align" or self._focused_widget_is_text_input():
            return
        self._auto_align_waveforms()

    def _handle_align_drag_mode_shortcut(self) -> None:
        if self.active_module != "align" or self._focused_widget_is_text_input():
            return
        if self.align_drag_pan_radio.isChecked():
            self.align_drag_offset_radio.setChecked(True)
        else:
            self.align_drag_pan_radio.setChecked(True)

    def _build_lyrics_page(self) -> QWidget:
        page = QWidget()
        shell = QVBoxLayout(page)
        shell.setContentsMargins(20, 20, 20, 20)
        shell.setSpacing(14)

        header = QVBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(8)
        title = QLabel("歌词检索")
        title.setStyleSheet('font-size: 20pt; font-weight: 700;')
        desc = QLabel(
            "输入歌名、歌手、专辑或歌词片段后搜索歌曲；结果会优先保留各来源原始搜索顺位，再用歌名、歌手、专辑等匹配度修正。"
        )
        desc.setWordWrap(True)
        header.addWidget(title)
        header.addWidget(desc)
        shell.addLayout(header)

        search_panel = QFrame()
        search_panel.setObjectName("WhitePanel")
        search_layout = QGridLayout(search_panel)
        search_layout.setContentsMargins(14, 14, 14, 14)
        search_layout.setHorizontalSpacing(10)
        search_layout.setVerticalSpacing(8)

        self.lyrics_source_combo = QComboBox()
        self.lyrics_source_combo.addItem("聚合", DEFAULT_LYRICS_PROVIDER_IDS)
        self.lyrics_source_combo.addItem("QQ音乐", ("qm",))
        self.lyrics_source_combo.addItem("酷狗音乐", ("kg",))
        self.lyrics_source_combo.addItem("网易云音乐", ("ne",))
        self.lyrics_source_combo.addItem("LRCLIB", ("lrclib",))
        self.lyrics_source_combo.setFont(build_lyrics_ui_font(point_size=10.5))
        self._install_single_click_combo_behavior(self.lyrics_source_combo)
        self.lyrics_source_combo.currentIndexChanged.connect(self._persist_lyrics_preferences)

        self.lyrics_keyword_edit = QLineEdit()
        self.lyrics_keyword_edit.setPlaceholderText("例如：Recollect / Reweave / Redo / Realize")
        self.lyrics_keyword_edit.returnPressed.connect(self._start_lyrics_search)
        self.lyrics_search_button = QPushButton("搜索歌曲")
        self.lyrics_search_button.clicked.connect(self._start_lyrics_search)
        self.lyrics_status_label = QLabel("当前支持聚合搜索，也可以手动切换到 QQ音乐、酷狗音乐、网易云音乐或 LRCLIB 单源搜索。")
        self.lyrics_status_label.setWordWrap(True)
        self.lyrics_status_label.setStyleSheet('font-size: 9pt; color: #475569;')
        self.lyrics_status_label.setFont(build_lyrics_ui_font(point_size=9.5))
        search_layout.addWidget(QLabel("搜索关键词"), 0, 0)
        search_layout.addWidget(self.lyrics_source_combo, 0, 1)
        search_layout.addWidget(self.lyrics_keyword_edit, 0, 2)
        search_layout.addWidget(self.lyrics_search_button, 0, 3)
        search_layout.addWidget(self.lyrics_status_label, 1, 1, 1, 3)
        search_layout.setColumnStretch(2, 1)
        shell.addWidget(search_panel)

        content = QHBoxLayout()
        content.setContentsMargins(0, 0, 0, 0)
        content.setSpacing(14)

        result_panel = QFrame()
        result_panel.setObjectName("WhitePanel")
        result_layout = QVBoxLayout(result_panel)
        result_layout.setContentsMargins(14, 14, 14, 14)
        result_layout.setSpacing(10)
        result_title = QLabel("匹配结果")
        result_title.setObjectName("PanelTitle")
        self.lyrics_results_summary_label = QLabel("还没有搜索结果。")
        self.lyrics_results_summary_label.setStyleSheet('font-size: 9pt; color: #475569;')
        self.lyrics_results_summary_label.setFont(build_lyrics_ui_font(point_size=9.5))
        self.lyrics_results_table = QTableWidget(0, 5)
        self.lyrics_results_table.setObjectName("LyricsResultsTable")
        self.lyrics_results_table.setHorizontalHeaderLabels(["歌曲", "艺术家", "专辑", "时长", "来源"])
        self.lyrics_results_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.lyrics_results_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.lyrics_results_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.lyrics_results_table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.lyrics_results_table.setAlternatingRowColors(False)
        self.lyrics_results_table.setShowGrid(False)
        self.lyrics_results_table.setMouseTracking(True)
        self.lyrics_results_table.viewport().setMouseTracking(True)
        self.lyrics_results_table.viewport().setCursor(Qt.CursorShape.PointingHandCursor)
        self.lyrics_results_table.setWordWrap(False)
        self.lyrics_results_table.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.lyrics_results_table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.lyrics_results_table.setFont(build_lyrics_ui_font(point_size=10.5))
        self.lyrics_results_table.verticalHeader().setVisible(False)
        self.lyrics_results_table.verticalHeader().setDefaultSectionSize(42)
        self.lyrics_results_table.horizontalHeader().setStretchLastSection(False)
        self.lyrics_results_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.lyrics_results_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        self.lyrics_results_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        self.lyrics_results_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        self.lyrics_results_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        self.lyrics_results_table.installEventFilter(self)
        self.lyrics_results_table.currentCellChanged.connect(self._handle_lyrics_result_selected)
        self.lyrics_results_table.verticalScrollBar().valueChanged.connect(self._maybe_load_more_lyrics_results)
        result_layout.addWidget(result_title)
        result_layout.addWidget(self.lyrics_results_summary_label)
        result_layout.addWidget(self.lyrics_results_table, 1)
        QTimer.singleShot(0, self._resize_lyrics_results_columns)
        content.addWidget(result_panel, 7)

        preview_panel = QFrame()
        preview_panel.setObjectName("WhitePanel")
        preview_layout = QVBoxLayout(preview_panel)
        preview_layout.setContentsMargins(14, 14, 14, 14)
        preview_layout.setSpacing(10)
        preview_header = QHBoxLayout()
        preview_header.setContentsMargins(0, 0, 0, 0)
        preview_title = QLabel("歌词预览")
        preview_title.setObjectName("PanelTitle")
        preview_header.addWidget(preview_title)
        preview_header.addStretch(1)
        self.copy_lyrics_button = QPushButton("复制歌词")
        self.copy_lyrics_button.clicked.connect(self._copy_current_lyrics_preview)
        self.copy_lyrics_button.setMinimumHeight(34)
        preview_header.addWidget(self.copy_lyrics_button)
        self.lyrics_strip_intro_checkbox = QCheckBox("省略歌曲介绍")
        self.lyrics_strip_intro_checkbox.setChecked(True)
        self.lyrics_strip_intro_checkbox.toggled.connect(lambda _: self._refresh_lyrics_preview())
        self.lyrics_strip_intro_checkbox.toggled.connect(self._persist_lyrics_preferences)
        preview_header.addWidget(self.lyrics_strip_intro_checkbox)
        preview_header.addWidget(QLabel("显示格式"))
        self.lyrics_preview_mode_combo = QComboBox()
        self.lyrics_preview_mode_combo.addItem("按行 LRC", LYRICS_PREVIEW_LINE)
        self.lyrics_preview_mode_combo.addItem("按字 LRC", LYRICS_PREVIEW_VERBATIM)
        self.lyrics_preview_mode_combo.currentIndexChanged.connect(lambda _: self._refresh_lyrics_preview())
        self.lyrics_preview_mode_combo.currentIndexChanged.connect(self._persist_lyrics_preferences)
        self._install_single_click_combo_behavior(self.lyrics_preview_mode_combo)
        preview_header.addWidget(self.lyrics_preview_mode_combo)

        self.lyrics_preview_title_label = QLabel("未选择歌曲")
        self.lyrics_preview_title_label.setStyleSheet('font-size: 12pt; font-weight: 700;')
        self.lyrics_preview_title_label.setWordWrap(True)
        self.lyrics_preview_title_label.setFont(build_lyrics_ui_font(point_size=12, bold=True))
        self.lyrics_preview_meta_label = QLabel("来源: -")
        self.lyrics_preview_meta_label.setWordWrap(True)
        self.lyrics_preview_meta_label.setFont(build_lyrics_ui_font(point_size=10.5))
        self.lyrics_match_summary_label = QLabel("匹配字段: -")
        self.lyrics_match_summary_label.setWordWrap(True)
        self.lyrics_match_summary_label.setStyleSheet('font-size: 9pt; color: #475569;')
        self.lyrics_match_summary_label.setFont(build_lyrics_ui_font(point_size=9.5))
        self.lyrics_preview_hint_label = QLabel("搜索后选择一首歌，即可查看逐行或按字的 LRC 预览。")
        self.lyrics_preview_hint_label.setWordWrap(True)
        self.lyrics_preview_hint_label.setStyleSheet('font-size: 9pt; color: #475569;')
        self.lyrics_preview_hint_label.setFont(build_lyrics_ui_font(point_size=9.5))
        self.lyrics_preview_edit = QPlainTextEdit()
        self.lyrics_preview_edit.setReadOnly(True)
        self.lyrics_preview_edit.setObjectName("LyricsPreviewText")
        self.lyrics_preview_edit.setFont(build_lyrics_ui_font(point_size=11))
        self.lyrics_preview_edit.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.lyrics_preview_edit.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.lyrics_preview_edit.setPlaceholderText("歌词会显示在这里。")

        preview_layout.addLayout(preview_header)
        preview_layout.addWidget(self.lyrics_preview_title_label)
        preview_layout.addWidget(self.lyrics_preview_meta_label)
        preview_layout.addWidget(self.lyrics_match_summary_label)
        preview_layout.addWidget(self.lyrics_preview_hint_label)
        preview_layout.addWidget(self.lyrics_preview_edit, 1)
        content.addWidget(preview_panel, 6)

        shell.addLayout(content, 1)
        self._clear_lyrics_results()
        return page

    def _start_lyrics_search(self, *, load_more: bool = False) -> None:
        if self.lyrics_search_task is not None and self.lyrics_search_task.isRunning():
            return
        if load_more and not self.lyrics_has_more_results:
            return
        keyword = self.lyrics_search_keyword if load_more else self.lyrics_keyword_edit.text().strip()
        if not keyword:
            QMessageBox.information(self, APP_TITLE, "请输入搜索关键词。")
            return

        self.lyrics_search_button.setEnabled(False)
        provider_ids = self.lyrics_search_provider_ids if load_more else self.lyrics_source_combo.currentData()
        if not isinstance(provider_ids, tuple):
            provider_ids = DEFAULT_LYRICS_PROVIDER_IDS
        if load_more:
            self._lyrics_loading_more = True
            selected_key = self.lyrics_selected_candidate.key if self.lyrics_selected_candidate is not None else ""
            self._render_lyrics_results_table(selected_key=selected_key)
            self.lyrics_status_label.setText(f"已加载 {len(self.lyrics_search_results)} 条结果，正在加载更多…")
        else:
            self._lyrics_loading_more = False
            self.lyrics_status_label.setText("正在搜索歌词候选歌曲…")
            self.lyrics_search_keyword = keyword
            self.lyrics_search_provider_ids = provider_ids
            self.lyrics_next_provider_pages = {}
            self.lyrics_has_more_results = False
            self.lyrics_pending_results = []
            self._clear_lyrics_results()

        def runner(logger: Callable[[str], None]) -> tuple[bool, LyricsSearchBatch]:
            _ = logger
            return (
                load_more,
                self.lyrics_search_service.search_batch(
                    keyword,
                    provider_ids=provider_ids,
                    limit=DEFAULT_LYRICS_SEARCH_LIMIT,
                    provider_pages=self.lyrics_next_provider_pages if load_more else None,
                ),
            )

        self.lyrics_search_task = BackgroundTask(runner)
        self.lyrics_search_task.task_succeeded.connect(self._finish_lyrics_search_success)
        self.lyrics_search_task.task_failed.connect(self._finish_lyrics_search_failure)
        self.lyrics_search_task.start()

    def _finish_lyrics_search_success(self, results: object) -> None:
        self.lyrics_search_task = None
        self.lyrics_search_button.setEnabled(True)
        load_more = False
        payload = results
        if isinstance(results, tuple) and len(results) == 2 and isinstance(results[0], bool):
            load_more = results[0]
            payload = results[1]

        batch = payload if isinstance(payload, LyricsSearchBatch) else None
        batch_results = list(batch.results) if batch is not None else (list(results) if isinstance(results, list) else [])
        if batch is not None:
            self.lyrics_pending_results.extend(batch.overflow_results)
        self.lyrics_next_provider_pages = dict(batch.next_provider_pages) if batch is not None else {}
        self.lyrics_has_more_results = bool(batch.has_more or self.lyrics_pending_results) if batch is not None else False

        if load_more:
            existing_keys = {candidate.key for candidate in self.lyrics_search_results}
            for candidate in batch_results:
                if candidate.key not in existing_keys:
                    self.lyrics_search_results.append(candidate)
                    existing_keys.add(candidate.key)
        else:
            self.lyrics_search_results = batch_results

        if not self.lyrics_search_results:
            self.lyrics_status_label.setText("没有找到匹配的歌词结果。")
            self._clear_lyrics_results()
            return

        selected_key = self.lyrics_selected_candidate.key if self.lyrics_selected_candidate is not None else ""
        self._render_lyrics_results_table(selected_key=selected_key if load_more else "")
        selected_source = self.lyrics_source_combo.currentText()
        if selected_source == "聚合":
            self.lyrics_status_label.setText(
                f"已加载 {len(self.lyrics_search_results)} 条候选结果，来源优先级：QQ > 酷狗 > 网易云 > LRCLIB。"
            )
        else:
            self.lyrics_status_label.setText(f"已加载 {len(self.lyrics_search_results)} 条候选结果，当前来源：{selected_source}。")
        self.lyrics_results_summary_label.setText(
            "结果优先保留各来源原始搜索顺位，再按歌曲、艺术家、专辑匹配度修正；同一首歌会保留不同来源。"
            + (" 向下滚动可继续加载更多结果。" if self.lyrics_has_more_results else "")
        )
        self._lyrics_loading_more = False
        self._render_lyrics_results_table(selected_key=selected_key if load_more else "")

    def _finish_lyrics_search_failure(self, message: str) -> None:
        self.lyrics_search_task = None
        self.lyrics_search_button.setEnabled(True)
        self._lyrics_loading_more = False
        if not self.lyrics_search_results:
            self._clear_lyrics_results()
        else:
            selected_key = self.lyrics_selected_candidate.key if self.lyrics_selected_candidate is not None else ""
            self._render_lyrics_results_table(selected_key=selected_key)
        self.lyrics_status_label.setText("歌词搜索失败。")
        QMessageBox.critical(self, APP_TITLE, message or "歌词搜索失败。")

    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        if watched is self.lyrics_results_table and event.type() in {QEvent.Type.Resize, QEvent.Type.Show}:
            QTimer.singleShot(0, self._resize_lyrics_results_columns)
        return super().eventFilter(watched, event)

    def _resize_lyrics_results_columns(self) -> None:
        viewport_width = self.lyrics_results_table.viewport().width()
        if viewport_width <= 0:
            return

        duration_width = 92
        source_width = 96
        remaining = max(120, viewport_width - duration_width - source_width)
        song_width = int(remaining * 0.36)
        artist_width = int(remaining * 0.27)
        album_width = max(0, remaining - song_width - artist_width)

        self.lyrics_results_table.setColumnWidth(0, song_width)
        self.lyrics_results_table.setColumnWidth(1, artist_width)
        self.lyrics_results_table.setColumnWidth(2, album_width)
        self.lyrics_results_table.setColumnWidth(3, duration_width)
        self.lyrics_results_table.setColumnWidth(4, source_width)

    def _render_lyrics_results_table(self, *, selected_key: str = "") -> None:
        row_count = len(self.lyrics_search_results) + (1 if self._lyrics_loading_more and self.lyrics_search_results else 0)
        self.lyrics_results_table.clearSpans()
        self.lyrics_results_table.setRowCount(row_count)
        self._resize_lyrics_results_columns()
        selected_row = -1
        for row, candidate in enumerate(self.lyrics_search_results):
            duration_text = format_media_duration(candidate.duration_seconds) if candidate.duration_seconds else "-"
            items = [
                QTableWidgetItem(candidate.title or "-"),
                QTableWidgetItem(candidate.artist or "-"),
                QTableWidgetItem(candidate.album or "-"),
                QTableWidgetItem(duration_text),
                QTableWidgetItem(candidate.provider_name),
            ]
            for column, item in enumerate(items):
                item.setData(Qt.ItemDataRole.UserRole, row)
                if column == 3:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.lyrics_results_table.setItem(row, column, item)
            if selected_key and candidate.key == selected_key:
                selected_row = row

        if self._lyrics_loading_more and self.lyrics_search_results:
            loading_row = len(self.lyrics_search_results)
            loading_item = QTableWidgetItem("加载中...")
            loading_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            loading_item.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self.lyrics_results_table.setSpan(loading_row, 0, 1, self.lyrics_results_table.columnCount())
            self.lyrics_results_table.setItem(loading_row, 0, loading_item)

        if selected_row < 0 and self.lyrics_search_results:
            selected_row = 0
        if selected_row >= 0:
            self.lyrics_results_table.selectRow(selected_row)
            self._handle_lyrics_result_selected(selected_row, 0, -1, -1)

    def _maybe_load_more_lyrics_results(self) -> None:
        if not self.lyrics_has_more_results or self._lyrics_loading_more:
            return
        if self.lyrics_search_task is not None and self.lyrics_search_task.isRunning():
            return
        scrollbar = self.lyrics_results_table.verticalScrollBar()
        if scrollbar.maximum() <= 0:
            return
        if scrollbar.value() < scrollbar.maximum() - 12:
            return
        if not self.lyrics_search_keyword:
            return
        if self.lyrics_pending_results:
            self._append_pending_lyrics_results()
            return
        self._start_lyrics_search(load_more=True)

    def _append_pending_lyrics_results(self) -> None:
        if not self.lyrics_pending_results:
            self.lyrics_has_more_results = bool(self.lyrics_next_provider_pages)
            return
        selected_key = self.lyrics_selected_candidate.key if self.lyrics_selected_candidate is not None else ""
        chunk = self.lyrics_pending_results[:DEFAULT_LYRICS_SEARCH_LIMIT]
        self.lyrics_pending_results = self.lyrics_pending_results[DEFAULT_LYRICS_SEARCH_LIMIT:]
        existing_keys = {candidate.key for candidate in self.lyrics_search_results}
        for candidate in chunk:
            if candidate.key not in existing_keys:
                self.lyrics_search_results.append(candidate)
                existing_keys.add(candidate.key)
        self.lyrics_has_more_results = bool(self.lyrics_pending_results or self.lyrics_next_provider_pages)
        self._render_lyrics_results_table(selected_key=selected_key)
        selected_source = self.lyrics_source_combo.currentText()
        if selected_source == "聚合":
            self.lyrics_status_label.setText(
                f"已加载 {len(self.lyrics_search_results)} 条候选结果，来源优先级：QQ > 酷狗 > 网易云 > LRCLIB。"
            )
        else:
            self.lyrics_status_label.setText(f"已加载 {len(self.lyrics_search_results)} 条候选结果，当前来源：{selected_source}。")
        self.lyrics_results_summary_label.setText(
            "结果优先保留各来源原始搜索顺位，再按歌曲、艺术家、专辑匹配度修正；同一首歌会保留不同来源。"
            + (" 向下滚动可继续加载更多结果。" if self.lyrics_has_more_results else "")
        )

    def _handle_lyrics_result_selected(
        self,
        current_row: int,
        current_column: int,
        previous_row: int,
        previous_column: int,
    ) -> None:
        _ = current_column, previous_row, previous_column
        if current_row < 0 or current_row >= len(self.lyrics_search_results):
            self.lyrics_selected_candidate = None
            self._refresh_lyrics_preview()
            return
        self.lyrics_selected_candidate = self.lyrics_search_results[current_row]
        self._ensure_selected_lyrics_loaded()
        self._refresh_lyrics_preview()

    def _refresh_lyrics_preview(self) -> None:
        candidate = self.lyrics_selected_candidate
        if candidate is None:
            self.lyrics_preview_title_label.setText("未选择歌曲")
            self.lyrics_preview_meta_label.setText("来源: -")
            self.lyrics_match_summary_label.setText("匹配字段: -")
            self.lyrics_preview_hint_label.setText("搜索后选择一首歌，即可查看逐行或按字的 LRC 预览。")
            self.lyrics_preview_edit.clear()
            return

        if candidate.load_error:
            self.lyrics_preview_title_label.setText(f"{candidate.title or '未命名'}")
            self.lyrics_preview_meta_label.setText(
                f"歌手: {candidate.artist or '-'}    专辑: {candidate.album or '-'}    来源: {candidate.provider_name}"
            )
            self.lyrics_match_summary_label.setText("歌词加载失败")
            self.lyrics_preview_hint_label.setText(candidate.load_error)
            self.lyrics_preview_edit.setPlainText(candidate.load_error)
            return

        if not candidate.lyrics_loaded:
            self.lyrics_preview_title_label.setText(f"{candidate.title or '未命名'}")
            self.lyrics_preview_meta_label.setText(
                f"歌手: {candidate.artist or '-'}    专辑: {candidate.album or '-'}    来源: {candidate.provider_name}"
            )
            self.lyrics_match_summary_label.setText(
                "匹配字段: "
                f"{candidate.match_source}；歌名 {candidate.title_score:.0f} / "
                f"歌手 {candidate.artist_score:.0f} / 专辑 {candidate.album_score:.0f}"
            )
            self.lyrics_preview_hint_label.setText(f"正在从 {candidate.provider_name} 加载歌词…")
            self.lyrics_preview_edit.setPlainText("正在加载歌词…")
            return

        preview_mode = self.lyrics_preview_mode_combo.currentData()
        if not isinstance(preview_mode, str):
            preview_mode = LYRICS_PREVIEW_LINE
        preview = build_lyrics_preview(
            candidate,
            preview_mode,
            strip_intro_lines=self.lyrics_strip_intro_checkbox.isChecked(),
        )
        self.lyrics_preview_title_label.setText(f"{candidate.title or '未命名'}")
        self.lyrics_preview_meta_label.setText(
            f"歌手: {candidate.artist or '-'}    专辑: {candidate.album or '-'}    来源: {candidate.provider_name}"
        )
        self.lyrics_match_summary_label.setText(
            "匹配字段: "
            f"{candidate.match_source}；歌名 {candidate.title_score:.0f} / "
            f"歌手 {candidate.artist_score:.0f} / 专辑 {candidate.album_score:.0f} / "
            f"歌词 {candidate.lyrics_score:.0f}"
        )
        self.lyrics_preview_hint_label.setText(self._build_lyrics_preview_hint(candidate, preview))
        self.lyrics_preview_edit.setPlainText(preview.text or "当前结果没有可显示的歌词。")

    def _build_lyrics_preview_hint(self, candidate: LyricsSearchCandidate, preview: LyricsPreview) -> str:
        if preview.used_synced_lyrics and preview.used_estimated_char_timing:
            return (
                f"{candidate.provider_name} 提供了逐行同步歌词；当前“按字 LRC”是基于相邻行时间做的轻量估算，"
                "方便先预览卡拉 OK 节奏。"
            )
        if preview.used_synced_lyrics:
            return f"{candidate.provider_name} 提供了同步歌词，当前优先显示这个来源的字幕。"
        return f"{candidate.provider_name} 当前只有纯文本歌词，暂时无法提供真实时间轴。"

    def _copy_current_lyrics_preview(self) -> None:
        clipboard = QApplication.clipboard()
        if clipboard is None:
            return
        clipboard.setText(self.lyrics_preview_edit.toPlainText())
        QToolTip.showText(
            self.copy_lyrics_button.mapToGlobal(self.copy_lyrics_button.rect().center()),
            "歌词已复制到剪切板",
            self.copy_lyrics_button,
            self.copy_lyrics_button.rect(),
            1600,
        )

    def _ensure_selected_lyrics_loaded(self) -> None:
        candidate = self.lyrics_selected_candidate
        if candidate is None or candidate.lyrics_loaded:
            return
        if self.lyrics_fetch_task is not None and self.lyrics_fetch_task.isRunning():
            return

        self._lyrics_loading_key = candidate.key

        def runner(logger: Callable[[str], None]) -> LyricsSearchCandidate:
            _ = logger
            return self.lyrics_search_service.fetch_lyrics(candidate)

        self.lyrics_fetch_task = BackgroundTask(runner)
        self.lyrics_fetch_task.task_succeeded.connect(self._finish_lyrics_fetch_success)
        self.lyrics_fetch_task.task_failed.connect(self._finish_lyrics_fetch_failure)
        self.lyrics_fetch_task.start()

    def _finish_lyrics_fetch_success(self, result: object) -> None:
        self.lyrics_fetch_task = None
        self._lyrics_loading_key = ""
        loaded_candidate = result if isinstance(result, LyricsSearchCandidate) else None
        if loaded_candidate is not None:
            for index, candidate in enumerate(self.lyrics_search_results):
                if candidate.key == loaded_candidate.key:
                    self.lyrics_search_results[index] = loaded_candidate
                    if self.lyrics_selected_candidate is not None and self.lyrics_selected_candidate.key == loaded_candidate.key:
                        self.lyrics_selected_candidate = loaded_candidate
                    break
        self._refresh_lyrics_preview()
        if self.lyrics_selected_candidate is not None and not self.lyrics_selected_candidate.lyrics_loaded:
            self._ensure_selected_lyrics_loaded()

    def _finish_lyrics_fetch_failure(self, message: str) -> None:
        self.lyrics_fetch_task = None
        failed_key = self._lyrics_loading_key
        self._lyrics_loading_key = ""
        for candidate in self.lyrics_search_results:
            if candidate.key == failed_key:
                candidate.load_error = message or f"{candidate.provider_name} 歌词加载失败。"
                if self.lyrics_selected_candidate is not None and self.lyrics_selected_candidate.key == failed_key:
                    self.lyrics_selected_candidate = candidate
                break
        self._refresh_lyrics_preview()
        if self.lyrics_selected_candidate is not None and not self.lyrics_selected_candidate.lyrics_loaded and not self.lyrics_selected_candidate.load_error:
            self._ensure_selected_lyrics_loaded()

    def _clear_lyrics_results(self) -> None:
        self.lyrics_search_results = []
        self.lyrics_pending_results = []
        self.lyrics_selected_candidate = None
        self.lyrics_next_provider_pages = {}
        self.lyrics_has_more_results = False
        self._lyrics_loading_more = False
        self._lyrics_loading_key = ""
        self.lyrics_results_table.clearContents()
        self.lyrics_results_table.setRowCount(0)
        self.lyrics_results_summary_label.setText("还没有搜索结果。")
        self._refresh_lyrics_preview()

    def _build_hires_page(self) -> QWidget:
        page = QWidget()
        shell = QVBoxLayout(page)
        shell.setContentsMargins(20, 20, 20, 20)
        shell.setSpacing(0)

        header = QVBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(8)
        title = QLabel("卡拉 OK 字幕视频一键 Hi-Res 生成")
        title.setStyleSheet('font-family: "Microsoft YaHei UI"; font-size: 20pt; font-weight: 700;')
        desc = QLabel("把字幕视频拖进下方卡片，再按需放入原唱音频和 / 或伴奏音频。至少提供一条音频就可以开始生成。")
        desc.setWordWrap(True)
        header.addWidget(title)
        header.addWidget(desc)
        shell.addLayout(header)

        output_row = QHBoxLayout()
        output_row.setContentsMargins(0, 18, 0, 10)
        output_label = QLabel("输出目录")
        output_label.setStyleSheet('font-family: "Microsoft YaHei UI"; font-size: 11pt; font-weight: 700;')
        self.output_dir_label = QLabel("跟随字幕视频所在目录")
        self.output_dir_label.setWordWrap(True)
        output_row.addWidget(output_label)
        output_row.addSpacing(12)
        output_row.addWidget(self.output_dir_label, 1)
        shell.addLayout(output_row)

        ffmpeg_row = QGridLayout()
        ffmpeg_row.setContentsMargins(0, 0, 0, 14)
        ffmpeg_row.setHorizontalSpacing(12)
        ffmpeg_title = QLabel("FFmpeg 目录")
        ffmpeg_title.setStyleSheet('font-family: "Microsoft YaHei UI"; font-size: 11pt; font-weight: 700;')
        self.hires_ffmpeg_label = QLabel(FFMPEG_DIR_PLACEHOLDER)
        self.hires_ffmpeg_label.setWordWrap(True)
        settings_button = QPushButton("设置")
        settings_button.clicked.connect(lambda: self._open_settings_window("hires"))
        ffmpeg_hint = QLabel("提示: FFmpeg 目录、输出命名等偏好设置可在“设置”窗口中调整并保存到本地。")
        ffmpeg_hint.setWordWrap(True)
        ffmpeg_hint.setStyleSheet('font-family: "Microsoft YaHei UI"; font-size: 9pt; color: #6b7280;')
        ffmpeg_row.addWidget(ffmpeg_title, 0, 0)
        ffmpeg_row.addWidget(self.hires_ffmpeg_label, 0, 1)
        ffmpeg_row.addWidget(settings_button, 0, 2)
        ffmpeg_row.addWidget(ffmpeg_hint, 1, 1, 1, 2)
        ffmpeg_row.setColumnStretch(1, 1)
        shell.addLayout(ffmpeg_row)

        card_row = QHBoxLayout()
        card_row.setContentsMargins(0, 0, 0, 0)
        card_row.setSpacing(10)
        self.video_zone = DropZoneCard(
            title="字幕视频",
            hint="支持 mkv / mp4 / mov / avi\n这里会决定输出文件名和输出目录。",
            extensions=VIDEO_EXTENSIONS,
            min_height=190,
        )
        self.video_zone.browseRequested.connect(self._choose_video)
        self.video_zone.pathChanged.connect(self.set_video_path)

        self.on_vocal_zone = DropZoneCard(
            title="原唱音频",
            hint="支持 flac / wav / mp3 / m4a / aac / ape / alac / mkv / mp4\n可单独生成原唱 Hi-Res 视频，也可和伴奏一起生成。",
            extensions=HIRES_AUDIO_EXTENSIONS,
            min_height=190,
        )
        self.on_vocal_zone.browseRequested.connect(self._choose_on_audio)
        self.on_vocal_zone.pathChanged.connect(self.set_on_vocal_path)

        self.off_vocal_zone = DropZoneCard(
            title="伴奏音频",
            hint="支持 flac / wav / mp3 / m4a / aac / ape / alac / mkv / mp4\n可单独生成伴奏 Hi-Res 视频，也可和原唱一起生成。",
            extensions=HIRES_AUDIO_EXTENSIONS,
            min_height=190,
        )
        self.off_vocal_zone.browseRequested.connect(self._choose_off_audio)
        self.off_vocal_zone.pathChanged.connect(self.set_off_vocal_path)

        card_row.addWidget(self.video_zone, 1)
        card_row.addWidget(self.on_vocal_zone, 1)
        card_row.addWidget(self.off_vocal_zone, 1)
        shell.addLayout(card_row)
        shell.addSpacing(10)

        log_panel = QFrame()
        log_panel.setObjectName("WhitePanel")
        log_layout = QGridLayout(log_panel)
        log_layout.setContentsMargins(14, 14, 14, 14)
        log_layout.setVerticalSpacing(10)
        log_title = QLabel("处理日志")
        log_title.setObjectName("PanelTitle")
        self.hires_log = QPlainTextEdit()
        self.hires_log.setObjectName("LogText")
        self.hires_log.setReadOnly(True)
        log_layout.addWidget(log_title, 0, 0)
        log_layout.addWidget(self.hires_log, 1, 0)
        log_layout.setRowStretch(1, 1)
        shell.addWidget(log_panel, 1)
        shell.addSpacing(18)

        controls = QHBoxLayout()
        self.hires_start_button = QPushButton("开始生成")
        self.hires_start_button.clicked.connect(self._start_hires)
        clear_button = QPushButton("清空已选文件")
        clear_button.clicked.connect(self._clear_hires_inputs)
        open_output_button = QPushButton("打开输出目录")
        open_output_button.clicked.connect(self._open_hires_output_dir)
        self.hires_progress = QProgressBar()
        self.hires_progress.setRange(0, 1)
        self.hires_progress.setValue(0)
        self.hires_progress.setFixedWidth(180)
        self.hires_progress.setTextVisible(False)
        self.hires_status_label = QLabel("准备就绪")
        self.hires_status_label.setStyleSheet('font-family: "Microsoft YaHei UI"; font-size: 10pt; font-weight: 700;')
        controls.addWidget(self.hires_start_button)
        controls.addWidget(clear_button)
        controls.addWidget(open_output_button)
        controls.addStretch(1)
        controls.addWidget(self.hires_progress)
        controls.addSpacing(12)
        controls.addWidget(self.hires_status_label)
        shell.addLayout(controls)
        return page

    def _build_alignment_page(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        page = QWidget()
        shell = QVBoxLayout(page)
        shell.setContentsMargins(20, 20, 20, 20)
        shell.setSpacing(0)

        self.waveform_view = WaveformView()
        self.waveform_view.playheadChanged.connect(self._handle_playhead_changed)
        self.waveform_view.offsetChanged.connect(self._handle_waveform_offset_changed)
        self.waveform_view.trimChanged.connect(self._refresh_align_trim_status)

        header = QGridLayout()
        header.setContentsMargins(0, 0, 0, 0)
        title = QLabel("音频波形对齐")
        title.setStyleSheet('font-family: "Microsoft YaHei UI"; font-size: 20pt; font-weight: 700;')
        title_font = QFont("Microsoft YaHei UI", 20)
        title_font.setBold(True)
        apply_safe_label_metrics(title, title_font, top_padding=4, bottom_padding=3)
        alignment_title_height = title.sizeHint().height()
        desc = QLabel("把字幕视频和原唱音源放进来，选择要修正的对象，手动对齐波形后导出对应文件。")
        desc.setWordWrap(True)
        settings_button = QPushButton("设置")
        settings_button.clicked.connect(lambda: self._open_settings_window("align"))
        header.addWidget(title, 0, 0)
        header.addWidget(desc, 1, 0)
        header.addWidget(settings_button, 0, 1)
        header.setColumnStretch(0, 1)
        shell.addLayout(header)

        drop_row = QGridLayout()
        drop_row.setContentsMargins(0, 14, 0, 10)
        drop_row.setHorizontalSpacing(16)
        self.align_video_zone = DropZoneCard(
            title="字幕视频",
            hint="支持 mkv / mp4 / mov / avi\n用于读取原视频里的参考音轨。",
            extensions=VIDEO_EXTENSIONS,
            min_height=150,
        )
        self.align_video_zone.setFixedHeight(150)
        self.align_video_zone.browseRequested.connect(self._choose_align_video)
        self.align_video_zone.pathChanged.connect(self.set_align_video_path)

        self.align_audio_zone = DropZoneCard(
            title="原唱音源",
            hint="支持 flac / wav / mp3 / m4a / aac / ape / alac / mkv / mp4\n可拖入音频或带音轨的 mp4，作为固定参考或导出修正后的音频。",
            extensions=ALIGN_AUDIO_EXTENSIONS,
            min_height=150,
        )
        self.align_audio_zone.setFixedHeight(150)
        self.align_audio_zone.browseRequested.connect(self._choose_align_audio)
        self.align_audio_zone.pathChanged.connect(self.set_align_audio_path)

        self.align_video_info_label = QLabel("字幕视频: 时长未知")
        self.align_audio_info_label = QLabel("原唱音源: 时长未知")
        drop_row.addWidget(self.align_video_zone, 0, 0)
        drop_row.addWidget(self.align_audio_zone, 0, 1)
        drop_row.addWidget(self.align_video_info_label, 1, 0)
        drop_row.addWidget(self.align_audio_info_label, 1, 1)
        drop_row.setColumnStretch(0, 1)
        drop_row.setColumnStretch(1, 1)
        shell.addLayout(drop_row)

        actions = QHBoxLayout()
        actions.setContentsMargins(0, 0, 0, 10)
        actions.setSpacing(8)
        self.align_analyze_button = QPushButton("生成波形")
        self.align_analyze_button.clicked.connect(self._start_alignment_analysis)
        self.align_auto_button = QPushButton("自动对齐")
        self.align_auto_button.clicked.connect(self._auto_align_waveforms)
        self.align_auto_button.setStyleSheet("color: #1d4ed8;")
        self.align_preview_button = QPushButton("播放预览")
        self.align_preview_button.clicked.connect(self._start_alignment_preview)
        self.align_stop_preview_button = QPushButton("停止播放")
        self.align_stop_preview_button.clicked.connect(self._stop_alignment_preview)
        self.align_export_button = QPushButton("导出对齐视频")
        self.align_export_button.clicked.connect(self._start_aligned_export)
        self.align_stop_export_button = QPushButton("停止导出")
        self.align_stop_export_button.clicked.connect(self._stop_alignment_export)
        self.align_stop_export_button.setEnabled(False)
        open_output_button = QPushButton("打开输出目录")
        open_output_button.clicked.connect(self._open_align_output_dir)
        clear_button = QPushButton("清空已选文件")
        clear_button.clicked.connect(self._clear_alignment_inputs)
        self.align_progress = QProgressBar()
        self.align_progress.setRange(0, 1)
        self.align_progress.setValue(0)
        self.align_progress.setFixedWidth(170)
        self.align_progress.setTextVisible(False)
        self.align_status_label = QLabel("准备生成波形")
        self.align_status_label.setStyleSheet('font-family: "Microsoft YaHei UI"; font-size: 10pt; font-weight: 700;')
        for button in (
            self.align_analyze_button,
            self.align_auto_button,
            self.align_preview_button,
            self.align_stop_preview_button,
            self.align_export_button,
            self.align_stop_export_button,
            open_output_button,
            clear_button,
        ):
            actions.addWidget(button)
        self.align_open_output_button = open_output_button
        self.align_clear_button = clear_button
        actions.addStretch(1)
        actions.addWidget(self.align_progress)
        actions.addSpacing(12)
        actions.addWidget(self.align_status_label)
        shell.addLayout(actions)

        control_panel = QFrame()
        self.align_control_panel = control_panel
        control_panel.setObjectName("ControlPanel")
        control_layout = QGridLayout(control_panel)
        control_layout.setContentsMargins(14, 12, 14, 12)
        control_layout.setHorizontalSpacing(14)
        control_layout.setVerticalSpacing(10)

        self.align_offset_label = QLabel("字幕视频偏移 +0.000s")
        self.align_offset_label.setStyleSheet('font-family: "Microsoft YaHei UI"; font-size: 12pt; font-weight: 700;')
        offset_title_font = QFont("Microsoft YaHei UI", 12)
        offset_title_font.setBold(True)
        apply_safe_label_metrics(self.align_offset_label, offset_title_font)
        offset_font = QFont("Microsoft YaHei UI", 12)
        offset_font.setBold(True)
        offset_metrics = QFontMetrics(offset_font)
        self.align_offset_label.setFixedWidth(
            max(
                offset_metrics.horizontalAdvance("字幕视频偏移 -99.999s"),
                offset_metrics.horizontalAdvance("原唱音源偏移 -99.999s"),
            )
            + 12
        )
        control_layout.addWidget(self.align_offset_label, 0, 0)

        target_row_widget = QFrame()
        target_row_widget.setFrameShape(QFrame.Shape.NoFrame)
        target_row_widget.setStyleSheet("background: transparent; border: 0;")
        target_row = QHBoxLayout(target_row_widget)
        target_row.setContentsMargins(0, 0, 0, 0)
        self.align_target_video_radio = QRadioButton("调整字幕视频")
        self.align_target_audio_radio = QRadioButton("调整原唱音源")
        self.align_target_video_radio.setChecked(True)
        self.align_target_video_radio.toggled.connect(self._handle_align_target_changed)
        self.align_target_group = QButtonGroup(self)
        self.align_target_group.setExclusive(True)
        self.align_target_group.addButton(self.align_target_video_radio)
        self.align_target_group.addButton(self.align_target_audio_radio)
        self.align_playhead_label = QLabel("播放位置 0.000s")
        target_row.addWidget(QLabel("对齐目标"))
        target_row.addWidget(self.align_target_video_radio)
        target_row.addWidget(self.align_target_audio_radio)
        target_row.addWidget(self.align_playhead_label)
        target_row.addStretch(1)
        control_layout.addWidget(target_row_widget, 0, 1)

        drag_row_widget = QFrame()
        drag_row_widget.setFrameShape(QFrame.Shape.NoFrame)
        drag_row_widget.setStyleSheet("background: transparent; border: 0;")
        drag_row = QHBoxLayout(drag_row_widget)
        drag_row.setContentsMargins(0, 0, 0, 0)
        self.align_drag_offset_radio = QRadioButton("移动字幕视频")
        self.align_drag_pan_radio = QRadioButton("平移视图")
        self.align_drag_offset_radio.setChecked(True)
        self.align_drag_group = QButtonGroup(self)
        self.align_drag_group.setExclusive(True)
        self.align_drag_group.addButton(self.align_drag_offset_radio)
        self.align_drag_group.addButton(self.align_drag_pan_radio)
        self.align_drag_offset_radio.toggled.connect(
            lambda checked: self.waveform_view.set_drag_mode("offset" if checked else "pan")
        )
        drag_row.addWidget(QLabel("拖动模式"))
        drag_row.addWidget(self.align_drag_offset_radio)
        drag_row.addWidget(self.align_drag_pan_radio)
        drag_row.addStretch(1)
        control_layout.addWidget(drag_row_widget, 1, 0, 1, 2)

        nudge_row = QHBoxLayout()
        nudge_row.setContentsMargins(0, 0, 0, 0)
        nudge_row.setSpacing(6)
        for text, delta in (("-0.100s", -0.1), ("-0.010s", -0.01), ("+0.010s", 0.01), ("+0.100s", 0.1)):
            button = QPushButton(text)
            button.setMinimumWidth(92)
            button.setMinimumHeight(34)
            button.setStyleSheet("color: #111827;")
            button.clicked.connect(lambda checked=False, delta=delta: self.waveform_view.nudge_offset(delta))
            nudge_row.addWidget(button)
        reset_offset_button = QPushButton("归零")
        reset_offset_button.setMinimumWidth(92)
        reset_offset_button.setMinimumHeight(34)
        reset_offset_button.setStyleSheet("color: #111827;")
        reset_offset_button.clicked.connect(lambda: self.waveform_view.set_offset(0.0))
        nudge_row.addWidget(reset_offset_button)
        nudge_row.addStretch(1)
        control_layout.addLayout(nudge_row, 2, 0, 1, 2)

        trim_widget = QFrame()
        trim_widget.setObjectName("TrimRow")
        trim_widget.setFrameShape(QFrame.Shape.NoFrame)
        trim_widget.setMinimumHeight(40)
        trim_layout = QGridLayout(trim_widget)
        trim_layout.setContentsMargins(0, 0, 0, 0)
        trim_layout.setHorizontalSpacing(8)
        trim_layout.setVerticalSpacing(4)
        trim_header = QHBoxLayout()
        trim_header.setContentsMargins(0, 0, 0, 0)
        trim_header.setSpacing(8)
        trim_title_label = QLabel("视频尾裁")
        self.align_trim_label = QLabel("未设置")
        self.align_trim_label.setWordWrap(False)
        self.align_trim_label.setMinimumHeight(22)
        self.align_trim_mark_button = QPushButton("将当前播放头设为尾裁点")
        self.align_trim_mark_button.setMinimumHeight(34)
        self.align_trim_mark_button.clicked.connect(
            lambda: self.waveform_view.set_trim_end(self.waveform_view.playhead_seconds)
        )
        self.align_trim_clear_button = QPushButton("清除尾裁点")
        self.align_trim_clear_button.setMinimumHeight(34)
        self.align_trim_clear_button.clicked.connect(self.waveform_view.clear_trim_end)
        trim_header.addWidget(trim_title_label)
        trim_header.addWidget(self.align_trim_label)
        trim_header.addWidget(self.align_trim_mark_button)
        trim_header.addWidget(self.align_trim_clear_button)
        trim_header.addStretch(1)
        trim_layout.addLayout(trim_header, 0, 0)
        control_layout.addWidget(trim_widget, 3, 0, 1, 2)
        control_layout.setRowMinimumHeight(3, 40)

        option_row_widget = QFrame()
        option_row_widget.setFrameShape(QFrame.Shape.NoFrame)
        option_row_widget.setStyleSheet("background: transparent; border: 0;")
        option_row_widget.setMinimumHeight(24)
        option_row = QHBoxLayout(option_row_widget)
        option_row.setContentsMargins(0, 0, 0, 0)
        option_row.setSpacing(12)
        self.align_extra_wav_check = QCheckBox("额外导出一份原唱音源的wav文件")
        self.align_force_1080p60_check = QCheckBox("导出视频时重编码为 1080p 60fps")
        self.align_auto_trim_check = QCheckBox("导出视频时自动裁到音频末尾")
        self.align_use_video_audio_check = QCheckBox("导出视频时选择保留裁剪后的源视频音轨")
        self.align_auto_trim_check.toggled.connect(lambda _checked: self._refresh_align_trim_status(self.waveform_view.trim_end_seconds))
        self.align_use_video_audio_check.toggled.connect(self._persist_alignment_preferences)
        option_row.addWidget(self.align_extra_wav_check)
        option_row.addWidget(self.align_force_1080p60_check)
        option_row.addWidget(self.align_auto_trim_check)
        option_row.addWidget(self.align_use_video_audio_check)
        option_row.addStretch(1)
        control_layout.addWidget(option_row_widget, 4, 0, 1, 2)
        control_layout.setRowMinimumHeight(4, 24)

        self.align_lead_row_widget = QFrame()
        self.align_lead_row_widget.setFrameShape(QFrame.Shape.NoFrame)
        self.align_lead_row_widget.setStyleSheet("background: transparent; border: 0;")
        lead_row = QHBoxLayout(self.align_lead_row_widget)
        lead_row.setContentsMargins(0, 0, 0, 0)
        lead_row.addWidget(QLabel("视频前导画面"))
        self.align_lead_fill_black_radio = QRadioButton("前黑（补黑屏）")
        self.align_lead_fill_white_radio = QRadioButton("前白（补白屏）")
        self.align_lead_fill_freeze_radio = QRadioButton("首帧定格（用首帧补）")
        self.align_lead_fill_black_radio.setChecked(True)
        self.align_lead_fill_group = QButtonGroup(self)
        self.align_lead_fill_group.setExclusive(True)
        self.align_lead_fill_group.addButton(self.align_lead_fill_black_radio)
        self.align_lead_fill_group.addButton(self.align_lead_fill_white_radio)
        self.align_lead_fill_group.addButton(self.align_lead_fill_freeze_radio)
        lead_row.addWidget(self.align_lead_fill_black_radio)
        lead_row.addWidget(self.align_lead_fill_white_radio)
        lead_row.addWidget(self.align_lead_fill_freeze_radio)
        lead_row.addStretch(1)
        control_layout.addWidget(self.align_lead_row_widget, 5, 0, 1, 2)

        encode_row = QHBoxLayout()
        encode_row.setContentsMargins(0, 0, 0, 0)
        encode_row.addWidget(QLabel("视频编码方式"))
        self.align_encode_software_radio = QRadioButton("软编（CPU）")
        self.align_encode_hardware_radio = QRadioButton("硬编（GPU）")
        self.align_encode_software_radio.setChecked(True)
        self.align_encode_group = QButtonGroup(self)
        self.align_encode_group.setExclusive(True)
        self.align_encode_group.addButton(self.align_encode_software_radio)
        self.align_encode_group.addButton(self.align_encode_hardware_radio)
        encode_row.addWidget(self.align_encode_software_radio)
        encode_row.addWidget(self.align_encode_hardware_radio)
        encode_row.addStretch(1)
        self.align_encode_row_widget = QFrame()
        self.align_encode_row_widget.setFrameShape(QFrame.Shape.NoFrame)
        self.align_encode_row_widget.setStyleSheet("background: transparent; border: 0;")
        self.align_encode_row_widget.setLayout(encode_row)
        control_layout.addWidget(self.align_encode_row_widget, 6, 0, 1, 2)

        zoom_row = QHBoxLayout()
        zoom_row.setContentsMargins(0, 0, 0, 0)
        zoom_row.setSpacing(8)
        zoom_row.addWidget(QLabel("缩放"))
        self.align_zoom_slider = QSlider(Qt.Orientation.Horizontal)
        self.align_zoom_slider.setRange(20, 800)
        self.align_zoom_slider.setValue(120)
        self.align_zoom_slider.valueChanged.connect(lambda value: self.waveform_view.set_zoom(float(value)))
        jump_to_end_button = QPushButton("跳到末尾")
        jump_to_end_button.setMinimumHeight(42)
        jump_to_end_button.setMinimumWidth(86)
        jump_to_end_button.clicked.connect(self.waveform_view.jump_to_end)
        self.align_jump_to_end_button = jump_to_end_button
        reset_view_button = QPushButton("回到开头")
        reset_view_button.setMinimumHeight(42)
        reset_view_button.setMinimumWidth(86)
        reset_view_button.clicked.connect(self.waveform_view.reset_view)
        self.align_reset_view_button = reset_view_button
        zoom_row.addWidget(self.align_zoom_slider, 1)
        zoom_row.addSpacing(10)
        zoom_row.addWidget(jump_to_end_button)
        zoom_row.addWidget(reset_view_button)
        control_layout.addLayout(zoom_row, 7, 0, 1, 2)

        shortcut_hint = QLabel(
            "快捷键: 空格生成波形 / 播放 / 停止，Alt+V 切换拖动模式，Ctrl+D 自动对齐，Ctrl+S 导出当前对齐目标；自动对齐后请播放确认。"
        )
        shortcut_hint.setWordWrap(True)
        shortcut_hint.setStyleSheet(
            'font-family: "Microsoft YaHei UI"; font-size: 9pt; color: #6b7280; '
            'background: #f1f5f9; border-radius: 4px; padding: 4px 8px;'
        )
        control_layout.addWidget(shortcut_hint, 8, 0, 1, 2)
        control_panel.setFixedHeight(max(260, control_panel.sizeHint().height()))
        shell.addWidget(control_panel)
        shell.addSpacing(10)

        self.waveform_view.setFixedHeight(200)
        shell.addWidget(self.waveform_view)
        shell.addSpacing(10)

        log_panel = QFrame()
        log_panel.setObjectName("WhitePanel")
        log_layout = QGridLayout(log_panel)
        log_layout.setContentsMargins(14, 8, 14, 8)
        log_layout.setVerticalSpacing(6)
        log_title = QLabel("对齐日志")
        log_title.setObjectName("PanelTitle")
        self.align_log = QPlainTextEdit()
        self.align_log.setObjectName("LogText")
        self.align_log.setReadOnly(True)
        self.align_log.setMinimumHeight(120)
        log_layout.addWidget(log_title, 0, 0)
        log_layout.addWidget(self.align_log, 1, 0)
        log_panel.setFixedHeight(max(135, log_panel.sizeHint().height() - alignment_title_height))
        shell.addWidget(log_panel)
        shell.addStretch(1)

        self._refresh_align_target_ui()
        self._refresh_alignment_preview_controls()
        scroll.setWidget(page)
        return scroll

    def _load_settings_into_ui(self) -> None:
        self._loading_settings_into_ui = True
        self.set_ffmpeg_dir(Path(self.settings.ffmpeg_dir) if self.settings.ffmpeg_dir.strip() else Path())
        self.set_output_name_mode(self.settings.output_name_mode)
        self.set_output_name_templates(self.settings.on_name_template, self.settings.off_name_template)
        self.align_video_name_template_value = self.settings.align_video_name_template or DEFAULT_ALIGNED_VIDEO_NAME_TEMPLATE
        self.align_audio_name_template_value = self.settings.align_audio_name_template or DEFAULT_ALIGNED_AUDIO_NAME_TEMPLATE
        self.align_use_video_audio_check.setChecked(bool(self.settings.align_export_use_video_audio))
        self._restore_lyrics_preferences()
        self._loading_settings_into_ui = False

    def _restore_lyrics_preferences(self) -> None:
        saved_source_ids = tuple(str(item) for item in (self.settings.lyrics_source_ids or DEFAULT_LYRICS_PROVIDER_IDS) if str(item))
        if not saved_source_ids:
            saved_source_ids = DEFAULT_LYRICS_PROVIDER_IDS
        for index in range(self.lyrics_source_combo.count()):
            item_data = self.lyrics_source_combo.itemData(index)
            if isinstance(item_data, tuple) and item_data == saved_source_ids:
                self.lyrics_source_combo.setCurrentIndex(index)
                break

        saved_preview_mode = str(self.settings.lyrics_preview_mode or LYRICS_PREVIEW_LINE)
        preview_index = self.lyrics_preview_mode_combo.findData(saved_preview_mode)
        if preview_index >= 0:
            self.lyrics_preview_mode_combo.setCurrentIndex(preview_index)
        self.lyrics_strip_intro_checkbox.setChecked(bool(self.settings.lyrics_strip_intro_lines))

    def _install_single_click_combo_behavior(self, combo: QComboBox) -> None:
        popup_view = combo.view()
        popup_view.pressed.connect(lambda index, combo=combo: self._handle_combo_popup_pressed(combo, index.row()))

    def _handle_combo_popup_pressed(self, combo: QComboBox, row: int) -> None:
        if row < 0 or row >= combo.count():
            return
        combo.setCurrentIndex(row)
        combo.hidePopup()

    def _persist_lyrics_preferences(self, *_args) -> None:
        if self._loading_settings_into_ui:
            return
        source_ids = self.lyrics_source_combo.currentData()
        if not isinstance(source_ids, tuple):
            source_ids = DEFAULT_LYRICS_PROVIDER_IDS
        preview_mode = self.lyrics_preview_mode_combo.currentData()
        if not isinstance(preview_mode, str):
            preview_mode = LYRICS_PREVIEW_LINE
        self.settings.lyrics_source_ids = tuple(source_ids)
        self.settings.lyrics_preview_mode = preview_mode
        self.settings.lyrics_strip_intro_lines = self.lyrics_strip_intro_checkbox.isChecked()
        save_app_settings(self.settings)

    def _persist_alignment_preferences(self, *_args) -> None:
        if self._loading_settings_into_ui:
            return
        self.settings.align_export_use_video_audio = self.align_use_video_audio_check.isChecked()
        save_app_settings(self.settings)

    def _sync_ffmpeg_labels(self) -> None:
        self.hires_ffmpeg_label.setText(self.ffmpeg_dir_text or FFMPEG_DIR_PLACEHOLDER)
        self._refresh_media_info_labels()

    def set_video_path(self, path: Path) -> None:
        self.video_zone.set_path(path)
        self.output_dir_label.setText(str(resolve_output_dir(path)))

    def set_on_vocal_path(self, path: Path) -> None:
        self.on_vocal_zone.set_path(path)

    def set_off_vocal_path(self, path: Path) -> None:
        self.off_vocal_zone.set_path(path)

    def set_align_video_path(self, path: Path) -> None:
        self.align_video_zone.set_path(path)
        self.align_video_info_label.setText(self._build_media_info(path, "字幕视频"))
        self._invalidate_alignment_waveforms()

    def set_align_audio_path(self, path: Path) -> None:
        self.align_audio_zone.set_path(path)
        self.align_audio_info_label.setText(self._build_media_info(path, "原唱音源"))
        self._invalidate_alignment_waveforms()

    def set_ffmpeg_dir(self, path: Path) -> None:
        self.ffmpeg_dir_text = str(path) if str(path).strip() else ""
        self._sync_ffmpeg_labels()

    def set_output_name_mode(self, mode: str) -> None:
        if mode == OUTPUT_NAME_MODE_VIDEO_NAME:
            mode = OUTPUT_NAME_MODE_TEMPLATE
            self.set_output_name_templates(DEFAULT_ON_NAME_TEMPLATE, DEFAULT_OFF_NAME_TEMPLATE)
        if mode not in {OUTPUT_NAME_MODE_FIXED, OUTPUT_NAME_MODE_TEMPLATE}:
            raise ProcessingError(f"不支持的输出命名模式: {mode}")
        self.output_name_mode_value = mode

    def set_output_name_templates(self, on_template: str, off_template: str) -> None:
        self.on_name_template_value = on_template
        self.off_name_template_value = off_template

    def _choose_video(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择字幕视频", "", "视频文件 (*.mkv *.mp4 *.mov *.avi);;所有文件 (*.*)")
        if path:
            self.set_video_path(Path(path))

    def _choose_on_audio(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择原唱音频",
            "",
            "音频文件 (*.flac *.wav *.mp3 *.m4a *.aac *.ape *.alac *.mkv *.mp4);;所有文件 (*.*)",
        )
        if path:
            self.set_on_vocal_path(Path(path))

    def _choose_off_audio(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择伴奏音频",
            "",
            "音频文件 (*.flac *.wav *.mp3 *.m4a *.aac *.ape *.alac *.mkv *.mp4);;所有文件 (*.*)",
        )
        if path:
            self.set_off_vocal_path(Path(path))

    def _choose_align_video(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择用于对齐的字幕视频", "", "视频文件 (*.mkv *.mp4 *.mov *.avi);;所有文件 (*.*)")
        if path:
            self.set_align_video_path(Path(path))

    def _choose_align_audio(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择需要对齐的原唱音源", "", "音频或 MP4 文件 (*.flac *.wav *.mp3 *.m4a *.aac *.ape *.alac *.mkv *.mp4);;所有文件 (*.*)")
        if path:
            self.set_align_audio_path(Path(path))

    def _refresh_media_info_labels(self) -> None:
        self.align_video_info_label.setText(self._build_media_info(self.align_video_zone.path, "字幕视频"))
        self.align_audio_info_label.setText(self._build_media_info(self.align_audio_zone.path, "原唱音源"))

    def _build_media_info(self, path: Path | None, label: str) -> str:
        if path is None:
            return f"{label}: 时长未知"
        cache_key = path.expanduser()
        cached_duration = self._media_duration_cache.get(cache_key)
        if cached_duration is not None:
            return f"{label}: {cached_duration}"
        try:
            ffprobe_path = find_tool("ffprobe.exe", self._resolve_ffmpeg_dir())
            info = probe_media(ffprobe_path, path)
        except Exception:  # noqa: BLE001
            return f"{label}: 时长未知"
        duration_text = format_media_duration(info.duration)
        self._media_duration_cache[cache_key] = duration_text
        return f"{label}: {duration_text}"

    def _open_settings_window(self, context: str) -> None:
        dialog = QDialog(self)
        title = "波形对齐设置" if context == "align" else "Hi-Res 生成设置"
        dialog.setWindowTitle(f"{APP_TITLE} - {title}")
        dialog.resize(860, 540)

        outer = QVBoxLayout(dialog)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        scroll = QScrollArea(dialog)
        scroll.setWidgetResizable(True)
        content = QWidget()
        scroll.setWidget(content)
        outer.addWidget(scroll)

        shell = QVBoxLayout(content)
        shell.setContentsMargins(20, 20, 20, 20)
        shell.setSpacing(18)

        heading = QLabel(title)
        heading.setStyleSheet('font-family: "Microsoft YaHei UI"; font-size: 18pt; font-weight: 700;')
        shell.addWidget(heading)

        ffmpeg_panel = QFrame()
        ffmpeg_panel.setObjectName("WhitePanel")
        ffmpeg_layout = QGridLayout(ffmpeg_panel)
        ffmpeg_layout.setContentsMargins(14, 14, 14, 14)
        ffmpeg_title = QLabel("FFmpeg 目录")
        ffmpeg_title.setObjectName("PanelTitle")
        ffmpeg_display = QLineEdit(self.ffmpeg_dir_text)
        ffmpeg_display.setPlaceholderText(FFMPEG_DIR_PLACEHOLDER)
        choose_button = QPushButton("选择目录")
        choose_button.clicked.connect(
            lambda: self._choose_ffmpeg_for_dialog(dialog, ffmpeg_display)
        )
        system_button = QPushButton("使用系统 PATH")
        system_button.clicked.connect(lambda: ffmpeg_display.setText(""))
        ffmpeg_hint_1 = QLabel("推荐直接选择 ffmpeg 的 bin 目录，例如 D:\\tools\\ffmpeg\\bin。")
        ffmpeg_hint_1.setStyleSheet('font-family: "Microsoft YaHei UI"; font-size: 9pt; color: #6b7280;')
        ffmpeg_hint_2 = QLabel("也可以选择 ffmpeg 根目录，程序会尝试其中的 bin\\ffmpeg.exe 和 bin\\ffprobe.exe。")
        ffmpeg_hint_2.setStyleSheet('font-family: "Microsoft YaHei UI"; font-size: 9pt; color: #6b7280;')
        ffmpeg_layout.addWidget(ffmpeg_title, 0, 0)
        ffmpeg_layout.addWidget(ffmpeg_display, 0, 1)
        ffmpeg_layout.addWidget(choose_button, 0, 2)
        ffmpeg_layout.addWidget(system_button, 0, 3)
        ffmpeg_layout.addWidget(ffmpeg_hint_1, 1, 1, 1, 3)
        ffmpeg_layout.addWidget(ffmpeg_hint_2, 2, 1, 1, 3)
        ffmpeg_layout.setColumnStretch(1, 1)
        shell.addWidget(ffmpeg_panel)

        status_label = QLabel("")
        status_label.setStyleSheet('font-family: "Microsoft YaHei UI"; font-size: 9pt; color: #177245;')

        if context == "align":
            naming_panel = QFrame()
            naming_panel.setObjectName("WhitePanel")
            naming_layout = QGridLayout(naming_panel)
            naming_layout.setContentsMargins(14, 14, 14, 14)
            naming_title = QLabel("对齐导出命名")
            naming_title.setObjectName("PanelTitle")
            video_template_edit = QLineEdit(self.align_video_name_template_value)
            audio_template_edit = QLineEdit(self.align_audio_name_template_value)
            naming_help_1 = QLabel("默认: 对齐后视频 {video_name}_aligned.mp4；对齐后音频 {audio_name}_aligned.wav。")
            naming_help_1.setStyleSheet('font-family: "Microsoft YaHei UI"; font-size: 9pt; color: #6b7280;')
            naming_help_2 = QLabel("视频模板支持 {video_name}；音频模板支持 {audio_name} 和 {video_name}。不用写扩展名。")
            naming_help_2.setStyleSheet('font-family: "Microsoft YaHei UI"; font-size: 9pt; color: #6b7280;')
            naming_layout.addWidget(naming_title, 0, 0)
            naming_layout.addWidget(QLabel("对齐后视频模板"), 1, 0)
            naming_layout.addWidget(video_template_edit, 1, 1)
            naming_layout.addWidget(QLabel("对齐后音频模板"), 2, 0)
            naming_layout.addWidget(audio_template_edit, 2, 1)
            naming_layout.addWidget(naming_help_1, 3, 1)
            naming_layout.addWidget(naming_help_2, 4, 1)
            naming_layout.setColumnStretch(1, 1)
            shell.addWidget(naming_panel)
        else:
            naming_panel = QFrame()
            naming_panel.setObjectName("WhitePanel")
            naming_layout = QGridLayout(naming_panel)
            naming_layout.setContentsMargins(14, 14, 14, 14)
            naming_title = QLabel("输出命名")
            naming_title.setObjectName("PanelTitle")
            mode_group = QButtonGroup(dialog)
            fixed_radio = QRadioButton("默认命名: on_vocal.mkv / off_vocal.mkv")
            template_radio = QRadioButton("自定义模板: 使用你自己的命名样式")
            mode_group.addButton(fixed_radio)
            mode_group.addButton(template_radio)
            if self.output_name_mode_value == OUTPUT_NAME_MODE_TEMPLATE:
                template_radio.setChecked(True)
            else:
                fixed_radio.setChecked(True)
            on_template_edit = QLineEdit(self.on_name_template_value)
            off_template_edit = QLineEdit(self.off_name_template_value)

            def sync_template_enabled() -> None:
                enabled = template_radio.isChecked()
                on_template_edit.setEnabled(enabled)
                off_template_edit.setEnabled(enabled)

            fixed_radio.toggled.connect(lambda _checked: sync_template_enabled())
            template_radio.toggled.connect(lambda _checked: sync_template_enabled())
            sync_template_enabled()

            naming_help_1 = QLabel("支持占位符 {video_name}。不用写 .mkv。示例: {video_name}_karaoke_on")
            naming_help_1.setStyleSheet('font-family: "Microsoft YaHei UI"; font-size: 9pt; color: #6b7280;')
            naming_help_2 = QLabel("默认: 原唱 on_vocal.mkv；伴奏 off_vocal.mkv。")
            naming_help_2.setStyleSheet('font-family: "Microsoft YaHei UI"; font-size: 9pt; color: #6b7280;')
            naming_layout.addWidget(naming_title, 0, 0)
            naming_layout.addWidget(fixed_radio, 1, 1)
            naming_layout.addWidget(template_radio, 2, 1)
            naming_layout.addWidget(QLabel("原唱模板"), 3, 0)
            naming_layout.addWidget(on_template_edit, 3, 1)
            naming_layout.addWidget(QLabel("伴奏模板"), 4, 0)
            naming_layout.addWidget(off_template_edit, 4, 1)
            naming_layout.addWidget(naming_help_1, 5, 1)
            naming_layout.addWidget(naming_help_2, 6, 1)
            naming_layout.setColumnStretch(1, 1)
            shell.addWidget(naming_panel)

        shell.addWidget(status_label)

        controls = QHBoxLayout()
        controls.addStretch(1)
        save_button = QPushButton("保存设置")
        close_button = QPushButton("关闭")
        close_button.clicked.connect(dialog.close)

        def save_settings_from_dialog() -> None:
            try:
                mode = self.output_name_mode_value
                on_template = self.on_name_template_value
                off_template = self.off_name_template_value
                align_video_template = self.align_video_name_template_value
                align_audio_template = self.align_audio_name_template_value
                if context == "align":
                    align_video_template = video_template_edit.text().strip() or DEFAULT_ALIGNED_VIDEO_NAME_TEMPLATE
                    align_audio_template = audio_template_edit.text().strip() or DEFAULT_ALIGNED_AUDIO_NAME_TEMPLATE
                else:
                    mode = OUTPUT_NAME_MODE_TEMPLATE if template_radio.isChecked() else OUTPUT_NAME_MODE_FIXED
                    on_template = on_template_edit.text().strip() or DEFAULT_ON_NAME_TEMPLATE
                    off_template = off_template_edit.text().strip() or DEFAULT_OFF_NAME_TEMPLATE

                saved_path = self._save_settings_payload(
                    output_name_mode=mode,
                    on_template=on_template,
                    off_template=off_template,
                    align_video_template=align_video_template,
                    align_audio_template=align_audio_template,
                    ffmpeg_dir_text=ffmpeg_display.text().strip(),
                )
            except ProcessingError as exc:
                QMessageBox.critical(dialog, APP_TITLE, str(exc))
                return

            status_label.setText("设置已保存到本地。")
            QMessageBox.information(dialog, APP_TITLE, f"设置已保存：\n{saved_path}")

        save_button.clicked.connect(save_settings_from_dialog)
        controls.addWidget(save_button)
        controls.addWidget(close_button)
        shell.addLayout(controls)
        dialog.exec()

    def _choose_ffmpeg_for_dialog(self, parent: QWidget, target: QLineEdit) -> None:
        path = QFileDialog.getExistingDirectory(parent, "选择 ffmpeg 所在目录")
        if path:
            target.setText(path)

    def _save_settings_payload(
        self,
        *,
        output_name_mode: str,
        on_template: str,
        off_template: str,
        align_video_template: str,
        align_audio_template: str,
        ffmpeg_dir_text: str,
    ) -> Path:
        ffmpeg_dir = Path(ffmpeg_dir_text).expanduser() if ffmpeg_dir_text.strip() else None
        if ffmpeg_dir is not None and not ffmpeg_dir.is_dir():
            raise ProcessingError("所选 ffmpeg 目录无效，请重新选择。")

        if output_name_mode not in {OUTPUT_NAME_MODE_FIXED, OUTPUT_NAME_MODE_TEMPLATE}:
            raise ProcessingError("输出命名模式无效，请重新选择。")
        if output_name_mode == OUTPUT_NAME_MODE_TEMPLATE:
            on_template = validate_output_name_template(on_template, "原唱")
            off_template = validate_output_name_template(off_template, "伴奏")

        align_video_template = self._validate_alignment_name_template(
            align_video_template,
            "对齐后视频",
            allowed_fields={"video_name"},
            extensions=(".mp4", ".mkv"),
        )
        align_audio_template = self._validate_alignment_name_template(
            align_audio_template,
            "对齐后音频",
            allowed_fields={"audio_name", "video_name"},
            extensions=(".wav",),
        )

        self.output_name_mode_value = output_name_mode
        self.on_name_template_value = on_template
        self.off_name_template_value = off_template
        self.align_video_name_template_value = align_video_template
        self.align_audio_name_template_value = align_audio_template
        self.ffmpeg_dir_text = str(ffmpeg_dir) if ffmpeg_dir else ""
        self._sync_ffmpeg_labels()
        self.settings.output_name_mode = self.output_name_mode_value
        self.settings.on_name_template = self.on_name_template_value
        self.settings.off_name_template = self.off_name_template_value
        self.settings.align_video_name_template = self.align_video_name_template_value
        self.settings.align_audio_name_template = self.align_audio_name_template_value
        self.settings.ffmpeg_dir = self.ffmpeg_dir_text
        self.settings.align_export_use_video_audio = self.align_use_video_audio_check.isChecked()
        return save_app_settings(self.settings)

    def _resolve_output_name_mode(self) -> str:
        if self.output_name_mode_value not in {OUTPUT_NAME_MODE_FIXED, OUTPUT_NAME_MODE_TEMPLATE}:
            raise ProcessingError("输出命名模式无效，请重新选择。")
        return self.output_name_mode_value

    def _resolve_output_name_templates(self, *, require_valid: bool) -> tuple[str, str]:
        on_template = self.on_name_template_value or DEFAULT_ON_NAME_TEMPLATE
        off_template = self.off_name_template_value or DEFAULT_OFF_NAME_TEMPLATE
        if require_valid:
            on_template = validate_output_name_template(on_template, "原唱")
            off_template = validate_output_name_template(off_template, "伴奏")
        return on_template, off_template

    def _resolve_alignment_name_templates(self, *, require_valid: bool) -> tuple[str, str]:
        video_template = self.align_video_name_template_value or DEFAULT_ALIGNED_VIDEO_NAME_TEMPLATE
        audio_template = self.align_audio_name_template_value or DEFAULT_ALIGNED_AUDIO_NAME_TEMPLATE
        if require_valid:
            video_template = self._validate_alignment_name_template(
                video_template,
                "对齐后视频",
                allowed_fields={"video_name"},
                extensions=(".mp4", ".mkv"),
            )
            audio_template = self._validate_alignment_name_template(
                audio_template,
                "对齐后音频",
                allowed_fields={"audio_name", "video_name"},
                extensions=(".wav",),
            )
        return video_template, audio_template

    def _validate_alignment_name_template(
        self,
        template: str,
        label: str,
        *,
        allowed_fields: set[str],
        extensions: tuple[str, ...],
    ) -> str:
        normalized = template.strip()
        for extension in extensions:
            if normalized.lower().endswith(extension):
                normalized = normalized[: -len(extension)].rstrip()
                break
        if not normalized:
            raise ProcessingError(f"{label}模板不能为空。")
        if "/" in normalized or "\\" in normalized:
            raise ProcessingError(f"{label}模板不能包含路径分隔符。")
        for _, field_name, _, _ in ALIGNMENT_TEMPLATE_FORMATTER.parse(normalized):
            if field_name and field_name not in allowed_fields:
                supported = "、".join(f"{{{name}}}" for name in sorted(allowed_fields))
                raise ProcessingError(f"{label}模板包含不支持的占位符 {field_name}。当前支持 {supported}。")
        return normalized

    def _resolve_ffmpeg_dir(self) -> Path | None:
        if not self.ffmpeg_dir_text.strip():
            return None
        path = Path(self.ffmpeg_dir_text).expanduser()
        if not path.is_dir():
            raise ProcessingError("所选 ffmpeg 目录无效，请重新选择。")
        return path

    def _validate_hires_inputs(
        self,
    ) -> tuple[Path, Path | None, Path | None, Path, Path | None, str, str | None, str | None]:
        video_path = self.video_zone.path
        on_vocal_path = self.on_vocal_zone.path
        off_vocal_path = self.off_vocal_zone.path
        ffmpeg_dir = self._resolve_ffmpeg_dir()
        output_name_mode = self._resolve_output_name_mode()

        missing: list[str] = []
        if video_path is None or not video_path.is_file():
            missing.append("字幕视频")
        if on_vocal_path is not None and not on_vocal_path.is_file():
            missing.append("原唱音频")
        if off_vocal_path is not None and not off_vocal_path.is_file():
            missing.append("伴奏音频")
        if missing:
            raise ProcessingError(f"请先选择有效的文件: {', '.join(missing)}")
        assert video_path is not None

        if on_vocal_path is None and off_vocal_path is None:
            raise ProcessingError("请至少选择原唱音频或伴奏音频中的一个。")
        if (
            on_vocal_path is not None
            and off_vocal_path is not None
            and on_vocal_path.resolve() == off_vocal_path.resolve()
        ):
            raise ProcessingError("原唱音频和伴奏音频不能是同一个文件。")

        output_dir = resolve_output_dir(video_path)
        if output_name_mode == OUTPUT_NAME_MODE_TEMPLATE:
            on_template, off_template = self._resolve_output_name_templates(require_valid=True)
        else:
            on_template, off_template = None, None

        return (
            video_path,
            on_vocal_path,
            off_vocal_path,
            output_dir,
            ffmpeg_dir,
            output_name_mode,
            on_template,
            off_template,
        )

    def _start_hires(self) -> None:
        if self.hires_task is not None and self.hires_task.isRunning():
            QMessageBox.information(self, APP_TITLE, "当前任务还在处理中，请稍等。")
            return

        try:
            args = self._validate_hires_inputs()
        except ProcessingError as exc:
            QMessageBox.critical(self, APP_TITLE, str(exc))
            return

        self.hires_log.clear()
        self.hires_start_button.setEnabled(False)
        self.hires_progress.setRange(0, 0)
        self.hires_status_label.setText("处理中…")

        def runner(logger: Callable[[str], None]) -> list[Path]:
            (
                video_path,
                on_vocal_path,
                off_vocal_path,
                output_dir,
                ffmpeg_dir,
                output_name_mode,
                on_template,
                off_template,
            ) = args
            return run_pipeline(
                video_path=video_path,
                on_vocal_path=on_vocal_path,
                off_vocal_path=off_vocal_path,
                output_dir=output_dir,
                ffmpeg_dir=ffmpeg_dir,
                output_name_mode=output_name_mode,
                on_name_template=on_template,
                off_name_template=off_template,
                logger=logger,
            )

        self.hires_task = BackgroundTask(runner)
        self.hires_task.log_message.connect(self._append_hires_log)
        self.hires_task.task_succeeded.connect(self._finish_hires_success)
        self.hires_task.task_failed.connect(self._finish_hires_failure)
        self.hires_task.start()

    def _append_hires_log(self, message: str) -> None:
        timestamp = time.strftime("%H:%M:%S")
        self.hires_log.appendPlainText(f"[{timestamp}] {message}")

    def _append_align_log(self, message: str) -> None:
        timestamp = time.strftime("%H:%M:%S")
        self.align_log.appendPlainText(f"[{timestamp}] {message}")

    def _finish_hires_success(self, outputs: object) -> None:
        self.hires_progress.setRange(0, 1)
        self.hires_progress.setValue(1)
        self.hires_start_button.setEnabled(True)
        self.hires_status_label.setText("完成")
        lines = "\n".join(str(path) for path in outputs) if isinstance(outputs, list) else str(outputs)
        QMessageBox.information(self, APP_TITLE, f"输出完成:\n{lines}")

    def _finish_hires_failure(self, message: str) -> None:
        self.hires_progress.setRange(0, 1)
        self.hires_progress.setValue(0)
        self.hires_start_button.setEnabled(True)
        self.hires_status_label.setText("失败")
        self._append_hires_log(f"处理失败: {message}")
        QMessageBox.critical(self, APP_TITLE, message)

    def _clear_hires_inputs(self) -> None:
        if self.hires_task is not None and self.hires_task.isRunning():
            QMessageBox.information(self, APP_TITLE, "当前生成任务还在处理中，请稍等。")
            return
        self.video_zone.clear_path()
        self.on_vocal_zone.clear_path()
        self.off_vocal_zone.clear_path()
        self.output_dir_label.setText("跟随字幕视频所在目录")
        self.hires_status_label.setText("已清空已选文件")

    def _open_hires_output_dir(self) -> None:
        video_path = self.video_zone.path
        if video_path is None:
            QMessageBox.information(self, APP_TITLE, "请先选择字幕视频。")
            return
        output_dir = resolve_output_dir(video_path)
        output_dir.mkdir(parents=True, exist_ok=True)
        open_in_explorer(output_dir)

    def _validate_alignment_inputs(self) -> tuple[Path, Path, Path | None]:
        video_path = self.align_video_zone.path
        audio_path = self.align_audio_zone.path
        ffmpeg_dir = self._resolve_ffmpeg_dir()
        if video_path is None or not video_path.is_file():
            raise ProcessingError("请先选择有效的字幕视频。")
        if audio_path is None or not audio_path.is_file():
            raise ProcessingError("请先选择有效的原唱音源。")
        return video_path, audio_path, ffmpeg_dir

    def _has_complete_alignment_inputs(self) -> bool:
        video_path = self.align_video_zone.path
        audio_path = self.align_audio_zone.path
        return (
            video_path is not None
            and audio_path is not None
            and video_path.is_file()
            and audio_path.is_file()
        )

    def _invalidate_alignment_waveforms(self) -> None:
        self._stop_alignment_preview(log_message=False)
        self.waveform_view.clear()
        self.align_status_label.setText("准备生成波形")
        self._refresh_align_target_ui()
        self._refresh_alignment_preview_controls()

    def _start_alignment_analysis(self) -> None:
        if self.align_analysis_task is not None and self.align_analysis_task.isRunning():
            QMessageBox.information(self, APP_TITLE, "当前波形任务还在处理中，请稍等。")
            return
        try:
            video_path, audio_path, ffmpeg_dir = self._validate_alignment_inputs()
        except ProcessingError as exc:
            QMessageBox.critical(self, APP_TITLE, str(exc))
            return

        self.align_log.clear()
        self.align_analyze_button.setEnabled(False)
        self.align_export_button.setEnabled(False)
        self.align_auto_button.setEnabled(False)
        self.align_preview_button.setEnabled(False)
        self.align_progress.setRange(0, 0)
        self.align_status_label.setText("生成波形中…")

        def runner(logger: Callable[[str], None]) -> tuple[WaveformData, WaveformData]:
            video_waveform = extract_waveform(video_path, ffmpeg_dir, logger, label="字幕视频音轨")
            audio_waveform = extract_waveform(audio_path, ffmpeg_dir, logger, label="原唱音源")
            return video_waveform, audio_waveform

        self.align_analysis_task = BackgroundTask(runner)
        self.align_analysis_task.log_message.connect(self._append_align_log)
        self.align_analysis_task.task_succeeded.connect(self._finish_alignment_analysis_success)
        self.align_analysis_task.task_failed.connect(self._finish_alignment_analysis_failure)
        self.align_analysis_task.start()

    def _finish_alignment_analysis_success(self, payload: object) -> None:
        self.align_progress.setRange(0, 1)
        self.align_progress.setValue(1)
        self.align_analyze_button.setEnabled(True)
        if not isinstance(payload, tuple) or len(payload) != 2:
            self._finish_alignment_analysis_failure("波形结果无效。")
            return
        video_waveform, audio_waveform = payload
        self.waveform_view.set_waveforms(video_waveform=video_waveform, audio_waveform=audio_waveform)
        self.align_video_info_label.setText(f"字幕视频: {format_media_duration(video_waveform.duration)}")
        self.align_audio_info_label.setText(f"原唱音源: {format_media_duration(audio_waveform.duration)}")
        self.align_status_label.setText("波形已生成")
        self._refresh_align_target_ui()
        self._refresh_alignment_preview_controls()

    def _finish_alignment_analysis_failure(self, message: str) -> None:
        self.align_progress.setRange(0, 1)
        self.align_progress.setValue(0)
        self.align_analyze_button.setEnabled(True)
        self.align_status_label.setText("波形生成失败")
        self._append_align_log(f"波形生成失败: {message}")
        self._refresh_alignment_preview_controls()
        QMessageBox.critical(self, APP_TITLE, message)

    def _auto_align_waveforms(self) -> None:
        if self.align_auto_task is not None and self.align_auto_task.isRunning():
            QMessageBox.information(self, APP_TITLE, "当前自动对齐任务还在处理中，请稍等。")
            return
        if self.waveform_view.video_waveform is None or self.waveform_view.audio_waveform is None:
            QMessageBox.critical(self, APP_TITLE, "请先生成波形。")
            return

        video_start_seconds, audio_start_seconds = self.waveform_view.source_starts()
        target_track = ALIGN_TARGET_VIDEO if self._is_align_video_target() else ALIGN_TARGET_AUDIO
        self._stop_alignment_preview(log_message=False)
        self.align_analyze_button.setEnabled(False)
        self.align_auto_button.setEnabled(False)
        self.align_export_button.setEnabled(False)
        self.align_preview_button.setEnabled(False)
        self.align_progress.setRange(0, 0)
        self.align_status_label.setText("自动对齐中…")
        self._append_align_log(
            f"自动对齐分析从当前视图左边界开始: 视频 {video_start_seconds:.3f}s，音频 {audio_start_seconds:.3f}s"
        )

        def runner(_logger: Callable[[str], None]) -> AutoAlignResult:
            return estimate_waveform_alignment(
                self.waveform_view.video_waveform,
                self.waveform_view.audio_waveform,
                target_track=target_track,
                video_start_seconds=video_start_seconds,
                audio_start_seconds=audio_start_seconds,
            )

        self.align_auto_task = BackgroundTask(runner)
        self.align_auto_task.task_succeeded.connect(self._finish_auto_align_success)
        self.align_auto_task.task_failed.connect(self._finish_auto_align_failure)
        self.align_auto_task.start()

    def _finish_auto_align_success(self, payload: object) -> None:
        self.align_progress.setRange(0, 1)
        self.align_progress.setValue(1)
        self.align_analyze_button.setEnabled(True)
        if not isinstance(payload, AutoAlignResult):
            self._finish_auto_align_failure("自动对齐结果无效。")
            return
        self.waveform_view.set_offset(payload.target_offset_seconds)
        self.waveform_view.set_playhead(max(0.0, payload.media_offset_seconds), keep_visible=True)
        confidence_percent = int(round(payload.confidence * 100))
        self.align_status_label.setText(f"自动对齐完成，置信度 {confidence_percent}%")
        target_label = "字幕视频" if self._is_align_video_target() else "原唱音源"
        self._append_align_log(
            f"自动对齐完成: 移动{target_label} {format_offset(payload.target_offset_seconds)}，"
            f"媒体相对偏移 {format_offset(payload.media_offset_seconds)}，置信度 {confidence_percent}%"
        )
        self._append_align_log(
            f"自动对齐评分: score={payload.score:.3f}, second={payload.second_score:.3f}, "
            f"overlap={payload.overlap_seconds:.2f}s, search=±{payload.search_seconds:.0f}s"
        )
        if payload.confidence < 0.55:
            self._append_align_log("自动对齐置信度偏低，建议先试听预览再确认。")
        self._refresh_alignment_preview_controls()

    def _finish_auto_align_failure(self, message: str) -> None:
        self.align_progress.setRange(0, 1)
        self.align_progress.setValue(0)
        self.align_analyze_button.setEnabled(True)
        self.align_status_label.setText("自动对齐失败")
        self._refresh_alignment_preview_controls()
        QMessageBox.critical(self, APP_TITLE, message)

    def _handle_align_target_changed(self) -> None:
        target_track = ALIGN_TARGET_VIDEO if self.align_target_video_radio.isChecked() else ALIGN_TARGET_AUDIO
        self._stop_alignment_preview(log_message=False)
        self.waveform_view.set_target_track(target_track)
        self._refresh_align_target_ui()

    def _refresh_align_target_ui(self) -> None:
        is_video_target = self._is_align_video_target()
        has_waveforms = self.waveform_view.video_waveform is not None and self.waveform_view.audio_waveform is not None
        self._handle_waveform_offset_changed(self.waveform_view.offset_seconds)
        self.align_drag_offset_radio.setText("移动字幕视频" if is_video_target else "移动原唱音源")
        self.align_export_button.setText("导出对齐视频" if is_video_target else "导出对齐音频")
        if not is_video_target:
            self.align_extra_wav_check.setChecked(False)
        self.align_extra_wav_check.setEnabled(has_waveforms and is_video_target)
        self.align_trim_mark_button.setEnabled(has_waveforms and is_video_target)
        self.align_trim_clear_button.setEnabled(has_waveforms and is_video_target)
        self.align_force_1080p60_check.setEnabled(has_waveforms and is_video_target)
        self.align_auto_trim_check.setEnabled(has_waveforms and is_video_target)
        self.align_use_video_audio_check.setEnabled(has_waveforms and is_video_target)
        if has_waveforms and is_video_target:
            self.align_lead_row_widget.setEnabled(True)
            self.align_encode_row_widget.setEnabled(True)
            if self._align_lead_fill_selection == LEAD_FILL_WHITE:
                self.align_lead_fill_white_radio.setChecked(True)
            elif self._align_lead_fill_selection == LEAD_FILL_FREEZE:
                self.align_lead_fill_freeze_radio.setChecked(True)
            else:
                self.align_lead_fill_black_radio.setChecked(True)
            if self._align_encode_selection == ENCODE_MODE_HARDWARE:
                self.align_encode_hardware_radio.setChecked(True)
            else:
                self.align_encode_software_radio.setChecked(True)
        else:
            if self.align_lead_fill_white_radio.isChecked():
                self._align_lead_fill_selection = LEAD_FILL_WHITE
            elif self.align_lead_fill_freeze_radio.isChecked():
                self._align_lead_fill_selection = LEAD_FILL_FREEZE
            else:
                self._align_lead_fill_selection = LEAD_FILL_BLACK
            if self.align_encode_hardware_radio.isChecked():
                self._align_encode_selection = ENCODE_MODE_HARDWARE
            else:
                self._align_encode_selection = ENCODE_MODE_SOFTWARE

            self.align_lead_fill_group.setExclusive(False)
            self.align_lead_fill_black_radio.setChecked(False)
            self.align_lead_fill_white_radio.setChecked(False)
            self.align_lead_fill_freeze_radio.setChecked(False)
            self.align_lead_fill_group.setExclusive(True)

            self.align_encode_group.setExclusive(False)
            self.align_encode_software_radio.setChecked(False)
            self.align_encode_hardware_radio.setChecked(False)
            self.align_encode_group.setExclusive(True)

            self.align_lead_row_widget.setEnabled(False)
            self.align_encode_row_widget.setEnabled(False)
        if self.align_control_panel is not None:
            self.align_control_panel.setEnabled(has_waveforms)
        self.waveform_view.setEnabled(has_waveforms)
        self._refresh_align_trim_status(self.waveform_view.trim_end_seconds)

    def _handle_waveform_offset_changed(self, seconds: float) -> None:
        label = "字幕视频偏移" if self._is_align_video_target() else "原唱音源偏移"
        self.align_offset_label.setText(f"{label} {format_offset(seconds)}")
        self._refresh_align_trim_status(self.waveform_view.trim_end_seconds)

    def _handle_playhead_changed(self, seconds: float) -> None:
        self.align_playhead_label.setText(f"播放位置 {seconds:.3f}s")
        if (
            not self._suppress_preview_seek_restart
            and self.align_preview_process is not None
            and self.align_preview_process.is_running()
        ):
            self._restart_alignment_preview_from_playhead()

    def _restart_alignment_preview_from_playhead(self) -> None:
        self._start_alignment_preview()

    def _refresh_align_trim_status(self, trim_seconds: object) -> None:
        if not self._is_align_video_target():
            self.align_trim_label.setText("仅在导出字幕视频时生效")
            return

        manual_trim = trim_seconds if isinstance(trim_seconds, float) else self.waveform_view.trim_end_seconds
        parts: list[str] = []
        if manual_trim is not None:
            parts.append(f"手动尾裁到 {manual_trim:.3f}s")
        if self.align_auto_trim_check.isChecked():
            auto_trim = self._compute_video_trim_duration()
            if auto_trim is not None and self.waveform_view.audio_waveform is not None:
                parts.append(f"自动最多保留到音频末尾 {self.waveform_view.audio_waveform.duration:.3f}s")
            else:
                parts.append("自动尾裁已开启")
        self.align_trim_label.setText("；".join(parts) if parts else "未设置")

    def _compute_video_trim_duration(self) -> float | None:
        if not self._is_align_video_target():
            return None
        if self.waveform_view.video_waveform is None:
            return None
        base_duration = max(0.0, self.waveform_view.video_waveform.duration + self.waveform_view.offset_seconds)
        if base_duration <= 0:
            return None
        candidates = [base_duration]
        if self.waveform_view.trim_end_seconds is not None:
            candidates.append(self.waveform_view.trim_end_seconds)
        if self.align_auto_trim_check.isChecked() and self.waveform_view.audio_waveform is not None:
            candidates.append(self.waveform_view.audio_waveform.duration)
        trim_duration = min(candidates)
        if trim_duration < base_duration - 0.001:
            return max(0.001, trim_duration)
        return None

    def _refresh_alignment_preview_controls(self) -> None:
        has_inputs = self._has_complete_alignment_inputs()
        has_waveforms = self.waveform_view.video_waveform is not None and self.waveform_view.audio_waveform is not None
        is_playing = self.align_preview_process is not None and self.align_preview_process.is_running()
        is_exporting = self._is_align_export_running()
        is_busy = (
            (self.align_analysis_task is not None and self.align_analysis_task.isRunning())
            or (self.align_auto_task is not None and self.align_auto_task.isRunning())
            or is_exporting
        )
        self.align_analyze_button.setEnabled(has_inputs and not is_playing and not is_busy)
        self.align_auto_button.setEnabled(has_waveforms and not is_playing and not is_busy)
        self.align_preview_button.setEnabled(has_waveforms and not is_playing and not is_busy)
        self.align_stop_preview_button.setEnabled(is_playing)
        self.align_export_button.setEnabled(has_waveforms and not is_playing and not is_busy)
        self.align_stop_export_button.setEnabled(is_exporting)
        if self.align_open_output_button is not None:
            self.align_open_output_button.setEnabled(has_waveforms)
        if self.align_clear_button is not None:
            self.align_clear_button.setEnabled(has_waveforms)
        if self.align_jump_to_end_button is not None:
            self.align_jump_to_end_button.setEnabled(has_waveforms)
        if self.align_reset_view_button is not None:
            self.align_reset_view_button.setEnabled(has_waveforms)
        self.align_zoom_slider.setEnabled(has_waveforms)

    def _is_align_export_running(self) -> bool:
        return self.align_export_task is not None and self.align_export_task.isRunning()

    def _register_align_export_process(self, process: subprocess.Popen | None) -> None:
        self._align_export_process = process

    def _cleanup_incomplete_align_exports(self) -> None:
        completed = set(self._align_export_completed_outputs)
        for path in self._align_export_expected_outputs:
            if path in completed or not path.exists():
                continue
            try:
                path.unlink()
                self._append_align_log(f"已清理未完成的输出文件: {path}")
            except OSError as exc:
                self._append_align_log(f"清理未完成的输出文件失败: {path} ({exc})")

    def _reset_align_export_state(self) -> None:
        self._align_export_cancel_requested = False
        self._align_export_process = None
        self._align_export_expected_outputs = []
        self._align_export_completed_outputs = []

    def _stop_alignment_export(self) -> None:
        if not self._is_align_export_running():
            return
        if not self._align_export_cancel_requested:
            self._align_export_cancel_requested = True
            self.align_status_label.setText("正在停止导出…")
            self._append_align_log("正在停止导出…")
        process = self._align_export_process
        if process is not None:
            terminate_process(process)

    def _is_align_video_target(self) -> bool:
        return self.align_target_video_radio.isChecked()

    def _start_alignment_preview(self) -> None:
        if self.waveform_view.video_waveform is None or self.waveform_view.audio_waveform is None:
            QMessageBox.critical(self, APP_TITLE, "请先生成波形并完成对齐。")
            return
        try:
            video_path, audio_path, ffmpeg_dir = self._validate_alignment_inputs()
        except ProcessingError as exc:
            QMessageBox.critical(self, APP_TITLE, str(exc))
            return

        self._stop_alignment_preview(log_message=False)
        target_track = ALIGN_TARGET_VIDEO if self._is_align_video_target() else ALIGN_TARGET_AUDIO
        preview_start_seconds = self.waveform_view.playhead_seconds
        try:
            self.align_preview_process = start_alignment_preview(
                video_path=video_path,
                audio_path=audio_path,
                offset_seconds=self.waveform_view.offset_seconds,
                ffmpeg_dir=ffmpeg_dir,
                logger=self._append_align_log,
                target_track=target_track,
                preview_start_seconds=preview_start_seconds,
            )
        except Exception as exc:  # noqa: BLE001
            self.align_preview_process = None
            self._append_align_log(f"播放预览失败: {exc}")
            QMessageBox.critical(self, APP_TITLE, f"播放预览失败:\n{exc}")
            self._refresh_alignment_preview_controls()
            return

        self.align_preview_started_at = time.monotonic()
        self.align_preview_start_seconds = preview_start_seconds
        self.align_status_label.setText("正在播放预览")
        self.preview_timer.start()
        self._refresh_alignment_preview_controls()

    def _stop_alignment_preview(self, *, log_message: bool = True) -> None:
        process = self.align_preview_process
        if process is not None:
            process.stop()
            self.align_preview_process = None
            if log_message:
                self._append_align_log("播放预览已停止")
        self.preview_timer.stop()
        self.align_preview_started_at = 0.0
        self.align_preview_start_seconds = 0.0
        self._refresh_alignment_preview_controls()

    def _poll_alignment_preview(self) -> None:
        process = self.align_preview_process
        if process is None:
            self.preview_timer.stop()
            self._refresh_alignment_preview_controls()
            return
        if process.is_running():
            elapsed = time.monotonic() - self.align_preview_started_at
            self._suppress_preview_seek_restart = True
            try:
                self.waveform_view.set_playhead(self.align_preview_start_seconds + elapsed, keep_visible=True)
            finally:
                self._suppress_preview_seek_restart = False
            return
        self.preview_timer.stop()
        self.align_preview_process = None
        self.align_preview_started_at = 0.0
        self.align_preview_start_seconds = 0.0
        self.align_status_label.setText("预览播放结束")
        self._append_align_log("播放预览结束")
        self._refresh_alignment_preview_controls()

    def _render_alignment_output_path(
        self,
        *,
        video_path: Path,
        audio_path: Path,
        is_video_target: bool,
    ) -> Path:
        video_template, audio_template = self._resolve_alignment_name_templates(require_valid=True)
        template = video_template if is_video_target else audio_template
        extension = ".mp4" if is_video_target else ".wav"
        try:
            stem = template.format(video_name=video_path.stem, audio_name=audio_path.stem).strip()
        except Exception as exc:  # noqa: BLE001
            label = "对齐后视频" if is_video_target else "对齐后音频"
            raise ProcessingError(f"{label}模板无法生成文件名: {exc}") from exc

        stem = stem.rstrip(". ")
        if not stem:
            raise ProcessingError("导出文件名不能为空。")
        invalid_chars = sorted({char for char in stem if char in WINDOWS_INVALID_FILENAME_CHARS})
        if invalid_chars:
            raise ProcessingError(f"文件名包含非法字符: {' '.join(invalid_chars)}")
        source_path = video_path if is_video_target else audio_path
        return source_path.with_name(f"{stem}{extension}")

    def _start_aligned_export(self) -> None:
        if self.align_export_task is not None and self.align_export_task.isRunning():
            QMessageBox.information(self, APP_TITLE, "当前导出任务还在处理中，请稍等。")
            return
        if self.waveform_view.video_waveform is None or self.waveform_view.audio_waveform is None:
            QMessageBox.critical(self, APP_TITLE, "请先生成波形并完成对齐。")
            return
        try:
            video_path, audio_path, ffmpeg_dir = self._validate_alignment_inputs()
            is_video_target = self._is_align_video_target()
            initial_path = self._render_alignment_output_path(
                video_path=video_path,
                audio_path=audio_path,
                is_video_target=is_video_target,
            )
            output_kind = "对齐视频" if is_video_target else "对齐音频"
            if is_video_target:
                output_path_text, _ = QFileDialog.getSaveFileName(
                    self,
                    "导出对齐视频",
                    str(initial_path),
                    "MP4 视频 (*.mp4);;Matroska 视频 (*.mkv);;所有文件 (*.*)",
                )
            else:
                output_path_text, _ = QFileDialog.getSaveFileName(
                    self,
                    "导出对齐音频",
                    str(initial_path),
                    "WAV 音频 (*.wav);;所有文件 (*.*)",
                )
        except ProcessingError as exc:
            QMessageBox.critical(self, APP_TITLE, str(exc))
            return

        if not output_path_text:
            return
        output_path = Path(output_path_text).expanduser()
        offset_seconds = self.waveform_view.offset_seconds
        encode_mode = ENCODE_MODE_HARDWARE if self.align_encode_hardware_radio.isChecked() else ENCODE_MODE_SOFTWARE
        if self.align_lead_fill_white_radio.isChecked():
            lead_fill_color = LEAD_FILL_WHITE
        elif self.align_lead_fill_freeze_radio.isChecked():
            lead_fill_color = LEAD_FILL_FREEZE
        else:
            lead_fill_color = LEAD_FILL_BLACK
        force_1080p60 = self.align_force_1080p60_check.isChecked()
        use_source_video_audio = self.align_use_video_audio_check.isChecked() if is_video_target else False
        video_trim_duration = self._compute_video_trim_duration() if is_video_target else None
        extra_wav_output: Path | None = None
        if self.align_extra_wav_check.isChecked():
            extra_candidate = self._render_alignment_output_path(
                video_path=video_path,
                audio_path=audio_path,
                is_video_target=False,
            )
            if not (not is_video_target and extra_candidate == output_path):
                extra_wav_output = extra_candidate
        self._align_export_cancel_requested = False
        self._align_export_process = None
        self._align_export_expected_outputs = [output_path]
        if extra_wav_output is not None:
            self._align_export_expected_outputs.append(extra_wav_output)
        self._align_export_completed_outputs = []

        self._stop_alignment_preview(log_message=False)
        self.align_analyze_button.setEnabled(False)
        self.align_auto_button.setEnabled(False)
        self.align_export_button.setEnabled(False)
        self.align_preview_button.setEnabled(False)
        self.align_progress.setRange(0, 0)
        self.align_status_label.setText("导出中…")

        def runner(logger: Callable[[str], None]) -> list[Path]:
            outputs: list[Path] = []
            if is_video_target:
                outputs.append(
                    export_aligned_video(
                        video_path=video_path,
                        audio_path=audio_path,
                        output_path=output_path,
                        offset_seconds=offset_seconds,
                        ffmpeg_dir=ffmpeg_dir,
                        logger=logger,
                        should_cancel=lambda: self._align_export_cancel_requested,
                        on_process_started=self._register_align_export_process,
                        encode_mode=encode_mode,
                        lead_fill_color=lead_fill_color,
                        force_1080p60=force_1080p60,
                        output_duration_seconds=video_trim_duration,
                        use_source_video_audio=use_source_video_audio,
                    )
                )
                self._align_export_completed_outputs.append(outputs[-1])
            else:
                outputs.append(
                    export_aligned_audio(
                        audio_path=audio_path,
                        output_path=output_path,
                        offset_seconds=offset_seconds,
                        ffmpeg_dir=ffmpeg_dir,
                        logger=logger,
                        should_cancel=lambda: self._align_export_cancel_requested,
                        on_process_started=self._register_align_export_process,
                    )
                )
                self._align_export_completed_outputs.append(outputs[-1])
            if extra_wav_output is not None:
                outputs.append(
                    export_aligned_audio(
                        audio_path=audio_path,
                        output_path=extra_wav_output,
                        offset_seconds=0.0 if is_video_target else offset_seconds,
                        ffmpeg_dir=ffmpeg_dir,
                        logger=logger,
                        should_cancel=lambda: self._align_export_cancel_requested,
                        on_process_started=self._register_align_export_process,
                    )
                )
                self._align_export_completed_outputs.append(outputs[-1])
            return outputs

        self.align_export_task = BackgroundTask(runner)
        self.align_export_task.log_message.connect(self._append_align_log)
        self.align_export_task.task_succeeded.connect(lambda outputs: self._finish_aligned_export(True, "", outputs, output_kind))
        self.align_export_task.task_failed.connect(lambda message: self._finish_aligned_export(False, message, None, output_kind))
        self.align_export_task.start()
        self._refresh_alignment_preview_controls()

    def _finish_aligned_export(
        self,
        success: bool,
        message: str,
        output_paths: object,
        output_kind: str,
    ) -> None:
        was_cancelled = self._align_export_cancel_requested
        self._align_export_process = None
        self.align_progress.setRange(0, 1)
        self.align_progress.setValue(1 if success and not was_cancelled else 0)
        self.align_analyze_button.setEnabled(True)
        if was_cancelled:
            self._cleanup_incomplete_align_exports()
            self.align_status_label.setText("导出已停止")
            self._refresh_alignment_preview_controls()
            self._reset_align_export_state()
            self._append_align_log("导出任务已终止，未完成的输出文件已清理。")
            return
        self.align_status_label.setText("导出完成" if success else "导出失败")
        self._refresh_alignment_preview_controls()
        self._reset_align_export_state()
        if success and isinstance(output_paths, list):
            QMessageBox.information(self, APP_TITLE, f"{output_kind}已导出:\n" + "\n".join(str(path) for path in output_paths))
            return
        self._append_align_log(f"导出失败: {message}")
        QMessageBox.critical(self, APP_TITLE, message)

    def _clear_alignment_inputs(self) -> None:
        if (
            (self.align_analysis_task is not None and self.align_analysis_task.isRunning())
            or (self.align_auto_task is not None and self.align_auto_task.isRunning())
            or (self.align_export_task is not None and self.align_export_task.isRunning())
        ):
            QMessageBox.information(self, APP_TITLE, "当前对齐任务还在处理中，请稍等。")
            return
        self._stop_alignment_preview(log_message=False)
        self.align_video_zone.clear_path()
        self.align_audio_zone.clear_path()
        self.align_log.clear()
        self.waveform_view.clear()
        self._refresh_media_info_labels()
        self._refresh_align_target_ui()
        self._refresh_alignment_preview_controls()
        self.align_status_label.setText("已清空已选文件")

    def _open_align_output_dir(self) -> None:
        source_path = self.align_audio_zone.path or self.align_video_zone.path
        if source_path is None:
            QMessageBox.information(self, APP_TITLE, "请先选择文件。")
            return
        output_dir = source_path.parent
        output_dir.mkdir(parents=True, exist_ok=True)
        open_in_explorer(output_dir)

    def closeEvent(self, event) -> None:  # noqa: N802
        self._stop_alignment_preview(log_message=False)
        super().closeEvent(event)


def launch_qt_app() -> int:
    set_explicit_app_user_model_id("KaraokeHelper.Desktop")
    app = QApplication.instance() or QApplication([])
    app_icon = load_taskbar_icon()
    if app_icon is not None:
        app.setWindowIcon(app_icon)
    window = KrokHelperQtApp()
    window.show()
    return app.exec()
