"""Select ruby annotations and map their authored spans to timing-line cells."""

from __future__ import annotations

from dataclasses import replace

from krok_helper.subtitle_render.engine.layout.layout_context import _LAYOUT_PASS
from krok_helper.subtitle_render.engine.timeline import compute_char_intervals
from krok_helper.subtitle_render.timing import RubyAnnotation, TimingLine


def ruby_time_indices(
    ruby: RubyAnnotation,
    intervals: list[tuple[int, int]],
) -> list[int]:
    return [
        index
        for index, (start, end) in enumerate(intervals)
        if start < ruby.pos_end_ms and end > ruby.pos_start_ms
    ]


def text_span_indices(
    text_span: tuple[int, int],
    line: TimingLine,
) -> list[int]:
    span_start, span_end = text_span
    indices: list[int] = []
    cursor = 0
    for index, char in enumerate(line.chars):
        unit_start = cursor
        unit_end = cursor + len(char.text)
        cursor = unit_end
        if unit_start < span_end and unit_end > span_start:
            indices.append(index)
    return indices


def find_ruby_text_span(
    kanji: str,
    line: TimingLine,
    *,
    preferred_indices: list[int] | None = None,
) -> tuple[int, int] | None:
    if not kanji:
        return None
    text = "".join(char.text for char in line.chars)
    occurrences: list[tuple[int, int]] = []
    position = text.find(kanji)
    while position >= 0:
        occurrences.append((position, position + len(kanji)))
        position = text.find(kanji, position + 1)
    if not occurrences:
        return None
    if not preferred_indices:
        return occurrences[0]

    preferred = set(preferred_indices)

    def score(span: tuple[int, int]) -> tuple[int, int]:
        indices = text_span_indices(span, line)
        overlap = len(preferred.intersection(indices))
        distance = min(
            (
                abs(index - candidate)
                for index in indices
                for candidate in preferred
            ),
            default=0,
        )
        return overlap, -distance

    return max(occurrences, key=score)


def find_ruby_text_indices(
    kanji: str,
    line: TimingLine,
    *,
    preferred_indices: list[int] | None = None,
) -> list[int]:
    if not kanji:
        return []
    span = find_ruby_text_span(
        kanji,
        line,
        preferred_indices=preferred_indices,
    )
    if span is None:
        return []
    return text_span_indices(span, line)


def ruby_explicit_target_indices(
    ruby: RubyAnnotation,
    line: TimingLine,
) -> list[int] | None:
    """Return the loader-resolved target, or ``None`` to fall back to search."""
    start = ruby.target_char_start
    end = ruby.target_char_end
    if start is None or end is None:
        return None
    if start < 0 or end <= start or end > len(line.chars):
        return None
    if ruby.kanji:
        target_text = "".join(char.text for char in line.chars[start:end])
        if target_text != ruby.kanji:
            return None
    return list(range(start, end))


def ruby_target_indices(
    ruby: RubyAnnotation,
    line: TimingLine,
    intervals: list[tuple[int, int]],
) -> list[int]:
    explicit = ruby_explicit_target_indices(ruby, line)
    if explicit is not None:
        return explicit
    time_indices = ruby_time_indices(ruby, intervals)
    if ruby.kanji:
        return find_ruby_text_indices(
            ruby.kanji,
            line,
            preferred_indices=time_indices,
        )
    return time_indices


def effective_ruby_for_target(
    ruby: RubyAnnotation,
    indices: list[int],
    intervals: list[tuple[int, int]],
) -> RubyAnnotation:
    """Clamp an annotation's wipe clock to its resolved base-character span."""
    valid_indices = [index for index in indices if 0 <= index < len(intervals)]
    if not valid_indices:
        return ruby
    start = min(intervals[index][0] for index in valid_indices)
    end = max(intervals[index][1] for index in valid_indices)
    if (
        ruby.pos_end_ms > ruby.pos_start_ms
        and ruby.pos_start_ms >= start
        and start < ruby.pos_end_ms < end
    ):
        end = ruby.pos_end_ms
    if start == ruby.pos_start_ms and end == ruby.pos_end_ms:
        return ruby
    target_duration = max(end - start, 0)
    reading_part_ms = [
        max(0, min(target_duration, relative_ms))
        for relative_ms in ruby.reading_part_ms
    ]
    return replace(
        ruby,
        pos_start_ms=start,
        pos_end_ms=end,
        reading_part_ms=reading_part_ms,
    )


