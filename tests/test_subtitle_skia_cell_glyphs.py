"""Authored-cell glyph policy (cell_glyph_schema=1) matches the existing GPU.

The existing GPU draws a lyric cell from its scalars' nominal cmap glyphs at
design advances, and only shapes a cell holding a supplementary scalar, or two
or more scalars with a mark/ZWJ. Its cell box always uses the nominal glyphs'
design metrics. Skia follows the same explicit, versioned rule.
"""
from dataclasses import replace
import copy
import sys

import numpy as np
import pytest
from PyQt6.QtGui import QFontDatabase

from test_subtitle_skia_rtl_flow import (application, binary, project, rtl_source, assert_gpu_parity, reverse_source,
                                         collect, configure, ir_for, selection)
from skia_text_fixture import make_font
from krok_helper.subtitle_render.engine.text.font_weight import clear_font_weight_cache
from krok_helper.subtitle_render.engine.text.skia_width import cell_geometry
from krok_helper.subtitle_render.domain.models import Style
from krok_helper.subtitle_render.native.backend import NativeRendererError, NativeRendererProcess
from krok_helper.subtitle_render.native.skia_backend import SkiaRendererProcess
from krok_helper.subtitle_render.native.skia_text import SkiaTextMetricsProcess


TEXTS = {'multi': ['fi', 'a\u0301', '字字', 'AA'], 'spaces': ['A ', '  ', ' a\u0301', '字 '],
         'features': ['AW', 'a\u0301', 'fi', '字字']}


def variant_font(path, kind):
    """``featured``: GPOS kern + mark anchors, no precomposed U+00E1 in cmap.
    ``unmapped``: precomposed U+00E1 only; the face maps no U+0301."""
    from fontTools.ttLib import TTFont
    from fontTools.feaLib.builder import addOpenTypeFeaturesFromString
    make_font(path, family=f'LinK P05 Cell {kind}', ps_name=f'LinK-P05-Cell-{kind}')
    with TTFont(path, recalcTimestamp=False) as font:
        for cmap in font['cmap'].tables:
            cmap.cmap.pop(225 if kind == 'featured' else 0x301, None)
        if kind == 'featured':
            addOpenTypeFeaturesFromString(font, '''
                feature kern { pos A W -75; } kern;
                markClass acutecomb <anchor 0 750> @top;
                feature mark { pos base a <anchor 250 700> mark @top; } mark;
            ''')
        font.save(path)
    return path


def measure(process, path, cells):
    return process.measure({'main': {'path': str(path)}}, [{'font_ids': ['main'], 'size': 50, 'cells': cells}])[0]['cells']


def placed(cell):
    return [(g['id'], g['x'], g['cluster_start'], g['cluster_end']) for g in cell['glyphs']]


def test_only_mark_joiner_or_supplementary_cells_use_opentype_shaping(binary, tmp_path):
    base, featured = make_font(tmp_path / 'base.ttf'), variant_font(tmp_path / 'featured.ttf', 'featured')
    with SkiaTextMetricsProcess(binary) as process:
        f, i, a, mark, cjk, woman = measure(process, base, ['f', 'i', 'a', '\u0301', '字', '\U0001F469'])
        fi, pair, mark_only, blank, ligated, emoji = measure(process, base, ['fi', '字字', '\u0301', '  ',
                                                                             'fi\u0301', '\U0001F469fi'])
        kerned, anchored = measure(process, featured, ['AW', 'a\u0301'])
    gid = lambda cell: cell['glyphs'][0]['id']
    # Nominal: one glyph per scalar at design advances, no liga.
    assert placed(fi) == [(gid(f), 0, 0, 1), (gid(i), 15, 1, 2)] and fi['advance'] == 25
    assert placed(pair) == [(gid(cjk), 0, 0, 3), (gid(cjk), 50, 3, 6)]
    # A lone mark is one UTF-16 unit: nominal, zero advance, never a dotted circle.
    assert [(g['x'], g['y']) for g in mark_only['glyphs']] == [(0, 0)] and mark_only['advance'] == 0
    assert len(blank['glyphs']) == 2 and blank['advance'] == 25
    # GPOS kerning is not applied to a mark-free cell.
    assert [g['x'] for g in kerned['glyphs']] == [0, 30] and kerned['advance'] == 70
    # A base+mark cell and a supplementary-plane cell are shaped: liga applies,
    # mark anchors are kept, and cluster boundaries stay HarfBuzz's.
    assert len(ligated['glyphs']) == 2 and gid(ligated) not in {gid(f), gid(i)}
    assert {(g['cluster_start'], g['cluster_end']) for g in ligated['glyphs']} == {(0, 4)}
    assert len(emoji['glyphs']) == 2 and emoji['glyphs'][0]['id'] == gid(woman)
    assert emoji['glyphs'][1]['id'] == gid(ligated)
    assert [(g['x'], g['y']) for g in anchored['glyphs']] == [(0, 0), (12.5, 2.5)]


