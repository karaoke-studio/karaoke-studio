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
    is_n3_latin_text,
    letter_spacing,
    truncate_div,
)
from krok_helper.subtitle_render.engine.text import char_left_positions
from krok_helper.subtitle_render.engine.timing.timeline import compute_char_intervals
from krok_helper.subtitle_render.domain.models import Style
from krok_helper.subtitle_render.domain.timing import RubyAnnotation, TimingLine


_RUBY_MEASURE_CACHE: dict[tuple, tuple[QFont, Style]] = {}
_RUBY_MEASURE_CACHE_MAX = 64
_RUBY_UNIT_LAYOUT_CACHE: dict[tuple, list[tuple[str, float, float, float, float]]] = {}
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
    if _ruby_auto_prefers_center(base_text or "") or _ruby_auto_prefers_center(
        reading
    ):
        return "center"
    return "equal_space"


# auto 判定的「脚本字母」区间：假名 / CJK / 谚文 / 希腊 / 西里尔。
# 与 D2D text_semantics.cpp rubyAutoCenterLayout 的区间完全一致。
_RUBY_SCRIPT_LETTER_RANGES = (
    (0x0370, 0x04FF),
    (0x1100, 0x11FF),
    (0x3040, 0x30FF),
    (0x3130, 0x318F),
    (0x3400, 0x9FFF),
    (0xAC00, 0xD7A3),
    (0xF900, 0xFAFF),
)


def _ruby_auto_prefers_center(text: str) -> bool:
    """auto 模式的居中判定（CPU/D2D 同口径，字符区间枚举）。

    N3 原判定要求全文纯 ASCII 字母数字，含一个符号（如 ``e-bay`` 的连
    字符）的英数读音会整体落进均分、字母被撑开。现改为两条规则：

    1. 拉丁字母数字（含 Latin-1 带调字母）多于其他可见字符 → 居中；
    2. 全部可见字符均为符号（既非拉丁字母数字、也非脚本字母区间，
       即「全由符号和空格组成」）→ 居中。

    空白不参与计数；平手（拉丁 == 其他）按均分处理。
    """
    latin = 0
    other = 0
    script_letters = 0
    for char in text:
        if char.isspace():
            continue
        if is_n3_latin_text(char):
            latin += 1
        else:
            other += 1
        if any(
            low <= ord(char) <= high
            for low, high in _RUBY_SCRIPT_LETTER_RANGES
        ):
            script_letters += 1
    if latin > other:
        return True
    return text.strip() != "" and latin == 0 and script_letters == 0


def ruby_layout_gap(
    natural_width: float,
    unit_count: int,
    target_width: float,
    style: Style | None,
    base_text: str | None,
    reading: str,
) -> float:
    """Base inter-unit gap before per-pair stroke clearance floors."""
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


def ruby_pair_gap_floors(
    unit_layouts: list[tuple[str, float, float, float, float]],
    style: Style | None,
) -> list[float]:
    """相邻注音单元的最小净空下限，保证两层描边不互相叠压。

    N3 的 advance 契约只预留第一层描边；当二重描边宽于一重、且相邻单元
    几乎没有 side bearing（英数窄字母）时，描边外环会压到邻字。此处按
    「两层描边需要量 − 该对单元在 gap=0 时的墨迹间距」逐对补足缺口，
    墨迹间距按单元盒内墨迹边（控制点多边形口径）计算。注音字间距为负
    表示用户主动压紧（允许叠压），净空整体停用；假名对通常有足够的
    side bearing，下限为 0，布局不受影响。CPU/D2D 两侧同口径
    （d2d_backend_configure.cpp 的 ruby 排布段）。
    """
    pair_count = max(len(unit_layouts) - 1, 0)
    if pair_count == 0 or style is None:
        return [0.0] * pair_count
    if ruby_interval_px(style) < 0:
        return [0.0] * pair_count
    needed = float(ruby_stroke_width(style) + ruby_stroke2_width(style))
    floors: list[float] = []
    for (_unit, width, _offset, _ink_left, ink_right), (
        _next_unit,
        _next_width,
        _next_offset,
        next_ink_left,
        _next_ink_right,
    ) in zip(unit_layouts, unit_layouts[1:]):
        existing = max(width - ink_right, 0.0) + max(next_ink_left, 0.0)
        floors.append(max(needed - existing, 0.0))
    return floors


def _ruby_pair_gaps(
    unit_layouts: list[tuple[str, float, float, float, float]],
    style: Style | None,
    base_gap: float,
) -> list[float]:
    # floor <= 0 时净空不参与，base_gap（含负值 interval）原样生效；
    # 只有正净空才作为下限，而正净空只可能出现在 interval >= 0 时。
    return [
        base_gap if floor <= 0.0 else max(base_gap, floor)
        for floor in ruby_pair_gap_floors(unit_layouts, style)
    ]


def ruby_layout_width(
    reading: str,
    ruby_metrics: QFontMetrics,
    target_width: int | float | None,
    style: Style | None = None,
    base_text: str | None = None,
) -> float:
    units = _ruby_utopia_visual_units(reading)
    unit_layouts = ruby_unit_layouts(units, ruby_metrics, style)
    natural = sum(
        width for _unit, width, _offset, _ink_left, _ink_right in unit_layouts
    )
    interval = float(ruby_interval_px(style))
    if target_width is None:
        gaps = _ruby_pair_gaps(unit_layouts, style, interval)
        return natural + sum(gaps)
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
    gaps = _ruby_pair_gaps(unit_layouts, style, gap)
    return max(target, natural + sum(gaps))


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
    natural = sum(
        width for _unit, width, _offset, _ink_left, _ink_right in unit_layouts
    )
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
        content_width = natural + sum(_ruby_pair_gaps(unit_layouts, style, gap))
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
    natural = sum(
        width for _unit, width, _offset, _ink_left, _ink_right in unit_layouts
    )
    interval = float(ruby_interval_px(style))
    if target_width is None:
        gaps = _ruby_pair_gaps(unit_layouts, style, interval)
        cursor = float(x)
        result: list[tuple[str, float, float, float]] = []
        for index, (unit, width, offset, _ink_left, _ink_right) in enumerate(
            unit_layouts
        ):
            result.append((unit, cursor, width, offset))
            cursor += width + (gaps[index] if index < len(gaps) else 0.0)
        return result

    target = float(target_width)
    if len(units) <= 1:
        unit, width, offset, _ink_left, _ink_right = unit_layouts[0]
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
    gaps = _ruby_pair_gaps(unit_layouts, style, gap)
    content_width = natural + sum(gaps)
    if alignment == "center":
        cursor = float(x) + float(truncate_div(int(target - content_width), 2))
    else:
        cursor = float(x) + (target - content_width) / 2.0
    result = []
    for index, (unit, width, offset, _ink_left, _ink_right) in enumerate(
        unit_layouts
    ):
        origin = float(int(cursor)) if alignment == "equal_space" else cursor
        result.append((unit, origin, width, offset))
        cursor += width + (gaps[index] if index < len(gaps) else 0.0)
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
    "ruby_pair_gap_floors",
    "ruby_unit_layouts",
]
