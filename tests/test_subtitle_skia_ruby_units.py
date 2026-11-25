"""Part-local glyph normalization and clock units match the existing GPU."""
import copy
from dataclasses import replace
import json
import sys
import unicodedata

import numpy as np
import pytest
from PyQt6.QtGui import QFontDatabase

from test_subtitle_skia_lower import application, binary, prepared_source
from test_subtitle_skia_roles import colors
from test_subtitle_skia_protocol import collect, selection
from test_subtitle_skia_protocol import test_actual_g5_preview_uses_skia_even_when_gpu_off as run_preview
from test_subtitle_skia_protocol import test_product_export_short_mp4_uses_skia as run_mp4
from test_subtitle_skia_ruby_groups import configure
from krok_helper.subtitle_render.domain.timing import RubyAnnotation, TimingChar
from krok_helper.subtitle_render.engine.render.skia_ir import build_skia_render_ir
from krok_helper.subtitle_render.engine.ruby.skia_metrics import measure_timed_readings, measure_readings
from krok_helper.subtitle_render.engine.ruby.skia_timing import ruby_unit_clock
from krok_helper.subtitle_render.engine.text.font_weight import clear_font_weight_cache
from krok_helper.subtitle_render.native.backend import NativeRendererProcess, NativeRendererError
from krok_helper.subtitle_render.native.protocol import apply_resolved_font_faces
from krok_helper.subtitle_render.native.skia_backend import SkiaRendererProcess
from krok_helper.subtitle_render.native.skia_text import SkiaTextMetricsProcess


CASES={'nfc':('a\u0301','W'), 'kana':('カ\u3099','W'),
       'hangul':('\u1100','\u1161'), 'tie':('a\u0361','b')}


@pytest.fixture
def unit_source(prepared_source):
    from fontTools.ttLib import TTFont
    track,style,resolve=prepared_source
    paths=[resolve(replace(style,font_family=name))[0]['path'] for name in ['Main','Latin']]
    for path in paths:
        font=TTFont(path,recalcTimestamp=False)
        font.setGlyphOrder([*font.getGlyphOrder(),'tie'])
        font['glyf']['tie']=font['glyf']['space']
        font['hmtx'].metrics['tie']=(0,0)
        for table in font['cmap'].tables:
            if table.isUnicode(): table.cmap.update({0x1100:'hangul',0x1161:'hangul',0xAC00:'hangul',
                0x30AB:'cjk',0x30AC:'cjk',0x3099:'acutecomb',0x0361:'tie',ord('b'):'A'})
        font.save(path); font.close()
    handles=[QFontDatabase.addApplicationFont(path) for path in paths]
    assert min(handles)>=0
    clear_font_weight_cache()
    style=replace(style,lower_ruby_enabled=False,ruby_latin_font_stretch_pct=100,
        dual_line_layout=False,line_horizontal_layout='center',smart_horizontal='none',
        entry_anim='none',exit_anim='none',karaoke_anim='none',ruby_gap_px=10,
        line_y_margin_px=40,line_lead_in_ms=400,line_tail_ms=500,
        karaoke_colors=colors('#FF0000','#00FF00'),ruby_colors_follow_main=False,
        ruby_karaoke_colors=colors('#FF0000','#00FF00'))
    line=replace(track.lines[0],chars=[TimingChar('字',200),TimingChar('字',900)],end_ms=1700,
                 track_line_index=0,display_start_override_ms=0,display_end_override_ms=5000)
    try: yield replace(track,lines=[line],rubies=[]),style,resolve
    finally:
        for handle in handles: QFontDatabase.removeApplicationFont(handle)
        clear_font_weight_cache()


def reading(source,kind,*,lower=False,mode='reading_units'):
    track,style,resolve=source
    a,b=CASES[kind]
    ruby=RubyAnnotation('字字',a+b,reading_parts=[a,'',b],reading_part_ms=[400,800],
        pos_start_ms=200,pos_end_ms=1700,target_line_index=0,target_char_start=0,target_char_end=2)
    return replace(track,rubies=[ruby]),replace(style,lower_ruby_enabled=lower,ruby_main_progress_mode=mode),resolve


def ir_for(binary,source,**kwargs):
    track,style,resolve=source
    with SkiaTextMetricsProcess(binary) as metrics:
        ir=build_skia_render_ir(track,style,measure=metrics.measure,resolve_font=resolve,
                               width=900,height=600,fps=60,**kwargs)
    apply_resolved_font_faces(ir)
    return ir


