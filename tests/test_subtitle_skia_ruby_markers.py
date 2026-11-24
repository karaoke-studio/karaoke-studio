"""Empty/phrase-marker product sources retain the existing GPU clock contract."""
import copy
from dataclasses import replace
import json
import sys

import numpy as np
import pytest
from PyQt6.QtGui import QFontDatabase

from test_subtitle_skia_lower import application, binary, prepared_source
from test_subtitle_skia_protocol import collect, selection
from test_subtitle_skia_protocol import test_actual_g5_preview_uses_skia_even_when_gpu_off as run_preview
from test_subtitle_skia_protocol import test_product_export_short_mp4_uses_skia as run_mp4
from test_subtitle_skia_roles import colors
from test_subtitle_skia_ruby_groups import configure
from krok_helper.subtitle_render.domain.timing import RubyAnnotation
from krok_helper.subtitle_render.engine.render.skia_ir import build_skia_render_ir
from krok_helper.subtitle_render.engine.text.font_weight import clear_font_weight_cache
from krok_helper.subtitle_render.native.backend import NativeRendererProcess, NativeRendererError
from krok_helper.subtitle_render.native.protocol import apply_resolved_font_faces
from krok_helper.subtitle_render.native.skia_backend import SkiaRendererProcess
from krok_helper.subtitle_render.native.skia_text import SkiaTextMetricsProcess


@pytest.fixture
def marker_source(prepared_source):
    from fontTools.ttLib import TTFont
    track, style, resolve = prepared_source
    paths=[resolve(replace(style,font_family=name))[0]['path'] for name in ['Main','Latin']]
    for path in paths:
        font=TTFont(path,recalcTimestamp=False)
        for table in font['cmap'].tables:
            if table.isUnicode(): table.cmap[ord('^')]='A'
        font.save(path); font.close()
    handles=[QFontDatabase.addApplicationFont(path) for path in paths]
    assert min(handles)>=0
    clear_font_weight_cache()
    style=replace(style,lower_ruby_enabled=False, ruby_latin_font_stretch_pct=100,
        entry_anim='none',exit_anim='none',karaoke_anim='none',
        dual_line_layout=False,line_horizontal_layout='center',smart_horizontal='none',
        line_y_margin_px=40,line_lead_in_ms=400,line_tail_ms=500,
        karaoke_colors=colors('#FF0000','#00FF00'),ruby_colors_follow_main=False,
        ruby_karaoke_colors=colors('#FF0000','#00FF00'))
    line=replace(track.lines[0],track_line_index=0,
                 display_start_override_ms=0,display_end_override_ms=5000)
    try: yield replace(track,lines=[line],rubies=[]),style,resolve
    finally:
        for handle in handles: QFontDatabase.removeApplicationFont(handle)
        clear_font_weight_cache()


def source_with_marker(source, text, *, lower=False):
    track,style,resolve=source
    ruby=RubyAnnotation('字かな',text,reading_parts=[text],reading_part_ms=[],
        pos_start_ms=200,pos_end_ms=1700,target_line_index=0,target_char_start=0,target_char_end=3)
    return replace(track,rubies=[ruby]),replace(style,lower_ruby_enabled=lower),resolve


def ir_for(binary,source,**kwargs):
    track,style,resolve=source
    with SkiaTextMetricsProcess(binary) as metrics:
        ir=build_skia_render_ir(track,style,measure=metrics.measure,resolve_font=resolve,
                                width=900,height=600,fps=60,**kwargs)
    apply_resolved_font_faces(ir)
    return ir


@pytest.mark.parametrize('text',['','^',' ',' ^ '])
@pytest.mark.parametrize('target',['gpu','raster'])
def test_marker_retains_source_without_owning_main_clock(binary,marker_source,text,target,tmp_path):
    source=source_with_marker(marker_source,text)
    ir=ir_for(binary,source)
    base=ir_for(binary,marker_source)
    assert ir['track']['rubies'][0]['reading']==text
    assert 'p05_ruby_markers' in ir['required_features'] and ir['skia_layout']['ruby_marker_schema']==1
    cells=ir['skia_layout']['lines'][0]['cells']
    assert cells==base['skia_layout']['lines'][0]['cells']
    assert all('wipe_points' not in c for c in cells)
    groups=ir['skia_layout']['lines'][0]['ruby_groups']
    assert bool(groups)==bool(text)  # Empty source is provenance, never a fake unit.
    times=[199,200,450,699,700,950,1200,1699,1700,1900,450]
    with SkiaRendererProcess(binary,selection=selection(target)) as process:
        process.configure(*source[:2],width=900,height=600,fps=60)
        frames,_=collect(process,times)
        configure(process,base); plain,_=collect(process,times,generation=2)
    assert np.array_equal(frames[2],frames[-1])
    # Source marker configurations use GPU-compatible overlap clipping; a
    # no-ruby source retains its previous unadjusted clip profile.
    if not text: assert np.array_equal(frames[-2],plain[-2])
    if target=='gpu' and text=='^':
        (tmp_path/'ruby-markers.json').write_text(json.dumps({
            'source':ir['track']['rubies'],'layout':ir['skia_layout']['lines'],
            'features':ir['required_features']},sort_keys=True),encoding='utf-8')


