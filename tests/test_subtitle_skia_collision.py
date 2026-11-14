"""Canonical main-ink collision ports, cache identity and resolved translations."""
import copy
from dataclasses import replace
import json
import sys
from types import SimpleNamespace

import numpy as np
import pytest

from test_subtitle_skia_grid import application, binary, project, grid
from test_subtitle_skia_protocol import collect, selection
from test_subtitle_skia_resolved import ir_for
from skia_text_fixture import make_font
from krok_helper.subtitle_render.domain.models import TimingChar, TimingLine
from krok_helper.subtitle_render.engine.render.skia_ir import build_skia_render_ir
from krok_helper.subtitle_render.engine.layout.display.resolver import (
    DisplayResolutionPorts, StyleDisplayResolutionPorts, resolve_display_lines_for_style,
)
from krok_helper.subtitle_render.engine.layout.plan.page_offsets import (
    clear_page_offset_cache, cached_page_offset_windows, store_page_offset_windows,
)
from krok_helper.subtitle_render.engine.text.skia_metrics import measure_track
from krok_helper.subtitle_render.engine.layout.line.style import style_for_line
from krok_helper.subtitle_render.engine.layout.display.skia_collision import SkiaCollisionGeometry
from krok_helper.subtitle_render.native.skia_fonts import resolve_skia_font
from krok_helper.subtitle_render.native.skia_text import SkiaTextMetricsProcess
from krok_helper.subtitle_render.native.skia_backend import SkiaRendererProcess
from krok_helper.subtitle_render.native.backend import NativeRendererError, NativeRendererProcess


@pytest.fixture
def collisions(grid):
    track,style=grid
    style=replace(style,line_alignments=['center','center'],line_offsets_x=[0,0],
        line_lead_in_ms=1500,line_tail_ms=2000,line_lane_gap_ms=100,
        allow_inter_page_line_overlap=False,auto_fill_section_time=True,sync_entry=False,
        force_top_bottom_n3=False,overlap_fallback_mode='lift')
    lines=[TimingLine(chars=[TimingChar('字',1000+i*500),TimingChar('A',1300+i*500)],
                      end_ms=1800+i*500) for i in range(6)]
    return replace(track,lines=lines),style


def pinned_pages(project):
    track,style=project
    return replace(track,lines=[replace(l,display_start_override_ms=0,display_end_override_ms=6000)
                                for l in track.lines[:4]]),style


@pytest.mark.parametrize('semantics',['legacy','n3_1074'])
def test_automatic_timing_compresses_and_fills_without_reallocating_wipe(binary,collisions,semantics,tmp_path):
    track,style=collisions
    style=replace(style,layout_semantics=semantics,overlap_fallback_mode='displace')
    ir=ir_for(binary,(track,style))
    lines=ir['track']['lines']
    assert lines[0]['display_end_ms']==1800
    assert lines[2]['display_start_ms']==1900
    assert lines[0]['resolved_intervals']==[[1000,1300],[1300,1800]]
    unfilled=ir_for(binary,(track,replace(style,auto_fill_section_time=False)))
    assert lines[-2]['display_end_ms']==lines[-1]['display_end_ms']
    assert lines[-2]['display_end_ms']>unfilled['track']['lines'][-2]['display_end_ms']
    assert all(l['layout_offset_windows']==[] for l in lines)
    if semantics=='n3_1074':
        lifted=ir_for(binary,pinned_pages((track,replace(style,overlap_fallback_mode='lift'))))
        (tmp_path/'collision-layout.json').write_text(json.dumps({'schedule':[
            {k:l[k] for k in ('lane','display_start_ms','display_end_ms','resolved_intervals','layout_offset_windows')}
            for l in lines], 'lifted':[{k:l[k] for k in ('lane','display_start_ms','display_end_ms','layout_offset_windows')}
                                     for l in lifted['track']['lines']]},sort_keys=True),encoding='utf-8')


def test_geometry_is_the_final_undecorated_main_ink_and_reuses_retimed_rows(binary,collisions):
    track,style=collisions
    with SkiaTextMetricsProcess(binary) as process:
        styles=[style_for_line(style,l) for l in track.lines]
        measured=measure_track(track,style,styles,measure=process.measure,resolve_font=resolve_skia_font)
    geometry=SkiaCollisionGeometry(track,style,styles,measured)
    item=SimpleNamespace(line=track.lines[0],lane=0,page_line_count=2)
    first=geometry.geometry(item,640,360)
    assert first.axis_min==205 and first.axis_max==240
    assert first.axis_anchor==240
    assert geometry.geometry(item,640,360) is first
    item.lane=1
    second=geometry.geometry(item,640,360)
    assert second.axis_anchor-first.axis_anchor==70
    assert len(geometry.cache)==2


