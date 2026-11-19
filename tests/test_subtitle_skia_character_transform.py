"""char_drip / spin_flip follow the existing GPU's canonical pivots and phases."""
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
from test_subtitle_skia_secondary_outline import two_outlines
from test_subtitle_skia_vector_guides import guide_source
from test_subtitle_skia_vertical import vertical_ruby_source
from test_subtitle_skia_scanline import band
from krok_helper.subtitle_render.domain.models import SubtitleStyleScheme
from krok_helper.subtitle_render.native.backend import NativeRendererError, NativeRendererProcess
from krok_helper.subtitle_render.native.skia_backend import SkiaRendererProcess


# Display 0-3000; cells sung 600-1000-1300-1800-2200, so every entry transform
# (stagger 116ms, 250ms ramp) ends before its cell starts and exit starts at 2400.
TIMES = [0, 30, 65, 125, 180, 200, 251, 300, 366, 400, 450, 500, 599, 700, 2200,
         2399, 2400, 2450, 2500, 2600, 2652, 2700, 2768, 2800, 2884, 2900, 2999, 3000]


def transform_source(project, entry='char_drip', exit='char_drip'):
    track, style = clock_source(project)
    line = track.lines[0]
    shift = 400
    chars = [replace(c, start_ms=c.start_ms + shift) for c in line.chars]
    line = replace(line, chars=chars, end_ms=line.end_ms + shift, wipe_reverse=False)
    return replace(track, lines=[line]), replace(style, right_to_left=False, entry_anim=entry, entry_lead_ms=600,
                                                 exit_anim=exit, exit_fade_ms=600)


@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_ir_carries_canonical_pivots_only_with_the_capability(binary, project, target, tmp_path):
    track, style = transform_source(project, 'spin_flip', 'char_drip')
    ir = ir_for(binary, (track, style), dpr=1.5)
    plain = ir_for(binary, (track, replace(style, entry_anim='none', exit_anim='none')), dpr=1.5)
    assert 'p07_character_transform' in ir['required_features'] and ir['skia_layout']['character_transform_schema'] == 2
    assert 'p07_character_transform' not in plain['required_features']
    assert 'p07_character_fade' not in ir['required_features']
    cells, bare = ir['skia_layout']['lines'][0]['cells'], plain['skia_layout']['lines'][0]['cells']
    keys = ('layout_left', 'layout_right', 'pivot_y')
    assert [{k: v for k, v in c.items() if k not in keys} for c in cells] == bare
    assert ir['track']['lines'][0]['resolved_intervals'] == plain['track']['lines'][0]['resolved_intervals']
    for a, b in zip(cells, cells[1:]):
        assert a['layout_left'] < a['layout_right'] <= b['layout_left'] + 1e-6
    for cell in cells:
        left, top, right, bottom = cell['metrics']['ink']
        assert cell['layout_left'] <= cell['origin_x'] + left and cell['origin_x'] + right <= cell['layout_right']
        # The run box center, never the ink center.
        assert cell['pivot_y'] < cell['baseline_y'] and cell['pivot_y'] != cell['baseline_y'] + (top + bottom) / 2
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, [0, 200, 700, 2600, 3000])
        configure(process, plain)
        steady, _ = collect(process, [700], generation=2)
    assert frames[0][..., 3].max() == 0 and frames[-1][..., 3].max() == 0
    assert np.array_equal(frames[2], steady[0])
    assert not np.array_equal(frames[1], steady[0]) and not np.array_equal(frames[3], steady[0])
    if target == 'gpu':
        (tmp_path / 'character-transform-layout.json').write_text(json.dumps({
            'features': ir['required_features'], 'schema': ir['skia_layout']['character_transform_schema'],
            'cells': cells, 'intervals': ir['track']['lines'][0]['resolved_intervals'],
            'window': [ir['track']['lines'][0]['display_start_ms'], ir['track']['lines'][0]['display_end_ms']],
        }, sort_keys=True), encoding='utf-8')


def test_zero_duration_transform_draws_the_unanimated_frame(binary, project):
    track, style = transform_source(project)
    disabled = replace(style, entry_lead_ms=0, exit_fade_ms=0)
    ir = ir_for(binary, (track, disabled))
    line = ir['track']['lines'][0]
    assert line['entry_duration_ms'] == line['exit_duration_ms'] == 0
    plain = ir_for(binary, (track, replace(disabled, entry_anim='none', exit_anim='none')))
    assert ir['skia_layout']['lines'][0]['cells'] == plain['skia_layout']['lines'][0]['cells']
    times = [0, 110, 700, 2700, 2999, 3000]
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        configure(process, ir)
        frames, _ = collect(process, times)
        configure(process, plain)
        expected, _ = collect(process, times, generation=2)
    assert np.array_equal(frames, expected)


