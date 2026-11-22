"""Publish Skia singing-scale and Utopia composition capabilities."""

from ..layout.line.skia_geometry import write_horizontal_pivots
from ..style.style_semantics import uses_authored_decoration
from .skia_line_animation_ir import is_noise_animation, ordinary_line_animation


def vertical_singing_lines(ir, plan):
    """Return source indices that can use singing poses in the vertical layout."""
    tracks = set()
    for item in plan.lines:
        look = item.layout_style
        source = ir['track']['lines'][item.track_index]
        if (look.vertical and not look.right_to_left
                and source.get('karaoke_anim') == 'utopia'
                and ordinary_line_animation(source)
                and ({source.get('entry_anim'), source.get('exit_anim')} == {'none'}
                     or not item.line.wipe_reverse)
                and not any(source.get(key) for key in ('stroke_flash', 'fx_bursts'))
                and all(char.role_label is None and char.vector_glyph is None for char in item.render_line.chars)):
            tracks.add(item.track_index)
    return tracks


def write_utopia_pivots(ir, plan, measured, transition_lines, particle_band_tracks, vertical_singing):
    """Negotiate singing effects and complete the canonical pose data."""
    singing, transition, singing_ruby = False, False, False
    vertical_ruby_zoom = False
    vertical_line_animation = False
    vertical_shadow = False
    role_singing = False
    with_line_animation = False
    for index, (final, item) in enumerate(zip(ir['skia_layout']['lines'], plan.lines, strict=True)):
        source = ir['track']['lines'][item.track_index]
        scales = source.get('karaoke_anim') == 'utopia'
        animations = {source.get('entry_anim'), source.get('exit_anim')}
        if not scales and 'utopia' not in animations:
            continue
        style = item.layout_style
        annotated = bool(final.get('ruby_groups'))
        if item.track_index in vertical_singing:
            singing = True
            singing_ruby |= annotated
            vertical_ruby_zoom |= annotated and bool(source.get('zoom_pulse'))
            vertical_line_animation |= animations != {'none'}
            vertical_shadow |= uses_authored_decoration(style, 'shadow')
            continue
        line_animation = scales and animations != {'none'} and ordinary_line_animation(source)
        with_character_transition = index in transition_lines
        with_noise = is_noise_animation(source) and 'p07_noise_composition' in ir['required_features']
        scanline_motion = (line_animation and 'p07_scanline_line_animation' in ir['required_features']
                           or item.track_index in particle_band_tracks)
        ruby_zoom = 'p07_ruby_zoom_pulse' in ir['required_features']
        inline_roles = any(c.role_label is not None for c in item.render_line.chars)
        role_scale = inline_roles and scales and animations == {'none'} and not source.get('zoom_pulse')
        role_scale = role_scale and not (source.get('scanline') or source.get('stroke_flash') or source.get('fx_bursts'))
        rtl_singing = scales and annotated and 'p07_rtl_ruby_singing_scale' in ir['required_features']
        if (style.vertical or (style.right_to_left and not rtl_singing) or item.line.wipe_reverse
                or (annotated and ((source.get('zoom_pulse') and not ruby_zoom)
                                   or (animations != {'none'} and not (line_animation or with_character_transition or with_noise))))
                or (not (line_animation or with_character_transition or with_noise) and not animations <= {'none', 'utopia'})
                or source.get('karaoke_anim') not in {'none', 'utopia'}
                or (line_animation and source.get('zoom_pulse'))
                or (source.get('scanline') and ((animations != {'none'} and not scanline_motion) or (source.get('zoom_pulse')
                    and 'p07_composed_scanline' not in ir['required_features'])))
                or (source.get('stroke_flash') and 'p07_composed_stroke_flash' not in ir['required_features'])
                or (source.get('fx_bursts') and ir['skia_layout'].get('sing_twinkle_schema', 0) < 12)
                or (inline_roles and not role_scale)):
            raise ValueError('Skia Utopia 动画当前仅支持正向横排实色正文')
        write_horizontal_pivots(final['cells'], measured.lines[item.track_index])
        singing = singing or scales
        role_singing = role_singing or role_scale
        transition = transition or 'utopia' in animations
        singing_ruby = singing_ruby or (scales and annotated)
        with_line_animation = with_line_animation or line_animation
    if singing:
        ir['required_features'].append('p07_singing_scale')
        ir['skia_layout']['singing_scale_schema'] = 1
    if vertical_singing:
        ir['required_features'].append('p07_vertical_singing_scale')
        if any(ir['track']['lines'][index].get('wipe_reverse') for index in vertical_singing):
            ir['required_features'].append('p07_vertical_reverse_singing')
        if any(ir['track']['lines'][index].get('zoom_pulse') for index in vertical_singing):
            ir['required_features'].append('p07_vertical_zoom_pulse')
        if 'p05_vertical_columns' in ir['required_features']:
            ir['required_features'].append('p07_vertical_columns_singing_scale')
    if role_singing:
        ir['required_features'].append('p07_role_singing_scale')
    if transition:
        ir['required_features'].append('p07_utopia_transition')
        ir['skia_layout']['utopia_transition_schema'] = 1
    if singing_ruby:
        ir['required_features'].append('p07_ruby_singing_scale')
    if vertical_ruby_zoom:
        ir['required_features'].append('p07_ruby_zoom_pulse')
    if vertical_line_animation:
        ir['required_features'].append('p07_vertical_singing_line_animation')
    if vertical_shadow:
        ir['required_features'].append('p07_vertical_singing_shadow')
    if with_line_animation or vertical_line_animation:
        ir['required_features'].append('p07_utopia_line_animation')
