"""Materialize lower-reading provenance and solid appearance at the IR boundary."""
from krok_helper.subtitle_render.domain.gradient_path import rgba
from krok_helper.subtitle_render.engine.style.style_semantics import effective_ruby_karaoke_colors
from krok_helper.subtitle_render.native.protocol import ruby_to_ir
from .effects.metrics import ruby_glow_concentration_level, ruby_glow_radius
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


def lower_solid_appearance(style):
    before, after = ruby_decoration_kinds(style)
    colors = effective_ruby_karaoke_colors(style)
    if (not {before, after} <= {'none', 'glow'}
            or colors.before.text.mode != 'solid' or colors.after.text.mode != 'solid'):
        raise ValueError('Skia 下方注音当前仅支持实色文字和实色光晕')
    result = {'paint': {'before': list(rgba(colors.before.text.color)),
                        'after': list(rgba(colors.after.text.color))}}
    glow = {}
    for phase, kind in (('before', before), ('after', after)):
        if kind != 'glow':
            continue
        layer = getattr(colors, phase).shadow
        radius = ruby_glow_radius(style, after=phase == 'after')
        if radius <= 0:
            continue
        if layer.mode != 'solid':
            raise ValueError('Skia 下方注音光晕当前仅支持实色')
        glow[phase] = {'radius': radius, 'color': list(rgba(layer.color)),
                       'concentration': ruby_glow_concentration_level(style, after=phase == 'after')}
    if glow:
        result['lower_glow'] = glow
    return result
