"""Ruby reading-unit timing semantics shared by subtitle renderers."""

from __future__ import annotations

from PyQt6.QtGui import QFontMetrics

from krok_helper.subtitle_render.engine.timing.timeline import char_fill_ratio
from krok_helper.subtitle_render.domain.timing import RubyAnnotation


_RUBY_COMBINING_CHARS = set(
    "ぁぃぅぇぉゃゅょゎァィゥェォャュョヮ\u3099\u309A"
)


def _ruby_progress_ratio(
    ruby: RubyAnnotation,
    t_ms: int,
    ruby_metrics: QFontMetrics | None = None,
) -> float:
    if not ruby.reading:
        return char_fill_ratio(ruby.pos_start_ms, ruby.pos_end_ms, t_ms)
    if not ruby.reading_part_ms:
        return char_fill_ratio(ruby.pos_start_ms, ruby.pos_end_ms, t_ms)

    parts, intervals = _ruby_progress_parts_and_intervals(ruby)
    if ruby_metrics is not None and len(parts) == len(intervals):
        weights = [max(float(ruby_metrics.horizontalAdvance(part)), 0.0) for part in parts]
    else:
        weights = [1.0] * len(intervals)
    total_weight = sum(weights)
    if total_weight <= 0.0:
        weights = [1.0] * len(intervals)
        total_weight = float(len(intervals))

    completed_weight = 0.0
    for weight, (start, end) in zip(weights, intervals):
        if t_ms < start:
            return completed_weight / total_weight
        if t_ms < end:
            local = char_fill_ratio(start, end, t_ms)
            return (completed_weight + weight * local) / total_weight
        completed_weight += weight
    return 1.0


def _ruby_progress_parts_and_intervals(
    ruby: RubyAnnotation,
) -> tuple[list[str], list[tuple[int, int]]]:
    """Return the exported ruby parts and their checkpoint intervals.

    Nicokara embeds one relative timestamp before every part after the first.
    Preserving those exact slices matters for SUG parity: a multi-character
    part owns its combined rendered width, while a part between consecutive
    timestamps is empty and consumes time without advancing the wipe.
    """
    parts = list(ruby.reading_parts)
    if (
        parts
        and len(parts) == len(ruby.reading_part_ms) + 1
        and "".join(parts) == ruby.reading
    ):
        start = int(ruby.pos_start_ms)
        end = max(start, int(ruby.pos_end_ms))
        anchors = [start]
        for relative_ms in ruby.reading_part_ms:
            timestamp = start + int(relative_ms)
            anchors.append(max(anchors[-1], min(end, timestamp)))
        anchors.append(max(anchors[-1], end))
        return parts, list(zip(anchors, anchors[1:]))

    units = _ruby_reading_units(ruby.reading)
    return units, _ruby_reading_intervals(ruby)


def _ruby_visual_units_and_intervals(
    ruby: RubyAnnotation,
) -> list[tuple[str, tuple[int, int]]]:
    """Expand exported ruby parts into N3 visual characters and exact times.

    An empty exported part is a real pause and therefore yields no geometry.
    Multi-character parts divide their own interval with integer boundaries;
    combining dakuten/handakuten stay attached to the preceding character.
    """
    parts, intervals = _ruby_progress_parts_and_intervals(ruby)
    result: list[tuple[str, tuple[int, int]]] = []
    for part, (start, end) in zip(parts, intervals):
        units = _ruby_utopia_visual_units(part)
        if not units:
            continue
        duration = max(int(end) - int(start), 0)
        count = len(units)
        for index, unit in enumerate(units):
            unit_start = int(start) + duration * index // count
            unit_end = int(start) + duration * (index + 1) // count
            result.append((unit, (unit_start, max(unit_start, unit_end))))
    return result


def ruby_visual_units_and_intervals(
    ruby: RubyAnnotation,
) -> list[tuple[str, tuple[int, int]]]:
    """Public timing contract for renderers that need visual ruby units."""
    return _ruby_visual_units_and_intervals(ruby)


def _ruby_reading_unit_progress_points(
    ruby: RubyAnnotation,
) -> list[tuple[int, float]]:
    """Return time/progress points for visible ruby characters.

    Every visible text element owns one equal spatial share.  Both ends of
    each element are retained so an empty exported part becomes a real
    progress plateau instead of being smoothed across as if it were text.
    """
    timed_units = _ruby_visual_units_and_intervals(ruby)
    if not timed_units:
        return []
    count = len(timed_units)
    raw_points: list[tuple[int, float]] = []
    for index, (_unit, (start, end)) in enumerate(timed_units):
        start = int(start)
        end = max(start, int(end))
        raw_points.append((start, index / count))
        raw_points.append((end, (index + 1) / count))

    # Zero-duration units can place multiple progress values at one time.
    # Keep the furthest value so the unit completes instantaneously there.
    points: list[tuple[int, float]] = []
    for timestamp, progress in raw_points:
        if points and timestamp == points[-1][0]:
            points[-1] = (timestamp, max(points[-1][1], progress))
        else:
            points.append((timestamp, progress))
    return points


