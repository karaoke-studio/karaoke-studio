"""Explicit physical font chains reach final glyph drawing and shared timing."""
from collections import Counter
import copy
from dataclasses import replace
import hashlib
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest
from PyQt6.QtCore import QChar
from PyQt6.QtGui import QFontDatabase

from test_subtitle_skia_protocol import application, binary, collect, selection, sample
from test_subtitle_skia_protocol import test_actual_g5_preview_uses_skia_even_when_gpu_off as run_preview
from test_subtitle_skia_protocol import test_product_export_short_mp4_uses_skia as run_mp4
from test_subtitle_skia_ruby_groups import configure, colors
from skia_text_fixture import make_font
from krok_helper.subtitle_render.domain.models import TimingChar, TimingLine, TimingTrack, SubtitleStyleScheme
from krok_helper.subtitle_render.domain.timing import RubyAnnotation
from krok_helper.subtitle_render.engine.render.skia_ir import build_skia_render_ir
from krok_helper.subtitle_render.engine.text.font_weight import clear_font_weight_cache
from krok_helper.subtitle_render.native.backend import NativeRendererError, NativeRendererProcess
from krok_helper.subtitle_render.native.protocol import apply_resolved_font_faces
from krok_helper.subtitle_render.native.skia_backend import SkiaRendererProcess
from krok_helper.subtitle_render.native.skia_font_preparation import iter_prepare_skia_fallbacks
from krok_helper.subtitle_render.native.skia_fonts import SkiaFontResolver
from krok_helper.subtitle_render.native.skia_text import SkiaTextMetricsProcess


class ExplicitFonts:
    def __init__(self, paths, options=None):
        self.paths, self.options = paths, options or [{}, {}]

    def resource(self, index):
        path = self.paths[index]
        return {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}

    def __call__(self, style):
        return self.resource(0), self.options[0]

    def resolve_cell_fonts(self, style, text):
        return [(self.resource(i), options) for i, options in enumerate(self.options)]


