"""The noise transition composites the existing tiled mask and streaks."""
from dataclasses import replace
import copy
import json
import sys

import numpy as np
import pytest

from test_subtitle_skia_line_fade import (
    application, binary, project, ir_for, collect, configure, selection, premultiplied,
)
from test_subtitle_skia_character_transform import transform_source
from test_subtitle_skia_lower_geometry import prepared_source, source as lower_source
from test_subtitle_skia_outline_ruby import annotated, mixed, pages, reading, upper_ir
from krok_helper.subtitle_render.domain.timing import LineAnimationOverride
from krok_helper.subtitle_render.native.backend import NativeRendererError, NativeRendererProcess
from krok_helper.subtitle_render.native.skia_backend import SkiaRendererProcess


TIMES = [0, 5, 40, 90, 159, 300, 480, 595, 600, 1100, 2399, 2400,
         2490, 2650, 2780, 2920, 2995, 3000]


@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_noise_transitions_preserve_final_text_and_are_seekable(binary, project, target, tmp_path):
    track, style = transform_source(project, 'noise', 'noise')
    ir = ir_for(binary, (track, style), dpr=1.5)
    plain = ir_for(binary, (track, replace(style, entry_anim='none', exit_anim='none')), dpr=1.5)
    assert 'p07_noise_transition' in ir['required_features']
    assert ir['skia_layout']['lines'] == plain['skia_layout']['lines']
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, [*TIMES, 159, 2780])
        configure(process, plain)
        steady, _ = collect(process, TIMES, generation=2)
    by_time = dict(zip(TIMES, frames[:len(TIMES)], strict=True))
    assert np.array_equal(by_time[159], frames[-2])
    assert np.array_equal(by_time[2780], frames[-1])
    assert all(not by_time[t][..., 3].any() for t in (0, 3000))
    assert all(np.array_equal(by_time[t], steady[TIMES.index(t)]) for t in (600, 1100, 2399, 2400))
    for t in (159, 2780):
        current = by_time[t][..., 3]
        full = steady[TIMES.index(t)][..., 3]
        assert ((current == 0) & (full == 255)).sum() > 30
        assert ((current > 0) & (full == 0)).sum() > 30
    if target == 'gpu':
        (tmp_path / 'noise-transition-layout.json').write_text(json.dumps({
            'line': ir['skia_layout']['lines'][0],
            'intervals': ir['track']['lines'][0]['resolved_intervals'],
            'features': ir['required_features'],
        }, sort_keys=True), encoding='utf-8')


@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_noise_with_glowing_ruby_preserves_wipe_and_previous_scene(binary, reading, target, tmp_path):
    from test_subtitle_skia_static_glow import glowing_ruby

    track, style = glowing_ruby(reading, 'inherited')
    style = replace(style, karaoke_anim='utopia')
    noise = upper_ir(binary, (track, replace(style, entry_anim='noise', entry_lead_ms=600)), 1.5)
    still = upper_ir(binary, (track, style), 1.5)
    assert 'p07_noise_composition' in noise['required_features']
    assert noise['skia_layout']['lines'] == still['skia_layout']['lines']
    bad = copy.deepcopy(noise)
    bad['required_features'].remove('p07_noise_composition')
    times = [40, 159, 650, 1500]
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, noise)
        frames, _ = collect(process, [*times, 159])
        replay, _ = collect(process, [159], generation=2)
        configure(process, still)
        steady, _ = collect(process, times, generation=3)
    assert np.array_equal(frames[1], frames[-1]) and np.array_equal(frames[1], replay[0])
    assert not np.array_equal(frames[1], steady[1])
    assert all(np.array_equal(a, b) for a, b in zip(frames[2:-1], steady[2:], strict=True))
    if target == 'gpu':
        (tmp_path / 'ruby-noise-layout.json').write_text(json.dumps({
            'features': noise['required_features'], 'line': noise['skia_layout']['lines'][0],
        }, sort_keys=True), encoding='utf-8')


