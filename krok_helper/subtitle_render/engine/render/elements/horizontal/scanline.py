"""Karaoke scan-line highlight painted at the moving wipe front."""

from __future__ import annotations

import math
from dataclasses import dataclass, replace

import numpy as np

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import (
    QColor,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QTransform,
)

from krok_helper.subtitle_render.domain.models import (
    SCANLINE_GLOBAL_ROLE_KEY,
    Style,
)
from krok_helper.subtitle_render.domain.paint import (
    KaraokeColors,
    KaraokeColorState,
    PaintFill,
)
from krok_helper.subtitle_render.engine.style.style_semantics import (
    effective_karaoke_colors,
)
from krok_helper.subtitle_render.engine.render.elements.horizontal.wipe import (
    fill_clip_band,
    segment_fill_ratio,
)
from krok_helper.subtitle_render.engine.render.elements.horizontal.transitions import (
    fx_unit_hash,
)
from krok_helper.subtitle_render.engine.render.effects import (
    main_stroke2_width,
    paint_fill_path,
    paint_stroke_path,
    stroke2_pen_width,
    stroke_pen_width,
)


@dataclass(frozen=True)
class ScanlineParams:
    """Resolved scan-line visual parameters shared by main text and ruby."""

    width_px: float
    color: str
    glow_px: int
    mode: str = "color"
    brightness: float = 0.6
    role_fill: PaintFill | None = None
    """``role`` 模式解析出的来源「走字后-主文字」填充；整条带（含描边）
    都用这一份填充重绘。``None`` = 回退单色。"""

    shape_fill: PaintFill | None = None
    """柱体形态实际使用的填充（跟随字体/复用配色方案时可能是渐变/拼色）。

    普通高亮带按 fill 重绘；柱体把渐变烘成逐像素颜色场，而不是取平均色。"""

    shape: str = "band"
    """锋面形态：``band`` 普通高亮带；``flame`` 烈焰棒；``bubble`` 水泡；
    ``image`` 自定义图片。

    烈焰棒把扫字线本体变成发光火棒并从两侧喷火舌；水泡从锋面底部发射
    镂空水泡。自定义图片在锋面上叠加用户素材（动图逐帧），由
    :mod:`..scanline_image` 绘制。"""

    image_path: str = ""
    """自定义图片形态的素材绝对路径（渲染期带工程目录回退解析）。"""

    image_band_enabled: bool = True
    """自定义图片形态下是否仍绘制普通文字高亮带；素材缺失时恒回退绘制。"""

    image_offset_x_px: float = 0.0
    """自定义图片左右偏移（输出像素，正右负左）。"""

    image_offset_y_px: float = 0.0
    """自定义图片上下偏移（输出像素，正上负下）。"""

    image_layers: tuple[str, ...] = ()
    """自定义图片取色层级组合（solid/stroke/decor 的任意子集）。"""

    image_scale: float = 1.0
    """自定义图片显示尺寸倍率（1.0 = 自然像素 × 画布高/1080）。"""

    image_tint_strength: int = 50
    """实色上色强度（0~100；100 = 纯目标色剪影）。"""

    time_ms: int = 0
    """当前帧时间（ms）：烈焰棒图样/水泡粒子动画用；普通高亮带忽略。"""

    column_growth: float = 1.0
    """柱体生长进度（0~1）：按当前字符唱字窗已过 180ms 计算，柱体从中心
    由小长大；普通高亮带忽略。"""

    @property
    def is_column_shape(self) -> bool:
        return self.shape in {"flame", "bubble", "image"}

    @property
    def suppress_strip(self) -> bool:
        """自定义图片形态关闭「文字高亮」时不再画普通高亮带。

        仅当素材**可用**才抑制：素材缺失时保持普通高亮带（用户口径的
        回退普通高光模式）。路径解析在 :mod:`..scanline_image`，这里
        延迟导入避免模块环。
        """

        if self.shape != "image" or self.image_band_enabled:
            return False
        from krok_helper.subtitle_render.engine.render.elements.scanline_image import (
            scanline_image_available,
        )

        return scanline_image_available(self.image_path)

    @property
    def half_width(self) -> float:
        return max(self.width_px, 1.0) / 2.0

    @property
    def is_brighten(self) -> bool:
        return self.mode == "brighten"

    @property
    def is_role(self) -> bool:
        return self.mode == "role" and self.role_fill is not None

    @property
    def follow_state(self) -> str | None:
        """``follow_before`` / ``follow_after`` 模式的固定态；其余为 ``None``。"""

        if self.mode == "follow_before":
            return "before"
        if self.mode == "follow_after":
            return "after"
        return None

    @property
    def solid_half_width(self) -> float:
        """Hard core left after the requested inner feather is removed."""

        return max(self.half_width - float(self.glow_px), 0.0)


def scanline_role_fill(style: Style) -> PaintFill | None:
    """Resolve the ``role``-mode fill: the named source's after-state text fill.

    Only the text fill is taken from the source; the whole glyph stack (stroke
    and second stroke included) is repainted with this one fill, matching the
    solid-colour mode's uniform-state structure. ``__global__`` resolves to
    the main style's own after-state text fill; other names must match a
    ``custom_style_schemes`` entry. Returns ``None`` unless the mode is active
    and the name resolves; callers fall back to the solid ``color``-mode
    paint in that case.
    """

    if style.scanline_mode != "role":
        return None
    name = str(style.scanline_role_name or "").strip()
    if not name:
        return None
    if name == SCANLINE_GLOBAL_ROLE_KEY:
        return effective_karaoke_colors(style).after.text
    scheme = style.custom_style_schemes.get(name)
    if scheme is None:
        return None
    return effective_karaoke_colors(scheme).after.text


def scanline_shape_fill(style: Style) -> PaintFill | None:
    """柱体要使用的实际填充（保留渐变/拼色，不折平均色）。"""

    mode = str(getattr(style, "scanline_mode", "") or "")
    if mode == "role":
        return scanline_role_fill(style)
    if mode in {"follow_before", "follow_after", "brighten"}:
        colors = effective_karaoke_colors(style)
        state = colors.before if mode == "follow_before" else colors.after
        return state.text
    return None


def scanline_params_for_style(style: Style) -> ScanlineParams:
    """Return the scan-line parameters carried by one style."""

    mode = (
        style.scanline_mode
        if style.scanline_mode
        in {
            "default",
            "color",
            "brighten",
            "follow_before",
            "follow_after",
            "role",
        }
        else "color"
    )
    color = (
        "#FFFFFF"
        if style.scanline_mode == "default"
        else (style.scanline_color or "#FFFFFF")
    )
    shape = (
        style.scanline_shape
        if getattr(style, "scanline_shape", "band")
        in {"band", "flame", "bubble", "image"}
        else "band"
    )
    return ScanlineParams(
        width_px=max(float(style.scanline_width_px), 1.0),
        color=color,
        glow_px=max(int(style.scanline_glow_px), 0),
        mode=mode,
        brightness=min(max(int(style.scanline_brightness_pct), 0), 100) / 100.0,
        role_fill=scanline_role_fill(style),
        shape_fill=scanline_shape_fill(style),
        shape=shape,
        image_path=str(getattr(style, "scanline_image_path", "") or ""),
        image_band_enabled=bool(
            getattr(style, "scanline_image_band_enabled", True)
        ),
        image_offset_x_px=float(
            getattr(style, "scanline_image_offset_x_px", 0) or 0
        ),
        image_offset_y_px=float(
            getattr(style, "scanline_image_offset_y_px", 0) or 0
        ),
        image_layers=tuple(
            token
            for token in str(
                getattr(style, "scanline_image_color_layers", "") or ""
            ).split(",")
            if token in {"solid", "stroke", "decor"}
        ),
        image_scale=max(
            float(getattr(style, "scanline_image_scale_pct", 100) or 100)
            / 100.0,
            0.1,
        ),
        image_tint_strength=max(
            0,
            min(
                int(getattr(style, "scanline_image_tint_strength_pct", 50) or 50),
                100,
            ),
        ),
    )


def scanline_band_rect(
    rect: QRectF,
    front: float,
    params: ScanlineParams,
    *,
    vertical: bool,
) -> QRectF:
    """Return the highlight band centred on the wipe front."""

    half = params.half_width
    if vertical:
        return QRectF(
            rect.left(),
            front - half,
            max(rect.width(), 1.0),
            half * 2.0,
        )
    return QRectF(
        front - half,
        rect.top(),
        half * 2.0,
        max(rect.height(), 1.0),
    )


