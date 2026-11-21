"""One authored cell can use explicit faces without splitting its clock."""
import copy
from dataclasses import replace
import json
import sys
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest
from PyQt6.QtCore import QChar
from PyQt6.QtGui import QFontDatabase

from skia_text_fixture import make_font
from test_subtitle_skia_fallback import ExplicitFonts
from test_subtitle_skia_protocol import application, binary, collect, selection, sample
from test_subtitle_skia_protocol import test_actual_g5_preview_uses_skia_even_when_gpu_off as run_preview
from test_subtitle_skia_protocol import test_product_export_short_mp4_uses_skia as run_mp4
from test_subtitle_skia_ruby_groups import configure, colors
from krok_helper.subtitle_render.domain.models import SubtitleStyleScheme
from krok_helper.subtitle_render.domain.timing import TimingChar, TimingLine, TimingTrack, RubyAnnotation
from krok_helper.subtitle_render.engine.render.skia_ir import build_skia_render_ir
from krok_helper.subtitle_render.engine.text.font_weight import clear_font_weight_cache
from krok_helper.subtitle_render.native.backend import NativeRendererError, NativeRendererProcess
from krok_helper.subtitle_render.native.protocol import apply_resolved_font_faces
from krok_helper.subtitle_render.native.skia_backend import SkiaRendererProcess
from krok_helper.subtitle_render.native.skia_fonts import SkiaFontResolver
from krok_helper.subtitle_render.native.skia_text import SkiaTextMetricsProcess


class MixedFonts(ExplicitFonts):
    allow_mixed_fonts = True


@pytest.fixture
def source(tmp_path, application):
    from fontTools.ttLib import TTFont
    paths=[]
    for name, keep in [('Primary', {32,65,87,105,102,97,225,0x301}),
                       ('Han', {32,0x5b57}), ('Hangul', {32,0xd55c}), ('Union', None)]:
        path=make_font(tmp_path/f'{name}.ttf',family=f'LinK Mixed {name}',ps_name=f'LinK-Mixed-{name}')
        with TTFont(path,recalcTimestamp=False) as font:
            if keep is not None:
                for cmap in font['cmap'].tables:
                    cmap.cmap={k:v for k,v in cmap.cmap.items() if k in keep}
            font.save(path)
        paths.append(path)
    handles=[QFontDatabase.addApplicationFont(str(path)) for path in paths]
    assert min(handles)>=0
    scripts={QChar.Script.Script_Han:'LinK Mixed Han', QChar.Script.Script_Hangul:'LinK Mixed Hangul'}
    previous={s:QFontDatabase.applicationFallbackFontFamilies(s) for s in scripts}
    for script,family in scripts.items():QFontDatabase.setApplicationFallbackFontFamilies(script,[family])
    clear_font_weight_cache()
    track=TimingTrack(lines=[TimingLine(chars=[TimingChar('A字한fi',200),TimingChar('A',800),
        TimingChar('字한',1100)],end_ms=1600,track_line_index=0,
        display_start_override_ms=0,display_end_override_ms=4000)])
    style=replace(sample()[1],font_family='LinK Mixed Primary',font_size_px=50,
        karaoke_colors=colors('#FF0000','#00FF00'),line_y_margin_px=60,letter_spacing_px=3)
    try:yield track,style,MixedFonts(paths[:3],[{},{},{}]),paths[3]
    finally:
        for script,families in previous.items():QFontDatabase.setApplicationFallbackFontFamilies(script,families)
        for handle in handles:QFontDatabase.removeApplicationFont(handle)
        clear_font_weight_cache()


def ir_for(binary,source,**kwargs):
    track,style,resolve,*_=source
    with SkiaTextMetricsProcess(binary) as process:
        ir=build_skia_render_ir(track,style,resolve_font=resolve,measure=process.measure,
                               width=800,height=600,fps=60,**kwargs)
    apply_resolved_font_faces(ir)
    return ir


