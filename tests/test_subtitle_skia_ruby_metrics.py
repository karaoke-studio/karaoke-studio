"""Ruby preparation: explicit font slots, canonical measurement and shared layout."""
import ast
from dataclasses import replace
import json
from pathlib import Path

import pytest

from test_subtitle_skia_font_slots import application, binary, mixed, project
from krok_helper.subtitle_render.domain.models import Style
from krok_helper.subtitle_render.engine.ruby.style_semantics import (
    font_style_for_ruby_cell, ruby_style_for_target_indices,
)
from krok_helper.subtitle_render.engine.ruby.unit_layout import (
    measured_ruby_origins, measured_ruby_width, measured_ruby_left_offset,
)
from krok_helper.subtitle_render.engine.ruby.skia_metrics import measure_readings
from krok_helper.subtitle_render.engine.text.skia_batch import TextMeasurementBatch
from krok_helper.subtitle_render.native.skia_fonts import resolve_skia_font
from krok_helper.subtitle_render.native.skia_text import SkiaTextMetricsProcess


@pytest.mark.parametrize('changes,text,expected', [
    ({}, '字', ('Main',45,400,False,1)),
    ({}, 'A', ('Latin',45,400,False,1.5)),  # Main Latin weight/size are not ruby inheritance.
    ({'italic':True}, '字', ('Main',45,400,True,1)),
    ({'italic':True,'ruby_italic':False}, '字', ('Main',45,400,False,1)),
    ({'italic':True,'ruby_italic':False,'ruby_latin_italic':True}, 'A', ('Latin',45,400,True,1.5)),
    ({'ruby_font_family':'Ruby','ruby_font_weight':500}, '字', ('Ruby',45,500,False,1)),
    ({'ruby_font_family':'Ruby','ruby_font_weight':500}, 'A', ('Latin',45,500,False,1.5)),
    ({'ruby_font_family':'Ruby','font_family_latin':None}, 'A', ('Ruby',45,400,False,1.5)),
    ({'ruby_font_family_latin':'RubyLatin','ruby_latin_font_weight':800,
      'ruby_latin_font_size_px':31,'ruby_latin_font_stretch_pct':250}, 'A', ('RubyLatin',31,800,False,2)),
    ({'ruby_font_size_px':0,'ruby_latin_font_size_px':0,'ruby_font_weight':0,
      'ruby_latin_font_weight':0,'ruby_latin_font_stretch_pct':0}, 'A', ('Latin',1,400,False,.5)),
    ({'ruby_font_family_latin':'RubyLatin'}, 'a\u0301', ('Main',45,400,False,1)),
    ({'ruby_font_family_latin':'RubyLatin'}, '.', ('Main',45,400,False,1)),
])
def test_ruby_slot_inheritance_preserves_gpu_rules(changes,text,expected):
    style=replace(Style(font_family='Main',font_family_latin='Latin',font_weight=400,
                        latin_font_weight=900,latin_font_size_px=99,latin_font_stretch_pct=150),**changes)
    chosen,stretch=font_style_for_ruby_cell(style,text)
    assert (chosen.font_family,chosen.font_size_px,chosen.font_weight,chosen.italic,stretch)==expected
    assert style.font_size_px!=chosen.font_size_px


def test_qt_font_adapter_consumes_the_same_slot(monkeypatch):
    from krok_helper.subtitle_render.engine.ruby import style as adapter
    calls=[]
    class Font:
        def setStretch(self,value): calls.append(('stretch',value))
    monkeypatch.setattr(adapter,'resolve_qt_font_family',lambda family:family)
    monkeypatch.setattr(adapter,'build_weight_font',lambda *a,**k:(calls.append((a,k)) or Font()))
    style=Style(font_family='Main',font_family_latin='Latin',latin_font_weight=900,
                ruby_font_weight=500,ruby_latin_font_size_px=30,ruby_latin_font_stretch_pct=125)
    adapter.build_ruby_font_for_text(style,'A')
    assert calls==[(('Latin',30,500),{'italic':False}),('stretch',125)]


