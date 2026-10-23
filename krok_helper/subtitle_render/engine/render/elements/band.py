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

import math
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QRect, QRectF, Qt
from PyQt6.QtGui import QBrush, QColor, QImage, QPainter, QTransform

from krok_helper.subtitle_render.domain.band_presets import (
    default_line_color_for,
    preset_caps,
    resolve_band_image,
    resolve_band_lines_image,
)

_TILE_CACHE_MAX = 16
_TILE_CACHE: "OrderedDict[tuple, QImage]" = OrderedDict()
_STRIP_CACHE: "OrderedDict[tuple, QImage]" = OrderedDict()
_IMAGE_CACHE_MAX = 16
_IMAGE_CACHE: "OrderedDict[tuple, QImage]" = OrderedDict()
_EDGE_CACHE_MAX = 8
_EDGE_CACHE: "OrderedDict[tuple, QImage]" = OrderedDict()
_LINE_CACHE_MAX = 8
_LINE_CACHE: "OrderedDict[tuple, QImage]" = OrderedDict()


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
    # 源图裁切边界（预设素材的"设计端点"）：中段永远从 ``src_cap_left`` 到
    # ``width - src_cap_right``，与用户拖动的显示端点解耦。显示端点只决定
    # 端点显示多宽，不会让中段把谱号等端点图案再铺一遍。自定义图等于 0。
    src_cap_left: int = 0
    src_cap_right: int = 0
    # 独立谱线层（如五线谱）：路径 + 上色颜色。
    line_image_path: str = ""
    line_color: str = ""
    # 图片非透明部分的投影/发光。
    edge_kind: str = "none"
    edge_color: str = "#000000"
    edge_size_px: int = 12
    edge_offset_x_px: int = 8
    edge_offset_y_px: int = 8
    edge_opacity_pct: int = 70


