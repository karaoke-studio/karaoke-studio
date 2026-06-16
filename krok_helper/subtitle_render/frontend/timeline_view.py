"""底部波形 + 字幕轨道。

- :class:`WaveformPanel` — 接受拖拽音频；A4 / A7 接入波形渲染
- :class:`TrackTimelineView` — 字幕轨道占位（数据从已加载 ``TimingTrack`` 来，不
  做拖拽）

当前 widget 只显示占位提示；A4 之后会接入真实数据。
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QLabel, QVBoxLayout, QWidget

from krok_helper.subtitle_render.frontend.drop_panel import DropPanel
from krok_helper.subtitle_render.frontend.theme import palette, themed


class WaveformPanel(DropPanel):
    """底部波形面板（空态拖拽 / 载入后显示波形）。"""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(
            extensions={
                ".wav", ".flac", ".mp3", ".m4a", ".aac",
                ".ogg", ".opus",
                # 含音频流的视频也接受
                ".mp4", ".mkv", ".mov",
            },
            empty_title="拖入音频文件",
            empty_hint="支持 .wav / .flac / .mp3 等\n或点击此处选择",
            empty_icon="🎵",
            parent=parent,
        )
        canvas = QWidget()
        canvas.setObjectName("WaveformCanvas")
        canvas.setMinimumHeight(72)
        themed(
            canvas,
            lambda: (
                f"#WaveformCanvas {{ background: {palette().preview_bg}; "
                f"border-radius: 4px; }}"
            ),
        )
        canvas_layout = QVBoxLayout(canvas)
        canvas_layout.setContentsMargins(0, 0, 0, 0)
        hint = QLabel("波形（A4 / A7 接入后显示峰值 + playhead）")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        themed(hint, lambda: f"color: {palette().text_hint}; font-size: 9pt;")
        canvas_layout.addWidget(hint)
        self.set_content(canvas)


class TrackTimelineView(QWidget):
    """字幕轨道（多行 T1/T2/T3）占位。"""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("TrackTimelineView")
        self.setMinimumHeight(120)
        themed(
            self,
            lambda: (
                f"#TrackTimelineView {{ background: {palette().panel_bg}; "
                f"border: 1px solid {palette().card_border}; "
                f"border-radius: 8px; }}"
            ),
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        hint = QLabel("字幕轨道 T1 / T2 / T3（A4 后按字符级色块展示）")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        themed(hint, lambda: f"color: {palette().text_hint}; font-size: 9.5pt;")
        layout.addWidget(hint)
