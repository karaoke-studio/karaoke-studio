"""Vertical solid main text against the existing GPU."""
from dataclasses import replace
import copy
import json
import sys

import numpy as np
import pytest

from test_subtitle_skia_rtl_flow import (application, binary, project, rtl_source, reverse_source, assert_gpu_parity,
                                         collect, configure, ir_for, selection, kana)
from test_subtitle_skia_protocol import test_actual_g5_preview_uses_skia_even_when_gpu_off as run_preview
from test_subtitle_skia_protocol import test_product_export_short_mp4_uses_skia as run_mp4
from test_subtitle_skia_font_slots import mixed
from krok_helper.subtitle_render.domain.timing import RubyAnnotation
from krok_helper.subtitle_render.domain.models import LyricsLayout, TimingChar, TimingLine
from krok_helper.subtitle_render.native.backend import NativeRendererError, NativeRendererProcess
from krok_helper.subtitle_render.native.skia_backend import SkiaRendererProcess

TEXTS = ['字', '한', '字', '字']
GPU = pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')


def vertical_source(project, kind='plain', *, reverse=False, effect='none', texts=TEXTS, position='bottom'):
    if kind == 'shared':
        track, style = reverse_source(project, 'shared', effect)
        line = replace(track.lines[0], wipe_reverse=reverse)
        style = replace(style, karaoke_anim=effect, reverse_karaoke_anim=effect)
    else:
        track, style = rtl_source(project, kind, reverse=reverse, effect=effect)
        line = track.lines[0]
    line = replace(line, chars=[replace(c, text=t) for c, t in zip(line.chars, texts)])
    return replace(track, lines=[line]), replace(style, vertical=True, right_to_left=False, line_y_position=position)


@GPU
def test_vertical_body_paints_match_actual_d2d_gpu(binary, project, tmp_path):
    from krok_helper.subtitle_render.domain.paint import PaintFill
    from test_subtitle_render_gpu_backend import _render_g1_frames
    from test_subtitle_skia_image_fills import image_paint, pattern, write_png
    from test_subtitle_skia_outline_ruby import render_skia
    from test_subtitle_skia_static_glow import assert_glow_parity

    track, base = vertical_source(project, position='center')
    colors = base.karaoke_colors
    gradient = PaintFill(mode='gradient_vertical', start_color='#FFE12D20', end_color='#FF285BDE')
    tile = write_png(tmp_path / 'vertical-fill.png', pattern('transparent'))
    style = replace(base, karaoke_colors=replace(colors,
        before=replace(colors.before, text=gradient), after=replace(colors.after, text=image_paint(tile, 137))))
    times, dpr = (175, 350, 550, 750, 1100, 1550, 2200), 3
    refs = []
    for look in (style, base):
        with NativeRendererProcess() as process:
            _, raw = _render_g1_frames(process, look, times, force_warp=False, track=track,
                                       width=640, height=360, dpr=dpr, realization_enabled=False)
        refs.append([np.frombuffer(frame, np.uint8).reshape(360*dpr, 640*dpr, 4) for frame in raw])
    assert np.count_nonzero(refs[0][0] != refs[1][0]) > 100
    assert np.count_nonzero(refs[0][-1] != refs[1][-1]) > 100
    ir = ir_for(binary, (track, style), dpr=dpr)
    assert 'p06_vertical_body_paints' in ir['required_features']
    gpu, raster = render_skia(binary, ir, times)
    assert_glow_parity(times, refs[0], gpu, raster, 'vertical_body_paints', check_p00_edges=True)


@pytest.mark.parametrize('reverse', [False, True])
def test_vertical_ir_is_a_versioned_fixed_cell_column(binary, project, reverse, tmp_path):
    track, style = vertical_source(project, reverse=reverse)
    ir = ir_for(binary, (track, style))
    flat = ir_for(binary, (track, replace(style, vertical=False)))
    line = ir['skia_layout']['lines'][0]
    cells = line['cells']
    assert 'p05_vertical_solid' in ir['required_features'] and ir['skia_layout']['vertical_solid_schema'] == 1
    assert 'p05_vertical_solid' not in flat['required_features'] and line['text_flow'] == 'vertical'
    # Same authored cells, glyphs and clocks as the horizontal line.
    assert [c['metrics'] for c in cells] == [c['metrics'] for c in flat['skia_layout']['lines'][0]['cells']]
    assert ir['track']['lines'][0]['resolved_intervals'] == flat['track']['lines'][0]['resolved_intervals']
    height = cells[0]['wipe_bottom'] - cells[0]['wipe_top']
    assert height == pytest.approx(50)  # font size 50, ascent 800 + descent 200 per 1000
    assert [c['wipe_top'] for c in cells] == pytest.approx([cells[0]['wipe_top'] + i * height for i in range(4)])
    assert all(c['baseline_y'] - c['wipe_top'] == pytest.approx(40) for c in cells)
    assert all(c['wipe_end_position'] == 1 for c in cells)
    if reverse:
        assert [c['clock_index'] for c in cells] == [3, 2, 1, 0]
    else:
        (tmp_path / 'vertical-solid-layout.json').write_text(json.dumps({
            'schema': ir['skia_layout']['vertical_solid_schema'],
            'line': line,
            'intervals': ir['track']['lines'][0]['resolved_intervals'],
        }, sort_keys=True), encoding='utf-8')


@pytest.mark.parametrize('change', ['control', 'dual_alt_grid'])
def test_unvalidated_vertical_combinations_stay_gated(binary, project, change):
    track, style = vertical_source(project)
    line = track.lines[0]
    if change == 'control':
        track = replace(track, lines=[replace(line, chars=[replace(line.chars[0], text='\x01'), *line.chars[1:]])])
    elif change == 'dual_alt_grid':
        track = replace(track, lines=[replace(line, layout_index=1)])
        style = replace(style, dual_line_layout=True, line_alignments=['left', 'right'],
                        layouts=[LyricsLayout(line_gap_px=style.line_gap_px + 8)])
    with pytest.raises(ValueError):
        ir_for(binary, (track, style))


def vertical_column_offsets(project):
    track, style = vertical_source(project, position='bottom')
    line, = track.lines
    first = replace(line, track_line_index=0, end_ms=1800, display_end_override_ms=2400)
    second = replace(first, track_line_index=1, chars=[replace(c, start_ms=c.start_ms + 80) for c in first.chars])
    track = replace(track, lines=[first, second])
    style = replace(style, dual_line_layout=True, layout_semantics='n3_1074', line_gap_px=24,
                    line_alignments=['left', 'right'], line_offsets_x=[12, -9], sync_entry=False)
    return track, style


def test_vertical_column_offsets_use_authored_slots(binary, project):
    track, style = vertical_column_offsets(project)
    ir = ir_for(binary, (track, replace(style, right_to_left=True)))
    control = ir_for(binary, (track, replace(style, line_offsets_x=[0, 0])))
    assert 'p05_vertical_column_offsets' in ir['required_features']
    assert not ir['style']['right_to_left'] and 'p05_horizontal_rtl' not in ir['required_features']
    for with_offset, without in zip(ir['skia_layout']['lines'], control['skia_layout']['lines'], strict=True):
        assert with_offset['cells'][0]['origin_x'] - without['cells'][0]['origin_x'] == pytest.approx(
            style.line_offsets_x[with_offset['vertical_lane']])
    bad = copy.deepcopy(ir)
    bad['required_features'].remove('p05_vertical_column_offsets')
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        configure(process, ir)
        frames, _ = collect(process, [600])
        with pytest.raises(NativeRendererError, match='列偏移'):
            configure(process, bad)
        replay, _ = collect(process, [600], generation=2)
    assert frames[0].any() and np.array_equal(frames, replay)


@GPU
def test_vertical_column_offsets_and_ignored_rtl_match_actual_d2d_gpu(binary, project):
    from test_subtitle_render_gpu_backend import _render_g1_frames
    from test_subtitle_skia_outline_ruby import render_skia
    from test_subtitle_skia_static_glow import assert_glow_parity

    track, style = vertical_column_offsets(project)
    times = (400, 620, 950, 1200, 1660, 2050)
    looks = (replace(style, right_to_left=True), style, replace(style, line_offsets_x=[0, 0]))
    references = []
    for look in looks:
        with NativeRendererProcess() as process:
            _, pixels = _render_g1_frames(process, look, times, force_warp=False, track=track,
                                          width=640, height=360, realization_enabled=False)
        references.append([np.frombuffer(frame, np.uint8).reshape(360, 640, 4) for frame in pixels])
    assert np.array_equal(references[0], references[1])
    assert any(np.count_nonzero(a != b) > 100 for a, b in zip(references[0], references[2], strict=True))
    gpu, raster = render_skia(binary, ir_for(binary, (track, looks[0])), times)
    assert_glow_parity(times, references[0], gpu, raster, 'vertical_column_offsets', check_p00_edges=True)


def test_vertical_multiscalar_cell_preserves_clock(binary, project):
    track, style = vertical_source(project, texts=['字字', 'A', '한', '字'])
    ir = ir_for(binary, (track, style))
    assert 'p05_vertical_text_cells' in ir['required_features']
    cells = ir['skia_layout']['lines'][0]['cells']
    assert len(cells[0]['metrics']['glyphs']) == 2
    assert len(cells) == len(ir['track']['lines'][0]['resolved_intervals']) == 4
    bad = copy.deepcopy(ir)
    bad['required_features'].remove('p05_vertical_text_cells')
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        configure(process, ir)
        before, _ = collect(process, [600, 950])
        with pytest.raises(NativeRendererError, match='竖排单元'):
            configure(process, bad)
        after, _ = collect(process, [950, 600], generation=2)
    assert all(frame.any() for frame in before) and np.array_equal(before, after[::-1])


@GPU
def test_vertical_punctuation_and_multiscalar_cell_match_actual_d2d_gpu(binary, project):
    from PyQt6.QtGui import QFontDatabase

    family = 'Meiryo'
    if family not in QFontDatabase.families():
        pytest.skip('Meiryo is required for the actual Direct2D typography reference')
    track, style = vertical_source(project, texts=['→', '，', '字字', 'θ'], position='center')
    style = replace(style, font_family=family, font_family_latin=family)
    ir = ir_for(binary, (track, style))
    cells = ir['skia_layout']['lines'][0]['cells']
    assert 'rotation_pivot_x' in cells[0] and 'rotation_pivot_x' not in cells[1]
    assert_gpu_parity(binary, track, style, 1.5, 'vertical_punctuation_multiscalar')


