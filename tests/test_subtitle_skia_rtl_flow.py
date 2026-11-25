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
from test_subtitle_skia_ruby_groups import annotated, configure, mixed, pages, reading
from test_subtitle_skia_protocol import collect, selection
from test_subtitle_skia_protocol import test_actual_g5_preview_uses_skia_even_when_gpu_off as run_preview
from test_subtitle_skia_protocol import test_product_export_short_mp4_uses_skia as run_mp4
from test_subtitle_skia_grid import grid
from skia_text_fixture import make_kana_font
from PyQt6.QtGui import QFontDatabase
from krok_helper.subtitle_render.engine.text.font_weight import clear_font_weight_cache
from krok_helper.subtitle_render.domain.models import SubtitleStyleScheme
from krok_helper.subtitle_render.domain.paint import PaintFill
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
    track,style=rtl_source(project,kind,reverse=reverse,effect=effect)
    style=replace(style,layout_semantics=semantics,right_to_left=text_flow)
    assert_gpu_parity(binary,track,style,dpr,(kind,reverse,effect,semantics,dpr,alignment),alignment=alignment)


def assert_gpu_parity(binary,track,style,dpr,case,*,alignment=None):
    """Original 1px ink and normalized sung-front budgets against actual D2D GPU."""
    from test_subtitle_render_gpu_backend import _render_g1_frames
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
@pytest.mark.parametrize('cells',[False,True])
def test_rtl_gpu_raster_seek_and_offsets(binary,project,reverse,effect,cells):
    track,style=(rtl_cell_source(project,'roles',reverse=reverse,effect=effect) if cells
                 else rtl_source(project,'roles',reverse=reverse,effect=effect))
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


def test_vertical_layout_ignores_authored_horizontal_direction(binary, project):
    track, style = rtl_source(project)
    vertical = replace(style, vertical=True)
    ir = ir_for(binary, (track, vertical))
    control = ir_for(binary, (track, replace(vertical, right_to_left=False)))
    assert vertical.right_to_left
    assert not ir['style']['right_to_left']
    assert 'p05_horizontal_rtl' not in ir['required_features']
    assert ir['skia_layout'] == control['skia_layout']
    assert ir['track']['lines'] == control['track']['lines']


def test_rtl_upper_reading_without_body_stroke_preserves_scene(binary, reading):
    track, style = reading
    style = replace(style, right_to_left=True, stroke_width_px=0)
    ir = ir_for(binary, (track, style))
    bad = copy.deepcopy(ir)
    bad['required_features'].remove('p05_rtl_ruby_without_body_stroke')
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        configure(process, ir)
        frames, _ = collect(process, [550, 1100, 2300])
        replay, _ = collect(process, [2300, 1100, 550], generation=2)
    assert np.array_equal(frames, replay[::-1]) and all(frame.any() for frame in frames)


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
def test_rtl_upper_reading_without_body_stroke_matches_d2d_gpu(binary, application, tmp_path):
    from scripts.subtitle_gpu_baseline.cases import build_cases
    from test_subtitle_skia_character_fade import assert_ruby_transition_gpu_parity

    case = next(case for case in build_cases(tmp_path) if case.case_id == 'rtl')
    style = replace(case.style, stroke_width_px=0, stroke2_enabled=False,
                    decoration_kind='none', decoration_kind_before='none',
                    ruby_decoration_kind='none', ruby_decoration_kind_before='none')
    assert_ruby_transition_gpu_parity(binary, replace(case, style=style), (1500, 2600, 3400, 3900, 4400, 4501))


@pytest.mark.parametrize('reading_on_guide_row', [False, True])
def test_rtl_reading_and_role_guide_use_own_cells(binary, project, reading_on_guide_row):
    from test_subtitle_skia_outline_ruby import render_skia
    from test_subtitle_skia_static_glow import assert_glow_parity
    from test_subtitle_skia_vector_guides import SQUARE

    track, style = rtl_source(project, 'roles')
    first, = track.lines
    first = replace(first, track_line_index=0, inline_guide_symbols={1: SQUARE})
    shift = 3000
    second = replace(first, track_line_index=1, inline_guide_symbols={},
                     chars=[replace(char, role_label=None, start_ms=char.start_ms + shift) for char in first.chars],
                     end_ms=first.end_ms + shift, display_start_override_ms=shift, display_end_override_ms=shift + 2400)
    host = first if reading_on_guide_row else second
    reading = RubyAnnotation(host.chars[2].text, '字', target_line_index=host.track_line_index,
                             target_char_start=2, target_char_end=3,
                             pos_start_ms=host.chars[2].start_ms, pos_end_ms=host.chars[3].start_ms)
    track = replace(track, lines=[first, second], rubies=[reading])
    style = replace(style, ruby_font_family=style.font_family, ruby_font_size_px=24, ruby_gap_px=8,
                    ruby_colors_follow_main=False, ruby_karaoke_colors=colors('#FF5544FF', '#FFFF8C21'))
    times = (250, 750, 1300, 3250, 3750, 4300)
    ir = ir_for(binary, (track, style), dpr=1.5)
    assert [bool(row['ruby_groups']) for row in ir['skia_layout']['lines']] == [
        reading_on_guide_row, not reading_on_guide_row]
    assert ir['track']['lines'][0]['chars'][1]['role_label'] == 'lead'
    reference = None
    if sys.platform == 'win32':
        from test_subtitle_render_gpu_backend import _render_g1_frames

        with NativeRendererProcess() as process:
            _, raw = _render_g1_frames(process, style, times, force_warp=False, track=track,
                                       width=640, height=360, dpr=1.5, realization_enabled=False)
        reference = [np.frombuffer(frame, np.uint8).reshape(540, 960, 4) for frame in raw]
    gpu, raster = render_skia(binary, ir, times)
    assert_glow_parity(times, reference, gpu, raster, ('rtl_reading_and_role_guide', reading_on_guide_row),
                       check_p00_edges=True)


