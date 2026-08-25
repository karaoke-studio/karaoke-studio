"""Ruby reading-unit timing semantics shared by subtitle renderers."""

from __future__ import annotations

from PyQt6.QtGui import QFontMetrics

from krok_helper.subtitle_render.domain.models import (
    Style,
    effective_karaoke_animation,
)
from krok_helper.subtitle_render.engine.timing.timeline import char_fill_ratio
from krok_helper.subtitle_render.domain.timing import RubyAnnotation, TimingLine
from krok_helper.subtitle_render.engine.ruby.selection import (
    effective_ruby_for_target,
    ruby_target_indices,
)


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


def ruby_for_char_index(
    rubies: list[RubyAnnotation],
    line: TimingLine,
    intervals: list[tuple[int, int]],
    index: int,
) -> RubyAnnotation | None:
    """Return the first ruby annotation whose resolved target owns ``index``."""

    for ruby in rubies:
        if index in ruby_target_indices(ruby, line, intervals):
            return ruby
    return None


def resolve_char_ruby_groups(
    rubies: list[RubyAnnotation],
    line: TimingLine,
    intervals: list[tuple[int, int]],
) -> dict[int, tuple[list[int], RubyAnnotation]]:
    """Resolve each character to its first matching ruby group once per line.

    The mapping is frame-independent. ``setdefault`` preserves the established
    first-match-wins rule when authored ruby annotations overlap.
    """

    groups: dict[int, tuple[list[int], RubyAnnotation]] = {}
    for ruby in rubies:
        indices = ruby_target_indices(ruby, line, intervals)
        for index in indices:
            groups.setdefault(index, (indices, ruby))
    return groups


def ruby_main_uses_base_timing(
    line: TimingLine,
    indices: list[int],
) -> bool:
    """Return whether a ruby target preserves authored main-text checkpoints.

    N3 converts ruby time to kanji time only when the target has no internal
    explicit main-text boundary. The group's outer start/end do not count as
    internal boundaries.
    """

    valid = [index for index in indices if 0 <= index < len(line.chars)]
    if len(valid) <= 1:
        return False
    last_offset = len(valid) - 1
    for offset, index in enumerate(valid):
        char = line.chars[index]
        if offset > 0 and char.explicit_start:
            return True
        if offset < last_offset and char.explicit_end:
            return True
    return False


def is_utopia_group_marker(ruby: RubyAnnotation) -> bool:
    """Return whether ``ruby`` is SUG's linked-phrase-only pause marker."""

    return ruby.reading.strip() in {"", "^"} and all(
        not part.strip() or part.strip() == "^" for part in ruby.reading_parts
    )


def character_fill_ratio(
    line: TimingLine,
    intervals: list[tuple[int, int]],
    char_x_ranges: list[tuple[int, int]],
    active_rubies: list[RubyAnnotation],
    index: int,
    t_ms: int,
    *,
    groups: dict[int, tuple[list[int], RubyAnnotation]] | None = None,
    ruby_main_progress_mode: str = "checkpoint_segments",
) -> float:
    """Return one main-text character's sung ratio under ruby timing rules."""

    if groups is not None:
        entry = groups.get(index)
        ruby = entry[1] if entry is not None else None
        raw_indices = entry[0] if entry is not None else None
    else:
        ruby = ruby_for_char_index(active_rubies, line, intervals, index)
        raw_indices = (
            ruby_target_indices(ruby, line, intervals)
            if ruby is not None
            else None
        )

    # SUG's pause-only linked-phrase marker groups Utopia motion but must not
    # replace the underlying syllable or character clock for main-text wipe.
    if ruby is not None and is_utopia_group_marker(ruby):
        ruby = None
        raw_indices = None
    if ruby is not None:
        indices = [
            candidate
            for candidate in raw_indices
            if 0 <= candidate < len(char_x_ranges)
        ]
        if indices and not ruby_main_uses_base_timing(line, indices):
            effective_ruby = effective_ruby_for_target(ruby, indices, intervals)
            if (
                ruby_main_progress_mode == "reading_units"
                and _ruby_visual_units_and_intervals(effective_ruby)
            ):
                base_index = indices.index(index)
                progress = _main_text_ruby_progress_ratio(
                    effective_ruby,
                    t_ms,
                    mode="reading_units",
                )
                return max(
                    0.0,
                    min(1.0, progress * len(indices) - base_index),
                )
            group_left = min(char_x_ranges[candidate][0] for candidate in indices)
            group_right = max(char_x_ranges[candidate][1] for candidate in indices)
            fill_end = group_left + (
                group_right - group_left
            ) * _main_text_ruby_progress_ratio(effective_ruby, t_ms)
            char_left, char_right = char_x_ranges[index]
            width = max(char_right - char_left, 1)
            return max(0.0, min(1.0, (fill_end - char_left) / width))
    if index >= len(intervals):
        return 0.0
    start, end = intervals[index]
    return char_fill_ratio(start, end, t_ms)