def test_cell_box_metrics_come_from_authored_nominal_glyphs(binary, tmp_path):
    base, unmapped = make_font(tmp_path / 'base.ttf'), variant_font(tmp_path / 'unmapped.ttf', 'unmapped')
    style = Style(font_size_px=50)
    with SkiaTextMetricsProcess(binary) as process:
        decomposed, precomposed, fi = measure(process, base, ['a\u0301', '\u00e1', 'fi'])
        (missing,) = measure(process, unmapped, ['a\u0301'])
    design = lambda c: (c['design_advance'], c['design_left_bearing'], c['design_right_bearing'])
    # Same composed outline/ink, but the box uses a + acutecomb (rsb -50, clamped).
    assert placed(decomposed) == [(precomposed['glyphs'][0]['id'], 0, 0, 3)]
    assert decomposed['ink'] == precomposed['ink']
    assert design(decomposed) == (500, 50, -50) and design(precomposed) == (500, 50, 50)
    assert cell_geometry('a\u0301', decomposed, style)[0] == 22
    assert cell_geometry('\u00e1', precomposed, style)[0] == 24
    # fi: f + i advances, f's left and i's right bearing.
    assert design(fi) == (500, 50, 50) and cell_geometry('fi', fi, style)[0] == 24
    # The face maps no U+0301: HarfBuzz still composes U+00E1, while the box uses
    # a + glyph 0 exactly as the GPU's nominal metric query does.
    assert placed(missing)[0][2:] == (0, 3) and design(missing) == (1100, 50, 50)


@pytest.mark.parametrize('forgery', ['old_identity', 'ligature_glyph', 'composed_box'])
@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_forged_or_old_cell_snapshots_keep_the_previous_scene(binary, project, forgery, target):
    track, style = rtl_source(project)
    line = track.lines[0]
    track = replace(track, lines=[replace(line, chars=[replace(c, text=t) for c, t in zip(line.chars, TEXTS['multi'])])])
    ir = ir_for(binary, (track, replace(style, right_to_left=False)))
    bad = copy.deepcopy(ir)
    fi, decomposed = (c['metrics'] for c in bad['skia_layout']['lines'][0]['cells'][:2])
    assert fi['font_identity'].startswith('skia-m150-hb-ft-v7:')
    if forgery == 'old_identity':
        fi['font_identity'] = fi['font_identity'].replace('-v7:', '-v6:', 1)
    elif forgery == 'ligature_glyph':
        # A cell_glyph_schema-less (HarfBuzz liga) snapshot of the same cell.
        fi['glyphs'] = [{**fi['glyphs'][0], 'cluster_end': 2}]
    else:
        decomposed['design_right_bearing'] = 50
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir); before, _ = collect(process, [750])
        with pytest.raises(NativeRendererError):
            configure(process, bad)
        after, _ = collect(process, [750], generation=2)
    assert np.array_equal(before[0], after[0])


def cell_source(project, kind, *, reverse=False, effect='none'):
    base = {'roles': 'roles', 'negative': 'negative_gap'}.get(kind, 'plain')
    track, style = rtl_source(project, base, reverse=reverse, effect=effect)
    line = track.lines[0]
    texts = TEXTS['spaces' if kind == 'spaces' else 'multi']
    track = replace(track, lines=[replace(line, chars=[replace(c, text=t) for c, t in zip(line.chars, texts)])])
    return track, replace(style, right_to_left=False)


