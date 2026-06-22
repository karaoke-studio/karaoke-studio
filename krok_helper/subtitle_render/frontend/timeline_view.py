"""底部字幕轨道（被动显示，不接受拖拽）。

音频源自动取自视频文件（``load_video`` 同时喂给 ``TransportBar``），所以这里
不做独立的"拖入音频"入口。波形图功能已弃用——不做峰值波形展示。
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QLabel, QVBoxLayout, QWidget

from krok_helper.subtitle_render.frontend.theme import palette, themed


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
