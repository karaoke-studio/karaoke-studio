"""Body gradients use the authored before/after fills and one line-wide domain."""
from dataclasses import replace
import copy
import json
import sys

import numpy as np
import pytest

from test_subtitle_skia_line_fade import (
    application, binary, project, ir_for, collect, configure, selection, premultiplied,
)
from test_subtitle_skia_primary_outline import within_one_pixel
from test_subtitle_skia_reverse import source as clock_source
from krok_helper.subtitle_render.domain.paint import PaintFill
from krok_helper.subtitle_render.native.backend import NativeRendererError, NativeRendererProcess
from krok_helper.subtitle_render.native.skia_backend import SkiaRendererProcess
from scripts.subtitle_gpu_baseline.policy import COMPARISON_BUDGETS


MODES = ('gradient_horizontal', 'gradient_vertical', 'split_vertical')
TIMES = [0, 200, 400, 750, 1100, 1550, 1800, 2200, 3000]


def gradient_source(project, mode, translucent=False):
    track, style = clock_source(project, 'plain')
    track = replace(track, lines=[replace(track.lines[0], wipe_reverse=False)])
    before, after = style.karaoke_colors.before, style.karaoke_colors.after
    colors = replace(
        style.karaoke_colors,
        before=replace(before, text=PaintFill(mode=mode, start_color='#E04418', end_color='#179BCA',
                                             split_stops=[(0, '#E04418'), (50, '#179BCA')])),
        after=replace(after, text=PaintFill(mode=mode, start_color='#28CB53', end_color='#BB37AF',
                                           split_stops=[(0, '#28CB53'), (50, '#BB37AF')])),
    )
    if translucent:
        colors = replace(
            colors,
            before=replace(colors.before, text=replace(colors.before.text, start_color='#80E04418', end_color='#E0179BCA')),
            after=replace(colors.after, text=replace(colors.after.text, start_color='#6028CB53', end_color='#C0BB37AF')),
        )
    return track, replace(style, karaoke_colors=colors, right_to_left=False)


@pytest.mark.parametrize('mode', MODES)
@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_gradient_uses_one_domain_and_replays(binary, project, mode, target, tmp_path):
    ir = ir_for(binary, gradient_source(project, mode), dpr=1.5)
    assert 'p06_body_gradient' in ir['required_features']
    bounds = ir['skia_layout']['lines'][0]['body_gradient_bounds']
    assert bounds[0] < bounds[2] and bounds[1] < bounds[3]
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, [*TIMES, 750])
    assert np.array_equal(frames[3], frames[-1])
    before, after = frames[0], frames[TIMES.index(2200)]
    assert not frames[-2][..., 3].any()
    for frame in (before, after):
        y, x = np.where(frame[..., 3] == 255)
        assert x.size > 50
        if mode == 'gradient_horizontal':
            midpoint = (x.max() + x.min()) / 2
            axis = x
        else:
            midpoint = (y.max() + y.min()) / 2
            axis = y
        left, right = frame[y[axis < midpoint], x[axis < midpoint], :3], frame[y[axis > midpoint], x[axis > midpoint], :3]
        assert abs(left.mean(axis=0) - right.mean(axis=0)).max() > 35
    assert not np.array_equal(before, after)
    if mode == 'gradient_horizontal' and target == 'gpu':
        (tmp_path / 'body-gradient-layout.json').write_text(json.dumps({
            'features': ir['required_features'], 'line': ir['skia_layout']['lines'][0],
        }, sort_keys=True), encoding='utf-8')


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('mode,translucent', [*((mode, False) for mode in MODES),
                                               ('gradient_horizontal', True), ('gradient_vertical', True)])
def test_gradient_matches_actual_d2d_and_gpu_raster(binary, project, mode, translucent):
    from test_subtitle_render_gpu_backend import _render_g1_frames

    track, style = gradient_source(project, mode, translucent)
    with NativeRendererProcess() as process:
        _, raw = _render_g1_frames(process, style, tuple(TIMES), force_warp=False, track=track,
                                   width=640, height=360, dpr=1.5, realization_enabled=False)
    refs = [np.frombuffer(p, np.uint8).reshape(540, 960, 4) for p in raw]
    ir = ir_for(binary, (track, style), dpr=1.5)
    results = []
    for target in ('gpu', 'raster'):
        with SkiaRendererProcess(binary, selection=selection(target)) as process:
            configure(process, ir)
            frames, _ = collect(process, TIMES)
        results.append(frames)
    budget = COMPARISON_BUDGETS['image_categories']['text']
    for t, d2d, gpu, raster in zip(TIMES, refs, *results, strict=True):
        for name, a, b in (('d2d', d2d, gpu), ('raster', gpu, raster)):
            ma, mb = a[..., 3] > 16, b[..., 3] > 16
            union = ma | mb
            if not union.any():
                continue
            assert ma.any() and mb.any(), (mode, name, t)
            assert within_one_pixel(ma, mb), (mode, name, t, 'ink')
            assert (ma & mb).sum() / union.sum() >= budget['min_alpha_iou'], (mode, name, t, 'mask')
            diff = abs(premultiplied(a)[union] - premultiplied(b)[union])
            assert diff.mean() <= budget['max_union_channel_mae'], (mode, name, t, diff.mean())
            assert np.percentile(diff, 95) <= budget['max_union_p95'], (mode, name, t, np.percentile(diff, 95))


@pytest.mark.parametrize('mutation', ['feature', 'bounds'])
def test_invalid_gradient_keeps_previous_scene(binary, project, mutation):
    ir = ir_for(binary, gradient_source(project, 'gradient_horizontal'))
    bad = copy.deepcopy(ir)
    if mutation == 'feature':
        bad['required_features'].remove('p06_body_gradient')
    else:
        bad['skia_layout']['lines'][0]['body_gradient_bounds'] = [1, 2, 0, 3]
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        configure(process, ir)
        before, _ = collect(process, [750])
        with pytest.raises(NativeRendererError):
            configure(process, bad)
        after, _ = collect(process, [750], generation=2)
    assert np.array_equal(before, after)
