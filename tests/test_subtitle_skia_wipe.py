"""Resolved ruby clocks and platform-independent segmented GPU/Raster drawing."""
from dataclasses import replace
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import numpy as np
import pytest

from test_subtitle_skia_resolved import application, binary, project, ir_for
from test_subtitle_skia_protocol import collect, selection
from krok_helper.subtitle_render.domain.models import TimingChar, TimingLine
from krok_helper.subtitle_render.domain.timing import RubyAnnotation
from krok_helper.subtitle_render.engine.ruby.skia_timing import (
    main_wipe_points, prepare_ruby_wipe_timing, wipe_points_to_ir,
)
from krok_helper.subtitle_render.engine.ruby.timing import ruby_main_wipe_intervals
from krok_helper.subtitle_render.native.skia_backend import SkiaRendererProcess
from krok_helper.subtitle_render.native.backend import NativeRendererProcess, NativeRendererError


def prepared(rubies):
    return SimpleNamespace(rubies=tuple(SimpleNamespace(target=SimpleNamespace(
        annotation_index=i,effective=ruby,indices=indices)) for i,(ruby,indices) in enumerate(rubies)))


def clock_ruby(**changes):
    return replace(RubyAnnotation('字字字','A字',pos_start_ms=300,pos_end_ms=2100,
        reading_parts=['A','','字'],reading_part_ms=[600,1100]),**changes)


def line_three():
    return TimingLine(chars=[TimingChar('字',t) for t in [300,900,1500]],end_ms=2100)


def test_reading_unit_and_checkpoint_clocks_have_distinct_empty_part_semantics():
    ruby=clock_ruby()
    assert ruby_main_wipe_intervals(ruby,'reading_units')==[(300,900),(1400,2100)]
    assert ruby_main_wipe_intervals(ruby,'checkpoint_segments')==[(300,900),(900,1400),(1400,2100)]
    a=prepare_ruby_wipe_timing(line_three(),prepared([(ruby,(0,1,2))]),'reading_units')
    b=prepare_ruby_wipe_timing(line_three(),prepared([(ruby,(0,1,2))]),'checkpoint_segments')
    assert a.main[0]==((300,0.),(700,1.))
    assert a.main[1][0]==(700,0.) and a.main[1][-1]==(1633,1.)
    assert a.main[1][1][0]==900 and a.main[1][2][0]==1400
    assert a.main[1][1][1]==pytest.approx(.5) and a.main[1][2][1]==pytest.approx(.5)
    assert b.main[1]==((900,0.),(1400,1.))
    assert a.readings==b.readings
    assert a.readings[0].units==('A','字') and a.readings[0].intervals==((300,900),(1400,2100))


def test_time_inversion_uses_gpu_half_up_rounding_and_keeps_zero_duration():
    assert main_wipe_points([(0,5)],2)==(((0,0.),(3,1.)),((3,0.),(5,1.)))
    assert main_wipe_points([(10,10)],1)==(((10,0.),(10,1.)),)
    assert main_wipe_points([(0,100),(300,400)],1)==(((0,0.),(100,.5),(300,.5),(400,1.)),)


@pytest.mark.parametrize('internal',['start','end'])
def test_explicit_internal_main_boundaries_preserve_authoritative_base_intervals(internal):
    line=line_three()
    index=1 if internal=='start' else 0
    chars=list(line.chars)
    chars[index]=replace(chars[index],**{'explicit_'+internal:True})
    result=prepare_ruby_wipe_timing(replace(line,chars=chars),prepared([(clock_ruby(),(0,1,2))]),'reading_units')
    assert result.main==(None,None,None)
    assert result.readings[0].intervals==((300,900),(1400,2100))


def test_outer_main_boundaries_do_not_override_ruby_clock():
    line=line_three()
    line=replace(line,chars=[replace(line.chars[0],explicit_start=True),line.chars[1],
                             replace(line.chars[2],explicit_end=True)])
    result=prepare_ruby_wipe_timing(line,prepared([(clock_ruby(),(0,1,2))]),'reading_units')
    assert all(points is not None for points in result.main)


