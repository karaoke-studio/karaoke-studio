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
    'stretch_out', 'stretch_in', 'glow_in', 'glow_out', 'lightning', 'flame', 'entry_flame', 'entry_lightning',
])
def test_ruby_character_transform_owns_its_anchor_and_replays(binary, reading, target, animation):
    from test_subtitle_skia_static_glow import glowing_ruby

    track, style = glowing_ruby(reading, 'inherited')
    spinning = animation == 'spin_flip'
    affine = animation in {'scatter_out', 'converge_out', 'stretch_out', 'stretch_in',
                           'glow_in', 'glow_out', 'flame', 'entry_flame'}
    exiting = spinning or animation.endswith('_out') or animation in {'petal', 'note', 'lightning', 'flame'}
    effect = animation.removeprefix('entry_')
    style = replace(style, entry_anim='none' if exiting else effect,
                    exit_anim=effect if exiting else 'none', entry_lead_ms=600, exit_fade_ms=600,
                    karaoke_anim='utopia')
    ir = upper_ir(binary, (track, style), 1)
    expected = {
        'spin_flip': 'p07_ruby_character_exit_spin',
        'tracking_in': 'p07_ruby_character_translation', 'wave_in': 'p07_ruby_character_translation',
        'assemble_in': 'p07_ruby_particle_assembly', 'dissolve_out': 'p07_ruby_particle_assembly',
        'petal': 'p07_ruby_particle_fade', 'note': 'p07_ruby_particle_fade',
        'lightning': 'p07_ruby_particle_fade', 'flame': 'p07_ruby_particle_fade',
        'entry_flame': 'p07_ruby_particle_fade', 'entry_lightning': 'p07_ruby_particle_fade',
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
    if animation == 'entry_flame':
        moving = line['display_start_ms'] + line['entry_duration_ms'] * 7 // 8
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


_ENERGY_PAIRS = [('flame', 'lightning'), ('lightning', 'flame'), ('flame', 'flame'), ('lightning', 'lightning')]


@pytest.mark.parametrize('target', ['gpu', 'raster'])
@pytest.mark.parametrize('entry,exit_', _ENERGY_PAIRS)
def test_ruby_energy_pair_keeps_both_phases_and_replays(binary, reading, target, entry, exit_):
    from test_subtitle_skia_static_glow import glowing_ruby

    track, style = glowing_ruby(reading, 'inherited')
    style = replace(style, entry_anim=entry, exit_anim=exit_, entry_lead_ms=600, exit_fade_ms=600,
                    karaoke_anim='utopia')
    ir = upper_ir(binary, (track, style), 1)
    feature = 'p07_ruby_energy_pair'
    assert feature in ir['required_features']
    same_feature = 'p07_ruby_same_energy_pair'
    assert (same_feature in ir['required_features']) == (entry == exit_)
    source, = ir['track']['lines']
    assert (source['entry_anim'], source['exit_anim']) == (entry, exit_)
    group, = ir['skia_layout']['lines'][0]['ruby_groups']
    assert group['fade_owner'] == 0
    if 'flame' in (entry, exit_):
        assert np.isfinite(group['affine_pivot_y'])
    else:
        assert 'affine_pivot_y' not in group
    entering = source['display_start_ms'] + source['entry_duration_ms'] * 7 // 8
    leaving = source['display_end_ms'] - 120
    times = [entering, leaving, entering]
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, times)
        invalid = copy.deepcopy(ir)
        invalid['required_features'].remove(same_feature if entry == exit_ else feature)
        with pytest.raises(NativeRendererError):
            configure(process, invalid)
        replay, _ = collect(process, [leaving, entering], generation=2)
    assert frames[0].any() and frames[1].any() and not np.array_equal(frames[0], frames[1])
    assert np.array_equal(frames[0], frames[2]) and np.array_equal(frames[0], replay[1])
    assert np.array_equal(frames[1], replay[0])


