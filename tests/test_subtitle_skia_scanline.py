"""Forward horizontal solid scanline band follows the existing GPU's line front."""
from dataclasses import replace
import copy
import json
from pathlib import Path
import sys

import numpy as np
import pytest

from test_subtitle_skia_line_fade import (
    application, binary, project, ir_for, collect, configure, selection, premultiplied,
)
from test_subtitle_skia_reverse import source as clock_source
from test_subtitle_skia_rtl_flow import colors, rtl_source
from test_subtitle_skia_secondary_outline import two_outlines
from test_subtitle_skia_sing_particles import assert_particle_region, blue_coverage
from test_subtitle_skia_vector_guides import guide_source
from test_subtitle_skia_vertical import vertical_ruby_source
from test_subtitle_skia_outline_ruby import annotated, mixed, pages, reading, upper_source, upper_ir, bands
from krok_helper.subtitle_render.domain.models import SubtitleStyleScheme
from krok_helper.subtitle_render.domain.paint import PaintFill
from krok_helper.subtitle_render.domain.timing import LineAnimationOverride
from krok_helper.subtitle_render.native.backend import NativeRendererError, NativeRendererProcess
from krok_helper.subtitle_render.native.skia_backend import SkiaRendererProcess


# A@200-600 字@600-900 한@900-1400 W@1400-1800; 'pause' releases A at 400 (gap to 600).
TIMES = [199, 200, 201, 230, 300, 399, 400, 401, 500, 599, 600, 601, 750, 899, 900, 901,
         1100, 1399, 1400, 1401, 1600, 1799, 1800, 1801, 2200]
BAND = '#FFFFFF'


def band(style, width=30):
    return replace(style, scanline_enabled=True, scanline_forward_enabled=True, scanline_reverse_enabled=False,
                   scanline_mode='color', scanline_color=BAND, scanline_width_px=width, scanline_glow_px=0,
                   scanline_shape='band', scanline_effect='none')


def reverse_band(track, style):
    return (replace(track, lines=[replace(track.lines[0], wipe_reverse=True)]),
            replace(style, scanline_reverse_enabled=True))


def scanline_source(project, kind='plain'):
    # 'utopia*': ordinary Utopia singing (no zoom pulse, no entry/exit) under the band.
    utopia = kind.startswith('utopia')
    if utopia:
        kind = kind.removeprefix('utopia').lstrip('_') or 'plain'
    if kind == 'outlines':
        track, style = two_outlines(project)
    else:
        track, style = clock_source(project, kind if kind in {'shared', 'pause', 'zero'} else 'plain')
        track = replace(track, lines=[replace(track.lines[0], wipe_reverse=False)])
    style = replace(style, right_to_left=False)
    if kind == 'char_fade':
        style = replace(style, entry_anim='char_fade', entry_lead_ms=600, exit_anim='char_fade', exit_fade_ms=600)
    if kind == 'motion':
        style = replace(style, entry_anim='rise', entry_lead_ms=600, exit_anim='slide_out', exit_fade_ms=600)
    if kind == 'fade':
        style = replace(style, entry_anim='fade', entry_lead_ms=400, exit_anim='fade', exit_fade_ms=600)
    style = band(style, 18 if kind == 'narrow' else 30)
    if kind == 'glow':
        style = replace(style, scanline_glow_px=12)
    if utopia:
        style = replace(style, karaoke_anim='utopia')
    if kind == 'reverse':
        return reverse_band(track, style)
    return track, style


def band_mask(frame):
    return (frame[..., :3].min(axis=-1) > 200) & (frame[..., 3] > 200)


def band_mass(frame):
    return (frame[..., :3].min(axis=-1).astype(float) * frame[..., 3] / 255**2).sum()


@pytest.mark.parametrize('target', ['gpu', 'raster'])
@pytest.mark.parametrize('kind', ['plain', 'pause'])
def test_band_ir_contract_and_closed_windows(binary, project, target, kind, tmp_path):
    track, style = scanline_source(project, kind)
    ir = ir_for(binary, (track, style))
    plain = ir_for(binary, (track, replace(style, scanline_enabled=False)))
    assert 'p07_scanline_band' in ir['required_features'] and ir['skia_layout']['scanline_band_schema'] == 3
    assert 'p07_scanline_band' not in plain['required_features'] and 'scanline_band_schema' not in plain['skia_layout']
    assert ir['track']['lines'][0]['scanline'] is True
    assert ir['skia_layout']['lines'] == plain['skia_layout']['lines']
    assert ir['track']['lines'][0]['resolved_intervals'] == plain['track']['lines'][0]['resolved_intervals']
    times = [199, 300, 500, 750, 1100, 1600, 1800, 2200]
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, times)
        configure(process, plain)
        bare, _ = collect(process, times, generation=2)
    banded = {t: bool(band_mask(f).any()) for t, f in zip(times, frames)}
    # Before the first start, inside a release gap and once complete: no band.
    assert banded == {199: False, 300: True, 500: kind == 'plain', 750: True, 1100: True, 1600: True,
                      1800: False, 2200: False}
    for t, a, b in zip(times, frames, bare):
        if not banded[t]:
            assert np.array_equal(a, b), t
            continue
        xs = np.where(band_mask(a).any(axis=0))[0]
        assert xs.max() - xs.min() + 1 <= 12, (t, xs.min(), xs.max())  # 30px at 1080 -> 10px
        # The repaint stays inside the glyphs: no band pixel where the line had no ink.
        assert not (band_mask(a) & (b[..., 3] == 0)).any(), t
    if target == 'gpu' and kind == 'plain':
        (tmp_path / 'scanline-band-layout.json').write_text(json.dumps({
            'features': ir['required_features'], 'schema': ir['skia_layout']['scanline_band_schema'],
            'line': ir['skia_layout']['lines'][0], 'intervals': ir['track']['lines'][0]['resolved_intervals'],
            'style': {k: ir['style'][k] for k in ('scanline_mode', 'scanline_color', 'scanline_width_px',
                                                  'scanline_glow_px', 'scanline_shape')},
            'banded': {str(t): v for t, v in banded.items()},
        }, sort_keys=True), encoding='utf-8')


def test_line_overrides_resolve_before_the_capability(binary, project):
    track, style = scanline_source(project)
    off = replace(track.lines[0], animation_override=LineAnimationOverride(scanline_enabled=False))
    ir = ir_for(binary, (replace(track, lines=[off]), style))
    assert ir['track']['lines'][0]['scanline'] is False
    assert 'p07_scanline_band' not in ir['required_features'] and 'scanline_band_schema' not in ir['skia_layout']
    ir = ir_for(binary, (track, replace(style, scanline_forward_enabled=False)))
    assert 'p07_scanline_band' not in ir['required_features']


@pytest.mark.parametrize('case', ['flame', 'image', 'combined_particles', 'reverse_image', 'rtl', 'vertical',
                                  'ruby_role_utopia', 'roles', 'guide', 'zoom_role', 'utopia_entry', 'utopia_reverse',
                                  'no_wipe'])