@pytest.mark.parametrize('semantics',['legacy','n3_1074'])
@pytest.mark.parametrize('dpr',[1,1.5])
def test_mixed_cell_keeps_one_clock_and_authored_cell_glyphs(binary,source,semantics,dpr,tmp_path):
    track,style,resolve,union=source
    ir=ir_for(binary,(track,replace(style,layout_semantics=semantics),resolve),dpr=dpr)
    layout=ir['skia_layout'];cells=layout['lines'][0]['cells']
    assert len(cells)==len(track.lines[0].chars)==3
    assert 'p05_mixed_fonts' in ir['required_features'] and layout['mixed_font_schema']==1
    mixed=cells[0]['metrics']
    assert mixed['selected_font']==-1
    assert [(r['byte_start'],r['byte_end'],r['font_index']) for r in mixed['font_runs']]==[
        (0,1,0),(1,4,1),(4,7,2),(7,9,0)]
    # cell_glyph_schema=1: this mark-free cell keeps nominal f and i glyphs.
    assert [(g['cluster_start'],g['cluster_end'],g['font_index']) for g in mixed['glyphs']]==[
        (0,1,0),(1,4,1),(4,7,2),(7,8,0),(8,9,0)]
    assert cells[1]['metrics']['selected_font']==0 and 'font_runs' not in cells[1]['metrics']
    assert ir['track']['lines'][0]['resolved_intervals']==[[200,800],[800,1100],[1100,1600]]
    output=[]
    for target in ['gpu','raster']:
        with SkiaRendererProcess(binary,selection=selection(target)) as process:
            configure(process,ir)
            frames,_=collect(process,[100,450,900,1500,1700,450])
        assert np.array_equal(frames[1],frames[-1])
        assert not np.array_equal(frames[1],frames[4])
        output.append(frames)
    for a,b in zip(*output,strict=True):assert np.abs(a.astype(float)-b).mean()<.3
    if semantics=='legacy' and dpr==1:
        (tmp_path/'mixed-font-layout.json').write_text(json.dumps({'cells':cells,
            'intervals':ir['track']['lines'][0]['resolved_intervals'],
            'metrics_identity':layout['metrics_identity']},sort_keys=True),encoding='utf-8')


def test_existing_complete_face_still_wins_before_any_mixed_fallback(binary,source):
    track,style,resolve,union=source
    resolve=MixedFonts([*resolve.paths,union],[{},{},{},{}])
    ir=ir_for(binary,(track,style,resolve))
    cell=ir['skia_layout']['lines'][0]['cells'][0]['metrics']
    assert cell['selected_font']==3 and 'font_runs' not in cell


def test_mixed_geometry_matches_identical_union_glyphs_before_any_rasterization(binary,source):
    track,style,resolve,union=source
    track=replace(track,lines=[replace(track.lines[0],chars=[TimingChar('A字한',200),TimingChar('字한A',900)])])
    a=ir_for(binary,(track,style,resolve))
    b=ir_for(binary,(track,style,MixedFonts([union],[{}])))
    for x,y in zip(a['skia_layout']['lines'][0]['cells'],b['skia_layout']['lines'][0]['cells'],strict=True):
        for key in ['origin_x','baseline_y','wipe_left','wipe_right']:assert x[key]==y[key],(key,x,y)
        for key in ['advance','ink','design_advance','design_left_bearing','design_right_bearing']:
            assert x['metrics'][key]==y['metrics'][key],(key,x,y)


