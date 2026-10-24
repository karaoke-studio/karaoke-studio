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
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QRect, QRectF, Qt
from PyQt6.QtGui import (
    QBrush,
    QColor,
    QImage,
    QImageReader,
    QPainter,
    QTransform,
)

from krok_helper.subtitle_render.domain.band_presets import (
    default_clef_color_for,
    default_line_color_for,
    resolve_band_clef_image,
    resolve_band_image,
    resolve_band_lines_image,
)

_TILE_CACHE_MAX = 16
_TILE_CACHE: "OrderedDict[tuple, QImage]" = OrderedDict()
_STRIP_CACHE: "OrderedDict[tuple, QImage]" = OrderedDict()
_IMAGE_CACHE_MAX = 16
_IMAGE_CACHE: "OrderedDict[tuple, QImage]" = OrderedDict()
_RASTER_CACHE: "OrderedDict[tuple, QImage]" = OrderedDict()
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
    # 独立谱线层 / 谱号层（如五线谱）：路径 + 各自上色颜色。
    line_image_path: str = ""
    line_color: str = ""
    clef_image_path: str = ""
    clef_color: str = ""
    base_image_path: str = ""
    # 图层效果（PS 图层样式口径）：每一种都有独立开关与配置，可以同时开。
    shadow: Optional["BandEffect"] = None
    stroke: Optional["BandEffect"] = None
    glow: Optional["BandEffect"] = None


@dataclass(frozen=True)
class BandEffect:
    """一种图层效果：作用于合成后的整张带子图（含 alpha）。"""

    kind: str
    color: str = "#000000"
    size_px: int = 0
    offset_x_px: int = 0
    offset_y_px: int = 0
    spread_pct: int = 0
    range_pct: int = 50
    position: str = "outside"


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
    clef_path = resolve_band_clef_image(image_spec)
    clef_color = str(getattr(style, "line_band_clef_color", "") or "")
    if not clef_color:
        clef_color = default_clef_color_for(image_spec)
    cap_left = max(int(style.line_band_cap_left_px), 0)
    cap_right = max(int(style.line_band_cap_right_px), 0)
    # 三段式切点 = 用户端点：0/1/2 三份按滑条切，切掉的部分会出现在中段
    # 开头（用户 2026-10-24 口径：故意把音符切进中段时要能看到它）。
    src_cap_left, src_cap_right = cap_left, cap_right

    def effect(prefix: str, *, offsets: bool = False) -> Optional[BandEffect]:
        distance = float(getattr(style, "line_band_shadow_distance_px", 3))
        angle = float(getattr(style, "line_band_shadow_angle_deg", 135))
        # 独立开关 + 尺寸：开关关闭或尺寸为 0 都等于不画（用户口径 2026-10-24）。
        if not bool(getattr(style, f"line_band_{prefix}_enabled", False)):
            return None
        size = max(int(getattr(style, f"line_band_{prefix}_size_px", 0)), 0)
        if size <= 0:
            return None
        return BandEffect(
            kind=prefix,
            spread_pct=int(getattr(style, f"line_band_{prefix}_spread_pct", 0)),
            range_pct=int(getattr(style, "line_band_glow_range_pct", 50)),
            position=str(getattr(style, "line_band_stroke_position", "outside")),
            color=str(getattr(style, f"line_band_{prefix}_color", "") or "#000000"),
            size_px=size,
            offset_x_px=int(round(
                math.cos(math.radians(angle)) * distance
            )) if offsets else 0,
            offset_y_px=int(round(
                math.sin(math.radians(angle)) * distance
            )) if offsets else 0,
        )

    shadow = effect("shadow", offsets=True)
    stroke = effect("stroke")
    glow = effect("glow")
    if shadow is None and stroke is None and glow is None:
        # 兼容旧字段（直接构造 Style 的调用方 / 旧会话内存态）：只有一个
        # line_band_edge_kind 时按它合出一种效果。
        legacy = str(getattr(style, "line_band_edge_kind", "none") or "none")
        if legacy in {"shadow", "stroke", "glow"}:
            legacy_effect = BandEffect(
                kind=legacy,
                # 旧字段没有「扩展」，按旧行为把剪影先膨胀一个 size，
                # 保证发光/投影确实长到轮廓之外。
                spread_pct=100 if legacy in {"glow", "shadow"} else 0,
                color=str(
                    getattr(style, "line_band_edge_color", "#000000")
                    or "#000000"
                ),
                size_px=max(
                    int(getattr(style, "line_band_edge_size_px", 12)), 1
                ),
                offset_x_px=int(
                    getattr(style, "line_band_edge_offset_x_px", 8)
                ),
                offset_y_px=int(
                    getattr(style, "line_band_edge_offset_y_px", 8)
                ),
            )
            if legacy == "shadow":
                shadow = legacy_effect
            elif legacy == "stroke":
                stroke = legacy_effect
            else:
                glow = legacy_effect
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
        clef_image_path=str(clef_path) if clef_path is not None else "",
        clef_color=clef_color if clef_path is not None else "",
        shadow=shadow,
        stroke=stroke,
        glow=glow,
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


