"""自定义图片扫字线形态：素材解析、动图选帧与「取色层级」装饰。

第四种扫字线形态（``scanline_shape == "image"``）：把用户图片（静态或
GIF/动图 WebP，含 alpha）叠加在走字锋面上，默认锚点 = 锋面 x + 主文字
垂直中心，可用 1080 基准偏移微调。素材按**绝对路径**存盘；渲染期路径
失效时自动回退到工程（.yurika）所在目录按文件名查找，仍找不到则整个
图片层不画、扫字线回退普通高亮带。

「取色层级」（``scanline_image_color_layers``）与粒子的四档累进制不同，
是三个可任意组合的开关：``solid`` 实色上色 / ``stroke`` 描边 /
``decor`` 装饰（阴影/发光）。颜色与几何取自扫字线颜色方案解析出的配色
态（与普通高亮带同一套模式：默认/单独颜色/底色发光/跟随字体/复用配色
方案），渐变在图片锚点处采样成实色。动图的每个动画帧 alpha 不同，
上色/描边/装饰全部按帧生成（与行背景带的图层效果同算法同缓存口径）。

图片层是行级 overlay：不参与 utopia/整字放大逐字变换，也不计入布局
防重叠统计（碰撞带只量主文字字形本身）。
"""

from __future__ import annotations

import math
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QPointF, QRectF
from PyQt6.QtGui import QImage, QPainter

from krok_helper.subtitle_render.domain.models import (
    SCANLINE_GLOBAL_ROLE_KEY,
    Style,
)
from krok_helper.subtitle_render.domain.paint import (
    KaraokeColors,
    KaraokeColorState,
    PaintFill,
)
from krok_helper.subtitle_render.engine.render.elements.band import (
    BandEffect,
    BandSpec,
    _alpha_to_image,
    _edge_source,
    _qimage_alpha,
    _tinted,
    animated_band_frames,
    band_frame_index,
)
from krok_helper.subtitle_render.engine.render.effects.metrics import (
    decoration_kind_for_state,
    glow_concentration_level,
    shadow_offset_x,
    shadow_offset_y,
)
from krok_helper.subtitle_render.engine.render.effects.particles import (
    fill_to_solid_color,
)


# ---------------------------------------------------------------------------
# 工程目录回退：.yurika 所在目录。session 在工程打开/另存/身份采纳时登记。
_SEARCH_ROOTS: list[Path] = []

# 路径解析的短时记忆：高亮带闸门按「每字形每帧」查询（suppress_strip），
# 不能每次都真做 stat。0.5s 内同 (路径, 搜索根) 复用结果——素材被移动/
# 删除后的回退切换最多延迟半秒，画面不会闪。
_RESOLVE_MEMO_TTL_S = 0.5
_resolve_memo: dict[tuple, tuple[float, Optional[str]]] = {}


def _resolve_memo_key(stored: str) -> tuple:
    return (stored, tuple(str(root) for root in _SEARCH_ROOTS))


def set_scanline_image_search_roots(paths: list[Path] | None) -> None:
    """登记渲染期路径回退的搜索根（通常只有工程所在目录一个）。"""

    _SEARCH_ROOTS[:] = [Path(item) for item in (paths or []) if str(item)]
    _resolve_memo.clear()


def scanline_image_search_roots() -> list[Path]:
    return list(_SEARCH_ROOTS)


def resolve_scanline_image_path(stored: str) -> Optional[str]:
    """素材路径解析：绝对路径优先，失效时按工程目录 basename 回退。

    返回可用的绝对路径字符串；未设置或两级查找都失败返回 ``None``
    （调用方整体跳过图片层，扫字线回退普通高亮带）。结果按
    ``_RESOLVE_MEMO_TTL_S`` 短时记忆（见上）。
    """

    text = str(stored or "").strip()
    if not text:
        return None
    key = _resolve_memo_key(text)
    now = time.monotonic()
    cached = _resolve_memo.get(key)
    if cached is not None and now - cached[0] < _RESOLVE_MEMO_TTL_S:
        return cached[1]
    resolved = _resolve_scanline_image_path_uncached(text)
    if len(_resolve_memo) > 64:
        _resolve_memo.clear()
    _resolve_memo[key] = (now, resolved)
    return resolved


