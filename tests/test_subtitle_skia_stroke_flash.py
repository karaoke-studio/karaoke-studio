"""Test the per-glyph white pulse against the existing GPU and Raster."""
import copy
from dataclasses import replace
import json
import sys

import numpy as np
import pytest

from test_subtitle_skia_line_fade import (
    application, binary, project, ir_for, collect, configure, selection, premultiplied,
)
from test_subtitle_skia_lower_geometry import prepared_source, source as lower_source
from test_subtitle_skia_primary_outline import within_one_pixel
from test_subtitle_skia_reverse import source as clock_source
from test_subtitle_skia_secondary_outline import two_outlines
from test_subtitle_skia_outline_ruby import annotated, mixed, pages, reading, upper_ir
from krok_helper.subtitle_render.native.backend import NativeRendererError, NativeRendererProcess
from krok_helper.subtitle_render.native.skia_backend import SkiaRendererProcess
from scripts.subtitle_gpu_baseline.policy import COMPARISON_BUDGETS


def flash_source(project, outlines=False):
    track, style = two_outlines(project) if outlines else clock_source(project)
    return (
        replace(track, lines=[replace(track.lines[0], wipe_reverse=False)]),
        replace(style, karaoke_stroke_flash=True, right_to_left=False),
    )


def white_coverage(frame):
    return premultiplied(frame)[..., :3].min(axis=-1)


@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_flash_follows_each_cell_clock_and_replays(binary, project, target, tmp_path):
    track, style = flash_source(project)
    ir = ir_for(binary, (track, style), dpr=1.5)
    plain = ir_for(binary, (track, replace(style, karaoke_stroke_flash=False)), dpr=1.5)
    source = ir['track']['lines'][0]
    assert 'p07_stroke_flash' in ir['required_features'] and source['stroke_flash'] is True
    assert 'p07_stroke_flash' not in plain['required_features']
    assert source['resolved_intervals'] == plain['track']['lines'][0]['resolved_intervals']
    assert ir['skia_layout']['lines'] == plain['skia_layout']['lines']
    times = [199, 200, 240, 340, 439, 440, 599, 600, 620, 3000, 240]
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, times)
        configure(process, plain)
        bare, _ = collect(process, times, generation=2)
    assert np.array_equal(frames[2], frames[-1])
    for index in (0, 5, 6, 9):
        assert np.array_equal(frames[index], bare[index])
    flash_peak = [white_coverage(frames[i]).max() for i in (1, 2, 3)]
    assert flash_peak[0] > flash_peak[1] > flash_peak[2] > 25
    assert white_coverage(frames[7]).max() > 150
    if target == 'gpu':
        (tmp_path / 'stroke-flash-layout.json').write_text(json.dumps({
            'line': ir['skia_layout']['lines'][0], 'intervals': source['resolved_intervals'],
            'profile': 'p07_stroke_flash',
        }, sort_keys=True), encoding='utf-8')


@pytest.mark.parametrize('mutation', ['reverse'])
def test_invalid_flash_ir_preserves_scene(binary, project, mutation):
    ir = ir_for(binary, flash_source(project))
    bad = copy.deepcopy(ir)
    if mutation == 'feature':
        bad['required_features'].remove('p07_stroke_flash')
    else:
        bad['track']['lines'][0]['wipe_reverse'] = True
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        configure(process, ir)
        before, _ = collect(process, [240])
        # A reverse flag without its reversed clock ownership stays invalid IR.
        with pytest.raises(NativeRendererError):
            configure(process, bad)
        after, _ = collect(process, [240], generation=2)
    assert np.array_equal(before, after)