# 动图行背景（GIF/动画 WebP）：帧上限与最小帧时长必须与 sidecar 一致，
# 否则预览与导出的选帧会来回跳。
BAND_ANIM_MAX_FRAMES = 4096   # 仅防损坏文件的保险丝：用户 GIF 必须完整保留
BAND_ANIM_MIN_FRAME_MS = 10
_ANIMATED_BAND_CACHE_MAX = 4
_ANIMATED_BAND_CACHE: "OrderedDict[tuple, Optional[tuple]]" = OrderedDict()


def animated_band_frames(
    path: str,
) -> Optional[tuple[tuple[QImage, ...], tuple[int, ...]]]:
    """解码动图行背景的全部帧（上限 ``BAND_ANIM_MAX_FRAMES``）。

    返回 ``(frames, delays_ms)``；静态图或解码失败返回 ``None``（并记住
    「不是动图」，避免每帧都重新问一次 ``imageCount``）。
    """
    if not path:
        return None
    try:
        stat = Path(path).stat()
    except OSError:
        return None
    key = (str(path), int(stat.st_mtime_ns), int(stat.st_size))
    cached = _ANIMATED_BAND_CACHE.get(key)
    if cached is not None or key in _ANIMATED_BAND_CACHE:
        _ANIMATED_BAND_CACHE.move_to_end(key)
        return cached
    frames: list[QImage] = []
    delays: list[int] = []
    try:
        reader = QImageReader(path)
        count = int(reader.imageCount())
        if count > 1:
            # 不截断用户 GIF：帧数完整保留 → 总时长/循环点与文件一致。
            # 上限只用于拦损坏文件（声明几万帧的那种）。
            limit = min(count, BAND_ANIM_MAX_FRAMES)
            while len(frames) < limit:
                image = reader.read()
                if image.isNull():
                    break
                frames.append(
                    image.convertToFormat(
                        QImage.Format.Format_ARGB32_Premultiplied
                    )
                )
                delays.append(
                    max(int(reader.nextImageDelay() or 0), BAND_ANIM_MIN_FRAME_MS)
                )
    except Exception:
        frames = []
    result = None
    if len(frames) > 1:
        result = (tuple(frames), tuple(delays))
    _ANIMATED_BAND_CACHE[key] = result
    while len(_ANIMATED_BAND_CACHE) > _ANIMATED_BAND_CACHE_MAX:
        _ANIMATED_BAND_CACHE.popitem(last=False)
    return result


def band_frame_index(delays_ms: tuple[int, ...], t_ms: int) -> int:
    """按累积延时表循环取帧下标（与 sidecar 的 WIC 选帧同契约）。"""
    if not delays_ms:
        return 0
    total = sum(delays_ms)
    if total <= 0:
        return 0
    position = max(int(t_ms), 0) % total
    cumulative = 0
    for index, delay in enumerate(delays_ms):
        cumulative += delay
        if position < cumulative:
            return index
    return len(delays_ms) - 1


def _band_layers(
    spec: BandSpec, frame_index: int = 0
) -> tuple[Optional[QImage], Optional[QImage]]:
    """加载素材本体（已上色）与谱线层（已按谱线色 multiply），带缓存。

    ``frame_index`` 对动图（GIF）生效：取对应帧作为素材本体。
    """
    path = Path(spec.image_path)
    try:
        path.stat()
    except OSError:
        return None, None
    animated = animated_band_frames(str(path))
    if animated is not None:
        frames, _delays = animated
        if 0 <= frame_index < len(frames):
            base = frames[frame_index].copy()
        else:
            base = frames[0].copy()
    else:
        base = QImage(str(path))
    if base.isNull():
        return None, None
    strength = spec.tint_strength_pct if spec.tint else 0
    if spec.tint and strength > 0:
        base = _tinted(base, spec.tint, strength)
    lines = _line_source(spec)
    return base, lines


