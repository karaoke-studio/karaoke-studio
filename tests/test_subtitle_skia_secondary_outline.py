"""Second solid outline, UseEdge2, and protected inner paint."""
from dataclasses import replace
import copy
import json
import sys

import numpy as np
import pytest

from test_subtitle_skia_primary_outline import (
    application, binary, project, outlined, ir_for, configure, collect, selection,
    assert_outline_matches_actual_gpu,
)
from krok_helper.subtitle_render.domain.paint import PaintFill
from krok_helper.subtitle_render.native.backend import NativeRendererError
from krok_helper.subtitle_render.native.skia_backend import SkiaRendererProcess


def two_outlines(project, *, inner=8, outer=6, body_alpha=False, inner_alpha=False, semantics='n3_1074'):
    track, style = outlined(project, inner, translucent=body_alpha, semantics=semantics)
    a = 'A0' if inner_alpha else 'FF'
    before = replace(style.karaoke_colors.before, stroke=PaintFill(color=f'#{a}0000FF'),
                     stroke2=PaintFill(color='#FF00FFFF'))
    after = replace(style.karaoke_colors.after, stroke=PaintFill(color=f'#{a}FF00FF'),
                    stroke2=PaintFill(color='#FFFFFF00'))
    colors = replace(style.karaoke_colors, before=before, after=after)
    return track, replace(style, stroke2_enabled=True, stroke2_width_px=outer, karaoke_colors=colors)


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('direction', ['reverse', 'rtl'])
def test_directional_solid_outlines_match_actual_d2d_gpu(binary, project, direction):
    from test_subtitle_render_gpu_backend import _render_g1_frames
    from test_subtitle_skia_outline_ruby import render_skia
    from test_subtitle_skia_static_glow import assert_glow_parity
    from krok_helper.subtitle_render.native.backend import NativeRendererProcess

    track, style = two_outlines(project, body_alpha=True, inner_alpha=True)
    track = replace(track, lines=[replace(line, wipe_reverse=direction == 'reverse') for line in track.lines])
    style = replace(style, right_to_left=direction == 'rtl')
    times, dpr = (250, 450, 750, 1100, 1400, 1750, 2200), 3
    refs = []
    for look in (style, replace(style, stroke_width_px=0, stroke2_enabled=False)):
        with NativeRendererProcess() as process:
            _, raw = _render_g1_frames(process, look, times, track=track, force_warp=False,
                                       width=640, height=360, dpr=dpr, realization_enabled=False)
        refs.append([np.frombuffer(frame, np.uint8).reshape(360*dpr, 640*dpr, 4) for frame in raw])
    assert any(np.count_nonzero((a[..., 3] > 16) & (b[..., 3] == 0)) > 100
               for a, b in zip(*refs, strict=True))
    ir = ir_for(binary, (track, style), dpr=dpr)
    assert 'p06_directional_solid_outline' in ir['required_features']
    gpu, raster = render_skia(binary, ir, times)
    assert_glow_parity(times, refs[0], gpu, raster, ('directional_solid_outlines', direction), check_p00_edges=True)


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
def test_reverse_singing_outlines_match_actual_d2d_gpu(binary, project):
    from test_subtitle_render_gpu_backend import _render_g1_frames
    from test_subtitle_skia_outline_ruby import render_skia
    from test_subtitle_skia_static_glow import assert_glow_parity
    from krok_helper.subtitle_render.native.backend import NativeRendererProcess

    track, style = two_outlines(project, body_alpha=True, inner_alpha=True)
    track = replace(track, lines=[replace(line, wipe_reverse=True) for line in track.lines])
    style = replace(style, reverse_karaoke_anim='utopia')
    times, dpr = (250, 450, 750, 1100, 1400, 1750, 2200), 3
    refs = []
    for look in (style, replace(style, reverse_karaoke_anim='none')):
        with NativeRendererProcess() as process:
            _, raw = _render_g1_frames(process, look, times, track=track, force_warp=False,
                                       width=640, height=360, dpr=dpr, realization_enabled=False)
        refs.append([np.frombuffer(frame, np.uint8).reshape(360*dpr, 640*dpr, 4) for frame in raw])
    assert any(np.count_nonzero(a != b) > 100 for a, b in zip(*refs, strict=True))
    ir = ir_for(binary, (track, style), dpr=dpr)
    assert 'p07_reverse_singing_outline' in ir['required_features']
    gpu, raster = render_skia(binary, ir, times)
    assert_glow_parity(times, refs[0], gpu, raster, 'reverse_singing_outlines', check_p00_edges=True)