def test_unproven_scanline_combinations_are_rejected(binary, project, case):
    track, style = scanline_source(project)
    if case == 'flame': style = replace(style, scanline_shape='flame', scanline_mode='brighten')
    elif case == 'image': style = replace(style, scanline_shape='image', scanline_mode='follow_after')
    elif case == 'combined_particles':
        track, style = reverse_band(track, replace(style, scanline_effect='sakura', sing_fx='twinkle'))
    elif case == 'reverse_image': track, style = reverse_band(track, replace(style, scanline_shape='image'))
    elif case == 'rtl': track, style = rtl_source(project); style = band(style)
    elif case == 'vertical': track, style = vertical_ruby_source(project); track = replace(track, rubies=[]); style = band(style)
    elif case == 'ruby_role_utopia':
        track, style = vertical_ruby_source(project)
        style = replace(band(replace(style, vertical=False)), scanline_mode='role', karaoke_anim='utopia')
    elif case == 'roles':
        style = replace(style, custom_style_schemes={'lead': SubtitleStyleScheme(font_size_px=70)})
        track = replace(track, lines=[replace(track.lines[0], chars=[
            replace(track.lines[0].chars[0], role_label='lead'), *track.lines[0].chars[1:]])])
    elif case == 'guide': track, style = guide_source(project); style = band(style)
    elif case == 'zoom_role': style = replace(style, karaoke_anim='zoom_pulse', scanline_mode='role')
    elif case == 'utopia_entry': style = replace(style, karaoke_anim='utopia', entry_anim='utopia', entry_lead_ms=600)
    elif case == 'utopia_reverse': track, style = reverse_band(track, replace(style, reverse_karaoke_anim='utopia'))
    else: style = replace(style, karaoke_anim='no_wipe')
    with pytest.raises(ValueError, match='扫字线|Utopia'):
        ir_for(binary, (track, style))


@pytest.mark.parametrize('mutation', ['feature', 'schema', True, '1', 7, 'legacy_mode', 'legacy_glow', 'shape', 'reverse',
                                      'legacy_utopia', 'utopia_zoom'])
def test_forged_scanline_ir_keeps_previous_scene(binary, project, mutation):
    ir = ir_for(binary, scanline_source(project))
    bad = copy.deepcopy(ir)
    if mutation == 'feature': bad['required_features'].remove('p07_scanline_band')
    elif mutation == 'schema': del bad['skia_layout']['scanline_band_schema']
    elif mutation == 'legacy_mode':
        bad['style']['scanline_mode'] = 'brighten'
        bad['skia_layout']['scanline_band_schema'] = 2
    elif mutation == 'legacy_glow':
        bad['style']['scanline_glow_px'] = 4
        bad['skia_layout']['scanline_band_schema'] = 1
    elif mutation == 'shape': bad['style']['scanline_shape'] = 'flame'
    elif mutation in {'legacy_utopia', 'utopia_zoom'}:
        bad = ir_for(binary, scanline_source(project, 'utopia'))
        if mutation == 'legacy_utopia':
            bad['skia_layout']['scanline_band_schema'] = 4
        else:
            bad['track']['lines'][0]['zoom_pulse'] = True
    elif mutation == 'reverse':
        bad['track']['lines'][0]['wipe_reverse'] = True
        bad['required_features'].append('p05_reverse_wipe')
        bad['skia_layout']['reverse_wipe_schema'] = 1
    else: bad['skia_layout']['scanline_band_schema'] = mutation
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        configure(process, ir)
        before, _ = collect(process, [750])
        with pytest.raises(NativeRendererError):
            configure(process, bad)
        after, _ = collect(process, [750], generation=2)
    assert band_mask(before[0]).any() and np.array_equal(before[0], after[0])


@pytest.mark.parametrize('kind', ['plain', 'shared', 'outlines', 'char_fade',
                                  'utopia', 'utopia_pause', 'utopia_outlines', 'utopia_glow'])
def test_gpu_and_raster_draw_the_same_band_under_out_of_order_seek(binary, project, kind):
    ir = ir_for(binary, scanline_source(project, kind), dpr=1.5)
    times = [*TIMES, 750, 300, 1600]
    results = []
    for target in ('gpu', 'raster'):
        with SkiaRendererProcess(binary, selection=selection(target)) as process:
            configure(process, ir)
            frames, _ = collect(process, times)
        for t, frame in zip(times[-3:], frames[-3:]):
            assert np.array_equal(frame, frames[TIMES.index(t)]), (target, t)
        results.append(frames[:len(TIMES)])
    for t, a, b in zip(TIMES, *results):
        ma, mb = band_mask(a), band_mask(b)
        assert ma.any() == mb.any(), (kind, t)
        if ma.any():
            xa, xb = np.where(ma.any(axis=0))[0], np.where(mb.any(axis=0))[0]
            assert abs(xa.min()-xb.min()) <= 1 and abs(xa.max()-xb.max()) <= 1, (kind, t, xa, xb)
        delta = abs(premultiplied(a) - premultiplied(b))
        assert delta.mean() < .15 and np.mean(delta.max(axis=-1) > 16) < .003, (kind, t)


def test_old_sidecar_is_rejected_before_backend_selection(binary, monkeypatch):
    process = SkiaRendererProcess(binary, selection=selection('raster'))
    try: ready = NativeRendererProcess.start(process)
    finally: process.abort(reason='handshake fixture')
    ready.pop('scanline_band_schema')
    sent = []
    monkeypatch.setattr(NativeRendererProcess, 'start', lambda self: ready)
    monkeypatch.setattr(SkiaRendererProcess, 'abort', lambda *a, **k: None)
    monkeypatch.setattr(SkiaRendererProcess, '_send', lambda self, p: sent.append(p))
    with pytest.raises(NativeRendererError, match='扫字线'): SkiaRendererProcess(binary).start()
    assert not sent


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('kind,dpr', [('plain', 1), ('plain', 1.5), ('pause', 1), ('zero', 1.5), ('shared', 1),
                                      ('narrow', 1.5), ('outlines', 1.5), ('char_fade', 1), ('fade', 1.5),
                                      ('motion', 1.5), ('reverse', 1.5),
                                      ('utopia', 1), ('utopia', 1.5), ('utopia_pause', 1), ('utopia_zero', 1.5),
                                      ('utopia_narrow', 1.5), ('utopia_outlines', 1.5)])
def test_band_matches_actual_d2d_gpu(binary, project, kind, dpr):
    """Original 1px ink, sung-front and band-edge budgets against actual D2D GPU."""
    from test_subtitle_render_gpu_backend import _render_g1_frames
    track, style = scanline_source(project, kind)
    with NativeRendererProcess() as process:
        _, raw = _render_g1_frames(process, style, tuple(TIMES), force_warp=False, track=track,
                                   width=640, height=360, dpr=dpr, realization_enabled=False)
    refs = [np.frombuffer(p, np.uint8).reshape(round(360*dpr), round(640*dpr), 4) for p in raw]
    with SkiaRendererProcess(binary, selection=selection('gpu')) as process:
        configure(process, ir_for(binary, (track, style), dpr=dpr))
        frames, _ = collect(process, TIMES)
    sung = lambda p: (p[..., 1].astype(float) * p[..., 3] / 255**2).sum()
    full_a, full_b = sung(refs[-1]), sung(frames[-1])
    banded = []
    for t, a, b in zip(TIMES, refs, frames, strict=True):
        if a[..., 3].max() == 0 or b[..., 3].max() == 0:
            assert a[..., 3].max() == b[..., 3].max() == 0, (kind, t)
            continue
        def bounds(frame):
            ys, xs = np.where(frame[..., 3] > 127)
            return np.array([xs.min(), ys.min(), xs.max(), ys.max()])
        aa, bb = bounds(a), bounds(b)
        assert abs(aa-bb).max() <= 1, (kind, t, 'ink', aa, bb)
        height = aa[3] - aa[1] + 1
        width = (full_a + full_b) / (2 * height)
        assert abs(sung(a)/full_a - sung(b)/full_b) * width <= 1, (kind, t, 'front')
        ma, mb = band_mask(a), band_mask(b)
        assert ma.any() == mb.any(), (kind, t, 'band presence')
        if ma.any():
            banded.append(t)
            xa, xb = np.where(ma.any(axis=0))[0], np.where(mb.any(axis=0))[0]
            assert abs(xa.min()-xb.min()) <= 1 and abs(xa.max()-xb.max()) <= 1, (kind, t, 'band', xa, xb)
            assert abs(band_mass(a) - band_mass(b)) / height <= 1, (kind, t, 'band mass')
    if kind == 'utopia_pause':
        assert len(banded) >= 5 and not set(banded).intersection({401, 500, 599}), (kind, banded)
    else:
        assert len(banded) >= 8, (kind, banded)


