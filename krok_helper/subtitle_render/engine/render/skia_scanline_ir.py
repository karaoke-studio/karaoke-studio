"""Resolve Skia scanline capabilities and the serialized shape and band versions."""

from ..guide.semantics import render_line_with_guide_symbols
from ..layout.line.style import style_for_line
from ..style.style_semantics import uses_authored_decoration
from .skia_line_animation_ir import ordinary_line_animation
from .skia_particle_ir import faded_band_singing_particles, vertical_singing_particle_modes


def inline_role_faded_band(line, style):
    animations = {line.get('entry_anim'), line.get('exit_anim')}
    return (line.get('scanline') and style.scanline_shape == 'band'
            and style.scanline_mode in {'default', 'color'} and line.get('karaoke_anim') == 'utopia'
            and 'fade' in animations and animations <= {'none', 'fade'}
            and not (style.vertical or style.right_to_left or style.lower_ruby_enabled or line.get('wipe_reverse')
                     or line.get('stroke_flash') or line.get('zoom_pulse') or line.get('fx_bursts')))


def scanline_features(track, style, lines, particle_band_tracks, vertical_singing_tracks):
    """Validate scanline modes and their supported compositions."""
    composed = role_layers = line_motion = False
    unscaled_line_motion = faded_band_particles = False
    inline_role_fade = False
    vertical_band = reverse_vertical_band = moving_vertical_band = vertical_singing_band = vertical_palette_band = False
    reverse_vertical_palette = False
    singing_vertical_palette = False
    vertical_zoom_band = False
    reverse_vertical_singing = False
    moving_vertical_singing = False
    shapes = set()
    moving_shapes = shaped_singing = particle_scanline_image = lower_readings = moving_lower_readings = False
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
        shape_style = look.scanline_shape in {'flame', 'bubble', 'image'} and look.scanline_mode == 'color'
        image_during_transition = particle_entry and look.scanline_shape == 'image'
        shape_with_singing = (shape_style and (ordinary or image_during_transition) and bool(track.rubies) and bool(bursts)
                              and all(burst['kind'] != 'scanline' for burst in bursts))
        solid_shape = (shape_style and not (source.wipe_reverse or line.get('zoom_pulse') or line.get('stroke_flash'))
                       and (not bursts or shape_with_singing))
        faded_band = faded_band_singing_particles(line, look)
        motion = (not ordinary and ordinary_line_animation(line)
                  and (look.scanline_shape == 'band' or solid_shape) and look.scanline_mode != 'role'
                  and line.get('karaoke_anim') in {'none', 'utopia'}
                  and not source.wipe_reverse
                  and not (line.get('zoom_pulse') or line.get('stroke_flash'))
                  and (not bursts or faded_band))
        shape = solid_shape and (ordinary or motion or image_during_transition)
        utopia = (line.get('karaoke_anim') == 'utopia' and not source.wipe_reverse
                  and (ordinary or motion or particle_entry))
        with_zoom_or_flash = line.get('zoom_pulse') or line.get('stroke_flash')
        lower_band = (look.lower_ruby_enabled and (ordinary or motion) and look.scanline_shape == 'band'
                      and look.scanline_mode != 'role' and not (source.wipe_reverse or with_zoom_or_flash or bursts))
        role_composition = look.scanline_mode == 'role' and (bool(track.rubies) or uses_authored_decoration(look, 'glow'))
        chars = render_line_with_guide_symbols(source).chars
        inline_roles = any(c.role_label is not None for c in chars)
        role_fade = inline_roles and inline_role_faded_band(line, look)
        if ((look.scanline_shape != 'band' and not shape) or look.right_to_left
                or (line.get('karaoke_anim') != 'none' and not utopia)
                or (with_zoom_or_flash and (not ordinary or source.wipe_reverse or look.scanline_mode == 'role'))
                or (look.lower_ruby_enabled and not lower_band)
                or uses_authored_decoration(look, 'shadow')
                or (role_composition and (not ordinary or utopia or source.wipe_reverse or line.get('fx_bursts')))
                or (track.rubies and not (ordinary or motion or particle_entry))
                or (inline_roles and not role_fade) or any(c.vector_glyph is not None for c in chars)):
            raise ValueError('Skia 暂不支持此扫字线与排版、画笔或动画的组合')
        composed = composed or with_zoom_or_flash
        role_layers = role_layers or role_composition
        line_motion |= motion and utopia
        faded_band_particles |= faded_band
        inline_role_fade |= role_fade
        unscaled_line_motion |= motion and not utopia
        lower_readings = lower_readings or lower_band
        moving_lower_readings |= lower_band and motion
        if shape:
            shapes.add(look.scanline_shape)
            particle_scanline_image |= image_during_transition
            moving_shapes = moving_shapes or motion
            shaped_singing = shaped_singing or shape_with_singing
    features = []
    if inline_role_fade:
        features.append('p07_inline_role_faded_scanline')
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
