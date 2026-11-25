"""Body gradients use the authored before/after fills and one line-wide domain."""
from dataclasses import replace
import copy
import json
import sys

import numpy as np
import pytest

from test_subtitle_skia_line_fade import (
    application, binary, project, ir_for, collect, configure, selection, premultiplied,
)
from test_subtitle_skia_primary_outline import within_one_pixel
from test_subtitle_skia_reverse import source as clock_source
from test_subtitle_skia_outline_ruby import annotated, mixed, pages, prepared_source, reading, source, upper_ir
from krok_helper.subtitle_render.domain.models import LineAnimationOverride
from krok_helper.subtitle_render.domain.paint import PaintFill
from krok_helper.subtitle_render.native.backend import NativeRendererError, NativeRendererProcess
from krok_helper.subtitle_render.native.skia_backend import SkiaRendererProcess
from scripts.subtitle_gpu_baseline.policy import COMPARISON_BUDGETS


MODES = ('gradient_horizontal', 'gradient_vertical', 'split_vertical')
TIMES = [0, 200, 400, 750, 1100, 1550, 1800, 2200, 3000]


def gradient_source(project, mode, translucent=False):
    track, style = clock_source(project, 'plain')
    track = replace(track, lines=[replace(track.lines[0], wipe_reverse=False)])
    before, after = style.karaoke_colors.before, style.karaoke_colors.after
    colors = replace(
        style.karaoke_colors,
        before=replace(before, text=PaintFill(mode=mode, start_color='#E04418', end_color='#179BCA',
                                             split_stops=[(0, '#E04418'), (50, '#179BCA')])),
        after=replace(after, text=PaintFill(mode=mode, start_color='#28CB53', end_color='#BB37AF',
                                           split_stops=[(0, '#28CB53'), (50, '#BB37AF')])),
    )
    if translucent:
        colors = replace(
            colors,
            before=replace(colors.before, text=replace(colors.before.text, start_color='#80E04418', end_color='#E0179BCA')),
            after=replace(colors.after, text=replace(colors.after.text, start_color='#6028CB53', end_color='#C0BB37AF')),
        )
    return track, replace(style, karaoke_colors=colors, right_to_left=False)


@pytest.mark.parametrize('target', ['gpu', 'raster'])
@pytest.mark.parametrize('karaoke', ['none', 'utopia'])
def test_upper_ruby_stays_independent_of_body_gradient(binary, reading, target, karaoke):
    track, style = reading
    style = replace(style, karaoke_anim=karaoke)
    gradient = PaintFill(mode='gradient_horizontal', start_color='#FFFFAA00', end_color='#FF0044FF')
    colors = replace(style.karaoke_colors, before=replace(style.karaoke_colors.before, text=gradient))
    ir = upper_ir(binary, (track, replace(style, karaoke_colors=colors)), 1)
    plain = upper_ir(binary, (track, style), 1)
    assert 'p06_ruby_main_paint' in ir['required_features']
    assert ('p06_utopia_main_paint' in ir['required_features']) == (karaoke == 'utopia')
    bad = copy.deepcopy(ir)
    bad['required_features'].remove('p06_utopia_main_paint' if karaoke == 'utopia' else 'p06_ruby_main_paint')
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, plain)
        ruby, _ = collect(process, [150])
        configure(process, ir)
        painted, _ = collect(process, [150], generation=2)
        replay, _ = collect(process, [150], generation=2)
    region = ruby[0][..., 3] > 16
    assert region.any() and np.array_equal(painted[0][region], ruby[0][region])
    assert np.count_nonzero(painted[0][..., 3] > 16) > np.count_nonzero(region)
    assert np.array_equal(replay[0], painted[0])


