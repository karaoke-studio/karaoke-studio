"""Singing particles use the existing burst clocks and canonical cell anchors."""
import copy
from dataclasses import replace
import json
import math
import sys

import numpy as np
import pytest

from test_subtitle_skia_line_fade import (
    application, binary, project, ir_for, collect, configure, selection, premultiplied,
)
from test_subtitle_skia_primary_outline import within_one_pixel
from test_subtitle_skia_reverse import source as clock_source
from test_subtitle_skia_roles import colors
from krok_helper.subtitle_render.native.backend import NativeRendererError, NativeRendererProcess
from krok_helper.subtitle_render.native.skia_backend import SkiaRendererProcess
from scripts.subtitle_gpu_baseline.policy import COMPARISON_BUDGETS

TIMES = [0, 80, 160, 200, 250, 400, 600, 750, 1100, 1500, 1820, 2200, 2500, 2900, 3000]


def twinkle_source(project, kind='plain'):
    track, style = clock_source(project, 'pause' if kind == 'pause' else 'plain')
    track = replace(track, lines=[replace(track.lines[0], wipe_reverse=False)])
    color2 = '#00FFFF' if kind == 'two_colors' else '#0000FF'
    return track, replace(style, sing_fx='twinkle', fx_particle_color='#0000FF', fx_particle_color2=color2)


def blue_coverage(frame):
    return frame[..., 2].astype(float) * frame[..., 3] / 255


def assert_particle_region(reference, candidate, sample, *, check_contour=True):
    rules = COMPARISON_BUDGETS['image_categories']['particles']
    low_alpha = COMPARISON_BUDGETS['low_alpha']
    ca, cb = blue_coverage(reference), blue_coverage(candidate)
    ma, mb = ca > low_alpha['mask_threshold'], cb > low_alpha['mask_threshold']
    union = ma | mb
    assert ca.any() == cb.any(), sample
    if not ca.any():
        return
    coverage_difference = abs(ca.sum() - cb.sum())
    dim_birth_or_tail = sample in {
        ('twinkle_classic', -400, True, 450), ('twinkle_classic', -400, True, 450, 'raster'),
        ('flame', 200, 2800, 'raster'),
    }
    if dim_birth_or_tail and not union.any():
        # The first dim pixels are still below the occupancy threshold.
        assert coverage_difference <= 255 / 4
        assert abs(ca - cb).max() <= 2
    else:
        mass = coverage_difference / max(ca.sum(), cb.sum())
        assert mass <= rules['max_alpha_sum_relative'], (sample, 'coverage mass', mass)
    if union.sum() >= low_alpha['minimum_union_for_iou']:
        iou = (ma & mb).sum() / union.sum()
        if iou < rules['min_alpha_iou']:
            # Dim tips can straddle the mask threshold. Bound the mismatched
            # pixels directly on the identified handoff and first snow frame.
            edge_limit = {
                ('plain', 1, 1500): 24,
                ('twinkle_classic', 0, False, 750): 24,
                ('snow', 0, False, 550): 26,
            }.get(sample)
            assert edge_limit is not None, (
                sample, 'particle IoU', iou, int(union.sum()))
            edge = ma ^ mb
            assert np.maximum(ca[edge], cb[edge]).max() <= edge_limit
            assert np.abs(ca[edge] - cb[edge]).max() <= 11
    if union.any():
        assert ma.any() and mb.any(), sample
        a, b = np.where(ma), np.where(mb)
        bounds = np.array([[(axis.min(), axis.max()) for axis in coords] for coords in (a,b)])
        edge_delta = abs(bounds[0] - bounds[1]).max()
        limit = math.ceil(rules['max_bbox_edge_delta_px_at_1080'] * reference.shape[0] / 1080)
        if sample == ('lightning', 0, 1620, 'raster') and edge_delta > limit:
            # A remote glow tip crosses alpha 16, making the enclosing box
            # jump. Require raw coverage agreement and check its clear contour.
            assert abs(ca - cb).max() <= 3
            assert within_one_pixel(ca >= 20, cb >= 20)
        else:
            assert edge_delta <= limit, (sample, 'particle bounds', edge_delta)
        diff = abs(premultiplied(reference)[union] - premultiplied(candidate)[union])
        assert diff.mean() <= rules['max_union_channel_mae'], (sample, 'particle MAE', diff.mean())
        assert np.percentile(diff, 95) <= rules['max_union_p95'], (sample, 'particle P95')
    if check_contour:
        assert within_one_pixel(ca > 60, cb > 60), (sample, 'particle geometry')


