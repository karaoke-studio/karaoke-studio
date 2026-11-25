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
from krok_helper.subtitle_render.domain.timing import LineAnimationOverride
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


@pytest.mark.parametrize('mutation', [ 'profile'])
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


SYMBOL_MODES = [
    ('note', 'heart', {'note', 'heart'}),
    ('ripple', 'ripple', {'ripple'}),
    ('sparkle', 'sparkle', {'sparkle'}),
    ('assemble_in', 'dissolve_out', {'assemble', 'dissolve'}),
]


def symbol_source(project, entry, exit_, speed=0):
    track, style = falling_source(project, entry, speed)
    return track, replace(style, exit_anim=exit_)


def symbol_window_samples(ir):
    source = ir['track']['lines'][0]
    start, end = source['display_start_ms'], source['display_end_ms']
    last = max(b['end_ms'] for b in source['fx_bursts'])
    return sorted({start-100, start-40, start-1, start, start+45, start+200, start+450,
                   2100, end-150, end-1, end, end+45, end+120, end+300, max(last, end)+1})


def test_particle_transition_rows_keep_independent_clocks(binary, project):
    from test_subtitle_skia_outline_ruby import render_skia

    track, style = symbol_source(project, 'note', 'heart')
    first, = track.lines
    shift = 4000
    second = replace(first, track_line_index=1, break_before='page',
                     chars=[replace(char, start_ms=char.start_ms + shift) for char in first.chars],
                     end_ms=first.end_ms + shift, display_start_override_ms=first.display_start_override_ms + shift,
                     display_end_override_ms=first.display_end_override_ms + shift,
                     animation_override=LineAnimationOverride(entry_anim='petal', exit_anim='petal',
                                                             entry_duration_ms=600, exit_duration_ms=600))
    track = replace(track, lines=[first, second])
    ir = ir_for(binary, (track, style), dpr=1.5)
    assert ir['skia_layout']['sing_twinkle_schema'] == 10
    bursts = [line['fx_bursts'] for line in ir['track']['lines']]
    assert [{burst['kind'] for burst in row} for row in bursts] == [{'note', 'heart'}, {'petal'}]
    assert max(burst['end_ms'] for burst in bursts[0]) < min(burst['start_ms'] for burst in bursts[1])
    times = (950, 1100, 1600, 2970, 3200, 4100, 4850, 5050, 5550, 7020, 7150)
    gpu, raster = render_skia(binary, ir, times)
    assert any(blue_coverage(frame).max() > 20 for frame in gpu[:5])
    assert any(blue_coverage(frame).max() > 20 for frame in gpu[6:])
    assert not gpu[5].any()
    reference = None
    if sys.platform == 'win32':
        from test_subtitle_render_gpu_backend import _render_g1_frames

        with NativeRendererProcess() as process:
            _, raw = _render_g1_frames(process, style, times, force_warp=False, track=track,
                                       width=640, height=360, dpr=1.5, realization_enabled=False)
        reference = [np.frombuffer(frame, np.uint8).reshape(540, 960, 4) for frame in raw]
    for index, time in enumerate(times):
        assert_particle_region(gpu[index], raster[index], ('particle_rows', time, 'raster'), check_contour=False)
        if reference is not None:
            assert_particle_region(reference[index], gpu[index], ('particle_rows', time), check_contour=False)


