"""Primary solid body outline and its shared timed fill clips."""
from dataclasses import replace
import copy
import json
import sys

import numpy as np
import pytest

from test_subtitle_skia_rtl_flow import application, binary, project, rtl_source, collect, configure, ir_for, selection
from test_subtitle_skia_protocol import test_actual_g5_preview_uses_skia_even_when_gpu_off as run_preview
from test_subtitle_skia_protocol import test_product_export_short_mp4_uses_skia as run_mp4
from krok_helper.subtitle_render.domain.models import SubtitleStyleScheme
from krok_helper.subtitle_render.domain.paint import PaintFill
from krok_helper.subtitle_render.native.backend import NativeRendererError, NativeRendererProcess
from krok_helper.subtitle_render.native.skia_backend import SkiaRendererProcess


def outlined(project, width, *, translucent=False, semantics='n3_1074', effect='none'):
    track, style = rtl_source(project, effect=effect)
    a = 'A0' if translucent else 'FF'
    before = replace(style.karaoke_colors.before, text=PaintFill(color=f'#{a}FF0000'),
                     stroke=PaintFill(color='#FF0000FF'))
    after = replace(style.karaoke_colors.after, text=PaintFill(color=f'#{a}00FF00'),
                    stroke=PaintFill(color='#FFFF00FF'))
    colors = replace(style.karaoke_colors, before=before, after=after)
    return track, replace(style, right_to_left=False, stroke_width_px=width, karaoke_colors=colors,
                          layout_semantics=semantics)


def within_one_pixel(first, second):
    def dilate(mask):
        h, w = mask.shape
        padded = np.pad(mask, 1)
        return np.logical_or.reduce([padded[y:y+h, x:x+w] for y in range(3) for x in range(3)])
    return not np.any(first & ~dilate(second)) and not np.any(second & ~dilate(first))


def assert_geometry(left, right, info, extra_masks=()):
    for name, mask in (
        ('body_and_outline', lambda f: f[..., 3] > 127),
        ('body_after', lambda f: f[..., 1] > 127),
        ('outline', lambda f: f[..., 2] > 127),
        ('outline_after', lambda f: (f[..., 2] > 127) & (f[..., 0] > 127)),
        *extra_masks,
    ):
        a, b = mask(left), mask(right)
        if not within_one_pixel(a, b):
            def bounds(pixels):
                y, x = np.nonzero(pixels)
                return (x.min(), y.min(), x.max(), y.max()) if x.size else ()
            pytest.fail(f'{info} {name}: bounds {bounds(a)} and {bounds(b)}, '
                        f'only first {bounds(a & ~b)}, only second {bounds(b & ~a)}, sizes {a.sum()} and {b.sum()}')


@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_translucent_body_protects_fill_and_uses_padded_wipe(binary, project, target, tmp_path):
    ir = ir_for(binary, outlined(project, 8, translucent=True), dpr=1.5)
    assert 'p06_primary_solid_outline' in ir['required_features']
    cells = ir['skia_layout']['lines'][0]['cells']
    left, top, right, bottom = cells[0]['metrics']['ink']
    x, y = cells[0]['origin_x'], cells[0]['baseline_y']
    assert cells[0]['wipe_left'] == pytest.approx(x + left - 4)
    assert cells[0]['wipe_right'] == pytest.approx(x + right + 4)
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, [199, 2200])
    mid_y = round((y + (top + bottom) / 2) * 1.5)
    mid_x = round((x + (left + right) / 2) * 1.5)
    outline_x = round((x + left - 2) * 1.5)
    assert tuple(frames[0][mid_y, mid_x]) == (255, 0, 0, 160)
    assert tuple(frames[1][mid_y, mid_x]) == (0, 255, 0, 160)
    assert tuple(frames[0][mid_y, outline_x]) == (0, 0, 255, 255)
    assert tuple(frames[1][mid_y, outline_x]) == (255, 0, 255, 255)
    if target == 'gpu':
        (tmp_path / 'primary-solid-outline-layout.json').write_text(json.dumps({
            'schema': ir['skia_layout']['primary_solid_outline_schema'],
            'line': ir['skia_layout']['lines'][0],
            'intervals': ir['track']['lines'][0]['resolved_intervals'],
        }, sort_keys=True), encoding='utf-8')


@pytest.mark.parametrize('mutation', ['schema', 'feature', 'wipe_pad', 'second_outline', 'vertical'])
def test_unsupported_outline_changes_keep_configured_scene(binary, project, mutation):
    ir = ir_for(binary, outlined(project, 8, translucent=True))
    bad = copy.deepcopy(ir)
    if mutation == 'schema':
        bad['skia_layout']['primary_solid_outline_schema'] = True
    elif mutation == 'feature':
        bad['required_features'].remove('p06_primary_solid_outline')
    elif mutation == 'wipe_pad':
        bad['skia_layout']['lines'][0]['cells'][0]['wipe_left'] += 1
    elif mutation == 'second_outline':
        bad['style']['stroke2_width_px'] = 6
        bad['style']['stroke2_enabled'] = True
    else:
        bad['style']['vertical'] = True
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        configure(process, ir)
        before, _ = collect(process, [750])
        with pytest.raises(NativeRendererError):
            configure(process, bad)
        after, _ = collect(process, [750], generation=2)
    assert np.array_equal(before[0], after[0])


