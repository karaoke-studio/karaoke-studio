"""Tests for ``krok_helper.subtitle_render.engine.timeline``."""

from __future__ import annotations

from krok_helper.subtitle_render.engine.timeline import (
    apply_n3_seq_line_breaks,
    assign_lanes,
    char_fill_ratio,
    compute_char_intervals,
    compute_display_lines,
    find_active_line,
    find_upcoming_line,
    paragraph_last_line_flags,
    track_duration_ms,
    visible_display_lines,
)
from krok_helper.subtitle_render.models import (
    TimingChar,
    TimingLine,
    TimingTrack,
)


def _make_line(specs, end_ms, *, blank=False, singer=None):
    """Build a TimingLine from list of (text, start_ms)."""
    chars = [TimingChar(text=t, start_ms=s) for t, s in specs]
    return TimingLine(chars=chars, end_ms=end_ms, is_blank=blank, singer_label=singer)


def _track(*lines):
    return TimingTrack(lines=list(lines))


# ---------------------------------------------------------------------------
# char_fill_ratio
# ---------------------------------------------------------------------------


def test_char_fill_ratio_before_start():
    assert char_fill_ratio(1000, 2000, 999) == 0.0
    assert char_fill_ratio(1000, 2000, 1000) == 0.0


def test_char_fill_ratio_after_end():
    assert char_fill_ratio(1000, 2000, 2000) == 1.0
    assert char_fill_ratio(1000, 2000, 5000) == 1.0


def test_char_fill_ratio_midpoint():
    assert char_fill_ratio(1000, 2000, 1500) == 0.5


def test_char_fill_ratio_zero_duration_clamps_to_one_ms():
    # duration <= 0 不应除零；end <= start 视为 1ms 区间
    assert char_fill_ratio(1000, 1000, 1500) == 1.0


# ---------------------------------------------------------------------------
# compute_char_intervals
# ---------------------------------------------------------------------------


def test_compute_char_intervals_basic():
    line = _make_line([("a", 1000), ("b", 1500), ("c", 2000)], end_ms=2500)
    assert compute_char_intervals(line) == [
        (1000, 1500),
        (1500, 2000),
        (2000, 2500),
    ]


def test_compute_char_intervals_weights_shared_lrc_span_like_sug():
    line = TimingLine(
        chars=[
            TimingChar(
                text="W",
                start_ms=1000,
                source_span_start_ms=1000,
                source_span_end_ms=2000,
                source_span_index=0,
                source_span_count=2,
            ),
            TimingChar(
                text="i",
                start_ms=1500,
                source_span_start_ms=1000,
                source_span_end_ms=2000,
                source_span_index=1,
                source_span_count=2,
            ),
        ],
        end_ms=2000,
    )

    assert compute_char_intervals(line, [30, 10]) == [
        (1000, 1750),
        (1750, 2000),
    ]
    # 没有字体宽度的消费者保持解析阶段兼容区间。
    assert compute_char_intervals(line) == [(1000, 1500), (1500, 2000)]


def test_compute_char_intervals_uses_pause_release_as_char_end():
    line = TimingLine(
        chars=[
            TimingChar(text="そ", start_ms=39_010, pause_release_ms=39_410),
            TimingChar(text="れ", start_ms=39_910),
        ],
        end_ms=40_370,
    )

    assert compute_char_intervals(line) == [
        (39_010, 39_410),
        (39_910, 40_370),
    ]


def test_compute_char_intervals_no_line_end_falls_back():
    line = _make_line([("a", 1000)], end_ms=None)
    intervals = compute_char_intervals(line)
    assert intervals == [(1000, 1500)]  # ch.start_ms + 500 fallback


def test_compute_char_intervals_empty_line():
    line = _make_line([], end_ms=None)
    assert compute_char_intervals(line) == []


def test_compute_char_intervals_clamps_when_end_before_start():
    # 异常数据：line.end_ms 比末字 start_ms 还早 → 末字区间退化为零
    line = _make_line([("a", 2000)], end_ms=1000)
    assert compute_char_intervals(line) == [(2000, 2000)]