@pytest.mark.parametrize('effect', ['fade', 'scanline', 'utopia'])
def test_gradient_effect_on_unannotated_row_works_with_another_rows_ruby(binary, reading, effect):
    from test_subtitle_skia_outline_ruby import render_skia
    from test_subtitle_skia_scanline import band
    from test_subtitle_skia_static_glow import assert_glow_parity

    track, style = reading
    ramp = PaintFill(mode='gradient_vertical', start_color='#FFCC40', end_color='#307AEA')
    colors = style.karaoke_colors
    style = replace(style, karaoke_colors=replace(colors, before=replace(colors.before, text=ramp)),
                    entry_anim='none', exit_anim='none', karaoke_anim='none', scanline_enabled=False)
    if effect == 'scanline':
        style = band(style)
    first, = track.lines
    first = replace(first, display_end_override_ms=2600, animation_override=LineAnimationOverride(scanline_enabled=False))
    second = replace(first, track_line_index=1, break_before='page',
                     chars=[replace(char, start_ms=char.start_ms + 3800) for char in first.chars],
                     end_ms=5900, display_start_override_ms=3500, display_end_override_ms=6000,
                     animation_override=LineAnimationOverride(
                         entry_anim='fade' if effect == 'fade' else 'none',
                         entry_duration_ms=600 if effect == 'fade' else 0,
                         scanline_enabled=effect == 'scanline',
                         karaoke_anim='utopia' if effect == 'utopia' else 'none'))
    track = replace(track, lines=[first, second])
    ir = upper_ir(binary, (track, style), 1)
    assert [bool(line['ruby_groups']) for line in ir['skia_layout']['lines']] == [True, False]
    feature = 'p06_unannotated_singing_paints' if effect == 'utopia' else f'p06_unscaled_paint_{effect}'
    assert {'p06_ruby_main_paint', feature} <= set(ir['required_features'])
    times = (450, 1300, 3600, 3800, 4200, 4850)
    gpu, raster = render_skia(binary, ir, times)

    reference = None
    if sys.platform == 'win32':
        from test_subtitle_render_gpu_backend import _render_g1_frames

        without_effect = replace(track, lines=[first, replace(second, animation_override=first.animation_override)])
        refs = []
        for source_track in (track, without_effect):
            with NativeRendererProcess() as process:
                _, raw = _render_g1_frames(process, style, times, force_warp=False, track=source_track,
                                           width=640, height=360, realization_enabled=False)
            refs.append([np.frombuffer(frame, np.uint8).reshape(360, 640, 4) for frame in raw])
        reference, control = refs
        assert all(np.array_equal(a, b) for a, b in zip(reference[:2], control[:2], strict=True))
        assert any(not np.array_equal(a, b) for a, b in zip(reference[2:], control[2:], strict=True))
    assert_glow_parity(times, reference, gpu, raster, ('gradient_on_unannotated_row', effect), check_p00_edges=True)


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('mode', [*MODES, 'image'])
@pytest.mark.parametrize('karaoke', ['none', 'utopia'])
def test_p00_ruby_with_main_paint_matches_actual_gpu(binary, application, tmp_path, mode, karaoke):
    from scripts.subtitle_gpu_baseline.cases import build_cases, representative_style
    from test_subtitle_render_gpu_backend import _render_g1_frames
    from test_subtitle_skia_static_glow import assert_glow_parity

    case = next(case for case in build_cases(tmp_path) if case.case_id == f'paint_{mode}')
    style = replace(case.style, karaoke_anim=karaoke, ruby_colors_follow_main=False,
                    ruby_karaoke_colors=representative_style().karaoke_colors)
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


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
def test_utopia_transparent_body_image_keeps_its_cutouts_between_outlines(binary, application, tmp_path):
    from scripts.subtitle_gpu_baseline.cases import build_cases, representative_style
    from test_subtitle_skia_character_fade import assert_ruby_transition_gpu_parity
    from test_subtitle_skia_image_fills import pattern, write_png

    base = next(case for case in build_cases(tmp_path) if case.case_id == 'paint_image')
    texture = write_png(tmp_path / 'paint-with-alpha.png', pattern('transparent'))
    colors = base.style.karaoke_colors
    before = replace(colors.before, text=replace(colors.before.text, image_path=str(texture)))
    after = replace(colors.after, text=replace(colors.after.text, image_path=str(texture)))
    style = replace(base.style, karaoke_anim='utopia', ruby_colors_follow_main=False,
                    karaoke_colors=replace(colors, before=before, after=after),
                    ruby_karaoke_colors=representative_style().karaoke_colors)
    case = replace(base, case_id='utopia_paint_image_with_alpha', style=style)
    assert_ruby_transition_gpu_parity(binary, case, (600, 1230, 1500, 2350, 2600, 3550, 4250, 4600))


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('mode', MODES)
def test_p00_inherited_ruby_gradients_match_actual_gpu(binary, application, tmp_path, mode):
    from scripts.subtitle_gpu_baseline.cases import build_cases
    from test_subtitle_skia_character_fade import assert_ruby_transition_gpu_parity

    case = next(case for case in build_cases(tmp_path) if case.case_id == f'paint_{mode}')
    times = tuple(sorted({*case.timestamps_ms, 3300, 3430, 3520, 3750, 4111, 4490, 4520}))
    assert_ruby_transition_gpu_parity(binary, case, times)


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('texture', ['original', 'transparent'])
def test_p00_inherited_ruby_image_matches_actual_gpu(binary, application, tmp_path, texture):
    from scripts.subtitle_gpu_baseline.cases import build_cases
    from test_subtitle_skia_character_fade import assert_ruby_transition_gpu_parity
    from test_subtitle_skia_image_fills import pattern, write_png

    case = next(case for case in build_cases(tmp_path) if case.case_id == 'paint_image')
    if texture == 'transparent':
        image = write_png(tmp_path / 'inherited-alpha.png', pattern(texture))
        colors = case.style.karaoke_colors
        before = replace(colors.before, text=replace(colors.before.text, image_path=str(image)))
        after = replace(colors.after, text=replace(colors.after.text, image_path=str(image)))
        case = replace(case, case_id='inherited_ruby_alpha_image',
                       style=replace(case.style, karaoke_colors=replace(colors, before=before, after=after)))
    times = tuple(sorted({*case.timestamps_ms, 3430, 3520, 3750, 4111, 4490, 4520}))
    assert_ruby_transition_gpu_parity(binary, case, times)