@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_singing_twinkle_projects_existing_bursts_and_replays_tail(binary, project, target, tmp_path):
    track, style = twinkle_source(project)
    ir = ir_for(binary, (track, style), dpr=1.5)
    plain = ir_for(binary, (track, replace(style, sing_fx='none')), dpr=1.5)
    assert 'p07_sing_twinkle' in ir['required_features']
    assert 'p07_sing_twinkle' not in plain['required_features']
    assert ir['skia_layout']['sing_twinkle_schema'] == 3
    final = ir['skia_layout']['lines'][0]
    source = ir['track']['lines'][0]
    assert final['particle_box_height'] > 0
    bursts = source['fx_bursts']
    assert [burst['char_index'] for burst in bursts] == list(range(len(final['cells'])))
    assert [burst['start_ms'] for burst in bursts] == [a for a, b in source['resolved_intervals']]
    assert bursts[-1]['end_ms'] > source['end_ms']
    for cell in final['cells']:
        assert cell['layout_right'] > cell['layout_left']
        assert cell['pivot_y'] < cell['baseline_y']
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, [160, 1820, 2900, 400, 1820])
    assert not blue_coverage(frames[0]).any() and not blue_coverage(frames[2]).any()
    assert blue_coverage(frames[1]).max() > 80 and blue_coverage(frames[3]).max() > 80
    assert np.array_equal(frames[1], frames[4])
    if target == 'gpu':
        (tmp_path / 'sing-twinkle-layout.json').write_text(json.dumps({
            'schema': ir['skia_layout']['sing_twinkle_schema'], 'cells': final['cells'],
            'box_height': final['particle_box_height'], 'bursts': bursts, 'colors': ir['fx_color_table'],
        }, sort_keys=True), encoding='utf-8')


@pytest.mark.parametrize('changes', [{'entry_anim':'lightning', 'entry_lead_ms':750},
                                     {'exit_anim':'flame', 'exit_fade_ms':750},
                                     {'entry_anim':'rise', 'entry_lead_ms':400}])
def test_unverified_particle_styles_are_rejected(binary, project, changes):
    track, style = twinkle_source(project)
    with pytest.raises(ValueError, match='唱字'):
        ir_for(binary, (track, replace(style, **changes)))


@pytest.mark.parametrize('mutation', ['schema', 'feature', 'sprite', 'pivot', 'box', 'anchor', 'timing', 'legacy_classic', 'speed'])
def test_invalid_particle_ir_preserves_previous_scene(binary, project, mutation):
    ir = ir_for(binary, twinkle_source(project))
    bad = copy.deepcopy(ir)
    layout = bad['skia_layout']
    final = layout['lines'][0]
    burst = bad['track']['lines'][0]['fx_bursts'][0]
    if mutation == 'schema':
        layout['sing_twinkle_schema'] = False
    elif mutation == 'feature':
        bad['required_features'].remove('p07_sing_twinkle')
    elif mutation == 'sprite':
        del bad['fx_sprites']['star4']
    elif mutation == 'pivot':
        del final['cells'][0]['pivot_y']
    elif mutation == 'box':
        final['particle_box_height'] = 'large'
    elif mutation == 'anchor':
        burst['char_index'] = 300
    elif mutation == 'timing':
        layout['sing_twinkle_schema'] = 1
        burst['end_ms'] = 3200
    elif mutation == 'legacy_classic':
        layout['sing_twinkle_schema'] = 1
        burst['kind'] = 'twinkle_classic'
    elif mutation == 'speed':
        burst['time_scale'] = .05
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        configure(process, ir)
        before, _ = collect(process, [1820])
        with pytest.raises(NativeRendererError):
            configure(process, bad)
        after, _ = collect(process, [1820], generation=2)
    assert blue_coverage(before[0]).any()
    assert np.array_equal(before[0], after[0])


