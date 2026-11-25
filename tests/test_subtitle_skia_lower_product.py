"""Real lower-reading product IR, paints, preview/export and failure contracts."""
from dataclasses import replace
import copy
import json
import sys

import numpy as np
import pytest

from test_subtitle_skia_lower_geometry import application, binary, prepared_source, source
from test_subtitle_skia_lower_geometry import test_lower_geometry_matches_actual_d2d_gpu as run_d2d
from test_subtitle_skia_protocol import collect, selection
from test_subtitle_skia_protocol import test_actual_g5_preview_uses_skia_even_when_gpu_off as run_preview
from test_subtitle_skia_protocol import test_product_export_short_mp4_uses_skia as run_mp4
from test_subtitle_skia_roles import colors
from test_subtitle_skia_ruby_groups import configure
from krok_helper.subtitle_render.domain.models import SubtitleStyleScheme
from krok_helper.subtitle_render.domain.paint import PaintFill
from krok_helper.subtitle_render.engine.render.skia_ir import build_skia_render_ir
from krok_helper.subtitle_render.native.backend import NativeRendererError, NativeRendererProcess
from krok_helper.subtitle_render.native.protocol import apply_resolved_font_faces
from krok_helper.subtitle_render.native.skia_backend import SkiaRendererProcess
from krok_helper.subtitle_render.native.skia_text import SkiaTextMetricsProcess


def ir_for(binary, source, **kwargs):
    track, style, resolve = source
    with SkiaTextMetricsProcess(binary) as metrics:
        ir = build_skia_render_ir(track, style, measure=metrics.measure, resolve_font=resolve,
                                  width=900, height=600, fps=60, **kwargs)
    apply_resolved_font_faces(ir)
    return ir


@pytest.mark.parametrize('lower', [False, True], ids=['upper', 'lower'])
def test_reading_and_vector_guide_share_line_fade(binary, source, lower):
    from test_subtitle_skia_outline_ruby import render_skia
    from test_subtitle_skia_static_glow import assert_glow_parity
    from test_subtitle_skia_vector_guides import SQUARE

    track, style, resolve = source
    line, = track.lines
    chars = [replace(char, start_ms=char.start_ms + 1000) for char in line.chars]
    line = replace(line, chars=chars, end_ms=line.end_ms + 1000,
                   display_start_override_ms=0, display_end_override_ms=3500,
                   guide_symbol=SQUARE if lower else None,
                   inline_guide_symbols={} if lower else {1: SQUARE})
    rubies = [replace(ruby, pos_start_ms=ruby.pos_start_ms + 1000, pos_end_ms=ruby.pos_end_ms + 1000)
              for ruby in track.rubies]
    track = replace(track, lines=[line], rubies=rubies)
    style = replace(style, entry_anim='fade', entry_lead_ms=600, lower_ruby_enabled=lower,
                    ruby_karaoke_colors=colors('#FF00FF', '#FF00FF'))
    ir = ir_for(binary, (track, style, resolve))
    assert ir['track']['lines'][0]['entry_duration_ms'] == 600
    groups = ir['skia_layout']['lines'][0]['ruby_groups']
    assert any(bool(group.get('lower')) == lower for group in groups)
    times = (250, 650)
    gpu, raster = render_skia(binary, ir, times)
    assert 0 < gpu[0][..., 3].max() < gpu[1][..., 3].max()
    assert_glow_parity(times, None, gpu, raster, ('vector_guide_reading_fade', lower), check_p00_edges=True)


@pytest.fixture
def independent(source):
    track, style, resolve = source
    return track, replace(style, lower_ruby_follow_upper=False,
        lower_ruby_style={'ruby_colors_follow_main': False,
            'ruby_karaoke_colors': {'before': {'text': {'mode': 'solid', 'color': '#800000FF'}},
                                   'after': {'text': {'mode': 'solid', 'color': '#80FFFF00'}}}}), resolve


@pytest.fixture
def glowing_lower(independent):
    track, style, resolve = independent
    return track, replace(style, decoration_before_sync=False, lower_ruby_style={
        **style.lower_ruby_style, 'ruby_decoration_kind': 'glow',
        'ruby_glow_before_radius_px': 3, 'ruby_glow_after_radius_px': 7,
        'ruby_glow_before_concentration_level': 0, 'ruby_glow_concentration_level': 2,
        'ruby_karaoke_colors': {
            'before': {'text': {'color': '#800000FF'}, 'shadow': {'color': '#FF00FF'}},
            'after': {'text': {'color': '#80FFFF00'}, 'shadow': {'color': '#00FFFF'}},
        },
    }), resolve


@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_independent_lower_glow_tracks_its_own_phase(binary, glowing_lower, target):
    ir = ir_for(binary, glowing_lower)
    assert 'p06_lower_ruby_glow' in ir['required_features']
    groups = ir['skia_layout']['lines'][0]['ruby_groups']
    lower = [g for g in groups if g.get('lower')]
    assert len(lower) == 3 and 'lower_glow' not in groups[0]
    expected = {'before': {'radius': 3, 'color': [255, 0, 255, 255], 'concentration': 0},
                'after': {'radius': 7, 'color': [0, 255, 255, 255], 'concentration': 2}}
    assert all(g['lower_glow'] == expected for g in lower)
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        track, style, _ = glowing_lower
        process.configure(track, style, width=900, height=600, fps=60)
        frames, _ = collect(process, [199, 900, 1900, 199])
    assert np.array_equal(frames[0], frames[-1])
    for frame, channel in ((frames[0], 0), (frames[2], 1)):
        r, g, b, a = np.moveaxis(frame, -1, 0)
        near_glow = (a > 15) & (a < 115) & (b > 200)
        match = near_glow & ((r > 200) & (g < 20) if channel == 0 else (g > 200) & (r < 20))
        wrong_phase = near_glow & ((g > 200) & (r < 20) if channel == 0 else (r > 200) & (g < 20))
        assert np.count_nonzero(match) > 100 and not np.any(wrong_phase)


