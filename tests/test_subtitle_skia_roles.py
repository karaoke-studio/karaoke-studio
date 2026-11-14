"""Inline roles share canonical timing/geometry and per-cell solid paints."""
from dataclasses import replace
import json
import sys

import numpy as np
import pytest
from PyQt6.QtGui import QColor

from test_subtitle_skia_font_slots import application, binary, project, mixed
from test_subtitle_skia_resolved import ir_for
from test_subtitle_skia_protocol import collect, selection
from krok_helper.subtitle_render.domain.models import SubtitleStyleScheme, TimingChar, TimingLine
from krok_helper.subtitle_render.domain.paint import KaraokeColors, KaraokeColorState, PaintFill
from krok_helper.subtitle_render.engine.render.skia_ir import build_skia_render_ir
from krok_helper.subtitle_render.engine.style.style_semantics import effective_karaoke_colors, style_for_role_in_layout
from krok_helper.subtitle_render.engine.layout.line.style import style_for_line
from krok_helper.subtitle_render.engine.text.metric_snapshot import CellMetric, LineMetric, TextMetricSnapshot
from krok_helper.subtitle_render.native.backend import NativeRendererError, NativeRendererProcess
from krok_helper.subtitle_render.native.skia_backend import SkiaRendererProcess
from krok_helper.subtitle_render.native.skia_fonts import resolve_skia_font
from krok_helper.subtitle_render.native.skia_text import SkiaTextMetricsProcess


def colors(before,after):
    return KaraokeColors(before=KaraokeColorState(text=PaintFill(color=before)),
                         after=KaraokeColorState(text=PaintFill(color=after)))


@pytest.fixture
def roles(mixed):
    track,style=mixed
    style=replace(style,custom_style_schemes={
        'lead':SubtitleStyleScheme(font_family=style.font_family_latin,font_family_latin=style.font_family,
            font_size_px=70,latin_font_size_px=35,latin_font_stretch_pct=125,
            letter_spacing_px=99,space_width_percent=100,allow_biting=True,
            karaoke_colors=colors('#8033AA11','#AACC1177')),
        'back':SubtitleStyleScheme(karaoke_colors=colors('#FF1122EE','#FFEEAA22'))})
    units=[('字',None),('A','lead'),('字','lead'),('한','back'),('A','missing'),('字','lead')]
    line=TimingLine(chars=[TimingChar(text,200+i*200,role_label=role) for i,(text,role) in enumerate(units)],end_ms=1800)
    return replace(track,lines=[line]),style


def test_role_layout_inheritance_preserves_active_spacing(roles):
    _,style=roles
    result=style_for_role_in_layout(style,'lead')
    assert result.font_size_px==70 and result.latin_font_size_px==35
    for key in ('letter_spacing_px','space_width_percent','allow_biting','ruby_gap_px'):
        assert getattr(result,key)==getattr(style,key)
    assert style_for_role_in_layout(style,'missing')==style


def test_old_sidecar_without_role_capability_is_rejected(monkeypatch,tmp_path):
    executable=tmp_path/'old';executable.touch()
    ready={'ok':True,'backend':'skia','schema':3,'selection_version':1,'resolved_layout_schema':2,
           'profiles':['p05_resolved_horizontal','p05_single_line_placement'],'execution_targets':['raster']}
    monkeypatch.setattr(NativeRendererProcess,'start',lambda self:ready)
    monkeypatch.setattr(SkiaRendererProcess,'abort',lambda *a,**k:None)
    sent=[]
    monkeypatch.setattr(SkiaRendererProcess,'_send',lambda self,payload:sent.append(payload))
    with pytest.raises(NativeRendererError,match='行内角色'):SkiaRendererProcess(executable).start()
    assert not sent


def test_role_instances_are_deduplicated_in_one_batch(binary,roles,tmp_path):
    track,style=roles
    batches=[]
    with SkiaTextMetricsProcess(binary) as process:
        def measure(fonts,runs):
            batches.append(runs)
            return process.measure(fonts,runs)
        ir=build_skia_render_ir(track,style,measure=measure,resolve_font=resolve_skia_font,width=640,height=360,fps=60)
    assert len(batches)==1 and len(batches[0])==4
    assert len(ir['skia_layout']['fonts'])==2
    cells=ir['skia_layout']['lines'][0]['cells']
    assert cells[2]['run_index']==cells[5]['run_index']
    assert cells[0]['run_index']==cells[3]['run_index']
    assert cells[1]['metrics']['advance']==pytest.approx(26.25,abs=1e-4)
    assert cells[2]['metrics']['advance']==pytest.approx(70,abs=1e-4)
    (tmp_path/'roles-layout.json').write_text(json.dumps({'cells':cells,
        'runs':[{k:v for k,v in r.items() if k!='font_ids'} for r in ir['skia_layout']['runs']],
        'intervals':ir['track']['lines'][0]['resolved_intervals']},sort_keys=True),encoding='utf-8')