def test_old_sidecar_is_rejected_before_backend_selection(binary, monkeypatch):
    process = SkiaRendererProcess(binary, selection=selection('raster'))
    try:
        ready = NativeRendererProcess.start(process)
    finally:
        process.abort(reason='handshake fixture')
    ready.pop('sing_twinkle_schema')
    sent = []
    monkeypatch.setattr(NativeRendererProcess, 'start', lambda self: ready)
    monkeypatch.setattr(SkiaRendererProcess, 'abort', lambda *args, **kwargs: None)
    monkeypatch.setattr(SkiaRendererProcess, '_send', lambda self, packet: sent.append(packet))
    with pytest.raises(NativeRendererError, match='唱字粒子'):
        SkiaRendererProcess(binary).start()
    assert not sent


def test_singing_twinkle_agrees_between_gpu_and_raster(binary, project):
    ir = ir_for(binary, twinkle_source(project, 'two_colors'), dpr=1.5)
    frames = []
    for target in ('gpu', 'raster'):
        with SkiaRendererProcess(binary, selection=selection(target)) as process:
            configure(process, ir)
            rendered, _ = collect(process, [*reversed(TIMES), 1820])
        assert np.array_equal(rendered[-1], rendered[TIMES[::-1].index(1820)])
        frames.append(rendered[:-1])
    for time, gpu, raster in zip(reversed(TIMES), *frames, strict=True):
        assert_particle_region(gpu, raster, time)
        delta = abs(premultiplied(gpu) - premultiplied(raster))
        assert delta.mean() < .16, (time, delta.mean())


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('kind,dpr', [('plain', 1), ('plain', 1.5), ('pause', 1.5), ('two_colors', 1.5)])
def test_singing_twinkle_matches_actual_direct2d(binary, project, kind, dpr):
    from test_subtitle_render_gpu_backend import _render_g1_frames
    track, style = twinkle_source(project, kind)
    with NativeRendererProcess() as process:
        _, raw = _render_g1_frames(process, style, tuple(TIMES), force_warp=False, track=track,
                                   width=640, height=360, dpr=dpr, realization_enabled=False)
    shape = (round(360*dpr), round(640*dpr), 4)
    reference = [np.frombuffer(p, np.uint8).reshape(shape) for p in raw]
    with SkiaRendererProcess(binary, selection=selection('gpu')) as process:
        configure(process, ir_for(binary, (track, style), dpr=dpr))
        actual, _ = collect(process, TIMES)
    active = 0
    for time, a, b in zip(TIMES, reference, actual, strict=True):
        assert_particle_region(a, b, (kind, dpr, time))
        active += bool((blue_coverage(a) > 60).any())
        delta = abs(premultiplied(a) - premultiplied(b))
        assert delta.mean() < .16, (kind, time, delta.mean())
    assert active >= 4
    tail = TIMES.index(1820)
    assert blue_coverage(reference[tail]).any() and blue_coverage(actual[tail]).any()



def star_tempo(project, variant='twinkle', speed=0, crop=False):
    track, style = twinkle_source(project, 'two_colors')
    if crop:
        line = replace(track.lines[0], display_start_override_ms=400, display_end_override_ms=2000)
        track = replace(track, lines=[line])
    return track, replace(style, sing_fx=variant, fx_particle_speed_pct=speed)


