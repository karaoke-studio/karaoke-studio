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
from test_subtitle_skia_outline_ruby import (
    annotated, mixed, pages, reading, upper_source, upper_ir, annotated_paint, render_skia, UPPER_TIMES,
)
from krok_helper.subtitle_render.domain.models import LyricsLayout, SubtitleStyleScheme
from krok_helper.subtitle_render.domain.paint import PaintFill
from krok_helper.subtitle_render.native.backend import NativeRendererError, NativeRendererProcess
from krok_helper.subtitle_render.native.skia_backend import SkiaRendererProcess
from scripts.subtitle_gpu_baseline.policy import COMPARISON_BUDGETS


_LINE_MOTIONS = [('fade', 'fade'), ('slide_in', 'slide_out'), ('rise', 'rise')]


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('case', ['solid', 'textured', 'motion', 'utopia', 'vector_guide'])
def test_inline_role_shadows_match_actual_d2d_gpu(binary, project, tmp_path, case):
    from test_subtitle_render_gpu_backend import _render_g1_frames
    from test_subtitle_skia_reverse import source
    from test_subtitle_skia_roles import colors
    from test_subtitle_skia_static_glow import assert_glow_parity

    track, style = source(project)
    line, = track.lines
    chars = [replace(c, role_label='lead') if i in (1, 2) else c for i, c in enumerate(line.chars)]
    line = replace(line, chars=chars, wipe_reverse=False)
    if case == 'vector_guide':
        from test_subtitle_skia_vector_guides import SQUARE

        line = replace(line, inline_guide_symbols={1: SQUARE})
    track = replace(track, lines=[line])
    palette = colors('#FFF4E7C0', '#FF177FDC')
    before, after = PaintFill(color='#A044AA40'), PaintFill(color='#D0E040FF')
    if case in {'textured', 'motion'}:
        from test_subtitle_skia_image_fills import image_paint, pattern, write_png

        tile = write_png(tmp_path / 'role-shadow.png', pattern('transparent'))
        before = PaintFill(mode='gradient_horizontal', start_color='#A044AA40', end_color='#C0406080')
        after = image_paint(tile, 137)
    palette = replace(palette, before=replace(palette.before, shadow=before),
                      after=replace(palette.after, shadow=after))
    role = SubtitleStyleScheme(decoration_kind='shadow', decoration_before_sync=True, karaoke_colors=palette,
                               shadow_before_offset_x=-13, shadow_before_offset_y=10,
                               shadow_offset_x=12, shadow_offset_y=13)
    style = replace(style, decoration_kind='none', custom_style_schemes={'lead': role})
    times, dpr = (250, 450, 725, 1050, 1375, 1600, 2100), 3
    if case == 'motion':
        style = replace(style, entry_anim='rise', entry_lead_ms=320, exit_anim='fade', exit_fade_ms=600)
        times = (35, 95, 180, 450, 725, 1850, 2450, 2850, 2999)
    elif case == 'utopia':
        style = replace(style, karaoke_anim='utopia')
    refs = []
    for appearance in (role, replace(role, decoration_kind='none')):
        with NativeRendererProcess() as process:
            _, raw = _render_g1_frames(process, replace(style, custom_style_schemes={'lead': appearance}),
                                       times, force_warp=False, track=track, width=640, height=360, dpr=dpr,
                                       realization_enabled=False)
        refs.append([np.frombuffer(p, np.uint8).reshape(360*dpr, 640*dpr, 4) for p in raw])
    assert any(np.count_nonzero((a[..., 3] > 16) & (b[..., 3] == 0)) > 100
               for a, b in zip(*refs, strict=True))
    if case == 'motion':
        static = replace(style, entry_anim='none', exit_anim='none')
        with NativeRendererProcess() as process:
            _, raw = _render_g1_frames(process, static, (35, 2850), force_warp=False, track=track,
                                       width=640, height=360, dpr=dpr, realization_enabled=False)
        assert all(np.count_nonzero(a != np.frombuffer(b, np.uint8).reshape(a.shape)) > 100
                   for a, b in zip((refs[0][0], refs[0][-2]), raw, strict=True))
    elif case == 'utopia':
        with NativeRendererProcess() as process:
            _, raw = _render_g1_frames(process, replace(style, karaoke_anim='none'), times, force_warp=False,
                                       track=track, width=640, height=360, dpr=dpr, realization_enabled=False)
        assert any(np.count_nonzero(a != np.frombuffer(b, np.uint8).reshape(a.shape)) > 100
                   for a, b in zip(refs[0], raw, strict=True))
    ir = ir_for(binary, (track, style), dpr=dpr)
    feature = {'solid': 'p06_role_static_shadow', 'vector_guide': 'p06_role_static_shadow', 'textured': 'p06_role_shadow_paints',
               'motion': 'p07_role_shadow_line_animation', 'utopia': 'p07_role_singing_shadow'}[case]
    assert feature in ir['required_features']
    gpu, raster = render_skia(binary, ir, times)
    assert_glow_parity(times, refs[0], gpu, raster, ('inline_role_shadows', case), check_p00_edges=True)


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
def test_role_body_and_reading_shadows_match_actual_d2d_gpu(binary, reading):
    from test_subtitle_render_gpu_backend import _render_g1_frames
    from test_subtitle_skia_static_glow import assert_glow_parity

    track, style = reading
    line, = track.lines
    track = replace(track, lines=[replace(line, chars=[replace(c, role_label='lead') for c in line.chars])])
    role = SubtitleStyleScheme(decoration_kind='shadow', shadow_offset_x=10, shadow_offset_y=8,
                               ruby_decoration_kind='shadow', ruby_shadow_offset_x=-12, ruby_shadow_offset_y=-5)
    style = replace(style, decoration_kind='none', ruby_decoration_kind='none',
                    custom_style_schemes={'lead': role})
    times, dpr = (325, 475, 725, 1000, 1450, 1750, 2250), 3
    refs = []
    for look in (role, replace(role, decoration_kind='none'), replace(role, ruby_decoration_kind='none')):
        with NativeRendererProcess() as process:
            _, raw = _render_g1_frames(process, replace(style, custom_style_schemes={'lead': look}), times,
                                       force_warp=False, track=track, width=640, height=360, dpr=dpr,
                                       realization_enabled=False)
        refs.append([np.frombuffer(p, np.uint8).reshape(360*dpr, 640*dpr, 4) for p in raw])
    for control in refs[1:]:
        assert any(np.count_nonzero((a[..., 3] > 16) & (b[..., 3] == 0)) > 30
                   for a, b in zip(refs[0], control, strict=True))
    ir = upper_ir(binary, (track, style), dpr)
    assert {'p06_role_shadow_ruby', 'p06_ruby_shadow'} <= set(ir['required_features'])
    gpu, raster = render_skia(binary, ir, times)
    assert_glow_parity(times, refs[0], gpu, raster, 'role_body_and_reading_shadows', check_p00_edges=True)


