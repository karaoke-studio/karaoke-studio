"""Painted color surfaces shared by the subtitle color dialog."""

from __future__ import annotations

import math

from PyQt6.QtCore import QPointF, QRectF, QSize, Qt, pyqtSignal as Signal
from PyQt6.QtGui import QColor, QConicalGradient, QImage, QLinearGradient, QPainter, QPainterPath, QPen, QRadialGradient
from PyQt6.QtWidgets import QAbstractButton, QWidget

from krok_helper.subtitle_render.frontend.widgets.theme import palette


def paint_checkerboard(painter: QPainter, rect: QRectF, tile: int = 8) -> None:
    """Draw the alpha background; callers clip it to their own rounded shape."""
    painter.fillRect(rect, QColor("#FFFFFF"))
    for row, y in enumerate(range(int(rect.top()), int(rect.bottom()) + 1, tile)):
        for col, x in enumerate(range(int(rect.left()), int(rect.right()) + 1, tile)):
            if (row + col) % 2 == 0:
                painter.fillRect(QRectF(x, y, tile, tile), QColor("#CDD2DA"))


def _draw_handle(painter: QPainter, center: QPointF) -> None:
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.setPen(QPen(QColor("#18202C"), 4))
    painter.drawEllipse(center, 6, 6)
    painter.setPen(QPen(QColor("#FFFFFF"), 2))
    painter.drawEllipse(center, 6, 6)


def _draw_slider_handle(painter: QPainter, center: QPointF) -> None:
    """Slim rounded thumb with a white face and contrasting dark edge."""
    rect = QRectF(center.x() - 2.5, center.y() - 10, 5, 20)
    painter.save()
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(0, 0, 0, 35))
    painter.drawRoundedRect(rect.adjusted(-.5, 0, .5, 1.5), 2.5, 2.5)
    painter.setPen(QPen(QColor("#46505F"), 1.1))
    painter.setBrush(QColor("#FFFFFF"))
    painter.drawRoundedRect(rect, 2, 2)
    painter.restore()


class HueSaturationPlane(QWidget):
    """N3-style hue (horizontal) / saturation (vertical) plane at full value."""

    colorChanged = Signal(float, float)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.hue = 0.0
        self.saturation = 1.0
        self.setMinimumSize(280, 220)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setAccessibleName("色相与饱和度")
        self.setToolTip("左右调整色相，上下调整饱和度；方向键微调，Shift 加速")

    def surface_rect(self) -> QRectF:
        return QRectF(self.rect()).adjusted(9, 9, -9, -9)

    def set_color(self, hue: float, saturation: float) -> None:
        self.hue = max(0.0, min(359.0, hue))
        self.saturation = max(0.0, min(1.0, saturation))
        self.update()

    def _pick(self, position: QPointF) -> None:
        rect = self.surface_rect()
        hue = max(0.0, min(1.0, (position.x() - rect.left()) / rect.width())) * 359
        saturation = 1 - max(0.0, min(1.0, (position.y() - rect.top()) / rect.height()))
        self.set_color(hue, saturation)
        self.colorChanged.emit(self.hue, self.saturation)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.setFocus(Qt.FocusReason.MouseFocusReason)
            self._pick(event.position())
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if event.buttons() & Qt.MouseButton.LeftButton:
            self._pick(event.position())
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        step = 10 if event.modifiers() & Qt.KeyboardModifier.ShiftModifier else 1
        key = event.key()
        if key in (Qt.Key.Key_Left, Qt.Key.Key_Right):
            self.set_color(self.hue + (step if key == Qt.Key.Key_Right else -step), self.saturation)
        elif key in (Qt.Key.Key_Up, Qt.Key.Key_Down):
            self.set_color(self.hue, self.saturation + (step if key == Qt.Key.Key_Up else -step) / 100)
        else:
            super().keyPressEvent(event)
            return
        self.colorChanged.emit(self.hue, self.saturation)
        event.accept()

    def focusInEvent(self, event) -> None:  # noqa: N802
        super().focusInEvent(event)
        self.update()

    def focusOutEvent(self, event) -> None:  # noqa: N802
        super().focusOutEvent(event)
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.surface_rect()
        path = QPainterPath()
        path.addRoundedRect(rect, 8, 8)
        painter.setClipPath(path)
        hue = QLinearGradient(rect.topLeft(), rect.topRight())
        for index in range(7):
            hue.setColorAt(index / 6, QColor.fromHsvF((index % 6) / 6, 1, 1))
        painter.fillRect(rect, hue)
        saturation = QLinearGradient(rect.topLeft(), rect.bottomLeft())
        saturation.setColorAt(0, QColor(255, 255, 255, 0))
        saturation.setColorAt(1, QColor("#FFFFFF"))
        painter.fillRect(rect, saturation)
        painter.setClipping(False)
        painter.setPen(QPen(QColor(palette().card_border), 1))
        painter.drawPath(path)
        _draw_handle(painter, QPointF(rect.left() + self.hue / 359 * rect.width(),
                                    rect.top() + (1 - self.saturation) * rect.height()))
        painter.end()


