"""Resolve ruby clocks once into final main-text progress keyframes.

The projection mirrors the existing GPU's equal authored-cell shares. It does
not measure glyphs, change resolved intervals, or evaluate frame-time layout.
Ruby visual-unit intervals remain separate from the main-text clock.
"""
from dataclasses import dataclass
import math

from .timing import (ruby_main_wipe_intervals, ruby_visual_units_and_intervals,
                     ruby_main_uses_base_timing, is_utopia_group_marker)


WipeCurve = tuple[tuple[int, float], ...]


@dataclass(frozen=True)
class RubyUnitClock:
    annotation_index: int
    units: tuple[str, ...]
    intervals: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class RubyWipeTiming:
    main: tuple[WipeCurve | None, ...]  # None retains the authoritative base interval.
    readings: tuple[RubyUnitClock, ...]


def main_wipe_points(intervals, base_count):
    """Project progress onto equal base-cell slots, preserving pause plateaus.

    Interior time inversion uses the GPU's half-up duration rounding. The
    caller supplies already-resolved/clamped intervals, never glyph advances.
    """
    if not intervals or type(base_count) is not int or not 0 < base_count <= 65536 or len(intervals) > 2048:
        raise ValueError('分段走字缺少有效区间或主字单元')
    count = len(intervals)
    raw = []
    previous = intervals[0][0]
    for index, (start, end) in enumerate(intervals):
        if (type(start) is not int or type(end) is not int or start < previous
                or end < start or abs(start) > 86400000 or abs(end) > 86400000):
            raise ValueError('分段走字包含无效、反向或非整数区间')
        raw.extend(((start, index / count), (end, (index + 1) / count)))
        previous = end

    def time_at(target, right):
        exact = [t for t, p in raw if abs(p-target) < .0001]
        if exact:
            return max(exact) if right else min(exact)
        for (t0, p0), (t1, p1) in zip(raw, raw[1:]):
            if p0 < target < p1:
                return t0 + math.floor((t1-t0) * (target-p0) / (p1-p0) + .5)
        return raw[0][0] if target <= 0 else raw[-1][0]

    result = []
    for index in range(base_count):
        left, right = index/base_count, (index+1)/base_count
        points = [(time_at(left, True), 0.)]
        points.extend((t, (p-left)/(right-left)) for t, p in raw
                      if p > left+.0001 and p < right-.0001)
        points.append((time_at(right, False), 1.))
        result.append(tuple(sorted(points, key=lambda p:p[0])))
    return tuple(result)


def prepare_ruby_wipe_timing(line, prepared_line, mode):
    """Consume selected targets in source order; first overlapping group wins.

    Explicit internal main boundaries preserve base timing and still reserve
    group ownership, matching the GPU projection. Outer boundaries do not.
    """
    main, readings, assigned = [None] * len(line.chars), [], set()
    for geometry in prepared_line.rubies:
        target = geometry.target
        ruby, indices = target.effective, target.indices
        if not indices or any(type(i) is not int or not 0 <= i < len(line.chars) for i in indices):
            raise ValueError('注音走字目标超出正文单元范围')
        timed_units = ruby_visual_units_and_intervals(ruby)
        readings.append(RubyUnitClock(target.annotation_index,
            tuple(u for u, _interval in timed_units), tuple(interval for _u, interval in timed_units)))
        if is_utopia_group_marker(ruby) or assigned.intersection(indices):
            continue
        if not ruby_main_uses_base_timing(line, list(indices)):
            first, last = min(indices), max(indices)
            curves = main_wipe_points(ruby_main_wipe_intervals(ruby, mode), last-first+1)
            main[first:last+1] = curves
        assigned.update(indices)
    return RubyWipeTiming(tuple(main), tuple(readings))


def wipe_points_to_ir(points):
    return [{'time_ms':t, 'position':p} for t,p in points]
