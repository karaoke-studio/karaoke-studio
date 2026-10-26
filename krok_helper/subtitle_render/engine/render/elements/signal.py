"""Sayatoo signal-cue geometry and time-state contracts."""

from __future__ import annotations

import math
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from threading import Lock
from typing import Hashable, Protocol

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import (
    QBrush,
    QColor,
    QFontMetrics,
    QImage,
    QPainter,
    QPainterPath,
    QPen,
    QPolygonF,
)

from krok_helper.subtitle_render.engine.layout.line.style import (
    line_end_ms,
    line_start_ms,
)
from krok_helper.subtitle_render.engine.layout.display.signal import (
    lit_signal_head_context,
    volume_signal_head_context,
)
from krok_helper.subtitle_render.engine.render.core.layers import (
    BakedLayer,
    LayerAnimation,
    LayerCompositor,
    LayerContext,
    SCOPE_LINE,
)
from krok_helper.subtitle_render.engine.render.image_resource import (
    image_file_signature,
)
from krok_helper.subtitle_render.domain.models import (
    Style,
    effective_karaoke_zoom_pulse,
    resolve_lit_appearance,
    resolve_volume_appearance,
)
from krok_helper.subtitle_render.engine.render.elements.horizontal.transitions import (
    character_transform,
    line_char_transition_context,
    transition_char_state,
    transition_char_glow,
    zoom_pulse_curve_level,
    zoom_pulse_wipe_scale,
)
from krok_helper.subtitle_render.engine.render.effects.metrics import (
    glow_extent,
    glow_radius,
    main_stroke2_width,
)
from krok_helper.subtitle_render.engine.render.effects.raster import (
    paint_text_layer_stack,
    paint_transition_glow_halo,
)
from krok_helper.subtitle_render.engine.layout.layout_context import _LAYOUT_PASS
from krok_helper.subtitle_render.engine.style.style_semantics import (
    appearance_role_source,
    auto_appearance_basis,
    effective_karaoke_colors,
    style_for_role,
)
from krok_helper.subtitle_render.engine.timing.timeline import DisplayLine
from krok_helper.subtitle_render.domain.timing import TimingLine, TimingTrack


# 每根柱的逐字动画状态：与文字字符同构的
# (opacity, dx, dy, rotation, scale_x, scale_y, skew_y)。
BarAnimationState = tuple[float, float, float, float, float, float, float]


@dataclass(frozen=True)
class SignalLitGroup:
    x: float
    y: float
    elapsed_ms: int
    duration_ms: int
    active_index: int | None
    opacity: float = 1.0
    active_opacity: float = 1.0
    dx: float = 0.0
    dy: float = 0.0
    phase: float = 0.0
    # 段首行入退场动画（与正文同一 line_animation_state）：柱体/灯组
    # 跟随文字一起位移与淡入淡出，native 侧在行级 OpacityLayer 内绘制。
    anim_dx: float = 0.0
    anim_dy: float = 0.0
    anim_opacity: float = 1.0
    # 逐字入退场动画（utopia / char_fade / char_drip / spin_flip）按柱展开：
    # 第 i 根柱 behaves 相当于该行第 i 个字符，与文字同窗口、同曲线、同
    # 交错节奏地入场/退场；None = 无激活的逐字动画（纯行级动画路径）。
    bar_animations: tuple[BarAnimationState, ...] | None = None
    # auto 档的装饰源样式：段首行第一个角色（无角色时为该行样式）的有效
    # 配色；role 档换成按名解析的固定方案。几何/尺寸仍来自全局投影样式；
    # None = 未解析（用组样式兜底）。
    bar_style: Style | None = None
    bar_glows: tuple[tuple[float, float, bool] | None, ...] = ()
    # 「真一组」横向渐变跨度（画布坐标）：柱组外接框 ∪ 段首行第一角色
    # 墨迹跨度的并集，auto/role 档柱体横向渐变画刷取此跨度——柱与正文
    # 第一角色共用同一条渐变带（正文侧由行布局把第一角色跨度左缘拓宽
    # 到柱左缘，见 horizontal/layout 的 signal_band_left）。None = 不参与
    # （非音量柱 / 无 band 布局），横向渐变回落柱组自身外接框。
    fill_band_rect: QRectF | None = None


@dataclass(frozen=True)
class SignalLayoutMetrics:
    count: int
    size: int
    item_width: int
    tracking: int
    stroke_extent: float
    group_width: float
    is_volume: bool


@dataclass(frozen=True)
class VolumeSignalGeometry:
    count: int
    size: int
    column_width: int
    column_spacing: int
    stroke_extent: float
    local_left: float
    group_width: float
    pitch: float
    front_height: float
    height_delta: float
    align_base_shift: float
    align_delta_shift: float


class SignalLineLayout(Protocol):
    baseline_y: int
    line_style: Style
    metrics: QFontMetrics
    total_w: int
    signal_x: float | None
    signal_y: float | None


@dataclass(frozen=True)
class SignalLineMeasurement:
    baseline_y: int
    line_style: Style
    metrics: QFontMetrics
    total_w: int
    signal_x: float | None = None
    signal_y: float | None = None


SignalLineMeasurer = Callable[
    [TimingTrack, DisplayLine, Mapping[int, int], int, Style],
    SignalLineMeasurement,
]


def volume_style(style: Style, *, auto_basis: Style | None = None) -> Style:
    """Project independent volume controls onto the legacy signal renderer.

    auto 外观模式在这里先行物化（大小/颜色跟随主文字），保证布局 union、
    绘制与 native IR（render_ir 同样经过 resolve_volume_appearance）三处
    消费到同一组数值。``auto_basis`` 是「跟随字体」推导源（主轨信号宿主
    行最高频首角色方案），与 render_ir / painter 布局入口同源传入。
    """
    style = resolve_volume_appearance(style, auto_basis=auto_basis)
    return replace(
        style,
        lit_enabled=True,
        lit_style="volume",
        signals_duration_ms=style.volume_duration_ms,
        lit_waiting_time_ms=style.volume_waiting_time_ms,
        lit_time_offset_ms=style.volume_time_offset_ms,
        lit_stroke_width=style.volume_stroke_width,
        lit_opacity_pct=style.volume_opacity_pct,
    )


def set_signal_auto_basis(style: Style, primary_track: TimingTrack | None) -> None:
    """渲染入口登记「跟随字体」大小推导基准（主轨信号宿主行口径）。

    基准 = 主轨段首行最高频首角色方案叠加全局样式（与 auto 档装饰源
    「段首行第一个角色」同构，见 ``auto_appearance_basis``）。只在排版
    区间内生效（区间外是独立工具调用，回退全局主样式旧口径）。基准以
    ``id(style)`` 为键：入口做完输出高度换算后传入换算后的 style 对象，
    深处的物化点用同一个对象取值。每帧一次计数，区间内复用（宿主行判定
    命中 ``signal_heads`` 同一缓存）。
    """
    cache = getattr(_LAYOUT_PASS, "signal_auto_basis", None)
    if cache is None:
        return
    basis = auto_appearance_basis(style, primary_track)
    cache[id(style)] = basis
    _LAYOUT_PASS.signal_auto_basis_refs.append(style)


def signal_auto_basis(style: Style) -> Style | None:
    """读取当前排版区间登记的推导基准；未登记返回 ``None``（旧口径）。"""
    cache = getattr(_LAYOUT_PASS, "signal_auto_basis", None)
    if cache is None:
        return None
    return cache.get(id(style))


def signal_stroke_extent(style: Style, *, is_volume: bool) -> float:
    stroke_width = max(int(style.lit_stroke_width), 0)
    soften = 0 if is_volume else max(int(style.lit_stroke_soften), 0)
    return float(stroke_width + soften)


def volume_signal_geometry(style: Style) -> VolumeSignalGeometry:
    # 列距口径：每列单元在自己的柱体两侧各预留一份描边厚度（pitch 含
    # 2*stroke_extent），相邻柱的描边外缘间隔恰为 column_spacing。C++ 侧
    # signal_state.cpp 的 volumeSignalGeometry 必须逐项镜像本公式，否则
    # union 宽度（→文字对齐）与列间距会在两后端分歧。
    count = max(1, min(int(style.volume_column_count), 16))
    size = max(int(style.volume_size), 1)
    column_width = max(int(style.volume_column_width), 1)
    column_spacing = max(int(style.volume_column_spacing), 0)
    stroke_extent = signal_stroke_extent(style, is_volume=True)
    pitch = float(column_width + column_spacing + 2 * stroke_extent)
    local_left = float(style.volume_offset_x) - stroke_extent
    group_width = float(count * pitch - column_spacing)

    ratio = max(float(style.volume_ratio), 0.01)
    base_factor = ratio
    depth_factor = 1.0
    if 1.0 < ratio:
        depth_factor = 1.0 / ratio
        base_factor = 1.0
    front_height = base_factor * size
    height_delta = (
        0.0
        if count < 2
        else ((depth_factor - base_factor) * size) / float(count - 1)
    )
    align_base_shift = 0.0
    align_delta_shift = 0.0
    align = int(style.volume_align)
    if align == 1:
        align_base_shift = (1.0 - base_factor) * size * 0.5
        align_delta_shift = -height_delta * 0.5
    elif align == 2:
        align_base_shift = (1.0 - base_factor) * size
        align_delta_shift = -height_delta

    return VolumeSignalGeometry(
        count=count,
        size=size,
        column_width=column_width,
        column_spacing=column_spacing,
        stroke_extent=stroke_extent,
        local_left=local_left,
        group_width=group_width,
        pitch=pitch,
        front_height=front_height,
        height_delta=height_delta,
        align_base_shift=align_base_shift,
        align_delta_shift=align_delta_shift,
    )


def inline_volume_geometry(
    track: TimingTrack, line: TimingLine, style: Style,
) -> tuple[Style, VolumeSignalGeometry] | None:
    """Resolve the stable inline module once, independent of frame opacity."""
    if style.vertical or not (
        style.volume_enabled or (style.lit_enabled and style.lit_style == "volume")
    ):
        return None
    heads = volume_signal_head_context(track, style)
    index = next((i for i, candidate in enumerate(track.lines) if candidate is line), None)
    if heads is None or index not in heads:
        return None
    active = (
        volume_style(style, auto_basis=signal_auto_basis(style))
        if style.volume_enabled else style
    )
    if active.signals_duration_ms - max(active.lit_waiting_time_ms, 0) <= 0:
        return None
    return active, volume_signal_geometry(active)


