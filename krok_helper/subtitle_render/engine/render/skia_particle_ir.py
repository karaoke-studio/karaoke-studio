"""Canonical anchors and capabilities for the singing-particle planner."""
from ..guide.semantics import guide_symbol_is_bitmap
from ..layout.line.skia_geometry import write_horizontal_pivots
from ..style.style_semantics import uses_authored_decoration
from .skia_line_animation_ir import ordinary_line_animation


FALLING_PARTICLES = frozenset({'petal', 'feather', 'snow', 'snow_solid'})
ENERGY_TRANSITIONS = frozenset({'flame', 'lightning'})
PARTICLE_TRANSITIONS = {
    **{kind: kind for kind in FALLING_PARTICLES | ENERGY_TRANSITIONS | {'note', 'heart', 'ripple', 'sparkle'}},
    'assemble_in': 'assemble',
    'dissolve_out': 'dissolve',
}
RUBY_PARTICLE_EXITS = {'petal', 'feather', 'snow', 'snow_solid', 'note', 'heart', 'ripple', 'sparkle'}
RUBY_PARTICLE_ENTRIES = RUBY_PARTICLE_EXITS
RUBY_SYMBOL_ENTRIES = {'sparkle', 'ripple', 'note'}
RUBY_PAIRED_PARTICLES = {'petal', 'feather', 'heart'}
# Companion layers the existing planner emits around each energy transition:
# fire tongues, back glow, embers and the ignition impact; bolts with their back
# glow and the strike impact. Burning out also drops ash.
_IMPACT = {'flash', 'shockwave', 'spark'}
_TRANSITION_LAYERS = {
    ('flame', True): {'flame', 'flame_alt', 'flame_glow', 'ember', 'fireball'} | _IMPACT,
    ('flame', False): {'flame', 'flame_alt', 'flame_glow', 'ember', 'fireball', 'ash'} | _IMPACT,
    ('lightning', True): {'lightning', 'lightning_glow'} | _IMPACT,
    ('lightning', False): {'lightning', 'lightning_glow'} | _IMPACT,
}
_FLAME_SINGING_SWEEPS = {'flame': 2, 'flame_alt': 2, 'flame_glow': 0, 'smoke': 0, 'ember': 0, 'spark': 0}
_ENERGY_PARTICLES = {'lightning', 'lightning_glow'} | _FLAME_SINGING_SWEEPS.keys()
_BACK_PARTICLES = {'ripple', 'lightning_glow', 'flame_glow', 'smoke', 'shockwave'}
_SINGING_STARS = {('twinkle', 1), ('twinkle_classic', 0)}
_VERTICAL_SHAPES = {(kind, 0) for kind in ('note', 'heart', 'ripple')}
_VERTICAL_FALLING = {(kind, 0) for kind in FALLING_PARTICLES}
_VERTICAL_LIGHTNING = {(kind, 0) for kind in ('lightning', 'lightning_glow')}
_VERTICAL_FLAME = set(_FLAME_SINGING_SWEEPS.items())
_VERTICAL_SCANLINE = {('scanline', 0)}
_VERTICAL_PARTICLE_FEATURES = (
    (_SINGING_STARS, 'p07_vertical_sing_twinkle'),
    (_VERTICAL_SHAPES, 'p07_vertical_sing_shapes'),
    (_VERTICAL_FALLING, 'p07_vertical_sing_falling'),
    (_VERTICAL_LIGHTNING, 'p07_vertical_sing_lightning'),
    (_VERTICAL_FLAME, 'p07_vertical_sing_flame'),
    (_VERTICAL_SCANLINE, 'p07_vertical_scanline_particles'),
)
_VERTICAL_PARTICLE_MODES = set().union(*(modes for modes, _ in _VERTICAL_PARTICLE_FEATURES))
_PARTICLES = ({'twinkle', 'twinkle_classic', 'scanline', 'fireball', 'flash', 'shockwave', 'ash'}
              | set(PARTICLE_TRANSITIONS.values()) | _ENERGY_PARTICLES)
_PAINTED_SPRITES = ({'twinkle', 'twinkle_classic', 'note', 'heart', 'sparkle', 'assemble', 'dissolve',
                     'fireball', 'flash', 'ash'} | FALLING_PARTICLES)
_SPRITE_PAINT_MODES = {'solid', 'gradient_vertical'}
_LOCAL_SPRITE_PAINT_MODES = _SPRITE_PAINT_MODES | {'gradient_horizontal', 'split_vertical', 'image'}
_RIPPLE_PAINT_MODES = _SPRITE_PAINT_MODES | {'gradient_horizontal', 'split_vertical'}


