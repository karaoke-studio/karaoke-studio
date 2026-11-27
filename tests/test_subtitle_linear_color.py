import pytest

from krok_helper.subtitle_render.domain.color import (
    display_hex, hsv_linear, linear_color, linear_hsv, linear_rgba, rgba_float,
    linear_to_srgb, srgb_to_linear,
)


def test_srgb_and_linear_are_distinct_representations_of_the_same_color():
    channels = rgba_float('#808080')
    assert channels[:3] == pytest.approx([.2158605] * 3)
    assert display_hex(linear_color(channels)) == '#FF808080'
    assert display_hex('linear(0.5,0.5,0.5,0.5)') == '#80BCBCBC'
    for c in (0, .003, .04, .5, 1, 10):
        assert srgb_to_linear(linear_to_srgb(c)) == pytest.approx(c)


def test_hdr_channels_round_trip_without_hex_quantization():
    channels = (10., .045123456789, .000000123, .314159265)
    assert linear_rgba(linear_color(channels)) == channels
    assert linear_hsv((1, .045, 0, 1)) == pytest.approx((2.7, 1, 1))
    assert linear_hsv((10, .045, 0, 1)) == pytest.approx((.27, 1, 10))
    assert linear_hsv((10, .45, 0, 1)) == pytest.approx((2.7, 1, 10))
    assert hsv_linear(*linear_hsv(channels), channels[3]) == pytest.approx(channels)


@pytest.mark.parametrize('value', ['linear(nan,0,0,1)', 'linear(inf,0,0,1)',
    'linear(-1,0,0,1)', 'linear(1,0,0,2)', 'linear(65505,0,0,1)', 'linear(1,2,3)', 'linear(1,2,3,1'])
def test_invalid_linear_colors_are_rejected(value):
    with pytest.raises(ValueError):
        linear_rgba(value)


def test_exposure_is_a_preview_conversion_and_leaves_alpha_untouched():
    value = 'linear(8,4,2,0.25)'
    assert display_hex(value, -3) == '#40FFBC89'
    assert linear_rgba(value) == (8., 4., 2., .25)


def test_gradient_routes_keep_hdr_endpoints_and_float_intermediate_stops():
    from krok_helper.subtitle_render.domain.gradient_path import anchor_route, bake_path
    from krok_helper.subtitle_render.serialization.paint import gradient_path_from_dict, paint_fill_to_dict
    from krok_helper.subtitle_render.domain.paint import PaintFill
    colors = ['linear(10,0.05,0,0.25)', 'linear(0,3,0,0.5)', 'linear(0,0,8,1)']
    route = anchor_route(colors)
    stops = bake_path(route, colors[0], colors[-1])
    assert rgba_float(stops[0][1]) == rgba_float(colors[0])
    assert rgba_float(stops[-1][1]) == rgba_float(colors[-1])
    assert all(linear_rgba(value) is not None for _, value in stops)
    assert any(max(rgba_float(value)[:3]) > 1 for _, value in stops[1:-1])
    payload = paint_fill_to_dict(PaintFill(gradient_path=route))
    assert gradient_path_from_dict(payload['gradient_path']) == route


def test_linear_colors_survive_style_json_roles_and_presets_without_changing_legacy_hex():
    import json
    from krok_helper.subtitle_render.domain.models import Style, SubtitleStyleScheme, style_to_dict, style_from_dict
    from krok_helper.subtitle_render.domain.paint import KaraokeColors, KaraokeColorState, PaintFill
    value = 'linear(10.0,0.045123456789,0.0,0.3456789)'
    palette = KaraokeColors(before=KaraokeColorState(text=PaintFill(color=value)))
    style = Style(karaoke_colors=palette, custom_style_schemes={'角色': SubtitleStyleScheme(karaoke_colors=palette)})
    restored = style_from_dict(json.loads(json.dumps(style_to_dict(style))))
    assert restored.karaoke_colors.before.text.color == value
    assert restored.custom_style_schemes['角色'].karaoke_colors.before.text.color == value
    legacy = style_to_dict(Style())
    assert style_to_dict(style_from_dict(legacy))['base_color'] == legacy['base_color']
