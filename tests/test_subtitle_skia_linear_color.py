"""Explicit linear colors keep intensity through the real product IR and paint shaders."""
from dataclasses import replace

import numpy as np
import pytest

from test_subtitle_skia_resolved import application, binary, project, ir_for
from test_subtitle_skia_outline_ruby import render_skia
from krok_helper.subtitle_render.domain.paint import KaraokeColors, KaraokeColorState, PaintFill
from krok_helper.subtitle_render.domain.color import display_hex, linear_color, rgba_float
from krok_helper.subtitle_render.serialization.paint import paint_fill_to_dict, paint_fill_from_dict


@pytest.fixture
def reference_project(project, tmp_path):
    from fontTools.ttLib import TTFont
    from PyQt6.QtGui import QFontDatabase
    from skia_text_fixture import make_font
    from krok_helper.subtitle_render.engine.text.font_weight import clear_font_weight_cache
    family = 'LinK Linear Reference'
    path = make_font(tmp_path / 'linear-reference.ttf', family=family)
    with TTFont(path, recalcTimestamp=False) as font:
        for cmap in font['cmap'].tables:
            if cmap.isUnicode():
                cmap.cmap.update({ord(c): 'cjk' for c in '国あいうえおアイウエオ'})
                cmap.cmap.update({ord(c): 'A' for c in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ-'})
        font.save(path)
    handle = QFontDatabase.addApplicationFont(str(path))
    assert handle >= 0
    clear_font_weight_cache()
    try:
        yield project[0], replace(project[1], font_family=family, font_family_latin=family,
                                  ruby_font_family=family, ruby_font_family_latin=family)
    finally:
        QFontDatabase.removeApplicationFont(handle)
        clear_font_weight_cache()


def linear_source(project, value, *, glow=False):
    track, style = project
    track = replace(track, lines=[replace(track.lines[0], chars=track.lines[0].chars[:1],
                    display_start_override_ms=0, display_end_override_ms=3000, end_ms=1800)])
    state = KaraokeColorState(text=PaintFill(color=value), shadow=PaintFill(color=value))
    return track, replace(style, karaoke_colors=KaraokeColors(before=state, after=state),
        entry_anim='none', exit_anim='none', karaoke_anim='none', stroke_width_px=0,
        stroke2_enabled=False, decoration_kind='glow' if glow else 'none',
        decoration_kind_before='glow' if glow else 'none', glow_radius_px=8,
        glow_after_radius_px=8, glow_before_radius_px=8)


def test_linear_paint_round_trip_keeps_float_channels_in_all_stops():
    value = 'linear(10.0,0.045123456789,0.0,0.3456789)'
    fill = PaintFill(color=value, start_color=value, end_color=value,
                     gradient_stops=[(0, value), (100, value)], split_stops=[(0, value), (100, value)])
    assert paint_fill_from_dict(paint_fill_to_dict(fill)) == fill


@pytest.mark.parametrize('value', ['linear(0.5,0.25,0.1,1)', 'linear(10,0.045,0,1)'])
def test_linear_paint_uses_correct_srgb_encoding_on_gpu_and_raster(binary, project, value):
    source = linear_source(project, value)
    ir = ir_for(binary, source)
    assert source[1].karaoke_colors.before.text.mode == 'solid'  # Projection never edits authored paint.
    gpu, raster = render_skia(binary, ir, [2200])
    expected = np.array([int(display_hex(value)[i:i+2], 16) for i in (3, 5, 7)])
    for frame in (gpu[0], raster[0]):
        pixels = frame[frame[..., 3] == 255, :3]
        assert len(pixels) > 30
        assert abs(np.median(pixels, axis=0) - expected).max() <= 1
    assert abs(gpu[0].astype(float)-raster[0]).mean() < .1


def test_linear_hex_equivalent_keeps_existing_sdr_paint_appearance(binary, project):
    value = '#FF804020'
    original = render_skia(binary, ir_for(binary, linear_source(project, value)), [2200])
    linear = render_skia(binary, ir_for(binary, linear_source(project, linear_color(rgba_float(value)))), [2200])
    for old, new in zip(original, linear, strict=True):
        assert abs(old[0].astype(float) - new[0]).mean() < .1


def test_hdr_glow_keeps_intensity_before_the_sdr_output_conversion(binary, project):
    frames = []
    for intensity in (1, 10):
        value = linear_color((0., 0., intensity, 1.))
        frames.append(render_skia(binary, ir_for(binary, linear_source(project, value, glow=True)), [2200]))
    for target in (0, 1):
        low, high = frames[0][target][0], frames[1][target][0]
        # Body clips in SDR, but its float light source must still produce a brighter halo.
        halo = (low[..., 3] > 0) & (low[..., 3] < 100)
        assert halo.any()
        low_energy = (low[..., 2].astype(float) * low[..., 3] / 255)[halo].sum()
        high_energy = (high[..., 2].astype(float) * high[..., 3] / 255)[halo].sum()
        assert high_energy > low_energy * 1.5


def test_linear_character_fade_glow_preserves_gpu_raster_parity(binary, project):
    track, style = linear_source(project, 'linear(0,0,10,1)', glow=True)
    style = replace(style, entry_anim='char_fade', entry_lead_ms=600)
    gpu, raster = render_skia(binary, ir_for(binary, (track, style)), [100, 300, 700])
    assert gpu[0].any()
    for a, b in zip(gpu, raster, strict=True):
        aa, bb = a.astype(float), b.astype(float)
        aa[..., :3] *= aa[..., 3:] / 255
        bb[..., :3] *= bb[..., 3:] / 255
        assert abs(aa - bb).mean() < .5


@pytest.mark.parametrize('mode', ['solid', 'gradient_horizontal'])
def test_linear_paints_match_direct2d_sdr_output(binary, project, mode):
    import sys
    if sys.platform != 'win32':
        pytest.skip('Windows Direct2D comparison')
    from test_subtitle_render_gpu_backend import _render_g1_frames
    from krok_helper.subtitle_render.native.backend import NativeRendererProcess
    track, style = linear_source(project, 'linear(0.5,0.25,0.1,1)')
    if mode != 'solid':
        paint = PaintFill(mode=mode, start_color='linear(0,0,0,1)', end_color='linear(1,1,1,1)',
                          gradient_stops=[(0, 'linear(0,0,0,1)'), (100, 'linear(1,1,1,1)')])
        state = KaraokeColorState(text=paint)
        style = replace(style, karaoke_colors=KaraokeColors(before=state, after=state))
    gpu, raster = render_skia(binary, ir_for(binary, (track, style)), [2200])
    with NativeRendererProcess() as process:
        _, raw = _render_g1_frames(process, style, [2200], force_warp=False, track=track,
                                  width=640, height=360, realization_enabled=False)
    ref = np.frombuffer(raw[0], np.uint8).reshape(360, 640, 4)
    interior = (ref[..., 3] == 255) & (gpu[0][..., 3] == 255)
    assert interior.sum() > 30
    for frame in (gpu[0], raster[0]):
        assert abs(ref[interior, :3].astype(float) - frame[interior, :3]).max() <= 3


@pytest.mark.parametrize('effect', ['none', 'utopia', 'fade'])
@pytest.mark.parametrize('lower', [False, True])
def test_linear_roles_ruby_outlines_and_glow_keep_replay_and_gpu_raster_parity(binary, reference_project, effect, lower):
    from krok_helper.subtitle_render.domain.models import SubtitleStyleScheme, RubyAnnotation
    track, style = linear_source(reference_project, 'linear(0.5,0.1,0.2,0.7)', glow=True)
    state = KaraokeColorState(text=PaintFill(color='linear(0.1,0.6,0.3,0.7)'),
                              stroke=PaintFill(color='linear(0.2,0.1,0.5,1)'),
                              stroke2=PaintFill(color='linear(0.4,0.2,0.1,1)'),
                              shadow=PaintFill(color='linear(0.1,0.3,4,1)'))
    palette = KaraokeColors(before=state, after=state)
    line = replace(track.lines[0], chars=[replace(track.lines[0].chars[0], role_label='linear')])
    track = replace(track, lines=[line], rubies=[RubyAnnotation('A', 'f')])
    style = replace(style, karaoke_colors=palette, ruby_karaoke_colors=palette,
                    lower_ruby_enabled=lower,
                    ruby_colors_follow_main=False, stroke_width_px=4, stroke2_enabled=True, stroke2_width_px=3,
                    custom_style_schemes={'linear': SubtitleStyleScheme(karaoke_colors=palette)},
                    entry_anim='fade' if effect == 'fade' else 'none', karaoke_anim='utopia' if effect == 'utopia' else 'none')
    times = [100, 700, 1200, 700]
    ir = ir_for(binary, (track, style))
    if lower:
        group = next(g for g in ir['skia_layout']['lines'][0]['ruby_groups'] if g.get('lower'))
        assert group['lower_glow_gradient']['after']['stops'][0][1] == 'linear(0.1,0.3,4,1)'
    gpu, raster = render_skia(binary, ir, times)
    assert all(frame.any() for frame in gpu)
    assert np.array_equal(gpu[1], gpu[-1]) and np.array_equal(raster[1], raster[-1])
    for a, b in zip(gpu, raster, strict=True):
        aa, bb = a.astype(float), b.astype(float)
        aa[..., :3] *= aa[..., 3:] / 255
        bb[..., :3] *= bb[..., 3:] / 255
        assert abs(aa - bb).mean() < .5