_UNDECORATED_RUBY_PARTICLE_ENTRIES = ['sparkle', 'heart', 'petal', 'feather']
_UNDECORATED_RUBY_PARTICLE_PAIRS = [
    (entry, exit_) for entry in ('heart', 'petal', 'feather') for exit_ in ('heart', 'petal', 'feather')
]


@pytest.mark.parametrize('target', ['gpu', 'raster'])
@pytest.mark.parametrize('entry,exit_', _UNDECORATED_RUBY_PARTICLE_PAIRS)
def test_undecorated_ruby_particle_pair_preserves_phase_clocks(binary, reading, target, entry, exit_):
    track, style = reading
    style = replace(style, entry_anim=entry, exit_anim=exit_, entry_lead_ms=600, exit_fade_ms=600,
                    karaoke_anim='utopia', decoration_kind='none', decoration_kind_before='none',
                    ruby_decoration_kind='none', ruby_decoration_kind_before='none')
    ir = upper_ir(binary, (track, style), 1)
    feature = 'p07_ruby_particle_entry_exit' if entry == exit_ else 'p07_ruby_mixed_particle_entry_exit'
    assert feature in ir['required_features']
    line, = ir['track']['lines']
    opening = line['display_start_ms']
    entry_burst = next(burst for burst in line['fx_bursts'] if burst['start_ms'] < opening + 600)
    exit_burst = max(line['fx_bursts'], key=lambda burst: burst['start_ms'])
    assert (entry_burst['kind'], exit_burst['kind']) == (entry, exit_)
    assert exit_burst['start_ms'] > entry_burst['end_ms']
    entering, leaving = ((burst['start_ms'] + burst['end_ms']) // 2 for burst in (entry_burst, exit_burst))
    times = [entering, leaving, line['display_end_ms'] - 7, line['display_end_ms']]
    bad = copy.deepcopy(ir)
    bad['required_features'].remove(feature)
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, times)
        with pytest.raises(NativeRendererError):
            configure(process, bad)
        replay, _ = collect(process, times[::-1], generation=2)
    assert frames[0].any() and frames[1].any() and not np.array_equal(frames[0], frames[1])
    assert all(np.array_equal(frame, repeated) for frame, repeated in zip(frames, replay[::-1], strict=True))


@pytest.mark.parametrize('target', ['gpu', 'raster'])
@pytest.mark.parametrize('effect', _UNDECORATED_RUBY_PARTICLE_ENTRIES)
def test_undecorated_ruby_particle_entry_replays(binary, reading, target, effect):
    track, style = reading
    style = replace(style, entry_anim=effect, entry_lead_ms=600, exit_anim='none', karaoke_anim='utopia',
                    decoration_kind='none', decoration_kind_before='none',
                    ruby_decoration_kind='none', ruby_decoration_kind_before='none')
    ir = upper_ir(binary, (track, style), 1)
    feature = 'p07_ruby_particle_entry'
    assert feature in ir['required_features']
    line, = ir['track']['lines']
    assert line['entry_anim'] == effect and line['fx_bursts']
    group, = ir['skia_layout']['lines'][0]['ruby_groups']
    assert group['fade_owner'] == 0
    entering = line['display_start_ms'] + line['entry_duration_ms'] * 3 // 4
    settled = line['display_start_ms'] + line['entry_duration_ms']
    bad = copy.deepcopy(ir)
    bad['required_features'].remove(feature)
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, [entering, settled])
        with pytest.raises(NativeRendererError):
            configure(process, bad)
        replay, _ = collect(process, [settled, entering], generation=2)
    assert frames[0].any() and frames[1].any()
    assert not np.array_equal(frames[0], frames[1])
    assert np.array_equal(frames[1], replay[0]) and np.array_equal(frames[0], replay[1])