GPU = pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')


@GPU
@pytest.mark.parametrize('kind', ['multi', 'spaces', 'roles', 'negative'])
@pytest.mark.parametrize('semantics', ['legacy', 'n3_1074'])
@pytest.mark.parametrize('dpr', [1, 1.5])
def test_multicodepoint_cells_match_actual_gpu(binary, project, kind, semantics, dpr):
    track, style = cell_source(project, kind)
    assert_gpu_parity(binary, track, replace(style, layout_semantics=semantics), dpr, (kind, semantics, dpr))


@GPU
@pytest.mark.parametrize('reverse,effect', [(True, 'none'), (False, 'no_wipe'), (True, 'no_wipe')])
@pytest.mark.parametrize('semantics', ['legacy', 'n3_1074'])
def test_multicodepoint_singing_variants_match_actual_gpu(binary, project, reverse, effect, semantics):
    track, style = cell_source(project, 'multi', reverse=reverse, effect=effect)
    assert_gpu_parity(binary, track, replace(style, layout_semantics=semantics), 1.5, (reverse, effect, semantics))


@GPU
@pytest.mark.parametrize('alignment', ['left', 'center', 'right'])
@pytest.mark.parametrize('semantics', ['legacy', 'n3_1074'])
def test_multicodepoint_row_anchors_match_actual_gpu(binary, project, alignment, semantics):
    track, style = cell_source(project, 'multi')
    assert_gpu_parity(binary, track, replace(style, layout_semantics=semantics), 1.5,
                      (alignment, semantics), alignment=alignment)


@GPU
@pytest.mark.parametrize('kind', ['featured', 'unmapped'])
@pytest.mark.parametrize('semantics', ['legacy', 'n3_1074'])
@pytest.mark.parametrize('dpr', [1, 1.5])
def test_opentype_feature_fonts_match_actual_gpu(binary, project, tmp_path, kind, semantics, dpr):
    assert_feature_font_parity(binary, project, tmp_path, kind, semantics, dpr)


# horizontal_rtl_cell_schema=1: the same cells as whole RTL cells, singing either way.
@GPU
@pytest.mark.parametrize('kind', ['featured', 'unmapped'])
@pytest.mark.parametrize('semantics', ['legacy', 'n3_1074'])
@pytest.mark.parametrize('reverse', [False, True])
def test_rtl_opentype_feature_fonts_match_actual_gpu(binary, project, tmp_path, kind, semantics, reverse):
    assert_feature_font_parity(binary, project, tmp_path, kind, semantics, 1.5, rtl=True, reverse=reverse)


def assert_feature_font_parity(binary, project, tmp_path, kind, semantics, dpr, *, rtl=False, reverse=False):
    handle = QFontDatabase.addApplicationFont(str(variant_font(tmp_path / f'{kind}.ttf', kind)))
    assert handle >= 0
    clear_font_weight_cache()
    try:
        track, style = cell_source(project, 'multi', reverse=reverse)
        line = track.lines[0]
        track = replace(track, lines=[replace(line, chars=[replace(c, text=t)
                                                          for c, t in zip(line.chars, TEXTS['features'])])])
        style = replace(style, font_family=QFontDatabase.applicationFontFamilies(handle)[0], layout_semantics=semantics,
                        right_to_left=rtl)
        assert_gpu_parity(binary, track, style, dpr, (kind, semantics, dpr, rtl, reverse))
    finally:
        QFontDatabase.removeApplicationFont(handle)
        clear_font_weight_cache()


# vertical_metrics_schema=1: line-box ascent/descent follow the existing GPU's
# font metrics: OS/2 typo when USE_TYPO_METRICS is set, otherwise OS/2 win.
# hhea never decides the baseline (it is what Skia/FreeType used before).
# name: (hhea asc, hhea desc, typo asc, typo desc, win asc, win desc, USE_TYPO_METRICS)
VERTICAL_FONTS = {'win_over_hhea': (800, 200, 800, 200, 900, 400, False),
                  'hhea_ignored': (900, 400, 800, 200, 800, 200, False),
                  'typo_flag': (800, 200, 900, 400, 800, 200, True),
                  'typo_flag_win_ignored': (800, 200, 800, 200, 900, 400, True)}


