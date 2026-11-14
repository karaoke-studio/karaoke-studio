"""Independent main/Latin font instances share one canonical baseline and batch."""
from dataclasses import replace
import json
import sys
import numpy as np
import pytest
from PyQt6.QtGui import QFontDatabase

from test_subtitle_skia_resolved import application, binary, project, ir_for
from test_subtitle_skia_protocol import collect, selection
from skia_text_fixture import make_font
from krok_helper.subtitle_render.domain.models import Style, TimingChar, TimingLine
from krok_helper.subtitle_render.engine.text.font_slots import font_style_for_cell
from krok_helper.subtitle_render.engine.text.font_weight import clear_font_weight_cache
from krok_helper.subtitle_render.engine.render.skia_ir import build_skia_render_ir
from krok_helper.subtitle_render.native.skia_fonts import resolve_skia_font
from krok_helper.subtitle_render.native.skia_text import SkiaTextMetricsProcess
from krok_helper.subtitle_render.native.skia_backend import SkiaRendererProcess
from krok_helper.subtitle_render.native.backend import NativeRendererError, NativeRendererProcess


@pytest.fixture
def mixed(project, tmp_path):
    path = make_font(tmp_path / 'latin.ttf', family='LinK Latin 字体', a_width=800,
                     ps_name='LinK-P05-Latin', ascent=900, descent=400)
    registered = QFontDatabase.addApplicationFont(str(path))
    assert registered >= 0
    clear_font_weight_cache()
    try:
        track, style = project
        family = QFontDatabase.applicationFontFamilies(registered)[0]
        yield track, replace(style, font_family_latin=family, latin_font_size_px=80,
                             latin_font_stretch_pct=150)
    finally:
        QFontDatabase.removeApplicationFont(registered)
        clear_font_weight_cache()


@pytest.mark.parametrize('text,latin', [('A',True),('fi',True),('á',True),('a\u0301',False),
                                     ('字A',False),('한',False),(' ',False),('.',False)])
def test_authored_cell_classification_keeps_existing_semantics(text, latin):
    style = Style(font_family='Main',font_family_latin='Latin',font_size_px=50,
                  latin_font_size_px=80,latin_font_weight=700,latin_italic=True,latin_font_stretch_pct=150)
    selected, stretch = font_style_for_cell(style, text)
    assert selected.font_family == ('Latin' if latin else 'Main')
    assert selected.font_size_px == (80 if latin else 50)
    assert selected.italic is latin
    assert stretch == (1.5 if latin else 1)
    assert style.font_family == 'Main' and style.font_size_px == 50


def test_latin_inherits_family_but_can_override_size_weight_and_italic():
    style = Style(font_family='Main',font_family_latin=None,font_size_px=50,
                  latin_font_size_px=80,latin_font_weight=700,latin_italic=True)
    selected, _ = font_style_for_cell(style, 'A')
    assert (selected.font_family,selected.font_size_px,selected.font_weight,selected.italic) == ('Main',80,700,True)


def test_old_layout_schema_is_rejected_before_backend_selection(monkeypatch,tmp_path):
    executable=tmp_path/'old-renderer';executable.touch()
    ready={'ok':True,'backend':'skia','schema':3,'selection_version':1,
           'profiles':['p05_resolved_horizontal'],'execution_targets':['raster'],'resolved_layout_schema':1}
    monkeypatch.setattr(NativeRendererProcess,'start',lambda self:ready)
    monkeypatch.setattr(SkiaRendererProcess,'abort',lambda *a,**k:None)
    sent=[]
    monkeypatch.setattr(SkiaRendererProcess,'_send',lambda self,payload:sent.append(payload))
    with pytest.raises(NativeRendererError,match='字体实例表'):
        SkiaRendererProcess(executable,selection=selection('raster')).start()
    assert not sent


def test_one_batch_deduplicates_instances_and_repeated_cells(binary, mixed):
    track, style = mixed
    line = TimingLine(chars=[TimingChar('A',200),TimingChar('A',400),TimingChar('字',600)],end_ms=1000)
    track = replace(track, lines=[line] * 12)
    discoveries, batches = [], []
    def discover(appearance):
        discoveries.append(appearance.font_family)
        return resolve_skia_font(appearance)
    with SkiaTextMetricsProcess(binary) as process:
        def measure(fonts,runs):
            batches.append(runs)
            return process.measure(fonts,runs)
        ir = build_skia_render_ir(track,style,resolve_font=discover,measure=measure,width=640,height=360,fps=60)
    assert len(discoveries) == 2 and len(batches) == 1
    assert len(batches[0]) == 2 and sum(len(r['cells']) for r in batches[0]) == 2
    assert len(ir['skia_layout']['runs']) == 2
    cells = ir['skia_layout']['lines'][0]['cells']
    assert cells[0]['metrics']['advance'] == pytest.approx(96,abs=1e-4)
    assert cells[2]['metrics']['advance'] == pytest.approx(50,abs=1e-4)
    assert cells[0]['run_index'] == cells[1]['run_index'] != cells[2]['run_index']