def test_noise_and_lower_reading_stay_on_their_own_rows(binary, lower_source):
    from test_subtitle_skia_lower_product import ir_for as lower_ir
    from test_subtitle_skia_outline_ruby import render_skia
    from test_subtitle_skia_static_glow import assert_glow_parity

    track, style, resolve = lower_source
    first, = track.lines
    shift = 4000
    animated = replace(first, track_line_index=1, break_before='page',
                       chars=[replace(char, text='字', start_ms=char.start_ms + shift) for char in first.chars],
                       end_ms=first.end_ms + shift, display_start_override_ms=shift - 600,
                       display_end_override_ms=first.end_ms + shift + 600,
                       animation_override=LineAnimationOverride(entry_anim='noise', entry_duration_ms=600,
                                                               karaoke_anim='utopia'))
    track = replace(track, lines=[first, animated])
    ir = lower_ir(binary, (track, style, resolve))
    assert 'p07_noise_composition' in ir['required_features']
    assert any(group.get('lower') for group in ir['skia_layout']['lines'][0]['ruby_groups'])
    assert not ir['skia_layout']['lines'][1]['ruby_groups']
    times = (500, 1000, 3560, 3850, 4200, 4700)
    reference = None
    if sys.platform == 'win32':
        from test_subtitle_render_gpu_backend import _render_g1_frames

        with NativeRendererProcess() as process:
            _, raw = _render_g1_frames(process, style, times, force_warp=False, track=track,
                                       width=900, height=600, realization_enabled=False)
        reference = [np.frombuffer(frame, np.uint8).reshape(600, 900, 4) for frame in raw]
    gpu, raster = render_skia(binary, ir, times)
    assert 0 < gpu[2][..., 3].sum() < gpu[4][..., 3].sum()
    assert_glow_parity(times, reference, gpu, raster, 'separate_noise_and_lower_reading')


def test_noise_entry_includes_lower_reading(binary, lower_source):
    from test_subtitle_skia_lower_product import ir_for as lower_ir
    from test_subtitle_skia_outline_ruby import render_skia
    from test_subtitle_skia_static_glow import assert_glow_parity

    track, style, resolve = lower_source
    style = replace(style, entry_anim='noise', entry_lead_ms=200)
    ir = lower_ir(binary, (track, style, resolve))
    assert {'p07_noise_composition', 'p05_lower_ruby'} <= set(ir['required_features'])
    times = (45, 115, 200, 450, 750)
    gpu, raster = render_skia(binary, ir, times)
    bare, _ = render_skia(binary, lower_ir(binary, (track, replace(style, lower_ruby_enabled=False), resolve)), times)
    assert any(np.count_nonzero(a != b) > 40 for a, b in zip(gpu[:2], bare[:2], strict=True))
    reference = None
    if sys.platform == 'win32':
        from test_subtitle_render_gpu_backend import _render_g1_frames

        with NativeRendererProcess() as process:
            _, raw = _render_g1_frames(process, style, times, force_warp=False, track=track,
                                       width=900, height=600, realization_enabled=False)
        reference = [np.frombuffer(frame, np.uint8).reshape(600, 900, 4) for frame in raw]
    assert_glow_parity(times, reference, gpu, raster, 'noise_entry_with_lower_reading', check_p00_edges=True)


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('dpr,content', [(1, 'plain'), (1.5, 'plain'), (1, 'guide'), (1, 'role'), (1, 'signal')])
def test_noise_matches_actual_d2d_and_gpu_raster(binary, project, dpr, content):
    from krok_helper.subtitle_render.domain.models import SubtitleStyleScheme
    from test_subtitle_render_gpu_backend import _render_g1_frames
    from test_subtitle_skia_roles import colors
    from test_subtitle_skia_vector_guides import SQUARE

    track, style = transform_source(project, 'noise', 'noise')
    line, = track.lines
    if content == 'guide':
        line = replace(line, inline_guide_symbols={1: SQUARE})
    elif content == 'role':
        chars = [line.chars[0], replace(line.chars[1], role_label='lead'), *line.chars[2:]]
        line = replace(line, chars=chars)
        style = replace(style, custom_style_schemes={'lead': SubtitleStyleScheme(
            font_family=style.font_family, font_size_px=60, karaoke_colors=colors('#FF00FF', '#00BBFF'))})
    elif content == 'signal':
        style = replace(style, lit_enabled=True, lit_appearance_mode='custom', lit_style='square', lit_number=1,
                        lit_size=24, lit_offset_y=-35, lit_fill_color='#FF00FF',
                        lit_stroke_width=0, lit_edge_brightness_pct=0, lit_shadow=False)
    track = replace(track, lines=[line])
    times = TIMES if content == 'plain' else [0, 159, 650, 2650, 3000]
    with NativeRendererProcess() as process:
        _, raw = _render_g1_frames(process, style, tuple(times), force_warp=False, track=track,
                                   width=640, height=360, dpr=dpr, realization_enabled=False)
    shape = (round(360*dpr), round(640*dpr), 4)
    reference = [np.frombuffer(p, np.uint8).reshape(shape) for p in raw]
    if content == 'signal':
        steady = reference[times.index(650)]
        assert np.count_nonzero(np.all(steady[..., :3] == [255, 0, 255], axis=-1) & (steady[..., 3] > 250)) > 400
    ir = ir_for(binary, (track, style), dpr=dpr)
    results = []
    for target in ('gpu', 'raster'):
        with SkiaRendererProcess(binary, selection=selection(target)) as process:
            configure(process, ir)
            frames, _ = collect(process, times)
        results.append(frames)
    for t, d2d, gpu, raster in zip(times, reference, *results, strict=True):
        for name, a, b in (('d2d', d2d, gpu), ('raster', gpu, raster)):
            pa, pb = premultiplied(a), premultiplied(b)
            assert (pa[..., 3].any() == pb[..., 3].any()), (t, name, 'visibility')
            assert abs(pa[..., 3].sum() - pb[..., 3].sum()) <= max(pa[..., 3].sum(), pb[..., 3].sum()) * .03, (
                t, name, 'coverage')
            union = (pa[..., 3] > 16) | (pb[..., 3] > 16)
            if union.any():
                diff = abs(pa[union] - pb[union])
                assert diff.mean() <= 5 and np.percentile(diff, 95) <= 24, (t, name, diff.mean())


