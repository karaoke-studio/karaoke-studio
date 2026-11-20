"""Timed line movement and opacity against Direct2D."""
from dataclasses import replace
import copy
import json
import sys

import numpy as np
import pytest

from test_subtitle_skia_line_fade import (
    application, binary, project, fade_project, ir_for, collect, configure, selection,
    premultiplied,
)
from test_subtitle_skia_outline_ruby import (
    annotated, annotated_paint, lower_ir, mixed, outlined, pages, prepared_source, reading, source, upper_ir, upper_source,
    assert_annotated_parity,
)
from krok_helper.subtitle_render.native.backend import NativeRendererError, NativeRendererProcess
from krok_helper.subtitle_render.native.skia_backend import SkiaRendererProcess


def motion_project(project, entry='slide_in', exit='slide_out', *, two_lanes=False, font_size=50):
    track, style = fade_project(project)
    style = replace(style, entry_anim=entry, exit_anim=exit, font_size_px=font_size)
    if two_lanes:
        first = track.lines[0]
        second = replace(first, chars=[replace(char, start_ms=char.start_ms+800) for char in first.chars],
                         end_ms=first.end_ms+800)
        track = replace(track, lines=[first, second])
        style = replace(style, dual_line_layout=True, line_alignments=['center', 'center'], line_gap_px=12)
    return track, style


def bounds(frame):
    alpha = frame[..., 3]
    y, x = np.nonzero(alpha > alpha.max() // 2)
    return np.array([x.min(), y.min(), x.max(), y.max()]) if x.size else np.array([])


def static_band_image(tmp_path):
    from test_subtitle_skia_image_fills import write_png

    pixels = np.full((12, 32, 4), [49, 117, 240, 255], dtype=np.uint8)
    pixels[:, :8] = [250, 92, 42, 255]
    pixels[:3, 16:] = [115, 48, 234, 210]
    pixels[8:, 12:] = [23, 246, 97, 190]
    return write_png(tmp_path / 'line-band.png', pixels)


BAND_FITS = ['stretch', 'cover', 'repeat', 'slice_stretch', 'slice_cover', 'slice_repeat']


def line_band_fit_settings(fit):
    if not fit.startswith('slice_'):
        return {'line_band_fill_mode': fit}
    return dict(line_band_fill_mode='three_slice', line_band_mid_mode=fit.removeprefix('slice_'),
                line_band_cap_left_px=5, line_band_cap_right_px=9)


@pytest.mark.parametrize('target', ['gpu', 'raster'])
@pytest.mark.parametrize('fit', BAND_FITS)
def test_static_line_bands_stay_below_active_rows_and_follow_line_motion(binary, project, tmp_path, target, fit):
    track, style = motion_project(project, two_lanes=True)
    band = replace(style, line_band_enabled=True, line_band_image=str(static_band_image(tmp_path)),
                   line_band_opacity_pct=65, line_band_pad_top_px=25, line_band_pad_bottom_px=30,
                   **line_band_fit_settings(fit))
    valid = ir_for(binary, (track, band), dpr=1.5)
    plain = ir_for(binary, (track, style), dpr=1.5)
    missing = ir_for(binary, (track, replace(band, line_band_image=str(tmp_path / 'missing.png'))), dpr=1.5)
    feature = 'p08_static_line_band'
    assert feature in valid['required_features']
    sliced = fit.startswith('slice_')
    assert ('p08_line_band_fit' in valid['required_features']) == (fit in {'cover', 'repeat'})
    assert ('p08_line_band_slices' in valid['required_features']) == sliced
    assert valid['skia_layout']['line_band_schema'] == (3 if sliced else 1 if fit == 'stretch' else 2)
    assert feature not in plain['required_features'] and feature not in missing['required_features']
    windows = [(line['display_start_ms'], line['display_end_ms']) for line in valid['track']['lines']]
    first, last = min(s for s, _ in windows), max(e for _, e in windows)
    overlap = (max(s for s, _ in windows) + min(e for _, e in windows)) // 2
    assert all(s < overlap < e for s, e in windows)
    times = [first - 1, first + 150, overlap, last - 150, last + 1]
    invalid = copy.deepcopy(valid)
    invalid['skia_layout']['lines'][0]['line_band_rect'][0] = float('nan')
    bad_schema = copy.deepcopy(valid)
    bad_schema['skia_layout']['line_band_schema'] = 2 if fit == 'stretch' else 1
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, valid)
        frames, _ = collect(process, times)
        with pytest.raises(NativeRendererError):
            configure(process, invalid)
        with pytest.raises(NativeRendererError, match='行背景带布局版本无效'):
            configure(process, bad_schema)
        replay, _ = collect(process, list(reversed(times)), generation=2)
        configure(process, plain)
        text, _ = collect(process, times, generation=3)
        configure(process, missing)
        fallback, _ = collect(process, times, generation=4)
    assert not frames[0].any() and not frames[-1].any()
    assert all(np.array_equal(a, b) for a, b in zip(text, fallback, strict=True))
    assert all(np.array_equal(a, b) for a, b in zip(frames, reversed(replay), strict=True))
    for frame, baseline in zip(frames[1:-1], text[1:-1], strict=True):
        assert np.count_nonzero((frame[..., 3] > 0) & (baseline[..., 3] == 0)) > 200
    opaque = text[2][..., 3] == 255
    assert opaque.any() and np.array_equal(frames[2][opaque], text[2][opaque])


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('entry,leave', [('none', 'none'), ('slide_in', 'slide_out')])
@pytest.mark.parametrize('fit', BAND_FITS)
def test_static_line_band_matches_actual_gpu(binary, application, tmp_path, entry, leave, fit):
    from scripts.subtitle_gpu_baseline.cases import build_cases
    from test_subtitle_skia_character_fade import assert_ruby_transition_gpu_parity

    base = next(case for case in build_cases(tmp_path) if case.case_id == 'band')
    case = replace(base, case_id=f'static_line_band_{fit}_{entry}', track=replace(base.track, rubies=[]),
                   style=replace(base.style, entry_anim=entry, exit_anim=leave,
                                 line_band_image=str(static_band_image(tmp_path)), line_band_include_ruby=False,
                                 **line_band_fit_settings(fit)))
    assert_ruby_transition_gpu_parity(binary, case, (299, 450, 650, 1000, 1500, 2600, 4950, 5300, 5450, 5500))


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
def test_static_line_band_caps_share_the_width_when_the_band_is_tall(binary, application, tmp_path):
    from scripts.subtitle_gpu_baseline.cases import build_cases
    from test_subtitle_skia_character_fade import assert_ruby_transition_gpu_parity

    base = next(case for case in build_cases(tmp_path) if case.case_id == 'band')
    style = replace(base.style, line_band_image=str(static_band_image(tmp_path)), line_band_include_ruby=False,
                    line_band_fill_mode='three_slice', line_band_cap_left_px=16, line_band_cap_right_px=16,
                    line_band_pad_top_px=220, line_band_pad_bottom_px=200)
    case = replace(base, case_id='static_line_band_crowded_caps', track=replace(base.track, rubies=[]), style=style)
    assert_ruby_transition_gpu_parity(binary, case, case.timestamps_ms)


