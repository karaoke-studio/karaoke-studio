"""Test the per-glyph white pulse against the existing GPU and Raster."""
import copy
from dataclasses import replace
import json
import sys

import numpy as np
import pytest

from test_subtitle_skia_line_fade import (
    application, binary, project, ir_for, collect, configure, selection, premultiplied,
)
from test_subtitle_skia_primary_outline import within_one_pixel
from test_subtitle_skia_reverse import source as clock_source
from test_subtitle_skia_secondary_outline import two_outlines
from test_subtitle_skia_outline_ruby import annotated, mixed, pages, reading, upper_ir
from krok_helper.subtitle_render.native.backend import NativeRendererError, NativeRendererProcess
from krok_helper.subtitle_render.native.skia_backend import SkiaRendererProcess
from scripts.subtitle_gpu_baseline.policy import COMPARISON_BUDGETS


def flash_source(project, outlines=False):
    track, style = two_outlines(project) if outlines else clock_source(project)
    return (
        replace(track, lines=[replace(track.lines[0], wipe_reverse=False)]),
        replace(style, karaoke_stroke_flash=True, right_to_left=False),
    )


def white_coverage(frame):
    return premultiplied(frame)[..., :3].min(axis=-1)


@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_flash_follows_each_cell_clock_and_replays(binary, project, target, tmp_path):
    track, style = flash_source(project)
    ir = ir_for(binary, (track, style), dpr=1.5)
    plain = ir_for(binary, (track, replace(style, karaoke_stroke_flash=False)), dpr=1.5)
    source = ir['track']['lines'][0]
    assert 'p07_stroke_flash' in ir['required_features'] and source['stroke_flash'] is True
    assert 'p07_stroke_flash' not in plain['required_features']
    assert source['resolved_intervals'] == plain['track']['lines'][0]['resolved_intervals']
    assert ir['skia_layout']['lines'] == plain['skia_layout']['lines']
    times = [199, 200, 240, 340, 439, 440, 599, 600, 620, 3000, 240]
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, times)
        configure(process, plain)
        bare, _ = collect(process, times, generation=2)
    assert np.array_equal(frames[2], frames[-1])
    for index in (0, 5, 6, 9):
        assert np.array_equal(frames[index], bare[index])
    flash_peak = [white_coverage(frames[i]).max() for i in (1, 2, 3)]
    assert flash_peak[0] > flash_peak[1] > flash_peak[2] > 25
    assert white_coverage(frames[7]).max() > 150
    if target == 'gpu':
        (tmp_path / 'stroke-flash-layout.json').write_text(json.dumps({
            'line': ir['skia_layout']['lines'][0], 'intervals': source['resolved_intervals'],
            'profile': 'p07_stroke_flash',
        }, sort_keys=True), encoding='utf-8')


@pytest.mark.parametrize('mutation', ['feature', 'reverse'])
def test_invalid_flash_ir_preserves_scene(binary, project, mutation):
    ir = ir_for(binary, flash_source(project))
    bad = copy.deepcopy(ir)
    if mutation == 'feature':
        bad['required_features'].remove('p07_stroke_flash')
    else:
        bad['track']['lines'][0]['wipe_reverse'] = True
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        configure(process, ir)
        before, _ = collect(process, [240])
        with pytest.raises(NativeRendererError, match='描边闪光'):
            configure(process, bad)
        after, _ = collect(process, [240], generation=2)
    assert np.array_equal(before, after)


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('outlines,dpr', [(False, 1), (False, 1.5), (True, 1.5)])
def test_flash_matches_actual_d2d_and_gpu_raster(binary, project, outlines, dpr):
    from test_subtitle_render_gpu_backend import _render_g1_frames

    track, style = flash_source(project, outlines)
    source = ir_for(binary, (track, style), dpr=dpr)
    clocks = source['track']['lines'][0]['resolved_intervals']
    first, second = clocks[0][0], clocks[1][0]
    times = sorted({first + offset for offset in (-1, 0, 10, 30, 90, 155, 230, 239, 240)}
                   | {second, second + 47})
    with NativeRendererProcess() as process:
        _, raw = _render_g1_frames(process, style, tuple(times), force_warp=False, track=track,
                                   width=640, height=360, dpr=dpr, realization_enabled=False)
    shape = (round(360*dpr), round(640*dpr), 4)
    reference = [np.frombuffer(p, np.uint8).reshape(shape) for p in raw]
    frames = []
    for target in ('gpu', 'raster'):
        with SkiaRendererProcess(binary, selection=selection(target)) as process:
            configure(process, source)
            images, _ = collect(process, times)
        frames.append(images)
    rules = COMPARISON_BUDGETS['image_categories']['decorated_text']
    for t, d2d, gpu, raster in zip(times, reference, *frames, strict=True):
        for label, a, b in (('d2d', d2d, gpu), ('raster', gpu, raster)):
            sample = (outlines, dpr, t, label)
            aw, bw = white_coverage(a), white_coverage(b)
            ma, mb = aw > 24, bw > 24
            assert within_one_pixel(ma, mb), (sample, 'stroke')
            mass = abs(aw.sum() - bw.sum())
            if 0 < max(aw.max(), bw.max()) <= 10:
                assert abs(aw - bw).max() <= 2 and mass <= 255, (sample, 'dim tail')
            else:
                limit = .05 if label == 'd2d' and not outlines and t in {210, 355, 647} else rules['max_alpha_sum_relative']
                assert mass <= limit * max(aw.sum(), bw.sum()), (sample, 'flash coverage', mass)
            alpha_a, alpha_b = (frame[..., 3].sum(dtype=float) for frame in (a, b))
            assert abs(alpha_a - alpha_b) <= rules['max_alpha_sum_relative'] * max(alpha_a, alpha_b), sample
            union = (a[..., 3] > 16) | (b[..., 3] > 16)
            if union.any():
                delta = abs(premultiplied(a)[union] - premultiplied(b)[union])
                assert delta.mean() <= rules['max_union_channel_mae'], (sample, 'paint', delta.mean())
                assert np.percentile(delta, 95) <= rules['max_union_p95'], (sample, 'paint P95')


