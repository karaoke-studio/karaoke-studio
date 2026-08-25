"""Paint-fill brushes, signatures, and their bounded resource caches."""

from __future__ import annotations

import math
from collections import OrderedDict
from threading import Lock

from PyQt6.QtCore import QPointF, QRectF
from PyQt6.QtGui import QColor, QBrush, QImage, QLinearGradient, QTransform

from krok_helper.subtitle_render.domain.paint import KaraokeColorState, PaintFill
from krok_helper.subtitle_render.engine.render.image_resource import (
    image_file_signature,
    warn_image_resource_skipped,
)


IMAGE_FILL_CACHE_MAX = 16
IMAGE_FILL_CACHE: "OrderedDict[tuple, QImage]" = OrderedDict()
IMAGE_BRUSH_CACHE: "OrderedDict[tuple, QBrush]" = OrderedDict()
HARD_BAND_BRUSH_CACHE_MAX = 128
HARD_BAND_BRUSH_CACHE: "OrderedDict[tuple, QBrush]" = OrderedDict()
IMAGE_FILL_LOCK = Lock()


def clear_fill_caches() -> None:
    with IMAGE_FILL_LOCK:
        IMAGE_FILL_CACHE.clear()
        IMAGE_BRUSH_CACHE.clear()
        HARD_BAND_BRUSH_CACHE.clear()


def fill_brush_rect(
    fill: PaintFill,
    rect: QRectF,
    horizontal_rect: QRectF | None,
) -> QRectF:
    """Use the shared ruby/main box only for horizontal gradients."""
    if fill.mode == "gradient_horizontal" and horizontal_rect is not None:
        return horizontal_rect
    return rect


def brush_for_fill(fill: PaintFill, rect: QRectF) -> QBrush:
    if fill.mode == "image" and fill.image_path:
        brush = cached_image_brush(fill.image_path, fill.image_scale_pct)
        if brush is not None:
            return brush

    if fill.mode == "gradient_horizontal":
        return linear_gradient_brush(fill, rect, 0)
    if fill.mode == "gradient_vertical":
        return linear_gradient_brush(fill, rect, 90)
    if fill.mode == "split_vertical":
        return split_vertical_brush(fill, rect)
    return QBrush(valid_color(fill.color, "#FFFFFF"))


def fill_is_alpha(fill: PaintFill) -> bool:
    """Return whether N3 protects the glyph body from its primary edge."""
    if fill.mode == "image":
        return True
    if fill.mode in {"gradient_horizontal", "gradient_vertical"}:
        colors = [color for _position, color in gradient_stops(fill)]
    elif fill.mode == "split_vertical":
        colors = [color for _position, color in split_gradient_stops(fill)]
    else:
        colors = [fill.color]
    return any(valid_color(color, fill.color).alpha() < 255 for color in colors)


def cached_image_brush(path: str, scale_pct: int) -> QBrush | None:
    signature = image_file_signature(path)
    if signature is None:
        return None
    scale = max(1, min(int(scale_pct), 1000))
    brush_key = (*signature, scale)
    with IMAGE_FILL_LOCK:
        brush = IMAGE_BRUSH_CACHE.get(brush_key)
        if brush is not None:
            IMAGE_BRUSH_CACHE.move_to_end(brush_key)
            return QBrush(brush)

    image = cached_fill_image(signature)
    if image is None or image.isNull():
        return None
    brush = QBrush(image)
    brush_scale = scale / 100.0
    brush.setTransform(QTransform().scale(brush_scale, brush_scale))

    with IMAGE_FILL_LOCK:
        IMAGE_BRUSH_CACHE[brush_key] = brush
        while len(IMAGE_BRUSH_CACHE) > IMAGE_FILL_CACHE_MAX:
            IMAGE_BRUSH_CACHE.popitem(last=False)
    return QBrush(brush)


def anchor_texture_brush(brush: QBrush, rect: QRectF) -> QBrush:
    anchored = QBrush(brush)
    transform = QTransform(anchored.transform())
    transform.translate(rect.left(), rect.top())
    anchored.setTransform(transform)
    return anchored


def cached_fill_image(signature: tuple[str, int, int]) -> QImage | None:
    with IMAGE_FILL_LOCK:
        cached = IMAGE_FILL_CACHE.get(signature)
        if cached is not None:
            IMAGE_FILL_CACHE.move_to_end(signature)
            return cached
    image = QImage(signature[0])
    if image.isNull():
        warn_image_resource_skipped(
            signature[0],
            "图片解码失败或不是有效图片文件",
        )
        return None
    with IMAGE_FILL_LOCK:
        IMAGE_FILL_CACHE[signature] = image
        while len(IMAGE_FILL_CACHE) > IMAGE_FILL_CACHE_MAX:
            IMAGE_FILL_CACHE.popitem(last=False)
    return image


