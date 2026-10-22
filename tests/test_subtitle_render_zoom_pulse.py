"""整字放大（zoom_pulse）model/serialization/curve/painter contracts."""

from __future__ import annotations

import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtGui import QImage  # noqa: E402

from krok_helper.subtitle_render.domain.models import (  # noqa: E402
    Style,
    effective_karaoke_animation,
    effective_karaoke_zoom_pulse,
    style_from_dict,
    style_to_dict,
    style_with_line_animation,
)
from krok_helper.subtitle_render.domain.timing import (  # noqa: E402
    LineAnimationOverride,
    TimingChar,
    TimingLine,
    TimingTrack,
)
from krok_helper.subtitle_render.native.protocol import (  # noqa: E402
    gpu_unsupported_features,
)
from krok_helper.subtitle_render.serialization.timing import (  # noqa: E402
    line_animation_override_from_dict,
    line_animation_override_to_dict,
)
from krok_helper.subtitle_render.engine.painter import paint_frame  # noqa: E402
from krok_helper.subtitle_render.engine.render.elements.horizontal.contracts import (  # noqa: E402
    LineCharTransition,
)
from krok_helper.subtitle_render.engine.render.elements.horizontal.transitions import (  # noqa: E402
    ZOOM_PULSE_PEAK_RATIO,
    ZOOM_PULSE_SHRINK_MS,
    character_scale_origin,
    is_zoom_pulse_active,
    transition_char_state,
    zoom_pulse_curve_level,
    zoom_pulse_wipe_scale,
)


def _zoom_pulse_style(**changes) -> Style:
    base = dict(
        entry_anim="none",
        exit_anim="none",
        karaoke_anim="zoom_pulse",
        sync_entry=False,
        sync_ending=False,
        sync_each_page=False,
        line_lead_in_ms=0,
        line_tail_ms=200,
    )
    base.update(changes)
    return Style(**base)


def _single_char_track() -> TimingTrack:
    return TimingTrack(
        lines=[TimingLine(chars=[TimingChar("歌", 0)], end_ms=1000)]
    )


def _utopia_transition(start_ms: int = 0, end_ms: int = 2000) -> LineCharTransition:
    return LineCharTransition(
        phase="utopia",
        effect="utopia",
        progress=1.0,
        start_ms=start_ms,
        end_ms=end_ms,
    )


# ---------------------------------------------------------------------------
# 模型与序列化
# ---------------------------------------------------------------------------


def test_zoom_pulse_resolves_to_utopia_base_animation() -> None:
    assert effective_karaoke_animation(_zoom_pulse_style()) == "utopia"
    # 组合档同样按 utopia 本体渲染。
    assert (
        effective_karaoke_animation(_zoom_pulse_style(karaoke_anim="zoom_pulse_scanline"))
        == "utopia"
    )
    # 其它档位不受影响。
    assert effective_karaoke_animation(_zoom_pulse_style(karaoke_anim="none")) == "none"
    assert effective_karaoke_animation(_zoom_pulse_style(karaoke_anim="utopia")) == "utopia"


def test_effective_karaoke_zoom_pulse_only_accepts_explicit_mode() -> None:
    assert effective_karaoke_zoom_pulse(_zoom_pulse_style())
    assert effective_karaoke_zoom_pulse(
        _zoom_pulse_style(karaoke_anim="zoom_pulse_scanline")
    )
    assert not effective_karaoke_zoom_pulse(_zoom_pulse_style(karaoke_anim="utopia"))
    assert not effective_karaoke_zoom_pulse(
        _zoom_pulse_style(karaoke_anim="utopia_scanline")
    )
    assert not effective_karaoke_zoom_pulse(_zoom_pulse_style(karaoke_anim="none"))
    # inherit 的旧推导（入退场含 utopia）不产生整字放大。
    assert not effective_karaoke_zoom_pulse(
        _zoom_pulse_style(karaoke_anim="inherit", entry_anim="utopia")
    )


def test_zoom_pulse_scanline_combines_both_overlays() -> None:
    from krok_helper.subtitle_render.domain.models import effective_karaoke_scanline

    combo = _zoom_pulse_style(karaoke_anim="zoom_pulse_scanline")
    assert effective_karaoke_scanline(combo)
    assert effective_karaoke_zoom_pulse(combo)
    # 纯整字放大档不点亮扫字线。
    assert not effective_karaoke_scanline(_zoom_pulse_style())