def band_touches_rect(band: QRectF, rect: QRectF, pad: float) -> bool:
    return band.intersects(
        QRectF(
            rect.left() - pad,
            rect.top() - pad,
            rect.width() + pad * 2.0,
            rect.height() + pad * 2.0,
        )
    )


def scanline_feather_slices(
    rect: QRectF,
    front: float,
    params: ScanlineParams,
    *,
    vertical: bool,
) -> list[tuple[QRectF, float]]:
    """Approximate an inner-only soft band with non-overlapping alpha slices."""

    half = params.half_width
    softness = min(max(float(params.glow_px), 0.0), half)
    core = half - softness
    count = max(1, min(int(round(half * 2.0)), 64))
    step = half * 2.0 / count
    result: list[tuple[QRectF, float]] = []
    for index in range(count):
        offset = -half + index * step
        distance = abs(offset + step * 0.5)
        if softness <= 0.0 or distance <= core:
            alpha = 1.0
        else:
            progress = max(0.0, min((half - distance) / softness, 1.0))
            alpha = progress * progress * (3.0 - 2.0 * progress)
        if alpha <= 0.0:
            continue
        slice_rect = (
            QRectF(rect.left(), front + offset, rect.width(), step)
            if vertical
            else QRectF(front + offset, rect.top(), step, rect.height())
        )
        result.append((slice_rect, alpha))
    return result


def _solid_scanline_fill(color: str) -> PaintFill:
    return PaintFill(
        mode="solid",
        color=color,
        start_color=color,
        end_color=color,
        gradient_stops=[(0, color), (100, color)],
        split_top_color=color,
        split_bottom_color=color,
        split_stops=[(0, color), (100, color)],
    )


def _brighten_color_hsv(value: str, amount: float) -> str:
    """Raise HSV value without changing the source hue or saturation."""

    color = QColor(value)
    if not color.isValid():
        return value
    hue, saturation, brightness, alpha = color.getHsvF()
    brightness = brightness + (1.0 - brightness) * max(0.0, min(amount, 1.0))
    result = QColor()
    result.setHsvF(hue, saturation, brightness, alpha)
    return result.name(
        QColor.NameFormat.HexArgb if alpha < 1.0 else QColor.NameFormat.HexRgb
    )


def _brighten_fill_hsv(fill: PaintFill, amount: float) -> PaintFill:
    """Return a fill whose authored colours have a higher HSV value."""

    brighten = lambda value: _brighten_color_hsv(value, amount)
    return replace(
        fill,
        color=brighten(fill.color),
        start_color=brighten(fill.start_color),
        end_color=brighten(fill.end_color),
        gradient_stops=[
            (position, brighten(color)) for position, color in fill.gradient_stops
        ],
        split_top_color=brighten(fill.split_top_color),
        split_bottom_color=brighten(fill.split_bottom_color),
        split_stops=[
            (position, brighten(color)) for position, color in fill.split_stops
        ],
    )


def _brighten_state_hsv(state: KaraokeColorState, amount: float) -> KaraokeColorState:
    return KaraokeColorState(
        text=_brighten_fill_hsv(state.text, amount),
        stroke=_brighten_fill_hsv(state.stroke, amount),
        stroke2=_brighten_fill_hsv(state.stroke2, amount),
        shadow=_brighten_fill_hsv(state.shadow, amount),
    )


def _uniform_scanline_state(fill: PaintFill) -> KaraokeColorState:
    """One fill applied to every layer, like the solid state but with a fill."""

    return KaraokeColorState(text=fill, stroke=fill, stroke2=fill, shadow=fill)


def _solid_scanline_state(color: str) -> KaraokeColorState:
    fill = _solid_scanline_fill(color)
    return KaraokeColorState(text=fill, stroke=fill, stroke2=fill, shadow=fill)


def _state_clip(
    rect: QRectF, front: float, *, vertical: bool, rtl: bool, after: bool
) -> QRectF:
    """Clip one side of the moving front to its actual before/after colour."""

    after_is_far_side = rtl
    far_side = after == after_is_far_side
    extent = 1_000_000.0
    if vertical:
        return (
            QRectF(rect.left() - extent, front, rect.width() + extent * 2.0, extent)
            if far_side
            else QRectF(
                rect.left() - extent,
                -extent,
                rect.width() + extent * 2.0,
                front + extent,
            )
        )
    return (
        QRectF(front, rect.top() - extent, extent, rect.height() + extent * 2.0)
        if far_side
        else QRectF(
            -extent, rect.top() - extent, front + extent, rect.height() + extent * 2.0
        )
    )


SCANLINE_COLUMN_WIDTH = 72
"""烈焰棒程序化图样的内部栅格宽度（px，绘制时等比缩放到锋面尺寸）。

72×108 在目标尺寸下已足够细腻；降低栅格是为了让 60fps 预览/导出时
逐帧生成与纹理上传的 CPU/GPU 成本可控（128×192 的逐帧 numpy 生成
在单行上就要 ~15ms，几乎吃掉整帧预算）。
"""

SCANLINE_COLUMN_HEIGHT = 108
"""烈焰棒程序化图样的内部栅格高度（px）。"""

SCANLINE_BUBBLE_COUNT = 18
"""旧柱状图样里的气泡数（兼容保留；生产水泡改为逐帧椭圆粒子）。"""


def _qcolor_rgb(value: str) -> tuple[float, float, float]:
    color = QColor(value)
    if not color.isValid():
        color = QColor("#FFFFFF")
    return (color.redF(), color.greenF(), color.blueF())


def _lighten_rgb(
    rgb: tuple[float, float, float], amount: float
) -> tuple[float, float, float]:
    amount = min(max(float(amount), 0.0), 1.0)
    return tuple(channel + (1.0 - channel) * amount for channel in rgb)


def _rgb_to_hex(rgb: tuple[float, float, float]) -> str:
    return QColor.fromRgbF(
        min(max(rgb[0], 0.0), 1.0),
        min(max(rgb[1], 0.0), 1.0),
        min(max(rgb[2], 0.0), 1.0),
    ).name()


def _representative_fill_color(fill: PaintFill) -> str:
    """把渐变/拼色/图片填充折算成一个代表色（烈焰棒/水泡的基色）。"""

    stops = fill.gradient_stops or fill.split_stops
    if len(stops) >= 2:
        red = sum(QColor(color).redF() for _position, color in stops) / len(stops)
        green = sum(QColor(color).greenF() for _position, color in stops) / len(stops)
        blue = sum(QColor(color).blueF() for _position, color in stops) / len(stops)
        return _rgb_to_hex((red, green, blue))
    return fill.color or "#FFFFFF"


def _fill_endpoint_palette(
    fill: PaintFill,
) -> tuple[tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]] | None:
    """把渐变/拼色的首尾色映射成柱体的外沿/内焰/火芯三色。

    单色和图片填充返回 None，继续走代表色逻辑；图片暂不支持把纹理
    采样进程序化柱体，只能取平均色。
    """

    if fill.mode not in {
        "gradient_horizontal",
        "gradient_vertical",
        "split_vertical",
    }:
        return None
    stops = [
        (float(position), str(color))
        for position, color in (fill.gradient_stops or fill.split_stops)
        if QColor(str(color)).isValid()
    ]
    if len(stops) < 2:
        return None
    stops.sort(key=lambda item: item[0])
    outer = _qcolor_rgb(stops[0][1])
    inner = _qcolor_rgb(stops[-1][1])
    return (outer, inner, _lighten_rgb(inner, 0.70))


def scanline_column_base_color(
    params: ScanlineParams,
    colors: KaraokeColors | None = None,
) -> str:
    """解析烈焰棒/水泡的基色（沿用扫字线的颜色方案）。

    ``role`` 取来源「走字后-主文字」填充的代表色；``brighten`` / 跟随字体
    取当前行实际配色的走字后/走字前文字色并应用亮度提升；``color`` 用单独
    颜色。与普通高亮带的取色口径一致。
    """

    if params.is_role and params.role_fill is not None:
        return _representative_fill_color(params.role_fill)
    state: KaraokeColorState | None = None
    if colors is not None:
        if params.mode == "follow_before":
            state = colors.before
        elif params.mode in {"follow_after", "brighten"}:
            state = colors.after
    if state is not None:
        # 与普通粒子同口径：渐变/拼色折算成平均实色，而不是只读
        # PaintFill.color（那只是渐变的一个端点/旧字段）。
        fill = state.text
        base_color = (
            _representative_fill_color(fill)
            if fill.mode
            in {"gradient_horizontal", "gradient_vertical", "split_vertical"}
            else fill.color
        )
        return _brighten_color_hsv(base_color, params.brightness)
    return params.color