def animated_shadow_style(style, effect, entry, exit_, body, ruby):
    return replace(style, right_to_left=False, stroke_width_px=4, karaoke_anim=effect, entry_anim=entry,
                   exit_anim=exit_, entry_lead_ms=600, exit_fade_ms=600, decoration_before_sync=False,
                   decoration_kind_before=body[0], decoration_kind=body[1],
                   ruby_decoration_kind_before=ruby[0], ruby_decoration_kind=ruby[1],
                   shadow_before_offset_x=-7, shadow_before_offset_y=5, shadow_offset_x=9, shadow_offset_y=6,
                   ruby_shadow_before_offset_x=-5, ruby_shadow_before_offset_y=3,
                   ruby_shadow_offset_x=4, ruby_shadow_offset_y=3,
                   glow_before_radius_px=6, glow_after_radius_px=6, ruby_glow_radius_px=4)


@pytest.mark.parametrize('target', ['gpu', 'raster'])
@pytest.mark.parametrize('effect', ['none', 'no_wipe'])
@pytest.mark.parametrize('entry,exit_', _LINE_MOTIONS)
def test_horizontal_shadow_motion_keeps_scene_and_replays(binary, reading, target, effect, entry, exit_):
    track, style = reading
    style = animated_shadow_style(style, effect, entry, exit_, ('glow', 'shadow'), ('shadow', 'glow'))
    ir = ir_for(binary, (track, style), dpr=1.5)
    feature = 'p07_horizontal_shadow_line_animation'
    assert feature in ir['required_features']
    line, = ir['track']['lines']
    start, end = line['display_start_ms'], line['display_end_ms']
    times = [start + 4, start + 150, start + 650, end - 150, end - 48, end - 4]
    bad = copy.deepcopy(ir)
    bad['required_features'].remove(feature)
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, times)
        replay, _ = collect(process, times[::-1], generation=2)
    alpha = [int(frame[..., 3].sum()) for frame in frames]
    assert 0 < alpha[0] < alpha[1] < alpha[2]
    assert alpha[-1] == 0 < alpha[-2] < alpha[-3] < alpha[2]
    assert all(np.array_equal(frame, repeated) for frame, repeated in zip(frames, replay[::-1], strict=True))


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('entry,exit_', _LINE_MOTIONS)
@pytest.mark.parametrize('effect', ['none', 'no_wipe'])
@pytest.mark.parametrize('layout,body,ruby', [
    ('body', ('shadow', 'shadow'), ('none', 'none')),
    ('ruby', ('none', 'none'), ('shadow', 'shadow')),
    ('both', ('shadow', 'shadow'), ('shadow', 'shadow')),
    ('both', ('glow', 'shadow'), ('shadow', 'glow')),
])
def test_horizontal_shadow_motion_matches_actual_gpu(binary, application, tmp_path, layout, body, ruby,
                                                     effect, entry, exit_):
    from scripts.subtitle_gpu_baseline.cases import build_cases
    from test_subtitle_skia_character_fade import assert_ruby_transition_gpu_parity

    case = next(case for case in build_cases(tmp_path) if case.case_id == 'rtl')
    track = replace(case.track, rubies=[]) if layout == 'body' else case.track
    style = animated_shadow_style(case.style, effect, entry, exit_, body, ruby)
    times = (299, 301, 307, 317, 336, 375, 500, 750, 899, 900, 1500, 3950,
             4500, 4900, 5100, 5350, 5464, 5483, 5493, 5499, 5500)
    assert_ruby_transition_gpu_parity(binary, replace(case, track=track, style=style), times)


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


