"""字幕视频渲染主窗口（Sayatoo 风格 + 左侧纵向导航 + 拖拽加载）。

照搬 SUG（lyrics_timing/.../frontend/main_window.py）的双模式骨架：

- ``SubtitleRenderWindow(embedded=False)`` — 默认 standalone
- ``SubtitleRenderWindow.for_embedding(parent, settings_provider, workflow_context)``
  — 嵌入工作台

UI 顶层结构（左侧 ``NavigationBar`` 纵向导航，仿 SUG MSFluentWindow 样式）：

  ┌────┬──────────────────────────────────────────────┐
  │ 预 │  ◆ 预览页（当前唯一可用）                       │
  │ 览 │    ┌─────────┬──────────────┬──────────────┐  │
  │    │    │ 左·歌词 │ 中·预览       │ 右·属性 tab │  │
  │ 导 │    │ (拖.lrc)│ + transport   │              │  │
  │ 出 │    ├─────────┴──────────────┴──────────────┤  │
  │    │    │ 底·字幕轨道                            │  │
  │    │    └─────────────────────────────────────────┘ │
  │    │  ◆ 导出页                                       │
  └────┴──────────────────────────────────────────────┘

三个素材区均接受拖拽 + 点击浏览（详见 :mod:`drop_panel`）。
"""

from __future__ import annotations

from pathlib import Path
import subprocess
from typing import Any, Optional

from PyQt6.QtCore import QObject, QRect, QSize, QThread, Qt, pyqtSignal as Signal
from PyQt6.QtGui import QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QFileDialog,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    Action,
    DropDownPushButton,
    FluentIcon as FIF,
    NavigationBar,
    RoundMenu,
)

from krok_helper.errors import ExportCancelled, ProcessingError
from krok_helper.ffmpeg import find_tool, probe_media, terminate_process
from krok_helper.models import MediaInfo
from krok_helper.settings import load_app_settings, save_app_settings
from krok_helper.subtitle_render.engine.encoder_select import (
    CPU_PRESETS,
    ENCODER_AMF,
    ENCODER_AUTO,
    ENCODER_CPU,
    ENCODER_NVENC,
    ENCODER_QSV,
)
from krok_helper.subtitle_render.engine.renderer import RenderJob, render_subtitle_video
from krok_helper.subtitle_render.engine.timeline import track_duration_ms
from krok_helper.subtitle_render.frontend.lyrics_list import LyricsPanel
from krok_helper.subtitle_render.frontend.preview_view import PreviewPanel, TransportBar
from krok_helper.subtitle_render.frontend.property_panel import (
    PropertyPanel,
    ScreenSettings,
    SCREEN_FPS_OPTIONS,
    match_screen_preset_key,
    screen_settings_from_dict,
    screen_settings_to_dict,
)
from krok_helper.subtitle_render.frontend.timeline_view import TrackTimelineView
from krok_helper.subtitle_render.models import (
    PROJECT_FILE_SUFFIX,
    Style,
    TimingTrack,
    style_from_dict,
    style_to_dict,
)
from krok_helper.subtitle_render.project_store import (
    load_render_project,
    project_output_payload,
    project_payload,
    save_render_project,
    split_project_paths,
)
from krok_helper.subtitle_render.subtitle_sources import load_nicokara_lrc
from krok_helper.subtitle_render.frontend.theme import control_qss, palette, themed

SUBTITLE_FILTER = "Nicokara 逐字 LRC (*.lrc);;所有文件 (*.*)"
VIDEO_FILTER = "视频文件 (*.mp4 *.mkv *.mov *.webm *.avi *.flv);;所有文件 (*.*)"
OUTPUT_FILTER = "MP4 视频 (*.mp4);;所有文件 (*.*)"
PROJECT_FILTER = f"字幕渲染项目 (*{PROJECT_FILE_SUFFIX});;所有文件 (*.*)"


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
        return QSize(960, 540)

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return QSize(426, 240)

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
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