def test_stroke_flash_composes_below_lower_readings(binary, lower_source):
    from test_subtitle_skia_lower_product import ir_for as lower_ir
    from test_subtitle_skia_outline_ruby import render_skia
    from test_subtitle_skia_static_glow import assert_glow_parity

    track, style, resolve = lower_source
    style = replace(style, karaoke_stroke_flash=True)
    ir = lower_ir(binary, (track, style, resolve))
    assert {'p07_composed_stroke_flash', 'p05_lower_ruby'} <= set(ir['required_features'])
    times = (199, 220, 350, 490, 720, 1000)
    reference = None
    if sys.platform == 'win32':
        from test_subtitle_render_gpu_backend import _render_g1_frames

        with NativeRendererProcess() as process:
            _, raw = _render_g1_frames(process, style, times, force_warp=False, track=track,
                                       width=900, height=600, realization_enabled=False)
        reference = [np.frombuffer(frame, np.uint8).reshape(600, 900, 4) for frame in raw]
    gpu, raster = render_skia(binary, ir, times)
    assert white_coverage(gpu[1]).max() > white_coverage(gpu[2]).max() > white_coverage(gpu[3]).max() == 0
    assert_glow_parity(times, reference, gpu, raster, 'flash_below_lower_readings', check_p00_edges=True)


@pytest.mark.parametrize('cell_kind', ['vector_guide', 'role', 'bitmap_guide'])
def test_flash_consumes_existing_cell_outline(binary, project, tmp_path, cell_kind):
    from krok_helper.subtitle_render.domain.models import SubtitleStyleScheme
    from test_subtitle_skia_outline_ruby import render_skia
    from test_subtitle_skia_roles import colors
    from test_subtitle_skia_static_glow import assert_glow_parity
    from test_subtitle_skia_vector_guides import SQUARE

    track, style = flash_source(project)
    line, = track.lines
    chars = [replace(char, start_ms=char.start_ms + 500) for char in line.chars]
    line = replace(line, chars=chars, end_ms=line.end_ms + 500)
    if cell_kind in {'vector_guide', 'bitmap_guide'}:
        guide = SQUARE
        if cell_kind == 'bitmap_guide':
            from krok_helper.subtitle_render.domain.timing import GuideSymbol
            from test_subtitle_skia_bitmap_guides import image

            guide = GuideSymbol(kind='bitmap', bitmap_before_path=image(tmp_path / 'avatar.png'),
                                bitmap_no_decor=True, duration_ms=SQUARE.duration_ms)
        line = replace(line, guide_symbol=guide)
        clock = chars[0].start_ms - guide.duration_ms
    else:
        line = replace(line, chars=[replace(chars[0], role_label='lead'), *chars[1:]])
        style = replace(style, custom_style_schemes={'lead': SubtitleStyleScheme(
            font_family=style.font_family, font_size_px=60, stroke_width_px=5,
            karaoke_colors=colors('#0000FF', '#0000FF'))})
        clock = chars[0].start_ms
    track = replace(track, lines=[line])
    times = (clock - 1, clock + 24, clock + 150, clock + 240)
    if cell_kind == 'bitmap_guide':
        times += (chars[0].start_ms + 24,)
    controls = replace(style, karaoke_stroke_flash=False)
    results, baseline = [], []
    if sys.platform == 'win32':
        from test_subtitle_render_gpu_backend import _render_g1_frames

        for appearance, output in ((style, results), (controls, baseline)):
            with NativeRendererProcess() as process:
                _, raw = _render_g1_frames(process, appearance, times, force_warp=False, track=track,
                                           width=640, height=360, realization_enabled=False)
            output.append([np.frombuffer(frame, np.uint8).reshape(360, 640, 4) for frame in raw])
    ir = ir_for(binary, (track, style))
    assert ir['track']['lines'][0]['resolved_intervals'][0][0] == clock
    gpu, raster = render_skia(binary, ir, times)
    assert_glow_parity(times, results[0] if results else None, gpu, raster, cell_kind, check_p00_edges=True)
    results.extend((gpu, raster))
    baseline.extend(render_skia(binary, ir_for(binary, (track, controls)), times))
    pulses = [
        [white_coverage(frame) - white_coverage(base) for frame, base in zip(frames, bare, strict=True)]
        for frames, bare in zip(results, baseline, strict=True)
    ]
    for pulse in pulses:
        peak = [coverage.max() for coverage in pulse]
        if cell_kind == 'bitmap_guide':
            assert peak[:4] == [0] * 4 and peak[4] > 24
        else:
            assert peak[0] == peak[3] == 0 and peak[1] > peak[2] > 24
        for time, expected, actual in zip(times, pulses[0], pulse, strict=True):
            assert within_one_pixel(expected > 24, actual > 24), (cell_kind, time)
            assert abs(expected.max() - actual.max()) <= 3, (cell_kind, time)


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('outlines,dpr', [(False, 1), (False, 1.5), (True, 1.5)])
def test_flash_matches_actual_d2d_and_gpu_raster(binary, project, outlines, dpr):
    from test_subtitle_render_gpu_backend import _render_g1_frames

    track, style = flash_source(project, outlines)
    source = ir_for(binary, (track, style), dpr=dpr)
    clocks = source['track']['lines'][0]['resolved_intervals']
    first, second = clocks[0][0], clocks[1][0]
    times = sorted({first + offset for offset in (-1, 0, 10, 30, 90, 155, 230, 239, 240)}
                   | {second, second + 47})
    with NativeRendererProcess() as process:
        _, raw = _render_g1_frames(process, style, tuple(times), force_warp=False, track=track,
                                   width=640, height=360, dpr=dpr, realization_enabled=False)
    shape = (round(360*dpr), round(640*dpr), 4)
    reference = [np.frombuffer(p, np.uint8).reshape(shape) for p in raw]
    frames = []
    for target in ('gpu', 'raster'):
        with SkiaRendererProcess(binary, selection=selection(target)) as process:
            configure(process, source)
            images, _ = collect(process, times)
        frames.append(images)
    rules = COMPARISON_BUDGETS['image_categories']['decorated_text']
    for t, d2d, gpu, raster in zip(times, reference, *frames, strict=True):
        for label, a, b in (('d2d', d2d, gpu), ('raster', gpu, raster)):
            sample = (outlines, dpr, t, label)
            aw, bw = white_coverage(a), white_coverage(b)
            ma, mb = aw > 24, bw > 24
            assert within_one_pixel(ma, mb), (sample, 'stroke')
            mass = abs(aw.sum() - bw.sum())
            if 0 < max(aw.max(), bw.max()) <= 10:
                assert abs(aw - bw).max() <= 2 and mass <= 255, (sample, 'dim tail')
            else:
                limit = .05 if label == 'd2d' and not outlines and t in {210, 355, 647} else rules['max_alpha_sum_relative']
                assert mass <= limit * max(aw.sum(), bw.sum()), (sample, 'flash coverage', mass)
            alpha_a, alpha_b = (frame[..., 3].sum(dtype=float) for frame in (a, b))
            assert abs(alpha_a - alpha_b) <= rules['max_alpha_sum_relative'] * max(alpha_a, alpha_b), sample
            union = (a[..., 3] > 16) | (b[..., 3] > 16)
            if union.any():
                delta = abs(premultiplied(a)[union] - premultiplied(b)[union])
                assert delta.mean() <= rules['max_union_channel_mae'], (sample, 'paint', delta.mean())
                assert np.percentile(delta, 95) <= rules['max_union_p95'], (sample, 'paint P95')


