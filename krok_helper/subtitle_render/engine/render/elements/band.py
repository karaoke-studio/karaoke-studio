"""Per-line background strip (行背景带) rendering for the QPainter backend.

The band is a purely decorative overlay: geometry comes from the line's text
box plus the four style paddings, and it never feeds back into layout.

Four fill modes are supported (mirrors CSS ``border-image-repeat``):

``stretch``      非等比铺满
``repeat``       等比贴合带高、横向平铺
``cover``        等比覆盖、居中裁切
``three_slice``  左右端点不缩放内容、中段按 ``mid_mode`` 平铺或拉伸
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import QColor, QImage, QPainter

from krok_helper.subtitle_render.domain.band_presets import resolve_band_image

_IMAGE_CACHE_MAX = 16
_IMAGE_CACHE: "OrderedDict[tuple, QImage]" = OrderedDict()


@dataclass(frozen=True)
class BandSpec:
    """Everything the painter needs to draw one band, resolved from ``Style``."""

    image_path: str
    fill_mode: str
    cap_left: int
    cap_right: int
    mid_mode: str
    tint: str
    tint_strength_pct: int
    opacity_pct: int


def band_spec(style) -> Optional[BandSpec]:
    """Build a drawable spec, or ``None`` when the band is off / unusable."""
    if not getattr(style, "line_band_enabled", False):
        return None
    path = resolve_band_image(style.line_band_image)
    if path is None:
        return None
    return BandSpec(
        image_path=str(path),
        fill_mode=str(style.line_band_fill_mode),
        cap_left=max(int(style.line_band_cap_left_px), 0),
        cap_right=max(int(style.line_band_cap_right_px), 0),
        mid_mode=str(style.line_band_mid_mode),
        tint=str(style.line_band_tint or ""),
        tint_strength_pct=max(min(int(style.line_band_tint_strength_pct), 100), 0),
        opacity_pct=max(min(int(style.line_band_opacity_pct), 100), 0),
    )


def band_rect(text_rect: QRectF, style, ruby_top: Optional[float] = None) -> QRectF:
    """Text box + four independent paddings -> the band rectangle."""
    top_ref = float(text_rect.top())
    if getattr(style, "line_band_include_ruby", False) and ruby_top is not None:
        top_ref = min(top_ref, float(ruby_top))
    offset_y = int(getattr(style, "line_band_offset_y_px", 0))
    left = float(text_rect.left()) - int(style.line_band_pad_left_px)
    right = float(text_rect.right()) + int(style.line_band_pad_right_px)
    top = top_ref - int(style.line_band_pad_top_px) + offset_y
    bottom = (
        float(text_rect.bottom())
        + int(style.line_band_pad_bottom_px)
        + offset_y
    )
    return QRectF(left, top, max(right - left, 1.0), max(bottom - top, 1.0))


def band_source(spec: BandSpec) -> Optional[QImage]:
    """Load (and cache) the band image with the user tint already applied."""
    path = Path(spec.image_path)
    try:
        stat = path.stat()
    except OSError:
        return None
    strength = spec.tint_strength_pct if spec.tint else 0
    key = (str(path), int(stat.st_mtime_ns), int(stat.st_size),
           spec.tint, strength)
    cached = _IMAGE_CACHE.get(key)
    if cached is not None:
        _IMAGE_CACHE.move_to_end(key)
        return cached

    image = QImage(str(path))
    if image.isNull():
        return None
    if spec.tint and strength > 0:
        image = _tinted(image, spec.tint, strength)
    _IMAGE_CACHE[key] = image
    while len(_IMAGE_CACHE) > _IMAGE_CACHE_MAX:
        _IMAGE_CACHE.popitem(last=False)
    return image


def clear_band_image_cache() -> None:
    _IMAGE_CACHE.clear()


def _tinted(image: QImage, color: str, strength_pct: int) -> QImage:
    """Tint an image toward a flat colour while keeping its alpha.

    强度 100% 时等于把素材压成单色剪影（保留原始透明边缘）；部分强度按
    ``C_out = (1-k)C + k*tint`` 向目标色混合，alpha 始终不变——与 native
    sidecar 在解码期的染色公式逐像素一致。
    """
    tinted = image.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
    base = QColor(color)
    if not base.isValid():
        return image
    painter = QPainter(tinted)
    try:
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
        painter.fillRect(tinted.rect(), base)
        strength = max(0, min(int(strength_pct), 100))
        if strength < 100:
            painter.setCompositionMode(
                QPainter.CompositionMode.CompositionMode_SourceOver
            )
            painter.setOpacity(1.0 - strength / 100.0)
            painter.drawImage(0, 0, image)
    finally:
        painter.end()
    return tinted


def paint_band(painter: QPainter, rect: QRectF, spec: BandSpec) -> None:
    """Draw one band into ``rect`` (already in line-local coordinates)."""
    image = band_source(spec)
    if image is None or rect.width() <= 0 or rect.height() <= 0:
        return
    painter.save()
    try:
        painter.setOpacity(painter.opacity() * spec.opacity_pct / 100.0)
        if spec.fill_mode == "three_slice":
            _paint_three_slice(painter, rect, image, spec)
        elif spec.fill_mode == "repeat":
            _paint_repeat(painter, rect, image)
        elif spec.fill_mode == "cover":
            _paint_cover(painter, rect, image)
        else:
            painter.drawImage(rect, image, QRectF(image.rect()))
    finally:
        painter.restore()


def _draw_slice(painter, dest: QRectF, image: QImage, src: QRectF) -> None:
    if dest.width() <= 0 or dest.height() <= 0 or src.width() <= 0:
        return
    painter.drawImage(dest, image, src)


def _paint_three_slice(painter, rect: QRectF, image: QImage, spec: BandSpec) -> None:
    img_h = float(image.height())
    img_w = float(image.width())
    scale = rect.height() / max(img_h, 1.0)
    left_w = spec.cap_left * scale
    right_w = spec.cap_right * scale
    if left_w + right_w > rect.width():
        # 带子比两端还窄：等比压缩端点，中段宽度归零（不溢出、不报错）。
        scale = rect.width() / max(spec.cap_left + spec.cap_right, 1)
        left_w = spec.cap_left * scale
        right_w = spec.cap_right * scale
    mid_w = max(rect.width() - left_w - right_w, 0.0)

    if spec.cap_left > 0:
        _draw_slice(
            painter,
            QRectF(rect.left(), rect.top(), left_w, rect.height()),
            image,
            QRectF(0.0, 0.0, float(spec.cap_left), img_h),
        )
    if spec.cap_right > 0:
        _draw_slice(
            painter,
            QRectF(rect.right() - right_w, rect.top(), right_w, rect.height()),
            image,
            QRectF(img_w - spec.cap_right, 0.0, float(spec.cap_right), img_h),
        )
    if mid_w <= 0 or img_w <= spec.cap_left + spec.cap_right:
        return

    mid_src = QRectF(
        float(spec.cap_left), 0.0,
        img_w - spec.cap_left - spec.cap_right, img_h,
    )
    mid_dest = QRectF(rect.left() + left_w, rect.top(), mid_w, rect.height())
    if spec.mid_mode == "stretch":
        _draw_slice(painter, mid_dest, image, mid_src)
        return

    tile_w = mid_src.width() * scale
    if tile_w <= 0.5:
        _draw_slice(painter, mid_dest, image, mid_src)
        return
    x = mid_dest.left()
    while x < mid_dest.right() - 0.5:
        width = min(tile_w, mid_dest.right() - x)
        _draw_slice(
            painter,
            QRectF(x, rect.top(), width, rect.height()),
            image,
            QRectF(mid_src.left(), 0.0, width / scale, img_h),
        )
        x += tile_w


def _paint_repeat(painter, rect: QRectF, image: QImage) -> None:
    scale = rect.height() / max(float(image.height()), 1.0)
    tile_w = image.width() * scale
    if tile_w <= 0.5:
        painter.drawImage(rect, image, QRectF(image.rect()))
        return
    x = rect.left()
    while x < rect.right() - 0.5:
        width = min(tile_w, rect.right() - x)
        _draw_slice(
            painter,
            QRectF(x, rect.top(), width, rect.height()),
            image,
            QRectF(0.0, 0.0, width / scale, float(image.height())),
        )
        x += tile_w


def _paint_cover(painter, rect: QRectF, image: QImage) -> None:
    img_w = float(image.width())
    img_h = float(image.height())
    scale = max(rect.width() / max(img_w, 1.0), rect.height() / max(img_h, 1.0))
    src_w = min(rect.width() / scale, img_w)
    src_h = min(rect.height() / scale, img_h)
    src = QRectF(
        (img_w - src_w) / 2.0,
        (img_h - src_h) / 2.0,
        src_w,
        src_h,
    )
    _draw_slice(painter, rect, image, src)


__all__ = [
    "BandSpec",
    "band_rect",
    "band_source",
    "band_spec",
    "clear_band_image_cache",
    "paint_band",
]