def noise_zoom_reading(reading):
    track, style = reading
    return track, replace(style, karaoke_anim='zoom_pulse', zoom_pulse_peak_pct=150,
                          entry_anim='noise', entry_lead_ms=600, exit_anim='noise', exit_fade_ms=600)


def test_noise_zoom_with_upper_reading_requires_capability(binary, reading):
    ir = ir_for(binary, noise_zoom_reading(reading))
    forged = copy.deepcopy(ir)
    forged['required_features'].remove('p07_ruby_zoom_noise')
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        configure(process, ir)
        before, _ = collect(process, [450, 1500])
        after, _ = collect(process, [1500, 450], generation=2)
    assert all(frame.any() for frame in before) and np.array_equal(before, after[::-1])


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
def test_noise_zoom_with_upper_reading_matches_actual_d2d_gpu(binary, reading):
    from test_subtitle_render_gpu_backend import _render_g1_frames
    from test_subtitle_skia_outline_ruby import render_skia
    from test_subtitle_skia_static_glow import assert_glow_parity

    track, style = noise_zoom_reading(reading)
    times, dpr = (90, 159, 325, 450, 550, 650, 1500, 4600, 4850), 3
    refs = []
    for look in (style, replace(style, entry_anim='none', exit_anim='none'), replace(style, karaoke_anim='utopia')):
        with NativeRendererProcess() as process:
            _, raw = _render_g1_frames(process, look, times, force_warp=False, track=track,
                                       width=640, height=360, dpr=dpr, realization_enabled=False)
        refs.append([np.frombuffer(frame, np.uint8).reshape(360*dpr, 640*dpr, 4) for frame in raw])
    reference, still, utopia = refs
    assert any(np.count_nonzero(a != b) > 100 for a, b in zip(reference, still, strict=True))
    assert any(np.count_nonzero(a != b) > 100 for a, b in zip(reference[2:5], utopia[2:5], strict=True))
    ir = ir_for(binary, (track, style), dpr=dpr)
    assert 'p07_ruby_zoom_noise' in ir['required_features']
    gpu, raster = render_skia(binary, ir, times)
    assert_glow_parity(times, reference, gpu, raster, 'noise_zoom_upper_reading')
