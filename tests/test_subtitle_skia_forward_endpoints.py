"""Every canonical line carries the existing GPU's final wipe endpoint.

``d2d_backend_configure.cpp`` gives every main and annotation character basic
wipe points and always applies ``AdjustWipeEnd``; the body is then drawn
through the per-character N3 phase path. Skia previously prepared endpoints
only for lower/marker/unit-projection scenes, leaving ordinary and upper-only
lines about 1.05 physical px behind at end-1ms (DPR 1.5, touching cells).
"""
import copy
from dataclasses import replace
import json
import sys
from types import SimpleNamespace

import numpy as np
import pytest

from test_subtitle_skia_ruby_markers import application, binary, prepared_source, marker_source, ir_for
from test_subtitle_skia_reverse_ruby import ruby_for, reverse_source, _bands
from test_subtitle_skia_roles import colors
from test_subtitle_skia_ruby_groups import configure
from test_subtitle_skia_protocol import collect, selection
from test_subtitle_skia_protocol import test_actual_g5_preview_uses_skia_even_when_gpu_off as run_preview
from test_subtitle_skia_protocol import test_product_export_short_mp4_uses_skia as run_mp4
from krok_helper.subtitle_render.domain.models import SubtitleStyleScheme
from krok_helper.subtitle_render.domain.timing import TimingChar
from krok_helper.subtitle_render.engine.ruby.skia_overlap import line_endpoints
from krok_helper.subtitle_render.native.backend import NativeRendererProcess, NativeRendererError
from krok_helper.subtitle_render.native.skia_backend import SkiaRendererProcess


KINDS = ['touching', 'overlap', 'gap', 'pause', 'first_zero', 'all_zero', 'roles', 'upper', 'mixed_direction']
TIMES = [199, 200, 201, 449, 450, 451, 699, 700, 701, 950, 1199, 1200, 1201, 1450, 1699, 1700, 1701,
         1799, 1800, 1801, 2400]


def forward_source(base, kind, effect='none'):
    """Forward lines of the controlled rectangle fonts; one interaction each."""
    track, style, resolve = base
    line = replace(track.lines[0], wipe_reverse=False)
    style = replace(style, karaoke_anim=effect, lower_ruby_enabled=False)
    rubies = []
    if kind == 'overlap': style = replace(style, letter_spacing_px=-12)
    if kind == 'gap': style = replace(style, letter_spacing_px=14)
    if kind == 'pause':
        line = replace(line, chars=[replace(line.chars[0], pause_release_ms=450), *line.chars[1:]])
    if kind == 'first_zero':
        line = replace(line, chars=[replace(line.chars[0], pause_release_ms=200), *line.chars[1:]])
    if kind == 'all_zero':
        line = replace(line, chars=[replace(c, start_ms=200) for c in line.chars], end_ms=200)
    if kind == 'roles':
        style = replace(style, custom_style_schemes={'lead': SubtitleStyleScheme(
            font_size_px=70, karaoke_colors=colors('#FFFF0040', '#FF00FF40'))})
        line = replace(line, chars=[line.chars[0], replace(line.chars[1], role_label='lead'), line.chars[2]])
    if kind == 'upper':
        rubies = [ruby_for(0)]
    if kind == 'mixed_direction':
        # Upper-only two-row page: a forward annotated line and a reverse line.
        mixed, mixed_style, _ = reverse_source(base, 'mixed', effect)
        return mixed, replace(mixed_style, lower_ruby_enabled=False), resolve
    return replace(track, lines=[line], rubies=rubies), style, resolve


def float32_forward(lefts, rights, dpr):
    """Independent IEEE float32 transcription of AdjustWipeEnd(rtl=false)."""
    out = [1.]*len(lefts)
    for i in range(len(lefts)-1):
        left, right, follow = (np.float32(lefts[i]*dpr), np.float32(rights[i]*dpr), np.float32(lefts[i+1]*dpr))
        if right >= follow:
            width = max(np.float32(np.float32(right-left)+np.float32(1)), np.float32(1))
            out[i] = float(min(max(np.float32((follow-left)/width), np.float32(0)), np.float32(1)))
    return out


@pytest.mark.parametrize('dpr', [1, 1.5, 1.25, 2])
@pytest.mark.parametrize('lefts,widths', [
    ([0, 30, 60], [30, 30, 30]), ([0, 25.3, 47.9], [31.1, 28.2, 30]), ([0, 40, 90], [30, 30, 30]),
    ([-12.7, 13.1], [26.3, 26.3]), ([5], [40]), ([], []), ([0, 0, 0], [0, 0, 0]),
])
def test_forward_endpoints_match_the_gpu_float32_rule(lefts, widths, dpr):
    measured = SimpleNamespace(offsets=[0.]*len(lefts), widths=widths)
    main, groups = line_endpoints([{'origin_x': x} for x in lefts], measured, dpr=dpr)
    assert groups == ()
    assert list(main) == float32_forward(lefts, [x+w for x, w in zip(lefts, widths)], dpr)
    assert not main or main[-1] == 1.