@pytest.mark.parametrize('target', ['gpu', 'raster'])
@pytest.mark.parametrize('semantics,gap', [('legacy', -5), ('n3_1074', 20)])
def test_vertical_columns_use_page_lanes_and_switch_without_moving_the_clocks(binary, project, target, semantics, gap):
    from test_subtitle_skia_outline_ruby import premultiplied

    track, style = vertical_source(project, position='top')
    lines = []
    for i in range(4):
        start = 200 + i * 180 + (2200 if i >= 2 else 0)
        lines.append(TimingLine(chars=[TimingChar('字', start), TimingChar('字', start + 160)],
                                end_ms=start + 400, track_line_index=i, display_start_override_ms=0 if i < 2 else 2200,
                                display_end_override_ms=1600 if i < 2 else 4400))
    track = replace(track, lines=lines)
    style = replace(style, dual_line_layout=True, layout_semantics=semantics, line_gap_px=gap,
                    line_alignments=['left', 'right'], line_offsets_x=[0, 0],
                    auto_fill_section_time=False, sync_entry=False)
    ir = ir_for(binary, (track, style))
    feature = 'p05_vertical_columns'
    assert feature in ir['required_features']
    final = ir['skia_layout']['lines']
    assert [line['vertical_lane'] for line in final] == [0, 1, 0, 1]
    columns = [line['cells'][0]['origin_x'] for line in final]
    assert columns[:2] == pytest.approx(columns[2:])
    assert columns[0] - columns[1] == pytest.approx(style.font_size_px + gap)
    for projected, layout, authored in zip(ir['track']['lines'], final, lines, strict=True):
        assert projected['resolved_intervals'] == [[c.start_ms, end] for c, end in
            zip(authored.chars, [authored.chars[1].start_ms, authored.end_ms], strict=True)]
        for cell in layout['cells']:
            assert cell['wipe_top'] < cell['wipe_bottom']
        assert layout['cells'][1]['wipe_top'] == pytest.approx(layout['cells'][0]['wipe_bottom'])
    times = [550, 2000, 3150]
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, times)
        bad = copy.deepcopy(ir)
        bad['skia_layout']['lines'][1]['vertical_lane'] = 0
        with pytest.raises(NativeRendererError):
            configure(process, bad)
        replay, _ = collect(process, [times[0]], generation=2)
    assert frames[0].any() and not frames[1].any() and frames[2].any()
    assert np.array_equal(frames[0], replay[0])
    if target == 'gpu':
        with SkiaRendererProcess(binary, selection=selection('raster')) as process:
            configure(process, ir)
            raster, _ = collect(process, times)
        for gpu, cpu in zip(frames, raster, strict=True):
            difference = abs(premultiplied(gpu) - premultiplied(cpu))
            assert difference.mean() < .15 and np.mean(difference.max(axis=-1) > 16) < .003


def decorated_columns(project, kind, *, reverse=False, outline=False, mixed_directions=False):
    """Two simultaneous columns whose static decoration is isolated by a transparent body."""
    from krok_helper.subtitle_render.domain.paint import PaintFill
    from test_subtitle_skia_outline_ruby import outlined
    from test_subtitle_skia_roles import colors

    track, style = vertical_source(project, position='top')
    lines = [TimingLine(chars=[TimingChar('字', start), TimingChar('字', start + 160)], end_ms=start + 400,
                        track_line_index=i, wipe_reverse=reverse != (mixed_directions and i == 1),
                        display_start_override_ms=0, display_end_override_ms=1600)
             for i, start in enumerate((200, 380))]
    style = replace(style, dual_line_layout=True, layout_semantics='n3_1074', line_gap_px=30,
                    line_alignments=['left', 'right'], line_offsets_x=[0, 0],
                    auto_fill_section_time=False, sync_entry=False)
    palette = outlined(style, 2).karaoke_colors if outline else colors('#00000000', '#00000000')
    palette = replace(palette, before=replace(palette.before, shadow=PaintFill(color='#FFFF0000')),
                      after=replace(palette.after, shadow=PaintFill(color='#FF0000FF')))
    if outline:
        style = outlined(style, 2)
    before_kind, after_kind = {'glow': ('glow', 'glow'), 'shadow': ('shadow', 'shadow'),
                               'shadow_to_glow': ('shadow', 'glow')}[kind]
    plain = replace(style, karaoke_colors=palette)
    style = replace(plain, decoration_before_sync=False, decoration_kind_before=before_kind,
                    decoration_kind=after_kind, glow_before_radius_px=6, glow_after_radius_px=6,
                    glow_before_concentration_level=0, glow_concentration_level=0,
                    shadow_before_offset_x=-6, shadow_before_offset_y=4, shadow_offset_x=6, shadow_offset_y=4)
    return replace(track, lines=lines), style, plain


@pytest.mark.parametrize('target', ['gpu', 'raster'])
@pytest.mark.parametrize('kind', ['glow', 'shadow', 'shadow_to_glow'])
@pytest.mark.parametrize('directions', ['forward', 'reverse', 'mixed'])
@pytest.mark.parametrize('outline', [False, True])
def test_vertical_column_decoration_keeps_positions_and_column_wipe(binary, project, target, kind, directions, outline):
    from test_subtitle_skia_outline_ruby import premultiplied

    track, style, plain = decorated_columns(project, kind, reverse=directions == 'reverse', outline=outline,
                                            mixed_directions=directions == 'mixed')
    ir = ir_for(binary, (track, style))
    undecorated = ir_for(binary, (track, plain))
    feature = 'p06_vertical_columns_decoration'
    features = ir['required_features']
    assert {feature, 'p05_vertical_columns'} <= set(features)
    assert ('p06_vertical_columns_outline' in features) == outline
    assert ('p06_vertical_glow' in features) == (kind != 'shadow')
    assert ('p06_vertical_shadow' in features) == (kind != 'glow')
    assert ('p06_vertical_reverse_glow' in features) == (directions != 'forward' and kind != 'shadow')
    assert ('p06_vertical_reverse_shadow' in features) == (directions != 'forward' and kind != 'glow')
    assert ('p06_vertical_mixed_column_decoration' in features) == (directions == 'mixed')
    assert feature not in undecorated['required_features']
    # Decoration does not move the columns, the page windows or the clocks.
    assert ir['skia_layout']['lines'] == undecorated['skia_layout']['lines']
    for key in ('layout_offset_windows', 'resolved_intervals', 'display_start_ms', 'display_end_ms'):
        assert [line.get(key) for line in ir['track']['lines']] == [line.get(key) for line in undecorated['track']['lines']]
    columns = [line['cells'][0]['origin_x'] for line in ir['skia_layout']['lines']]
    assert [line['vertical_lane'] for line in ir['skia_layout']['lines']] == [0, 1]
    times = [100, 440, 1000]
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, times)
        for missing in (feature, 'p05_vertical_columns'):
            bad = copy.deepcopy(ir)
            bad['required_features'].remove(missing)
            with pytest.raises(NativeRendererError):
                configure(process, bad)
        replay, _ = collect(process, [times[1]], generation=2)
        configure(process, undecorated)
        baseline, _ = collect(process, times, generation=3)
    assert np.array_equal(frames[1], replay[0])
    assert all(not np.array_equal(a, b) for a, b in zip(frames, baseline, strict=True))
    if not outline:
        # origin_x is the cell's left edge; each window holds one column and its +-6px shadows.
        half = (columns[0] - columns[1]) / 2

        def rows(frame, channel, column):
            middle = column + style.font_size_px / 2
            area = frame[:, round(middle - half):round(middle + half)]
            return (area[..., channel].astype(float) * area[..., 3]).sum(axis=1)

        def center(weights):
            assert weights.sum() > 0
            return np.average(np.arange(weights.size), weights=weights)

        for column, line in zip(columns, track.lines, strict=True):
            direction = -1 if line.wipe_reverse else 1
            assert rows(frames[0], 0, column).any() and not rows(frames[0], 2, column).any()
            middle, late = rows(frames[1], 2, column), rows(frames[2], 2, column)
            assert 0 < middle.sum() < late.sum()
            assert direction * (center(late) - center(middle)) > 3
            if kind != 'shadow':
                assert not rows(frames[2], 0, column).any()
    if target == 'gpu':
        with SkiaRendererProcess(binary, selection=selection('raster')) as process:
            configure(process, ir)
            raster, _ = collect(process, times)
        diff = abs(premultiplied(np.stack(frames)) - premultiplied(np.stack(raster)))
        assert diff.mean() < .15 and np.mean(diff.max(axis=-1) > 16) < .003


@pytest.mark.parametrize('kind', ['glow', 'shadow', 'shadow_to_glow'])
@pytest.mark.parametrize('directions', ['forward', 'reverse', 'mixed'])
def test_vertical_columns_switch_decoration_at_their_own_unit_ends(binary, project, kind, directions):
    from test_subtitle_skia_outline_ruby import premultiplied

    track, style, _ = decorated_columns(project, kind, reverse=directions != 'forward',
                                        mixed_directions=directions == 'mixed')
    style = replace(style, karaoke_anim='no_wipe', reverse_karaoke_anim='no_wipe')
    ir = ir_for(binary, (track, style))
    feature = 'p07_vertical_columns_decoration_no_wipe'
    assert feature in ir['required_features']
    reverse = 'p07_vertical_reverse_decoration_no_wipe'
    assert (reverse in ir['required_features']) == (directions != 'forward')
    ends = [line['resolved_intervals'][0][1] for line in ir['track']['lines']]
    times = [time for end in ends for time in (end - 2, end - 1, end)]
    columns = [line['cells'][0]['origin_x'] for line in ir['skia_layout']['lines']]
    # Measure core pixels. The two columns' outer halos can meet in the gap.
    windows = [slice(round(origin + style.font_size_px / 4), round(origin + style.font_size_px * 3 / 4))
               for origin in columns]
    required = [feature] if directions == 'forward' else [feature, reverse]
    captures = []
    for target in ('gpu', 'raster'):
        with SkiaRendererProcess(binary, selection=selection(target)) as process:
            configure(process, ir)
            frames, _ = collect(process, times)
            for missing in required:
                bad = copy.deepcopy(ir)
                bad['required_features'].remove(missing)
                with pytest.raises(NativeRendererError):
                    configure(process, bad)
            replay, _ = collect(process, times[::-1], generation=2)
        assert all(np.array_equal(frame, repeated) for frame, repeated in zip(frames, replay[::-1], strict=True))
        for index, window in enumerate(windows):
            first, before, after = (frame[:, window] for frame in frames[3 * index:3 * index + 3])
            assert np.array_equal(first, before)
            blue_before, blue_after = [(frame[..., 2].astype(np.int64) * frame[..., 3]).sum()
                                       for frame in (before, after)]
            assert blue_after > blue_before
            rows = (after[..., 2].astype(float) * after[..., 3]).sum(axis=1)
            cells = ir['skia_layout']['lines'][index]['cells']
            middle = (cells[0]['wipe_top'] + cells[-1]['wipe_bottom']) / 2
            assert (np.average(np.arange(rows.size), weights=rows) > middle) == track.lines[index].wipe_reverse
            other = windows[1 - index]
            assert np.array_equal(frames[3 * index + 1][:, other], frames[3 * index + 2][:, other])
        captures.append(np.stack(frames))
    diff = abs(premultiplied(captures[0]) - premultiplied(captures[1]))
    assert diff.mean() < .15 and np.mean(diff.max(axis=-1) > 16) < .003


