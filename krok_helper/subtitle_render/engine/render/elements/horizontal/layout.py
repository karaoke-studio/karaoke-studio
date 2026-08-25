"""Frame-independent glyph geometry for horizontal subtitle lines."""

from __future__ import annotations

from PyQt6.QtCore import QRectF
from PyQt6.QtGui import QFontMetrics, QPainterPath

from krok_helper.subtitle_render.domain.models import Style
from krok_helper.subtitle_render.domain.timing import TimingTrack
from krok_helper.subtitle_render.engine.guide import guide_symbol_is_bitmap
from krok_helper.subtitle_render.engine.layout.line.style import lane_count
from krok_helper.subtitle_render.engine.render.effects import (
    glow_concentration_level,
    glow_radius,
    karaoke_state_signature,
    main_stroke2_width,
    ruby_vertical_extra,
    visual_text_padding,
)
from krok_helper.subtitle_render.engine.ruby import (
    active_rubies_for_line,
    build_ruby_font,
)
from krok_helper.subtitle_render.engine.style.style_semantics import (
    effective_karaoke_colors,
)
from krok_helper.subtitle_render.engine.text import (
    GlyphLayout,
    TextLayout,
    build_font,
)
from krok_helper.subtitle_render.engine.timing.timeline import DisplayLine
from krok_helper.subtitle_render.sources.guide_symbols import (
    scaled_guide_symbol_path,
)


def bitmap_guide_is_no_wipe(symbol: object | None) -> bool:
    return guide_symbol_is_bitmap(symbol) and not bool(
        getattr(symbol, "bitmap_after_path", None)
    )


def fixed_line_geometry(style: Style) -> tuple[int, int, int, int]:
    font = build_font(style)
    metrics = QFontMetrics(font)
    ruby_metrics = QFontMetrics(build_ruby_font(style))
    ruby_extra = ruby_vertical_extra(style, ruby_metrics)
    if style.layout_semantics == "n3_1074":
        font_size = max(int(style.font_size_px), 1)
        edge = max(int(style.stroke_width_px), 0)
        main_h = font_size + edge
        metric_total = max(metrics.ascent() + metrics.descent(), 1)
        main_descent = (
            font_size * max(metrics.descent(), 0) // metric_total
            + edge // 2
        )
        main_descent = min(max(main_descent, 0), main_h)
        main_ascent = main_h - main_descent
        return main_h, main_ascent, main_descent, 0
    pad = visual_text_padding(style)
    main_h = metrics.ascent() + metrics.descent() + pad * 2
    return main_h, metrics.ascent() + pad, metrics.descent() + pad, ruby_extra


def resolve_baseline_y(
    metrics: QFontMetrics,
    img_h: int,
    style: Style,
    ruby_metrics: QFontMetrics | None = None,
) -> int:
    pos = style.line_y_position
    margin = style.line_y_margin_px
    if style.layout_semantics == "n3_1074":
        main_h, main_ascent, main_descent, ruby_extra = fixed_line_geometry(style)
        if pos == "top":
            return margin + ruby_extra + main_ascent
        if pos == "center":
            return (img_h - main_h) // 2 + main_ascent
        return img_h - margin - main_descent
    pad = visual_text_padding(style)
    ruby_extra = 0
    if ruby_metrics is not None:
        ruby_extra = ruby_vertical_extra(style, ruby_metrics)
    if pos == "top":
        return margin + ruby_extra + pad + metrics.ascent()
    if pos == "center":
        block_h = metrics.height() + ruby_extra + pad * 2
        return (img_h - block_h) // 2 + ruby_extra + pad + metrics.ascent()
    return img_h - margin - pad - metrics.descent()


def resolve_display_baselines(
    img_h: int,
    track: TimingTrack,
    display_lines: list[DisplayLine],
    style: Style,
) -> dict[int, int]:
    if not style.dual_line_layout:
        font = build_font(style)
        metrics = QFontMetrics(font)
        line = display_lines[0].line if display_lines else None
        ruby_metrics = (
            QFontMetrics(build_ruby_font(style))
            if line is not None and active_rubies_for_line(track.rubies, line)
            else None
        )
        baseline = resolve_baseline_y(metrics, img_h, style, ruby_metrics)
        if style.line_horizontal_layout == "per_row":
            baseline += style.row1_offset_y
        return {0: baseline}

    main_h, main_ascent, main_descent, ruby_extra = fixed_line_geometry(style)
    gap = int(style.line_gap_px)
    margin = style.line_y_margin_px
    lanes = lane_count(style)
    step = main_h + gap

    if style.line_y_position == "top":
        first_baseline = margin + ruby_extra + main_ascent
    elif style.line_y_position == "center":
        total_h = main_h * lanes + gap * (lanes - 1)
        first_baseline = (img_h - total_h) // 2 + main_ascent
    else:
        last_baseline = img_h - margin - main_descent
        first_baseline = last_baseline - step * (lanes - 1)
    baselines = {lane: first_baseline + step * lane for lane in range(lanes)}
    if style.line_horizontal_layout == "per_row":
        if 0 in baselines:
            baselines[0] += style.row1_offset_y
        if 1 in baselines:
            baselines[1] += style.row2_offset_y
    return baselines


def bitmap_guide_anchor_descent(glyph: GlyphLayout) -> int:
    if glyph.style.layout_semantics == "n3_1074":
        return fixed_line_geometry(glyph.style)[2]
    return max(int(glyph.metrics.descent()), 0)


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