@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_static_line_band_can_include_the_upper_reading_box(binary, reading, target, tmp_path):
    from test_subtitle_skia_static_glow import glowing_ruby

    track, style = glowing_ruby(reading, 'inherited')
    style = replace(style, line_band_enabled=True, line_band_fill_mode='stretch',
                    line_band_image=str(static_band_image(tmp_path)), line_band_pad_top_px=2)
    body = upper_ir(binary, (track, replace(style, line_band_include_ruby=False)), 1)
    annotated = upper_ir(binary, (track, replace(style, line_band_include_ruby=True)), 1)
    feature = 'p08_ruby_line_band'
    assert feature in body['required_features'] and feature in annotated['required_features']
    source = annotated['skia_layout']['lines'][0]
    rect = source['line_band_rect']
    body_rect = body['skia_layout']['lines'][0]['line_band_rect']
    assert rect[0] == body_rect[0] and rect[2:] == body_rect[2:]
    assert rect[1] < body_rect[1] and rect[1] < min(u['baseline_y'] for u in source['ruby_groups'][0]['units'])
    bad = copy.deepcopy(annotated)
    bad['required_features'].remove(feature)
    t = annotated['track']['lines'][0]['display_start_ms']+170
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, annotated)
        before, _ = collect(process, [t])
        with pytest.raises(NativeRendererError):
            configure(process, bad)
        after, _ = collect(process, [t], generation=2)
    assert before[0].any() and np.array_equal(before[0], after[0])


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('include_ruby', [False, True])
@pytest.mark.parametrize('semantics', ['legacy', 'n3_1074'])
def test_static_line_band_with_ruby_matches_actual_gpu(binary, application, tmp_path, include_ruby, semantics):
    from scripts.subtitle_gpu_baseline.cases import build_cases
    from test_subtitle_skia_character_fade import assert_ruby_transition_gpu_parity

    base = next(case for case in build_cases(tmp_path) if case.case_id == 'band')
    style = replace(base.style, line_band_image=str(static_band_image(tmp_path)), line_band_include_ruby=include_ruby,
                    line_band_cap_left_px=5, line_band_cap_right_px=9, layout_semantics=semantics,
                    entry_anim='slide_in', exit_anim='slide_out')
    case = replace(base, case_id=f'static_line_band_ruby_{semantics}_{include_ruby}', style=style)
    assert_ruby_transition_gpu_parity(binary, case, (299, 450, 650, 1000, 1500, 2600, 4950, 5300, 5450, 5500))


