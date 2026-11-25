"""Page-wide SmartHorizon from canonical widths, independent of visibility."""
from dataclasses import replace
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from test_subtitle_skia_resolved import application, binary, project, ir_for
from test_subtitle_skia_protocol import collect, selection
from krok_helper.subtitle_render.domain.models import LyricsLayout, SubtitleStyleScheme, TimingChar, TimingLine
from krok_helper.subtitle_render.domain.timing import TrackPage, TrackSection, TrackPagePlan
from krok_helper.subtitle_render.engine.layout.layout_context import layout_pass
from krok_helper.subtitle_render.engine.layout.page.pagination import renderable_page_map, line_center_override
from krok_helper.subtitle_render.engine.layout.page.skia_smart_horizontal import smart_horizontal_shift
from krok_helper.subtitle_render.engine.layout.display.skia_collision import SkiaCollisionGeometry
from krok_helper.subtitle_render.engine.layout.line.style import style_for_line
from krok_helper.subtitle_render.engine.text.skia_metrics import measure_track
from krok_helper.subtitle_render.native.backend import NativeRendererError, NativeRendererProcess
from krok_helper.subtitle_render.native.skia_backend import SkiaRendererProcess
from krok_helper.subtitle_render.native.skia_fonts import resolve_skia_font
from krok_helper.subtitle_render.native.skia_text import SkiaTextMetricsProcess


@pytest.mark.parametrize('mode,align,expected',[
    ('center_position','left',215),('center_position','right',-215.5),
    ('equal_margins','left',156),('equal_margins','right',-156),
    ('center_position','center',0),('equal_margins','center',0),('none','left',0)])
def test_smart_arithmetic_preserves_odd_canvas_and_fractional_widths(mode,align,expected):
    assert smart_horizontal_shift(mode,width=641,own_width=100.5,own_font=51,alignment=align,
        margin=30,maxima={'left':100,'center':80,'right':140},page_head_font=51)==expected


def test_smart_does_not_push_long_lines_or_invent_a_missing_alignment():
    args=dict(width=640,own_width=500,own_font=50,alignment='left',margin=30,
              maxima={'left':500,'center':80,'right':400},page_head_font=50)
    assert smart_horizontal_shift('center_position',**args)==0
    assert smart_horizontal_shift('equal_margins',**args)==0
    args['maxima']={'left':50,'center':80,'right':0}
    assert smart_horizontal_shift('equal_margins',**args)==0


@pytest.fixture
def smart(project):
    track,style=project
    style=replace(style,dual_line_layout=True,line_alignments=['left','center','right'],
        line_offsets_x=[7,-11,13],line_horizontal_layout='asymmetric',smart_horizontal='equal_margins',
        horizontal_margin_px=30,line_y_margin_px=40,line_gap_px=120,
        allow_inter_page_line_overlap=False,auto_fill_section_time=True,sync_entry=False,
        custom_style_schemes={'lead':SubtitleStyleScheme(font_family=style.font_family,
            font_family_latin=style.font_family,font_size_px=72,latin_font_size_px=112,font_weight=400)})
    lines=[TimingLine(chars=[TimingChar(t,200+j*200,role_label='lead' if i==0 and j==0 else None)
        for j,t in enumerate(text)],end_ms=1800,display_start_override_ms=0,display_end_override_ms=12000)
        for i,text in enumerate(('A字','字','字字A'))]
    return replace(track,lines=lines),style


def test_persisted_page_membership_and_short_page_centering_use_authored_pages(smart):
    track,style=smart
    track=replace(track,page_plan=TrackPagePlan(sections=[TrackSection(pages=[TrackPage(2),TrackPage(1)])]))
    with layout_pass():
        page=renderable_page_map(track,style,respect_page_plan=True)
        assert len(page[id(track.lines[0])])==2 and len(page[id(track.lines[2])])==1
        assert not line_center_override(track,track.lines[0],style,respect_page_plan=True)
        assert line_center_override(track,track.lines[2],style,respect_page_plan=True)
        # The production policy retains timing-derived membership, even when
        # the experimental adapter populated the same pass's page cache first.
        assert len(renderable_page_map(track,style)[id(track.lines[0])])==3
        assert not line_center_override(track,track.lines[2],style)
        track.page_plan=TrackPagePlan(sections=[TrackSection(pages=[TrackPage(1),TrackPage(2)])])
        assert line_center_override(track,track.lines[0],style,respect_page_plan=True)
        assert not line_center_override(track,track.lines[2],style,respect_page_plan=True)


