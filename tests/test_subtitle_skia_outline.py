"""Design-space outline accuracy at integer cell-width boundaries."""
from dataclasses import replace
import pytest

from test_subtitle_skia_resolved import application, binary, project, ir_for
from skia_text_fixture import make_cff_font, make_variable_font, make_colr_font
from krok_helper.subtitle_render.domain.models import TimingChar, TimingLine
from krok_helper.subtitle_render.engine.text.skia_width import cell_geometry
from krok_helper.subtitle_render.native.skia_text import SkiaTextMetricsProcess
from krok_helper.subtitle_render.native.skia_fonts import resolve_skia_font
from krok_helper.subtitle_render.engine.render.skia_ir import build_skia_render_ir
from krok_helper.subtitle_render.native.backend import NativeRendererError
from krok_helper.subtitle_render.native.protocol import apply_resolved_font_faces
from krok_helper.subtitle_render.native.skia_backend import SkiaRendererProcess
from test_subtitle_skia_protocol import collect, selection
import numpy as np


def test_ttf_ink_width_does_not_truncate_below_exact_integer(binary,project):
    track,style=project
    resource,options=resolve_skia_font(style)
    with SkiaTextMetricsProcess(binary) as process:
        cells=process.measure({'main':resource},[{'font_ids':['main'],'size':50,'cells':['A','字'],**options}])[0]['cells']
    assert cells[1]['ink'][2]-cells[1]['ink'][0]==pytest.approx(45,abs=1e-4)
    assert cell_geometry('字',cells[1],style)[0]==49
    assert cells[1]['design_left_bearing']==50 and cells[1]['design_right_bearing']==50


def test_cff_outline_uses_the_same_design_precision(binary,tmp_path):
    path=make_cff_font(tmp_path/'canonical.otf')
    with SkiaTextMetricsProcess(binary) as process:
        cell=process.measure({'font':{'path':str(path)}},[{'font_ids':['font'],'size':50,'cells':['A']}])[0]['cells'][0]
    assert cell['outline_available']
    assert cell['ink']==pytest.approx([2.5,-35,27.5,0],abs=1e-4)
    assert (cell['design_advance'],cell['design_left_bearing'],cell['design_right_bearing'])==(600,50,50)


def test_variable_outline_retains_fractional_design_units(binary,tmp_path):
    path=make_variable_font(tmp_path)
    with SkiaTextMetricsProcess(binary) as process:
        cell=process.measure({'font':{'path':str(path),'axes':{'wght':500.5}}},
                             [{'font_ids':['font'],'size':50,'cells':['A']}])[0]['cells'][0]
    # At em ppem, two 26.6 design quanta cover coordinate/variation rounding;
    # this is substantially finer than a quantum at SkFont's canonical 64px.
    assert cell['ink'][2]-cell['ink'][0]==pytest.approx(35.0125,abs=2*50/(1000*64)+1e-5)


def test_color_layer_glyph_is_not_mistaken_for_its_monochrome_base(binary,tmp_path):
    path=make_colr_font(tmp_path/'color.ttf')
    with SkiaTextMetricsProcess(binary) as process:
        cell=process.measure({'font':{'path':str(path)}},[{'font_ids':['font'],'size':50,'cells':['👩']}])[0]['cells'][0]
    assert cell['advance']>0
    assert not cell['outline_available']


@pytest.mark.parametrize('target',['gpu','raster'])
def test_unsupported_color_glyph_does_not_replace_the_live_scene(binary,project,tmp_path,target):
    track,style=project
    path=make_colr_font(tmp_path/'color.ttf')
    source=replace(track,lines=[TimingLine(chars=[TimingChar('👩',200)],end_ms=1000)])
    with SkiaTextMetricsProcess(binary) as metrics:
        ir=build_skia_render_ir(source,style,measure=metrics.measure,
            resolve_font=lambda _:({'path':str(path)},{}),width=640,height=360,fps=60)
    apply_resolved_font_faces(ir)
    with SkiaRendererProcess(binary,selection=selection(target)) as process:
        process.configure(*project,width=640,height=360,fps=60)
        before,_=collect(process,[800])
        process._send({'cmd':'configure','ir':ir})
        with pytest.raises(NativeRendererError,match='矢量轮廓'):
            process._expect_ok(process._read_until_event('configured'))
        after,_=collect(process,[800],generation=2)
        assert np.array_equal(before[0],after[0])


@pytest.mark.parametrize('semantics',['legacy','n3_1074'])
def test_zero_width_cell_does_not_consume_letter_spacing(binary,project,semantics):
    track,style=project
    source=replace(track,lines=[TimingLine(chars=[TimingChar('i',200),TimingChar('A',400)],end_ms=1000)])
    style=replace(style,layout_semantics=semantics,font_size_px=2,latin_font_size_px=2,letter_spacing_px=3)
    ir=ir_for(binary,(source,style))
    a,b=ir['skia_layout']['lines'][0]['cells']
    assert cell_geometry('i',a['metrics'],style)[0]==0
    assert a['wipe_left']==pytest.approx(b['wipe_left'],abs=1e-4)
