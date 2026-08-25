"""Backend-neutral text layout model built from shared Qt text measurements."""

from __future__ import annotations

from dataclasses import dataclass, replace

from PyQt6.QtCore import QRectF
from PyQt6.QtGui import QFont, QFontMetrics

from krok_helper.subtitle_render.engine.guide import vector_glyph_width
from krok_helper.subtitle_render.engine.style.style_semantics import style_for_role
from krok_helper.subtitle_render.engine.text.metrics import (
    build_font,
    build_latin_font,
    char_layout_width,
    char_path_left_offset,
    is_emoji_text,
    is_n3_latin_text,
    letter_spacing,
    make_font_for,
)
from krok_helper.subtitle_render.domain.models import (
    LYRICS_LAYOUT_CHAR_FIELDS,
    Style,
)
from krok_helper.subtitle_render.domain.timing import TimingLine


@dataclass(frozen=True)
class GlyphLayout:
    index: int
    text: str
    role_label: str | None
    style: Style
    font: QFont
    metrics: QFontMetrics
    left: int
    width: int
    path_offset_x: float = 0.0
    brush_style: Style | None = None
    vector_glyph: object | None = None


@dataclass(frozen=True)
class TextLayout:
    glyphs: list[GlyphLayout]
    total_width: int
    ascent: int
    descent: int
    height: int
    line_rect: QRectF


def char_left_positions(
    char_widths: list[int],
    base_x: int,
    rtl: bool,
    letter_spacing_px: int = 0,
    char_gaps: list[int] | None = None,
    n3_no_backtracking: bool = False,
) -> list[int]:
    """Return each character's left edge for LTR or RTL text flow."""
    lefts: list[int] = []
    total_width = sum(char_widths) + letter_spacing_px * max(
        len(char_widths) - 1,
        0,
    )
    if rtl:
        cursor = base_x + total_width
        for width in char_widths:
            cursor -= width
            lefts.append(cursor)
            advance = width + letter_spacing_px
            cursor -= (
                max(advance, 0) - width
                if n3_no_backtracking
                else letter_spacing_px
            )
    else:
        cursor = base_x
        for index, width in enumerate(char_widths):
            if char_gaps is not None and index < len(char_gaps):
                cursor += char_gaps[index]
            lefts.append(cursor)
            advance = width + letter_spacing_px
            cursor += max(advance, 0) if n3_no_backtracking else advance
    return lefts


def main_script_stroke_style(style: Style, text: str) -> Style:
    """Materialize the active script font slot's outlines into common fields."""
    if is_n3_latin_text(text):
        width = (
            style.stroke_width_px
            if style.latin_stroke_width_px is None
            or int(style.latin_stroke_width_px) <= 0
            else int(style.latin_stroke_width_px)
        )
        enabled = (
            style.stroke2_enabled
            if style.latin_stroke2_enabled is None
            else bool(style.latin_stroke2_enabled)
        )
        width2 = (
            style.stroke2_width_px
            if style.latin_stroke2_width_px is None
            or int(style.latin_stroke2_width_px) <= 0
            else int(style.latin_stroke2_width_px)
        )
    else:
        width = style.stroke_width_px
        enabled = style.stroke2_enabled
        width2 = style.stroke2_width_px
    effective_width2 = max(int(width2), 0) if enabled else 0
    if (
        int(style.stroke_width_px) == int(width)
        and int(style.stroke2_width_px) == effective_width2
    ):
        return style
    return replace(
        style,
        stroke_width_px=max(int(width), 0),
        stroke2_enabled=True,
        stroke2_width_px=effective_width2,
    )


def style_for_role_in_layout(style: Style, role_label: str | None) -> Style:
    """Apply role visuals while retaining the active layout's spacing fields."""
    role_style = style_for_role(style, role_label)
    return replace(
        role_style,
        **{name: getattr(style, name) for name in LYRICS_LAYOUT_CHAR_FIELDS},
    )