@pytest.mark.parametrize('entry,exit_,kinds', SYMBOL_MODES)
@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_particle_transitions_keep_symbol_windows_and_replay(binary, project, entry, exit_, kinds, target, tmp_path):
    ir = ir_for(binary, symbol_source(project, entry, exit_), dpr=1.5)
    assert ir['skia_layout']['character_transform_schema'] == 7
    assert ir['skia_layout']['sing_twinkle_schema'] == 10
    source = ir['track']['lines'][0]
    bursts = source['fx_bursts']
    assert {b['kind'] for b in bursts} == kinds
    assert all(b['sweep'] == 0 and b['front'] == (b['kind'] != 'ripple') for b in bursts)
    times = symbol_window_samples(ir)
    first, last = source['display_start_ms'], source['display_end_ms']
    replay = source['display_start_ms'] + 200
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, [*times, replay])
    assert np.array_equal(frames[times.index(replay)], frames[-1])
    assert not frames[0][..., 3].any() and not frames[-2][..., 3].any()
    assert frames[times.index(2100)][..., :2].any()
    assert any(blue_coverage(f).max() > 20 for t, f in zip(times, frames[:-1], strict=True) if first <= t < last)
    before_text = frames[times.index(first-1)]
    assert not before_text[..., :2].any()
    assert bool(blue_coverage(before_text).any()) == (exit_ != 'dissolve_out')
    if entry == 'ripple':
        # At this radius transition the ring must continue to grow smoothly.
        assert blue_coverage(frames[times.index(first)]).sum() > blue_coverage(before_text).sum()
    after_text = [f for t, f in zip(times, frames[:-1], strict=True) if t >= last]
    assert not any(f[..., :2].any() for f in after_text)
    assert any(blue_coverage(f).max() > 20 for f in after_text) == (exit_ != 'dissolve_out')
    if entry == 'assemble_in' and target == 'gpu':
        (tmp_path / 'symbol-transition-layout.json').write_text(json.dumps({
            'line': ir['skia_layout']['lines'][0], 'bursts': bursts,
            'features': ir['required_features'], 'display': [first, last],
        }, sort_keys=True), encoding='utf-8')


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('entry,exit_,kinds', SYMBOL_MODES)
def test_symbol_transitions_match_actual_d2d_and_gpu_raster(binary, project, entry, exit_, kinds):
    from test_subtitle_render_gpu_backend import _render_g1_frames

    track, style = symbol_source(project, entry, exit_)
    ir = ir_for(binary, (track, style), dpr=1.5)
    times = symbol_window_samples(ir)
    with NativeRendererProcess() as process:
        _, raw = _render_g1_frames(process, style, tuple(times), force_warp=False, track=track,
                                   width=640, height=360, dpr=1.5, realization_enabled=False)
    reference = [np.frombuffer(p, np.uint8).reshape(540, 960, 4) for p in raw]
    actual = []
    for target in ('gpu', 'raster'):
        with SkiaRendererProcess(binary, selection=selection(target)) as process:
            configure(process, ir)
            frames, _ = collect(process, times)
        actual.append(frames)
    for t, d2d, gpu, raster in zip(times, reference, *actual, strict=True):
        sample = ('symbol_transition', entry, t)
        if entry == 'ripple':
            assert_thin_particle_contours(d2d, gpu, sample)
        else:
            assert_particle_region(d2d, gpu, sample, check_contour=False)
        assert_particle_region(gpu, raster, (*sample, 'raster'), check_contour=False)


_IMPACT = {'flash', 'shockwave', 'spark'}
ENERGY_LAYERS = {
    ('flame', True): {'flame', 'flame_alt', 'flame_glow', 'ember', 'fireball'} | _IMPACT,
    ('flame', False): {'flame', 'flame_alt', 'flame_glow', 'ember', 'fireball', 'ash'} | _IMPACT,
    ('lightning', True): {'lightning', 'lightning_glow'} | _IMPACT,
    ('lightning', False): {'lightning', 'lightning_glow'} | _IMPACT,
}
ENERGY_MODES = [('flame', 'flame'), ('lightning', 'lightning'), ('lightning', 'flame')]


def alpha_coverage(frame):
    return frame[..., 3].astype(float)


def energy_window_samples(ir):
    source = ir['track']['lines'][0]
    start, end = source['display_start_ms'], source['display_end_ms']
    last = max(b['end_ms'] for b in source['fx_bursts'])
    assert end < last
    return sorted({start-1, start, start+20, start+60, start+150, start+300, start+450, start+600, start+860,
                   2100, 2830, 2900, end-100, end-1, end, end+40, end+120, end+250, last-1, last+1})


