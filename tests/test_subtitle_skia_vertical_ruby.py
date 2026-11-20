"""Multi-unit vertical reading geometry and its authored clocks."""
from dataclasses import replace
import copy
import json

import numpy as np
import pytest
from PyQt6.QtGui import QFontDatabase

from test_subtitle_skia_vertical import GPU, application, binary, project, vertical_ruby_source
from test_subtitle_skia_rtl_flow import collect, configure, ir_for, selection, kana
from krok_helper.subtitle_render.native.backend import NativeRendererError, NativeRendererProcess
from krok_helper.subtitle_render.native.skia_backend import SkiaRendererProcess


def with_pause(track):
    return replace(track, rubies=[replace(track.rubies[0], reading_parts=['字', '', '漢'],
                                           reading_part_ms=[200, 500])])


def test_vertical_ruby_units_split_one_span_and_keep_the_pause(binary, project, tmp_path):
    track, style = vertical_ruby_source(project, span=(1, 3), reading='字한')
    ruby = replace(track.rubies[0], reading_parts=['字', '', '한'], reading_part_ms=[200, 500])
    ir = ir_for(binary, (replace(track, rubies=[ruby]), style), dpr=1.5)
    layout = ir['skia_layout']
    assert layout['vertical_ruby_schema'] == 2
    line = layout['lines'][0]
    units = line['ruby_groups'][0]['units']
    assert [u['text'] for u in units] == ['字', '한']
    assert [u['interval_ms'] for u in units] == [[600, 800], [1100, 1400]]
    assert units[1]['baseline_y'] - units[0]['baseline_y'] == pytest.approx(50)
    for unit in units:
        _, top, _, bottom = unit['metrics']['ink']
        assert unit['wipe_top'] == pytest.approx(unit['baseline_y'] + top)
        assert unit['wipe_bottom'] == pytest.approx(unit['baseline_y'] + bottom)
    (tmp_path / 'vertical-multi-ruby-layout.json').write_text(json.dumps({
        'schema': layout['vertical_ruby_schema'],
        'line': line,
        'intervals': ir['track']['lines'][0]['resolved_intervals'],
    }, sort_keys=True), encoding='utf-8')


def test_previous_vertical_ruby_schema_rejects_multi_unit_groups(binary, project):
    ir = ir_for(binary, vertical_ruby_source(project, span=(0, 1), reading='字한'))
    bad = copy.deepcopy(ir)
    bad['skia_layout']['vertical_ruby_schema'] = 1
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        configure(process, ir)
        before, _ = collect(process, [399])
        with pytest.raises(NativeRendererError):
            configure(process, bad)
        after, _ = collect(process, [399], generation=2)
    assert np.array_equal(before[0], after[0])


