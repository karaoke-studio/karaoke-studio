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


def _needs_inline_bitmap_profile(line, cells):
    symbol = line.guide_symbol
    return (bool(line.inline_guide_symbols) or symbol is None or not guide_symbol_is_bitmap(symbol)
            or bool(symbol.replacement_prefix)
            or any(c.vector_glyph is not symbol or c.role_label is not None for c in cells))


def is_undecorated_bitmap_line(line):
    return bool(line.chars) and all(guide_symbol_is_bitmap(c.vector_glyph) and c.vector_glyph.bitmap_no_decor
                                    for c in line.chars)


def uses_inline_bitmap_guides(plan_lines):
    for item in plan_lines:
        cells = [c for c in item.render_line.chars if guide_symbol_is_bitmap(c.vector_glyph)]
        if cells and _needs_inline_bitmap_profile(item.line, cells):
            return True
    return False


def validate_bitmap_guides(track, style, *, static_title=False):
    """Validate line-prefix guides and undecorated static inline avatars."""
    from ..layout.line.style import style_for_line
    animated = False
    for line in track.lines:
        rendered = render_line_with_guide_symbols(line)
        cells = [c for c in rendered.chars if guide_symbol_is_bitmap(c.vector_glyph)]
        if not cells:
            continue
        inline = not static_title and _needs_inline_bitmap_profile(line, cells)
        if inline and any(not cell.vector_glyph.bitmap_no_decor for cell in cells):
            raise ValueError('Skia 行内头像暂不支持独立图片装饰')
        images = [cell.vector_glyph for cell in cells]
        paths = {path for image in images for path in (image.bitmap_before_path, image.bitmap_after_path) if path}
        for path in sorted(paths):
            if QImageReader(path).imageCount() > 1:
                if not static_title:
                    raise ValueError('Skia 图片导唱符暂不支持动图')
                animated = True
            if bitmap_guide_image(path) is None:
                raise ValueError('Skia 图片导唱符无法读取图片')
        appearance = style_for_line(style, line)
        if (appearance.vertical or appearance.right_to_left or appearance.lower_ruby_enabled
                or (line.wipe_reverse and not (inline and is_undecorated_bitmap_line(rendered)))
                or (not inline and any(c.role_label is not None for c in cells))):
            raise ValueError('Skia 图片导唱符仅支持已验证的横排正向正文子集')
        if not inline and (appearance.decoration_kind == 'shadow' or (
                not appearance.decoration_before_sync and appearance.decoration_kind_before == 'shadow')):
            raise ValueError('Skia 图片导唱符暂不支持阴影装饰')
    return animated


# Verified with the P00 line-prefix bitmap guide: Utopia singing, upper ruby,
# solid double outlines and glow. Other capabilities stay rejected.
BITMAP_GUIDE_FEATURES = frozenset({
    'p05_fixed_row_grid', 'p05_inline_roles', 'p05_overlap_wipe', 'p05_page_collisions',
    'p05_resolved_horizontal', 'p05_resolved_ruby', 'p05_segmented_wipe', 'p05_single_line_placement',
    'p05_smart_horizontal', 'p05_upper_ruby', 'p08_bitmap_guides',
    'p06_outline_ruby', 'p06_primary_solid_outline', 'p06_ruby_glow', 'p06_ruby_solid_outline',
    'p06_secondary_solid_outline', 'p06_static_glow', 'p07_ruby_singing_scale', 'p07_singing_glow',
    'p07_singing_scale'})
INLINE_BITMAP_GUIDE_FEATURES = frozenset({
    'p08_inline_bitmap_guides', 'p05_font_fallback', 'p05_mixed_fonts',
    'p05_ruby_units', 'p05_ruby_markers', 'p05_standalone_marks',
    'p06_role_solid_outlines', 'p06_multi_lane_outline', 'p06_role_body_paints',
    'p06_ruby_role_paints', 'p06_role_static_glow', 'p06_ruby_role_glow',
    'p06_role_outline_gradient', 'p06_ruby_outline_gradient',
    'p06_static_shadow', 'p06_ruby_shadow', 'p06_gradient_glow',
    'p06_vertical_gradient_glow', 'p06_role_gradient_glow',
    'p07_role_singing_scale', 'p07_role_singing_glow', 'p07_line_fade',
    'p07_role_line_fade', 'p07_ruby_line_animation', 'p07_utopia_line_animation'})
REVERSE_BITMAP_GUIDE_FEATURES = frozenset({
    'p08_reverse_bitmap_guides', 'p05_reverse_wipe', 'p05_reverse_overlap', 'p06_static_shadow'})


def declare_bitmap_guides(plan_lines, ir, *, static_title=False):
    """Append p08_bitmap_guides, or reject an unverified combination."""
    if not has_bitmap_guides(item.render_line for item in plan_lines):
        return
    features = ir['required_features']
    features.append('p08_bitmap_guides')
    ir['skia_layout']['bitmap_guide_schema'] = 1
    allowed = BITMAP_GUIDE_FEATURES | {'p05_vector_guides'} if static_title else BITMAP_GUIDE_FEATURES
    if 'p08_inline_bitmap_guides' in features:
        allowed |= INLINE_BITMAP_GUIDE_FEATURES
    if 'p08_reverse_bitmap_guides' in features:
        allowed |= REVERSE_BITMAP_GUIDE_FEATURES
    if not set(features) <= allowed:
        raise ValueError('Skia 图片导唱符组合仅支持已验证的 Utopia、上注音、实色描边与光晕')
