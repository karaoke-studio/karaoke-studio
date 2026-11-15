"""Materialize lower-reading provenance and solid paint at the IR boundary."""
from krok_helper.subtitle_render.domain.gradient_path import rgba
from krok_helper.subtitle_render.domain.models import materialize_ruby_decoration_changes
from krok_helper.subtitle_render.engine.style.style_semantics import effective_ruby_karaoke_colors
from krok_helper.subtitle_render.engine.ruby.style_semantics import ruby_stroke_width, ruby_stroke2_width
from krok_helper.subtitle_render.native.protocol import ruby_to_ir


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
    decoration = materialize_ruby_decoration_changes(style)
    after = style.ruby_decoration_kind or decoration.get('ruby_decoration_kind', style.decoration_kind)
    before = after if style.decoration_before_sync else (
        style.ruby_decoration_kind_before or decoration.get('ruby_decoration_kind_before', after))
    colors = effective_ruby_karaoke_colors(style)
    if (ruby_stroke_width(style) or ruby_stroke2_width(style) or before != 'none' or after != 'none'
            or colors.before.text.mode != 'solid' or colors.after.text.mode != 'solid'):
        raise ValueError('Skia 下方注音当前仅支持实色、无描边和装饰')
    return {'before': list(rgba(colors.before.text.color)), 'after': list(rgba(colors.after.text.color))}
