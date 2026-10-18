"""2026-09 逐字几何特效 / 描边闪光 / 装饰粒子的核心数学与 IR 契约测试。

镜像约束：transitions._geo_char_state ↔ d2d_fx.geoCharState、
particles.burst_particles_at ↔ d2d_fx.burstParticlesAt——这里断言的是
Python 侧不变量（确定性、窗口、单调性、哈希域），跨后端逐位一致由
两侧共用同一整数哈希与纯函数轨迹保证。
"""

from __future__ import annotations

import math

from dataclasses import replace

import pytest

from krok_helper.subtitle_render.domain.models import Style, TimingLine
from krok_helper.subtitle_render.domain.timing import LineAnimationOverride
from krok_helper.subtitle_render.engine.render.elements.horizontal.transitions import (
    _geo_char_state,
    fx_unit_hash,
    line_char_transition_context,
    transition_char_state,
)
from krok_helper.subtitle_render.engine.render.elements.horizontal.contracts import (
    LineCharTransition,
)
from krok_helper.subtitle_render.engine.render.effects.particles import (
    FX_SPRITES,
    burst_particles_at,
    exit_particle_tail_end_ms,
    plan_line_bursts,
    sprite_for_kind,
)
from krok_helper.subtitle_render.native.protocol import gpu_unsupported_features
from krok_helper.subtitle_render.serialization.timing import (
    line_animation_override_from_dict,
    line_animation_override_to_dict,
)


def _entry_transition(effect: str, start_ms: int = 0) -> LineCharTransition:
    return LineCharTransition(
        phase="entry", effect=effect, progress=1.0, start_ms=start_ms, end_ms=start_ms + 800
    )


def _exit_transition(effect: str, start_ms: int) -> LineCharTransition:
    return LineCharTransition(
        phase="exit", effect=effect, progress=1.0, start_ms=start_ms, end_ms=start_ms + 700
    )


def test_fx_unit_hash_is_deterministic_and_bounded():
    for index in range(0, 256, 7):
        for salt in range(1, 5):
            value = fx_unit_hash(index, salt)
            assert 0.0 <= value < 1.0
            assert value == fx_unit_hash(index, salt)
    assert fx_unit_hash(0, 1) != fx_unit_hash(1, 1)


def test_tracking_in_converges_from_outward_offsets():
    style = Style(font_size_px=100)
    count = 5
    early = _geo_char_state(
        style, _entry_transition("tracking_in"), 0, count,
        t_ms=0, char_center_x=None, line_center_x=None,
    )
    settled = _geo_char_state(
        style, _entry_transition("tracking_in"), 0, count,
        t_ms=10_000, char_center_x=None, line_center_x=None,
    )
    # 首字符位于最左：初始 dx 为负（向左展开），归位后为 0。
    assert early[1] < 0.0
    assert early[0] < 1.0
    assert settled[1] == 0.0
    assert settled[0] == 1.0
    # 对称：首尾字符初始偏移互为相反数。
    last = _geo_char_state(
        style, _entry_transition("tracking_in"), count - 1, count,
        t_ms=0, char_center_x=None, line_center_x=None,
    )
    assert last[1] == pytest.approx(-early[1])


def test_wave_in_rises_with_stagger():
    style = Style(font_size_px=100)
    state = _geo_char_state(
        style, _entry_transition("wave_in"), 2, 5,
        t_ms=0, char_center_x=None, line_center_x=None,
    )
    assert state[2] > 24.0
    done = _geo_char_state(
        style, _entry_transition("wave_in"), 2, 5,
        t_ms=100_000, char_center_x=None, line_center_x=None,
    )
    assert done[2] == 0.0
    assert done[0] == 1.0


def test_geo_entry_motion_visible_while_opaque():
    """回归护栏：入场行程前 1/4 字符已全不透明且位移仍有 8 成以上。

    旧实现透明度与运动共用缓动（快衰减），字符变清晰时位移只剩 ~12%，
    观感退化为普通逐字淡入（用户判「完全没有动画」）。
    """
    style = Style(font_size_px=100, entry_lead_ms=600)
    tracking = _geo_char_state(
        style, _entry_transition("tracking_in", 0), 0, 5,
        t_ms=65, char_center_x=None, line_center_x=None,
    )
    spread = 100 * 1.6
    assert tracking[0] == 1.0
    assert tracking[1] < -0.8 * spread

    wave = _geo_char_state(
        style, _entry_transition("wave_in", 0), 0, 5,
        t_ms=65, char_center_x=None, line_center_x=None,
    )  # 同 style 共用 entry_lead_ms=600
    assert wave[0] == 1.0
    assert wave[2] > 0.8 * max(100 * 0.5, 24.0)


def test_scatter_out_moves_fades_and_rotates():
    style = Style(font_size_px=100)
    start = _geo_char_state(
        style, _exit_transition("scatter_out", 2000), 3, 5,
        t_ms=2000, char_center_x=None, line_center_x=None,
    )
    end = _geo_char_state(
        style, _exit_transition("scatter_out", 2000), 3, 5,
        t_ms=100_000, char_center_x=None, line_center_x=None,
    )
    assert start[0] == 1.0
    assert end[0] == 0.0
    # 至少有一个分量发生位移（随机方向不保证单一轴向）。
    assert abs(end[1]) + abs(end[2]) > 100.0
    assert end[4] < 1.0


def test_converge_out_pulls_toward_line_center():
    style = Style(font_size_px=100)
    state = _geo_char_state(
        style, _exit_transition("converge_out", 3000), 0, 3,
        t_ms=3100, char_center_x=100.0, line_center_x=500.0,
    )
    # 字符在行中心左侧：dx 为正（向中心靠拢）。
    assert state[1] > 0.0
    assert state[1] <= 400.0
    assert 0.0 < state[0] < 1.0


def test_geo_context_windows_and_gating():
    style = Style(
        font_size_px=100,
        entry_anim="wave_in",
        entry_lead_ms=300,
        exit_anim="scatter_out",
        exit_fade_ms=300,
    )
    line = TimingLine()
    entry = line_char_transition_context(style, line, 100, 0, 6000, 3)
    assert entry is not None and entry.effect == "wave_in" and entry.phase == "entry"

    after_entry = line_char_transition_context(style, line, 900, 0, 6000, 3)
    # 入场窗口结束后不再命中入场相位（默认 karaoke_anim=utopia 会接管为
    # 稳态 wipe 路径，这是既有语义而非新特效窗口泄漏）。
    assert after_entry is None or after_entry.phase != "entry"

    exit_ctx = line_char_transition_context(style, line, 5800, 0, 6000, 3)
    assert exit_ctx is not None and exit_ctx.effect == "scatter_out"

    # 旧语义：时长 0 = 无动画（与 char_fade / 淡入滑入同口径）。
    zero_lead = Style(
        entry_anim="wave_in",
        entry_lead_ms=0,
        exit_anim="scatter_out",
        exit_fade_ms=0,
        karaoke_anim="none",
    )
    assert line_char_transition_context(zero_lead, line, 100, 0, 6000, 3) is None
    assert line_char_transition_context(zero_lead, line, 5800, 0, 6000, 3) is None
    # 时长驱动窗口：300ms 时长 → t=350 已过入场窗。
    short = Style(
        entry_anim="wave_in", entry_lead_ms=300, karaoke_anim="none"
    )
    after_short = line_char_transition_context(short, line, 350, 0, 6000, 3)
    assert after_short is None or after_short.phase != "entry"
    # 显式 none 才是不动画。
    disabled = Style(entry_anim="none", karaoke_anim="none")
    assert line_char_transition_context(disabled, line, 0, 0, 6000, 3) is None


def test_transition_char_state_dispatches_geo_kinds():
    style = Style(font_size_px=64)
    transition = _entry_transition("tracking_in")
    opacity, dx, _dy, _rot, sx, sy, skew = transition_char_state(
        style, transition, 0, 4, t_ms=0
    )
    assert opacity < 1.0
    assert dx < 0.0
    assert (sx, sy, skew) == (1.0, 1.0, 0.0)


def test_plan_line_bursts_kinds_and_windows():
    """出入场动画粒子（固定默认档）+ 唱字粒子（旋钮档）的规划契约。"""
    style = Style(
        entry_anim="sparkle",
        entry_lead_ms=600,
        exit_anim="ripple",
        exit_fade_ms=600,
        sing_fx="twinkle",
        fx_particle_size_em=0.5,
        fx_particle_count=10,
        font_size_px=100,
        karaoke_anim="none",
    )
    bursts = plan_line_bursts(
        style,
        line_index=2,
        display_start_ms=1000,
        display_end_ms=4000,
        line_end_ms=3900,
        char_windows=[(1200, 1600), (1600, 1600), (1600, 3000)],
    )
    kinds = [burst["kind"] for burst in bursts]
    # 入场动画：星光逐字符（2026-10 结构统一，3 字 × 双色拆分 = 6 条
    # burst）；整行数量预算 14 颗均匀分到 3 字 = 4+6+4（双色各拆 2+2 /
    # 3+3 / 2+2），左→右逐字错峰 = SPARKLE_SWEEP_MS/(n-1) = 175ms，窗口
    # 终点与旧行锚点档同一条（显示窗起点 + 700ms）。
    assert kinds.count("sparkle") == 6
    sparkle = [b for b in bursts if b["kind"] == "sparkle"]
    assert all(b["anchor"] == "char" for b in sparkle)
    assert [b["char_index"] for b in sparkle] == [0, 0, 1, 1, 2, 2]
    assert sum(b["count"] for b in sparkle) == 14
    assert sorted(b["count"] for b in sparkle) == [2, 2, 2, 2, 3, 3]
    first_start = {}
    for burst in sparkle:
        first_start.setdefault(burst["char_index"], burst["start_ms"])
        assert burst["end_ms"] == 1700
        assert burst["sweep"] == 0
    assert first_start == {0: 971, 1: 1179, 2: 1286}  # 20 数偏移表
    assert {b["color"] for b in sparkle} == {"#FFB7C5", "#FFD7E0"}
    assert sparkle[0]["size_px"] == pytest.approx(100 * 0.40)
    # 退场动画涟漪（2026-10 分离口径）：标准错峰（步距 175）+ 每环完整
    # 寿命 600ms；锚点 = max(行末 3900, 显示末 4000 - 950) = 3900，
    # 末环越过显示窗终点（渲染端尾奏通道续播）。
    assert kinds.count("ripple") == 6
    exit_ripples = [b for b in bursts if b["kind"] == "ripple"]
    assert {b["start_ms"] for b in exit_ripples} == {3871, 4079, 4186}
    assert all(b["end_ms"] == b["start_ms"] + 600 for b in exit_ripples)
    assert [b["char_index"] for b in exit_ripples] == [0, 0, 1, 1, 2, 2]
    assert sorted({b["count"] for b in exit_ripples}) == [1, 2]
    assert all(b["anchor"] == "char" for b in exit_ripples)
    assert {b["color"] for b in exit_ripples} == {"#FFB7C5", "#FFD7E0"}
    # 唱字（旋钮档）：零时长字符不发射；星光并入出入场运动学后每字数量
    # = max(5, count//2)（默认 7，2026-10 密度对齐出入场观感）。
    assert kinds.count("twinkle") == 2
    twinkle = next(b for b in bursts if b["kind"] == "twinkle")
    assert twinkle["count"] == 5
    assert twinkle["anchor"] == "char"
    assert twinkle["end_ms"] == twinkle["start_ms"] + 700
    assert twinkle["size_px"] == pytest.approx(50.0)  # 100px * 0.5em 旋钮
    # 行下标参与种子：不同行不同轨迹；同参数可复现。
    other = plan_line_bursts(
        style, 3, 1000, 4000, 3900, [(1200, 1600), (1600, 1600), (1600, 3000)]
    )
    assert other[0]["seed"] != sparkle[0]["seed"]
    again = plan_line_bursts(
        style, 2, 1000, 4000, 3900, [(1200, 1600), (1600, 1600), (1600, 3000)]
    )
    assert again[0]["seed"] == sparkle[0]["seed"]


def test_entry_exit_sparkle_per_char_roles_budget_and_sweep():
    """入退场星光逐字符结构（2026-10 统一）：外观走完整解析——多角色行
    每字跟自己的方案（图片、follow_mix 前后各一均生效）；整行 14 颗数量
    预算按可见字符分配；入场左→右、退场右→左错峰；空格不参与；不再输
    出 ``char_colors`` 专用颜色表。"""
    from krok_helper.subtitle_render.domain.models import SubtitleStyleScheme
    from krok_helper.subtitle_render.domain.paint import (
        KaraokeColorState,
        KaraokeColors,
        PaintFill,
    )
    from krok_helper.subtitle_render.engine.style.style_semantics import style_for_role

    scheme_a = SubtitleStyleScheme(
        karaoke_colors=KaraokeColors(
            before=KaraokeColorState(
                text=PaintFill(mode="image", image_path="a.png")
            ),
            after=KaraokeColorState(
                text=PaintFill(mode="image", image_path="a.png")
            ),
        )
    )
    scheme_b = SubtitleStyleScheme(
        karaoke_colors=KaraokeColors(
            before=KaraokeColorState(
                text=PaintFill(mode="image", image_path="b.png")
            ),
            after=KaraokeColorState(
                text=PaintFill(mode="image", image_path="b.png")
            ),
        )
    )
    style = Style(
        font_size_px=100,
        entry_anim="sparkle",
        entry_lead_ms=600,
        exit_anim="sparkle",
        exit_fade_ms=600,
        fx_apply_to_entry_exit=True,
        fx_particle_color_mode="follow_after",
        fx_particle_color_layers="decor",
        karaoke_anim="none",
        custom_style_schemes={"主唱": scheme_a, "和声": scheme_b},
        karaoke_colors=_karaoke_matrix("#111111", "#222222"),
    )
    # 4 字符，字 1 是空格（char_visible=False 不参与）；A/B/A 角色分布。
    char_styles = [
        style_for_role(style, "主唱"),
        style_for_role(style, "主唱"),
        style_for_role(style, "和声"),
        style_for_role(style, "主唱"),
    ]
    visible = [True, False, True, True]
    windows = [(1200, 1600), (1600, 1600), (1600, 2000), (2000, 2400)]
    bursts = plan_line_bursts(
        style,
        0,
        1000,
        4000,
        3900,
        windows,
        char_visible=visible,
        char_styles=char_styles,
    )
    assert all("char_colors" not in b for b in bursts)
    entry = [b for b in bursts if b["kind"] == "sparkle" and b["start_ms"] < 1700]
    # 预算 14 颗均匀分 3 槽 = 4+6+4（粒子major 网格）；单态档每字一条 burst。
    assert [b["char_index"] for b in entry] == [0, 2, 3]
    assert [b["count"] for b in entry] == [4, 6, 4]
    # 每字跟自己的角色：字 0/3 = 主唱 a.png，字 2 = 和声 b.png。
    assert entry[0]["paint"]["fill"]["mode"] == "image"
    assert entry[0]["paint"]["fill"]["image_path"] == "a.png"
    assert entry[1]["paint"]["fill"]["image_path"] == "b.png"
    assert entry[2]["paint"]["fill"]["image_path"] == "a.png"
    # 入场左→右：字 0 → 2 → 3，错峰 = 350/(3-1) = 175ms，终点同 1700。
    assert [b["start_ms"] for b in entry] == [971, 1111, 1387]
    assert all(b["end_ms"] == 1700 for b in entry)
    # 退场右→左（2026-10 分离口径）：120ms 退场旋钮不再压缩粒子——
    # exit_start = max(3900, 4000-600) = 3900，标准错峰 175ms、每字
    # 完整寿命 600ms，末字越过显示窗终点完整播放（旧「窗预算钳制/120
    # 兜底」已随分离口径废除——旋钮只控制文字渐隐）。
    exit_sparkle = sorted(
        (
            b
            for b in bursts
            if b["kind"] == "sparkle" and b["start_ms"] > 3000
        ),
        key=lambda b: b["start_ms"],
    )
    assert {b["char_index"] for b in exit_sparkle} == {0, 2, 3}
    assert {b["start_ms"] for b in exit_sparkle} == {3937, 4011, 4221}
    assert all(b["end_ms"] == b["start_ms"] + 600 for b in exit_sparkle)

    # follow_mix（前后各一）：多角色行星光同样逐字跟各自角色（此前
    # 行锚点档取首字符方案覆盖整行）。
    mix_style = replace(
        style, fx_particle_color_mode="follow_mix"
    )
    mix_char_styles = [
        style_for_role(mix_style, "主唱"),
        style_for_role(mix_style, "和声"),
    ]
    mix_bursts = plan_line_bursts(
        mix_style,
        0,
        1000,
        4000,
        3900,
        [(1200, 1600), (1600, 2000)],
        char_visible=[True, True],
        char_styles=mix_char_styles,
    )
    mix_entry = [
        b for b in mix_bursts
        if b["kind"] == "sparkle" and b["start_ms"] < 1700
    ]
    # 每字拆「走字前/后」两条变体；字 0 全部 a.png，字 1 全部 b.png。
    by_char = {0: [], 1: []}
    for burst in mix_entry:
        by_char[burst["char_index"]].append(
            burst["paint"]["fill"]["image_path"]
        )
    assert set(by_char[0]) == {"a.png"}
    assert set(by_char[1]) == {"b.png"}


def test_entry_sparkle_count_budget_on_long_line():
    """整行 14 颗数量预算：长行按可见字符**均匀**分配（每字 0~1 颗，
    部分字符分不到，但行首/行尾必有粒子——不再集中给前几个字符），
    单字行独占全部预算；空格不占预算。"""
    style = Style(
        font_size_px=100,
        entry_anim="sparkle",
        entry_lead_ms=600,
        karaoke_anim="none",
    )
    windows = [(1000, 1100)] * 20
    bursts = plan_line_bursts(
        style,
        0,
        1000,
        3000,
        2900,
        windows,
        char_visible=[True] * 9 + [False] + [True] * 10,
    )
    sparkle = [b for b in bursts if b["kind"] == "sparkle"]
    assert sum(b["count"] for b in sparkle) == 14
    assert all(b["char_index"] != 9 for b in sparkle)  # 空格不参与
    assert max(b["count"] for b in sparkle) <= 1
    assert len(sparkle) == 14  # 19 槽 14 颗：14 个槽各 1 颗（每字单 burst）
    # 粒子major 均匀网格：两端字符必有粒子（回归：曾只分给前 14 个字）。
    char_indexes = {b["char_index"] for b in sparkle}
    assert 0 in char_indexes and 19 in char_indexes
    assert min(char_indexes) == 0
    assert max(char_indexes) == 19

    single = plan_line_bursts(
        style, 0, 1000, 3000, 2900, [(1000, 1600)], char_visible=[True]
    )
    single_sparkle = [b for b in single if b["kind"] == "sparkle"]
    assert len(single_sparkle) == 2  # 双色拆 7+7
    assert sum(b["count"] for b in single_sparkle) == 14


def test_entry_sparkle_single_particle_keeps_both_variants():
    """回归（单颗预算丢弃第二变体）：预算分到每字 1 颗时长行，两种
    默认颜色都要出现——按字符序轮换变体，而不是固定只发第一种。"""
    style = Style(
        font_size_px=100,
        entry_anim="sparkle",
        entry_lead_ms=600,
        karaoke_anim="none",
    )
    bursts = plan_line_bursts(
        style,
        0,
        1000,
        3000,
        2900,
        [(1000, 1100)] * 20,
        char_visible=[True] * 20,
    )
    sparkle = [b for b in bursts if b["kind"] == "sparkle"]
    assert all(b["count"] == 1 for b in sparkle)
    # 双色档：跨字符必须两种颜色都在（曾整行只剩第一种）。
    assert {b["color"] for b in sparkle} == {"#FFB7C5", "#FFD7E0"}


def test_short_exit_sparkle_every_char_visible():
    """回归（短退场粒子无法出生）：三字行 120ms 退场窗口——错峰必须为
    出生延迟与最短可见寿命让位，且求值器把随机出生延迟钳到窗口内，
    每个字符的粒子在整个窗口内逐毫秒至少有一帧可见。"""
    style = Style(
        font_size_px=100,
        entry_anim="none",
        exit_anim="sparkle",
        exit_fade_ms=120,
        karaoke_anim="none",
    )
    bursts = plan_line_bursts(
        style,
        0,
        1000,
        4000,
        3900,
        [(1200, 1600), (1600, 2000), (2000, 2400)],
        char_visible=[True] * 3,
    )
    sparkle = [b for b in bursts if b["kind"] == "sparkle"]
    assert sparkle
    for burst in sparkle:
        span = burst["end_ms"] - burst["start_ms"]
        # 求值期：随机出生延迟被钳到「寿命 - 最短可见寿命」，钳制上限
        # 之后必须已可见、并一直可见到窗口末尾（回归：3998ms 启动的字符
        # 曾因 130ms 延迟全程不可见）。
        clamped_delay = min(130.0, max(float(span) - 40.0, 0.0))
        first_visible = next(
            t_ms
            for t_ms in range(burst["start_ms"], burst["end_ms"] + 1)
            if burst_particles_at(burst, t_ms, 500.0, 300.0, 120.0, 100.0)
        )
        assert first_visible <= burst["start_ms"] + clamped_delay + 1
        assert burst_particles_at(
            burst, burst["end_ms"] - 1, 500.0, 300.0, 120.0, 100.0
        )


