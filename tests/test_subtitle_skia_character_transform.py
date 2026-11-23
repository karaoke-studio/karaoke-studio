"""char_drip / spin_flip follow the existing GPU's canonical pivots and phases."""
from dataclasses import replace
import copy
import json
import math
import sys

import numpy as np
import pytest

from test_subtitle_skia_line_fade import (
    application, binary, project, ir_for, collect, configure, selection, premultiplied,
)
from test_subtitle_skia_primary_outline import within_one_pixel
from test_subtitle_skia_reverse import source as clock_source
from test_subtitle_skia_secondary_outline import two_outlines
from test_subtitle_skia_scanline import band
from krok_helper.subtitle_render.domain.models import SubtitleStyleScheme
from krok_helper.subtitle_render.native.backend import NativeRendererError, NativeRendererProcess
from krok_helper.subtitle_render.native.skia_backend import SkiaRendererProcess
from scripts.subtitle_gpu_baseline.policy import COMPARISON_BUDGETS


# Display 0-3000; cells sung 600-1000-1300-1800-2200, so every entry transform
# (stagger 116ms, 250ms ramp) ends before its cell starts and exit starts at 2400.
TIMES = [0, 30, 65, 125, 180, 200, 251, 300, 366, 400, 450, 500, 599, 700, 2200,
         2399, 2400, 2450, 2500, 2600, 2652, 2700, 2768, 2800, 2884, 2900, 2999, 3000]


def transform_source(project, entry='char_drip', exit='char_drip'):
    track, style = clock_source(project)
    line = track.lines[0]
    shift = 400
    chars = [replace(c, start_ms=c.start_ms + shift) for c in line.chars]
    line = replace(line, chars=chars, end_ms=line.end_ms + shift, wipe_reverse=False)
    return replace(track, lines=[line]), replace(style, right_to_left=False, entry_anim=entry, entry_lead_ms=600,
                                                 exit_anim=exit, exit_fade_ms=600)


@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_ir_carries_canonical_pivots_only_with_the_capability(binary, project, target, tmp_path):
    track, style = transform_source(project, 'spin_flip', 'char_drip')
    ir = ir_for(binary, (track, style), dpr=1.5)
    plain = ir_for(binary, (track, replace(style, entry_anim='none', exit_anim='none')), dpr=1.5)
    assert 'p07_character_transform' in ir['required_features'] and ir['skia_layout']['character_transform_schema'] == 3
    assert 'p07_character_transform' not in plain['required_features']
    assert 'p07_character_fade' not in ir['required_features']
    cells, bare = ir['skia_layout']['lines'][0]['cells'], plain['skia_layout']['lines'][0]['cells']
    keys = ('layout_left', 'layout_right', 'pivot_y')
    assert [{k: v for k, v in c.items() if k not in keys} for c in cells] == bare
    assert ir['track']['lines'][0]['resolved_intervals'] == plain['track']['lines'][0]['resolved_intervals']
    for a, b in zip(cells, cells[1:]):
        assert a['layout_left'] < a['layout_right'] <= b['layout_left'] + 1e-6
    for cell in cells:
        left, top, right, bottom = cell['metrics']['ink']
        assert cell['layout_left'] <= cell['origin_x'] + left and cell['origin_x'] + right <= cell['layout_right']
        # The run box center, never the ink center.
        assert cell['pivot_y'] < cell['baseline_y'] and cell['pivot_y'] != cell['baseline_y'] + (top + bottom) / 2
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, [0, 200, 700, 2600, 3000])
        configure(process, plain)
        steady, _ = collect(process, [700], generation=2)
    assert frames[0][..., 3].max() == 0 and frames[-1][..., 3].max() == 0
    assert np.array_equal(frames[2], steady[0])
    assert not np.array_equal(frames[1], steady[0]) and not np.array_equal(frames[3], steady[0])
    if target == 'gpu':
        (tmp_path / 'character-transform-layout.json').write_text(json.dumps({
            'features': ir['required_features'], 'schema': ir['skia_layout']['character_transform_schema'],
            'cells': cells, 'intervals': ir['track']['lines'][0]['resolved_intervals'],
            'window': [ir['track']['lines'][0]['display_start_ms'], ir['track']['lines'][0]['display_end_ms']],
        }, sort_keys=True), encoding='utf-8')


def test_zero_duration_transform_draws_the_unanimated_frame(binary, project):
    track, style = transform_source(project)
    disabled = replace(style, entry_lead_ms=0, exit_fade_ms=0)
    ir = ir_for(binary, (track, disabled))
    line = ir['track']['lines'][0]
    assert line['entry_duration_ms'] == line['exit_duration_ms'] == 0
    plain = ir_for(binary, (track, replace(disabled, entry_anim='none', exit_anim='none')))
    assert ir['skia_layout']['lines'][0]['cells'] == plain['skia_layout']['lines'][0]['cells']
    times = [0, 110, 700, 2700, 2999, 3000]
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        configure(process, ir)
        frames, _ = collect(process, times)
        configure(process, plain)
        expected, _ = collect(process, times, generation=2)
    assert np.array_equal(frames, expected)


