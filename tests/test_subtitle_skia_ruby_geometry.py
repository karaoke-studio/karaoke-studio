"""Canonical ruby target clocks, interference and local geometry preparation."""
from dataclasses import replace
import json
import math
import sys
from types import SimpleNamespace

import pytest
import numpy as np

from test_subtitle_skia_font_slots import application, binary, project, mixed
from krok_helper.subtitle_render.domain.models import TimingChar, TimingLine, TimingTrack, Style, SubtitleStyleScheme
from krok_helper.subtitle_render.domain.timing import RubyAnnotation
from krok_helper.subtitle_render.engine.layout.layout_context import layout_pass
from krok_helper.subtitle_render.engine.layout.line.style import style_for_line
from krok_helper.subtitle_render.engine.text.cell_positions import char_left_positions
from krok_helper.subtitle_render.engine.ruby.selection import active_rubies_for_line
from krok_helper.subtitle_render.engine.ruby.interference import RubySpacingSpan, resolve_ruby_spacing
from krok_helper.subtitle_render.engine.ruby.skia_geometry import prepare_ruby_geometry, ruby_line_position
from krok_helper.subtitle_render.engine.text.skia_metrics import measure_track
from krok_helper.subtitle_render.native.skia_fonts import resolve_skia_font
from krok_helper.subtitle_render.native.skia_text import SkiaTextMetricsProcess
from krok_helper.subtitle_render.native.backend import NativeRendererProcess


def prepare(binary, track, style, *, resolve_font=resolve_skia_font, on_measure=None):
    styles=[style_for_line(style,line) for line in track.lines]
    with SkiaTextMetricsProcess(binary) as process:
        def measure(fonts,runs):
            if on_measure is not None:on_measure(fonts,runs)
            return process.measure(fonts,runs)
        main=measure_track(track,style,styles,resolve_font=resolve_font,measure=measure)
        ruby=prepare_ruby_geometry(track,style,styles,main,resolve_font=resolve_font,measure=measure)
    return main,ruby


@pytest.fixture
def annotated(mixed):
    _,style=mixed
    style=replace(style,latin_font_size_px=50,latin_font_stretch_pct=100,
                  ruby_font_size_px=28,ruby_latin_font_size_px=37,ruby_latin_font_stretch_pct=125,
                  ruby_stroke_width_px=0,ruby_stroke2_enabled=False,ruby_interval_px=4)
    line=TimingLine(chars=[TimingChar(t,200+i*300) for i,t in enumerate(['字','A','字','字'])],end_ms=1500)
    rubies=[RubyAnnotation('字','AAA字',pos_start_ms=200,pos_end_ms=500,
                          target_line_index=0,target_char_start=0,target_char_end=1),
            RubyAnnotation('字','AA',pos_start_ms=800,pos_end_ms=1100,
                           target_line_index=0,target_char_start=2,target_char_end=3),
            RubyAnnotation('字','字A',pos_start_ms=1100,pos_end_ms=1500,
                           target_line_index=0,target_char_start=3,target_char_end=4)]
    return TimingTrack(lines=[line],rubies=rubies),style


def test_canonical_selection_never_recomputes_intervals_or_reuses_default_cache(monkeypatch):
    from krok_helper.subtitle_render.engine.ruby import selection
    line=TimingLine(chars=[TimingChar('A',0),TimingChar('字',0)],end_ms=1000)
    ruby=RubyAnnotation('字','A',pos_start_ms=500,pos_end_ms=600,target_char_start=1,target_char_end=2)
    monkeypatch.setattr(selection,'compute_char_intervals',lambda *_:[(0,900),(900,1000)])
    with layout_pass():
        assert active_rubies_for_line([ruby],line)==[]
        rubies=[ruby]
        assert active_rubies_for_line(rubies,line,intervals=[(0,900),(900,1000)])==[]
        assert active_rubies_for_line(rubies,line,intervals=[(0,100),(100,1000)])==[ruby]
        monkeypatch.setattr(selection,'compute_char_intervals',lambda *_:pytest.fail('implicit clock recomputed'))
        assert active_rubies_for_line(rubies,line,intervals=[(0,100),(100,1000)])==[ruby]


