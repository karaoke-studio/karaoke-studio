"""N3 ``TopLongAdjuster`` 移植的金标准。

期望值不是"当前实现输出什么"，而是从 ``NicoKaraMaker3.dll`` 反编译源码逐行推导
出来的：``TopLongAdjuster.AdjustShowTimes`` + ``ShowTimeAdjuster``
（``BottomLineShowBeginTime`` / ``AdjustSamePositionShowTimesIfNeeded``）。
默认参数 PreTime=1800 / PostTime=1000 / IntervalTime=300 / ProtectTime=500。
"""

from __future__ import annotations

from krok_helper.subtitle_render.engine.show_time import (
    MAX_SHOW_TIME_MS,
    ShowTimePage,
    compute_show_times,
    protect_time_ms,
)

PRE, POST, INTERVAL = 1800, 1000, 300


def _pages(*specs: tuple[int, int, ...]) -> list[ShowTimePage]:
    """``specs`` 每项 = ``(section, configured_rows, *渲染行索引)``。"""
    return [
        ShowTimePage(
            lines=tuple(item[2:]),
            section=item[0],
            configured_rows=item[1],
            vertical_position="bottom",
        )
        for item in specs
    ]


def _run(begins, ends, pages, **kw):
    params = dict(
        pre_time_ms=PRE,
        post_time_ms=POST,
        interval_ms=INTERVAL,
        protect_ms=protect_time_ms(PRE, POST),
    )
    params.update(kw)
    return compute_show_times(begins, ends, pages, **params)


# ---------------------------------------------------------------------------
# ProtectTime（WipeTimingSettingsModel.ProtectTime）
# ---------------------------------------------------------------------------


def test_protect_time_defaults_to_half_the_smaller_of_pre_and_post():
    assert protect_time_ms(1800, 1000) == 500
    assert protect_time_ms(1000, 1800) == 500
    assert protect_time_ms(1800, 0) == 0


def test_manual_protect_time_is_capped_by_the_smaller_of_pre_and_post():
    assert protect_time_ms(1800, 1000, 300) == 300
    assert protect_time_ms(1800, 1000, 5000) == 1000


def test_protect_time_resolution_is_idempotent():
    # painter 先解析一次、timeline 再解析一次，两次结果必须一致。
    once = protect_time_ms(1800, 1000, 1200)
    assert protect_time_ms(1800, 1000, once) == once


# ---------------------------------------------------------------------------
# 上行 / 下行的不对称规则
# ---------------------------------------------------------------------------


def test_top_row_holds_until_the_next_page_shows_minus_interval():
    # TopLineShowEndTime 的非末页分支：max(下一页 ShowBegin − IntervalTime, 0)。
    begins, ends = [10_000, 14_000, 30_000, 34_000], [13_000, 17_000, 33_000, 37_000]
    pages = _pages((0, 2, 0, 1), (0, 2, 2, 3))
    out = _run(begins, ends, pages)

    assert out.starts[2] == 30_000 - PRE  # 28_200
    assert out.ends[0] == 28_200 - INTERVAL  # 27_900：远超自身演唱结束 + PostTime
    assert out.ends[0] - ends[0] == 14_900


def test_top_row_of_the_last_page_in_a_section_uses_bottom_row_sing_end():
    begins, ends = [10_000, 14_000], [13_000, 17_000]
    out = _run(begins, ends, _pages((0, 2, 0, 1)))

    assert out.ends[0] == 17_000 + POST
    assert out.ends[1] == 17_000 + POST


def test_bottom_row_always_exits_at_its_own_sing_end_plus_post_time():
    begins, ends = [10_000, 14_000, 30_000, 34_000], [13_000, 17_000, 33_000, 37_000]
    out = _run(begins, ends, _pages((0, 2, 0, 1), (0, 2, 2, 3)))

    assert out.ends[1] == 17_000 + POST
    assert out.ends[3] == 37_000 + POST


def test_head_page_bottom_row_enters_together_with_the_top_row():
    # BottomLineShowBeginTime 的 IsHeadPage 分支 → 上行的 ShowBeginTime。
    begins, ends = [10_000, 14_000], [13_000, 17_000]
    out = _run(begins, ends, _pages((0, 2, 0, 1)))

    assert out.starts[0] == out.starts[1] == 10_000 - PRE