@pytest.mark.parametrize('target', ['gpu', 'raster'])
@pytest.mark.parametrize('scanline', [False, True])
def test_ruby_petal_entry_and_singing_flame_keep_independent_clocks(binary, reading, target, scanline):
    track, style = reading
    style = replace(style, entry_anim='petal', entry_lead_ms=600, exit_anim='none',
                    sing_fx='flame', anim_fx_speed_pct=50, fx_particle_speed_pct=200,
                    karaoke_anim='utopia_scanline' if scanline else 'utopia', scanline_enabled=scanline,
                    scanline_effect='star' if scanline else 'none',
                    decoration_kind='none', decoration_kind_before='none',
                    ruby_decoration_kind='none', ruby_decoration_kind_before='none')
    ir = upper_ir(binary, (track, style), 1)
    petals = upper_ir(binary, (track, replace(style, karaoke_anim='utopia', sing_fx='none',
                                            scanline_enabled=False, scanline_effect='none')), 1)
    flames = upper_ir(binary, (track, replace(style, entry_anim='none')), 1)
    source, = ir['track']['lines']
    bursts = source['fx_bursts']
    petal_bursts = [burst for burst in bursts if burst['kind'] == 'petal']
    singing_bursts = [burst for burst in bursts if burst['kind'] != 'petal']
    assert any(burst['kind'] == 'scanline' for burst in singing_bursts) == scanline
    assert petal_bursts and singing_bursts
    for mixed, separate in [(petal_bursts, petals), (singing_bursts, flames)]:
        standalone = separate['track']['lines'][0]['fx_bursts']
        for after, before in zip(mixed, standalone, strict=True):
            assert {key: value for key, value in after.items() if key != 'color_id'} == {
                key: value for key, value in before.items() if key != 'color_id'}
            assert ir['fx_color_table'][after['color_id']] == separate['fx_color_table'][before['color_id']]
    feature = 'p07_ruby_petal_entry_scanline' if scanline else 'p07_ruby_petal_entry_flame_singing'
    assert feature in ir['required_features']
    missing = copy.deepcopy(ir)
    missing['required_features'].remove(feature)
    bad_sweep = copy.deepcopy(ir)
    flame = next(burst for burst in bad_sweep['track']['lines'][0]['fx_bursts'] if burst['kind'] == 'flame')
    flame['sweep'] = 1
    times = [source['display_start_ms'] + 180, flame['start_ms'] + 100]
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, times)
        for invalid in (missing, bad_sweep):
            with pytest.raises(NativeRendererError):
                configure(process, invalid)
        replay, _ = collect(process, times[::-1], generation=2)
    assert all(frame.any() for frame in frames) and not np.array_equal(*frames)
    assert all(np.array_equal(frame, repeated) for frame, repeated in zip(frames, replay[::-1], strict=True))


@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_ruby_assembly_pair_owns_both_phases_and_replays(binary, reading, target):
    from test_subtitle_skia_static_glow import glowing_ruby

    track, style = glowing_ruby(reading, 'inherited')
    style = replace(style, entry_anim='assemble_in', exit_anim='dissolve_out', entry_lead_ms=600,
                    exit_fade_ms=600, karaoke_anim='utopia')
    ir = upper_ir(binary, (track, style), 1)
    feature = 'p07_ruby_particle_assembly_pair'
    assert feature in ir['required_features']
    source, = ir['track']['lines']
    assert {burst['kind'] for burst in source['fx_bursts']} == {'assemble', 'dissolve'}
    group, = ir['skia_layout']['lines'][0]['ruby_groups']
    assert group['fade_owner'] == 0
    times = [source['display_start_ms'] + 170, source['display_end_ms'] - 350]
    bad = copy.deepcopy(ir)
    bad['required_features'].remove(feature)
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, times)
        with pytest.raises(NativeRendererError):
            configure(process, bad)
        replay, _ = collect(process, times[::-1], generation=2)
    assert all(frame.any() for frame in frames) and not np.array_equal(*frames)
    assert all(np.array_equal(frame, repeated) for frame, repeated in zip(frames, replay[::-1], strict=True))


