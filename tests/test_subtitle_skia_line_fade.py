"""Line-level entry/exit fades with the existing GPU's composed opacity."""
from dataclasses import replace
import copy
import json
import sys

import numpy as np
import pytest
from PyQt6.QtGui import QFontDatabase

from test_subtitle_skia_rtl_flow import application, binary, project, rtl_source, ir_for, collect, configure, selection
from test_subtitle_skia_protocol import test_actual_g5_preview_uses_skia_even_when_gpu_off as run_preview
from test_subtitle_skia_protocol import test_product_export_short_mp4_uses_skia as run_mp4
from test_subtitle_skia_secondary_outline import two_outlines
from test_subtitle_skia_vector_guides import guide_source
from test_subtitle_skia_vertical import vertical_ruby_source
from krok_helper.subtitle_render.native.backend import NativeRendererError, NativeRendererProcess
from krok_helper.subtitle_render.native.skia_backend import SkiaRendererProcess


def fade_project(project, case='plain'):
    if case == 'outlines':
        track, style = two_outlines(project, body_alpha=True, inner_alpha=True)
        style = replace(style, letter_spacing_px=-10)
    elif case == 'guide':
        track, style = guide_source(project)
    elif case == 'vertical_ruby':
        track, style = vertical_ruby_source(project, span=(1, 3), reading='\u5b57\u6f22', family='Meiryo')
    else:
        track, style = rtl_source(project)
    return track, replace(style, right_to_left=False, entry_anim='fade', entry_lead_ms=400,
                          exit_anim='fade', exit_fade_ms=600)


def premultiplied(frame):
    result = frame.astype(float)
    result[..., :3] *= result[..., 3:] / 255
    return result


@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_fade_scales_the_composed_outline_stack_and_keeps_timing(binary, project, target, tmp_path):
    track, style = fade_project(project, 'outlines')
    ir = ir_for(binary, (track, style), dpr=1.5)
    static = ir_for(binary, (track, replace(style, entry_anim='none', exit_anim='none')), dpr=1.5)
    line = ir['track']['lines'][0]
    assert line['resolved_intervals'] == static['track']['lines'][0]['resolved_intervals']
    assert 'p07_line_fade' in ir['required_features'] and ir['skia_layout']['line_fade_schema'] == 1
    assert ir['skia_layout']['lines'] == static['skia_layout']['lines']
    assert line['entry_duration_ms'] == 200 and line['exit_duration_ms'] == 600
    start, end = line['display_start_ms'], line['display_end_ms']
    times = [start, start + 50, start + 200, end - 300, end - 1, end]
    opacity = [0, 0.4375, 1, 0.25, 1 / 360000, 0]
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, times)
        configure(process, static)
        opaque, _ = collect(process, times, generation=2)
    assert opaque[1][..., 3].max() == 255
    for t, scale, actual, baseline in zip(times, opacity, frames, opaque, strict=True):
        delta = abs(premultiplied(actual) - premultiplied(baseline) * scale)
        assert delta.max() <= 2, (t, scale, delta.max())
    if target == 'gpu':
        (tmp_path / 'line-fade-layout.json').write_text(json.dumps({
            'schema': ir['skia_layout']['line_fade_schema'],
            'line': ir['skia_layout']['lines'][0],
            'intervals': line['resolved_intervals'],
            'window': [start, end],
            'durations': [line['entry_duration_ms'], line['exit_duration_ms']],
        }, sort_keys=True), encoding='utf-8')