def test_plan_line_bursts_twinkle_classic_keeps_legacy_shape():
    """旧版唱字星光档：颗数/画层/无扫过保持 2026-10 运动学改造前口径。"""
    style = Style(
        sing_fx="twinkle_classic",
        fx_particle_size_em=0.5,
        fx_particle_count=10,
        font_size_px=100,
        karaoke_anim="none",
    )
    bursts = plan_line_bursts(
        style, 2, 1000, 4000, 3900, [(1200, 1600), (1600, 1600), (1600, 3000)]
    )
    # 零时长字符不发射，与新版档同口径。
    assert [b["kind"] for b in bursts] == ["twinkle_classic", "twinkle_classic"]
    classic = bursts[0]
    # 旧版密度口径 max(3, count//4)（新版 max(5, count//2)）。
    assert classic["count"] == max(3, 10 // 4)
    assert classic["anchor"] == "char"
    assert classic["char_index"] == 0
    assert classic["start_ms"] == 1200
    assert classic["end_ms"] == 1200 + 700
    assert classic["front"] is True
    assert classic["sweep"] == 0
    assert classic["size_px"] == pytest.approx(50.0)
    assert sprite_for_kind("twinkle_classic") == "star4"
    # 新版档不受影响：保持扫过运动学与新密度口径。
    modern = plan_line_bursts(
        Style(
            sing_fx="twinkle",
            fx_particle_size_em=0.5,
            fx_particle_count=10,
            font_size_px=100,
            karaoke_anim="none",
        ),
        2, 1000, 4000, 3900, [(1200, 1600), (1600, 1600), (1600, 3000)],
    )
    assert modern[0]["kind"] == "twinkle"
    assert modern[0]["sweep"] == 1
    assert modern[0]["count"] == 5


def test_twinkle_classic_in_place_sin_envelope():
    """旧版求值器：原地闪烁——位置/旋转恒定，sin 包络放大-熄灭。

    每颗寿命 300–450ms、出生延迟 u3×0.8×(700−寿命)；用哈希直接算出
    12 颗的公共存活窗，在窗内取两个时刻逐颗对位（全存活 → 顺序对位安全）。
    """
    burst = {
        "kind": "twinkle_classic", "anchor": "char", "char_index": 0,
        "start_ms": 0, "end_ms": 700, "count": 12, "seed": 1234,
        "size_px": 40.0, "travel_px": 72.0, "front": True, "sweep": 0,
    }
    delays, ends = [], []
    for i in range(12):
        u3 = fx_unit_hash(1234 + i, 3)
        u4 = fx_unit_hash(1234 + i, 4)
        life_i = 300.0 + 150.0 * u4
        spread = max(700.0 - life_i, 0.0)
        delays.append(u3 * spread * 0.8)
        ends.append(u3 * spread * 0.8 + life_i)
    t_early = math.ceil(max(delays)) + 1
    t_later = math.floor(min(ends)) - 1
    assert t_early < t_later  # 公共存活窗非空
    early = burst_particles_at(burst, t_early, 500.0, 300.0, 120.0, 100.0)
    later = burst_particles_at(burst, t_later, 500.0, 300.0, 120.0, 100.0)
    assert len(early) == len(later) == 12
    for state_a, state_b in zip(early, later):
        # 原地：位置与固定旋转不随 t 变化（新版运动学有漂移项）。
        assert state_b.x == pytest.approx(state_a.x)
        assert state_b.y == pytest.approx(state_a.y)
        assert state_b.rotation_deg == pytest.approx(state_a.rotation_deg)
        assert -30.0 <= state_a.rotation_deg <= 90.0  # u3*120-30
        assert 0.0 < state_a.alpha <= 1.0
        assert state_a.size_px > 0.0
    # 对称铺满字框：y ∈ ±0.425×box_h（新版偏置带 [-0.62,+0.30] 外加漂移）。
    for state in early:
        assert -42.5 - 1e-6 <= state.y - 300.0 <= 42.5 + 1e-6
    # 窗口外/寿命终了即消失。
    assert burst_particles_at(burst, -1, 500.0, 300.0, 120.0, 100.0) == []
    assert burst_particles_at(burst, 701, 500.0, 300.0, 120.0, 100.0) == []


def test_twinkle_classic_wireup_serialization_gpu_and_colors():
    """旧版档接线：.yurika 行级覆盖 round-trip、GPU 白名单、多颜色粒子。"""
    # 行级覆盖序列化 round-trip。
    override = LineAnimationOverride(sing_fx="twinkle_classic")
    data = line_animation_override_to_dict(override)
    assert data["sing_fx"] == "twinkle_classic"
    restored = line_animation_override_from_dict(data)
    assert restored is not None and restored.sing_fx == "twinkle_classic"
    # GPU sidecar 原生求值（burst 随 IR 下发），不触发整帧 Painter 回退。
    track = type("Track", (), {"lines": []})()
    style = Style(sing_fx="twinkle_classic", karaoke_anim="none")
    assert gpu_unsupported_features(track, style) == ()
    # 多颜色粒子设计接线：颜色模式与新版档同路——「跟随字体·走字后」
    # 产出实色 + paint 装饰规格（取色层级 +装饰，渐变/描边随粒子下发）。
    wired = plan_line_bursts(
        Style(
            sing_fx="twinkle_classic",
            fx_particle_size_em=0.5,
            fx_particle_count=10,
            fx_particle_color_mode="follow_after",
            fx_particle_color_layers="decor",
            font_size_px=100,
            karaoke_anim="none",
        ),
        0, 0, 1000, 900, [(0, 400)],
    )
    assert wired and wired[0]["kind"] == "twinkle_classic"
    assert wired[0]["color"].startswith("#")
    assert "paint" in wired[0]
    assert {"fill", "stroke", "stroke2"} <= set(wired[0]["paint"])


def test_plan_line_bursts_anim_kinds_and_sing_ripple():
    """音符/拼接入场动画 + 唱字涟漪档。"""
    style = Style(
        entry_anim="note",
        entry_lead_ms=600,
        exit_anim="dissolve_out",
        exit_fade_ms=600,
        sing_fx="ripple",
        font_size_px=100,
        karaoke_anim="none",
    )
    bursts = plan_line_bursts(
        style, 0, 1000, 4200, 4100,
        [(1200, 1600), (1600, 2000), (2000, 3000)],
    )
    kinds = [burst["kind"] for burst in bursts]
    # 音符入场：逐字错峰（3 字 → 步距 175）+ ±80ms 确定性时间抖动，
    # 每字 3 颗拆樱花粉双色 2+1（同字两变体共享起点）。
    assert kinds.count("note") == 6
    notes = [b for b in bursts if b["kind"] == "note"]
    for char_index, base in enumerate((1000, 1175, 1350)):
        starts = [
            b["start_ms"] for b in notes if b["char_index"] == char_index
        ]
        assert len(starts) == 2
        assert all(abs(start - base) <= 80 for start in starts)
    assert len({b["start_ms"] for b in notes}) > 1  # 逐字不再严格等差错峰
    assert sorted({b["count"] for b in notes}) == [1, 2]
    assert {b["color"] for b in notes} == {"#FFB7C5", "#FFD7E0"}
    # 消散退场：像素方块、固定默认档（7 颗/字拆双色 4+3）。
    assert kinds.count("dissolve") == 6
    dissolve = next(b for b in bursts if b["kind"] == "dissolve")
    assert dissolve["count"] == 4
    assert dissolve["size_px"] == pytest.approx(100 * 0.40 * 0.75)
    assert {b["color"] for b in bursts if b["kind"] == "dissolve"} == {
        "#FFB7C5", "#FFD7E0",
    }
    # 唱字涟漪：每字两圈细环、旋钮尺寸 ×3.6（三个窗都非零时长）。
    assert kinds.count("ripple") == 3
    sing_ripple = next(b for b in bursts if b["kind"] == "ripple")
    assert sing_ripple["count"] == 3  # RIPPLE_RING_COUNT
    assert sing_ripple["size_px"] == pytest.approx(100 * 0.40 * 3.6)
    assert any(
        abs(sing_ripple["start_ms"] - base) <= 80
        for base in (1200, 1600, 2000)
    )


def test_burst_particles_window_and_shapes():
    style = Style(
        entry_anim="sparkle",
        entry_lead_ms=600,
        exit_anim="ripple",
        exit_fade_ms=600,
        fx_particle_count=6,
        font_size_px=100,
        karaoke_anim="none",
    )
    bursts = plan_line_bursts(style, 0, 0, 1000, 900, [(0, 500)])
    sparkle = bursts[0]
    assert burst_particles_at(
        sparkle, sparkle["start_ms"] - 1, 0.0, 0.0, 0, 0
    ) == []
    assert burst_particles_at(
        sparkle, sparkle["end_ms"] + 1, 0.0, 0.0, 0, 0
    ) == []
    # 整行盒：粒子应铺满整行宽度（sweep=1，出生按横向位置错峰）。
    states = burst_particles_at(sparkle, 300, 0.0, 0.0, 600.0, 100.0)
    assert 0 < len(states) <= sparkle["count"]
    for state in states:
        assert 0.0 < state.alpha <= 1.0
        assert state.size_px > 0.0
        assert -360.0 <= state.x <= 360.0

    ripple_bursts = plan_line_bursts(
        Style(exit_anim="ripple", exit_fade_ms=600, karaoke_anim="none"),
        0, None, 2000, 1900, [(0, 100)],
    )
    # 固定默认档拆樱花粉双色（3 环 → 2+1）；环级物理用合成 3 环 burst 验。
    assert [b["kind"] for b in ripple_bursts[:2]] == ["ripple", "ripple"]
    assert sorted(b["count"] for b in ripple_bursts[:2]) == [1, 2]
    ripple = {**ripple_bursts[0], "count": 3}
    assert ripple["kind"] == "ripple"
    # 水波纹：第 2 环延迟 150ms、第 3 环 300ms 出生；各环完整走完「小→大」。
    first_only = burst_particles_at(ripple, ripple["start_ms"] + 50, 0.0, 0.0, 0, 0)
    assert len(first_only) == 1
    two = burst_particles_at(ripple, ripple["start_ms"] + 200, 0.0, 0.0, 0, 0)
    assert len(two) == 2
    all_rings = burst_particles_at(ripple, ripple["start_ms"] + 380, 0.0, 0.0, 0, 0)
    assert len(all_rings) == 3
    # 后出生的环更年轻 → 半径更小、更亮。
    assert all_rings[2].size_px < all_rings[1].size_px < all_rings[0].size_px
    assert all_rings[2].alpha > all_rings[1].alpha > all_rings[0].alpha
    assert burst_particles_at(ripple, ripple["end_ms"] + 1, 0.0, 0.0, 0, 0) == []


def test_fx_sprites_contract():
    # ring：外/内两段子路径且绕向相反（winding 与 even-odd 都呈圆环）。
    ring = FX_SPRITES["ring"]["path_commands"]
    moves = [command for command in ring if command[0] == "M"]
    assert len(moves) == 2
    assert FX_SPRITES["star4"]["path_commands"][0][0] == "M"
    for name in ("star4", "ring", "note"):
        commands = FX_SPRITES[name]["path_commands"]
        assert commands[-1][0] == "Z"
        for command in commands:
            assert command[0] in {"M", "L", "C", "Q", "Z"}
    assert sprite_for_kind("sparkle") == "star4"
    assert sprite_for_kind("ripple") == "ring"
    assert sprite_for_kind("note") == "note"


def test_gpu_supports_new_kinds_without_fallback():
    track = type("Track", (), {"lines": []})()
    style = Style(
        entry_anim="tracking_in",
        exit_anim="scatter_out",
        karaoke_anim="utopia",
        reverse_karaoke_anim="inherit",
        entry_fx="sparkle",
        exit_fx="ripple",
        sing_fx="note",
        karaoke_stroke_flash=True,
    )
    assert gpu_unsupported_features(track, style) == ()

    legacy = Style()
    assert gpu_unsupported_features(track, legacy) == ()



def test_line_animation_override_accepts_new_kinds():
    override = LineAnimationOverride(
        entry_anim="wave_in",
        exit_anim="converge_out",
    )
    assert override.entry_anim == "wave_in"
    assert override.exit_anim == "converge_out"


def test_painter_sing_fx_char_anchor_smoke(qapp):
    """CPU painter 唱字粒子（char 锚点）冒烟：不抛异常且画面有粒子。"""
    from PyQt6.QtGui import QImage

    from krok_helper.subtitle_render.domain.timing import TimingChar, TimingTrack
    from krok_helper.subtitle_render.engine.painter import paint_frame

    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[
                    TimingChar(text="あ", start_ms=1000),
                    TimingChar(text="い", start_ms=1600),
                ],
                end_ms=2200,
            )
        ]
    )
    style = Style(
        sing_fx="twinkle",
        karaoke_anim="utopia",
        fx_particle_size_em=0.6,
        fx_particle_count=8,
    )
    img = QImage(800, 450, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(0xFF101010)
    paint_frame(img, track, 1300, style)  # 「あ」唱到一半，wipe 锚点命中


def test_painter_sing_fx_classic_smoke(qapp):
    """CPU painter 旧版唱字星光冒烟：char 锚点 + 原地闪烁不抛异常。"""
    from PyQt6.QtGui import QImage

    from krok_helper.subtitle_render.domain.timing import TimingChar, TimingTrack
    from krok_helper.subtitle_render.engine.painter import paint_frame

    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[
                    TimingChar(text="あ", start_ms=1000),
                    TimingChar(text="い", start_ms=1600),
                ],
                end_ms=2200,
            )
        ]
    )
    style = Style(
        sing_fx="twinkle_classic",
        karaoke_anim="utopia",
        fx_particle_size_em=0.6,
        fx_particle_count=8,
    )
    img = QImage(800, 450, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(0xFF101010)
    paint_frame(img, track, 1300, style)


def test_style_controller_keeps_new_geo_kinds():
    """回归护栏：属性面板样式漏斗（PropertyStyleController.update →
    normalize_entry/exit_animation）必须保留新几何档位。

    2026-09 用户实测定位：白名单缺新档位时回落 "none"，表现为「选中
    新特效后所有字幕行被刷为无、预览退化为无特效」——渲染端一切正常。
    """
    from krok_helper.subtitle_render.settings.property_controllers import (
        PropertyStyleController,
    )

    controller = PropertyStyleController()
    base = Style()
    cases = {
        "entry_anim": (
            "tracking_in", "wave_in", "stretch_in", "glow_in", "assemble_in",
            "sparkle", "ripple", "note", "heart", "petal", "snow", "snow_solid",
        ),
        "exit_anim": (
            "scatter_out", "converge_out", "stretch_out", "glow_out", "dissolve_out",
            "sparkle", "ripple", "note", "heart", "petal", "snow", "snow_solid",
        ),
        "section_head_anim": (
            "tracking_in", "wave_in", "stretch_in", "glow_in", "assemble_in",
            "sparkle", "ripple", "note", "heart", "petal", "snow", "snow_solid",
        ),
        "section_tail_anim": (
            "scatter_out", "converge_out", "stretch_out", "glow_out", "dissolve_out",
            "sparkle", "ripple", "note", "heart", "petal", "snow", "snow_solid",
        ),
    }
    for field, values in cases.items():
        for value in values:
            result = controller.update(base, {field: value})
            assert getattr(result.style, field) == value
    # 未知值仍回落 none（既有语义不回归）。
    assert controller.update(base, {"entry_anim": "bogus"}).style.entry_anim == "none"


def test_ring_sprite_bezier_midpoints_stay_on_circle():
    """回归护栏（纯数学，无 Qt 依赖）：圆环 sprite 的每段贝塞尔中点必须
    落在圆周上（距圆心 = 半径，±2%）。

    2026-09 用户实测定位：_circle_commands 的 flip 只反转了角度、没同步
    反转切向，内圆四段贝塞尔外翻、孔洞被挤成菱形（铜钱观感）。
    """
    import math

    commands = FX_SPRITES["ring"]["path_commands"]
    circles: list[tuple[float, list[tuple[float, float]]]] = []
    current_radius = 0.0
    points: list[tuple[float, float]] = []
    previous = (0.0, 0.0)
    for command in commands:
        kind = command[0]
        if kind == "M":
            if points:
                circles.append((current_radius, points))
            current_radius = abs(float(command[1]))
            previous = (float(command[1]), float(command[2]))
            points = []
        elif kind == "C":
            p0 = previous
            c1 = (float(command[1]), float(command[2]))
            c2 = (float(command[3]), float(command[4]))
            p1 = (float(command[5]), float(command[6]))
            mid = tuple(
                (p0[i] + 3 * c1[i] + 3 * c2[i] + p1[i]) / 8.0 for i in (0, 1)
            )
            points.append(mid)
            previous = p1
    if points:
        circles.append((current_radius, points))

    assert len(circles) == 2
    for radius, mids in circles:
        # kappa 圆的每段中点落在 45° 弧上，距圆心即半径（±2%）；
        # 旧缺陷（切向未随绕向翻转）下中点半径会掉到 ~0.41r。
        for mid in mids:
            distance = math.hypot(*mid)
            assert distance == pytest.approx(radius, rel=0.02), (
                f"半径 {radius} 的贝塞尔中点 {mid} 偏离圆周"
            )


def test_stretch_and_glow_transition_math():
    """逐字拉伸（stretch）与整行辉光（glow）的编排与包络。"""
    from krok_helper.subtitle_render.engine.render.elements.horizontal.transitions import (
        transition_char_glow,
    )

    style = Style(font_size_px=100, entry_lead_ms=600, exit_fade_ms=600)
    ctx = line_char_transition_context(
        Style(entry_anim="glow_in", entry_lead_ms=600, karaoke_anim="none"),
        TimingLine(), 100, 0, 6000, 3,
    )
    assert ctx is not None and ctx.effect == "glow_in"

    # 逐字拉伸：出生 3.2× 横向拉伸、归位 1.0；消散反向拉到 3.2×。
    born = _geo_char_state(
        style, _entry_transition("stretch_in", 0), 0, 5,
        t_ms=0, char_center_x=None, line_center_x=None,
    )
    assert born[0] == 0.0
    assert born[4] == pytest.approx(3.2)
    settled = _geo_char_state(
        style, _entry_transition("stretch_in", 0), 0, 5,
        t_ms=10_000, char_center_x=None, line_center_x=None,
    )
    assert settled[0] == 1.0 and settled[4] == pytest.approx(1.0)
    gone = _geo_char_state(
        style, _exit_transition("stretch_out", 0), 0, 5,
        t_ms=10_000, char_center_x=None, line_center_x=None,
    )
    assert gone[0] == 0.0 and gone[4] == pytest.approx(3.2)

    # 整行辉光：全字同步（任意 index 同值），出生半径 2.0em + 高亮表。
    glow_a = transition_char_glow(_entry_transition("glow_in", 0), 0, 5, t_ms=0, configured_ms=600)
    glow_b = transition_char_glow(_entry_transition("glow_in", 0), 4, 5, t_ms=0, configured_ms=600)
    assert glow_a == glow_b  # 无逐字错峰
    assert glow_a[0] == pytest.approx(1.0)
    assert glow_a[1] == pytest.approx(1.0)  # 回声间距系数（出生 1.0）
    assert glow_a[2] is True  # 横向回声（bright 通道）
    glow_settled = transition_char_glow(
        _entry_transition("glow_in", 0), 2, 5, t_ms=10_000, configured_ms=600
    )
    assert glow_settled[0] == pytest.approx(0.0)
    assert glow_settled[1] == pytest.approx(0.75)
    # 字形保持原形（发光靠重影副本，无 scaleX 拉伸）。
    glow_born_state = _geo_char_state(
        style, _entry_transition("glow_in", 0), 3, 5,
        t_ms=0, char_center_x=None, line_center_x=None,
    )
    assert glow_born_state[0] == 0.0
    assert glow_born_state[4] == pytest.approx(1.6)  # 本体轻度拉伸回弹

    # 拉伸辉光：常规表 + 逐字错峰（不同 index 不同相位）。
    stretch_a = transition_char_glow(
        _entry_transition("stretch_in", 0), 0, 5, t_ms=0, configured_ms=600
    )
    stretch_b = transition_char_glow(
        _entry_transition("stretch_in", 0), 4, 5, t_ms=0, configured_ms=600
    )
    assert stretch_a is not None and stretch_a[2] is False
    # 拉伸辉光逐字错峰：后字出生相位不同（此处仍处满强度等待出生）。
    assert stretch_b is not None

    # 拼接/消散核心曲线：拼接抵达后显形；消散先行散去。
    assembling = _geo_char_state(
        style, _entry_transition("assemble_in", 0), 0, 5,
        t_ms=10_000, char_center_x=None, line_center_x=None,
    )
    assert assembling[0] == 1.0
    early = _geo_char_state(
        style, _entry_transition("assemble_in", 0), 0, 5,
        t_ms=0, char_center_x=None, line_center_x=None,
    )
    assert early[0] == 0.0
    dissolved = _geo_char_state(
        style, _exit_transition("dissolve_out", 0), 0, 5,
        t_ms=10_000, char_center_x=None, line_center_x=None,
    )
    assert dissolved[0] == 0.0

    # 非辉光类特效返回 None。
    assert transition_char_glow(
        _entry_transition("tracking_in", 0), 0, 5, t_ms=0, configured_ms=600
    ) is None


def test_plan_line_bursts_assemble_and_dissolve():
    """粒子拼接/消散：由动画档位驱动，逐字错峰、锚在字符上。"""
    style = Style(
        entry_anim="assemble_in",
        entry_lead_ms=600,
        exit_anim="dissolve_out",
        exit_fade_ms=600,
        fx_particle_count=12,
        font_size_px=100,
        karaoke_anim="none",
    )
    bursts = plan_line_bursts(
        style, 0, 1000, 4000, 3900,
        [(1200, 1600), (1600, 2000), (2000, 3000)],
    )
    kinds = [burst["kind"] for burst in bursts]
    # 固定默认档樱花粉双色：每字 7 颗拆 4+3（拆后总量守恒）。
    assert kinds.count("assemble") == 6
    assert kinds.count("dissolve") == 6
    assemble = next(b for b in bursts if b["kind"] == "assemble")
    assert assemble["anchor"] == "char"
    assert assemble["count"] == 4  # 固定档 max(4, 14//2)=7 拆双色
    assert assemble["start_ms"] == 1000
    assert assemble["end_ms"] == 1000 + 320
    dissolve = next(b for b in bursts if b["kind"] == "dissolve")
    assert dissolve["start_ms"] >= 3900 - 120  # 最小窗钳制


def test_style_funnel_and_gpu_support_glow_kinds():
    """辉光档位全链路：UI 漏斗保留 + GPU 无回退 + 工程序列化可往返。"""
    from krok_helper.subtitle_render.settings.property_controllers import (
        PropertyStyleController,
    )

    controller = PropertyStyleController()
    result = controller.update(Style(), {"entry_anim": "glow_in"})
    assert result.style.entry_anim == "glow_in"
    result = controller.update(Style(), {"exit_anim": "glow_out"})
    assert result.style.exit_anim == "glow_out"

    track = type("Track", (), {"lines": []})()
    style = Style(entry_anim="glow_in", exit_anim="glow_out", karaoke_anim="utopia")
    assert gpu_unsupported_features(track, style) == ()

    from krok_helper.subtitle_render.serialization.timing import (
        line_animation_override_from_dict,
    )

    override = line_animation_override_from_dict(
        {"entry_anim": "glow_in", "exit_anim": "glow_out"}
    )
    assert override is not None and override.entry_anim == "glow_in"


def test_particle_anim_core_is_staggered_char_transition():
    """星光/涟漪/音符出入场动画的本体 = 逐字显形/淡出（粒子拼接同款编排）。"""
    style = Style(font_size_px=100, entry_lead_ms=600, exit_fade_ms=600)
    ctx = line_char_transition_context(
        Style(entry_anim="sparkle", entry_lead_ms=600, karaoke_anim="none"),
        TimingLine(), 100, 0, 6000, 3,
    )
    assert ctx is not None and ctx.effect == "sparkle"

    # 入场：逐字错峰（char1 尚未出生、char0 已显形）。
    first = _geo_char_state(
        style, _entry_transition("sparkle", 0), 0, 5,
        t_ms=100, char_center_x=None, line_center_x=None,
    )
    second = _geo_char_state(
        style, _entry_transition("sparkle", 0), 1, 5,
        t_ms=100, char_center_x=None, line_center_x=None,
    )
    assert first[0] > 0.8
    assert second[0] < 0.5  # 错峰：后字明显滞后（透明度尚低）
    settled = _geo_char_state(
        style, _entry_transition("note", 0), 4, 5,
        t_ms=10_000, char_center_x=None, line_center_x=None,
    )
    assert settled[0] == 1.0

    # 退场：逐字淡出（中段时 char0 已淡、char4 尚未轮到）。
    gone = _geo_char_state(
        style, _exit_transition("ripple", 0), 0, 5,
        t_ms=200, char_center_x=None, line_center_x=None,
    )
    intact = _geo_char_state(
        style, _exit_transition("ripple", 0), 4, 5,
        t_ms=200, char_center_x=None, line_center_x=None,
    )
    assert gone[0] < 0.3
    assert intact[0] == 1.0
    # 窗口结束后全部淡出。
    assert _geo_char_state(
        style, _exit_transition("ripple", 0), 4, 5,
        t_ms=10_000, char_center_x=None, line_center_x=None,
    )[0] == 0.0

    # 花瓣（petal）同款逐字渐显/渐隐 + 过渡上下文接线。
    petal_ctx = line_char_transition_context(
        Style(entry_anim="petal", entry_lead_ms=600, karaoke_anim="none"),
        TimingLine(), 100, 0, 6000, 3,
    )
    assert petal_ctx is not None and petal_ctx.effect == "petal"
    assert line_char_transition_context(
        Style(exit_anim="petal", exit_fade_ms=600, karaoke_anim="none"),
        TimingLine(), 5900, 0, 6000, 3,
    ).effect == "petal"
    petal_first = _geo_char_state(
        style, _entry_transition("petal", 0), 0, 5,
        t_ms=100, char_center_x=None, line_center_x=None,
    )
    petal_second = _geo_char_state(
        style, _entry_transition("petal", 0), 1, 5,
        t_ms=100, char_center_x=None, line_center_x=None,
    )
    assert petal_first[0] > 0.8
    assert petal_second[0] < 0.5


def test_sing_particles_skip_whitespace_chars():
    """空格无走字内容：唱字粒子跳过；出入场粒子仍整行参与。"""
    style = Style(
        sing_fx="twinkle",
        entry_anim="ripple",
        entry_lead_ms=600,
        karaoke_anim="none",
    )
    # 「あ い」中间是空格：3 个字符窗。
    windows = [(1200, 1600), (1600, 2000), (2000, 2400)]
    visible = [True, False, True]
    bursts = plan_line_bursts(
        style, 0, 1000, 4000, 3900, windows, char_visible=visible
    )
    sing_starts = [b["start_ms"] for b in bursts if b["kind"] == "twinkle"]
    # 空格（1600 起）不发射唱字粒子。
    assert 1600 not in sing_starts
    assert sorted(sing_starts) == [1200, 2000]
    # 入场涟漪仍按整行逐字（3 字 × 固定档双色 2+1 拆），含空格位。
    ripples = [b for b in bursts if b["kind"] == "ripple"]
    assert [b["char_index"] for b in ripples] == [0, 0, 1, 1, 2, 2]


# ---------------------------------------------------------------------------
# 2026-10 粒子颜色模式（单独颜色 / 跟随字体·走字前后 / 复用配色方案）
# 与「入退场同用」联动开关
# ---------------------------------------------------------------------------


def _karaoke_matrix(before: str, after: str):
    from krok_helper.subtitle_render.domain.paint import (
        KaraokeColorState,
        KaraokeColors,
    )
    from krok_helper.subtitle_render.engine.style.style_semantics import solid_fill

    return KaraokeColors(
        before=KaraokeColorState(text=solid_fill(before)),
        after=KaraokeColorState(text=solid_fill(after)),
    )


def test_particle_color_modes_follow_font_states():
    """跟随字体·走字前/后：取所在行有效配色的主文字填充折算实色。"""
    from krok_helper.subtitle_render.engine.render.effects.particles import (
        particle_solid_color,
    )

    style = Style(
        fx_particle_color="#00FF00",
        karaoke_colors=_karaoke_matrix("#123456", "#ABCDEF"),
    )
    assert particle_solid_color(style) == "#00FF00"  # 默认单独颜色
    assert particle_solid_color(
        Style(
            fx_particle_color="#00FF00",
            karaoke_colors=_karaoke_matrix("#123456", "#ABCDEF"),
            fx_particle_color_mode="follow_before",
        )
    ) == "#123456"
    assert particle_solid_color(
        Style(
            fx_particle_color="#00FF00",
            karaoke_colors=_karaoke_matrix("#123456", "#ABCDEF"),
            fx_particle_color_mode="follow_after",
        )
    ) == "#ABCDEF"
    # 旧工程无配色矩阵：走 legacy 推导（before=base_color，after=fill_color）。
    legacy_before = particle_solid_color(
        Style(
            base_color="#2468AC",
            fill_color="#FF00FF",
            fx_particle_color_mode="follow_before",
        )
    )
    legacy_after = particle_solid_color(
        Style(
            base_color="#2468AC",
            fill_color="#FF00FF",
            fx_particle_color_mode="follow_after",
        )
    )
    assert legacy_before == "#2468AC" and legacy_after == "#FF00FF"


def test_particle_color_mode_gradient_averages_stops():
    """渐变/拼色填充折算为停止色平均；图片填充回退单独颜色。"""
    from krok_helper.subtitle_render.domain.paint import (
        KaraokeColorState,
        KaraokeColors,
        PaintFill,
    )
    from krok_helper.subtitle_render.engine.render.effects.particles import (
        fill_to_solid_color,
        particle_solid_color,
    )

    gradient = PaintFill(
        mode="gradient_horizontal",
        start_color="#FF0000",
        end_color="#0000FF",
        gradient_stops=[(0, "#FF0000"), (50, "#00FF00"), (100, "#0000FF")],
    )
    # (FF0000 + 00FF00 + 0000FF) / 3 = (85, 85, 85)。
    assert fill_to_solid_color(gradient, "#123456") == "#555555"
    split = PaintFill(
        mode="split_vertical",
        split_top_color="#FF0000",
        split_bottom_color="#0000FF",
        split_stops=[(0, "#FF0000"), (100, "#0000FF")],
    )
    assert fill_to_solid_color(split, "#123456") == "#7F007F"
    image = PaintFill(mode="image", image_path="x.png")
    assert fill_to_solid_color(image, "#00FF00") == "#00FF00"
    style = Style(
        fx_particle_color="#00FF00",
        fx_particle_color_mode="follow_after",
        karaoke_colors=KaraokeColors(
            after=KaraokeColorState(text=gradient),
        ),
    )
    assert particle_solid_color(style) == "#555555"


def test_particle_color_mode_role_source_and_dangling_fallback():
    """复用配色方案：取来源「走字后-主文字」；悬空引用回退单独颜色。"""
    from krok_helper.subtitle_render.domain.models import (
        SCANLINE_GLOBAL_ROLE_KEY,
        SubtitleStyleScheme,
    )
    from krok_helper.subtitle_render.engine.render.effects.particles import (
        particle_solid_color,
    )

    scheme = SubtitleStyleScheme(
        karaoke_colors=_karaoke_matrix("#000000", "#EE7700")
    )
    style = Style(
        fx_particle_color="#00FF00",
        fx_particle_color_mode="role",
        fx_particle_role_name="主唱",
        custom_style_schemes={"主唱": scheme},
        karaoke_colors=_karaoke_matrix("#111111", "#222222"),
    )
    assert particle_solid_color(style) == "#EE7700"
    # 全局默认 = 主样式自身的走字后。
    assert particle_solid_color(
        Style(
            fx_particle_color="#00FF00",
            fx_particle_color_mode="role",
            fx_particle_role_name=SCANLINE_GLOBAL_ROLE_KEY,
            karaoke_colors=_karaoke_matrix("#111111", "#222222"),
        )
    ) == "#222222"
    # 名字悬空（历史工程/手工 JSON）：回退 color 档。
    assert particle_solid_color(
        Style(
            fx_particle_color="#00FF00",
            fx_particle_color_mode="role",
            fx_particle_role_name="已删除",
        )
    ) == "#00FF00"


def test_fx_apply_to_entry_exit_switch_plans():
    """「入退场同用」：开启后入退场动画粒子的颜色/尺寸吃旋钮；数量恒固定。"""

    def _style(**extra):
        base = dict(
            entry_anim="assemble_in",
            entry_lead_ms=600,
            exit_anim="dissolve_out",
            exit_fade_ms=600,
            sing_fx="twinkle",
            fx_particle_size_em=0.5,
            fx_particle_count=10,
            fx_particle_color="#FF8800",
            font_size_px=100,
            karaoke_anim="none",
        )
        base.update(extra)
        return Style(**base)

    windows = [(1200, 1600), (1600, 3000)]
    off = plan_line_bursts(_style(), 0, 1000, 4000, 3900, windows)
    # 默认关：入退场动画粒子固定默认档 = 樱花粉双色（14//2=7 拆 4+3）
    # + 固定尺寸（40% × 0.75），唱字吃旋钮。
    assemble_off = [b for b in off if b["kind"] == "assemble"]
    assert sorted(b["count"] for b in assemble_off) == [3, 3, 4, 4]  # 2 字 × (4+3)
    assert {b["color"] for b in assemble_off} == {"#FFB7C5", "#FFD7E0"}
    for burst in assemble_off:
        assert burst["size_px"] == pytest.approx(100 * 0.40 * 0.75)
    twinkle = next(b for b in off if b["kind"] == "twinkle")
    assert twinkle["color"] == "#FF8800"
    assert twinkle["size_px"] == pytest.approx(50.0)

    on = plan_line_bursts(
        _style(fx_apply_to_entry_exit=True), 0, 1000, 4000, 3900, windows
    )
    # 开启后：颜色与尺寸跟随旋钮；数量仍固定。自定义颜色一与默认白色
    # 颜色二按「必须双色」口径混发（拆双色后总量守恒：4+3=7）。
    assemble_pair = [b for b in on if b["kind"] == "assemble"][:2]
    twinkle_pair = [b for b in on if b["kind"] == "twinkle"][:2]
    assert {b["color"] for b in assemble_pair} == {"#FF8800", "#FFFFFF"}
    for burst in assemble_pair:
        assert burst["size_px"] == pytest.approx(50.0 * 0.75)
    assert sorted(b["count"] for b in assemble_pair) == [3, 4]
    assert {b["color"] for b in twinkle_pair} == {"#FF8800", "#FFFFFF"}
    for burst in twinkle_pair:
        assert burst["size_px"] == pytest.approx(50.0)

    # 颜色模式对入退场动画粒子同样生效（开启联动时）。
    follow = plan_line_bursts(
        _style(
            fx_apply_to_entry_exit=True,
            fx_particle_color_mode="follow_after",
            karaoke_colors=_karaoke_matrix("#000000", "#EE7700"),
        ),
        0,
        1000,
        4000,
        3900,
        windows,
    )
    assert next(b for b in follow if b["kind"] == "assemble")["color"] == "#EE7700"
    # 关闭联动时模式只影响唱字粒子，入退场动画粒子仍是固定樱花粉默认档。
    follow_off = plan_line_bursts(
        _style(
            fx_particle_color_mode="follow_after",
            karaoke_colors=_karaoke_matrix("#000000", "#EE7700"),
        ),
        0,
        1000,
        4000,
        3900,
        windows,
    )
    assert {
        b["color"] for b in follow_off if b["kind"] == "assemble"
    } == {"#FFB7C5", "#FFD7E0"}
    assert next(b for b in follow_off if b["kind"] == "twinkle")["color"] == "#EE7700"


def test_particle_color_mode_serialization_roundtrip():
    """颜色模式/来源/联动开关三字段的工程序列化往返与非法值回退。"""
    from krok_helper.subtitle_render.domain.models import style_from_dict, style_to_dict

    style = Style(
        fx_particle_color_mode="follow_after",
        fx_particle_role_name="主唱",
        fx_apply_to_entry_exit=True,
    )
    restored = style_from_dict(style_to_dict(style))
    assert restored.fx_particle_color_mode == "follow_after"
    assert restored.fx_particle_role_name == "主唱"
    assert restored.fx_apply_to_entry_exit is True
    # 旧工程 payload（无新字段）：回落默认，不炸。
    legacy = style_from_dict({})
    assert legacy.fx_particle_color_mode == "color"
    assert legacy.fx_particle_role_name is None
    assert legacy.fx_apply_to_entry_exit is False
    assert legacy.fx_particle_color2 == "#FFFFFF"  # 双色槽默认白（恒双色）
    # 双色槽往返。
    dual = style_from_dict(
        style_to_dict(
            Style(fx_particle_color="#40E0FF", fx_particle_color2="#FF69B4")
        )
    )
    assert dual.fx_particle_color == "#40E0FF"
    assert dual.fx_particle_color2 == "#FF69B4"
    # 取色层级往返 + 非法值回退（默认仅实色）。
    assert Style().fx_particle_color_layers == "solid"
    layered = style_from_dict(
        style_to_dict(Style(fx_particle_color_layers="all"))
    )
    assert layered.fx_particle_color_layers == "all"
    bogus = style_to_dict(Style())
    bogus["fx_particle_color_layers"] = "bogus"
    assert style_from_dict(bogus).fx_particle_color_layers == "solid"
    # 非法值回落默认；空串来源名归一 None。
    payload = style_to_dict(Style())
    payload["fx_particle_color_mode"] = "brighten"  # 粒子无此档
    payload["fx_particle_role_name"] = "  "
    payload["fx_apply_to_entry_exit"] = 0
    degraded = style_from_dict(payload)
    assert degraded.fx_particle_color_mode == "color"
    assert degraded.fx_particle_role_name is None
    assert degraded.fx_apply_to_entry_exit is False


def test_particle_role_reference_remap_chain():
    """来源改名连带改写；删除连模式一起回退单独颜色（扫字线同款）。"""
    from krok_helper.subtitle_render.domain.models import (
        remap_particle_role_reference,
    )

    style = Style(fx_particle_color_mode="role", fx_particle_role_name="主唱")
    renamed = remap_particle_role_reference(style, {"主唱": "领唱"})
    assert renamed is not None
    assert renamed.fx_particle_role_name == "领唱"
    assert renamed.fx_particle_color_mode == "role"
    deleted = remap_particle_role_reference(style, {"主唱": None})
    assert deleted is not None
    assert deleted.fx_particle_color_mode == "color"
    assert deleted.fx_particle_role_name is None
    # 引用未触及返回 None（调用方跳过样式写回）。
    assert remap_particle_role_reference(style, {"和声": "伴唱"}) is None
    assert remap_particle_role_reference(Style(), {"主唱": None}) is None


def test_painter_paints_per_burst_particle_color(qapp):
    """CPU painter 按 burst 颜色绘制（与 D2D burst.color 同口径）。

    2026-10 发现的历史分歧：painter 此前把所有 burst 一律画成
    ``fx_particle_color``，而 GPU 侧入退场动画粒子按规划走固定默认档
    （现为樱花粉双色）——旋钮色非默认时两后端画面不一致。这里用「无粒子
    基线」逐像素差分验证跟随字体两档的唱字粒子按行配色着色（字形本体
    两帧完全一致，差分只剩粒子贡献）。
    """
    from PyQt6.QtGui import QImage

    from krok_helper.subtitle_render.domain.timing import TimingChar, TimingTrack
    from krok_helper.subtitle_render.engine.painter import paint_frame

    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[
                    TimingChar(text="あ", start_ms=1000),
                    TimingChar(text="い", start_ms=1600),
                ],
                end_ms=2200,
            )
        ]
    )

    def _render(mode: str) -> QImage:
        style = Style(
            sing_fx="twinkle",
            karaoke_anim="utopia",
            fx_particle_size_em=0.8,
            fx_particle_count=16,
            fx_particle_color_mode=mode,
            karaoke_colors=_karaoke_matrix("#FF0000", "#0000FF"),
        )
        img = QImage(800, 450, QImage.Format.Format_ARGB32_Premultiplied)
        img.fill(0xFF101010)
        paint_frame(img, track, 1300, style)
        return img

    def _dominance(mode: str) -> tuple[int, int]:
        """与「无粒子基线」差分中红/蓝主导的像素数。

        半透明粒子叠在异色字形上会向基线色偏移（弱信号不计入主导），
        因此断言用两档之间的相对主导方向判颜色，而不是绝对为零。
        """
        with_fx = _render(mode)
        none_img = QImage(800, 450, QImage.Format.Format_ARGB32_Premultiplied)
        none_img.fill(0xFF101010)
        paint_frame(
            none_img,
            track,
            1300,
            Style(
                sing_fx="none",
                karaoke_anim="utopia",
                karaoke_colors=_karaoke_matrix("#FF0000", "#0000FF"),
            ),
        )
        red_dominant = blue_dominant = 0
        for y in range(0, with_fx.height(), 2):
            for x in range(0, with_fx.width(), 2):
                pixel = with_fx.pixel(x, y)
                if pixel == none_img.pixel(x, y):
                    continue  # 粒子未覆盖的纯字形/背景像素
                red = (pixel >> 16) & 0xFF
                green = (pixel >> 8) & 0xFF
                blue = pixel & 0xFF
                if red > 140 and red > green + 40 and red > blue + 40:
                    red_dominant += 1
                if blue > 140 and blue > red + 40 and blue > green + 40:
                    blue_dominant += 1
        return red_dominant, blue_dominant

    # 跟随字体·走字前：粒子红主导（走字前主文字色）。
    red_before, blue_before = _dominance("follow_before")
    assert red_before > 8, f"走字前粒子应红主导，实测红 {red_before} 蓝 {blue_before}"
    assert red_before > blue_before
    # 跟随字体·走字后：粒子蓝主导（走字后主文字色）。
    red_after, blue_after = _dominance("follow_after")
    assert blue_after > 8, f"走字后粒子应蓝主导，实测蓝 {blue_after} 红 {red_after}"
    assert blue_after > red_after


