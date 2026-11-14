"""Pure ruby font-slot, outline and role inheritance; no font discovery."""
from dataclasses import replace
from krok_helper.subtitle_render.domain.models import Style
from krok_helper.subtitle_render.domain.timing import TimingLine
from krok_helper.subtitle_render.engine.text.font_slots import is_n3_latin_text
from krok_helper.subtitle_render.engine.style.style_semantics import style_for_role_in_layout


def font_style_for_ruby_cell(style: Style, text: str) -> tuple[Style, float]:
    """Mirror the GPU ruby slots, including their distinct Latin inheritance.

    Follow-main ruby inherits the main Japanese weight, even for Latin units.
    An unset independent ruby Latin family prefers main Latin before ruby main.
    """
    follow = ruby_uses_main_font(style)
    family = style.font_family if follow else style.ruby_font_family or style.font_family
    weight = (style.ruby_font_weight if not follow and style.ruby_font_weight is not None
              and int(style.ruby_font_weight) > 0 else style.font_weight)
    size, italic, stretch = ruby_font_size(style), ruby_italic_value(style), 1.0
    if is_n3_latin_text(text):
        family = (style.font_family_latin or style.font_family) if follow else (
            style.ruby_font_family_latin or style.font_family_latin
            or style.ruby_font_family or style.font_family)
        if not follow and style.ruby_latin_font_weight is not None and int(style.ruby_latin_font_weight) > 0:
            weight = style.ruby_latin_font_weight
        if style.ruby_latin_font_size_px is not None and int(style.ruby_latin_font_size_px) > 0:
            size = int(style.ruby_latin_font_size_px)
        italic = ruby_latin_italic_value(style)
        value = (style.ruby_latin_font_stretch_pct if style.ruby_latin_font_stretch_pct is not None
                 else style.latin_font_stretch_pct)
        stretch = max(50, min(200, int(value))) / 100
    return replace(style, font_family=family, font_size_px=max(size, 1),
                   font_weight=int(weight), italic=italic), stretch


def ruby_uses_main_font(style: Style) -> bool:
    """Return whether an untouched ruby font slot still follows main text."""
    return bool(style.ruby_font_follow_main) and all(
        value is None
        for value in (
            style.ruby_font_family,
            style.ruby_font_family_latin,
            style.ruby_font_weight,
            style.ruby_latin_font_size_px,
            style.ruby_latin_font_weight,
        )
    ) and int(style.ruby_font_size_px) == 45


def ruby_font_size(style: Style) -> int:
    return max(int(style.ruby_font_size_px), 1)


def ruby_italic_value(style: Style) -> bool:
    """注音·日文槽斜体的生效值：``None`` = 跟随主文字（与描边 2 同链）。"""
    value = style.ruby_italic
    return bool(style.italic if value is None else value)


def ruby_latin_italic_value(style: Style) -> bool:
    """注音·英数槽斜体的生效值：``None`` = 跟随注音·日文槽的生效值。"""
    value = style.ruby_latin_italic
    return bool(ruby_italic_value(style) if value is None else value)


def ruby_scale(style: Style) -> float:
    return ruby_font_size(style) / max(style.font_size_px, 1)


def scaled_px(value: int, scale: float) -> int:
    if value <= 0:
        return 0
    return max(1, int(round(value * scale)))


def scaled_signed_px(value: int, scale: float) -> int:
    if value == 0:
        return 0
    sign = 1 if value > 0 else -1
    return sign * max(1, int(round(abs(value) * scale)))


def ruby_stroke_width(style: Style) -> int:
    if style.ruby_stroke_width_px is not None:
        return max(int(style.ruby_stroke_width_px), 0)
    return scaled_px(style.stroke_width_px, ruby_scale(style))


def ruby_stroke2_enabled(style: Style) -> bool:
    return (
        style.stroke2_enabled
        if style.ruby_stroke2_enabled is None
        else bool(style.ruby_stroke2_enabled)
    )


def ruby_stroke2_width_value(style: Style) -> int:
    """Return the inherited second-outline width before applying its switch."""
    if style.ruby_stroke2_width_px is not None:
        return max(int(style.ruby_stroke2_width_px), 0)
    return scaled_px(style.stroke2_width_px, ruby_scale(style))


def ruby_stroke2_width(style: Style) -> int:
    return ruby_stroke2_width_value(style) if ruby_stroke2_enabled(style) else 0


def ruby_script_stroke_style(style: Style, reading: str) -> Style:
    """Materialize the reading script's outlines into common ruby fields."""
    if not is_n3_latin_text(reading):
        return style
    width = (
        ruby_stroke_width(style)
        if style.ruby_latin_stroke_width_px is None
        or int(style.ruby_latin_stroke_width_px) <= 0
        else max(int(style.ruby_latin_stroke_width_px), 0)
    )
    enabled = (
        ruby_stroke2_enabled(style)
        if style.ruby_latin_stroke2_enabled is None
        else bool(style.ruby_latin_stroke2_enabled)
    )
    width2 = (
        ruby_stroke2_width_value(style)
        if style.ruby_latin_stroke2_width_px is None
        or int(style.ruby_latin_stroke2_width_px) <= 0
        else max(int(style.ruby_latin_stroke2_width_px), 0)
    )
    return replace(
        style,
        ruby_stroke_width_px=width,
        ruby_stroke2_enabled=True,
        ruby_stroke2_width_px=width2 if enabled else 0,
    )


def ruby_style_for_target_indices(
    style: Style,
    line: TimingLine,
    indices: list[int],
) -> Style:
    """Resolve the first role-specific style covered by a ruby target."""
    for index in indices:
        if 0 <= index < len(line.chars):
            role_label = line.chars[index].role_label
            if role_label:
                return style_for_role_in_layout(style, role_label)
    return style