@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_multitrack_shadows_stay_with_their_source(binary, reading, target):
    track, style = shaded_ruby(reading, True, True)
    style = replace(style, line_y_position='bottom',
                    layouts=[LyricsLayout(layout_id='top', line_y_position='top', line_y_margin_px=24)])
    side = replace(track, lines=[replace(track.lines[0], layout_index=1)],
                   meta=replace(track.meta, offset_ms=1000))
    ir = ir_for(binary, (track, style), extra_tracks=[side])
    assert all({'p06_static_shadow', 'p06_ruby_shadow'} <= set(source['required_features'])
               for source in (ir, *ir['skia_extra_sources']))
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, [500, 1800, 5300, 6100, 500])
    top = np.stack(frames)[:, :180, :, 3]
    bottom = np.stack(frames)[:, 180:, :, 3]
    assert not top[0].any() and bottom[0].any()
    assert top[1].any() and bottom[1].any()
    assert top[2].any() and not bottom[2].any()
    assert not frames[3].any() and np.array_equal(frames[0], frames[-1])


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
def test_multitrack_shadows_match_actual_d2d_gpu(binary, application, tmp_path):
    from scripts.subtitle_gpu_baseline.cases import build_cases
    from test_subtitle_skia_character_fade import assert_ruby_transition_gpu_parity

    base = next(case for case in build_cases(tmp_path) if case.case_id == 'shadow')
    style = replace(base.style, line_y_position='bottom',
                    layouts=[LyricsLayout(layout_id='top', line_y_position='top', line_y_margin_px=160)])
    side = replace(base.track, lines=[replace(base.track.lines[0], layout_index=1)],
                   meta=replace(base.track.meta, offset_ms=900))
    case = replace(base, style=style, case_id='multitrack_shadows')
    assert_ruby_transition_gpu_parity(binary, case, (299, 600, 1199, 1200, 1500, 2600, 4400, 5500, 6399, 6400),
                                      extra_tracks=[side])


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