# ---------------------------------------------------------------------------
# 2026-10 二期：逐字角色解析 + 完整装饰规格（渐变/描边/二重描边 + 极坐标环）
# ---------------------------------------------------------------------------


def _gradient_fill(mode: str, start: str, end: str):
    from krok_helper.subtitle_render.domain.paint import PaintFill

    return PaintFill(
        mode=mode,
        start_color=start,
        end_color=end,
        gradient_stops=[(0, start), (100, end)],
        split_top_color=start,
        split_bottom_color=end,
        split_stops=[(0, start), (100, end)],
    )


def test_particle_paint_spec_follows_each_chars_role():
    """跟随字体逐字取色：同一行内不同角色方案的字符拿到各自的三层规格。"""
    from krok_helper.subtitle_render.domain.models import SubtitleStyleScheme
    from krok_helper.subtitle_render.domain.paint import (
        KaraokeColorState,
        KaraokeColors,
    )
    from krok_helper.subtitle_render.engine.style.style_semantics import style_for_role

    scheme_a = SubtitleStyleScheme(
        karaoke_colors=KaraokeColors(
            after=KaraokeColorState(text=_gradient_fill(
                "gradient_horizontal", "#FF8800", "#00FF88"
            ))
        )
    )
    scheme_b = SubtitleStyleScheme(
        karaoke_colors=KaraokeColors(
            after=KaraokeColorState(text=_gradient_fill(
                "gradient_vertical", "#FF0000", "#0000FF"
            ))
        )
    )
    style = Style(
        font_size_px=100,
        sing_fx="twinkle",
        fx_particle_color_mode="follow_after",
        fx_particle_color_layers="decor",
        karaoke_anim="none",
        custom_style_schemes={"主唱": scheme_a, "和声": scheme_b},
        karaoke_colors=_karaoke_matrix("#111111", "#222222"),
    )
    char_styles = [
        style_for_role(style, "主唱"),
        style_for_role(style, "和声"),
        None,  # 未挂角色的字符：回落行样式
    ]
    windows = [(1200, 1600), (1600, 2000), (2000, 2400)]
    bursts = plan_line_bursts(
        style, 0, 1000, 4000, 3900, windows, char_styles=char_styles
    )
    twinkle = sorted(
        (b for b in bursts if b["kind"] == "twinkle"),
        key=lambda b: b["char_index"],
    )
    assert len(twinkle) == 3
    # 字 0：主唱方案的横向渐变；字 1：和声方案的竖向渐变；字 2：行样式纯色。
    assert twinkle[0]["paint"]["fill"]["mode"] == "gradient_horizontal"
    assert twinkle[1]["paint"]["fill"]["mode"] == "gradient_vertical"
    assert twinkle[2]["paint"]["fill"]["mode"] == "solid"
    assert twinkle[2]["paint"]["fill"]["color"] == "#222222"
    # 实色回退同步按字符解析（burst.color 与规格同源）。
    assert twinkle[0]["color"] == "#7FC344"  # (FF8800+00FF88)/2
    assert twinkle[1]["color"] == "#7F007F"
    assert twinkle[2]["color"] == "#222222"
    # 未提供 char_styles 时（旧调用方）：整体回落行样式。
    fallback = plan_line_bursts(style, 0, 1000, 4000, 3900, windows)
    assert all(
        b["paint"]["fill"]["color"] == "#222222"
        for b in fallback
        if b["kind"] == "twinkle"
    )


