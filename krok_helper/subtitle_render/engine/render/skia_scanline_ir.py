"""Resolve Skia scanline capabilities and the serialized shape and band versions."""

from ..guide.semantics import render_line_with_guide_symbols
from ..guide.skia_vector import vector_guides_form_prefix
from ..layout.line.style import style_for_line
from ..style.style_semantics import effective_karaoke_colors, uses_authored_decoration
from .elements.horizontal.scanline import scanline_role_fill
from .skia_line_animation_ir import ordinary_line_animation
from .skia_particle_ir import faded_band_singing_particles, vertical_singing_particle_modes


def suppress_inactive_scanline_bands(ir, plan):
    for item in plan.lines:
        line = ir['track']['lines'][item.track_index]
        if not line.get('scanline'):
            continue
        look = item.layout_style
        inactive_vertical_shape = look.vertical and (
            look.scanline_shape != 'band' or look.scanline_mode not in {'color', 'brighten', 'follow_before', 'follow_after'})
        if line.get('karaoke_anim') != 'no_wipe' and not inactive_vertical_shape:
            continue
        # Unsupported vertical shapes have no paint path. Horizontal
        # whole-unit coloring keeps the separate scanline particle clocks.
        line['scanline'] = False
        if look.vertical and line.get('fx_bursts'):
            line['fx_bursts'] = [burst for burst in line['fx_bursts'] if burst['kind'] != 'scanline']


def fixed_faded_utopia_band(line, style):
    animations = {line.get('entry_anim'), line.get('exit_anim')}
    return (line.get('scanline') and style.scanline_shape == 'band'
            and style.scanline_mode in {'default', 'color'} and line.get('karaoke_anim') == 'utopia'
            and 'fade' in animations and animations <= {'none', 'fade'}
            and not (style.vertical or style.right_to_left or style.lower_ruby_enabled or line.get('wipe_reverse')
                     or line.get('stroke_flash') or line.get('zoom_pulse') or line.get('fx_bursts')))


def transformed_cell_band(line, style):
    animations = {line.get('entry_anim'), line.get('exit_anim')}
    transforms = {'char_drip', 'spin_flip'}
    return (line.get('scanline') and bool(animations & transforms) and animations <= transforms | {'none'}
            and line.get('karaoke_anim') == 'none' and style.scanline_shape == 'band'
            and style.scanline_mode in {'default', 'color'}
            and not (style.vertical or style.right_to_left or style.lower_ruby_enabled or line.get('wipe_reverse')
                     or line.get('stroke_flash') or line.get('zoom_pulse') or line.get('fx_bursts')))


def utopia_entry_band(line, style):
    return (line.get('scanline') and line.get('entry_anim') == 'utopia' and line.get('exit_anim') == 'none'
            and line.get('karaoke_anim') == 'utopia' and style.scanline_shape == 'band'
            and style.scanline_mode in {'default', 'color'}
            and not (style.vertical or style.right_to_left or style.lower_ruby_enabled or line.get('wipe_reverse')
                     or line.get('stroke_flash') or line.get('zoom_pulse') or line.get('fx_bursts')
                     or any(uses_authored_decoration(style, kind) for kind in ('shadow', 'glow'))))


def reverse_singing_scale(line, style, *, allow_outline=False, allow_glow=False, allow_particles=False):
    return (line.get('wipe_reverse') and line.get('karaoke_anim') == 'utopia'
            and {line.get('entry_anim'), line.get('exit_anim')} == {'none'}
            and not (style.vertical or style.right_to_left or style.lower_ruby_enabled or line.get('stroke_flash')
                     or line.get('zoom_pulse') or uses_authored_decoration(style, 'shadow'))
            and (allow_particles or not line.get('fx_bursts'))
            and (allow_outline or not (style.stroke_width_px or (style.stroke2_enabled and style.stroke2_width_px)))
            and (allow_glow or not uses_authored_decoration(style, 'glow'))
            and (not line.get('scanline') or (style.scanline_shape == 'band'
                                            and style.scanline_mode in {'default', 'color'})))


