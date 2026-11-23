"""Solid horizontal vector guide symbols (vector_guide_schema=1) against the existing GPU."""
from dataclasses import replace
import copy
import json
import sys

import numpy as np
import pytest

from test_subtitle_skia_rtl_flow import (application, binary, project, rtl_source, assert_gpu_parity,
                                         collect, configure, ir_for, selection)
from test_subtitle_skia_protocol import test_actual_g5_preview_uses_skia_even_when_gpu_off as run_preview
from krok_helper.subtitle_render.domain.models import SubtitleStyleScheme
from krok_helper.subtitle_render.domain.paint import PaintFill
from krok_helper.subtitle_render.domain.timing import GuideSymbol, LineAnimationOverride, RubyAnnotation
from krok_helper.subtitle_render.engine.guide.skia_vector import vector_guide_advance, vector_guide_ink
from krok_helper.subtitle_render.native.backend import NativeRendererError, NativeRendererProcess
from krok_helper.subtitle_render.native.protocol import VectorGlyphTable
from krok_helper.subtitle_render.native.skia_backend import SkiaRendererProcess

GPU = pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
SQUARE = GuideSymbol(path_commands=(('M', 100, -700), ('L', 900, -700), ('L', 900, 0), ('L', 100, 0), ('Z',)),
                     units_per_em=1000, advance_width=1010, duration_ms=300)
# Curves, an implicitly filled open figure and a fractional advance.
CURVES = GuideSymbol(path_commands=(('M', 50, 0), ('C', 50, -900, 950, -900, 950, 0), ('Z',),
                                    ('M', 300, -200), ('Q', 500, -650, 700, -200), ('L', 500, -100)),
                     units_per_em=2048, advance_width=2100, duration_ms=250, count=2)


def guide_source(project, kind='prefix', symbol=SQUARE, *, reverse=False, effect='none', semantics='legacy'):
    track, style = rtl_source(project, reverse=reverse, effect=effect)
    line = track.lines[0]
    if kind == 'prefix':
        line = replace(line, guide_symbol=symbol)
    elif kind == 'anchored':
        line = replace(line, guide_symbol=replace(symbol, prefix_timing='anchored'))
    elif kind == 'replacement':
        line = replace(line, guide_symbol=replace(symbol, replacement_prefix=(line.chars[0].text,)))
    else:
        line = replace(line, inline_guide_symbols={1: symbol})
    return replace(track, lines=[line]), replace(style, right_to_left=False, layout_semantics=semantics)


@pytest.mark.parametrize('placement,entry', [('prefix', 'fade'), ('inline', 'fade'), ('inline', 'slide_in')])
def test_vector_guide_and_line_animation_share_the_same_render_path(binary, project, placement, entry):
    from test_subtitle_skia_outline_ruby import render_skia
    from test_subtitle_skia_static_glow import assert_glow_parity

    track, style = guide_source(project, placement)
    style = replace(style, stroke_width_px=3, karaoke_anim='none', entry_anim='none', exit_anim='none')
    guide, = track.lines
    guide = replace(guide, track_line_index=0, display_start_override_ms=0, display_end_override_ms=3000,
                    chars=[replace(char, start_ms=char.start_ms + 800) for char in guide.chars],
                    end_ms=guide.end_ms + 800)
    animation = LineAnimationOverride(entry_anim=entry, entry_duration_ms=600, karaoke_anim='none')
    next_line = replace(guide, guide_symbol=None, track_line_index=1, break_before='page',
                        chars=[replace(char, start_ms=char.start_ms + 3000) for char in guide.chars],
                        end_ms=guide.end_ms + 3000, display_start_override_ms=3000,
                        display_end_override_ms=5500, animation_override=animation)
    cases = [([replace(guide, animation_override=animation)], (250, 450, 750, 1100), [True])]
    if placement == 'prefix':
        cases.append(([guide, next_line], (950, 1100, 3100, 3350, 3650), [False, True]))
    for lines, times, expected_animation in cases:
        source = replace(track, lines=lines)
        ir = ir_for(binary, (source, style))
        feature = 'p07_line_fade' if entry == 'fade' else 'p07_line_motion'
        assert {'p05_vector_guide_composition', feature} <= set(ir['required_features'])
        assert [line['entry_duration_ms'] == 600 for line in ir['track']['lines']] == expected_animation
        gpu, raster = render_skia(binary, ir, times)
        assert all(frame.any() for frame in gpu)
        if len(lines) == 1:
            assert 0 < gpu[0][..., 3].sum() < gpu[-1][..., 3].sum()

        reference = None
        if sys.platform == 'win32':
            from test_subtitle_render_gpu_backend import _render_g1_frames

            with NativeRendererProcess() as process:
                _, pixels = _render_g1_frames(process, style, times, force_warp=False, track=source,
                                              width=640, height=360, realization_enabled=False)
            reference = [np.frombuffer(frame, np.uint8).reshape(360, 640, 4) for frame in pixels]
        assert_glow_parity(times, reference, gpu, raster, ('vector_guide_line_animation', placement, entry, len(lines)),
                           check_p00_edges=True)