@pytest.mark.parametrize('target', ['gpu', 'raster'])
@pytest.mark.parametrize('semantics', ['legacy', 'n3_1074'])
@pytest.mark.parametrize('effect', ['none', 'utopia', 'no_wipe'])
def test_rtl_upper_reading_keeps_clocks_and_replays(binary, reading, target, semantics, effect):
    track, style = reading
    body = colors('#FFFFFFFF', '#FFFFFFFF')
    stroke = PaintFill(color='#FF000000')
    palette = replace(body, before=replace(body.before, stroke=stroke), after=replace(body.after, stroke=stroke))
    style = replace(style, right_to_left=True, stroke_width_px=4, stroke2_enabled=False, karaoke_colors=palette,
                    karaoke_anim=effect, layout_semantics=semantics)
    ir = ir_for(binary, (track, style))
    ltr = ir_for(binary, (track, replace(style, right_to_left=False)))
    feature = {'none': 'p05_horizontal_rtl_ruby', 'utopia': 'p07_rtl_ruby_singing_scale',
               'no_wipe': 'p07_rtl_ruby_no_wipe'}[effect]
    assert feature in ir['required_features']
    units = ir['skia_layout']['lines'][0]['ruby_groups'][0]['units']
    plain = ltr['skia_layout']['lines'][0]['ruby_groups'][0]['units']
    assert units[0]['origin_x'] > units[1]['origin_x']
    assert [cell['interval_ms'] for cell in units] == [cell['interval_ms'] for cell in plain]
    start, end = units[0]['interval_ms']
    times = [start - 1, (start + end) // 2, units[-1]['interval_ms'][1]]
    if effect == 'no_wipe':
        times[:2] = [end - 1, end]
    bad = copy.deepcopy(ir)
    bad['required_features'].remove(feature)
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, times)
        replay, _ = collect(process, times[::-1], generation=2)
    assert all(np.array_equal(frame, repeated) for frame, repeated in zip(frames, replay[::-1], strict=True))
    green = (frames[1][..., 1] > frames[1][..., 0]) & (frames[1][..., 3] > 15)
    red = (frames[1][..., 0] > frames[1][..., 1]) & (frames[1][..., 3] > 15)
    assert green.any() and red.any() and np.nonzero(green)[1].mean() > np.nonzero(red)[1].mean()
    if effect == 'no_wipe':
        assert not np.any((frames[0][..., 1] > frames[0][..., 0]) & (frames[0][..., 3] > 15))


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('semantics', ['legacy', 'n3_1074'])
@pytest.mark.parametrize('glow', [False, True])
@pytest.mark.parametrize('effect', ['none', 'utopia', 'no_wipe'])
def test_rtl_upper_reading_matches_actual_gpu(binary, application, tmp_path, semantics, glow, effect):
    from scripts.subtitle_gpu_baseline.cases import build_cases
    from test_subtitle_skia_character_fade import assert_ruby_transition_gpu_parity

    case = next(case for case in build_cases(tmp_path) if case.case_id == 'rtl')
    style = replace(case.style, layout_semantics=semantics, karaoke_anim=effect)
    if not glow:
        style = replace(style, decoration_kind='none', decoration_kind_before='none',
                        ruby_decoration_kind='none', ruby_decoration_kind_before='none')
    times = (300, 600, 1500, 2600, 3400, 3450, 3500, 3650, 3900, 4120, 4400, 4499, 4500, 4501, 5000)
    if effect == 'no_wipe':
        times = tuple(sorted({*times, 3674, 3675, 3676, 3949, 3950, 3951}))
    assert_ruby_transition_gpu_parity(binary, replace(case, style=style), times)


@pytest.mark.parametrize('target', ['gpu', 'raster'])
@pytest.mark.parametrize('effect', ['none', 'no_wipe'])
def test_rtl_ruby_shadow_keeps_the_same_front_after_seek(binary, reading, target, effect):
    track, style = reading
    palette = colors('#00000000', '#00000000')
    palette = replace(palette, before=replace(palette.before, shadow=PaintFill(color='#FFFF0000')),
                      after=replace(palette.after, shadow=PaintFill(color='#FF0000FF')))
    style = replace(style, right_to_left=True, stroke_width_px=4, karaoke_anim=effect,
                    decoration_before_sync=False, ruby_karaoke_colors=palette,
                    ruby_decoration_kind='shadow', ruby_decoration_kind_before='shadow',
                    ruby_shadow_offset_x=4, ruby_shadow_before_offset_x=4)
    ir = ir_for(binary, (track, style))
    feature = 'p06_rtl_ruby_shadow'
    assert feature in ir['required_features']
    group, = ir['skia_layout']['lines'][0]['ruby_groups']
    start, end = group['units'][0]['interval_ms']
    times = [start - 1, end if effect == 'no_wipe' else (start + end) // 2]
    bad = copy.deepcopy(ir)
    bad['required_features'].remove(feature)
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, times)
        replay, _ = collect(process, times[::-1], generation=2)
    assert all(np.array_equal(frame, repeated) for frame, repeated in zip(frames, replay[::-1], strict=True))
    channels = [np.sum(frames[1][..., channel].astype(float) * frames[1][..., 3], axis=0) for channel in (0, 2)]
    assert all(weights.sum() > 0 for weights in channels)
    red, blue = [np.average(np.arange(weights.size), weights=weights) for weights in channels]
    assert blue > red


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('effect', ['utopia', 'utopia_scanline'])
def test_rtl_singing_reading_shadow_and_band_match_actual_gpu(binary, application, effect):
    """Singing RTL readings clip shadows and sweep bands from the right, like the GPU."""
    from types import SimpleNamespace
    from krok_helper.subtitle_render.domain.timing import RubyAnnotation, TimingChar, TimingLine, TimingTrack
    from scripts.subtitle_render_corpus import _base_style
    from test_subtitle_skia_character_fade import render_ruby_transition_frames
    from test_subtitle_skia_static_glow import assert_glow_parity

    line = TimingLine(chars=[TimingChar('漢', 200), TimingChar('字', 700), TimingChar('か', 1100)], end_ms=1600)
    ruby = RubyAnnotation(kanji='漢字', reading='かんじ', reading_parts=['か', '', 'んじ'],
                          reading_part_ms=[450, 950], pos_start_ms=200, pos_end_ms=1100)
    track = TimingTrack(lines=[line], rubies=[ruby])
    style = _base_style(karaoke_anim=effect, right_to_left=True, ruby_decoration_kind='shadow',
                        ruby_decoration_kind_before='shadow', entry_lead_ms=500, exit_fade_ms=500)
    times = (250, 350, 450, 600, 800, 1000)
    reference, gpu, raster = render_ruby_transition_frames(binary, SimpleNamespace(track=track, style=style), times)

    def recoloured(expected, rendered):
        both = (expected[..., 3] > 200) & (rendered[..., 3] > 200)
        delta = np.abs(expected[..., :3].astype(int) - rendered[..., :3].astype(int)).max(-1)
        return round(float((delta[both] > 40).mean()), 4)

    # A reading shadow clipped on the wrong side recolours about 3% of the opaque pixels.
    shares = [[recoloured(*pair) for pair in zip(reference, frames, strict=True)] for frames in (gpu, raster)]
    assert max(map(max, shares)) < .015, shares
    assert_glow_parity(times, reference, gpu, raster, f'rtl_singing_reading_shadow_{effect}', check_p00_edges=True)