FEATHER_TIMES = [199, 230, 300, 399, 400, 500, 599, 600, 750, 900, 1100, 1600, 1800, 2200]


def feather_coverage(frame):
    return frame[..., :3].min(axis=-1).astype(float) * frame[..., 3] / 255**2


@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_feather_falls_off_inside_ink_and_clamps_to_half_width(binary, project, target, tmp_path):
    track, style = scanline_source(project, 'pause')
    inputs = [replace(style, scanline_glow_px=glow) for glow in (0, 15, 200)]
    raw, normal, cap = [ir_for(binary, (track, variant)) for variant in inputs]
    legacy = copy.deepcopy(raw)
    legacy['skia_layout']['scanline_band_schema'] = 1
    times = [199, 300, 500, 750, 1800, 300]
    clips = []
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        for generation, ir in enumerate((raw, normal, cap, legacy), start=1):
            configure(process, ir)
            frames, _ = collect(process, times, generation=generation)
            assert np.array_equal(frames[1], frames[-1])
            clips.append(frames)
    assert np.array_equal(clips[0], clips[3])
    assert np.array_equal(clips[1], clips[2])
    for time, hard, soft in zip(times, *clips[:2], strict=True):
        if time in (199, 500, 1800):
            assert np.array_equal(hard, soft)
            continue
        sharp, feathered = feather_coverage(hard), feather_coverage(soft)
        assert (feathered > .8).any()
        assert np.count_nonzero((sharp > .99) & (feathered > .02) & (feathered < .8)) >= 10
        assert not ((feathered > .01) & (hard[..., 3] == 0)).any()
        assert feathered.sum() < sharp.sum() * .75
    if target == 'gpu':
        (tmp_path / 'scanline-feather-layout.json').write_text(json.dumps({
            'schema': normal['skia_layout']['scanline_band_schema'],
            'line': normal['skia_layout']['lines'][0],
            'width': normal['style']['scanline_width_px'],
            'softness': normal['style']['scanline_glow_px'],
        }, sort_keys=True), encoding='utf-8')


def assert_feather_profile(reference, candidate, bare_reference, bare_candidate, sample):
    # Strip the static text colors and antialiased outline intersections.
    a = np.maximum(feather_coverage(reference) - feather_coverage(bare_reference), 0)
    b = np.maximum(feather_coverage(candidate) - feather_coverage(bare_candidate), 0)
    ma, mb = a > .2, b > .2
    assert ma.any() == mb.any(), (sample, 'band presence')
    if not ma.any():
        return
    xa, xb = np.where(ma.any(axis=0))[0], np.where(mb.any(axis=0))[0]
    assert abs(xa.min() - xb.min()) <= 1 and abs(xa.max() - xb.max()) <= 1, (sample, 'band edges')
    height = max(np.count_nonzero((ma | mb).any(axis=1)), 1)
    lo, hi = max(0, min(xa.min(), xb.min()) - 3), min(a.shape[1], max(xa.max(), xb.max()) + 4)
    ca, cb = a[:, lo:hi].sum(axis=0), b[:, lo:hi].sum(axis=0)
    assert abs(ca.sum() - cb.sum()) / height <= 1, (sample, 'feather coverage')
    # The D2D bitmap sampler can choose the neighboring texel at a half-pixel
    # tie. Check location above and compare the shape at that allowed shift.
    ca = np.pad(ca, 1)
    cb = np.pad(cb, 2)
    shape = min(abs(ca - cb[shift:shift + len(ca)]).sum() for shift in range(3)) / height
    assert shape <= 1, (sample, 'feather profile', shape)
    delta = abs(premultiplied(reference) - premultiplied(candidate))
    assert delta.mean() < .15, (sample, 'RGBA', delta.mean())


RUBY_BAND_TIMES = [299, 450, 550, 899, 1100, 1400, 1550, 1900, 2100, 2300]


def ruby_band_source(reading, softness, utopia=False):
    track, style = upper_source(reading, 2)
    style = replace(band(style, 42), scanline_glow_px=softness, ruby_interval_px=9)
    return track, replace(style, karaoke_anim='utopia') if utopia else style


@pytest.mark.parametrize('target', ['gpu', 'raster'])
@pytest.mark.parametrize('utopia', [False, True])
def test_upper_ruby_band_keeps_own_clock_and_seek(binary, reading, target, utopia, tmp_path):
    track, style = ruby_band_source(reading, 10, utopia)
    ir = upper_ir(binary, (track, style), 1)
    plain = upper_ir(binary, (track, replace(style, scanline_enabled=False)), 1)
    assert 'p07_ruby_scanline' in ir['required_features']
    assert ir['skia_layout']['scanline_band_schema'] == (5 if utopia else 3)
    assert ir['skia_layout']['lines'] == plain['skia_layout']['lines']
    bad = copy.deepcopy(ir)
    bad['required_features'].remove('p07_ruby_scanline')
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, [*RUBY_BAND_TIMES, 550])
        with pytest.raises(NativeRendererError):
            configure(process, bad)
        replay, _ = collect(process, [550], generation=2)
        configure(process, plain)
        baseline, _ = collect(process, RUBY_BAND_TIMES, generation=3)
    assert np.array_equal(frames[2], frames[-1]) and np.array_equal(frames[2], replay[0])
    reading_y = bands(baseline[0])[1][1] - 2
    ink = [not np.array_equal(a[:reading_y], b[:reading_y]) for a, b in zip(frames[:-1], baseline, strict=True)]
    assert ink[2] and ink[7] and not (ink[0] or ink[4] or ink[-1])
    if target == 'gpu':
        filename = 'ruby-utopia-scanline-layout.json' if utopia else 'ruby-scanline-layout.json'
        (tmp_path / filename).write_text(json.dumps({
            'features': ir['required_features'], 'line': ir['skia_layout']['lines'][0], 'banded': ink,
        }, sort_keys=True), encoding='utf-8')