@pytest.mark.parametrize('target', ['gpu', 'raster'])
@pytest.mark.parametrize('before_kind,after_kind', [('shadow', 'none'), ('none', 'shadow'),
                                                  ('shadow', 'shadow'), ('shadow', 'glow')])
@pytest.mark.parametrize('reverse', [False, True])
def test_vertical_static_shadow_respects_y_wipe_and_screen_offsets(binary, project, target, before_kind, after_kind, reverse):
    from test_subtitle_skia_roles import colors
    from test_subtitle_skia_vertical import vertical_source

    track, style = vertical_source(project, texts=['字'], position='top')
    if reverse:
        track = replace(track, lines=[replace(track.lines[0], wipe_reverse=True)])
    palette = colors('#00000000', '#00000000')
    palette = replace(palette, before=replace(palette.before, shadow=PaintFill(color='#FFFF0000')),
                      after=replace(palette.after, shadow=PaintFill(color='#FF0000FF')))
    style = replace(style, karaoke_colors=palette, decoration_before_sync=False,
                    decoration_kind_before=before_kind, decoration_kind=after_kind,
                    shadow_before_offset_x=-12, shadow_before_offset_y=4,
                    shadow_offset_x=12, shadow_offset_y=7, glow_after_radius_px=6)
    ir = ir_for(binary, (track, style))
    feature = 'p06_vertical_reverse_shadow' if reverse else 'p06_vertical_shadow'
    assert feature in ir['required_features']
    assert ('p06_vertical_glow' in ir['required_features']) == (after_kind == 'glow')
    (start, end), = ir['track']['lines'][0]['resolved_intervals']
    times = [start - 1, (start + end) // 2, end + 1]
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, times)
        bad = copy.deepcopy(ir)
        bad['required_features'].remove(feature)
        replay, _ = collect(process, [times[1]], generation=2)
    assert np.array_equal(frames[1], replay[0])

    def weights(frame, channel, axis):
        return (frame[..., channel].astype(float) * frame[..., 3]).sum(axis=axis)

    def center(row_weights):
        assert row_weights.sum() > 0
        return np.average(np.arange(row_weights.size), weights=row_weights)

    early = weights(frames[0], 0, 1)
    direction = -1 if reverse else 1
    assert bool(early.any()) == (before_kind == 'shadow')
    if after_kind == 'none':
        middle = weights(frames[1], 0, 1)
        assert 0 < middle.sum() < early.sum()
        assert direction * (center(middle) - center(early)) > 0
        assert not frames[2].any()
    else:
        middle, late = (weights(frame, 2, 1) for frame in frames[1:])
        assert 0 < middle.sum() < late.sum()
        assert direction * (center(late) - center(middle)) > 3
    if after_kind == 'shadow' and before_kind == 'shadow':
        assert center(weights(frames[2], 2, 0)) - center(weights(frames[0], 0, 0)) == pytest.approx(24, abs=1)
    if target == 'gpu':
        with SkiaRendererProcess(binary, selection=selection('raster')) as process:
            configure(process, ir)
            raster, _ = collect(process, times)
        diff = abs(premultiplied(np.stack(frames)) - premultiplied(np.stack(raster)))
        assert diff.mean() < .15 and np.mean(diff.max(axis=-1) > 16) < .003


