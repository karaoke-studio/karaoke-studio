"""Pure character-transition state and transform calculations."""

from __future__ import annotations

import math

from PyQt6.QtGui import QTransform

from krok_helper.subtitle_render.domain.models import (
    Style,
    effective_karaoke_animation,
    effective_karaoke_zoom_pulse,
)
from krok_helper.subtitle_render.domain.timing import (
    CHAR_TRANSITION_ANIMS,
    ENTRY_ASSEMBLE_ANIMS,
    ENTRY_GEO_ANIMS,
    ENTRY_GLOW_ANIMS,
    ENTRY_PARTICLE_ANIMS,
    ENTRY_STRETCH_ANIMS,
    EXIT_ASSEMBLE_ANIMS,
    EXIT_GEO_ANIMS,
    EXIT_GLOW_ANIMS,
    EXIT_PARTICLE_ANIMS,
    EXIT_STRETCH_ANIMS,
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
# 整字放大（zoom_pulse）：整个唱字期间缓出放大到峰值，唱字结束后
# 固定 ZOOM_PULSE_SHRINK_MS 缓入缩回。曲线用多项式近似贝塞尔
# （缓出 1-(1-p)^n / 缓入 1-q^n），峰值两侧导数为 0，停留感最强；
# 阶数 n 由 Style.zoom_pulse_curve_level（0~5）调节，0/1 为线性。
ZOOM_PULSE_PEAK_RATIO = 1.25
ZOOM_PULSE_SHRINK_MS = 300
ZOOM_PULSE_DEFAULT_CURVE_LEVEL = 1
CHAR_FADE_INTRO_DELAY_MS = 350
CHAR_FADE_IN_TIME_MS = 250
CHAR_FADE_OUT_TIME_MS = 250
# ---- 2026-09 逐字几何特效幅度参数（与 C++ d2d_fx.cpp 镜像，改动需双侧同步）----
# 编排与 char_fade 完全同构：「入场/退场动画时长」旋钮仅作 >0 的播放门，
# 窗口 600ms，逐字错峰步距 350ms//(字数-1)、每字行程 250ms（常量，不随旋钮缩放）。
# 入场透明度 4 倍速上升（行程前 1/4 即全不透明）、运动包络 smoothstep，
# 保证字符「先清晰可见、再走完收拢/上浮行程」。
# 字距收拢（tracking_in）：从两侧按行中心归一化位置展开的行程（em）。
TRACKING_SPREAD_EM = 1.6
# 波浪上浮（wave_in）：自下方浮起的振幅（em，带最小像素下限）。
WAVE_AMPLITUDE_EM = 0.5
WAVE_AMPLITUDE_MIN_PX = 24.0
# 碎散爆开（scatter_out）：随机方向飞散的行程（em）与缩水量。
SCATTER_TRAVEL_EM = 2.2
SCATTER_SHRINK = 0.4
# 收拢消散（converge_out）：向行中心收拢的缩水量。
CONVERGE_SHRINK = 0.2
# ---- 辉光浮现/消散（glow_in / glow_out，2026-09 参考视频底部字幕）----
# 多级圆角描边层近似高斯衰减的弥散光晕：(描边宽 em 占半径系数, 相对强度)。
# 双端镜像：C++ d2d_fx.cpp kGlowHaloStrokes。
GLOW_HALO_STROKES: tuple[tuple[float, float], ...] = (
    (0.10, 0.42),
    (0.22, 0.24),
    (0.38, 0.14),
    (0.58, 0.08),
    (0.82, 0.045),
)
# 辉光浮现/消散 = 细密横向回声（参考图口径）：每侧 N 个字形重影副本、
# 固定间距 pitch、亮度几何衰减，叠出可分辨残像结构的横向光痕。
GLOW_ECHO_PITCH_EM = 0.12
GLOW_ECHO_COPIES = 14
GLOW_ECHO_DECAY = 0.82


def fx_unit_hash(index: int, salt: int) -> float:
    """Deterministic per-index hash in [0, 1)；Python/C++ 共用的整数哈希。

    纯整数运算（uint32 溢出截断）保证两侧逐位一致，末尾一次除法得到
    24bit 定点小数。碎散/粒子的随机量全部由此派生，禁用语言级随机库。
    """
    h = (index * 0x85EBCA77 + salt * 0xC2B2AE3D) & 0xFFFFFFFF
    h ^= h >> 15
    h = (h * 0x2545F491) & 0xFFFFFFFF
    h ^= h >> 13
    return (h & 0xFFFFFF) / 0x1000000


def _center_norm(index: int, count: int) -> float:
    """字符中心相对行中心的归一化横向位置（-1 左缘 .. +1 右缘）。"""
    if count <= 1:
        return 0.0
    return (index - (count - 1) / 2.0) / ((count - 1) / 2.0)


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
        style.exit_anim in CHAR_TRANSITION_ANIMS
        and style.exit_fade_ms > 0
    ):
        # 旧 char_fade 家族保持历史固定 600ms 窗；新档位族（几何/辉光/
        # 粒子类）窗口 = 时长旋钮值，与编排缩放一致。
        if style.exit_anim in {"char_fade", "char_drip", "spin_flip"}:
            window_ms = CHAR_FADE_INTRO_DELAY_MS + CHAR_FADE_OUT_TIME_MS
        else:
            window_ms = min(max(style.exit_fade_ms, 120), 3000)
        exit_start = max(
            line_end_ms(line),
            end - window_ms,
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
        style.entry_anim in CHAR_TRANSITION_ANIMS
        and style.entry_lead_ms > 0
    ):
        if style.entry_anim in {"char_fade", "char_drip", "spin_flip"}:
            window_ms = CHAR_FADE_INTRO_DELAY_MS + CHAR_FADE_IN_TIME_MS
        else:
            window_ms = min(max(style.entry_lead_ms, 120), 3000)
        entry_end = start + window_ms
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