@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_upper_ruby_scanline_particles_keep_clocks_and_seek(binary, reading, target, tmp_path):
    track, style = ruby_band_source(reading, 10, utopia=True)
    plain = upper_ir(binary, (track, style), 1.5)
    style = replace(style, scanline_effect='star', scanline_effect_speed_pct=-250)
    ir = upper_ir(binary, (track, style), 1.5)
    profile = 'p07_ruby_scanline_particles'
    assert profile in ir['required_features']
    final, = ir['skia_layout']['lines']
    source, = ir['track']['lines']
    assert final['ruby_groups'] == plain['skia_layout']['lines'][0]['ruby_groups']
    assert source['resolved_intervals'] == plain['track']['lines'][0]['resolved_intervals']
    bursts = source['fx_bursts']
    assert bursts and all(b['kind'] == 'scanline' and b['sprite'] == 'star4' and b['emit_ms'] > 0
                          and 'time_scale' not in b for b in bursts)
    bad = copy.deepcopy(ir)
    bad['required_features'].remove(profile)
    times = [299, 550, 1100, 1900, 2300]
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, [*times, 550])
        with pytest.raises(NativeRendererError):
            configure(process, bad)
        again, _ = collect(process, [550], generation=2)
        configure(process, plain)
        baseline, _ = collect(process, times, generation=3)
    assert np.array_equal(frames[0], baseline[0])
    assert not np.array_equal(frames[1], baseline[1])
    assert np.array_equal(frames[1], frames[-1]) and np.array_equal(frames[1], again[0])
    if target == 'gpu':
        (tmp_path / 'ruby-scanline-particles-layout.json').write_text(json.dumps({
            'features': ir['required_features'], 'groups': final['ruby_groups'], 'bursts': bursts,
        }, sort_keys=True), encoding='utf-8')


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('softness,dpr,utopia', [(0, 1, False), (10, 1, False), (10, 1.5, False),
                                                 (0, 1, True), (0, 1.5, True), (10, 1.5, True)])
def test_upper_ruby_band_matches_actual_d2d_and_gpu_raster(binary, reading, softness, dpr, utopia):
    from test_subtitle_render_gpu_backend import _render_g1_frames
    from test_subtitle_skia_outline_ruby import render_skia

    track, style = ruby_band_source(reading, softness, utopia)
    bare = replace(style, scanline_enabled=False)
    expected, expected_bare = [], []
    for look, output in ((style, expected), (bare, expected_bare)):
        with NativeRendererProcess() as process:
            _, raw = _render_g1_frames(process, look, tuple(RUBY_BAND_TIMES), force_warp=False, track=track,
                width=640, height=360, dpr=dpr, realization_enabled=False)
        output.extend(np.frombuffer(p, np.uint8).reshape(round(360*dpr), round(640*dpr), 4) for p in raw)
    gpu, raster = render_skia(binary, upper_ir(binary, (track, style), dpr), RUBY_BAND_TIMES)
    gpu_bare, raster_bare = render_skia(binary, upper_ir(binary, (track, bare), dpr), RUBY_BAND_TIMES)
    reading_y = bands(expected_bare[0])[1][1] - 2
    for time, ref, ref_bare, first, first_bare, second, second_bare in zip(
            RUBY_BAND_TIMES, expected, expected_bare, gpu, gpu_bare, raster, raster_bare, strict=True):
        top = slice(0, reading_y)
        assert_feather_profile(ref[top], first[top], ref_bare[top], first_bare[top],
                               ('D2D', time, softness, dpr, utopia))
        assert_feather_profile(first[top], second[top], first_bare[top], second_bare[top],
                               ('Raster', time, softness, dpr, utopia))


def test_feather_gpu_and_raster_match_under_repeated_seek(binary, project):
    track, style = scanline_source(project, 'outlines')
    ir = ir_for(binary, (track, replace(style, scanline_glow_px=15)), dpr=1.5)
    bare_ir = ir_for(binary, (track, replace(style, scanline_enabled=False)), dpr=1.5)
    times = [*reversed(FEATHER_TIMES), 300, 1600, 300]
    results, controls = [], []
    for target in ('gpu', 'raster'):
        with SkiaRendererProcess(binary, selection=selection(target)) as process:
            configure(process, ir)
            frames, _ = collect(process, times)
            configure(process, bare_ir)
            bare, _ = collect(process, times, generation=2)
        assert np.array_equal(frames[-3], frames[-1])
        results.append(frames)
        controls.append(bare)
    for time, gpu, raster, bare_gpu, bare_raster in zip(times, *results, *controls, strict=True):
        assert_feather_profile(gpu, raster, bare_gpu, bare_raster, time)


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('kind,glow,dpr', [('plain', 8, 1), ('plain', 15, 1.5), ('pause', 40, 1.5),
                                         ('outlines', 8, 1.5), ('char_fade', 8, 1), ('motion', 8, 1.5), ('reverse', 8, 1.5)])
def test_feather_profile_matches_actual_d2d_gpu(binary, project, kind, glow, dpr):
    from test_subtitle_render_gpu_backend import _render_g1_frames

    track, style = scanline_source(project, kind)
    style = replace(style, scanline_glow_px=glow)
    bare = replace(style, scanline_enabled=False)
    refs, controls = [], []
    for look in (style, bare):
        with NativeRendererProcess() as process:
            _, raw = _render_g1_frames(process, look, tuple(FEATHER_TIMES), force_warp=False,
                                       track=track, width=640, height=360, dpr=dpr, realization_enabled=False)
        refs.append([np.frombuffer(frame, np.uint8).reshape(round(360*dpr), round(640*dpr), 4) for frame in raw])
    with SkiaRendererProcess(binary, selection=selection('gpu')) as process:
        for generation, look in enumerate((style, bare), start=1):
            configure(process, ir_for(binary, (track, look), dpr=dpr))
            frames, _ = collect(process, FEATHER_TIMES, generation=generation)
            controls.append(frames)
    for time, a, bare_a, b, bare_b in zip(FEATHER_TIMES, *refs, *controls, strict=True):
        assert_feather_profile(a, b, bare_a, bare_b, (kind, glow, dpr, time))


PAINT_TIMES = [199, 300, 500, 750, 1100, 1600, 1800, 2200]


def paint_band(project, mode, kind='plain', softness=0, brightness=50):
    track, style = scanline_source(project, kind)
    palette = colors('#401020', '#103060')
    if kind == 'tiny':
        style = replace(style, scanline_width_px=2)
    if kind == 'outlines':
        palette = replace(style.karaoke_colors,
                          before=replace(style.karaoke_colors.before, text=palette.before.text),
                          after=replace(style.karaoke_colors.after, text=palette.after.text))
    return track, replace(
        style, scanline_mode=mode, scanline_glow_px=softness, scanline_brightness_pct=brightness,
        scanline_role_name='spotlight',
        custom_style_schemes={'spotlight': SubtitleStyleScheme(karaoke_colors=colors('#224412', '#DEBB83'))},
        karaoke_colors=palette,
    )


@pytest.mark.parametrize('target', ['gpu', 'raster'])
@pytest.mark.parametrize('mode,palette', [('follow_before', ((160, 40, 80),)),
                                         ('follow_after', ((29, 88, 176),)),
                                         ('brighten', ((160, 40, 80), (29, 88, 176))),
                                         ('role', ((222, 187, 131),))])
def test_plain_scanline_uses_the_authored_color_sides(binary, project, target, mode, palette, tmp_path):
    track, style = paint_band(project, mode)
    ir = ir_for(binary, (track, style))
    assert ir['skia_layout']['scanline_band_schema'] == 3
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, [750, 1100, 750])
    assert np.array_equal(frames[0], frames[2])
    visible = frames[0][..., :3][frames[0][..., 3] == 255]
    for color in palette:
        assert np.count_nonzero((visible == color).all(axis=-1)) > 10, (mode, target, color)
    if target == 'gpu' and mode == 'brighten':
        (tmp_path / 'scanline-paint-layout.json').write_text(json.dumps({
            'schema': ir['skia_layout']['scanline_band_schema'],
            'line': ir['skia_layout']['lines'][0], 'mode': mode, 'brightness': ir['style']['scanline_brightness_pct'],
        }, sort_keys=True), encoding='utf-8')


