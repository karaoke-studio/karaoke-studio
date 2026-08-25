"""Pure character-transition state and transform calculations."""

from __future__ import annotations

import math

from PyQt6.QtGui import QTransform

from krok_helper.subtitle_render.domain.models import (
    Style,
    effective_karaoke_animation,
)
from krok_helper.subtitle_render.domain.timing import TimingLine
from krok_helper.subtitle_render.engine.layout.line.style import (
    line_end_ms,
    line_start_ms,
)
from krok_helper.subtitle_render.engine.render.elements.horizontal.contracts import (
    LineCharTransition,
)
from krok_helper.subtitle_render.engine.text import GlyphLayout
from krok_helper.subtitle_render.engine.timing.timeline import compute_char_intervals


UTOPIA_INTRO_TIME_MS = 700
UTOPIA_INTRO_DELAY_MS = 200
UTOPIA_INTRO_ENLARGE_MS = 400
UTOPIA_INTRO_CONDENSE_MS = 100
UTOPIA_INTRO_OVER_RATIO = 1.3
UTOPIA_WIPE_OVER_RATIO = 1.15
UTOPIA_WIPE_OVER_TIME_RATIO = 0.25
UTOPIA_WIPE_OVER_TIME_LIMIT_MS = 100
UTOPIA_FADE_OUT_TIME_MS = 750
CHAR_FADE_INTRO_DELAY_MS = 350
CHAR_FADE_IN_TIME_MS = 250
CHAR_FADE_OUT_TIME_MS = 250


def line_char_transition_context(
    style: Style,
    line: TimingLine,
    t_ms: int,
    display_start_ms: int | None,
    display_end_ms: int | None,
    char_count: int,
    *,
    intervals: list[tuple[int, int]] | None = None,
) -> LineCharTransition | None:
    """Select the active whole-line character-transition mode at ``t_ms``."""

    if char_count <= 0:
        return None
    start = display_start_ms if display_start_ms is not None else line_start_ms(line)
    end = display_end_ms if display_end_ms is not None else line_end_ms(line)

    if (
        style.exit_anim in {"char_fade", "char_drip", "spin_flip"}
        and style.exit_fade_ms > 0
    ):
        exit_start = max(
            line_end_ms(line),
            end - CHAR_FADE_INTRO_DELAY_MS - CHAR_FADE_OUT_TIME_MS,
        )
        if t_ms >= exit_start:
            return LineCharTransition(
                phase="exit",
                effect=style.exit_anim,
                progress=1.0,
                start_ms=exit_start,
                end_ms=end,
            )

    if (
        style.entry_anim in {"char_fade", "char_drip", "spin_flip"}
        and style.entry_lead_ms > 0
    ):
        entry_end = start + CHAR_FADE_INTRO_DELAY_MS + CHAR_FADE_IN_TIME_MS
        if t_ms <= entry_end:
            return LineCharTransition(
                phase="entry",
                effect=style.entry_anim,
                progress=1.0,
                start_ms=start,
                end_ms=entry_end,
            )

    if (
        style.entry_anim == "utopia"
        or style.exit_anim == "utopia"
        or effective_karaoke_animation(style) == "utopia"
    ):
        intervals = intervals if intervals is not None else compute_char_intervals(line)
        # Keep one Utopia render path throughout visibility so entry, wipe and
        # exit state changes cannot introduce antialiasing or glow color flashes.
        # Active non-Utopia character entry/exit effects still take precedence.
        if start <= t_ms <= end:
            return LineCharTransition(
                phase="utopia",
                effect="utopia",
                progress=1.0,
                start_ms=start,
                end_ms=end,
            )
    return None


def spin_flip_char_transform(
    glyph: GlyphLayout,
    baseline_y: int,
    transition: LineCharTransition,
    opacity: float,
) -> QTransform | None:
    """Return the residual scale/skew transform for one spin-flip glyph."""
    direction = 1.0 if transition.phase == "exit" else -1.0
    skew_y = direction * spin_flip_skew(opacity)
    center_x = glyph.left + glyph.width / 2
    center_y = baseline_y - glyph.metrics.ascent() + glyph.metrics.height() / 2
    transform = character_transform(
        center_x=center_x,
        center_y=center_y,
        scale_x=opacity,
        scale_y=opacity,
        skew_y=skew_y,
    )
    return None if transform.isIdentity() else transform


