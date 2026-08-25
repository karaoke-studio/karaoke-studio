"""Sayatoo signal-cue geometry and time-state contracts."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Hashable, Protocol

from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import QBrush, QColor, QFontMetrics, QPainter, QPen

from krok_helper.subtitle_render.engine.layout.line.style import (
    line_end_ms,
    line_start_ms,
)
from krok_helper.subtitle_render.engine.layout.display.signal import (
    signal_head_context,
)
from krok_helper.subtitle_render.engine.render.layers import (
    BakedLayer,
    LayerAnimation,
    LayerCompositor,
    LayerContext,
    SCOPE_LINE,
)
from krok_helper.subtitle_render.models import Style
from krok_helper.subtitle_render.engine.timing.timeline import DisplayLine
from krok_helper.subtitle_render.timing import TimingLine, TimingTrack


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
    spacing: int
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


def signal_stroke_extent(style: Style, *, is_volume: bool) -> float:
    stroke_width = max(int(style.lit_stroke_width), 0)
    soften = 0 if is_volume else max(int(style.lit_stroke_soften), 0)
    return float(stroke_width + soften)


def volume_signal_geometry(style: Style) -> VolumeSignalGeometry:
    count = max(1, min(int(style.volume_column_count), 16))
    size = max(int(style.volume_size), 1)
    column_width = max(int(style.volume_column_width), 1)
    column_spacing = max(int(style.volume_column_spacing), 0)
    spacing = max(0, int(getattr(style, "volume_spacing", 0)))
    stroke_extent = signal_stroke_extent(style, is_volume=True)
    pitch = float(column_width + column_spacing + 2 * stroke_extent)
    local_left = float(style.volume_offset_x) - stroke_extent
    group_width = float(count * pitch + spacing - column_spacing)

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
        spacing=spacing,
        stroke_extent=stroke_extent,
        local_left=local_left,
        group_width=group_width,
        pitch=pitch,
        front_height=front_height,
        height_delta=height_delta,
        align_base_shift=align_base_shift,
        align_delta_shift=align_delta_shift,
    )


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
) -> bool:
    if not is_signal_head:
        return False
    duration = max(int(style.signals_duration_ms), 0)
    active_duration = max(duration - max(int(style.lit_waiting_time_ms), 0), 0)
    if active_duration <= 0:
        return False
    signal_end = line_start_ms(line) + int(style.lit_time_offset_ms)
    display_end = line_end_ms(line) + max(int(style.line_tail_ms), 0)
    return signal_end - active_duration <= t_ms <= display_end


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
    return 1.0 - opacity if style.lit_transition_mode == "fade" else opacity, dx, dy


def _valid_signal_color(value: str, fallback: str) -> QColor:
    color = QColor(value)
    if color.isValid():
        return color
    return QColor(fallback)


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
        if self.group_opacity <= 0.0:
            return
        painter.save()
        try:
            painter.setOpacity(painter.opacity() * self.group_opacity)
            painter.save()
            try:
                painter.setOpacity(painter.opacity() * self.group.opacity)
                if self.is_volume:
                    _draw_volume_lit_group(painter, self.group, self.style)
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
        if self.group_opacity <= 0.0 or self.group.opacity <= 0.0:
            return None
        if self.is_volume:
            return _volume_signal_vertical_bounds(self.group, self.style)
        return _shape_signal_vertical_bounds(self)


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
        painter.save()
        try:
            if is_active:
                painter.setOpacity(painter.opacity() * group.active_opacity)
            _draw_lit_shape(
                painter,
                rect,
                layer.style,
                layer.fill,
                layer.stroke,
                layer.stroke_width,
                layer.soften,
                layer.edge_brightness if is_active else 0.0,
            )
        finally:
            painter.restore()


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
    top = min(rect.top() for rect in rects) - pad
    bottom = max(rect.bottom() for rect in rects) + pad
    return int(math.floor(top)), int(math.ceil(bottom))


def _draw_volume_lit_group(
    painter: QPainter,
    group: SignalLitGroup,
    style: Style,
) -> None:
    fill = _valid_signal_color(style.volume_fill_color, "#FFFFFF")
    stroke = _valid_signal_color(style.volume_stroke_color, "#0000FF")
    overlay_fill = _valid_signal_color(style.volume_overlay_fill_color, "#0000FF")
    overlay_stroke = _valid_signal_color(
        style.volume_overlay_stroke_color,
        "#FFFFFF",
    )
    stroke_width = max(int(style.lit_stroke_width), 0)
    geometry = volume_signal_geometry(style)
    if group.opacity <= 0:
        return

    painter.save()
    try:
        painter.setOpacity(painter.opacity() * group.opacity)
        rects = volume_signal_column_rects(group.x, group.y, geometry)
        active_index = group.active_index if group.active_index is not None else -1
        for index in range(active_index + 1, geometry.count):
            _draw_volume_column(painter, rects[index], fill, stroke, stroke_width)
        for index in range(0, active_index + 1):
            _draw_volume_column(
                painter,
                rects[index],
                overlay_fill,
                overlay_stroke,
                stroke_width,
            )
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
        highlight = QColor("#FFFFFF")
        highlight.setAlphaF(min(edge_brightness * 0.55, 1.0))
        inset = rect.width() * 0.18
        highlight_rect = QRectF(
            rect.left() + inset,
            rect.top() + inset,
            rect.width() * 0.32,
            rect.height() * 0.32,
        )
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(highlight))
        painter.drawEllipse(highlight_rect)


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
    if style.lit_style == "volume":
        group_width = volume_signal_geometry(style).group_width
    else:
        group_width = count * size + max(count - 1, 0) * (size * 0.5 + tracking)
    signal_heads = signal_head_context(track, style)
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
        if not (active_start <= t_ms <= display_end):
            continue

        elapsed = max(t_ms - active_start, 0)
        if style.lit_style == "volume":
            elapsed = min(elapsed, max(active_duration - 1, 0))
            active_index, phase, opacity = volume_signal_state(
                elapsed,
                active_duration,
                count,
                line_style,
            )
            active_opacity, dx, dy = 1.0, 0.0, 0.0
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

        x = (
            line_layout.signal_x
            if line_layout.signal_x is not None
            else signal_lit_x(img_w, group_width, line_style, stroke_extent)
        )
        y = (
            line_layout.signal_y
            if line_layout.signal_y is not None
            else signal_lit_y(
                baseline_y,
                metrics,
                size,
                line_style,
                stroke_extent,
            )
        )
        offset_x, offset_y = (
            line_offsets.get(id(line), (0.0, 0.0))
            if line_offsets is not None
            else (0.0, 0.0)
        )
        groups.append(
            SignalLitGroup(
                x=x + offset_x,
                y=y + offset_y,
                elapsed_ms=elapsed,
                duration_ms=active_duration,
                active_index=active_index,
                opacity=opacity,
                active_opacity=active_opacity,
                dx=dx,
                dy=dy,
                phase=phase,
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
) -> list[SignalLitsLayer]:
    if not style.lit_enabled:
        return []
    metrics = signal_layout_metrics(style)
    groups = resolve_signal_lit_groups(
        track,
        display_lines,
        baselines,
        img_w,
        img_h,
        t_ms,
        style,
        metrics.count,
        metrics.size,
        metrics.item_width,
        metrics.tracking,
        metrics.stroke_extent,
        measure_line=measure_line,
        line_layouts=line_layouts,
        line_offsets=line_offsets,
    )
    return build_signal_layers(groups, style)


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
