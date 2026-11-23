"""Publish Skia singing-scale and Utopia composition capabilities."""

from ..guide.semantics import guide_symbol_is_bitmap
from ..layout.line.skia_geometry import write_horizontal_pivots
from ..style.style_semantics import effective_karaoke_colors, style_for_role_in_layout, uses_authored_decoration
from .skia_line_animation_ir import is_noise_animation, ordinary_line_animation
from .skia_particle_ir import faded_band_singing_particles, vertical_singing_particle_modes
from .skia_scanline_ir import fixed_faded_utopia_band, reverse_singing_scale, utopia_entry_band


def _reverse_role_scale(source, style):
    colors = effective_karaoke_colors(style)
    return reverse_singing_scale(source, style) and colors.before.text.mode == colors.after.text.mode == 'solid'


def reverse_singing_glow(item, source, *, allow_bitmap=False):
    style = item.layout_style
    if (not uses_authored_decoration(style, 'glow') or source.get('scanline')
            or not reverse_singing_scale(source, style, allow_outline=True, allow_glow=True)
            or any(c.role_label is not None or (c.vector_glyph is not None
                   and not (allow_bitmap and guide_symbol_is_bitmap(c.vector_glyph))) for c in item.render_line.chars)):
        return False
    colors = effective_karaoke_colors(style)
    return colors.before.shadow.mode == colors.after.shadow.mode == 'solid'


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
                and not source.get('stroke_flash')
                and (not source.get('fx_bursts') or vertical_singing_particle_modes(source))
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
    reverse_singing = False
    reverse_role_singing = False
    reverse_outlined_singing = False
    reverse_glowing_singing = False
    with_line_animation = False
    role_guides = {'p08_inline_bitmap_guides', 'p05_role_vector_guides'}.intersection(ir['required_features'])
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
                           or ('p07_scanline_utopia_entry' in ir['required_features'] and utopia_entry_band(source, style))
                           or item.track_index in particle_band_tracks)
        ruby_zoom = 'p07_ruby_zoom_pulse' in ir['required_features']
        ruby_zoom_motion = annotated and 'p07_ruby_zoom_line_animation' in ir['required_features']
        roles = {c.role_label for c in item.render_line.chars if c.role_label is not None}
        inline_roles = bool(roles)
        reverse_role_scale = False
        if (inline_roles and source.get('wipe_reverse') and not source.get('scanline')
                and all(c.vector_glyph is None for c in item.render_line.chars)):
            reverse_role_scale = all(_reverse_role_scale(source, style_for_role_in_layout(style, label))
                                     for label in roles)
        bitmap_glow = 'p08_reverse_bitmap_singing_glow' in ir['required_features']
        plain_or_bitmap = all(c.vector_glyph is None or ('p08_reverse_bitmap_singing' in ir['required_features']
                              and guide_symbol_is_bitmap(c.vector_glyph)
                              and (c.vector_glyph.bitmap_no_decor or bitmap_glow))
                              for c in item.render_line.chars)
        allow_reverse_outline = (
            (style.stroke_width_px > 0 or (style.stroke2_enabled and style.stroke2_width_px > 0))
            and not (inline_roles or source.get('scanline'))
            and plain_or_bitmap
        )
        allow_reverse_glow = reverse_singing_glow(item, source, allow_bitmap=bitmap_glow)
        reverse_scale = (not annotated and (not inline_roles or reverse_role_scale)
                         and reverse_singing_scale(source, style, allow_outline=allow_reverse_outline,
                                                   allow_glow=allow_reverse_glow)
                         and plain_or_bitmap)
        static_role_scale = animations == {'none'} and not (
            source.get('zoom_pulse') or source.get('scanline') or source.get('stroke_flash') or source.get('fx_bursts'))
        faded_role_scale = ('p07_inline_role_faded_scanline' in ir['required_features']
                            and fixed_faded_utopia_band(source, style))
        guide_role_scale = (bool(role_guides) and 'fade' in animations
                             and animations <= {'none', 'fade'} and not any(
                                 source.get(flag) for flag in ('scanline', 'zoom_pulse', 'stroke_flash', 'fx_bursts')))
        role_scale = inline_roles and scales and (static_role_scale or faded_role_scale or guide_role_scale)
        rtl_singing = scales and annotated and 'p07_rtl_ruby_singing_scale' in ir['required_features']
        if (style.vertical or (style.right_to_left and not rtl_singing) or (item.line.wipe_reverse and not reverse_scale)
                or (annotated and ((source.get('zoom_pulse') and not ruby_zoom)
                                   or (animations != {'none'} and not (line_animation or with_character_transition or with_noise))))
                or (not (line_animation or with_character_transition or with_noise) and not animations <= {'none', 'utopia'})
                or source.get('karaoke_anim') not in {'none', 'utopia'}
                or (line_animation and source.get('zoom_pulse') and not ruby_zoom_motion)
                or (source.get('scanline') and ((animations != {'none'} and not scanline_motion) or (source.get('zoom_pulse')
                    and 'p07_composed_scanline' not in ir['required_features'])))
                or (source.get('stroke_flash') and 'p07_composed_stroke_flash' not in ir['required_features'])
                or (source.get('fx_bursts') and ir['skia_layout'].get('sing_twinkle_schema', 0) < 12
                    and not faded_band_singing_particles(source, style))
                or (inline_roles and not role_scale)):
            raise ValueError('Skia Utopia 动画当前仅支持正向横排实色正文')
        write_horizontal_pivots(final['cells'], measured.lines[item.track_index])
        singing = singing or scales
        role_singing = role_singing or role_scale
        reverse_singing |= reverse_scale
        reverse_role_singing |= reverse_scale and reverse_role_scale
        reverse_outlined_singing |= reverse_scale and allow_reverse_outline
        reverse_glowing_singing |= reverse_scale and allow_reverse_glow
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
    if reverse_singing:
        ir['required_features'].append('p07_horizontal_reverse_singing')
    if reverse_role_singing:
        ir['required_features'].append('p07_reverse_role_singing')
    if reverse_outlined_singing:
        ir['required_features'].append('p07_reverse_singing_outline')
    if reverse_glowing_singing:
        ir['required_features'].append('p07_reverse_singing_glow')
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
