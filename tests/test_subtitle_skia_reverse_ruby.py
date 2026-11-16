"""Reverse singing in source-ruby tracks follows the existing GPU contract.

Production selection (``active_rubies_for_line``), lower derivation, Painter and
the Qt->GPU projection all omit every upper/lower reading on a ``wipe_reverse``
line; the D2D ruby index remap is unreachable. Skia therefore keeps the source
annotations as provenance, draws no group on reverse lines, keeps forward lines
untouched and mirrors only the GPU's main-cell overlap endpoint.
"""
import copy
from dataclasses import replace
import json
import struct
import sys
from types import SimpleNamespace

import numpy as np
import pytest

from test_subtitle_skia_ruby_markers import application, binary, prepared_source, marker_source, ir_for
from test_subtitle_skia_roles import colors, project, mixed, roles
from test_subtitle_skia_resolved import ir_for as role_ir_for
from test_subtitle_skia_ruby_groups import configure
from test_subtitle_skia_protocol import collect, selection
from test_subtitle_skia_protocol import test_actual_g5_preview_uses_skia_even_when_gpu_off as run_preview
from test_subtitle_skia_protocol import test_product_export_short_mp4_uses_skia as run_mp4
from krok_helper.subtitle_render.domain.timing import RubyAnnotation, TimingChar
from krok_helper.subtitle_render.engine.ruby.skia_overlap import prepare_overlap_wipes
from krok_helper.subtitle_render.native.backend import NativeRendererProcess, NativeRendererError
from krok_helper.subtitle_render.native.skia_backend import SkiaRendererProcess


KINDS = ['upper', 'lower', 'mark', 'mixed']
TIMES = [199, 200, 201, 450, 699, 700, 701, 950, 1199, 1200, 1201, 1450, 1699, 1700, 1701,
         1799, 1800, 1801, 2400]


def ruby_for(line_index, *, mark=False, zero=False):
    if mark:
        return RubyAnnotation('字かな', '^', reading_parts=['^'], reading_part_ms=[],
            pos_start_ms=200, pos_end_ms=1700, target_line_index=line_index,
            target_char_start=0, target_char_end=3)
    start = 200 + 100*line_index
    return RubyAnnotation('字', 'なぐ', reading_parts=['な', '', 'ぐ'],
        reading_part_ms=[0, 0] if zero else [150, 250], pos_start_ms=start, pos_end_ms=start+500,
        target_line_index=line_index, target_char_start=0, target_char_end=1)


def reverse_source(base, kind, effect='none', mode='reading_units', *, zero=False):
    """One reverse line, or a forward ruby line plus a reverse ruby line."""
    track, style, resolve = base
    line = replace(track.lines[0], wipe_reverse=True)
    style = replace(style, karaoke_anim=effect, reverse_karaoke_anim='inherit',
                    lower_ruby_enabled=kind in {'lower', 'mixed'}, ruby_main_progress_mode=mode)
    if kind != 'mixed':
        return replace(track, lines=[line], rubies=[ruby_for(0, mark=kind == 'mark', zero=zero)]), style, resolve
    forward = replace(track.lines[0], wipe_reverse=False)
    reverse = replace(line, chars=[replace(c, start_ms=c.start_ms+100) for c in line.chars],
                      end_ms=line.end_ms+100, track_line_index=1)
    style = replace(style, dual_line_layout=True, line_alignments=['center', 'center'],
                    line_horizontal_layout='center', allow_inter_page_line_overlap=True,
                    auto_fill_section_time=False, line_gap_px=40)
    return replace(track, lines=[forward, reverse], rubies=[ruby_for(0), ruby_for(1, zero=zero)]), style, resolve


def without_ruby(source):
    """Remove annotation sources only; the lower setting still owns the row frame."""
    track, style, resolve = source
    return replace(track, rubies=[]), style, resolve


def reverse_index(ir):
    return [i for i, line in enumerate(ir['track']['lines']) if line.get('wipe_reverse')]


