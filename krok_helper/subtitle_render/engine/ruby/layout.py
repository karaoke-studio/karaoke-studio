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
    ruby_italic_value,
    ruby_latin_italic_value,
    ruby_script_stroke_style,
    ruby_stroke2_width,
    ruby_stroke_width,
    ruby_style_for_target_indices,
)
from krok_helper.subtitle_render.engine.ruby.timing import _ruby_utopia_visual_units
from krok_helper.subtitle_render.engine.text import (
    char_glyph_ink_box,
    char_layout_width,
    char_path_left_offset,
    letter_spacing,
)
from krok_helper.subtitle_render.engine.text import char_left_positions
from krok_helper.subtitle_render.engine.timing.timeline import compute_char_intervals
from krok_helper.subtitle_render.domain.models import Style
from krok_helper.subtitle_render.domain.timing import RubyAnnotation, TimingLine


from .unit_layout import (
    ruby_interval_px,
    resolve_ruby_alignment,
    ruby_layout_gap,
    ruby_pair_gap_floors,
    measured_ruby_width,
    measured_ruby_left_offset,
    measured_ruby_origins,
)

_RUBY_MEASURE_CACHE: dict[tuple, tuple[QFont, Style]] = {}
_RUBY_MEASURE_CACHE_MAX = 64
_RUBY_UNIT_LAYOUT_CACHE: dict[tuple, list[tuple[str, float, float, float, float]]] = {}
_RUBY_UNIT_LAYOUT_CACHE_MAX = 4096


def ruby_layout_width(
    reading: str, ruby_metrics: QFontMetrics, target_width: int | float | None,
    style: Style | None = None, base_text: str | None = None,
) -> float:
    return measured_ruby_width(ruby_unit_layouts(_ruby_utopia_visual_units(reading), ruby_metrics, style),
                               target_width, style, base_text)


def ruby_layout_left_offset(
    reading: str, ruby_metrics: QFontMetrics, target_width: int | float | None,
    style: Style | None = None, base_text: str | None = None,
) -> float:
    if target_width is None or not reading:
        return 0.0
    return measured_ruby_left_offset(ruby_unit_layouts(_ruby_utopia_visual_units(reading), ruby_metrics, style),
                                     target_width, style, base_text)


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
    units: list[str], ruby_metrics: QFontMetrics, x: int | float, target_width: int | float | None,
    *, style: Style | None = None, base_text: str | None = None,
) -> list[tuple[str, float, float, float]]:
    """Qt measurement adapter for shared N3 ruby placement."""
    return measured_ruby_origins(ruby_unit_layouts(units, ruby_metrics, style), x, target_width,
                                 style=style, base_text=base_text)


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
    # 覆盖 build_ruby_font_for_text 解析逐单元字体所需的全部字段：拉丁单元
    # 的宽度随拉丁族/字号/字重/stretch/斜体变化，漏一项就会命中过期缓存。
    return (
        style.font_family,
        style.font_family_latin,
        int(style.font_weight),
        bool(style.ruby_font_follow_main),
        style.ruby_font_family,
        style.ruby_font_family_latin,
        style.ruby_font_weight,
        style.ruby_latin_font_size_px,
        style.ruby_latin_font_weight,
        ruby_font_size(style),
        style.italic,
        ruby_italic_value(style),
        ruby_latin_italic_value(style),
        style.latin_font_stretch_pct,
        style.ruby_latin_font_stretch_pct,
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
) -> list[tuple[str, float, float, float, float]]:
    """Return per-unit ``(unit, box_width, pen_offset, ink_left, ink_right)``.

    ink_left/ink_right 是墨迹盒（控制点多边形口径）在单元盒内的左右边，
    供 :func:`ruby_pair_gap_floors` 计算相邻描边净空；空墨迹单元退化为
    pen_offset 处的零宽墨迹。
    """
    if style is None:
        return [
            (
                unit,
                float(ruby_metrics.horizontalAdvance(unit)),
                0.0,
                0.0,
                float(ruby_metrics.horizontalAdvance(unit)),
            )
            for unit in units
        ]
    measure_key = _ruby_measure_key(style)
    # 宽度只依赖 (units, style 字段)：逐单元字体/度量全部由 style 派生，
    # 调用方传入的 ruby_metrics 不再参与测量，无需进键。
    layout_key = (tuple(units), measure_key)
    cached = _RUBY_UNIT_LAYOUT_CACHE.get(layout_key)
    if cached is not None:
        return cached
    _ruby_font, measure_style = _ruby_measure_resources(style, measure_key)
    # GPU 对齐：D2D 逐读音单元按脚本选测量字体且 measureFace == drawingFace
    # （d2d_backend_configure.cpp）。拉丁单元必须用拉丁字体测量，否则
    # 「日文墨水 × 拉丁 advance」的混合盒子会窄于实际绘制字形，相邻字母
    # 描边互相叠压（2026-09 英数注音挤压问题）。metrics 一律取自单元字体，
    # 不复用调用方传入的 ruby_metrics——它与日文 ruby 字体的对应关系没有保证。
    result = []
    for unit in units:
        unit_font = build_ruby_font_for_text(style, unit)
        unit_metrics = QFontMetrics(unit_font)
        width = float(char_layout_width(
            unit, unit_font, unit_metrics, unit_metrics, None, measure_style,
        ))
        offset = float(char_path_left_offset(
            unit, unit_font, unit_metrics, unit_metrics, None, measure_style,
        ))
        ink_box = char_glyph_ink_box(
            unit, unit_font, unit_metrics, unit_metrics, None
        )
        if ink_box is None:
            ink_left = ink_right = offset
        else:
            ink_left = offset + float(ink_box[0])
            ink_right = offset + float(ink_box[2])
        result.append((unit, width, offset, ink_left, ink_right))
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
    if (not rubies and not style.lower_ruby_enabled) or not line.chars or style.vertical or style.right_to_left:
        return zero, 0, 0
    if intervals is None:
        intervals = compute_char_intervals(line, char_widths)
    spacing = letter_spacing(style)
    interval = ruby_interval_px(style)

    lower_rubies = []
    if style.lower_ruby_enabled:
        from krok_helper.subtitle_render.engine.ruby.lower import lower_rubies_for_line
        lower_rubies = lower_rubies_for_line(line, rubies, intervals, style)
    entries: list[tuple[int, int, RubyAnnotation, RubyAnnotation, Style]] = []
    for ruby in [*rubies, *lower_rubies]:
        indices = ruby_target_indices(ruby, line, intervals)
        if not indices:
            continue
        paint_ruby = effective_ruby_for_target(ruby, indices, intervals)
        target_style = (ruby.render_style if ruby in lower_rubies
                        else ruby_style_for_target_indices(style, line, indices))
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
    entries.sort(key=lambda item: (item[3] in lower_rubies, item[0]))

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
    previous_lower = False
    for first, last, paint_ruby, ruby, ruby_style in entries:
        is_lower = ruby in lower_rubies
        if is_lower != previous_lower:
            previous_right = None
        previous_lower = is_lower
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
    "ruby_pair_gap_floors",
    "ruby_unit_layouts",
]