def test_zoom_pulse_scanline_serializes_and_bakes_per_line() -> None:
    style = _zoom_pulse_style(karaoke_anim="zoom_pulse_scanline")
    restored = style_from_dict(style_to_dict(style))
    assert restored.karaoke_anim == "zoom_pulse_scanline"
    override = line_animation_override_from_dict(
        {"entry_anim": "none", "exit_anim": "none", "karaoke_anim": "zoom_pulse_scanline"}
    )
    assert override is not None and override.karaoke_anim == "zoom_pulse_scanline"
    reverse = TimingLine(
        chars=[TimingChar("歌", 0)], end_ms=500, wipe_reverse=True
    )
    assert effective_karaoke_zoom_pulse(style_with_line_animation(style, reverse))


def test_zoom_pulse_scanline_gpu_ir_marks_all_three_flags() -> None:
    from krok_helper.subtitle_render.engine.render.render_ir import build_render_ir

    ir = build_render_ir(
        _single_char_track(),
        _zoom_pulse_style(karaoke_anim="zoom_pulse_scanline"),
        width=640,
        height=360,
        fps=60,
    )
    line = ir["track"]["lines"][0]
    assert line["karaoke_anim"] == "utopia"
    assert line["scanline"] is True
    assert line["zoom_pulse"] is True


def test_zoom_pulse_scanline_does_not_force_gpu_fallback() -> None:
    assert (
        gpu_unsupported_features(
            _single_char_track(),
            _zoom_pulse_style(karaoke_anim="zoom_pulse_scanline"),
        )
        == ()
    )


def test_per_line_override_controls_zoom_pulse() -> None:
    style = _zoom_pulse_style()
    off = TimingLine(
        chars=[TimingChar("歌", 0)],
        end_ms=500,
        animation_override=LineAnimationOverride(karaoke_anim="none"),
    )
    assert not effective_karaoke_zoom_pulse(style_with_line_animation(style, off))
    keep = TimingLine(chars=[TimingChar("歌", 0)], end_ms=500)
    assert effective_karaoke_zoom_pulse(style_with_line_animation(style, keep))


def test_reverse_karaoke_zoom_pulse_bakes_per_line() -> None:
    style = _zoom_pulse_style(karaoke_anim="utopia", reverse_karaoke_anim="zoom_pulse")
    forward = TimingLine(chars=[TimingChar("歌", 0)], end_ms=500)
    reverse = TimingLine(chars=[TimingChar("歌", 0)], end_ms=500, wipe_reverse=True)
    assert not effective_karaoke_zoom_pulse(style_with_line_animation(style, forward))
    assert effective_karaoke_zoom_pulse(style_with_line_animation(style, reverse))


def test_zoom_pulse_style_round_trip() -> None:
    style = _zoom_pulse_style()
    restored = style_from_dict(style_to_dict(style))
    assert restored.karaoke_anim == "zoom_pulse"
    assert restored.reverse_karaoke_anim == "inherit"


def test_zoom_pulse_line_override_serialization_round_trip() -> None:
    override = LineAnimationOverride(karaoke_anim="zoom_pulse")
    data = line_animation_override_to_dict(override)
    assert data["karaoke_anim"] == "zoom_pulse"
    restored = line_animation_override_from_dict(
        {"entry_anim": "none", "exit_anim": "none", "karaoke_anim": "zoom_pulse"}
    )
    assert restored is not None and restored.karaoke_anim == "zoom_pulse"
    invalid = line_animation_override_from_dict(
        {"entry_anim": "none", "exit_anim": "none", "karaoke_anim": "wat"}
    )
    assert invalid is not None and invalid.karaoke_anim == "inherit"


def test_zoom_pulse_does_not_force_gpu_fallback() -> None:
    assert gpu_unsupported_features(_single_char_track(), _zoom_pulse_style()) == ()
    reverse = _zoom_pulse_style(
        karaoke_anim="utopia", reverse_karaoke_anim="zoom_pulse"
    )
    assert gpu_unsupported_features(_single_char_track(), reverse) == ()