@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('transition', ['entry', 'exit'])
def test_rtl_reading_spin_turns_about_its_run_centre_like_actual_gpu(binary, application, transition):
    """An RTL reading lists its rightmost unit first; its spin still pivots on the whole run."""
    from types import SimpleNamespace
    from krok_helper.subtitle_render.domain.timing import RubyAnnotation, TimingChar, TimingLine, TimingTrack
    from scripts.subtitle_render_corpus import _base_style
    from test_subtitle_skia_character_fade import render_ruby_transition_frames
    from test_subtitle_skia_static_glow import assert_glow_parity

    line = TimingLine(chars=[TimingChar('漢', 200), TimingChar('字', 700), TimingChar('か', 1100)], end_ms=1600)
    ruby = RubyAnnotation(kanji='漢字', reading='かんじ', reading_parts=['か', '', 'んじ'],
                          reading_part_ms=[450, 950], pos_start_ms=200, pos_end_ms=1100)
    style = _base_style(right_to_left=True, entry_lead_ms=500, exit_fade_ms=500,
                        **{f'{transition}_anim': 'spin_flip'})
    times = (50, 100, 150, 200) if transition == 'entry' else (2200, 2300, 2400, 2500)
    case = SimpleNamespace(track=TimingTrack(lines=[line], rubies=[ruby]), style=style)
    reference, gpu, raster = render_ruby_transition_frames(binary, case, times)
    assert_glow_parity(times, reference, gpu, raster, f'rtl_reading_spin_{transition}', check_p00_edges=True)

_RTL_RUBY_LINE_PHASES = [('fade', 'fade'), ('slide_in', 'slide_out'), ('rise', 'rise')]