@pytest.mark.parametrize('case', ['outline', 'outer_outline', 'scanline', 'rtl',
                                  'reverse', 'vertical', 'ruby', 'roles', 'guide', 'no_wipe'])
def test_unverified_combinations_are_rejected(binary, project, case):
    track, style = transform_source(project)
    if case == 'outline': style = replace(style, stroke_width_px=6)
    elif case == 'outer_outline': style = replace(two_outlines(project)[1], entry_anim='char_drip', entry_lead_ms=600)
    elif case == 'scanline': style = band(style)
    elif case == 'rtl': style = replace(style, right_to_left=True)
    elif case == 'reverse': track = replace(track, lines=[replace(track.lines[0], wipe_reverse=True)])
    elif case == 'vertical':
        track, style = vertical_ruby_source(project); track = replace(track, rubies=[])
    elif case == 'ruby':
        track, style = vertical_ruby_source(project); style = replace(style, vertical=False)
    elif case == 'roles':
        style = replace(style, custom_style_schemes={'lead': SubtitleStyleScheme(font_size_px=70)})
        track = replace(track, lines=[replace(track.lines[0], chars=[
            replace(track.lines[0].chars[0], role_label='lead'), *track.lines[0].chars[1:]])])
    elif case == 'guide': track, style = guide_source(project)
    elif case == 'no_wipe': style = replace(style, karaoke_anim='no_wipe')
    if case in {'outer_outline', 'vertical', 'ruby', 'guide'}:
        style = replace(style, entry_anim='char_drip', entry_lead_ms=600)
    with pytest.raises(ValueError, match='逐字滴落/翻转'):
        ir_for(binary, (track, style))


@pytest.mark.parametrize('mutation', ['feature', 'schema', True, '1', 3, 'missing_pivot', 'pivot_type',
                                      'inverted_cell', 'unclaimed_pivot', 'overlap'])
def test_forged_transform_ir_keeps_previous_scene(binary, project, mutation):
    ir = ir_for(binary, transform_source(project))
    bad = copy.deepcopy(ir)
    cell = bad['skia_layout']['lines'][0]['cells'][1]
    if mutation == 'feature': bad['required_features'].remove('p07_character_transform')
    elif mutation == 'schema': del bad['skia_layout']['character_transform_schema']
    elif mutation == 'missing_pivot': del cell['pivot_y']
    elif mutation == 'pivot_type': cell['layout_left'] = '1'
    elif mutation == 'inverted_cell': cell['layout_right'] = cell['layout_left'] - 1
    elif mutation == 'unclaimed_pivot':
        bad = ir_for(binary, (transform_source(project)[0], replace(transform_source(project)[1],
                                                                    entry_anim='none', exit_anim='none')))
        bad['skia_layout']['lines'][0]['cells'][0]['layout_left'] = 0
    elif mutation == 'overlap':
        bad['skia_layout']['character_transform_schema'] = 1
        bad['track']['lines'][0]['display_start_ms'] = 400
    else: bad['skia_layout']['character_transform_schema'] = mutation
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        configure(process, ir)
        before, _ = collect(process, [200])
        with pytest.raises(NativeRendererError):
            configure(process, bad)
        after, _ = collect(process, [200], generation=2)
    assert before[0][..., 3].max() > 0 and np.array_equal(before[0], after[0])


@pytest.mark.parametrize('entry,exit', [('char_drip', 'char_drip'), ('spin_flip', 'spin_flip'), ('spin_flip', 'char_drip')])
def test_gpu_and_raster_agree_under_independent_seeks(binary, project, entry, exit):
    ir = ir_for(binary, transform_source(project, entry, exit), dpr=1.5)
    order = [*reversed(TIMES), *TIMES[::3]]
    results = []
    for target in ('gpu', 'raster'):
        with SkiaRendererProcess(binary, selection=selection(target)) as process:
            configure(process, ir)
            frames, _ = collect(process, order)
        by_time = dict(zip(order[:len(TIMES)], frames))
        for t, frame in zip(order[len(TIMES):], frames[len(TIMES):]):
            assert np.array_equal(frame, by_time[t]), (target, t)
        results.append([by_time[t] for t in TIMES])
    for t, a, b in zip(TIMES, *results):
        assert abs(int(a[..., 3].max()) - int(b[..., 3].max())) <= 2, t
        assert within_one_pixel(a[..., 3] > 127, b[..., 3] > 127), t
        delta = abs(premultiplied(a) - premultiplied(b))
        assert delta.mean() < .15 and np.mean(delta.max(axis=-1) > 16) < .003, (t, delta.mean())


