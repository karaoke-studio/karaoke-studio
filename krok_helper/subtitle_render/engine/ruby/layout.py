"""N3-compatible ruby unit measurement, alignment and horizontal layout."""

from __future__ import annotations

import math
from dataclasses import replace

from PyQt6.QtGui import QFont, QFontMetrics

from krok_helper.subtitle_render.engine.layout.layout_context import _LAYOUT_PASS
from krok_helper.subtitle_render.engine.ruby.selection import (
    effective_ruby_for_target,
    ruby_target_indices,
)
from krok_helper.subtitle_render.engine.ruby.style import (
    build_ruby_font,
    build_ruby_font_for_text,
    ruby_font_size,
    ruby_script_stroke_style,
    ruby_stroke2_width,
    ruby_stroke_width,
    ruby_style_for_target_indices,
)
from krok_helper.subtitle_render.engine.ruby.timing import _ruby_utopia_visual_units
from krok_helper.subtitle_render.engine.text import (
    char_layout_width,
    char_path_left_offset,
    letter_spacing,
    truncate_div,
)
from krok_helper.subtitle_render.engine.text import char_left_positions
from krok_helper.subtitle_render.engine.timing.timeline import compute_char_intervals
from krok_helper.subtitle_render.models import Style
from krok_helper.subtitle_render.timing import RubyAnnotation, TimingLine


_RUBY_MEASURE_CACHE: dict[tuple, tuple[QFont, Style]] = {}
_RUBY_MEASURE_CACHE_MAX = 64
_RUBY_UNIT_LAYOUT_CACHE: dict[tuple, list[tuple[str, float, float]]] = {}
_RUBY_UNIT_LAYOUT_CACHE_MAX = 4096


def ruby_interval_px(style: Style | None) -> int:
    return int(getattr(style, "ruby_interval_px", 0) or 0)


def resolve_ruby_alignment(
    style: Style | None,
    base_text: str | None,
    reading: str,
) -> str:
    mode = str(getattr(style, "ruby_alignment", "auto") or "auto")
    if mode in {"center", "equal_space"}:
        return mode
    if (base_text and _is_ascii_alnum(base_text)) or _is_ascii_alnum(reading):
        return "center"
    return "equal_space"


def _is_ascii_alnum(text: str) -> bool:
    stripped = [char for char in text if not char.isspace()]
    return bool(stripped) and all(
        ord(char) < 128 and char.isalnum() for char in stripped
    )


def ruby_layout_gap(
    natural_width: float,
    unit_count: int,
    target_width: float,
    style: Style | None,
    base_text: str | None,
    reading: str,
) -> float:
    if unit_count <= 1:
        return 0.0
    interval = float(ruby_interval_px(style))
    if resolve_ruby_alignment(style, base_text, reading) == "center":
        return interval
    if target_width <= natural_width:
        gap = (target_width - natural_width) / (unit_count - 1)
    else:
        gap = (target_width - natural_width) / (unit_count + 1)
    return max(gap, interval)


def ruby_layout_width(
    reading: str,
    ruby_metrics: QFontMetrics,
    target_width: int | float | None,
    style: Style | None = None,
    base_text: str | None = None,
) -> float:
    units = _ruby_utopia_visual_units(reading)
    unit_layouts = ruby_unit_layouts(units, ruby_metrics, style)
    natural = sum(width for _unit, width, _offset in unit_layouts)
    interval = float(ruby_interval_px(style))
    if target_width is None:
        return natural + interval * max(len(units) - 1, 0)
    target = float(max(target_width, 0))
    if len(units) <= 1:
        return max(target, natural)
    gap = ruby_layout_gap(
        natural,
        len(units),
        target,
        style,
        base_text,
        reading,
    )
    return max(target, natural + gap * (len(units) - 1))


def ruby_layout_left_offset(
    reading: str,
    ruby_metrics: QFontMetrics,
    target_width: int | float | None,
    style: Style | None = None,
    base_text: str | None = None,
) -> float:
    if target_width is None:
        return 0.0
    units = _ruby_utopia_visual_units(reading)
    if not units:
        return 0.0
    unit_layouts = ruby_unit_layouts(units, ruby_metrics, style)
    natural = sum(width for _unit, width, _offset in unit_layouts)
    target = float(target_width)
    if len(units) <= 1:
        content_width = natural
    else:
        gap = ruby_layout_gap(
            natural,
            len(units),
            target,
            style,
            base_text,
            reading,
        )
        content_width = natural + gap * (len(units) - 1)
    return min((target - content_width) / 2.0, 0.0)


def ruby_layout_left_overhang(
    reading: str,
    ruby_metrics: QFontMetrics,
    target_width: int | float | None,
    style: Style | None = None,
    base_text: str | None = None,
) -> float:
    return max(
        0.0,
        -ruby_layout_left_offset(
            reading,
            ruby_metrics,
            target_width,
            style,
            base_text,
        ),
    )