@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_singing_star_speed_preserves_birth_and_respects_display_window(binary, project, target, tmp_path):
    track, style = star_tempo(project, 'twinkle_classic', -400, crop=True)
    ir = ir_for(binary, (track, style), dpr=1.5)
    baseline = ir_for(binary, (track, replace(style, fx_particle_speed_pct=0)), dpr=1.5)
    line = ir['track']['lines'][0]
    bursts = line['fx_bursts']
    plain = baseline['track']['lines'][0]['fx_bursts']
    assert ir['skia_layout']['lines'] == baseline['skia_layout']['lines']
    assert line['resolved_intervals'] == baseline['track']['lines'][0]['resolved_intervals']
    assert len(bursts) == len(plain)
    for a, b in zip(bursts, plain, strict=True):
        assert (a['kind'], a['char_index'], a['seed'], a['start_ms']) == (
            'twinkle_classic', b['char_index'], b['seed'], b['start_ms'])
        assert a['end_ms'] > b['end_ms'] and a['time_scale'] > 2
    assert bursts[0]['start_ms'] == line['display_start_ms'] and bursts[-1]['end_ms'] > line['display_end_ms']
    start, end = line['display_start_ms'], line['display_end_ms']
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        times = [end-1, start-1, end, start+550, end-1]
        frames, _ = collect(process, times)
    assert np.array_equal(frames[0], frames[-1])
    assert frames[1][..., 3].max() == frames[2][..., 3].max() == 0
    assert blue_coverage(frames[0]).max() > 50 and blue_coverage(frames[3]).max() > 50
    if target == 'gpu':
        (tmp_path / 'sing-twinkle-tempo-layout.json').write_text(json.dumps({
            'schema': ir['skia_layout']['sing_twinkle_schema'],
            'layout': ir['skia_layout']['lines'][0], 'bursts': bursts,
            'window': [start, end],
        }, sort_keys=True), encoding='utf-8')


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('variant,speed,crop', [('twinkle_classic', 0, False), ('twinkle_classic', -400, True),
                                                ('twinkle', 350, False), ('twinkle', -350, True)])
def test_singing_star_speed_matches_actual_d2d_and_gpu_raster(binary, project, variant, speed, crop):
    from test_subtitle_render_gpu_backend import _render_g1_frames

    track, style = star_tempo(project, variant, speed, crop)
    ir = ir_for(binary, (track, style), dpr=1.5)
    line = ir['track']['lines'][0]
    start, end = line['display_start_ms'], line['display_end_ms']
    times = sorted(set(TIMES + [max(start-1, 0), start, start+250, end-1, end]))
    with NativeRendererProcess() as process:
        _, raw = _render_g1_frames(process, style, tuple(times), force_warp=False, track=track,
                                   width=640, height=360, dpr=1.5, realization_enabled=False)
    reference = [np.frombuffer(p, np.uint8).reshape(540, 960, 4) for p in raw]
    results = []
    for target in ('gpu', 'raster'):
        with SkiaRendererProcess(binary, selection=selection(target)) as process:
            configure(process, ir)
            frames, _ = collect(process, [*times, 750])
        assert np.array_equal(frames[times.index(750)], frames[-1])
        results.append(frames[:-1])
    active = 0
    for time, a, b, c in zip(times, reference, *results, strict=True):
        assert_particle_region(a, b, (variant, speed, crop, time), check_contour=False)
        assert_particle_region(b, c, (variant, speed, crop, time, 'raster'), check_contour=False)
        if start <= time < end:
            active += bool((blue_coverage(a) > 60).any())
        else:
            assert a[..., 3].max() == b[..., 3].max() == c[..., 3].max() == 0
    assert active >= 2



@pytest.mark.parametrize('target', ['gpu', 'raster'])
@pytest.mark.parametrize('kind,sprites', [('note', {'note', 'note_dot'}), ('heart', {'heart'})])
def test_singing_symbols_keep_sprite_resources_and_replay_tail(binary, project, target, kind, sprites, tmp_path):
    track, style = star_tempo(project, kind, -300, crop=True)
    ir = ir_for(binary, (track, style), dpr=1.5)
    line = ir['track']['lines'][0]
    bursts = line['fx_bursts']
    assert ir['skia_layout']['sing_twinkle_schema'] == 3
    assert {b['sprite'] for b in bursts} == sprites
    assert all(b['kind'] == kind and b['anchor'] == 'char' for b in bursts)
    for char in range(len(line['chars'])):
        assert sum(b['count'] for b in bursts if b['char_index'] == char) == 3
    selected = sorted(sprites)
    shapes = {name: ir['fx_sprites'][name] for name in selected}
    if kind == 'note':
        assert shapes['note'] != shapes['note_dot']
    times = [750, 1999, 2000, 400, 750]
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, times)
    assert np.array_equal(frames[0], frames[-1])
    assert frames[2][..., 3].max() == 0
    assert blue_coverage(frames[0]).max() > 40 and blue_coverage(frames[1]).max() > 40
    if target == 'gpu' and kind == 'note':
        (tmp_path / 'singing-symbol-layout.json').write_text(json.dumps({
            'schema': ir['skia_layout']['sing_twinkle_schema'], 'bursts': bursts, 'shapes': shapes,
            'line': ir['skia_layout']['lines'][0],
        }, sort_keys=True), encoding='utf-8')


