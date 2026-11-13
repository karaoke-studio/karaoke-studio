"""Shared representative colors for role identity indicators."""

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap

from krok_helper.subtitle_render.domain.models import Style


def scheme_swatch_color(style: Style, role: str) -> QColor | None:
    """Match the role's after-fill swatch; undefined schemes have no color."""
    if not role:
        if style.karaoke_colors is not None:
            return QColor(style.karaoke_colors.after.text.color)
        return QColor(style.fill_color)
    scheme = style.custom_style_schemes.get(role)
    if scheme is None:
        return None
    if scheme.karaoke_colors is not None:
        return QColor(scheme.karaoke_colors.after.text.color)
    if scheme.fill_color:
        return QColor(scheme.fill_color)
    return None


def role_swatch_icon(color: QColor | None) -> QIcon:
    """A circular swatch; undefined schemes retain an outlined neutral ring."""
    pixmap = QPixmap(24, 24)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    if color is None:
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(148, 163, 184, 200), 2))
    else:
        painter.setBrush(color)
        painter.setPen(QPen(QColor(0, 0, 0, 64), 1.5))
    painter.drawEllipse(5, 5, 14, 14)
    painter.end()
    return QIcon(pixmap)
