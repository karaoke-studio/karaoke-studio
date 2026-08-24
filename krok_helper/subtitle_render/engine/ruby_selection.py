"""Select ruby annotations and map their authored spans to timing-line cells."""

from __future__ import annotations

from krok_helper.subtitle_render.engine.layout_context import _LAYOUT_PASS
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
    "ruby_has_global_position",
    "ruby_owns_line",
    "ruby_time_indices",
    "text_span_indices",
    "timed_ruby_matches_line",
]