def vertical_font(path, name, *, os2=True):
    from fontTools.ttLib import TTFont
    make_font(path, family=f'LinK Vertical {name}', ps_name=f'LinK-Vertical-{name}')
    font = TTFont(path, recalcTimestamp=False)
    hhea_up, hhea_down, typo_up, typo_down, win_up, win_down, use_typo = VERTICAL_FONTS.get(name, VERTICAL_FONTS['hhea_ignored'])
    font['hhea'].ascent, font['hhea'].descent = hhea_up, -hhea_down
    if os2:
        table = font['OS/2']
        table.sTypoAscender, table.sTypoDescender, table.usWinAscent, table.usWinDescent = typo_up, -typo_down, win_up, win_down
        table.version = max(table.version, 4)
        table.fsSelection = (table.fsSelection | 1 << 7) if use_typo else (table.fsSelection & ~(1 << 7))
    else:
        del font['OS/2']
    font.save(path)
    return path


@pytest.mark.parametrize('name,expected', [('win_over_hhea', (900, 400)), ('hhea_ignored', (800, 200)),
                                           ('typo_flag', (900, 400)), ('typo_flag_win_ignored', (800, 200))])
def test_line_box_metrics_follow_gpu_os2_rule(binary, tmp_path, name, expected):
    path = vertical_font(tmp_path / f'{name}.ttf', name)
    with SkiaTextMetricsProcess(binary) as process:
        run = process.measure({'f': {'path': str(path)}}, [{'font_ids': ['f'], 'size': 1000, 'cells': ['A']}])[0]
    assert (run['font_metrics']['ascent'], run['font_metrics']['descent']) == pytest.approx(expected, abs=1e-3)
    assert (run['cells'][0]['ascent'], run['cells'][0]['descent']) == pytest.approx(expected, abs=1e-3)
    assert run['font_metrics']['font_identity'].startswith('skia-m150-hb-ft-v7:')


def test_face_without_os2_keeps_hhea_line_box(binary, tmp_path):
    path = vertical_font(tmp_path / 'no-os2.ttf', 'no_os2', os2=False)
    with SkiaTextMetricsProcess(binary) as process:
        run = process.measure({'f': {'path': str(path)}}, [{'font_ids': ['f'], 'size': 1000, 'cells': ['A']}])[0]
    assert (run['font_metrics']['ascent'], run['font_metrics']['descent']) == pytest.approx((900, 400), abs=1e-3)


VERTICAL_CASES = [*[(name, sem, dpr, False, False, 'plain') for name in VERTICAL_FONTS
                    for sem, dpr in (('legacy', 1.5), ('n3_1074', 1))],
                  ('win_over_hhea', 'n3_1074', 1.5, True, True, 'roles'),
                  ('win_over_hhea', 'legacy', 1, True, False, 'plain'),
                  ('hhea_ignored', 'n3_1074', 1.5, False, True, 'no_wipe')]


@GPU
@pytest.mark.parametrize('case', VERTICAL_CASES, ids=lambda c: '-'.join(map(str, c)))
def test_line_box_metrics_match_actual_gpu(binary, project, tmp_path, case):
    name, semantics, dpr, rtl, reverse, kind = case
    handle = QFontDatabase.addApplicationFont(str(vertical_font(tmp_path / f'{name}.ttf', name)))
    assert handle >= 0
    clear_font_weight_cache()
    try:
        effect = 'no_wipe' if kind == 'no_wipe' else 'none'
        track, style = rtl_source(project, 'roles' if kind == 'roles' else 'plain', reverse=reverse, effect=effect)
        family = QFontDatabase.applicationFontFamilies(handle)[0]
        style = replace(style, font_family=family, right_to_left=rtl, layout_semantics=semantics)
        if kind == 'roles':
            style = replace(style, custom_style_schemes={'lead': replace(style.custom_style_schemes['lead'], font_family=family)})
        assert_gpu_parity(binary, track, style, dpr, case)
    finally:
        QFontDatabase.removeApplicationFont(handle)
        clear_font_weight_cache()