def _scanline_column_palette(
    params: ScanlineParams,
    colors: KaraokeColors | None = None,
) -> tuple[tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]]:
    """返回 (外沿/环, 内焰/填充, 火芯/高光) 三个 RGB 浮点色。

    单独颜色且仍是默认白时使用主题默认（火焰=标准火色阶、气泡=水蓝高光）；
    其它情况从扫字线颜色方案解析出的基色推导，保证「复用配色方案」等档位
    能直接改柱体颜色。
    """

    base = scanline_column_base_color(params, colors)
    if params.mode == "default":
        if params.shape == "flame":
            return (
                _qcolor_rgb("#E83A12"),
                _qcolor_rgb("#FFB02E"),
                _qcolor_rgb("#FFF1B8"),
            )
        return (
            _qcolor_rgb("#8FD3FF"),
            _qcolor_rgb("#F2F8FF"),
            _qcolor_rgb("#FFFFFF"),
        )
    if params.is_role and params.role_fill is not None:
        endpoint_palette = _fill_endpoint_palette(params.role_fill)
        if endpoint_palette is not None:
            return endpoint_palette
    base_rgb = _qcolor_rgb(base)
    if params.shape == "flame":
        return (
            base_rgb,
            _lighten_rgb(base_rgb, 0.40),
            _lighten_rgb(base_rgb, 0.80),
        )
    return (
        base_rgb,
        _lighten_rgb(base_rgb, 0.55),
        _lighten_rgb(base_rgb, 0.85),
    )


def render_scanline_column_image(
    params: ScanlineParams,
    colors: KaraokeColors | None = None,
) -> QImage:
    """旧柱状图样（兼容保留）。

    生产路径已改为：烈焰棒用 :func:`render_scanline_rod_image`，水泡用
    :func:`paint_scanline_bubbles` 逐帧发射椭圆粒子。本函数仅供旧测试/
    外部调用回看历史图样，不再参与实际绘制。
    """

    width = SCANLINE_COLUMN_WIDTH
    height = SCANLINE_COLUMN_HEIGHT
    outer, inner, core = _scanline_column_palette(params, colors)
    outer_rgb = np.asarray(outer, dtype=np.float32)
    inner_rgb = np.asarray(inner, dtype=np.float32)
    core_rgb = np.asarray(core, dtype=np.float32)
    u = (np.arange(width, dtype=np.float32) + 0.5) / width * 2.0 - 1.0
    v = (np.arange(height, dtype=np.float32) + 0.5) / height
    uu = np.broadcast_to(u, (height, width))
    vv = np.broadcast_to(v[:, None], (height, width))
    t = float(params.time_ms) / 1000.0

    def smoothstep_array(value):
        progress = np.clip(value, 0.0, 1.0)
        return progress * progress * (3.0 - 2.0 * progress)

    if params.shape == "flame":
        body_width = 0.50 + 0.06 * math.sin(t * 1.9)
        envelope = smoothstep_array((vv - 0.06) / 0.22) * (
            1.0 - smoothstep_array((vv - 0.86) / 0.16)
        )
        body = np.clip(1.0 - np.abs(uu) / body_width, 0.0, 1.0) ** 1.1
        body = body * envelope * (0.72 + 0.28 * np.sin(t * 5.0 + vv * 7.0))
        outer_alpha = np.clip(body * 0.55, 0.0, 1.0)
        # 5 条火舌：各自有出生相位/速度/宽度，逐帧生长并左右摆动。
        for index in range(5):
            phase = fx_unit_hash(index, 201)
            base_x = (fx_unit_hash(index, 203) - 0.5) * 0.85
            wobble = 0.045 * math.sin(
                t * (1.3 + 0.6 * fx_unit_hash(index, 206))
                + phase * 2.0 * math.pi
            )
            center_x = base_x + wobble
            lobe_height = (
                0.34 + 0.38 * fx_unit_hash(index, 204)
            ) * (
                0.78
                + 0.22 * math.sin(
                    t * (0.8 + 0.5 * fx_unit_hash(index, 202))
                    + phase * 2.0 * math.pi
                )
            )
            lobe_width = 0.28 + 0.16 * fx_unit_hash(index, 205)
            tip = 0.72 - lobe_height
            progress = np.clip(
                (0.72 - vv) / max(lobe_height, 1e-3), 0.0, 1.0
            )
            half_width = lobe_width * np.sqrt(progress) + 0.008
            distance = np.abs(uu - center_x) / half_width
            lobe = np.clip(1.0 - distance, 0.0, 1.0) ** 1.35
            lobe = lobe * np.clip((vv - tip) / 0.04, 0.0, 1.0) * (
                1.0 - np.clip((vv - 0.72) / 0.05, 0.0, 1.0)
            )
            lobe = lobe * (
                0.82
                + 0.18 * np.sin(
                    t * (5.0 + 3.0 * fx_unit_hash(index, 207))
                    + phase * 2.0 * math.pi
                    + vv * 9.0
                )
            )
            outer_alpha = np.maximum(outer_alpha, lobe * 0.85)
        core_width = 0.24 + 0.03 * math.sin(t * 2.3)
        core_mask = np.clip(1.0 - np.abs(uu) / core_width, 0.0, 1.0) ** 1.2
        core_mask = core_mask * envelope
        inner_alpha = np.maximum(body * 0.75, core_mask * 0.90)
        core_alpha = np.maximum(body * 0.50, core_mask * 0.95) * np.clip(
            (0.86 - vv) / 0.25, 0.0, 1.0
        )
        rgb = np.broadcast_to(outer_rgb, (height, width, 3)).copy()
        alpha = np.clip(outer_alpha, 0.0, 1.0)
        for layer_color, layer_alpha in (
            (inner_rgb, inner_alpha),
            (core_rgb, core_alpha),
        ):
            layer = np.clip(layer_alpha, 0.0, 1.0)
            rgb = layer_color * layer[..., None] + rgb * (1.0 - layer[..., None])
            alpha = layer + alpha * (1.0 - layer)
    else:
        body_width = 0.34 + 0.04 * math.sin(t * 2.4)
        envelope = np.clip((vv - 0.02) / 0.12, 0.0, 1.0) * (
            1.0 - np.clip((vv - 0.90) / 0.10, 0.0, 1.0)
        )
        body = np.clip(1.0 - np.abs(uu) / body_width, 0.0, 1.0) ** 1.2
        body = body * envelope * (0.18 + 0.06 * np.sin(t * 4.0))
        core_width = 0.07 + 0.02 * math.sin(t * 3.1)
        core_mask = np.clip(1.0 - np.abs(uu) / core_width, 0.0, 1.0) ** 1.5
        core_mask = core_mask * envelope * (0.35 + 0.15 * np.sin(t * 5.0))
        rgb = np.broadcast_to(inner_rgb, (height, width, 3)).copy()
        alpha = np.clip(body, 0.0, 1.0)
        for index in range(SCANLINE_BUBBLE_COUNT):
            phase = fx_unit_hash(index, 301)
            speed = 0.30 + 0.30 * fx_unit_hash(index, 302)
            base_x = (fx_unit_hash(index, 303) - 0.5) * 0.60
            age = math.fmod(phase + t * speed, 1.0)
            center_y = 0.72 - age * 0.68
            center_x = base_x + 0.035 * math.sin(
                t * 1.8 + phase * 2.0 * math.pi
            )
            fade = min(age / 0.08, 1.0) * min((1.0 - age) / 0.18, 1.0)
            if fade <= 0.0:
                continue
            radius = 0.09 + 0.10 * fx_unit_hash(index, 304)
            grow = 0.7 + 0.5 * min(age / 0.25, 1.0)
            radius_px = max(radius * width * grow, 1.0)
            dx = (uu - center_x) * (width * 0.5)
            dy = (vv - center_y) * height
            distance = np.sqrt(dx * dx + dy * dy) / radius_px
            ring = np.exp(-(((distance - 0.82) / 0.11) ** 2))
            fill = np.clip(1.0 - distance, 0.0, 1.0) ** 1.5 * 0.22
            highlight = (
                np.exp(
                    -(
                        (
                            (dx + 0.32 * radius_px) ** 2
                            + (dy + 0.32 * radius_px) ** 2
                        )
                        / (0.18 * radius_px) ** 2
                    )
                )
                * 0.90
            )
            weights = (ring * 0.90 + fill + highlight) * fade
            bubble_alpha = np.clip(weights, 0.0, 1.0)
            if bubble_alpha.max() <= 0.0:
                continue
            weighted = (
                outer_rgb * (ring * 0.90 * fade)[..., None]
                + inner_rgb * (fill * fade)[..., None]
                + core_rgb * (highlight * fade)[..., None]
            )
            bubble_rgb = np.divide(
                weighted,
                np.maximum(weights, 1e-6)[..., None],
                out=np.zeros_like(weighted),
                where=np.maximum(weights, 1e-6)[..., None] > 0.0,
            )
            new_alpha = bubble_alpha + alpha * (1.0 - bubble_alpha)
            safe = np.maximum(new_alpha, 1e-6)
            rgb = (
                bubble_rgb * bubble_alpha[..., None]
                + rgb * alpha[..., None] * (1.0 - bubble_alpha[..., None])
            ) / safe[..., None]
            alpha = new_alpha
        core_layer = np.clip(core_mask, 0.0, 1.0)
        rgb = core_rgb * core_layer[..., None] + rgb * (
            1.0 - core_layer[..., None]
        )
        alpha = core_layer + alpha * (1.0 - core_layer)
    out = np.empty((height, width, 4), dtype=np.uint8)
    safe_alpha = np.maximum(alpha, 1e-6)
    straight_rgb = rgb / safe_alpha[..., None]
    out[:, :, :3] = np.clip(
        straight_rgb * 255.0 + 0.5, 0.0, 255.0
    ).astype(np.uint8)
    out[:, :, 3] = np.clip(alpha * 255.0 + 0.5, 0.0, 255.0).astype(np.uint8)
    image = QImage(
        out.tobytes(),
        width,
        height,
        width * 4,
        QImage.Format.Format_RGBA8888,
    )
    return image.copy()