@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_static_staff_line_band_resolves_colored_layers_and_note_toggle(binary, project, target):
    track, style = motion_project(project)
    style = replace(style, line_band_enabled=True, line_band_image='preset:staff_scroll',
                    line_band_dynamic_notes_enabled=False, line_band_cap_left_px=190, line_band_cap_right_px=160,
                    line_band_line_color='#FD6262', line_band_clef_color='#50F078')
    modes = [ir_for(binary, (track, replace(style, line_band_static_notes_enabled=enabled))) for enabled in (True, False)]
    for ir, asset in zip(modes, ('staff_scroll_static.png', 'staff_scroll_no_notes.png'), strict=True):
        assert 'p08_staff_line_band' in ir['required_features']
        assert ir['style']['line_band_image'].endswith(asset)
        assert ir['style']['line_band_lines_image'].endswith('staff_scroll_lines.png')
        assert ir['style']['line_band_clef_image'].endswith('staff_scroll_clef.png')
    bad = copy.deepcopy(modes[0])
    bad['required_features'].remove('p08_staff_line_band')
    t = modes[0]['track']['lines'][0]['display_start_ms'] + 550
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, modes[0])
        notes, _ = collect(process, [t])
        with pytest.raises(NativeRendererError):
            configure(process, bad)
        replay, _ = collect(process, [t], generation=2)
        configure(process, modes[1])
        without, _ = collect(process, [t], generation=3)
    assert notes[0].any() and np.array_equal(notes[0], replay[0])
    assert np.count_nonzero(notes[0] != without[0]) > 100


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('notes', [False, True])
def test_static_staff_line_band_matches_actual_gpu(binary, application, tmp_path, notes):
    from scripts.subtitle_gpu_baseline.cases import build_cases
    from test_subtitle_skia_character_fade import assert_ruby_transition_gpu_parity

    base = next(case for case in build_cases(tmp_path) if case.case_id == 'band')
    style = replace(base.style, line_band_dynamic_notes_enabled=False, line_band_static_notes_enabled=notes,
                    line_band_cap_left_px=190, line_band_cap_right_px=160,
                    line_band_line_color='#F86262', line_band_clef_color='#50E878')
    case = replace(base, case_id=f'static_staff_line_band_{notes}', style=style)
    assert_ruby_transition_gpu_parity(binary, case, (*case.timestamps_ms, 5499, 5500))


@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_animated_staff_line_band_loops_on_scene_time_and_replays(binary, project, target):
    track, style = motion_project(project, entry='none', exit='none')
    track = replace(track, lines=[replace(track.lines[0], display_start_override_ms=0, display_end_override_ms=6000)])
    style = replace(style, line_band_enabled=True, line_band_image='preset:staff_scroll',
                    line_band_cap_left_px=190, line_band_cap_right_px=160)
    ir = ir_for(binary, (track, style))
    feature = 'p08_animated_staff_line_band'
    assert feature in ir['required_features']
    assert ir['style']['line_band_image'].endswith('staff_scroll.webp')
    bad = copy.deepcopy(ir)
    bad['required_features'].remove(feature)
    # The shipped note animation has sixty 20ms frames. Sample two adjacent
    # frames in different cycles, after the lyric has finished.
    times = [2420, 2400, 3620, 3600, 2420]
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, times)
        with pytest.raises(NativeRendererError):
            configure(process, bad)
        replay, _ = collect(process, [2420], generation=2)
    assert all(np.array_equal(frames[0], other) for other in (frames[2], frames[4], replay[0]))
    assert np.array_equal(frames[1], frames[3])
    assert not np.array_equal(frames[0], frames[1])


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('static_notes', [True, False])
def test_animated_staff_line_band_matches_actual_gpu(binary, application, tmp_path, static_notes):
    from scripts.subtitle_gpu_baseline.cases import build_cases
    from test_subtitle_skia_character_fade import assert_ruby_transition_gpu_parity

    case = next(case for case in build_cases(tmp_path) if case.case_id == 'band')
    if not static_notes:
        case = replace(case, case_id='band_dynamic_only', style=replace(case.style, line_band_static_notes_enabled=False))
    times = sorted(set(case.timestamps_ms) | {1199, 1200, 1220, 2399, 2400, 2420, 4800, 5499})
    assert_ruby_transition_gpu_parity(binary, case, times)


