"""Frame-independent geometry and wipe policy for horizontal ruby text."""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import Callable, Hashable, Optional

from PyQt6.QtCore import QPointF, QRectF
from PyQt6.QtGui import QFont, QFontMetrics, QImage, QPainter, QPainterPath

from krok_helper.subtitle_render.domain.models import Style
from krok_helper.subtitle_render.domain.timing import RubyAnnotation, TimingLine
from krok_helper.subtitle_render.engine.render.effects import (
    fill_signature,
    fill_brush_rect,
    glow_concentration_level,
    glow_extent,
    karaoke_state_signature,
    paint_glow_path,
    paint_split_glow_path,
    paint_text_layer_stack,
    ruby_decoration_kind,
    ruby_glow_radius,
    ruby_glow_concentration_level,
    ruby_shadow_dx,
    ruby_shadow_dy,
    ruby_paint_style,
    ruby_vertical_extra,
    ruby_visual_padding,
    ruby_baseline_y,
    visual_stroke_extent,
)
from krok_helper.subtitle_render.engine.render.core.layers import (
    BakedLayer,
    LayerAnimation,
    LayerCache,
    LayerContext,
    SCOPE_LINE,
)
from krok_helper.subtitle_render.engine.style.style_semantics import (
    effective_ruby_karaoke_colors,
)
from krok_helper.subtitle_render.engine.render.elements.horizontal.contracts import (
    LineLayout,
    RubyLayout,
    RubyWipeSegment,
)
from krok_helper.subtitle_render.engine.render.elements.horizontal.layout import (
    n3_main_fill_rect,
)
from krok_helper.subtitle_render.engine.ruby import (
    build_ruby_font_for_text,
    effective_ruby_for_target,
    ruby_font_size,
    ruby_layout_left_offset,
    ruby_layout_left_overhang,
    ruby_layout_units,
    ruby_layout_width,
    ruby_script_stroke_style,
    ruby_stroke2_width,
    ruby_stroke_width,
    ruby_style_for_target_indices,
    ruby_target_indices,
    ruby_target_x_range,
    ruby_visual_units_and_intervals,
)
from krok_helper.subtitle_render.engine.text import (
    TextLayout,
    build_font,
    n3_char_box_ascent,
)
from krok_helper.subtitle_render.engine.ruby.timing import (
    _ruby_progress_ratio as ruby_progress_ratio,
    _ruby_utopia_visual_units as ruby_utopia_visual_units,
)


HORIZONTAL_RUBY_GLOW_CACHE = LayerCache(max_items=128)


def clear_horizontal_ruby_glow_cache() -> None:
    """Drop cached full-glow bitmaps for horizontal ruby readings."""

    HORIZONTAL_RUBY_GLOW_CACHE.clear()


def get_or_build_ruby_glow(
    layout: RubyLayout,
    ruby_font: QFont,
    ruby_metrics: QFontMetrics,
    style: Style,
    rtl: bool,
    *,
    after: bool,
) -> BakedLayer:
    key = (
        "ruby_full_glow",
        ruby_glow_layer_key(layout, ruby_font, style, rtl, after=after),
    )

    def _build() -> BakedLayer:
        image, dx, dy = build_ruby_glow_layer(
            layout,
            ruby_font,
            ruby_metrics,
            style,
            rtl,
            after=after,
        )
        return BakedLayer(image=image, offset=QPointF(float(dx), float(dy)))

    return HORIZONTAL_RUBY_GLOW_CACHE.get_or_build(key, _build)


def blit_cached_ruby_glow(
    painter: QPainter,
    layout: RubyLayout,
    ruby_font: QFont,
    ruby_metrics: QFontMetrics,
    style: Style,
    rtl: bool,
    *,
    after: bool,
) -> None:
    if ruby_glow_radius(style, after=after) <= 0:
        return
    baked = get_or_build_ruby_glow(
        layout,
        ruby_font,
        ruby_metrics,
        style,
        rtl,
        after=after,
    )
    if baked.image.isNull():
        return
    anchor = QPointF(
        float(layout.x) + baked.offset.x(),
        float(layout.baseline_y) + baked.offset.y(),
    )
    painter.save()
    try:
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.drawImage(anchor, baked.image)
    finally:
        painter.restore()