@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_reverse_vertical_shadow_starts_on_the_bottom_cell(binary, project, target):
    from test_subtitle_skia_roles import colors
    from test_subtitle_skia_vertical import vertical_source

    track, style = vertical_source(project, texts=['字', '字'], position='top')
    track = replace(track, lines=[replace(track.lines[0], wipe_reverse=True)])
    blank = colors('#00000000', '#00000000')
    palette = replace(blank, after=replace(blank.after, shadow=PaintFill(color='#FF0000FF')))
    style = replace(style, karaoke_colors=palette, decoration_before_sync=False,
                    decoration_kind_before='none', decoration_kind='shadow', shadow_offset_x=12, shadow_offset_y=7)
    ir = ir_for(binary, (track, style))
    intervals = ir['track']['lines'][0]['resolved_intervals']
    start, end = min(intervals)
    times = [start - 1, (start + end) // 2, max(end for _, end in intervals) + 1]
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, times)
    assert not frames[0].any()
    row_masks = [(frame[..., 3] > 16).any(axis=1) for frame in frames[1:]]
    assert row_masks[0].any() and row_masks[1].any()
    assert np.flatnonzero(row_masks[0]).min() > np.flatnonzero(row_masks[1]).mean()


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


def shaded_ruby(reading, utopia, separate):
    track, style = upper_source(reading, 2)
    style = annotated_paint(style, 2, True)
    return track, replace(style, decoration_kind='shadow', shadow_offset_x=11, shadow_offset_y=6,
                          ruby_decoration_kind='shadow' if separate else None,
                          ruby_shadow_offset_x=-9 if separate else None,
                          ruby_shadow_offset_y=2 if separate else None,
                          karaoke_anim='utopia' if utopia else 'none')


def gradient_shadow_source(reading):
    track, style = shaded_ruby(reading, False, True)
    horizontal = PaintFill(mode='gradient_horizontal', start_color='#D0EC3010', end_color='#A05030D0')
    vertical = PaintFill(mode='gradient_vertical', start_color='#A000DD40', end_color='#EF2030C0')

    def shaded(palette, before, after):
        return replace(palette, before=replace(palette.before, shadow=before),
                       after=replace(palette.after, shadow=after))

    body, ruby = style.karaoke_colors, style.ruby_karaoke_colors
    style = replace(style, decoration_before_sync=True,
                    karaoke_colors=shaded(body, horizontal, vertical),
                    ruby_karaoke_colors=shaded(ruby, vertical, horizontal),
                    ruby_horizontal_gradient_with_main=True)
    return track, style, (
        replace(style, karaoke_colors=body), replace(style, ruby_karaoke_colors=ruby),
    )


def image_shadow_source(reading, tmp_path):
    from test_subtitle_skia_image_fills import image_paint, pattern, write_png

    track, style = shaded_ruby(reading, False, True)
    tile = write_png(tmp_path / 'shadow.png', pattern('transparent'))
    body = style.karaoke_colors
    ruby = style.ruby_karaoke_colors
    style = replace(style, decoration_before_sync=True, shadow_offset_x=20, shadow_offset_y=11,
                    ruby_shadow_offset_x=-20, ruby_shadow_offset_y=-9,
                    karaoke_colors=replace(body, before=replace(body.before, shadow=image_paint(tile, 137))),
                    ruby_karaoke_colors=replace(ruby, after=replace(ruby.after, shadow=image_paint(tile, 103))))
    return track, style, (replace(style, karaoke_colors=body), replace(style, ruby_karaoke_colors=ruby))