def test_old_sidecar_is_rejected_before_backend_selection(binary, monkeypatch):
    process = SkiaRendererProcess(binary, selection=selection('raster'))
    try: ready = NativeRendererProcess.start(process)
    finally: process.abort(reason='handshake fixture')
    ready.pop('character_transform_schema')
    sent = []
    monkeypatch.setattr(NativeRendererProcess, 'start', lambda self: ready)
    monkeypatch.setattr(SkiaRendererProcess, 'abort', lambda *a, **k: None)
    monkeypatch.setattr(SkiaRendererProcess, '_send', lambda self, p: sent.append(p))
    with pytest.raises(NativeRendererError, match='逐字滴落/翻转'): SkiaRendererProcess(binary).start()
    assert not sent


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('entry,exit,dpr', [('char_drip', 'char_drip', 1), ('char_drip', 'char_drip', 1.5),
                                            ('spin_flip', 'spin_flip', 1), ('spin_flip', 'spin_flip', 1.5),
                                            ('char_drip', 'spin_flip', 1.5), ('rise', 'spin_flip', 1),
                                            ('spin_flip', 'slide_out', 1.5), ('char_fade', 'char_drip', 1)])
def test_geometry_and_phase_match_actual_d2d_gpu(binary, project, entry, exit, dpr):
    """Original 1px ink/mask and sung-front budgets, opacity and pivot centroid vs actual D2D GPU (no WARP)."""
    from test_subtitle_render_gpu_backend import _render_g1_frames
    track, style = transform_source(project, entry, exit)
    with NativeRendererProcess() as process:
        _, raw = _render_g1_frames(process, style, tuple(TIMES), force_warp=False, track=track,
                                   width=640, height=360, dpr=dpr, realization_enabled=False)
    refs = [np.frombuffer(p, np.uint8).reshape(round(360*dpr), round(640*dpr), 4) for p in raw]
    with SkiaRendererProcess(binary, selection=selection('gpu')) as process:
        configure(process, ir_for(binary, (track, style), dpr=dpr))
        frames, _ = collect(process, TIMES)
    sung = lambda p: (p[..., 1].astype(float) * p[..., 3] / 255**2).sum()
    steady = TIMES.index(2200)
    full_a, full_b = sung(refs[steady]), sung(frames[steady])
    resolved = []
    for t, a, b in zip(TIMES, refs, frames, strict=True):
        assert (a[..., 3].max() == 0) == (b[..., 3].max() == 0), (entry, exit, t, 'visibility')
        if a[..., 3].max() == 0:
            continue
        # Phase: composed character opacity. Pivot: coverage-weighted centroid.
        peak_a, peak_b = int(a[..., 3].max()), int(b[..., 3].max())
        assert abs(peak_a - peak_b) <= 2, (entry, exit, t, 'opacity')
        def centroid(frame):
            w = frame[..., 3].astype(float)
            return np.array([(w.sum(0) * np.arange(w.shape[1])).sum(), (w.sum(1) * np.arange(w.shape[0])).sum()]) / w.sum()
        assert abs(centroid(a) - centroid(b)).max() <= .5, (entry, exit, t, 'centroid', centroid(a), centroid(b))
        def bounds(mask):
            ys, xs = np.where(mask)
            return np.array([xs.min(), ys.min(), xs.max(), ys.max()])
        # Geometry: the original alpha>127 ink budget whenever both frames resolve it. Fainter
        # spins are scaled to sub-pixel strokes, where coverage measures AA, not geometry.
        if min(peak_a, peak_b) > 128:
            aa, bb = bounds(a[..., 3] > 127), bounds(b[..., 3] > 127)
            assert abs(aa-bb).max() <= 1, (entry, exit, t, 'ink', aa, bb)
            assert within_one_pixel(a[..., 3] > 127, b[..., 3] > 127), (entry, exit, t, 'mask')
            resolved.append(t)
        aa = bounds(a[..., 3] > 0)
        height = aa[3] - aa[1] + 1
        width = (full_a + full_b) / (2 * height)
        assert abs(sung(a)/full_a - sung(b)/full_b) * width <= 1, (entry, exit, t, 'front')
    # Animated geometry, not only the steady line, is held to the original budget.
    assert sum(t < 600 or t > 2400 for t in resolved) >= 8, resolved