def test_particle_solid_layer_keeps_gradient_fill_only():
    """“仅实色” = 仅文字填充层：渐变/图片照样透传，只去掉描边1/描边2/
    装饰；纯 solid 填充才走无 paint 的纯色快路径（2026-10-18 老 BUG）。"""
    from krok_helper.subtitle_render.domain.paint import (
        KaraokeColorState,
        KaraokeColors,
    )

    gradient = _gradient_fill("gradient_horizontal", "#FF8800", "#00FF88")
    style = Style(
        font_size_px=100,
        sing_fx="twinkle",
        fx_particle_color_mode="follow_after",
        fx_particle_color_layers="solid",
        karaoke_anim="none",
        stroke_width_px=8,
        stroke2_enabled=True,
        stroke2_width_px=4,
        decoration_kind="shadow",
        karaoke_colors=KaraokeColors(
            after=KaraokeColorState(text=gradient),
        ),
    )
    bursts = plan_line_bursts(style, 0, 1000, 4000, 3900, [(1200, 1600)])
    spec = next(b for b in bursts if b["kind"] == "twinkle")["paint"]
    assert spec["fill"]["mode"] == "gradient_horizontal"
    assert spec["fill"]["start_color"] == "#FF8800"
    assert spec["fill"]["end_color"] == "#00FF88"
    assert spec["stroke_width_px"] == 0.0
    assert spec["stroke2_width_px"] == 0.0
    assert "decor" not in spec

    solid_style = Style(
        font_size_px=100,
        sing_fx="twinkle",
        fx_particle_color_mode="follow_after",
        fx_particle_color_layers="solid",
        karaoke_anim="none",
        karaoke_colors=_karaoke_matrix("#111111", "#FF8800"),
    )
    solid_bursts = plan_line_bursts(
        solid_style, 0, 1000, 4000, 3900, [(1200, 1600)]
    )
    assert "paint" not in next(
        b for b in solid_bursts if b["kind"] == "twinkle"
    )


def test_particle_paint_spec_stroke_widths_scale_with_source_font():
    """描边/二重描边宽按 粒子尺寸/来源字号 缩放，上限半个粒子边长。"""
    style = Style(
        font_size_px=100,
        sing_fx="twinkle",
        fx_particle_size_em=0.5,  # 粒子 50px
        fx_particle_color_mode="follow_after",
        fx_particle_color_layers="stroke",
        karaoke_anim="none",
        stroke_width_px=8,
        stroke2_enabled=True,
        stroke2_width_px=4,
        karaoke_colors=_karaoke_matrix("#000000", "#FFFFFF"),
    )
    bursts = plan_line_bursts(style, 0, 1000, 4000, 3900, [(1200, 1600)])
    spec = next(b for b in bursts if b["kind"] == "twinkle")["paint"]
    assert spec["stroke_width_px"] == pytest.approx(8 * 0.5)
    assert spec["stroke2_width_px"] == pytest.approx(4 * 0.5)
    # 巨描边钳到半个粒子边长。
    huge = Style(
        font_size_px=20,
        sing_fx="twinkle",
        fx_particle_size_em=0.5,  # 粒子 10px，半边长 5px
        fx_particle_color_mode="follow_after",
        fx_particle_color_layers="stroke",
        karaoke_anim="none",
        stroke_width_px=40,
        stroke2_enabled=False,
        karaoke_colors=_karaoke_matrix("#000000", "#FFFFFF"),
    )
    capped = plan_line_bursts(huge, 0, 1000, 4000, 3900, [(1200, 1600)])
    assert (
        next(b for b in capped if b["kind"] == "twinkle")["paint"][
            "stroke_width_px"
        ]
        <= 5.0 + 1e-6
    )
    # UseEdge2 关闭：二重描边宽恒 0（N3 语义不回归）。
    off = Style(
        font_size_px=100,
        sing_fx="twinkle",
        fx_particle_color_mode="follow_after",
        fx_particle_color_layers="stroke",
        karaoke_anim="none",
        stroke_width_px=8,
        stroke2_enabled=False,
        stroke2_width_px=4,
        karaoke_colors=_karaoke_matrix("#000000", "#FFFFFF"),
    )
    off_spec = next(
        b for b in plan_line_bursts(off, 0, 1000, 4000, 3900, [(1200, 1600)])
        if b["kind"] == "twinkle"
    )["paint"]
    assert off_spec["stroke2_width_px"] == 0.0


def test_particle_paint_spec_image_fill_passes_through():
    """图片填充按位图笔刷透传（两条后端共用 sprite 剪影平铺口径），
    实色仅作缺图回退；取色层级为 solid 时图片也产出规格。"""
    from krok_helper.subtitle_render.domain.paint import (
        KaraokeColorState,
        KaraokeColors,
        PaintFill,
    )

    image_fill = PaintFill(mode="image", image_path="x.png")
    style = Style(
        font_size_px=100,
        sing_fx="twinkle",
        fx_particle_color="#ABCDEF",
        fx_particle_color_mode="follow_after",
        fx_particle_color_layers="decor",
        karaoke_anim="none",
        karaoke_colors=KaraokeColors(
            after=KaraokeColorState(text=image_fill)
        ),
    )
    bursts = plan_line_bursts(style, 0, 1000, 4000, 3900, [(1200, 1600)])
    spec = next(b for b in bursts if b["kind"] == "twinkle")["paint"]
    assert spec["fill"]["mode"] == "image"
    assert spec["fill"]["image_path"] == "x.png"

    # 默认取色层级（solid）下图片同样产出规格（仅实色层没有可用实色）。
    solid_layers_style = replace(style, fx_particle_color_layers="solid")
    solid_bursts = plan_line_bursts(
        solid_layers_style, 0, 1000, 4000, 3900, [(1200, 1600)]
    )
    solid_spec = next(b for b in solid_bursts if b["kind"] == "twinkle")["paint"]
    assert solid_spec["fill"]["mode"] == "image"
    assert solid_spec["stroke_width_px"] == 0.0


