"""Zero-duration annotation clocks keep the existing GPU's no-wipe boundaries."""
from dataclasses import replace
import copy
import json
import sys

import numpy as np
import pytest

from test_subtitle_skia_no_wipe import application, binary, prepared_source, unit_source, mark_source, discrete, ir_for
from test_subtitle_skia_ruby_groups import configure
from test_subtitle_skia_protocol import collect, selection
from test_subtitle_skia_protocol import test_actual_g5_preview_uses_skia_even_when_gpu_off as run_preview
from test_subtitle_skia_protocol import test_product_export_short_mp4_uses_skia as run_mp4
from test_subtitle_skia_roles import colors
from krok_helper.subtitle_render.native.backend import NativeRendererProcess
from krok_helper.subtitle_render.native.backend import NativeRendererError
from krok_helper.subtitle_render.native.skia_backend import SkiaRendererProcess
from krok_helper.subtitle_render.domain.models import SubtitleStyleScheme


def zero_reading(source, kind='leading', *, lower=False, mode='reading_units', translucent=False):
    track, style, resolve = discrete(source, 'lower' if lower else 'upper', mode)
    parts, checkpoints, end = {
        'leading': (['A', '', 'W'], [0, 800], 1700),
        'trailing': (['A', 'W'], [1500], 1700),
        'interior': (['A', 'A', 'W'], [400, 400], 1700),
        'leading_pair': (['A', 'A', 'W'], [0, 0], 1700),
        # A trailing empty pause keeps the source annotation window alive
        # while every visible unit has a zero-duration clock.
        'all_zero': (['A', 'W', ''], [0, 0], 1700),
    }[kind]
    ruby = replace(track.rubies[0], reading=''.join(parts), reading_parts=parts,
                   reading_part_ms=checkpoints, pos_end_ms=end)
    style = replace(style, ruby_karaoke_colors=colors('#80FF0000', '#8000FF00') if translucent
                    else colors('#FFFF0000', '#FF00FF00'))
    return replace(track, rubies=[ruby]), style, resolve


def bands(frame):
    ys = np.flatnonzero((frame[..., 3] > 16).any(axis=1))
    return [v for v in np.split(ys, np.flatnonzero(np.diff(ys) > 1)+1) if len(v)]


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('kind', ['leading', 'all_zero'])
def test_actual_gpu_initial_zero_no_wipe_can_draw_both_stacks(mark_source, kind, tmp_path):
    """Record the singular GPU boundary; do not assume exclusive alpha here."""
    from test_subtitle_render_gpu_backend import _render_g1_frames
    track, style, _ = zero_reading(mark_source, kind, translucent=True)
    times = [199, 200, 201, 1900]
    with NativeRendererProcess() as process:
        _, raw = _render_g1_frames(process, style, tuple(times), force_warp=False, track=track,
                                   width=900, height=600, dpr=1, realization_enabled=False)
    frames = [np.frombuffer(p, np.uint8).reshape(600, 900, 4) for p in raw]
    observations = []
    for time, frame in zip(times, frames):
        rows = bands(frame)[0]  # Upper annotation is separated from the body.
        annotation = frame[rows[0]:rows[-1]+1]
        observations.append({'time': time, 'max_alpha': int(annotation[..., 3].max()),
                             'opaque_interior': int((annotation[..., 3] > 150).sum())})
    (tmp_path/'gpu-zero-ruby-boundary.json').write_text(json.dumps(observations), encoding='utf-8')
    assert observations[0]['max_alpha'] == 128 and observations[2]['max_alpha'] == 128
    assert observations[1]['max_alpha'] == 192 and observations[1]['opaque_interior'] > 0


def test_product_zero_no_wipe_preserves_the_authored_clocks(binary, mark_source):
    source = zero_reading(mark_source)
    ir = ir_for(binary, source)
    smooth = ir_for(binary, (source[0], replace(source[1], karaoke_anim='none'), source[2]))
    assert ir['skia_layout']['lines'] == smooth['skia_layout']['lines']
    assert ir['track']['lines'][0]['resolved_intervals'] == smooth['track']['lines'][0]['resolved_intervals']
    assert 'p07_zero_ruby_no_wipe' in ir['required_features']
    assert ir['skia_layout']['zero_ruby_no_wipe_schema'] == 1