def linear_gradient_brush(fill: PaintFill, rect: QRectF, angle_deg: int) -> QBrush:
    angle = math.radians(angle_deg % 360)
    dx = math.cos(angle)
    dy = math.sin(angle)
    projection = abs(rect.width() * dx) + abs(rect.height() * dy)
    if projection <= 0:
        projection = max(rect.width(), rect.height(), 1.0)
    half = projection / 2.0
    center = rect.center()
    start = QPointF(center.x() - dx * half, center.y() - dy * half)
    end = QPointF(center.x() + dx * half, center.y() + dy * half)

    gradient = QLinearGradient(start, end)
    for position, color in gradient_stops(fill):
        gradient.setColorAt(position / 100.0, valid_color(color, fill.color))
    return QBrush(gradient)


def split_vertical_brush(fill: PaintFill, rect: QRectF) -> QBrush:
    """Return an exact hard-band texture, cached by height and stop values."""
    stops = split_gradient_stops(fill)
    height = max(int(math.ceil(rect.height())), 1)
    stop_key = tuple(
        (position, valid_color(color, fill.color).rgba())
        for position, color in stops
    )
    key = (height, stop_key)
    with IMAGE_FILL_LOCK:
        base = HARD_BAND_BRUSH_CACHE.get(key)
        if base is not None:
            HARD_BAND_BRUSH_CACHE.move_to_end(key)
        else:
            image = QImage(1, height, QImage.Format.Format_ARGB32_Premultiplied)
            band_index = 0
            for y in range(height):
                position = (y + 0.5) * 100.0 / height
                while (
                    band_index + 1 < len(stops)
                    and stops[band_index + 1][0] <= position
                ):
                    band_index += 1
                image.setPixelColor(
                    0,
                    y,
                    valid_color(stops[band_index][1], fill.color),
                )
            base = QBrush(image)
            HARD_BAND_BRUSH_CACHE[key] = base
            while len(HARD_BAND_BRUSH_CACHE) > HARD_BAND_BRUSH_CACHE_MAX:
                HARD_BAND_BRUSH_CACHE.popitem(last=False)
    return anchor_texture_brush(base, rect)


def split_gradient_stops(fill: PaintFill) -> list[tuple[float, str]]:
    raw = list(fill.split_stops)
    if len(raw) < 2:
        raw = [
            (0, fill.split_top_color),
            (fill.split_position_pct, fill.split_bottom_color),
            (100, fill.split_bottom_color),
        ]
    stops = sorted(
        (gradient_stop_position(position), color)
        for position, color in raw
    )
    if stops[0][0] > 0:
        stops.insert(0, (0, stops[0][1]))
    if stops[-1][0] < 100:
        stops.append((100, stops[-1][1]))
    return stops


def fill_signature(fill: PaintFill) -> tuple:
    return (
        fill.mode,
        fill.color,
        fill.start_color,
        fill.end_color,
        tuple(gradient_stops(fill)),
        fill.split_top_color,
        fill.split_bottom_color,
        fill.split_position_pct,
        tuple(fill.split_stops),
        fill.image_path,
        fill.image_scale_pct,
    )


def karaoke_state_signature(state: KaraokeColorState) -> tuple:
    return (
        fill_signature(state.text),
        fill_signature(state.stroke),
        fill_signature(state.stroke2),
        fill_signature(state.shadow),
    )


def gradient_stop_position(value: object) -> float:
    try:
        position = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        position = 0.0
    if not math.isfinite(position):
        position = 0.0
    return max(0.0, min(100.0, position))


def gradient_stops(fill: PaintFill) -> list[tuple[float, str]]:
    raw = fill.gradient_stops or [(0, fill.start_color), (100, fill.end_color)]
    normalized = [
        (gradient_stop_position(position), color)
        for position, color in raw
    ]
    normalized.sort(key=lambda item: item[0])
    positions = {position for position, _color in normalized}
    if 0 not in positions:
        normalized.insert(0, (0, fill.start_color))
    if 100 not in positions:
        normalized.append((100, fill.end_color))
    return normalized


def valid_color(value: str, fallback: str) -> QColor:
    color = QColor(value)
    if color.isValid():
        return color
    fallback_color = QColor(fallback)
    return fallback_color if fallback_color.isValid() else QColor("#FF5A6F")
