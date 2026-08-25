"""Reusable fill, outline, and shadow path painting primitives."""

from __future__ import annotations

from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import (
    QPainter,
    QPainterPath,
    QPainterPathStroker,
    QPen,
    QTransform,
)

from krok_helper.subtitle_render.domain.paint import PaintFill
from krok_helper.subtitle_render.engine.render.effects.fills import brush_for_fill
from krok_helper.subtitle_render.engine.render.effects.metrics import (
    stroke2_pen_width,
    stroke_pen_width,
)


def paint_fill_path(
    painter: QPainter,
    path: QPainterPath,
    fill: PaintFill,
    rect: QRectF,
) -> None:
    painter.fillPath(path, brush_for_fill(fill, rect))


def paint_stroke_path(
    painter: QPainter,
    path: QPainterPath,
    fill: PaintFill,
    rect: QRectF,
    width: int,
    *,
    protect_body: bool = False,
) -> None:
    brush = brush_for_fill(fill, rect)
    pen_width = max(width, 1)
    if protect_body:
        stroker = QPainterPathStroker()
        stroker.setWidth(float(pen_width))
        stroker.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        stroker.setCapStyle(Qt.PenCapStyle.RoundCap)
        outline = stroker.createStroke(path).subtracted(path)
        painter.fillPath(outline, brush)
        return
    pen = QPen(brush, pen_width)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.strokePath(path, pen)


def paint_shadow_silhouette(
    painter: QPainter,
    path: QPainterPath,
    fill: PaintFill,
    rect: QRectF,
    dx: int,
    dy: int,
    stroke_width: int,
    stroke2_width: int,
) -> None:
    """Paint N3's translated whole-glyph shadow silhouette."""
    shadow_path = QTransform().translate(dx, dy).map(path)
    shadow_rect = rect.translated(dx, dy)
    pen_width = (
        stroke2_pen_width(stroke_width, stroke2_width)
        if stroke2_width > 0
        else stroke_pen_width(stroke_width)
    )
    if pen_width > 0:
        paint_stroke_path(painter, shadow_path, fill, shadow_rect, pen_width)
    paint_fill_path(painter, shadow_path, fill, shadow_rect)