@pytest.mark.parametrize('target', ['gpu', 'raster'])
@pytest.mark.parametrize('axis', ['slide', 'rise'])
def test_motion_relative_to_static_line(binary, project, target, axis, tmp_path):
    entry, exit = ('slide_in', 'slide_out') if axis == 'slide' else ('rise', 'rise')
    track, style = motion_project(project, entry, exit, font_size=20)
    ir = ir_for(binary, (track, style), dpr=1.5)
    static = ir_for(binary, (track, replace(style, entry_anim='none', exit_anim='none')), dpr=1.5)
    assert 'p07_line_motion' in ir['required_features'] and ir['skia_layout']['line_motion_schema'] == 1
    line = ir['track']['lines'][0]
    assert line['resolved_intervals'] == static['track']['lines'][0]['resolved_intervals']
    assert ir['skia_layout']['lines'] == static['skia_layout']['lines']
    start, end = line['display_start_ms'], line['display_end_ms']
    times = [start + line['entry_duration_ms']//2, end - line['exit_duration_ms']//2]
    if axis == 'slide':
        expected = ([-9, 0, -9, 0], [-27, 0, -27, 0])
    else:
        expected = ([0, 4.5, 0, 4.5], [0, -13.5, 0, -13.5])
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, times)
        configure(process, static)
        original, _ = collect(process, times, generation=2)
    for frame, baseline, offset in zip(frames, original, expected, strict=True):
        assert abs(bounds(frame) - bounds(baseline) - offset).max() <= 1
    if target == 'gpu' and axis == 'slide':
        (tmp_path / 'line-motion-layout.json').write_text(json.dumps({
            'schema': ir['skia_layout']['line_motion_schema'],
            'line': ir['skia_layout']['lines'][0],
            'times': times,
            'window': [start, end],
            'durations': [line['entry_duration_ms'], line['exit_duration_ms']],
        }, sort_keys=True), encoding='utf-8')


@pytest.mark.parametrize('mutation', ['feature', 'schema', 'wrong_phase'])
def test_invalid_line_motion_keeps_scene(binary, project, mutation):
    ir = ir_for(binary, motion_project(project))
    bad = copy.deepcopy(ir)
    if mutation == 'feature':
        bad['required_features'].remove('p07_line_motion')
    elif mutation == 'schema':
        bad['skia_layout']['line_motion_schema'] = '1'
    else:
        bad['track']['lines'][0]['exit_anim'] = 'slide_in'
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        configure(process, ir)
        before, _ = collect(process, [100])
        with pytest.raises(NativeRendererError):
            configure(process, bad)
        after, _ = collect(process, [100], generation=2)
    assert np.array_equal(before[0], after[0])


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('entry,exit,dpr,two_lanes', [
    ('slide_in', 'slide_out', 1, False),
    ('rise', 'rise', 1.5, False),
    ('fade', 'slide_out', 1.5, False),
    ('slide_in', 'slide_out', 1.5, True),
])
def test_line_motion_matches_actual_gpu(binary, project, entry, exit, dpr, two_lanes):
    from test_subtitle_render_gpu_backend import _render_g1_frames

    track, style = motion_project(project, entry, exit, two_lanes=two_lanes)
    ir = ir_for(binary, (track, style), dpr=dpr)
    line = ir['track']['lines'][0]
    regions = [slice(None)]
    if two_lanes:
        assert {item['lane'] for item in ir['track']['lines']} == {0, 1}
        top, bottom = (item['cells'] for item in ir['skia_layout']['lines'])
        first_bottom = max(c['baseline_y'] + c['metrics']['ink'][3] for c in top)
        second_top = min(c['baseline_y'] + c['metrics']['ink'][1] for c in bottom)
        split = round((first_bottom + second_top) * dpr / 2)
        regions = [slice(0, split), slice(split, None)]
    start, end = line['display_start_ms'], line['display_end_ms']
    times = sorted({start, start+17, start+50, start+100, start+200, start+400,
                    end-600, end-300, end-100, end-1, end})
    with NativeRendererProcess() as process:
        _, raw = _render_g1_frames(process, style, tuple(times), force_warp=False, track=track,
                                   width=640, height=360, dpr=dpr, realization_enabled=False)
    reference = [np.frombuffer(p, np.uint8).reshape(round(360*dpr), round(640*dpr), 4) for p in raw]
    for target in ('gpu', 'raster'):
        with SkiaRendererProcess(binary, selection=selection(target)) as process:
            configure(process, ir)
            frames, _ = collect(process, times)
        for t, expected, actual in zip(times, reference, frames, strict=True):
            for region in regions:
                ref, frame = expected[region], actual[region]
                assert abs(int(ref[..., 3].max()) - int(frame[..., 3].max())) <= 2, (target, t, region)
                if ref[..., 3].max() >= 16:
                    a, b = bounds(ref), bounds(frame)
                    assert a.size == b.size and abs(a-b).max() <= 1, (target, t, region, a, b)
            delta = abs(premultiplied(expected) - premultiplied(actual))
            assert delta.mean() < .16, (target, t, delta.mean())
            assert np.mean(delta.max(axis=-1) > 16) < .003, (target, t)


