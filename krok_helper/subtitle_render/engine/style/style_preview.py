"""Public rendering primitives used by the subtitle style preview UI.

The property panel must not depend on Painter's private implementation names.
This module is the compatibility boundary while the primitives are migrated out
of the CPU renderer incrementally.  Keeping direct aliases preserves the exact
call signatures and rendering behaviour of the existing preview.
"""

from __future__ import annotations

from krok_helper.subtitle_render.engine.text import (
    build_font,
    build_latin_font,
    main_script_stroke_style,
    n3_char_box_ascent,
)
from krok_helper.subtitle_render.engine.render.effects import (
    glow_extent,
    main_stroke2_width,
    ruby_baseline_y,
    ruby_decoration_kind,
    ruby_glow_radius,
    ruby_shadow_dx,
    ruby_shadow_dy,
)
from krok_helper.subtitle_render.engine.ruby import (
    build_ruby_font_for_text,
    ruby_script_stroke_style,
    ruby_stroke2_width,
    ruby_stroke_width,
)
from krok_helper.subtitle_render.engine.render.elements.horizontal import (
    paint_char_karaoke_stack,
)
from krok_helper.subtitle_render.engine.painter import (
    _paint_ruby_karaoke_fragment as paint_ruby_karaoke_fragment,
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
