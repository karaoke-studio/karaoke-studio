"""The two karaoke states use the authored multi-pass glow source."""

from dataclasses import replace
import copy
import json
import sys

import numpy as np
import pytest

from test_subtitle_skia_static_shadow import application, binary, project, ir_for, collect, configure, selection
from test_subtitle_skia_static_shadow import shadow_source, shaded_ruby, premultiplied
from test_subtitle_skia_outline_ruby import (
    annotated, mixed, pages, reading, upper_ir, render_skia, UPPER_TIMES,
)
from krok_helper.subtitle_render.domain.paint import PaintFill
from krok_helper.subtitle_render.native.backend import NativeRendererError, NativeRendererProcess
from krok_helper.subtitle_render.native.skia_backend import SkiaRendererProcess
from scripts.subtitle_gpu_baseline.policy import COMPARISON_BUDGETS


CASES = ('merged', 'split', 'before', 'after', 'zero', 'three_passes', 'shadow_and_glow')
TIMES = [0, 199, 200, 399, 599, 600, 750, 900, 1200, 1799, 1800, 2200]


def glow_source(project, case):
    track, style = shadow_source(project, 'transparent')
    fields = {
        'decoration_kind': 'glow', 'decoration_kind_before': 'glow',
        'glow_before_radius_px': 8, 'glow_after_radius_px': 8,
        'glow_before_concentration_level': 1, 'glow_concentration_level': 1,
    }
    if case == 'split':
        fields.update(glow_before_radius_px=5, glow_before_concentration_level=0,
                      glow_after_radius_px=13, glow_concentration_level=2)
    elif case == 'before':
        fields['decoration_kind'] = 'none'
    elif case == 'after':
        fields['decoration_kind_before'] = 'none'
    elif case == 'zero':
        fields.update(glow_before_radius_px=0, glow_after_radius_px=0)
    elif case == 'three_passes':
        fields.update(glow_before_concentration_level=2, glow_concentration_level=2,
                      stroke_width_px=0, stroke2_enabled=False)
    elif case == 'shadow_and_glow':
        fields['decoration_kind_before'] = 'shadow'
    return track, replace(style, **fields)


@pytest.mark.parametrize('case', CASES)
@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_static_glow_palette_preserves_geometry_and_replay(binary, project, case, target, tmp_path):
    track, style = glow_source(project, case)
    ir = ir_for(binary, (track, style), dpr=1.5)
    plain = ir_for(binary, (track, replace(style, decoration_kind='none', decoration_kind_before='none')), dpr=1.5)
    assert 'p06_static_glow' in ir['required_features']
    assert ir['skia_layout']['lines'] == plain['skia_layout']['lines']
    frames_at = [0, 750, 2200, 750, 0]
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, frames_at)
        if case != 'zero':
            bad = copy.deepcopy(ir)
            bad['required_features'].remove('p06_static_glow')
            with pytest.raises(NativeRendererError):
                configure(process, bad)
            after, _ = collect(process, [750], generation=2)
            assert np.array_equal(frames[1], after[0])
        configure(process, plain)
        baseline, _ = collect(process, frames_at[:3], generation=3)
    assert np.array_equal(frames[1], frames[3]) and np.array_equal(frames[0], frames[4])
    first, middle, last = [not np.array_equal(a, b) for a, b in zip(frames[:3], baseline, strict=True)]
    assert first == (case not in ('after', 'zero'))
    assert last == (case not in ('before', 'zero'))
    assert middle == (case != 'zero')
    if target == 'gpu' and case == 'split':
        (tmp_path / 'static-glow-layout.json').write_text(json.dumps({
            'features': ir['required_features'], 'line': ir['skia_layout']['lines'][0],
        }, sort_keys=True), encoding='utf-8')


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('case,dpr', [(case, 1.5) for case in CASES] + [('merged', 1)])
def test_static_glow_matches_actual_d2d_and_gpu_raster(binary, project, case, dpr):
    from test_subtitle_render_gpu_backend import _render_g1_frames

    track, style = glow_source(project, case)
    with NativeRendererProcess() as process:
        _, raw = _render_g1_frames(process, style, tuple(TIMES), force_warp=False, track=track,
                                   width=640, height=360, dpr=dpr, realization_enabled=False)
    refs = [np.frombuffer(p, np.uint8).reshape(round(360*dpr), round(640*dpr), 4) for p in raw]
    ir = ir_for(binary, (track, style), dpr=dpr)
    draws = []
    for target in ('gpu', 'raster'):
        with SkiaRendererProcess(binary, selection=selection(target)) as process:
            configure(process, ir)
            frames, _ = collect(process, TIMES)
        draws.append(frames)
    assert_glow_parity(TIMES, refs, *draws, (case, dpr))


