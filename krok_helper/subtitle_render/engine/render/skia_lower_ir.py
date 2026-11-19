"""Materialize lower-reading provenance and solid paint at the IR boundary."""
from krok_helper.subtitle_render.domain.gradient_path import rgba
from krok_helper.subtitle_render.engine.style.style_semantics import effective_ruby_karaoke_colors
from krok_helper.subtitle_render.native.protocol import ruby_to_ir
from .skia_ruby_paint_ir import ruby_decoration_kinds


def lower_sources(prepared):
    sources, indices = [], {}
    for line_index, local in enumerate(prepared.geometry.ruby.lines):
        for group in local.rubies:
            if not group.target.lower:
                continue
            key = group.target.annotation_index
            if key in indices:
                raise ValueError('Skia 下方注音来源身份重复')
            indices[key] = len(prepared.geometry.track.rubies)+len(sources)
            sources.append({**ruby_to_ir(group.target.effective), 'lower': True,
                            'lower_line_index': line_index})
    return sources, indices


def lower_solid_paint(style):
    """Only the accepted solid subset reaches the shared drawing core."""
    before, after = ruby_decoration_kinds(style)
    colors = effective_ruby_karaoke_colors(style)
    if (before != 'none' or after != 'none'
            or colors.before.text.mode != 'solid' or colors.after.text.mode != 'solid'):
        raise ValueError('Skia 下方注音当前仅支持实色和无装饰')
    return {'before': list(rgba(colors.before.text.color)), 'after': list(rgba(colors.after.text.color))}