@pytest.mark.parametrize('semantics',['legacy','n3_1074'])
@pytest.mark.parametrize('mode',['center_position','equal_margins'])
def test_page_wide_shift_uses_invisible_siblings_and_preserves_timing(binary,smart,semantics,mode,tmp_path):
    track,style=smart
    style=replace(style,layout_semantics=semantics,smart_horizontal=mode)
    a=ir_for(binary,(track,style))
    b=ir_for(binary,(track,replace(style,smart_horizontal='none')))
    assert [l['resolved_intervals'] for l in a['track']['lines']]==[l['resolved_intervals'] for l in b['track']['lines']]
    coords=lambda ir:[[c['origin_x'] for c in l['cells']] for l in ir['skia_layout']['lines']]
    assert coords(a)[1]==coords(b)[1] # Centered sibling is unaffected.
    assert coords(a)[0]!=coords(b)[0] and coords(a)[2]!=coords(b)[2]
    delayed=replace(track,lines=[track.lines[0],*[replace(l,display_start_override_ms=3000) for l in track.lines[1:]]])
    c=ir_for(binary,(delayed,style))
    assert coords(a)==coords(c)
    shifted=ir_for(binary,(track,replace(style,line_offsets_x=[24,6,30])))
    for left,right in zip(a['skia_layout']['lines'],shifted['skia_layout']['lines']):
        assert all(y['origin_x']-x['origin_x']==pytest.approx(17,abs=1e-4) for x,y in zip(left['cells'],right['cells']))
    if semantics=='n3_1074' and mode=='equal_margins':
        (tmp_path/'smart-layout.json').write_text(json.dumps({'cells':[l['cells'] for l in a['skia_layout']['lines']],
            'intervals':[l['resolved_intervals'] for l in a['track']['lines']]},sort_keys=True),encoding='utf-8')


def test_first_character_main_slot_controls_smart_font_not_latin_size(binary,smart):
    track,style=smart
    def run(scheme):
        return ir_for(binary,(track,replace(style,custom_style_schemes={'lead':scheme})))
    base=run(style.custom_style_schemes['lead'])
    larger=run(replace(style.custom_style_schemes['lead'],font_size_px=90))
    # The page-head character A keeps its 112px Latin outline, but its role's
    # Japanese size changes the slack by 18px, producing +/-9px shifts.
    for i,delta in ((0,9),(2,-9)):
        assert larger['skia_layout']['lines'][i]['cells'][0]['origin_x']-base['skia_layout']['lines'][i]['cells'][0]['origin_x']==pytest.approx(delta,abs=1e-4)
    assert larger['skia_layout']['lines'][0]['cells'][0]['metrics']==base['skia_layout']['lines'][0]['cells'][0]['metrics']


def test_collision_uses_smart_position_and_invalidates_when_a_sibling_changes_lane(binary,smart):
    track,style=smart
    styles=[style_for_line(style,line) for line in track.lines]
    with SkiaTextMetricsProcess(binary) as process:
        measured=measure_track(track,style,styles,measure=process.measure,resolve_font=resolve_skia_font)
    geometry=SkiaCollisionGeometry(track,style,styles,measured)
    items=[SimpleNamespace(line=line,lane=i,page_line_count=3) for i,line in enumerate(track.lines)]
    geometry.set_lanes(items)
    first=geometry.geometry(items[2],641,720)
    ir=ir_for(binary,smart)
    cells=ir['skia_layout']['lines'][2]['cells']
    # Canvas width differs only by 1px from ir_for; use the actual placement
    # entry point for an exact comparison against the shared final geometry.
    item=SimpleNamespace(**vars(items[2]),layout_style=styles[2],center_override=False)
    position=geometry.position(item,640,360)
    assert position.offset_x==pytest.approx(cells[0]['origin_x']-geometry.locals[2].cells[0]['origin_x'],abs=1e-4)
    changed=[replace_item(items[0],lane=1),*items[1:]]
    geometry.set_lanes(changed)
    second=geometry.geometry(items[2],641,720)
    assert first is not second and first.cross_min!=second.cross_min


def replace_item(item,**changes):
    return SimpleNamespace(**(vars(item)|changes))


@pytest.mark.parametrize('mode',['center_position','equal_margins'])
def test_persisted_short_pages_and_layout_override_keep_correct_members(binary,smart,mode):
    track,style=smart
    style=replace(style,smart_horizontal=mode,layouts=[LyricsLayout(layout_id='pair',line_alignments=['left','right'],
        smart_horizontal=mode,line_gap_px=120,horizontal_margin_px=30)])
    track=replace(track,page_plan=TrackPagePlan(sections=[TrackSection(pages=[TrackPage(2,'pair'),TrackPage(1)])]),
        lines=[replace(l,layout_index=1 if i<2 else 0) for i,l in enumerate(track.lines)])
    ir=ir_for(binary,(track,style))
    assert [l['center_override'] for l in ir['track']['lines']]==[False,False,True]
    last=ir['skia_layout']['lines'][2]['cells']
    plain=ir_for(binary,(track,replace(style,smart_horizontal='none')))
    assert last[0]['origin_x']!=plain['skia_layout']['lines'][2]['cells'][0]['origin_x']


