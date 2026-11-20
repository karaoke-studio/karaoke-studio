"""Multi-row canonical geometry with shared page/show-time semantics."""
import copy
from dataclasses import replace
import json
import sys
from types import SimpleNamespace

import numpy as np
import pytest

from test_subtitle_skia_roles import application, binary, colors, project, roles, mixed
from test_subtitle_skia_resolved import ir_for
from test_subtitle_skia_protocol import collect, selection
from krok_helper.subtitle_render.domain.models import LyricsLayout, SubtitleStyleScheme, TimingChar, TimingLine
from krok_helper.subtitle_render.domain.timing import TrackDisplayTiming, TrackPage, TrackSection, TrackPagePlan
from krok_helper.subtitle_render.engine.layout.line.skia_placement import place_horizontal_line
from krok_helper.subtitle_render.engine.layout.display.skia_schedule import display_lines
from krok_helper.subtitle_render.native.backend import NativeRendererError, NativeRendererProcess
from krok_helper.subtitle_render.native.skia_backend import SkiaRendererProcess


@pytest.fixture
def multiple_sources(project):
    track, style = project
    style = replace(style, timing_offset_ms=110, font_size_px=90,
                    karaoke_colors=colors('#E21416', '#E21416'),
                    custom_style_schemes={
                        'green': SubtitleStyleScheme(karaoke_colors=colors('#14E216', '#14E216')),
                        'blue': SubtitleStyleScheme(karaoke_colors=colors('#1416E2', '#1416E2')),
                    })
    line = TimingLine(chars=[TimingChar('A', 1000)], end_ms=1400,
                      display_start_override_ms=0, display_end_override_ms=2400)
    main = replace(track, lines=[line], meta=replace(track.meta, offset_ms=140))
    extras = []
    for label, offset, end in [('green', -90, 1600), ('blue', 270, 1750)]:
        side_line = replace(line, chars=[replace(line.chars[0], role_label=label)],
                            display_start_override_ms=800, display_end_override_ms=end)
        extras.append(replace(track, lines=[side_line], meta=replace(track.meta, offset_ms=offset)))
    extras[0].display_timing = TrackDisplayTiming(follow_main=False, overrides={'timing_offset_ms': 420})
    return main, style, extras


@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_multiple_sources_use_separate_clocks_and_composite_in_list_order(binary, multiple_sources, target):
    main, style, extras = multiple_sources
    style = replace(style, viewport_offset_x=24, viewport_offset_y=-16)
    args = dict(width=640, height=360, fps=60, roll_shift_ms=350)
    times = [599, 600, 1479, 1480, 1530, 2280, 2480, 3000, 1480]
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        original = process.configure(main, style, extra_tracks=extras, **args)
        output, _ = collect(process, times)
        swapped = process.configure(main, style, extra_tracks=extras[::-1], **args)
        reverse, _ = collect(process, [1530], generation=2)
    assert original['metrics_identity'] != swapped['metrics_identity']
    assert not output[0][..., 3].any() and not output[-2][..., 3].any()
    assert np.array_equal(output[1], output[2]) and np.array_equal(output[3], output[-1])
    # Removing the second source changes the antialiased edge under the top
    # source, even though the opaque center retains the same top color.
    assert output[4][..., 3].sum() > output[5][..., 3].sum()
    assert np.array_equal(output[1], output[6])
    for frame, color in ((output[1], [226, 20, 22]), (output[3], [20, 226, 22]),
                         (output[4], [20, 22, 226]), (output[5], [20, 22, 226]),
                         (reverse[0], [20, 226, 22])):
        opaque = frame[..., 3] == 255
        assert opaque.any()
        assert np.all(frame[opaque][:, :3] == color)


def test_multiple_sources_validate_all_sources_before_replacing_scene(binary, multiple_sources):
    main, style, extras = multiple_sources
    ir = ir_for(binary, (main, style), extra_tracks=extras)
    valid = ir['skia_extra_sources']
    assert ir['track']['meta']['offset_ms'] == 140
    assert valid[0]['style']['timing_offset_ms'] == 420 and valid[0]['track']['meta']['offset_ms'] == -90
    assert valid[1]['style']['timing_offset_ms'] == 110
    mutations = []
    for key, value in [('screen', {**ir['screen'], 'width': 641}), ('skia_extra_sources', []),
                       ('skia_layout', None), ('required_features', ['p08_static_line_band'])]:
        invalid = copy.deepcopy(ir)
        invalid['skia_extra_sources'][1][key] = value
        mutations.append(invalid)
    undeclared = copy.deepcopy(ir)
    undeclared['required_features'].remove('p08_multiple_sources')
    mutations.append(undeclared)
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        process.configure(main, style, extra_tracks=extras, width=640, height=360, fps=60)
        before, _ = collect(process, [1200])
        for invalid in mutations:
            process._send({'cmd': 'configure', 'ir': invalid})
            with pytest.raises(NativeRendererError):
                process._expect_ok(process._read_until_event('configured'))
        after, _ = collect(process, [1200], generation=2)
    assert np.array_equal(before[0], after[0])


