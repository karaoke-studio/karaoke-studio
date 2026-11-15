"""Shared fill previews for the preset library and role indicators."""

from pathlib import Path
from collections import OrderedDict

from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap

from krok_helper.subtitle_render.domain.models import Style, SubtitleStyleScheme
from krok_helper.subtitle_render.domain.paint import PaintFill
from krok_helper.subtitle_render.engine.render.effects.fills import (
    brush_for_fill,
    cached_fill_image,
    fill_signature,
    gradient_stops,
    split_gradient_stops,
)
from krok_helper.subtitle_render.engine.render.image_resource import image_file_signature

_TINT_CACHE: OrderedDict[tuple, QColor] = OrderedDict()


def swatch_icon_from_pixmap(pixmap: QPixmap) -> QIcon:
    """Color samples must retain their colors in selected/disabled rows."""
    icon = QIcon()
    for mode in QIcon.Mode:
        for state in QIcon.State:
            icon.addPixmap(pixmap, mode, state)
    return icon


def scheme_text_fills(scheme: Style | SubtitleStyleScheme) -> tuple[PaintFill, PaintFill]:
    """Prefer the actual color matrix, retaining legacy schemes' solid fills."""
    if scheme.karaoke_colors is not None:
        return scheme.karaoke_colors.before.text, scheme.karaoke_colors.after.text
    return (
        PaintFill(color=scheme.base_color or "#FFFFFF"),
        PaintFill(color=scheme.fill_color or "#FF5A6F"),
    )


def scheme_swatch_fill(style: Style, role: str) -> PaintFill | None:
    scheme = style.custom_style_schemes.get(role) if role else style
    if scheme is None:
        return None
    return scheme_text_fills(scheme)[1]


def swatch_key(fill: PaintFill | QColor | None) -> tuple:
    if isinstance(fill, PaintFill):
        image_key = image_file_signature(fill.image_path) if fill.mode == "image" else None
        return ("fill", fill_signature(fill), image_key)
    return (None,) if fill is None else (fill.rgba(),)


def paint_fill_swatch(painter: QPainter, rect: QRectF, fill: PaintFill | QColor) -> None:
    """Paint a thumbnail with a checkerboard beneath translucent fills.

    Gradients and hard bands share the render brush semantics. Image fills use
    an aspect-preserving thumbnail so a small swatch shows the actual texture.
    The caller supplies any rounded/circular clip and border.
    """
    painter.save()
    painter.setClipRect(rect, Qt.ClipOperation.IntersectClip)
    painter.fillRect(rect, QColor("#FFFFFF"))
    tile = 4
    for y in range(0, int(rect.height()) + tile, tile):
        for x in range(0, int(rect.width()) + tile, tile):
            if (x // tile + y // tile) % 2 == 0:
                painter.fillRect(QRectF(rect.left() + x, rect.top() + y, tile, tile), QColor("#D5D9E0"))
    if isinstance(fill, QColor):
        painter.fillRect(rect, fill)
    elif fill.mode == "image":
        signature = image_file_signature(fill.image_path)
        image = cached_fill_image(signature) if signature is not None else None
        if image is not None and not image.isNull():
            scale = max(rect.width() / image.width(), rect.height() / image.height())
            width, height = rect.width() / scale, rect.height() / scale
            source = QRectF((image.width() - width) / 2, (image.height() - height) / 2, width, height)
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            painter.drawImage(rect, image, source)
        else:
            # A missing texture must not masquerade as a saved solid color.
            painter.setPen(QPen(QColor("#7A8494"), 1))
            painter.drawLine(rect.topLeft(), rect.bottomRight())
            painter.drawLine(rect.topRight(), rect.bottomLeft())
    else:
        painter.fillRect(rect, brush_for_fill(fill, rect))
    painter.restore()


def fill_description(fill: PaintFill) -> str:
    if fill.mode == "image":
        suffix = "" if image_file_signature(fill.image_path) is not None else "（缺失）"
        return f"图片 · {Path(fill.image_path).name}{suffix}"
    if fill.mode == "solid":
        return fill.color.upper()
    stops = split_gradient_stops(fill) if fill.mode == "split_vertical" else gradient_stops(fill)
    label = {"gradient_horizontal": "横向渐变", "gradient_vertical": "纵向渐变", "split_vertical": "拼色"}[fill.mode]
    return label + " · " + " / ".join(f"{position:g}% {color.upper()}" for position, color in stops)


def scheme_color_tooltip(scheme: Style | SubtitleStyleScheme) -> str:
    before, after = scheme_text_fills(scheme)
    return f"走字前：{fill_description(before)}\n走字后：{fill_description(after)}"


def scheme_swatch_color(style: Style, role: str) -> QColor | None:
    """A center sample for background tints; icons use the complete fill."""
    fill = scheme_swatch_fill(style, role)
    if fill is None:
        return None
    if fill.mode == "solid":
        return QColor(fill.color)
    key = swatch_key(fill)
    if key in _TINT_CACHE:
        _TINT_CACHE.move_to_end(key)
        return QColor(_TINT_CACHE[key])
    pixmap = QPixmap(16, 16)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    paint_fill_swatch(painter, QRectF(0, 0, 16, 16), fill)
    painter.end()
    color = pixmap.toImage().pixelColor(8, 8)
    _TINT_CACHE[key] = color
    while len(_TINT_CACHE) > 128:
        _TINT_CACHE.popitem(last=False)
    return QColor(color)


def role_swatch_icon(color: PaintFill | QColor | None) -> QIcon:
    """A circular swatch; undefined schemes retain an outlined neutral ring."""
    pixmap = QPixmap(24, 24)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    if color is None:
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(148, 163, 184, 200), 2))
    else:
        painter.save()
        path = QPainterPath()
        path.addEllipse(QRectF(5, 5, 14, 14))
        painter.setClipPath(path)
        paint_fill_swatch(painter, QRectF(5, 5, 14, 14), color)
        painter.restore()
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(0, 0, 0, 80), 1.5))
    painter.drawEllipse(5, 5, 14, 14)
    painter.end()
    return swatch_icon_from_pixmap(pixmap)