@pytest.mark.parametrize('entry,exit_', ENERGY_MODES)
@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_energy_transitions_keep_companion_layers_and_outside_tail(binary, project, entry, exit_, target, tmp_path):
    ir = ir_for(binary, symbol_source(project, entry, exit_), dpr=1.5)
    assert ir['skia_layout']['character_transform_schema'] == 8
    assert ir['skia_layout']['sing_twinkle_schema'] == 11
    source = ir['track']['lines'][0]
    start, end = source['display_start_ms'], source['display_end_ms']
    bursts = source['fx_bursts']
    entry_side = [b for b in bursts if b['start_ms'] < 2000]
    exit_side = [b for b in bursts if b['start_ms'] >= 2000]
    # Every companion layer of the existing planner is present, at its own depth.
    assert {b['kind'] for b in entry_side} == ENERGY_LAYERS[(entry, True)]
    assert {b['kind'] for b in exit_side} == ENERGY_LAYERS[(exit_, False)]
    assert all(b['sweep'] == 0 and b['front'] == (b['kind'] not in {'flame_glow', 'lightning_glow', 'shockwave'})
               for b in bursts)
    times = energy_window_samples(ir)
    replay = [2830, end + 40, start + 150]
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, [*times, *replay])
    for t, frame in zip(replay, frames[len(times):], strict=True):
        assert np.array_equal(frame, frames[times.index(t)]), (target, t)
    frames = frames[:len(times)]
    # Energy kinds have no prelude, but the existing tail gate also admits an
    # entry burst that is already active before the text window opens.
    early = any(b['start_ms'] <= start - 1 < b['end_ms'] for b in bursts)
    assert bool(frames[0][..., 3].any()) == early == (entry == 'lightning')
    assert not frames[-1][..., 3].any()
    assert frames[times.index(2100)][..., 3].any()
    tail = [f for t, f in zip(times, frames, strict=True) if end <= t < times[-1]]
    assert all(f[..., 3].any() for f in tail[:3])
    if (entry, exit_) == ('flame', 'flame') and target == 'gpu':
        (tmp_path / 'energy-transition-layout.json').write_text(json.dumps({
            'line': ir['skia_layout']['lines'][0], 'bursts': bursts,
            'features': ir['required_features'], 'display': [start, end], 'colors': ir['fx_color_table'],
        }, sort_keys=True), encoding='utf-8')


@pytest.mark.parametrize('mutation', ['profile', 'foreign_layer', 'front_shockwave', 'entry_ash'])
def test_invalid_energy_transition_keeps_scene(binary, project, mutation):
    ir = ir_for(binary, symbol_source(project, 'flame', 'lightning'))
    bad = copy.deepcopy(ir)
    bursts = bad['track']['lines'][0]['fx_bursts']
    if mutation == 'profile':
        bad['skia_layout']['sing_twinkle_schema'] = 10
    elif mutation == 'transform_profile':
        bad['skia_layout']['character_transform_schema'] = 7
    elif mutation == 'foreign_layer':
        next(b for b in bursts if b['kind'] == 'lightning')['kind'] = 'smoke'
    elif mutation == 'front_shockwave':
        next(b for b in bursts if b['kind'] == 'shockwave')['front'] = True
    else:
        next(b for b in bursts if b['kind'] == 'fireball')['kind'] = 'ash'
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        configure(process, ir)
        before, _ = collect(process, [1100])
        with pytest.raises(NativeRendererError):
            configure(process, bad)
        after, _ = collect(process, [1100], generation=2)
    assert before[0][..., 3].any() and np.array_equal(before, after)


def energy_companion_source(project, companion):
    track, style = symbol_source(project, 'flame', 'lightning')
    if companion == 'fade':
        return track, replace(style, exit_anim='fade')
    return track, replace(style, sing_fx='twinkle')


@pytest.mark.parametrize('companion,feature', [
    ('fade', 'p07_energy_transition_line_animation'), ('stars', 'p07_energy_transition_singing_stars'),
])
def test_energy_companion_keeps_scene_after_missing_capability(binary, project, companion, feature):
    ir = ir_for(binary, energy_companion_source(project, companion))
    bad = copy.deepcopy(ir)
    bad['required_features'].remove(feature)
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        configure(process, ir)
        frames, _ = collect(process, [1100, 1840, 2850])
        replay, _ = collect(process, [2850, 1840, 1100], generation=2)
    assert np.array_equal(frames, replay[::-1]) and all(frame.any() for frame in frames)


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('companion', ['fade', 'stars'])
def test_energy_companion_matches_actual_d2d_gpu(binary, project, companion):
    from test_subtitle_render_gpu_backend import _render_g1_frames
    from test_subtitle_skia_outline_ruby import render_skia

    track, style = energy_companion_source(project, companion)
    ir = ir_for(binary, (track, style))
    line, = ir['track']['lines']
    start, end = line['display_start_ms'], line['display_end_ms']
    times = (start + 150, start + 450, 2100, end - 200, end - 20, end + 40)
    control = replace(style, exit_anim='none') if companion == 'fade' else replace(style, sing_fx='none')
    references = []
    for look in (style, control):
        with NativeRendererProcess() as process:
            _, raw = _render_g1_frames(process, look, times, force_warp=False, track=track,
                                       width=640, height=360, realization_enabled=False)
        references.append([np.frombuffer(frame, np.uint8).reshape(360, 640, 4) for frame in raw])
    reference, bare = references
    assert any(np.count_nonzero(a != b) > 100 for a, b in zip(reference, bare, strict=True))
    gpu, raster = render_skia(binary, ir, times)
    for time, ref, first, second in zip(times, reference, gpu, raster, strict=True):
        assert_particle_region(ref, first, ('energy_companion', companion, time), check_contour=False,
                               coverage=alpha_coverage)
        assert_particle_region(first, second, ('energy_companion_raster', companion, time), check_contour=False,
                               coverage=alpha_coverage)


