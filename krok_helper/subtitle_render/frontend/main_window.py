"""字幕视频渲染主窗口（Sayatoo 风格 + 顶部工作区导航 + 拖拽加载）。

照搬 SUG（lyrics_timing/.../frontend/main_window.py）的双模式骨架：

- ``SubtitleRenderWindow(embedded=False)`` — 默认 standalone
- ``SubtitleRenderWindow.for_embedding(parent, settings_provider, workflow_context)``
  — 嵌入工作台

UI 顶层结构（工作区导航居中放在项目命令栏）：

  ┌──────────────────────────────────────────────────────┐
  │  项目命令栏             [预览] [导出]                 │
  │  ┌─────────┬──────────────┬──────────────┐          │
  │  │ 左·歌词 │ 中·预览       │ 右·属性 tab │          │
  │  │(拖.sug/.lrc) + transport│              │          │
  │  ├─────────┴──────────────┴──────────────┤          │
  │  │ 底·字幕轨道                            │          │
  │  └─────────────────────────────────────────┘          │
  └──────────────────────────────────────────────────────┘

三个素材区均接受拖拽 + 点击浏览（详见 :mod:`drop_panel`）。
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, fields, replace
from datetime import datetime
import hashlib
import logging
from math import isfinite
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Optional, TYPE_CHECKING
from uuid import uuid4

if TYPE_CHECKING:  # 只为类型标注，运行时不引入宿主包，保持模块可独立运行
    from krok_helper.workflow_host import SubtitleVideoSink

from PyQt6.QtCore import (
    QEvent,
    QFileSystemWatcher,
    QObject,
    QPoint,
    QRect,
    QSize,
    QThread,
    QTimer,
    QUrl,
    Qt,
    pyqtSignal as Signal,
)
from PyQt6.QtGui import (
    QColor,
    QDesktopServices,
    QIcon,
    QImage,
    QKeySequence,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QShortcut,
)
from PyQt6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QFileDialog,
    QColorDialog,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    Action,
    BodyLabel,
    CaptionLabel,
    CheckBox,
    ComboBox as FluentComboBox,
    DropDownPushButton,
    FluentIcon as FIF,
    InfoBar,
    InfoBarPosition,
    LineEdit as FluentLineEdit,
    ListWidget as FluentListWidget,
    PrimaryPushButton as FluentPrimaryPushButton,
    ProgressBar as FluentProgressBar,
    PushButton as FluentPushButton,
    RadioButton as FluentRadioButton,
    RoundMenu,
    SimpleCardWidget,
    SpinBox as FluentSpinBox,
    StrongBodyLabel,
    ToolButton as FluentToolButton,
    TitleLabel,
)

from krok_helper.errors import ExportCancelled, ProcessingError
from krok_helper.ffmpeg import find_tool, probe_media, terminate_process
from krok_helper.models import MediaInfo
from krok_helper.notifications import play_completion_sound
from krok_helper.qfluent_compat import (
    ModelessDialog,
    apply_qfluent_menu_lifetime_patch,
    apply_qfluent_tooltip_parent_patch,
)
from krok_helper.settings import get_settings_path, load_app_settings, save_app_settings
from krok_helper.subtitle_render.engine.encoder_select import (
    CODEC_H264,
    CODEC_HEVC,
    CPU_PRESETS,
    ENCODER_AMF,
    ENCODER_AUTO,
    ENCODER_CPU,
    ENCODER_NVENC,
    ENCODER_QSV,
)
from krok_helper.subtitle_render.engine.painter import (
    LayoutMarginWarning,
    _resolve_title_text,
    apply_layout_to_page,
    assign_layout_to_all,
    auto_assign_layouts_by_page,
    check_layout_margins,
    display_windows_for_style,
    layout_pass,
)
from krok_helper.subtitle_render.engine.page_plan import (
    build_legacy_page_plan,
    build_page_plan,
    delete_boundary,
    insert_boundary,
    move_page_boundary,
    normalize_page_plan,
    page_plan_has_manual_changes,
    project_page_plan_to_legacy_fields,
    reflow_pages_for_layout_capacity,
    resolve_page_plan,
)
from krok_helper.subtitle_render.engine.renderer import RenderJob, render_subtitle_video
from krok_helper.subtitle_render.engine.timeline import (
    apply_n3_seq_line_breaks,
    track_duration_ms,
)
from krok_helper.subtitle_render.guide_symbols import (
    GuideSymbolImportError,
    import_svg_guide_symbol,
)
from krok_helper.subtitle_render.frontend.drop_panel import DropPanel
from krok_helper.subtitle_render.frontend.fluent_dialogs import (
    fluent_button_row,
    fluent_choice,
    fluent_error,
    fluent_get_int,
    fluent_info,
    fluent_question,
    fluent_warning,
)
from krok_helper.subtitle_render.frontend.guide_replacement import (
    GuidePrefixMatch,
    GuidePrefixReplaceDialog,
    choose_guide_role_scheme,
    replacement_symbol_for_match,
)
from krok_helper.subtitle_render.frontend.lyrics_list import LyricsPanel
from krok_helper.subtitle_render.frontend.playback import (
    PlaybackController,
    unified_player_enabled,
)
from krok_helper.subtitle_render.frontend.preview_view import PreviewPanel, TransportBar
from krok_helper.subtitle_render.frontend.preview_async import (
    DEFAULT_PREVIEW_QUALITY,
    gpu_preview_enabled,
    normalize_preview_quality,
)
from krok_helper.subtitle_render.frontend.property_panel import (
    PropertyPanel,
    ScreenSettings,
    SCREEN_FPS_OPTIONS,
    match_screen_preset_key,
    screen_settings_from_dict,
    screen_settings_to_dict,
)
from krok_helper.subtitle_render.frontend.timeline_view import TrackTimelineView
from krok_helper.subtitle_render.frontend.workspace_switcher import WorkspaceSwitcher
from krok_helper.subtitle_render.models import (
    BackgroundSource,
    DEFAULT_EXPORT_NAME_TEMPLATE,
    DEFAULT_OUTPUT_NAME_SUFFIX,
    EXPORT_NAME_TEMPLATE_FIELDS,
    GuideSymbol,
    LineAnimationOverride,
    LYRICS_LAYOUT_FIELDS,
    PROJECT_FILE_SUFFIX,
    StylePreset,
    SubtitleLoadingSettings,
    SubtitleStyleScheme,
    Style,
    TITLE_SCHEME_NAME,
    TitleOverlay,
    TimingTrack,
    background_sequence_frame_path,
    guide_symbol_from_dict,
    guide_symbol_replacement_count,
    guide_symbol_role_labels,
    guide_symbol_with_role_labels,
    guide_symbol_to_dict,
    ensure_page_layout_defaults,
    layout_capacity,
    layout_display_name,
    layout_id_for_index,
    line_animation_override_from_dict,
    line_animation_override_to_dict,
    migrate_legacy_app_title_default,
    normalize_title_char_role_labels,
    rescale_font_sizes,
    rescale_layout_sizes,
    subtitle_style_scheme_from_dict,
    subtitle_style_scheme_to_dict,
    style_from_dict,
    style_to_dict,
    subtitle_loading_settings_from_dict,
    subtitle_loading_settings_to_dict,
    track_page_plan_from_dict,
    track_page_plan_to_dict,
    timing_line_start_ms,
    infer_image_sequence_pattern,
)
from krok_helper.subtitle_render.n3_font_catalog import (
    get_n3_font_catalog,
    normalize_scheme_font_families,
    normalize_style_font_families,
)
from krok_helper.subtitle_render.n3proj_import import (
    N3_PROJECT_FILE_SUFFIX,
    N3_PROJECT_FILTER,
    load_n3proj,
)
from krok_helper.subtitle_render.project_store import (
    ProjectFileRevision,
    RecoveryCandidate,
    backup_project_file,
    background_payload,
    inspect_project_file,
    invalidate_recovery_project,
    load_render_project,
    project_output_payload,
    project_payload,
    save_discarded_project_backup,
    save_recovery_project,
    save_render_project,
    scan_recovery_projects,
    split_project_paths,
)
from krok_helper.subtitle_render.subtitle_sources import load_nicokara_lrc
from krok_helper.subtitle_render.sug_project import (
    load_sug_timing_track,
    timing_track_from_sug_project,
)
from krok_helper.subtitle_render.source_reload import (
    TrackReloadMerge,
    merge_reloaded_track,
)
from krok_helper.subtitle_render.frontend.theme import palette, stage_bg, themed

apply_qfluent_menu_lifetime_patch()
apply_qfluent_tooltip_parent_patch()

SUBTITLE_FILTER = "SUG 项目 / Nicokara LRC (*.sug *.lrc);;SUG 项目 (*.sug);;Nicokara 逐字 LRC (*.lrc);;所有文件 (*.*)"

_UNDO_STACK_LIMIT = 200
"""撤销栈上限（字幕轨道显示/隐藏时间编辑）。"""
VIDEO_FILTER = "视频文件 (*.mp4 *.mkv *.mov *.webm *.avi *.flv);;所有文件 (*.*)"
IMAGE_FILTER = "图片文件 (*.png *.jpg *.jpeg *.bmp *.webp *.tif *.tiff);;所有文件 (*.*)"
AUDIO_FILTER = "音频文件 (*.wav *.flac *.mp3 *.m4a *.aac *.ogg *.opus);;所有文件 (*.*)"
BACKGROUND_MEDIA_FILTER = (
    "背景素材 (*.mp4 *.mkv *.mov *.webm *.avi *.flv *.png *.jpg *.jpeg *.bmp *.webp *.tif *.tiff);;"
    + VIDEO_FILTER + ";;" + IMAGE_FILTER
)
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff"}
PROJECT_FILTER = f"字幕渲染项目 (*{PROJECT_FILE_SUFFIX});;所有文件 (*.*)"
EXPORT_DIR_SOURCE_VIDEO = "source_video"
EXPORT_DIR_CUSTOM = "custom"
AUTO_SAVE_DEBOUNCE_MS = 2_000
_PERSISTED_STATE_SAVE_DEBOUNCE_MS = 1_500
"""应用级偏好落盘的空闲窗口：编辑停手后才写 settings.json。"""
DEFAULT_AUTO_SAVE_INTERVAL_MINUTES = 5
AUTO_SAVE_THREAD_WAIT_MS = 3_000
GPU_PREVIEW_DEFAULT_VERSION = 2
GPU_EXPORT_DEFAULT_VERSION = 1
DEFAULT_PROJECT_BACKUP_COUNT = 5
DISCARDED_BACKUP_RETENTION_DAYS = 7
RENDER_WORKER_OPTIONS = (0, 4, 8, 12, 16)
"""0 = 自动（最多 8）；其余值为用户显式选择的渲染进程数。"""


def _layout_issue_icon() -> QIcon:
    """Return the outlined warning-triangle icon used by the issue entry."""
    pixmap = QPixmap(24, 24)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    color = QColor(palette().text_primary)
    painter.setPen(QPen(color, 1.7, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    path = QPainterPath()
    path.moveTo(12, 2.8)
    path.lineTo(22, 20.5)
    path.lineTo(2, 20.5)
    path.closeSubpath()
    painter.drawPath(path)
    painter.drawLine(12, 8, 12, 15)
    painter.setBrush(color)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawEllipse(11, 17, 2, 2)
    painter.end()
    return QIcon(pixmap)


@dataclass(frozen=True)
class SubtitleProjectState:
    """宿主可消费的字幕渲染项目状态。"""

    display_name: str
    path: Optional[Path]
    has_project: bool
    dirty: bool
    saving: bool
    save_error: Optional[str]
    exporting: bool
    recovery_path: Optional[Path]
    missing_resources: tuple[tuple[str, Path], ...] = ()

    def status_text(self) -> Optional[str]:
        if not self.has_project:
            return None
        states: list[str] = []
        if self.saving:
            states.append("正在保存")
        elif self.save_error:
            states.append("保存失败")
        elif self.dirty:
            states.append("未保存")
        if self.exporting:
            states.append("导出中")
        if self.missing_resources:
            states.append(f"素材缺失 {len(self.missing_resources)} 项")
        return f"{self.display_name} · {' · '.join(states)}" if states else self.display_name


@dataclass(frozen=True)
class _LayoutIssue:
    """One persistent layout issue, including its subtitle-source identity."""

    track_index: int
    source_name: str
    warning: LayoutMarginWarning


class _LayoutIssuesDialog(ModelessDialog):
    """Modeless, clickable list of the current lyrics layout issues."""

    issueActivated = Signal(int, int)

    def __init__(
        self,
        issues: list[_LayoutIssue],
        parent: Optional[QWidget] = None,
    ) -> None:
        anchor = parent.window() if parent is not None else None
        super().__init__(anchor)
        self.setObjectName("LayoutIssuesDialog")
        self.setWindowTitle("当前歌词问题")
        self.setMinimumSize(620, 360)
        self.resize(720, 440)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(10)
        layout.addWidget(StrongBodyLabel("当前歌词存在的问题", self))
        self._summary_label = CaptionLabel("", self)
        self._summary_label.setWordWrap(True)
        layout.addWidget(self._summary_label)

        self._list_widget = FluentListWidget(self)
        self._list_widget.setObjectName("LayoutIssuesList")
        self._list_widget.setWordWrap(False)
        self._list_widget.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._list_widget.itemClicked.connect(self._on_item_clicked)
        layout.addWidget(self._list_widget, 1)

        button_row = QHBoxLayout()
        button_row.addStretch(1)
        close_button = FluentPushButton("关闭", self)
        close_button.clicked.connect(self.close)
        button_row.addWidget(close_button)
        layout.addLayout(button_row)

        themed(
            self,
            lambda: (
                f"#LayoutIssuesDialog {{ background: {palette().panel_bg}; "
                f"color: {palette().text_primary}; }}"
            ),
        )
        self.set_issues(issues)

    def set_issues(self, issues: list[_LayoutIssue]) -> None:
        """Replace the list while the dialog is open."""
        self._list_widget.clear()
        overflow_count = sum(
            issue.warning.level == "overflow" for issue in issues
        )
        margin_count = len(issues) - overflow_count
        parts = []
        if overflow_count:
            parts.append(f"{overflow_count} 行超出画面")
        if margin_count:
            parts.append(f"{margin_count} 行侵入左右余白")
        summary = "、".join(parts) if parts else "未发现歌词布局问题"
        self._summary_label.setText(f"{summary}。点击任一行可跳转歌词与预览。")
        for issue in issues:
            warning = issue.warning
            kind = "字幕溢出画面" if warning.level == "overflow" else "左右余白无法确保"
            text = " ".join(warning.text.split()) or "（空歌词）"
            display = (
                f"{issue.source_name} · 第 {warning.line_index + 1} 行　"
                f"{kind}　{text}"
            )
            item = QListWidgetItem(display)
            item.setData(
                Qt.ItemDataRole.UserRole,
                (issue.track_index, warning.line_index),
            )
            item.setToolTip(
                f"{issue.source_name} · 第 {warning.line_index + 1} 行\n"
                f"{kind}\n{text}"
            )
            self._list_widget.addItem(item)

    def _on_item_clicked(self, item: QListWidgetItem) -> None:
        target = item.data(Qt.ItemDataRole.UserRole)
        if (
            isinstance(target, tuple)
            and len(target) == 2
            and all(isinstance(value, int) for value in target)
        ):
            self.issueActivated.emit(target[0], target[1])


class _ExportLocationDialog(ModelessDialog):
    """字幕视频导出目录与文件名偏好。"""

    def __init__(
        self,
        mode: str,
        custom_dir: str,
        initial_dir: Path,
        parent: Optional[QWidget] = None,
        name_template: str = DEFAULT_EXPORT_NAME_TEMPLATE,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("导出视频位置与命名")
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.setMinimumWidth(520)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)

        title = StrongBodyLabel("导出视频位置", self)
        layout.addWidget(title)
        self.source_radio = FluentRadioButton("保存在字幕视频所在目录", self)
        self.custom_radio = FluentRadioButton("保存在指定目录", self)
        group = QButtonGroup(self)
        group.addButton(self.source_radio)
        group.addButton(self.custom_radio)
        layout.addWidget(self.source_radio)
        layout.addWidget(self.custom_radio)

        directory_row = QHBoxLayout()
        directory_row.setContentsMargins(24, 0, 0, 0)
        directory_row.setSpacing(8)
        self.directory_edit = FluentLineEdit(self)
        self.directory_edit.setReadOnly(True)
        self.directory_edit.setPlaceholderText("选择指定目录")
        self.directory_edit.setText(custom_dir)
        self.browse_button = FluentPushButton(FIF.FOLDER, "浏览", self)
        directory_row.addWidget(self.directory_edit, 1)
        directory_row.addWidget(self.browse_button)
        layout.addLayout(directory_row)

        hint = CaptionLabel(
            "没有视频素材时，将使用背景素材或字幕文件所在目录。",
            self,
        )
        layout.addWidget(hint)

        layout.addSpacing(6)
        layout.addWidget(StrongBodyLabel("默认文件名", self))
        self.name_template_edit = FluentLineEdit(self)
        self.name_template_edit.setPlaceholderText(DEFAULT_EXPORT_NAME_TEMPLATE)
        self.name_template_edit.setText(name_template)
        layout.addWidget(self.name_template_edit)

        placeholder_lines = "；".join(
            f"{{{name}}} {desc}" for name, desc in EXPORT_NAME_TEMPLATE_FIELDS.items()
        )
        name_hint = CaptionLabel(
            f"可用占位符：{placeholder_lines}。不用写 .mp4。\n"
            f"留空则用默认：{DEFAULT_EXPORT_NAME_TEMPLATE}。"
            "改动只影响之后新载入的素材，不会改掉你已经手填的文件名。",
            self,
        )
        name_hint.setWordWrap(True)
        layout.addWidget(name_hint)
        self.name_error_label = CaptionLabel("", self)
        self.name_error_label.setWordWrap(True)
        self.name_error_label.setVisible(False)
        layout.addWidget(self.name_error_label)
        self.name_template_edit.textChanged.connect(self._sync_controls)

        button_row = QHBoxLayout()
        button_row.addStretch(1)
        cancel_button = FluentPushButton("取消", self)
        self.ok_button = FluentPrimaryPushButton("确定", self)
        cancel_button.clicked.connect(self.reject)
        self.ok_button.clicked.connect(self.accept)
        button_row.addWidget(cancel_button)
        button_row.addWidget(self.ok_button)
        layout.addLayout(button_row)

        self._initial_dir = initial_dir
        self.source_radio.setChecked(mode != EXPORT_DIR_CUSTOM)
        self.custom_radio.setChecked(mode == EXPORT_DIR_CUSTOM)
        self.source_radio.toggled.connect(self._sync_controls)
        self.custom_radio.toggled.connect(self._sync_controls)
        self.browse_button.clicked.connect(self._browse)
        self._sync_controls()

    def _browse(self) -> None:
        start = self.directory_edit.text().strip() or str(self._initial_dir)
        selected = QFileDialog.getExistingDirectory(
            self, "选择字幕视频导出目录", start
        )
        if selected:
            self.directory_edit.setText(selected)
            self.custom_radio.setChecked(True)
            self._sync_controls()

    def _name_template_error(self) -> str:
        """模板不合法时的原因；空串表示可用。留空视为用默认。"""
        from krok_helper.errors import ProcessingError
        from krok_helper.pipeline import validate_output_name_template

        template = self.name_template_edit.text().strip()
        if not template:
            return ""
        try:
            validate_output_name_template(
                template, "导出文件名", set(EXPORT_NAME_TEMPLATE_FIELDS)
            )
        except ProcessingError as exc:
            return str(exc)
        return ""

    def _sync_controls(self) -> None:
        custom = self.custom_radio.isChecked()
        self.directory_edit.setEnabled(custom)
        self.browse_button.setEnabled(custom)
        # 模板写错就在这里拦住，别等到点了导出才报错。
        error = self._name_template_error()
        self.name_error_label.setText(error)
        self.name_error_label.setVisible(bool(error))
        self.ok_button.setEnabled(
            (not custom or bool(self.directory_edit.text().strip())) and not error
        )

    def selection(self) -> tuple[str, str]:
        mode = EXPORT_DIR_CUSTOM if self.custom_radio.isChecked() else EXPORT_DIR_SOURCE_VIDEO
        return mode, self.directory_edit.text().strip()

    def name_template(self) -> str:
        return self.name_template_edit.text().strip() or DEFAULT_EXPORT_NAME_TEMPLATE


class _GuideSymbolSettingsDialog(ModelessDialog):
    """Configure how many inline guide glyphs precede a lyric line."""

    def __init__(
        self,
        *,
        count: int = 1,
        interval_ms: int = 1000,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent.window() if parent is not None else None)
        self.setWindowTitle("导唱符设置")
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.setMinimumWidth(460)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(10)

        layout.addWidget(BodyLabel("插入的导唱符数量：", self))
        self.count_spin = FluentSpinBox(self)
        self.count_spin.setRange(1, 32)
        self.count_spin.setValue(max(1, min(int(count), 32)))
        layout.addWidget(self.count_spin)

        layout.addWidget(BodyLabel("每个导唱符距离后一个字符多少毫秒：", self))
        self.interval_spin = FluentSpinBox(self)
        self.interval_spin.setRange(0, 10_000)
        self.interval_spin.setSingleStep(50)
        self.interval_spin.setValue(max(0, min(int(interval_ms), 10_000)))
        self.interval_spin.selectAll()
        layout.addWidget(self.interval_spin)

        hint = CaptionLabel(
            "例如数量为 3、间隔为 1000ms，会在歌词首字前 3000、2000、1000ms 依次走字。",
            self,
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)
        button_row, self.ok_button, _cancel_button = fluent_button_row(self)
        layout.addLayout(button_row)

    def settings(self) -> tuple[int, int]:
        return int(self.count_spin.value()), int(self.interval_spin.value())


class _AutoSaveSettingsDialog(ModelessDialog):
    """Project auto-save and history-backup settings."""

    def __init__(
        self,
        enabled: bool,
        interval_minutes: int,
        backup_count: int,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("项目保存与备份")
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.setMinimumWidth(420)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)

        layout.addWidget(StrongBodyLabel("项目保存与备份", self))
        self.enabled_check = CheckBox("启用字幕项目自动保存", self)
        self.enabled_check.setChecked(enabled)
        layout.addWidget(self.enabled_check)

        interval_row = QHBoxLayout()
        interval_row.addWidget(CaptionLabel("周期保存间隔", self))
        self.interval_spin = FluentSpinBox(self)
        self.interval_spin.setRange(1, 60)
        self.interval_spin.setSuffix(" 分钟")
        self.interval_spin.setValue(max(1, min(60, int(interval_minutes))))
        interval_row.addWidget(self.interval_spin)
        interval_row.addStretch(1)
        layout.addLayout(interval_row)
        layout.addWidget(
            CaptionLabel("编辑停止 2 秒后会先写一次恢复快照。", self)
        )

        backup_row = QHBoxLayout()
        backup_row.addWidget(CaptionLabel("手动保存历史备份", self))
        self.backup_count_spin = FluentSpinBox(self)
        self.backup_count_spin.setRange(1, 20)
        self.backup_count_spin.setSuffix(" 份")
        self.backup_count_spin.setValue(
            max(1, min(20, int(backup_count)))
        )
        backup_row.addWidget(self.backup_count_spin)
        backup_row.addStretch(1)
        layout.addLayout(backup_row)
        layout.addWidget(
            CaptionLabel("放弃未保存修改时，另保留 7 天紧急备份。", self)
        )

        button_row = QHBoxLayout()
        button_row.addStretch(1)
        cancel_button = FluentPushButton("取消", self)
        ok_button = FluentPrimaryPushButton("保存设置", self)
        cancel_button.clicked.connect(self.reject)
        ok_button.clicked.connect(self.accept)
        button_row.addWidget(cancel_button)
        button_row.addWidget(ok_button)
        layout.addLayout(button_row)

        self.enabled_check.toggled.connect(self.interval_spin.setEnabled)
        self.interval_spin.setEnabled(enabled)

    def selection(self) -> tuple[bool, int, int]:
        return (
            self.enabled_check.isChecked(),
            self.interval_spin.value(),
            self.backup_count_spin.value(),
        )

_BUILTIN_SCHEME_STYLE_FIELDS = frozenset(
    field.name
    for field in fields(SubtitleStyleScheme)
    if field.name in {style_field.name for style_field in fields(Style)}
    and field.name not in LYRICS_LAYOUT_FIELDS
)
_LAYOUT_DEFAULT_VALUE_FIELDS = frozenset(
    (*LYRICS_LAYOUT_FIELDS, "upper_line_left_margin_px", "lower_line_right_margin_px")
)
_LAYOUT_DEFAULT_STYLE_FIELDS = frozenset(
    (*_LAYOUT_DEFAULT_VALUE_FIELDS, "layouts", "layout_reference_height")
)
_FONT_DEFAULT_STYLE_FIELDS = frozenset({"font_reference_height"})
_PROJECT_ONLY_STYLE_FIELDS = frozenset(
    {"custom_style_schemes", "singer_style_overrides", "title_overlay"}
)


# 纯上色字段：只决定用什么颜色画，不影响字形几何、行宽或演唱时间。
# 只列这些，其余一律当几何字段——将来 Style 加了新字段而忘了归类，判定会保守地
# 认为「不是纯上色」，从而照常重算，不会漏掉该更新的东西。
_PAINT_ONLY_STYLE_FIELDS: frozenset[str] = frozenset({
    "base_color", "fill_color", "fill_gradient_enabled", "fill_gradient_start_color",
    "fill_gradient_end_color", "fill_gradient_angle_deg", "stroke_color", "shadow_color",
    "karaoke_colors", "ruby_color", "ruby_colors_follow_main", "ruby_karaoke_colors",
    "lit_fill_color", "lit1_fill_color", "lit2_fill_color", "lit3_fill_color",
    "lit_stroke_color", "volume_fill_color", "volume_stroke_color",
    "volume_overlay_fill_color", "volume_overlay_stroke_color",
})
_PAINT_ONLY_SCHEME_FIELDS: frozenset[str] = frozenset({
    "base_color", "fill_color", "fill_gradient_enabled", "fill_gradient_start_color",
    "fill_gradient_end_color", "fill_gradient_angle_deg", "stroke_color", "shadow_color",
    "ruby_color", "karaoke_colors", "ruby_colors_follow_main", "ruby_karaoke_colors",
})


def _paint_only_style_delta(previous: Style, current: Style) -> bool:
    """``current`` 与 ``previous`` 只差在纯上色字段上时为真。

    实现方式是把 ``previous`` 的上色字段折到 ``current`` 的副本上再整体比较：
    任何没被列进上色集合的字段仍然要逐字段相等，所以漏列一个字段只会让判定
    更保守，不会放过真正的几何变化。
    """

    def _strip(style: Style) -> Style:
        schemes = {
            name: replace(
                scheme,
                **{
                    field: getattr(previous_scheme, field)
                    for field in _PAINT_ONLY_SCHEME_FIELDS
                },
            )
            if (previous_scheme := previous.custom_style_schemes.get(name)) is not None
            else scheme
            for name, scheme in style.custom_style_schemes.items()
        }
        return replace(
            style,
            **{name: getattr(previous, name) for name in _PAINT_ONLY_STYLE_FIELDS},
            custom_style_schemes=schemes,
        )

    return _strip(current) == _strip(previous)


def fit_size_to_aspect(box: QSize, aspect_ratio: float) -> QSize:
    """把 ``box`` 缩到给定宽高比的最大内接尺寸（用于跟随画布形状的下限/建议值）。"""
    ratio = max(float(aspect_ratio), 0.1)
    width = max(box.width(), 1)
    height = max(box.height(), 1)
    if width / height >= ratio:
        return QSize(max(int(round(height * ratio)), 1), height)
    return QSize(width, max(int(round(width / ratio)), 1))


class _AspectRatioBox(QWidget):
    """Keep one child centered at a fixed aspect ratio."""

    def __init__(
        self,
        child: QWidget,
        *,
        aspect_ratio: float = 16 / 9,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._child = child
        self._aspect_ratio = max(float(aspect_ratio), 0.1)
        self._child.setParent(self)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(self.minimumSizeHint())

    def sizeHint(self) -> QSize:  # noqa: N802
        return fit_size_to_aspect(QSize(960, 540), self._aspect_ratio)

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return fit_size_to_aspect(QSize(426, 240), self._aspect_ratio)

    def set_aspect_ratio(self, width: int, height: int) -> None:
        """Update the child aspect ratio from an output size."""
        if width <= 0 or height <= 0:
            return
        ratio = max(float(width) / float(height), 0.1)
        if ratio == self._aspect_ratio:
            return
        self._aspect_ratio = ratio
        # 竖屏 / 4:3 画布的最小尺寸也要跟着换形，否则窗口被 16:9 的下限撑宽。
        self.setMinimumSize(self.minimumSizeHint())
        self._update_child_geometry()
        self.updateGeometry()

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        self._update_child_geometry()

    def _update_child_geometry(self) -> None:
        w = max(self.width(), 1)
        h = max(self.height(), 1)
        target_w = w
        target_h = int(round(target_w / self._aspect_ratio))
        if target_h > h:
            target_h = h
            target_w = int(round(target_h * self._aspect_ratio))
        x = (w - target_w) // 2
        y = (h - target_h) // 2
        self._child.setGeometry(QRect(x, y, max(target_w, 1), max(target_h, 1)))


class _SubtitleLoadingSettingsDialog(ModelessDialog):
    """Source-loading settings card, positioned to the right of its gear button."""

    def __init__(
        self,
        *,
        mode: str,
        effective: SubtitleLoadingSettings,
        global_defaults: SubtitleLoadingSettings,
        anchor: Optional[QWidget],
        parent: QWidget,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("加载字幕设置")
        self.setModal(False)
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.setMinimumWidth(390)
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(14)
        title = StrongBodyLabel("加载字幕设置", self)
        root.addWidget(title)
        hint = CaptionLabel("这些设置只控制字幕如何分段、分页，与渲染样式隔离。", self)
        hint.setWordWrap(True)
        root.addWidget(hint)

        form = QFormLayout()
        form.setSpacing(10)
        self._mode_combo = FluentComboBox(self)
        self._mode_combo.addItem("使用全局加载设置", userData="global")
        self._mode_combo.addItem("为此字幕源单独设置", userData="custom")
        self._mode_combo.setToolTip(
            "“使用全局加载设置”会修改应用级默认值，并自动刷新所有跟随全局设置的"
            "字幕源；“为此字幕源单独设置”只保存和刷新当前主字幕或副字幕源。"
            "两种模式都只影响分段、分页，不改变字体、颜色和画面布局参数。"
        )
        self._mode_combo.setCurrentIndex(0 if mode == "global" else 1)
        form.addRow("设置范围", self._mode_combo)

        self._gap_enabled = CheckBox("按演唱空隙分段", self)
        self._gap_enabled.setToolTip(
            "比较相邻两句的真实演唱时间。当“下一句开始时间 − 上一句结束时间”"
            "大于设定值时，从下一句开始新段落。提前显示、结束延时和动画时长"
            "不参与计算。保存加载设置或点击刷新后重新计算，并覆盖现有手工分页。"
        )
        form.addRow("", self._gap_enabled)
        self._gap_spin = FluentSpinBox(self)
        self._gap_spin.setRange(0, 120_000)
        self._gap_spin.setSingleStep(100)
        self._gap_spin.setSuffix(" ms")
        self._gap_spin.setToolTip(
            "自动分段使用的演唱空隙阈值，单位为毫秒。例如上一句在 10.000 秒结束、"
            "下一句在 14.500 秒开始，空隙为 4500 毫秒；阈值为 4000 毫秒时会"
            "开始新段落。仅在“按演唱空隙分段”启用时生效。"
        )
        form.addRow("分段间隔", self._gap_spin)
        self._blank_enabled = CheckBox("空行开始新段落", self)
        self._blank_enabled.setToolTip(
            "启用后，字幕源中的一个或多个连续空行会在下一条有效歌词前开始新段落。"
            "空行不占用字幕轨道，也不计入每页行数。关闭后，空行不影响段落和分页，"
            "也不会在歌词表中单独占一行。"
        )
        form.addRow("", self._blank_enabled)
        self._rows_spin = FluentSpinBox(self)
        self._rows_spin.setRange(1, 4)
        self._rows_spin.setToolTip(
            "在每个段落内按照指定行数依次建立页面，范围为 1～4。源文件中的显式"
            "分页仍会提前结束当前页；段落最后一页或显式分页前的页面允许不足指定"
            "行数，但仍使用基础行数对应的项目默认布局。例如基础行数为 3 时，只有"
            "1 行或 2 行的尾页也使用 3 行默认布局。"
        )
        form.addRow("每页基础行数", self._rows_spin)
        root.addLayout(form)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = FluentPushButton("取消", self)
        save = FluentPrimaryPushButton("保存并刷新", self)
        cancel.clicked.connect(self.reject)
        save.clicked.connect(self.accept)
        buttons.addWidget(cancel)
        buttons.addWidget(save)
        root.addLayout(buttons)

        self._global_defaults = global_defaults
        self._custom_draft = effective
        self._mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        self._set_values(global_defaults if mode == "global" else effective)
        self.adjustSize()
        if anchor is not None:
            point = anchor.mapToGlobal(QPoint(anchor.width() + 8, 0))
            self.move(point)

    def _set_values(self, settings: SubtitleLoadingSettings) -> None:
        self._gap_enabled.setChecked(settings.time_gap_section_enabled)
        self._gap_spin.setValue(settings.section_gap_ms)
        self._blank_enabled.setChecked(settings.blank_line_section_enabled)
        self._rows_spin.setValue(settings.rows_per_page)

    def _current_values(self) -> SubtitleLoadingSettings:
        return SubtitleLoadingSettings(
            time_gap_section_enabled=self._gap_enabled.isChecked(),
            section_gap_ms=self._gap_spin.value(),
            blank_line_section_enabled=self._blank_enabled.isChecked(),
            rows_per_page=self._rows_spin.value(),
        )

    def _on_mode_changed(self, _index: int) -> None:
        mode = str(self._mode_combo.currentData() or "global")
        if mode == "custom":
            self._set_values(self._custom_draft)
        else:
            self._custom_draft = self._current_values()
            self._set_values(self._global_defaults)

    def result_value(self) -> tuple[str, SubtitleLoadingSettings]:
        return (
            str(self._mode_combo.currentData() or "global"),
            self._current_values(),
        )


@dataclass
class ExtraSubtitleSource:
    """一个副字幕源（对标 N3 ``SourceLyricsInfos`` 的コーラス槽位）。"""

    name: str
    path: Path
    track: TimingTrack


@dataclass
class _WatchedSubtitleState:
    path: Path
    baseline: TimingTrack
    seen_digest: str
    missing_notified: bool = False


class _WindowEdgeGrip(QWidget):
    """无边框窗口的边缘/角落拖拽调整手柄。

    覆盖在窗口内容之上的透明细条。缩放为**手动实现**（按下记录起始几何，
    拖动按边计算新几何）：Windows 上 ``startSystemResize`` 对无边框窗口
    会返回成功但实际不进入缩放循环（缺 ``WS_THICKFRAME``），不可依赖。
    """

    _EDGE_CURSORS = {
        Qt.Edge.LeftEdge.value: Qt.CursorShape.SizeHorCursor,
        Qt.Edge.RightEdge.value: Qt.CursorShape.SizeHorCursor,
        Qt.Edge.TopEdge.value: Qt.CursorShape.SizeVerCursor,
        Qt.Edge.BottomEdge.value: Qt.CursorShape.SizeVerCursor,
        (Qt.Edge.TopEdge | Qt.Edge.LeftEdge).value: Qt.CursorShape.SizeFDiagCursor,
        (Qt.Edge.BottomEdge | Qt.Edge.RightEdge).value: Qt.CursorShape.SizeFDiagCursor,
        (Qt.Edge.TopEdge | Qt.Edge.RightEdge).value: Qt.CursorShape.SizeBDiagCursor,
        (Qt.Edge.BottomEdge | Qt.Edge.LeftEdge).value: Qt.CursorShape.SizeBDiagCursor,
    }

    def __init__(self, window: QWidget, edges) -> None:
        super().__init__(window)
        self._window = window
        self._edges = edges
        self._edge_bits = int(edges.value)
        self._drag_start: Optional[QPoint] = None
        self._start_geometry: Optional[QRect] = None
        cursor = self._EDGE_CURSORS.get(edges.value)
        if cursor is not None:
            self.setCursor(cursor)

    def mousePressEvent(self, event):  # noqa: N802
        is_expanded = getattr(self._window, "_is_expanded", self._window.isMaximized)
        if event.button() == Qt.MouseButton.LeftButton and not is_expanded():
            self._drag_start = event.globalPosition().toPoint()
            self._start_geometry = QRect(self._window.geometry())
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):  # noqa: N802
        if self._drag_start is None or self._start_geometry is None:
            super().mouseMoveEvent(event)
            return
        delta = event.globalPosition().toPoint() - self._drag_start
        geometry = QRect(self._start_geometry)
        hint = self._window.minimumSizeHint()
        min_w = max(self._window.minimumWidth(), hint.width(), 1)
        min_h = max(self._window.minimumHeight(), hint.height(), 1)
        if self._edge_bits & Qt.Edge.LeftEdge.value:
            geometry.setLeft(min(geometry.left() + delta.x(), geometry.right() - min_w + 1))
        if self._edge_bits & Qt.Edge.RightEdge.value:
            geometry.setRight(max(geometry.right() + delta.x(), geometry.left() + min_w - 1))
        if self._edge_bits & Qt.Edge.TopEdge.value:
            geometry.setTop(min(geometry.top() + delta.y(), geometry.bottom() - min_h + 1))
        if self._edge_bits & Qt.Edge.BottomEdge.value:
            geometry.setBottom(max(geometry.bottom() + delta.y(), geometry.top() + min_h - 1))
        self._window.setGeometry(geometry)
        event.accept()

    def mouseReleaseEvent(self, event):  # noqa: N802
        if self._drag_start is not None:
            self._drag_start = None
            self._start_geometry = None
            event.accept()
            return
        super().mouseReleaseEvent(event)


class PreviewPlayerWindow(QWidget):
    """独立预览窗口：只承载视频预览画面，形状跟随当前输出画布。"""

    userClosed = Signal()
    _TITLE_BAR_HEIGHT = 42
    _MIN_VIDEO_BOX = QSize(426, 240)
    _COLLAPSED_SIZE = QSize(220, 44)
    _COLLAPSED_CENTER_Y_RATIO = 0.70

    def __init__(self, owner: QWidget) -> None:
        super().__init__(
            owner,
            Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint,
        )
        self._owner = owner
        self._drag_origin: Optional[QPoint] = None
        self._suppress_control_show = False
        self._collapsed = False
        self._output_aspect = 16 / 9
        self._media_title = "字幕视频预览"
        self.setWindowTitle("字幕视频预览")
        self.setObjectName("SubtitlePreviewPlayerWindow")
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self._preview_panel = PreviewPanel(self)
        self._preview_frame = _AspectRatioBox(self._preview_panel, parent=self)
        self._preview_frame.setMouseTracking(True)
        self._preview_panel.setMouseTracking(True)
        self._install_video_interaction_filters()

        self._top_controls = QWidget(self)
        self._top_controls.setObjectName("PreviewTopControls")
        self._top_controls.setMouseTracking(True)
        top_layout = QHBoxLayout(self._top_controls)
        top_layout.setContentsMargins(12, 0, 8, 0)
        top_layout.setSpacing(8)
        self._title_label = QLabel("字幕视频预览", self._top_controls)
        self._title_label.setObjectName("PreviewTitleLabel")
        top_layout.addWidget(self._title_label, 1)

        self._transport_bar = TransportBar(self)
        self._transport_bar.setObjectName("PreviewTransportBar")
        self._bottom_controls = self._transport_bar
        transport_layout = self._transport_bar.layout()
        transport_layout.removeWidget(self._transport_bar._preview_quality_label)
        transport_layout.removeWidget(self._transport_bar._preview_quality_combo)
        self._transport_bar._preview_quality_label.setParent(self._top_controls)
        self._transport_bar._preview_quality_combo.setParent(self._top_controls)
        self._transport_bar._preview_quality_label.setFixedWidth(48)
        self._transport_bar._preview_quality_combo.setFixedSize(120, 28)
        self._transport_bar._preview_quality_combo.setObjectName(
            "PreviewQualityCombo"
        )
        top_layout.addWidget(self._transport_bar._preview_quality_label)
        top_layout.addWidget(self._transport_bar._preview_quality_combo)

        self._minimize_button = QPushButton("－", self._top_controls)
        self._maximize_button = QPushButton("□", self._top_controls)
        self._close_button = QPushButton("×", self._top_controls)
        for button in (self._minimize_button, self._maximize_button, self._close_button):
            button.setFixedSize(28, 28)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            top_layout.addWidget(button)
        self._minimize_button.clicked.connect(self._collapse_window)
        self._maximize_button.clicked.connect(self._toggle_maximized)
        self._close_button.clicked.connect(self.close)

        self._init_playback_shortcuts()

        self._hide_controls_timer = QTimer(self)
        self._hide_controls_timer.setSingleShot(True)
        self._hide_controls_timer.setInterval(2600)
        self._hide_controls_timer.timeout.connect(self._on_controls_idle_timeout)
        self._apply_player_transport_style()

        self._apply_minimum_window_size()

        # 无边框窗口的八向拖拽调整手柄（边 + 角），叠在最上层。
        edge = Qt.Edge
        self._edge_grips = [
            _WindowEdgeGrip(self, edge.LeftEdge),
            _WindowEdgeGrip(self, edge.RightEdge),
            _WindowEdgeGrip(self, edge.TopEdge),
            _WindowEdgeGrip(self, edge.BottomEdge),
            _WindowEdgeGrip(self, edge.TopEdge | edge.LeftEdge),
            _WindowEdgeGrip(self, edge.TopEdge | edge.RightEdge),
            _WindowEdgeGrip(self, edge.BottomEdge | edge.LeftEdge),
            _WindowEdgeGrip(self, edge.BottomEdge | edge.RightEdge),
        ]

        themed(
            self,
            lambda: (
                """
                #SubtitlePreviewPlayerWindow {
                    background: #15171A;
                }
                #PreviewTopControls {
                    background: rgba(0, 0, 0, 178);
                }
                #PreviewTitleLabel {
                    color: #F8FAFC;
                    font-size: 9.5pt;
                    font-family: "Microsoft YaHei UI";
                }
                #PreviewTopControls QPushButton {
                    background: transparent;
                    color: #FFFFFF;
                    border: none;
                    border-radius: 4px;
                    font-size: 13pt;
                }
                #PreviewTopControls QPushButton:hover {
                    background: rgba(255, 255, 255, 48);
                }
                #PreviewTopControls QPushButton:pressed {
                    background: rgba(255, 255, 255, 72);
                }
                #PreviewTopControls QComboBox#PreviewQualityCombo {
                    background: transparent;
                    color: rgba(255, 255, 255, 210);
                    border: 1px solid rgba(255, 255, 255, 36);
                    border-radius: 4px;
                    padding: 0 20px 0 6px;
                    text-align: left;
                    font-size: 9pt;
                    font-family: "Microsoft YaHei UI";
                }
                #PreviewTopControls QComboBox#PreviewQualityCombo:hover {
                    background: rgba(255, 255, 255, 18);
                    border-color: rgba(255, 255, 255, 64);
                }
                #PreviewTopControls QComboBox#PreviewQualityCombo:on {
                    background: rgba(255, 255, 255, 28);
                    border-color: rgba(255, 255, 255, 80);
                }
                #PreviewTopControls QComboBox#PreviewQualityCombo::drop-down {
                    width: 22px;
                    border: none;
                    background: transparent;
                }
                #PreviewTopControls QComboBox#PreviewQualityCombo QAbstractItemView {
                    color: rgba(255, 255, 255, 225);
                    background: #202225;
                    border: 1px solid rgba(255, 255, 255, 42);
                    outline: none;
                    selection-color: #FFFFFF;
                    selection-background-color: #34373B;
                }
                #PreviewTransportBar {
                    background: rgba(0, 0, 0, 0);
                    border-top: none;
                }
                #PreviewTransportBar QLabel {
                    color: #F8FAFC;
                }
                """
            ),
        )
        self._preview_frame.lower()
        self.show_controls()
        self.apply_workspace_geometry()

    @property
    def preview_panel(self) -> PreviewPanel:
        return self._preview_panel

    @property
    def transport_bar(self) -> TransportBar:
        return self._transport_bar

    def _install_video_interaction_filters(self) -> None:
        targets = [self._preview_frame, self._preview_panel]
        canvas = self._preview_panel.canvas
        targets.append(canvas)
        viewport = getattr(canvas, "viewport", lambda: None)()
        if viewport is not None:
            targets.append(viewport)
        for target in targets:
            target.installEventFilter(self)
            target.setMouseTracking(True)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802
        if self._video_interaction_target(watched):
            if event.type() == QEvent.Type.MouseButtonRelease:
                if (
                    event.button() == Qt.MouseButton.LeftButton
                    and self._video_area_contains(watched, event.position().toPoint())
                ):
                    self._toggle_playback()
                    event.accept()
                    return True
            elif event.type() == QEvent.Type.MouseButtonDblClick:
                if (
                    event.button() == Qt.MouseButton.LeftButton
                    and self._video_area_contains(watched, event.position().toPoint())
                ):
                    self._toggle_maximized()
                    self.show_controls()
                    event.accept()
                    return True
        return super().eventFilter(watched, event)

    def _video_interaction_target(self, watched: QObject) -> bool:
        if watched in {self._preview_frame, self._preview_panel}:
            return True
        canvas = self._preview_panel.canvas
        if watched is canvas:
            return True
        viewport = getattr(canvas, "viewport", lambda: None)()
        return watched is viewport

    def _video_area_contains(self, watched: QObject, pos: QPoint) -> bool:
        if self._collapsed or not self._preview_panel.is_populated():
            return False
        widget = watched if isinstance(watched, QWidget) else None
        if widget is None:
            return False
        window_pos = widget.mapTo(self, pos)
        frame_rect = QRect(
            self._preview_frame.mapTo(self, QPoint(0, 0)),
            self._preview_frame.size(),
        )
        return frame_rect.contains(window_pos)

    def min_video_size(self) -> QSize:
        """当前画布形状下的最小画面尺寸（16:9 时仍是原来的 426×240）。"""
        return fit_size_to_aspect(self._MIN_VIDEO_BOX, self._output_aspect)

    def _apply_minimum_window_size(self) -> None:
        min_video = self.min_video_size()
        self.setMinimumSize(
            QSize(
                min_video.width(),
                min_video.height() + self._TITLE_BAR_HEIGHT,
            )
        )

    def set_output_size(self, width: int, height: int) -> None:
        """跟随输出画布换形：非 16:9 的视频不再被补成 16:9 的预览画面。"""
        if width <= 0 or height <= 0:
            return
        aspect = max(float(width) / float(height), 0.1)
        if aspect == self._output_aspect:
            return
        self._output_aspect = aspect
        self._preview_frame.set_aspect_ratio(width, height)
        if self._collapsed:
            return
        self._apply_minimum_window_size()
        if self.isVisible() and not self._is_expanded():
            self.apply_workspace_geometry()
        self._layout_edge_grips()

    def apply_workspace_geometry(self) -> None:
        if self._collapsed:
            self._apply_collapsed_geometry()
            return
        workspace_size = self._owner.size()
        min_video = self.min_video_size()
        width = max(min_video.width(), workspace_size.width() // 2)
        video_height = max(
            min_video.height(), int(round(width / self._output_aspect))
        )
        height = video_height + self._TITLE_BAR_HEIGHT
        max_height = max(
            min_video.height() + self._TITLE_BAR_HEIGHT,
            workspace_size.height() // 2 + self._TITLE_BAR_HEIGHT,
        )
        if height > max_height:
            height = max_height
            video_height = max(
                min_video.height(), height - self._TITLE_BAR_HEIGHT
            )
            width = max(
                min_video.width(), int(round(video_height * self._output_aspect))
            )
        top_left = self._owner.mapToGlobal(QPoint(0, 0))
        self.setGeometry(QRect(top_left, QSize(width, height)))

    def _apply_collapsed_geometry(self) -> None:
        size = self._COLLAPSED_SIZE
        owner_size = self._owner.size()
        owner_top_left = self._owner.mapToGlobal(QPoint(0, 0))
        left = owner_top_left.x() + (owner_size.width() - size.width()) // 2
        center_y = owner_top_left.y() + round(
            owner_size.height() * self._COLLAPSED_CENTER_Y_RATIO
        )
        top = center_y - size.height() // 2
        self.setGeometry(left, top, size.width(), size.height())

    def show_near_workspace(self) -> None:
        if self._collapsed:
            self._restore_from_collapsed()
            return
        if self._is_expanded():
            self._restore_windowed()
        self.apply_workspace_geometry()
        self.show()
        self.show_controls()

    def set_media_title(self, path: Optional[Path]) -> None:
        self._media_title = path.name if path is not None else "字幕视频预览"
        if not self._collapsed:
            self._title_label.setText(self._media_title)

    def _init_playback_shortcuts(self) -> None:
        self._space_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Space), self)
        self._backward_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Z), self)
        self._forward_shortcut = QShortcut(QKeySequence(Qt.Key.Key_X), self)
        for shortcut in (
            self._space_shortcut,
            self._backward_shortcut,
            self._forward_shortcut,
        ):
            shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        self._space_shortcut.activated.connect(self._toggle_playback)
        self._backward_shortcut.activated.connect(lambda: self._seek_relative(-5_000))
        self._forward_shortcut.activated.connect(lambda: self._seek_relative(5_000))

    def _toggle_playback(self) -> None:
        self._transport_bar.toggle_play()
        self.show_controls()

    def _seek_relative(self, delta_ms: int) -> None:
        self._transport_bar.seek_relative(delta_ms)
        self.show_controls()

    def show_controls(self) -> None:
        if self._collapsed:
            self._hide_controls_timer.stop()
            self._top_controls.show()
            self._bottom_controls.hide()
            self._top_controls.raise_()
            return
        if self._suppress_control_show:
            return
        self._top_controls.show()
        self._bottom_controls.show()
        self._top_controls.raise_()
        self._bottom_controls.raise_()
        # 控制栏覆盖窗口上下沿；若最后提升的是控制栏，四个角的透明 resize
        # grip 就收不到鼠标事件。始终让边角手柄位于控制栏之上。
        self._raise_edge_grips()
        self._hide_controls_timer.start()

    def _on_controls_idle_timeout(self) -> None:
        if self._collapsed:
            self._top_controls.show()
            return
        self.hide_controls(force=False)

    def hide_controls(self, *, force: bool = False) -> None:
        if self._collapsed:
            self._hide_controls_timer.stop()
            self._top_controls.show()
            self._bottom_controls.hide()
            return
        if self.underMouse() and not force:
            self._hide_controls_timer.start()
            return
        self._hide_controls_timer.stop()
        self._suppress_control_show = True
        try:
            self._top_controls.setVisible(True)
            self._top_controls.raise_()
            self._bottom_controls.setVisible(False)
        finally:
            self._suppress_control_show = False

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        top_height = self.height() if self._collapsed else self._TITLE_BAR_HEIGHT
        video_top = 0 if self._collapsed else top_height
        self._preview_frame.setGeometry(
            0,
            video_top,
            self.width(),
            max(0, self.height() - video_top),
        )
        self._top_controls.setGeometry(0, 0, self.width(), top_height)
        self._bottom_controls.setGeometry(0, max(0, self.height() - 58), self.width(), 58)
        self._top_controls.raise_()
        self._bottom_controls.raise_()
        self._layout_edge_grips()

    def _layout_edge_grips(self) -> None:
        w, h = self.width(), self.height()
        margin = 6  # 边条厚度
        corner = 14  # 角块边长
        edge = Qt.Edge
        rects = {
            edge.LeftEdge.value: QRect(0, corner, margin, max(h - corner * 2, 0)),
            edge.RightEdge.value: QRect(w - margin, corner, margin, max(h - corner * 2, 0)),
            edge.TopEdge.value: QRect(corner, 0, max(w - corner * 2, 0), margin),
            edge.BottomEdge.value: QRect(corner, h - margin, max(w - corner * 2, 0), margin),
            (edge.TopEdge | edge.LeftEdge).value: QRect(0, 0, corner, corner),
            (edge.TopEdge | edge.RightEdge).value: QRect(w - corner, 0, corner, corner),
            (edge.BottomEdge | edge.LeftEdge).value: QRect(0, h - corner, corner, corner),
            (edge.BottomEdge | edge.RightEdge).value: QRect(w - corner, h - corner, corner, corner),
        }
        maximized = self._is_expanded()
        for grip in self._edge_grips:
            grip.setGeometry(rects[grip._edges.value])
            grip.setVisible(not maximized and not self._collapsed)
        self._raise_edge_grips()

    def _raise_edge_grips(self) -> None:
        if not hasattr(self, "_edge_grips"):
            return
        for grip in self._edge_grips:
            grip.raise_()

    def focusInEvent(self, event):  # noqa: N802
        super().focusInEvent(event)
        self.show_controls()

    def focusOutEvent(self, event):  # noqa: N802
        super().focusOutEvent(event)
        self._hide_controls_timer.start(900)

    def enterEvent(self, event):  # noqa: N802
        super().enterEvent(event)
        self.show_controls()

    def mouseMoveEvent(self, event):  # noqa: N802
        super().mouseMoveEvent(event)
        if self._drag_origin is not None:
            self.move(event.globalPosition().toPoint() - self._drag_origin)
        self.show_controls()

    def mousePressEvent(self, event):  # noqa: N802
        if (
            event.button() == Qt.MouseButton.LeftButton
            and event.position().y() <= self._top_controls.height()
        ):
            # 兜底命中：个别情况下（覆盖层/事件路由异常）标题栏按钮收不到点击，
            # 在窗口层按坐标直接分发，保证 最小化/最大化/关闭 永远可用。
            if self._dispatch_titlebar_button(event.position().toPoint()):
                event.accept()
                return
            if self._is_expanded():
                # 最大化状态下不允许手动拖动（会把窗口拖成"假最大化"状态）。
                event.accept()
                return
            self._drag_origin = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):  # noqa: N802
        # 顶栏双击 = 最大化/还原（与原生窗口一致）；避开按钮区域。
        if (
            event.button() == Qt.MouseButton.LeftButton
            and event.position().y() <= self._top_controls.height()
            and not self._titlebar_button_at(event.position().toPoint())
        ):
            self._toggle_maximized()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def _titlebar_button_at(self, pos: QPoint) -> Optional[QPushButton]:
        if not self._top_controls.isVisible():
            return None
        for button in (self._minimize_button, self._maximize_button, self._close_button):
            rect = QRect(button.mapTo(self, QPoint(0, 0)), button.size())
            if rect.contains(pos):
                return button
        return None

    def _dispatch_titlebar_button(self, pos: QPoint) -> bool:
        button = self._titlebar_button_at(pos)
        if button is None:
            return False
        button.click()
        return True

    def mouseReleaseEvent(self, event):  # noqa: N802
        self._drag_origin = None
        super().mouseReleaseEvent(event)

    def closeEvent(self, event):  # noqa: N802
        self._hide_controls_timer.stop()
        self._transport_bar.stop()
        self.userClosed.emit()
        super().closeEvent(event)

    def _is_expanded(self) -> bool:
        expanded = Qt.WindowState.WindowMaximized | Qt.WindowState.WindowFullScreen
        return bool(self.windowState() & expanded) or self.isMaximized() or self.isFullScreen()

    def is_collapsed(self) -> bool:
        return self._collapsed

    def _collapse_window(self) -> None:
        if self._collapsed:
            return
        self._collapsed = True
        self._hide_controls_timer.stop()
        self.setWindowState(Qt.WindowState.WindowNoState)
        self.showNormal()
        self.setMinimumSize(self._COLLAPSED_SIZE)
        self._preview_frame.hide()
        self._bottom_controls.hide()
        self._transport_bar._preview_quality_label.hide()
        self._transport_bar._preview_quality_combo.hide()
        self._minimize_button.hide()
        self._maximize_button.setToolTip("恢复预览窗口")
        self._title_label.setText("预览窗口")
        self.setWindowTitle("预览窗口")
        self._apply_collapsed_geometry()
        self._top_controls.show()
        self._top_controls.raise_()
        self._layout_edge_grips()
        self.show()
        self.raise_()

    def _restore_from_collapsed(self) -> None:
        if not self._collapsed:
            return
        self._collapsed = False
        self._apply_minimum_window_size()
        self._transport_bar._preview_quality_label.show()
        self._transport_bar._preview_quality_combo.show()
        self._minimize_button.show()
        self._maximize_button.setToolTip("")
        self._title_label.setText(self._media_title)
        self.setWindowTitle("字幕视频预览")
        self._preview_frame.show()
        self.showNormal()
        self.apply_workspace_geometry()
        self._layout_edge_grips()
        self.show_controls()

    def _restore_windowed(self) -> None:
        if self._collapsed:
            self._restore_from_collapsed()
            return
        self.setWindowState(Qt.WindowState.WindowNoState)
        self.showNormal()
        self.apply_workspace_geometry()
        self._layout_edge_grips()
        self.show_controls()

    def _toggle_maximized(self) -> None:
        if self._collapsed:
            self._restore_from_collapsed()
            return
        if self._is_expanded():
            self._restore_windowed()
        else:
            self.showMaximized()

    def _apply_player_transport_style(self) -> None:
        self._transport_bar.setFixedHeight(58)
        self._transport_bar.setStyleSheet(
            """
            #PreviewTransportBar {
                background: rgba(0, 0, 0, 0);
                border-top: none;
            }
            """
        )
        self._transport_bar._play_btn.setFixedSize(36, 36)
        self._transport_bar._play_btn.setStyleSheet(
            """
            QToolButton {
                background: transparent;
                color: #FFFFFF;
                border: none;
                border-radius: 18px;
                font-size: 18pt;
                font-weight: 700;
            }
            QToolButton:hover {
                background: rgba(255, 255, 255, 42);
            }
            QToolButton:pressed {
                background: rgba(255, 255, 255, 66);
            }
            """
        )
        for label in (
            self._transport_bar._timecode,
            self._transport_bar._fps_label,
            self._transport_bar._volume_label,
        ):
            label.setStyleSheet(
                """
                QLabel {
                    color: rgba(255, 255, 255, 210);
                    background: transparent;
                    font-family: "Consolas", "Courier New", monospace;
                    font-size: 9.5pt;
                }
                """
            )
        self._transport_bar._preview_quality_label.setStyleSheet(
            """
            QLabel {
                color: rgba(255, 255, 255, 160);
                background: transparent;
                font-family: "Microsoft YaHei UI";
                font-size: 9pt;
            }
            """
        )


_EXPORT_PREVIEW_DEFAULT_WIDTH = 640
_EXPORT_PREVIEW_MIN_WIDTH = 320


def _export_preview_width(
    view_size: QSize,
    device_pixel_ratio: float,
    output_width: int,
    output_height: int,
) -> int:
    """Return the fitted preview width in physical pixels."""
    safe_output_width = max(int(output_width), 1)
    fallback = min(safe_output_width, _EXPORT_PREVIEW_DEFAULT_WIDTH)
    if (
        view_size.width() <= 0
        or view_size.height() <= 0
        or output_width <= 0
        or output_height <= 0
        or not isfinite(device_pixel_ratio)
        or device_pixel_ratio <= 0
    ):
        return fallback
    fitted_logical_width = min(
        float(view_size.width()),
        float(view_size.height()) * output_width / output_height,
    )
    physical_width = int(round(fitted_logical_width * device_pixel_ratio))
    return min(safe_output_width, max(_EXPORT_PREVIEW_MIN_WIDTH, physical_width))


def _physical_preview_size(size: QSize, device_pixel_ratio: float) -> QSize:
    """Convert a logical widget size to a positive physical-pixel size."""
    dpr = device_pixel_ratio if isfinite(device_pixel_ratio) and device_pixel_ratio > 0 else 1.0
    return QSize(
        max(int(round(size.width() * dpr)), 1),
        max(int(round(size.height() * dpr)), 1),
    )


def _scaled_preview_pixmap(
    frame: QPixmap,
    logical_size: QSize,
    device_pixel_ratio: float,
) -> QPixmap:
    """Scale a frame for a logical widget while retaining physical pixels."""
    dpr = device_pixel_ratio if isfinite(device_pixel_ratio) and device_pixel_ratio > 0 else 1.0
    target_size = _physical_preview_size(logical_size, dpr)
    pixmap = frame.scaled(
        target_size,
        Qt.AspectRatioMode.KeepAspectRatioByExpanding,
        Qt.TransformationMode.SmoothTransformation,
    )
    if pixmap.size() != target_size:
        pixmap = pixmap.copy(
            max((pixmap.width() - target_size.width()) // 2, 0),
            max((pixmap.height() - target_size.height()) // 2, 0),
            target_size.width(),
            target_size.height(),
        )
    pixmap.setDevicePixelRatio(dpr)
    return pixmap


class _RecoverySaveWorker(QObject):
    saved = Signal(object, int, int, int, bool)
    failed = Signal(object, int, int, int, str)

    def __init__(
        self,
        path: Path,
        payload: dict,
        generation: int,
        revision: int,
        snapshot_id: int,
    ) -> None:
        super().__init__()
        self._path = path
        self._payload = payload
        self._generation = generation
        self._revision = revision
        self._snapshot_id = snapshot_id

    def run(self) -> None:
        try:
            try:
                written = save_recovery_project(self._path, self._payload)
            except (OSError, TypeError, ValueError) as exc:
                self.failed.emit(
                    self._path,
                    self._generation,
                    self._revision,
                    self._snapshot_id,
                    str(exc),
                )
                return
            self.saved.emit(
                self._path,
                self._generation,
                self._revision,
                self._snapshot_id,
                written,
            )
        finally:
            QThread.currentThread().quit()


class _RenderWorker(QObject):
    progressChanged = Signal(int, int)
    logMessage = Signal(str)
    finished = Signal(Path)
    cancelled = Signal(str)
    failed = Signal(str)

    def __init__(
        self,
        job: RenderJob,
        ffmpeg_dir: Optional[Path],
        preview_image_path: Optional[Path] = None,
        preview_width: Optional[int] = None,
    ) -> None:
        super().__init__()
        self._job = job
        self._ffmpeg_dir = ffmpeg_dir
        self._preview_image_path = preview_image_path
        self._preview_width = preview_width
        self._process: Optional[subprocess.Popen] = None
        self._cancel_requested = False

    def run(self) -> None:
        worker_log = logging.getLogger("krok_helper.subtitle_render.export")
        worker_log.info(
            "字幕视频导出开始 output=%s size=%sx%s fps=%s",
            self._job.output_path,
            self._job.width,
            self._job.height,
            self._job.fps,
        )

        def emit_log(message: str) -> None:
            worker_log.info("字幕视频导出: %s", message)
            self.logMessage.emit(message)

        try:
            output = render_subtitle_video(
                self._job,
                ffmpeg_dir=self._ffmpeg_dir,
                logger=emit_log,
                should_cancel=self.should_cancel,
                on_progress=self.progressChanged.emit,
                on_process_started=self._set_process,
                preview_image_path=self._preview_image_path,
                preview_width=self._preview_width,
            )
        except ExportCancelled as exc:
            worker_log.info("字幕视频导出取消: %s", exc)
            self.cancelled.emit(str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            worker_log.exception("字幕视频导出失败")
            self.failed.emit(str(exc))
            return
        worker_log.info("字幕视频导出完成 output=%s", output)
        self.finished.emit(output)

    def cancel(self) -> None:
        self._cancel_requested = True
        process = self._process
        if process is not None:
            terminate_process(process)

    def should_cancel(self) -> bool:
        return self._cancel_requested

    def _set_process(self, process: Optional[subprocess.Popen]) -> None:
        self._process = process


class _ExportMonitorView(QLabel):
    """导出预览画面（仿 N3 出力预览）：保持纵横比缩放显示最近合成帧。

    无帧时显示占位文案；有帧后 resize 会用原图重新缩放，避免累积模糊。
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._frame: Optional[QPixmap] = None
        self.setMinimumSize(1, 1)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        themed(
            self,
            lambda: (
                f"background: {stage_bg()}; border-radius: 8px;"
                f" color: {palette().text_hint}; font-size: 10pt;"
            ),
        )
        self.clear_frame()

    def set_frame(self, image: QImage) -> None:
        self._frame = QPixmap.fromImage(image)
        self.setText("")
        self._rescale()

    def clear_frame(self) -> None:
        self._frame = None
        self.setPixmap(QPixmap())
        self.setText("准备开始导出")

    def _rescale(self) -> None:
        if self._frame is None or self._frame.isNull():
            return
        self.setPixmap(
            _scaled_preview_pixmap(
                self._frame,
                self.size(),
                float(self.devicePixelRatioF()),
            )
        )

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._rescale()


