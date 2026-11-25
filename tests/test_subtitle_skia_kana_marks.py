"""Decomposed kana use authored glyphs, with only font-supplied positioning."""
import copy
from dataclasses import replace
import json
import sys

import numpy as np
import pytest
from fontTools.ttLib import TTFont

from krok_helper.subtitle_render.native.backend import NativeRendererError, NativeRendererProcess
from krok_helper.subtitle_render.native.skia_backend import SkiaRendererProcess
from krok_helper.subtitle_render.native.skia_text import SkiaTextMetricsProcess
from skia_text_fixture import make_kana_font
from test_subtitle_skia_rtl_flow import (
    application, assert_gpu_parity, binary, collect, configure, ir_for, kana, kana_source, project, selection,
)
from test_subtitle_skia_text import run


TEXTS = ['か\u3099', 'は\u309a', 'ハ\u309a', 'う\u3099か']


@pytest.mark.parametrize('positioning,mark_x,following_x', [
    ('gpos', 950, 1000), ('gdef', 1000, 1000), ('nominal', 1000, 1100),
])
def test_kana_mark_measurement_preserves_source_clusters(binary, tmp_path, positioning, mark_x, following_x):
    path = make_kana_font(tmp_path / 'kana.ttf', gpos=positioning == 'gpos',
                          gdef_only=positioning == 'gdef', mark_advance=100)
    with SkiaTextMetricsProcess(binary) as process:
        cells = run(process, path, [*TEXTS, 'が', '\u3099'])
    for text, cell in zip(TEXTS, cells):
        glyphs = cell['glyphs']
        assert len(glyphs) == len(text)
        assert [(g['cluster_start'], g['cluster_end']) for g in glyphs[:2]] == [(0, 6)] * 2
        assert glyphs[1]['x'] == pytest.approx(mark_x * .048)
        assert glyphs[1]['y'] == pytest.approx(0)
        assert cell['design_advance'] == 1100 + (1000 if len(text) == 3 else 0)
    assert cells[3]['glyphs'][2]['x'] == pytest.approx(following_x * .048)
    assert len(cells[4]['glyphs']) == len(cells[5]['glyphs']) == 1
    assert cells[0]['glyphs'][0]['id'] != cells[4]['glyphs'][0]['id']


def test_kana_mark_mixed_boundary_keeps_base_and_mark_together(binary, tmp_path):
    def subset(filename, keep):
        path = make_kana_font(tmp_path / filename)
        with TTFont(path, recalcTimestamp=False) as font:
            for table in font['cmap'].tables:
                table.cmap = {k: v for k, v in table.cmap.items() if k in keep}
            font.save(path)
        return path

    fonts = {'latin': subset('latin.ttf', {65}), 'kana': subset('kana.ttf', {0x304b, 0x3099}),
             'base': subset('base.ttf', {0x304b}), 'mark': subset('mark.ttf', {0x3099})}
    resources = {key: {'path': str(path)} for key, path in fonts.items()}
    request = {'size': 48, 'cells': ['Aか\u3099'], 'mixed_fallback': True}
    with SkiaTextMetricsProcess(binary) as process:
        mixed = process.measure(resources, [dict(request, font_ids=['latin', 'kana'])])[0]['cells'][0]
        assert [(r['byte_start'], r['byte_end']) for r in mixed['font_runs']] == [(0, 1), (1, 7)]
        assert [(g['cluster_start'], g['cluster_end']) for g in mixed['glyphs']] == [(0, 1), (1, 7), (1, 7)]
        with pytest.raises(NativeRendererError, match='完整字形'):
            process.measure(resources, [dict(request, font_ids=['latin', 'base', 'mark'])])


