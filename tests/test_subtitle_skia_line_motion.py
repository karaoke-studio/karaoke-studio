"""Timed line movement and opacity against Direct2D."""
from base64 import b64decode
from dataclasses import replace
from pathlib import Path
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
from krok_helper.subtitle_render.domain.models import LyricsLayout
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


def test_saved_line_band_edge_fields_keep_current_layers(binary, project, tmp_path):
    from test_subtitle_skia_outline_ruby import render_skia
    from test_subtitle_skia_static_glow import assert_glow_parity

    track, style = motion_project(project, two_lanes=True)
    current = replace(style, line_band_enabled=True, line_band_image=str(static_band_image(tmp_path)),
                      line_band_stroke_enabled=True, line_band_stroke_color='#FFBA44',
                      line_band_stroke_size_px=3, line_band_pad_top_px=16)
    saved = replace(current, line_band_edge_kind='shadow', line_band_edge_color='#2200EE',
                    line_band_edge_size_px=12, line_band_edge_offset_x_px=25)
    times = (450, 750, 1400, 2250)
    gpu, raster = render_skia(binary, ir_for(binary, (track, saved), dpr=1.5), times)
    expected = render_skia(binary, ir_for(binary, (track, current), dpr=1.5), times)
    assert any(frame.any() for frame in gpu)
    assert np.array_equal(gpu, expected[0]) and np.array_equal(raster, expected[1])
    reference = None
    if sys.platform == 'win32':
        from test_subtitle_render_gpu_backend import _render_g1_frames

        with NativeRendererProcess() as process:
            _, raw = _render_g1_frames(process, saved, times, force_warp=False, track=track,
                                       width=640, height=360, dpr=1.5, realization_enabled=False)
        reference = [np.frombuffer(frame, np.uint8).reshape(540, 960, 4) for frame in raw]
    assert_glow_parity(times, reference, gpu, raster, 'saved_line_band_edges', check_p00_edges=True)


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
@pytest.mark.parametrize('preset', ['stars', 'bubbles', 'diagonal', 'plain_round'])
def test_other_line_band_presets_animate_or_preserve_tinted_alpha(binary, project, target, preset):
    from krok_helper.subtitle_render.domain.band_presets import band_preset

    track, style = motion_project(project, entry='none', exit='none')
    track = replace(track, lines=[replace(track.lines[0], display_start_override_ms=0, display_end_override_ms=6000)])
    spec = band_preset(preset)
    style = replace(style, line_band_enabled=True, line_band_image=f'preset:{preset}',
                    line_band_fill_mode=spec.default_fill_mode, line_band_cap_left_px=spec.cap_left_px,
                    line_band_cap_right_px=spec.cap_right_px, line_band_tint=spec.default_tint or '',
                    line_band_tint_strength_pct=50)
    ir = ir_for(binary, (track, style))
    is_animated = preset in {'stars', 'bubbles'}
    feature = 'p08_animated_line_band' if is_animated else 'p08_tinted_line_band'
    assert feature in ir['required_features'] and 'p08_staff_line_band' not in ir['required_features']
    bad = copy.deepcopy(ir)
    bad['required_features'].remove(feature)
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, [2400, 2500, 3600])
        if not is_animated:
            configure(process, ir_for(binary, (track, replace(style, line_band_tint=''))))
            plain, _ = collect(process, [2400], generation=2)
            assert np.array_equal(frames[0][..., 3], plain[0][..., 3])
            assert np.count_nonzero(frames[0] != plain[0]) > 100
    assert frames[0].any() and np.array_equal(frames[0], frames[2])
    assert np.array_equal(frames[0], frames[1]) != is_animated


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('preset', ['stars', 'bubbles', 'diagonal', 'plain_round'])
def test_other_line_band_presets_match_actual_gpu(binary, application, tmp_path, preset):
    from krok_helper.subtitle_render.domain.band_presets import band_preset
    from scripts.subtitle_gpu_baseline.cases import build_cases
    from test_subtitle_skia_character_fade import assert_ruby_transition_gpu_parity

    base = next(case for case in build_cases(tmp_path) if case.case_id == 'band')
    spec = band_preset(preset)
    style = replace(base.style, line_band_image=f'preset:{preset}', line_band_fill_mode=spec.default_fill_mode,
                    line_band_cap_left_px=spec.cap_left_px, line_band_cap_right_px=spec.cap_right_px,
                    line_band_tint=spec.default_tint or '', line_band_tint_strength_pct=50)
    case = replace(base, case_id=f'line_band_{preset}', style=style)
    assert_ruby_transition_gpu_parity(binary, case, (*case.timestamps_ms, 1199, 1200, 2420, 5499))


@pytest.mark.parametrize('target', ['gpu', 'raster'])
@pytest.mark.parametrize('format', ['gif', 'webp'])
def test_custom_line_band_sequence_preserves_transparency_and_seek(binary, project, tmp_path, target, format):
    path = tmp_path / f'band.{format}'
    if format == 'gif':
        path.write_bytes((Path(__file__).parent / 'data' / 'guide_anim_3frames.gif').read_bytes())
    else:
        # A still 4x5 lossless WebP with non-opaque pixels.
        path.write_bytes(b64decode('UklGRh4AAABXRUJQVlA4TBEAAAAvAwABEAdQt8qUu6CBiOh/AAA='))
    track, style = motion_project(project, entry='none', exit='none')
    track = replace(track, lines=[replace(track.lines[0], display_start_override_ms=0, display_end_override_ms=4000)])
    style = replace(style, line_band_enabled=True, line_band_image=str(path), line_band_fill_mode='cover',
                    line_band_opacity_pct=90)
    ir = ir_for(binary, (track, style))
    feature = 'p08_line_band_image_sequence'
    assert feature in ir['required_features']
    bad = copy.deepcopy(ir)
    bad['style']['line_band_image'] = str(tmp_path / f'absent.{format}')
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, [2400, 2500, 2700])
        with pytest.raises(NativeRendererError):
            configure(process, bad)
        replay, _ = collect(process, [2400], generation=2)
    assert frames[0].any() and np.array_equal(frames[0], frames[2]) and np.array_equal(frames[0], replay[0])
    assert np.array_equal(frames[0], frames[1]) == (format == 'webp')
    if format == 'webp':
        left, top, _, _ = ir['skia_layout']['lines'][0]['line_band_rect']
        assert int(frames[0][int(top+3), int(left+3), 3]) == 144


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
def test_custom_line_band_gif_matches_actual_gpu(binary, application, tmp_path):
    from scripts.subtitle_gpu_baseline.cases import build_cases
    from test_subtitle_skia_character_fade import assert_ruby_transition_gpu_parity

    base = next(case for case in build_cases(tmp_path) if case.case_id == 'band')
    gif = Path(__file__).parent / 'data' / 'guide_anim_3frames.gif'
    style = replace(base.style, line_band_image=str(gif), line_band_fill_mode='repeat',
                    entry_anim='slide_in', exit_anim='slide_out')
    case = replace(base, case_id='custom_gif_line_band', style=style)
    assert_ruby_transition_gpu_parity(binary, case, (299, 350, 399, 400, 500, 600, 1499, 1500, 2600, 5499))


def test_multiframe_band_does_not_reject_an_inactive_tint(binary, project, tmp_path):
    from test_subtitle_skia_outline_ruby import render_skia
    from test_subtitle_skia_static_glow import assert_glow_parity

    track, style = motion_project(project, entry='none', exit='none')
    track = replace(track, lines=[replace(track.lines[0], display_start_override_ms=0, display_end_override_ms=4000)])
    asset = Path(__file__).parent / 'data' / 'guide_anim_3frames.gif'
    style = replace(solid_line_band(style, tmp_path), line_band_image=str(asset))
    tinted = replace(style, line_band_tint='#00F531', line_band_tint_strength_pct=100)
    times = (2400, 2500, 2700)
    reference = None
    if sys.platform == 'win32':
        from test_subtitle_render_gpu_backend import _render_g1_frames

        samples = []
        for look in (style, tinted):
            with NativeRendererProcess() as process:
                _, raw = _render_g1_frames(process, look, times, force_warp=False, track=track,
                                           width=640, height=360, realization_enabled=False)
            samples.append([np.frombuffer(frame, np.uint8).reshape(360, 640, 4) for frame in raw])
        assert np.array_equal(*samples)
        reference = samples[0]
    gpu, raster = render_skia(binary, ir_for(binary, (track, tinted)), times)
    plain_gpu, plain_raster = render_skia(binary, ir_for(binary, (track, style)), times)
    assert not np.array_equal(gpu[0], gpu[1]) and np.array_equal(gpu[0], gpu[2])
    assert np.array_equal(gpu, plain_gpu) and np.array_equal(raster, plain_raster)
    assert_glow_parity(times, reference, gpu, raster, 'multiframe_band_tint', check_p00_edges=True)


@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_line_band_with_scanline_keeps_effect_geometry_and_seek(binary, reading, tmp_path, target):
    from test_subtitle_skia_scanline import ruby_band_source

    track, style = ruby_band_source(reading, 10, utopia=True)
    baseline = upper_ir(binary, (track, style), 1)
    style = replace(style, line_band_enabled=True, line_band_image=str(static_band_image(tmp_path)),
                    line_band_fill_mode='stretch')
    ir = upper_ir(binary, (track, style), 1)
    feature = 'p08_line_band_with_effects'
    assert feature in ir['required_features']
    line, = ir['skia_layout']['lines']
    unbanded, = baseline['skia_layout']['lines']
    assert {k: v for k, v in line.items() if k != 'line_band_rect'} == unbanded
    bad = copy.deepcopy(ir)
    bad['required_features'].remove(feature)
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, [550, 1250, 550])
        with pytest.raises(NativeRendererError):
            configure(process, bad)
        replay, _ = collect(process, [550], generation=2)
        configure(process, baseline)
        plain, _ = collect(process, [550], generation=3)
    assert np.array_equal(frames[0], frames[2]) and np.array_equal(frames[0], replay[0])
    assert np.count_nonzero((frames[0][..., 3] > 0) & (plain[0][..., 3] == 0)) > 100


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('shape', ['image', 'band'])
def test_line_band_with_scanline_and_singing_particles_matches_actual_gpu(binary, application, tmp_path, shape):
    from scripts.subtitle_gpu_baseline.cases import build_cases
    from test_subtitle_skia_character_fade import assert_ruby_transition_gpu_parity

    original = next(case for case in build_cases(tmp_path) if case.case_id == 'combined_effects')
    style = replace(original.style, entry_anim='none', exit_anim='none', scanline_shape=shape,
                    scanline_effect='star' if shape == 'band' else 'none')
    case = replace(original, case_id=f'line_band_with_{shape}_scanline', style=style)
    assert_ruby_transition_gpu_parity(binary, case, (299, 400, 1199, 1200, 1280, 1500, 2600, 3700, 4700, 5499, 5500))