def test_same_text_different_roles_do_not_collide_in_interval_snapshot(binary,roles):
    track,style=roles
    units=[TimingChar('A',200,source_span_start_ms=200,source_span_end_ms=1000,source_span_index=0,source_span_count=2),
           TimingChar('字',200,source_span_start_ms=200,source_span_end_ms=1000,source_span_index=1,source_span_count=2)]
    plain=TimingLine(chars=units,end_ms=1000)
    lead=replace(plain,chars=[replace(c,role_label='lead') for c in units])
    def capture(line):
        ir=ir_for(binary,(replace(track,lines=[line]),style))
        cells=ir['skia_layout']['lines'][0]['cells']
        from krok_helper.subtitle_render.engine.text.skia_width import cell_geometry
        from krok_helper.subtitle_render.engine.text.font_slots import font_style_for_cell
        widths=[cell_geometry(c.text,m['metrics'],font_style_for_cell(style_for_role_in_layout(style,c.role_label),c.text)[0])[0]
                for c,m in zip(line.chars,cells)]
        return LineMetric.capture(line,style,[CellMetric.from_payload(c.text,m['metrics']) for c,m in zip(line.chars,cells)],layout_widths=widths)
    a,b=capture(plain),capture(lead)
    snapshot=TextMetricSnapshot((a,b))
    assert snapshot.resolve_intervals(plain,style)!=snapshot.resolve_intervals(lead,style)
    # Exercise the real plan as well: two identical-text lines use different role widths.
    ir=ir_for(binary,(replace(track,lines=[plain,replace(lead,chars=[replace(c,start_ms=c.start_ms+2000,
        source_span_start_ms=2200,source_span_end_ms=3000) for c in lead.chars],end_ms=3000)]),style))
    assert len(ir['skia_layout']['lines'])==2


@pytest.mark.parametrize('semantics',['legacy','n3_1074'])
def test_role_size_and_first_character_do_not_move_line_baseline(binary,roles,semantics):
    track,style=roles
    style=replace(style,layout_semantics=semantics,singer_style_overrides={1:SubtitleStyleScheme(font_size_px=65)})
    line=replace(track.lines[0],singer_id=1)
    decorated=ir_for(binary,(replace(track,lines=[line]),style))
    plain=ir_for(binary,(replace(track,lines=[replace(line,chars=[replace(c,role_label=None) for c in line.chars])]),style))
    for a,b in zip(decorated['skia_layout']['lines'][0]['cells'],plain['skia_layout']['lines'][0]['cells']):
        assert a['baseline_y']==b['baseline_y']


def test_n3_role_can_clear_the_global_latin_font_override(binary,roles):
    track,style=roles
    lead=replace(style.custom_style_schemes['lead'],n3_font_inheritance=True,
                 font_family_latin=None,latin_font_size_px=None)
    style=replace(style,custom_style_schemes={**style.custom_style_schemes,'lead':lead})
    ir=ir_for(binary,(track,style))
    cells=ir['skia_layout']['lines'][0]['cells']
    runs=ir['skia_layout']['runs']
    a,b=(runs[c['run_index']] for c in cells[1:3])
    assert a['font_ids']==b['font_ids'] and a['size']==b['size']==70
    assert cells[1]['metrics']['advance']==pytest.approx(70*.8*1.25,abs=1e-4)


def test_role_synthetic_instances_are_resolved_before_drawing(binary,roles):
    track,style=roles
    lead=replace(style.custom_style_schemes['lead'],font_weight=700,italic=True,latin_font_weight=700,latin_italic=True)
    ir=ir_for(binary,(track,replace(style,custom_style_schemes={**style.custom_style_schemes,'lead':lead})))
    cells=ir['skia_layout']['lines'][0]['cells']
    for cell in cells[1:3]:
        run=ir['skia_layout']['runs'][cell['run_index']]
        assert run['synthetic_bold'] and run['synthetic_italic']