@pytest.mark.parametrize('target', ['gpu', 'raster'])
@pytest.mark.parametrize('animation', ['utopia', 'zoom_pulse'])
def test_composed_flash_preserves_ruby_clocks_and_replays(binary, reading, target, animation, tmp_path):
    from test_subtitle_skia_static_glow import glowing_ruby

    track, style = glowing_ruby(reading, 'inherited')
    style = replace(style, karaoke_anim=animation, karaoke_stroke_flash=True)
    ir = upper_ir(binary, (track, style), 1)
    plain = upper_ir(binary, (track, replace(style, karaoke_stroke_flash=False)), 1)
    zoom = animation == 'zoom_pulse'
    assert 'p07_composed_stroke_flash' in ir['required_features']
    assert ('p07_zoom_pulse_decoration' in ir['required_features']) == zoom
    assert ir['skia_layout']['lines'] == plain['skia_layout']['lines']
    assert ir['track']['lines'][0]['resolved_intervals'] == plain['track']['lines'][0]['resolved_intervals']
    bad = copy.deepcopy(ir)
    bad['required_features'].remove('p07_zoom_pulse_decoration' if zoom else 'p07_composed_stroke_flash')
    times = [299, 300, 340, 540, 340]
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, times)
        with pytest.raises(NativeRendererError, match='描边闪光'):
            configure(process, bad)
        replay, _ = collect(process, [340], generation=2)
        configure(process, plain)
        bare, _ = collect(process, times, generation=3)
    assert np.array_equal(frames[2], frames[4]) and np.array_equal(frames[2], replay[0])
    assert [np.array_equal(a, b) for a, b in zip(frames, bare, strict=True)] == [True, False, False, True, False]
    if target == 'gpu':
        name = 'zoom-stroke-flash-layout.json' if zoom else 'composed-stroke-flash-layout.json'
        (tmp_path / name).write_text(json.dumps({
            'features': ir['required_features'], 'line': ir['skia_layout']['lines'][0],
        }, sort_keys=True), encoding='utf-8')


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('case_id', ['karaoke_inherit', 'karaoke_none', 'karaoke_utopia', 'karaoke_zoom_pulse'])
def test_p00_composed_flash_matches_actual_gpu(binary, application, case_id, tmp_path):
    from scripts.subtitle_gpu_baseline.cases import build_cases
    from test_subtitle_render_gpu_backend import _render_g1_frames
    from test_subtitle_skia_static_glow import assert_glow_parity

    case = next(case for case in build_cases(tmp_path / 'assets') if case.case_id == case_id)
    with NativeRendererProcess() as process:
        _, raw = _render_g1_frames(process, case.style, case.timestamps_ms, force_warp=False,
                                   track=case.track, width=1920, height=1080, dpr=1, realization_enabled=False)
    refs = [np.frombuffer(p, np.uint8).reshape(1080, 1920, 4) for p in raw]
    rendered = []
    for target in ('gpu', 'raster'):
        with SkiaRendererProcess(binary, selection=selection(target)) as process:
            process.configure(case.track, case.style, width=1920, height=1080, fps=60)
            frames, _ = collect(process, case.timestamps_ms)
        rendered.append(frames)
    assert_glow_parity(case.timestamps_ms, refs, *rendered, case_id, check_p00_edges=True)
