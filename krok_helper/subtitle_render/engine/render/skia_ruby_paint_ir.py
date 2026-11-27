"""Project annotation outline widths and fallback colors from the reading style."""

from krok_helper.subtitle_render.domain.gradient_path import rgba
from krok_helper.subtitle_render.domain.models import materialize_ruby_decoration_changes
from krok_helper.subtitle_render.engine.ruby.style_semantics import ruby_stroke_width, ruby_stroke2_width
from krok_helper.subtitle_render.engine.style.style_semantics import effective_ruby_karaoke_colors


def ruby_outlines(style):
    colors = effective_ruby_karaoke_colors(style)
    outlines = {}
    gradients = set()
    modes = {'solid', 'gradient_horizontal', 'gradient_vertical', 'split_vertical', 'image'}
    for layer, width in (('stroke', ruby_stroke_width(style)), ('stroke2', ruby_stroke2_width(style))):
        if not width:
            continue
        before, after = getattr(colors.before, layer), getattr(colors.after, layer)
        if not all(paint.mode in modes for paint in (before, after)):
            raise ValueError('Skia 注音描边的画笔类型不支持')
        gradients.update({before.mode, after.mode} - {'solid', 'image'})
        outlines[layer] = {'width': width, 'before': list(rgba(before.color)), 'after': list(rgba(after.color))}
    return outlines, gradients


def ruby_decoration_kinds(style):
    decoration = materialize_ruby_decoration_changes(style)
    after = style.ruby_decoration_kind or decoration.get('ruby_decoration_kind', style.decoration_kind)
    before = after if style.decoration_before_sync else (
        style.ruby_decoration_kind_before or decoration.get('ruby_decoration_kind_before', after))
    return before, after