def _transition_layers(kind, entry):
    return _TRANSITION_LAYERS.get((kind, entry), {kind})


def _utopia_color_scanline(line, style):
    return bool(line.get('scanline')) and line.get('karaoke_anim') == 'utopia' and style.scanline_mode == 'color'


def petal_scanline_with_note_exit(line, style):
    return (line.get('entry_anim') == 'petal' and line.get('exit_anim') == 'note' and style.sing_fx == 'flame'
            and line['entry_duration_ms'] > 0 and line['exit_duration_ms'] > 0
            and style.scanline_shape in {'band', 'image'} and _utopia_color_scanline(line, style))


def petal_entry_with_flame(line, style):
    return (line.get('entry_anim') == 'petal' and style.sing_fx == 'flame'
            and (line.get('exit_anim') == 'none' or petal_scanline_with_note_exit(line, style)))


def petal_entry_with_scanline(line, style):
    return petal_scanline_with_note_exit(line, style) or (
        petal_entry_with_flame(line, style) and style.scanline_shape == 'band' and _utopia_color_scanline(line, style))


def faded_band_singing_particles(line, style):
    """Forward horizontal Utopia bands whose singing particles share a whole-line fade."""
    animations = {line.get('entry_anim'), line.get('exit_anim')}
    bursts = line.get('fx_bursts', ())
    return (bool(bursts) and 'fade' in animations and animations <= {'none', 'fade'}
            and line.get('scanline') and line.get('karaoke_anim') == 'utopia'
            and style.scanline_shape == 'band' and style.scanline_mode != 'role'
            and not (style.vertical or style.right_to_left or line.get('wipe_reverse')
                     or line.get('zoom_pulse') or line.get('stroke_flash'))
            and all(burst['kind'] in {'twinkle', 'twinkle_classic', 'note', 'heart', 'snow_solid'} for burst in bursts))


def _sprite_layer_modes(paint):
    return {paint['fill']['mode']} | {paint[layer]['mode'] for layer in ('stroke', 'stroke2')
                                    if paint[f'{layer}_width_px']}


def _supports_singing_particle_paint(paint, *, local_sprite=False):
    decor = paint.get('decor')
    modes = _LOCAL_SPRITE_PAINT_MODES if local_sprite else _SPRITE_PAINT_MODES
    if not _sprite_layer_modes(paint) <= modes or (
            not local_sprite and (paint['stroke_width_px'] or paint['stroke2_width_px'])):
        return False
    kinds = {'glow', 'shadow'} if local_sprite else {'glow'}
    # A sprite's decoration samples its line span like its other layers.
    decor_modes = _LOCAL_SPRITE_PAINT_MODES if local_sprite else _SPRITE_PAINT_MODES
    return decor is None or (decor['kind'] in kinds and decor['fill']['mode'] in decor_modes)


def vertical_singing_particle_modes(source):
    """Return supported particle kinds and sweeps for a fixed vertical cell anchor."""
    limited_motion = source.get('wipe_reverse') and (
        source.get('entry_anim'), source.get('exit_anim')) != ('none', 'none')
    if (source.get('stroke_flash') or limited_motion
            or source.get('karaoke_anim') not in {'none', 'no_wipe', 'utopia'}
            or (source.get('karaoke_anim') == 'no_wipe' and source.get('scanline'))
            or not ordinary_line_animation(source)):
        return set()
    modes = {(burst['kind'], burst['sweep']) for burst in source.get('fx_bursts', ())}
    if not source.get('scanline') and modes & _VERTICAL_SCANLINE:
        return set()
    return modes if modes <= _VERTICAL_PARTICLE_MODES else set()