def _resolve_scanline_image_path_uncached(text: str) -> Optional[str]:
    try:
        direct = Path(text)
    except (OSError, ValueError):
        return None
    if direct.is_file():
        return str(direct)
    name = direct.name
    if not name:
        return None
    for root in _SEARCH_ROOTS:
        try:
            candidate = root / name
            if candidate.is_file():
                return str(candidate)
        except OSError:
            continue
    return None


def scanline_image_available(stored: str) -> bool:
    return resolve_scanline_image_path(stored) is not None


# ---------------------------------------------------------------------------
# 帧解码：动图复用行背景的解码/选帧（帧上限/最小时长与 sidecar 同契约），
# 静态图按文件签名缓存；失败记负缓存避免每帧重新探测。
_STATIC_CACHE_MAX = 8
_STATIC_CACHE: "OrderedDict[tuple, Optional[QImage]]" = OrderedDict()


def _static_scanline_image(path: str) -> Optional[QImage]:
    try:
        stat = Path(path).stat()
    except OSError:
        return None
    key = (str(path), int(stat.st_mtime_ns), int(stat.st_size))
    if key in _STATIC_CACHE:
        _STATIC_CACHE.move_to_end(key)
        return _STATIC_CACHE[key]
    image = QImage(str(path))
    if not image.isNull():
        image = image.convertToFormat(
            QImage.Format.Format_ARGB32_Premultiplied
        )
    else:
        image = None
    _STATIC_CACHE[key] = image
    while len(_STATIC_CACHE) > _STATIC_CACHE_MAX:
        _STATIC_CACHE.popitem(last=False)
    return image


def scanline_image_frame(resolved_path: str, t_ms: int) -> Optional[QImage]:
    """取 ``t_ms`` 时刻应显示的素材帧（动图循环选帧；静态图原样返回）。"""

    animated = animated_band_frames(resolved_path)
    if animated is not None:
        frames, delays = animated
        index = band_frame_index(delays, int(t_ms))
        frame = frames[index] if 0 <= index < len(frames) else frames[0]
        return frame
    return _static_scanline_image(resolved_path)


# ---------------------------------------------------------------------------
# 取色：按扫字线颜色模式解析「配色态 + 几何来源样式」，再在锚点采样实色。
def _scanline_role_source(style: Style) -> Optional[Style]:
    """复用配色方案模式的来源样式（与 ``scanline_role_fill`` 同一解析口径）。"""

    name = str(style.scanline_role_name or "").strip()
    if not name:
        return None
    if name == SCANLINE_GLOBAL_ROLE_KEY:
        return style
    scheme = style.custom_style_schemes.get(name)
    if scheme is None:
        return None
    return scheme


@dataclass(frozen=True)
class ScanlineImagePaintSource:
    """图片取色层的一站式来源：配色态 + 几何参数来自哪个样式。"""

    state: KaraokeColorState
    geometry_style: Style


# role 模式几何槽位：方案 None = 继承全局（与角色外观其余字段的语义
# 一致；方案里未写的槽位不回落成 0）。GPU IR 的物化值用同一张表。
_ROLE_GEOMETRY_FIELDS: tuple[str, ...] = (
    "stroke_width_px",
    "stroke2_enabled",
    "stroke2_width_px",
    "decoration_kind",
    "glow_radius_px",
    "glow_before_radius_px",
    "glow_after_radius_px",
    "glow_concentration_level",
    "glow_before_concentration_level",
    "shadow_offset_x",
    "shadow_offset_y",
    "font_size_px",
)


def _scanline_role_geometry_style(style: Style, source) -> Style:
    """把方案的非空几何槽位叠加到主样式上（None = 继承主样式）。

    ``source`` 为 None 或主样式本身时原样返回主样式。装饰/发光只消费
    after 态，before 槽位仅为保持字段完整。
    """

    if source is None or source is style:
        return style
    from dataclasses import replace as _replace

    merged = style
    for field_name in _ROLE_GEOMETRY_FIELDS:
        value = getattr(source, field_name, None)
        if value is not None:
            merged = _replace(merged, **{field_name: value})
    return merged


