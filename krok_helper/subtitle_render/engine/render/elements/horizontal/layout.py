"""Frame-independent glyph geometry for horizontal subtitle lines."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from PyQt6.QtCore import QRectF
from PyQt6.QtGui import QFontMetrics, QPainterPath

from krok_helper.subtitle_render.domain.models import Style
from krok_helper.subtitle_render.domain.timing import (
    RubyAnnotation,
    TimingLine,
    TimingTrack,
)
from krok_helper.subtitle_render.engine.guide import (
    guide_symbol_is_bitmap,
    render_line_with_guide_symbols,
    vector_glyph_width,
)
from krok_helper.subtitle_render.engine.layout.line.geometry import (
    line_has_role_labels,
)
from krok_helper.subtitle_render.engine.layout.page.pagination import (
    line_center_override,
)
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
    ruby_char_gaps,
)
from krok_helper.subtitle_render.engine.style.style_semantics import (
    effective_karaoke_colors,
)
from krok_helper.subtitle_render.engine.text import (
    GlyphLayout,
    TextLayout,
    build_font,
    build_latin_font,
    build_role_text_layout,
    build_text_layout,
    char_left_positions,
    letter_spacing,
    line_text_width,
    make_font_for,
    role_char_geometry_by_index,
)
from krok_helper.subtitle_render.engine.timing.timeline import (
    DisplayLine,
    compute_char_intervals,
)
from krok_helper.subtitle_render.engine.render.elements.horizontal.contracts import (
    FillSegment,
    LineLayout,
    RubyLayout,
)
from krok_helper.subtitle_render.engine.render.elements.horizontal.positioning import (
    line_lane_alignment,
    resolve_line_x_smart,
)
from krok_helper.subtitle_render.sources.guide_symbols import (
    scaled_guide_symbol_path,
)


@dataclass(frozen=True)
class HorizontalLayoutPorts:
    """Painter-owned capabilities needed to build horizontal line layouts."""

    char_layout_width: Callable[..., int]
    karaoke_fill_segments: Callable[..., list[FillSegment]]
    layout_rubies: Callable[..., list[RubyLayout]]
    n3_char_wipe_ranges_by_index: Callable[..., list[tuple[int, int]]]
    role_char_ink_ranges_by_index: Callable[..., list[tuple[int, int]]]
    role_ruby_vertical_extra: Callable[..., int]


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


def layout_line_uncached(
    track: TimingTrack,
    line: TimingLine,
    style: Style,
    img_w: int,
    img_h: int,
    ports: HorizontalLayoutPorts,
    *,
    baseline_y: int | None = None,
    line_x: int | None = None,
    lane: int | None = None,
    line_plan: object | None = None,
) -> LineLayout | None:
    render_line = (
        line_plan.render_line
        if line_plan is not None
        else render_line_with_guide_symbols(line)
    )
    resolved_intervals = (
        line_plan.resolved_intervals if line_plan is not None else None
    )
    center_override = line_plan.center_override if line_plan is not None else None
    if line_has_role_labels(render_line):
        return layout_role_line(
            track,
            render_line,
            style,
            img_w,
            img_h,
            ports,
            baseline_y=baseline_y,
            line_x=line_x,
            lane=lane,
            source_line=line,
            resolved_intervals=resolved_intervals,
            center_override=center_override,
        )
    return layout_plain_line(
        track,
        render_line,
        style,
        img_w,
        img_h,
        ports,
        baseline_y=baseline_y,
        line_x=line_x,
        lane=lane,
        source_line=line,
        resolved_intervals=resolved_intervals,
        center_override=center_override,
    )


def layout_plain_line(
    track: TimingTrack,
    line: TimingLine,
    style: Style,
    img_w: int,
    img_h: int,
    ports: HorizontalLayoutPorts,
    *,
    baseline_y: int | None = None,
    line_x: int | None = None,
    lane: int | None = None,
    source_line: TimingLine | None = None,
    resolved_intervals: tuple[tuple[int, int], ...] | None = None,
    center_override: bool | None = None,
) -> LineLayout:
    font = build_font(style)
    metrics = QFontMetrics(font)
    latin_font = build_latin_font(style)
    font_for = make_font_for(style, font, latin_font)
    latin_metrics = QFontMetrics(latin_font) if font_for is not None else metrics
    source_line = source_line or line
    active_rubies = active_rubies_for_line(track.rubies, source_line)
    ruby_font = build_ruby_font(style)
    ruby_metrics = QFontMetrics(ruby_font) if active_rubies else None

    char_widths = [
        (
            vector_glyph_width(char.vector_glyph, style)
            if char.vector_glyph is not None
            else ports.char_layout_width(
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
    intervals = (
        list(resolved_intervals)
        if resolved_intervals is not None
        else compute_char_intervals(line, char_widths)
    )
    char_gaps, ruby_left_ext, ruby_right_ext = ruby_char_gaps(
        line,
        char_widths,
        active_rubies,
        style,
        intervals,
    )
    total_w = line_text_width(char_widths, style) + sum(char_gaps)
    left_ext = ruby_left_ext
    right_ext = ruby_right_ext
    if center_override is None:
        center_override = line_center_override(track, source_line, style)
    n3_main_center = (
        style.layout_semantics == "n3_1074"
        and not center_override
        and style.line_horizontal_layout == "asymmetric"
        and line_lane_alignment(track, source_line, style, lane) == "center"
    )
    x0 = (
        line_x
        if line_x is not None
        else resolve_line_x_smart(
            img_w,
            total_w,
            track,
            source_line,
            style,
            lane,
            center_override=False,
        )
        if n3_main_center
        else resolve_line_x_smart(
            img_w,
            total_w + left_ext + right_ext,
            track,
            source_line,
            style,
            lane,
            center_override=center_override,
        )
        + left_ext
    )
    y = (
        baseline_y
        if baseline_y is not None
        else resolve_baseline_y(metrics, img_h, style, ruby_metrics)
    )
    rtl = style.right_to_left
    char_lefts = char_left_positions(
        char_widths,
        x0,
        rtl,
        letter_spacing(style),
        char_gaps=char_gaps,
        n3_no_backtracking=style.layout_semantics == "n3_1074",
    )
    char_x_ranges = [
        (left, left + width)
        for left, width in zip(char_lefts, char_widths)
    ]
    text_layout = build_text_layout(
        line,
        style,
        x0=x0,
        baseline_y=y,
        inline_styles=False,
        char_gaps=char_gaps,
    )
    ink_x_ranges = ports.role_char_ink_ranges_by_index(
        line,
        text_layout,
        char_x_ranges,
    )
    wipe_x_ranges = ports.n3_char_wipe_ranges_by_index(
        line,
        text_layout,
        char_x_ranges,
        ink_x_ranges,
    )
    fill_segments = ports.karaoke_fill_segments(
        char_widths,
        intervals,
        ink_x_ranges,
        active_rubies,
        line,
        release_x_ranges=wipe_x_ranges,
        layout_x_ranges=char_x_ranges,
        ruby_main_progress_mode=style.ruby_main_progress_mode,
    )
    line_rect = QRectF(
        float(x0),
        float(y - metrics.ascent()),
        float(total_w),
        float(metrics.height()),
    )
    colors = effective_karaoke_colors(style)
    ruby_layouts = tuple(
        ports.layout_rubies(
            ruby_metrics,
            line,
            intervals,
            char_x_ranges,
            y,
            active_rubies,
            style,
            main_ascent_px=text_layout.ascent,
            text_layout=text_layout,
            ruby_font=ruby_font,
        )
        if ruby_metrics is not None
        else ()
    )
    return LineLayout(
        text_layout=text_layout,
        font=font,
        metrics=metrics,
        latin_font=latin_font,
        font_for=font_for,
        active_rubies=active_rubies,
        ruby_font=ruby_font,
        ruby_metrics=ruby_metrics,
        char_widths=char_widths,
        total_w=total_w,
        x0=x0,
        baseline_y=y,
        intervals=intervals,
        char_lefts=char_lefts,
        char_x_ranges=char_x_ranges,
        fill_segments=fill_segments,
        line_rect=line_rect,
        colors=colors,
        rtl=rtl,
        has_inline_styles=False,
        ink_x_ranges=ink_x_ranges,
        ruby_layouts=ruby_layouts,
        render_line=line,
    )


def layout_role_line(
    track: TimingTrack,
    line: TimingLine,
    style: Style,
    img_w: int,
    img_h: int,
    ports: HorizontalLayoutPorts,
    *,
    baseline_y: int | None = None,
    line_x: int | None = None,
    lane: int | None = None,
    source_line: TimingLine | None = None,
    resolved_intervals: tuple[tuple[int, int], ...] | None = None,
    center_override: bool | None = None,
) -> LineLayout | None:
    has_shared_baseline = baseline_y is not None
    source_line = source_line or line
    active_rubies = active_rubies_for_line(track.rubies, source_line)
    ruby_font = build_ruby_font(style)
    ruby_metrics = QFontMetrics(ruby_font) if active_rubies else None
    measure_layout = build_role_text_layout(line, style, x0=0, baseline_y=0)
    if not measure_layout.glyphs:
        return None
    char_widths, _measure_ranges = role_char_geometry_by_index(line, measure_layout)
    intervals = (
        list(resolved_intervals)
        if resolved_intervals is not None
        else compute_char_intervals(line, char_widths)
    )
    ruby_extra = ports.role_ruby_vertical_extra(
        line,
        active_rubies,
        intervals,
        style,
    )
    char_gaps, ruby_left_ext, ruby_right_ext = ruby_char_gaps(
        line,
        char_widths,
        active_rubies,
        style,
        intervals,
    )
    left_ext = ruby_left_ext
    right_ext = ruby_right_ext
    total_w = measure_layout.total_width + sum(char_gaps)
    if center_override is None:
        center_override = line_center_override(track, source_line, style)
    n3_main_center = (
        style.layout_semantics == "n3_1074"
        and not center_override
        and style.line_horizontal_layout == "asymmetric"
        and line_lane_alignment(track, source_line, style, lane) == "center"
    )
    x0 = (
        line_x
        if line_x is not None
        else resolve_line_x_smart(
            img_w,
            total_w,
            track,
            source_line,
            style,
            lane,
            center_override=False,
        )
        if n3_main_center
        else resolve_line_x_smart(
            img_w,
            total_w + left_ext + right_ext,
            track,
            source_line,
            style,
            lane,
            center_override=center_override,
        )
        + left_ext
    )
    y = (
        baseline_y
        if baseline_y is not None
        else resolve_role_baseline_y(measure_layout, img_h, style, ruby_extra)
    )
    if not has_shared_baseline:
        y = clamp_role_baseline_y(y, measure_layout, img_h, style, ruby_extra)
    text_layout = build_role_text_layout(
        line,
        style,
        x0=x0,
        baseline_y=y,
        char_gaps=char_gaps,
    )
    char_widths, char_x_ranges = role_char_geometry_by_index(line, text_layout)
    intervals = (
        list(resolved_intervals)
        if resolved_intervals is not None
        else compute_char_intervals(line, char_widths)
    )
    ink_x_ranges = ports.role_char_ink_ranges_by_index(
        line,
        text_layout,
        char_x_ranges,
    )
    wipe_x_ranges = ports.n3_char_wipe_ranges_by_index(
        line,
        text_layout,
        char_x_ranges,
        ink_x_ranges,
    )
    fill_segments = ports.karaoke_fill_segments(
        char_widths,
        intervals,
        ink_x_ranges,
        active_rubies,
        line,
        release_x_ranges=wipe_x_ranges,
        layout_x_ranges=char_x_ranges,
        ruby_main_progress_mode=style.ruby_main_progress_mode,
    )
    ruby_layouts = tuple(
        ports.layout_rubies(
            ruby_metrics,
            line,
            intervals,
            char_x_ranges,
            y,
            active_rubies,
            style,
            main_ascent_px=text_layout.ascent,
            text_layout=text_layout,
            ruby_font=ruby_font,
        )
        if ruby_metrics is not None
        else ()
    )
    return LineLayout(
        text_layout=text_layout,
        active_rubies=active_rubies,
        font=text_layout.glyphs[0].font,
        metrics=text_layout.glyphs[0].metrics,
        latin_font=build_latin_font(style),
        font_for=None,
        ruby_font=ruby_font,
        ruby_metrics=ruby_metrics,
        char_widths=char_widths,
        total_w=text_layout.total_width,
        x0=int(text_layout.line_rect.left()),
        baseline_y=y,
        intervals=intervals,
        char_lefts=[char_range[0] for char_range in char_x_ranges],
        char_x_ranges=char_x_ranges,
        fill_segments=fill_segments,
        line_rect=text_layout.line_rect,
        colors=effective_karaoke_colors(style),
        rtl=style.right_to_left,
        has_inline_styles=True,
        ink_x_ranges=ink_x_ranges,
        ruby_layouts=ruby_layouts,
        render_line=line,
    )