@pytest.mark.parametrize('kind', ['brightness_zero', 'tiny'])
def test_base_brighten_zero_and_subpixel_clips(binary, project, kind):
    track, style = paint_band(project, 'brighten', kind, brightness=0 if kind == 'brightness_zero' else 50)
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        configure(process, ir_for(binary, (track, style)))
        bright, _ = collect(process, PAINT_TIMES)
        configure(process, ir_for(binary, (track, replace(style, scanline_enabled=False))))
        bare, _ = collect(process, PAINT_TIMES, generation=2)
    if kind == 'brightness_zero':
        assert np.array_equal(bright, bare)
    else:
        delta = [abs(premultiplied(a) - premultiplied(b)).max() for a, b in zip(bright, bare)]
        assert 0 < max(delta) <= 7
        assert delta[0] == delta[-1] == 0


def test_gradient_role_scanline_is_explicitly_rejected(binary, project):
    track, style = paint_band(project, 'role')
    role = style.custom_style_schemes['spotlight']
    gradient = PaintFill(mode='gradient_horizontal', start_color='#224412', end_color='#DEBB83')
    paint = replace(role.karaoke_colors, after=replace(role.karaoke_colors.after, text=gradient))
    bad = replace(style, custom_style_schemes={'spotlight': replace(role, karaoke_colors=paint)})
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        configure(process, ir_for(binary, (track, style)))
        before, _ = collect(process, [750])
        with pytest.raises(NativeRendererError, match='角色取色'):
            configure(process, ir_for(binary, (track, bad)))
        after, _ = collect(process, [750], generation=2)
    assert np.array_equal(before, after)


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('mode,kind,softness,dpr', [
    ('follow_before', 'plain', 0, 1), ('follow_after', 'outlines', 8, 1.5),
    ('role', 'plain', 8, 1), ('role', 'outlines', 0, 1.5),
    ('brighten', 'plain', 0, 1), ('brighten', 'outlines', 8, 1.5), ('brighten', 'tiny', 0, 1), ('brighten', 'reverse', 6, 1.5),
])
def test_scanline_color_modes_match_actual_d2d_gpu(binary, project, mode, kind, softness, dpr):
    from test_subtitle_render_gpu_backend import _render_g1_frames

    track, style = paint_band(project, mode, kind, softness)
    bare = replace(style, scanline_enabled=False)
    reference, results = [], []
    times = [*PAINT_TIMES, 750]
    for look in (style, bare):
        with NativeRendererProcess() as process:
            _, raw = _render_g1_frames(process, look, tuple(times), force_warp=False, track=track,
                                       width=640, height=360, dpr=dpr, realization_enabled=False)
        reference.append([np.frombuffer(frame, np.uint8).reshape(round(360*dpr), round(640*dpr), 4)
                          for frame in raw])
    irs = [ir_for(binary, (track, look), dpr=dpr) for look in (style, bare)]
    for target in ('gpu', 'raster'):
        with SkiaRendererProcess(binary, selection=selection(target)) as process:
            frames = []
            for generation, ir in enumerate(irs, start=1):
                configure(process, ir)
                received, _ = collect(process, times, generation=generation)
                assert np.array_equal(received[3], received[-1])
                frames.append(received)
        results.append(frames)
    for time, expected, bare_d2d, gpu, bare_gpu, raster, bare_raster in zip(times, *reference, *results[0], *results[1]):
        base_a, base_b, base_c = (premultiplied(x) for x in (bare_d2d, bare_gpu, bare_raster))
        et, gt, rt = premultiplied(expected), premultiplied(gpu), premultiplied(raster)
        ref = abs(et - base_a)[..., :3].max(axis=-1)
        cand = abs(gt - base_b)[..., :3].max(axis=-1)
        cpu = abs(rt - base_c)[..., :3].max(axis=-1)
        assert bool((ref > 20).any()) == bool((cand > 20).any()) == bool((cpu > 20).any()), (mode, time)
        if kind == 'tiny':
            # The inset clips meet only within AA coverage at this width.
            # Vulkan can cull that intersection while D2D/Raster retain it.
            for delta in (ref, cand, cpu):
                assert delta.max() <= 7, (mode, time, 'subpixel coverage')
                assert np.count_nonzero((delta > 1).any(axis=0)) <= 1, (mode, time, 'subpixel extent')
        if (ref > 20).any():
            ys, xs = np.where(ref > 20)
            y2, x2 = np.where(cand > 20)
            assert max(abs(xs.min()-x2.min()), abs(xs.max()-x2.max()),
                       abs(ys.min()-y2.min()), abs(ys.max()-y2.max())) <= 1, (mode, time, 'ink')
            h = ys.max() - ys.min() + 1
            assert abs(ref.sum()-cand.sum()) / (255*h) <= 1, (mode, time, 'band mass')
        assert abs(et - gt).mean() < .15, (mode, time, 'RGBA')
        assert abs(gt - rt).mean() < .12, (mode, time, 'GPU/Raster')


SCANLINE_PARTICLE_SPRITES = [('star', 'star4'), ('sakura', 'petal'), ('feather', 'feather'),
                             ('snow', 'snow'), ('snow_solid', 'snow_solid'), ('note', 'note')]


def particle_band(project, kind, speed=0):
    track, style = scanline_source(project, 'pause')
    return track, replace(style, scanline_color='#FFFF00', scanline_effect=kind, scanline_effect_count=9,
                          scanline_effect_size_pct=55, scanline_effect_speed_pct=speed,
                          scanline_effect_color_mode='color', scanline_effect_color='#0000FF',
                          scanline_effect_color2='#0055FF')


@pytest.mark.parametrize('target', ['gpu', 'raster'])
@pytest.mark.parametrize('kind,sprite', SCANLINE_PARTICLE_SPRITES)
def test_scanline_particles_preserve_emit_clock_and_extend_tail(binary, project, kind, sprite, target, tmp_path):
    track, style = particle_band(project, kind, -250)
    ir = ir_for(binary, (track, style), dpr=1.5)
    original = ir_for(binary, (track, replace(style, scanline_effect_speed_pct=0)), dpr=1.5)
    line = ir['track']['lines'][0]
    bursts = line['fx_bursts']
    assert ir['skia_layout']['sing_twinkle_schema'] == 7
    assert {it['sprite'] for it in bursts} == {sprite}
    unchanged = ('kind', 'seed', 'sprite', 'char_index', 'count', 'start_ms', 'emit_ms')
    for default, slowed in zip(original['track']['lines'][0]['fx_bursts'], bursts, strict=True):
        assert all(default[name] == slowed[name] for name in unchanged)
        assert slowed['end_ms'] > default['end_ms']
        assert 'time_scale' not in slowed and slowed['front'] and not slowed['reverse']
    assert {it['emit_ms'] for it in bursts if it['char_index'] == 0} == {200}
    assert line['resolved_intervals'] == original['track']['lines'][0]['resolved_intervals']
    times = [199, 401, 500, 750, 1600, 1850, 2150, 3000, 500]
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, times)
    assert np.array_equal(frames[2], frames[-1])
    assert not blue_coverage(frames[0]).any() and not frames[-2][..., 3].any()
    assert max(blue_coverage(frame).max() for frame in frames[5:7]) > 25
    assert blue_coverage(frames[2]).max() > 12  # The first glyph already released.
    if kind == 'star' and target == 'gpu':
        (tmp_path / 'scanline-particles-layout.json').write_text(json.dumps({
            'bursts': bursts, 'line': ir['skia_layout']['lines'][0],
            'schema': ir['skia_layout']['sing_twinkle_schema'],
        }, sort_keys=True), encoding='utf-8')