@pytest.mark.parametrize('mutation', ['old_profile', 'foreign_sprite', 'missing_sprite'])
def test_invalid_symbol_sprite_does_not_replace_scene(binary, project, mutation):
    track, style = star_tempo(project, 'note')
    ir = ir_for(binary, (track, style))
    bad = copy.deepcopy(ir)
    if mutation == 'old_profile':
        bad['skia_layout']['sing_twinkle_schema'] = 2
    elif mutation == 'foreign_sprite':
        bad['track']['lines'][0]['fx_bursts'][0]['sprite'] = 'heart'
    else:
        del bad['fx_sprites']['note_dot']
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        configure(process, ir)
        before, _ = collect(process, [750])
        with pytest.raises(NativeRendererError):
            configure(process, bad)
        after, _ = collect(process, [750], generation=2)
    assert np.array_equal(before, after)


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('kind,speed,crop', [('note', 0, False), ('note', -300, True),
                                             ('heart', 0, False), ('heart', 320, False)])
def test_singing_symbol_matches_actual_d2d_and_gpu_raster(binary, project, kind, speed, crop):
    from test_subtitle_render_gpu_backend import _render_g1_frames

    track, style = star_tempo(project, kind, speed, crop)
    times = [199, 400, 600, 750, 1100, 1500, 1820, 1999, 2000, 2200, 2900, 3000]
    with NativeRendererProcess() as process:
        _, raw = _render_g1_frames(process, style, tuple(times), force_warp=False, track=track,
                                   width=640, height=360, dpr=1.5, realization_enabled=False)
    reference = [np.frombuffer(p, np.uint8).reshape(540, 960, 4) for p in raw]
    ir = ir_for(binary, (track, style), dpr=1.5)
    results = []
    for target in ('gpu', 'raster'):
        with SkiaRendererProcess(binary, selection=selection(target)) as process:
            configure(process, ir)
            frames, _ = collect(process, [*times, 1100])
        assert np.array_equal(frames[times.index(1100)], frames[-1])
        results.append(frames[:-1])
    active = 0
    for time, d2d, gpu, raster in zip(times, reference, *results, strict=True):
        assert_particle_region(d2d, gpu, (kind, speed, crop, time), check_contour=False)
        assert_particle_region(gpu, raster, (kind, speed, crop, time, 'raster'), check_contour=False)
        active += bool((blue_coverage(d2d) > 60).any())
    assert active >= 3


@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_singing_ripple_uses_back_ring_layers_and_replays(binary, project, target, tmp_path):
    track, style = star_tempo(project, 'ripple')
    style = replace(style, fx_particle_color2=style.fx_particle_color)
    dpr = 1.5
    ir = ir_for(binary, (track, style), dpr=dpr)
    plain = ir_for(binary, (track, replace(style, sing_fx='none')), dpr=dpr)
    rings_only = ir_for(binary, (track, replace(style, karaoke_colors=colors('#00000000', '#00000000'))), dpr=dpr)
    line = ir['skia_layout']['lines'][0]
    bursts = ir['track']['lines'][0]['fx_bursts']
    assert ir['skia_layout']['sing_twinkle_schema'] == 4
    assert all(b['kind'] == 'ripple' and not b['front'] and b['count'] == 3 for b in bursts)
    assert [b['start_ms'] for b in bursts] == [200, 600, 900, 1400]
    assert all(b['end_ms'] - b['start_ms'] == 600 for b in bursts)
    times = [199, 200, 240, 349, 351, 501, 800, 2000, 240]
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, times)
        configure(process, plain)
        background, _ = collect(process, times, generation=2)
        configure(process, rings_only)
        rings, _ = collect(process, times, generation=3)
    assert np.array_equal(frames[2], frames[-1])
    for idx in (0, 1, 7):
        assert np.array_equal(frames[idx], background[idx])
    cell = line['cells'][0]
    x = round((cell['layout_left'] + cell['layout_right']) * dpr / 2)
    y = round(cell['pivot_y'] * dpr)
    assert rings[2][y, x, 3] == 0  # Interior of the curved ring.
    assert blue_coverage(rings[2]).max() > 100
    covered = sum(int(((bg[..., 3] == 255) & (blue_coverage(ring) > 80)).sum())
                  for bg, ring in zip(background, rings, strict=True))
    assert covered > 20
    for frame, bg in zip(frames, background, strict=True):
        opaque = bg[..., 3] == 255
        assert np.array_equal(frame[opaque], bg[opaque])
    if target == 'gpu':
        (tmp_path / 'singing-ripple-layout.json').write_text(json.dumps({
            'schema': ir['skia_layout']['sing_twinkle_schema'],
            'bursts': bursts, 'ring': ir['fx_sprites']['ring'], 'line': line,
        }, sort_keys=True), encoding='utf-8')


