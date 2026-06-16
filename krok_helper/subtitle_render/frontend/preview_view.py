"""中央预览区（拖拽视频 + 占位）。

UI 设计：

- **空态**：居中显示"拖入背景视频 / 点击此处选择"
- **载入后**：嵌入预览画布（A4 之前先放一个深色占位 widget；A4 时换成
  ``QGraphicsView`` + 字幕叠加 / ``QMediaPlayer.setVideoSink``）
- 下方独立 :class:`TransportBar` 放播放控件占位
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from krok_helper.subtitle_render.frontend.drop_panel import DropPanel
from krok_helper.theme_workbench import palette, themed


class PreviewPanel(DropPanel):
    """预览面板。空态拖拽 / 载入后显示视频画面。"""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(
            extensions={".mp4", ".mkv", ".mov", ".webm", ".avi", ".flv"},
            empty_title="拖入背景视频",
            empty_hint="支持 .mp4 / .mkv / .mov / .webm 等\n或点击此处选择",
            empty_icon="🎬",
            parent=parent,
        )
        # 载入后的预览画布占位（A4 实装时替换为 QGraphicsView + 字幕叠加）
        canvas = QWidget()
        canvas.setObjectName("PreviewCanvas")
        canvas.setMinimumHeight(240)
        themed(
            canvas,
            lambda: (
                f"#PreviewCanvas {{ background: {palette().preview_bg}; "
                f"border: 1px solid {palette().preview_border}; "
                f"border-radius: 6px; }}"
            ),
        )
        canvas_layout = QVBoxLayout(canvas)
        canvas_layout.setContentsMargins(0, 0, 0, 0)
        hint = QLabel("预览画面（A4 实装后显示带卡拉ok高亮的字幕叠加）")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        themed(hint, lambda: f"color: {palette().text_hint}; font-size: 10pt;")
        canvas_layout.addWidget(hint)
        self.set_content(canvas)


class TransportBar(QWidget):
    """播放控件 + 时间码（占位，A4 / A7 接 QMediaPlayer 后实装）。"""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("TransportBar")
        self.setFixedHeight(44)
        themed(
            self,
            lambda: (
                f"#TransportBar {{ background: transparent; "
                f"border-top: 1px solid {palette().card_border}; }}"
            ),
        )
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 6, 12, 6)
        layout.setSpacing(8)

        hint = QLabel("⏵   ⏺   ⏵︎│      ────────────────────")
        themed(hint, lambda: f"color: {palette().text_hint}; font-family: monospace;")
        layout.addWidget(hint)
        layout.addStretch(1)

        speed = QLabel("1.0×")
        themed(speed, lambda: f"color: {palette().text_secondary};")
        layout.addWidget(speed)

        timecode = QLabel("00:00.00")
        themed(timecode, lambda: f"color: {palette().text_primary}; font-family: monospace;")
        layout.addWidget(timecode)