def band_source(spec: BandSpec, t_ms: int = 0) -> Optional[QImage]:
    """整条带子的完整图案（缓存）：素材本体 + 谱线层合成一张。

    投影/发光按「整张图作为整体」计算，所以合成必须发生在描边之前——调用
    方拿到的就是最终会画到画面上的那一张。
    """
    path = Path(spec.image_path)
    try:
        stat = path.stat()
    except OSError:
        return None
    lines_path = Path(spec.line_image_path) if spec.line_image_path else None
    lines_key = None
    if lines_path is not None:
        try:
            lines_stat = lines_path.stat()
        except OSError:
            lines_path = None
        else:
            lines_key = (str(lines_path), int(lines_stat.st_mtime_ns),
                         int(lines_stat.st_size))
    strength = spec.tint_strength_pct if spec.tint else 0
    animated = animated_band_frames(str(path))
    frame_index = 0
    if animated is not None:
        frame_index = band_frame_index(animated[1], t_ms)
    key = (str(path), int(stat.st_mtime_ns), int(stat.st_size),
           spec.tint, strength, lines_key,
           spec.line_color if lines_path is not None else "",
           frame_index)
    cached = _IMAGE_CACHE.get(key)
    if cached is not None:
        _IMAGE_CACHE.move_to_end(key)
        return cached

    base, lines = _band_layers(spec, frame_index)
    if base is None:
        return None
    clef = _clef_source(spec)
    if lines is not None or clef is not None:
        composite = QImage(
            base.size(), QImage.Format.Format_ARGB32_Premultiplied
        )
        composite.fill(0)
        painter = QPainter(composite)
        try:
            # 图层顺序：谱线 → 谱号/升号 → 素材本体（音符在上）。
            if lines is not None:
                painter.drawImage(0, 0, lines)
            if clef is not None:
                painter.drawImage(0, 0, clef)
            painter.drawImage(0, 0, base.convertToFormat(
                QImage.Format.Format_ARGB32_Premultiplied
            ))
        finally:
            painter.end()
        base = composite
    _IMAGE_CACHE[key] = base
    while len(_IMAGE_CACHE) > _IMAGE_CACHE_MAX:
        _IMAGE_CACHE.popitem(last=False)
    return base


def band_artwork(spec: BandSpec) -> Optional[QImage]:
    """切割检查用的「原图」：素材本体不上色 + 谱线层按当前谱线色合成。

    属性卡预览要看到切割前的图案本身（上色/描边是效果，会掩盖端点边界），
    所以这里不复用 `band_source`（它带 tint）。
    """
    path = Path(spec.image_path)
    try:
        stat = path.stat()
    except OSError:
        return None
    lines_path = Path(spec.line_image_path) if spec.line_image_path else None
    lines_key = None
    if lines_path is not None:
        try:
            lines_stat = lines_path.stat()
        except OSError:
            lines_path = None
        else:
            lines_key = (str(lines_path), int(lines_stat.st_mtime_ns),
                         int(lines_stat.st_size))
    key = ("artwork", str(path), int(stat.st_mtime_ns), int(stat.st_size),
           lines_key,
           spec.line_color if lines_path is not None else "")
    cached = _IMAGE_CACHE.get(key)
    if cached is not None:
        _IMAGE_CACHE.move_to_end(key)
        return cached
    base = QImage(str(path))
    if base.isNull():
        return None
    if lines_path is not None:
        lines = _multiplied(
            QImage(str(lines_path)), spec.line_color or "#000000"
        )
        composite = QImage(
            base.size(), QImage.Format.Format_ARGB32_Premultiplied
        )
        composite.fill(0)
        painter = QPainter(composite)
        try:
            # 图层顺序：谱线 → 谱号/升号 → 素材本体。
            painter.drawImage(0, 0, lines)
            clef_art = _clef_source(spec)
            if clef_art is not None:
                painter.drawImage(0, 0, clef_art)
            painter.drawImage(0, 0, base.convertToFormat(
                QImage.Format.Format_ARGB32_Premultiplied
            ))
        finally:
            painter.end()
        base = composite
    _IMAGE_CACHE[key] = base
    while len(_IMAGE_CACHE) > _IMAGE_CACHE_MAX:
        _IMAGE_CACHE.popitem(last=False)
    return base


