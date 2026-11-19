"""Resolve ruby clocks once into final main-text progress keyframes.

The projection mirrors the existing GPU's equal authored-cell shares. It does
not measure glyphs, change resolved intervals, or evaluate frame-time layout.
Ruby visual-unit intervals remain separate from the main-text clock.
"""
from dataclasses import dataclass
import math
import struct
import unicodedata
from strange_uta_game.backend.domain.graphemes import split_graphemes

from .timing import (ruby_main_wipe_intervals, ruby_visual_units_and_intervals,
                     ruby_main_uses_base_timing, is_utopia_group_marker)


WipeCurve = tuple[tuple[int, float], ...]


def _f32(value):
    """Match the existing GPU projection's intermediate float arithmetic."""
    return struct.unpack('f', struct.pack('f', value))[0]


def _gpu_boundary(value):
    # Fallback reading boundaries use C++ round(duration), not Python ties-to-even.
    return math.floor(value + .5)


@dataclass(frozen=True)
class RubyUnitClock:
    annotation_index: int
    units: tuple[str, ...]
    intervals: tuple[tuple[int, int], ...]

    @property
    def glyph_units(self):
        """GPU normalizes each timed unit, never across a part boundary."""
        return tuple(unicodedata.normalize('NFC', unit) for unit in self.units)


@dataclass(frozen=True)
class RubyWipeTiming:
    main: tuple[WipeCurve | None, ...]  # None retains the authoritative base interval.
    readings: tuple[RubyUnitClock, ...]


def ruby_unit_clock(ruby, annotation_index):
    """Final GPU visual-unit clock, shared by upper and lower readings."""
    units = ruby_visual_units_and_intervals(
        ruby, round_boundary=_gpu_boundary, split_reading_units=split_graphemes)
    return RubyUnitClock(annotation_index, tuple(u for u, _ in units),
                         tuple(interval for _, interval in units))


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
        raw.extend(((start, _f32(index / count)), (end, _f32((index + 1) / count))))
        previous = end

    def time_at(target, right):
        exact = [t for t, p in raw if abs(_f32(p-target)) < _f32(.0001)]
        if exact:
            return max(exact) if right else min(exact)
        for (t0, p0), (t1, p1) in zip(raw, raw[1:]):
            if p0 < target < p1:
                local = _f32(_f32(target-p0) / _f32(p1-p0))
                return t0 + math.floor(_f32(_f32(t1-t0) * local) + .5)
        return raw[0][0] if target <= 0 else raw[-1][0]

    result = []
    for index in range(base_count):
        left, right = _f32(index/base_count), _f32((index+1)/base_count)
        points = [(time_at(left, True), 0.)]
        points.extend((t, _f32(_f32(p-left)/_f32(right-left))) for t, p in raw
                      if p > _f32(left+_f32(.0001)) and p < _f32(right-_f32(.0001)))
        points.append((time_at(right, False), 1.))
        result.append(tuple(sorted(points, key=lambda p:p[0])))
    return tuple(result)


def prepare_ruby_wipe_timing(line, prepared_line, mode):
    return prepare_ruby_target_timing(line, (geometry.target for geometry in prepared_line.rubies), mode)


def prepare_ruby_target_timing(line, targets, mode):
    """Consume selected targets in source order; first overlapping group wins.

    Explicit internal main boundaries preserve base timing and still reserve
    group ownership, matching the GPU projection. Outer boundaries do not.
    """
    main, readings, assigned = [None] * len(line.chars), [], set()
    for target in targets:
        ruby, indices = target.effective, target.indices
        if not indices or any(type(i) is not int or not 0 <= i < len(line.chars) for i in indices):
            raise ValueError('注音走字目标超出正文单元范围')
        readings.append(ruby_unit_clock(ruby, target.annotation_index))
        if getattr(target, 'lower', False) or is_utopia_group_marker(ruby) or assigned.intersection(indices):
            continue
        if not ruby_main_uses_base_timing(line, list(indices)):
            first, last = min(indices), max(indices)
            curves = main_wipe_points(ruby_main_wipe_intervals(ruby, mode, round_boundary=_gpu_boundary,
                                                            split_reading_units=split_graphemes), last-first+1)
            main[first:last+1] = curves
        assigned.update(indices)
    return RubyWipeTiming(tuple(main), tuple(readings))


def wipe_points_to_ir(points):
    return [{'time_ms':t, 'position':p} for t,p in points]