def test_qt_free_guide_geometry_matches_the_gpu_contract():
    table = VectorGlyphTable()
    square = table.payload[table.reference(SQUARE)]
    curves = table.payload[table.reference(CURVES)]
    assert vector_guide_advance(square, 50) == 50.5
    assert vector_guide_ink(square, 50) == (5.0, -35.0, 45.0, 0.0)
    left, top, right, bottom = vector_guide_ink(curves, 64)
    scale = 64 / 2048
    assert (left, right, bottom) == pytest.approx((50 * scale, 950 * scale, 0))
    assert top == pytest.approx(-675 * scale)
    assert vector_guide_advance(curves, 64) == pytest.approx(64 * 2100 / 2048)
    symbol = replace(SQUARE, path_commands=(('M', 0.004, -1.006),))
    glyph = table.payload[table.reference(symbol)]
    assert glyph['path_commands'][0] == ['M', 0.0, -1.01]


@pytest.mark.parametrize('kind', ['prefix', 'anchored', 'replacement', 'inline'])
def test_vector_guide_ir_keeps_authored_cells_clocks_and_one_resource(binary, project, kind, tmp_path):
    track, style = guide_source(project, kind)
    two = replace(track, lines=[track.lines[0], replace(track.lines[0], end_ms=3000)])
    ir = ir_for(binary, (two, style))
    assert 'p05_vector_guides' in ir['required_features'] and ir['skia_layout']['vector_guide_schema'] == 1
    assert len(ir['vector_glyphs']) == 1  # Both lines share one deduplicated outline.
    glyph_id = next(iter(ir['vector_glyphs']))
    for source, final in zip(ir['track']['lines'], ir['skia_layout']['lines'], strict=True):
        chars, cells = source['chars'], final['cells']
        assert len(chars) == len(cells) == len(source['resolved_intervals'])
        guides = [i for i, c in enumerate(chars) if c.get('vector_glyph_id')]
        assert guides == ([1] if kind == 'inline' else [0]) and chars[guides[0]]['vector_glyph_id'] == glyph_id
        metrics = cells[guides[0]]['metrics']
        assert metrics['vector_glyph_id'] == glyph_id and metrics['glyphs'] == []
        assert metrics['advance'] == 50.5 and metrics['ink'] == [5.0, -35.0, 45.0, 0.0]
    starts = [c['start_ms'] for c in ir['track']['lines'][0]['chars']]
    expected = {'prefix': [-100, 200, 600, 900, 1400], 'anchored': [200, 200, 600, 900, 1400],
                'replacement': [200, 600, 900, 1400], 'inline': [200, 600, 900, 1400]}[kind]
    assert starts == expected
    if kind == 'prefix':
        (tmp_path / 'vector-guide-layout.json').write_text(json.dumps({
            'schema': ir['skia_layout']['vector_guide_schema'],
            'lines': ir['skia_layout']['lines'],
            'intervals': [line['resolved_intervals'] for line in ir['track']['lines']],
            'outlines': ir['vector_glyphs'],
        }, sort_keys=True), encoding='utf-8')


def test_same_lyrics_with_different_guides_keep_distinct_metrics(binary, project):
    track, style = guide_source(project)
    line = track.lines[0]
    wider = replace(line, guide_symbol=replace(SQUARE, advance_width=1215))
    ir = ir_for(binary, (replace(track, lines=[line, wider]), style))
    assert len(ir['vector_glyphs']) == 2
    metrics = [item['cells'][0]['metrics'] for item in ir['skia_layout']['lines']]
    assert [item['advance'] for item in metrics] == pytest.approx([50.5, 60.75])
    assert metrics[0]['vector_glyph_id'] != metrics[1]['vector_glyph_id']


def test_nonfinite_control_point_is_rejected():
    glyph = {'units_per_em': 1000, 'path_commands': [
        ['M', 0, 0], ['C', float('nan'), -700, 900, -700, 900, 0], ['Z'],
    ]}
    with pytest.raises(ValueError, match='非有限'):
        vector_guide_ink(glyph, 50)


def test_missing_bitmap_guide_image_is_explicit(binary, project):
    track, style = guide_source(project)
    line = track.lines[0]
    bitmap = replace(SQUARE, kind='bitmap', path_commands=(), bitmap_before_path='missing.png')
    track = replace(track, lines=[replace(line, guide_symbol=bitmap)])
    with pytest.raises(ValueError, match='无法读取图片'):
        ir_for(binary, (track, style))


def missing_vector_guide_role(project):
    track, style = guide_source(project)
    line, = track.lines
    unresolved = replace(track, lines=[replace(line, guide_symbol=replace(SQUARE, role_labels=('missing',)))])
    return track, unresolved, style


def test_missing_vector_guide_role_preserves_authored_label(binary, project):
    track, unresolved, style = missing_vector_guide_role(project)
    base = ir_for(binary, (track, style))
    ir = ir_for(binary, (unresolved, style))
    assert 'p05_role_vector_guides' not in ir['required_features']
    assert ir['skia_layout']['lines'] == base['skia_layout']['lines']
    assert unresolved.lines[0].guide_symbol.role_labels == ('missing',)


