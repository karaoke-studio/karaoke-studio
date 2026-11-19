"""Canonical horizontal anchors for the existing singing-particle planner."""
from ..layout.line.skia_geometry import write_horizontal_pivots


_FALLING_PARTICLES = {'petal', 'feather', 'snow', 'snow_solid'}
_SINGING_PARTICLES = {'twinkle', 'twinkle_classic', 'note', 'heart', 'ripple'} | _FALLING_PARTICLES


def write_singing_particles(track, ir, plan, measured) -> int:
    """Bind solid particles to final cells and return the required profile version."""
    version = 3
    for final, item in zip(ir['skia_layout']['lines'], plan.lines, strict=True):
        source = ir['track']['lines'][item.track_index]
        bursts = source.get('fx_bursts', ())
        if not bursts:
            continue
        look = item.layout_style
        if (look.vertical or look.right_to_left or item.line.wipe_reverse or source.get('scanline')
                or source.get('karaoke_anim') != 'none' or source.get('entry_anim') != 'none'
                or source.get('exit_anim') != 'none' or track.rubies or look.lower_ruby_enabled
                or look.stroke_width_px > 0 or (look.stroke2_enabled and look.stroke2_width_px > 0)
                or any(c.role_label is not None or c.vector_glyph is not None for c in item.render_line.chars)):
            raise ValueError('Skia 唱字粒子仅支持正向横排基础正文')
        for burst in bursts:
            kind = burst['kind']
            ripple = kind == 'ripple'
            if (kind not in _SINGING_PARTICLES
                    or burst.get('front', True) != (not ripple)
                    or 'paint_id' in burst or 'char_color_ids' in burst):
                raise ValueError('Skia 暂不支持此唱字粒子类型或配色方式')
            if ripple:
                version = max(version, 4)
            if kind in _FALLING_PARTICLES:
                version = 5
        metrics = measured.lines[item.track_index]
        write_horizontal_pivots(final['cells'], metrics)
        final['particle_box_height'] = (
            max(float(run['ascent']) for run in metrics.slot_metrics)
            + max(float(run['descent']) for run in metrics.slot_metrics)
        )
    return version