@pytest.mark.parametrize('target', ['gpu', 'raster'])
@pytest.mark.parametrize('text, factor', [('ゃ', .10), ('、', .28), ('。', .28)])
def test_vertical_ruby_shifted_japanese_glyphs_keep_their_clock(binary, project, kana, target, text, factor):
    from krok_helper.subtitle_render.engine.ruby.style_semantics import font_style_for_ruby_cell
    from krok_helper.subtitle_render.engine.text.skia_width import cell_geometry
    from test_subtitle_skia_outline_ruby import premultiplied

    track, style = vertical_ruby_source(project, reading=text, family=kana)
    plain_track = replace(track, rubies=[replace(track.rubies[0], reading='や')])
    plain = ir_for(binary, (plain_track, style))
    ir = ir_for(binary, (track, style))
    feature = 'p05_vertical_shifted_ruby'
    assert feature in ir['required_features'] and feature not in plain['required_features']
    assert ir['skia_layout']['vertical_ruby_shift_schema'] == 1
    line, = ir['skia_layout']['lines']
    old_line, = plain['skia_layout']['lines']
    unit, = line['ruby_groups'][0]['units']
    normal, = old_line['ruby_groups'][0]['units']
    assert line['cells'] == old_line['cells'] and unit['interval_ms'] == normal['interval_ms']
    assert unit['text'] == text

    def column_cell_center(unit):
        slot, _ = font_style_for_ruby_cell(style, unit['text'])
        width, offset = cell_geometry(unit['text'], unit['metrics'], slot, edge_size=0)
        return unit['origin_x'] - offset + width / 2

    font_height = normal['metrics']['ascent'] + normal['metrics']['descent']
    assert column_cell_center(unit) - column_cell_center(normal) == pytest.approx(factor * style.ruby_font_size_px)
    assert normal['baseline_y'] - unit['baseline_y'] == pytest.approx(factor * font_height)
    assert unit['wipe_top'] == pytest.approx(unit['baseline_y'] + unit['metrics']['ink'][1])
    assert unit['wipe_bottom'] == pytest.approx(unit['baseline_y'] + unit['metrics']['ink'][3])
    start, end = unit['interval_ms']
    times = [start - 1, (start + end) // 2, end + 1]
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, times)
        bad = copy.deepcopy(ir)
        bad['required_features'].remove(feature)
        with pytest.raises(NativeRendererError):
            configure(process, bad)
        replay, _ = collect(process, [times[1]], generation=2)
    assert replay[0].any() and np.array_equal(frames[1], replay[0])
    x0, x1 = int(unit['wipe_left']), int(unit['wipe_right']) + 1
    y0, y1 = int(unit['wipe_top']), int(unit['wipe_bottom']) + 1
    before, _, after = (frame[y0:y1, x0:x1] for frame in frames)
    assert np.any((before[..., 2] > 200) & (before[..., 0] < 50) & (before[..., 3] > 16))
    assert np.any((after[..., 2] > 200) & (after[..., 0] > 200) & (after[..., 3] > 16))
    if target == 'gpu':
        with SkiaRendererProcess(binary, selection=selection('raster')) as process:
            configure(process, ir)
            raster, _ = collect(process, times)
        for gpu, cpu in zip(frames, raster, strict=True):
            difference = abs(premultiplied(gpu) - premultiplied(cpu))
            assert difference.mean() < .15 and np.mean(difference.max(axis=-1) > 16) < .003


@pytest.mark.parametrize('target', ['gpu', 'raster'])
@pytest.mark.parametrize('text,dpr', [('「', 1.5), ('」', 1), ('ー', 1.5), ('〜', 1)])
def test_vertical_ruby_rotated_punctuation_wipes_over_rotated_ink(binary, project, kana, target, text, dpr):
    from test_subtitle_skia_outline_ruby import premultiplied

    source = vertical_ruby_source(project, reading=text, family=kana)
    ir = ir_for(binary, source, dpr=dpr)
    feature = 'p05_vertical_rotated_ruby'
    assert feature in ir['required_features']
    assert ir['skia_layout']['vertical_ruby_rotation_schema'] == 1
    unit, = ir['skia_layout']['lines'][0]['ruby_groups'][0]['units']
    assert unit['text'] == text
    px, py = unit['vertical_rotation_pivot']
    left, top, right, bottom = unit['metrics']['ink']
    expected = np.array([px + py - unit['baseline_y'] - bottom, py + unit['origin_x'] + left - px,
                         px + py - unit['baseline_y'] - top, py + unit['origin_x'] + right - px])
    assert [unit['wipe_top'], unit['wipe_bottom']] == pytest.approx(expected[[1, 3]])

    def mask(frame, sung):
        red = frame[..., 0] > 200 if sung else frame[..., 0] < 50
        return red & (frame[..., 1] < 50) & (frame[..., 2] > 200) & (frame[..., 3] > 127)

    start, end = unit['interval_ms']
    times = [start - 1, (start + end) // 2, end + 1]
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, times)
        for change in ('feature', 'pivot', 'clip'):
            bad = copy.deepcopy(ir)
            mutated = bad['skia_layout']['lines'][0]['ruby_groups'][0]['units'][0]
            if change == 'feature':
                bad['required_features'].remove(feature)
            elif change == 'pivot':
                mutated['vertical_rotation_pivot'][0] = float('nan')
            else:
                mutated['wipe_top'] += .25
            with pytest.raises(NativeRendererError):
                configure(process, bad)
        replay, _ = collect(process, [times[1]], generation=2)
    assert np.max(abs(ink_bounds(mask(frames[0], False)) - expected * dpr)) <= 1.5
    sung, unsung = mask(frames[1], True), mask(frames[1], False)
    assert sung.any() and unsung.any() and np.nonzero(sung)[0].max() <= np.nonzero(unsung)[0].min()
    assert np.array_equal(replay[0], frames[1]) and mask(frames[2], True).any()
    if target == 'gpu':
        with SkiaRendererProcess(binary, selection=selection('raster')) as process:
            configure(process, ir)
            raster, _ = collect(process, times)
        for gpu, cpu in zip(frames, raster, strict=True):
            difference = abs(premultiplied(gpu) - premultiplied(cpu))
            assert difference.mean() < .15 and np.mean(difference.max(axis=-1) > 16) < .003