def test_painter_image_particle_uses_bitmap_fill(qapp, tmp_path):
    """CPU painter：跟随字体·图片模式时粒子按位图剪影绘制（不是实色
    回退）——整张纯绿素材应把 sprite 区域染成绿色。"""
    from PyQt6.QtGui import QColor, QImage

    from krok_helper.subtitle_render.domain.paint import (
        KaraokeColorState,
        KaraokeColors,
        PaintFill,
    )
    from krok_helper.subtitle_render.domain.timing import TimingChar, TimingTrack
    from krok_helper.subtitle_render.engine.painter import paint_frame

    bitmap = QImage(32, 32, QImage.Format.Format_ARGB32_Premultiplied)
    bitmap.fill(QColor("#00CC44"))
    bitmap_path = tmp_path / "fill.png"
    assert bitmap.save(str(bitmap_path))

    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[TimingChar(text="あ", start_ms=1000)],
                end_ms=1600,
            )
        ]
    )
    style = Style(
        sing_fx="twinkle",
        karaoke_anim="utopia",
        fx_particle_size_em=0.8,
        fx_particle_count=16,
        fx_particle_color="#ABCDEF",
        fx_particle_color_mode="follow_after",
        fx_particle_color_layers="decor",
        karaoke_colors=KaraokeColors(
            after=KaraokeColorState(
                text=PaintFill(
                    mode="image", image_path=str(bitmap_path)
                )
            )
        ),
    )
    img = QImage(800, 450, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(0xFF101010)
    paint_frame(img, track, 1300, style)
    green = 0
    for y in range(0, img.height(), 2):
        for x in range(0, img.width(), 2):
            px = img.pixel(x, y)
            r, g, b = (px >> 16) & 0xFF, (px >> 8) & 0xFF, px & 0xFF
            if g > 120 and g > r + 40 and g > b + 40:
                green += 1
    assert green > 5, f"图片填充的粒子应染上素材绿色，实测 {green}"


def _role_image_scheme(color: str, *, dark_before: str = "#202020"):
    """构造「走字前=近黑实色、走字后=纯色图片」的角色方案：入场时刻
    文字本体尚未走字（before 态近黑），画面上的彩色像素只能来自跟随
    「走字后」图片的入退场粒子。"""
    from krok_helper.subtitle_render.domain.models import SubtitleStyleScheme
    from krok_helper.subtitle_render.domain.paint import (
        KaraokeColorState,
        KaraokeColors,
        PaintFill,
    )

    return SubtitleStyleScheme(
        karaoke_colors=KaraokeColors(
            before=KaraokeColorState(
                text=PaintFill(mode="solid", color=dark_before)
            ),
            after=KaraokeColorState(
                text=PaintFill(mode="image", image_path=color)
            ),
        )
    )


def test_painter_entry_sparkle_multi_role_image(qapp, tmp_path):
    """CPU painter：入场星光多角色行逐字跟各自角色的图片（红/绿两种
    素材都出现），不再退首字符颜色/实色回退。"""
    from PyQt6.QtGui import QColor, QImage

    from krok_helper.subtitle_render.domain.timing import (
        TimingChar,
        TimingLine,
        TimingTrack,
    )
    from krok_helper.subtitle_render.engine.painter import paint_frame

    def png(name: str, color: str) -> str:
        image = QImage(32, 32, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(QColor(color))
        path = tmp_path / name
        assert image.save(str(path))
        return str(path)

    style = Style(
        font_size_px=100,
        entry_anim="sparkle",
        entry_lead_ms=600,
        fx_apply_to_entry_exit=True,
        fx_particle_color_mode="follow_after",
        fx_particle_color_layers="decor",
        karaoke_anim="none",
        base_color="#202020",
        fill_color="#202020",
        stroke_color="#101010",
        stroke_width_px=0,
        stroke2_enabled=False,
        decoration_kind="none",
        custom_style_schemes={
            "主唱": _role_image_scheme(png("red.png", "#FF0000")),
            "和声": _role_image_scheme(png("green.png", "#00FF00")),
        },
    )
    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[
                    TimingChar("あ", 1000, role_label="主唱"),
                    TimingChar("い", 1500, role_label="和声"),
                ],
                end_ms=2500,
            )
        ]
    )
    img = QImage(800, 450, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(0xFF101010)
    # 阈值放宽到 90：粒子透明度随生命期 (1-p) 衰减，取两字窗口重叠的
    # 中段（字 1 启动后不久、字 0 尚未淡尽）两色都应明显可见。
    paint_frame(img, track, 400, style)
    red = green = 0
    for y in range(0, img.height(), 2):
        for x in range(0, img.width(), 2):
            px = img.pixel(x, y)
            r, g, b = (px >> 16) & 0xFF, (px >> 8) & 0xFF, px & 0xFF
            if r > 90 and r > g + 40 and r > b + 40:
                red += 1
            if g > 90 and g > r + 40 and g > b + 20:
                green += 1
    assert red > 50 and green > 50, (
        f"多角色入场星光应分别染上红/绿素材，实测 r={red} g={green}"
    )


def test_painter_gradient_particle_and_radial_ring(qapp):
    """CPU painter：规格档粒子按完整 PaintFill 绘制；涟漪竖向渐变做径向
    映射（新出生的内圈环=起点色亮红，扩散中的外圈环沿渐变轴推进）。"""
    from PyQt6.QtGui import QImage

    from krok_helper.subtitle_render.domain.paint import (
        KaraokeColorState,
        KaraokeColors,
    )
    from krok_helper.subtitle_render.domain.timing import TimingChar, TimingTrack
    from krok_helper.subtitle_render.engine.painter import paint_frame

    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[TimingChar(text="あ", start_ms=1000)],
                end_ms=1600,
            )
        ]
    )
    # 横向渐变填充：粒子内部左端橙、右端绿（主文字纯色不产生绿端）。
    grad_h = _gradient_fill("gradient_horizontal", "#FF8800", "#00FF88")
    style_h = Style(
        sing_fx="twinkle",
        karaoke_anim="utopia",
        fx_particle_size_em=0.8,
        fx_particle_count=16,
        fx_particle_color_mode="follow_after",
        fx_particle_color_layers="decor",
        karaoke_colors=KaraokeColors(
            after=KaraokeColorState(text=grad_h)
        ),
    )
    img = QImage(800, 450, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(0xFF101010)
    paint_frame(img, track, 1300, style_h)
    orange = green = 0
    for y in range(0, img.height(), 2):
        for x in range(0, img.width(), 2):
            px = img.pixel(x, y)
            r, g, b = (px >> 16) & 0xFF, (px >> 8) & 0xFF, px & 0xFF
            if r > 170 and 90 < g < 200 and b < 90:
                orange += 1
            if g > 130 and g > r + 30 and g > b + 10:
                green += 1
    assert orange > 5 and green > 5, f"渐变两端都应出现 o={orange} g={green}"

    # 竖向渐变涟漪：径向映射——出生不久的环按进度取到偏红段（阈值
    # 放宽到 60/20：环透明度随生命期 (1-p)² 衰减，老环必然偏暗）。
    grad_v = _gradient_fill("gradient_vertical", "#FF0000", "#0000FF")
    style_ring = Style(
        sing_fx="ripple",
        karaoke_anim="utopia",
        fx_particle_size_em=0.8,
        fx_particle_color_mode="follow_after",
        fx_particle_color_layers="decor",
        karaoke_colors=KaraokeColors(after=KaraokeColorState(text=grad_v)),
    )
    img2 = QImage(800, 450, QImage.Format.Format_ARGB32_Premultiplied)
    img2.fill(0xFF101010)
    paint_frame(img2, track, 1100, style_ring)
    red_around = 0
    for y in range(0, img2.height(), 2):
        for x in range(0, img2.width(), 2):
            px = img2.pixel(x, y)
            r, g, b = (px >> 16) & 0xFF, (px >> 8) & 0xFF, px & 0xFF
            if r > 60 and r > g + 20 and r > b + 20:
                red_around += 1
    assert red_around > 20, (
        f"径向环新环应取渐变起点的红段，实测红像素 {red_around}"
    )


def test_fx_radial_ring_samples_gradient_by_expansion():
    """涟漪非横向渐变的径向映射：渐变轴 → 半径方向。

    每颗环按自身扩散进度（eased，量化 1/32 档）在渐变轴上采样一个实心
    色：新出生的小环（内圈）= 起点色，扩散开的旧环（外圈）= 终点色——
    「从圆心内到外圆渐变」（2026-10 用户口径；conic 角向扫过已被否定）。
    """
    from krok_helper.subtitle_render.domain.timing import TimingChar, TimingTrack
    from krok_helper.subtitle_render.engine.painter import _fx_fill_color_at

    grad = _gradient_fill("gradient_vertical", "#FF0000", "#0000FF")
    # 采样器口径：0=起点色（纯红），1=终点色（纯蓝），线性插值。
    quarter = _fx_fill_color_at(grad, 0.25)
    assert abs(quarter.red() - 191) <= 1 and abs(quarter.blue() - 64) <= 1
    half = _fx_fill_color_at(grad, 0.5)
    assert abs(half.red() - 128) <= 1 and abs(half.blue() - 128) <= 1
    # 拼色：分段常数（每段标记该色起点）。
    from krok_helper.subtitle_render.domain.paint import PaintFill

    split = PaintFill(
        mode="split_vertical",
        split_top_color="#00FF00",
        split_bottom_color="#FFFFFF",
        split_stops=[(0, "#00FF00"), (50, "#FFFFFF"), (100, "#FFFFFF")],
    )
    assert _fx_fill_color_at(split, 0.3).name().upper() == "#00FF00"
    assert _fx_fill_color_at(split, 0.9).name().upper() == "#FFFFFF"


def test_ripple_bursts_carry_no_stroke_and_spec_cache_reuses():
    """涟漪光环不带描边（环体发丝线，叠描边显著变粗——2026-10 用户口径）；
    同一「样式 × 角色 × 尺寸」组合的装饰规格全帧复用同一对象（缓存命中）。"""
    from krok_helper.subtitle_render.engine.render.effects.particles import (
        clear_particle_paint_cache,
    )

    clear_particle_paint_cache()
    style = Style(
        font_size_px=64,
        sing_fx="ripple",
        karaoke_anim="none",
        fx_particle_color_mode="follow_after",
        fx_particle_color_layers="decor",
        stroke_width_px=8,
        stroke2_enabled=True,
        stroke2_width_px=4,
        karaoke_colors=_karaoke_matrix("#40E0FF", "#FF5A6F"),
    )
    windows = [(1200, 1600), (1600, 2000)]
    first = plan_line_bursts(style, 0, 1000, 4000, 3900, windows)
    rings = [b for b in first if b["kind"] == "ripple"]
    assert rings, "涟漪 burst 应存在"
    for burst in rings:
        assert burst["paint"]["stroke_width_px"] == 0.0
        assert burst["paint"]["stroke2_width_px"] == 0.0
    # 非涟漪档（twinkle）仍带描边——开关只作用于涟漪。
    twinkle_style = Style(
        font_size_px=64,
        sing_fx="twinkle",
        karaoke_anim="none",
        fx_particle_color_mode="follow_after",
        fx_particle_color_layers="stroke",
        stroke_width_px=8,
        stroke2_enabled=True,
        stroke2_width_px=4,
        karaoke_colors=_karaoke_matrix("#40E0FF", "#FF5A6F"),
    )
    twinkle = plan_line_bursts(twinkle_style, 0, 1000, 4000, 3900, windows)
    spec = next(b for b in twinkle if b["kind"] == "twinkle")["paint"]
    assert spec["stroke_width_px"] > 0.0
    assert spec["stroke2_width_px"] > 0.0
    # 缓存 identity：同参数二次规划，spec 与实色对象复用（只烘焙一次）。
    again = plan_line_bursts(style, 0, 1000, 4000, 3900, windows)
    ring_a = next(b for b in first if b["kind"] == "ripple")
    ring_b = next(b for b in again if b["kind"] == "ripple")
    assert ring_a["paint"] is ring_b["paint"]
    assert ring_a["color"] is ring_b["color"] or ring_a["color"] == ring_b["color"]
    # 样式对象更换（编辑后）后缓存不串：新解析、值正确。
    edited = Style(
        font_size_px=64,
        sing_fx="ripple",
        karaoke_anim="none",
        fx_particle_color_mode="follow_after",
        stroke_width_px=8,
        karaoke_colors=_karaoke_matrix("#40E0FF", "#112233"),
    )
    edited_ring = next(
        b
        for b in plan_line_bursts(edited, 0, 1000, 4000, 3900, windows)
        if b["kind"] == "ripple"
    )
    assert edited_ring["color"] == "#112233"
    clear_particle_paint_cache()


def test_painter_follow_mode_uses_each_chars_role(qapp):
    """回归护栏（2026-10）：CPU painter 必须把逐字角色样式传入规划器。

    曾出现 char_styles 已解析却未传给 plan_line_bursts 的漏洞——CPU 逐字
    粒子全回落行样式而 GPU 逐字生效，两端口径分歧。
    """
    from PyQt6.QtGui import QImage

    from krok_helper.subtitle_render.domain.models import SubtitleStyleScheme
    from krok_helper.subtitle_render.domain.paint import (
        KaraokeColorState,
        KaraokeColors,
    )
    from krok_helper.subtitle_render.domain.timing import TimingChar, TimingTrack
    from krok_helper.subtitle_render.engine.painter import paint_frame
    from krok_helper.subtitle_render.engine.render.effects.particles import (
        clear_particle_paint_cache,
    )

    clear_particle_paint_cache()
    grad_h = _gradient_fill("gradient_horizontal", "#FF8800", "#00FF88")
    scheme = SubtitleStyleScheme(
        karaoke_colors=KaraokeColors(
            after=KaraokeColorState(text=grad_h)
        )
    )
    first = TimingChar("あ", 500)
    first.role_label = "主唱"
    track = TimingTrack(
        lines=[TimingLine(chars=[first, TimingChar("い", 1200)], end_ms=1800)]
    )
    style = Style(
        font_size_px=64,
        sing_fx="twinkle",
        karaoke_anim="utopia",
        fx_particle_size_em=0.8,
        fx_particle_count=16,
        fx_particle_color_mode="follow_after",
        custom_style_schemes={"主唱": scheme},
        karaoke_colors=KaraokeColors(
            # 行样式走字后 = 纯红：不产生绿端像素。
            after=KaraokeColorState(
                text=_gradient_fill("solid", "#FF0000", "#FF0000")
            )
        ),
    )
    img = QImage(800, 450, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(0xFF101010)
    paint_frame(img, track, 700, style)
    # 主唱字在行**左端**：行级横向映射下采样到渐变的橙端（#FF8800）；
    # 行样式（纯红 #FF0000）不可能产生橙——橙像素存在即证明逐字角色
    # 样式 + 行内位置采样同时生效（2026-10 口径）。
    orangeish = 0
    for y in range(0, img.height(), 2):
        for x in range(0, img.width(), 2):
            px = img.pixel(x, y)
            r, g, b = (px >> 16) & 0xFF, (px >> 8) & 0xFF, px & 0xFF
            if r > 170 and 80 < g < 160 and b < 90:
                orangeish += 1
    assert orangeish > 10, f"逐字角色粒子未生效，橙端像素 {orangeish}"
    clear_particle_paint_cache()


def test_painter_horizontal_gradient_maps_to_line_span(qapp):
    """横向渐变的行级映射（2026-10 用户实测 bug 的回归护栏）。

    文字的横向渐变以整条显示行为跨度（n3_main_fill_rect 行墨迹并集 /
    GPU fillBounds）；粒子若在自身小框里重走整段渐变，观感与行配色完全
    不同。修复后每颗粒子按其在行内的横向位置采样实心色——左端字=起点
    色、右端字=终点色，粒子群整体还原行级色带。
    """
    from PyQt6.QtGui import QImage

    from krok_helper.subtitle_render.domain.models import SubtitleStyleScheme
    from krok_helper.subtitle_render.domain.paint import (
        KaraokeColorState,
        KaraokeColors,
        PaintFill,
    )
    from krok_helper.subtitle_render.domain.timing import TimingChar, TimingTrack
    from krok_helper.subtitle_render.engine.painter import paint_frame
    from krok_helper.subtitle_render.engine.render.effects.particles import (
        clear_particle_paint_cache,
    )

    clear_particle_paint_cache()
    grad = PaintFill(
        mode="gradient_horizontal",
        start_color="#FF0000",
        end_color="#0000FF",
        gradient_stops=[(0, "#FF0000"), (100, "#0000FF")],
    )
    dark = PaintFill(mode="solid", color="#202020")
    scheme = SubtitleStyleScheme(
        karaoke_colors=KaraokeColors(
            after=KaraokeColorState(text=grad)
        )
    )
    chars = [TimingChar("詞", 1000 + i * 400) for i in range(6)]
    for char in chars:
        char.role_label = "主唱"
    track = TimingTrack(
        lines=[TimingLine(chars=chars, end_ms=1000 + 6 * 400)]
    )
    style = Style(
        font_size_px=64,
        sing_fx="twinkle",
        karaoke_anim="none",
        fx_particle_size_em=0.7,
        fx_particle_count=24,
        fx_particle_color_mode="role",
        fx_particle_role_name="主唱",
        custom_style_schemes={"主唱": scheme},
        stroke_width_px=0,
        karaoke_colors=KaraokeColors(
            before=KaraokeColorState(text=dark),
            after=KaraokeColorState(text=dark),
        ),
    )
    img = QImage(800, 450, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(0xFF101010)
    paint_frame(img, track, 2600, style)

    def half_counts(left: bool):
        red = blue = 0
        xs = range(0, img.width() // 2) if left else range(
            img.width() // 2, img.width()
        )
        for y in range(0, img.height(), 2):
            for x in xs:
                px = img.pixel(x, y)
                r, g, b = (px >> 16) & 0xFF, (px >> 8) & 0xFF, px & 0xFF
                if r > 100 and r > g + 30 and r > b + 30:
                    red += 1
                if b > 100 and b > r + 30 and b > g + 30:
                    blue += 1
        return red, blue

    left_red, left_blue = half_counts(True)
    right_red, right_blue = half_counts(False)
    # 左半 = 起点红、右半 = 终点蓝，两侧互不越界（行级一条色带）。
    assert left_red > 10 and left_blue == 0, (left_red, left_blue)
    assert right_blue > 10 and right_red == 0, (right_red, right_blue)
    clear_particle_paint_cache()


def test_entry_exit_sparkle_follows_first_char_role():
    """回归护栏（2026-10 用户实测）：联动开启时行锚点入退场跟随首字符角色。

    逐字角色不进行样式（只有歌手方案进），行锚点 burst（入退场星光）此前
    直接回落行样式 = 全局默认——主文字挂角色 A、唱字跟随正确、入退场却跟
    了全局默认。修复后行锚点取首字符的角色样式（无角色 = 行样式，语义不
    变；与指示灯/音量柱「段首行第一个角色」同款先例）。
    """
    from krok_helper.subtitle_render.domain.models import SubtitleStyleScheme
    from krok_helper.subtitle_render.domain.paint import (
        KaraokeColorState,
        KaraokeColors,
        PaintFill,
    )
    from krok_helper.subtitle_render.engine.render.effects.particles import (
        clear_particle_paint_cache,
    )
    from krok_helper.subtitle_render.engine.style.style_semantics import style_for_role

    clear_particle_paint_cache()
    role_a = SubtitleStyleScheme(
        karaoke_colors=KaraokeColors(
            after=KaraokeColorState(text=PaintFill(mode="solid", color="#40E0FF"))
        )
    )
    style = Style(
        font_size_px=64,
        sing_fx="note",
        entry_anim="sparkle",
        entry_lead_ms=600,
        exit_anim="sparkle",
        exit_fade_ms=600,
        karaoke_anim="none",
        fx_particle_color_mode="follow_after",
        fx_apply_to_entry_exit=True,
        custom_style_schemes={"A": role_a},
        karaoke_colors=KaraokeColors(
            after=KaraokeColorState(
                text=PaintFill(mode="solid", color="#FF5A6F")
            )
        ),
    )
    windows = [(1200, 1600), (1600, 2000), (2000, 2400)]
    char_styles = [style_for_role(style, "A") for _ in windows]
    bursts = plan_line_bursts(
        style, 0, 1000, 4000, 3900, windows, char_styles=char_styles
    )
    sparkle = next(b for b in bursts if b["kind"] == "sparkle")
    note = next(b for b in bursts if b["kind"] == "note")
    assert sparkle["color"] == "#40E0FF", sparkle["color"]
    assert note["color"] == "#40E0FF"
    # 联动关闭（默认）：入退场维持固定樱花粉默认档。
    off = plan_line_bursts(
        Style(
            **{
                **style.__dict__,
                "fx_apply_to_entry_exit": False,
            }
        ),
        0,
        1000,
        4000,
        3900,
        windows,
        char_styles=char_styles,
    )
    assert {
        b["color"] for b in off if b["kind"] == "sparkle"
    } == {"#FFB7C5", "#FFD7E0"}
    # 星光画在主文字前（2026-10 用户复调：背后看不清），音符在前。
    assert sparkle["front"] is True
    assert note["front"] is True
    clear_particle_paint_cache()


def test_star_particles_bias_up_and_avoid_repeat_quadrant():
    """星光族纵向分布（2026-10 用户口径）：

    - 锚点纵向带 [−0.62, +0.30]×行高：约 2/3 在字上侧，上缘只稍微溢出
      字形顶（字形顶 = −0.5），下侧紧贴行中心；
    - 连续两颗星不落同象限（左/右 × 上/下，按出生位置链式判定）。
    """
    from krok_helper.subtitle_render.engine.render.elements.horizontal.transitions import (
        fx_unit_hash,
    )
    from krok_helper.subtitle_render.engine.render.effects.particles import (
        STAR_Y_SPLIT_U,
        _star_quadrant,
        star_y_fraction,
        star_y_resample,
    )

    # 纵向带边界与上侧概率。
    assert star_y_fraction(0.0) == pytest.approx(-0.62)
    assert star_y_fraction(1.0) == pytest.approx(0.30)
    assert star_y_fraction(0.5) < 0.0  # 中位即在上侧
    # 上侧占比 ≈ 0.62/0.92 ≈ 67%。
    samples = [star_y_fraction((i + 0.5) / 200) for i in range(200)]
    above = sum(1 for v in samples if v < 0.0)
    assert 130 <= above <= 136, above

    # 连续象限链：任意种子序列不出现同象限相邻。
    for kind_seed_base in (7919, 104729):
        for seed in range(120):
            prev = -1
            for i in range(24):
                base = kind_seed_base * seed + i
                u1 = fx_unit_hash(base, 1)
                u2 = star_y_resample(
                    fx_unit_hash(base, 2),
                    fx_unit_hash(base, 6),
                    fx_unit_hash(base, 7),
                    prev,
                    (u1 - 0.5) < 0.0,
                )
                frac = star_y_fraction(u2)
                assert -0.62 - 1e-9 <= frac <= 0.30 + 1e-9
                quadrant = _star_quadrant((u1 - 0.5) < 0.0, frac < 0.0)
                assert quadrant != prev
                prev = quadrant

    # 求值器整体（多种子聚合，规避单种子抽样噪声）：出生帧 ≥60% 在上侧
    # （含象限互斥把边际从 67% 拉低的影响）、最高点不超过 −0.63×行高
    # （稍微溢出字形顶 + 少量上漂）；延迟帧含漂移后仍 ≥55% 在上。
    ups = total = 0
    lowest = 0.0
    for seed in range(30):
        burst = {
            "kind": "sparkle", "start_ms": 0, "end_ms": 1200, "count": 24,
            "seed": seed * 7919 + 13, "size_px": 50.0, "travel_px": 90.0,
            "front": True, "sweep": 0,
        }
        # sweep=0 时出生延迟 ≤130ms：t=140 全员刚出生（漂移极小≈锚点）。
        birth = burst_particles_at(burst, 140, 0.0, 0.0, 400.0, 100.0)
        assert birth
        ups += sum(1 for s in birth if s.y < 0.0)
        total += len(birth)
        lowest = min(lowest, min(s.y for s in birth))
    assert ups / total >= 0.6, ups / total
    assert lowest >= -63.0  # -0.63×行高（稍微溢出字形顶）
    mid = burst_particles_at(burst, 700, 0.0, 0.0, 400.0, 100.0)
    ups_mid = [s for s in mid if s.y < 0.0]
    assert len(ups_mid) / len(mid) >= 0.5


# ---------------------------------------------------------------------------
# 2026-10 花瓣（樱花飘落）：入场飘入 / 退场飘散 / 唱字飘动 + 双色随机档。
# ---------------------------------------------------------------------------


def test_plan_line_bursts_petal_entry_exit_sing():
    """花瓣三档规划接线（与音符同构）：错峰、计数、行程与旋钮口径。"""
    style = Style(
        entry_anim="petal",
        entry_lead_ms=600,
        exit_anim="petal",
        exit_fade_ms=600,
        sing_fx="petal",
        fx_particle_count=14,
        font_size_px=100,
        karaoke_anim="none",
    )
    bursts = plan_line_bursts(
        style, 0, 1000, 4200, 4100,
        [(1200, 1600), (1600, 2000), (2000, 3000)],
    )
    # 入场花瓣：逐字错峰（3 字 → 步距 175）+ ±80ms 确定性时间抖动，
    # 每字 3 颗拆固定樱花粉双色 2+1；位置偏移全部 box 比例（travel 恒 0）。
    entry = [b for b in bursts if b["kind"] == "petal" and b["sweep"] == 1]
    assert len(entry) == 6
    for char_index, base in enumerate((1000, 1175, 1350)):
        starts = [
            b["start_ms"] for b in entry if b["char_index"] == char_index
        ]
        assert len(starts) == 2
        assert all(abs(start - base) <= 80 for start in starts)
    assert len({b["start_ms"] for b in entry}) > 1
    assert sorted({b["count"] for b in entry}) == [1, 2]
    assert {b["color"] for b in entry} == {"#FFB7C5", "#FFD7E0"}
    assert all(b["travel_px"] == 0.0 for b in entry)
    # 退场花瓣（2026-10 分离口径）：锚点 = max(行末 4100, 显示末 4200 -
    # 1250) = 4100，标准错峰 175ms、每字完整寿命 900ms（越过显示窗
    # 终点，尾奏通道续播；旧「120 兜底 + 错峰压缩」已废除）。
    exit_petals = [b for b in bursts if b["kind"] == "petal" and b["sweep"] == -1]
    assert len(exit_petals) == 6
    for char_index, base in enumerate((4100, 4275, 4450)):
        starts = [
            b["start_ms"] for b in exit_petals if b["char_index"] == char_index
        ]
        assert len(starts) == 2
        assert all(abs(start - base) <= 80 for start in starts)
    assert len({b["start_ms"] for b in exit_petals}) > 1
    assert all(b["end_ms"] == b["start_ms"] + 900 for b in exit_petals)
    assert {b["color"] for b in exit_petals} == {"#FFB7C5", "#FFD7E0"}
    # 唱字花瓣：sweep=0、每字 max(3, 14//3)=4 颗、尺寸吃旋钮（40% 字号）。
    sing = [b for b in bursts if b["kind"] == "petal" and b["sweep"] == 0]
    assert len(sing) == 3
    assert all(b["count"] == 4 for b in sing)
    assert sing[0]["start_ms"] in (1200, 1600, 2000)
    assert sing[0]["size_px"] == pytest.approx(100 * 0.40)


def test_petal_trajectory_windows_and_downward_drift():
    """花瓣轨迹：窗口外为空、确定性、三档都单调下沉且**贴近字框**
    （box 比例寻路——星光/音符同约定；初版字号倍数行程出生过高被否）。"""
    base = {
        "kind": "petal", "anchor": "char", "char_index": 0,
        "start_ms": 0, "end_ms": 900, "count": 6, "seed": 12345,
        "size_px": 30.0, "travel_px": 0.0, "front": True,
    }
    for sweep in (1, -1, 0):
        burst = {**base, "sweep": sweep}
        assert burst_particles_at(burst, -1, 0.0, 0.0, 120.0, 100.0) == []
        assert burst_particles_at(burst, 901, 0.0, 0.0, 120.0, 100.0) == []
        early = burst_particles_at(burst, 200, 0.0, 0.0, 120.0, 100.0)
        again = burst_particles_at(burst, 200, 0.0, 0.0, 120.0, 100.0)
        assert early == again  # 纯时间函数：重放逐位一致
        assert len(early) == 6  # 出生延迟上限 110ms < 200
        late = burst_particles_at(burst, 700, 0.0, 0.0, 120.0, 100.0)
        for before, after in zip(early, late):
            assert after.y >= before.y  # 无 y 向摇摆项：单调下沉
        # 2026-10-18：透明度前 75% 保持、落到底才收；落点至少到字底
        #（0.42+ box_h），不是过字中线就消失。
        hold = burst_particles_at(burst, 600, 0.0, 0.0, 120.0, 100.0)
        assert hold and min(state.alpha for state in hold) > 0.8
        bottom = burst_particles_at(burst, 899, 0.0, 0.0, 120.0, 100.0)
        if sweep > 0:
            assert bottom and min(state.y for state in bottom) > 35.0
        if sweep < 0:
            # 飘逸随机、**终点向右**（2026-10 用户口径）：每颗粒子终点在
            # 起点右侧（幅度随机），整簇随时间右移（sin 摇摆是振荡项，
            # 逐粒子 x 非单调，按簇和判方向）。
            assert sum(s.x for s in late) > sum(s.x for s in early)
        # 贴近字框：横向 |x| ≤ 一个字宽（飘散档终点右移 ≤1.4 字宽 + 摇摆
        # ±1.5×粒子尺寸，起点最左 -0.43 字宽）、纵向在 [-0.95, +1.2]
        # box_h 内（溢出量与星光 -0.62 同量级，不再飘进行间距）。
        for states in (early, late):
            for state in states:
                assert 0.0 < state.alpha <= 1.0
                assert state.size_px > 0.0
                if sweep < 0:
                    assert -100.0 <= state.x <= 240.0
                else:
                    assert abs(state.x) <= 120.0
                assert -95.0 <= state.y <= 120.0


def test_petal_two_color_variant_split():
    """双色档：同轨迹拆两条 burst（数量对半、种子错开、各带一实色）；
    2026-10 用户口径：双色系一律实色（单独颜色双槽 / 前后实色 / 樱花粉）。"""
    from krok_helper.subtitle_render.domain.paint import (
        KaraokeColorState,
        KaraokeColors,
        PaintFill,
    )
    from krok_helper.subtitle_render.engine.style.style_semantics import solid_fill

    sakura = Style(
        sing_fx="petal",
        fx_particle_color_mode="sakura",
        fx_particle_count=14,
        font_size_px=100,
        karaoke_anim="none",
    )
    bursts = plan_line_bursts(sakura, 0, 0, 3000, 2900, [(100, 400)])
    petals = [b for b in bursts if b["kind"] == "petal"]
    assert len(petals) == 2
    assert sorted(b["count"] for b in petals) == [2, 2]  # 4 → 2+2
    assert {b["color"] for b in petals} == {"#FFB7C5", "#FFD7E0"}
    assert petals[0]["seed"] != petals[1]["seed"]
    assert all("paint" not in b for b in petals)  # 双色档不带装饰规格
    # 奇数数量：余数归第一条（唱字档数量吃旋钮：9 → 每字 3 → 2+1；
    # 入退场动画粒子走固定樱花粉默认档、与这里独立）。
    odd = Style(
        sing_fx="petal",
        fx_particle_color_mode="sakura",
        fx_particle_count=9,
        font_size_px=100,
        karaoke_anim="none",
    )
    odd_bursts = plan_line_bursts(odd, 0, 0, 3000, 2900, [(100, 400)])
    entry_pair = [b for b in odd_bursts if b["kind"] == "petal"]
    assert sorted(b["count"] for b in entry_pair) == [1, 2]

    # 跟随字体·前后实色：两实色 = 行配色走字前/后「主文字」实色，
    # 不携带装饰规格（渐变折停止色平均）。
    mix = Style(
        sing_fx="petal",
        fx_particle_color_mode="follow_mix",
        karaoke_colors=_karaoke_matrix("#123456", "#ABCDEF"),
        font_size_px=100,
        karaoke_anim="none",
    )
    mix_bursts = plan_line_bursts(mix, 0, 0, 3000, 2900, [(100, 400)])
    mix_petals = [b for b in mix_bursts if b["kind"] == "petal"]
    assert {b["color"] for b in mix_petals} == {"#123456", "#ABCDEF"}
    assert all("paint" not in b for b in mix_petals)
    # 渐变走字后态：仅实色层现在携带**填充渐变规格**（2026-10-18 修复：
    # 仅实色 = 仅文字配色，不是退化成平均实色）；burst.color 仍保留平均
    # 实色作旧渲染端回退。
    gradient = PaintFill(
        mode="gradient_horizontal",
        start_color="#FF0000",
        end_color="#0000FF",
        gradient_stops=[(0, "#FF0000"), (50, "#00FF00"), (100, "#0000FF")],
    )
    grad_style = Style(
        sing_fx="petal",
        fx_particle_color_mode="follow_mix",
        fx_particle_color="#00FF00",
        font_size_px=100,
        karaoke_anim="none",
        karaoke_colors=KaraokeColors(
            before=KaraokeColorState(text=solid_fill("#2468AC")),
            after=KaraokeColorState(text=gradient),
        ),
    )
    grad_bursts = plan_line_bursts(grad_style, 0, 0, 3000, 2900, [(100, 400)])
    grad_petals = [b for b in grad_bursts if b["kind"] == "petal"]
    assert {b["color"] for b in grad_petals} == {"#2468AC", "#555555"}
    before_variant = [b for b in grad_petals if b["color"] == "#2468AC"]
    after_variant = [b for b in grad_petals if b["color"] == "#555555"]
    assert all("paint" not in b for b in before_variant)
    assert after_variant[0]["paint"]["fill"]["mode"] == "gradient_horizontal"
    assert after_variant[0]["paint"]["stroke_width_px"] == 0.0
    assert "decor" not in after_variant[0]["paint"]

    # 单独颜色档·双色槽：恒双色随机混发——颜色二默认白色（白色即颜色
    # 本身，无「未设置」态，2026-10 用户口径：必须设置双色）。
    single = Style(
        sing_fx="petal",
        fx_particle_color="#40E0FF",
        font_size_px=100,
        karaoke_anim="none",
    )
    single_bursts = plan_line_bursts(single, 0, 0, 3000, 2900, [(100, 400)])
    petals = [b for b in single_bursts if b["kind"] == "petal"]
    assert len(petals) == 2
    assert {b["color"] for b in petals} == {"#40E0FF", "#FFFFFF"}
    # 设置颜色二 → 双色随机混发。
    dual = Style(
        sing_fx="petal",
        fx_particle_color="#40E0FF",
        fx_particle_color2="#FF69B4",
        font_size_px=100,
        karaoke_anim="none",
    )
    dual_bursts = plan_line_bursts(dual, 0, 0, 3000, 2900, [(100, 400)])
    dual_petals = [b for b in dual_bursts if b["kind"] == "petal"]
    assert len(dual_petals) == 2
    assert {b["color"] for b in dual_petals} == {"#40E0FF", "#FF69B4"}
    # 两槽同色：折单 burst（观感等价的双色折叠，省一半 burst）。
    same = Style(
        sing_fx="petal",
        fx_particle_color="#40E0FF",
        fx_particle_color2="#40E0FF",
        font_size_px=100,
        karaoke_anim="none",
    )
    same_bursts = plan_line_bursts(same, 0, 0, 3000, 2900, [(100, 400)])
    same_petals = [b for b in same_bursts if b["kind"] == "petal"]
    assert len(same_petals) == 1
    assert same_petals[0]["count"] == 4 and same_petals[0]["color"] == "#40E0FF"


def test_petal_role_mode_pair_but_other_kinds_stay_single():
    """复用配色方案：花瓣取来源走字前/后双色；其余粒子保持走字后单色。"""
    from krok_helper.subtitle_render.domain.models import SubtitleStyleScheme

    scheme = SubtitleStyleScheme(
        karaoke_colors=_karaoke_matrix("#000000", "#EE7700")
    )
    style = Style(
        fx_particle_color="#00FF00",
        fx_particle_color_mode="role",
        fx_particle_role_name="主唱",
        custom_style_schemes={"主唱": scheme},
        font_size_px=100,
        karaoke_anim="none",
    )
    from dataclasses import replace as _replace

    petal_style = _replace(style, sing_fx="petal")
    petal_bursts = plan_line_bursts(petal_style, 0, 0, 3000, 2900, [(100, 400)])
    petal_pair = [b for b in petal_bursts if b["kind"] == "petal"]
    assert {b["color"] for b in petal_pair} == {"#000000", "#EE7700"}
    # 花瓣的 role 档 = 来源走字前/后实色双色（不带装饰规格，
    # 与 follow_mix「前后实色」同口径）。
    assert all("paint" not in b for b in petal_pair)
    # 音符在同一 role 模式下保持单色（走字后）——不改既有观感。
    note_style = _replace(style, sing_fx="note")
    note_bursts = plan_line_bursts(note_style, 0, 0, 3000, 2900, [(100, 400)])
    notes = [b for b in note_bursts if b["kind"] == "note"]
    assert len(notes) == 1 and notes[0]["color"] == "#EE7700"


def test_petal_sprite_contract():
    """花瓣 sprite：单轮廓、闭合、em 空间内；kind → sprite 映射正确。"""
    petal = FX_SPRITES["petal"]["path_commands"]
    assert petal[0][0] == "M" and petal[-1][0] == "Z"
    assert sum(1 for c in petal if c[0] == "M") == 1  # 单轮廓（无自交叠）
    for command in petal:
        assert command[0] in {"M", "L", "C", "Q", "Z"}
    points = [
        (float(command[i]), float(command[i + 1]))
        for command in petal
        for i in range(1, len(command) - 1, 2)
    ]
    assert max(abs(x) for x, _y in points) <= 500.0
    assert max(abs(y) for _x, y in points) <= 500.0
    # 修长比例（画法参考：瓣长 ≈ 1.3~1.5 倍瓣宽；初版过宽被否）。
    width = 2.0 * max(abs(x) for x, _y in points)
    span_y = max(y for _x, y in points) - min(y for _x, y in points)
    assert span_y / width >= 1.3
    assert sprite_for_kind("petal") == "petal"


def test_petal_gpu_support_and_override_roundtrip():
    """花瓣档位不触发 GPU 整帧回退；逐行覆盖可往返序列化。"""
    track = type("Track", (), {"lines": []})()
    style = Style(
        entry_anim="petal",
        exit_anim="petal",
        sing_fx="petal",
        karaoke_anim="inherit",
        reverse_karaoke_anim="inherit",
    )
    assert gpu_unsupported_features(track, style) == ()
    data = line_animation_override_to_dict(
        LineAnimationOverride(entry_anim="petal", exit_anim="petal", sing_fx="petal")
    )
    restored = line_animation_override_from_dict(data)
    assert restored is not None
    assert restored.entry_anim == "petal"
    assert restored.exit_anim == "petal"
    assert restored.sing_fx == "petal"


def test_painter_petal_smoke(qapp):
    """CPU painter 花瓣三档冒烟：char 锚点轨迹不抛异常（飘入/飘动各一帧）。"""
    from PyQt6.QtGui import QImage

    from krok_helper.subtitle_render.domain.timing import TimingChar, TimingTrack
    from krok_helper.subtitle_render.engine.painter import paint_frame

    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[
                    TimingChar(text="あ", start_ms=1000),
                    TimingChar(text="い", start_ms=1600),
                ],
                end_ms=2200,
            )
        ]
    )
    sing = Style(
        sing_fx="petal",
        fx_particle_color_mode="sakura",
        karaoke_anim="utopia",
        fx_particle_size_em=0.6,
        fx_particle_count=8,
    )
    img = QImage(800, 450, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(0xFF101010)
    paint_frame(img, track, 1300, sing)  # 「あ」唱到一半：花瓣飘动
    entry = Style(
        entry_anim="petal",
        entry_lead_ms=600,
        fx_particle_color_mode="follow_mix",
        karaoke_anim="utopia",
    )
    img2 = QImage(800, 450, QImage.Format.Format_ARGB32_Premultiplied)
    img2.fill(0xFF101010)
    paint_frame(img2, track, 1100, entry)  # 入场窗口内：花瓣飘入


def test_d2d_geo_transition_gates_cover_particle_anims():
    """C++ 逐字过渡总闸名单必须覆盖 Python 侧全部粒子档（2026-10 花瓣踩坑）。

    GPU 的逐字渐显/渐隐链路：isGeoEntry/isGeoExit → hasCharacterTransition
    / activeCharacterTransition → characterAnimationAt → geoCharState。档位
    漏进总闸名单时整条链路不触发（求值器加了也没用），出入场整行弹出。
    本测试按源码文本对齐 timing.py 的集合与两个 native 名单（竖排禁用名单
    同口径），改 Python 档位时若忘同步 C++ 名单即在此失败。
    """
    import re
    from pathlib import Path

    from krok_helper.subtitle_render.domain.timing import (
        ENTRY_PARTICLE_ANIMS,
        EXIT_PARTICLE_ANIMS,
    )

    root = Path(__file__).resolve().parents[1]
    render_source = (
        root
        / "native/subtitle_renderer/src/backends/direct2d/d2d_backend_render.cpp"
    ).read_text(encoding="utf-8")
    projection_source = (
        root
        / "native/subtitle_renderer/src/backends/qt/gpu_scene_projection.cpp"
    ).read_text(encoding="utf-8")

    def _animation_names(source: str, anchor: str) -> set[str]:
        start = source.index(anchor)
        end = source.index("};", start)
        return set(
            re.findall(r'animation == "([a-z_]+)"', source[start:end])
        )

    entry_names = _animation_names(render_source, "const auto isGeoEntry = []")
    exit_names = _animation_names(render_source, "const auto isGeoExit = []")
    assert ENTRY_PARTICLE_ANIMS <= entry_names, sorted(
        ENTRY_PARTICLE_ANIMS - entry_names
    )
    assert EXIT_PARTICLE_ANIMS <= exit_names, sorted(
        EXIT_PARTICLE_ANIMS - exit_names
    )

    vertical_start = projection_source.index(
        "const auto verticalCharacterAnimation = [&]"
    )
    vertical_end = projection_source.index("};", vertical_start)
    vertical_names = set(
        re.findall(
            r'animation == QStringLiteral\("([a-z_]+)"\)',
            projection_source[vertical_start:vertical_end],
        )
    )
    assert (ENTRY_PARTICLE_ANIMS | EXIT_PARTICLE_ANIMS) <= vertical_names, (
        sorted((ENTRY_PARTICLE_ANIMS | EXIT_PARTICLE_ANIMS) - vertical_names)
    )


# ---------------------------------------------------------------------------
# 2026-10 三期：取色层级（仅实色 / +描边 / +装饰 / 全有，偏好记忆）
# ---------------------------------------------------------------------------


def test_particle_color_layers_trim_source_decor():
    """取色层级四档（2026-10 用户口径）：solid（默认）/ +描边（不加装饰，
    描边栈按方案）/ +装饰（不加描边，仅装饰层）/ 全有（描边栈 + 装饰）。"""
    from krok_helper.subtitle_render.domain.paint import (
        KaraokeColorState,
        KaraokeColors,
    )
    from krok_helper.subtitle_render.engine.style.style_semantics import solid_fill

    def _style(layers: str, **extra) -> Style:
        base = dict(
            font_size_px=100,
            sing_fx="twinkle",
            fx_particle_size_em=0.5,  # 粒子 50px → 缩放 0.5
            fx_particle_color_mode="follow_after",
            fx_particle_color_layers=layers,
            karaoke_anim="none",
            stroke_width_px=8,
            stroke2_enabled=True,
            stroke2_width_px=4,
            shadow_offset_x=6,
            shadow_offset_y=4,
            karaoke_colors=KaraokeColors(
                after=KaraokeColorState(text=solid_fill("#FF5A6F"))
            ),
        )
        base.update(extra)
        return Style(**base)

    def _burst(layers: str, **extra) -> dict:
        bursts = plan_line_bursts(
            _style(layers, **extra), 0, 0, 1000, 900, [(0, 400)]
        )
        return next(b for b in bursts if b["kind"] == "twinkle")

    # 默认 = 仅实色：不携带 paint 规格（纯色剪影）。
    assert Style().fx_particle_color_layers == "solid"
    burst = _burst("solid")
    assert "paint" not in burst
    assert burst["color"] == "#FF5A6F"
    # +描边：按方案描边栈（方案启用二重描边 → 一起加），不加装饰。
    spec = _burst("stroke")["paint"]
    assert spec["stroke_width_px"] == pytest.approx(4.0)  # 8 × 0.5
    assert spec["stroke2_width_px"] == pytest.approx(2.0)  # 4 × 0.5
    assert "decor" not in spec
    # 方案未启用二重描边：只加单描边（仍按方案）。
    spec = _burst("stroke", stroke2_enabled=False)["paint"]
    assert spec["stroke_width_px"] == pytest.approx(4.0)
    assert spec["stroke2_width_px"] == 0.0
    # +装饰：不加描边、仅装饰层（默认方案 decoration_kind=shadow → 偏移剪影）。
    spec = _burst("decor")["paint"]
    assert spec["stroke_width_px"] == 0.0
    assert spec["stroke2_width_px"] == 0.0
    assert spec["decor"]["kind"] == "shadow"
    assert spec["decor"]["offset_x_px"] == pytest.approx(3.0)  # 6 × 0.5
    assert spec["decor"]["offset_y_px"] == pytest.approx(2.0)  # 4 × 0.5
    # 全有：描边栈 + 装饰层。
    spec = _burst("all")["paint"]
    assert spec["stroke_width_px"] == pytest.approx(4.0)
    assert spec["stroke2_width_px"] == pytest.approx(2.0)
    assert spec["decor"]["kind"] == "shadow"
    # 装饰随角色方案的 decoration_kind：glow → 半径/浓度（走字后半径）。
    glow_spec = _burst(
        "all",
        decoration_kind="glow",
        glow_after_radius_px=12,
        glow_concentration_level=1,
    )["paint"]
    assert glow_spec["decor"]["kind"] == "glow"
    assert glow_spec["decor"]["radius_px"] == pytest.approx(6.0)  # 12 × 0.5
    assert glow_spec["decor"]["concentration_level"] == 1
    # 走字前档取 before 半径（每态各自的发光半径）。
    before_glow = _burst(
        "decor",
        fx_particle_color_mode="follow_before",
        decoration_kind="glow",
        glow_before_radius_px=10,
        glow_after_radius_px=30,
    )["paint"]
    assert before_glow["decor"]["radius_px"] == pytest.approx(5.0)
    # decoration_kind=none：装饰档不产出装饰（退化为纯色 / 描边栈）。
    none_spec = _burst("decor", decoration_kind="none")["paint"]
    assert "decor" not in none_spec
    assert none_spec["stroke_width_px"] == 0.0
    none_all = _burst("all", decoration_kind="none")["paint"]
    assert "decor" not in none_all
    assert none_all["stroke_width_px"] == pytest.approx(4.0)


def test_particle_color_layers_apply_to_dual_variants():
    """前后各一 / 花瓣复用：双变体各带该态的裁剪规格（层级统一生效）。"""
    from krok_helper.subtitle_render.domain.models import SubtitleStyleScheme

    mix = Style(
        font_size_px=100,
        sing_fx="petal",
        fx_particle_color_mode="follow_mix",
        fx_particle_color_layers="all",
        karaoke_anim="none",
        stroke_width_px=8,
        stroke2_enabled=True,
        stroke2_width_px=4,
        karaoke_colors=_karaoke_matrix("#123456", "#ABCDEF"),
    )
    petals = [
        b
        for b in plan_line_bursts(mix, 0, 0, 3000, 2900, [(100, 400)])
        if b["kind"] == "petal"
    ]
    assert len(petals) == 2
    assert all("paint" in b for b in petals)
    assert {b["paint"]["fill"]["color"] for b in petals} == {
        "#123456", "#ABCDEF",
    }
    assert all(b["paint"]["stroke_width_px"] > 0.0 for b in petals)
    assert all(b["paint"]["decor"]["kind"] == "shadow" for b in petals)
    # 默认仅实色档：同一配置不带规格（旧观感）。
    plain = Style(
        font_size_px=100,
        sing_fx="petal",
        fx_particle_color_mode="follow_mix",
        karaoke_anim="none",
        karaoke_colors=_karaoke_matrix("#123456", "#ABCDEF"),
    )
    plain_petals = [
        b
        for b in plan_line_bursts(plain, 0, 0, 3000, 2900, [(100, 400)])
        if b["kind"] == "petal"
    ]
    assert all("paint" not in b for b in plain_petals)
    # 花瓣复用（role）同口径：来源态双变体带完整层级规格。
    scheme = SubtitleStyleScheme(
        karaoke_colors=_karaoke_matrix("#000000", "#EE7700")
    )
    role_style = Style(
        font_size_px=100,
        sing_fx="petal",
        fx_particle_color_mode="role",
        fx_particle_role_name="主唱",
        fx_particle_color_layers="all",
        custom_style_schemes={"主唱": scheme},
        karaoke_anim="none",
        stroke_width_px=8,
    )
    role_petals = [
        b
        for b in plan_line_bursts(role_style, 0, 0, 3000, 2900, [(100, 400)])
        if b["kind"] == "petal"
    ]
    assert all("paint" in b for b in role_petals)
    assert all("decor" in b["paint"] for b in role_petals)


def test_painter_particle_shadow_layer_smoke(qapp):
    """CPU painter：装饰层（全有档）两种 kind 绘制不抛异常。

    阴影（decoration_kind=shadow，常量偏移剪影）与发光（=glow，多级描边
    弥散晕）都走一遍。
    """
    from PyQt6.QtGui import QImage

    from krok_helper.subtitle_render.domain.timing import TimingChar, TimingTrack
    from krok_helper.subtitle_render.engine.painter import paint_frame

    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[TimingChar(text="あ", start_ms=1000)],
                end_ms=1600,
            )
        ]
    )
    style = Style(
        sing_fx="twinkle",
        karaoke_anim="utopia",
        fx_particle_size_em=0.8,
        fx_particle_count=16,
        fx_particle_color_mode="follow_after",
        fx_particle_color_layers="all",
        stroke_width_px=6,
        stroke2_enabled=True,
        stroke2_width_px=3,
        shadow_offset_x=6,
        shadow_offset_y=6,
        karaoke_colors=_karaoke_matrix("#40E0FF", "#FF5A6F"),
    )
    img = QImage(800, 450, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(0xFF101010)
    paint_frame(img, track, 1300, style)
    # 同档位换发光装饰：多级描边弥散晕路径同样可画（无异常）。
    from dataclasses import replace as _replace

    glow_style = _replace(
        style, decoration_kind="glow", glow_after_radius_px=12,
        glow_concentration_level=1,
    )
    img2 = QImage(800, 450, QImage.Format.Format_ARGB32_Premultiplied)
    img2.fill(0xFF101010)
    paint_frame(img2, track, 1300, glow_style)


# ---------------------------------------------------------------------------
# 2026-10 雪花（❄ 枝晶 + 匀速慢落 + ruby 锚出生 + 尺寸递减）：
# 入场飘入 / 退场飘散 / 唱字飘动。
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("snow_kind", ["snow", "snow_solid"])
def test_plan_line_bursts_snow_entry_exit_sing(snow_kind):
    """雪花两档（·镂空/·实心）规划同源：出入场整行近似同刻 + 逐字
    ±80ms 时间抖动 + 早现晚收/同步晚收时序 + 雪色 + 逐字唱字；两档只差
    kind（决定 sprite）。"""
    style = Style(
        entry_anim=snow_kind,
        entry_lead_ms=600,
        exit_anim=snow_kind,
        exit_fade_ms=600,
        sing_fx=snow_kind,
        fx_particle_count=14,
        font_size_px=100,
        karaoke_anim="none",
    )
    bursts = plan_line_bursts(
        style, 0, 1000, 4200, 4100,
        [(1200, 1600), (1600, 2000), (2000, 3000)],
    )
    # 入场雪花：整行近似同刻（无逐字等差错峰，但每字 ±80ms 抖动），
    # 提前 350ms 起飘、整段落 1800ms（慢雪；旧 1050ms 被用户否为“太快”）。
    entry = [b for b in bursts if b["kind"] == snow_kind and b["sweep"] == 1]
    assert len(entry) == 6
    assert all(abs(b["start_ms"] - 650) <= 80 for b in entry)
    assert len({b["start_ms"] for b in entry}) > 1
    assert {b["end_ms"] for b in entry} == {2450}  # 650 + 1800
    assert sorted({b["count"] for b in entry}) == [1, 2]  # 拆雪色双色
    assert {b["color"] for b in entry} == {"#F2F8FF", "#BFDFFF"}
    assert all(b["travel_px"] == 0.0 for b in entry)
    # 退场雪花：落窗同样 1800ms，从显示末前 1.5s 起飘（不再锚行末只剩
    # 400ms），逐字 ±80ms 抖动；终点晚于显示末 300ms。
    exit_snow = [b for b in bursts if b["kind"] == snow_kind and b["sweep"] == -1]
    assert len(exit_snow) == 6
    assert all(abs(b["start_ms"] - 2700) <= 80 for b in exit_snow)
    assert len({b["start_ms"] for b in exit_snow}) > 1
    assert {b["end_ms"] for b in exit_snow} == {4500}  # 4200 + 300
    assert {b["color"] for b in exit_snow} == {"#F2F8FF", "#BFDFFF"}
    # 唱字雪花：逐字发射（唱字窗起点）、每字 4 颗、尺寸吃旋钮。
    sing = [b for b in bursts if b["kind"] == snow_kind and b["sweep"] == 0]
    assert len(sing) == 3
    assert all(b["count"] == 4 for b in sing)
    assert sing[0]["size_px"] == pytest.approx(100 * 0.40)
    assert sing[0]["end_ms"] - sing[0]["start_ms"] == 1100


@pytest.mark.parametrize("snow_kind", ["snow", "snow_solid"])
def test_snow_default_colors_differ_from_other_kinds(snow_kind):
    """默认双色按 kind：雪花两档=雪白+冰蓝；花瓣与音符等维持樱花粉。"""
    def _sing_colors(kind: str) -> set[str]:
        bursts = plan_line_bursts(
            Style(
                sing_fx=kind,
                fx_particle_color_mode="sakura",
                fx_particle_count=12,
                font_size_px=100,
                karaoke_anim="none",
            ),
            0, 0, 3000, 2900, [(100, 400)],
        )
        return {b["color"] for b in bursts if b["kind"] == kind}

    assert _sing_colors(snow_kind) == {"#F2F8FF", "#BFDFFF"}
    assert _sing_colors("petal") == {"#FFB7C5", "#FFD7E0"}
    assert _sing_colors("twinkle") == {"#FFB7C5", "#FFD7E0"}


@pytest.mark.parametrize("snow_kind", ["snow", "snow_solid"])
def test_snow_trajectory_windows_and_bounds(snow_kind):
    """雪花两档轨迹同源：窗口外空、确定性、匀速单调下沉、ruby 锚出生
    （高于字形顶）、尺寸随播放递减、飘散终点向右（同花瓣口径）。"""
    base = {
        "kind": snow_kind, "anchor": "char", "char_index": 0,
        "start_ms": 0, "end_ms": 1800, "count": 6, "seed": 54321,
        "size_px": 30.0, "travel_px": 0.0, "front": True,
    }
    for sweep in (1, -1, 0):
        burst = {**base, "sweep": sweep}
        assert burst_particles_at(burst, -1, 0.0, 0.0, 120.0, 100.0) == []
        assert burst_particles_at(burst, 1801, 0.0, 0.0, 120.0, 100.0) == []
        early = burst_particles_at(burst, 250, 0.0, 0.0, 120.0, 100.0)
        assert early == burst_particles_at(
            burst, 250, 0.0, 0.0, 120.0, 100.0
        )  # 纯时间函数：重放逐位一致
        assert len(early) == 6  # 出生延迟上限 160ms < 250
        late = burst_particles_at(burst, 800, 0.0, 0.0, 120.0, 100.0)
        for before, after in zip(early, late):
            assert after.y >= before.y  # 无 y 向摇摆项：匀速单调下沉
            # 尺寸随播放由大变小（2026-10 用户口径：近大远小）。
            assert after.size_px < before.size_px
        if sweep < 0:
            # 飘散终点向右：整簇随时间右移（摇摆是振荡项，按簇和判向）。
            assert sum(s.x for s in late) > sum(s.x for s in early)
        # ruby 锚出生：三档（含退场——雪统一从天上落到地上，2026-10
        # 用户口径：不考虑与字面的连续性）早期整簇都在字形顶之上
        #（-0.5 box_h；退场行程更长，阈值放宽到 -0.45）。
        top = burst_particles_at(burst, 150, 0.0, 0.0, 120.0, 100.0)
        assert top and max(s.y for s in top) < -45.0
        for states in (early, late):
            for state in states:
                assert 0.0 < state.alpha <= 1.0
                assert state.size_px > 0.0
                if sweep < 0:
                    assert -80.0 <= state.x <= 200.0
                else:
                    assert abs(state.x) <= 140.0  # 字框 + 宽幅摇摆余量
                assert -95.0 <= state.y <= 120.0
        if sweep > 0:
            # 飘入落到**字底或更低**：末期整簇已沉到字底附近（慢雪，
            # 1800ms 落程；不再过字中线就消失）。
            bottom = burst_particles_at(
                burst, 1790, 0.0, 0.0, 120.0, 100.0
            )
            assert bottom and min(s.y for s in bottom) > 40.0
        # 透明度前 78% 保持：1400ms 时（p≈0.8）仍清晰可见。
        hold = burst_particles_at(burst, 1400, 0.0, 0.0, 120.0, 100.0)
        assert hold and min(s.alpha for s in hold) > 0.5


def _sprite_loops(commands):
    """把 sprite path_commands 拆成闭合环（每个 M 起一环）。"""
    loops = []
    for command in commands:
        if command[0] == "M":
            loops.append([(float(command[1]), float(command[2]))])
        elif command[0] == "L":
            loops[-1].append((float(command[1]), float(command[2])))
    return loops


def _sprite_loop_area(points):
    """鞋带公式（逆时针为正）。"""
    total = 0.0
    for i in range(len(points)):
        x0, y0 = points[i]
        x1, y1 = points[(i + 1) % len(points)]
        total += x0 * y1 - x1 * y0
    return total * 0.5


def _sprite_loop_contains(point, polygon):
    """射线交叉计数：点是否在（可能凹的）简单多边形内。"""
    x, y = point
    inside = False
    for i in range(len(polygon)):
        x0, y0 = polygon[i]
        x1, y1 = polygon[(i + 1) % len(polygon)]
        if (y0 > y) != (y1 > y) and x < x0 + (y - y0) * (x1 - x0) / (y1 - y0):
            inside = not inside
    return inside


def _sprite_loop_intersections(loops):
    """严格自交叠/环间交叠计数（1e-9 容差；共享端点与共线贴边不误报）。"""

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    def sign(value):
        return 0 if abs(value) < 1e-9 else (1 if value > 0 else -1)

    segments = [
        (loop[i], loop[(i + 1) % len(loop)])
        for loop in loops
        for i in range(len(loop))
    ]
    hits = 0
    for i in range(len(segments)):
        p1, p2 = segments[i]
        for j in range(i + 1, len(segments)):
            p3, p4 = segments[j]
            if (
                sign(cross(p3, p4, p1)) * sign(cross(p3, p4, p2)) < 0
                and sign(cross(p1, p2, p3)) * sign(cross(p1, p2, p4)) < 0
            ):
                hits += 1
    return hits


def test_union_outline_overlapping_squares():
    """并集例程：重叠方块 → 单环，L 形顶点、面积精确、无自交叠。"""
    from krok_helper.subtitle_render.engine.render.effects.particles import (
        _union_outline,
    )

    def square(x0, y0, x1, y1):
        return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]

    loops = _union_outline(
        [square(0.0, 0.0, 10.0, 10.0), square(5.0, 5.0, 15.0, 15.0)]
    )
    assert len(loops) == 1
    assert len(loops[0]) == 8  # 4+4 顶点，重叠角两交点
    assert abs(_sprite_loop_area(loops[0]) - 175.0) < 1e-6
    assert _sprite_loop_intersections(loops) == 0