def test_mixed_face_geometry_normalizes_size_scale_and_different_em_units(binary,source,tmp_path):
    from fontTools.ttLib import TTFont
    from fontTools.ttLib.scaleUpem import scale_upem
    track,style,resolve,_=source
    with TTFont(resolve.paths[1],recalcTimestamp=False) as font:
        scale_upem(font,2000);font.save(resolve.paths[1])
    resolve.options=[{}, {'size':75,'scale_x':1.5,'synthetic_italic':True},
                     {'size':40,'scale_x':.75,'synthetic_bold':True}]
    ir=ir_for(binary,(track,style,resolve))
    cell=ir['skia_layout']['lines'][0]['cells'][0]['metrics']
    assert cell['selected_font']==-1
    # The trailing mark-free fi keeps nominal f+i advances (cell_glyph_schema=1).
    assert cell['advance']==pytest.approx(30+112.5+30.6+15+10,abs=.001)
    # All design terms use the primary face's logical em; raw 2000 and 1000
    # design units must never be added directly.
    assert cell['design_advance']==pytest.approx(600+2250+612+300+200,abs=.001)
    assert cell['glyphs'][2]['x']==pytest.approx(142.5,abs=.001)
    with SkiaRendererProcess(binary,selection=selection('raster')) as process:configure(process,ir)


@pytest.mark.parametrize('text',['a\u0301字','字fi한','한A字'])
def test_combining_and_ligature_clusters_are_not_split_between_faces(binary,source,text):
    track,style,resolve,_=source
    track=replace(track,lines=[replace(track.lines[0],chars=[TimingChar(text,200)])])
    cell=ir_for(binary,(track,style,resolve))['skia_layout']['lines'][0]['cells'][0]['metrics']
    assert cell['selected_font']==-1
    if text.startswith('a'):
        assert cell['glyphs'][0]['cluster_end']==3 and cell['glyphs'][0]['font_index']==0
    assert all(g['id']>0 for g in cell['glyphs'])


def test_missing_mark_cannot_be_stitched_to_a_base_from_another_face(binary,source):
    from fontTools.ttLib import TTFont
    track,style,resolve,_=source
    for index,path in enumerate(resolve.paths):
        with TTFont(path,recalcTimestamp=False) as font:
            for cmap in font['cmap'].tables:
                cmap.cmap.pop(225,None);cmap.cmap.pop(0x301,None)
                if index==1:cmap.cmap[0x301]='acutecomb'
            font.save(path)
    track=replace(track,lines=[replace(track.lines[0],chars=[TimingChar('a\u0301字',200)])])
    with pytest.raises(NativeRendererError,match='完整字形'):ir_for(binary,(track,style,resolve))


@pytest.mark.parametrize('text,reason',[('a\u0361b字','连结字素'),('A👩\u200d💻字','连结字素'),
                                       ('Aب字','跨字体连接'),('Aक字','跨字体连接')])
def test_unverified_joined_scripts_remain_explicitly_gated(binary,source,text,reason):
    from fontTools.ttLib import TTFont
    track,style,resolve,_=source
    with TTFont(resolve.paths[0],recalcTimestamp=False) as font:
        for cmap in font['cmap'].tables:
            cmap.cmap.update({ord('b'):'A',0x361:'acutecomb',0x628:'A',0x915:'A'})
        font.save(resolve.paths[0])
    track=replace(track,lines=[replace(track.lines[0],chars=[TimingChar(text,200)])])
    with pytest.raises(NativeRendererError,match=reason):ir_for(binary,(track,style,resolve))


@pytest.mark.parametrize('mutation',['glyph_font','font_run','byte_boundary','identity','feature','schema','flag','flag_type'])
def test_mixed_snapshot_tampering_preserves_old_scene(binary,source,mutation):
    ir=ir_for(binary,source);bad=copy.deepcopy(ir);cell=bad['skia_layout']['lines'][0]['cells'][0]['metrics']
    if mutation=='glyph_font':cell['glyphs'][1]['font_index']=0
    elif mutation=='font_run':cell['font_runs'][1]['font_index']=0
    elif mutation=='byte_boundary':cell['font_runs'][1]['byte_end']=3
    elif mutation=='identity':cell['font_runs'][1]['font_identity']='wrong'
    elif mutation=='feature':bad['required_features'].remove('p05_mixed_fonts')
    elif mutation=='schema':bad['skia_layout']['mixed_font_schema']=2
    else:
        run=bad['skia_layout']['runs'][bad['skia_layout']['lines'][0]['cells'][0]['run_index']]
        run['mixed_fallback']=False if mutation=='flag' else 1
    with SkiaRendererProcess(binary,selection=selection('raster')) as process:
        configure(process,ir);before,_=collect(process,[450])
        with pytest.raises(NativeRendererError):configure(process,bad)
        after,_=collect(process,[450],generation=2)
    assert np.array_equal(before[0],after[0])