@pytest.fixture
def source(tmp_path, application):
    from fontTools.ttLib import TTFont
    paths = []
    for name, width, ascent, descent in [('Primary', 600, 800, 200), ('Backup', 900, 1000, 400)]:
        path = make_font(tmp_path / f'{name}.ttf', family=f'LinK Fallback {name}', a_width=width,
                         ps_name=f'LinK-Fallback-{name}', ascent=ascent, descent=descent)
        with TTFont(path, recalcTimestamp=False) as font:
            for cmap in font['cmap'].tables:
                if cmap.isUnicode():
                    cmap.cmap.update({ord(c): 'A' for c in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ-'})
                    cmap.cmap.update({ord(c): 'cjk' for c in '国あいうえおアイウエオなぐ'})
                    if name == 'Primary':
                        for c in '字한なぐng': cmap.cmap.pop(ord(c), None)
            font.save(path)
        paths.append(path)
    handles = [QFontDatabase.addApplicationFont(str(p)) for p in paths]
    assert min(handles) >= 0
    scripts = [QChar.Script.Script_Han, QChar.Script.Script_Hangul,
               QChar.Script.Script_Hiragana, QChar.Script.Script_Latin]
    previous = {script: QFontDatabase.applicationFallbackFontFamilies(script) for script in scripts}
    for script in scripts: QFontDatabase.setApplicationFallbackFontFamilies(script, ['LinK Fallback Backup'])
    clear_font_weight_cache()
    style = replace(sample()[1], font_family='LinK Fallback Primary', font_size_px=50,
                    karaoke_colors=colors('#FF0000', '#00FF00'), letter_spacing_px=3,
                    custom_style_schemes={'lead': SubtitleStyleScheme(font_family='LinK Fallback Primary',
                        font_size_px=60, karaoke_colors=colors('#800000FF', '#80FFFF00'))})
    line = TimingLine(chars=[TimingChar(c, 200+i*200, role_label='lead' if i==2 else None)
                             for i, c in enumerate(['A','字','한','fi'])], end_ms=1400,
                      track_line_index=0, display_start_override_ms=0, display_end_override_ms=3000)
    try: yield TimingTrack(lines=[line]), style, ExplicitFonts(paths)
    finally:
        for script, families in previous.items(): QFontDatabase.setApplicationFallbackFontFamilies(script, families)
        for handle in handles: QFontDatabase.removeApplicationFont(handle)
        clear_font_weight_cache()


def ir_for(binary, source, **kwargs):
    track, style, resolve = source
    with SkiaTextMetricsProcess(binary) as process:
        ir = build_skia_render_ir(track, style, resolve_font=resolve, measure=process.measure,
                                  width=800, height=600, fps=60, **kwargs)
    apply_resolved_font_faces(ir)
    return ir


@pytest.mark.parametrize('semantics', ['legacy', 'n3_1074'])
@pytest.mark.parametrize('dpr', [1, 1.5])
def test_final_chain_draws_primary_and_fallback_with_shared_clock(binary, source, semantics, dpr, tmp_path):
    track, style, resolve = source
    style = replace(style, layout_semantics=semantics)
    ir = ir_for(binary, (track, style, resolve), dpr=dpr)
    assert 'p05_font_fallback' in ir['required_features'] and ir['skia_layout']['font_chain_schema'] == 1
    cells = ir['skia_layout']['lines'][0]['cells']
    assert [c['metrics']['selected_font'] for c in cells] == [0,1,1,0]
    assert cells[0]['baseline_y'] == cells[1]['baseline_y'] == cells[2]['baseline_y']
    assert [c.text for c in track.lines[0].chars] == ['A','字','한','fi']
    output = []
    for target in ['gpu','raster']:
        with SkiaRendererProcess(binary, selection=selection(target)) as process:
            configure(process, ir)
            frames, _ = collect(process, [100,500,1000,1500,500])
        assert np.array_equal(frames[1], frames[-1])
        assert not np.array_equal(frames[0], frames[2])
        output.append(frames)
    for a,b in zip(*output, strict=True):
        assert np.abs(a.astype(float)-b).mean() < .3
    if semantics == 'legacy' and dpr == 1:
        # Only content identities and numeric geometry, never local paths.
        (tmp_path/'fallback-layout.json').write_text(json.dumps({'cells': cells,
            'intervals': ir['track']['lines'][0]['resolved_intervals'],
            'metrics_identity': ir['skia_layout']['metrics_identity']}, sort_keys=True), encoding='utf-8')


def test_chain_instances_have_independent_simulations_size_and_scale(binary, source):
    _, style, resolve = source
    fonts = {str(i): resolve.resource(i) for i in range(2)}
    options = [{'synthetic_bold': True, 'synthetic_italic': False, 'scale_x': .75},
               {'synthetic_bold': False, 'synthetic_italic': True, 'size': 65, 'scale_x': 1.5}]
    with SkiaTextMetricsProcess(binary) as process:
        run = {'font_ids': ['0','1'], 'size': 50, **options[0], 'font_options': options, 'cells': ['字']}
        chain = process.measure(fonts, [run])[0]
        direct = process.measure(fonts, [{'font_ids': ['1'], **options[1], 'cells': ['字']}])[0]
    cell = dict(chain['cells'][0], selected_font=0)
    assert cell == direct['cells'][0]
    assert chain['font_metrics']['ascent'] == pytest.approx(40, abs=1e-4)
    assert len(chain['font_chain']) == 2 and chain['font_metrics'] != direct['font_metrics']


@pytest.mark.parametrize('mutation', ['order','selected','options','nan','count','entry','unknown','feature','schema'])
def test_bad_chain_rejected_without_replacing_valid_scene(binary, source, mutation):
    ir = ir_for(binary, source)
    bad = copy.deepcopy(ir)
    run = next(r for r in bad['skia_layout']['runs'] if len(r['font_ids']) == 2)
    if mutation == 'order': run['font_ids'].reverse()
    elif mutation == 'selected': bad['skia_layout']['lines'][0]['cells'][1]['metrics']['selected_font'] = 0
    elif mutation == 'options': run['font_options'][1]['scale_x'] = 1.5
    elif mutation == 'nan': run['font_options'][1]['size'] = float('nan')
    elif mutation == 'count': run['font_options'].pop()
    elif mutation == 'entry': run['font_options'][1] = False
    elif mutation == 'unknown': run['font_options'][1]['rtl'] = True
    elif mutation == 'feature': bad['required_features'].remove('p05_font_fallback')
    elif mutation == 'schema': bad['skia_layout']['font_chain_schema'] = 2
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        configure(process, ir)
        before, _ = collect(process, [700])
        with pytest.raises(NativeRendererError): configure(process, bad)
        after, _ = collect(process, [700], generation=2)
    assert np.array_equal(before[0], after[0])


def test_unused_fallback_replacement_invalidates_identity(binary, source):
    from fontTools.ttLib import TTFont
    track, style, resolve = source
    track = replace(track, lines=[replace(track.lines[0], chars=[TimingChar('A',200)])])
    a = ir_for(binary, (track, style, resolve))
    with TTFont(resolve.paths[1], recalcTimestamp=False) as font:
        font['head'].fontRevision += .125
        font.save(resolve.paths[1])
    b = ir_for(binary, (track, style, resolve))
    assert a['skia_layout']['metrics_identity'] != b['skia_layout']['metrics_identity']
    assert a['skia_layout']['lines'] == b['skia_layout']['lines']


def test_no_candidate_covering_entire_cell_fails_instead_of_stitching_fonts(binary, source):
    from fontTools.ttLib import TTFont
    track, style, resolve = source
    with TTFont(resolve.paths[1], recalcTimestamp=False) as font:
        for cmap in font['cmap'].tables: cmap.cmap.pop(ord('A'), None)
        font.save(resolve.paths[1])
    track = replace(track, lines=[replace(track.lines[0], chars=[TimingChar('A字',200)])])
    with pytest.raises(NativeRendererError, match='完整字形'): ir_for(binary, (track, style, resolve))


@pytest.mark.parametrize('target', ['gpu','raster'])
def test_platform_preparation_exports_physical_faces_and_worker_never_discovers(binary, source, target, monkeypatch):
    from krok_helper.subtitle_render.native.skia_font_preparation import iter_prepare_skia_fallbacks
    from krok_helper.subtitle_render.engine.text import font_fallback
    track, style, _ = source
    list(iter_prepare_skia_fallbacks(track, style))
    resolver = SkiaFontResolver()
    monkeypatch.setattr(font_fallback, 'prewarm_fallback', lambda *a: pytest.fail('worker font discovery'))
    # The GUI loop waits on a worker, exactly as product configuration does.
    with ThreadPoolExecutor(max_workers=1) as pool:
        ir = pool.submit(ir_for, binary, (track, style, resolver)).result(timeout=20)
    assert [c['metrics']['selected_font'] for c in ir['skia_layout']['lines'][0]['cells']] == [0,1,1,0]
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, [700])
    assert frames[0][...,3].max() > 0


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows monochrome lyric symbol font and Direct2D reference')
def test_original_grapheme_corpus_uses_the_legacy_symbol_font(binary, application, tmp_path, monkeypatch):
    from fontTools.ttLib import TTFont
    from krok_helper.subtitle_render.native import skia_fonts
    from scripts.subtitle_gpu_baseline.cases import build_cases
    from test_subtitle_skia_character_fade import assert_ruby_transition_gpu_parity

    case = next(case for case in build_cases(tmp_path) if case.case_id == 'grapheme_fallback')
    list(iter_prepare_skia_fallbacks(case.track, case.style))
    with monkeypatch.context() as worker:
        worker.setattr(skia_fonts, 'gui_font_instance', lambda *a, **k: pytest.fail('worker font discovery'))
        with ThreadPoolExecutor(max_workers=1) as pool:
            ir = pool.submit(ir_for, binary, (case.track, case.style, SkiaFontResolver())).result(timeout=20)
    layout = ir['skia_layout']
    line, = layout['lines']
    symbol_path = None
    for cell in line['cells'][-2:]:
        run = layout['runs'][cell['run_index']]
        selected = cell['metrics']['selected_font']
        assert selected == 0
        assert cell['metrics']['outline_available']
        path = layout['fonts'][run['font_ids'][selected]]['path']
        assert symbol_path is None or path == symbol_path
        symbol_path = path
    with TTFont(symbol_path) as font:
        assert 'Segoe UI Symbol' in {record.toUnicode() for record in font['name'].names if record.nameID == 1}
        assert 'COLR' not in font
    assert_ruby_transition_gpu_parity(binary, case, (1500, 2600, 3575, 4125, 4400))


@pytest.mark.parametrize('target', ['gpu','raster'])
@pytest.mark.parametrize('discovery', ['explicit','platform_worker'])
def test_upper_and_lower_readings_use_the_same_explicit_chain(binary, source, target, discovery, monkeypatch):
    track, style, resolve = source
    track = replace(track, lines=[replace(track.lines[0], chars=[TimingChar('字',200)])], rubies=[
        RubyAnnotation('字','なぐ',reading_parts=['な','','ぐ'],reading_part_ms=[400,700],
            pos_start_ms=200,pos_end_ms=1400,target_line_index=0,target_char_start=0,target_char_end=1)])
    style = replace(style, custom_style_schemes={}, lower_ruby_enabled=True,
        _lower_romaji_options=(False,False,False,False), ruby_font_size_px=25,
        ruby_stroke_width_px=0,ruby_stroke2_enabled=False)
    if discovery == 'platform_worker':
        from krok_helper.subtitle_render.native.skia_font_preparation import iter_prepare_skia_fallbacks
        from krok_helper.subtitle_render.engine.text import font_fallback
        list(iter_prepare_skia_fallbacks(track, style))
        monkeypatch.setattr(font_fallback, 'prewarm_fallback', lambda *a: pytest.fail('worker discovery'))
        with ThreadPoolExecutor(max_workers=1) as pool:
            ir = pool.submit(ir_for, binary, (track, style, SkiaFontResolver())).result(timeout=20)
    else:
        ir = ir_for(binary, (track, style, resolve))
    groups = ir['skia_layout']['lines'][0]['ruby_groups']
    assert len(groups) == 2
    assert [u['metrics']['selected_font'] for u in groups[0]['units']] == [1,1]
    assert 1 in [u['metrics']['selected_font'] for u in groups[1]['units']]
    assert ir['track']['rubies'][0]['reading_parts'] == ['な','','ぐ']
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, [100,700,900,1500,700])
    assert np.array_equal(frames[1], frames[-1]) and frames[1][...,3].max() > 0


