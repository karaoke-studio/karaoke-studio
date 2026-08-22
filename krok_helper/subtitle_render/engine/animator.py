"""入场 / 退场动画关键帧插值。"""

from __future__ import annotations

import math
from dataclasses import dataclass

from krok_helper.subtitle_render.models import Style, effective_karaoke_animation


@dataclass(frozen=True)
class LineAnimationState:
    opacity: float = 1.0
    dx: float = 0.0
    dy: float = 0.0


def line_animation_state(
    style: Style,
    *,
    t_ms: int,
    display_start_ms: int,
    display_end_ms: int,
    lane: int | None,
) -> LineAnimationState:
    """Return opacity and translation for the current display line."""
    opacity = 1.0
    dx = 0.0
    dy = 0.0

    entry_duration = max(style.entry_lead_ms, 0)
    if style.entry_anim != "none" and entry_duration > 0:
        progress = _ease_out(_progress(t_ms - display_start_ms, entry_duration))
        if style.entry_anim == "fade":
            opacity *= progress
        elif style.entry_anim == "slide_in":
            opacity *= progress
            direction = -1.0 if lane in {None, 0} else 1.0
            dx += direction * (1.0 - progress) * _slide_distance(style)
        elif style.entry_anim == "rise":
            opacity *= progress
            dy += (1.0 - progress) * _rise_distance(style)

    exit_duration = max(style.exit_fade_ms, 0)
    if style.exit_anim != "none" and exit_duration > 0:
        remaining = _ease_in(_progress(display_end_ms - t_ms, exit_duration))
        if style.exit_anim == "fade":
            opacity *= remaining
        elif style.exit_anim == "slide_out":
            opacity *= remaining
            direction = -1.0 if lane in {None, 0} else 1.0
            dx += direction * (1.0 - remaining) * _slide_distance(style)
        elif style.exit_anim == "rise":
            opacity *= remaining
            dy -= (1.0 - remaining) * _rise_distance(style)

    return LineAnimationState(
        opacity=max(0.0, min(1.0, opacity)),
        dx=dx,
        dy=dy,
    )


def _progress(elapsed_ms: int, duration_ms: int) -> float:
    if duration_ms <= 0:
        return 1.0
    return max(0.0, min(1.0, elapsed_ms / duration_ms))


def _ease_out(value: float) -> float:
    return 1.0 - (1.0 - value) * (1.0 - value)


def _ease_in(value: float) -> float:
    return value * value


def _slide_distance(style: Style) -> float:
    return max(style.font_size_px * 0.9, 36.0)


def _rise_distance(style: Style) -> float:
    return max(style.font_size_px * 0.35, 18.0)


# utopia 退场相位 y_travel = sin(π·local/2)·amp（painter._char_transition_state），
# amp = frame_height/15；intro 相位另有 1.3×放大，与旋转一并按 1.5×字号近似。
def _utopia_excursion(style: Style, frame_height: float) -> float:
    height = float(frame_height) if frame_height and frame_height > 0 else 1080.0
    font_px = max(float(getattr(style, "font_size_px", 0.0) or 0.0), 0.0)
    return height / 15.0 + font_px * 1.5


def max_line_animation_excursion(style: Style, frame_height: float) -> float:
    """任一帧内动画能把内容移出静止纵向包络的最大距离（像素，向上或向下）。

    导出条带/多带预扫只按采样时刻取并集，动画峰值可能落在采样间隙之间；
    该上界并入安全边后，条带对任意帧都不会裁掉可见像素。slide/char_fade
    无纵向位移；char_drip / spin_flip 逐字绕字心剪切（skew×半字宽），按
    1.5×字号近似上界。
    """
    anims = {style.entry_anim, style.exit_anim}
    excursion = 0.0
    if "rise" in anims:
        excursion = max(excursion, _rise_distance(style))
    if "utopia" in anims or effective_karaoke_animation(style) == "utopia":
        excursion = max(excursion, _utopia_excursion(style, frame_height))
    if anims & {"char_drip", "spin_flip"}:
        excursion = max(
            excursion,
            float(getattr(style, "font_size_px", 0.0) or 0.0) * 1.5,
        )
    if not math.isfinite(excursion) or excursion < 0.0:
        return 0.0
    return excursion