@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_ruby_gradient_can_use_its_ink_or_the_shared_body_domain(binary, reading, target):
    track, style = reading
    shades = PaintFill(mode='gradient_horizontal', start_color='#FFDD1100', end_color='#FF2211DD')
    body = replace(style.karaoke_colors, before=replace(style.karaoke_colors.before, text=shades))
    ruby = replace(style.ruby_karaoke_colors, before=replace(style.ruby_karaoke_colors.before, text=shades))
    style = replace(style, karaoke_anim='utopia', karaoke_colors=body, ruby_karaoke_colors=ruby)
    layout = [upper_ir(binary, (track, replace(style, ruby_horizontal_gradient_with_main=share)), 1)
              for share in (False, True)]
    feature = 'p06_ruby_fill_gradient'
    assert all(feature in ir['required_features'] for ir in layout)
    assert layout[0]['skia_layout']['lines'] == layout[1]['skia_layout']['lines']
    units = layout[0]['skia_layout']['lines'][0]['ruby_groups'][0]['units']
    cutoff = round(max(unit['baseline_y'] + unit['metrics']['ink'][3] for unit in units)) + 2
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, layout[0])
        local, _ = collect(process, [150])
        configure(process, layout[1])
        shared, _ = collect(process, [150, 500, 150], generation=2)
        bad = copy.deepcopy(layout[1])
        bad['required_features'].remove(feature)
        replay, _ = collect(process, [150], generation=2)
    assert np.array_equal(shared[0], shared[-1]) and np.array_equal(shared[0], replay[0])
    assert np.array_equal(local[0][..., 3], shared[0][..., 3])
    assert np.array_equal(local[0][cutoff:], shared[0][cutoff:])
    assert not np.array_equal(local[0][:cutoff], shared[0][:cutoff])


