"""Authored isolated accents retain their own physical glyph and clock."""
import copy
from dataclasses import replace
import json
from pathlib import Path
import sys

import numpy as np
import pytest
from PyQt6.QtGui import QFontDatabase

from test_subtitle_skia_ruby_units import application, binary, prepared_source, unit_source, reading
from test_subtitle_skia_protocol import collect, selection
from test_subtitle_skia_protocol import test_actual_g5_preview_uses_skia_even_when_gpu_off as run_preview
from test_subtitle_skia_protocol import test_product_export_short_mp4_uses_skia as run_mp4
from test_subtitle_skia_ruby_groups import configure
from krok_helper.subtitle_render.domain.timing import RubyAnnotation
from krok_helper.subtitle_render.engine.ruby.skia_metrics import measure_timed_readings
from krok_helper.subtitle_render.engine.ruby.skia_timing import ruby_unit_clock
from krok_helper.subtitle_render.engine.ruby.skia_standalone_marks import supported_standalone_mark, validate_mark_metrics
from krok_helper.subtitle_render.engine.render.skia_ir import build_skia_render_ir
from krok_helper.subtitle_render.engine.text.font_weight import clear_font_weight_cache
from krok_helper.subtitle_render.native.backend import NativeRendererProcess, NativeRendererError
from krok_helper.subtitle_render.native.protocol import apply_resolved_font_faces
from krok_helper.subtitle_render.native.skia_backend import SkiaRendererProcess
from krok_helper.subtitle_render.native.skia_text import SkiaTextMetricsProcess


CASES={'acute':('a','\u0301'), 'below':('a','\u0329'), 'kana':('カ','\u3099'),
       'leading':('\u0301','W'), 'only':('\u0301',)}


@pytest.fixture
def mark_source(unit_source):
    from fontTools.ttLib import TTFont
    track,style,resolve=unit_source
    paths=[resolve(replace(style,font_family=name))[0]['path'] for name in ['Main','Latin']]
    for path in paths:
        with TTFont(path,recalcTimestamp=False) as font:
            for cmap in font['cmap'].tables:
                if cmap.isUnicode():cmap.cmap.update({0x329:'acutecomb',0x309a:'acutecomb'})
            font.save(path)
    handles=[QFontDatabase.addApplicationFont(path) for path in paths]
    assert min(handles)>=0
    clear_font_weight_cache()
    try:yield track,style,resolve
    finally:
        for handle in handles:QFontDatabase.removeApplicationFont(handle)
        clear_font_weight_cache()


def marked(source,kind='acute',*,lower=False,mode='reading_units'):
    track,style,resolve=reading(source,'nfc',lower=lower,mode=mode)
    units=CASES[kind]
    parts=[units[0],'',units[1]] if len(units)==2 else list(units)
    ruby=replace(track.rubies[0],reading=''.join(units),reading_parts=parts,
                 reading_part_ms=[400,800] if len(units)==2 else [])
    return replace(track,rubies=[ruby]),style,resolve


def ir_for(binary,source,**kwargs):
    track,style,resolve=source
    with SkiaTextMetricsProcess(binary) as process:
        ir=build_skia_render_ir(track,style,resolve_font=resolve,measure=process.measure,
                               width=900,height=600,fps=60,**kwargs)
    apply_resolved_font_faces(ir)
    return ir


@pytest.mark.parametrize('kind',list(CASES))
@pytest.mark.parametrize('target',['gpu','raster'])
def test_product_keeps_detached_mark_and_authored_parts(binary,mark_source,kind,target,tmp_path):
    source=marked(mark_source,kind);track,style,_=source
    before=copy.deepcopy((track,style))
    ir=ir_for(binary,source)
    assert (track,style)==before
    assert 'p05_standalone_marks' in ir['required_features']
    assert ir['skia_layout']['standalone_mark_schema']==1
    assert ir['skia_layout']['ruby_unit_schema']==1 and ir['skia_layout']['overlap_schema']==1
    units=ir['skia_layout']['lines'][0]['ruby_groups'][0]['units']
    clock=ruby_unit_clock(track.rubies[0],0)
    assert [u['text'] for u in units]==list(clock.units)
    assert [u['interval_ms'] for u in units]==list(map(list,clock.intervals))
    for unit in units:
        if supported_standalone_mark(unit['text']):
            metrics=unit['metrics'];assert metrics['advance']==0
            assert len(metrics['glyphs'])==1 and metrics['glyphs'][0]['id']==9
            assert (metrics['glyphs'][0]['x'],metrics['glyphs'][0]['y'])==(0,0)
            assert metrics['glyphs'][0]['cluster_end']==len(unit['text'].encode())
    with SkiaRendererProcess(binary,selection=selection(target)) as process:
        process.configure(track,style,width=900,height=600,fps=60)
        frames,_=collect(process,[199,450,600,750,950,1200,1699,1700,1900,750])
    assert np.array_equal(frames[3],frames[-1])
    if len(CASES[kind])==2:assert np.array_equal(frames[3],frames[4])
    if kind=='acute' and target=='gpu':
        (tmp_path/'standalone-mark-layout.json').write_text(json.dumps({
            'sources':ir['track']['rubies'],'layout':ir['skia_layout']['lines'],
            'features':ir['required_features']},sort_keys=True),encoding='utf-8')


