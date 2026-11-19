"""Project solid annotation outlines from the effective reading style."""

from krok_helper.subtitle_render.domain.gradient_path import rgba
from krok_helper.subtitle_render.domain.models import materialize_ruby_decoration_changes
from krok_helper.subtitle_render.engine.ruby.style_semantics import ruby_stroke_width, ruby_stroke2_width
from krok_helper.subtitle_render.engine.style.style_semantics import effective_ruby_karaoke_colors


def solid_ruby_outlines(style):
    colors = effective_ruby_karaoke_colors(style)
    outlines = {}
    for layer, width in (('stroke', ruby_stroke_width(style)), ('stroke2', ruby_stroke2_width(style))):
        if not width:
            continue
        before, after = getattr(colors.before, layer), getattr(colors.after, layer)
        if before.mode != 'solid' or after.mode != 'solid':
            raise ValueError('Skia 注音描边当前仅支持实色')
        outlines[layer] = {'width': width, 'before': list(rgba(before.color)), 'after': list(rgba(after.color))}
    return outlines


def ruby_decoration_kinds(style):
    decoration = materialize_ruby_decoration_changes(style)
    after = style.ruby_decoration_kind or decoration.get('ruby_decoration_kind', style.decoration_kind)
    before = after if style.decoration_before_sync else (
        style.ruby_decoration_kind_before or decoration.get('ruby_decoration_kind_before', after))
    return before, after