@pytest.mark.parametrize('dpr', [0, -1, float('nan'), float('inf')])
def test_endpoint_preparation_rejects_invalid_pixel_ratio(dpr):
    with pytest.raises(ValueError, match='像素比例'):
        line_endpoints([{'origin_x': 0}], SimpleNamespace(offsets=[0], widths=[1]), dpr=dpr)


@pytest.mark.parametrize('kind', KINDS)
def test_every_nonempty_canonical_cell_has_one_final_endpoint(binary, marker_source, kind, tmp_path):
    ir = ir_for(binary, forward_source(marker_source, kind))
    assert 'p05_overlap_wipe' in ir['required_features'] and ir['skia_layout']['overlap_schema'] == 1
    for source, line in zip(ir['track']['lines'], ir['skia_layout']['lines'], strict=True):
        cells = line['cells']
        assert cells and all(0 <= c['wipe_end_position'] <= 1 for c in cells)
        sung_last = cells[0] if source.get('wipe_reverse') else cells[-1]
        assert sung_last['wipe_end_position'] == 1
        for group in line.get('ruby_groups', []):
            assert all(0 <= u['wipe_end_position'] <= 1 for u in group['units'])
    if kind == 'touching':
        # Touching unit boxes keep the GPU's 1-physical-pixel compensation.
        assert all(c['wipe_end_position'] < 1 for c in ir['skia_layout']['lines'][0]['cells'][:-1])
        (tmp_path/'forward-endpoint-layout.json').write_text(json.dumps({
            'features': ir['required_features'], 'lines': ir['skia_layout']['lines'],
            'intervals': ir['track']['lines'][0]['resolved_intervals']}, sort_keys=True), encoding='utf-8')
    if kind == 'gap':
        assert all(c['wipe_end_position'] == 1 for c in ir['skia_layout']['lines'][0]['cells'])


@pytest.mark.parametrize('blank', [False, True])
@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_empty_and_blank_lines_keep_the_base_contract(binary, marker_source, blank, target):
    track, style, resolve = forward_source(marker_source, 'touching')
    line = replace(track.lines[0], chars=[TimingChar(' ', 200), TimingChar(' ', 700)] if blank else [])
    ir = ir_for(binary, (replace(track, lines=[line]), style, resolve))
    cells = ir['skia_layout']['lines'][0]['cells']
    assert 'p05_overlap_wipe' in ir['required_features'] and ir['skia_layout']['overlap_schema'] == 1
    assert len(cells) == (2 if blank else 0) and all('wipe_end_position' in c for c in cells)
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir); frames, _ = collect(process, [199, 450, 2400])
    assert all(not p.any() for p in frames)
    empty = ir_for(binary, (replace(track, lines=[]), style, resolve))
    assert 'p05_overlap_wipe' in empty['required_features'] and empty['skia_layout']['lines'] == []


@pytest.mark.parametrize('kind', ['touching', 'overlap', 'pause', 'roles', 'upper', 'mixed_direction'])
@pytest.mark.parametrize('effect', ['none', 'no_wipe'])
def test_gpu_raster_seek_offsets_and_dpr_are_identical(binary, marker_source, kind, effect):
    track, style, resolve = forward_source(marker_source, kind, effect)
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
            assert np.array_equal(frames[9], frames[-2]) and np.array_equal(frames[4], frames[-1])
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
def test_translucent_interiors_and_singing_order(binary, marker_source, target):
    track, style, resolve = forward_source(marker_source, 'touching')
    style = replace(style, karaoke_colors=colors('#80FF0000', '#8000FF00'))
    ir = ir_for(binary, (track, style, resolve))
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir); frames, _ = collect(process, [950, 2400])
    cells = ir['skia_layout']['lines'][0]['cells']
    def inside(frame, cell):
        ink = cell['metrics']['ink']
        x0, x1 = int(np.floor(cell['origin_x']+ink[0]))+2, int(np.ceil(cell['origin_x']+ink[2]))-2
        y0, y1 = int(np.floor(cell['baseline_y']+ink[1]))+2, int(np.ceil(cell['baseline_y']+ink[3]))-2
        return frame[y0:y1, x0:x1]
    assert (inside(frames[0], cells[0])[..., 1] > 100).mean() > .9
    assert (inside(frames[0], cells[2])[..., 0] > 100).mean() > .9
    assert abs(int(np.median(inside(frames[1], cells[1])[..., 3])) - 128) <= 1