def test_platform_worker_exports_every_required_physical_face(binary,source,monkeypatch):
    from krok_helper.subtitle_render.native.skia_font_preparation import iter_prepare_skia_fallbacks
    from krok_helper.subtitle_render.engine.text import font_fallback
    track,style,*_=source
    list(iter_prepare_skia_fallbacks(track,style))
    monkeypatch.setattr(font_fallback,'prewarm_fallback',lambda *a:pytest.fail('worker font discovery'))
    with ThreadPoolExecutor(max_workers=1) as pool:
        ir=pool.submit(ir_for,binary,(track,style,SkiaFontResolver())).result(timeout=20)
    cell=ir['skia_layout']['lines'][0]['cells'][0]['metrics']
    assert cell['selected_font']==-1
    assert len({r['font_identity'] for r in cell['font_runs']})==3
    with SkiaRendererProcess(binary,selection=selection('raster')) as process:configure(process,ir)


def test_lower_ruby_reference_strings_can_use_multiple_explicit_faces(binary,source):
    from fontTools.ttLib import TTFont
    from krok_helper.subtitle_render.engine.ruby.skia_reference import measure_references
    from krok_helper.subtitle_render.engine.ruby.lower_semantics import lower_style
    track,style,resolve,_=source
    for index,path in enumerate(resolve.paths[:2]):
        with TTFont(path,recalcTimestamp=False) as font:
            for cmap in font['cmap'].tables:
                if not cmap.isUnicode():continue
                if index==0:
                    cmap.cmap.update({ord(c):'A' for c in 'abcdefghijklmnopqrstuvwxyz'})
                    cmap.cmap.update({ord(c):'cjk' for c in '国あいうえアイウエオ'})
                    for c in 'gn':cmap.cmap.pop(ord(c),None)
                else:cmap.cmap.update({ord(c):'cjk' for c in 'gnおなぐ'})
            font.save(path)
    track=replace(track,lines=[replace(track.lines[0],chars=[TimingChar('A字한',200)])],rubies=[
        RubyAnnotation('A字한','なぐ',pos_start_ms=200,pos_end_ms=1600,
            target_line_index=0,target_char_start=0,target_char_end=1)])
    style=replace(style,lower_ruby_enabled=True,_lower_romaji_options=(False,)*4,
        ruby_font_size_px=25,ruby_stroke_width_px=0,ruby_stroke2_enabled=False)
    ir=ir_for(binary,(track,style,resolve))
    assert len(ir['skia_layout']['lines'][0]['ruby_groups'])==2
    assert ir['skia_layout']['lines'][0]['cells'][0]['metrics']['selected_font']==-1
    with SkiaTextMetricsProcess(binary) as process:
        references=measure_references([(style,lower_style(style))],resolve_font=resolve,measure=process.measure)
    assert references[0].font_chains
    with SkiaRendererProcess(binary,selection=selection('gpu')) as process:
        configure(process,ir);frames,_=collect(process,[450,1100,450])
    assert np.array_equal(frames[0],frames[-1]) and frames[0][...,3].max()>0


