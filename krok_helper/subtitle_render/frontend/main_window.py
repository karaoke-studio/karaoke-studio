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
from typing import Any, Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QFileDialog,
    QLabel,
    QMessageBox,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import Pivot

from krok_helper.errors import ProcessingError
from krok_helper.ffmpeg import find_tool, probe_media
from krok_helper.models import MediaInfo
from krok_helper.settings import load_app_settings
from krok_helper.subtitle_render.frontend.lyrics_list import LyricsPanel
from krok_helper.subtitle_render.frontend.preview_view import PreviewPanel, TransportBar
from krok_helper.subtitle_render.frontend.property_panel import PropertyPanel
from krok_helper.subtitle_render.frontend.timeline_view import (
    TrackTimelineView,
    WaveformPanel,
)
from krok_helper.subtitle_render.models import TimingTrack
from krok_helper.subtitle_render.subtitle_sources import load_nicokara_lrc
from krok_helper.theme_workbench import palette, themed

SUBTITLE_FILTER = "Nicokara 逐字 LRC (*.lrc);;所有文件 (*.*)"
VIDEO_FILTER = "视频文件 (*.mp4 *.mkv *.mov *.webm *.avi *.flv);;所有文件 (*.*)"
AUDIO_FILTER = (
    "音频 / 视频文件 (*.wav *.flac *.mp3 *.m4a *.aac *.ogg *.opus *.mp4 *.mkv *.mov);;"
    "所有文件 (*.*)"
)


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

        themed(
            self,
            lambda: f"SubtitleRenderWindow {{ background: {palette().shell_bg}; }}",
        )

        self._init_layout()

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
        self._preview_panel.pathDropped.connect(self.load_video)
        self._preview_panel.browseRequested.connect(self._browse_video)
        center_layout.addWidget(self._preview_panel, 1)
        self._transport_bar = TransportBar()
        center_layout.addWidget(self._transport_bar)
        top.addWidget(center)

        self._property_panel = PropertyPanel()
        top.addWidget(self._property_panel)

        top.setStretchFactor(0, 1)
        top.setStretchFactor(1, 3)
        top.setStretchFactor(2, 1)
        top.setSizes([280, 760, 320])
        body.addWidget(top)

        # 底部：波形 + 字幕轨道
        self._waveform_panel = WaveformPanel()
        self._waveform_panel.pathDropped.connect(self.load_audio)
        self._waveform_panel.browseRequested.connect(self._browse_audio)
        body.addWidget(self._waveform_panel)

        self._tracks_view = TrackTimelineView()
        body.addWidget(self._tracks_view)

        body.setStretchFactor(0, 6)
        body.setStretchFactor(1, 1)
        body.setStretchFactor(2, 2)
        body.setSizes([520, 80, 140])

        outer.addWidget(body, 1)
        return page

    def _make_export_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(48, 48, 48, 48)
        layout.addStretch(1)
        title = QLabel("导出尚未实装")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        themed(
            title,
            lambda: f"color: {palette().title_text}; font-size: 18pt; font-weight: 700;",
        )
        layout.addWidget(title)
        hint = QLabel(
            "A8 / A9 阶段开放：分辨率 / fps / 码率 / 硬编 / 输出路径 / "
            "开始渲染 / 取消"
        )
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint.setWordWrap(True)
        themed(hint, lambda: f"color: {palette().text_hint}; font-size: 10.5pt;")
        layout.addWidget(hint)
        layout.addStretch(2)
        return page

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

    def _browse_audio(self) -> None:
        start_dir = str(self._audio_path.parent) if self._audio_path else ""
        path_str, _ = QFileDialog.getOpenFileName(
            self, "选择音频", start_dir, AUDIO_FILTER
        )
        if path_str:
            self.load_audio(Path(path_str))

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
        return track

    def load_video(self, path: Path) -> Optional[MediaInfo]:
        """加载背景视频，调用 ffprobe 读取分辨率 / 帧率 / 时长。"""
        info = self._probe(path, "视频")
        if info is None:
            return None
        if info.video_streams == 0:
            QMessageBox.warning(self, "背景视频不可用", f"该文件不含视频流：\n{path}")
            return None
        self._video_path = path
        self._video_info = info
        self._preview_panel.set_populated(True)
        return info

    def load_audio(self, path: Path) -> Optional[MediaInfo]:
        """加载音轨，调用 ffprobe 读取时长 / 采样率。"""
        info = self._probe(path, "音频")
        if info is None:
            return None
        if info.audio_streams == 0:
            QMessageBox.warning(self, "音频不可用", f"该文件不含音频流：\n{path}")
            return None
        self._audio_path = path
        self._audio_info = info
        self._waveform_panel.set_populated(True)
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