@pytest.mark.parametrize('kind', KINDS)
@pytest.mark.parametrize('effect', ['none', 'no_wipe'])
def test_reverse_line_keeps_sources_but_has_no_final_groups(binary, marker_source, kind, effect, tmp_path):
    source = reverse_source(marker_source, kind, effect)
    original = copy.deepcopy(source[:2])
    ir = ir_for(binary, source)
    assert source[:2] == original
    assert 'p05_reverse_wipe' in ir['required_features'] and ir['skia_layout']['reverse_wipe_schema'] == 1
    assert 'p05_reverse_overlap' in ir['required_features'] and ir['skia_layout']['reverse_overlap_schema'] == 1
    authored = [r for r in ir['track']['rubies'] if not r.get('lower')]
    assert [(r['reading'], r['target_line_index']) for r in authored] == [
        (r.reading, r.target_line_index) for r in source[0].rubies]
    index = reverse_index(ir)
    assert len(index) == 1
    final = ir['skia_layout']['lines'][index[0]]
    assert final['ruby_groups'] == []
    cells = final['cells']
    assert [c['clock_index'] for c in cells] == list(range(len(cells)-1, -1, -1))
    assert not any(r.get('lower') and r['lower_line_index'] == index[0] for r in ir['track']['rubies'])
    # The reverse line is exactly the same final main geometry as without any
    # annotation: no interference gap, no anchor/union shift, no paint change.
    plain = ir_for(binary, without_ruby(source))
    strip = lambda c: {k: v for k, v in c.items() if k not in {'wipe_end_position'}}
    assert [strip(c) for c in cells] == [strip(c) for c in plain['skia_layout']['lines'][index[0]]['cells']]
    assert ir['track']['lines'][index[0]]['resolved_intervals'] == plain['track']['lines'][index[0]]['resolved_intervals']
    # The GPU always applies its right-to-left endpoint on reverse lines.
    assert 'p05_overlap_wipe' in ir['required_features'] and ir['skia_layout']['overlap_schema'] == 1
    assert all('wipe_end_position' in c for c in cells) and cells[0]['wipe_end_position'] == 1
    if kind == 'mixed':
        forward = ir['skia_layout']['lines'][0]
        assert forward['ruby_groups'] and all('clock_index' not in c for c in forward['cells'])
        # Direction of a peer line never changes this line's prepared layout.
        peer = ir_for(binary, (replace(source[0], lines=[source[0].lines[0], replace(source[0].lines[1],
            wipe_reverse=False)]), *source[1:]))
        assert forward == peer['skia_layout']['lines'][0]
        if effect == 'none':
            (tmp_path/'reverse-ruby-layout.json').write_text(json.dumps({
                'features': ir['required_features'], 'sources': ir['track']['rubies'],
                'lines': ir['skia_layout']['lines'],
                'intervals': [line['resolved_intervals'] for line in ir['track']['lines']]},
                sort_keys=True), encoding='utf-8')


def _f32(value):
    return struct.unpack('f', struct.pack('f', value))[0]


def d2d_rtl_endpoints(lefts, rights, dpr):
    """Independent mirror of d2d_backend_configure adjustWipeEnd(rtl=true).

    Cells are first reversed into singing order; each compares itself with the
    following (left) neighbour in IEEE float32.
    """
    order = list(range(len(lefts)-1, -1, -1))
    out = [1.]*len(lefts)
    for a, b in zip(order, order[1:]):
        left, right, follow = (np.float32(lefts[a]*dpr), np.float32(rights[a]*dpr),
                               np.float32(rights[b]*dpr))
        if left <= follow:
            width = max(np.float32(np.float32(right-left)+np.float32(1)), np.float32(1))
            out[a] = float(min(max(np.float32((right-follow)/width), np.float32(0)), np.float32(1)))
    return out