RUBY_ANIMATIONS = [('fade', 'fade'), ('slide_in', 'slide_out'), ('rise', 'rise')]


def animated_ruby(track, style, entry, exit_):
    return track, replace(annotated_paint(style, 2, True), entry_anim=entry, exit_anim=exit_,
                          entry_lead_ms=400, exit_fade_ms=600)


@pytest.mark.parametrize('target', ['gpu', 'raster'])
@pytest.mark.parametrize('entry,exit_', RUBY_ANIMATIONS)
@pytest.mark.parametrize('utopia', [False, True])
def test_line_animation_keeps_ruby_layout_and_replays(binary, reading, target, entry, exit_, utopia, tmp_path):
    track, style = upper_source(reading, 2)
    if utopia:
        style = replace(style, karaoke_anim='utopia')
    ir = upper_ir(binary, animated_ruby(track, style, entry, exit_), 1.5)
    static = upper_ir(binary, animated_ruby(track, style, 'none', 'none'), 1.5)
    assert 'p07_ruby_line_animation' in ir['required_features']
    assert ('p07_utopia_line_animation' in ir['required_features']) == utopia
    assert ir['skia_layout']['lines'] == static['skia_layout']['lines']
    bad = copy.deepcopy(ir)
    bad['required_features'].remove('p07_utopia_line_animation' if utopia else 'p07_ruby_line_animation')
    line = ir['track']['lines'][0]
    first = line['display_start_ms'] + 100
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, [first, 900, line['display_end_ms'] - 300, first])
        with pytest.raises(NativeRendererError):
            configure(process, bad)
        after, _ = collect(process, [first], generation=2)
        configure(process, static)
        normal, _ = collect(process, [first, 900], generation=3)
    assert np.array_equal(frames[0], frames[-1]) and np.array_equal(frames[0], after[0])
    assert np.array_equal(frames[1], normal[1])
    assert not np.array_equal(frames[0], normal[0])
    if target == 'gpu' and entry == 'fade':
        name = 'ruby-utopia-line-animation-layout.json' if utopia else 'ruby-line-animation-layout.json'
        (tmp_path / name).write_text(json.dumps({
            'features': ir['required_features'], 'line': ir['skia_layout']['lines'][0],
            'durations': [line['entry_duration_ms'], line['exit_duration_ms']],
        }, sort_keys=True), encoding='utf-8')


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('entry,exit_', RUBY_ANIMATIONS)
@pytest.mark.parametrize('dpr', [1, 1.5])
@pytest.mark.parametrize('utopia', [False, True])
def test_animated_outlined_ruby_matches_actual_gpu(binary, reading, entry, exit_, dpr, utopia):
    track, style = animated_ruby(*upper_source(reading, 2), entry, exit_)
    if utopia:
        style = replace(style, karaoke_anim='utopia')
    ir = upper_ir(binary, (track, style), dpr)
    compare_animated_ruby(binary, track, style, ir, 640, 360, dpr)


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('case_id', [
    'entry_fade', 'entry_slide_in', 'entry_rise', 'exit_fade', 'exit_slide_out', 'exit_rise',
    'entry_char_fade', 'viewport', 'entry_noise', 'exit_noise',
])
def test_p00_glowing_ruby_line_motion_matches_actual_gpu(binary, application, case_id, tmp_path):
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