def char_drip_char_transform(
    glyph: GlyphLayout,
    baseline_y: int,
    transition: LineCharTransition,
    progress: float,
) -> QTransform | None:
    """Return N3's corner-pivot shear transform for one CharDrip glyph."""
    direction = 1.0 if transition.phase == "entry" else -1.0
    skew_y = direction * spin_flip_skew(progress)
    pivot_x = glyph.left + glyph.width
    pivot_y = (
        baseline_y
        if transition.phase == "entry"
        else baseline_y - glyph.metrics.height()
    )
    transform = character_transform(
        center_x=pivot_x,
        center_y=pivot_y,
        skew_y=skew_y,
    )
    return None if transform.isIdentity() else transform


def transition_char_state(
    style: Style,
    transition: LineCharTransition,
    index: int,
    count: int,
    *,
    char_start_ms: int | None = None,
    char_end_ms: int | None = None,
    t_ms: int | None = None,
    frame_height: int | None = None,
    following_done_ms: int | None = None,
) -> tuple[float, float, float, float, float, float, float]:
    if transition.effect == "utopia" and transition.phase == "utopia":
        if (
            style.entry_anim == "utopia"
            and t_ms is not None
            and transition.start_ms is not None
            and t_ms <= transition.start_ms + UTOPIA_INTRO_TIME_MS
        ):
            intro_transition = LineCharTransition(
                phase="entry",
                effect="utopia",
                progress=clamped_ratio(
                    t_ms - transition.start_ms,
                    UTOPIA_INTRO_TIME_MS,
                ),
                start_ms=transition.start_ms,
                end_ms=transition.start_ms + UTOPIA_INTRO_TIME_MS,
            )
            return transition_char_state(
                style,
                intro_transition,
                index,
                count,
                char_start_ms=char_start_ms,
                char_end_ms=char_end_ms,
                t_ms=t_ms,
                frame_height=frame_height,
                following_done_ms=following_done_ms,
            )
        if (
            style.exit_anim == "utopia"
            and t_ms is not None
            and following_done_ms is not None
            and t_ms > following_done_ms
        ):
            outro_transition = LineCharTransition(
                phase="exit",
                effect="utopia",
                progress=1.0,
            )
            return transition_char_state(
                style,
                outro_transition,
                index,
                count,
                char_start_ms=char_start_ms,
                char_end_ms=char_end_ms,
                t_ms=t_ms,
                frame_height=frame_height,
                following_done_ms=following_done_ms,
            )
        if (
            effective_karaoke_animation(style) == "utopia"
            and t_ms is not None
            and char_start_ms is not None
            and char_end_ms is not None
            and is_utopia_wiping(t_ms, char_start_ms, char_end_ms)
        ):
            wipe_transition = LineCharTransition(
                phase="wipe",
                effect="utopia",
                progress=1.0,
            )
            return transition_char_state(
                style,
                wipe_transition,
                index,
                count,
                char_start_ms=char_start_ms,
                char_end_ms=char_end_ms,
                t_ms=t_ms,
                frame_height=frame_height,
                following_done_ms=following_done_ms,
            )
        return 1.0, 0.0, 0.0, 0.0, 1.0, 1.0, 0.0

    if transition.effect == "utopia" and transition.phase == "entry":
        if t_ms is None or transition.start_ms is None:
            local = staggered_char_progress(transition.progress, index, count)
            opacity = min(max(local, 0.0), 1.0)
            return opacity, 0.0, 0.0, 0.0, opacity, opacity, 0.0
        delay = utopia_intro_delay_step(count) * index
        elapsed = t_ms - transition.start_ms - delay
        if elapsed < 0:
            return 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0
        opacity = min(elapsed / UTOPIA_INTRO_ENLARGE_MS, 1.0)
        if elapsed < UTOPIA_INTRO_ENLARGE_MS:
            scale = UTOPIA_INTRO_OVER_RATIO * elapsed / UTOPIA_INTRO_ENLARGE_MS
        elif elapsed < UTOPIA_INTRO_ENLARGE_MS + UTOPIA_INTRO_CONDENSE_MS:
            remaining = (
                UTOPIA_INTRO_ENLARGE_MS
                + UTOPIA_INTRO_CONDENSE_MS
                - elapsed
            )
            scale = 1.0 + (
                (UTOPIA_INTRO_OVER_RATIO - 1.0)
                * remaining
                / UTOPIA_INTRO_CONDENSE_MS
            )
        else:
            scale = 1.0
        return opacity, 0.0, 0.0, 0.0, scale, scale, 0.0

    if transition.phase == "exit" and transition.effect == "utopia":
        if t_ms is None:
            local = transition.progress
        else:
            done_ms = (
                following_done_ms
                if following_done_ms is not None
                else char_end_ms
            )
            if done_ms is None:
                local = transition.progress
            else:
                local = (t_ms - done_ms) / UTOPIA_FADE_OUT_TIME_MS
        local = min(max(local, 0.0), 1.0)
        opacity = max(0.0, 1.0 - local)
        shrink = 1.0 - local
        height = frame_height if frame_height and frame_height > 0 else 1080
        amp = height / 15.0
        if local <= 0.5:
            x_travel = math.sin(math.pi * local) * amp
        else:
            x_travel = amp + math.sin((local - 0.5) * math.pi) * amp
        y_travel = math.sin(math.pi * local / 2.0) * amp
        x_flip = math.cos(math.pi * local)
        rotation = -180.0 * local
        return (
            opacity,
            -x_travel,
            y_travel,
            rotation,
            shrink * x_flip,
            shrink,
            0.0,
        )

    if transition.phase == "wipe" and transition.effect == "utopia":
        if char_start_ms is None or char_end_ms is None or t_ms is None:
            return 1.0, 0.0, 0.0, 0.0, 1.0, 1.0, 0.0
        scale = utopia_wipe_scale(t_ms, char_start_ms, char_end_ms)
        return 1.0, 0.0, 0.0, 0.0, scale, scale, 0.0

    if transition.effect in {"char_fade", "char_drip", "spin_flip"}:
        progress = char_fade_opacity(
            transition,
            index,
            count,
            t_ms=t_ms,
        )
        if transition.effect == "spin_flip":
            direction = 1.0 if transition.phase == "exit" else -1.0
            skew_y = direction * spin_flip_skew(progress)
            return progress, 0.0, 0.0, 0.0, progress, progress, skew_y
        if transition.effect == "char_drip":
            direction = 1.0 if transition.phase == "entry" else -1.0
            skew_y = direction * spin_flip_skew(progress)
            return float(progress > 0.0), 0.0, 0.0, 0.0, 1.0, 1.0, skew_y
        return progress, 0.0, 0.0, 0.0, 1.0, 1.0, 0.0

    local = staggered_char_progress(transition.progress, index, count)
    eased = 1.0 - (1.0 - local) * (1.0 - local)
    if transition.phase == "entry":
        opacity = 0.22 + 0.78 * eased
        return opacity, 0.0, 0.0, 0.0, 1.0, 1.0, 0.0

    opacity = 1.0 - eased
    if transition.effect == "utopia":
        return 0.0, 0.0, 0.0, 0.0, 1.0, 1.0, 0.0
    return opacity, 0.0, 0.0, 0.0, 1.0, 1.0, 0.0


