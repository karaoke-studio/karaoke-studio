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
from test_subtitle_skia_rtl_flow import colors, rtl_source
from test_subtitle_skia_secondary_outline import two_outlines
from test_subtitle_skia_vector_guides import guide_source
from test_subtitle_skia_vertical import vertical_ruby_source
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


@pytest.mark.parametrize('case', ['flame', 'image', 'effect', 'reverse', 'rtl', 'vertical',
                                  'ruby', 'roles', 'guide', 'utopia', 'no_wipe'])
def test_unproven_scanline_combinations_are_rejected(binary, project, case):
    track, style = scanline_source(project)
    if case == 'flame': style = replace(style, scanline_shape='flame')
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


@pytest.mark.parametrize('mutation', ['feature', 'schema', True, '1', 4, 'legacy_mode', 'legacy_glow', 'shape', 'reverse'])
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
                                         ('outlines', 8, 1.5), ('char_fade', 8, 1), ('motion', 8, 1.5)])
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
    ('brighten', 'plain', 0, 1), ('brighten', 'outlines', 8, 1.5), ('brighten', 'tiny', 0, 1),
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