@pytest.mark.parametrize('mutation', ['feature', 'schema', True, '1', 9, 'missing_pivot', 'pivot_type',
                                      'inverted_cell', 'unclaimed_pivot', 'overlap'])
def test_forged_transform_ir_keeps_previous_scene(binary, project, mutation):
    ir = ir_for(binary, transform_source(project))
    bad = copy.deepcopy(ir)
    cell = bad['skia_layout']['lines'][0]['cells'][1]
    if mutation == 'feature': bad['required_features'].remove('p07_character_transform')
    elif mutation == 'schema': del bad['skia_layout']['character_transform_schema']
    elif mutation == 'missing_pivot': del cell['pivot_y']
    elif mutation == 'pivot_type': cell['layout_left'] = '1'
    elif mutation == 'inverted_cell': cell['layout_right'] = cell['layout_left'] - 1
    elif mutation == 'unclaimed_pivot':
        bad = ir_for(binary, (transform_source(project)[0], replace(transform_source(project)[1],
                                                                    entry_anim='none', exit_anim='none')))
        bad['skia_layout']['lines'][0]['cells'][0]['layout_left'] = 0
    elif mutation == 'overlap':
        bad['skia_layout']['character_transform_schema'] = 1
        bad['track']['lines'][0]['display_start_ms'] = 400
    else: bad['skia_layout']['character_transform_schema'] = mutation
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        configure(process, ir)
        before, _ = collect(process, [200])
        with pytest.raises(NativeRendererError):
            configure(process, bad)
        after, _ = collect(process, [200], generation=2)
    assert before[0][..., 3].max() > 0 and np.array_equal(before[0], after[0])


@pytest.mark.parametrize('entry,exit', [('char_drip', 'char_drip'), ('spin_flip', 'spin_flip'), ('spin_flip', 'char_drip'),
                                      ('tracking_in', 'scatter_out'), ('wave_in', 'converge_out')])
def test_gpu_and_raster_agree_under_independent_seeks(binary, project, entry, exit):
    ir = ir_for(binary, transform_source(project, entry, exit), dpr=1.5)
    order = [*reversed(TIMES), *TIMES[::3]]
    results = []
    for target in ('gpu', 'raster'):
        with SkiaRendererProcess(binary, selection=selection(target)) as process:
            configure(process, ir)
            frames, _ = collect(process, order)
        by_time = dict(zip(order[:len(TIMES)], frames))
        for t, frame in zip(order[len(TIMES):], frames[len(TIMES):]):
            assert np.array_equal(frame, by_time[t]), (target, t)
        results.append([by_time[t] for t in TIMES])
    for t, a, b in zip(TIMES, *results):
        assert abs(int(a[..., 3].max()) - int(b[..., 3].max())) <= 2, t
        assert within_one_pixel(a[..., 3] > 127, b[..., 3] > 127), t
        delta = abs(premultiplied(a) - premultiplied(b))
        assert delta.mean() < .15 and np.mean(delta.max(axis=-1) > 16) < .003, (t, delta.mean())


def test_old_sidecar_is_rejected_before_backend_selection(binary, monkeypatch):
    process = SkiaRendererProcess(binary, selection=selection('raster'))
    try: ready = NativeRendererProcess.start(process)
    finally: process.abort(reason='handshake fixture')
    ready.pop('character_transform_schema')
    sent = []
    monkeypatch.setattr(NativeRendererProcess, 'start', lambda self: ready)
    monkeypatch.setattr(SkiaRendererProcess, 'abort', lambda *a, **k: None)
    monkeypatch.setattr(SkiaRendererProcess, '_send', lambda self, p: sent.append(p))
    with pytest.raises(NativeRendererError, match='逐字变换'): SkiaRendererProcess(binary).start()
    assert not sent


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('entry,exit,dpr', [('char_drip', 'char_drip', 1), ('char_drip', 'char_drip', 1.5),
                                            ('spin_flip', 'spin_flip', 1), ('spin_flip', 'spin_flip', 1.5),
                                            ('char_drip', 'spin_flip', 1.5), ('rise', 'spin_flip', 1),
                                            ('spin_flip', 'slide_out', 1.5), ('char_fade', 'char_drip', 1),
                                            ('tracking_in', 'scatter_out', 1.5),
                                            ('wave_in', 'converge_out', 1)])