# ---------------------------------------------------------------------------
# find_active_line / find_upcoming_line
# ---------------------------------------------------------------------------


def test_find_active_line_returns_line_in_range():
    line1 = _make_line([("a", 1000), ("b", 1500)], end_ms=2000)
    line2 = _make_line([("c", 3000), ("d", 3500)], end_ms=4000)
    track = _track(line1, line2)

    assert find_active_line(track, 500) is None
    assert find_active_line(track, 1500) is line1
    assert find_active_line(track, 2500) is None
    assert find_active_line(track, 3000) is line2
    assert find_active_line(track, 5000) is None


def test_find_active_line_honors_lead_in_without_changing_default():
    line = _make_line([("a", 1000)], end_ms=2000)
    track = _track(line)

    assert find_active_line(track, 950) is None
    assert find_active_line(track, 950, lead_in_ms=80) is line


def test_find_active_line_prefers_live_line_over_lead_in_line():
    line1 = _make_line([("a", 1000)], end_ms=2000)
    line2 = _make_line([("b", 1500)], end_ms=2500)
    track = _track(line1, line2)

    assert find_active_line(track, 1450, lead_in_ms=80) is line1


def test_find_active_line_skips_blank_lines():
    line1 = _make_line([("a", 1000)], end_ms=2000)
    blank = _make_line([], end_ms=None, blank=True)
    line3 = _make_line([("b", 3000)], end_ms=4000)
    track = _track(line1, blank, line3)
    assert find_active_line(track, 1500) is line1
    assert find_active_line(track, 3500) is line3


def test_find_active_line_overlap_picks_latest_start():
    # 合唱叠唱：两行重叠区间，find_active_line 返回较晚开始的那条
    line1 = _make_line([("a", 1000)], end_ms=3000)
    line2 = _make_line([("b", 2000)], end_ms=4000)
    track = _track(line1, line2)
    assert find_active_line(track, 2500) is line2


def test_find_upcoming_line_returns_next():
    line1 = _make_line([("a", 1000)], end_ms=2000)
    line2 = _make_line([("b", 3000)], end_ms=4000)
    track = _track(line1, line2)
    assert find_upcoming_line(track, 500) is line1
    assert find_upcoming_line(track, 2500) is line2
    assert find_upcoming_line(track, 5000) is None


# ---------------------------------------------------------------------------
# Display layout windows
# ---------------------------------------------------------------------------


def test_compute_display_lines_matches_n3_top_long_two_lane_model():
    line1 = _make_line([("a", 55_490)], end_ms=59_090)
    line2 = _make_line([("b", 59_340)], end_ms=62_470)
    line3 = _make_line([("c", 62_540)], end_ms=66_280)
    line4 = _make_line([("d", 66_650)], end_ms=71_740)
    line5 = _make_line([("e", 71_980)], end_ms=74_910)
    line6 = _make_line([("f", 75_150)], end_ms=79_240)
    track = _track(line1, line2, line3, line4, line5, line6)

    layouts = compute_display_lines(
        track,
        lead_in_ms=1800,
        tail_ms=1000,
        lane_gap_ms=300,
    )

    # N3 TopLongAdjuster：上行挂到下一页上屏前 300ms；下行 = 自身演唱结束 + 1000。
    # 段首页两行同时入场（BottomLineShowBeginTime → 上行的 ShowBeginTime），
    # 之后每页下行 = 上一页下行消失 + IntervalTime。
    assert [(item.lane, item.display_start_ms, item.display_end_ms) for item in layouts] == [
        (0, 53_690, 60_440),
        (1, 53_690, 63_470),
        (0, 60_740, 69_880),
        (1, 63_770, 72_740),
        (0, 70_180, 80_240),
        (1, 73_040, 80_240),
    ]


