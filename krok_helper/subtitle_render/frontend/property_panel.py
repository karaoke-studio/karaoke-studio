"""右侧属性面板（占位）。

仿 Sayatoo 顶级 tab：基本 / 字幕 / 特效 / 装饰。

每个 tab 内最终用 ``SettingCardGroup`` 折叠分组（详见设计文档 §C）。当前阶段
仅显示标签页骨架，便于 A4 之后按字段陆续填入。
"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QLabel, QTabWidget, QVBoxLayout, QWidget


def _placeholder_page(text: str) -> QWidget:
    page = QWidget()
    layout = QVBoxLayout(page)
    layout.setContentsMargins(16, 16, 16, 16)
    label = QLabel(text)
    label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    label.setWordWrap(True)
    label.setStyleSheet("color: #888;")
    layout.addWidget(label)
    layout.addStretch(1)
    return page


class PropertyPanel(QTabWidget):
    """字幕样式 / 特效 / 装饰属性面板。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumWidth(280)
        self.addTab(
            _placeholder_page(
                "屏幕预设 / 像素纵横比 / 宽高 / 视图 / 布局 / 时间偏移……\n"
                "（A8 / A10 之后接入）"
            ),
            "基本",
        )
        self.addTab(
            _placeholder_page(
                "字体 / 字号 / 字重 / 描边 / 阴影 / 底色 / 填充色……\n"
                "（A4 / A5 / A6 之后接入）"
            ),
            "字幕",
        )
        self.addTab(
            _placeholder_page(
                "入场 / 退场动画、渐变填充、发光（P1 / P2 任务）。"
            ),
            "特效",
        )
        self.addTab(
            _placeholder_page(
                "标题字幕、时段图片、注音样式（B7 / P2 任务）。"
            ),
            "装饰",
        )