@pytest.mark.parametrize('mutation', ['radius', 'phase', 'upper'])
def test_invalid_lower_glow_keeps_previous_scene(binary, glowing_lower, mutation):
    valid = ir_for(binary, glowing_lower)
    bad = copy.deepcopy(valid)
    groups = bad['skia_layout']['lines'][0]['ruby_groups']
    glow = next(g['lower_glow'] for g in groups if g.get('lower'))
    if mutation == 'feature': bad['required_features'].remove('p06_lower_ruby_glow')
    elif mutation == 'schema': bad['skia_layout']['lower_ruby_glow_schema'] = 2
    elif mutation == 'radius': glow['before']['radius'] = float('nan')
    elif mutation == 'phase': glow['else'] = glow['before']
    elif mutation == 'upper': groups[0]['lower_glow'] = glow
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        configure(process, valid)
        before, _ = collect(process, [900])
        with pytest.raises(NativeRendererError): configure(process, bad)
        after, _ = collect(process, [900], generation=2)
    assert np.array_equal(before[0], after[0])


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
def test_lower_glow_matches_p00_actual_gpu(binary, application, tmp_path):
    from scripts.subtitle_gpu_baseline.cases import build_cases
    from test_subtitle_skia_character_fade import assert_ruby_transition_gpu_parity

    case = next(c for c in build_cases(tmp_path) if c.case_id == 'lower_ruby')
    assert_ruby_transition_gpu_parity(binary, case, tuple(sorted((*case.timestamps_ms, 3175, 3575, 4125))))


@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_lower_reading_line_band_preserves_reading_geometry(binary, source, tmp_path, target):
    from test_subtitle_skia_line_motion import static_band_image

    track, style, resolve = source
    plain = ir_for(binary, (track, style, resolve))
    style = replace(style, line_band_enabled=True, line_band_fill_mode='stretch',
                    line_band_image=str(static_band_image(tmp_path)), line_band_include_ruby=True)
    ir = ir_for(binary, (track, style, resolve))
    feature = 'p08_lower_ruby_line_band'
    assert feature in ir['required_features']
    assert len(ir['skia_layout']['lines']) == len(plain['skia_layout']['lines'])
    for line, normal in zip(ir['skia_layout']['lines'], plain['skia_layout']['lines'], strict=True):
        assert {key: value for key, value in line.items() if key != 'line_band_rect'} == normal
    t = ir['track']['lines'][0]['display_start_ms'] + 50
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, [t])
        replay, _ = collect(process, [t], generation=2)
    assert frames[0].any() and np.array_equal(frames[0], replay[0])


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('image', ['static', 'staff'])
def test_lower_reading_line_band_matches_actual_gpu(binary, application, tmp_path, image):
    from scripts.subtitle_gpu_baseline.cases import build_cases
    from test_subtitle_skia_character_fade import assert_ruby_transition_gpu_parity
    from test_subtitle_skia_line_motion import static_band_image

    base = next(c for c in build_cases(tmp_path) if c.case_id == 'lower_ruby')
    path = str(static_band_image(tmp_path)) if image == 'static' else 'preset:staff_scroll'
    style = replace(base.style, line_band_enabled=True, line_band_image=path, line_band_include_ruby=True)
    case = replace(base, case_id=f'lower_reading_line_band_{image}', style=style)
    assert_ruby_transition_gpu_parity(binary, case, (*case.timestamps_ms, 3175, 3575, 4125, 5499))


def lower_shadow_style(style, phase):
    return replace(style, lower_ruby_follow_upper=False, decoration_before_sync=False,
        lower_ruby_style={**style.lower_ruby_style,
            'ruby_decoration_kind_before': 'shadow' if phase == 'before' else 'glow',
            'ruby_decoration_kind': 'shadow' if phase == 'after' else 'glow',
            'ruby_glow_before_radius_px': 4, 'ruby_glow_after_radius_px': 6,
            'ruby_shadow_before_offset_x': -12, 'ruby_shadow_before_offset_y': 9,
            'ruby_shadow_offset_x': 10, 'ruby_shadow_offset_y': 12,
            'ruby_colors_follow_main': False,
            'ruby_karaoke_colors': {
                'before': {'text': {'color': '#1446D0'}, 'shadow': {'color': '#FF8000'}},
                'after': {'text': {'color': '#F0FF23'}, 'shadow': {'color': '#00FFFF'}},
            },
        })


@pytest.mark.parametrize('target', ['gpu', 'raster'])
@pytest.mark.parametrize('phase', ['before', 'after'])
def test_lower_shadow_keeps_its_own_wipe_phase_and_offset(binary, independent, target, phase):
    track, style, resolve = independent
    style = lower_shadow_style(style, phase)
    ir = ir_for(binary, (track, style, resolve), dpr=1.5)
    assert {'p06_lower_ruby_shadow', 'p06_lower_ruby_glow'} <= set(ir['required_features'])
    lower = [g for g in ir['skia_layout']['lines'][0]['ruby_groups'] if g.get('lower')]
    offset = [-12, 9] if phase == 'before' else [10, 12]
    color = [255, 128, 0, 255] if phase == 'before' else [0, 255, 255, 255]
    assert len(lower) == 3 and all(g['lower_shadow'] == {phase: {'color': color, 'offset': offset}} for g in lower)
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, [199, 900, 1900, 199])
    assert np.array_equal(frames[0], frames[-1])
    selected, other = (frames[0], frames[2]) if phase == 'before' else (frames[2], frames[0])
    def coverage(frame, paint):
        return (frame[..., 3] > 235) & np.all(frame[..., :3] == paint[:3], axis=-1)
    y, x = np.where(coverage(selected, color))
    fill_y, fill_x = np.where(coverage(selected, [20, 70, 208] if phase == 'before' else [240, 255, 35]))
    assert len(y) > 300 and len(fill_y) > 300 and not coverage(other, color).any()
    # A half-pixel shift can land the last fully covered row on either side.
    assert abs(y.max() - fill_y.max() - offset[1] * 1.5) <= .5
    # The before-only shadow's wipe clips its leading edge at the fixed front.
    assert abs(x.max() - fill_x.max() - offset[0] * 1.5) <= 1


def test_lower_shadow_cannot_replace_valid_scene_with_two_decorations_in_same_phase(binary, independent):
    track, style, resolve = independent
    valid = ir_for(binary, (track, lower_shadow_style(style, 'after'), resolve))
    bad = copy.deepcopy(valid)
    group = next(g for g in bad['skia_layout']['lines'][0]['ruby_groups'] if g.get('lower'))
    group['lower_shadow']['before'] = group['lower_shadow']['after']
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        configure(process, valid)
        before, _ = collect(process, [900])
        with pytest.raises(NativeRendererError): configure(process, bad)
        after, _ = collect(process, [900], generation=2)
    assert np.array_equal(before[0], after[0])


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('phase', ['before', 'after'])
def test_lower_shadow_and_glow_match_actual_gpu(binary, application, tmp_path, phase):
    from scripts.subtitle_gpu_baseline.cases import build_cases
    from test_subtitle_skia_character_fade import assert_ruby_transition_gpu_parity

    base = next(c for c in build_cases(tmp_path) if c.case_id == 'lower_ruby')
    case = replace(base, case_id=f'lower_shadow_{phase}', style=lower_shadow_style(base.style, phase))
    assert_ruby_transition_gpu_parity(binary, case, (600, 1450, 2600, 3175, 3575, 4125, 4400))