def clear_band_image_cache() -> None:
    _IMAGE_CACHE.clear()
    _TILE_CACHE.clear()
    _STRIP_CACHE.clear()
    _EDGE_CACHE.clear()
    _RASTER_CACHE.clear()
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


def paint_band(
    painter: QPainter, rect: QRectF, spec: BandSpec, t_ms: int = 0
) -> None:
    """Effects consume the displayed current-frame alpha, after all layout modes."""
    image = band_source(spec, t_ms)
    if image is None or rect.width() <= 0 or rect.height() <= 0:
        return
    painter.save()
    try:
        painter.setOpacity(painter.opacity() * spec.opacity_pct / 100.0)
        effects = [effect for effect in (spec.shadow, spec.glow, spec.stroke)
                   if effect is not None]
        if not effects:
            _paint_mode(painter, rect, image, spec)
            return
        scale = rect.height() / max(float(image.height()), 1.0)
        key = (int(image.cacheKey()), rect.width(), rect.height(), spec.fill_mode,
               spec.cap_left, spec.cap_right, spec.src_cap_left, spec.src_cap_right,
               spec.mid_mode)
        raster = _RASTER_CACHE.get(key)
        if raster is None:
            raster = QImage(math.ceil(rect.width()), math.ceil(rect.height()),
                            QImage.Format.Format_ARGB32_Premultiplied)
            raster.fill(0)
            layer = QPainter(raster)
            try:
                _paint_mode(layer, QRectF(0, 0, rect.width(), rect.height()), image, spec)
            finally:
                layer.end()
            _RASTER_CACHE[key] = raster
            while len(_RASTER_CACHE) > 8:
                _RASTER_CACHE.popitem(last=False)
        _RASTER_CACHE.move_to_end(key)
        for above in (False, True):
            if above:
                painter.drawImage(rect.topLeft(), raster)
            for effect in effects:
                over = effect.kind == "stroke" and effect.position != "outside"
                if above != over:
                    continue
                scaled = replace(effect, size_px=max(1, round(effect.size_px * scale)))
                result = _edge_source(spec, raster, scaled)
                if result is None:
                    continue
                edge, pad = result
                dx = effect.offset_x_px if effect.kind == "shadow" else 0
                dy = effect.offset_y_px if effect.kind == "shadow" else 0
                painter.drawImage(QRectF(rect.left() - pad + dx, rect.top() - pad + dy,
                                        edge.width(), edge.height()), edge)
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


def _qimage_alpha(image: QImage):
    """QImage → numpy alpha 平面（0..255）。"""
    import numpy as np

    width, height = image.width(), image.height()
    bits = image.constBits()
    bits.setsize(image.sizeInBytes())
    array = np.frombuffer(bytes(bits), dtype=np.uint8).reshape(
        height, image.bytesPerLine() // 4, 4
    )[:, :width, :]
    return array[:, :, 3].copy()


def _alpha_to_image(alpha, color: str) -> QImage:
    """alpha 平面 + 颜色 → 预乘 ARGB32 的 QImage。"""
    import numpy as np

    height, width = alpha.shape
    rgba = np.zeros((height, width, 4), np.uint8)
    rgba[:, :, 0] = QColor(color).blue()
    rgba[:, :, 1] = QColor(color).green()
    rgba[:, :, 2] = QColor(color).red()
    rgba[:, :, 3] = (alpha.astype("float32") * QColor(color).alphaF()).round().astype("uint8")
    buffer = np.ascontiguousarray(rgba)
    image = QImage(
        buffer.data, width, height, width * 4,
        QImage.Format.Format_ARGB32,
    ).copy()
    return image.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)