def test_visible_display_lines_returns_both_lanes_when_windows_overlap():
    line1 = _make_line([("a", 55_490)], end_ms=59_090)
    line2 = _make_line([("b", 59_340)], end_ms=62_470)
    track = _track(line1, line2)

    visible = visible_display_lines(
        track,
        58_000,
        lead_in_ms=1800,
        tail_ms=1000,
        lane_gap_ms=300,
    )

    assert [item.line for item in visible] == [line1, line2]


def test_compute_display_lines_never_cuts_before_own_sing_end():
    line1 = _make_line([("a", 40_530)], end_ms=44_340)
    line2 = _make_line([("b", 44_700)], end_ms=None)
    line3 = _make_line([("c", 45_530)], end_ms=48_040)
    track = _track(line1, line2, line3)

    layouts = compute_display_lines(
        track,
        lead_in_ms=1800,
        tail_ms=1000,
        lane_gap_ms=300,
    )

    assert [(item.display_start_ms, item.display_end_ms) for item in layouts] == [
        (38_730, 44_840),  # 上行：下一页上屏 44_915 − IntervalTime 300 之前被挤压过
        (38_730, 46_700),  # 下行：无 end_ms → 末字 +1000 当演唱结束，再 + PostTime
        (44_915, 49_040),
    ]
    assert layouts[0].display_end_ms >= line1.end_ms


def test_compute_display_lines_preserves_protected_exit_tail():
    line1 = _make_line([("a", 280)], end_ms=3420)
    line2 = _make_line([("b", 1200)], end_ms=3000)
    line3 = _make_line([("c", 5210)], end_ms=6600)
    track = _track(line1, line2, line3)

    layouts = compute_display_lines(
        track,
        lead_in_ms=1800,
        tail_ms=1000,
        lane_gap_ms=300,
        protect_ms=500,
    )

    assert layouts[0].display_end_ms >= line1.end_ms + 500


def test_compute_display_lines_keeps_next_line_protected_lead_in():
    line1 = _make_line([("a", 1000)], end_ms=2000)
    line2 = _make_line([("b", 9000)], end_ms=10_700)
    line3 = _make_line([("c", 10_800)], end_ms=11_500)
    line4 = _make_line([("d", 11_000)], end_ms=12_000)
    track = _track(line1, line2, line3, line4)

    layouts = compute_display_lines(
        track,
        lead_in_ms=1800,
        tail_ms=1000,
        lane_gap_ms=300,
        protect_ms=500,
    )

    assert [(item.display_start_ms, item.display_end_ms) for item in layouts] == [
        (0, 8_700),
        (0, 10_700),
        (9_000, 13_000),
        (10_700, 13_000),
    ]
    # 第 2 页下行紧跟上一页下行消失（IntervalTime 已被挤压吃掉），但仍早于开唱。
    assert layouts[3].display_start_ms <= line4.chars[0].start_ms


def test_compute_display_lines_has_no_max_hold_cap():
    # N3 没有"最长挂屏"的概念：TopLong 的整页都挂到本段最后一页的演唱结束
    # + PostTime，哪怕窗口远超过旧实现那个 12s 上限。
    line1 = _make_line([("a", 24_060), ("b", 25_060)], end_ms=30_300)
    line2 = _make_line([("c", 30_750)], end_ms=35_770)
    track = _track(line1, line2)

    layouts = compute_display_lines(
        track,
        lead_in_ms=1800,
        tail_ms=1000,
        lane_gap_ms=300,
    )

    assert [(item.display_start_ms, item.display_end_ms) for item in layouts] == [
        (22_260, 36_770),
        (22_260, 36_770),
    ]
    assert layouts[0].display_end_ms - layouts[0].display_start_ms > 12_000


# ---------------------------------------------------------------------------
# track_duration_ms
# ---------------------------------------------------------------------------


def test_track_duration_ms_uses_max_line_end():
    line1 = _make_line([("a", 1000)], end_ms=2000)
    line2 = _make_line([("b", 3000)], end_ms=4500)
    track = _track(line1, line2)
    assert track_duration_ms(track) == 4500


def test_track_duration_ms_empty_track():
    assert track_duration_ms(_track()) == 0


