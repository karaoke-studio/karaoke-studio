"""Geometry and cache policy for horizontal render layers."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Hashable

from PyQt6.QtCore import QPointF, QRectF
from PyQt6.QtGui import QImage, QPainter, QTransform

from krok_helper.subtitle_render.domain.models import Style
from krok_helper.subtitle_render.domain.timing import TimingLine
from krok_helper.subtitle_render.domain.paint import (
    KaraokeColors,
    KaraokeColorState,
)
from krok_helper.subtitle_render.engine.render.effects import (
    fill_is_alpha,
    fill_signature,
    glow_extent,
    glow_concentration_level,
    glow_radius,
    karaoke_state_signature,
    main_stroke2_width,
    paint_fill_path,
    paint_glow_path,
    paint_shadow_silhouette,
    paint_stroke_path,
    paint_text_layer_stack,
    stroke2_pen_width,
    stroke_pen_width,
    text_visual_padding,
    visual_stroke_extent,
)
from krok_helper.subtitle_render.engine.guide import (
    bitmap_guide_content_size,
    bitmap_guide_image,
    guide_symbol_is_bitmap,
)
from krok_helper.subtitle_render.engine.render.core.layers import (
    BakedLayer,
    LayerAnimation,
    LayerContext,
    SCOPE_GROUP,
    SCOPE_LINE,
)
from krok_helper.subtitle_render.engine.render.elements.horizontal.contracts import (
    FillSegment,
    LineCharTransition,
    LineLayout,
)
from krok_helper.subtitle_render.engine.render.elements.horizontal.layout import (
    bitmap_guide_anchor_descent,
    bitmap_guide_glyphs,
    glyph_is_bitmap_guide,
    glyph_run_path,
    glyph_run_rect,
    n3_main_fill_rect,
    text_glyph_runs,
)
from krok_helper.subtitle_render.engine.render.elements.horizontal.transitions import (
    char_drip_char_transform,
    char_fade_opacity,
    spin_flip_char_transform,
    transition_char_state,
    utopia_following_done_time,
)
from krok_helper.subtitle_render.engine.style.style_semantics import (
    effective_karaoke_colors,
)
from krok_helper.subtitle_render.engine.text import GlyphLayout
from krok_helper.subtitle_render.engine.value_signature import value_signature


@dataclass(frozen=True)
class BitmapGuidePorts:
    """Painter-owned sweep-band capabilities used by bitmap guide layers."""

    fill_clip_band: Callable[..., tuple[int, int] | None]
    fill_clip_band_for_glyphs: Callable[..., tuple[int, int] | None]
    n3_following_wipe_band: Callable[..., tuple[int, int] | None]


@dataclass(frozen=True)
class GlyphLayerPorts:
    """Painter-owned raster and sweep capabilities used by glyph layers."""

    fill_clip_band: Callable[..., tuple[int, int] | None]
    fill_clip_band_for_glyphs: Callable[..., tuple[int, int] | None]
    n3_following_wipe_band: Callable[..., tuple[int, int] | None]
    paint_glyph_run_after_glow_wipe: Callable[..., None]
    paint_glyph_run_before_glow_direct: Callable[..., None]
    paint_glyph_run_combined_glow: Callable[..., None]
    run_fill_complete: Callable[..., bool]


@dataclass(frozen=True)
class LayerStackPorts:
    """Compatibility factories and sweep hook for a horizontal layer stack."""

    bitmap_guide_layer: Callable[..., object]
    fill_clip_band_for_glyphs: Callable[..., tuple[int, int] | None]
    glyph_run_after_glow_layer: Callable[..., object]
    glyph_run_before_glow_layer: Callable[..., object]
    glyph_run_layer: Callable[..., object]
    glyph_run_split_glow_layer: Callable[..., object]


@dataclass(frozen=True)
class TransitionLayerStackPorts:
    """Compatibility factories and sweep hook for character transitions."""

    fill_clip_band_for_glyphs: Callable[..., tuple[int, int] | None]
    glyph_run_after_glow_layer: Callable[..., object]
    glyph_run_before_glow_layer: Callable[..., object]
    glyph_run_layer: Callable[..., object]


def horizontal_after_clip_rect(band: tuple[int, int], rtl: bool) -> QRectF:
    band_left, band_right = band
    if rtl:
        return QRectF(
            float(band_left),
            -1_000_000.0,
            1_000_000.0,
            2_000_000.0,
        )
    return QRectF(
        -1_000_000.0,
        -1_000_000.0,
        float(band_right) + 1_000_000.0,
        2_000_000.0,
    )


def horizontal_before_clip_rect(band: tuple[int, int], rtl: bool) -> QRectF:
    """Keep the before layer only on the unsung side of the wipe front."""
    band_left, band_right = band
    if rtl:
        return QRectF(
            -1_000_000.0,
            -1_000_000.0,
            float(band_left) + 1_000_000.0,
            2_000_000.0,
        )
    return QRectF(
        float(band_right),
        -1_000_000.0,
        1_000_000.0,
        2_000_000.0,
    )


def paint_glyph_run_direct(
    painter: QPainter,
    glyphs: list[GlyphLayout],
    baseline_y: int,
    *,
    after: bool,
    fill_rect: QRectF | None = None,
    draw_glow: bool | None = None,
) -> None:
    """Paint one horizontal glyph run without the layer cache."""

    role_style = glyphs[0].style
    colors = effective_karaoke_colors(role_style)
    state = colors.after if after else colors.before
    path = glyph_run_path(glyphs, baseline_y)
    rect = glyph_run_rect(glyphs, baseline_y)
    if draw_glow is None:
        draw_glow = not (after and role_style.decoration_kind == "glow")
    paint_text_layer_stack(
        painter,
        path,
        rect,
        state,
        role_style,
        stroke_width=role_style.stroke_width_px,
        stroke2_width=role_style.stroke2_width_px,
        shadow_dx=role_style.shadow_offset_x,
        shadow_dy=role_style.shadow_offset_y,
        glow_radius=glow_radius(role_style, after=after),
        draw_glow=draw_glow,
        fill_rect=fill_rect,
    )


def build_glyph_run_layer(
    glyphs: list[GlyphLayout],
    role_style: Style,
    colors: KaraokeColors,
    *,
    after: bool,
    supersample: float = 1.0,
    fill_rect: QRectF | None = None,
    baseline_y: int = 0,
) -> tuple[QImage, int, int]:
    """Bake one horizontal glyph-run state into a transparent image."""

    state = colors.after if after else colors.before
    run_left = min(glyph.left for glyph in glyphs)
    run_right = max(glyph.left + glyph.width for glyph in glyphs)
    run_ascent = max(glyph.metrics.ascent() for glyph in glyphs)
    run_descent = max(glyph.metrics.descent() for glyph in glyphs)
    run_w = max(run_right - run_left, 1)
    run_h = max(run_ascent + run_descent, 1)
    stroke2_width = main_stroke2_width(role_style)

    is_glow = role_style.decoration_kind == "glow"
    bake_glow = (
        is_glow
        and not after
        and not karaoke_glow_states_differ(role_style, colors)
    )
    has_shadow = (
        role_style.decoration_kind == "shadow"
        and bool(role_style.shadow_color)
        and bool(role_style.shadow_offset_x or role_style.shadow_offset_y)
    )

    stroke_extent = visual_stroke_extent(
        role_style.stroke_width_px,
        stroke2_width,
    )
    glow_extra = (
        glow_extent(
            role_style.stroke_width_px,
            stroke2_width,
            glow_radius(role_style, after=False),
        )
        if bake_glow
        else 0
    )
    extent = max(stroke_extent, glow_extra, 0) + 4
    shadow_dx = role_style.shadow_offset_x if has_shadow else 0
    shadow_dy = role_style.shadow_offset_y if has_shadow else 0
    pad_left = max(0, -shadow_dx) + extent
    pad_right = max(0, shadow_dx) + extent
    pad_top = max(0, -shadow_dy) + extent
    pad_bottom = max(0, shadow_dy) + extent

    img_w = max(pad_left + run_w + pad_right, 1)
    img_h = max(pad_top + run_h + pad_bottom, 1)

    # Render the natural-coordinate layer at Sx and let the caller downscale
    # it, preserving the sharp Utopia entrance phase and natural offsets.
    scale = max(float(supersample), 1.0)
    image = QImage(
        max(int(round(img_w * scale)), 1),
        max(int(round(img_h * scale)), 1),
        QImage.Format.Format_ARGB32_Premultiplied,
    )
    image.fill(0)

    target_origin_x = float(run_left - pad_left)
    target_origin_y = float(baseline_y - run_ascent - pad_top)
    path = glyph_run_path(glyphs, baseline_y)
    rect = QRectF(
        float(run_left),
        float(baseline_y - run_ascent),
        float(run_w),
        float(run_h),
    )
    brush_rect = fill_rect if fill_rect is not None else rect

    painter = QPainter(image)
    try:
        if scale != 1.0:
            painter.scale(scale, scale)
        painter.translate(-target_origin_x, -target_origin_y)
        painter.setRenderHints(
            QPainter.RenderHint.Antialiasing
            | QPainter.RenderHint.TextAntialiasing
            | QPainter.RenderHint.SmoothPixmapTransform
        )
        if bake_glow:
            paint_glow_path(
                painter,
                path,
                state.shadow,
                brush_rect,
                glow_radius(role_style, after=False),
                role_style.stroke_width_px,
                stroke2_width,
                concentration_level=glow_concentration_level(role_style),
            )
        elif has_shadow:
            paint_shadow_silhouette(
                painter,
                path,
                state.shadow,
                brush_rect,
                role_style.shadow_offset_x,
                role_style.shadow_offset_y,
                role_style.stroke_width_px,
                stroke2_width,
            )
        if stroke2_width > 0:
            paint_stroke_path(
                painter,
                path,
                state.stroke2,
                brush_rect,
                stroke2_pen_width(role_style.stroke_width_px, stroke2_width),
            )
        if role_style.stroke_color and role_style.stroke_width_px > 0:
            paint_stroke_path(
                painter,
                path,
                state.stroke,
                brush_rect,
                stroke_pen_width(role_style.stroke_width_px),
                protect_body=fill_is_alpha(state.text),
            )
        paint_fill_path(painter, path, state.text, brush_rect)
    finally:
        painter.end()

    return image, -pad_left, -(pad_top + run_ascent)


def build_glyph_run_glow_layer(
    glyphs: list[GlyphLayout],
    role_style: Style,
    colors: KaraokeColors,
    *,
    after: bool,
    fill_rect: QRectF | None = None,
    baseline_y: int = 0,
) -> tuple[QImage, int, int]:
    """Bake the full unclipped glow image for a horizontal glyph run."""

    state = colors.after if after else colors.before
    run_left = min(glyph.left for glyph in glyphs)
    run_right = max(glyph.left + glyph.width for glyph in glyphs)
    run_ascent = max(glyph.metrics.ascent() for glyph in glyphs)
    run_descent = max(glyph.metrics.descent() for glyph in glyphs)
    run_w = max(run_right - run_left, 1)
    run_h = max(run_ascent + run_descent, 1)
    radius = glow_radius(role_style, after=after)
    stroke2_width = main_stroke2_width(role_style)
    extent = glow_extent(
        role_style.stroke_width_px,
        stroke2_width,
        radius,
    ) + 4

    image = QImage(
        max(extent + run_w + extent, 1),
        max(extent + run_h + extent, 1),
        QImage.Format.Format_ARGB32_Premultiplied,
    )
    image.fill(0)

    target_origin_x = float(run_left - extent)
    target_origin_y = float(baseline_y - run_ascent - extent)
    path = glyph_run_path(glyphs, baseline_y)
    rect = QRectF(
        float(run_left),
        float(baseline_y - run_ascent),
        float(run_w),
        float(run_h),
    )
    brush_rect = fill_rect if fill_rect is not None else rect

    painter = QPainter(image)
    try:
        painter.translate(-target_origin_x, -target_origin_y)
        painter.setRenderHints(
            QPainter.RenderHint.Antialiasing
            | QPainter.RenderHint.TextAntialiasing
            | QPainter.RenderHint.SmoothPixmapTransform
        )
        paint_glow_path(
            painter,
            path,
            state.shadow,
            brush_rect,
            radius,
            role_style.stroke_width_px,
            stroke2_width,
            concentration_level=glow_concentration_level(role_style),
        )
    finally:
        painter.end()

    return image, -extent, -(extent + run_ascent)


def build_glyph_run_after_glow_layer(
    glyphs: list[GlyphLayout],
    role_style: Style,
    colors: KaraokeColors,
    *,
    fill_rect: QRectF | None = None,
    baseline_y: int = 0,
) -> tuple[QImage, int, int]:
    """Bake the full unclipped after-glow image for a glyph run."""

    return build_glyph_run_glow_layer(
        glyphs,
        role_style,
        colors,
        after=True,
        fill_rect=fill_rect,
        baseline_y=baseline_y,
    )


def paint_glyph_run_after_glow_direct(
    painter: QPainter,
    glyphs: list[GlyphLayout],
    baseline_y: int,
    band: tuple[int, int],
    *,
    rtl: bool,
    complete: bool,
    fill_rect: QRectF | None = None,
) -> None:
    """Paint one after-glow from a source clipped at the karaoke front."""

    role_style = glyphs[0].style
    colors = effective_karaoke_colors(role_style)
    path = glyph_run_path(glyphs, baseline_y)
    rect = glyph_run_rect(glyphs, baseline_y)
    pad = glow_extent(
        role_style.stroke_width_px,
        role_style.stroke2_width_px,
        glow_radius(role_style, after=True),
    )
    paint_glow_path(
        painter,
        path,
        colors.after.shadow,
        fill_rect if fill_rect is not None else rect,
        glow_radius(role_style, after=True),
        role_style.stroke_width_px,
        role_style.stroke2_width_px,
        source_clip=after_glow_source_clip_rect(
            band,
            rect,
            pad,
            rtl,
            complete,
        ),
        concentration_level=glow_concentration_level(role_style),
    )


def paint_line_direct(
    painter: QPainter,
    layout: LineLayout,
    t_ms: int,
    *,
    glyph_ports: GlyphLayerPorts,
    bitmap_ports: BitmapGuidePorts,
) -> None:
    """Paint a complete horizontal line without cached raster layers."""

    runs = text_glyph_runs(layout.text_layout, layout.has_inline_styles)
    baseline_y = layout.baseline_y
    fill_rect = n3_main_fill_rect(layout.text_layout, baseline_y)
    combined_glow_runs = [
        run for run in runs if glyph_run_can_combine_split_glow(run)
    ]
    combined_run_ids = {id(run) for run in combined_glow_runs}

    for run in combined_glow_runs:
        glyph_ports.paint_glyph_run_combined_glow(
            painter,
            run,
            baseline_y,
            layout.fill_segments,
            t_ms,
            layout.rtl,
            fill_rect=fill_rect,
        )
    for run in runs:
        if id(run) in combined_run_ids or not glyph_run_needs_before_glow_split(
            run
        ):
            continue
        before_band = glyph_ports.fill_clip_band_for_glyphs(
            layout.fill_segments,
            run,
            t_ms,
            layout.rtl,
        )
        complete = glyph_ports.run_fill_complete(
            layout.fill_segments,
            {glyph.index for glyph in run},
            t_ms,
        )
        glyph_ports.paint_glyph_run_before_glow_direct(
            painter,
            run,
            baseline_y,
            before_band,
            rtl=layout.rtl,
            complete=complete,
            fill_rect=fill_rect,
        )

    for run in runs:
        if id(run) in combined_run_ids:
            continue
        for glyph in run:
            glyph_run = [glyph]
            glyph_band = glyph_ports.fill_clip_band_for_glyphs(
                layout.fill_segments,
                glyph_run,
                t_ms,
                layout.rtl,
            )
            if glyph_band is None or glyph.text.isspace():
                continue
            glyph_complete = glyph_ports.run_fill_complete(
                layout.fill_segments,
                {glyph.index},
                t_ms,
            )
            following_band = glyph_ports.n3_following_wipe_band(
                layout.fill_segments,
                {glyph.index},
                t_ms,
                layout.rtl,
            )
            if following_band is not None:
                glyph_band = following_band
            glyph_released = glyph_complete and following_band is None
            if glyph_run_needs_after_glow(glyph_run):
                paint_glyph_run_after_glow_direct(
                    painter,
                    glyph_run,
                    baseline_y,
                    glyph_band,
                    rtl=layout.rtl,
                    complete=glyph_released,
                    fill_rect=fill_rect,
                )

    for run in runs:
        split_glow = glyph_run_needs_before_glow_split(run)
        if not split_glow:
            paint_glyph_run_direct(
                painter,
                run,
                baseline_y,
                after=False,
                fill_rect=fill_rect,
            )
            continue
        before_band = glyph_ports.fill_clip_band_for_glyphs(
            layout.fill_segments,
            run,
            t_ms,
            layout.rtl,
        )
        complete = glyph_ports.run_fill_complete(
            layout.fill_segments,
            {glyph.index for glyph in run},
            t_ms,
        )
        if complete:
            continue
        if before_band is None:
            paint_glyph_run_direct(
                painter,
                run,
                baseline_y,
                after=False,
                fill_rect=fill_rect,
                draw_glow=not split_glow,
            )
            continue
        painter.save()
        try:
            painter.setClipRect(horizontal_before_clip_rect(before_band, layout.rtl))
            paint_glyph_run_direct(
                painter,
                run,
                baseline_y,
                after=False,
                fill_rect=fill_rect,
                draw_glow=not split_glow,
            )
        finally:
            painter.restore()

    paint_bitmap_guide_glyphs(
        painter,
        layout,
        t_ms,
        after=False,
        ports=bitmap_ports,
    )

    for run in runs:
        for glyph in run:
            glyph_run = [glyph]
            glyph_band = glyph_ports.fill_clip_band_for_glyphs(
                layout.fill_segments,
                glyph_run,
                t_ms,
                layout.rtl,
            )
            if glyph_band is None or glyph.text.isspace():
                continue
            glyph_complete = glyph_ports.run_fill_complete(
                layout.fill_segments,
                {glyph.index},
                t_ms,
            )
            following_band = glyph_ports.n3_following_wipe_band(
                layout.fill_segments,
                {glyph.index},
                t_ms,
                layout.rtl,
            )
            if following_band is not None:
                glyph_band = following_band
            glyph_released = glyph_complete and following_band is None
            if glyph_released:
                paint_glyph_run_direct(
                    painter,
                    glyph_run,
                    baseline_y,
                    after=True,
                    fill_rect=fill_rect,
                )
                continue
            painter.save()
            try:
                painter.setClipRect(
                    horizontal_after_clip_rect(glyph_band, layout.rtl)
                )
                paint_glyph_run_direct(
                    painter,
                    glyph_run,
                    baseline_y,
                    after=True,
                    fill_rect=fill_rect,
                )
            finally:
                painter.restore()

    paint_bitmap_guide_glyphs(
        painter,
        layout,
        t_ms,
        after=True,
        ports=bitmap_ports,
    )


def after_glow_loose_clip_rect(
    band: tuple[int, int],
    rect: QRectF,
    glow_pad: int,
    rtl: bool,
    complete: bool,
) -> QRectF:
    """Return the already-blurred after-glow bitmap clip."""
    band_left, band_right = band
    glow_pad_f = float(glow_pad)
    left = float(band_left) - (0.0 if rtl and not complete else glow_pad_f)
    right = float(band_right) + (glow_pad_f if rtl or complete else 0.0)
    return QRectF(
        left,
        rect.top() - glow_pad,
        right - left,
        rect.height() + glow_pad * 2,
    )


def after_glow_source_clip_rect(
    band: tuple[int, int],
    rect: QRectF,
    glow_pad: int,
    rtl: bool,
    complete: bool,
) -> QRectF | None:
    """Return the sung-side source clip applied before Gaussian blur."""
    if complete:
        return None
    band_left, band_right = band
    top = rect.top() - glow_pad
    height = rect.height() + glow_pad * 2
    if rtl:
        return QRectF(float(band_left), top, 1_000_000.0, height)
    return QRectF(
        -1_000_000.0,
        top,
        float(band_right) + 1_000_000.0,
        height,
    )


def before_glow_source_clip_rect(
    band: tuple[int, int],
    rect: QRectF,
    glow_pad: int,
    rtl: bool,
) -> QRectF:
    """Return the unsung-side source clip applied before Gaussian blur."""
    band_left, band_right = band
    top = rect.top() - glow_pad
    height = rect.height() + glow_pad * 2
    if rtl:
        return QRectF(
            -1_000_000.0,
            top,
            float(band_left) + 1_000_000.0,
            height,
        )
    return QRectF(float(band_right), top, 1_000_000.0, height)


def inflate_rect(rect: QRectF, pad: int | float) -> QRectF:
    pad_f = float(max(pad, 0))
    return rect.adjusted(-pad_f, -pad_f, pad_f, pad_f)


def glyph_run_layer_key(
    glyphs: list[GlyphLayout],
    role_style: Style,
    colors: KaraokeColors,
    *,
    after: bool,
) -> tuple:
    """Return the position-independent cache key for one glyph-run layer."""
    run_left = min(glyph.left for glyph in glyphs)
    glyph_sig = tuple(
        (
            glyph.text,
            glyph.font.family(),
            glyph.font.pixelSize(),
            int(glyph.font.weight()),
            glyph.font.italic(),
            glyph.left - run_left,
            round(float(glyph.path_offset_x), 3),
            glyph.width,
            value_signature(glyph.vector_glyph),
        )
        for glyph in glyphs
    )
    state = colors.after if after else colors.before
    return (
        glyph_sig,
        karaoke_state_signature(state),
        role_style.shadow_offset_x,
        role_style.shadow_offset_y,
        role_style.stroke_width_px,
        main_stroke2_width(role_style),
        role_style.decoration_kind,
        glow_radius(role_style, after=False),
        glow_concentration_level(role_style),
        after,
    )


def relative_fill_rect_signature(
    glyphs: list[GlyphLayout],
    baseline_y: int,
    fill_rect: QRectF | None,
    *,
    global_anchor: bool = False,
) -> tuple[float, float, float, float] | None:
    """Return brush coordinates that affect a cached glyph run."""
    run_left = min(glyph.left for glyph in glyphs)
    if global_anchor:
        if fill_rect is None:
            return (
                round(float(run_left), 3),
                round(float(baseline_y), 3),
                0.0,
                0.0,
            )
        return (
            round(float(fill_rect.left()), 3),
            round(float(fill_rect.top()), 3),
            round(float(fill_rect.width()), 3),
            round(float(fill_rect.height()), 3),
        )
    if fill_rect is None:
        return None
    return (
        round(float(fill_rect.left()) - run_left, 3),
        round(float(fill_rect.top()) - baseline_y, 3),
        round(float(fill_rect.width()), 3),
        round(float(fill_rect.height()), 3),
    )


def karaoke_state_uses_image(state: KaraokeColorState) -> bool:
    return any(
        fill.mode == "image"
        for fill in (state.text, state.stroke, state.stroke2, state.shadow)
    )


def glyph_run_after_glow_key(
    glyphs: list[GlyphLayout],
    role_style: Style,
    colors: KaraokeColors,
) -> tuple:
    run_left = min(glyph.left for glyph in glyphs)
    glyph_sig = tuple(
        (
            glyph.text,
            glyph.font.family(),
            glyph.font.pixelSize(),
            int(glyph.font.weight()),
            glyph.font.italic(),
            glyph.left - run_left,
            round(float(glyph.path_offset_x), 3),
            glyph.width,
            value_signature(glyph.vector_glyph),
        )
        for glyph in glyphs
    )
    return (
        "after_glow",
        glyph_sig,
        fill_signature(colors.after.shadow),
        role_style.stroke_width_px,
        main_stroke2_width(role_style),
        glow_radius(role_style, after=True),
        glow_concentration_level(role_style),
        role_style.decoration_kind,
    )


def karaoke_glow_states_differ(style: Style, colors: KaraokeColors) -> bool:
    """Return whether before/after glow sources need split processing."""
    if style.decoration_kind != "glow":
        return False
    return (
        fill_signature(colors.before.shadow)
        != fill_signature(colors.after.shadow)
        or glow_radius(style, after=False) != glow_radius(style, after=True)
    )


def glyph_run_needs_after_glow(glyphs: list[GlyphLayout]) -> bool:
    if not glyphs:
        return False
    role_style = glyphs[0].style
    if glow_radius(role_style, after=True) == 0:
        return False
    return karaoke_glow_states_differ(
        role_style,
        effective_karaoke_colors(role_style),
    )


def glyph_run_needs_before_glow_split(glyphs: list[GlyphLayout]) -> bool:
    if not glyphs:
        return False
    role_style = glyphs[0].style
    if glow_radius(role_style, after=False) == 0:
        return False
    return karaoke_glow_states_differ(
        role_style,
        effective_karaoke_colors(role_style),
    )


def glyph_run_can_combine_split_glow(glyphs: list[GlyphLayout]) -> bool:
    if not glyph_run_needs_before_glow_split(glyphs):
        return False
    style = glyphs[0].style
    before_radius = glow_radius(style, after=False)
    return before_radius > 0 and before_radius == glow_radius(style, after=True)


def line_layer_stack(
    layout: LineLayout,
    t_ms: int,
    ports: LayerStackPorts,
) -> list:
    """Build the ordered static horizontal text layer stack."""
    runs = text_glyph_runs(layout.text_layout, layout.has_inline_styles)
    y = layout.baseline_y
    fill_rect = n3_main_fill_rect(layout.text_layout, y)
    combined_glow_runs = [
        run for run in runs if glyph_run_can_combine_split_glow(run)
    ]
    combined_run_ids = {id(run) for run in combined_glow_runs}
    combined_glow_layers = [
        ports.glyph_run_split_glow_layer(
            run,
            y,
            layout.fill_segments,
            t_ms,
            layout.rtl,
            fill_rect=fill_rect,
        )
        for run in combined_glow_runs
    ]
    before_glow_layers = [
        ports.glyph_run_before_glow_layer(
            run,
            y,
            layout.fill_segments,
            t_ms,
            layout.rtl,
            fill_rect=fill_rect,
        )
        for run in runs
        if id(run) not in combined_run_ids
        and glyph_run_needs_before_glow_split(run)
    ]
    before_layers = [
        ports.glyph_run_layer(
            run,
            y,
            layout.fill_segments,
            t_ms,
            layout.rtl,
            after=False,
            fill_rect=fill_rect,
        )
        for run in runs
    ]
    bitmap_before_layers = [
        ports.bitmap_guide_layer(
            glyph,
            y,
            layout.fill_segments,
            t_ms,
            layout.rtl,
            after=False,
            z_index=len(runs) * 2,
        )
        for glyph in bitmap_guide_glyphs(layout.text_layout)
    ]
    after_glow_layers = []
    after_body_layers = []
    bitmap_after_layers = []
    z_index = len(runs) * 2 + len(bitmap_before_layers)
    for run in runs:
        combined_glow = id(run) in combined_run_ids
        for glyph in run:
            glyph_run = [glyph]
            after_band = ports.fill_clip_band_for_glyphs(
                layout.fill_segments,
                glyph_run,
                t_ms,
                layout.rtl,
            )
            if after_band is None or glyph.text.isspace():
                continue
            if not combined_glow and glyph_run_needs_after_glow(glyph_run):
                after_glow_layers.append(
                    ports.glyph_run_after_glow_layer(
                        glyph_run,
                        y,
                        layout.fill_segments,
                        t_ms,
                        layout.rtl,
                        clip_band=after_band,
                        z_index=z_index,
                        fill_rect=fill_rect,
                    )
                )
                z_index += 1
            after_body_layers.append(
                ports.glyph_run_layer(
                    glyph_run,
                    y,
                    layout.fill_segments,
                    t_ms,
                    layout.rtl,
                    after=True,
                    clip_band=after_band,
                    z_index=z_index,
                    fill_rect=fill_rect,
                )
            )
            z_index += 1
    for glyph in bitmap_guide_glyphs(layout.text_layout):
        bitmap_after_layers.append(
            ports.bitmap_guide_layer(
                glyph,
                y,
                layout.fill_segments,
                t_ms,
                layout.rtl,
                after=True,
                z_index=z_index,
            )
        )
        z_index += 1
    return (
        combined_glow_layers
        + before_glow_layers
        + after_glow_layers
        + before_layers
        + bitmap_before_layers
        + after_body_layers
        + bitmap_after_layers
    )


def char_transition_layer_stack(
    layout: LineLayout,
    t_ms: int,
    transition: LineCharTransition,
    char_count: int,
    ports: TransitionLayerStackPorts,
) -> list:
    """Build per-glyph layers for fade, drip, and spin-flip transitions."""
    y = layout.baseline_y
    rtl = layout.rtl
    fill_rect = n3_main_fill_rect(layout.text_layout, y)
    is_spin = transition.effect == "spin_flip"
    is_drip = transition.effect == "char_drip"
    before_glow_layers: list = []
    after_glow_layers: list = []
    body_layers: list = []
    z = 0
    for glyph in layout.text_layout.glyphs:
        progress = char_fade_opacity(
            transition,
            glyph.index,
            char_count,
            t_ms=t_ms,
        )
        if progress <= 0.0:
            continue
        opacity = 1.0 if is_drip else progress
        if is_spin:
            transform = spin_flip_char_transform(
                glyph,
                y,
                transition,
                progress,
            )
        elif is_drip:
            transform = char_drip_char_transform(
                glyph,
                y,
                transition,
                progress,
            )
        else:
            transform = None
        run = [glyph]
        if glyph_run_needs_before_glow_split(run):
            before_glow_layers.append(
                ports.glyph_run_before_glow_layer(
                    run,
                    y,
                    layout.fill_segments,
                    t_ms,
                    rtl,
                    z_index=z,
                    fade_opacity=opacity,
                    transform=transform,
                    fill_rect=fill_rect,
                )
            )
        body_layers.append(
            ports.glyph_run_layer(
                run,
                y,
                layout.fill_segments,
                t_ms,
                rtl,
                after=False,
                z_index=z,
                fade_opacity=opacity,
                transform=transform,
                fill_rect=fill_rect,
            )
        )
        z += 1
        after_band = ports.fill_clip_band_for_glyphs(
            layout.fill_segments,
            run,
            t_ms,
            rtl,
        )
        if after_band is None:
            continue
        if glyph_run_needs_after_glow(run):
            after_glow_layers.append(
                ports.glyph_run_after_glow_layer(
                    run,
                    y,
                    layout.fill_segments,
                    t_ms,
                    rtl,
                    clip_band=after_band,
                    z_index=z,
                    fade_opacity=opacity,
                    transform=transform,
                    fill_rect=fill_rect,
                )
            )
            z += 1
        body_layers.append(
            ports.glyph_run_layer(
                run,
                y,
                layout.fill_segments,
                t_ms,
                rtl,
                after=True,
                clip_band=after_band,
                z_index=z,
                fade_opacity=opacity,
                transform=transform,
                fill_rect=fill_rect,
            )
        )
        z += 1
    return before_glow_layers + after_glow_layers + body_layers


def bitmap_guide_target_rect(
    glyph: GlyphLayout,
    baseline_y: int,
) -> QRectF | None:
    symbol = glyph.vector_glyph
    if not guide_symbol_is_bitmap(symbol):
        return None
    width, height = bitmap_guide_content_size(symbol, glyph.style)
    left = float(glyph.left + int(symbol.bitmap_margin_left_px))
    bottom = (
        baseline_y
        + bitmap_guide_anchor_descent(glyph)
        - int(symbol.bitmap_margin_bottom_px)
    )
    top = float(bottom - height)
    return QRectF(left, top, float(max(width, 1)), float(max(height, 1)))


def paint_bitmap_guide_glyph(
    painter: QPainter,
    glyph: GlyphLayout,
    baseline_y: int,
    *,
    after: bool,
    band: tuple[int, int] | None,
    rtl: bool,
) -> None:
    symbol = glyph.vector_glyph
    if not guide_symbol_is_bitmap(symbol):
        return
    image_path = (
        symbol.bitmap_after_path
        if after and symbol.bitmap_after_path
        else symbol.bitmap_before_path
    )
    image = bitmap_guide_image(image_path)
    rect = bitmap_guide_target_rect(glyph, baseline_y)
    if image is None or rect is None or image.isNull():
        return
    painter.save()
    try:
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        if after and band is not None:
            painter.setClipRect(horizontal_after_clip_rect(band, rtl))
        painter.drawImage(rect, image)
    finally:
        painter.restore()


def bitmap_guide_band_for_segments(
    fill_segments: list[FillSegment],
    glyph: GlyphLayout,
    t_ms: int,
    rtl: bool,
    ports: BitmapGuidePorts,
) -> tuple[int, int] | None:
    band = ports.fill_clip_band_for_glyphs(
        fill_segments,
        [glyph],
        t_ms,
        rtl,
    )
    if band is None:
        band = ports.fill_clip_band(fill_segments, t_ms, rtl)
    following_band = ports.n3_following_wipe_band(
        fill_segments,
        {glyph.index},
        t_ms,
        rtl,
    )
    if following_band is not None:
        return following_band
    return band


def bitmap_guide_band_for_glyph(
    layout: LineLayout,
    glyph: GlyphLayout,
    t_ms: int,
    ports: BitmapGuidePorts,
) -> tuple[int, int] | None:
    return bitmap_guide_band_for_segments(
        layout.fill_segments,
        glyph,
        t_ms,
        layout.rtl,
        ports,
    )


def paint_bitmap_guide_glyphs(
    painter: QPainter,
    layout: LineLayout,
    t_ms: int,
    ports: BitmapGuidePorts,
    *,
    after: bool,
) -> None:
    for glyph in bitmap_guide_glyphs(layout.text_layout):
        band = bitmap_guide_band_for_glyph(layout, glyph, t_ms, ports)
        if after and band is None:
            continue
        paint_bitmap_guide_glyph(
            painter,
            glyph,
            layout.baseline_y,
            after=after,
            band=band,
            rtl=layout.rtl,
        )


def paint_bitmap_guide_transition_glyph(
    painter: QPainter,
    glyph: GlyphLayout,
    fill_segments: list[FillSegment],
    baseline_y: int,
    line: TimingLine,
    intervals: list[tuple[int, int]],
    index: int,
    t_ms: int,
    transition: LineCharTransition,
    style: Style,
    ports: BitmapGuidePorts,
    *,
    rtl: bool,
) -> None:
    if not glyph_is_bitmap_guide(glyph):
        return
    following_done_ms = (
        utopia_following_done_time(line, intervals, index, style)
        if transition.effect == "utopia"
        else None
    )
    char_start_ms = intervals[index][0] if index < len(intervals) else glyph.index
    char_end_ms = (
        intervals[index][1]
        if index < len(intervals)
        else char_start_ms
    )
    opacity = transition_char_state(
        style,
        transition,
        index,
        max(len(line.chars), 1),
        char_start_ms=char_start_ms,
        char_end_ms=char_end_ms,
        t_ms=t_ms,
        frame_height=painter.device().height(),
        following_done_ms=following_done_ms,
    )[0]
    if opacity <= 0.0:
        return
    band = bitmap_guide_band_for_segments(
        fill_segments,
        glyph,
        t_ms,
        rtl,
        ports,
    )
    painter.save()
    try:
        painter.setOpacity(painter.opacity() * opacity)
        paint_bitmap_guide_glyph(
            painter,
            glyph,
            baseline_y,
            after=False,
            band=band,
            rtl=rtl,
        )
        if band is not None:
            paint_bitmap_guide_glyph(
                painter,
                glyph,
                baseline_y,
                after=True,
                band=band,
                rtl=rtl,
            )
    finally:
        painter.restore()


@dataclass(frozen=True)
class BitmapGuideLayer:
    glyph: GlyphLayout
    baseline_y: int
    fill_segments: list
    t_ms: int
    rtl: bool
    after: bool
    ports: BitmapGuidePorts
    z_index: int = 0
    scope: str = SCOPE_LINE

    def active_window(self, ctx: LayerContext) -> list[tuple[int, int]]:
        return []

    def layout(self, ctx: LayerContext) -> "BitmapGuideLayer":
        return self

    def static_key(self, ctx: LayerContext, layout: object) -> None:
        return None

    def bake(
        self,
        ctx: LayerContext,
        layout: object,
        key: Hashable,
    ) -> BakedLayer:
        return BakedLayer(image=QImage(), offset=QPointF())

    def animate(self, ctx: LayerContext, layout: object) -> LayerAnimation:
        return LayerAnimation()

    def paint_dynamic(
        self,
        painter: QPainter,
        ctx: LayerContext,
        layout: object,
    ) -> None:
        band = bitmap_guide_band_for_segments(
            self.fill_segments,
            self.glyph,
            self.t_ms,
            self.rtl,
            self.ports,
        )
        if self.after and band is None:
            return
        paint_bitmap_guide_glyph(
            painter,
            self.glyph,
            self.baseline_y,
            after=self.after,
            band=band,
            rtl=self.rtl,
        )

    def vertical_bounds(
        self,
        ctx: LayerContext,
        layout: object,
    ) -> tuple[int, int] | None:
        rect = bitmap_guide_target_rect(self.glyph, self.baseline_y)
        if rect is None:
            return None
        return int(math.floor(rect.top())), int(math.ceil(rect.bottom()))


@dataclass(frozen=True)
class GlyphRunLayer:
    """Layer wrapper for a horizontal text glyph-run body."""

    glyphs: list[GlyphLayout]
    baseline_y: int
    fill_segments: list[FillSegment]
    t_ms: int
    rtl: bool
    after: bool
    ports: GlyphLayerPorts = field(repr=False, compare=False)
    clip_band: tuple[int, int] | None = None
    z_index: int = 0
    scope: str = SCOPE_LINE
    fade_opacity: float = 1.0
    transform: QTransform | None = None
    fill_rect: QRectF | None = None

    def active_window(self, ctx: LayerContext) -> list[tuple[int, int]]:
        return []

    def layout(self, ctx: LayerContext) -> "GlyphRunLayer":
        return self

    def static_key(self, ctx: LayerContext, layout: object) -> tuple:
        role_style = self.glyphs[0].style
        colors = effective_karaoke_colors(role_style)
        state = colors.after if self.after else colors.before
        return (
            glyph_run_layer_key(
                self.glyphs,
                role_style,
                colors,
                after=self.after,
            ),
            relative_fill_rect_signature(
                self.glyphs,
                self.baseline_y,
                self.fill_rect,
                global_anchor=karaoke_state_uses_image(state),
            ),
        )

    def bake(
        self,
        ctx: LayerContext,
        layout: object,
        key: Hashable,
    ) -> BakedLayer:
        role_style = self.glyphs[0].style
        colors = effective_karaoke_colors(role_style)
        image, dx, dy = build_glyph_run_layer(
            self.glyphs,
            role_style,
            colors,
            after=self.after,
            fill_rect=self.fill_rect,
            baseline_y=self.baseline_y,
        )
        return BakedLayer(image=image, offset=QPointF(float(dx), float(dy)))

    def animate(self, ctx: LayerContext, layout: object) -> LayerAnimation:
        run_left = min(glyph.left for glyph in self.glyphs)
        clip_rect = None
        role_style = self.glyphs[0].style
        if (
            not self.after
            and glow_radius(role_style, after=False) > 0
            and karaoke_glow_states_differ(
                role_style,
                effective_karaoke_colors(role_style),
            )
        ):
            band = self.ports.fill_clip_band_for_glyphs(
                self.fill_segments,
                self.glyphs,
                self.t_ms,
                self.rtl,
            )
            if band is not None:
                if self.ports.run_fill_complete(
                    self.fill_segments,
                    {glyph.index for glyph in self.glyphs},
                    self.t_ms,
                ):
                    return LayerAnimation(opacity=0.0)
                clip_rect = horizontal_before_clip_rect(band, self.rtl)
        elif self.after:
            indices = {glyph.index for glyph in self.glyphs}
            following_band = self.ports.n3_following_wipe_band(
                self.fill_segments,
                indices,
                self.t_ms,
                self.rtl,
            )
            band = (
                following_band
                or self.clip_band
                or self.ports.fill_clip_band(
                    self.fill_segments,
                    self.t_ms,
                    self.rtl,
                )
            )
            if band is None:
                return LayerAnimation(opacity=0.0)
            if (
                self.ports.run_fill_complete(
                    self.fill_segments,
                    indices,
                    self.t_ms,
                )
                and following_band is None
            ):
                clip_rect = None
            else:
                clip_rect = horizontal_after_clip_rect(band, self.rtl)
        return LayerAnimation(
            top_left=QPointF(float(run_left), float(self.baseline_y)),
            clip_rect=clip_rect,
            opacity=self.fade_opacity,
            transform=self.transform,
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
        rect = glyph_run_rect(self.glyphs, self.baseline_y)
        pad = text_visual_padding(self.glyphs[0].style, after=self.after)
        return (
            int(math.floor(rect.top() - pad)),
            int(math.ceil(rect.bottom() + pad)),
        )


@dataclass(frozen=True)
class GlyphRunBeforeGlowLayer:
    """N3 before-glow: split the outline source, then blur it."""

    glyphs: list[GlyphLayout]
    baseline_y: int
    fill_segments: list[FillSegment]
    t_ms: int
    rtl: bool
    ports: GlyphLayerPorts = field(repr=False, compare=False)
    z_index: int = 0
    scope: str = SCOPE_LINE
    fade_opacity: float = 1.0
    transform: QTransform | None = None
    fill_rect: QRectF | None = None

    def active_window(self, ctx: LayerContext) -> list[tuple[int, int]]:
        return []

    def layout(self, ctx: LayerContext) -> "GlyphRunBeforeGlowLayer":
        return self

    def _state(self) -> tuple[tuple[int, int] | None, bool]:
        indices = {glyph.index for glyph in self.glyphs}
        following_band = self.ports.n3_following_wipe_band(
            self.fill_segments,
            indices,
            self.t_ms,
            self.rtl,
        )
        band = following_band or self.ports.fill_clip_band_for_glyphs(
            self.fill_segments,
            self.glyphs,
            self.t_ms,
            self.rtl,
        )
        complete = (
            self.ports.run_fill_complete(
                self.fill_segments,
                indices,
                self.t_ms,
            )
            and following_band is None
        )
        return band, complete

    def static_key(
        self,
        ctx: LayerContext,
        layout: object,
    ) -> tuple | None:
        band, complete = self._state()
        if complete or band is not None:
            return None
        role_style = self.glyphs[0].style
        colors = effective_karaoke_colors(role_style)
        return (
            glyph_run_layer_key(
                self.glyphs,
                role_style,
                colors,
                after=False,
            ),
            "before-glow",
            relative_fill_rect_signature(
                self.glyphs,
                self.baseline_y,
                self.fill_rect,
                global_anchor=colors.before.shadow.mode == "image",
            ),
        )

    def bake(
        self,
        ctx: LayerContext,
        layout: object,
        key: Hashable,
    ) -> BakedLayer:
        role_style = self.glyphs[0].style
        colors = effective_karaoke_colors(role_style)
        image, dx, dy = build_glyph_run_glow_layer(
            self.glyphs,
            role_style,
            colors,
            after=False,
            fill_rect=self.fill_rect,
            baseline_y=self.baseline_y,
        )
        return BakedLayer(image=image, offset=QPointF(float(dx), float(dy)))

    def animate(self, ctx: LayerContext, layout: object) -> LayerAnimation:
        run_left = min(glyph.left for glyph in self.glyphs)
        return LayerAnimation(
            top_left=QPointF(float(run_left), float(self.baseline_y)),
            opacity=self.fade_opacity,
            transform=self.transform,
        )

    def paint_dynamic(
        self,
        painter: QPainter,
        ctx: LayerContext,
        layout: object,
    ) -> None:
        band, complete = self._state()
        if complete:
            return
        painter.save()
        try:
            painter.setOpacity(painter.opacity() * self.fade_opacity)
            if self.transform is not None:
                painter.setTransform(self.transform, combine=True)
            self.ports.paint_glyph_run_before_glow_direct(
                painter,
                self.glyphs,
                self.baseline_y,
                band,
                rtl=self.rtl,
                complete=False,
                fill_rect=self.fill_rect,
            )
        finally:
            painter.restore()

    def vertical_bounds(
        self,
        ctx: LayerContext,
        layout: object,
    ) -> tuple[int, int] | None:
        rect = glyph_run_rect(self.glyphs, self.baseline_y)
        role_style = self.glyphs[0].style
        pad = glow_extent(
            role_style.stroke_width_px,
            role_style.stroke2_width_px,
            glow_radius(role_style, after=False),
        )
        return (
            int(math.floor(rect.top() - pad)),
            int(math.ceil(rect.bottom() + pad)),
        )


@dataclass(frozen=True)
class GlyphRunSplitGlowLayer:
    """Combined before/after decoration for equal-radius N3 glow wipes."""

    glyphs: list[GlyphLayout]
    baseline_y: int
    fill_segments: list[FillSegment]
    t_ms: int
    rtl: bool
    ports: GlyphLayerPorts = field(repr=False, compare=False)
    z_index: int = 0
    scope: str = SCOPE_LINE
    fill_rect: QRectF | None = None

    def active_window(self, ctx: LayerContext) -> list[tuple[int, int]]:
        return []

    def layout(self, ctx: LayerContext) -> "GlyphRunSplitGlowLayer":
        return self

    def static_key(self, ctx: LayerContext, layout: object) -> None:
        return None

    def bake(
        self,
        ctx: LayerContext,
        layout: object,
        key: Hashable,
    ) -> BakedLayer:
        raise AssertionError("combined split glow is painted dynamically")

    def animate(self, ctx: LayerContext, layout: object) -> LayerAnimation:
        return LayerAnimation()

    def paint_dynamic(
        self,
        painter: QPainter,
        ctx: LayerContext,
        layout: object,
    ) -> None:
        self.ports.paint_glyph_run_combined_glow(
            painter,
            self.glyphs,
            self.baseline_y,
            self.fill_segments,
            self.t_ms,
            self.rtl,
            fill_rect=self.fill_rect,
        )

    def vertical_bounds(
        self,
        ctx: LayerContext,
        layout: object,
    ) -> tuple[int, int] | None:
        rect = glyph_run_rect(self.glyphs, self.baseline_y)
        style = self.glyphs[0].style
        pad = max(
            glow_extent(
                style.stroke_width_px,
                style.stroke2_width_px,
                glow_radius(style, after=after),
            )
            for after in (False, True)
        )
        return (
            int(math.floor(rect.top() - pad)),
            int(math.ceil(rect.bottom() + pad)),
        )


@dataclass(frozen=True)
class GlyphRunAfterGlowLayer:
    """Layer wrapper for the after-glow bitmap of a glyph run."""

    glyphs: list[GlyphLayout]
    baseline_y: int
    fill_segments: list[FillSegment]
    t_ms: int
    rtl: bool
    ports: GlyphLayerPorts = field(repr=False, compare=False)
    clip_band: tuple[int, int] | None = None
    z_index: int = 0
    scope: str = SCOPE_LINE
    fade_opacity: float = 1.0
    transform: QTransform | None = None
    fill_rect: QRectF | None = None

    def active_window(self, ctx: LayerContext) -> list[tuple[int, int]]:
        return []

    def layout(self, ctx: LayerContext) -> "GlyphRunAfterGlowLayer":
        return self

    def static_key(
        self,
        ctx: LayerContext,
        layout: object,
    ) -> tuple | None:
        role_style = self.glyphs[0].style
        colors = effective_karaoke_colors(role_style)
        if role_style.decoration_kind != "glow":
            return None
        before_radius = glow_radius(role_style, after=False)
        after_radius = glow_radius(role_style, after=True)
        if after_radius == 0:
            return None
        need_after_glow = (
            fill_signature(colors.before.shadow)
            != fill_signature(colors.after.shadow)
            or before_radius != after_radius
        )
        band = self.clip_band or self.ports.fill_clip_band(
            self.fill_segments,
            self.t_ms,
            self.rtl,
        )
        if not need_after_glow or band is None:
            return None
        indices = {glyph.index for glyph in self.glyphs}
        if (
            not self.ports.run_fill_complete(
                self.fill_segments,
                indices,
                self.t_ms,
            )
            or self.ports.n3_following_wipe_band(
                self.fill_segments,
                indices,
                self.t_ms,
                self.rtl,
            )
            is not None
        ):
            return None
        return (
            glyph_run_after_glow_key(self.glyphs, role_style, colors),
            relative_fill_rect_signature(
                self.glyphs,
                self.baseline_y,
                self.fill_rect,
                global_anchor=colors.after.shadow.mode == "image",
            ),
        )

    def bake(
        self,
        ctx: LayerContext,
        layout: object,
        key: Hashable,
    ) -> BakedLayer:
        role_style = self.glyphs[0].style
        colors = effective_karaoke_colors(role_style)
        image, dx, dy = build_glyph_run_after_glow_layer(
            self.glyphs,
            role_style,
            colors,
            fill_rect=self.fill_rect,
            baseline_y=self.baseline_y,
        )
        return BakedLayer(image=image, offset=QPointF(float(dx), float(dy)))

    def animate(self, ctx: LayerContext, layout: object) -> LayerAnimation:
        run_left = min(glyph.left for glyph in self.glyphs)
        indices = {glyph.index for glyph in self.glyphs}
        following_band = self.ports.n3_following_wipe_band(
            self.fill_segments,
            indices,
            self.t_ms,
            self.rtl,
        )
        band = (
            following_band
            or self.clip_band
            or self.ports.fill_clip_band(
                self.fill_segments,
                self.t_ms,
                self.rtl,
            )
        )
        if band is None:
            return LayerAnimation(opacity=0.0)
        rect = glyph_run_rect(self.glyphs, self.baseline_y)
        role_style = self.glyphs[0].style
        pad = glow_extent(
            role_style.stroke_width_px,
            role_style.stroke2_width_px,
            glow_radius(role_style, after=True),
        )
        complete = (
            self.ports.run_fill_complete(
                self.fill_segments,
                indices,
                self.t_ms,
            )
            and following_band is None
        )
        clip_rect = (
            None
            if complete
            else after_glow_loose_clip_rect(
                band,
                rect,
                pad,
                self.rtl,
                complete,
            )
        )
        return LayerAnimation(
            top_left=QPointF(float(run_left), float(self.baseline_y)),
            clip_rect=clip_rect,
            opacity=self.fade_opacity,
            transform=self.transform,
        )

    def paint_dynamic(
        self,
        painter: QPainter,
        ctx: LayerContext,
        layout: object,
    ) -> None:
        indices = {glyph.index for glyph in self.glyphs}
        following_band = self.ports.n3_following_wipe_band(
            self.fill_segments,
            indices,
            self.t_ms,
            self.rtl,
        )
        band = (
            following_band
            or self.clip_band
            or self.ports.fill_clip_band(
                self.fill_segments,
                self.t_ms,
                self.rtl,
            )
        )
        if band is None:
            return
        opacity = max(0.0, min(float(self.fade_opacity), 1.0))
        if opacity <= 0.0:
            return
        complete = (
            self.ports.run_fill_complete(
                self.fill_segments,
                indices,
                self.t_ms,
            )
            and following_band is None
        )
        painter.save()
        try:
            painter.setOpacity(painter.opacity() * opacity)
            if self.transform is not None:
                painter.setTransform(self.transform, combine=True)
            self.ports.paint_glyph_run_after_glow_wipe(
                painter,
                self.glyphs,
                self.baseline_y,
                band,
                rtl=self.rtl,
                complete=complete,
                fill_rect=self.fill_rect,
            )
        finally:
            painter.restore()

    def vertical_bounds(
        self,
        ctx: LayerContext,
        layout: object,
    ) -> tuple[int, int] | None:
        rect = glyph_run_rect(self.glyphs, self.baseline_y)
        role_style = self.glyphs[0].style
        pad = glow_extent(
            role_style.stroke_width_px,
            role_style.stroke2_width_px,
            glow_radius(role_style, after=True),
        )
        return (
            int(math.floor(rect.top() - pad)),
            int(math.ceil(rect.bottom() + pad)),
        )


@dataclass(frozen=True)
class ScopeBoundsLayer:
    """Bounds-only layer for effects that remain dynamically painted."""

    rect: QRectF
    scope_id: Hashable
    z_index: int = 0
    scope: str = SCOPE_GROUP

    def active_window(self, ctx: LayerContext) -> list[tuple[int, int]]:
        return []

    def layout(self, ctx: LayerContext) -> "ScopeBoundsLayer":
        return self

    def static_key(
        self,
        ctx: LayerContext,
        layout: object,
    ) -> Hashable | None:
        return None

    def bake(
        self,
        ctx: LayerContext,
        layout: object,
        key: Hashable,
    ) -> BakedLayer:
        raise AssertionError("bounds-only layers are never baked")

    def animate(self, ctx: LayerContext, layout: object) -> LayerAnimation:
        return LayerAnimation(clip_rect=self.rect)

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
        return (
            int(math.floor(self.rect.top())),
            int(math.ceil(self.rect.bottom())),
        )