class HueSaturationWheel(QWidget):
    """HSV disk at V=1: hue counterclockwise from red/right, saturation radial.

    The six RGB/CMY hues are equally spaced by 60 degrees. Mixing white into
    the full-saturation hue is exactly HSV at V=1, not a perceptual/RYB wheel.
    """

    colorChanged = Signal(float, float)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.hue = 0.0
        self.saturation = 1.0
        self._dragging = False
        self._surface_key: tuple[float, float] | None = None
        self._surface = QImage()
        self.setMinimumSize(320, 320)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setAccessibleName("HSV 色相与饱和度色盘")
        self.setToolTip("HSV 色盘：沿圆周选择色相，从圆心向外增加饱和度；"
                        "左右键调整色相，上下键调整饱和度，Shift 加速；明度由下方滑条调整")

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(340, 340)

    def surface_rect(self) -> QRectF:
        side = max(min(self.width(), self.height()) - 18, 1)
        return QRectF((self.width() - side) / 2, (self.height() - side) / 2, side, side)

    def handle_position(self) -> QPointF:
        rect = self.surface_rect()
        angle = math.radians(self.hue)
        radius = rect.width() / 2 * self.saturation
        return rect.center() + QPointF(math.cos(angle) * radius, -math.sin(angle) * radius)

    def set_color(self, hue: float, saturation: float) -> None:
        self.hue = hue % 360.0
        self.saturation = max(0.0, min(1.0, saturation))
        self.update()

    def _pick(self, position: QPointF) -> None:
        rect = self.surface_rect()
        dx, dy = position.x() - rect.center().x(), rect.center().y() - position.y()
        distance = math.hypot(dx, dy)
        # White has no hue: reaching the center must not erase the selected hue.
        hue = math.degrees(math.atan2(dy, dx)) % 360 if distance > 1e-9 else self.hue
        saturation = min(distance / (rect.width() / 2), 1.0)
        self.set_color(hue, saturation)
        self.colorChanged.emit(self.hue, self.saturation)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            rect = self.surface_rect()
            delta = event.position() - rect.center()
            if math.hypot(delta.x(), delta.y()) > rect.width() / 2 + 0.5:
                event.ignore()
                return
            self._dragging = True
            self.setFocus(Qt.FocusReason.MouseFocusReason)
            self._pick(event.position())
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._dragging and event.buttons() & Qt.MouseButton.LeftButton:
            self._pick(event.position())
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self._dragging:
            self._pick(event.position())
            self._dragging = False
            event.accept()
        else:
            super().mouseReleaseEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        step = 10 if event.modifiers() & Qt.KeyboardModifier.ShiftModifier else 1
        key = event.key()
        if key in (Qt.Key.Key_Left, Qt.Key.Key_Right):
            self.set_color(self.hue + (step if key == Qt.Key.Key_Right else -step), self.saturation)
        elif key in (Qt.Key.Key_Up, Qt.Key.Key_Down):
            self.set_color(self.hue, self.saturation + (step if key == Qt.Key.Key_Up else -step) / 100)
        else:
            super().keyPressEvent(event)
            return
        self.colorChanged.emit(self.hue, self.saturation)
        event.accept()

    def focusInEvent(self, event) -> None:  # noqa: N802
        super().focusInEvent(event)
        self.update()

    def focusOutEvent(self, event) -> None:  # noqa: N802
        super().focusOutEvent(event)
        self.update()

    def surface_image(self) -> QImage:
        """Cache the static disk at device resolution; dragging only moves a handle."""
        side = self.surface_rect().width()
        dpr = self.devicePixelRatioF()
        key = (side, dpr)
        if self._surface_key == key:
            return self._surface
        pixels = max(int(math.ceil(side * dpr)), 1)
        image = QImage(pixels, pixels, QImage.Format.Format_ARGB32_Premultiplied)
        image.setDevicePixelRatio(dpr)
        image.fill(0)
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(0, 0, side, side)
        path = QPainterPath()
        path.addEllipse(rect)
        painter.setClipPath(path)
        hue = QConicalGradient(rect.center(), 0)
        for index in range(7):
            hue.setColorAt(index / 6, QColor.fromHsvF((index % 6) / 6, 1, 1))
        painter.fillRect(rect, hue)
        saturation = QRadialGradient(rect.center(), side / 2)
        saturation.setColorAt(0, QColor("#FFFFFF"))
        saturation.setColorAt(1, QColor(255, 255, 255, 0))
        painter.fillRect(rect, saturation)
        painter.end()
        self._surface_key, self._surface = key, image
        return image

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.surface_rect()
        painter.drawImage(rect.topLeft(), self.surface_image())
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(palette().card_border), 1))
        painter.drawEllipse(rect)
        _draw_handle(painter, self.handle_position())
        painter.end()