def test_first_overlapping_group_wins_without_replacing_other_cells():
    line=TimingLine(chars=[TimingChar('字',i*100) for i in range(4)],end_ms=400)
    first=clock_ruby(pos_start_ms=0,pos_end_ms=200,reading_part_ms=[])
    second=clock_ruby(pos_start_ms=100,pos_end_ms=400,reading_part_ms=[])
    result=prepare_ruby_wipe_timing(line,prepared([(first,(0,1)),(second,(1,2,3))]),'reading_units')
    assert result.main[:2]==main_wipe_points(ruby_main_wipe_intervals(first,'reading_units'),2)
    assert result.main[2:]==(None,None)
    assert len(result.readings)==2


def test_pause_only_phrase_marker_does_not_replace_main_clock():
    marker=clock_ruby(reading='^',reading_parts=['^'])
    result=prepare_ruby_wipe_timing(line_three(),prepared([(marker,(0,1,2))]),'reading_units')
    assert result.main==(None,None,None)


@pytest.mark.parametrize('indices',[(),(-1,),(3,),(True,)])
def test_invalid_ruby_target_cannot_extend_or_reindex_the_main_clock(indices):
    with pytest.raises(ValueError,match='超出'):
        prepare_ruby_wipe_timing(line_three(),prepared([(clock_ruby(),indices)]),'reading_units')


@pytest.mark.parametrize('intervals,count', [([],1),([(0,1)],0),([(0,1)],1.5),
    ([(100,50)],1),([(0,100),(50,200)],1),([(0.,100)],1),([(0,86400001)],1)])
def test_invalid_prepared_clock_is_rejected(intervals,count):
    with pytest.raises(ValueError):main_wipe_points(intervals,count)


def configure_ir(process,ir):
    process._send({'cmd':'configure','ir':ir})
    return process._expect_ok(process._read_until_event('configured'))


def static_project(project):
    track,style=project
    return replace(track,lines=[replace(track.lines[0],display_start_override_ms=0,
                                       display_end_override_ms=5000)]),style


def with_points(binary,project,points,**kwargs):
    ir=ir_for(binary,static_project(project),**kwargs)
    for cell in ir['skia_layout']['lines'][0]['cells']:
        cell['wipe_points']=wipe_points_to_ir(points)
    return ir


@pytest.mark.parametrize('target',['gpu','raster'])
def test_segmented_wipe_draws_exact_pause_and_random_seek(binary,project,target,tmp_path):
    points=((200,0.),(400,.35),(1400,.35),(2000,1.))
    ir=with_points(binary,project,points)
    times=[0,500,800,1200,1600,2500,800]
    with SkiaRendererProcess(binary,selection=selection(target)) as process:
        configure_ir(process,ir)
        frames,_=collect(process,times)
    assert np.array_equal(frames[1],frames[2]) and np.array_equal(frames[2],frames[3])
    assert np.array_equal(frames[2],frames[6])
    assert not np.array_equal(frames[3],frames[4]) and not np.array_equal(frames[0],frames[5])
    if target=='gpu':
        (tmp_path/'wipe-layout.json').write_text(json.dumps({'cells':ir['skia_layout']['lines'][0]['cells'],
            'reading_units':prepare_ruby_wipe_timing(line_three(),prepared([(clock_ruby(),(0,1,2))]),'reading_units').main,
            'checkpoints':prepare_ruby_wipe_timing(line_three(),prepared([(clock_ruby(),(0,1,2))]),'checkpoint_segments').main,
        },sort_keys=True),encoding='utf-8')


@pytest.mark.parametrize('target',['gpu','raster'])
def test_zero_duration_and_duplicate_time_follow_gpu_boundary_rules(binary,project,target):
    points=((200,0.),(400,.2),(400,.8),(600,1.))
    ir=with_points(binary,project,points)
    with SkiaRendererProcess(binary,selection=selection(target)) as process:
        configure_ir(process,ir)
        frames,_=collect(process,[399,400,401])
        assert not np.array_equal(frames[0],frames[1])
        configure_ir(process,with_points(binary,project,((200,0.),(200,1.))))
        frames,_=collect(process,[199,200,201],generation=2)
        assert np.array_equal(frames[0],frames[1]) and not np.array_equal(frames[1],frames[2])