@pytest.mark.parametrize('change', [{'emit_ms': 0}, {'emit_ms': 20000}, {'sprite': 'smoke'},
                                   {'time_scale': 2}, {'front': False}, {'reverse': True}])
def test_invalid_scanline_particle_keeps_raster_scene(binary, project, change):
    ir = ir_for(binary, particle_band(project, 'star'))
    bad = copy.deepcopy(ir)
    bad['track']['lines'][0]['fx_bursts'][0].update(change)
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        configure(process, ir)
        before, _ = collect(process, [500])
        with pytest.raises(NativeRendererError):
            configure(process, bad)
        after, _ = collect(process, [500], generation=2)
    assert np.array_equal(before, after)


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('kind,speed,reverse', [
    ('star', 0, False), ('star', -250, False), ('sakura', 140, False), ('note', 0, False),
    ('star', -250, True), ('note', 0, True),
])
def test_scanline_particle_tail_matches_actual_d2d_and_gpu_raster(binary, project, kind, speed, reverse):
    from test_subtitle_render_gpu_backend import _render_g1_frames

    track, style = particle_band(project, kind, speed)
    if reverse:
        track, style = reverse_band(track, style)
    times = [199, 300, 401, 500, 599, 750, 950, 1400, 1600, 1850, 2000, 2200, 3000]
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
    assert sum(blue_coverage(frame).max() > 30 for frame in reference) >= 4
    for time, d2d, gpu, raster in zip(times, reference, *results, strict=True):
        assert_particle_region(d2d, gpu, ('scanline', kind, speed, reverse, time), check_contour=False)
        assert_particle_region(gpu, raster, ('scanline', kind, speed, reverse, time, 'raster'), check_contour=False)



@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_reverse_scanline_band_and_particle_anchors_follow_singing_order(binary, project, target, tmp_path):
    track, style = particle_band(project, 'star', -200)
    track, style = reverse_band(track, replace(style, scanline_color=BAND))
    ir = ir_for(binary, (track, style), dpr=1.5)
    plain = ir_for(binary, (track, replace(style, scanline_effect='none')), dpr=1.5)
    line = ir['skia_layout']['lines'][0]
    bursts = ir['track']['lines'][0]['fx_bursts']
    assert ir['skia_layout']['scanline_band_schema'] == 4
    assert all(b['reverse'] and b['front'] and 'time_scale' not in b for b in bursts)
    assert [c['clock_index'] for c in line['cells']] == [3, 2, 1, 0]
    times = [199, 300, 500, 750, 1650, 1820, 2200, 3000, 500]
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, times)
        configure(process, plain)
        bands, _ = collect(process, times, generation=2)
    assert np.array_equal(frames[2], frames[-1])
    assert not blue_coverage(frames[0]).any() and not frames[-2][..., 3].any()
    # At the pause the first burst still belongs to the rightmost cell,
    # while the band only appears within the closed singing windows.
    assert np.array_equal(frames[0], bands[0])
    assert not band_mask(bands[2]).any()
    first_particle = np.where(blue_coverage(frames[2]) > 24)[1]
    assert first_particle.size and first_particle.mean() > 480
    columns = [np.where(band_mask(bands[i]))[1] for i in (1, 3, 4)]
    assert all(xs.size for xs in columns)
    moving_bands = [xs.mean() for xs in columns]
    assert moving_bands == sorted(moving_bands, reverse=True)
    if target == 'gpu':
        (tmp_path / 'scanline-reverse-layout.json').write_text(json.dumps({
            'bursts': bursts, 'line': line, 'schema': ir['skia_layout']['scanline_band_schema'],
        }, sort_keys=True), encoding='utf-8')



def combined_particles(project, singing, scan):
    track, style = particle_band(project, scan, -180)
    return track, replace(style, sing_fx=singing, fx_particle_color='#0000FF',
                          fx_particle_color2='#0000FF', fx_particle_speed_pct=140)


@pytest.mark.parametrize('target', ['gpu', 'raster'])
@pytest.mark.parametrize('kind', ['ripple', 'note'])
def test_combined_particle_layers_keep_both_clocks(binary, project, target, kind, tmp_path):
    track, style = combined_particles(project, kind, 'star')
    combined = ir_for(binary, (track, style), dpr=1.5)
    singing = ir_for(binary, (track, replace(style, scanline_effect='none')), dpr=1.5)
    scan = ir_for(binary, (track, replace(style, sing_fx='none')), dpr=1.5)
    assert combined['skia_layout']['sing_twinkle_schema'] == 8
    assert {'p07_sing_twinkle', 'p07_scanline_band'} <= set(combined['required_features'])
    bursts = combined['track']['lines'][0]['fx_bursts']
    assert {b['kind'] for b in bursts} == {kind, 'scanline'}
    for ir, expected_kind in ((singing, kind), (scan, 'scanline')):
        expected = ir['track']['lines'][0]['fx_bursts']
        actual = [b for b in bursts if b['kind'] == expected_kind]
        assert len(actual) == len(expected)
        for after, before in zip(actual, expected, strict=True):
            assert {k: v for k, v in after.items() if k != 'color_id'} == {
                k: v for k, v in before.items() if k != 'color_id'}
            assert combined['fx_color_table'][after['color_id']] == ir['fx_color_table'][before['color_id']]
    times = [240, 500, 620, 750, 1200, 1600, 1900, 2350, 3000, 750]
    variants = []
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        for index, ir in enumerate((combined, singing, scan), start=1):
            configure(process, ir)
            frames, _ = collect(process, times, generation=index)
            assert np.array_equal(frames[3], frames[-1])
            variants.append(frames)
    for single in variants[1:]:
        assert sum(not np.array_equal(a, b) for a, b in zip(variants[0][:-2], single[:-2], strict=True)) >= 3
    assert all(not frames[-2][..., 3].any() for frames in variants)
    if kind == 'ripple' and target == 'gpu':
        (tmp_path / 'particle-layers-layout.json').write_text(json.dumps({
            'schema': combined['skia_layout']['sing_twinkle_schema'],
            'bursts': bursts, 'line': combined['skia_layout']['lines'][0],
        }, sort_keys=True), encoding='utf-8')


def test_invalid_combined_particle_version_keeps_raster_scene(binary, project):
    ir = ir_for(binary, combined_particles(project, 'ripple', 'star'))
    bad = copy.deepcopy(ir)
    bad['skia_layout']['sing_twinkle_schema'] = 7
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        configure(process, ir)
        before, _ = collect(process, [620])
        with pytest.raises(NativeRendererError):
            configure(process, bad)
        after, _ = collect(process, [620], generation=2)
    assert np.array_equal(before, after)


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('singing,scan', [('note', 'star'), ('twinkle_classic', 'feather')])
def test_combined_particle_layers_match_actual_d2d_and_gpu_raster(binary, project, singing, scan):
    from test_subtitle_render_gpu_backend import _render_g1_frames

    track, style = combined_particles(project, singing, scan)
    times = [199, 300, 401, 500, 620, 750, 1100, 1600, 1850, 2000, 2200, 3000]
    with NativeRendererProcess() as process:
        _, raw = _render_g1_frames(process, style, tuple(times), force_warp=False, track=track,
                                   width=640, height=360, dpr=1.5, realization_enabled=False)
    refs = [np.frombuffer(p, np.uint8).reshape(540, 960, 4) for p in raw]
    ir = ir_for(binary, (track, style), dpr=1.5)
    results = []
    for target in ('gpu', 'raster'):
        with SkiaRendererProcess(binary, selection=selection(target)) as process:
            configure(process, ir)
            frames, _ = collect(process, times)
        results.append(frames)
    for time, d2d, gpu, raster in zip(times, refs, *results, strict=True):
        assert_particle_region(d2d, gpu, ('combined', singing, scan, time), check_contour=False)
        assert_particle_region(gpu, raster, ('combined', singing, scan, time, 'raster'), check_contour=False)