class ColorChannelSlider(QWidget):
    """Horizontal gradient slider with direct clicking, dragging and keyboard input."""

    valueChanged = Signal(int)

    def __init__(self, channel: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.channel = channel
        self._maximum = 3599 if channel == "hue" else 1000 if channel in {"saturation", "pro_value", "pro_alpha"} else 255
        self._color = QColor("#FFFFFF")
        self._value = 255
        self.setFixedHeight(30)
        self.setMinimumWidth(150)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAccessibleName({"red": "红色通道", "green": "绿色通道", "blue": "蓝色通道",
                               "hue": "色相", "saturation": "饱和度", "pro_alpha": "不透明度"}.get(channel,
                               "不透明度" if channel == "alpha" else "明度"))

    @property
    def alpha(self) -> int:
        return self._value

    @property
    def value(self) -> int:
        return self._value

    def set_color(self, color: QColor, value: int | None = None) -> None:
        self._color = QColor(color)
        self._value = (color.alpha() if self.channel == "alpha" else color.value()) if value is None else value
        value_text = (f"{self._value / 10:.1f}°" if self.channel == "hue"
                      else str(self._value) if self.channel in {"red", "green", "blue"}
                      else f"{round(self._value / self._maximum * 100)}%")
        self.setToolTip(f"{self.accessibleName()}：{value_text}")
        self.update()

    def surface_rect(self) -> QRectF:
        return QRectF(self.rect()).adjusted(9, 9, -9, -9)

    def _set_value(self, value: int) -> None:
        value = max(0, min(self._maximum, value))
        if value != self._value:
            self._value = value
            self.update()
            self.valueChanged.emit(value)

    def _pick(self, x: float) -> None:
        rect = self.surface_rect()
        self._set_value(round((x - rect.left()) / rect.width() * self._maximum))

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.setFocus(Qt.FocusReason.MouseFocusReason)
            self._pick(event.position().x())
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if event.buttons() & Qt.MouseButton.LeftButton:
            self._pick(event.position().x())
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        step = 10 if event.modifiers() & Qt.KeyboardModifier.ShiftModifier else 1
        key = event.key()
        if key in (Qt.Key.Key_Right, Qt.Key.Key_Up, Qt.Key.Key_PageUp):
            self._set_value(self._value + (16 if key == Qt.Key.Key_PageUp else step))
        elif key in (Qt.Key.Key_Left, Qt.Key.Key_Down, Qt.Key.Key_PageDown):
            self._set_value(self._value - (16 if key == Qt.Key.Key_PageDown else step))
        elif key in (Qt.Key.Key_Home, Qt.Key.Key_End):
            self._set_value(0 if key == Qt.Key.Key_Home else self._maximum)
        else:
            super().keyPressEvent(event)
            return
        event.accept()

    def focusInEvent(self, event) -> None:  # noqa: N802
        super().focusInEvent(event)
        self.update()

    def focusOutEvent(self, event) -> None:  # noqa: N802
        super().focusOutEvent(event)
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.surface_rect()
        path = QPainterPath()
        path.addRoundedRect(rect, 3, 3)
        painter.setClipPath(path)
        start, end = QColor(self._color), QColor(self._color)
        end.setAlpha(255)
        if self.channel in {"alpha", "pro_alpha"}:
            paint_checkerboard(painter, rect, 6)
            start.setAlpha(0)
        elif self.channel in {"red", "green", "blue"}:
            setter = {"red": "setRed", "green": "setGreen", "blue": "setBlue"}[self.channel]
            start.setAlpha(255)
            getattr(start, setter)(0)
            getattr(end, setter)(255)
        elif self.channel == "saturation":
            hue = max(0, self._color.hsvHueF())
            start = QColor.fromHsvF(hue, 0, self._color.valueF())
            end = QColor.fromHsvF(hue, 1, self._color.valueF())
        else:
            start = QColor("#000000")
        gradient = QLinearGradient(rect.topLeft(), rect.topRight())
        gradient.setColorAt(0, start)
        gradient.setColorAt(1, end)
        if self.channel == "hue":
            for index in range(7):
                gradient.setColorAt(index / 6, QColor.fromHsvF((index % 6) / 6, 1, 1))
        painter.fillRect(rect, gradient)
        painter.setClipping(False)
        painter.setPen(QPen(QColor(palette().card_border), 1))
        painter.drawPath(path)
        _draw_slider_handle(painter, QPointF(rect.left() + self._value / self._maximum * rect.width(), rect.center().y()))
        painter.end()


class ColorTile(QAbstractButton):
    """Rounded RGBA swatch; empty history slots are distinct from real white."""

    def __init__(self, color: QColor | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.color = QColor(color) if color is not None else QColor()
        self.setMinimumSize(24, 24)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.set_color(self.color)

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(28, 28)

    def set_color(self, color: QColor) -> None:
        self.color = QColor(color)
        label = color.name(QColor.NameFormat.HexArgb).upper() if color.isValid() else "暂无颜色"
        self.setAccessibleName(label)
        self.setToolTip(label)
        self.setEnabled(color.isValid())
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(2, 2, -2, -2)
        path = QPainterPath()
        path.addRoundedRect(rect, 5, 5)
        painter.setClipPath(path)
        if self.color.isValid():
            paint_checkerboard(painter, rect, 5)
            painter.fillRect(rect, self.color)
        else:
            painter.fillRect(rect, QColor(palette().shell_bg))
        painter.setClipping(False)
        highlighted = self.isChecked() or self.hasFocus()
        painter.setPen(QPen(QColor(palette().accent_primary if highlighted or self.underMouse() else palette().input_border),
                            2 if highlighted else 1))
        painter.drawPath(path)
        painter.end()