def utopia_main_group_for_index(
    rubies: list[RubyAnnotation],
    line: TimingLine,
    intervals: list[tuple[int, int]],
    index: int,
    *,
    groups: dict[int, tuple[list[int], RubyAnnotation]] | None = None,
) -> tuple[list[int], RubyAnnotation] | None:
    """Return the multi-character ruby group driving Utopia at ``index``."""

    if groups is not None:
        entry = groups.get(index)
        if entry is None:
            return None
        raw_indices, ruby = entry
    else:
        ruby = ruby_for_char_index(rubies, line, intervals, index)
        if ruby is None:
            return None
        raw_indices = ruby_target_indices(ruby, line, intervals)
    indices = [
        candidate for candidate in raw_indices if 0 <= candidate < len(line.chars)
    ]
    if len(indices) <= 1:
        return None
    return indices, ruby


def utopia_wipe_window_for_index(
    line: TimingLine,
    intervals: list[tuple[int, int]],
    char_x_ranges: list[tuple[int, int]],
    groups: dict[int, tuple[list[int], RubyAnnotation]],
    index: int,
    style: Style,
    *,
    fallback_start: int,
    fallback_end: int,
) -> tuple[int, int]:
    """Map one base glyph to its Utopia visual wipe time window."""

    if effective_karaoke_animation(style) != "utopia":
        return fallback_start, fallback_end
    entry = groups.get(index)
    if entry is None:
        return fallback_start, fallback_end
    raw_indices, ruby = entry
    if is_utopia_group_marker(ruby):
        return fallback_start, fallback_end
    indices = [
        candidate
        for candidate in raw_indices
        if 0 <= candidate < len(char_x_ranges)
    ]
    if index not in indices or ruby_main_uses_base_timing(line, indices):
        return fallback_start, fallback_end

    effective_ruby = effective_ruby_for_target(ruby, indices, intervals)
    if (
        style.ruby_main_progress_mode == "reading_units"
        and _ruby_visual_units_and_intervals(effective_ruby)
    ):
        base_index = indices.index(index)
        return _ruby_main_text_slot_times(effective_ruby, base_index, len(indices))

    group_left = min(char_x_ranges[candidate][0] for candidate in indices)
    group_right = max(char_x_ranges[candidate][1] for candidate in indices)
    if group_right <= group_left:
        return fallback_start, fallback_end
    char_left, char_right = char_x_ranges[index]
    group_width = group_right - group_left
    start_ratio = (char_left - group_left) / group_width
    end_ratio = (char_right - group_left) / group_width
    start = _main_text_ruby_progress_time_at_ratio(
        effective_ruby,
        start_ratio,
        mode=style.ruby_main_progress_mode,
        plateau_side="right",
    )
    end = _main_text_ruby_progress_time_at_ratio(
        effective_ruby,
        end_ratio,
        mode=style.ruby_main_progress_mode,
        plateau_side="left",
    )
    return start, max(start, end)
