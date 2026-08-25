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
from dataclasses import replace
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Callable, Optional, TYPE_CHECKING
from uuid import uuid4

if TYPE_CHECKING:  # 只为类型标注，运行时不引入宿主包，保持模块可独立运行
    from krok_helper.workflow_host import SubtitleVideoSink

from PyQt6.QtCore import (
    QObject,
    QThread,
    QTimer,
    QUrl,
    Qt,
    pyqtSignal as Signal,
)
from PyQt6.QtGui import (
    QColor,
    QDesktopServices,
    QImage,
    QKeySequence,
    QShortcut,
    QValidator,
)
from PyQt6.QtWidgets import (
    QApplication,
    QFileDialog,
    QColorDialog,
    QDialog,
    QHBoxLayout,
    QLabel,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    Action,
    CaptionLabel,
    DropDownPushButton,
    FluentIcon as FIF,
    InfoBar,
    InfoBarPosition,
    RoundMenu,
    TitleLabel,
)

from krok_helper.background_throttle import UiActivityGuard, ui_active
from krok_helper.errors import ProcessingError
from krok_helper.ffmpeg import find_tool, probe_media
from krok_helper.models import MediaInfo
from krok_helper.notifications import play_completion_sound
from krok_helper.qfluent_compat import (
    apply_qfluent_menu_lifetime_patch,
    apply_qfluent_tooltip_parent_patch,
)
from krok_helper.settings import get_settings_path, load_app_settings, save_app_settings
from krok_helper.subtitle_render.domain.background import (
    BackgroundSource,
    infer_image_sequence_pattern,
)
from krok_helper.subtitle_render.engine.export.encoder_select import (
    CODEC_H264,
    CODEC_HEVC,
    CPU_PRESETS,
    ENCODER_AMF,
    ENCODER_AUTO,
    ENCODER_CPU,
    ENCODER_NVENC,
    ENCODER_QSV,
)
from krok_helper.subtitle_render.engine.layout.page.assignment import (
    apply_layout_to_page,
    assign_layout_to_all,
    auto_assign_layouts_by_page,
)
from krok_helper.subtitle_render.engine.layout.display.diagnostics import (
    LayoutMarginWarning,
    LayoutTimingDiagnostic,
    layout_pass,
)
from krok_helper.subtitle_render.engine.render.adapters.layout_diagnostics import (
    check_layout_margins,
    display_windows_for_style,
    layout_timing_diagnostics_for_style,
)
from krok_helper.subtitle_render.engine.style.title_semantics import resolve_title_text
from krok_helper.subtitle_render.engine.layout.page.plan import (
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
from krok_helper.subtitle_render.engine.export.render_job import RenderJob
from krok_helper.subtitle_render.engine.timing.timeline import apply_n3_seq_line_breaks
from krok_helper.subtitle_render.sources.guide_symbols import (
    GuideSymbolImportError,
    import_svg_guide_symbol,
)
from krok_helper.subtitle_render.frontend.workflow.background_tasks import (
    _MediaProbeWorker,
)
from krok_helper.subtitle_render.frontend.workflow.export_runtime import (
    ExportRuntimeCallbacks,
    ExportRuntimeController,
    ExportRuntimeHandles,
)
from krok_helper.subtitle_render.frontend.dialogs.fluent_dialogs import (
    fluent_choice,
    fluent_error,
    fluent_get_int,
    fluent_info,
    fluent_question,
    fluent_warning,
)
from krok_helper.subtitle_render.frontend.dialogs.workspace_dialogs import (
    GuideSymbolSettingsDialog as _GuideSymbolSettingsDialog,
    LayoutIssue as _LayoutIssue,
    LayoutIssuesDialog as _LayoutIssuesDialog,
    SubtitleLoadingSettingsDialog as _SubtitleLoadingSettingsDialog,
    TimingIssue as _TimingIssue,
    layout_issue_icon as _layout_issue_icon,
)
from krok_helper.subtitle_render.frontend.widgets.font_loading import font_list_loading_overlay
from krok_helper.subtitle_render.frontend.dialogs.guide_replacement import (
    GuidePrefixMatch,
    GuidePrefixReplaceDialog,
    choose_guide_role_scheme,
    replacement_symbol_for_match,
)
from krok_helper.subtitle_render.frontend.workflow.import_controller import (
    N3ProjectImportController,
)
from krok_helper.subtitle_render.frontend.workflow.export_controller import (
    ExportJobController,
    ExportJobInputs,
)
from krok_helper.subtitle_render.frontend.workflow.export_view import (
    EXPORT_DIR_CUSTOM,
    EXPORT_DIR_SOURCE_VIDEO,
    EXPORT_PREVIEW_DEFAULT_WIDTH as _EXPORT_PREVIEW_DEFAULT_WIDTH,
    EXPORT_PREVIEW_MIN_WIDTH as _EXPORT_PREVIEW_MIN_WIDTH,
    ExportLocationDialog as _ExportLocationDialog,
    ExportMonitorView as _ExportMonitorView,
    ExportWorkspaceView,
    export_preview_width as _export_preview_width,
    format_elapsed_seconds as _format_elapsed_seconds,
    format_eta_seconds as _format_eta_seconds,
    format_warning_lines as _format_warning_lines,
    physical_preview_size as _physical_preview_size,
    scaled_preview_pixmap as _scaled_preview_pixmap,
    sync_export_preset_enabled,
)
from krok_helper.subtitle_render.frontend.editor.edit_history import (
    redo_edit,
    undo_edit,
)
from krok_helper.subtitle_render.frontend.preview.playback import (
    PlaybackController,
    unified_player_enabled,
)
from krok_helper.subtitle_render.frontend.preview.player_window import (
    AspectRatioBox as _AspectRatioBox,
    PreviewPlayerWindow,
    WindowEdgeGrip as _WindowEdgeGrip,
    fit_size_to_aspect,
)
from krok_helper.subtitle_render.frontend.preview.workspace import (
    PreviewWorkspaceView,
)
from krok_helper.subtitle_render.frontend.preview.preview_view import PreviewPanel, TransportBar
from krok_helper.subtitle_render.frontend.preview.preview_async import (
    DEFAULT_PREVIEW_QUALITY,
    gpu_preview_enabled,
    normalize_preview_quality,
)
from krok_helper.subtitle_render.frontend.preview.preview_controller import (
    PreviewDurationController,
    PreviewPreferenceController,
    PreviewWindowController,
)
from krok_helper.subtitle_render.frontend.project.project_commands import (
    ProjectCommandController,
    ProjectSaveAction,
)
from krok_helper.subtitle_render.frontend.project.project_autosave import (
    ProjectAutoSaveRuntime,
    RecoverySaveRequest,
)
from krok_helper.subtitle_render.frontend.project.project_recovery import (
    ProjectRecoveryController,
)
from krok_helper.subtitle_render.frontend.project.project_settings import (
    AutoSaveSettingsDialog as _AutoSaveSettingsDialog,
)
from krok_helper.subtitle_render.frontend.project.recent_projects import (
    RecentProjectsController,
)
from krok_helper.subtitle_render.frontend.project.source_watch import (
    SubtitleSourceWatchRuntime,
    WatchedSubtitleState as _WatchedSubtitleState,
    subtitle_source_digest,
    subtitle_source_key,
)
from krok_helper.subtitle_render.settings.screen import (
    ScreenSettings,
    SCREEN_FPS_OPTIONS,
    match_screen_preset_key,
    screen_settings_from_dict,
    screen_settings_to_dict,
)
from krok_helper.subtitle_render.frontend.widgets.workspace_switcher import WorkspaceSwitcher
from krok_helper.subtitle_render.domain.timing import (
    assign_role_to_track_rows,
    GuideSymbol,
    LineAnimationOverride,
    SubtitleLoadingSettings,
    TimingTrack,
    guide_symbol_has_visual,
    guide_symbol_replaces_prefix,
    guide_symbol_replacement_count,
    guide_symbol_role_labels,
    guide_symbol_with_role_labels,
    timing_line_start_ms,
)
from krok_helper.subtitle_render.serialization.timing import (
    guide_symbol_from_dict,
    guide_symbol_to_dict,
    line_animation_override_from_dict,
    line_animation_override_to_dict,
    subtitle_loading_settings_from_dict,
    subtitle_loading_settings_to_dict,
    track_page_plan_from_dict,
)
from krok_helper.subtitle_render.domain.models import (
    assign_role_to_title_rows,
    DEFAULT_EXPORT_NAME_TEMPLATE,
    DEFAULT_OUTPUT_NAME_SUFFIX,
    PROJECT_FILE_SUFFIX,
    StylePreset,
    SubtitleStyleScheme,
    Style,
    TITLE_SCHEME_NAME,
    TitleOverlay,
    ensure_page_layout_defaults,
    layout_capacity,
    layout_display_name,
    layout_id_for_index,
    normalize_title_char_role_labels,
    rescale_font_sizes,
    rescale_layout_sizes,
    subtitle_style_scheme_from_dict,
    subtitle_style_scheme_to_dict,
    style_from_dict,
    style_to_dict,
)
from krok_helper.subtitle_render.n3.font_catalog import (
    get_n3_font_catalog,
    normalize_scheme_font_families,
    normalize_style_font_families,
)
from krok_helper.subtitle_render.settings.preferences import (
    AppOutputPreferenceValues,
    AppPreferenceSaveInput,
    BUILTIN_SCHEME_STYLE_FIELDS as _BUILTIN_SCHEME_STYLE_FIELDS,
    DEFAULT_AUTO_SAVE_INTERVAL_MINUTES,
    DEFAULT_PROJECT_BACKUP_COUNT,
    DISCARDED_BACKUP_RETENTION_DAYS,
    LAYOUT_DEFAULT_STYLE_FIELDS as _LAYOUT_DEFAULT_STYLE_FIELDS,
    LAYOUT_DEFAULT_VALUE_FIELDS as _LAYOUT_DEFAULT_VALUE_FIELDS,
    load_app_style_preferences,
    load_app_runtime_preferences,
    prepare_app_preferences,
)
from krok_helper.subtitle_render.project.controller import (
    SubtitleProjectController,
)
from krok_helper.subtitle_render.project.load import ProjectLoadPlan
from krok_helper.subtitle_render.project.recovery import ProjectRecoveryPolicy
from krok_helper.subtitle_render.project.resources import (
    find_missing_project_resources,
)
from krok_helper.subtitle_render.project.recent import RecentProjectPolicy
from krok_helper.subtitle_render.engine.timing.auto_chorus import (
    DEFAULT_CHORUS_BEGIN_CHARS,
    DEFAULT_CHORUS_END_CHARS,
    apply_chorus_roles,
    pick_chorus_role,
)
from krok_helper.subtitle_render.frontend.dialogs.auto_chorus_dialog import AutoChorusDialog
from krok_helper.subtitle_render.n3.project_import import (
    N3_PROJECT_FILE_SUFFIX,
)
from krok_helper.subtitle_render.project.store import (
    ProjectFileRevision,
    RecoveryCandidate,
    project_output_payload,
    save_discarded_project_backup,
    save_recovery_project,
)
from krok_helper.subtitle_render.sources.loader import SubtitleSourceLoader
from krok_helper.subtitle_render.sources.reload import (
    apply_reloaded_tracks,
    merge_reloaded_track,
    prepare_reloaded_tracks,
)
from krok_helper.subtitle_render.project.session import (
    ExtraSubtitleSource,
    SubtitleProjectDocument,
    SubtitleProjectSession,
    SubtitleProjectState,
    SubtitleTrackMutation,
)
from krok_helper.subtitle_render.settings.store import SubtitleRenderSettingsStore
from krok_helper.subtitle_render.frontend.widgets.theme import palette, themed

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
PROJECT_FILTER = (
    f"字幕渲染项目 (*{PROJECT_FILE_SUFFIX} *{N3_PROJECT_FILE_SUFFIX});;"
    f"Yurika 项目 (*{PROJECT_FILE_SUFFIX});;"
    f"NicoKaraMaker3 项目 (*{N3_PROJECT_FILE_SUFFIX});;"
    "所有文件 (*.*)"
)
AUTO_SAVE_DEBOUNCE_MS = 2_000
_PERSISTED_STATE_SAVE_DEBOUNCE_MS = 1_500
"""应用级偏好落盘的空闲窗口：编辑停手后才写 settings.json。"""
AUTO_SAVE_THREAD_WAIT_MS = 3_000
GPU_PREVIEW_DEFAULT_VERSION = 2
GPU_EXPORT_DEFAULT_VERSION = 1
_RECENT_PROJECTS_SETTINGS_KEY = "recent_projects"
_MAX_RECENT_PROJECTS = 10
RENDER_WORKER_OPTIONS = (0, 4, 8, 12, 16)
"""0 = 自动（最多 8）；其余值为用户显式选择的渲染进程数。"""


#: 标题里跟着"用户习惯"走的字段（标题文字、显示时长这些是逐曲的，不在此列）。
#:
#: 淡入淡出时长改一次就该一直沿用 —— 每开一个新项目重设一遍 300 → 250 很烦。
#: 尾段那两项是 ``Optional``：``None`` 表示"跟随开头"，原样记住即可。
_TITLE_PREFERENCE_FIELDS = (
    "enabled",
    "layout_index",
    "fade_in_ms",
    "fade_out_ms",
    "tail_fade_in_ms",
    "tail_fade_out_ms",
)

#: 上面那几项里，纯粹的时长字段（``enabled`` / ``layout_index`` 另有存取方式）。
_TITLE_FADE_FIELDS = (
    "fade_in_ms",
    "fade_out_ms",
    "tail_fade_in_ms",
    "tail_fade_out_ms",
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






def _sug_software_compensation_ms() -> int:
    """打轴模块（SUG）「设置 → 导出 → 软件导出补偿」的当前值（毫秒）。

    SUG 只在导出（除 ``.sug`` 外的所有格式）时把它叠加到时间戳上，所以
    ``.sug`` 本体不含此补偿；KS 直读 ``.sug`` 时取该值即可复刻一次 SUG
    导出的结果。宿主把 SUG 设置树整体持久化在 ``AppSettings.lyrics_timing``
    （嵌套 dict，``export.software_compensation_ms`` 对应
    ``["export"]["software_compensation_ms"]``）。
    """

    try:
        sug_settings = load_app_settings().lyrics_timing
    except Exception:  # noqa: BLE001 — 设置读取失败按无补偿处理
        return 0
    export = (
        sug_settings.get("export") if isinstance(sug_settings, dict) else None
    )
    value = (
        export.get("software_compensation_ms")
        if isinstance(export, dict)
        else None
    )
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


class SubtitleRenderWindow(QWidget):
    """字幕视频渲染模块主 widget。"""

    projectStateChanged = Signal(object)
    _tracksViewWindowsReady = Signal(int, object)
    _embedded: bool = False

    # Project-document compatibility facade. The existing frontend and tests
    # can keep using their established private names while content ownership
    # moves out of the QWidget.
    @property
    def _timing_track(self) -> Optional[TimingTrack]:
        return self._project_document.timing_track

    @_timing_track.setter
    def _timing_track(self, value: Optional[TimingTrack]) -> None:
        self._project_document.timing_track = value

    @property
    def _extra_sources(self) -> list[ExtraSubtitleSource]:
        return self._project_document.extra_sources

    @_extra_sources.setter
    def _extra_sources(self, value: list[ExtraSubtitleSource]) -> None:
        self._project_document.extra_sources = list(value)

    @property
    def _subtitle_path(self) -> Optional[Path]:
        return self._project_document.subtitle_path

    @_subtitle_path.setter
    def _subtitle_path(self, value: Optional[Path]) -> None:
        self._project_document.subtitle_path = value

    @property
    def _video_path(self) -> Optional[Path]:
        return self._project_document.video_path

    @_video_path.setter
    def _video_path(self, value: Optional[Path]) -> None:
        self._project_document.video_path = value

    @property
    def _video_info(self) -> Optional[MediaInfo]:
        return self._project_document.video_info

    @_video_info.setter
    def _video_info(self, value: Optional[MediaInfo]) -> None:
        self._project_document.video_info = value

    @property
    def _background_source(self) -> Optional[BackgroundSource]:
        return self._project_document.background_source

    @_background_source.setter
    def _background_source(self, value: Optional[BackgroundSource]) -> None:
        self._project_document.background_source = value

    @property
    def _audio_path(self) -> Optional[Path]:
        return self._project_document.audio_path

    @_audio_path.setter
    def _audio_path(self, value: Optional[Path]) -> None:
        self._project_document.audio_path = value

    @property
    def _audio_info(self) -> Optional[MediaInfo]:
        return self._project_document.audio_info

    @_audio_info.setter
    def _audio_info(self, value: Optional[MediaInfo]) -> None:
        self._project_document.audio_info = value

    @property
    def _style(self) -> Style:
        return self._project_document.style

    @_style.setter
    def _style(self, value: Style) -> None:
        self._project_document.style = value

    # Compatibility facade for existing frontend code and tests. Mutable
    # lifecycle state has one owner (``_project_session``); these names remain
    # available while callers migrate to explicit session operations.
    @property
    def _project_path(self) -> Optional[Path]:
        return self._project_session.path

    @_project_path.setter
    def _project_path(self, value: Optional[Path]) -> None:
        self._project_session.path = value

    @property
    def _project_dirty(self) -> bool:
        return self._project_session.dirty

    @_project_dirty.setter
    def _project_dirty(self, value: bool) -> None:
        self._project_session.dirty = bool(value)

    @property
    def _project_saving(self) -> bool:
        return self._project_session.saving

    @_project_saving.setter
    def _project_saving(self, value: bool) -> None:
        self._project_session.saving = bool(value)

    @property
    def _project_save_error(self) -> Optional[str]:
        return self._project_session.save_error

    @_project_save_error.setter
    def _project_save_error(self, value: Optional[str]) -> None:
        self._project_session.save_error = value

    @property
    def _project_generation(self) -> int:
        return self._project_session.generation

    @_project_generation.setter
    def _project_generation(self, value: int) -> None:
        self._project_session.generation = int(value)

    @property
    def _project_revision(self) -> int:
        return self._project_session.revision

    @_project_revision.setter
    def _project_revision(self, value: int) -> None:
        self._project_session.revision = int(value)

    @property
    def _saved_revision(self) -> int:
        return self._project_session.saved_revision

    @_saved_revision.setter
    def _saved_revision(self, value: int) -> None:
        self._project_session.saved_revision = int(value)

    @property
    def _project_disk_revision(self) -> Optional[ProjectFileRevision]:
        return self._project_session.disk_revision

    @_project_disk_revision.setter
    def _project_disk_revision(self, value: Optional[ProjectFileRevision]) -> None:
        self._project_session.disk_revision = value

    @property
    def _missing_resources(self) -> tuple[tuple[str, Path], ...]:
        return self._project_session.missing_resources

    @_missing_resources.setter
    def _missing_resources(self, value: tuple[tuple[str, Path], ...]) -> None:
        self._project_session.missing_resources = tuple(value)

    @property
    def _unresolved_resource_labels(self) -> set[str]:
        return self._project_session.unresolved_resource_labels

    @_unresolved_resource_labels.setter
    def _unresolved_resource_labels(self, value: set[str]) -> None:
        self._project_session.unresolved_resource_labels = set(value)

    @property
    def _missing_resource_source_data(self) -> Optional[dict]:
        return self._project_session.missing_resource_source_data

    @_missing_resource_source_data.setter
    def _missing_resource_source_data(self, value: Optional[dict]) -> None:
        self._project_session.missing_resource_source_data = value

    @property
    def _auto_save_thread(self) -> Optional[QThread]:
        """Compatibility view of the extracted auto-save runtime thread."""
        return self._auto_save_runtime.thread

    @property
    def _auto_save_worker(self) -> Optional[QObject]:
        """Compatibility view of the extracted auto-save runtime worker."""
        return self._auto_save_runtime.worker

    @property
    def _auto_save_pending(self) -> bool:
        return self._auto_save_runtime.pending

    @_auto_save_pending.setter
    def _auto_save_pending(self, value: bool) -> None:
        self._auto_save_runtime.pending = bool(value)

    @property
    def _auto_save_timer(self) -> QTimer:
        """Compatibility view of the extracted debounce timer."""
        return self._auto_save_runtime.debounce_timer

    @property
    def _periodic_auto_save_timer(self) -> QTimer:
        """Compatibility view of the extracted periodic timer."""
        return self._auto_save_runtime.periodic_timer

    @property
    def _recent_project_paths(self) -> list[str]:
        """Compatibility view of the extracted recent-project state."""
        return self._recent_projects_controller.paths

    @_recent_project_paths.setter
    def _recent_project_paths(self, value: list[str]) -> None:
        self._recent_projects_controller.paths = list(value)

    @property
    def _preview_window_requested(self) -> bool:
        """Compatibility view of the extracted preview-window request state."""
        return self._preview_window_controller.requested

    @_preview_window_requested.setter
    def _preview_window_requested(self, value: bool) -> None:
        self._preview_window_controller.requested = bool(value)

    @property
    def _preview_reposition_on_next_show(self) -> bool:
        return self._preview_window_controller.reposition_on_next_show

    @_preview_reposition_on_next_show.setter
    def _preview_reposition_on_next_show(self, value: bool) -> None:
        self._preview_window_controller.reposition_on_next_show = bool(value)

    @property
    def _source_watcher(self):
        """Compatibility view of the extracted QFileSystemWatcher."""
        return self._source_watch_runtime.watcher

    @property
    def _source_change_timer(self) -> QTimer:
        """Compatibility view of the extracted source debounce timer."""
        return self._source_watch_runtime.timer

    @property
    def _source_watch_states(self) -> dict[str, _WatchedSubtitleState]:
        """Compatibility view of externally watched source baselines."""
        return self._source_watch_runtime.states

    @property
    def _pending_source_reload_keys(self) -> set[str]:
        return self._source_watch_runtime.pending_keys

    @property
    def _source_reload_retries(self) -> dict[str, int]:
        return self._source_watch_runtime.retries

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
        self._settings_store = SubtitleRenderSettingsStore(settings_provider)
        self._project_controller = SubtitleProjectController()
        self._subtitle_source_loader = SubtitleSourceLoader()
        self._n3_import_controller = N3ProjectImportController()
        self._export_job_controller = ExportJobController()
        self._export_runtime_controller = ExportRuntimeController()
        self._preview_duration_controller = PreviewDurationController()
        self._preview_window_controller = PreviewWindowController()
        self._project_command_controller = ProjectCommandController(
            PROJECT_FILTER,
            PROJECT_FILE_SUFFIX,
        )
        self._recovery_policy = ProjectRecoveryPolicy(self._recovery_root())
        self._project_recovery_controller = ProjectRecoveryController(
            self._recovery_policy
        )
        self._auto_save_runtime = ProjectAutoSaveRuntime(
            self,
            debounce_ms=AUTO_SAVE_DEBOUNCE_MS,
        )
        self._auto_save_runtime.saved.connect(self._on_recovery_auto_save_success)
        self._auto_save_runtime.failed.connect(self._on_recovery_auto_save_failure)
        self._auto_save_runtime.rerunRequested.connect(
            self._on_recovery_auto_save_rerun_requested
        )
        self._auto_save_runtime.saveRequested.connect(
            self._start_recovery_auto_save
        )
        self._workflow_context = workflow_context

        self._project_document = SubtitleProjectDocument()
        # 字幕轨道编辑的撤销/重做栈：兼容显示窗口四元组与逐行动画批量命令。
        self._undo_stack: list[tuple] = []
        self._redo_stack: list[tuple] = []
        # 副字幕源（N3 多歌词文件，如コーラス轨）与主字幕同帧叠绘。
        self._active_source_index = 0
        """歌词列表当前显示的源：0 = 主字幕，k >= 1 = ``_extra_sources[k-1]``。"""
        self._title_source_active = False
        """左侧列表当前是否显示末位的特殊「标题」源。"""
        self._audio_menu_actions: list[Action] = []
        self._app_default_style: Style = Style()
        self._subtitle_loading_defaults = SubtitleLoadingSettings()
        self._style_presets: dict[str, StylePreset] = {}
        #: 「自动识别和声」的上次选择（app 级偏好，随 subtitle_render 命名空间落盘）。
        self._auto_chorus_role = ""
        self._auto_chorus_begin_chars = DEFAULT_CHORUS_BEGIN_CHARS
        self._auto_chorus_end_chars = DEFAULT_CHORUS_END_CHARS
        self._auto_chorus_overwrite = False
        self._screen_settings: ScreenSettings = ScreenSettings()
        self._selected_scheme_key = "global"
        self._layout_assignment_preference: Optional[dict[str, object]] = None
        self._export_dir_mode = EXPORT_DIR_SOURCE_VIDEO
        self._export_custom_dir = ""
        self._export_name_template = DEFAULT_EXPORT_NAME_TEMPLATE
        self._project_session = SubtitleProjectSession()
        self._recent_project_policy = RecentProjectPolicy(
            project_suffix=PROJECT_FILE_SUFFIX,
            limit=_MAX_RECENT_PROJECTS,
        )
        self._recent_projects_controller = RecentProjectsController(
            self._recent_project_policy,
            self._settings_store,
            settings_key=_RECENT_PROJECTS_SETTINGS_KEY,
        )
        self._last_logged_project_state: Optional[tuple[object, ...]] = None
        self._loading_project = False
        self._syncing_screen_controls = False
        self._auto_save_enabled = True
        self._auto_save_interval_minutes = DEFAULT_AUTO_SAVE_INTERVAL_MINUTES
        self._project_backup_count = DEFAULT_PROJECT_BACKUP_COUNT
        self._handoff_probe_thread: Optional[QThread] = None
        self._handoff_probe_worker: Optional[_MediaProbeWorker] = None
        self._last_auto_save_error = ""
        self._render_thread: Optional[QThread] = None
        self._render_worker: Optional[Any] = None
        self._watch_primary_subtitle_source = False
        self._source_watch_runtime = SubtitleSourceWatchRuntime(
            self,
            reload_suspended=lambda: self._render_thread is not None,
        )
        self._source_watch_runtime.fileChanged.connect(
            self._on_subtitle_source_file_changed
        )
        self._source_watch_runtime.directoryChanged.connect(
            self._on_subtitle_source_directory_changed
        )
        self._source_watch_runtime.pendingReady.connect(
            self._process_subtitle_source_changes
        )
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
        self._closing_window = False
        self._suppress_next_render_command_log = False
        # 左右余白检查：属性面板每个 SpinBox tick 都会触发样式变更，
        # 用单发定时器合并成一次检查，提示只在结果变化时弹出。
        self._margin_check_timer = QTimer(self)
        self._margin_check_timer.setSingleShot(True)
        self._margin_check_timer.setInterval(400)
        self._margin_check_timer.timeout.connect(self._check_layout_margins)
        self._last_margin_warning_key = ""
        self._layout_issues: list[_LayoutIssue | _TimingIssue] = []
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
        self._recent_project_paths = self._load_recent_projects()
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
        if self._playback is not None:
            self._playback.shutdown()
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
        self._preview_preference_controller = PreviewPreferenceController(
            preview_panel=self._preview_panel,
            gpu_checkbox=self._gpu_preview_check,
            local_output_preferences=self._local_output_preferences,
            save_persisted_state=self._save_persisted_state,
            warn_gpu_unavailable=self._warn_gpu_preview_unavailable,
            warn_gpu_fallback=self._show_gpu_preview_fallback,
        )
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
        return self._preview_window_controller.context_allowed(
            host_visible=not self._closing_window and self.isVisible(),
            preview_tab_active=self._stack.currentWidget() is self._preview_tab,
            exporting=self._render_thread is not None,
        )

    def _hide_preview_window_for_context(self) -> None:
        """Pause and hide without treating the action as a user close."""
        if not hasattr(self, "_preview_window"):
            return
        self._preview_window_controller.hide(
            self._preview_window,
            self._transport_bar,
        )

    def _sync_preview_window_visibility(self) -> None:
        """Keep the top-level preview inside the preview-tab lifecycle."""
        if not hasattr(self, "_preview_window"):
            return
        self._preview_window_controller.sync(
            self._preview_window,
            self._transport_bar,
            context_allowed=self._preview_window_context_allowed(),
        )

    def _request_preview_window(self) -> None:
        self._preview_window_controller.request(
            self._preview_window,
            self._transport_bar,
            context_allowed=self._preview_window_context_allowed(),
        )

    def _on_preview_window_user_closed(self) -> None:
        if self._closing_window:
            return
        self._preview_window_controller.user_closed()

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
        self._recent_projects_menu = RoundMenu("最近打开的项目", menu)
        self._recent_projects_menu.setIcon(FIF.HISTORY)
        self._rebuild_recent_projects_menu()
        menu.addMenu(self._recent_projects_menu)
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

    @staticmethod
    def _recent_project_path_key(path: Path | str) -> str:
        """Return the platform-normalized key used to deduplicate recent paths."""
        return RecentProjectPolicy.path_key(path)

    def _load_recent_projects(self) -> list[str]:
        """Load valid native projects and prune stale or duplicate entries."""
        return self._recent_projects_controller.load()

    def _persist_recent_projects(self, paths: list[str]) -> None:
        """Persist only the recent-project field within the module namespace."""
        self._recent_projects_controller.persist(paths)

    def _rebuild_recent_projects_menu(self) -> None:
        """Refresh only the recent-project submenu and keep its parents intact."""
        self._recent_projects_controller.rebuild_menu(
            self._recent_projects_menu,
            open_recent=self._open_recent_project,
            clear_recent=self._clear_recent_projects,
        )

    def _set_recent_projects(self, paths: list[str]) -> None:
        """Update paths and rebuild only the recent-project submenu when changed."""
        rebuild = (
            self._rebuild_recent_projects_menu
            if hasattr(self, "_recent_projects_menu")
            else lambda: None
        )
        self._recent_projects_controller.set_paths(paths, rebuild=rebuild)

    def _record_recent_project(self, path: Path | str) -> None:
        """Move one successfully opened native project to the front."""
        self._recent_projects_controller.record(
            path,
            rebuild=self._rebuild_recent_projects_menu,
        )

    def _clear_recent_projects(self, _checked: bool = False) -> None:
        self._recent_projects_controller.clear(
            rebuild=self._rebuild_recent_projects_menu,
        )

    def _open_recent_project(self, file_path: str) -> None:
        self._recent_projects_controller.open(
            file_path,
            parent=self,
            rebuild=self._rebuild_recent_projects_menu,
            show_warning=fluent_warning,
            open_project=self._open_project_path,
        )

    def _balance_project_bar(self) -> None:
        if not hasattr(self, "_project_bar_left"):
            return
        self._project_bar_left.layout().invalidate()
        self._project_bar_right_balance.setFixedWidth(
            self._project_bar_left.sizeHint().width()
        )

    def _show_preview_window(self) -> None:
        if not hasattr(self, "_preview_window"):
            return
        self._request_preview_window()
        self._preview_window_controller.activate_visible(self._preview_window)

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
        return self._project_session.snapshot(
            has_project=has_project,
            exporting=self._render_thread is not None,
            recovery_path=(
                recovery_path
                if recovery_path is not None and recovery_path.is_file()
                else None
            ),
        )

    def connect_project_state_changed(self, callback: Callable[[object], Any]) -> None:
        """Subscribe the host without leaking the concrete Qt signal contract."""
        self.projectStateChanged.connect(callback)

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
        self._project_session.set_dirty(dirty)
        if dirty:
            self._schedule_recovery_auto_save()
        elif hasattr(self, "_auto_save_timer"):
            self._auto_save_timer.stop()
        self._refresh_project_title()

    def _mark_project_dirty(self) -> None:
        if self._loading_project:
            return
        was_dirty, had_save_error = self._project_session.mark_dirty()
        self._schedule_recovery_auto_save()
        if not was_dirty or had_save_error:
            self._refresh_project_title()

    def _begin_project_generation(self) -> None:
        """Invalidate recovery jobs belonging to the previously loaded project."""
        self._project_session.begin_generation()
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
        self._auto_save_runtime.configure(
            enabled=self._auto_save_enabled,
            interval_ms=self._auto_save_interval_minutes * 60 * 1000,
        )

    def _schedule_recovery_auto_save(self) -> None:
        self._auto_save_runtime.schedule(
            enabled=self._auto_save_enabled and not self._loading_project
        )

    def _recovery_payload_snapshot(self) -> tuple[dict, int]:
        snapshot = self._recovery_policy.capture(
            self._current_project_data,
            project_path=self._project_path,
            generation=self._project_generation,
            revision=self._project_revision,
        )
        return snapshot.payload, snapshot.snapshot_id

    def _start_recovery_auto_save(self) -> None:
        if not self._auto_save_enabled or not self._project_dirty or self._loading_project:
            return
        payload, snapshot_id = self._recovery_payload_snapshot()
        self._auto_save_runtime.start(
            RecoverySaveRequest(
                path=self._recovery_path(),
                payload=payload,
                generation=self._project_generation,
                revision=self._project_revision,
                snapshot_id=snapshot_id,
            )
        )

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

    def _on_recovery_auto_save_rerun_requested(self) -> None:
        if self._project_dirty and self._auto_save_enabled:
            QTimer.singleShot(0, self._start_recovery_auto_save)

    def _stop_auto_save_runtime(self, *, wait: bool) -> None:
        self._auto_save_runtime.stop_scheduling()
        if wait:
            self._wait_for_recovery_worker()

    def _wait_for_recovery_worker(self) -> bool:
        if self._auto_save_runtime.wait(AUTO_SAVE_THREAD_WAIT_MS):
            return True
        logging.getLogger(__name__).warning("等待字幕项目自动保存线程退出超时")
        return False

    @staticmethod
    def _cleanup_recovery_snapshot(path: Path, snapshot_id: int) -> None:
        ProjectRecoveryPolicy.cleanup_snapshot(path, snapshot_id)

    def _current_project_data(self) -> dict:
        if hasattr(self, "_export_width_spin"):
            self._flush_export_spin_edits()
        payload = self._project_document.to_project_data(
            screen=screen_settings_to_dict(self._screen_settings),
            selected_scheme_key=self._selected_scheme_key,
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
        return self._project_session.merge_unresolved_resource_references(payload)

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
        plan = ProjectLoadPlan.from_data(data)
        self._project_document.remember_project_data(plan.source_data)
        # 项目内容整体替换，旧的样式/轨道撤销记录全部失效
        self._clear_undo_history()
        # 1) 样式 / 屏幕 / 配色方案
        self._screen_settings = plan.screen
        # 字体目录冷构建可达秒级且必须留在主线程——先给出可见占位再读取
        with font_list_loading_overlay(self):
            catalog = get_n3_font_catalog()
        self._style, _font_names_changed = normalize_style_font_families(
            plan.style, catalog
        )
        if plan.selected_scheme_key is not None:
            self._selected_scheme_key = plan.selected_scheme_key
        self._property_panel.set_style(self._style)
        self._property_panel.set_current_scheme_key(self._selected_scheme_key)
        self._selected_scheme_key = self._property_panel.current_scheme_key()
        self._preview_panel.set_style(self._style)
        self._lyrics_panel.set_style(self._style)
        self._set_export_screen_controls(self._screen_settings)
        self._sync_preview_output_size()
        # 2) 导出参数
        self._apply_output_settings(plan.output)
        # 3) 素材（存在才加载；缺失静默跳过，不阻塞打开）
        if plan.subtitle_path is not None and plan.subtitle_path.is_file():
            self.load_subtitle_source(plan.subtitle_path)
            self._apply_line_breaks_before(plan.line_breaks_before)
            self._apply_line_layout_indices(plan.line_layout_indices)
            if self._timing_track is not None:
                self._restore_track_page_state(self._timing_track, plan.source_data)
            self._apply_char_role_labels(plan.char_role_labels)
            guide_mismatches = self._apply_guide_symbol_rows(
                self._timing_track, plan.line_guide_symbols
            )
            self._apply_inline_guide_symbol_rows(
                self._timing_track, plan.line_inline_guide_symbols
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
                    self._timing_track, plan.line_display_overrides
                )
                self._apply_animation_override_rows(
                    self._timing_track, plan.line_animation_overrides
                )
            self._refresh_tracks_view_windows()
        if self._defer_project_assets:
            self._queue_project_deferred_loads(plan)
        else:
            self._apply_extra_subtitle_sources(plan.extra_subtitle_sources)
            if plan.background is not None:
                self._load_background_payload(plan.background)
            elif (
                plan.fallback_video_path is not None
                and plan.fallback_video_path.is_file()
            ):
                self.load_video(plan.fallback_video_path)
            audio = plan.audio_path
            if audio is not None and audio.is_file() and audio != self._video_path:
                self.load_audio(audio)
        # Project/N3 role payloads are authoritative.  Populate missing role
        # schemes only after those payloads have replaced source-LRC markers;
        # otherwise a transient ``【アクア】`` marker can auto-create an unrelated
        # palette before FontIndex=0 clears it back to the global N3 scheme.
        self._apply_project_role_options(plan.project_role_names)

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
        self._apply_project_role_names(self._property_panel.role_names)
        self._lyrics_panel.set_role_options(self._merged_role_options())

    def _apply_project_role_names(self, role_names: list[str]) -> None:
        """Mirror the UI role registry into project-owned document state."""
        self._project_document.role_names = [str(name) for name in role_names if name]

    def _queue_project_deferred_loads(
        self,
        plan: ProjectLoadPlan,
    ) -> None:
        self._project_deferred_loads = [
            (asset.kind, asset.payload) for asset in plan.deferred_assets()
        ]
        self._project_deferred_load_generation = self._project_generation
        if self._project_deferred_loads:
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
        return self._project_command_controller.confirm_discard(
            self,
            dirty=self._project_dirty,
            choose=fluent_choice,
            save=self._save_project,
            discard=self.discard_unsaved,
        )

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
        self._project_session.adopt_project_identity(
            path=None,
            disk_revision=None,
        )
        self._set_project_dirty(False)

    def _clear_loaded_media(self) -> None:
        """清空已加载的字幕 / 视频 / 音频，把各面板复位到空态（新建项目用）。"""
        self._loading_project = True
        try:
            self._project_document.clear_loaded_media()
            self._active_source_index = 0
            self._title_source_active = False
            self._clear_undo_history()
            self._watch_primary_subtitle_source = False
            self._property_panel.set_n3_template_lyrics_directory(None)
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
        path = self._project_command_controller.choose_open_path(
            self,
            current_project_path=self._project_path,
            choose_file=QFileDialog.getOpenFileName,
        )
        if path is None:
            return
        self._open_project_path(path, confirm_discard=False)

    def _open_project_path(
        self,
        path: Path,
        *,
        confirm_discard: bool = True,
    ) -> bool:
        """Open a ``.yurika`` project selected from the menu or dropped。

        ``.n3proj`` 在这里改道走 N3 导入：它是个 zip，按 ``.yurika`` 的路子读会
        撞上一句谁也看不懂的 ``'utf-8' codec can't decode byte 0x.. ``。拖进来时
        本来就是按扩展名分流的，从「打开」菜单或命令行进来的却不是 —— 同一个文件
        两条路两种结果。
        """
        path = Path(path)
        if path.suffix.lower() == N3_PROJECT_FILE_SUFFIX:
            return self._import_n3_project_path(path, confirm_discard=confirm_discard)
        if confirm_discard and not self._confirm_discard_changes():
            return False
        try:
            loaded = self._project_controller.open(path)
        except (OSError, ValueError) as exc:
            fluent_error(
                self,
                "打开项目失败",
                f"无法读取项目文件：\n{path}\n\n{exc}",
                copyable=True,
            )
            return False
        data = loaded.data
        missing_resources = loaded.missing_resources
        self._begin_project_generation()
        self._clear_loaded_media()
        self._apply_project_data(data, defer_assets=True)
        self._project_session.adopt_project_identity(
            path=path,
            disk_revision=loaded.revision,
            missing_resources=missing_resources,
            source_data=data,
        )
        self._set_project_dirty(False)
        self._record_recent_project(path)
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
        return find_missing_project_resources(data)

    def _resolve_unresolved_resource_labels(self, labels: set[str]) -> None:
        """Drop unresolved references replaced explicitly by the user."""
        changed = self._project_session.resolve_missing_resource_labels(labels)
        if changed and not self._loading_project:
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
        path = self._n3_import_controller.choose_path(
            self,
            current_project_path=self._project_path,
            choose_file=QFileDialog.getOpenFileName,
        )
        if path is None:
            return
        self._import_n3_project_path(path, confirm_discard=False)

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
            result = self._n3_import_controller.load(path)
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
                self._style = self._n3_import_controller.rebase_style_for_video(
                    self._style,
                    video_height,
                )
                self._property_panel.set_style(self._style)
                self._preview_panel.set_style(self._style)
                self._lyrics_panel.set_style(self._style)
            self._sync_output_size_to_video(self._video_info)
        # 导入的是外来工程：保存时必须另存为 .yurika，因此视为未命名 + 有改动。
        missing_resources = self._missing_project_resources(result.project_data)
        self._project_session.adopt_project_identity(
            path=None,
            disk_revision=None,
            missing_resources=missing_resources,
            source_data=result.project_data,
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
        path = self._project_command_controller.choose_save_path(
            self,
            current_project_path=self._project_path,
            subtitle_path=self._subtitle_path,
            video_path=self._video_path,
            choose_file=QFileDialog.getSaveFileName,
        )
        if path is None:
            return False
        return self._write_project(path)

    def _write_project(self, path: Path) -> bool:
        path = Path(path)
        preflight = self._project_command_controller.preflight_save(
            self,
            path=path,
            current_project_path=self._project_path,
            known_disk_revision=self._project_disk_revision,
            inspect=self._project_controller.inspect,
            choose=fluent_choice,
        )
        if preflight.action == ProjectSaveAction.INSPECTION_FAILED:
            self._project_session.record_save_inspection_failure(preflight.error)
            self._refresh_project_title()
            fluent_error(
                self,
                "无法检查项目文件",
                f"保存前无法确认文件是否被外部修改：\n{path}\n\n{preflight.error}",
                copyable=True,
            )
            return False
        if preflight.action == ProjectSaveAction.SAVE_AS:
            return self._save_project_as()
        if preflight.action != ProjectSaveAction.CONTINUE:
            return False
        previous_recovery_path = self._recovery_path()
        self._recovery_policy.invalidate(previous_recovery_path, delete=False)
        self._auto_save_timer.stop()
        self._auto_save_pending = False
        self._wait_for_recovery_worker()
        revision_at_save = self._project_session.begin_save()
        self._refresh_project_title()
        try:
            saved_disk_revision = self._project_controller.save(
                path,
                self._current_project_data(),
                backup_root=self._backup_root(),
                backup_count=self._project_backup_count,
            )
        except (OSError, TypeError, ValueError) as exc:
            self._project_session.fail_save(str(exc))
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
        self._project_session.complete_save(
            path=path,
            disk_revision=saved_disk_revision,
            saved_revision=revision_at_save,
        )
        self._set_project_dirty(False)
        self._record_recent_project(path)
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
        page = PreviewWorkspaceView(
            owner=self,
            style=self._style,
            style_presets=self._style_presets,
            output_width=self._screen_settings.width,
            output_height=self._screen_settings.height,
            preview_fps=self._screen_settings.fps,
            splitter_ratio=self._preview_splitter_ratio,
            background_extensions={
                ".mp4",
                ".mkv",
                ".mov",
                ".webm",
                ".avi",
                ".flv",
                *IMAGE_EXTENSIONS,
                PROJECT_FILE_SUFFIX,
                N3_PROJECT_FILE_SUFFIX,
            },
        )
        page.layoutIssuesRequested.connect(self._show_layout_issues)
        page.previewWindowRequested.connect(self._show_preview_window)
        page.backgroundVideoRequested.connect(self._browse_video)
        page.backgroundImageRequested.connect(self._browse_background_image)
        page.backgroundSequenceRequested.connect(
            self._browse_background_sequence
        )
        page.solidBackgroundRequested.connect(self._choose_solid_background)

        controls = page.controls
        # Compatibility aliases keep the coordinator stable while the view
        # becomes the unique owner of preview workspace construction.
        self._preview_body_splitter = controls.body_splitter
        self._preview_splitter = controls.workspace_splitter
        self._preview_window = controls.preview_window
        self._preview_panel = controls.preview_panel
        self._transport_bar = controls.transport_bar
        self._lyrics_panel = controls.lyrics_panel
        self._property_panel = controls.property_panel
        self._layout_issues_button = controls.layout_issues_button
        self._show_preview_btn = controls.show_preview_button
        self._video_settings_panel = controls.video_settings_panel
        self._tracks_view = controls.tracks_view

        self._preview_window.userClosed.connect(
            self._on_preview_window_user_closed
        )
        self._preview_panel.pathDropped.connect(self._load_dropped_background)
        self._preview_panel.browseRequested.connect(self._browse_background_media)

        self._lyrics_panel.pathDropped.connect(self._load_dropped_subtitle)
        self._lyrics_panel.browseRequested.connect(self._browse_subtitle)
        self._lyrics_panel.roleChanged.connect(self._on_lyrics_role_changed)
        self._lyrics_panel.roleChangeRequested.connect(
            self._on_lyrics_roles_changed
        )
        self._lyrics_panel.charRolesChanged.connect(
            self._on_lyrics_char_roles_changed
        )
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
        self._lyrics_panel.autoChorusRequested.connect(
            self._on_auto_chorus_requested
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
        self._lyrics_panel.layoutChangeRequested.connect(
            self._on_layout_change_requested
        )
        self._lyrics_panel.sourceSelected.connect(self._on_source_selected)
        self._lyrics_panel.sourceAddRequested.connect(
            self._on_source_add_requested
        )
        self._lyrics_panel.sourceRemoveRequested.connect(
            self._on_source_remove_requested
        )
        self._lyrics_panel.sourceReplaceRequested.connect(
            self._on_source_replace_requested
        )
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

        self._transport_bar.timeChanged.connect(self._preview_panel.set_time)
        self._transport_bar.playbackStateChanged.connect(
            self._preview_panel.set_playing
        )
        self._transport_bar.previewQualityChanged.connect(
            self._on_preview_quality_changed
        )
        self._preview_panel.canvas.framePainted.connect(
            self._transport_bar.note_preview_frame_painted
        )
        self._preview_panel.gpuFallback.connect(self._on_gpu_preview_fallback)

        # Preserve the existing optional unified-player runtime ownership.
        self._playback: Optional[PlaybackController] = None
        if unified_player_enabled():
            controller = PlaybackController(self)
            if self._preview_panel.use_external_player(controller):
                self._playback = controller
                self._transport_bar.attach_playback_controller(controller)

        self._property_panel.styleChanged.connect(self._apply_style)
        self._property_panel.rolesChanged.connect(
            self._apply_project_role_names
        )
        self._property_panel.presetSchemesChanged.connect(
            self._apply_style_presets
        )
        self._property_panel.defaultSchemeSaveRequested.connect(
            self._save_builtin_scheme_default
        )
        self._property_panel.defaultLayoutSaveRequested.connect(
            self._save_layout_default
        )
        self._property_panel.schemeSelectionChanged.connect(
            self._on_scheme_selection_changed
        )
        self._property_panel.layoutAssignAllRequested.connect(
            self._on_layout_assign_all
        )
        self._property_panel.layoutAutoAssignRequested.connect(
            self._on_layout_auto_assign
        )
        self._property_panel.layoutDeleted.connect(self._on_layout_deleted)
        self._property_panel.backgroundBrowseRequested.connect(
            self._on_panel_background_browse
        )
        self._property_panel.backgroundClearRequested.connect(
            self._on_panel_background_clear
        )
        self._property_panel.backgroundSolidColorChanged.connect(
            self._on_panel_solid_color
        )
        self._property_panel.imageFitChanged.connect(
            self._on_panel_image_fit_changed
        )
        self._property_panel.audioBrowseRequested.connect(self._browse_audio)
        self._property_panel.audioClearRequested.connect(self._clear_audio)
        self._property_panel.screenSizeChanged.connect(
            self._on_panel_screen_size_changed
        )

        # Applying persisted selections can emit the same signals as a user
        # action, so retain the original project-loading guard.
        was_loading_project = self._loading_project
        self._loading_project = True
        try:
            self._property_panel.set_current_scheme_key(
                self._selected_scheme_key
            )
            self._selected_scheme_key = (
                self._property_panel.current_scheme_key()
            )
            self._sync_background_panel_state()
        finally:
            self._loading_project = was_loading_project

        self._video_settings_panel.pathDropped.connect(
            self._load_dropped_background
        )
        self._video_settings_panel.browseRequested.connect(
            self._browse_background_media
        )
        self._preview_splitter.splitterMoved.connect(
            self._on_preview_splitter_moved
        )
        self._tracks_view.seekRequested.connect(self._transport_bar.set_time)
        self._tracks_view.displayWindowEdited.connect(
            self._on_display_window_edited
        )
        self._transport_bar.timeChanged.connect(self._tracks_view.set_time)
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
        page = ExportWorkspaceView(
            fps_options=SCREEN_FPS_OPTIONS,
            render_worker_options=RENDER_WORKER_OPTIONS,
            gpu_preview_checked=gpu_preview_enabled(),
            gpu_controls_visible=sys.platform == "win32",
        )
        page.locationSettingsRequested.connect(
            self._open_export_location_settings
        )
        page.directoryEditingFinished.connect(
            self._on_export_directory_edited
        )
        page.browseRequested.connect(self._browse_export_output)
        page.encoderChanged.connect(self._update_export_preset_enabled)
        page.codecChanged.connect(self._refresh_export_format_label)
        page.startRequested.connect(self._start_render_export)
        page.stopRequested.connect(self._stop_render_export)

        controls = page.controls
        # Compatibility aliases keep the surrounding coordinator stable while
        # the view becomes the unique owner of widget construction.
        self._export_theme_labels = controls.theme_labels
        self._export_settings_col = controls.settings_col
        self._export_location_settings_button = controls.location_settings_button
        self._export_dir_edit = controls.directory_edit
        self._export_browse_button = controls.browse_button
        self._export_name_edit = controls.name_edit
        self._export_width_spin = controls.width_spin
        self._export_height_spin = controls.height_spin
        self._export_fps_combo = controls.fps_combo
        self._export_encoder_combo = controls.encoder_combo
        self._export_codec_combo = controls.codec_combo
        self._export_preset_combo = controls.preset_combo
        self._export_crf_spin = controls.crf_spin
        self._export_render_workers_combo = controls.render_workers_combo
        self._export_native_check = controls.native_check
        self._gpu_preview_check = controls.gpu_preview_check
        self._gpu_export_check = controls.gpu_export_check
        self._export_monitor_card = controls.monitor_card
        self._export_monitor_layout = controls.monitor_layout
        self._export_eta_label = controls.eta_label
        self._export_monitor_header = controls.monitor_header
        self._export_monitor_view = controls.monitor_view
        self._export_monitor_frame = controls.monitor_frame
        self._export_format_label = controls.format_label
        self._export_progress = controls.progress
        self._export_status_label = controls.status_label
        self._export_start_button = controls.start_button
        self._export_stop_button = controls.stop_button
        self._export_auto_name = ""

        # Keep export runtime ownership and visibility semantics unchanged.
        self._export_preview_timer = QTimer(self)
        self._export_preview_timer.setInterval(500)
        self._export_preview_timer.timeout.connect(self._poll_export_preview)
        self._export_preview_guard = UiActivityGuard(self)
        self._export_preview_activity = self._export_preview_guard.manage(
            self._export_preview_timer, on_resume=self._poll_export_preview
        )
        self._export_preview_guard.on_visibility(self._flush_pending_export_progress)
        self._export_pending_progress: Optional[tuple[int, int]] = None
        self._export_pending_log: Optional[str] = None
        self._export_preview_dir: Optional[Path] = None
        self._export_preview_file: Optional[Path] = None
        self._export_preview_mtime_ns = 0
        self._export_started_monotonic = 0.0

        self._update_export_preset_enabled()
        return page
    def _update_export_preset_enabled(self) -> None:
        sync_export_preset_enabled(
            self._export_encoder_combo,
            self._export_preset_combo,
        )

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
        # 拖入 = 替换当前选中的那一轨，与「替换」按钮同一条路径。选中「标题」时
        # 没有可替换的歌词文件，回落到主字幕（也是空态首次载入走的那条）。
        if self._timing_track is not None and not self._title_source_active:
            self._replace_source_file(self._active_source_index, path)
            return
        self.load_subtitle_source(path)

    def _on_panel_background_browse(self, kind: str) -> None:
        """「背景/音频」卡片点击 → 打开对应素材选择（与旧空态按钮同一组）。"""
        handlers = {
            "video": self._browse_video,
            "image": self._browse_background_image,
            "image_sequence": self._browse_background_sequence,
            "solid": self._choose_solid_background,
        }
        handler = handlers.get(kind)
        if handler is not None:
            handler()

    def _on_panel_background_clear(self) -> None:
        self.set_solid_background("#000000")

    def _on_panel_solid_color(self, color: str) -> None:
        """「背景/音频」纯色 ColorButton 的色值输入 / 取色 / 选色结果。"""
        qcolor = QColor(color)
        if qcolor.isValid():
            self.set_solid_background(qcolor.name())

    def _on_panel_image_fit_changed(self, fit: str) -> None:
        """图片缩放策略（铺满/黑边）：只改图片类背景，预览即时生效。"""
        source = self._background_source
        if source is None or source.kind not in {"image", "image_sequence"}:
            return
        if fit not in {"cover", "contain"} or source.image_fit == fit:
            return
        updated = replace(source, image_fit=fit)
        self._background_source = updated
        self._preview_panel.set_background_source(updated)
        self._property_panel.set_background_state(updated)
        self._mark_project_dirty()

    def _clear_audio(self) -> None:
        """移除独立音频（仅图片/图片序列/纯色背景配过时有效）。"""
        if self._audio_path is None and self._audio_info is None:
            self._sync_background_panel_state()
            return
        self._audio_path = None
        self._audio_info = None
        self._transport_bar.set_audio_source(None)
        self._refresh_transport_duration()
        self._sync_audio_action_enabled()
        self._sync_background_panel_state()
        self._mark_project_dirty()

    def _on_panel_screen_size_changed(self) -> None:
        """面板宽/高/帧率 → 导出页 spin（触发既有联动）与预览同步。"""
        width, height, fps = self._property_panel.screen_size()
        settings = ScreenSettings(
            preset_key=match_screen_preset_key(width, height, self._screen_settings.par),
            par=self._screen_settings.par,
            width=width,
            height=height,
            fps=fps,
        )
        self._set_export_screen_controls(settings)
        self._sync_preview_output_size()
        self._on_export_screen_changed()

    def _sync_background_panel_state(self) -> None:
        """把当前背景源 / 独立音频状态回填到「背景/音频」卡片。"""
        if not hasattr(self, "_property_panel"):
            return
        self._property_panel.set_background_state(
            self._background_source or BackgroundSource()
        )
        self._property_panel.set_audio_state(self._audio_path)

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
            track = self._subtitle_source_loader.load_lrc(path)
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
            track = self._subtitle_source_loader.load_sug(
                path,
                software_compensation_ms=self._sug_compensation_value(),
            )
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
            track = self._subtitle_source_loader.load_sug_project(
                project,
                nicokara_tags=nicokara_tags,
                software_compensation_ms=self._sug_compensation_value(),
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
        return subtitle_source_key(path)

    @staticmethod
    def _subtitle_source_digest(path: Path) -> str:
        return subtitle_source_digest(path)

    def _set_subtitle_source_baseline(self, path: Path, track: TimingTrack) -> None:
        self._source_watch_runtime.set_baseline(path, track)

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
        self._source_watch_runtime.sync(self._referenced_subtitle_sources())

    def _on_subtitle_source_file_changed(self, path_text: str) -> None:
        key = self._subtitle_source_key(Path(path_text))
        if self._source_watch_runtime.state(key) is not None:
            self._queue_subtitle_source_reload(key)
        # Editors may replace a file atomically, which removes Qt's file watch.
        self._sync_subtitle_source_watcher()

    def _on_subtitle_source_directory_changed(self, path_text: str) -> None:
        directory_key = self._subtitle_source_key(Path(path_text))
        for key, state in self._source_watch_runtime.states.items():
            if self._subtitle_source_key(state.path.parent) == directory_key:
                self._queue_subtitle_source_reload(key)
        self._sync_subtitle_source_watcher()

    def _queue_subtitle_source_reload(self, key: str) -> None:
        self._source_watch_runtime.queue(key)

    def _process_subtitle_source_changes(self) -> None:
        if self._render_thread is not None:
            return
        for key in self._source_watch_runtime.take_pending():
            self._reload_external_subtitle_source(key)

    def _retry_subtitle_source_reload(self, key: str, error: Exception) -> None:
        if self._source_watch_runtime.retry(key):
            return
        state = self._source_watch_runtime.state(key)
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
        state = self._source_watch_runtime.state(key)
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

        primary_track = None
        if (
            self._watch_primary_subtitle_source
            and self._subtitle_path is not None
            and self._timing_track is not None
            and self._subtitle_source_key(self._subtitle_path) == key
        ):
            primary_track = self._timing_track
        # 与手动刷新同口径：按所属轨道的加载设置决定是否应用 .sug 导出偏移。
        # 同一路径被多个源引用时沿用现有「单次解析」语义，取首个所属轨道。
        owner_track = primary_track
        if owner_track is None:
            owner_track = next(
                (
                    source.track
                    for source in self._extra_sources
                    if self._subtitle_source_key(source.path) == key
                ),
                None,
            )
        try:
            prepared = prepare_reloaded_tracks(
                path,
                seen_digest=state.seen_digest,
                baseline=state.baseline,
                load_candidate=lambda source_path: self._load_timing_track_file(
                    source_path,
                    apply_sug_export_compensation=(
                        self._sug_compensation_enabled_for_track(owner_track)
                    ),
                ),
                primary_track=primary_track,
                extra_tracks=(
                    (index, source.track)
                    for index, source in enumerate(self._extra_sources)
                    if self._subtitle_source_key(source.path) == key
                ),
            )
        except Exception as exc:  # noqa: BLE001 - partial external writes are retried
            self._retry_subtitle_source_reload(key, exc)
            return

        state.missing_notified = False
        self._source_watch_runtime.acknowledge(key)
        if prepared.candidate is None:
            self._sync_subtitle_source_watcher()
            return
        if prepared.plan is None:
            state.seen_digest = prepared.digest
            self._sync_subtitle_source_watcher()
            return

        candidate = prepared.candidate
        plan = prepared.plan
        if plan.conflicts:
            details = "\n".join(f"• {item}" for item in plan.conflicts[:8])
            suffix = "\n• 还有其他冲突……" if len(plan.conflicts) > 8 else ""
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
                state.seen_digest = prepared.digest
                self._sync_subtitle_source_watcher()
                return

        for track in apply_reloaded_tracks(self._project_document, plan):
            track.page_plan = build_legacy_page_plan(track, self._style)
            project_page_plan_to_legacy_fields(track, self._style)

        if plan.structure_changed:
            self._clear_undo_history()
        state.baseline = deepcopy(candidate)
        state.seen_digest = prepared.digest
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
                if plan.timing_only
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

    def load_video(
        self, path: Path, info: Optional[MediaInfo] = None
    ) -> Optional[MediaInfo]:
        """加载背景视频，调用 ffprobe 读取分辨率 / 帧率 / 时长。

        视频如果含音频流，会自动用作播放音轨——用户不需要再单独选音频。
        ``info`` 传入预探测结果时跳过内部 ffprobe（工作流交接的后台探测
        路径）；为 ``None`` 时同步探测并维持原有的错误弹窗行为。
        """
        if info is None:
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
        self._sync_background_panel_state()
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
        # H.264/H.265 的 yuv420p 输出要求偶数尺寸；源视频本身带奇数时
        # 向内收敛，避免导出渲染完整轮后才在编码器处失败。
        width -= width % 2
        height -= height % 2
        settings = ScreenSettings(
            preset_key=match_screen_preset_key(width, height, self._screen_settings.par),
            par=self._screen_settings.par,
            width=width,
            height=height,
            fps=self._export_fps_value(),
        )
        size_changed = (
            width != self._screen_settings.width or height != self._screen_settings.height
        )
        self._set_export_screen_controls(settings)
        self._sync_preview_output_size()
        self._refresh_export_format_label()
        self._on_export_screen_changed()
        if size_changed:
            InfoBar.info(
                title="输出尺寸已跟随背景视频",
                content=f"宽度和高度已改为 {width}×{height}；如需其他尺寸请在导出页重新填写。",
                parent=self,
                position=InfoBarPosition.BOTTOM_RIGHT,
                duration=3500,
            )

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
        self._sync_background_panel_state()
        if source.path:
            self._preview_window.set_media_title(Path(source.path))
        self._request_preview_window()
        self._refresh_transport_duration()
        self._mark_project_dirty()

    def _load_background_payload(self, payload: dict) -> None:
        kind = str(payload.get("kind") or "solid")
        path = Path(str(payload.get("path"))) if payload.get("path") else None
        raw_fit = str(payload.get("image_fit") or "cover")
        source = BackgroundSource(
            kind=kind if kind in {"video", "image", "image_sequence", "solid"} else "solid",
            path=str(path) if path is not None else None,
            color=str(payload.get("color") or "#000000"),
            source_fps=(int(payload["source_fps"]) if payload.get("source_fps") else None),
            sequence_start_number=max(int(payload.get("sequence_start_number") or 0), 0),
            video_offset_ms=int(payload.get("video_offset_ms") or 0),
            image_fit=raw_fit if raw_fit in {"cover", "contain"} else "cover",
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

    def load_audio(
        self, path: Path, info: Optional[MediaInfo] = None
    ) -> Optional[MediaInfo]:
        """为图片/图片序列/纯色背景加载独立音轨。

        视频背景严格使用内嵌音轨，避免预览形成两个媒体时钟。
        ``info`` 传入预探测结果时跳过内部 ffprobe（工作流交接的后台
        探测路径）；为 ``None`` 时同步探测并维持原有的错误弹窗行为。
        """
        if self._background_source is not None and self._background_source.kind == "video":
            fluent_warning(
                self,
                "无法添加独立音频",
                "视频背景只使用视频内嵌音轨，以避免双时钟造成音画不同步。",
            )
            return None
        if info is None:
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
        self._sync_background_panel_state()
        self._mark_project_dirty()
        return info

    def load_media_async(self, path: Path, *, as_video: bool) -> None:
        """工作流「进入下一步」交接入口：后台探测媒体后加载，不阻塞 UI。

        保存完成切到本页后由宿主调用。ffprobe 子进程在 Windows 上启动要
        上百毫秒，是交接链上仅剩的同步重活，这里把探测放到后台线程，
        完成后回 UI 线程调用 :meth:`load_video` / :meth:`load_audio` 并
        携带预探测结果，避免二次探测。探测失败回退为同步加载，沿用
        ``_probe`` 的错误弹窗语义。
        """
        thread = QThread(self)
        worker = _MediaProbeWorker(self._resolve_ffprobe_path(), path, as_video)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.probed.connect(self._on_handoff_media_probed)
        self._handoff_probe_worker = worker
        self._handoff_probe_thread = thread
        thread.start()

    def _on_handoff_media_probed(
        self, worker: "_MediaProbeWorker", info: Optional[MediaInfo]
    ) -> None:
        if self._handoff_probe_worker is not worker:
            return  # 已有更新的交接请求，丢弃过期探测结果
        self._handoff_probe_worker = None
        self._handoff_probe_thread = None
        if info is None:
            if worker.as_video:
                self.load_video(worker.media_path)
            else:
                self.load_audio(worker.media_path)
            return
        if worker.as_video:
            self.load_video(worker.media_path, info=info)
        else:
            self.load_audio(worker.media_path, info=info)

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
        self._preview_duration_controller.refresh(
            tracks=self._all_tracks(),
            video_info=self._video_info,
            audio_info=self._audio_info,
            tracks_view=self._tracks_view,
            preview_panel=self._preview_panel,
            transport_bar=self._transport_bar,
        )

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
                if 0 <= index < len(line.chars) and guide_symbol_has_visual(symbol)
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
                    if 0 <= index < len(line.chars) and guide_symbol_has_visual(
                        symbol
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
                # 副源在项目打开时按磁盘文件重新解析；导出偏移开关沿用该项目
                # 保存时该源的加载设置（旧项目快照没有该字段时按默认值应用）。
                apply_compensation = (
                    self._subtitle_loading_defaults.apply_sug_export_compensation
                )
                if str(item.get("loading_settings_mode") or "") == "custom":
                    apply_compensation = (
                        subtitle_loading_settings_from_dict(
                            item.get("loading_settings")
                        ).apply_sug_export_compensation
                    )
                try:
                    track = self._load_timing_track_file(
                        path, apply_sug_export_compensation=apply_compensation
                    )
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
        return self._project_document.tracks()

    def _extra_track_list(self) -> list[TimingTrack]:
        return [source.track for source in self._extra_sources]

    def _active_track(self) -> Optional[TimingTrack]:
        """歌词列表当前显示的 track（0 = 主字幕）。"""
        index = max(int(self._active_source_index), 0)
        return self._project_document.track_at(index) or self._timing_track

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
            # 「标题」没有歌词文件，不能换文件；其余每个源都能。
            replaceable_indices=set(range(len(self._extra_sources) + 1)),
        )

    def _refresh_lyrics_panel_source(self) -> None:
        """把当前选中源的行喂给歌词列表。"""
        if self._title_source_active:
            title = self._style.title_overlay
            if title is not None and self._timing_track is not None:
                title = replace(
                    title,
                    text_template=resolve_title_text(title, self._timing_track),
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
        self._margin_check_timer.start()
        self._mark_project_dirty()

    def _track_by_index(self, track_index: int) -> Optional[TimingTrack]:
        return self._project_document.track_at(track_index)

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
                or not guide_symbol_has_visual(item[1])
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
                    or not guide_symbol_has_visual(item[1])
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
        undo_edit(self._undo_stack, self._redo_stack, self)

    def _redo_edit(self) -> None:
        """Ctrl+Y / Ctrl+Shift+Z：重做被撤销的样式或字幕轨道编辑。"""
        redo_edit(self._undo_stack, self._redo_stack, self)

    def _clear_undo_history(self) -> None:
        self._undo_stack.clear()
        self._redo_stack.clear()

    def _effective_loading_settings(self, track: TimingTrack) -> SubtitleLoadingSettings:
        if track.loading_settings_mode == "custom" and track.loading_settings is not None:
            return track.loading_settings
        return self._subtitle_loading_defaults

    def _sug_compensation_enabled_for_track(
        self, track: Optional[TimingTrack]
    ) -> bool:
        """该轨道重新解析 ``.sug`` 时是否应用软件导出补偿（跟随其加载设置）。"""
        if track is None:
            return self._subtitle_loading_defaults.apply_sug_export_compensation
        return (
            self._effective_loading_settings(track).apply_sug_export_compensation
        )

    def _sug_compensation_value(self) -> int:
        """按全局加载设置解析 ``.sug`` 读取时使用的软件导出补偿值。"""
        if not self._subtitle_loading_defaults.apply_sug_export_compensation:
            return 0
        return _sug_software_compensation_ms()

    def _source_path_for_track_index(self, track_index: int) -> Optional[Path]:
        if track_index == 0:
            return self._subtitle_path
        if 1 <= track_index <= len(self._extra_sources):
            return self._extra_sources[track_index - 1].path
        return None

    def _set_track_by_index(self, track_index: int, track: TimingTrack) -> bool:
        return self._project_document.replace_track(track_index, track)

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

    def _record_track_mutation(self, mutation: SubtitleTrackMutation) -> None:
        self._record_track_snapshot(
            mutation.track_index,
            mutation.before,
            mutation.after,
        )

    def _on_page_boundary_requested(self, action: str, track_line_index: int) -> None:
        track = self._active_track()
        if track is None or self._title_source_active:
            return
        operation = {
            "insert_page": ("page", True),
            "delete_page": ("page", False),
            "insert_section": ("paragraph", True),
            "delete_section": ("paragraph", False),
        }.get(str(action))
        if operation is None:
            return
        kind, inserting = operation
        mutation = self._project_document.mutate_track(
            self._active_source_index,
            lambda target: (
                insert_boundary(
                    target, self._style, int(track_line_index), kind=kind
                )
                if inserting
                else delete_boundary(
                    target, self._style, int(track_line_index), kind=kind
                )
            ),
        )
        if mutation is None or not mutation.result:
            fluent_info(
                self,
                "无法修改边界",
                "当前位置没有可修改的边界，或合并后的页面超过 8 行。",
            )
            return
        if mutation.changed:
            self._record_track_mutation(mutation)
        self._refresh_after_track_structure_changed()

    def _on_page_move_requested(
        self, section_index: int, page_index: int, direction: int
    ) -> None:
        track = self._active_track()
        if track is None or self._title_source_active:
            return
        mutation = self._project_document.mutate_track(
            self._active_source_index,
            lambda target: move_page_boundary(
                target,
                self._style,
                int(section_index),
                int(page_index),
                direction=int(direction),
            ),
        )
        if mutation is None or not mutation.result:
            fluent_info(
                self,
                "无法移动歌词行",
                "目标页面已经达到 8 行、相邻页面不存在，或当前行不是可移动的分页边界行。",
            )
            return
        if mutation.changed:
            self._record_track_mutation(mutation)
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
            parsed_source = self._load_timing_track_file(
                path,
                apply_sug_export_compensation=(
                    settings.apply_sug_export_compensation
                ),
            )
            state = self._source_watch_runtime.state(
                self._subtitle_source_key(path)
            )
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
                content="已按保存的加载设置重新读取字幕，并生成段落、页面和按行数布局。",
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
            track = self._load_timing_track_file(
                path,
                apply_sug_export_compensation=(
                    self._subtitle_loading_defaults.apply_sug_export_compensation
                ),
            )
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

    def _load_timing_track_file(
        self, path: Path, *, apply_sug_export_compensation: bool = True
    ) -> TimingTrack:
        """按需解析字幕文件；``.sug`` 的软件导出补偿开关由调用方的加载设置决定。"""
        return self._subtitle_source_loader.load_file(
            path,
            software_compensation_ms=(
                _sug_software_compensation_ms()
                if apply_sug_export_compensation
                else 0
            ),
        )

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

    def _on_source_replace_requested(self, index: int) -> None:
        """换掉某个源的歌词文件：0 = 主字幕，k >= 1 = 第 k 个副字幕源。"""
        track_index = int(index)
        if self._timing_track is None or not 0 <= track_index <= len(self._extra_sources):
            return
        current = self._source_path_for_track_index(track_index)
        start_dir = str(current.parent) if current is not None else ""
        name = "主字幕" if track_index == 0 else self._extra_sources[track_index - 1].name
        path_str, _ = QFileDialog.getOpenFileName(
            self, f"替换「{name}」的歌词文件", start_dir, SUBTITLE_FILTER
        )
        if not path_str:
            return
        self._replace_source_file(track_index, Path(path_str))

    def _replace_source_file(self, track_index: int, path: Path) -> None:
        """把 ``track_index`` 指向的源换成 ``path``。主字幕走既有的整体加载路径。"""
        if track_index <= 0:
            self.load_subtitle_source(path)
            return
        extra_index = track_index - 1
        if not 0 <= extra_index < len(self._extra_sources):
            return
        try:
            track = self._load_timing_track_file(
                path,
                apply_sug_export_compensation=(
                    self._subtitle_loading_defaults.apply_sug_export_compensation
                ),
            )
        except Exception as exc:  # noqa: BLE001 — 统一错误弹窗
            fluent_error(
                self, "加载字幕失败", f"无法解析字幕文件：\n{path}\n\n错误：{exc}"
            )
            return
        # 与「添加副字幕源」同口径：新文件按全局加载设置重新分段分页。
        track.loading_settings_mode = "global"
        track.loading_settings = None
        track.loading_settings_snapshot = self._subtitle_loading_defaults
        track.page_plan = build_page_plan(
            track, self._subtitle_loading_defaults, self._style
        )
        project_page_plan_to_legacy_fields(track, self._style)
        self._apply_remembered_layout_assignment(track)
        self._apply_imported_role_preset_choices(track.role_options)
        self._set_subtitle_source_baseline(path, track)
        source = self._extra_sources[extra_index]
        renamed = source.name == source.path.stem
        source.path = path
        source.track = track
        # 名字是用户可见标识：只在它还是旧文件名（没被改过）时跟着新文件走。
        if renamed:
            source.name = path.stem
        self._active_source_index = track_index
        self._title_source_active = False
        # 换文件后旧的行索引全部失效
        self._clear_undo_history()
        self._refresh_source_ui()
        self._refresh_lyrics_panel_source()
        self._property_panel.merge_roles(self._content_role_options())
        self._lyrics_panel.set_role_options(self._merged_role_options())
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
        def apply_selected_pages(target: TimingTrack) -> set[int]:
            changed: set[int] = set()
            for row in rows:
                if isinstance(row, int) and 0 <= row < len(target.lines):
                    changed.update(
                        apply_layout_to_page(
                            target, self._style, row, int(layout_index)
                        )
                    )
            return changed

        mutation = self._project_document.mutate_track(
            self._active_source_index,
            apply_selected_pages,
        )
        if mutation is not None and mutation.changed:
            self._record_track_mutation(mutation)
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
        self._remember_layout_assignment("all", int(layout_index))
        mutation = self._project_document.mutate_track(
            self._active_source_index,
            lambda target: assign_layout_to_all(
                target, int(layout_index), self._style
            ),
        )
        if mutation is not None and mutation.changed:
            self._record_track_mutation(mutation)
            self._refresh_after_layout_assignment()

    def _on_layout_auto_assign(self) -> None:
        track = self._active_track()
        if track is None:
            return
        self._remember_layout_assignment("auto")
        mutation = self._project_document.mutate_track(
            self._active_source_index,
            lambda target: auto_assign_layouts_by_page(target, self._style),
        )
        if mutation is not None and mutation.changed:
            self._record_track_mutation(mutation)
            self._refresh_after_layout_assignment()

    def _on_layout_deleted(self, deleted_index: int) -> None:
        """布局被删除后修正歌词行引用（全部字幕源）：被删的回默认，其后的序号前移。"""
        track_indices = tuple(range(len(self._all_tracks())))
        def repair_layout_references(tracks: tuple[TimingTrack, ...]) -> None:
            for track in tracks:
                if track.page_plan is not None:
                    track.page_plan = normalize_page_plan(track, self._style)
                    project_page_plan_to_legacy_fields(track, self._style)
                    continue
                for line in track.lines:
                    index = int(getattr(line, "layout_index", 0) or 0)
                    if index == deleted_index:
                        line.layout_index = 0
                    elif index > deleted_index:
                        line.layout_index = index - 1

        mutation = self._project_document.mutate_tracks(
            track_indices,
            repair_layout_references,
        )
        if mutation is not None and mutation.changed:
            top = self._undo_stack[-1] if self._undo_stack else None
            if top is not None and top[0] == "style":
                self._undo_stack[-1] = (
                    "style_tracks",
                    top[1],
                    top[2],
                    mutation.track_indices,
                    mutation.before,
                    mutation.after,
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
        # 布局写在 track 上而不是 Style 上，``set_style`` 比对签名后会直接返回，
        # 「布局」列刷不到 —— 得单独叫一次。
        self._lyrics_panel.refresh_layout_assignments()
        self._sync_extra_tracks_to_preview()
        self._margin_check_timer.start()
        self._mark_project_dirty()

    def _collect_layout_issues(self) -> list[_LayoutIssue | _TimingIssue]:
        """Collect margin, timing and page-placement diagnostics."""
        tracks = self._all_tracks()
        source_names = ["主字幕", *(source.name for source in self._extra_sources)]
        issues: list[_LayoutIssue | _TimingIssue] = []
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
            diagnostics = layout_timing_diagnostics_for_style(
                self._screen_settings.width,
                self._screen_settings.height,
                track,
                self._style,
            )
            issues.extend(
                _TimingIssue(
                    track_index=track_index,
                    source_name=source_name,
                    diagnostic=diagnostic,
                )
                for diagnostic in diagnostics
                if diagnostic.line_indices
                and all(
                    0 <= line_index < len(track.lines)
                    for line_index in diagnostic.line_indices
                )
            )
        return issues

    def _set_layout_issues(
        self, issues: list[_LayoutIssue | _TimingIssue]
    ) -> None:
        self._layout_issues = list(issues)
        if hasattr(self, "_layout_issues_button"):
            count = len(issues)
            self._layout_issues_button.setToolTip(
                f"当前字幕诊断（{count} 条）" if count else "当前字幕没有诊断信息"
            )
            self._layout_issues_button.setAccessibleName(
                f"当前字幕诊断，{count} 条" if count else "当前字幕没有诊断信息"
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
            fluent_info(
                self,
                "当前字幕诊断",
                "没有发现歌词溢出、时间压缩或页面避让问题。",
            )
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
            if isinstance(issue, _LayoutIssue)
            if issue.warning.level == "overflow"
        ]
        margin = [
            issue.warning
            for issue in issues
            if isinstance(issue, _LayoutIssue)
            if issue.warning.level == "margin"
        ]
        key = "|".join(
            (
                f"{issue.track_index}:{issue.warning.line_index}:{issue.warning.level}"
                if isinstance(issue, _LayoutIssue)
                else f"{issue.track_index}:{issue.diagnostic.kind}:"
                f"{','.join(map(str, issue.diagnostic.line_indices))}:"
                f"{issue.diagnostic.summary}"
            )
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
        # 画面尺寸（宽/高/帧率）与随之重算的样式快照一起入撤销栈：
        # 误触改动可以用 Ctrl+Z 整体撤回（_rescale_layout_for_height 本身
        # 不入样式撤销栈，快照在这里统一补）。
        old_screen = screen_settings_to_dict(self._screen_settings)
        old_style = style_to_dict(self._style)
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
        if hasattr(self, "_property_panel"):
            # 「背景/音频」卡片里的画面尺寸与导出页双向联动
            # （panel 侧自身的 _syncing guard 拦住回环）。
            self._property_panel.set_screen_size(
                self._screen_settings.width,
                self._screen_settings.height,
                self._screen_settings.fps,
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
        self._record_screen_undo(old_screen, old_style)

    def _record_screen_undo(self, old_screen: dict, old_style: dict) -> None:
        """画面尺寸 + 高度重算样式入撤销栈；连续微调按 1.2s 窗口合并。"""
        new_screen = screen_settings_to_dict(self._screen_settings)
        new_style = style_to_dict(self._style)
        if old_screen == new_screen and old_style == new_style:
            return
        now = time.monotonic()
        top = self._undo_stack[-1] if self._undo_stack else None
        if (
            top is not None
            and top[0] == "screen"
            and now - top[5] <= self._STYLE_UNDO_MERGE_WINDOW_S
        ):
            # 合并：保留最早的旧快照，滚动更新新值与时间戳。
            if top[1] == new_screen and top[2] == new_style:
                self._undo_stack.pop()
            else:
                self._undo_stack[-1] = (
                    "screen", top[1], top[2], new_screen, new_style, now,
                )
        else:
            self._undo_stack.append(
                ("screen", old_screen, old_style, new_screen, new_style, now)
            )
            del self._undo_stack[:-_UNDO_STACK_LIMIT]
        self._redo_stack.clear()

    def _restore_screen(self, screen_payload: object, style_payload: object) -> bool:
        """撤销/重做时整体恢复画面尺寸与配套样式（不再录制新的撤销记录）。"""
        if not isinstance(screen_payload, dict):
            return False
        settings = screen_settings_from_dict(screen_payload)
        self._screen_settings = settings
        self._set_export_screen_controls(settings)
        self._sync_preview_output_size()
        self._refresh_export_format_label()
        self._transport_bar.set_preview_fps(settings.fps)
        self._margin_check_timer.start()
        self._schedule_persisted_state_save()
        self._mark_project_dirty()
        return self._restore_style(style_payload)

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
            self._property_panel.set_screen_size(
                settings.width, settings.height, settings.fps
            )

    def _flush_export_spin_edits(self) -> None:
        """提交宽/高输入框里尚未失焦的键盘编辑（导出 / 保存前兜底）。

        宽/高已关闭键盘跟踪，键入中的文本要等回车或失焦才生效；窗口级
        快捷键（如 Ctrl+S）不会移走焦点，这里手动收敛一次。只提交完整
        可解析的文本，半成品（清空、暂时越界）保持原样，留给用户继续
        输入或失焦时由 Qt 按常规规则处理——与属性面板数值字段的既定
        语义一致。
        """
        for spin in (self._export_width_spin, self._export_height_spin):
            editor = spin.lineEdit()
            state, _fixed, _pos = spin.validate(editor.text(), editor.cursorPosition())
            if state == QValidator.State.Acceptable:
                spin.interpretText()

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
        self._preview_preference_controller.apply_gpu_enabled(enabled)

    def _warn_gpu_preview_unavailable(self) -> None:
        fluent_warning(
            self,
            "GPU 预览不可用",
            "当前预览模式不支持 GPU 字幕层，已继续使用 Painter。",
        )

    def _on_preview_quality_changed(self, quality: str) -> None:
        """Apply and persist a local preview-only raster quality preference."""
        self._preview_preference_controller.apply_quality(quality)

    def _on_gpu_export_changed(self, _enabled: bool) -> None:
        """Persist GPU subtitle export independently from encoder selection."""
        self._save_persisted_state()

    def _on_gpu_preview_fallback(self, message: str) -> None:
        self._preview_preference_controller.report_gpu_fallback(message)

    def _show_gpu_preview_fallback(self, message: str) -> None:
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
        resolved = resolve_title_text(title, self._timing_track)
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
            assignment = assign_role_to_title_rows(title, rows, role_name)
            if assignment is None:
                return
            if assignment.role_label:
                self._materialize_role_schemes({assignment.role_label})
            self._property_panel.set_style(
                replace(
                    self._style,
                    title_overlay=assignment.title,
                ),
                emit=True,
            )
            return
        track_index = self._active_source_index
        track = self._track_by_index(track_index)
        if track is None:
            return
        assignment = assign_role_to_track_rows(track, rows, role_name)
        if assignment is None:
            return
        if assignment.role_label:
            self._materialize_role_schemes({assignment.role_label})
        self._undo_stack.append(
            (
                (
                    "inline_roles_batch"
                    if assignment.includes_guide_symbols
                    else "char_roles_batch"
                ),
                track_index,
                assignment.rows,
                assignment.old_values,
                assignment.new_values,
            )
        )
        del self._undo_stack[:-_UNDO_STACK_LIMIT]
        self._redo_stack.clear()
        self._refresh_after_role_labels_changed(assignment.rows)

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

    def _on_auto_chorus_requested(self) -> None:
        """右键「自动识别和声…」：整个歌词源按括号分配角色。"""
        track = self._active_track()
        if track is None or self._title_source_active:
            return
        role_options = self._content_role_options()
        dialog = AutoChorusDialog(
            role_options=role_options,
            selected_role=(
                self._auto_chorus_role
                if self._auto_chorus_role in role_options
                else pick_chorus_role(role_options) if role_options else ""
            ),
            begin_chars=self._auto_chorus_begin_chars,
            end_chars=self._auto_chorus_end_chars,
            overwrite=self._auto_chorus_overwrite,
            parent=self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        role = dialog.selected_role().strip() or pick_chorus_role(role_options)
        self._auto_chorus_role = role
        self._auto_chorus_begin_chars = dialog.begin_chars()
        self._auto_chorus_end_chars = dialog.end_chars()
        self._auto_chorus_overwrite = dialog.overwrite()
        self._schedule_persisted_state_save()

        changed_rows = self._apply_auto_chorus_roles(
            track,
            role=role,
            begin_chars=self._auto_chorus_begin_chars,
            end_chars=self._auto_chorus_end_chars,
            overwrite=self._auto_chorus_overwrite,
        )
        if not changed_rows:
            fluent_info(
                self,
                "没有找到和声",
                "整个歌词源里没有成对的起止字符，或者括号里的字符都已经分配过角色。",
            )
            return
        InfoBar.success(
            title="已识别和声",
            content=f"{len(changed_rows)} 行的括号内容已分配到「{role}」。",
            parent=self,
            position=InfoBarPosition.BOTTOM_RIGHT,
            duration=2500,
        )

    def _apply_auto_chorus_roles(
        self,
        track: TimingTrack,
        *,
        role: str,
        begin_chars: str,
        end_chars: str,
        overwrite: bool,
    ) -> tuple[int, ...]:
        """整源写回；**整批只入一条撤销**，否则撤销要按几十次。

        不动 ``line.guide_symbol``：导唱符是行首的引导标记，不属于括号里的和声段。
        """
        track_index = self._active_source_index
        rows: list[int] = []
        old_values: list[tuple] = []
        new_values: list[tuple] = []
        for row, line in enumerate(track.lines):
            if line.is_blank or not line.chars:
                continue
            current = [ch.role_label for ch in line.chars]
            updated = apply_chorus_roles(
                [ch.text for ch in line.chars],
                current,
                role,
                begin_chars=begin_chars,
                end_chars=end_chars,
                overwrite=overwrite,
            )
            if tuple(updated) == tuple(current):
                continue
            rows.append(row)
            old_values.append(tuple(current))
            new_values.append(tuple(updated))
        if not rows:
            return ()
        for row, labels in zip(rows, new_values):
            for ch, label in zip(track.lines[row].chars, labels):
                ch.role_label = label
        self._materialize_role_schemes({role})
        # 全新的角色名要靠 set_roles → _ensure_role_schemes 才会真的建出配色方案，
        # 不建 painter 解析不到、颜色不会变。它会另外留一条 style 撤销记录（"新建了
        # 一个配色方案"本来就该能单独撤销），排在角色那条前面，所以第一次撤销撤的
        # 仍然是角色分配。
        self._property_panel.set_roles(self._content_role_options())
        self._undo_stack.append(
            (
                "char_roles_batch",
                track_index,
                tuple(rows),
                tuple(old_values),
                tuple(new_values),
            )
        )
        del self._undo_stack[:-_UNDO_STACK_LIMIT]
        self._redo_stack.clear()
        self._refresh_after_role_labels_changed(tuple(rows))
        return tuple(rows)

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
            # 行首标记替换默认走 line.guide_symbol；但该槽位可能已被「不占字符」的
            # 行前导唱符占着（@Emoji 小头像就是这种），直接赋值会把它顶掉。这种行
            # 改用行内替换：小头像照旧画在行首，SVG 顶掉的仍是那几个真实字符。
            use_prefix_slot = match.is_prefix and (
                line.guide_symbol is None
                or guide_symbol_replaces_prefix(line.guide_symbol)
            )
            if use_prefix_slot:
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
            # 只有替代了这段标记的导唱符才跟着改角色；行前小头像/插入式导唱符不
            # 属于所选标记跨度，别把它一起染色。
            if prefix_selected and guide_symbol_replaces_prefix(line.guide_symbol):
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
            if not guide_symbol_has_visual(vector_symbol):
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
            bool(previous_title.enabled),
            int(previous_title.layout_index or 0),
            *(getattr(previous_title, name) for name in _TITLE_FADE_FIELDS),
        ) != (
            bool(current_title.enabled),
            int(current_title.layout_index or 0),
            *(getattr(current_title, name) for name in _TITLE_FADE_FIELDS),
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
            source_title = current_title if title_preference_changed else app_title
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
                    **{
                        name: getattr(source_title, name)
                        for name in _TITLE_FADE_FIELDS
                    },
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
        runtime_preferences = load_app_runtime_preferences(
            data,
            chorus_begin_default=DEFAULT_CHORUS_BEGIN_CHARS,
            chorus_end_default=DEFAULT_CHORUS_END_CHARS,
        )
        self._subtitle_loading_defaults = subtitle_loading_settings_from_dict(
            data.get("subtitle_loading_defaults")
        )
        self._local_output_preferences = runtime_preferences.output
        catalog = get_n3_font_catalog()
        loaded_style_preferences = load_app_style_preferences(
            data,
            font_catalog=catalog,
        )
        self._app_default_style = deepcopy(loaded_style_preferences.style)
        self._style = deepcopy(loaded_style_preferences.style)
        self._layout_assignment_preference = (
            deepcopy(loaded_style_preferences.layout_assignment)
            if loaded_style_preferences.layout_assignment is not None
            else None
        )
        style_changed = loaded_style_preferences.changed
        self._auto_chorus_role = runtime_preferences.auto_chorus_role
        self._auto_chorus_begin_chars = runtime_preferences.auto_chorus_begin_chars
        self._auto_chorus_end_chars = runtime_preferences.auto_chorus_end_chars
        self._auto_chorus_overwrite = runtime_preferences.auto_chorus_overwrite
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
        self._selected_scheme_key = runtime_preferences.selected_scheme_key
        self._preview_splitter_ratio = runtime_preferences.preview_splitter_ratio
        self._auto_save_enabled = runtime_preferences.auto_save_enabled
        self._auto_save_interval_minutes = (
            runtime_preferences.auto_save_interval_minutes
        )
        self._project_backup_count = runtime_preferences.project_backup_count
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
        if hasattr(self, "_export_width_spin"):
            self._flush_export_spin_edits()
        output_values = None
        if hasattr(self, "_export_native_check"):
            local_output = self._local_output_preferences
            output_values = AppOutputPreferenceValues(
                gpu_preview_enabled=self._gpu_preview_check.isChecked(),
                gpu_preview_default_version=GPU_PREVIEW_DEFAULT_VERSION,
                preview_quality=self._transport_bar.preview_quality(),
                gpu_export_enabled=self._gpu_export_check.isChecked(),
                gpu_export_default_version=GPU_EXPORT_DEFAULT_VERSION,
                directory_mode=self._export_dir_mode,
                custom_directory=self._export_custom_dir,
                name_template=self._export_name_template,
                encoder_mode=str(local_output.get("encoder_mode") or ENCODER_CPU),
                codec=str(local_output.get("codec") or CODEC_H264),
                preset=str(local_output.get("preset") or "medium"),
                crf=local_output.get("crf", 18),
                render_workers=local_output.get("render_workers", 0),
                allowed_render_workers=RENDER_WORKER_OPTIONS,
            )
        prepared = prepare_app_preferences(
            self._load_subtitle_settings(),
            AppPreferenceSaveInput(
                app_default_style=self._app_default_style,
                project_style=self._style,
                layout_assignment=self._layout_assignment_preference,
                subtitle_loading_defaults=subtitle_loading_settings_to_dict(
                    self._subtitle_loading_defaults
                ),
                style_presets=_style_presets_to_dict(self._style_presets),
                screen=screen_settings_to_dict(self._screen_settings),
                auto_chorus_role=self._auto_chorus_role,
                auto_chorus_begin_chars=self._auto_chorus_begin_chars,
                auto_chorus_end_chars=self._auto_chorus_end_chars,
                auto_chorus_overwrite=self._auto_chorus_overwrite,
                selected_scheme_key=self._selected_scheme_key,
                preview_splitter_ratio=self._preview_splitter_ratio,
                auto_save_enabled=self._auto_save_enabled,
                auto_save_interval_minutes=self._auto_save_interval_minutes,
                project_backup_count=self._project_backup_count,
                output=output_values,
            ),
        )
        self._app_default_style = prepared.app_default_style
        data = prepared.data
        try:
            self._settings_store.save(data)
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
            return self._settings_store.load()
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
        if hasattr(self, "_export_width_spin"):
            self._flush_export_spin_edits()
        directory = self._export_dir_edit.text().strip()
        name = self._normalized_export_name()
        default_name = self._default_export_name() if not name else ""
        result = self._export_job_controller.build(
            ExportJobInputs(
                track=self._timing_track,
                style=self._style,
                background_video_path=self._video_path,
                background_source=self._background_source,
                audio_path=(
                    self._audio_path
                    if self._audio_path is not None
                    and self._audio_path != self._video_path
                    else None
                ),
                output_directory=directory,
                output_name=name,
                default_output_name=default_name,
                extra_tracks=tuple(self._extra_track_list()),
                width=self._export_width_spin.value(),
                height=self._export_height_spin.value(),
                fps=self._export_fps_value(),
                duration_ms=self._current_export_duration_ms(),
                include_audio=bool(
                    self._audio_info and self._audio_info.audio_streams > 0
                ),
                encoder_mode=str(
                    self._export_encoder_combo.currentData() or ENCODER_CPU
                ),
                crf=self._export_crf_spin.value(),
                preset=str(self._export_preset_combo.currentData() or "medium"),
                codec=self._export_codec_value(),
                gpu_export_enabled=self._gpu_export_check.isChecked(),
                render_workers=self._export_render_workers_value(),
            )
        )
        if result.used_default_name:
            self._export_name_edit.setText(result.output_name)
            self._export_auto_name = result.output_name
        return result.job

    def _export_render_workers_value(self) -> Optional[int]:
        value = int(self._export_render_workers_combo.currentData() or 0)
        return value if value in RENDER_WORKER_OPTIONS[1:] else None

    def _current_export_duration_ms(self) -> int:
        return self._export_job_controller.resolve_duration_ms(
            self._all_tracks(),
            video_info=self._video_info,
            audio_info=self._audio_info,
        )

    def _confirm_project_saved_before_export(self) -> bool:
        """导出前先把工程落盘；返回 ``False`` 表示用户取消了这次导出。

        成片一旦导出，用户回头多半要照着同一版工程改字幕或配色。这里不给
        「不保存直接导出」那条路：要么存，要么这次不导。
        """
        if self._project_path is not None and not self._project_dirty:
            return True
        detail = (
            "当前项目有未保存的改动。"
            if self._project_path is not None
            else f"当前项目还没有保存成工程文件（{PROJECT_FILE_SUFFIX}）。"
        )
        if not fluent_question(
            self,
            "导出前先保存项目",
            f"{detail}\n保存后导出的成片才有对应的工程可以回头再改。",
            yes_text="保存并导出",
            no_text="取消导出",
            default_cancel=True,
        ):
            return False
        return self._save_project()

    def _start_render_export(self) -> None:
        if self._export_runtime_controller.is_active(
            self._current_export_runtime()
        ):
            fluent_info(self, "导出中", "当前导出任务还在处理中，请稍等。")
            return
        try:
            job = self._build_render_job()
        except ProcessingError as exc:
            fluent_error(self, "无法导出", str(exc))
            return
        # 先校验再问保存：素材都没齐时不该先把用户拖进另存为对话框。
        if not self._confirm_project_saved_before_export():
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
        self._export_preview_activity.set_desired(True)

        preview_width = _export_preview_width(
            self._export_monitor_view.size(),
            float(self._export_monitor_view.devicePixelRatioF()),
            job.width,
            job.height,
        )
        runtime = self._export_runtime_controller.prepare(
            parent=self,
            job=job,
            ffmpeg_dir=self._resolve_ffmpeg_dir(),
            preview_image_path=self._export_preview_file,
            preview_width=preview_width,
            callbacks=ExportRuntimeCallbacks(
                progress=self._on_render_progress,
                log=self._on_render_log,
                success=self._finish_render_success,
                cancelled=self._finish_render_cancelled,
                failed=self._finish_render_failure,
                thread_finished=self._clear_render_thread,
            ),
        )
        self._render_thread = runtime.thread
        self._render_worker = runtime.worker
        self._sync_preview_window_visibility()
        self._export_runtime_controller.start(runtime)
        self._refresh_project_title()

    def _stop_render_export(self) -> None:
        runtime = self._current_export_runtime()
        if runtime is None or not self._export_runtime_controller.is_active(
            runtime
        ):
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
        self._export_runtime_controller.cancel(runtime)

    def _current_export_runtime(self) -> Optional[ExportRuntimeHandles]:
        if self._render_thread is None or self._render_worker is None:
            return None
        return ExportRuntimeHandles(
            thread=self._render_thread,
            worker=self._render_worker,
        )

    def _on_render_progress(self, done: int, total: int) -> None:
        if not ui_active(self):
            # 导出 worker 全速推进；隐藏期间只留最新一帧数据，
            # 恢复可见（on_visibility 回调）时再落到 UI。
            self._export_pending_progress = (done, total)
            return
        self._apply_render_progress(done, total)

    def _apply_render_progress(self, done: int, total: int) -> None:
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

    def _flush_pending_export_progress(self) -> None:
        """恢复可见（或仍可见时的冗余调用）时重放隐藏期间攒下的导出状态。"""
        if not ui_active(self):
            return
        progress = self._export_pending_progress
        if progress is not None:
            self._export_pending_progress = None
            self._apply_render_progress(*progress)
        log = self._export_pending_log
        if log is not None:
            self._export_pending_log = None
            self._export_status_label.setText(log)

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
        if not ui_active(self):
            self._export_pending_log = message
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
        self._export_preview_activity.set_desired(False)
        # 任务收尾：三个 finish 路径都会重写导出 UI 终态，
        # 攒下的隐藏期进度不能在之后恢复可见时被重放。
        self._export_pending_progress = None
        self._export_pending_log = None
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
        self._source_watch_runtime.start_pending(0)

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
        return self._project_recovery_controller.has_pending()

    def check_crash_recovery(self, dialog_parent: Optional[QWidget] = None) -> bool:
        """Prompt for valid and corrupt recovery files; return True if restored."""
        parent = dialog_parent or self
        return self._project_recovery_controller.check(
            parent,
            choose=fluent_choice,
            show_error=fluent_error,
            restore=self._restore_recovery_candidate,
        )

    def _restore_recovery_candidate(self, candidate: RecoveryCandidate) -> bool:
        try:
            loaded = self._project_controller.open_recovery(candidate)
        except (OSError, ValueError) as exc:
            fluent_error(
                self,
                "恢复字幕项目失败",
                f"无法读取恢复文件：\n{candidate.path}\n\n{exc}",
            )
            return False
        data = loaded.data
        missing_resources = loaded.missing_resources
        self._begin_project_generation()
        self._clear_loaded_media()
        self._apply_project_data(data)
        self._project_session.adopt_project_identity(
            path=loaded.source_project_path,
            disk_revision=loaded.source_disk_revision,
            missing_resources=missing_resources,
            source_data=data,
        )
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
        return self._recovery_policy.path_for(self._project_path)

    def _cleanup_recovery_file(self, path: Optional[Path] = None) -> None:
        target = path or self._recovery_path()
        self._recovery_policy.invalidate(target)


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