# empty_cell_schema=1: an authored empty-text TimingChar keeps its index, text and
# clock, draws nothing, and occupies the existing GPU's inkless box
# (size * space% * 25 / 100 / 10 + edge), exactly like Skia's cell_geometry.
EMPTY = {'mixed': ['A', '', 'W', '字'], 'edges': ['', 'A', 'W', ''], 'trailing': ['A', 'W', '字', ''],
         'empty_only': ['', '', '', '']}


def empty_source(project, texts='mixed', kind='plain', *, reverse=False, effect='none', rtl=False):
    if kind == 'shared':
        track, style = reverse_source(project, 'shared', effect)
        line = replace(track.lines[0], wipe_reverse=reverse)
        style = replace(style, karaoke_anim=effect, reverse_karaoke_anim=effect)
    else:
        track, style = rtl_source(project, kind, reverse=reverse, effect=effect)
        line = track.lines[0]
    line = replace(line, chars=[replace(c, text=t) for c, t in zip(line.chars, EMPTY[texts])])
    return replace(track, lines=[line]), replace(style, right_to_left=rtl)


@pytest.mark.parametrize('rtl', [False, True])
@pytest.mark.parametrize('reverse', [False, True])
def test_empty_cells_keep_authored_clocks_and_inkless_boxes(binary, project, rtl, reverse):
    track, style = empty_source(project, 'edges', rtl=rtl, reverse=reverse)
    ir = ir_for(binary, (track, style))
    lettered = ir_for(binary, (replace(track, lines=[replace(track.lines[0], chars=[
        replace(c, text=c.text or 'A') for c in track.lines[0].chars])]), style))
    cells = ir['skia_layout']['lines'][0]['cells']
    assert [c['text'] for c in ir['track']['lines'][0]['chars']] == EMPTY['edges']
    assert len(cells) == 4
    for index in (0, 3):
        metrics = cells[index]['metrics']
        assert metrics['glyphs'] == [] and metrics['ink'] == [0, 0, 0, 0] and metrics['design_advance'] == 0
        assert metrics['font_identity'].startswith('skia-m150-hb-ft-v7:')
        assert cells[index]['wipe_right'] - cells[index]['wipe_left'] == cell_geometry('', metrics, style)[0]
    size, space = int(style.font_size_px), int(style.space_width_percent)
    assert cell_geometry('', cells[0]['metrics'], style)[0] == size * space * 25 // 100 // 10
    # Clocks stay with the authored cells; nothing is dropped or merged.
    assert ir['track']['lines'][0]['resolved_intervals'] == lettered['track']['lines'][0]['resolved_intervals']
    if reverse:
        assert [c['clock_index'] for c in cells] == [3, 2, 1, 0]


@pytest.mark.parametrize('forgery', ['glyph', 'advance', 'text'])
@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_forged_empty_cell_snapshots_keep_the_previous_scene(binary, project, forgery, target):
    ir = ir_for(binary, empty_source(project))
    bad = copy.deepcopy(ir)
    empty, letter = (c['metrics'] for c in (bad['skia_layout']['lines'][0]['cells'][1], bad['skia_layout']['lines'][0]['cells'][0]))
    if forgery == 'glyph': empty['glyphs'] = copy.deepcopy(letter['glyphs'])
    elif forgery == 'advance': empty['design_advance'] = 1
    else: bad['track']['lines'][0]['chars'][1]['text'] = 'A'
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir); before, _ = collect(process, [750])
        with pytest.raises(NativeRendererError):
            configure(process, bad)
        after, _ = collect(process, [750], generation=2)
    assert np.array_equal(before[0], after[0])


@pytest.mark.parametrize('rtl', [False, True])
def test_empty_only_lines_draw_nothing_on_skia_and_actual_gpu(binary, project, rtl):
    track, style = empty_source(project, 'empty_only', rtl=rtl, reverse=rtl)
    ir = ir_for(binary, (track, style), dpr=1.5)
    assert len(ir['skia_layout']['lines'][0]['cells']) == 4
    times = [199, 700, 1500, 2200]
    for target in ('gpu', 'raster'):
        with SkiaRendererProcess(binary, selection=selection(target)) as process:
            configure(process, ir); frames, _ = collect(process, times)
        assert all(not f[..., 3].any() for f in frames)
    if sys.platform == 'win32':
        from test_subtitle_render_gpu_backend import _render_g1_frames
        with NativeRendererProcess() as process:
            _, raw = _render_g1_frames(process, style, tuple(times), force_warp=False, track=track,
                                       width=640, height=360, dpr=1.5, realization_enabled=False)
        assert all(not np.frombuffer(r, np.uint8)[3::4].any() for r in raw)


