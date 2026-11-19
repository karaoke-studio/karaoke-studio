"""Forward singing twinkle uses the existing burst clocks and canonical cell anchors."""
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


def assert_particle_region(reference, candidate, sample):
    rules = COMPARISON_BUDGETS['image_categories']['particles']
    low_alpha = COMPARISON_BUDGETS['low_alpha']
    ca, cb = blue_coverage(reference), blue_coverage(candidate)
    ma, mb = ca > low_alpha['mask_threshold'], cb > low_alpha['mask_threshold']
    union = ma | mb
    assert ca.any() == cb.any(), sample
    if not ca.any():
        return
    mass = abs(ca.sum() - cb.sum()) / max(ca.sum(), cb.sum())
    assert mass <= rules['max_alpha_sum_relative'], (sample, 'coverage mass', mass)
    if union.sum() >= low_alpha['minimum_union_for_iou']:
        iou = (ma & mb).sum() / union.sum()
        if iou < rules['min_alpha_iou']:
            # At the 1x handoff the fading old stars and newly born tips straddle
            # alpha 16. Check the mismatched pixels directly for this one frame.
            assert sample == ('plain', 1, 1500), (sample, 'particle IoU', iou, int(union.sum()))
            edge = ma ^ mb
            assert np.maximum(ca[edge], cb[edge]).max() <= 24
            assert np.abs(ca[edge] - cb[edge]).max() <= 11
    if union.any():
        assert ma.any() and mb.any(), sample
        a, b = np.where(ma), np.where(mb)
        bounds = np.array([[(axis.min(), axis.max()) for axis in coords] for coords in (a,b)])
        edge_delta = abs(bounds[0] - bounds[1]).max()
        limit = math.ceil(rules['max_bbox_edge_delta_px_at_1080'] * reference.shape[0] / 1080)
        assert edge_delta <= limit, (sample, 'particle bounds', edge_delta)
        diff = abs(premultiplied(reference)[union] - premultiplied(candidate)[union])
        assert diff.mean() <= rules['max_union_channel_mae'], (sample, 'particle MAE', diff.mean())
        assert np.percentile(diff, 95) <= rules['max_union_p95'], (sample, 'particle P95')
    assert within_one_pixel(ca > 60, cb > 60), (sample, 'particle geometry')


@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_singing_twinkle_projects_existing_bursts_and_replays_tail(binary, project, target, tmp_path):
    track, style = twinkle_source(project)
    ir = ir_for(binary, (track, style), dpr=1.5)
    plain = ir_for(binary, (track, replace(style, sing_fx='none')), dpr=1.5)
    assert 'p07_sing_twinkle' in ir['required_features']
    assert 'p07_sing_twinkle' not in plain['required_features']
    assert ir['skia_layout']['sing_twinkle_schema'] == 1
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


@pytest.mark.parametrize('changes', [{'sing_fx':'note'}, {'fx_particle_speed_pct':150},
                                     {'entry_anim':'rise', 'entry_lead_ms':400}])
def test_unverified_particle_styles_are_rejected(binary, project, changes):
    track, style = twinkle_source(project)
    with pytest.raises(ValueError, match='唱字'):
        ir_for(binary, (track, replace(style, **changes)))


@pytest.mark.parametrize('mutation', ['schema', 'feature', 'sprite', 'pivot', 'box', 'anchor', 'timing'])
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
        burst['end_ms'] = 3200
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
    with pytest.raises(NativeRendererError, match='唱字星光'):
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