def test_latin_size_and_content_do_not_change_main_font_baseline(binary,mixed):
    track,style=mixed
    baselines=[]
    for texts,size in ((['A','fi'],80),(['字','A'],100),(['한'],40)):
        source=replace(track,lines=[TimingLine(chars=[TimingChar(t,200+i*200) for i,t in enumerate(texts)],end_ms=1000)])
        ir=ir_for(binary,(source,replace(style,latin_font_size_px=size)))
        cells=ir['skia_layout']['lines'][0]['cells']
        baselines.extend(c['baseline_y'] for c in cells)
    assert baselines == pytest.approx([360-style.line_y_margin_px-10]*len(baselines),abs=1e-4)


@pytest.mark.parametrize('target',['gpu','raster'])
def test_mixed_fonts_draw_with_same_geometry_and_seek(binary,mixed,target,tmp_path):
    with SkiaRendererProcess(binary,selection=selection(target)) as process:
        process.configure(*mixed,width=640,height=360,fps=60)
        frames,_=collect(process,[800,1900,800])
    assert np.array_equal(frames[0],frames[2]) and frames[0][...,3].max()==255
    ir=ir_for(binary,mixed)
    if target=='gpu':
        (tmp_path/'font-slots-layout.json').write_text(json.dumps({
            'cells':ir['skia_layout']['lines'][0]['cells'],
            'runs':[{k:v for k,v in r.items() if k!='font_ids'} for r in ir['skia_layout']['runs']],
            'intervals':ir['track']['lines'][0]['resolved_intervals']},sort_keys=True),encoding='utf-8')


@pytest.mark.parametrize('value',[-1,.5,999,'0',None])
def test_invalid_font_instance_reference_rejected_without_replacing_scene(binary,mixed,value):
    with SkiaRendererProcess(binary,selection=selection('raster')) as process:
        process.configure(*mixed,width=640,height=360,fps=60)
        before,_=collect(process,[800])
        ir=ir_for(binary,mixed)
        ir['skia_layout']['lines'][0]['cells'][0]['run_index']=value
        process._send({'cmd':'configure','ir':ir})
        with pytest.raises(NativeRendererError): process._expect_ok(process._read_until_event('configured'))
        after,_=collect(process,[800],generation=2)
    assert np.array_equal(before[0],after[0])


def test_synthetic_instances_are_explicit_and_do_not_move_baseline(binary,mixed):
    track,style=mixed
    ir=ir_for(binary,(track,replace(style,latin_font_weight=700,latin_italic=True)))
    runs=ir['skia_layout']['runs']
    latin=runs[ir['skia_layout']['lines'][0]['cells'][0]['run_index']]
    assert latin['synthetic_bold'] and latin['synthetic_italic'] and latin['scale_x']==1.5
    assert ir['skia_layout']['lines'][0]['cells'][0]['baseline_y']==pytest.approx(290,abs=1e-4)


def test_main_font_content_invalidates_an_all_latin_line(binary,mixed,tmp_path):
    import hashlib
    track,style=mixed
    track=replace(track,lines=[TimingLine(chars=[TimingChar('A',200)],end_ms=1000)])
    outputs=[]
    for descent in (200,300):
        path=make_font(tmp_path/f'main-{descent}.ttf',descent=descent)
        def discover(appearance):
            if appearance.font_family!=style.font_family: return resolve_skia_font(appearance)
            return {'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()},{}
        with SkiaTextMetricsProcess(binary) as process:
            outputs.append(build_skia_render_ir(track,style,resolve_font=discover,measure=process.measure,
                                                width=640,height=360,fps=60))
    old,new=[ir['skia_layout'] for ir in outputs]
    assert old['metrics_identity']!=new['metrics_identity']
    a,b=old['lines'][0]['cells'][0],new['lines'][0]['cells'][0]
    assert a['metrics']==b['metrics']
    assert a['baseline_y']-b['baseline_y']==pytest.approx(5,abs=1e-4)


def test_mixed_font_gpu_and_raster_pixels_share_geometry(binary,mixed):
    output=[]
    for target in ('gpu','raster'):
        with SkiaRendererProcess(binary,selection=selection(target)) as process:
            process.configure(*mixed,width=640,height=360,fps=60)
            frames,_=collect(process,[800,1900])
            pixels=np.stack(frames).astype(float)
            pixels[...,:3]*=pixels[...,3:]/255
            output.append(pixels)
    delta=np.abs(output[0]-output[1])
    assert delta.mean()<.15 and np.mean(np.max(delta,axis=-1)>16)<.003


@pytest.mark.skipif(sys.platform!='win32',reason='Direct2D GPU comparison')
def test_mixed_font_baseline_matches_d2d_gpu(binary,mixed):
    from test_subtitle_render_gpu_backend import _render_g1_frames
    track,style=mixed
    source=replace(track,lines=[TimingLine(chars=[TimingChar(t,200+i*200) for i,t in enumerate(['字','A','한'])],end_ms=1800)])
    with NativeRendererProcess() as process:
        _,raw=_render_g1_frames(process,style,(1900,),force_warp=False,track=source)
    with SkiaRendererProcess(binary,selection=selection('gpu')) as process:
        process.configure(source,style,width=640,height=360,fps=60)
        frames,_=collect(process,[1900])
    expected=np.frombuffer(raw[0],np.uint8).reshape(360,640,4)
    def bounds(p):
        y,x=np.where(p[...,3]>127)
        return np.array([x.min(),y.min(),x.max(),y.max()])
    assert np.max(np.abs(bounds(expected)-bounds(frames[0])))<=1