@pytest.mark.parametrize('target', ['gpu', 'raster'])
@pytest.mark.parametrize('main_outline', [False, True])
def test_vertical_ruby_outlines_follow_ink_and_keep_independent_colors(binary, project, kana, target, main_outline):
    from krok_helper.subtitle_render.domain.paint import PaintFill
    from test_subtitle_skia_outline_ruby import outlined, premultiplied

    track, style = vertical_ruby_source(project, reading='ー', family=kana)
    if main_outline:
        style = outlined(style, 2)
    ruby = style.ruby_karaoke_colors
    before = replace(ruby.before, text=PaintFill(color='#800000FF'),
                     stroke=PaintFill(color='#FFFFFFFF'), stroke2=PaintFill(color='#FFFF8000'))
    after = replace(ruby.after, text=PaintFill(color='#80FF00FF'),
                    stroke=PaintFill(color='#FFFFFFFF'), stroke2=PaintFill(color='#FFFF8000'))
    style = replace(style, ruby_stroke_width_px=3, ruby_stroke2_enabled=True, ruby_stroke2_width_px=3,
                    ruby_karaoke_colors=replace(ruby, before=before, after=after))
    ir = ir_for(binary, (track, style), dpr=1.5)
    feature = 'p06_vertical_ruby_outline'
    assert {feature, 'p06_ruby_solid_outline'} <= set(ir['required_features'])
    assert ('p06_vertical_outline' in ir['required_features']) == main_outline
    group, = ir['skia_layout']['lines'][0]['ruby_groups']
    assert [group['outlines'][layer]['width'] for layer in ('stroke', 'stroke2')] == [3, 3]
    unit, = group['units']
    start, end = unit['interval_ms']
    times = [start - 1, (start + end) // 2, end + 1]
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, times)
        bad = copy.deepcopy(ir)
        bad['required_features'].remove(feature)
        with pytest.raises(NativeRendererError):
            configure(process, bad)
        replay, _ = collect(process, times[::-1], generation=2)
    assert np.array_equal(frames[1], replay[1])

    def mask(frame, color, alpha):
        return (frame[..., :3] == color).all(axis=-1) & (frame[..., 3] == alpha)

    blue = mask(frames[0], [0, 0, 255], 128)
    y, x = np.nonzero(blue)
    assert y.size and np.ptp(y) > 4 * np.ptp(x)
    assert mask(frames[0], [255, 255, 255], 255).any()
    assert mask(frames[0], [255, 128, 0], 255).any()
    sung = np.nonzero(mask(frames[1], [255, 0, 255], 128))[0]
    unsung = np.nonzero(mask(frames[1], [0, 0, 255], 128))[0]
    assert sung.size and unsung.size and sung.max() < unsung.min()
    assert mask(frames[2], [255, 0, 255], 128).any()
    if target == 'gpu':
        with SkiaRendererProcess(binary, selection=selection('raster')) as process:
            configure(process, ir)
            raster, _ = collect(process, times)
        for gpu, cpu in zip(frames, raster, strict=True):
            difference = abs(premultiplied(gpu) - premultiplied(cpu))
            assert difference.mean() < .15 and np.mean(difference.max(axis=-1) > 16) < .003