@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_viewport_translation_moves_body_and_ruby_without_retiming(binary, reading, target, tmp_path):
    track, style = upper_source(reading, 2)
    plain = upper_ir(binary, (track, style), 1.5)
    moved = upper_ir(binary, (track, replace(style, viewport_offset_x=12, viewport_offset_y=-8)), 1.5)
    assert 'p08_viewport' in moved['required_features']
    assert moved['skia_layout']['lines'] == plain['skia_layout']['lines']
    assert moved['track']['lines'] == plain['track']['lines']
    invalid = copy.deepcopy(moved)
    invalid['required_features'].remove('p08_viewport')
    times = [299, 550, 1400, 2300]
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, plain)
        original, _ = collect(process, times)
        configure(process, moved)
        translated, _ = collect(process, [*times, 550], generation=2)
        with pytest.raises(NativeRendererError):
            configure(process, invalid)
        replay, _ = collect(process, [550], generation=3)
    expected = []
    for source in original:
        shifted = np.zeros_like(source)
        shifted[:-12, 18:] = source[12:, :-18]
        expected.append(shifted)
    assert_annotated_parity(times, expected, translated[:len(times)], ('viewport', target), 2)
    assert np.array_equal(translated[1], translated[-1])
    assert np.array_equal(translated[1], replay[0])
    if target == 'gpu':
        payload = {key: value for key, value in moved['style'].items() if key.startswith('viewport_')}
        (tmp_path / 'viewport-layout.json').write_text(json.dumps(payload, sort_keys=True), encoding='utf-8')


def test_glowing_ruby_fade_preserves_first_nonzero_alpha_on_both_targets(binary, reading):
    from test_subtitle_skia_outline_ruby import render_skia
    from test_subtitle_skia_static_glow import glowing_ruby

    track, style = glowing_ruby(reading, 'inherited')
    style = replace(style, entry_anim='fade', entry_lead_ms=600, karaoke_anim='utopia')
    ir = upper_ir(binary, (track, style), 1)
    line = ir['track']['lines'][0]
    start, duration = line['display_start_ms'], line['entry_duration_ms']
    opacity = 1 - (1 - 1 / duration) ** 2
    expected_peak = round(opacity * 255)
    gpu, raster = render_skia(binary, ir, [start, start + 1, start + 200, start + 1])
    for frames in (gpu, raster):
        assert not frames[0].any()
        assert frames[1][..., 3].max() == expected_peak > 0
        assert frames[2][..., 3].max() > expected_peak
        assert np.array_equal(frames[1], frames[-1])


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
def test_line_motion_with_lower_romanization_matches_actual_gpu(binary, source):
    track, style, resolve = source
    track, style = animated_ruby(track, outlined(style, 2), 'rise', 'rise')
    ir = lower_ir(binary, (track, style, resolve), dpr=1.5)
    compare_animated_ruby(binary, track, style, ir, 900, 600, 1.5)


def compare_animated_ruby(binary, track, style, ir, width, height, dpr):
    from test_subtitle_render_gpu_backend import _render_g1_frames
    from scripts.subtitle_gpu_baseline.policy import COMPARISON_BUDGETS

    line = ir['track']['lines'][0]
    start, end = line['display_start_ms'], line['display_end_ms']
    times = sorted({start, start+17, start+100, start+200, start+320, 900,
                    end-601, end-300, end-100, end-17, end})
    with NativeRendererProcess() as process:
        _, raw = _render_g1_frames(process, style, tuple(times), force_warp=False, track=track,
                                   width=width, height=height, dpr=dpr, realization_enabled=False)
    shape = (round(height*dpr), round(width*dpr), 4)
    refs = [np.frombuffer(p, np.uint8).reshape(shape) for p in raw]
    results = []
    for target in ('gpu', 'raster'):
        with SkiaRendererProcess(binary, selection=selection(target)) as process:
            configure(process, ir)
            frames, _ = collect(process, times)
        results.append(frames)
    budget = COMPARISON_BUDGETS['image_categories']['text']
    for t, d2d, vulkan, raster in zip(times, refs, *results, strict=True):
        for oracle, compared in ((d2d, vulkan), (vulkan, raster)):
            assert abs(int(oracle[..., 3].max()) - int(compared[..., 3].max())) <= 2, t
            if oracle[..., 3].max() < 16:
                continue
            a, b = bounds(oracle), bounds(compared)
            assert a.size == b.size and abs(a-b).max() <= 1, (t, a, b)
            mass_a, mass_b = (int(frame[..., 3].sum()) for frame in (oracle, compared))
            assert abs(mass_a - mass_b) <= max(mass_a, mass_b) * .02, (t, mass_a, mass_b)
            mask = (oracle[..., 3] > 16) | (compared[..., 3] > 16)
            diff = abs(premultiplied(oracle)[mask] - premultiplied(compared)[mask])
            if diff.size:
                assert diff.mean() <= budget['max_union_channel_mae'], (t, diff.mean())
                assert np.percentile(diff, 95) <= budget['max_union_p95'], t