def test_gpu_raster_and_all_time_offsets_use_the_same_clock(binary,project):
    points=((200,0.),(400,.35),(1400,.35),(2000,1.))
    track,style=static_project(project)
    outputs=[]
    for target in ['gpu','raster']:
        both=[]
        for shifted in [False,True]:
            source=replace(track,meta=replace(track.meta,offset_ms=137)) if shifted else track
            appearance=replace(style,timing_offset_ms=229) if shifted else style
            ir=with_points(binary,(source,appearance),points,dpr=1.5,roll_shift_ms=400 if shifted else 0)
            with SkiaRendererProcess(binary,selection=selection(target)) as process:
                configure_ir(process,ir)
                frames,_=collect(process,[t+(766 if shifted else 0) for t in [500,1200,1600,2200]])
            both.append(frames)
        assert all(np.array_equal(a,b) for a,b in zip(*both))
        outputs.append(both[0])
    a,b=[np.stack(frames).astype(float) for frames in outputs]
    a[...,:3]*=a[...,3:4]/255;b[...,:3]*=b[...,3:4]/255
    delta=np.abs(a-b)
    assert delta.mean()<.15 and (delta.max(axis=-1)>16).mean()<.003


def test_cpu_only_compilation_executes_the_same_segmented_clock(binary,project):
    directory=Path(binary).parent
    cpu=directory.parent/(directory.name.rsplit('-',1)[0]+'-raster')/Path(binary).name
    if not cpu.is_file():pytest.skip('Build the CPU-only sidecar')
    ir=with_points(binary,project,((200,0.),(400,.35),(1400,.35),(2000,1.)))
    output=[]
    for executable in [binary,cpu]:
        with SkiaRendererProcess(executable,selection=selection('raster')) as process:
            configure_ir(process,ir)
            frames,_=collect(process,[500,800,1600,2200])
        output.append(frames)
    assert all(np.array_equal(a,b) for a,b in zip(*output))


@pytest.mark.parametrize('mutation',['type','empty','one','start','end','reversed_time','reversed_progress',
    'fraction_time','bool_time','string_position','nan','inf','range','count','schema','feature'])
def test_bad_clock_preserves_previous_scene(binary,project,mutation):
    ir=with_points(binary,project,((200,0.),(400,.4),(1400,.4),(2000,1.)))
    points=ir['skia_layout']['lines'][0]['cells'][0]['wipe_points']
    cell=ir['skia_layout']['lines'][0]['cells'][0]
    if mutation=='type':cell['wipe_points']={}
    elif mutation=='empty':cell['wipe_points']=[]
    elif mutation=='one':cell['wipe_points']=points[:1]
    elif mutation=='start':points[0]['position']=.1
    elif mutation=='end':points[-1]['position']=.9
    elif mutation=='reversed_time':points[1]['time_ms']=100
    elif mutation=='reversed_progress':points[2]['position']=.3
    elif mutation=='fraction_time':points[1]['time_ms']=400.5
    elif mutation=='bool_time':points[1]['time_ms']=True
    elif mutation=='string_position':points[1]['position']='.4'
    elif mutation=='nan':points[1]['position']=float('nan')
    elif mutation=='inf':points[1]['time_ms']=float('inf')
    elif mutation=='range':points[-1]['time_ms']=86400001
    elif mutation=='count':cell['wipe_points']=points*1025
    elif mutation=='schema':ir['skia_layout']['wipe_schema']=2
    elif mutation=='feature':ir['required_features'].remove('p05_segmented_wipe')
    with SkiaRendererProcess(binary,selection=selection('raster')) as process:
        process.configure(*project,width=640,height=360,fps=60)
        before,_=collect(process,[800])
        with pytest.raises(NativeRendererError):configure_ir(process,ir)
        after,_=collect(process,[800],generation=2)
    assert np.array_equal(before[0],after[0])