def test_multiple_sources_keep_independent_page_plans(binary, grid):
    track, style = grid
    style = replace(style, sync_entry=True, sync_ending=True, sync_each_page=True)
    main = replace(track, lines=track.lines[:3],
                   page_plan=TrackPagePlan(sections=[TrackSection(pages=[TrackPage(3)])]))
    side = replace(track, lines=track.lines[3:7],
                   page_plan=TrackPagePlan(sections=[TrackSection(pages=[TrackPage(2), TrackPage(2)])]))
    combined = ir_for(binary, (main, style), extra_tracks=[side])
    extra = combined['skia_extra_sources'][0]
    for source, payload in ((main, combined), (side, extra)):
        single = ir_for(binary, (source, style))
        assert payload['track'] == single['track']
        assert payload['skia_layout']['lines'] == single['skia_layout']['lines']
    assert [line['lane'] for line in combined['track']['lines']] == [0, 1, 2]
    assert [line['lane'] for line in extra['track']['lines']] == [1, 2, 1, 2]


@pytest.mark.skipif(sys.platform != 'win32', reason='Direct2D GPU comparison')
def test_multiple_sources_match_d2d_gpu(binary, multiple_sources):
    from test_subtitle_render_gpu_backend import _render_g1_frames
    from test_subtitle_skia_static_glow import assert_glow_parity

    main, style, extras = multiple_sources
    style = replace(style, viewport_offset_x=44, viewport_offset_y=-25)
    times = (249, 250, 1129, 1130, 1180, 1930, 2130, 2650)
    with NativeRendererProcess() as d2d:
        _, raw = _render_g1_frames(d2d, style, times, track=main, extra_tracks=extras, force_warp=False,
                                   width=1920, height=1080, realization_enabled=False)
    ref = [np.frombuffer(frame, np.uint8).reshape(1080, 1920, 4) for frame in raw]
    output = []
    for target in ('gpu', 'raster'):
        with SkiaRendererProcess(binary, selection=selection(target)) as process:
            process.configure(main, style, extra_tracks=extras, width=1920, height=1080, fps=60)
            frames, _ = collect(process, times)
            output.append(frames)
    assert_glow_parity(times, ref, *output, 'multiple_sources', check_p00_edges=True)


@pytest.fixture
def grid(project):
    track,style=project
    style=replace(style,dual_line_layout=True,line_alignments=['left','center','right'],
        line_offsets_x=[5,11,-7],line_horizontal_layout='asymmetric',smart_horizontal='none',
        line_gap_px=20,line_y_margin_px=40,horizontal_margin_px=30,
        allow_inter_page_line_overlap=True,auto_fill_section_time=False,
        line_lead_in_ms=400,line_tail_ms=300)
    lines=[TimingLine(chars=[TimingChar('字',1000+i*1000),TimingChar('A',1300+i*1000)],
                      end_ms=1800+i*1000) for i in range(8)]
    return replace(track,lines=lines),style


@pytest.mark.parametrize('semantics,top,center', [('legacy',125,185),('n3_1074',80,185)])
def test_fixed_grid_is_independent_of_line_contents(semantics,top,center):
    from krok_helper.subtitle_render.domain.models import Style
    for position,first in (('top',top),('center',center),('bottom',290)):
        style=Style(layout_semantics=semantics,dual_line_layout=True,line_alignments=['left']*3,
            line_horizontal_layout='center',font_size_px=50,line_y_position=position,
            line_y_margin_px=40,line_gap_px=20,stroke_width_px=0,stroke2_width_px=0,
            ruby_font_size_px=45,ruby_stroke_width_px=0)
        for lane in range(3):
            item=SimpleNamespace(layout_style=style,lane=lane,page_line_count=3,center_override=False)
            result=place_horizontal_line(item,{'ascent':40,'descent':10},width=640,height=480,
                ink_left=0,ink_right=100,layout_width=120)
            assert result.baseline_y==first+70*lane


