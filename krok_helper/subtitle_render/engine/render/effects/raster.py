"""Shared path, glow, and text-stack rasterization primitives."""

from __future__ import annotations

import math

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import (
    QImage,
    QPainter,
    QPainterPath,
    QPainterPathStroker,
    QPen,
    QTransform,
)

from krok_helper.subtitle_render.domain.models import (
    Style,
    normalize_glow_concentration_level,
)
from krok_helper.subtitle_render.domain.paint import KaraokeColorState, PaintFill
from krok_helper.subtitle_render.engine.render.core.raster_blur import blur_image
from krok_helper.subtitle_render.engine.render.effects.fills import (
    brush_for_fill,
    fill_brush_rect,
    fill_is_alpha,
)
from krok_helper.subtitle_render.engine.render.effects.metrics import (
    glow_blur_radii,
    glow_concentration_level,
    glow_extent,
    glow_pen_width,
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

def paint_glow_path(
    painter: QPainter,
    path: QPainterPath,
    fill: PaintFill,
    rect: QRectF,
    radius: int,
    stroke_width: int,
    stroke2_width: int,
    source_clip: QRectF | None = None,
    concentration_level: int = 0,
    target_clip: QRectF | None = None,
) -> None:
    if normalize_glow_concentration_level(concentration_level) < 0:
        return
    radius = max(int(radius), 0)
    if radius == 0:
        return
    width = glow_pen_width(stroke_width, stroke2_width, radius)
    bounds = path.boundingRect()
    if bounds.isEmpty():
        return
    pad = glow_extent(stroke_width, stroke2_width, radius) + 2
    layer_rect = bounds.adjusted(-pad, -pad, pad, pad)
    if target_clip is not None:
        needed_left = float(target_clip.left()) - pad
        needed_right = float(target_clip.right()) + pad
        if needed_left > layer_rect.left():
            layer_rect.setLeft(
                layer_rect.left()
                + math.floor(needed_left - layer_rect.left())
            )
        if needed_right < layer_rect.right():
            layer_rect.setRight(
                layer_rect.right()
                - math.floor(layer_rect.right() - needed_right)
            )
        if layer_rect.isEmpty():
            return
    image_w = max(1, math.ceil(layer_rect.width()))
    image_h = max(1, math.ceil(layer_rect.height()))
    source = QImage(image_w, image_h, QImage.Format.Format_ARGB32_Premultiplied)
    source.fill(0)

    local_path = QPainterPath(path)
    local_path.translate(-layer_rect.left(), -layer_rect.top())
    local_rect = rect.translated(-layer_rect.left(), -layer_rect.top())
    source_painter = QPainter(source)
    try:
        source_painter.setRenderHints(
            QPainter.RenderHint.Antialiasing
            | QPainter.RenderHint.TextAntialiasing
            | QPainter.RenderHint.SmoothPixmapTransform
        )
        if source_clip is not None:
            source_painter.setClipRect(
                source_clip.translated(-layer_rect.left(), -layer_rect.top())
            )
        paint_stroke_path(source_painter, local_path, fill, local_rect, width)
    finally:
        source_painter.end()

    target = QPointF(layer_rect.left(), layer_rect.top())
    painter.save()
    try:
        if target_clip is not None:
            painter.setClipRect(target_clip)
        for blur_radius in glow_blur_radii(radius, concentration_level):
            painter.drawImage(target, blur_image(source, blur_radius))
    finally:
        painter.restore()

def paint_split_glow_path(
    painter: QPainter,
    path: QPainterPath,
    before_fill: PaintFill,
    after_fill: PaintFill,
    rect: QRectF,
    radius: int,
    stroke_width: int,
    stroke2_width: int,
    *,
    before_source_clip: QRectF,
    after_source_clip: QRectF,
    concentration_level: int = 0,
    target_clip: QRectF | None = None,
    horizontal_fill_rect: QRectF | None = None,
) -> None:
    """Paint both wipe-source colours into one bitmap, then blur once."""
    if normalize_glow_concentration_level(concentration_level) < 0:
        return
    radius = max(int(radius), 0)
    if radius == 0:
        return
    width = glow_pen_width(stroke_width, stroke2_width, radius)
    bounds = path.boundingRect()
    if bounds.isEmpty():
        return
    pad = glow_extent(stroke_width, stroke2_width, radius) + 2
    layer_rect = bounds.adjusted(-pad, -pad, pad, pad)
    if target_clip is not None:
        needed_left = float(target_clip.left()) - pad
        needed_right = float(target_clip.right()) + pad
        if needed_left > layer_rect.left():
            layer_rect.setLeft(
                layer_rect.left()
                + math.floor(needed_left - layer_rect.left())
            )
        if needed_right < layer_rect.right():
            layer_rect.setRight(
                layer_rect.right()
                - math.floor(layer_rect.right() - needed_right)
            )
        if layer_rect.isEmpty():
            return
    image_w = max(1, math.ceil(layer_rect.width()))
    image_h = max(1, math.ceil(layer_rect.height()))
    source = QImage(image_w, image_h, QImage.Format.Format_ARGB32_Premultiplied)
    source.fill(0)
    local_path = QPainterPath(path)
    local_path.translate(-layer_rect.left(), -layer_rect.top())
    local_rect = rect.translated(-layer_rect.left(), -layer_rect.top())
    local_horizontal_rect = (
        horizontal_fill_rect.translated(-layer_rect.left(), -layer_rect.top())
        if horizontal_fill_rect is not None
        else None
    )
    source_painter = QPainter(source)
    try:
        source_painter.setRenderHints(
            QPainter.RenderHint.Antialiasing
            | QPainter.RenderHint.TextAntialiasing
            | QPainter.RenderHint.SmoothPixmapTransform
        )
        for fill, clip in (
            (before_fill, before_source_clip),
            (after_fill, after_source_clip),
        ):
            source_painter.save()
            try:
                source_painter.setClipRect(
                    clip.translated(-layer_rect.left(), -layer_rect.top())
                )
                paint_stroke_path(
                    source_painter,
                    local_path,
                    fill,
                    fill_brush_rect(fill, local_rect, local_horizontal_rect),
                    width,
                )
            finally:
                source_painter.restore()
    finally:
        source_painter.end()
    target = QPointF(layer_rect.left(), layer_rect.top())
    painter.save()
    try:
        if target_clip is not None:
            painter.setClipRect(target_clip)
        for blur_radius in glow_blur_radii(radius, concentration_level):
            painter.drawImage(target, blur_image(source, blur_radius))
    finally:
        painter.restore()

def paint_text_layer_stack(
    painter: QPainter,
    path: QPainterPath,
    rect: QRectF,
    colors: KaraokeColorState,
    style: Style,
    *,
    stroke_width: int,
    stroke2_width: int,
    shadow_dx: int,
    shadow_dy: int,
    glow_radius: int,
    draw_glow: bool = True,
    fill_rect: QRectF | None = None,
    horizontal_fill_rect: QRectF | None = None,
    draw_shadow: bool = True,
) -> None:
    brush_rect = fill_rect if fill_rect is not None else rect
    if style.decoration_kind == "glow":
        # ``draw_glow=False`` 让调用方把发光单独按「发光级」宽松裁切处理（卡拉ok 走字
        # 时发光软晕不能跟描边/填充一样按字框硬裁，否则会被裁成方框）。
        if draw_glow:
            paint_glow_path(
                painter,
                path,
                colors.shadow,
                fill_brush_rect(colors.shadow, brush_rect, horizontal_fill_rect),
                glow_radius,
                stroke_width,
                stroke2_width,
                concentration_level=glow_concentration_level(style),
            )
    elif (
        style.decoration_kind == "shadow"
        and draw_shadow
        and (shadow_dx or shadow_dy)
    ):
        paint_shadow_silhouette(
            painter,
            path,
            colors.shadow,
            fill_brush_rect(colors.shadow, brush_rect, horizontal_fill_rect),
            shadow_dx,
            shadow_dy,
            stroke_width,
            stroke2_width,
        )

    if stroke2_width > 0:
        paint_stroke_path(
            painter,
            path,
            colors.stroke2,
            fill_brush_rect(colors.stroke2, brush_rect, horizontal_fill_rect),
            stroke2_pen_width(stroke_width, stroke2_width),
        )
    if stroke_width > 0:
        paint_stroke_path(
            painter,
            path,
            colors.stroke,
            fill_brush_rect(colors.stroke, brush_rect, horizontal_fill_rect),
            stroke_pen_width(stroke_width),
            protect_body=fill_is_alpha(colors.text),
        )
    paint_fill_path(
        painter,
        path,
        colors.text,
        fill_brush_rect(colors.text, brush_rect, horizontal_fill_rect),
    )