def test_image_shadows_require_corresponding_capabilities(binary, reading, tmp_path):
    track, style, _ = image_shadow_source(reading, tmp_path)
    ir = upper_ir(binary, (track, style), 1)
    times = [450, 1100, 2300]
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        configure(process, ir)
        before, _ = collect(process, times)
        for feature in ('p06_body_image_shadow', 'p06_ruby_image_shadow'):
            forged = copy.deepcopy(ir)
            forged['required_features'].remove(feature)
        replay, _ = collect(process, times[::-1], generation=2)
    assert all(frame.any() for frame in before) and np.array_equal(before, replay[::-1])


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
def test_image_shadows_match_actual_d2d_gpu(binary, reading, tmp_path):
    from test_subtitle_render_gpu_backend import _render_g1_frames
    from test_subtitle_skia_static_glow import assert_glow_parity

    track, style, controls = image_shadow_source(reading, tmp_path)
    times, dpr = (275, 450, 550, 900, 1150, 1550, 2300), 3
    refs = []
    for look in (style, *controls):
        with NativeRendererProcess() as process:
            _, raw = _render_g1_frames(process, look, times, force_warp=False, track=track,
                                       width=640, height=360, dpr=dpr, realization_enabled=False)
        refs.append([np.frombuffer(p, np.uint8).reshape(360*dpr, 640*dpr, 4) for p in raw])
    for control in refs[1:]:
        assert any(np.count_nonzero(a != b) > 100 for a, b in zip(refs[0], control, strict=True))
    gpu, raster = render_skia(binary, upper_ir(binary, (track, style), dpr), times)
    assert_glow_parity(times, refs[0], gpu, raster, 'image_shadows', check_p00_edges=True)


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
def test_multitrack_image_shadows_match_actual_d2d_gpu(binary, reading, tmp_path):
    from test_subtitle_render_gpu_backend import _render_g1_frames
    from test_subtitle_skia_static_glow import assert_glow_parity

    track, style, controls = image_shadow_source(reading, tmp_path)
    other = replace(track, meta=replace(track.meta, offset_ms=620))
    times, dpr = (275, 450, 900, 1150, 1550, 2300, 2850), 3
    viewport = dict(width=640, height=360, dpr=dpr)
    refs = []
    for look in (style, *controls):
        with NativeRendererProcess() as process:
            _, raw = _render_g1_frames(process, look, times, force_warp=False, track=track,
                                       extra_tracks=[other], realization_enabled=False, **viewport)
        refs.append([np.frombuffer(frame, np.uint8).reshape(360*dpr, 640*dpr, 4) for frame in raw])
    for control in refs[1:]:
        assert any(np.count_nonzero(a != b) > 100 for a, b in zip(refs[0], control, strict=True))
    ir = ir_for(binary, (track, style), dpr=dpr, extra_tracks=[other])
    features = {'p06_body_image_shadow', 'p06_ruby_image_shadow'}
    assert all(features <= set(source['required_features']) for source in (ir, *ir['skia_extra_sources']))
    gpu, raster = render_skia(binary, ir, times)
    assert_glow_parity(times, refs[0], gpu, raster, 'multitrack_image_shadows', check_p00_edges=True)


def test_vector_guide_uses_body_gradient_and_image_shadows(binary, project, tmp_path):
    from test_subtitle_skia_image_fills import image_paint, pattern, write_png
    from test_subtitle_skia_static_glow import assert_glow_parity
    from test_subtitle_skia_vector_guides import CURVES

    track, style = shadow_source(project, 'transparent')
    line, = track.lines
    track = replace(track, lines=[replace(line, inline_guide_symbols={1: CURVES})])
    tile = write_png(tmp_path / 'guide-shadow.png', pattern('transparent'))
    ramp = PaintFill(mode='gradient_horizontal', start_color='#A044AA40', end_color='#C0406080')
    palette = style.karaoke_colors
    style = replace(style, karaoke_colors=replace(
        palette, before=replace(palette.before, shadow=ramp),
        after=replace(palette.after, shadow=image_paint(tile, 137))))
    times, dpr = (250, 550, 900, 1150, 1550), 3
    ir = ir_for(binary, (track, style), dpr=dpr)
    assert {'p06_body_gradient_shadow', 'p06_body_image_shadow', 'p05_vector_guide_composition'} <= set(
        ir['required_features'])
    reference = None
    if sys.platform == 'win32':
        from test_subtitle_render_gpu_backend import _render_g1_frames

        with NativeRendererProcess() as process:
            _, raw = _render_g1_frames(process, style, times, force_warp=False, track=track,
                                       width=640, height=360, dpr=dpr, realization_enabled=False)
        reference = [np.frombuffer(frame, np.uint8).reshape(360*dpr, 640*dpr, 4) for frame in raw]
    gpu, raster = render_skia(binary, ir, times)
    assert_glow_parity(times, reference, gpu, raster, 'vector_guide_painted_shadows', check_p00_edges=True)


