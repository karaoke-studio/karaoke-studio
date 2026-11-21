"""Reverse singing keeps source glyphs/paints and reverses final clock ownership."""
import copy
from dataclasses import replace
import json
import sys

import numpy as np
import pytest

from test_subtitle_skia_roles import application, binary, project, mixed, roles, colors
from test_subtitle_skia_grid import grid
from test_subtitle_skia_resolved import ir_for
from test_subtitle_skia_protocol import collect, selection
from test_subtitle_skia_protocol import test_actual_g5_preview_uses_skia_even_when_gpu_off as run_preview
from test_subtitle_skia_protocol import test_product_export_short_mp4_uses_skia as run_mp4
from test_subtitle_skia_ruby_groups import configure
from krok_helper.subtitle_render.domain.timing import TimingChar, TimingLine
from krok_helper.subtitle_render.engine.render.skia_ir import build_skia_render_ir
from krok_helper.subtitle_render.engine.render.skia_direction_ir import project_reverse_wipe
from krok_helper.subtitle_render.native.backend import NativeRendererProcess, NativeRendererError
from krok_helper.subtitle_render.native.skia_backend import SkiaRendererProcess


TIMES = [199, 200, 201, 300, 599, 600, 601, 750, 899, 900, 901, 1200,
         1399, 1400, 1401, 1600, 1799, 1800, 1801, 2200]


def source(project, kind='plain', effect='none'):
    track, style = project
    chars = [TimingChar(t, at) for t, at in zip(['A', '字', '한', 'W'], [200, 600, 900, 1400])]
    end = 1800
    if kind == 'one': chars = chars[:1]
    if kind == 'shared':
        chars = [replace(c, start_ms=200, source_span_start_ms=200, source_span_end_ms=end,
                         source_span_index=i, source_span_count=len(chars)) for i, c in enumerate(chars)]
    if kind in {'pause', 'zero'}:
        chars[0] = replace(chars[0], pause_release_ms=400 if kind == 'pause' else 200)
    if kind == 'all_zero': chars = [replace(c, start_ms=200) for c in chars]; end = 200
    line = TimingLine(chars=chars, end_ms=end, wipe_reverse=True,
                      display_start_override_ms=0, display_end_override_ms=3000)
    return replace(track, lines=[line], rubies=[]), replace(style, karaoke_anim='none',
        reverse_karaoke_anim=effect, lower_ruby_enabled=False, entry_anim='none', exit_anim='none',
        karaoke_colors=colors('#FF0000', '#00FF00'))


@pytest.mark.parametrize('kind', ['plain', 'shared', 'pause', 'zero', 'all_zero', 'one'])
@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_final_clock_map_keeps_geometry_and_authored_intervals(binary, project, kind, target, tmp_path):
    track, style = source(project, kind)
    ir = ir_for(binary, (track, style))
    plain = ir_for(binary, (replace(track, lines=[replace(track.lines[0], wipe_reverse=False)]), style))
    assert ir['track']['lines'][0]['resolved_intervals'] == plain['track']['lines'][0]['resolved_intervals']
    cells = ir['skia_layout']['lines'][0]['cells']
    assert [c['clock_index'] for c in cells] == list(range(len(cells)-1, -1, -1))
    strip = lambda c: {k: v for k, v in c.items() if k not in {'clock_index', 'wipe_end_position'}}
    assert [strip(c) for c in cells] == [strip(c) for c in plain['skia_layout']['lines'][0]['cells']]
    # Every reverse cell carries the GPU's right-to-left endpoint; the
    # leftmost cell is sung last and keeps the basic endpoint.
    assert all(0 <= c['wipe_end_position'] <= 1 for c in cells) and cells[0]['wipe_end_position'] == 1
    assert 'p05_reverse_overlap' in ir['required_features'] and ir['skia_layout']['reverse_overlap_schema'] == 1
    assert 'p05_overlap_wipe' in ir['required_features'] and ir['skia_layout']['overlap_schema'] == 1
    assert ir['skia_layout']['reverse_wipe_schema'] == 1 and 'p05_reverse_wipe' in ir['required_features']
    assert all(not run.get('rtl', False) for run in ir['skia_layout']['runs'])
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, [*TIMES, 750, 300])
    assert np.array_equal(frames[7], frames[-2]) and np.array_equal(frames[3], frames[-1])
    assert not np.array_equal(frames[0], frames[-3])
    if kind == 'shared' and target == 'gpu':
        (tmp_path/'reverse-wipe-layout.json').write_text(json.dumps({
            'features': ir['required_features'], 'cells': cells,
            'intervals': ir['track']['lines'][0]['resolved_intervals']}, sort_keys=True), encoding='utf-8')