OVERLAP_TIMES = [0, 35, 45, 65, 100, 125, 140, 149, 159, 160, 171, 200, 248, 249,
                 250, 251, 260, 275, 330, 365, 400, 599, 700, 1200, 2200]


def singing_overlap(project, effect, handoff=False):
    track, style = transform_source(project, effect, 'none')
    line = track.lines[0]
    chars = [replace(c, start_ms=t) for c, t in zip(line.chars, (40, 150, 260, 380), strict=True)]
    if handoff:
        chars[0] = replace(chars[0], pause_release_ms=140)
    return replace(track, lines=[replace(line, chars=chars, end_ms=1200)]), style


@pytest.mark.parametrize('effect', ['char_drip', 'spin_flip'])
def test_transform_wipe_keeps_same_clock_and_supports_out_of_order_seek(binary, project, effect):
    track, style = singing_overlap(project, effect, handoff=True)
    ir = ir_for(binary, (track, style), dpr=1.5)
    plain = ir_for(binary, (track, replace(style, entry_anim='none')), dpr=1.5)
    assert ir['skia_layout']['character_transform_schema'] == 2
    assert ir['track']['lines'][0]['resolved_intervals'] == plain['track']['lines'][0]['resolved_intervals']
    results = []
    times = [400, 125, 330, 200, 125]
    for target in ('gpu', 'raster'):
        with SkiaRendererProcess(binary, selection=selection(target)) as process:
            configure(process, ir)
            frames, _ = collect(process, times)
        assert np.array_equal(frames[1], frames[-1])
        assert frames[1][..., 1].max() > 0 and frames[1][..., 0].max() > 0
        results.append(frames)
    for time, gpu, raster in zip(times, *results, strict=True):
        assert within_one_pixel(gpu[..., 3] > 127, raster[..., 3] > 127), time
        assert abs(premultiplied(gpu) - premultiplied(raster)).mean() < .13, time


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('effect,handoff,dpr', [('char_drip', False, 1), ('char_drip', True, 1.5),
                                                ('spin_flip', False, 1), ('spin_flip', True, 1.5)])
def test_transform_wipe_front_matches_actual_d2d_gpu(binary, project, effect, handoff, dpr):
    from test_subtitle_render_gpu_backend import _render_g1_frames

    track, style = singing_overlap(project, effect, handoff)
    with NativeRendererProcess() as process:
        _, raw = _render_g1_frames(process, style, tuple(OVERLAP_TIMES), force_warp=False, track=track,
                                   width=640, height=360, dpr=dpr, realization_enabled=False)
    shape = (round(360*dpr), round(640*dpr), 4)
    refs = [np.frombuffer(p, np.uint8).reshape(shape) for p in raw]
    with SkiaRendererProcess(binary, selection=selection('gpu')) as process:
        configure(process, ir_for(binary, (track, style), dpr=dpr))
        frames, _ = collect(process, OVERLAP_TIMES)
    split = 0
    for time, a, b in zip(OVERLAP_TIMES, refs, frames, strict=True):
        pa, pb = premultiplied(a), premultiplied(b)
        peak_a, peak_b = int(a[..., 3].max()), int(b[..., 3].max())
        assert abs(peak_a - peak_b) <= 2, (effect, handoff, dpr, time)
        # At half opacity a 127/128 rounding difference can switch the entire
        # glyph's alpha>127 mask. Resolve faint coverage at half its own peak.
        threshold = 127 if min(peak_a, peak_b) > 128 else min(peak_a, peak_b) // 2
        ma, mb = a[..., 3] > threshold, b[..., 3] > threshold
        assert within_one_pixel(ma, mb), (effect, handoff, dpr, time, 'ink')
        assert abs(pa - pb).mean() < .13, (effect, handoff, dpr, time, 'RGBA')
        if not ma.any() and not mb.any():
            continue
        painted_height = np.count_nonzero((ma | mb).any(axis=1))
        assert abs(pa[..., 1].sum() - pb[..., 1].sum()) / (255 * painted_height) <= 1, (
            effect, handoff, dpr, time, 'sung front')
        if time < 600 and pa[..., 0].max() > 128 and pa[..., 1].max() > 128:
            assert within_one_pixel((pa[..., 1] > 128), (pb[..., 1] > 128)), (
                effect, handoff, dpr, time, 'front shape')
            split += 1
    assert split >= 5
