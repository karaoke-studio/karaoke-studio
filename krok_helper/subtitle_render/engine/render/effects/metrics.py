"""Deterministic visual extents shared by renderers and style previews."""

from __future__ import annotations

import math
from dataclasses import replace

from PyQt6.QtGui import QFontMetrics

from krok_helper.subtitle_render.domain.models import (
    DecorationKind,
    Style,
    TitleOverlay,
    normalize_glow_concentration_level,
)
from krok_helper.subtitle_render.engine.ruby import (
    ruby_font_size,
    ruby_scale,
    ruby_stroke2_width,
    ruby_stroke_width,
    scaled_px,
    scaled_signed_px,
)
from krok_helper.subtitle_render.engine.text import n3_char_box_descent


def main_stroke2_width(style: Style) -> int:
    return max(int(style.stroke2_width_px), 0) if style.stroke2_enabled else 0


def visual_stroke_extent(stroke_width: int, stroke2_width: int) -> int:
    return math.ceil((max(stroke_width, 0) + max(stroke2_width, 0)) / 2)


def visual_text_padding(style: Style) -> int:
    return visual_stroke_extent(style.stroke_width_px, main_stroke2_width(style))


def ruby_stroke_extent(style: Style) -> int:
    return visual_stroke_extent(
        ruby_stroke_width(style),
        ruby_stroke2_width(style),
    )


def ruby_vertical_extra(
    style: Style,
    ruby_metrics: QFontMetrics,
    *,
    font_size_px: int | None = None,
) -> int:
    """Return the height reserved above main text for ruby."""
    del ruby_metrics
    effective_size = (
        ruby_font_size(style)
        if font_size_px is None
        else max(int(font_size_px), 1)
    )
    return max(
        int(
            round(
                int(style.ruby_gap_px)
                + effective_size
                + max(ruby_stroke_width(style), 0)
            )
        ),
        0,
    )


def ruby_baseline_y(
    main_baseline_y: int,
    main_box_ascent: float,
    ruby_metrics: QFontMetrics,
    style: Style,
    *,
    font_size_px: int | None = None,
) -> int:
    """Resolve the N3 ruby baseline above the main text box."""
    main_top = main_baseline_y - main_box_ascent
    effective_size = (
        ruby_font_size(style)
        if font_size_px is None
        else max(int(font_size_px), 1)
    )
    return int(
        round(
            main_top
            - int(style.ruby_gap_px)
            - n3_char_box_descent(
                ruby_metrics,
                effective_size,
                ruby_stroke_width(style),
            )
        )
    )


def stroke_pen_width(stroke_width: int) -> int:
    return max(stroke_width, 0)


def stroke2_pen_width(stroke_width: int, stroke2_width: int) -> int:
    return max(stroke_width, 0) + max(stroke2_width, 0)


def glow_pen_width(stroke_width: int, stroke2_width: int, glow_radius: int) -> int:
    if glow_radius <= 0:
        return 0
    base_width = (
        stroke2_pen_width(stroke_width, stroke2_width)
        if stroke2_width > 0
        else stroke_pen_width(stroke_width)
    )
    return max(1, base_width + glow_radius)


def glow_extent(stroke_width: int, stroke2_width: int, glow_radius: int) -> int:
    if glow_radius <= 0:
        return 0
    return math.ceil(
        glow_pen_width(stroke_width, stroke2_width, glow_radius) / 2
        + glow_radius * 3
    )


def glow_blur_radii(radius: int, concentration_level: int) -> tuple[int, ...]:
    """N3 ``DrawOneLineDecorBlurMulti`` radii for low/medium/high density."""
    radius = max(int(radius), 0)
    level = normalize_glow_concentration_level(concentration_level)
    if radius == 0 or level < 0:
        return ()
    passes = level + 1
    return tuple(radius - (index * radius // passes) for index in range(passes))


def glow_concentration_level(style: Style) -> int:
    return normalize_glow_concentration_level(style.glow_concentration_level)


def glow_radius(style: Style, *, after: bool) -> int:
    if glow_concentration_level(style) < 0:
        return 0
    value = style.glow_after_radius_px if after else style.glow_before_radius_px
    return max(int(value), 0)


def ruby_decoration_kind(style: Style) -> DecorationKind:
    value = style.ruby_decoration_kind
    return value if value in {"none", "shadow", "glow"} else style.decoration_kind


def ruby_shadow_dx(style: Style) -> int:
    if ruby_decoration_kind(style) != "shadow":
        return 0
    if style.ruby_shadow_offset_x is not None:
        return int(style.ruby_shadow_offset_x)
    return scaled_signed_px(style.shadow_offset_x, ruby_scale(style))


def ruby_shadow_dy(style: Style) -> int:
    if ruby_decoration_kind(style) != "shadow":
        return 0
    if style.ruby_shadow_offset_y is not None:
        return int(style.ruby_shadow_offset_y)
    return scaled_signed_px(style.shadow_offset_y, ruby_scale(style))


def ruby_glow_radius(style: Style, *, after: bool) -> int:
    if ruby_glow_concentration_level(style) < 0:
        return 0
    value = (
        style.ruby_glow_after_radius_px
        if after
        else style.ruby_glow_before_radius_px
    )
    if value is None and style.ruby_glow_radius_px is not None:
        value = style.ruby_glow_radius_px
    if value is not None:
        return max(int(value), 0)
    return scaled_glow_radius(style, ruby_scale(style), after=after)


def ruby_glow_concentration_level(style: Style) -> int:
    value = style.ruby_glow_concentration_level
    if value is None:
        return glow_concentration_level(style)
    return normalize_glow_concentration_level(value)


def ruby_paint_style(style: Style) -> Style:
    decoration = ruby_decoration_kind(style)
    concentration = ruby_glow_concentration_level(style)
    if (
        decoration == style.decoration_kind
        and concentration == glow_concentration_level(style)
    ):
        return style
    return replace(
        style,
        decoration_kind=decoration,
        glow_concentration_level=concentration,
    )


def text_visual_padding(style: Style, *, after: bool) -> int:
    stroke2_width = main_stroke2_width(style)
    pad = visual_stroke_extent(style.stroke_width_px, stroke2_width)
    if style.decoration_kind == "glow":
        pad = max(
            pad,
            glow_extent(
                style.stroke_width_px,
                stroke2_width,
                glow_radius(style, after=after),
            ),
        )
    elif style.decoration_kind == "shadow":
        pad += abs(style.shadow_offset_y)
    return max(pad, 2)


def ruby_visual_padding(style: Style, *, after: bool) -> int:
    stroke_width = ruby_stroke_width(style)
    stroke2_width = ruby_stroke2_width(style)
    pad = visual_stroke_extent(stroke_width, stroke2_width)
    if ruby_decoration_kind(style) == "glow":
        pad = max(
            pad,
            glow_extent(
                stroke_width,
                stroke2_width,
                ruby_glow_radius(style, after=after),
            ),
        )
    else:
        pad += abs(ruby_shadow_dy(style))
    return max(pad, 2)


def title_visual_padding(title: TitleOverlay) -> int:
    pad = visual_stroke_extent(title.stroke_width_px, title.stroke2_width_px)
    if title.decoration_kind == "glow":
        pad = max(
            pad,
            glow_extent(
                title.stroke_width_px,
                title.stroke2_width_px,
                max(int(title.glow_radius_px), 0),
            ),
        )
    elif title.decoration_kind == "shadow":
        pad += abs(title.shadow_offset_y)
    return max(pad, 2)


def scaled_glow_radius(style: Style, scale: float, *, after: bool) -> int:
    return scaled_px(glow_radius(style, after=after), scale)
