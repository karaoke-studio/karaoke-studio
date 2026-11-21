"""Resolve Skia scanline capabilities and the serialized shape and band versions."""

from ..guide.semantics import render_line_with_guide_symbols
from ..layout.line.style import style_for_line
from ..style.style_semantics import uses_authored_decoration
from .skia_line_animation_ir import ordinary_line_animation


def scanline_features(track, style, lines, particle_band_tracks, vertical_singing_tracks):
    """Validate scanline modes and their supported compositions."""
    composed = role_layers = line_motion = False
    unscaled_line_motion = False
    vertical_band = reverse_vertical_band = moving_vertical_band = vertical_singing_band = vertical_palette_band = False
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
                       and ordinary and not source.wipe_reverse and not singing)
            if (look.scanline_shape != 'band' or (look.scanline_mode != 'color' and not palette) or not (ordinary or motion)
                    or (line.get('karaoke_anim') != 'none' and not singing) or look.lower_ruby_enabled
                    or line.get('zoom_pulse') or line.get('stroke_flash') or bursts):
                raise ValueError('Skia 竖排扫字线当前仅支持普通走字或正向 Utopia 的实色高亮带')
            vertical_band = True
            reverse_vertical_band |= source.wipe_reverse
            moving_vertical_band |= motion
            vertical_singing_band |= singing
            vertical_palette_band |= palette
            continue
        shape_style = look.scanline_shape in {'flame', 'bubble', 'image'} and look.scanline_mode == 'color'
        image_during_transition = particle_entry and look.scanline_shape == 'image'
        shape_with_singing = (shape_style and (ordinary or image_during_transition) and bool(track.rubies) and bool(bursts)
                              and all(burst['kind'] != 'scanline' for burst in bursts))
        solid_shape = (shape_style and not (source.wipe_reverse or line.get('zoom_pulse') or line.get('stroke_flash'))
                       and (not bursts or shape_with_singing))
        motion = (not ordinary and ordinary_line_animation(line)
                  and (look.scanline_shape == 'band' or solid_shape) and look.scanline_mode != 'role'
                  and line.get('karaoke_anim') in {'none', 'utopia'}
                  and not source.wipe_reverse
                  and not (line.get('zoom_pulse') or line.get('stroke_flash') or line.get('fx_bursts')))
        shape = solid_shape and (ordinary or motion or image_during_transition)
        utopia = (line.get('karaoke_anim') == 'utopia' and not source.wipe_reverse
                  and (ordinary or motion or particle_entry))
        with_zoom_or_flash = line.get('zoom_pulse') or line.get('stroke_flash')
        lower_band = (look.lower_ruby_enabled and (ordinary or motion) and look.scanline_shape == 'band'
                      and look.scanline_mode != 'role' and not (source.wipe_reverse or with_zoom_or_flash or bursts))
        role_composition = look.scanline_mode == 'role' and (bool(track.rubies) or uses_authored_decoration(look, 'glow'))
        if ((look.scanline_shape != 'band' and not shape) or look.right_to_left
                or (line.get('karaoke_anim') != 'none' and not utopia)
                or (with_zoom_or_flash and (not ordinary or source.wipe_reverse or look.scanline_mode == 'role'))
                or (look.lower_ruby_enabled and not lower_band)
                or uses_authored_decoration(look, 'shadow')
                or (role_composition and (not ordinary or utopia or source.wipe_reverse or line.get('fx_bursts')))
                or (track.rubies and not (ordinary or motion or particle_entry))
                or any(c.role_label is not None or c.vector_glyph is not None
                       for c in render_line_with_guide_symbols(source).chars)):
            raise ValueError('Skia 扫字线当前仅支持横排实色高亮带')
        composed = composed or with_zoom_or_flash
        role_layers = role_layers or role_composition
        line_motion |= motion and utopia
        unscaled_line_motion |= motion and not utopia
        lower_readings = lower_readings or lower_band
        moving_lower_readings |= lower_band and motion
        if shape:
            shapes.add(look.scanline_shape)
            particle_scanline_image |= image_during_transition
            moving_shapes = moving_shapes or motion
            shaped_singing = shaped_singing or shape_with_singing
    features = []
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
    if vertical_palette_band:
        features.append('p07_vertical_scanline_palette')
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
