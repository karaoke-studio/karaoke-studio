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


def glow_concentration_level(style: Style, *, after: bool = True) -> int:
    """Return the glow concentration for one karaoke state.

    ``after=False`` reads the before-state override; ``None`` keeps the old
    shared value (and therefore the legacy appearance).  The editor's
    "同步" switch forces the before state onto the after value.
    """
    if after or style.decoration_before_sync:
        value = style.glow_concentration_level
    else:
        value = (
            style.glow_before_concentration_level
            if style.glow_before_concentration_level is not None
            else style.glow_concentration_level
        )
    return normalize_glow_concentration_level(value)


def shadow_offset_x(style: Style, *, after: bool) -> int:
    if after or style.decoration_before_sync:
        return int(style.shadow_offset_x)
    value = (
        style.shadow_before_offset_x
        if style.shadow_before_offset_x is not None
        else style.shadow_offset_x
    )
    return int(value)


def shadow_offset_y(style: Style, *, after: bool) -> int:
    if after or style.decoration_before_sync:
        return int(style.shadow_offset_y)
    value = (
        style.shadow_before_offset_y
        if style.shadow_before_offset_y is not None
        else style.shadow_offset_y
    )
    return int(value)


def decoration_kind_for_state(style: Style, *, after: bool) -> DecorationKind:
    """Resolve the effective decoration kind for one karaoke state.

    A glow whose width or concentration resolves to zero is treated as
    ``none``: the old renderers still painted a 1 px halo for width 0, which
    users saw as an unwanted decoration instead of "no decoration".
    """
    if after:
        kind = style.decoration_kind
    elif style.decoration_before_sync:
        kind = style.decoration_kind
    else:
        kind = style.decoration_kind_before or style.decoration_kind
    if kind not in {"none", "shadow", "glow"}:
        return "none"
    if kind == "glow":
        concentration = glow_concentration_level(style, after=after)
        width = (
            style.glow_after_radius_px
            if after
            else style.glow_before_radius_px
        )
        if concentration < 0 or max(int(width), 0) <= 0:
            return "none"
    return kind


def glow_radius(style: Style, *, after: bool) -> int:
    if glow_concentration_level(style, after=after) < 0:
        return 0
    value = style.glow_after_radius_px if after else style.glow_before_radius_px
    return max(int(value), 0)


def ruby_decoration_kind(style: Style, *, after: bool = True) -> DecorationKind:
    if after or style.decoration_before_sync:
        value = style.ruby_decoration_kind
    else:
        value = style.ruby_decoration_kind_before
        if value is None:
            value = style.ruby_decoration_kind
    if value not in {"none", "shadow", "glow"}:
        value = decoration_kind_for_state(style, after=after)
    if value == "glow":
        concentration = ruby_glow_concentration_level(style, after=after)
        width = _ruby_glow_width(style, after=after)
        if concentration < 0 or width <= 0:
            return "none"
    return value  # type: ignore[return-value]


def ruby_shadow_dx(style: Style, *, after: bool = True) -> int:
    if ruby_decoration_kind(style, after=after) != "shadow":
        return 0
    if after or style.decoration_before_sync:
        value = style.ruby_shadow_offset_x
    else:
        value = style.ruby_shadow_before_offset_x
        if value is None:
            value = style.ruby_shadow_offset_x
    if value is not None:
        return int(value)
    return scaled_signed_px(shadow_offset_x(style, after=after), ruby_scale(style))


def ruby_shadow_dy(style: Style, *, after: bool = True) -> int:
    if ruby_decoration_kind(style, after=after) != "shadow":
        return 0
    if after or style.decoration_before_sync:
        value = style.ruby_shadow_offset_y
    else:
        value = style.ruby_shadow_before_offset_y
        if value is None:
            value = style.ruby_shadow_offset_y
    if value is not None:
        return int(value)
    return scaled_signed_px(shadow_offset_y(style, after=after), ruby_scale(style))


def _ruby_glow_width(style: Style, *, after: bool) -> int:
    value = (
        style.ruby_glow_after_radius_px
        if after
        else style.ruby_glow_before_radius_px
    )
    if value is None and style.ruby_glow_radius_px is not None:
        value = style.ruby_glow_radius_px
    if value is None and not after and style.ruby_glow_after_radius_px is not None:
        # 旧工程只存走字后侧或只在新版单侧设值时，未设的一侧继续沿用
        # 共享半径语义，而不是突然回退到主文字半径。
        value = style.ruby_glow_after_radius_px
    if value is not None:
        return max(int(value), 0)
    return scaled_glow_radius(style, ruby_scale(style), after=after)


def ruby_glow_radius(style: Style, *, after: bool) -> int:
    if ruby_glow_concentration_level(style, after=after) < 0:
        return 0
    return _ruby_glow_width(style, after=after)


