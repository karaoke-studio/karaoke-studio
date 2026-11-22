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
from krok_helper.subtitle_render.domain.paint import PaintFill
from krok_helper.subtitle_render.domain.timing import GuideSymbol, RubyAnnotation
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


@pytest.mark.parametrize('change', ['bitmap', 'vertical', 'rtl', 'ruby', 'lower', 'role', 'stroke', 'shadow'])
def test_unvalidated_guide_combinations_stay_gated(binary, project, change):
    # Ruby and outlines are verified only with a line-level prefix guide.
    track, style = guide_source(project, 'inline' if change in {'ruby', 'stroke'} else 'prefix')
    line = track.lines[0]
    if change == 'bitmap':
        track = replace(track, lines=[replace(line, guide_symbol=replace(SQUARE, kind='bitmap', path_commands=(),
                                                                         bitmap_before_path='missing.png'))])
    elif change == 'vertical': style = replace(style, vertical=True)
    elif change == 'rtl': style = replace(style, right_to_left=True)
    elif change == 'ruby':
        track = replace(track, rubies=[RubyAnnotation('字', 'A', pos_start_ms=600, pos_end_ms=900, target_line_index=0,
                                                      target_char_start=1, target_char_end=2)])
    elif change == 'lower': style = replace(style, lower_ruby_enabled=True)
    elif change == 'role':
        track = replace(track, lines=[replace(line, guide_symbol=replace(SQUARE, role_labels=('lead',)))])
    elif change == 'shadow':
        style = replace(style, stroke_width_px=4, decoration_kind='shadow', shadow_offset_x=6, shadow_offset_y=4)
    else: style = replace(style, stroke_width_px=4)
    with pytest.raises(ValueError):
        ir_for(binary, (track, style))


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
        for mutation in ('feature', 'unverified'):
            bad = copy.deepcopy(ir)
            if mutation == 'feature':
                bad['required_features'].remove('p05_vector_guide_composition')
            else:
                bad['required_features'].append('p06_static_shadow')
            with pytest.raises(NativeRendererError):
                configure(process, bad)
        after, _ = collect(process, [950], generation=2)
    assert before[0].any() and np.array_equal(before[0], after[0])


@pytest.mark.parametrize('scanline', [False, True], ids=['gradient', 'scanline'])
def test_faded_gradient_with_leading_replacement_guide(binary, project, scanline):
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
    if scanline:
        style = replace(style, scanline_enabled=True, scanline_forward_enabled=True, scanline_mode='color',
                        scanline_color='#FFFFFF', scanline_width_px=20, scanline_glow_px=3)
    ir = ir_for(binary, (track, style))
    feature = 'p07_faded_scanline_prefix_guides' if scanline else 'p06_animated_singing_gradient_fill'
    assert {feature, 'p05_vector_guide_composition', 'p06_ruby_outline_gradient'} <= set(ir['required_features'])
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
def test_faded_solid_replacement_guides_match_actual_gpu(binary, application, tmp_path):
    from test_subtitle_skia_character_fade import assert_ruby_transition_gpu_parity

    case, track = p00_guide(tmp_path)
    line = track.lines[0]
    guide = replace(line.guide_symbol, replacement_prefix=tuple(char.text for char in line.chars[:3]))
    track = replace(track, lines=[replace(line, guide_symbol=guide)])
    style = replace(case.style, karaoke_anim='utopia', entry_anim='fade', exit_anim='fade',
                    latin_stroke2_enabled=False)
    times = (300, 450, 650, 1500, 2600, 3700, 5100, 5300, 5500)
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
