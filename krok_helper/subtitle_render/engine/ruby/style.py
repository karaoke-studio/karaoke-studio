"""Ruby font selection and inherited outline semantics."""

from __future__ import annotations

from PyQt6.QtGui import QFont

from krok_helper.subtitle_render.engine.text.font_weight import build_weight_font
from krok_helper.subtitle_render.domain.models import Style
from krok_helper.subtitle_render.n3.font_catalog import resolve_qt_font_family
from .style_semantics import (
    ruby_uses_main_font,
    ruby_font_size,
    ruby_italic_value,
    ruby_latin_italic_value,
    ruby_scale,
    scaled_px,
    scaled_signed_px,
    ruby_stroke_width,
    ruby_stroke2_enabled,
    ruby_stroke2_width_value,
    ruby_stroke2_width,
    ruby_script_stroke_style,
    ruby_style_for_target_indices,
    font_style_for_ruby_cell,
)


def _build_ruby_slot(style: Style, text: str) -> QFont:
    selected, stretch = font_style_for_ruby_cell(style, text)
    font = build_weight_font(resolve_qt_font_family(selected.font_family),
                             selected.font_size_px, selected.font_weight, italic=selected.italic)
    if stretch != 1:
        font.setStretch(round(stretch * 100))
    return font

def build_ruby_font(style: Style) -> QFont:
    return _build_ruby_slot(style, "")

def build_ruby_font_for_text(style: Style, reading: str) -> QFont:
    return _build_ruby_slot(style, reading)


__all__ = [
    "build_ruby_font",
    "build_ruby_font_for_text",
    "ruby_font_size",
    "ruby_italic_value",
    "ruby_latin_italic_value",
    "ruby_scale",
    "ruby_script_stroke_style",
    "ruby_style_for_target_indices",
    "ruby_stroke2_enabled",
    "ruby_stroke2_width",
    "ruby_stroke2_width_value",
    "ruby_stroke_width",
    "ruby_uses_main_font",
    "scaled_px",
    "scaled_signed_px",
]