@pytest.mark.parametrize('text',['','^'])
@pytest.mark.parametrize('lower',[False,True])
def test_marker_does_not_reserve_a_following_reading_main_clock(binary,marker_source,text,lower):
    source=source_with_marker(marker_source,text,lower=lower)
    track,style,resolve=source
    actual=replace(track.rubies[0],reading='AA',reading_parts=['A','','A'],reading_part_ms=[200,900])
    expected=ir_for(binary,(replace(track,rubies=[actual]),style,resolve))
    ir=ir_for(binary,(replace(track,rubies=[track.rubies[0],actual]),style,resolve))
    # Empty marker geometry is absent; caret geometry is real, but neither
    # claims main timing. With lower off the main cells are identical too.
    curves=lambda value:[c.get('wipe_points') for c in value['skia_layout']['lines'][0]['cells']]
    assert curves(ir)==curves(expected) and any(curves(ir))
    with SkiaRendererProcess(binary,selection=selection('raster')) as process: configure(process,ir)


@pytest.mark.parametrize('text',['','^'])
@pytest.mark.parametrize('mode',['reading_units','checkpoint_segments'])
@pytest.mark.parametrize('target',['gpu','raster'])
def test_marker_parts_keep_pause_without_replacing_main_clock(binary,marker_source,text,mode,target):
    track,style,resolve=source_with_marker(marker_source,text)
    ruby=replace(track.rubies[0],reading_parts=['',text,''],reading_part_ms=[300,800])
    source=replace(track,rubies=[ruby]),replace(style,ruby_main_progress_mode=mode),resolve
    ir=ir_for(binary,source)
    assert all('wipe_points' not in c for c in ir['skia_layout']['lines'][0]['cells'])
    groups=ir['skia_layout']['lines'][0]['ruby_groups']
    assert ([u['interval_ms'] for u in groups[0]['units']] if groups else [])==([[500,1000]] if text else [])
    with SkiaRendererProcess(binary,selection=selection(target)) as process:
        configure(process,ir); frames,_=collect(process,[450,500,750,1000,1300,750])
    assert np.array_equal(frames[2],frames[-1])
    if text:
        ys=np.flatnonzero((frames[0][...,3]>127).any(axis=1))
        separation=ys[np.flatnonzero(np.diff(ys)>1)[0]]+1
        assert np.array_equal(frames[0][:separation],frames[1][:separation])
        assert np.array_equal(frames[3][:separation],frames[4][:separation])
        assert not np.array_equal(frames[1][:separation],frames[2][:separation])


@pytest.mark.parametrize('text',['','^',' ',' ^ '])
@pytest.mark.parametrize('lower',[False,True])
@pytest.mark.parametrize('semantics',['legacy','n3_1074'])
@pytest.mark.parametrize('dpr',[1,1.5])
@pytest.mark.skipif(sys.platform!='win32',reason='Actual Direct2D GPU reference')
def test_product_marker_matches_d2d_gpu(binary,marker_source,text,lower,semantics,dpr):
    from test_subtitle_render_gpu_backend import _render_g1_frames
    source=source_with_marker(marker_source,text,lower=lower)
    track,style,_=source
    style=replace(style,layout_semantics=semantics)
    times=[199,200,450,699,700,950,1200,1699,1700,1900]
    with NativeRendererProcess() as process:
        _,raw=_render_g1_frames(process,style,tuple(times),force_warp=False,track=track,width=900,height=600,dpr=dpr)
    with SkiaRendererProcess(binary,selection=selection('gpu')) as process:
        process.configure(track,style,width=900,height=600,fps=60,dpr=dpr)
        frames,_=collect(process,times)
    refs=[np.frombuffer(raw_frame,np.uint8).reshape(round(600*dpr),round(900*dpr),4) for raw_frame in raw]
    def boxes(p):
        mask=p[...,3]>127
        ys=np.flatnonzero(mask.any(axis=1))
        bands=[v for v in np.split(ys,np.flatnonzero(np.diff(ys)>1)+1) if len(v)]
        return [np.array([np.flatnonzero(mask[b[0]:b[-1]+1].any(axis=0)).min(),b[0],
                         np.flatnonzero(mask[b[0]:b[-1]+1].any(axis=0)).max(),b[-1]]) for b in bands]
    templates=boxes(refs[-1])
    assert templates
    for t,a,b in zip(times,refs,frames,strict=True):
        left,right=boxes(a),boxes(b)
        assert len(left)==len(right)==len(templates)
        assert np.max(np.abs(np.array(left)-right))<=1,(text,lower,t,left,right)
        for template in templates:
            lo,hi=max(template[1]-2,0),template[3]+3
            def sung(p): return (p[lo:hi,:,1].astype(float)*p[lo:hi,:,3]/255**2).sum()
            full_a,full_b=sung(refs[-1]),sung(frames[-1])
            width=(full_a+full_b)/(2*(template[3]-template[1]+1))
            # Same 1 physical pixel front criterion as the lower product
            # probe. Thresholded red boxes jump across gaps at end-1ms when
            # only fractional antialias coverage remains.
            assert abs(sung(a)/full_a-sung(b)/full_b)*width<=1,(text,lower,t,template)


