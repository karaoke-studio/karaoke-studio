"""Canonical anchors and capabilities for the singing-particle planner."""
from ..layout.line.skia_geometry import write_horizontal_pivots
from .skia_line_animation_ir import ordinary_line_animation


FALLING_PARTICLES = frozenset({'petal', 'feather', 'snow', 'snow_solid'})
ENERGY_TRANSITIONS = frozenset({'flame', 'lightning'})
PARTICLE_TRANSITIONS = {
    **{kind: kind for kind in FALLING_PARTICLES | ENERGY_TRANSITIONS | {'note', 'heart', 'ripple', 'sparkle'}},
    'assemble_in': 'assemble',
    'dissolve_out': 'dissolve',
}
RUBY_PARTICLE_EXITS = {'petal', 'feather', 'snow', 'snow_solid', 'note', 'heart', 'ripple', 'sparkle'}
RUBY_PARTICLE_ENTRIES = {'petal', 'feather', 'heart', 'sparkle', 'snow', 'snow_solid'}
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
_VERTICAL_STARS = {('twinkle', 1), ('twinkle_classic', 0)}
_VERTICAL_SHAPES = {(kind, 0) for kind in ('note', 'heart', 'ripple')}
_VERTICAL_FALLING = {(kind, 0) for kind in FALLING_PARTICLES}
_VERTICAL_LIGHTNING = {(kind, 0) for kind in ('lightning', 'lightning_glow')}
_VERTICAL_FLAME = set(_FLAME_SINGING_SWEEPS.items())
_VERTICAL_SCANLINE = {('scanline', 0)}
_VERTICAL_PARTICLE_FEATURES = (
    (_VERTICAL_STARS, 'p07_vertical_sing_twinkle'),
    (_VERTICAL_SHAPES, 'p07_vertical_sing_shapes'),
    (_VERTICAL_FALLING, 'p07_vertical_sing_falling'),
    (_VERTICAL_LIGHTNING, 'p07_vertical_sing_lightning'),
    (_VERTICAL_FLAME, 'p07_vertical_sing_flame'),
    (_VERTICAL_SCANLINE, 'p07_vertical_scanline_particles'),
)
_VERTICAL_PARTICLE_MODES = set().union(*(modes for modes, _ in _VERTICAL_PARTICLE_FEATURES))
_PARTICLES = ({'twinkle', 'twinkle_classic', 'scanline', 'fireball', 'flash', 'shockwave', 'ash'}
              | set(PARTICLE_TRANSITIONS.values()) | _ENERGY_PARTICLES)


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


def vertical_singing_particle_modes(source):
    """Return supported particle kinds and sweeps for a fixed vertical cell anchor."""
    limited_motion = source.get('wipe_reverse') and (
        source.get('entry_anim'), source.get('exit_anim')) != ('none', 'none')
    if (source.get('stroke_flash') or limited_motion
            or source.get('karaoke_anim') not in {'none', 'utopia'} or not ordinary_line_animation(source)):
        return set()
    modes = {(burst['kind'], burst['sweep']) for burst in source.get('fx_bursts', ())}
    if not source.get('scanline') and modes & _VERTICAL_SCANLINE:
        return set()
    return modes if modes <= _VERTICAL_PARTICLE_MODES else set()