@pytest.mark.parametrize('target',['gpu','raster'])
def test_mixed_main_with_ruby_role_and_shared_timing(binary,source,target):
    track,style,resolve,_=source
    chars=[TimingChar('A字한',200,role_label='lead',source_span_start_ms=200,source_span_end_ms=1400,
        source_span_index=0,source_span_count=2),TimingChar('A',200,
        source_span_start_ms=200,source_span_end_ms=1400,source_span_index=1,source_span_count=2)]
    track=replace(track,lines=[replace(track.lines[0],chars=chars,end_ms=1400)],rubies=[
        RubyAnnotation('A字한','AA',reading_parts=['A','','A'],reading_part_ms=[400,700],
            pos_start_ms=200,pos_end_ms=1400,target_line_index=0,target_char_start=0,target_char_end=1)])
    style=replace(style,ruby_font_size_px=25,ruby_stroke_width_px=0,ruby_stroke2_enabled=False,
        custom_style_schemes={'lead':SubtitleStyleScheme(font_size_px=60,
            karaoke_colors=colors('#800000FF','#80FFFF00'))})
    ir=ir_for(binary,(track,style,resolve))
    assert ir['skia_layout']['lines'][0]['cells'][0]['metrics']['selected_font']==-1
    assert len(ir['skia_layout']['lines'][0]['ruby_groups'])==1
    assert len(ir['track']['lines'][0]['resolved_intervals'])==2
    with SkiaRendererProcess(binary,selection=selection(target)) as process:
        configure(process,ir);frames,_=collect(process,[100,450,700,1100,1500,450])
    assert np.array_equal(frames[1],frames[-1]) and frames[1][...,3].max()>0


@pytest.mark.parametrize('gpu,missing_loader',[(True,False),(False,False),(True,True)])
def test_real_mixed_preview(binary,source,monkeypatch,tmp_path,gpu,missing_loader):
    run_preview(binary,monkeypatch,tmp_path,source[:2],gpu,missing_loader)


@pytest.mark.parametrize('gpu,missing_loader',[(True,False),(False,False),(True,True)])
def test_real_mixed_mp4(binary,source,monkeypatch,tmp_path,gpu,missing_loader):
    run_mp4(binary,monkeypatch,tmp_path,source[:2],gpu,missing_loader)


@pytest.mark.skipif(sys.platform!='win32',reason='Direct2D controlled union-font reference')
@pytest.mark.parametrize('semantics',['legacy','n3_1074'])
@pytest.mark.parametrize('dpr',[1,1.5])
def test_mixed_paths_match_d2d_gpu_with_equivalent_controlled_union_face(binary,source,semantics,dpr):
    from test_subtitle_render_gpu_backend import _render_g1_frames
    track,style,resolve,union=source
    track=replace(track,lines=[replace(track.lines[0],chars=[TimingChar('A字한',200),TimingChar('字한A',900)])])
    style=replace(style,layout_semantics=semantics)
    ir=ir_for(binary,(track,style,resolve),dpr=dpr)
    # D2D's existing TimingChar uses one outline face. A deterministic union
    # face contains the exact same source glyphs/metrics for this GPU oracle;
    # it is not a claim that production D2D already supports mixed-face cells.
    times=[199,200,450,899,900,1200,1599,1600,1700]
    with NativeRendererProcess() as process:
        _,raw=_render_g1_frames(process,replace(style,font_family='LinK Mixed Union'),tuple(times),
            force_warp=False,track=track,width=800,height=600,dpr=dpr,realization_enabled=False)
    with SkiaRendererProcess(binary,selection=selection('gpu')) as process:
        configure(process,ir);frames,_=collect(process,times)
        union_ir=ir_for(binary,(track,style,MixedFonts([union],[{}])),dpr=dpr)
        configure(process,union_ir);union_frames,_=collect(process,times,generation=2)
    assert all(np.array_equal(a,b) for a,b in zip(frames,union_frames,strict=True))
    refs=[np.frombuffer(p,np.uint8).reshape(round(600*dpr),round(800*dpr),4) for p in raw]
    def box(p):
        y,x=np.where(p[...,3]>127);return np.array([x.min(),y.min(),x.max(),y.max()])
    for t,a,b in zip(times,refs,frames,strict=True):
        assert np.max(np.abs(box(a)-box(b)))<=1,(t,box(a),box(b))
        def sung(p):return (p[...,1].astype(float)*p[...,3]/255**2).sum()
        full_a,full_b=sung(refs[-1]),sung(frames[-1])
        width=(full_a+full_b)/(2*(box(a)[3]-box(a)[1]+1))
        assert abs(sung(a)/full_a-sung(b)/full_b)*width<=1,t


