"""Title-overlay font selection and immutable layout contracts."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from PyQt6.QtGui import QFont, QFontMetrics

from krok_helper.subtitle_render.engine.style.title_semantics import (
    resolve_title_role_overlay,
    resolve_title_text,
)
from krok_helper.subtitle_render.engine.text import (
    clamp_weight,
    n3_char_box_ascent,
    n3_char_box_descent,
)
from krok_helper.subtitle_render.models import (
    Style,
    TitleOverlay,
    normalize_title_char_role_labels,
)
from krok_helper.subtitle_render.n3.font_catalog import resolve_qt_font_family
from krok_helper.subtitle_render.timing import TimingTrack


@dataclass(frozen=True)
class TitleGlyphLayout:
    text: str
    x: float
    advance: float
    font: QFont
    metrics: QFontMetrics
    title: TitleOverlay


@dataclass(frozen=True)
class TitleOverlayLayout:
    """Time-independent geometry for one title overlay."""

    lines: list[str]
    widths: list[float]
    block_w: float
    block_h: float
    line_h: float
    gap: int
    x0: float
    y_top: float
    font: QFont
    metrics: QFontMetrics
    latin_font: QFont
    latin_metrics: QFontMetrics
    font_for: Callable[[str], QFont] | None
    glyph_rows: list[list[TitleGlyphLayout]]
    line_heights: list[float]
    line_ascents: list[float]


def build_title_font(title: TitleOverlay) -> QFont:
    font = QFont(
        resolve_qt_font_family(title.font_family),
        max(title.font_size_px, 1),
    )
    font.setPixelSize(max(title.font_size_px, 1))
    font.setWeight(clamp_weight(title.font_weight))
    font.setItalic(title.italic)
    return font


def build_title_latin_font(title: TitleOverlay) -> QFont:
    family = title.font_family_latin or title.font_family
    font = QFont(resolve_qt_font_family(family), max(title.font_size_px, 1))
    font.setPixelSize(max(title.font_size_px, 1))
    font.setWeight(clamp_weight(title.font_weight))
    font.setItalic(title.italic)
    return font


def make_title_font_for(
    title: TitleOverlay,
    jp_font: QFont,
    latin_font: QFont,
) -> Callable[[str], QFont] | None:
    if not title.font_family_latin or latin_font.family() == jp_font.family():
        return None

    def font_for(text: str) -> QFont:
        return latin_font if (text and text.isascii()) else jp_font

    return font_for


def title_block_origin(
    img_w: int,
    img_h: int,
    block_w: float,
    block_h: float,
    title: TitleOverlay,
    *,
    edge_px: float = 0.0,
) -> tuple[float, float]:
    """Place a title block on its nine-grid anchor."""
    anchor = title.anchor
    half_edge = max(float(edge_px), 0.0) / 2.0
    if anchor.endswith("left"):
        x0 = title.offset_x + half_edge
    elif anchor.endswith("right"):
        x0 = img_w - block_w - title.offset_x - half_edge
    else:
        x0 = (img_w - block_w) / 2.0 + title.offset_x
    if anchor.startswith("top"):
        y_top = float(title.offset_y)
    elif anchor.startswith("bottom"):
        y_top = img_h - block_h - title.offset_y
    else:
        y_top = (img_h - block_h) / 2.0 + title.offset_y
    return x0, y_top


def layout_title_overlay(
    img_w: int,
    img_h: int,
    track: TimingTrack,
    title: TitleOverlay,
    *,
    style: Style | None = None,
) -> TitleOverlayLayout | None:
    text = resolve_title_text(title, track)
    lines = text.split("\n")
    if not any(line.strip() for line in lines):
        return None
    font = build_title_font(title)
    metrics = QFontMetrics(font)
    latin_font = build_title_latin_font(title)
    font_for = make_title_font_for(title, font, latin_font)
    latin_metrics = QFontMetrics(latin_font) if font_for is not None else metrics
    labels = normalize_title_char_role_labels(text, title.char_role_labels)
    title_space_percent = max(
        10,
        min(
            int(style.space_width_percent if style is not None else Style.space_width_percent),
            100,
        ),
    )
    glyph_rows: list[list[TitleGlyphLayout]] = []
    widths: list[float] = []
    line_heights: list[float] = []
    line_ascents: list[float] = []
    max_edge = 0.0
    fallback_ascent = n3_char_box_ascent(
        metrics,
        title.font_size_px,
        title.stroke_width_px,
    )
    fallback_descent = n3_char_box_descent(
        metrics,
        title.font_size_px,
        title.stroke_width_px,
    )
    for row_index, text_line in enumerate(lines):
        glyphs: list[TitleGlyphLayout] = []
        cursor = 0.0
        max_ascent = 0.0
        max_descent = 0.0
        for char_index, char in enumerate(text_line):
            glyph_title = (
                resolve_title_role_overlay(
                    style,
                    title,
                    labels[row_index][char_index],
                )
                if style is not None
                else title
            )
            glyph_jp_font = build_title_font(glyph_title)
            glyph_latin_font = build_title_latin_font(glyph_title)
            glyph_font_for = make_title_font_for(
                glyph_title,
                glyph_jp_font,
                glyph_latin_font,
            )
            glyph_font = (
                glyph_font_for(char) if glyph_font_for is not None else glyph_jp_font
            )
            glyph_metrics = QFontMetrics(glyph_font)
            if char == " ":
                space_unit = glyph_font.pixelSize()
                if space_unit <= 0:
                    space_unit = max(int(glyph_title.font_size_px), 1)
                advance = float(space_unit * title_space_percent // 100)
            else:
                advance = float(glyph_metrics.horizontalAdvance(char))
            glyphs.append(
                TitleGlyphLayout(
                    text=char,
                    x=cursor,
                    advance=advance,
                    font=glyph_font,
                    metrics=glyph_metrics,
                    title=glyph_title,
                )
            )
            cursor += advance
            if char_index + 1 < len(text_line):
                cursor += int(glyph_title.letter_spacing_px)
            max_ascent = max(
                max_ascent,
                n3_char_box_ascent(
                    glyph_metrics,
                    glyph_title.font_size_px,
                    glyph_title.stroke_width_px,
                ),
            )
            max_descent = max(
                max_descent,
                n3_char_box_descent(
                    glyph_metrics,
                    glyph_title.font_size_px,
                    glyph_title.stroke_width_px,
                ),
            )
            max_edge = max(max_edge, float(max(glyph_title.stroke_width_px, 0)))
        if not glyphs:
            max_ascent = fallback_ascent
            max_descent = fallback_descent
        glyph_rows.append(glyphs)
        widths.append(cursor)
        line_ascents.append(max_ascent)
        line_heights.append(max_ascent + max_descent)
    block_w = max(widths) if widths else 0.0
    line_h = max(line_heights, default=metrics.height())
    gap = max(int(title.line_gap_px), 0)
    block_h = sum(line_heights) + gap * max(len(lines) - 1, 0)
    if block_w <= 0 or block_h <= 0:
        return None

    x0, y_top = title_block_origin(
        img_w,
        img_h,
        block_w,
        block_h,
        title,
        edge_px=max_edge,
    )
    return TitleOverlayLayout(
        lines=lines,
        widths=widths,
        block_w=block_w,
        block_h=float(block_h),
        line_h=line_h,
        gap=gap,
        x0=x0,
        y_top=y_top,
        font=font,
        metrics=metrics,
        latin_font=latin_font,
        latin_metrics=latin_metrics,
        font_for=font_for,
        glyph_rows=glyph_rows,
        line_heights=line_heights,
        line_ascents=line_ascents,
    )


__all__ = [
    "TitleGlyphLayout",
    "TitleOverlayLayout",
    "build_title_font",
    "build_title_latin_font",
    "layout_title_overlay",
    "make_title_font_for",
    "title_block_origin",
]