def ruby_glow_concentration_level(style: Style, *, after: bool = True) -> int:
    if after or style.decoration_before_sync:
        value = style.ruby_glow_concentration_level
    else:
        value = style.ruby_glow_before_concentration_level
        if value is None:
            value = style.ruby_glow_concentration_level
    if value is None:
        return glow_concentration_level(style, after=after)
    return normalize_glow_concentration_level(value)


def ruby_paint_style(style: Style, *, after: bool = True) -> Style:
    decoration = ruby_decoration_kind(style, after=after)
    concentration = ruby_glow_concentration_level(style, after=after)
    if (
        decoration == style.decoration_kind
        and concentration == style.glow_concentration_level
    ):
        return style
    return replace(
        style,
        decoration_kind=decoration,
        glow_concentration_level=concentration,
    )


def style_for_state(style: Style, *, after: bool) -> Style:
    """Project a style onto one karaoke state's decoration parameters.

    Renderers consume a single ``Style`` per painted layer.  Projecting the
    before/after decoration slots (kind, concentration, shadow offsets and the
    effective ruby overrides) onto the legacy shared fields keeps every
    existing paint primitive unchanged while making all four colour cards
    truly independent.
    """
    kind = decoration_kind_for_state(style, after=after)
    concentration = glow_concentration_level(style, after=after)
    shadow_x = shadow_offset_x(style, after=after)
    shadow_y = shadow_offset_y(style, after=after)
    ruby_kind = ruby_decoration_kind(style, after=after)
    ruby_concentration = ruby_glow_concentration_level(style, after=after)
    ruby_shadow_x = ruby_shadow_dx(style, after=after)
    ruby_shadow_y = ruby_shadow_dy(style, after=after)
    changes: dict[str, object] = {}
    if style.decoration_kind != kind:
        changes["decoration_kind"] = kind
    if style.decoration_kind_before != kind:
        changes["decoration_kind_before"] = kind
    if style.glow_concentration_level != concentration:
        changes["glow_concentration_level"] = concentration
    if style.glow_before_concentration_level != concentration:
        changes["glow_before_concentration_level"] = concentration
    if style.shadow_offset_x != shadow_x:
        changes["shadow_offset_x"] = shadow_x
    if style.shadow_offset_y != shadow_y:
        changes["shadow_offset_y"] = shadow_y
    if style.shadow_before_offset_x != shadow_x:
        changes["shadow_before_offset_x"] = shadow_x
    if style.shadow_before_offset_y != shadow_y:
        changes["shadow_before_offset_y"] = shadow_y
    if style.ruby_decoration_kind != ruby_kind:
        changes["ruby_decoration_kind"] = ruby_kind
    if style.ruby_decoration_kind_before != ruby_kind:
        changes["ruby_decoration_kind_before"] = ruby_kind
    if style.ruby_glow_concentration_level != ruby_concentration:
        changes["ruby_glow_concentration_level"] = ruby_concentration
    if style.ruby_glow_before_concentration_level != ruby_concentration:
        changes["ruby_glow_before_concentration_level"] = ruby_concentration
    if style.ruby_shadow_offset_x != ruby_shadow_x:
        changes["ruby_shadow_offset_x"] = ruby_shadow_x
    if style.ruby_shadow_offset_y != ruby_shadow_y:
        changes["ruby_shadow_offset_y"] = ruby_shadow_y
    if style.ruby_shadow_before_offset_x != ruby_shadow_x:
        changes["ruby_shadow_before_offset_x"] = ruby_shadow_x
    if style.ruby_shadow_before_offset_y != ruby_shadow_y:
        changes["ruby_shadow_before_offset_y"] = ruby_shadow_y
    return replace(style, **changes) if changes else style


def text_visual_padding(style: Style, *, after: bool) -> int:
    stroke2_width = main_stroke2_width(style)
    pad = visual_stroke_extent(style.stroke_width_px, stroke2_width)
    if decoration_kind_for_state(style, after=after) == "glow":
        pad = max(
            pad,
            glow_extent(
                style.stroke_width_px,
                stroke2_width,
                glow_radius(style, after=after),
            ),
        )
    elif decoration_kind_for_state(style, after=after) == "shadow":
        pad += abs(shadow_offset_y(style, after=after))
    return max(pad, 2)


def ruby_visual_padding(style: Style, *, after: bool) -> int:
    stroke_width = ruby_stroke_width(style)
    stroke2_width = ruby_stroke2_width(style)
    pad = visual_stroke_extent(stroke_width, stroke2_width)
    if ruby_decoration_kind(style, after=after) == "glow":
        pad = max(
            pad,
            glow_extent(
                stroke_width,
                stroke2_width,
                ruby_glow_radius(style, after=after),
            ),
        )
    else:
        pad += abs(ruby_shadow_dy(style, after=after))
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