def test_gradient_shadows_require_corresponding_capabilities(binary, reading):
    track, style, _ = gradient_shadow_source(reading)
    ir = upper_ir(binary, (track, style), 1)
    times = [550, 1100, 2300]
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        configure(process, ir)
        initial, _ = collect(process, times)
        for feature in ('p06_body_gradient_shadow', 'p06_ruby_gradient_shadow'):
            bad = copy.deepcopy(ir)
            bad['required_features'].remove(feature)
        replay, _ = collect(process, times[::-1], generation=2)
    assert all(frame.any() for frame in initial) and np.array_equal(initial, replay[::-1])


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
def test_gradient_shadows_match_actual_d2d_gpu(binary, reading):
    from test_subtitle_render_gpu_backend import _render_g1_frames
    from test_subtitle_skia_static_glow import assert_glow_parity

    track, style, solid_controls = gradient_shadow_source(reading)
    times = (450, 550, 1100, 1550, 2300)
    references = []
    for look in (style, *solid_controls):
        with NativeRendererProcess() as process:
            _, raw = _render_g1_frames(process, look, times, force_warp=False, track=track,
                                       width=640, height=360, realization_enabled=False)
        references.append([np.frombuffer(frame, np.uint8).reshape(360, 640, 4) for frame in raw])
    reference, *controls = references
    for control in controls:
        assert any(np.count_nonzero(a[..., :3] != b[..., :3]) > 100 for a, b in zip(reference, control, strict=True))
    gpu, raster = render_skia(binary, upper_ir(binary, (track, style), 1), times)
    assert_glow_parity(times, reference, gpu, raster, 'gradient_shadows', check_p00_edges=True)


@pytest.mark.parametrize('target', ['gpu', 'raster'])
@pytest.mark.parametrize('utopia', [False, True])
def test_ruby_shadow_keeps_clocks_and_seeks(binary, reading, target, utopia, tmp_path):
    track, style = shaded_ruby(reading, utopia, False)
    ir = upper_ir(binary, (track, style), 1.5)
    plain = upper_ir(binary, (track, replace(style, decoration_kind='none')), 1.5)
    assert {'p06_static_shadow', 'p06_ruby_shadow'} <= set(ir['required_features'])
    assert ir['skia_layout']['lines'] == plain['skia_layout']['lines']
    bad = copy.deepcopy(ir)
    bad['required_features'].remove('p06_ruby_shadow')
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, [299, 550, 2300, 550])
        again, _ = collect(process, [550], generation=2)
        configure(process, plain)
        still, _ = collect(process, [299, 550, 2300], generation=3)
    assert np.array_equal(frames[1], frames[-1]) and np.array_equal(frames[1], again[0])
    assert all(np.count_nonzero(a[..., 3] > 16) > np.count_nonzero(b[..., 3] > 16)
               for a, b in zip(frames[:3], still, strict=True))
    if target == 'gpu' and utopia:
        (tmp_path / 'ruby-shadow-layout.json').write_text(json.dumps({
            'features': ir['required_features'], 'line': ir['skia_layout']['lines'][0],
        }, sort_keys=True), encoding='utf-8')