@pytest.mark.parametrize('target', ['gpu', 'raster'])
@pytest.mark.parametrize('body_shadow', [False, True])
def test_vertical_ruby_decoration_follows_its_reading_front(binary, project, target, body_shadow):
    from krok_helper.subtitle_render.domain.paint import PaintFill
    from test_subtitle_skia_outline_ruby import premultiplied
    from test_subtitle_skia_roles import colors

    track, style = vertical_ruby_source(project, span=(1, 3))
    blank = colors('#00000000', '#00000000')
    body = replace(blank, before=replace(blank.before, shadow=PaintFill(color='#FF00FF00')),
                   after=replace(blank.after, shadow=PaintFill(color='#FF00FF00')))
    ruby = replace(blank, before=replace(blank.before, shadow=PaintFill(color='#FFFF0000')),
                   after=replace(blank.after, shadow=PaintFill(color='#FF0000FF')))
    body_kind = 'shadow' if body_shadow else 'none'
    style = replace(style, karaoke_colors=body, ruby_karaoke_colors=ruby, decoration_before_sync=False,
                    decoration_kind=body_kind, decoration_kind_before=body_kind,
                    shadow_offset_x=-18, shadow_before_offset_x=-18,
                    ruby_decoration_kind='glow', ruby_decoration_kind_before='shadow',
                    ruby_shadow_before_offset_x=0, ruby_shadow_before_offset_y=4, ruby_glow_after_radius_px=6)
    ir = ir_for(binary, (track, style))
    feature = 'p06_vertical_ruby_decoration'
    assert {feature, 'p06_ruby_shadow', 'p06_ruby_glow'} <= set(ir['required_features'])
    assert ('p06_vertical_shadow' in ir['required_features']) == body_shadow
    unit, = ir['skia_layout']['lines'][0]['ruby_groups'][0]['units']
    start, end = unit['interval_ms']
    times = [start - 1, (start + end) // 2, end + 1]
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, times)
        bad = copy.deepcopy(ir)
        bad['required_features'].remove(feature)
        with pytest.raises(NativeRendererError):
            configure(process, bad)
        replay, _ = collect(process, [times[1]], generation=2)
    assert np.array_equal(frames[1], replay[0])

    def rows(frame, channel):
        return (frame[..., channel].astype(float) * frame[..., 3]).sum(axis=1)

    def center(weights):
        assert weights.sum() > 0
        return np.average(np.arange(weights.size), weights=weights)

    assert rows(frames[0], 0).any() and not rows(frames[0], 2).any()
    assert rows(frames[2], 2).any() and not rows(frames[2], 0).any()
    assert center(rows(frames[1], 2)) + 3 < center(rows(frames[1], 0))
    assert all(bool(rows(frame, 1).any()) == body_shadow for frame in frames)
    if target == 'gpu':
        with SkiaRendererProcess(binary, selection=selection('raster')) as process:
            configure(process, ir)
            raster, _ = collect(process, times)
        diff = abs(premultiplied(np.stack(frames)) - premultiplied(np.stack(raster)))
        assert diff.mean() < .15 and np.mean(diff.max(axis=-1) > 16) < .003