def test_track_duration_ms_no_end_ms_falls_back():
    line = _make_line([("a", 1000)], end_ms=None)
    assert track_duration_ms(_track(line)) == 2000  # 1000 + 1000 fallback


def test_assign_lanes_honors_explicit_n3_page_breaks():
    lines = [
        _make_line([("a", 0)], end_ms=1000),
        _make_line([("b", 1000)], end_ms=2000),
        _make_line([("c", 2000)], end_ms=3000),
        _make_line([("d", 3000)], end_ms=4000),
    ]
    lines[2].break_before = "page"
    lines[3].break_before = "paragraph"

    lanes, page_starts, page_rows = assign_lanes(lines, 2)

    assert lanes == [0, 1, 0, 0]
    assert page_starts == [0, 0, 2, 3]
    assert page_rows == [2, 2, 1, 1]


def test_assign_lanes_ends_page_at_auto_section_boundary():
    lines = [
        _make_line([("a", 0)], end_ms=1000),
        _make_line([("b", 1000)], end_ms=2000),
        _make_line([("c", 2000)], end_ms=3000),
        _make_line([("d", 9000)], end_ms=10000),
    ]

    lanes, page_starts, page_rows = assign_lanes(
        lines, 2, section_gap_ms=4000
    )

    assert lanes == [0, 1, 0, 0]
    assert page_starts == [0, 0, 2, 3]
    assert page_rows == [2, 2, 1, 1]


def test_n3_seq_line_breaker_matches_marginality_opening():
    """N3 10.74 SeqLinesBreaker 默认参数的真实项目回归。"""
    times = [
        (9_670, 16_480),
        (16_670, 23_210),
        (32_220, 41_940),
        (43_410, 52_890),
        (55_020, 63_040),
        (63_720, 66_940),
        (67_610, 72_940),
        (76_180, 81_560),
        (81_960, 87_080),
    ]
    track = _track(
        *[
            _make_line([(chr(ord("a") + index), start)], end_ms=end)
            for index, (start, end) in enumerate(times)
        ]
    )

    assert apply_n3_seq_line_breaks(track) == [
        "none",
        "none",
        "paragraph",
        "none",
        "page",
        "none",
        "page",
        "paragraph",
        "none",
    ]


# ---------------------------------------------------------------------------
# 段落 / 同步退场（按间奏间隔自动分段）
# ---------------------------------------------------------------------------


def _sectioned_track():
    # section0: L0,L1,L2（间隔均 0）；L3 与 L2 间隔 6000ms → section1
    l0 = _make_line([("a", 0)], end_ms=1000)
    l1 = _make_line([("b", 1000)], end_ms=2000)
    l2 = _make_line([("c", 2000)], end_ms=3000)
    l3 = _make_line([("d", 9000)], end_ms=10000)
    return _track(l0, l1, l2, l3)


def _compute(track, **kw):
    base = dict(
        lead_in_ms=0, tail_ms=0, lane_gap_ms=0, section_gap_ms=4000,
    )
    base.update(kw)
    return compute_display_lines(track, **base)


def test_section_ending_clear_caps_cross_section_linger():
    track = _sectioned_track()
    hold = _compute(track, section_ending_mode="hold")
    clear = _compute(track, section_ending_mode="clear")
    # An odd final line must end its page instead of pairing with section1.
    assert hold[2].display_end_ms == 3000
    assert clear[2].display_end_ms == 3000


def test_sync_ending_extends_earlier_lane_to_section_end():
    track = _sectioned_track()
    nosync = _compute(track, sync_ending=False)
    sync = _compute(track, sync_ending=True)
    # L1(lane1) 是 section0 内 lane1 的末行；同步退场把它延到段末 3000
    assert nosync[1].display_end_ms == 2000
    assert sync[1].display_end_ms == 3000