def test_mixed_run_limit_fails_explicitly(binary,source):
    track,style,resolve,_=source
    track=replace(track,lines=[replace(track.lines[0],chars=[TimingChar('A字'*513,200)])])
    with pytest.raises(NativeRendererError,match='分段数量超限'):ir_for(binary,(track,style,resolve))


@pytest.mark.parametrize('target',['gpu','raster'])
def test_mixed_cells_apply_source_style_roll_offsets_and_dpr_once(binary,source,target):
    track,style,resolve,_=source;output=[]
    for shifted in [False,True]:
        ir=ir_for(binary,(replace(track,meta=replace(track.meta,offset_ms=137)) if shifted else track,
            replace(style,timing_offset_ms=229) if shifted else style,resolve),dpr=1.5,
            roll_shift_ms=400 if shifted else 0)
        with SkiaRendererProcess(binary,selection=selection(target)) as process:
            configure(process,ir)
            frames,_=collect(process,[t+(766 if shifted else 0) for t in [199,450,900,1500,1700]])
        output.append(frames)
    assert all(np.array_equal(a,b) for a,b in zip(*output,strict=True))


def test_mixed_color_face_cannot_be_replaced_by_a_later_monochrome_candidate(binary,source):
    from fontTools.ttLib import TTFont
    from fontTools.colorLib.builder import buildCOLR,buildCPAL
    track,style,resolve,_=source
    mono=resolve.paths[1].with_name('HanMono.ttf')
    mono.write_bytes(resolve.paths[1].read_bytes())
    resolve=MixedFonts([*resolve.paths,mono],[{},{},{},{}])
    valid=ir_for(binary,(track,style,resolve))
    with SkiaRendererProcess(binary,selection=selection('raster')) as process:
        configure(process,valid);before,_=collect(process,[450])
        with TTFont(resolve.paths[1],recalcTimestamp=False) as font:
            font['COLR']=buildCOLR({'cjk':[('A',0)]},version=0)
            font['CPAL']=buildCPAL([[(1.0,0.0,0.0,1.0)]])
            font.save(resolve.paths[1])
        invalid=ir_for(binary,(track,style,resolve))
        assert not invalid['skia_layout']['lines'][0]['cells'][0]['metrics']['outline_available']
        with pytest.raises(NativeRendererError,match='矢量轮廓'):configure(process,invalid)
        after,_=collect(process,[450],generation=2)
    assert np.array_equal(before[0],after[0])


def test_comparison_requires_exact_font_and_byte_indices():
    from scripts.compare_skia_text_metrics import compare
    for name in ['font_index','byte_start','byte_end']:
        with pytest.raises(ValueError):compare({name:1},{name:1.000001})


def test_old_sidecar_cannot_accept_mixed_cells(binary,monkeypatch):
    process=SkiaRendererProcess(binary,selection=selection('raster'))
    try:ready=NativeRendererProcess.start(process)
    finally:process.abort(reason='handshake fixture')
    ready.pop('mixed_font_schema')
    sent=[]
    monkeypatch.setattr(NativeRendererProcess,'start',lambda self:ready)
    monkeypatch.setattr(SkiaRendererProcess,'abort',lambda *a,**k:None)
    monkeypatch.setattr(SkiaRendererProcess,'_send',lambda self,p:sent.append(p))
    with pytest.raises(NativeRendererError,match='混合字体'):SkiaRendererProcess(binary).start()
    assert not sent
