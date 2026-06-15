"""字幕视频渲染主窗口（骨架）。

照搬 SUG（lyrics_timing/.../frontend/main_window.py）的双模式骨架：

- ``SubtitleRenderWindow(embedded=False)`` — 默认 standalone
- ``SubtitleRenderWindow.for_embedding(parent, settings_provider, workflow_context)`` — 嵌入工作台

骨架阶段 UI 仅含"该模块仍在开发"提示，便于先把工作流接线跑通；后续 P0 任务
（A1-A11）会替换为完整四分区 + 顶/底栏布局。
"""

from __future__ import annotations

from typing import Any, Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QLabel, QVBoxLayout, QWidget


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
        workflow_context: 嵌入模式下宿主注入的工作流上下文（含 ``subtitle_project_obj`` /
            ``subtitle_ass_path`` / ``aligned_video_path`` / ``alignment_offset_ms`` 等）
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

        self._init_layout()

    def _init_layout(self) -> None:
        shell = QVBoxLayout(self)
        shell.setContentsMargins(20, 20, 20, 20)
        shell.setSpacing(18)

        title = QLabel("字幕视频生成")
        title.setStyleSheet("font-size: 22pt; font-weight: 700;")
        shell.addWidget(title)

        hint = QLabel(
            "本模块正在开发中。\n"
            "后续将按计划实装 NicoKaraMaker3 等效功能：\n"
            "  • 加载字幕（.ass / .sug / .nkm）与背景视频 / 音轨\n"
            "  • 卡拉ok逐字高亮、字体 / 颜色配置、实时预览\n"
            "  • 输出 MP4（H.264 + AAC，硬编 / 软编可选）"
        )
        hint.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        hint.setWordWrap(True)
        hint.setStyleSheet("font-size: 11pt;")
        shell.addWidget(hint, 1)

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