def scanline_features(track, style, lines, particle_band_tracks, vertical_singing_tracks):
    """Validate scanline modes and their supported compositions."""
    composed = role_layers = line_motion = False
    unscaled_line_motion = faded_band_particles = False
    inline_role_fade = prefix_guide_fade = False
    static_cell_band = False
    transform_band = False
    utopia_entry_bands = False
    vertical_band = reverse_vertical_band = moving_vertical_band = vertical_singing_band = vertical_palette_band = False
    reverse_vertical_palette = False
    singing_vertical_palette = False
    vertical_zoom_band = False
    reverse_vertical_singing = False
    moving_vertical_singing = False
    shapes = set()
    moving_shapes = shaped_singing = particle_scanline_image = lower_readings = moving_lower_readings = False
    lower_singing_particles = False
    role_gradient = False
    role_line_motion = False
    role_scanline_particles = False
    zoom_shapes = False
    role_singing_band = False
    shaped_palette = False
    rtl_band = False
    reverse_image = original_image_palette = reverse_procedural_shape = False
    shadow_band = False
    for index, (source, line) in enumerate(zip(track.lines, lines, strict=True)):
        if not line.get('scanline'):
            continue
        look = style_for_line(style, source)
        ordinary = {line.get('entry_anim'), line.get('exit_anim')} == {'none'}
        particle_entry = index in particle_band_tracks
        bursts = line.get('fx_bursts', ())
        if look.vertical:
            motion = not ordinary and ordinary_line_animation(line) and not source.wipe_reverse
            singing = index in vertical_singing_tracks
            palette = (look.scanline_mode in {'brighten', 'follow_before', 'follow_after'}
                       and (ordinary or (motion and singing)))
            if (look.scanline_shape != 'band' or (look.scanline_mode != 'color' and not palette) or not (ordinary or motion)
                    or (line.get('karaoke_anim') != 'none' and not singing) or look.lower_ruby_enabled
                    or line.get('stroke_flash') or (bursts and not vertical_singing_particle_modes(line))):
                raise ValueError('Skia 竖排扫字线的布局或高亮组合暂不支持')
            vertical_band = True
            reverse_vertical_band |= source.wipe_reverse
            moving_vertical_band |= motion and not singing
            vertical_singing_band |= singing
            moving_vertical_singing |= motion and singing
            reverse_vertical_singing |= singing and source.wipe_reverse
            vertical_zoom_band |= singing and bool(line.get('zoom_pulse'))
            vertical_palette_band |= palette
            reverse_vertical_palette |= palette and source.wipe_reverse
            singing_vertical_palette |= palette and singing
            continue
        role_fill = scanline_role_fill(look)
        role_motion = (look.scanline_mode == 'role' and look.scanline_shape == 'band'
                       and (role_fill is None or role_fill.mode == 'solid')
                       and line.get('karaoke_anim') == 'none' and not look.lower_ruby_enabled)
        palette_shape = False
        procedural = (ordinary and look.scanline_shape in {'flame', 'bubble'} and not (
            look.lower_ruby_enabled or bursts or line.get('karaoke_anim') != 'none'
            or line.get('zoom_pulse') or line.get('stroke_flash')))
        reverse_procedural = procedural and source.wipe_reverse and not (look.right_to_left or track.rubies)
        if procedural and (not source.wipe_reverse or reverse_procedural):
            colors = effective_karaoke_colors(look)
            palette_shape = (look.scanline_mode == 'default'
                             or (look.scanline_mode == 'role' and not track.rubies
                                 and role_fill is not None and role_fill.mode == 'solid')
                             or (look.scanline_mode in {'follow_before', 'follow_after', 'brighten'}
                                 and colors.before.text.mode == colors.after.text.mode == 'solid'))
        shape_style = (palette_shape or (look.scanline_shape in {'flame', 'bubble', 'image'}
                                         and look.scanline_mode == 'color'))
        basic_image = (look.scanline_shape == 'image' and ordinary and not (
            track.rubies or look.lower_ruby_enabled or bursts or look.right_to_left
            or line.get('karaoke_anim') != 'none' or line.get('zoom_pulse') or line.get('stroke_flash')))
        image_follows_palette = (basic_image and not look.scanline_image_color_layers
                                 and look.scanline_mode in {'default', 'brighten', 'follow_before', 'follow_after'})
        image_reverse = basic_image and source.wipe_reverse and (look.scanline_mode == 'color' or image_follows_palette)
        shape_style |= image_follows_palette
        image_during_transition = particle_entry and look.scanline_shape == 'image'
        shape_with_singing = (shape_style and (ordinary or image_during_transition) and bool(track.rubies) and bool(bursts)
                              and all(burst['kind'] != 'scanline' for burst in bursts))
        zoom_shape = (ordinary and look.scanline_shape == 'flame' and look.scanline_mode == 'color'
                      and line.get('karaoke_anim') == 'utopia' and line.get('zoom_pulse')
                      and not (look.right_to_left or source.wipe_reverse or look.lower_ruby_enabled or bursts))
        solid_shape = (shape_style and (not source.wipe_reverse or image_reverse or reverse_procedural)
                       and (not line.get('zoom_pulse') or zoom_shape) and not line.get('stroke_flash')
                       and (not bursts or shape_with_singing))
        faded_band = faded_band_singing_particles(line, look)
        motion = (not ordinary and ordinary_line_animation(line)
                  and (look.scanline_shape == 'band' or solid_shape) and (look.scanline_mode != 'role' or role_motion)
                  and line.get('karaoke_anim') in {'none', 'utopia'}
                  and not source.wipe_reverse
                  and not (line.get('zoom_pulse') or line.get('stroke_flash'))
                  and (not bursts or faded_band))
        shape = solid_shape and (ordinary or motion or image_during_transition)
        entering_utopia = not track.rubies and utopia_entry_band(line, look)
        utopia_entry_bands |= entering_utopia
        reverse_singing = not track.rubies and reverse_singing_scale(line, look)
        utopia = (line.get('karaoke_anim') == 'utopia'
                  and (reverse_singing or not source.wipe_reverse and (ordinary or motion or particle_entry or entering_utopia)))
        with_zoom_or_flash = line.get('zoom_pulse') or line.get('stroke_flash')
        lower_particles = faded_band and look.scanline_mode in {'default', 'color'}
        lower_band = (look.lower_ruby_enabled and (ordinary or motion) and look.scanline_shape == 'band'
                      and look.scanline_mode != 'role' and not (source.wipe_reverse or with_zoom_or_flash)
                      and (not bursts or lower_particles))
        role_composition = look.scanline_mode == 'role' and (bool(track.rubies) or uses_authored_decoration(look, 'glow'))
        role_particles = (look.scanline_mode == 'role' and look.scanline_shape == 'band'
                          and bool(track.rubies) and ordinary and bool(bursts)
                          and line.get('karaoke_anim') == 'none' and not look.right_to_left and not source.wipe_reverse
                          and not (line.get('zoom_pulse') or line.get('stroke_flash') or look.lower_ruby_enabled)
                          and (role_fill is None or role_fill.mode == 'solid')
                          and all(burst['kind'] == 'scanline' for burst in bursts))
        chars = render_line_with_guide_symbols(source).chars
        inline_roles = any(c.role_label is not None for c in chars)
        vector_guides = any(c.vector_glyph is not None for c in chars)
        transform_band |= (transformed_cell_band(line, look)
                           and not (track.rubies or inline_roles or vector_guides))
        rtl_scanline = (look.right_to_left and ordinary and look.scanline_shape == 'band'
                        and look.scanline_mode in {'color', 'default', 'brighten', 'follow_before', 'follow_after'}
                        and line.get('karaoke_anim') == 'none' and not track.rubies and not look.lower_ruby_enabled
                        and not (inline_roles or vector_guides or bursts or with_zoom_or_flash
                                 or uses_authored_decoration(look, 'glow')))
        fixed_fade = fixed_faded_utopia_band(line, look)
        role_fade = inline_roles and fixed_fade
        prefix_fade = (vector_guides and not inline_roles and vector_guides_form_prefix(chars)
                       and (fixed_fade or (faded_band and look.scanline_mode in {'default', 'color'})))
        static_cells = ((inline_roles or vector_guides) and ordinary and look.scanline_shape == 'band'
                        and look.scanline_mode != 'role' and line.get('karaoke_anim') == 'none'
                        and not (look.right_to_left or source.wipe_reverse or look.lower_ruby_enabled
                                 or track.rubies or with_zoom_or_flash or bursts))
        body_shadow = uses_authored_decoration(look, 'shadow')
        role_scaled = (look.scanline_mode == 'role' and look.scanline_shape == 'band' and utopia and ordinary
                       and (role_fill is None or role_fill.mode == 'solid')
                       and not (look.right_to_left or look.lower_ruby_enabled or inline_roles or vector_guides
                                or line.get('stroke_flash') or bursts or body_shadow
                                or uses_authored_decoration(look, 'glow')))
        static_shadow_band = (body_shadow and ordinary and look.scanline_shape == 'band'
                              and look.scanline_mode in {'default', 'color'} and line.get('karaoke_anim') == 'none'
                              and not (look.right_to_left or source.wipe_reverse or look.lower_ruby_enabled
                                       or inline_roles or vector_guides or with_zoom_or_flash or bursts
                                       or uses_authored_decoration(look, 'glow')))
        if ((look.scanline_shape != 'band' and not shape) or (look.right_to_left and not rtl_scanline)
                or (line.get('karaoke_anim') != 'none' and not utopia)
                or (with_zoom_or_flash and (not ordinary or source.wipe_reverse
                                            or (look.scanline_mode == 'role' and not role_scaled)))
                or (look.lower_ruby_enabled and not lower_band)
                or (body_shadow and not static_shadow_band)
                or (role_composition and not role_scaled
                    and (not (ordinary or (motion and role_motion)) or utopia or source.wipe_reverse
                         or (bursts and not role_particles)))
                or (track.rubies and not (ordinary or motion or particle_entry))
                or (inline_roles and not (role_fade or static_cells))
                or (vector_guides and not (prefix_fade or static_cells))):
            raise ValueError('Skia 暂不支持此扫字线与排版、画笔或动画的组合')
        rtl_band |= rtl_scanline
        shadow_band |= static_shadow_band
        static_cell_band |= static_cells
        role_gradient |= (role_fill is not None
                          and role_fill.mode in {'gradient_horizontal', 'gradient_vertical', 'split_vertical'}
                          and ordinary and not (track.rubies or bursts or with_zoom_or_flash or utopia or inline_roles))
        composed = composed or with_zoom_or_flash
        role_layers = role_layers or role_composition
        role_line_motion |= motion and role_motion
        role_scanline_particles |= role_particles
        role_singing_band |= role_scaled
        line_motion |= motion and utopia
        faded_band_particles |= faded_band
        inline_role_fade |= role_fade
        prefix_guide_fade |= prefix_fade
        unscaled_line_motion |= motion and not utopia
        lower_readings = lower_readings or lower_band
        moving_lower_readings |= lower_band and motion
        lower_singing_particles |= lower_band and lower_particles
        if shape:
            shapes.add(look.scanline_shape)
            zoom_shapes |= zoom_shape
            shaped_palette |= palette_shape
            reverse_image |= image_reverse
            reverse_procedural_shape |= reverse_procedural
            original_image_palette |= image_follows_palette
            particle_scanline_image |= image_during_transition
            moving_shapes = moving_shapes or motion
            shaped_singing = shaped_singing or shape_with_singing
    features = []
    if role_gradient:
        features.append('p07_scanline_role_gradient')
    if shaped_palette:
        features.append('p07_scanline_shape_palette')
    if rtl_band:
        features.append('p07_rtl_scanline_band')
    if shadow_band:
        features.append('p07_scanline_shadow')
    if reverse_image:
        features.append('p07_reverse_scanline_image')
    if reverse_procedural_shape:
        features.append('p07_reverse_procedural_scanline')
    if original_image_palette:
        features.append('p07_original_scanline_image_palette')
    if inline_role_fade:
        features.append('p07_inline_role_faded_scanline')
    if static_cell_band:
        features.append('p07_scanline_static_cells')
    if transform_band:
        features.append('p07_scanline_character_transform')
    if utopia_entry_bands:
        features.append('p07_scanline_utopia_entry')
    if prefix_guide_fade:
        features.append('p07_faded_scanline_prefix_guides')
    if vertical_band:
        features.append('p07_vertical_scanline_band')
    if reverse_vertical_band:
        features.append('p07_vertical_reverse_scanline')
        if track.rubies:
            features.append('p07_vertical_mixed_scanline')
    if moving_vertical_band:
        features.append('p07_vertical_scanline_line_animation')
    if vertical_singing_band:
        features.append('p07_vertical_scanline_singing')
    if reverse_vertical_singing:
        features.append('p07_vertical_reverse_scanline_singing')
    if moving_vertical_singing:
        features.append('p07_vertical_singing_scanline_line_animation')
    if vertical_zoom_band:
        features.append('p07_vertical_zoom_scanline')
    if vertical_palette_band:
        features.append('p07_vertical_scanline_palette')
    if reverse_vertical_palette:
        features.append('p07_vertical_reverse_scanline_palette')
    if singing_vertical_palette:
        features.append('p07_vertical_singing_scanline_palette')
    if shapes:
        features.append('p07_scanline_shape')
    if zoom_shapes:
        features.append('p07_zoom_scanline_shape')
    if 'image' in shapes:
        features.append('p07_scanline_image')
    if particle_scanline_image:
        features.append('p07_ruby_petal_scanline_image')
    if composed:
        features.append('p07_composed_scanline')
    if line_motion:
        features.append('p07_scanline_line_animation')
    if faded_band_particles:
        features.append('p07_faded_scanline_singing_particles')
    if unscaled_line_motion or moving_vertical_band:
        features.append('p07_scanline_unscaled_line_animation')
    if moving_lower_readings:
        features.append('p07_lower_ruby_scanline_line_animation')
    if lower_singing_particles:
        features.append('p07_lower_ruby_scanline_singing_particles')
    if particle_band_tracks:
        features.append('p07_ruby_petal_entry_scanline')
    if moving_shapes:
        features.append('p07_scanline_shape_line_animation')
    if shaped_singing:
        features.append('p07_shaped_scanline_singing_particles')
    if lower_readings:
        features.append('p07_lower_ruby_scanline')
    if role_layers:
        features.append('p07_role_scanline_layers')
    if role_line_motion:
        features.append('p07_role_scanline_line_animation')
    if role_scanline_particles:
        features.append('p07_role_scanline_particles')
    if role_singing_band:
        features.append('p07_role_scanline_singing')
    features.append('p07_scanline_band')
    return features


