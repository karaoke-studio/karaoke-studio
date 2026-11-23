"""Materialize lower-reading provenance and appearance at the IR boundary."""
from krok_helper.subtitle_render.domain.gradient_path import rgba
from krok_helper.subtitle_render.engine.style.style_semantics import effective_ruby_karaoke_colors
from krok_helper.subtitle_render.native.protocol import ruby_to_ir
from .effects.metrics import ruby_glow_concentration_level, ruby_glow_radius, ruby_shadow_dx, ruby_shadow_dy
from .effects.fills import gradient_stops
from .skia_ruby_paint_ir import ruby_decoration_kinds


_GRADIENT_MODES = {'gradient_horizontal', 'gradient_vertical', 'split_vertical'}


def _gradient_payload(fill):
    return {'mode': fill.mode,
            'stops': [[position, list(rgba(color))] for position, color in gradient_stops(fill)]}


def _image_payload(fill):
    return {'path': str(fill.image_path or ''), 'scale_pct': fill.image_scale_pct}


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


def lower_appearance(style, *, legacy_vertical_gradient=False):
    before, after = ruby_decoration_kinds(style)
    colors = effective_ruby_karaoke_colors(style)
    result = {'paint': {'before': list(rgba(colors.before.text.color)),
                        'after': list(rgba(colors.after.text.color))}}
    gradients, static_gradients, images = {}, {}, {}
    for phase, palette in (('before', colors.before), ('after', colors.after)):
        fill = palette.text
        if fill.mode == 'solid':
            continue
        if fill.mode == 'image':
            images[phase] = _image_payload(fill)
            continue
        if fill.mode not in _GRADIENT_MODES:
            raise ValueError('Skia 下方注音文字画笔类型不支持')
        gradient = _gradient_payload(fill)
        if legacy_vertical_gradient and fill.mode == 'gradient_vertical':
            gradients[phase] = gradient['stops']
        else:
            static_gradients[phase] = gradient
    if gradients:
        result['fill_gradient'] = gradients
    if static_gradients:
        result['static_fill_gradient'] = static_gradients
        result['fill_gradient_with_main'] = style.ruby_horizontal_gradient_with_main
    if images:
        result['static_fill_image'] = images
    glow, shadow, shadow_gradients, shadow_images = {}, {}, {}, {}
    for phase, kind in (('before', before), ('after', after)):
        if kind == 'none':
            continue
        sung = phase == 'after'
        radius = ruby_glow_radius(style, after=sung) if kind == 'glow' else 0
        if kind == 'glow' and radius <= 0:
            continue
        layer = getattr(colors, phase).shadow
        if kind == 'shadow' and layer.mode == 'image':
            shadow_images[phase] = _image_payload(layer)
        elif layer.mode != 'solid':
            if kind != 'shadow' or layer.mode not in _GRADIENT_MODES:
                raise ValueError('Skia 下方注音装饰当前不支持此画笔')
            shadow_gradients[phase] = _gradient_payload(layer)
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
    if shadow_gradients:
        result['lower_shadow_gradient'] = shadow_gradients
        result['shadow_gradient_with_main'] = style.ruby_horizontal_gradient_with_main
    if shadow_images:
        result['lower_shadow_image'] = shadow_images
    return result