@GPU
def test_missing_vector_guide_role_falls_back_like_actual_d2d_gpu(binary, project):
    from test_subtitle_render_gpu_backend import _render_g1_frames
    from test_subtitle_skia_outline_ruby import render_skia
    from test_subtitle_skia_static_glow import assert_glow_parity

    track, unresolved, style = missing_vector_guide_role(project)
    times = (250, 450, 750, 1550)
    references = []
    for source in (track, unresolved):
        with NativeRendererProcess() as process:
            _, raw = _render_g1_frames(process, style, times, force_warp=False, track=source,
                                       width=640, height=360, realization_enabled=False)
        references.append([np.frombuffer(frame, np.uint8).reshape(360, 640, 4) for frame in raw])
    assert np.array_equal(*references)
    gpu, raster = render_skia(binary, ir_for(binary, (unresolved, style)), times)
    assert_glow_parity(times, references[0], gpu, raster, 'missing_vector_guide_role', check_p00_edges=True)


@GPU
@pytest.mark.parametrize('entry', ['none', 'fade'])
def test_vector_guide_and_lower_reading_match_actual_d2d_gpu(binary, application, tmp_path, entry):
    from scripts.subtitle_gpu_baseline.cases import build_cases
    from test_subtitle_render_gpu_backend import _render_g1_frames
    from test_subtitle_skia_character_fade import assert_ruby_transition_gpu_parity

    base = next(case for case in build_cases(tmp_path / 'assets') if case.case_id == 'lower_ruby')
    track = replace(base.track, lines=[replace(line, guide_symbol=SQUARE) for line in base.track.lines])
    style = replace(base.style, entry_anim=entry, exit_anim='none', karaoke_anim='none')
    times = (1000, 1550, 2600, 4110, 4280)
    if entry == 'fade':
        style = replace(style, decoration_kind='none', ruby_decoration_kind='none')
        times = (450, 600, 850, 1000, 1550)
    case = replace(base, track=track, style=style)
    references = []
    for style in (case.style, replace(case.style, lower_ruby_enabled=False)):
        with NativeRendererProcess() as process:
            _, frames = _render_g1_frames(process, style, times, force_warp=False, track=case.track,
                                          realization_enabled=False)
        references.append(frames)
    assert any(with_reading != without for with_reading, without in zip(*references, strict=True))
    ir = ir_for(binary, (case.track, case.style))
    assert 'p05_vector_guide_lower_ruby' in ir['required_features']
    if entry == 'fade':
        assert ir['track']['lines'][0]['entry_duration_ms'] == 600
    assert_ruby_transition_gpu_parity(binary, case, times)


@GPU
@pytest.mark.parametrize('entry', ['none', 'fade'])
def test_inline_vector_guide_and_upper_reading_match_actual_d2d_gpu(binary, application, tmp_path, entry):
    from scripts.subtitle_gpu_baseline.cases import build_cases
    from test_subtitle_render_gpu_backend import _render_g1_frames
    from test_subtitle_skia_character_fade import assert_ruby_transition_gpu_parity

    base = next(case for case in build_cases(tmp_path / 'assets') if case.case_id == 'lower_ruby')
    line, = base.track.lines
    track = replace(base.track, lines=[replace(line, inline_guide_symbols={1: SQUARE})])
    style = replace(base.style, lower_ruby_enabled=False, entry_anim=entry, exit_anim='none', karaoke_anim='none')
    times = (1150, 1550, 2600, 3600, 4110, 4280)
    if entry == 'fade':
        style = replace(style, decoration_kind='none', ruby_decoration_kind='none')
        times = (450, 600, 850, 1550, 4280)
    references = []
    for source in (track, replace(track, rubies=[]), base.track):
        with NativeRendererProcess() as process:
            _, frames = _render_g1_frames(process, style, times, force_warp=False, track=source,
                                          realization_enabled=False)
        references.append(frames)
    for control in references[1:]:
        assert any(a != b for a, b in zip(references[0], control, strict=True))
    ir = ir_for(binary, (track, style))
    assert 'p05_inline_vector_guide_ruby' in ir['required_features']
    if entry == 'fade':
        assert ir['track']['lines'][0]['entry_duration_ms'] == 600
    assert_ruby_transition_gpu_parity(binary, replace(base, track=track, style=style), times)


