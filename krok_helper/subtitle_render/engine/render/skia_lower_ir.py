"""Materialize lower-reading provenance and appearance at the IR boundary."""
from krok_helper.subtitle_render.domain.gradient_path import rgba
from krok_helper.subtitle_render.engine.style.style_semantics import effective_ruby_karaoke_colors
from krok_helper.subtitle_render.native.protocol import ruby_to_ir
from .effects.metrics import ruby_glow_concentration_level, ruby_glow_radius, ruby_shadow_dx, ruby_shadow_dy
from .effects.fills import gradient_stops
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


def lower_appearance(style, *, allow_gradient=False):
    before, after = ruby_decoration_kinds(style)
    colors = effective_ruby_karaoke_colors(style)
    result = {'paint': {'before': list(rgba(colors.before.text.color)),
                        'after': list(rgba(colors.after.text.color))}}
    gradients = {}
    for phase, palette in (('before', colors.before), ('after', colors.after)):
        fill = palette.text
        if fill.mode == 'solid':
            continue
        if not allow_gradient or fill.mode != 'gradient_vertical':
            raise ValueError('Skia 下方注音文字当前仅支持实色或已验证的纵向渐变')
        gradients[phase] = [[position, list(rgba(color))] for position, color in gradient_stops(fill)]
    if gradients:
        result['fill_gradient'] = gradients
    glow, shadow = {}, {}
    for phase, kind in (('before', before), ('after', after)):
        if kind == 'none':
            continue
        sung = phase == 'after'
        radius = ruby_glow_radius(style, after=sung) if kind == 'glow' else 0
        if kind == 'glow' and radius <= 0:
            continue
        layer = getattr(colors, phase).shadow
        if layer.mode != 'solid':
            raise ValueError('Skia 下方注音装饰当前仅支持实色')
        color = list(rgba(layer.color))
        if kind == 'glow':
            glow[phase] = {'radius': radius, 'color': color,
                           'concentration': ruby_glow_concentration_level(style, after=sung)}
        elif kind == 'shadow':
            shadow[phase] = {'offset': [ruby_shadow_dx(style, after=sung), ruby_shadow_dy(style, after=sung)],
                             'color': color}
        else:
            raise ValueError('Skia 下方注音装饰类型无效')
    if glow:
        result['lower_glow'] = glow
    if shadow:
        result['lower_shadow'] = shadow
    return result
