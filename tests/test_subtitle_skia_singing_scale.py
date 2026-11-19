"""Singing-only Utopia and zoom pulse use canonical cell pivots and clocks."""

from dataclasses import replace
import copy
import json
import sys

import numpy as np
import pytest

from test_subtitle_skia_line_fade import application, binary, project, ir_for, collect, configure, selection, premultiplied
from test_subtitle_skia_reverse import source as clock_source
from test_subtitle_skia_secondary_outline import two_outlines
from test_subtitle_skia_primary_outline import within_one_pixel
from krok_helper.subtitle_render.native.backend import NativeRendererError, NativeRendererProcess
from krok_helper.subtitle_render.native.skia_backend import SkiaRendererProcess
from scripts.subtitle_gpu_baseline.policy import COMPARISON_BUDGETS


TIMES = [0, 199, 200, 201, 275, 300, 450, 599, 600, 601, 750, 900, 1100, 1400, 1799, 1800, 1900, 2099, 2100, 2200]
CASES = [('utopia', False, 125, 1), ('utopia', True, 125, 1),
         ('zoom_pulse', False, 200, 0), ('zoom_pulse', True, 150, 5)]


def singing_source(project, effect, outlined, peak, curve):
    track, style = two_outlines(project) if outlined else clock_source(project)
    track = replace(track, lines=[replace(track.lines[0], wipe_reverse=False)])
    return track, replace(style, karaoke_anim=effect, zoom_pulse_curve_level=curve, zoom_pulse_peak_pct=peak)


@pytest.mark.parametrize('target', ['gpu', 'raster'])
@pytest.mark.parametrize('effect,outlined,peak,curve', CASES)
def test_singing_scale_preserves_clocks_and_replays(binary, project, target, effect, outlined, peak, curve, tmp_path):
    track, style = singing_source(project, effect, outlined, peak, curve)
    ir = ir_for(binary, (track, style), dpr=1.5)
    plain = ir_for(binary, (track, replace(style, karaoke_anim='none')), dpr=1.5)
    assert 'p07_singing_scale' in ir['required_features']
    assert ir['skia_layout']['singing_scale_schema'] == 1
    assert ir['track']['lines'][0]['resolved_intervals'] == plain['track']['lines'][0]['resolved_intervals']
    for animated, still in zip(ir['skia_layout']['lines'][0]['cells'], plain['skia_layout']['lines'][0]['cells'], strict=True):
        assert {k: v for k, v in animated.items() if k not in ('layout_left', 'layout_right', 'pivot_y')} == still
    times = [0, 275, 1900, 2200, 275]
    bad = copy.deepcopy(ir)
    bad['required_features'].remove('p07_singing_scale')
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, times)
        with pytest.raises(NativeRendererError):
            configure(process, bad)
        previous, _ = collect(process, [275], generation=2)
        configure(process, plain)
        still, _ = collect(process, times[:-1], generation=3)
    assert np.array_equal(frames[1], frames[-1]) and np.array_equal(frames[1], previous[0])
    assert np.array_equal(frames[0], still[0]) and np.array_equal(frames[3], still[3])
    assert not np.array_equal(frames[1], still[1])
    if effect == 'zoom_pulse' and not outlined:
        assert not np.array_equal(frames[2], still[2])
    if effect == 'zoom_pulse' and target == 'gpu' and outlined:
        (tmp_path / 'singing-scale-layout.json').write_text(json.dumps({
            'features': ir['required_features'], 'line': ir['skia_layout']['lines'][0],
        }, sort_keys=True), encoding='utf-8')


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('dpr', [1, 1.5])
@pytest.mark.parametrize('effect,outlined,peak,curve', CASES)
def test_singing_scale_matches_actual_d2d_and_gpu_raster(binary, project, dpr, effect, outlined, peak, curve):
    from test_subtitle_render_gpu_backend import _render_g1_frames

    track, style = singing_source(project, effect, outlined, peak, curve)
    with NativeRendererProcess() as process:
        _, raw = _render_g1_frames(process, style, tuple(TIMES), force_warp=False, track=track,
                                   width=640, height=360, dpr=dpr, realization_enabled=False)
    refs = [np.frombuffer(p, np.uint8).reshape(round(360*dpr), round(640*dpr), 4) for p in raw]
    ir = ir_for(binary, (track, style), dpr=dpr)
    frames = []
    for target in ('gpu', 'raster'):
        with SkiaRendererProcess(binary, selection=selection(target)) as process:
            configure(process, ir)
            result, _ = collect(process, TIMES)
        frames.append(result)
    budget = COMPARISON_BUDGETS['image_categories']['text']
    for t, ref, gpu, raster in zip(TIMES, refs, *frames, strict=True):
        for label, a, b in (('D2D', ref, gpu), ('Raster', gpu, raster)):
            left, right = a[..., 3] > 16, b[..., 3] > 16
            union = left | right
            assert left.any() == right.any(), (effect, outlined, dpr, label, t)
            if not union.any():
                continue
            assert within_one_pixel(left, right), (effect, outlined, dpr, label, t, 'ink')
            assert (left & right).sum() / union.sum() >= budget['min_alpha_iou'], (effect, outlined, dpr, label, t, 'mask')
            delta = abs(premultiplied(a)[union] - premultiplied(b)[union])
            assert delta.mean() <= budget['max_union_channel_mae'], (effect, outlined, dpr, label, t, delta.mean())
            assert np.percentile(delta, 95) <= budget['max_union_p95'], (effect, outlined, dpr, label, t)