@GPU
@pytest.mark.parametrize('entry', ['none', 'fade'])
def test_vector_guide_shadow_matches_actual_d2d_gpu(binary, project, entry):
    from test_subtitle_render_gpu_backend import _render_g1_frames
    from test_subtitle_skia_outline_ruby import render_skia
    from test_subtitle_skia_static_glow import assert_glow_parity

    track, style = guide_source(project)
    line, = track.lines
    if entry == 'fade':
        line = replace(line, chars=[replace(char, start_ms=char.start_ms + 1000) for char in line.chars],
                       end_ms=line.end_ms + 1000, display_start_override_ms=0, display_end_override_ms=3500)
    track = replace(track, lines=[replace(line, inline_guide_symbols={2: CURVES})])
    palette = style.karaoke_colors
    palette = replace(palette, before=replace(palette.before, shadow=PaintFill(color='#C03429F6')),
                      after=replace(palette.after, shadow=PaintFill(color='#C0F07010')))
    style = replace(style, stroke_width_px=4, decoration_kind='shadow', shadow_offset_x=9, shadow_offset_y=13,
                    karaoke_colors=palette, entry_anim=entry, entry_lead_ms=600)
    times, dpr = (250, 350, 650, 950, 1550, 1900), 3
    refs = []
    for look in (style, replace(style, decoration_kind='none')):
        with NativeRendererProcess() as process:
            _, raw = _render_g1_frames(process, look, times, force_warp=False, track=track,
                                       width=640, height=360, dpr=dpr, realization_enabled=False)
        refs.append([np.frombuffer(frame, np.uint8).reshape(360*dpr, 640*dpr, 4) for frame in raw])
    reference, bare = refs
    ir = ir_for(binary, (track, style), dpr=dpr)
    assert 'p06_vector_guide_shadow' in ir['required_features']
    if entry == 'fade':
        assert ir['track']['lines'][0]['entry_duration_ms'] == 600
        assert 0 < reference[0][..., 3].max() < reference[2][..., 3].max()
    guide = ir['skia_layout']['lines'][0]['cells'][0]
    xmin, xmax = round(guide['wipe_left'] * dpr), round(guide['wipe_right'] * dpr)
    assert any(np.count_nonzero((a[:, xmin:xmax, 3] > 16) & (b[:, xmin:xmax, 3] == 0)) > 40
               for a, b in zip(reference, bare, strict=True))
    gpu, raster = render_skia(binary, ir, times)
    assert_glow_parity(times, reference, gpu, raster, 'vector_guide_shadow', check_p00_edges=True)


def vertical_vector_guide(project, reverse=False):
    from test_subtitle_skia_vertical import vertical_source

    track, style = vertical_source(project, position='center', texts=['字', '한', '字', '한'], reverse=reverse)
    line, = track.lines
    return replace(track, lines=[replace(line, guide_symbol=SQUARE)]), style


def test_vertical_vector_guide_keeps_cell_clock_and_centers_ink(binary, project):
    ir = ir_for(binary, vertical_vector_guide(project))
    first, second, *_ = ir['skia_layout']['lines'][0]['cells']
    assert first['metrics']['vector_glyph_id']
    assert first['wipe_bottom'] == pytest.approx(second['wipe_top'])
    ink = first['metrics']['ink']
    assert first['baseline_y'] + (ink[1] + ink[3]) / 2 == pytest.approx(
        (first['wipe_top'] + first['wipe_bottom']) / 2)
    missing = copy.deepcopy(ir)
    missing['required_features'].remove('p05_vertical_vector_guides')
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        configure(process, ir)
        frames, _ = collect(process, [250, 750])
        with pytest.raises(NativeRendererError, match='导唱符'):
            configure(process, missing)
        replay, _ = collect(process, [750, 250], generation=2)
    assert all(frame.any() for frame in frames) and np.array_equal(frames, replay[::-1])


@GPU
@pytest.mark.parametrize('reverse', [False, True], ids=['top_to_bottom', 'bottom_to_top'])
def test_vertical_vector_guide_matches_actual_d2d_gpu(binary, project, reverse):
    from test_subtitle_render_gpu_backend import _render_g1_frames
    from test_subtitle_skia_outline_ruby import render_skia
    from test_subtitle_skia_static_glow import assert_glow_parity

    track, style = vertical_vector_guide(project, reverse)
    line, = track.lines
    times = (250, 450, 750, 1100, 1550, 1900)
    references = []
    for source in (track, replace(track, lines=[replace(line, guide_symbol=None)])):
        with NativeRendererProcess() as process:
            _, raw = _render_g1_frames(process, style, times, force_warp=False, track=source,
                                       width=640, height=360, realization_enabled=False)
        references.append([np.frombuffer(frame, np.uint8).reshape(360, 640, 4) for frame in raw])
    reference, control = references
    assert any(np.count_nonzero((a[..., 3] > 16) & (b[..., 3] == 0)) > 300
               for a, b in zip(reference, control, strict=True))
    gpu, raster = render_skia(binary, ir_for(binary, (track, style)), times)
    assert_glow_parity(times, reference, gpu, raster, 'vertical_vector_guide', check_p00_edges=True)


