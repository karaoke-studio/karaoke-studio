"""Public rendering primitives used by the subtitle style preview UI.

The property panel must not depend on Painter's private implementation names.
This module is the compatibility boundary while the primitives are migrated out
of the CPU renderer incrementally.  Keeping direct aliases preserves the exact
call signatures and rendering behaviour of the existing preview.
"""

from __future__ import annotations

from krok_helper.subtitle_render.engine.painter import (
    _build_font as build_font,
    _build_latin_font as build_latin_font,
    _build_ruby_font_for_text as build_ruby_font_for_text,
    _glow_extent as glow_extent,
    _main_script_stroke_style as main_script_stroke_style,
    _main_stroke2_width as main_stroke2_width,
    _n3_char_box_ascent as n3_char_box_ascent,
    _paint_char_karaoke_stack as paint_char_karaoke_stack,
    _paint_ruby_karaoke_fragment as paint_ruby_karaoke_fragment,
    _ruby_baseline_y as ruby_baseline_y,
    _ruby_decoration_kind as ruby_decoration_kind,
    _ruby_glow_radius as ruby_glow_radius,
    _ruby_script_stroke_style as ruby_script_stroke_style,
    _ruby_shadow_dx as ruby_shadow_dx,
    _ruby_shadow_dy as ruby_shadow_dy,
    _ruby_stroke2_width as ruby_stroke2_width,
    _ruby_stroke_width as ruby_stroke_width,
)

__all__ = [
    "build_font",
    "build_latin_font",
    "build_ruby_font_for_text",
    "glow_extent",
    "main_script_stroke_style",
    "main_stroke2_width",
    "n3_char_box_ascent",
    "paint_char_karaoke_stack",
    "paint_ruby_karaoke_fragment",
    "ruby_baseline_y",
    "ruby_decoration_kind",
    "ruby_glow_radius",
    "ruby_script_stroke_style",
    "ruby_shadow_dx",
    "ruby_shadow_dy",
    "ruby_stroke2_width",
    "ruby_stroke_width",
]