def test_zoom_pulse_render_ir_marks_utopia_body_plus_flag() -> None:
    from krok_helper.subtitle_render.engine.render.render_ir import build_render_ir

    ir = build_render_ir(
        _single_char_track(), _zoom_pulse_style(), width=640, height=360, fps=60
    )
    line = ir["track"]["lines"][0]
    assert line["karaoke_anim"] == "utopia"
    assert line["zoom_pulse"] is True
    plain = build_render_ir(
        _single_char_track(),
        _zoom_pulse_style(karaoke_anim="utopia"),
        width=640,
        height=360,
        fps=60,
    )
    assert plain["track"]["lines"][0]["karaoke_anim"] == "utopia"
    assert plain["track"]["lines"][0]["zoom_pulse"] is False


# ---------------------------------------------------------------------------
# 缩放曲线
# ---------------------------------------------------------------------------


def test_zoom_pulse_scale_curve_boundaries() -> None:
    start, end = 0, 1000
    assert zoom_pulse_wipe_scale(start, start, end) == 1.0
    assert zoom_pulse_wipe_scale(end, start, end) == pytest.approx(
        ZOOM_PULSE_PEAK_RATIO
    )
    # 唱字结束后 150ms（尾窗一半）仍在缩小，未回到 1.0。
    assert zoom_pulse_wipe_scale(end + 150, start, end) > 1.1
    assert zoom_pulse_wipe_scale(end + ZOOM_PULSE_SHRINK_MS, start, end) == 1.0
    assert zoom_pulse_wipe_scale(end + ZOOM_PULSE_SHRINK_MS + 1, start, end) == 1.0


def test_zoom_pulse_active_window_covers_shrink_tail() -> None:
    start, end = 0, 1000
    assert not is_zoom_pulse_active(start, start, end)
    assert is_zoom_pulse_active(500, start, end)
    assert is_zoom_pulse_active(end, start, end)
    assert is_zoom_pulse_active(end + ZOOM_PULSE_SHRINK_MS - 1, start, end)
    assert not is_zoom_pulse_active(end + ZOOM_PULSE_SHRINK_MS, start, end)
    # 零时长字符恒不活跃（与 is_utopia_wiping 同口径）。
    assert not is_zoom_pulse_active(100, 100, 100)
    assert zoom_pulse_wipe_scale(100, 100, 100) == 1.0


def test_zoom_pulse_enlarge_phase_eases_out() -> None:
    start, end = 0, 1000
    mid = zoom_pulse_wipe_scale(500, start, end, 3)
    # 三次缓出：中点已越过线性中点（1.125）逼近峰值。
    assert mid == pytest.approx(1.21875)
    assert mid > 1.125
    # 3/4 处几乎贴住峰值（停留感）。
    assert zoom_pulse_wipe_scale(750, start, end, 3) > 1.24


def test_zoom_pulse_shrink_phase_eases_in() -> None:
    start, end = 0, 1000
    # 缩回段前 1/3：仍贴近峰值（远高于线性位置 1.1667）。
    assert zoom_pulse_wipe_scale(end + 100, start, end, 3) == pytest.approx(
        1.0 + 0.25 * (1.0 - (100.0 / 300.0) ** 3), rel=1e-6
    )
    assert zoom_pulse_wipe_scale(end + 100, start, end, 3) > 1.23
    # 尾段快速落回（q=5/6 时三次缓入只剩约 10% 的放大量）。
    assert zoom_pulse_wipe_scale(end + 250, start, end, 3) < 1.11


def test_zoom_pulse_peak_is_c1_smooth() -> None:
    start, end = 0, 1000
    before = zoom_pulse_wipe_scale(end - 1, start, end, 3)
    after = zoom_pulse_wipe_scale(end, start, end, 3)
    # 峰值两侧导数均为 0：单帧步进远小于峰值的 0.5%。
    assert abs(after - before) < ZOOM_PULSE_PEAK_RATIO * 0.005


def test_zoom_pulse_short_char_reaches_peak_then_shrinks() -> None:
    start, end = 0, 50
    assert zoom_pulse_wipe_scale(end, start, end, 3) == pytest.approx(
        ZOOM_PULSE_PEAK_RATIO
    )
    assert zoom_pulse_wipe_scale(end + 50, start, end, 3) > 1.2
    assert zoom_pulse_wipe_scale(end + ZOOM_PULSE_SHRINK_MS, start, end, 3) == 1.0


# ---------------------------------------------------------------------------
# 缓动档位（zoom_pulse_curve_level）
# ---------------------------------------------------------------------------