def _fill_color_field(
    fill: PaintFill | None,
    uu: np.ndarray,
    vv: np.ndarray,
    horizontal_coord: np.ndarray | None = None,
) -> np.ndarray | None:
    """把横向/纵向渐变（或拼色）烘成逐像素颜色场；实色/图片返回 None。"""

    if fill is None or fill.mode not in {
        "gradient_horizontal",
        "gradient_vertical",
        "split_vertical",
    }:
        return None
    raw_stops = fill.gradient_stops or fill.split_stops
    stops = [
        (float(position) / 100.0, _qcolor_rgb(str(color)))
        for position, color in raw_stops
        if QColor(str(color)).isValid()
    ]
    if len(stops) < 2:
        return None
    stops.sort(key=lambda item: item[0])
    positions = np.asarray([item[0] for item in stops], dtype=np.float32)
    colors = np.asarray([item[1] for item in stops], dtype=np.float32)
    if fill.mode == "split_vertical":
        # 拼色是硬边色带：取坐标落在的色带起点颜色，不做插值。
        coord = np.clip(vv, 0.0, 1.0)
        indices = np.clip(
            np.searchsorted(positions, coord, side="right") - 1,
            0,
            len(colors) - 1,
        )
        return colors[indices]
    coord = np.clip(
        (
            horizontal_coord
            if horizontal_coord is not None
            else (uu + 1.0) * 0.5
        )
        if fill.mode == "gradient_horizontal"
        else vv,
        0.0,
        1.0,
    )
    channels = [
        np.interp(coord, positions, colors[:, channel])
        for channel in range(3)
    ]
    return np.stack(channels, axis=-1).astype(np.float32)


def _lighten_color_field(field: np.ndarray, amount: float) -> np.ndarray:
    return np.clip(field + (1.0 - field) * amount, 0.0, 1.0)


def render_scanline_rod_image(
    params: ScanlineParams,
    colors: KaraokeColors | None = None,
    *,
    gradient_target: QRectF | None = None,
    gradient_line: QRectF | None = None,
) -> QImage:
    """生成一帧「烈焰棒」RGBA 图样。

    中心是一根金色发光柱，五条火焰缎带以螺旋方式**环绕**柱体：前半圈压在
    柱身前，后半圈被柱身遮住，只从两侧露出。缎带参数按行预计算，逐像素只
    做带状距离与衰减，保证 60fps 预览/导出时 CPU 生成成本足够低。
    """

    width = SCANLINE_COLUMN_WIDTH
    height = SCANLINE_COLUMN_HEIGHT
    outer, inner, core = _scanline_column_palette(params, colors)
    outer_rgb = np.asarray(outer, dtype=np.float32)
    inner_rgb = np.asarray(inner, dtype=np.float32)
    core_rgb = np.asarray(core, dtype=np.float32)
    u = (np.arange(width, dtype=np.float32) + 0.5) / width * 2.0 - 1.0
    v = (np.arange(height, dtype=np.float32) + 0.5) / height
    uu = np.broadcast_to(u, (height, width))
    vv = np.broadcast_to(v[:, None], (height, width))
    t = float(params.time_ms) / 1000.0

    def smoothstep_array(value):
        progress = np.clip(value, 0.0, 1.0)
        return progress * progress * (3.0 - 2.0 * progress)

    vertical_row = smoothstep_array((v - 0.03) / 0.07) * (
        1.0 - smoothstep_array((v - 0.94) / 0.05)
    )
    vertical = vertical_row[:, None]
    breath = 0.5 + 0.5 * math.sin(t * 2.8)
    ribbon_scale = 0.96 + 0.08 * breath
    horizontal_coord = None
    if (
        gradient_target is not None
        and gradient_line is not None
        and gradient_line.width() > 0.0
    ):
        scene_x = (
            gradient_target.left()
            + (uu + 1.0) * 0.5 * gradient_target.width()
        )
        horizontal_coord = (
            scene_x - gradient_line.left()
        ) / gradient_line.width()
    gradient_field = _fill_color_field(
        params.shape_fill, uu, vv, horizontal_coord
    )
    if gradient_field is None:
        outer_field = np.broadcast_to(outer_rgb, (height, width, 3))
        inner_field = np.broadcast_to(inner_rgb, (height, width, 3))
        core_field = np.broadcast_to(core_rgb, (height, width, 3))
    else:
        outer_field = gradient_field
        inner_field = _lighten_color_field(gradient_field, 0.20)
        core_field = _lighten_color_field(gradient_field, 0.50)

    specs = (
        (outer_field, inner_field, 1.85, 0.00, 0.46, 0.180, 0.92),
        (outer_field, inner_field, 2.12, 0.52, 0.42, 0.145, 0.82),
        (inner_field, core_field, 1.72, 0.24, 0.39, 0.105, 0.92),
        (core_field, core_field, 1.55, 0.55, 0.32, 0.048, 0.70),
    )
    rows = []
    for _edge, _center, turns, phase, amplitude, base_half, strength in specs:
        angle = (
            v * turns * 2.0 * math.pi
            + phase * 2.0 * math.pi
            + t * (2.0 + 0.60 * turns)
        )
        depth = np.cos(angle)
        swell = np.sin(angle * 2.0 + t * 2.3 + phase * 7.0)
        turb = np.sin(v * 13.0 + t * 5.0 + phase * 6.0) * np.sin(
            v * 7.0 - t * 2.5 + phase * 3.0
        )
        burn = 0.85 + 0.15 * np.sin(v * 55.0 - t * 12.0 + phase * 8.0)
        rows.append((
            amplitude * ribbon_scale * np.sin(angle) + 0.02 * turb,
            np.maximum(
                base_half * ribbon_scale
                * (0.45 + 0.55 * swell * swell)
                * (0.85 + 0.25 * np.abs(turb)),
                0.008,
            ),
            np.clip(depth * 2.6, 0.0, 1.0),
            np.clip(-depth * 2.6, 0.0, 1.0),
            0.88 + 0.12 * np.sin(angle * 2.0 - t * 3.1 + phase * 5.0),
            strength,
            burn,
        ))

    def ribbon_layers(*, back: bool):
        rgb = np.zeros((height, width, 3), dtype=np.float32)
        alpha = np.zeros((height, width), dtype=np.float32)
        for spec, row in zip(specs, rows):
            edge_color, center_color = spec[0], spec[1]
            center, half, front, back_facing, flicker, strength, burn = row
            dx = (uu - center[:, None]) / half[:, None]
            band = np.exp(-(dx ** 4))
            glow = np.exp(-((np.abs(dx) / 1.5) ** 2)) * 0.12
            facing = back_facing if back else front
            layer_alpha = (
                (band + glow)
                * facing[:, None]
                * flicker[:, None]
                * burn[:, None]
                * vertical
                * strength
            )
            if back:
                layer_alpha *= 0.38
            layer_alpha = np.clip(layer_alpha, 0.0, 1.0)
            core_mix = np.clip(1.0 - np.abs(dx), 0.0, 1.0)[..., None] ** 1.4
            color = edge_color * (1.0 - core_mix) + center_color * core_mix
            if back:
                # 后半圈只做暗部体积，不要暗红近黑，否则会像脏阴影。
                color = color * 0.40 + inner_field * 0.60
            rgb = color * layer_alpha[..., None] + rgb * (
                1.0 - layer_alpha[..., None]
            )
            alpha = layer_alpha + alpha * (1.0 - layer_alpha)
        return rgb, alpha

    back_rgb, back_alpha = ribbon_layers(back=True)

    # 软实体光晕：不是硬边柱体，给外围火圈一个连续的发光体/体积感。
    halo_half = 0.210 + 0.030 * math.sin(t * 3.1)
    halo_core = np.exp(
        -((np.abs(uu) / halo_half) ** 2.2)
    ) * vertical
    halo_wide = np.exp(
        -((np.abs(uu) / (halo_half * 2.6)) ** 2.0)
    ) * vertical
    halo_alpha = np.clip(
        0.32 * halo_core + 0.14 * halo_wide, 0.0, 0.70
    )
    halo_rgb = inner_field * 0.85 + core_field * 0.15

    # 中央金色柱：固定细柱 + 竖向流光 + 白热芯。
    rod_half = 0.130 + 0.018 * np.sin(v * 18.0 + t * 6.0)
    rod_half_col = rod_half[:, None]
    rod_body = np.exp(-((np.abs(uu) / rod_half_col) ** 2.4)) * vertical
    rod_glow = np.exp(
        -((np.abs(uu) / (rod_half_col * 3.2)) ** 2)
    ) * vertical * 0.32
    rod_alpha = np.clip(rod_glow + rod_body * 0.92, 0.0, 1.0)
    streak = 0.86 + 0.14 * np.sin(vv * 62.0 + uu * 7.0 + t * 9.0)
    rod_rgb = outer_field * streak[..., None]
    inner_mask = np.clip(
        np.exp(-((np.abs(uu) / (rod_half_col * 0.68)) ** 2.2)) * vertical,
        0.0,
        1.0,
    )
    core_mask = np.clip(
        np.exp(-((np.abs(uu) / (rod_half_col * 0.28)) ** 2.2)) * vertical,
        0.0,
        1.0,
    )
    rod_rgb = inner_field * inner_mask[..., None] + rod_rgb * (
        1.0 - inner_mask[..., None]
    )
    rod_rgb = core_field * core_mask[..., None] + rod_rgb * (
        1.0 - core_mask[..., None]
    )
    rod_alpha = np.maximum(rod_alpha, np.maximum(inner_mask, core_mask))

    front_rgb, front_alpha = ribbon_layers(back=False)

    # 合成：后半圈 → 柱体 → 前半圈。
    rgb = np.zeros((height, width, 3), dtype=np.float32)
    alpha = np.zeros((height, width), dtype=np.float32)
    for layer_rgb, layer_alpha in (
        (back_rgb, back_alpha),
        (halo_rgb, halo_alpha),
        (front_rgb, front_alpha),
    ):
        a = np.clip(layer_alpha, 0.0, 1.0)
        rgb = layer_rgb * a[..., None] + rgb * (1.0 - a[..., None])
        alpha = a + alpha * (1.0 - a)

    out = np.empty((height, width, 4), dtype=np.uint8)
    safe_alpha = np.maximum(alpha, 1e-6)
    straight_rgb = rgb / safe_alpha[..., None]
    out[:, :, :3] = np.clip(
        straight_rgb * 255.0 + 0.5, 0.0, 255.0
    ).astype(np.uint8)
    out[:, :, 3] = np.clip(alpha * 255.0 + 0.5, 0.0, 255.0).astype(np.uint8)
    image = QImage(
        out.tobytes(),
        width,
        height,
        width * 4,
        QImage.Format.Format_RGBA8888,
    )
    return image.copy()