def volume_signal_column_rects(
    x: float,
    y: float,
    geometry: VolumeSignalGeometry,
) -> list[QRectF]:
    return [
        QRectF(
            float(x + geometry.stroke_extent + index * geometry.pitch),
            float(
                y
                + geometry.stroke_extent
                + geometry.align_base_shift
                + index * geometry.align_delta_shift
            ),
            float(geometry.column_width),
            float(max(geometry.front_height + index * geometry.height_delta, 1.0)),
        )
        for index in range(geometry.count)
    ]


def signal_layout_metrics(style: Style) -> SignalLayoutMetrics:
    is_volume = style.lit_style == "volume"
    if is_volume:
        geometry = volume_signal_geometry(style)
        count = geometry.count
        size = geometry.size
        tracking = geometry.column_spacing
        item_width = geometry.column_width
        stroke_extent = geometry.stroke_extent
        group_width = geometry.group_width
    else:
        count = max(1, min(int(style.lit_number), 8))
        size = max(int(style.lit_size), 1)
        tracking = max(int(style.lit_tracking), 0)
        item_width = size
        stroke_extent = signal_stroke_extent(style, is_volume=False)
        group_width = count * size + max(count - 1, 0) * (size * 0.5 + tracking)
    return SignalLayoutMetrics(
        count=count,
        size=size,
        item_width=item_width,
        tracking=tracking,
        stroke_extent=stroke_extent,
        group_width=float(group_width),
        is_volume=is_volume,
    )


def line_has_active_signal(
    line: TimingLine,
    t_ms: int,
    style: Style,
    *,
    is_signal_head: bool = True,
    display_start_ms: int | None = None,
    display_end_ms: int | None = None,
) -> bool:
    if not is_signal_head:
        return False
    duration = max(int(style.signals_duration_ms), 0)
    active_duration = max(duration - max(int(style.lit_waiting_time_ms), 0), 0)
    if active_duration <= 0:
        return False
    signal_end = line_start_ms(line) + int(style.lit_time_offset_ms)
    active_start = signal_end - active_duration
    # union 窗口必须与柱体可见窗口共用同一显示终点（display_end_ms）；直接
    # 用 line_end + tail 会在「拖过消失时间 / 同步退场延长」的延长段里让文字
    # 先退回单独锚定，而柱体仍按 union 框绘制，重叠或跳到视口左边距。
    display_end = (
        int(display_end_ms)
        if display_end_ms is not None
        else line_end_ms(line) + max(int(style.line_tail_ms), 0)
    )
    # 可见下界 = 所在行显示窗起点（display_start_ms）：特效随所在行一并
    # 出现/入场；动画时间轴（闪烁/填充/逐个熄灭）仍按 active_start 锚定，
    # 在动画开始前显示初始状态（满灯 / 初帧柱体）。缺省回退 active_start
    # 保持旧行为口径。
    display_start = (
        int(display_start_ms)
        if display_start_ms is not None
        else active_start
    )
    # 显示窗口统一半开区间 [start, end)，与 resolve_display_lines /
    # native 侧 tMs >= displayEndMs 同口径；含端点会让 CPU 在窗口终点
    # 那一帧比 GPU 多画一帧灯。
    return display_start <= t_ms < display_end


def signal_local_x(metrics: SignalLayoutMetrics, style: Style) -> float:
    if metrics.is_volume:
        return float(style.volume_offset_x) - metrics.group_width
    return float(style.lit_offset_x)


def signal_offset_x(style: Style) -> float:
    """Return the user X offset, which moves only the active indicator."""
    return float(
        style.volume_offset_x if style.lit_style == "volume" else style.lit_offset_x
    )


def signal_lit_y(
    baseline_y: int,
    metrics: QFontMetrics,
    size: int,
    style: Style,
    stroke_extent: float = 0.0,
) -> float:
    if style.lit_style == "volume":
        text_metric = (metrics.height() * 0.5) - metrics.descent()
        return float(
            baseline_y
            + style.volume_offset_y
            - stroke_extent
            - size * 0.5
            - text_metric
        )
    return float(baseline_y + style.lit_offset_y - metrics.ascent() - size)


def signal_lit_x(
    img_w: int,
    group_width: int | float,
    style: Style,
    stroke_extent: float = 0.0,
) -> float:
    """Return a viewport-bounded fallback X when union layout is unavailable."""
    offset_x = (
        style.volume_offset_x if style.lit_style == "volume" else style.lit_offset_x
    )
    x = float(style.horizontal_margin_px + offset_x)
    if style.lit_style == "volume":
        x -= stroke_extent
    return max(0.0, min(x, float(max(img_w - group_width, 0))))


def shape_active_index_and_phase(
    elapsed: int,
    duration: int,
    count: int,
) -> tuple[int, float]:
    if duration <= 0 or count <= 1:
        return 0, 1.0
    if elapsed >= duration:
        return -1, 1.0
    raw = ((duration - max(elapsed, 0)) * count) / duration
    active_index = max(0, min(count - 1, int(raw)))
    phase = raw - active_index
    return active_index, max(0.0, min(phase, 1.0))


def volume_active_index_and_phase(
    elapsed: int,
    duration: int,
    count: int,
) -> tuple[int, float]:
    if duration <= 0 or count <= 1:
        return 0, 1.0
    raw = (count * max(elapsed, 0)) / duration
    active_index = max(0, min(count - 1, int(raw)))
    phase = raw - active_index
    if active_index == count - 1 and elapsed >= duration:
        phase = 1.0
    return active_index, max(0.0, min(phase, 1.0))


def volume_flash_alpha(elapsed: int, duration: int, style: Style) -> float:
    if duration <= 0 or elapsed < 0:
        return 0.0
    times = max(int(style.volume_flash_times), 0)
    if times == 0:
        return 1.0
    per_flash = duration / times if times else 0.0
    if per_flash <= 0:
        return 1.0
    phase = (elapsed / per_flash) % 1.0
    phase *= 2.0
    if phase > 1.0:
        phase = 2.0 - phase
    transition = max(
        0.0,
        min(float(style.volume_transition_ratio_pct) / 100.0, 1.0),
    )
    if transition <= 0:
        return 1.0 - (1.0 if (phase * 2.0 - 1.0) > 0.0 else 0.0)
    fade = ((phase * 3.0 - 1.0) * 0.67) / transition
    fade = max(0.0, min(fade, 1.0))
    return 1.0 - fade


def volume_signal_state(
    elapsed: int,
    duration: int,
    count: int,
    style: Style,
) -> tuple[int, float, float]:
    if duration <= 0:
        return -1, 0.0, 0.0
    times = max(int(style.volume_flash_times), 0)
    flash_ratio = max(float(style.volume_flash_duration_ratio), 0.0)
    if times <= 0 or flash_ratio <= 0.0:
        active_index, phase = volume_active_index_and_phase(elapsed, duration, count)
        return active_index, phase, 1.0

    fill_duration = duration / (times * flash_ratio + 1.0)
    flash_duration = max(duration - fill_duration, 0.0)
    if elapsed < flash_duration:
        return -1, 0.0, volume_flash_alpha(
            elapsed,
            int(max(flash_duration, 1.0)),
            style,
        )
    fill_elapsed = int(max(elapsed - flash_duration, 0.0))
    active_index, phase = volume_active_index_and_phase(
        fill_elapsed,
        int(max(fill_duration, 1.0)),
        count,
    )
    return active_index, phase, 1.0


def volume_bar_transition_states(
    style: Style,
    line: TimingLine,
    display_start_ms: int | None,
    display_end_ms: int | None,
    t_ms: int,
    count: int,
    frame_height: int,
    *,
    centers: tuple[float, ...] | None = None,
    line_center_x: float | None = None,
) -> tuple[BarAnimationState, ...] | None:
    """Resolve per-bar character-transition states for the volume group.

    柱体复用正文逐字动画系统（``horizontal.transitions``）：同一行、同一
    显示窗口，柱 index 映射到字符 index 的交错公式（``count`` 传柱数）。
    utopia 退场的每「字」完成时刻文字取「后一个字唱完」，柱体不演唱，
    改为把 done 时刻均匀铺在演唱窗口 ``[line_start, line_end]`` 上——文字
    按演唱进度逐字离场，柱体按同一进度逐根离场，节奏一致。
    全部柱状态静止（恒等）时返回 ``None``，保留组级绘制快路径。
    """
    if count <= 0:
        return None
    transition = line_char_transition_context(
        style,
        line,
        t_ms,
        display_start_ms,
        display_end_ms,
        count,
    )
    if transition is None:
        return None
    utopia_exit = transition.effect == "utopia" and style.exit_anim == "utopia"
    line_start = line_start_ms(line)
    line_end = line_end_ms(line)
    span = max(line_end - line_start, 0)
    states: list[BarAnimationState] = []
    idle = True
    identity: BarAnimationState = (1.0, 0.0, 0.0, 0.0, 1.0, 1.0, 0.0)
    for index in range(count):
        if utopia_exit:
            done = line_start + (span * index) / max(count - 1, 1)
            state = transition_char_state(
                style,
                transition,
                index,
                count,
                t_ms=t_ms,
                frame_height=frame_height,
                char_center_x=centers[index] if centers is not None else None,
                line_center_x=line_center_x,
                following_done_ms=int(done),
            )
        else:
            state = transition_char_state(
                style,
                transition,
                index,
                count,
                t_ms=t_ms,
                frame_height=frame_height,
                char_center_x=centers[index] if centers is not None else None,
                line_center_x=line_center_x,
            )
        if state != identity:
            idle = False
        states.append(state)
    return None if idle else tuple(states)