def test_zoom_pulse_curve_level_zero_is_linear() -> None:
    start, end = 0, 1000
    # 0 档：放大/缩小均为线性插值。
    assert zoom_pulse_wipe_scale(500, start, end, 0) == pytest.approx(1.125)
    assert zoom_pulse_wipe_scale(250, start, end, 0) == pytest.approx(1.0625)
    assert zoom_pulse_wipe_scale(1100, start, end, 0) == pytest.approx(
        1.0 + 0.25 * (1.0 - 100.0 / 300.0)
    )


def test_zoom_pulse_curve_level_monotonic_stay_at_peak() -> None:
    start, end = 0, 1000
    mids = [zoom_pulse_wipe_scale(500, start, end, level) for level in range(2, 6)]
    # 阶数越大，中点越贴近峰值（停留感越强），单调递增。
    assert mids == sorted(mids)
    assert mids[0] < mids[-1]
    assert mids[-1] > 1.24
    # 5 档尾窗 1/3 处几乎贴住峰值。
    assert zoom_pulse_wipe_scale(1100, start, end, 5) > 1.248


def test_zoom_pulse_curve_level_defaults_and_clamps() -> None:
    assert zoom_pulse_curve_level(Style(karaoke_anim="zoom_pulse")) == 1
    assert zoom_pulse_curve_level(Style(zoom_pulse_curve_level=99)) == 5
    assert zoom_pulse_curve_level(Style(zoom_pulse_curve_level=-2)) == 0
    # 曲线函数自身对越界档位也做钳制。
    assert zoom_pulse_wipe_scale(500, 0, 1000, 99) == pytest.approx(
        zoom_pulse_wipe_scale(500, 0, 1000, 5)
    )


def test_zoom_pulse_curve_level_round_trip_and_ir() -> None:
    from krok_helper.subtitle_render.engine.render.render_ir import build_render_ir

    style = _zoom_pulse_style(zoom_pulse_curve_level=5)
    restored = style_from_dict(style_to_dict(style))
    assert restored.zoom_pulse_curve_level == 5
    ir = build_render_ir(
        _single_char_track(), style, width=640, height=360, fps=60
    )
    assert ir["style"]["zoom_pulse_curve_level"] == 5


def test_transition_char_state_obeys_curve_level() -> None:
    style = _zoom_pulse_style(zoom_pulse_curve_level=0)
    transition = _utopia_transition()
    _, _, _, _, scale_x, _, _ = transition_char_state(
        style,
        transition,
        0,
        1,
        char_start_ms=0,
        char_end_ms=1000,
        t_ms=500,
    )
    assert scale_x == pytest.approx(1.125)


# ---------------------------------------------------------------------------
# transition_char_state 集成
# ---------------------------------------------------------------------------


def test_transition_char_state_uses_zoom_pulse_curve() -> None:
    style = _zoom_pulse_style(zoom_pulse_curve_level=3)
    transition = _utopia_transition()
    opacity, dx, dy, rotation, scale_x, scale_y, skew_y = transition_char_state(
        style,
        transition,
        0,
        1,
        char_start_ms=0,
        char_end_ms=1000,
        t_ms=500,
    )
    assert opacity == 1.0
    assert (dx, dy, rotation, skew_y) == (0.0, 0.0, 0.0, 0.0)
    assert scale_x == pytest.approx(1.21875)
    assert scale_y == pytest.approx(1.21875)


def test_zoom_pulse_shrink_tail_survives_past_char_end() -> None:
    style = _zoom_pulse_style(zoom_pulse_curve_level=3)
    transition = _utopia_transition()
    # t > char_end：is_utopia_wiping 已为 False，但整字放大尾窗仍返回缩放。
    _, _, _, _, scale_x, _, _ = transition_char_state(
        style,
        transition,
        0,
        1,
        char_start_ms=0,
        char_end_ms=1000,
        t_ms=1100,
    )
    assert scale_x == pytest.approx(1.0 + 0.25 * (1.0 - (100.0 / 300.0) ** 3), rel=1e-6)
    # 尾窗结束后回到恒等。
    _, _, _, _, scale_x_done, _, _ = transition_char_state(
        style,
        transition,
        0,
        1,
        char_start_ms=0,
        char_end_ms=1000,
        t_ms=1400,
    )
    assert scale_x_done == 1.0