@GPU
def test_reverse_vertical_no_wipe_keeps_shadow_overhang_before_first_unit(binary, application, tmp_path):
    from scripts.subtitle_gpu_baseline.cases import build_cases
    from test_subtitle_skia_character_fade import assert_ruby_transition_gpu_parity

    case = next(case for case in build_cases(tmp_path) if case.case_id == 'vertical')
    track = replace(case.track, rubies=[], lines=[replace(case.track.lines[0], wipe_reverse=True)])
    style = replace(case.style, karaoke_anim='no_wipe', reverse_karaoke_anim='no_wipe',
                    decoration_before_sync=False, decoration_kind_before='shadow', decoration_kind='glow',
                    shadow_before_offset_x=9, shadow_before_offset_y=6)
    times = (300, 600, 1199, 1200, 1500, 1800, 3900, 4499, 4500)
    assert_ruby_transition_gpu_parity(binary, replace(case, track=track, style=style), times)


def test_vertical_column_decoration_keeps_ruby_when_ignoring_shaped_scanline(binary, project):
    track, style, _ = decorated_columns(project, 'glow')
    track = replace(track, rubies=[RubyAnnotation('字', '字', pos_start_ms=200, pos_end_ms=360, target_line_index=0,
                                                  target_char_start=0, target_char_end=1)])
    flame = replace(style, karaoke_anim='scanline', scanline_enabled=True,
                    scanline_shape='flame', scanline_effect='sakura')
    ir = ir_for(binary, (track, flame))
    control = ir_for(binary, (track, style))
    assert not any(line.get('scanline') or line.get('fx_bursts') for line in ir['track']['lines'])
    assert ir['skia_layout']['lines'] == control['skia_layout']['lines']
    assert any(line['ruby_groups'] for line in ir['skia_layout']['lines'])
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        configure(process, control)
        frames, _ = collect(process, [260, 540])
        configure(process, ir)
        replay, _ = collect(process, [540, 260], generation=2)
    assert any(frame.any() for frame in frames) and np.array_equal(frames, replay[::-1])


@GPU
def test_unsupported_vertical_scanline_shape_is_ignored_like_d2d(binary, project):
    from test_subtitle_render_gpu_backend import _render_g1_frames
    from test_subtitle_skia_outline_ruby import render_skia
    from test_subtitle_skia_scanline import band
    from test_subtitle_skia_static_glow import assert_glow_parity

    track, style = vertical_source(project)
    flame = replace(band(style), scanline_shape='flame', scanline_effect='sakura')
    times = (300, 520, 750, 1100, 1550, 1780)
    draws = []
    for look in (style, flame):
        with NativeRendererProcess() as process:
            _, pixels = _render_g1_frames(process, look, times, force_warp=False, track=track,
                                          width=640, height=360, realization_enabled=False)
        draws.append([np.frombuffer(frame, np.uint8).reshape(360, 640, 4) for frame in pixels])
    assert np.array_equal(*draws)
    gpu, raster = render_skia(binary, ir_for(binary, (track, flame)), times)
    assert_glow_parity(times, draws[0], gpu, raster, 'inactive_vertical_scanline', check_p00_edges=True)


def vertical_inactive_effects(project):
    track, base = vertical_source(project)
    base = replace(base, stroke_width_px=3, entry_lead_ms=600, exit_fade_ms=600)
    return track, base, replace(base, entry_anim='char_drip', exit_anim='lightning', karaoke_stroke_flash=True)


def test_vertical_inactive_effects_preserve_display_windows(binary, project):
    track, base, effects = vertical_inactive_effects(project)
    control = ir_for(binary, (track, base))['track']['lines'][0]
    line = ir_for(binary, (track, effects))['track']['lines'][0]
    assert line['entry_anim'] == line['exit_anim'] == 'none'
    assert line['entry_duration_ms'] == line['exit_duration_ms'] == 0
    assert not line['stroke_flash'] and not line['fx_bursts']
    assert line['display_start_ms'] == control['display_start_ms']
    assert line['display_end_ms'] == control['display_end_ms']
    assert effects.entry_anim == 'char_drip' and effects.exit_anim == 'lightning' and effects.karaoke_stroke_flash


@GPU
def test_vertical_inactive_effects_are_ignored_like_d2d(binary, project):
    from test_subtitle_render_gpu_backend import _render_g1_frames
    from test_subtitle_skia_outline_ruby import render_skia
    from test_subtitle_skia_static_glow import assert_glow_parity

    track, base, effects = vertical_inactive_effects(project)
    times = (300, 520, 750, 1100, 1550, 1780)
    references = []
    for look in (base, effects):
        with NativeRendererProcess() as process:
            _, pixels = _render_g1_frames(process, look, times, force_warp=False, track=track,
                                          width=640, height=360, realization_enabled=False)
        references.append([np.frombuffer(frame, np.uint8).reshape(360, 640, 4) for frame in pixels])
    assert np.array_equal(*references)
    gpu, raster = render_skia(binary, ir_for(binary, (track, effects)), times)
    assert_glow_parity(times, references[0], gpu, raster, 'inactive_vertical_effects', check_p00_edges=True)


@pytest.mark.parametrize('target', ['gpu', 'raster'])
@pytest.mark.parametrize('directions', ['forward', 'reverse', 'mixed'])
def test_vertical_column_decoration_fades_each_line_independently(binary, project, target, directions):
    from test_subtitle_skia_outline_ruby import premultiplied

    track, style, _ = decorated_columns(project, 'shadow_to_glow', reverse=directions == 'reverse', outline=True,
                                        mixed_directions=directions == 'mixed')
    # Separate the two glow footprints so a column crop has one source alpha.
    style = replace(style, line_gap_px=100)
    track = replace(track, lines=[track.lines[0], replace(track.lines[1], display_start_override_ms=100,
                                                        display_end_override_ms=1500)])
    animated = replace(style, entry_anim='fade', exit_anim='fade', entry_lead_ms=200, exit_fade_ms=400)
    valid = ir_for(binary, (track, animated))
    baseline = ir_for(binary, (track, style))
    feature = 'p07_vertical_columns_decoration_fade'
    assert feature in valid['required_features'] and feature not in baseline['required_features']
    assert valid['skia_layout']['lines'] == baseline['skia_layout']['lines']
    first, second = valid['track']['lines']
    times = [second['display_start_ms'] + second['entry_duration_ms'] // 2,
             second['display_end_ms'] - second['exit_duration_ms'] // 2]
    boundary = round(sum(line['cells'][0]['origin_x'] for line in valid['skia_layout']['lines']) / 2
                     + style.font_size_px / 2)
    bad = copy.deepcopy(valid)
    bad['required_features'].remove(feature)
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, valid)
        frames, _ = collect(process, times)
        with pytest.raises(NativeRendererError):
            configure(process, bad)
        replay, _ = collect(process, list(reversed(times)), generation=2)
        configure(process, baseline)
        full, _ = collect(process, times, generation=3)
    assert all(np.array_equal(a, b) for a, b in zip(frames, reversed(replay), strict=True))
    for t, actual, original in zip(times, frames, full, strict=True):
        for column, line in ((slice(boundary, None), first), (slice(None, boundary), second)):
            entry = min((t - line['display_start_ms']) / line['entry_duration_ms'], 1)
            leave = min((line['display_end_ms'] - t) / line['exit_duration_ms'], 1)
            opacity = (1 - (1 - entry) ** 2) * leave ** 2
            assert 0 < opacity <= 1 and original[:, column, 3].max() > 16
            delta = abs(premultiplied(actual[:, column]) - premultiplied(original[:, column]) * opacity)
            y, x, _ = np.unravel_index(delta.argmax(), delta.shape)
            assert delta.max() <= 2, (t, opacity, delta.max(), actual[:, column][y, x], original[:, column][y, x])


@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_native_rejects_mixed_direction_column_decoration_without_capability(binary, project, target):
    track, style, plain = decorated_columns(project, 'glow', mixed_directions=True)
    plain_mixed = ir_for(binary, (track, plain))
    ir = ir_for(binary, (track, style))
    mixed = copy.deepcopy(ir)
    mixed['required_features'].remove('p06_vertical_mixed_column_decoration')
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        # Mixed directions remain valid for undecorated (transparent) columns.
        configure(process, plain_mixed)
        configure(process, ir)
        frames, _ = collect(process, [440])
        with pytest.raises(NativeRendererError):
            configure(process, mixed)
        replay, _ = collect(process, [440], generation=2)
    assert frames[0].any() and np.array_equal(frames[0], replay[0])


