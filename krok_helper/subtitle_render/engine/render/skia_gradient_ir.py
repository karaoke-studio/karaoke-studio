"""Project the line-wide coordinates needed by Skia body and outline gradients and image paints."""

from ..style.style_semantics import (
    effective_karaoke_colors, effective_ruby_karaoke_colors, style_for_role_in_layout, uses_authored_decoration,
)
from ..guide.semantics import guide_symbol_is_bitmap
from .skia_line_animation_ir import ordinary_line_animation
from .skia_particle_ir import faded_band_singing_particles
from .skia_ruby_paint_ir import ruby_decoration_kinds
from .skia_scanline_ir import fixed_faded_utopia_band


_GRADIENTS = {'gradient_horizontal', 'gradient_vertical', 'split_vertical'}
_TEXTURED = _GRADIENTS | {'image'}


def _declare_role_body_paints(ir, plan):
    palettes = []
    has_roles = False
    for item in plan.lines:
        look = item.layout_style
        labels = {cell.role_label for cell in item.render_line.chars
                  if cell.role_label in look.custom_style_schemes}
        has_roles |= bool(labels)
        appearances = (look, *(style_for_role_in_layout(look, label) for label in labels))
        palettes.extend(effective_karaoke_colors(appearance) for appearance in appearances)
    if not has_roles:
        return False
    states = [state for colors in palettes for state in (colors.before, colors.after)]
    features = ir['required_features']
    layers = [layer for layer, feature in (('stroke', 'p06_primary_solid_outline'),
                                            ('stroke2', 'p06_secondary_solid_outline')) if feature in features]
    outlines = {getattr(state, layer).mode for state in states for layer in layers} - {'solid'}
    if not outlines and not any(state.text.mode in _TEXTURED for state in states):
        return False
    if outlines:
        if 'p08_inline_bitmap_guides' in features and outlines <= {'gradient_vertical', 'split_vertical'}:
            features.append('p06_role_outline_gradient')
        else:
            features.append('p06_role_outline_paints')
    features.append('p06_role_body_paints')
    return True


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


def _decoration_paints(palette, before, after, decoration):
    return {state.shadow.mode for state, kind in ((palette.before, before), (palette.after, after))
            if kind == decoration}


def _body_decoration_kinds(style):
    after = style.decoration_kind
    return after if style.decoration_before_sync else (style.decoration_kind_before or after), after


def _gradient_glow_paints(layout, plan):
    modes = set()
    for item in plan.lines:
        base = item.layout_style
        labels = {cell.role_label for cell in item.render_line.chars if cell.role_label in base.custom_style_schemes}
        for look in (base, *(style_for_role_in_layout(base, label) for label in labels)):
            modes.update(_decoration_paints(effective_karaoke_colors(look), *_body_decoration_kinds(look), 'glow'))
    for look in _upper_reading_styles(layout, plan):
        modes.update(_decoration_paints(effective_ruby_karaoke_colors(look), *ruby_decoration_kinds(look), 'glow'))
    return modes & _GRADIENTS