@pytest.mark.parametrize('mutation', ['feature', 'schema', 'schema_type', 'value', 'value_type', 'nan'])
@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_invalid_endpoint_protocol_keeps_previous_scene(binary, marker_source, mutation, target):
    ir = ir_for(binary, forward_source(marker_source, 'touching'))
    bad = copy.deepcopy(ir)
    cell = bad['skia_layout']['lines'][0]['cells'][0]
    if mutation == 'feature': bad['required_features'].remove('p05_overlap_wipe')
    elif mutation == 'schema': bad['skia_layout']['overlap_schema'] = 2
    elif mutation == 'schema_type': bad['skia_layout']['overlap_schema'] = '1'
    elif mutation == 'value': cell['wipe_end_position'] = 1.5
    elif mutation == 'value_type': cell['wipe_end_position'] = True
    else: cell['wipe_end_position'] = float('nan')
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir); before, _ = collect(process, [950])
        with pytest.raises(NativeRendererError): configure(process, bad)
        after, _ = collect(process, [950], generation=2)
    assert np.array_equal(before[0], after[0])


def test_sidecar_without_overlap_capability_is_rejected_before_selection(binary, monkeypatch):
    process = SkiaRendererProcess(binary, selection=selection('raster'))
    try: ready = NativeRendererProcess.start(process)
    finally: process.abort(reason='handshake fixture')
    ready.pop('overlap_schema'); sent = []
    monkeypatch.setattr(NativeRendererProcess, 'start', lambda self: ready)
    monkeypatch.setattr(SkiaRendererProcess, 'abort', lambda *a, **k: None)
    monkeypatch.setattr(SkiaRendererProcess, '_send', lambda self, p: sent.append(p))
    with pytest.raises(NativeRendererError, match='重叠走字'): SkiaRendererProcess(binary).start()
    assert not sent


@pytest.mark.parametrize('gpu,missing_loader', [(True, False), (False, False), (True, True)])
def test_real_forward_endpoint_preview(binary, marker_source, monkeypatch, tmp_path, gpu, missing_loader):
    run_preview(binary, monkeypatch, tmp_path, forward_source(marker_source, 'roles')[:2], gpu, missing_loader)


@pytest.mark.parametrize('gpu,missing_loader', [(True, False), (False, False), (True, True)])
def test_real_forward_endpoint_mp4(binary, marker_source, monkeypatch, tmp_path, gpu, missing_loader):
    run_mp4(binary, monkeypatch, tmp_path, forward_source(marker_source, 'upper')[:2], gpu, missing_loader)


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('kind', KINDS)
@pytest.mark.parametrize('effect', ['none', 'no_wipe'])
@pytest.mark.parametrize('semantics', ['legacy', 'n3_1074'])
@pytest.mark.parametrize('dpr', [1, 1.5])
def test_forward_endpoints_match_actual_d2d_gpu(binary, marker_source, kind, effect, semantics, dpr):
    """Raw D2D path (realization disabled), as in every prior P05 oracle."""
    from test_subtitle_render_gpu_backend import _render_g1_frames
    track, style, resolve = forward_source(marker_source, kind, effect)
    style = replace(style, layout_semantics=semantics)
    with NativeRendererProcess() as process:
        _, raw = _render_g1_frames(process, style, tuple(TIMES), force_warp=False, track=track,
            width=900, height=600, dpr=dpr, realization_enabled=False)
    with SkiaRendererProcess(binary, selection=selection('gpu')) as process:
        configure(process, ir_for(binary, (track, style, resolve), dpr=dpr))
        frames, _ = collect(process, TIMES)
    refs = [np.frombuffer(p, np.uint8).reshape(round(600*dpr), round(900*dpr), 4) for p in raw]
    case = (kind, effect, semantics, dpr)
    for t, a, b in zip(TIMES, refs, frames, strict=True):
        aa, bb = _bands(a), _bands(b)
        assert len(aa) == len(bb) > 0, (case, t)
        for ya, yb in zip(aa, bb, strict=True):
            xa = np.flatnonzero((a[ya[0]:ya[-1]+1, :, 3] > 127).any(axis=0))
            xb = np.flatnonzero((b[yb[0]:yb[-1]+1, :, 3] > 127).any(axis=0))
            assert max(abs(np.array([xa[0], xa[-1], ya[0], ya[-1]]) -
                           [xb[0], xb[-1], yb[0], yb[-1]])) <= 1, (case, t)
            lo, hi = max(min(ya[0], yb[0])-2, 0), max(ya[-1], yb[-1])+3
            sung = lambda p: (p[lo:hi, :, 1].astype(float)*p[lo:hi, :, 3]/255**2).sum()
            full_a, full_b = sung(refs[-1]), sung(frames[-1])
            width = (full_a+full_b)/(2*(ya[-1]-ya[0]+1))
            assert abs(sung(a)/full_a-sung(b)/full_b)*width <= 1, (case, t)