KINDS = ['leading', 'trailing', 'interior', 'leading_pair', 'all_zero']
TIMES = [199, 200, 201, 599, 600, 601, 999, 1000, 1001, 1699, 1700, 1701, 1900]


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('kind', KINDS)
@pytest.mark.parametrize('lower', [False, True], ids=['upper', 'both'])
@pytest.mark.parametrize('mode', ['reading_units', 'checkpoint_segments'])
@pytest.mark.parametrize('semantics', ['legacy', 'n3_1074'])
@pytest.mark.parametrize('dpr', [1, 1.5])
def test_zero_ruby_matches_actual_d2d_gpu(binary, mark_source, kind, lower, mode, semantics, dpr):
    from test_subtitle_render_gpu_backend import _render_g1_frames
    track, style, resolve = zero_reading(mark_source, kind, lower=lower, mode=mode, translucent=True)
    style = replace(style, layout_semantics=semantics)
    with NativeRendererProcess() as process:
        _, raw = _render_g1_frames(process, style, tuple(TIMES), force_warp=False, track=track,
                                   width=900, height=600, dpr=dpr, realization_enabled=False)
    refs = [np.frombuffer(p, np.uint8).reshape(round(600*dpr), round(900*dpr), 4) for p in raw]
    with SkiaRendererProcess(binary, selection=selection('gpu')) as process:
        configure(process, ir_for(binary, (track, style, resolve), dpr=dpr))
        frames, _ = collect(process, TIMES)
    case = kind, lower, mode, semantics, dpr
    for time, a, b in zip(TIMES, refs, frames):
        aa, bb = bands(a), bands(b)
        assert len(aa) == len(bb) > 0, (case, time)
        for ya, yb in zip(aa, bb):
            xa = np.flatnonzero((a[ya[0]:ya[-1]+1, :, 3] > 127).any(axis=0))
            xb = np.flatnonzero((b[yb[0]:yb[-1]+1, :, 3] > 127).any(axis=0))
            assert max(abs(np.array([xa[0], xa[-1], ya[0], ya[-1]])-
                           [xb[0], xb[-1], yb[0], yb[-1]])) <= 1, (case, time)
            lo, hi = max(min(ya[0], yb[0])-2, 0), max(ya[-1], yb[-1])+3
            sung = lambda p: (p[lo:hi, :, 1].astype(float)*p[lo:hi, :, 3]/255**2).sum()
            full_a, full_b = sung(refs[-1]), sung(frames[-1])
            height = ya[-1]-ya[0]+1
            width = (full_a+full_b)/(2*height)
            assert abs(sung(a)/full_a-sung(b)/full_b)*width <= 1, (case, time)
            # Include the singular Before+After alpha overlap in the budget.
            alpha = lambda p: p[lo:hi, :, 3].astype(float).sum()/255
            assert abs(alpha(a)-alpha(b))/height <= 1, (case, time, 'alpha')


@pytest.mark.parametrize('kind', KINDS)
@pytest.mark.parametrize('lower', [False, True])
def test_gpu_raster_repeated_seek_and_offsets(binary, mark_source, kind, lower, tmp_path):
    track, style, resolve = zero_reading(mark_source, kind, lower=lower, translucent=True)
    results = []
    for target in ('gpu', 'raster'):
        original = None
        for shifted in (False, True):
            source = (replace(track, meta=replace(track.meta, offset_ms=137)) if shifted else track,
                      replace(style, timing_offset_ms=229) if shifted else style, resolve)
            ir = ir_for(binary, source, dpr=1.5, roll_shift_ms=400 if shifted else 0)
            with SkiaRendererProcess(binary, selection=selection(target)) as process:
                configure(process, ir)
                frames, _ = collect(process, [t+(766 if shifted else 0) for t in [*TIMES, 600, 200]])
            assert np.array_equal(frames[4], frames[-2]) and np.array_equal(frames[1], frames[-1])
            if shifted:
                assert all(np.array_equal(a, b) for a, b in zip(original, frames))
            else:
                original = frames
            if kind == 'leading' and lower and target == 'gpu' and not shifted:
                (tmp_path/'zero-ruby-no-wipe-layout.json').write_text(json.dumps({
                    'features': ir['required_features'], 'schema': ir['skia_layout']['zero_ruby_no_wipe_schema'],
                    'lines': ir['skia_layout']['lines'], 'intervals': ir['track']['lines'][0]['resolved_intervals']},
                    sort_keys=True), encoding='utf-8')
        results.append(original)
    for a, b in zip(*results):
        def premult(p):
            p = p.astype(float); p[..., :3] *= p[..., 3:] / 255
            return p
        delta = abs(premult(a)-premult(b))
        assert delta.mean() < .15 and np.mean(delta.max(axis=-1) > 16) < .003


@pytest.mark.parametrize('target', ['gpu', 'raster'])
@pytest.mark.parametrize('mutation', ['feature', 'schema', 'parent_feature', 'bool', 'string', 'version'])
def test_missing_or_invalid_zero_capability_preserves_previous_scene(binary, mark_source, target, mutation):
    ir = ir_for(binary, zero_reading(mark_source))
    bad = copy.deepcopy(ir)
    if mutation == 'feature': bad['required_features'].remove('p07_zero_ruby_no_wipe')
    elif mutation == 'parent_feature': bad['required_features'].remove('p07_no_wipe')
    elif mutation == 'schema': del bad['skia_layout']['zero_ruby_no_wipe_schema']
    else: bad['skia_layout']['zero_ruby_no_wipe_schema'] = {'bool': True, 'string': '1', 'version': 2}[mutation]
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir); before, _ = collect(process, [200])
        with pytest.raises(NativeRendererError): configure(process, bad)
        after, _ = collect(process, [200], generation=2)
    assert np.array_equal(before[0], after[0])