@pytest.mark.parametrize('target', ['gpu', 'raster'])
@pytest.mark.parametrize('effect', ['utopia', 'none', 'no_wipe'])
@pytest.mark.parametrize('entry,exit_', _RTL_RUBY_LINE_PHASES)
def test_rtl_ruby_line_motion_keeps_clocks_and_replays(binary, reading, target, effect, entry, exit_):
    track, style = reading
    body = colors('#FFFFFFFF', '#FFFFFFFF')
    stroke = PaintFill(color='#FF000000')
    palette = replace(body, before=replace(body.before, stroke=stroke), after=replace(body.after, stroke=stroke))
    decoration = {'utopia': 'none', 'none': 'shadow', 'no_wipe': 'glow'}[effect]
    style = replace(style, right_to_left=True, stroke_width_px=4, stroke2_enabled=False, karaoke_colors=palette,
                    karaoke_anim=effect, entry_anim=entry, exit_anim=exit_, entry_lead_ms=600, exit_fade_ms=600,
                    decoration_kind=decoration, ruby_decoration_kind=decoration, shadow_offset_x=9,
                    shadow_offset_y=6, ruby_shadow_offset_x=4, ruby_shadow_offset_y=3,
                    glow_before_radius_px=6, glow_after_radius_px=6, ruby_glow_radius_px=4)
    ir = ir_for(binary, (track, style), dpr=1.5)
    feature = ('p07_rtl_ruby_line_animation' if effect == 'utopia' else 'p07_rtl_ruby_unscaled_line_animation')
    assert feature in ir['required_features']
    line, = ir['track']['lines']
    assert (line['karaoke_anim'], line['entry_anim'], line['exit_anim']) == (effect, entry, exit_)
    times = [line['display_start_ms'] + 150, line['display_start_ms'] + 650, line['display_end_ms'] - 150]
    bad = copy.deepcopy(ir)
    bad['required_features'].remove(feature)
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, times)
        replay, _ = collect(process, times[::-1], generation=2)
    assert all(frame.any() for frame in frames)
    assert all(not np.array_equal(frame, frames[1]) for frame in (frames[0], frames[2]))
    assert all(np.array_equal(frame, repeated) for frame, repeated in zip(frames, replay[::-1], strict=True))


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('entry,exit_', _RTL_RUBY_LINE_PHASES)
def test_rtl_ruby_line_motion_matches_actual_gpu(binary, application, tmp_path, entry, exit_):
    from scripts.subtitle_gpu_baseline.cases import build_cases
    from test_subtitle_skia_character_fade import assert_ruby_transition_gpu_parity

    case = next(case for case in build_cases(tmp_path) if case.case_id == 'rtl')
    style = replace(case.style, entry_anim=entry, exit_anim=exit_)
    times = (299, 301, 375, 500, 750, 899, 900, 1500, 3950, 4500, 4900, 5100, 5350, 5499, 5500)
    assert_ruby_transition_gpu_parity(binary, replace(case, style=style), times)


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('effect,entry,exit_,before,after,dpr', [
    (effect, entry, exit_, before, after, 1)
    for effect in ('none', 'no_wipe')
    for entry, exit_ in _RTL_RUBY_LINE_PHASES
    for before, after in (('none', 'none'), ('shadow', 'shadow'), ('glow', 'glow'),
                          ('shadow', 'glow'), ('glow', 'shadow'))
] + [
    ('none', 'fade', 'fade', 'none', 'none', 1.5),
    ('none', 'slide_in', 'slide_out', 'shadow', 'shadow', 1.5),
    ('no_wipe', 'rise', 'rise', 'glow', 'shadow', 1.5),
])
def test_rtl_ruby_unscaled_motion_matches_actual_gpu(binary, application, tmp_path, effect, entry, exit_, before, after, dpr):
    from scripts.subtitle_gpu_baseline.cases import build_cases
    from test_subtitle_skia_character_fade import assert_ruby_transition_gpu_parity

    case = next(case for case in build_cases(tmp_path) if case.case_id == 'rtl')
    style = replace(case.style, karaoke_anim=effect, entry_anim=entry, exit_anim=exit_, decoration_before_sync=False,
                    decoration_kind=after, decoration_kind_before=before,
                    ruby_decoration_kind=after, ruby_decoration_kind_before=before,
                    shadow_offset_x=9, shadow_offset_y=6, shadow_before_offset_x=9, shadow_before_offset_y=6,
                    ruby_shadow_offset_x=4, ruby_shadow_offset_y=3,
                    ruby_shadow_before_offset_x=4, ruby_shadow_before_offset_y=3)
    times = (299, 301, 307, 317, 336, 375, 500, 750, 899, 900, 1500, 3950,
             4500, 4900, 5100, 5350, 5464, 5483, 5493, 5499, 5500)
    if dpr != 1:
        times = (301, 307, 317, 336, 500, 900, 1500, 4500, 5350, 5464, 5483, 5493, 5499)
    assert_ruby_transition_gpu_parity(binary, replace(case, style=style), times, dpr=dpr)


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('effect,semantics,before,after', [
    ('none', 'legacy', 'shadow', 'shadow'), ('none', 'n3_1074', 'shadow', 'shadow'),
    ('no_wipe', 'legacy', 'shadow', 'shadow'), ('no_wipe', 'n3_1074', 'shadow', 'shadow'),
    ('no_wipe', 'legacy', 'shadow', 'glow'), ('no_wipe', 'n3_1074', 'glow', 'shadow'),
])
def test_rtl_ruby_shadow_matches_actual_gpu(binary, application, tmp_path, effect, semantics, before, after):
    from scripts.subtitle_gpu_baseline.cases import build_cases
    from test_subtitle_skia_character_fade import assert_ruby_transition_gpu_parity

    case = next(case for case in build_cases(tmp_path) if case.case_id == 'rtl')
    style = replace(case.style, layout_semantics=semantics, karaoke_anim=effect, decoration_before_sync=False,
                    decoration_kind=after, decoration_kind_before=before,
                    ruby_decoration_kind=after, ruby_decoration_kind_before=before,
                    shadow_offset_x=9, shadow_offset_y=6, shadow_before_offset_x=9, shadow_before_offset_y=6,
                    ruby_shadow_offset_x=4, ruby_shadow_offset_y=3,
                    ruby_shadow_before_offset_x=4, ruby_shadow_before_offset_y=3)
    times = (300, 600, 1500, 2600, 3400, 3450, 3500, 3650, 3900, 4120, 4400, 4499, 4500, 4501, 5000)
    assert_ruby_transition_gpu_parity(binary, replace(case, style=style), times)


# horizontal_rtl_cell_schema=1: GPU-validated multi-codepoint authored cells
# (Latin/Latin-1, combining diacritics, CJK ideographs, Hangul syllables).
CELL_TEXTS={'cells':['fi','a\u0301','字字','AA'],'spaces':['A ','  ',' \u00e1','字 ']}


def rtl_cell_source(project,kind='cells',*,reverse=False,effect='none'):
    base={'roles':'roles','negative':'negative_gap'}.get(kind,'plain')
    track,style=rtl_source(project,base,reverse=reverse,effect=effect)
    line=track.lines[0]
    texts=CELL_TEXTS['spaces' if kind=='spaces' else 'cells']
    return replace(track,lines=[replace(line,chars=[replace(c,text=t) for c,t in zip(line.chars,texts)])]),style


@pytest.mark.parametrize('text',['\u3099か','か\u3099\u309a','A\u3099','ゝあ','A\uff5e','שׁל','سل','A\u200dA','A\u200cA','A\ufe0f','\U0001F469\U0001F4BB',
                                 'a\u034f','a\u035cb','a\u00adb','\u0103a'])
def test_unvalidated_rtl_multicodepoint_cells_stay_gated(binary,project,text):
    track,style=rtl_source(project)
    line=track.lines[0]
    track=replace(track,lines=[replace(line,chars=[replace(line.chars[0],text=text),*line.chars[1:]])])
    with pytest.raises(ValueError,match='未验证'):ir_for(binary,(track,style))