def ruby_layout_units(
    units: list[str],
    ruby_metrics: QFontMetrics,
    x: int | float,
    target_width: int | float | None,
    *,
    style: Style | None = None,
    base_text: str | None = None,
) -> list[tuple[str, float, float]]:
    origins = ruby_layout_origins(
        units,
        ruby_metrics,
        x,
        target_width,
        style=style,
        base_text=base_text,
    )
    return [
        (unit, origin + offset, width)
        for unit, origin, width, offset in origins
    ]


def ruby_layout_origins(
    units: list[str],
    ruby_metrics: QFontMetrics,
    x: int | float,
    target_width: int | float | None,
    *,
    style: Style | None = None,
    base_text: str | None = None,
) -> list[tuple[str, float, float, float]]:
    """Return N3 ``CharPoint.X`` positions before path-bearing correction."""
    unit_layouts = ruby_unit_layouts(units, ruby_metrics, style)
    if not units:
        return []
    natural = sum(width for _unit, width, _offset in unit_layouts)
    interval = float(ruby_interval_px(style))
    if target_width is None:
        cursor = float(x)
        result: list[tuple[str, float, float, float]] = []
        for unit, width, offset in unit_layouts:
            result.append((unit, cursor, width, offset))
            cursor += width + interval
        return result

    target = float(target_width)
    if len(units) <= 1:
        unit, width, offset = unit_layouts[0]
        unit_left = float(x) + float(truncate_div(int(target - width), 2))
        return [(unit, unit_left, width, offset)]

    reading = "".join(units)
    alignment = resolve_ruby_alignment(style, base_text, reading)
    gap = ruby_layout_gap(
        natural,
        len(units),
        target,
        style,
        base_text,
        reading,
    )
    content_width = natural + gap * (len(units) - 1)
    if alignment == "center":
        cursor = float(x) + float(truncate_div(int(target - content_width), 2))
    else:
        cursor = float(x) + (target - content_width) / 2.0
    result = []
    for unit, width, offset in unit_layouts:
        origin = float(int(cursor)) if alignment == "equal_space" else cursor
        result.append((unit, origin, width, offset))
        cursor += width + gap
    return result


def ruby_layout_draw_bounds(
    units: list[str],
    ruby_metrics: QFontMetrics,
    x: int | float,
    target_width: int | float | None,
    *,
    style: Style | None = None,
    base_text: str | None = None,
) -> tuple[float, float]:
    """Return N3 ruby ``DrawLineLeft/DrawLineRight`` bounds."""
    origins = ruby_layout_origins(
        units,
        ruby_metrics,
        x,
        target_width,
        style=style,
        base_text=base_text,
    )
    if not origins:
        return float(x), float(x)
    return (
        min(origin for _unit, origin, _width, _offset in origins),
        max(origin + width for _unit, origin, width, _offset in origins),
    )


def _ruby_measure_key(style: Style) -> tuple:
    return (
        style.font_family,
        ruby_font_size(style),
        style.italic,
        ruby_stroke_width(style),
        ruby_stroke2_width(style),
        int(style.space_width_percent),
        bool(style.allow_biting),
    )


def _ruby_measure_resources(style: Style, key: tuple) -> tuple[QFont, Style]:
    cached = _RUBY_MEASURE_CACHE.get(key)
    if cached is not None:
        return cached
    ruby_font = build_ruby_font(style)
    measure_style = replace(
        style,
        font_size_px=ruby_font_size(style),
        stroke_width_px=ruby_stroke_width(style),
        stroke2_width_px=ruby_stroke2_width(style),
    )
    if len(_RUBY_MEASURE_CACHE) >= _RUBY_MEASURE_CACHE_MAX:
        _RUBY_MEASURE_CACHE.clear()
    _RUBY_MEASURE_CACHE[key] = (ruby_font, measure_style)
    return ruby_font, measure_style


def ruby_unit_layouts(
    units: list[str],
    ruby_metrics: QFontMetrics,
    style: Style | None,
) -> list[tuple[str, float, float]]:
    if style is None:
        return [
            (unit, float(ruby_metrics.horizontalAdvance(unit)), 0.0)
            for unit in units
        ]
    measure_key = _ruby_measure_key(style)
    metrics_signature = (
        ruby_metrics.height(),
        ruby_metrics.ascent(),
        ruby_metrics.averageCharWidth(),
        ruby_metrics.maxWidth(),
    )
    layout_key = (tuple(units), metrics_signature, measure_key)
    cached = _RUBY_UNIT_LAYOUT_CACHE.get(layout_key)
    if cached is not None:
        return cached
    ruby_font, measure_style = _ruby_measure_resources(style, measure_key)
    result = [
        (
            unit,
            float(
                char_layout_width(
                    unit,
                    ruby_font,
                    ruby_metrics,
                    ruby_metrics,
                    None,
                    measure_style,
                )
            ),
            char_path_left_offset(
                unit,
                ruby_font,
                ruby_metrics,
                ruby_metrics,
                None,
                measure_style,
            ),
        )
        for unit in units
    ]
    if len(_RUBY_UNIT_LAYOUT_CACHE) >= _RUBY_UNIT_LAYOUT_CACHE_MAX:
        _RUBY_UNIT_LAYOUT_CACHE.clear()
    _RUBY_UNIT_LAYOUT_CACHE[layout_key] = result
    return result


