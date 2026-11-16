"""Right-to-left authored-cell placement is separate from singing reversal."""
from dataclasses import replace
import copy
import json
import sys

import numpy as np
import pytest

from test_subtitle_skia_reverse import application, binary, project, source as reverse_source
from test_subtitle_skia_roles import colors
from test_subtitle_skia_resolved import ir_for
from test_subtitle_skia_ruby_groups import configure
from test_subtitle_skia_protocol import collect, selection
from test_subtitle_skia_protocol import test_actual_g5_preview_uses_skia_even_when_gpu_off as run_preview
from test_subtitle_skia_protocol import test_product_export_short_mp4_uses_skia as run_mp4
from test_subtitle_skia_grid import grid
from krok_helper.subtitle_render.domain.models import SubtitleStyleScheme
from krok_helper.subtitle_render.domain.timing import RubyAnnotation
from krok_helper.subtitle_render.native.backend import NativeRendererProcess, NativeRendererError
from krok_helper.subtitle_render.native.skia_backend import SkiaRendererProcess


TIMES = [199, 200, 201, 399, 400, 401, 599, 600, 601, 899, 900, 901,
         1399, 1400, 1401, 1799, 1800, 1801, 2200]
KINDS = ['plain', 'pause', 'zero', 'all_zero', 'roles', 'negative_gap']


def rtl_source(project, kind='plain', *, reverse=False, effect='none'):
    track, style = reverse_source(project, kind if kind in {'pause','zero','all_zero'} else 'plain', effect)
    line = replace(track.lines[0], wipe_reverse=reverse)
    style = replace(style, right_to_left=True, karaoke_anim=effect, reverse_karaoke_anim=effect)
    if kind in {'negative_gap','collapsed_gap'}:
        style = replace(style, letter_spacing_px=-18 if kind=='negative_gap' else -100)
    if kind == 'units':
        line = replace(line, chars=[replace(c,text=t) for c,t in zip(line.chars,['A',' ','á','字'])])
    if kind == 'decomposed_unit':
        line = replace(line, chars=[replace(c,text=t) for c,t in zip(line.chars,['A',' ','a\u0301','字'])])
    if kind == 'ligature_only':
        line = replace(line, chars=[replace(line.chars[0],text='fi')])
    if kind == 'roles':
        style = replace(style, custom_style_schemes={'lead': SubtitleStyleScheme(
            font_size_px=70, karaoke_colors=colors('#FFFF0040', '#FF00FF40'))})
        line = replace(line, chars=[line.chars[0], replace(line.chars[1], role_label='lead'), *line.chars[2:]])
    return replace(track, lines=[line]), style


@pytest.mark.parametrize('reverse', [False, True])
def test_rtl_moves_cells_without_mirroring_glyphs_or_retiming(binary, project, reverse, tmp_path):
    track, style = rtl_source(project, reverse=reverse)
    ir = ir_for(binary, (track, style))
    ltr = ir_for(binary, (track, replace(style, right_to_left=False)))
    cells, plain = ir['skia_layout']['lines'][0]['cells'], ltr['skia_layout']['lines'][0]['cells']
    assert all(a['origin_x'] > b['origin_x'] for a,b in zip(cells,cells[1:]))
    assert [c['metrics'] for c in cells] == [c['metrics'] for c in plain]
    assert [c['run_index'] for c in cells] == [c['run_index'] for c in plain]
    assert ir['track']['lines'][0]['resolved_intervals'] == ltr['track']['lines'][0]['resolved_intervals']
    assert ir['skia_layout']['lines'][0]['text_flow'] == 'rtl'
    assert ir['skia_layout']['horizontal_rtl_schema'] == 1
    assert 'p05_horizontal_rtl' in ir['required_features']
    assert all(not run.get('rtl',False) for run in ir['skia_layout']['runs'])
    if reverse:
        assert [c['clock_index'] for c in cells] == list(reversed(range(len(cells))))
    else:
        assert all('clock_index' not in c for c in cells)
        (tmp_path/'rtl-flow-layout.json').write_text(json.dumps({
            'features':ir['required_features'], 'schema':ir['skia_layout']['horizontal_rtl_schema'],
            'lines':ir['skia_layout']['lines'], 'intervals':ir['track']['lines'][0]['resolved_intervals']},
            sort_keys=True),encoding='utf-8')


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('kind', KINDS)
@pytest.mark.parametrize('reverse', [False,True])
@pytest.mark.parametrize('effect', ['none','no_wipe'])
@pytest.mark.parametrize('semantics', ['legacy','n3_1074'])
@pytest.mark.parametrize('dpr', [1,1.5])
def test_rtl_flow_matches_actual_gpu(binary, project, kind, reverse, effect, semantics, dpr):
    assert_actual_gpu_parity(binary,project,kind,reverse,effect,semantics,dpr)