@GPU
@pytest.mark.parametrize('decoration,reverse', [('glow', False), ('shadow', True)])
def test_vertical_vector_guide_decoration_matches_actual_d2d_gpu(binary, project, decoration, reverse):
    from krok_helper.subtitle_render.domain.paint import PaintFill
    from test_subtitle_render_gpu_backend import _render_g1_frames
    from test_subtitle_skia_outline_ruby import render_skia
    from test_subtitle_skia_static_glow import assert_glow_parity

    track, base = vertical_vector_guide(project, reverse)
    palette = base.karaoke_colors
    palette = replace(palette, before=replace(palette.before, shadow=PaintFill(color='#D0FC4022')),
                      after=replace(palette.after, shadow=PaintFill(color='#D0E030FF')))
    style = replace(base, decoration_before_sync=True, decoration_kind=decoration, line_y_margin_px=60,
                    glow_before_radius_px=5, glow_after_radius_px=5, shadow_offset_x=9, shadow_offset_y=8,
                    karaoke_colors=palette)
    times, dpr = (250, 450, 750, 1100, 1550, 1750), 3
    refs = []
    for look in (style, replace(style, decoration_kind='none')):
        with NativeRendererProcess() as process:
            _, raw = _render_g1_frames(process, look, times, force_warp=False, track=track,
                                       width=640, height=360, dpr=dpr, realization_enabled=False)
        refs.append([np.frombuffer(frame, np.uint8).reshape(360*dpr, 640*dpr, 4) for frame in raw])
    ir = ir_for(binary, (track, style), dpr=dpr)
    assert 'p06_vertical_vector_guide_decoration' in ir['required_features']
    guide, next_cell, *_ = ir['skia_layout']['lines'][0]['cells']
    above_text = slice(max(0, int((guide['wipe_top'] - 15)*dpr)), int(next_cell['wipe_top']*dpr))
    assert any(np.count_nonzero((a[above_text, :, 3] > 16) & (b[above_text, :, 3] == 0)) > 30
               for a, b in zip(*refs, strict=True))
    gpu, raster = render_skia(binary, ir, times)
    assert_glow_parity(times, refs[0], gpu, raster, ('vertical_vector_guide', decoration), check_p00_edges=True)


def outlined_inline_guide(project):
    from test_subtitle_skia_outline_ruby import outlined

    track, style = guide_source(project, 'inline', symbol=CURVES)
    return track, outlined(style, 2)


def test_inline_vector_outlines_preserve_scene_after_missing_capability(binary, project):
    ir = ir_for(binary, outlined_inline_guide(project))
    bad = copy.deepcopy(ir)
    bad['required_features'].remove('p06_inline_vector_guide_outline')
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        configure(process, ir)
        frames, _ = collect(process, [550, 750, 1100])
        with pytest.raises(NativeRendererError, match='导唱符'):
            configure(process, bad)
        replay, _ = collect(process, [1100, 750, 550], generation=2)
    assert np.array_equal(frames, replay[::-1]) and all(frame.any() for frame in frames)


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
def test_inline_vector_outlines_match_actual_d2d_gpu(binary, project):
    from test_subtitle_render_gpu_backend import _render_g1_frames
    from test_subtitle_skia_outline_ruby import render_skia
    from test_subtitle_skia_static_glow import assert_glow_parity

    track, style = outlined_inline_guide(project)
    times = (450, 550, 750, 1100, 1550)
    with NativeRendererProcess() as process:
        _, raw = _render_g1_frames(process, style, times, force_warp=False, track=track,
                                   width=640, height=360, realization_enabled=False)
    body = [np.frombuffer(frame, np.uint8).reshape(360, 640, 4) for frame in raw]
    gpu, raster = render_skia(binary, ir_for(binary, (track, style)), times)
    assert_glow_parity(times, body, gpu, raster, 'inline_vector_outlines', check_p00_edges=True)


def test_rtl_vector_guide_preserves_signal_and_scene(binary, project):
    track, style = guide_source(project, 'inline')
    style = replace(style, right_to_left=True)
    ir = ir_for(binary, (track, style))
    bad = copy.deepcopy(ir)
    bad['required_features'].remove('p05_rtl_vector_guides')
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        configure(process, ir)
        frames, _ = collect(process, [250, 750, 1100])
        with pytest.raises(NativeRendererError, match='导唱符'):
            configure(process, bad)
        replay, _ = collect(process, [1100, 750, 250], generation=2)
    assert np.array_equal(frames, replay[::-1]) and all(frame.any() for frame in frames)


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('kind', ['prefix', 'inline'])
def test_rtl_vector_guide_matches_d2d_gpu(binary, project, kind):
    from test_subtitle_render_gpu_backend import _render_g1_frames
    from test_subtitle_skia_outline_ruby import render_skia
    from test_subtitle_skia_static_glow import assert_glow_parity

    track, style = guide_source(project, kind)
    style = replace(style, right_to_left=True)
    times = (250, 550, 750, 1100, 1550)
    with NativeRendererProcess() as process:
        _, raw = _render_g1_frames(process, style, times, force_warp=False, track=track,
                                   width=640, height=360, realization_enabled=False)
    reference = [np.frombuffer(frame, np.uint8).reshape(360, 640, 4) for frame in raw]
    gpu, raster = render_skia(binary, ir_for(binary, (track, style)), times)
    assert_glow_parity(times, reference, gpu, raster, ('rtl_vector_guide', kind), check_p00_edges=True)


