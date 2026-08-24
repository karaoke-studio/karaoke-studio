"""Qt text measurement adapter for backend-independent line geometry policy."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from PyQt6.QtGui import QFontMetrics

from krok_helper.subtitle_render.engine.line_geometry import line_has_role_labels
from krok_helper.subtitle_render.engine.text_metrics import (
    build_font,
    build_latin_font,
    char_layout_width,
    line_text_width,
    make_font_for,
)
from krok_helper.subtitle_render.models import Style
from krok_helper.subtitle_render.timing import TimingLine, TimingTrack


RoleCharWidthsResolver = Callable[[TimingLine, Style], Sequence[int]]
VectorGlyphWidthResolver = Callable[[object, Style], int]
RubySpacingResolver = Callable[
    [TimingTrack, TimingLine, list[int], Style],
    tuple[Sequence[int], float, float],
]


def char_widths_for_intervals(
    line: TimingLine,
    style: Style,
    *,
    role_char_widths: RoleCharWidthsResolver,
    vector_glyph_width: VectorGlyphWidthResolver,
) -> list[int]:
    """Measure interval weights while delegating role and guide specifics."""
    if line_has_role_labels(line):
        return list(role_char_widths(line, style))
    font = build_font(style)
    metrics = QFontMetrics(font)
    latin_font = build_latin_font(style)
    font_for = make_font_for(style, font, latin_font)
    latin_metrics = QFontMetrics(latin_font) if font_for is not None else metrics
    return [
        (
            vector_glyph_width(char.vector_glyph, style)
            if char.vector_glyph is not None
            else char_layout_width(
                char.text,
                font,
                metrics,
                latin_metrics,
                font_for,
                style,
            )
        )
        for char in line.chars
    ]


def measure_guide_anchor_bounds(
    track: TimingTrack,
    line: TimingLine,
    style: Style,
    *,
    ruby_spacing: RubySpacingResolver,
) -> tuple[float, float]:
    """Measure the source text box from which vector guides grow."""
    font = build_font(style)
    metrics = QFontMetrics(font)
    latin_font = build_latin_font(style)
    font_for = make_font_for(style, font, latin_font)
    latin_metrics = QFontMetrics(latin_font) if font_for is not None else metrics
    char_widths = [
        char_layout_width(
            char.text,
            font,
            metrics,
            latin_metrics,
            font_for,
            style,
        )
        for char in line.chars
    ]
    char_gaps, ruby_left, ruby_right = ruby_spacing(
        track,
        line,
        char_widths,
        style,
    )
    text_width = line_text_width(char_widths, style) + sum(char_gaps)
    return -ruby_left, int(round(text_width)) + ruby_right


__all__ = ["char_widths_for_intervals", "measure_guide_anchor_bounds"]
