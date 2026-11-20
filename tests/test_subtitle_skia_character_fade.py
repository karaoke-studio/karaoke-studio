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
from test_subtitle_skia_outline_ruby import annotated, mixed, pages, reading, upper_ir
from test_subtitle_skia_rtl_flow import rtl_source
from test_subtitle_skia_vertical import vertical_ruby_source
from krok_helper.subtitle_render.domain.timing import LineAnimationOverride
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


@pytest.mark.parametrize('target', ['gpu', 'raster'])
@pytest.mark.parametrize('animation', ['none', 'utopia'])
def test_ruby_and_glow_follow_their_first_character_entry_fade(binary, reading, target, animation, tmp_path):
    from test_subtitle_skia_static_glow import glowing_ruby

    track, style = glowing_ruby(reading, 'two_groups')
    style = replace(style, entry_anim='char_fade', entry_lead_ms=600, karaoke_anim=animation)
    ir = upper_ir(binary, (track, style), 1)
    assert 'p07_character_entry_fade_composition' in ir['required_features']
    layout = ir['skia_layout']['lines'][0]
    groups = layout['ruby_groups']
    assert [group['fade_owner'] for group in groups] == [0, 1]
    invalid = copy.deepcopy(ir)
    invalid['skia_layout']['lines'][0]['ruby_groups'][0]['fade_owner'] = len(layout['cells'])
    unsupported = copy.deepcopy(ir)
    unsupported['track']['lines'][0].update(exit_anim='char_fade', exit_duration_ms=600)
    source = ir['track']['lines'][0]
    start, end = source['display_start_ms'], source['display_end_ms']
    times = [start, start+125, start+600, end-200, end, start+125]
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, times)
        with pytest.raises(NativeRendererError):
            configure(process, invalid)
        with pytest.raises(NativeRendererError):
            configure(process, unsupported)
        previous, _ = collect(process, [start+125], generation=2)

    def alpha(frame, group):
        unit, = group['units']
        l, t, r, b = unit['metrics']['ink']
        x, y = unit['origin_x'], unit['baseline_y']
        return frame[int(y+t):int(y+b)+1, int(x+l):int(x+r)+1, 3].max()

    assert not frames[0].any() and not frames[4].any()
    assert 0 < alpha(frames[1], groups[0]) < alpha(frames[2], groups[0])
    assert alpha(frames[1], groups[1]) == 0 < alpha(frames[2], groups[1])
    assert alpha(frames[3], groups[0]) > 0 and alpha(frames[3], groups[1]) > 0
    assert np.array_equal(frames[1], frames[-1]) and np.array_equal(frames[1], previous[0])
    if target == 'gpu' and animation == 'utopia':
        (tmp_path / 'ruby-character-entry-fade-layout.json').write_text(
            json.dumps(layout, sort_keys=True), encoding='utf-8')


@pytest.mark.parametrize('target', ['gpu', 'raster'])
@pytest.mark.parametrize('animation', [
    'spin_flip', 'tracking_in', 'wave_in', 'assemble_in', 'dissolve_out', 'petal', 'note', 'scatter_out', 'converge_out',
    'stretch_out', 'stretch_in', 'glow_in', 'glow_out', 'lightning',
])
def test_ruby_character_transform_owns_its_anchor_and_replays(binary, reading, target, animation):
    from test_subtitle_skia_static_glow import glowing_ruby

    track, style = glowing_ruby(reading, 'inherited')
    spinning = animation == 'spin_flip'
    affine = animation in {'scatter_out', 'converge_out', 'stretch_out', 'stretch_in', 'glow_in', 'glow_out'}
    exiting = spinning or animation.endswith('_out') or animation in {'petal', 'note', 'lightning'}
    style = replace(style, entry_anim='none' if exiting else animation,
                    exit_anim=animation if exiting else 'none', entry_lead_ms=600, exit_fade_ms=600,
                    karaoke_anim='utopia')
    ir = upper_ir(binary, (track, style), 1)
    expected = {
        'spin_flip': 'p07_ruby_character_exit_spin',
        'tracking_in': 'p07_ruby_character_translation', 'wave_in': 'p07_ruby_character_translation',
        'assemble_in': 'p07_ruby_particle_assembly', 'dissolve_out': 'p07_ruby_particle_assembly',
        'petal': 'p07_ruby_particle_fade', 'note': 'p07_ruby_particle_fade', 'lightning': 'p07_ruby_particle_fade',
        'scatter_out': 'p07_ruby_character_affine_exit', 'converge_out': 'p07_ruby_character_affine_exit',
        'stretch_out': 'p07_ruby_character_stretch_exit', 'stretch_in': 'p07_ruby_character_stretch_entry',
        'glow_in': 'p07_ruby_character_glow_transition', 'glow_out': 'p07_ruby_character_glow_transition',
    }[animation]
    assert expected in ir['required_features']
    group, = ir['skia_layout']['lines'][0]['ruby_groups']
    assert group['fade_owner'] == 0
    if spinning:
        assert len(group['spin_pivot']) == 2 and np.isfinite(group['spin_pivot']).all()
    if affine:
        assert np.isfinite(group['affine_pivot_y'])
        assert all(np.isfinite(unit['affine_pivot_x']) for unit in group['units'])
    bad = copy.deepcopy(ir)
    del bad['skia_layout']['lines'][0]['ruby_groups'][0][
        'spin_pivot' if spinning else 'affine_pivot_y' if affine else 'fade_owner']
    line = ir['track']['lines'][0]
    moving = line['display_end_ms']-400 if exiting else line['display_start_ms']+170
    steady = line['display_end_ms']-600 if exiting else line['display_start_ms']+600
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, [moving, steady, moving])
        with pytest.raises(NativeRendererError):
            configure(process, bad)
        replay, _ = collect(process, [moving])
    assert frames[0].any() and not np.array_equal(frames[0], frames[1])
    assert np.array_equal(frames[0], frames[2]) and np.array_equal(frames[0], replay[0])