def latin_outline_project(project, use_role):
    track, style = outlined(project, 10, translucent=True)
    line = track.lines[0]
    chars = [replace(char, text=text, role_label='contrast' if use_role and i < 2 else None)
             for i, (char, text) in enumerate(zip(line.chars, ('A', '字', 'W', '한'), strict=True))]
    if use_role:
        role = SubtitleStyleScheme(stroke_width_px=12, latin_stroke_width_px=4, stroke2_enabled=False,
                                   karaoke_colors=style.karaoke_colors)
        style = replace(style, custom_style_schemes={'contrast': role})
    else:
        style = replace(style, latin_stroke_width_px=4)
    return replace(track, lines=[replace(line, chars=chars)]), style


@pytest.mark.parametrize('target', ['gpu', 'raster'])
@pytest.mark.parametrize('use_role', [False, True], ids=['main', 'role'])
def test_latin_primary_outline_uses_authored_cell_slot(binary, project, target, use_role):
    ir = ir_for(binary, latin_outline_project(project, use_role))
    assert 'p06_latin_primary_outline' in ir['required_features']
    pads = []
    for cell in ir['skia_layout']['lines'][0]['cells']:
        left = cell['metrics']['ink'][0]
        pads.append(cell['origin_x'] + left - cell['wipe_left'])
    assert pads == pytest.approx([2, 6, 5, 5] if use_role else [2, 5, 2, 5])
    undeclared = copy.deepcopy(ir)
    undeclared['required_features'].remove('p06_latin_primary_outline')
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        before, _ = collect(process, [550, 750])
        with pytest.raises(NativeRendererError):
            configure(process, undeclared)
        after, _ = collect(process, [750, 550], generation=2)
    assert not np.array_equal(before[0], before[1])
    assert np.array_equal(before[0], after[1]) and np.array_equal(before[1], after[0])


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('use_role,dpr', [(False, 1), (True, 1.5)])
def test_latin_primary_outline_matches_actual_gpu(binary, project, use_role, dpr):
    track, style = latin_outline_project(project, use_role)
    assert_outline_matches_actual_gpu(binary, track, style, dpr)


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('latin_enabled', [False, True], ids=['japanese-only', 'latin-only'])
def test_latin_secondary_outline_matches_actual_gpu(binary, project, latin_enabled):
    track, style = latin_outline_project(project, use_role=False)
    style = replace(style, latin_stroke_width_px=None, stroke2_enabled=not latin_enabled,
                    stroke2_width_px=5, latin_stroke2_enabled=latin_enabled, latin_stroke2_width_px=7)
    ir = ir_for(binary, (track, style))
    feature = 'p06_latin_secondary_outline'
    assert feature in ir['required_features']
    ir['required_features'].remove(feature)
    with SkiaRendererProcess(binary, selection=selection('raster')) as process, pytest.raises(NativeRendererError):
        configure(process, ir)
    assert_outline_matches_actual_gpu(binary, track, style, dpr=1)


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('width,translucent,semantics,dpr,effect,position,gap', [
    (6, False, 'legacy', 1, 'none', 'bottom', 0),
    (7, True, 'legacy', 1.5, 'none', 'bottom', 0),
    (8, False, 'n3_1074', 1.5, 'none', 'bottom', 0),
    (8, True, 'n3_1074', 1.5, 'none', 'top', 0),
    (9, True, 'n3_1074', 1, 'no_wipe', 'center', 0),
    (6, False, 'legacy', 1.5, 'none', 'top', 0),
    (6, True, 'n3_1074', 1, 'none', 'bottom', -10),
])
def test_primary_outline_matches_actual_gpu(binary, project, width, translucent, semantics, dpr, effect, position, gap):
    track, style = outlined(project, width, translucent=translucent, semantics=semantics, effect=effect)
    style = replace(style, line_y_position=position, letter_spacing_px=gap)
    assert_outline_matches_actual_gpu(binary, track, style, dpr)


def assert_outline_matches_actual_gpu(binary, track, style, dpr, extra_masks=()):
    from test_subtitle_render_gpu_backend import _render_g1_frames

    times = [199, 200, 399, 599, 600, 750, 899, 900, 1200, 1799, 1800, 2200]
    with NativeRendererProcess() as process:
        _, raw = _render_g1_frames(process, style, tuple(times), force_warp=False, track=track,
                                   width=640, height=360, dpr=dpr, realization_enabled=False)
    frames_d2d = [np.frombuffer(p, np.uint8).reshape(round(360*dpr), round(640*dpr), 4) for p in raw]
    ir = ir_for(binary, (track, style), dpr=dpr)
    with SkiaRendererProcess(binary, selection=selection('gpu')) as process:
        configure(process, ir)
        frames_gpu, _ = collect(process, times)
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        configure(process, ir)
        frames_cpu, _ = collect(process, times)
    for t, ref, gpu, cpu in zip(times, frames_d2d, frames_gpu, frames_cpu, strict=True):
        assert_geometry(ref, gpu, ('D2D', t), extra_masks)
        assert_geometry(gpu, cpu, ('Raster', t), extra_masks)


def test_primary_outline_product_preview(binary, project, monkeypatch, tmp_path):
    run_preview(binary, monkeypatch, tmp_path, outlined(project, 8), True, False)


def test_primary_outline_product_raster_export(binary, project, monkeypatch, tmp_path):
    run_mp4(binary, monkeypatch, tmp_path, outlined(project, 8, translucent=True), False, False)