@pytest.mark.parametrize('kind',['acute','kana'])
@pytest.mark.parametrize('mode',['reading_units','checkpoint_segments'])
def test_upper_and_lower_keep_part_local_marks_and_pauses(binary,mark_source,kind,mode):
    source=marked(mark_source,kind,lower=True,mode=mode)
    ir=ir_for(binary,source)
    groups=ir['skia_layout']['lines'][0]['ruby_groups']
    assert len(groups)==2 and groups[1]['lower']
    assert ir['track']['rubies'][0]['reading_parts']==source[0].rubies[0].reading_parts
    output=[]
    for target in ['gpu','raster']:
        with SkiaRendererProcess(binary,selection=selection(target)) as process:
            configure(process,ir);frames,_=collect(process,[450,750,950,1200,1900,750])
        assert np.array_equal(frames[1],frames[-1]);output.append(frames)
    for a,b in zip(*output,strict=True):assert np.abs(a.astype(float)-b).mean()<.3


@pytest.mark.parametrize('mutation',['feature','schema','type','glyph_text','circle','position','selected_font'])
def test_bad_standalone_contract_preserves_old_scene(binary,mark_source,mutation):
    ir=ir_for(binary,marked(mark_source));bad=copy.deepcopy(ir)
    unit=bad['skia_layout']['lines'][0]['ruby_groups'][0]['units'][1]
    if mutation=='feature':bad['required_features'].remove('p05_standalone_marks')
    elif mutation=='schema':bad['skia_layout']['standalone_mark_schema']=2
    elif mutation=='type':bad['skia_layout']['standalone_mark_schema']='1'
    elif mutation=='glyph_text':unit['glyph_text']='á'
    elif mutation=='circle':unit['metrics']['glyphs'].append(dict(unit['metrics']['glyphs'][0],id=2))
    elif mutation=='position':unit['metrics']['glyphs'][0]['x']=1
    elif mutation=='selected_font':unit['metrics']['selected_font']=-1
    with SkiaRendererProcess(binary,selection=selection('raster')) as process:
        configure(process,ir);before,_=collect(process,[1200])
        with pytest.raises(NativeRendererError):configure(process,bad)
        after,_=collect(process,[1200],generation=2)
    assert np.array_equal(before[0],after[0])


@pytest.mark.parametrize('unit',['\u0301\u0300','\u0361','\u034f','\ufe0f','\u20dd','\u093e','\U0001d165'])
def test_unverified_mark_sequences_fail_before_font_measurement(unit):
    ruby=RubyAnnotation('字',unit,reading_parts=[unit],pos_start_ms=200,pos_end_ms=1000)
    fail=lambda *a,**k:pytest.fail('unsupported mark reached font discovery')
    assert not supported_standalone_mark(unit)
    with pytest.raises(ValueError,match='独立组合标记'):
        measure_timed_readings([(ruby,None)],resolve_font=fail,measure=fail)


def test_invalid_measurement_cannot_hide_a_dotted_circle_or_reposition_the_mark():
    for glyphs,selected in [([],0),([{'x':0,'y':0}]*2,0),([{'x':1,'y':0}],0),([{'x':0,'y':1}],0),
                            ([{'x':0,'y':0}],-1)]:
        with pytest.raises(ValueError,match='单字体原位'):
            validate_mark_metrics('\u0301',{'glyphs':glyphs,'selected_font':selected})


def test_missing_mark_uses_explicit_fallback_without_merging_with_previous_base(binary,mark_source,tmp_path):
    from fontTools.ttLib import TTFont
    track,style,resolve=marked(mark_source)
    backup=tmp_path/'mark-backup.ttf'
    path=resolve(style)[0]['path'];backup.write_bytes(Path(path).read_bytes())
    for name in ['Main','Latin']:
        path=resolve(replace(style,font_family=name))[0]['path']
        with TTFont(path,recalcTimestamp=False) as font:
            for cmap in font['cmap'].tables:cmap.cmap.pop(0x301,None)
            font.save(path)
    class Explicit:
        allow_mixed_fonts=True
        def __call__(self,appearance):return resolve(appearance)
        def resolve_cell_fonts(self,appearance,text):return [resolve(appearance),({'path':str(backup)}, {})]
    ir=ir_for(binary,(track,style,Explicit()))
    units=ir['skia_layout']['lines'][0]['ruby_groups'][0]['units']
    assert units[0]['metrics']['selected_font']==0 and units[1]['metrics']['selected_font']==1
    assert [u['text'] for u in units]==['a','\u0301']
    with SkiaRendererProcess(binary,selection=selection('raster')) as process:configure(process,ir)