def test_final_clock_projection_moves_curves_without_moving_source_cells():
    points = [[{'time_ms': 100*i, 'position': 0}, {'time_ms': 100*i+50, 'position': 1}] for i in range(3)]
    ends = [1, .9, .8]
    ir = {'track': {'lines': [{'wipe_reverse': True}]}, 'required_features': ['p05_overlap_wipe'],
          'skia_layout': {'overlap_schema': 1, 'lines': [{'cells': [{'source': i, 'wipe_points': p, 'wipe_end_position': e}
                                               for i, (p, e) in enumerate(zip(points, ends))]}]}}
    unadjusted = copy.deepcopy(ir)
    project_reverse_wipe(ir)
    cells = ir['skia_layout']['lines'][0]['cells']
    assert [c['source'] for c in cells] == [0, 1, 2]
    assert [c['wipe_points'] for c in cells] == points[::-1]
    # Endpoints are geometry of the source cell, never moved with the clock.
    assert [c['wipe_end_position'] for c in cells] == ends
    with pytest.raises(ValueError, match='重复投影'): project_reverse_wipe(ir)
    undeclared = copy.deepcopy(unadjusted)
    undeclared['required_features'].remove('p05_overlap_wipe')
    with pytest.raises(ValueError, match='重叠走字能力'): project_reverse_wipe(undeclared)
    del unadjusted['skia_layout']['lines'][0]['cells'][1]['wipe_end_position']
    with pytest.raises(ValueError, match='重叠终点'): project_reverse_wipe(unadjusted)


REVERSE_DECORATIONS = (
    ('glow', 'glow'), ('shadow', 'shadow'), ('shadow', 'glow'),
    ('glow', 'shadow'), ('shadow', 'none'), ('none', 'shadow'),
)


def decorated_reverse(project, before, after, effect, entry='none', leave='none'):
    from test_subtitle_skia_static_glow import glow_source

    track, style = glow_source(project, 'split')
    track = replace(track, lines=[replace(line, wipe_reverse=True) for line in track.lines])
    style = replace(style, decoration_kind_before=before, decoration_kind=after, reverse_karaoke_anim=effect,
                    entry_anim=entry, exit_anim=leave, entry_lead_ms=600, exit_fade_ms=600)
    return track, style


@pytest.mark.parametrize('before,after', REVERSE_DECORATIONS)
@pytest.mark.parametrize('target', ['gpu', 'raster'])
@pytest.mark.parametrize('effect', ['none', 'no_wipe'])
def test_reverse_decoration_replays_with_translucent_outlines(binary, project, before, after, target, effect):
    track, style = decorated_reverse(project, before, after, effect)
    ir = ir_for(binary, (track, style), dpr=1.5)
    feature = 'p07_reverse_decoration_no_wipe' if effect == 'no_wipe' else 'p06_reverse_decoration'
    assert feature in ir['required_features']
    assert ir['track']['lines'][0]['wipe_reverse']
    assert ir['track']['lines'][0]['karaoke_anim'] == effect
    missing = copy.deepcopy(ir)
    missing['required_features'].remove(feature)
    times = [199, 399, 750, 1200, 1801]
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, times)
        with pytest.raises(NativeRendererError):
            configure(process, missing)
        replay, _ = collect(process, times[::-1], generation=2)
    assert all(frame.any() for frame in frames)
    assert not np.array_equal(frames[0], frames[-1])
    assert all(np.array_equal(frame, repeated) for frame, repeated in zip(frames, replay[::-1], strict=True))