def test_later_page_bottom_row_enters_after_the_previous_bottom_row_exits():
    begins, ends = [10_000, 14_000, 30_000, 34_000], [13_000, 17_000, 33_000, 37_000]
    out = _run(begins, ends, _pages((0, 2, 0, 1), (0, 2, 2, 3)))

    assert out.starts[3] == out.ends[1] + INTERVAL  # 18_000 + 300


def test_three_row_page_bottom_rows_use_page_top_sing_start_minus_pre():
    # numPageLines > 2 → BottomLineShowBeginTime 回到 top.LyricsActualBeginTime − PreTime。
    begins = [10_000, 14_000, 30_000, 32_000, 34_000]
    ends = [13_000, 17_000, 31_000, 33_000, 37_000]
    out = _run(begins, ends, _pages((0, 2, 0, 1), (0, 3, 2, 3, 4)))

    assert out.starts[3] == 30_000 - PRE
    assert out.starts[4] == 30_000 - PRE


def test_sections_do_not_link_across_a_paragraph_break():
    # NextPageTopLineIndex / PrevPageTopLineIndex 都在 ParagraphBreak 处返回 -1，
    # 所以段末页按末页规则退场，段首页按段首页规则入场。
    begins, ends = [10_000, 14_000, 60_000, 64_000], [13_000, 17_000, 63_000, 67_000]
    out = _run(begins, ends, _pages((0, 2, 0, 1), (1, 2, 2, 3)))

    assert out.ends[0] == 17_000 + POST  # 段末页 → 不再挂到下一段
    assert out.starts[2] == out.starts[3] == 60_000 - PRE  # 段首页两行同时入场


def test_no_hold_cap_exists():
    # 长间奏也不截断：N3 没有"最长挂屏"这个概念。
    begins, ends = [10_000, 12_000], [11_000, 60_000]
    out = _run(begins, ends, _pages((0, 2, 0, 1)))

    assert out.ends[0] == 60_000 + POST
    assert out.ends[0] - out.starts[0] > 50_000


def test_show_times_are_clamped_to_the_n3_sentinel():
    begins, ends = [MAX_SHOW_TIME_MS - 100], [MAX_SHOW_TIME_MS - 50]
    out = _run(begins, ends, _pages((0, 1, 0)))

    assert out.ends[0] == MAX_SHOW_TIME_MS


# ---------------------------------------------------------------------------
# AdjustSamePositionShowTimesIfNeeded
# ---------------------------------------------------------------------------


def test_same_position_squeeze_is_skipped_when_ideal_windows_already_fit():
    # 同屏幕行位的两句间隔 >= PostTime + IntervalTime + PreTime(3100) → 不挤压。
    begins, ends = [10_000, 14_000, 30_000, 34_000], [13_000, 17_000, 33_000, 37_000]
    out = _run(begins, ends, _pages((0, 2, 0, 1), (0, 2, 2, 3)))

    assert begins[2] - ends[0] >= POST + INTERVAL + PRE
    assert out.starts[2] == begins[2] - PRE  # 入场时间完整


def test_same_position_squeeze_only_requires_a_quarter_interval_gap():
    # 上行 0 与上行 2 同屏幕行位，两者演唱间隔 14_000 − 12_000 = 2000 < 3100
    # → 触发挤压。挤压后所需的最小间隔是 IntervalTime / 4 = 75ms，不是完整的 300ms。
    begins, ends = [10_000, 12_500, 14_000, 16_000], [12_000, 13_500, 15_000, 17_000]
    protect = protect_time_ms(PRE, POST)
    out = _run(begins, ends, _pages((0, 2, 0, 1), (0, 2, 2, 3)))

    assert begins[2] - ends[0] < POST + INTERVAL + PRE
    assert out.starts[2] - out.ends[0] == INTERVAL // 4 == 75
    # 让出顺序（N3 的 6 级降级）：先把上句的 PostTime 削到 ProtectTime …
    assert out.ends[0] == ends[0] + protect  # 12_500
    # … 再把本句入场往后推剩下的 375ms（入场时间从 1800 降到 1425，但不低于 protect）。
    assert out.starts[2] == 12_575
    assert begins[2] - out.starts[2] == 1425 >= protect