@pytest.mark.parametrize('target',['gpu','raster'])
def test_marker_time_offsets_and_dpr_are_applied_once(binary,marker_source,target):
    track,style,_=source_with_marker(marker_source,'^',lower=True)
    result=[]
    for shifted in [False,True]:
        with SkiaRendererProcess(binary,selection=selection(target)) as process:
            process.configure(replace(track,meta=replace(track.meta,offset_ms=137)) if shifted else track,
                replace(style,timing_offset_ms=229) if shifted else style,width=900,height=600,fps=60,dpr=1.5,
                roll_shift_ms=400 if shifted else 0)
            frames,_=collect(process,[t+(766 if shifted else 0) for t in [199,450,700,950,1200,1699,1900]])
        result.append(frames)
    assert all(np.array_equal(a,b) for a,b in zip(*result,strict=True))


@pytest.mark.parametrize('mutation',['feature','schema',False,None,'1',2])
def test_marker_requires_versioned_capability_and_keeps_previous_scene(binary,marker_source,mutation):
    ir=ir_for(binary,source_with_marker(marker_source,'^'))
    broken=copy.deepcopy(ir)
    if mutation=='feature': broken['required_features'].remove('p05_ruby_markers')
    elif mutation=='schema': del broken['skia_layout']['ruby_marker_schema']
    else: broken['skia_layout']['ruby_marker_schema']=mutation
    with SkiaRendererProcess(binary,selection=selection('raster')) as process:
        configure(process,ir); before,_=collect(process,[450])
        with pytest.raises(NativeRendererError): configure(process,broken)
        after,_=collect(process,[450],generation=2)
    assert np.array_equal(before[0],after[0])


@pytest.mark.parametrize('target',['gpu','raster'])
def test_marker_retains_clocks_during_utopia(binary,marker_source,target):
    track,style,resolve=source_with_marker(marker_source,'^')
    moving=replace(style,karaoke_anim='utopia')
    base=ir_for(binary,(track,style,resolve))
    ir=ir_for(binary,(track,moving,resolve))
    line=ir['skia_layout']['lines'][0]
    assert 'p05_ruby_markers' in ir['required_features']
    assert all('wipe_points' not in cell for cell in line['cells'])
    assert ir['track']['lines'][0]['resolved_intervals']==base['track']['lines'][0]['resolved_intervals']
    assert [[unit['interval_ms'] for unit in group['units']] for group in line['ruby_groups']]==[
        [unit['interval_ms'] for unit in group['units']]
        for group in base['skia_layout']['lines'][0]['ruby_groups']]
    with SkiaRendererProcess(binary,selection=selection(target)) as process:
        process.configure(track,moving,width=900,height=600,fps=60)
        frames,_=collect(process,[450,950,450])
    assert np.array_equal(frames[0],frames[2])
    assert not np.array_equal(frames[0],frames[1])


def test_marker_with_zoom_pulse_keeps_previous_scene(binary,marker_source):
    track,style,_=source_with_marker(marker_source,'^')
    with SkiaRendererProcess(binary,selection=selection('raster')) as process:
        process.configure(track,style,width=900,height=600,fps=60)
        before,_=collect(process,[450])
        after,_=collect(process,[450],generation=2)
    assert np.array_equal(before[0],after[0])


@pytest.mark.parametrize('gpu,missing_loader',[(True,False),(False,False),(True,True)])
def test_real_marker_preview_uses_skia(binary,marker_source,monkeypatch,tmp_path,gpu,missing_loader):
    run_preview(binary,monkeypatch,tmp_path,source_with_marker(marker_source,'^')[:2],gpu,missing_loader)


@pytest.mark.parametrize('gpu,missing_loader',[(True,False),(False,False),(True,True)])
def test_real_marker_mp4_uses_skia(binary,marker_source,monkeypatch,tmp_path,gpu,missing_loader):
    run_mp4(binary,monkeypatch,tmp_path,source_with_marker(marker_source,'^')[:2],gpu,missing_loader)


def test_old_sidecar_cannot_silently_accept_markers(binary,monkeypatch):
    process=SkiaRendererProcess(binary,selection=selection('raster'))
    try: ready=NativeRendererProcess.start(process)
    finally: process.abort(reason='handshake fixture')
    ready['profiles'].remove('p05_ruby_markers')
    monkeypatch.setattr(NativeRendererProcess,'start',lambda _:ready)
    monkeypatch.setattr(SkiaRendererProcess,'abort',lambda *a,**k:None)
    sent=[]; monkeypatch.setattr(SkiaRendererProcess,'_send',lambda self,v:sent.append(v))
    with pytest.raises(NativeRendererError,match='短语标记'): SkiaRendererProcess(binary).start()
    assert not sent