@pytest.mark.parametrize('dpr', [1, 1.5, 1.25])
@pytest.mark.parametrize('lefts,widths', [
    ([0, 30, 60], [30, 30, 30]),            # touching unit boxes
    ([0, 25.3, 47.9], [31.1, 28.2, 30]),    # overlapping (negative spacing)
    ([0, 40, 90], [30, 30, 30]),            # gaps keep the basic endpoint
    ([-12.7, 13.1], [26.3, 26.3]),          # negative origins
    ([5], [40]),
])
def test_reverse_endpoints_match_the_gpu_rtl_rule(lefts, widths, dpr):
    offsets = [0.]*len(lefts)
    local = SimpleNamespace(main=SimpleNamespace(cells=[{'origin_x': x+o} for x, o in zip(lefts, offsets)]), rubies=())
    measured = SimpleNamespace(offsets=offsets, widths=widths)
    timing = SimpleNamespace(main=[None]*len(lefts))
    main, groups = prepare_overlap_wipes(local, measured, timing, dpr=dpr, reverse=True)
    assert groups == ()
    rights = [x+w for x, w in zip(lefts, widths)]
    assert list(main) == d2d_rtl_endpoints(lefts, rights, dpr)
    assert main[0] == 1.
    forward, _ = prepare_overlap_wipes(local, measured, timing, dpr=dpr)
    touching = any(lefts[i] <= rights[i-1] for i in range(1, len(lefts)))
    assert forward[-1] == 1. and (forward != main) == touching


def test_reverse_endpoint_never_accepts_annotation_groups():
    local = SimpleNamespace(main=SimpleNamespace(cells=[{'origin_x': 0}]), rubies=(object(),))
    with pytest.raises(ValueError, match='反向'):
        prepare_overlap_wipes(local, SimpleNamespace(offsets=[0], widths=[1]),
                              SimpleNamespace(main=[None]), reverse=True)


@pytest.mark.parametrize('kind', KINDS)
@pytest.mark.parametrize('effect', ['none', 'no_wipe'])
def test_gpu_raster_seek_offsets_and_dpr_are_identical(binary, marker_source, kind, effect):
    track, style, resolve = reverse_source(marker_source, kind, effect)
    outputs = []
    for target in ('gpu', 'raster'):
        runs = []
        for shifted in (False, True):
            source = (replace(track, meta=replace(track.meta, offset_ms=137)) if shifted else track,
                      replace(style, timing_offset_ms=229) if shifted else style, resolve)
            ir = ir_for(binary, source, dpr=1.5, roll_shift_ms=400 if shifted else 0)
            times = [t+(766 if shifted else 0) for t in [*TIMES, 950, 450]]
            with SkiaRendererProcess(binary, selection=selection(target)) as process:
                configure(process, ir)
                frames, _ = collect(process, times)
            assert np.array_equal(frames[7], frames[-2]) and np.array_equal(frames[3], frames[-1])
            runs.append(frames)
        assert all(np.array_equal(a, b) for a, b in zip(*runs, strict=True))
        outputs.append(runs[0])
    for a, b in zip(*outputs, strict=True):
        def premult(p):
            p = p.astype(float); p[..., :3] *= p[..., 3:] / 255
            return p
        delta = abs(premult(a)-premult(b))
        assert delta.mean() < .15 and np.mean(delta.max(axis=-1) > 16) < .003


@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_reverse_singing_order_and_translucent_alpha(binary, marker_source, target):
    track, style, resolve = reverse_source(marker_source, 'lower')
    style = replace(style, karaoke_colors=colors('#80FF0000', '#8000FF00'))
    ir = ir_for(binary, (track, style, resolve))
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, [450, 950, 1450, 2400])
    cells, intervals = ir['skia_layout']['lines'][0]['cells'], ir['track']['lines'][0]['resolved_intervals']
    def green(frame, cell):
        x0 = int(np.floor(cell['origin_x']+cell['metrics']['ink'][0]))+2
        x1 = int(np.ceil(cell['origin_x']+cell['metrics']['ink'][2]))-2
        y0 = int(np.floor(cell['baseline_y']+cell['metrics']['ink'][1]))+2
        y1 = int(np.ceil(cell['baseline_y']+cell['metrics']['ink'][3]))-2
        return frame[y0:y1, x0:x1]
    # Rightmost cell owns the first interval; later frames finish leftwards.
    first = cells[-1]
    assert intervals[first['clock_index']] == intervals[0]
    done = green(frames[1], first)
    assert (done[..., 1] > 100).mean() > .9 and (done[..., 0] < 20).mean() > .9
    untouched = green(frames[1], cells[0])
    assert (untouched[..., 0] > 100).mean() > .9
    inside = green(frames[3], cells[1])
    assert abs(int(np.median(inside[..., 3])) - 128) <= 1