def write_scanline_schema(ir, track, style):
    if 'p07_scanline_band' in ir['required_features']:
        scanned = [line for line in ir['track']['lines'] if line.get('scanline')]
        glowing = any(source.get('scanline') and uses_authored_decoration(style_for_line(style, line), 'glow')
                      for source, line in zip(ir['track']['lines'], track.lines, strict=True))
        if 'p07_scanline_shape' in ir['required_features']:
            ir['skia_layout']['scanline_shape_schema'] = 1
        ir['skia_layout']['scanline_band_schema'] = (
            6 if glowing
            else 5 if any(line.get('karaoke_anim') == 'utopia' for line in scanned)
            else 4 if any(line.get('wipe_reverse') for line in scanned) else 3
        )


def declare_vertical_scanline_viewport(ir):
    features = set(ir['required_features'])
    if 'p07_vertical_scanline_band' in features and not features.intersection({
            'p07_vertical_reverse_scanline', 'p07_vertical_scanline_line_animation',
            'p07_vertical_singing_scanline_line_animation'}):
        ir['required_features'].append('p08_vertical_scanline_viewport')
        if 'p07_vertical_scanline_palette' in features:
            ir['required_features'].append('p08_vertical_scanline_palette_viewport')
        if 'p07_vertical_scanline_singing' in features:
            ir['required_features'].append('p08_vertical_singing_scanline_viewport')