# scanline_band_schema=6: the band repaints over a static glow (main and upper ruby).
P00_SCANLINE_CASES = ['scanline_band', 'scanline_mode_color', 'scanline_mode_brighten', 'scanline_mode_role',
                      'scanline_mode_follow_before', 'scanline_mode_follow_after', 'scanline_particles_none',
                      'karaoke_scanline', 'karaoke_utopia_scanline', 'karaoke_zoom_pulse_scanline']
P00_SCANLINE_CASES += ['scanline_image', 'scanline_animated']
P00_SCANLINE_CASES += [f'scanline_particles_{kind}' for kind, _ in SCANLINE_PARTICLE_SPRITES]
P00_SCANLINE_CASES += ['scanline_flame', 'scanline_bubble']


def glow_band_source(reading, case, utopia=False):
    from test_subtitle_skia_static_glow import glowing_ruby
    track, style = glowing_ruby(reading, case)
    style = replace(band(style, 42), scanline_glow_px=10)
    return track, replace(style, karaoke_anim='utopia') if utopia else style


def glow_band_frames(binary, source, dpr, times):
    from test_subtitle_skia_outline_ruby import render_skia
    return render_skia(binary, upper_ir(binary, source, dpr), times)


@pytest.mark.parametrize('case,animation,flash', [('inherited', 'none', False), ('independent', 'none', False),
                                                 ('inherited', 'utopia', False), ('inherited', 'none', True),
                                                 ('inherited', 'zoom_pulse', True)])
def test_glow_band_capability_and_out_of_order_seek(binary, reading, case, animation, flash, tmp_path):
    track, style = glow_band_source(reading, case)
    style = replace(style, karaoke_anim=animation, karaoke_stroke_flash=flash)
    ir = upper_ir(binary, (track, style), 1.5)
    bare = upper_ir(binary, (track, replace(style, scanline_enabled=False)), 1.5)
    main_glow = case == 'inherited'
    assert ('p07_composed_scanline' in ir['required_features']) == (flash or animation == 'zoom_pulse')
    assert ir['skia_layout']['scanline_band_schema'] == (6 if main_glow else 5 if animation != 'none' else 3)
    assert ir['skia_layout']['lines'] == bare['skia_layout']['lines']
    order = [*RUBY_BAND_TIMES, 1100, 450, 1900]
    results = []
    for target in ('gpu', 'raster'):
        with SkiaRendererProcess(binary, selection=selection(target)) as process:
            configure(process, ir)
            frames, _ = collect(process, order)
            configure(process, bare)
            plain, _ = collect(process, RUBY_BAND_TIMES, generation=2)
        by_time = dict(zip(RUBY_BAND_TIMES, frames))
        for t, frame in zip(order[len(RUBY_BAND_TIMES):], frames[len(RUBY_BAND_TIMES):], strict=True):
            assert np.array_equal(frame, by_time[t]), (case, animation, target, t)
        # The band repaints while singing and leaves the unsung and completed frames alone.
        changed = [not np.array_equal(by_time[t], p) for t, p in zip(RUBY_BAND_TIMES, plain, strict=True)]
        assert any(changed) and not changed[0] and not changed[-1], (case, animation, target, changed)
        results.append([by_time[t] for t in RUBY_BAND_TIMES])
    from test_subtitle_skia_static_glow import assert_glow_parity
    assert_glow_parity(RUBY_BAND_TIMES, results[0], results[0], results[1], ('seek', case, animation, 1.5))
    if main_glow and animation != 'none':
        name = 'zoom-scanline-flash-layout.json' if animation == 'zoom_pulse' else 'scanline-glow-layout.json'
        (tmp_path / name).write_text(json.dumps({
            'features': ir['required_features'],
            'schema': ir['skia_layout']['scanline_band_schema'],
            'line': ir['skia_layout']['lines'][0],
        }, sort_keys=True), encoding='utf-8')


@pytest.mark.parametrize('animation', ['none', 'zoom_pulse', 'role'])
def test_glow_band_requires_its_capability_and_keeps_scene(binary, reading, animation, tmp_path):
    track, style = glow_band_source(reading, 'inherited')
    style = (replace(style, scanline_mode='role', scanline_role_name='__global__') if animation == 'role'
             else replace(style, karaoke_anim=animation))
    ir = upper_ir(binary, (track, style), 1)
    bad = copy.deepcopy(ir)
    if animation == 'zoom_pulse':
        bad['required_features'].remove('p07_composed_scanline')
    elif animation == 'role':
        bad['required_features'].remove('p07_role_scanline_layers')
    else:
        bad['skia_layout']['scanline_band_schema'] = 5
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        configure(process, ir)
        before, _ = collect(process, [1100])
        with pytest.raises(NativeRendererError):
            configure(process, bad)
        after, _ = collect(process, [1100], generation=2)
    assert before[0][..., 3].any() and np.array_equal(before, after)
    if animation == 'role':
        (tmp_path / 'ruby-role-scanline-layout.json').write_text(json.dumps({
            'features': ir['required_features'], 'line': ir['skia_layout']['lines'][0],
        }, sort_keys=True), encoding='utf-8')


@pytest.mark.parametrize('change', [
    {'decoration_kind': 'shadow', 'decoration_kind_before': 'shadow'},
    {'scanline_mode': 'role', 'scanline_role_name': '__global__', 'scanline_effect': 'star'},
    {'entry_anim': 'fade', 'entry_lead_ms': 400},
    {'karaoke_anim': 'zoom_pulse_scanline', 'scanline_shape': 'flame'},
    {'scanline_effect': 'star', 'sing_fx': 'twinkle'},
])
def test_unverified_glow_band_combinations_are_rejected(binary, reading, change):
    track, style = glow_band_source(reading, 'inherited')
    with pytest.raises(ValueError):
        upper_ir(binary, (track, replace(style, **change)), 1)


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('case,utopia,dpr', [('inherited', False, 1), ('inherited', False, 1.5),
                                            ('independent', False, 1.5), ('inherited', True, 1),
                                            ('inherited', True, 1.5)])
