"""Canonical single-line anchors, including independent legacy/N3 semantics."""
from dataclasses import replace
from itertools import product
import json
import sys
from types import SimpleNamespace

import numpy as np
import pytest

from test_subtitle_skia_font_slots import application, binary, mixed, project
from test_subtitle_skia_protocol import collect, selection
from test_subtitle_skia_resolved import ir_for
from krok_helper.subtitle_render.domain.models import LyricsLayout, Style, TimingChar, TimingLine
from krok_helper.subtitle_render.engine.layout.line.skia_placement import place_horizontal_line as place_single_line
from krok_helper.subtitle_render.engine.render.skia_ir import build_skia_render_ir
from krok_helper.subtitle_render.native.backend import NativeRendererError, NativeRendererProcess
from krok_helper.subtitle_render.native.skia_backend import SkiaRendererProcess
from krok_helper.subtitle_render.native.skia_fonts import resolve_skia_font
from krok_helper.subtitle_render.native.skia_text import SkiaTextMetricsProcess


def item(style, **kwargs):
    return SimpleNamespace(layout_style=style, lane=0, page_line_count=1, center_override=False, **kwargs)


@pytest.mark.parametrize('semantics,expected', [('legacy',(195,237.5,280)),('n3_1074',(100,195,290))])
def test_baseline_box_semantics_are_content_independent(semantics, expected):
    for position, baseline in zip(('top','center','bottom'), expected):
        style=Style(layout_semantics=semantics,dual_line_layout=False,line_horizontal_layout='center',
                    font_size_px=50,line_y_position=position,line_y_margin_px=60,ruby_font_size_px=45,
                    ruby_gap_px=0,ruby_stroke_width_px=10)
        result=place_single_line(item(style),{'ascent':80,'descent':20},width=640,height=360,
                                 ink_left=10,ink_right=110,layout_width=150)
        assert result.baseline_y==baseline


@pytest.mark.parametrize('semantics,expected', [('legacy',(20,267,514)),('n3_1074',(30,252,474))])
def test_horizontal_ink_and_cell_grid_are_distinct(semantics, expected):
    for align,x in zip(('left','center','right'),expected):
        style=Style(layout_semantics=semantics,dual_line_layout=False,line_horizontal_layout='asymmetric',
                    line_alignments=[align],horizontal_margin_px=23,line_offsets_x=[7])
        result=place_single_line(item(style),{'ascent':40,'descent':10},width=640,height=360,
                                 ink_left=10,ink_right=110,layout_width=150)
        assert result.offset_x==x


def test_bottom_tail_alignment_does_not_change_single_lane_offset_slot():
    style=Style(dual_line_layout=False,line_horizontal_layout='asymmetric',line_y_position='bottom',
                line_alignments=['left','right'],line_offsets_x=[7,99],horizontal_margin_px=23)
    result=place_single_line(item(style),{'ascent':40,'descent':10},width=640,height=360,
                             ink_left=10,ink_right=110,layout_width=150)
    assert result.offset_x==514


@pytest.mark.parametrize('semantics',['legacy','n3_1074'])
def test_single_line_matrix_is_canonical_and_timing_stays_shared(binary,mixed,semantics,tmp_path):
    track,style=mixed
    snapshots=[]
    with SkiaTextMetricsProcess(binary) as process:
        for align,position in product(('left','center','right'),('top','center','bottom')):
            appearance=replace(style,layout_semantics=semantics,line_horizontal_layout='asymmetric',
                               line_alignments=[align],line_y_position=position,horizontal_margin_px=23,
                               line_y_margin_px=31,line_offsets_x=[7])
            ir=build_skia_render_ir(track,appearance,measure=process.measure,resolve_font=resolve_skia_font,
                                    width=640,height=360,fps=60)
            cells=ir['skia_layout']['lines'][0]['cells']
            snapshots.append({'align':align,'position':position,'cells':cells,
                              'intervals':ir['track']['lines'][0]['resolved_intervals']})
    assert all(s['intervals']==snapshots[0]['intervals'] for s in snapshots)
    assert snapshots[0]['cells'][0]['baseline_y'] < snapshots[1]['cells'][0]['baseline_y'] < snapshots[2]['cells'][0]['baseline_y']
    if semantics=='n3_1074':
        (tmp_path/'placement-layout.json').write_text(json.dumps(snapshots,sort_keys=True),encoding='utf-8')