@pytest.mark.parametrize('semantics',['legacy','n3_1074'])
def test_spacing_pushes_later_targets_and_preserves_overhang(semantics):
    style=Style(layout_semantics=semantics,letter_spacing_px=0,ruby_interval_px=2)
    spans=[RubySpacingSpan(0,0,False,0),RubySpacingSpan(1,1,False,1)]
    # Each 60px reading is centered over a 20px main cell.
    bounds=lambda span,x,width:(x+(width-60)/2,x+(width+60)/2)
    assert resolve_ruby_spacing([20,20],style,spans,draw_bounds=bounds)==([0,42],20,20)
    assert char_left_positions([20,20],0,False,char_gaps=[0,42])==[0,62]


def test_spacing_lower_group_resets_interference_and_negative_interval_is_honored():
    style=Style(letter_spacing_px=0,ruby_interval_px=-3)
    spans=[RubySpacingSpan(0,0,False,0),RubySpacingSpan(1,1,True,1)]
    bounds=lambda span,x,width:(x-20,x+40)
    assert resolve_ruby_spacing([20,20],style,spans,draw_bounds=bounds)==([0,0],20,20)
    spans[1]=replace(spans[1],lower=False)
    assert resolve_ruby_spacing([20,20],style,spans,draw_bounds=bounds)==([0,37],20,20)


@pytest.mark.parametrize('value',[float('nan'),float('inf'),float('-inf')])
def test_interference_rejects_nonfinite_reading_bounds(value):
    with pytest.raises(ValueError,match='非有限'):
        resolve_ruby_spacing([20],Style(),[RubySpacingSpan(0,0,False,0)],
                             draw_bounds=lambda *a:(value,50))


def test_empty_spacing_and_target_bounds_validation():
    fail=lambda *a:pytest.fail('no spans should require no measuring')
    assert resolve_ruby_spacing([],Style(),[],draw_bounds=fail)==([],0,0)
    assert resolve_ruby_spacing([10,20],Style(),[],draw_bounds=fail)==([0,0],0,0)
    with pytest.raises(ValueError,match='超出'):
        resolve_ruby_spacing([20],Style(),[RubySpacingSpan(0,1,False,0)],draw_bounds=fail)


@pytest.mark.parametrize('semantics',['legacy','n3_1074'])
def test_canonical_geometry_maps_repeated_targets_and_inserts_main_gaps(binary,annotated,semantics,monkeypatch,tmp_path):
    from krok_helper.subtitle_render.engine.ruby import layout,selection
    from krok_helper.subtitle_render.engine.text import metrics
    fail=lambda *a,**k:pytest.fail('Qt ruby preparation invoked')
    monkeypatch.setattr(layout,'QFontMetrics',fail)
    monkeypatch.setattr(metrics,'QFontMetrics',fail)
    monkeypatch.setattr(selection,'compute_char_intervals',fail)
    track,style=annotated
    style=replace(style,layout_semantics=semantics)
    calls=[]
    main,result=prepare(binary,track,style,on_measure=lambda *a:calls.append(a))
    assert len(calls)==2  # One batch for main text, one for all readings.
    line=result.lines[0]
    assert [r.target.indices for r in line.rubies]==[(0,),(2,),(3,)]
    assert line.gaps[2]>0 and line.gaps[3]>0 and line.overflow[0]>0
    assert line.intervals==tuple(main.snapshot.resolve_intervals(track.lines[0],style))
    assert line.main.layout_width>sum(main.lines[0].widths)
    for r in line.rubies:
        first,last=r.target.indices[0],r.target.indices[-1]
        lefts=char_left_positions(main.lines[0].widths,0,False,style.letter_spacing_px,char_gaps=line.gaps,
                                 n3_no_backtracking=semantics=='n3_1074')
        assert r.span==(lefts[first],lefts[last]+main.lines[0].widths[last])
        assert all(c['baseline_y']==r.baseline_offset for c in r.cells)
        assert r.baseline_offset<0 and all(c['wipe_left']<=c['wipe_right'] for c in r.cells)
    # Latin ruby uses a deeper (37px, 900/400 font) box than Japanese (28px, 800/200).
    assert line.rubies[0].baseline_offset==pytest.approx(-40-37*400/1300,abs=1e-4)
    if semantics=='legacy':
        (tmp_path/'ruby-geometry.json').write_text(json.dumps({
            'gaps':line.gaps,'overflow':line.overflow,'intervals':line.intervals,
            'anchor':line.anchor,
            'main':line.main.cells,'width':line.main.layout_width,'ink':line.main.ink,
            'rubies':[{'annotation_index':r.target.annotation_index,'indices':r.target.indices,
                       'span':r.span,'baseline':r.baseline_offset,'cells':r.cells,
                       'clock':[r.target.effective.pos_start_ms,r.target.effective.pos_end_ms]}
                      for r in line.rubies],
        },sort_keys=True),encoding='utf-8')


