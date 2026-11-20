"""Canonical cell geometry of line-prefix bitmap guides (bitmap_guide_schema=1).

The existing GPU sizes a bitmap guide by its after image (else before):
``fix_size`` keeps the pixel size, otherwise the height is
``max(unit * zoom // 100, 1)`` and the width keeps the aspect ratio
(unrounded, at least 1). The advance is ``max(width + margins, 0)``. The
image bottom sits ``unit * descent // (ascent + descent) + edge // 2 -
margin_bottom`` below the baseline, from the cell's left edge plus the left
margin. Images are decoded through the shared guide image boundary; the
native renderer re-derives the same box from its own decode.
"""
from __future__ import annotations

import math

from PyQt6.QtGui import QImageReader

from .metrics import bitmap_guide_image, vector_glyph_width
from .semantics import guide_symbol_is_bitmap, render_line_with_guide_symbols


def bitmap_guide_box(symbol, size, font_metrics, edge_size):
    """Return ``(advance, ink)`` in logical pixels relative to origin/baseline."""
    unit = max(int(size), 1)
    image = bitmap_guide_image(symbol.bitmap_after_path) if symbol.bitmap_after_path else None
    if image is None:
        image = bitmap_guide_image(symbol.bitmap_before_path)
    content_w, content_h = 1.0, float(unit)
    if image is not None and not image.isNull():
        image_w, image_h = max(float(image.width()), 1.0), max(float(image.height()), 1.0)
        if symbol.bitmap_fix_size:
            content_w, content_h = image_w, image_h
        else:
            content_h = float(max(unit * max(int(symbol.bitmap_zoom_percent), 1) // 100, 1))
            content_w = max(content_h * image_w / image_h, 1.0)
    ascent, descent = float(font_metrics['ascent']), float(font_metrics['descent'])
    if not (ascent >= 0 and descent >= 0 and ascent + descent > 0):
        raise ValueError('Skia 图片导唱符缺少有效的字体纵向度量')
    # Integer design-unit ratio of the existing GPU; tolerate float noise only.
    anchor = math.floor(unit * descent / (ascent + descent) + 1e-6) + max(int(edge_size), 0) // 2
    left = float(symbol.bitmap_margin_left_px)
    bottom = float(anchor - int(symbol.bitmap_margin_bottom_px))
    advance = max(content_w + left + float(symbol.bitmap_margin_right_px), 0.0)
    return advance, (left, bottom - content_h, left + content_w, bottom)


def bitmap_guide_identity(symbol):
    from krok_helper.subtitle_render.native.protocol import bitmap_guide_to_ir
    payload = bitmap_guide_to_ir(symbol)
    payload.pop('anim_anchor_ms')
    return 'bitmap:' + ':'.join(f'{key}={payload[key]}' for key in sorted(payload))


def bitmap_guide_metrics(symbol, size, font_metrics, edge_size):
    """Cell metrics payload: glyphless; the image box is the cell's ink."""
    advance, ink = bitmap_guide_box(symbol, size, font_metrics, edge_size)
    return {'advance': advance, 'ascent': float(font_metrics['ascent']),
            'descent': float(font_metrics['descent']), 'leading': float(font_metrics.get('leading', 0.0)),
            'ink': list(ink), 'glyphs': [], 'outline_available': True,
            'design_advance': 0.0, 'design_left_bearing': 0.0, 'design_right_bearing': 0.0,
            'font_identity': bitmap_guide_identity(symbol), 'bitmap_guide': True, 'selected_font': 0}


def bitmap_guide_timing_width(symbol, style):
    """The shared timeline keeps the existing integer guide width."""
    return vector_glyph_width(symbol, style)


def has_bitmap_guides(lines):
    return any(guide_symbol_is_bitmap(c.vector_glyph) for line in lines for c in line.chars)


def validate_bitmap_guides(track, style):
    """bitmap_guide_schema=1: one static line-prefix image guide per line.

    Verified subset: horizontal LTR forward lines with a pre-roll/anchored
    prefix (never replacement or inline), static images, no roles, and only
    solid glow decoration. Shadows, animated images and other effects stay
    rejected rather than approximated.
    """
    from ..layout.line.style import style_for_line
    for line in track.lines:
        cells = [c for c in render_line_with_guide_symbols(line).chars if guide_symbol_is_bitmap(c.vector_glyph)]
        if not cells:
            continue
        symbol = line.guide_symbol
        if (line.inline_guide_symbols or symbol is None or not guide_symbol_is_bitmap(symbol)
                or symbol.replacement_prefix or any(c.vector_glyph is not symbol for c in cells)):
            raise ValueError('Skia 图片导唱符仅支持整行前置导唱符')
        for path in (symbol.bitmap_before_path, symbol.bitmap_after_path):
            if path and QImageReader(path).imageCount() > 1:
                raise ValueError('Skia 图片导唱符暂不支持动图')
            if path and bitmap_guide_image(path) is None:
                raise ValueError('Skia 图片导唱符无法读取图片')
        appearance = style_for_line(style, line)
        if (appearance.vertical or appearance.right_to_left or appearance.lower_ruby_enabled or line.wipe_reverse
                or any(c.role_label is not None for c in cells)):
            raise ValueError('Skia 图片导唱符仅支持已验证的横排正向正文子集')
        if appearance.decoration_kind == 'shadow' or (
                not appearance.decoration_before_sync and appearance.decoration_kind_before == 'shadow'):
            raise ValueError('Skia 图片导唱符暂不支持阴影装饰')


# Verified with the P00 line-prefix bitmap guide: Utopia singing, upper ruby,
# solid double outlines and glow. Other capabilities stay rejected.
BITMAP_GUIDE_FEATURES = frozenset({
    'p05_fixed_row_grid', 'p05_inline_roles', 'p05_overlap_wipe', 'p05_page_collisions',
    'p05_resolved_horizontal', 'p05_resolved_ruby', 'p05_segmented_wipe', 'p05_single_line_placement',
    'p05_smart_horizontal', 'p05_upper_ruby', 'p08_bitmap_guides',
    'p06_outline_ruby', 'p06_primary_solid_outline', 'p06_ruby_glow', 'p06_ruby_solid_outline',
    'p06_secondary_solid_outline', 'p06_static_glow', 'p07_ruby_singing_scale', 'p07_singing_glow',
    'p07_singing_scale'})


def declare_bitmap_guides(plan_lines, ir):
    """Append p08_bitmap_guides, or reject an unverified combination."""
    if not has_bitmap_guides(item.render_line for item in plan_lines):
        return
    features = ir['required_features']
    features.append('p08_bitmap_guides')
    ir['skia_layout']['bitmap_guide_schema'] = 1
    if not set(features) <= BITMAP_GUIDE_FEATURES:
        raise ValueError('Skia 图片导唱符组合仅支持已验证的 Utopia、上注音、实色描边与光晕')