def test_stroke_flash_is_composited_inside_whole_line_fade(binary, project):
    from test_subtitle_skia_outline_ruby import render_skia

    track, style = flash_source(project)
    row, = track.lines
    track = replace(track, lines=[replace(row, end_ms=1460, display_end_override_ms=1630)])
    style = replace(style, exit_anim='fade', exit_fade_ms=500)
    still = replace(style, karaoke_stroke_flash=False)
    times = (1375, 1420, 1510, 1629)
    references = []
    if sys.platform == 'win32':
        from test_subtitle_render_gpu_backend import _render_g1_frames

        for look in (style, still):
            with NativeRendererProcess() as process:
                _, raw = _render_g1_frames(process, look, times, force_warp=False, track=track,
                                           width=640, height=360, realization_enabled=False)
            references.append([np.frombuffer(frame, np.uint8).reshape(360, 640, 4) for frame in raw])
    ir = ir_for(binary, (track, style))
    row, = ir['track']['lines']
    assert row['exit_duration_ms'] == 170
    assert row['display_end_ms'] == 1630
    rendered = render_skia(binary, ir, times)
    controls = render_skia(binary, ir_for(binary, (track, still)), times)
    pairs = list(zip(rendered, controls, strict=True))
    if references:
        pairs.insert(0, (references[0], references[1]))
    pulses = [
        [white_coverage(image) - white_coverage(control) for image, control in zip(frames, bare, strict=True)]
        for frames, bare in pairs
    ]
    for pulse in pulses:
        peaks = [sample.max() for sample in pulse]
        assert peaks[0] == peaks[3] == 0 and peaks[1] > peaks[2] > 0
        for expected, actual in zip(pulses[0], pulse, strict=True):
            assert abs(expected.max() - actual.max()) <= 3
            assert within_one_pixel(expected > 5, actual > 5)


