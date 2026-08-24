"""Ruby font selection and inherited outline semantics."""

from __future__ import annotations

from dataclasses import replace

from PyQt6.QtGui import QFont

from krok_helper.subtitle_render.engine.text_metrics import (
    clamp_weight,
    is_n3_latin_text,
    latin_font_weight,
)
from krok_helper.subtitle_render.models import Style
from krok_helper.subtitle_render.n3_font_catalog import resolve_qt_font_family


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


def build_ruby_font(style: Style) -> QFont:
    family = (
        style.font_family
        if ruby_uses_main_font(style)
        else style.ruby_font_family or style.font_family
    )
    size = ruby_font_size(style)
    weight = (
        style.font_weight
        if ruby_uses_main_font(style)
        else style.ruby_font_weight
        if style.ruby_font_weight is not None and int(style.ruby_font_weight) > 0
        else style.font_weight
    )
    font = QFont(resolve_qt_font_family(family), size)
    font.setPixelSize(size)
    font.setWeight(clamp_weight(int(weight)))
    font.setItalic(style.italic)
    return font


def build_ruby_font_for_text(style: Style, reading: str) -> QFont:
    """Build the effective Japanese or Latin ruby font for one reading."""
    if not is_n3_latin_text(reading):
        return build_ruby_font(style)
    if ruby_uses_main_font(style):
        family = style.font_family_latin or style.font_family
        weight = latin_font_weight(style)
    else:
        family = (
            style.ruby_font_family_latin
            or style.ruby_font_family
            or style.font_family
        )
        weight = (
            int(style.ruby_latin_font_weight)
            if style.ruby_latin_font_weight is not None
            and int(style.ruby_latin_font_weight) > 0
            else int(style.ruby_font_weight)
            if style.ruby_font_weight is not None and int(style.ruby_font_weight) > 0
            else int(style.font_weight)
        )
    size = (
        int(style.ruby_latin_font_size_px)
        if style.ruby_latin_font_size_px is not None
        and int(style.ruby_latin_font_size_px) > 0
        else ruby_font_size(style)
    )
    font = QFont(resolve_qt_font_family(family), max(size, 1))
    font.setPixelSize(max(size, 1))
    font.setWeight(clamp_weight(weight))
    font.setItalic(style.italic)
    return font


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


__all__ = [
    "build_ruby_font",
    "build_ruby_font_for_text",
    "ruby_font_size",
    "ruby_scale",
    "ruby_script_stroke_style",
    "ruby_stroke2_enabled",
    "ruby_stroke2_width",
    "ruby_stroke2_width_value",
    "ruby_stroke_width",
    "ruby_uses_main_font",
    "scaled_px",
    "scaled_signed_px",
]
