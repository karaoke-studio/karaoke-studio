"""Check shared falling particles before and after the authored text window."""
import copy
from dataclasses import replace
import json
import sys

import numpy as np
import pytest

from test_subtitle_skia_line_fade import (
    application, binary, project, ir_for, collect, configure, selection,
)
from test_subtitle_skia_reverse import source as clock_source
from test_subtitle_skia_sing_particles import assert_particle_region, assert_thin_particle_contours, blue_coverage
from krok_helper.subtitle_render.native.backend import NativeRendererError, NativeRendererProcess
from krok_helper.subtitle_render.native.skia_backend import SkiaRendererProcess


def falling_source(project, kind, speed=0):
    track, style = clock_source(project)
    line = replace(track.lines[0], wipe_reverse=False, end_ms=2850,
                   chars=[replace(ch, start_ms=t) for ch, t in zip(
                       track.lines[0].chars, (1650, 2000, 2430, 2650), strict=True)],
                   display_start_override_ms=900, display_end_override_ms=3050)
    return replace(track, lines=[line]), replace(
        style, entry_anim=kind, exit_anim=kind, entry_lead_ms=600, exit_fade_ms=600,
        anim_fx_color_mode='color', anim_fx_color='#0000FF', anim_fx_color2='#0000FF',
        anim_fx_speed_pct=speed,
    )


def window_samples(ir):
    source = ir['track']['lines'][0]
    start, end = source['display_start_ms'], source['display_end_ms']
    bursts = source['fx_bursts']
    first, last = min(b['start_ms'] for b in bursts), max(b['end_ms'] for b in bursts)
    assert first < start < end < last
    return sorted({first-1, start-125, start-65, start-1, start, start+30, start+200,
                   start+450, 2100, end-150, end-1, end, end+65, end+130,
                   max((end+last)//2, end+1), last-1, last})


@pytest.mark.parametrize('kind', ['petal', 'feather', 'snow', 'snow_solid'])
@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_falling_transitions_keep_prelude_and_tail_outside_text(binary, project, kind, target, tmp_path):
    track, style = falling_source(project, kind, -150)
    ir = ir_for(binary, (track, style), dpr=1.5)
    source = ir['track']['lines'][0]
    assert ir['skia_layout']['character_transform_schema'] == 6
    assert ir['skia_layout']['sing_twinkle_schema'] == 9
    assert {b['sweep'] for b in source['fx_bursts']} == {-1, 1}
    assert all(b['kind'] == kind and b['front'] and b['time_scale'] > 1 for b in source['fx_bursts'])
    times = window_samples(ir)
    start, end = source['display_start_ms'], source['display_end_ms']
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, [*times, start-1, end+65])
    assert np.array_equal(frames[times.index(start-1)], frames[-2])
    assert np.array_equal(frames[times.index(end+65)], frames[-1])
    assert not frames[0][..., 3].any() and not frames[-3][..., 3].any()
    prelude = [frame for t, frame in zip(times, frames[:-2], strict=True) if t < start]
    tail = [frame for t, frame in zip(times, frames[:-2], strict=True) if t >= end]
    for window in (prelude, tail):
        assert any(blue_coverage(f).max() > 20 for f in window)
        assert not any(f[..., :2].any() for f in window)
    assert frames[times.index(2100)][..., :2].any()
    if target == 'gpu' and kind == 'snow':
        (tmp_path / 'falling-transition-layout.json').write_text(json.dumps({
            'features': ir['required_features'], 'line': ir['skia_layout']['lines'][0],
            'bursts': source['fx_bursts'], 'display': [start, end],
        }, sort_keys=True), encoding='utf-8')


@pytest.mark.parametrize('mutation', ['clock', 'profile'])
def test_invalid_falling_transition_keeps_scene(binary, project, mutation):
    ir = ir_for(binary, falling_source(project, 'petal'))
    bad = copy.deepcopy(ir)
    if mutation == 'profile':
        bad['skia_layout']['sing_twinkle_schema'] = 8
    else:
        bad['track']['lines'][0]['fx_bursts'][0]['sweep'] = 0
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        configure(process, ir)
        before, _ = collect(process, [850])
        with pytest.raises(NativeRendererError):
            configure(process, bad)
        after, _ = collect(process, [850], generation=2)
    assert np.array_equal(before, after)


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('kind,speed', [('petal', 0), ('feather', 0), ('snow', 0), ('snow_solid', -150)])
def test_falling_transitions_match_actual_d2d_and_gpu_raster(binary, project, kind, speed):
    from test_subtitle_render_gpu_backend import _render_g1_frames

    track, style = falling_source(project, kind, speed)
    ir = ir_for(binary, (track, style), dpr=1.5)
    times = window_samples(ir)
    with NativeRendererProcess() as process:
        _, raw = _render_g1_frames(process, style, tuple(times), force_warp=False, track=track,
                                   width=640, height=360, dpr=1.5, realization_enabled=False)
    reference = [np.frombuffer(p, np.uint8).reshape(540, 960, 4) for p in raw]
    results = []
    for target in ('gpu', 'raster'):
        with SkiaRendererProcess(binary, selection=selection(target)) as process:
            configure(process, ir)
            frames, _ = collect(process, times)
        results.append(frames)
    source = ir['track']['lines'][0]
    assert any(blue_coverage(ref).max() > 20 for t, ref in zip(times, reference, strict=True)
               if t < source['display_start_ms'])
    assert any(blue_coverage(ref).max() > 20 for t, ref in zip(times, reference, strict=True)
               if t >= source['display_end_ms'])
    for t, d2d, gpu, raster in zip(times, reference, *results, strict=True):
        sample = ('falling_transition', kind, t)
        if kind == 'snow' or (kind, t) == ('feather', 930):
            # Thin rotated branches and a single feather tip cross the
            # occupancy threshold. Preserve raw mass and compare local coverage.
            assert_thin_particle_contours(d2d, gpu, sample, coverage_epsilon=4 if kind == 'snow' else 2)
        else:
            assert_particle_region(d2d, gpu, sample, check_contour=False)
        assert_particle_region(gpu, raster, ('falling_transition', kind, t, 'raster'), check_contour=False)
