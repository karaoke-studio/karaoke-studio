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
        # 裸 QWidget 不设此属性时 QSS 背景/边框不会绘制
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        # 尚未接入真实轨道数据，占位期与拖拽区一致用虚线空态样式
        themed(
            self,
            lambda: (
                f"#TrackTimelineView {{ background: {palette().panel_bg}; "
                f"border: 1px dashed {palette().input_border}; "
                f"border-radius: 8px; }}"
            ),
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(4)
        layout.addStretch(1)
        title = QLabel("字幕轨道")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        themed(
            title,
            lambda: (
                f"color: {palette().text_secondary}; "
                'font-family: "Microsoft YaHei UI"; font-size: 10pt; font-weight: 600;'
            ),
        )
        layout.addWidget(title)
        hint = QLabel("加载字幕后，将按 T1 / T2 / T3 轨道显示字符级时间色块")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        themed(hint, lambda: f"color: {palette().text_hint}; font-size: 9pt;")
        layout.addWidget(hint)
        layout.addStretch(1)