def test_sync_entry_is_noop_when_n3_already_enters_the_head_page_together():
    # N3 的段首页下行本来就跟上行同时上屏（BottomLineShowBeginTime → 上行时刻），
    # 所以"同步入场"在这种结构上无事可做。
    track = _sectioned_track()
    nosync = _compute(track, sync_entry=False)
    sync = _compute(track, sync_entry=True)

    assert [item.display_start_ms for item in nosync] == [0, 0, 2000, 9000]
    assert [item.display_start_ms for item in sync] == [0, 0, 2000, 9000]


def test_sync_entry_keeps_manual_start_override_authoritative():
    track = _sectioned_track()
    track.lines[1].display_start_override_ms = 750

    sync = _compute(track, sync_entry=True)

    assert sync[0].display_start_ms == 0
    assert sync[1].display_start_ms == 750


def test_auto_section_boundary_keeps_next_section_from_entering_early():
    # 跨段不联动：section1 的首行按自己的 PreTime 入场，不会被 section0 拉早。
    track = _sectioned_track()
    layouts = _compute(track, section_ending_mode="hold")

    assert layouts[3].display_start_ms == 9000


def test_auto_section_boundary_prevents_cross_section_hold():
    track = _sectioned_track()
    # 单段时 L2/L3 同页，上行挂到页尾（TopLong 的 tail page 分支）。
    single = compute_display_lines(
        track, lead_in_ms=0, tail_ms=0, lane_gap_ms=0,
    )
    sectioned = _compute(track, sync_ending=False, section_ending_mode="hold")

    assert [item.lane for item in single] == [0, 1, 0, 1]
    assert single[2].display_end_ms == 10000
    assert sectioned[2].display_end_ms == 3000
    assert [item.lane for item in sectioned] == [0, 1, 0, 0]


def test_red_fraction_odd_section_last_line_uses_own_page_end():
    track = _track(
        _make_line([("Do what you think", 65_085)], end_ms=66_365),
        _make_line([("Give it with dedication", 66_585)], end_ms=68_315),
        _make_line([("I'll put out your misery", 68_515)], end_ms=72_065),
        _make_line([("Have no prayer", 84_715)], end_ms=86_915),
    )
    settings = dict(
        lead_in_ms=1800,
        tail_ms=1000,
        lane_gap_ms=300,
        section_gap_ms=4000,
        section_ending_mode="hold",
    )

    nosync = compute_display_lines(track, sync_ending=False, **settings)
    sync = compute_display_lines(track, sync_ending=True, **settings)

    assert [item.lane for item in nosync] == [0, 1, 0, 0]
    assert [item.display_end_ms for item in nosync] == [66_865, 69_315, 73_065, 87_915]
    # 同步退场把 section0 内每个 lane 的末行都延到段末 73_065。
    assert [item.display_end_ms for item in sync] == [66_865, 73_065, 73_065, 87_915]


# ---------------------------------------------------------------------------
# paragraph_last_line_flags（逆向 NKM3 EmptyLineBreaker + SetParagraphBreaks）
# ---------------------------------------------------------------------------


def test_paragraph_last_blank_lines_split_pages():
    # 空行 = 页边界；每页最后一行标 True
    track = _track(
        _make_line([("a", 1000)], end_ms=2000),
        _make_line([("b", 2100)], end_ms=3000),
        TimingLine(is_blank=True),
        _make_line([("c", 10000)], end_ms=11000),
    )
    assert paragraph_last_line_flags(track, threshold_ms=3100) == [
        False,
        True,
        False,
        True,
    ]


def test_paragraph_last_timing_gap_splits_page():
    # 页内演唱空隙 >= 阈值 → 开新段落；间隔小于阈值不分
    track = _track(
        _make_line([("a", 1000)], end_ms=2000),
        _make_line([("b", 2100)], end_ms=3000),
        # 3000 → 10000 空隙 7000 >= 3100 → b 是段落末行
        _make_line([("c", 10000)], end_ms=11000),
        _make_line([("d", 11100)], end_ms=12000),
    )
    assert paragraph_last_line_flags(track, threshold_ms=3100) == [
        False,
        True,
        False,
        True,
    ]


