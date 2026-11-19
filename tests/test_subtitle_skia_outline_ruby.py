"""Solid main outlines under upper ruby and lower romanization keep the GPU placement."""
from dataclasses import replace
import copy
import json
import sys

import numpy as np
import pytest

from test_subtitle_skia_lower_geometry import application, binary, prepared_source, source
from test_subtitle_skia_lower_product import ir_for as lower_ir
from test_subtitle_skia_ruby_groups import project, reading, pages, annotated, mixed, configure
from test_subtitle_skia_protocol import collect, selection
from test_subtitle_skia_roles import colors
from test_subtitle_skia_primary_outline import within_one_pixel
from test_subtitle_skia_line_fade import premultiplied
from krok_helper.subtitle_render.domain.paint import PaintFill
from krok_helper.subtitle_render.engine.render.skia_ir import build_skia_render_ir
from krok_helper.subtitle_render.native.backend import NativeRendererError, NativeRendererProcess
from krok_helper.subtitle_render.native.protocol import apply_resolved_font_faces
from krok_helper.subtitle_render.native.skia_backend import SkiaRendererProcess
from krok_helper.subtitle_render.native.skia_fonts import resolve_skia_font
from krok_helper.subtitle_render.native.skia_text import SkiaTextMetricsProcess
from scripts.subtitle_gpu_baseline.policy import COMPARISON_BUDGETS


UPPER_TIMES = [299, 300, 550, 899, 900, 1100, 1400, 1700, 2100, 2300]
LOWER_TIMES = [199, 200, 350, 400, 450, 699, 800, 1200, 1900]


def outlined(style, layers, body_alpha='FF'):
    """Red/green body with blue/yellow primary and cyan/magenta outer outline."""
    base = colors(f'#{body_alpha}FF0000', f'#{body_alpha}00FF00')
    before = replace(base.before, stroke=PaintFill(color='#FF0000FF'), stroke2=PaintFill(color='#FF00FFFF'))
    after = replace(base.after, stroke=PaintFill(color='#FFFFFF00'), stroke2=PaintFill(color='#FFFF00FF'))
    return replace(style, karaoke_colors=replace(base, before=before, after=after),
                   stroke_width_px=6 if layers == 1 else 5, stroke2_enabled=layers == 2,
                   stroke2_width_px=4 if layers == 2 else 0)


def upper_source(reading, layers, body_alpha='FF'):
    track, style = reading
    style = outlined(style, layers, body_alpha)
    return track, replace(style, ruby_karaoke_colors=colors('#FF0000', '#00FF00'))


def upper_ir(binary, source, dpr):
    track, style = source
    with SkiaTextMetricsProcess(binary) as metrics:
        ir = build_skia_render_ir(track, style, measure=metrics.measure, resolve_font=resolve_skia_font,
                                  width=640, height=360, fps=60, dpr=dpr)
    apply_resolved_font_faces(ir)
    return ir


def bands(frame):
    """Separate vertical ink bands (reading, main, romanization)."""
    mask = frame[..., 3] > 127
    ys = np.flatnonzero(mask.any(axis=1))
    out = []
    for rows in (v for v in np.split(ys, np.flatnonzero(np.diff(ys) > 1) + 1) if len(v)):
        xs = np.flatnonzero(mask[rows[0]:rows[-1]+1].any(axis=0))
        out.append(np.array([xs.min(), rows[0], xs.max(), rows[-1]]))
    return out