@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_zero_duration_ruby_on_reverse_line_is_omitted_but_forward_gate_remains(binary, marker_source, target):
    # A reverse line draws no annotation, so its zero-duration source cannot
    # violate the no_wipe whole-cell gate; the same source on a forward line can.
    track, style, resolve = reverse_source(marker_source, 'upper', 'no_wipe', zero=True)
    ir = ir_for(binary, (track, style, resolve))
    assert ir['skia_layout']['lines'][0]['ruby_groups'] == []
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir); frames, _ = collect(process, [450, 2400])
    assert frames[0].any() and not np.array_equal(frames[0], frames[1])
    forward = (replace(track, lines=[replace(track.lines[0], wipe_reverse=False)]), style, resolve)
    with pytest.raises(ValueError, match='零时长注音'):
        ir_for(binary, forward)


@pytest.mark.parametrize('mutation', ['groups', 'lower_group', 'overlap_feature', 'overlap_value',
                                      'clock', 'feature', 'forward_endpoint_shape', 'ruby_feature',
                                      'ruby_schema', 'ruby_schema_type'])
@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_invalid_reverse_ruby_ir_keeps_previous_scene(binary, marker_source, mutation, target):
    ir = ir_for(binary, reverse_source(marker_source, 'mixed'))
    bad = copy.deepcopy(ir)
    reverse = reverse_index(bad)[0]
    line = bad['skia_layout']['lines'][reverse]
    if mutation == 'groups':
        line['ruby_groups'] = [g for g in bad['skia_layout']['lines'][0]['ruby_groups'] if not g.get('lower')]
    elif mutation == 'lower_group':
        line['ruby_groups'] = [g for g in bad['skia_layout']['lines'][0]['ruby_groups'] if g.get('lower')]
    elif mutation == 'overlap_feature':
        bad['required_features'].remove('p05_overlap_wipe')
    elif mutation == 'overlap_value':
        line['cells'][1]['wipe_end_position'] = 1.5
    elif mutation == 'clock':
        line['cells'][0]['clock_index'] = 0
    elif mutation == 'feature':
        bad['required_features'].remove('p05_reverse_wipe')
    elif mutation == 'ruby_feature':
        bad['required_features'].remove('p05_reverse_overlap')
    elif mutation == 'ruby_schema':
        bad['skia_layout']['reverse_overlap_schema'] = 2
    elif mutation == 'ruby_schema_type':
        bad['skia_layout']['reverse_overlap_schema'] = '1'
    else:
        line['cells'][1]['wipe_end_position'] = 'x'
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir); before, _ = collect(process, [950])
        with pytest.raises(NativeRendererError): configure(process, bad)
        after, _ = collect(process, [950], generation=2)
    assert np.array_equal(before[0], after[0])


@pytest.mark.parametrize('gpu,missing_loader', [(True, False), (False, False), (True, True)])
def test_real_reverse_ruby_preview(binary, marker_source, monkeypatch, tmp_path, gpu, missing_loader):
    run_preview(binary, monkeypatch, tmp_path, reverse_source(marker_source, 'mixed')[:2], gpu, missing_loader)


@pytest.mark.parametrize('gpu,missing_loader', [(True, False), (False, False), (True, True)])
def test_real_reverse_ruby_mp4(binary, marker_source, monkeypatch, tmp_path, gpu, missing_loader):
    run_mp4(binary, monkeypatch, tmp_path, reverse_source(marker_source, 'mixed')[:2], gpu, missing_loader)


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('kind', ['upper', 'lower', 'mark'])
@pytest.mark.parametrize('semantics', ['legacy', 'n3_1074'])
def test_actual_d2d_omits_every_annotation_on_reverse_lines(kind, marker_source, semantics):
    """The oracle itself: identical D2D pixels with and without the sources."""
    from test_subtitle_render_gpu_backend import _render_g1_frames
    source = reverse_source(marker_source, kind)
    outputs = []
    for track, style, _ in (source, without_ruby(source)):
        with NativeRendererProcess() as process:
            _, raw = _render_g1_frames(process, replace(style, layout_semantics=semantics), (450, 950, 2400),
                force_warp=False, track=track, width=900, height=600, realization_enabled=False)
        outputs.append(raw)
    assert all(a == b for a, b in zip(*outputs, strict=True))
    assert outputs[0][0] != outputs[0][2]


