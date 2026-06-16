"""字幕视频渲染主窗口（A1 阶段）。

照搬 SUG（lyrics_timing/.../frontend/main_window.py）的双模式骨架：

- ``SubtitleRenderWindow(embedded=False)`` — 默认 standalone
- ``SubtitleRenderWindow.for_embedding(parent, settings_provider, workflow_context)`` — 嵌入工作台

A1 阶段：开放"选择字幕文件"按钮，加载 Nicokara 逐字 LRC（SUG ``NicokaraExporter``
产物，``.lrc``），展示加载摘要（行数 / 字数 / 注音数 / 元数据）。后续 P0 任务会扩出
四分区 + 顶/底栏完整布局。
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

from krok_helper.subtitle_render.models import TimingTrack
from krok_helper.subtitle_render.subtitle_sources import load_nicokara_lrc

SUBTITLE_FILTER = "Nicokara 逐字 LRC (*.lrc);;所有文件 (*.*)"


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

        self._init_layout()

    def _init_layout(self) -> None:
        shell = QVBoxLayout(self)
        shell.setContentsMargins(20, 20, 20, 20)
        shell.setSpacing(18)

        title = QLabel("字幕视频生成")
        title.setStyleSheet("font-size: 22pt; font-weight: 700;")
        shell.addWidget(title)

        intro = QLabel(
            "选择 SUG 导出的 Nicokara 逐字 LRC 文件作为字幕源。"
            "后续步骤（背景视频 / 音轨 / 样式 / 渲染输出）将依次开放。"
        )
        intro.setWordWrap(True)
        intro.setStyleSheet("font-size: 10pt; color: #666;")
        shell.addWidget(intro)

        action_row = QHBoxLayout()
        action_row.setSpacing(10)
        self._load_button = QPushButton("选择字幕文件…")
        self._load_button.setMinimumHeight(36)
        self._load_button.clicked.connect(self._on_load_subtitle_clicked)
        action_row.addWidget(self._load_button)
        action_row.addStretch(1)
        shell.addLayout(action_row)

        self._summary_label = QLabel("尚未加载字幕。")
        self._summary_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self._summary_label.setWordWrap(True)
        self._summary_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self._summary_label.setStyleSheet("font-size: 11pt;")
        shell.addWidget(self._summary_label, 1)

    # ------------------------------------------------------------------ events

    def _on_load_subtitle_clicked(self) -> None:
        start_dir = ""
        if self._subtitle_path is not None:
            start_dir = str(self._subtitle_path.parent)
        path_str, _ = QFileDialog.getOpenFileName(
            self,
            "选择 Nicokara 逐字 LRC 文件",
            start_dir,
            SUBTITLE_FILTER,
        )
        if not path_str:
            return
        self.load_from_lrc(Path(path_str))

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
        self._summary_label.setText(self._format_summary(path, track))
        return track

    @property
    def timing_track(self) -> Optional[TimingTrack]:
        return self._timing_track

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _format_summary(path: Path, track: TimingTrack) -> str:
        meta = track.meta
        meta_lines = []
        if meta.title:
            meta_lines.append(f"  • 曲名：{meta.title}")
        if meta.artist:
            meta_lines.append(f"  • 艺术家：{meta.artist}")
        if meta.album:
            meta_lines.append(f"  • 专辑：{meta.album}")
        if meta.tagging_by:
            meta_lines.append(f"  • 打轴：{meta.tagging_by}")
        if meta.offset_ms:
            meta_lines.append(f"  • 全局偏移：{meta.offset_ms} ms")
        if meta.silence_ms:
            meta_lines.append(f"  • 曲首静音：{meta.silence_ms} ms")
        meta_block = "\n".join(meta_lines) if meta_lines else "  （无元数据）"

        return (
            f"已加载：{path}\n"
            f"\n"
            f"  • 行数（含空行）：{len(track.lines)}\n"
            f"  • 非空行数：{track.non_blank_line_count}\n"
            f"  • 字符数：{track.char_count}\n"
            f"  • 注音条数：{len(track.rubies)}\n"
            f"\n元数据：\n{meta_block}"
        )

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