def write_body_gradient_bounds(ir, plan):
    """Write the glyph ink union and declare the corresponding paint capabilities.

    The existing GPU maps every gradient layer of a line onto this ink union: a
    horizontal paint spans the glyph ink, a vertical paint spans the ink padded
    by the line's visual outline pad (applied natively at the reference scale).
    Image paints need no line domain: the existing GPU tiles them on the canvas.
    """
    glow_features = {'p06_static_glow', 'p06_role_static_glow', 'p06_ruby_glow'}
    features = ir['required_features']
    layout = ir['skia_layout']
    if 'p06_role_static_shadow' in features:
        for item in plan.lines:
            base = item.layout_style
            labels = {cell.role_label for cell in item.render_line.chars if cell.role_label in base.custom_style_schemes}
            if any(_decoration_paints(effective_karaoke_colors(look), *_body_decoration_kinds(look), 'shadow')
                   & _TEXTURED for look in (style_for_role_in_layout(base, label) for label in labels)):
                features.append('p06_role_shadow_paints')
                break
    glow_paints = _gradient_glow_paints(layout, plan) if glow_features.intersection(features) else set()
    if 'p06_static_shadow' in features:
        body_modes = set().union(*(
            _decoration_paints(effective_karaoke_colors(item.layout_style),
                               *_body_decoration_kinds(item.layout_style), 'shadow') for item in plan.lines
        ))
        if body_modes & _GRADIENTS:
            features.append('p06_body_gradient_shadow')
        if 'image' in body_modes:
            features.append('p06_body_image_shadow')
    if 'p06_ruby_shadow' in features:
        ruby_modes = set().union(*(
            _decoration_paints(effective_ruby_karaoke_colors(look), *ruby_decoration_kinds(look), 'shadow')
            for look in _upper_reading_styles(layout, plan)
        ))
        if ruby_modes & _GRADIENTS:
            features.append('p06_ruby_gradient_shadow')
        if 'image' in ruby_modes:
            features.append('p06_ruby_image_shadow')
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
    unscaled_paint_fade = False
    unscaled_paint_scanline = False
    directional_paints = False
    directional_solid_outline = False
    vertical_paints = False
    unannotated_singing_paints = False
    unannotated_zoom_paints = False
    independent_ruby_fill = False
    upper_ruby = 'p05_upper_ruby' in ir['required_features']
    for final, item in zip(ir['skia_layout']['lines'], plan.lines, strict=True):
        look = item.layout_style
        modes, outlined = _layer_modes(look)
        source = ir['track']['lines'][item.track_index]
        animations = {source.get('entry_anim'), source.get('exit_anim')}
        annotated = bool(final.get('ruby_groups'))
        directional = look.right_to_left or item.line.wipe_reverse
        static_paints = (animations == {'none'} and source.get('karaoke_anim') == 'none'
                         and not (look.lower_ruby_enabled or ir['track'].get('rubies') or upper_ruby or annotated)
                         and not any(source.get(flag) for flag in ('scanline', 'fx_bursts', 'zoom_pulse', 'stroke_flash'))
                         and not any(uses_authored_decoration(look, kind) for kind in ('shadow', 'glow')))
        static_directional = static_paints and not look.vertical and look.right_to_left != item.line.wipe_reverse
        static_vertical = static_paints and look.vertical
        directional_solid_outline |= (static_directional and outlined and modes == {'solid'}
                                      and all(c.role_label is None and c.vector_glyph is None
                                              for c in item.render_line.chars))
        ruby_modes = set()
        if annotated:
            colors = effective_ruby_karaoke_colors(look)
            ruby_modes = {colors.before.text.mode, colors.after.text.mode}
            independent_gradient = bool(ruby_modes & _GRADIENTS) and not modes & _GRADIENTS
            independent_image = 'image' in ruby_modes and 'image' not in modes
            independent_ruby_fill |= (
                (independent_gradient or independent_image)
                and not (look.vertical or look.right_to_left or item.line.wipe_reverse or look.lower_ruby_enabled)
                and animations == {'none'} and source.get('karaoke_anim') in {'none', 'utopia'}
                and not any(source.get(flag) for flag in ('scanline', 'fx_bursts', 'zoom_pulse', 'stroke_flash'))
                and not any(cell.role_label or cell.vector_glyph for cell in item.render_line.chars))
        if not modes & _TEXTURED:
            continue
        particle_band = ('p07_faded_scanline_singing_particles' in ir['required_features']
                         and faded_band_singing_particles(source, look) and look.scanline_mode in {'default', 'color'})
        prefix_band = ('p07_faded_scanline_prefix_guides' in ir['required_features']
                       and (fixed_faded_utopia_band(source, look) or particle_band))
        faded_band = (prefix_band or particle_band) and modes <= {'solid', 'gradient_vertical'}
        paint_fade = ('fade' in animations and animations <= {'none', 'fade'}
                      and source.get('karaoke_anim') == 'none' and not (annotated or upper_ruby)
                      and not (source.get('scanline') or source.get('fx_bursts') or source.get('zoom_pulse')))
        paint_scanline = (bool(source.get('scanline')) and look.scanline_shape == 'band'
                          and look.scanline_mode in {'default', 'color'} and animations == {'none'}
                          and source.get('karaoke_anim') == 'none' and not (annotated or upper_ruby)
                          and not (source.get('fx_bursts') or source.get('zoom_pulse')))
        palette_band = (source.get('scanline') and look.scanline_shape == 'band' and look.scanline_mode == 'brighten'
                        and source.get('karaoke_anim') == 'utopia' and not source.get('zoom_pulse')
                        and not source.get('fx_bursts') and 'fade' in animations and animations <= {'none', 'fade'}
                        and modes <= {'solid', 'gradient_vertical'})
        moving = (not source.get('scanline') and not source.get('fx_bursts')
                  and animations != {'none'}
                  and ordinary_line_animation(source) and modes <= {'solid', 'gradient_vertical'}
                  and source.get('karaoke_anim') == 'utopia' and not source.get('zoom_pulse'))
        unannotated_singing = (not (look.vertical or look.right_to_left or upper_ruby or annotated)
                               and animations == {'none'}
                               and not any(source.get(flag) for flag in ('scanline', 'fx_bursts', 'stroke_flash'))
                               and not any(uses_authored_decoration(look, kind) for kind in ('shadow', 'glow')))
        zoom_paints = bool(source.get('zoom_pulse')) and unannotated_singing and not directional
        singing = (source.get('karaoke_anim') == 'utopia'
                   and (annotated or faded_band or palette_band or moving or unannotated_singing)
                   and (not source.get('zoom_pulse') or zoom_paints))
        if (not modes <= (_GRADIENTS | {'solid', 'image'})
                or (look.vertical and not static_vertical)
                or (directional and not (static_directional or static_vertical or (singing and unannotated_singing)))
                or (not (faded_band or palette_band or moving or paint_fade or paint_scanline) and (animations != {'none'}
                                                  or source.get('scanline') or source.get('fx_bursts')))
                or source.get('stroke_flash')
                or (source.get('karaoke_anim') != 'none' and not singing)
                or (look.lower_ruby_enabled and not (particle_band
                    and 'p07_lower_ruby_scanline_singing_particles' in ir['required_features']))
                or any(c.role_label is not None or (c.vector_glyph is not None
                       and (not (moving or prefix_band) or guide_symbol_is_bitmap(c.vector_glyph)))
                       for c in item.render_line.chars)):
            raise ValueError('Skia 正文渐变当前仅支持正向横排基础正文')
        ruby_paint |= annotated or (moving and upper_ruby)
        singing_paint |= singing and (annotated or moving)
        unannotated_singing_paints |= singing and unannotated_singing
        unannotated_zoom_paints |= singing and zoom_paints
        faded_scanline_gradient |= faded_band
        palette_scanline_gradient |= palette_band
        animated_singing_gradient |= moving
        unscaled_paint_fade |= paint_fade
        unscaled_paint_scanline |= paint_scanline
        directional_paints |= static_directional
        vertical_paints |= static_vertical
        if annotated:
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
            line_top, line_bottom = (final['cells'][0]['wipe_top'], final['cells'][-1]['wipe_bottom']
                                     ) if static_vertical else (min(top), max(bottom))
            final['body_gradient_bounds'] = [min(left), line_top, max(right), line_bottom]
        enabled = True
        outlined_gradient = outlined_gradient or outlined
    for enabled_feature, feature in (
        (enabled, 'p06_body_gradient'), (outlined_gradient, 'p06_outline_gradient'),
        (images, 'p06_image_fill'), (ruby_paint, 'p06_ruby_main_paint'),
        (singing_paint, 'p06_utopia_main_paint'),
        (unannotated_singing_paints, 'p06_unannotated_singing_paints'),
        (unannotated_zoom_paints, 'p06_unannotated_zoom_paints'),
        (ruby_gradient, 'p06_ruby_fill_gradient'),
        (ruby_image, 'p06_ruby_fill_image'),
        (faded_scanline_gradient, 'p06_faded_scanline_gradient_fill'),
        (palette_scanline_gradient, 'p06_faded_scanline_palette_gradient'),
        (animated_singing_gradient, 'p06_animated_singing_gradient_fill'),
        (unscaled_paint_fade, 'p06_unscaled_paint_fade'),
        (unscaled_paint_scanline, 'p06_unscaled_paint_scanline'),
        (directional_paints, 'p06_directional_body_paints'),
        (directional_solid_outline, 'p06_directional_solid_outline'),
        (vertical_paints, 'p06_vertical_body_paints'),
        (independent_ruby_fill, 'p06_independent_ruby_fill'),
    ):
        if enabled_feature:
            ir['required_features'].append(feature)