@pytest.mark.parametrize('target', ['gpu', 'raster'])
@pytest.mark.parametrize('layers', [1, 2])
@pytest.mark.parametrize('reverse', [False, True])
def test_vertical_outlines_follow_rotated_body_and_fixed_wipe_axis(binary, project, kana, target, layers, reverse):
    from test_subtitle_skia_outline_ruby import outlined, premultiplied

    track, style = vertical_source(project, texts=['ー'], position='top', reverse=reverse)
    track = replace(track, lines=[replace(track.lines[0], chars=track.lines[0].chars[:1])])
    style = outlined(replace(style, font_family=kana), layers, '80')
    ir = ir_for(binary, (track, style), dpr=1.5)
    feature = 'p06_vertical_outline'
    assert feature in ir['required_features']
    assert ('p06_vertical_reverse_outline' in ir['required_features']) == reverse
    assert ('p06_secondary_solid_outline' in ir['required_features']) == (layers == 2)
    line, = ir['track']['lines']
    (start, end), = line['resolved_intervals']
    times = [start - 1, (start + end) // 2, end + 1]
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, times)
        bad = copy.deepcopy(ir)
        bad['required_features'].remove('p06_vertical_reverse_outline' if reverse else feature)
        with pytest.raises(NativeRendererError):
            configure(process, bad)
        replay, _ = collect(process, [times[1]], generation=2)

    def pixels(frame, color, alpha):
        return (frame[..., :3] == color).all(axis=-1) & (frame[..., 3] == alpha)

    red = pixels(frames[0], [255, 0, 0], 128)
    y, x = np.nonzero(red)
    assert y.size > 0 and np.ptp(y) > 4 * np.ptp(x)
    assert pixels(frames[0], [0, 0, 255], 255).any()
    assert pixels(frames[0], [0, 255, 255], 255).any() == (layers == 2)
    green = pixels(frames[1], [0, 255, 0], 128)
    red = pixels(frames[1], [255, 0, 0], 128)
    assert red.any() and green.any()
    ys_sung, ys_unsung = np.nonzero(green)[0], np.nonzero(red)[0]
    top, bottom = (ys_unsung, ys_sung) if reverse else (ys_sung, ys_unsung)
    assert top.max() < bottom.min()
    if layers == 2:
        assert pixels(frames[1], [255, 0, 255], 255).any()
        assert pixels(frames[1], [0, 255, 255], 255).any()
    assert pixels(frames[2], [0, 255, 0], 128).any()
    assert np.array_equal(frames[1], replay[0])
    if target == 'gpu':
        with SkiaRendererProcess(binary, selection=selection('raster')) as process:
            configure(process, ir)
            raster, _ = collect(process, times)
        for gpu, cpu in zip(frames, raster, strict=True):
            difference = abs(premultiplied(gpu) - premultiplied(cpu))
            assert difference.mean() < .15 and np.mean(difference.max(axis=-1) > 16) < .003