@pytest.mark.parametrize('target', ['gpu', 'raster'])
@pytest.mark.parametrize('outlined', [False, True])
def test_vertical_ruby_groups_keep_independent_decoration_clocks(binary, project, target, outlined):
    from krok_helper.subtitle_render.domain.paint import PaintFill
    from test_subtitle_skia_roles import colors

    track, style = vertical_ruby_source(project, span=(0, 1))
    other, _ = vertical_ruby_source(project, span=(2, 3))
    track = replace(track, rubies=track.rubies + other.rubies)
    blank = colors('#00000000', '#00000000')
    ruby = replace(blank, before=replace(blank.before, shadow=PaintFill(color='#FF0000'),
                                         stroke=PaintFill(color='#00FF00'), stroke2=PaintFill(color='#FFFF00')),
                   after=replace(blank.after, shadow=PaintFill(color='#0000FF'),
                                 stroke=PaintFill(color='#00FF00'), stroke2=PaintFill(color='#FFFF00')))
    style = replace(style, karaoke_colors=blank, ruby_karaoke_colors=ruby, decoration_before_sync=False,
                    ruby_decoration_kind='glow', ruby_decoration_kind_before='shadow',
                    ruby_shadow_before_offset_y=3, ruby_shadow_before_offset_x=0, ruby_glow_after_radius_px=4,
                    ruby_stroke_width_px=2 if outlined else 0, ruby_stroke2_width_px=2,
                    ruby_stroke2_enabled=outlined)
    ir = ir_for(binary, (track, style))
    feature = 'p06_vertical_ruby_group_paint'
    assert {feature, 'p06_vertical_ruby_decoration'} <= set(ir['required_features'])
    assert ('p06_vertical_ruby_outline' in ir['required_features']) == outlined
    groups = ir['skia_layout']['lines'][0]['ruby_groups']
    first, second = [g['units'][0] for g in groups]
    boundary = round((first['wipe_bottom'] + second['wipe_top']) / 2)
    times = [first['interval_ms'][0] - 1, first['interval_ms'][1] + 1, second['interval_ms'][1] + 1]
    bad = copy.deepcopy(ir)
    bad['required_features'].remove(feature)
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, times)
        with pytest.raises(NativeRendererError):
            configure(process, bad)
        replay, _ = collect(process, list(reversed(times)), generation=2)
    assert all(np.array_equal(a, b) for a, b in zip(frames, reversed(replay), strict=True))
    for section, switch in [(slice(None, boundary), 1), (slice(boundary, None), 2)]:
        def blue(frame):
            pixels = frame[section]
            return ((pixels[..., 2] > 128) & (pixels[..., 3] > 8)).sum()
        assert all(blue(frame) == 0 for frame in frames[:switch])
        assert all(blue(frame) > 20 for frame in frames[switch:])


def ink_bounds(mask):
    y, x = np.nonzero(mask)
    return np.array([x.min(), y.min(), x.max(), y.max()]) if x.size else np.array([])


@GPU
@pytest.mark.parametrize('span, reading, style_name, dpr, pause, no_wipe', [
    ((0, 1), '字漢', 'legacy', 1, False, False),
    ((1, 3), '字漢字', 'n3_1074', 1.5, False, False),
    ((1, 3), '字漢', 'n3_1074', 1.5, True, False),
    ((1, 3), '字漢', 'legacy', 1, True, True),
])
def test_vertical_ruby_multiple_units_match_actual_gpu(binary, project, span, reading, style_name, dpr, pause, no_wipe):
    from test_subtitle_render_gpu_backend import _render_g1_frames

    if 'Meiryo' not in QFontDatabase.families():
        pytest.skip('Meiryo is required for the actual Direct2D ruby allowance')
    track, style = vertical_ruby_source(project, span=span, reading=reading, family='Meiryo')
    track = with_pause(track) if pause else track
    style = replace(style, layout_semantics=style_name, karaoke_anim='no_wipe' if no_wipe else 'none')
    times = (199, 350, 399, 400, 450, 599, 600, 601, 799, 800, 900, 1099, 1100, 1200, 1399, 1400, 2200)
    with NativeRendererProcess() as process:
        _, raw = _render_g1_frames(process, style, times, force_warp=False, track=track,
                                   width=640, height=360, dpr=dpr, realization_enabled=False)
    reference = [np.frombuffer(p, np.uint8).reshape(round(360*dpr), round(640*dpr), 4) for p in raw]
    ir = ir_for(binary, (track, style), dpr=dpr)
    for target in ('gpu', 'raster'):
        with SkiaRendererProcess(binary, selection=selection(target)) as process:
            configure(process, ir)
            frames, _ = collect(process, times)
        for t, expected, actual in zip(times, reference, frames, strict=True):
            for after in (False, True):
                def mask(pixels):
                    ruby = pixels[..., 2] > 127
                    return ruby & (pixels[..., 0] > 127) if after else ruby
                a, b = ink_bounds(mask(expected)), ink_bounds(mask(actual))
                if after and a.size != b.size:
                    # A subpixel front may color a single antialiasing row
                    # on one target before it crosses the mask threshold on both.
                    emerging = a if a.size else b
                    assert emerging[3] == emerging[1], (target, t, a, b)
                    continue
                assert a.size == b.size, (target, t, after, a, b)
                if a.size:
                    assert abs(a-b).max() <= 1, (target, t, after, a, b)
