"""音频合成页（第 2 步「音视频处理」的第三个页签）。

多条音频 → 后台分析（波形 + FFmpeg ``silencedetect`` 首尾静音检测）→ 手动
微调（顺序 / 裁剪点 / 淡化参数，全部可视化可编辑）→ 一键「处理静音段并合成」
或「按当前设置合成」，接缝处短淡化直拼防爆音。

页面结构与音频分离工作区同一套设计语言：FluentScrollArea + 状态行 +
ResponsiveGrid 双列素材/输出卡 + 全宽波形裁剪卡与合成设置卡 + 底部
「提示左、主按钮右」操作行 + ``#LogText`` 日志区。重活全部丢
:class:`BackgroundTask`，与外壳的往来只经 :class:`MergeHost`。所有会碰控件的
读取（``resolve_ffmpeg_dir``、参数 SpinBox）都在 GUI 线程先取好快照再进
runner —— runner 里只碰纯 Python 状态。
"""

from __future__ import annotations

import subprocess

from datetime import datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

from krok_helper.app_paths import temp_dir

from PyQt6.QtCore import Qt, QUrl, pyqtSignal
from PyQt6.QtWidgets import (
    QFileDialog,
    QFrame,
    QScrollBar,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CheckBox,
    ComboBox,
    FluentIcon as FIF,
    IconWidget,
    IndeterminateProgressBar,
    LineEdit,
    MessageBox,
    PrimaryPushButton,
    PushButton,
    StrongBodyLabel,
    ToolButton,
)
from qfluentwidgets.components.widgets import ScrollArea as FluentScrollArea

# 「音量柱」同款 slider+输入框复合控件：复用字幕导出模块的成熟实现，
# 拖动只刷显示、松手才提交的契约由 CanvasSliderSpinBox 内部保证。
from krok_helper.subtitle_render.frontend.properties.controls.inputs import (
    CanvasSliderSpinBox,
    WheelFocusedSpinBox,
)

from krok_helper.audio_processing.merge.analysis import (
    DEFAULT_MARGIN_SECONDS,
    DEFAULT_MIN_SILENCE_SECONDS,
    DEFAULT_SILENCE_THRESHOLD_DB,
    analyze_clip,
)
from krok_helper.audio_processing.merge.commands import (
    DEFAULT_JOINT_FADE_SECONDS,
    SPLICE_BUTT,
    SPLICE_CROSSFADE,
    run_merge,
    unique_output_path,
)
from krok_helper.audio_processing.merge.model import (
    MergeClip,
    TRIM_SOURCE_FULL,
    TRIM_SOURCE_MANUAL,
)
from krok_helper.audio_processing.merge.timeline_view import ArrangementView
from krok_helper.audio_processing.responsive import ResponsiveGrid
from krok_helper.audio_processing.separation.audio_io import (
    ACCEPTED_AUDIO_EXTENSIONS,
    ACCEPTED_INPUT_EXTENSIONS,
    ACCEPTED_VIDEO_EXTENSIONS,
)
from krok_helper.audio_processing.separation.widgets import OutputSettingsCard
from krok_helper.background import BackgroundTask
from krok_helper.errors import ExportCancelled, ProcessingError
from krok_helper.ffmpeg import terminate_process
from krok_helper.notifications import play_completion_sound
from krok_helper.qfluent_compat import show_fluent_error, show_fluent_info
from krok_helper.ui_kit import CardWidget, ElidedLabel
from krok_helper.windows import open_in_explorer

__all__ = ["MergeHost", "MergePage"]


@runtime_checkable
class MergeHost(Protocol):
    """音频合成页需要外壳提供的全部能力。"""

    settings: object

    def track_background_task(self, task: BackgroundTask) -> BackgroundTask: ...

    def resolve_ffmpeg_dir(self) -> Path | None: ...


#: 分区色：沿用工作台既有色系（波形对齐页轨道蓝/红、分离页状态徽标橙/绿），
#: 与品牌色一致、不随明暗主题变化（对齐页轨道配色同款做法）。
COLOR_SPLICE = "#2F6BFF"  # 拼接与淡化（蓝，音频轨同款）
COLOR_DETECT = "#c07f1a"  # 静音检测（橙，警告态同款）
#: 剪辑行序号徽章的循环色。
_CLIP_BADGE_COLORS = ("#2F6BFF", "#F04452", "#2e9e5b", "#c07f1a")


def _tint(color: str, alpha: float) -> str:
    from PyQt6.QtGui import QColor

    c = QColor(color)
    return f"rgba({c.red()}, {c.green()}, {c.blue()}, {alpha:.2f})"


def _palette():
    from krok_helper.theme_workbench import palette

    return palette()


class _SliderParamRow(QWidget):
    """分区色标签 + 字幕导出模块「音量柱」同款 slider+输入框复合控件。

    直接复用 :class:`CanvasSliderSpinBox`（内部是隐藏箭头的
    ``WheelFocusedSpinBox`` 输入框 + qfluentwidgets 滑条）：输入框持有真实
    值并保留完整取值范围，滑条同一数值可视可拖，拖动只刷显示、松手才提交。
    对外值统一换算成浮点（秒 / dB），``scale`` 是整数刻度到对外单位的乘数
    （如毫秒→秒为 0.001）。
    """

    valueChanged = pyqtSignal(float)

    def __init__(
        self,
        label_text: str,
        *,
        lo: int,
        hi: int,
        step: int,
        unit: str,
        scale: float,
        color: str,
        label_width: int = 96,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._scale = float(scale)

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)
        label = CaptionLabel(label_text, self)
        label.setFixedWidth(label_width)
        label.setStyleSheet(f"color: {color}; background: transparent;")
        row.addWidget(label, 0, Qt.AlignmentFlag.AlignVCenter)

        spin = WheelFocusedSpinBox(self)
        spin.setRange(lo, hi)
        spin.setSingleStep(step)
        spin.setSuffix(unit)
        self._combo = CanvasSliderSpinBox(spin, self)
        self._combo.set_slider_range(lo, hi)
        row.addWidget(self._combo, 1)
        self._combo.valueChanged.connect(self._emit_value)

    # ── 对外 API ───────────────────────────────────────────────

    def value(self) -> float:
        return self._combo.value() * self._scale

    def setValue(self, value: float, *, emit: bool = False) -> None:
        target = int(round(value / self._scale))
        blocked = self._combo.blockSignals(not emit)
        try:
            self._combo.setValue(target)
        finally:
            self._combo.blockSignals(blocked)
        if emit and self._combo.value() != target:
            self.valueChanged.emit(self.value())

    def setEnabled(self, enabled: bool) -> None:  # noqa: N802 - Qt API
        super().setEnabled(enabled)

    def _emit_value(self, _value: int) -> None:
        self.valueChanged.emit(self.value())


def format_timecode(seconds: float) -> str:
    """把秒数格式化成 ``hh:mm:ss:mmm``（毫秒段三位）。"""
    total_ms = int(round(max(0.0, seconds) * 1000))
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, ms = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}:{ms:03d}"


def parse_timecode(text: str) -> float | None:
    """把用户输入解析成秒数；无法解析返回 ``None``。

    支持冒号分段的简写（按段数推断量纲），最后一段允许小数：

    - ``83`` / ``83.45``          → 秒
    - ``1:23`` / ``1:23.45``      → 分:秒
    - ``1:02:03``                 → 时:分:秒
    - ``1:02:03:450``             → 时:分:秒:毫秒（完整格式）
    """
    cleaned = text.strip().replace("：", ":").replace("；", ":")
    if not cleaned:
        return None
    parts = cleaned.split(":")
    if len(parts) > 4:
        return None
    try:
        numbers = [float(part) for part in parts]
    except ValueError:
        return None
    if any(value < 0 for value in numbers):
        return None
    if len(numbers) == 1:
        return numbers[0]
    if len(numbers) == 2:
        return numbers[0] * 60 + numbers[1]
    if len(numbers) == 3:
        return numbers[0] * 3600 + numbers[1] * 60 + numbers[2]
    return numbers[0] * 3600 + numbers[1] * 60 + numbers[2] + numbers[3] / 1000.0