def test_old_sidecar_is_rejected_before_backend_selection(monkeypatch,tmp_path):
    executable=tmp_path/'old-renderer';executable.touch()
    ready={'ok':True,'backend':'skia','schema':3,'selection_version':1,'resolved_layout_schema':2,
        'profiles':['p05_resolved_horizontal','p05_single_line_placement','p05_inline_roles',
                    'p05_fixed_row_grid','p05_page_collisions','p05_smart_horizontal'],
        'execution_targets':['raster']}
    monkeypatch.setattr(NativeRendererProcess,'start',lambda self:ready)
    monkeypatch.setattr(SkiaRendererProcess,'abort',lambda *a,**k:None)
    sent=[];monkeypatch.setattr(SkiaRendererProcess,'_send',lambda self,payload:sent.append(payload))
    with pytest.raises(NativeRendererError,match='分段走字'):
        SkiaRendererProcess(executable,selection=selection('raster')).start()
    assert not sent


@pytest.mark.skipif(sys.platform!='win32',reason='Direct2D GPU reference is Windows-only')
@pytest.mark.parametrize('mode',['reading_units','checkpoint_segments'])
@pytest.mark.parametrize('explicit',[False,True])
def test_resolved_main_ruby_clock_matches_actual_d2d_gpu(binary,project,mode,explicit):
    from test_subtitle_render_gpu_backend import _render_g1_frames
    from krok_helper.subtitle_render.domain.paint import KaraokeColors,KaraokeColorState,PaintFill
    track,style=project
    line=line_three()
    if explicit:
        line=replace(line,chars=[line.chars[0],replace(line.chars[1],explicit_start=True),line.chars[2]])
    line=replace(line,display_start_override_ms=0,display_end_override_ms=5000)
    ruby=clock_ruby()
    colors=KaraokeColors(before=KaraokeColorState(text=PaintFill(color='#FF0000')),
                         after=KaraokeColorState(text=PaintFill(color='#00FF00')))
    transparent=KaraokeColorState(text=PaintFill(color='#00000000'))
    style=replace(style,ruby_main_progress_mode=mode,karaoke_colors=colors,
        ruby_font_size_px=12,ruby_latin_font_size_px=12,ruby_font_family=style.font_family,
        ruby_font_family_latin=style.font_family,ruby_latin_font_stretch_pct=100,
        ruby_stroke_width_px=0,ruby_stroke2_enabled=False,ruby_decoration_kind='none',
        ruby_colors_follow_main=False,ruby_karaoke_colors=KaraokeColors(before=transparent,after=transparent))
    source=replace(track,lines=[line],rubies=[])
    ir=ir_for(binary,(source,style))
    timing=prepare_ruby_wipe_timing(line,prepared([(ruby,(0,1,2))]),mode)
    for cell,points in zip(ir['skia_layout']['lines'][0]['cells'],timing.main,strict=True):
        if points is not None:cell['wipe_points']=wipe_points_to_ir(points)
    times=[299,300,699,900,1100,1399,1400,1633,2000,2101]
    with NativeRendererProcess() as d2d:
        _,raw=_render_g1_frames(d2d,style,tuple(times),force_warp=False,
                              track=replace(source,rubies=[ruby]),width=640,height=360)
    with SkiaRendererProcess(binary,selection=selection('gpu')) as process:
        configure_ir(process,ir)
        frames,_=collect(process,times)
    def after_bounds(p):
        p=p.astype(np.int16)
        y,x=np.where((p[...,3]>200)&(p[...,1]>p[...,0]+50))
        return None if not len(x) else np.array([x.min(),y.min(),x.max(),y.max()])
    for time,reference,actual in zip(times,raw,frames,strict=True):
        a,b=after_bounds(np.frombuffer(reference,np.uint8).reshape(360,640,4)),after_bounds(actual)
        assert (a is None)==(b is None),(mode,explicit,time,a,b)
        if a is not None:assert np.max(np.abs(a-b))<=1,(mode,explicit,time,a,b)