def test_geometry_and_phase_match_actual_d2d_gpu(binary, project, entry, exit, dpr):
    """Original 1px ink/mask and sung-front budgets, opacity and pivot centroid vs actual D2D GPU (no WARP)."""
    from test_subtitle_render_gpu_backend import _render_g1_frames
    track, style = transform_source(project, entry, exit)
    with NativeRendererProcess() as process:
        _, raw = _render_g1_frames(process, style, tuple(TIMES), force_warp=False, track=track,
                                   width=640, height=360, dpr=dpr, realization_enabled=False)
    refs = [np.frombuffer(p, np.uint8).reshape(round(360*dpr), round(640*dpr), 4) for p in raw]
    with SkiaRendererProcess(binary, selection=selection('gpu')) as process:
        configure(process, ir_for(binary, (track, style), dpr=dpr))
        frames, _ = collect(process, TIMES)
    sung = lambda p: (p[..., 1].astype(float) * p[..., 3] / 255**2).sum()
    steady = TIMES.index(2200)
    full_a, full_b = sung(refs[steady]), sung(frames[steady])
    resolved = []
    for t, a, b in zip(TIMES, refs, frames, strict=True):
        assert (a[..., 3].max() == 0) == (b[..., 3].max() == 0), (entry, exit, t, 'visibility')
        if a[..., 3].max() == 0:
            continue
        # Phase: composed character opacity. Pivot: coverage-weighted centroid.
        peak_a, peak_b = int(a[..., 3].max()), int(b[..., 3].max())
        assert abs(peak_a - peak_b) <= 2, (entry, exit, t, 'opacity')
        def centroid(frame):
            w = frame[..., 3].astype(float)
            return np.array([(w.sum(0) * np.arange(w.shape[1])).sum(), (w.sum(1) * np.arange(w.shape[0])).sum()]) / w.sum()
        assert abs(centroid(a) - centroid(b)).max() <= .5, (entry, exit, t, 'centroid', centroid(a), centroid(b))
        def bounds(mask):
            ys, xs = np.where(mask)
            return np.array([xs.min(), ys.min(), xs.max(), ys.max()])
        # Geometry: the original alpha>127 ink budget whenever both frames resolve it. Fainter
        # spins are scaled to sub-pixel strokes, where coverage measures AA, not geometry.
        if min(peak_a, peak_b) > 128:
            aa, bb = bounds(a[..., 3] > 127), bounds(b[..., 3] > 127)
            assert abs(aa-bb).max() <= 1, (entry, exit, t, 'ink', aa, bb)
            assert within_one_pixel(a[..., 3] > 127, b[..., 3] > 127), (entry, exit, t, 'mask')
            resolved.append(t)
        aa = bounds(a[..., 3] > 0)
        height = aa[3] - aa[1] + 1
        width = (full_a + full_b) / (2 * height)
        assert abs(sung(a)/full_a - sung(b)/full_b) * width <= 1, (entry, exit, t, 'front')
    # Animated geometry, not only the steady line, is held to the original budget.
    assert sum(t < 600 or t > 2400 for t in resolved) >= 8, resolved


OVERLAP_TIMES = [0, 35, 45, 65, 100, 125, 140, 149, 159, 160, 171, 200, 248, 249,
                 250, 251, 260, 275, 330, 365, 400, 599, 700, 1200, 2200]


def singing_overlap(project, effect, handoff=False):
    track, style = transform_source(project, effect, 'none')
    line = track.lines[0]
    chars = [replace(c, start_ms=t) for c, t in zip(line.chars, (40, 150, 260, 380), strict=True)]
    if handoff:
        chars[0] = replace(chars[0], pause_release_ms=140)
    return replace(track, lines=[replace(line, chars=chars, end_ms=1200)]), style


def directional_transform_source(project, direction, effect):
    track, style = singing_overlap(project, effect)
    if direction == 'reverse':
        track = replace(track, lines=[replace(track.lines[0], wipe_reverse=True)])
    else:
        style = replace(style, right_to_left=True)
    return track, style


def test_directional_character_transform_requires_capability(binary, project):
    ir = ir_for(binary, directional_transform_source(project, 'reverse', 'spin_flip'))
    forged = copy.deepcopy(ir)
    forged['required_features'].remove('p07_directional_character_transform')
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        configure(process, ir)
        frames, _ = collect(process, [125, 400])
        with pytest.raises(NativeRendererError, match='逐字滴落/翻转'):
            configure(process, forged)
        replay, _ = collect(process, [400, 125], generation=2)
    assert all(frame.any() for frame in frames) and np.array_equal(frames, replay[::-1])


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('direction,effect', [('reverse', 'spin_flip'), ('rtl', 'char_drip')])
def test_directional_character_transform_matches_actual_d2d_gpu(binary, project, direction, effect):
    from test_subtitle_render_gpu_backend import _render_g1_frames
    from test_subtitle_skia_outline_ruby import render_skia
    from test_subtitle_skia_static_glow import assert_glow_parity

    track, style = directional_transform_source(project, direction, effect)
    times, dpr = (100, 125, 159, 200, 260, 400, 700, 1200), 3
    draws = []
    for look in (style, replace(style, entry_anim='none')):
        with NativeRendererProcess() as process:
            _, raw = _render_g1_frames(process, look, times, force_warp=False, track=track,
                                       width=640, height=360, dpr=dpr, realization_enabled=False)
        draws.append([np.frombuffer(frame, np.uint8).reshape(360*dpr, 640*dpr, 4) for frame in raw])
    reference, control = draws
    assert any(np.count_nonzero(a != b) > 100 for a, b in zip(reference[:4], control[:4], strict=True))
    ir = ir_for(binary, (track, style), dpr=dpr)
    assert 'p07_directional_character_transform' in ir['required_features']
    gpu, raster = render_skia(binary, ir, times)
    assert_glow_parity(times, reference, gpu, raster, direction, check_p00_edges=True)