def outlined_energy_source(project):
    from test_subtitle_skia_secondary_outline import two_outlines

    track, style = symbol_source(project, 'flame', 'lightning')
    _, outlines = two_outlines(project, inner=4, outer=2)
    return track, replace(style, stroke_width_px=4, stroke2_width_px=2, stroke2_enabled=True,
                          karaoke_colors=outlines.karaoke_colors)


def test_outlined_energy_transition_keeps_scene_after_missing_capability(binary, project):
    ir = ir_for(binary, outlined_energy_source(project))
    bad = copy.deepcopy(ir)
    bad['required_features'].remove('p07_energy_transition_outlines')
    times = (1100, 1340, 2850)
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        configure(process, ir)
        frames, _ = collect(process, times)
        replay, _ = collect(process, times[::-1], generation=2)
    assert np.array_equal(frames, replay[::-1]) and all(frame.any() for frame in frames)


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
def test_outlined_energy_transitions_match_actual_d2d_gpu(binary, project):
    from test_subtitle_render_gpu_backend import _render_g1_frames
    from test_subtitle_skia_outline_ruby import render_skia

    track, style = outlined_energy_source(project)
    ir = ir_for(binary, (track, style))
    line, = ir['track']['lines']
    start, end = line['display_start_ms'], line['display_end_ms']
    times = (start + 150, start + 450, 2100, end - 150, end - 20, end + 40)
    frames = []
    for look in (style, replace(style, stroke_width_px=0, stroke2_enabled=False)):
        with NativeRendererProcess() as process:
            _, raw = _render_g1_frames(process, look, times, force_warp=False, track=track,
                                       width=640, height=360, realization_enabled=False)
        frames.append([np.frombuffer(frame, np.uint8).reshape(360, 640, 4) for frame in raw])
    reference, bare = frames
    assert any(np.count_nonzero(a != b) > 100 for a, b in zip(reference[:2], bare[:2], strict=True))
    assert any(np.count_nonzero(a != b) > 100 for a, b in zip(reference[3:5], bare[3:5], strict=True))
    gpu, raster = render_skia(binary, ir, times)
    for time, ref, first, second in zip(times, reference, gpu, raster, strict=True):
        assert_particle_region(ref, first, ('outlined_energy', time), check_contour=False, coverage=alpha_coverage)
        assert_particle_region(first, second, ('outlined_energy_raster', time), check_contour=False,
                               coverage=alpha_coverage)


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('entry,exit_', ENERGY_MODES)
def test_energy_transitions_match_actual_d2d_and_gpu_raster(binary, project, entry, exit_):
    from test_subtitle_render_gpu_backend import _render_g1_frames

    track, style = symbol_source(project, entry, exit_)
    ir = ir_for(binary, (track, style), dpr=1.5)
    times = energy_window_samples(ir)
    with NativeRendererProcess() as process:
        _, raw = _render_g1_frames(process, style, tuple(times), force_warp=False, track=track,
                                   width=640, height=360, dpr=1.5, realization_enabled=False)
    reference = [np.frombuffer(p, np.uint8).reshape(540, 960, 4) for p in raw]
    actual = []
    for target in ('gpu', 'raster'):
        with SkiaRendererProcess(binary, selection=selection(target)) as process:
            configure(process, ir)
            frames, _ = collect(process, times)
        actual.append(frames)
    end = ir['track']['lines'][0]['display_end_ms']
    assert any(ref[..., 3].any() for t, ref in zip(times, reference, strict=True) if t >= end)
    for t, d2d, gpu, raster in zip(times, reference, *actual, strict=True):
        sample = ('energy_transition', entry, exit_, t)
        # Blue fire/bolt layers, then the whole composed frame (text body and every companion layer).
        assert_particle_region(d2d, gpu, sample, check_contour=False)
        assert_particle_region(d2d, gpu, (*sample, 'alpha'), check_contour=False, coverage=alpha_coverage)
        assert_particle_region(gpu, raster, (*sample, 'raster'), check_contour=False, coverage=alpha_coverage)
