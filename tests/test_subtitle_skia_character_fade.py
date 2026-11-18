"""Character fade keeps the GPU's per-brush opacity and fixed stagger."""
from dataclasses import replace
import copy
import json
import sys

import numpy as np
import pytest

from test_subtitle_skia_line_fade import (
    application, binary, project, fade_project, ir_for, collect, configure, selection, premultiplied,
)
from test_subtitle_skia_rtl_flow import rtl_source
from test_subtitle_skia_vertical import vertical_ruby_source
from krok_helper.subtitle_render.native.backend import NativeRendererError, NativeRendererProcess
from krok_helper.subtitle_render.native.skia_backend import SkiaRendererProcess


def character_fade_project(project, case='plain'):
    if case == 'reverse':
        track, style = rtl_source(project, reverse=True)
    else:
        track, style = fade_project(project, case)
    return track, replace(style, right_to_left=False, entry_anim='rise' if case == 'mixed' else 'char_fade',
                          exit_anim='char_fade', entry_lead_ms=600, exit_fade_ms=600)


@pytest.mark.parametrize('target', ['gpu', 'raster'])
@pytest.mark.parametrize('single', [False, True])
def test_character_fade_matches_fixed_stagger(binary, project, target, single, tmp_path):
    track, style = character_fade_project(project)
    if single:
        track = replace(track, lines=[replace(track.lines[0], chars=track.lines[0].chars[:1])])
    ir = ir_for(binary, (track, style))
    assert 'p07_character_fade' in ir['required_features']
    assert 'p07_line_fade' not in ir['required_features']
    layout = ir['skia_layout']['lines'][0]
    line = ir['track']['lines'][0]
    start, end = line['display_start_ms'], line['display_end_ms']
    times = [start, start+125, start+351, start+600, end-500, end-300, end-1, end]
    num = len(layout['cells'])
    delay = 350 // (num-1) if num > 1 else 0
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, times)
    for i, cell in enumerate(layout['cells']):
        left, top, right, bottom = cell['metrics']['ink']
        x = round(cell['origin_x'] + (left+right)/2)
        y = round(cell['baseline_y'] + (top+bottom)/2)
        for time, frame in zip(times, frames, strict=True):
            if time >= end:
                full = 0
            elif time >= max(line['end_ms'], end-600):
                full = np.clip((end-delay*(num-i-1)-time)/250, 0, 1)
            elif time <= start+600:
                full = np.clip((time-start-delay*i)/250, 0, 1)
            else:
                full = 1
            assert abs(int(frame[y,x,3])-round(255*full)) <= 1, (target, time, i)
    if target == 'gpu' and not single:
        (tmp_path / 'character-fade-layout.json').write_text(json.dumps({
            'schema': ir['skia_layout']['character_fade_schema'],
            'line': layout,
            'timers': [start, end, line['end_ms'], line['entry_duration_ms'], line['exit_duration_ms']],
        }, sort_keys=True), encoding='utf-8')


@pytest.mark.parametrize('mutated', ['feature', 'schema', 'ruby'])
def test_character_fade_capabilities_and_unsupported_ruby_preserve_scene(binary, project, mutated):
    ir = ir_for(binary, character_fade_project(project))
    bad = copy.deepcopy(ir)
    if mutated == 'feature':
        bad['required_features'].remove('p07_character_fade')
    elif mutated == 'schema':
        bad['skia_layout']['character_fade_schema'] = True
    else:
        track, style = vertical_ruby_source(project)
        bad = ir_for(binary, (track, replace(style, entry_anim='char_fade', entry_lead_ms=180)))
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        configure(process, ir)
        before, _ = collect(process, [125])
        with pytest.raises(NativeRendererError):
            configure(process, bad)
        after, _ = collect(process, [125], generation=2)
    assert np.array_equal(before[0], after[0])


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('case,dpr', [('plain', 1), ('outlines', 1.5), ('guide', 1.5), ('reverse', 1.5), ('mixed', 1)])
def test_character_fade_matches_actual_gpu(binary, project, case, dpr):
    from test_subtitle_render_gpu_backend import _render_g1_frames

    track, style = character_fade_project(project, case)
    ir = ir_for(binary, (track, style), dpr=dpr)
    line = ir['track']['lines'][0]
    start, end = line['display_start_ms'], line['display_end_ms']
    times = sorted({start, start+65, start+125, start+250, start+351, start+600,
                    end-601, end-600, end-500, end-300, end-100, end-1, end})
    with NativeRendererProcess() as process:
        _, raw = _render_g1_frames(process, style, tuple(times), force_warp=False, track=track,
                                   width=640, height=360, dpr=dpr, realization_enabled=False)
    reference = [np.frombuffer(p, np.uint8).reshape(round(360*dpr), round(640*dpr), 4) for p in raw]
    for target in ('gpu', 'raster'):
        with SkiaRendererProcess(binary, selection=selection(target)) as process:
            configure(process, ir)
            frames, _ = collect(process, times)
        for t, expected, actual in zip(times, reference, frames, strict=True):
            assert abs(int(expected[..., 3].max()) - int(actual[..., 3].max())) <= 2, (case, target, t)
            delta = abs(premultiplied(expected) - premultiplied(actual))
            assert delta.mean() < .13, (case, target, t, delta.mean())
            assert np.mean(delta.max(axis=-1) > 16) < .003, (case, target, t)
