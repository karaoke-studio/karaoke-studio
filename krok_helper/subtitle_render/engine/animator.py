"""入场 / 退场动画关键帧插值。"""

from __future__ import annotations

from dataclasses import dataclass

from krok_helper.subtitle_render.models import Style


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