@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_outer_ring_preserves_inner_alpha(binary, project, target, tmp_path):
    track, style = two_outlines(project, body_alpha=True, inner_alpha=True)
    ir = ir_for(binary, (track, style), dpr=1.5)
    assert 'p06_secondary_solid_outline' in ir['required_features']
    assert ir['skia_layout']['secondary_solid_outline_schema'] == 1
    line = ir['skia_layout']['lines'][0]
    cell = line['cells'][0]
    x, y = cell['origin_x'], cell['baseline_y']
    left, top, right, bottom = cell['metrics']['ink']
    assert cell['wipe_left'] == pytest.approx(x + left - 4)
    assert cell['wipe_right'] == pytest.approx(x + right + 4)
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, [199, 2200])
    row = round((y + (top + bottom) / 2) * 1.5)
    body_x = round((x + (left + right) / 2) * 1.5)
    inner_x = round((x + left - 2) * 1.5)
    outer_x = round((x + left - 5.5) * 1.5)
    assert tuple(frames[0][row, body_x]) == (255, 0, 0, 160)
    assert tuple(frames[0][row, inner_x]) == (0, 0, 255, 160)
    assert tuple(frames[0][row, outer_x]) == (0, 255, 255, 255)
    assert tuple(frames[1][row, outer_x]) == (255, 255, 0, 255)
    if target == 'gpu':
        (tmp_path / 'secondary-solid-outline-layout.json').write_text(json.dumps({
            'schema': ir['skia_layout']['secondary_solid_outline_schema'],
            'line': line,
            'intervals': ir['track']['lines'][0]['resolved_intervals'],
        }, sort_keys=True), encoding='utf-8')


@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_use_edge2_off_matches_an_unset_width(binary, project, target):
    track, style = two_outlines(project, semantics='legacy')
    disabled = ir_for(binary, (track, replace(style, stroke2_enabled=False)))
    absent = ir_for(binary, (track, replace(style, stroke2_width_px=0)))
    assert disabled['skia_layout']['lines'] == absent['skia_layout']['lines']
    assert 'p06_secondary_solid_outline' not in disabled['required_features']
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, disabled)
        before, _ = collect(process, [199, 600, 2200])
        configure(process, absent)
        after, _ = collect(process, [199, 600, 2200], generation=2)
    assert all(np.array_equal(a, b) for a, b in zip(before, after, strict=True))


@pytest.mark.parametrize('mutation', ['schema', 'primary_feature', 'secondary_feature'])
def test_native_requires_both_outline_capabilities(binary, project, mutation):
    ir = ir_for(binary, two_outlines(project))
    bad = copy.deepcopy(ir)
    if mutation == 'schema':
        bad['skia_layout']['secondary_solid_outline_schema'] = '1'
    else:
        bad['required_features'].remove(
            'p06_primary_solid_outline' if mutation == 'primary_feature' else 'p06_secondary_solid_outline'
        )
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        configure(process, ir)
        before, _ = collect(process, [750])
        with pytest.raises(NativeRendererError):
            configure(process, bad)
        after, _ = collect(process, [750], generation=2)
    assert np.array_equal(before[0], after[0])


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('inner,body_alpha,inner_alpha,semantics,dpr,enabled', [
    (8, False, False, 'legacy', 1, True),
    (7, True, False, 'legacy', 1.5, True),
    (8, False, True, 'n3_1074', 1.5, True),
    (8, True, True, 'n3_1074', 1.5, True),
    (0, True, False, 'n3_1074', 1, True),
    (8, True, True, 'legacy', 1.5, False),
])
def test_secondary_outline_matches_actual_gpu(binary, project, inner, body_alpha, inner_alpha, semantics, dpr, enabled):
    track, style = two_outlines(project, inner=inner, body_alpha=body_alpha, inner_alpha=inner_alpha, semantics=semantics)
    style = replace(style, stroke2_enabled=enabled)
    masks = (
        ('outer_before', lambda f: (f[..., 1] > 200) & (f[..., 2] > 200) & (f[..., 0] < 64)),
        ('outer_after', lambda f: (f[..., 1] > 200) & (f[..., 0] > 200) & (f[..., 2] < 64)),
    )
    assert_outline_matches_actual_gpu(binary, track, style, dpr, masks)