@pytest.mark.parametrize('reverse',[False,True])
def test_rtl_multicodepoint_cells_are_versioned_whole_cells(binary,project,reverse,tmp_path):
    track,style=rtl_cell_source(project,reverse=reverse)
    ir=ir_for(binary,(track,style))
    ltr=ir_for(binary,(track,replace(style,right_to_left=False)))
    cells,plain=ir['skia_layout']['lines'][0]['cells'],ltr['skia_layout']['lines'][0]['cells']
    assert 'p05_horizontal_rtl_cells' in ir['required_features'] and ir['skia_layout']['horizontal_rtl_cell_schema']==1
    assert 'p05_horizontal_rtl_cells' not in ltr['required_features']
    assert all(a['origin_x']>b['origin_x'] for a,b in zip(cells,cells[1:]))
    # Whole authored cells move: glyphs, clusters, shaping policy and clocks are the LTR ones.
    assert [c['metrics'] for c in cells]==[c['metrics'] for c in plain]
    assert [len(c['metrics']['glyphs']) for c in cells]==[2,1,2,2]
    assert ir['track']['lines'][0]['resolved_intervals']==ltr['track']['lines'][0]['resolved_intervals']
    single=ir_for(binary,rtl_source(project,reverse=reverse))
    assert 'p05_horizontal_rtl_cells' not in single['required_features']
    assert 'horizontal_rtl_cell_schema' not in single['skia_layout']
    if reverse:assert [c['clock_index'] for c in cells]==[3,2,1,0]
    else:
        (tmp_path/'rtl-cell-layout.json').write_text(json.dumps({
            'features':ir['required_features'],'schema':ir['skia_layout']['horizontal_rtl_cell_schema'],
            'lines':ir['skia_layout']['lines'],'intervals':ir['track']['lines'][0]['resolved_intervals']},
            sort_keys=True),encoding='utf-8')


@pytest.mark.parametrize('mutation',['feature','base_feature','schema','schema_bool','schema_string','schema_version',
                                     'unvalidated_text'])
@pytest.mark.parametrize('target',['gpu','raster'])
def test_native_rtl_cell_contract_rejects_forgeries_and_keeps_scene(binary,project,mutation,target):
    ir=ir_for(binary,rtl_cell_source(project))
    bad=copy.deepcopy(ir)
    if mutation=='feature':bad['required_features'].remove('p05_horizontal_rtl_cells')
    elif mutation=='base_feature':bad['required_features'].remove('p05_horizontal_rtl')
    elif mutation=='schema':del bad['skia_layout']['horizontal_rtl_cell_schema']
    elif mutation.startswith('schema_'):
        bad['skia_layout']['horizontal_rtl_cell_schema']={'schema_bool':True,'schema_string':'1','schema_version':2}[mutation]
    else:bad['track']['lines'][0]['chars'][2]['text']='きゃ'
    with SkiaRendererProcess(binary,selection=selection(target)) as process:
        configure(process,ir);before,_=collect(process,[750])
        with pytest.raises(NativeRendererError):configure(process,bad)
        after,_=collect(process,[750],generation=2)
    assert np.array_equal(before[0],after[0])


def test_rtl_multicodepoint_negative_spacing_is_whole_cell_layout(binary,project):
    # body_phase_order_schema=1 proves overlapping cells; no extra layout feature.
    ir=ir_for(binary,rtl_cell_source(project,'negative'))
    assert ir['skia_layout']['horizontal_rtl_cell_schema']==1
    cells=ir['skia_layout']['lines'][0]['cells']
    assert all(a['origin_x']>b['origin_x'] for a,b in zip(cells,cells[1:]))


@pytest.mark.parametrize('target',['gpu','raster'])
def test_role_spacing_never_changes_the_rtl_layout_gate(binary,project,target):
    # Roles keep layout spacing fields: a negative role scheme spacing changes
    # neither geometry nor the gate, in Python or native.
    track,style=rtl_cell_source(project,'roles')
    lead=replace(style.custom_style_schemes['lead'],letter_spacing_px=-18)
    ir=ir_for(binary,(track,replace(style,custom_style_schemes={'lead':lead})))
    plain=ir_for(binary,(track,style))
    assert ir['skia_layout']['lines']==plain['skia_layout']['lines']
    with SkiaRendererProcess(binary,selection=selection(target)) as process:configure(process,ir)