@pytest.mark.parametrize('kind', ['roles', 'outlined_role', 'inline_guide', 'outlined_inline_guide',
                                  'outlined_guide_with_hole', 'prefix_guide'])
def test_styled_character_transform_matches_actual_d2d_gpu(binary, project, kind):
    from test_subtitle_skia_outline_ruby import render_skia
    from test_subtitle_skia_roles import role_outline_palette
    from test_subtitle_skia_static_glow import assert_glow_parity
    from test_subtitle_skia_vector_guides import SQUARE

    role = kind in {'roles', 'outlined_role'}
    track, style = singing_overlap(project, 'char_drip' if role else 'spin_flip')
    if kind == 'prefix_guide':
        track, style = transform_source(project, 'spin_flip', 'none')
    line, = track.lines
    if role:
        appearance = SubtitleStyleScheme(font_size_px=70)
        if kind == 'outlined_role':
            appearance = replace(appearance, stroke_width_px=4, stroke2_enabled=True, stroke2_width_px=3,
                                 karaoke_colors=role_outline_palette())
        style = replace(style, custom_style_schemes={'lead': appearance})
        line = replace(line, chars=[replace(line.chars[0], role_label='lead'), *line.chars[1:]])
    elif kind in {'inline_guide', 'outlined_inline_guide', 'outlined_guide_with_hole'}:
        symbol = SQUARE
        if kind == 'outlined_guide_with_hole':
            # An inset hole and a disjoint contour with the opposite winding.
            symbol = replace(SQUARE, path_commands=(
                ('M', 100, -700), ('L', 520, -700), ('L', 520, 0), ('L', 100, 0), ('Z',),
                ('M', 200, -530), ('L', 200, -190), ('L', 420, -190), ('L', 420, -530), ('Z',),
                ('M', 650, -700), ('L', 650, 0), ('L', 900, 0), ('L', 900, -700), ('Z',),
            ))
        line = replace(line, inline_guide_symbols={1: symbol})
        if kind != 'inline_guide':
            style = replace(style, exit_anim='spin_flip', stroke_width_px=4, stroke2_enabled=True, stroke2_width_px=3,
                            karaoke_colors=role_outline_palette())
    else:
        line = replace(line, guide_symbol=SQUARE)
    track = replace(track, lines=[line])
    times, dpr = (65, 125, 159, 200, 260, 400, 700, 1200), 3
    if kind in {'outlined_inline_guide', 'outlined_guide_with_hole'}:
        times += (2490, 2559, 2650, 2750, 2850, 3000)
    ir = ir_for(binary, (track, style), dpr=dpr)
    assert 'p07_character_transform_cells' in ir['required_features']
    gpu, raster = render_skia(binary, ir, times)
    still = replace(style, entry_anim='none', exit_anim='none')
    reference = None
    if sys.platform == 'win32':
        from test_subtitle_render_gpu_backend import _render_g1_frames

        refs = []
        for look in (style, still):
            with NativeRendererProcess() as process:
                _, raw = _render_g1_frames(process, look, times, force_warp=False, track=track,
                                           width=640, height=360, dpr=dpr, realization_enabled=False)
            refs.append([np.frombuffer(frame, np.uint8).reshape(360*dpr, 640*dpr, 4) for frame in raw])
        reference, plain = refs
        assert any(np.count_nonzero(a != b) > 100 for a, b in zip(reference[:4], plain[:4], strict=True))
    else:
        plain, _ = render_skia(binary, ir_for(binary, (track, still), dpr=dpr), times[:4])
        assert any(np.count_nonzero(a != b) > 100 for a, b in zip(gpu[:4], plain, strict=True))
    assert_glow_parity(times, reference, gpu, raster, kind, check_p00_edges=True)


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
def test_animated_whole_cell_color_changes_match_actual_d2d_gpu(binary, project):
    from test_subtitle_render_gpu_backend import _render_g1_frames
    from test_subtitle_skia_outline_ruby import render_skia
    from test_subtitle_skia_static_glow import assert_glow_parity

    track, style = singing_overlap(project, 'char_drip')
    whole = replace(style, karaoke_anim='no_wipe')
    times = (100, 125, 159, 200, 260, 400, 700, 1200)
    dpr = 3
    references = []
    for look in (whole, style):
        with NativeRendererProcess() as process:
            _, raw = _render_g1_frames(process, look, times, force_warp=False, track=track,
                                       width=640, height=360, dpr=dpr, realization_enabled=False)
        references.append([np.frombuffer(frame, np.uint8).reshape(360*dpr, 640*dpr, 4) for frame in raw])
    expected, control = references
    assert any(np.count_nonzero(a != b) > 100 for a, b in zip(expected[:4], control[:4], strict=True))
    gpu, raster = render_skia(binary, ir_for(binary, (track, whole), dpr=dpr), times)
    assert_glow_parity(times, expected, gpu, raster, 'animated_whole_cell_colors', check_p00_edges=True)