def _bands(p):
    mask = p[..., 3] > 127
    ys = np.flatnonzero(mask.any(axis=1))
    return [v for v in np.split(ys, np.flatnonzero(np.diff(ys) > 1)+1) if len(v)]


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('case', [*((k, 'reading_units') for k in KINDS), ('mixed', 'checkpoint_segments')])
@pytest.mark.parametrize('effect', ['none', 'no_wipe'])
@pytest.mark.parametrize('semantics', ['legacy', 'n3_1074'])
@pytest.mark.parametrize('dpr', [1, 1.5])
def test_reverse_ruby_matches_actual_d2d_gpu(binary, marker_source, case, effect, semantics, dpr):
    """Raw D2D path (realization disabled), as in every prior P05 oracle."""
    from test_subtitle_render_gpu_backend import _render_g1_frames
    kind, mode = case
    track, style, resolve = reverse_source(marker_source, kind, effect, mode)
    style = replace(style, layout_semantics=semantics)
    with NativeRendererProcess() as process:
        _, raw = _render_g1_frames(process, style, tuple(TIMES), force_warp=False, track=track,
            width=900, height=600, dpr=dpr, realization_enabled=False)
    with SkiaRendererProcess(binary, selection=selection('gpu')) as process:
        configure(process, ir_for(binary, (track, style, resolve), dpr=dpr))
        frames, _ = collect(process, TIMES)
    refs = [np.frombuffer(p, np.uint8).reshape(round(600*dpr), round(900*dpr), 4) for p in raw]
    for t, a, b in zip(TIMES, refs, frames, strict=True):
        aa, bb = _bands(a), _bands(b)
        assert len(aa) == len(bb) > 0, (case, effect, semantics, dpr, t)
        for ya, yb in zip(aa, bb, strict=True):
            xa = np.flatnonzero((a[ya[0]:ya[-1]+1, :, 3] > 127).any(axis=0))
            xb = np.flatnonzero((b[yb[0]:yb[-1]+1, :, 3] > 127).any(axis=0))
            assert max(abs(np.array([xa[0], xa[-1], ya[0], ya[-1]]) -
                           [xb[0], xb[-1], yb[0], yb[-1]])) <= 1, (case, effect, semantics, dpr, t)
            lo, hi = max(min(ya[0], yb[0])-2, 0), max(ya[-1], yb[-1])+3
            sung = lambda p: (p[lo:hi, :, 1].astype(float)*p[lo:hi, :, 3]/255**2).sum()
            full_a, full_b = sung(refs[-1]), sung(frames[-1])
            width = (full_a+full_b)/(2*(ya[-1]-ya[0]+1))
            assert abs(sung(a)/full_a-sung(b)/full_b)*width <= 1, (case, effect, semantics, dpr, t)


def test_old_reverse_sidecar_is_rejected_before_selection(binary, monkeypatch):
    process = SkiaRendererProcess(binary, selection=selection('raster'))
    try: ready = NativeRendererProcess.start(process)
    finally: process.abort(reason='handshake fixture')
    ready.pop('reverse_overlap_schema'); sent = []
    monkeypatch.setattr(NativeRendererProcess, 'start', lambda self: ready)
    monkeypatch.setattr(SkiaRendererProcess, 'abort', lambda *a, **k: None)
    monkeypatch.setattr(SkiaRendererProcess, '_send', lambda self, p: sent.append(p))
    with pytest.raises(NativeRendererError, match='反向走字重叠终点'): SkiaRendererProcess(binary).start()
    assert not sent


def test_reverse_line_without_annotations_uses_the_same_endpoints(binary, marker_source):
    # D2D renders these two sources pixel-identically (see the oracle test),
    # so Skia must not make a reverse line depend on unrelated sources.
    source = reverse_source(marker_source, 'upper')
    annotated, plain = ir_for(binary, source), ir_for(binary, without_ruby(source))
    assert 'p05_resolved_ruby' not in plain['required_features']
    assert {'p05_reverse_wipe', 'p05_reverse_overlap', 'p05_overlap_wipe'} <= set(plain['required_features'])
    assert annotated['skia_layout']['lines'][0]['cells'] == plain['skia_layout']['lines'][0]['cells']


