"""字幕视频渲染主窗口（Sayatoo 风格 + Pivot tabs + 拖拽加载）。

照搬 SUG（lyrics_timing/.../frontend/main_window.py）的双模式骨架：

- ``SubtitleRenderWindow(embedded=False)`` — 默认 standalone
- ``SubtitleRenderWindow.for_embedding(parent, settings_provider, workflow_context)``
  — 嵌入工作台

UI 顶层结构：

  ┌─ Pivot：预览 / 导出 ─────────────────────────────┐
  ├─ QStackedWidget ────────────────────────────────┤
  │                                                  │
  │  ◆ 预览 Tab（当前唯一可用）                       │
  │    ┌─────────┬──────────────┬──────────────┐    │
  │    │ 左·歌词 │ 中·预览       │ 右·属性 tab │    │
  │    │ (拖.lrc)│ + transport   │              │    │
  │    ├─────────┴──────────────┴──────────────┤    │
  │    │ 底·波形（拖音频）                       │    │
  │    │ 底·字幕轨道                              │    │
  │    └─────────────────────────────────────────┘   │
  │                                                  │
  │  ◆ 导出 Tab（A8 实装后开放）                      │
  │                                                  │
  └──────────────────────────────────────────────────┘

三个素材区均接受拖拽 + 点击浏览（详见 :mod:`drop_panel`）。
"""

from __future__ import annotations

from pathlib import Path
import subprocess
from typing import Any, Optional

from PyQt6.QtCore import QObject, QThread, Qt, pyqtSignal as Signal
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
    QSplitter,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import Pivot

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
from krok_helper.subtitle_render.frontend.property_panel import PropertyPanel
from krok_helper.subtitle_render.frontend.timeline_view import (
    TrackTimelineView,
    WaveformPanel,
)
from krok_helper.subtitle_render.models import Style, TimingTrack, style_from_dict, style_to_dict
from krok_helper.subtitle_render.subtitle_sources import load_nicokara_lrc
from krok_helper.subtitle_render.frontend.theme import palette, themed

SUBTITLE_FILTER = "Nicokara 逐字 LRC (*.lrc);;所有文件 (*.*)"
VIDEO_FILTER = "视频文件 (*.mp4 *.mkv *.mov *.webm *.avi *.flv);;所有文件 (*.*)"
OUTPUT_FILTER = "MP4 视频 (*.mp4);;所有文件 (*.*)"


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
        self._selected_scheme_key = "global"
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
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 12, 16, 16)
        root.setSpacing(8)

        # 顶部：Pivot 大 tab（预览 / 导出）
        self._pivot = Pivot(self)
        self._pivot.setFixedHeight(40)
        root.addWidget(self._pivot)

        # 中间：QStackedWidget 承载 tab 内容
        self._stack = QStackedWidget(self)
        root.addWidget(self._stack, 1)

        self._preview_tab = self._make_preview_tab()
        self._export_tab = self._make_export_tab()
        self._stack.addWidget(self._preview_tab)
        self._stack.addWidget(self._export_tab)
        self._sync_preview_output_size()
        self._export_width_spin.valueChanged.connect(self._sync_preview_output_size)
        self._export_height_spin.valueChanged.connect(self._sync_preview_output_size)

        self._pivot.addItem(
            routeKey="preview",
            text="预览",
            onClick=lambda _checked=False: self._stack.setCurrentIndex(0),
        )
        self._pivot.addItem(
            routeKey="export",
            text="导出",
            onClick=lambda _checked=False: self._stack.setCurrentIndex(1),
        )
        self._pivot.setCurrentItem("preview")
        self._stack.setCurrentIndex(0)

    def _make_preview_tab(self) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 8, 0, 0)
        outer.setSpacing(8)

        body = QSplitter(Qt.Orientation.Vertical)
        body.setChildrenCollapsible(False)

        # 上半部：左·歌词 ┃ 中·预览 ┃ 右·属性
        top = QSplitter(Qt.Orientation.Horizontal)
        top.setChildrenCollapsible(False)

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
        center_layout.addWidget(self._preview_panel, 1)
        self._transport_bar = TransportBar()
        self._transport_bar.timeChanged.connect(self._preview_panel.set_time)
        self._transport_bar.playbackStateChanged.connect(self._preview_panel.set_playing)
        center_layout.addWidget(self._transport_bar)
        top.addWidget(center)

        self._property_panel = PropertyPanel()
        self._property_panel.set_style(self._style)
        self._property_panel.styleChanged.connect(self._apply_style)
        self._property_panel.schemeSelectionChanged.connect(self._on_scheme_selection_changed)
        self._property_panel.set_current_scheme_key(self._selected_scheme_key)
        top.addWidget(self._property_panel)

        top.setStretchFactor(0, 1)
        top.setStretchFactor(1, 3)
        top.setStretchFactor(2, 1)
        top.setSizes([280, 760, 320])
        body.addWidget(top)

        # 底部：波形 + 字幕轨道（波形被动展示，不收拖拽——音频从视频自动取）
        self._waveform_panel = WaveformPanel()
        body.addWidget(self._waveform_panel)

        self._tracks_view = TrackTimelineView()
        body.addWidget(self._tracks_view)

        body.setStretchFactor(0, 6)
        body.setStretchFactor(1, 1)
        body.setStretchFactor(2, 2)
        body.setSizes([520, 80, 140])

        outer.addWidget(body, 1)
        return page

    def _init_shortcuts(self) -> None:
        # 空格键播放 / 暂停（窗口范围内有效，避免误伤未来的文本输入）
        self._space_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Space), self)
        self._space_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._space_shortcut.activated.connect(self._transport_bar.toggle_play)

    def _make_export_tab(self) -> QWidget:
        page = QWidget()
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
        self._export_fps_spin = self._export_spin(1, 120, 60, " fps")
        params_row.addWidget(self._labeled_export_control("宽度", self._export_width_spin))
        params_row.addWidget(self._labeled_export_control("高度", self._export_height_spin))
        params_row.addWidget(self._labeled_export_control("帧率", self._export_fps_spin))
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

    def _on_scheme_selection_changed(self, key: str) -> None:
        self._selected_scheme_key = key
        self._save_persisted_state()

    def _load_persisted_state(self) -> None:
        data = self._load_subtitle_settings()
        self._style = style_from_dict(data.get("style"))
        key = data.get("selected_scheme_key")
        if isinstance(key, str) and key:
            self._selected_scheme_key = key

    def _save_persisted_state(self) -> None:
        data = self._load_subtitle_settings()
        data["style"] = style_to_dict(self._style)
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
            fps=self._export_fps_spin.value(),
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