@pytest.mark.parametrize('target', ['gpu', 'raster'])
@pytest.mark.parametrize('with_ruby', [False, True])
def test_vertical_decoration_fades_as_a_complete_layer(binary, project, target, with_ruby):
    from krok_helper.subtitle_render.domain.paint import PaintFill
    from test_subtitle_skia_roles import colors

    track, style = vertical_ruby_source(project, span=(1, 3))
    line = replace(track.lines[0], display_start_override_ms=0, display_end_override_ms=3000)
    track = replace(track, lines=[line], rubies=track.rubies if with_ruby else [])
    blank = colors('#00000000', '#00000000')
    before = replace(blank.before, shadow=PaintFill(color='#FFFF0000'))
    after = replace(blank.after, shadow=PaintFill(color='#FF0000FF'))
    style = replace(style, karaoke_colors=replace(blank, before=before, after=after),
                    ruby_karaoke_colors=replace(blank, before=after, after=after),
                    decoration_kind_before='shadow', decoration_kind='glow', decoration_before_sync=False,
                    shadow_before_offset_x=-36, shadow_before_offset_y=4,
                    glow_after_radius_px=8, ruby_decoration_kind='glow', ruby_decoration_kind_before='glow',
                    ruby_glow_before_radius_px=6, ruby_glow_after_radius_px=6,
                    entry_anim='fade', exit_anim='fade', entry_lead_ms=200, exit_fade_ms=400)
    ir = ir_for(binary, (track, style))
    opaque = ir_for(binary, (track, replace(style, entry_anim='none', exit_anim='none')))
    assert ir['skia_layout']['lines'] == opaque['skia_layout']['lines']
    feature = 'p07_vertical_decoration_fade'
    assert feature in ir['required_features'] and feature not in opaque['required_features']
    source, = ir['track']['lines']
    assert source['entry_duration_ms'] == 200 and source['exit_duration_ms'] == 400
    times = [50, 2700]
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        faded, _ = collect(process, times)
        bad = copy.deepcopy(ir)
        bad['required_features'].remove(feature)
        with pytest.raises(NativeRendererError):
            configure(process, bad)
        replay, _ = collect(process, times[::-1], generation=2)
        configure(process, opaque)
        full, _ = collect(process, times, generation=3)
    assert all(np.array_equal(a, b) for a, b in zip(faded, replay[::-1], strict=True))
    for time, opacity, actual, baseline in zip(times, (.4375, .5625), faded, full, strict=True):
        assert baseline[..., 3].max() > 16
        delta = abs(premultiplied(actual) - premultiplied(baseline) * opacity)
        pos = np.unravel_index(delta.argmax(), delta.shape)
        assert delta.max() <= 2, (time, pos, actual[pos[:2]], baseline[pos[:2]], delta.max())


@pytest.mark.parametrize('mutation', ['missing', 'schema', 'unsupported'])
def test_invalid_fade_profile_preserves_the_previous_scene(binary, project, mutation):
    ir = ir_for(binary, fade_project(project))
    bad = copy.deepcopy(ir)
    if mutation == 'missing':
        bad['required_features'].remove('p07_line_fade')
    elif mutation == 'schema':
        bad['skia_layout']['line_fade_schema'] = True
    else:
        bad['track']['lines'][0]['entry_anim'] = 'rise'
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        configure(process, ir)
        before, _ = collect(process, [300])
        with pytest.raises(NativeRendererError):
            configure(process, bad)
        after, _ = collect(process, [300], generation=2)
    assert np.array_equal(before[0], after[0])


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('case,dpr', [('plain', 1), ('outlines', 1.5), ('guide', 1), ('vertical_ruby', 1.5)])
def test_line_fade_matches_actual_gpu(binary, project, case, dpr):
    from test_subtitle_render_gpu_backend import _render_g1_frames

    if case == 'vertical_ruby' and 'Meiryo' not in QFontDatabase.families():
        pytest.skip('Meiryo is required for the actual Direct2D ruby allowance')
    track, style = fade_project(project, case)
    ir = ir_for(binary, (track, style), dpr=dpr)
    line = ir['track']['lines'][0]
    start, end = line['display_start_ms'], line['display_end_ms']
    times = [start, start+17, start+100, start+245, start+400, end-601, end-300, end-100, end-1, end]
    with NativeRendererProcess() as process:
        _, raw = _render_g1_frames(process, style, tuple(times), force_warp=False, track=track,
                                   width=640, height=360, dpr=dpr, realization_enabled=False)
    oracle = [np.frombuffer(p, np.uint8).reshape(round(360*dpr), round(640*dpr), 4) for p in raw]
    for target in ('gpu', 'raster'):
        with SkiaRendererProcess(binary, selection=selection(target)) as process:
            configure(process, ir)
            frames, _ = collect(process, times)
        for t, expected, actual in zip(times, oracle, frames, strict=True):
            assert abs(int(expected[..., 3].max()) - int(actual[..., 3].max())) <= 2, (t, case, target)
            delta = abs(premultiplied(expected) - premultiplied(actual))
            assert delta.mean() < .13, (t, case, target, delta.mean())
            assert np.mean(delta.max(axis=-1) > 16) < .003, (t, case, target)


def test_fade_g5_preview(binary, project, monkeypatch, tmp_path):
    run_preview(binary, monkeypatch, tmp_path, fade_project(project, 'outlines'), True, False)


def test_fade_raster_export(binary, project, monkeypatch, tmp_path):
    run_mp4(binary, monkeypatch, tmp_path, fade_project(project), False, False)