@pytest.mark.parametrize('semantics',['legacy','n3_1074'])
def test_paging_and_display_windows_use_the_shared_resolver(binary,grid,semantics,tmp_path,monkeypatch):
    track,style=grid
    style=replace(style,layout_semantics=semantics,sync_entry=True,sync_ending=True,sync_each_page=True)
    from krok_helper.subtitle_render.engine.text import metrics as qt_metrics
    monkeypatch.setattr(qt_metrics,'QFontMetrics',lambda *a:pytest.fail('Qt font measurement'))
    monkeypatch.setattr(qt_metrics,'QPainterPath',lambda *a:pytest.fail('Qt path measurement'))
    expected=display_lines(track,style,SimpleNamespace(identity=('grid-timing',semantics)),logical_w=640,logical_h=480)
    ir=ir_for(binary,(track,style)) # ir_for's canvas is 640x360; timing has no geometry in this mode.
    actual=ir['track']['lines']
    assert len(actual)==len(expected)==8
    assert [(l['lane'],l['display_start_ms'],l['display_end_ms']) for l in actual]==[
        (l.lane,l.display_start_ms,l.display_end_ms) for l in expected]
    assert [l['lane'] for l in actual[:6]]==[0,1,2,0,1,2]
    cells=[l['cells'] for l in ir['skia_layout']['lines']]
    assert all(cells[i+1][0]['baseline_y']-cells[i][0]['baseline_y']==pytest.approx(70,abs=1e-4) for i in (0,1,3,4))
    if semantics=='n3_1074':
        (tmp_path/'grid-layout.json').write_text(json.dumps({'cells':cells,
            'schedule':[{k:l[k] for k in ('lane','page_line_count','display_start_ms','display_end_ms','resolved_intervals')}
                        for l in actual]},sort_keys=True),encoding='utf-8')


@pytest.mark.parametrize('position',['top','center','bottom'])
def test_persisted_short_pages_layout_override_and_blank_lines(binary,grid,position):
    track,style=grid
    style=replace(style,line_y_position=position,layouts=[LyricsLayout(layout_id='two',
        line_alignments=['left','right'],line_gap_px=31,line_offsets_x=[17,-19],smart_horizontal='none')])
    lines=[*track.lines[:3],TimingLine(chars=[],is_blank=True),
           *[replace(l,layout_index=1) for l in track.lines[3:5]]]
    plan=TrackPagePlan(sections=[TrackSection(pages=[TrackPage(3),TrackPage(2,'two')])])
    source=replace(track,lines=lines,page_plan=plan)
    ir=ir_for(binary,(source,style))
    rows=ir['skia_layout']['lines']
    assert rows[3]['cells']==[]
    assert rows[5]['cells'][0]['baseline_y']-rows[4]['cells'][0]['baseline_y']==pytest.approx(81,abs=1e-4)
    assert rows[2]['cells'][0]['baseline_y']-rows[1]['cells'][0]['baseline_y']==pytest.approx(70,abs=1e-4)


@pytest.mark.parametrize('mode',['asymmetric','per_row','center'])
@pytest.mark.parametrize('semantics',['legacy','n3_1074'])
def test_gpu_raster_grid_and_seek(binary,grid,semantics,mode):
    track,style=grid
    style=replace(style,layout_semantics=semantics,line_horizontal_layout=mode,
                  row1_offset_y=3,row2_offset_y=-5,row1_offset_x=15,row2_offset_x=-23)
    frames=[]
    for target in ('gpu','raster'):
        with SkiaRendererProcess(binary,selection=selection(target)) as process:
            process.configure(track,style,width=640,height=480,fps=60,dpr=1.5)
            output,_=collect(process,[1400,3400,6400,8400,1400])
            assert np.array_equal(output[0],output[4])
            assert all(p[...,3].max()>0 for p in output)
            premult=np.stack(output).astype(float);premult[...,:3]*=premult[...,3:]/255
            frames.append(premult)
    delta=np.abs(frames[0]-frames[1])
    assert delta.mean()<.15 and np.mean(np.max(delta,axis=-1)>16)<.003


@pytest.mark.parametrize('mutation',['smart','too_many'])
def test_unsupported_grid_settings_fail_without_replacing_scene(binary,grid,mutation):
    track,style=grid
    changes={'smart':{'smart_horizontal':'unknown'},'too_many':{'line_alignments':['left']*33}}[mutation]
    with SkiaRendererProcess(binary,selection=selection('raster')) as process:
        process.configure(track,style,width=640,height=360,fps=60)
        before,_=collect(process,[1400])
        with pytest.raises(NativeRendererError):process.configure(track,replace(style,**changes),width=640,height=360,fps=60)
        after,_=collect(process,[1400],generation=2)
        assert np.array_equal(before[0],after[0])