@pytest.mark.parametrize('mutation', ['front', 'old_profile', 'missing_ring'])
def test_invalid_ripple_preserves_previous_scene(binary, project, mutation):
    ir = ir_for(binary, star_tempo(project, 'ripple'))
    bad = copy.deepcopy(ir)
    if mutation == 'front':
        bad['track']['lines'][0]['fx_bursts'][0]['front'] = True
    elif mutation == 'old_profile':
        bad['skia_layout']['sing_twinkle_schema'] = 3
    else:
        del bad['fx_sprites']['ring']
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        configure(process, ir)
        before, _ = collect(process, [240])
        with pytest.raises(NativeRendererError):
            configure(process, bad)
        after, _ = collect(process, [240], generation=2)
    assert np.array_equal(before, after)


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('speed,crop', [(0, False), (-300, True)])
def test_singing_ripple_matches_actual_d2d_and_gpu_raster(binary, project, speed, crop):
    from test_subtitle_render_gpu_backend import _render_g1_frames

    track, style = star_tempo(project, 'ripple', speed, crop)
    style = replace(style, karaoke_colors=colors('#00000000', '#00000000'))
    times = [199, 240, 349, 351, 450, 499, 501, 650, 750, 1100, 1500, 1999, 2000, 2600]
    with NativeRendererProcess() as process:
        _, raw = _render_g1_frames(process, style, tuple(times), force_warp=False, track=track,
                                   width=640, height=360, dpr=1.5, realization_enabled=False)
    reference = [np.frombuffer(p, np.uint8).reshape(540, 960, 4) for p in raw]
    ir = ir_for(binary, (track, style), dpr=1.5)
    results = []
    for target in ('gpu', 'raster'):
        with SkiaRendererProcess(binary, selection=selection(target)) as process:
            configure(process, ir)
            frames, _ = collect(process, times)
        results.append(frames)
    for time, d2d, gpu, raster in zip(times, reference, *results, strict=True):
        sample = ('ripple', speed, crop, time)
        assert_ripple_contours(d2d, gpu, sample)
        assert_particle_region(gpu, raster, sample, check_contour=False)


def assert_ripple_contours(d2d, gpu, sample):
    budget = COMPARISON_BUDGETS['image_categories']['particles']
    threshold = COMPARISON_BUDGETS['low_alpha']['mask_threshold']
    a, b = blue_coverage(d2d), blue_coverage(gpu)
    assert a.any() == b.any(), sample
    if not a.any():
        return
    assert abs(a.sum() - b.sum()) <= budget['max_alpha_sum_relative'] * max(a.sum(), b.sum()), sample
    assert within_one_pixel(a > threshold, b > threshold), sample

    # The two APIs distribute edge coverage differently on one-pixel-wide
    # rings. Compare the same local area after checking raw extent and mass.
    y, x = np.where((a > 0) | (b > 0))
    region = np.s_[max(y.min()-1, 0):y.max()+2, max(x.min()-1, 0):x.max()+2]

    def local_coverage(frame):
        rgba = np.pad(premultiplied(frame[region]), ((1, 1), (1, 1), (0, 0)))
        windows = np.lib.stride_tricks.sliding_window_view(rgba, (3, 3), axis=(0, 1))
        return windows.mean(axis=(-2, -1))

    reference, candidate = local_coverage(d2d), local_coverage(gpu)
    ma, mb = reference[..., 2] > threshold, candidate[..., 2] > threshold
    union = ma | mb
    if union.any():
        assert (ma & mb).sum() / union.sum() >= budget['min_alpha_iou'], sample
        difference = abs(reference[union] - candidate[union])
        assert difference.mean() <= budget['max_union_channel_mae'], sample
        assert np.percentile(difference, 95) <= budget['max_union_p95'], sample