def lit_transition_state(phase: float, style: Style) -> tuple[float, float, float]:
    mode = style.lit_transition_mode
    ratio = max(0, min(int(style.lit_transition_ratio_pct), 100)) / 100.0
    progress = 1.0 if ratio <= 0 else (phase - (1.0 - ratio)) / ratio
    progress = max(0.0, min(float(progress), 1.0))
    if mode == "fade":
        return progress, 0.0, 0.0
    if mode == "slide":
        distance = max(int(style.lit_transition_distance), 0) * (1.0 - progress)
        radians = math.radians(float(style.lit_transition_angle_deg))
        return progress, -math.cos(radians) * distance, -math.sin(radians) * distance
    return 1.0, 0.0, 0.0


def lit_extinguish_transition_state(
    phase: float,
    style: Style,
) -> tuple[float, float, float]:
    opacity, dx, dy = lit_transition_state(1.0 - phase, style)
    if style.lit_transition_mode == "fade":
        return 1.0 - opacity, dx, dy
    if style.lit_transition_mode == "slide":
        # 退场是入场的时间反演：不透明度 1→0，位移从原位长到入场起点
        # （−angle 方向）。入场曲线（opacity=progress、位移随 (1-progress)
        # 收拢）不能在退场时机原样重放，否则当前灯任期内前段不可见、临近
        # 交接才「滑入」，方向与 fade 相反。C++ shapeSignalState 镜像本式。
        distance = max(int(style.lit_transition_distance), 0) * opacity
        radians = math.radians(float(style.lit_transition_angle_deg))
        return (
            1.0 - opacity,
            -math.cos(radians) * distance,
            -math.sin(radians) * distance,
        )
    return opacity, dx, dy


def _valid_signal_color(value: str, fallback: str) -> QColor:
    color = QColor(value)
    if color.isValid():
        return color
    return QColor(fallback)


_LIT_IMAGE_LOCK = Lock()
_LIT_IMAGE_CACHE: OrderedDict[tuple[str, int, int], QImage] = OrderedDict()
_LIT_IMAGE_CACHE_MAX = 8


def cached_lit_image(path: str) -> QImage | None:
    """Load one lamp sprite keyed by (path, mtime_ns, size).

    失效口径与填充图一致（``image_file_signature``）；文件缺失/解码失败
    返回 None，调用方回退为矢量圆形（与 native 同口径）。
    """
    if not path:
        return None
    signature = image_file_signature(path)
    if signature is None:
        return None
    with _LIT_IMAGE_LOCK:
        cached = _LIT_IMAGE_CACHE.get(signature)
        if cached is not None:
            _LIT_IMAGE_CACHE.move_to_end(signature)
            return cached
    image = QImage(signature[0])
    if image.isNull():
        return None
    with _LIT_IMAGE_LOCK:
        _LIT_IMAGE_CACHE[signature] = image
        while len(_LIT_IMAGE_CACHE) > _LIT_IMAGE_CACHE_MAX:
            _LIT_IMAGE_CACHE.popitem(last=False)
    return image


def _draw_lit_image(painter: QPainter, rect: QRectF, image: QImage) -> None:
    """等比 contain 进 size 方形槽位并居中（native DrawBitmap 同口径）。"""
    if image.width() <= 0 or image.height() <= 0:
        return
    scale = min(rect.width() / image.width(), rect.height() / image.height())
    if scale <= 0:
        return
    width = image.width() * scale
    height = image.height() * scale
    target = QRectF(
        rect.left() + (rect.width() - width) * 0.5,
        rect.top() + (rect.height() - height) * 0.5,
        width,
        height,
    )
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawImage(target, image)


def build_signal_layers(
    groups: list[SignalLitGroup],
    style: Style,
) -> list[SignalLitsLayer]:
    if not groups:
        return []
    is_volume = style.lit_style == "volume"
    count = (
        max(1, min(int(style.volume_column_count), 16))
        if is_volume
        else max(1, min(int(style.lit_number), 8))
    )
    size = max(int(style.volume_size if is_volume else style.lit_size), 1)
    tracking = max(
        int(style.volume_column_spacing if is_volume else style.lit_tracking),
        0,
    )
    fill = _valid_signal_color(style.lit_fill_color, "#0000FF")
    stroke = _valid_signal_color(style.lit_stroke_color, "#FFFFFF")
    stroke_width = max(int(style.lit_stroke_width), 0)
    soften = max(int(style.lit_stroke_soften), 0)
    group_opacity = max(0, min(int(style.lit_opacity_pct), 100)) / 100.0
    edge_brightness = (
        max(0, min(int(style.lit_edge_brightness_pct), 100)) / 100.0
    )
    return [
        SignalLitsLayer(
            group=group,
            style=style,
            count=count,
            size=size,
            tracking=tracking,
            fill=fill,
            stroke=stroke,
            stroke_width=stroke_width,
            soften=soften,
            group_opacity=group_opacity,
            edge_brightness=edge_brightness,
            is_volume=is_volume,
            z_index=index,
        )
        for index, group in enumerate(groups)
    ]


@dataclass(frozen=True)
class SignalLitsLayer:
    """Dynamic LayerCompositor adapter for one Sayatoo signal group."""

    group: SignalLitGroup
    style: Style
    count: int
    size: int
    tracking: int
    fill: QColor
    stroke: QColor
    stroke_width: int
    soften: int
    group_opacity: float
    edge_brightness: float
    is_volume: bool
    z_index: int = 0
    scope: str = SCOPE_LINE

    def active_window(self, ctx: LayerContext) -> list[tuple[int, int]]:
        return []

    def layout(self, ctx: LayerContext) -> SignalLitsLayer:
        return self

    def static_key(self, ctx: LayerContext, layout: object) -> None:
        return None

    def bake(self, ctx: LayerContext, layout: object, key: Hashable) -> BakedLayer:
        raise AssertionError("Signal layers are dynamic in the QPainter backend")

    def animate(self, ctx: LayerContext, layout: object) -> LayerAnimation:
        return LayerAnimation()

    def paint_dynamic(
        self,
        painter: QPainter,
        ctx: LayerContext,
        layout: object,
    ) -> None:
        if self.group_opacity <= 0.0 or self.group.anim_opacity <= 0.0:
            return
        painter.save()
        try:
            # 段首行入退场动画的透明度与正文同源（位移已折进 group.x/y）。
            painter.setOpacity(painter.opacity() * self.group.anim_opacity)
            painter.setOpacity(painter.opacity() * self.group_opacity)
            painter.save()
            try:
                painter.setOpacity(painter.opacity() * self.group.opacity)
                if self.is_volume:
                    _draw_volume_lit_group(painter, self.group, self.style)
                elif _lit_auto_decorated(self.style):
                    _draw_lit_decorated_group(painter, self, self.group)
                else:
                    _paint_shape_signal_group(painter, self)
            finally:
                painter.restore()
        finally:
            painter.restore()

    def vertical_bounds(
        self,
        ctx: LayerContext,
        layout: object,
    ) -> tuple[int, int] | None:
        if (
            self.group_opacity <= 0.0
            or self.group.opacity <= 0.0
            or self.group.anim_opacity <= 0.0
        ):
            return None
        if self.is_volume:
            bounds = _volume_signal_vertical_bounds(self.group, self.style)
            if bounds is None or not self.group.bar_animations:
                return bounds
            # 逐字退场把柱体甩出行盒（utopia 行程 = 画布高/15 + 形体尺寸），
            # 静态柱盒必须按该上界外扩，导出条带/避让包络才不会裁掉飞行柱。
            geometry = volume_signal_geometry(self.style)
            excursion = (
                ctx.logical_h / 15.0
                + geometry.size
                + geometry.column_width
                + 2.0 * geometry.stroke_extent
            )
            return (
                int(math.floor(bounds[0] - excursion)),
                int(math.ceil(bounds[1] + excursion)),
            )
        return _shape_signal_vertical_bounds(self)


def _lit_shape_ink_rect(rect: QRectF, layer: SignalLitsLayer) -> QRectF:
    """Bound one custom lamp's ink (shadow + soften + body) for fade compositing."""
    ink = QRectF(rect)
    if layer.style.lit_shadow:
        ink = ink.united(
            rect.translated(
                max(rect.width() * 0.08, 1.0),
                max(rect.height() * 0.08, 1.0),
            )
        )
    pad = float(layer.stroke_width + layer.soften) + 2.0
    return ink.adjusted(-pad, -pad, pad, pad)


def _paint_shape_signal_group(
    painter: QPainter,
    layer: SignalLitsLayer,
) -> None:
    group = layer.group
    for index in range(layer.count):
        if group.active_index is None or index > group.active_index:
            continue
        is_active = index == group.active_index
        dx = group.dx if is_active else 0.0
        dy = group.dy if is_active else 0.0
        x = group.x + dx + index * (layer.size * 1.5 + layer.tracking)
        rect = QRectF(x, group.y + dy, float(layer.size), float(layer.size))
        # 高光（原「边缘亮度」）常驻所有未熄灯：仅画在激活灯上会在其开始
        # 熄灭的瞬间无淡入地弹出一颗灰色圆形（2026-10 修复，native 同口径）。
        # 正在淡出的激活灯必须整灯（阴影+柔化+灯体+高光）先离屏合成再一次
        # 乘 alpha——高光/阴影是半透明层，逐层各自乘 alpha 会让灯体在渐入
        # 渐出中段提前透出背景（与音量柱闪烁整体烘焙同一口径）。
        lamp_alpha = group.active_opacity if is_active else 1.0
        if lamp_alpha >= 1.0:
            painter.save()
            try:
                _draw_lit_shape(
                    painter,
                    rect,
                    layer.style,
                    layer.fill,
                    layer.stroke,
                    layer.stroke_width,
                    layer.soften,
                    layer.edge_brightness,
                )
            finally:
                painter.restore()
            continue
        ink = _lit_shape_ink_rect(rect, layer)
        left = math.floor(ink.left())
        top = math.floor(ink.top())
        width = max(1, math.ceil(ink.right()) - left)
        height = max(1, math.ceil(ink.bottom()) - top)
        image = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        layer_painter = QPainter(image)
        try:
            layer_painter.setRenderHints(
                QPainter.RenderHint.Antialiasing
                | QPainter.RenderHint.TextAntialiasing
                | QPainter.RenderHint.SmoothPixmapTransform
            )
            layer_painter.translate(-left, -top)
            _draw_lit_shape(
                layer_painter,
                rect,
                layer.style,
                layer.fill,
                layer.stroke,
                layer.stroke_width,
                layer.soften,
                layer.edge_brightness,
            )
        finally:
            layer_painter.end()
        painter.save()
        try:
            painter.setOpacity(painter.opacity() * lamp_alpha)
            painter.drawImage(QPointF(float(left), float(top)), image)
        finally:
            painter.restore()