@pytest.mark.parametrize('target',['gpu','raster'])
def test_offsets_dpr_and_zero_duration_keep_seek_deterministic(binary,mark_source,target):
    track,style,resolve=marked(mark_source)
    track=replace(track,rubies=[replace(track.rubies[0],reading_part_ms=[0,400])]);output=[]
    for shifted in [False,True]:
        ir=ir_for(binary,(replace(track,meta=replace(track.meta,offset_ms=137)) if shifted else track,
            replace(style,timing_offset_ms=229) if shifted else style,resolve),dpr=1.5,
            roll_shift_ms=400 if shifted else 0)
        with SkiaRendererProcess(binary,selection=selection(target)) as process:
            configure(process,ir)
            frames,_=collect(process,[t+(766 if shifted else 0) for t in [199,200,450,750,1200,1700,450]])
        assert np.array_equal(frames[2],frames[-1]);output.append(frames)
    assert all(np.array_equal(a,b) for a,b in zip(*output,strict=True))


@pytest.mark.parametrize('gpu,missing_loader',[(True,False),(False,False),(True,True)])
def test_real_standalone_preview(binary,mark_source,monkeypatch,tmp_path,gpu,missing_loader):
    run_preview(binary,monkeypatch,tmp_path,marked(mark_source)[:2],gpu,missing_loader)


@pytest.mark.parametrize('gpu,missing_loader',[(True,False),(False,False),(True,True)])
def test_real_standalone_mp4(binary,mark_source,monkeypatch,tmp_path,gpu,missing_loader):
    run_mp4(binary,monkeypatch,tmp_path,marked(mark_source)[:2],gpu,missing_loader)


@pytest.mark.skipif(sys.platform!='win32',reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('kind',['acute','below','kana','leading'])
@pytest.mark.parametrize('mode',['reading_units','checkpoint_segments'])
@pytest.mark.parametrize('semantics',['legacy','n3_1074'])
@pytest.mark.parametrize('dpr',[1,1.5])
def test_standalone_mark_matches_d2d_gpu(binary,mark_source,kind,mode,semantics,dpr):
    from test_subtitle_render_gpu_backend import _render_g1_frames
    track,style,resolve=marked(mark_source,kind,mode=mode);style=replace(style,layout_semantics=semantics)
    times=[199,200,450,599,600,750,999,1000,1200,1699,1700,1900]
    with NativeRendererProcess() as process:
        _,raw=_render_g1_frames(process,style,tuple(times),force_warp=False,track=track,width=900,height=600,
                               dpr=dpr,realization_enabled=False)
    ir=ir_for(binary,(track,style,resolve),dpr=dpr)
    with SkiaRendererProcess(binary,selection=selection('gpu')) as process:
        configure(process,ir);frames,_=collect(process,times)
    refs=[np.frombuffer(p,np.uint8).reshape(round(600*dpr),round(900*dpr),4) for p in raw]
    def boxes(p):
        mask=p[...,3]>127;ys=np.flatnonzero(mask.any(axis=1))
        bands=[v for v in np.split(ys,np.flatnonzero(np.diff(ys)>1)+1) if len(v)]
        return [np.array([np.flatnonzero(mask[b[0]:b[-1]+1].any(axis=0)).min(),b[0],
                         np.flatnonzero(mask[b[0]:b[-1]+1].any(axis=0)).max(),b[-1]]) for b in bands]
    templates=boxes(refs[-1]);assert templates
    for t,a,b in zip(times,refs,frames,strict=True):
        left,right=boxes(a),boxes(b)
        assert len(left)==len(right)==len(templates)
        assert np.max(np.abs(np.array(left)-right))<=1,(kind,mode,dpr,t,left,right)
        for band in templates:
            lo,hi=max(band[1]-2,0),band[3]+3
            def sung(p):return (p[lo:hi,:,1].astype(float)*p[lo:hi,:,3]/255**2).sum()
            full_a,full_b=sung(refs[-1]),sung(frames[-1])
            width=(full_a+full_b)/(2*(band[3]-band[1]+1))
            assert abs(sung(a)/full_a-sung(b)/full_b)*width<=1,(kind,mode,dpr,t,band)


def test_old_sidecar_cannot_silently_accept_standalone_marks(binary,monkeypatch):
    process=SkiaRendererProcess(binary,selection=selection('raster'))
    try:ready=NativeRendererProcess.start(process)
    finally:process.abort(reason='handshake fixture')
    ready.pop('standalone_mark_schema');sent=[]
    monkeypatch.setattr(NativeRendererProcess,'start',lambda self:ready)
    monkeypatch.setattr(SkiaRendererProcess,'abort',lambda *a,**k:None)
    monkeypatch.setattr(SkiaRendererProcess,'_send',lambda self,p:sent.append(p))
    with pytest.raises(NativeRendererError,match='独立组合标记'):SkiaRendererProcess(binary).start()
    assert not sent
