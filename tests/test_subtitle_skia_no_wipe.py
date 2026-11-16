"""Whole-cell color switches preserve the GPU's final clock endpoints."""
import copy
from dataclasses import replace
import json
import sys

import numpy as np
import pytest

from test_subtitle_skia_standalone_marks import (
    application, binary, prepared_source, unit_source, mark_source, marked, ir_for,
)
from test_subtitle_skia_ruby_units import reading
from test_subtitle_skia_ruby_markers import marker_source, source_with_marker
from test_subtitle_skia_ruby_groups import configure
from test_subtitle_skia_protocol import collect, selection
from test_subtitle_skia_protocol import test_actual_g5_preview_uses_skia_even_when_gpu_off as run_preview
from test_subtitle_skia_protocol import test_product_export_short_mp4_uses_skia as run_mp4
from krok_helper.subtitle_render.domain.timing import LineAnimationOverride
from krok_helper.subtitle_render.engine.ruby.skia_timing import wipe_points_to_ir
from krok_helper.subtitle_render.native.backend import NativeRendererProcess, NativeRendererError
from krok_helper.subtitle_render.native.skia_backend import SkiaRendererProcess


def discrete(source, kind='upper', mode='reading_units'):
    track, style, resolve = (marked(source, lower=kind == 'lower', mode=mode) if kind == 'mark'
                             else reading(source, 'nfc', lower=kind == 'lower', mode=mode))
    if kind in {'main', 'first_zero', 'all_zero'}:
        track = replace(track, rubies=[])
    if kind in {'first_zero', 'all_zero'}:
        line = track.lines[0]
        chars = [replace(line.chars[0], pause_release_ms=200), line.chars[1]]
        if kind == 'all_zero': chars = [replace(c, start_ms=200) for c in chars]
        track = replace(track, lines=[replace(line, chars=chars, end_ms=200 if kind == 'all_zero' else line.end_ms)])
    if kind == 'ruby_zero':
        track = replace(track, rubies=[replace(track.rubies[0], reading_part_ms=[0, 800])])
    return track, replace(style, karaoke_anim='no_wipe'), resolve


@pytest.mark.parametrize('kind', ['main', 'upper', 'lower', 'mark'])
@pytest.mark.parametrize('mode', ['reading_units', 'checkpoint_segments'])
@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_product_switches_only_at_final_endpoints(binary, mark_source, kind, mode, target, tmp_path):
    source = discrete(mark_source, kind, mode)
    ir = ir_for(binary, source)
    smooth = ir_for(binary, (source[0], replace(source[1], karaoke_anim='none'), source[2]))
    assert 'p07_no_wipe' in ir['required_features'] and ir['skia_layout']['no_wipe_schema'] == 1
    # The mode is a drawing policy, never an alternate layout or timing allocation.
    content = lambda layout: {k: v for k, v in layout.items()
                              if k not in {'no_wipe_schema', 'metrics_identity'}}
    assert content(ir['skia_layout']) == content(smooth['skia_layout'])
    assert ir['track']['lines'][0]['resolved_intervals'] == smooth['track']['lines'][0]['resolved_intervals']
    line = ir['skia_layout']['lines'][0]
    ends = [c['wipe_points'][-1]['time_ms'] if 'wipe_points' in c else interval[1]
            for c, interval in zip(line['cells'], ir['track']['lines'][0]['resolved_intervals'], strict=True)]
    ends += [c['wipe_points'][0]['time_ms'] if 'wipe_points' in c else interval[0]
             for c, interval in zip(line['cells'], ir['track']['lines'][0]['resolved_intervals'], strict=True)]
    ends += [t for g in line.get('ruby_groups', []) for c in g['units'] for t in c['interval_ms']]
    boundaries = sorted({0, *ends, 2400})
    times = [t for a, b in zip(boundaries, boundaries[1:]) if b-a > 3 for t in (a+1, b-1)]
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, [*times, times[1], times[0]])
    for a, b in zip(frames[:-2:2], frames[1:-2:2]):
        assert np.array_equal(a, b), (kind, mode, target)
    assert np.array_equal(frames[0], frames[-1]) and np.array_equal(frames[1], frames[-2])
    assert not np.array_equal(frames[0], frames[-3])
    if kind == 'lower' and mode == 'reading_units' and target == 'gpu':
        (tmp_path/'no-wipe-layout.json').write_text(json.dumps({
            'features': ir['required_features'], 'lines': ir['skia_layout']['lines'],
            'intervals': ir['track']['lines'][0]['resolved_intervals'],
            'schema': ir['skia_layout']['no_wipe_schema']}, sort_keys=True), encoding='utf-8')