def assert_actual_gpu_parity(binary,project,kind,reverse,effect,semantics,dpr,*,alignment=None,text_flow=True):
    from test_subtitle_render_gpu_backend import _render_g1_frames
    track,style=rtl_source(project,kind,reverse=reverse,effect=effect)
    style=replace(style,layout_semantics=semantics,right_to_left=text_flow)
    if alignment:
        style=replace(style,dual_line_layout=True,line_horizontal_layout='per_row',
                      line_alignments=[alignment],row1_align=alignment,row1_offset_x=13,row1_offset_y=7)
    with NativeRendererProcess() as process:
        _,raw=_render_g1_frames(process,style,tuple(TIMES),force_warp=False,track=track,
                                width=640,height=360,dpr=dpr,realization_enabled=False)
    refs=[np.frombuffer(p,np.uint8).reshape(round(360*dpr),round(640*dpr),4) for p in raw]
    with SkiaRendererProcess(binary,selection=selection('gpu')) as process:
        configure(process,ir_for(binary,(track,style),dpr=dpr))
        frames,_=collect(process,TIMES)
    case=kind,reverse,effect,semantics,dpr,alignment
    for time,a,b in zip(TIMES,refs,frames):
        def bounds(frame):
            ys,xs=np.where(frame[...,3]>127)
            return np.array([xs.min(),ys.min(),xs.max(),ys.max()])
        aa,bb=bounds(a),bounds(b)
        assert abs(aa-bb).max()<=1,(case,time,'ink',aa,bb)
        sung=lambda p:(p[...,1].astype(float)*p[...,3]/255**2).sum()
        full_a,full_b=sung(refs[-1]),sung(frames[-1])
        height=aa[3]-aa[1]+1
        width=(full_a+full_b)/(2*height)
        assert abs(sung(a)/full_a-sung(b)/full_b)*width<=1,(case,time,'front')


@pytest.mark.parametrize('reverse',[False,True])
@pytest.mark.parametrize('effect',['none','no_wipe'])
def test_rtl_gpu_raster_seek_and_offsets(binary,project,reverse,effect):
    track,style=rtl_source(project,'roles',reverse=reverse,effect=effect)
    results=[]
    for target in ('gpu','raster'):
        original=None
        for shifted in (False,True):
            source=(replace(track,meta=replace(track.meta,offset_ms=137)) if shifted else track,
                    replace(style,timing_offset_ms=229) if shifted else style)
            ir=ir_for(binary,source,dpr=1.5,roll_shift_ms=400 if shifted else 0)
            with SkiaRendererProcess(binary,selection=selection(target)) as process:
                configure(process,ir)
                frames,_=collect(process,[t+(766 if shifted else 0) for t in [*TIMES,200,900]])
            assert np.array_equal(frames[1],frames[-2]) and np.array_equal(frames[10],frames[-1])
            if shifted:assert all(np.array_equal(a,b) for a,b in zip(original,frames))
            else:original=frames
        results.append(original)
    for a,b in zip(*results):
        def premult(p):
            p=p.astype(float);p[...,:3]*=p[...,3:]/255
            return p
        delta=abs(premult(a)-premult(b))
        assert delta.mean()<.15 and np.mean(delta.max(axis=-1)>16)<.003


@pytest.mark.parametrize('mutation',['feature','schema','schema_bool','schema_string','schema_version',
                                     'flow','flow_type','flow_missing','source_type','source_direction','run_direction'])
@pytest.mark.parametrize('target',['gpu','raster'])
def test_invalid_rtl_contract_retains_previous_scene(binary,project,mutation,target):
    ir=ir_for(binary,rtl_source(project))
    bad=copy.deepcopy(ir)
    if mutation=='feature':bad['required_features'].remove('p05_horizontal_rtl')
    elif mutation=='schema':del bad['skia_layout']['horizontal_rtl_schema']
    elif mutation.startswith('schema_'):
        bad['skia_layout']['horizontal_rtl_schema']={'schema_bool':True,'schema_string':'1','schema_version':2}[mutation]
    elif mutation=='flow':bad['skia_layout']['lines'][0]['text_flow']='ltr'
    elif mutation=='flow_type':bad['skia_layout']['lines'][0]['text_flow']=True
    elif mutation=='flow_missing':del bad['skia_layout']['lines'][0]['text_flow']
    elif mutation=='source_type':bad['style']['right_to_left']='true'
    elif mutation=='source_direction':bad['style']['right_to_left']=False
    else:bad['skia_layout']['runs'][0]['rtl']=True
    with SkiaRendererProcess(binary,selection=selection(target)) as process:
        configure(process,ir);before,_=collect(process,[750])
        with pytest.raises(NativeRendererError):configure(process,bad)
        after,_=collect(process,[750],generation=2)
    assert np.array_equal(before[0],after[0])