def test_explicit_line_ownership_does_not_leak_into_equal_overlapping_lines(binary,annotated):
    track,style=annotated
    track=replace(track,lines=[track.lines[0],replace(track.lines[0])])
    _,result=prepare(binary,track,style)
    assert len(result.lines[0].rubies)==3 and not result.lines[1].rubies


def test_repeated_text_without_explicit_target_uses_canonical_time(binary,annotated):
    track,style=annotated
    ruby=RubyAnnotation('字','A',pos_start_ms=850,pos_end_ms=950)
    _,result=prepare(binary,replace(track,rubies=[ruby]),style)
    r=result.lines[0].rubies[0]
    assert r.target.indices==(2,)
    assert (r.target.effective.pos_start_ms,r.target.effective.pos_end_ms)==(800,950)


def test_shared_source_span_intervals_are_preserved_after_interference(binary,annotated):
    track,style=annotated
    line=TimingLine(chars=[TimingChar(t,200,source_span_start_ms=200,source_span_end_ms=1400,
                            source_span_index=i,source_span_count=3) for i,t in enumerate(['A','i','字'])],end_ms=1400)
    ruby=RubyAnnotation('字','AAAA',pos_start_ms=200,pos_end_ms=1400,target_char_start=2,target_char_end=3)
    main,result=prepare(binary,replace(track,lines=[line],rubies=[ruby]),style)
    expected=tuple(main.snapshot.resolve_intervals(line,style))
    assert result.lines[0].intervals==expected
    assert expected[0][1]-expected[0][0]>expected[1][1]-expected[1][0]
    assert result.lines[0].rubies[0].target.effective.pos_start_ms==expected[2][0]


def test_partial_target_inside_authored_cell_uses_existing_fraction_rule(binary,annotated):
    track,style=annotated
    line=TimingLine(chars=[TimingChar('fi',200),TimingChar('字',800)],end_ms=1400)
    ruby=RubyAnnotation('i','A',pos_start_ms=200,pos_end_ms=800)
    main,result=prepare(binary,replace(track,lines=[line],rubies=[ruby]),style)
    assert result.lines[0].rubies[0].target.indices==(0,)
    width=main.lines[0].widths[0]
    assert result.lines[0].rubies[0].span==(round(width/2),width)


def test_ruby_first_target_role_and_affects_anchor_fallback(binary,annotated):
    track,style=annotated
    role=SubtitleStyleScheme(font_size_px=80,ruby_font_size_px=24,affects_ruby_anchor=False)
    style=replace(style,affects_ruby_anchor=False,custom_style_schemes={'lead':role},
                  ruby_font_family_latin=None,font_family_latin=None,ruby_latin_font_size_px=None)
    line=replace(track.lines[0],chars=[replace(c,role_label='lead' if i==2 else None)
                                     for i,c in enumerate(track.lines[0].chars)])
    _,result=prepare(binary,replace(track,lines=[line],rubies=[track.rubies[1]]),style)
    r=result.lines[0].rubies[0]
    assert r.target.style.ruby_font_size_px==24
    assert r.baseline_offset==pytest.approx(-80*.8-24*.2,abs=1e-4)