@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_vertical_reverse_outline_uses_bottom_cells_first(binary, project, target):
    from test_subtitle_skia_outline_ruby import outlined

    track, style = vertical_source(project, texts=['字', '字'], position='top', reverse=True)
    ir = ir_for(binary, (track, outlined(style, 2)))
    cells = ir['skia_layout']['lines'][0]['cells']
    assert [cell['clock_index'] for cell in cells] == [1, 0]
    first, second = ir['track']['lines'][0]['resolved_intervals']
    times = [(first[0] + first[1]) // 2, second[1] + 1]
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        first_frame, finished = collect(process, times)[0]

    def sung_rows(frame):
        outer = (frame[..., :3] == [255, 0, 255]).all(axis=-1) & (frame[..., 3] == 255)
        return np.nonzero(outer)[0]

    front = cells[1]['wipe_top'] + (cells[1]['wipe_bottom'] - cells[1]['wipe_top']) / 2
    assert sung_rows(first_frame).size and sung_rows(first_frame).min() >= int(front)
    assert sung_rows(finished).min() < cells[1]['wipe_top']


@pytest.mark.parametrize('dpr,softness,decoration,mode,karaoke', [
    (1, 0, 'none', 'color', 'none'), (1.5, 8, 'glow', 'color', 'none'), (1.5, 8, 'shadow', 'color', 'none'),
    (1.5, 5, 'glow', 'brighten', 'none'), (1, 0, 'none', 'follow_before', 'none'),
    (1.5, 5, 'shadow', 'follow_after', 'none'),
    (1, 0, 'none', 'color', 'utopia'), (1.5, 5, 'glow', 'brighten', 'zoom_pulse'),
    (1.5, 5, 'glow', 'follow_before', 'utopia'), (1.5, 5, 'glow', 'follow_after', 'zoom_pulse'),
])
def test_vertical_reverse_scanline_follows_the_wipe_from_bottom_to_top(
        binary, project, dpr, softness, decoration, mode, karaoke):
    from test_subtitle_skia_outline_ruby import premultiplied
    from test_subtitle_skia_roles import colors

    track, base = vertical_source(project, texts=['字', '字'], position='top', reverse=True)
    palette = colors('#FF0000', '#00FF00') if mode == 'color' else colors('#700000', '#007000')
    base = replace(base, karaoke_colors=palette, decoration_kind=decoration,
                   reverse_karaoke_anim=karaoke, zoom_pulse_peak_pct=130,
                   glow_before_radius_px=6, glow_after_radius_px=6, shadow_before_offset_x=8, shadow_offset_x=8)
    style = replace(base, scanline_enabled=True, scanline_forward_enabled=False, scanline_reverse_enabled=True,
                    scanline_shape='band', scanline_mode=mode, scanline_color='#00FFFF',
                    scanline_brightness_pct=100, scanline_width_px=36, scanline_glow_px=softness)
    ir = ir_for(binary, (track, style), dpr=dpr)
    plain = ir_for(binary, (track, base), dpr=dpr)
    feature = 'p07_vertical_reverse_scanline'
    assert {feature, 'p07_vertical_scanline_band', 'p05_reverse_wipe'} <= set(ir['required_features'])
    if mode != 'color':
        feature = 'p07_vertical_reverse_scanline_palette'
        assert {feature, 'p07_vertical_scanline_palette'} <= set(ir['required_features'])
    if karaoke != 'none':
        feature = 'p07_vertical_reverse_scanline_singing'
        assert {feature, 'p07_vertical_reverse_singing', 'p07_vertical_scanline_singing'} <= set(ir['required_features'])
    line, = ir['track']['lines']
    (start, end), (_, last_end) = line['resolved_intervals']
    top, bottom = ir['skia_layout']['lines'][0]['cells']
    split = round((top['wipe_bottom'] + bottom['wipe_top']) * dpr / 2)
    times = [start - 1, start + (end - start) // 3, end - (end - start) // 3, last_end + 1]
    invalid = copy.deepcopy(ir)
    invalid['required_features'].remove(feature)
    targets = []
    for target in ('gpu', 'raster'):
        with SkiaRendererProcess(binary, selection=selection(target)) as process:
            configure(process, ir)
            frames, _ = collect(process, times)
            with pytest.raises(NativeRendererError):
                configure(process, invalid)
            replay, _ = collect(process, times[1:3][::-1], generation=2)
            configure(process, plain)
            reference, _ = collect(process, times, generation=3)
        assert np.array_equal(frames[1], replay[1]) and np.array_equal(frames[2], replay[0])
        assert np.array_equal(frames[0], reference[0]) and np.array_equal(frames[-1], reference[-1])
        centers = []
        for frame in frames[1:3]:
            red, green, blue, alpha = np.moveaxis(frame, -1, 0)
            if mode == 'color':
                mask = (red < 40) & (green > 180) & (blue > 180)
            else:
                before = (red > 180) & (blue < 40) & (alpha > 64)
                after = (green > 180) & (blue < 40) & (alpha > 64)
                assert bool(before.any()) == (mode != 'follow_after')
                assert bool(after.any()) == (mode != 'follow_before')
                if mode == 'brighten':
                    assert np.median(np.nonzero(after)[0]) > np.median(np.nonzero(before)[0])
                mask = before | after
            rows, _ = np.nonzero(mask & (alpha > 64))
            assert rows.size and rows.min() >= split
            centers.append(np.median(rows))
        assert centers[0] > centers[1] + 2
        targets.append(np.stack(frames))
    diff = abs(premultiplied(targets[0]) - premultiplied(targets[1]))
    assert diff.mean() < .15 and np.mean(diff.max(axis=-1) > 16) < .003


@pytest.mark.parametrize('dpr,columns,scanline', [(1, False, False), (1.5, True, False), (1.5, True, True)])
def test_vertical_zoom_pulse_holds_peak_at_cell_end_then_releases(binary, project, dpr, columns, scanline):
    from test_subtitle_skia_outline_ruby import premultiplied
    from test_subtitle_skia_roles import colors

    track, style = vertical_source(project, texts=['字'], position='top')
    line, = track.lines
    line = replace(line, end_ms=line.chars[0].start_ms + 400)
    track = replace(track, lines=[line])
    if columns:
        peer = replace(line, chars=[replace(cell, start_ms=cell.start_ms + 700) for cell in line.chars],
                       end_ms=line.end_ms + 700, track_line_index=1)
        track = replace(track, lines=[line, peer])
        style = replace(style, dual_line_layout=True, line_alignments=['left', 'right'], line_gap_px=28,
                        auto_fill_section_time=False, sync_entry=False,
                        decoration_kind='glow', glow_before_radius_px=4, glow_after_radius_px=5)
    style = replace(style, karaoke_colors=colors('#FF0000', '#00FF00'),
                    karaoke_anim='zoom_pulse', zoom_pulse_peak_pct=155, zoom_pulse_curve_level=2)
    if scanline:
        style = replace(style, scanline_enabled=True, scanline_shape='band', scanline_mode='color',
                        scanline_color='#00FFFF', scanline_width_px=24, scanline_glow_px=5)
    ir = ir_for(binary, (track, style), dpr=dpr)
    plain = ir_for(binary, (track, replace(style, karaoke_anim='none')), dpr=dpr)
    feature = 'p07_vertical_zoom_pulse'
    assert {feature, 'p07_vertical_singing_scale'} <= set(ir['required_features'])
    assert ('p07_vertical_columns_singing_scale' in ir['required_features']) == columns
    assert ('p07_vertical_zoom_scanline' in ir['required_features']) == scanline
    first = ir['skia_layout']['lines'][0]['cells'][0]
    _, end = ir['track']['lines'][0]['resolved_intervals'][0]
    x = first['layout_left']
    box = np.s_[:round((first['wipe_bottom'] + 5) * dpr), round((x - 35)*dpr):round((x + 35)*dpr)]
    times = [end, end + 120, end + 299, end + 300]
    invalid = copy.deepcopy(ir)
    invalid['required_features'].remove(feature)
    captures = []

    def green_ink(frame):
        red, green, _, alpha = np.moveaxis(frame[box], -1, 0)
        return np.count_nonzero((green > 200) & (red < 30) & (alpha > 160))

    for target in ('gpu', 'raster'):
        with SkiaRendererProcess(binary, selection=selection(target)) as process:
            configure(process, ir)
            frames, _ = collect(process, times)
            with pytest.raises(NativeRendererError):
                configure(process, invalid)
            replay, _ = collect(process, times[::-1], generation=2)
            configure(process, plain)
            steady, _ = collect(process, [end + 300], generation=3)
        assert all(np.array_equal(a, b) for a, b in zip(frames, reversed(replay), strict=True))
        sizes = [green_ink(frame) for frame in frames]
        assert sizes[0] > sizes[1] > sizes[2] >= sizes[3] > 0
        assert np.array_equal(frames[3][box], steady[0][box])
        captures.append(np.stack(frames))
    diff = abs(premultiplied(captures[0]) - premultiplied(captures[1]))
    assert diff.mean() < .15 and np.mean(diff.max(axis=-1) > 16) < .003


@pytest.mark.parametrize('karaoke,glow', [('utopia', False), ('zoom_pulse', True)])
def test_reverse_vertical_scaling_keeps_bottom_to_top_wipe(binary, project, karaoke, glow):
    from test_subtitle_skia_outline_ruby import premultiplied
    from test_subtitle_skia_roles import colors

    track, style = vertical_ruby_source(project, position='top')
    line, = track.lines
    line = replace(line, chars=line.chars[:1], end_ms=700, wipe_reverse=True)
    track = replace(track, lines=[line])
    plain = replace(style, reverse_karaoke_anim='none', karaoke_colors=colors('#FF0000', '#00FF00'))
    if glow:
        plain = replace(plain, decoration_kind='glow', glow_before_radius_px=3, glow_after_radius_px=4)
    animated = replace(plain, reverse_karaoke_anim=karaoke, zoom_pulse_peak_pct=145)
    ir = ir_for(binary, (track, animated), dpr=1.5)
    baseline = ir_for(binary, (track, plain), dpr=1.5)
    bare = ir_for(binary, (replace(track, rubies=[]), animated), dpr=1.5)
    feature = 'p07_vertical_reverse_singing'
    assert {feature, 'p07_vertical_singing_scale', 'p05_reverse_wipe'} <= set(ir['required_features'])
    resolved, = ir['skia_layout']['lines']
    assert resolved == {**bare['skia_layout']['lines'][0], 'ruby_groups': []}
    (start, end), = ir['track']['lines'][0]['resolved_intervals']
    times = [(start + end) // 2, end + 300]
    bad = copy.deepcopy(ir)
    bad['required_features'].remove(feature)
    captures = []
    for target in ('gpu', 'raster'):
        with SkiaRendererProcess(binary, selection=selection(target)) as process:
            configure(process, ir)
            frames, _ = collect(process, times)
            with pytest.raises(NativeRendererError):
                configure(process, bad)
            replay, _ = collect(process, times[::-1], generation=2)
            configure(process, baseline)
            steady, _ = collect(process, times, generation=3)
            configure(process, bare)
            without_reading, _ = collect(process, times, generation=4)
        assert all(np.array_equal(a, b) for a, b in zip(frames, reversed(replay), strict=True))
        assert all(np.array_equal(a, b) for a, b in zip(frames, without_reading, strict=True))
        # Peak bounds can change the blur's last coverage bit, even at unit scale.
        settled = abs(premultiplied(frames[-1]) - premultiplied(steady[-1]))
        assert settled.max() <= int(glow)
        assert np.count_nonzero(frames[0][..., 3] > 160) > np.count_nonzero(steady[0][..., 3] > 160)
        red, green, _, alpha = np.moveaxis(frames[0], -1, 0)
        sung = np.nonzero((green > 190) & (red < 30) & (alpha > 160))[0]
        unsung = np.nonzero((red > 190) & (green < 30) & (alpha > 160))[0]
        assert sung.size and unsung.size and np.median(sung) > np.median(unsung)
        captures.append(np.stack(frames))
    diff = abs(premultiplied(captures[0]) - premultiplied(captures[1]))
    assert diff.mean() < .15 and np.mean(diff.max(axis=-1) > 16) < .003


@pytest.mark.parametrize('reverse,karaoke', [(False, 'utopia'), (False, 'zoom_pulse'),
                                           (True, 'utopia'), (True, 'zoom_pulse')])
def test_vertical_singing_shadow_scales_around_the_fixed_wipe_axis(binary, project, reverse, karaoke):
    from krok_helper.subtitle_render.domain.paint import PaintFill
    from test_subtitle_skia_outline_ruby import premultiplied
    from test_subtitle_skia_roles import colors

    track, style = vertical_source(project, texts=['字'], position='top', reverse=reverse)
    line, = track.lines
    track = replace(track, lines=[replace(line, end_ms=700, display_end_override_ms=1400)])
    palette = colors('#00000000', '#00000000')
    palette = replace(palette, before=replace(palette.before, shadow=PaintFill(color='#FF0000')),
                      after=replace(palette.after, shadow=PaintFill(color='#0000FF')))
    baseline = replace(style, karaoke_colors=palette, decoration_kind='shadow', decoration_before_sync=False,
                       decoration_kind_before='shadow', shadow_before_offset_x=-12, shadow_offset_x=-12,
                       shadow_before_offset_y=2, shadow_offset_y=2, stroke_width_px=0)
    animated = replace(baseline, karaoke_anim=karaoke, reverse_karaoke_anim=karaoke,
                       zoom_pulse_peak_pct=200, zoom_pulse_curve_level=2)
    ir = ir_for(binary, (track, animated), dpr=1.5)
    before_only = ir_for(binary, (track, replace(animated, decoration_kind='none')), dpr=1.5)
    static = ir_for(binary, (track, baseline), dpr=1.5)
    feature = 'p07_vertical_singing_shadow'
    assert {feature, 'p06_vertical_shadow', 'p07_vertical_singing_scale'} <= set(ir['required_features'])
    invalid = copy.deepcopy(ir)
    invalid['required_features'].remove(feature)
    (start, end), = ir['track']['lines'][0]['resolved_intervals']
    times = [start - 1, (start + end) // 2, end + 300]
    captures = []
    for target in ('gpu', 'raster'):
        with SkiaRendererProcess(binary, selection=selection(target)) as process:
            configure(process, ir)
            frames, _ = collect(process, times)
            with pytest.raises(NativeRendererError):
                configure(process, invalid)
            replay, _ = collect(process, times[::-1], generation=2)
            configure(process, static)
            steady, _ = collect(process, times, generation=3)
            configure(process, before_only)
            unsung, _ = collect(process, times[1:], generation=4)
        assert all(np.array_equal(a, b) for a, b in zip(frames, reversed(replay), strict=True))
        red, _, blue, alpha = np.moveaxis(frames[1], -1, 0)
        before = np.nonzero((red > 210) & (blue < 20) & (alpha > 160))[0]
        after = np.nonzero((blue > 210) & (red < 20) & (alpha > 160))[0]
        assert before.size and after.size
        assert bool(np.median(after) > np.median(before)) == reverse
        remaining = np.nonzero((unsung[0][..., 0] > 210) & (unsung[0][..., 3] > 160))[0]
        assert remaining.size and not unsung[1].any()
        compare_extent = np.min if reverse else np.max
        assert compare_extent(remaining) == compare_extent(before)
        assert np.count_nonzero(alpha > 160) > np.count_nonzero(steady[1][..., 3] > 160)
        for i in (0, 2):
            assert abs(premultiplied(frames[i]) - premultiplied(steady[i])).max() <= 1
        captures.append(np.stack(frames))
    diff = abs(premultiplied(captures[0]) - premultiplied(captures[1]))
    assert diff.mean() < .15 and np.mean(diff.max(axis=-1) > 16) < .003


@pytest.mark.parametrize('mutation', ['feature', 'schema', 'schema_bool', 'flow', 'top_missing', 'box_inverted',
                                      'rotated_text', 'horizontal_box'])
@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_native_vertical_contract_rejects_forgeries_and_keeps_scene(binary, project, mutation, target):
    ir = ir_for(binary, vertical_source(project))
    bad = copy.deepcopy(ir)
    cell = bad['skia_layout']['lines'][0]['cells'][1]
    if mutation == 'feature': bad['required_features'].remove('p05_vertical_solid')
    elif mutation == 'schema': del bad['skia_layout']['vertical_solid_schema']
    elif mutation == 'schema_bool': bad['skia_layout']['vertical_solid_schema'] = True
    elif mutation == 'flow': bad['skia_layout']['lines'][0]['text_flow'] = 'ltr'
    elif mutation == 'top_missing': del cell['wipe_top']
    elif mutation == 'box_inverted': cell['wipe_top'], cell['wipe_bottom'] = cell['wipe_bottom'], cell['wipe_top']
    elif mutation == 'rotated_text': bad['track']['lines'][0]['chars'][1]['text'] = '→'
    else:
        bad['style']['vertical'] = False
        del bad['skia_layout']['lines'][0]['text_flow']
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir); before, _ = collect(process, [750])
        with pytest.raises(NativeRendererError):
            configure(process, bad)
        after, _ = collect(process, [750], generation=2)
    assert np.array_equal(before[0], after[0])


@pytest.mark.parametrize('mutation', ['schema', 'feature', 'bool', 'version'])
def test_old_vertical_sidecar_rejected_before_selection(binary, monkeypatch, mutation):
    process = SkiaRendererProcess(binary, selection=selection('raster'))
    try: ready = NativeRendererProcess.start(process)
    finally: process.abort(reason='vertical handshake fixture')
    if mutation == 'schema': del ready['vertical_solid_schema']
    elif mutation == 'feature': ready['profiles'].remove('p05_vertical_solid')
    else: ready['vertical_solid_schema'] = True if mutation == 'bool' else 1
    sent = []
    monkeypatch.setattr(NativeRendererProcess, 'start', lambda self: ready)
    monkeypatch.setattr(SkiaRendererProcess, 'abort', lambda *a, **k: None)
    monkeypatch.setattr(SkiaRendererProcess, '_send', lambda self, p: sent.append(p))
    with pytest.raises(NativeRendererError, match='竖排'):
        SkiaRendererProcess(binary).start()
    assert not sent


@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('japanese', [False, True])
def test_vertical_gpu_raster_and_out_of_order_seek(binary, project, kana, reverse, japanese):
    track, style = vertical_source(project, reverse=reverse, texts=['ゃ', '、', 'ー', '」'] if japanese else TEXTS)
    if japanese:
        style = replace(style, font_family=kana)
    ir = ir_for(binary, (track, style), dpr=1.5)
    order = [2200, 601, 199, 900, 599, 1401, 600, 899, 1399, 200]
    results = []
    for target in ('gpu', 'raster'):
        with SkiaRendererProcess(binary, selection=selection(target)) as process:
            configure(process, ir)
            ordered, _ = collect(process, sorted(order))
            shuffled, _ = collect(process, order, generation=2)
        by_time = dict(zip(sorted(order), ordered))
        assert all(np.array_equal(by_time[t], f) for t, f in zip(order, shuffled))
        assert not np.array_equal(ordered[0], ordered[-1])
        results.append(ordered)
    for a, b in zip(*results):
        def premult(p):
            p = p.astype(float); p[..., :3] *= p[..., 3:] / 255
            return p
        delta = abs(premult(a) - premult(b))
        assert delta.mean() < .15 and np.mean(delta.max(axis=-1) > 16) < .003


CASES = [*[('plain', False, 'none', pos, sem, dpr) for pos in ('bottom', 'top', 'center')
           for sem, dpr in (('legacy', 1.5), ('n3_1074', 1))],
         ('plain', False, 'none', 'bottom', 'n3_1074', 1.5), ('plain', False, 'none', 'center', 'legacy', 1),
         ('plain', True, 'none', 'bottom', 'legacy', 1.5), ('plain', True, 'none', 'top', 'n3_1074', 1),
         ('plain', False, 'no_wipe', 'bottom', 'n3_1074', 1.5), ('plain', True, 'no_wipe', 'center', 'legacy', 1.5),
         ('pause', False, 'none', 'bottom', 'legacy', 1.5), ('zero', True, 'none', 'bottom', 'n3_1074', 1.5),
         ('shared', False, 'none', 'bottom', 'n3_1074', 1.5)]


@GPU
@pytest.mark.parametrize('case', CASES, ids=lambda c: '-'.join(map(str, c)))
def test_vertical_matches_actual_gpu(binary, project, case):
    kind, reverse, effect, position, semantics, dpr = case
    track, style = vertical_source(project, kind, reverse=reverse, effect=effect, position=position)
    assert_gpu_parity(binary, track, replace(style, layout_semantics=semantics), dpr, case)


@GPU
@pytest.mark.parametrize('reverse', [False, True])
def test_vertical_kana_matches_actual_gpu(binary, project, kana, reverse):
    track, style = vertical_source(project, reverse=reverse, texts=['か', 'キ', 'あ', 'ハ'])
    assert_gpu_parity(binary, track, replace(style, font_family=kana, layout_semantics='n3_1074'), 1.5, ('kana', reverse))


@pytest.mark.parametrize('gpu', [True, False])
def test_real_vertical_preview(binary, project, monkeypatch, tmp_path, gpu):
    run_preview(binary, monkeypatch, tmp_path, vertical_source(project), gpu, False)


@pytest.mark.parametrize('gpu', [True, False])
def test_real_vertical_mp4(binary, project, monkeypatch, tmp_path, gpu):
    run_mp4(binary, monkeypatch, tmp_path, vertical_source(project, reverse=True), gpu, False)


def test_vertical_japanese_glyphs_keep_cell_geometry(binary, project, kana, tmp_path):
    track, style = vertical_source(project, texts=['ゃ', '、', 'ー', '」'])
    style = replace(style, font_family=kana)
    ir = ir_for(binary, (track, style), dpr=1.5)
    line = ir['skia_layout']['lines'][0]
    cells = line['cells']
    assert 'p05_vertical_japanese_glyphs' in ir['required_features']
    assert ir['skia_layout']['vertical_japanese_glyph_schema'] == 1
    assert [c['wipe_bottom'] - c['wipe_top'] for c in cells] == pytest.approx([50] * 4)
    assert [c['baseline_y'] - c['wipe_top'] for c in cells] == pytest.approx([39, 30, 44, 44])
    assert not any('rotation_pivot_x' in c for c in cells[:2])
    assert cells[2]['rotation_pivot_x'] == cells[3]['rotation_pivot_x']
    (tmp_path / 'vertical-japanese-layout.json').write_text(json.dumps({
        'schema': ir['skia_layout']['vertical_japanese_glyph_schema'],
        'line': line,
        'intervals': ir['track']['lines'][0]['resolved_intervals'],
    }, sort_keys=True), encoding='utf-8')


@pytest.mark.parametrize('mutation', ['feature', 'schema', 'missing_pivot', 'unexpected_pivot', 'pivot_type'])
def test_invalid_vertical_japanese_transforms_keep_previous_scene(binary, project, kana, mutation):
    track, style = vertical_source(project, texts=['ゃ', '、', 'ー', '」'])
    ir = ir_for(binary, (track, replace(style, font_family=kana)))
    bad = copy.deepcopy(ir)
    cells = bad['skia_layout']['lines'][0]['cells']
    if mutation == 'feature':
        bad['required_features'].remove('p05_vertical_japanese_glyphs')
    elif mutation == 'schema':
        bad['skia_layout']['vertical_japanese_glyph_schema'] = True
    elif mutation == 'missing_pivot':
        del cells[2]['rotation_pivot_x']
    elif mutation == 'unexpected_pivot':
        cells[1]['rotation_pivot_x'] = cells[2]['rotation_pivot_x']
    else:
        cells[2]['rotation_pivot_x'] = '500'
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        configure(process, ir)
        before, _ = collect(process, [750])
        with pytest.raises(NativeRendererError):
            configure(process, bad)
        after, _ = collect(process, [750], generation=2)
    assert np.array_equal(before[0], after[0])


@GPU
@pytest.mark.parametrize('texts', [('ゃ', '、', 'ー', '」'), ('ャ', '。', '〜', '「'), ('っ', 'ァ', '『', '』')])
@pytest.mark.parametrize('reverse', [False, True])
def test_vertical_japanese_glyphs_match_actual_gpu(binary, project, kana, texts, reverse):
    track, style = vertical_source(project, reverse=reverse, texts=texts)
    semantics, dpr = ('legacy', 1) if reverse else ('n3_1074', 1.5)
    assert_gpu_parity(binary, track, replace(style, font_family=kana, layout_semantics=semantics),
                      dpr, (texts, reverse))


@pytest.mark.parametrize('reverse', [False, True])
def test_vertical_spaces_and_empty_cells_keep_their_box_and_clock(binary, project, reverse, tmp_path):
    texts = ['A', '', ' ', '字']
    track, style = vertical_source(project, reverse=reverse, texts=texts)
    ir = ir_for(binary, (track, style), dpr=1.5)
    horizontal = ir_for(binary, (track, replace(style, vertical=False)), dpr=1.5)
    line = ir['skia_layout']['lines'][0]
    cells = line['cells']
    intervals = ir['track']['lines'][0]['resolved_intervals']
    assert ir['skia_layout']['vertical_solid_schema'] == 2
    assert [c['text'] for c in ir['track']['lines'][0]['chars']] == texts
    assert intervals == horizontal['track']['lines'][0]['resolved_intervals']
    assert all(c['wipe_bottom'] - c['wipe_top'] == pytest.approx(50) for c in cells)
    assert cells[1]['metrics']['glyphs'] == []
    assert all(c['metrics']['ink'] == [0, 0, 0, 0] for c in cells[1:3])
    bad = copy.deepcopy(ir)
    bad['skia_layout']['vertical_solid_schema'] = 1
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        configure(process, ir)
        before, _ = collect(process, [1000])
        with pytest.raises(NativeRendererError):
            configure(process, bad)
        after, _ = collect(process, [1000], generation=2)
    assert np.array_equal(before[0], after[0])
    if not reverse:
        (tmp_path / 'vertical-inkless-layout.json').write_text(json.dumps({
            'schema': ir['skia_layout']['vertical_solid_schema'],
            'line': line,
            'intervals': intervals,
        }, sort_keys=True), encoding='utf-8')


@GPU
@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('kind', ['blank_between', 'blank_at_edges', 'latin_slots', 'latin_blank', 'japanese_space'])
def test_vertical_spaces_and_latin_match_actual_gpu(binary, project, mixed, kana, reverse, kind):
    texts = {
        'blank_between': ['A', '', ' ', '字'],
        'blank_at_edges': ['', 'á', '한', ' '],
        'latin_slots': ['A', '字', 'W', '한'],
        'latin_blank': ['A', '', 'W', ' '],
        'japanese_space': ['あ', '　', '', 'ー'],
    }
    track, style = vertical_source(mixed if kind in {'latin_slots', 'latin_blank'} else project,
                                   reverse=reverse, texts=texts[kind])
    if kind == 'japanese_space':
        style = replace(style, font_family=kana)
    assert_gpu_parity(binary, track, replace(style, layout_semantics='legacy' if reverse else 'n3_1074'),
                      1 if reverse else 1.5, (kind, reverse))


# vertical_ruby_schema=1: one single-scalar upright upper reading per forward
# vertical line. The main column moves left by the rounded ruby line height plus
# the gap; the reading wipes along y over its final ink bounds.
def vertical_ruby_source(project, *, span=(0, 1), reading='字', position='bottom', family=None, extra=None):
    from test_subtitle_skia_roles import colors
    track, style = vertical_source(project, position=position)
    chars = track.lines[0].chars
    ruby = RubyAnnotation(''.join(c.text for c in chars[slice(*span)]), reading,
                          pos_start_ms=chars[span[0]].start_ms, pos_end_ms=chars[span[1]].start_ms,
                          target_line_index=0, target_char_start=span[0], target_char_end=span[1])
    style = replace(style, ruby_font_family=family or style.font_family, ruby_font_size_px=24, ruby_gap_px=6,
                    ruby_stroke_width_px=0, ruby_stroke2_enabled=False, ruby_colors_follow_main=False,
                    ruby_karaoke_colors=colors('#0000FF', '#FF00FF'))
    return replace(track, rubies=[ruby, *(extra or [])]), style


def test_vertical_ruby_ir_is_one_versioned_upright_group(binary, project, tmp_path):
    track, style = vertical_ruby_source(project)
    ir = ir_for(binary, (track, style), dpr=1.5)
    plain = ir_for(binary, (replace(track, rubies=[]), style), dpr=1.5)
    assert {'p05_vertical_ruby', 'p05_resolved_ruby', 'p05_upper_ruby', 'p05_vertical_solid'} <= set(ir['required_features'])
    assert ir['skia_layout']['vertical_ruby_schema'] == 1 and ir['skia_layout']['ruby_schema'] == 1
    line = ir['skia_layout']['lines'][0]
    (group,) = line['ruby_groups']
    (unit,) = group['units']
    assert group['style_cell'] == 0 and unit['text'] == '字' and unit['interval_ms'] == [200, 600]
    assert unit['wipe_bottom'] > unit['wipe_top'] and unit['wipe_end_position'] == 1
    # Same cells and clocks; the column only moves left by the ruby allowance.
    shift = {round(a['origin_x'] - b['origin_x'], 6) for a, b in zip(line['cells'], plain['skia_layout']['lines'][0]['cells'])}
    assert len(shift) == 1 and next(iter(shift)) < 0
    assert ir['track']['lines'][0]['resolved_intervals'] == plain['track']['lines'][0]['resolved_intervals']
    (tmp_path / 'vertical-ruby-layout.json').write_text(json.dumps({
        'schema': ir['skia_layout']['vertical_ruby_schema'],
        'line': ir['skia_layout']['lines'][0],
        'intervals': ir['track']['lines'][0]['resolved_intervals'],
    }, sort_keys=True), encoding='utf-8')


def vertical_role_source(project):
    track, style = vertical_source(project, 'roles', position='center')
    return track, replace(style, stroke_width_px=3)


def test_vertical_roles_and_lower_reading_leave_the_original_track_unchanged(binary, project):
    track, style = vertical_role_source(project)
    style = replace(style, lower_ruby_enabled=True)
    ir = ir_for(binary, (track, style))
    assert track.lines[0].chars[1].role_label == 'lead' and style.lower_ruby_enabled
    assert 'p05_lower_ruby' not in ir['required_features']
    assert all('role_label' not in c for c in ir['track']['lines'][0]['chars'])
    line, = track.lines
    bare = replace(track, lines=[replace(line, chars=[replace(c, role_label=None) for c in line.chars])])
    control = ir_for(binary, (bare, replace(style, lower_ruby_enabled=False)))
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        configure(process, ir)
        frames, _ = collect(process, [450, 750, 1350])
        configure(process, control)
        replay, _ = collect(process, [1350, 750, 450], generation=2)
    assert all(frame.any() for frame in frames) and np.array_equal(frames, replay[::-1])


@GPU
def test_vertical_roles_and_lower_reading_match_actual_d2d_gpu(binary, project):
    from PyQt6.QtGui import QFontDatabase
    from test_subtitle_render_gpu_backend import _render_g1_frames
    from test_subtitle_skia_outline_ruby import render_skia
    from test_subtitle_skia_static_glow import assert_glow_parity

    if 'Meiryo' not in QFontDatabase.families():
        pytest.skip('Meiryo is required for the actual Direct2D ruby allowance')
    track, role_style = vertical_role_source(project)
    annotated, style = vertical_ruby_source(project, span=(0, 2), reading='かな', position='center', family='Meiryo')
    track = replace(track, rubies=annotated.rubies)
    style = replace(style, custom_style_schemes=role_style.custom_style_schemes)
    times = (299, 450, 750, 899, 1150, 1600, 1900)
    refs = []
    roles_and_lower = replace(style, lower_ruby_enabled=True)
    for look in (roles_and_lower, replace(style, custom_style_schemes={})):
        with NativeRendererProcess() as process:
            _, raw = _render_g1_frames(process, look, times, force_warp=False, track=track,
                                       width=640, height=360, realization_enabled=False)
        refs.append([np.frombuffer(frame, np.uint8).reshape(360, 640, 4) for frame in raw])
    reference, bare = refs
    assert np.array_equal(reference, bare)
    gpu, raster = render_skia(binary, ir_for(binary, (track, roles_and_lower)), times)
    assert_glow_parity(times, reference, gpu, raster, 'vertical_inline_roles', check_p00_edges=True)


def test_unproven_vertical_ruby_stays_gated(binary, project):
    track, style = vertical_ruby_source(project, reading='👩‍💻')
    with pytest.raises(ValueError):
        ir_for(binary, (track, style))


@GPU
def test_vertical_overlapping_readings_match_actual_d2d_gpu(binary, project):
    from PyQt6.QtGui import QFontDatabase
    from test_subtitle_render_gpu_backend import _render_g1_frames
    from test_subtitle_skia_outline_ruby import render_skia
    from test_subtitle_skia_roles import colors
    from test_subtitle_skia_static_glow import assert_glow_parity

    if 'Meiryo' not in QFontDatabase.families():
        pytest.skip('Meiryo is required for the actual Direct2D ruby allowance')
    later, _ = vertical_ruby_source(project, span=(1, 3), reading='かな', family='Meiryo')
    earlier, style = vertical_ruby_source(project, span=(0, 2), reading='字字', family='Meiryo')
    track = replace(earlier, rubies=later.rubies + earlier.rubies)
    style = replace(style, karaoke_colors=colors('#00000000', '#00000000'))
    times, dpr = (250, 425, 650, 825, 1075, 1400, 1800), 3
    refs = []
    for readings in (track.rubies, track.rubies[:1], track.rubies[1:]):
        with NativeRendererProcess() as process:
            _, raw = _render_g1_frames(process, style, times, force_warp=False,
                                       track=replace(track, rubies=readings), width=640, height=360, dpr=dpr,
                                       realization_enabled=False)
        refs.append([np.frombuffer(p, np.uint8).reshape(360*dpr, 640*dpr, 4) for p in raw])
    for control in refs[1:]:
        assert any(np.count_nonzero((a[..., 3] > 127) & (b[..., 3] == 0)) > 80
                   for a, b in zip(refs[0], control, strict=True))
    ir = ir_for(binary, (track, style), dpr=dpr)
    assert 'p05_vertical_overlapping_ruby' in ir['required_features']
    first, second = ir['skia_layout']['lines'][0]['ruby_groups']
    assert (first['style_cell'], first['span_end_cell']) == (1, 3)
    assert (second['style_cell'], second['span_end_cell']) == (0, 2)
    gpu, raster = render_skia(binary, ir, times)
    assert_glow_parity(times, refs[0], gpu, raster, 'vertical_overlapping_ruby', check_p00_edges=True)
    ir['required_features'].remove('p05_vertical_overlapping_ruby')
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        with pytest.raises(NativeRendererError, match='竖排重叠注音'):
            configure(process, ir)


def test_vertical_reading_latin_and_middle_dot_require_their_capability(binary, project, kana):
    source = vertical_ruby_source(project, span=(0, 2), reading='A・', family=kana)
    ir = ir_for(binary, source)
    bad = copy.deepcopy(ir)
    bad['required_features'].remove('p05_vertical_ruby_extended_units')
    group, = ir['skia_layout']['lines'][0]['ruby_groups']
    first, second = group['units']
    assert first['text'] == 'A' and second['text'] == '・'
    assert first['interval_ms'][1] == second['interval_ms'][0]
    times = [300, 450, 750, 1100]
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        configure(process, ir)
        frames, _ = collect(process, times)
        with pytest.raises(NativeRendererError, match='竖排注音'):
            configure(process, bad)
        replay, _ = collect(process, times[::-1], generation=2)
    assert all(frame.any() for frame in frames) and np.array_equal(frames, replay[::-1])


@GPU
def test_vertical_reading_latin_and_middle_dot_match_actual_gpu(binary, project):
    from PyQt6.QtGui import QFontDatabase

    if 'Meiryo' not in QFontDatabase.families():
        pytest.skip('Meiryo is required for the actual Direct2D ruby allowance')
    track, style = vertical_ruby_source(project, span=(0, 2), reading='A・', family='Meiryo')
    assert_gpu_parity(binary, track, style, 1.5, 'vertical_latin_punctuation_reading')


def test_vertical_reading_kana_marks_keep_authored_clocks(binary, project, kana):
    source = vertical_ruby_source(project, span=(0, 2), reading='か\u309aか\u3099', family=kana)
    ir = ir_for(binary, source)
    group, = ir['skia_layout']['lines'][0]['ruby_groups']
    mark, normalized = group['units']
    assert mark['text'] == 'か\u309a' and len(mark['metrics']['glyphs']) == 2
    assert normalized['text'] == 'か\u3099' and normalized['glyph_text'] == 'が'
    assert mark['interval_ms'][1] == normalized['interval_ms'][0]
    bad = copy.deepcopy(ir)
    bad['skia_layout']['lines'][0]['ruby_groups'][0]['units'][1]['glyph_text'] = 'か'
    times = [300, 550, 750, 1100]
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        configure(process, ir)
        frames, _ = collect(process, times)
        with pytest.raises(NativeRendererError, match='NFC'):
            configure(process, bad)
        replay, _ = collect(process, times[::-1], generation=2)
    assert all(frame.any() for frame in frames) and np.array_equal(frames, replay[::-1])


@GPU
def test_vertical_reading_kana_marks_match_actual_gpu(binary, project):
    from PyQt6.QtGui import QFontDatabase

    if 'Meiryo' not in QFontDatabase.families():
        pytest.skip('Meiryo is required for the actual Direct2D ruby allowance')
    track, style = vertical_ruby_source(project, span=(0, 2), reading='か\u309aか\u3099', family='Meiryo')
    assert_gpu_parity(binary, track, style, 1.5, 'vertical_reading_kana_marks')


def vertical_guide_and_reading(project, tmp_path, guide_kind, family=None):
    from test_subtitle_skia_bitmap_guides import image
    from test_subtitle_skia_vector_guides import SQUARE
    from krok_helper.subtitle_render.domain.timing import GuideSymbol

    track, style = vertical_ruby_source(project, span=(1, 2), position='center', family=family)
    line, = track.lines
    guide = SQUARE
    if guide_kind == 'bitmap':
        guide = GuideSymbol(kind='bitmap', bitmap_before_path=image(tmp_path / 'guide.png'),
                            bitmap_no_decor=True, bitmap_zoom_percent=70, duration_ms=300)
    return replace(track, lines=[replace(line, guide_symbol=guide)]), style


@pytest.mark.parametrize('guide_kind', ['vector', 'bitmap'])
def test_vertical_guide_moves_the_reading_target_to_render_cells(binary, project, tmp_path, guide_kind):
    ir = ir_for(binary, vertical_guide_and_reading(project, tmp_path, guide_kind))
    source, = ir['track']['lines']
    layout, = ir['skia_layout']['lines']
    group, = layout['ruby_groups']
    assert group['style_cell'] == 2
    assert group['units'][0]['interval_ms'] == source['resolved_intervals'][2]
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        configure(process, ir)
        frames, _ = collect(process, [450, 750, 1350, 750])
    assert all(frame.any() for frame in frames) and np.array_equal(frames[1], frames[-1])


@GPU
@pytest.mark.parametrize('guide_kind', ['vector', 'bitmap'])
def test_vertical_guide_and_reading_match_actual_d2d_gpu(binary, project, tmp_path, guide_kind):
    from PyQt6.QtGui import QFontDatabase
    from test_subtitle_render_gpu_backend import _render_g1_frames
    from test_subtitle_skia_outline_ruby import render_skia
    from test_subtitle_skia_static_glow import assert_glow_parity

    if 'Meiryo' not in QFontDatabase.families():
        pytest.skip('Meiryo is required for the actual Direct2D ruby allowance')
    track, style = vertical_guide_and_reading(project, tmp_path, guide_kind, family='Meiryo')
    times = (450, 750, 950, 1350, 1670)
    with NativeRendererProcess() as process:
        _, raw = _render_g1_frames(process, style, times, force_warp=False, track=track,
                                   width=640, height=360, realization_enabled=False)
    refs = [np.frombuffer(frame, np.uint8).reshape(360, 640, 4) for frame in raw]
    gpu, raster = render_skia(binary, ir_for(binary, (track, style)), times)
    assert_glow_parity(times, refs, gpu, raster, ('vertical_guide_and_reading', guide_kind), check_p00_edges=True)


@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_vertical_ruby_groups_in_one_column_keep_independent_clocks(binary, project, target):
    # Reverse source order to exercise top-to-bottom placement by authored span.
    track, style = vertical_ruby_source(project, span=(0, 1), reading='字한')
    other, _ = vertical_ruby_source(project, span=(2, 3), reading='字')
    ir = ir_for(binary, (replace(track, rubies=other.rubies + track.rubies), style))
    feature = 'p05_vertical_ruby_groups'
    assert feature in ir['required_features']
    assert ir['skia_layout']['vertical_ruby_schema'] == 2
    first, second = ir['skia_layout']['lines'][0]['ruby_groups']
    assert [(g['style_cell'], g['span_end_cell']) for g in (first, second)] == [(0, 1), (2, 3)]
    assert max(u['wipe_bottom'] for u in first['units']) < second['units'][0]['wipe_top']
    assert first['units'][0]['interval_ms'][0] == track.rubies[0].pos_start_ms
    assert first['units'][-1]['interval_ms'][1] == track.rubies[0].pos_end_ms
    assert second['units'][0]['interval_ms'] == [other.rubies[0].pos_start_ms, other.rubies[0].pos_end_ms]

    def visible_rows(frame, group, *, lit):
        units = group['units']
        x0, x1 = int(min(u['wipe_left'] for u in units)), int(max(u['wipe_right'] for u in units)) + 1
        y0, y1 = int(min(u['wipe_top'] for u in units)), int(max(u['wipe_bottom'] for u in units)) + 1
        pixels = frame[y0:y1, x0:x1]
        red = pixels[..., 0] > 200 if lit else pixels[..., 0] < 50
        return np.nonzero(red & (pixels[..., 2] > 200) & (pixels[..., 3] > 16))[0]

    early = track.rubies[0].pos_start_ms - 1
    between = track.rubies[0].pos_end_ms + 1
    middle = (other.rubies[0].pos_start_ms + other.rubies[0].pos_end_ms) // 2
    late = other.rubies[0].pos_end_ms + 1
    times = [early, between, middle, late]
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, times)
        for mutation in ('feature', 'missing_span', 'overlapping_span', 'overlapping_ink'):
            bad = copy.deepcopy(ir)
            upper, lower = bad['skia_layout']['lines'][0]['ruby_groups']
            if mutation == 'feature':
                bad['required_features'].remove(feature)
            elif mutation == 'missing_span':
                del lower['span_end_cell']
            elif mutation == 'overlapping_span':
                upper['span_end_cell'] = lower['style_cell'] + 1
            else:
                unit = lower['units'][0]
                unit['baseline_y'] = upper['units'][0]['baseline_y']
                unit['wipe_top'] = unit['baseline_y'] + unit['metrics']['ink'][1]
                unit['wipe_bottom'] = unit['baseline_y'] + unit['metrics']['ink'][3]
            with pytest.raises(NativeRendererError):
                configure(process, bad)
        replay, _ = collect(process, [late, early, middle, between], generation=2)
    assert visible_rows(frames[0], first, lit=True).size == 0
    assert visible_rows(frames[1], first, lit=True).size > 0
    assert visible_rows(frames[1], second, lit=True).size == 0
    sung = visible_rows(frames[2], second, lit=True)
    unsung = visible_rows(frames[2], second, lit=False)
    assert sung.size > 0 and unsung.size > 0 and sung.max() <= unsung.min()
    assert visible_rows(frames[3], second, lit=True).size > 0
    assert all(np.array_equal(a, b) for a, b in zip(frames, (replay[1], replay[3], replay[2], replay[0]), strict=True))
    if target == 'gpu':
        from test_subtitle_skia_outline_ruby import premultiplied

        with SkiaRendererProcess(binary, selection=selection('raster')) as process:
            configure(process, ir)
            raster, _ = collect(process, times)
        for gpu, cpu in zip(frames, raster, strict=True):
            difference = abs(premultiplied(gpu) - premultiplied(cpu))
            assert difference.mean() < .15 and np.mean(difference.max(axis=-1) > 16) < .003


@pytest.mark.parametrize('mutation', ['feature', 'schema', 'schema_bool', 'unit_top', 'unit_box', 'unit_ink', 'two_units',
                                      'unit_text', 'groups_missing', 'two_groups'])
@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_native_vertical_ruby_rejects_forgeries_and_keeps_scene(binary, project, mutation, target):
    ir = ir_for(binary, vertical_ruby_source(project))
    bad = copy.deepcopy(ir)
    line = bad['skia_layout']['lines'][0]
    unit = line['ruby_groups'][0]['units'][0]
    if mutation == 'feature': bad['required_features'].remove('p05_vertical_ruby')
    elif mutation == 'schema': del bad['skia_layout']['vertical_ruby_schema']
    elif mutation == 'schema_bool': bad['skia_layout']['vertical_ruby_schema'] = True
    elif mutation == 'unit_top': del unit['wipe_top']
    elif mutation == 'unit_box': unit['wipe_top'], unit['wipe_bottom'] = unit['wipe_bottom'], unit['wipe_top']
    elif mutation == 'unit_ink': unit['wipe_top'] += .25
    elif mutation == 'two_units': line['ruby_groups'][0]['units'].append(copy.deepcopy(unit))
    elif mutation == 'unit_text': unit['text'] = 'じじ'
    elif mutation == 'groups_missing': del line['ruby_groups']
    else: line['ruby_groups'].append(copy.deepcopy(line['ruby_groups'][0]))
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir); before, _ = collect(process, [750])
        with pytest.raises(NativeRendererError):
            configure(process, bad)
        after, _ = collect(process, [750], generation=2)
    assert np.array_equal(before[0], after[0])


@pytest.mark.parametrize('mutation', ['schema', 'feature', 'bool', 'version'])
def test_old_vertical_ruby_sidecar_rejected_before_selection(binary, monkeypatch, mutation):
    process = SkiaRendererProcess(binary, selection=selection('raster'))
    try: ready = NativeRendererProcess.start(process)
    finally: process.abort(reason='vertical ruby handshake fixture')
    if mutation == 'schema': del ready['vertical_ruby_schema']
    elif mutation == 'feature': ready['profiles'].remove('p05_vertical_ruby')
    else: ready['vertical_ruby_schema'] = True if mutation == 'bool' else 1
    sent = []
    monkeypatch.setattr(NativeRendererProcess, 'start', lambda self: ready)
    monkeypatch.setattr(SkiaRendererProcess, 'abort', lambda *a, **k: None)
    monkeypatch.setattr(SkiaRendererProcess, '_send', lambda self, p: sent.append(p))
    with pytest.raises(NativeRendererError, match='竖排注音'):
        SkiaRendererProcess(binary).start()
    assert not sent


def test_vertical_ruby_gpu_raster_and_out_of_order_seek(binary, project):
    ir = ir_for(binary, vertical_ruby_source(project, span=(1, 3)), dpr=1.5)
    order = [2200, 601, 199, 900, 599, 1401, 600, 750, 1399, 200]
    results = []
    for target in ('gpu', 'raster'):
        with SkiaRendererProcess(binary, selection=selection(target)) as process:
            configure(process, ir)
            ordered, _ = collect(process, sorted(order))
            shuffled, _ = collect(process, order, generation=2)
        by_time = dict(zip(sorted(order), ordered))
        assert all(np.array_equal(by_time[t], f) for t, f in zip(order, shuffled))
        assert not np.array_equal(by_time[750], by_time[2200])
        results.append(ordered)
    for a, b in zip(*results):
        def premult(p):
            p = p.astype(float); p[..., :3] *= p[..., 3:] / 255
            return p
        delta = abs(premult(a) - premult(b))
        assert delta.mean() < .15 and np.mean(delta.max(axis=-1) > 16) < .003


@GPU
@pytest.mark.parametrize('span,position,dpr,semantics', [((0, 1), 'bottom', 1, 'legacy'), ((0, 1), 'top', 1.5, 'n3_1074'),
                                                         ((1, 3), 'center', 1, 'legacy'), ((1, 3), 'bottom', 1.5, 'n3_1074')])
def test_vertical_ruby_matches_actual_gpu(binary, project, span, position, dpr, semantics):
    from PyQt6.QtGui import QFontDatabase
    # The GPU's ruby allowance reads system face metrics; an app-only font
    # would fall back to another face there, so the reading uses Meiryo.
    if 'Meiryo' not in QFontDatabase.families():
        pytest.skip('Meiryo is required for the actual Direct2D ruby allowance')
    track, style = vertical_ruby_source(project, span=span, position=position, family='Meiryo')
    assert_gpu_parity(binary, track, replace(style, layout_semantics=semantics), dpr, (span, position, semantics))