@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_ruby_geometry_uses_both_line_override_phases(binary, reading, target):
    from test_subtitle_skia_static_glow import glowing_ruby

    track, style = glowing_ruby(reading, 'inherited')
    override = LineAnimationOverride(entry_anim='wave_in', exit_anim='scatter_out',
                                     entry_duration_ms=600, exit_duration_ms=600)
    track = replace(track, lines=[replace(track.lines[0], animation_override=override)])
    style = replace(style, entry_anim='fade', exit_anim='fade', karaoke_anim='utopia')
    ir = upper_ir(binary, (track, style), 1)
    feature = 'p07_ruby_character_wave_scatter'
    line, = ir['track']['lines']
    assert (line['entry_anim'], line['exit_anim']) == ('wave_in', 'scatter_out')
    assert feature in ir['required_features']
    group, = ir['skia_layout']['lines'][0]['ruby_groups']
    assert np.isfinite(group['affine_pivot_y'])
    assert all(np.isfinite(unit['affine_pivot_x']) for unit in group['units'])
    bad = copy.deepcopy(ir)
    bad['required_features'].remove(feature)
    entering = line['display_start_ms'] + 150
    leaving = line['display_end_ms'] - 350
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, [entering, leaving, entering])
        with pytest.raises(NativeRendererError):
            configure(process, bad)
        replay, _ = collect(process, [leaving, entering], generation=2)
    assert frames[0].any() and frames[1].any()
    assert not np.array_equal(frames[0], frames[1])
    assert np.array_equal(frames[0], frames[2])
    assert np.array_equal(frames[1], replay[0]) and np.array_equal(frames[0], replay[1])


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('case_id', [
    'exit_spin_flip', 'entry_tracking_in', 'entry_wave_in', 'entry_assemble_in', 'exit_dissolve_out',
    'exit_petal', 'exit_feather', 'exit_snow_solid', 'exit_note', 'exit_heart', 'exit_sparkle', 'exit_lightning',
    'exit_scatter_out', 'exit_converge_out',
    'exit_stretch_out', 'entry_stretch_in', 'entry_glow_in', 'exit_glow_out', 'line_override',
])
def test_p00_ruby_character_transform_matches_actual_gpu(binary, application, tmp_path, case_id):
    from scripts.subtitle_gpu_baseline.cases import build_cases
    from test_subtitle_render_gpu_backend import _render_g1_frames
    from test_subtitle_skia_static_glow import assert_glow_parity

    case = next(case for case in build_cases(tmp_path) if case.case_id == case_id)
    with NativeRendererProcess() as process:
        _, raw = _render_g1_frames(process, case.style, case.timestamps_ms, force_warp=False,
                                   track=case.track, width=1920, height=1080, dpr=1, realization_enabled=False)
    reference = [np.frombuffer(p, np.uint8).reshape(1080, 1920, 4) for p in raw]
    rendered = []
    for target in ('gpu', 'raster'):
        with SkiaRendererProcess(binary, selection=selection(target)) as process:
            process.configure(case.track, case.style, width=1920, height=1080, fps=60)
            frames, _ = collect(process, case.timestamps_ms)
            rendered.append(frames)
    assert_glow_parity(case.timestamps_ms, reference, *rendered, case.case_id, check_p00_edges=True)


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