def p00_guide(tmp_path, count=1):
    from scripts.subtitle_gpu_baseline.cases import build_cases
    case = next(case for case in build_cases(tmp_path / 'assets') if case.case_id == 'guide_vector')
    line = case.track.lines[0]
    track = replace(case.track, lines=[replace(line, guide_symbol=replace(line.guide_symbol, count=count))])
    return case, track


@pytest.mark.parametrize('count', [1, 2])
@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_prefix_guide_composes_ruby_on_render_cells(binary, project, count, target):
    from test_subtitle_skia_outline_ruby import outlined

    track, style = guide_source(project, symbol=replace(SQUARE, count=count), effect='utopia')
    line = track.lines[0]
    line = replace(line, chars=[replace(char, text='字') for char in line.chars])
    # All occurrences have the same text. Explicit authored ownership must
    # survive insertion even when the old slot still matches the reading text.
    ruby = RubyAnnotation('字', 'A', target_char_start=2, target_char_end=3)
    track = replace(track, lines=[line], rubies=[ruby])
    look = replace(outlined(style, 2), latin_stroke_width_px=17, latin_stroke2_enabled=False)
    ir = ir_for(binary, (track, look))
    assert 'p05_vector_guide_composition' in ir['required_features']
    assert 'p06_latin_primary_outline' not in ir['required_features']
    assert ir['skia_layout']['vector_guide_schema'] == 1
    group, = ir['skia_layout']['lines'][0]['ruby_groups']
    source, = ir['track']['lines']
    assert group['style_cell'] == 2 + count
    assert group['units'][0]['interval_ms'] == source['resolved_intervals'][count + 2]
    assert [c['text'] for c in source['chars'][:count + 1]] == ['\uFFFC'] * count + ['字']
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        before, _ = collect(process, [950])
        bad = copy.deepcopy(ir)
        bad['required_features'].remove('p05_vector_guide_composition')
        with pytest.raises(NativeRendererError):
            configure(process, bad)
        after, _ = collect(process, [950], generation=2)
        # A supported capability elsewhere in the source does not alter this line.
        unrelated = copy.deepcopy(ir)
        unrelated['required_features'].append('p06_static_shadow')
        configure(process, unrelated)
        replay, _ = collect(process, [950], generation=3)
    assert before[0].any() and np.array_equal(before[0], after[0])
    assert np.array_equal(before[0], replay[0])


@pytest.mark.parametrize('effect', ['gradient', 'scanline', 'gradient_glow', 'scanline_particles', 'scanline_snow'])
def test_faded_gradient_with_leading_replacement_guide(binary, project, effect):
    from test_subtitle_skia_outline_ruby import outlined

    track, style = guide_source(project, effect='utopia')
    line = track.lines[0]
    line = replace(line, chars=[replace(char, text='字') for char in line.chars],
                   guide_symbol=None, inline_guide_symbols={0: SQUARE},
                   display_start_override_ms=0, display_end_override_ms=2400)
    track = replace(track, lines=[line], rubies=[RubyAnnotation('字', 'A', target_char_start=2, target_char_end=3)])
    style = outlined(style, 2)
    palette = style.karaoke_colors
    ramp = PaintFill(mode='gradient_vertical', start_color='#FF0066FF', end_color='#FFECA4F0')
    style = replace(style, karaoke_colors=replace(palette, before=replace(palette.before, stroke=ramp)),
                    ruby_colors_follow_main=True, entry_anim='fade', exit_anim='fade',
                    entry_lead_ms=500, exit_fade_ms=400)
    feature = 'p06_animated_singing_gradient_fill'
    if effect in {'scanline', 'scanline_particles', 'scanline_snow'}:
        style = replace(style, scanline_enabled=True, scanline_forward_enabled=True, scanline_mode='color',
                        scanline_color='#FFFFFF', scanline_width_px=20, scanline_glow_px=3)
        feature = 'p07_faded_scanline_prefix_guides'
        if effect == 'scanline_particles':
            palette = style.karaoke_colors
            style = replace(style, sing_fx='twinkle',
                            karaoke_colors=replace(palette, before=replace(palette.before, shadow=ramp)),
                            decoration_kind='glow', glow_before_radius_px=6, glow_after_radius_px=6,
                            fx_particle_color_mode='follow_before', fx_particle_color_layers='decor')
        elif effect == 'scanline_snow':
            style = replace(style, sing_fx='snow_solid')
    elif effect == 'gradient_glow':
        palette = style.karaoke_colors
        style = replace(style, karaoke_colors=replace(palette, before=replace(palette.before, shadow=ramp)),
                        decoration_kind='glow', glow_before_radius_px=6, glow_after_radius_px=6,
                        entry_anim='slide_in', exit_anim='rise')
        feature = 'p06_gradient_glow'
    ir = ir_for(binary, (track, style))
    assert {feature, 'p05_vector_guide_composition', 'p06_ruby_outline_gradient'} <= set(ir['required_features'])
    if effect == 'scanline_particles':
        assert {'p07_faded_scanline_singing_particles', 'p07_faded_scanline_particle_paint'} <= set(ir['required_features'])
    if effect == 'scanline_snow':
        assert 'p07_faded_scanline_singing_particles' in ir['required_features']
        assert {burst['kind'] for burst in ir['track']['lines'][0]['fx_bursts']} == {'snow_solid'}
    assert ir['track']['lines'][0]['chars'][0].get('vector_glyph_id')
    assert ir['skia_layout']['lines'][0]['ruby_groups'][0]['style_cell'] == 2
    invalid = copy.deepcopy(ir)
    invalid['required_features'].remove(feature)
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        configure(process, ir)
        frames, _ = collect(process, [180, 650, 950, 2300, 180])
        with pytest.raises(NativeRendererError):
            configure(process, invalid)
    assert frames[0][..., 3].any() and not np.array_equal(frames[1], frames[2])
    assert np.array_equal(frames[0], frames[-1])
    with pytest.raises(ValueError, match='前置导唱符|扫字线'):
        ir_for(binary, (replace(track, lines=[replace(line, inline_guide_symbols={1: SQUARE})]), style))