def _reading_unit_progress_ratio(ruby: RubyAnnotation, t_ms: int) -> float | None:
    points = _ruby_reading_unit_progress_points(ruby)
    if not points:
        return None
    if t_ms < points[0][0]:
        return 0.0
    if t_ms >= points[-1][0]:
        return 1.0
    for (start_ms, start_progress), (end_ms, end_progress) in zip(
        points, points[1:]
    ):
        if t_ms < end_ms:
            if end_ms <= start_ms or end_progress <= start_progress:
                return start_progress
            local = (t_ms - start_ms) / (end_ms - start_ms)
            return start_progress + (end_progress - start_progress) * local
    return 1.0


def _ruby_progress_time_at_ratio(
    ruby: RubyAnnotation,
    target: float,
    *,
    plateau_side: str,
) -> int:
    """Invert reading-unit progress, choosing either side of a pause plateau."""
    points = _ruby_reading_unit_progress_points(ruby)
    if not points:
        start = int(ruby.pos_start_ms)
        end = max(start, int(ruby.pos_end_ms))
        return int(round(start + (end - start) * max(0.0, min(1.0, target))))
    target = max(0.0, min(1.0, target))
    if target <= 0.0:
        return points[0][0]
    if target >= 1.0:
        return points[-1][0]

    exact_times = [timestamp for timestamp, progress in points if progress == target]
    if exact_times:
        return min(exact_times) if plateau_side == "left" else max(exact_times)
    for (start_ms, start_progress), (end_ms, end_progress) in zip(
        points, points[1:]
    ):
        if start_progress < target < end_progress:
            local = (target - start_progress) / (end_progress - start_progress)
            return int(round(start_ms + (end_ms - start_ms) * local))
    return points[-1][0]


def _ruby_main_text_slot_times(
    ruby: RubyAnnotation,
    base_index: int,
    base_count: int,
) -> tuple[int, int]:
    count = max(int(base_count), 1)
    start = _ruby_progress_time_at_ratio(
        ruby, base_index / count, plateau_side="right"
    )
    end = _ruby_progress_time_at_ratio(
        ruby, (base_index + 1) / count, plateau_side="left"
    )
    return start, max(start, end)


def _main_text_ruby_progress_ratio(
    ruby: RubyAnnotation,
    t_ms: int,
    *,
    mode: str = "checkpoint_segments",
) -> float:
    """Return the selected progress clock for the ruby's base text.

    The historical ``checkpoint_segments`` mode divides base-text progress
    equally by checkpoint segment.  ``reading_units`` instead gives every
    visible ruby text element one equal share and preserves empty-part pauses;
    callers then map that normalized reading position across the covered base
    characters.  Both clocks stay separate from :func:`_ruby_progress_ratio`,
    whose spatial weights follow rendered ruby widths.
    """
    if mode == "reading_units":
        reading_progress = _reading_unit_progress_ratio(ruby, t_ms)
        if reading_progress is not None:
            return reading_progress

    if not ruby.reading_part_ms:
        return char_fill_ratio(ruby.pos_start_ms, ruby.pos_end_ms, t_ms)

    start = int(ruby.pos_start_ms)
    end = max(start, int(ruby.pos_end_ms))
    anchors = [start]
    for relative_ms in ruby.reading_part_ms:
        timestamp = start + int(relative_ms)
        anchors.append(max(anchors[-1], min(end, timestamp)))
    anchors.append(max(anchors[-1], end))

    segment_count = len(anchors) - 1
    if segment_count <= 0:
        return 1.0
    if t_ms < anchors[0]:
        return 0.0
    if t_ms >= anchors[-1]:
        return 1.0
    for index in range(segment_count):
        segment_start = anchors[index]
        segment_end = anchors[index + 1]
        if t_ms < segment_end:
            duration = segment_end - segment_start
            local = (t_ms - segment_start) / duration if duration > 0 else 1.0
            local = max(0.0, min(1.0, local))
            return (index + local) / segment_count
    return 1.0