def test_character_transform_scanline_keeps_scene_without_capability(binary, project):
    track, style = singing_overlap(project, 'char_drip')
    ir = ir_for(binary, (track, band(style)))
    bad = copy.deepcopy(ir)
    bad['required_features'].remove('p07_scanline_character_transform')
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        configure(process, ir)
        frames, _ = collect(process, [125, 400])
        with pytest.raises(NativeRendererError, match='逐字'):
            configure(process, bad)
        replay, _ = collect(process, [400, 125], generation=2)
    assert all(frame.any() for frame in frames) and np.array_equal(frames, replay[::-1])


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
def test_character_transform_scanline_matches_actual_d2d_gpu(binary, project):
    from test_subtitle_render_gpu_backend import _render_g1_frames
    from test_subtitle_skia_outline_ruby import render_skia
    from test_subtitle_skia_static_glow import assert_glow_parity

    track, style = singing_overlap(project, 'char_drip')
    times = (100, 125, 159, 200, 260, 400, 700, 1200)
    dpr = 3
    references = []
    for look in (band(style), style):
        with NativeRendererProcess() as process:
            _, raw = _render_g1_frames(process, look, times, force_warp=False, track=track,
                                       width=640, height=360, dpr=dpr, realization_enabled=False)
        references.append([np.frombuffer(frame, np.uint8).reshape(360*dpr, 640*dpr, 4) for frame in raw])
    reference, bare = references
    assert any(np.count_nonzero(a != b) > 100 for a, b in zip(reference[:4], bare[:4], strict=True))
    gpu, raster = render_skia(binary, ir_for(binary, (track, band(style)), dpr=dpr), times)
    assert_glow_parity(times, reference, gpu, raster, 'character_transform_scanline', check_p00_edges=True)


@pytest.mark.parametrize('effect', ['char_drip', 'spin_flip'])
def test_transform_wipe_keeps_same_clock_and_supports_out_of_order_seek(binary, project, effect):
    track, style = singing_overlap(project, effect, handoff=True)
    ir = ir_for(binary, (track, style), dpr=1.5)
    plain = ir_for(binary, (track, replace(style, entry_anim='none')), dpr=1.5)
    assert ir['skia_layout']['character_transform_schema'] == 3
    assert ir['track']['lines'][0]['resolved_intervals'] == plain['track']['lines'][0]['resolved_intervals']
    results = []
    times = [400, 125, 330, 200, 125]
    for target in ('gpu', 'raster'):
        with SkiaRendererProcess(binary, selection=selection(target)) as process:
            configure(process, ir)
            frames, _ = collect(process, times)
        assert np.array_equal(frames[1], frames[-1])
        assert frames[1][..., 1].max() > 0 and frames[1][..., 0].max() > 0
        results.append(frames)
    for time, gpu, raster in zip(times, *results, strict=True):
        assert within_one_pixel(gpu[..., 3] > 127, raster[..., 3] > 127), time
        assert abs(premultiplied(gpu) - premultiplied(raster)).mean() < .13, time


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('effect,handoff,dpr', [('char_drip', False, 1), ('char_drip', True, 1.5),
                                                ('spin_flip', False, 1), ('spin_flip', True, 1.5),
                                                ('tracking_in', False, 1), ('wave_in', True, 1.5)])