@pytest.mark.parametrize('target', ['gpu', 'raster'])
@pytest.mark.parametrize('shadow', [False, True])
@pytest.mark.parametrize('effect', ['utopia', 'none', 'no_wipe'])
def test_lower_decoration_keeps_its_clocks_under_line_animation(binary, glowing_lower, target, shadow, effect):
    track, style, resolve = glowing_lower
    track = replace(track, lines=[replace(track.lines[0], display_start_override_ms=50, display_end_override_ms=2400)])
    style = lower_shadow_style(style, 'after') if shadow else style
    style = replace(style, karaoke_anim=effect)
    animated = replace(style, entry_anim='slide_in', exit_anim='slide_out', entry_lead_ms=600, exit_fade_ms=600)
    ir = ir_for(binary, (track, animated, resolve))
    still = ir_for(binary, (track, style, resolve))
    feature = ('p07_lower_ruby_decoration_line_animation' if effect == 'utopia'
               else 'p07_lower_ruby_unscaled_line_animation')
    assert feature in ir['required_features'] and feature not in still['required_features']
    assert ir['skia_layout']['lines'] == still['skia_layout']['lines']
    bad = copy.deepcopy(ir)
    bad['required_features'].remove(feature)
    source, = ir['track']['lines']
    start, end = source['display_start_ms'], source['display_end_ms']
    entry_ms, exit_ms = source['entry_duration_ms'], source['exit_duration_ms']
    early, late = start + entry_ms // 2, end - exit_ms // 2
    middle = (start + entry_ms + end - exit_ms) // 2
    times = [start, early, middle, late, end]
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, [*times, early])
        replay, _ = collect(process, [late], generation=2)
        configure(process, still)
        static, _ = collect(process, times, generation=3)
    assert not frames[0].any() and not frames[-2].any()
    assert np.array_equal(frames[1], frames[-1]) and np.array_equal(frames[3], replay[0])
    assert np.array_equal(frames[2], static[2])
    for index in (1, 3):
        assert 0 < frames[index][..., 3].sum() < static[index][..., 3].sum()


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('effect', ['utopia', 'none', 'no_wipe'])
@pytest.mark.parametrize('entry,leave,shadow', [('slide_in', 'slide_out', None), ('rise', 'rise', 'before'),
                                               ('fade', 'fade', 'after')])
def test_lower_decoration_line_animation_matches_actual_gpu(binary, application, tmp_path, entry, leave, shadow, effect):
    from scripts.subtitle_gpu_baseline.cases import build_cases
    from test_subtitle_skia_character_fade import assert_ruby_transition_gpu_parity

    base = next(c for c in build_cases(tmp_path) if c.case_id == 'lower_ruby')
    line = replace(base.track.lines[0], display_start_override_ms=950, display_end_override_ms=4650)
    style = lower_shadow_style(base.style, shadow) if shadow else base.style
    case = replace(base, case_id=f'lower_decoration_{effect}_{entry}', track=replace(base.track, lines=[line]),
                   style=replace(style, karaoke_anim=effect, entry_anim=entry, exit_anim=leave))
    times = (950, 1120, 1260, 1380, 1550, 2600, 4110, 4280, 4450, 4530, 4610, 4650)
    if effect != 'utopia':
        times = tuple(sorted((*times, 951, 957, 967, 986, 4614, 4633, 4643, 4649)))
    assert_ruby_transition_gpu_parity(binary, case, times)


@pytest.mark.parametrize('target', ['gpu', 'raster'])
@pytest.mark.parametrize('shadow', [False, True])
def test_lower_decoration_no_wipe_switches_only_at_clock_boundaries(binary, glowing_lower, target, shadow):
    track, style, resolve = glowing_lower
    style = lower_shadow_style(style, 'after') if shadow else style
    ir = ir_for(binary, (track, replace(style, karaoke_anim='no_wipe'), resolve))
    final, = ir['skia_layout']['lines']
    source, = ir['track']['lines']
    feature = 'p07_lower_ruby_no_wipe_decoration'
    assert feature in ir['required_features']
    intervals = [interval for interval in source['resolved_intervals']]
    intervals += [unit['interval_ms'] for group in final['ruby_groups'] for unit in group['units']]
    boundaries = sorted({source['display_start_ms'], source['display_end_ms'],
                         *(time for interval in intervals for time in interval),
                         *(knot['time_ms'] for cell in final['cells'] for knot in cell.get('wipe_points', ()))})
    times = [time for start, end in zip(boundaries, boundaries[1:]) if end - start > 2
             for time in (start + 1, end - 1)]
    bad = copy.deepcopy(ir)
    bad['required_features'].remove(feature)
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, times)
        replay, _ = collect(process, [times[-1], times[0]], generation=2)
    assert all(np.array_equal(a, b) for a, b in zip(frames[::2], frames[1::2], strict=True))
    assert not np.array_equal(frames[0], frames[-1])
    assert np.array_equal(frames[-1], replay[0]) and np.array_equal(frames[0], replay[1])


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('shadow', [None, 'after'])
def test_lower_decoration_no_wipe_matches_actual_gpu(binary, application, tmp_path, shadow):
    from scripts.subtitle_gpu_baseline.cases import build_cases
    from test_subtitle_skia_character_fade import assert_ruby_transition_gpu_parity

    base = next(c for c in build_cases(tmp_path) if c.case_id == 'lower_ruby')
    style = lower_shadow_style(base.style, shadow) if shadow else base.style
    case = replace(base, case_id=f'lower_decoration_no_wipe_{shadow}', style=replace(style, karaoke_anim='no_wipe'))
    assert_ruby_transition_gpu_parity(binary, case, (600, 1450, 1750, 2050, 2600, 3175, 3575, 4125, 4400, 4500, 4501))


