"""字幕视频渲染主窗口（Sayatoo 风格四区布局）。

照搬 SUG（lyrics_timing/.../frontend/main_window.py）的双模式骨架：

- ``SubtitleRenderWindow(embedded=False)`` — 默认 standalone
- ``SubtitleRenderWindow.for_embedding(parent, settings_provider, workflow_context)`` — 嵌入工作台

布局参考 Sayatoo（详见设计文档 §C）：

  顶部工具栏（选字幕 / 选视频 / 选音频 + 状态）
  ├─ 左·歌词列表 ┃ 中·预览 + transport ┃ 右·属性 tab
  └─ 底·波形 + 字幕轨道（全宽）

当前阶段：
- A1 字幕加载已接通，填充左侧歌词列表
- A2 / A3 视频 / 音频加载已接通，顶栏状态文字反馈
- 预览 / 属性 / 波形 / 轨道全是占位 widget，A4 之后陆续填充
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from krok_helper.errors import ProcessingError
from krok_helper.ffmpeg import find_tool, probe_media
from krok_helper.models import MediaInfo
from krok_helper.settings import load_app_settings
from krok_helper.subtitle_render.frontend.lyrics_list import LyricsListWidget
from krok_helper.subtitle_render.frontend.preview_view import PreviewView, TransportBar
from krok_helper.subtitle_render.frontend.property_panel import PropertyPanel
from krok_helper.subtitle_render.frontend.timeline_view import TrackTimelineView, WaveformView
from krok_helper.subtitle_render.models import TimingTrack
from krok_helper.subtitle_render.subtitle_sources import load_nicokara_lrc

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

        self._init_layout()

    # ------------------------------------------------------------------ layout

    def _init_layout(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._make_toolbar())
        root.addWidget(self._make_divider())
        root.addWidget(self._make_body_splitter(), 1)

    def _make_toolbar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("SubtitleRenderToolbar")
        bar.setStyleSheet(
            "#SubtitleRenderToolbar { background-color: palette(window); }"
        )
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(8)

        self._load_subtitle_button = QPushButton("选择字幕文件…")
        self._load_subtitle_button.clicked.connect(self._on_load_subtitle_clicked)
        layout.addWidget(self._load_subtitle_button)

        self._load_video_button = QPushButton("选择背景视频…")
        self._load_video_button.clicked.connect(self._on_load_video_clicked)
        layout.addWidget(self._load_video_button)

        self._load_audio_button = QPushButton("选择音频…")
        self._load_audio_button.clicked.connect(self._on_load_audio_clicked)
        layout.addWidget(self._load_audio_button)

        layout.addSpacing(12)

        self._status_label = QLabel("尚未加载素材")
        self._status_label.setStyleSheet("color: #888;")
        self._status_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout.addWidget(self._status_label, 1)

        return bar

    @staticmethod
    def _make_divider() -> QFrame:
        d = QFrame()
        d.setFrameShape(QFrame.Shape.HLine)
        d.setFrameShadow(QFrame.Shadow.Sunken)
        d.setStyleSheet("color: #333;")
        return d

    def _make_body_splitter(self) -> QSplitter:
        body = QSplitter(Qt.Orientation.Vertical)

        # 上半部：左·歌词 ┃ 中·预览 ┃ 右·属性
        top = QSplitter(Qt.Orientation.Horizontal)
        self._lyrics_list = LyricsListWidget()
        top.addWidget(self._lyrics_list)

        center = QWidget()
        center_layout = QVBoxLayout(center)
        center_layout.setContentsMargins(0, 0, 0, 0)
        center_layout.setSpacing(0)
        self._preview_view = PreviewView()
        center_layout.addWidget(self._preview_view, 1)
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

        # 下半部：波形 + 字幕轨道
        self._waveform_view = WaveformView()
        body.addWidget(self._waveform_view)

        self._tracks_view = TrackTimelineView()
        body.addWidget(self._tracks_view)

        body.setStretchFactor(0, 6)
        body.setStretchFactor(1, 1)
        body.setStretchFactor(2, 2)
        body.setSizes([520, 80, 140])

        return body

    # ------------------------------------------------------------------ events

    def _on_load_subtitle_clicked(self) -> None:
        start_dir = str(self._subtitle_path.parent) if self._subtitle_path else ""
        path_str, _ = QFileDialog.getOpenFileName(
            self, "选择 Nicokara 逐字 LRC 文件", start_dir, SUBTITLE_FILTER
        )
        if path_str:
            self.load_from_lrc(Path(path_str))

    def _on_load_video_clicked(self) -> None:
        start_dir = str(self._video_path.parent) if self._video_path else ""
        path_str, _ = QFileDialog.getOpenFileName(
            self, "选择背景视频", start_dir, VIDEO_FILTER
        )
        if path_str:
            self.load_video(Path(path_str))

    def _on_load_audio_clicked(self) -> None:
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
        self._lyrics_list.set_track(track)
        self._refresh_status()
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
        self._refresh_status()
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
        self._refresh_status()
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

    def _refresh_status(self) -> None:
        bits: list[str] = []
        if self._timing_track is not None and self._subtitle_path is not None:
            t = self._timing_track
            bits.append(
                f"字幕：{self._subtitle_path.name}"
                f"（{t.non_blank_line_count} 行 / {t.char_count} 字 / {len(t.rubies)} 注音）"
            )
        if self._video_info is not None and self._video_path is not None:
            v = self._video_info
            dim = f"{v.video_width}×{v.video_height}" if v.video_width else "?"
            fps = f"{v.video_fps:.2f}fps" if v.video_fps else "?fps"
            bits.append(f"视频：{self._video_path.name}（{dim} / {fps}）")
        if self._audio_info is not None and self._audio_path is not None:
            a = self._audio_info
            sr = f"{a.sample_rate}Hz" if a.sample_rate else "?Hz"
            bits.append(f"音频：{self._audio_path.name}（{sr}）")
        self._status_label.setText("    │    ".join(bits) if bits else "尚未加载素材")

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