def test_utopia_wipe_unchanged_by_zoom_pulse_flag() -> None:
    utopia = _zoom_pulse_style(karaoke_anim="utopia")
    transition = _utopia_transition()
    # utopia 档：唱完后立即回到恒等（原有脉冲语义不变）。
    _, _, _, _, scale_x, _, _ = transition_char_state(
        utopia,
        transition,
        0,
        1,
        char_start_ms=0,
        char_end_ms=1000,
        t_ms=1100,
    )
    assert scale_x == 1.0


def test_utopia_exit_composes_with_zoom_pulse_shrink_tail() -> None:
    style = _zoom_pulse_style(exit_anim="utopia")
    transition = _utopia_transition()
    # following_done_ms 与行尾相同：退场从峰值这一帧开始，整字放大的缩回
    # 同时继续叠乘在退场缩放上，交接帧连续（没有峰值→原大的跳变）。
    peak = transition_char_state(
        style,
        transition,
        0,
        1,
        char_start_ms=0,
        char_end_ms=1000,
        t_ms=1000,
        following_done_ms=1000,
    )
    first_exit = transition_char_state(
        style,
        transition,
        0,
        1,
        char_start_ms=0,
        char_end_ms=1000,
        t_ms=1001,
        following_done_ms=1000,
    )
    assert peak[4] == pytest.approx(ZOOM_PULSE_PEAK_RATIO)
    assert first_exit[3] < 0.0
    assert abs(first_exit[4] - peak[4]) < 0.01
    # 退场中段：旋转/淡出与缩回同时在走（缩放 = 退场 shrink × 缩回系数）。
    opacity, dx, dy, rotation, scale_x, scale_y, _ = transition_char_state(
        style,
        transition,
        0,
        1,
        char_start_ms=0,
        char_end_ms=1000,
        t_ms=1100,
        following_done_ms=1000,
    )
    assert rotation < 0.0
    assert opacity < 1.0
    assert 1.0 < scale_y < 1.05
    # 缩回结束后只剩退场自身的缩水。
    _, _, _, rotation_after, scale_after, _, _ = transition_char_state(
        style,
        transition,
        0,
        1,
        char_start_ms=0,
        char_end_ms=1000,
        t_ms=1500,
        following_done_ms=1000,
    )
    assert rotation_after < 0.0
    assert scale_after < 0.5


def test_char_transition_exit_starts_on_time_and_composes_zoom() -> None:
    from krok_helper.subtitle_render.engine.render.elements.horizontal.transitions import (
        line_char_transition_context,
    )

    line = TimingLine(
        chars=[TimingChar("あ", 0), TimingChar("い", 500)],
        end_ms=1000,
    )
    # 退场窗从行尾开始：退场仍按原时间启动（不延后），但整字放大的缩放
    # 继续合成进退场状态里，最后一个字不会在峰值处跳回原大。
    style = _zoom_pulse_style(
        exit_anim="char_fade",
        exit_fade_ms=250,
        line_tail_ms=500,
    )
    at_end = line_char_transition_context(
        style, line, 1100, 0, 1500, 2
    )
    assert at_end is not None and at_end.phase == "exit"
    _, _, _, _, scale_x, scale_y, _ = transition_char_state(
        style,
        at_end,
        1,
        2,
        char_start_ms=500,
        char_end_ms=1000,
        t_ms=1100,
    )
    assert scale_x > 1.1
    assert scale_y > 1.1


def test_character_scale_origin_switches_to_center_for_zoom_pulse() -> None:
    zoom = _zoom_pulse_style()
    utopia = _zoom_pulse_style(karaoke_anim="utopia")
    assert character_scale_origin(zoom, 10.0, 200.0) == (None, None)
    assert character_scale_origin(utopia, 10.0, 200.0) == (10.0, 200.0)


# ---------------------------------------------------------------------------
# Painter 像素
# ---------------------------------------------------------------------------


def _alpha_bounds(track: TimingTrack, style: Style, t_ms: int):
    image = QImage(800, 450, QImage.Format.Format_RGBA8888)
    image.fill(0)
    paint_frame(image, track, t_ms, style)
    pixels = np.frombuffer(
        image.constBits().asstring(image.sizeInBytes()), dtype=np.uint8
    ).reshape(450, 800, 4)
    alpha = pixels[:, :, 3]
    rows = np.flatnonzero(alpha.max(axis=1) > 8)
    cols = np.flatnonzero(alpha.max(axis=0) > 8)
    assert rows.size and cols.size, f"no visible glyph at t={t_ms}"
    return (
        float(cols[0]),
        float(cols[-1]),
        float(rows[0]),
        float(rows[-1]),
    )