UNITS=(('字',20.,0.,0.,20.),('字',10.,0.,0.,10.),('字',20.,0.,0.,20.))


@pytest.mark.parametrize('mode,interval,expected', [
    ('auto',0,[7.,35.,52.]),('equal_space',0,[7.,35.,52.]),
    ('center',0,[15.,35.,45.]),('center',-4,[19.,35.,41.]),
])
def test_ruby_alignment_uses_existing_integer_rounding(mode,interval,expected):
    style=Style(ruby_stroke_width_px=0,ruby_stroke2_enabled=False,ruby_alignment=mode,ruby_interval_px=interval)
    placed=measured_ruby_origins(UNITS,0,80,style=style,base_text='字')
    assert [c[1] for c in placed]==expected
    assert measured_ruby_width(UNITS,80,style,'字')==80
    assert measured_ruby_left_offset(UNITS,80,style,'字')==0


def test_ruby_overhang_singleton_and_unconstrained_spacing():
    style=Style(ruby_stroke_width_px=0,ruby_stroke2_enabled=False,ruby_alignment='center',ruby_interval_px=3)
    assert measured_ruby_origins([('A',24.,2.,2.,26.)],0,19,style=style)==[('A',-2.,24.,2.)]
    assert measured_ruby_left_offset([('A',24.,2.,2.,26.)],19,style)==-2.5
    assert [c[1] for c in measured_ruby_origins(UNITS,-10.5,None,style=style)]==[-10.5,12.5,25.5]
    assert measured_ruby_width(UNITS,None,style)==56
    assert measured_ruby_origins([],0,80,style=style)==[]


def test_ruby_outline_clearance_and_negative_interval_override():
    style=Style(ruby_stroke_width_px=3,ruby_stroke2_enabled=True,ruby_stroke2_width_px=5,
                ruby_alignment='center',ruby_interval_px=0)
    assert measured_ruby_width(UNITS,None,style)==66
    assert measured_ruby_left_offset(UNITS,20,style)==-23
    assert [c[1] for c in measured_ruby_origins(UNITS,0,20,style=style)]==[-23,5,23]
    assert measured_ruby_width(UNITS,None,replace(style,ruby_interval_px=-2))==46


def test_empty_batch_does_not_discover_fonts_or_start_ipc():
    fail=lambda *a,**k:pytest.fail('empty batch invoked a capability')
    result=measure_readings([],resolve_font=fail,measure=fail)
    assert not result.fonts and not result.runs and not result.readings


@pytest.mark.parametrize('value',[float('nan'),float('inf'),float('-inf')])
def test_shared_batch_rejects_nonfinite_baseline(value):
    batch=TextMeasurementBatch(lambda style:({'path':'font.ttf'},{'scale_x':1}))
    batch.instance(Style())
    result={'font_metrics':{'font_identity':'test','ascent':value,'descent':1,'leading':0},'cells':[]}
    with pytest.raises(ValueError,match='基线无效'): batch.measure(lambda *a:[result])


def test_ruby_preparation_boundaries_do_not_import_qt_measurement():
    root=Path(__file__).resolve().parents[1]/'krok_helper/subtitle_render/engine'
    for name in ('ruby/style_semantics.py','ruby/unit_layout.py','ruby/skia_metrics.py','text/skia_batch.py'):
        tree=ast.parse((root/name).read_text(encoding='utf-8'))
        imports=[n.module or '' for n in ast.walk(tree) if isinstance(n,ast.ImportFrom)]
        assert not any(i.startswith('PyQt6') or i.endswith(('.painter','.ruby.layout','.ruby.style'))
                       or i=='krok_helper.subtitle_render.engine.text' for i in imports),name