def test_display_and_page_offset_cache_namespaces_include_metrics(collisions):
    track,style=collisions
    style=replace(style,allow_inter_page_line_overlap=True,auto_fill_section_time=False)
    calls=[]
    def build(w,h,kwargs):
        calls.append(1)
        return DisplayResolutionPorts(lambda **k:[],lambda items,*a:items,lambda i:(),lambda i:(),
                                      lambda i:i,lambda items,*a:items)
    ports=StyleDisplayResolutionPorts(build)
    for identity in (None,('skia','a'),('skia','b'),('skia','a')):
        resolve_display_lines_for_style(track,style,{},ports,logical_w=640,logical_h=360,metrics_identity=identity)
    assert len(calls)==3
    clear_page_offset_cache()
    for identity,value in ((None,1),(('skia','a'),2),(('skia','b'),3)):
        store_page_offset_windows(640,360,track,style,{0:((0,1000,0,value),)},metrics_identity=identity)
    for identity,value in ((None,1),(('skia','a'),2),(('skia','b'),3)):
        assert cached_page_offset_windows(640,360,track,style,metrics_identity=identity)[0][0][3]==value


def test_changed_font_bytes_remeasure_collisions_without_style_or_path_change(binary,collisions,tmp_path,monkeypatch):
    track,style=pinned_pages(collisions)
    path=tmp_path/'mutable.ttf'
    outputs=[]
    from krok_helper.subtitle_render.engine.text import metrics as qt_metrics
    monkeypatch.setattr(qt_metrics,'QFontMetrics',lambda *a:pytest.fail('Qt collision metrics'))
    monkeypatch.setattr(qt_metrics,'QPainterPath',lambda *a:pytest.fail('Qt collision path'))
    for size in (1000,1400):
        make_font(path,ascent=size,descent=200)
        with SkiaTextMetricsProcess(binary) as process:
            outputs.append(build_skia_render_ir(track,style,measure=process.measure,
                resolve_font=lambda _:({'path':str(path)},{}),width=640,height=480,fps=60))
    assert outputs[0]['skia_layout']['metrics_identity']!=outputs[1]['skia_layout']['metrics_identity']
    a,b=[ir['track']['lines'][2]['layout_offset_windows'] for ir in outputs]
    assert a!=b


@pytest.mark.parametrize('target',['gpu','raster'])
@pytest.mark.parametrize('semantics',['legacy','n3_1074'])
def test_lifted_pages_keep_manual_windows_and_seek_stable(binary,collisions,semantics,target):
    track,style=pinned_pages(collisions)
    style=replace(style,layout_semantics=semantics)
    ir=ir_for(binary,(track,style))
    windows=[l['layout_offset_windows'] for l in ir['track']['lines']]
    assert windows[0]==windows[1] and windows[2]==windows[3]
    assert windows[2][0]['offset_y']<0
    assert all(l['display_start_ms']==0 and l['display_end_ms']==6000 for l in ir['track']['lines'])
    with SkiaRendererProcess(binary,selection=selection(target)) as process:
        process.configure(track,style,width=640,height=480,fps=60,dpr=1.5)
        frames,_=collect(process,[5999,800,6000,800,4000])
        assert np.array_equal(frames[1],frames[3]) and not frames[2].any()
        assert all(frames[i][...,3].max()>0 for i in (0,1,4))


@pytest.mark.parametrize('mode',['lift','displace'])
def test_gpu_raster_translations_and_all_time_offsets_agree(binary,collisions,mode):
    track,style=pinned_pages(collisions)
    style=replace(style,overlap_fallback_mode=mode,base_color='#80FFFFFF',fill_color='#AACC3355')
    base_times=[800,2200,4000,5999,6000]
    results=[]
    for target in ('gpu','raster'):
        visits=[]
        for shifted in (False,True):
            source=replace(track,meta=replace(track.meta,offset_ms=137)) if shifted else track
            appearance=replace(style,timing_offset_ms=229) if shifted else style
            with SkiaRendererProcess(binary,selection=selection(target)) as process:
                process.configure(source,appearance,width=640,height=480,fps=60,dpr=1.5,
                                  roll_shift_ms=400 if shifted else 0)
                frames,_=collect(process,[t+(766 if shifted else 0) for t in base_times])
                visits.append(frames)
        assert all(np.array_equal(a,b) for a,b in zip(*visits))
        pixels=np.stack(visits[0]).astype(float);pixels[...,:3]*=pixels[...,3:]/255
        results.append(pixels)
    delta=np.abs(results[0]-results[1])
    assert delta.mean()<.15 and np.mean(np.max(delta,axis=-1)>16)<.003