def ruby_char_gaps(
    line: TimingLine,
    char_widths: list[int],
    rubies: list[RubyAnnotation],
    style: Style,
    intervals: list[tuple[int, int]] | None = None,
) -> tuple[list[int], int, int]:
    """Return ruby collision gaps and left/right line-box overflow."""
    cache = getattr(_LAYOUT_PASS, "ruby_gaps", None)
    if cache is None:
        return _ruby_char_gaps_uncached(
            line,
            char_widths,
            rubies,
            style,
            intervals,
        )
    cache_key = (
        id(line),
        tuple(char_widths),
        tuple(id(ruby) for ruby in rubies),
        id(style),
        None if intervals is None else tuple(intervals),
    )
    hit = cache.get(cache_key)
    if hit is None:
        gaps, left, right = _ruby_char_gaps_uncached(
            line,
            char_widths,
            rubies,
            style,
            intervals,
        )
        hit = (tuple(gaps), left, right)
        cache[cache_key] = hit
        _LAYOUT_PASS.lines.append(line)
        _LAYOUT_PASS.ruby_lists.append(list(rubies))
        _LAYOUT_PASS.styles.append(style)
    return list(hit[0]), hit[1], hit[2]


def _ruby_char_gaps_uncached(
    line: TimingLine,
    char_widths: list[int],
    rubies: list[RubyAnnotation],
    style: Style,
    intervals: list[tuple[int, int]] | None = None,
) -> tuple[list[int], int, int]:
    zero = [0] * len(char_widths)
    if not rubies or not line.chars or style.vertical or style.right_to_left:
        return zero, 0, 0
    if intervals is None:
        intervals = compute_char_intervals(line, char_widths)
    spacing = letter_spacing(style)
    interval = ruby_interval_px(style)

    entries: list[tuple[int, int, RubyAnnotation, RubyAnnotation, Style]] = []
    for ruby in rubies:
        indices = ruby_target_indices(ruby, line, intervals)
        if not indices:
            continue
        paint_ruby = effective_ruby_for_target(ruby, indices, intervals)
        target_style = ruby_style_for_target_indices(style, line, indices)
        ruby_style = ruby_script_stroke_style(
            target_style,
            paint_ruby.reading,
        )
        entries.append(
            (
                min(indices),
                max(indices),
                paint_ruby,
                ruby,
                ruby_style,
            )
        )
    if not entries:
        return zero, 0, 0
    entries.sort(key=lambda item: item[0])

    gaps = [0] * len(char_widths)

    def char_span(first: int, last: int) -> tuple[float, float]:
        lefts = char_left_positions(
            char_widths,
            0,
            False,
            spacing,
            char_gaps=gaps,
            n3_no_backtracking=style.layout_semantics == "n3_1074",
        )
        return float(lefts[first]), float(lefts[last] + char_widths[last])

    previous_right: float | None = None
    min_ruby_left = 0.0
    max_ruby_right = 0.0
    for first, last, paint_ruby, ruby, ruby_style in entries:
        span_left, span_right = char_span(
            first,
            min(last, len(char_widths) - 1),
        )
        target_width = max(span_right - span_left, 1.0)
        ruby_metrics = QFontMetrics(
            build_ruby_font_for_text(ruby_style, paint_ruby.reading)
        )
        ruby_left, ruby_right = ruby_layout_draw_bounds(
            _ruby_utopia_visual_units(paint_ruby.reading),
            ruby_metrics,
            span_left,
            target_width,
            style=ruby_style,
            base_text=ruby.kanji,
        )
        if previous_right is not None and first > 0:
            deficit = (previous_right + interval) - ruby_left
            if deficit > 0:
                push = int(math.ceil(deficit))
                gaps[first] += push
                ruby_left += push
                ruby_right += push
        previous_right = ruby_right
        min_ruby_left = min(min_ruby_left, ruby_left)
        max_ruby_right = max(max_ruby_right, ruby_right)

    if style.layout_semantics == "n3_1074":
        lefts = char_left_positions(
            char_widths,
            0,
            False,
            spacing,
            char_gaps=gaps,
            n3_no_backtracking=True,
        )
        text_width = float(lefts[-1] + char_widths[-1])
    else:
        text_width = float(
            sum(char_widths)
            + spacing * max(len(char_widths) - 1, 0)
            + sum(gaps)
        )
    left_overflow = max(0, int(math.ceil(-min_ruby_left)))
    right_overflow = max(0, int(math.ceil(max_ruby_right - text_width)))
    return gaps, left_overflow, right_overflow


__all__ = [
    "resolve_ruby_alignment",
    "ruby_interval_px",
    "ruby_char_gaps",
    "ruby_layout_draw_bounds",
    "ruby_layout_gap",
    "ruby_layout_left_offset",
    "ruby_layout_left_overhang",
    "ruby_layout_origins",
    "ruby_layout_units",
    "ruby_layout_width",
    "ruby_unit_layouts",
]