# Formerly a strict xfail: a finishing cell's front inside an overlapping
# neighbor's ink. Fixed by body_phase_order_schema=1 (GPU N3 phase stacking).
@pytest.mark.skipif(sys.platform != 'win32',reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('text_flow',[False,True])
def test_overlapping_cell_front_matches_gpu(binary,project,text_flow):
    track,style=rtl_source(project,'negative_gap')
    line=track.lines[0]
    texts=['A' if text_flow else 'fi','\u0301','字','W']
    track=replace(track,lines=[replace(line,chars=[replace(c,text=t) for c,t in zip(line.chars,texts)])])
    assert_gpu_parity(binary,track,replace(style,right_to_left=text_flow),1.5,('overlap',text_flow))


# body_phase_order_schema=1: the shared core stacks overlapping body cells in
# the existing GPU's N3 phase order (unsung last-to-first, sung, wiping on top).
OVERLAP={'mark':['fi','\u0301','字','W'],'cells':['fi','a\u0301','字字','AA']}
OVERLAP_CASES=[
    *[('mark',rtl,rev,'none','plain',-18,False,'n3_1074',1.5) for rtl in (False,True) for rev in (False,True)],
    *[('cells',rtl,rev,'none','plain',-18,False,'legacy',1.5) for rtl in (False,True) for rev in (False,True)],
    ('mark',False,False,'no_wipe','plain',-18,False,'n3_1074',1.5),
    ('cells',True,True,'no_wipe','plain',-18,False,'legacy',1.5),
    ('mark',True,False,'none','pause',-18,False,'n3_1074',1),
    ('cells',False,True,'none','zero',-18,False,'legacy',1.5),
    ('cells',True,False,'none','all_zero',-18,False,'n3_1074',1),
    ('cells',True,False,'none','roles',-18,False,'n3_1074',1.5),
    ('mark',False,True,'none','roles',-18,False,'legacy',1),
    ('cells',True,False,'none','plain',3,True,'legacy',1),
    ('cells',False,True,'none','plain',3,True,'n3_1074',1.5),
    ('cells',True,False,'none','plain',-100,False,'n3_1074',1.5),
    ('cells',False,False,'none','plain',3,False,'legacy',1),
]


def overlap_source(project,texts,rtl,reverse,effect,base,spacing,biting):
    track,style=rtl_source(project,base,reverse=reverse,effect=effect)
    line=track.lines[0]
    track=replace(track,lines=[replace(line,chars=[replace(c,text=t) for c,t in zip(line.chars,OVERLAP[texts])])])
    return track,replace(style,right_to_left=rtl,letter_spacing_px=spacing,allow_biting=biting)


@pytest.mark.skipif(sys.platform != 'win32',reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('case',OVERLAP_CASES,ids=lambda c:'-'.join(map(str,c)))
def test_overlapping_cells_match_actual_gpu(binary,project,case):
    texts,rtl,reverse,effect,base,spacing,biting,semantics,dpr=case
    track,style=overlap_source(project,texts,rtl,reverse,effect,base,spacing,biting)
    assert_gpu_parity(binary,track,replace(style,layout_semantics=semantics),dpr,case)


@pytest.mark.parametrize('texts,rtl,reverse',[('cells',True,False),('mark',False,True),('cells',True,True)])
def test_overlapping_cells_gpu_raster_and_out_of_order_seek(binary,project,texts,rtl,reverse):
    track,style=overlap_source(project,texts,rtl,reverse,'none','roles',-18,False)
    ir=ir_for(binary,(track,style),dpr=1.5)
    order=[2200,601,199,900,599,1401,600,899,1399,200]
    results=[]
    for target in ('gpu','raster'):
        with SkiaRendererProcess(binary,selection=selection(target)) as process:
            configure(process,ir)
            ordered,_=collect(process,sorted(order))
            shuffled,_=collect(process,order,generation=2)
        by_time=dict(zip(sorted(order),ordered))
        assert all(np.array_equal(by_time[t],f) for t,f in zip(order,shuffled))
        results.append(ordered)
    for a,b in zip(*results):
        def premult(p):
            p=p.astype(float);p[...,:3]*=p[...,3:]/255
            return p
        delta=abs(premult(a)-premult(b))
        assert delta.mean()<.15 and np.mean(delta.max(axis=-1)>16)<.003


@pytest.mark.parametrize('mutation',['schema','bool','version'])
def test_old_body_phase_sidecar_rejected_before_selection(binary,monkeypatch,mutation):
    process=SkiaRendererProcess(binary,selection=selection('raster'))
    try:ready=NativeRendererProcess.start(process)
    finally:process.abort(reason='body phase handshake fixture')
    if mutation=='schema':del ready['body_phase_order_schema']
    else:ready['body_phase_order_schema']=True if mutation=='bool' else 2
    sent=[]
    monkeypatch.setattr(NativeRendererProcess,'start',lambda self:ready)
    monkeypatch.setattr(SkiaRendererProcess,'abort',lambda *a,**k:None)
    monkeypatch.setattr(SkiaRendererProcess,'_send',lambda self,p:sent.append(p))
    with pytest.raises(NativeRendererError,match='层序'):SkiaRendererProcess(binary).start()
    assert not sent


# horizontal_rtl_kana_schema=1: kana/Japanese punctuation cells drawn from
# nominal glyphs. Combining U+3099/U+309A use the separate kana mark policy.
KANA={'kana':['きゃ','がっ','パァ','キャ'],'punct':['「あ」','、。','ー！','〜・'],
      'spacing':['か゛','は゜','ぁ\u3000','ッ？']}


@pytest.fixture
def kana(tmp_path,application,request):
    handle=QFontDatabase.addApplicationFont(str(make_kana_font(tmp_path/'kana.ttf',gpos=getattr(request,'param',True))))
    assert handle>=0
    clear_font_weight_cache()
    try:yield QFontDatabase.applicationFontFamilies(handle)[0]
    finally:
        QFontDatabase.removeApplicationFont(handle)
        clear_font_weight_cache()


def kana_source(project,family,texts='kana',kind='plain',*,reverse=False,effect='none',rtl=True):
    track,style=rtl_source(project,kind,reverse=reverse,effect=effect)
    line=track.lines[0]
    chars=KANA[texts] if isinstance(texts,str) else texts
    track=replace(track,lines=[replace(line,chars=[replace(c,text=t) for c,t in zip(line.chars,chars)])])
    style=replace(style,font_family=family,right_to_left=rtl)
    if kind=='roles':
        style=replace(style,custom_style_schemes={'lead':replace(style.custom_style_schemes['lead'],font_family=family)})
    return track,style


@pytest.mark.parametrize('reverse',[False,True])
def test_rtl_kana_cells_are_versioned_whole_cells(binary,project,kana,reverse,tmp_path):
    track,style=kana_source(project,kana,'punct',reverse=reverse)
    ir=ir_for(binary,(track,style))
    ltr=ir_for(binary,(track,replace(style,right_to_left=False)))
    cells,plain=ir['skia_layout']['lines'][0]['cells'],ltr['skia_layout']['lines'][0]['cells']
    assert {'p05_horizontal_rtl_cells','p05_horizontal_rtl_kana_cells'}<=set(ir['required_features'])
    assert ir['skia_layout']['horizontal_rtl_kana_schema']==1
    assert 'p05_horizontal_rtl_kana_cells' not in ltr['required_features']
    assert all(a['origin_x']>b['origin_x'] for a,b in zip(cells,cells[1:]))
    # Whole cells, nominal glyphs and clocks are exactly the LTR ones.
    assert [c['metrics'] for c in cells]==[c['metrics'] for c in plain]
    assert [len(c['metrics']['glyphs']) for c in cells]==[3,2,2,2]
    assert ir['track']['lines'][0]['resolved_intervals']==ltr['track']['lines'][0]['resolved_intervals']
    latin=ir_for(binary,rtl_cell_source(project,reverse=reverse))
    assert 'p05_horizontal_rtl_kana_cells' not in latin['required_features']
    assert 'horizontal_rtl_kana_schema' not in latin['skia_layout']
    if reverse:assert [c['clock_index'] for c in cells]==[3,2,1,0]
    else:
        (tmp_path/'rtl-kana-layout.json').write_text(json.dumps({
            'features':ir['required_features'],'schema':ir['skia_layout']['horizontal_rtl_kana_schema'],
            'lines':ir['skia_layout']['lines'],'intervals':ir['track']['lines'][0]['resolved_intervals']},
            sort_keys=True),encoding='utf-8')


@pytest.mark.parametrize('mutation',['feature','cells_feature','schema','schema_bool','schema_string','schema_version',
                                     'combining_text'])
@pytest.mark.parametrize('target',['gpu','raster'])
def test_native_rtl_kana_contract_rejects_forgeries_and_keeps_scene(binary,project,kana,mutation,target):
    ir=ir_for(binary,kana_source(project,kana))
    bad=copy.deepcopy(ir)
    if mutation=='feature':bad['required_features'].remove('p05_horizontal_rtl_kana_cells')
    elif mutation=='cells_feature':bad['required_features'].remove('p05_horizontal_rtl_cells')
    elif mutation=='schema':del bad['skia_layout']['horizontal_rtl_kana_schema']
    elif mutation.startswith('schema_'):
        bad['skia_layout']['horizontal_rtl_kana_schema']={'schema_bool':True,'schema_string':'1','schema_version':2}[mutation]
    else:bad['track']['lines'][0]['chars'][0]['text']='か\u3099\u309a'
    with SkiaRendererProcess(binary,selection=selection(target)) as process:
        configure(process,ir);before,_=collect(process,[750])
        with pytest.raises(NativeRendererError):configure(process,bad)
        after,_=collect(process,[750],generation=2)
    assert np.array_equal(before[0],after[0])


@pytest.mark.parametrize('mutation',['schema','feature','bool','version'])
def test_old_rtl_kana_sidecar_rejected_before_selection(binary,monkeypatch,mutation):
    process=SkiaRendererProcess(binary,selection=selection('raster'))
    try:ready=NativeRendererProcess.start(process)
    finally:process.abort(reason='RTL kana handshake fixture')
    if mutation=='schema':del ready['horizontal_rtl_kana_schema']
    elif mutation=='feature':ready['profiles'].remove('p05_horizontal_rtl_kana_cells')
    else:ready['horizontal_rtl_kana_schema']=True if mutation=='bool' else 2
    sent=[]
    monkeypatch.setattr(NativeRendererProcess,'start',lambda self:ready)
    monkeypatch.setattr(SkiaRendererProcess,'abort',lambda *a,**k:None)
    monkeypatch.setattr(SkiaRendererProcess,'_send',lambda self,p:sent.append(p))
    with pytest.raises(NativeRendererError,match='假名'):SkiaRendererProcess(binary).start()
    assert not sent


KANA_CASES=[
    *[(t,rev,'none','plain',sem,dpr) for t in ('kana','punct','spacing')
      for rev,sem,dpr in ((False,'n3_1074',1.5),(True,'legacy',1),(False,'legacy',1),(True,'n3_1074',1.5))],
    ('kana',False,'no_wipe','plain','legacy',1.5),('kana',True,'no_wipe','plain','n3_1074',1),
    ('kana',False,'none','negative_gap','n3_1074',1.5),('punct',True,'none','negative_gap','legacy',1.5),
    ('kana',True,'none','roles','n3_1074',1.5),('spacing',False,'none','roles','legacy',1),
    ('punct',False,'none','pause','n3_1074',1.5),('spacing',True,'none','zero','legacy',1.5),
]


@pytest.mark.skipif(sys.platform != 'win32',reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('case',KANA_CASES,ids=lambda c:'-'.join(map(str,c)))
def test_rtl_kana_cells_match_actual_gpu(binary,project,kana,case):
    texts,reverse,effect,kind,semantics,dpr=case
    track,style=kana_source(project,kana,texts,kind,reverse=reverse,effect=effect)
    assert_gpu_parity(binary,track,replace(style,layout_semantics=semantics),dpr,case)


@pytest.mark.skipif(sys.platform != 'win32',reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('alignment',['left','center','right'])
def test_rtl_kana_row_anchors_match_actual_gpu(binary,project,kana,alignment):
    track,style=kana_source(project,kana,'punct',reverse=alignment=='right')
    assert_gpu_parity(binary,track,replace(style,layout_semantics='n3_1074'),1.5,('anchor',alignment),alignment=alignment)


# Former strict xfails for canonical composition and fallback positioning.
@pytest.mark.skipif(sys.platform != 'win32',reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('kana,texts',[(True,['か\u3099','は\u309a','あ','き']),
                                       (False,['う\u3099','あ','き','う'])],indirect=['kana'],ids=['compose','fallback'])
def test_ltr_kana_voicing_marks_match_gpu(binary,project,kana,texts):
    track,style=kana_source(project,kana,texts,rtl=False)
    assert_gpu_parity(binary,track,replace(style,layout_semantics='n3_1074'),1.5,('voicing',texts))


def test_rtl_kana_gpu_raster_and_out_of_order_seek(binary,project,kana):
    track,style=kana_source(project,kana,'kana','roles',reverse=True)
    ir=ir_for(binary,(track,style),dpr=1.5)
    order=[2200,601,199,900,599,1401,600,899,1399,200]
    results=[]
    for target in ('gpu','raster'):
        with SkiaRendererProcess(binary,selection=selection(target)) as process:
            configure(process,ir)
            ordered,_=collect(process,sorted(order))
            shuffled,_=collect(process,order,generation=2)
        by_time=dict(zip(sorted(order),ordered))
        assert all(np.array_equal(by_time[t],f) for t,f in zip(order,shuffled))
        results.append(ordered)
    for a,b in zip(*results):
        def premult(p):
            p=p.astype(float);p[...,:3]*=p[...,3:]/255
            return p
        delta=abs(premult(a)-premult(b))
        assert delta.mean()<.15 and np.mean(delta.max(axis=-1)>16)<.003


@pytest.mark.parametrize('gpu,missing_loader',[(True,False),(False,False),(True,True)])
def test_real_rtl_kana_preview(binary,project,kana,monkeypatch,tmp_path,gpu,missing_loader):
    run_preview(binary,monkeypatch,tmp_path,kana_source(project,kana,'punct','roles',reverse=True),gpu,missing_loader)


@pytest.mark.parametrize('mutation',['schema','feature','bool','version'])
def test_old_rtl_cell_sidecar_rejected_before_selection(binary,monkeypatch,mutation):
    process=SkiaRendererProcess(binary,selection=selection('raster'))
    try:ready=NativeRendererProcess.start(process)
    finally:process.abort(reason='RTL cell handshake fixture')
    if mutation=='schema':del ready['horizontal_rtl_cell_schema']
    elif mutation=='feature':ready['profiles'].remove('p05_horizontal_rtl_cells')
    else:ready['horizontal_rtl_cell_schema']=True if mutation=='bool' else 2
    sent=[]
    monkeypatch.setattr(NativeRendererProcess,'start',lambda self:ready)
    monkeypatch.setattr(SkiaRendererProcess,'abort',lambda *a,**k:None)
    monkeypatch.setattr(SkiaRendererProcess,'_send',lambda self,p:sent.append(p))
    with pytest.raises(NativeRendererError,match='多码点'):SkiaRendererProcess(binary).start()
    assert not sent


@pytest.mark.skipif(sys.platform != 'win32',reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('kind',['cells','spaces','roles','negative'])
@pytest.mark.parametrize('semantics',['legacy','n3_1074'])
@pytest.mark.parametrize('dpr',[1,1.5])
def test_rtl_multicodepoint_cells_match_actual_gpu(binary,project,kind,semantics,dpr):
    track,style=rtl_cell_source(project,kind)
    assert_gpu_parity(binary,track,replace(style,layout_semantics=semantics),dpr,(kind,semantics,dpr))


@pytest.mark.skipif(sys.platform != 'win32',reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('reverse,effect',[(True,'none'),(False,'no_wipe'),(True,'no_wipe')])
@pytest.mark.parametrize('semantics',['legacy','n3_1074'])
def test_rtl_multicodepoint_singing_variants_match_actual_gpu(binary,project,reverse,effect,semantics):
    track,style=rtl_cell_source(project,reverse=reverse,effect=effect)
    assert_gpu_parity(binary,track,replace(style,layout_semantics=semantics),1.5,(reverse,effect,semantics))


@pytest.mark.skipif(sys.platform != 'win32',reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('alignment',['left','center','right'])
@pytest.mark.parametrize('semantics',['legacy','n3_1074'])
@pytest.mark.parametrize('reverse',[False,True])
def test_rtl_multicodepoint_row_anchors_match_actual_gpu(binary,project,alignment,semantics,reverse):
    track,style=rtl_cell_source(project,reverse=reverse)
    assert_gpu_parity(binary,track,replace(style,layout_semantics=semantics),1.5,
                      (alignment,semantics,reverse),alignment=alignment)


@pytest.mark.parametrize('gpu,missing_loader',[(True,False),(False,False),(True,True)])
def test_real_rtl_multicodepoint_preview(binary,project,monkeypatch,tmp_path,gpu,missing_loader):
    run_preview(binary,monkeypatch,tmp_path,rtl_cell_source(project,'roles',reverse=True),gpu,missing_loader)


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


# Formerly strict xfails: fixed by cell_glyph_schema=1 (nominal mark-free
# cells; nominal-glyph cell boxes), see test_subtitle_skia_cell_glyphs.py.
@pytest.mark.skipif(sys.platform != 'win32',reason='Actual Direct2D GPU reference')
def test_ltr_ligature_source_cell_matches_gpu(binary,project):
    assert_actual_gpu_parity(binary,project,'ligature_only',False,'none','legacy',1.5,text_flow=False)


@pytest.mark.skipif(sys.platform != 'win32',reason='Actual Direct2D GPU reference')
def test_ltr_decomposed_accent_cell_width_matches_gpu(binary,project):
    assert_actual_gpu_parity(binary,project,'decomposed_unit',False,'none','n3_1074',1.5,
                              alignment='left',text_flow=False)


@pytest.mark.skipif(sys.platform != 'win32',reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('reverse',[False,True])
@pytest.mark.parametrize('semantics',['legacy','n3_1074'])
def test_rtl_collapsed_negative_spacing_matches_gpu(binary,project,reverse,semantics):
    assert_actual_gpu_parity(binary,project,'collapsed_gap',reverse,'none',semantics,1.5)