def test_faded_gradient_preserves_body_and_annotation_shadows(binary, reading):
    track, style = shaded_ruby(reading, utopia=True, separate=True)
    track = replace(track, lines=[replace(track.lines[0], display_end_override_ms=2700)])
    palette = style.karaoke_colors
    fill = PaintFill(mode='gradient_vertical', start_color='#FFA08010', end_color='#FFFAC020')
    style = replace(style, karaoke_colors=replace(palette, before=replace(palette.before, text=fill)),
                    entry_anim='fade', exit_anim='fade', entry_lead_ms=600, exit_fade_ms=600)
    ir = upper_ir(binary, (track, style), 1)
    plain_style = replace(style, decoration_kind='none', ruby_decoration_kind='none')
    plain = upper_ir(binary, (track, plain_style), 1)
    assert {'p06_animated_singing_gradient_fill', 'p06_static_shadow', 'p06_ruby_shadow'} <= set(ir['required_features'])
    assert ir['skia_layout']['lines'] == plain['skia_layout']['lines']
    line, = ir['track']['lines']
    times = [line['display_start_ms'] + 150, 850, line['display_end_ms'] - line['exit_duration_ms'] // 2]
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        configure(process, ir)
        frames, _ = collect(process, [*times, times[0]])
        configure(process, plain)
        bare, _ = collect(process, times, generation=2)
    assert np.array_equal(frames[0], frames[-1])
    for t, frame, no_shadow in zip(times, frames[:3], bare, strict=True):
        assert np.count_nonzero(frame[..., 3] > 16) > np.count_nonzero(no_shadow[..., 3] > 16), t


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
def test_faded_utopia_gradient_without_readings_matches_actual_gpu(binary, application, tmp_path):
    from scripts.subtitle_gpu_baseline.cases import build_cases
    from test_subtitle_skia_character_fade import assert_ruby_transition_gpu_parity

    case = next(case for case in build_cases(tmp_path) if case.case_id == 'paint_gradient_vertical')
    track = replace(case.track, rubies=[])
    style = replace(case.style, entry_anim='fade', exit_anim='fade', karaoke_anim='utopia')
    ir = upper_ir(binary, (track, style), 1)
    assert 'p06_animated_singing_gradient_fill' in ir['required_features']
    assert 'p06_ruby_main_paint' not in ir['required_features']
    assert_ruby_transition_gpu_parity(binary, replace(case, track=track, style=style),
                                     (300, 450, 650, 1500, 2600, 3700, 5100, 5300, 5500))


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('utopia,separate,dpr', [(False, False, 1), (False, True, 1.5), (True, False, 1), (True, True, 1.5)])
def test_ruby_shadow_matches_actual_d2d_and_gpu_raster(binary, reading, utopia, separate, dpr):
    from test_subtitle_render_gpu_backend import _render_g1_frames

    track, style = shaded_ruby(reading, utopia, separate)
    with NativeRendererProcess() as process:
        _, raw = _render_g1_frames(process, style, tuple(UPPER_TIMES), force_warp=False, track=track,
                                   width=640, height=360, dpr=dpr, realization_enabled=False)
    refs = [np.frombuffer(p, np.uint8).reshape(round(360*dpr), round(640*dpr), 4) for p in raw]
    gpu, raster = render_skia(binary, upper_ir(binary, (track, style), dpr), UPPER_TIMES)
    budget = COMPARISON_BUDGETS['image_categories']['decorated_text']
    for t, ref, gpu_frame, raster_frame in zip(UPPER_TIMES, refs, gpu, raster, strict=True):
        for label, a, b in (('D2D', ref, gpu_frame), ('Raster', gpu_frame, raster_frame)):
            left, right = a[..., 3] > 16, b[..., 3] > 16
            union = left | right
            assert union.any()
            where = (utopia, separate, dpr, label, t)
            assert within_one_pixel(left, right), (*where, 'outline')
            iou = (left & right).sum() / union.sum()
            assert iou >= budget['min_alpha_iou'], (*where, iou)
            diff = abs(premultiplied(a)[union] - premultiplied(b)[union])
            assert diff.mean() <= budget['max_union_channel_mae'], (*where, diff.mean())
            assert np.percentile(diff, 95) <= budget['max_union_p95'], where