@pytest.mark.parametrize('target', ['gpu', 'raster'])
@pytest.mark.parametrize('shape', ['circle', 'square', 'rounded', 'star', 'note8', 'note16', 'notepair'])
def test_custom_shape_signal_countdown(binary, project, target, shape):
    track, style = project
    text = replace(style, entry_anim='none', exit_anim='none')
    lit = replace(text, lit_enabled=True, lit_appearance_mode='custom', lit_style=shape,
                  lit_offset_y=-15, lit_size=22, lit_number=4, lit_transition_mode='slide', lit_shadow=True)
    plain = ir_for(binary, (track, text))
    ir = ir_for(binary, (track, lit))
    line = ir['track']['lines'][0]
    start = line['display_start_ms']
    end = line['chars'][0]['start_ms']
    before = max(start, end-300)
    times = [before, end+1, before]
    assert line['lit_head'] and 'p08_custom_shape_signal' in ir['required_features']
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        lamps, _ = collect(process, times)
        broken = copy.deepcopy(ir)
        broken['skia_layout']['lines'][0]['shape_signal_anchor'] = [None, 1]
        with pytest.raises(NativeRendererError, match='最终锚点'):
            configure(process, broken)
        replay, _ = collect(process, [before])
        configure(process, plain)
        baseline, _ = collect(process, times, generation=2)
    assert np.count_nonzero(lamps[0][..., 3] > baseline[0][..., 3]) > 0
    assert np.array_equal(lamps[1], baseline[1])
    assert np.array_equal(lamps[0], lamps[-1]) and np.array_equal(lamps[0], replay[0])


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('case_id,appearance', [
    ('lit_appearance_custom', None), ('lamp_shape_image', 'custom'),
    ('lamp', None), ('lamp_shape_circle', None), ('lamp_shape_square', None),
    ('lamp_shape_rounded', None), ('lamp_shape_image', None),
    ('lit_appearance_auto', None), ('lit_appearance_role', None),
    ('lamp_shape_star', None), ('lamp_shape_note8', None),
    ('lamp_shape_note16', None), ('lamp_shape_notepair', None),
])
def test_p00_shape_signal_matches_actual_gpu(binary, application, tmp_path, case_id, appearance):
    from scripts.subtitle_gpu_baseline.cases import build_cases
    from test_subtitle_render_gpu_backend import _render_g1_frames
    from test_subtitle_skia_static_glow import assert_glow_parity

    case = next(case for case in build_cases(tmp_path) if case.case_id == case_id)
    style = case.style if appearance is None else replace(case.style, lit_appearance_mode=appearance)
    with NativeRendererProcess() as process:
        _, raw = _render_g1_frames(process, style, case.timestamps_ms, force_warp=False,
                                   track=case.track, width=1920, height=1080, dpr=1, realization_enabled=False)
    reference = [np.frombuffer(p, np.uint8).reshape(1080, 1920, 4) for p in raw]
    rendered = []
    for target in ('gpu', 'raster'):
        with SkiaRendererProcess(binary, selection=selection(target)) as process:
            process.configure(case.track, style, width=1920, height=1080, fps=60)
            frames, _ = collect(process, case.timestamps_ms)
            rendered.append(frames)
    assert_glow_parity(case.timestamps_ms, reference, *rendered, case.case_id, check_p00_edges=True)


@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_custom_image_signal_contains_asset_and_falls_back(binary, project, tmp_path, target):
    from PyQt6.QtGui import QImage

    track, style = project
    asset = tmp_path / 'lamp.png'
    bitmap = QImage(12, 6, QImage.Format.Format_ARGB32)
    bitmap.fill(0xFFFF00FF)
    assert bitmap.save(str(asset))
    lamp = replace(style, lit_enabled=True, lit_appearance_mode='custom', lit_style='image',
                   lit_image_path=str(asset), lit_offset_y=-15, lit_size=28, lit_number=4,
                   lit_shadow=True, lit_edge_brightness_pct=100, lit_stroke_soften=4)
    valid = ir_for(binary, (track, lamp))
    fallback = ir_for(binary, (track, replace(lamp, lit_style='circle', lit_shadow=False,
                                             lit_edge_brightness_pct=0, lit_stroke_soften=0)))
    assert 'p08_custom_image_signal' in valid['required_features']
    start = valid['track']['lines'][0]['display_start_ms']
    times = [start, start+1, start]
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, valid)
        images, _ = collect(process, times)
        asset.unlink()
        held, _ = collect(process, [start])
        configure(process, valid)
        missing, _ = collect(process, times, generation=2)
        configure(process, fallback)
        circles, _ = collect(process, times, generation=3)
    assert np.array_equal(images[0], images[-1]) and np.array_equal(held[0], images[0])
    colored = (images[0][..., 0] > 240) & (images[0][..., 1] < 5) & (images[0][..., 2] > 240)
    ys, xs = np.where(colored)
    assert abs(int(ys.max()-ys.min()+1) - 14) <= 1
    assert np.unique(xs).size > 14
    assert all(np.array_equal(a, b) for a, b in zip(missing, circles, strict=True))