@pytest.mark.parametrize('semantics',['legacy','n3_1074'])
def test_singer_and_role_inheritance_share_gpu_raster_geometry(binary,roles,semantics):
    track,style=roles
    source=replace(track,lines=[replace(track.lines[0],singer_id=1)])
    style=replace(style,layout_semantics=semantics,singer_style_overrides={1:SubtitleStyleScheme(
        font_size_px=60,karaoke_colors=colors('#80446688','#BBDD5522'))})
    cells=ir_for(binary,(source,style))['skia_layout']['lines'][0]['cells']
    output=[]
    for target in ('gpu','raster'):
        with SkiaRendererProcess(binary,selection=selection(target)) as process:
            process.configure(source,style,width=640,height=360,fps=60,dpr=1.5)
            frames,_=collect(process,[800,1900,100,800])
            assert np.array_equal(frames[0],frames[3])
            for char,cell in zip(source.lines[0].chars,cells):
                ink=cell['metrics']['ink']
                x=int((cell['origin_x']+(ink[0]+ink[2])/2)*1.5)
                y=int((cell['baseline_y']+(ink[1]+ink[3])/2)*1.5)
                appearance=style_for_role_in_layout(style_for_line(style,source.lines[0]),char.role_label)
                expected=QColor(effective_karaoke_colors(appearance).after.text.color).getRgb()
                assert np.max(np.abs(frames[1][y,x].astype(int)-expected))<=2
            pixels=np.stack(frames).astype(float);pixels[...,:3]*=pixels[...,3:]/255
            output.append(pixels)
    delta=np.abs(output[0]-output[1])
    assert delta.mean()<.15 and np.mean(np.max(delta,axis=-1)>16)<.003


@pytest.mark.parametrize('semantics',['legacy','n3_1074'])
@pytest.mark.parametrize('target',['gpu','raster'])
def test_role_paints_and_seek_are_per_cell(binary,roles,target,semantics):
    track,style=roles
    style=replace(style,layout_semantics=semantics)
    ir=ir_for(binary,(track,style))
    cells=ir['skia_layout']['lines'][0]['cells']
    with SkiaRendererProcess(binary,selection=selection(target)) as process:
        process.configure(track,style,width=640,height=360,fps=60)
        frames,_=collect(process,[100,800,1900,800])
        assert np.array_equal(frames[1],frames[3])
    for char,cell in zip(track.lines[0].chars,cells):
        ink=cell['metrics']['ink']
        x=int(cell['origin_x']+(ink[0]+ink[2])/2)
        y=int(cell['baseline_y']+(ink[1]+ink[3])/2)
        paint=effective_karaoke_colors(style_for_role_in_layout(style,char.role_label))
        for frame,state in ((frames[0],paint.before),(frames[2],paint.after)):
            expected=QColor(state.text.color).getRgb()
            assert np.max(np.abs(frame[y,x].astype(int)-expected))<=2,(char.role_label,frame[y,x],expected)


@pytest.mark.parametrize('mutation',['stroke','gradient','decoration'])
def test_unsupported_role_paint_rejected_without_replacing_scene(binary,roles,mutation):
    with SkiaRendererProcess(binary,selection=selection('raster')) as process:
        process.configure(*roles,width=640,height=360,fps=60)
        before,_=collect(process,[800])
        ir=ir_for(binary,roles)
        scheme=ir['style']['custom_style_schemes']['lead']
        if mutation=='stroke': scheme['stroke_width_px']=5
        elif mutation=='decoration': scheme['decoration_kind']='glow'
        else: scheme['karaoke_colors']['before']['text']['mode']='gradient_vertical'
        process._send({'cmd':'configure','ir':ir})
        with pytest.raises(NativeRendererError):process._expect_ok(process._read_until_event('configured'))
        after,_=collect(process,[800],generation=2)
        assert np.array_equal(before[0],after[0])


@pytest.mark.skipif(sys.platform!='win32',reason='Direct2D GPU comparison')
@pytest.mark.parametrize('semantics',['legacy','n3_1074'])
def test_role_geometry_matches_d2d_gpu(binary,roles,semantics):
    from test_subtitle_render_gpu_backend import _render_g1_frames
    track,style=roles
    # Give the D2D compatibility adapter an explicit physical app-font hint:
    # color-only schemes otherwise clear that old adapter's font_hint slot.
    back=replace(style.custom_style_schemes['back'],font_family=style.font_family,
                 font_family_latin=style.font_family_latin,font_weight=style.font_weight)
    style=replace(style,layout_semantics=semantics,custom_style_schemes={**style.custom_style_schemes,'back':back})
    with NativeRendererProcess() as d2d:
        _,raw=_render_g1_frames(d2d,style,(1900,),force_warp=False,track=track)
    with SkiaRendererProcess(binary,selection=selection('gpu')) as skia:
        skia.configure(track,style,width=640,height=360,fps=60)
        frames,_=collect(skia,[1900])
    expected=np.frombuffer(raw[0],np.uint8).reshape(360,640,4)
    def bounds(p):
        y,x=np.where(p[...,3]>127)
        return np.array([x.min(),y.min(),x.max(),y.max()])
    assert np.max(np.abs(bounds(expected)-bounds(frames[0])))<=1