@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_line_band_character_transition_uses_its_own_smoothstep(binary, project, tmp_path, target):
    track, style = motion_project(project, entry='char_fade', exit='char_fade')
    style = replace(style, entry_lead_ms=600, exit_fade_ms=600, line_band_enabled=True,
                    line_band_image=str(static_band_image(tmp_path)), line_band_fill_mode='stretch',
                    line_band_opacity_pct=100, line_band_pad_top_px=35)
    ir = ir_for(binary, (track, style))
    feature = 'p08_line_band_transition'
    assert feature in ir['required_features']
    bad = copy.deepcopy(ir)
    bad['required_features'].remove(feature)
    source, = ir['track']['lines']
    start, end = source['display_start_ms'], source['display_end_ms']
    entry, leave = source['entry_duration_ms'] or 600, source['exit_duration_ms'] or 600
    left, top, _, _ = ir['skia_layout']['lines'][0]['line_band_rect']
    x, y = int(left+4), int(top+4)
    times = [start, start+entry//4, start+entry//2, start+entry, end-leave//2, end-leave//4, end]
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, times)
        with pytest.raises(NativeRendererError):
            configure(process, bad)
        replay, _ = collect(process, [times[1]], generation=2)
    alpha = np.array([int(frame[y, x, 3]) for frame in frames])
    intervals = np.array(times)
    rising = np.clip((intervals - start) / entry, 0, 1)
    falling = np.clip((end - intervals) / leave, 0, 1)
    expected = np.rint(255 * rising**2 * (3 - 2 * rising) * falling**2 * (3 - 2 * falling))
    assert np.max(np.abs(alpha - expected)) <= 1
    assert np.array_equal(replay[0], frames[1])


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('effect', ['entry_char_fade', 'entry_flame', 'exit_lightning'])
def test_line_band_character_transition_matches_actual_gpu(binary, application, tmp_path, effect):
    from scripts.subtitle_gpu_baseline.cases import build_cases
    from test_subtitle_skia_character_fade import assert_ruby_transition_gpu_parity

    base = next(case for case in build_cases(tmp_path) if case.case_id == effect)
    style = replace(base.style, line_band_enabled=True, line_band_image='preset:staff_scroll')
    case = replace(base, case_id=f'line_band_{effect}', style=style)
    assert_ruby_transition_gpu_parity(binary, case, case.timestamps_ms)


def test_line_band_uses_its_opacity_curve_during_noise(binary, project, tmp_path):
    from test_subtitle_skia_character_transform import transform_source
    from test_subtitle_skia_outline_ruby import render_skia

    track, style = transform_source(project, 'noise', 'noise')
    style = replace(solid_line_band(style, tmp_path), entry_anim='noise', exit_anim='noise',
                    line_band_pad_left_px=28, line_band_pad_top_px=28)
    times = (100, 300, 650, 2700, 2920)
    reference = None
    if sys.platform == 'win32':
        from test_subtitle_render_gpu_backend import _render_g1_frames

        with NativeRendererProcess() as process:
            _, raw = _render_g1_frames(process, style, times, force_warp=False, track=track,
                                       width=640, height=360, realization_enabled=False)
        reference = [np.frombuffer(frame, np.uint8).reshape(360, 640, 4) for frame in raw]
    ir = ir_for(binary, (track, style))
    assert 'p08_line_band_transition' in ir['required_features']
    left, top, _, _ = ir['skia_layout']['lines'][0]['line_band_rect']
    x, y = round(left + 4), round(top + 4)
    gpu, raster = render_skia(binary, ir, times)
    expected = [int(frame[y, x, 3]) for frame in reference or gpu]
    assert 0 < expected[0] < expected[1] < expected[2] == 255
    assert abs(expected[1] - expected[3]) <= 1 and 0 < expected[4] < expected[3]
    for frames in (gpu, raster):
        alpha = [int(frame[y, x, 3]) for frame in frames]
        assert max(abs(a - b) for a, b in zip(alpha, expected, strict=True)) <= 1


def transparent_band_image(tmp_path):
    from test_subtitle_skia_image_fills import write_png

    y, x = np.ogrid[:32, :96]
    pixels = np.zeros((32, 96, 4), dtype=np.uint8)
    pixels[(x - 47.5)**2 / 40**2 + (y - 15.5)**2 / 10**2 < 1] = [60, 140, 245, 255]
    return write_png(tmp_path / 'transparent-band.png', pixels)


@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_line_band_outer_stroke_expands_transparent_alpha(binary, project, tmp_path, target):
    track, style = motion_project(project, entry='none', exit='none')
    style = replace(style, line_band_enabled=True, line_band_image=str(transparent_band_image(tmp_path)),
                    line_band_fill_mode='stretch', line_band_stroke_size_px=7, line_band_stroke_color='#F4CE00')
    plain = ir_for(binary, (track, style))
    outlined = ir_for(binary, (track, replace(style, line_band_stroke_enabled=True)))
    feature = 'p08_line_band_outer_stroke'
    assert feature not in plain['required_features'] and feature in outlined['required_features']
    t = outlined['track']['lines'][0]['display_start_ms'] + 250
    bad = copy.deepcopy(outlined)
    bad['style']['line_band_stroke_position'] = 'inside'
    oversized = copy.deepcopy(outlined)
    oversized['skia_layout']['lines'][0]['line_band_rect'][2] = 9000
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, plain)
        bare, _ = collect(process, [t])
        configure(process, outlined)
        outline, _ = collect(process, [t], generation=2)
        with pytest.raises(NativeRendererError):
            configure(process, bad)
        with pytest.raises(NativeRendererError, match='行背景带描边尺寸超限'):
            configure(process, oversized)
        replay, _ = collect(process, [t], generation=3)
    extended = (bare[0][..., 3] == 0) & (outline[0][..., 3] > 100)
    assert np.count_nonzero(extended) > 200
    assert np.mean(outline[0][extended, 2]) < 4
    assert np.array_equal(outline[0], replay[0])


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('entry, leave', [('none', 'none'), ('fade', 'slide_out')])
@pytest.mark.parametrize('position', ['outside', 'inside', 'center'])
def test_line_band_stroke_matches_actual_gpu(binary, application, tmp_path, entry, leave, position):
    from scripts.subtitle_gpu_baseline.cases import build_cases
    from test_subtitle_skia_character_fade import assert_ruby_transition_gpu_parity

    base = next(case for case in build_cases(tmp_path) if case.case_id == 'band')
    style = replace(base.style, line_band_image=str(transparent_band_image(tmp_path)), line_band_fill_mode='stretch',
                    line_band_stroke_enabled=True, line_band_stroke_size_px=3, line_band_stroke_color='#4FD5FD',
                    line_band_stroke_position=position, entry_anim=entry, exit_anim=leave)
    case = replace(base, case_id=f'line_band_stroke_{position}_{entry}', style=style)
    assert_ruby_transition_gpu_parity(binary, case, (299, 450, 600, 1500, 2600, 4400, 5200, 5499, 5500))


@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_line_band_stroke_inside_stays_in_image_alpha(binary, project, tmp_path, target):
    track, style = motion_project(project, entry='none', exit='none')
    style = replace(style, line_band_enabled=True, line_band_image=str(transparent_band_image(tmp_path)),
                    line_band_fill_mode='stretch', line_band_opacity_pct=100, line_band_stroke_size_px=3,
                    line_band_stroke_color='#F4CE00', line_band_stroke_enabled=True)
    irs = [ir_for(binary, (track, replace(style, line_band_stroke_position=pos))) for pos in ('inside', 'center')]
    plain = ir_for(binary, (track, replace(style, line_band_stroke_enabled=False)))
    # Use integral edges so both the direct and intermediate-image samples
    # have the same origin when checking the alpha mask.
    for ir in [plain, *irs]:
        line, = ir['skia_layout']['lines']
        line['line_band_rect'] = [round(edge) for edge in line['line_band_rect']]
    assert all('p08_line_band_stroke_position' in ir['required_features'] for ir in irs)
    t = irs[0]['track']['lines'][0]['display_start_ms'] + 250
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, plain)
        bare, _ = collect(process, [t])
        configure(process, irs[0])
        inside, _ = collect(process, [t], generation=2)
        configure(process, irs[1])
        centered, _ = collect(process, [t], generation=3)
    outside = bare[0][..., 3] == 0
    assert not np.any(inside[0][outside, 3]) and np.count_nonzero(centered[0][outside, 3]) > 100
    assert np.count_nonzero(inside[0] != bare[0]) > 100