@pytest.mark.parametrize('gpu,missing_loader', [(True,False),(False,False),(True,True)])
def test_real_fallback_preview(binary, source, monkeypatch, tmp_path, gpu, missing_loader):
    run_preview(binary, monkeypatch, tmp_path, source[:2], gpu, missing_loader)


@pytest.mark.parametrize('gpu,missing_loader', [(True,False),(False,False),(True,True)])
def test_real_fallback_mp4(binary, source, monkeypatch, tmp_path, gpu, missing_loader):
    run_mp4(binary, monkeypatch, tmp_path, source[:2], gpu, missing_loader)


def test_old_font_chain_sidecar_rejected_before_backend_selection(binary, monkeypatch):
    process = SkiaRendererProcess(binary, selection=selection('raster'))
    try: ready = NativeRendererProcess.start(process)
    finally: process.abort(reason='handshake fixture')
    ready.pop('font_chain_schema')
    sent = []
    monkeypatch.setattr(NativeRendererProcess, 'start', lambda self: ready)
    monkeypatch.setattr(SkiaRendererProcess, 'abort', lambda *a,**k: None)
    monkeypatch.setattr(SkiaRendererProcess, '_send', lambda self, payload: sent.append(payload))
    with pytest.raises(NativeRendererError, match='字体链'): SkiaRendererProcess(binary).start()
    assert not sent


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('semantics', ['legacy','n3_1074'])
@pytest.mark.parametrize('dpr', [1,1.5])
def test_platform_fallback_matches_d2d_gpu(binary, source, semantics, dpr):
    from test_subtitle_render_gpu_backend import _render_g1_frames
    from krok_helper.subtitle_render.native.skia_font_preparation import iter_prepare_skia_fallbacks
    track, style, _ = source
    # Keep separate glyph bands simple so the 1 physical pixel budget is
    # sensitive to face, baseline, slot width and actual sung coverage.
    track = replace(track, lines=[replace(track.lines[0], chars=[TimingChar('A',200),
        TimingChar('字',500),TimingChar('한',900)])])
    style = replace(style, layout_semantics=semantics, custom_style_schemes={},
                    font_weight=400, line_y_margin_px=60)
    list(iter_prepare_skia_fallbacks(track, style))
    times = [199,200,350,500,700,900,1100,1400,1500]
    with NativeRendererProcess() as process:
        _, raw = _render_g1_frames(process, style, tuple(times), force_warp=False,
                                   track=track, width=800,height=600,dpr=dpr)
    with SkiaRendererProcess(binary, selection=selection('gpu')) as process:
        process.configure(track, style,width=800,height=600,fps=60,dpr=dpr)
        frames,_ = collect(process,times)
    refs = [np.frombuffer(p,np.uint8).reshape(round(600*dpr),round(800*dpr),4) for p in raw]
    def box(p):
        y,x = np.where(p[...,3]>127)
        return np.array([x.min(),y.min(),x.max(),y.max()])
    for t,a,b in zip(times,refs,frames,strict=True):
        assert np.max(np.abs(box(a)-box(b)))<=1,(t,box(a),box(b))
        def sung(p): return (p[...,1].astype(float)*p[...,3]/255**2).sum()
        full_a,full_b = sung(refs[-1]),sung(frames[-1])
        width = (full_a+full_b)/(2*(box(a)[3]-box(a)[1]+1))
        assert abs(sung(a)/full_a-sung(b)/full_b)*width<=1,t