@pytest.mark.parametrize('semantics',['legacy','n3_1074'])
@pytest.mark.parametrize('mode',['center_position','equal_margins'])
def test_smart_gpu_raster_and_seek_share_final_geometry(binary,smart,semantics,mode):
    track,style=smart
    style=replace(style,layout_semantics=semantics,smart_horizontal=mode)
    output=[]
    for target in ('gpu','raster'):
        with SkiaRendererProcess(binary,selection=selection(target)) as process:
            process.configure(track,style,width=641,height=720,fps=60,dpr=1.5)
            frames,_=collect(process,[800,1900,800,13000])
            assert np.array_equal(frames[0],frames[2]) and not frames[3].any()
            pixels=np.stack(frames).astype(float);pixels[...,:3]*=pixels[...,3:]/255
            output.append(pixels)
    delta=np.abs(output[0]-output[1])
    assert delta.mean()<.15 and np.mean(np.max(delta,axis=-1)>16)<.003


@pytest.mark.skipif(sys.platform!='win32',reason='Direct2D GPU comparison')
@pytest.mark.parametrize('semantics',['legacy','n3_1074'])
@pytest.mark.parametrize('mode',['center_position','equal_margins'])
def test_smart_page_geometry_matches_d2d_gpu(binary,smart,semantics,mode):
    from test_subtitle_render_gpu_backend import _render_g1_frames
    track,style=smart
    with NativeRendererProcess() as d2d, SkiaRendererProcess(binary,selection=selection('gpu')) as skia:
        for count in (3,2,1):
            source=replace(track,lines=track.lines[:count])
            appearance=replace(style,layout_semantics=semantics,smart_horizontal=mode)
            _,raw=_render_g1_frames(d2d,appearance,(10000,),force_warp=False,track=source,width=641,height=720)
            skia.configure(source,appearance,width=641,height=720,fps=60)
            frames,_=collect(skia,[10000])
            def boxes(p):
                ys=np.flatnonzero((p[...,3]>127).any(axis=1))
                result=[]
                for band in np.split(ys,np.flatnonzero(np.diff(ys)>1)+1):
                    if not len(band):continue
                    xs=np.flatnonzero((p[band[0]:band[-1]+1,:,3]>127).any(axis=0))
                    result.append([xs[0],band[0],xs[-1],band[-1]])
                return result
            a,b=boxes(np.frombuffer(raw[0],np.uint8).reshape(720,641,4)),boxes(frames[0])
            assert len(a)==len(b)==count,(semantics,mode,count,a,b)
            assert np.max(np.abs(np.array(a)-np.array(b)))<=1,(semantics,mode,count,a,b)


@pytest.mark.skipif(sys.platform!='win32',reason='Installed Windows fonts and Direct2D comparison')
@pytest.mark.parametrize('semantics',['legacy','n3_1074'])
@pytest.mark.parametrize('mode',['center_position','equal_margins'])
def test_smart_mixed_installed_fonts_match_d2d_gpu(binary,smart,semantics,mode):
    if not Path('C:/Windows/Fonts/meiryo.ttc').is_file():pytest.skip('Meiryo fixture')
    from test_subtitle_render_gpu_backend import _render_g1_frames
    track,style=smart
    lead=replace(style.custom_style_schemes['lead'],font_family='Meiryo',font_family_latin='Arial')
    style=replace(style,font_family='Meiryo',font_family_latin='Arial',custom_style_schemes={'lead':lead},
                  layout_semantics=semantics,smart_horizontal=mode)
    with NativeRendererProcess() as d2d:
        _,raw=_render_g1_frames(d2d,style,(10000,),force_warp=False,track=track,width=641,height=720)
    with SkiaRendererProcess(binary,selection=selection('gpu')) as skia:
        skia.configure(track,style,width=641,height=720,fps=60)
        frames,_=collect(skia,[10000])
    def bounds(p):
        y,x=np.where(p[...,3]>127)
        return np.array([x.min(),y.min(),x.max(),y.max()])
    expected=np.frombuffer(raw[0],np.uint8).reshape(720,641,4)
    assert np.max(np.abs(bounds(expected)-bounds(frames[0])))<=1