def test_old_sidecar_without_collision_capability_is_rejected(monkeypatch,tmp_path):
    executable=tmp_path/'old';executable.touch()
    ready={'ok':True,'backend':'skia','schema':3,'selection_version':1,'resolved_layout_schema':2,
        'profiles':['p05_resolved_horizontal','p05_single_line_placement','p05_inline_roles','p05_fixed_row_grid'],
        'execution_targets':['raster']}
    monkeypatch.setattr(NativeRendererProcess,'start',lambda self:ready)
    monkeypatch.setattr(SkiaRendererProcess,'abort',lambda *a,**k:None)
    with pytest.raises(NativeRendererError,match='跨页避让'):SkiaRendererProcess(executable).start()


@pytest.mark.parametrize('target',['gpu','raster'])
def test_translation_half_open_windows_move_paths_and_wipe_together(binary,grid,target):
    track,style=grid
    track=replace(track,lines=[replace(track.lines[0],chars=[TimingChar('字',0)],end_ms=0,
        display_start_override_ms=0,display_end_override_ms=5000)])
    base=ir_for(binary,(track,style))
    moved=copy.deepcopy(base)
    moved['track']['lines'][0]['layout_offset_windows']=[{'start_ms':1000,'end_ms':2000,'offset_x':16,'offset_y':-24}]
    with SkiaRendererProcess(binary,selection=selection(target)) as process:
        process._send({'cmd':'configure','ir':base});process._expect_ok(process._read_until_event('configured'))
        reference,_=collect(process,[1500])
        process._send({'cmd':'configure','ir':moved});process._expect_ok(process._read_until_event('configured'))
        frames,_=collect(process,[999,1000,1999,2000,1500],generation=2)
    assert np.array_equal(frames[0],reference[0]) and np.array_equal(frames[3],reference[0])
    expected=np.roll(reference[0],(-24,16),(0,1))
    assert all(np.array_equal(frames[i],expected) for i in (1,2,4))


@pytest.mark.parametrize('mutation',['fractional_time','reversed','overlap','nan','infinity','string','missing'])
def test_invalid_translation_rejected_transactionally(binary,grid,mutation):
    with SkiaRendererProcess(binary,selection=selection('raster')) as process:
        process.configure(*grid,width=640,height=360,fps=60)
        before,_=collect(process,[1400])
        ir=ir_for(binary,grid)
        window={'start_ms':0,'end_ms':2000,'offset_x':0,'offset_y':-20}
        if mutation=='fractional_time':window['start_ms']=.5
        elif mutation=='reversed':window['end_ms']=-1
        elif mutation=='nan':window['offset_y']=float('nan')
        elif mutation=='infinity':window['offset_y']=float('inf')
        elif mutation=='string':window['offset_x']='2'
        elif mutation=='missing':del window['offset_y']
        ir['track']['lines'][0]['layout_offset_windows']=[window,window] if mutation=='overlap' else [window]
        process._send({'cmd':'configure','ir':ir})
        with pytest.raises(NativeRendererError):process._expect_ok(process._read_until_event('configured'))
        after,_=collect(process,[1400],generation=2)
        assert np.array_equal(before[0],after[0])


@pytest.mark.skipif(sys.platform!='win32',reason='Direct2D GPU comparison')
@pytest.mark.parametrize('mode',['lift','displace'])
@pytest.mark.parametrize('semantics',['legacy','n3_1074'])
def test_manual_page_collision_results_match_d2d_gpu(binary,collisions,semantics,mode):
    from test_subtitle_render_gpu_backend import _render_g1_frames
    track,style=pinned_pages(collisions)
    style=replace(style,layout_semantics=semantics,overlap_fallback_mode=mode)
    with NativeRendererProcess() as d2d:
        _,raw=_render_g1_frames(d2d,style,(800,2200,4000),force_warp=False,track=track,width=640,height=480)
    with SkiaRendererProcess(binary,selection=selection('gpu')) as skia:
        skia.configure(track,style,width=640,height=480,fps=60)
        frames,_=collect(skia,[800,2200,4000])
    def bands(p):
        ys=np.flatnonzero((p[...,3]>127).any(axis=1))
        return [(v[0],v[-1]) for v in np.split(ys,np.flatnonzero(np.diff(ys)>1)+1) if len(v)]
    for expected,actual in zip(raw,frames):
        a,b=bands(np.frombuffer(expected,np.uint8).reshape(480,640,4)),bands(actual)
        assert len(a)==len(b) and a
        assert np.max(np.abs(np.array(a)-np.array(b)))<=1,(semantics,mode,a,b)