def test_old_sidecar_without_grid_capability_is_rejected(monkeypatch,tmp_path):
    executable=tmp_path/'old';executable.touch()
    ready={'ok':True,'backend':'skia','schema':3,'selection_version':1,'resolved_layout_schema':2,
        'profiles':['p05_resolved_horizontal','p05_single_line_placement','p05_inline_roles'],'execution_targets':['raster']}
    monkeypatch.setattr(NativeRendererProcess,'start',lambda self:ready)
    monkeypatch.setattr(SkiaRendererProcess,'abort',lambda *a,**k:None)
    with pytest.raises(NativeRendererError,match='多行网格'):SkiaRendererProcess(executable).start()


@pytest.mark.parametrize('lane',[-1,32,.5,'1',None])
def test_invalid_native_grid_lane_keeps_the_live_scene(binary,grid,lane):
    with SkiaRendererProcess(binary,selection=selection('raster')) as process:
        process.configure(*grid,width=640,height=360,fps=60)
        before,_=collect(process,[1400])
        ir=ir_for(binary,grid)
        ir['track']['lines'][0]['lane']=lane
        process._send({'cmd':'configure','ir':ir})
        with pytest.raises(NativeRendererError):process._expect_ok(process._read_until_event('configured'))
        after,_=collect(process,[1400],generation=2)
        assert np.array_equal(before[0],after[0])


def test_used_role_sizes_change_the_source_grid_consistently_across_pages(binary,grid,roles):
    track,style=grid
    _,role_style=roles
    style=replace(style,custom_style_schemes=role_style.custom_style_schemes,
                  font_family_latin=role_style.font_family_latin)
    source=replace(track,lines=[replace(l,chars=[replace(c,role_label='lead' if i%2 else None) for c in l.chars])
                                for i,l in enumerate(track.lines)])
    a=ir_for(binary,(source,style))
    b=ir_for(binary,(track,style))
    left=[l['cells'][0]['baseline_y'] for l in a['skia_layout']['lines']]
    right=[l['cells'][0]['baseline_y'] for l in b['skia_layout']['lines']]
    assert left[1]-left[0] == 111  # used role box 63+28, plus 20px row gap.
    assert right[1]-right[0] == 85  # used plain slots 45+20, plus 20px row gap.
    for index in range(6):
        assert left[index] == left[index%3]
        assert right[index] == right[index%3]
    with SkiaRendererProcess(binary,selection=selection('raster')) as process:
        process.configure(source,style,width=640,height=480,fps=60)
        frames,_=collect(process,[1400,3400])
        assert all(f[...,3].max()>0 for f in frames)


@pytest.mark.skipif(sys.platform!='win32',reason='Direct2D GPU comparison')
@pytest.mark.parametrize('semantics',['legacy','n3_1074'])
def test_full_and_short_page_grid_matches_d2d_gpu(binary,grid,semantics):
    from test_subtitle_render_gpu_backend import _render_g1_frames
    track,style=grid
    with NativeRendererProcess() as d2d, SkiaRendererProcess(binary,selection=selection('gpu')) as skia:
        for count in (3,2,1):
            source=replace(track,lines=[replace(l,display_start_override_ms=0,display_end_override_ms=15000)
                                      for l in track.lines[:count]])
            for position in ('top','center','bottom'):
                appearance=replace(style,layout_semantics=semantics,line_y_position=position)
                _,raw=_render_g1_frames(d2d,appearance,(10000,),force_warp=False,track=source,width=640,height=480)
                skia.configure(source,appearance,width=640,height=480,fps=60)
                output,_=collect(skia,[10000])
                reference=np.frombuffer(raw[0],np.uint8).reshape(480,640,4)
                def bands(p):
                    ys=np.flatnonzero((p[...,3]>127).any(axis=1))
                    parts=np.split(ys,np.flatnonzero(np.diff(ys)>1)+1)
                    boxes=[]
                    for part in parts:
                        if not len(part):continue
                        xs=np.flatnonzero((p[part[0]:part[-1]+1,:,3]>127).any(axis=0))
                        boxes.append((xs[0],part[0],xs[-1],part[-1]))
                    return boxes
                a,b=bands(reference),bands(output[0])
                assert len(a)==len(b)==count,(semantics,count,position,a,b)
                assert np.max(np.abs(np.array(a)-np.array(b)))<=1,(semantics,count,position,a,b)
