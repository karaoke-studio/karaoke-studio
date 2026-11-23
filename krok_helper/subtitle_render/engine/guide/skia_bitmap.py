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

from ..render.skia_line_animation_ir import ordinary_line_animation
from ..style.style_semantics import effective_karaoke_colors, uses_authored_decoration
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


def validate_bitmap_guides(track, *, static_title=False):
    """Validate bitmap guide assets and placement."""
    animated = False
    for line in track.lines:
        rendered = render_line_with_guide_symbols(line)
        cells = [c for c in rendered.chars if guide_symbol_is_bitmap(c.vector_glyph)]
        if not cells:
            continue
        inline = not static_title and _needs_inline_bitmap_profile(line, cells)
        images = [cell.vector_glyph for cell in cells]
        paths = {path for image in images for path in (image.bitmap_before_path, image.bitmap_after_path) if path}
        for path in sorted(paths):
            animated |= QImageReader(path).imageCount() > 1
            if bitmap_guide_image(path) is None:
                raise ValueError('Skia 图片导唱符无法读取图片')
        if ((line.wipe_reverse and static_title)
                or (not inline and any(c.role_label is not None for c in cells))):
            raise ValueError('Skia 图片导唱符仅支持已验证的横排正向正文子集')
    return animated


def _has_bitmap_gradient_glow(item):
    style = item.layout_style
    colors = effective_karaoke_colors(style)
    before = style.decoration_kind
    if not style.decoration_before_sync:
        before = style.decoration_kind_before or before
    phases = ((before, colors.before), (style.decoration_kind, colors.after))
    return (any(kind == 'glow' and phase.shadow.mode in {'gradient_horizontal', 'gradient_vertical', 'split_vertical'}
                for kind, phase in phases)
            and has_bitmap_guides((item.render_line,)))


def declare_bitmap_guides(plan_lines, ir, *, static_title=False):
    """Declare capabilities for the bitmap cells present in this source."""
    if not has_bitmap_guides(item.render_line for item in plan_lines):
        return
    features = ir['required_features']
    features.append('p08_bitmap_guides')
    ir['skia_layout']['bitmap_guide_schema'] = 1
    if not static_title and 'p05_vector_guides' in features:
        features.append('p08_mixed_guide_kinds')
    vertical_lines = [item for item in plan_lines
                      if item.layout_style.vertical and has_bitmap_guides((item.render_line,))]
    for item in vertical_lines:
        source = ir['track']['lines'][item.track_index]
        if (static_title or source.get('karaoke_anim') != 'none'
                or not ordinary_line_animation(source)
                or any(source.get(flag) for flag in ('scanline', 'zoom_pulse', 'stroke_flash', 'fx_bursts'))):
            raise ValueError('Skia 竖排图片导唱符需要整行出入场和普通走字')
    if vertical_lines:
        features.append('p08_vertical_bitmap_guides')
        if {'p06_vertical_shadow', 'p06_vertical_glow'}.intersection(features):
            features.append('p08_vertical_bitmap_decoration')
    if 'p08_inline_bitmap_guides' in features:
        for item in plan_lines:
            cells = [cell for cell in item.render_line.chars if guide_symbol_is_bitmap(cell.vector_glyph)]
            if _needs_inline_bitmap_profile(item.line, cells) and any(not cell.vector_glyph.bitmap_no_decor for cell in cells):
                features.append('p08_inline_bitmap_decoration')
                break
    if 'p05_horizontal_rtl' in features:
        features.append('p08_rtl_bitmap_guides')
        rtl_requirements = set()
        for item, final in zip(plan_lines, ir['skia_layout']['lines'], strict=True):
            if not item.layout_style.right_to_left or not has_bitmap_guides((item.render_line,)):
                continue
            has_reading = bool(final.get('ruby_groups'))
            decorates_bitmap = any(guide_symbol_is_bitmap(cell.vector_glyph) and not cell.vector_glyph.bitmap_no_decor
                                   for cell in item.render_line.chars)
            # The static RTL profile carries the lyric paint as well as the guide paint.
            body_decoration = not has_reading and any(
                uses_authored_decoration(item.layout_style, kind) for kind in ('shadow', 'glow'))
            if not (has_reading or decorates_bitmap or body_decoration):
                continue
            source = ir['track']['lines'][item.track_index]
            singing = (has_reading and 'p07_rtl_ruby_singing_scale' in features
                       and source.get('karaoke_anim') == 'utopia'
                       and not uses_authored_decoration(item.layout_style, 'shadow'))
            if (item.line.wipe_reverse or (source.get('karaoke_anim') != 'none' and not singing)
                    or not ordinary_line_animation(source)
                    or any(source.get(flag) for flag in ('scanline', 'zoom_pulse', 'stroke_flash', 'fx_bursts'))
                    or any(cell.role_label is not None for cell in item.render_line.chars)):
                raise ValueError('Skia 右至左图片导唱符装饰或注音当前需要整行出入场和普通走字或 Utopia')
            if decorates_bitmap or body_decoration:
                rtl_requirements.add('p08_rtl_bitmap_guide_decoration')
            if has_reading:
                rtl_requirements.add('p08_rtl_bitmap_upper_ruby')
            if singing:
                rtl_requirements.add('p08_rtl_bitmap_singing')
        features.extend(sorted(rtl_requirements))
    if 'p05_lower_ruby' in features:
        for item, final in zip(plan_lines, ir['skia_layout']['lines'], strict=True):
            if (not has_bitmap_guides((item.render_line,))
                    or not any(group.get('lower') for group in final.get('ruby_groups', ()))):
                continue
            source = ir['track']['lines'][item.track_index]
            if (item.line.wipe_reverse
                    or source.get('karaoke_anim') not in {'none', 'no_wipe', 'utopia'}
                    or not ordinary_line_animation(source)
                    or any(source.get(flag) for flag in ('scanline', 'zoom_pulse', 'stroke_flash', 'fx_bursts'))):
                raise ValueError('Skia 图片导唱符与下方注音的行布局或动画尚未支持')
        features.append('p08_bitmap_lower_ruby')
    if any(uses_authored_decoration(item.layout_style, 'shadow')
           and any(guide_symbol_is_bitmap(cell.vector_glyph) and not cell.vector_glyph.bitmap_no_decor
                   for cell in item.render_line.chars)
           for item in plan_lines):
        features.append('p08_bitmap_guide_shadow')
        if 'p06_body_gradient_shadow' in features:
            features.append('p08_bitmap_guide_gradient_shadow')
        if 'p06_body_image_shadow' in features:
            features.append('p08_bitmap_guide_image_shadow')
    if (not static_title and 'p06_gradient_glow' in features
            and any(_has_bitmap_gradient_glow(item) for item in plan_lines)):
        features.append('p08_bitmap_guide_gradient_glow')
