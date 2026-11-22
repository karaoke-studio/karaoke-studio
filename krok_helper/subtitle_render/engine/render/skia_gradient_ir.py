"""Project the line-wide coordinates needed by Skia body and outline gradients and image paints."""

from ..style.style_semantics import effective_karaoke_colors, effective_ruby_karaoke_colors, style_for_role_in_layout
from ..guide.semantics import guide_symbol_is_bitmap
from .skia_line_animation_ir import ordinary_line_animation
from .skia_particle_ir import faded_band_singing_particles
from .skia_ruby_paint_ir import ruby_decoration_kinds


_GRADIENTS = {'gradient_horizontal', 'gradient_vertical', 'split_vertical'}
_TEXTURED = _GRADIENTS | {'image'}


def _declare_role_body_paints(ir, plan):
    for item in plan.lines:
        look = item.layout_style
        labels = {cell.role_label for cell in item.render_line.chars
                  if cell.role_label in look.custom_style_schemes}
        if not labels:
            continue
        appearances = (look, *(style_for_role_in_layout(look, label) for label in labels))
        palettes = [effective_karaoke_colors(appearance) for appearance in appearances]
        if not any(state.text.mode in _TEXTURED for colors in palettes for state in (colors.before, colors.after)):
            continue
        if any(state.stroke.mode != 'solid' or state.stroke2.mode != 'solid'
               for colors in palettes for state in (colors.before, colors.after)):
            raise ValueError('Skia 正文渐变暂不支持角色的复杂描边')
        ir['required_features'].append('p06_role_body_paints')
        return True
    return False


def _layer_modes(look):
    """Paint modes of every drawn layer: body, then each present outline."""
    colors = effective_karaoke_colors(look)
    modes = {colors.before.text.mode, colors.after.text.mode}
    outlined = look.stroke_width_px > 0 or (look.stroke2_enabled and look.stroke2_width_px > 0)
    if look.stroke_width_px > 0:
        modes |= {colors.before.stroke.mode, colors.after.stroke.mode}
    if look.stroke2_enabled and look.stroke2_width_px > 0:
        modes |= {colors.before.stroke2.mode, colors.after.stroke2.mode}
    return modes, outlined


def _upper_reading_styles(layout, plan):
    for final, item in zip(layout['lines'], plan.lines, strict=True):
        for group in final.get('ruby_groups', ()):
            if group.get('lower'):
                continue
            role = item.render_line.chars[group['style_cell']].role_label
            yield style_for_role_in_layout(item.layout_style, role)


def _has_textured_upper_ruby(layout, plan):
    for look in _upper_reading_styles(layout, plan):
        palette = effective_ruby_karaoke_colors(look)
        if {palette.before.text.mode, palette.after.text.mode} & _TEXTURED:
            return True
    return False


def _glow_paints(palette, before, after):
    return {state.shadow.mode for state, kind in ((palette.before, before), (palette.after, after))
            if kind == 'glow'}


def _gradient_glow_paints(layout, plan):
    modes = set()
    for item in plan.lines:
        base = item.layout_style
        labels = {cell.role_label for cell in item.render_line.chars if cell.role_label in base.custom_style_schemes}
        for look in (base, *(style_for_role_in_layout(base, label) for label in labels)):
            before = look.decoration_kind if look.decoration_before_sync else (
                look.decoration_kind_before or look.decoration_kind)
            modes.update(_glow_paints(effective_karaoke_colors(look), before, look.decoration_kind))
    for look in _upper_reading_styles(layout, plan):
        modes.update(_glow_paints(effective_ruby_karaoke_colors(look), *ruby_decoration_kinds(look)))
    return modes & _GRADIENTS