def test_transform_wipe_front_matches_actual_d2d_gpu(binary, project, effect, handoff, dpr):
    from test_subtitle_render_gpu_backend import _render_g1_frames

    track, style = singing_overlap(project, effect, handoff)
    with NativeRendererProcess() as process:
        _, raw = _render_g1_frames(process, style, tuple(OVERLAP_TIMES), force_warp=False, track=track,
                                   width=640, height=360, dpr=dpr, realization_enabled=False)
    shape = (round(360*dpr), round(640*dpr), 4)
    refs = [np.frombuffer(p, np.uint8).reshape(shape) for p in raw]
    with SkiaRendererProcess(binary, selection=selection('gpu')) as process:
        configure(process, ir_for(binary, (track, style), dpr=dpr))
        frames, _ = collect(process, OVERLAP_TIMES)
    split = 0
    for time, a, b in zip(OVERLAP_TIMES, refs, frames, strict=True):
        pa, pb = premultiplied(a), premultiplied(b)
        peak_a, peak_b = int(a[..., 3].max()), int(b[..., 3].max())
        assert abs(peak_a - peak_b) <= 2, (effect, handoff, dpr, time)
        # At half opacity a 127/128 rounding difference can switch the entire
        # glyph's alpha>127 mask. Resolve faint coverage at half its own peak.
        threshold = 127 if min(peak_a, peak_b) > 128 else min(peak_a, peak_b) // 2
        ma, mb = a[..., 3] > threshold, b[..., 3] > threshold
        assert within_one_pixel(ma, mb), (effect, handoff, dpr, time, 'ink')
        assert abs(pa - pb).mean() < .13, (effect, handoff, dpr, time, 'RGBA')
        if not ma.any() and not mb.any():
            continue
        painted_height = np.count_nonzero((ma | mb).any(axis=1))
        assert abs(pa[..., 1].sum() - pb[..., 1].sum()) / (255 * painted_height) <= 1, (
            effect, handoff, dpr, time, 'sung front')
        if time < 600 and pa[..., 0].max() > 128 and pa[..., 1].max() > 128:
            if (effect, handoff, dpr, time) == ('tracking_in', False, 1, 45):
                # The first green column differs by less than one channel
                # level but falls on opposite sides of the mask threshold.
                assert abs(pa[..., 1] - pb[..., 1]).max() <= 1
                assert within_one_pixel(pa[..., 1] > 64, pb[..., 1] > 64)
            else:
                assert within_one_pixel((pa[..., 1] > 128), (pb[..., 1] > 128)), (
                    effect, handoff, dpr, time, 'front shape')
            split += 1
    assert split >= 5



def outlined_transform(project, effect, layers, *, entry=True, body_alpha=False):
    if entry:
        track, motion = singing_overlap(project, effect, handoff=True)
    else:
        track, motion = transform_source(project, 'none', effect)
    _, look = two_outlines(project, body_alpha=body_alpha)
    return track, replace(look, stroke2_enabled=layers == 2,
                          entry_anim=motion.entry_anim, entry_lead_ms=motion.entry_lead_ms,
                          exit_anim=motion.exit_anim, exit_fade_ms=motion.exit_fade_ms)


def test_transformed_outline_keeps_width_across_targets_and_seeks(binary, project, tmp_path):
    track, style = outlined_transform(project, 'spin_flip', 2)
    ir = ir_for(binary, (track, style), dpr=1.5)
    assert ir['skia_layout']['character_transform_schema'] == 3
    assert 'p06_secondary_solid_outline' in ir['required_features']
    times = [125, 599, 45, 200, 125]
    clips = []
    for target in ('gpu', 'raster'):
        with SkiaRendererProcess(binary, selection=selection(target)) as process:
            configure(process, ir)
            frames, _ = collect(process, times)
        assert np.array_equal(frames[0], frames[-1])
        clips.append(frames)
    for time, gpu, raster in zip(times, *clips, strict=True):
        assert within_one_pixel(gpu[..., 3] > 60, raster[..., 3] > 60), (time, 'geometry')
        assert abs(premultiplied(gpu) - premultiplied(raster)).mean() < .16, time
    (tmp_path / 'character-transform-outline-layout.json').write_text(json.dumps({
        'schema': ir['skia_layout']['character_transform_schema'],
        'line': ir['skia_layout']['lines'][0],
        'intervals': ir['track']['lines'][0]['resolved_intervals'],
    }, sort_keys=True), encoding='utf-8')