def test_timing_changes_do_not_move_geometry_but_invalidate_preparation_identity(binary,annotated):
    track,style=annotated
    _,a=prepare(binary,track,style)
    altered=replace(track.rubies[0],reading_part_ms=[100,150],reading_parts=['A','','AA字'])
    _,b=prepare(binary,replace(track,rubies=[altered,*track.rubies[1:]]),style)
    assert a.lines[0].gaps==b.lines[0].gaps
    assert a.lines[0].rubies[0].cells==b.lines[0].rubies[0].cells
    assert a.identity!=b.identity


def test_blank_and_unmatched_lines_have_no_ruby_measurement_batch(binary,annotated):
    track,style=annotated
    track=replace(track,lines=[TimingLine(chars=[],is_blank=True),*track.lines],rubies=[])
    batches=[]
    _,result=prepare(binary,track,style,on_measure=lambda *a:batches.append(a))
    assert len(batches)==1
    assert result.lines[0].gaps==() and result.lines[0].main.ink is None
    assert not result.measured.runs
    assert all(not l.rubies for l in result.lines)


def test_main_role_anchor_can_come_from_outside_ruby_target(binary,annotated):
    track,style=annotated
    role=SubtitleStyleScheme(font_size_px=100,affects_ruby_anchor=True)
    style=replace(style,custom_style_schemes={'lead':role},font_family_latin=None,
                  ruby_font_family_latin=None,ruby_latin_font_size_px=None)
    line=replace(track.lines[0],chars=[replace(c,role_label='lead' if i==3 else None)
                                     for i,c in enumerate(track.lines[0].chars)])
    _,result=prepare(binary,replace(track,lines=[line],rubies=[track.rubies[0]]),style)
    assert result.lines[0].rubies[0].baseline_offset==pytest.approx(-80-28*.2,abs=1e-4)


def test_invalid_main_outline_and_reversed_clock_are_gated(binary,annotated):
    track,style=annotated
    with pytest.raises(ValueError,match='带描边'):
        prepare(binary,track,replace(style,stroke_width_px=5))
    with pytest.raises(ValueError,match='正向横排上注音'):
        prepare(binary,replace(track,lines=[replace(track.lines[0],wipe_reverse=True)]),style)


@pytest.mark.parametrize('semantics',['legacy','n3_1074'])
def test_negative_spacing_and_zero_width_cells_share_main_position_policy(binary,annotated,semantics):
    from krok_helper.subtitle_render.engine.layout.line.skia_geometry import local_line_geometry
    from types import SimpleNamespace
    metrics={'ink':[0,-10,5,0]}
    measured=SimpleNamespace(cells=[metrics]*3,widths=[10,0,10],offsets=[0]*3,run_indices=[0]*3)
    style=replace(annotated[1],letter_spacing_px=-20,layout_semantics=semantics)
    local=local_line_geometry(measured,style,char_gaps=[0,3,5])
    expected=char_left_positions(measured.widths,0,False,-20,char_gaps=[0,3,5],
                                 n3_no_backtracking=semantics=='n3_1074')
    assert [c['origin_x'] for c in local.cells]==expected
    assert local.layout_width==expected[-1]+10


@pytest.mark.parametrize('value',[float('nan'),float('inf'),float('-inf')])
def test_nonfinite_ruby_glyph_snapshot_is_rejected(binary,annotated,value):
    from krok_helper.subtitle_render.engine.ruby.skia_metrics import measure_readings
    _,style=annotated
    with SkiaTextMetricsProcess(binary) as process:
        def measure(fonts,runs):
            results=process.measure(fonts,runs)
            for result in results:
                if result['cells']:result['cells'][0]['ink'][0]=value
            return results
        with pytest.raises(ValueError,match='非有限'):
            measure_readings([('字',style)],resolve_font=resolve_skia_font,measure=measure)


@pytest.mark.parametrize('change',[{'vertical':True},{'right_to_left':True},{'lower_ruby_enabled':True}])
def test_unimplemented_geometry_direction_is_explicitly_rejected(binary,annotated,change):
    track,style=annotated
    with pytest.raises(ValueError,match='正向横排上注音'):
        prepare(binary,track,replace(style,**change))


