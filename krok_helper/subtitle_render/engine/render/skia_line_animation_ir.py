"""Check line animation modes and declare whole-line animation in Skia IR."""

from dataclasses import replace

from ...domain.models import effective_karaoke_animation, effective_karaoke_scanline
from ...domain.timing import CHAR_TRANSITION_ANIMS


_INACTIVE_VERTICAL_TRANSITIONS = CHAR_TRANSITION_ANIMS | {'utopia'}


def project_vertical_line_animations(plan):
    """Keep the scheduled display windows and project only drawable animations."""
    lines = []
    for line in plan.lines:
        style = line.animation_style
        overrides = {}
        if style.entry_anim in _INACTIVE_VERTICAL_TRANSITIONS:
            overrides.update(entry_anim='none', entry_lead_ms=0)
        if style.exit_anim in _INACTIVE_VERTICAL_TRANSITIONS:
            overrides.update(exit_anim='none', exit_fade_ms=0)
        if style.karaoke_stroke_flash:
            overrides['karaoke_stroke_flash'] = False
        if any(cell.vector_glyph is not None for cell in line.render_line.chars):
            # The vertical guide profiles retain the owning cell's basic clock.
            if effective_karaoke_animation(style) == 'utopia' or style.karaoke_anim == 'scanline':
                overrides['karaoke_anim'] = 'none'
            if effective_karaoke_scanline(style):
                overrides['scanline_enabled'] = False
            if style.sing_fx != 'none':
                overrides['sing_fx'] = 'none'
        lines.append(replace(line, animation_style=replace(style, **overrides)) if overrides else line)
    return replace(plan, lines=tuple(lines))


def ordinary_line_animation(source):
    return (source.get('entry_anim') in {'none', 'fade', 'slide_in', 'rise'}
            and source.get('exit_anim') in {'none', 'fade', 'slide_out', 'rise'})


def is_noise_animation(source):
    animations = {source.get('entry_anim'), source.get('exit_anim')}
    return 'noise' in animations and animations <= {'none', 'noise'}


def declare_line_animations(ir):
    if any(line.get('entry_anim') == 'fade' or line.get('exit_anim') == 'fade'
           for line in ir['track']['lines']):
        ir['required_features'].append('p07_line_fade')
    if any(line.get('entry_anim') in {'slide_in', 'rise'} or line.get('exit_anim') in {'slide_out', 'rise'}
           for line in ir['track']['lines']):
        ir['required_features'].append('p07_line_motion')


def write_line_animation_schemas(ir) -> bool:
    animates_line = False
    for feature, schema in (('p07_line_fade', 'line_fade_schema'), ('p07_line_motion', 'line_motion_schema')):
        if feature in ir['required_features']:
            ir['skia_layout'][schema] = 1
            animates_line = True
    return animates_line