def test_paragraph_last_gap_below_threshold_keeps_paragraph():
    track = _track(
        _make_line([("a", 1000)], end_ms=2000),
        _make_line([("b", 2100)], end_ms=3000),
        _make_line([("c", 5000)], end_ms=6000),  # 空隙 2000 < 3100
    )
    assert paragraph_last_line_flags(track, threshold_ms=3100) == [
        False,
        False,
        True,
    ]


def test_paragraph_last_single_line_page_is_last():
    track = _track(
        _make_line([("a", 1000)], end_ms=2000),
        TimingLine(is_blank=True),
    )
    assert paragraph_last_line_flags(track, threshold_ms=3100) == [True, False]


def test_paragraph_last_gap_measured_against_max_end_so_far():
    # NKM3 用「段内已扫描行的最晚结束」对比「后续行的最早开始」——
    # 叠唱场景第一行拖得比第二行长时，以第一行的结束为准
    track = _track(
        _make_line([("a", 1000)], end_ms=9000),  # 长行，结束晚
        _make_line([("b", 2000)], end_ms=3000),  # 与 a 重叠
        _make_line([("c", 10000)], end_ms=11000),  # 距 max(9000,3000)=9000 仅 1000
    )
    assert paragraph_last_line_flags(track, threshold_ms=3100) == [
        False,
        False,
        True,
    ]


# ---------------------------------------------------------------------------
# 逐行显示/隐藏时间覆盖（字幕轨道把手）
# ---------------------------------------------------------------------------


def test_compute_display_lines_respects_per_line_overrides():
    line1 = _make_line([("a", 5000), ("b", 5500)], end_ms=6000)
    line2 = _make_line([("c", 9000)], end_ms=9500)
    line1.display_start_override_ms = 3000
    line1.display_end_override_ms = 8000
    track = _track(line1, line2)

    items = compute_display_lines(
        track,
        lead_in_ms=500,
        tail_ms=500,
        lane_gap_ms=0,
    )

    assert items[0].display_start_ms == 3000
    assert items[0].display_end_ms == 8000
    # 手动时刻参与本趟计算：段首页下行跟着上行（已被覆盖的）时刻一起入场。
    assert items[1].display_start_ms == 3000
    assert items[1].display_end_ms == 10_000


def test_display_overrides_clamped_to_singing_interval():
    # 覆盖只编辑演唱区间外侧的余量：上屏不晚于首字符，消失不早于行末
    line = _make_line([("a", 5000), ("b", 5500)], end_ms=6000)
    line.display_start_override_ms = 5800
    line.display_end_override_ms = 5200
    track = _track(line)

    items = compute_display_lines(
        track,
        lead_in_ms=500,
        tail_ms=500,
        lane_gap_ms=0,
    )

    assert items[0].display_start_ms == 5000
    assert items[0].display_end_ms == 6000


def test_n3_bottom_single_pages_alternate_only_against_immediate_page():
    lines = [
        _make_line([("a", 1000)], end_ms=2000),
        _make_line([("b", 4000)], end_ms=5000),
        _make_line([("c", 6000)], end_ms=7000),
    ]
    for line in lines[1:]:
        line.break_before = "page"
    lines[0].display_start_override_ms = 0
    lines[0].display_end_override_ms = 10000
    lines[1].display_start_override_ms = 3000
    lines[1].display_end_override_ms = 8000
    lines[2].display_start_override_ms = 5000
    lines[2].display_end_override_ms = 9000
    settings = dict(
        lead_in_ms=500,
        tail_ms=500,
        lane_gap_ms=0,
        lane_count=2,
        row_count_of=lambda _line: 2,
    )

    top_aligned = compute_display_lines(_track(*lines), **settings)
    n3 = compute_display_lines(
        _track(*lines), bottom_align_of=lambda _line: True, **settings
    )

    assert [item.lane for item in top_aligned] == [0, 0, 0]
    # 手动时刻让三页窗口互相重叠：第 2 页被顶上一行，第 3 页又能用回最下行。
    assert [item.lane for item in n3] == [1, 0, 1]