@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_inherited_ruby_image_uses_shared_texture_and_missing_asset_fallback(binary, reading, tmp_path, target):
    from test_subtitle_skia_image_fills import pattern, write_png

    track, style = reading
    texture = write_png(tmp_path / 'body-and-ruby.png', pattern('transparent'))
    style = replace(style, karaoke_anim='utopia', ruby_colors_follow_main=True, ruby_karaoke_colors=None)

    def layout(fill):
        colors = replace(style.karaoke_colors, before=replace(style.karaoke_colors.before, text=fill))
        return upper_ir(binary, (track, replace(style, karaoke_colors=colors)), 1)

    image = PaintFill(mode='image', color='#FF822ADA', image_path=str(texture))
    present = layout(image)
    missing = layout(replace(image, image_path=str(tmp_path / 'missing.png')))
    solid = layout(PaintFill(color=image.color))
    assert 'p06_ruby_fill_image' in present['required_features']
    units = present['skia_layout']['lines'][0]['ruby_groups'][0]['units']
    cutoff = round(max(unit['baseline_y'] + unit['metrics']['ink'][3] for unit in units)) + 2
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, present)
        actual, _ = collect(process, [150, 500, 150])
        configure(process, missing)
        fallback, _ = collect(process, [150], generation=2)
        configure(process, solid)
        expected, _ = collect(process, [150], generation=3)
    assert np.array_equal(actual[0], actual[-1])
    assert fallback[0][:cutoff].any() and np.array_equal(fallback, expected)
    assert not np.array_equal(actual[0][:cutoff], expected[0][:cutoff])


def independently_painted_reading(reading, tmp_path):
    from test_subtitle_skia_image_fills import pattern, write_png
    from test_subtitle_skia_roles import colors

    track, style = reading
    texture = write_png(tmp_path / 'reading.png', pattern('opaque'))
    palette = colors('#FFE6A0', '#8CF0E6')
    before = replace(palette.before, text=PaintFill(
        mode='gradient_horizontal', start_color='#F95831', end_color='#325BF2'))
    after = replace(palette.after, text=PaintFill(mode='image', image_path=str(texture)))
    return track, replace(style, karaoke_anim='none', ruby_colors_follow_main=False,
                          ruby_karaoke_colors=replace(palette, before=before, after=after))


def test_independent_ruby_paint_uses_separate_phases_and_preserves_scene(binary, reading, tmp_path):
    ir = upper_ir(binary, independently_painted_reading(reading, tmp_path), 1)
    feature = 'p06_independent_ruby_fill'
    assert feature in ir['required_features']
    assert 'p06_body_gradient' not in ir['required_features'] and 'p06_image_fill' not in ir['required_features']
    forged = copy.deepcopy(ir)
    forged['required_features'].remove(feature)
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        configure(process, ir)
        frames, _ = collect(process, [550, 1100, 2300])
        replay, _ = collect(process, [2300, 1100, 550], generation=2)
    assert np.array_equal(frames, replay[::-1])
    assert all(frame.any() for frame in frames)


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
def test_independent_ruby_paints_match_actual_d2d_gpu(binary, reading, tmp_path):
    from test_subtitle_render_gpu_backend import _render_g1_frames
    from test_subtitle_skia_outline_ruby import render_skia
    from test_subtitle_skia_static_glow import assert_glow_parity

    track, style = independently_painted_reading(reading, tmp_path)
    times = (550, 1100, 1700, 2300)
    with NativeRendererProcess() as process:
        _, raw = _render_g1_frames(process, style, times, force_warp=False, track=track,
                                   width=640, height=360, realization_enabled=False)
    refs = [np.frombuffer(frame, np.uint8).reshape(360, 640, 4) for frame in raw]
    gpu, raster = render_skia(binary, upper_ir(binary, (track, style), 1), times)
    assert_glow_parity(times, refs, gpu, raster, 'independent_ruby_paint', check_p00_edges=True)