def _fill_color_at(
    fill: PaintFill | None,
    u_norm: float,
    v_norm: float,
    horizontal_coord: float | None = None,
) -> tuple[float, float, float] | None:
    """Sampling a gradient fill at one normalized point (bubble centers)."""

    if fill is None:
        return None
    uu = np.asarray([[(float(u_norm) * 2.0) - 1.0]], dtype=np.float32)
    vv = np.asarray([[float(v_norm)]], dtype=np.float32)
    hcoord = (
        np.asarray([[float(horizontal_coord)]], dtype=np.float32)
        if horizontal_coord is not None
        else None
    )
    field = _fill_color_field(fill, uu, vv, hcoord)
    if field is None:
        return None
    return (float(field[0, 0, 0]), float(field[0, 0, 1]), float(field[0, 0, 2]))


def paint_scanline_bubbles(
    painter: QPainter,
    rect: QRectF,
    *,
    front: float,
    params: ScanlineParams,
    colors: KaraokeColors | None = None,
    opacity: float = 1.0,
    font_px: float = 100.0,
    target_top: float | None = None,
    gradient_line: QRectF | None = None,
) -> None:
    """在锋面底部发射水泡，并用浅蓝光柱营造前后层次。

    光柱在锋面中心（延伸到注音层），偶数号水泡画在光柱后、奇数号画在
    光柱前；每颗水泡仍是低透明填充 + 高光圆环 + 左上高光。
    """

    if params.shape != "bubble":
        return
    if rect.height() <= 0.0 or rect.width() <= 0.0:
        return
    if front < rect.left() or front > rect.right():
        return
    font = max(float(font_px), 1.0)
    outer_rgb, inner_rgb, core_rgb = _scanline_column_palette(params, colors)
    base = QColor.fromRgbF(*outer_rgb)
    rim = QColor.fromRgbF(*inner_rgb)
    highlight = QColor.fromRgbF(*core_rgb)
    pillar_color = QColor.fromRgbF(*inner_rgb)
    t = float(params.time_ms) / 1000.0
    bottom = rect.bottom() + rect.height() * 0.01
    rise = rect.height() * 1.55
    spread = max(rect.height() * 0.24, font * 0.22)
    pillar_height = rect.height() * 1.65
    pillar_top = (
        rect.center().y() - pillar_height * 0.5 - pillar_height * 0.12
    )
    pillar_bottom = pillar_top + pillar_height
    if target_top is not None and target_top < pillar_top:
        pillar_top = float(target_top)
        pillar_height = max(pillar_bottom - pillar_top, rect.height() * 1.65)
    rise = max(rise, bottom - pillar_top + font * 0.10)
    pillar_width = max(font * 0.42, rect.height() * 0.22)
    count = 18

    def draw_bubble(index: int, *, in_front: bool) -> None:
        phase = fx_unit_hash(index, 601)
        speed = 0.80 + 0.50 * fx_unit_hash(index, 602)
        cycle = math.fmod(phase + t * speed, 1.0)
        fade = min(cycle / 0.08, 1.0) * min((1.0 - cycle) / 0.20, 1.0)
        if fade <= 0.0:
            return
        depth_alpha = 1.0 if in_front else 0.58
        depth_size = 1.0 if in_front else 0.88
        x = (
            front
            + (fx_unit_hash(index, 603) - 0.5) * spread * 2.0
            + math.sin(t * 2.6 + phase * 2.0 * math.pi) * font * 0.045
        )
        y = bottom - cycle * rise
        size = (
            font
            * (0.060 + 0.060 * fx_unit_hash(index, 604))
            * (0.65 + 0.45 * cycle)
            * depth_size
        )
        if size <= 0.5:
            return
        local_base = base
        local_rim = rim
        local_highlight = highlight
        sampled = _fill_color_at(
            params.shape_fill,
            (x - rect.left()) / max(rect.width(), 1.0),
            (y - rect.top()) / max(rect.height(), 1.0),
            (
                (x - gradient_line.left()) / gradient_line.width()
                if gradient_line is not None and gradient_line.width() > 0.0
                else None
            ),
        )
        if sampled is not None:
            local_base = QColor.fromRgbF(*sampled)
            local_rim = QColor.fromRgbF(
                *np.clip(
                    np.asarray(sampled)
                    + (1.0 - np.asarray(sampled)) * 0.30,
                    0.0,
                    1.0,
                )
            )
            local_highlight = QColor.fromRgbF(
                *np.clip(
                    np.asarray(sampled)
                    + (1.0 - np.asarray(sampled)) * 0.60,
                    0.0,
                    1.0,
                )
            )
        fill = QColor(local_base)
        fill.setAlpha(int(70 * fade * depth_alpha))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(fill)
        painter.drawEllipse(QPointF(x, y), size, size)
        rim_color = QColor(local_rim)
        rim_color.setAlpha(int(205 * fade * depth_alpha))
        pen = painter.pen()
        pen.setColor(rim_color)
        pen.setWidthF(max(0.8, size * 0.10))
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(QPointF(x, y), size, size)
        highlight_color = QColor(local_highlight)
        highlight_color.setAlpha(int(215 * fade * depth_alpha))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(highlight_color)
        painter.drawEllipse(
            QPointF(x - size * 0.32, y - size * 0.32),
            size * 0.18,
            size * 0.18,
        )

    def draw_light_pillar() -> None:
        # 浅蓝光柱：竖向透明→亮→透明渐变，叠加宽/中/窄三层模拟柔边。
        pillar_pulse = 0.90 + 0.10 * math.sin(t * 3.2)
        for scale, alpha_scale in ((1.0, 0.40), (0.58, 0.65), (0.24, 0.50)):
            width = max(pillar_width * scale * pillar_pulse, 2.0)
            gradient = QLinearGradient(
                0.0, pillar_top, 0.0, pillar_top + pillar_height
            )
            for stop, alpha in (
                (0.00, 0),
                (0.14, 18),
                (0.50, 48),
                (0.86, 18),
                (1.00, 0),
            ):
                color = QColor(pillar_color)
                color.setAlpha(int(alpha * alpha_scale))
                gradient.setColorAt(stop, color)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(gradient)
            painter.drawRoundedRect(
                QRectF(
                    front - width * 0.5,
                    pillar_top,
                    width,
                    pillar_height,
                ),
                width * 0.5,
                width * 0.5,
            )

    def draw_scanline_core() -> None:
        # 水泡形态仍保留一条水蓝色高亮扫字线，位于锋面正中、正文高度，
        # 光柱只做背景，不能取代这条扫字线。
        core_width = max(1.6, font * 0.035)
        top = rect.top()
        height = rect.height()
        for scale, alpha, color in (
            (4.6, 34, base),
            (2.2, 92, highlight),
            (1.0, 232, highlight),
        ):
            width = max(core_width * scale, 1.0)
            layer = QColor(color)
            layer.setAlpha(alpha)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(layer)
            painter.drawRoundedRect(
                QRectF(front - width * 0.5, top, width, height),
                width * 0.5,
                width * 0.5,
            )

    painter.save()
    try:
        painter.setOpacity(max(0.0, min(painter.opacity() * opacity, 1.0)))
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        # 后层水泡 → 光柱 → 前层水泡，形成前后遮挡的立体感。
        for index in range(0, count, 2):
            draw_bubble(index, in_front=False)
        draw_light_pillar()
        # 中心高亮棒由普通扫字线带按字形绘制；这里不再画直线芯。
        for index in range(1, count, 2):
            draw_bubble(index, in_front=True)
    finally:
        painter.restore()