def write_particle_anchors(ir, plan, measured, composed_lines=()) -> int:
    """Bind solid particles to final cells and return the required profile version."""
    version = 3
    ruby_particles = ruby_scanline = ruby_band_singing = painted_singing = False
    painted_sprites = sprite_shadows = sprite_line_gradients = sprite_images = False
    painted_ripples = False
    painted_energy = False
    sprite_split_paints = False
    vertical_modes_used = set()
    vertical_scaled_particles = False
    reverse_vertical_particles = False
    motion_vertical_particles = False
    band_vertical_particles = False
    reverse_vertical_band_particles = False
    moving_vertical_band_particles = False
    outlined_vertical_particles = False
    no_wipe_vertical_particles = False
    reverse_horizontal_singing = False
    rtl_horizontal_singing = False
    no_wipe_horizontal_singing = False
    horizontal_line_motion = False
    plain_scaled_singing = False
    plain_zoom_singing = False
    horizontal_outlined_singing = False
    unscaled_outlined_singing = False
    energy_line_motion = energy_singing_stars = False
    for index, (final, item) in enumerate(zip(ir['skia_layout']['lines'], plan.lines, strict=True)):
        source = ir['track']['lines'][item.track_index]
        bursts = source.get('fx_bursts', ())
        if not bursts:
            continue
        look = item.layout_style
        band = bool(source.get('scanline'))
        entry, exit_ = source.get('entry_anim'), source.get('exit_anim')
        entry_kind, exit_kind = PARTICLE_TRANSITIONS.get(entry), PARTICLE_TRANSITIONS.get(exit_)
        animated = bool(entry_kind or exit_kind)
        readings = final.get('ruby_groups', ())
        annotated = bool(readings)
        ruby_particles |= annotated
        has_lower = any(group.get('lower') for group in readings)
        supported_band = not band or (look.scanline_shape == 'band' and look.scanline_mode != 'role')
        reverse_singing = (item.line.wipe_reverse and not (look.vertical or look.right_to_left or annotated)
                           and supported_band and (entry, exit_) == ('none', 'none')
                           and not (source.get('stroke_flash') or source.get('zoom_pulse'))
                           and any(burst['kind'] != 'scanline' for burst in bursts))
        no_wipe_singing = (source.get('karaoke_anim') == 'no_wipe' and not
                          (look.vertical or look.right_to_left or item.line.wipe_reverse or annotated or band)
                          and (entry, exit_) == ('none', 'none')
                          and not (source.get('stroke_flash') or source.get('zoom_pulse')))
        vertical_modes = vertical_singing_particle_modes(source) if look.vertical and supported_band else set()
        vertical_particles = bool(vertical_modes)
        if look.vertical and not vertical_particles:
            # Direct2D draws no particles on vertical lines; drop the ones the
            # vertical profile cannot anchor instead of rejecting the scene.
            source['fx_bursts'] = []
            continue
        outlined = look.stroke_width_px > 0 or (look.stroke2_enabled and look.stroke2_width_px > 0)
        rtl_singing = (look.right_to_left and not (look.vertical or item.line.wipe_reverse or annotated or band or outlined)
                       and source.get('karaoke_anim') == 'none' and (entry, exit_) == ('none', 'none')
                       and not (source.get('stroke_flash') or source.get('zoom_pulse'))
                       and not any(uses_authored_decoration(look, kind) for kind in ('shadow', 'glow')))
        if vertical_particles:
            vertical_modes_used.update(vertical_modes)
            vertical_scaled_particles |= source.get('karaoke_anim') == 'utopia'
            reverse_vertical_particles |= item.line.wipe_reverse
            motion_vertical_particles |= (entry, exit_) != ('none', 'none')
            band_vertical_particles |= band and bool(vertical_modes - _VERTICAL_SCANLINE)
            reverse_vertical_band_particles |= band and item.line.wipe_reverse
            moving_vertical_band_particles |= band and (entry, exit_) != ('none', 'none')
            outlined_vertical_particles |= outlined and not annotated
            no_wipe_vertical_particles |= source.get('karaoke_anim') == 'no_wipe'
        petal_with_flame = (annotated and petal_entry_with_flame(source, look)
                           and 'p07_ruby_petal_entry_flame_singing' in ir['required_features'])
        petal_with_band = (petal_with_flame and petal_entry_with_scanline(source, look)
                           and 'p07_ruby_petal_entry_scanline' in ir['required_features'])
        faded_band = faded_band_singing_particles(source, look)
        chars = item.render_line.chars
        faded_vector_guides = (faded_band and 'p07_faded_scanline_prefix_guides' in ir['required_features']
                               and any(c.vector_glyph is not None and not guide_symbol_is_bitmap(c.vector_glyph)
                                       for c in chars))
        singing_under_shape = (annotated and band and (not animated or petal_with_band) and look.scanline_shape != 'band'
                               and 'p07_shaped_scanline_singing_particles' in ir['required_features'])
        singing_under_band = (not look.vertical and annotated and band
                              and (not animated or petal_with_band) and look.scanline_shape == 'band'
                              and look.scanline_mode != 'role' and not item.line.wipe_reverse
                              and any(burst['kind'] != 'scanline' for burst in bursts))
        ruby_band_singing = ruby_band_singing or singing_under_band
        unannotated_scale = (source.get('karaoke_anim') == 'utopia' and (entry, exit_) == ('none', 'none')
                             and not (annotated or look.vertical or look.right_to_left
                                      or band or source.get('stroke_flash')
                                      or (item.line.wipe_reverse and source.get('zoom_pulse'))
                                      or any(uses_authored_decoration(look, kind) for kind in ('shadow', 'glow'))))
        outlined_singing = unannotated_scale and outlined and not (item.line.wipe_reverse or source.get('zoom_pulse'))
        plain_scale = unannotated_scale and (not outlined or outlined_singing)
        static_sprite_paint = (not (look.vertical or look.right_to_left or item.line.wipe_reverse or band or annotated)
                               and (entry, exit_) == ('none', 'none') and source.get('karaoke_anim') == 'none'
                               and not (source.get('zoom_pulse') or source.get('stroke_flash')))
        unscaled_outline = static_sprite_paint and outlined and not any(
            uses_authored_decoration(look, kind) for kind in ('shadow', 'glow'))
        transition_singing = (not annotated and index in composed_lines and source.get('karaoke_anim') == 'utopia'
                              and not source.get('zoom_pulse'))
        scaled_singing = (faded_band or plain_scale or transition_singing
                          or (annotated and source.get('karaoke_anim') == 'utopia' and not source.get('zoom_pulse')))
        unscaled_motion = (not look.vertical and not look.right_to_left and not item.line.wipe_reverse
                           and not band and not animated and not annotated and not outlined
                           and source.get('karaoke_anim') == 'none' and not source.get('stroke_flash')
                           and (entry, exit_) != ('none', 'none') and ordinary_line_animation(source))
        outlined_energy = ('p07_energy_transition_outlines' in ir['required_features']
                           and bool({entry, exit_} & ENERGY_TRANSITIONS)
                           and {entry, exit_} <= ENERGY_TRANSITIONS | {'none'})
        energy = (not annotated and not band and not look.vertical and not look.right_to_left
                  and not item.line.wipe_reverse and source.get('karaoke_anim') == 'none'
                  and bool({entry, exit_} & ENERGY_TRANSITIONS))
        energy_motion = (energy and not outlined
                         and ((entry in ENERGY_TRANSITIONS and exit_ in {'fade', 'slide_out', 'rise'})
                              or (exit_ in ENERGY_TRANSITIONS and entry in {'fade', 'slide_in', 'rise'})))
        energy_stars = (energy and look.sing_fx in {'twinkle', 'twinkle_classic'}
                        and ({entry, exit_} <= ENERGY_TRANSITIONS | {'none'} or energy_motion))
        entry_sweep = 1 if entry_kind in FALLING_PARTICLES else 0
        exit_sweep = -1 if exit_kind in FALLING_PARTICLES else 0
        horizontal_line_motion |= unscaled_motion
        plain_scaled_singing |= plain_scale
        plain_zoom_singing |= plain_scale and bool(source.get('zoom_pulse'))
        horizontal_outlined_singing |= outlined_singing
        unscaled_outlined_singing |= unscaled_outline
        reverse_horizontal_singing |= reverse_singing
        rtl_horizontal_singing |= rtl_singing
        no_wipe_horizontal_singing |= no_wipe_singing
        energy_line_motion |= energy_motion
        energy_singing_stars |= energy_stars
        for burst in bursts:
            kind = burst['kind']
            has_paint = 'paint_id' in burst
            if has_paint:
                paint = ir['fx_paint_table'][burst['paint_id']]
                if faded_band and _supports_singing_particle_paint(paint):
                    painted_singing = True
                # Particle paints attach to each burst whatever the line does,
                # as in Direct2D.
                elif (kind == 'ripple' and paint['fill']['mode'] in _RIPPLE_PAINT_MODES
                      and not (paint['stroke_width_px'] or paint['stroke2_width_px'] or paint.get('decor'))):
                    painted_sprites = painted_ripples = True
                    sprite_line_gradients |= paint['fill']['mode'] == 'gradient_horizontal'
                elif kind in (_PAINTED_SPRITES | _ENERGY_PARTICLES) and _supports_singing_particle_paint(
                        paint, local_sprite=True):
                    painted_sprites = True
                    painted_energy |= kind in _ENERGY_PARTICLES
                    decor = paint.get('decor')
                    modes = _sprite_layer_modes(paint)
                    sprite_shadows |= decor is not None and decor['kind'] == 'shadow'
                    sprite_line_gradients |= 'gradient_horizontal' in modes or (
                        decor is not None and decor['fill']['mode'] == 'gradient_horizontal')
                    sprite_images |= 'image' in modes or (decor is not None and decor['fill']['mode'] == 'image')
                    sprite_split_paints |= 'split_vertical' in modes or (
                        decor is not None and decor['fill']['mode'] == 'split_vertical')
                else:
                    raise ValueError('Skia 暂不支持此唱字粒子画笔或装饰')
            if (kind not in _PARTICLES
                    or (kind == 'scanline' and not (band or no_wipe_singing))
                    or burst.get('front', True) != (kind not in _BACK_PARTICLES)
                    or 'char_color_ids' in burst):
                raise ValueError('Skia 暂不支持此粒子类型、配色或扫字线组合')
            if animated:
                sweep = burst['sweep']
            elif kind in {'fireball', 'flash', 'shockwave', 'ash'}:
                raise ValueError('Skia 暂不支持此粒子类型、配色或扫字线组合')
            if kind == 'ripple':
                version = max(version, 4)
            if kind in FALLING_PARTICLES:
                version = max(version, 5)
            if kind in _ENERGY_PARTICLES:
                version = max(version, 6)
            if kind == 'scanline':
                version = max(version, 8 if vertical_particles else 7)
                ruby_scanline |= annotated and not vertical_particles
            elif band:
                version = max(version, 8)
        if animated:
            falling_only = all(not kind or kind in FALLING_PARTICLES for kind in (entry_kind, exit_kind))
            if {entry_kind, exit_kind} & ENERGY_TRANSITIONS:
                version = max(version, 11)
            else:
                version = max(version, 9 if falling_only else 10)
        if not vertical_particles:
            metrics = measured.lines[item.track_index]
            write_horizontal_pivots(final['cells'], metrics)
            final['particle_box_height'] = (
                max(float(run['ascent']) for run in metrics.slot_metrics)
                + max(float(run['descent']) for run in metrics.slot_metrics)
            )
    ir['required_features'].extend(feature for modes, feature in _VERTICAL_PARTICLE_FEATURES
                                   if modes & vertical_modes_used)
    if vertical_scaled_particles:
        ir['required_features'].append('p07_vertical_singing_with_particles')
    if reverse_vertical_particles:
        ir['required_features'].append('p07_vertical_reverse_singing_particles')
    if motion_vertical_particles:
        ir['required_features'].append('p07_vertical_singing_particle_line_animation')
    if band_vertical_particles:
        ir['required_features'].append('p07_vertical_scanline_singing_particles')
    if reverse_vertical_band_particles:
        ir['required_features'].append('p07_vertical_reverse_scanline_particles')
    if moving_vertical_band_particles:
        ir['required_features'].append('p07_vertical_scanline_particle_line_animation')
    if outlined_vertical_particles:
        ir['required_features'].append('p07_vertical_outlined_singing_particles')
    if no_wipe_vertical_particles:
        ir['required_features'].append('p07_vertical_no_wipe_singing_particles')
    if ruby_scanline:
        ir['required_features'].append('p07_ruby_scanline_particles')
    if ruby_band_singing:
        ir['required_features'].append('p07_ruby_band_singing_particles')
    if painted_singing:
        ir['required_features'].append('p07_faded_scanline_particle_paint')
    if painted_sprites:
        ir['required_features'].append('p07_singing_sprite_paints')
    if sprite_shadows:
        ir['required_features'].append('p07_singing_sprite_shadows')
    if sprite_line_gradients:
        ir['required_features'].append('p07_singing_sprite_line_gradients')
    if sprite_images:
        ir['required_features'].append('p07_singing_sprite_images')
    if painted_ripples:
        ir['required_features'].append('p07_ripple_singing_paints')
    if painted_energy:
        ir['required_features'].append('p07_energy_singing_paints')
    if sprite_split_paints:
        ir['required_features'].append('p07_singing_sprite_split_paints')
    if horizontal_line_motion:
        ir['required_features'].append('p07_singing_particle_line_animation')
    if plain_scaled_singing:
        ir['required_features'].append('p07_plain_scaled_singing_particles')
    if plain_zoom_singing:
        ir['required_features'].append('p07_plain_zoom_singing_particles')
    if horizontal_outlined_singing:
        ir['required_features'].append('p07_horizontal_outlined_singing_particles')
    if unscaled_outlined_singing:
        ir['required_features'].append('p07_unscaled_outlined_singing_particles')
    if reverse_horizontal_singing:
        ir['required_features'].append('p07_reverse_singing_particles')
    if rtl_horizontal_singing:
        ir['required_features'].append('p07_rtl_singing_particles')
    if no_wipe_horizontal_singing:
        ir['required_features'].append('p07_no_wipe_singing_particles')
    if energy_line_motion:
        ir['required_features'].append('p07_energy_transition_line_animation')
    if energy_singing_stars:
        ir['required_features'].append('p07_energy_transition_singing_stars')
    return 13 if ruby_particles else version