@pytest.mark.parametrize('mode', MODES)
@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_gradient_uses_one_domain_and_replays(binary, project, mode, target, tmp_path):
    ir = ir_for(binary, gradient_source(project, mode), dpr=1.5)
    assert 'p06_body_gradient' in ir['required_features']
    bounds = ir['skia_layout']['lines'][0]['body_gradient_bounds']
    assert bounds[0] < bounds[2] and bounds[1] < bounds[3]
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, [*TIMES, 750])
    assert np.array_equal(frames[3], frames[-1])
    before, after = frames[0], frames[TIMES.index(2200)]
    assert not frames[-2][..., 3].any()
    for frame in (before, after):
        y, x = np.where(frame[..., 3] == 255)
        assert x.size > 50
        if mode == 'gradient_horizontal':
            midpoint = (x.max() + x.min()) / 2
            axis = x
        else:
            midpoint = (y.max() + y.min()) / 2
            axis = y
        left, right = frame[y[axis < midpoint], x[axis < midpoint], :3], frame[y[axis > midpoint], x[axis > midpoint], :3]
        assert abs(left.mean(axis=0) - right.mean(axis=0)).max() > 35
    assert not np.array_equal(before, after)
    if mode == 'gradient_horizontal' and target == 'gpu':
        (tmp_path / 'body-gradient-layout.json').write_text(json.dumps({
            'features': ir['required_features'], 'line': ir['skia_layout']['lines'][0],
        }, sort_keys=True), encoding='utf-8')


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('mode,translucent', [*((mode, False) for mode in MODES),
                                               ('gradient_horizontal', True), ('gradient_vertical', True)])
def test_gradient_matches_actual_d2d_and_gpu_raster(binary, project, mode, translucent):
    from test_subtitle_render_gpu_backend import _render_g1_frames

    track, style = gradient_source(project, mode, translucent)
    with NativeRendererProcess() as process:
        _, raw = _render_g1_frames(process, style, tuple(TIMES), force_warp=False, track=track,
                                   width=640, height=360, dpr=1.5, realization_enabled=False)
    refs = [np.frombuffer(p, np.uint8).reshape(540, 960, 4) for p in raw]
    ir = ir_for(binary, (track, style), dpr=1.5)
    results = []
    for target in ('gpu', 'raster'):
        with SkiaRendererProcess(binary, selection=selection(target)) as process:
            configure(process, ir)
            frames, _ = collect(process, TIMES)
        results.append(frames)
    budget = COMPARISON_BUDGETS['image_categories']['text']
    for t, d2d, gpu, raster in zip(TIMES, refs, *results, strict=True):
        for name, a, b in (('d2d', d2d, gpu), ('raster', gpu, raster)):
            ma, mb = a[..., 3] > 16, b[..., 3] > 16
            union = ma | mb
            if not union.any():
                continue
            assert ma.any() and mb.any(), (mode, name, t)
            assert within_one_pixel(ma, mb), (mode, name, t, 'ink')
            assert (ma & mb).sum() / union.sum() >= budget['min_alpha_iou'], (mode, name, t, 'mask')
            diff = abs(premultiplied(a)[union] - premultiplied(b)[union])
            assert diff.mean() <= budget['max_union_channel_mae'], (mode, name, t, diff.mean())
            assert np.percentile(diff, 95) <= budget['max_union_p95'], (mode, name, t, np.percentile(diff, 95))


@pytest.mark.parametrize('mutation', ['bounds'])
def test_invalid_gradient_keeps_previous_scene(binary, project, mutation):
    ir = ir_for(binary, gradient_source(project, 'gradient_horizontal'))
    bad = copy.deepcopy(ir)
    if mutation == 'feature':
        bad['required_features'].remove('p06_body_gradient')
    else:
        bad['skia_layout']['lines'][0]['body_gradient_bounds'] = [1, 2, 0, 3]
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        configure(process, ir)
        before, _ = collect(process, [750])
        with pytest.raises(NativeRendererError):
            configure(process, bad)
        after, _ = collect(process, [750], generation=2)
    assert np.array_equal(before, after)


def layer_paint(mode, start, end):
    if mode == 'solid':
        return PaintFill(color=start)
    return PaintFill(mode=mode, start_color=start, end_color=end, split_stops=[(0, start), (50, end)])


# (body, outline, outer ring); translucent stops exercise the existing GPU's body protection.
OUTLINE_CASES = {
    'primary': ('solid', 'gradient_horizontal', None),
    'secondary': ('solid', 'solid', 'gradient_vertical'),
    'body_padded': ('gradient_vertical', 'solid', 'solid'),
    'all': ('split_vertical', 'gradient_vertical', 'gradient_horizontal'),
    'translucent': ('gradient_horizontal', 'gradient_vertical', 'solid'),
}