def test_union_outline_frame_gives_opposite_wound_hole():
    """并集例程：四边条围成回字形 → 外环 CCW + 洞环 CW（even-odd 与 winding
    都呈回字形，与 ring sprite 同款约定）。"""
    from krok_helper.subtitle_render.engine.render.effects.particles import (
        _union_outline,
    )

    loops = _union_outline(
        [
            [(-8.0, 6.0), (8.0, 6.0), (8.0, 10.0), (-8.0, 10.0)],
            [(-8.0, -10.0), (8.0, -10.0), (8.0, -6.0), (-8.0, -6.0)],
            [(-10.0, -8.0), (-6.0, -8.0), (-6.0, 8.0), (-10.0, 8.0)],
            [(6.0, -8.0), (10.0, -8.0), (10.0, 8.0), (6.0, 8.0)],
        ]
    )
    assert len(loops) == 2
    areas = sorted(_sprite_loop_area(loop) for loop in loops)
    # 洞 12×12；外环 20×20 减去四角 2×2 的阶梯缺口（刻意错位，避免共线重边）。
    assert abs(areas[0] + 144.0) < 1e-6  # 洞环 CW
    assert abs(areas[1] - 384.0) < 1e-6  # 外环 CCW
    assert _sprite_loop_intersections(loops) == 0