def test_empty_cells_gpu_raster_and_out_of_order_seek(binary, project):
    ir = ir_for(binary, empty_source(project, 'mixed', 'roles', reverse=True, rtl=True), dpr=1.5)
    order = [2200, 601, 199, 900, 599, 1401, 600, 899, 1399, 200]
    results = []
    for target in ('gpu', 'raster'):
        with SkiaRendererProcess(binary, selection=selection(target)) as process:
            configure(process, ir)
            ordered, _ = collect(process, sorted(order))
            shuffled, _ = collect(process, order, generation=2)
        by_time = dict(zip(sorted(order), ordered))
        assert all(np.array_equal(by_time[t], f) for t, f in zip(order, shuffled))
        results.append(ordered)
    for a, b in zip(*results):
        def premult(p):
            p = p.astype(float); p[..., :3] *= p[..., 3:] / 255
            return p
        delta = abs(premult(a) - premult(b))
        assert delta.mean() < .15 and np.mean(delta.max(axis=-1) > 16) < .003


EMPTY_CASES = [
    ('mixed', False, False, 'none', 'plain', 'legacy', 1.5, None), ('mixed', False, False, 'none', 'plain', 'n3_1074', 1, None),
    ('mixed', True, False, 'none', 'plain', 'n3_1074', 1.5, None), ('mixed', True, True, 'none', 'plain', 'legacy', 1, None),
    ('edges', False, True, 'none', 'plain', 'n3_1074', 1.5, None), ('edges', True, False, 'no_wipe', 'plain', 'legacy', 1.5, None),
    ('trailing', False, False, 'no_wipe', 'plain', 'n3_1074', 1, None), ('trailing', True, True, 'none', 'plain', 'n3_1074', 1.5, None),
    ('mixed', False, False, 'none', 'pause', 'legacy', 1.5, None), ('mixed', True, False, 'none', 'zero', 'n3_1074', 1.5, None),
    ('mixed', False, False, 'none', 'shared', 'n3_1074', 1.5, None), ('edges', True, True, 'none', 'shared', 'legacy', 1.5, None),
    ('mixed', False, True, 'none', 'roles', 'n3_1074', 1.5, None), ('mixed', True, False, 'none', 'negative_gap', 'legacy', 1.5, None),
    ('edges', False, False, 'none', 'plain', 'n3_1074', 1.5, 'left'), ('trailing', True, False, 'none', 'plain', 'n3_1074', 1.5, 'right'),
]


@GPU
@pytest.mark.parametrize('case', EMPTY_CASES, ids=lambda c: '-'.join(map(str, c)))
def test_empty_cells_match_actual_gpu(binary, project, case):
    texts, rtl, reverse, effect, kind, semantics, dpr, alignment = case
    track, style = empty_source(project, texts, kind, reverse=reverse, effect=effect, rtl=rtl)
    assert_gpu_parity(binary, track, replace(style, layout_semantics=semantics), dpr, case, alignment=alignment)


@GPU
@pytest.mark.parametrize('span', [(1, 2), (0, 3)], ids=['empty_base', 'spanning_empty'])
def test_upper_ruby_over_empty_cells_matches_actual_gpu(binary, project, span):
    from krok_helper.subtitle_render.domain.timing import RubyAnnotation
    track, style = empty_source(project, 'mixed')
    ruby = RubyAnnotation('X', 'A', pos_start_ms=200, pos_end_ms=900, target_line_index=0,
                          target_char_start=span[0], target_char_end=span[1])
    track = replace(track, rubies=[ruby])
    ir = ir_for(binary, (track, style), dpr=1.5)
    assert 'p05_resolved_ruby' in ir['required_features']
    assert_gpu_parity(binary, track, style, 1.5, ('ruby', span))