@pytest.mark.parametrize('target', ['gpu', 'raster'])
@pytest.mark.parametrize('karaoke', ['none', 'utopia'])
@pytest.mark.parametrize('animated', [False, True])
def test_lower_scanline_follows_the_romanized_reading_clock(binary, glowing_lower, target, karaoke, animated):
    track, style, resolve = glowing_lower
    style = replace(style, karaoke_anim=karaoke)
    if animated:
        style = replace(style, entry_anim='fade', exit_anim='fade', entry_lead_ms=600, exit_fade_ms=600)
    base = ir_for(binary, (track, style, resolve))
    started = replace(style, scanline_enabled=True, scanline_forward_enabled=True,
                      scanline_mode='color', scanline_color='#FFFFFF', scanline_width_px=18, scanline_glow_px=6)
    ir = ir_for(binary, (track, started, resolve))
    feature = 'p07_lower_ruby_scanline_line_animation' if animated else 'p07_lower_ruby_scanline'
    assert feature in ir['required_features'] and feature not in base['required_features']
    assert ir['skia_layout']['lines'] == base['skia_layout']['lines']
    groups = ir['skia_layout']['lines'][0]['ruby_groups']
    units = [unit for group in groups if group.get('lower') for unit in group['units']]
    top = int(min(unit['baseline_y'] + unit['metrics']['ink'][1] for unit in units))
    samples = {t for unit in units for t in [sum(unit['interval_ms']) // 2]}
    times = sorted({199, 2200, *samples})
    bad = copy.deepcopy(ir)
    bad['required_features'].remove(feature)
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, times)
        replay, _ = collect(process, list(reversed(times)), generation=2)
        configure(process, base)
        plain, _ = collect(process, times, generation=3)
    assert all(np.array_equal(a, b) for a, b in zip(frames, reversed(replay), strict=True))
    assert np.array_equal(frames[0], plain[0]) and np.array_equal(frames[-1], plain[-1])
    assert sum(not np.array_equal(a[top:], b[top:]) for a, b in zip(frames, plain, strict=True)) >= 2


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('karaoke', ['none', 'utopia'])
def test_lower_scanline_matches_actual_gpu(binary, application, tmp_path, karaoke):
    from scripts.subtitle_gpu_baseline.cases import build_cases
    from test_subtitle_skia_character_fade import assert_ruby_transition_gpu_parity

    base = next(c for c in build_cases(tmp_path) if c.case_id == 'lower_ruby')
    style = replace(base.style, karaoke_anim=karaoke, scanline_enabled=True, scanline_forward_enabled=True,
                    scanline_mode='color', scanline_color='#FFFFAA', scanline_width_px=22, scanline_glow_px=6)
    case = replace(base, case_id=f'lower_scanline_{karaoke}', style=style)
    assert_ruby_transition_gpu_parity(binary, case, (600, 1450, 1750, 2050, 2600, 3175, 3575, 4125, 4400, 4550))


def test_reverse_flame_scanline_coexists_with_readings_on_another_row(binary, source):
    from test_subtitle_skia_outline_ruby import render_skia
    from test_subtitle_skia_static_glow import assert_glow_parity

    track, style, resolve = source
    first, = track.lines
    shift = 4000
    second = replace(first, track_line_index=1, wipe_reverse=True,
                     chars=[replace(char, start_ms=char.start_ms + shift) for char in first.chars],
                     end_ms=first.end_ms + shift, display_start_override_ms=shift,
                     display_end_override_ms=first.end_ms + shift + 600)
    track = replace(track, lines=[first, second])
    style = replace(style, scanline_enabled=True, scanline_forward_enabled=False, scanline_reverse_enabled=True,
                    scanline_mode='default', scanline_shape='flame', scanline_width_px=25)
    times = (500, 950, 4500, 5080, 5740)
    active = ir_for(binary, (track, style, resolve))
    base = ir_for(binary, (track, replace(style, scanline_enabled=False), resolve))
    first_final, second_final = active['skia_layout']['lines']
    assert any(group.get('lower') for group in first_final['ruby_groups']) and not second_final['ruby_groups']
    assert 'p07_reverse_procedural_scanline' in active['required_features']
    assert 'p07_ruby_scanline' not in active['required_features']
    reference = None
    if sys.platform == 'win32':
        from test_subtitle_render_gpu_backend import _render_g1_frames

        with NativeRendererProcess() as process:
            _, raw = _render_g1_frames(process, style, times, force_warp=False, track=track,
                                       width=900, height=600, realization_enabled=False)
        reference = [np.frombuffer(frame, np.uint8).reshape(600, 900, 4) for frame in raw]
    gpu, raster = render_skia(binary, active, times)
    assert_glow_parity(times, reference, gpu, raster, 'separate_reverse_flame_and_readings', check_p00_edges=True)
    still, _ = render_skia(binary, base, times)
    assert np.array_equal(gpu[:2], still[:2])
    assert any(np.count_nonzero(a != b) > 100 for a, b in zip(gpu[2:], still[2:], strict=True))


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('karaoke,entry,exit_,mode,shadow,dpr', [
    (karaoke, entry, exit_, mode, shadow, 1)
    for karaoke in ('none', 'utopia')
    for entry, exit_, mode, shadow in (
        ('fade', 'fade', 'color', None),
        ('slide_in', 'slide_out', 'follow_before', 'before'),
        ('rise', 'rise', 'brighten', 'after'),
    )
] + [
    ('none', 'fade', 'fade', 'color', None, 1.5),
    ('utopia', 'rise', 'rise', 'brighten', 'after', 1.5),
])
def test_lower_scanline_line_motion_matches_actual_gpu(binary, application, tmp_path, karaoke, entry, exit_, mode,
                                                       shadow, dpr):
    from scripts.subtitle_gpu_baseline.cases import build_cases
    from test_subtitle_skia_character_fade import assert_ruby_transition_gpu_parity

    base = next(c for c in build_cases(tmp_path) if c.case_id == 'lower_ruby')
    line = replace(base.track.lines[0], display_start_override_ms=950, display_end_override_ms=4650)
    style = lower_shadow_style(base.style, shadow) if shadow else base.style
    style = replace(style, karaoke_anim=karaoke, scanline_enabled=True, scanline_forward_enabled=True,
                    scanline_mode=mode, scanline_color='#FFFFAA', scanline_width_px=22, scanline_glow_px=6,
                    entry_anim=entry, exit_anim=exit_)
    case = replace(base, case_id=f'lower_scanline_motion_{karaoke}_{mode}', track=replace(base.track, lines=[line]),
                   style=style)
    times = (950, 951, 957, 967, 986, 1120, 1260, 1380, 1550, 2600,
             4110, 4280, 4450, 4530, 4610, 4614, 4633, 4643, 4649, 4650)
    assert_ruby_transition_gpu_parity(binary, case, times, dpr=dpr)


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
def test_lower_gradient_scanline_with_singing_particles_matches_actual_gpu(binary, application, tmp_path):
    from scripts.subtitle_gpu_baseline.cases import build_cases
    from test_subtitle_skia_character_fade import assert_ruby_transition_gpu_parity
    from krok_helper.subtitle_render.native.skia_scene import prepare_skia_scene_ir

    base = next(c for c in build_cases(tmp_path) if c.case_id == 'lower_ruby')
    line = replace(base.track.lines[0], display_start_override_ms=950, display_end_override_ms=4650)
    style = replace(base.style, karaoke_anim='utopia', entry_anim='fade', exit_anim='fade',
                    scanline_enabled=True, scanline_forward_enabled=True, scanline_mode='default',
                    scanline_effect='none', sing_fx='twinkle', lower_ruby_follow_upper=True,
                    ruby_colors_follow_main=True)
    ramp = PaintFill(mode='gradient_vertical', start_color='#FFF0A030', end_color='#FF4060E0')
    colors = style.karaoke_colors
    style = replace(style, karaoke_colors=replace(colors, before=replace(colors.before, text=ramp)))
    case = replace(base, track=replace(base.track, lines=[line]), style=style)
    valid = prepare_skia_scene_ir(case.track, style, executable=binary, width=1920, height=1080, fps=60)
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        configure(process, valid)
        assert 'p06_lower_ruby_fill_gradient' in valid['required_features']
        invalid = copy.deepcopy(valid)
        group = next(group for group in invalid['skia_layout']['lines'][0]['ruby_groups']
                     if 'fill_gradient' in group)
        group['fill_gradient']['before'][0][0] = -1
        frames, _ = collect(process, [1550, 2600])
        with pytest.raises(NativeRendererError, match='色标'):
            configure(process, invalid)
        replay, _ = collect(process, [2600, 1550], generation=2)
        assert all(np.array_equal(a, b) for a, b in zip(frames, reversed(replay), strict=True))
    assert_ruby_transition_gpu_parity(binary, case, (951, 1120, 1260, 1380, 1550, 2600, 4110, 4450, 4530, 4649))


@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_product_lower_preserves_source_and_uses_independent_alpha_paints(binary, independent, target, monkeypatch, tmp_path):
    from krok_helper.subtitle_render.engine.ruby import lower, style as qt_ruby
    from krok_helper.subtitle_render.engine.render.elements.horizontal import layout
    fail = lambda *a, **k: pytest.fail('legacy lower font/baseline/derivation invoked')
    for module, names in [(lower, ['lower_rubies_for_line', 'lower_style_snapshot']),
            (qt_ruby, ['build_ruby_font_for_text']),
            (layout, ['resolve_display_baselines', 'lower_ruby_baseline_offset'])]:
        for name in names: monkeypatch.setattr(module, name, fail)
    ir = ir_for(binary, independent)
    assert len(ir['track']['rubies']) == 4 and not ir['track']['rubies'][0].get('lower')
    assert sum(r.get('lower', False) for r in ir['track']['rubies']) == 3
    assert 'p05_lower_ruby' in ir['required_features'] and ir['skia_layout']['lower_ruby_schema'] == 1
    assert all('annotation_baseline_y' not in l for l in ir['track']['lines'])
    groups = ir['skia_layout']['lines'][0]['ruby_groups']
    lowers = [g for g in groups if g.get('lower')]
    assert [g['source_index'] for g in lowers] == [1, 2, 3]
    assert all(g['paint'] == {'before': [0, 0, 255, 128], 'after': [255, 255, 0, 128]} for g in lowers)
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        track, style, _ = independent
        process.configure(track, style, width=900, height=600, fps=60)
        frames, _ = collect(process, [199, 350, 400, 450, 800, 1900, 400])
    assert np.array_equal(frames[1], frames[2]) and np.array_equal(frames[2], frames[-1])
    for frame, color in [(frames[0], [0, 0, 255]), (frames[-2], [255, 255, 0])]:
        mask = np.all(frame[..., :3] == color, axis=-1) & (frame[..., 3] >= 120)
        assert mask.sum() > 200 and frame[..., 3][mask].max() == 128
    if target == 'gpu':
        (tmp_path/'lower-ruby-product.json').write_text(json.dumps({
            'sources': ir['track']['rubies'], 'layout': ir['skia_layout']['lines'],
            'features': ir['required_features']}, sort_keys=True), encoding='utf-8')


@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_product_lower_source_style_roll_offsets_and_dpr(binary, source, target):
    track, style, _ = source
    result = []
    for shift in [False, True]:
        with SkiaRendererProcess(binary, selection=selection(target)) as process:
            process.configure(replace(track, meta=replace(track.meta, offset_ms=137)) if shift else track,
                replace(style, timing_offset_ms=229) if shift else style,
                width=900, height=600, fps=60, dpr=1.5, roll_shift_ms=400 if shift else 0)
            frames, _ = collect(process, [t+(766 if shift else 0) for t in [199,350,400,450,699,800,1200,1900]])
        result.append(frames)
    assert all(np.array_equal(a,b) for a,b in zip(*result, strict=True))


def test_kana_only_lower_and_unused_lower_source_are_valid(binary, source):
    track, style, resolve = source
    track = replace(track, rubies=[])
    ir = ir_for(binary, (track, style, resolve))
    assert ir['track']['rubies'] and all(r['lower'] for r in ir['track']['rubies'])
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        configure(process, ir); frames, _ = collect(process, [450])
        assert frames[0].any()
    # Lower-enabled ASCII produces no generated sources but keeps the new
    # capability handshake; absence of romanizable text is not an error.
    line = replace(track.lines[0], chars=[replace(c, text='A') for c in track.lines[0].chars])
    ir = ir_for(binary, (replace(track, lines=[line]), style, resolve))
    assert not ir['track']['rubies']
    with SkiaRendererProcess(binary, selection=selection('raster')) as process: configure(process, ir)


def test_role_lower_paint_and_two_line_source_ownership(binary, source):
    track, style, resolve = source
    style = replace(style, custom_style_schemes={'lead': SubtitleStyleScheme(
        ruby_colors_follow_main=False, ruby_karaoke_colors=colors('#0000FF', '#FFFF00'))})
    line = replace(track.lines[0], chars=[replace(c, role_label='lead') for c in track.lines[0].chars], track_line_index=1)
    ir = ir_for(binary, (replace(track, lines=[track.lines[0], line]), style, resolve))
    groups = ir['skia_layout']['lines'][1]['ruby_groups']
    assert all(g['paint'] == {'before': [0,0,255,255], 'after': [255,255,0,255]} for g in groups if g.get('lower'))
    assert all(r['lower_line_index'] in [0,1] for r in ir['track']['rubies'] if r.get('lower'))
    with SkiaRendererProcess(binary, selection=selection('raster')) as process: configure(process, ir)


@pytest.mark.parametrize('mutation', ['source_missing', 'source_duplicate', 'source_line', 'source_target', 'source_reading', 'source_clock', 'paint_missing', 'paint_size', 'paint_nan', 'paint_fraction', 'paint_bool', 'paint_range', 'lower_bool', 'upper_paint'])
def test_invalid_lower_contract_keeps_previous_scene(binary, independent, mutation):
    valid = ir_for(binary, independent)
    ir = copy.deepcopy(valid)
    groups = ir['skia_layout']['lines'][0]['ruby_groups']
    group = next(g for g in groups if g.get('lower'))
    source = ir['track']['rubies'][group['source_index']]
    if mutation == 'feature': ir['required_features'].remove('p05_lower_ruby')
    elif mutation == 'schema': ir['skia_layout']['lower_ruby_schema'] = 2
    elif mutation == 'overlap': ir['required_features'].remove('p05_overlap_wipe')
    elif mutation == 'source_missing': groups.remove(group)
    elif mutation == 'source_duplicate': groups.append(copy.deepcopy(group))
    elif mutation == 'source_line': source['lower_line_index'] = 1
    elif mutation == 'source_target': source['target_char_end'] = 99
    elif mutation == 'source_reading': source['reading'] += 'A'
    elif mutation == 'source_clock': group['units'][0]['interval_ms'] = [-100, 200]
    elif mutation == 'paint_missing': del group['paint']
    elif mutation == 'paint_size': group['paint']['before'] = [0,0,0]
    elif mutation == 'paint_nan': group['paint']['before'][0] = float('nan')
    elif mutation == 'paint_fraction': group['paint']['before'][0] = .5
    elif mutation == 'paint_bool': group['paint']['before'][0] = False
    elif mutation == 'paint_range': group['paint']['after'][3] = 256
    elif mutation == 'lower_bool': group['lower'] = 'true'
    elif mutation == 'upper_paint': groups[0]['paint'] = group['paint']
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        configure(process, valid); before, _ = collect(process, [400])
        with pytest.raises(NativeRendererError): configure(process, ir)
        after, _ = collect(process, [400], generation=2)
    assert np.array_equal(before[0], after[0])


def lower_gradient_shadow_style(style):
    style = lower_shadow_style(style, 'after')
    lower = style.lower_ruby_style
    colors = lower['ruby_karaoke_colors']
    return replace(style, lower_ruby_style={
        **lower, 'ruby_decoration_kind_before': 'shadow', 'ruby_horizontal_gradient_with_main': False,
        'ruby_karaoke_colors': {
            'before': {**colors['before'], 'shadow': {'mode': 'gradient_horizontal',
                                                      'start_color': '#9EFA4040', 'end_color': '#E02030E0'}},
            'after': {**colors['after'], 'shadow': {'mode': 'gradient_vertical',
                                                    'start_color': '#FF40C090', 'end_color': '#A0202050'}},
        },
    })


def lower_image_shadow_style(style, path):
    style = lower_shadow_style(style, 'after')
    lower = style.lower_ruby_style
    palette = lower['ruby_karaoke_colors']
    return replace(style, lower_ruby_style={
        **lower, 'ruby_decoration_kind_before': 'shadow',
        'ruby_karaoke_colors': {
            phase: {**palette[phase], 'shadow': {'mode': 'image', 'image_path': str(path),
                                                 'image_scale_pct': scale, 'color': '#CF3039A0'}}
            for phase, scale in (('before', 137), ('after', 109))
        },
    })


def test_lower_image_shadow_validates_each_paint_before_replacing_scene(binary, independent, tmp_path):
    from test_subtitle_skia_image_fills import pattern, write_png

    track, style, resolve = independent
    style = lower_image_shadow_style(style, write_png(tmp_path / 'shadow.png', pattern('transparent')))
    ir = ir_for(binary, (track, style, resolve))
    invalid = copy.deepcopy(ir)
    group = next(group for group in invalid['skia_layout']['lines'][0]['ruby_groups'] if group.get('lower'))
    group['lower_shadow_image']['before']['scale_pct'] = 0
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        configure(process, ir)
        before, _ = collect(process, [400, 950])
        for rejected in (invalid,):
            with pytest.raises(NativeRendererError, match='下方注音图片'):
                configure(process, rejected)
        after, _ = collect(process, [950, 400], generation=2)
    assert all(frame.any() for frame in before) and np.array_equal(before, after[::-1])


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
def test_lower_image_shadow_matches_actual_d2d_gpu(binary, application, tmp_path):
    from scripts.subtitle_gpu_baseline.cases import build_cases
    from test_subtitle_render_gpu_backend import _render_g1_frames
    from test_subtitle_skia_character_fade import assert_ruby_transition_gpu_parity
    from test_subtitle_skia_image_fills import pattern, write_png

    base = next(case for case in build_cases(tmp_path / 'assets') if case.case_id == 'lower_ruby')
    style = replace(base.style, entry_anim='none', exit_anim='none', karaoke_anim='none')
    tile = write_png(tmp_path / 'shadow.png', pattern('transparent'))
    case = replace(base, style=lower_image_shadow_style(style, tile))
    times = (1550, 2600, 4110, 4280)
    references = []
    for path in (tile, tmp_path / 'missing.png'):
        with NativeRendererProcess() as process:
            _, raw = _render_g1_frames(process, lower_image_shadow_style(style, path), times,
                                       force_warp=False, track=case.track, realization_enabled=False)
        references.append(raw)
    assert any(a != b for a, b in zip(*references, strict=True))
    assert_ruby_transition_gpu_parity(binary, case, times)


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
def test_multitrack_lower_image_shadow_matches_actual_d2d_gpu(binary, application, tmp_path):
    from scripts.subtitle_gpu_baseline.cases import build_cases
    from test_subtitle_render_gpu_backend import _render_g1_frames
    from test_subtitle_skia_image_fills import pattern, write_png
    from test_subtitle_skia_static_glow import assert_glow_parity

    case = next(case for case in build_cases(tmp_path / 'assets') if case.case_id == 'lower_ruby')
    track = case.track
    other = replace(track, meta=replace(track.meta, offset_ms=900))
    style = replace(case.style, entry_anim='none', exit_anim='none', karaoke_anim='none')
    tile = write_png(tmp_path / 'shadow.png', pattern('transparent'))
    viewport = dict(width=1920, height=1080)
    times = (299, 1550, 2600, 4110, 5150, 5750, 6399)
    references = []
    for path in (tile, tmp_path / 'missing.png'):
        with NativeRendererProcess() as process:
            _, raw = _render_g1_frames(process, lower_image_shadow_style(style, path), times,
                                       track=track, extra_tracks=[other], force_warp=False,
                                       realization_enabled=False, **viewport)
        references.append([np.frombuffer(frame, np.uint8).reshape(1080, 1920, 4) for frame in raw])
    assert any(np.count_nonzero(a != b) > 100 for a, b in zip(*references, strict=True))
    output = []
    for target in ('gpu', 'raster'):
        with SkiaRendererProcess(binary, selection=selection(target)) as process:
            process.configure(track, lower_image_shadow_style(style, tile), extra_tracks=[other], fps=60, **viewport)
            frames, _ = collect(process, times)
        output.append(frames)
    assert_glow_parity(times, references[0], *output, 'multitrack_lower_image_shadow', check_p00_edges=True)


def test_lower_gradient_shadow_rejects_forged_paint_without_replacing_scene(binary, independent):
    track, style, resolve = independent
    ir = ir_for(binary, (track, lower_gradient_shadow_style(style), resolve))
    bad = copy.deepcopy(ir)
    bad['required_features'].remove('p06_lower_ruby_shadow_gradient')
    invalid = copy.deepcopy(ir)
    group = next(group for group in invalid['skia_layout']['lines'][0]['ruby_groups'] if group.get('lower'))
    group['lower_shadow_gradient']['before']['stops'][0][0] = -1
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        configure(process, ir)
        before, _ = collect(process, [400, 950])
        for rejected in (bad, invalid):
            with pytest.raises(NativeRendererError, match='下方注音渐变'):
                configure(process, rejected)
        after, _ = collect(process, [950, 400], generation=2)
    assert np.array_equal(before, after[::-1])


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('gradient', [False, True], ids=['solid', 'gradient'])
def test_lower_shadow_offsets_match_actual_d2d_gpu(binary, application, tmp_path, gradient):
    from scripts.subtitle_gpu_baseline.cases import build_cases
    from test_subtitle_skia_character_fade import assert_ruby_transition_gpu_parity

    case = next(c for c in build_cases(tmp_path) if c.case_id == 'lower_ruby')
    style = replace(case.style, entry_anim='none', exit_anim='none', karaoke_anim='none')
    if gradient:
        style = lower_gradient_shadow_style(style)
    else:
        style = lower_shadow_style(style, 'after')
        style = replace(style, lower_ruby_style={**style.lower_ruby_style, 'ruby_decoration_kind_before': 'shadow'})
    assert_ruby_transition_gpu_parity(binary, replace(case, style=style), (1550, 2600, 4110, 4280))


def lower_gradient_style(style, shared):
    return replace(style, lower_ruby_follow_upper=False, lower_ruby_style={
        'ruby_colors_follow_main': False,
        'ruby_horizontal_gradient_with_main': shared,
        'ruby_karaoke_colors': {
            'before': {'text': {'mode': 'gradient_horizontal', 'start_color': '#0040E8', 'end_color': '#EE6040'}},
            'after': {'text': {'mode': 'split_vertical', 'start_color': '#FFA010', 'end_color': '#6040FF'}},
        },
    })


def lower_image_style(style, path):
    return replace(style, lower_ruby_follow_upper=False, lower_ruby_style={
        'ruby_colors_follow_main': False,
        'ruby_karaoke_colors': {
            'before': {'text': {'mode': 'image', 'image_path': str(path),
                                'image_scale_pct': 138, 'color': '#FFD06030'}},
            'after': {'text': {'mode': 'solid', 'color': '#FF70CCF0'}},
        },
    })


def test_lower_reading_image_rejects_invalid_phase_and_preserves_scene(binary, independent, tmp_path):
    from test_subtitle_skia_image_fills import pattern, write_png

    track, style, resolve = independent
    tile = write_png(tmp_path / 'ローマ音.png', pattern('transparent'))
    ir = ir_for(binary, (track, lower_image_style(style, tile), resolve))
    missing = copy.deepcopy(ir)
    missing['required_features'].remove('p06_lower_ruby_static_image')
    invalid = copy.deepcopy(ir)
    group = next(g for g in invalid['skia_layout']['lines'][0]['ruby_groups'] if g.get('lower'))
    group['static_fill_image']['before']['scale_pct'] = 0
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        configure(process, ir)
        frames, _ = collect(process, [450, 950])
        with pytest.raises(NativeRendererError, match='下方注音图片画笔|参数'):
            configure(process, invalid)
        replay, _ = collect(process, [950, 450], generation=2)
    assert all(frame.any() for frame in frames) and np.array_equal(frames, replay[::-1])


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
def test_lower_reading_image_matches_actual_d2d_gpu(binary, application, tmp_path):
    from scripts.subtitle_gpu_baseline.cases import build_cases
    from test_subtitle_skia_character_fade import assert_ruby_transition_gpu_parity
    from test_subtitle_skia_image_fills import pattern, write_png
    from test_subtitle_render_gpu_backend import _render_g1_frames

    case = next(c for c in build_cases(tmp_path / 'assets') if c.case_id == 'lower_ruby')
    style = replace(case.style, entry_anim='none', exit_anim='none', karaoke_anim='none')
    tile = write_png(tmp_path / 'ローマ音.png', pattern('transparent'))
    textured = lower_image_style(style, tile)
    control = lower_image_style(style, tmp_path / 'missing.png')
    times = (1550, 2600, 4110, 4280)
    reference = []
    for look in (textured, control):
        with NativeRendererProcess() as process:
            _, raw = _render_g1_frames(process, look, times, force_warp=False, track=case.track,
                                       realization_enabled=False)
        reference.append(raw)
    assert any(left != right for left, right in zip(*reference, strict=True))
    assert_ruby_transition_gpu_parity(binary, replace(case, style=textured), times)


def test_lower_reading_gradient_keeps_existing_scene_after_invalid_paint(binary, independent):
    track, style, resolve = independent
    valid = ir_for(binary, (track, lower_gradient_style(style, True), resolve))
    feature = 'p06_lower_ruby_static_gradient'
    assert feature in valid['required_features']
    missing = copy.deepcopy(valid)
    missing['required_features'].remove(feature)
    invalid = copy.deepcopy(valid)
    group = next(group for group in invalid['skia_layout']['lines'][0]['ruby_groups'] if group.get('lower'))
    group['static_fill_gradient']['before']['stops'][0][0] = -1
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        configure(process, valid)
        expected, _ = collect(process, [400, 950])
        with pytest.raises(NativeRendererError, match='下方注音渐变'):
            configure(process, invalid)
        frames, _ = collect(process, [950, 400], generation=2)
    assert np.array_equal(expected, frames[::-1])
    assert all(frame.any() for frame in frames)


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('shared', [False, True], ids=['local', 'body'])
def test_lower_reading_gradient_matches_actual_d2d_gpu(binary, application, tmp_path, shared):
    from scripts.subtitle_gpu_baseline.cases import build_cases
    from test_subtitle_skia_character_fade import assert_ruby_transition_gpu_parity

    case = next(c for c in build_cases(tmp_path) if c.case_id == 'lower_ruby')
    style = lower_gradient_style(replace(case.style, entry_anim='none', exit_anim='none', karaoke_anim='none'), shared)
    case = replace(case, case_id=f'lower_reading_gradient_{shared}', style=style)
    assert_ruby_transition_gpu_parity(binary, case, (1550, 2600, 4110, 4280))


def test_rtl_lower_reading_preserves_direction_and_scene(binary, independent):
    track, style, resolve = independent
    ir = ir_for(binary, (track, replace(style, right_to_left=True), resolve))
    bad = copy.deepcopy(ir)
    bad['required_features'].remove('p05_rtl_lower_ruby')
    groups = ir['skia_layout']['lines'][0]['ruby_groups']
    assert any(group.get('lower') for group in groups)
    reading = next(group for group in groups if group.get('lower') and len(group['units']) > 1)
    assert reading['units'][0]['origin_x'] > reading['units'][-1]['origin_x']
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        configure(process, ir)
        frames, _ = collect(process, [400, 950])
        replay, _ = collect(process, [950, 400], generation=2)
    assert np.array_equal(frames, replay[::-1]) and all(frame.any() for frame in frames)


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
def test_rtl_lower_reading_matches_actual_d2d_gpu(binary, application, tmp_path):
    from scripts.subtitle_gpu_baseline.cases import build_cases
    from test_subtitle_skia_character_fade import assert_ruby_transition_gpu_parity

    case = next(c for c in build_cases(tmp_path) if c.case_id == 'lower_ruby')
    style = replace(case.style, right_to_left=True, entry_anim='none', exit_anim='none', karaoke_anim='none',
                    decoration_kind='none', decoration_kind_before='none',
                    ruby_decoration_kind='none', ruby_decoration_kind_before='none')
    assert_ruby_transition_gpu_parity(binary, replace(case, style=style), (1550, 2600, 4110, 4280))


@pytest.mark.parametrize('gpu,missing_loader', [(True, False),(False, False),(True, True)])
def test_real_lower_preview_uses_skia(binary, independent, monkeypatch, tmp_path, gpu, missing_loader):
    run_preview(binary, monkeypatch, tmp_path, independent[:2], gpu, missing_loader)


@pytest.mark.parametrize('gpu,missing_loader', [(True, False),(False, False),(True, True)])
def test_real_lower_mp4_uses_skia(binary, independent, monkeypatch, tmp_path, gpu, missing_loader):
    run_mp4(binary, monkeypatch, tmp_path, independent[:2], gpu, missing_loader)


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('semantics', ['legacy','n3_1074'])
@pytest.mark.parametrize('independent', [False,True])
@pytest.mark.parametrize('dpr', [1,1.5])
def test_real_lower_product_matches_d2d(binary, source, semantics, independent, dpr, monkeypatch):
    import test_subtitle_skia_lower_geometry as probe
    monkeypatch.setattr(probe, 'probe_ir', lambda source, value, dpr=1: ir_for(binary, source, dpr=dpr))
    run_d2d(binary, source, semantics, independent, dpr)


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('semantics', ['legacy','n3_1074'])
@pytest.mark.parametrize('dpr', [1,1.5])
def test_independent_lower_alpha_colors_match_d2d(binary, independent, semantics, dpr):
    from test_subtitle_render_gpu_backend import _render_g1_frames
    track, style, _ = independent
    style = replace(style, layout_semantics=semantics)
    times = [199,350,400,450,699,800,1200,1900]
    with NativeRendererProcess() as d2d:
        _, raw = _render_g1_frames(d2d, style, tuple(times), force_warp=False,
                                  track=track, width=900, height=600, dpr=dpr)
    with SkiaRendererProcess(binary, selection=selection('gpu')) as skia:
        skia.configure(track, style, width=900, height=600, fps=60, dpr=dpr)
        frames, _ = collect(skia, times)
    references=[np.frombuffer(p,np.uint8).reshape(round(600*dpr),round(900*dpr),4) for p in raw]
    # Only the lower layer has half-alpha; other layers have opaque interiors.
    def bounds(p):
        y,x=np.where((p[...,3]>=120) & (p[...,3]<=128) & (p[...,2]>200))
        return None if not len(y) else np.array([x.min(),y.min(),x.max(),y.max()])
    a,b=bounds(references[0]),bounds(frames[0])
    assert a is not None and b is not None and np.max(np.abs(a-b))<=1
    lo,hi=max(min(a[1],b[1])-2,0),max(a[3],b[3])+3
    def sung(p): return (p[lo:hi,:,1].astype(float)*p[lo:hi,:,3]/255**2).sum()
    full_a,full_b=sung(references[-1]),sung(frames[-1])
    width=(full_a+full_b)/(2*(a[3]-a[1]+1)*(128/255))
    for t,a,b in zip(times,references,frames,strict=True):
        assert abs(sung(a)/full_a-sung(b)/full_b)*width<=1, t
    for p in [references[-1],frames[-1]]:
        mask=(p[lo:hi,:,0]>240)&(p[lo:hi,:,1]>240)&(p[lo:hi,:,2]<10)&(p[lo:hi,:,3]>=120)
        # Negative intervals may overlap adjacent half-alpha glyph edges.
        # Their source-over alpha exceeds 128; isolated interiors keep 128.
        assert np.count_nonzero(mask & (p[lo:hi,:,3]==128))>200
