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
from test_subtitle_skia_outline_ruby import annotated, mixed, pages, reading, upper_ir
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
        with pytest.raises(NativeRendererError):
            configure(process, bad)
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


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('dpr', [1, 1.5])
def test_noise_matches_actual_d2d_and_gpu_raster(binary, project, dpr):
    from test_subtitle_render_gpu_backend import _render_g1_frames

    track, style = transform_source(project, 'noise', 'noise')
    with NativeRendererProcess() as process:
        _, raw = _render_g1_frames(process, style, tuple(TIMES), force_warp=False, track=track,
                                   width=640, height=360, dpr=dpr, realization_enabled=False)
    shape = (round(360*dpr), round(640*dpr), 4)
    reference = [np.frombuffer(p, np.uint8).reshape(shape) for p in raw]
    ir = ir_for(binary, (track, style), dpr=dpr)
    results = []
    for target in ('gpu', 'raster'):
        with SkiaRendererProcess(binary, selection=selection(target)) as process:
            configure(process, ir)
            frames, _ = collect(process, TIMES)
        results.append(frames)
    for t, d2d, gpu, raster in zip(TIMES, reference, *results, strict=True):
        for name, a, b in (('d2d', d2d, gpu), ('raster', gpu, raster)):
            pa, pb = premultiplied(a), premultiplied(b)
            assert (pa[..., 3].any() == pb[..., 3].any()), (t, name, 'visibility')
            assert abs(pa[..., 3].sum() - pb[..., 3].sum()) <= max(pa[..., 3].sum(), pb[..., 3].sum()) * .03, (
                t, name, 'coverage')
            union = (pa[..., 3] > 16) | (pb[..., 3] > 16)
            if union.any():
                diff = abs(pa[union] - pb[union])
                assert diff.mean() <= 5 and np.percentile(diff, 95) <= 24, (t, name, diff.mean())