@pytest.mark.parametrize('kind',list(CASES))
@pytest.mark.parametrize('target',['gpu','raster'])
def test_product_uses_part_local_glyph_units_and_preserves_source_clock(binary,unit_source,kind,target,tmp_path):
    source=reading(unit_source,kind)
    ir=ir_for(binary,source)
    a,b=CASES[kind]
    assert ir['track']['rubies'][0]['reading']==a+b
    assert ir['track']['rubies'][0]['reading_parts']==[a,'',b]
    units=ir['skia_layout']['lines'][0]['ruby_groups'][0]['units']
    assert [u['text'] for u in units]==[a,b]
    assert [u['glyph_text'] for u in units]==[unicodedata.normalize('NFC',a),unicodedata.normalize('NFC',b)]
    assert [u['interval_ms'] for u in units]==[[200,600],[1000,1700]]
    assert 'p05_ruby_units' in ir['required_features'] and ir['skia_layout']['ruby_unit_schema']==1
    if kind=='hangul': assert [u['glyph_text'] for u in units]!=[unicodedata.normalize('NFC',a+b)]
    with SkiaRendererProcess(binary,selection=selection(target)) as process:
        process.configure(*source[:2],width=900,height=600,fps=60)
        frames,_=collect(process,[199,450,600,750,950,1200,1699,1900,750])
    # At exactly end the GPU can still use the finishing cell's clip;
    # subsequent pause frames delegate to the next cell's starting edge.
    assert np.array_equal(frames[3],frames[4])
    assert np.array_equal(frames[3],frames[-1])
    if kind=='hangul' and target=='gpu':
        (tmp_path/'ruby-unit-layout.json').write_text(json.dumps({'sources':ir['track']['rubies'],
            'layout':ir['skia_layout']['lines'],'features':ir['required_features']},sort_keys=True),encoding='utf-8')


def test_timed_measurement_deduplicates_but_boundary_changes_geometry_and_identity(binary,unit_source):
    track,style,resolve=reading(unit_source,'hangul')
    ruby=track.rubies[0]; whole=replace(ruby,reading_parts=[],reading_part_ms=[])
    with SkiaTextMetricsProcess(binary) as metrics:
        batches=[]
        def measure(fonts,runs): batches.append(runs); return metrics.measure(fonts,runs)
        segmented=measure_timed_readings([(ruby,style)]*2,resolve_font=resolve,measure=measure)
        joined=measure_timed_readings([(whole,style)]*2,resolve_font=resolve,measure=measure)
    assert len(batches)==2 and sum(len(r['cells']) for r in batches[0])==2
    assert segmented.readings[0]==segmented.readings[1]
    assert len(segmented.readings[0].cells)==2 and len(joined.readings[0].cells)==1
    assert segmented.identity!=joined.identity


@pytest.mark.parametrize('units',[[],[('A',)],[(None,)], [('','A')], [('W','W')]])
def test_injected_units_cannot_drop_or_replace_source_text(units):
    fail=lambda *a,**k:pytest.fail('invalid units reached measurement')
    with pytest.raises((ValueError,TypeError)):
        measure_readings([('AA',None)],visual_units=units,resolve_font=fail,measure=fail)


@pytest.mark.parametrize('lower',[False,True])
@pytest.mark.parametrize('mode',['reading_units','checkpoint_segments'])
def test_normalized_units_work_with_lower_reading_and_both_main_modes(binary,unit_source,lower,mode):
    source=reading(unit_source,'nfc',lower=lower,mode=mode)
    ir=ir_for(binary,source)
    assert ir['track']['rubies'][0]['reading']=='a\u0301W'
    for target in ['gpu','raster']:
        with SkiaRendererProcess(binary,selection=selection(target)) as process:
            configure(process,ir); frames,_=collect(process,[450,750,1200,1900,750])
        assert np.array_equal(frames[1],frames[-1])


@pytest.mark.parametrize('mutation',['type', 'wrong_nfc', 'whole_nfc', 'cluster'])
def test_invalid_unit_projection_keeps_previous_scene(binary,unit_source,mutation):
    ir=ir_for(binary,reading(unit_source,'hangul'))
    broken=copy.deepcopy(ir); unit=broken['skia_layout']['lines'][0]['ruby_groups'][0]['units'][0]
    if mutation=='feature': broken['required_features'].remove('p05_ruby_units')
    elif mutation=='schema': broken['skia_layout']['ruby_unit_schema']=2
    elif mutation=='type': unit['glyph_text']=False
    elif mutation=='wrong_nfc': unit['glyph_text']='W'
    elif mutation=='whole_nfc': unit['glyph_text']='가'
    elif mutation=='cluster': unit['metrics']['glyphs'][0]['cluster_end']=99
    with SkiaRendererProcess(binary,selection=selection('raster')) as process:
        configure(process,ir); before,_=collect(process,[450])
        with pytest.raises(NativeRendererError): configure(process,broken)
        after,_=collect(process,[450],generation=2)
    assert np.array_equal(before[0],after[0])