def build_ruby_text_layer(
    layout: RubyLayout,
    ruby_font: QFont,
    ruby_metrics: QFontMetrics,
    style: Style,
    rtl: bool,
    *,
    after: bool,
    draw_glow: bool = True,
) -> tuple[QImage, int, int]:
    colors = effective_ruby_karaoke_colors(style)
    state = colors.after if after else colors.before
    paint_style = ruby_paint_style(style)
    stroke_width = ruby_stroke_width(style)
    stroke2_width = ruby_stroke2_width(style)
    shadow_dx = ruby_shadow_dx(style)
    shadow_dy = ruby_shadow_dy(style)
    glow_radius = ruby_glow_radius(style, after=after)
    stroke_extent = visual_stroke_extent(stroke_width, stroke2_width)
    glow_extra = (
        glow_extent(stroke_width, stroke2_width, glow_radius)
        if ruby_decoration_kind(style) == "glow"
        else 0
    )
    extent = max(
        stroke_extent,
        glow_extra,
        stroke_extent + abs(shadow_dx),
        stroke_extent + abs(shadow_dy),
        2,
    ) + 4
    layout_overhang_left = int(math.ceil(ruby_layout_left_overhang(
        layout.ruby.reading,
        ruby_metrics,
        layout.target_width,
        style,
        layout.ruby.kanji,
    )))
    pad_left = max(0, -shadow_dx) + extent + layout_overhang_left
    pad_right = max(0, shadow_dx) + extent
    pad_top = max(0, -shadow_dy) + extent
    pad_bottom = max(0, shadow_dy) + extent

    ruby_w = max(int(math.ceil(layout.reading_width)), 1)
    ruby_h = max(ruby_metrics.height(), 1)
    img_w = max(pad_left + ruby_w + pad_right, 1)
    img_h = max(pad_top + ruby_h + pad_bottom, 1)
    image = QImage(img_w, img_h, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(0)

    reading = (
        "".join(reversed(ruby_utopia_visual_units(layout.ruby.reading)))
        if rtl
        else layout.ruby.reading
    )
    local_baseline = pad_top + ruby_metrics.ascent()
    path, rect = ruby_text_path_and_rect(
        reading,
        ruby_font,
        ruby_metrics,
        pad_left,
        local_baseline,
        layout.target_width,
        style,
        base_text=layout.ruby.kanji,
    )
    fill_rect = layout.gradient_rect.translated(
        -float(layout.x) + float(pad_left),
        -float(layout.baseline_y) + float(local_baseline),
    )
    horizontal_fill_rect = (
        layout.horizontal_gradient_rect.translated(
            -float(layout.x) + float(pad_left),
            -float(layout.baseline_y) + float(local_baseline),
        )
        if layout.horizontal_gradient_rect is not None
        else None
    )

    p = QPainter(image)
    try:
        p.setRenderHints(
            QPainter.RenderHint.Antialiasing
            | QPainter.RenderHint.TextAntialiasing
            | QPainter.RenderHint.SmoothPixmapTransform
        )
        paint_text_layer_stack(
            p,
            path,
            rect,
            state,
            paint_style,
            stroke_width=stroke_width,
            stroke2_width=stroke2_width,
            shadow_dx=shadow_dx,
            shadow_dy=shadow_dy,
            glow_radius=glow_radius,
            draw_glow=draw_glow,
            fill_rect=fill_rect,
            horizontal_fill_rect=horizontal_fill_rect,
        )
    finally:
        p.end()

    return image, -pad_left, -(pad_top + ruby_metrics.ascent())


def build_ruby_glow_layer(
    layout: RubyLayout,
    ruby_font: QFont,
    ruby_metrics: QFontMetrics,
    style: Style,
    rtl: bool,
    *,
    after: bool,
) -> tuple[QImage, int, int]:
    colors = effective_ruby_karaoke_colors(style)
    state = colors.after if after else colors.before
    stroke_width = ruby_stroke_width(style)
    stroke2_width = ruby_stroke2_width(style)
    glow_radius = ruby_glow_radius(style, after=after)
    extent = glow_extent(stroke_width, stroke2_width, glow_radius) + 4
    layout_overhang_left = int(math.ceil(ruby_layout_left_overhang(
        layout.ruby.reading,
        ruby_metrics,
        layout.target_width,
        style,
        layout.ruby.kanji,
    )))
    pad_left = extent + layout_overhang_left
    pad_right = extent
    pad_top = extent
    pad_bottom = extent

    ruby_w = max(int(math.ceil(layout.reading_width)), 1)
    ruby_h = max(ruby_metrics.height(), 1)
    img_w = max(pad_left + ruby_w + pad_right, 1)
    img_h = max(pad_top + ruby_h + pad_bottom, 1)
    image = QImage(img_w, img_h, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(0)

    reading = (
        "".join(reversed(ruby_utopia_visual_units(layout.ruby.reading)))
        if rtl
        else layout.ruby.reading
    )
    local_baseline = pad_top + ruby_metrics.ascent()
    path, rect = ruby_text_path_and_rect(
        reading,
        ruby_font,
        ruby_metrics,
        pad_left,
        local_baseline,
        layout.target_width,
        style,
        base_text=layout.ruby.kanji,
    )
    fill_rect = layout.gradient_rect.translated(
        -float(layout.x) + float(pad_left),
        -float(layout.baseline_y) + float(local_baseline),
    )
    horizontal_fill_rect = (
        layout.horizontal_gradient_rect.translated(
            -float(layout.x) + float(pad_left),
            -float(layout.baseline_y) + float(local_baseline),
        )
        if layout.horizontal_gradient_rect is not None
        else None
    )

    p = QPainter(image)
    try:
        p.setRenderHints(
            QPainter.RenderHint.Antialiasing
            | QPainter.RenderHint.TextAntialiasing
            | QPainter.RenderHint.SmoothPixmapTransform
        )
        paint_glow_path(
            p,
            path,
            state.shadow,
            fill_brush_rect(state.shadow, fill_rect, horizontal_fill_rect),
            glow_radius,
            stroke_width,
            stroke2_width,
            concentration_level=ruby_glow_concentration_level(style),
        )
    finally:
        p.end()

    return image, -pad_left, -(pad_top + ruby_metrics.ascent())


@dataclass(frozen=True)
class RubyLayerPorts:
    """Painter-owned raster capabilities used by horizontal ruby layers."""

    blit_cached_ruby_glow: Callable[..., None]
    build_ruby_glow_layer: Callable[..., tuple]
    build_ruby_text_layer: Callable[..., tuple]
    paint_split_glow_path: Callable[..., None]
    paint_text_layer_stack: Callable[..., None]
    ruby_text_path_and_rect: Callable[..., tuple]


@dataclass(frozen=True)
class RubyStackPorts:
    """Compatibility factories used to build ordered horizontal ruby layers."""

    ruby_glow_layer: Callable[..., object]
    ruby_split_glow_layer: Callable[..., object]
    ruby_text_layer: Callable[..., object]


@dataclass(frozen=True)
class RubyLayoutPorts:
    """Compatibility hook needed while Painter exposes ruby wipe geometry."""

    ruby_wipe_geometry: Callable[..., tuple]


def ruby_text_layers(
    layouts: list[RubyLayout],
    ruby_font: QFont,
    ruby_metrics: QFontMetrics,
    t_ms: int,
    style: Style,
    rtl: bool,
    ports: RubyStackPorts,
    *,
    draw_glow: bool = True,
) -> list:
    """Build the ordered before/after body layers for horizontal ruby text."""
    layers = []
    for index, layout in enumerate(layouts):
        target_ruby_font = layout.font or ruby_font
        target_ruby_metrics = layout.metrics or ruby_metrics
        layers.append(
            ports.ruby_text_layer(
                layout,
                target_ruby_font,
                target_ruby_metrics,
                t_ms,
                layout.style,
                rtl,
                after=False,
                z_index=index * 2,
                draw_glow=draw_glow,
            )
        )
        layers.append(
            ports.ruby_text_layer(
                layout,
                target_ruby_font,
                target_ruby_metrics,
                t_ms,
                layout.style,
                rtl,
                after=True,
                z_index=index * 2 + 1,
                draw_glow=draw_glow,
            )
        )
    return layers


def ruby_glow_layers(
    layouts: list[RubyLayout],
    ruby_font: QFont,
    ruby_metrics: QFontMetrics,
    t_ms: int,
    style: Style,
    rtl: bool,
    ports: RubyStackPorts,
) -> list:
    """Build the ordered decoration layers for horizontal ruby text."""
    layers = []
    for index, layout in enumerate(layouts):
        target_ruby_font = layout.font or ruby_font
        target_ruby_metrics = layout.metrics or ruby_metrics
        if ruby_glow_can_combine_split(layout.style):
            layers.append(
                ports.ruby_split_glow_layer(
                    layout,
                    target_ruby_font,
                    target_ruby_metrics,
                    t_ms,
                    layout.style,
                    rtl,
                    z_index=index,
                )
            )
            continue
        layers.append(
            ports.ruby_glow_layer(
                layout,
                target_ruby_font,
                target_ruby_metrics,
                t_ms,
                layout.style,
                rtl,
                after=False,
                z_index=index * 2,
            )
        )
        layers.append(
            ports.ruby_glow_layer(
                layout,
                target_ruby_font,
                target_ruby_metrics,
                t_ms,
                layout.style,
                rtl,
                after=True,
                z_index=index * 2 + 1,
            )
        )
    return layers


def ruby_layer_stack(
    layout: LineLayout,
    line: TimingLine,
    t_ms: int,
    style: Style,
    ports: RubyStackPorts,
) -> list:
    """Build the static ruby body stack attached to one horizontal line."""
    if layout.ruby_metrics is None:
        return []
    return ruby_text_layers(
        list(layout.ruby_layouts),
        layout.ruby_font,
        layout.ruby_metrics,
        t_ms,
        style,
        layout.rtl,
        ports,
    )


def ruby_wipe_geometry(
    ruby: RubyAnnotation,
    ruby_font: QFont,
    ruby_metrics: QFontMetrics,
    x: int,
    baseline_y: int,
    target_width: int,
    style: Style,
    *,
    rtl: bool,
) -> tuple[tuple[RubyWipeSegment, ...], float, float, tuple]:
    """Build N3-style timed glyph geometry independently from its layout box."""
    logical_units = ruby_visual_units_and_intervals(ruby)
    if not logical_units:
        return (), float(x), float(x), ()
    visual_units = list(reversed(logical_units)) if rtl else logical_units
    unit_layouts = ruby_layout_units(
        [unit for unit, _interval in visual_units],
        ruby_metrics,
        x,
        target_width,
        style=style,
        base_text=ruby.kanji,
    )
    segments: list[RubyWipeSegment] = []
    signature: list[tuple] = []
    bounds: list[tuple[float, float]] = []
    edge_half = float(max(int(ruby_stroke_width(style)), 0) // 2)
    for (unit, interval), (_draw_unit, unit_x, unit_width) in zip(
        visual_units,
        unit_layouts,
    ):
        path = QPainterPath()
        path.addText(float(unit_x), float(baseline_y), ruby_font, unit)
        ink = path.boundingRect()
        if ink.isEmpty():
            ink_left = float(unit_x)
            ink_right = float(unit_x + max(unit_width, 0.0))
        else:
            ink_left = float(ink.left())
            ink_right = float(ink.right())
        if ink_right < ink_left:
            ink_left, ink_right = ink_right, ink_left
        draw_left = ink_left - edge_half
        draw_right = ink_right + edge_half
        start_ms, end_ms = interval
        segments.append(
            RubyWipeSegment(
                int(start_ms),
                max(int(start_ms), int(end_ms)),
                draw_right if rtl else draw_left,
                draw_left if rtl else draw_right,
            )
        )
        bounds.append((draw_left, draw_right))
        signature.append(
            (
                unit,
                round(float(unit_x) - float(x), 3),
                round(float(unit_width), 3),
                round(ink_left - float(x), 3),
                round(ink_right - float(x), 3),
            )
        )
    if not bounds:
        return (), float(x), float(x), tuple(signature)
    segments.sort(key=lambda segment: (segment.start_ms, segment.end_ms))
    adjusted = list(segments)
    for index in range(len(adjusted) - 1):
        current = adjusted[index]
        following = adjusted[index + 1]
        overlaps = (
            current.axis_end <= following.axis_start
            if rtl
            else current.axis_end >= following.axis_start
        )
        if overlaps:
            adjusted[index] = replace(current, axis_end=following.axis_start)
    return (
        tuple(adjusted),
        min(left for left, _right in bounds),
        max(right for _left, right in bounds),
        tuple(signature),
    )


def layout_rubies(
    ruby_metrics: QFontMetrics,
    line: TimingLine,
    intervals: list[tuple[int, int]],
    char_x_ranges: list[tuple[int, int]],
    main_baseline_y: int,
    rubies: list[RubyAnnotation],
    style: Style,
    ports: RubyLayoutPorts,
    *,
    main_ascent_px: int | None = None,
    text_layout: TextLayout | None = None,
    ruby_font: QFont | None = None,
) -> list[RubyLayout]:
    """Build frame-independent horizontal ruby layouts."""
    if not rubies:
        return []
    main_box_ascent: Optional[float] = None
    if main_ascent_px is not None and text_layout is not None and text_layout.glyphs:
        height_glyphs = [
            glyph
            for glyph in text_layout.glyphs
            if glyph.text.strip() and glyph.style.affects_ruby_anchor
        ]
        if not height_glyphs:
            target_indices = {
                index
                for ruby in rubies
                for index in ruby_target_indices(ruby, line, intervals)
            }
            height_glyphs = [
                glyph
                for glyph in text_layout.glyphs
                if glyph.text.strip() and glyph.index in target_indices
            ]
        candidates = [
            n3_char_box_ascent(
                glyph.metrics,
                glyph.style.font_size_px,
                glyph.style.stroke_width_px,
            )
            for glyph in height_glyphs
        ]
        if candidates:
            main_box_ascent = max(candidates)
    if main_box_ascent is None:
        main_box_ascent = n3_char_box_ascent(
            QFontMetrics(build_font(style)),
            style.font_size_px,
            style.stroke_width_px,
        )
    layouts: list[RubyLayout] = []
    for ruby in rubies:
        indices = ruby_target_indices(ruby, line, intervals)
        if not indices:
            continue
        paint_ruby = effective_ruby_for_target(ruby, indices, intervals)
        target_range = ruby_target_x_range(
            ruby,
            line,
            intervals,
            char_x_ranges,
        )
        if target_range is None:
            continue
        ruby_brush_style = ruby_style_for_target_indices(style, line, indices)
        ruby_style = ruby_script_stroke_style(
            ruby_brush_style,
            paint_ruby.reading,
        )
        target_ruby_font = build_ruby_font_for_text(
            ruby_style,
            paint_ruby.reading,
        )
        target_ruby_metrics = QFontMetrics(target_ruby_font)
        target_ruby_size = max(target_ruby_font.pixelSize(), 1)
        baseline_y = ruby_baseline_y(
            main_baseline_y,
            main_box_ascent,
            target_ruby_metrics,
            ruby_style,
            font_size_px=target_ruby_size,
        )
        left, right = target_range
        target_width = max(right - left, 1)
        gradient_rect = n3_ruby_fill_rect(
            left,
            target_width,
            baseline_y,
            target_ruby_metrics,
            ruby_style,
            brush_style=ruby_brush_style,
            font_size_px=target_ruby_size,
        )
        reading_width = ruby_layout_width(
            paint_ruby.reading,
            target_ruby_metrics,
            target_width,
            style=ruby_style,
            base_text=paint_ruby.kanji,
        )
        wipe_segments, wipe_left, wipe_right, geometry_signature = (
            ports.ruby_wipe_geometry(
                paint_ruby,
                target_ruby_font,
                target_ruby_metrics,
                left,
                baseline_y,
                target_width,
                ruby_style,
                rtl=style.right_to_left,
            )
        )
        layouts.append(
            RubyLayout(
                ruby=paint_ruby,
                indices=indices,
                style=ruby_style,
                x=left,
                baseline_y=baseline_y,
                target_width=target_width,
                reading_width=reading_width,
                gradient_rect=gradient_rect,
                wipe_segments=wipe_segments,
                wipe_left=wipe_left,
                wipe_right=wipe_right,
                geometry_signature=geometry_signature,
                font=target_ruby_font,
                metrics=target_ruby_metrics,
            )
        )
    if text_layout is not None and layouts:
        main_rect = n3_main_fill_rect(text_layout, main_baseline_y)
        top = min(
            float(main_rect.top()),
            *(float(layout.gradient_rect.top()) for layout in layouts),
        )
        bottom = max(
            float(main_rect.bottom()),
            *(float(layout.gradient_rect.bottom()) for layout in layouts),
        )
        shared_rect = QRectF(
            float(main_rect.left()),
            top,
            float(max(main_rect.width(), 1.0)),
            float(max(bottom - top, 1.0)),
        )
        layouts = [
            replace(
                layout,
                horizontal_gradient_rect=(
                    shared_rect
                    if layout.style.ruby_horizontal_gradient_with_main
                    else None
                ),
            )
            for layout in layouts
        ]
    return layouts


def role_ruby_vertical_extra(
    line: TimingLine,
    rubies: list[RubyAnnotation],
    intervals: list[tuple[int, int]],
    style: Style,
) -> int:
    """Reserve enough vertical space for the largest role-specific ruby."""
    extra = 0
    for ruby in rubies:
        indices = ruby_target_indices(ruby, line, intervals)
        if not indices:
            continue
        paint_ruby = effective_ruby_for_target(ruby, indices, intervals)
        ruby_style = ruby_script_stroke_style(
            ruby_style_for_target_indices(style, line, indices),
            paint_ruby.reading,
        )
        font = build_ruby_font_for_text(ruby_style, paint_ruby.reading)
        metrics = QFontMetrics(font)
        extra = max(
            extra,
            ruby_vertical_extra(
                ruby_style,
                metrics,
                font_size_px=max(font.pixelSize(), 1),
            ),
        )
    return extra


def n3_ruby_fill_rect(
    left: int,
    width: int,
    baseline_y: int,
    ruby_metrics: QFontMetrics,
    style: Style,
    *,
    brush_style: Style | None = None,
    font_size_px: int | None = None,
) -> QRectF:
    """Return the ruby ``DrawLineInfo`` gradient area used by N3."""
    font_size = (
        ruby_font_size(style)
        if font_size_px is None
        else max(int(font_size_px), 1)
    )
    metric_total = max(ruby_metrics.ascent() + ruby_metrics.descent(), 1)
    descent = font_size * max(ruby_metrics.descent(), 0) // metric_total
    draw_edge = ruby_stroke_width(style)
    anchor_style = brush_style or style
    anchor_edge = ruby_stroke_width(anchor_style)
    anchor_edge2 = ruby_stroke2_width(anchor_style)
    draw_bottom = float(baseline_y + descent + draw_edge // 2)
    draw_top = draw_bottom - float(font_size + draw_edge)
    inset = float((anchor_edge + anchor_edge2) // 2)
    top = draw_top + inset
    bottom = draw_bottom - inset
    return QRectF(
        float(left),
        top,
        float(max(width, 1)),
        float(max(bottom - top, 1.0)),
    )


def ruby_text_rect(layout: RubyLayout, ruby_metrics: QFontMetrics) -> QRectF:
    left_offset = ruby_layout_left_offset(
        layout.ruby.reading,
        ruby_metrics,
        layout.target_width,
        layout.style,
        layout.ruby.kanji,
    )
    return QRectF(
        float(layout.x + left_offset),
        float(layout.baseline_y - ruby_metrics.ascent()),
        float(layout.reading_width),
        float(ruby_metrics.height()),
    )


def ruby_text_path_and_rect(
    reading: str,
    ruby_font: QFont,
    ruby_metrics: QFontMetrics,
    x: int | float,
    baseline_y: int | float,
    target_width: int | float | None,
    style: Style | None = None,
    base_text: str | None = None,
) -> tuple[QPainterPath, QRectF]:
    """Build the visible ruby path and its authored horizontal layout box."""

    path = QPainterPath()
    if target_width is None:
        path.addText(float(x), float(baseline_y), ruby_font, reading)
        width = ruby_metrics.horizontalAdvance(reading)
        return path, QRectF(
            float(x),
            float(baseline_y - ruby_metrics.ascent()),
            float(width),
            float(ruby_metrics.height()),
        )

    units = ruby_utopia_visual_units(reading)
    layout_units = ruby_layout_units(
        units,
        ruby_metrics,
        x,
        target_width,
        style=style,
        base_text=base_text,
    )
    for unit, unit_x, _unit_width in layout_units:
        path.addText(float(unit_x), float(baseline_y), ruby_font, unit)
    layout_width = ruby_layout_width(
        reading,
        ruby_metrics,
        target_width,
        style=style,
        base_text=base_text,
    )
    layout_left = float(x) + ruby_layout_left_offset(
        reading,
        ruby_metrics,
        target_width,
        style,
        base_text,
    )
    return path, QRectF(
        layout_left,
        float(baseline_y - ruby_metrics.ascent()),
        float(layout_width),
        float(ruby_metrics.height()),
    )


def _ruby_clip_padding(style: Style, *, after: bool) -> int:
    stroke_width = ruby_stroke_width(style)
    stroke2_width = ruby_stroke2_width(style)
    stroke_extent = visual_stroke_extent(stroke_width, stroke2_width)
    if not after:
        return max(
            stroke_extent,
            glow_extent(
                stroke_width,
                stroke2_width,
                ruby_glow_radius(style, after=False),
            ),
            2,
        )
    return max(
        stroke_extent,
        (
            glow_extent(
                stroke_width,
                stroke2_width,
                ruby_glow_radius(style, after=True),
            )
            if ruby_decoration_kind(style) == "glow"
            else 0
        ),
        stroke_extent + abs(ruby_shadow_dx(style)),
        stroke_extent + abs(ruby_shadow_dy(style)),
        2,
    )


def ruby_after_clip_rect(
    layout: RubyLayout,
    ruby_metrics: QFontMetrics,
    style: Style,
    rtl: bool,
    ratio: float,
) -> QRectF:
    rect = ruby_text_rect(layout, ruby_metrics)
    pad = _ruby_clip_padding(style, after=True)
    ratio_c = min(ratio, 1.0)
    clip_left = (
        rect.left()
        + (rect.width() * (1.0 - ratio_c) if rtl else 0.0)
        - pad
    )
    return QRectF(
        clip_left,
        rect.top() - pad,
        rect.width() * ratio_c + pad,
        rect.height() + pad * 2,
    )


def ruby_wipe_state(
    layout: RubyLayout,
    t_ms: int,
) -> tuple[bool, bool, float]:
    """Return ``(visible, complete, front)`` for the ruby glyph wipe."""
    segments = layout.wipe_segments
    if not segments:
        ratio = ruby_progress_ratio(layout.ruby, t_ms)
        front = layout.wipe_left + (layout.wipe_right - layout.wipe_left) * ratio
        return ratio > 0.0, ratio >= 1.0, front
    return ruby_segment_wipe_state(segments, layout.ruby.pos_end_ms, t_ms)


def ruby_segment_wipe_state(
    segments: tuple[RubyWipeSegment, ...],
    pos_end_ms: int,
    t_ms: int,
) -> tuple[bool, bool, float]:
    """Evaluate timed glyph-axis segments, including empty-part pauses."""
    first = segments[0]
    if t_ms <= first.start_ms:
        return False, False, first.axis_start
    previous_front = first.axis_start
    for segment in segments:
        if t_ms < segment.start_ms:
            return True, False, previous_front
        if t_ms < segment.end_ms:
            duration = segment.end_ms - segment.start_ms
            local = (
                (t_ms - segment.start_ms) / duration
                if duration > 0
                else 1.0
            )
            front = (
                segment.axis_start
                + (segment.axis_end - segment.axis_start) * local
            )
            return True, False, front
        previous_front = segment.axis_end
    complete = t_ms >= max(int(pos_end_ms), segments[-1].end_ms)
    return True, complete, previous_front


def ruby_after_clip_rect_at_time(
    layout: RubyLayout,
    ruby_metrics: QFontMetrics,
    style: Style,
    rtl: bool,
    t_ms: int,
) -> QRectF:
    """Clip the after layer at the current glyph front."""
    _visible, _complete, front = ruby_wipe_state(layout, t_ms)
    rect = ruby_text_rect(layout, ruby_metrics)
    pad = _ruby_clip_padding(style, after=True)
    wipe_left = layout.wipe_left if layout.wipe_segments else rect.left()
    wipe_right = layout.wipe_right if layout.wipe_segments else rect.right()
    if rtl:
        left = min(max(front, wipe_left), wipe_right)
        return QRectF(
            left - pad,
            rect.top() - pad,
            max(wipe_right - left, 0.0) + pad,
            rect.height() + pad * 2,
        )
    right = min(max(front, wipe_left), wipe_right)
    return QRectF(
        wipe_left - pad,
        rect.top() - pad,
        max(right - wipe_left, 0.0) + pad,
        rect.height() + pad * 2,
    )


def ruby_before_clip_rect_at_time(
    layout: RubyLayout,
    ruby_metrics: QFontMetrics,
    style: Style,
    rtl: bool,
    t_ms: int,
) -> QRectF:
    """Keep the before ruby glow on the unsung side of the glyph front."""
    _visible, _complete, front = ruby_wipe_state(layout, t_ms)
    rect = ruby_text_rect(layout, ruby_metrics)
    pad = _ruby_clip_padding(style, after=False)
    wipe_left = layout.wipe_left if layout.wipe_segments else rect.left()
    wipe_right = layout.wipe_right if layout.wipe_segments else rect.right()
    front = min(max(front, wipe_left), wipe_right)
    if rtl:
        return QRectF(
            wipe_left - pad,
            rect.top() - pad,
            max(front - wipe_left, 0.0) + pad,
            rect.height() + pad * 2,
        )
    return QRectF(
        front,
        rect.top() - pad,
        max(wipe_right - front, 0.0) + pad,
        rect.height() + pad * 2,
    )


def ruby_horizontal_gradient_rect_signature(
    layout: RubyLayout,
) -> tuple[float, float, float, float] | None:
    rect = layout.horizontal_gradient_rect
    if rect is None:
        return None
    return (
        round(rect.left() - layout.x, 3),
        round(rect.top() - layout.baseline_y, 3),
        round(rect.width(), 3),
        round(rect.height(), 3),
    )


def ruby_text_layer_key(
    layout: RubyLayout,
    ruby_font: QFont,
    style: Style,
    rtl: bool,
    *,
    after: bool,
    draw_glow: bool = True,
) -> tuple:
    colors = effective_ruby_karaoke_colors(style)
    state = colors.after if after else colors.before
    return (
        layout.ruby.reading,
        layout.target_width,
        round(layout.reading_width, 3),
        layout.geometry_signature,
        (
            round(layout.gradient_rect.left() - layout.x, 3),
            round(layout.gradient_rect.top() - layout.baseline_y, 3),
            round(layout.gradient_rect.width(), 3),
            round(layout.gradient_rect.height(), 3),
        ),
        ruby_horizontal_gradient_rect_signature(layout),
        rtl,
        ruby_font.family(),
        ruby_font.pixelSize(),
        int(ruby_font.weight()),
        ruby_font.italic(),
        karaoke_state_signature(state),
        ruby_stroke_width(style),
        ruby_stroke2_width(style),
        ruby_shadow_dx(style),
        ruby_shadow_dy(style),
        ruby_decoration_kind(style),
        ruby_glow_radius(style, after=after),
        ruby_glow_concentration_level(style),
        after,
        draw_glow,
    )


def ruby_glow_layer_key(
    layout: RubyLayout,
    ruby_font: QFont,
    style: Style,
    rtl: bool,
    *,
    after: bool,
) -> tuple:
    colors = effective_ruby_karaoke_colors(style)
    state = colors.after if after else colors.before
    return (
        "ruby_glow",
        layout.ruby.reading,
        layout.target_width,
        round(layout.reading_width, 3),
        layout.geometry_signature,
        (
            round(layout.gradient_rect.left() - layout.x, 3),
            round(layout.gradient_rect.top() - layout.baseline_y, 3),
            round(layout.gradient_rect.width(), 3),
            round(layout.gradient_rect.height(), 3),
        ),
        ruby_horizontal_gradient_rect_signature(layout),
        rtl,
        ruby_font.family(),
        ruby_font.pixelSize(),
        int(ruby_font.weight()),
        ruby_font.italic(),
        fill_signature(state.shadow),
        ruby_stroke_width(style),
        ruby_stroke2_width(style),
        ruby_glow_radius(style, after=after),
        ruby_glow_concentration_level(style),
        after,
    )


def ruby_glow_states_differ(style: Style) -> bool:
    """Return whether before/after ruby glow sources need split processing."""
    if ruby_decoration_kind(style) != "glow":
        return False
    colors = effective_ruby_karaoke_colors(style)
    return (
        fill_signature(colors.before.shadow)
        != fill_signature(colors.after.shadow)
        or ruby_glow_radius(style, after=False)
        != ruby_glow_radius(style, after=True)
    )


def paint_ruby_karaoke_fragment(
    painter: QPainter,
    path: QPainterPath,
    rect: QRectF,
    ratio: float,
    style: Style,
    rtl: bool = False,
    fill_rect: QRectF | None = None,
    horizontal_fill_rect: QRectF | None = None,
    after_clip_rect: QRectF | None = None,
    before_glow_clip_rect: QRectF | None = None,
) -> None:
    """Paint one before/after ruby fragment through the owned raster contract."""
    ports = HORIZONTAL_RUBY_LAYER_PORTS
    colors = effective_ruby_karaoke_colors(style)
    paint_style = ruby_paint_style(style)
    stroke_width = ruby_stroke_width(style)
    stroke2_width = ruby_stroke2_width(style)
    shadow_dx = ruby_shadow_dx(style)
    shadow_dy = ruby_shadow_dy(style)
    before_glow_radius = ruby_glow_radius(style, after=False)
    after_glow_radius = ruby_glow_radius(style, after=True)
    glow_states_differ = ruby_glow_states_differ(style)

    # N3 clips before/after outline sources at WipeLeft and blurs afterwards.
    # The sharp colour boundary therefore stays on the ruby ink/edge while the
    # two soft halos may blend across it.
    clip_before_glow = ratio > 0.0 and glow_states_differ and (
        before_glow_radius > 0 or after_glow_radius > 0
    )
    if clip_before_glow and ratio < 1.0:
        if before_glow_clip_rect is not None:
            front = (
                before_glow_clip_rect.right()
                if rtl
                else before_glow_clip_rect.left()
            )
        else:
            front = rect.left() + rect.width() * (1.0 - ratio if rtl else ratio)
        before_pad = glow_extent(
            stroke_width, stroke2_width, before_glow_radius
        )
        before_source_clip = (
            QRectF(
                -1_000_000.0,
                rect.top() - before_pad,
                front + 1_000_000.0,
                rect.height() + before_pad * 2,
            )
            if rtl
            else QRectF(
                front,
                rect.top() - before_pad,
                1_000_000.0,
                rect.height() + before_pad * 2,
            )
        )
        paint_glow_path(
            painter,
            path,
            colors.before.shadow,
            fill_brush_rect(
                colors.before.shadow,
                fill_rect if fill_rect is not None else rect,
                horizontal_fill_rect,
            ),
            before_glow_radius,
            stroke_width,
            stroke2_width,
            source_clip=before_source_clip,
            concentration_level=glow_concentration_level(paint_style),
        )
        after_pad = glow_extent(
            stroke_width, stroke2_width, after_glow_radius
        )
        after_source_clip = (
            QRectF(
                front,
                rect.top() - after_pad,
                1_000_000.0,
                rect.height() + after_pad * 2,
            )
            if rtl
            else QRectF(
                -1_000_000.0,
                rect.top() - after_pad,
                front + 1_000_000.0,
                rect.height() + after_pad * 2,
            )
        )
        paint_glow_path(
            painter,
            path,
            colors.after.shadow,
            fill_brush_rect(
                colors.after.shadow,
                fill_rect if fill_rect is not None else rect,
                horizontal_fill_rect,
            ),
            after_glow_radius,
            stroke_width,
            stroke2_width,
            source_clip=after_source_clip,
            concentration_level=glow_concentration_level(paint_style),
        )

    if ratio < 1.0:
        ports.paint_text_layer_stack(
            painter,
            path,
            rect,
            colors.before,
            paint_style,
            stroke_width=stroke_width,
            stroke2_width=stroke2_width,
            shadow_dx=shadow_dx,
            shadow_dy=shadow_dy,
            glow_radius=before_glow_radius,
            draw_glow=not clip_before_glow,
            fill_rect=fill_rect,
            horizontal_fill_rect=horizontal_fill_rect,
        )

    if ratio <= 0.0:
        return

    painter.save()
    try:
        if ratio < 1.0 or after_clip_rect is not None:
            stroke_extent = visual_stroke_extent(stroke_width, stroke2_width)
            pad = max(
                stroke_extent,
                glow_extent(stroke_width, stroke2_width, after_glow_radius)
                if ruby_decoration_kind(style) == "glow"
                else 0,
                stroke_extent + abs(shadow_dx),
                stroke_extent + abs(shadow_dy),
                2,
            )
            # RTL: the sung area hugs the reading's right edge while the wipe
            # front moves left. The extra pad only expands the outer edges.
            if after_clip_rect is None:
                if rtl:
                    front = rect.left() + rect.width() * (1.0 - ratio)
                    after_clip_rect = QRectF(
                        front,
                        rect.top() - pad,
                        rect.width() * ratio + pad,
                        rect.height() + pad * 2,
                    )
                else:
                    after_clip_rect = QRectF(
                        rect.left() - pad,
                        rect.top() - pad,
                        rect.width() * ratio + pad,
                        rect.height() + pad * 2,
                    )
            painter.setClipRect(after_clip_rect)
        ports.paint_text_layer_stack(
            painter,
            path,
            rect,
            colors.after,
            paint_style,
            stroke_width=stroke_width,
            stroke2_width=stroke2_width,
            shadow_dx=shadow_dx,
            shadow_dy=shadow_dy,
            glow_radius=after_glow_radius,
            # Matching before/after glow is already supplied by the unsung
            # layer; after a complete wipe the sung layer must supply it.
            draw_glow=ratio >= 1.0,
            fill_rect=fill_rect,
            horizontal_fill_rect=horizontal_fill_rect,
        )
    finally:
        painter.restore()


def ruby_glow_can_combine_split(style: Style) -> bool:
    """Return whether one source bitmap can represent both ruby glow colours."""
    if not ruby_glow_states_differ(style):
        return False
    before_radius = ruby_glow_radius(style, after=False)
    return (
        before_radius > 0
        and before_radius == ruby_glow_radius(style, after=True)
    )


@dataclass(frozen=True)
class RubySplitGlowLayer:
    """Combined before/after ruby glow with a cached moving-front strip."""

    ruby_layout: RubyLayout
    ruby_font: QFont
    ruby_metrics: QFontMetrics
    t_ms: int
    style: Style
    rtl: bool
    ports: RubyLayerPorts = field(repr=False, compare=False)
    z_index: int = 0
    scope: str = SCOPE_LINE

    def active_window(self, ctx: LayerContext) -> list[tuple[int, int]]:
        return []

    def layout(self, ctx: LayerContext) -> "RubySplitGlowLayer":
        return self

    def static_key(self, ctx: LayerContext, layout: object) -> None:
        return None

    def bake(
        self,
        ctx: LayerContext,
        layout: object,
        key: Hashable,
    ) -> BakedLayer:
        raise AssertionError("combined ruby split glow is painted dynamically")

    def animate(self, ctx: LayerContext, layout: object) -> LayerAnimation:
        return LayerAnimation()

    def paint_dynamic(
        self,
        painter: QPainter,
        ctx: LayerContext,
        layout: object,
    ) -> None:
        visible, complete, front = ruby_wipe_state(
            self.ruby_layout,
            self.t_ms,
        )
        if not visible:
            self.ports.blit_cached_ruby_glow(
                painter,
                self.ruby_layout,
                self.ruby_font,
                self.ruby_metrics,
                self.style,
                self.rtl,
                after=False,
            )
            return
        if complete:
            self.ports.blit_cached_ruby_glow(
                painter,
                self.ruby_layout,
                self.ruby_font,
                self.ruby_metrics,
                self.style,
                self.rtl,
                after=True,
            )
            return

        reading = (
            "".join(
                reversed(
                    ruby_utopia_visual_units(
                        self.ruby_layout.ruby.reading
                    )
                )
            )
            if self.rtl
            else self.ruby_layout.ruby.reading
        )
        path, rect = self.ports.ruby_text_path_and_rect(
            reading,
            self.ruby_font,
            self.ruby_metrics,
            self.ruby_layout.x,
            self.ruby_layout.baseline_y,
            self.ruby_layout.target_width,
            self.style,
            base_text=self.ruby_layout.ruby.kanji,
        )
        radius = ruby_glow_radius(self.style, after=False)
        stroke_width = ruby_stroke_width(self.style)
        stroke2_width = ruby_stroke2_width(self.style)
        pad = glow_extent(stroke_width, stroke2_width, radius)
        top = rect.top() - pad
        height = rect.height() + pad * 2
        if self.rtl:
            before_source_clip = QRectF(
                -1_000_000.0,
                top,
                front + 1_000_000.0,
                height,
            )
            after_source_clip = QRectF(front, top, 1_000_000.0, height)
            before_baked_clip = QRectF(
                -1_000_000.0,
                -1_000_000.0,
                front - pad + 1_000_000.0,
                2_000_000.0,
            )
            after_baked_clip = QRectF(
                front + pad,
                -1_000_000.0,
                1_000_000.0,
                2_000_000.0,
            )
        else:
            before_source_clip = QRectF(front, top, 1_000_000.0, height)
            after_source_clip = QRectF(
                -1_000_000.0,
                top,
                front + 1_000_000.0,
                height,
            )
            before_baked_clip = QRectF(
                front + pad,
                -1_000_000.0,
                1_000_000.0,
                2_000_000.0,
            )
            after_baked_clip = QRectF(
                -1_000_000.0,
                -1_000_000.0,
                front - pad + 1_000_000.0,
                2_000_000.0,
            )

        strip_clip = QRectF(
            front - pad,
            -1_000_000.0,
            float(pad * 2),
            2_000_000.0,
        )
        colors = effective_ruby_karaoke_colors(self.style)
        self.ports.paint_split_glow_path(
            painter,
            path,
            colors.before.shadow,
            colors.after.shadow,
            self.ruby_layout.gradient_rect,
            radius,
            stroke_width,
            stroke2_width,
            before_source_clip=before_source_clip,
            after_source_clip=after_source_clip,
            concentration_level=ruby_glow_concentration_level(self.style),
            target_clip=strip_clip,
            horizontal_fill_rect=self.ruby_layout.horizontal_gradient_rect,
        )
        for after, clip in (
            (False, before_baked_clip),
            (True, after_baked_clip),
        ):
            painter.save()
            try:
                painter.setClipRect(clip)
                self.ports.blit_cached_ruby_glow(
                    painter,
                    self.ruby_layout,
                    self.ruby_font,
                    self.ruby_metrics,
                    self.style,
                    self.rtl,
                    after=after,
                )
            finally:
                painter.restore()

    def vertical_bounds(
        self,
        ctx: LayerContext,
        layout: object,
    ) -> tuple[int, int] | None:
        rect = ruby_text_rect(self.ruby_layout, self.ruby_metrics)
        pad = max(
            ruby_visual_padding(self.style, after=after)
            for after in (False, True)
        )
        return (
            int(math.floor(rect.top() - pad)),
            int(math.ceil(rect.bottom() + pad)),
        )


@dataclass(frozen=True)
class RubyGlowLayer:
    """Glow-only layer for one horizontal ruby reading."""

    ruby_layout: RubyLayout
    ruby_font: QFont
    ruby_metrics: QFontMetrics
    t_ms: int
    style: Style
    rtl: bool
    after: bool
    ports: RubyLayerPorts = field(repr=False, compare=False)
    z_index: int = 0
    scope: str = SCOPE_LINE

    def active_window(self, ctx: LayerContext) -> list[tuple[int, int]]:
        return []

    def layout(self, ctx: LayerContext) -> "RubyGlowLayer":
        return self

    def static_key(
        self,
        ctx: LayerContext,
        layout: object,
    ) -> tuple | None:
        if ruby_decoration_kind(self.style) != "glow":
            return None
        if ruby_glow_radius(self.style, after=self.after) == 0:
            return None
        visible, complete, _front = ruby_wipe_state(
            self.ruby_layout,
            self.t_ms,
        )
        if self.after:
            if not visible or not ruby_glow_states_differ(self.style):
                return None
            if not complete:
                return None
        elif ruby_glow_states_differ(self.style) and visible:
            return None
        return ruby_glow_layer_key(
            self.ruby_layout,
            self.ruby_font,
            self.style,
            self.rtl,
            after=self.after,
        )

    def bake(
        self,
        ctx: LayerContext,
        layout: object,
        key: Hashable,
    ) -> BakedLayer:
        image, dx, dy = self.ports.build_ruby_glow_layer(
            self.ruby_layout,
            self.ruby_font,
            self.ruby_metrics,
            self.style,
            self.rtl,
            after=self.after,
        )
        return BakedLayer(image=image, offset=QPointF(float(dx), float(dy)))

    def animate(self, ctx: LayerContext, layout: object) -> LayerAnimation:
        return LayerAnimation(
            top_left=QPointF(
                float(self.ruby_layout.x),
                float(self.ruby_layout.baseline_y),
            ),
        )

    def paint_dynamic(
        self,
        painter: QPainter,
        ctx: LayerContext,
        layout: object,
    ) -> None:
        if not ruby_glow_states_differ(self.style):
            return
        visible, complete, front = ruby_wipe_state(
            self.ruby_layout,
            self.t_ms,
        )
        if not visible or complete:
            return
        reading = (
            "".join(
                reversed(
                    ruby_utopia_visual_units(
                        self.ruby_layout.ruby.reading
                    )
                )
            )
            if self.rtl
            else self.ruby_layout.ruby.reading
        )
        path, rect = self.ports.ruby_text_path_and_rect(
            reading,
            self.ruby_font,
            self.ruby_metrics,
            self.ruby_layout.x,
            self.ruby_layout.baseline_y,
            self.ruby_layout.target_width,
            self.style,
            base_text=self.ruby_layout.ruby.kanji,
        )
        radius = ruby_glow_radius(self.style, after=self.after)
        pad = glow_extent(
            ruby_stroke_width(self.style),
            ruby_stroke2_width(self.style),
            radius,
        )
        source_clip = (
            QRectF(
                front,
                rect.top() - pad,
                1_000_000.0,
                rect.height() + pad * 2,
            )
            if self.rtl == self.after
            else QRectF(
                -1_000_000.0,
                rect.top() - pad,
                front + 1_000_000.0,
                rect.height() + pad * 2,
            )
        )
        colors = effective_ruby_karaoke_colors(self.style)
        state = colors.after if self.after else colors.before
        paint_glow_path(
            painter,
            path,
            state.shadow,
            fill_brush_rect(
                state.shadow,
                self.ruby_layout.gradient_rect,
                self.ruby_layout.horizontal_gradient_rect,
            ),
            radius,
            ruby_stroke_width(self.style),
            ruby_stroke2_width(self.style),
            source_clip=source_clip,
            concentration_level=ruby_glow_concentration_level(self.style),
        )

    def vertical_bounds(
        self,
        ctx: LayerContext,
        layout: object,
    ) -> tuple[int, int] | None:
        rect = ruby_text_rect(self.ruby_layout, self.ruby_metrics)
        pad = ruby_visual_padding(self.style, after=self.after)
        return (
            int(math.floor(rect.top() - pad)),
            int(math.ceil(rect.bottom() + pad)),
        )


@dataclass(frozen=True)
class RubyTextLayer:
    """Layer wrapper for one horizontal ruby reading."""

    ruby_layout: RubyLayout
    ruby_font: QFont
    ruby_metrics: QFontMetrics
    t_ms: int
    style: Style
    rtl: bool
    after: bool
    ports: RubyLayerPorts = field(repr=False, compare=False)
    z_index: int = 0
    scope: str = SCOPE_LINE
    draw_glow: bool = True

    def active_window(self, ctx: LayerContext) -> list[tuple[int, int]]:
        return []

    def layout(self, ctx: LayerContext) -> "RubyTextLayer":
        return self

    def static_key(
        self,
        ctx: LayerContext,
        layout: object,
    ) -> tuple | None:
        if self.after:
            visible, _complete, _front = ruby_wipe_state(
                self.ruby_layout,
                self.t_ms,
            )
            if not visible:
                return None
        return ruby_text_layer_key(
            self.ruby_layout,
            self.ruby_font,
            self.style,
            self.rtl,
            after=self.after,
            draw_glow=self.draw_glow,
        )

    def bake(
        self,
        ctx: LayerContext,
        layout: object,
        key: Hashable,
    ) -> BakedLayer:
        image, dx, dy = self.ports.build_ruby_text_layer(
            self.ruby_layout,
            self.ruby_font,
            self.ruby_metrics,
            self.style,
            self.rtl,
            after=self.after,
            draw_glow=self.draw_glow,
        )
        return BakedLayer(image=image, offset=QPointF(float(dx), float(dy)))

    def animate(self, ctx: LayerContext, layout: object) -> LayerAnimation:
        clip_rect = None
        if self.after:
            visible, complete, _front = ruby_wipe_state(
                self.ruby_layout,
                self.t_ms,
            )
            if not visible:
                return LayerAnimation(opacity=0.0)
            if not complete:
                clip_rect = ruby_after_clip_rect_at_time(
                    self.ruby_layout,
                    self.ruby_metrics,
                    self.style,
                    self.rtl,
                    self.t_ms,
                )
        return LayerAnimation(
            top_left=QPointF(
                float(self.ruby_layout.x),
                float(self.ruby_layout.baseline_y),
            ),
            clip_rect=clip_rect,
        )

    def paint_dynamic(
        self,
        painter: QPainter,
        ctx: LayerContext,
        layout: object,
    ) -> None:
        return

    def vertical_bounds(
        self,
        ctx: LayerContext,
        layout: object,
    ) -> tuple[int, int] | None:
        rect = ruby_text_rect(self.ruby_layout, self.ruby_metrics)
        pad = ruby_visual_padding(self.style, after=self.after)
        return (
            int(math.floor(rect.top() - pad)),
            int(math.ceil(rect.bottom() + pad)),
        )


HORIZONTAL_RUBY_LAYER_PORTS = RubyLayerPorts(
    blit_cached_ruby_glow=lambda *args, **kwargs: (
        blit_cached_ruby_glow(*args, **kwargs)
    ),
    build_ruby_glow_layer=lambda *args, **kwargs: (
        build_ruby_glow_layer(*args, **kwargs)
    ),
    build_ruby_text_layer=lambda *args, **kwargs: (
        build_ruby_text_layer(*args, **kwargs)
    ),
    paint_split_glow_path=lambda *args, **kwargs: (
        paint_split_glow_path(*args, **kwargs)
    ),
    paint_text_layer_stack=lambda *args, **kwargs: (
        paint_text_layer_stack(*args, **kwargs)
    ),
    ruby_text_path_and_rect=lambda *args, **kwargs: (
        ruby_text_path_and_rect(*args, **kwargs)
    ),
)