@pytest.mark.parametrize('target', ['gpu', 'raster'])
@pytest.mark.parametrize('mutation', ['feature', 'parent', 'unproven'])
def test_kana_mark_forged_layout_preserves_previous_scene(binary, project, kana, target, mutation):
    ir = ir_for(binary, kana_source(project, kana, TEXTS))
    bad = copy.deepcopy(ir)
    if mutation == 'feature':
        bad['required_features'].remove('p05_horizontal_rtl_kana_marks')
    elif mutation == 'parent':
        bad['required_features'].remove('p05_horizontal_rtl_kana_cells')
    elif mutation == 'schema':
        bad['skia_layout']['kana_mark_schema'] = True
    else:
        bad['track']['lines'][0]['chars'][0]['text'] = 'か\u064e'
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        before, _ = collect(process, [750])
        with pytest.raises(NativeRendererError):
            configure(process, bad)
        after, _ = collect(process, [750], generation=2)
    np.testing.assert_array_equal(before[0], after[0])


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('kana', [False, True], indirect=True, ids=['nominal', 'gpos'])
@pytest.mark.parametrize('rtl,reverse,effect,kind,semantics,dpr', [
    (False, False, 'none', 'plain', 'n3_1074', 1.5),
    (True, False, 'none', 'negative_gap', 'n3_1074', 1.5),
    (True, True, 'none', 'roles', 'legacy', 1),
    (True, False, 'no_wipe', 'pause', 'legacy', 1.5),
    (False, True, 'no_wipe', 'zero', 'n3_1074', 1),
])
def test_kana_marks_match_actual_gpu(binary, project, kana, rtl, reverse, effect, kind, semantics, dpr):
    track, style = kana_source(project, kana, TEXTS, kind, rtl=rtl, reverse=reverse, effect=effect)
    assert_gpu_parity(binary, track, replace(style, layout_semantics=semantics), dpr,
                      (kana, rtl, reverse, effect, kind, semantics))


def test_kana_marks_keep_whole_cell_clocks_and_gpu_raster_seek(binary, project, kana, tmp_path):
    source = kana_source(project, kana, TEXTS, 'roles', reverse=True)
    ir = ir_for(binary, source, dpr=1.5)
    ltr = ir_for(binary, (source[0], replace(source[1], right_to_left=False)), dpr=1.5)
    lines, plain = ir['skia_layout']['lines'], ltr['skia_layout']['lines']
    assert 'p05_horizontal_rtl_kana_marks' in ir['required_features']
    assert 'p05_horizontal_rtl_kana_marks' not in ltr['required_features']
    assert ir['skia_layout']['kana_mark_schema'] == 1
    assert [c['metrics'] for c in lines[0]['cells']] == [c['metrics'] for c in plain[0]['cells']]
    assert [c['clock_index'] for c in lines[0]['cells']] == [3, 2, 1, 0]
    assert ir['track']['lines'][0]['resolved_intervals'] == ltr['track']['lines'][0]['resolved_intervals']
    (tmp_path / 'rtl-kana-mark-layout.json').write_text(json.dumps({
        'features': ir['required_features'], 'schema': ir['skia_layout']['kana_mark_schema'],
        'lines': lines, 'intervals': ir['track']['lines'][0]['resolved_intervals'],
    }, sort_keys=True), encoding='utf-8')
    frames = []
    for target in ('gpu', 'raster'):
        with SkiaRendererProcess(binary, selection=selection(target)) as process:
            configure(process, ir)
            ordered, _ = collect(process, [200, 599, 900, 2200])
            shuffled, _ = collect(process, [2200, 599, 900, 200], generation=2)
        assert all(np.array_equal(ordered[i], frame) for i, frame in zip([3, 1, 2, 0], shuffled))
        frames.append(ordered)
    for gpu, raster in zip(*frames):
        a, b = gpu.astype(float), raster.astype(float)
        a[..., :3] *= a[..., 3:] / 255
        b[..., :3] *= b[..., 3:] / 255
        delta = abs(a - b)
        assert delta.mean() < .15 and np.mean(delta.max(axis=-1) > 16) < .003