@GPU
@pytest.mark.parametrize('count', [1, 2])
def test_p00_prefix_guide_with_utopia_ruby_outlines_and_glow_matches_actual_gpu(binary, application, tmp_path, count):
    from test_subtitle_render_gpu_backend import _render_g1_frames
    from test_subtitle_skia_protocol import collect as collect_frames
    from test_subtitle_skia_static_glow import assert_glow_parity
    case, track = p00_guide(tmp_path, count)
    times = [*case.timestamps_ms, 250, 3700, 4200]
    with NativeRendererProcess() as process:
        _, raw = _render_g1_frames(process, case.style, times, force_warp=False, track=track,
                                   width=1920, height=1080, dpr=1, realization_enabled=False)
    reference = [np.frombuffer(p, np.uint8).reshape(1080, 1920, 4) for p in raw]
    rendered = []
    for target in ('gpu', 'raster'):
        with SkiaRendererProcess(binary, selection=selection(target)) as process:
            process.configure(track, case.style, width=1920, height=1080, fps=60)
            frames, _ = collect_frames(process, times)
            rendered.append(frames)
    assert_glow_parity(times, reference, *rendered, f'guide_vector-{count}', check_p00_edges=True)


@GPU
@pytest.mark.parametrize('appearance', ['japanese', 'latin-outline', 'guide-role', 'guide-shadow'])
def test_faded_solid_replacement_guides_match_actual_gpu(binary, application, tmp_path, appearance):
    from test_subtitle_skia_character_fade import assert_ruby_transition_gpu_parity

    case, track = p00_guide(tmp_path, count=3)
    line = track.lines[0]
    guide_role = appearance in {'guide-role', 'guide-shadow'}
    latin_outline = appearance in {'latin-outline', 'guide-role'}
    if latin_outline:
        line = replace(line, chars=[*line.chars[:3], replace(line.chars[3], text='A'), *line.chars[4:]])
    guide = replace(line.guide_symbol, replacement_prefix=tuple(char.text for char in line.chars[:3]))
    track = replace(track, lines=[replace(line, guide_symbol=guide)])
    style = replace(case.style, karaoke_anim='utopia', entry_anim='fade', exit_anim='fade',
                    latin_stroke_width_px=2 if latin_outline else None,
                    latin_stroke2_enabled=None if latin_outline else False)
    if guide_role:
        palette = style.karaoke_colors
        colors = replace(palette, before=replace(palette.before, text=PaintFill(color='#FFD890F0')))
        role = SubtitleStyleScheme(font_size_px=style.font_size_px, stroke_width_px=style.stroke_width_px,
                                   font_family=style.font_family, font_family_latin=style.font_family_latin,
                                   karaoke_colors=colors)
        line = replace(track.lines[0], guide_symbol=replace(guide, role_labels=('lead',) * 3))
        track = replace(track, lines=[line])
        style = replace(style, custom_style_schemes={'lead': role})
        if appearance == 'guide-shadow':
            role = replace(role, decoration_kind='shadow', shadow_offset_x=-8, shadow_offset_y=7)
            style = replace(style, decoration_kind='shadow', shadow_offset_x=5, shadow_offset_y=4,
                            custom_style_schemes={'lead': role})
            plain = replace(line, guide_symbol=None, chars=[replace(char, start_ms=char.start_ms + 6000)
                            for char in line.chars], end_ms=line.end_ms + 6000,
                            display_start_override_ms=6300, display_end_override_ms=11500)
            track = replace(track, lines=[line, plain])
    ir = ir_for(binary, (track, style))
    assert 'p05_vector_guide_composition' in ir['required_features']
    assert sum('vector_glyph_id' in cell for cell in ir['track']['lines'][0]['chars']) == 3
    assert ('p05_role_vector_guides' in ir['required_features']) == guide_role
    times = (300, 450, 650, 1500, 2600, 3700, 5100, 5300, 5500)
    if appearance == 'guide-shadow':
        times += (6450, 7250, 10650, 11300)
    assert_ruby_transition_gpu_parity(binary, replace(case, track=track, style=style), times)


