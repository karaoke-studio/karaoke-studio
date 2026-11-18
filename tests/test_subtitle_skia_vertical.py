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


@pytest.mark.parametrize('change', ['small_kana', 'rotated', 'corner', 'greek', 'multi', 'role', 'ruby', 'rtl',
                                    'dual', 'lower', 'offset'])
def test_unvalidated_vertical_combinations_stay_gated(binary, project, change):
    texts = {'small_kana': 'ゖ', 'rotated': '→', 'corner': '，', 'greek': 'θ', 'multi': '字字'}
    track, style = vertical_source(project, 'roles' if change == 'role' else 'plain')
    line = track.lines[0]
    if change in texts:
        track = replace(track, lines=[replace(line, chars=[replace(line.chars[0], text=texts[change]), *line.chars[1:]])])
    elif change == 'ruby':
        track = replace(track, rubies=[RubyAnnotation('字', 'A', pos_start_ms=200, pos_end_ms=600, target_line_index=0,
                                                      target_char_start=0, target_char_end=1)])
    elif change == 'rtl': style = replace(style, right_to_left=True)
    elif change == 'dual': style = replace(style, dual_line_layout=True, line_alignments=['left', 'right'])
    elif change == 'lower': style = replace(style, lower_ruby_enabled=True)
    elif change == 'offset': style = replace(style, line_offsets_x=[12])
    with pytest.raises(ValueError):
        ir_for(binary, (track, style))


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
