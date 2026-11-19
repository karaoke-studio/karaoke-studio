"""Timed line movement and opacity against Direct2D."""
from dataclasses import replace
import copy
import json
import sys

import numpy as np
import pytest

from test_subtitle_skia_line_fade import (
    application, binary, project, fade_project, ir_for, collect, configure, selection,
    premultiplied,
)
from krok_helper.subtitle_render.native.backend import NativeRendererError, NativeRendererProcess
from krok_helper.subtitle_render.native.skia_backend import SkiaRendererProcess


def motion_project(project, entry='slide_in', exit='slide_out', *, two_lanes=False, font_size=50):
    track, style = fade_project(project)
    style = replace(style, entry_anim=entry, exit_anim=exit, font_size_px=font_size)
    if two_lanes:
        first = track.lines[0]
        second = replace(first, chars=[replace(char, start_ms=char.start_ms+800) for char in first.chars],
                         end_ms=first.end_ms+800)
        track = replace(track, lines=[first, second])
        style = replace(style, dual_line_layout=True, line_alignments=['center', 'center'], line_gap_px=12)
    return track, style


def bounds(frame):
    alpha = frame[..., 3]
    y, x = np.nonzero(alpha > alpha.max() // 2)
    return np.array([x.min(), y.min(), x.max(), y.max()]) if x.size else np.array([])


@pytest.mark.parametrize('target', ['gpu', 'raster'])
@pytest.mark.parametrize('axis', ['slide', 'rise'])
def test_motion_relative_to_static_line(binary, project, target, axis, tmp_path):
    entry, exit = ('slide_in', 'slide_out') if axis == 'slide' else ('rise', 'rise')
    track, style = motion_project(project, entry, exit, font_size=20)
    ir = ir_for(binary, (track, style), dpr=1.5)
    static = ir_for(binary, (track, replace(style, entry_anim='none', exit_anim='none')), dpr=1.5)
    assert 'p07_line_motion' in ir['required_features'] and ir['skia_layout']['line_motion_schema'] == 1
    line = ir['track']['lines'][0]
    assert line['resolved_intervals'] == static['track']['lines'][0]['resolved_intervals']
    assert ir['skia_layout']['lines'] == static['skia_layout']['lines']
    start, end = line['display_start_ms'], line['display_end_ms']
    times = [start + line['entry_duration_ms']//2, end - line['exit_duration_ms']//2]
    if axis == 'slide':
        expected = ([-9, 0, -9, 0], [-27, 0, -27, 0])
    else:
        expected = ([0, 4.5, 0, 4.5], [0, -13.5, 0, -13.5])
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, times)
        configure(process, static)
        original, _ = collect(process, times, generation=2)
    for frame, baseline, offset in zip(frames, original, expected, strict=True):
        assert abs(bounds(frame) - bounds(baseline) - offset).max() <= 1
    if target == 'gpu' and axis == 'slide':
        (tmp_path / 'line-motion-layout.json').write_text(json.dumps({
            'schema': ir['skia_layout']['line_motion_schema'],
            'line': ir['skia_layout']['lines'][0],
            'times': times,
            'window': [start, end],
            'durations': [line['entry_duration_ms'], line['exit_duration_ms']],
        }, sort_keys=True), encoding='utf-8')


@pytest.mark.parametrize('mutation', ['feature', 'schema', 'wrong_phase'])
def test_invalid_line_motion_keeps_scene(binary, project, mutation):
    ir = ir_for(binary, motion_project(project))
    bad = copy.deepcopy(ir)
    if mutation == 'feature':
        bad['required_features'].remove('p07_line_motion')
    elif mutation == 'schema':
        bad['skia_layout']['line_motion_schema'] = '1'
    else:
        bad['track']['lines'][0]['exit_anim'] = 'slide_in'
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        configure(process, ir)
        before, _ = collect(process, [100])
        with pytest.raises(NativeRendererError):
            configure(process, bad)
        after, _ = collect(process, [100], generation=2)
    assert np.array_equal(before[0], after[0])


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('entry,exit,dpr,two_lanes', [
    ('slide_in', 'slide_out', 1, False),
    ('rise', 'rise', 1.5, False),
    ('fade', 'slide_out', 1.5, False),
    ('slide_in', 'slide_out', 1.5, True),
])
def test_line_motion_matches_actual_gpu(binary, project, entry, exit, dpr, two_lanes):
    from test_subtitle_render_gpu_backend import _render_g1_frames

    track, style = motion_project(project, entry, exit, two_lanes=two_lanes)
    ir = ir_for(binary, (track, style), dpr=dpr)
    line = ir['track']['lines'][0]
    regions = [slice(None)]
    if two_lanes:
        assert {item['lane'] for item in ir['track']['lines']} == {0, 1}
        top, bottom = (item['cells'] for item in ir['skia_layout']['lines'])
        first_bottom = max(c['baseline_y'] + c['metrics']['ink'][3] for c in top)
        second_top = min(c['baseline_y'] + c['metrics']['ink'][1] for c in bottom)
        split = round((first_bottom + second_top) * dpr / 2)
        regions = [slice(0, split), slice(split, None)]
    start, end = line['display_start_ms'], line['display_end_ms']
    times = sorted({start, start+17, start+50, start+100, start+200, start+400,
                    end-600, end-300, end-100, end-1, end})
    with NativeRendererProcess() as process:
        _, raw = _render_g1_frames(process, style, tuple(times), force_warp=False, track=track,
                                   width=640, height=360, dpr=dpr, realization_enabled=False)
    reference = [np.frombuffer(p, np.uint8).reshape(round(360*dpr), round(640*dpr), 4) for p in raw]
    for target in ('gpu', 'raster'):
        with SkiaRendererProcess(binary, selection=selection(target)) as process:
            configure(process, ir)
            frames, _ = collect(process, times)
        for t, expected, actual in zip(times, reference, frames, strict=True):
            for region in regions:
                ref, frame = expected[region], actual[region]
                assert abs(int(ref[..., 3].max()) - int(frame[..., 3].max())) <= 2, (target, t, region)
                if ref[..., 3].max() >= 16:
                    a, b = bounds(ref), bounds(frame)
                    assert a.size == b.size and abs(a-b).max() <= 1, (target, t, region, a, b)
            delta = abs(premultiplied(expected) - premultiplied(actual))
            assert delta.mean() < .16, (target, t, delta.mean())
            assert np.mean(delta.max(axis=-1) > 16) < .003, (target, t)