@pytest.mark.parametrize('blank', [False, True])
def test_empty_reverse_line_needs_no_endpoint_capability(binary, marker_source, blank):
    track, style, resolve = reverse_source(marker_source, 'upper')
    line = replace(track.lines[0], chars=[TimingChar(' ', 200)] if blank else [])
    ir = ir_for(binary, (replace(track, lines=[line], rubies=[]), style, resolve))
    assert 'p05_reverse_wipe' in ir['required_features']
    assert ('p05_reverse_overlap' in ir['required_features']) == blank


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.xfail(strict=True, raises=AssertionError, reason='Pre-existing forward gap recorded in plan §14.38: historical forward '
                   'scenes without an overlap requirement omit the GPU endpoint (about 1.05 physical px at '
                   'end-1ms, DPR 1.5, touching full-cell glyphs). Not changed by the reverse batch.')
def test_recorded_forward_endpoint_gap_without_overlap_requirement(binary, marker_source):
    from test_subtitle_render_gpu_backend import _render_g1_frames
    track, style, resolve = without_ruby(reverse_source(marker_source, 'upper'))
    track = replace(track, lines=[replace(track.lines[0], wipe_reverse=False)])
    times = [1199, 2400]
    with NativeRendererProcess() as process:
        _, raw = _render_g1_frames(process, style, tuple(times), force_warp=False, track=track,
            width=900, height=600, dpr=1.5, realization_enabled=False)
    with SkiaRendererProcess(binary, selection=selection('gpu')) as process:
        configure(process, ir_for(binary, (track, style, resolve), dpr=1.5)); frames, _ = collect(process, times)
    a, full_a = (np.frombuffer(p, np.uint8).reshape(900, 1350, 4) for p in raw)
    b, full_b = frames
    ya = _bands(a)[-1]; lo, hi = ya[0]-2, ya[-1]+3
    sung = lambda p: (p[lo:hi, :, 1].astype(float)*p[lo:hi, :, 3]/255**2).sum()
    width = (sung(full_a)+sung(full_b))/(2*(ya[-1]-ya[0]+1))
    assert abs(sung(a)/sung(full_a)-sung(b)/sung(full_b))*width <= 1


@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_reverse_roles_keep_font_runs_and_paints_in_annotation_track(binary, roles, target):
    track, style = roles
    line = replace(track.lines[0], wipe_reverse=True, track_line_index=0,
                   display_start_override_ms=0, display_end_override_ms=3000)
    ruby = RubyAnnotation('字', 'じ', pos_start_ms=200, pos_end_ms=400, target_line_index=0,
                          target_char_start=0, target_char_end=1)
    style = replace(style, reverse_karaoke_anim='no_wipe', stroke_width_px=0, stroke2_width_px=0)
    ir = role_ir_for(binary, (replace(track, lines=[line], rubies=[ruby]), style))
    plain = role_ir_for(binary, (replace(track, lines=[line], rubies=[]), style))
    final = ir['skia_layout']['lines'][0]
    assert final['ruby_groups'] == [] and ir['track']['rubies'][0]['reading'] == 'じ'
    assert final['cells'] == plain['skia_layout']['lines'][0]['cells']
    runs = lambda layout: [layout['runs'][c['run_index']] for c in layout['lines'][0]['cells']]
    assert runs(ir['skia_layout']) == runs(plain['skia_layout'])
    intervals = ir['track']['lines'][0]['resolved_intervals']
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, [100, 750, 2500])
    for c in final['cells']:
        ink = c['metrics']['ink']
        x0, x1 = int(np.floor(c['origin_x']+ink[0])), int(np.ceil(c['origin_x']+ink[2]))
        y0, y1 = int(np.floor(c['baseline_y']+ink[1])), int(np.ceil(c['baseline_y']+ink[3]))
        expected = frames[2] if intervals[c['clock_index']][1] <= 750 else frames[0]
        assert np.array_equal(frames[1][y0:y1, x0:x1], expected[y0:y1, x0:x1])
