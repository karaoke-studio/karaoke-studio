"""Frame-independent glyph geometry for horizontal subtitle lines."""

from __future__ import annotations

from PyQt6.QtCore import QRectF
from PyQt6.QtGui import QPainterPath

from krok_helper.subtitle_render.domain.models import Style
from krok_helper.subtitle_render.engine.guide import guide_symbol_is_bitmap
from krok_helper.subtitle_render.engine.render.effects import (
    glow_concentration_level,
    glow_radius,
    karaoke_state_signature,
    main_stroke2_width,
    visual_text_padding,
)
from krok_helper.subtitle_render.engine.style.style_semantics import (
    effective_karaoke_colors,
)
from krok_helper.subtitle_render.engine.text import GlyphLayout, TextLayout
from krok_helper.subtitle_render.sources.guide_symbols import (
    scaled_guide_symbol_path,
)


def bitmap_guide_is_no_wipe(symbol: object | None) -> bool:
    return guide_symbol_is_bitmap(symbol) and not bool(
        getattr(symbol, "bitmap_after_path", None)
    )


def glyph_path(glyph: GlyphLayout, baseline_y: int) -> QPainterPath:
    if glyph.vector_glyph is not None:
        if guide_symbol_is_bitmap(glyph.vector_glyph):
            return QPainterPath()
        return scaled_guide_symbol_path(
            glyph.vector_glyph,
            pixel_size=max(int(glyph.font.pixelSize()), 1),
            left=float(glyph.left),
            baseline_y=float(baseline_y),
        )
    path = QPainterPath()
    path.addText(
        float(glyph.left + glyph.path_offset_x),
        float(baseline_y),
        glyph.font,
        glyph.text,
    )
    return path


def role_visual_text_padding(layout: TextLayout) -> int:
    if not layout.glyphs:
        return 0
    return max(visual_text_padding(glyph.style) for glyph in layout.glyphs)


def resolve_role_baseline_y(
    layout: TextLayout,
    img_h: int,
    style: Style,
    ruby_extra: int = 0,
) -> int:
    pos = style.line_y_position
    margin = style.line_y_margin_px
    pad = role_visual_text_padding(layout)
    ruby_extra = max(int(ruby_extra), 0)
    if pos == "top":
        return margin + ruby_extra + pad + layout.ascent
    if pos == "center":
        block_h = layout.height + ruby_extra + pad * 2
        return (img_h - block_h) // 2 + ruby_extra + pad + layout.ascent
    return img_h - margin - pad - layout.descent


def clamp_role_baseline_y(
    baseline_y: int,
    layout: TextLayout,
    img_h: int,
    style: Style,
    ruby_extra: int = 0,
) -> int:
    pad = role_visual_text_padding(layout)
    ruby_extra = max(int(ruby_extra), 0)
    min_y = ruby_extra + pad + layout.ascent
    max_y = img_h - pad - layout.descent
    if max_y < min_y:
        return min_y
    return max(min_y, min(max_y, baseline_y))


def glyph_run_signature(glyph: GlyphLayout) -> tuple:
    colors = effective_karaoke_colors(glyph.style)
    return (
        karaoke_state_signature(colors.before),
        karaoke_state_signature(colors.after),
        glyph.style.shadow_offset_x,
        glyph.style.shadow_offset_y,
        glyph.style.stroke_width_px,
        glyph.style.stroke2_width_px,
        glyph.style.decoration_kind,
        glow_radius(glyph.style, after=False),
        glow_radius(glyph.style, after=True),
        glow_concentration_level(glyph.style),
    )


def glyph_runs(layout: TextLayout) -> list[list[GlyphLayout]]:
    runs: list[list[GlyphLayout]] = []
    current: list[GlyphLayout] = []
    current_signature: tuple | None = None
    signature_cache: dict[int, tuple] = {}
    for glyph in layout.glyphs:
        style_id = id(glyph.style)
        signature = signature_cache.get(style_id)
        if signature is None:
            signature = glyph_run_signature(glyph)
            signature_cache[style_id] = signature
        if not current or signature == current_signature:
            current.append(glyph)
            current_signature = signature
            continue
        runs.append(current)
        current = [glyph]
        current_signature = signature
    if current:
        runs.append(current)
    return runs


def glyph_is_bitmap_guide(glyph: GlyphLayout) -> bool:
    return guide_symbol_is_bitmap(glyph.vector_glyph)


def text_glyph_runs(
    layout: TextLayout,
    has_inline_styles: bool,
) -> list[list[GlyphLayout]]:
    runs = [layout.glyphs] if not has_inline_styles else glyph_runs(layout)
    result: list[list[GlyphLayout]] = []
    for run in runs:
        text_run = [glyph for glyph in run if not glyph_is_bitmap_guide(glyph)]
        if text_run:
            result.append(text_run)
    return result


def bitmap_guide_glyphs(layout: TextLayout) -> list[GlyphLayout]:
    return [glyph for glyph in layout.glyphs if glyph_is_bitmap_guide(glyph)]


def glyph_run_path(glyphs: list[GlyphLayout], baseline_y: int) -> QPainterPath:
    path = QPainterPath()
    for glyph in glyphs:
        path.addPath(glyph_path(glyph, baseline_y))
    return path


def glyph_run_rect(glyphs: list[GlyphLayout], baseline_y: int) -> QRectF:
    left = min(glyph.left for glyph in glyphs)
    right = max(glyph.left + glyph.width for glyph in glyphs)
    ascent = max(glyph.metrics.ascent() for glyph in glyphs)
    descent = max(glyph.metrics.descent() for glyph in glyphs)
    return QRectF(
        float(left),
        float(baseline_y - ascent),
        float(max(right - left, 1)),
        float(max(ascent + descent, 1)),
    )


def n3_main_fill_rect(layout: TextLayout, baseline_y: int) -> QRectF:
    """Return N3's shared vertical brush area for one main-text line."""
    glyphs = layout.glyphs
    if not glyphs:
        return QRectF(layout.line_rect)

    first = glyphs[0]
    font_size = max(int(first.font.pixelSize()), 1)
    metric_total = max(first.metrics.ascent() + first.metrics.descent(), 1)
    descent = font_size * max(first.metrics.descent(), 0) // metric_total
    brush_style = first.brush_style or first.style
    draw_edge = max(int(first.style.stroke_width_px), 0)
    anchor_edge = max(int(brush_style.stroke_width_px), 0)
    anchor_edge2 = main_stroke2_width(brush_style)
    draw_bottom = float(baseline_y + descent + draw_edge // 2)
    draw_height = max(
        max(int(glyph.font.pixelSize()), 1)
        + max(int(glyph.style.stroke_width_px), 0)
        for glyph in glyphs
    )
    draw_top = draw_bottom - float(draw_height)
    inset = float((anchor_edge + anchor_edge2) // 2)
    top = draw_top + inset
    bottom = draw_bottom - inset
    return QRectF(
        float(layout.line_rect.left()),
        top,
        float(max(layout.line_rect.width(), 1.0)),
        float(max(bottom - top, 1.0)),
    )
