"""Theme-aware property tabs with generous hit targets and overflow scrolling."""

from PyQt6.QtCore import QRectF, QSize, Qt
from PyQt6.QtGui import QColor, QPainter, QPen
from PyQt6.QtWidgets import QFrame, QScrollArea, QSizePolicy, QWidget
from qfluentwidgets import SegmentedWidget
from qfluentwidgets.components.navigation.segmented_widget import SegmentedItem

from krok_helper.subtitle_render.frontend.widgets.theme import palette


class _PropertyNavigationItem(SegmentedItem):
    def __init__(self, text, icon, parent):
        super().__init__(parent=parent)
        self.setText(text)
        self._page_icon = icon
        self.setIcon(icon)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        font = self.font()
        font.setPixelSize(13)
        self.setFont(font)
        self.setFixedHeight(32)
        self.setMinimumWidth(self.sizeHint().width())
        self.setToolTip(text)

    def sizeHint(self):  # noqa: N802
        return QSize(self.fontMetrics().horizontalAdvance(self.text()) + 38, 32)

    def hitButton(self, pos):  # noqa: N802
        # The inherited Fluent style excludes 3px above/below its bevel, but
        # our custom tab and hover cover the full widget rectangle.
        return self.rect().contains(pos)

    def paintEvent(self, event):  # noqa: N802
        p = palette()
        selected = self.isSelected
        background = p.accent_primary if selected else p.secondary_button_hover_bg
        if self.isDown():
            background = p.accent_pressed if selected else p.secondary_button_pressed_bg
        elif self.underMouse():
            background = p.accent_hover if selected else p.secondary_button_pressed_bg
        foreground = "#FFFFFF" if selected else p.text_primary
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(background))
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), 7, 7)
        if self.hasFocus():
            painter.setPen(QPen(QColor(p.input_border_focus), 1.5))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(QRectF(self.rect()).adjusted(2, 2, -2, -2), 6, 6)
        font = self.font()
        font.setWeight(font.Weight.DemiBold if selected else font.Weight.Normal)
        painter.setFont(font)
        text_width = painter.fontMetrics().horizontalAdvance(self.text())
        left = (self.width() - text_width - 22) / 2
        self._page_icon.render(
            painter, QRectF(left, (self.height() - 16) / 2, 16, 16),
            fill=foreground if selected else p.text_secondary,
        )
        painter.setPen(QColor(foreground))
        painter.drawText(
            QRectF(left + 22, 0, text_width + 2, self.height()),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self.text(),
        )


class PropertyPageNavigation(SegmentedWidget):
    """Retain the existing route/selection API without the tiny underline."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.hBoxLayout.setContentsMargins(4, 4, 4, 4)
        self.hBoxLayout.setSpacing(4)
        self.setStyleSheet("SegmentedWidget { background: transparent; border: none; }")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        self.setFixedHeight(40)
        self.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)

    def insertItem(self, index, routeKey, text, onClick=None, icon=None):  # noqa: N802
        if routeKey in self.items:
            return
        item = _PropertyNavigationItem(text, icon, self)
        self.insertWidget(index, routeKey, item, onClick)
        self.setFixedWidth(self.hBoxLayout.sizeHint().width())
        return item

    def paintEvent(self, event):  # noqa: N802
        # Selection is painted on the buttons themselves, rather than as a
        # second moving underline beneath the selected pill.
        QWidget.paintEvent(self, event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        p = palette()
        painter.setPen(QPen(QColor(p.card_border), 1))
        painter.setBrush(QColor(p.card_bg))
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(.5, .5, -.5, -.5), 10, 10)


class PropertyNavigationScrollArea(QScrollArea):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setWidgetResizable(False)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setMinimumWidth(0)
        self.setFixedHeight(40)
        self.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        self.viewport().setAutoFillBackground(False)

    def setWidget(self, widget):  # noqa: N802
        super().setWidget(widget)
        # QScrollArea.setWidget enables auto-fill on its child. Keep rounded
        # corners transparent instead of painting a rectangular palette behind.
        widget.setAutoFillBackground(False)

    def wheelEvent(self, event):  # noqa: N802
        bar = self.horizontalScrollBar()
        if bar.maximum() == 0:
            super().wheelEvent(event)
            return
        delta = event.pixelDelta().x() or event.pixelDelta().y()
        if not delta:
            delta = (event.angleDelta().x() or event.angleDelta().y()) / 2
        bar.setValue(bar.value() - round(delta))
        event.accept()