def assert_glow_parity(times, refs, gpu_frames, raster_frames, case, *, check_p00_edges=False):
    budget = COMPARISON_BUDGETS['image_categories']['decorated_text']
    for t, ref, gpu, raster in zip(times, refs, gpu_frames, raster_frames, strict=True):
        for name, first, second in (('D2D', ref, gpu), ('Raster', gpu, raster)):
            a, b = first[..., 3], second[..., 3]
            left, right = a > 16, b > 16
            union = left | right
            if not union.any() and not check_p00_edges:
                continue
            mass_a, mass_b = int(a.sum()), int(b.sum())
            assert abs(mass_a - mass_b) <= max(mass_a, mass_b) * budget['max_alpha_sum_relative'], (
                case, t, name, mass_a, mass_b)
            if not union.any():
                continue
            assert left.any() and right.any(), (case, t, name, 'missing image')
            if check_p00_edges:
                ya, xa = np.where(left)
                yb, xb = np.where(right)
                delta = np.array([xa.min() - xb.min(), ya.min() - yb.min(),
                                  xa.max() - xb.max(), ya.max() - yb.max()])
                assert abs(delta).max() <= budget['max_bbox_edge_delta_px_at_1080'], (case, t, name, delta)
            iou = (left & right).sum() / union.sum()
            assert iou >= budget['min_alpha_iou'], (case, t, name, iou)
            diff = abs(premultiplied(first)[union] - premultiplied(second)[union])
            assert diff.mean() <= budget['max_union_channel_mae'], (case, t, name, diff.mean())
            assert np.percentile(diff, 95) <= budget['max_union_p95'], (case, t, name)


def glowing_ruby(reading, case):
    track, style = shaded_ruby(reading, False, False)
    colors = style.ruby_karaoke_colors
    colors = replace(colors,
        before=replace(colors.before, shadow=PaintFill(color='#CCAA5820')),
        after=replace(colors.after, shadow=PaintFill(color='#B03084D0')))
    style = replace(style, decoration_before_sync=False,
                    decoration_kind='glow', decoration_kind_before='glow',
                    glow_before_radius_px=6, glow_after_radius_px=6,
                    glow_before_concentration_level=1, glow_concentration_level=1,
                    ruby_karaoke_colors=colors)
    if case == 'independent':
        style = replace(style, decoration_kind='none', decoration_kind_before='none',
            ruby_decoration_kind='glow', ruby_decoration_kind_before='glow',
            ruby_glow_before_radius_px=4, ruby_glow_after_radius_px=8,
            ruby_glow_before_concentration_level=0, ruby_glow_concentration_level=1)
    elif case == 'two_groups':
        original = track.rubies[0]
        first = replace(original, kanji='字', reading='A', reading_parts=['A'], reading_part_ms=[],
                        target_char_end=1, pos_end_ms=900)
        second = replace(first, target_char_start=1, target_char_end=2, pos_start_ms=900, pos_end_ms=2100)
        track = replace(track, rubies=[first, second])
    return track, style