def _edge_source(
    spec: BandSpec, image: QImage, effect: "BandEffect"
) -> Optional[tuple[QImage, int]]:
    """按 PS 图层样式的标准算法生成效果位图。

    * 剪影：当前显示图的原始 alpha，保留半透明边缘；
    * 扩展 spread：先做形态学膨胀（`spread% × size`），再做模糊/描边；
    * 阴影/发光：真高斯模糊（`ImageFilter.GaussianBlur`，σ≈size/2）；
    * 描边：不模糊，按位置（外/中/内）膨胀/收缩；
    * 发光 range：控制衰减曲线（PS「范围」）。
    """
    from PIL import Image, ImageFilter

    size = max(int(effect.size_px), 0)
    if size <= 0:
        return None
    spread_px = int(round(size * max(min(effect.spread_pct, 100), 0) / 100.0))
    key = (
        int(image.cacheKey()),
        effect.kind,
        effect.color,
        size,
        spread_px,
        int(effect.range_pct),
        effect.position,
    )
    cached = _EDGE_CACHE.get(key)
    if cached is not None:
        _EDGE_CACHE.move_to_end(key)
        return cached

    alpha = _qimage_alpha(image)
    # pad：模糊/膨胀都要长到轮廓之外
    pad = size * 3 if effect.kind in {"glow", "shadow"} else size + spread_px
    if pad > 0:
        import numpy as np

        alpha = np.pad(alpha, pad, mode="constant", constant_values=0)
    plane = Image.fromarray(alpha)
    if spread_px > 0:
        # 形态学膨胀：MaxFilter 的窗口必须是奇数
        plane = plane.filter(
            ImageFilter.MaxFilter(2 * spread_px + 1)
        )
    if effect.kind in {"glow", "shadow"}:
        plane = plane.filter(ImageFilter.GaussianBlur(max(size * 0.7, 0.6)))
    elif effect.kind == "stroke":
        # 外侧：膨胀后直接盖在带子下面，露出的就是外圈；
        # 居中/内侧：先只保留「原轮廓之外/之内」的部分。
        import numpy as np

        if effect.position == "center":
            half = max(size // 2, 1)
            outer = np.asarray(plane.filter(ImageFilter.MaxFilter(2 * half + 1)), np.int16)
            inner = np.asarray(plane.filter(ImageFilter.MinFilter(2 * half + 1)), np.int16)
            plane = Image.fromarray(np.clip(outer - inner, 0, 255).astype(np.uint8))
        elif effect.position == "outside":
            plane = plane.filter(ImageFilter.MaxFilter(2 * size + 1))
        elif effect.position == "inside":
            # 内描边：原轮廓 - 腐蚀(原轮廓)，即轮廓内侧一圈
            solid = Image.fromarray(alpha)
            if size > 0:
                eroded = solid.filter(ImageFilter.MinFilter(2 * size + 1))
            else:
                eroded = solid
            ring = np.clip(
                np.asarray(solid, np.int16) - np.asarray(eroded, np.int16),
                0, 255,
            ).astype(np.uint8)
            plane = Image.fromarray(ring)
    result_alpha = np.asarray(plane, dtype=np.uint8)
    if effect.kind == "glow" and effect.range_pct != 50:
        import numpy as np

        gamma = 50.0 / max(min(effect.range_pct, 100), 1)
        curve = [round(255 * (v / 255.0) ** gamma) for v in range(256)]
        result_alpha = np.asarray(plane.point(curve), dtype=np.uint8)
    out = _alpha_to_image(result_alpha, effect.color)
    _EDGE_CACHE[key] = (out, pad)
    while len(_EDGE_CACHE) > _EDGE_CACHE_MAX:
        _EDGE_CACHE.popitem(last=False)
    return out, pad


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


def _clef_source(spec: BandSpec) -> Optional[QImage]:
    """独立谱号层（含升降号）：白色素材 multiply 成谱号颜色。"""
    path = spec.clef_image_path
    if not path:
        return None
    color = spec.clef_color or "#FFFFFF"
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
    if spec.mid_mode == "cover":
        # 中段也用裁切（覆盖）：中段源图等比覆盖整段，居中裁掉溢出。
        cover_scale = max(
            mid_dest.width() / max(mid_src.width(), 1.0),
            mid_dest.height() / max(mid_src.height(), 1.0),
        )
        cover_w = min(mid_dest.width() / cover_scale, mid_src.width())
        cover_h = min(mid_dest.height() / cover_scale, mid_src.height())
        _draw_slice(
            painter,
            mid_dest,
            image,
            QRectF(
                mid_src.left() + (mid_src.width() - cover_w) / 2.0,
                mid_src.top() + (mid_src.height() - cover_h) / 2.0,
                cover_w,
                cover_h,
            ),
        )
        return
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
    "BandEffect",
    "BandSpec",
    "band_rect",
    "band_artwork",
    "band_source",
    "band_spec",
    "clear_band_image_cache",
    "paint_band",
]