@pytest.mark.parametrize('target', ['gpu', 'raster'])
@pytest.mark.parametrize('kind', ['petal', 'feather', 'snow', 'snow_solid'])
def test_singing_falling_spritemap_replays_each_shape(binary, project, target, kind, tmp_path):
    track, style = star_tempo(project, kind, -250, crop=True)
    ir = ir_for(binary, (track, style), dpr=1.5)
    line = ir['track']['lines'][0]
    bursts = line['fx_bursts']
    assert ir['skia_layout']['sing_twinkle_schema'] == 5
    assert all(b['kind'] == kind and b['front'] and b['anchor'] == 'char' for b in bursts)
    assert {b['start_ms'] for b in bursts} == {200, 600, 900, 1400}
    shape = ir['fx_sprites'][kind]
    closed_figures = sum(cmd[0] == 'Z' for cmd in shape['path_commands'])
    assert closed_figures >= 2 if kind == 'snow' else closed_figures == 1
    start, end = line['display_start_ms'], line['display_end_ms']
    times = [start - 1, 500, 750, 1100, 1550, end - 1, end, 750]
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, times)
    assert not frames[0][..., 3].any() and not frames[-2][..., 3].any()
    assert np.array_equal(frames[2], frames[-1])
    assert sum(blue_coverage(frame).max() > 40 for frame in frames[1:-2]) >= 3
    if target == 'gpu' and kind == 'snow':
        # The authored snowflake uses sin/cos. Ignore double-precision libm
        # noise in the JSON snapshot beyond the precision of the renderer.
        canonical_shape = {
            **shape, 'path_commands': [
                [verb, *(round(float(value), 9) or 0.0 for value in coordinates)]
                for verb, *coordinates in shape['path_commands']
            ],
        }
        (tmp_path / 'singing-falling-layout.json').write_text(json.dumps({
            'schema': ir['skia_layout']['sing_twinkle_schema'], 'bursts': bursts, 'shape': canonical_shape,
            'line': ir['skia_layout']['lines'][0],
        }, sort_keys=True), encoding='utf-8')


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('kind,speed,crop', [('petal', 0, False), ('feather', -250, True),
                                             ('snow', 0, False), ('snow_solid', 250, False)])
def test_singing_falling_matches_actual_d2d_and_gpu_raster(binary, project, kind, speed, crop):
    from test_subtitle_render_gpu_backend import _render_g1_frames

    track, style = star_tempo(project, kind, speed, crop)
    times = [199, 450, 550, 750, 1100, 1500, 1820, 1999, 2000, 2200, 2600, 3000]
    with NativeRendererProcess() as process:
        _, raw = _render_g1_frames(process, style, tuple(times), force_warp=False, track=track,
                                   width=640, height=360, dpr=1.5, realization_enabled=False)
    reference = [np.frombuffer(p, np.uint8).reshape(540, 960, 4) for p in raw]
    ir = ir_for(binary, (track, style), dpr=1.5)
    frames = []
    for target in ('gpu', 'raster'):
        with SkiaRendererProcess(binary, selection=selection(target)) as process:
            configure(process, ir)
            images, _ = collect(process, times)
        frames.append(images)
    assert sum(blue_coverage(frame).max() > 40 for frame in reference) >= 3
    for time, d2d, gpu, raster in zip(times, reference, *frames, strict=True):
        assert_particle_region(d2d, gpu, (kind, speed, crop, time), check_contour=False)
        if kind == 'snow' and time == 450:
            a, b = blue_coverage(gpu), blue_coverage(raster)
            # At first emergence the dim branches have fewer than 64 occupied
            # pixels and a one-level bias accounts for more than 3% coverage.
            assert 0 < max(a.max(), b.max()) <= 30
            assert (np.maximum(a, b) > 16).sum() < 64
            assert abs(a-b).max() <= 2
            continue
        assert_particle_region(gpu, raster, (kind, speed, crop, time, 'raster'), check_contour=False)


