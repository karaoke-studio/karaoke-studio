"""Pure model semantics for resolving subtitle style schemes."""

from __future__ import annotations

from dataclasses import replace

from krok_helper.subtitle_render.paint import (
    KaraokeColors,
    KaraokeColorState,
    PaintFill,
)
from krok_helper.subtitle_render.models import (
    N3_FONT_INHERITANCE_FIELDS,
    Style,
    SubtitleStyleScheme,
)


SUBTITLE_SCHEME_STYLE_FIELDS: tuple[str, ...] = (
    "font_family",
    "font_family_latin",
    "font_size_px",
    "letter_spacing_px",
    "space_width_percent",
    "latin_font_size_px",
    "latin_font_weight",
    "latin_stroke_width_px",
    "latin_stroke2_enabled",
    "latin_stroke2_width_px",
    "allow_biting",
    "font_weight",
    "italic",
    "affects_ruby_anchor",
    "base_color",
    "fill_color",
    "fill_gradient_enabled",
    "fill_gradient_start_color",
    "fill_gradient_end_color",
    "fill_gradient_angle_deg",
    "stroke_color",
    "stroke_width_px",
    "stroke2_enabled",
    "stroke2_width_px",
    "decoration_kind",
    "glow_radius_px",
    "glow_before_radius_px",
    "glow_after_radius_px",
    "glow_concentration_level",
    "shadow_color",
    "shadow_offset_x",
    "shadow_offset_y",
    "ruby_font_size_px",
    "ruby_font_family",
    "ruby_font_family_latin",
    "ruby_font_weight",
    "ruby_latin_font_size_px",
    "ruby_latin_font_weight",
    "ruby_font_follow_main",
    "ruby_color",
    "ruby_gap_px",
    "ruby_stroke_width_px",
    "ruby_stroke2_enabled",
    "ruby_stroke2_width_px",
    "ruby_latin_stroke_width_px",
    "ruby_latin_stroke2_enabled",
    "ruby_latin_stroke2_width_px",
    "ruby_decoration_kind",
    "ruby_glow_radius_px",
    "ruby_glow_before_radius_px",
    "ruby_glow_after_radius_px",
    "ruby_glow_concentration_level",
    "ruby_shadow_offset_x",
    "ruby_shadow_offset_y",
    "ruby_colors_follow_main",
    "ruby_horizontal_gradient_with_main",
    "karaoke_colors",
    "ruby_karaoke_colors",
)


def style_scheme_changes(scheme: SubtitleStyleScheme) -> dict[str, object]:
    return {
        field: value
        for field in SUBTITLE_SCHEME_STYLE_FIELDS
        if (value := getattr(scheme, field)) is not None
    }


def style_for_role(style: Style, role_label: str | None) -> Style:
    if not role_label:
        return style
    scheme = style.custom_style_schemes.get(role_label)
    if scheme is None:
        return style
    changes = style_scheme_changes(scheme)
    if scheme.n3_font_inheritance:
        changes.update(
            {field: getattr(scheme, field) for field in N3_FONT_INHERITANCE_FIELDS}
        )
    has_legacy_color_changes = any(
        getattr(scheme, field) is not None
        for field in (
            "base_color",
            "fill_color",
            "fill_gradient_enabled",
            "fill_gradient_start_color",
            "fill_gradient_end_color",
            "fill_gradient_angle_deg",
            "stroke_color",
            "shadow_color",
        )
    )
    if scheme.karaoke_colors is None and has_legacy_color_changes:
        changes["karaoke_colors"] = None
    if scheme.ruby_karaoke_colors is None and (
        scheme.karaoke_colors is not None or has_legacy_color_changes
    ):
        changes["ruby_karaoke_colors"] = None
    if not changes:
        return style
    return replace(style, **changes)


def solid_fill(color: str) -> PaintFill:
    return PaintFill(
        mode="solid",
        color=color,
        start_color=color,
        end_color=color,
        gradient_stops=[(0, color), (100, color)],
        split_top_color=color,
        split_bottom_color=color,
    )


def legacy_after_text_fill(style: Style) -> PaintFill:
    if not style.fill_gradient_enabled:
        return solid_fill(style.fill_color)
    mode = (
        "gradient_vertical"
        if style.fill_gradient_angle_deg in {90, 270}
        else "gradient_horizontal"
    )
    return PaintFill(
        mode=mode,
        color=style.fill_color,
        start_color=style.fill_gradient_start_color,
        end_color=style.fill_gradient_end_color,
        gradient_stops=[
            (0, style.fill_gradient_start_color),
            (100, style.fill_gradient_end_color),
        ],
        split_top_color=style.fill_gradient_start_color,
        split_bottom_color=style.fill_gradient_end_color,
    )


def effective_karaoke_colors(style: Style) -> KaraokeColors:
    if style.karaoke_colors is not None:
        return style.karaoke_colors

    before = KaraokeColorState(
        text=solid_fill(style.base_color),
        stroke=solid_fill(style.stroke_color),
        stroke2=solid_fill("#000000"),
        shadow=solid_fill(style.shadow_color),
    )
    after = KaraokeColorState(
        text=legacy_after_text_fill(style),
        stroke=solid_fill(style.stroke_color),
        stroke2=solid_fill("#000000"),
        shadow=solid_fill(style.shadow_color),
    )
    return KaraokeColors(before=before, after=after)