def scanline_image_role_geometry(style: Style) -> dict[str, float | str] | None:
    """物化 role 模式的装饰几何（GPU IR 下发用；CPU 渲染不经过这里）。

    返回 dict（键与 ``_scanline_role_source`` 解析出的来源几何一致：方案
    None 槽位继承全局）；``scanline_mode != "role"`` 或悬空引用返回 None
    （native 端沿用行样式几何）。
    """

    if str(getattr(style, "scanline_mode", "")) != "role":
        return None
    source = _scanline_role_source(style)
    if source is None:
        return None
    merged = _scanline_role_geometry_style(style, source)
    kind = decoration_kind_for_state(merged, after=True)
    glow_radius = 0
    if kind == "glow":
        concentration = glow_concentration_level(merged, after=True)
        if concentration >= 0:
            glow_radius = int(merged.glow_after_radius_px or 0)
    return {
        "stroke_width_px": float(merged.stroke_width_px or 0),
        "stroke2_width_px": float(
            (merged.stroke2_width_px or 0)
            if getattr(merged, "stroke2_enabled", False)
            else 0
        ),
        "font_size_px": float(merged.font_size_px or 0),
        "glow_radius_px": float(glow_radius),
        "shadow_offset_x_px": float(
            shadow_offset_x(merged, after=True) if kind == "shadow" else 0
        ),
        "shadow_offset_y_px": float(
            shadow_offset_y(merged, after=True) if kind == "shadow" else 0
        ),
        "decoration_kind": kind,
    }


def scanline_image_paint_source(
    style: Style,
    shape_colors: KaraokeColors,
    shape_style: Style | None,
    params,
) -> ScanlineImagePaintSource:
    """按 ``params.mode`` 解析图片装饰的配色态与几何来源样式。

    与普通高亮带的模式语义一一对应：``color``/``default`` 折成以扫字线
    单色为全槽的均匀态（描边/装饰与主体同色）；``brighten``/``follow_*``
    用锋面所在字符的配色态（亮度提升同口径，几何参数也取该字符样式）；
    ``role`` 取来源方案「走字后-主文字」的均匀态。悬空引用回退单色档。
    """

    from krok_helper.subtitle_render.engine.render.elements.horizontal.scanline import (
        _brighten_state_hsv,
        _uniform_scanline_state,
    )
    from krok_helper.subtitle_render.engine.style.style_semantics import (
        effective_karaoke_colors,
    )

    mode = str(params.mode)
    if mode == "role":
        source = _scanline_role_source(style)
        if source is not None:
            return ScanlineImagePaintSource(
                _uniform_scanline_state(
                    effective_karaoke_colors(source).after.text
                ),
                # 几何槽位：方案 None 继承全局（与角色外观其余字段同语义）。
                _scanline_role_geometry_style(style, source),
            )
    elif mode in {"brighten", "follow_before", "follow_after"}:
        base = (
            shape_colors.before if mode == "follow_before" else shape_colors.after
        )
        return ScanlineImagePaintSource(
            _brighten_state_hsv(base, params.brightness),
            shape_style if shape_style is not None else style,
        )
    return ScanlineImagePaintSource(
        _uniform_scanline_state(
            PaintFill(mode="solid", color=str(params.color or "#FFFFFF"))
        ),
        style,
    )