def _main_text_ruby_progress_time_at_ratio(
    ruby: RubyAnnotation,
    target: float,
    *,
    mode: str = "checkpoint_segments",
    plateau_side: str,
) -> int:
    if mode == "reading_units" and _ruby_visual_units_and_intervals(ruby):
        return _ruby_progress_time_at_ratio(
            ruby,
            target,
            plateau_side=plateau_side,
        )

    start = int(ruby.pos_start_ms)
    end = max(start, int(ruby.pos_end_ms))
    target = max(0.0, min(1.0, float(target)))
    if not ruby.reading_part_ms:
        return int(round(start + (end - start) * target))

    anchors = [start]
    for relative_ms in ruby.reading_part_ms:
        timestamp = start + int(relative_ms)
        anchors.append(max(anchors[-1], min(end, timestamp)))
    anchors.append(max(anchors[-1], end))

    segment_count = len(anchors) - 1
    if segment_count <= 0:
        return end
    if target <= 0.0:
        return anchors[0]
    if target >= 1.0:
        return anchors[-1]

    points = [
        (timestamp, index / segment_count)
        for index, timestamp in enumerate(anchors)
    ]
    exact_times = [timestamp for timestamp, progress in points if progress == target]
    if exact_times:
        return min(exact_times) if plateau_side == "left" else max(exact_times)
    for (start_ms, start_progress), (end_ms, end_progress) in zip(points, points[1:]):
        if start_progress <= target <= end_progress:
            if end_progress <= start_progress or end_ms <= start_ms:
                return start_ms if plateau_side == "left" else end_ms
            local = (target - start_progress) / (end_progress - start_progress)
            return int(round(start_ms + (end_ms - start_ms) * local))
    return anchors[-1]


def _ruby_reading_intervals(ruby: RubyAnnotation) -> list[tuple[int, int]]:
    units = _ruby_reading_units(ruby.reading)
    if len(ruby.reading_part_ms) >= 2 * max(len(units) - 1, 0):
        return _ruby_reading_intervals_with_pauses(ruby, len(units))
    result: list[tuple[int, int]] = []
    boundaries = _ruby_reading_boundaries(ruby, len(units))
    for index, _unit in enumerate(units):
        start = boundaries[index]
        end = boundaries[index + 1]
        if end < start:
            end = start
        result.append((start, end))
    return result


def _ruby_reading_intervals_with_pauses(
    ruby: RubyAnnotation,
    unit_count: int,
) -> list[tuple[int, int]]:
    if unit_count <= 0:
        return []
    intervals: list[tuple[int, int]] = []
    current_start = ruby.pos_start_ms
    for index in range(unit_count - 1):
        release = ruby.pos_start_ms + ruby.reading_part_ms[index * 2]
        next_start = ruby.pos_start_ms + ruby.reading_part_ms[index * 2 + 1]
        release = max(current_start, min(release, ruby.pos_end_ms))
        next_start = max(release, min(next_start, ruby.pos_end_ms))
        intervals.append((current_start, release))
        current_start = next_start
    intervals.append((current_start, max(current_start, ruby.pos_end_ms)))
    return intervals


def _ruby_utopia_reading_units_and_intervals(ruby: RubyAnnotation) -> list[tuple[str, tuple[int, int]]]:
    mora_units = _ruby_reading_units(ruby.reading)
    mora_intervals = _ruby_reading_intervals(ruby)
    result: list[tuple[str, tuple[int, int]]] = []
    for mora, (start, end) in zip(mora_units, mora_intervals):
        visual_units = _ruby_utopia_visual_units(mora)
        if len(visual_units) <= 1:
            result.append((mora, (start, end)))
            continue
        duration = max(end - start, 0)
        for index, visual in enumerate(visual_units):
            unit_start = start + round(duration * index / len(visual_units))
            unit_end = start + round(duration * (index + 1) / len(visual_units))
            result.append((visual, (unit_start, max(unit_start, unit_end))))
    return result


def _ruby_utopia_visual_units(text: str) -> list[str]:
    units: list[str] = []
    for ch in text:
        if units and ch in {"\u3099", "\u309A"}:
            units[-1] += ch
        else:
            units.append(ch)
    return units


def _ruby_reading_units(reading: str) -> list[str]:
    units: list[str] = []
    for ch in reading:
        if units and ch in _RUBY_COMBINING_CHARS:
            units[-1] += ch
        else:
            units.append(ch)
    return units


def _ruby_reading_boundaries(ruby: RubyAnnotation, unit_count: int) -> list[int]:
    if unit_count <= 0:
        return [ruby.pos_start_ms, ruby.pos_end_ms]
    boundaries = [ruby.pos_start_ms]
    for rel_ms in ruby.reading_part_ms[: max(unit_count - 1, 0)]:
        ts = ruby.pos_start_ms + rel_ms
        ts = max(boundaries[-1], min(ruby.pos_end_ms, ts))
        boundaries.append(ts)
    if len(boundaries) < unit_count:
        start = boundaries[-1]
        remaining = unit_count - len(boundaries) + 1
        for step in range(1, remaining):
            boundaries.append(start + round((ruby.pos_end_ms - start) * step / remaining))
    boundaries.append(max(boundaries[-1], ruby.pos_end_ms))
    return boundaries