def build_text_layout(
    line: TimingLine,
    style: Style,
    *,
    x0: int,
    baseline_y: int,
    inline_styles: bool,
    char_gaps: list[int] | None = None,
) -> TextLayout:
    """Build immutable glyph positions for a normal or inline-role text line."""
    rtl = style.right_to_left
    measured: list[
        tuple[
            int,
            str,
            str | None,
            Style,
            Style,
            QFont,
            QFontMetrics,
            int,
            int,
            float,
            object | None,
        ]
    ] = []
    total_w = 0
    max_ascent = 0
    max_descent = 0
    plain_font = build_font(style) if not inline_styles else None
    plain_metrics = QFontMetrics(plain_font) if plain_font is not None else None
    plain_latin_font = build_latin_font(style) if not inline_styles else None
    plain_font_for = (
        make_font_for(style, plain_font, plain_latin_font)
        if plain_font is not None and plain_latin_font is not None
        else None
    )
    plain_latin_metrics = (
        QFontMetrics(plain_latin_font)
        if plain_font_for is not None and plain_latin_font is not None
        else plain_metrics
    )
    inline_resource_cache: dict[
        str | None,
        tuple[Style, str | None, QFont, QFontMetrics, object, QFontMetrics],
    ] = {}
    for index, char in enumerate(line.chars):
        if inline_styles:
            cached = inline_resource_cache.get(char.role_label)
            if cached is None:
                role_style = style_for_role_in_layout(style, char.role_label)
                role_label = char.role_label
                font = build_font(role_style)
                metrics = QFontMetrics(font)
                latin_font = build_latin_font(role_style)
                font_for = make_font_for(role_style, font, latin_font)
                latin_metrics = (
                    QFontMetrics(latin_font) if font_for is not None else metrics
                )
                cached = (
                    role_style,
                    role_label,
                    font,
                    metrics,
                    font_for,
                    latin_metrics,
                )
                inline_resource_cache[char.role_label] = cached
            role_style, role_label, font, metrics, font_for, latin_metrics = cached
        else:
            role_style = style
            role_label = None
            font = plain_font
            metrics = plain_metrics
            font_for = plain_font_for
            latin_metrics = plain_latin_metrics
            if font is None or metrics is None or latin_metrics is None:
                continue
        is_guide = char.vector_glyph is not None
        glyph_style = (
            role_style
            if is_guide
            else main_script_stroke_style(role_style, char.text)
        )
        glyph_font = (
            font
            if is_guide
            else font_for(char.text)
            if font_for is not None
            else font
        )
        glyph_metrics = (
            QFontMetrics(glyph_font)
            if not is_guide and is_emoji_text(char.text)
            else latin_metrics
            if not is_guide and font_for is not None and is_n3_latin_text(char.text)
            else metrics
        )
        width = (
            vector_glyph_width(char.vector_glyph, role_style)
            if is_guide
            else char_layout_width(
                char.text,
                font,
                metrics,
                latin_metrics,
                font_for,
                glyph_style,
            )
        )
        spacing_after = letter_spacing(role_style) if index < len(line.chars) - 1 else 0
        measured.append(
            (
                index,
                char.text,
                role_label,
                glyph_style,
                role_style,
                glyph_font,
                glyph_metrics,
                width,
                spacing_after,
                0.0
                if is_guide
                else char_path_left_offset(
                    char.text,
                    font,
                    metrics,
                    latin_metrics,
                    font_for,
                    glyph_style,
                ),
                char.vector_glyph,
            )
        )
        advance = width + spacing_after
        total_w += (
            max(advance, 0)
            if style.layout_semantics == "n3_1074" and spacing_after
            else advance
        )
        if char.text.strip():
            max_ascent = max(max_ascent, glyph_metrics.ascent())
            max_descent = max(max_descent, glyph_metrics.descent())

    if measured and max_ascent == 0 and max_descent == 0:
        fallback_metrics = measured[0][6]
        max_ascent = fallback_metrics.ascent()
        max_descent = fallback_metrics.descent()

    gap_total = (
        sum(char_gaps[: len(line.chars)])
        if char_gaps is not None and not rtl
        else 0
    )
    layout_total_w = total_w + gap_total
    glyphs: list[GlyphLayout] = []
    if rtl:
        cursor = x0 + layout_total_w
        for item in measured:
            (
                index,
                text,
                role_label,
                glyph_style,
                brush_style,
                glyph_font,
                metrics,
                width,
                spacing_after,
                path_offset_x,
                vector_glyph,
            ) = item
            cursor -= width
            glyphs.append(
                GlyphLayout(
                    index=index,
                    text=text,
                    role_label=role_label,
                    style=glyph_style,
                    font=glyph_font,
                    metrics=metrics,
                    left=cursor,
                    width=width,
                    path_offset_x=path_offset_x,
                    brush_style=brush_style,
                    vector_glyph=vector_glyph,
                )
            )
            advance = width + spacing_after
            cursor -= (
                max(advance, 0) - width
                if style.layout_semantics == "n3_1074" and spacing_after
                else spacing_after
            )
    else:
        cursor = x0
        for item in measured:
            (
                index,
                text,
                role_label,
                glyph_style,
                brush_style,
                glyph_font,
                metrics,
                width,
                spacing_after,
                path_offset_x,
                vector_glyph,
            ) = item
            if char_gaps is not None and index < len(char_gaps):
                cursor += char_gaps[index]
            glyphs.append(
                GlyphLayout(
                    index=index,
                    text=text,
                    role_label=role_label,
                    style=glyph_style,
                    font=glyph_font,
                    metrics=metrics,
                    left=cursor,
                    width=width,
                    path_offset_x=path_offset_x,
                    brush_style=brush_style,
                    vector_glyph=vector_glyph,
                )
            )
            advance = width + spacing_after
            cursor += (
                max(advance, 0)
                if style.layout_semantics == "n3_1074" and spacing_after
                else advance
            )

    height = max_ascent + max_descent
    line_rect = QRectF(
        float(x0),
        float(baseline_y - max_ascent),
        float(max(layout_total_w, 0)),
        float(max(height, 1)),
    )
    return TextLayout(
        glyphs=glyphs,
        total_width=max(layout_total_w, 0),
        ascent=max_ascent,
        descent=max_descent,
        height=max(height, 1),
        line_rect=line_rect,
    )


def build_role_text_layout(
    line: TimingLine,
    style: Style,
    *,
    x0: int,
    baseline_y: int,
    char_gaps: list[int] | None = None,
) -> TextLayout:
    return build_text_layout(
        line,
        style,
        x0=x0,
        baseline_y=baseline_y,
        inline_styles=True,
        char_gaps=char_gaps,
    )


def role_char_geometry_by_index(
    line: TimingLine,
    layout: TextLayout,
) -> tuple[list[int], list[tuple[int, int]]]:
    widths = [0 for _ in line.chars]
    ranges = [(0, 0) for _ in line.chars]
    for glyph in layout.glyphs:
        if 0 <= glyph.index < len(line.chars):
            widths[glyph.index] = glyph.width
            ranges[glyph.index] = (glyph.left, glyph.left + glyph.width)
    return widths, ranges


__all__ = [
    "GlyphLayout",
    "TextLayout",
    "build_role_text_layout",
    "build_text_layout",
    "char_left_positions",
    "main_script_stroke_style",
    "role_char_geometry_by_index",
    "style_for_role_in_layout",
]