def _sample_fill_color(
    fill: PaintFill | None,
    fallback: str,
    *,
    gradient_line: QRectF | None,
    anchor_x: float,
    anchor_y: float,
) -> str:
    """把一份填充折成实色：渐变/拼色在锚点处按场景坐标采样。

    横向渐变取锚点在 ``gradient_line`` 上的水平进度、纵向渐变/拼色取
    垂直进度——图片沿锋面移动时颜色跟随所在位置变化，与烈焰棒的
    逐像素颜色场同口径；实色直接返回，图片填充取停止色平均。
    """

    if fill is None:
        return fallback
    if fill.mode == "solid":
        return str(fill.color or fallback)
    from krok_helper.subtitle_render.engine.render.elements.horizontal.scanline import (
        _fill_color_at,
    )

    line = gradient_line if gradient_line is not None else QRectF(0.0, 0.0, 1.0, 1.0)
    horizontal_coord = None
    v_norm = 0.5
    if line.width() > 0.0:
        horizontal_coord = (float(anchor_x) - line.left()) / line.width()
    if line.height() > 0.0:
        v_norm = (float(anchor_y) - line.top()) / line.height()
    field = _fill_color_at(fill, 0.5, v_norm, horizontal_coord)
    if field is not None:
        channels = [
            int(round(max(min(channel, 1.0), 0.0) * 255.0)) for channel in field
        ]
        return f"#{channels[0]:02X}{channels[1]:02X}{channels[2]:02X}"
    return fill_to_solid_color(fill, fallback)


# ---------------------------------------------------------------------------
# 装饰合成：把一帧栅格按取色层级烘成「装饰→描边2→描边→本体」的整图。
_DECOR_CACHE_MAX = 8
_DECOR_CACHE: "OrderedDict[tuple, tuple[QImage, int]]" = OrderedDict()

# _edge_source 的 spec 形参只读效果字段；这里给一个最小合法壳。
_EDGE_SPEC = BandSpec(
    image_path="",
    fill_mode="stretch",
    cap_left=0,
    cap_right=0,
    mid_mode="tile",
    tint="",
    tint_strength_pct=0,
    opacity_pct=100,
)


@dataclass(frozen=True)
class _DecorPlan:
    """取色层级解析后的具体绘制参数（全部已是实色/整数像素）。"""

    tint_strength: int = 100
    tint_color: str = ""
    stroke_color: str = ""
    stroke2_color: str = ""
    decor_kind: str = "none"
    decor_color: str = ""
    stroke_w: int = 0
    stroke2_w: int = 0
    glow_w: int = 0
    shadow_dx: float = 0.0
    shadow_dy: float = 0.0


def _decor_plan(
    layers: tuple[str, ...],
    source: ScanlineImagePaintSource,
    *,
    tint_strength: int,
    effect_ratio: float,
    raster_h: float,
    gradient_line: QRectF | None,
    anchor_x: float,
    anchor_y: float,
    fallback_color: str,
) -> Optional[_DecorPlan]:
    """效果几何换算（2026-10 用户口径）：按图片**显示后**长边与画布的
    比值折算（``effect_ratio = max(自然宽, 自然高) × 尺寸% / 1080``，
    与画布高度无关的无量纲值），宽度/发光半径向上取整、阴影偏移按比例
    浮点缩放；不再按字号折算。"""

    if not layers:
        return None
    geometry = source.geometry_style
    scale = max(float(effect_ratio), 0.0)
    half_cap = max(float(raster_h) * 0.5, 4.0)

    def width_of(raw: float) -> int:
        if raw <= 0.0:
            return 0
        import math

        return max(1, min(int(math.ceil(raw * scale)), int(half_cap)))

    def color_of(fill: PaintFill | None) -> str:
        return _sample_fill_color(
            fill,
            fallback_color,
            gradient_line=gradient_line,
            anchor_x=anchor_x,
            anchor_y=anchor_y,
        )

    state = source.state
    decor_kind = "none"
    glow_w = 0
    shadow_dx = shadow_dy = 0.0
    if "decor" in layers:
        kind = decoration_kind_for_state(geometry, after=True)
        if kind == "glow":
            concentration = glow_concentration_level(geometry, after=True)
            radius_raw = (
                int(geometry.glow_after_radius_px or 0) if concentration >= 0 else 0
            )
            glow_w = width_of(float(radius_raw))
            if glow_w > 0:
                decor_kind = "glow"
        elif kind == "shadow":
            shadow_dx = float(shadow_offset_x(geometry, after=True)) * scale
            shadow_dy = float(shadow_offset_y(geometry, after=True)) * scale
            decor_kind = "shadow"
    stroke2_w = 0
    if "stroke" in layers and getattr(geometry, "stroke2_enabled", False):
        stroke2_w = width_of(float(geometry.stroke2_width_px or 0))
    return _DecorPlan(
        tint_strength=tint_strength,
        tint_color=(
            color_of(state.text) if "solid" in layers else ""
        ),
        stroke_color=color_of(state.stroke),
        stroke2_color=color_of(state.stroke2),
        decor_kind=decor_kind,
        decor_color=color_of(state.shadow),
        stroke_w=width_of(float(geometry.stroke_width_px or 0)) if "stroke" in layers else 0,
        stroke2_w=stroke2_w,
        glow_w=glow_w,
        shadow_dx=shadow_dx,
        shadow_dy=shadow_dy,
    )


