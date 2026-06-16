"""中央预览区（占位）。

最终实装：``QGraphicsView`` + 自定义场景，背景层 ``QMediaPlayer.setVideoSink`` /
``QGraphicsPixmapItem``（纯色 / 静态图），字幕层 ``QGraphicsPixmapItem``（QPainter
离屏帧）。预览与渲染共享同一 ``paint_frame``。

A4 阶段才把真实字符高亮渲染进来；当前 widget 只画一个空白占位提示。
"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QPainter
from PyQt6.QtWidgets import QFrame, QLabel, QVBoxLayout, QWidget


class PreviewView(QWidget):
    """字幕视频预览区占位。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumSize(480, 270)
        self.setStyleSheet(
            "PreviewView { background-color: #1a1a1a; border: 1px solid #333; }"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        hint = QLabel("预览区（A4 实装后显示带卡拉ok高亮的字幕画面）")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint.setStyleSheet("color: #888; font-size: 11pt;")
        layout.addWidget(hint)


class TransportBar(QWidget):
    """播放控件占位（A4 接 QMediaPlayer 后实装）。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(36)
        self.setStyleSheet(
            "TransportBar { background-color: #2a2a2a; border-top: 1px solid #333; }"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        hint = QLabel("⏵ ⏺ ⏵︎| ────────────────────── 1.0x  00:00.00")
        hint.setStyleSheet("color: #aaa; font-family: monospace;")
        layout.addWidget(hint)