def _transition_char_state_core(
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
    char_center_x: float | None = None,
    line_center_x: float | None = None,
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
            and (
                is_zoom_pulse_active(t_ms, char_start_ms, char_end_ms)
                if effective_karaoke_zoom_pulse(style)
                else is_utopia_wiping(t_ms, char_start_ms, char_end_ms)
            )
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
        scale = (
            zoom_pulse_wipe_scale(
                t_ms, char_start_ms, char_end_ms, zoom_pulse_curve_level(style)
            )
            if effective_karaoke_zoom_pulse(style)
            else utopia_wipe_scale(t_ms, char_start_ms, char_end_ms)
        )
        return 1.0, 0.0, 0.0, 0.0, scale, scale, 0.0

    if (
        transition.effect
        in ENTRY_GEO_ANIMS | EXIT_GEO_ANIMS
        | ENTRY_GLOW_ANIMS | EXIT_GLOW_ANIMS
        | ENTRY_STRETCH_ANIMS | EXIT_STRETCH_ANIMS
        | ENTRY_ASSEMBLE_ANIMS | EXIT_ASSEMBLE_ANIMS
        | ENTRY_PARTICLE_ANIMS | EXIT_PARTICLE_ANIMS
    ):
        return _geo_char_state(
            style,
            transition,
            index,
            count,
            t_ms=t_ms,
            char_center_x=char_center_x,
            line_center_x=line_center_x,
        )

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


def zoom_pulse_overlay_scale(
    style: Style,
    t_ms: int | None,
    char_start_ms: int | None,
    char_end_ms: int | None,
) -> float:
    """缩回期间叠加在退场等其它逐字变换上的整字放大缩放系数。

    整字放大与退场是**并行**的：退场照原时间开始，缩放曲线继续乘在退场的
    旋转/位移/缩水上，直到 300ms 缩回走完；不再让退场把缩放重置为 1.0。
    """

    if (
        t_ms is None
        or char_start_ms is None
        or char_end_ms is None
        or not effective_karaoke_zoom_pulse(style)
    ):
        return 1.0
    if not is_zoom_pulse_active(t_ms, char_start_ms, char_end_ms):
        return 1.0
    return zoom_pulse_wipe_scale(
        t_ms,
        char_start_ms,
        char_end_ms,
        zoom_pulse_curve_level(style),
    )


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
    char_center_x: float | None = None,
    line_center_x: float | None = None,
) -> tuple[float, float, float, float, float, float, float]:
    """Per-character transform state; zoom-pulse overlays exit transforms.

    退场相位下整字放大不打断：缩放系数乘到退场自身的 scale 上（退场照常
    开始、照常结束），行尾最后两个字因此不会再从放大峰值跳回静态原大。
    """

    state = _transition_char_state_core(
        style,
        transition,
        index,
        count,
        char_start_ms=char_start_ms,
        char_end_ms=char_end_ms,
        t_ms=t_ms,
        frame_height=frame_height,
        following_done_ms=following_done_ms,
        char_center_x=char_center_x,
        line_center_x=line_center_x,
    )
    if transition.phase != "exit":
        return state
    overlay = zoom_pulse_overlay_scale(style, t_ms, char_start_ms, char_end_ms)
    if overlay == 1.0:
        return state
    opacity, dx, dy, rotation, scale_x, scale_y, skew_y = state
    return (
        opacity,
        dx,
        dy,
        rotation,
        scale_x * overlay,
        scale_y * overlay,
        skew_y,
    )


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