def test_snow_sprite_contract():
    """雪花 sprite：letsdrawthat 教程画法——空心中心六边形 + 六条 60° 尖主臂
    + 内层 12 枝两两触接成六角星 + 外层每臂一对箭羽枝。并集轮廓 = 1 外环
    + 6 窗洞 + 1 中心洞，全直线段、无自交叠；even-odd 与 winding 两填充
    规则结果一致（Python painter 与 C++ D2D 后端各用一种）。"""
    snow = FX_SPRITES["snow"]["path_commands"]
    assert snow[0][0] == "M" and snow[-1][0] == "Z"
    for command in snow:
        assert command[0] in {"M", "L", "Z"}
    loops = _sprite_loops(snow)
    assert len(loops) == 8
    areas = [_sprite_loop_area(loop) for loop in loops]
    assert sum(1 for area in areas if area > 0.0) == 1  # 外环 CCW
    assert sum(1 for area in areas if area < 0.0) == 7  # 6 窗洞 + 1 中心洞 CW
    outer_index = max(range(len(loops)), key=lambda i: abs(areas[i]))
    outer = loops[outer_index]
    holes = [loop for i, loop in enumerate(loops) if i != outer_index]
    for hole in holes:
        assert all(_sprite_loop_contains(point, outer) for point in hole)
    assert _sprite_loop_intersections(loops) == 0

    radii = [math.hypot(x, y) for loop in loops for x, y in loop]
    assert max(radii) == pytest.approx(470.0, abs=1e-6)  # 臂尖（包络 ≤ 500）
    assert sum(1 for r in radii if abs(r - 93.5) < 1e-6) == 6  # 中心洞六边形顶点
    window_corners = [r for r in radii if 100.0 < r < 110.0]
    assert len(window_corners) == 12  # 6 窗洞 × 2 内角（六边形外沿与臂缘交点）

    # 六折对称：外环旋转 60° 后与原顶点集合重合。
    cos60 = math.cos(math.pi / 3.0)
    sin60 = math.sin(math.pi / 3.0)
    for x, y in outer:
        rotated = (x * cos60 - y * sin60, x * sin60 + y * cos60)
        nearest = min(
            math.hypot(rotated[0] - px, rotated[1] - py) for px, py in outer
        )
        assert nearest < 1e-6

    # 双填充规则一致：even-odd 奇偶 vs winding 代数和，网格逐点同结果。
    for gx in range(-36, 37, 3):
        for gy in range(-36, 37, 3):
            point = (gx * 13.0, gy * 13.0)
            parity = sum(1 for loop in loops if _sprite_loop_contains(point, loop))
            winding = sum(
                (1 if _sprite_loop_area(loop) > 0.0 else -1)
                for loop in loops
                if _sprite_loop_contains(point, loop)
            )
            assert (parity % 2 == 1) == (winding != 0), point

    # 代表性点：中心洞/窗洞/外部为空，臂、星环、外层枝、臂尖为实。
    def filled(point):
        return sum(1 for loop in loops if _sprite_loop_contains(point, loop)) % 2 == 1

    assert not filled((0.0, 0.0))
    assert not filled((110.0, 60.0))
    assert not filled((0.0, 500.0))
    assert filled((250.0, 0.0))
    assert filled((450.0, 0.0))
    assert filled((0.0, 290.0))
    assert filled((380.0, 130.0))
    assert sprite_for_kind("snow") == "snow"


def test_snow_solid_sprite_contract():
    """·实心档 sprite = **0890458d 时代的 ❄ 枝晶星形**（2026-10-07 用户
    口径指定保留）：单轮廓 24 顶点、三档半径（主臂尖 470 / 侧枝尖 250 /
    臂间谷 85）、六重对称、无自交叠；与·镂空档是两套不同外形（差分=
    整套形状，不是洞的有无）。"""
    solid = FX_SPRITES["snow_solid"]["path_commands"]
    assert solid[0][0] == "M" and solid[-1][0] == "Z"
    for command in solid:
        assert command[0] in {"M", "L", "Z"}
    assert sum(1 for c in solid if c[0] == "M") == 1
    lines = [c for c in solid if c[0] == "L"]
    assert len(lines) == 23  # 6 臂 × (主臂尖+侧枝+谷+侧枝) - 1 个 M
    points = [
        (float(c[i]), float(c[i + 1]))
        for c in solid
        for i in range(1, len(c) - 1, 2)
    ]
    assert len(points) == 24
    radii = sorted({round(math.hypot(x, y), 3) for x, y in points})
    assert radii == [85.0, 250.0, 470.0]  # 臂间谷/侧枝尖/主臂尖
    solid_loops = _sprite_loops(solid)
    assert len(solid_loops) == 1  # 单轮廓
    assert _sprite_loop_intersections(solid_loops) == 0
    # 中心实心（ray-cast 在多边形内）。
    x, y = 0.0, 0.0
    inside = False
    loop = solid_loops[0]
    for i in range(len(loop)):
        x1, y1 = loop[i]
        x2, y2 = loop[(i + 1) % len(loop)]
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            inside = not inside
    assert inside
    # 六折对称：旋转 60° 后与原顶点集合重合。
    cos60 = math.cos(math.pi / 3.0)
    sin60 = math.sin(math.pi / 3.0)
    for px, py in loop:
        rotated = (px * cos60 - py * sin60, px * sin60 + py * cos60)
        nearest = min(math.hypot(rotated[0] - qx, rotated[1] - qy) for qx, qy in loop)
        assert nearest < 1e-6
    # 与镂空档是两套形状：顶点集合不同（差分=整套外形）。
    hollow = FX_SPRITES["snow"]["path_commands"]
    hollow_points = {
        (round(float(c[i]), 3), round(float(c[i + 1]), 3))
        for c in hollow
        for i in range(1, len(c) - 1, 2)
    }
    solid_points = {(round(x, 3), round(y, 3)) for x, y in loop}
    assert solid_points != hollow_points
    assert sprite_for_kind("snow_solid") == "snow_solid"


def test_snow_solid_and_hollow_plan_identically():
    """·实心/·镂空两档除 kind（决定 sprite）外规划完全一致：同一轨迹、
    配色与时序，差分只在 sprite。"""
    def plan(kind: str):
        style = Style(
            entry_anim=kind, entry_lead_ms=600, exit_anim=kind,
            exit_fade_ms=600, sing_fx=kind, fx_particle_count=14,
            font_size_px=100, karaoke_anim="none",
        )
        return plan_line_bursts(
            style, 0, 1000, 4200, 4100,
            [(1200, 1600), (1600, 2000), (2000, 3000)],
        )

    hollow = plan("snow")
    solid = plan("snow_solid")
    assert len(hollow) == len(solid) > 0
    for one, other in zip(hollow, solid):
        assert (one["kind"], other["kind"]) == ("snow", "snow_solid")
        assert {k: v for k, v in one.items() if k != "kind"} == {
            k: v for k, v in other.items() if k != "kind"
        }


@pytest.mark.parametrize("snow_kind", ["snow", "snow_solid"])
def test_snow_gpu_support_and_override_roundtrip(snow_kind):
    """雪花两档都不触发 GPU 整帧回退；逐行覆盖可往返序列化。"""
    track = type("Track", (), {"lines": []})()
    style = Style(
        entry_anim=snow_kind,
        exit_anim=snow_kind,
        sing_fx=snow_kind,
        karaoke_anim="inherit",
        reverse_karaoke_anim="inherit",
    )
    assert gpu_unsupported_features(track, style) == ()
    data = line_animation_override_to_dict(
        LineAnimationOverride(entry_anim=snow_kind, exit_anim=snow_kind, sing_fx=snow_kind)
    )
    restored = line_animation_override_from_dict(data)
    assert restored is not None
    assert restored.entry_anim == snow_kind
    assert restored.exit_anim == snow_kind
    assert restored.sing_fx == snow_kind
    # 未知值不在往返白名单：整份覆盖直接拒绝（返回 None）。
    assert (
        line_animation_override_from_dict({**data, "entry_anim": "snow_solid2"})
        is None
    )


@pytest.mark.parametrize("snow_kind", ["snow", "snow_solid"])
def test_painter_snow_smoke(qapp, snow_kind):
    """CPU painter 雪花两档冒烟：飘入/飘动各一帧不抛异常。"""
    from PyQt6.QtGui import QImage

    from krok_helper.subtitle_render.domain.timing import TimingChar, TimingTrack
    from krok_helper.subtitle_render.engine.painter import paint_frame

    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[
                    TimingChar(text="あ", start_ms=1000),
                    TimingChar(text="い", start_ms=1600),
                ],
                end_ms=2200,
            )
        ]
    )
    sing = Style(
        sing_fx=snow_kind,
        karaoke_anim="utopia",
        fx_particle_size_em=0.6,
        fx_particle_count=8,
    )
    img = QImage(800, 450, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(0xFF101010)
    paint_frame(img, track, 1300, sing)
    entry = Style(
        entry_anim=snow_kind,
        entry_lead_ms=600,
        karaoke_anim="utopia",
    )
    img2 = QImage(800, 450, QImage.Format.Format_ARGB32_Premultiplied)
    img2.fill(0xFF101010)
    paint_frame(img2, track, 1100, entry)


@pytest.mark.parametrize("sing_kind", ["petal", "snow", "snow_solid", "heart", "flame"])
def test_petal_and_snow_sing_particles_skip_whitespace(sing_kind):
    """空格例外（旧粒子口径）对花瓣/雪花同样成立：空格字符不发射唱字
    粒子（无走字内容），出入场粒子仍整行逐字参与（含空格位）。"""
    style = Style(
        sing_fx=sing_kind,
        entry_anim=sing_kind,
        entry_lead_ms=600,
        karaoke_anim="none",
    )
    # 「あ い」中间是空格：3 个字符窗。
    windows = [(1200, 1600), (1600, 2000), (2000, 2400)]
    visible = [True, False, True]
    bursts = plan_line_bursts(
        style, 0, 1000, 4000, 3900, windows, char_visible=visible
    )
    # 音符族（note/heart）与火焰族三档 sweep 全 0（原地出生型），唱字/
    # 入场按「起点是否落在唱字窗内」区分；花瓣/雪花族按 sweep 区分。
    if sing_kind == "flame":
        # 火焰 v3 拆四层色阶（flame/flame_alt），且出入场时长/簇数现在
        # 跟随时长旋钮、起点还带固定偏移表——直接分两次规划，避免用
        # 时间集合区分唱字/入场。
        flame_kinds = ("flame", "flame_alt")
        sing_bursts = plan_line_bursts(
            Style(sing_fx="flame", karaoke_anim="none"),
            0, 1000, 4000, 3900, windows, char_visible=visible,
        )
        sing = [b for b in sing_bursts if b["kind"] in flame_kinds]
        entry_bursts = plan_line_bursts(
            Style(entry_anim="flame", entry_lead_ms=600, karaoke_anim="none"),
            0, 1000, 4000, 3900, windows, char_visible=visible,
        )
        entry = [b for b in entry_bursts if b["kind"] in flame_kinds]
        assert not any(b["char_index"] == 1 for b in sing)
        assert {b["char_index"] for b in sing} == {0, 2}
        assert {b["char_index"] for b in entry} == {0, 1, 2}
        return
    if sing_kind in ("note", "heart"):
        sing = [b for b in bursts if b["kind"] == sing_kind
                and b["start_ms"] in (1200, 2000)]
        entry = [b for b in bursts if b["kind"] == sing_kind
                 and b["start_ms"] not in (1200, 2000)]
    else:
        sing = [b for b in bursts if b["kind"] == sing_kind and b["sweep"] == 0]
        entry = [b for b in bursts if b["kind"] == sing_kind and b["sweep"] == 1]
    # 空格（1600 起）不发射唱字粒子；可见字各 1 条（默认单独颜色双白
    # 折叠单 burst——唱字粒子不吃固定默认档）。
    assert 1600 not in [b["start_ms"] for b in sing]
    assert sorted(b["start_ms"] for b in sing) == [1200, 2000]
    # 入场仍按整行逐字（3 字 × 固定档双色拆分，含空格位）。
    assert sorted(b["char_index"] for b in entry) == [0, 0, 1, 1, 2, 2]


def test_heart_sprite_contract():
    """爱心 sprite = **Twemoji ❤️（U+2764，CC-BY 4.0）官方 path 归一化**
    （2026-10-18 用户口径：联网取现成软萌轮廓、不要自行设计）：M + 8×C
    + Z 单轮廓，宽 ±470 / 高 ±421（h/w ≈ 0.90 略宽于高 = emoji 比例），
    双瓣顶浅缺刻、底点在中线。"""
    heart = FX_SPRITES["heart"]["path_commands"]
    assert [c[0] for c in heart] == ["M"] + ["C"] * 8 + ["Z"]
    assert sum(1 for c in heart if c[0] == "M") == 1

    def flatten():
        out = []
        cur = None
        for c in heart:
            if c[0] == "M":
                cur = (float(c[1]), float(c[2]))
                out.append(cur)
            elif c[0] == "C":
                p0 = cur
                p1 = (float(c[1]), float(c[2]))
                p2 = (float(c[3]), float(c[4]))
                p3 = (float(c[5]), float(c[6]))
                for k in range(1, 33):
                    t = k / 32
                    mt = 1 - t
                    out.append((
                        mt**3*p0[0] + 3*mt*mt*t*p1[0] + 3*mt*t*t*p2[0] + t**3*p3[0],
                        mt**3*p0[1] + 3*mt*mt*t*p1[1] + 3*mt*t*t*p2[1] + t**3*p3[1],
                    ))
                cur = p3
        return out

    pts = flatten()
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    assert max(xs) == pytest.approx(470.0, abs=0.2)
    assert min(xs) == pytest.approx(-470.0, abs=0.2)
    w = max(xs) - min(xs)
    h = max(ys) - min(ys)
    assert 0.85 <= h / w <= 0.95  # emoji 比例：略宽于高
    # 顶部缺刻：最高点在两瓣（|x|>100），中线缺刻点显著低于瓣顶。
    top_y = min(ys)
    top_pts = [p for p in pts if p[1] < top_y + 15]
    assert all(abs(p[0]) > 100.0 for p in top_pts)
    notch_y = min(p[1] for p in pts if abs(p[0]) < 1.0)
    assert notch_y - top_y > 60.0
    # 底点在中线（emoji 心形的钝圆底）。
    bottom = max(ys)
    tip = [p for p in pts if p[1] > bottom - 10]
    assert any(abs(p[0]) < 5.0 for p in tip)
    assert sprite_for_kind("heart") == "heart"


def test_heart_plans_mimic_note_all_three_modes():
    """爱心三档 = 音符的完全克隆（时序/数量/行程/锚点/种子结构同款，
    kind 与颜色不同）：入场逐字错峰、退场同窗反向、唱字每字 3 颗。"""
    def plan(kind: str):
        style = Style(
            entry_anim=kind, entry_lead_ms=600, exit_anim=kind,
            exit_fade_ms=600, sing_fx=kind, fx_particle_count=14,
            font_size_px=100, karaoke_anim="none",
        )
        return plan_line_bursts(
            style, 0, 1000, 4200, 4100,
            [(1200, 1600), (1600, 2000), (2000, 3000)],
        )

    note = plan("note")
    heart = plan("heart")
    assert len(note) == len(heart) > 0

    def strip(bursts, kind):
        out = []
        for b in bursts:
            b = dict(b)
            b.pop("seed", None)
            b.pop("color", None)  # 配色按 kind 取默认对，本就不同
            assert b["kind"] == kind
            b.pop("kind")
            out.append(b)
        return out

    note_bursts = strip(note, "note")
    heart_bursts = strip(heart, "heart")
    assert note_bursts == heart_bursts  # 除 kind/seed 外逐字段相等

    heart_all = [b for b in heart]
    entry = [b for b in heart_all if b.get("sweep") == 0 and b["start_ms"] < 1500]
    # 入场逐字错峰（3 字 3 组起点递增）。
    starts = sorted({b["start_ms"] for b in entry if b["char_index"] in (0, 1, 2)})
    assert len(starts) >= 2
    # 出入场固定默认档双色：正红 + 浅粉（canvify 情人节表 Classic Valentine
    # Red/Blush Pink）；唱字不吃固定默认档（默认单独颜色=白，与音符同口径）。
    window_starts = {1200, 1600, 2000}
    anim_colors = {
        b["color"] for b in heart_all if b["start_ms"] not in window_starts
    }
    assert anim_colors == {"#E63946", "#F9C2D1"}


def test_heart_default_colors_differ_from_other_kinds():
    """默认双色按 kind：爱心=正红+浅粉；雪花=雪色；音符=樱花粉。"""

    def _sing_colors(kind: str) -> set[str]:
        bursts = plan_line_bursts(
            Style(
                sing_fx=kind,
                fx_particle_color_mode="sakura",
                fx_particle_count=12,
                font_size_px=100,
                karaoke_anim="none",
            ),
            0, 0, 3000, 2900, [(100, 400)],
        )
        return {b["color"] for b in bursts if b["kind"] == kind}

    assert _sing_colors("heart") == {"#E63946", "#F9C2D1"}
    assert _sing_colors("note") == {"#FFB7C5", "#FFD7E0"}


def test_heart_trajectory_identical_to_note():
    """轨迹同构：同参数下 heart 与 note 的求值器输出逐位一致（纯克隆，
    只有 sprite 不同——渲染期按 kind 取表）。"""
    base = {
        "kind": "note", "anchor": "char", "char_index": 0,
        "start_ms": 0, "end_ms": 900, "count": 6, "seed": 777,
        "size_px": 30.0, "travel_px": 54.0, "front": True, "sweep": 0,
    }
    for t in (120, 300, 600):
        note = burst_particles_at(base, t, 0.0, 0.0, 120.0, 100.0)
        heart = burst_particles_at(
            {**base, "kind": "heart"}, t, 0.0, 0.0, 120.0, 100.0
        )
        assert len(note) == len(heart)
        for a, b in zip(note, heart):
            assert (a.x, a.y, a.rotation_deg, a.size_px, a.alpha) == (
                b.x, b.y, b.rotation_deg, b.size_px, b.alpha
            )
        # 音符族语义：上升（y 随时间减小）+ 摆动。
        early = burst_particles_at(base, 150, 0.0, 0.0, 120.0, 100.0)
        late = burst_particles_at(base, 800, 0.0, 0.0, 120.0, 100.0)
        for a, b in zip(early, late):
            assert b.y < a.y


def test_heart_gpu_support_and_override_roundtrip():
    """爱心档不触发 GPU 整帧回退；逐行覆盖可往返序列化。"""
    track = type("Track", (), {"lines": []})()
    style = Style(
        entry_anim="heart",
        exit_anim="heart",
        sing_fx="heart",
        karaoke_anim="inherit",
        reverse_karaoke_anim="inherit",
    )
    assert gpu_unsupported_features(track, style) == ()
    data = line_animation_override_to_dict(
        LineAnimationOverride(entry_anim="heart", exit_anim="heart", sing_fx="heart")
    )
    restored = line_animation_override_from_dict(data)
    assert restored is not None
    assert restored.entry_anim == "heart"
    assert restored.exit_anim == "heart"
    assert restored.sing_fx == "heart"


def test_painter_heart_smoke(qapp):
    """CPU painter 爱心三档冒烟：唱字/入场各一帧不抛异常。"""
    from PyQt6.QtGui import QImage

    from krok_helper.subtitle_render.domain.timing import TimingChar, TimingTrack
    from krok_helper.subtitle_render.engine.painter import paint_frame

    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[
                    TimingChar(text="あ", start_ms=1000),
                    TimingChar(text="い", start_ms=1600),
                ],
                end_ms=2200,
            )
        ]
    )
    sing = Style(
        sing_fx="heart",
        karaoke_anim="utopia",
        fx_particle_size_em=0.6,
        fx_particle_count=8,
    )
    img = QImage(800, 450, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(0xFF101010)
    paint_frame(img, track, 1300, sing)
    entry = Style(
        entry_anim="heart",
        entry_lead_ms=600,
        karaoke_anim="utopia",
    )
    img2 = QImage(800, 450, QImage.Format.Format_ARGB32_Premultiplied)
    img2.fill(0xFF101010)
    paint_frame(img2, track, 1100, entry)


def test_entry_exit_particle_emission_decoupled_from_duration_knob():
    """2026-10 分离口径：粒子发射恒按标准速率——「入场/退场动画时长」
    旋钮（120ms~3000ms 全档位）不再改变粒子窗口；短旋钮下退场粒子越过
    显示窗终点完整播放；尾端助手与规划分支同一算式。"""
    def windows(exit_fade_ms: int):
        style = Style(
            entry_anim="note", entry_lead_ms=600,
            exit_anim="note", exit_fade_ms=exit_fade_ms,
            font_size_px=100, karaoke_anim="none",
        )
        bursts = plan_line_bursts(
            style, 0, 1000, 4200, 4100,
            [(1200, 1600), (1600, 2000), (2000, 3000)],
        )
        entry = sorted(
            (b["char_index"], b["start_ms"], b["end_ms"])
            for b in bursts
            if b["kind"] == "note" and b["start_ms"] < 1500
        )
        exit_ = sorted(
            (b["char_index"], b["start_ms"], b["end_ms"])
            for b in bursts
            if b["kind"] == "note" and b["start_ms"] >= 3000
        )
        return entry, exit_

    baseline_entry, baseline_exit = windows(600)
    # 逐字 ±80ms 抖动：每字两色变体共享起点，起点不再严格等差错峰。
    for char_index, base in enumerate((1000, 1175, 1350)):
        rows = [row for row in baseline_entry if row[0] == char_index]
        assert len(rows) == 2
        assert all(abs(row[1] - base) <= 80 for row in rows)
        assert all(row[2] == row[1] + 800 for row in rows)
    assert len({row[1] for row in baseline_entry}) > 1
    for char_index, base in enumerate((4100, 4275, 4450)):
        rows = [row for row in baseline_exit if row[0] == char_index]
        assert len(rows) == 2
        assert all(abs(row[1] - base) <= 80 for row in rows)
        assert all(row[2] == row[1] + 800 for row in rows)
    # 发射窗口与时长旋钮完全解耦（各档位逐字段相等）。
    for fade in (120, 300, 600, 1200, 3000):
        entry, exit_ = windows(fade)
        assert entry == baseline_entry, fade
        assert exit_ == baseline_exit, fade
    # 末字终点越过显示窗终点（4200）——完整播放口径。
    assert max(row[2] for row in baseline_exit) > 4200
    # 尾端助手与规划同式（CPU 尾奏通道的淘汰界；含最大正向抖动）。
    assert exit_particle_tail_end_ms("note", 3, 4100, 4200) == 5250 + 78
    assert exit_particle_tail_end_ms("sparkle", 3, 4100, 4200) == 4100 + 350 + 600 + 78
    assert exit_particle_tail_end_ms("petal", 3, 4100, 4200) == 4100 + 350 + 900 + 78
    assert exit_particle_tail_end_ms("snow", 3, 4100, 4200) == 4500
    assert exit_particle_tail_end_ms("dissolve_out", 3, 4100, 4200) is None


def _bright_pixels(img) -> int:
    n = 0
    for y in range(0, img.height(), 3):
        for x in range(0, img.width(), 3):
            c = img.pixelColor(x, y)
            if c.red() > 40 or c.green() > 40:
                n += 1
    return n


def test_painter_exit_particle_tail_plays_beyond_display_end(qapp):
    """2026-10 分离口径（CPU 渲染端）：退场粒子越过显示窗终点完整播放。

    note 退场锚 = max(行末 3000, 显示末-1150)，末字 burst 终点因
    ±80ms 抖动可越过显示窗终点——显示窗外的一段只有「退场尾奏通道」
    （_paint_exit_particle_tails）能画；fade 对照档在显示窗外应全空。
    画布取 800×450（视口变换在 400×200 下内容出画）。"""
    from PyQt6.QtGui import QImage

    from krok_helper.subtitle_render.domain.timing import TimingChar, TimingTrack
    from krok_helper.subtitle_render.engine.painter import paint_frame

    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[
                    TimingChar(text="AB", start_ms=2000),
                    TimingChar(text="CD", start_ms=2400),
                ],
                end_ms=3000,
            )
        ]
    )

    def frame(t: int, exit_anim: str) -> int:
        style = Style(
            font_size_px=80,
            font_family="Arial",
            karaoke_anim="none",
            entry_anim="none",
            exit_anim=exit_anim,
            exit_fade_ms=200,
            karaoke_colors=_karaoke_matrix("#FFFFFF", "#FFF0AA"),
        )
        img = QImage(800, 450, QImage.Format.Format_ARGB32_Premultiplied)
        img.fill(0xFF101010)
        paint_frame(img, track, t, style)
        return _bright_pixels(img)

    # 窗内（正常路径：文字 + 粒子）与尾奏段（仅尾奏通道）都有内容。
    # 逐字 ±80ms 时间抖动后末 burst 终点会略微平移：取两个相邻尾部采样
    # 覆盖抖动范围，而不是逐帧扫描（pixelColor 采样很慢）。
    assert frame(2800, "note") > 0
    assert any(frame(t, "note") > 0 for t in (3400, 3600))
    assert frame(4200, "note") == 0
    # 非粒子退场档不进尾奏通道：显示窗外全空。
    assert frame(4200, "fade") == 0


