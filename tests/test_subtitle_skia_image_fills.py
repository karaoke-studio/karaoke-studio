"""Static image paints tile on the canvas like the existing GPU's bitmap brush."""
from dataclasses import replace
import copy
import json
from pathlib import Path
import sys

import numpy as np
import pytest
from PyQt6.QtGui import QImage

from test_subtitle_skia_line_fade import application, binary, project, ir_for, collect, configure, selection
from test_subtitle_skia_gradients import TIMES, assert_text_parity, gradient_source, layer_paint
from krok_helper.subtitle_render.domain.models import LyricsLayout
from krok_helper.subtitle_render.domain.paint import PaintFill
from krok_helper.subtitle_render.native.backend import NativeRendererError, NativeRendererProcess
from krok_helper.subtitle_render.native.skia_backend import SkiaRendererProcess


def write_png(path, rgba):
    rgba = np.ascontiguousarray(rgba, dtype=np.uint8)
    height, width = rgba.shape[:2]
    image = QImage(rgba.tobytes(), width, height, width * 4, QImage.Format.Format_RGBA8888).copy()
    path.parent.mkdir(parents=True, exist_ok=True)
    assert image.save(str(path), 'PNG')
    return path


def pattern(kind):
    """Non-power-of-two tiles with distinct rows/columns, so misalignment is visible."""
    y, x = np.mgrid[0:17, 0:23]
    rgba = np.zeros((17, 23, 4), np.uint8)
    rgba[..., 0] = (x * 11) % 256
    rgba[..., 1] = (y * 15) % 256
    rgba[..., 2] = np.where((x // 4 + y // 4) % 2, 230, 40)
    rgba[..., 3] = 255
    if kind == 'transparent':
        rgba[..., 3] = np.where((x + y) % 5 == 0, 0, 90 + (x * 7) % 160)
    return rgba


@pytest.fixture
def images(tmp_path):
    folder = tmp_path / '画像フィル'
    return {kind: write_png(folder / f'テクスチャ_{kind}.png', pattern(kind)) for kind in ('opaque', 'transparent')}


def image_paint(path, scale=100, fallback='#FF808080'):
    return PaintFill(mode='image', image_path=str(path), image_scale_pct=scale, color=fallback)


# (body before/after, outline before/after, outer before/after); None = no such layer.
def image_source(project, images, case):
    track, style = gradient_source(project, 'gradient_vertical')
    colors = style.karaoke_colors
    before, after = colors.before, colors.after
    missing = images['opaque'].parent / 'ない.png'
    stroke = stroke2 = 0
    if case == 'body':
        before = replace(before, text=image_paint(images['opaque']))
        after = replace(after, text=PaintFill(color='#FF00FF00'))
    elif case == 'mixed':
        # Translucent image body under a gradient outline and solid outer ring.
        before = replace(before, text=image_paint(images['transparent']),
                         stroke=layer_paint('gradient_horizontal', '#FF2040E0', '#FFF0D020'),
                         stroke2=PaintFill(color='#FF102060'))
        after = replace(after, text=layer_paint('gradient_vertical', '#FF28CB53', '#FFBB37AF'),
                        stroke=image_paint(images['opaque'], 137), stroke2=PaintFill(color='#FF601020'))
        stroke, stroke2 = 5, 4
    elif case == 'outlines':
        # Image rings at a non-integer scale around a solid body.
        before = replace(before, text=PaintFill(color='#FFFF0000'), stroke=image_paint(images['opaque'], 137),
                         stroke2=image_paint(images['transparent'], 61))
        after = replace(after, text=PaintFill(color='#FF00FF00'), stroke=image_paint(images['transparent'], 137),
                        stroke2=image_paint(images['opaque'], 61))
        stroke, stroke2 = 6, 5
    else:  # missing: the GPU falls back to each paint's solid colour, still protecting the body.
        before = replace(before, text=image_paint(missing, fallback='#A0E04418'),
                         stroke=image_paint(missing, fallback='#FF2040E0'))
        after = replace(after, text=image_paint(missing, fallback='#FF28CB53'),
                        stroke=image_paint(images['opaque']))
        stroke = 5
    return track, replace(style, karaoke_colors=replace(colors, before=before, after=after),
                          stroke_width_px=stroke, stroke2_enabled=stroke2 > 0, stroke2_width_px=stroke2)


CASES = ('body', 'mixed', 'outlines', 'missing')


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('direction', ['reverse', 'rtl'])
def test_directional_body_paints_match_actual_d2d_gpu(binary, project, images, direction):
    from test_subtitle_render_gpu_backend import _render_g1_frames
    from test_subtitle_skia_outline_ruby import render_skia
    from test_subtitle_skia_static_glow import assert_glow_parity

    track, style = image_source(project, images, 'mixed')
    track = replace(track, lines=[replace(line, wipe_reverse=direction == 'reverse') for line in track.lines])
    style = replace(style, right_to_left=direction == 'rtl')
    color = style.karaoke_colors
    plain = replace(style, karaoke_colors=replace(color,
        before=replace(color.before, text=PaintFill(color='#A0E04418')),
        after=replace(color.after, text=PaintFill(color='#FF28CB53'))))
    times, dpr = (175, 250, 450, 750, 1100, 1400, 1750, 2200), 3
    refs = []
    for look in (style, plain):
        with NativeRendererProcess() as process:
            _, pixels = _render_g1_frames(process, look, times, force_warp=False, track=track,
                                          width=640, height=360, dpr=dpr, realization_enabled=False)
        refs.append([np.frombuffer(frame, np.uint8).reshape(360*dpr, 640*dpr, 4) for frame in pixels])
    assert np.count_nonzero(refs[0][0] != refs[1][0]) > 100
    assert np.count_nonzero(refs[0][-1] != refs[1][-1]) > 100
    ir = ir_for(binary, (track, style), dpr=dpr)
    assert 'p06_directional_body_paints' in ir['required_features']
    gpu, raster = render_skia(binary, ir, times)
    assert_glow_parity(times, refs[0], gpu, raster, ('directional_body_paints', direction), check_p00_edges=True)


@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_multitrack_paints_keep_independent_domains(binary, project, images, target):
    from test_subtitle_skia_static_glow import assert_glow_parity

    track, style = image_source(project, images, 'mixed')
    style = replace(style, line_y_position='bottom',
                    layouts=[LyricsLayout(layout_id='top', line_y_position='top', line_y_margin_px=55)])
    line = replace(track.lines[0], display_start_override_ms=0, display_end_override_ms=2700)
    track = replace(track, lines=[line])
    side = replace(track, lines=[replace(line, chars=line.chars[:2], layout_index=1)],
                   meta=replace(track.meta, offset_ms=500))
    primary_ir = ir_for(binary, (track, style))
    secondary_ir = ir_for(binary, (side, style))
    combined_ir = ir_for(binary, (track, style), extra_tracks=[side])
    for separate, merged in ((primary_ir, combined_ir), (secondary_ir, combined_ir['skia_extra_sources'][0])):
        assert separate['skia_layout']['lines'] == merged['skia_layout']['lines']
    times = [0, 499, 500, 1200, 2500, 2700, 3200, 1200]
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        outputs = []
        for generation, (main, extras) in enumerate(((track, []), (side, []), (track, [side])), 1):
            process.configure(main, style, width=640, height=360, fps=60, extra_tracks=extras)
            frames, _ = collect(process, times, generation=generation)
            outputs.append(np.stack(frames))
    primary, secondary, combined = outputs
    assert primary[0].any() and not secondary[:2].any() and secondary[2].any()
    assert not np.any((primary[..., 3] > 0) & (secondary[..., 3] > 0))
    expected = np.maximum(primary, secondary)
    if target == 'raster':
        assert np.array_equal(combined, expected)
    else:
        assert_glow_parity(times, expected, combined, combined, 'paint domains', check_p00_edges=True)
    assert np.array_equal(combined[3], combined[-1])


@pytest.mark.skipif(sys.platform != 'win32', reason='Direct2D GPU comparison')
@pytest.mark.parametrize('mode', ['image', 'gradient_vertical'])
def test_multitrack_paints_match_actual_d2d_gpu(binary, application, tmp_path, mode):
    from scripts.subtitle_gpu_baseline.cases import build_cases
    from test_subtitle_render_gpu_backend import _render_g1_frames
    from test_subtitle_skia_static_glow import assert_glow_parity

    case = next(case for case in build_cases(tmp_path) if case.case_id == f'paint_{mode}')
    track = case.track
    style = replace(case.style, decoration_kind='none',
                    layouts=[LyricsLayout(layout_id='top', line_y_position='top', line_y_margin_px=140)])
    side_line = replace(track.lines[0], chars=track.lines[0].chars[2:], layout_index=1)
    side = replace(track, lines=[side_line], meta=replace(track.meta, offset_ms=900))
    times = (299, 600, 1199, 1200, 1500, 2600, 4400, 5500, 6399, 6400)
    with NativeRendererProcess() as d2d:
        _, raw = _render_g1_frames(d2d, style, times, track=track, extra_tracks=[side], force_warp=False,
                                   width=1920, height=1080, realization_enabled=False)
    reference = [np.frombuffer(frame, np.uint8).reshape(1080, 1920, 4) for frame in raw]
    output = []
    for target in ('gpu', 'raster'):
        with SkiaRendererProcess(binary, selection=selection(target)) as process:
            process.configure(track, style, extra_tracks=[side], width=1920, height=1080, fps=60)
            frames, _ = collect(process, times)
            output.append(frames)
    assert_glow_parity(times, reference, *output, f'multitrack_paints_{mode}', check_p00_edges=True)


@pytest.mark.parametrize('case', CASES)
@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_image_fills_declare_capability_seek_and_reconfigure(binary, project, images, case, target, tmp_path):
    track, style = image_source(project, images, case)
    ir = ir_for(binary, (track, style), dpr=1.5)
    assert 'p06_image_fill' in ir['required_features']
    assert ('body_gradient_bounds' in ir['skia_layout']['lines'][0]) == (case == 'mixed')
    order = [*TIMES, 750, 0, 2200]
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, order)
        configure(process, ir)
        again, _ = collect(process, [2200, 750], generation=2)
    by_time = dict(zip(TIMES, frames))
    for t, frame in zip(order[len(TIMES):], frames[len(TIMES):], strict=True):
        assert np.array_equal(frame, by_time[t]), (case, t)
    assert np.array_equal(again[0], by_time[2200]) and np.array_equal(again[1], by_time[750])
    assert by_time[200][..., 3].any() and not np.array_equal(by_time[200], by_time[2200])
    if case == 'body' and target == 'gpu':
        paint = ir['style']['karaoke_colors']['before']['text']
        (tmp_path / 'image-fill-layout.json').write_text(json.dumps({
            'features': ir['required_features'], 'line': ir['skia_layout']['lines'][0],
            # The temporary root differs per machine; keep the Unicode file name.
            'paint': {**paint, 'image_path': Path(paint['image_path']).name},
        }, sort_keys=True, ensure_ascii=False), encoding='utf-8')


def test_replaced_and_deleted_images_take_effect_on_reconfigure(binary, project, images):
    track, style = image_source(project, images, 'body')
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        configure(process, ir_for(binary, (track, style), dpr=1.5))
        first, _ = collect(process, [200])
        write_png(images['opaque'], pattern('opaque')[::-1, ::-1])
        configure(process, ir_for(binary, (track, style), dpr=1.5))
        replaced, _ = collect(process, [200], generation=2)
        images['opaque'].unlink()
        configure(process, ir_for(binary, (track, style), dpr=1.5))
        missing, _ = collect(process, [200], generation=3)
    coverage = [frame[0][..., 3] > 0 for frame in (first, replaced, missing)]
    assert np.array_equal(coverage[0], coverage[1]) and np.array_equal(coverage[0], coverage[2])
    assert not np.array_equal(first, replaced)
    # Missing images fall back to the paint's solid colour.
    body = missing[0][missing[0][..., 3] == 255]
    assert body.size and np.all(body[:, :3] == body[0, :3])


@pytest.mark.parametrize('mutation', ['feature', 'gradient_feature'])
def test_invalid_image_fill_keeps_previous_scene(binary, project, images, mutation):
    ir = ir_for(binary, image_source(project, images, 'mixed'))
    bad = copy.deepcopy(ir)
    bad['required_features'].remove('p06_image_fill' if mutation == 'feature' else 'p06_outline_gradient')
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        configure(process, ir)
        before, _ = collect(process, [750])
        with pytest.raises(NativeRendererError):
            configure(process, bad)
        after, _ = collect(process, [750], generation=2)
    assert before[0][..., 3].any() and np.array_equal(before, after)


def scanline_paint_source(project, images):
    track, style = image_source(project, images, 'body')
    return track, replace(style, scanline_enabled=True, scanline_forward_enabled=True,
                          scanline_mode='color', scanline_color='#FF48FF70', scanline_glow_px=0)


def test_image_fill_scanline_keeps_existing_scene_after_missing_capability(binary, project, images):
    ir = ir_for(binary, scanline_paint_source(project, images))
    unsupported = copy.deepcopy(ir)
    unsupported['required_features'].remove('p06_unscaled_paint_scanline')
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        configure(process, ir)
        expected, _ = collect(process, [750])
        with pytest.raises(NativeRendererError, match='正文渐变'):
            configure(process, unsupported)
        replay, _ = collect(process, [750], generation=2)
    assert np.array_equal(expected, replay)


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
def test_image_fill_scanline_matches_actual_d2d_gpu(binary, project, images):
    from test_subtitle_render_gpu_backend import _render_g1_frames
    from test_subtitle_skia_outline_ruby import render_skia
    from test_subtitle_skia_static_glow import assert_glow_parity

    track, style = scanline_paint_source(project, images)
    times = (450, 750, 1100, 1550)
    reference = []
    for look in (style, replace(style, scanline_enabled=False)):
        with NativeRendererProcess() as process:
            _, raw = _render_g1_frames(process, look, times, force_warp=False, track=track,
                                       width=640, height=360, realization_enabled=False)
        reference.append([np.frombuffer(frame, np.uint8).reshape(360, 640, 4) for frame in raw])
    scans, bare = reference
    highlight = np.array([72, 255, 112])
    assert any(np.count_nonzero(np.all(frame[..., :3] == highlight, axis=-1)) > 10 for frame in scans)
    assert all(not np.any(np.all(frame[..., :3] == highlight, axis=-1)) for frame in bare)
    gpu, raster = render_skia(binary, ir_for(binary, (track, style)), times)
    assert_glow_parity(times, scans, gpu, raster, 'image_fill_scanline')


def fading_paint_source(project, images):
    track, style = image_source(project, images, 'mixed')
    return track, replace(style, entry_anim='fade', exit_anim='fade', entry_lead_ms=400, exit_fade_ms=450)


def test_image_fill_fade_keeps_existing_scene_after_missing_capability(binary, project, images):
    ir = ir_for(binary, fading_paint_source(project, images))
    bad = copy.deepcopy(ir)
    bad['required_features'].remove('p06_unscaled_paint_fade')
    line, = ir['track']['lines']
    start, end = line['display_start_ms'], line['display_end_ms']
    times = [start + 150, end - 150]
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        configure(process, ir)
        before, _ = collect(process, times)
        with pytest.raises(NativeRendererError, match='正文渐变'):
            configure(process, bad)
        replay, _ = collect(process, times[::-1], generation=2)
    assert np.array_equal(before, replay[::-1])
    assert all(frame.any() for frame in before)


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
def test_image_fill_fade_matches_actual_d2d_gpu(binary, project, images):
    from test_subtitle_render_gpu_backend import _render_g1_frames
    from test_subtitle_skia_outline_ruby import render_skia
    from test_subtitle_skia_static_glow import assert_glow_parity

    track, style = fading_paint_source(project, images)
    ir = ir_for(binary, (track, style))
    line, = ir['track']['lines']
    start, end = line['display_start_ms'], line['display_end_ms']
    times = (start + 80, start + 220, start + 400, end - 200, end - 80)
    with NativeRendererProcess() as process:
        _, raw = _render_g1_frames(process, style, times, force_warp=False, track=track,
                                   width=640, height=360, realization_enabled=False)
    refs = [np.frombuffer(frame, np.uint8).reshape(360, 640, 4) for frame in raw]
    gpu, raster = render_skia(binary, ir, times)
    assert_glow_parity(times, refs, gpu, raster, 'image_fill_fade')


def test_old_sidecar_without_image_fills_is_rejected(binary, monkeypatch):
    process = SkiaRendererProcess(binary, selection=selection('raster'))
    try: ready = NativeRendererProcess.start(process)
    finally: process.abort(reason='handshake fixture')
    ready['profiles'] = [p for p in ready['profiles'] if p != 'p06_image_fill']
    sent = []
    monkeypatch.setattr(NativeRendererProcess, 'start', lambda self: ready)
    monkeypatch.setattr(SkiaRendererProcess, 'abort', lambda *a, **k: None)
    monkeypatch.setattr(SkiaRendererProcess, '_send', lambda self, p: sent.append(p))
    with pytest.raises(NativeRendererError, match='图片填充'): SkiaRendererProcess(binary).start()
    assert not sent


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('case,dpr', [('body', 1), ('body', 1.5), ('mixed', 1), ('mixed', 1.5),
                                      ('outlines', 1.5), ('outlines', 1), ('missing', 1.5)])
def test_image_fill_matches_actual_d2d_and_gpu_raster(binary, project, images, case, dpr):
    from test_subtitle_render_gpu_backend import _render_g1_frames

    track, style = image_source(project, images, case)
    with NativeRendererProcess() as process:
        _, raw = _render_g1_frames(process, style, tuple(TIMES), force_warp=False, track=track,
                                   width=640, height=360, dpr=dpr, realization_enabled=False)
    refs = [np.frombuffer(p, np.uint8).reshape(round(360*dpr), round(640*dpr), 4) for p in raw]
    ir = ir_for(binary, (track, style), dpr=dpr)
    results = []
    for target in ('gpu', 'raster'):
        with SkiaRendererProcess(binary, selection=selection(target)) as process:
            configure(process, ir)
            frames, _ = collect(process, TIMES)
        results.append(frames)
    # The reference really draws the decoded texture (not the solid fallback) where an
    # image paint is visible, and stays uniform where the file is missing.
    opaque = refs[TIMES.index(200)][refs[TIMES.index(200)][..., 3] == 255][:, :3]
    distinct = len(np.unique(opaque, axis=0))
    assert distinct > 40 if case in {'body', 'outlines'} else True, (case, distinct)
    if case == 'missing':
        sung = refs[TIMES.index(2200)]
        assert len(np.unique(sung[sung[..., 3] == 255][:, :3], axis=0)) > 40
    assert_text_parity(refs, results, ('image', case, dpr))