@pytest.mark.parametrize('target', ['gpu', 'raster'])
@pytest.mark.parametrize('singing', [False, True])
def test_ruby_glow_keeps_clocks_and_seeks(binary, reading, target, singing, tmp_path):
    track, style = glowing_ruby(reading, 'two_groups')
    if singing:
        style = replace(style, karaoke_anim='utopia')
    ir = upper_ir(binary, (track, style), 1)
    plain = upper_ir(binary, (track, replace(style, ruby_decoration_kind='none',
                                             ruby_decoration_kind_before='none')), 1)
    assert {'p06_static_glow', 'p06_ruby_glow'} <= set(ir['required_features'])
    assert ('p07_singing_glow' in ir['required_features']) == singing
    assert 'p06_ruby_glow' not in plain['required_features']
    assert ir['skia_layout']['lines'] == plain['skia_layout']['lines']
    assert len(ir['skia_layout']['lines'][0]['ruby_groups']) == 2
    invalid = copy.deepcopy(ir)
    invalid['required_features'].remove('p07_singing_glow' if singing else 'p06_ruby_glow')
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, [299, 550, 2300, 550])
        with pytest.raises(NativeRendererError):
            configure(process, invalid)
        again, _ = collect(process, [550], generation=2)
        configure(process, plain)
        baseline, _ = collect(process, [299, 550, 2300], generation=3)
    assert np.array_equal(frames[1], frames[-1]) and np.array_equal(frames[1], again[0])
    assert all(np.count_nonzero(a[..., 3] > 16) > np.count_nonzero(b[..., 3] > 16)
               for a, b in zip(frames[:3], baseline, strict=True))
    if target == 'gpu':
        filename = 'ruby-singing-glow-layout.json' if singing else 'ruby-glow-layout.json'
        (tmp_path / filename).write_text(json.dumps({
            'features': ir['required_features'], 'line': ir['skia_layout']['lines'][0],
        }, sort_keys=True), encoding='utf-8')


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('case,dpr,singing', [
    ('inherited', 1, False), ('inherited', 1.5, False),
    ('independent', 1.5, False), ('two_groups', 1, False),
    ('inherited', 1, True), ('independent', 1.5, True), ('two_groups', 1, True),
])
def test_ruby_glow_matches_actual_d2d_and_gpu_raster(binary, reading, case, dpr, singing):
    from test_subtitle_render_gpu_backend import _render_g1_frames

    track, style = glowing_ruby(reading, case)
    if singing:
        style = replace(style, karaoke_anim='utopia')
    with NativeRendererProcess() as process:
        _, raw = _render_g1_frames(process, style, tuple(UPPER_TIMES), force_warp=False, track=track,
                                   width=640, height=360, dpr=dpr, realization_enabled=False)
    refs = [np.frombuffer(p, np.uint8).reshape(round(360*dpr), round(640*dpr), 4) for p in raw]
    gpu, raster = render_skia(binary, upper_ir(binary, (track, style), dpr), UPPER_TIMES)
    assert_glow_parity(UPPER_TIMES, refs, gpu, raster, (case, singing, dpr))


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('case_id', ['ordinary_dual_ruby_glow', 'font_variable_latin'])
def test_p00_glow_matches_actual_d2d_and_gpu_raster(binary, application, case_id, tmp_path):
    from PyQt6.QtGui import QFontDatabase
    from scripts.subtitle_gpu_baseline.cases import build_cases
    from test_subtitle_render_gpu_backend import _render_g1_frames

    if case_id == 'font_variable_latin' and 'Bahnschrift' not in QFontDatabase.families():
        pytest.skip('Bahnschrift system font is unavailable')
    case = next(case for case in build_cases(tmp_path / 'assets') if case.case_id == case_id)
    with NativeRendererProcess() as process:
        _, raw = _render_g1_frames(process, case.style, case.timestamps_ms, force_warp=False,
                                   track=case.track, width=1920, height=1080, dpr=1, realization_enabled=False)
    refs = [np.frombuffer(p, np.uint8).reshape(1080, 1920, 4) for p in raw]
    with SkiaRendererProcess(binary, selection=selection('gpu')) as gpu:
        gpu.configure(case.track, case.style, width=1920, height=1080, fps=60)
        gpu_frames, _ = collect(gpu, case.timestamps_ms)
    with SkiaRendererProcess(binary, selection=selection('raster')) as raster:
        raster.configure(case.track, case.style, width=1920, height=1080, fps=60)
        raster_frames, _ = collect(raster, case.timestamps_ms)
    assert_glow_parity(case.timestamps_ms, refs, gpu_frames, raster_frames, case.case_id, check_p00_edges=True)
