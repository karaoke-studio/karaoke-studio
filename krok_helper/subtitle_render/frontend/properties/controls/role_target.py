"""Emphasize the active style target while retaining normal combo-box behavior."""

from PyQt6.QtCore import QRectF, Qt, pyqtSignal as Signal
from PyQt6.QtGui import QColor, QFont, QPainter, QPen
from qfluentwidgets import FluentIcon as FIF

from krok_helper.subtitle_render.frontend.properties.controls.inputs import WheelFocusedComboBox
from krok_helper.subtitle_render.frontend.widgets.theme import palette


class RoleTargetComboBox(WheelFocusedComboBox):
    menuAboutToShow = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._identity_color = None
        self._global_target = True
        font = self.font()
        font.setWeight(QFont.Weight.DemiBold)
        self.setFont(font)

    def set_identity(self, color: QColor | None, *, global_target: bool) -> None:
        color = QColor(color) if color is not None and color.isValid() else None
        if color == self._identity_color and global_target == self._global_target:
            return
        self._identity_color = color
        self._global_target = global_target
        self.update()

    def _createComboMenu(self):
        self.menuAboutToShow.emit()
        return super()._createComboMenu()

    def paintEvent(self, event):  # noqa: N802
        p = palette()
        color = self._identity_color or QColor(p.text_hint)
        base = QColor(p.input_bg)
        border = QColor(p.input_border)
        if not self._global_target:
            # Tint the surface instead of using the role color for text: white,
            # dark and translucent role fills must all leave the name legible.
            def blend(background, amount):
                return QColor.fromRgbF(*[
                    a * (1 - amount) + b * amount for a, b in zip(
                        background.getRgbF()[:3], color.getRgbF()[:3],
                    )
                ])
            base = blend(base, .16 if p.is_dark else .09)
            border = blend(border, .65)
        if self.hasFocus():
            border = QColor(p.input_border_focus)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(border, 1.5 if self.hasFocus() else 1))
        painter.setBrush(base)
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), 6, 6)
        if self.isHover or self.isPressed:
            painter.fillRect(self.rect().adjusted(6, 4, -6, -4), QColor(128, 128, 128, 10))
        painter.setPen(QPen(QColor(p.input_border_hover), 1))
        painter.setBrush(color)
        painter.drawEllipse(QRectF(11, (self.height() - 10) / 2, 10, 10))
        painter.setPen(QColor(p.text_primary))
        painter.setFont(self.font())
        text = painter.fontMetrics().elidedText(
            self.currentText(), Qt.TextElideMode.ElideRight, max(0, self.width() - 58),
        )
        painter.drawText(
            self.rect().adjusted(30, 0, -28, 0),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, text,
        )
        FIF.ARROW_DOWN.render(
            painter, QRectF(self.width() - 22, self.height() / 2 - 5 + self.arrowAni.y, 10, 10),
            fill=p.text_secondary,
        )