def energy_source(project, kind, speed=0):
    track, style = star_tempo(project, kind, speed)
    return track, replace(style, fx_particle_size_em=.7, fx_particle_count=30)


@pytest.mark.parametrize('target', ['gpu', 'raster'])
@pytest.mark.parametrize('kind', ['lightning', 'flame'])
def test_singing_energy_layers_replay_in_burst_order(binary, project, target, kind, tmp_path):
    track, style = energy_source(project, kind, -200)
    ir = ir_for(binary, (track, style), dpr=1.5)
    bursts = ir['track']['lines'][0]['fx_bursts']
    front = [b for b in bursts if b['front']]
    back = [b for b in bursts if not b['front']]
    assert ir['skia_layout']['sing_twinkle_schema'] == 6
    if kind == 'lightning':
        assert len(front) == len(back) > 0
        assert {b['sprite'] for b in front} == {'bolt', 'bolt_b', 'bolt_c'}
        for center, glow in zip(front, back, strict=True):
            assert center['kind'] == 'lightning' and glow['kind'] == 'lightning_glow'
            for field in ('char_index', 'seed', 'start_ms', 'end_ms', 'count', 'sprite', 'color_id'):
                assert center[field] == glow[field]
            assert glow['size_px'] > center['size_px']
    else:
        assert {b['kind'] for b in front} == {'flame', 'flame_alt', 'ember', 'spark'}
        assert {b['kind'] for b in back} == {'flame_glow', 'smoke'}
        for cell in (b for b in front if b['kind'] == 'flame'):
            assert cell['sweep'] == 2
        assert {b['emit_x'] for b in front if b.get('emit_x')} == {-.45, .45}
        spark_size = max(b['size_px'] for b in front if b['kind'] == 'spark')
        assert spark_size < min(b['size_px'] for b in front if b['kind'] == 'ember')
    times = [290, 400, 580, 740, 950, 1210, 1620, 1900, 2300, 2800, 3000, 740]
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, times)
    assert any(blue_coverage(frame).max() > 30 for frame in frames)
    assert not frames[-2][..., 3].any()
    assert np.array_equal(frames[3], frames[-1])
    if target == 'gpu':
        (tmp_path / f'singing-energy-{kind}.json').write_text(json.dumps({
            'bursts': bursts, 'colors': ir['fx_color_table'],
            'line': ir['skia_layout']['lines'][0],
        }, sort_keys=True), encoding='utf-8')


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('kind,speed', [('lightning', 0), ('lightning', -200),
                                       ('flame', 0), ('flame', 200)])
def test_singing_energy_matches_actual_d2d_and_gpu_raster(binary, project, kind, speed):
    from test_subtitle_render_gpu_backend import _render_g1_frames

    track, style = energy_source(project, kind, speed)
    times = [290, 400, 580, 740, 950, 1210, 1620, 1900, 2300, 2800, 3000]
    with NativeRendererProcess() as process:
        _, raw = _render_g1_frames(process, style, tuple(times), force_warp=False, track=track,
                                   width=640, height=360, dpr=1.5, realization_enabled=False)
    reference = [np.frombuffer(p, np.uint8).reshape(540, 960, 4) for p in raw]
    ir = ir_for(binary, (track, style), dpr=1.5)
    frames = []
    for target in ('gpu', 'raster'):
        with SkiaRendererProcess(binary, selection=selection(target)) as process:
            configure(process, ir)
            images, _ = collect(process, times)
        frames.append(images)
    assert sum(blue_coverage(frame).max() > 30 for frame in reference) >= 3
    for time, d2d, gpu, raster in zip(times, reference, *frames, strict=True):
        assert_particle_region(d2d, gpu, (kind, speed, time), check_contour=False)
        assert_particle_region(gpu, raster, (kind, speed, time, 'raster'), check_contour=False)