class _TimecodeEdit(LineEdit):
    """``hh:mm:ss:mmm`` 时间码输入框（纯行编辑，无步进箭头）。

    回车或失焦时解析：合法就规整回显并发出 ``secondsCommitted``，非法则
    回退到上一个有效值。程序写入用 :meth:`set_seconds`，不触发提交。
    """

    secondsCommitted = pyqtSignal(float)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setPlaceholderText("hh:mm:ss:mmm")
        self.setMinimumWidth(148)
        self._seconds = 0.0
        self.editingFinished.connect(self._on_edit_done)

    def seconds(self) -> float:
        return self._seconds

    def set_seconds(self, value: float) -> None:
        self._seconds = max(0.0, float(value))
        self.setText(format_timecode(self._seconds))

    def _on_edit_done(self) -> None:
        parsed = parse_timecode(self.text())
        if parsed is None:
            self.setText(format_timecode(self._seconds))
            return
        self._seconds = parsed
        self.setText(format_timecode(parsed))
        self.secondsCommitted.emit(parsed)


def _section_header(text: str, color: str, parent: QWidget) -> QWidget:
    """带彩色圆点的分组标题（图例式，参照对齐页轨道配色标注）。"""
    header = QWidget(parent)
    row = QHBoxLayout(header)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(6)
    dot = QLabel(header)
    dot.setFixedSize(8, 8)
    dot.setStyleSheet(
        f"background: {color}; border-radius: 4px; border: none;"
    )
    row.addWidget(dot, 0, Qt.AlignmentFlag.AlignVCenter)
    title = StrongBodyLabel(text, header)
    row.addWidget(title, 0, Qt.AlignmentFlag.AlignVCenter)
    row.addStretch(1)
    return header


def _card_header(title: str, icon: FIF, color: str, parent: QWidget) -> QHBoxLayout:
    """卡片标题行：彩色图标 + 标题（参照分离页任务卡 / 对齐页素材卡）。"""
    from PyQt6.QtGui import QColor

    header = QHBoxLayout()
    header.setContentsMargins(0, 0, 0, 0)
    header.setSpacing(8)
    icon_widget = IconWidget(icon.icon(color=QColor(color)), parent)
    icon_widget.setFixedSize(20, 20)
    header.addWidget(icon_widget, 0, Qt.AlignmentFlag.AlignVCenter)
    header.addWidget(StrongBodyLabel(title, parent))
    header.addStretch(1)
    return header