def assert_annotated_parity(times, refs, frames, sample, expected_bands):
    """Band boxes and the sung front per band within 1px; whole frame within the text budget."""
    budget = COMPARISON_BUDGETS['image_categories']['text']
    templates = bands(refs[-1])
    assert len(templates) == expected_bands, (sample, len(templates))
    def sung(frame, top, bottom):
        band = frame[top:bottom].astype(float)
        # Isolate the sung green body from cyan/yellow outlines.
        green = np.maximum(band[..., 1] - np.maximum(band[..., 0], band[..., 2]), 0)
        return (green * band[..., 3] / 255**2).sum()

    ranges = [(max(box[1] - 2, 0), box[3] + 3) for box in templates]
    totals = [(sung(refs[-1], lo, hi), sung(frames[-1], lo, hi)) for lo, hi in ranges]
    for t, a, b in zip(times, refs, frames, strict=True):
        left, right = bands(a), bands(b)
        assert len(left) == len(right), (*sample, t, len(left), len(right))
        if left:
            assert np.max(np.abs(np.array(left) - np.array(right))) <= 1, (*sample, t, left, right)
        for template, (lo, hi), (full_a, full_b) in zip(templates, ranges, totals, strict=True):
            current_a, current_b = sung(a, lo, hi), sung(b, lo, hi)
            width = (full_a + full_b) / (2 * (template[3] - template[1] + 1))
            assert full_a > 0 and full_b > 0, (*sample, template, 'no sung ink')
            assert (current_a == 0) == (current_b == 0), (*sample, t, template)
            assert abs(current_a/full_a - current_b/full_b) * width <= 1, (*sample, t, template, 'front')
        ma, mb = a[..., 3] > 16, b[..., 3] > 16
        union = ma | mb
        if not union.any():
            continue
        assert within_one_pixel(ma, mb), (*sample, t, 'ink')
        assert (ma & mb).sum() / union.sum() >= budget['min_alpha_iou'], (*sample, t, 'mask')
        diff = abs(premultiplied(a)[union] - premultiplied(b)[union])
        assert diff.mean() <= budget['max_union_channel_mae'], (*sample, t, diff.mean())
        assert np.percentile(diff, 95) <= budget['max_union_p95'], (*sample, t, np.percentile(diff, 95))


def render_skia(binary, ir, times):
    out = []
    for target in ('gpu', 'raster'):
        with SkiaRendererProcess(binary, selection=selection(target)) as process:
            configure(process, ir)
            frames, _ = collect(process, times)
        out.append(frames)
    return out


@pytest.mark.parametrize('layers', [1, 2])
@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_outlined_upper_ruby_declares_capability_and_replays(binary, reading, layers, target, tmp_path):
    source = upper_source(reading, layers)
    ir = upper_ir(binary, source, 1.5)
    features = ir['required_features']
    assert {'p06_outline_ruby', 'p06_primary_solid_outline', 'p05_resolved_ruby', 'p05_upper_ruby'} <= set(features)
    plain = upper_ir(binary, (source[0], replace(source[1], stroke_width_px=0, stroke2_enabled=False)), 1.5)
    assert 'p06_outline_ruby' not in plain['required_features']
    groups = ir['skia_layout']['lines'][0]['ruby_groups']
    # The outline widens the main box: the reading moves up by the main edge / 2.
    plain_groups = plain['skia_layout']['lines'][0]['ruby_groups']
    assert groups[0]['units'][0]['baseline_y'] < plain_groups[0]['units'][0]['baseline_y']
    order = [*UPPER_TIMES, 900, 299, 1700]
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, order)
        configure(process, ir)
        again, _ = collect(process, [1100], generation=2)
    by_time = dict(zip(UPPER_TIMES, frames))
    for t, frame in zip(order[len(UPPER_TIMES):], frames[len(UPPER_TIMES):], strict=True):
        assert np.array_equal(frame, by_time[t]), (layers, t)
    assert np.array_equal(again[0], by_time[1100])
    if layers == 2 and target == 'gpu':
        (tmp_path / 'outline-ruby-layout.json').write_text(json.dumps({
            'features': features, 'line': ir['skia_layout']['lines'][0],
        }, sort_keys=True), encoding='utf-8')


@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_outlined_lower_ruby_replays_all_three_bands(binary, source, target):
    track, style, resolve = source
    ir = lower_ir(binary, (track, outlined(style, 2), resolve), dpr=1.5)
    assert {'p06_outline_ruby', 'p05_lower_ruby'} <= set(ir['required_features'])
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, [199, 450, 1900, 450])
    assert all(len(bands(frame)) == 3 for frame in frames)
    assert not np.array_equal(frames[0], frames[2])
    assert np.array_equal(frames[1], frames[-1])


