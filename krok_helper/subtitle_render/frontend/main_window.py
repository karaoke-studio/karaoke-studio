"""字幕视频渲染主窗口（A1-A3 阶段）。

照搬 SUG（lyrics_timing/.../frontend/main_window.py）的双模式骨架：

- ``SubtitleRenderWindow(embedded=False)`` — 默认 standalone
- ``SubtitleRenderWindow.for_embedding(parent, settings_provider, workflow_context)`` — 嵌入工作台

当前阶段开放的素材入口：

- **A1** 字幕源：Nicokara 逐字 LRC（``.lrc``，SUG ``NicokaraExporter`` 产物）
- **A2** 背景视频：任意 ffprobe 支持的视频容器
- **A3** 音轨：任意 ffprobe 支持的音频容器（含视频文件本身）

后续 P0 会扩出四分区 + 顶/底栏完整布局；当前 UI 只展示加载摘要，便于早期回归。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from krok_helper.errors import ProcessingError
from krok_helper.ffmpeg import find_tool, probe_media
from krok_helper.models import MediaInfo
from krok_helper.settings import load_app_settings
from krok_helper.subtitle_render.models import TimingTrack
from krok_helper.subtitle_render.subtitle_sources import load_nicokara_lrc

SUBTITLE_FILTER = "Nicokara 逐字 LRC (*.lrc);;所有文件 (*.*)"
VIDEO_FILTER = "视频文件 (*.mp4 *.mkv *.mov *.webm *.avi *.flv);;所有文件 (*.*)"
AUDIO_FILTER = (
    "音频 / 视频文件 (*.wav *.flac *.mp3 *.m4a *.aac *.ogg *.opus *.mp4 *.mkv *.mov);;"
    "所有文件 (*.*)"
)


class SubtitleRenderWindow(QWidget):
    """字幕视频渲染模块主 widget。

    支持两种模式：

    - **standalone**（默认 ``embedded=False``）：作为顶层窗口跑，可自管菜单栏 /
      文件 IO / 窗口几何
    - **embedded**（``embedded=True``）：作为工作台第 5 步嵌入页面，由宿主
      统一管理装饰；用 :meth:`for_embedding` 工厂方法创建

    构造参数:
        embedded: 是否嵌入模式
        settings_provider: 嵌入模式下宿主注入的设置桥
            （:class:`KrokHelperSubtitleRenderSettingsBridge`）
        workflow_context: 嵌入模式下宿主注入的工作流上下文（含 ``subtitle_lrc_path`` /
            ``aligned_video_path`` / ``alignment_offset_ms`` 等）
    """

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

    def _init_layout(self) -> None:
        shell = QVBoxLayout(self)
        shell.setContentsMargins(20, 20, 20, 20)
        shell.setSpacing(18)

        title = QLabel("字幕视频生成")
        title.setStyleSheet("font-size: 22pt; font-weight: 700;")
        shell.addWidget(title)

        intro = QLabel(
            "依次选择字幕（Nicokara 逐字 LRC）、背景视频、音轨；后续步骤"
            "（样式 / 实时预览 / 渲染输出）将依次开放。"
        )
        intro.setWordWrap(True)
        intro.setStyleSheet("font-size: 10pt; color: #666;")
        shell.addWidget(intro)

        action_row = QHBoxLayout()
        action_row.setSpacing(10)
        self._load_subtitle_button = QPushButton("选择字幕文件…")
        self._load_subtitle_button.setMinimumHeight(36)
        self._load_subtitle_button.clicked.connect(self._on_load_subtitle_clicked)
        action_row.addWidget(self._load_subtitle_button)

        self._load_video_button = QPushButton("选择背景视频…")
        self._load_video_button.setMinimumHeight(36)
        self._load_video_button.clicked.connect(self._on_load_video_clicked)
        action_row.addWidget(self._load_video_button)

        self._load_audio_button = QPushButton("选择音频…")
        self._load_audio_button.setMinimumHeight(36)
        self._load_audio_button.clicked.connect(self._on_load_audio_clicked)
        action_row.addWidget(self._load_audio_button)

        action_row.addStretch(1)
        shell.addLayout(action_row)

        self._summary_label = QLabel(self._format_summary())
        self._summary_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self._summary_label.setWordWrap(True)
        self._summary_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self._summary_label.setStyleSheet("font-size: 11pt;")
        shell.addWidget(self._summary_label, 1)

    # ------------------------------------------------------------------ events

    def _on_load_subtitle_clicked(self) -> None:
        start_dir = str(self._subtitle_path.parent) if self._subtitle_path else ""
        path_str, _ = QFileDialog.getOpenFileName(
            self,
            "选择 Nicokara 逐字 LRC 文件",
            start_dir,
            SUBTITLE_FILTER,
        )
        if not path_str:
            return
        self.load_from_lrc(Path(path_str))

    def _on_load_video_clicked(self) -> None:
        start_dir = str(self._video_path.parent) if self._video_path else ""
        path_str, _ = QFileDialog.getOpenFileName(
            self,
            "选择背景视频",
            start_dir,
            VIDEO_FILTER,
        )
        if not path_str:
            return
        self.load_video(Path(path_str))

    def _on_load_audio_clicked(self) -> None:
        start_dir = str(self._audio_path.parent) if self._audio_path else ""
        path_str, _ = QFileDialog.getOpenFileName(
            self,
            "选择音频",
            start_dir,
            AUDIO_FILTER,
        )
        if not path_str:
            return
        self.load_audio(Path(path_str))

    # ------------------------------------------------------------------ public

    def load_from_lrc(self, path: Path) -> Optional[TimingTrack]:
        """加载 Nicokara 逐字 LRC 文件。返回解析结果（失败返回 None 并弹错）。"""
        try:
            track = load_nicokara_lrc(path)
        except Exception as exc:  # noqa: BLE001 — 暴露给用户的统一错误处理
            QMessageBox.critical(
                self,
                "加载字幕失败",
                f"无法解析字幕文件：\n{path}\n\n错误：{exc}",
            )
            return None
        self._timing_track = track
        self._subtitle_path = path
        self._refresh_summary()
        return track

    def load_video(self, path: Path) -> Optional[MediaInfo]:
        """加载背景视频，调用 ffprobe 读取分辨率 / 帧率 / 时长。"""
        info = self._probe(path, "视频")
        if info is None:
            return None
        if info.video_streams == 0:
            QMessageBox.warning(
                self,
                "背景视频不可用",
                f"该文件不含视频流：\n{path}",
            )
            return None
        self._video_path = path
        self._video_info = info
        self._refresh_summary()
        return info

    def load_audio(self, path: Path) -> Optional[MediaInfo]:
        """加载音轨，调用 ffprobe 读取时长 / 采样率。"""
        info = self._probe(path, "音频")
        if info is None:
            return None
        if info.audio_streams == 0:
            QMessageBox.warning(
                self,
                "音频不可用",
                f"该文件不含音频流：\n{path}",
            )
            return None
        self._audio_path = path
        self._audio_info = info
        self._refresh_summary()
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
        # 优先用工作台设置里的 ffmpeg_dir；找不到再回退 PATH。
        ffmpeg_dir: Optional[Path] = None
        try:
            settings = load_app_settings()
            raw = (settings.ffmpeg_dir or "").strip()
            if raw:
                ffmpeg_dir = Path(raw)
        except Exception:
            ffmpeg_dir = None
        return find_tool("ffprobe", ffmpeg_dir)

    def _refresh_summary(self) -> None:
        self._summary_label.setText(self._format_summary())

    def _format_summary(self) -> str:
        parts: list[str] = []

        # 字幕
        if self._timing_track is not None and self._subtitle_path is not None:
            track = self._timing_track
            meta = track.meta
            meta_bits: list[str] = []
            if meta.title:
                meta_bits.append(f"曲名 {meta.title}")
            if meta.artist:
                meta_bits.append(f"艺术家 {meta.artist}")
            if meta.offset_ms:
                meta_bits.append(f"全局偏移 {meta.offset_ms} ms")
            meta_str = "（" + " / ".join(meta_bits) + "）" if meta_bits else ""
            parts.append(
                "【字幕】" + str(self._subtitle_path) + "\n"
                f"  • 非空行数 {track.non_blank_line_count} / "
                f"字符数 {track.char_count} / "
                f"注音条数 {len(track.rubies)} {meta_str}"
            )
        else:
            parts.append("【字幕】尚未加载")

        # 视频
        if self._video_info is not None and self._video_path is not None:
            v = self._video_info
            dim = (
                f"{v.video_width}×{v.video_height}"
                if v.video_width and v.video_height
                else "分辨率未知"
            )
            fps = f"{v.video_fps:.3f} fps" if v.video_fps else "帧率未知"
            parts.append(
                "【背景视频】" + str(self._video_path) + "\n"
                f"  • {dim} / {fps} / 时长 {_format_duration(v.duration)}"
            )
        else:
            parts.append("【背景视频】尚未加载")

        # 音频
        if self._audio_info is not None and self._audio_path is not None:
            a = self._audio_info
            sr = f"{a.sample_rate} Hz" if a.sample_rate else "采样率未知"
            ch = f"{a.channels} 声道" if a.channels else "声道数未知"
            parts.append(
                "【音频】" + str(self._audio_path) + "\n"
                f"  • {sr} / {ch} / 时长 {_format_duration(a.duration)}"
            )
        else:
            parts.append("【音频】尚未加载")

        return "\n\n".join(parts)

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


def _format_duration(seconds: float) -> str:
    if seconds is None or seconds <= 0:
        return "未知"
    total = int(round(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"