class _RenderWorker(QObject):
    progressChanged = Signal(int, int)
    logMessage = Signal(str)
    finished = Signal(Path)
    cancelled = Signal(str)
    failed = Signal(str)

    def __init__(self, job: RenderJob, ffmpeg_dir: Optional[Path]) -> None:
        super().__init__()
        self._job = job
        self._ffmpeg_dir = ffmpeg_dir
        self._process: Optional[subprocess.Popen] = None
        self._cancel_requested = False

    def run(self) -> None:
        try:
            output = render_subtitle_video(
                self._job,
                ffmpeg_dir=self._ffmpeg_dir,
                logger=self.logMessage.emit,
                should_cancel=self.should_cancel,
                on_progress=self.progressChanged.emit,
                on_process_started=self._set_process,
            )
        except ExportCancelled as exc:
            self.cancelled.emit(str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))
            return
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


class SubtitleRenderWindow(QWidget):
    """字幕视频渲染模块主 widget。"""

    _embedded: bool = False

    def __init__(
        self,
        embedded: bool = False,
        settings_provider: Optional[Any] = None,
        workflow_context: Optional[Any] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._embedded = embedded
        self._settings_provider = settings_provider
        self._workflow_context = workflow_context

        self._timing_track: Optional[TimingTrack] = None
        self._subtitle_path: Optional[Path] = None
        self._video_path: Optional[Path] = None
        self._video_info: Optional[MediaInfo] = None
        self._audio_path: Optional[Path] = None
        self._audio_info: Optional[MediaInfo] = None
        self._style: Style = Style()
        self._screen_settings: ScreenSettings = ScreenSettings()
        self._selected_scheme_key = "global"
        self._project_path: Optional[Path] = None
        self._project_dirty = False
        self._loading_project = False
        self._syncing_screen_controls = False
        self._render_thread: Optional[QThread] = None
        self._render_worker: Optional[_RenderWorker] = None
        self._load_persisted_state()

        themed(
            self,
            lambda: f"SubtitleRenderWindow {{ background: {palette().shell_bg}; }}",
        )

        self._init_layout()
        self._init_shortcuts()

    # ------------------------------------------------------------------ layout

    def _init_layout(self) -> None:
        # 左侧纵向导航栏（图标 + 文字，仿 SUG MSFluentWindow 的 NavigationBar）+ 右侧内容。
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._nav = NavigationBar(self)
        root.addWidget(self._nav)

        content = QWidget(self)
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(16, 8, 16, 14)
        content_layout.setSpacing(6)
        root.addWidget(content, 1)

        # 顶部项目命令栏（新建 / 打开 / 保存 / 另存为 + 当前项目名）。standalone 与嵌入
        # 模式都显示——嵌入工作台里也能把当前字幕样式工程存成 .yurika 复用。
        self._project_bar = self._make_project_bar()
        content_layout.addWidget(self._project_bar)

        # QStackedWidget 承载各页内容
        self._stack = QStackedWidget(content)
        content_layout.addWidget(self._stack, 1)

        self._preview_tab = self._make_preview_tab()
        self._export_tab = self._make_export_tab()
        self._stack.addWidget(self._preview_tab)
        self._stack.addWidget(self._export_tab)
        self._set_export_screen_controls(self._screen_settings)
        self._sync_preview_output_size()
        self._export_width_spin.valueChanged.connect(self._sync_preview_output_size)
        self._export_height_spin.valueChanged.connect(self._sync_preview_output_size)
        self._export_width_spin.valueChanged.connect(self._on_export_screen_changed)
        self._export_height_spin.valueChanged.connect(self._on_export_screen_changed)
        self._export_fps_combo.currentIndexChanged.connect(self._on_export_screen_changed)

        self._nav.addItem(
            routeKey="preview",
            icon=FIF.VIEW,
            text="预览",
            onClick=lambda: self._stack.setCurrentIndex(0),
            selectable=True,
        )
        self._nav.addItem(
            routeKey="export",
            icon=FIF.SHARE,
            text="导出",
            onClick=lambda: self._stack.setCurrentIndex(1),
            selectable=True,
        )
        self._nav.setCurrentItem("preview")
        self._stack.setCurrentIndex(0)
        self._refresh_project_title()

    # ----------------------------------------------------------- 项目文件（A11）

    def _make_project_bar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("SrProjectBar")
        themed(bar, lambda: "#SrProjectBar { background: transparent; }")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        # 「文件管理 ▾」单个下拉，菜单含 新建/打开/保存/另存为（仿 SUG，省横向空间）。
        self._file_menu_btn = DropDownPushButton(FIF.FOLDER, "文件管理")
        self._file_menu_btn.setFixedHeight(30)
        menu = RoundMenu(parent=self._file_menu_btn)
        menu.addAction(Action(FIF.ADD, "新建", triggered=self._new_project))
        menu.addAction(Action(FIF.FOLDER, "打开", triggered=self._open_project))
        menu.addAction(Action(FIF.SAVE, "保存", triggered=self._save_project))
        menu.addAction(Action(FIF.SAVE_AS, "另存为", triggered=self._save_project_as))
        self._file_menu_btn.setMenu(menu)
        layout.addWidget(self._file_menu_btn)

        # 项目名：超长用 … 截断（完整名放 tooltip）。
        self._project_name_label = QLabel("")
        self._project_name_label.setMaximumWidth(260)
        themed(
            self._project_name_label,
            lambda: f"color: {palette().text_secondary}; font-size: 9.5pt;",
        )
        layout.addWidget(self._project_name_label)
        layout.addStretch(1)
        return bar

    def _refresh_project_title(self) -> None:
        if not hasattr(self, "_project_name_label"):
            return
        name = self._project_path.name if self._project_path else "未命名项目"
        full = f"{'● ' if self._project_dirty else ''}{name}"
        metrics = self._project_name_label.fontMetrics()
        elided = metrics.elidedText(
            full, Qt.TextElideMode.ElideRight, self._project_name_label.maximumWidth()
        )
        self._project_name_label.setText(elided)
        self._project_name_label.setToolTip(full if elided != full else "")

    def _mark_project_dirty(self) -> None:
        if self._loading_project:
            return
        if not self._project_dirty:
            self._project_dirty = True
            self._refresh_project_title()

    def _current_project_data(self) -> dict:
        independent_audio = (
            self._audio_path
            if self._audio_path is not None and self._audio_path != self._video_path
            else None
        )
        return project_payload(
            subtitle_path=self._subtitle_path,
            video_path=self._video_path,
            audio_path=independent_audio,
            style=style_to_dict(self._style),
            screen=screen_settings_to_dict(self._screen_settings),
            selected_scheme_key=self._selected_scheme_key,
            output=project_output_payload(
                encoder_mode=str(self._export_encoder_combo.currentData() or ENCODER_CPU),
                crf=self._export_crf_spin.value(),
                preset=str(self._export_preset_combo.currentData() or "veryfast"),
                output_path=self._export_output_edit.text().strip(),
            ),
        )

    def _apply_project_data(self, data: dict) -> None:
        self._loading_project = True
        try:
            self._apply_project_data_inner(data)
        finally:
            self._loading_project = False

    def _apply_project_data_inner(self, data: dict) -> None:
        # 1) 样式 / 屏幕 / 配色方案
        self._style = style_from_dict(data.get("style"))
        self._screen_settings = screen_settings_from_dict(data.get("screen"))
        key = data.get("selected_scheme_key")
        if isinstance(key, str) and key:
            self._selected_scheme_key = key
        self._property_panel.set_style(self._style)
        self._property_panel.set_screen_settings(self._screen_settings)
        self._property_panel.set_current_scheme_key(self._selected_scheme_key)
        self._preview_panel.set_style(self._style)
        self._set_export_screen_controls(self._screen_settings)
        self._sync_preview_output_size()
        # 2) 导出参数
        output = data.get("output") if isinstance(data.get("output"), dict) else {}
        self._apply_output_settings(output)
        # 3) 素材（存在才加载；缺失静默跳过，不阻塞打开）
        paths = split_project_paths(data)
        if paths["subtitle_path"] is not None and paths["subtitle_path"].is_file():
            self.load_from_lrc(paths["subtitle_path"])
        if paths["video_path"] is not None and paths["video_path"].is_file():
            self.load_video(paths["video_path"])
        audio = paths["audio_path"]
        if audio is not None and audio.is_file() and audio != self._video_path:
            self.load_audio(audio)

    def _apply_output_settings(self, output: dict) -> None:
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
        out_path = output.get("output_path")
        if isinstance(out_path, str) and out_path.strip():
            self._export_output_edit.setText(out_path.strip())

    def _confirm_discard_changes(self) -> bool:
        """有未保存改动时弹确认；返回 True 表示可以继续（已处理）。"""
        if not self._project_dirty:
            return True
        choice = QMessageBox.question(
            self,
            "未保存的改动",
            "当前项目有未保存的改动，是否先保存？",
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
        )
        if choice == QMessageBox.StandardButton.Cancel:
            return False
        if choice == QMessageBox.StandardButton.Save:
            return self._save_project()
        return True

    def _new_project(self) -> None:
        if not self._confirm_discard_changes():
            return
        self._clear_loaded_media()
        self._apply_project_data(
            {
                "style": style_to_dict(Style()),
                "screen": screen_settings_to_dict(ScreenSettings()),
                "selected_scheme_key": "global",
            }
        )
        self._project_path = None
        self._project_dirty = False
        self._refresh_project_title()

    def _clear_loaded_media(self) -> None:
        """清空已加载的字幕 / 视频 / 音频，把各面板复位到空态（新建项目用）。"""
        self._loading_project = True
        try:
            self._timing_track = None
            self._subtitle_path = None
            self._video_path = None
            self._video_info = None
            self._audio_path = None
            self._audio_info = None
            # 歌词列表回空态
            self._lyrics_panel.set_track(None)
            # 预览回空态：清字幕 + 视频 + 取消 populated
            self._preview_panel.set_track(None)
            self._preview_panel.set_video_source(None)
            self._preview_panel.set_populated(False)
            self._property_panel.set_singers([])
            # 播放条复位
            self._transport_bar.set_audio_source(None)
            self._transport_bar.set_time(0)
            self._transport_bar.set_duration(0)
        finally:
            self._loading_project = False

    def _open_project(self) -> None:
        if not self._confirm_discard_changes():
            return
        start_dir = str(self._project_path.parent) if self._project_path else ""
        path_str, _ = QFileDialog.getOpenFileName(
            self, "打开字幕渲染项目", start_dir, PROJECT_FILTER
        )
        if not path_str:
            return
        try:
            data = load_render_project(Path(path_str))
        except (OSError, ValueError) as exc:
            QMessageBox.critical(self, "打开项目失败", f"无法读取项目文件：\n{path_str}\n\n{exc}")
            return
        self._apply_project_data(data)
        self._project_path = Path(path_str)
        self._project_dirty = False
        self._refresh_project_title()

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
        try:
            save_render_project(path, self._current_project_data())
        except OSError as exc:
            QMessageBox.critical(self, "保存项目失败", f"无法写入项目文件：\n{path}\n\n{exc}")
            return False
        self._project_path = path
        self._project_dirty = False
        self._refresh_project_title()
        return True

    def _make_preview_tab(self) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(8)

        body = QSplitter(Qt.Orientation.Vertical)
        body.setChildrenCollapsible(False)

        # 上半部：左·歌词 ┃ 中·预览 ┃ 右·属性
        top = QSplitter(Qt.Orientation.Horizontal)
        top.setChildrenCollapsible(False)
        self._preview_splitter = top

        self._lyrics_panel = LyricsPanel()
        self._lyrics_panel.pathDropped.connect(self.load_from_lrc)
        self._lyrics_panel.browseRequested.connect(self._browse_subtitle)
        top.addWidget(self._lyrics_panel)

        center = QWidget()
        center_layout = QVBoxLayout(center)
        center_layout.setContentsMargins(0, 0, 0, 0)
        center_layout.setSpacing(0)
        self._preview_panel = PreviewPanel()
        self._preview_panel.set_style(self._style)
        self._preview_panel.pathDropped.connect(self.load_video)
        self._preview_panel.browseRequested.connect(self._browse_video)
        self._preview_frame = _AspectRatioBox(self._preview_panel)
        center_layout.addWidget(self._preview_frame, 1)
        self._transport_bar = TransportBar()
        self._transport_bar.set_preview_fps(self._screen_settings.fps)
        self._transport_bar.timeChanged.connect(self._preview_panel.set_time)
        self._transport_bar.playbackStateChanged.connect(self._preview_panel.set_playing)
        center_layout.addWidget(self._transport_bar)
        top.addWidget(center)

        self._property_panel = PropertyPanel()
        self._property_panel.set_style(self._style)
        self._property_panel.set_screen_settings(self._screen_settings)
        self._property_panel.styleChanged.connect(self._apply_style)
        self._property_panel.screenChanged.connect(self._apply_screen_settings)
        self._property_panel.schemeSelectionChanged.connect(self._on_scheme_selection_changed)
        self._property_panel.set_current_scheme_key(self._selected_scheme_key)
        top.addWidget(self._property_panel)

        top.setStretchFactor(0, 1)
        top.setStretchFactor(1, 4)
        top.setStretchFactor(2, 0)
        top.setSizes([340, 900, self._property_panel.minimumWidth()])
        body.addWidget(top)

        # 底部：字幕轨道（波形已移除，不做波形图功能）
        self._tracks_view = TrackTimelineView()
        body.addWidget(self._tracks_view)

        body.setStretchFactor(0, 6)
        body.setStretchFactor(1, 2)
        body.setSizes([560, 160])

        outer.addWidget(body, 1)
        return page

    def _init_shortcuts(self) -> None:
        # 空格键播放 / 暂停（窗口范围内有效，避免误伤未来的文本输入）
        self._space_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Space), self)
        self._space_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._space_shortcut.activated.connect(self._transport_bar.toggle_play)

        # 项目文件快捷键。作用域限制在本模块内（WidgetWithChildrenShortcut），
        # 嵌入工作台时不会和宿主的全局快捷键打架。
        self._project_shortcuts = []
        for seq, handler in (
            (QKeySequence.StandardKey.New, self._new_project),
            (QKeySequence.StandardKey.Open, self._open_project),
            (QKeySequence.StandardKey.Save, self._save_project),
            (QKeySequence.StandardKey.SaveAs, self._save_project_as),
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
            lambda: (
                f"""
                #SubtitleExportPage {{
                    background: transparent;
                }}
                {control_qss("#SubtitleExportPage")}
                """
            ),
        )
        layout = QVBoxLayout(page)
        layout.setContentsMargins(32, 28, 32, 32)
        layout.setSpacing(14)

        title = QLabel("导出 MP4")
        themed(
            title,
            lambda: f"color: {palette().title_text}; font-size: 16pt; font-weight: 700;",
        )
        layout.addWidget(title)

        output_row = QHBoxLayout()
        output_row.setContentsMargins(0, 0, 0, 0)
        output_row.setSpacing(8)
        self._export_output_edit = QLineEdit()
        self._export_output_edit.setPlaceholderText("选择输出 MP4 路径")
        self._export_browse_button = QPushButton("浏览")
        self._export_browse_button.clicked.connect(self._browse_export_output)
        output_row.addWidget(self._export_output_edit, 1)
        output_row.addWidget(self._export_browse_button)
        layout.addLayout(output_row)

        params_row = QHBoxLayout()
        params_row.setContentsMargins(0, 0, 0, 0)
        params_row.setSpacing(10)
        self._export_width_spin = self._export_spin(160, 7680, 1920, " 宽")
        self._export_height_spin = self._export_spin(90, 4320, 1080, " 高")
        self._export_fps_combo = QComboBox()
        self._export_fps_combo.setMinimumHeight(32)
        for fps in SCREEN_FPS_OPTIONS:
            self._export_fps_combo.addItem(f"{fps} fps", fps)
        params_row.addWidget(self._labeled_export_control("宽度", self._export_width_spin))
        params_row.addWidget(self._labeled_export_control("高度", self._export_height_spin))
        params_row.addWidget(self._labeled_export_control("帧率", self._export_fps_combo))
        layout.addLayout(params_row)

        encode_row = QHBoxLayout()
        encode_row.setContentsMargins(0, 0, 0, 0)
        encode_row.setSpacing(10)
        self._export_encoder_combo = QComboBox()
        self._export_encoder_combo.setMinimumHeight(32)
        self._export_encoder_combo.addItem("CPU / libx264", ENCODER_CPU)
        self._export_encoder_combo.addItem("自动硬编", ENCODER_AUTO)
        self._export_encoder_combo.addItem("NVIDIA NVENC", ENCODER_NVENC)
        self._export_encoder_combo.addItem("Intel QSV", ENCODER_QSV)
        self._export_encoder_combo.addItem("AMD AMF", ENCODER_AMF)
        self._export_preset_combo = QComboBox()
        self._export_preset_combo.setMinimumHeight(32)
        for preset in CPU_PRESETS:
            self._export_preset_combo.addItem(preset, preset)
        self._export_preset_combo.setCurrentText("veryfast")
        self._export_crf_spin = self._export_spin(0, 51, 18, " CRF")
        encode_row.addWidget(self._labeled_export_control("编码器", self._export_encoder_combo))
        encode_row.addWidget(self._labeled_export_control("CPU preset", self._export_preset_combo))
        encode_row.addWidget(self._labeled_export_control("质量", self._export_crf_spin))
        layout.addLayout(encode_row)

        self._export_progress = QProgressBar()
        self._export_progress.setRange(0, 1)
        self._export_progress.setValue(0)
        layout.addWidget(self._export_progress)

        self._export_status_label = QLabel("加载字幕和背景视频后即可导出。")
        themed(self._export_status_label, lambda: f"color: {palette().text_hint}; font-size: 10pt;")
        layout.addWidget(self._export_status_label)

        action_row = QHBoxLayout()
        action_row.setContentsMargins(0, 0, 0, 0)
        action_row.setSpacing(8)
        self._export_start_button = QPushButton("开始导出")
        self._export_start_button.setMinimumHeight(38)
        self._export_start_button.clicked.connect(self._start_render_export)
        self._export_stop_button = QPushButton("停止导出")
        self._export_stop_button.setMinimumHeight(38)
        self._export_stop_button.setEnabled(False)
        self._export_stop_button.clicked.connect(self._stop_render_export)
        action_row.addWidget(self._export_start_button, 1)
        action_row.addWidget(self._export_stop_button)
        layout.addLayout(action_row)

        layout.addStretch(1)
        return page

    @staticmethod
    def _export_spin(minimum: int, maximum: int, value: int, suffix: str) -> QSpinBox:
        spin = QSpinBox()
        spin.setRange(minimum, maximum)
        spin.setValue(value)
        spin.setSuffix(suffix)
        spin.setMinimumHeight(32)
        return spin

    def _labeled_export_control(self, label_text: str, control: QWidget) -> QWidget:
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        label = QLabel(label_text)
        themed(label, lambda: f"color: {palette().text_secondary}; font-size: 9.5pt;")
        layout.addWidget(label)
        layout.addWidget(control)
        return box

    # ------------------------------------------------------------------ browse fallback

    def _browse_subtitle(self) -> None:
        start_dir = str(self._subtitle_path.parent) if self._subtitle_path else ""
        path_str, _ = QFileDialog.getOpenFileName(
            self, "选择 Nicokara 逐字 LRC 文件", start_dir, SUBTITLE_FILTER
        )
        if path_str:
            self.load_from_lrc(Path(path_str))

    def _browse_video(self) -> None:
        start_dir = str(self._video_path.parent) if self._video_path else ""
        path_str, _ = QFileDialog.getOpenFileName(
            self, "选择背景视频", start_dir, VIDEO_FILTER
        )
        if path_str:
            self.load_video(Path(path_str))

    def _browse_export_output(self) -> None:
        start = self._default_export_path()
        path_str, _ = QFileDialog.getSaveFileName(self, "导出字幕视频", str(start), OUTPUT_FILTER)
        if path_str:
            path = Path(path_str)
            if path.suffix.lower() != ".mp4":
                path = path.with_suffix(".mp4")
            self._export_output_edit.setText(str(path))

    # ------------------------------------------------------------------ public

    def load_from_lrc(self, path: Path) -> Optional[TimingTrack]:
        """加载 Nicokara 逐字 LRC 文件。返回解析结果（失败返回 None 并弹错）。"""
        try:
            track = load_nicokara_lrc(path)
        except Exception as exc:  # noqa: BLE001 — 暴露给用户的统一错误处理
            QMessageBox.critical(
                self, "加载字幕失败", f"无法解析字幕文件：\n{path}\n\n错误：{exc}"
            )
            return None
        self._timing_track = track
        self._subtitle_path = path
        self._lyrics_panel.set_track(track)
        self._property_panel.set_singers(track.singer_options)
        self._property_panel.set_current_scheme_key(self._selected_scheme_key)
        self._preview_panel.set_track(track)
        self._refresh_transport_duration()
        self._transport_bar.set_time(0)
        self._mark_project_dirty()
        return track

    def load_video(self, path: Path) -> Optional[MediaInfo]:
        """加载背景视频，调用 ffprobe 读取分辨率 / 帧率 / 时长。

        视频如果含音频流，会自动用作播放音轨——用户不需要再单独选音频。
        """
        info = self._probe(path, "视频")
        if info is None:
            return None
        if info.video_streams == 0:
            QMessageBox.warning(self, "背景视频不可用", f"该文件不含视频流：\n{path}")
            return None
        self._video_path = path
        self._video_info = info
        self._preview_panel.set_video_source(path)
        if not self._export_output_edit.text().strip():
            self._export_output_edit.setText(str(self._default_export_path()))
        # 视频自带音频 → 喂给 TransportBar 走 QMediaPlayer 播放
        if info.audio_streams > 0:
            self._audio_path = path
            self._audio_info = info
            self._transport_bar.set_audio_source(path)
        self._refresh_transport_duration()
        self._mark_project_dirty()
        return info

    def load_audio(self, path: Path) -> Optional[MediaInfo]:
        """加载独立音轨（覆盖视频自带音频）。

        当前 UI 不直接暴露此入口；保留为 API，便于将来高级用户 / 测试 /
        嵌入工作流（A10）从外部喂独立音频。
        """
        info = self._probe(path, "音频")
        if info is None:
            return None
        if info.audio_streams == 0:
            QMessageBox.warning(self, "音频不可用", f"该文件不含音频流：\n{path}")
            return None
        self._audio_path = path
        self._audio_info = info
        self._transport_bar.set_audio_source(path)
        self._refresh_transport_duration()
        self._mark_project_dirty()
        return info

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
            QMessageBox.critical(self, f"加载{label}失败", str(exc))
            return None
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(
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
        candidates: list[int] = []
        if self._timing_track is not None:
            candidates.append(track_duration_ms(self._timing_track))
        if self._video_info is not None and self._video_info.duration > 0:
            candidates.append(int(self._video_info.duration * 1000))
        if self._audio_info is not None and self._audio_info.duration > 0:
            candidates.append(int(self._audio_info.duration * 1000))
        duration = max(candidates, default=0)
        if duration > 0:
            self._transport_bar.set_duration(duration)

    def _apply_style(self, style: Style) -> None:
        self._style = style
        self._preview_panel.set_style(style)
        self._save_persisted_state()
        self._mark_project_dirty()

    def _apply_screen_settings(self, settings: object) -> None:
        self._screen_settings = screen_settings_from_dict(
            screen_settings_to_dict(settings)
            if isinstance(settings, ScreenSettings)
            else settings
        )
        self._set_export_screen_controls(self._screen_settings)
        self._transport_bar.set_preview_fps(self._screen_settings.fps)
        self._sync_preview_output_size()
        self._save_persisted_state()
        self._mark_project_dirty()

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
        self._property_panel.set_screen_settings(self._screen_settings)
        self._transport_bar.set_preview_fps(self._screen_settings.fps)
        self._save_persisted_state()

    def _set_export_screen_controls(self, settings: ScreenSettings) -> None:
        self._syncing_screen_controls = True
        try:
            self._export_width_spin.setValue(settings.width)
            self._export_height_spin.setValue(settings.height)
            self._set_export_fps_value(settings.fps)
        finally:
            self._syncing_screen_controls = False

    def _export_fps_value(self) -> int:
        data = self._export_fps_combo.currentData()
        return int(data) if data in SCREEN_FPS_OPTIONS else 60

    def _set_export_fps_value(self, fps: int) -> None:
        index = self._export_fps_combo.findData(fps)
        self._export_fps_combo.setCurrentIndex(index if index >= 0 else 0)

    def _on_scheme_selection_changed(self, key: str) -> None:
        self._selected_scheme_key = key
        self._save_persisted_state()
        self._mark_project_dirty()

    def _load_persisted_state(self) -> None:
        data = self._load_subtitle_settings()
        self._style = style_from_dict(data.get("style"))
        self._screen_settings = screen_settings_from_dict(data.get("screen"))
        key = data.get("selected_scheme_key")
        if isinstance(key, str) and key:
            self._selected_scheme_key = key

    def _save_persisted_state(self) -> None:
        data = self._load_subtitle_settings()
        data["style"] = style_to_dict(self._style)
        data["screen"] = screen_settings_to_dict(self._screen_settings)
        data["selected_scheme_key"] = self._selected_scheme_key
        try:
            if self._settings_provider is not None and hasattr(self._settings_provider, "save"):
                self._settings_provider.save(data)
                return
            settings = load_app_settings()
            settings.subtitle_render = data
            save_app_settings(settings)
        except Exception:
            return

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
        self._preview_panel.set_output_size(
            self._export_width_spin.value(),
            self._export_height_spin.value(),
        )

    def _default_export_path(self) -> Path:
        base = self._video_path or self._subtitle_path
        if base is None:
            return Path.cwd() / "subtitle_render.mp4"
        return base.with_name(f"{base.stem}_subtitle.mp4")

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
        if self._video_path is None:
            raise ProcessingError("请先加载背景视频。")
        output_text = self._export_output_edit.text().strip()
        if not output_text:
            raise ProcessingError("请先选择输出路径。")
        output_path = Path(output_text).expanduser()
        if output_path.suffix.lower() != ".mp4":
            output_path = output_path.with_suffix(".mp4")
            self._export_output_edit.setText(str(output_path))
        duration_ms = self._current_export_duration_ms()
        return RenderJob(
            track=self._timing_track,
            style=self._style,
            background_video_path=self._video_path,
            output_path=output_path,
            width=self._export_width_spin.value(),
            height=self._export_height_spin.value(),
            fps=self._export_fps_value(),
            duration_ms=duration_ms,
            include_audio=bool(self._video_info and self._video_info.audio_streams > 0),
            encoder_mode=str(self._export_encoder_combo.currentData() or ENCODER_CPU),
            crf=self._export_crf_spin.value(),
            preset=str(self._export_preset_combo.currentData() or "veryfast"),
        )

    def _current_export_duration_ms(self) -> int:
        candidates: list[int] = []
        if self._timing_track is not None:
            candidates.append(track_duration_ms(self._timing_track))
        if self._video_info is not None and self._video_info.duration > 0:
            candidates.append(int(round(self._video_info.duration * 1000)))
        return max(candidates, default=0)

    def _start_render_export(self) -> None:
        if self._render_thread is not None and self._render_thread.isRunning():
            QMessageBox.information(self, "导出中", "当前导出任务还在处理中，请稍等。")
            return
        try:
            job = self._build_render_job()
        except ProcessingError as exc:
            QMessageBox.critical(self, "无法导出", str(exc))
            return

        self._export_start_button.setEnabled(False)
        self._export_stop_button.setEnabled(True)
        self._export_progress.setRange(0, 0)
        self._export_status_label.setText("正在准备导出…")

        thread = QThread(self)
        worker = _RenderWorker(job, self._resolve_ffmpeg_dir())
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
        thread.start()

    def _stop_render_export(self) -> None:
        if self._render_worker is None or self._render_thread is None or not self._render_thread.isRunning():
            return
        self._export_stop_button.setEnabled(False)
        self._export_status_label.setText("正在停止导出…")
        self._render_worker.cancel()

    def _on_render_progress(self, done: int, total: int) -> None:
        self._export_progress.setRange(0, max(total, 1))
        self._export_progress.setValue(done)
        self._export_status_label.setText(f"正在导出… {done}/{total} 帧")

    def _on_render_log(self, message: str) -> None:
        self._export_status_label.setText(message)

    def _finish_render_success(self, output_path: Path) -> None:
        self._export_progress.setRange(0, 1)
        self._export_progress.setValue(1)
        self._export_status_label.setText(f"导出完成: {output_path}")
        self._export_start_button.setEnabled(True)
        self._export_stop_button.setEnabled(False)

    def _finish_render_cancelled(self, message: str) -> None:
        self._export_progress.setRange(0, 1)
        self._export_progress.setValue(0)
        self._export_status_label.setText("导出已停止，未完成文件已清理。" if message else "导出已停止。")
        self._export_start_button.setEnabled(True)
        self._export_stop_button.setEnabled(False)

    def _finish_render_failure(self, message: str) -> None:
        self._export_progress.setRange(0, 1)
        self._export_progress.setValue(0)
        self._export_status_label.setText("导出失败")
        self._export_start_button.setEnabled(True)
        self._export_stop_button.setEnabled(False)
        QMessageBox.critical(self, "导出失败", message)

    def _clear_render_thread(self) -> None:
        self._render_thread = None
        self._render_worker = None

    # ------------------------------------------------------------------ embed

    @staticmethod
    def for_embedding(
        parent: Optional[QWidget] = None,
        settings_provider: Optional[Any] = None,
        workflow_context: Optional[Any] = None,
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
        """宿主销毁本 widget 前调用的兜底（占位）。"""
        return