@pytest.mark.parametrize('layer', ['decoration_kind', 'ruby_decoration_kind'])
def test_ruby_particle_entry_rejects_unverified_glow(binary, reading, layer):
    track, style = reading
    style = replace(style, entry_anim='sparkle', exit_anim='none', **{layer: 'glow'})
    with pytest.raises(ValueError, match='粒子入场暂不支持阴影或光晕'):
        upper_ir(binary, (track, style), 1)


def assert_ruby_transition_gpu_parity(binary, case, times, *, extra_tracks=None, dpr=1):
    from test_subtitle_render_gpu_backend import _render_g1_frames
    from test_subtitle_skia_static_glow import assert_glow_parity

    with NativeRendererProcess() as process:
        _, raw = _render_g1_frames(process, case.style, times, force_warp=False,
                                   track=case.track, width=1920, height=1080, dpr=dpr, realization_enabled=False,
                                   extra_tracks=extra_tracks)
    reference = [np.frombuffer(p, np.uint8).reshape(round(1080 * dpr), round(1920 * dpr), 4) for p in raw]
    rendered = []
    for target in ('gpu', 'raster'):
        with SkiaRendererProcess(binary, selection=selection(target)) as process:
            process.configure(case.track, case.style, width=1920, height=1080, fps=60, extra_tracks=extra_tracks, dpr=dpr)
            frames, _ = collect(process, times)
            rendered.append(frames)
    assert_glow_parity(times, reference, *rendered, case.case_id, check_p00_edges=True)


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('entry,exit_', _ENERGY_PAIRS)
def test_ruby_energy_pair_matches_actual_gpu(binary, application, tmp_path, entry, exit_):
    from scripts.subtitle_gpu_baseline.cases import build_cases

    source = next(case for case in build_cases(tmp_path) if case.case_id == 'entry_flame')
    style = replace(source.style, entry_anim=entry, exit_anim=exit_)
    case = replace(source, case_id=f'{entry}_in_{exit_}_out', style=style)
    times = (100, 299, 301, 450, 735, 840, 1000, 1500, 4500, 4900, 5100, 5370, 5499, 5900)
    assert_ruby_transition_gpu_parity(binary, case, times)