def outline_gradient_source(project, case):
    body, outline, outer = OUTLINE_CASES[case]
    track, style = gradient_source(project, body if body != 'solid' else 'gradient_horizontal',
                                   translucent=case == 'translucent')
    alpha = '#A0' if case == 'translucent' else '#FF'
    colors = style.karaoke_colors
    before, after = colors.before, colors.after
    if body == 'solid':
        before, after = replace(before, text=PaintFill(color='#FFFF0000')), replace(after, text=PaintFill(color='#FF00FF00'))
    before = replace(before, stroke=layer_paint(outline, alpha + '2040E0', alpha + 'F0D020'))
    after = replace(after, stroke=layer_paint(outline, alpha + 'E02090', alpha + '20E0E0'))
    if outer:
        before = replace(before, stroke2=layer_paint(outer, '#FF102060', '#FFE0E0E0'))
        after = replace(after, stroke2=layer_paint(outer, '#FF601020', '#FF30A040'))
    return track, replace(style, karaoke_colors=replace(colors, before=before, after=after), stroke_width_px=5,
                          stroke2_enabled=outer is not None, stroke2_width_px=4 if outer else 0)


def test_whole_cell_switch_uses_body_and_outline_gradients(binary, project):
    from test_subtitle_skia_outline_ruby import render_skia
    from test_subtitle_skia_static_glow import assert_glow_parity

    track, style = outline_gradient_source(project, 'all')
    style = replace(style, karaoke_anim='no_wipe')
    ir = upper_ir(binary, (track, style), 1)
    assert {'p06_body_gradient', 'p06_outline_gradient', 'p07_no_wipe'} <= set(ir['required_features'])
    _, end = ir['track']['lines'][0]['resolved_intervals'][0]
    times = (end - 100, end - 1, end, end + 1, end + 2, 1550)
    gpu, raster = render_skia(binary, ir, times)
    assert np.array_equal(gpu[0], gpu[1])
    assert not np.array_equal(gpu[1], gpu[2])
    assert np.array_equal(gpu[3], gpu[4])
    reference = None
    if sys.platform == 'win32':
        from test_subtitle_render_gpu_backend import _render_g1_frames

        with NativeRendererProcess() as process:
            _, raw = _render_g1_frames(process, style, times, force_warp=False, track=track,
                                       width=640, height=360, realization_enabled=False)
        reference = [np.frombuffer(frame, np.uint8).reshape(360, 640, 4) for frame in raw]
    assert_glow_parity(times, reference, gpu, raster, 'whole_cell_gradient', check_p00_edges=True)


def assert_text_parity(refs, results, sample):
    budget = COMPARISON_BUDGETS['image_categories']['text']
    for t, d2d, gpu, raster in zip(TIMES, refs, *results, strict=True):
        for name, a, b in (('d2d', d2d, gpu), ('raster', gpu, raster)):
            ma, mb = a[..., 3] > 16, b[..., 3] > 16
            union = ma | mb
            if not union.any():
                continue
            assert ma.any() and mb.any(), (*sample, name, t)
            assert within_one_pixel(ma, mb), (*sample, name, t, 'ink')
            assert (ma & mb).sum() / union.sum() >= budget['min_alpha_iou'], (*sample, name, t, 'mask')
            diff = abs(premultiplied(a)[union] - premultiplied(b)[union])
            assert diff.mean() <= budget['max_union_channel_mae'], (*sample, name, t, diff.mean())
            assert np.percentile(diff, 95) <= budget['max_union_p95'], (*sample, name, t, np.percentile(diff, 95))