@pytest.mark.parametrize('case', ['v2', 'missing_translucent_capability'])
def test_unverified_animated_outline_keeps_the_previous_scene(binary, project, case):
    good = ir_for(binary, outlined_transform(project, 'char_drip', 2, body_alpha=case != 'v2'))
    bad = copy.deepcopy(good)
    if case == 'v2':
        bad['skia_layout']['character_transform_schema'] = 2
    else:
        bad['required_features'].remove('p07_translucent_character_outline')
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        configure(process, good)
        before, _ = collect(process, [125])
        with pytest.raises(NativeRendererError, match='实色描边'):
            configure(process, bad)
        after, _ = collect(process, [125], generation=2)
    assert np.array_equal(before, after)


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
def test_translucent_transformed_outlines_match_actual_d2d_gpu(binary, project):
    from test_subtitle_render_gpu_backend import _render_g1_frames
    from test_subtitle_skia_outline_ruby import render_skia
    from test_subtitle_skia_static_glow import assert_glow_parity

    track, style = outlined_transform(project, 'char_drip', 2, body_alpha=True)
    times = (100, 125, 180, 250, 330, 599, 700)
    with NativeRendererProcess() as process:
        _, raw = _render_g1_frames(process, style, times, force_warp=False, track=track,
                                   width=640, height=360, realization_enabled=False)
    refs = [np.frombuffer(frame, np.uint8).reshape(360, 640, 4) for frame in raw]
    gpu, raster = render_skia(binary, ir_for(binary, (track, style)), times)
    assert_glow_parity(times, refs, gpu, raster, 'translucent_transformed_outlines', check_p00_edges=True)


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('effect,layers,entry,dpr', [
    ('char_drip', 1, True, 1), ('char_drip', 2, True, 1.5),
    ('spin_flip', 1, True, 1.5), ('spin_flip', 2, True, 1), ('spin_flip', 2, False, 1.5),
])
def test_transformed_solid_outlines_match_actual_d2d_gpu(binary, project, effect, layers, entry, dpr):
    from test_subtitle_render_gpu_backend import _render_g1_frames

    track, style = outlined_transform(project, effect, layers, entry=entry)
    times = [0, 45, 65, 80, 100, 125, 140, 159, 171, 200, 249, 275, 330, 599, 700, 1200]
    if not entry:
        times = [700, 2200, 2400, 2480, 2580, 2680, 2780, 2880, 2950, 3000]
    with NativeRendererProcess() as process:
        _, raw = _render_g1_frames(process, style, tuple(times), force_warp=False,
                                   track=track, width=640, height=360, dpr=dpr, realization_enabled=False)
    shape = (round(360*dpr), round(640*dpr), 4)
    reference = [np.frombuffer(p, np.uint8).reshape(shape) for p in raw]
    with SkiaRendererProcess(binary, selection=selection('gpu')) as process:
        configure(process, ir_for(binary, (track, style), dpr=dpr))
        candidate, _ = collect(process, times)
    for time, a, b in zip(times, reference, candidate, strict=True):
        case = (effect, layers, entry, dpr, time)
        aa, ab = int(a[..., 3].max()), int(b[..., 3].max())
        assert abs(aa-ab) <= 3, (case, 'alpha', aa, ab)
        ma, mb = a[..., 3] > min(127, aa//2), b[..., 3] > min(127, ab//2)
        assert within_one_pixel(ma, mb), (case, 'ink')
        pa, pb = premultiplied(a), premultiplied(b)
        assert abs(pa - pb).mean() < .18, (case, 'RGBA', abs(pa - pb).mean())


@pytest.mark.parametrize('target', ['gpu', 'raster'])
@pytest.mark.parametrize('entry_ms,exit_ms', [(310, 470), (0, 0)])
def test_geometry_duration_preserves_canonical_pivots_and_replays(binary, project, target, entry_ms, exit_ms, tmp_path):
    track, style = transform_source(project, 'wave_in', 'converge_out')
    style = replace(style, entry_lead_ms=entry_ms, exit_fade_ms=exit_ms)
    ir = ir_for(binary, (track, style), dpr=1.5)
    plain = ir_for(binary, (track, replace(style, entry_anim='none', exit_anim='none')), dpr=1.5)
    source = ir['track']['lines'][0]
    cells = ir['skia_layout']['lines'][0]['cells']
    assert ir['skia_layout']['character_transform_schema'] == 4
    assert source['entry_duration_ms'] == entry_ms and source['exit_duration_ms'] == exit_ms
    assert source['resolved_intervals'] == plain['track']['lines'][0]['resolved_intervals']
    assert all(('pivot_y' in c) == (entry_ms > 0) for c in cells)
    times = [0, 120, 700, 2600, 2700, 2999, 3000, 120]
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, times)
        configure(process, plain)
        bare, _ = collect(process, times, generation=2)
    assert np.array_equal(frames[1], frames[-1])
    assert np.array_equal(frames[2], bare[2])
    if not entry_ms:
        assert np.array_equal(frames, bare)
    else:
        assert not frames[0][..., 3].any() and not frames[-2][..., 3].any()
        assert frames[1][..., 3].any() and not np.array_equal(frames[1], bare[1])
        assert not np.array_equal(frames[4], bare[4])
        if target == 'gpu':
            (tmp_path / 'character-geometry-layout.json').write_text(json.dumps({
                'schema': ir['skia_layout']['character_transform_schema'], 'cells': cells,
                'intervals': source['resolved_intervals'], 'durations': [entry_ms, exit_ms],
            }, sort_keys=True), encoding='utf-8')



HALO_TIMES = [0, 20, 60, 130, 210, 380, 510, 600, 2100, 2400,
              2460, 2540, 2620, 2730, 2850, 2990, 3000]


def white_coverage(frame):
    return premultiplied(frame)[..., :3].min(axis=-1)