@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_main_curve_ignores_internal_checkpoints_and_switches_at_end(binary, mark_source, target):
    ir = ir_for(binary, discrete(mark_source, 'main'))
    for c in ir['skia_layout']['lines'][0]['cells']:
        c['wipe_points'] = wipe_points_to_ir([(200, 0), (400, .4), (800, .4), (1000, 1)])
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, [199, 200, 400, 700, 999, 1000, 1001])
    assert all(np.array_equal(frames[0], p) for p in frames[1:5])
    assert np.array_equal(frames[5], frames[6]) and not np.array_equal(frames[4], frames[5])


@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_zero_duration_uses_end_threshold_and_offsets_once(binary, mark_source, target):
    outputs = []
    track, style, resolve = discrete(mark_source, 'main')
    for shifted in (False, True):
        source = (replace(track, meta=replace(track.meta, offset_ms=137)) if shifted else track,
                  replace(style, timing_offset_ms=229) if shifted else style, resolve)
        ir = ir_for(binary, source, dpr=1.5, roll_shift_ms=400 if shifted else 0)
        for cell in ir['skia_layout']['lines'][0]['cells']:
            cell['wipe_points'] = wipe_points_to_ir([(200, 0), (200, 1)])
        with SkiaRendererProcess(binary, selection=selection(target)) as process:
            configure(process, ir)
            frames, _ = collect(process, [t+(766 if shifted else 0) for t in [199, 200, 201, 199]])
        assert np.array_equal(frames[0], frames[-1])
        assert np.array_equal(frames[0], frames[1]) and not np.array_equal(frames[1], frames[2])
        outputs.append(frames)
    assert all(np.array_equal(a, b) for a, b in zip(*outputs, strict=True))


@pytest.mark.parametrize('mutation', ['feature', 'schema', True, '1', 0, 2, None])
def test_missing_or_invalid_capability_keeps_previous_scene(binary, mark_source, mutation):
    ir = ir_for(binary, discrete(mark_source, 'lower'))
    broken = copy.deepcopy(ir)
    if mutation == 'feature': broken['required_features'].remove('p07_no_wipe')
    elif mutation == 'schema': del broken['skia_layout']['no_wipe_schema']
    else: broken['skia_layout']['no_wipe_schema'] = mutation
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        configure(process, ir)
        before, _ = collect(process, [700])
        with pytest.raises(NativeRendererError): configure(process, broken)
        after, _ = collect(process, [700], generation=2)
    assert np.array_equal(before[0], after[0])


def test_line_override_is_resolved_before_no_wipe_capability(binary, mark_source):
    track, style, resolve = discrete(mark_source, 'main')
    line = replace(track.lines[0], animation_override=LineAnimationOverride(karaoke_anim='none'))
    ir = ir_for(binary, (replace(track, lines=[line]), style, resolve))
    assert ir['track']['lines'][0]['karaoke_anim'] == 'none'
    assert 'p07_no_wipe' not in ir['required_features'] and 'no_wipe_schema' not in ir['skia_layout']


@pytest.mark.parametrize('kind', ['main', 'upper', 'lower', 'mark'])
def test_identical_final_scene_on_gpu_and_raster(binary, mark_source, kind):
    ir = ir_for(binary, discrete(mark_source, kind), dpr=1.5)
    outputs = []
    for target in ('gpu', 'raster'):
        with SkiaRendererProcess(binary, selection=selection(target)) as process:
            configure(process, ir)
            frames, _ = collect(process, [199, 200, 599, 600, 999, 1000, 1699, 1700, 1900])
        outputs.append(frames)
    for a, b in zip(*outputs, strict=True):
        def premult(p):
            p = p.astype(float)
            p[..., :3] *= p[..., 3:] / 255
            return p
        delta = abs(premult(a)-premult(b))
        assert delta.mean() < .15 and np.mean(np.max(delta, axis=-1) > 16) < .003


@pytest.mark.parametrize('marker', ['', '^'])
@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_phrase_markers_keep_no_wipe_main_timing(binary, marker_source, marker, target):
    track, style, resolve = source_with_marker(marker_source, marker)
    style = replace(style, karaoke_anim='no_wipe')
    ir = ir_for(binary, (track, style, resolve))
    assert ir['track']['rubies'][0]['reading'] == marker
    assert 'p05_ruby_markers' in ir['required_features'] and 'p07_no_wipe' in ir['required_features']
    assert not any(c.get('wipe_points') for c in ir['skia_layout']['lines'][0]['cells'])
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, [450, 1900, 450])
    assert np.array_equal(frames[0], frames[2]) and not np.array_equal(frames[0], frames[1])


@pytest.mark.parametrize('gpu,missing_loader', [(True, False), (False, False), (True, True)])
def test_real_discrete_preview(binary, mark_source, monkeypatch, tmp_path, gpu, missing_loader):
    run_preview(binary, monkeypatch, tmp_path, discrete(mark_source, 'lower')[:2], gpu, missing_loader)