def character_transform(
    *,
    center_x: float,
    center_y: float,
    dx: float = 0.0,
    dy: float = 0.0,
    rotation: float = 0.0,
    scale_x: float = 1.0,
    scale_y: float = 1.0,
    skew_y: float = 0.0,
    scale_origin_x: float | None = None,
    scale_origin_y: float | None = None,
) -> QTransform:
    transform = QTransform()
    if (
        not dx
        and not dy
        and not rotation
        and scale_x == 1.0
        and scale_y == 1.0
        and not skew_y
    ):
        return transform
    if scale_origin_x is not None and scale_origin_y is not None:
        transform.translate(scale_origin_x + dx, scale_origin_y + dy)
        if skew_y:
            transform.shear(0.0, skew_y)
        if scale_x != 1.0 or scale_y != 1.0:
            transform.scale(scale_x, scale_y)
        transform.translate(center_x - scale_origin_x, center_y - scale_origin_y)
        if rotation:
            transform.rotate(rotation)
        transform.translate(-center_x, -center_y)
        return transform
    transform.translate(center_x + dx, center_y + dy)
    if rotation:
        transform.rotate(rotation)
    if skew_y:
        transform.shear(0.0, skew_y)
    if scale_x != 1.0 or scale_y != 1.0:
        transform.scale(scale_x, scale_y)
    transform.translate(-center_x, -center_y)
    return transform