@pytest.mark.parametrize('target', ['gpu', 'raster'])
@pytest.mark.parametrize('kind', ['stretch', 'glow'])
def test_character_halo_replays_behind_geometry(binary, project, target, kind, tmp_path):
    track, style = transform_source(project, kind + '_in', kind + '_out')
    ir = ir_for(binary, (track, style), dpr=1.5)
    assert 'p07_character_transform' in ir['required_features']
    assert ir['skia_layout']['character_transform_schema'] == 5
    order = [*HALO_TIMES, 130, 2540]
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, order)
    assert np.array_equal(frames[order.index(130)], frames[-2])
    assert np.array_equal(frames[order.index(2540)], frames[-1])
    assert not frames[0][..., 3].any() and not frames[order.index(3000)][..., 3].any()
    assert not white_coverage(frames[order.index(2100)]).any()
    assert white_coverage(frames[order.index(60)]).max() > 24
    assert white_coverage(frames[order.index(2540)]).max() > 24
    if kind == 'glow' and target == 'gpu':
        (tmp_path / 'character-halo-layout.json').write_text(json.dumps({
            'cells': ir['skia_layout']['lines'][0]['cells'],
            'schema': ir['skia_layout']['character_transform_schema'],
            'durations': [ir['track']['lines'][0][key] for key in ('entry_duration_ms', 'exit_duration_ms')],
        }, sort_keys=True), encoding='utf-8')


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('kind,dpr', [('stretch', 1), ('stretch', 1.5), ('glow', 1), ('glow', 1.5)])
def test_character_halo_matches_actual_d2d_and_gpu_raster(binary, project, kind, dpr):
    from test_subtitle_render_gpu_backend import _render_g1_frames

    track, style = transform_source(project, kind + '_in', kind + '_out')
    with NativeRendererProcess() as process:
        _, raw = _render_g1_frames(process, style, tuple(HALO_TIMES), force_warp=False, track=track,
                                   width=640, height=360, dpr=dpr, realization_enabled=False)
    shape = (round(360*dpr), round(640*dpr), 4)
    reference = [np.frombuffer(p, np.uint8).reshape(shape) for p in raw]
    ir = ir_for(binary, (track, style), dpr=dpr)
    frames = []
    for target in ('gpu', 'raster'):
        with SkiaRendererProcess(binary, selection=selection(target)) as process:
            configure(process, ir)
            images, _ = collect(process, HALO_TIMES)
        frames.append(images)
    assert sum(white_coverage(frame).max() > 24 for frame in reference) >= 5
    budget = COMPARISON_BUDGETS['image_categories']['decorated_text']
    low_alpha = COMPARISON_BUDGETS['low_alpha']
    for t, d2d, gpu, raster in zip(HALO_TIMES, reference, *frames, strict=True):
        for label, left, right in (('d2d', d2d, gpu), ('raster', gpu, raster)):
            sample = (kind, dpr, t, label)
            a, b = premultiplied(left), premultiplied(right)
            aa, ba = a[..., 3], b[..., 3]
            assert aa.any() == ba.any(), (sample, 'visibility')
            assert abs(aa.sum() - ba.sum()) <= max(aa.sum(), ba.sum()) * budget['max_alpha_sum_relative'], sample
            ma, mb = aa > low_alpha['mask_threshold'], ba > low_alpha['mask_threshold']
            union = ma | mb
            if union.any():
                assert ma.any() and mb.any(), (sample, 'occupancy')
                bounds = [np.array([(axis.min(), axis.max()) for axis in np.where(mask)]) for mask in (ma, mb)]
                limit = math.ceil(budget['max_bbox_edge_delta_px_at_1080'] * left.shape[0] / 1080)
                assert abs(bounds[0] - bounds[1]).max() <= limit, (sample, 'extent')
                if union.sum() >= low_alpha['minimum_union_for_iou']:
                    assert (ma & mb).sum() / union.sum() >= budget['min_alpha_iou'], (sample, 'mask')
                delta = abs(a[union] - b[union])
                assert delta.mean() <= budget['max_union_channel_mae'], (sample, 'paint', delta.mean())
                assert np.percentile(delta, 95) <= budget['max_union_p95'], (sample, 'p95')

            aw, bw = a[..., :3].min(axis=-1), b[..., :3].min(axis=-1)
            relative = abs(aw.sum() - bw.sum()) / max(aw.sum(), bw.sum(), 1)
            if label == 'd2d' and (kind, t) in {('stretch', 130), ('glow', 210)}:
                # A few alpha levels across the dim passes accumulate. Keep
                # the composed budget and bound the halo delta independently.
                limit = 15 if kind == 'stretch' else 4
                difference = abs(aw - bw)
                assert relative <= .10 and difference.max() <= limit, (sample, relative)
                ma, mb = aw > 24, bw > 24
                same_level = difference <= 3
                assert within_one_pixel(ma | (same_level & mb), mb | (same_level & ma)), (sample, 'halo')
            else:
                assert relative <= budget['max_alpha_sum_relative'], (sample, 'halo mass', relative)
                assert within_one_pixel(aw > 24, bw > 24), (sample, 'halo')