@pytest.mark.parametrize('mutation',['schema','feature','bool','version'])
def test_old_sidecar_rejected_before_selection(binary,monkeypatch,mutation):
    process=SkiaRendererProcess(binary,selection=selection('raster'))
    try:ready=NativeRendererProcess.start(process)
    finally:process.abort(reason='RTL handshake fixture')
    if mutation=='schema':del ready['horizontal_rtl_schema']
    elif mutation=='feature':ready['profiles'].remove('p05_horizontal_rtl')
    else:ready['horizontal_rtl_schema']=True if mutation=='bool' else 2
    sent=[]
    monkeypatch.setattr(NativeRendererProcess,'start',lambda self:ready)
    monkeypatch.setattr(SkiaRendererProcess,'abort',lambda *a,**k:None)
    monkeypatch.setattr(SkiaRendererProcess,'_send',lambda self,p:sent.append(p))
    with pytest.raises(NativeRendererError,match='右至左'):SkiaRendererProcess(binary).start()
    assert not sent


@pytest.mark.parametrize('value',[1,'true',None])
def test_python_flow_flag_must_be_boolean(binary,project,value):
    track,style=rtl_source(project)
    with pytest.raises(ValueError,match='布尔值'):ir_for(binary,(track,replace(style,right_to_left=value)))


@pytest.mark.parametrize('kind',['upper','lower','vertical'])
def test_unvalidated_rtl_combinations_stay_explicitly_gated(binary,project,kind):
    track,style=rtl_source(project)
    if kind=='upper':
        ruby=RubyAnnotation('字','A',pos_start_ms=600,pos_end_ms=900,
                            target_line_index=0,target_char_start=1,target_char_end=2)
        track=replace(track,rubies=[ruby])
    elif kind=='lower':style=replace(style,lower_ruby_enabled=True)
    else:style=replace(style,vertical=True)
    with pytest.raises(ValueError):ir_for(binary,(track,style))


@pytest.mark.parametrize('text',['fi','AA','字字','  ','a\u0301'])
def test_rtl_multicodepoint_cells_remain_gated_until_geometry_calibration(binary,project,text):
    track,style=rtl_source(project)
    line=track.lines[0]
    track=replace(track,lines=[replace(line,chars=[replace(line.chars[0],text=text),*line.chars[1:]])])
    with pytest.raises(ValueError,match='单码点'):ir_for(binary,(track,style))


@pytest.mark.parametrize('target',['gpu','raster'])
def test_native_rejects_multicodepoint_rtl_before_accepting_forged_geometry(binary,project,target):
    ir=ir_for(binary,rtl_source(project))
    bad=copy.deepcopy(ir);bad['track']['lines'][0]['chars'][0]['text']='AA'
    with SkiaRendererProcess(binary,selection=selection(target)) as process:
        configure(process,ir);before,_=collect(process,[750])
        with pytest.raises(NativeRendererError,match='单码点'):configure(process,bad)
        after,_=collect(process,[750],generation=2)
    assert np.array_equal(before[0],after[0])


@pytest.mark.parametrize('reverse',[False,True])
@pytest.mark.parametrize('blank',[False,True])
@pytest.mark.parametrize('target',['gpu','raster'])
def test_empty_and_blank_rtl_lines(binary,project,reverse,blank,target):
    from krok_helper.subtitle_render.domain.models import TimingChar
    track,style=rtl_source(project,reverse=reverse)
    chars=[TimingChar(' ',200),TimingChar(' ',600)] if blank else []
    track=replace(track,lines=[replace(track.lines[0],chars=chars)])
    ir=ir_for(binary,(track,style))
    assert ir['skia_layout']['lines'][0]['text_flow']=='rtl'
    with SkiaRendererProcess(binary,selection=selection(target)) as process:
        configure(process,ir);frames,_=collect(process,[199,750,2000])
    assert all(not p.any() for p in frames)