def test_flame_sprite_contract():
    """火焰 v3 sprite 契约（2026-10-18 用户口径：标准 Aeg 模板的软火团 +
    细长焰芯，不是锐利叶片）：``flame`` = M+6×C+Z 圆钝火团，h/w≈1.76、
    焰尖偏右；``flame_alt`` = M+8×C+Z 细长 S 形内焰，h/w≈2.65、明显更窄；
    ``ember`` 用 ``dot`` 实心圆（M+4×C+Z）。"""
    flame = FX_SPRITES["flame"]["path_commands"]
    assert [c[0] for c in flame] == ["M"] + ["C"] * 6 + ["Z"]
    alt = FX_SPRITES["flame_alt"]["path_commands"]
    assert [c[0] for c in alt] == ["M"] + ["C"] * 8 + ["Z"]
    dot = FX_SPRITES["dot"]["path_commands"]
    assert [c[0] for c in dot] == ["M"] + ["C"] * 4 + ["Z"]

    def flatten(commands):
        out = []
        cur = None
        for c in commands:
            if c[0] == "M":
                cur = (float(c[1]), float(c[2]))
                out.append(cur)
            elif c[0] == "C":
                p0 = cur
                p1 = (float(c[1]), float(c[2]))
                p2 = (float(c[3]), float(c[4]))
                p3 = (float(c[5]), float(c[6]))
                for k in range(1, 33):
                    t = k / 32
                    mt = 1 - t
                    out.append((
                        mt**3*p0[0] + 3*mt*mt*t*p1[0] + 3*mt*t*t*p2[0] + t**3*p3[0],
                        mt**3*p0[1] + 3*mt*mt*t*p1[1] + 3*mt*t*t*p2[1] + t**3*p3[1],
                    ))
                cur = p3
        return out

    pts = flatten(flame)
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    w = max(xs) - min(xs)
    h = max(ys) - min(ys)
    assert 1.65 <= h / w <= 1.90  # 火团修长比例
    assert max(xs) <= 500.0 and -min(ys) <= 500.0
    # 焰尖：顶部 3% 高度内收成一窄尖（不是圆顶）。
    top_zone = [p for p in pts if p[1] < min(ys) + h * 0.03]
    assert top_zone and max(abs(p[0]) for p in top_zone) < w * 0.10
    # 圆钝火团：上段不比下段窄出“叶片腰”。
    upper = [p for p in pts if p[1] < min(ys) + h * 0.35]
    lower = [p for p in pts if p[1] > min(ys) + h * 0.55]
    assert max(abs(p[0]) for p in upper) > max(abs(p[0]) for p in lower) * 0.95

    alt_pts = flatten(alt)
    alt_xs = [p[0] for p in alt_pts]
    alt_ys = [p[1] for p in alt_pts]
    alt_w = max(alt_xs) - min(alt_xs)
    alt_h = max(alt_ys) - min(alt_ys)
    assert alt_h / alt_w > 2.4  # 细长焰芯
    assert alt_w < w * 0.75  # 明显窄于主火团

    assert sprite_for_kind("flame") == "flame"
    assert sprite_for_kind("flame_alt") == "flame_alt"
    assert sprite_for_kind("ember") == "dot"
    # 灰烬 speck：小碎块单轮廓（五边直线）。
    ash = FX_SPRITES["ash"]["path_commands"]
    assert [c[0] for c in ash] == ["M", "L", "L", "L", "L", "Z"]
    radii = [
        math.hypot(float(c[i]), float(c[i + 1]))
        for c in ash
        for i in range(1, len(c) - 1, 2)
    ]
    assert max(radii) <= 200.0 and min(radii) >= 100.0
    assert sprite_for_kind("ash") == "ash"


def test_flame_plans_duration_driven_entry_exit_and_sing():
    """火焰 v3 规划（2026-10-18 用户口径更新）：火粒总长跟随出入场
    时长；文字全部出现/消失后只留 FLAME_TAIL_MS 短尾；四层色阶、
    背光/余烬/灰烬仍在；唱字按字长持续喷发。"""
    from krok_helper.subtitle_render.engine.render.effects.particles import (
        CHAR_TIME_OFFSETS_MS,
        FLAME_TAIL_MS,
    )

    fire_kinds = ("flame", "flame_alt")

    def plan(*, fade=600, entry_lead=600):
        style = Style(
            entry_anim="flame", entry_lead_ms=entry_lead,
            exit_anim="flame", exit_fade_ms=fade,
            sing_fx="none", font_size_px=100, karaoke_anim="none",
        )
        return plan_line_bursts(
            style,
            0,
            1000,
            4100 + fade,
            4100,
            [(1200, 1600), (1600, 2000), (2000, 3000)],
        )

    # 入场：时长 600 → 3 簇/字、每字 32 颗；火在文字全部出现后 260ms
    # 熄灭。
    bursts = plan(fade=600, entry_lead=600)
    entry_fire_end = 1000 + 600 + FLAME_TAIL_MS
    entry = [b for b in bursts if b["kind"] in fire_kinds and b["start_ms"] < 3000]
    assert len(entry) == 3 * 3 * 4  # 字 × 簇 × 层
    for char_index in range(3):
        base = 1000 + 175 * char_index + CHAR_TIME_OFFSETS_MS[char_index]
        starts = sorted({
            b["start_ms"] for b in entry if b["char_index"] == char_index
        })
        assert starts == [base, base + 200, base + 400]
        per_char = [
            b for b in entry if b["char_index"] == char_index
        ]
        assert sum(b["count"] for b in per_char) == 32
        assert max(b["end_ms"] for b in per_char) == entry_fire_end
    # 每簇 4 层 = 2 主火团 + 2 细长焰芯。
    for start in {b["start_ms"] for b in entry}:
        at = [b for b in entry if b["start_ms"] == start]
        assert len(at) == 4
        assert [b["kind"] for b in at].count("flame") == 2
        assert [b["kind"] for b in at].count("flame_alt") == 2
    # 四层色阶 + 尺寸/行程递减。
    assert {b["color"] for b in entry} == {
        "#FF6B1A", "#FFA31A", "#FFD24A", "#FFF7D6",
    }
    assert sorted({round(float(b["size_px"]), 2) for b in entry}) == [
        21.0, 29.4, 36.96, 42.0,
    ]
    assert sorted({round(float(b["travel_px"]), 2) for b in entry}) == [
        114.24, 125.66, 134.23, 142.8,
    ]
    # 入场时长翻倍 → 数量/簇数同比放大，短尾仍固定 260ms。
    longer = plan(fade=600, entry_lead=1200)
    longer_entry = [
        b for b in longer if b["kind"] in fire_kinds and b["start_ms"] < 3000
    ]
    longer_counts = [
        sum(
            b["count"]
            for b in longer_entry
            if b["char_index"] == char_index
        )
        for char_index in range(3)
    ]
    # 尾字第 5 簇越过短尾会被裁掉，因此允许 52~64；总量仍明显大于
    # 600ms 档（3×32）。
    assert all(52 <= count <= 64 for count in longer_counts)
    assert sum(longer_counts) > 3 * 32
    for char_index in range(3):
        per_char = [
            b for b in longer_entry if b["char_index"] == char_index
        ]
        assert len({b["start_ms"] for b in per_char}) in (4, 5)  # 5 簇/裁尾
        assert max(b["end_ms"] for b in per_char) == 1000 + 1200 + FLAME_TAIL_MS

    # 退场：总长跟随 fade；文字全部消失后 260ms 熄灭；各伴生层不越过短尾。
    for fade in (120, 600, 3000):
        bursts = plan(fade=fade, entry_lead=600)
        fire_end = 4100 + fade + FLAME_TAIL_MS
        flame = [
            b for b in bursts
            if b["kind"] in fire_kinds and b["start_ms"] >= 3000
        ]
        assert flame
        assert max(b["end_ms"] for b in flame) == fire_end
        assert all(b["end_ms"] <= fire_end for b in flame)
        for char_index in range(3):
            per_char = [b for b in flame if b["char_index"] == char_index]
            assert per_char  # 每个字都至少烧到短尾
        ash = [b for b in bursts if b["kind"] == "ash"]
        assert ash and all(b["end_ms"] <= fire_end for b in ash)
        assert {b["color"] for b in ash} == {"#9C9C9C", "#555555"}
        assert all(b["size_px"] == pytest.approx(14.0) for b in ash)
        assert all(b["travel_px"] == 0.0 for b in ash)
        glows = [
            b for b in bursts
            if b["kind"] == "flame_glow" and b["start_ms"] >= 3000
        ]
        assert glows and all(b["end_ms"] <= fire_end for b in glows)
        assert all(b["front"] is False for b in glows)
        assert all(b["size_px"] == pytest.approx(42.0 * 2.8) for b in glows)
        embers = [
            b for b in bursts
            if b["kind"] == "ember" and b["start_ms"] >= 3000
        ]
        assert embers and all(b["end_ms"] <= fire_end for b in embers)
        assert {b["color"] for b in embers} == {"#FFD873", "#FF7A1A"}
        assert all(b["size_px"] == pytest.approx(42.0 * 0.20) for b in embers)
        assert all(b["front"] is True for b in embers)

    # 唱字：按字长持续喷发。400ms 字 = 2 簇；1000ms 字 = 4 簇。
    sing_style = Style(
        sing_fx="flame", fx_particle_count=14,
        font_size_px=100, karaoke_anim="none",
    )
    sing = [
        b for b in plan_line_bursts(
            sing_style, 0, 1000, 4200, 4100,
            [(1200, 1600), (1600, 2000), (2000, 3000)],
        )
        if b["kind"] in fire_kinds
    ]
    for char_index, expected_starts in (
        (0, [1200, 1400]),
        (1, [1600, 1800]),
        (2, [2000, 2250, 2500, 2750]),
    ):
        starts = sorted({
            b["start_ms"] for b in sing if b["char_index"] == char_index
        })
        assert starts == expected_starts, char_index
    per_char_counts = {
        char_index: sum(
            b["count"] for b in sing if b["char_index"] == char_index
        )
        for char_index in range(3)
    }
    assert per_char_counts == {0: 14, 1: 14, 2: 24}
    assert {b["color"] for b in sing} == {
        "#FF6B1A", "#FFA31A", "#FFD24A", "#FFF7D6",
    }
    sing_sizes = sorted({round(float(b["size_px"]), 2) for b in sing})
    assert sing_sizes == [24.0, 33.6, 42.24, 48.0]
    assert len([b for b in plan_line_bursts(
        sing_style, 0, 1000, 4200, 4100,
        [(1200, 1600), (1600, 2000), (2000, 3000)],
    ) if b["kind"] == "flame_glow"]) == 8
    assert len([b for b in plan_line_bursts(
        sing_style, 0, 1000, 4200, 4100,
        [(1200, 1600), (1600, 2000), (2000, 3000)],
    ) if b["kind"] == "ember"]) == 16


def test_flame_trajectory_rises_grows_then_cools():
    """火焰 v3 轨迹：单调上升（加速 p^1.35）、尺寸快涨后冷却收缩、
    速度拉伸（越升越窄越长）、快显长灭、确定性。"""
    base = {
        "kind": "flame", "anchor": "char", "char_index": 0,
        "start_ms": 0, "end_ms": 1100, "count": 1, "seed": 4242,
        "size_px": 40.0, "travel_px": 136.0, "front": True, "sweep": 0,
    }
    at_200 = burst_particles_at(base, 200, 0.0, 0.0, 120.0, 100.0)[0]
    at_400 = burst_particles_at(base, 400, 0.0, 0.0, 120.0, 100.0)[0]
    at_800 = burst_particles_at(base, 800, 0.0, 0.0, 120.0, 100.0)[0]
    assert at_200 == burst_particles_at(base, 200, 0.0, 0.0, 120.0, 100.0)[0]
    assert at_400.y < at_200.y  # 上升
    assert at_200.y < 100.0  # 200ms 仍在字框下半部附近
    assert at_800.y < at_400.y
    assert at_400.size_px > at_200.size_px  # 快涨
    assert at_800.size_px < at_400.size_px  # 冷却收缩
    assert at_400.stretch_y > at_200.stretch_y  # 越长
    assert at_800.stretch_y > at_400.stretch_y
    assert at_400.stretch_x < at_200.stretch_x  # 越窄
    assert at_800.stretch_x < at_400.stretch_x
    for state in (at_200, at_400, at_800):
        assert 0.0 < state.alpha <= 1.0
    assert burst_particles_at(base, -1, 0.0, 0.0, 120.0, 100.0) == []
    assert burst_particles_at(base, 1101, 0.0, 0.0, 120.0, 100.0) == []
    # flame_alt 与主火团同一条运动学（只有 sprite 不同）。
    alt = {**base, "kind": "flame_alt"}
    for t in (200, 400, 800):
        main = burst_particles_at(base, t, 0.0, 0.0, 120.0, 100.0)
        other = burst_particles_at(alt, t, 0.0, 0.0, 120.0, 100.0)
        assert main == other


def test_ember_trajectory_rises_twinkles_and_shrinks():
    """余烬：快速上蹿、横向漂移、边升边缩小、闪烁衰减。"""
    base = {
        "kind": "ember", "anchor": "char", "char_index": 0,
        "start_ms": 0, "end_ms": 1300, "count": 8, "seed": 777,
        "size_px": 8.0, "travel_px": 100.0, "front": True, "sweep": 0,
    }
    early = burst_particles_at(base, 300, 0.0, 0.0, 120.0, 100.0)
    late = burst_particles_at(base, 900, 0.0, 0.0, 120.0, 100.0)
    assert len(early) > 0 and len(late) > 0
    assert min(p.y for p in late) < min(p.y for p in early)
    for state in early + late:
        assert 0.0 < state.alpha <= 1.0
        assert state.stretch_x == pytest.approx(1.0)
        assert state.stretch_y == pytest.approx(1.0)
    assert burst_particles_at(base, 1301, 0.0, 0.0, 120.0, 100.0) == []


def test_flame_glow_trajectory_is_large_and_low_alpha():
    """背光：大尺寸、低透明度（压在文字下方只做辉光，不叠亮正文）。"""
    base = {
        "kind": "flame_glow", "anchor": "char", "char_index": 0,
        "start_ms": 0, "end_ms": 900, "count": 2, "seed": 515,
        "size_px": 120.0, "travel_px": 80.0, "front": False, "sweep": 0,
    }
    states = burst_particles_at(base, 300, 0.0, 0.0, 120.0, 100.0)
    assert len(states) > 0
    assert all(0.0 < state.alpha < 0.2 for state in states)
    assert all(state.size_px > 60.0 for state in states)
    assert sprite_for_kind("flame_glow") == "flame"
    assert burst_particles_at(base, 901, 0.0, 0.0, 120.0, 100.0) == []


def test_ash_trajectory_falls_shrinks_and_fades():
    """灰烬轨迹：飘落（y 随时间增大）、边落边缩小、快显长灭。"""
    base = {
        "kind": "ash", "anchor": "char", "char_index": 0,
        "start_ms": 0, "end_ms": 950, "count": 6, "seed": 777,
        "size_px": 22.0, "travel_px": 0.0, "front": True, "sweep": 0,
    }
    early = burst_particles_at(base, 200, 0.0, 0.0, 120.0, 100.0)
    late = burst_particles_at(base, 800, 0.0, 0.0, 120.0, 100.0)
    assert len(early) > 0
    for a, b in zip(early, late):
        assert b.y > a.y  # 飘落（灰烬下坠，区别于火焰上升）
        assert b.size_px < a.size_px  # 边落边缩小
    for state in early + late:
        assert 0.0 < state.alpha <= 1.0
    assert burst_particles_at(base, -1, 0.0, 0.0, 120.0, 100.0) == []
    assert burst_particles_at(base, 951, 0.0, 0.0, 120.0, 100.0) == []


def test_flame_char_state_late_stretch_reveal_and_dissolve():
    """火焰档文字本体（2026-10-18 用户口径）：参考既有 stretch_in/out 的
    光条凝聚/弥散，但延后到火烧到后段（62%）——入场前 62% 完全隐形，
    后 38% 由 3.2× 横向光条收束成字形；退场前 62% 保持清晰，后 38%
    横向拉伸成光条淡出。"""
    style = Style(font_size_px=100, entry_lead_ms=600, exit_fade_ms=600)
    entry = _entry_transition("flame")
    early = transition_char_state(style, entry, 0, 4, t_ms=entry.start_ms + 40)
    assert early[0] == pytest.approx(0.0)  # 前 62% 隐形
    assert early[4] == pytest.approx(3.2)  # 3.2× 横向光条待凝聚
    late = transition_char_state(style, entry, 0, 4, t_ms=entry.start_ms + 250)
    assert late[0] == pytest.approx(1.0)
    assert late[4] == pytest.approx(1.0)  # 收束完成
    exit_t = _exit_transition("flame", 1000)
    keep = transition_char_state(style, exit_t, 0, 4, t_ms=1000 + 100)
    assert keep[0] == pytest.approx(1.0)  # 前 62% 仍清晰
    assert keep[4] == pytest.approx(1.0)
    vanish = transition_char_state(style, exit_t, 0, 4, t_ms=1000 + 250)
    assert vanish[0] == pytest.approx(0.0)  # 烧完才消失
    assert vanish[4] == pytest.approx(3.2)  # 拉伸成光条


def test_flame_gpu_support_and_override_roundtrip():
    """火焰出入场档不触发 GPU 整帧回退；逐行覆盖可往返序列化（无唱字）。"""
    track = type("Track", (), {"lines": []})()
    style = Style(
        entry_anim="flame",
        exit_anim="flame",
        sing_fx="flame",
        karaoke_anim="inherit",
        reverse_karaoke_anim="inherit",
    )
    assert gpu_unsupported_features(track, style) == ()
    data = line_animation_override_to_dict(
        LineAnimationOverride(
            entry_anim="flame", exit_anim="flame", sing_fx="flame"
        )
    )
    restored = line_animation_override_from_dict(data)
    assert restored is not None
    assert restored.entry_anim == "flame"
    assert restored.exit_anim == "flame"
    assert restored.sing_fx == "flame"


def test_painter_flame_smoke(qapp):
    """CPU painter 火焰两档冒烟：入场/退场各一帧不抛异常。"""
    from PyQt6.QtGui import QImage

    from krok_helper.subtitle_render.domain.timing import TimingChar, TimingTrack
    from krok_helper.subtitle_render.engine.painter import paint_frame

    track = TimingTrack(
        lines=[
            TimingLine(
                chars=[
                    TimingChar(text="あ", start_ms=1000),
                    TimingChar(text="い", start_ms=1600),
                ],
                end_ms=2200,
            )
        ]
    )
    entry = Style(
        entry_anim="flame",
        entry_lead_ms=600,
        karaoke_anim="utopia",
    )
    img = QImage(800, 450, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(0xFF101010)
    paint_frame(img, track, 1100, entry)
    exit_style = Style(
        exit_anim="flame",
        exit_fade_ms=600,
        karaoke_anim="utopia",
    )
    img2 = QImage(800, 450, QImage.Format.Format_ARGB32_Premultiplied)
    img2.fill(0xFF101010)
    paint_frame(img2, track, 2100, exit_style)


def test_front_particles_paint_after_ruby_layer():
    """前层装饰粒子必须画在注音之后（2026-10-18 用户口径：粒子本体在
    主文字前，生长时不应被注音压住——front 统一盖过正文与注音）。

    这是双端图层顺序的源码级护栏：painter 三处 front 调用紧跟在
    paint_rubies_on_top() 之后；D2D 的 drawFxBursts(true) 在 ruby 循环后。
    """
    import inspect
    from pathlib import Path

    from krok_helper.subtitle_render.engine import painter as painter_module

    source = inspect.getsource(painter_module._paint_line_static)
    lines = [line.strip() for line in source.splitlines()]
    hits = 0
    for index, line in enumerate(lines):
        if not line.startswith("paint_fx_particles(front=True)"):
            continue
        probe = index - 1
        while probe >= 0 and (
            not lines[probe] or lines[probe].startswith("#")
        ):
            probe -= 1
        assert lines[probe] == "paint_rubies_on_top()"
        hits += 1
    assert hits >= 3

    gpu_source = (
        Path(__file__).resolve().parents[1]
        / "native/subtitle_renderer/src/backends/direct2d/d2d_backend_render.cpp"
    ).read_text(encoding="utf-8")
    marker = "// 前层装饰粒子：画在注音之后"
    front_call = gpu_source.index("drawFxBursts(true);", gpu_source.index(marker))
    ruby_loop = gpu_source.index(
        "for (std::size_t rubyIndex = 0;"
        " rubyIndex < line->rubies.size(); ++rubyIndex)"
    )
    assert front_call > ruby_loop