@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_line_band_blur_uses_independent_shadow_and_glow_layers(binary, project, tmp_path, target):
    track, style = motion_project(project, entry='none', exit='none')
    style = replace(style, line_band_enabled=True, line_band_image=str(transparent_band_image(tmp_path)),
                    line_band_fill_mode='stretch', line_band_shadow_enabled=True, line_band_shadow_size_px=4,
                    line_band_shadow_distance_px=15, line_band_shadow_angle_deg=0,
                    line_band_shadow_color='#0000FF', line_band_glow_enabled=True, line_band_glow_size_px=3,
                    line_band_glow_color='#FF0000')
    ir = ir_for(binary, (track, style))
    feature = 'p08_line_band_blur'
    assert feature in ir['required_features']
    no_glow = ir_for(binary, (track, replace(style, line_band_glow_enabled=False)))
    no_shadow = ir_for(binary, (track, replace(style, line_band_shadow_enabled=False)))
    bad = copy.deepcopy(ir)
    bad['required_features'].remove(feature)
    t = ir['track']['lines'][0]['display_start_ms'] + 250
    oversized = copy.deepcopy(ir)
    oversized['style']['line_band_glow_size_px'] = 512
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        both, _ = collect(process, [t])
        with pytest.raises(NativeRendererError):
            configure(process, bad)
        with pytest.raises(NativeRendererError, match='阴影或发光尺寸超限'):
            configure(process, oversized)
        replay, _ = collect(process, [t], generation=2)
        configure(process, no_glow)
        shadow, _ = collect(process, [t], generation=3)
        configure(process, no_shadow)
        glow, _ = collect(process, [t], generation=4)
    assert np.array_equal(both[0], replay[0])
    composite = premultiplied(both[0])
    assert np.count_nonzero(composite[..., 0] - premultiplied(shadow[0])[..., 0] > 4) > 50
    assert np.count_nonzero(composite[..., 2] - premultiplied(glow[0])[..., 2] > 4) > 50


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('effect', ['shadow', 'glow', 'both'])
def test_line_band_blur_matches_actual_gpu(binary, application, tmp_path, effect):
    from scripts.subtitle_gpu_baseline.cases import build_cases
    from test_subtitle_skia_character_fade import assert_ruby_transition_gpu_parity

    base = next(case for case in build_cases(tmp_path) if case.case_id == 'band')
    style = replace(base.style, line_band_image=str(transparent_band_image(tmp_path)), line_band_fill_mode='stretch',
                    line_band_shadow_enabled=effect in {'shadow', 'both'}, line_band_shadow_size_px=2,
                    line_band_shadow_spread_pct=35, line_band_shadow_distance_px=11, line_band_shadow_angle_deg=40,
                    line_band_shadow_color='#B65225', line_band_glow_enabled=effect in {'glow', 'both'},
                    line_band_glow_size_px=3, line_band_glow_spread_pct=25, line_band_glow_range_pct=65,
                    line_band_glow_color='#1DD8F6', line_band_stroke_enabled=effect == 'both',
                    line_band_stroke_position='center', line_band_stroke_size_px=2,
                    entry_anim='fade', exit_anim='slide_out')
    case = replace(base, case_id=f'line_band_blur_{effect}', style=style)
    assert_ruby_transition_gpu_parity(binary, case, (299, 450, 600, 1500, 2600, 4400, 5200, 5499, 5500))


def animated_alpha_band(tmp_path):
    path = tmp_path / 'alpha-band.gif'
    # Two frames with different opaque regions and durations of 100 and 150ms.
    path.write_bytes(b64decode(
        'R0lGODlhCQAIAIEAAAAAAP+lQAAAAAAAACH/C05FVFNDQVBFMi4wAwEAAAAh+QQJCgAAACwAAAAACQAIAAAIGAAB'
        'CBxIsCDBAAgNIgygMGHBhQYjShQYEAAh+QQJDwAAACwFAAIAAwAEAIEAAABJyf8AAAAAAAAICAADCBxIUGBAADs='))
    return str(path)


@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_multitrack_line_bands_share_material_and_keep_each_window(binary, project, tmp_path, target):
    track, style = motion_project(project, entry='none', exit='none')
    line = replace(track.lines[0], display_start_override_ms=0, display_end_override_ms=5000)
    track = replace(track, lines=[line])
    side = replace(track, lines=[replace(line, layout_index=1)], meta=replace(track.meta, offset_ms=1000))
    style = replace(style, line_y_position='bottom', layouts=[LyricsLayout(layout_id='top', line_y_position='top')],
                    line_band_enabled=True, line_band_image=animated_alpha_band(tmp_path),
                    line_band_fill_mode='stretch', line_band_opacity_pct=60)
    ir = ir_for(binary, (track, style), extra_tracks=[side])
    bad = copy.deepcopy(ir)
    bad['skia_extra_sources'][0]['style']['line_band_opacity_pct'] = 70
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        output, _ = collect(process, [0, 1000, 5000, 6000, 1000])
        with pytest.raises(NativeRendererError, match='必须共享'):
            configure(process, bad)
        replay, _ = collect(process, [1000], generation=2)
    bottom, both, top, hidden, seek = output
    assert bottom.any() and top.any() and not hidden.any()
    assert not np.any((bottom[..., 3] > 0) & (top[..., 3] > 0))
    assert np.array_equal(both, seek) and np.array_equal(both, replay[0])
    assert np.any((both[..., 3] > 0) & (bottom[..., 3] > 0))
    assert np.any((both[..., 3] > 0) & (top[..., 3] > 0))


@pytest.mark.skipif(sys.platform != 'win32', reason='Direct2D GPU comparison')
@pytest.mark.parametrize('overlap', [False, True])
def test_multitrack_line_bands_match_actual_d2d_gpu(binary, application, tmp_path, overlap):
    from scripts.subtitle_gpu_baseline.cases import build_cases
    from test_subtitle_render_gpu_backend import _render_g1_frames
    from test_subtitle_skia_static_glow import assert_glow_parity

    case = next(case for case in build_cases(tmp_path) if case.case_id == 'band')
    track = case.track
    other = replace(track, lines=[replace(track.lines[0], layout_index=0 if overlap else 1)],
                    meta=replace(track.meta, offset_ms=900))
    style = replace(case.style, layouts=[LyricsLayout(layout_id='top', line_y_position='top', line_y_margin_px=140)],
                    decoration_kind='none', line_band_opacity_pct=60, line_band_glow_enabled=True,
                    line_band_glow_size_px=2, line_band_glow_color='#7438DF')
    times = (299, 600, 1199, 1200, 1500, 2600, 4400, 5500, 6399, 6400)
    with NativeRendererProcess() as d2d:
        _, raw = _render_g1_frames(d2d, style, times, track=track, extra_tracks=[other], force_warp=False,
                                   width=1920, height=1080, realization_enabled=False)
    reference = [np.frombuffer(frame, np.uint8).reshape(1080, 1920, 4) for frame in raw]
    output = []
    for target in ('gpu', 'raster'):
        with SkiaRendererProcess(binary, selection=selection(target)) as process:
            process.configure(track, style, extra_tracks=[other], width=1920, height=1080, fps=60)
            frames, _ = collect(process, times)
            output.append(frames)
    assert_glow_parity(times, reference, *output, f'multitrack_line_bands_{overlap}', check_p00_edges=True)


@pytest.mark.parametrize('target', ['gpu', 'raster'])
@pytest.mark.parametrize('wide_blur', [False, True], ids=['stroke', 'wide_blur'])
def test_animated_line_band_decoration_uses_current_frame_alpha(binary, project, tmp_path, target, wide_blur):
    track, style = motion_project(project, entry='none', exit='none')
    track = replace(track, lines=[replace(track.lines[0], display_start_override_ms=0, display_end_override_ms=6000)])
    style = replace(style, line_band_enabled=True, line_band_image=animated_alpha_band(tmp_path),
                    line_band_fill_mode='stretch', line_band_stroke_enabled=True,
                    line_band_stroke_size_px=1, line_band_stroke_color='#AC1A60',
                    line_band_glow_enabled=wide_blur, line_band_glow_size_px=4,
                    line_band_glow_spread_pct=50, line_band_glow_color='#B526E2')
    ir = ir_for(binary, (track, style))
    feature = 'p08_line_band_animated_decoration'
    assert feature in ir['required_features']
    if wide_blur:
        band = ir['skia_layout']['lines'][0]['line_band_rect']
        assert (band[3] - band[1]) * 4 / 8 >= 15.5
    bad = copy.deepcopy(ir)
    bad['required_features'].remove(feature)
    times = [2500, 2600, 2750, 2850, 2500]
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, times)
        with pytest.raises(NativeRendererError):
            configure(process, bad)
        replay, _ = collect(process, [2600], generation=2)
    assert np.count_nonzero(frames[0][..., 3] != frames[1][..., 3]) > 100
    assert all(np.array_equal(frames[0], frame) for frame in (frames[2], frames[-1]))
    assert np.array_equal(frames[1], frames[3]) and np.array_equal(frames[1], replay[0])


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('material', ['gif', 'gif_blur', 'staff_scroll', 'stars', 'bubbles'])
def test_animated_line_band_decoration_matches_actual_gpu(binary, application, tmp_path, material):
    from scripts.subtitle_gpu_baseline.cases import build_cases
    from test_subtitle_skia_character_fade import assert_ruby_transition_gpu_parity

    base = next(case for case in build_cases(tmp_path) if case.case_id == 'band')
    blur = material != 'gif'
    path = animated_alpha_band(tmp_path) if material.startswith('gif') else f'preset:{material}'
    style = replace(base.style, line_band_image=path, line_band_fill_mode='stretch',
                    line_band_shadow_enabled=blur, line_band_shadow_size_px=2,
                    line_band_shadow_distance_px=14, line_band_shadow_angle_deg=15,
                    line_band_shadow_color='#21508F', line_band_glow_enabled=blur,
                    line_band_glow_size_px=2, line_band_glow_color='#C028B8',
                    line_band_glow_range_pct=65, line_band_stroke_enabled=True,
                    line_band_stroke_size_px=1, line_band_stroke_position='inside',
                    entry_anim='fade', exit_anim='slide_out')
    case = replace(base, case_id=f'animated_line_band_decoration_{material}', style=style)
    assert_ruby_transition_gpu_parity(binary, case, (299, 349, 350, 374, 400, 450, 599, 600, 1500, 2600, 5499, 5500))


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


