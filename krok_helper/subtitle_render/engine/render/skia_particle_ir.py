"""Canonical horizontal anchors for the existing singing-particle planner."""
from ..layout.line.skia_geometry import write_horizontal_pivots


FALLING_PARTICLES = frozenset({'petal', 'feather', 'snow', 'snow_solid'})
ENERGY_TRANSITIONS = frozenset({'flame', 'lightning'})
PARTICLE_TRANSITIONS = {
    **{kind: kind for kind in FALLING_PARTICLES | ENERGY_TRANSITIONS | {'note', 'heart', 'ripple', 'sparkle'}},
    'assemble_in': 'assemble',
    'dissolve_out': 'dissolve',
}
RUBY_PARTICLE_EXITS = {'petal', 'feather', 'snow_solid', 'note', 'heart', 'sparkle', 'lightning', 'flame'}
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
_ENERGY_PARTICLES = {'lightning', 'lightning_glow', 'flame', 'flame_alt', 'flame_glow', 'smoke', 'ember', 'spark'}
_BACK_PARTICLES = {'ripple', 'lightning_glow', 'flame_glow', 'smoke', 'shockwave'}
_PARTICLES = ({'twinkle', 'twinkle_classic', 'scanline', 'fireball', 'flash', 'shockwave', 'ash'}
              | set(PARTICLE_TRANSITIONS.values()) | _ENERGY_PARTICLES)


def _transition_layers(kind, entry):
    return _TRANSITION_LAYERS.get((kind, entry), {kind})


def write_particle_anchors(track, ir, plan, measured, composed_lines=()) -> int:
    """Bind solid particles to final cells and return the required profile version."""
    version = 3
    ruby_scanline = False
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
        scaled_singing = annotated and source.get('karaoke_anim') == 'utopia' and not source.get('zoom_pulse')
        entry_sweep = 1 if entry_kind in FALLING_PARTICLES else 0
        exit_sweep = -1 if exit_kind in FALLING_PARTICLES else 0
        if (look.vertical or look.right_to_left or (item.line.wipe_reverse and not band)
                or (source.get('karaoke_anim') != 'none' and not scaled_singing)
                or (entry != 'none' and (not entry_kind or entry == 'dissolve_out'))
                or (exit_ != 'none' and (not exit_kind or exit_ == 'assemble_in'))
                or (animated and (len(plan.lines) != 1 or band or look.sing_fx != 'none'))
                or look.lower_ruby_enabled or (annotated and animated and index not in composed_lines)
                or (not annotated and (look.stroke_width_px > 0
                    or (look.stroke2_enabled and look.stroke2_width_px > 0)))
                or any(c.role_label is not None or c.vector_glyph is not None for c in item.render_line.chars)):
            raise ValueError('Skia 唱字粒子仅支持正向横排基础正文')
        for burst in bursts:
            kind = burst['kind']
            if (kind not in _PARTICLES
                    or (kind == 'scanline' and not band)
                    or (annotated and band and kind != 'scanline')
                    or (band and item.line.wipe_reverse and kind != 'scanline')
                    or burst.get('front', True) != (kind not in _BACK_PARTICLES)
                    or 'paint_id' in burst or 'char_color_ids' in burst):
                raise ValueError('Skia 暂不支持此粒子类型、配色或扫字线组合')
            if animated:
                sweep = burst['sweep']
                if not ((entry_kind and sweep == entry_sweep and kind in _transition_layers(entry_kind, True))
                        or (exit_kind and sweep == exit_sweep and kind in _transition_layers(exit_kind, False))):
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
                version = max(version, 7)
                ruby_scanline = ruby_scanline or annotated
            elif band:
                version = 8
        if animated:
            falling_only = all(not kind or kind in FALLING_PARTICLES for kind in (entry_kind, exit_kind))
            version = 9 if falling_only else 10
            if {entry_kind, exit_kind} & ENERGY_TRANSITIONS:
                version = 11
        metrics = measured.lines[item.track_index]
        write_horizontal_pivots(final['cells'], metrics)
        final['particle_box_height'] = (
            max(float(run['ascent']) for run in metrics.slot_metrics)
            + max(float(run['descent']) for run in metrics.slot_metrics)
        )
    if ruby_scanline:
        ir['required_features'].append('p07_ruby_scanline_particles')
    return 13 if track.rubies else version