@pytest.mark.parametrize('mutation', ['feature', 'schema', 'ink', 'advance', 'metrics_id', 'different_id',
                                      'inline_outline', 'wipe_box'])
@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_native_vector_guide_rejects_forgeries_and_keeps_scene(binary, project, mutation, target):
    ir = ir_for(binary, guide_source(project))
    bad = copy.deepcopy(ir)
    cell = bad['skia_layout']['lines'][0]['cells'][0]
    if mutation == 'feature': bad['required_features'].remove('p05_vector_guides')
    elif mutation == 'schema': bad['skia_layout']['vector_guide_schema'] = 2
    elif mutation == 'ink': cell['metrics']['ink'][2] += 0.5
    elif mutation == 'advance': cell['metrics']['advance'] = 50.0
    elif mutation == 'metrics_id': del cell['metrics']['vector_glyph_id']
    elif mutation == 'different_id': cell['metrics']['vector_glyph_id'] = 'g_' + 'f' * 64
    elif mutation == 'inline_outline':
        char = bad['track']['lines'][0]['chars'][0]
        char['vector_glyph'] = bad['vector_glyphs'][char.pop('vector_glyph_id')]
    else: cell['wipe_right'] += 2
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir); before, _ = collect(process, [750])
        with pytest.raises(NativeRendererError):
            configure(process, bad)
        after, _ = collect(process, [750], generation=2)
    assert np.array_equal(before[0], after[0])


@pytest.mark.parametrize('mutation', ['schema', 'feature', 'bool', 'version'])
def test_old_vector_guide_sidecar_rejected_before_selection(binary, monkeypatch, mutation):
    process = SkiaRendererProcess(binary, selection=selection('raster'))
    try: ready = NativeRendererProcess.start(process)
    finally: process.abort(reason='vector guide handshake fixture')
    if mutation == 'schema': del ready['vector_guide_schema']
    elif mutation == 'feature': ready['profiles'].remove('p05_vector_guides')
    else: ready['vector_guide_schema'] = True if mutation == 'bool' else 2
    sent = []
    monkeypatch.setattr(NativeRendererProcess, 'start', lambda self: ready)
    monkeypatch.setattr(SkiaRendererProcess, 'abort', lambda *a, **k: None)
    monkeypatch.setattr(SkiaRendererProcess, '_send', lambda self, p: sent.append(p))
    with pytest.raises(NativeRendererError, match='导唱符'):
        SkiaRendererProcess(binary).start()
    assert not sent


@pytest.mark.parametrize('kind', ['prefix', 'inline'])
def test_vector_guide_gpu_raster_and_out_of_order_seek(binary, project, kind):
    ir = ir_for(binary, guide_source(project, kind, CURVES), dpr=1.5)
    order = [2200, 601, 99, 900, 599, 1401, 350, 899, 1399, 199]
    results = []
    for target in ('gpu', 'raster'):
        with SkiaRendererProcess(binary, selection=selection(target)) as process:
            configure(process, ir)
            ordered, _ = collect(process, sorted(order))
            shuffled, _ = collect(process, order, generation=2)
        by_time = dict(zip(sorted(order), ordered))
        assert all(np.array_equal(by_time[t], f) for t, f in zip(order, shuffled))
        assert not np.array_equal(by_time[199 if kind == 'prefix' else 599], by_time[2200])
        results.append(ordered)
    for a, b in zip(*results):
        def premult(p):
            p = p.astype(float); p[..., :3] *= p[..., 3:] / 255
            return p
        delta = abs(premult(a) - premult(b))
        assert delta.mean() < .15 and np.mean(delta.max(axis=-1) > 16) < .003


@pytest.mark.parametrize('gpu', [True, False])
def test_real_vector_guide_preview(binary, project, monkeypatch, tmp_path, gpu):
    run_preview(binary, monkeypatch, tmp_path, guide_source(project, 'prefix', CURVES), gpu, False)


CASES = [*[(kind, 'square', semantics, dpr, False, 'none') for kind in ('prefix', 'inline')
           for semantics in ('legacy', 'n3_1074') for dpr in (1, 1.5)],
         ('replacement', 'square', 'n3_1074', 1.5, False, 'none'), ('anchored', 'square', 'legacy', 1, False, 'none'),
         ('prefix', 'curves', 'legacy', 1.5, False, 'none'), ('inline', 'curves', 'n3_1074', 1, False, 'none'),
         ('prefix', 'square', 'n3_1074', 1.5, True, 'none'), ('inline', 'curves', 'legacy', 1.5, False, 'no_wipe')]


@GPU
@pytest.mark.parametrize('case', CASES, ids=lambda c: '-'.join(map(str, c)))
def test_vector_guides_match_actual_gpu(binary, project, case):
    kind, shape, semantics, dpr, reverse, effect = case
    track, style = guide_source(project, kind, SQUARE if shape == 'square' else CURVES,
                                reverse=reverse, effect=effect, semantics=semantics)
    assert_gpu_parity(binary, track, style, dpr, case)
