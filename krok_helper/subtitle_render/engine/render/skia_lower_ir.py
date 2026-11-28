"""Materialize lower-reading provenance and appearance at the IR boundary."""
from krok_helper.subtitle_render.domain.gradient_path import rgba
from krok_helper.subtitle_render.domain.color import linear_rgba
from krok_helper.subtitle_render.domain.models import materialize_ruby_decoration_changes, normalize_glow_strength
from krok_helper.subtitle_render.engine.ruby.style_semantics import ruby_stroke_width, ruby_stroke2_width
from krok_helper.subtitle_render.engine.style.style_semantics import effective_ruby_karaoke_colors
from krok_helper.subtitle_render.native.protocol import ruby_to_ir
from .effects.metrics import ruby_glow_concentration_level, ruby_glow_radius
from .effects.fills import gradient_stops
from .skia_ruby_paint_ir import ruby_decoration_kinds


_GRADIENT_MODES = {'gradient_horizontal', 'gradient_vertical', 'gradient_radial', 'split_vertical'}


def _shadow_offset(style, axis, sung):
    """Lower reading shadow offset as the GPU resolves it.

    A reading without its own offset uses the main text's offset unscaled
    (the GPU does not scale it by the reading size, unlike the CPU painter).
    """
    own = getattr(style, f'ruby_shadow_offset_{axis}')
    if sung or style.decoration_before_sync:
        return int(own if own is not None else getattr(style, f'shadow_offset_{axis}'))
    for value in (getattr(style, f'ruby_shadow_before_offset_{axis}'), own,
                  getattr(style, f'shadow_before_offset_{axis}')):
        if value is not None:
            return int(value)
    return int(getattr(style, f'shadow_offset_{axis}'))


def _split_stops(fill):
    # Split bands use their own stops, as the GPU's paint projection does.
    raw = fill.split_stops or [(0, fill.split_top_color), (fill.split_position_pct, fill.split_bottom_color),
                               (100, fill.split_bottom_color)]
    return sorted(((min(max(float(position), 0.0), 100.0), color) for position, color in raw), key=lambda s: s[0])


def _gradient_payload(fill):
    stops = _split_stops(fill) if fill.mode == 'split_vertical' else gradient_stops(fill)
    payload = {'mode': fill.mode, 'stops': [[position, color if linear_rgba(color) is not None else list(rgba(color))]
                                            for position, color in stops]}
    if fill.gradient_angle_deg is not None:
        payload['gradient_angle_deg'] = round(float(fill.gradient_angle_deg) % 360.0, 3)
    if fill.gradient_space != 'line':
        payload['gradient_space'] = fill.gradient_space
    if fill.mode == 'gradient_radial':
        payload['radial_center_x_pct'] = float(fill.radial_center_x_pct)
        payload['radial_center_y_pct'] = float(fill.radial_center_y_pct)
        payload['radial_radius_x_pct'] = float(fill.radial_radius_x_pct)
        payload['radial_radius_y_pct'] = float(fill.radial_radius_y_pct)
    return payload


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
        if (legacy_vertical_gradient and fill.mode == 'gradient_vertical'
                and fill.gradient_angle_deg is None and fill.gradient_space == 'line'):
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
    # Like the GPU, lower outlines paint with the lower reading's own stroke fills.
    outline_gradients, outline_images = {}, {}
    for layer, width in (('stroke', ruby_stroke_width(style)), ('stroke2', ruby_stroke2_width(style))):
        if width <= 0:
            continue
        for phase, palette in (('before', colors.before), ('after', colors.after)):
            fill = getattr(palette, layer)
            if fill.mode in _GRADIENT_MODES:
                outline_gradients.setdefault(layer, {})[phase] = _gradient_payload(fill)
            elif fill.mode == 'image':
                outline_images.setdefault(layer, {})[phase] = _image_payload(fill)
    if outline_gradients:
        result['lower_outline_gradient'] = outline_gradients
        result['outline_gradient_with_main'] = style.ruby_horizontal_gradient_with_main
    if outline_images:
        result['lower_outline_image'] = outline_images
    glow, shadow, shadow_gradients, shadow_images, glow_gradients, glow_images = {}, {}, {}, {}, {}, {}
    ruby_shadow_defaults = materialize_ruby_decoration_changes(style)
    for phase, kind in (('before', before), ('after', after)):
        if kind == 'none':
            continue
        sung = phase == 'after'
        radius = ruby_glow_radius(style, after=sung) if kind in {'glow', 'both'} else 0
        if kind == 'glow' and radius <= 0:
            continue
        state = getattr(colors, phase)
        shadow_layer = state.shadow
        glow_layer = state.glow or shadow_layer
        if kind in {'glow', 'both'} and radius > 0:
            layer = glow_layer
            if layer.mode not in {'solid', 'image', *_GRADIENT_MODES}:
                raise ValueError('Skia 下方注音发光画笔类型无效')
            color = list(rgba(layer.color))
            glow[phase] = {'radius': radius, 'color': color,
                           'concentration': ruby_glow_concentration_level(style, after=sung),
                           'strength': normalize_glow_strength(style.glow_strength)}
            if layer.mode == 'image':
                glow_images[phase] = _image_payload(layer)
            elif layer.mode != 'solid':
                glow_gradients[phase] = _gradient_payload(layer)
        if kind in {'shadow', 'both'}:
            layer = shadow_layer
            if layer.mode not in {'solid', 'image', *_GRADIENT_MODES}:
                raise ValueError('Skia 下方注音阴影画笔类型无效')
            color = list(rgba(layer.color))
            blur = style.ruby_shadow_blur_px
            spread = style.ruby_shadow_spread_px
            shadow_state = {'offset': [_shadow_offset(style, 'x', sung), _shadow_offset(style, 'y', sung)],
                            'color': color}
            blur_px = int(blur if blur is not None else ruby_shadow_defaults['ruby_shadow_blur_px'])
            spread_px = int(spread if spread is not None else ruby_shadow_defaults['ruby_shadow_spread_px'])
            if blur_px or spread_px:
                shadow_state.update(blur=blur_px, spread=spread_px)
            shadow[phase] = shadow_state
            if layer.mode == 'image':
                shadow_images[phase] = _image_payload(layer)
            elif layer.mode != 'solid':
                shadow_gradients[phase] = _gradient_payload(layer)
    if glow:
        result['lower_glow'] = glow
    if shadow:
        result['lower_shadow'] = shadow
    if shadow_gradients:
        result['lower_shadow_gradient'] = shadow_gradients
        result['shadow_gradient_with_main'] = style.ruby_horizontal_gradient_with_main
    if shadow_images:
        result['lower_shadow_image'] = shadow_images
    if glow_gradients:
        result['lower_glow_gradient'] = glow_gradients
        result['glow_gradient_with_main'] = style.ruby_horizontal_gradient_with_main
    if glow_images:
        result['lower_glow_image'] = glow_images
    return result