def test_ruby_canonical_batch_deduplicates_fonts_and_preserves_graphemes(binary,mixed,monkeypatch,tmp_path):
    from krok_helper.subtitle_render.engine.ruby import style as qt_style, layout as qt_layout
    fail=lambda *a,**k:pytest.fail('Qt ruby font measurement invoked')
    monkeypatch.setattr(qt_style,'build_ruby_font_for_text',fail)
    monkeypatch.setattr(qt_layout,'QFontMetrics',fail)
    _,style=mixed
    style=replace(style,ruby_font_size_px=28,ruby_latin_font_size_px=37,ruby_latin_font_stretch_pct=125,
                  ruby_stroke_width_px=0,ruby_stroke2_enabled=False)
    discoveries,batches=[],[]
    def discover(s):
        discoveries.append(s.font_family)
        return resolve_skia_font(s)
    with SkiaTextMetricsProcess(binary) as process:
        def measure(fonts,runs):
            batches.append(runs)
            return process.measure(fonts,runs)
        result=measure_readings([('A字a\u0301fi ',style),('A字a\u0301fi ',style),('',style)],
                                resolve_font=discover,measure=measure)
    assert len(discoveries)==2 and len(batches)==1
    assert sum(len(r['cells']) for r in batches[0])==6
    first,duplicate,empty=result.readings
    assert first==duplicate and not empty.cells
    assert [u[0] for u in first.unit_layouts]==['A','字','a\u0301','f','i',' ']
    assert first.baseline_metrics==empty.baseline_metrics
    assert first.baseline_metrics['ascent']==pytest.approx(22.4,abs=1e-4)
    assert first.cells[0]['advance']==pytest.approx(37*.8*1.25,abs=1e-4)
    # FreeType's fixed-point scale is rounded before HarfBuzz applies it.
    assert first.cells[1]['advance']==pytest.approx(28,abs=1/4096)
    assert first.run_indices[0]!=first.run_indices[1]==first.run_indices[2]
    assert result.runs[first.run_indices[0]]['size']==37
    assert result.runs[first.run_indices[0]]['scale_x']==1.25
    (tmp_path/'ruby-metrics.json').write_text(json.dumps({
        'readings':[{'cells':r.cells,'run_indices':r.run_indices,'unit_layouts':r.unit_layouts,
                     'baseline_metrics':r.baseline_metrics,
                     'origins':measured_ruby_origins(r.unit_layouts,13,160,style=style,base_text='字')}
                    for r in result.readings],
        'runs':[{k:v for k,v in run.items() if k!='font_ids'} for run in result.runs],
    },sort_keys=True),encoding='utf-8')


def test_ruby_main_baseline_is_independent_of_latin_size_and_inherits_role(binary,mixed):
    from krok_helper.subtitle_render.domain.models import TimingChar,TimingLine,SubtitleStyleScheme
    _,style=mixed
    style=replace(style,custom_style_schemes={'lead':SubtitleStyleScheme(font_size_px=70,ruby_font_size_px=32)},
                  ruby_font_size_px=28)
    target=ruby_style_for_target_indices(style,TimingLine(chars=[TimingChar('字',0,role_label='lead')]),[0])
    with SkiaTextMetricsProcess(binary) as process:
        readings=measure_readings([('AA',replace(style,ruby_latin_font_size_px=90)),
                                  ('字',replace(style,ruby_latin_font_size_px=12)),
                                  ('A',target)],resolve_font=resolve_skia_font,measure=process.measure).readings
    assert readings[0].baseline_metrics==readings[1].baseline_metrics
    assert readings[2].baseline_metrics['ascent']==pytest.approx(32*.8,abs=1e-4)


def test_ruby_stroke_measurement_changes_boxes_without_reshaping(binary,mixed):
    _,style=mixed
    style=replace(style,ruby_font_size_px=28,ruby_stroke_width_px=0,ruby_stroke2_enabled=False)
    with SkiaTextMetricsProcess(binary) as process:
        first=measure_readings([('字',style)],resolve_font=resolve_skia_font,measure=process.measure)
        second=measure_readings([('字',replace(style,ruby_stroke_width_px=5))],
                               resolve_font=resolve_skia_font,measure=process.measure)
    a,b=first.readings[0],second.readings[0]
    assert a.cells==b.cells
    assert b.unit_layouts[0][1]==a.unit_layouts[0][1]+5
    # D2D places an odd five-pixel stroke with a two-pixel inset.
    assert b.unit_layouts[0][2] == a.unit_layouts[0][2] + 2
    assert first.identity!=second.identity