def test_chain_order_can_skip_multiple_missing_candidates(binary, source):
    track, style, resolve = source
    resolver = ExplicitFonts([resolve.paths[0],resolve.paths[0],resolve.paths[1]], [{},{},{}])
    ir = ir_for(binary, (track,style,resolver))
    assert [c['metrics']['selected_font'] for c in ir['skia_layout']['lines'][0]['cells']] == [0,2,2,0]
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        configure(process,ir)
        frames,_ = collect(process,[700])
    assert frames[0][...,3].max() > 0


def test_platform_font_replacement_invalidates_prewarmed_chain(binary, source, tmp_path):
    from krok_helper.subtitle_render.native.skia_font_preparation import iter_prepare_skia_fallbacks
    track,style,_ = source
    resolver = SkiaFontResolver()
    list(iter_prepare_skia_fallbacks(track,style))
    before = ir_for(binary,(track,style,resolver))
    path = make_font(tmp_path/'replacement.ttf',family='LinK Fallback Replacement',
                     ps_name='LinK-Fallback-Replacement',a_width=1000)
    handle = QFontDatabase.addApplicationFont(str(path))
    assert handle >= 0
    script = QChar.Script.Script_Han
    previous = QFontDatabase.applicationFallbackFontFamilies(script)
    try:
        QFontDatabase.setApplicationFallbackFontFamilies(script,['LinK Fallback Replacement'])
        clear_font_weight_cache()
        list(iter_prepare_skia_fallbacks(track,style))
        after = ir_for(binary,(track,style,resolver))
        a,b = [ir['skia_layout']['lines'][0]['cells'][1]['metrics'] for ir in (before,after)]
        assert a['font_identity'] != b['font_identity']
        assert before['skia_layout']['metrics_identity'] != after['skia_layout']['metrics_identity']
        with SkiaRendererProcess(binary,selection=selection('raster')) as process:
            configure(process,after)
    finally:
        QFontDatabase.setApplicationFallbackFontFamilies(script,previous)
        QFontDatabase.removeApplicationFont(handle)
        clear_font_weight_cache()