def _lit_auto_decorated(style: Style) -> bool:
    """形状指示灯 auto/role 外观：矢量灯走主文字装饰管线。

    与 ``_draw_volume_lit_group`` 的 auto 分支闸门同理：legacy volume 兼容
    路径（lit_style="volume"）不经 resolve_lit_appearance 物化，这里若只看
    模式字段会让 Painter 走推导、native 读物化前的原始值，两后端岔开。
    ``role`` 档（复用配色方案）与 auto 同走装饰管线，只是装饰源换成
    ``lit_role_name`` 指定方案（见 ``_signal_decor_source``）。
    """
    return (
        style.lit_enabled
        and style.lit_style != "volume"
        and style.lit_appearance_mode in {"auto", "role"}
    )


_LIT_NOTE_STYLES = ("note8", "note16", "notepair")
"""音符族灯形：八分音符 / 十六分音符 / 组合（双音符加横梁）。"""


def _lit_star_note_path(rect: QRectF, lit_style: str) -> QPainterPath:
    """星型 / 音符族复合路径（边长比例口径与 native lampShapeGeometry 镜像）。

    音符各部件先各自成子路径再 ``united()`` 成单一轮廓——描边只画外形，
    不在符干与符头的接缝处画内线（native 侧用布尔并集同口径镜像）。
    """
    w = rect.width()
    h = rect.height()
    left = rect.left()
    top = rect.top()
    path = QPainterPath()
    if lit_style == "star":
        # 五角星：外接半径 0.5×边长，内切半径 0.45×外接，顶点朝上。
        outer = min(w, h) * 0.5
        inner = outer * 0.45
        center_x = left + w * 0.5
        center_y = top + h * 0.5
        polygon = QPolygonF()
        for step in range(10):
            radius = outer if step % 2 == 0 else inner
            angle = -math.pi / 2.0 + step * math.pi / 5.0
            polygon.append(
                QPointF(
                    center_x + radius * math.cos(angle),
                    center_y + radius * math.sin(angle),
                )
            )
        path.addPolygon(polygon)
        path.closeSubpath()
        return path

    def _ellipse(cx: float, cy: float, rx: float, ry: float) -> QPainterPath:
        head = QPainterPath()
        head.addEllipse(QPointF(left + w * cx, top + h * cy), w * rx, h * ry)
        return head

    def _rect(x: float, y: float, rw: float, rh: float) -> QPainterPath:
        part = QPainterPath()
        part.addRect(left + w * x, top + h * y, w * rw, h * rh)
        return part

    def _flag(y0: float) -> QPainterPath:
        # 符旗：附着在符干顶部、向右下弯的三角旗面（十六分音符的第二面
        # 旗 = 同形下移 0.16h）。旗面左缘与符干右缘（0.532w）重合。
        flag = QPainterPath()
        flag.moveTo(left + w * 0.532, top + h * y0)
        flag.cubicTo(
            left + w * 0.747, top + h * (y0 + 0.06),
            left + w * 0.827, top + h * (y0 + 0.20),
            left + w * 0.707, top + h * (y0 + 0.36),
        )
        flag.lineTo(left + w * 0.632, top + h * (y0 + 0.285))
        flag.cubicTo(
            left + w * 0.722, top + h * (y0 + 0.18),
            left + w * 0.647, top + h * (y0 + 0.09),
            left + w * 0.532, top + h * (y0 + 0.055),
        )
        flag.closeSubpath()
        return flag

    if lit_style in ("note8", "note16"):
        # 单音符：符头（左下椭圆）+ 符干（右侧竖线）+ 符旗（十六分两面）。
        # 符干右缘收在符头右极点（0.54w）之内、下端沉过符头中心线——
        # 竖直符干与水平放置的椭圆只在极点相切，右下角露出矩形直角
        # 会刺出符头轮廓（native 侧同口径）。
        path = _ellipse(0.34, 0.78, 0.20, 0.12).united(
            _rect(0.468, 0.10, 0.064, 0.70)
        )
        path = path.united(_flag(0.10))
        if lit_style == "note16":
            path = path.united(_flag(0.26))
        return path

    # 组合（♪♪ 横梁）：左符头低、右符头高，双符干接顶部斜横梁。符干
    # 同样右缘收进各自符头右极点内、下端沉过符头中心线。
    path = _ellipse(0.24, 0.74, 0.17, 0.11).united(_rect(0.342, 0.16, 0.06, 0.60))
    path = path.united(_ellipse(0.62, 0.60, 0.17, 0.11))
    path = path.united(_rect(0.722, 0.04, 0.06, 0.58))
    beam = QPainterPath()
    beam.moveTo(left + w * 0.342, top + h * 0.08)
    beam.lineTo(left + w * 0.782, top + h * 0.02)
    beam.lineTo(left + w * 0.782, top + h * 0.12)
    beam.lineTo(left + w * 0.342, top + h * 0.18)
    beam.closeSubpath()
    return path.united(beam)


def _lit_shape_path(rect: QRectF, lit_style: str) -> QPainterPath:
    """灯形矢量路径，与 `_draw_lit_shape_raw` 的形状口径一致。"""
    path = QPainterPath()
    if lit_style == "square":
        path.addRect(rect)
    elif lit_style == "rounded":
        radius = max(rect.width() * 0.22, 1.0)
        path.addRoundedRect(rect, radius, radius)
    elif lit_style == "star" or lit_style in _LIT_NOTE_STYLES:
        path = _lit_star_note_path(rect, lit_style)
    else:
        path.addEllipse(rect)
    return path


def _lit_highlight_geometry(
    lit_style: str,
    rect: QRectF,
) -> tuple[float, float, float]:
    """高光锚点（圆心 + 半径，占位形边长比例）。

    圆/方/圆角取左上 34% 处（原口径）；星型取星核上部居中（五角星顶臂
    与中心的连接处）；音符取符头中心——两种新形状的受光面与方形灯不同。
    """
    size = min(rect.width(), rect.height())
    if lit_style == "star":
        return (
            rect.left() + rect.width() * 0.5,
            rect.top() + rect.height() * 0.36,
            size * 0.10,
        )
    if lit_style in ("note8", "note16"):
        # 单音符：高光落在符头（左下）上部，半径取符头短半轴的一半。
        return (
            rect.left() + rect.width() * 0.29,
            rect.top() + rect.height() * 0.745,
            size * 0.06,
        )
    if lit_style == "notepair":
        # 组合：高光落在左符头（组合符头比单音符小一圈）。
        return (
            rect.left() + rect.width() * 0.20,
            rect.top() + rect.height() * 0.705,
            size * 0.055,
        )
    return (
        rect.left() + rect.width() * 0.34,
        rect.top() + rect.height() * 0.34,
        size * 0.16,
    )


