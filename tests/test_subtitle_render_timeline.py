"""Tests for ``krok_helper.subtitle_render.engine.timeline``."""

from __future__ import annotations

from krok_helper.subtitle_render.engine.timeline import (
    char_fill_ratio,
    compute_char_intervals,
    find_active_line,
    find_upcoming_line,
    track_duration_ms,
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