@pytest.mark.parametrize('effect',['none','no_wipe'])
def test_rtl_translucent_interiors_do_not_stack(binary,project,effect):
    track,style=rtl_source(project,effect=effect)
    style=replace(style,karaoke_colors=colors('#80FF0000','#8000FF00'))
    ir=ir_for(binary,(track,style))
    for target in ('gpu','raster'):
        with SkiaRendererProcess(binary,selection=selection(target)) as process:
            configure(process,ir);frames,_=collect(process,[199,750,2200])
        assert all(p[...,3].max()==128 for p in frames)
        assert not np.array_equal(frames[0],frames[2])


@pytest.mark.parametrize('semantics',['legacy','n3_1074'])
@pytest.mark.parametrize('smart',['none','center_position','equal_margins'])
def test_rtl_uses_shared_page_and_grid_plan(binary,grid,semantics,smart):
    track,style=grid
    style=replace(style,right_to_left=True,layout_semantics=semantics,smart_horizontal=smart)
    ir=ir_for(binary,(track,style))
    ltr=ir_for(binary,(track,replace(style,right_to_left=False)))
    assert ir['track']['lines']==ltr['track']['lines']
    for final,plain in zip(ir['skia_layout']['lines'],ltr['skia_layout']['lines']):
        assert [c['metrics'] for c in final['cells']]==[c['metrics'] for c in plain['cells']]
        assert all(a['origin_x']>b['origin_x'] for a,b in zip(final['cells'],final['cells'][1:]))
    images=[]
    for target in ('gpu','raster'):
        with SkiaRendererProcess(binary,selection=selection(target)) as process:
            configure(process,ir);frames,_=collect(process,[1400,2400,3400,1400])
        assert np.array_equal(frames[0],frames[-1]);images.append(frames)
    for a,b in zip(*images):
        delta=abs(a.astype(float)-b.astype(float))
        assert delta.mean()<.15 and np.mean(delta.max(axis=-1)>16)<.003


@pytest.mark.parametrize('gpu,missing_loader',[(True,False),(False,False),(True,True)])
def test_real_rtl_preview(binary,project,monkeypatch,tmp_path,gpu,missing_loader):
    run_preview(binary,monkeypatch,tmp_path,rtl_source(project,'roles'),gpu,missing_loader)


@pytest.mark.parametrize('gpu,missing_loader',[(True,False),(False,False),(True,True)])
def test_real_rtl_mp4(binary,project,monkeypatch,tmp_path,gpu,missing_loader):
    run_mp4(binary,monkeypatch,tmp_path,rtl_source(project,reverse=True,effect='no_wipe'),gpu,missing_loader)


@pytest.mark.skipif(sys.platform != 'win32',reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('alignment',['left','center','right'])
@pytest.mark.parametrize('semantics',['legacy','n3_1074'])
@pytest.mark.parametrize('reverse',[False,True])
def test_spaces_precomposed_letters_and_row_anchors_match_gpu(binary,project,alignment,semantics,reverse):
    assert_actual_gpu_parity(binary,project,'units',reverse,'none',semantics,1.5,alignment=alignment)


@pytest.mark.skipif(sys.platform != 'win32',reason='Actual Direct2D GPU reference')
@pytest.mark.xfail(strict=True,raises=AssertionError,
    reason='Existing fi-ligature ink mismatch also reproduces with RTL disabled: about 2px at DPR1.5; P05.2 remains open.')
def test_recorded_existing_ltr_ligature_gap(binary,project):
    assert_actual_gpu_parity(binary,project,'ligature_only',False,'none','legacy',1.5,text_flow=False)


@pytest.mark.skipif(sys.platform != 'win32',reason='Actual Direct2D GPU reference')
@pytest.mark.xfail(strict=True,raises=AssertionError,
    reason='Existing LTR decomposed-accent cell width differs from GPU by about 3px at DPR1.5; P05.2 remains open.')
def test_recorded_existing_ltr_decomposed_width_gap(binary,project):
    assert_actual_gpu_parity(binary,project,'decomposed_unit',False,'none','n3_1074',1.5,
                              alignment='left',text_flow=False)


@pytest.mark.skipif(sys.platform != 'win32',reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('reverse',[False,True])
@pytest.mark.parametrize('semantics',['legacy','n3_1074'])
def test_rtl_collapsed_negative_spacing_matches_gpu(binary,project,reverse,semantics):
    assert_actual_gpu_parity(binary,project,'collapsed_gap',reverse,'none',semantics,1.5)
