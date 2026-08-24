"""Sayatoo signal-cue geometry and time-state contracts."""

from __future__ import annotations

import math
from dataclasses import dataclass

from PyQt6.QtCore import QRectF
from PyQt6.QtGui import QFontMetrics

from krok_helper.subtitle_render.engine.layout.line_style import (
    line_end_ms,
    line_start_ms,
)
from krok_helper.subtitle_render.models import Style
from krok_helper.subtitle_render.timing import TimingLine


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


__all__ = [
    "SignalLayoutMetrics",
    "SignalLitGroup",
    "VolumeSignalGeometry",
    "line_has_active_signal",
    "lit_extinguish_transition_state",
    "lit_transition_state",
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