def paint_scanline_column(
    painter: QPainter,
    rect: QRectF,
    *,
    front: float,
    params: ScanlineParams,
    colors: KaraokeColors | None = None,
    opacity: float = 1.0,
    font_px: float = 100.0,
    target_top: float | None = None,
    gradient_line: QRectF | None = None,
) -> None:
    """在锋面处绘制「烈焰棒」或发射水泡（替换普通高亮带）。

    烈焰棒是扫字线本体变成一根细长发光棒，两侧逐帧喷出火舌；水泡是从
    锋面底部向上冒出的镂空水泡粒子。两者都不画普通高亮带。
    """

    if params.shape not in {"flame", "bubble"}:
        return
    if rect.height() <= 0.0 or rect.width() <= 0.0:
        return
    if front < rect.left() or front > rect.right():
        return
    if params.shape == "bubble":
        paint_scanline_bubbles(
            painter,
            rect,
            front=front,
            params=params,
            colors=colors,
            opacity=opacity,
            font_px=font_px,
            target_top=target_top,
            gradient_line=gradient_line,
        )
        return
    font = max(float(font_px), 1.0)
    # 烈焰棒：柱体高度覆盖正文与注音最外沿；呼吸只改外圈半径。
    base_width = max(
        params.width_px * 4.0, rect.height() * 0.70
    )
    base_height = max(rect.height() * 1.65, base_width * 2.7)
    column_width = base_width
    column_height = base_height
    left = front - column_width * 0.5
    # 向上延伸覆盖注音层；target_top 由 ruby 最外沿决定，避免差一截。
    top = rect.center().y() - column_height * 0.5 - column_height * 0.12
    bottom = top + column_height
    if target_top is not None and target_top < top:
        top = float(target_top)
        column_height = max(bottom - top, base_height)
        bottom = top + column_height
    image = render_scanline_rod_image(
        params,
        colors,
        gradient_target=QRectF(left, top, column_width, column_height),
        gradient_line=gradient_line or rect,
    )
    painter.save()
    try:
        painter.setOpacity(max(0.0, min(painter.opacity() * opacity, 1.0)))
        painter.drawImage(
            QRectF(left, top, column_width, column_height), image
        )
    finally:
        painter.restore()


def paint_scanline_strip(
    painter: QPainter,
    path: QPainterPath,
    rect: QRectF,
    *,
    front: float,
    params: ScanlineParams,
    style: Style,
    vertical: bool = False,
    rtl: bool = False,
    colors: KaraokeColors | None = None,
    opacity: float = 1.0,
    allow_column: bool = True,
) -> None:
    """Paint one glyph path's scan-line highlight band centred at ``front``.

    普通高亮带会在字形内部重绘；``params.shape`` 为烈焰棒/水泡时**不画
    高亮带**，改为在锋面处画一幅程序化柱状图样（``allow_column=False`` 的
    注音调用不画，避免与主文字柱重复）。
    With a feather radius (``scanline_glow_px``) the band is painted from a
    blurred silhouette of the glyph stack: the highlight eases out of the
    stroke outline by the blur radius while closed counters stay transparent
    (the blurred source keeps its holes, and the radius is small relative to
    counter sizes). With ``scanline_glow_px == 0`` the band repaints the exact
    glyph geometry, so nothing ever expands outside it.
    ``path`` must already be in device space when a utopia transform is active;
    pass the mapped ``front`` accordingly (see
    :func:`map_front_through_transform`).
    """

    opacity = max(0.0, min(opacity, 1.0))
    if opacity <= 0.0:
        return
    if params.suppress_strip:
        # 自定义图片形态关闭「文字高亮」：普通高亮带整体不画（主文字与
        # 注音的所有调用点共用本闸门）；素材缺失时 suppress 为 False，
        # 高亮带自动回归（回退普通高光模式）。
        return
    if params.is_column_shape:
        # 柱体形态的中心高亮仍由普通扫字线带绘制：完全走字形 geometry /
        # feather / 角色配色。外围火圈/水泡由 painter 在 ruby 之后补画，
        # 这里不能直接 return，否则中心棒会丢掉字形裁剪。
        pass
    stroke_width = style.stroke_width_px
    stroke2_width = main_stroke2_width(style)
    band = scanline_band_rect(rect, front, params, vertical=vertical)
    if not band_touches_rect(band, path.boundingRect(), 0.0):
        return
    if params.is_brighten and params.brightness <= 0.0:
        return
    follow_state = params.follow_state
    if params.is_brighten and colors is not None:
        states = (
            (False, _brighten_state_hsv(colors.before, params.brightness)),
            (True, _brighten_state_hsv(colors.after, params.brightness)),
        )
    elif follow_state is not None and colors is not None:
        # 跟随字体：与底色发光同一条通路（当前行实际配色、全层重绘、共用
        # 亮度提升），但整条带固定用一态——锋面前侧被提前染成走字后色
        # （follow_after）或后侧暂回走字前色（follow_before）。亮度 0 =
        # 原样颜色，依旧有视觉差异，因此不做 brighten 那种零亮度跳过。
        state = _brighten_state_hsv(
            colors.after if follow_state == "after" else colors.before,
            params.brightness,
        )
        states = ((False, state), (True, state))
    else:
        solid = _solid_scanline_state(params.color)
        states = ((False, solid), (True, solid))
    if params.is_role:
        # 复用配色方案：取来源「走字后-主文字」这一份填充，整条带（含描边
        # 在内的全层）统一用它重绘——与单独颜色模式的全层同色结构一致。
        state = _uniform_scanline_state(params.role_fill)
        states = ((False, state), (True, state))
    if params.glow_px > 0:
        _paint_adjustment_strip(
            painter,
            path,
            rect,
            front=front,
            params=params,
            style=style,
            vertical=vertical,
            rtl=rtl,
            opacity=opacity,
            layer_states=states,
        )
        return
    for after, state in states:
        painter.save()
        try:
            painter.setOpacity(max(painter.opacity() * opacity, 0.0))
            painter.setClipRect(
                _state_clip(rect, front, vertical=vertical, rtl=rtl, after=after)
            )
            _paint_strip_body(
                painter,
                path,
                rect,
                band=band,
                state=state,
                params=params,
                style=style,
                stroke_width=stroke_width,
                stroke2_width=stroke2_width,
                vertical=vertical,
                front=front,
            )
        finally:
            painter.restore()