@pytest.mark.parametrize('target', ['gpu', 'raster'])
@pytest.mark.parametrize('glow', ['none', 'ruby', 'body', 'both'])
def test_ruby_utopia_exit_uses_last_base_unit(binary, reading, target, glow):
    from test_subtitle_skia_static_glow import glowing_ruby

    track, style = reading
    ruby_glow, body_glow = glow in {'ruby', 'both'}, glow in {'body', 'both'}
    if ruby_glow:
        track, style = glowing_ruby(reading, 'independent')
    ruby_decoration = 'glow' if ruby_glow else 'none'
    body_decoration = 'glow' if body_glow else 'none'
    style = replace(style, entry_anim='none', exit_anim='utopia', karaoke_anim='utopia', line_tail_ms=0,
                    decoration_kind=body_decoration, decoration_kind_before=body_decoration,
                    glow_before_radius_px=6, glow_after_radius_px=6,
                    ruby_decoration_kind=ruby_decoration, ruby_decoration_kind_before=ruby_decoration)
    ir = upper_ir(binary, (track, style), 1)
    feature = 'p07_ruby_utopia_exit'
    assert feature in ir['required_features']
    glow_features = {'p07_ruby_utopia_exit_glow': ruby_glow, 'p07_ruby_utopia_exit_body_glow': body_glow}
    for capability, enabled in glow_features.items():
        assert (capability in ir['required_features']) == enabled
    group, = ir['skia_layout']['lines'][0]['ruby_groups']
    assert group['utopia_exit_owner'] == 1
    assert 'fade_owner' not in group
    done = ir['track']['lines'][0]['resolved_intervals'][1][1]
    times = [done, done + 200, done + 750]
    without_owner = copy.deepcopy(ir)
    without_owner['skia_layout']['lines'][0]['ruby_groups'][0]['utopia_exit_owner'] = 2
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, times)
        with pytest.raises(NativeRendererError):
            configure(process, without_owner)
        for capability, enabled in glow_features.items():
            if not enabled:
                continue
            missing = copy.deepcopy(ir)
            missing['required_features'].remove(capability)
            with pytest.raises(NativeRendererError):
                configure(process, missing)
        replay, _ = collect(process, times[::-1], generation=2)
    assert frames[0].any() and frames[1].any() and not frames[2].any()
    assert not np.array_equal(frames[0], frames[1])
    assert all(np.array_equal(frame, repeated) for frame, repeated in zip(frames, replay[::-1], strict=True))


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('speed', [50, 100, 200])
@pytest.mark.parametrize('karaoke', ['none', 'utopia'])
def test_ruby_petal_entry_and_singing_flame_match_actual_gpu(binary, application, tmp_path, speed, karaoke):
    from scripts.subtitle_gpu_baseline.cases import build_cases

    case = next(case for case in build_cases(tmp_path) if case.case_id == 'entry_petal')
    style = replace(case.style, karaoke_anim=karaoke, sing_fx='flame',
                    anim_fx_speed_pct=speed, fx_particle_speed_pct=speed,
                    decoration_kind='none', decoration_kind_before='none',
                    ruby_decoration_kind='none', ruby_decoration_kind_before='none')
    times = (100, 240, 299, 301, 450, 650, 900, 1000, 1200, 1300, 1500, 1750, 2600, 3700, 4500, 5500, 5900)
    assert_ruby_transition_gpu_parity(binary, replace(case, style=style), times)


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('effect,speed,overlap', [
    *((effect, speed, False) for effect in ('none', 'star') for speed in (50, 100, 200)),
    ('star', 100, True),
])
def test_ruby_petal_entry_and_singing_scanline_match_actual_gpu(binary, application, tmp_path, speed, effect, overlap):
    from scripts.subtitle_gpu_baseline.cases import build_cases

    case = next(case for case in build_cases(tmp_path) if case.case_id == 'particles_speed_50')
    style = replace(case.style, exit_anim='none', scanline_enabled=True, scanline_effect=effect,
                    anim_fx_speed_pct=speed, fx_particle_speed_pct=speed,
                    decoration_kind='none', decoration_kind_before='none',
                    ruby_decoration_kind='none', ruby_decoration_kind_before='none')
    times = (100, 240, 299, 301, 450, 650, 900, 1000, 1200, 1300, 1500, 1750, 2600, 3700, 4500, 5500, 5900)
    if overlap:
        line, = case.track.lines
        starts = (400, 650, 900)
        chars = [replace(char, start_ms=time) for char, time in zip(line.chars[:len(starts)], starts, strict=True)]
        track = replace(case.track, lines=[replace(line, chars=chars + line.chars[len(starts):])])
        case = replace(case, track=track)
        times = (301, 450, 525, 600, 650, 750, 850, 899, 900, 901, 1100, 2300, 4400, 5499, 5500)
    assert_ruby_transition_gpu_parity(binary, replace(case, style=style), times)


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('glow', ['none', 'ruby', 'body', 'both'])
def test_ruby_utopia_exit_matches_actual_gpu(binary, application, tmp_path, glow):
    from scripts.subtitle_gpu_baseline.cases import build_cases

    case = next(case for case in build_cases(tmp_path) if case.case_id == 'exit_utopia')
    style = case.style
    if glow in {'none', 'ruby'}:
        style = replace(style, decoration_kind='none', decoration_kind_before='none')
    if glow != 'both':
        ruby_decoration = 'glow' if glow == 'ruby' else 'none'
        style = replace(style, ruby_decoration_kind=ruby_decoration, ruby_decoration_kind_before=ruby_decoration)
    times = (299, 301, 1500, 2800, 3000, 3200, 3400, 3600, 3900, 4000, 4500,
             4501, 4700, 4900, 5100, 5249, 5250, 5499, 5900)
    assert_ruby_transition_gpu_parity(binary, replace(case, style=style), times)


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('effect', ['none', 'utopia'])
def test_ruby_assembly_pair_matches_actual_gpu(binary, application, tmp_path, effect):
    from scripts.subtitle_gpu_baseline.cases import build_cases

    case = next(case for case in build_cases(tmp_path) if case.case_id == 'entry_assemble_in')
    style = replace(case.style, exit_anim='dissolve_out', karaoke_anim=effect)
    times = (100, 299, 301, 450, 650, 899, 900, 901, 1500, 4500, 4900, 5100, 5499, 5500, 5900)
    assert_ruby_transition_gpu_parity(binary, replace(case, style=style), times)


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('case_id', [
    'exit_spin_flip', 'entry_tracking_in', 'entry_wave_in', 'entry_assemble_in', 'exit_dissolve_out',
    'exit_petal', 'exit_feather', 'exit_snow_solid', 'exit_note', 'exit_heart', 'exit_sparkle', 'exit_lightning',
    'exit_flame', 'entry_flame', 'entry_lightning',
    'exit_scatter_out', 'exit_converge_out',
    'exit_stretch_out', 'entry_stretch_in', 'entry_glow_in', 'exit_glow_out', 'line_override',
])
def test_p00_ruby_character_transform_matches_actual_gpu(binary, application, tmp_path, case_id):
    from scripts.subtitle_gpu_baseline.cases import build_cases

    case = next(case for case in build_cases(tmp_path) if case.case_id == case_id)
    times = case.timestamps_ms
    if case_id == 'exit_flame':
        times = tuple(sorted({*times, 5240, 5305, 5370, 5440}))
    elif case_id == 'entry_flame':
        times = tuple(sorted({*times, 735, 780, 840}))
    assert_ruby_transition_gpu_parity(binary, case, times)


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('entry,exit_,speed,dpr', [
    (entry, exit_, speed, 1)
    for entry, exit_ in _UNDECORATED_RUBY_PARTICLE_PAIRS
    for speed in ((100,) if entry == exit_ else (50, 100, 200))
] + [
    ('petal', 'heart', 200, 1.5), ('feather', 'petal', 50, 1.5), ('heart', 'feather', 100, 1.5),
])
def test_undecorated_ruby_particle_pair_matches_actual_gpu(binary, application, tmp_path, entry, exit_, speed, dpr):
    from scripts.subtitle_gpu_baseline.cases import build_cases

    case = next(case for case in build_cases(tmp_path) if case.case_id == f'entry_{entry}')
    style = replace(case.style, exit_anim=exit_, anim_fx_speed_pct=speed,
                    decoration_kind='none', decoration_kind_before='none',
                    ruby_decoration_kind='none', ruby_decoration_kind_before='none')
    times = (100, 240, 299, 301, 450, 650, 899, 901, 1500, 4500, 4900, 5100, 5300,
             5450, 5475, 5480, 5493, 5499, 5500, 5900)
    if dpr != 1:
        times = (301, 650, 899, 1500, 4900, 5300, 5450, 5475, 5480, 5493, 5499, 5500)
    assert_ruby_transition_gpu_parity(
        binary, replace(case, case_id=f'{entry}_in_{exit_}_out_{speed}', style=style), times, dpr=dpr)


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('effect', _UNDECORATED_RUBY_PARTICLE_ENTRIES)
def test_undecorated_ruby_particle_entry_matches_actual_gpu(binary, application, tmp_path, effect):
    from scripts.subtitle_gpu_baseline.cases import build_cases

    original = next(case for case in build_cases(tmp_path) if case.case_id == f'entry_{effect}')
    style = replace(original.style, decoration_kind='none', decoration_kind_before='none',
                    ruby_decoration_kind='none', ruby_decoration_kind_before='none')
    times = tuple(sorted({*original.timestamps_ms, 100, 240, 400, 550, 800, 899, 900, 901, 2600, 3700}))
    assert_ruby_transition_gpu_parity(binary, replace(original, style=style), times)


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