def ruby_text_span_x_range(
    text_span: tuple[int, int],
    line: TimingLine,
    char_x_ranges: list[tuple[int, int]],
) -> tuple[int, int] | None:
    span_start, span_end = text_span
    cursor = 0
    left: int | None = None
    right: int | None = None
    for index, char in enumerate(line.chars):
        if index >= len(char_x_ranges):
            break
        text_length = len(char.text)
        unit_start = cursor
        unit_end = cursor + text_length
        cursor = unit_end
        if (
            text_length <= 0
            or unit_end <= span_start
            or unit_start >= span_end
        ):
            continue
        overlap_start = max(span_start, unit_start) - unit_start
        overlap_end = min(span_end, unit_end) - unit_start
        char_left, char_right = char_x_ranges[index]
        width = char_right - char_left
        segment_left = char_left + round(width * overlap_start / text_length)
        segment_right = char_left + round(width * overlap_end / text_length)
        left = segment_left if left is None else min(left, segment_left)
        right = segment_right if right is None else max(right, segment_right)
    if left is None or right is None or right <= left:
        return None
    return left, right


def ruby_target_x_range(
    ruby: RubyAnnotation,
    line: TimingLine,
    intervals: list[tuple[int, int]],
    char_x_ranges: list[tuple[int, int]],
) -> tuple[int, int] | None:
    explicit = ruby_explicit_target_indices(ruby, line)
    if explicit is not None:
        return (
            min(char_x_ranges[index][0] for index in explicit),
            max(char_x_ranges[index][1] for index in explicit),
        )
    time_indices = ruby_time_indices(ruby, intervals)
    if ruby.kanji:
        text_span = find_ruby_text_span(
            ruby.kanji,
            line,
            preferred_indices=time_indices,
        )
        if text_span is None:
            return None
        return ruby_text_span_x_range(text_span, line, char_x_ranges)

    if not time_indices:
        return None
    left = min(char_x_ranges[index][0] for index in time_indices)
    right = max(char_x_ranges[index][1] for index in time_indices)
    return left, right


def ruby_owns_line(ruby: RubyAnnotation, line: TimingLine) -> bool:
    """Return whether loader-resolved ownership permits this timing line."""
    return (
        ruby.target_line_index is None
        or line.track_line_index is None
        or ruby.target_line_index == line.track_line_index
    )


def ruby_has_global_position(ruby: RubyAnnotation) -> bool:
    return ruby.pos_start_ms == 0 and ruby.pos_end_ms == 0


def timed_ruby_matches_line(
    ruby: RubyAnnotation,
    line: TimingLine,
    intervals: list[tuple[int, int]],
) -> bool:
    time_indices = ruby_time_indices(ruby, intervals)
    if not time_indices:
        return False
    if not ruby.kanji:
        return True
    preferred = set(time_indices)
    text = "".join(char.text for char in line.chars)
    position = text.find(ruby.kanji)
    while position >= 0:
        indices = text_span_indices(
            (position, position + len(ruby.kanji)),
            line,
        )
        if preferred.intersection(indices):
            return True
        position = text.find(ruby.kanji, position + 1)
    return False


def active_rubies_for_line(
    rubies: list[RubyAnnotation],
    line: TimingLine,
) -> list[RubyAnnotation]:
    """Return annotations active on one line through the layout-pass cache."""
    cache = getattr(_LAYOUT_PASS, "active_rubies", None)
    if cache is None:
        return _active_rubies_for_line_uncached(rubies, line)
    cache_key = (id(rubies), id(line))
    hit = cache.get(cache_key)
    if hit is None:
        hit = tuple(_active_rubies_for_line_uncached(rubies, line))
        cache[cache_key] = hit
        _LAYOUT_PASS.lines.append(line)
        _LAYOUT_PASS.ruby_lists.append(rubies)
    return list(hit)


def _active_rubies_for_line_uncached(
    rubies: list[RubyAnnotation],
    line: TimingLine,
) -> list[RubyAnnotation]:
    if not rubies or not line.chars:
        return []
    line_start = line.chars[0].start_ms
    line_end = line.end_ms if line.end_ms is not None else line.chars[-1].start_ms
    intervals = compute_char_intervals(line)
    return [
        ruby
        for ruby in rubies
        if ruby.reading
        and ruby_owns_line(ruby, line)
        and (
            ruby_has_global_position(ruby)
            or (
                ruby.pos_end_ms > line_start
                and ruby.pos_start_ms < line_end
                and timed_ruby_matches_line(ruby, line, intervals)
            )
        )
    ]


__all__ = [
    "active_rubies_for_line",
    "effective_ruby_for_target",
    "find_ruby_text_indices",
    "find_ruby_text_span",
    "ruby_has_global_position",
    "ruby_explicit_target_indices",
    "ruby_owns_line",
    "ruby_target_indices",
    "ruby_target_x_range",
    "ruby_text_span_x_range",
    "ruby_time_indices",
    "text_span_indices",
    "timed_ruby_matches_line",
]
