"""Forward horizontal solid scanline band follows the existing GPU's line front."""
from dataclasses import replace
import copy
import json
import sys

import numpy as np
import pytest

from test_subtitle_skia_line_fade import (
    application, binary, project, ir_for, collect, configure, selection, premultiplied,
)
from test_subtitle_skia_reverse import source as clock_source
from test_subtitle_skia_rtl_flow import rtl_source
from test_subtitle_skia_secondary_outline import two_outlines
from test_subtitle_skia_vector_guides import guide_source
from test_subtitle_skia_vertical import vertical_ruby_source
from krok_helper.subtitle_render.domain.models import SubtitleStyleScheme
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


def scanline_source(project, kind='plain'):
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
    return track, band(style, 18 if kind == 'narrow' else 30)


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
    assert 'p07_scanline_band' in ir['required_features'] and ir['skia_layout']['scanline_band_schema'] == 1
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


@pytest.mark.parametrize('case', ['brighten', 'glow', 'flame', 'image', 'effect', 'reverse', 'rtl', 'vertical',
                                  'ruby', 'roles', 'guide', 'utopia', 'no_wipe'])
def test_unproven_scanline_combinations_are_rejected(binary, project, case):
    track, style = scanline_source(project)
    if case == 'brighten': style = replace(style, scanline_mode='brighten')
    elif case == 'glow': style = replace(style, scanline_glow_px=8)
    elif case == 'flame': style = replace(style, scanline_shape='flame')
    elif case == 'image': style = replace(style, scanline_shape='image')
    elif case == 'effect': style = replace(style, scanline_effect='sakura')
    elif case == 'reverse':
        track = replace(track, lines=[replace(track.lines[0], wipe_reverse=True)])
        style = replace(style, scanline_reverse_enabled=True)
    elif case == 'rtl': track, style = rtl_source(project); style = band(style)
    elif case == 'vertical': track, style = vertical_ruby_source(project); track = replace(track, rubies=[]); style = band(style)
    elif case == 'ruby': track, style = vertical_ruby_source(project); style = band(replace(style, vertical=False))
    elif case == 'roles':
        style = replace(style, custom_style_schemes={'lead': SubtitleStyleScheme(font_size_px=70)})
        track = replace(track, lines=[replace(track.lines[0], chars=[
            replace(track.lines[0].chars[0], role_label='lead'), *track.lines[0].chars[1:]])])
    elif case == 'guide': track, style = guide_source(project); style = band(style)
    elif case == 'utopia': style = replace(style, karaoke_anim='utopia')
    else: style = replace(style, karaoke_anim='no_wipe')
    with pytest.raises(ValueError, match='扫字线'):
        ir_for(binary, (track, style))


@pytest.mark.parametrize('mutation', ['feature', 'schema', True, '1', 2, 'mode', 'glow', 'shape', 'reverse'])
def test_forged_scanline_ir_keeps_previous_scene(binary, project, mutation):
    ir = ir_for(binary, scanline_source(project))
    bad = copy.deepcopy(ir)
    if mutation == 'feature': bad['required_features'].remove('p07_scanline_band')
    elif mutation == 'schema': del bad['skia_layout']['scanline_band_schema']
    elif mutation == 'mode': bad['style']['scanline_mode'] = 'brighten'
    elif mutation == 'glow': bad['style']['scanline_glow_px'] = 4
    elif mutation == 'shape': bad['style']['scanline_shape'] = 'flame'
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


@pytest.mark.parametrize('kind', ['plain', 'shared', 'outlines', 'char_fade'])
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
                                      ('motion', 1.5)])
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
    banded = 0
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
            banded += 1
            xa, xb = np.where(ma.any(axis=0))[0], np.where(mb.any(axis=0))[0]
            assert abs(xa.min()-xb.min()) <= 1 and abs(xa.max()-xb.max()) <= 1, (kind, t, 'band', xa, xb)
            assert abs(band_mass(a) - band_mass(b)) / height <= 1, (kind, t, 'band mass')
    assert banded >= 8, (kind, banded)
