"""工作台共用的基础控件与字体工具。

从 ``gui_qt.py`` 里抽出来的一层薄公共件，让页面模块（``alignment/``、
``video_download/`` …）能直接复用，而不必反过来 import ``gui_qt`` ——
后者会导致循环依赖，也是各页各自复制一份 ``CardWidget`` 的原因。

只放"没有业务语义、任何页面都可能用"的东西；带工作流状态的控件请留在
各自的页面包里。
"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

__all__ = [
    "CardWidget",
    "ControlBar",
    "DEFAULT_UI_FONT_FAMILIES",
    "ElidedLabel",
    "build_app_ui_font",
]


DEFAULT_UI_FONT_FAMILIES = [
    "Microsoft YaHei UI",
    "Microsoft YaHei",
    "Segoe UI",
    "Yu Gothic UI",
    "Meiryo UI",
    "Meiryo",
    "PingFang SC",
]


def build_app_ui_font(*, point_size: float = 10.5, bold: bool = False) -> QFont:
    font = QFont()
    font.setFamilies(DEFAULT_UI_FONT_FAMILIES)
    font.setPointSizeF(point_size)
    font.setStyleStrategy(QFont.StyleStrategy.PreferDefault)
    font.setHintingPreference(QFont.HintingPreference.PreferFullHinting)
    if bold:
        font.setBold(True)
    return font


class CardWidget(QFrame):
    """圆角卡片容器。配色由全局 QSS 的 ``QFrame[cardWidget="true"]`` 驱动。"""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        radius: int = 10,
        padding: tuple[int, int, int, int] = (14, 14, 14, 14),
        spacing: int = 12,
    ) -> None:
        super().__init__(parent)
        self.setProperty("cardWidget", True)
        self.setProperty("cardRadius", radius)
        self._default_padding = padding
        self._default_spacing = spacing

    def createVBoxLayout(self) -> QVBoxLayout:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(*self._default_padding)
        layout.setSpacing(self._default_spacing)
        return layout

    def createHBoxLayout(self) -> QHBoxLayout:
        layout = QHBoxLayout(self)
        layout.setContentsMargins(*self._default_padding)
        layout.setSpacing(self._default_spacing)
        return layout

    def createGridLayout(self) -> QGridLayout:
        layout = QGridLayout(self)
        layout.setContentsMargins(*self._default_padding)
        layout.setHorizontalSpacing(self._default_spacing)
        layout.setVerticalSpacing(self._default_spacing)
        return layout


class ElidedLabel(QLabel):
    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._full_text = ""
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.setWordWrap(False)
        self.setText(text)

    def setText(self, text: str) -> None:  # noqa: N802
        self._full_text = str(text or "")
        self.setToolTip(self._full_text)
        self._sync_elided_text()

    def setFont(self, font: QFont) -> None:  # noqa: N802
        super().setFont(font)
        self._sync_elided_text()

    def setMaximumWidth(self, maxw: int) -> None:  # noqa: N802
        super().setMaximumWidth(maxw)
        self._sync_elided_text()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._sync_elided_text()

    def _sync_elided_text(self) -> None:
        width = max(0, self.width())
        if width <= 0 and self.maximumWidth() < 16_777_215:
            width = self.maximumWidth()
        if width <= 0:
            display = self._full_text
        else:
            display = self.fontMetrics().elidedText(
                self._full_text,
                Qt.TextElideMode.ElideRight,
                width,
            )
        if super().text() != display:
            super().setText(display)


class ControlBar(CardWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent, radius=10, padding=(14, 12, 14, 12), spacing=10)

    def apply_button_metrics(self, *buttons: QWidget) -> None:
        for button in buttons:
            if hasattr(button, "setMinimumHeight"):
                button.setMinimumHeight(34)