def test_same_position_squeeze_never_cuts_into_either_wipe_interval():
    # The two same-position lines sing over each other, so no amount of
    # PreTime/PostTime squeezing can create a temporal gap.  Automatic
    # adjustment must stop at the singing boundaries and leave the remaining
    # conflict to spatial placement.
    begins = [10_000, 10_500, 11_000, 11_500]
    ends = [12_500, 13_000, 13_500, 14_000]
    out = _run(begins, ends, _pages((0, 2, 0, 1), (0, 2, 2, 3)))

    assert out.starts[2] <= begins[2]
    assert out.ends[0] >= ends[0]
    assert out.ends[0] > out.starts[2]


def test_auto_squeeze_keeps_250ms_entry_but_allows_zero_exit():
    begins = [10_000, 10_500, 12_100, 12_500]
    ends = [12_000, 12_400, 13_500, 14_000]
    out = _run(
        begins,
        ends,
        _pages((0, 2, 0, 1), (0, 2, 2, 3)),
        auto_entry_reserve_ms=[250, 250, 250, 250],
    )

    assert out.starts[2] == begins[2] - 250
    assert out.ends[0] == ends[0]
    assert out.ends[0] > out.starts[2]


def test_manual_show_start_can_reduce_entry_reserve_below_250ms():
    begins = [10_000, 10_500, 12_100, 12_500]
    ends = [12_000, 12_400, 13_500, 14_000]
    overrides = [(None, None), (None, None), (12_050, None), (None, None)]
    out = _run(
        begins,
        ends,
        _pages((0, 2, 0, 1), (0, 2, 2, 3)),
        overrides=overrides,
        auto_entry_reserve_ms=[250, 250, 250, 250],
    )

    assert out.starts[2] == 12_050
    assert begins[2] - out.starts[2] == 50


def test_animation_aware_squeeze_links_adjacent_pages_across_sections():
    begins = [10_000, 12_500, 14_000, 16_000]
    ends = [12_000, 13_500, 15_000, 17_000]
    pages = _pages((0, 2, 0, 1), (1, 2, 2, 3))

    out = _run(
        begins,
        ends,
        pages,
        auto_entry_reserve_ms=[250] * 4,
        entry_animation_ms=[300] * 4,
        exit_animation_ms=[300] * 4,
    )

    # 相邻段落仍可能在画面上相撞。时间压缩先让上一页的稳定文字阶段
    # 与下一页稳定文字阶段恰好相接，但保留允许互相穿越的退/入场动画。
    previous_stable_end = out.ends[1] - 300
    incoming_stable_start = out.starts[2] + 300
    assert previous_stable_end == incoming_stable_start == 13_500
    assert out.ends[1] > out.starts[2]
    assert out.ends[1] == 13_800
    assert out.starts[2] == 13_200


def test_manual_override_wins_and_is_visible_to_later_pages():
    begins, ends = [10_000, 14_000, 30_000, 34_000], [13_000, 17_000, 33_000, 37_000]
    pages = _pages((0, 2, 0, 1), (0, 2, 2, 3))
    overrides = [(None, None), (None, 25_000), (None, None), (None, None)]
    out = _run(begins, ends, pages, overrides=overrides)

    assert out.ends[1] == 25_000  # 覆盖生效
    # 下一页下行按被覆盖后的时刻排队，而不是按自动算出来的 18_000。
    assert out.starts[3] == 25_000 + INTERVAL


# ---------------------------------------------------------------------------
# ForceBottom（单行底部页的行位）
# ---------------------------------------------------------------------------


def test_single_bottom_page_takes_the_bottom_row_when_nothing_overlaps():
    begins, ends = [10_000, 40_000], [13_000, 43_000]
    out = _run(begins, ends, _pages((0, 2, 0), (0, 2, 1)))

    assert out.force_bottom[1] is True


def test_single_bottom_page_is_pushed_up_when_the_previous_page_still_shows():
    # 前一页单行页挂到 13_000 + PostTime = 14_000，本页 14_000 − 1800 前就要上屏
    # → 重叠 → ForceBottom = False（N3 把它上移一行）。
    begins, ends = [10_000, 14_500], [13_000, 17_000]
    out = _run(begins, ends, _pages((0, 2, 0), (0, 2, 1)))

    assert out.force_bottom[0] is True
    assert out.force_bottom[1] is False


def test_empty_input_returns_empty_result():
    out = _run([], [], [])
    assert out.starts == [] and out.ends == [] and out.force_bottom == []