def test_dangling_combining_mark_remains_explicitly_unsupported(binary,unit_source):
    track,style,_=reading(unit_source,'nfc')
    ruby=replace(track.rubies[0],reading='a\u0301\u0300',reading_parts=['a','\u0301\u0300'],reading_part_ms=[400])
    with SkiaRendererProcess(binary,selection=selection('raster')) as process:
        process.configure(track,style,width=900,height=600,fps=60)
        before,_=collect(process,[450])
        with pytest.raises(NativeRendererError,match='独立组合标记'):
            process.configure(replace(track,rubies=[ruby]),style,width=900,height=600,fps=60)
        after,_=collect(process,[450],generation=2)
    assert np.array_equal(before[0],after[0])


@pytest.mark.parametrize('target',['gpu','raster'])
def test_unit_projection_time_offsets_and_dpr_are_applied_once(binary,unit_source,target):
    track,style,_=reading(unit_source,'hangul')
    result=[]
    for shift in [False,True]:
        with SkiaRendererProcess(binary,selection=selection(target)) as process:
            process.configure(replace(track,meta=replace(track.meta,offset_ms=137)) if shift else track,
                replace(style,timing_offset_ms=229) if shift else style,width=900,height=600,fps=60,dpr=1.5,
                roll_shift_ms=400 if shift else 0)
            frames,_=collect(process,[t+(766 if shift else 0) for t in [199,450,750,1200,1699,1900]])
        result.append(frames)
    assert all(np.array_equal(a,b) for a,b in zip(*result,strict=True))


@pytest.mark.parametrize('gpu,missing_loader',[(True,False),(False,False),(True,True)])
def test_real_part_units_preview_uses_skia(binary,unit_source,monkeypatch,tmp_path,gpu,missing_loader):
    run_preview(binary,monkeypatch,tmp_path,reading(unit_source,'hangul')[:2],gpu,missing_loader)


@pytest.mark.parametrize('gpu,missing_loader',[(True,False),(False,False),(True,True)])
def test_real_part_units_mp4_uses_skia(binary,unit_source,monkeypatch,tmp_path,gpu,missing_loader):
    run_mp4(binary,monkeypatch,tmp_path,reading(unit_source,'nfc')[:2],gpu,missing_loader)


@pytest.mark.skipif(sys.platform!='win32',reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('kind',list(CASES))
@pytest.mark.parametrize('mode',['reading_units','checkpoint_segments'])
@pytest.mark.parametrize('dpr',[1,1.5])
@pytest.mark.parametrize('semantics',['legacy','n3_1074'])
def test_part_local_units_match_d2d_gpu(binary,unit_source,kind,mode,dpr,semantics):
    from test_subtitle_render_gpu_backend import _render_g1_frames
    source=reading(unit_source,kind,mode=mode); track,style,_=source
    style=replace(style,layout_semantics=semantics)
    times=[199,200,450,599,600,750,999,1000,1200,1699,1700,1900]
    with NativeRendererProcess() as process:
        _,raw=_render_g1_frames(process,style,tuple(times),force_warp=False,track=track,width=900,height=600,dpr=dpr)
    with SkiaRendererProcess(binary,selection=selection('gpu')) as process:
        process.configure(track,style,width=900,height=600,fps=60,dpr=dpr)
        frames,_=collect(process,times)
    refs=[np.frombuffer(p,np.uint8).reshape(round(600*dpr),round(900*dpr),4) for p in raw]
    def boxes(p):
        mask=p[...,3]>127; ys=np.flatnonzero(mask.any(axis=1))
        bands=[v for v in np.split(ys,np.flatnonzero(np.diff(ys)>1)+1) if len(v)]
        return [np.array([np.flatnonzero(mask[b[0]:b[-1]+1].any(axis=0)).min(),b[0],
                         np.flatnonzero(mask[b[0]:b[-1]+1].any(axis=0)).max(),b[-1]]) for b in bands]
    templates=boxes(refs[-1]); assert len(templates)==2
    for t,a,b in zip(times,refs,frames,strict=True):
        left,right=boxes(a),boxes(b)
        assert len(left)==len(right)==2
        assert np.max(np.abs(np.array(left)-right))<=1,(kind,mode,dpr,t,left,right)
        for template in templates:
            lo,hi=max(template[1]-2,0),template[3]+3
            def sung(p): return (p[lo:hi,:,1].astype(float)*p[lo:hi,:,3]/255**2).sum()
            full_a,full_b=sung(refs[-1]),sung(frames[-1])
            width=(full_a+full_b)/(2*(template[3]-template[1]+1))
            assert abs(sung(a)/full_a-sung(b)/full_b)*width<=1,(kind,mode,dpr,t,template)