class _MergeOutputCard(OutputSettingsCard):
    """音频合成的输出卡：共享输出设置（目录/格式）+ 文件名 + 右下角合成按钮。

    追加的页脚挂在共享卡 VBox 的底部 stretch 之后，正好贴到卡片右下角。
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent, title="输出")

        from PyQt6.QtWidgets import QVBoxLayout as _QVBL  # noqa: F401

        layout = self.layout()
        name_row = QHBoxLayout()
        name_row.setContentsMargins(0, 0, 0, 0)
        name_row.setSpacing(10)
        name_row.addWidget(CaptionLabel("输出文件名", self))
        self.name_edit = LineEdit(self)
        self.name_edit.setPlaceholderText("留空则按 合成_日期时间 命名")
        name_row.addWidget(self.name_edit, 1)
        layout.addLayout(name_row)

        action_row = QHBoxLayout()
        action_row.setContentsMargins(0, 0, 0, 0)
        action_row.setSpacing(10)
        action_row.addStretch(1)
        self.merge_button = PrimaryPushButton("合成", self)
        self.merge_button.setMinimumWidth(170)
        action_row.addWidget(self.merge_button)
        layout.addLayout(action_row)


class _MergeDropZone(QFrame):
    """多文件拖放区：点击选择或一次拖入多条音频。

    视觉与音频分离页的 ``_DropZoneFrame`` 同款：居中竖排「图标 + 主文案 +
    副文案」，拖拽经过换下载图标与强调色文案；不同的是一次收多条文件。
    """

    clicked = pyqtSignal()
    filesDropped = pyqtSignal(list)
    dragActiveChanged = pyqtSignal(bool)

    _EMPTY_LABEL = "点击选择文件，或拖拽多条音频到此处"
    _FORMAT_HINT = "支持 wav / flac / mp3 / m4a / aac / ape / alac，按顺序拼接"

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("MergeDropZone")
        self.setAcceptDrops(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(110)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self._dragging = False
        self._filled = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(4)
        self._icon = IconWidget(FIF.MUSIC.icon(), self)
        self._icon.setFixedSize(26, 26)
        layout.addWidget(self._icon, 0, Qt.AlignmentFlag.AlignHCenter)
        self._label = BodyLabel(self._EMPTY_LABEL, self)
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._label)
        self._hint = CaptionLabel(self._FORMAT_HINT, self)
        self._hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._hint)
        # 图标与文字不吃鼠标事件，避免划过时拖放区悬停态闪烁。
        for child in (self._icon, self._label, self._hint):
            child.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        from krok_helper.theme_workbench import theme as _wb_theme

        _wb_theme.changed.connect(self._on_theme_changed)
        self._apply_style()
        self._refresh_zone()

    def _on_theme_changed(self) -> None:
        from krok_helper.theme_workbench import schedule_theme_refresh

        schedule_theme_refresh(self, self._apply_theme_refresh)

    def _apply_theme_refresh(self) -> None:
        try:
            self._refresh_zone()
        except RuntimeError:  # C++ 侧已销毁
            pass

    def set_filled(self, filled: bool) -> None:
        if filled != self._filled:
            self._filled = filled
            self._apply_style()

    def _refresh_zone(self, *, dragging: bool = False) -> None:
        from PyQt6.QtGui import QColor

        accent = _palette().accent_primary
        emphasis = f"color: {accent}; font-weight: 600; background: transparent;"
        plain = "background: transparent;"
        self._hint.setStyleSheet(plain)
        if dragging:
            self._icon.setIcon(FIF.DOWNLOAD.icon(color=QColor(accent)))
            self._label.setText("松开即可载入")
            self._label.setStyleSheet(emphasis)
        elif self._filled:
            self._icon.setIcon(FIF.COMPLETED.icon(color=QColor(accent)))
            self._label.setText("继续拖入可追加素材")
            self._label.setStyleSheet(emphasis)
        else:
            self._icon.setIcon(FIF.MUSIC.icon())
            self._label.setText(self._EMPTY_LABEL)
            self._label.setStyleSheet(plain)

    def _apply_style(self) -> None:
        p = _palette()
        if self._dragging:
            border, width, style = p.accent_primary, 2, "dashed"
            background = _tint(p.accent_primary, 0.16)
        elif self._filled:
            border, width, style = p.accent_primary, 1, "solid"
            background = _tint(p.accent_primary, 0.07)
        else:
            border, width, style = p.input_border_hover, 1, "dashed"
            background = "transparent"
        self.setStyleSheet(
            f"#MergeDropZone {{ border: {width}px {style} {border}; "
            f"border-radius: 8px; background: {background}; }}"
        )

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)

    def _set_dragging(self, dragging: bool) -> None:
        if dragging == self._dragging:
            return
        self._dragging = dragging
        self._apply_style()
        self._refresh_zone(dragging=dragging)
        self.dragActiveChanged.emit(dragging)

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if self._matching_files(event.mimeData()):
            event.acceptProposedAction()
            self._set_dragging(True)
        else:
            event.ignore()

    def dragLeaveEvent(self, event) -> None:  # noqa: N802
        self._set_dragging(False)
        super().dragLeaveEvent(event)

    def dropEvent(self, event) -> None:  # noqa: N802
        self._set_dragging(False)
        files = self._matching_files(event.mimeData())
        if files:
            event.acceptProposedAction()
            self.filesDropped.emit(files)

    @staticmethod
    def _matching_files(mime) -> list[str]:
        matched: list[str] = []
        for url in mime.urls():
            local = url.toLocalFile()
            if local and Path(local).suffix.lower() in ACCEPTED_INPUT_EXTENSIONS:
                matched.append(local)
        return matched


class _ClipRow(QFrame):
    """剪辑列表的一行：序号 + 文件名 + 状态 + 上移/下移/试听/移除。"""

    selected = pyqtSignal()
    moveUpRequested = pyqtSignal()
    moveDownRequested = pyqtSignal()
    removeRequested = pyqtSignal()
    playRequested = pyqtSignal()

    _STATUS_COLORS = {
        "未分析": "#94a3b8",
        "分析失败": "#d64545",
        "未处理": "#94a3b8",
        "已去静音": "#2e9e5b",
        "手动裁剪": "#c07f1a",
    }

    def __init__(self, clip: MergeClip, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.clip = clip
        self.setObjectName("MergeClipRow")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._selected = False
        self._playing = False

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(8)

        self._index_badge = QLabel("1", self)
        self._index_badge.setFixedSize(22, 22)
        self._index_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._index_badge, 0, Qt.AlignmentFlag.AlignVCenter)

        text_col = QVBoxLayout()
        text_col.setSpacing(1)
        self._name_label = ElidedLabel(clip.path.name, self)
        text_col.addWidget(self._name_label)
        self._info_label = CaptionLabel("", self)
        text_col.addWidget(self._info_label)
        layout.addLayout(text_col, 1)

        self._status_label = CaptionLabel("未分析", self)
        layout.addWidget(self._status_label, 0, Qt.AlignmentFlag.AlignVCenter)

        self._play_button = ToolButton(FIF.PLAY, self)
        self._play_button.setToolTip("试听")
        self._play_button.clicked.connect(self.playRequested.emit)
        layout.addWidget(self._play_button)
        self._up_button = ToolButton(FIF.CARE_UP_SOLID, self)
        self._up_button.setToolTip("上移")
        self._up_button.clicked.connect(self.moveUpRequested.emit)
        layout.addWidget(self._up_button)
        self._down_button = ToolButton(FIF.CARE_DOWN_SOLID, self)
        self._down_button.setToolTip("下移")
        self._down_button.clicked.connect(self.moveDownRequested.emit)
        layout.addWidget(self._down_button)
        self._remove_button = ToolButton(FIF.DELETE, self)
        self._remove_button.setToolTip("移除")
        self._remove_button.clicked.connect(self.removeRequested.emit)
        layout.addWidget(self._remove_button)

        self.refresh()

    def set_index(self, index: int, total: int) -> None:
        self._index_badge.setText(str(index + 1))
        color = _CLIP_BADGE_COLORS[index % len(_CLIP_BADGE_COLORS)]
        self._index_badge.setStyleSheet(
            f"background: {color}; color: #FFFFFF; border-radius: 11px; "
            f"font-weight: 600; border: none;"
        )
        self._up_button.setEnabled(index > 0)
        self._down_button.setEnabled(index < total - 1)

    def set_selected(self, selected: bool) -> None:
        if selected == self._selected:
            return
        self._selected = selected
        self._apply_style()

    def set_playing(self, playing: bool) -> None:
        if playing == self._playing:
            return
        self._playing = playing
        self._play_button.setIcon(FIF.PAUSE.icon() if playing else FIF.PLAY.icon())

    def refresh(self) -> None:
        status = self.clip.status_text()
        self._status_label.setText(status)
        color = self._STATUS_COLORS.get(status, "#94a3b8")
        self._status_label.setStyleSheet(f"color: {color};")
        if self.clip.error:
            self._info_label.setText(self.clip.error)
            self._info_label.setToolTip(self.clip.error)
        elif self.clip.duration > 0:
            trim = ""
            if self.clip.trim_source != "full":
                trim = (
                    f"，裁剪 {self.clip.trim_start:.3f}s ~ "
                    f"{self.clip.effective_end():.3f}s（保留 {self.clip.trimmed_duration():.3f}s）"
                )
            self._info_label.setText(f"时长 {self.clip.duration:.3f}s{trim}")
            self._info_label.setToolTip("")
        else:
            self._info_label.setText("等待分析…")
            self._info_label.setToolTip("")
        self._apply_style()

    def _apply_style(self) -> None:
        p = _palette()
        if self._selected:
            self.setStyleSheet(
                f"#MergeClipRow {{ background: {_tint(p.accent_primary, 0.08)}; "
                f"border: 1px solid {p.accent_primary}; border-radius: 8px; }}"
            )
        else:
            self.setStyleSheet(
                f"#MergeClipRow {{ background: {p.input_bg}; "
                f"border: 1px solid {p.card_border}; border-radius: 8px; }}"
            )

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.selected.emit()
        super().mousePressEvent(event)


#: 整体预览临时子目录（%TEMP%\LinKLyrics\merge-preview，统一临时根见
#: :func:`krok_helper.app_paths.temp_root`）；目录内任何时刻最多保留一个在用的预览 wav。
_PREVIEW_DIR_NAME = "merge-preview"


def _preview_dir() -> Path:
    return temp_dir(_PREVIEW_DIR_NAME)


def purge_stale_preview_files() -> None:
    """清扫预览临时目录里的陈旧 wav（崩溃/强退残留、被播放器占用后遗留的旧名）。

    删除失败（如文件仍被播放进程占用）时静默跳过，下次页面构造或预览渲染
    时再试，保证目录内不会累积无用文件。
    """
    try:
        candidates = list(_preview_dir().glob("preview*.wav"))
    except OSError:
        return
    for path in candidates:
        try:
            path.unlink()
        except OSError:
            pass


class MergePage(QWidget):
    """音频合成页。"""

    def __init__(
        self,
        host: MergeHost,
        settings,
        save_settings,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._host = host
        self._settings = settings
        self._save_settings = save_settings
        self._clips: list[MergeClip] = []
        self._clip_rows: list[_ClipRow] = []
        self._selected_index: int | None = None
        self._syncing_ui = False

        self.merge_analysis_task: BackgroundTask | None = None
        self.merge_export_task: BackgroundTask | None = None
        self._cancel_requested = False
        self._merge_process: subprocess.Popen | None = None
        self._merge_after_analysis = False
        self._last_output: Path | None = None
        self._preview_path: Path | None = None
        self._player = None
        self._audio_output = None
        self._playing_clip: MergeClip | None = None
        #: 播放通道："" 空闲 / "clip" 素材列表单曲试听 / "preview" 整体预览。
        #: 只有 preview 才驱动时间轴播放头，单曲试听不串轴。
        self._playback_mode = ""
        #: 播放中参数变化 → 重渲染后从当前播放头续播的意图标记。
        self._resume_after_render = False
        #: 起播严格定位：媒体加载完成后仍待应用的 seek（毫秒）。
        self._pending_seek_ms = 0
        #: 合成预览是否需要重渲染（顺序/裁剪/淡化参数任一变化即置脏）。
        self._preview_dirty = True
        # 上次会话崩溃/强退可能留下整体预览临时文件，构造时顺手清掉。
        purge_stale_preview_files()

        self._build_ui()
        self._restore_settings()
        from krok_helper.theme_workbench import theme as _wb_theme

        _wb_theme.changed.connect(self._on_theme_changed)

    def _on_theme_changed(self) -> None:
        from krok_helper.theme_workbench import schedule_theme_refresh

        schedule_theme_refresh(self, self._apply_theme_refresh)

    def _apply_theme_refresh(self) -> None:
        """明暗主题切换后重刷自绘样式（行样式、徽章、拖放区边框）。"""
        try:
            self._rebuild_clip_rows()
            self._refresh_selected_clip()
        except RuntimeError:  # C++ 侧已销毁
            pass

    # ── UI 组装 ────────────────────────────────────────────────

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        scroll = FluentScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.enableTransparentBackground()
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        content = QWidget(scroll)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(2, 2, 10, 8)
        layout.setSpacing(12)

        # 与音频分离工作区同款：素材卡 + 输出卡双列自适应（右下角即合成入口）。
        self._output_card = _MergeOutputCard(content)
        self._materials_grid = ResponsiveGrid(
            min_column_width=360, max_columns=2, parent=content
        )
        self._materials_grid.set_widgets([self._build_input_card(), self._output_card])
        self._merge_button = self._output_card.merge_button
        self._merge_button.clicked.connect(self._on_merge_clicked)
        layout.addWidget(self._materials_grid)

        layout.addWidget(self._build_timeline_card())
        layout.addWidget(self._build_settings_card())
        layout.addWidget(self._build_action_card())
        layout.addStretch(1)

        scroll.setWidget(content)
        root.addWidget(scroll)

    def _build_input_card(self) -> QWidget:
        card = CardWidget()
        layout = card.createVBoxLayout()

        header = _card_header("音频素材", FIF.MUSIC, COLOR_SPLICE, card)
        add_button = ToolButton(FIF.ADD, card)
        add_button.setToolTip("添加文件")
        add_button.clicked.connect(self._browse_files)
        header.addWidget(add_button)
        layout.addLayout(header)

        self._drop_zone = _MergeDropZone(card)
        self._drop_zone.clicked.connect(self._browse_files)
        self._drop_zone.filesDropped.connect(self._on_files_dropped)
        layout.addWidget(self._drop_zone)

        self._clip_rows_layout = QVBoxLayout()
        self._clip_rows_layout.setSpacing(6)
        layout.addLayout(self._clip_rows_layout)
        self._empty_hint = CaptionLabel("拖入多条音频后自动分析静音并生成波形。", card)
        self._clip_rows_layout.addWidget(self._empty_hint)
        return card

    def _build_timeline_card(self) -> QWidget:
        card = CardWidget()
        layout = card.createVBoxLayout()
        layout.addLayout(_card_header("合成时间轴", FIF.VOLUME, "#F04452", card))
        layout.addWidget(
            CaptionLabel(
                "拖动条目整体换位、拖动条目边缘微调裁剪；点刻度尺移动播放头后按「播放」从该处试听。"
                "播放中修改切割/拼接会自动重渲染并从当前位置续播。",
                card,
            )
        )

        self._timeline = ArrangementView(card)
        self._timeline.set_bar_colors(_CLIP_BADGE_COLORS)
        self._timeline.clipSelected.connect(self._on_timeline_clip_selected)
        self._timeline.clipsReordered.connect(self._on_timeline_reordered)
        self._timeline.clipTrimChanged.connect(self._on_timeline_trim)
        self._timeline.playheadChanged.connect(self._on_timeline_playhead)

        layout.addWidget(self._timeline, 1)
        # 横向滚动条：内容放大超宽时由时间轴自身驱动范围（内建偏移平移，
        # 不嵌套 QScrollArea——嵌套滚动区在宿主布局里实测拿不到宽度）。
        self._timeline_hscroll = QScrollBar(Qt.Orientation.Horizontal, card)
        self._timeline.attach_scrollbar(self._timeline_hscroll)
        layout.addWidget(self._timeline_hscroll)

        transport_row = QHBoxLayout()
        transport_row.setSpacing(10)
        zoom_out_button = ToolButton(FIF.ZOOM_OUT, card)
        zoom_out_button.setToolTip("缩小时间轴（滚轮亦可）")
        zoom_out_button.clicked.connect(self._timeline.zoom_out)
        transport_row.addWidget(zoom_out_button)
        zoom_in_button = ToolButton(FIF.ZOOM_IN, card)
        zoom_in_button.setToolTip("放大时间轴（滚轮亦可）")
        zoom_in_button.clicked.connect(self._timeline.zoom_in)
        transport_row.addWidget(zoom_in_button)
        fit_button = PushButton(FIF.PIN if hasattr(FIF, "PIN") else FIF.ZOOM, "适应宽度", card)
        fit_button.setToolTip("缩放回适应窗口宽度")
        fit_button.clicked.connect(self._timeline.fit_width)
        transport_row.addWidget(fit_button)
        self._play_button = PushButton(FIF.PLAY, "播放", card)
        self._play_button.setMinimumWidth(110)
        self._play_button.setEnabled(False)
        self._play_button.clicked.connect(self._on_play_clicked)
        transport_row.addWidget(self._play_button)
        self._stop_playback_button = PushButton(FIF.PAUSE, "停止", card)
        self._stop_playback_button.setMinimumWidth(96)
        self._stop_playback_button.setEnabled(False)
        self._stop_playback_button.clicked.connect(self._stop_playback)
        transport_row.addWidget(self._stop_playback_button)
        self._playhead_label = CaptionLabel("00:00:00:000 / 00:00:00:000", card)
        transport_row.addWidget(self._playhead_label, 0, Qt.AlignmentFlag.AlignVCenter)
        transport_row.addStretch(1)
        layout.addLayout(transport_row)

        trim_row = QHBoxLayout()
        trim_row.setSpacing(10)
        trim_row.addWidget(CaptionLabel("选中条目", card))
        self._selected_name_label = CaptionLabel("—", card)
        self._selected_name_label.setStyleSheet("font-weight: 600; background: transparent;")
        trim_row.addWidget(self._selected_name_label)
        trim_row.addWidget(CaptionLabel("起点", card))
        self._trim_start_edit = _TimecodeEdit(card)
        self._trim_start_edit.setEnabled(False)
        self._trim_start_edit.secondsCommitted.connect(lambda _v: self._on_trim_edited())
        trim_row.addWidget(self._trim_start_edit)
        trim_row.addWidget(CaptionLabel("终点", card))
        self._trim_end_edit = _TimecodeEdit(card)
        self._trim_end_edit.setEnabled(False)
        self._trim_end_edit.secondsCommitted.connect(lambda _v: self._on_trim_edited())
        trim_row.addWidget(self._trim_end_edit)
        trim_row.addStretch(1)
        reset_button = PushButton(FIF.ROTATE, "重置为全长", card)
        reset_button.clicked.connect(self._on_reset_trim)
        trim_row.addWidget(reset_button)
        self._redetect_button = PushButton(FIF.SYNC, "重新检测本条", card)
        self._redetect_button.setEnabled(False)
        self._redetect_button.clicked.connect(self._on_redetect_selected)
        trim_row.addWidget(self._redetect_button)
        self._redetect_all_button = PushButton(FIF.UPDATE, "重新检测全部", card)
        self._redetect_all_button.clicked.connect(self._on_redetect_all_clicked)
        trim_row.addWidget(self._redetect_all_button)
        layout.addLayout(trim_row)
        return card

    def _build_settings_card(self) -> QWidget:
        card = CardWidget()
        layout = card.createVBoxLayout()
        layout.addLayout(_card_header("合成设置", FIF.MIX_VOLUMES, "#2e9e5b", card))

        # ── 拼接与淡化（蓝）──────────────────────────────────────
        layout.addWidget(_section_header("拼接与淡化", COLOR_SPLICE, card))

        mode_row = QHBoxLayout()
        mode_row.setSpacing(10)
        mode_row.addWidget(CaptionLabel("拼接方式", card))
        self._splice_combo = ComboBox(card)
        self._splice_combo.addItem("直拼（截断后拼接）")
        self._splice_combo.addItem("交叉淡化（重叠切歌）")
        self._splice_combo.setMinimumWidth(190)
        self._splice_combo.currentIndexChanged.connect(
            lambda _i: self._on_splice_param_changed()
        )
        mode_row.addWidget(self._splice_combo)
        mode_row.addStretch(1)
        layout.addLayout(mode_row)

        self._fade_row = _SliderParamRow(
            "接缝淡化时长",
            lo=0,
            hi=15000,
            step=5,
            unit=" ms",
            scale=0.001,
            color=COLOR_SPLICE,
            parent=card,
        )
        self._fade_row.valueChanged.connect(lambda _v: self._on_splice_param_changed())
        layout.addWidget(self._fade_row)
        self._gap_row = _SliderParamRow(
            "拼接间隔",
            lo=0,
            hi=5000,
            step=50,
            unit=" ms",
            scale=0.001,
            color=COLOR_SPLICE,
            parent=card,
        )
        self._gap_row.valueChanged.connect(lambda _v: self._on_splice_param_changed())
        layout.addWidget(self._gap_row)

        check_row = QHBoxLayout()
        check_row.setContentsMargins(96, 0, 0, 0)
        check_row.setSpacing(18)
        self._head_fade_check = CheckBox("首部整体淡入", card)
        self._head_fade_check.stateChanged.connect(lambda _s: self._on_splice_param_changed())
        self._tail_fade_check = CheckBox("尾部整体淡出", card)
        self._tail_fade_check.stateChanged.connect(lambda _s: self._on_splice_param_changed())
        check_row.addWidget(self._head_fade_check)
        check_row.addWidget(self._tail_fade_check)
        check_row.addStretch(1)
        layout.addLayout(check_row)
        layout.addWidget(
            CaptionLabel(
                "直拼：接缝处前后短淡入/淡出防爆音，不改变总时长；"
                "交叉淡化：相邻两条重叠接缝淡化时长（DJ 切歌式过渡，此时间隔不参与）。",
                card,
            )
        )

        # ── 静音检测（橙）────────────────────────────────────────
        layout.addWidget(_section_header("静音检测", COLOR_DETECT, card))
        self._threshold_row = _SliderParamRow(
            "静音阈值",
            lo=-60,
            hi=-20,
            step=1,
            unit=" dB",
            scale=1.0,
            color=COLOR_DETECT,
            parent=card,
        )
        self._threshold_row.valueChanged.connect(lambda _v: self._persist_settings())
        layout.addWidget(self._threshold_row)
        self._min_silence_row = _SliderParamRow(
            "最短静音时长",
            lo=50,
            hi=2000,
            step=10,
            unit=" ms",
            scale=0.001,
            color=COLOR_DETECT,
            parent=card,
        )
        self._min_silence_row.valueChanged.connect(lambda _v: self._persist_settings())
        layout.addWidget(self._min_silence_row)
        self._margin_row = _SliderParamRow(
            "保留余量",
            lo=0,
            hi=300,
            step=5,
            unit=" ms",
            scale=0.001,
            color=COLOR_DETECT,
            parent=card,
        )
        self._margin_row.valueChanged.connect(lambda _v: self._persist_settings())
        layout.addWidget(self._margin_row)
        layout.addWidget(
            CaptionLabel(
                "静音检测参数修改后，用「重新检测本条 / 全部」生效（会覆盖该条的裁剪）。",
                card,
            )
        )


        return card

    def _build_action_card(self) -> QWidget:
        card = CardWidget()
        layout = card.createVBoxLayout()

        run_row = QHBoxLayout()
        run_row.setSpacing(10)
        self._status_label = CaptionLabel("就绪", card)
        self._status_label.setWordWrap(True)
        run_row.addWidget(self._status_label, 1)
        self._open_output_button = ToolButton(FIF.FOLDER, card)
        self._open_output_button.setToolTip("打开输出目录")
        self._open_output_button.setEnabled(False)
        self._open_output_button.clicked.connect(self._on_open_output_clicked)
        run_row.addWidget(self._open_output_button)
        self._stop_button = ToolButton(FIF.CANCEL, card)
        self._stop_button.setToolTip("停止当前任务")
        self._stop_button.setEnabled(False)
        self._stop_button.clicked.connect(self._on_stop_clicked)
        run_row.addWidget(self._stop_button)
        layout.addLayout(run_row)

        self._progress = IndeterminateProgressBar(card)
        self._progress.setVisible(False)
        layout.addWidget(self._progress)

        self._log_view = QPlainTextEdit(card)
        self._log_view.setObjectName("LogText")
        self._log_view.setReadOnly(True)
        self._log_view.setPlaceholderText("运行日志")
        self._log_view.setFixedHeight(120)
        layout.addWidget(self._log_view)
        return card

    # ── 设置持久化 ─────────────────────────────────────────────

    def _settings_ns(self) -> dict:
        namespace = getattr(self._settings, "audio_merge", None)
        if not isinstance(namespace, dict):
            namespace = {}
            setattr(self._settings, "audio_merge", namespace)
        return namespace

    def _on_splice_param_changed(self, *_args) -> None:
        """拼接方式/淡化/间隔变化：落盘 + 预览置脏 + 时间轴条目间距重算。"""
        crossfade = self.splice_mode() == SPLICE_CROSSFADE
        self._gap_row.setEnabled(not crossfade)
        self._persist_settings()
        self._mark_preview_dirty()
        self._sync_timeline()

    def _persist_settings(self) -> None:
        if self._syncing_ui:
            return
        ns = self._settings_ns()
        ns["joint_fade_s"] = self._fade_row.value()
        ns["gap_s"] = self._gap_row.value()
        ns["head_fade"] = self._head_fade_check.isChecked()
        ns["tail_fade"] = self._tail_fade_check.isChecked()
        ns["threshold_db"] = self._threshold_row.value()
        ns["min_silence_s"] = self._min_silence_row.value()
        ns["margin_s"] = self._margin_row.value()
        ns["splice_mode"] = self.splice_mode()
        ns["output_dir"] = self._output_card.output_dir()
        ns["output_format"] = self._output_card.output_format()
        if self._save_settings is not None:
            self._save_settings()

    def _restore_settings(self) -> None:
        ns = self._settings_ns()
        self._syncing_ui = True
        try:
            self._splice_combo.setCurrentIndex(
                1 if ns.get("splice_mode") == SPLICE_CROSSFADE else 0
            )
            self._fade_row.setValue(float(ns.get("joint_fade_s", DEFAULT_JOINT_FADE_SECONDS)))
            self._gap_row.setValue(float(ns.get("gap_s", 0.0)))
            self._head_fade_check.setChecked(bool(ns.get("head_fade", False)))
            self._tail_fade_check.setChecked(bool(ns.get("tail_fade", False)))
            self._threshold_row.setValue(float(ns.get("threshold_db", DEFAULT_SILENCE_THRESHOLD_DB)))
            self._min_silence_row.setValue(float(ns.get("min_silence_s", DEFAULT_MIN_SILENCE_SECONDS)))
            self._margin_row.setValue(float(ns.get("margin_s", DEFAULT_MARGIN_SECONDS)))
            if ns.get("output_dir"):
                self._output_card.set_output_dir(str(ns["output_dir"]), emit=False)
            if ns.get("output_format"):
                self._output_card.set_output_format(str(ns["output_format"]))
        finally:
            self._syncing_ui = False
        self._output_card.outputDirChanged.connect(lambda _v: self._persist_settings())
        self._output_card.formatChanged.connect(lambda _v: self._persist_settings())
        self._update_action_state()

    # ── 素材管理 ───────────────────────────────────────────────

    def _browse_files(self) -> None:
        audio_patterns = " ".join(f"*{ext}" for ext in sorted(ACCEPTED_AUDIO_EXTENSIONS))
        video_patterns = " ".join(f"*{ext}" for ext in sorted(ACCEPTED_VIDEO_EXTENSIONS))
        filters = ";;".join(
            (
                f"音频或视频 ({audio_patterns} {video_patterns})",
                f"音频文件 ({audio_patterns})",
                f"视频文件 ({video_patterns})",
            )
        )
        files, _filter = QFileDialog.getOpenFileNames(self, "选择待拼接素材", "", filters)
        if files:
            self._on_files_dropped(files)

    def _on_files_dropped(self, files: list) -> None:
        if self.is_busy():
            show_fluent_error(self, "当前有任务在运行，请等它结束或先停止。")
            return
        existing = {str(clip.path.resolve()).lower() for clip in self._clips}
        added = False
        for raw in files:
            path = Path(raw)
            key = str(path.resolve()).lower()
            if key in existing or path.suffix.lower() not in ACCEPTED_INPUT_EXTENSIONS:
                continue
            existing.add(key)
            self._clips.append(MergeClip(path=path))
            added = True
        if not added:
            return
        self._mark_preview_dirty()
        self._selected_index = len(self._clips) - 1
        self._rebuild_clip_rows()
        self._refresh_selected_clip()
        self._start_analysis()

    def _remove_clip(self, clip: MergeClip) -> None:
        if self.is_busy():
            return
        if self._playing_clip is clip:
            self._stop_playback()
        try:
            index = self._clips.index(clip)
        except ValueError:
            return
        self._clips.pop(index)
        self._mark_preview_dirty()
        if not self._clips:
            self._selected_index = None
        elif self._selected_index is not None:
            self._selected_index = min(self._selected_index, len(self._clips) - 1)
        self._rebuild_clip_rows()
        self._refresh_selected_clip()
        self._update_action_state()

    def _move_clip(self, clip: MergeClip, offset: int) -> None:
        if self.is_busy():
            return
        try:
            index = self._clips.index(clip)
        except ValueError:
            return
        target = index + offset
        if target < 0 or target >= len(self._clips):
            return
        self._clips[index], self._clips[target] = self._clips[target], self._clips[index]
        self._mark_preview_dirty()
        if self._selected_index == index:
            self._selected_index = target
        elif self._selected_index == target:
            self._selected_index = index
        self._rebuild_clip_rows()
        self._refresh_selected_clip()

    def _rebuild_clip_rows(self) -> None:
        while self._clip_rows_layout.count():
            item = self._clip_rows_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._clip_rows = []
        self._drop_zone.set_filled(bool(self._clips))
        if not self._clips:
            self._empty_hint = CaptionLabel("拖入多条音频后自动分析静音并生成波形。", self)
            self._clip_rows_layout.addWidget(self._empty_hint)
            return
        for index, clip in enumerate(self._clips):
            row = _ClipRow(clip, self)
            row.set_index(index, len(self._clips))
            row.set_selected(index == self._selected_index)
            row.set_playing(self._playing_clip is clip)
            row.selected.connect(lambda c=clip: self._select_clip(c))
            row.moveUpRequested.connect(lambda c=clip: self._move_clip(c, -1))
            row.moveDownRequested.connect(lambda c=clip: self._move_clip(c, 1))
            row.removeRequested.connect(lambda c=clip: self._remove_clip(c))
            row.playRequested.connect(lambda c=clip: self._toggle_playback(c))
            self._clip_rows_layout.addWidget(row)
            self._clip_rows.append(row)
        self._sync_timeline()

    def _select_clip(self, clip: MergeClip) -> None:
        try:
            self._selected_index = self._clips.index(clip)
        except ValueError:
            return
        self._refresh_selected_clip()

    def _selected_clip(self) -> MergeClip | None:
        if self._selected_index is None or not (0 <= self._selected_index < len(self._clips)):
            return None
        return self._clips[self._selected_index]

    def _refresh_selected_clip(self) -> None:
        selected = self._selected_clip()
        for index, row in enumerate(self._clip_rows):
            row.set_selected(self._clips[index] is selected)
        self._timeline.set_selected(selected)
        self._syncing_ui = True
        try:
            if selected is not None and selected.analyzed():
                self._selected_name_label.setText(selected.path.name)
                self._trim_start_edit.set_seconds(selected.trim_start)
                self._trim_end_edit.set_seconds(selected.effective_end())
                self._trim_start_edit.setEnabled(True)
                self._trim_end_edit.setEnabled(True)
                self._redetect_button.setEnabled(True)
            else:
                self._selected_name_label.setText("—")
                self._trim_start_edit.setEnabled(False)
                self._trim_end_edit.setEnabled(False)
                self._redetect_button.setEnabled(selected is not None)
        finally:
            self._syncing_ui = False

    def splice_mode(self) -> str:
        return (
            SPLICE_CROSSFADE
            if self._splice_combo.currentIndex() == 1
            else SPLICE_BUTT
        )

    def _junction_seconds(self) -> float:
        """时间轴相邻条目步进：直拼=间隔；交叉淡化=-接缝淡化时长（重叠）。"""
        if self.splice_mode() == SPLICE_CROSSFADE:
            return -self._fade_row.value()
        return self._gap_row.value()

    def _sync_timeline(self) -> None:
        self._timeline.set_clips(self._clips, gap_seconds=self._junction_seconds())
        self._refresh_playhead_label()

    def _refresh_playhead_label(self) -> None:
        self._playhead_label.setText(
            f"{format_timecode(self._timeline.playhead())} / {format_timecode(self._timeline.total_duration())}"
        )

    # ── 裁剪编辑 ───────────────────────────────────────────────

    def _on_trim_edited(self) -> None:
        """时间码输入框提交（回车/失焦）后落到剪辑并规整回显。"""
        if self._syncing_ui:
            return
        clip = self._selected_clip()
        if clip is None or not clip.analyzed():
            return
        start = max(
            0.0, min(self._trim_start_edit.seconds(), self._trim_end_edit.seconds() - 0.05)
        )
        end_value = self._trim_end_edit.seconds()
        end = None if abs(end_value - clip.duration) < 0.0005 else end_value
        clip.apply_manual(start, end)
        self._syncing_ui = True
        try:
            self._trim_start_edit.set_seconds(clip.trim_start)
            self._trim_end_edit.set_seconds(clip.effective_end())
        finally:
            self._syncing_ui = False
        self._sync_timeline()
        self._mark_preview_dirty()
        self._refresh_row_for_clip(clip)

    def _on_timeline_clip_selected(self, clip) -> None:
        """时间轴上点选条目：同步选中态（不回发时间轴）。"""
        if clip is None:
            self._selected_index = None
        else:
            try:
                self._selected_index = self._clips.index(clip)
            except ValueError:
                return
        self._refresh_selected_clip()

    def _on_timeline_reordered(self, new_order: list) -> None:
        if self.is_busy() or len(new_order) != len(self._clips):
            self._sync_timeline()
            return
        # 焦点跟着被拖动的条目走：先在旧顺序里取到它，再换新顺序并改写选中索引。
        dragged = self._selected_clip()
        self._clips = list(new_order)
        if dragged is not None and dragged in self._clips:
            self._selected_index = self._clips.index(dragged)
        self._rebuild_clip_rows()
        self._refresh_selected_clip()
        self._mark_preview_dirty()

    def _on_timeline_trim(self, clip, start: float, end) -> None:
        """时间轴条目边缘拖拽提交的裁剪。"""
        clip.apply_manual(start, end)
        self._syncing_ui = True
        try:
            if clip is self._selected_clip():
                self._trim_start_edit.set_seconds(clip.trim_start)
                self._trim_end_edit.set_seconds(clip.effective_end())
        finally:
            self._syncing_ui = False
        self._sync_timeline()
        self._mark_preview_dirty()
        self._refresh_row_for_clip(clip)

    def _on_timeline_playhead(self, seconds: float) -> None:
        self._refresh_playhead_label()
        if self._player is not None and self._player_playback_active():
            self._player.setPosition(int(seconds * 1000))

    def _on_reset_trim(self) -> None:
        clip = self._selected_clip()
        if clip is None:
            return
        clip.reset_to_full()
        self._refresh_selected_clip()
        self._sync_timeline()
        self._mark_preview_dirty()
        self._refresh_row_for_clip(clip)

    def _refresh_row_for_clip(self, clip: MergeClip) -> None:
        for row in self._clip_rows:
            if row.clip is clip:
                row.refresh()
                return

    # ── 分析任务 ───────────────────────────────────────────────

    def _start_analysis(self, clips: list[MergeClip] | None = None) -> None:
        if self.is_busy():
            return
        targets = list(clips) if clips is not None else list(self._clips)
        if not targets:
            return
        try:
            ffmpeg_dir = self._host.resolve_ffmpeg_dir()
        except ProcessingError as exc:
            show_fluent_error(self.window(), str(exc))
            return
        threshold_db = self._threshold_row.value()
        min_silence = self._min_silence_row.value()
        margin = self._margin_row.value()
        for clip in targets:
            clip.error = ""
            self._refresh_row_for_clip(clip)
        self._cancel_requested = False
        self._set_busy(True, "正在分析素材静音…")

        def runner(logger) -> list:
            failures: list = []
            for position, clip in enumerate(targets, 1):
                if self._cancel_requested:
                    raise ExportCancelled("已停止分析。")
                logger(f"({position}/{len(targets)}) 正在分析: {clip.path.name}")
                clip.error = ""
                try:
                    analysis = analyze_clip(
                        clip.path,
                        ffmpeg_dir,
                        logger,
                        label=f"素材 {position}",
                        threshold_db=threshold_db,
                        min_silence_seconds=min_silence,
                        margin_seconds=margin,
                        should_cancel=lambda: self._cancel_requested,
                    )
                except ExportCancelled:
                    raise
                except ProcessingError as exc:
                    from krok_helper.media_tools import report_missing_tool

                    report_missing_tool(exc)
                    clip.error = str(exc)
                    failures.append(clip.path.name)
                    logger(f"分析失败: {clip.path.name}")
                    continue
                clip.waveform = analysis.waveform
                clip.duration = analysis.waveform.duration
                clip.sample_rate = analysis.sample_rate
                clip.channels = analysis.channels
                clip.apply_suggested(analysis.trim_start, analysis.trim_end)
            return failures

        task = BackgroundTask(runner)
        task.log_message.connect(self._append_log)
        task.task_succeeded.connect(self._on_analysis_succeeded)
        task.task_failed.connect(self._on_analysis_failed)
        self.merge_analysis_task = self._register_task("merge_analysis_task", task)
        task.start()

    def _on_analysis_succeeded(self, failures: object) -> None:
        self._set_busy(False, "素材分析完成")
        self._mark_preview_dirty()
        self._rebuild_clip_rows()
        self._refresh_selected_clip()
        failed = list(failures or [])
        if failed:
            self._append_log(f"有 {len(failed)} 条素材分析失败，已保留原样，可移除后重试。")
            show_fluent_error(self, "以下素材分析失败：\n" + "\n".join(failed))
        if self._merge_after_analysis:
            self._merge_after_analysis = False
            if failed:
                self._status_label.setText("有素材分析失败，已取消自动合成")
                return
            self._start_export()

    def _on_analysis_failed(self, message: str) -> None:
        self._merge_after_analysis = False
        self._set_busy(False, "素材分析失败")
        self._rebuild_clip_rows()
        self._refresh_selected_clip()
        if "已停止" in message or "取消" in message:
            self._status_label.setText("已停止")
            self._append_log("分析已停止。")
        else:
            self._append_log(f"分析任务失败: {message}")
            show_fluent_error(self.window(), f"素材分析失败：\n{message}")

    def _on_redetect_selected(self) -> None:
        clip = self._selected_clip()
        if clip is None or self.is_busy():
            return
        self._start_analysis([clip])

    # ── 合成任务 ───────────────────────────────────────────────

    def _mergeable(self) -> bool:
        return bool(self._clips) and all(clip.analyzed() for clip in self._clips)

    def _on_merge_clicked(self) -> None:
        """唯一合成入口：以用户当前调整好的状态（预览所听即所得）导出。

        若尚有未做静音处理的素材，先自动补做（只处理 ``full`` 状态的条目，
        绝不覆盖用户已手动/自动调整过的），随后自动导出。
        """
        if self.is_busy() or not self._mergeable():
            return
        untreated = [
            clip
            for clip in self._clips
            if clip.analyzed() and clip.trim_source == TRIM_SOURCE_FULL
        ]
        if untreated:
            self._append_log(f"有 {len(untreated)} 条素材未做静音处理，先自动补做（不覆盖已调整的条目）。")
            self._merge_after_analysis = True
            self._start_analysis(untreated)
        else:
            self._start_export()

    def _on_redetect_all_clicked(self) -> None:
        """全部重新检测静音：会覆盖所有条目的裁剪，含手动调整（先确认）。"""
        if self.is_busy() or not self._clips:
            return
        manual_clips = [clip for clip in self._clips if clip.trim_source == TRIM_SOURCE_MANUAL]
        if manual_clips:
            names = "、".join(clip.path.name for clip in manual_clips[:3])
            more = " 等" if len(manual_clips) > 3 else ""
            box = MessageBox(
                "覆盖手动调整？",
                f"{names}{more}曾被手动调整过裁剪点。重新检测全部会用新的检测结果覆盖"
                f"这些手动值，之后仍可继续手动微调。是否继续？",
                self.window(),
            )
            if not box.exec():
                return
        self._start_analysis()

    def _resolve_output_path(self) -> Path:
        fmt = self._output_card.output_format()
        raw_name = self._output_card.name_edit.text().strip()
        if raw_name:
            stem = raw_name
            if stem.lower().endswith(f".{fmt}"):
                stem = stem[: -(len(fmt) + 1)]
            stem = stem.strip() or datetime.now().strftime("合成_%Y%m%d-%H%M%S")
        else:
            stem = datetime.now().strftime("合成_%Y%m%d-%H%M%S")
        output_dir = Path(self._output_card.output_dir() or self._clips[0].path.parent)
        return unique_output_path(output_dir, f"{stem}.{fmt}")

    def _start_export(self, *, preview: bool = False) -> None:
        if self.is_busy() or not self._mergeable():
            return
        try:
            ffmpeg_dir = self._host.resolve_ffmpeg_dir()
        except ProcessingError as exc:
            show_fluent_error(self.window(), str(exc))
            return
        if preview:
            # 先清旧预览再取名：正常情况下目录里始终只有 preview.wav 一个文件，
            # 不会随历次渲染递增出 preview (2).wav、preview (3).wav……
            self._cleanup_preview()
            output_path = unique_output_path(_preview_dir(), "preview.wav")
        else:
            output_path = self._resolve_output_path()
        joint_fade = self._fade_row.value()
        gap = self._gap_row.value()
        head_fade = self._head_fade_check.isChecked()
        tail_fade = self._tail_fade_check.isChecked()
        splice_mode = self.splice_mode()
        self._cancel_requested = False
        if preview:
            self._preview_path = output_path
            self._set_busy(True, "正在渲染整体预览…")
        else:
            self._set_busy(True, f"正在合成: {output_path.name}")
        clips_snapshot = list(self._clips)

        def runner(logger) -> Path:
            return run_merge(
                clips_snapshot,
                output_path,
                ffmpeg_dir,
                logger,
                joint_fade_seconds=joint_fade,
                gap_seconds=gap,
                head_fade=head_fade,
                tail_fade=tail_fade,
                splice_mode=splice_mode,
                should_cancel=lambda: self._cancel_requested,
                on_process_started=self._register_merge_process,
            )

        task = BackgroundTask(runner)
        task.log_message.connect(self._append_log)
        task.task_succeeded.connect(self._on_preview_succeeded if preview else self._on_export_succeeded)
        task.task_failed.connect(self._on_export_failed)
        self.merge_export_task = self._register_task("merge_export_task", task)
        task.start()

    def _on_export_succeeded(self, output: object) -> None:
        self._set_busy(False, "合成完成")
        self._last_output = output if isinstance(output, Path) else None
        self._open_output_button.setEnabled(self._last_output is not None)
        play_completion_sound()
        if self._last_output is not None:
            show_fluent_info(
                self.window(),
                f"音频合成完成：\n{self._last_output}",
                title="合成完成",
            )

    def _on_export_failed(self, message: str) -> None:
        self._set_busy(False, "合成失败")
        self._cleanup_preview()
        if "已停止" in message or "取消" in message:
            self._status_label.setText("已停止")
            self._append_log("任务已停止。")
            return
        self._append_log(f"任务失败: {message}")
        show_fluent_error(self.window(), f"音频合成失败：\n{message}")

    def _register_merge_process(self, process: subprocess.Popen | None) -> None:
        self._merge_process = process

    # ── 整体预览播放（时间轴走带）──────────────────────────────

    def _mark_preview_dirty(self) -> None:
        self._preview_dirty = True
        # 播放中改切割/拼接：立刻后台重渲染整体预览，完成后从当前播放头续播。
        if (
            self._player_playback_active()
            and not self.is_busy()
            and self._mergeable()
        ):
            self._resume_after_render = True
            self._start_export(preview=True)

    def _player_playback_active(self) -> bool:
        from PyQt6.QtMultimedia import QMediaPlayer

        return (
            self._player is not None
            and self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
        )

    def _on_play_clicked(self) -> None:
        if self.is_busy() or not self._mergeable():
            return
        if self._player_playback_active():
            self._stop_playback()
            return
        needs_render = (
            self._preview_dirty
            or self._preview_path is None
            or not self._preview_path.is_file()
        )
        if needs_render:
            self._start_export(preview=True)
        else:
            self._start_playback_at_playhead()

    def _start_playback_at_playhead(self) -> None:
        player = self._ensure_player()
        seek_ms = int(self._timeline.playhead() * 1000)
        self._pending_seek_ms = seek_ms
        player.setSource(QUrl.fromLocalFile(str(self._preview_path)))
        self._playback_mode = "preview"
        player.play()
        # FFmpeg 后端媒体加载是异步的：play 前的 setPosition 可能被加载过程
        # 冲掉，这里先立即试一次，LoadedMedia 后再补一次，保证严格从播放头起播。
        player.setPosition(seek_ms)
        self._status_label.setText("正在播放整体效果，拖动刻度尺可跳转")
        self._update_action_state()

    def _on_preview_succeeded(self, output: object) -> None:
        self._set_busy(False, "整体预览已就绪")
        path = output if isinstance(output, Path) else None
        if path is None or not path.is_file():
            return
        self._preview_dirty = False
        if self._resume_after_render:
            self._resume_after_render = False
            self._status_label.setText("切割/拼接已更新，从当前位置继续播放")
        self._start_playback_at_playhead()

    def _cleanup_preview(self) -> None:
        if self._preview_path is not None:
            try:
                self._preview_path.unlink(missing_ok=True)
            except OSError:
                pass
            self._preview_path = None
        # 当前文件之外可能还有删失败的残留（如播放器曾占用），一并尝试清掉。
        purge_stale_preview_files()

    # ── 试听播放 ───────────────────────────────────────────────

    def _ensure_player(self):
        if self._player is not None:
            return self._player
        from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer

        self._audio_output = QAudioOutput(self)
        self._player = QMediaPlayer(self)
        self._player.setAudioOutput(self._audio_output)
        self._player.playbackStateChanged.connect(self._on_playback_state_changed)
        self._player.positionChanged.connect(self._on_player_position)
        self._player.mediaStatusChanged.connect(self._on_media_status_changed)
        return self._player

    def _on_media_status_changed(self, status) -> None:
        from PyQt6.QtMultimedia import QMediaPlayer

        if (
            status == QMediaPlayer.MediaStatus.LoadedMedia
            and self._pending_seek_ms > 0
            and self._playback_mode == "preview"
        ):
            self._player.setPosition(self._pending_seek_ms)
            self._pending_seek_ms = 0

    def _on_player_position(self, position_ms: int) -> None:
        if self._playback_mode != "preview":
            return  # 单曲试听不带动时间轴播放头，避免「串轴」误导
        self._timeline.set_playhead(position_ms / 1000.0, from_player=True)
        self._refresh_playhead_label()

    def _toggle_playback(self, clip: MergeClip) -> None:
        if self._playing_clip is clip:
            self._stop_playback()
            return
        self._start_playback(clip.path, clip)

    def _start_playback(self, path: Path, clip: MergeClip | None) -> None:
        player = self._ensure_player()
        self._stop_playback()
        self._playing_clip = clip
        self._playback_mode = "clip"
        player.setSource(QUrl.fromLocalFile(str(path)))
        player.play()
        self._status_label.setText(f"正在试听素材：{clip.path.name if clip else ''}")
        self._refresh_play_icons()

    def _stop_playback(self) -> None:
        if self._player is not None:
            self._player.stop()
        self._playing_clip = None
        self._playback_mode = ""
        self._pending_seek_ms = 0
        self._resume_after_render = False
        self._refresh_play_icons()
        self._update_action_state()

    def _on_playback_state_changed(self, state) -> None:
        from PyQt6.QtMultimedia import QMediaPlayer

        if state == QMediaPlayer.PlaybackState.StoppedState:
            self._playing_clip = None
            self._refresh_play_icons()
            if self._status_label.text().startswith("正在播放整体效果"):
                self._status_label.setText("播放结束，可拖动刻度尺继续试听")
        self._update_action_state()

    def _refresh_play_icons(self) -> None:
        for row in self._clip_rows:
            row.set_playing(self._playing_clip is row.clip)

    # ── 操作条 / 忙碌状态 ──────────────────────────────────────

    def _on_stop_clicked(self) -> None:
        self._cancel_requested = True
        self._stop_playback()
        process = self._merge_process
        if process is not None and process.poll() is None:
            terminate_process(process)
        self._status_label.setText("正在停止…")

    def _on_open_output_clicked(self) -> None:
        if self._last_output is None:
            return
        open_in_explorer(self._last_output)

    def _set_busy(self, busy: bool, status: str) -> None:
        self._status_label.setText(status)
        self._progress.setVisible(busy)
        if busy:
            self._progress.start()
        else:
            self._progress.stop()
            self._merge_process = None
        self._update_action_state()

    def _update_action_state(self) -> None:
        busy = self.is_busy()
        mergeable = self._mergeable()
        playing = self._player_playback_active()
        self._merge_button.setEnabled(not busy and mergeable)
        self._play_button.setEnabled(not busy and mergeable and (not playing))
        self._stop_playback_button.setEnabled(playing)
        self._stop_button.setEnabled(busy)
        self._drop_zone.setEnabled(not busy)
        if not self._clips and not busy:
            self._status_label.setText("就绪")

    def _register_task(self, slot: str, task: BackgroundTask) -> BackgroundTask:
        setattr(self, slot, task)
        task.finished.connect(lambda slot=slot, task=task: self._forget_task(slot, task))
        return self._host.track_background_task(task)

    def _forget_task(self, slot: str, task: BackgroundTask) -> None:
        if getattr(self, slot, None) is task:
            setattr(self, slot, None)

    def running_tasks(self) -> list[BackgroundTask]:
        tasks = (self.merge_analysis_task, self.merge_export_task)
        return [t for t in tasks if t is not None and t.isRunning()]

    def is_busy(self) -> bool:
        return bool(self.running_tasks())

    def update_blocking_labels(self) -> list[str]:
        labels = []
        if self.merge_analysis_task is not None and self.merge_analysis_task.isRunning():
            labels.append("音频合成－素材分析中")
        if self.merge_export_task is not None and self.merge_export_task.isRunning():
            labels.append("音频合成－合成中")
        return labels

    def _append_log(self, message: str) -> None:
        self._log_view.appendPlainText(message)

    # ── 生命周期 ───────────────────────────────────────────────

    def shutdown(self) -> None:
        """受控收尾：停播放、删播放器、清预览临时文件。

        由外壳 closeEvent 的模块收尾链调用（AGENTS.md §9 的显式销毁顺序
        套路）：把这些资源的销毁从解释器终结阶段提前到窗口关闭时、事件循
        环还活着的时候，避免 Qt 收尾竞态。
        """
        self._cancel_requested = True
        self._stop_playback()
        if self._player is not None:
            try:
                self._player.deleteLater()
            except RuntimeError:
                pass
            self._player = None
        if self._audio_output is not None:
            try:
                self._audio_output.deleteLater()
            except RuntimeError:
                pass
            self._audio_output = None
        self._cleanup_preview()
        if self._timeline is not None:
            self._timeline.attach_scrollbar(None)

    def hideEvent(self, event) -> None:  # noqa: N802
        self._stop_playback()
        super().hideEvent(event)

    def closeEvent(self, event) -> None:  # noqa: N802
        self._stop_playback()
        self._cleanup_preview()
        super().closeEvent(event)