@pytest.mark.parametrize('case', list(OUTLINE_CASES))
@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_outline_gradients_declare_capability_and_replay(binary, project, case, target, tmp_path):
    track, style = outline_gradient_source(project, case)
    ir = ir_for(binary, (track, style), dpr=1.5)
    features = ir['required_features']
    assert {'p06_body_gradient', 'p06_outline_gradient', 'p06_primary_solid_outline'} <= set(features)
    assert ('p06_secondary_solid_outline' in features) == (OUTLINE_CASES[case][2] is not None)
    solid = ir_for(binary, (track, replace(style, karaoke_colors=replace(
        style.karaoke_colors,
        before=replace(style.karaoke_colors.before, text=PaintFill(color='#FFFF0000'),
                       stroke=PaintFill(color='#FF2040E0'), stroke2=PaintFill(color='#FF102060')),
        after=replace(style.karaoke_colors.after, text=PaintFill(color='#FF00FF00'),
                      stroke=PaintFill(color='#FFE02090'), stroke2=PaintFill(color='#FF601020'))))), dpr=1.5)
    # Gradients change paint only: the same glyph geometry and clocks as solid outlines.
    line = dict(ir['skia_layout']['lines'][0])
    line.pop('body_gradient_bounds')
    assert line == solid['skia_layout']['lines'][0]
    assert ir['track']['lines'][0]['resolved_intervals'] == solid['track']['lines'][0]['resolved_intervals']
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, [*TIMES, 750, 0])
        configure(process, solid)
        plain, _ = collect(process, TIMES, generation=2)
    assert np.array_equal(frames[3], frames[-2]) and np.array_equal(frames[0], frames[-1])
    for t, frame, reference in zip(TIMES, frames[:len(TIMES)], plain, strict=True):
        # Opaque shader and solid paint paths can round AA coverage differently.
        # Compare the alpha values themselves so one LSB at an edge is permitted
        # while shifted or missing outline pixels still fail.
        if case != 'translucent':
            delta = np.abs(frame[..., 3].astype(np.int16) - reference[..., 3].astype(np.int16))
            assert delta.max() <= 1, (case, t, int(delta.max()), int((delta > 1).sum()))
    assert not np.array_equal(frames[1], plain[1])
    if case == 'all' and target == 'gpu':
        (tmp_path / 'outline-gradient-layout.json').write_text(json.dumps({
            'features': features, 'line': ir['skia_layout']['lines'][0],
        }, sort_keys=True), encoding='utf-8')


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('case,dpr', [('primary', 1.5), ('secondary', 1.5), ('body_padded', 1.5), ('body_padded', 1),
                                      ('all', 1.5), ('all', 1), ('translucent', 1.5)])
def test_outline_gradient_matches_actual_d2d_and_gpu_raster(binary, project, case, dpr):
    from test_subtitle_render_gpu_backend import _render_g1_frames

    track, style = outline_gradient_source(project, case)
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
    assert_text_parity(refs, results, (case, dpr))


@pytest.mark.parametrize('mutation', ['bounds'])
def test_invalid_outline_gradient_keeps_previous_scene(binary, project, mutation):
    ir = ir_for(binary, outline_gradient_source(project, 'all'))
    bad = copy.deepcopy(ir)
    if mutation == 'feature':
        bad['required_features'].remove('p06_outline_gradient')
    elif mutation == 'body_feature':
        bad['required_features'].remove('p06_body_gradient')
    else:
        del bad['skia_layout']['lines'][0]['body_gradient_bounds']
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        configure(process, ir)
        before, _ = collect(process, [750])
        with pytest.raises(NativeRendererError):
            configure(process, bad)
        after, _ = collect(process, [750], generation=2)
    assert before[0][..., 3].any() and np.array_equal(before, after)


def test_outline_gradient_with_roles_preserves_scene_if_capability_is_missing(binary, project):
    from krok_helper.subtitle_render.domain.models import SubtitleStyleScheme
    track, style = outline_gradient_source(project, 'primary')
    style = replace(style, custom_style_schemes={'lead': SubtitleStyleScheme(font_size_px=70)})
    line = track.lines[0]
    track = replace(track, lines=[replace(line, chars=[replace(line.chars[0], role_label='lead'), *line.chars[1:]])])
    ir = ir_for(binary, (track, style))
    assert {'p06_role_body_paints', 'p06_role_outline_paints'} <= set(ir['required_features'])
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        configure(process, ir)
        frames, _ = collect(process, [600, 1500])
        replay, _ = collect(process, [1500, 600], generation=2)
    assert frames[0].any() and frames[1].any()
    np.testing.assert_array_equal(frames[0], replay[1])
    np.testing.assert_array_equal(frames[1], replay[0])