def utopia_intro_delay_step(count: int) -> int:
    if count <= 1:
        return 0
    return UTOPIA_INTRO_DELAY_MS // (count - 1)


def is_utopia_wiping(t_ms: int, char_start_ms: int, char_end_ms: int) -> bool:
    return char_start_ms < t_ms < char_end_ms and char_start_ms != char_end_ms


def utopia_wipe_scale(
    t_ms: int,
    char_start_ms: int,
    char_end_ms: int,
) -> float:
    if not is_utopia_wiping(t_ms, char_start_ms, char_end_ms):
        return 1.0
    over_ms = min(
        int((char_end_ms - char_start_ms) * UTOPIA_WIPE_OVER_TIME_RATIO),
        UTOPIA_WIPE_OVER_TIME_LIMIT_MS,
    )
    if over_ms <= 0:
        return 1.0
    peak_ms = char_start_ms + over_ms
    if t_ms <= peak_ms:
        progress = (t_ms - char_start_ms) / over_ms
    else:
        release_ms = max(char_end_ms - peak_ms, 1)
        progress = (char_end_ms - t_ms) / release_ms
    return 1.0 + (
        (UTOPIA_WIPE_OVER_RATIO - 1.0)
        * min(max(progress, 0.0), 1.0)
    )


def utopia_following_done_time(
    line: TimingLine,
    intervals: list[tuple[int, int]],
    index: int,
    style: Style,
) -> int:
    if not intervals:
        return line_end_ms(line)
    index = min(max(index, 0), len(intervals) - 1)
    current_end = intervals[index][1]
    next_index = next_valid_char_index(line, index + 1)
    if next_index is not None and next_index < len(intervals):
        next_end = intervals[next_index][1]
        if current_end <= next_end:
            return next_end
    return current_end + utopia_tail_delay_ms(style)


def next_valid_char_index(line: TimingLine, start_index: int) -> int | None:
    for index in range(start_index, len(line.chars)):
        text = line.chars[index].text
        if text and not text.isspace():
            return index
    return None


def utopia_tail_delay_ms(style: Style) -> int:
    return max(0, style.line_tail_ms - UTOPIA_FADE_OUT_TIME_MS)


def char_fade_delay_step(count: int) -> int:
    if count <= 1:
        return 0
    return CHAR_FADE_INTRO_DELAY_MS // (count - 1)


def char_fade_opacity(
    transition: LineCharTransition,
    index: int,
    count: int,
    *,
    t_ms: int | None,
) -> float:
    if t_ms is None:
        return transition.progress
    if transition.phase == "entry":
        start_ms = (
            (transition.start_ms or 0)
            + char_fade_delay_step(count) * index
        )
        return clamped_ratio(t_ms - start_ms, CHAR_FADE_IN_TIME_MS)
    if transition.phase == "exit":
        end_ms = (
            (transition.end_ms or t_ms)
            - char_fade_delay_step(count) * (count - index - 1)
        )
        if t_ms > end_ms:
            return 0.0
        if t_ms < end_ms - CHAR_FADE_OUT_TIME_MS:
            return 1.0
        return clamped_ratio(end_ms - t_ms, CHAR_FADE_OUT_TIME_MS)
    return 1.0


def spin_flip_skew(opacity: float) -> float:
    opacity = max(0.0, min(1.0, opacity))
    if opacity <= 0.0:
        return 0.0
    angle = (math.pi / 2.0) * (1.0 - opacity)
    return math.tan(min(angle, math.radians(89.0)))


def staggered_char_progress(progress: float, index: int, count: int) -> float:
    if count <= 1:
        return progress
    span = 0.68
    window = 1.0 - span
    offset = (index / max(count - 1, 1)) * span
    return max(0.0, min(1.0, (progress - offset) / window))


def clamped_ratio(elapsed_ms: int, duration_ms: int) -> float:
    if duration_ms <= 0:
        return 1.0
    return max(0.0, min(1.0, elapsed_ms / duration_ms))