@pytest.fixture
def mutable_primary(source, tmp_path, monkeypatch):
    """Pin platform discovery to one physical path whose bytes change in place.

    Both variants only differ in the primary ``A`` advance, so every table and
    the file size are identical; the restored mtime defeats any stat heuristic.
    """
    from fontTools.ttLib import TTFont
    from krok_helper.subtitle_render.native import skia_fonts
    variants = []
    for advance in (600, 700):
        with TTFont(source[2].paths[0], recalcTimestamp=False) as font:
            font['hmtx']['A'] = (advance, 50)
            font.save(tmp_path / f'variant-{advance}.ttf')
        variants.append((tmp_path / f'variant-{advance}.ttf').read_bytes())
    assert len(variants[0]) == len(variants[1]) and variants[0] != variants[1]
    path = tmp_path / 'mutable-primary.ttf'
    path.write_bytes(variants[0])
    stamp = path.stat().st_mtime_ns
    # Capture utime's round-trip precision (DrvFS may truncate nanoseconds).
    os.utime(path, ns=(stamp, stamp))
    stamp = path.stat().st_mtime_ns

    def write(index):
        path.write_bytes(variants[index])
        os.utime(path, ns=(stamp, stamp))
        assert (path.stat().st_size, path.stat().st_mtime_ns) == (len(variants[0]), stamp)
        return hashlib.sha256(variants[index]).hexdigest()

    discover = skia_fonts.resolve_font_instance
    monkeypatch.setattr(skia_fonts, 'resolve_font_instance',
                        lambda *a, **k: replace(discover(*a, **k), resource_path=str(path)))
    list(iter_prepare_skia_fallbacks(*source[:2]))
    return path, write