def write_body_gradient_bounds(ir, plan):
    """Write the glyph ink union and declare the corresponding paint capabilities.

    The existing GPU maps every gradient layer of a line onto this ink union: a
    horizontal paint spans the glyph ink, a vertical paint spans the ink padded
    by the line's visual outline pad (applied natively at the reference scale).
    Image paints need no line domain: the existing GPU tiles them on the canvas.
    """
    glow_features = {'p06_static_glow', 'p06_role_static_glow', 'p06_ruby_glow'}
    glow_paints = (_gradient_glow_paints(ir['skia_layout'], plan)
                   if glow_features.intersection(ir['required_features']) else set())
    if _declare_role_body_paints(ir, plan):
        if _has_textured_upper_ruby(ir['skia_layout'], plan):
            ir['required_features'].append('p06_ruby_role_paints')
        if glow_paints:
            feature = ('p06_vertical_gradient_glow' if glow_paints == {'gradient_vertical'}
                       else 'p06_role_gradient_glow')
            ir['required_features'].append(feature)
        return
    if glow_paints:
        ir['required_features'].append('p06_gradient_glow')
    enabled = outlined_gradient = images = ruby_paint = singing_paint = ruby_gradient = False
    ruby_image = faded_scanline_gradient = animated_singing_gradient = palette_scanline_gradient = False
    upper_ruby = 'p05_upper_ruby' in ir['required_features']
    for final, item in zip(ir['skia_layout']['lines'], plan.lines, strict=True):
        look = item.layout_style
        modes, outlined = _layer_modes(look)
        if not modes & (_GRADIENTS | {'image'}):
            continue
        source = ir['track']['lines'][item.track_index]
        annotated = bool(final.get('ruby_groups'))
        faded_band = ('p07_faded_scanline_singing_particles' in ir['required_features']
                      and faded_band_singing_particles(source, look) and look.scanline_mode in {'default', 'color'}
                      and modes <= {'solid', 'gradient_vertical'})
        animations = {source.get('entry_anim'), source.get('exit_anim')}
        palette_band = (source.get('scanline') and look.scanline_shape == 'band' and look.scanline_mode == 'brighten'
                        and source.get('karaoke_anim') == 'utopia' and not source.get('zoom_pulse')
                        and not source.get('fx_bursts') and 'fade' in animations and animations <= {'none', 'fade'}
                        and modes <= {'solid', 'gradient_vertical'})
        moving = (upper_ruby and not source.get('scanline') and not source.get('fx_bursts')
                  and animations != {'none'}
                  and ordinary_line_animation(source) and modes <= {'solid', 'gradient_vertical'}
                  and source.get('karaoke_anim') == 'utopia' and not source.get('zoom_pulse'))
        singing = (source.get('karaoke_anim') == 'utopia' and (annotated or faded_band or palette_band or moving)
                   and not source.get('zoom_pulse'))
        if (not modes <= (_GRADIENTS | {'solid', 'image'})
                or look.vertical or look.right_to_left or item.line.wipe_reverse
                or (not (faded_band or palette_band or moving) and (animations != {'none'}
                                                  or source.get('scanline') or source.get('fx_bursts')))
                or source.get('stroke_flash')
                or (source.get('karaoke_anim') != 'none' and not singing) or look.lower_ruby_enabled
                or any(c.role_label is not None or (c.vector_glyph is not None
                       and (not moving or guide_symbol_is_bitmap(c.vector_glyph))) for c in item.render_line.chars)):
            raise ValueError('Skia 正文渐变当前仅支持正向横排基础正文')
        ruby_paint |= annotated or moving
        singing_paint |= singing and (annotated or moving)
        faded_scanline_gradient |= faded_band
        palette_scanline_gradient |= palette_band
        animated_singing_gradient |= moving
        if annotated:
            colors = effective_ruby_karaoke_colors(look)
            ruby_modes = {colors.before.text.mode, colors.after.text.mode}
            if (faded_band or palette_band or moving) and not ruby_modes <= {'solid', 'gradient_vertical'}:
                raise ValueError('Skia 整行出入场的上注音填充当前支持实色和纵向渐变')
            ruby_gradient = ruby_gradient or bool(modes & _GRADIENTS and ruby_modes & _GRADIENTS)
            ruby_image = ruby_image or ('image' in modes and 'image' in ruby_modes)
        images = images or 'image' in modes
        if not modes & _GRADIENTS:
            continue
        boxes = []
        for cell in final['cells']:
            left, top, right, bottom = cell['metrics']['ink']
            if left < right and top < bottom:
                x, y = cell['origin_x'], cell['baseline_y']
                boxes.append((x + left, y + top, x + right, y + bottom))
        if boxes:
            left, top, right, bottom = zip(*boxes, strict=True)
            final['body_gradient_bounds'] = [min(left), min(top), max(right), max(bottom)]
        enabled = True
        outlined_gradient = outlined_gradient or outlined
    for enabled_feature, feature in (
        (enabled, 'p06_body_gradient'), (outlined_gradient, 'p06_outline_gradient'),
        (images, 'p06_image_fill'), (ruby_paint, 'p06_ruby_main_paint'),
        (singing_paint, 'p06_utopia_main_paint'),
        (ruby_gradient, 'p06_ruby_fill_gradient'),
        (ruby_image, 'p06_ruby_fill_image'),
        (faded_scanline_gradient, 'p06_faded_scanline_gradient_fill'),
        (palette_scanline_gradient, 'p06_faded_scanline_palette_gradient'),
        (animated_singing_gradient, 'p06_animated_singing_gradient_fill'),
    ):
        if enabled_feature:
            ir['required_features'].append(feature)