@pytest.mark.parametrize('mutation', ['schema', 'feature', 'bool', 'version'])
def test_old_sidecar_is_rejected_before_selection(binary, monkeypatch, mutation):
    process = SkiaRendererProcess(binary, selection=selection('raster'))
    try: ready = NativeRendererProcess.start(process)
    finally: process.abort(reason='zero ruby handshake fixture')
    if mutation == 'schema': del ready['zero_ruby_no_wipe_schema']
    elif mutation == 'feature': ready['profiles'].remove('p07_zero_ruby_no_wipe')
    else: ready['zero_ruby_no_wipe_schema'] = True if mutation == 'bool' else 2
    sent = []
    monkeypatch.setattr(NativeRendererProcess, 'start', lambda self: ready)
    monkeypatch.setattr(SkiaRendererProcess, 'abort', lambda *a, **k: None)
    monkeypatch.setattr(SkiaRendererProcess, '_send', lambda self, p: sent.append(p))
    with pytest.raises(NativeRendererError, match='零时长注音'): SkiaRendererProcess(binary).start()
    assert not sent


@pytest.mark.parametrize('target', ['gpu', 'raster'])
@pytest.mark.parametrize('kind', ['leading', 'all_zero'])
def test_initial_zero_alpha_overlap_is_local_to_the_exact_sample(binary, mark_source, target, kind):
    ir = ir_for(binary, zero_reading(mark_source, kind, translucent=True))
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, [199, 200, 201, 200])
    alpha = []
    for frame in frames:
        rows = bands(frame)[0]
        alpha.append(int(frame[rows[0]:rows[-1]+1, :, 3].max()))
    assert alpha == [128, 192, 128, 192]
    assert np.array_equal(frames[1], frames[3])


@pytest.mark.parametrize('gpu,missing_loader', [(True, False), (False, False), (True, True)])
def test_real_zero_ruby_preview(binary, mark_source, monkeypatch, tmp_path, gpu, missing_loader):
    run_preview(binary, monkeypatch, tmp_path, zero_reading(mark_source, 'leading', lower=True)[:2], gpu, missing_loader)


@pytest.mark.parametrize('gpu,missing_loader', [(True, False), (False, False), (True, True)])
def test_real_zero_ruby_mp4(binary, mark_source, monkeypatch, tmp_path, gpu, missing_loader):
    run_mp4(binary, monkeypatch, tmp_path, zero_reading(mark_source, 'interior')[:2], gpu, missing_loader)


@pytest.mark.parametrize('mode', ['reading_units', 'checkpoint_segments'])
def test_empty_source_window_keeps_existing_annotation_omission(binary, mark_source, mode):
    track, style, resolve = zero_reading(mark_source, 'all_zero', mode=mode)
    ruby = replace(track.rubies[0], pos_end_ms=track.rubies[0].pos_start_ms)
    ir = ir_for(binary, (replace(track, rubies=[ruby]), style, resolve))
    assert ir['track']['rubies']  # Source provenance is retained.
    assert all(not line.get('ruby_groups') for line in ir['skia_layout']['lines'])
    assert 'p07_zero_ruby_no_wipe' not in ir['required_features']


@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_inline_role_geometry_and_paints_keep_zero_ruby_clock(binary, mark_source, target):
    track, style, resolve = zero_reading(mark_source, 'leading', lower=True, translucent=True)
    line = track.lines[0]
    line = replace(line, chars=[line.chars[0], replace(line.chars[1], role_label='lead')])
    style = replace(style, custom_style_schemes={'lead': SubtitleStyleScheme(
        font_family='Latin', font_family_latin='Latin', font_size_px=70,
        karaoke_colors=colors('#FFFF0040', '#FF00FF40'))})
    ir = ir_for(binary, (replace(track, lines=[line]), style, resolve))
    smooth = ir_for(binary, (replace(track, lines=[line]), replace(style, karaoke_anim='none'), resolve))
    assert ir['skia_layout']['lines'] == smooth['skia_layout']['lines']
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, [199, 200, 201, 1900, 200])
    assert np.array_equal(frames[1], frames[-1])
    assert not np.array_equal(frames[0], frames[1]) and not np.array_equal(frames[1], frames[2])
    for cell, before, after in zip(ir['skia_layout']['lines'][0]['cells'],
                                   [(255, 0, 0, 255), (255, 0, 64, 255)],
                                   [(0, 255, 0, 255), (0, 255, 64, 255)]):
        ink = cell['metrics']['ink']
        x = int(cell['origin_x']+(ink[0]+ink[2])/2)
        y = int(cell['baseline_y']+(ink[1]+ink[3])/2)
        assert np.max(abs(frames[0][y, x].astype(int)-before)) <= 1
        assert np.max(abs(frames[3][y, x].astype(int)-after)) <= 1