def _decorated_scanline_frame(
    raster: QImage,
    plan: _DecorPlan,
) -> tuple[QImage, int]:
    """按帧（含动图逐帧）生成装饰图；返回 ``(整图, 外扩边距)``。

    缓存键含帧对象的 ``cacheKey`` 与全部实色/尺寸参数：动图每帧是不同
    QImage，效果位图逐帧独立生成，满足「上色/描边/装饰跟随每一帧的
    alpha 动」。图层顺序与文字栈一致：装饰 → 描边2 → 描边 → 本体。
    """

    key = (int(raster.cacheKey()), plan)
    cached = _DECOR_CACHE.get(key)
    if cached is not None:
        _DECOR_CACHE.move_to_end(key)
        return cached

    body = raster
    if plan.tint_color:
        # 强度混合（保留素材 alpha；100 = 纯目标色剪影，与用户口径
        # 「避免整图变目标色」一致——默认 50% 保留一半原色）。
        body = _tinted(raster, plan.tint_color, plan.tint_strength)

    margin = 0
    effects: list[tuple[QImage, float, float]] = []
    if plan.decor_kind == "glow":
        result = _edge_source(
            _EDGE_SPEC,
            raster,
            BandEffect(
                kind="glow", color=plan.decor_color, size_px=plan.glow_w
            ),
        )
        if result is not None:
            edge, pad = result
            effects.append((edge, float(-pad), float(-pad)))
            margin = max(margin, pad)
    elif plan.decor_kind == "shadow":
        silhouette = _alpha_to_image(_qimage_alpha(raster), plan.decor_color)
        effects.append((silhouette, plan.shadow_dx, plan.shadow_dy))
        margin = max(
            margin,
            int(math.ceil(abs(plan.shadow_dx) + 1.0)),
            int(math.ceil(abs(plan.shadow_dy) + 1.0)),
        )
    if plan.stroke2_w > 0:
        result = _edge_source(
            _EDGE_SPEC,
            raster,
            BandEffect(
                kind="stroke", color=plan.stroke2_color, size_px=plan.stroke2_w
            ),
        )
        if result is not None:
            edge, pad = result
            effects.append((edge, float(-pad), float(-pad)))
            margin = max(margin, pad)
    if plan.stroke_w > 0:
        result = _edge_source(
            _EDGE_SPEC,
            raster,
            BandEffect(
                kind="stroke", color=plan.stroke_color, size_px=plan.stroke_w
            ),
        )
        if result is not None:
            edge, pad = result
            effects.append((edge, float(-pad), float(-pad)))
            margin = max(margin, pad)

    composite = QImage(
        raster.width() + margin * 2,
        raster.height() + margin * 2,
        QImage.Format.Format_ARGB32_Premultiplied,
    )
    composite.fill(0)
    layer = QPainter(composite)
    try:
        for image, dx, dy in effects:
            layer.drawImage(
                QPointF(float(margin) + dx, float(margin) + dy), image
            )
        layer.drawImage(margin, margin, body)
    finally:
        layer.end()
    out = (composite, margin)
    _DECOR_CACHE[key] = out
    while len(_DECOR_CACHE) > _DECOR_CACHE_MAX:
        _DECOR_CACHE.popitem(last=False)
    return out