def band_spec(style) -> Optional[BandSpec]:
    """Build a drawable spec, or ``None`` when the band is off / unusable."""
    if not getattr(style, "line_band_enabled", False):
        return None
    image_spec = str(style.line_band_image or "")
    path = resolve_band_image(image_spec)
    if path is None:
        return None
    lines_path = resolve_band_lines_image(image_spec)
    line_color = str(getattr(style, "line_band_line_color", "") or "")
    if not line_color:
        line_color = default_line_color_for(image_spec)
    cap_left = max(int(style.line_band_cap_left_px), 0)
    cap_right = max(int(style.line_band_cap_right_px), 0)
    # 三段式：0/2 直接画在带子左右两端，中间 1 填缝。**切点与显示端点分开**：
    # 预设素材有它的设计端点（谱号占 0..190），中段永远从设计端点开始切；
    # 端点滑条只把端点裁窄，不会把谱号喂进中段再铺一遍（用户 2026-10-24 实测
    # 截图：端点切进谱号时，左侧谱号出现两遍）。自定义图没有设计端点 → 相同。
    caps = preset_caps(image_spec)
    src_cap_left, src_cap_right = (
        (max(int(caps[0]), 0), max(int(caps[1]), 0))
        if caps is not None
        else (cap_left, cap_right)
    )
    return BandSpec(
        image_path=str(path),
        fill_mode=str(style.line_band_fill_mode),
        cap_left=cap_left,
        cap_right=cap_right,
        mid_mode=str(style.line_band_mid_mode),
        tint=str(style.line_band_tint or ""),
        tint_strength_pct=max(min(int(style.line_band_tint_strength_pct), 100), 0),
        opacity_pct=max(min(int(style.line_band_opacity_pct), 100), 0),
        src_cap_left=src_cap_left,
        src_cap_right=src_cap_right,
        line_image_path=str(lines_path) if lines_path is not None else "",
        line_color=line_color if lines_path is not None else "",
        edge_kind=str(getattr(style, "line_band_edge_kind", "none") or "none"),
        edge_color=str(getattr(style, "line_band_edge_color", "#000000") or "#000000"),
        edge_size_px=max(int(getattr(style, "line_band_edge_size_px", 12)), 0),
        edge_offset_x_px=int(getattr(style, "line_band_edge_offset_x_px", 8)),
        edge_offset_y_px=int(getattr(style, "line_band_edge_offset_y_px", 8)),
        edge_opacity_pct=max(
            min(int(getattr(style, "line_band_edge_opacity_pct", 70)), 100), 0
        ),
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
    _TILE_CACHE.clear()
    _STRIP_CACHE.clear()
    _EDGE_CACHE.clear()
    _LINE_CACHE.clear()


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
        if spec.edge_kind in {"shadow", "glow"}:
            edge = _edge_source(spec, image)
            if edge is not None:
                base_opacity = painter.opacity()
                if spec.edge_kind == "shadow":
                    painter.save()
                    try:
                        painter.setOpacity(
                            base_opacity * spec.edge_opacity_pct / 100.0
                        )
                        painter.translate(
                            spec.edge_offset_x_px, spec.edge_offset_y_px
                        )
                        _paint_mode(painter, rect, edge, spec)
                    finally:
                        painter.restore()
                else:
                    # 发光要长到带子轮廓之外：剪影绕圆周多次轻叠（模糊后的
                    # 剪影本身不会越出画布，靠偏移把它扩出去）。
                    radius = max(float(spec.edge_size_px), 1.0)
                    steps = 16 if radius >= 8.0 else 8
                    per_step = (
                        spec.edge_opacity_pct / 100.0 / float(steps) * 2.0
                    )
                    for index in range(steps):
                        angle = math.tau * index / float(steps)
                        painter.save()
                        try:
                            painter.setOpacity(
                                min(base_opacity * per_step, 1.0)
                            )
                            painter.translate(
                                math.cos(angle) * radius,
                                math.sin(angle) * radius,
                            )
                            _paint_mode(painter, rect, edge, spec)
                        finally:
                            painter.restore()
        # 谱线层（含高音谱号）在下，音符/终止线在上：谱线不得压在谱号上，
        # 谱号用自己的描边和谱线分开。
        lines = _line_source(spec)
        if lines is not None:
            _paint_mode(painter, rect, lines, spec)
        _paint_mode(painter, rect, image, spec)
    finally:
        painter.restore()


def _paint_mode(
    painter: QPainter, rect: QRectF, image: QImage, spec: BandSpec
) -> None:
    if spec.fill_mode == "three_slice":
        _paint_three_slice(painter, rect, image, spec)
    elif spec.fill_mode == "repeat":
        _paint_repeat(painter, rect, image)
    elif spec.fill_mode == "cover":
        _paint_cover(painter, rect, image)
    else:
        painter.drawImage(rect, image, QRectF(image.rect()))


def _edge_source(spec: BandSpec, image: QImage) -> Optional[QImage]:
    """把素材 alpha 压成单色剪影；``glow``/``shadow`` 再按半径做降采样模糊。"""
    key = (
        int(image.cacheKey()),
        spec.edge_kind,
        spec.edge_color,
        int(spec.edge_size_px),
    )
    cached = _EDGE_CACHE.get(key)
    if cached is not None:
        _EDGE_CACHE.move_to_end(key)
        return cached
    silhouette = QImage(image.size(), QImage.Format.Format_ARGB32_Premultiplied)
    silhouette.fill(0)
    painter = QPainter(silhouette)
    try:
        painter.drawImage(0, 0, image)
        painter.setCompositionMode(
            QPainter.CompositionMode.CompositionMode_SourceIn
        )
        color = QColor(spec.edge_color)
        if not color.isValid():
            color = QColor("#000000")
        painter.fillRect(silhouette.rect(), color)
    finally:
        painter.end()
    radius = max(int(spec.edge_size_px), 0)
    if radius > 0 and spec.edge_kind in {"glow", "shadow"}:
        # 降采样再放大 ≈ 可分离模糊；比逐像素卷积快得多，效果对发光足够。
        step = max(2, min(radius, max(image.width() // 4, 2)))
        for _ in range(2):
            small = silhouette.scaled(
                max(int(silhouette.width() / step), 1),
                max(int(silhouette.height() / step), 1),
                Qt.AspectRatioMode.IgnoreAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            silhouette = small.scaled(
                image.width(),
                image.height(),
                Qt.AspectRatioMode.IgnoreAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
    _EDGE_CACHE[key] = silhouette
    while len(_EDGE_CACHE) > _EDGE_CACHE_MAX:
        _EDGE_CACHE.popitem(last=False)
    return silhouette


def _line_source(spec: BandSpec) -> Optional[QImage]:
    """五线谱等预设的独立谱线层：**multiply** 上色。

    multiply 而不是压成单色剪影，是为了让同一层里的高音谱号保持自己的深色
    描边：白色部分被染成用户颜色，描边的近黑永远保持近黑（用户口径：谱线
    颜色要把高音谱号一并改掉，且谱号要有描边）。
    """
    path = spec.line_image_path
    if not path:
        return None
    color = spec.line_color or "#000000"
    try:
        stat = Path(path).stat()
    except OSError:
        return None
    key = (path, int(stat.st_mtime_ns), int(stat.st_size), color)
    cached = _LINE_CACHE.get(key)
    if cached is not None:
        _LINE_CACHE.move_to_end(key)
        return cached
    image = QImage(path)
    if image.isNull():
        return None
    image = _multiplied(image, color)
    _LINE_CACHE[key] = image
    while len(_LINE_CACHE) > _LINE_CACHE_MAX:
        _LINE_CACHE.popitem(last=False)
    return image


def _multiplied(image: QImage, color: str) -> QImage:
    """逐像素乘色（保留 alpha 与素材内部的明暗/描边）。"""
    try:
        import numpy as np
    except Exception:  # pragma: no cover - numpy 是应用依赖
        return image
    base = QColor(color)
    if not base.isValid():
        base = QColor("#000000")
    converted = image.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
    width, height = converted.width(), converted.height()
    bits = converted.constBits()
    bits.setsize(converted.sizeInBytes())
    pixels = np.frombuffer(bytes(bits), dtype=np.uint8).reshape(
        height, converted.bytesPerLine() // 4, 4
    )[:, :width, :].copy()
    for channel, value in (
        (0, base.blue()),
        (1, base.green()),
        (2, base.red()),
    ):
        pixels[:, :, channel] = (
            pixels[:, :, channel].astype(np.uint16) * value // 255
        ).astype(np.uint8)
    return QImage(
        pixels.tobytes(),
        width,
        height,
        width * 4,
        QImage.Format.Format_ARGB32_Premultiplied,
    ).copy()


def _mid_tile(image: QImage, cap_left: int, cap_right: int) -> QImage:
    """中段切片（按原图像素裁一次，重复绘制复用同一张）。"""
    key = (int(image.cacheKey()), int(cap_left), int(cap_right))
    cached = _TILE_CACHE.get(key)
    if cached is not None:
        _TILE_CACHE.move_to_end(key)
        return cached
    width = max(int(image.width()) - int(cap_left) - int(cap_right), 1)
    tile = image.copy(QRect(int(cap_left), 0, width, int(image.height())))
    _TILE_CACHE[key] = tile
    while len(_TILE_CACHE) > _TILE_CACHE_MAX:
        _TILE_CACHE.popitem(last=False)
    return tile


def _mid_strip(tile: QImage, needed_width: int) -> QImage:
    """把中段瓦片按整数像素拼成一条长条（按需要的块数缓存）。"""
    tile_w = max(int(tile.width()), 1)
    tiles = max(1, int(math.ceil(max(needed_width, 1) / tile_w)))
    key = (int(tile.cacheKey()), tiles)
    cached = _STRIP_CACHE.get(key)
    if cached is not None:
        _STRIP_CACHE.move_to_end(key)
        return cached
    strip = QImage(
        tiles * tile_w, tile.height(), QImage.Format.Format_ARGB32_Premultiplied
    )
    strip.fill(0)
    strip_painter = QPainter(strip)
    try:
        for index in range(tiles):
            strip_painter.drawImage(index * tile_w, 0, tile)
    finally:
        strip_painter.end()
    _STRIP_CACHE[key] = strip
    while len(_STRIP_CACHE) > _TILE_CACHE_MAX:
        _STRIP_CACHE.popitem(last=False)
    return strip


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
    # 中段按**源图设计边界**切，不按显示端点：显示端点调小只是把端点裁窄，
    # 不会把端点图案喂给中段（否则谱号会在中段开头再出现一遍）。
    src_left = min(max(int(spec.src_cap_left), 0), int(img_w))
    src_right = min(max(int(spec.src_cap_right), 0), int(img_w))
    if mid_w <= 0 or img_w <= src_left + src_right:
        return

    mid_src = QRectF(
        float(src_left), 0.0,
        img_w - src_left - src_right, img_h,
    )
    mid_dest = QRectF(rect.left() + left_w, rect.top(), mid_w, rect.height())
    if spec.mid_mode == "stretch":
        _draw_slice(painter, mid_dest, image, mid_src)
        return

    tile_w = mid_src.width() * scale
    if tile_w <= 0.5:
        _draw_slice(painter, mid_dest, image, mid_src)
        return
    # 中段一次取样预拼条：逐块 drawImage 会在每块边缘各留一条半像素竖缝
    # （用户实测「每个预设拼接痕迹都很明显」）。预拼条是同一张连续纹理，
    # 一次绘制取样，没有块边界。与 native 侧 8192px 预拼条同口径。
    needed = int(math.ceil(mid_dest.width() / max(scale, 1e-6)))
    strip = _mid_strip(_mid_tile(image, src_left, src_right), needed)
    _draw_slice(
        painter,
        mid_dest,
        strip,
        QRectF(0.0, 0.0, min(float(strip.width()), mid_dest.width() / max(scale, 1e-6)), float(strip.height())),
    )


def _paint_repeat(painter, rect: QRectF, image: QImage) -> None:
    scale = rect.height() / max(float(image.height()), 1.0)
    if image.width() * scale <= 0.5:
        painter.drawImage(rect, image, QRectF(image.rect()))
        return
    needed = int(math.ceil(rect.width() / max(scale, 1e-6)))
    strip = _mid_strip(image, needed)
    _draw_slice(
        painter,
        rect,
        strip,
        QRectF(
            0.0, 0.0,
            min(float(strip.width()), rect.width() / max(scale, 1e-6)),
            float(strip.height()),
        ),
    )


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
