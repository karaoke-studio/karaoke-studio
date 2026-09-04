"""Guide-symbol materialization and Qt-backed measurement contracts."""

from krok_helper.subtitle_render.engine.guide.metrics import (
    bitmap_guide_content_size,
    bitmap_guide_frame_at,
    bitmap_guide_image,
    vector_glyph_width,
)
from krok_helper.subtitle_render.engine.guide.semantics import (
    guide_symbol_is_bitmap,
    render_line_with_guide_symbols,
)


__all__ = [
    "bitmap_guide_content_size",
    "bitmap_guide_frame_at",
    "bitmap_guide_image",
    "guide_symbol_is_bitmap",
    "render_line_with_guide_symbols",
    "vector_glyph_width",
]