def test_painter_zoom_pulse_enlarges_glyph_and_keeps_center(qapp) -> None:
    track = _single_char_track()
    style = _zoom_pulse_style()
    static = _alpha_bounds(track, style, 0)
    peak = _alpha_bounds(track, style, 1000)
    # 峰值帧包围盒明显更大。
    assert (peak[1] - peak[0]) > (static[1] - static[0]) * 1.1
    assert (peak[3] - peak[2]) > (static[3] - static[2]) * 1.1
    # 字符中心原点：包围盒中心基本不动（两侧对称生长）。
    static_center_x = (static[0] + static[1]) / 2
    peak_center_x = (peak[0] + peak[1]) / 2
    assert abs(peak_center_x - static_center_x) <= 4.0


def test_painter_zoom_pulse_shrink_tail_still_enlarged(qapp) -> None:
    track = _single_char_track()
    # 行显示窗 = 行尾 + line_tail_ms；加大尾巴让 1300ms（尾窗刚结束）仍在窗内。
    style = _zoom_pulse_style(line_tail_ms=600)
    static = _alpha_bounds(track, style, 0)
    tail = _alpha_bounds(track, style, 1100)
    assert (tail[1] - tail[0]) > (static[1] - static[0]) * 1.08
    settled = _alpha_bounds(track, style, 1300)
    # 尾窗结束后回到原始包围盒宽度（容差给抗锯齿）。
    assert abs((settled[1] - settled[0]) - (static[1] - static[0])) <= 6.0


def test_painter_zoom_pulse_differs_from_utopia_growth_direction(qapp) -> None:
    track = _single_char_track()
    zoom = _zoom_pulse_style()
    utopia = _zoom_pulse_style(karaoke_anim="utopia")
    zoom_bounds = _alpha_bounds(track, zoom, 1000)
    utopia_bounds = _alpha_bounds(track, utopia, 500)
    # 峰值更高（1.25 vs utopia 走字脉冲 1.15），且 utopia 从左缘向右上生长。
    assert (zoom_bounds[1] - zoom_bounds[0]) > (utopia_bounds[1] - utopia_bounds[0])


def test_painter_zoom_pulse_composes_with_char_transition_exit(qapp) -> None:
    track = _single_char_track()
    # 退场从行尾开始（行尾显示窗 500ms）：退场照常播，但整字放大的缩回
    # 仍叠加在 char_fade 退场状态上，最后一个字不会跳回静态原大。
    style = _zoom_pulse_style(
        exit_anim="char_fade",
        exit_fade_ms=250,
        line_tail_ms=500,
    )
    static = _alpha_bounds(track, style, 0)
    during_tail = _alpha_bounds(track, style, 1100)
    assert (during_tail[1] - during_tail[0]) > (static[1] - static[0]) * 1.05


def _frame_bytes(track: TimingTrack, style: Style, t_ms: int) -> bytes:
    image = QImage(800, 450, QImage.Format.Format_RGBA8888)
    image.fill(0)
    paint_frame(image, track, t_ms, style)
    bits = image.constBits()
    bits.setsize(image.sizeInBytes())
    return bytes(bits)


def _visible_diff(left: bytes, right: bytes) -> int:
    a = np.frombuffer(left, dtype=np.uint8).reshape(-1, 4).astype(np.int16)
    b = np.frombuffer(right, dtype=np.uint8).reshape(-1, 4).astype(np.int16)
    return int(np.count_nonzero(np.abs(a - b).max(axis=1) > 2))


def test_painter_zoom_pulse_scanline_highlights_the_moving_front(qapp) -> None:
    track = _single_char_track()
    base = _zoom_pulse_style()
    combo = _zoom_pulse_style(karaoke_anim="zoom_pulse_scanline")
    # 唱字进行中：整字放大保持不变，锋面高亮带叠加出可见差异。
    assert _visible_diff(_frame_bytes(track, base, 500), _frame_bytes(track, combo, 500)) > 20
    # 未开始（t=0）：无锋面无差异，两档画面一致。
    assert _visible_diff(_frame_bytes(track, base, 0), _frame_bytes(track, combo, 0)) == 0