def assert_reverse_decoration_parity(binary, track, style, times, case):
    from test_subtitle_render_gpu_backend import _render_g1_frames
    from test_subtitle_skia_static_glow import assert_glow_parity

    dpr = 1.5
    with NativeRendererProcess() as process:
        _, raw = _render_g1_frames(process, style, times, force_warp=False, track=track,
                                   width=640, height=360, dpr=dpr, realization_enabled=False)
    reference = [np.frombuffer(frame, np.uint8).reshape(540, 960, 4) for frame in raw]
    ir = ir_for(binary, (track, style), dpr=dpr)
    rendered = []
    for target in ('gpu', 'raster'):
        with SkiaRendererProcess(binary, selection=selection(target)) as process:
            configure(process, ir)
            frames, _ = collect(process, times)
        rendered.append(frames)
    assert_glow_parity(times, reference, *rendered, case, check_p00_edges=True)


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('before,after', REVERSE_DECORATIONS)
@pytest.mark.parametrize('effect', ['none', 'no_wipe'])
def test_reverse_decoration_matches_actual_d2d_and_gpu_raster(binary, project, before, after, effect):
    track, style = decorated_reverse(project, before, after, effect)
    times = [199, 200, 300, 399, 599, 600, 750, 899, 900, 1200, 1799, 1800, 2200]
    assert_reverse_decoration_parity(binary, track, style, times, (before, after, effect))


REVERSE_DECORATION_MOTION = (
    ('shadow', 'shadow', 'none', 'fade', 'fade'),
    ('glow', 'glow', 'no_wipe', 'slide_in', 'slide_out'),
    ('shadow', 'glow', 'no_wipe', 'rise', 'rise'),
)


@pytest.mark.parametrize('target', ['gpu', 'raster'])
@pytest.mark.parametrize('case', REVERSE_DECORATION_MOTION)
def test_reverse_decoration_motion_replays(binary, project, case, target):
    track, style = decorated_reverse(project, *case)
    ir = ir_for(binary, (track, style), dpr=1.5)
    feature = 'p07_reverse_decoration_line_animation'
    assert feature in ir['required_features']
    obsolete = copy.deepcopy(ir)
    obsolete['required_features'].remove(feature)
    times = [0, 1, 450, 900, 2400, 2700, 3000]
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, times)
        with pytest.raises(NativeRendererError):
            configure(process, obsolete)
        replay, _ = collect(process, times[::-1], generation=2)
    assert not frames[0].any() and not frames[-1].any()
    assert all(frame.any() for frame in frames[2:-1])
    assert all(np.array_equal(frame, repeated) for frame, repeated in zip(frames, replay[::-1], strict=True))


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('case', REVERSE_DECORATION_MOTION)
def test_reverse_decoration_motion_matches_actual_d2d_and_gpu_raster(binary, project, case):
    track, style = decorated_reverse(project, *case)
    times = [0, 1, 100, 250, 450, 599, 600, 750, 1200, 1799, 2399, 2400, 2745, 2999, 3000]
    assert_reverse_decoration_parity(binary, track, style, times, case)


@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_no_wipe_roles_keep_glyph_font_and_paint_ownership(binary, roles, target):
    track, style = roles
    line = replace(track.lines[0], wipe_reverse=True, display_start_override_ms=0, display_end_override_ms=3000)
    ir = ir_for(binary, (replace(track, lines=[line]), replace(style, reverse_karaoke_anim='no_wipe')))
    intervals = ir['track']['lines'][0]['resolved_intervals']
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, [100, 750, 2500])
    for c in ir['skia_layout']['lines'][0]['cells']:
        ink = c['metrics']['ink']
        x0, x1 = int(np.floor(c['origin_x']+ink[0])), int(np.ceil(c['origin_x']+ink[2]))
        y0, y1 = int(np.floor(c['baseline_y']+ink[1])), int(np.ceil(c['baseline_y']+ink[3]))
        expected = frames[2] if intervals[c['clock_index']][1] <= 750 else frames[0]
        assert np.array_equal(frames[1][y0:y1, x0:x1], expected[y0:y1, x0:x1])


