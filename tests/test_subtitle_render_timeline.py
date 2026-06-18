"""Tests for ``krok_helper.subtitle_render.engine.timeline``."""

from __future__ import annotations

from krok_helper.subtitle_render.engine.timeline import (
    char_fill_ratio,
    compute_char_intervals,
    compute_display_lines,
    find_active_line,
    find_upcoming_line,
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


def test_compute_display_lines_matches_two_lane_timing_model():
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
        max_hold_ms=12_000,
        continuity_snap_ms=800,
    )

    assert [(item.lane, item.display_start_ms, item.display_end_ms) for item in layouts] == [
        (0, 53_690, 60_440),
        (1, 56_690, 63_470),
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
        max_hold_ms=12_000,
        continuity_snap_ms=800,
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
        max_hold_ms=12_000,
        continuity_snap_ms=800,
    )

    assert layouts[0].display_end_ms >= line1.end_ms
    assert layouts[2].display_start_ms >= line1.end_ms + 300


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