def primary_digest(ir, path):
    digests = {f['sha256'] for f in ir['skia_layout']['fonts'].values() if f['path'] == str(path)}
    assert len(digests) == 1  # One snapshot never mixes two contents of one path.
    return digests.pop()


def test_reused_resolver_consumes_in_place_replacement_across_preparations(binary, source, mutable_primary):
    track, style, _ = source
    path, write = mutable_primary
    resolver = SkiaFontResolver()
    old = write(0)
    before = ir_for(binary, (track, style, resolver))
    assert primary_digest(before, path) == old
    new = write(1)
    after = ir_for(binary, (track, style, resolver))
    assert primary_digest(after, path) == new != old
    cells = [ir['skia_layout']['lines'][0]['cells'] for ir in (before, after)]
    assert [c['metrics']['selected_font'] for c in cells[1]] == [0, 1, 1, 0]
    assert cells[0][0]['metrics']['font_identity'] != cells[1][0]['metrics']['font_identity']
    assert cells[0][1]['metrics']['font_identity'] == cells[1][1]['metrics']['font_identity']
    assert before['skia_layout']['metrics_identity'] != after['skia_layout']['metrics_identity']
    for target in ['gpu', 'raster']:
        with SkiaRendererProcess(binary, selection=selection(target)) as process:
            with pytest.raises(NativeRendererError, match='SHA-256'): configure(process, before)
            configure(process, after)
            frames, _ = collect(process, [700])
        assert frames[0][..., 3].max() > 0


def test_preparation_reads_each_physical_face_once_across_instances(binary, source, mutable_primary, monkeypatch):
    from krok_helper.subtitle_render.native import skia_fonts
    track, _, _ = source
    path, write = mutable_primary
    # The same physical face as a bold+italic role keeps independent simulations.
    style = replace(source[1], custom_style_schemes={'lead': SubtitleStyleScheme(
        font_family='LinK Fallback Primary', font_size_px=60, font_weight=700, italic=True)})
    track = replace(track, lines=track.lines*8)
    reads, digest = [], skia_fonts._file_sha256
    monkeypatch.setattr(skia_fonts, '_file_sha256', lambda p: reads.append(str(p)) or digest(p))
    resolver = SkiaFontResolver()
    for _ in range(2):
        reads.clear()
        ir = ir_for(binary, (track, style, resolver))
        # Primary and Backup once per preparation, not per cell, instance or batch.
        assert sorted(Counter(reads).values()) == [1, 1] and str(path) in reads
        runs = ir['skia_layout']['runs']
        assert {(r['synthetic_bold'], r['synthetic_italic']) for r in runs} == {(False, False), (True, True)}
        assert len({r['font_ids'][0] for r in runs}) == 1


def test_removed_face_fails_then_recovers_by_content_identity(binary, source, mutable_primary):
    track, style, _ = source
    path, write = mutable_primary
    resolver = SkiaFontResolver()
    original = ir_for(binary, (track, style, resolver))
    path.unlink()
    with pytest.raises(OSError): ir_for(binary, (track, style, resolver))
    new = write(1)
    replaced = ir_for(binary, (track, style, resolver))
    assert primary_digest(replaced, path) == new
    write(0)
    restored = ir_for(binary, (track, style, resolver))
    assert restored['skia_layout'] == original['skia_layout']
    assert replaced['skia_layout']['metrics_identity'] != original['skia_layout']['metrics_identity']