def _format_eta_seconds(seconds: float) -> str:
    """导出剩余时间的短文案：1 小时 5 分 / 3 分 20 秒 / 45 秒。"""
    total = max(int(round(seconds)), 0)
    if total >= 3600:
        return f"{total // 3600} 小时 {total % 3600 // 60} 分"
    if total >= 60:
        return f"{total // 60} 分 {total % 60} 秒"
    return f"{total} 秒"


def _format_elapsed_seconds(seconds: float) -> str:
    """Format a completed export duration with minutes and seconds."""
    total = max(int(round(seconds)), 0)
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours} 小时 {minutes} 分 {seconds} 秒"
    return f"{minutes} 分 {seconds} 秒"


def _format_warning_lines(warnings: list) -> str:
    """把余白警告压成「第 1、3 行」式的短文案，最多点名 4 行。"""
    numbers = [str(w.line_index + 1) for w in warnings[:4]]
    text = f"第 {'、'.join(numbers)} 行"
    if len(warnings) > 4:
        text += f" 等 {len(warnings)} 行"
    return text


class SubtitleRenderWindow(QWidget):
    """字幕视频渲染模块主 widget。"""

    projectStateChanged = Signal(object)
    _tracksViewWindowsReady = Signal(int, object)
    _embedded: bool = False

    def __init__(
        self,
        embedded: bool = False,
        settings_provider: Optional[Any] = None,
        workflow_context: "SubtitleVideoSink | None" = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._embedded = embedded
        self._settings_provider = settings_provider
        self._workflow_context = workflow_context

        self._timing_track: Optional[TimingTrack] = None
        # 字幕轨道编辑的撤销/重做栈：兼容显示窗口四元组与逐行动画批量命令。
        self._undo_stack: list[tuple] = []
        self._redo_stack: list[tuple] = []
        self._extra_sources: list[ExtraSubtitleSource] = []
        """副字幕源（N3 多歌词文件，如コーラス轨）：与主字幕同帧叠绘。"""
        self._active_source_index = 0
        """歌词列表当前显示的源：0 = 主字幕，k >= 1 = ``_extra_sources[k-1]``。"""
        self._title_source_active = False
        """左侧列表当前是否显示末位的特殊「标题」源。"""
        self._subtitle_path: Optional[Path] = None
        self._video_path: Optional[Path] = None
        self._video_info: Optional[MediaInfo] = None
        self._background_source: Optional[BackgroundSource] = None
        self._audio_menu_actions: list[Action] = []
        self._audio_path: Optional[Path] = None
        self._audio_info: Optional[MediaInfo] = None
        self._style: Style = Style()
        self._app_default_style: Style = Style()
        self._subtitle_loading_defaults = SubtitleLoadingSettings()
        self._style_presets: dict[str, StylePreset] = {}
        self._screen_settings: ScreenSettings = ScreenSettings()
        self._selected_scheme_key = "global"
        self._layout_assignment_preference: Optional[dict[str, object]] = None
        self._export_dir_mode = EXPORT_DIR_SOURCE_VIDEO
        self._export_custom_dir = ""
        self._export_name_template = DEFAULT_EXPORT_NAME_TEMPLATE
        self._project_path: Optional[Path] = None
        self._project_dirty = False
        self._project_saving = False
        self._project_save_error: Optional[str] = None
        self._project_generation = 0
        self._project_revision = 0
        self._saved_revision = 0
        self._project_disk_revision: Optional[ProjectFileRevision] = None
        self._missing_resources: tuple[tuple[str, Path], ...] = ()
        self._unresolved_resource_labels: set[str] = set()
        self._missing_resource_source_data: Optional[dict] = None
        self._last_logged_project_state: Optional[tuple[object, ...]] = None
        self._loading_project = False
        self._syncing_screen_controls = False
        self._auto_save_enabled = True
        self._auto_save_interval_minutes = DEFAULT_AUTO_SAVE_INTERVAL_MINUTES
        self._project_backup_count = DEFAULT_PROJECT_BACKUP_COUNT
        self._auto_save_thread: Optional[QThread] = None
        self._auto_save_worker: Optional[_RecoverySaveWorker] = None
        self._auto_save_pending = False
        self._last_auto_save_error = ""
        self._render_thread: Optional[QThread] = None
        self._render_worker: Optional[_RenderWorker] = None
        self._watch_primary_subtitle_source = False
        self._source_watch_states: dict[str, _WatchedSubtitleState] = {}
        self._pending_source_reload_keys: set[str] = set()
        self._source_reload_retries: dict[str, int] = {}
        self._source_watcher = QFileSystemWatcher(self)
        self._source_watcher.fileChanged.connect(self._on_subtitle_source_file_changed)
        self._source_watcher.directoryChanged.connect(
            self._on_subtitle_source_directory_changed
        )
        self._source_change_timer = QTimer(self)
        self._source_change_timer.setSingleShot(True)
        self._source_change_timer.setInterval(450)
        self._source_change_timer.timeout.connect(self._process_subtitle_source_changes)
        self._tracks_window_refresh_timer = QTimer(self)
        self._tracks_window_refresh_timer.setSingleShot(True)
        self._tracks_window_refresh_timer.setInterval(120)
        self._tracks_window_refresh_timer.timeout.connect(
            self._refresh_tracks_view_windows_async
        )
        # 整轨显示窗口重算（真实工程实测约 600ms）挪到后台线程，结果经队列信号回
        # GUI 线程。代号用于丢弃过期结果：连续调参时只有最后一次算得的窗口才作数。
        self._tracks_window_generation = 0
        self._tracks_window_worker_busy = False
        self._tracks_window_rerun_pending = False
        self._tracksViewWindowsReady.connect(
            self._on_tracks_view_windows_ready, Qt.ConnectionType.QueuedConnection
        )
        self._project_deferred_loads: list[tuple[str, object]] = []
        self._project_deferred_load_generation = 0
        self._defer_project_assets = False
        self._project_deferred_load_timer = QTimer(self)
        self._project_deferred_load_timer.setSingleShot(True)
        self._project_deferred_load_timer.timeout.connect(
            self._process_project_deferred_load
        )
        self._preview_window_requested = False
        self._preview_reposition_on_next_show = True
        self._closing_window = False
        self._suppress_next_render_command_log = False
        # 左右余白检查：属性面板每个 SpinBox tick 都会触发样式变更，
        # 用单发定时器合并成一次检查，提示只在结果变化时弹出。
        self._margin_check_timer = QTimer(self)
        self._margin_check_timer.setSingleShot(True)
        self._margin_check_timer.setInterval(400)
        self._margin_check_timer.timeout.connect(self._check_layout_margins)
        self._last_margin_warning_key = ""
        self._layout_issues: list[_LayoutIssue] = []
        self._layout_issues_dialog: Optional[_LayoutIssuesDialog] = None
        # 歌词 / 属性面板分割比例：默认 4:6，用户拖动后记忆。拖动过程中
        # splitterMoved 连续触发，用单发定时器合并成一次落盘。
        self._preview_splitter_ratio = 0.4
        self._splitter_save_timer = QTimer(self)
        self._splitter_save_timer.setSingleShot(True)
        self._splitter_save_timer.setInterval(400)
        self._splitter_save_timer.timeout.connect(self._save_persisted_state)
        # 应用级偏好落盘：一次 _save_persisted_state 要读一遍 settings.json、把整份
        # AppSettings 重新序列化再原子写回。属性面板每提交一次样式都同步走一趟磁盘，
        # 编辑手感直接被拖垮。改成脏标记 + 空闲定时器，真正的写在停手后 / 隐藏 /
        # 关闭 / 退出时发生。
        self._persisted_state_dirty = False
        self._persisted_state_save_timer = QTimer(self)
        self._persisted_state_save_timer.setSingleShot(True)
        self._persisted_state_save_timer.setInterval(_PERSISTED_STATE_SAVE_DEBOUNCE_MS)
        self._persisted_state_save_timer.timeout.connect(
            self._flush_persisted_state_save
        )
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self._flush_persisted_state_save)
        self._load_persisted_state()
        self._auto_save_timer = QTimer(self)
        self._auto_save_timer.setSingleShot(True)
        self._auto_save_timer.setInterval(AUTO_SAVE_DEBOUNCE_MS)
        self._auto_save_timer.timeout.connect(self._start_recovery_auto_save)
        self._periodic_auto_save_timer = QTimer(self)
        self._periodic_auto_save_timer.timeout.connect(self._start_recovery_auto_save)
        self._apply_auto_save_timer_config()

        themed(
            self,
            lambda: f"SubtitleRenderWindow {{ background: {palette().shell_bg}; }}",
        )

        self._init_layout()
        self._init_shortcuts()

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        if hasattr(self, "_preview_window"):
            self._preview_window.apply_workspace_geometry()

    def moveEvent(self, event):  # noqa: N802
        super().moveEvent(event)
        if hasattr(self, "_preview_window"):
            self._preview_window.apply_workspace_geometry()

    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        QTimer.singleShot(0, self._sync_preview_window_visibility)

    def hideEvent(self, event):  # noqa: N802
        self._hide_preview_window_for_context()
        # 切走工作区 = 一次自然的空闲点，把欠着的偏好写掉。
        self._flush_persisted_state_save()
        super().hideEvent(event)

    def closeEvent(self, event):  # noqa: N802
        self._closing_window = True
        self._flush_persisted_state_save()
        self._stop_auto_save_runtime(wait=True)
        if self._layout_issues_dialog is not None:
            self._layout_issues_dialog.close()
            self._layout_issues_dialog = None
        if hasattr(self, "_preview_window"):
            self._preview_window.close()
        super().closeEvent(event)

    # ------------------------------------------------------------------ layout

    def _init_layout(self) -> None:
        # 主布局：共用项目命令栏 + 内容区。工作区导航放在命令栏正中间，
        # 不再单独占用字幕轨道下方的高度。
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # QStackedWidget 承载各页内容
        self._stack = QStackedWidget(self)
        self._preview_tab = self._make_preview_tab()
        self._export_tab = self._make_export_tab()
        self._stack.addWidget(self._preview_tab)
        self._stack.addWidget(self._export_tab)
        self._stack.currentChanged.connect(self._on_workspace_tab_changed)

        self._project_bar = self._make_project_bar()
        # Compatibility aliases: the command bar is now shared by both pages.
        self._preview_project_bar = self._project_bar
        self._export_project_bar = self._project_bar
        root.addWidget(self._project_bar)
        root.addWidget(self._stack, 1)

        persisted = self._load_subtitle_settings()
        output = persisted.get("output") if isinstance(persisted.get("output"), dict) else {}
        self._apply_output_settings(output)
        self._remember_local_export_defaults()
        self._set_export_screen_controls(self._screen_settings)
        self._sync_preview_output_size()
        self._connect_project_output_signals()
        self._export_width_spin.valueChanged.connect(self._sync_preview_output_size)
        self._export_height_spin.valueChanged.connect(self._sync_preview_output_size)
        self._export_width_spin.valueChanged.connect(self._on_export_screen_changed)
        self._export_height_spin.valueChanged.connect(self._on_export_screen_changed)
        self._export_fps_combo.currentIndexChanged.connect(self._on_export_screen_changed)
        self._export_width_spin.valueChanged.connect(self._refresh_export_format_label)
        self._export_height_spin.valueChanged.connect(self._refresh_export_format_label)
        self._export_fps_combo.currentIndexChanged.connect(
            self._refresh_export_format_label
        )
        self._refresh_export_format_label()

        self._bottom_navigation.setCurrentItem("preview")
        self._stack.setCurrentIndex(0)
        self._refresh_project_title()

    def _connect_project_output_signals(self) -> None:
        """Connect user-editable project output fields after initial state loading."""
        self._export_encoder_combo.currentIndexChanged.connect(
            self._on_output_settings_changed
        )
        self._export_codec_combo.currentIndexChanged.connect(
            self._on_output_settings_changed
        )
        self._export_preset_combo.currentIndexChanged.connect(
            self._on_output_settings_changed
        )
        self._export_crf_spin.valueChanged.connect(self._on_output_settings_changed)
        self._export_name_edit.textEdited.connect(self._on_output_settings_changed)
        self._export_render_workers_combo.currentIndexChanged.connect(
            self._on_render_workers_changed
        )
        self._gpu_preview_check.toggled.connect(self._on_gpu_preview_changed)
        self._gpu_export_check.toggled.connect(self._on_gpu_export_changed)

    def _switch_tab(self, key: str) -> None:
        idx = 0 if key == "preview" else 1
        self._stack.setCurrentIndex(idx)
        self._bottom_navigation.setCurrentItem(key)

    def _on_workspace_tab_changed(self, _index: int) -> None:
        self._sync_playback_shortcut_scope()
        self._sync_preview_window_visibility()

    def _playback_shortcuts_allowed(self) -> bool:
        return bool(
            hasattr(self, "_stack")
            and hasattr(self, "_preview_tab")
            and self._stack.currentWidget() is self._preview_tab
        )

    def _sync_playback_shortcut_scope(self) -> None:
        if hasattr(self, "_space_shortcut"):
            self._space_shortcut.setEnabled(self._playback_shortcuts_allowed())

    def _toggle_playback_from_shortcut(self) -> None:
        if not self._playback_shortcuts_allowed():
            return
        self._transport_bar.toggle_play()

    def _preview_window_context_allowed(self) -> bool:
        return bool(
            not self._closing_window
            and self.isVisible()
            and self._stack.currentWidget() is self._preview_tab
            and self._render_thread is None
        )

    def _hide_preview_window_for_context(self) -> None:
        """Pause and hide without treating the action as a user close."""
        if not hasattr(self, "_preview_window"):
            return
        self._transport_bar.pause()
        if self._preview_window.isVisible():
            self._preview_window.hide()

    def _sync_preview_window_visibility(self) -> None:
        """Keep the top-level preview inside the preview-tab lifecycle."""
        if not hasattr(self, "_preview_window"):
            return
        should_show = bool(
            self._preview_window_requested
            and self._preview_window_context_allowed()
        )
        if not should_show:
            self._hide_preview_window_for_context()
            return
        if self._preview_window.isVisible():
            return
        if self._preview_reposition_on_next_show:
            self._preview_window.show_near_workspace()
            self._preview_reposition_on_next_show = False
        else:
            self._preview_window.show()
            self._preview_window.show_controls()

    def _request_preview_window(self) -> None:
        self._preview_window_requested = True
        self._sync_preview_window_visibility()

    def _on_preview_window_user_closed(self) -> None:
        if self._closing_window:
            return
        self._preview_window_requested = False
        self._preview_reposition_on_next_show = True

    # ----------------------------------------------------------- 项目文件（A11）

    def _make_project_bar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("SrProjectBar")
        themed(bar, lambda: "#SrProjectBar { background: transparent; }")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(24, 4, 24, 0)
        layout.setSpacing(8)

        left = QWidget(bar)
        left.setObjectName("SrProjectBarLeft")
        themed(left, lambda: "#SrProjectBarLeft { background: transparent; }")
        left_layout = QHBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(8)
        self._project_bar_left = left

        # 「文件管理 ▾」单个下拉，菜单含 新建/打开/保存/另存为（仿 SUG，省横向空间）。
        self._file_menu_btn = DropDownPushButton(FIF.FOLDER, "文件管理")
        self._file_menu_btn.setFixedHeight(30)
        menu = RoundMenu(parent=self._file_menu_btn)
        menu.addAction(Action(FIF.ADD, "新建", triggered=self._new_project))
        menu.addAction(Action(FIF.FOLDER, "打开", triggered=self._open_project))
        self._save_project_action = Action(FIF.SAVE, "保存", triggered=self._save_project)
        self._save_project_as_action = Action(
            FIF.SAVE_AS, "另存为", triggered=self._save_project_as
        )
        menu.addAction(self._save_project_action)
        menu.addAction(self._save_project_as_action)
        menu.addSeparator()
        menu.addAction(
            Action(FIF.HISTORY, "保存与备份设置…", triggered=self._open_auto_save_settings)
        )
        menu.addAction(
            Action(FIF.FOLDER, "打开备份目录", triggered=self._open_project_backup_directory)
        )
        menu.addAction(
            Action("刷新素材状态", triggered=self._refresh_missing_resource_status)
        )
        menu.addSeparator()
        menu.addAction(Action(FIF.DOWNLOAD, "导入 N3 项目", triggered=self._import_n3_project))
        self._file_menu_btn.setMenu(menu)
        left_layout.addWidget(self._file_menu_btn)

        self._background_menu_btn = DropDownPushButton("添加背景素材")
        self._background_menu_btn.setFixedHeight(30)
        background_menu = RoundMenu(parent=self._background_menu_btn)
        background_menu.addAction(Action("背景视频…", triggered=self._browse_video))
        background_menu.addAction(Action("静态图片…", triggered=self._browse_background_image))
        background_menu.addAction(Action("图片序列首帧…", triggered=self._browse_background_sequence))
        background_menu.addAction(Action("纯色背景…", triggered=self._choose_solid_background))
        background_menu.addSeparator()
        audio_action = Action("独立音频…", triggered=self._browse_audio)
        background_menu.addAction(audio_action)
        self._audio_menu_actions.append(audio_action)
        self._background_menu_btn.setMenu(background_menu)
        left_layout.addWidget(self._background_menu_btn)

        # 项目名：超长用 … 截断（完整名放 tooltip）。
        self._project_name_label = QLabel("")
        self._project_name_label.setMaximumWidth(260)
        themed(
            self._project_name_label,
            lambda: f"color: {palette().text_secondary}; font-size: 9.5pt;",
        )
        left_layout.addWidget(self._project_name_label)

        layout.addWidget(left)
        layout.addStretch(1)

        self._bottom_navigation = WorkspaceSwitcher(bar)
        self._nav_btns: dict[str, QWidget] = {}
        for key, text, icon in [
            ("preview", "预览", FIF.VIEW),
            ("export", "导出", FIF.VIDEO),
        ]:
            btn = self._bottom_navigation.addItem(
                key,
                text,
                onClick=lambda _checked=False, k=key: self._switch_tab(k),
                icon=icon,
            )
            self._nav_btns[key] = btn
        layout.addWidget(self._bottom_navigation)
        layout.addStretch(1)

        # Match the left controls on the right so navigation is centered in the
        # entire command bar, not just in the remaining horizontal space.
        self._project_bar_right_balance = QWidget(bar)
        self._project_bar_right_balance.setFixedWidth(left.sizeHint().width())
        layout.addWidget(self._project_bar_right_balance)
        return bar

    def _balance_project_bar(self) -> None:
        if not hasattr(self, "_project_bar_left"):
            return
        self._project_bar_left.layout().invalidate()
        self._project_bar_right_balance.setFixedWidth(
            self._project_bar_left.sizeHint().width()
        )

    def _make_preview_window_button(self, parent: QWidget) -> FluentPushButton:
        """Create the preview-only entry anchored above the preview workspace."""
        button = FluentPushButton("预览窗口", parent)
        button.setFixedHeight(30)
        button.setToolTip("打开 / 唤起字幕预览窗口")
        button.clicked.connect(self._show_preview_window)
        return button

    def _make_layout_issues_button(self, parent: QWidget) -> FluentToolButton:
        """Create the persistent entry for current lyrics layout issues."""
        button = FluentToolButton(_layout_issue_icon(), parent)
        button.setFixedWidth(34)
        button.setIconSize(QSize(20, 20))
        button.setToolTip("当前歌词问题")
        button.setAccessibleName("当前歌词问题")
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.clicked.connect(self._show_layout_issues)
        button.hide()
        return button

    def _show_preview_window(self) -> None:
        if not hasattr(self, "_preview_window"):
            return
        self._request_preview_window()
        if self._preview_window.isVisible():
            if self._preview_window.is_collapsed():
                self._preview_window._restore_from_collapsed()
            self._preview_window.raise_()
            self._preview_window.activateWindow()

    def _refresh_project_title(self) -> None:
        if not hasattr(self, "_project_name_label"):
            return
        state = self.project_state()
        full = state.status_text() or state.display_name
        metrics = self._project_name_label.fontMetrics()
        elided = metrics.elidedText(
            full, Qt.TextElideMode.ElideRight, self._project_name_label.maximumWidth()
        )
        self._project_name_label.setText(elided)
        self._project_name_label.setToolTip(full if elided != full else "")
        self._balance_project_bar()
        if hasattr(self, "_save_project_action"):
            idle = not state.saving and not state.exporting
            self._save_project_action.setEnabled(
                bool(state.has_project and state.dirty and idle)
            )
            self._save_project_as_action.setEnabled(bool(state.has_project and idle))
        diagnostic_state = (
            state.has_project,
            state.path is not None,
            state.dirty,
            state.saving,
            state.save_error is not None,
            state.exporting,
            state.recovery_path is not None,
            len(state.missing_resources),
        )
        if diagnostic_state != self._last_logged_project_state:
            logging.getLogger(__name__).info(
                "字幕项目状态变化: has_project=%s named=%s dirty=%s "
                "saving=%s save_failed=%s exporting=%s recovery=%s missing=%d",
                *diagnostic_state,
            )
            self._last_logged_project_state = diagnostic_state
        self.projectStateChanged.emit(state)

    def project_state(self) -> SubtitleProjectState:
        """Return a stable project-state snapshot for the host application."""
        path = self._project_path
        has_project = bool(
            path is not None
            or self._project_dirty
            or self._timing_track is not None
            or self._background_source is not None
            or self._audio_path is not None
            or self._extra_sources
        )
        recovery_path = self._recovery_path() if self._project_dirty else None
        return SubtitleProjectState(
            display_name=path.name if path is not None else "未命名项目",
            path=path,
            has_project=has_project,
            dirty=bool(self._project_dirty),
            saving=bool(self._project_saving),
            save_error=self._project_save_error,
            exporting=self._render_thread is not None,
            recovery_path=recovery_path if recovery_path and recovery_path.is_file() else None,
            missing_resources=self._missing_resources,
        )

    def has_unsaved_changes(self) -> bool:
        """Public embedding API used by the host close coordinator."""
        return self.project_state().dirty

    def trigger_save(self) -> bool:
        """Public embedding API; return False when saving fails or is cancelled."""
        return self._save_project()

    def discard_unsaved(self) -> None:
        """Acknowledge that the current dirty state is intentionally discarded."""
        self._auto_save_timer.stop()
        self._auto_save_pending = False
        self._wait_for_recovery_worker()
        if self._project_dirty:
            try:
                backup = save_discarded_project_backup(
                    self._backup_root(),
                    self._current_project_data(),
                    source_project_path=self._project_path,
                    retention_days=DISCARDED_BACKUP_RETENTION_DAYS,
                )
                logging.getLogger(__name__).info(
                    "已保留字幕项目已放弃修改备份: named=%s",
                    self._project_path is not None,
                )
            except (OSError, TypeError, ValueError) as exc:
                logging.getLogger(__name__).warning(
                    "保留字幕项目已放弃修改备份失败: %s", exc
                )
                InfoBar.warning(
                    title="紧急备份失败",
                    content=str(exc),
                    parent=self,
                    position=InfoBarPosition.BOTTOM_RIGHT,
                    duration=5000,
                )
        self._set_project_dirty(False)
        self._cleanup_recovery_file()

    def is_busy(self) -> bool:
        """Return whether a render export is still running."""
        return self._render_thread is not None

    def _set_project_dirty(self, dirty: bool) -> None:
        was_dirty = self._project_dirty
        self._project_dirty = bool(dirty)
        if dirty and not was_dirty:
            self._project_revision += 1
        if dirty or not self._project_saving:
            self._project_save_error = None
        if dirty:
            self._schedule_recovery_auto_save()
        elif hasattr(self, "_auto_save_timer"):
            self._auto_save_timer.stop()
        self._refresh_project_title()

    def _mark_project_dirty(self) -> None:
        if self._loading_project:
            return
        was_dirty = self._project_dirty
        had_save_error = self._project_save_error is not None
        self._project_revision += 1
        self._project_dirty = True
        self._project_save_error = None
        self._schedule_recovery_auto_save()
        if not was_dirty or had_save_error:
            self._refresh_project_title()

    def _begin_project_generation(self) -> None:
        """Invalidate recovery jobs belonging to the previously loaded project."""
        self._project_generation += 1
        self._project_revision = 0
        self._saved_revision = 0
        self._project_disk_revision = None
        self._missing_resources = ()
        self._unresolved_resource_labels = set()
        self._missing_resource_source_data = None
        self._project_deferred_loads = []
        if hasattr(self, "_project_deferred_load_timer"):
            self._project_deferred_load_timer.stop()
        self._auto_save_pending = False
        if hasattr(self, "_auto_save_timer"):
            self._auto_save_timer.stop()

    def _open_auto_save_settings(self) -> None:
        dialog = _AutoSaveSettingsDialog(
            self._auto_save_enabled,
            self._auto_save_interval_minutes,
            self._project_backup_count,
            self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        enabled, interval, backup_count = dialog.selection()
        self._configure_auto_save(
            enabled, interval, backup_count=backup_count, persist=True
        )
        InfoBar.success(
            title="自动保存设置已更新",
            content=(
                f"已启用，每 {interval} 分钟保存一次恢复快照；"
                f"手动保存保留 {backup_count} 份历史备份。"
                if enabled
                else f"已关闭自动保存；手动保存保留 {backup_count} 份历史备份。"
            ),
            parent=self,
            position=InfoBarPosition.BOTTOM_RIGHT,
            duration=2500,
        )

    def _configure_auto_save(
        self,
        enabled: bool,
        interval_minutes: int,
        *,
        backup_count: Optional[int] = None,
        persist: bool,
    ) -> None:
        self._auto_save_enabled = bool(enabled)
        self._auto_save_interval_minutes = max(1, min(60, int(interval_minutes)))
        if backup_count is not None:
            self._project_backup_count = max(1, min(20, int(backup_count)))
        self._apply_auto_save_timer_config()
        if persist:
            self._save_persisted_state()
        if self._auto_save_enabled and self._project_dirty:
            self._schedule_recovery_auto_save()

    def _apply_auto_save_timer_config(self) -> None:
        if not hasattr(self, "_periodic_auto_save_timer"):
            return
        self._periodic_auto_save_timer.setInterval(
            self._auto_save_interval_minutes * 60 * 1000
        )
        if self._auto_save_enabled:
            self._periodic_auto_save_timer.start()
        else:
            self._periodic_auto_save_timer.stop()
            self._auto_save_timer.stop()

    def _schedule_recovery_auto_save(self) -> None:
        if (
            self._auto_save_enabled
            and not self._loading_project
            and hasattr(self, "_auto_save_timer")
        ):
            self._auto_save_timer.start()

    def _recovery_payload_snapshot(self) -> tuple[dict, int]:
        snapshot_id = time.time_ns()
        payload = deepcopy(self._current_project_data())
        payload["recovery"] = {
            "source_project_path": str(self._project_path) if self._project_path else None,
            "created_at_unix": time.time(),
            "snapshot_id": snapshot_id,
            "project_generation": self._project_generation,
            "project_revision": self._project_revision,
        }
        return payload, snapshot_id

    def _start_recovery_auto_save(self) -> None:
        if not self._auto_save_enabled or not self._project_dirty or self._loading_project:
            return
        if self._auto_save_thread is not None:
            self._auto_save_pending = True
            return
        payload, snapshot_id = self._recovery_payload_snapshot()
        path = self._recovery_path()
        generation = self._project_generation
        revision = self._project_revision
        worker = _RecoverySaveWorker(
            path,
            payload,
            generation,
            revision,
            snapshot_id,
        )
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.saved.connect(self._on_recovery_auto_save_success)
        worker.failed.connect(self._on_recovery_auto_save_failure)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(self._finish_recovery_auto_save)
        self._auto_save_worker = worker
        self._auto_save_thread = thread
        thread.start()

    def _on_recovery_auto_save_success(
        self,
        path: Path,
        generation: int,
        revision: int,
        snapshot_id: int,
        _written: bool,
    ) -> None:
        self._last_auto_save_error = ""
        if generation != self._project_generation or revision <= self._saved_revision:
            self._cleanup_recovery_snapshot(path, snapshot_id)
            return
        self._refresh_project_title()

    def _on_recovery_auto_save_failure(
        self,
        _path: Path,
        generation: int,
        _revision: int,
        _snapshot_id: int,
        error: str,
    ) -> None:
        logging.getLogger(__name__).warning("字幕项目自动保存失败: %s", error)
        if generation != self._project_generation or error == self._last_auto_save_error:
            return
        self._last_auto_save_error = error
        InfoBar.warning(
            title="自动保存失败",
            content=error,
            parent=self,
            position=InfoBarPosition.BOTTOM_RIGHT,
            duration=5000,
        )

    def _finish_recovery_auto_save(self) -> None:
        self._auto_save_thread = None
        self._auto_save_worker = None
        if self._auto_save_pending:
            self._auto_save_pending = False
            if self._project_dirty and self._auto_save_enabled:
                QTimer.singleShot(0, self._start_recovery_auto_save)

    def _stop_auto_save_runtime(self, *, wait: bool) -> None:
        if hasattr(self, "_auto_save_timer"):
            self._auto_save_timer.stop()
        if hasattr(self, "_periodic_auto_save_timer"):
            self._periodic_auto_save_timer.stop()
        self._auto_save_pending = False
        if wait:
            self._wait_for_recovery_worker()

    def _wait_for_recovery_worker(self) -> bool:
        thread = self._auto_save_thread
        if thread is None or not thread.isRunning():
            return True
        if thread.wait(AUTO_SAVE_THREAD_WAIT_MS):
            return True
        logging.getLogger(__name__).warning("等待字幕项目自动保存线程退出超时")
        return False

    @staticmethod
    def _cleanup_recovery_snapshot(path: Path, snapshot_id: int) -> None:
        try:
            data = load_render_project(path)
            recovery = data.get("recovery")
            current_id = (
                int(recovery.get("snapshot_id") or 0)
                if isinstance(recovery, dict)
                else 0
            )
            if current_id == snapshot_id:
                path.unlink(missing_ok=True)
        except (OSError, TypeError, ValueError):
            pass

    def _current_project_data(self) -> dict:
        independent_audio = (
            self._audio_path
            if self._audio_path is not None and self._audio_path != self._video_path
            else None
        )
        line_layout_indices = (
            [int(getattr(line, "layout_index", 0) or 0) for line in self._timing_track.lines]
            if self._timing_track is not None
            else None
        )
        line_breaks_before = self._line_break_rows(self._timing_track)
        char_role_labels = self._collect_char_role_labels()
        line_guide_symbols = self._guide_symbol_rows(self._timing_track)
        line_inline_guide_symbols = self._inline_guide_symbol_rows(self._timing_track)
        line_display_overrides = self._display_override_rows(self._timing_track)
        line_animation_overrides = self._animation_override_rows(self._timing_track)
        extra_subtitle_sources = [
            {
                "name": source.name,
                "path": str(source.path),
                "line_layout_indices": [
                    int(getattr(line, "layout_index", 0) or 0) for line in source.track.lines
                ],
                "line_breaks_before": self._line_break_rows(source.track),
                "char_role_labels": self._char_role_rows(source.track),
                "line_guide_symbols": self._guide_symbol_rows(source.track),
                "line_inline_guide_symbols": self._inline_guide_symbol_rows(source.track),
                "line_display_overrides": self._display_override_rows(source.track),
                "line_animation_overrides": self._animation_override_rows(source.track),
                "page_plan": track_page_plan_to_dict(source.track.page_plan),
                "loading_settings_mode": source.track.loading_settings_mode,
                "loading_settings": (
                    subtitle_loading_settings_to_dict(source.track.loading_settings)
                    if source.track.loading_settings is not None
                    else None
                ),
                "loading_settings_snapshot": subtitle_loading_settings_to_dict(
                    source.track.loading_settings_snapshot
                ),
            }
            for source in self._extra_sources
        ] or None
        payload = project_payload(
            subtitle_path=self._subtitle_path,
            video_path=self._video_path,
            audio_path=independent_audio,
            background=background_payload(
                kind=self._background_source.kind,
                path=Path(self._background_source.path) if self._background_source.path else None,
                color=self._background_source.color,
                source_fps=self._background_source.source_fps,
                sequence_start_number=self._background_source.sequence_start_number,
                video_offset_ms=self._background_source.video_offset_ms,
            ) if self._background_source is not None else None,
            style=style_to_dict(self._style),
            screen=screen_settings_to_dict(self._screen_settings),
            selected_scheme_key=self._selected_scheme_key,
            line_layout_indices=line_layout_indices,
            line_breaks_before=line_breaks_before,
            char_role_labels=char_role_labels,
            line_guide_symbols=line_guide_symbols,
            line_inline_guide_symbols=line_inline_guide_symbols,
            line_display_overrides=line_display_overrides,
            line_animation_overrides=line_animation_overrides,
            page_plan=(
                track_page_plan_to_dict(self._timing_track.page_plan)
                if self._timing_track is not None
                else None
            ),
            loading_settings_mode=(
                self._timing_track.loading_settings_mode
                if self._timing_track is not None
                else None
            ),
            loading_settings=(
                subtitle_loading_settings_to_dict(self._timing_track.loading_settings)
                if self._timing_track is not None
                and self._timing_track.loading_settings is not None
                else None
            ),
            loading_settings_snapshot=(
                subtitle_loading_settings_to_dict(
                    self._timing_track.loading_settings_snapshot
                )
                if self._timing_track is not None
                else None
            ),
            extra_subtitle_sources=extra_subtitle_sources,
            project_role_names=self._property_panel.role_names,
            output=project_output_payload(
                encoder_mode=str(self._export_encoder_combo.currentData() or ENCODER_CPU),
                crf=self._export_crf_spin.value(),
                preset=str(self._export_preset_combo.currentData() or "medium"),
                codec=self._export_codec_value(),
                output_path=self._export_output_text(),
                native_export_enabled=False,
            ),
        )
        return self._merge_unresolved_resource_references(payload)

    def _merge_unresolved_resource_references(self, payload: dict) -> dict:
        """Keep skipped missing paths in the project without loading or dirtying them."""
        source = self._missing_resource_source_data
        labels = self._unresolved_resource_labels
        if not isinstance(source, dict) or not labels:
            return payload
        merged = deepcopy(payload)
        source_paths = split_project_paths(source)
        if "主字幕" in labels and not merged.get("subtitle_path"):
            path = source_paths["subtitle_path"]
            merged["subtitle_path"] = str(path) if path is not None else None
        background_labels = {"背景视频", "背景图片", "背景图片序列"}
        if labels & background_labels and not merged.get("background"):
            source_background = source.get("background")
            if isinstance(source_background, dict):
                merged["background"] = deepcopy(source_background)
            elif source_paths["video_path"] is not None:
                merged["video_path"] = str(source_paths["video_path"])
        if "独立音频" in labels and not merged.get("audio_path"):
            path = source_paths["audio_path"]
            merged["audio_path"] = str(path) if path is not None else None
        source_extras = source.get("extra_subtitle_sources")
        if isinstance(source_extras, list):
            current_extras = (
                list(merged.get("extra_subtitle_sources"))
                if isinstance(merged.get("extra_subtitle_sources"), list)
                else []
            )
            current_paths = {
                str(item.get("path") or "")
                for item in current_extras
                if isinstance(item, dict)
            }
            for index, item in enumerate(source_extras, start=1):
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "").strip() or str(index)
                label = f"副字幕「{name}」"
                path_text = str(item.get("path") or "").strip()
                if label in labels and path_text and path_text not in current_paths:
                    current_extras.append(deepcopy(item))
                    current_paths.add(path_text)
            if current_extras:
                merged["extra_subtitle_sources"] = current_extras
        return merged

    def _apply_project_data(self, data: dict, *, defer_assets: bool = False) -> None:
        self._loading_project = True
        self._tracks_window_refresh_timer.stop()
        self._project_deferred_load_timer.stop()
        self._project_deferred_loads = []
        self._defer_project_assets = bool(defer_assets)
        applied = False
        try:
            self._apply_project_data_inner(data)
            applied = True
        finally:
            self._loading_project = False
            self._defer_project_assets = False
        if applied and self._timing_track is not None:
            if defer_assets:
                self._tracks_window_refresh_timer.start(250)
            else:
                self._refresh_tracks_view_windows()

    def _apply_project_data_inner(self, data: dict) -> None:
        # 项目内容整体替换，旧的样式/轨道撤销记录全部失效
        self._clear_undo_history()
        # 1) 样式 / 屏幕 / 配色方案
        style_payload = data.get("style")
        project_style = style_from_dict(style_payload)
        self._screen_settings = screen_settings_from_dict(data.get("screen"))
        # Older projects stored already-resolved pixel sizes without a reference
        # height.  Treat those values as belonging to the saved output height so
        # the first later resize does not scale them from an incorrect 1080 base.
        if (
            not isinstance(style_payload, dict)
            or "font_reference_height" not in style_payload
        ):
            project_style = replace(
                project_style,
                font_reference_height=max(int(self._screen_settings.height), 1),
            )
        if project_style.title_overlay is None:
            project_style = replace(project_style, title_overlay=TitleOverlay())
        self._style, _font_names_changed = normalize_style_font_families(
            project_style, get_n3_font_catalog()
        )
        key = data.get("selected_scheme_key")
        if isinstance(key, str) and key:
            self._selected_scheme_key = key
        self._property_panel.set_style(self._style)
        self._property_panel.set_current_scheme_key(self._selected_scheme_key)
        self._selected_scheme_key = self._property_panel.current_scheme_key()
        self._preview_panel.set_style(self._style)
        self._lyrics_panel.set_style(self._style)
        self._set_export_screen_controls(self._screen_settings)
        self._sync_preview_output_size()
        # 2) 导出参数
        output = data.get("output") if isinstance(data.get("output"), dict) else {}
        self._apply_output_settings(output)
        # 3) 素材（存在才加载；缺失静默跳过，不阻塞打开）
        paths = split_project_paths(data)
        if paths["subtitle_path"] is not None and paths["subtitle_path"].is_file():
            self.load_subtitle_source(paths["subtitle_path"])
            self._apply_line_breaks_before(data.get("line_breaks_before"))
            self._apply_line_layout_indices(data.get("line_layout_indices"))
            if self._timing_track is not None:
                self._restore_track_page_state(self._timing_track, data)
            self._apply_char_role_labels(data.get("char_role_labels"))
            guide_mismatches = self._apply_guide_symbol_rows(
                self._timing_track, data.get("line_guide_symbols")
            )
            self._apply_inline_guide_symbol_rows(
                self._timing_track, data.get("line_inline_guide_symbols")
            )
            if guide_mismatches:
                rows = "、".join(str(row + 1) for row in guide_mismatches[:12])
                suffix = "…" if len(guide_mismatches) > 12 else ""
                fluent_warning(
                    self,
                    "部分导唱符替换未应用",
                    f"源字幕的行首标记已经变化，以下歌词行保持原文：{rows}{suffix}",
                )
            self._lyrics_panel.set_track(self._timing_track)
            self._preview_panel.set_track(self._timing_track)
            if self._timing_track is not None:
                self._apply_display_override_rows(
                    self._timing_track, data.get("line_display_overrides")
                )
                self._apply_animation_override_rows(
                    self._timing_track, data.get("line_animation_overrides")
                )
            self._refresh_tracks_view_windows()
        background = data.get("background") if isinstance(data.get("background"), dict) else None
        if self._defer_project_assets:
            self._queue_project_deferred_loads(
                background=background,
                fallback_video_path=paths["video_path"],
                audio_path=paths["audio_path"],
                extra_subtitle_sources=data.get("extra_subtitle_sources"),
                project_role_names=data.get("project_role_names"),
            )
        else:
            self._apply_extra_subtitle_sources(data.get("extra_subtitle_sources"))
            if background is not None:
                self._load_background_payload(background)
            elif paths["video_path"] is not None and paths["video_path"].is_file():
                self.load_video(paths["video_path"])
            audio = paths["audio_path"]
            if audio is not None and audio.is_file() and audio != self._video_path:
                self.load_audio(audio)
        # Project/N3 role payloads are authoritative.  Populate missing role
        # schemes only after those payloads have replaced source-LRC markers;
        # otherwise a transient ``【アクア】`` marker can auto-create an unrelated
        # palette before FontIndex=0 clears it back to the global N3 scheme.
        self._apply_project_role_options(data.get("project_role_names"))

    def _apply_project_role_options(self, project_roles: object) -> None:
        content_roles = self._content_role_options()
        if isinstance(project_roles, list):
            seen = set(content_roles)
            for value in project_roles:
                name = str(value or "").strip()
                if (
                    name
                    and name != TITLE_SCHEME_NAME
                    and name in self._style.custom_style_schemes
                    and name not in seen
                ):
                    seen.add(name)
                    content_roles.append(name)
        self._property_panel.set_roles(content_roles)
        self._lyrics_panel.set_role_options(self._merged_role_options())

    def _queue_project_deferred_loads(
        self,
        *,
        background: Optional[dict],
        fallback_video_path: Optional[Path],
        audio_path: Optional[Path],
        extra_subtitle_sources: object,
        project_role_names: object,
    ) -> None:
        loads: list[tuple[str, object]] = []
        if background is not None:
            loads.append(("background", deepcopy(background)))
        elif fallback_video_path is not None and fallback_video_path.is_file():
            loads.append(("video", fallback_video_path))
        if audio_path is not None:
            loads.append(("audio", audio_path))
        if isinstance(extra_subtitle_sources, list) and extra_subtitle_sources:
            loads.append(
                (
                    "extra_subtitle_sources",
                    (deepcopy(extra_subtitle_sources), deepcopy(project_role_names)),
                )
            )
        self._project_deferred_loads = loads
        self._project_deferred_load_generation = self._project_generation
        if loads:
            self._project_deferred_load_timer.start(500)

    def _process_project_deferred_load(self) -> None:
        if (
            not self._project_deferred_loads
            or self._project_deferred_load_generation != self._project_generation
        ):
            self._project_deferred_loads = []
            return
        kind, payload = self._project_deferred_loads.pop(0)
        was_loading = self._loading_project
        self._loading_project = True
        refresh_tracks = False
        try:
            if kind == "background" and isinstance(payload, dict):
                self._load_background_payload(payload)
            elif kind == "video" and isinstance(payload, Path) and payload.is_file():
                self.load_video(payload)
            elif kind == "audio" and isinstance(payload, Path):
                if payload.is_file() and payload != self._video_path:
                    self.load_audio(payload)
            elif kind == "extra_subtitle_sources" and isinstance(payload, tuple):
                sources, project_roles = payload
                self._apply_extra_subtitle_sources(sources)
                self._apply_project_role_options(project_roles)
                refresh_tracks = True
        finally:
            self._loading_project = was_loading
        if refresh_tracks and self._timing_track is not None:
            self._refresh_tracks_view_windows()
        if (
            self._project_deferred_loads
            and self._project_deferred_load_generation == self._project_generation
        ):
            self._project_deferred_load_timer.start(120)

    def _apply_output_settings(self, output: dict) -> None:
        directory_mode = output.get("directory_mode")
        custom_directory = output.get("custom_directory")
        if directory_mode in {EXPORT_DIR_SOURCE_VIDEO, EXPORT_DIR_CUSTOM}:
            if directory_mode != EXPORT_DIR_CUSTOM or (
                isinstance(custom_directory, str) and custom_directory.strip()
            ):
                self._export_dir_mode = str(directory_mode)
                self._export_custom_dir = str(custom_directory or "").strip()
        name_template = output.get("name_template")
        if isinstance(name_template, str) and name_template.strip():
            self._export_name_template = name_template.strip()
        encoder = output.get("encoder_mode")
        if encoder is not None:
            idx = self._export_encoder_combo.findData(encoder)
            if idx >= 0:
                self._export_encoder_combo.setCurrentIndex(idx)
        preset = output.get("preset")
        if isinstance(preset, str):
            p_idx = self._export_preset_combo.findData(preset)
            if p_idx >= 0:
                self._export_preset_combo.setCurrentIndex(p_idx)
        crf = output.get("crf")
        if isinstance(crf, int):
            self._export_crf_spin.setValue(crf)
        codec = output.get("codec")
        if isinstance(codec, str):
            c_idx = self._export_codec_combo.findData(codec)
            if c_idx >= 0:
                self._export_codec_combo.setCurrentIndex(c_idx)
        render_workers = output.get("render_workers")
        if (
            not self._loading_project
            and isinstance(render_workers, int)
            and render_workers in RENDER_WORKER_OPTIONS
        ):
            workers_idx = self._export_render_workers_combo.findData(render_workers)
            if workers_idx >= 0:
                self._export_render_workers_combo.setCurrentIndex(workers_idx)
        try:
            gpu_default_version = int(
                output.get("gpu_preview_default_version", 0) or 0
            )
        except (TypeError, ValueError):
            gpu_default_version = 0
        gpu_env_override = os.environ.get("KROK_SUBTITLE_GPU_PREVIEW")
        if gpu_env_override is not None or gpu_default_version < GPU_PREVIEW_DEFAULT_VERSION:
            gpu_preview_on = gpu_preview_enabled()
        else:
            gpu_preview_on = sys.platform == "win32" and bool(
                output.get("gpu_preview_enabled", True)
            )
        try:
            gpu_export_default_version = int(
                output.get("gpu_export_default_version", 0) or 0
            )
        except (TypeError, ValueError):
            gpu_export_default_version = 0
        if gpu_export_default_version < GPU_EXPORT_DEFAULT_VERSION:
            gpu_export_enabled = sys.platform == "win32"
        else:
            gpu_export_enabled = sys.platform == "win32" and bool(
                output.get("gpu_export_enabled", True)
            )
        # GPU preferences are local application settings and are intentionally
        # absent from project files. Loading a project must not reset them.
        if not self._loading_project:
            preview_quality = normalize_preview_quality(
                output.get("preview_quality", DEFAULT_PREVIEW_QUALITY)
            )
            self._transport_bar.set_preview_quality(preview_quality)
            self._preview_panel.set_preview_quality(preview_quality)
            self._gpu_preview_check.setChecked(gpu_preview_on)
            self._preview_panel.set_gpu_preview_enabled(gpu_preview_on)
            self._gpu_export_check.setChecked(gpu_export_enabled)
        if self._loading_project:
            # Project output names are authoritative.  Forget the previous
            # project's auto-generated name before its media starts loading.
            self._export_name_edit.clear()
            self._export_auto_name = ""
        out_path = output.get("output_path")
        if isinstance(out_path, str) and out_path.strip():
            path = Path(out_path.strip())
            self._export_name_edit.setText(path.stem)
        self._sync_export_directory()
        blocked = self._export_native_check.blockSignals(True)
        try:
            self._export_native_check.setChecked(False)
        finally:
            self._export_native_check.blockSignals(blocked)

    def _reset_export_settings_for_new_project(self) -> None:
        """Restore the last local export choices for a newly created project."""
        local_output = self._local_output_preferences
        controls = (
            self._export_encoder_combo,
            self._export_codec_combo,
            self._export_preset_combo,
            self._export_crf_spin,
            self._export_render_workers_combo,
            self._export_name_edit,
            self._export_native_check,
        )
        previous_signal_states = [control.blockSignals(True) for control in controls]
        try:
            encoder = local_output.get("encoder_mode", ENCODER_CPU)
            self._export_encoder_combo.setCurrentIndex(
                max(self._export_encoder_combo.findData(encoder), 0)
            )
            codec = local_output.get("codec", CODEC_H264)
            self._export_codec_combo.setCurrentIndex(
                max(self._export_codec_combo.findData(codec), 0)
            )
            preset = local_output.get("preset", "medium")
            preset_index = self._export_preset_combo.findData(preset)
            if preset_index < 0:
                preset_index = self._export_preset_combo.findData("medium")
            self._export_preset_combo.setCurrentIndex(
                max(preset_index, 0)
            )
            crf = local_output.get("crf", 18)
            self._export_crf_spin.setValue(
                crf if isinstance(crf, int) and 0 <= crf <= 51 else 18
            )
            render_workers = local_output.get("render_workers", 0)
            self._export_render_workers_combo.setCurrentIndex(
                max(self._export_render_workers_combo.findData(render_workers), 0)
            )
            self._export_native_check.setChecked(False)
            name = self._default_export_name()
            self._export_name_edit.setText(name)
            self._export_auto_name = name
        finally:
            for control, was_blocked in zip(controls, previous_signal_states):
                control.blockSignals(was_blocked)
        # Directory mode/custom path is an app preference, not project state.
        self._sync_export_directory()
        self._update_export_preset_enabled()
        self._refresh_export_format_label()

    def _confirm_discard_changes(self) -> bool:
        """有未保存改动时弹确认；返回 True 表示可以继续（已处理）。"""
        if not self._project_dirty:
            return True
        choice = fluent_choice(
            self,
            "未保存的改动",
            "当前项目有未保存的改动，是否先保存？",
            ["保存", "放弃", "取消"],
            default=2,
        )
        if choice not in (0, 1):
            return False
        if choice == 0:
            return self._save_project()
        self.discard_unsaved()
        return True

    def _new_project(self) -> None:
        if not self._confirm_discard_changes():
            return
        self._begin_project_generation()
        self._clear_loaded_media()
        self._apply_project_data(
            {
                "style": style_to_dict(deepcopy(self._app_default_style)),
                "screen": screen_settings_to_dict(ScreenSettings()),
                "selected_scheme_key": "global",
            }
        )
        self._reset_export_settings_for_new_project()
        self._project_path = None
        self._set_project_dirty(False)

    def _clear_loaded_media(self) -> None:
        """清空已加载的字幕 / 视频 / 音频，把各面板复位到空态（新建项目用）。"""
        self._loading_project = True
        try:
            self._timing_track = None
            self._extra_sources = []
            self._active_source_index = 0
            self._title_source_active = False
            self._clear_undo_history()
            self._subtitle_path = None
            self._watch_primary_subtitle_source = False
            self._property_panel.set_n3_template_lyrics_directory(None)
            self._video_path = None
            self._video_info = None
            self._background_source = None
            self._audio_path = None
            self._audio_info = None
            self._sync_audio_action_enabled()
            # 歌词列表回空态
            self._lyrics_panel.set_track(None)
            self._lyrics_panel.set_role_options([])
            self._lyrics_panel.set_sources([], 0)
            self._preview_panel.set_extra_tracks([])
            # 预览回空态：清字幕 + 视频 + 取消 populated
            self._preview_panel.set_track(None)
            self._preview_panel.set_video_source(None)
            self._preview_panel.set_populated(False)
            self._video_settings_panel.set_populated(False)
            self._property_panel.set_roles([])
            # 播放条 + 字幕轨道复位
            self._transport_bar.set_audio_source(None)
            self._transport_bar.set_time(0)
            self._transport_bar.set_duration(0)
            self._tracks_view.set_tracks([])
            self._tracks_view.set_duration(0)
            self._tracks_view.set_time(0)
            self._set_layout_issues([])
            self._last_margin_warning_key = ""
        finally:
            self._loading_project = False
            self._sync_subtitle_source_watcher()

    def open_initial_project(self, project_path: Path | str) -> bool:
        """Open a project supplied by the host application at startup."""
        return self._open_project_path(
            Path(project_path).expanduser(),
            confirm_discard=False,
        )

    def _open_project(self) -> None:
        if not self._confirm_discard_changes():
            return
        start_dir = str(self._project_path.parent) if self._project_path else ""
        path_str, _ = QFileDialog.getOpenFileName(
            self, "打开字幕渲染项目", start_dir, PROJECT_FILTER
        )
        if not path_str:
            return
        self._open_project_path(Path(path_str), confirm_discard=False)

    def _open_project_path(
        self,
        path: Path,
        *,
        confirm_discard: bool = True,
    ) -> bool:
        """Open a ``.yurika`` project selected from the menu or dropped."""
        if confirm_discard and not self._confirm_discard_changes():
            return False
        try:
            revision_before = inspect_project_file(path)
            data = load_render_project(path)
            revision_after = inspect_project_file(path)
            if revision_before != revision_after:
                raise OSError("项目文件在打开期间发生了变化，请重试")
        except (OSError, ValueError) as exc:
            fluent_error(
                self,
                "打开项目失败",
                f"无法读取项目文件：\n{path}\n\n{exc}",
                copyable=True,
            )
            return False
        missing_resources = self._missing_project_resources(data)
        self._begin_project_generation()
        self._clear_loaded_media()
        self._apply_project_data(data, defer_assets=True)
        self._project_path = path
        self._project_disk_revision = revision_after
        self._missing_resources = tuple(missing_resources)
        self._unresolved_resource_labels = {
            label for label, _path in missing_resources
        }
        self._missing_resource_source_data = deepcopy(data) if missing_resources else None
        self._set_project_dirty(False)
        if missing_resources:
            fluent_warning(
                self,
                "项目已打开，但部分素材未找到",
                "以下素材路径无效，已跳过加载：\n\n"
                + "\n".join(
                    f"• {label}：{path}" for label, path in missing_resources
                ),
                copyable=True,
            )
        return True

    @staticmethod
    def _missing_project_resources(data: dict) -> list[tuple[str, Path]]:
        """Collect missing project assets without blocking project loading."""
        missing: list[tuple[str, Path]] = []
        seen: set[str] = set()

        def add(label: str, path: Optional[Path], *, exists: Optional[bool] = None) -> None:
            if path is None:
                return
            key = str(path)
            if key in seen or (path.is_file() if exists is None else exists):
                return
            seen.add(key)
            missing.append((label, path))

        paths = split_project_paths(data)
        add("主字幕", paths["subtitle_path"])

        background = (
            data.get("background") if isinstance(data.get("background"), dict) else None
        )
        if background is not None:
            kind = str(background.get("kind") or "solid")
            raw_path = str(background.get("path") or "").strip()
            path = Path(raw_path) if raw_path else None
            if kind == "video":
                add("背景视频", path)
            elif kind == "image":
                add("背景图片", path)
            elif kind == "image_sequence" and path is not None:
                try:
                    sequence_start = max(
                        int(background.get("sequence_start_number") or 0), 0
                    )
                except (TypeError, ValueError):
                    sequence_start = 0
                source = BackgroundSource(
                    kind="image_sequence",
                    path=str(path),
                    sequence_start_number=sequence_start,
                )
                first_frame = background_sequence_frame_path(source, 0)
                add(
                    "背景图片序列",
                    path,
                    exists=first_frame is not None and first_frame.is_file(),
                )
        else:
            add("背景视频", paths["video_path"])

        add("独立音频", paths["audio_path"])

        extras = data.get("extra_subtitle_sources")
        if isinstance(extras, list):
            for index, item in enumerate(extras, start=1):
                if not isinstance(item, dict):
                    continue
                path_text = str(item.get("path") or "").strip()
                if not path_text:
                    continue
                name = str(item.get("name") or "").strip() or str(index)
                add(f"副字幕「{name}」", Path(path_text))
        return missing

    def _resolve_unresolved_resource_labels(self, labels: set[str]) -> None:
        """Drop unresolved references replaced explicitly by the user."""
        if not labels:
            return
        before = self._unresolved_resource_labels
        self._unresolved_resource_labels = before - set(labels)
        if self._missing_resources:
            self._missing_resources = tuple(
                item for item in self._missing_resources if item[0] not in labels
            )
        if not self._unresolved_resource_labels:
            self._missing_resource_source_data = None
        if before != self._unresolved_resource_labels and not self._loading_project:
            self._refresh_project_title()

    def _refresh_missing_resource_status(self, _checked: bool = False) -> None:
        """Refresh availability only; never load assets or mark project dirty."""
        if not self._missing_resources:
            InfoBar.info(
                title="素材状态",
                content="当前项目没有已知的缺失素材。",
                parent=self,
                position=InfoBarPosition.BOTTOM_RIGHT,
                duration=2500,
            )
            return
        current = self._missing_resources
        source = self._missing_resource_source_data
        if isinstance(source, dict):
            still_missing = {
                (label, str(path))
                for label, path in self._missing_project_resources(source)
            }
            remaining = tuple(
                item
                for item in current
                if (item[0], str(item[1])) in still_missing
            )
        else:
            remaining = tuple(item for item in current if not item[1].is_file())
        recovered_count = len(current) - len(remaining)
        self._missing_resources = remaining
        self._refresh_project_title()
        InfoBar.success(
            title="素材状态已刷新",
            content=(
                f"已恢复 {recovered_count} 项素材路径，项目内容未自动修改。"
                if recovered_count
                else f"仍有 {len(remaining)} 项素材缺失。"
            ),
            parent=self,
            position=InfoBarPosition.BOTTOM_RIGHT,
            duration=3500,
        )

    def _open_project_backup_directory(self, _checked: bool = False) -> None:
        directory = self._backup_root()
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            fluent_error(
                self,
                "无法打开备份目录",
                f"{directory}\n\n{exc}",
                copyable=True,
            )
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(directory))):
            fluent_error(
                self,
                "无法打开备份目录",
                str(directory),
                copyable=True,
            )

    def _import_n3_project(self) -> None:
        """导入 NicoKaraMaker3 项目（.n3proj）：素材 / 字体配色 / 布局 / 标题 / 输出。"""
        if not self._confirm_discard_changes():
            return
        start_dir = str(self._project_path.parent) if self._project_path else ""
        path_str, _ = QFileDialog.getOpenFileName(
            self, "导入 NicoKaraMaker3 项目", start_dir, N3_PROJECT_FILTER
        )
        if not path_str:
            return
        self._import_n3_project_path(Path(path_str), confirm_discard=False)

    def _import_n3_project_path(
        self,
        path: Path,
        *,
        confirm_discard: bool = True,
    ) -> bool:
        """Import an N3 project selected from the menu or dropped onto a panel."""
        path = Path(path)
        if confirm_discard and not self._confirm_discard_changes():
            return False
        try:
            result = load_n3proj(path)
        except (OSError, ValueError) as exc:
            fluent_error(
                self, "导入失败", f"无法读取 NicoKaraMaker3 项目文件：\n{path}\n\n{exc}"
            )
            return False
        self._begin_project_generation()
        self._clear_loaded_media()
        self._apply_project_data(result.project_data)
        # Opening a saved .yurika project must preserve its explicit canvas,
        # but a direct N3 import uses the referenced video's actual dimensions.
        # N3 visual fields are already absolute target pixels, so rebase their
        # reference heights before changing the canvas instead of scaling the
        # values a second time (for example, 180 px must stay 180 px at 4K).
        if self._video_info is not None:
            video_height = int(self._video_info.video_height or 0)
            if video_height > 0:
                self._style = replace(
                    self._style,
                    font_reference_height=video_height,
                    layout_reference_height=video_height,
                )
                self._property_panel.set_style(self._style)
                self._preview_panel.set_style(self._style)
                self._lyrics_panel.set_style(self._style)
            self._sync_output_size_to_video(self._video_info)
        # 导入的是外来工程：保存时必须另存为 .yurika，因此视为未命名 + 有改动。
        self._project_path = None
        missing_resources = self._missing_project_resources(result.project_data)
        self._missing_resources = tuple(missing_resources)
        self._unresolved_resource_labels = {
            label for label, _path in missing_resources
        }
        self._missing_resource_source_data = (
            deepcopy(result.project_data) if missing_resources else None
        )
        self._set_project_dirty(True)
        if result.warnings:
            fluent_info(
                self,
                "导入完成（部分设置需注意）",
                "已导入 N3 项目，以下内容请检查：\n\n"
                + "\n".join(f"• {warning}" for warning in result.warnings),
                copyable=True,
            )
        else:
            InfoBar.success(
                title="N3 项目导入完成",
                content=path.name,
                parent=self,
                position=InfoBarPosition.BOTTOM_RIGHT,
                duration=2500,
            )
        return True

    def _save_project(self) -> bool:
        if self._project_path is None:
            return self._save_project_as()
        return self._write_project(self._project_path)

    def _save_project_as(self) -> bool:
        start = str(self._project_path) if self._project_path else (
            str((self._subtitle_path or self._video_path or Path.cwd()).with_suffix(""))
            + PROJECT_FILE_SUFFIX
        )
        path_str, _ = QFileDialog.getSaveFileName(
            self, "保存字幕渲染项目", start, PROJECT_FILTER
        )
        if not path_str:
            return False
        if not path_str.endswith(PROJECT_FILE_SUFFIX):
            path_str += PROJECT_FILE_SUFFIX
        return self._write_project(Path(path_str))

    def _write_project(self, path: Path) -> bool:
        path = Path(path)
        if self._project_path is not None and path == self._project_path:
            try:
                disk_revision = inspect_project_file(path)
            except OSError as exc:
                self._project_save_error = str(exc)
                self._refresh_project_title()
                fluent_error(
                    self,
                    "无法检查项目文件",
                    f"保存前无法确认文件是否被外部修改：\n{path}\n\n{exc}",
                    copyable=True,
                )
                return False
            if (
                self._project_disk_revision is not None
                and disk_revision != self._project_disk_revision
            ):
                choice = fluent_choice(
                    self,
                    "项目文件已被外部修改",
                    f"磁盘上的项目文件在打开或上次保存后发生了变化：\n"
                    f"{path}\n\n直接覆盖可能丢失其他程序的修改。",
                    ("覆盖", "另存为", "取消"),
                    default=2,
                )
                if choice == 1:
                    return self._save_project_as()
                if choice != 0:
                    return False
        previous_recovery_path = self._recovery_path()
        invalidate_recovery_project(previous_recovery_path, delete=False)
        self._auto_save_timer.stop()
        self._auto_save_pending = False
        self._wait_for_recovery_worker()
        revision_at_save = self._project_revision
        self._project_saving = True
        self._project_save_error = None
        self._refresh_project_title()
        try:
            backup_project_file(
                path,
                self._backup_root(),
                max_count=self._project_backup_count,
            )
            save_render_project(path, self._current_project_data())
            saved_disk_revision = inspect_project_file(path)
        except (OSError, TypeError, ValueError) as exc:
            self._project_saving = False
            self._project_save_error = str(exc)
            self._refresh_project_title()
            self._schedule_recovery_auto_save()
            logging.getLogger(__name__).warning(
                "字幕项目保存失败: named=%s error_type=%s",
                self._project_path is not None,
                type(exc).__name__,
            )
            fluent_error(
                self,
                "保存项目失败",
                f"无法写入项目文件：\n{path}\n\n{exc}",
                copyable=True,
            )
            return False
        self._project_path = path
        self._project_disk_revision = saved_disk_revision
        self._saved_revision = revision_at_save
        self._project_saving = False
        self._project_save_error = None
        self._set_project_dirty(False)
        self._cleanup_recovery_file(previous_recovery_path)
        self._cleanup_recovery_file()
        InfoBar.success(
            title="项目已保存",
            content=str(path),
            parent=self,
            position=InfoBarPosition.BOTTOM_RIGHT,
            duration=2500,
        )
        return True

    def _make_preview_tab(self) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(24, 4, 24, 4)
        outer.setSpacing(4)

        body = QSplitter(Qt.Orientation.Vertical)
        body.setChildrenCollapsible(False)
        self._preview_body_splitter = body

        # 上半部：左·歌词 / 右·背景视频拖入，视频加载后右侧切换为属性设置。
        top = QSplitter(Qt.Orientation.Horizontal)
        top.setChildrenCollapsible(False)
        self._preview_splitter = top

        self._preview_window = PreviewPlayerWindow(self)
        self._preview_window.userClosed.connect(self._on_preview_window_user_closed)
        self._preview_panel = self._preview_window.preview_panel
        self._preview_panel.set_style(self._style)
        self._preview_panel.pathDropped.connect(self._load_dropped_background)
        self._preview_panel.browseRequested.connect(self._browse_background_media)
        self._add_background_empty_actions(self._preview_panel)
        self._transport_bar = self._preview_window.transport_bar

        self._lyrics_panel = LyricsPanel()
        self._lyrics_panel.set_style(self._style)
        self._lyrics_panel.pathDropped.connect(self._load_dropped_subtitle)
        self._lyrics_panel.browseRequested.connect(self._browse_subtitle)
        self._lyrics_panel.roleChanged.connect(self._on_lyrics_role_changed)
        self._lyrics_panel.roleChangeRequested.connect(
            self._on_lyrics_roles_changed
        )
        self._lyrics_panel.charRolesChanged.connect(self._on_lyrics_char_roles_changed)
        self._lyrics_panel.guideCharRolesChanged.connect(
            self._on_guide_char_roles_changed
        )
        self._lyrics_panel.inlineCharEditChanged.connect(
            self._on_inline_char_edit_changed
        )
        self._lyrics_panel.guideSymbolImportRequested.connect(
            self._on_guide_symbol_import_requested
        )
        self._lyrics_panel.guideSymbolRemoveRequested.connect(
            self._on_guide_symbol_remove_requested
        )
        self._lyrics_panel.guidePrefixReplaceRequested.connect(
            self._on_guide_prefix_replace_requested
        )
        self._lyrics_panel.titleEditRequested.connect(
            self._freeze_title_template_for_character_edit
        )
        self._lyrics_panel.animationOverrideRequested.connect(
            self._on_line_animation_override_requested
        )
        self._lyrics_panel.rowClicked.connect(self._on_lyrics_row_clicked)
        self._lyrics_panel.layoutChangeRequested.connect(self._on_layout_change_requested)
        self._lyrics_panel.sourceSelected.connect(self._on_source_selected)
        self._lyrics_panel.sourceAddRequested.connect(self._on_source_add_requested)
        self._lyrics_panel.sourceRemoveRequested.connect(self._on_source_remove_requested)
        self._lyrics_panel.sourceRefreshRequested.connect(
            self._on_source_refresh_requested
        )
        self._lyrics_panel.sourceSettingsRequested.connect(
            self._on_source_settings_requested
        )
        self._lyrics_panel.pageBoundaryRequested.connect(
            self._on_page_boundary_requested
        )
        self._lyrics_panel.pageMoveRequested.connect(
            self._on_page_move_requested
        )
        top.addWidget(self._lyrics_panel)

        self._transport_bar.set_preview_fps(self._screen_settings.fps)
        self._transport_bar.timeChanged.connect(self._preview_panel.set_time)
        self._transport_bar.playbackStateChanged.connect(self._preview_panel.set_playing)
        self._transport_bar.previewQualityChanged.connect(
            self._on_preview_quality_changed
        )
        self._preview_panel.canvas.framePainted.connect(self._transport_bar.note_preview_frame_painted)
        self._preview_panel.gpuFallback.connect(self._on_gpu_preview_fallback)
        # 单播放器统一（步骤2，§10.9，flag KROK_SUBTITLE_UNIFIED_PLAYER 默认关）：
        # 视频自带音频时同一文件本不该被音频/视频两个 QMediaPlayer 各自解码。开启后用一个
        # 共享 PlaybackController 同时驱动音视频（A/V 天然锁帧），预览不再自建视频 player。
        # raster 回退画布暂不支持 → use_external_player 返回 False，自动回退旧三播放器路径。
        self._playback: Optional[PlaybackController] = None
        if unified_player_enabled():
            controller = PlaybackController(self)
            if self._preview_panel.use_external_player(controller):
                self._playback = controller
                self._transport_bar.attach_playback_controller(controller)

        self._property_panel = PropertyPanel()
        self._layout_issues_button = self._make_layout_issues_button(
            self._property_panel
        )
        self._show_preview_btn = self._make_preview_window_button(
            self._property_panel
        )
        self._property_panel.set_navigation_actions(
            [self._layout_issues_button, self._show_preview_btn]
        )
        self._property_panel.set_style(self._style)
        self._property_panel.set_preset_schemes(self._style_presets)
        self._property_panel.set_output_size(
            self._screen_settings.width,
            self._screen_settings.height,
        )
        self._property_panel.styleChanged.connect(self._apply_style)
        self._property_panel.presetSchemesChanged.connect(self._apply_style_presets)
        self._property_panel.defaultSchemeSaveRequested.connect(
            self._save_builtin_scheme_default
        )
        self._property_panel.defaultLayoutSaveRequested.connect(
            self._save_layout_default
        )
        self._property_panel.schemeSelectionChanged.connect(self._on_scheme_selection_changed)
        self._property_panel.layoutAssignAllRequested.connect(self._on_layout_assign_all)
        self._property_panel.layoutAutoAssignRequested.connect(self._on_layout_auto_assign)
        self._property_panel.layoutDeleted.connect(self._on_layout_deleted)
        self._property_panel.set_current_scheme_key(self._selected_scheme_key)
        self._selected_scheme_key = self._property_panel.current_scheme_key()

        self._video_settings_panel = DropPanel(
            extensions={
                ".mp4", ".mkv", ".mov", ".webm", ".avi", ".flv",
                *IMAGE_EXTENSIONS,
                PROJECT_FILE_SUFFIX,
                N3_PROJECT_FILE_SUFFIX,
            },
            empty_title="拖入背景素材",
            empty_hint="拖入视频、静态图片、Yurika 工程（.yurika）\n或 N3 项目（.n3proj）；图片序列与纯色请用下方按钮",
            empty_icon="🎬",
        )
        self._video_settings_panel.pathDropped.connect(self._load_dropped_background)
        self._video_settings_panel.browseRequested.connect(self._browse_background_media)
        self._add_background_empty_actions(self._video_settings_panel)
        self._video_settings_panel.set_content(self._property_panel)
        top.addWidget(self._video_settings_panel)

        # 不设 stretch factor：QSplitter 默认按当前尺寸比例分配新增空间，
        # 窗口缩放时能保持用户拖出的比例。传大数值让 setSizes 按比例缩放
        # 到实际宽度（面板各自的最小宽仍然优先）。
        ratio = self._preview_splitter_ratio
        top.setSizes([round(ratio * 10_000), round((1.0 - ratio) * 10_000)])
        top.splitterMoved.connect(self._on_preview_splitter_moved)
        body.addWidget(top)

        # 底部：字幕轨道（波形已移除，不做波形图功能）
        self._tracks_view = TrackTimelineView()
        self._tracks_view.seekRequested.connect(self._transport_bar.set_time)
        self._tracks_view.displayWindowEdited.connect(self._on_display_window_edited)
        self._transport_bar.timeChanged.connect(self._tracks_view.set_time)
        body.addWidget(self._tracks_view)

        body.setStretchFactor(0, 5)
        body.setStretchFactor(1, 2)
        body.setSizes([520, 180])

        outer.addWidget(body, 1)
        return page

    def _init_shortcuts(self) -> None:
        # 空格键播放 / 暂停（窗口范围内有效，避免误伤未来的文本输入）
        self._space_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Space), self)
        self._space_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._space_shortcut.activated.connect(self._toggle_playback_from_shortcut)
        self._sync_playback_shortcut_scope()

        # 项目文件快捷键。作用域限制在本模块内（WidgetWithChildrenShortcut），
        # 嵌入工作台时不会和宿主的全局快捷键打架。
        self._project_shortcuts = []
        for seq, handler in (
            (QKeySequence.StandardKey.New, self._new_project),
            (QKeySequence.StandardKey.Open, self._open_project),
            (QKeySequence.StandardKey.Save, self._save_project),
            (QKeySequence.StandardKey.SaveAs, self._save_project_as),
            # 撤销/重做：样式（字体/布局等）与字幕轨道编辑（Ctrl+Z / Ctrl+Y；
            # 另补 Ctrl+Shift+Z，StandardKey.Redo 在 Windows 上只映射 Ctrl+Y）
            (QKeySequence.StandardKey.Undo, self._undo_edit),
            (QKeySequence.StandardKey.Redo, self._redo_edit),
            ("Ctrl+Shift+Z", self._redo_edit),
        ):
            shortcut = QShortcut(QKeySequence(seq), self)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(handler)
            self._project_shortcuts.append(shortcut)

    def _make_export_tab(self) -> QWidget:
        page = QWidget()
        page.setObjectName("SubtitleExportPage")
        themed(
            page,
            lambda: "#SubtitleExportPage { background: transparent; }",
        )
        outer = QVBoxLayout(page)
        outer.setContentsMargins(24, 4, 24, 16)
        outer.setSpacing(10)

        # 内容列限制最大宽度并水平居中，宽屏下表单不再拉满整行。
        column = QWidget()
        column.setObjectName("SrExportColumn")
        themed(column, lambda: "#SrExportColumn { background: transparent; }")
        column.setMaximumWidth(1200)
        column.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        layout = QVBoxLayout(column)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        center_row = QHBoxLayout()
        center_row.setContentsMargins(0, 0, 0, 0)
        center_row.addStretch(1)
        center_row.addWidget(column)
        center_row.addStretch(1)
        outer.addLayout(center_row, 1)

        # qfluentwidgets 语义标签自行跟随主题；保留实例引用，防止被 GC 移出
        # styleSheetManager 的 WeakKeyDictionary 后主题失效（同 SUG 导出页的教训）。
        self._export_theme_labels: list[QWidget] = []
        # 主体两栏：左·设置卡片列（定宽），右·导出预览（吃掉剩余空间）
        body_row = QHBoxLayout()
        body_row.setContentsMargins(0, 0, 0, 0)
        body_row.setSpacing(16)
        settings_col = QWidget()
        settings_col.setObjectName("SrExportSettingsCol")
        themed(settings_col, lambda: "#SrExportSettingsCol { background: transparent; }")
        settings_col.setFixedWidth(430)
        self._export_settings_col = settings_col
        settings_layout = QVBoxLayout(settings_col)
        settings_layout.setContentsMargins(0, 0, 0, 0)
        settings_layout.setSpacing(12)

        # 卡片 1：输出文件（第一行选文件夹，第二行文件名，扩展名固定 .mp4）
        self._export_location_settings_button = FluentToolButton(FIF.SETTING)
        self._export_location_settings_button.setToolTip("导出视频位置与默认文件名设置")
        self._export_location_settings_button.setFixedSize(30, 30)
        self._export_location_settings_button.setIconSize(QSize(16, 16))
        self._export_location_settings_button.clicked.connect(
            self._open_export_location_settings
        )
        output_card, output_layout = self._make_export_card(
            "输出文件", self._export_location_settings_button, icon=FIF.SAVE_AS
        )
        dir_row = QHBoxLayout()
        dir_row.setContentsMargins(0, 0, 0, 0)
        dir_row.setSpacing(8)
        self._export_dir_edit = FluentLineEdit()
        self._export_dir_edit.setPlaceholderText("选择输出文件夹")
        self._export_dir_edit.editingFinished.connect(
            self._on_export_directory_edited
        )
        self._export_browse_button = FluentPushButton(FIF.FOLDER, "浏览")
        self._export_browse_button.clicked.connect(self._browse_export_output)
        dir_row.addWidget(self._export_dir_edit, 1)
        dir_row.addWidget(self._export_browse_button)
        output_layout.addLayout(dir_row)

        name_row = QHBoxLayout()
        name_row.setContentsMargins(0, 0, 0, 0)
        name_row.setSpacing(8)
        self._export_name_edit = FluentLineEdit()
        self._export_name_edit.setPlaceholderText("文件名（默认：视频文件名_yurika出力）")
        name_suffix = QLabel(".mp4")
        name_suffix.setObjectName("SrExportExtBadge")
        themed(
            name_suffix,
            lambda: (
                "#SrExportExtBadge {{ color: {fg}; background: {bg};"
                " border-radius: 6px; padding: 4px 8px;"
                " font-size: 9pt; font-weight: 600; }}"
            ).format(
                fg=palette().accent_primary,
                bg=(
                    "rgba(255, 122, 140, 0.16)"
                    if palette().is_dark
                    else "rgba(255, 90, 111, 0.10)"
                ),
            ),
        )
        name_row.addWidget(self._export_name_edit, 1)
        name_row.addWidget(name_suffix)
        output_layout.addLayout(name_row)
        # 最近一次自动生成的文件名——用户没改过就跟随视频切换更新
        self._export_auto_name = ""
        settings_layout.addWidget(output_card)

        # 卡片 2：画面与编码
        params_card, params_layout = self._make_export_card(
            "画面与编码", icon=FIF.VIDEO
        )
        sync_hint = QLabel("宽度 / 高度 / 帧率与预览页的「画面」设置双向联动。")
        sync_hint.setObjectName("SrExportSyncHint")
        sync_hint.setWordWrap(True)
        themed(
            sync_hint,
            lambda: (
                "#SrExportSyncHint {{ background: {bg}; color: {fg};"
                " border-radius: 6px; padding: 6px 10px; font-size: 9pt; }}"
            ).format(
                bg="#26313F" if palette().is_dark else "#EEF4FF",
                fg="#A6C8FF" if palette().is_dark else "#3D6BBF",
            ),
        )
        params_layout.addWidget(sync_hint)

        params_row = QHBoxLayout()
        params_row.setContentsMargins(0, 0, 0, 0)
        params_row.setSpacing(10)
        # 字段上方已有 CaptionLabel 标签，SpinBox 不再重复「宽/高」后缀
        self._export_width_spin = self._export_spin(160, 7680, 1920, "")
        self._export_height_spin = self._export_spin(90, 4320, 1080, "")
        self._export_fps_combo = FluentComboBox()
        self._export_fps_combo.setMinimumHeight(32)
        for fps in SCREEN_FPS_OPTIONS:
            self._export_fps_combo.addItem(f"{fps} fps", userData=fps)
        params_row.addWidget(self._labeled_export_control("宽度", self._export_width_spin))
        params_row.addWidget(self._labeled_export_control("高度", self._export_height_spin))
        params_row.addWidget(self._labeled_export_control("帧率", self._export_fps_combo))
        params_layout.addLayout(params_row)

        encode_row = QHBoxLayout()
        encode_row.setContentsMargins(0, 0, 0, 0)
        encode_row.setSpacing(10)
        self._export_encoder_combo = FluentComboBox()
        self._export_encoder_combo.setMinimumHeight(32)
        self._export_encoder_combo.addItem("CPU 软编", userData=ENCODER_CPU)
        self._export_encoder_combo.addItem("自动硬编", userData=ENCODER_AUTO)
        self._export_encoder_combo.addItem("NVIDIA NVENC", userData=ENCODER_NVENC)
        self._export_encoder_combo.addItem("Intel QSV", userData=ENCODER_QSV)
        self._export_encoder_combo.addItem("AMD AMF", userData=ENCODER_AMF)
        self._export_encoder_combo.currentIndexChanged.connect(
            self._update_export_preset_enabled
        )
        self._export_codec_combo = FluentComboBox()
        self._export_codec_combo.setMinimumHeight(32)
        self._export_codec_combo.addItem("H.264 (AVC)", userData=CODEC_H264)
        self._export_codec_combo.addItem("H.265 (HEVC)", userData=CODEC_HEVC)
        self._export_codec_combo.setToolTip(
            "H.265 同画质体积更小，但编码更慢、老设备兼容性略差。"
        )
        self._export_codec_combo.currentIndexChanged.connect(
            self._refresh_export_format_label
        )
        encode_row.addWidget(self._labeled_export_control("编码器", self._export_encoder_combo))
        encode_row.addWidget(self._labeled_export_control("视频编码", self._export_codec_combo))
        params_layout.addLayout(encode_row)

        quality_row = QHBoxLayout()
        quality_row.setContentsMargins(0, 0, 0, 0)
        quality_row.setSpacing(10)
        self._export_preset_combo = FluentComboBox()
        self._export_preset_combo.setMinimumHeight(32)
        for preset in CPU_PRESETS:
            self._export_preset_combo.addItem(preset, userData=preset)
        self._export_preset_combo.setCurrentText("medium")
        self._export_crf_spin = self._export_spin(0, 51, 18, "")
        self._export_crf_spin.setToolTip("CRF 质量：数值越小画质越高、文件越大；18 约为视觉无损。")
        quality_row.addWidget(self._labeled_export_control("CPU preset", self._export_preset_combo))
        quality_row.addWidget(self._labeled_export_control("质量 (CRF)", self._export_crf_spin))
        params_layout.addLayout(quality_row)

        self._export_render_workers_combo = FluentComboBox()
        self._export_render_workers_combo.setMinimumHeight(32)
        self._export_render_workers_combo.addItem("自动（最多 8 进程）", userData=0)
        for workers in RENDER_WORKER_OPTIONS[1:]:
            self._export_render_workers_combo.addItem(
                f"{workers} 进程", userData=workers
            )
        self._export_render_workers_combo.setToolTip(
            "字幕帧渲染进程数。12/16 适合核心数较多且内存充足的电脑；"
            "进程越多不一定越快，且会明显增加内存占用。"
        )
        params_layout.addWidget(
            self._labeled_export_control(
                "渲染进程", self._export_render_workers_combo
            )
        )
        settings_layout.addWidget(params_card)

        self._export_native_check = CheckBox("实验：使用 native 字幕渲染器导出")
        self._export_native_check.setChecked(False)
        self._export_native_check.setEnabled(False)
        self._export_native_check.setVisible(False)
        self._export_native_check.setToolTip("native 字幕渲染器暂时停用。")
        self._gpu_preview_check = CheckBox("使用 GPU 渲染字幕预览")
        self._gpu_preview_check.setChecked(gpu_preview_enabled())
        self._gpu_preview_check.setVisible(sys.platform == "win32")
        self._gpu_preview_check.setToolTip(
            "使用稳定的 G5 shared-memory/QImage 路径加速字幕透明层；不可用或失败时自动回退 Painter。"
        )
        self._gpu_export_check = CheckBox("使用 GPU 渲染字幕导出")
        self._gpu_export_check.setChecked(sys.platform == "win32")
        self._gpu_export_check.setVisible(sys.platform == "win32")
        self._gpu_export_check.setToolTip(
            "仅用 Direct2D 渲染字幕条带，仍由当前 ffmpeg 编码器输出；"
            "失败时会删除半成品并从头回退 Painter。"
        )
        settings_layout.addWidget(self._gpu_preview_check)
        settings_layout.addWidget(self._gpu_export_check)
        settings_layout.addWidget(self._export_native_check)
        settings_layout.addStretch(1)

        # 右栏：导出预览（仿 N3 出力预览——边导出边显示 ffmpeg 合成帧）
        monitor_card = SimpleCardWidget()
        self._export_monitor_card = monitor_card
        monitor_layout = QVBoxLayout(monitor_card)
        self._export_monitor_layout = monitor_layout
        monitor_layout.setContentsMargins(20, 14, 20, 16)
        monitor_layout.setSpacing(10)
        monitor_header = QHBoxLayout()
        monitor_header.setContentsMargins(0, 0, 0, 0)
        monitor_title = StrongBodyLabel("导出预览")
        self._export_theme_labels.append(monitor_title)
        self._export_eta_label = CaptionLabel("")
        monitor_header.setSpacing(8)
        monitor_header.addWidget(
            self._make_card_icon_badge(FIF.MOVIE), 0, Qt.AlignmentFlag.AlignVCenter
        )
        monitor_header.addWidget(monitor_title)
        monitor_header.addStretch(1)
        monitor_header.addWidget(self._export_eta_label)
        self._export_monitor_header = monitor_header
        monitor_layout.addLayout(monitor_header)
        self._export_monitor_view = _ExportMonitorView()
        self._export_monitor_frame = _AspectRatioBox(
            self._export_monitor_view,
            aspect_ratio=(
                self._export_width_spin.value() / self._export_height_spin.value()
            ),
        )
        self._export_monitor_frame.setMinimumSize(240, 135)
        # 比例容器占用全部可用区域，画面按导出比例尽量吃满卡片宽度。
        monitor_layout.addWidget(self._export_monitor_frame, 1)
        self._export_format_label = CaptionLabel("输出格式: MP4 · H.264 (AVC)")
        monitor_layout.addWidget(self._export_format_label)

        body_row.addWidget(settings_col, 0, Qt.AlignmentFlag.AlignTop)
        body_row.addWidget(monitor_card, 0, Qt.AlignmentFlag.AlignTop)
        body_row.addStretch(1)
        layout.addStretch(1)
        layout.addLayout(body_row)
        layout.addStretch(1)

        # 底部横贯操作区：进度 + 状态 + 开始/停止
        self._export_progress = FluentProgressBar()
        self._export_progress.setRange(0, 1)
        self._export_progress.setValue(0)
        layout.addWidget(self._export_progress)

        self._export_status_label = CaptionLabel("")
        self._export_status_label.setWordWrap(True)
        self._export_status_label.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        layout.addWidget(self._export_status_label)

        action_row = QHBoxLayout()
        action_row.setContentsMargins(0, 0, 0, 0)
        action_row.setSpacing(8)
        self._export_start_button = FluentPrimaryPushButton(FIF.PLAY, "开始导出")
        self._export_start_button.setMinimumHeight(38)
        self._export_start_button.clicked.connect(self._start_render_export)
        self._export_stop_button = FluentPushButton(FIF.CLOSE, "停止导出")
        self._export_stop_button.setMinimumHeight(38)
        self._export_stop_button.setEnabled(False)
        self._export_stop_button.clicked.connect(self._stop_render_export)
        action_row.addWidget(self._export_start_button, 1)
        action_row.addWidget(self._export_stop_button)
        layout.addLayout(action_row)

        # 导出预览轮询：ffmpeg 持续覆盖写预览 JPG，定时读文件 mtime 变化后刷新
        self._export_preview_timer = QTimer(self)
        self._export_preview_timer.setInterval(500)
        self._export_preview_timer.timeout.connect(self._poll_export_preview)
        self._export_preview_dir: Optional[Path] = None
        self._export_preview_file: Optional[Path] = None
        self._export_preview_mtime_ns = 0
        self._export_started_monotonic = 0.0

        self._update_export_preset_enabled()
        return page

    def _make_export_card(
        self,
        title_text: str,
        header_action: Optional[QWidget] = None,
        icon: Optional[FIF] = None,
    ) -> tuple[SimpleCardWidget, QVBoxLayout]:
        card = SimpleCardWidget()
        layout = QVBoxLayout(card)
        layout.setContentsMargins(20, 14, 20, 16)
        layout.setSpacing(10)
        header = StrongBodyLabel(title_text)
        self._export_theme_labels.append(header)
        header_row = QHBoxLayout()
        header_row.setContentsMargins(0, 0, 0, 0)
        header_row.setSpacing(8)
        if icon is not None:
            header_row.addWidget(
                self._make_card_icon_badge(icon), 0, Qt.AlignmentFlag.AlignVCenter
            )
        header_row.addWidget(header)
        header_row.addStretch(1)
        if header_action is not None:
            header_row.addWidget(header_action, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addLayout(header_row)
        return card, layout

    def _make_card_icon_badge(self, icon: FIF) -> QLabel:
        badge = QLabel()
        badge.setObjectName("SrExportCardBadge")
        badge.setFixedSize(26, 26)
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)

        def _qss() -> str:
            # themed() 主题切换时重跑 factory，顺带把 pixmap 换成当前品牌色
            p = palette()
            badge.setPixmap(
                icon.icon(color=QColor(p.accent_primary)).pixmap(QSize(14, 14))
            )
            tint = (
                "rgba(255, 122, 140, 0.18)" if p.is_dark else "rgba(255, 90, 111, 0.12)"
            )
            return f"#SrExportCardBadge {{ background: {tint}; border-radius: 8px; }}"

        themed(badge, _qss)
        return badge

    def _update_export_preset_enabled(self) -> None:
        # CPU preset 只影响 libx264；「自动硬编」可能回退 CPU，保持可编辑。
        mode = str(self._export_encoder_combo.currentData() or ENCODER_CPU)
        cpu_possible = mode in (ENCODER_CPU, ENCODER_AUTO)
        self._export_preset_combo.setEnabled(cpu_possible)
        self._export_preset_combo.setToolTip(
            "" if cpu_possible else "CPU preset 仅在 CPU / libx264 编码时生效。"
        )

    @staticmethod
    def _export_spin(
        minimum: int, maximum: int, value: int, suffix: str
    ) -> FluentSpinBox:
        spin = FluentSpinBox()
        spin.setRange(minimum, maximum)
        spin.setValue(value)
        spin.setSuffix(suffix)
        spin.setMinimumHeight(32)
        return spin

    def _labeled_export_control(self, label_text: str, control: QWidget) -> QWidget:
        box = QWidget()
        # 工作台全局 QSS 会给裸 QWidget 刷底色，在白色卡片里会显出灰块
        box.setObjectName("SrExportFieldBox")
        themed(box, lambda: "#SrExportFieldBox { background: transparent; }")
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        label = CaptionLabel(label_text)
        self._export_theme_labels.append(label)
        layout.addWidget(label)
        layout.addWidget(control)
        return box

    # ------------------------------------------------------------------ browse fallback

    def _browse_subtitle(self) -> None:
        start_dir = str(self._subtitle_path.parent) if self._subtitle_path else ""
        path_str, _ = QFileDialog.getOpenFileName(
            self, "选择 SUG 项目或 Nicokara 逐字 LRC 文件", start_dir, SUBTITLE_FILTER
        )
        if path_str:
            self.load_subtitle_source(Path(path_str))

    def _browse_video(self) -> None:
        start_dir = str(self._video_path.parent) if self._video_path else ""
        path_str, _ = QFileDialog.getOpenFileName(
            self, "选择背景视频", start_dir, VIDEO_FILTER
        )
        if path_str:
            self.load_video(Path(path_str))

    def _browse_background_media(self) -> None:
        current = (
            Path(self._background_source.path)
            if self._background_source is not None and self._background_source.path
            else self._video_path
        )
        start_dir = str(current.parent) if current is not None else ""
        path_str, _ = QFileDialog.getOpenFileName(
            self, "选择背景视频或静态图片", start_dir, BACKGROUND_MEDIA_FILTER
        )
        if path_str:
            self._load_dropped_background(Path(path_str))

    def _load_dropped_background(self, path: Path) -> None:
        suffix = path.suffix.lower()
        if suffix == PROJECT_FILE_SUFFIX:
            self._open_project_path(path)
            return
        if suffix == N3_PROJECT_FILE_SUFFIX:
            self._import_n3_project_path(path)
            return
        if suffix in IMAGE_EXTENSIONS:
            self.load_background_image(path)
        else:
            self.load_video(path)

    def _load_dropped_subtitle(self, path: Path) -> None:
        suffix = path.suffix.lower()
        if suffix == PROJECT_FILE_SUFFIX:
            self._open_project_path(path)
            return
        if suffix == N3_PROJECT_FILE_SUFFIX:
            self._import_n3_project_path(path)
            return
        self.load_subtitle_source(path)

    def _add_background_empty_actions(self, panel: DropPanel) -> None:
        panel.add_empty_action("视频", self._browse_video)
        panel.add_empty_action("静态图", self._browse_background_image)
        panel.add_empty_action("图片序列", self._browse_background_sequence)
        panel.add_empty_action("纯色", self._choose_solid_background)

    def _browse_background_image(self) -> None:
        start_dir = str(Path(self._background_source.path).parent) if self._background_source and self._background_source.path else ""
        path_str, _ = QFileDialog.getOpenFileName(self, "选择静态背景图片", start_dir, IMAGE_FILTER)
        if path_str:
            self.load_background_image(Path(path_str))

    def _browse_background_sequence(self) -> None:
        start_dir = str(Path(self._background_source.path).parent) if self._background_source and self._background_source.path else ""
        path_str, _ = QFileDialog.getOpenFileName(self, "选择图片序列首帧", start_dir, IMAGE_FILTER)
        if path_str:
            fps, ok = fluent_get_int(
                self,
                "图片序列帧率",
                "源帧率（每秒图片数）",
                value=(
                    int(self._background_source.source_fps or self._screen_settings.fps)
                    if self._background_source is not None
                    else self._screen_settings.fps
                ),
                minimum=1,
                maximum=240,
            )
            if ok:
                self.load_background_sequence(Path(path_str), fps)

    def _choose_solid_background(self) -> None:
        initial = self._background_source.color if self._background_source else "#000000"
        color = QColorDialog.getColor(initial=QColor(initial), parent=self, title="选择纯色背景")
        if color.isValid():
            self.set_solid_background(color.name())

    def _browse_audio(self) -> None:
        start_dir = str(self._audio_path.parent) if self._audio_path else ""
        path_str, _ = QFileDialog.getOpenFileName(self, "选择独立音频", start_dir, AUDIO_FILTER)
        if path_str:
            self.load_audio(Path(path_str))

    def _browse_export_output(self) -> None:
        start = self._export_dir_edit.text().strip() or str(self._default_export_dir())
        path_str = QFileDialog.getExistingDirectory(self, "选择输出文件夹", start)
        if path_str:
            self._set_export_directory_settings(
                EXPORT_DIR_CUSTOM, path_str, persist=True
            )

    def _on_export_directory_edited(self) -> None:
        directory = self._export_dir_edit.text().strip()
        if directory:
            self._set_export_directory_settings(
                EXPORT_DIR_CUSTOM, directory, persist=True
            )

    def _open_export_location_settings(self) -> None:
        dialog = _ExportLocationDialog(
            self._export_dir_mode,
            self._export_custom_dir,
            self._default_export_dir(),
            self,
            name_template=self._export_name_template,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        mode, custom_dir = dialog.selection()
        template_changed = dialog.name_template() != self._export_name_template
        self._export_name_template = dialog.name_template()
        self._set_export_directory_settings(mode, custom_dir, persist=True)
        if template_changed:
            # 只重刷「还是自动生成」的那份文件名；用户手填过的不动。
            current = self._normalized_export_name()
            if not current or current == self._export_auto_name:
                name = self._default_export_name()
                self._export_name_edit.setText(name)
                self._export_auto_name = name
        InfoBar.success(
            title="导出设置已保存",
            content=(
                "将保存在指定目录。"
                if mode == EXPORT_DIR_CUSTOM
                else "将保存在字幕视频所在目录。"
            ),
            parent=self,
            position=InfoBarPosition.BOTTOM_RIGHT,
            duration=2500,
        )

    # ------------------------------------------------------------------ public

    def load_subtitle_source(self, path: Path) -> Optional[TimingTrack]:
        """加载字幕源文件。支持 SUG 项目（.sug）与 Nicokara 逐字 LRC（.lrc）。"""
        suffix = path.suffix.lower()
        if suffix == ".sug":
            return self.load_from_sug(path)
        return self.load_from_lrc(path)

    def load_from_lrc(self, path: Path) -> Optional[TimingTrack]:
        """加载 Nicokara 逐字 LRC 文件。返回解析结果（失败返回 None 并弹错）。"""
        try:
            track = load_nicokara_lrc(path)
        except Exception as exc:  # noqa: BLE001 — 暴露给用户的统一错误处理
            fluent_error(
                self, "加载字幕失败", f"无法解析字幕文件：\n{path}\n\n错误：{exc}"
            )
            return None
        self._apply_timing_track(track, path, watch_source=True)
        return track

    def load_from_sug(self, path: Path) -> Optional[TimingTrack]:
        """加载 SUG 项目文件，直接读取打轴数据而不导出中间 LRC。"""
        try:
            track = load_sug_timing_track(path)
        except Exception as exc:  # noqa: BLE001 — 暴露给用户的统一错误处理
            fluent_error(
                self, "加载字幕失败", f"无法解析 SUG 项目：\n{path}\n\n错误：{exc}"
            )
            return None
        self._apply_timing_track(track, path, watch_source=True)
        return track

    def load_from_sug_project(
        self,
        project: object,
        source_path: Optional[Path] = None,
        *,
        nicokara_tags: Optional[dict] = None,
    ) -> Optional[TimingTrack]:
        """加载嵌入式 SUG 当前项目对象，供主工作流第 4 步 → 第 5 步接线使用。"""
        try:
            track = timing_track_from_sug_project(
                project, nicokara_tags=nicokara_tags
            )
        except Exception as exc:  # noqa: BLE001
            fluent_error(
                self, "加载字幕失败", f"无法读取打轴项目：\n{exc}"
            )
            return None
        # In-memory workflow handoff is intentionally not coupled to the SUG
        # editor.  Only files explicitly imported from disk are watched.
        self._apply_timing_track(track, source_path, watch_source=False)
        return track

    def _apply_timing_track(
        self,
        track: TimingTrack,
        source_path: Optional[Path],
        *,
        watch_source: bool = False,
    ) -> None:
        self._watch_primary_subtitle_source = bool(watch_source and source_path)
        if self._watch_primary_subtitle_source and source_path is not None:
            self._set_subtitle_source_baseline(source_path, track)
        self._timing_track = track
        self._subtitle_path = source_path
        if not self._loading_project:
            track.loading_settings_mode = "global"
            track.loading_settings = None
            track.loading_settings_snapshot = self._subtitle_loading_defaults
            track.page_plan = build_page_plan(
                track, self._subtitle_loading_defaults, self._style
            )
            project_page_plan_to_legacy_fields(track, self._style)
            self._apply_remembered_layout_assignment(track)
            self._resolve_unresolved_resource_labels({"主字幕"})
        self._property_panel.set_n3_template_lyrics_directory(
            source_path.parent if source_path is not None else None
        )
        self._active_source_index = 0
        self._title_source_active = False
        # 换字幕源后旧的行索引全部失效
        self._clear_undo_history()
        self._refresh_source_ui()
        self._lyrics_panel.set_track(track)
        if not self._loading_project:
            self._apply_imported_role_preset_choices(track.role_options)
            self._property_panel.merge_roles(self._content_role_options())
            self._lyrics_panel.set_role_options(self._merged_role_options())
        self._property_panel.set_current_scheme_key(self._selected_scheme_key)
        self._selected_scheme_key = self._property_panel.current_scheme_key()
        self._preview_panel.set_track(track)
        self._sync_tracks_view()
        self._refresh_transport_duration()
        self._transport_bar.set_time(0)
        self._prefill_export_output()
        self._margin_check_timer.start()
        self._sync_subtitle_source_watcher()
        self._mark_project_dirty()

    @staticmethod
    def _subtitle_source_key(path: Path) -> str:
        resolved = str(Path(path).resolve(strict=False))
        return resolved.casefold() if sys.platform == "win32" else resolved

    @staticmethod
    def _subtitle_source_digest(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _set_subtitle_source_baseline(self, path: Path, track: TimingTrack) -> None:
        source_path = Path(path).resolve(strict=False)
        try:
            digest = self._subtitle_source_digest(source_path)
        except OSError:
            digest = ""
        self._source_watch_states[self._subtitle_source_key(source_path)] = (
            _WatchedSubtitleState(
                path=source_path,
                baseline=deepcopy(track),
                seen_digest=digest,
            )
        )

    def _referenced_subtitle_sources(self) -> dict[str, tuple[Path, TimingTrack]]:
        referenced: dict[str, tuple[Path, TimingTrack]] = {}
        if (
            self._watch_primary_subtitle_source
            and self._subtitle_path is not None
            and self._timing_track is not None
        ):
            path = self._subtitle_path.resolve(strict=False)
            referenced[self._subtitle_source_key(path)] = (path, self._timing_track)
        for source in self._extra_sources:
            path = source.path.resolve(strict=False)
            referenced.setdefault(self._subtitle_source_key(path), (path, source.track))
        return referenced

    def _sync_subtitle_source_watcher(self) -> None:
        referenced = self._referenced_subtitle_sources()
        for key in list(self._source_watch_states):
            if key not in referenced:
                self._source_watch_states.pop(key, None)
                self._pending_source_reload_keys.discard(key)
                self._source_reload_retries.pop(key, None)
        for key, (path, track) in referenced.items():
            if key not in self._source_watch_states:
                self._set_subtitle_source_baseline(path, track)

        watched_files = self._source_watcher.files()
        watched_directories = self._source_watcher.directories()
        if watched_files:
            self._source_watcher.removePaths(watched_files)
        if watched_directories:
            self._source_watcher.removePaths(watched_directories)

        files = sorted(
            {str(state.path) for state in self._source_watch_states.values() if state.path.is_file()}
        )
        directories = sorted(
            {
                str(state.path.parent)
                for state in self._source_watch_states.values()
                if state.path.parent.is_dir()
            }
        )
        if files:
            self._source_watcher.addPaths(files)
        if directories:
            self._source_watcher.addPaths(directories)

    def _on_subtitle_source_file_changed(self, path_text: str) -> None:
        key = self._subtitle_source_key(Path(path_text))
        if key in self._source_watch_states:
            self._queue_subtitle_source_reload(key)
        # Editors may replace a file atomically, which removes Qt's file watch.
        self._sync_subtitle_source_watcher()

    def _on_subtitle_source_directory_changed(self, path_text: str) -> None:
        directory_key = self._subtitle_source_key(Path(path_text))
        for key, state in self._source_watch_states.items():
            if self._subtitle_source_key(state.path.parent) == directory_key:
                self._queue_subtitle_source_reload(key)
        self._sync_subtitle_source_watcher()

    def _queue_subtitle_source_reload(self, key: str) -> None:
        self._pending_source_reload_keys.add(key)
        if self._render_thread is None:
            self._source_change_timer.start()

    def _process_subtitle_source_changes(self) -> None:
        if self._render_thread is not None:
            return
        pending = tuple(self._pending_source_reload_keys)
        self._pending_source_reload_keys.clear()
        for key in pending:
            self._reload_external_subtitle_source(key)

    def _retry_subtitle_source_reload(self, key: str, error: Exception) -> None:
        attempt = self._source_reload_retries.get(key, 0) + 1
        if attempt <= 5:
            self._source_reload_retries[key] = attempt
            self._pending_source_reload_keys.add(key)
            self._source_change_timer.start(400)
            return
        self._source_reload_retries.pop(key, None)
        state = self._source_watch_states.get(key)
        if state is None:
            return
        logging.getLogger(__name__).warning(
            "外部字幕源重新解析失败: path=%s error=%s", state.path, error
        )
        InfoBar.warning(
            title="字幕源更新失败",
            content=f"无法读取更新后的字幕文件，已保留当前内容：\n{state.path}",
            parent=self,
            position=InfoBarPosition.BOTTOM_RIGHT,
            duration=5000,
        )

    def _reload_external_subtitle_source(self, key: str) -> None:
        state = self._source_watch_states.get(key)
        if state is None:
            return
        path = state.path
        if not path.is_file():
            if not state.missing_notified:
                state.missing_notified = True
                InfoBar.warning(
                    title="字幕源不可用",
                    content=f"外部字幕文件已被删除或移动，当前内容将继续保留：\n{path}",
                    parent=self,
                    position=InfoBarPosition.BOTTOM_RIGHT,
                    duration=5000,
                )
            return

        try:
            before = path.stat()
            digest = self._subtitle_source_digest(path)
            if digest == state.seen_digest:
                state.missing_notified = False
                self._source_reload_retries.pop(key, None)
                self._sync_subtitle_source_watcher()
                return
            candidate = self._load_timing_track_file(path)
            after = path.stat()
            if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
                raise OSError("字幕文件仍在写入")
        except Exception as exc:  # noqa: BLE001 - partial external writes are retried
            self._retry_subtitle_source_reload(key, exc)
            return

        state.missing_notified = False
        self._source_reload_retries.pop(key, None)
        if state.baseline == candidate:
            state.seen_digest = digest
            self._sync_subtitle_source_watcher()
            return

        primary_merge: Optional[TrackReloadMerge] = None
        if (
            self._watch_primary_subtitle_source
            and self._subtitle_path is not None
            and self._timing_track is not None
            and self._subtitle_source_key(self._subtitle_path) == key
        ):
            primary_merge = merge_reloaded_track(
                self._timing_track, state.baseline, candidate
            )

        extra_merges: dict[int, TrackReloadMerge] = {}
        for index, source in enumerate(self._extra_sources):
            if self._subtitle_source_key(source.path) == key:
                extra_merges[index] = merge_reloaded_track(
                    source.track, state.baseline, candidate
                )

        merges = ([primary_merge] if primary_merge is not None else []) + list(
            extra_merges.values()
        )
        conflicts = list(dict.fromkeys(item for merge in merges for item in merge.conflicts))
        if conflicts:
            details = "\n".join(f"• {item}" for item in conflicts[:8])
            suffix = "\n• 还有其他冲突……" if len(conflicts) > 8 else ""
            accepted = fluent_question(
                self,
                "字幕源结构已变化",
                "更新后的歌词结构与当前项目不同，以下设置无法自动迁移：\n"
                f"{details}{suffix}\n\n是否仍然载入新字幕？",
                yes_text="载入新字幕",
                no_text="保留当前内容",
                default_cancel=True,
            )
            if not accepted:
                state.seen_digest = digest
                self._sync_subtitle_source_watcher()
                return

        if primary_merge is not None:
            self._timing_track = primary_merge.track
            self._timing_track.page_plan = build_legacy_page_plan(
                self._timing_track, self._style
            )
            project_page_plan_to_legacy_fields(self._timing_track, self._style)
        for index, merge in extra_merges.items():
            self._extra_sources[index].track = merge.track
            merge.track.page_plan = build_legacy_page_plan(merge.track, self._style)
            project_page_plan_to_legacy_fields(merge.track, self._style)

        structure_changed = any(merge.structure_changed for merge in merges)
        timing_only = bool(merges) and all(merge.timing_only for merge in merges)
        if structure_changed:
            self._clear_undo_history()
        state.baseline = deepcopy(candidate)
        state.seen_digest = digest
        self._refresh_source_ui()
        self._refresh_lyrics_panel_source()
        self._property_panel.merge_roles(self._content_role_options())
        self._lyrics_panel.set_role_options(self._merged_role_options())
        if self._timing_track is not None:
            self._preview_panel.set_track(self._timing_track)
        self._sync_extra_tracks_to_preview()
        self._refresh_transport_duration()
        self._margin_check_timer.start()
        self._mark_project_dirty()
        InfoBar.success(
            title="字幕源已更新",
            content=(
                f"已自动载入 {path.name} 的最新时间轴。"
                if timing_only
                else f"已自动载入 {path.name} 的最新内容。"
            ),
            parent=self,
            position=InfoBarPosition.BOTTOM_RIGHT,
            duration=3000,
        )
        self._sync_subtitle_source_watcher()

    def _apply_imported_role_preset_choices(self, role_names: list[str]) -> None:
        """Resolve cross-group preset collisions before roles are materialized."""

        selected = self._property_panel.choose_role_presets_for_import(role_names)
        if not selected:
            return
        schemes = dict(self._style.custom_style_schemes)
        changed = False
        for role_name, scheme in selected.items():
            if role_name in schemes:
                continue
            schemes[role_name] = deepcopy(scheme)
            changed = True
        if not changed:
            return
        style = replace(self._style, custom_style_schemes=schemes)
        self._property_panel.set_style(style)
        self._apply_style(style)

    def load_video(self, path: Path) -> Optional[MediaInfo]:
        """加载背景视频，调用 ffprobe 读取分辨率 / 帧率 / 时长。

        视频如果含音频流，会自动用作播放音轨——用户不需要再单独选音频。
        """
        info = self._probe(path, "视频")
        if info is None:
            return None
        if info.video_streams == 0:
            fluent_warning(self, "背景视频不可用", f"该文件不含视频流：\n{path}")
            return None
        old_video = self._video_path
        had_independent_audio = (
            self._audio_path is not None and self._audio_path != old_video
        )
        if had_independent_audio:
            self._audio_path = None
            self._audio_info = None
        self._video_path = path
        self._video_info = info
        if not self._loading_project:
            self._sync_output_size_to_video(info)
        self._background_source = BackgroundSource(kind="video", path=str(path))
        # Export state belongs to the loaded media model, not to the floating
        # preview window.  Keep it in sync before touching preview widgets: a
        # preview show/layout failure must not leave the export page pointing
        # at the previous video.
        self._prefill_export_output(force_name=not self._loading_project)
        if not self._loading_project:
            self._resolve_unresolved_resource_labels(
                {"背景视频", "背景图片", "背景图片序列", "独立音频"}
            )
        self._preview_panel.set_background_source(self._background_source)
        self._video_settings_panel.set_populated(True)
        self._preview_window.set_media_title(path)
        self._request_preview_window()
        # 视频自带音频 → 喂给 TransportBar 走 QMediaPlayer 播放
        if info.audio_streams > 0:
            self._audio_path = path
            self._audio_info = info
        elif self._audio_path == old_video:
            self._audio_path = None
            self._audio_info = None
        if self._playback is not None:
            # 单播放器：视频（无论是否含音频）整体交给共享 controller（同时出视频 + 音频）。
            self._transport_bar.set_audio_source(path)
        elif info.audio_streams > 0:
            self._transport_bar.set_audio_source(path)
        else:
            self._transport_bar.set_audio_source(None)
        self._sync_audio_action_enabled()
        if had_independent_audio:
            InfoBar.warning(
                title="已移除独立音频",
                content="视频背景只使用内嵌音轨，避免双时钟造成音画不同步。",
                parent=self,
                position=InfoBarPosition.BOTTOM_RIGHT,
                duration=3500,
            )
        self._refresh_transport_duration()
        self._mark_project_dirty()
        return info

    def _sync_output_size_to_video(self, info: MediaInfo) -> None:
        """Make a newly imported video's dimensions the current output size."""
        width = int(info.video_width or 0)
        height = int(info.video_height or 0)
        if width <= 0 or height <= 0:
            return
        settings = ScreenSettings(
            preset_key=match_screen_preset_key(width, height, self._screen_settings.par),
            par=self._screen_settings.par,
            width=width,
            height=height,
            fps=self._export_fps_value(),
        )
        self._set_export_screen_controls(settings)
        self._sync_preview_output_size()
        self._refresh_export_format_label()
        self._on_export_screen_changed()

    def load_background_image(self, path: Path) -> bool:
        image = QImage(str(path))
        if image.isNull():
            fluent_warning(self, "背景图片不可用", f"无法读取图片：\n{path}")
            return False
        self._set_non_video_background(BackgroundSource(kind="image", path=str(path)))
        return True

    def load_background_sequence(self, first_frame: Path, source_fps: int = 60) -> bool:
        image = QImage(str(first_frame))
        if image.isNull():
            fluent_warning(self, "图片序列不可用", f"无法读取首帧：\n{first_frame}")
            return False
        pattern, start_number = infer_image_sequence_pattern(first_frame)
        if pattern == first_frame:
            fluent_warning(
                self,
                "图片序列命名无效",
                "首帧文件名需要以连续编号结尾，例如 frame_0001.png。",
            )
            return False
        self._set_non_video_background(
            BackgroundSource(
                kind="image_sequence", path=str(pattern), source_fps=max(int(source_fps), 1),
                sequence_start_number=start_number,
            )
        )
        return True

    def set_solid_background(self, color: str) -> None:
        self._set_non_video_background(BackgroundSource(kind="solid", color=color))

    def _set_non_video_background(self, source: BackgroundSource) -> None:
        old_video = self._video_path
        self._video_path = None
        self._video_info = None
        if self._audio_path == old_video:
            self._audio_path = None
            self._audio_info = None
            self._transport_bar.set_audio_source(None)
        self._background_source = source
        # As with video backgrounds, resolve export state from the model before
        # preview-window work so the two pages cannot diverge on a UI failure.
        self._prefill_export_output()
        if not self._loading_project:
            self._resolve_unresolved_resource_labels(
                {"背景视频", "背景图片", "背景图片序列"}
            )
        self._preview_panel.set_background_source(source)
        self._video_settings_panel.set_populated(True)
        self._sync_audio_action_enabled()
        if source.path:
            self._preview_window.set_media_title(Path(source.path))
        self._request_preview_window()
        self._refresh_transport_duration()
        self._mark_project_dirty()

    def _load_background_payload(self, payload: dict) -> None:
        kind = str(payload.get("kind") or "solid")
        path = Path(str(payload.get("path"))) if payload.get("path") else None
        source = BackgroundSource(
            kind=kind if kind in {"video", "image", "image_sequence", "solid"} else "solid",
            path=str(path) if path is not None else None,
            color=str(payload.get("color") or "#000000"),
            source_fps=(int(payload["source_fps"]) if payload.get("source_fps") else None),
            sequence_start_number=max(int(payload.get("sequence_start_number") or 0), 0),
            video_offset_ms=int(payload.get("video_offset_ms") or 0),
        )
        if kind == "video" and path is not None and path.is_file():
            self.load_video(path)
            self._background_source = source
            self._preview_panel.set_background_source(source)
        elif kind == "image" and path is not None and path.is_file():
            self._set_non_video_background(source)
        elif kind == "image_sequence" and path is not None:
            self._set_non_video_background(source)
        elif kind == "solid":
            self._set_non_video_background(source)

    def load_audio(self, path: Path) -> Optional[MediaInfo]:
        """为图片/图片序列/纯色背景加载独立音轨。

        视频背景严格使用内嵌音轨，避免预览形成两个媒体时钟。
        """
        if self._background_source is not None and self._background_source.kind == "video":
            fluent_warning(
                self,
                "无法添加独立音频",
                "视频背景只使用视频内嵌音轨，以避免双时钟造成音画不同步。",
            )
            return None
        info = self._probe(path, "音频")
        if info is None:
            return None
        if info.audio_streams == 0:
            fluent_warning(self, "音频不可用", f"该文件不含音频流：\n{path}")
            return None
        self._audio_path = path
        if not self._loading_project:
            self._resolve_unresolved_resource_labels({"独立音频"})
        self._audio_info = info
        self._transport_bar.set_audio_source(path)
        self._refresh_transport_duration()
        self._mark_project_dirty()
        return info

    def _sync_audio_action_enabled(self) -> None:
        enabled = not (
            self._background_source is not None
            and self._background_source.kind == "video"
        )
        for action in self._audio_menu_actions:
            action.setEnabled(enabled)

    @property
    def timing_track(self) -> Optional[TimingTrack]:
        return self._timing_track

    @property
    def video_info(self) -> Optional[MediaInfo]:
        return self._video_info

    @property
    def audio_info(self) -> Optional[MediaInfo]:
        return self._audio_info

    # ------------------------------------------------------------------ helpers

    def _probe(self, path: Path, label: str) -> Optional[MediaInfo]:
        try:
            ffprobe_path = self._resolve_ffprobe_path()
            return probe_media(ffprobe_path, path)
        except ProcessingError as exc:
            fluent_error(self, f"加载{label}失败", str(exc))
            return None
        except Exception as exc:  # noqa: BLE001
            fluent_error(
                self,
                f"加载{label}失败",
                f"无法读取媒体信息：\n{path}\n\n错误：{exc}",
            )
            return None

    def _resolve_ffprobe_path(self) -> str:
        ffmpeg_dir: Optional[Path] = None
        try:
            settings = load_app_settings()
            raw = (settings.ffmpeg_dir or "").strip()
            if raw:
                ffmpeg_dir = Path(raw)
        except Exception:
            ffmpeg_dir = None
        return find_tool("ffprobe", ffmpeg_dir)

    def _refresh_transport_duration(self) -> None:
        candidates: list[int] = [track_duration_ms(track) for track in self._all_tracks()]
        if self._video_info is not None and self._video_info.duration > 0:
            candidates.append(int(self._video_info.duration * 1000))
        if self._audio_info is not None and self._audio_info.duration > 0:
            candidates.append(int(self._audio_info.duration * 1000))
        duration = max(candidates, default=0)
        self._tracks_view.set_duration(duration)
        self._preview_panel.set_duration(duration)
        if duration > 0:
            self._transport_bar.set_duration(duration)

    def _apply_style(self, style: Style) -> None:
        previous = self._style
        style = ensure_page_layout_defaults(style)
        paint_only = previous is not style and _paint_only_style_delta(previous, style)
        old_capacities = {
            "default": max(len(previous.line_alignments), 1),
            **{
                layout.layout_id: max(len(layout.line_alignments), 1)
                for layout in previous.layouts
                if layout.layout_id
            },
        }
        new_ids = {
            "default",
            *(layout.layout_id for layout in style.layouts if layout.layout_id),
        }
        shrunk = [
            layout_id
            for layout_id, old_capacity in old_capacities.items()
            if layout_id in new_ids
            and layout_capacity(style, layout_id) < old_capacity
        ]
        self._style = style
        affected_pages = 0
        added_pages = 0
        track_indices: tuple[int, ...] = ()
        tracks_before: tuple[TimingTrack, ...] = ()
        if shrunk:
            # 只有缩容重排会改轨道，也只有那条路径需要轨道撤销快照。全轨深拷贝
            # 是 O(全部行×全部字符)，绝不能挂在每次样式微调的必经路径上。
            tracks = self._all_tracks()
            track_indices = tuple(range(len(tracks)))
            tracks_before = tuple(deepcopy(track) for track in tracks)
            for layout_id in shrunk:
                for track in tracks:
                    affected, added = reflow_pages_for_layout_capacity(
                        track, style, layout_id
                    )
                    affected_pages += affected
                    added_pages += added
        self._property_panel.set_style(style)
        self._remember_style_preferences(previous, style)
        self._preview_panel.set_style(style)
        self._lyrics_panel.set_style(style)
        # 角色在属性面板中新建 / 重命名 / 删除时，同步逐字符编辑器的可选项。
        self._lyrics_panel.set_role_options(self._merged_role_options())
        if (
            bool(previous.title_overlay and previous.title_overlay.enabled)
            != bool(style.title_overlay and style.title_overlay.enabled)
        ):
            if not (style.title_overlay and style.title_overlay.enabled):
                if self._title_source_active:
                    self._active_source_index = 0
                self._title_source_active = False
            self._refresh_source_ui()
        if self._title_source_active:
            self._refresh_lyrics_panel_source()
        # 提前入场/延迟退场等布局参数会改行显示窗口 → 同步轨道把手数据。
        # 但纯改配色既不会移动显示窗口，也不会改变余白告警（两者只依赖时间轴、
        # 布局和字体）。这两项在真实工程上要各花几百毫秒的界面线程时间——实测
        # 一条 41 行 + 一条 15 行、共 172 条注音的曲目，轨道窗口重算 599ms、
        # 余白检查 118ms——是改色时窗口卡住的主因，纯上色时直接跳过。
        if not paint_only:
            self._schedule_tracks_view_window_refresh()
            self._margin_check_timer.start()
        self._schedule_persisted_state_save()
        self._mark_project_dirty()
        # 调用方预先改写过 self._style 的路径（如导出高度重算）不入撤销栈。
        if previous is not style:
            if affected_pages:
                tracks_after = tuple(
                    deepcopy(track) for track in self._all_tracks()
                )
                self._undo_stack.append(
                    (
                        "style_tracks",
                        style_to_dict(previous),
                        style_to_dict(style),
                        track_indices,
                        tracks_before,
                        tracks_after,
                    )
                )
                del self._undo_stack[:-_UNDO_STACK_LIMIT]
                self._redo_stack.clear()
            else:
                self._record_style_undo(previous, style)
        if affected_pages:
            InfoBar.warning(
                title="布局行数已缩小",
                content=(
                    f"{affected_pages} 个引用页面已按新容量自动重排"
                    + (f"，新增 {added_pages} 页" if added_pages else "")
                    + "。"
                ),
                parent=self,
                position=InfoBarPosition.BOTTOM_RIGHT,
                duration=5000,
            )

    _STYLE_UNDO_MERGE_WINDOW_S = 1.2
    """同一批字段的连续样式微调（spin 连点 / 文本逐字输入）合并为一条撤销记录。"""

    @staticmethod
    def _style_diff_paths(old: object, new: object, prefix: str = "", depth: int = 3) -> set[str]:
        """两份样式快照的差异路径（如 ``custom_style_schemes.标题.font_size_px``）。

        只下钻 dict（层数受限），列表等按叶子整体比较——签名用于「同一控件的
        连续微调」合并判定，精确到字段即可。
        """
        if not (isinstance(old, dict) and isinstance(new, dict) and depth > 0):
            return {prefix} if old != new else set()
        paths: set[str] = set()
        for key in set(old) | set(new):
            if old.get(key) == new.get(key):
                continue
            child_prefix = f"{prefix}.{key}" if prefix else str(key)
            paths |= SubtitleRenderWindow._style_diff_paths(
                old.get(key), new.get(key), child_prefix, depth - 1
            )
        return paths

    def _record_style_undo(self, previous: Style, current: Style) -> None:
        """字体/布局等属性面板编辑入撤销栈（Ctrl+Z / Ctrl+Y）。"""
        old_payload = style_to_dict(previous)
        new_payload = style_to_dict(current)
        if old_payload == new_payload:
            return
        changed = frozenset(self._style_diff_paths(old_payload, new_payload))
        now = time.monotonic()
        top = self._undo_stack[-1] if self._undo_stack else None
        if (
            top is not None
            and top[0] == "style"
            and self._style_change_paths_mergeable(top[3], changed)
            and now - top[4] <= self._STYLE_UNDO_MERGE_WINDOW_S
        ):
            # 合并：保留最早的旧值，滚动更新新值与时间戳。
            if top[1] == new_payload:
                self._undo_stack.pop()
            else:
                self._undo_stack[-1] = ("style", top[1], new_payload, changed, now)
        else:
            self._undo_stack.append(("style", old_payload, new_payload, changed, now))
            del self._undo_stack[:-_UNDO_STACK_LIMIT]
        self._redo_stack.clear()

    def _restore_style(self, payload: object) -> bool:
        """把撤销/重做快照套回全局样式（不再录制新的撤销记录）。"""
        if not isinstance(payload, dict):
            return False
        style = style_from_dict(payload)
        previous = self._style
        self._style = style
        self._remember_style_preferences(previous, style)
        self._property_panel.set_style(style)
        self._preview_panel.set_style(style)
        self._lyrics_panel.set_style(style)
        if not (style.title_overlay and style.title_overlay.enabled):
            if self._title_source_active:
                self._active_source_index = 0
            self._title_source_active = False
        self._refresh_source_ui()
        if self._title_source_active:
            self._refresh_lyrics_panel_source()
        self._refresh_tracks_view_windows()
        self._margin_check_timer.start()
        self._schedule_persisted_state_save()
        self._mark_project_dirty()
        return True

    def _restore_style_and_tracks(
        self, style_payload: object, indices: object, tracks: object
    ) -> bool:
        if (
            not isinstance(style_payload, dict)
            or not isinstance(indices, tuple)
            or not isinstance(tracks, tuple)
            or len(indices) != len(tracks)
            or any(
                not isinstance(index, int) or not isinstance(track, TimingTrack)
                for index, track in zip(indices, tracks)
            )
        ):
            return False
        if any(self._track_by_index(index) is None for index in indices):
            return False
        if not self._restore_style(style_payload):
            return False
        for index, track in zip(indices, tracks):
            self._set_track_by_index(index, deepcopy(track))
        self._refresh_after_track_structure_changed()
        return True

    def _apply_line_layout_indices(self, payload: object) -> None:
        """把项目文件里的每行布局引用套回刚加载的 track。"""
        track = self._timing_track
        if track is None or not isinstance(payload, list):
            return
        limit = len(self._style.layouts)
        for line, value in zip(track.lines, payload):
            try:
                index = int(value)
            except (TypeError, ValueError):
                continue
            line.layout_index = index if 0 <= index <= limit else 0
        self._lyrics_panel.set_style(self._style)
        self._preview_panel.set_style(self._style)

    @staticmethod
    def _line_break_rows(track: Optional[TimingTrack]) -> Optional[list[str]]:
        if track is None:
            return None
        return [str(getattr(line, "break_before", "none")) for line in track.lines]

    def _apply_line_breaks_before(self, payload: object) -> None:
        """恢复 N3 的显式 PageBreak / ParagraphBreak 页边界。"""
        track = self._timing_track
        if track is None or not isinstance(payload, list):
            return
        for line, value in zip(track.lines, payload):
            kind = str(value)
            line.break_before = kind if kind in {"page", "paragraph"} else "none"
        self._lyrics_panel.set_track(track)
        self._preview_panel.set_track(track)

    def _restore_track_page_state(self, track: TimingTrack, payload: object) -> None:
        """Restore schema-v2 page data or migrate schema-v1 line projections."""

        data = payload if isinstance(payload, dict) else {}
        restored = track_page_plan_from_dict(data.get("page_plan"))
        if restored is None:
            saved_breaks = data.get("line_breaks_before")
            has_complete_legacy_breaks = (
                isinstance(saved_breaks, list)
                and len(saved_breaks) >= len(track.lines)
            )
            if not has_complete_legacy_breaks:
                # Early schema-v1 projects did not persist break_before.  At
                # that time the LRC parser always reconstructed N3's default
                # SeqLinesBreaker boundaries (two lines per page).  The modern
                # parser intentionally stays structure-free, so replay that
                # historical rule only for in-memory legacy migration.
                apply_n3_seq_line_breaks(track)
            track.page_plan = build_legacy_page_plan(
                track,
                self._style,
                section_gap_ms=max(int(self._style.section_gap_ms), 0),
            )
            track.loading_settings_mode = "custom"
            track.loading_settings = SubtitleLoadingSettings(
                time_gap_section_enabled=True,
                section_gap_ms=max(int(self._style.section_gap_ms), 0),
                blank_line_section_enabled=False,
                rows_per_page=2,
            )
            track.loading_settings_snapshot = track.loading_settings
        else:
            track.page_plan = normalize_page_plan(track, self._style, restored)
            mode = str(data.get("loading_settings_mode") or "global")
            track.loading_settings_mode = (
                mode if mode in {"global", "custom"} else "global"
            )
            track.loading_settings = (
                subtitle_loading_settings_from_dict(data.get("loading_settings"))
                if track.loading_settings_mode == "custom"
                else None
            )
            track.loading_settings_snapshot = subtitle_loading_settings_from_dict(
                data.get("loading_settings_snapshot")
            )
        project_page_plan_to_legacy_fields(track, self._style)

    @staticmethod
    def _display_override_rows(track: Optional[TimingTrack]) -> Optional[list]:
        """采集逐行显示/隐藏覆盖：与 ``track.lines`` 对齐，无覆盖的行为 None。"""
        if track is None:
            return None
        rows = [
            (
                [line.display_start_override_ms, line.display_end_override_ms]
                if line.display_start_override_ms is not None
                or line.display_end_override_ms is not None
                else None
            )
            for line in track.lines
        ]
        return rows if any(row is not None for row in rows) else None

    @staticmethod
    def _apply_display_override_rows(track: TimingTrack, payload: object) -> None:
        """把项目文件里的逐行显示/隐藏覆盖套回刚加载的 track。"""
        if not isinstance(payload, list):
            return
        for line, row in zip(track.lines, payload):
            if not isinstance(row, (list, tuple)) or len(row) != 2:
                continue
            start, end = row
            line.display_start_override_ms = (
                int(start) if isinstance(start, (int, float)) else None
            )
            line.display_end_override_ms = (
                int(end) if isinstance(end, (int, float)) else None
            )

    @staticmethod
    def _animation_override_rows(track: Optional[TimingTrack]) -> Optional[list]:
        """采集逐行动画覆盖；全部继承全局时不写项目字段。"""
        if track is None:
            return None
        rows = [line_animation_override_to_dict(line.animation_override) for line in track.lines]
        return rows if any(row is not None for row in rows) else None

    @staticmethod
    def _apply_animation_override_rows(track: TimingTrack, payload: object) -> None:
        if not isinstance(payload, list):
            return
        for line, row in zip(track.lines, payload):
            line.animation_override = line_animation_override_from_dict(row)

    def _collect_char_role_labels(self) -> Optional[list]:
        """收集主字幕每行逐字角色标签用于项目持久化；全部为空则返回 None（不写盘）。"""
        if self._timing_track is None:
            return None
        return self._char_role_rows(self._timing_track)

    @staticmethod
    def _char_role_rows(track: TimingTrack) -> Optional[list]:
        rows: list = []
        any_label = False
        for line in track.lines:
            if any(ch.role_label for ch in line.chars):
                any_label = True
                rows.append([ch.role_label for ch in line.chars])
            else:
                rows.append(None)
        return rows if any_label else None

    def _apply_char_role_labels(self, payload: object) -> None:
        """把项目文件 / N3 导入的逐字角色标签套回刚加载的 track。"""
        track = self._timing_track
        if track is None or not isinstance(payload, list):
            return
        changed = False
        for line, labels in zip(track.lines, payload):
            if not isinstance(labels, list):
                continue
            for ch, label in zip(line.chars, labels):
                new_label = str(label) if label else None
                if ch.role_label != new_label:
                    ch.role_label = new_label
                    changed = True
        if not changed:
            return
        self._lyrics_panel.set_track(track)
        self._lyrics_panel.set_role_options(self._merged_role_options())
        self._property_panel.set_roles(self._content_role_options())
        self._preview_panel.set_track(track)

    @staticmethod
    def _guide_symbol_rows(track: Optional[TimingTrack]) -> Optional[list]:
        if track is None:
            return None
        rows = [guide_symbol_to_dict(line.guide_symbol) for line in track.lines]
        return rows if any(row is not None for row in rows) else None

    @staticmethod
    def _inline_guide_symbol_rows(track: Optional[TimingTrack]) -> Optional[list]:
        if track is None:
            return None
        rows = [
            {
                str(index): guide_symbol_to_dict(symbol)
                for index, symbol in sorted(line.inline_guide_symbols.items())
                if 0 <= index < len(line.chars) and symbol.path_commands
            }
            or None
            for line in track.lines
        ]
        return rows if any(row is not None for row in rows) else None

    @staticmethod
    def _apply_guide_symbol_rows(track: TimingTrack, payload: object) -> list[int]:
        if not isinstance(payload, list):
            return []
        mismatches: list[int] = []
        for row, (line, value) in enumerate(zip(track.lines, payload)):
            symbol = guide_symbol_from_dict(value)
            if (
                symbol is not None
                and symbol.replacement_prefix
                and guide_symbol_replacement_count(line, symbol) == 0
            ):
                line.guide_symbol = None
                mismatches.append(row)
                continue
            line.guide_symbol = symbol
        return mismatches

    @staticmethod
    def _apply_inline_guide_symbol_rows(track: TimingTrack, payload: object) -> None:
        if not isinstance(payload, list):
            return
        for line, value in zip(track.lines, payload):
            symbols: dict[int, GuideSymbol] = {}
            if isinstance(value, dict):
                for raw_index, raw_symbol in value.items():
                    try:
                        index = int(raw_index)
                    except (TypeError, ValueError):
                        continue
                    symbol = guide_symbol_from_dict(raw_symbol)
                    if (
                        0 <= index < len(line.chars)
                        and symbol is not None
                        and (
                            symbol.path_commands
                            or (
                                symbol.kind == "bitmap"
                                and bool(symbol.bitmap_before_path)
                            )
                        )
                    ):
                        symbols[index] = symbol
            line.inline_guide_symbols = symbols

    # ------------------------------------------------------- 副字幕源（N3 多歌词文件）

    def _apply_extra_subtitle_sources(self, payload: object) -> None:
        """从项目快照 / N3 导入恢复副字幕源（含每行布局与逐字角色）。"""
        self._extra_sources = []
        self._active_source_index = 0
        self._title_source_active = False
        if isinstance(payload, list):
            layout_limit = len(self._style.layouts)
            for item in payload:
                if not isinstance(item, dict):
                    continue
                path_text = str(item.get("path") or "").strip()
                if not path_text:
                    continue
                path = Path(path_text)
                if not path.is_file():
                    continue
                try:
                    track = self._load_timing_track_file(path)
                except Exception:  # noqa: BLE001 — 单个副源坏了不阻塞项目打开
                    continue
                self._set_subtitle_source_baseline(path, track)
                layout_indices = item.get("line_layout_indices")
                if isinstance(layout_indices, list):
                    for line, value in zip(track.lines, layout_indices):
                        try:
                            index = int(value)
                        except (TypeError, ValueError):
                            continue
                        line.layout_index = index if 0 <= index <= layout_limit else 0
                breaks = item.get("line_breaks_before")
                if isinstance(breaks, list):
                    for line, value in zip(track.lines, breaks):
                        kind = str(value)
                        line.break_before = (
                            kind if kind in {"page", "paragraph"} else "none"
                        )
                role_rows = item.get("char_role_labels")
                if isinstance(role_rows, list):
                    for line, labels in zip(track.lines, role_rows):
                        if not isinstance(labels, list):
                            continue
                        for ch, label in zip(line.chars, labels):
                            ch.role_label = str(label) if label else None
                self._apply_guide_symbol_rows(
                    track, item.get("line_guide_symbols")
                )
                self._apply_inline_guide_symbol_rows(
                    track, item.get("line_inline_guide_symbols")
                )
                self._apply_display_override_rows(
                    track, item.get("line_display_overrides")
                )
                self._apply_animation_override_rows(
                    track, item.get("line_animation_overrides")
                )
                self._restore_track_page_state(track, item)
                name = str(item.get("name") or "").strip() or path.stem
                self._extra_sources.append(
                    ExtraSubtitleSource(name=name, path=path, track=track)
                )
        self._refresh_source_ui()
        self._refresh_lyrics_panel_source()
        self._property_panel.set_roles(self._content_role_options())
        self._sync_extra_tracks_to_preview()
        self._refresh_transport_duration()
        self._sync_subtitle_source_watcher()

    def _all_tracks(self) -> list[TimingTrack]:
        tracks = [] if self._timing_track is None else [self._timing_track]
        tracks.extend(source.track for source in self._extra_sources)
        return tracks

    def _extra_track_list(self) -> list[TimingTrack]:
        return [source.track for source in self._extra_sources]

    def _active_track(self) -> Optional[TimingTrack]:
        """歌词列表当前显示的 track（0 = 主字幕）。"""
        index = self._active_source_index
        if index <= 0:
            return self._timing_track
        if index - 1 < len(self._extra_sources):
            return self._extra_sources[index - 1].track
        return self._timing_track

    def _title_source_index(self) -> Optional[int]:
        title = self._style.title_overlay
        if title is None or not title.enabled or self._timing_track is None:
            return None
        return len(self._extra_sources) + 1

    def _refresh_source_ui(self) -> None:
        """刷新歌词面板的字幕源下拉；无主字幕时隐藏。"""
        if self._timing_track is None:
            self._active_source_index = 0
            self._title_source_active = False
            self._lyrics_panel.set_sources([], 0)
            return
        names = ["主字幕"] + [source.name for source in self._extra_sources]
        title_index = self._title_source_index()
        if title_index is not None:
            names.append("标题")
        self._active_source_index = max(
            0, min(self._active_source_index, len(self._extra_sources))
        )
        active_index = title_index if self._title_source_active and title_index is not None else self._active_source_index
        self._lyrics_panel.set_sources(
            names,
            active_index,
            removable_indices=set(range(1, len(self._extra_sources) + 1)),
        )

    def _refresh_lyrics_panel_source(self) -> None:
        """把当前选中源的行喂给歌词列表。"""
        if self._title_source_active:
            title = self._style.title_overlay
            if title is not None and self._timing_track is not None:
                title = replace(
                    title,
                    text_template=_resolve_title_text(title, self._timing_track),
                )
            self._lyrics_panel.set_title(title)
        else:
            self._lyrics_panel.set_track(self._active_track())
        self._lyrics_panel.set_role_options(self._merged_role_options())

    def _sync_extra_tracks_to_preview(self) -> None:
        self._preview_panel.set_extra_tracks(self._extra_track_list())
        self._sync_tracks_view()

    def _sync_tracks_view(self) -> None:
        """把主 + 副字幕源喂给底部字幕轨道（T1 = 主字幕）。"""
        if self._timing_track is None:
            self._tracks_view.set_tracks([])
            return
        named = [("主字幕", self._timing_track)]
        named.extend((source.name, source.track) for source in self._extra_sources)
        self._tracks_view.set_tracks(named)
        self._refresh_tracks_view_windows()

    def _refresh_tracks_view_windows(self) -> None:
        """按当前样式重算各轨行显示窗口，推给字幕轨道（把手条数据源）。

        这条和余白检查一样跑在 GUI 线程上，且要对每条轨道整轨排版。放进
        ``layout_pass`` 让它享受与 IR 构建同一套整轨缓存，否则同样的分页和逐行
        样式会在这里重算一遍。
        """
        if self._timing_track is None or self._loading_project:
            return
        self._tracks_view.set_style(self._style)
        with layout_pass():
            windows = [
                display_windows_for_style(
                    track,
                    self._style,
                    logical_w=self._screen_settings.width,
                    logical_h=self._screen_settings.height,
                )
                for track in self._all_tracks()
            ]
        self._tracks_view.set_display_windows(windows)

    def _refresh_tracks_view_windows_async(self) -> None:
        """与 ``_refresh_tracks_view_windows`` 同样的结果，只是不在 GUI 线程上算。

        排版本身要跑整轨，真实工程一次约 600ms；放在 GUI 线程上就意味着用户每改
        一次样式，界面都要僵这么久。轨道把手上的窗口本来就是 debounce 之后才更新
        的，晚几百毫秒到达不影响任何东西——但期间界面可以照常用。

        与渲染 IR 的后台构建一样直接读取实时的 track/style 对象；同一时刻只跑一
        个，跑的过程中又来了新请求就在结束后补跑一次，过期结果按代号丢弃。
        """
        if self._timing_track is None or self._loading_project:
            return
        if self._tracks_window_worker_busy:
            self._tracks_window_rerun_pending = True
            return
        self._tracks_window_generation += 1
        generation = self._tracks_window_generation
        tracks = list(self._all_tracks())
        style = self._style
        logical_w = self._screen_settings.width
        logical_h = self._screen_settings.height

        def compute() -> None:
            try:
                with layout_pass():
                    windows = [
                        display_windows_for_style(
                            track,
                            style,
                            logical_w=logical_w,
                            logical_h=logical_h,
                        )
                        for track in tracks
                    ]
            except Exception:
                # 后台重算失败不该拖垮窗口：把手保持上一版数据即可。
                logging.getLogger(__name__).exception(
                    "字幕轨道显示窗口后台重算失败"
                )
                windows = None
            self._tracksViewWindowsReady.emit(generation, windows)

        self._tracks_window_worker_busy = True
        self._tracks_view.set_style(style)
        threading.Thread(
            target=compute, name="tracks-window-refresh", daemon=True
        ).start()

    def _on_tracks_view_windows_ready(self, generation: int, windows: object) -> None:
        self._tracks_window_worker_busy = False
        if self._tracks_window_rerun_pending:
            self._tracks_window_rerun_pending = False
            self._refresh_tracks_view_windows_async()
            return
        if generation != self._tracks_window_generation or windows is None:
            return
        if self._timing_track is None or self._loading_project:
            return
        self._tracks_view.set_display_windows(windows)

    def _schedule_tracks_view_window_refresh(self) -> None:
        if self._timing_track is None or self._loading_project:
            return
        self._tracks_window_refresh_timer.start()

    def _on_display_window_edited(
        self, track_index: int, line_index: int, old_values: object, new_values: object
    ) -> None:
        """字幕轨道拖动把手改了某句显示/隐藏时间：入撤销栈 + 刷新预览 + 标脏。"""
        self._undo_stack.append((track_index, line_index, old_values, new_values))
        del self._undo_stack[:-_UNDO_STACK_LIMIT]
        self._redo_stack.clear()
        self._refresh_after_display_edit(track_index)

    def _on_line_animation_override_requested(
        self, rows: list[int], override: Optional[LineAnimationOverride]
    ) -> None:
        """歌词列表批量修改逐行特效：应用、入撤销栈并立即刷新预览。"""
        track_index = self._active_source_index
        track = self._track_by_index(track_index)
        if track is None:
            return
        valid_rows = sorted({int(row) for row in rows if 0 <= int(row) < len(track.lines)})
        if not valid_rows:
            return
        old_values = tuple(track.lines[row].animation_override for row in valid_rows)
        new_values = tuple(override for _row in valid_rows)
        if old_values == new_values:
            return
        for row in valid_rows:
            track.lines[row].animation_override = override
        self._undo_stack.append(
            ("animation", track_index, tuple(valid_rows), old_values, new_values)
        )
        del self._undo_stack[:-_UNDO_STACK_LIMIT]
        self._redo_stack.clear()
        self._refresh_after_display_edit(track_index)
        for row in valid_rows:
            self._lyrics_panel.refresh_row_effect(row)
        if hasattr(self, "_style") and hasattr(self, "_transport_bar"):
            first_window = display_windows_for_style(
                track,
                self._style,
                logical_w=self._screen_settings.width,
                logical_h=self._screen_settings.height,
            ).get(valid_rows[0])
            if first_window is not None:
                self._transport_bar.set_time(max(first_window[0], 0))

    def _refresh_after_display_edit(self, track_index: int) -> None:
        # 覆盖值已直接写在 TimingLine 上；track 是原地修改的，
        # 预览（含异步渲染 worker）不会自己发现——重新喂一次。
        if track_index == 0 and self._timing_track is not None:
            self._preview_panel.set_track(self._timing_track)
        elif track_index > 0:
            # 不走 _sync_extra_tracks_to_preview：它会重建轨道视图、丢掉选中态
            self._preview_panel.set_extra_tracks(self._extra_track_list())
        self._schedule_tracks_view_window_refresh()
        self._mark_project_dirty()

    def _track_by_index(self, track_index: int) -> Optional[TimingTrack]:
        if track_index == 0:
            return self._timing_track
        if 1 <= track_index <= len(self._extra_sources):
            return self._extra_sources[track_index - 1].track
        return None

    def _restore_display_override(
        self, track_index: int, line_index: int, values: object
    ) -> bool:
        track = self._track_by_index(track_index)
        if (
            track is None
            or not isinstance(values, tuple)
            or not 0 <= line_index < len(track.lines)
        ):
            return False
        start, end = values
        line = track.lines[line_index]
        line.display_start_override_ms = start
        line.display_end_override_ms = end
        self._refresh_after_display_edit(track_index)
        return True

    def _restore_animation_overrides(
        self, track_index: int, rows: object, values: object
    ) -> bool:
        track = self._track_by_index(track_index)
        if track is None or not isinstance(rows, tuple) or not isinstance(values, tuple):
            return False
        if len(rows) != len(values) or any(not 0 <= row < len(track.lines) for row in rows):
            return False
        for row, value in zip(rows, values):
            track.lines[row].animation_override = value
        self._refresh_after_display_edit(track_index)
        if track_index == self._active_source_index:
            for row in rows:
                self._lyrics_panel.refresh_row_effect(row)
        return True

    def _restore_guide_symbols(
        self, track_index: int, rows: object, values: object
    ) -> bool:
        track = self._track_by_index(track_index)
        if track is None or not isinstance(rows, tuple) or not isinstance(values, tuple):
            return False
        if len(rows) != len(values) or any(not 0 <= row < len(track.lines) for row in rows):
            return False
        for row, value in zip(rows, values):
            track.lines[row].guide_symbol = value
        if track_index == self._active_source_index:
            self._refresh_after_guide_symbols_changed(rows)
        else:
            self._sync_extra_tracks_to_preview()
            self._mark_project_dirty()
        return True

    def _restore_guide_char_roles(
        self, track_index: int, row: int, value: object
    ) -> bool:
        track = self._track_by_index(track_index)
        if (
            track is None
            or not 0 <= row < len(track.lines)
            or not isinstance(value, tuple)
            or len(value) != 2
        ):
            return False
        symbol, labels = value
        line = track.lines[row]
        if not isinstance(labels, tuple) or len(labels) != len(line.chars):
            return False
        line.guide_symbol = symbol
        for char, label in zip(line.chars, labels):
            char.role_label = label
        if track_index == self._active_source_index:
            self._refresh_after_guide_symbols_changed((row,))
        else:
            self._sync_extra_tracks_to_preview()
            self._mark_project_dirty()
        return True

    def _restore_inline_char_edit(
        self, track_index: int, row: int, value: object
    ) -> bool:
        track = self._track_by_index(track_index)
        if (
            track is None
            or not 0 <= row < len(track.lines)
            or not isinstance(value, tuple)
            or len(value) != 3
        ):
            return False
        symbol, labels, inline_values = value
        line = track.lines[row]
        if (
            not isinstance(labels, tuple)
            or len(labels) != len(line.chars)
            or not isinstance(inline_values, tuple)
        ):
            return False
        inline_symbols: dict[int, GuideSymbol] = {}
        for item in inline_values:
            if (
                not isinstance(item, tuple)
                or len(item) != 2
                or not isinstance(item[0], int)
                or not 0 <= item[0] < len(line.chars)
                or not isinstance(item[1], GuideSymbol)
                or not item[1].path_commands
            ):
                return False
            inline_symbols[item[0]] = item[1]
        line.guide_symbol = symbol
        line.inline_guide_symbols = inline_symbols
        for char, label in zip(line.chars, labels):
            char.role_label = label
        if track_index == self._active_source_index:
            self._refresh_after_guide_symbols_changed((row,))
        else:
            self._sync_extra_tracks_to_preview()
            self._mark_project_dirty()
        return True

    def _restore_guide_replacement_rows(
        self, track_index: int, rows: object, values: object
    ) -> bool:
        track = self._track_by_index(track_index)
        if (
            track is None
            or not isinstance(rows, tuple)
            or not isinstance(values, tuple)
            or len(rows) != len(values)
            or any(not 0 <= row < len(track.lines) for row in rows)
        ):
            return False
        restored: list[tuple[object, dict[int, GuideSymbol]]] = []
        for row, value in zip(rows, values):
            if not isinstance(value, tuple) or len(value) != 2:
                return False
            guide, inline_values = value
            if guide is not None and not isinstance(guide, GuideSymbol):
                return False
            if not isinstance(inline_values, tuple):
                return False
            inline_symbols: dict[int, GuideSymbol] = {}
            for item in inline_values:
                if (
                    not isinstance(item, tuple)
                    or len(item) != 2
                    or not isinstance(item[0], int)
                    or not 0 <= item[0] < len(track.lines[row].chars)
                    or not isinstance(item[1], GuideSymbol)
                    or not item[1].path_commands
                ):
                    return False
                inline_symbols[item[0]] = item[1]
            restored.append((guide, inline_symbols))
        for row, (guide, inline_symbols) in zip(rows, restored):
            track.lines[row].guide_symbol = guide
            track.lines[row].inline_guide_symbols = inline_symbols
        if track_index == self._active_source_index:
            self._refresh_after_guide_symbols_changed(rows)
        else:
            self._sync_extra_tracks_to_preview()
            self._mark_project_dirty()
        return True

    def _restore_inline_role_rows(
        self, track_index: int, rows: object, values: object
    ) -> bool:
        track = self._track_by_index(track_index)
        if track is None or not isinstance(rows, tuple) or not isinstance(values, tuple):
            return False
        if len(rows) != len(values) or any(not 0 <= row < len(track.lines) for row in rows):
            return False
        for row, value in zip(rows, values):
            if not isinstance(value, tuple) or len(value) != 2:
                return False
            symbol, labels = value
            if not isinstance(labels, tuple) or len(labels) != len(track.lines[row].chars):
                return False
            track.lines[row].guide_symbol = symbol
            for char, label in zip(track.lines[row].chars, labels):
                char.role_label = label
        if track_index == self._active_source_index:
            self._refresh_after_guide_symbols_changed(rows)
        else:
            self._sync_extra_tracks_to_preview()
            self._mark_project_dirty()
        return True

    def _undo_edit(self) -> None:
        """Ctrl+Z：撤销最近一次样式（字体/布局等）、轨道时间或逐行特效编辑。"""
        while self._undo_stack:
            command = self._undo_stack.pop()
            if command[0] == "track_snapshot":
                _kind, track_index, old_track, _new_track = command
                if self._restore_track_snapshot(track_index, old_track):
                    self._redo_stack.append(command)
                    return
                continue
            if command[0] == "tracks_snapshot":
                _kind, indices, old_tracks, _new_tracks = command
                if self._restore_tracks_snapshot(indices, old_tracks):
                    self._redo_stack.append(command)
                    return
                continue
            if command[0] == "style_tracks":
                _kind, old_style, _new_style, indices, old_tracks, _new_tracks = command
                if self._restore_style_and_tracks(old_style, indices, old_tracks):
                    self._redo_stack.append(command)
                    return
                continue
            if command[0] == "style":
                if self._restore_style(command[1]):
                    self._redo_stack.append(command)
                    return
                continue
            if command[0] == "char_roles":
                _kind, track_index, row, old_labels, _new_labels = command
                if self._restore_char_roles(track_index, row, old_labels):
                    self._redo_stack.append(command)
                    return
                continue
            if command[0] == "char_roles_batch":
                _kind, track_index, rows, old_values, _new_values = command
                if self._restore_char_role_rows(track_index, rows, old_values):
                    self._redo_stack.append(command)
                    return
                continue
            if command[0] == "guide_symbols":
                _kind, track_index, rows, old_values, _new_values = command
                if self._restore_guide_symbols(track_index, rows, old_values):
                    self._redo_stack.append(command)
                    return
                continue
            if command[0] == "guide_char_roles":
                _kind, track_index, row, old_value, _new_value = command
                if self._restore_guide_char_roles(track_index, row, old_value):
                    self._redo_stack.append(command)
                    return
                continue
            if command[0] == "inline_char_edit":
                _kind, track_index, row, old_value, _new_value = command
                if self._restore_inline_char_edit(track_index, row, old_value):
                    self._redo_stack.append(command)
                    return
                continue
            if command[0] == "guide_replacements":
                _kind, track_index, rows, old_values, _new_values = command
                if self._restore_guide_replacement_rows(
                    track_index, rows, old_values
                ):
                    self._redo_stack.append(command)
                    return
                continue
            if command[0] == "inline_roles_batch":
                _kind, track_index, rows, old_values, _new_values = command
                if self._restore_inline_role_rows(track_index, rows, old_values):
                    self._redo_stack.append(command)
                    return
                continue
            if len(command) == 5 and command[0] == "animation":
                _kind, track_index, rows, old_values, new_values = command
                if self._restore_animation_overrides(track_index, rows, old_values):
                    self._redo_stack.append(command)
                    return
                continue
            track_index, line_index, old_values, new_values = command
            if self._restore_display_override(track_index, line_index, old_values):
                self._redo_stack.append(
                    (track_index, line_index, old_values, new_values)
                )
                return
            # 目标轨道/行已不存在（换源等）→ 丢弃该条继续往下找

    def _redo_edit(self) -> None:
        """Ctrl+Y / Ctrl+Shift+Z：重做被撤销的样式或字幕轨道编辑。"""
        while self._redo_stack:
            command = self._redo_stack.pop()
            if command[0] == "track_snapshot":
                _kind, track_index, _old_track, new_track = command
                if self._restore_track_snapshot(track_index, new_track):
                    self._undo_stack.append(command)
                    return
                continue
            if command[0] == "tracks_snapshot":
                _kind, indices, _old_tracks, new_tracks = command
                if self._restore_tracks_snapshot(indices, new_tracks):
                    self._undo_stack.append(command)
                    return
                continue
            if command[0] == "style_tracks":
                _kind, _old_style, new_style, indices, _old_tracks, new_tracks = command
                if self._restore_style_and_tracks(new_style, indices, new_tracks):
                    self._undo_stack.append(command)
                    return
                continue
            if command[0] == "style":
                if self._restore_style(command[2]):
                    self._undo_stack.append(command)
                    return
                continue
            if command[0] == "char_roles":
                _kind, track_index, row, _old_labels, new_labels = command
                if self._restore_char_roles(track_index, row, new_labels):
                    self._undo_stack.append(command)
                    return
                continue
            if command[0] == "char_roles_batch":
                _kind, track_index, rows, _old_values, new_values = command
                if self._restore_char_role_rows(track_index, rows, new_values):
                    self._undo_stack.append(command)
                    return
                continue
            if command[0] == "guide_symbols":
                _kind, track_index, rows, _old_values, new_values = command
                if self._restore_guide_symbols(track_index, rows, new_values):
                    self._undo_stack.append(command)
                    return
                continue
            if command[0] == "guide_char_roles":
                _kind, track_index, row, _old_value, new_value = command
                if self._restore_guide_char_roles(track_index, row, new_value):
                    self._undo_stack.append(command)
                    return
                continue
            if command[0] == "inline_char_edit":
                _kind, track_index, row, _old_value, new_value = command
                if self._restore_inline_char_edit(track_index, row, new_value):
                    self._undo_stack.append(command)
                    return
                continue
            if command[0] == "guide_replacements":
                _kind, track_index, rows, _old_values, new_values = command
                if self._restore_guide_replacement_rows(
                    track_index, rows, new_values
                ):
                    self._undo_stack.append(command)
                    return
                continue
            if command[0] == "inline_roles_batch":
                _kind, track_index, rows, _old_values, new_values = command
                if self._restore_inline_role_rows(track_index, rows, new_values):
                    self._undo_stack.append(command)
                    return
                continue
            if len(command) == 5 and command[0] == "animation":
                _kind, track_index, rows, old_values, new_values = command
                if self._restore_animation_overrides(track_index, rows, new_values):
                    self._undo_stack.append(command)
                    return
                continue
            track_index, line_index, old_values, new_values = command
            if self._restore_display_override(track_index, line_index, new_values):
                self._undo_stack.append(
                    (track_index, line_index, old_values, new_values)
                )
                return

    def _clear_undo_history(self) -> None:
        self._undo_stack.clear()
        self._redo_stack.clear()

    def _effective_loading_settings(self, track: TimingTrack) -> SubtitleLoadingSettings:
        if track.loading_settings_mode == "custom" and track.loading_settings is not None:
            return track.loading_settings
        return self._subtitle_loading_defaults

    def _source_path_for_track_index(self, track_index: int) -> Optional[Path]:
        if track_index == 0:
            return self._subtitle_path
        if 1 <= track_index <= len(self._extra_sources):
            return self._extra_sources[track_index - 1].path
        return None

    def _set_track_by_index(self, track_index: int, track: TimingTrack) -> bool:
        if track_index == 0:
            self._timing_track = track
            return True
        if 1 <= track_index <= len(self._extra_sources):
            self._extra_sources[track_index - 1].track = track
            return True
        return False

    def _refresh_after_track_structure_changed(self) -> None:
        self._refresh_source_ui()
        self._refresh_lyrics_panel_source()
        if self._timing_track is not None:
            self._preview_panel.set_track(self._timing_track)
        self._sync_extra_tracks_to_preview()
        self._refresh_tracks_view_windows()
        self._refresh_transport_duration()
        self._margin_check_timer.start()
        self._mark_project_dirty()

    def _restore_track_snapshot(self, track_index: int, value: object) -> bool:
        if not isinstance(value, TimingTrack):
            return False
        if not self._set_track_by_index(track_index, deepcopy(value)):
            return False
        self._refresh_after_track_structure_changed()
        return True

    def _restore_tracks_snapshot(self, indices: object, values: object) -> bool:
        if not isinstance(indices, tuple) or not isinstance(values, tuple):
            return False
        if len(indices) != len(values):
            return False
        if any(
            not isinstance(index, int) or not isinstance(value, TimingTrack)
            for index, value in zip(indices, values)
        ):
            return False
        for index, value in zip(indices, values):
            if not self._set_track_by_index(index, deepcopy(value)):
                return False
        self._refresh_after_track_structure_changed()
        return True

    def _record_track_snapshot(
        self, track_index: int, before: TimingTrack, after: TimingTrack
    ) -> None:
        self._undo_stack.append(
            ("track_snapshot", int(track_index), deepcopy(before), deepcopy(after))
        )
        del self._undo_stack[:-_UNDO_STACK_LIMIT]
        self._redo_stack.clear()

    def _on_page_boundary_requested(self, action: str, track_line_index: int) -> None:
        track = self._active_track()
        if track is None or self._title_source_active:
            return
        before = deepcopy(track)
        operation = {
            "insert_page": ("page", True),
            "delete_page": ("page", False),
            "insert_section": ("paragraph", True),
            "delete_section": ("paragraph", False),
        }.get(str(action))
        if operation is None:
            return
        kind, inserting = operation
        changed = (
            insert_boundary(
                track, self._style, int(track_line_index), kind=kind
            )
            if inserting
            else delete_boundary(
                track, self._style, int(track_line_index), kind=kind
            )
        )
        if not changed:
            fluent_info(
                self,
                "无法修改边界",
                "当前位置没有可修改的边界，或合并后的页面超过 8 行。",
            )
            return
        self._record_track_snapshot(
            self._active_source_index, before, deepcopy(track)
        )
        self._refresh_after_track_structure_changed()

    def _on_page_move_requested(
        self, section_index: int, page_index: int, direction: int
    ) -> None:
        track = self._active_track()
        if track is None or self._title_source_active:
            return
        before = deepcopy(track)
        if not move_page_boundary(
            track,
            self._style,
            int(section_index),
            int(page_index),
            direction=int(direction),
        ):
            fluent_info(
                self,
                "无法移动歌词行",
                "目标页面已经达到 8 行、相邻页面不存在，或当前行不是可移动的分页边界行。",
            )
            return
        self._record_track_snapshot(
            self._active_source_index, before, deepcopy(track)
        )
        self._refresh_after_track_structure_changed()

    def _build_refreshed_track(
        self,
        track_index: int,
        settings: SubtitleLoadingSettings,
        *,
        mode: str,
    ) -> tuple[TimingTrack, Optional[TimingTrack], tuple[str, ...]]:
        current = self._track_by_index(track_index)
        if current is None:
            raise ValueError("字幕源不存在")
        path = self._source_path_for_track_index(track_index)
        parsed_source: Optional[TimingTrack] = None
        if path is not None and path.is_file():
            parsed_source = self._load_timing_track_file(path)
            state = self._source_watch_states.get(self._subtitle_source_key(path))
            baseline = state.baseline if state is not None else current
            merge = merge_reloaded_track(
                current,
                baseline,
                parsed_source,
                preserve_page_structure=False,
            )
            refreshed = merge.track
            conflicts = merge.conflicts
        else:
            refreshed = deepcopy(current)
            for line in refreshed.lines:
                line.layout_index = 0
                line.break_before = "none"
            conflicts = ()
        refreshed.loading_settings_mode = (
            mode if mode in {"global", "custom"} else current.loading_settings_mode
        )
        refreshed.loading_settings = settings if refreshed.loading_settings_mode == "custom" else None
        refreshed.loading_settings_snapshot = settings
        refreshed.page_plan = build_page_plan(refreshed, settings, self._style)
        project_page_plan_to_legacy_fields(refreshed, self._style)
        return refreshed, parsed_source, conflicts

    def _refresh_track_indices(
        self,
        indices: list[int],
        settings_by_index: dict[int, tuple[str, SubtitleLoadingSettings]],
    ) -> bool:
        indices = list(dict.fromkeys(int(index) for index in indices))
        tracks = [
            self._track_by_index(index)
            for index in indices
        ]
        if not indices or any(track is None for track in tracks):
            return False
        manual_count = sum(
            1
            for track in tracks
            if track is not None
            and page_plan_has_manual_changes(track, self._style)
        )
        if manual_count and not fluent_question(
            self,
            "重新生成段落和页面",
            f"{manual_count} 个字幕源包含手工分页、分段或逐页布局。刷新会按加载设置"
            "重新生成这些结构，但会尽量保留角色、特效、导唱符和显示时间。是否继续？",
            yes_text="刷新",
            no_text="取消",
            default_cancel=True,
        ):
            return False
        prepared: list[tuple[int, TimingTrack, Optional[TimingTrack]]] = []
        conflicts: list[str] = []
        try:
            for index in indices:
                mode, settings = settings_by_index[index]
                refreshed, parsed, merge_conflicts = self._build_refreshed_track(
                    index, settings, mode=mode
                )
                prepared.append((index, refreshed, parsed))
                conflicts.extend(merge_conflicts)
        except Exception as exc:  # noqa: BLE001 - transactional refresh
            fluent_error(
                self,
                "刷新字幕失败",
                f"无法重新读取字幕，当前项目未作任何更改。\n\n错误：{exc}",
            )
            return False
        if conflicts:
            details = "\n".join(f"• {item}" for item in dict.fromkeys(conflicts))
            if not fluent_question(
                self,
                "部分设置无法匹配",
                f"{details}\n\n是否继续使用能够可靠匹配的设置？",
                yes_text="继续刷新",
                no_text="取消",
                default_cancel=True,
            ):
                return False

        before = tuple(deepcopy(track) for track in tracks if track is not None)
        for index, refreshed, parsed in prepared:
            self._set_track_by_index(index, refreshed)
            path = self._source_path_for_track_index(index)
            if path is not None and parsed is not None:
                self._set_subtitle_source_baseline(path, parsed)
        after = tuple(
            deepcopy(self._track_by_index(index)) for index in indices
        )
        self._undo_stack.append(("tracks_snapshot", tuple(indices), before, after))
        del self._undo_stack[:-_UNDO_STACK_LIMIT]
        self._redo_stack.clear()
        self._refresh_after_track_structure_changed()
        return True

    def _on_source_refresh_requested(self) -> None:
        track = self._active_track()
        if track is None or self._title_source_active:
            return
        settings = self._effective_loading_settings(track)
        if self._refresh_track_indices(
            [self._active_source_index],
            {
                self._active_source_index: (
                    track.loading_settings_mode,
                    settings,
                )
            },
        ):
            InfoBar.success(
                title="字幕已刷新",
                content="已按保存的加载设置重新生成段落、页面和按行数布局。",
                parent=self,
                position=InfoBarPosition.BOTTOM_RIGHT,
                duration=3000,
            )

    def _on_source_settings_requested(self, anchor: object) -> None:
        track = self._active_track()
        if track is None or self._title_source_active:
            return
        dialog = _SubtitleLoadingSettingsDialog(
            mode=track.loading_settings_mode,
            effective=self._effective_loading_settings(track),
            global_defaults=self._subtitle_loading_defaults,
            anchor=anchor if isinstance(anchor, QWidget) else None,
            parent=self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        mode, settings = dialog.result_value()
        if mode == "global":
            affected = [
                index
                for index, item in enumerate(self._all_tracks())
                if item.loading_settings_mode == "global"
                or index == self._active_source_index
            ]
            settings_map = {
                index: ("global", settings) for index in affected
            }
            if not self._refresh_track_indices(affected, settings_map):
                return
            self._subtitle_loading_defaults = settings
        else:
            if not self._refresh_track_indices(
                [self._active_source_index],
                {self._active_source_index: ("custom", settings)},
            ):
                return
        self._save_persisted_state()

    def _on_source_selected(self, index: int) -> None:
        index = max(int(index), 0)
        title_index = self._title_source_index()
        self._title_source_active = title_index is not None and index == title_index
        if not self._title_source_active:
            self._active_source_index = min(index, len(self._extra_sources))
        self._refresh_lyrics_panel_source()

    def _on_source_add_requested(self) -> None:
        if self._timing_track is None:
            fluent_info(self, "先加载主字幕", "请先加载主字幕文件，再添加副字幕源。")
            return
        start_dir = str(self._subtitle_path.parent) if self._subtitle_path else ""
        path_str, _ = QFileDialog.getOpenFileName(
            self, "添加副字幕源（与主字幕同时显示）", start_dir, SUBTITLE_FILTER
        )
        if not path_str:
            return
        path = Path(path_str)
        try:
            track = self._load_timing_track_file(path)
        except Exception as exc:  # noqa: BLE001 — 统一错误弹窗
            fluent_error(
                self, "加载字幕失败", f"无法解析字幕文件：\n{path}\n\n错误：{exc}"
            )
            return
        track.loading_settings_mode = "global"
        track.loading_settings_snapshot = self._subtitle_loading_defaults
        track.page_plan = build_page_plan(
            track, self._subtitle_loading_defaults, self._style
        )
        project_page_plan_to_legacy_fields(track, self._style)
        self._apply_remembered_layout_assignment(track)
        self._apply_imported_role_preset_choices(track.role_options)
        self._set_subtitle_source_baseline(path, track)
        self._extra_sources.append(
            ExtraSubtitleSource(name=path.stem, path=path, track=track)
        )
        self._active_source_index = len(self._extra_sources)
        self._refresh_source_ui()
        self._refresh_lyrics_panel_source()
        self._property_panel.merge_roles(self._content_role_options())
        self._lyrics_panel.set_role_options(self._merged_role_options())
        self._sync_extra_tracks_to_preview()
        self._refresh_transport_duration()
        self._margin_check_timer.start()
        self._sync_subtitle_source_watcher()
        self._mark_project_dirty()

    @staticmethod
    def _load_timing_track_file(path: Path) -> TimingTrack:
        if path.suffix.lower() == ".sug":
            return load_sug_timing_track(path)
        return load_nicokara_lrc(path)

    def _on_source_remove_requested(self, index: int) -> None:
        extra_index = int(index) - 1
        if not 0 <= extra_index < len(self._extra_sources):
            return
        source = self._extra_sources[extra_index]
        confirmed = fluent_question(
            self,
            "移除副字幕源",
            f"确定移除副字幕源「{source.name}」？\n（不会删除歌词文件本身）",
            yes_text="移除",
            no_text="取消",
            default_cancel=True,
        )
        if not confirmed:
            return
        del self._extra_sources[extra_index]
        self._active_source_index = 0
        # 副字幕源序号整体前移，撤销记录里的轨道序号失效
        self._clear_undo_history()
        self._refresh_source_ui()
        self._refresh_lyrics_panel_source()
        self._sync_extra_tracks_to_preview()
        self._refresh_transport_duration()
        self._margin_check_timer.start()
        self._sync_subtitle_source_watcher()
        self._mark_project_dirty()

    def _rescale_layout_for_height(self, new_height: int) -> None:
        """按 N3 SizeAndRatio 语义重算字体与布局像素字段。"""
        rescaled = rescale_layout_sizes(self._style, new_height)
        rescaled = rescale_font_sizes(rescaled, new_height)
        if rescaled is self._style:
            return
        self._style = rescaled
        self._property_panel.set_style(self._style)
        self._apply_style(self._style)

    def _on_layout_change_requested(self, rows: list, layout_index: int) -> None:
        """歌词列表右键应用布局：对每个选中行按页联动写入（作用于当前选中源）。"""
        if self._title_source_active:
            title = self._style.title_overlay
            if title is None:
                return
            index = max(0, min(int(layout_index), len(self._style.layouts)))
            if int(title.layout_index or 0) == index:
                return
            self._property_panel.set_style(
                replace(
                    self._style,
                    title_overlay=replace(title, layout_index=index),
                ),
                emit=True,
            )
            return
        track = self._active_track()
        if track is None:
            return
        before = deepcopy(track)
        changed: set[int] = set()
        for row in rows:
            if isinstance(row, int) and 0 <= row < len(track.lines):
                changed.update(
                    apply_layout_to_page(track, self._style, row, int(layout_index))
                )
        if changed:
            self._record_track_snapshot(
                self._active_source_index, before, deepcopy(track)
            )
            self._refresh_after_layout_assignment()

    def _on_layout_assign_all(self, layout_index: int) -> None:
        track = self._active_track()
        if track is None:
            return
        if track.page_plan is not None:
            resolved = resolve_page_plan(track, self._style)
            layout_id = layout_id_for_index(self._style, int(layout_index))
            capacity = layout_capacity(self._style, layout_id)
            max_rows = max(
                (page.line_count for page in resolved.pages),
                default=0,
            )
            if capacity < max_rows:
                fluent_warning(
                    self,
                    "布局无法应用到全部页",
                    f"当前布局只能容纳 {capacity} 行，但字幕中存在 {max_rows} 行页面。"
                    "请改用更大布局，或先调整分页。",
                )
                return
        before = deepcopy(track)
        self._remember_layout_assignment("all", int(layout_index))
        if assign_layout_to_all(track, int(layout_index), self._style):
            self._record_track_snapshot(
                self._active_source_index, before, deepcopy(track)
            )
            self._refresh_after_layout_assignment()

    def _on_layout_auto_assign(self) -> None:
        track = self._active_track()
        if track is None:
            return
        before = deepcopy(track)
        self._remember_layout_assignment("auto")
        if auto_assign_layouts_by_page(track, self._style):
            self._record_track_snapshot(
                self._active_source_index, before, deepcopy(track)
            )
            self._refresh_after_layout_assignment()

    def _on_layout_deleted(self, deleted_index: int) -> None:
        """布局被删除后修正歌词行引用（全部字幕源）：被删的回默认，其后的序号前移。"""
        track_indices = tuple(range(len(self._all_tracks())))
        tracks_before = tuple(deepcopy(track) for track in self._all_tracks())
        changed = False
        for track in self._all_tracks():
            if track.page_plan is not None:
                previous_plan = deepcopy(track.page_plan)
                track.page_plan = normalize_page_plan(track, self._style)
                project_page_plan_to_legacy_fields(track, self._style)
                changed |= track.page_plan != previous_plan
                continue
            for line in track.lines:
                index = int(getattr(line, "layout_index", 0) or 0)
                if index == deleted_index:
                    line.layout_index = 0
                    changed = True
                elif index > deleted_index:
                    line.layout_index = index - 1
                    changed = True
        if changed:
            top = self._undo_stack[-1] if self._undo_stack else None
            if top is not None and top[0] == "style":
                self._undo_stack[-1] = (
                    "style_tracks",
                    top[1],
                    top[2],
                    track_indices,
                    tracks_before,
                    tuple(deepcopy(track) for track in self._all_tracks()),
                )
            InfoBar.warning(
                title="已替换被删除的布局",
                content="引用该布局的页面已改用能够容纳当前行数的项目布局。",
                parent=self,
                position=InfoBarPosition.BOTTOM_RIGHT,
                duration=5000,
            )
            self._refresh_after_layout_assignment()

    def _refresh_after_layout_assignment(self) -> None:
        # track 是就地修改的，set_style 只为触发预览/列表重绘；副轨需重喂 worker。
        self._preview_panel.set_style(self._style)
        self._lyrics_panel.set_style(self._style)
        self._sync_extra_tracks_to_preview()
        self._margin_check_timer.start()
        self._mark_project_dirty()

    def _collect_layout_issues(self) -> list[_LayoutIssue]:
        """Collect margin warnings with enough source identity for navigation."""
        tracks = self._all_tracks()
        source_names = ["主字幕", *(source.name for source in self._extra_sources)]
        issues: list[_LayoutIssue] = []
        for track_index, track in enumerate(tracks):
            warnings = check_layout_margins(
                track,
                self._style,
                self._screen_settings.width,
            )
            source_name = (
                source_names[track_index]
                if track_index < len(source_names)
                else f"字幕源 {track_index + 1}"
            )
            issues.extend(
                _LayoutIssue(
                    track_index=track_index,
                    source_name=source_name,
                    warning=warning,
                )
                for warning in warnings
                if 0 <= warning.line_index < len(track.lines)
            )
        return issues

    def _set_layout_issues(self, issues: list[_LayoutIssue]) -> None:
        self._layout_issues = list(issues)
        if hasattr(self, "_layout_issues_button"):
            count = len(issues)
            self._layout_issues_button.setToolTip(
                f"当前歌词问题（{count} 条）" if count else "当前歌词没有布局问题"
            )
            self._layout_issues_button.setAccessibleName(
                f"当前歌词问题，{count} 条" if count else "当前歌词没有布局问题"
            )
            self._layout_issues_button.setVisible(bool(issues))
        if self._layout_issues_dialog is not None:
            self._layout_issues_dialog.set_issues(self._layout_issues)

    def _show_layout_issues(self) -> None:
        """Open or focus the persistent list of current lyrics problems."""
        try:
            self._set_layout_issues(self._collect_layout_issues())
        except Exception:  # noqa: BLE001 — diagnostics must not block editing
            logging.getLogger(__name__).warning(
                "刷新歌词布局问题失败", exc_info=True
            )
        if not self._layout_issues:
            fluent_info(self, "当前歌词问题", "没有发现歌词溢出或侵入左右余白的问题。")
            return
        if self._layout_issues_dialog is None:
            dialog = _LayoutIssuesDialog(self._layout_issues, self)
            dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            dialog.issueActivated.connect(self._jump_to_layout_issue)
            dialog.destroyed.connect(self._on_layout_issues_dialog_destroyed)
            self._layout_issues_dialog = dialog
        else:
            self._layout_issues_dialog.set_issues(self._layout_issues)
        self._layout_issues_dialog.show()
        self._layout_issues_dialog.raise_()
        self._layout_issues_dialog.activateWindow()

    def _on_layout_issues_dialog_destroyed(self, _object: object = None) -> None:
        self._layout_issues_dialog = None

    def _jump_to_layout_issue(self, track_index: int, line_index: int) -> None:
        """Switch source, select its row, seek, and reveal the preview window."""
        track = self._track_by_index(track_index)
        if track is None or not 0 <= line_index < len(track.lines):
            return
        self._title_source_active = False
        self._active_source_index = max(
            0,
            min(int(track_index), len(self._extra_sources)),
        )
        self._refresh_source_ui()
        self._refresh_lyrics_panel_source()
        self._lyrics_panel.select_row(line_index)
        self._on_lyrics_row_clicked(line_index)
        self._show_preview_window()

    def _check_layout_margins(self) -> None:
        """N3 式左右余白检查（全部字幕源）：溢出画面 → Warning；侵入余白 → Information。"""
        if not self._all_tracks():
            self._set_layout_issues([])
            self._last_margin_warning_key = ""
            return
        try:
            # 余白检查同样要整轨排版，且在 GUI 线程上；共用整轨缓存。
            with layout_pass():
                issues = self._collect_layout_issues()
        except Exception:  # noqa: BLE001 — 检查失败不影响正常编辑
            return
        self._set_layout_issues(issues)
        overflow = [
            issue.warning
            for issue in issues
            if issue.warning.level == "overflow"
        ]
        margin = [
            issue.warning
            for issue in issues
            if issue.warning.level == "margin"
        ]
        key = "|".join(
            f"{issue.track_index}:{issue.warning.line_index}:{issue.warning.level}"
            for issue in issues
        )
        if key == self._last_margin_warning_key:
            return
        self._last_margin_warning_key = key
        if overflow:
            InfoBar.warning(
                title="字幕溢出画面",
                content=f"{_format_warning_lines(overflow)}超出画面范围，"
                "请调小字号或缩短该行。",
                parent=self,
                position=InfoBarPosition.BOTTOM_RIGHT,
                duration=5000,
            )
        elif margin:
            InfoBar.info(
                title="左右余白无法确保",
                content=f"{_format_warning_lines(margin)}侵入左右余白。",
                parent=self,
                position=InfoBarPosition.BOTTOM_RIGHT,
                duration=4000,
            )

    def _apply_style_presets(self, presets: dict) -> None:
        self._style_presets = _style_presets_from_dict(presets)
        if hasattr(self, "_lyrics_panel") and self._lyrics_panel is not None:
            self._lyrics_panel.set_role_options(self._merged_role_options())
        self._save_persisted_state()

    def _on_export_screen_changed(self) -> None:
        if self._syncing_screen_controls:
            return
        self._screen_settings = ScreenSettings(
            preset_key="custom",
            par=self._screen_settings.par,
            width=self._export_width_spin.value(),
            height=self._export_height_spin.value(),
            fps=self._export_fps_value(),
        )
        self._screen_settings = ScreenSettings(
            preset_key=match_screen_preset_key(
                self._screen_settings.width,
                self._screen_settings.height,
                self._screen_settings.par,
            ),
            par=self._screen_settings.par,
            width=self._screen_settings.width,
            height=self._screen_settings.height,
            fps=self._screen_settings.fps,
        )
        self._transport_bar.set_preview_fps(self._screen_settings.fps)
        self._rescale_layout_for_height(self._screen_settings.height)
        self._property_panel.set_output_size(
            self._screen_settings.width,
            self._screen_settings.height,
        )
        self._margin_check_timer.start()
        self._schedule_persisted_state_save()
        self._mark_project_dirty()

    def _set_export_screen_controls(self, settings: ScreenSettings) -> None:
        self._syncing_screen_controls = True
        try:
            self._export_width_spin.setValue(settings.width)
            self._export_height_spin.setValue(settings.height)
            self._set_export_fps_value(settings.fps)
        finally:
            self._syncing_screen_controls = False
        if hasattr(self, "_property_panel"):
            self._property_panel.set_output_size(settings.width, settings.height)

    def _export_fps_value(self) -> int:
        data = self._export_fps_combo.currentData()
        return int(data) if data in SCREEN_FPS_OPTIONS else 60

    def _set_export_fps_value(self, fps: int) -> None:
        index = self._export_fps_combo.findData(fps)
        self._export_fps_combo.setCurrentIndex(index if index >= 0 else 0)

    def _on_output_settings_changed(self) -> None:
        if self._loading_project:
            return
        self._remember_local_export_defaults()
        self._schedule_persisted_state_save()
        self._mark_project_dirty()

    def _on_render_workers_changed(self) -> None:
        """Persist this hardware-specific preference without dirtying the project."""
        self._remember_local_export_defaults()
        self._schedule_persisted_state_save()

    def _remember_local_export_defaults(self) -> None:
        self._local_output_preferences.update(
            {
                "encoder_mode": str(
                    self._export_encoder_combo.currentData() or ENCODER_CPU
                ),
                "codec": self._export_codec_value(),
                "preset": str(
                    self._export_preset_combo.currentData() or "medium"
                ),
                "crf": int(self._export_crf_spin.value()),
                "render_workers": int(
                    self._export_render_workers_combo.currentData() or 0
                ),
            }
        )

    def _on_gpu_preview_changed(self, enabled: bool) -> None:
        """Apply and persist the experimental subtitle-preview backend."""
        if not self._preview_panel.set_gpu_preview_enabled(bool(enabled)):
            blocked = self._gpu_preview_check.blockSignals(True)
            try:
                self._gpu_preview_check.setChecked(False)
            finally:
                self._gpu_preview_check.blockSignals(blocked)
            fluent_warning(
                self,
                "GPU 预览不可用",
                "当前预览模式不支持 GPU 字幕层，已继续使用 Painter。",
            )
        self._save_persisted_state()

    def _on_preview_quality_changed(self, quality: str) -> None:
        """Apply and persist a local preview-only raster quality preference."""
        normalized = normalize_preview_quality(quality)
        self._preview_panel.set_preview_quality(normalized)
        self._local_output_preferences["preview_quality"] = normalized
        self._save_persisted_state()

    def _on_gpu_export_changed(self, _enabled: bool) -> None:
        """Persist GPU subtitle export independently from encoder selection."""
        self._save_persisted_state()

    def _on_gpu_preview_fallback(self, message: str) -> None:
        InfoBar.warning(
            title="GPU 预览已回退",
            content=str(message),
            parent=self,
            position=InfoBarPosition.BOTTOM_RIGHT,
            duration=6000,
        )

    def _on_scheme_selection_changed(self, key: str) -> None:
        self._selected_scheme_key = key
        self._schedule_persisted_state_save()
        self._mark_project_dirty()

    def _merged_role_options(self) -> list[str]:
        """返回属性面板当前可分配角色，供歌词列表与逐字符编辑器使用。

        预设库只是可复用模板，不代表当前项目里的角色；但用户通过预设库
        “导入为项目角色”的方案已经进入属性面板角色导航，必须在首次分配前
        同步到左侧歌词表格。直接读取 ``role_names`` 还能排除 N3 覆盖后残留的
        旧 LRC 标签，因为 ``set_roles`` 会以当前内容角色重建该列表。
        """
        if hasattr(self, "_property_panel"):
            options = self._property_panel.role_names
            seen = set(options)
            for name in self._content_role_options():
                if name not in seen:
                    seen.add(name)
                    options.append(name)
            return options
        return self._content_role_options()

    def _content_role_options(self) -> list[str]:
        """歌词与标题实际引用的角色名；不混入历史预设。"""
        options: list[str] = []
        seen: set[str] = set()
        for track in self._all_tracks():
            for name in track.role_options:
                if name and name != TITLE_SCHEME_NAME and name not in seen:
                    seen.add(name)
                    options.append(name)
        title = self._style.title_overlay
        if title is not None:
            for row in title.char_role_labels:
                for label in row:
                    name = str(label or "").strip()
                    if name and name != TITLE_SCHEME_NAME and name not in seen:
                        seen.add(name)
                        options.append(name)
        return options

    def _freeze_title_template_for_character_edit(self) -> None:
        """首次逐字编辑前把标题元数据模板展开为当前固定文字。"""
        title = self._style.title_overlay
        if (
            title is None
            or self._timing_track is None
            or ("{title}" not in title.text_template and "{artist}" not in title.text_template)
        ):
            return
        resolved = _resolve_title_text(title, self._timing_track)
        if not resolved:
            InfoBar.warning(
                title="标题为空",
                content="请先在标题页输入文字，再进行逐字符角色分配。",
                parent=self,
                position=InfoBarPosition.BOTTOM_RIGHT,
                duration=3000,
            )
            return
        fixed = replace(
            title,
            text_template=resolved,
            char_role_labels=[[None] * len(line) for line in resolved.split("\n")],
        )
        self._property_panel.set_style(
            replace(self._style, title_overlay=fixed), emit=True
        )
        InfoBar.info(
            title="标题模板已固定",
            content="已按当前歌曲信息展开为固定文字，可逐字符分配角色。",
            parent=self,
            position=InfoBarPosition.BOTTOM_RIGHT,
            duration=2500,
        )

    def _on_lyrics_role_changed(self, row: int, role_name: str) -> None:
        """用户修改了某句歌词的角色时，将角色名写入该行所有字素（当前选中源）。"""
        if self._title_source_active:
            title = self._style.title_overlay
            if title is None:
                return
            lines = title.text_template.split("\n")
            if not 0 <= row < len(lines):
                return
            label = role_name.strip() if role_name else None
            self._set_title_role_labels(row, [label] * len(lines[row]))
            return
        track = self._active_track()
        if track is None:
            return
        if row < 0 or row >= len(track.lines):
            return
        label = role_name.strip() if role_name else None
        self._set_line_role_labels(
            track, row, [label for _ch in track.lines[row].chars]
        )

    def _on_lyrics_roles_changed(self, rows: list[int], role_name: str) -> None:
        """把一个角色方案批量覆盖到所选歌词行，并作为一条命令撤销/重做。"""
        if self._title_source_active:
            # 整行覆盖不展开 {title} / {artist}：整行角色本来就与字符数无关。
            title = self._style.title_overlay
            if title is None:
                return
            lines = title.text_template.split("\n")
            valid_rows = sorted(
                {
                    int(row)
                    for row in rows
                    if 0 <= int(row) < len(lines) and lines[int(row)]
                }
            )
            if not valid_rows:
                return
            label = role_name.strip() if role_name else None
            labels = normalize_title_char_role_labels(
                title.text_template, title.char_role_labels
            )
            changed = False
            for row in valid_rows:
                new_values = [label] * len(lines[row])
                if labels[row] != new_values:
                    labels[row] = new_values
                    changed = True
            if not changed:
                return
            if label:
                self._materialize_role_schemes({label})
            self._property_panel.set_style(
                replace(
                    self._style,
                    title_overlay=replace(title, char_role_labels=labels),
                ),
                emit=True,
            )
            return
        track_index = self._active_source_index
        track = self._track_by_index(track_index)
        if track is None:
            return
        valid_rows = tuple(
            sorted(
                {
                    int(row)
                    for row in rows
                    if 0 <= int(row) < len(track.lines)
                    and track.lines[int(row)].chars
                    and not track.lines[int(row)].is_blank
                }
            )
        )
        if not valid_rows:
            return
        label = role_name.strip() if role_name else None
        has_guides = any(
            track.lines[row].guide_symbol is not None for row in valid_rows
        )
        if not has_guides:
            old_values = tuple(
                tuple(ch.role_label for ch in track.lines[row].chars)
                for row in valid_rows
            )
            new_values = tuple(
                tuple(label for _ch in track.lines[row].chars)
                for row in valid_rows
            )
            if old_values == new_values:
                return
            for row, labels in zip(valid_rows, new_values):
                for ch, value in zip(track.lines[row].chars, labels):
                    ch.role_label = value
            if label:
                self._materialize_role_schemes({label})
            self._undo_stack.append(
                ("char_roles_batch", track_index, valid_rows, old_values, new_values)
            )
            del self._undo_stack[:-_UNDO_STACK_LIMIT]
            self._redo_stack.clear()
            self._refresh_after_role_labels_changed(valid_rows)
            return
        old_values = tuple(
            (
                track.lines[row].guide_symbol,
                tuple(ch.role_label for ch in track.lines[row].chars),
            )
            for row in valid_rows
        )
        new_values = tuple(
            (
                guide_symbol_with_role_labels(
                    track.lines[row].guide_symbol,
                    [label] * max(int(track.lines[row].guide_symbol.count), 1),
                )
                if track.lines[row].guide_symbol is not None
                else None,
                tuple(label for _ch in track.lines[row].chars),
            )
            for row in valid_rows
        )
        if old_values == new_values:
            return
        for row, (symbol, labels) in zip(valid_rows, new_values):
            track.lines[row].guide_symbol = symbol
            for ch, value in zip(track.lines[row].chars, labels):
                ch.role_label = value
        if label:
            self._materialize_role_schemes({label})
        self._undo_stack.append(
            ("inline_roles_batch", track_index, valid_rows, old_values, new_values)
        )
        del self._undo_stack[:-_UNDO_STACK_LIMIT]
        self._redo_stack.clear()
        self._refresh_after_role_labels_changed(valid_rows)

    def _on_lyrics_char_roles_changed(self, row: int, labels: list) -> None:
        """行内逐字符角色编辑器确定后写回（当前选中源）。"""
        if self._title_source_active:
            self._set_title_role_labels(row, labels)
            return
        track = self._active_track()
        if track is None:
            return
        if row < 0 or row >= len(track.lines):
            return
        line = track.lines[row]
        if len(labels) != len(line.chars):
            return
        normalized = [str(label).strip() or None if label else None for label in labels]
        self._set_line_role_labels(track, row, normalized)

    def _on_guide_symbol_import_requested(self, rows: list[int]) -> None:
        track = self._active_track()
        if track is None or self._title_source_active:
            return
        valid_rows = tuple(
            sorted(
                {
                    int(row)
                    for row in rows
                    if 0 <= int(row) < len(track.lines)
                    and track.lines[int(row)].chars
                    and not track.lines[int(row)].is_blank
                }
            )
        )
        if not valid_rows:
            return
        start_dir = str(self._subtitle_path.parent) if self._subtitle_path else ""
        path_str, _ = QFileDialog.getOpenFileName(
            self, "选择 SVG 导唱符", start_dir, "SVG 文件 (*.svg)"
        )
        if not path_str:
            return
        current = track.lines[valid_rows[0]].guide_symbol
        settings_dialog = _GuideSymbolSettingsDialog(
            count=current.count if current is not None else 1,
            interval_ms=current.duration_ms if current is not None else 1000,
            parent=self,
        )
        if settings_dialog.exec() != QDialog.DialogCode.Accepted:
            return
        count, duration_ms = settings_dialog.settings()
        try:
            symbol = import_svg_guide_symbol(
                Path(path_str), duration_ms=duration_ms, count=count
            )
        except GuideSymbolImportError as exc:
            fluent_error(self, "无法导入导唱符", str(exc))
            return
        old_values = tuple(track.lines[row].guide_symbol for row in valid_rows)
        new_values = tuple(symbol for _row in valid_rows)
        if old_values == new_values:
            return
        for row in valid_rows:
            track.lines[row].guide_symbol = symbol
        self._undo_stack.append(
            ("guide_symbols", self._active_source_index, valid_rows, old_values, new_values)
        )
        del self._undo_stack[:-_UNDO_STACK_LIMIT]
        self._redo_stack.clear()
        self._refresh_after_guide_symbols_changed(valid_rows)

    @staticmethod
    def _style_change_paths_mergeable(
        previous: frozenset[str], current: frozenset[str]
    ) -> bool:
        """Treat a newly materialized mapping and its leaf edit as one action."""
        if previous == current:
            return True
        if len(previous) != 1 or len(current) != 1:
            return False
        old_path = next(iter(previous))
        new_path = next(iter(current))
        return old_path.startswith(new_path + ".") or new_path.startswith(old_path + ".")

    def _on_guide_prefix_replace_requested(self) -> None:
        track = self._active_track()
        if track is None or self._title_source_active:
            return
        start_dir = str(self._subtitle_path.parent) if self._subtitle_path else ""
        dialog = GuidePrefixReplaceDialog(
            track,
            start_dir=start_dir,
            role_options_provider=self._guide_role_scheme_options,
            parent=self,
        )
        role_signal = getattr(dialog, "roleSchemeApplyRequested", None)
        if role_signal is not None:
            role_signal.connect(self._on_guide_matches_role_scheme_requested)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        svg_path = dialog.svg_path()
        selected = dialog.selected_matches()
        if svg_path is None or not selected:
            return
        try:
            base_symbol = import_svg_guide_symbol(svg_path)
        except GuideSymbolImportError as exc:
            fluent_error(self, "无法导入导唱符", str(exc))
            return

        candidate_rows = tuple(
            sorted({match.row for match in selected if 0 <= match.row < len(track.lines)})
        )
        old_by_row = {
            row: (
                track.lines[row].guide_symbol,
                tuple(sorted(track.lines[row].inline_guide_symbols.items())),
            )
            for row in candidate_rows
        }
        applied_rows: set[int] = set()
        applied_matches: list[GuidePrefixMatch] = []
        for match in selected:
            if not 0 <= match.row < len(track.lines):
                continue
            line = track.lines[match.row]
            start = int(match.start_index)
            end = start + int(match.count)
            if (
                start < 0
                or end > len(line.chars)
                or tuple(char.text for char in line.chars[start:end]) != match.prefix
            ):
                continue
            if match.is_prefix:
                symbol = replacement_symbol_for_match(base_symbol, line, match)
                if symbol is None:
                    continue
                line.guide_symbol = symbol
            else:
                line.inline_guide_symbols = dict(line.inline_guide_symbols)
                for index in range(start, end):
                    line.inline_guide_symbols[index] = base_symbol
            applied_rows.add(match.row)
            applied_matches.append(match)

        if not applied_rows:
            fluent_warning(
                self,
                "没有可替换项",
                "候选歌词在窗口打开后已发生变化，无法确认原标记位置。请重新检测后再试。",
            )
            return
        row_tuple = tuple(sorted(applied_rows))
        old_tuple = tuple(old_by_row[row] for row in row_tuple)
        new_tuple = tuple(
            (
                track.lines[row].guide_symbol,
                tuple(sorted(track.lines[row].inline_guide_symbols.items())),
            )
            for row in row_tuple
        )
        self._undo_stack.append(
            (
                "guide_replacements",
                self._active_source_index,
                row_tuple,
                old_tuple,
                new_tuple,
            )
        )
        del self._undo_stack[:-_UNDO_STACK_LIMIT]
        self._redo_stack.clear()
        self._refresh_after_guide_symbols_changed(row_tuple)
        role_name = choose_guide_role_scheme(
            self._guide_role_scheme_options(),
            prompt="是否将刚刚批量替换的导唱符统一应用为以下角色方案？",
            cancel_text="暂不应用",
            parent=self,
        )
        if role_name:
            self._apply_guide_match_role_scheme(applied_matches, role_name)

    def _guide_role_scheme_options(self) -> list[str]:
        """Return project role schemes in navigation order, excluding title."""
        options = self._merged_role_options()
        seen = set(options)
        for name in self._style.custom_style_schemes:
            if name == TITLE_SCHEME_NAME or name in seen:
                continue
            seen.add(name)
            options.append(name)
        return options

    def _on_guide_matches_role_scheme_requested(
        self, matches: object, role_name: str
    ) -> None:
        if isinstance(matches, (list, tuple)):
            self._apply_guide_match_role_scheme(list(matches), role_name)

    def _apply_guide_match_role_scheme(
        self, matches: list[object], role_name: str
    ) -> bool:
        """Apply one role only to selected marker spans and attached guides."""
        track = self._active_track()
        label = str(role_name or "").strip()
        if track is None or self._title_source_active or not label:
            return False

        matches_by_row: dict[int, list[GuidePrefixMatch]] = {}
        for value in matches:
            if not isinstance(value, GuidePrefixMatch):
                continue
            row = int(value.row)
            start = int(value.start_index)
            end = start + int(value.count)
            if (
                not 0 <= row < len(track.lines)
                or start < 0
                or end > len(track.lines[row].chars)
                or tuple(
                    char.text for char in track.lines[row].chars[start:end]
                )
                != value.prefix
            ):
                continue
            matches_by_row.setdefault(row, []).append(value)
        if not matches_by_row:
            return False

        rows = tuple(sorted(matches_by_row))
        old_by_row = {
            row: (
                track.lines[row].guide_symbol,
                tuple(char.role_label for char in track.lines[row].chars),
            )
            for row in rows
        }
        for row in rows:
            line = track.lines[row]
            prefix_selected = False
            for match in matches_by_row[row]:
                start = int(match.start_index)
                end = start + int(match.count)
                for char in line.chars[start:end]:
                    char.role_label = label
                prefix_selected |= match.is_prefix
            if prefix_selected and line.guide_symbol is not None:
                line.guide_symbol = guide_symbol_with_role_labels(
                    line.guide_symbol,
                    [label] * max(int(line.guide_symbol.count), 1),
                )

        changed_rows = tuple(
            row
            for row in rows
            if old_by_row[row]
            != (
                track.lines[row].guide_symbol,
                tuple(char.role_label for char in track.lines[row].chars),
            )
        )
        if not changed_rows:
            return False
        old_values = tuple(old_by_row[row] for row in changed_rows)
        new_values = tuple(
            (
                track.lines[row].guide_symbol,
                tuple(char.role_label for char in track.lines[row].chars),
            )
            for row in changed_rows
        )
        self._materialize_role_schemes({label})
        self._undo_stack.append(
            (
                "inline_roles_batch",
                self._active_source_index,
                changed_rows,
                old_values,
                new_values,
            )
        )
        del self._undo_stack[:-_UNDO_STACK_LIMIT]
        self._redo_stack.clear()
        self._refresh_after_guide_symbols_changed(changed_rows)
        return True

    def _on_guide_symbol_remove_requested(self, rows: list[int]) -> None:
        track = self._active_track()
        if track is None or self._title_source_active:
            return
        valid_rows = tuple(
            sorted(
                {
                    int(row)
                    for row in rows
                    if 0 <= int(row) < len(track.lines)
                    and track.lines[int(row)].guide_symbol is not None
                }
            )
        )
        if not valid_rows:
            return
        old_values = tuple(track.lines[row].guide_symbol for row in valid_rows)
        new_values = tuple(None for _row in valid_rows)
        for row in valid_rows:
            track.lines[row].guide_symbol = None
        self._undo_stack.append(
            ("guide_symbols", self._active_source_index, valid_rows, old_values, new_values)
        )
        del self._undo_stack[:-_UNDO_STACK_LIMIT]
        self._redo_stack.clear()
        self._refresh_after_guide_symbols_changed(valid_rows)

    def _on_guide_char_roles_changed(
        self, row: int, guide_labels: object, labels: list
    ) -> None:
        track = self._active_track()
        if track is None or not 0 <= row < len(track.lines):
            return
        line = track.lines[row]
        symbol = line.guide_symbol
        replacement_count = guide_symbol_replacement_count(line)
        visible_chars = line.chars[replacement_count:]
        if (
            symbol is None
            or len(labels) != len(visible_chars)
            or not isinstance(guide_labels, list)
            or len(guide_labels) != max(int(symbol.count), 1)
        ):
            return
        normalized_guides = [
            str(label).strip() or None if label else None
            for label in guide_labels
        ]
        normalized = [str(label).strip() or None if label else None for label in labels]
        old_value = (symbol, tuple(ch.role_label for ch in line.chars))
        new_symbol = guide_symbol_with_role_labels(symbol, normalized_guides)
        new_char_labels = tuple(
            [*normalized_guides[:replacement_count], *normalized]
            if replacement_count
            else normalized
        )
        new_value = (new_symbol, new_char_labels)
        if old_value == new_value:
            return
        line.guide_symbol = new_symbol
        for char, label in zip(line.chars, new_char_labels):
            char.role_label = label
        self._materialize_role_schemes(
            {label for label in [*normalized_guides, *normalized] if label}
        )
        self._undo_stack.append(
            ("guide_char_roles", self._active_source_index, row, old_value, new_value)
        )
        del self._undo_stack[:-_UNDO_STACK_LIMIT]
        self._redo_stack.clear()
        self._refresh_after_guide_symbols_changed((row,))

    def _on_inline_char_edit_changed(
        self, row: int, guide_labels: object, labels: list, vector_symbols: object
    ) -> None:
        """Commit SVG replacements selected in the per-character role dialog."""
        if self._title_source_active:
            return
        track = self._active_track()
        if track is None or not 0 <= row < len(track.lines):
            return
        line = track.lines[row]
        symbol = line.guide_symbol
        replacement_count = guide_symbol_replacement_count(line)
        visible_chars = line.chars[replacement_count:]
        if (
            len(labels) != len(visible_chars)
            or not isinstance(vector_symbols, list)
            or len(vector_symbols) != len(visible_chars)
        ):
            return
        if symbol is None:
            if guide_labels is not None:
                return
            normalized_guides: list[Optional[str]] = []
            new_symbol = None
        else:
            if (
                not isinstance(guide_labels, list)
                or len(guide_labels) != max(int(symbol.count), 1)
            ):
                return
            normalized_guides = [
                str(label).strip() or None if label else None
                for label in guide_labels
            ]
            new_symbol = guide_symbol_with_role_labels(symbol, normalized_guides)
        normalized = [
            str(label).strip() or None if label else None for label in labels
        ]
        inline_symbols: dict[int, GuideSymbol] = {}
        for offset, vector_symbol in enumerate(vector_symbols):
            if vector_symbol is None:
                continue
            if not isinstance(vector_symbol, GuideSymbol) or not vector_symbol.path_commands:
                return
            inline_symbols[replacement_count + offset] = vector_symbol
        new_char_labels = tuple(
            [*normalized_guides[:replacement_count], *normalized]
            if replacement_count
            else normalized
        )
        old_value = (
            symbol,
            tuple(char.role_label for char in line.chars),
            tuple(sorted(line.inline_guide_symbols.items())),
        )
        new_value = (
            new_symbol,
            new_char_labels,
            tuple(sorted(inline_symbols.items())),
        )
        if old_value == new_value:
            return
        line.guide_symbol = new_symbol
        line.inline_guide_symbols = inline_symbols
        for char, label in zip(line.chars, new_char_labels):
            char.role_label = label
        self._materialize_role_schemes(
            {label for label in [*normalized_guides, *normalized] if label}
        )
        self._undo_stack.append(
            ("inline_char_edit", self._active_source_index, row, old_value, new_value)
        )
        del self._undo_stack[:-_UNDO_STACK_LIMIT]
        self._redo_stack.clear()
        self._refresh_after_guide_symbols_changed((row,))

    def _refresh_after_guide_symbols_changed(self, rows: tuple[int, ...]) -> None:
        track = self._active_track()
        if track is None:
            return
        self._lyrics_panel.set_track(track)
        self._lyrics_panel.set_role_options(self._merged_role_options())
        if self._active_source_index == 0:
            self._preview_panel.set_track(track)
        else:
            self._sync_extra_tracks_to_preview()
        self._margin_check_timer.start()
        self._mark_project_dirty()

    def _set_title_role_labels(self, row: int, labels: list) -> None:
        """写回标题某行逐字符角色，作为 Style 修改进入统一撤销栈。"""
        title = self._style.title_overlay
        if title is None:
            return
        rows = [list(values) for values in title.char_role_labels]
        lines = title.text_template.split("\n")
        if not 0 <= row < len(lines) or len(labels) != len(lines[row]):
            return
        while len(rows) < len(lines):
            rows.append([None] * len(lines[len(rows)]))
        normalized = [str(label).strip() or None if label else None for label in labels]
        if rows[row] == normalized:
            return
        rows[row] = normalized
        self._materialize_role_schemes({label for label in normalized if label})
        self._property_panel.set_style(
            replace(
                self._style,
                title_overlay=replace(title, char_role_labels=rows),
            ),
            emit=True,
        )

    def _set_line_role_labels(
        self, track: TimingTrack, row: int, labels: list[Optional[str]]
    ) -> None:
        """逐字符写回角色标签：物化方案 + 入撤销栈 + 刷新（整行/逐字共用）。"""
        line = track.lines[row]
        old_labels = tuple(ch.role_label for ch in line.chars)
        new_labels = tuple(labels)
        old_symbol = line.guide_symbol
        new_symbol = (
            guide_symbol_with_role_labels(
                old_symbol,
                [labels[0] if labels else None] * max(int(old_symbol.count), 1),
            )
            if old_symbol is not None
            else None
        )
        if new_labels == old_labels and new_symbol == old_symbol:
            return
        for ch, label in zip(line.chars, labels):
            ch.role_label = label
        line.guide_symbol = new_symbol
        self._materialize_role_schemes({label for label in labels if label})
        if old_symbol is not None:
            self._undo_stack.append(
                (
                    "guide_char_roles",
                    self._active_source_index,
                    row,
                    (old_symbol, old_labels),
                    (new_symbol, new_labels),
                )
            )
        else:
            self._undo_stack.append(
                ("char_roles", self._active_source_index, row, old_labels, new_labels)
            )
        del self._undo_stack[:-_UNDO_STACK_LIMIT]
        self._redo_stack.clear()
        self._refresh_after_role_labels_changed(row)

    def _materialize_role_schemes(self, labels: set[str]) -> None:
        """把还没有配色方案的角色名物化进 custom_style_schemes。

        预设库命中的深拷贝预设；全新名字（对话框「＋新建」）交给
        ``set_roles`` → ``_ensure_role_schemes`` 按当前面板值自动建。
        不物化 painter 就解析不到，改了角色毫无视觉变化。
        """
        missing = [label for label in labels if label not in self._style.custom_style_schemes]
        if not missing:
            return
        from_presets: dict[str, SubtitleStyleScheme] = {}
        for label in missing:
            matches = [
                preset
                for preset in self._style_presets.values()
                if preset.name == label
            ]
            if len(matches) == 1:
                from_presets[label] = deepcopy(matches[0].scheme)
        if from_presets:
            schemes = dict(self._style.custom_style_schemes)
            schemes.update(from_presets)
            self._style = replace(self._style, custom_style_schemes=schemes)
            self._property_panel.set_style(self._style)
            self._preview_panel.set_style(self._style)
            self._lyrics_panel.set_style(self._style)
            self._schedule_persisted_state_save()
        if any(label not in from_presets for label in missing):
            track = self._active_track()
            if track is not None:
                # 触发属性面板为新角色自动建方案（styleChanged 回流 _apply_style）
                self._property_panel.merge_roles(
                    self._content_role_options()
                    + [label for label in missing if label not in self._content_role_options()]
                )
                self._lyrics_panel.set_role_options(self._merged_role_options())

    def _refresh_after_role_labels_changed(self, rows: int | tuple[int, ...]) -> None:
        # track 是就地修改的，预览（含异步渲染 worker）不会自己发现——
        # 重新喂一次让当前帧立即按新角色配色重渲染。
        if self._active_source_index == 0 and self._timing_track is not None:
            self._preview_panel.set_track(self._timing_track)
        else:
            self._sync_extra_tracks_to_preview()
        affected_rows = (rows,) if isinstance(rows, int) else rows
        for row in affected_rows:
            self._lyrics_panel.refresh_row_role(row)
        self._mark_project_dirty()

    def _restore_char_roles(
        self, track_index: int, row: int, labels: object
    ) -> bool:
        """撤销/重做：直接写回整行角色标签（不经信号，不再入栈）。"""
        track = self._track_by_index(track_index)
        if (
            track is None
            or not isinstance(labels, tuple)
            or not 0 <= row < len(track.lines)
            or len(labels) != len(track.lines[row].chars)
        ):
            return False
        for ch, label in zip(track.lines[row].chars, labels):
            ch.role_label = label
        if track_index == 0 and self._timing_track is not None:
            self._preview_panel.set_track(self._timing_track)
        else:
            self._sync_extra_tracks_to_preview()
        if track_index == self._active_source_index:
            self._lyrics_panel.refresh_row_role(row)
        self._mark_project_dirty()
        return True

    def _restore_char_role_rows(
        self, track_index: int, rows: object, values: object
    ) -> bool:
        """撤销/重做一次批量整行角色覆盖。"""
        track = self._track_by_index(track_index)
        if (
            track is None
            or not isinstance(rows, tuple)
            or not isinstance(values, tuple)
            or len(rows) != len(values)
        ):
            return False
        for row, labels in zip(rows, values):
            if (
                not isinstance(row, int)
                or not isinstance(labels, tuple)
                or not 0 <= row < len(track.lines)
                or len(labels) != len(track.lines[row].chars)
            ):
                return False
        for row, labels in zip(rows, values):
            for ch, label in zip(track.lines[row].chars, labels):
                ch.role_label = label
        if track_index == 0 and self._timing_track is not None:
            self._preview_panel.set_track(self._timing_track)
        else:
            self._sync_extra_tracks_to_preview()
        if track_index == self._active_source_index:
            for row in rows:
                self._lyrics_panel.refresh_row_role(row)
        self._mark_project_dirty()
        return True

    def _on_lyrics_row_clicked(self, row: int) -> None:
        """点击歌词列表某行 → 预览跳转到该行起始时间（当前选中源）。"""
        if self._title_source_active:
            return
        track = self._active_track()
        if track is None:
            return
        if row < 0 or row >= len(track.lines):
            return
        line = track.lines[row]
        if line.is_blank or not line.chars:
            return
        start_ms = timing_line_start_ms(line)
        self._transport_bar.set_time(start_ms)

    @staticmethod
    def _layout_name_for_index(style: Style, index: object) -> Optional[str]:
        try:
            resolved = int(index)
        except (TypeError, ValueError):
            resolved = 0
        if 1 <= resolved <= len(style.layouts):
            return style.layouts[resolved - 1].name
        return None

    @staticmethod
    def _layout_index_for_name(style: Style, name: object) -> int:
        if not isinstance(name, str) or not name:
            return 0
        return next(
            (
                index
                for index, layout in enumerate(style.layouts, start=1)
                if layout.name == name
            ),
            0,
        )

    def _sync_app_layout_defaults(self, style: Style) -> None:
        """Remember the current layout catalog at the app-default reference size."""

        target_reference = max(int(self._app_default_style.layout_reference_height), 1)
        source = rescale_layout_sizes(deepcopy(style), target_reference)
        changes = {
            field_name: deepcopy(getattr(source, field_name))
            for field_name in _LAYOUT_DEFAULT_STYLE_FIELDS
        }
        self._app_default_style = replace(self._app_default_style, **changes)

    def _remember_style_preferences(self, previous: Style, current: Style) -> None:
        """Copy user-edited title/layout habits into new-project defaults."""

        layout_changed = any(
            getattr(previous, field_name) != getattr(current, field_name)
            for field_name in _LAYOUT_DEFAULT_STYLE_FIELDS
        )
        previous_title = previous.title_overlay or TitleOverlay()
        current_title = current.title_overlay or TitleOverlay()
        title_preference_changed = (
            bool(previous_title.enabled), int(previous_title.layout_index or 0)
        ) != (
            bool(current_title.enabled), int(current_title.layout_index or 0)
        )
        app_title = self._app_default_style.title_overlay or TitleOverlay()
        remembered_layout_name = self._layout_name_for_index(
            self._app_default_style, app_title.layout_index
        )
        if layout_changed or title_preference_changed:
            self._sync_app_layout_defaults(current)
            layout_name = (
                self._layout_name_for_index(current, current_title.layout_index)
                if title_preference_changed
                else remembered_layout_name
            )
            app_layout_index = self._layout_index_for_name(
                self._app_default_style, layout_name
            )
            self._app_default_style = replace(
                self._app_default_style,
                title_overlay=replace(
                    TitleOverlay(),
                    enabled=(
                        bool(current_title.enabled)
                        if title_preference_changed
                        else bool(app_title.enabled)
                    ),
                    layout_index=app_layout_index,
                ),
            )

    def _remember_layout_assignment(
        self, mode: str, layout_index: Optional[int] = None
    ) -> None:
        """Remember an explicit batch-assignment action for future subtitle sources."""

        self._sync_app_layout_defaults(self._style)
        if mode == "auto":
            self._layout_assignment_preference = {"mode": "auto"}
        else:
            self._layout_assignment_preference = {
                "mode": "all",
                "layout_name": self._layout_name_for_index(
                    self._style, layout_index
                ),
            }
        self._schedule_persisted_state_save()

    def _apply_remembered_layout_assignment(self, track: TimingTrack) -> None:
        preference = self._layout_assignment_preference
        if not isinstance(preference, dict):
            return
        mode = preference.get("mode")
        if mode == "auto":
            auto_assign_layouts_by_page(track, self._style)
        elif mode == "all":
            index = self._layout_index_for_name(
                self._style, preference.get("layout_name")
            )
            assign_layout_to_all(track, index, self._style)

    def _load_persisted_state(self) -> None:
        data = self._load_subtitle_settings()
        self._subtitle_loading_defaults = subtitle_loading_settings_from_dict(
            data.get("subtitle_loading_defaults")
        )
        self._local_output_preferences = (
            dict(data.get("output"))
            if isinstance(data.get("output"), dict)
            else {}
        )
        # 应用级旧默认曾错误使用“游明朝 100px / 15px 描边”。只在加载
        # 应用默认时迁移到 N3「情報小」；打开 .yurika / .n3proj 时保留项目
        # 明确选择的标题方案。
        loaded_style = migrate_legacy_app_title_default(
            style_from_dict(data.get("style"))
        )
        catalog = get_n3_font_catalog()
        normalized_style, style_changed = normalize_style_font_families(
            loaded_style, catalog
        )
        title_scheme = normalized_style.custom_style_schemes.get(
            TITLE_SCHEME_NAME,
            Style().custom_style_schemes[TITLE_SCHEME_NAME],
        )
        persisted_style = data.get("style")
        had_persisted_title = (
            isinstance(persisted_style, dict)
            and "title_overlay" in persisted_style
        )
        defaults = (
            dict(data.get("new_project_defaults"))
            if isinstance(data.get("new_project_defaults"), dict)
            else {}
        )
        legacy_title = normalized_style.title_overlay or TitleOverlay()
        title_enabled = (
            bool(defaults.get("title_enabled"))
            if "title_enabled" in defaults
            else bool(legacy_title.enabled) if had_persisted_title else False
        )
        if "title_layout_name" in defaults:
            title_layout_index = self._layout_index_for_name(
                normalized_style, defaults.get("title_layout_name")
            )
        elif had_persisted_title:
            title_layout_index = int(legacy_title.layout_index or 0)
        else:
            title_layout_index = int(TitleOverlay().layout_index or 0)
        app_default_style = replace(
            normalized_style,
            custom_style_schemes={TITLE_SCHEME_NAME: deepcopy(title_scheme)},
            singer_style_overrides={},
            title_overlay=replace(
                TitleOverlay(),
                enabled=title_enabled,
                layout_index=title_layout_index,
            ),
        )
        style_changed |= had_persisted_title or replace(
            app_default_style,
            title_overlay=normalized_style.title_overlay,
        ) != normalized_style
        self._app_default_style = deepcopy(app_default_style)
        self._style = deepcopy(app_default_style)
        assignment = defaults.get("layout_assignment")
        if isinstance(assignment, dict) and assignment.get("mode") in {"all", "auto"}:
            self._layout_assignment_preference = deepcopy(assignment)
        else:
            self._layout_assignment_preference = None
        loaded_presets = _style_presets_from_dict(data.get("style_presets"))
        self._style_presets = {}
        presets_changed = False
        for name, preset in loaded_presets.items():
            scheme, changed = normalize_scheme_font_families(preset.scheme, catalog)
            self._style_presets[name] = (
                replace(preset, scheme=scheme) if changed else preset
            )
            presets_changed |= changed
        self._screen_settings = screen_settings_from_dict(data.get("screen"))
        self._style = rescale_layout_sizes(
            self._style,
            self._screen_settings.height,
        )
        self._style = rescale_font_sizes(
            self._style,
            self._screen_settings.height,
        )
        key = data.get("selected_scheme_key")
        if isinstance(key, str) and key:
            self._selected_scheme_key = key
        ratio = data.get("preview_splitter_ratio")
        if isinstance(ratio, (int, float)):
            # 钳到两侧都还能正常操作的区间，坏数据回落默认 4:6
            self._preview_splitter_ratio = min(max(float(ratio), 0.15), 0.85)
        auto_save = data.get("auto_save")
        if isinstance(auto_save, dict):
            self._auto_save_enabled = bool(auto_save.get("enabled", True))
            try:
                interval = int(
                    auto_save.get(
                        "interval_minutes", DEFAULT_AUTO_SAVE_INTERVAL_MINUTES
                    )
                )
            except (TypeError, ValueError):
                interval = DEFAULT_AUTO_SAVE_INTERVAL_MINUTES
            self._auto_save_interval_minutes = max(1, min(60, interval))
        backup = data.get("backup")
        if isinstance(backup, dict):
            try:
                backup_count = int(
                    backup.get("history_count", DEFAULT_PROJECT_BACKUP_COUNT)
                )
            except (TypeError, ValueError):
                backup_count = DEFAULT_PROJECT_BACKUP_COUNT
            self._project_backup_count = max(1, min(20, backup_count))
        if style_changed or presets_changed:
            self._save_persisted_state()

    def _on_preview_splitter_moved(self, _pos: int, _index: int) -> None:
        sizes = self._preview_splitter.sizes()
        total = sum(sizes)
        if total <= 0:
            return
        self._preview_splitter_ratio = sizes[0] / total
        self._splitter_save_timer.start()

    def _schedule_persisted_state_save(self) -> None:
        """标脏并推迟落盘，供属性面板等高频编辑路径调用。

        真正的写发生在停手 ``_PERSISTED_STATE_SAVE_DEBOUNCE_MS`` 之后，或在
        隐藏 / 关闭 / 退出时由 :meth:`_flush_persisted_state_save` 补齐。
        """
        self._persisted_state_dirty = True
        self._persisted_state_save_timer.start()

    def _flush_persisted_state_save(self) -> None:
        """有待落盘的偏好就立刻写一次，否则什么都不做。"""
        self._persisted_state_save_timer.stop()
        if not self._persisted_state_dirty:
            return
        self._save_persisted_state()

    def _save_persisted_state(self) -> None:
        self._persisted_state_save_timer.stop()
        self._persisted_state_dirty = False
        protected_fields = (
            _BUILTIN_SCHEME_STYLE_FIELDS
            | _LAYOUT_DEFAULT_STYLE_FIELDS
            | _FONT_DEFAULT_STYLE_FIELDS
            | _PROJECT_ONLY_STYLE_FIELDS
        )
        common_changes = {
            field.name: deepcopy(getattr(self._style, field.name))
            for field in fields(Style)
            if field.name not in protected_fields
        }
        title_scheme = self._app_default_style.custom_style_schemes.get(
            TITLE_SCHEME_NAME,
            Style().custom_style_schemes[TITLE_SCHEME_NAME],
        )
        self._app_default_style = replace(
            self._app_default_style,
            **common_changes,
            custom_style_schemes={TITLE_SCHEME_NAME: deepcopy(title_scheme)},
            singer_style_overrides={},
        )
        data = self._load_subtitle_settings()
        persisted_style = style_to_dict(self._app_default_style)
        persisted_style.pop("title_overlay", None)
        data["style"] = persisted_style
        default_title = self._app_default_style.title_overlay or TitleOverlay()
        new_project_defaults = (
            dict(data.get("new_project_defaults"))
            if isinstance(data.get("new_project_defaults"), dict)
            else {}
        )
        new_project_defaults["title_enabled"] = bool(default_title.enabled)
        new_project_defaults["title_layout_name"] = self._layout_name_for_index(
            self._app_default_style, default_title.layout_index
        )
        if self._layout_assignment_preference is None:
            new_project_defaults.pop("layout_assignment", None)
        else:
            new_project_defaults["layout_assignment"] = deepcopy(
                self._layout_assignment_preference
            )
        data["new_project_defaults"] = new_project_defaults
        data["subtitle_loading_defaults"] = subtitle_loading_settings_to_dict(
            self._subtitle_loading_defaults
        )
        data["style_presets"] = _style_presets_to_dict(self._style_presets)
        data["screen"] = screen_settings_to_dict(self._screen_settings)
        data["selected_scheme_key"] = (
            self._selected_scheme_key
            if self._selected_scheme_key
            in {"global", f"custom:{TITLE_SCHEME_NAME}"}
            else "global"
        )
        data["preview_splitter_ratio"] = round(self._preview_splitter_ratio, 4)
        data["auto_save"] = {
            "enabled": bool(self._auto_save_enabled),
            "interval_minutes": int(self._auto_save_interval_minutes),
        }
        data["backup"] = {
            "history_count": int(self._project_backup_count),
            "discarded_retention_days": DISCARDED_BACKUP_RETENTION_DAYS,
        }
        if hasattr(self, "_export_native_check"):
            output = dict(data.get("output")) if isinstance(data.get("output"), dict) else {}
            output["native_export_enabled"] = False
            output["gpu_preview_enabled"] = bool(
                self._gpu_preview_check.isChecked()
            )
            output["gpu_preview_default_version"] = GPU_PREVIEW_DEFAULT_VERSION
            output["preview_quality"] = self._transport_bar.preview_quality()
            output["gpu_export_enabled"] = bool(
                self._gpu_export_check.isChecked()
            )
            output["gpu_export_default_version"] = GPU_EXPORT_DEFAULT_VERSION
            output["directory_mode"] = self._export_dir_mode
            output["custom_directory"] = self._export_custom_dir
            output["name_template"] = self._export_name_template
            local_output = self._local_output_preferences
            output["encoder_mode"] = str(
                local_output.get("encoder_mode") or ENCODER_CPU
            )
            output["codec"] = str(local_output.get("codec") or CODEC_H264)
            output["preset"] = str(local_output.get("preset") or "medium")
            local_crf = local_output.get("crf", 18)
            output["crf"] = (
                int(local_crf)
                if isinstance(local_crf, int) and 0 <= local_crf <= 51
                else 18
            )
            local_workers = local_output.get("render_workers", 0)
            output["render_workers"] = (
                int(local_workers)
                if isinstance(local_workers, int)
                and local_workers in RENDER_WORKER_OPTIONS
                else 0
            )
            data["output"] = output
        try:
            if self._settings_provider is not None and hasattr(self._settings_provider, "save"):
                self._settings_provider.save(data)
                return
            settings = load_app_settings()
            settings.subtitle_render = data
            save_app_settings(settings)
        except Exception:
            # 原来是彻底静默的：写盘失败（文件被占用 / 没权限 / 磁盘满）时用户什么
            # 都看不到，只会觉得"存了但没存上"。仍然不往上抛（保存失败不该把正在
            # 编辑的界面带崩），但至少要留下痕迹。
            logging.getLogger(__name__).warning("保存字幕渲染模块设置失败", exc_info=True)
            return

    def _save_builtin_scheme_default(self, key: str) -> None:
        target_reference = max(int(self._app_default_style.font_reference_height), 1)
        source_style = rescale_font_sizes(deepcopy(self._style), target_reference)
        if key == "global":
            changes = {
                field_name: deepcopy(getattr(source_style, field_name))
                for field_name in _BUILTIN_SCHEME_STYLE_FIELDS
            }
            self._app_default_style = replace(self._app_default_style, **changes)
            target = "全局默认"
        elif key == f"custom:{TITLE_SCHEME_NAME}":
            scheme = source_style.custom_style_schemes.get(TITLE_SCHEME_NAME)
            if scheme is None:
                return
            schemes = dict(self._app_default_style.custom_style_schemes)
            schemes[TITLE_SCHEME_NAME] = deepcopy(scheme)
            self._app_default_style = replace(
                self._app_default_style,
                custom_style_schemes=schemes,
            )
            target = "标题"
        else:
            return
        self._save_persisted_state()
        InfoBar.success(
            title="已保存软件默认值",
            content=f"新建项目时将使用当前“{target}”方案。",
            parent=self,
            position=InfoBarPosition.BOTTOM_RIGHT,
            duration=2500,
        )

    def _save_layout_default(self, index: int) -> None:
        """Persist one selected layout without leaking other project layouts."""

        if not 0 <= int(index) <= len(self._style.layouts):
            return
        target_reference = max(int(self._app_default_style.layout_reference_height), 1)
        source_style = rescale_layout_sizes(deepcopy(self._style), target_reference)
        if index == 0:
            changes = {
                field_name: deepcopy(getattr(source_style, field_name))
                for field_name in _LAYOUT_DEFAULT_VALUE_FIELDS
            }
            self._app_default_style = replace(self._app_default_style, **changes)
            target = layout_display_name(self._style, "default")
        else:
            saved_layout = deepcopy(source_style.layouts[index - 1])
            layouts = deepcopy(self._app_default_style.layouts)
            matched = next(
                (
                    saved_index
                    for saved_index, layout in enumerate(layouts)
                    if layout.name == saved_layout.name
                ),
                None,
            )
            if matched is None:
                layouts.append(saved_layout)
            else:
                layouts[matched] = saved_layout
            self._app_default_style = replace(
                self._app_default_style,
                layouts=layouts,
            )
            target = saved_layout.name
        self._save_persisted_state()
        InfoBar.success(
            title="已保存软件默认布局",
            content=(
                f"以后新建项目将携带布局“{target}”的当前参数；"
                "当前页面及现有项目不受影响。"
            ),
            parent=self,
            position=InfoBarPosition.BOTTOM_RIGHT,
            duration=2500,
        )

    def _load_subtitle_settings(self) -> dict:
        try:
            if self._settings_provider is not None and hasattr(self._settings_provider, "load"):
                loaded = self._settings_provider.load()
            else:
                loaded = load_app_settings().subtitle_render
            return dict(loaded) if isinstance(loaded, dict) else {}
        except Exception:
            return {}

    def _sync_preview_output_size(self) -> None:
        width = self._export_width_spin.value()
        height = self._export_height_spin.value()
        self._preview_panel.set_output_size(width, height)
        if hasattr(self, "_preview_window"):
            self._preview_window.set_output_size(width, height)
        if hasattr(self, "_export_monitor_frame"):
            self._export_monitor_frame.set_aspect_ratio(width, height)
            self._sync_export_monitor_card_size(width, height)

    def _sync_export_monitor_card_size(self, width: int, height: int) -> None:
        if width <= 0 or height <= 0 or not hasattr(self, "_export_monitor_card"):
            return
        target_height = max(self._export_settings_col.sizeHint().height(), 1)
        margins = self._export_monitor_layout.contentsMargins()
        spacing = max(self._export_monitor_layout.spacing(), 0)
        chrome_height = (
            margins.top()
            + margins.bottom()
            + self._export_monitor_header.sizeHint().height()
            + self._export_format_label.sizeHint().height()
            + spacing * 2
        )
        frame_height = max(target_height - chrome_height, 1)
        frame_width = int(round(frame_height * width / height))
        target_width = max(frame_width + margins.left() + margins.right(), 1)
        self._export_monitor_card.setFixedHeight(target_height)
        self._export_monitor_card.setMaximumWidth(target_width)

    def _export_output_base(self) -> Optional[Path]:
        """默认输出目录 / 文件名的来源素材：视频 > 背景素材 > 字幕文件。"""
        background_path = (
            Path(self._background_source.path)
            if self._background_source is not None and self._background_source.path
            else None
        )
        return self._video_path or background_path or self._subtitle_path

    def _default_export_dir(self) -> Path:
        base = self._export_output_base()
        return base.parent if base is not None else Path.cwd()

    def _resolved_export_dir(self) -> Path:
        if self._export_dir_mode == EXPORT_DIR_CUSTOM and self._export_custom_dir:
            return Path(self._export_custom_dir).expanduser()
        return self._default_export_dir()

    def _sync_export_directory(self) -> None:
        if hasattr(self, "_export_dir_edit"):
            self._export_dir_edit.setText(str(self._resolved_export_dir()))

    def _set_export_directory_settings(
        self,
        mode: str,
        custom_dir: str,
        *,
        persist: bool,
    ) -> None:
        if mode not in {EXPORT_DIR_SOURCE_VIDEO, EXPORT_DIR_CUSTOM}:
            raise ValueError(f"unsupported export directory mode: {mode}")
        custom_dir = str(custom_dir or "").strip()
        if mode == EXPORT_DIR_CUSTOM and not custom_dir:
            raise ValueError("custom export directory is required")
        self._export_dir_mode = mode
        self._export_custom_dir = custom_dir
        self._sync_export_directory()
        if persist:
            self._save_persisted_state()

    def _export_name_template_values(self) -> dict[str, str]:
        """模板占位符取值。

        ``source_name`` 在什么素材都没有时退回 ``subtitle_render``——这是改成模板
        之前就有的兜底，缺了它默认名会变成孤零零的 ``_yurika出力``。另外两个是用户
        显式选的占位符，素材没到位就留空。
        """
        base = self._export_output_base()
        return {
            "source_name": base.stem if base is not None else "subtitle_render",
            "video_name": self._video_path.stem if self._video_path is not None else "",
            "subtitle_name": (
                self._subtitle_path.stem if self._subtitle_path is not None else ""
            ),
        }

    def _default_export_name(self) -> str:
        """按命名模板生成默认文件名；模板出问题时退回内置默认，不打断导出。"""
        from krok_helper.pipeline import render_name_template

        values = self._export_name_template_values()
        for template in (self._export_name_template, DEFAULT_EXPORT_NAME_TEMPLATE):
            try:
                rendered = render_name_template(template, "导出文件名", values)
            except Exception:
                continue
            if rendered:
                return rendered
        return f"subtitle_render{DEFAULT_OUTPUT_NAME_SUFFIX}"

    def _normalized_export_name(self) -> str:
        """文件名输入框内容（用户手滑带上 .mp4 时剥掉，扩展名由拼装统一补）。"""
        name = self._export_name_edit.text().strip()
        if name.lower().endswith(".mp4"):
            name = name[:-4].strip()
        return name

    def _export_output_text(self) -> str:
        """当前输出全路径文本；目录或文件名为空时返回空串（存项目用）。"""
        directory = self._export_dir_edit.text().strip()
        name = self._normalized_export_name()
        if not directory or not name:
            return ""
        return str(Path(directory) / f"{name}.mp4")

    def _prefill_export_output(self, *, force_name: bool = False) -> None:
        """Prefill output location/name, optionally rebasing the name to new media."""
        self._sync_export_directory()
        current = self._export_name_edit.text().strip()
        if force_name or not current or current == self._export_auto_name:
            name = self._default_export_name()
            self._export_name_edit.setText(name)
            self._export_auto_name = name

    def _resolve_ffmpeg_dir(self) -> Optional[Path]:
        try:
            settings = load_app_settings()
            raw = (settings.ffmpeg_dir or "").strip()
            return Path(raw) if raw else None
        except Exception:
            return None

    def _build_render_job(self) -> RenderJob:
        if self._timing_track is None:
            raise ProcessingError("请先加载字幕文件。")
        if self._background_source is None:
            raise ProcessingError("请先选择背景源。")
        directory = self._export_dir_edit.text().strip()
        if not directory:
            raise ProcessingError("请先选择输出文件夹。")
        name = self._normalized_export_name()
        if not name:
            name = self._default_export_name()
            self._export_name_edit.setText(name)
            self._export_auto_name = name
        output_path = Path(directory).expanduser() / f"{name}.mp4"
        duration_ms = self._current_export_duration_ms()
        return RenderJob(
            track=self._timing_track,
            style=self._style,
            background_video_path=self._video_path,
            background_source=self._background_source,
            audio_path=(
                self._audio_path
                if self._audio_path is not None and self._audio_path != self._video_path
                else None
            ),
            output_path=output_path,
            extra_tracks=tuple(self._extra_track_list()),
            width=self._export_width_spin.value(),
            height=self._export_height_spin.value(),
            fps=self._export_fps_value(),
            duration_ms=duration_ms,
            include_audio=bool(self._audio_info and self._audio_info.audio_streams > 0),
            encoder_mode=str(self._export_encoder_combo.currentData() or ENCODER_CPU),
            crf=self._export_crf_spin.value(),
            preset=str(self._export_preset_combo.currentData() or "medium"),
            codec=self._export_codec_value(),
            native_export_enabled=False,
            gpu_export_enabled=self._gpu_export_check.isChecked(),
            render_workers=self._export_render_workers_value(),
        )

    def _export_render_workers_value(self) -> Optional[int]:
        value = int(self._export_render_workers_combo.currentData() or 0)
        return value if value in RENDER_WORKER_OPTIONS[1:] else None

    def _current_export_duration_ms(self) -> int:
        candidates: list[int] = [track_duration_ms(track) for track in self._all_tracks()]
        if self._video_info is not None and self._video_info.duration > 0:
            candidates.append(int(round(self._video_info.duration * 1000)))
        if self._audio_info is not None and self._audio_info.duration > 0:
            candidates.append(int(round(self._audio_info.duration * 1000)))
        return max(candidates, default=0)

    def _start_render_export(self) -> None:
        if self._render_thread is not None and self._render_thread.isRunning():
            fluent_info(self, "导出中", "当前导出任务还在处理中，请稍等。")
            return
        try:
            job = self._build_render_job()
        except ProcessingError as exc:
            fluent_error(self, "无法导出", str(exc))
            return

        self._export_start_button.setEnabled(False)
        self._export_stop_button.setEnabled(True)
        self._export_progress.setPaused(False)
        self._export_progress.setError(False)
        self._export_progress.setRange(0, 0)
        self._export_status_label.setText("正在准备导出…")

        # 导出预览：临时目录承接 ffmpeg 持续覆盖写入的合成帧
        self._cleanup_export_preview_dir()
        try:
            self._export_preview_dir = Path(tempfile.mkdtemp(prefix="krok_export_preview_"))
            self._export_preview_file = self._export_preview_dir / "frame.jpg"
        except OSError:
            self._export_preview_dir = None
            self._export_preview_file = None
        self._export_preview_mtime_ns = 0
        self._export_monitor_view.clear_frame()
        self._export_eta_label.setText("正在准备…")
        self._export_format_label.setText(self._export_format_text(job))
        self._export_started_monotonic = time.monotonic()
        self._export_preview_timer.start()

        thread = QThread(self)
        preview_width = _export_preview_width(
            self._export_monitor_view.size(),
            float(self._export_monitor_view.devicePixelRatioF()),
            job.width,
            job.height,
        )
        worker = _RenderWorker(
            job,
            self._resolve_ffmpeg_dir(),
            self._export_preview_file,
            preview_width,
        )
        worker.moveToThread(thread)
        worker.progressChanged.connect(self._on_render_progress)
        worker.logMessage.connect(self._on_render_log)
        worker.finished.connect(self._finish_render_success)
        worker.cancelled.connect(self._finish_render_cancelled)
        worker.failed.connect(self._finish_render_failure)
        worker.finished.connect(thread.quit)
        worker.cancelled.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(self._clear_render_thread)
        thread.started.connect(worker.run)
        self._render_thread = thread
        self._render_worker = worker
        self._sync_preview_window_visibility()
        thread.start()
        self._refresh_project_title()

    def _stop_render_export(self) -> None:
        if self._render_worker is None or self._render_thread is None or not self._render_thread.isRunning():
            return
        confirmed = fluent_question(
            self,
            "停止导出",
            "确定要停止当前导出吗？\n未完成文件将被清理。",
            yes_text="停止导出",
            no_text="继续导出",
            default_cancel=True,
        )
        if not confirmed:
            return
        self._export_stop_button.setEnabled(False)
        self._export_status_label.setText("正在停止导出…")
        self._render_worker.cancel()

    def _on_render_progress(self, done: int, total: int) -> None:
        self._export_progress.setRange(0, max(total, 1))
        self._export_progress.setValue(done)
        self._export_status_label.setText(f"正在导出… {done}/{total} 帧")
        elapsed = time.monotonic() - self._export_started_monotonic
        if done > 0 and elapsed >= 1.0:
            rate = done / elapsed
            remaining = max(total - done, 0) / max(rate, 1e-6)
            self._export_eta_label.setText(
                f"剩余约 {_format_eta_seconds(remaining)} · {rate:.0f} 帧/秒"
            )

    def _on_render_log(self, message: str) -> None:
        if message == "执行命令:":
            self._suppress_next_render_command_log = True
            return
        if self._suppress_next_render_command_log:
            self._suppress_next_render_command_log = False
            return
        if "Late SEI is not implemented" in message or (
            "If you want to help, upload a sample of this file" in message
            and "ffmpeg-devel" in message
        ):
            return
        self._export_status_label.setText(message)

    def _export_codec_value(self) -> str:
        return str(self._export_codec_combo.currentData() or CODEC_H264)

    @staticmethod
    def _codec_display(codec: str) -> str:
        return "H.265 (HEVC)" if codec == CODEC_HEVC else "H.264 (AVC)"

    def _refresh_export_format_label(self) -> None:
        # 导出进行中标签由 _export_format_text 的完整信息占据，不在此覆盖
        if not self._export_start_button.isEnabled():
            return
        directory_text = self._export_dir_edit.text().strip()
        directory = Path(directory_text).expanduser() if directory_text else None
        self._export_format_label.setText(
            self._export_format_values_text(
                codec=self._export_codec_value(),
                width=self._export_width_spin.value(),
                height=self._export_height_spin.value(),
                fps=self._export_fps_value(),
                output_directory=directory,
            )
        )

    def _export_format_text(self, job: RenderJob) -> str:
        return self._export_format_values_text(
            codec=job.codec,
            width=job.width,
            height=job.height,
            fps=job.fps,
            output_directory=job.output_path.parent,
        )

    def _export_format_values_text(
        self,
        *,
        codec: str,
        width: int,
        height: int,
        fps: int,
        output_directory: Optional[Path],
    ) -> str:
        text = (
            f"输出格式: MP4 · {self._codec_display(codec)}"
            f" · {width}×{height} @ {fps}fps"
        )
        if output_directory is None:
            return text
        try:
            probe = (
                output_directory
                if output_directory.exists()
                else Path(output_directory.anchor or ".")
            )
            free_gb = shutil.disk_usage(probe).free / 1024**3
            text += f" · 磁盘可用 {free_gb:.0f} GB"
        except OSError:
            pass
        return text

    def _poll_export_preview(self) -> None:
        path = self._export_preview_file
        if path is None:
            return
        try:
            mtime_ns = path.stat().st_mtime_ns
        except OSError:
            return
        if mtime_ns == self._export_preview_mtime_ns:
            return
        image = QImage(str(path))
        if image.isNull():
            return  # 极少数情况下文件尚未写完，下个周期再试
        self._export_preview_mtime_ns = mtime_ns
        self._export_monitor_view.set_frame(image)

    def _stop_export_preview_polling(self) -> None:
        self._export_preview_timer.stop()
        self._poll_export_preview()  # 收尾再读一次，保住最后写入的帧

    def _cleanup_export_preview_dir(self) -> None:
        directory = self._export_preview_dir
        self._export_preview_dir = None
        self._export_preview_file = None
        if directory is not None:
            shutil.rmtree(directory, ignore_errors=True)

    def _finish_render_success(self, output_path: Path) -> None:
        self._stop_export_preview_polling()
        elapsed = (
            time.monotonic() - self._export_started_monotonic
            if self._export_started_monotonic > 0
            else 0.0
        )
        elapsed_text = _format_elapsed_seconds(elapsed)
        self._export_eta_label.setText("已完成")
        self._export_progress.setRange(0, 1)
        self._export_progress.setValue(1)
        self._export_status_label.setText(f"导出完成: {output_path}")
        self._export_start_button.setEnabled(True)
        self._export_stop_button.setEnabled(False)
        play_completion_sound()
        choice = fluent_choice(
            self,
            "视频导出完成",
            (
                f"视频已成功导出：\n{output_path}"
                f"\n\n本次导出耗时：{elapsed_text}"
                "\n\n是否自动进入下一步？"
            ),
            ("打开文件夹", "进入下一步", "取消"),
            default=1,
            # 「打开文件夹」保持弹窗不关，方便检查完成片后再决定下一步。
            sticky={0: lambda: self._open_export_folder(output_path)},
        )
        if choice == 1:
            from krok_helper.workflow_host import SubtitleVideoSink

            host = self._workflow_context
            if host is None:
                # 字幕渲染模块被单独拉起来跑，没有下一步可交。
                return
            if not isinstance(host, SubtitleVideoSink):
                # 宿主改了方法名的话，以前这里静默什么也不做。
                logging.getLogger(__name__).warning(
                    "工作台宿主缺少 accept_subtitle_video，成片无法转交下一步"
                )
                return
            host.accept_subtitle_video(output_path)

    def _open_export_folder(self, output_path: Path) -> None:
        # Windows 下用资源管理器直接选中导出文件，其余平台退回打开所在目录。
        if sys.platform == "win32" and output_path.exists():
            subprocess.Popen(["explorer", "/select,", str(output_path)])
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(output_path.parent)))

    def _finish_render_cancelled(self, message: str) -> None:
        self._stop_export_preview_polling()
        self._export_eta_label.setText("已停止")
        # 保留已完成的进度并转入「暂停」黄色态；若仍是忙碌态才重置。
        if self._export_progress.maximum() <= 0:
            self._export_progress.setRange(0, 1)
            self._export_progress.setValue(0)
        self._export_progress.setPaused(True)
        self._export_status_label.setText("导出已停止，未完成文件已清理。" if message else "导出已停止。")
        self._export_start_button.setEnabled(True)
        self._export_stop_button.setEnabled(False)

    def _finish_render_failure(self, message: str) -> None:
        self._stop_export_preview_polling()
        self._export_eta_label.setText("")
        if self._export_progress.maximum() <= 0:
            self._export_progress.setRange(0, 1)
            self._export_progress.setValue(0)
        self._export_progress.setError(True)
        self._export_status_label.setText("导出失败")
        self._export_start_button.setEnabled(True)
        self._export_stop_button.setEnabled(False)
        fluent_error(self, "导出失败", message)

    def _clear_render_thread(self) -> None:
        self._render_thread = None
        self._render_worker = None
        self._cleanup_export_preview_dir()
        self._refresh_project_title()
        self._sync_preview_window_visibility()
        if self._pending_source_reload_keys:
            self._source_change_timer.start(0)

    # ------------------------------------------------------------------ embed

    @staticmethod
    def for_embedding(
        parent: Optional[QWidget] = None,
        settings_provider: Optional[Any] = None,
        workflow_context: "SubtitleVideoSink | None" = None,
    ) -> "SubtitleRenderWindow":
        """创建嵌入工作台用的实例。"""
        instance = SubtitleRenderWindow(
            embedded=True,
            settings_provider=settings_provider,
            workflow_context=workflow_context,
            parent=parent,
        )
        return instance

    def flush_unsaved(self) -> None:
        """Persist the latest dirty snapshot with a bounded forced-exit wait."""
        if not self._project_dirty:
            return
        self._auto_save_timer.stop()
        self._auto_save_pending = False
        self._wait_for_recovery_worker()
        recovery_path = self._recovery_path()
        payload, _snapshot_id = self._recovery_payload_snapshot()
        errors: list[Exception] = []

        def write_snapshot() -> None:
            try:
                save_recovery_project(recovery_path, payload)
            except (OSError, TypeError, ValueError) as exc:
                errors.append(exc)

        thread = threading.Thread(target=write_snapshot, daemon=True)
        thread.start()
        thread.join(AUTO_SAVE_THREAD_WAIT_MS / 1000)
        if thread.is_alive():
            logging.getLogger(__name__).warning(
                "强制退出前写字幕恢复快照超时，保留上一次完整快照"
            )
            return
        if errors:
            logging.getLogger(__name__).warning(
                "强制退出前写字幕恢复快照失败: %s", errors[0]
            )
            return
        self._refresh_project_title()

    def has_pending_crash_recovery(self) -> bool:
        candidates, invalid, stale = scan_recovery_projects(self._recovery_root())
        for path in stale:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        return bool(candidates or invalid)

    def check_crash_recovery(self, dialog_parent: Optional[QWidget] = None) -> bool:
        """Prompt for valid and corrupt recovery files; return True if restored."""
        parent = dialog_parent or self
        candidates, invalid, stale = scan_recovery_projects(self._recovery_root())
        for path in stale:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

        for path in invalid:
            choice = fluent_choice(
                parent,
                "字幕项目恢复文件损坏",
                f"无法读取以下恢复文件：\n{path}\n\n可以删除该文件，或保留以便手动检查。",
                ("删除", "保留"),
                default=1,
            )
            if choice == 0:
                try:
                    path.unlink(missing_ok=True)
                except OSError as exc:
                    fluent_error(parent, "删除恢复文件失败", f"{path}\n\n{exc}")

        for candidate in candidates:
            source = candidate.source_project_path
            source_text = str(source) if source is not None else "未命名字幕项目"
            saved_at = datetime.fromtimestamp(candidate.created_at_unix).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            choice = fluent_choice(
                parent,
                "发现字幕项目恢复数据",
                f"项目：{source_text}\n恢复快照时间：{saved_at}\n\n是否恢复？",
                ("恢复", "放弃", "稍后处理"),
                default=2,
            )
            if choice == 1:
                try:
                    candidate.path.unlink(missing_ok=True)
                except OSError as exc:
                    fluent_error(parent, "删除恢复文件失败", f"{candidate.path}\n\n{exc}")
                continue
            if choice != 0:
                continue
            if self._restore_recovery_candidate(candidate):
                return True
        return False

    def _restore_recovery_candidate(self, candidate: RecoveryCandidate) -> bool:
        try:
            data = load_render_project(candidate.path)
        except (OSError, ValueError) as exc:
            fluent_error(
                self,
                "恢复字幕项目失败",
                f"无法读取恢复文件：\n{candidate.path}\n\n{exc}",
            )
            return False
        data.pop("recovery", None)
        missing_resources = self._missing_project_resources(data)
        self._begin_project_generation()
        self._clear_loaded_media()
        self._apply_project_data(data)
        self._project_path = candidate.source_project_path
        if self._project_path is not None:
            try:
                self._project_disk_revision = inspect_project_file(self._project_path)
            except OSError:
                self._project_disk_revision = None
        self._missing_resources = tuple(missing_resources)
        self._unresolved_resource_labels = {
            label for label, _path in missing_resources
        }
        self._missing_resource_source_data = deepcopy(data) if missing_resources else None
        self._set_project_dirty(True)
        if missing_resources:
            fluent_warning(
                self,
                "项目已恢复，但部分素材未找到",
                "以下素材路径无效，已跳过加载：\n\n"
                + "\n".join(
                    f"• {label}：{path}" for label, path in missing_resources
                ),
                copyable=True,
            )
        InfoBar.success(
            title="字幕项目已恢复",
            content="恢复内容尚未写入正式项目，请及时保存。",
            parent=self,
            position=InfoBarPosition.BOTTOM_RIGHT,
            duration=5000,
        )
        return True

    @staticmethod
    def _recovery_root() -> Path:
        return get_settings_path().parent / "subtitle_render_recovery"

    @staticmethod
    def _backup_root() -> Path:
        return get_settings_path().parent / "subtitle_render_backups"

    def _recovery_path(self) -> Path:
        root = self._recovery_root()
        if self._project_path is None:
            return root / "untitled.yurika.recovery"
        identity = str(self._project_path.resolve()).encode("utf-8", errors="surrogatepass")
        suffix = hashlib.sha256(identity).hexdigest()[:12]
        return root / f"{self._project_path.name}.{suffix}.recovery"

    def _cleanup_recovery_file(self, path: Optional[Path] = None) -> None:
        target = path or self._recovery_path()
        invalidate_recovery_project(target)


def _style_presets_from_dict(payload: object) -> dict[str, StylePreset]:
    """Load stable-ID presets and migrate the legacy ``name -> scheme`` mapping."""
    result: dict[str, StylePreset] = {}

    def add(raw_id: object, raw_name: object, value: object) -> None:
        name = str(raw_name or "").strip()
        if not name:
            return
        preset_id = str(raw_id or "").strip()
        if not preset_id or preset_id in result:
            preset_id = uuid4().hex
        if isinstance(value, StylePreset):
            resolved_name = str(value.name).strip() or name
            resolved_id = str(value.preset_id).strip() or preset_id
            if resolved_id in result:
                resolved_id = uuid4().hex
            result[resolved_id] = StylePreset(
                name=resolved_name,
                group=str(value.group).strip(),
                scheme=deepcopy(value.scheme),
                preset_id=resolved_id,
                source_type=str(value.source_type).strip(),
                source_data=deepcopy(value.source_data),
            )
            return
        if isinstance(value, SubtitleStyleScheme):
            result[preset_id] = StylePreset(
                name=name,
                scheme=deepcopy(value),
                preset_id=preset_id,
            )
            return
        source_type = ""
        source_data: dict = {}
        if isinstance(value, dict) and isinstance(value.get("scheme"), dict):
            group = str(value.get("group") or "").strip()
            scheme_payload = value["scheme"]
            source_type = str(value.get("source_type") or "").strip()
            if isinstance(value.get("source_data"), dict):
                source_data = deepcopy(value["source_data"])
        else:
            group = ""
            scheme_payload = value
        result[preset_id] = StylePreset(
            name=name,
            group=group,
            scheme=subtitle_style_scheme_from_dict(scheme_payload),
            preset_id=preset_id,
            source_type=source_type,
            source_data=source_data,
        )

    if isinstance(payload, list):
        for item in payload:
            if not isinstance(item, dict):
                continue
            add(item.get("id"), item.get("name"), item)
        return result
    if not isinstance(payload, dict):
        return result
    for raw_key, value in payload.items():
        if isinstance(value, StylePreset):
            add(value.preset_id or raw_key, value.name or raw_key, value)
        else:
            add(raw_key, raw_key, value)
    return result


def _style_presets_to_dict(
    presets: dict[str, StylePreset],
) -> list[dict]:
    return [
        {
            "id": str(preset.preset_id or preset_id),
            "name": str(preset.name).strip(),
            "group": str(preset.group).strip(),
            "scheme": subtitle_style_scheme_to_dict(preset.scheme),
            "source_type": str(preset.source_type).strip(),
            "source_data": deepcopy(preset.source_data),
        }
        for preset_id, preset in presets.items()
        if str(preset.name).strip()
    ]
