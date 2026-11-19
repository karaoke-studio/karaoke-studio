"""Canonical horizontal anchors for the existing singing-particle planner."""
from ..layout.line.skia_geometry import write_horizontal_pivots


_FALLING_PARTICLES = {'petal', 'feather', 'snow', 'snow_solid'}
_ENERGY_PARTICLES = {'lightning', 'lightning_glow', 'flame', 'flame_alt', 'flame_glow', 'smoke', 'ember', 'spark'}
_BACK_PARTICLES = {'ripple', 'lightning_glow', 'flame_glow', 'smoke'}
_PARTICLES = {'twinkle', 'twinkle_classic', 'note', 'heart', 'ripple', 'scanline'} | _FALLING_PARTICLES | _ENERGY_PARTICLES


def write_particle_anchors(track, ir, plan, measured) -> int:
    """Bind solid particles to final cells and return the required profile version."""
    version = 3
    for final, item in zip(ir['skia_layout']['lines'], plan.lines, strict=True):
        source = ir['track']['lines'][item.track_index]
        bursts = source.get('fx_bursts', ())
        if not bursts:
            continue
        look = item.layout_style
        if (look.vertical or look.right_to_left or item.line.wipe_reverse
                or source.get('karaoke_anim') != 'none' or source.get('entry_anim') != 'none'
                or source.get('exit_anim') != 'none' or track.rubies or look.lower_ruby_enabled
                or look.stroke_width_px > 0 or (look.stroke2_enabled and look.stroke2_width_px > 0)
                or any(c.role_label is not None or c.vector_glyph is not None for c in item.render_line.chars)):
            raise ValueError('Skia 唱字粒子仅支持正向横排基础正文')
        for burst in bursts:
            kind = burst['kind']
            if (kind not in _PARTICLES
                    or bool(source.get('scanline')) != (kind == 'scanline')
                    or burst.get('front', True) != (kind not in _BACK_PARTICLES)
                    or 'paint_id' in burst or 'char_color_ids' in burst):
                raise ValueError('Skia 暂不支持此粒子类型、配色或扫字线组合')
            if kind == 'ripple':
                version = max(version, 4)
            if kind in _FALLING_PARTICLES:
                version = max(version, 5)
            if kind in _ENERGY_PARTICLES:
                version = max(version, 6)
            if kind == 'scanline':
                version = 7
        metrics = measured.lines[item.track_index]
        write_horizontal_pivots(final['cells'], metrics)
        final['particle_box_height'] = (
            max(float(run['ascent']) for run in metrics.slot_metrics)
            + max(float(run['descent']) for run in metrics.slot_metrics)
        )
    return version