def test_glow_band_matches_actual_d2d_and_gpu_raster(binary, reading, case, utopia, dpr):
    from test_subtitle_render_gpu_backend import _render_g1_frames
    from test_subtitle_skia_static_glow import assert_glow_parity
    track, style = glow_band_source(reading, case, utopia)
    refs = {}
    for name, look in (('band', style), ('bare', replace(style, scanline_enabled=False))):
        with NativeRendererProcess() as process:
            _, raw = _render_g1_frames(process, look, tuple(RUBY_BAND_TIMES), force_warp=False, track=track,
                                       width=640, height=360, dpr=dpr, realization_enabled=False)
        refs[name] = [np.frombuffer(p, np.uint8).reshape(round(360*dpr), round(640*dpr), 4) for p in raw]
    gpu, raster = glow_band_frames(binary, (track, style), dpr, RUBY_BAND_TIMES)
    assert_glow_parity(RUBY_BAND_TIMES, refs['band'], gpu, raster, ('glow_band', case, utopia, dpr))
    # Both renderers really draw a band over the glow at the same samples.
    gpu_bare, _ = glow_band_frames(binary, (track, replace(style, scanline_enabled=False)), dpr, RUBY_BAND_TIMES)
    for t, ref, ref_bare, skia, skia_bare in zip(RUBY_BAND_TIMES, refs['band'], refs['bare'], gpu, gpu_bare,
                                                 strict=True):
        assert np.array_equal(ref, ref_bare) == np.array_equal(skia, skia_bare), (case, utopia, dpr, t)


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('case_id', P00_SCANLINE_CASES)
def test_p00_scanline_matches_actual_d2d_and_gpu_raster(binary, application, case_id, tmp_path):
    """Original 1080p P00 scanline cases: outlines, static glow, outlined upper ruby and a feathered band."""
    from scripts.subtitle_gpu_baseline.cases import build_cases
    from scripts.subtitle_gpu_baseline.policy import COMPARISON_BUDGETS
    from test_subtitle_render_gpu_backend import _render_g1_frames
    from test_subtitle_skia_static_glow import assert_glow_parity

    case = next(case for case in build_cases(tmp_path / 'assets') if case.case_id == case_id)
    animation = case.style.karaoke_anim
    bare_style = replace(case.style, scanline_enabled=False,
                         karaoke_anim=animation.removesuffix('_scanline') if animation.endswith('_scanline') else 'none')
    refs = {}
    for name, look in (('band', case.style), ('bare', bare_style)):
        with NativeRendererProcess() as process:
            _, raw = _render_g1_frames(process, look, case.timestamps_ms, force_warp=False,
                                       track=case.track, width=1920, height=1080, dpr=1, realization_enabled=False)
        refs[name] = [np.frombuffer(p, np.uint8).reshape(1080, 1920, 4) for p in raw]
    frames = {}
    for target in ('gpu', 'raster'):
        with SkiaRendererProcess(binary, selection=selection(target)) as process:
            process.configure(case.track, case.style, width=1920, height=1080, fps=60)
            frames[target], _ = collect(process, case.timestamps_ms)
    with SkiaRendererProcess(binary, selection=selection('gpu')) as process:
        process.configure(case.track, bare_style, width=1920, height=1080, fps=60)
        bare, _ = collect(process, case.timestamps_ms)
    assert_glow_parity(case.timestamps_ms, refs['band'], frames['gpu'], frames['raster'], case_id)
    limit = COMPARISON_BUDGETS['image_categories']['decorated_text']['max_bbox_edge_delta_px_at_1080']

    def bounds(frame):
        rows, columns = np.where(frame[..., 3] > 16)
        return np.array([columns.min(), rows.min(), columns.max(), rows.max()])

    banded = 0
    for t, ref, ref_bare, gpu, gpu_bare, raster in zip(
            case.timestamps_ms, refs['band'], refs['bare'], frames['gpu'], bare, frames['raster'], strict=True):
        assert np.max(np.abs(bounds(ref) - bounds(gpu))) <= limit, (case_id, t, 'D2D')
        assert np.max(np.abs(bounds(gpu) - bounds(raster))) <= limit, (case_id, t, 'Raster')
        drawn = not np.array_equal(ref, ref_bare)
        assert drawn == (not np.array_equal(gpu, gpu_bare)), (case_id, t, 'band presence')
        banded += drawn
    assert banded >= 1, case_id


# scanline_shape_schema=1: procedural flame/bubble at the front, solid color mode.
SHAPE_TIMES = [199, 300, 500, 750, 1100, 1600, 1800, 2200]


@pytest.mark.parametrize('shape', ['flame', 'bubble'])
@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_scanline_shapes_negotiate_draw_and_seek(binary, project, shape, target, tmp_path):
    track, style = scanline_source(project)
    ir = ir_for(binary, (track, replace(style, scanline_shape=shape)), dpr=1.5)
    band_ir = ir_for(binary, (track, style), dpr=1.5)
    assert 'p07_scanline_shape' in ir['required_features'] and ir['skia_layout']['scanline_shape_schema'] == 1
    assert 'p07_scanline_shape' not in band_ir['required_features']
    assert ir['skia_layout']['lines'] == band_ir['skia_layout']['lines']
    bad = copy.deepcopy(ir)
    bad['required_features'].remove('p07_scanline_shape')
    order = [*SHAPE_TIMES, 750, 300, 1600]
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, order)
        with pytest.raises(NativeRendererError):
            configure(process, bad)
        kept, _ = collect(process, [750], generation=2)
        configure(process, band_ir)
        bands, _ = collect(process, SHAPE_TIMES, generation=3)
    by_time = dict(zip(SHAPE_TIMES, frames))
    for t, frame in zip(order[len(SHAPE_TIMES):], frames[len(SHAPE_TIMES):], strict=True):
        assert np.array_equal(frame, by_time[t]), (shape, target, t)
    assert np.array_equal(kept[0], by_time[750])
    # The shape exists only while the band front is active; it adds ink around it.
    added = [int(np.abs(by_time[t][..., 3].astype(int) - b[..., 3].astype(int)).sum())
             for t, b in zip(SHAPE_TIMES, bands, strict=True)]
    assert added[0] == 0 and added[-1] == 0 and min(added[2:5]) > 1000, (shape, added)


    if target == 'gpu':
        (tmp_path / f'scanline-{shape}-layout.json').write_text(json.dumps({
            'shape': ir['style']['scanline_shape'], 'schema': ir['skia_layout']['scanline_shape_schema'],
            'line': ir['skia_layout']['lines'][0],
        }, sort_keys=True), encoding='utf-8')


@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_scanline_image_replays_and_missing_asset_restores_band(binary, project, target, tmp_path):
    track, style = scanline_source(project)
    source = Path(__file__).with_name('data') / 'guide_anim_3frames.gif'
    look = replace(style, scanline_shape='image', scanline_image_path=str(source),
                   scanline_image_band_enabled=False, scanline_image_scale_pct=250, scanline_image_offset_y_px=-70)
    valid = ir_for(binary, (track, look), dpr=1.5)
    missing = ir_for(binary, (track, replace(look, scanline_image_path=str(tmp_path/'missing.gif'))), dpr=1.5)
    band_ir = ir_for(binary, (track, style), dpr=1.5)
    assert 'p07_scanline_image' in valid['required_features']
    assert valid['skia_layout']['lines'] == missing['skia_layout']['lines'] == band_ir['skia_layout']['lines']
    times = [199, 550, 650, 750, 2200]
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, valid)
        frames, _ = collect(process, [*times, 750])
        configure(process, missing)
        fallback, _ = collect(process, times, generation=2)
        configure(process, band_ir)
        baseline, _ = collect(process, times, generation=3)
    assert np.array_equal(frames[3], frames[-1])
    assert all(np.array_equal(a, b) for a, b in zip(fallback, baseline, strict=True))
    assert np.array_equal(frames[0], fallback[0]) and np.array_equal(frames[4], fallback[4])
    # The fixture has 100ms red, green, then blue frames on the project clock.
    for index, channel in [(1, 2), (2, 0), (3, 1)]:
        sprite = frames[index][(frames[index][..., 3] > 200) & (fallback[index][..., 3] == 0), :3]
        assert len(sprite) and np.argmax(sprite.mean(axis=0)) == channel
    if target == 'gpu':
        (tmp_path / 'scanline-image-layout.json').write_text(json.dumps({
            'features': valid['required_features'], 'line': valid['skia_layout']['lines'][0],
        }, sort_keys=True), encoding='utf-8')
