"""Static shadow palettes, translation, silhouette and wipe clipping."""

from dataclasses import replace
import copy
import json
import sys

import numpy as np
import pytest

from test_subtitle_skia_line_fade import application, binary, project, ir_for, collect, configure, selection, premultiplied
from test_subtitle_skia_secondary_outline import two_outlines
from test_subtitle_skia_primary_outline import within_one_pixel
from krok_helper.subtitle_render.domain.paint import PaintFill
from krok_helper.subtitle_render.native.backend import NativeRendererError, NativeRendererProcess
from krok_helper.subtitle_render.native.skia_backend import SkiaRendererProcess
from scripts.subtitle_gpu_baseline.policy import COMPARISON_BUDGETS


TIMES = [0, 199, 200, 399, 599, 600, 750, 899, 900, 1200, 1799, 1800, 2200]
CASES = ('before', 'after', 'both', 'transparent', 'plain', 'zero_offset')


def shadow_source(project, case):
    track, style = two_outlines(project, body_alpha=case == 'transparent', inner_alpha=case == 'transparent')
    colors = style.karaoke_colors
    before = replace(colors.before, shadow=PaintFill(color='#A0E06010' if case == 'transparent' else '#E06010'))
    after = replace(colors.after, shadow=PaintFill(color='#902078E0' if case == 'transparent' else '#2078E0'))
    style = replace(style, karaoke_colors=replace(colors, before=before, after=after),
                    decoration_before_sync=False, decoration_kind='none' if case == 'before' else 'shadow',
                    decoration_kind_before='none' if case == 'after' else 'shadow',
                    shadow_offset_x=9, shadow_offset_y=7, shadow_before_offset_x=-7, shadow_before_offset_y=5)
    if case == 'zero_offset':
        style = replace(style, shadow_offset_x=0, shadow_offset_y=0, shadow_before_offset_x=0, shadow_before_offset_y=0)
    if case == 'plain':
        style = replace(style, stroke_width_px=0, stroke2_enabled=False,
                        decoration_before_sync=True, shadow_offset_x=-11, shadow_offset_y=-9)
    return track, style


@pytest.mark.parametrize('case', CASES)
@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_shadow_palette_keeps_geometry_and_replays(binary, project, case, target, tmp_path):
    track, style = shadow_source(project, case)
    ir = ir_for(binary, (track, style), dpr=1.5)
    plain = ir_for(binary, (track, replace(style, decoration_kind='none', decoration_kind_before='none')), dpr=1.5)
    assert 'p06_static_shadow' in ir['required_features']
    assert 'p06_static_shadow' not in plain['required_features']
    assert ir['skia_layout']['lines'] == plain['skia_layout']['lines']
    times = [0, 750, 2200, 750]
    bad = copy.deepcopy(ir)
    bad['required_features'].remove('p06_static_shadow')
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, times)
        with pytest.raises(NativeRendererError):
            configure(process, bad)
        again, _ = collect(process, [750], generation=2)
        configure(process, plain)
        baseline, _ = collect(process, times[:3], generation=3)
    assert np.array_equal(frames[1], frames[-1]) and np.array_equal(frames[1], again[0])
    first, middle, last = [not np.array_equal(a, b) for a, b in zip(frames[:3], baseline, strict=True)]
    assert first == (case not in ('after', 'zero_offset'))
    assert last == (case not in ('before', 'zero_offset'))
    assert middle == (case != 'zero_offset')
    if case == 'both' and target == 'gpu':
        (tmp_path / 'static-shadow-layout.json').write_text(json.dumps({
            'features': ir['required_features'], 'line': ir['skia_layout']['lines'][0],
        }, sort_keys=True), encoding='utf-8')


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('case,dpr', [(case, 1.5) for case in CASES] + [('both', 1)])
def test_static_shadow_matches_actual_d2d_and_gpu_raster(binary, project, case, dpr):
    from test_subtitle_render_gpu_backend import _render_g1_frames

    track, style = shadow_source(project, case)
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
    tolerance = COMPARISON_BUDGETS['image_categories']['text']
    for t, ref, gpu, raster in zip(TIMES, refs, *draws, strict=True):
        for name, first, second in (('D2D', ref, gpu), ('Raster', gpu, raster)):
            left, right = first[..., 3] > 16, second[..., 3] > 16
            union = left | right
            if not union.any():
                continue
            assert within_one_pixel(left, right), (case, t, name, 'silhouette')
            assert (left & right).sum() / union.sum() >= tolerance['min_alpha_iou'], (case, t, name, 'alpha')
            diff = abs(premultiplied(first)[union] - premultiplied(second)[union])
            assert diff.mean() <= tolerance['max_union_channel_mae'], (case, t, name, diff.mean())
            assert np.percentile(diff, 95) <= tolerance['max_union_p95'], (case, t, name)