@pytest.mark.parametrize('effect', ['none', 'no_wipe'])
def test_gpu_raster_offsets_and_partial_curve_are_identical(binary, project, effect):
    track, style = source(project, effect=effect)
    outputs = []
    for target in ('gpu', 'raster'):
        shifted_results = []
        for shifted in (False, True):
            ir = ir_for(binary, (replace(track, meta=replace(track.meta, offset_ms=137)) if shifted else track,
                replace(style, timing_offset_ms=229) if shifted else style), dpr=1.5,
                roll_shift_ms=400 if shifted else 0)
            for cell in ir['skia_layout']['lines'][0]['cells']:
                cell['wipe_points'] = [{'time_ms': 200, 'position': 0}, {'time_ms': 400, 'position': .5},
                                       {'time_ms': 1000, 'position': .5}, {'time_ms': 1800, 'position': 1}]
            with SkiaRendererProcess(binary, selection=selection(target)) as process:
                configure(process, ir)
                frames, _ = collect(process, [t+(766 if shifted else 0) for t in [199, 700, 900, 1900, 700]])
            assert np.array_equal(frames[1], frames[2]) and np.array_equal(frames[1], frames[-1])
            shifted_results.append(frames)
        assert all(np.array_equal(a, b) for a, b in zip(*shifted_results, strict=True))
        outputs.append(shifted_results[0])
    for a, b in zip(*outputs, strict=True):
        def premult(p):
            p = p.astype(float); p[..., :3] *= p[..., 3:] / 255
            return p
        delta = abs(premult(a)-premult(b))
        assert delta.mean() < .15 and np.mean(delta.max(axis=-1) > 16) < .003


def test_mixed_forward_reverse_pages_keep_the_existing_schedule(binary, grid):
    track, style = grid
    track = replace(track, lines=[replace(l, wipe_reverse=i % 2 == 1) for i, l in enumerate(track.lines)])
    ir = ir_for(binary, (track, style))
    forward = ir_for(binary, (replace(track, lines=[replace(l, wipe_reverse=False) for l in track.lines]), style))
    for a, b, final in zip(ir['track']['lines'], forward['track']['lines'], ir['skia_layout']['lines'], strict=True):
        assert {k: a[k] for k in ('lane', 'display_start_ms', 'display_end_ms', 'resolved_intervals')} == {
            k: b[k] for k in ('lane', 'display_start_ms', 'display_end_ms', 'resolved_intervals')}
        assert all(('clock_index' in c) == a['wipe_reverse'] for c in final['cells'])
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        configure(process, ir); frames, _ = collect(process, [1500, 3500, 6500, 1500])
    assert np.array_equal(frames[0], frames[-1])


@pytest.mark.parametrize('mutation', ['feature', 'schema', True, '1', 2, 'missing_clock', 'duplicate', 'fraction', 'bool_clock', 'flag', 'forward', 'overlap', 'reverse_overlap', 'reverse_overlap_schema'])
def test_invalid_direction_contract_keeps_old_scene(binary, project, mutation):
    ir = ir_for(binary, source(project)); bad = copy.deepcopy(ir)
    cell = bad['skia_layout']['lines'][0]['cells'][0]
    if mutation == 'feature': bad['required_features'].remove('p05_reverse_wipe')
    elif mutation == 'schema': del bad['skia_layout']['reverse_wipe_schema']
    elif mutation == 'missing_clock': del cell['clock_index']
    elif mutation == 'duplicate': cell['clock_index'] = 0
    elif mutation == 'fraction': cell['clock_index'] -= .5
    elif mutation == 'bool_clock': cell['clock_index'] = True
    elif mutation == 'flag': bad['track']['lines'][0]['wipe_reverse'] = 'true'
    elif mutation == 'forward': bad['track']['lines'][0]['wipe_reverse'] = False
    elif mutation == 'overlap': bad['required_features'].remove('p05_overlap_wipe')
    elif mutation == 'reverse_overlap': bad['required_features'].remove('p05_reverse_overlap')
    elif mutation == 'reverse_overlap_schema': bad['skia_layout']['reverse_overlap_schema'] = 2
    else: bad['skia_layout']['reverse_wipe_schema'] = mutation
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        configure(process, ir); before, _ = collect(process, [750])
        with pytest.raises(NativeRendererError): configure(process, bad)
        after, _ = collect(process, [750], generation=2)
    assert np.array_equal(before[0], after[0])


@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_reconfigure_cannot_keep_previous_direction(binary, project, target):
    reverse = ir_for(binary, source(project))
    track, style = source(project)
    forward = ir_for(binary, (replace(track, lines=[replace(track.lines[0], wipe_reverse=False)]), style))
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        outputs = []
        for generation, ir in enumerate([forward, reverse, forward], 1):
            configure(process, ir); frames, _ = collect(process, [750], generation=generation); outputs.append(frames[0])
    assert np.array_equal(outputs[0], outputs[2]) and not np.array_equal(outputs[0], outputs[1])


