"""工作台共用的基础控件与字体工具。

从 ``gui_qt.py`` 里抽出来的一层薄公共件，让页面模块（``alignment/``、
``video_download/`` …）能直接复用，而不必反过来 import ``gui_qt`` ——
后者会导致循环依赖，也是各页各自复制一份 ``CardWidget`` 的原因。

只放"没有业务语义、任何页面都可能用"的东西；带工作流状态的控件请留在
各自的页面包里。
"""

from __future__ import annotations

import ctypes

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PyQt6.QtWidgets import (
    QFrame,
    QGraphicsDropShadowEffect,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

# ``QComboBox`` 这个名字在本仓库一律指 qfluentwidgets 的 ``ComboBox``；
# 换成 PyQt 原生的会让下拉框退回系统外观，和整页对不上。
from qfluentwidgets import ComboBox as QComboBox
from qfluentwidgets.components.widgets.combo_box import ComboBoxMenu
from qfluentwidgets.components.widgets.menu import MenuAnimationType

#: Win11 圆角策略：下拉菜单自绘边框，交给 DWM 会多一层描边。
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWCP_DONOTROUND = 1

__all__ = [
    "apply_card_shadow",
    "apply_safe_label_metrics",
    "CardWidget",
    "StyledComboBox",
    "WhiteComboBoxMenu",
    "build_lyrics_ui_font",
    "combo_box_view_qss",
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


def combo_box_view_qss() -> str:
    from krok_helper.theme_workbench import palette

    p = palette()
    selected_bg = "#3A2A2C" if p.is_dark else "#FFF1F2"
    selected_text = p.text_primary if p.is_dark else "#111827"
    hover_bg = p.input_hover_bg if p.is_dark else "#F8FAFC"
    return f"""
    QAbstractItemView {{
        background-color: transparent;
        border: none;
        border-radius: 0px;
        padding: 4px;
        outline: none;
        color: {p.text_primary};
        selection-background-color: {selected_bg};
        selection-color: {selected_text};
    }}

    QAbstractItemView::item {{
        height: 32px;
        padding: 0 12px;
        border-radius: 6px;
        color: {p.text_primary};
    }}

    QAbstractItemView::item:hover {{
        background-color: {hover_bg};
    }}

    QAbstractItemView::item:selected {{
        background-color: {selected_bg};
        color: {selected_text};
    }}
    """


def build_lyrics_ui_font(*, point_size: float = 10.5, bold: bool = False) -> QFont:
    return build_app_ui_font(point_size=point_size, bold=bold)


class WhiteComboBoxMenu(ComboBoxMenu):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.NoDropShadowWindowHint)
        # 保留 qfluentwidgets 默认的透明顶层窗口，不要关闭 WA_TranslucentBackground
        self.view.setStyleSheet(combo_box_view_qss())
        self.view.setFrameShape(QFrame.Shape.NoFrame)
        self.hBoxLayout.setContentsMargins(0, 0, 0, 0)
        self.hBoxLayout.setSpacing(0)
        self.view.setViewportMargins(0, 0, 0, 0)
        self.setShadowEffect(blurRadius=0, offset=(0, 0), color=QColor(0, 0, 0, 0))

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        try:
            preference = ctypes.c_int(DWMWCP_DONOTROUND)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                int(self.winId()),
                DWMWA_WINDOW_CORNER_PREFERENCE,
                ctypes.byref(preference),
                ctypes.sizeof(preference),
            )
        except Exception:
            pass

    def exec(self, pos, ani=True, aniType=MenuAnimationType.DROP_DOWN):
        self.view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.view.adjustSize(pos, aniType)

        overflow = self.view.verticalScrollBar().maximum()
        if overflow > 0:
            self.view.setFixedHeight(self.view.height() + overflow + 8)

        self.adjustSize()
        return super().exec(pos, ani, aniType)

    def paintEvent(self, event) -> None:  # noqa: N802
        from krok_helper.theme_workbench import palette

        p = palette()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor(p.input_border), 1))
        painter.setBrush(QColor(p.input_bg))
        painter.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 8, 8)


class StyledComboBox(QComboBox):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

    def _createComboMenu(self):
        return WhiteComboBoxMenu(self)


def apply_safe_label_metrics(
    label: QLabel,
    font: QFont,
    *,
    top_padding: int = 3,
    bottom_padding: int = 2,
) -> None:
    margins = label.contentsMargins()
    label.setContentsMargins(margins.left(), top_padding, margins.right(), bottom_padding)
    label.setMinimumHeight(QFontMetrics(font).height() + top_padding + bottom_padding)


def apply_card_shadow(widget: QWidget, *, alpha: int = 20) -> None:
    shadow = QGraphicsDropShadowEffect(widget)
    shadow.setBlurRadius(12)
    shadow.setXOffset(0)
    shadow.setYOffset(2)
    shadow.setColor(QColor(16, 24, 40, alpha))
    widget.setGraphicsEffect(shadow)