def write_particle_anchors(track, ir, plan, measured, composed_lines=()) -> int:
    """Bind solid particles to final cells and return the required profile version."""
    version = 3
    ruby_scanline = ruby_band_singing = False
    vertical_modes_used = set()
    vertical_scaled_particles = False
    reverse_vertical_particles = False
    motion_vertical_particles = False
    band_vertical_particles = False
    reverse_vertical_band_particles = False
    moving_vertical_band_particles = False
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
        annotated = bool(track.rubies)
        supported_band = not band or (look.scanline_shape == 'band' and look.scanline_mode != 'role')
        vertical_modes = vertical_singing_particle_modes(source) if look.vertical and supported_band else set()
        vertical_particles = bool(vertical_modes)
        if vertical_particles:
            vertical_modes_used.update(vertical_modes)
            vertical_scaled_particles |= source.get('karaoke_anim') == 'utopia'
            reverse_vertical_particles |= item.line.wipe_reverse
            motion_vertical_particles |= (entry, exit_) != ('none', 'none')
            band_vertical_particles |= band and bool(vertical_modes - _VERTICAL_SCANLINE)
            reverse_vertical_band_particles |= band and item.line.wipe_reverse
            moving_vertical_band_particles |= band and (entry, exit_) != ('none', 'none')
        petal_with_flame = (annotated and petal_entry_with_flame(source, look)
                           and 'p07_ruby_petal_entry_flame_singing' in ir['required_features'])
        petal_with_band = (petal_with_flame and petal_entry_with_scanline(source, look)
                           and 'p07_ruby_petal_entry_scanline' in ir['required_features'])
        singing_under_shape = (annotated and band and (not animated or petal_with_band) and look.scanline_shape != 'band'
                               and 'p07_shaped_scanline_singing_particles' in ir['required_features'])
        singing_under_band = (not look.vertical and annotated and band
                              and (not animated or petal_with_band) and look.scanline_shape == 'band'
                              and look.scanline_mode != 'role' and not item.line.wipe_reverse
                              and any(burst['kind'] != 'scanline' for burst in bursts))
        ruby_band_singing = ruby_band_singing or singing_under_band
        scaled_singing = annotated and source.get('karaoke_anim') == 'utopia' and not source.get('zoom_pulse')
        entry_sweep = 1 if entry_kind in FALLING_PARTICLES else 0
        exit_sweep = -1 if exit_kind in FALLING_PARTICLES else 0
        if ((look.vertical and not vertical_particles) or look.right_to_left
                or (item.line.wipe_reverse and not (band or vertical_particles))
                or (source.get('karaoke_anim') != 'none' and not (scaled_singing or vertical_particles))
                or (not vertical_particles and entry != 'none' and (not entry_kind or entry == 'dissolve_out'))
                or (not vertical_particles and exit_ != 'none' and (not exit_kind or exit_ == 'assemble_in'))
                or (animated and (len(plan.lines) != 1 or (band and not petal_with_band)
                                  or (look.sing_fx != 'none' and not petal_with_flame)))
                or look.lower_ruby_enabled or (annotated and animated and index not in composed_lines)
                or (not annotated and (look.stroke_width_px > 0
                    or (look.stroke2_enabled and look.stroke2_width_px > 0)))
                or any(c.role_label is not None or c.vector_glyph is not None for c in item.render_line.chars)):
            raise ValueError('Skia 唱字粒子的布局或特效组合暂不支持')
        for burst in bursts:
            kind = burst['kind']
            if (kind not in _PARTICLES
                    or (kind == 'scanline' and not band)
                    or (annotated and band and kind != 'scanline'
                        and not (vertical_particles or singing_under_shape or singing_under_band))
                    or (singing_under_shape and kind == 'scanline')
                    or (band and item.line.wipe_reverse and kind != 'scanline' and not vertical_particles)
                    or burst.get('front', True) != (kind not in _BACK_PARTICLES)
                    or 'paint_id' in burst or 'char_color_ids' in burst):
                raise ValueError('Skia 暂不支持此粒子类型、配色或扫字线组合')
            if animated:
                sweep = burst['sweep']
                if not ((entry_kind and sweep == entry_sweep and kind in _transition_layers(entry_kind, True))
                        or (exit_kind and sweep == exit_sweep and kind in _transition_layers(exit_kind, False))
                        or (petal_with_flame and sweep == _FLAME_SINGING_SWEEPS.get(kind))
                        or (petal_with_band and kind == 'scanline' and sweep == 0)):
                    raise ValueError('Skia 粒子出入场暂不支持与唱字粒子同场')
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
                version = 8
        if animated:
            falling_only = all(not kind or kind in FALLING_PARTICLES for kind in (entry_kind, exit_kind))
            version = 9 if falling_only else 10
            if {entry_kind, exit_kind} & ENERGY_TRANSITIONS:
                version = 11
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
    if ruby_scanline:
        ir['required_features'].append('p07_ruby_scanline_particles')
    if ruby_band_singing:
        ir['required_features'].append('p07_ruby_band_singing_particles')
    return 13 if track.rubies else version