def _paint_adjustment_strip(
    painter: QPainter,
    path: QPainterPath,
    rect: QRectF,
    *,
    front: float,
    params: ScanlineParams,
    style: Style,
    vertical: bool,
    opacity: float,
    layer_states: list[tuple[bool, KaraokeColorState]] | None,
    rtl: bool = False,
) -> None:
    """Adjustment-layer band: colour crossfade eased by a continuous mask.

    PR-style mask feather: the band is an adjustment whose strength fades
    with a continuous smoothstep along the sweep axis. Every pixel's **RGB**
    lerps between the original and the repainted stack while its **alpha
    stays exactly the original's** — a feathered adjustment never changes
    opacity, so translucent source colours cannot make the band look more
    transparent than the surrounding glyphs (the repaint's own alpha would
    otherwise drag it down: the original composites extra layers beneath the
    glyph). Anti-aliased edges keep their coverage; closed counters stay
    transparent; nothing expands outside the glyph geometry.
    """

    device = painter.device()
    if not isinstance(device, QImage):
        # 同步 QPainter 预览把字幕直接画在 QWidget / QPixmap 上。这里不能
        # 读回目标像素做「保留 alpha 的 RGB crossfade」，但可以用 SourceAtop
        # 达到同一效果：源图自身带羽化 alpha，合成时目标 alpha 原样保留，
        # 字形外的像素完全不动。旧实现遇到非 QImage 直接 return，导致
        # CPU 同步预览（以及 widget.grab()）整条扫字线透明。
        _paint_adjustment_strip_widget(
            painter,
            path,
            rect,
            front=front,
            params=params,
            style=style,
            vertical=vertical,
            opacity=opacity,
            layer_states=layer_states,
            rtl=rtl,
        )
        return
    stroke_width = style.stroke_width_px
    stroke2_width = main_stroke2_width(style)
    stroke_pad = float(max(stroke_width, stroke2_width) + 2)
    bounds = path.boundingRect()
    band = scanline_band_rect(rect, front, params, vertical=vertical)
    layer_rect = band.intersected(
        bounds.adjusted(-stroke_pad, -stroke_pad, stroke_pad, stroke_pad)
    ).toAlignedRect()
    if layer_rect.isEmpty():
        return
    width = max(layer_rect.width(), 1)
    height = max(layer_rect.height(), 1)

    # A：扫字线版（不透明全层），前后态按锋面分色（role 两侧同 state）。
    adjusted = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
    adjusted.fill(Qt.GlobalColor.transparent)
    offset_x = float(layer_rect.left())
    offset_y = float(layer_rect.top())
    local_path = QPainterPath(path)
    local_path.translate(-offset_x, -offset_y)
    local_rect = rect.translated(-offset_x, -offset_y)
    local_front = front - (offset_y if vertical else offset_x)
    source_painter = QPainter(adjusted)
    try:
        source_painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if layer_states is not None:
            for after, state in layer_states:
                source_painter.save()
                try:
                    source_painter.setClipRect(
                        _state_clip(
                            local_rect,
                            local_front,
                            vertical=vertical,
                            rtl=rtl,
                            after=after,
                        )
                    )
                    if stroke2_width > 0:
                        paint_stroke_path(
                            source_painter,
                            local_path,
                            state.stroke2,
                            local_rect,
                            stroke2_pen_width(stroke_width, stroke2_width),
                        )
                    if stroke_width > 0:
                        paint_stroke_path(
                            source_painter,
                            local_path,
                            state.stroke,
                            local_rect,
                            stroke_pen_width(stroke_width),
                        )
                    paint_fill_path(source_painter, local_path, state.text, local_rect)
                finally:
                    source_painter.restore()
    finally:
        source_painter.end()

    # D：同区域的已合成原画面（含原始字形与下层内容）。混合模式只在
    # 预乘格式上定义良好，目标画布（RGBA8888 直通）必须先转换。
    # ``layer_rect`` 是逻辑坐标；高 DPI 预览的 QImage 是物理像素
    # （``setDevicePixelRatio``），直接按逻辑矩形 copy 会取到错位/空白
    # 区域，RGB crossfade 后整条带 alpha 变 0——4K + Windows 150%/200%
    # 缩放下 CPU 预览扫字线完全透明的根因。这里用 painter 的逻辑→设备
    # 变换取真实区域，再缩回逻辑尺寸与 adjusted 对齐。
    transform = painter.deviceTransform()
    if transform.isIdentity():
        copy_rect = layer_rect
    else:
        copy_rect = transform.mapRect(QRectF(layer_rect)).toAlignedRect()
    copy_rect = copy_rect.intersected(device.rect())
    if copy_rect.isEmpty():
        return
    original = device.copy(copy_rect).convertToFormat(
        QImage.Format.Format_ARGB32_Premultiplied
    )
    if original.size() != adjusted.size():
        original = original.scaled(
            adjusted.size(),
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )

    # 颜色 crossfade、alpha 保持原画面（半透明配色不会把带内压得更透明）。
    composite = _crossfade_keep_alpha(
        adjusted, original, params, local_front, vertical, opacity
    )
    painter.save()
    try:
        painter.setClipRect(layer_rect)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
        painter.fillRect(layer_rect, Qt.GlobalColor.transparent)
        painter.setCompositionMode(
            QPainter.CompositionMode.CompositionMode_SourceOver
        )
        painter.drawImage(QPointF(offset_x, offset_y), composite)
    finally:
        painter.restore()


def _paint_adjustment_strip_widget(
    painter: QPainter,
    path: QPainterPath,
    rect: QRectF,
    *,
    front: float,
    params: ScanlineParams,
    style: Style,
    vertical: bool,
    opacity: float,
    layer_states: list[tuple[bool, KaraokeColorState]] | None,
    rtl: bool,
) -> None:
    """Non-QImage devices: recolour the glyph stack with SourceAtop.

    SourceAtop keeps the destination alpha exactly (anti-aliased edges and
    translucent glyph colours are untouched) while replacing the source RGB
    inside the glyph silhouette. That is the same visual contract as the
    image-device crossfade path, without requiring a readback of the target.
    """

    stroke_width = style.stroke_width_px
    stroke2_width = main_stroke2_width(style)
    stroke_pad = float(max(stroke_width, stroke2_width) + 2)
    bounds = path.boundingRect()
    band = scanline_band_rect(rect, front, params, vertical=vertical)
    layer_rect = band.intersected(
        bounds.adjusted(-stroke_pad, -stroke_pad, stroke_pad, stroke_pad)
    ).toAlignedRect()
    if layer_rect.isEmpty():
        return
    width = max(layer_rect.width(), 1)
    height = max(layer_rect.height(), 1)
    adjusted = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
    adjusted.fill(Qt.GlobalColor.transparent)
    offset_x = float(layer_rect.left())
    offset_y = float(layer_rect.top())
    local_path = QPainterPath(path)
    local_path.translate(-offset_x, -offset_y)
    local_rect = rect.translated(-offset_x, -offset_y)
    local_front = front - (offset_y if vertical else offset_x)
    source_painter = QPainter(adjusted)
    try:
        source_painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if layer_states is not None:
            for after, state in layer_states:
                source_painter.save()
                try:
                    source_painter.setClipRect(
                        _state_clip(
                            local_rect,
                            local_front,
                            vertical=vertical,
                            rtl=rtl,
                            after=after,
                        )
                    )
                    if stroke2_width > 0:
                        paint_stroke_path(
                            source_painter,
                            local_path,
                            state.stroke2,
                            local_rect,
                            stroke2_pen_width(stroke_width, stroke2_width),
                        )
                    if stroke_width > 0:
                        paint_stroke_path(
                            source_painter,
                            local_path,
                            state.stroke,
                            local_rect,
                            stroke_pen_width(stroke_width),
                        )
                    paint_fill_path(source_painter, local_path, state.text, local_rect)
                finally:
                    source_painter.restore()
    finally:
        source_painter.end()

    strength = _feather_strength_2d(
        width, height, local_front, params, vertical, opacity
    )
    feathered = _scale_premultiplied_alpha(adjusted, strength)
    painter.save()
    try:
        painter.setClipRect(layer_rect)
        painter.setCompositionMode(
            QPainter.CompositionMode.CompositionMode_SourceAtop
        )
        painter.drawImage(QPointF(offset_x, offset_y), feathered)
    finally:
        painter.restore()