def test_replacement_during_one_preparation_is_rejected_not_mixed(binary, source, mutable_primary):
    track, style, _ = source
    path, write = mutable_primary
    track = replace(track, lines=[replace(track.lines[0], chars=[TimingChar('字',200)])], rubies=[
        RubyAnnotation('字','なぐ',pos_start_ms=200,pos_end_ms=1400,target_line_index=0,
                       target_char_start=0,target_char_end=1)])
    style = replace(style, custom_style_schemes={}, lower_ruby_enabled=True,
        _lower_romaji_options=(False,False,False,False), ruby_font_size_px=25,
        ruby_stroke_width_px=0, ruby_stroke2_enabled=False)
    list(iter_prepare_skia_fallbacks(track, style))
    resolver, requested = SkiaFontResolver(), []
    write(0)
    with SkiaTextMetricsProcess(binary) as process:
        def measure(fonts, runs):
            requested.append({f['sha256'] for f in fonts.values() if f['path'] == str(path)})
            try: return process.measure(fonts, runs)
            finally: write(1)  # Later batches of this same preparation see new bytes.
        with pytest.raises(NativeRendererError, match='SHA-256'):
            build_skia_render_ir(track, style, resolve_font=resolver, measure=measure,
                                 width=800, height=600, fps=60)
    assert len(requested) >= 2 and len(set.union(*requested)) == 1
    # The next canonical preparation starts a new snapshot.
    assert primary_digest(ir_for(binary, (track, style, resolver)), path) == write(1)


def test_selected_color_face_stays_gated_instead_of_skipping_to_monochrome(binary, source, tmp_path):
    from fontTools.ttLib import TTFont
    from skia_text_fixture import make_colr_font
    track,style,resolve = source
    with TTFont(resolve.paths[0],recalcTimestamp=False) as font:
        for cmap in font['cmap'].tables: cmap.cmap.pop(0x1f469,None)
        font.save(resolve.paths[0])
    color = make_colr_font(tmp_path/'color.ttf')
    resolver = ExplicitFonts([resolve.paths[0],color,resolve.paths[1]],[{},{},{}])
    valid = ir_for(binary,source)
    track = replace(track,lines=[replace(track.lines[0],chars=[TimingChar('👩',200)])])
    invalid = ir_for(binary,(track,style,resolver))
    assert invalid['skia_layout']['lines'][0]['cells'][0]['metrics']['selected_font'] == 1
    with SkiaRendererProcess(binary,selection=selection('raster')) as process:
        configure(process,valid)
        before,_ = collect(process,[700])
        with pytest.raises(NativeRendererError,match='矢量轮廓'): configure(process,invalid)
        after,_ = collect(process,[700],generation=2)
    assert np.array_equal(before[0],after[0])


def test_identical_chains_batch_all_unique_cells_once(binary, source):
    from krok_helper.subtitle_render.engine.text.skia_metrics import measure_track
    track,style,resolve = source
    track = replace(track,lines=track.lines*12)
    batches=[]
    with SkiaTextMetricsProcess(binary) as process:
        def measure(fonts,runs):
            batches.append(copy.deepcopy(runs))
            return process.measure(fonts,runs)
        measure_track(track,style,[style]*12,resolve_font=resolve,measure=measure)
    assert len(batches) == 1
    assert sum(len(r['cells']) for r in batches[0]) == 4
    assert len([r for r in batches[0] if len(r['font_ids'])>1]) == 2  # Main and role size.


def test_source_row_box_does_not_depend_on_whether_first_glyph_uses_fallback(binary, source):
    track,style,resolve = source
    output=[]
    for texts in [('A','字'),('字','A')]:
        line = replace(track.lines[0],chars=[TimingChar(c,200+i*300,role_label='lead')
                                            for i,c in enumerate(texts)])
        ir = ir_for(binary,(replace(track,lines=[line]),style,resolve))
        output.append(ir['skia_layout']['lines'][0]['cells'][0]['baseline_y'])
    assert output[0] == output[1]