def character_scale_origin(
    style: Style,
    left: float,
    baseline_y: float,
) -> tuple[float | None, float | None]:
    """Pick the scale origin for Utopia-style character transforms.

    整字放大（zoom_pulse）以字符中心为缩放原点四面对称生长；其余 utopia
    相位维持 N3 语义的「字框左缘 + 基线」。相位边界处的缩放均为恒等，
    原点切换不产生位置跳变。
    """
    if effective_karaoke_zoom_pulse(style):
        return None, None
    return left, baseline_y


def _geo_char_state(
    style: Style,
    transition: LineCharTransition,
    index: int,
    count: int,
    *,
    t_ms: int | None,
    char_center_x: float | None,
    line_center_x: float | None,
) -> tuple[float, float, float, float, float, float, float]:
    """逐字几何特效（tracking_in / wave_in / scatter_out / converge_out）。

    编排与 char_fade 完全同构：「入场/退场动画时长」仅作 >0 的播放门，
    固定 350ms 错峰 + 250ms 行程（窗口 600ms），不随旋钮值缩放。随机量经
    :func:`fx_unit_hash` 派生（与 C++ d2d_fx.cpp 逐位一致）。
    ``t_ms`` 缺省（旧调用/静态探针）时退化为纯透明度插值。
    """
    font_px = max(float(getattr(style, "font_size_px", 0.0) or 0.0), 1.0)
    # 编排总时长由「入场/退场动画时长」旋钮驱动（与旧的淡入/滑入/上移
    # 同口径）：0 = 默认 600ms（保持选中即播），>0 按值缩放；错峰:行程
    # 保持 7:5（默认 350/250ms）。
    if transition.phase == "exit":
        configured = int(getattr(style, "exit_fade_ms", 0) or 0)
    else:
        configured = int(getattr(style, "entry_lead_ms", 0) or 0)
    # 旧语义：时长 0 = 无动画（context 门已拦，这里只做量程钳制）。
    total = min(max(configured, 120), 3000)
    stagger_total = total * 7 // 12
    travel_total = max(total - stagger_total, 40)
    def _stagger_progress() -> float:
        if t_ms is None or transition.start_ms is None:
            return transition.progress
        step = 0 if count <= 1 else stagger_total // (count - 1)
        return clamped_ratio(
            t_ms - transition.start_ms - step * index, travel_total
        )

    def _line_progress() -> float:
        # 整行同步（辉光浮现/消散）：无逐字错峰，全字共享同一时间线。
        if t_ms is None or transition.start_ms is None:
            return transition.progress
        return clamped_ratio(t_ms - transition.start_ms, travel_total)

    if transition.effect == "tracking_in":
        p = _stagger_progress()
        q = p * p * (3.0 - 2.0 * p)
        spread = font_px * TRACKING_SPREAD_EM
        dx = _center_norm(index, count) * spread * (1.0 - q)
        opacity = min(p * 4.0, 1.0)
        return opacity, dx, 0.0, 0.0, 1.0, 1.0, 0.0

    if transition.effect == "wave_in":
        p = _stagger_progress()
        q = p * p * (3.0 - 2.0 * p)
        amplitude = max(font_px * WAVE_AMPLITUDE_EM, WAVE_AMPLITUDE_MIN_PX)
        dy = amplitude * (1.0 - q)
        opacity = min(p * 4.0, 1.0)
        return opacity, 0.0, dy, 0.0, 1.0, 1.0, 0.0

    if transition.effect == "scatter_out":
        p = _stagger_progress()
        theta = fx_unit_hash(index, 1) * 2.0 * math.pi
        speed = 0.55 + 0.45 * fx_unit_hash(index, 2)
        spin = (fx_unit_hash(index, 3) - 0.5) * 720.0
        travel = font_px * SCATTER_TRAVEL_EM * speed * (1.0 - (1.0 - p) ** 2)
        scale = 1.0 - SCATTER_SHRINK * p
        fade = (1.0 - p) * (1.0 - p)
        return (
            fade,
            math.cos(theta) * travel,
            math.sin(theta) * travel,
            spin * p,
            scale,
            scale,
            0.0,
        )

    if transition.effect == "stretch_in":
        # 逐字拉伸入场（光条凝聚，Aegisub \fscx+\blur+\t 同款语言）：字符自
        # 3.2× 横向拉伸的弥散光条横向收束成清晰字形，光晕同步熄灭。
        p = _stagger_progress()
        q = p * p * (3.0 - 2.0 * p)
        return min(p * 4.0, 1.0), 0.0, 0.0, 0.0, 1.0 + 2.2 * (1.0 - q), 1.0, 0.0

    if transition.effect == "stretch_out":
        # 逐字拉伸退场：反向——清晰字形横向拉伸成光条弥散，同时淡出。
        p = _stagger_progress()
        q = p * p * (3.0 - 2.0 * p)
        return 1.0 - q, 0.0, 0.0, 0.0, 1.0 + 2.2 * q, 1.0, 0.0

    if transition.effect == "glow_in":
        # 辉光浮现（整行同步，无逐字错峰）：细密横向回声自两侧收拢
        # 熄灭，文字快速显形；本体轻度横向拉伸后回弹（1.6×→1.0）。
        p = _line_progress()
        q = p * p * (3.0 - 2.0 * p)
        return min(p * 3.0, 1.0), 0.0, 0.0, 0.0, 1.0 + 0.6 * (1.0 - q), 1.0, 0.0

    if transition.effect == "glow_out":
        # 辉光消散（整行同步）：回声外扩变疏，文字本体轻度拉伸后随
        # 光痕一同淡出（1.0→1.6×）。
        p = _line_progress()
        q = p * p * (3.0 - 2.0 * p)
        return 1.0 - q, 0.0, 0.0, 0.0, 1.0 + 0.6 * q, 1.0, 0.0

    if transition.effect == "flame":
        # 火焰档文字本体（2026-10-18 用户口径）：参考既有 stretch_in/out
        # 的“光条凝聚/弥散”语言，但**延后到火烧到后段**才发生——
        #   入场：前 62% 窗口完全隐形（只有火在烧），后 38% 由 3.2× 横向
        #   光条快速收束成字形（透明度 4 倍速上升）；
        #   退场：前 62% 保持清晰，后 38% 随火势渐尽横向拉伸成光条淡出。
        # 不叠 stretch 的辉光层：火焰粒子本身已是高亮层，避免互抢亮度。
        p = _stagger_progress()
        raw = min(max((p - 0.62) / 0.38, 0.0), 1.0)
        q = raw * raw * (3.0 - 2.0 * raw)
        if transition.phase == "exit":
            return 1.0 - q, 0.0, 0.0, 0.0, 1.0 + 2.2 * q, 1.0, 0.0
        return min(raw * 4.0, 1.0), 0.0, 0.0, 0.0, 1.0 + 2.2 * (1.0 - q), 1.0, 0.0

    if transition.effect in {
        "sparkle", "ripple", "note", "heart", "petal", "snow", "snow_solid",
        "feather", "lightning",
    }:
        # 粒子类出入场动画的本体：文字逐字显形（入场 4 倍速透明度，
        # 粒子拼接同款编排），退场逐字淡出；粒子由 planner 叠加。
        p = _stagger_progress()
        if transition.phase == "exit":
            q = p * p * (3.0 - 2.0 * p)
            return 1.0 - q, 0.0, 0.0, 0.0, 1.0, 1.0, 0.0
        return min(p * 4.0, 1.0), 0.0, 0.0, 0.0, 1.0, 1.0, 0.0

    if transition.effect == "assemble_in":
        # 粒子拼接：粒子飞入期间字形尚未成形，抵达后字形才显形。
        p = _stagger_progress()
        appear = p * p * (3.0 - 2.0 * p)
        return max(0.0, (appear - 0.45) / 0.55), 0.0, 0.0, 0.0, 1.0, 1.0, 0.0

    if transition.effect == "dissolve_out":
        # 粒子消散：字形先行散去，粒子随后飞离。
        p = _stagger_progress()
        vanish = min(max(p / 0.55, 0.0), 1.0)
        return 1.0 - vanish * vanish * (3.0 - 2.0 * vanish), 0.0, 0.0, 0.0, 1.0, 1.0, 0.0

    # converge_out：向行中心收拢淡出；优先用调用方传入的真实中心坐标
    #（painter 的字形布局 / C++ 的 CachedChar），缺省回退 index 归一化估算。
    p = _stagger_progress()
    eased = p * p * (3.0 - 2.0 * p)
    if char_center_x is not None and line_center_x is not None:
        dx = (line_center_x - char_center_x) * eased
    else:
        half_width_em = count * 0.5
        dx = -_center_norm(index, count) * font_px * half_width_em * eased
    scale = 1.0 - CONVERGE_SHRINK * eased
    return 1.0 - eased, dx, 0.0, 0.0, scale, scale, 0.0