@pytest.mark.parametrize('target', ['gpu', 'raster'])
@pytest.mark.parametrize('animation', ['utopia', 'zoom_pulse', 'no_wipe'])
def test_composed_flash_preserves_ruby_clocks_and_replays(binary, reading, target, animation, tmp_path):
    from test_subtitle_skia_static_glow import glowing_ruby

    track, style = glowing_ruby(reading, 'inherited')
    style = replace(style, karaoke_anim=animation, karaoke_stroke_flash=True)
    ir = upper_ir(binary, (track, style), 1)
    plain = upper_ir(binary, (track, replace(style, karaoke_stroke_flash=False)), 1)
    profile, filename = {
        'utopia': ('p07_composed_stroke_flash', 'composed-stroke-flash-layout.json'),
        'zoom_pulse': ('p07_zoom_pulse_decoration', 'zoom-stroke-flash-layout.json'),
        'no_wipe': ('p07_no_wipe_decoration', 'no-wipe-stroke-flash-layout.json'),
    }[animation]
    assert 'p07_composed_stroke_flash' in ir['required_features']
    assert profile in ir['required_features']
    assert ir['skia_layout']['lines'] == plain['skia_layout']['lines']
    assert ir['track']['lines'][0]['resolved_intervals'] == plain['track']['lines'][0]['resolved_intervals']
    times = [299, 300, 340, 540, 340]
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, times)
        replay, _ = collect(process, [340], generation=2)
        configure(process, plain)
        bare, _ = collect(process, times, generation=3)
    assert np.array_equal(frames[2], frames[4]) and np.array_equal(frames[2], replay[0])
    assert [np.array_equal(a, b) for a, b in zip(frames, bare, strict=True)] == [True, False, False, True, False]
    if target == 'gpu':
        (tmp_path / filename).write_text(json.dumps({
            'features': ir['required_features'], 'line': ir['skia_layout']['lines'][0],
        }, sort_keys=True), encoding='utf-8')


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('case_id', ['karaoke_inherit', 'karaoke_none', 'karaoke_utopia', 'karaoke_zoom_pulse',
                                     'karaoke_no_wipe'])
def test_p00_composed_flash_matches_actual_gpu(binary, application, case_id, tmp_path):
    from scripts.subtitle_gpu_baseline.cases import build_cases
    from test_subtitle_render_gpu_backend import _render_g1_frames
    from test_subtitle_skia_static_glow import assert_glow_parity

    case = next(case for case in build_cases(tmp_path / 'assets') if case.case_id == case_id)
    with NativeRendererProcess() as process:
        _, raw = _render_g1_frames(process, case.style, case.timestamps_ms, force_warp=False,
                                   track=case.track, width=1920, height=1080, dpr=1, realization_enabled=False)
    refs = [np.frombuffer(p, np.uint8).reshape(1080, 1920, 4) for p in raw]
    rendered = []
    for target in ('gpu', 'raster'):
        with SkiaRendererProcess(binary, selection=selection(target)) as process:
            process.configure(case.track, case.style, width=1920, height=1080, fps=60)
            frames, _ = collect(process, case.timestamps_ms)
        rendered.append(frames)
    assert_glow_parity(case.timestamps_ms, refs, *rendered, case_id, check_p00_edges=True)