@pytest.mark.parametrize('signal,entry_anim,exit_anim', [
    ('lamp', 'fade', 'fade'), ('lamp', 'slide_in', 'rise'), ('volume', 'slide_in', 'rise'),
    ('decorated_lamp', 'slide_in', 'rise'),
])
def test_signal_follows_its_line_animation(binary, project, signal, entry_anim, exit_anim):
    track, style = project
    line = track.lines[0]
    line = replace(line, chars=[replace(char, start_ms=char.start_ms + 1000) for char in line.chars],
                   end_ms=line.end_ms + 1000, display_start_override_ms=0, display_end_override_ms=3400)
    track = replace(track, lines=[line])
    style = replace(style, entry_anim=entry_anim, entry_lead_ms=600, exit_anim=exit_anim, exit_fade_ms=400)
    if signal != 'volume':
        style = replace(style, lit_enabled=True, lit_appearance_mode='custom', lit_style='square', lit_number=1,
                        lit_size=24, lit_offset_y=-36, lit_fill_color='#FF00FF', lit_stroke_width=0,
                        lit_edge_brightness_pct=0, lit_shadow=False)
    else:
        style = replace(style, volume_enabled=True, volume_appearance_mode='custom', volume_column_count=1,
                        volume_fill_color='#FF00FF', volume_overlay_fill_color='#FF00FF', volume_stroke_width=0,
                        volume_duration_ms=3400, volume_flash_times=0, volume_time_offset_ms=2200)
    if signal == 'decorated_lamp':
        from krok_helper.subtitle_render.domain.models import SubtitleStyleScheme
        from test_subtitle_skia_roles import colors

        palette = SubtitleStyleScheme(karaoke_colors=colors('#FF00FF', '#FF00FF'), stroke_width_px=0,
                                      stroke2_enabled=False, decoration_kind='none')
        style = replace(style, lit_appearance_mode='role', lit_role_name='signal-palette',
                        custom_style_schemes={'signal-palette': palette})
    ir = ir_for(binary, (track, style))
    source, = ir['track']['lines']
    assert source['entry_duration_ms'] == 600 and source['exit_duration_ms'] == 400
    times = [250, 650, 3000, 3200]

    def signal_alpha(frame):
        magenta = (frame[..., 0] > 245) & (frame[..., 1] < 10) & (frame[..., 2] > 245)
        return np.where(magenta, frame[..., 3], 0)

    def signal_bounds(alpha):
        pixels = np.argwhere(alpha > alpha.max() // 2)
        return np.array([pixels.min(axis=0), pixels.max(axis=0)])

    results = []
    if sys.platform == 'win32':
        from test_subtitle_render_gpu_backend import _render_g1_frames

        with NativeRendererProcess() as process:
            _, raw = _render_g1_frames(process, style, tuple(times), force_warp=False, track=track,
                                       width=640, height=360, realization_enabled=False)
        results.append([signal_alpha(np.frombuffer(frame, np.uint8).reshape(360, 640, 4)) for frame in raw])
    for target in ('gpu', 'raster'):
        with SkiaRendererProcess(binary, selection=selection(target)) as process:
            configure(process, ir)
            frames, _ = collect(process, times)
        results.append([signal_alpha(frame) for frame in frames])

    for frames in results:
        entering, full, before_exit, leaving = [int(frame.max()) for frame in frames]
        assert 0 < entering < full == before_exit
        if entry_anim == 'slide_in':
            assert signal_bounds(frames[0])[0, 1] < signal_bounds(frames[1])[0, 1]
        if signal == 'decorated_lamp':
            assert leaving == before_exit
            assert np.array_equal(frames[2], frames[3])
        else:
            assert 0 < leaving < before_exit
        if exit_anim == 'rise' and signal != 'decorated_lamp':
            assert signal_bounds(frames[3])[0, 0] < signal_bounds(frames[2])[0, 0]
    for frames in results[1:]:
        for time, expected, actual in zip(times, results[0], frames, strict=True):
            assert abs(int(expected.max()) - int(actual.max())) <= 2, time
            assert abs(signal_bounds(expected) - signal_bounds(actual)).max() <= 1, time


def solid_line_band(style, tmp_path):
    from PyQt6.QtGui import QImage

    asset = tmp_path / 'band.png'
    image = QImage(4, 4, QImage.Format.Format_ARGB32)
    image.fill(0xFF38DF54)
    assert image.save(str(asset))
    return replace(style, entry_anim='none', exit_anim='none', line_band_enabled=True,
                   line_band_image=str(asset), line_band_fill_mode='stretch', line_band_opacity_pct=100,
                   line_band_pad_left_px=4, line_band_pad_right_px=4,
                   line_band_pad_top_px=4, line_band_pad_bottom_px=4)


def solid_band_box(frame):
    mask = np.all(frame[..., :3] == [56, 223, 84], axis=-1) & (frame[..., 3] > 250)
    points = np.argwhere(mask)
    assert len(points) > 1000
    return np.array([points.min(axis=0), points.max(axis=0)])


def test_line_band_covers_inline_volume_geometry(binary, project, tmp_path):
    track, style = project
    style = replace(solid_line_band(style, tmp_path),
                    volume_enabled=True, volume_appearance_mode='custom', volume_size=84,
                    volume_offset_x=-8, volume_offset_y=-16, volume_stroke_width=2,
                    volume_fill_color='#FF00FF', volume_overlay_fill_color='#FF00FF',
                    volume_stroke_color='#FF00FF', volume_overlay_stroke_color='#FF00FF',
                    volume_flash_times=0, volume_column_count=3, volume_duration_ms=1200, volume_time_offset_ms=800)
    times = (250, 650)
    results = []
    if sys.platform == 'win32':
        from test_subtitle_render_gpu_backend import _render_g1_frames

        with NativeRendererProcess() as process:
            _, raw = _render_g1_frames(process, style, times, force_warp=False, track=track,
                                       width=640, height=360, realization_enabled=False)
        results.append([np.frombuffer(frame, np.uint8).reshape(360, 640, 4) for frame in raw])
    ir = ir_for(binary, (track, style))
    line, = ir['skia_layout']['lines']
    assert 'volume_signal_anchor' in line and 'line_band_rect' in line
    for target in ('gpu', 'raster'):
        with SkiaRendererProcess(binary, selection=selection(target)) as process:
            configure(process, ir)
            frames, _ = collect(process, times)
        results.append(frames)

    def band_box(frame):
        low, high = solid_band_box(frame)
        volume = np.argwhere((frame[..., 0] > 245) & (frame[..., 1] < 10)
                             & (frame[..., 2] > 245) & (frame[..., 3] > 245))
        assert len(volume) > 100
        assert np.all(low < volume.min(axis=0)) and np.all(high > volume.max(axis=0))
        return np.array([low, high])

    reference = [band_box(frame) for frame in results[0]]
    assert np.array_equal(*reference)
    for frames in results[1:]:
        for frame, box in zip(frames, reference, strict=True):
            assert abs(band_box(frame) - box).max() <= 1


@pytest.mark.parametrize('content', ['vector', 'bitmap', 'role'])
def test_line_band_uses_text_ink_with_guides_and_roles(binary, project, tmp_path, content):
    from krok_helper.subtitle_render.domain.models import SubtitleStyleScheme
    from krok_helper.subtitle_render.domain.timing import GuideSymbol
    from test_subtitle_skia_bitmap_guides import image
    from test_subtitle_skia_outline_ruby import render_skia
    from test_subtitle_skia_roles import colors, role_outline_palette
    from test_subtitle_skia_vector_guides import SQUARE

    track, style = project
    line, = track.lines
    if content == 'vector':
        line = replace(line, guide_symbol=SQUARE)
    elif content == 'bitmap':
        avatar = image(tmp_path / 'avatar.png', color='#FF00FF')
        line = replace(line, guide_symbol=GuideSymbol(kind='bitmap', bitmap_before_path=avatar,
                                                     bitmap_after_path=avatar, bitmap_zoom_percent=185))
    else:
        line = replace(line, chars=[replace(line.chars[0], role_label='lead'), *line.chars[1:]])
        style = replace(style, custom_style_schemes={'lead': SubtitleStyleScheme(
            font_family=style.font_family, font_size_px=style.font_size_px, stroke_width_px=8,
            stroke2_enabled=True, stroke2_width_px=4, karaoke_colors=role_outline_palette())})
    track = replace(track, lines=[line])
    style = solid_line_band(style, tmp_path)
    style = replace(style, karaoke_colors=colors('#FFD200', '#FFD200'))
    times = (500,)
    results = []
    if sys.platform == 'win32':
        from test_subtitle_render_gpu_backend import _render_g1_frames

        with NativeRendererProcess() as process:
            _, raw = _render_g1_frames(process, style, times, force_warp=False, track=track,
                                       width=640, height=360, realization_enabled=False)
        results.append(np.frombuffer(raw[0], np.uint8).reshape(360, 640, 4))
    gpu, raster = render_skia(binary, ir_for(binary, (track, style)), times)
    results.extend((gpu[0], raster[0]))
    reference = solid_band_box(results[0])
    for frame in results:
        box = solid_band_box(frame)
        assert abs(box - reference).max() <= 1, (content, box, reference)
        if content == 'bitmap':
            magenta = np.all(frame[..., :3] == [255, 0, 255], axis=-1)
            assert np.count_nonzero(magenta[:box[0, 0]]) > 100


def test_custom_bmp_line_band_uses_existing_image_decoder(binary, project, tmp_path):
    from PyQt6.QtGui import QImage
    from test_subtitle_skia_outline_ruby import render_skia
    from test_subtitle_skia_static_glow import assert_glow_parity

    track, style = project
    style = solid_line_band(style, tmp_path)
    asset = tmp_path / 'band.bmp'
    assert QImage(style.line_band_image).save(str(asset))
    bmp = replace(style, line_band_image=str(asset))
    times = (500,)
    reference = None
    if sys.platform == 'win32':
        from test_subtitle_render_gpu_backend import _render_g1_frames

        with NativeRendererProcess() as process:
            _, raw = _render_g1_frames(process, bmp, times, force_warp=False, track=track,
                                       width=640, height=360, realization_enabled=False)
        reference = [np.frombuffer(raw[0], np.uint8).reshape(360, 640, 4)]
        solid_band_box(reference[0])
    gpu, raster = render_skia(binary, ir_for(binary, (track, bmp)), times)
    png_gpu, png_raster = render_skia(binary, ir_for(binary, (track, style)), times)
    assert np.array_equal(gpu, png_gpu) and np.array_equal(raster, png_raster)
    solid_band_box(gpu[0])
    assert_glow_parity(times, reference, gpu, raster, 'custom_bmp_line_band', check_p00_edges=True)


def test_rtl_line_band_follows_the_resolved_text_box(binary, project, tmp_path):
    from test_subtitle_skia_outline_ruby import render_skia
    from test_subtitle_skia_rtl_flow import rtl_source

    track, style = rtl_source(project)
    style = replace(solid_line_band(style, tmp_path), line_band_pad_left_px=28, line_band_pad_right_px=9)
    times = (300, 750)
    reference = None
    if sys.platform == 'win32':
        from test_subtitle_render_gpu_backend import _render_g1_frames

        with NativeRendererProcess() as process:
            _, raw = _render_g1_frames(process, style, times, force_warp=False, track=track,
                                       width=640, height=360, realization_enabled=False)
        reference = [solid_band_box(np.frombuffer(frame, np.uint8).reshape(360, 640, 4)) for frame in raw]
    ir = ir_for(binary, (track, style))
    assert ir['skia_layout']['lines'][0]['text_flow'] == 'rtl'
    gpu, raster = render_skia(binary, ir, times)
    boxes = [solid_band_box(frame) for frame in gpu]
    assert np.array_equal(*boxes)
    for frames in (gpu, raster):
        for frame, expected in zip(frames, reference or boxes, strict=True):
            assert abs(solid_band_box(frame) - expected).max() <= 1


def test_shape_signal_draws_above_line_band(binary, project, tmp_path):
    track, style = project
    style = replace(style, entry_anim='none', exit_anim='none',
                    line_band_enabled=True, line_band_image=str(static_band_image(tmp_path)),
                    line_band_fill_mode='stretch', line_band_pad_top_px=75, line_band_pad_left_px=16,
                    line_band_opacity_pct=100)
    lamp = replace(style, lit_enabled=True, lit_appearance_mode='custom', lit_style='square',
                   lit_number=1, lit_size=18, lit_offset_x=8, lit_offset_y=-9, lit_fill_color='#FF00FF',
                   lit_stroke_width=0, lit_edge_brightness_pct=0, lit_shadow=False)
    band = ir_for(binary, (track, style))
    time = band['track']['lines'][0]['display_start_ms']

    def lamp_pixels(frame):
        return np.argwhere((frame[..., 0] > 245) & (frame[..., 1] < 10)
                           & (frame[..., 2] > 245) & (frame[..., 3] > 245))

    positions = []
    if sys.platform == 'win32':
        from test_subtitle_render_gpu_backend import _render_g1_frames

        with NativeRendererProcess() as process:
            _, raw = _render_g1_frames(process, lamp, (time,), force_warp=False, track=track,
                                       width=640, height=360, realization_enabled=False)
        positions.append(lamp_pixels(np.frombuffer(raw[0], np.uint8).reshape(360, 640, 4)))
    combined = ir_for(binary, (track, lamp))
    assert combined['skia_layout']['lines'][0]['line_band_rect'] == band['skia_layout']['lines'][0]['line_band_rect']
    for target in ('gpu', 'raster'):
        with SkiaRendererProcess(binary, selection=selection(target)) as process:
            configure(process, band)
            plain, _ = collect(process, [time])
            configure(process, combined)
            frames, _ = collect(process, [time], generation=2)
        points = lamp_pixels(frames[0])
        assert len(points) >= 16 * 16
        y, x = points.T
        assert np.count_nonzero(plain[0][y, x, 3]) > len(points) / 2
        positions.append(points)
    assert all(len(points) >= 16 * 16 for points in positions)
    for points in positions[1:]:
        assert abs(positions[0].min(axis=0) - points.min(axis=0)).max() <= 1
        assert abs(positions[0].max(axis=0) - points.max(axis=0)).max() <= 1


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


@pytest.mark.parametrize('source', ['role_lamp', 'role_volume', 'vector_lamp'])
def test_automatic_signal_uses_first_visible_cell_palette(binary, project, source):
    from krok_helper.subtitle_render.domain.models import SubtitleStyleScheme
    from test_subtitle_skia_outline_ruby import render_skia
    from test_subtitle_skia_roles import colors
    from test_subtitle_skia_static_glow import assert_glow_parity
    from test_subtitle_skia_vector_guides import SQUARE

    track, style = project
    line, = track.lines
    chars = [replace(char, start_ms=char.start_ms + 1000) for char in line.chars]
    chars[0] = replace(chars[0], role_label='lead')
    line = replace(line, chars=chars, end_ms=line.end_ms + 1000, display_start_override_ms=0)
    if source == 'vector_lamp':
        line = replace(line, guide_symbol=SQUARE)
    track = replace(track, lines=[line])
    palette = SubtitleStyleScheme(font_family=style.font_family, font_size_px=style.font_size_px,
                                  karaoke_colors=colors('#FF00FF', '#FF00FF'),
                                  stroke_width_px=0, stroke2_enabled=False, decoration_kind='none')
    style = replace(style, karaoke_colors=colors('#0045DC', '#0045DC'), custom_style_schemes={'lead': palette})
    if source.endswith('lamp'):
        style = replace(style, lit_enabled=True, lit_appearance_mode='auto', lit_style='square',
                        lit_offset_y=-40, lit_transition_mode='none', lit_edge_brightness_pct=0)
    else:
        style = replace(style, volume_enabled=True, volume_appearance_mode='auto', volume_column_count=3,
                        volume_duration_ms=1000, volume_flash_times=0, volume_offset_x=-10)
    times = (400, 800)
    reference = None
    if sys.platform == 'win32':
        from test_subtitle_render_gpu_backend import _render_g1_frames

        with NativeRendererProcess() as process:
            _, raw = _render_g1_frames(process, style, times, force_warp=False, track=track,
                                       width=640, height=360, realization_enabled=False)
        reference = [np.frombuffer(frame, np.uint8).reshape(360, 640, 4) for frame in raw]
    ir = ir_for(binary, (track, style))
    gpu, raster = render_skia(binary, ir, times)
    assert_glow_parity(times, reference, gpu, raster, ('first_visible_cell_palette', source), check_p00_edges=True)
    first = ir['skia_layout']['lines'][0]['cells'][0]
    left, top, _, _ = first['metrics']['ink']
    for frame in gpu:
        expected = [0, 69, 220] if source == 'vector_lamp' else [255, 0, 255]
        colored = np.all(frame[..., :3] == expected, axis=-1) & (frame[..., 3] > 250)
        region = (colored[:round(first['baseline_y'] + top) - 3] if source.endswith('lamp')
                  else colored[:, :round(first['origin_x'] + left) - 3])
        assert np.count_nonzero(region) > 100


@pytest.mark.parametrize('signal,entry', [
    ('lamp', 'char_fade'), ('volume', 'char_fade'), ('lamp', 'spin_flip'), ('volume', 'char_drip'),
    ('lamp', 'tracking_in'), ('volume', 'wave_in'), ('lamp', 'petal'),
])
def test_signal_units_follow_their_own_staggered_entry(binary, project, signal, entry):
    _assert_signal_entry(binary, project, signal, entry)


@pytest.mark.parametrize('signal', ['lamp', 'volume'])
def test_signal_utopia_entry_uses_the_unit_center(binary, project, signal):
    _assert_signal_entry(binary, project, signal, 'utopia')


def test_volume_utopia_exit_uses_the_columns_own_singing_intervals(binary, project):
    from test_subtitle_skia_outline_ruby import render_skia
    from test_subtitle_skia_static_glow import assert_glow_parity

    track, style = project
    line, = track.lines
    track = replace(track, lines=[replace(line, display_start_override_ms=0, display_end_override_ms=2750)])
    style = replace(style, entry_anim='none', exit_anim='utopia', exit_fade_ms=750,
                    volume_enabled=True, volume_appearance_mode='custom', volume_column_count=3,
                    volume_fill_color='#FF00FF', volume_overlay_fill_color='#FF00FF',
                    volume_duration_ms=1200, volume_stroke_width=0, volume_flash_times=0)
    times, dpr = (190, 360, 950, 1500, 2050, 2250), 3
    reference = None
    if sys.platform == 'win32':
        from test_subtitle_render_gpu_backend import _render_g1_frames

        with NativeRendererProcess() as process:
            _, raw = _render_g1_frames(process, style, times, force_warp=False, track=track,
                                       width=640, height=360, dpr=dpr, realization_enabled=False)
        reference = [np.frombuffer(frame, np.uint8).reshape(360 * dpr, 640 * dpr, 4) for frame in raw]
    gpu, raster = render_skia(binary, ir_for(binary, (track, style), dpr=dpr), times)
    assert_glow_parity(times, reference, gpu, raster, 'volume_utopia_exit', check_p00_edges=True)
    for frames in (gpu, raster):
        coverage = [int(frame[..., 3][np.all(frame[..., :3] == [255, 0, 255], axis=-1)].sum()) for frame in frames]
        assert all(first > second > 0 for first, second in zip(coverage, coverage[1:]))


def _assert_signal_entry(binary, project, signal, entry):
    from test_subtitle_skia_outline_ruby import render_skia
    from test_subtitle_skia_static_glow import assert_glow_parity

    track, style = project
    line, = track.lines
    chars = [replace(char, start_ms=char.start_ms + 1000) for char in line.chars]
    track = replace(track, lines=[replace(line, chars=chars, end_ms=line.end_ms + 1000,
                                         display_start_override_ms=0, display_end_override_ms=3400)])
    style = replace(style, entry_anim=entry, entry_lead_ms=600, exit_anim='none')
    if signal == 'lamp':
        style = replace(style, lit_enabled=True, lit_appearance_mode='custom', lit_style='square',
                        lit_size=20, lit_number=3, lit_fill_color='#FF00FF', lit_offset_y=-30,
                        lit_stroke_width=0, lit_edge_brightness_pct=0, lit_shadow=False,
                        signals_duration_ms=5000, lit_time_offset_ms=3000, lit_transition_mode='none')
    else:
        style = replace(style, volume_enabled=True, volume_appearance_mode='custom', volume_column_count=3,
                        volume_fill_color='#FF00FF', volume_overlay_fill_color='#FF00FF',
                        volume_duration_ms=1000, volume_stroke_width=0, volume_flash_times=0)
    times = (85, 260, 480, 750 if entry == 'utopia' else 650)
    dpr = 1 if entry == 'char_fade' else 3
    reference = None
    if sys.platform == 'win32':
        from test_subtitle_render_gpu_backend import _render_g1_frames

        with NativeRendererProcess() as process:
            _, raw = _render_g1_frames(process, style, times, force_warp=False, track=track,
                                       width=640, height=360, dpr=dpr, realization_enabled=False)
        reference = [np.frombuffer(frame, np.uint8).reshape(360 * dpr, 640 * dpr, 4) for frame in raw]
    ir = ir_for(binary, (track, style), dpr=dpr)
    if entry != 'utopia':
        assert ir['track']['lines'][0]['entry_duration_ms'] == 600
    gpu, raster = render_skia(binary, ir, times)
    assert_glow_parity(times, reference, gpu, raster, ('signal_unit_entry', signal), check_p00_edges=True)

    def signal_pixels(frame):
        return (frame[..., 0] > 245) & (frame[..., 1] < 10) & (frame[..., 2] > 245) & (frame[..., 3] > 20)

    for frames in (gpu, raster):
        if entry == 'tracking_in':
            assert np.nonzero(signal_pixels(frames[0]))[1].min() < np.nonzero(signal_pixels(frames[-1]))[1].min()
            continue
        if entry == 'wave_in':
            assert np.nonzero(signal_pixels(frames[0]))[0].max() > np.nonzero(signal_pixels(frames[-1]))[0].max()
            continue
        widths = []
        for frame in frames:
            columns = np.flatnonzero(signal_pixels(frame).any(axis=0))
            widths.append(1 + columns[-1] - columns[0])
        if entry == 'utopia':
            assert 0 < widths[0] < widths[3] / 3 and widths[1] > widths[0] * 3
        else:
            assert 0 < widths[0] < widths[1] < widths[2] <= widths[3]
            assert (widths[2] < widths[3]) == (entry == 'spin_flip')


@pytest.mark.parametrize('signal,exit_', [
    ('lamp', 'scatter_out'), ('volume', 'converge_out'), ('volume', 'dissolve_out'),
])
def test_signal_geometry_departure_matches_device_policy(binary, project, signal, exit_):
    from test_subtitle_skia_character_transform import transform_source
    from test_subtitle_skia_outline_ruby import render_skia
    from test_subtitle_skia_static_glow import assert_glow_parity

    track, style = transform_source(project, 'none', exit_)
    if signal == 'lamp':
        style = replace(style, lit_enabled=True, lit_style='square', lit_size=20, lit_number=3,
                        lit_appearance_mode='custom', lit_fill_color='#FF00FF', lit_offset_y=-30,
                        lit_edge_brightness_pct=0, lit_stroke_width=0, lit_shadow=False,
                        signals_duration_ms=5000, lit_time_offset_ms=7000, lit_transition_mode='none')
    else:
        style = replace(style, volume_enabled=True, volume_appearance_mode='custom', volume_column_count=3,
                        volume_fill_color='#FF00FF', volume_overlay_fill_color='#FF00FF',
                        volume_stroke_width=0, volume_flash_times=0)
    times, dpr = (2390, 2500, 2680, 2850), 3
    reference = None
    if sys.platform == 'win32':
        from test_subtitle_render_gpu_backend import _render_g1_frames

        with NativeRendererProcess() as process:
            _, raw = _render_g1_frames(process, style, times, force_warp=False, track=track,
                                       width=640, height=360, dpr=dpr, realization_enabled=False)
        reference = [np.frombuffer(frame, np.uint8).reshape(360 * dpr, 640 * dpr, 4) for frame in raw]
    gpu, raster = render_skia(binary, ir_for(binary, (track, style), dpr=dpr), times)
    assert_glow_parity(times, reference, gpu, raster, (signal, exit_), check_p00_edges=True)
    for frames in (gpu, raster):
        signal_coverage = [
            int(frame[..., 3][np.all(frame[..., :3] == [255, 0, 255], axis=-1)].sum())
            for frame in frames
        ]
        assert signal_coverage[0] > 100
        if signal == 'lamp':
            assert len(set(signal_coverage)) == 1
        else:
            assert signal_coverage[0] > signal_coverage[1] > signal_coverage[-1]


@pytest.mark.parametrize('signal', ['lamp', 'volume'])
def test_decorated_signal_zoom_pulses_on_its_own_countdown(binary, project, signal):
    from krok_helper.subtitle_render.domain.models import SubtitleStyleScheme
    from test_subtitle_skia_outline_ruby import render_skia
    from test_subtitle_skia_roles import colors
    from test_subtitle_skia_static_glow import assert_glow_parity

    track, style = project
    line, = track.lines
    shift = 1000
    chars = [replace(char, start_ms=char.start_ms + shift) for char in line.chars]
    track = replace(track, lines=[replace(line, chars=chars, end_ms=line.end_ms + shift,
                                         display_start_override_ms=0, display_end_override_ms=3400)])
    palette = SubtitleStyleScheme(font_size_px=50, karaoke_colors=colors('#FF00FF', '#FF00FF'))
    style = replace(style, karaoke_anim='zoom_pulse', zoom_pulse_peak_pct=165,
                    entry_anim='none', exit_anim='none', custom_style_schemes={'signal-palette': palette})
    if signal == 'lamp':
        style = replace(style, lit_enabled=True, lit_style='square', lit_size=34, lit_number=3,
                        lit_appearance_mode='role', lit_role_name='signal-palette',
                        lit_edge_brightness_pct=0, lit_stroke_width=0, lit_shadow=False,
                        signals_duration_ms=1200, lit_time_offset_ms=0, lit_transition_mode='none')
        times = (95, 260, 399, 620, 950)
    else:
        style = replace(style, volume_enabled=True, volume_appearance_mode='role',
                        volume_role_name='signal-palette', volume_column_count=3, volume_duration_ms=1200,
                        volume_flash_times=2, volume_flash_duration_ratio=.5, volume_time_offset_ms=0)
        times = (150, 540, 710, 860, 1110)
    reference, dpr = None, 3
    if sys.platform == 'win32':
        from test_subtitle_render_gpu_backend import _render_g1_frames

        with NativeRendererProcess() as process:
            _, raw = _render_g1_frames(process, style, times, force_warp=False, track=track,
                                       width=640, height=360, dpr=dpr, realization_enabled=False)
        reference = [np.frombuffer(frame, np.uint8).reshape(360 * dpr, 640 * dpr, 4) for frame in raw]
    gpu, raster = render_skia(binary, ir_for(binary, (track, style), dpr=dpr), times)
    assert_glow_parity(times, reference, gpu, raster, (signal, 'zoom_pulse'), check_p00_edges=True)
    plain = ir_for(binary, (track, replace(style, karaoke_anim='utopia')), dpr=dpr)
    still, _ = render_skia(binary, plain, times)
    active = len(times) // 2
    assert all(np.count_nonzero(frames[active] != still[active]) > 100 for frames in (gpu, raster))
    if signal == 'volume':
        assert np.array_equal(gpu[0], still[0])  # The preceding flash interval must not grow a column.


@pytest.mark.parametrize('signal,effects', [('lamp', ('glow_in', 'none')), ('volume', ('none', 'stretch_out'))])
def test_signal_transition_halo_composes_with_the_row(binary, project, signal, effects):
    from test_subtitle_skia_character_transform import transform_source
    from test_subtitle_skia_outline_ruby import render_skia
    from test_subtitle_skia_roles import colors
    from test_subtitle_skia_static_glow import assert_glow_parity

    track, style = transform_source(project, *effects)
    style = replace(style, karaoke_colors=colors('#12402C', '#12402C'))
    if signal == 'lamp':
        style = replace(style, lit_enabled=True, lit_appearance_mode='custom', lit_style='square',
                        lit_size=22, lit_number=3, lit_fill_color='#FF00FF', lit_offset_y=-75,
                        lit_stroke_width=0, lit_edge_brightness_pct=0, lit_shadow=False,
                        signals_duration_ms=5000, lit_time_offset_ms=3000, lit_transition_mode='none')
        times = (0, 45, 105, 260, 620)
    else:
        style = replace(style, volume_enabled=True, volume_appearance_mode='custom', volume_column_count=3,
                        volume_fill_color='#FF00FF', volume_overlay_fill_color='#FF00FF', volume_offset_y=-75,
                        volume_stroke_width=0, volume_flash_times=0)
        times = (2390, 2470, 2580, 2770, 2860)
    reference, dpr = None, 3
    if sys.platform == 'win32':
        from test_subtitle_render_gpu_backend import _render_g1_frames

        with NativeRendererProcess() as process:
            _, raw = _render_g1_frames(process, style, times, force_warp=False, track=track,
                                       width=640, height=360, dpr=dpr, realization_enabled=False)
        reference = [np.frombuffer(frame, np.uint8).reshape(360 * dpr, 640 * dpr, 4) for frame in raw]
    gpu, raster = render_skia(binary, ir_for(binary, (track, style), dpr=dpr), times)
    assert_glow_parity(times, reference, gpu, raster, ('signal_transition_halo', signal), check_p00_edges=True)
    for frames in (gpu, raster):
        settled = frames[-1] if signal == 'lamp' else frames[0]
        halo = frames[0] if signal == 'lamp' else frames[1]
        ys = np.nonzero(np.all(settled[..., :3] == [255, 0, 255], axis=-1) & (settled[..., 3] > 240))[0]
        assert ys.size > 100
        signal_band = halo[ys.min():ys.max() + 1]
        assert np.count_nonzero((signal_band[..., 3] > 20) & np.all(signal_band[..., :3] > 240, axis=-1)) > 100


@pytest.mark.parametrize('shape', ['square', 'star'])
def test_decorated_lamp_gradient_keeps_its_group_span(binary, project, shape):
    from krok_helper.subtitle_render.domain.models import SubtitleStyleScheme
    from krok_helper.subtitle_render.domain.paint import PaintFill
    from test_subtitle_skia_outline_ruby import render_skia
    from test_subtitle_skia_roles import colors
    from test_subtitle_skia_static_glow import assert_glow_parity

    track, style = project
    line, = track.lines
    shift = 1200
    track = replace(track, lines=[replace(
        line, chars=[replace(char, start_ms=char.start_ms + shift) for char in line.chars],
        end_ms=line.end_ms + shift, display_start_override_ms=0, display_end_override_ms=3700)])
    color = colors('#FFFFFF', '#FFFFFF')
    color = replace(color, after=replace(color.after, text=PaintFill(
        mode='gradient_horizontal', start_color='#FD1906', end_color='#0845FE')))
    style = replace(style, entry_anim='none', exit_anim='none', lit_enabled=True, lit_style=shape,
        karaoke_colors=colors('#CCCCCC', '#CCCCCC'),
        lit_appearance_mode='role', lit_role_name='signal-palette', lit_size=34, lit_number=3,
        lit_offset_y=-35, lit_edge_brightness_pct=0, lit_stroke_width=0, lit_shadow=False,
        signals_duration_ms=1500, lit_time_offset_ms=0, lit_transition_mode='none',
        custom_style_schemes={'signal-palette': SubtitleStyleScheme(font_size_px=50, karaoke_colors=color)})
    times, dpr = (150, 470, 980), 3
    reference = None
    if sys.platform == 'win32':
        from test_subtitle_render_gpu_backend import _render_g1_frames

        with NativeRendererProcess() as process:
            _, raw = _render_g1_frames(process, style, times, force_warp=False, track=track,
                                       width=640, height=360, dpr=dpr, realization_enabled=False)
        reference = [np.frombuffer(frame, np.uint8).reshape(360 * dpr, 640 * dpr, 4) for frame in raw]
    gpu, raster = render_skia(binary, ir_for(binary, (track, style), dpr=dpr), times)
    assert_glow_parity(times, reference, gpu, raster, 'decorated_lamp_group_gradient', check_p00_edges=True)
    for frames in (gpu, raster):
        lamp_pixels = [(frame[..., 3] > 250) & np.any(frame[..., :3] != frame[..., :1], axis=-1)
                       for frame in frames]
        ys, xs = np.nonzero(lamp_pixels[-1])
        assert xs.size > 1000 and np.count_nonzero(lamp_pixels[0]) > xs.size * 2
        assert np.array_equal(frames[0][ys, xs], frames[-1][ys, xs])
        assert len(np.unique(frames[-1][ys, xs, 0])) > 24


@pytest.mark.parametrize('signal', ['lamp', 'volume'])
def test_decorated_signal_ignores_brushes_on_disabled_layers(binary, project, signal):
    from krok_helper.subtitle_render.domain.models import SubtitleStyleScheme
    from krok_helper.subtitle_render.domain.paint import PaintFill
    from test_subtitle_skia_outline_ruby import render_skia
    from test_subtitle_skia_roles import colors
    from test_subtitle_skia_static_glow import assert_glow_parity

    track, style = project
    before_after = colors('#FF00FF', '#FF00FF')
    unused = PaintFill(mode='gradient_horizontal', start_color='#EE2406', end_color='#0845FE')
    before_after = replace(before_after, before=replace(before_after.before, stroke=unused, stroke2=unused, shadow=unused),
                           after=replace(before_after.after, stroke=unused, stroke2=unused, shadow=unused))
    palette = SubtitleStyleScheme(font_size_px=50, stroke_width_px=0, stroke2_enabled=True, stroke2_width_px=0,
                                  decoration_kind='none', karaoke_colors=before_after)
    style = replace(style, custom_style_schemes={'signal-palette': palette})
    if signal == 'lamp':
        style = replace(style, lit_enabled=True, lit_style='square', lit_size=25, lit_number=1, lit_offset_y=-35,
                        lit_appearance_mode='role', lit_role_name='signal-palette', lit_edge_brightness_pct=0)
    else:
        style = replace(style, volume_enabled=True, volume_appearance_mode='role', volume_role_name='signal-palette',
                        volume_column_count=3, volume_flash_times=0)
    times = (360, 750)
    reference = None
    if sys.platform == 'win32':
        from test_subtitle_render_gpu_backend import _render_g1_frames

        with NativeRendererProcess() as process:
            _, raw = _render_g1_frames(process, style, times, force_warp=False, track=track,
                                       width=640, height=360, realization_enabled=False)
        reference = [np.frombuffer(frame, np.uint8).reshape(360, 640, 4) for frame in raw]
    gpu, raster = render_skia(binary, ir_for(binary, (track, style)), times)
    assert_glow_parity(times, reference, gpu, raster, ('decorated_signal_unused_paints', signal), check_p00_edges=True)
    for frames in (gpu, raster):
        assert all(np.count_nonzero(np.all(frame[..., :3] == [255, 0, 255], axis=-1)) > 100 for frame in frames)


def test_decorated_volume_columns_use_the_same_split_height(binary, project):
    from krok_helper.subtitle_render.domain.models import SubtitleStyleScheme
    from krok_helper.subtitle_render.domain.paint import PaintFill
    from test_subtitle_skia_outline_ruby import render_skia
    from test_subtitle_skia_roles import colors
    from test_subtitle_skia_static_glow import assert_glow_parity

    track, style = project
    palette = colors('#FFFFFF', '#FFFFFF')
    unfilled = PaintFill(mode='split_vertical', split_top_color='#2547E8', split_bottom_color='#FFDE24')
    filled = PaintFill(mode='split_vertical', split_top_color='#EA005E', split_bottom_color='#44CD3A')
    palette = SubtitleStyleScheme(font_size_px=48, karaoke_colors=replace(
        palette, before=replace(palette.before, text=unfilled), after=replace(palette.after, text=filled)))
    style = replace(style, volume_enabled=True, volume_appearance_mode='role', volume_role_name='split',
                    custom_style_schemes={'split': palette}, volume_column_count=3, volume_flash_times=0,
                    volume_time_offset_ms=900, volume_duration_ms=900)
    times, dpr = (250, 500, 850), 3
    reference = None
    if sys.platform == 'win32':
        from test_subtitle_render_gpu_backend import _render_g1_frames

        with NativeRendererProcess() as process:
            _, raw = _render_g1_frames(process, style, times, force_warp=False, track=track,
                                       width=640, height=360, dpr=dpr, realization_enabled=False)
        reference = [np.frombuffer(frame, np.uint8).reshape(360 * dpr, 640 * dpr, 4) for frame in raw]
    gpu, raster = render_skia(binary, ir_for(binary, (track, style), dpr=dpr), times)
    assert_glow_parity(times, reference, gpu, raster, 'split_volume_columns', check_p00_edges=True)
    for frames in (gpu, raster):
        filled_y = np.nonzero(np.all(frames[0][..., :3] == [234, 0, 94], axis=-1) & (frames[0][..., 3] > 250))[0]
        unfilled_y = np.nonzero(np.all(frames[0][..., :3] == [37, 71, 232], axis=-1) & (frames[0][..., 3] > 250))[0]
        assert filled_y.size > 100 and unfilled_y.size > 100
        assert filled_y.max() == unfilled_y.max()
        assert not np.any(np.all(frames[-1][..., :3] == [37, 71, 232], axis=-1) & (frames[-1][..., 3] > 250))


@pytest.mark.parametrize('signal', ['lamp', 'volume'])
def test_decorated_signal_uses_the_gradient_for_each_outline(binary, project, signal):
    from krok_helper.subtitle_render.domain.models import SubtitleStyleScheme
    from krok_helper.subtitle_render.domain.paint import PaintFill
    from test_subtitle_skia_outline_ruby import render_skia
    from test_subtitle_skia_roles import colors
    from test_subtitle_skia_static_glow import assert_glow_parity

    track, style = project
    line, = track.lines
    track = replace(track, lines=[replace(line, display_start_override_ms=0)])
    palette = colors('#EEEEEE', '#EEEEEE')
    inner = PaintFill(mode='gradient_vertical', start_color='#EA111C', end_color='#F6860C')
    outer = PaintFill(mode='gradient_horizontal' if signal == 'lamp' else 'gradient_vertical',
                      start_color='#1034ED', end_color='#046DC1')
    palette = SubtitleStyleScheme(font_size_px=48, stroke_width_px=8, stroke2_enabled=True, stroke2_width_px=12,
                                  karaoke_colors=replace(palette, before=replace(palette.before, stroke=inner, stroke2=outer),
                                                         after=replace(palette.after, stroke=inner, stroke2=outer)))
    style = replace(style, karaoke_colors=colors('#BBBBBB', '#BBBBBB'),
                    custom_style_schemes={'outlined-signal': palette})
    if signal == 'lamp':
        style = replace(style, lit_enabled=True, lit_style='square', lit_appearance_mode='role',
                        lit_role_name='outlined-signal', lit_size=34, lit_number=3,
                        lit_edge_brightness_pct=0, lit_offset_y=-42, lit_time_offset_ms=1200,
                        signals_duration_ms=1200, lit_transition_mode='none')
    else:
        style = replace(style, volume_enabled=True, volume_appearance_mode='role', volume_role_name='outlined-signal',
                        volume_size=55, volume_column_count=3, volume_flash_times=0)
    times, dpr = (260, 650, 1000), 3
    reference = None
    if sys.platform == 'win32':
        from test_subtitle_render_gpu_backend import _render_g1_frames

        with NativeRendererProcess() as process:
            _, raw = _render_g1_frames(process, style, times, force_warp=False, track=track,
                                       width=640, height=360, dpr=dpr, realization_enabled=False)
        reference = [np.frombuffer(frame, np.uint8).reshape(360 * dpr, 640 * dpr, 4) for frame in raw]
    gpu, raster = render_skia(binary, ir_for(binary, (track, style), dpr=dpr), times)
    assert_glow_parity(times, reference, gpu, raster, ('decorated_signal_gradient_outlines', signal),
                       check_p00_edges=True)
    for frames in (gpu, raster):
        r, g, b, a = np.moveaxis(frames[0], -1, 0)
        outline_pixels = a > 250
        assert len(np.unique(g[outline_pixels & (r > 180) & (b < 60)])) > 10
        assert len(np.unique(g[outline_pixels & (b > 140) & (r < 80)])) > 10


@pytest.mark.parametrize('appearance', ['role', 'auto'])
def test_volume_horizontal_gradient_spans_the_columns_and_first_text_role(binary, project, appearance):
    from krok_helper.subtitle_render.domain.models import SubtitleStyleScheme
    from krok_helper.subtitle_render.domain.paint import PaintFill
    from test_subtitle_skia_outline_ruby import render_skia
    from test_subtitle_skia_roles import colors
    from test_subtitle_skia_static_glow import assert_glow_parity

    track, style = project
    line, = track.lines
    track = replace(track, lines=[replace(line, chars=[
        *line.chars[:3], *(replace(char, role_label='following') for char in line.chars[3:])])])
    neutral = colors('#BBBBBB', '#BBBBBB')
    paint = PaintFill(mode='gradient_horizontal', start_color='#EE4008', end_color='#123DFF')
    column_palette = replace(neutral, before=replace(neutral.before, text=paint), after=replace(neutral.after, text=paint))
    style = replace(style, karaoke_colors=column_palette if appearance == 'auto' else neutral,
                    volume_enabled=True, volume_appearance_mode=appearance, volume_offset_x=-12,
                    volume_role_name='column-gradient', volume_size=55, volume_column_count=3, volume_flash_times=0,
                    custom_style_schemes={
                        'column-gradient': SubtitleStyleScheme(font_size_px=50, karaoke_colors=column_palette),
                        'following': SubtitleStyleScheme(font_size_px=50, karaoke_colors=neutral),
                    })
    times, dpr = (260, 650, 1000), 3
    reference = None
    if sys.platform == 'win32':
        from test_subtitle_render_gpu_backend import _render_g1_frames

        with NativeRendererProcess() as process:
            _, raw = _render_g1_frames(process, style, times, force_warp=False, track=track,
                                       width=640, height=360, dpr=dpr, realization_enabled=False)
        reference = [np.frombuffer(frame, np.uint8).reshape(360 * dpr, 640 * dpr, 4) for frame in raw]
    ir = ir_for(binary, (track, style), dpr=dpr)
    assert ir['track']['lines'][0]['signal_band_join'] == (appearance == 'auto')
    gpu, raster = render_skia(binary, ir, times)
    assert_glow_parity(times, reference, gpu, raster, ('volume_horizontal_brush', appearance), check_p00_edges=True)
    text_left = round(ir['skia_layout']['lines'][0]['volume_signal_anchor'][0] * dpr)
    for frames in (gpu, raster):
        r, g, b, a = np.moveaxis(frames[0][:, :text_left], -1, 0)
        column_red = r[(a > 250) & ((r != g) | (g != b))]
        assert len(np.unique(column_red)) > 12
        assert 18 < column_red.min() < column_red.max() <= 238


@pytest.mark.parametrize('signal', ['lamp', 'volume'])
def test_signal_shadow_uses_its_authored_gradient(binary, project, signal):
    _assert_signal_decoration_gradient(binary, project, signal, 'shadow')


@pytest.mark.parametrize('signal', ['lamp', 'volume'])
def test_signal_glow_uses_its_authored_gradient(binary, project, signal):
    _assert_signal_decoration_gradient(binary, project, signal, 'glow')


@pytest.mark.parametrize('signal,decoration', [('lamp', 'glow'), ('volume', 'shadow')])
def test_signal_texture_decorates_the_actual_outline_and_halo(binary, project, tmp_path, signal, decoration):
    from test_subtitle_skia_image_fills import image_paint, pattern, write_png

    texture = image_paint(write_png(tmp_path / 'signal-decor.png', pattern('opaque')), scale=165)
    _assert_signal_decoration_gradient(binary, project, signal, decoration, texture=texture)


def _assert_signal_decoration_gradient(binary, project, signal, decoration, *, texture=None):
    from krok_helper.subtitle_render.domain.models import SubtitleStyleScheme
    from krok_helper.subtitle_render.domain.paint import PaintFill
    from test_subtitle_skia_outline_ruby import render_skia
    from test_subtitle_skia_roles import colors
    from test_subtitle_skia_static_glow import assert_glow_parity

    track, style = project
    neutral = colors('#BBBBBB', '#BBBBBB')
    decor = texture or PaintFill(mode='gradient_vertical', start_color='#D52C36', end_color='#2FC82A')
    appearance = neutral
    if texture:
        appearance = replace(neutral, before=replace(neutral.before, stroke=texture, stroke2=texture),
                              after=replace(neutral.after, stroke=texture, stroke2=texture))
    palette = SubtitleStyleScheme(font_size_px=50, decoration_kind=decoration, shadow_offset_x=13, shadow_offset_y=9,
                                  glow_before_radius_px=9, glow_after_radius_px=9, glow_concentration_level=1,
                                  stroke_width_px=3 if texture else 0, stroke2_enabled=bool(texture), stroke2_width_px=5,
                                  karaoke_colors=replace(appearance, before=replace(appearance.before, shadow=decor),
                                                         after=replace(appearance.after, shadow=decor)))
    style = replace(style, karaoke_colors=neutral, custom_style_schemes={'signal-shadow': palette})
    if signal == 'lamp':
        style = replace(style, lit_enabled=True, lit_style='square', lit_appearance_mode='role',
                        lit_role_name='signal-shadow', lit_size=34, lit_number=1,
                        lit_edge_brightness_pct=0, lit_offset_y=-42)
    else:
        style = replace(style, volume_enabled=True, volume_appearance_mode='role', volume_role_name='signal-shadow',
                        volume_size=55, volume_column_count=3, volume_flash_times=0)
    times, dpr = (260, 650), 3
    reference = None
    if sys.platform == 'win32':
        from test_subtitle_render_gpu_backend import _render_g1_frames

        with NativeRendererProcess() as process:
            _, raw = _render_g1_frames(process, style, times, force_warp=False, track=track,
                                       width=640, height=360, dpr=dpr, realization_enabled=False)
        reference = [np.frombuffer(frame, np.uint8).reshape(360 * dpr, 640 * dpr, 4) for frame in raw]
    gpu, raster = render_skia(binary, ir_for(binary, (track, style), dpr=dpr), times)
    assert_glow_parity(times, reference, gpu, raster, ('signal_gradient', signal, decoration), check_p00_edges=True)
    for frames in (gpu, raster):
        r, g, b, a = np.moveaxis(frames[0], -1, 0)
        shaded = (a > (250 if decoration == 'shadow' else 40)) & ((r != g) | (g != b))
        assert np.count_nonzero(shaded) > 500
        assert len(np.unique(g[shaded])) > 12


@pytest.mark.parametrize('signal,entry', [('lamp', 'char_drip'), ('volume', 'slide_in')])
def test_signal_image_fill_follows_the_authored_brush_transform(binary, project, tmp_path, signal, entry):
    from krok_helper.subtitle_render.domain.models import SubtitleStyleScheme
    from test_subtitle_skia_image_fills import image_paint, pattern, write_png
    from test_subtitle_skia_outline_ruby import render_skia
    from test_subtitle_skia_roles import colors
    from test_subtitle_skia_static_glow import assert_glow_parity

    track, style = project
    line, = track.lines
    shift = 1000
    track = replace(track, lines=[replace(
        line, chars=[replace(char, start_ms=char.start_ms + shift) for char in line.chars],
        end_ms=line.end_ms + shift, display_start_override_ms=0, display_end_override_ms=3400)])
    fill = image_paint(write_png(tmp_path / 'signal-fill.png', pattern('opaque')), scale=110)
    neutral = colors('#BBBBBB', '#BBBBBB')
    palette = SubtitleStyleScheme(font_size_px=50, karaoke_colors=replace(
        neutral, before=replace(neutral.before, text=fill), after=replace(neutral.after, text=fill)))
    style = replace(style, karaoke_colors=neutral, entry_anim=entry, entry_lead_ms=600, exit_anim='none',
                    custom_style_schemes={'textured-signal': palette})
    if signal == 'lamp':
        style = replace(style, lit_enabled=True, lit_style='square', lit_appearance_mode='role',
                        lit_role_name='textured-signal', lit_size=30, lit_number=3, lit_offset_y=-35,
                        lit_edge_brightness_pct=0, signals_duration_ms=5000, lit_time_offset_ms=5000,
                        lit_transition_mode='none')
    else:
        style = replace(style, volume_enabled=True, volume_appearance_mode='role', volume_role_name='textured-signal',
                        volume_size=52, volume_column_count=3, volume_flash_times=0)
    times, dpr = (180, 360, 650), 3
    reference = None
    if sys.platform == 'win32':
        from test_subtitle_render_gpu_backend import _render_g1_frames

        with NativeRendererProcess() as process:
            _, raw = _render_g1_frames(process, style, times, force_warp=False, track=track,
                                       width=640, height=360, dpr=dpr, realization_enabled=False)
        reference = [np.frombuffer(frame, np.uint8).reshape(360 * dpr, 640 * dpr, 4) for frame in raw]
    gpu, raster = render_skia(binary, ir_for(binary, (track, style), dpr=dpr), times)
    assert_glow_parity(times, reference, gpu, raster, ('textured_signal', signal), check_p00_edges=True)
    for frames in (gpu, raster):
        assert all(np.count_nonzero((frame[..., 3] > 30) & (frame[..., 0] != frame[..., 1])) > 100 for frame in frames)
    comparisons = [(gpu, raster)] if reference is None else [(reference, gpu), (gpu, raster)]
    for expected, actual in comparisons:
        for a, b in zip(expected, actual, strict=True):
            colored = (a[..., 0] != a[..., 1]) | (b[..., 0] != b[..., 1])
            peak = min(a[..., 3][colored].max(), b[..., 3][colored].max())
            interior = colored & (a[..., 3] >= peak - 2) & (b[..., 3] >= peak - 2)
            diff = abs(premultiplied(a[interior]) - premultiplied(b[interior]))
            assert diff.size and diff.mean() <= 5 and np.percentile(diff, 95) <= 24


def test_rtl_signals_share_resolved_text_anchor(binary, project):
    from test_subtitle_skia_outline_ruby import render_skia
    from test_subtitle_skia_rtl_flow import rtl_source
    from test_subtitle_skia_static_glow import assert_glow_parity

    track, style = rtl_source(project)
    line, = track.lines
    chars = [replace(char, start_ms=char.start_ms + 1000) for char in line.chars]
    track = replace(track, lines=[replace(line, chars=chars, end_ms=line.end_ms + 1000)])
    style = replace(style, lit_enabled=True, lit_appearance_mode='custom', lit_style='square',
                    lit_size=20, lit_offset_y=-30, lit_number=1, lit_fill_color='#FF00FF',
                    lit_stroke_width=0, lit_edge_brightness_pct=0, lit_shadow=False,
                    volume_enabled=True, volume_appearance_mode='custom', volume_fill_color='#35DFF0',
                    volume_overlay_fill_color='#35DFF0', volume_stroke_width=0, volume_duration_ms=1100,
                    volume_column_count=3, volume_flash_times=0)
    times = (500, 950)
    reference = None
    if sys.platform == 'win32':
        from test_subtitle_render_gpu_backend import _render_g1_frames

        with NativeRendererProcess() as process:
            _, raw = _render_g1_frames(process, style, times, force_warp=False, track=track,
                                       width=640, height=360, realization_enabled=False)
        reference = [np.frombuffer(frame, np.uint8).reshape(360, 640, 4) for frame in raw]
    ir = ir_for(binary, (track, style))
    row, = ir['skia_layout']['lines']
    assert {'text_flow', 'shape_signal_anchor', 'volume_signal_anchor'} <= row.keys()
    gpu, raster = render_skia(binary, ir, times)
    assert_glow_parity(times, reference, gpu, raster, 'rtl_line_signals', check_p00_edges=True)
    for frame in gpu:
        for rgb in ([255, 0, 255], [53, 223, 240]):
            assert np.count_nonzero(np.all(frame[..., :3] == rgb, axis=-1) & (frame[..., 3] > 250)) > 100


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