@pytest.mark.skipif(sys.platform!='win32',reason='Direct2D GPU geometry reference is Windows-only')
@pytest.mark.parametrize('semantics',['legacy','n3_1074'])
def test_prepared_main_and_ruby_bounds_match_d2d_gpu(binary,annotated,semantics):
    track,style=annotated
    from PyQt6.QtGui import QFontDatabase
    if not {'Meiryo','Arial'}<=set(QFontDatabase.families()):pytest.skip('Install Meiryo and Arial')
    style=replace(style,layout_semantics=semantics,font_family='Meiryo',font_family_latin='Arial',
                  ruby_font_family='Meiryo',ruby_font_family_latin='Arial')
    # Isolate box baselines and one target from the separate page-anchor and
    # interference integration, which is not enabled by this preparation layer.
    ruby=replace(track.rubies[0],kanji='字A',reading='字A',target_char_end=2,pos_end_ms=800)
    track=replace(track,rubies=[ruby])
    _assert_gpu_bounds(binary,track,style)


def _assert_gpu_bounds(binary,track,style):
    from test_subtitle_render_gpu_backend import _render_g1_frames
    main,prepared=prepare(binary,track,style)
    local=prepared.lines[0]
    item=SimpleNamespace(line=track.lines[0],layout_style=style,lane=0,page_line_count=1,center_override=False)
    position=ruby_line_position(item,main.lines[0],local,640,360)
    boxes=[local.main.ink]
    for ruby in local.rubies:
        for cell in ruby.cells:
            a,b,c,d=cell['metrics']['ink']
            if c>a and d>b:
                boxes.append((a+cell['origin_x'],b+cell['baseline_y'],
                              c+cell['origin_x'],d+cell['baseline_y']))
    expected=np.array([math.floor(min(b[0] for b in boxes)+position.offset_x),
                       math.floor(min(b[1] for b in boxes)+position.baseline_y),
                       math.ceil(max(b[2] for b in boxes)+position.offset_x)-1,
                       math.ceil(max(b[3] for b in boxes)+position.baseline_y)-1])
    with NativeRendererProcess() as d2d:
        _,raw=_render_g1_frames(d2d,style,(1200,),force_warp=False,track=track,width=640,height=360)
    pixels=np.frombuffer(raw[0],np.uint8).reshape(360,640,4)
    y,x=np.where(pixels[...,3]>127)
    observed=np.array([x.min(),y.min(),x.max(),y.max()])
    assert np.max(np.abs(expected-observed))<=1,(expected,observed)


@pytest.mark.skipif(sys.platform!='win32',reason='Direct2D GPU geometry reference is Windows-only')
@pytest.mark.parametrize('explicit',[False,True])
@pytest.mark.parametrize('role',[False,True])
def test_application_font_ruby_gpu_parity(binary,annotated,explicit,role):
    track,style=annotated
    if explicit:
        style=replace(style,ruby_font_family=style.font_family,ruby_font_family_latin=style.font_family_latin)
    if role:
        # Role faces previously lost both the physical resource and axis hint
        # in Direct2D, independently of the root style's resource selection.
        scheme=SubtitleStyleScheme(ruby_font_family=style.font_family_latin,
            ruby_font_family_latin=style.font_family,ruby_font_size_px=35,
            ruby_latin_font_size_px=29,ruby_latin_font_stretch_pct=110)
        style=replace(style,custom_style_schemes={'lead':scheme})
        track=replace(track,lines=[replace(l,chars=[replace(c,role_label='lead') for c in l.chars])
                                   for l in track.lines])
    _assert_gpu_bounds(binary,track,style)


@pytest.mark.skipif(sys.platform!='win32',reason='Direct2D GPU geometry reference is Windows-only')
@pytest.mark.parametrize('semantics',['legacy','n3_1074'])
def test_interference_line_anchor_matches_d2d_gpu(binary,annotated,semantics):
    from PyQt6.QtGui import QFontDatabase
    if not {'Meiryo','Arial'}<=set(QFontDatabase.families()):pytest.skip('Install Meiryo and Arial')
    track,style=annotated
    style=replace(style,layout_semantics=semantics,font_family='Meiryo',font_family_latin='Arial',
                  ruby_font_family='Meiryo',ruby_font_family_latin='Arial')
    _assert_gpu_bounds(binary,track,style)