def transition_char_glow(
    transition: LineCharTransition,
    index: int,
    count: int,
    *,
    t_ms: int | None,
    configured_ms: int = 0,
) -> tuple[float, float, bool] | None:
    """拉伸/辉光类特效在 ``t_ms`` 的辉光状态 ``(强度, 间距/半径系数, 回声)``。

    非辉光类特效返回 ``None``。编排总时长由 ``configured_ms``（时长旋钮，
    0 = 默认 600ms）缩放；逐字拉伸沿用错峰编排与常规描边表，辉光浮现/
    消散整行同步并用横向回声。与 C++ d2d_fx.cpp 的 glow 字段镜像。
    """
    effect = transition.effect
    if (
        effect
        not in ENTRY_GLOW_ANIMS | EXIT_GLOW_ANIMS
        | ENTRY_STRETCH_ANIMS | EXIT_STRETCH_ANIMS
    ):
        return None
    if t_ms is None or transition.start_ms is None:
        return None
    # 旧语义：时长 0 = 无辉光（调用方仅在门内到达）；量程钳制。
    total = min(max(int(configured_ms), 120), 3000)
    travel_total = max(total - total * 7 // 12, 40)
    step = 0 if count <= 1 else (total * 7 // 12) // (count - 1)
    if effect in ENTRY_GLOW_ANIMS | EXIT_GLOW_ANIMS:
        step = 0
    p = clamped_ratio(
        t_ms - transition.start_ms - step * index, travel_total
    )
    q = p * p * (3.0 - 2.0 * p)
    if effect == "stretch_in":
        return (1.0 - q) ** 1.5, 1.2 - 0.5 * q, False
    if effect == "stretch_out":
        return math.sin(math.pi * q) * 0.9, 0.7 + 0.8 * q, False
    if effect == "glow_in":
        # 整行辉光（横向回声）：出生即满强度，收束时间距略收紧后熄灭。
        return (1.0 - q) * math.sqrt(1.0 - q), 1.0 - 0.25 * q, True
    # 辉光消散：正弦包络，回声间距随消散外扩变疏。
    return math.sin(math.pi * q), 1.0 + 0.9 * q, True


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


def is_zoom_pulse_active(t_ms: int, char_start_ms: int, char_end_ms: int) -> bool:
    return (
        char_start_ms != char_end_ms
        and char_start_ms < t_ms < char_end_ms + ZOOM_PULSE_SHRINK_MS
    )


def zoom_pulse_curve_level(style: Style) -> int:
    """Clamp the zoom-pulse easing order to the supported 0..5 range."""
    try:
        level = int(style.zoom_pulse_curve_level)
    except (AttributeError, TypeError, ValueError):
        return ZOOM_PULSE_DEFAULT_CURVE_LEVEL
    return max(0, min(5, level))


def zoom_pulse_wipe_scale(
    t_ms: int,
    char_start_ms: int,
    char_end_ms: int,
    curve_level: int = ZOOM_PULSE_DEFAULT_CURVE_LEVEL,
) -> float:
    if not is_zoom_pulse_active(t_ms, char_start_ms, char_end_ms):
        return 1.0
    level = max(0, min(5, int(curve_level)))
    if t_ms < char_end_ms:
        # 缓出：起点快速离开 1.0，逼近峰值时导数→0（峰值停留）。
        progress = (t_ms - char_start_ms) / (char_end_ms - char_start_ms)
        eased = progress if level <= 0 else 1.0 - (1.0 - progress) ** level
        return 1.0 + (ZOOM_PULSE_PEAK_RATIO - 1.0) * eased
    # 缓入：刚唱完时导数≈0（继续停留在峰值附近），结尾快速落回 1.0。
    shrink = (t_ms - char_end_ms) / ZOOM_PULSE_SHRINK_MS
    eased = 1.0 - shrink if level <= 0 else 1.0 - shrink**level
    return 1.0 + (ZOOM_PULSE_PEAK_RATIO - 1.0) * eased


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
