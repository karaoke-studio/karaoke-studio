"""底部波形 + 字幕轨道（被动显示，不接受拖拽）。

音频源自动取自视频文件（``load_video`` 同时喂给 ``TransportBar``），所以这里
不再做独立的"拖入音频"入口；占位框留着，后续 A7 / A8 接入真实波形渲染。
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QLabel, QVBoxLayout, QWidget

from krok_helper.subtitle_render.frontend.theme import palette, themed


class WaveformPanel(QWidget):
    """波形面板占位——音频从视频自动取，无独立拖拽入口。"""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("WaveformPanel")
        self.setMinimumHeight(72)
        themed(
            self,
            lambda: (
                f"#WaveformPanel {{ background: {palette().preview_bg}; "
                f"border: 1px solid {palette().card_border}; "
                f"border-radius: 8px; }}"
            ),
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        hint = QLabel("波形（接入视频音频后显示峰值 + playhead）")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        themed(hint, lambda: f"color: {palette().text_hint}; font-size: 9pt;")
        layout.addWidget(hint)


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