@pytest.mark.parametrize('blank', [False, True])
@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_empty_reverse_line_is_transparent(binary, project, blank, target):
    track, style = source(project)
    line = replace(track.lines[0], chars=[TimingChar(' ', 200)] if blank else [])
    ir = ir_for(binary, (replace(track, lines=[line]), style))
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir); frames, _ = collect(process, [199, 750, 2200])
    assert all(not p.any() for p in frames)


# Annotation tracks are covered by test_subtitle_skia_reverse_ruby.py.
@pytest.mark.parametrize('flag', [1, 'true', None])
def test_unverified_reverse_sources_fail_before_font_discovery(project, flag):
    track, style = source(project)
    track = replace(track, lines=[replace(track.lines[0], wipe_reverse=flag)])
    fail = lambda *a, **k: pytest.fail('unsupported source reached font discovery')
    with pytest.raises(ValueError, match='反向走字'):
        build_skia_render_ir(track, style, measure=fail, resolve_font=fail, width=640, height=360, fps=60)


@pytest.mark.parametrize('gpu,missing_loader', [(True, False), (False, False), (True, True)])
def test_real_reverse_preview(binary, project, monkeypatch, tmp_path, gpu, missing_loader):
    run_preview(binary, monkeypatch, tmp_path, source(project), gpu, missing_loader)


@pytest.mark.parametrize('gpu,missing_loader', [(True, False), (False, False), (True, True)])
def test_real_reverse_mp4(binary, project, monkeypatch, tmp_path, gpu, missing_loader):
    run_mp4(binary, monkeypatch, tmp_path, source(project), gpu, missing_loader)


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('kind', ['plain', 'shared', 'pause', 'zero', 'all_zero', 'one'])
@pytest.mark.parametrize('effect', ['none', 'no_wipe'])
@pytest.mark.parametrize('semantics', ['legacy', 'n3_1074'])
@pytest.mark.parametrize('dpr', [1, 1.5])
def test_reverse_matches_actual_d2d_gpu(binary, project, kind, effect, semantics, dpr):
    from test_subtitle_render_gpu_backend import _render_g1_frames
    track, style = source(project, kind, effect)
    style = replace(style, layout_semantics=semantics)
    with NativeRendererProcess() as process:
        _, raw = _render_g1_frames(process, style, tuple(TIMES), force_warp=False, track=track,
            width=640, height=360, dpr=dpr, realization_enabled=False)
    with SkiaRendererProcess(binary, selection=selection('gpu')) as process:
        configure(process, ir_for(binary, (track, style), dpr=dpr)); frames, _ = collect(process, TIMES)
    refs = [np.frombuffer(p, np.uint8).reshape(round(360*dpr), round(640*dpr), 4) for p in raw]
    for t, a, b in zip(TIMES, refs, frames, strict=True):
        def box(p):
            ys, xs = np.where(p[..., 3] > 127)
            assert len(xs)
            return np.array([xs.min(), ys.min(), xs.max(), ys.max()])
        bounds = box(a)
        assert np.abs(bounds-box(b)).max() <= 1, (kind, effect, semantics, dpr, t)
        sung = lambda p: (p[..., 1].astype(float)*p[..., 3]/255**2).sum()
        full_a, full_b = sung(refs[-1]), sung(frames[-1])
        width = (full_a+full_b)/(2*(bounds[3]-bounds[1]+1))
        assert abs(sung(a)/full_a-sung(b)/full_b)*width <= 1, (kind, effect, semantics, dpr, t)


def test_old_sidecar_is_rejected_before_selection(binary, monkeypatch):
    process = SkiaRendererProcess(binary, selection=selection('raster'))
    try: ready = NativeRendererProcess.start(process)
    finally: process.abort(reason='handshake fixture')
    ready.pop('reverse_wipe_schema'); sent = []
    monkeypatch.setattr(NativeRendererProcess, 'start', lambda self: ready)
    monkeypatch.setattr(SkiaRendererProcess, 'abort', lambda *a, **k: None)
    monkeypatch.setattr(SkiaRendererProcess, '_send', lambda self, p: sent.append(p))
    with pytest.raises(NativeRendererError, match='反向走字'): SkiaRendererProcess(binary).start()
    assert not sent