@pytest.mark.parametrize('gpu,missing_loader', [(True, False), (False, False), (True, True)])
def test_real_discrete_mp4(binary, mark_source, monkeypatch, tmp_path, gpu, missing_loader):
    run_mp4(binary, monkeypatch, tmp_path, discrete(mark_source, 'lower')[:2], gpu, missing_loader)


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('kind', ['main', 'upper', 'lower', 'mark', 'first_zero', 'all_zero'])
@pytest.mark.parametrize('mode', ['reading_units', 'checkpoint_segments'])
@pytest.mark.parametrize('semantics', ['legacy', 'n3_1074'])
@pytest.mark.parametrize('dpr', [1, 1.5])
def test_discrete_matches_actual_d2d_gpu(binary, mark_source, kind, mode, semantics, dpr):
    from test_subtitle_render_gpu_backend import _render_g1_frames
    track, style, resolve = discrete(mark_source, kind, mode)
    style = replace(style, layout_semantics=semantics)
    times = [199, 200, 201, 450, 599, 600, 601, 750, 999, 1000, 1001, 1200, 1699, 1700, 1701, 1900]
    with NativeRendererProcess() as process:
        _, raw = _render_g1_frames(process, style, tuple(times), force_warp=False, track=track,
            width=900, height=600, dpr=dpr, realization_enabled=False)
    with SkiaRendererProcess(binary, selection=selection('gpu')) as process:
        configure(process, ir_for(binary, (track, style, resolve), dpr=dpr))
        frames, _ = collect(process, times)
    refs = [np.frombuffer(p, np.uint8).reshape(round(600*dpr), round(900*dpr), 4) for p in raw]
    def bands(p):
        mask = p[..., 3] > 127
        ys = np.flatnonzero(mask.any(axis=1))
        return [v for v in np.split(ys, np.flatnonzero(np.diff(ys) > 1)+1) if len(v)]
    for t, a, b in zip(times, refs, frames, strict=True):
        aa, bb = bands(a), bands(b)
        assert len(aa) == len(bb) > 0
        for ya, yb in zip(aa, bb, strict=True):
            xa = np.flatnonzero((a[ya[0]:ya[-1]+1, :, 3] > 127).any(axis=0))
            xb = np.flatnonzero((b[yb[0]:yb[-1]+1, :, 3] > 127).any(axis=0))
            assert max(abs(np.array([xa[0], xa[-1], ya[0], ya[-1]])-
                           [xb[0], xb[-1], yb[0], yb[-1]])) <= 1, (kind, mode, semantics, dpr, t)
            lo, hi = max(min(ya[0], yb[0])-2, 0), max(ya[-1], yb[-1])+3
            sung = lambda p: (p[lo:hi, :, 1].astype(float)*p[lo:hi, :, 3]/255**2).sum()
            full_a, full_b = sung(refs[-1]), sung(frames[-1])
            width = (full_a+full_b)/(2*(hi-lo))
            assert abs(sung(a)/full_a-sung(b)/full_b)*width <= 1, (kind, mode, semantics, dpr, t)


def test_old_sidecar_is_rejected_before_backend_selection(binary, monkeypatch):
    process = SkiaRendererProcess(binary, selection=selection('raster'))
    try: ready = NativeRendererProcess.start(process)
    finally: process.abort(reason='handshake fixture')
    ready.pop('no_wipe_schema')
    sent = []
    monkeypatch.setattr(NativeRendererProcess, 'start', lambda self: ready)
    monkeypatch.setattr(SkiaRendererProcess, 'abort', lambda *a, **k: None)
    monkeypatch.setattr(SkiaRendererProcess, '_send', lambda self, p: sent.append(p))
    with pytest.raises(NativeRendererError, match='整字变色'): SkiaRendererProcess(binary).start()
    assert not sent


@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_zero_duration_ruby_is_rejected_without_losing_old_scene(binary, mark_source, target):
    source = discrete(mark_source, 'ruby_zero')
    with pytest.raises(ValueError, match='零时长注音'): ir_for(binary, source)
    track, style, resolve = source
    # Exercise the C++ boundary too, bypassing only the Python product gate.
    bad = ir_for(binary, (track, replace(style, karaoke_anim='none'), resolve))
    bad['track']['lines'][0]['karaoke_anim'] = 'no_wipe'
    bad['required_features'].append('p07_no_wipe')
    bad['skia_layout']['no_wipe_schema'] = 1
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir_for(binary, discrete(mark_source, 'upper')))
        before, _ = collect(process, [700])
        with pytest.raises(NativeRendererError, match='零时长注音'): configure(process, bad)
        after, _ = collect(process, [700], generation=2)
    assert np.array_equal(before[0], after[0])
