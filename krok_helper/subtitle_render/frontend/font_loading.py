"""字体列表冷读取时的可见占位反馈。"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QFrame, QVBoxLayout, QWidget
from qfluentwidgets import IndeterminateProgressBar, StrongBodyLabel

from krok_helper.subtitle_render.n3_font_catalog import is_n3_font_catalog_ready


class FontListLoadingOverlay(QWidget):
    """覆盖父控件的半透明占位层：「正在读取字体列表…」。"""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(
            "FontListLoadingOverlay { background: rgba(0, 0, 0, 110); }"
            "QFrame#fontListLoadingCard { background: rgba(255, 255, 255, 240);"
            " border-radius: 8px; }"
            "StrongBodyLabel { color: rgba(0, 0, 0, 210); }"
        )
        root = QVBoxLayout(self)
        root.addStretch(1)
        card = QFrame(self)
        card.setObjectName("fontListLoadingCard")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(24, 20, 24, 20)
        card_layout.setSpacing(12)
        label = StrongBodyLabel("正在读取字体列表…")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        bar = IndeterminateProgressBar()
        bar.setFixedWidth(220)
        card_layout.addWidget(label)
        card_layout.addWidget(bar, 0, Qt.AlignmentFlag.AlignHCenter)
        root.addWidget(card, 0, Qt.AlignmentFlag.AlignCenter)
        root.addStretch(1)
        self.resize(parent.size())
        self.raise_()

    def paint_immediately(self) -> None:
        """在主线程阻塞读取前把占位层画出来。

        Qt 字体库枚举不是线程安全的（EMBEDDING §8），读取只能留在主线程，
        事件循环期间不会刷新动画；这里先同步绘制一帧保证占位可见。
        """

        self.show()
        self.repaint()
        QApplication.processEvents()


@contextlib.contextmanager
def font_list_loading_overlay(parent: Optional[QWidget]) -> Iterator[Optional[FontListLoadingOverlay]]:
    """冷目录构建期间显示占位层，读取完成后销毁。

    目录已暖（进程内已构建过，只有毫秒级字典操作）或父控件不可见时
    不打扰——暖路径闪现占位反而干扰。
    """

    overlay: Optional[FontListLoadingOverlay] = None
    if (
        parent is not None
        and parent.isVisible()
        and not is_n3_font_catalog_ready()
    ):
        overlay = FontListLoadingOverlay(parent)
        overlay.paint_immediately()
    try:
        yield overlay
    finally:
        if overlay is not None:
            overlay.deleteLater()