def clear_scanline_image_caches() -> None:
    """清空素材/装饰缓存（测试与素材热替换排查用）。"""

    _STATIC_CACHE.clear()
    _DECOR_CACHE.clear()
    _resolve_memo.clear()


# ---------------------------------------------------------------------------
# 绘制入口：由 painter 的行级 overlay 调用（文字与注音之后）。
def paint_scanline_image(
    painter: QPainter,
    *,
    params,
    style: Style,
    shape_colors: KaraokeColors,
    shape_style: Style | None,
    anchor_x: float,
    anchor_y: float,
    gradient_line: QRectF | None,
    opacity: float,
    canvas_scale: float,
) -> bool:
    """在默认锚点 ``(anchor_x, anchor_y)`` 处绘制当前帧的自定义图片。

    ``anchor_x``/``anchor_y`` 是画布像素坐标（走字锋面 + 主文字垂直中心，
    不含用户偏移——偏移由 ``params.image_offset_*`` 在内部叠加，直连调用
    与 overlay 路径同口径）；``canvas_scale`` = 当前输出高度 / 1080，图片
    按自然像素 × 该系数缩放，与偏移字段的 1080 基准换算同口径。返回是否
    真的画了（素材缺失时 ``False``，调用方据此回退普通高亮带）。
    """

    opacity = max(0.0, min(float(opacity), 1.0))
    if opacity <= 0.0:
        return False
    resolved = resolve_scanline_image_path(params.image_path)
    if resolved is None:
        return False
    frame = scanline_image_frame(resolved, params.time_ms)
    if frame is None or frame.width() <= 0 or frame.height() <= 0:
        return False
    anchor_x = float(anchor_x) + float(params.image_offset_x_px)
    # 屏幕坐标 Y 轴向下：正值向下 = 加 Y（2026-10 用户复验后改回：
    # 与屏幕自然方向一致，负上正下；仅垂直偏移，水平仍负左正右）。
    anchor_y = float(anchor_y) + float(params.image_offset_y_px)
    scale = max(float(canvas_scale) * float(params.image_scale), 0.01)
    target_w = max(int(round(frame.width() * scale)), 1)
    target_h = max(int(round(frame.height() * scale)), 1)
    # 先把帧缩放到目标尺寸：效果几何全部按画布像素计算，不随原图
    # 分辨率漂移；SmoothPixmapTransform 让缩放半透明边缘保持干净。
    raster = QImage(target_w, target_h, QImage.Format.Format_ARGB32_Premultiplied)
    raster.fill(0)
    scaler = QPainter(raster)
    try:
        scaler.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        scaler.drawImage(QRectF(0.0, 0.0, target_w, target_h), frame)
    finally:
        scaler.end()

    composite: QImage | None = None
    margin = 0
    layers = tuple(params.image_layers or ())
    if layers:
        paint_source = scanline_image_paint_source(
            style, shape_colors, shape_style, params
        )
        plan = _decor_plan(
            layers,
            paint_source,
            tint_strength=max(
                0, min(int(params.image_tint_strength), 100)
            ),
            effect_ratio=(
                max(float(frame.width()), float(frame.height()))
                * float(params.image_scale)
                / 1080.0
            ),
            raster_h=float(target_h),
            gradient_line=gradient_line,
            anchor_x=float(anchor_x),
            anchor_y=float(anchor_y),
            fallback_color=str(params.color or "#FFFFFF"),
        )
        if plan is not None:
            composite, margin = _decorated_scanline_frame(raster, plan)
    if composite is None:
        composite = raster

    left = float(anchor_x) - target_w * 0.5 - margin
    top = float(anchor_y) - target_h * 0.5 - margin
    painter.save()
    try:
        painter.setOpacity(max(0.0, min(painter.opacity() * opacity, 1.0)))
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.drawImage(
            QRectF(left, top, float(composite.width()), float(composite.height())),
            composite,
        )
    finally:
        painter.restore()
    return True
