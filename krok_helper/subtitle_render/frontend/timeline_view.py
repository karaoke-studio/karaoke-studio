"""底部波形 + 字幕轨道（占位）。

- :class:`WaveformView` — 全宽波形显示，含 playhead。最终实装会复用波形对齐
  模块（``audio_alignment.py::extract_waveform``）的数据，传给自定义场景渲染。
- :class:`TrackTimelineView` — 多行字幕轨道（T1 / T2 / T3...），按字符级色块
  渲染歌词区间；与预览 playhead 同步。

当前 widget 只显示占位提示；A4 之后会接入真实数据。
"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QLabel, QVBoxLayout, QWidget


class WaveformView(QWidget):
    """全宽波形占位。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(72)
        self.setStyleSheet(
            "WaveformView { background-color: #161616; border-top: 1px solid #333; }"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        hint = QLabel("波形区（接入音频后显示峰值波形 + playhead）")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint.setStyleSheet("color: #555; font-size: 10pt;")
        layout.addWidget(hint)


class TrackTimelineView(QWidget):
    """字幕轨道（多行 T1/T2/T3）占位。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(120)
        self.setStyleSheet(
            "TrackTimelineView { background-color: #1f1f1f; border-top: 1px solid #333; }"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        hint = QLabel("字幕轨道 T1 / T2 / T3（A4 后按字符级色块展示）")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint.setStyleSheet("color: #555; font-size: 10pt;")
        layout.addWidget(hint)