@pytest.mark.parametrize('target', ['gpu', 'raster'])
@pytest.mark.parametrize('role', [None, '__global__', 'signal-palette'])
def test_decorated_shape_signal_uses_after_palette_before_lyrics_start(binary, project, target, role):
    from krok_helper.subtitle_render.domain.models import SubtitleStyleScheme
    from test_subtitle_skia_roles import colors

    track, style = project
    named = role == 'signal-palette'
    style = replace(style, karaoke_colors=colors('#FF0000', '#00FF00'),
                    custom_style_schemes={'signal-palette': SubtitleStyleScheme(
                        karaoke_colors=colors('#FFFFFF', '#0000FF'))},
                    lit_enabled=True, lit_appearance_mode='role' if role else 'auto', lit_role_name=role or '',
                    lit_style='square', lit_offset_y=-15, lit_size=28, lit_number=4,
                    lit_edge_brightness_pct=0, lit_opacity_pct=60, lit_transition_mode='none',
                    decoration_kind='none', stroke_width_px=0, stroke2_enabled=False)
    ir = ir_for(binary, (track, style))
    start = ir['track']['lines'][0]['display_start_ms']
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, [start, 201, start])
    assert np.array_equal(frames[0], frames[2])
    lamp = (frames[0][..., 2 if named else 1] > 250) & (frames[0][..., 0] < 5)
    assert np.count_nonzero(lamp) > 500
    assert np.all(frames[0][..., 3][lamp] <= 153)
    assert not np.any(frames[1][..., 3][lamp])


@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_volume_signal_seek_and_reconfigure(binary, project, target):
    track, style = project
    volume = replace(style, volume_enabled=True, volume_appearance_mode='custom',
                     volume_fill_color='#FF00FF', volume_overlay_fill_color='#00FF00',
                     volume_stroke_width=0, volume_flash_times=0, volume_column_count=4,
                     volume_duration_ms=1200, volume_time_offset_ms=800)
    ir = ir_for(binary, (track, volume))
    line = ir['track']['lines'][0]
    assert line['volume_head'] and 'p08_volume_signal' in ir['required_features']
    assert 'volume_signal_anchor' in ir['skia_layout']['lines'][0]
    times = [max(line['display_start_ms'], 0), 600, 950, max(line['display_start_ms'], 0)]
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, times)
        invalid = copy.deepcopy(ir)
        invalid['skia_layout']['lines'][0]['volume_signal_anchor'] = [0, None]
        with pytest.raises(NativeRendererError, match='最终锚点'):
            configure(process, invalid)
        replay, _ = collect(process, times[:1])
    assert np.array_equal(frames[0], frames[-1]) and np.array_equal(frames[0], replay[0])
    covered = [np.count_nonzero((f[..., 0] < 5) & (f[..., 1] > 250) & (f[..., 2] < 5))
               for f in frames[:3]]
    assert covered[0] < covered[1] < covered[2]


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('case_id', [
    'volume', 'lamp_shape_volume', 'volume_appearance_custom', 'volume_appearance_auto', 'volume_appearance_role',
])
def test_p00_volume_signal_matches_actual_gpu(binary, application, tmp_path, case_id):
    from scripts.subtitle_gpu_baseline.cases import build_cases
    from test_subtitle_render_gpu_backend import _render_g1_frames
    from test_subtitle_skia_static_glow import assert_glow_parity

    case = next(case for case in build_cases(tmp_path) if case.case_id == case_id)
    with NativeRendererProcess() as process:
        _, raw = _render_g1_frames(process, case.style, case.timestamps_ms, force_warp=False, track=case.track,
                                   width=1920, height=1080, dpr=1, realization_enabled=False)
    reference = [np.frombuffer(p, np.uint8).reshape(1080, 1920, 4) for p in raw]
    rendered = []
    for target in ('gpu', 'raster'):
        with SkiaRendererProcess(binary, selection=selection(target)) as process:
            process.configure(case.track, case.style, width=1920, height=1080, fps=60)
            frames, _ = collect(process, case.timestamps_ms)
            rendered.append(frames)
    assert_glow_parity(case.timestamps_ms, reference, *rendered, case_id, check_p00_edges=True)