def _feather_strength_2d(
    width: int,
    height: int,
    front: float,
    params: ScanlineParams,
    vertical: bool,
    opacity: float,
) -> np.ndarray:
    """Per-pixel feather strength with the shared ordered dither."""

    count = height if vertical else width
    columns = _feather_strength(count, front, params) * opacity
    np.clip(columns, 0.0, 1.0, out=columns)
    strength = (
        np.repeat(columns.astype(np.float32), width).reshape(height, width)
        if vertical
        else np.broadcast_to(columns.astype(np.float32), (height, width))
    )
    tile = _DITHER_MATRIX.shape[0]
    dither = np.tile(
        _DITHER_MATRIX, ((height + tile - 1) // tile, (width + tile - 1) // tile)
    )[:height, :width]
    return np.clip(strength + dither / 255.0, 0.0, 1.0)


def _scale_premultiplied_alpha(image: QImage, strength: np.ndarray) -> QImage:
    """Multiply premultiplied RGBA by a feather mask (SourceAtop source)."""

    width, height = image.width(), image.height()
    bits = image.constBits()
    bits.setsize(image.sizeInBytes())
    stride = image.bytesPerLine() // 4
    pixels = np.frombuffer(bytes(bits), dtype=np.uint8).reshape(
        image.height(), stride, 4
    )[:, :width].astype(np.float32)
    pixels *= strength[:, :, None]
    out = np.clip(pixels + 0.5, 0.0, 255.0).astype(np.uint8)
    return QImage(
        out.tobytes(),
        width,
        height,
        width * 4,
        QImage.Format.Format_ARGB32_Premultiplied,
    ).copy()


def _bayer_matrix(size: int) -> np.ndarray:
    """Recursively build a Bayer ordered-dither matrix (power of two)."""

    matrix = np.array([[0.0]], dtype=np.float32)
    base = np.array([[0.0, 2.0], [3.0, 1.0]], dtype=np.float32)
    n = 1
    while n < size:
        n *= 2
        matrix = np.block(
            [
                [4 * matrix + base[0, 0], 4 * matrix + base[0, 1]],
                [4 * matrix + base[1, 0], 4 * matrix + base[1, 1]],
            ]
        )
    return matrix / float(n * n) - 0.5


_DITHER_MATRIX = _bayer_matrix(16)
"""Ordered-dither offsets in [-0.5, 0.5): break 8-bit quantisation plateaus
into high-frequency texture instead of visible banding. 16x8/16x16 keeps the
pattern's own periodicity below the eye's structure threshold even on the
gentlest falloff slopes and under preview scaling."""


def _feather_strength(
    count: int, front: float, params: ScanlineParams
) -> np.ndarray:
    """Per-index crossfade strength along the sweep axis (continuous)."""

    softness = min(max(float(params.glow_px), 0.0), params.half_width)
    core = params.half_width - softness
    columns = np.empty(count, dtype=np.float32)
    for index in range(count):
        distance = abs(index + 0.5 - front)
        if softness <= 0.0 or distance <= core:
            strength = 1.0
        else:
            progress = max(0.0, min((params.half_width - distance) / softness, 1.0))
            strength = progress * progress * (3.0 - 2.0 * progress)
        columns[index] = max(0.0, min(strength, 1.0))
    return columns


def _crossfade_keep_alpha(
    adjusted: QImage,
    original: QImage,
    params: ScanlineParams,
    front: float,
    vertical: bool,
    opacity: float,
) -> QImage:
    """RGB lerp between two premultiplied layers; alpha comes from original."""

    width, height = adjusted.width(), adjusted.height()

    def straight(image: QImage):
        bits = image.constBits()
        bits.setsize(image.sizeInBytes())
        stride = image.bytesPerLine() // 4
        px = np.frombuffer(
            bytes(bits), dtype=np.uint8
        ).reshape(image.height(), stride, 4)[:, :image.width()].astype(np.float32)
        premult = px / 255.0
        alpha = premult[:, :, 3]
        rgb = np.divide(
            premult[:, :, :3],
            alpha[:, :, None],
            out=np.zeros_like(premult[:, :, :3]),
            where=alpha[:, :, None] > 0.0,
        )
        return rgb, alpha

    adjust_rgb, adjust_alpha = straight(adjusted)
    original_rgb, original_alpha = straight(original)
    # 有序抖动：羽化低斜率区的 8bit 量化平台会显成一条条竖线，抖动把
    # 平台打散成人眼不敏感的高频纹理。
    strength = _feather_strength_2d(width, height, front, params, vertical, opacity)
    # 重绘层未覆盖的像素（其 alpha 为 0）保持原画面。
    effective = strength * (adjust_alpha > 0.0)
    out_rgb = (
        original_rgb * (1.0 - effective[:, :, None])
        + adjust_rgb * effective[:, :, None]
    )
    out = np.empty((height, width, 4), dtype=np.uint8)
    out[:, :, :3] = np.clip(
        out_rgb * original_alpha[:, :, None] * 255.0 + 0.5, 0.0, 255.0
    )
    out[:, :, 3] = np.clip(original_alpha * 255.0 + 0.5, 0.0, 255.0)
    result = QImage(
        out.tobytes(), width, height, width * 4,
        QImage.Format.Format_ARGB32_Premultiplied,
    )
    return result.copy()


def _paint_strip_body(
    painter: QPainter,
    path: QPainterPath,
    rect: QRectF,
    *,
    band: QRectF,
    state: KaraokeColorState,
    params: ScanlineParams,
    style: Style,
    stroke_width: int,
    stroke2_width: int,
    vertical: bool,
    front: float,
) -> None:
    del band, style
    for slice_rect, alpha in scanline_feather_slices(
        rect, front, params, vertical=vertical
    ):
        painter.save()
        try:
            painter.setOpacity(painter.opacity() * alpha)
            painter.setClipRect(slice_rect)
            if stroke2_width > 0:
                paint_stroke_path(
                    painter,
                    path,
                    state.stroke2,
                    rect,
                    stroke2_pen_width(stroke_width, stroke2_width),
                )
            if stroke_width > 0:
                paint_stroke_path(
                    painter,
                    path,
                    state.stroke,
                    rect,
                    stroke_pen_width(stroke_width),
                )
            paint_fill_path(painter, path, state.text, rect)
        finally:
            painter.restore()


def map_front_through_transform(
    front: float,
    baseline_y: float,
    transform: QTransform | None,
) -> float:
    """Map a logical wipe-front x through a utopia character transform."""

    if transform is None or transform.isIdentity():
        return front
    return float(transform.map(QPointF(front, baseline_y)).x())


def main_scanline_front(
    segments: list,
    t_ms: int,
    rtl: bool,
) -> float | None:
    """Return the main-text wipe front, or ``None`` when no front is moving.

    ``None`` covers "nothing sung yet", "the whole line is complete" and the
    timing gaps in between: the highlight only exists while the front travels,
    so a front resting at a finished segment's endpoint between gaps draws
    nothing. Segment boundary frames (``t == start/end``) still count as
    travelling — the hand-off arrival keeps its band (相控口径，与逐单元注音
    扫字线 / GPU 的逐字相判定一致).
    """

    band = fill_clip_band(segments, t_ms, rtl)
    if band is None:
        return None
    if all(segment_fill_ratio(segment, t_ms) >= 1.0 for segment in segments):
        return None
    if not any(
        int(segment.start_ms) <= t_ms <= int(segment.end_ms)
        for segment in segments
    ):
        return None
    return float(band[0] if rtl else band[1])


__all__ = [
    "SCANLINE_GLOBAL_ROLE_KEY",
    "ScanlineParams",
    "band_touches_rect",
    "main_scanline_front",
    "map_front_through_transform",
    "paint_scanline_column",
    "paint_scanline_bubbles",
    "paint_scanline_strip",
    "render_scanline_column_image",
    "render_scanline_rod_image",
    "scanline_band_rect",
    "scanline_column_base_color",
    "scanline_feather_slices",
    "scanline_params_for_style",
    "scanline_role_fill",
]