@pytest.mark.parametrize('semantics',['legacy','n3_1074'])
def test_row_offsets_apply_once_and_do_not_change_intervals(binary,mixed,semantics):
    track,style=mixed
    style=replace(style,layout_semantics=semantics,line_horizontal_layout='per_row',row1_align='right',
                  row1_offset_x=0,row1_offset_y=0,line_offsets_x=[0],horizontal_margin_px=99)
    a=ir_for(binary,(track,style))
    b=ir_for(binary,(track,replace(style,row1_offset_x=-17,row1_offset_y=9,line_offsets_x=[13])))
    left,right=a['skia_layout']['lines'][0]['cells'],b['skia_layout']['lines'][0]['cells']
    for x,y in zip(left,right):
        assert y['baseline_y']-x['baseline_y']==pytest.approx(9,abs=1e-4)
        for key in ('origin_x','wipe_left','wipe_right'):
            assert y[key]-x[key]==pytest.approx(-4,abs=1e-4)
    assert a['track']['lines'][0]['resolved_intervals']==b['track']['lines'][0]['resolved_intervals']


def test_layout_override_and_negative_margins_use_resolved_line_style(binary,mixed):
    track,style=mixed
    style=replace(style,line_horizontal_layout='asymmetric',layouts=[LyricsLayout(
        line_y_position='top',line_y_margin_px=-5,line_alignments=['right'],horizontal_margin_px=-11,
        line_offsets_x=[-7],ruby_gap_px=-100)])
    source=replace(track,lines=[replace(track.lines[0],layout_index=1)])
    ir=ir_for(binary,(source,style))
    cells=ir['skia_layout']['lines'][0]['cells']
    assert cells[0]['baseline_y']==pytest.approx(35,abs=1e-4) # ascent 40, no ruby reserve after clamping.
    assert max(c['wipe_right'] for c in cells)==pytest.approx(644,abs=1e-4)


def test_old_sidecar_without_placement_capability_is_rejected(monkeypatch,tmp_path):
    executable=tmp_path/'old';executable.touch()
    ready={'ok':True,'backend':'skia','schema':3,'selection_version':1,'resolved_layout_schema':2,
           'profiles':['p05_resolved_horizontal'],'execution_targets':['raster']}
    monkeypatch.setattr(NativeRendererProcess,'start',lambda self:ready)
    monkeypatch.setattr(SkiaRendererProcess,'abort',lambda *a,**k:None)
    sent=[]
    monkeypatch.setattr(SkiaRendererProcess,'_send',lambda self,payload:sent.append(payload))
    with pytest.raises(NativeRendererError,match='单行定位'): SkiaRendererProcess(executable).start()
    assert not sent


@pytest.mark.parametrize('semantics',['legacy','n3_1074'])
def test_gpu_raster_placement_and_dpr(binary,mixed,semantics):
    track,style=mixed
    style=replace(style,layout_semantics=semantics,line_horizontal_layout='per_row',row1_align='right',
                  row1_offset_x=-31,row1_offset_y=7,line_y_position='center',line_offsets_x=[-9])
    output=[]
    for target in ('gpu','raster'):
        with SkiaRendererProcess(binary,selection=selection(target)) as process:
            process.configure(track,style,width=640,height=360,fps=60,dpr=1.5)
            frames,_=collect(process,[800,1900,800])
            assert np.array_equal(frames[0],frames[2])
            pixels=np.stack(frames).astype(float);pixels[...,:3]*=pixels[...,3:]/255
            output.append(pixels)
    delta=np.abs(output[0]-output[1])
    assert delta.mean()<.15 and np.mean(np.max(delta,axis=-1)>16)<.003


@pytest.mark.skipif(sys.platform!='win32',reason='Direct2D GPU comparison')
@pytest.mark.parametrize('semantics',['legacy','n3_1074'])
def test_nine_positions_match_d2d_gpu(binary,mixed,semantics):
    from test_subtitle_render_gpu_backend import _render_g1_frames
    track,style=mixed
    source=replace(track,lines=[TimingLine(chars=[TimingChar(t,200+i*200) for i,t in enumerate(['字','A','한'])],end_ms=1800)])
    def bounds(p):
        y,x=np.where(p[...,3]>127)
        return np.array([x.min(),y.min(),x.max(),y.max()])
    with NativeRendererProcess() as d2d, SkiaRendererProcess(binary,selection=selection('gpu')) as skia:
        for align,position in product(('left','center','right'),('top','center','bottom')):
            appearance=replace(style,layout_semantics=semantics,line_horizontal_layout='asymmetric',
                               line_alignments=[align],line_y_position=position,horizontal_margin_px=23,
                               line_y_margin_px=31,line_offsets_x=[7])
            _,raw=_render_g1_frames(d2d,appearance,(1900,),force_warp=False,track=source)
            skia.configure(source,appearance,width=640,height=360,fps=60)
            frames,_=collect(skia,[1900])
            expected=np.frombuffer(raw[0],np.uint8).reshape(360,640,4)
            assert np.max(np.abs(bounds(expected)-bounds(frames[0])))<=1,(semantics,align,position)