def _draw_lit_decorated_group(
    painter: QPainter,
    layer: SignalLitsLayer,
    group: SignalLitGroup,
) -> None:
    """auto/role 档形状灯走主文字装饰管线。

    装饰源是段首行第一个角色的有效样式（``group.bar_style``，无角色时
    为该行样式）；role 档换成 ``lit_role_name`` 按名解析的固定方案：指示灯
    不区分走字前后、直接淡化消失，全程取其配色矩阵
    的 **after（走字后）** 状态；填充（含渐变/图片填充）跨度为灯组自身
    外接框，描边/二重描边宽度、发光半径与阴影偏移按 灯尺寸/该角色字号
    同比缩放（上限半个灯宽）。「整字放大」唱字动画开启时，正在熄灭的灯
    按同一曲线在其倒计时窗口内放大-缩回；高光常驻所有未熄灯且画在装饰
    栈之上。

    组级透明度（lit_opacity_pct）与熄灭灯的转场透明度都必须先把整灯
    （发光多 pass + 描边 + 填充 + 高光）离屏合成、再一次乘 alpha：逐层
    各自乘 alpha 会让发光环比灯体褪色慢，淡出中段残留"空心灯"残影（与
    音量柱闪烁整体烘焙同一口径）。native 端以 OpacityLayer 镜像本语义。
    """
    style = layer.style
    decor_style = group.bar_style if group.bar_style is not None else style
    colors = effective_karaoke_colors(decor_style)
    font_size = max(int(decor_style.font_size_px), 1)
    size = float(layer.size)
    scale = size / float(font_size)
    stroke_width = min(
        max(int(int(decor_style.stroke_width_px or 0) * scale + 0.5), 0),
        max(int(size) // 2, 0),
    )
    stroke2_width = min(
        max(int(main_stroke2_width(decor_style) * scale + 0.5), 0),
        max(int(size) // 2, 0),
    )
    shadow_dx = _scaled_signed_px(decor_style.shadow_offset_x, scale)
    shadow_dy = _scaled_signed_px(decor_style.shadow_offset_y, scale)
    glow_after = int(glow_radius(decor_style, after=True) * scale + 0.5)
    glow_pad = (
        float(glow_extent(stroke_width, stroke2_width, glow_after))
        if decor_style.decoration_kind == "glow"
        else 0.0
    )
    half_pen = (stroke_width + stroke2_width) * 0.5
    pad_x = max(glow_pad, half_pen + abs(shadow_dx)) + 2.0
    pad_y = max(glow_pad, half_pen + abs(shadow_dy)) + 2.0
    edge_brightness = layer.edge_brightness
    lit_image = (
        cached_lit_image(style.lit_image_path)
        if style.lit_style == "image"
        else None
    )

    duration = max(int(group.duration_ms), 0)
    pulse_enabled = effective_karaoke_zoom_pulse(decor_style)
    pulse_level = zoom_pulse_curve_level(decor_style)
    count = max(int(layer.count), 1)

    # 可见灯：index 0..active_index；active_index 处为正在熄灭的灯（带
    # 转场位移/透明度）。整字放大窗口 = 该灯自身的倒计时窗口（镜像音量柱
    # 「覆盖窗口」公式，方向相反：countdown 从右往左熄灭）。
    pitch = size * 1.5 + float(layer.tracking)
    entries: list[tuple[QRectF, bool, float]] = []
    if group.active_index is not None and group.active_index >= 0:
        for index in range(group.active_index + 1):
            is_active = index == group.active_index
            dx = group.dx if is_active else 0.0
            dy = group.dy if is_active else 0.0
            rect = QRectF(
                group.x + dx + index * pitch,
                group.y + dy,
                size,
                size,
            )
            pulse = 1.0
            if pulse_enabled and duration > 0:
                lamp_start = duration * (count - index - 1) // count
                lamp_end = duration * (count - index) // count
                pulse = zoom_pulse_wipe_scale(
                    int(group.elapsed_ms), lamp_start, lamp_end, pulse_level
                )
            entries.append((rect, is_active, pulse))
    if not entries:
        return
    state = colors.after
    # 渐变/图片填充的画刷跨度取**全部 count 个槽位**的外接框（基准位，不含
    # 熄灭灯的转场位移）：倒计时从右往左逐个熄灭，若按可见灯取并集，跨度
    # 会随熄灭收缩、渐变被逐帧重新压缩——整组配色随灯减少漂移（native
    # litLampGroupRect 同口径）。逐灯缓冲（_entry_ink）仍按可见灯取。
    full_span = (max(int(count) - 1, 0)) * pitch + size
    group_rect = QRectF(group.x, group.y, full_span, size)

    def _draw_one(target: QPainter, rect: QRectF, pulse: float) -> None:
        # 单灯绘制单元：pulse 缩放 + （图片 | 装饰栈 + 高光）。高光必须与
        # 灯体同一单元——淡出时整灯一次乘 alpha，不能逐层各自乘。
        target.save()
        try:
            if pulse != 1.0:
                center = rect.center()
                target.translate(center.x(), center.y())
                target.scale(pulse, pulse)
                target.translate(-center.x(), -center.y())
            if lit_image is not None:
                _draw_lit_image(target, rect, lit_image)
                return
            path = _lit_shape_path(rect, style.lit_style)
            paint_text_layer_stack(
                target,
                path,
                rect,
                state,
                decor_style,
                stroke_width=stroke_width,
                stroke2_width=stroke2_width,
                shadow_dx=shadow_dx,
                shadow_dy=shadow_dy,
                glow_radius=glow_after,
                fill_rect=group_rect,
            )
            if edge_brightness > 0.0:
                hx, hy, radius = _lit_highlight_geometry(
                    style.lit_style, rect
                )
                highlight = QColor("#FFFFFF")
                highlight.setAlphaF(min(edge_brightness * 0.55, 1.0))
                target.setPen(Qt.PenStyle.NoPen)
                target.setBrush(QBrush(highlight))
                target.drawEllipse(QPointF(hx, hy), radius, radius)
        finally:
            target.restore()

    def _entry_ink(rect: QRectF, is_active: bool, pulse: float) -> QRectF:
        animation = (
            (1.0, group.dx, group.dy, 0.0, 1.0, 1.0, 0.0)
            if is_active
            else None
        )
        return _volume_decorated_bar_ink_rect(
            rect, animation, pulse, pad_x, pad_y
        )

    def _composite_buffer(
        items: list[tuple[QRectF, bool, float]], alpha: float
    ) -> None:
        ink = _entry_ink(*items[0])
        for rect, is_active, pulse in items[1:]:
            ink = ink.united(_entry_ink(rect, is_active, pulse))
        left = math.floor(ink.left())
        top = math.floor(ink.top())
        width = max(1, math.ceil(ink.right()) - left)
        height = max(1, math.ceil(ink.bottom()) - top)
        image = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        layer_painter = QPainter(image)
        try:
            layer_painter.setRenderHints(
                QPainter.RenderHint.Antialiasing
                | QPainter.RenderHint.TextAntialiasing
                | QPainter.RenderHint.SmoothPixmapTransform
            )
            layer_painter.translate(-left, -top)
            for rect, _is_active, pulse in items:
                _draw_one(layer_painter, rect, pulse)
        finally:
            layer_painter.end()
        painter.save()
        try:
            painter.setOpacity(painter.opacity() * alpha)
            painter.drawImage(QPointF(float(left), float(top)), image)
        finally:
            painter.restore()

    group_alpha = painter.opacity()
    steady_entries = [
        (rect, is_active, pulse)
        for rect, is_active, pulse in entries
        if not (is_active and group.active_opacity < 1.0)
    ]
    fading_entries = [
        (rect, is_active, pulse)
        for rect, is_active, pulse in entries
        if is_active and group.active_opacity < 1.0
    ]
    # 组级透明度（lit_opacity_pct）作用在整组上：先合成再一次乘；全透明
    # 走直绘快路径。
    if group_alpha < 1.0 and steady_entries:
        _composite_buffer(steady_entries, 1.0)
    elif steady_entries:
        for rect, _is_active, pulse in steady_entries:
            _draw_one(painter, rect, pulse)
    # 正在熄灭的灯：整灯独立合成，一次乘 组级×转场 alpha。
    if fading_entries:
        _composite_buffer(fading_entries, group.active_opacity)


def _volume_signal_vertical_bounds(
    group: SignalLitGroup,
    style: Style,
) -> tuple[int, int] | None:
    geometry = volume_signal_geometry(style)
    rects = volume_signal_column_rects(group.x, group.y, geometry)
    if not rects:
        return None
    pad = max(int(style.lit_stroke_width), 0) + 2
    top = min(rect.top() for rect in rects) - pad
    bottom = max(rect.bottom() for rect in rects) + pad
    return int(math.floor(top)), int(math.ceil(bottom))


def _shape_signal_vertical_bounds(
    layer: SignalLitsLayer,
) -> tuple[int, int] | None:
    group = layer.group
    if group.active_index is None or group.active_index < 0:
        return None
    rects: list[QRectF] = []
    for index in range(layer.count):
        if index > group.active_index:
            continue
        is_active = index == group.active_index
        dx = group.dx if is_active else 0.0
        dy = group.dy if is_active else 0.0
        x = group.x + dx + index * (layer.size * 1.5 + layer.tracking)
        rect = QRectF(x, group.y + dy, float(layer.size), float(layer.size))
        rects.append(rect)
        if layer.style.lit_shadow:
            rects.append(
                rect.translated(
                    max(rect.width() * 0.08, 1.0),
                    max(rect.height() * 0.08, 1.0),
                )
            )
    if not rects:
        return None
    pad = signal_stroke_extent(layer.style, is_volume=False) + 2
    if _lit_auto_decorated(layer.style):
        # auto 装饰灯的墨迹可远超灯体（发光晕/二重描边/阴影偏移），
        # 导出条带/避让包络必须按装饰外扩，否则裁掉光晕。
        decor_style = (
            group.bar_style if group.bar_style is not None else layer.style
        )
        font_size = max(int(decor_style.font_size_px), 1)
        scale = float(layer.size) / font_size
        stroke_width = min(
            max(int(int(decor_style.stroke_width_px or 0) * scale + 0.5), 0),
            max(int(layer.size) // 2, 0),
        )
        stroke2_width = min(
            max(int(main_stroke2_width(decor_style) * scale + 0.5), 0),
            max(int(layer.size) // 2, 0),
        )
        glow_pad = (
            float(
                glow_extent(
                    stroke_width,
                    stroke2_width,
                    int(glow_radius(decor_style, after=True) * scale + 0.5),
                )
            )
            if decor_style.decoration_kind == "glow"
            else 0.0
        )
        half_pen = (stroke_width + stroke2_width) * 0.5
        shadow_dy = abs(_scaled_signed_px(decor_style.shadow_offset_y, scale))
        decor_pad = max(glow_pad, half_pen + shadow_dy) + 2.0
        pad = max(pad, decor_pad)
    top = min(rect.top() for rect in rects) - pad
    bottom = max(rect.bottom() for rect in rects) + pad
    return int(math.floor(top)), int(math.ceil(bottom))


def _scaled_signed_px(value: int, scale: float) -> int:
    if value >= 0:
        return int(value * scale + 0.5)
    return -int(-value * scale + 0.5)


def _draw_volume_lit_group(
    painter: QPainter,
    group: SignalLitGroup,
    style: Style,
) -> None:
    geometry = volume_signal_geometry(style)
    if group.opacity <= 0:
        return
    rects = volume_signal_column_rects(group.x, group.y, geometry)
    active_index = group.active_index if group.active_index is not None else -1
    bar_animations = group.bar_animations

    # All inline paths use the same halo rasterizer as text.
    for index, glow in enumerate(group.bar_glows):
        if glow is None:
            continue
        path = QPainterPath()
        rect = rects[index]
        radius = max(min(rect.width(), rect.height()) * 0.22, 1.0)
        path.addRoundedRect(rect, radius, radius)
        painter.save()
        try:
            if bar_animations is not None:
                opacity, dx, dy, rotation, sx, sy, skew = bar_animations[index]
                path = character_transform(
                    center_x=rect.center().x(), center_y=rect.center().y(),
                    dx=dx, dy=dy, rotation=rotation,
                    scale_x=sx, scale_y=sy, skew_y=skew,
                ).map(path)
                painter.setOpacity(painter.opacity() * opacity)
            paint_transition_glow_halo(
                painter, path, float(style.font_size_px),
                glow[0], glow[1], bright=glow[2],
            )
        finally:
            painter.restore()

    # auto/role 装饰管线只属于独立音量柱模块：旧版 lit_style="volume"
    # 兼容路径不经 resolve_volume_appearance 物化（render_ir 同门），这里若
    # 只看模式字段会让 Painter 走推导、native 读物化前的原始值，两后端岔开。
    if style.volume_enabled and style.volume_appearance_mode in {"auto", "role"}:
        _draw_volume_decorated_group(
            painter, group, style, geometry, rects, active_index, bar_animations
        )
        return

    fill = _valid_signal_color(style.volume_fill_color, "#FFFFFF")
    stroke = _valid_signal_color(style.volume_stroke_color, "#0000FF")
    overlay_fill = _valid_signal_color(style.volume_overlay_fill_color, "#0000FF")
    overlay_stroke = _valid_signal_color(
        style.volume_overlay_stroke_color,
        "#FFFFFF",
    )
    stroke_width = max(int(style.lit_stroke_width), 0)

    # 闪烁 alpha（group.opacity）只在 paint_dynamic 处乘一次：这里再乘会把
    # 明灭曲线变成 α²，与 native 端的 α¹ 分歧（2026-10 修复）。
    painter.save()
    try:
        for index in range(active_index + 1, geometry.count):
            _draw_volume_column_animated(
                painter,
                rects[index],
                (fill, stroke, stroke_width),
                bar_animations[index] if bar_animations is not None else None,
            )
        for index in range(0, active_index + 1):
            _draw_volume_column_animated(
                painter,
                rects[index],
                (overlay_fill, overlay_stroke, stroke_width),
                bar_animations[index] if bar_animations is not None else None,
            )
    finally:
        painter.restore()


def _volume_decorated_bar_ink_rect(
    rect: QRectF,
    animation: BarAnimationState | None,
    pulse: float,
    pad_x: float,
    pad_y: float,
) -> QRectF:
    """Bound one decorated bar's ink for the flash composite buffer.

    覆盖逐字动画（位移/旋转/缩放/剪切，柱心为轴）与整字放大的行程，
    再外扩发光晕 / 阴影 / 描边笔宽。变换按 AABB 取界（宽松即可），
    与 `_draw_volume_column_animated` 的变换语义一致。
    """
    bounds = QRectF(rect)
    if animation is not None:
        _opacity, dx, dy, rotation, scale_x, scale_y, skew_y = animation
        transform = character_transform(
            center_x=rect.center().x(),
            center_y=rect.center().y(),
            dx=dx,
            dy=dy,
            rotation=rotation,
            scale_x=scale_x,
            scale_y=scale_y,
            skew_y=skew_y,
        )
        if not transform.isIdentity():
            bounds = transform.mapRect(bounds)
    if pulse != 1.0:
        center = bounds.center()
        half_w = bounds.width() * pulse * 0.5
        half_h = bounds.height() * pulse * 0.5
        bounds = QRectF(
            center.x() - half_w,
            center.y() - half_h,
            half_w * 2.0,
            half_h * 2.0,
        )
    return bounds.adjusted(-pad_x, -pad_y, pad_x, pad_y)


def _draw_volume_decorated_group(
    painter: QPainter,
    group: SignalLitGroup,
    style: Style,
    geometry: VolumeSignalGeometry,
    rects: list[QRectF],
    active_index: int,
    bar_animations: tuple[BarAnimationState, ...] | None,
) -> None:
    """auto/role 档柱体走主文字装饰管线。

    装饰源是段首行第一个角色的有效样式（``group.bar_style``，无角色时
    为该行样式）；role 档换成 ``volume_role_name`` 按名解析的固定方案：
    填充（含渐变/图片填充）取其配色矩阵的 before/after
    状态，渐变跨度为柱组自身外接框；描边/二重描边宽度、发光/阴影半径与
    偏移按 柱高/该角色字号 同比缩放（描边几何预留仍来自全局解析，柱距
    口径不受角色差异影响）；「整字放大」唱字动画开启时，倒计时扫到的
    那根柱按同一曲线在其覆盖窗口内放大-缩回。

    闪烁段（``group.opacity < 1``）整组先离屏合成、再一次性乘 alpha：
    发光的多 pass 叠加（浓度语义）与描边/填充必须作为整体明灭，否则
    发光环比柱体褪色慢，明灭中段会残留一根"空心柱"残影。native 端以
    OpacityLayer 镜像本语义（d2d_backend_render 的 volumeFlashLayer）。
    """
    decor_style = group.bar_style if group.bar_style is not None else style
    colors = effective_karaoke_colors(decor_style)
    font_size = max(int(decor_style.font_size_px), 1)
    scale = geometry.size / float(font_size)
    stroke_width = min(
        max(int(int(decor_style.stroke_width_px or 0) * scale + 0.5), 0),
        max(geometry.column_width // 2, 0),
    )
    stroke2_width = min(
        max(int(main_stroke2_width(decor_style) * scale + 0.5), 0),
        max(geometry.column_width // 2, 0),
    )
    shadow_dx = _scaled_signed_px(decor_style.shadow_offset_x, scale)
    shadow_dy = _scaled_signed_px(decor_style.shadow_offset_y, scale)
    glow_before = int(glow_radius(decor_style, after=False) * scale + 0.5)
    glow_after = int(glow_radius(decor_style, after=True) * scale + 0.5)
    # 渐变/图片填充的画刷跨度：柱组自身外接框（未覆盖柱与覆盖柱共享）。
    group_rect = rects[0].united(rects[-1]) if rects else QRectF()
    # 横向渐变专用跨度（「真一组」）：柱组 ∪ 第一角色墨迹跨度的并集，
    # 柱与正文共用同一条渐变带（正文侧同步把第一角色跨度左缘拓宽到柱
    # 左缘）。非横向填充不受影响——fill_brush_rect 只对 gradient_horizontal
    # 取 horizontal 跨度，竖向渐变/分段/图片仍用柱组自身框。
    fill_band = group.fill_band_rect

    pulse_enabled = effective_karaoke_zoom_pulse(decor_style)
    pulse_level = zoom_pulse_curve_level(decor_style)
    duration = max(int(group.duration_ms), 0)
    times = max(int(style.volume_flash_times), 0)
    flash_ratio = max(float(style.volume_flash_duration_ratio), 0.0)
    if times > 0 and flash_ratio > 0.0:
        fill_duration = duration / (times * flash_ratio + 1.0)
        flash_duration = max(duration - fill_duration, 0.0)
    else:
        fill_duration = float(duration)
        flash_duration = 0.0
    fill_elapsed = max(float(group.elapsed_ms) - flash_duration, 0.0)

    bars: list[tuple[QRectF, BarAnimationState | None, float, object]] = []
    for index in range(geometry.count):
        animation = (
            bar_animations[index] if bar_animations is not None else None
        )
        if animation is not None and animation[0] <= 0.0:
            continue
        covered = index <= active_index
        state = colors.after if covered else colors.before
        pulse = 1.0
        if pulse_enabled and fill_duration > 0.0:
            bar_start = int(fill_duration * index / geometry.count)
            bar_end = int(fill_duration * (index + 1) / geometry.count)
            pulse = zoom_pulse_wipe_scale(
                int(fill_elapsed), bar_start, bar_end, pulse_level
            )
        bars.append(
            (
                rects[index],
                animation,
                pulse,
                (
                    state,
                    decor_style,
                    stroke_width,
                    stroke2_width,
                    shadow_dx,
                    shadow_dy,
                    glow_after if covered else glow_before,
                    group_rect,
                    fill_band,
                    pulse,
                ),
            )
        )
    if not bars:
        return

    layer_image: QImage | None = None
    layer_painter: QPainter | None = None
    origin = QPointF(0.0, 0.0)
    if group.opacity < 1.0:
        # 闪烁中段：先在不透明离屏缓冲里画完整个柱组（发光叠加发生在乘
        # alpha 之前），再整体乘 alpha 落回主画布。alpha 由 paint_dynamic
        # 预先乘进 painter.opacity()，此处只负责"一次"合成。
        glow_pad = (
            float(
                glow_extent(
                    stroke_width, stroke2_width, max(glow_before, glow_after)
                )
            )
            if decor_style.decoration_kind == "glow"
            else 0.0
        )
        half_pen = (stroke_width + stroke2_width) * 0.5
        pad_x = max(glow_pad, half_pen + abs(shadow_dx)) + 2.0
        pad_y = max(glow_pad, half_pen + abs(shadow_dy)) + 2.0
        ink = _volume_decorated_bar_ink_rect(
            bars[0][0], bars[0][1], bars[0][2], pad_x, pad_y
        )
        for rect, animation, pulse, _decorated in bars[1:]:
            ink = ink.united(
                _volume_decorated_bar_ink_rect(
                    rect, animation, pulse, pad_x, pad_y
                )
            )
        left = math.floor(ink.left())
        top = math.floor(ink.top())
        width = max(1, math.ceil(ink.right()) - left)
        height = max(1, math.ceil(ink.bottom()) - top)
        layer_image = QImage(
            width, height, QImage.Format.Format_ARGB32_Premultiplied
        )
        layer_image.fill(Qt.GlobalColor.transparent)
        layer_painter = QPainter(layer_image)
        layer_painter.setRenderHints(
            QPainter.RenderHint.Antialiasing
            | QPainter.RenderHint.TextAntialiasing
            | QPainter.RenderHint.SmoothPixmapTransform
        )
        layer_painter.translate(-left, -top)
        origin = QPointF(float(left), float(top))

    draw_painter = layer_painter if layer_painter is not None else painter
    painter.save()
    try:
        for rect, animation, _pulse, decorated in bars:
            _draw_volume_column_animated(
                draw_painter,
                rect,
                None,
                animation,
                decorated=decorated,
            )
    finally:
        painter.restore()
    if layer_painter is not None:
        layer_painter.end()
        painter.drawImage(origin, layer_image)


def _draw_volume_column_animated(
    painter: QPainter,
    rect: QRectF,
    solid: tuple[QColor, QColor, int] | None,
    animation: BarAnimationState | None,
    decorated: tuple | None = None,
) -> None:
    """Draw one volume bar, optionally through its character-animation state.

    变换语义与文字字符一致（``character_transform``：柱中心为轴的
    位移/旋转/剪切/缩放），透明度与组级行动画相乘。``solid`` 为传统
    纯色柱参数；``decorated`` 提供 auto 档的装饰管线参数（改走
    ``paint_text_layer_stack``，pulse 为整字放大附加缩放）。
    """
    if animation is None and solid is not None:
        fill, stroke, stroke_width = solid
        _draw_volume_column(painter, rect, fill, stroke, stroke_width)
        return
    if animation is not None and animation[0] <= 0.0:
        return
    center = rect.center()
    painter.save()
    try:
        if animation is not None:
            opacity, dx, dy, rotation, scale_x, scale_y, skew_y = animation
            if opacity < 1.0:
                painter.setOpacity(painter.opacity() * opacity)
            transform = character_transform(
                center_x=center.x(),
                center_y=center.y(),
                dx=dx,
                dy=dy,
                rotation=rotation,
                scale_x=scale_x,
                scale_y=scale_y,
                skew_y=skew_y,
            )
            if not transform.isIdentity():
                painter.setTransform(transform, combine=True)
        if decorated is not None:
            (
                state,
                style,
                stroke_width,
                stroke2_width,
                shadow_dx,
                shadow_dy,
                glow_r,
                group_rect,
                fill_band,
                pulse,
            ) = decorated
            if pulse != 1.0:
                painter.translate(center.x(), center.y())
                painter.scale(pulse, pulse)
                painter.translate(-center.x(), -center.y())
            path = QPainterPath()
            path.addRoundedRect(
                rect,
                max(min(rect.width(), rect.height()) * 0.22, 1.0),
                max(min(rect.width(), rect.height()) * 0.22, 1.0),
            )
            paint_text_layer_stack(
                painter,
                path,
                rect,
                state,
                style,
                stroke_width=stroke_width,
                stroke2_width=stroke2_width,
                shadow_dx=shadow_dx,
                shadow_dy=shadow_dy,
                glow_radius=glow_r,
                fill_rect=group_rect,
                horizontal_fill_rect=fill_band,
            )
        elif solid is not None:
            fill, stroke, stroke_width = solid
            _draw_volume_column(painter, rect, fill, stroke, stroke_width)
    finally:
        painter.restore()


def _draw_volume_column(
    painter: QPainter,
    rect: QRectF,
    fill: QColor,
    stroke: QColor,
    stroke_width: int,
) -> None:
    painter.setBrush(QBrush(fill))
    if stroke_width > 0 and stroke.alpha() > 0:
        painter.setPen(QPen(stroke, stroke_width))
    else:
        painter.setPen(Qt.PenStyle.NoPen)
    radius = max(min(rect.width(), rect.height()) * 0.22, 1.0)
    painter.drawRoundedRect(rect, radius, radius)


def _draw_lit_shape(
    painter: QPainter,
    rect: QRectF,
    style: Style,
    fill: QColor,
    stroke: QColor,
    stroke_width: int,
    soften: int,
    edge_brightness: float,
) -> None:
    if style.lit_style == "image":
        # 图片模式：直接把素材 contain 进槽位。描边/柔化/阴影/边缘亮度
        # 是矢量形状专属装饰，图片下不绘制；缺图回退圆形（走 else 分支）。
        image = cached_lit_image(style.lit_image_path)
        if image is not None:
            _draw_lit_image(painter, rect, image)
            return
    if style.lit_shadow:
        shadow = QColor("#000000")
        shadow.setAlphaF(0.35)
        shadow_rect = rect.translated(
            max(rect.width() * 0.08, 1.0),
            max(rect.height() * 0.08, 1.0),
        )
        _draw_lit_shape_raw(
            painter,
            shadow_rect,
            style.lit_style,
            shadow,
            QColor("#00000000"),
            0,
        )
    if soften > 0 and stroke_width > 0:
        soft = QColor(stroke)
        soft.setAlphaF(0.28)
        _draw_lit_shape_raw(
            painter,
            rect,
            style.lit_style,
            fill,
            soft,
            stroke_width + soften,
        )
    _draw_lit_shape_raw(
        painter,
        rect,
        style.lit_style,
        fill,
        stroke,
        stroke_width,
    )
    if edge_brightness > 0:
        # 高光锚点按形状适配：星型取星核上部、音符取符头（圆/方/圆角
        # 维持原左上口径）。
        hx, hy, radius = _lit_highlight_geometry(style.lit_style, rect)
        highlight = QColor("#FFFFFF")
        highlight.setAlphaF(min(edge_brightness * 0.55, 1.0))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(highlight))
        painter.drawEllipse(QPointF(hx, hy), radius, radius)


def _draw_lit_shape_raw(
    painter: QPainter,
    rect: QRectF,
    lit_style: str,
    fill: QColor,
    stroke: QColor,
    stroke_width: int,
) -> None:
    painter.setBrush(QBrush(fill))
    if stroke_width > 0 and stroke.alpha() > 0:
        painter.setPen(QPen(stroke, stroke_width))
    else:
        painter.setPen(Qt.PenStyle.NoPen)
    if lit_style == "square":
        painter.drawRect(rect)
    elif lit_style == "rounded":
        radius = max(rect.width() * 0.22, 1.0)
        painter.drawRoundedRect(rect, radius, radius)
    elif lit_style == "star" or lit_style in _LIT_NOTE_STYLES:
        painter.drawPath(_lit_star_note_path(rect, lit_style))
    else:
        painter.drawEllipse(rect)


def resolve_signal_lit_groups(
    track: TimingTrack,
    display_lines: list[DisplayLine],
    baselines: Mapping[int, int],
    img_w: int,
    img_h: int,
    t_ms: int,
    style: Style,
    count: int,
    size: int,
    item_width: int,
    tracking: int,
    stroke_extent: float = 0.0,
    *,
    measure_line: SignalLineMeasurer,
    line_layouts: Mapping[int, SignalLineLayout] | None = None,
    line_offsets: Mapping[int, tuple[float, float]] | None = None,
    line_animations: Mapping[int, tuple[float, float, float]] | None = None,
    line_entry_animations: Mapping[int, tuple[float, float, float]] | None = None,
    text_anchor: bool = False,
) -> list[SignalLitGroup]:
    del item_width
    duration = max(int(style.signals_duration_ms), 0)
    if duration <= 0:
        return []
    active_duration = max(duration - max(int(style.lit_waiting_time_ms), 0), 0)
    if active_duration <= 0:
        return []
    groups: list[SignalLitGroup] = []
    time_offset = int(style.lit_time_offset_ms)
    # auto 档形状灯跟随行**入场**动画（与正文同步出现、含位移）；退场不
    # 跟随——独立悬浮模块靠自身倒计时转场完成渐变消失。
    lit_auto_independent = _lit_auto_decorated(style)
    if style.lit_style == "volume":
        volume_geometry = volume_signal_geometry(style)
        group_width = volume_geometry.group_width
    else:
        volume_geometry = None
        group_width = count * size + max(count - 1, 0) * (size * 0.5 + tracking)
    # 分模块宿主门（特效行开关）：本 pass 的 active_style 属于哪个模块
    # （柱组 = volume 投影或 legacy volume 口径；其余 = 形状灯），就按该
    # 模块的宿主行（段首基线 + 行级挂载覆盖）逐行过滤。None（模块未
    # 启用）维持 fail-open，由上层显示过滤（宿主并集）兜底。
    if style.lit_style == "volume":
        signal_heads = volume_signal_head_context(track, style)
    else:
        signal_heads = lit_signal_head_context(track, style)
    index_of = (
        {id(line): index for index, line in enumerate(track.lines)}
        if signal_heads is not None
        else None
    )
    for display_line in display_lines:
        line = display_line.line
        if line.is_blank or not line.chars:
            continue
        if index_of is not None and index_of.get(id(line)) not in signal_heads:
            continue
        anim_dx, anim_dy, anim_opacity = (
            line_animations.get(id(line), (0.0, 0.0, 1.0))
            if line_animations is not None
            else (0.0, 0.0, 1.0)
        )
        if lit_auto_independent:
            # auto 档：只取入场分量（无入场数据时按恒等，退场不隐藏灯组）。
            if line_entry_animations is not None:
                anim_dx, anim_dy, anim_opacity = line_entry_animations.get(
                    id(line), (0.0, 0.0, 1.0)
                )
            else:
                anim_dx, anim_dy, anim_opacity = 0.0, 0.0, 1.0
        # 与正文同口径：动画透明度归零的帧整行不画，柱体/灯组同样跳过
        # （auto 档的透明度来自入场分量，退场归零不影响灯组）。
        if anim_opacity <= 0.0:
            continue
        line_layout = (
            line_layouts.get(id(display_line.line))
            if line_layouts is not None
            else None
        )
        if line_layout is None:
            line_layout = measure_line(track, display_line, baselines, img_h, style)
        line_style = line_layout.line_style
        metrics = line_layout.metrics
        total_w = line_layout.total_w
        baseline_y = line_layout.baseline_y
        if total_w <= 0:
            continue

        signal_end = line_start_ms(line) + time_offset
        active_start = signal_end - active_duration
        display_end = display_line.display_end_ms
        if display_end is None:
            display_end = line_end_ms(line) + max(int(line_style.line_tail_ms), 0)
        # 特效随所在行一并入场：可见窗口下界取所在行显示窗起点；动画
        # （闪烁段/填充段/逐个熄灭）仍从 active_start 播放——显示时长超过
        # 特效时长的行，动画开始前显示初始状态（满灯 / 初帧柱体），
        # elapsed 钳 0 即初始帧。
        display_start = (
            display_line.display_start_ms
            if display_line.display_start_ms is not None
            else active_start
        )
        if not (display_start <= t_ms < display_end):
            continue

        elapsed = max(t_ms - active_start, 0)
        bar_animations = None
        bar_glows = ()
        # 装饰源样式（auto 档）：段首行第一个非空白字符的角色方案叠加进行
        # 样式（native 端取第一个有几何字符的 styleIndex，两端口径一致）。
        # 音量柱 auto 装饰与形状灯 auto 装饰共用该源。
        # role 档（复用配色方案）：装饰源换成按名解析的固定方案
        # （``__global__``/「标题」/角色方案，叠加到全局样式），不随所在行
        # 变化；名字悬空回退 auto 口径（native 端 scene 级 decor style
        # 同一悬空判定，两端口径一致）。
        first_role = next(
            (
                char.role_label
                for char in line.chars
                if char.text and not char.text.isspace()
            ),
            None,
        )
        if style.lit_style == "volume":
            role_source = (
                appearance_role_source(style, style.volume_role_name)
                if style.volume_appearance_mode == "role"
                else None
            )
        else:
            role_source = (
                appearance_role_source(style, style.lit_role_name)
                if style.lit_appearance_mode == "role"
                else None
            )
        bar_style = (
            role_source
            if role_source is not None
            else style_for_role(line_style, first_role)
        )
        if style.lit_style == "volume":
            elapsed = min(elapsed, max(active_duration - 1, 0))
            active_index, phase, opacity = volume_signal_state(
                elapsed,
                active_duration,
                count,
                line_style,
            )
            active_opacity, dx, dy = 1.0, 0.0, 0.0
            transition = line_char_transition_context(
                line_style, line, t_ms, display_line.display_start_ms, display_end, count
            )
            if transition is not None:
                bar_glows = tuple(
                    transition_char_glow(
                        transition, i, count, t_ms=t_ms,
                        configured_ms=(
                            line_style.exit_fade_ms if transition.phase == "exit"
                            else line_style.entry_lead_ms
                        ),
                    )
                    for i in range(count)
                )
            bar_animations = volume_bar_transition_states(
                line_style,
                line,
                display_line.display_start_ms,
                display_end,
                t_ms,
                count,
                img_h,
                centers=tuple(
                    line_style.volume_offset_x - volume_geometry.group_width
                    + volume_geometry.stroke_extent + i * volume_geometry.pitch
                    + volume_geometry.column_width * 0.5
                    for i in range(count)
                ) if volume_geometry is not None else None,
                line_center_x=(
                    (min(0.0, line_style.volume_offset_x - volume_geometry.group_width)
                     + max(float(total_w), float(line_style.volume_offset_x))) * 0.5
                ) if volume_geometry is not None else None,
            )
        else:
            active_index, phase = shape_active_index_and_phase(
                elapsed,
                active_duration,
                count,
            )
            active_opacity, dx, dy = lit_extinguish_transition_state(
                phase,
                line_style,
            )
            opacity = 1.0

        if (
            not text_anchor
            and line_layout is not None
            and line_layout.signal_x is not None
        ):
            x = line_layout.signal_x
        elif text_anchor and line_layout is not None:
            text_x = getattr(line_layout, "text_x", None)
            if text_x is not None:
                # 双模块时形状灯悬浮在文字实际起点（union 已被音量柱右移）。
                # 不借属于音量柱的 signal_x，也不退到视口左边距。
                x = float(text_x) + float(line_style.lit_offset_x)
            else:
                x = signal_lit_x(
                    img_w, group_width, line_style, stroke_extent
                )
        else:
            x = signal_lit_x(img_w, group_width, line_style, stroke_extent)
        if text_anchor:
            # signal_y 同样属于音量柱（高度/垂直公式不同），形状灯按自身
            # 悬浮语义重算 y，只借 baseline 与字体度量。
            y = signal_lit_y(
                baseline_y,
                metrics,
                size,
                line_style,
                stroke_extent,
            )
        elif line_layout.signal_y is not None:
            y = line_layout.signal_y
        else:
            y = signal_lit_y(
                baseline_y,
                metrics,
                size,
                line_style,
                stroke_extent,
            )
        offset_x, offset_y = (
            line_offsets.get(id(line), (0.0, 0.0))
            if line_offsets is not None
            else (0.0, 0.0)
        )
        # 「真一组」横向渐变跨度：布局侧（_resolve_sayatoo_line_layouts）算
        # 好的本地 band（左 = 柱左缘，右 = 第一角色推进右缘），换算到画布。
        # y 高度取 1：横向渐变画刷只消费 x 跨度（fill_brush_rect/
        # createPaintBrush 同口径）。
        fill_band_rect = None
        if (
            volume_geometry is not None
            and line_layout is not None
            and getattr(line_layout, "signal_band", None) is not None
        ):
            band_left = x + offset_x + anim_dx + volume_geometry.stroke_extent
            band_right = float(line_layout.text_x) + line_layout.signal_band[1]
            if band_right > band_left:
                fill_band_rect = QRectF(
                    band_left,
                    y + offset_y + anim_dy,
                    band_right - band_left,
                    1.0,
                )
        groups.append(
            SignalLitGroup(
                x=x + offset_x + anim_dx,
                y=y + offset_y + anim_dy,
                elapsed_ms=elapsed,
                duration_ms=active_duration,
                active_index=active_index,
                opacity=opacity,
                active_opacity=active_opacity,
                dx=dx,
                dy=dy,
                phase=phase,
                anim_opacity=anim_opacity,
                bar_animations=bar_animations,
                bar_style=bar_style,
                bar_glows=bar_glows,
                fill_band_rect=fill_band_rect,
            )
        )
    return groups


def resolve_signal_layers(
    track: TimingTrack,
    display_lines: list[DisplayLine],
    baselines: Mapping[int, int],
    img_w: int,
    img_h: int,
    t_ms: int,
    style: Style,
    *,
    measure_line: SignalLineMeasurer,
    line_layouts: Mapping[int, SignalLineLayout] | None = None,
    line_offsets: Mapping[int, tuple[float, float]] | None = None,
    line_animations: Mapping[int, tuple[float, float, float]] | None = None,
    line_entry_animations: Mapping[int, tuple[float, float, float]] | None = None,
) -> list[SignalLitsLayer]:
    styles: list[Style] = []
    legacy_volume = style.lit_enabled and style.lit_style == "volume"
    # 「跟随字体」大小推导基准（主轨最高频角色方案）：布局 union、绘制与
    # native IR（render_ir 显式传基准）三处同源。
    auto_basis = signal_auto_basis(style)
    if style.volume_enabled or legacy_volume:
        styles.append(
            volume_style(style, auto_basis=auto_basis)
            if style.volume_enabled
            else style
        )
    if style.lit_enabled and not legacy_volume:
        # auto 外观（大小/颜色跟随主文字）在这里物化，与 painter 布局、
        # native IR（render_ir 同门）消费同一组数值。
        styles.append(resolve_lit_appearance(style, auto_basis=auto_basis))
    layers: list[SignalLitsLayer] = []
    for active_style in styles:
        metrics = signal_layout_metrics(active_style)
        # 双模块时主布局的 signal 坐标属于会参与 union 的音量柱；形状灯
        # 改按文字实际起点锚定（text_anchor），避免借到音量柱坐标或退到
        # 视口左边距。
        shape_over_volume = (
            active_style.lit_style != "volume" and style.volume_enabled
        )
        groups = resolve_signal_lit_groups(
            track,
            display_lines,
            baselines,
            img_w,
            img_h,
            t_ms,
            active_style,
            metrics.count,
            metrics.size,
            metrics.item_width,
            metrics.tracking,
            metrics.stroke_extent,
            measure_line=measure_line,
            line_layouts=line_layouts,
            line_offsets=line_offsets,
            line_animations=line_animations,
            line_entry_animations=line_entry_animations,
            text_anchor=shape_over_volume,
        )
        layers.extend(build_signal_layers(groups, active_style))
    return layers


def paint_signal_lits(
    painter: QPainter,
    img_w: int,
    img_h: int,
    track: TimingTrack,
    display_lines: list[DisplayLine],
    baselines: Mapping[int, int],
    t_ms: int,
    style: Style,
    *,
    compositor: LayerCompositor,
    measure_line: SignalLineMeasurer,
    line_layouts: Mapping[int, SignalLineLayout] | None = None,
    line_offsets: Mapping[int, tuple[float, float]] | None = None,
    line_animations: Mapping[int, tuple[float, float, float]] | None = None,
    line_entry_animations: Mapping[int, tuple[float, float, float]] | None = None,
) -> None:
    layers = resolve_signal_layers(
        track,
        display_lines,
        baselines,
        img_w,
        img_h,
        t_ms,
        style,
        measure_line=measure_line,
        line_layouts=line_layouts,
        line_offsets=line_offsets,
        line_animations=line_animations,
        line_entry_animations=line_entry_animations,
    )
    if not layers:
        return
    compositor.paint_ordered(
        painter,
        LayerContext(t_ms=t_ms, logical_w=img_w, logical_h=img_h),
        layers,
    )


def active_lit_indices(
    track: TimingTrack,
    display_lines: list[DisplayLine],
    t_ms: int,
    style: Style,
    count: int,
    *,
    measure_line: SignalLineMeasurer,
) -> set[int]:
    # 独立音量柱工程的 lit_style 是形状灯值，必须先投影到 volume 口径，
    # 否则会按形状灯的尺寸/时序参数计算活跃索引。
    if style.volume_enabled:
        style = volume_style(style, auto_basis=signal_auto_basis(style))
    is_volume = style.lit_style == "volume"
    groups = resolve_signal_lit_groups(
        track,
        display_lines,
        {display_line.lane: 0 for display_line in display_lines},
        1920,
        1080,
        t_ms,
        style,
        count,
        max(int(style.volume_size if is_volume else style.lit_size), 1),
        max(int(style.volume_column_width if is_volume else style.lit_size), 1),
        max(
            int(style.volume_column_spacing if is_volume else style.lit_tracking),
            0,
        ),
        signal_stroke_extent(style, is_volume=is_volume),
        measure_line=measure_line,
    )
    return {
        group.active_index
        for group in groups
        if group.opacity > 0
        and group.active_index is not None
        and group.active_index >= 0
    }


__all__ = [
    "SignalLayoutMetrics",
    "SignalLineLayout",
    "SignalLineMeasurement",
    "SignalLineMeasurer",
    "SignalLitGroup",
    "VolumeSignalGeometry",
    "active_lit_indices",
    "build_signal_layers",
    "line_has_active_signal",
    "lit_extinguish_transition_state",
    "lit_transition_state",
    "paint_signal_lits",
    "resolve_signal_layers",
    "resolve_signal_lit_groups",
    "shape_active_index_and_phase",
    "signal_layout_metrics",
    "signal_lit_x",
    "signal_lit_y",
    "signal_local_x",
    "signal_offset_x",
    "signal_stroke_extent",
    "volume_active_index_and_phase",
    "volume_flash_alpha",
    "volume_signal_column_rects",
    "volume_signal_geometry",
    "volume_signal_state",
]