@pytest.mark.parametrize('mutation', ['feature', 'outline_feature'])
def test_outlined_ruby_without_capability_keeps_previous_scene(binary, reading, mutation):
    ir = upper_ir(binary, upper_source(reading, 2), 1)
    bad = copy.deepcopy(ir)
    if mutation == 'feature':
        bad['required_features'].remove('p06_outline_ruby')
    else:
        bad['required_features'] = [f for f in bad['required_features'] if 'solid_outline' not in f]
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        configure(process, ir)
        before, _ = collect(process, [1100])
        with pytest.raises(NativeRendererError):
            configure(process, bad)
        after, _ = collect(process, [1100], generation=2)
    assert before[0][..., 3].any() and np.array_equal(before, after)


@pytest.mark.parametrize('change', [{'ruby_stroke_width_px': 4}, {'ruby_stroke2_enabled': True, 'ruby_stroke2_width_px': 3}])
def test_annotation_outlines_remain_explicitly_gated(binary, reading, change):
    track, style = upper_source(reading, 1)
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        with pytest.raises(NativeRendererError):
            process.configure(track, replace(style, **change), width=640, height=360, fps=60)


def test_old_sidecar_without_outlined_ruby_is_rejected(binary, monkeypatch):
    process = SkiaRendererProcess(binary, selection=selection('raster'))
    try: ready = NativeRendererProcess.start(process)
    finally: process.abort(reason='handshake fixture')
    ready['profiles'] = [p for p in ready['profiles'] if p != 'p06_outline_ruby']
    sent = []
    monkeypatch.setattr(NativeRendererProcess, 'start', lambda self: ready)
    monkeypatch.setattr(SkiaRendererProcess, 'abort', lambda *a, **k: None)
    monkeypatch.setattr(SkiaRendererProcess, '_send', lambda self, p: sent.append(p))
    with pytest.raises(NativeRendererError, match='描边正文与注音'): SkiaRendererProcess(binary).start()
    assert not sent


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('semantics', ['legacy', 'n3_1074'])
@pytest.mark.parametrize('layers,body_alpha', [(1, 'FF'), (2, 'FF'), (2, 'A0')])
@pytest.mark.parametrize('dpr', [1, 1.5])
def test_outlined_upper_ruby_matches_actual_d2d_and_gpu_raster(binary, reading, semantics, layers, body_alpha, dpr):
    from test_subtitle_render_gpu_backend import _render_g1_frames
    track, style = upper_source(reading, layers, body_alpha)
    style = replace(style, layout_semantics=semantics)
    with NativeRendererProcess() as process:
        _, raw = _render_g1_frames(process, style, tuple(UPPER_TIMES), force_warp=False, track=track,
                                   width=640, height=360, dpr=dpr, realization_enabled=False)
    refs = [np.frombuffer(p, np.uint8).reshape(round(360*dpr), round(640*dpr), 4) for p in raw]
    gpu, raster = render_skia(binary, upper_ir(binary, (track, style), dpr), UPPER_TIMES)
    sample = ('upper', semantics, layers, body_alpha, dpr)
    assert_annotated_parity(UPPER_TIMES, refs, gpu, sample, 2)
    assert_annotated_parity(UPPER_TIMES, gpu, raster, (*sample, 'raster'), 2)


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('semantics', ['legacy', 'n3_1074'])
@pytest.mark.parametrize('layers', [1, 2])
@pytest.mark.parametrize('dpr', [1, 1.5])
def test_outlined_upper_and_lower_ruby_match_actual_d2d_and_gpu_raster(binary, source, semantics, layers, dpr):
    from test_subtitle_render_gpu_backend import _render_g1_frames
    track, style, resolve = source
    style = replace(outlined(style, layers), layout_semantics=semantics)
    with NativeRendererProcess() as process:
        _, raw = _render_g1_frames(process, style, tuple(LOWER_TIMES), force_warp=False, track=track,
                                   width=900, height=600, dpr=dpr, realization_enabled=False)
    refs = [np.frombuffer(p, np.uint8).reshape(round(600*dpr), round(900*dpr), 4) for p in raw]
    ir = lower_ir(binary, (track, style, resolve), dpr=dpr)
    assert {'p06_outline_ruby', 'p05_lower_ruby'} <= set(ir['required_features'])
    gpu, raster = render_skia(binary, ir, LOWER_TIMES)
    sample = ('lower', semantics, layers, dpr)
    assert_annotated_parity(LOWER_TIMES, refs, gpu, sample, 3)
    assert_annotated_parity(LOWER_TIMES, gpu, raster, (*sample, 'raster'), 3)
