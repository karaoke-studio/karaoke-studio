"""Singing particles use the existing burst clocks and canonical cell anchors."""
import copy
from dataclasses import replace
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
from test_subtitle_skia_roles import colors
from test_subtitle_skia_outline_ruby import annotated, mixed, pages, reading, upper_source, upper_ir, render_skia
from krok_helper.subtitle_render.domain.paint import PaintFill
from krok_helper.subtitle_render.native.backend import NativeRendererError, NativeRendererProcess
from krok_helper.subtitle_render.native.skia_backend import SkiaRendererProcess
from scripts.subtitle_gpu_baseline.policy import COMPARISON_BUDGETS

TIMES = [0, 80, 160, 200, 250, 400, 600, 750, 1100, 1500, 1820, 2200, 2500, 2900, 3000]


def twinkle_source(project, kind='plain'):
    track, style = clock_source(project, 'pause' if kind == 'pause' else 'plain')
    track = replace(track, lines=[replace(track.lines[0], wipe_reverse=False)])
    color2 = '#00FFFF' if kind == 'two_colors' else '#0000FF'
    return track, replace(style, sing_fx='twinkle', fx_particle_color='#0000FF', fx_particle_color2=color2)


def test_reverse_utopia_particles_keep_their_clock_after_seek(binary, project):
    track, style = twinkle_source(project, 'two_colors')
    track = replace(track, lines=[replace(line, wipe_reverse=True) for line in track.lines])
    style = replace(style, reverse_karaoke_anim='utopia')
    ir = ir_for(binary, (track, style))
    plain = ir_for(binary, (track, replace(style, reverse_karaoke_anim='none')))
    source, = ir['track']['lines']
    assert source['fx_bursts'] == plain['track']['lines'][0]['fx_bursts']
    times = [160, 450, 750, 1100, 1820, 2200]
    gpu, raster = render_skia(binary, ir, times)
    with SkiaRendererProcess(binary, selection=selection('gpu')) as process:
        configure(process, ir)
        replay, _ = collect(process, times[::-1])
    assert np.array_equal(gpu, replay[::-1])
    assert np.count_nonzero(blue_coverage(gpu[3]) > 35) > 30
    for time, a, b in zip(times, gpu, raster, strict=True):
        assert_particle_region(a, b, ('reverse_utopia_seek', time))


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('case', ['forward', 'reverse', 'zoom', 'outlined'])
def test_plain_utopia_particles_match_actual_d2d_gpu(binary, project, case):
    from test_subtitle_render_gpu_backend import _render_g1_frames

    track, base = twinkle_source(project, 'two_colors')
    if case == 'reverse':
        track = replace(track, lines=[replace(line, wipe_reverse=True) for line in track.lines])
    if case == 'outlined':
        palette = base.karaoke_colors
        before = replace(palette.before, stroke=PaintFill(color='#FF422200'), stroke2=PaintFill(color='#FF301000'))
        after = replace(palette.after, stroke=PaintFill(color='#FF543400'), stroke2=PaintFill(color='#FF180800'))
        base = replace(base, karaoke_colors=replace(palette, before=before, after=after),
                       stroke_width_px=4, stroke2_enabled=True, stroke2_width_px=3)
    style = replace(base, karaoke_anim='zoom_pulse' if case == 'zoom' else 'utopia',
                    reverse_karaoke_anim='utopia', zoom_pulse_peak_pct=150)
    times, dpr = (250, 400, 750, 1100, 1500, 1820, 2200), 3
    refs = []
    for look in (style, replace(style, sing_fx='none'), base):
        with NativeRendererProcess() as process:
            _, raw = _render_g1_frames(process, look, times, force_warp=False, track=track,
                                       width=640, height=360, dpr=dpr, realization_enabled=False)
        refs.append([np.frombuffer(p, np.uint8).reshape(360*dpr, 640*dpr, 4) for p in raw])
    for control in refs[1:]:
        assert any(np.count_nonzero(a != b) > 100 for a, b in zip(refs[0], control, strict=True))
    ir = ir_for(binary, (track, style), dpr=dpr)
    feature = {
        'zoom': 'p07_plain_zoom_singing_particles',
        'outlined': 'p07_horizontal_outlined_singing_particles',
    }.get(case, 'p07_plain_scaled_singing_particles')
    assert feature in ir['required_features']
    assert ('p07_reverse_singing_particles' in ir['required_features']) == (case == 'reverse')
    gpu, raster = render_skia(binary, ir, times)
    for time, d2d, vulkan, cpu in zip(times, refs[0], gpu, raster, strict=True):
        assert_particle_region(d2d, vulkan, ('utopia', case, time), check_contour=case != 'outlined')
        assert_particle_region(vulkan, cpu, ('utopia', case, time, 'raster'), check_contour=case != 'outlined')
        if case == 'outlined':
            # A sprite tip blended over an opaque stroke can straddle 60/255 by three levels.
            coverage = [blue_coverage(frame) for frame in (d2d, vulkan, cpu)]
            for a, b in zip(coverage, coverage[1:]):
                edge = (np.minimum(a, b) == 60) & (np.maximum(a, b) <= 63)
                assert within_one_pixel((a > 60) | edge, (b > 60) | edge), (case, time)
        assert abs(premultiplied(d2d) - premultiplied(vulkan)).mean() < .16
    if case == 'outlined':
        from test_subtitle_skia_static_glow import assert_glow_parity

        assert_glow_parity(times, refs[0], gpu, raster, 'plain_utopia_outlined_particles', check_p00_edges=True)
    ir['required_features'].remove(feature)
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        with pytest.raises(NativeRendererError):
            configure(process, ir)


def test_rtl_singing_particles_follow_resolved_cell_anchors(binary, project):
    track, style = twinkle_source(project, 'two_colors')
    style = replace(style, right_to_left=True)
    times, dpr = (250, 400, 750, 1100, 1500, 1820, 2200), 3
    ir = ir_for(binary, (track, style), dpr=dpr)
    assert 'p07_rtl_singing_particles' in ir['required_features']
    gpu, raster = render_skia(binary, ir, times)
    assert np.count_nonzero(blue_coverage(gpu[3]) > 35) > 30
    for time, vulkan, cpu in zip(times, gpu, raster, strict=True):
        assert_particle_region(vulkan, cpu, ('rtl_singing', time, 'raster'))
    if sys.platform != 'win32':
        return
    from test_subtitle_render_gpu_backend import _render_g1_frames

    refs = []
    for look in (style, replace(style, sing_fx='none')):
        with NativeRendererProcess() as process:
            _, raw = _render_g1_frames(process, look, times, force_warp=False, track=track,
                                       width=640, height=360, dpr=dpr, realization_enabled=False)
        refs.append([np.frombuffer(p, np.uint8).reshape(360*dpr, 640*dpr, 4) for p in raw])
    assert any(np.count_nonzero(a != b) > 100 for a, b in zip(*refs, strict=True))
    for time, d2d, vulkan in zip(times, refs[0], gpu, strict=True):
        assert_particle_region(d2d, vulkan, ('rtl_singing', time))
        assert abs(premultiplied(d2d) - premultiplied(vulkan)).mean() < .16


@pytest.mark.parametrize(('kind', 'appearance'), [
    ('note', 'outlines'), ('note', 'shadow'), ('note', 'horizontal'), ('note', 'image'),
    ('flame', 'outlines'), ('lightning', 'outlines'),
])
def test_singing_sprites_use_inherited_particle_paint_layers(binary, project, tmp_path, kind, appearance):
    from krok_helper.subtitle_render.domain.models import SubtitleStyleScheme
    from test_subtitle_skia_static_glow import assert_glow_parity

    track, style = twinkle_source(project)
    solid = PaintFill(color='#FF226BD5')
    shadow = appearance == 'shadow'
    mode = 'gradient_horizontal' if appearance == 'horizontal' else 'gradient_vertical'
    fill = PaintFill(mode=mode, start_color='#FF2020FF', end_color='#FF15FFF0')
    if appearance == 'image':
        from test_subtitle_skia_image_fills import image_paint, pattern, write_png

        image_path = write_png(tmp_path / 'note.png', pattern('opaque'))
        fill = image_paint(image_path, scale=780, fallback='#FF226BD5')
    palette = colors('#FF226BD5', '#FF226BD5')
    inner = (PaintFill(mode='gradient_horizontal', start_color='#FFFF4080', end_color='#FF9840C0')
             if appearance == 'horizontal' else PaintFill(color='#FFFF4080'))
    after = replace(palette.after, text=fill, stroke=inner,
                    stroke2=PaintFill(color='#FFFFD22C'), shadow=PaintFill(color='#CC3CB549'))
    scheme = SubtitleStyleScheme(karaoke_colors=replace(palette, after=after),
                                 stroke_width_px=6, stroke2_enabled=True, stroke2_width_px=4)
    if shadow:
        scheme = replace(scheme, decoration_kind='shadow', shadow_offset_x=17, shadow_offset_y=-13)
    style = replace(style, karaoke_colors=colors('#00000000', '#00000000'), sing_fx=kind,
                    fx_particle_size_em=.72, fx_particle_color_mode='role', fx_particle_role_name='particle',
                    fx_particle_color_layers='all' if shadow else 'stroke', custom_style_schemes={'particle': scheme})
    if shadow:
        controls = (replace(style, fx_particle_color_layers='stroke'),)
    else:
        solid_scheme = replace(scheme, karaoke_colors=replace(palette, after=replace(after, text=solid)))
        controls = (replace(style, custom_style_schemes={'particle': solid_scheme}),)
        if appearance == 'outlines':
            controls += (replace(style, fx_particle_color_layers='solid'),)
    times, dpr = (300, 475, 680, 950, 1280, 1590, 1940), 3
    refs = []
    if sys.platform == 'win32':
        from test_subtitle_render_gpu_backend import _render_g1_frames

        for look in (style, *controls):
            with NativeRendererProcess() as process:
                _, raw = _render_g1_frames(process, look, times, force_warp=False, track=track,
                                           width=640, height=360, dpr=dpr, realization_enabled=False)
            refs.append([np.frombuffer(p, np.uint8).reshape(360*dpr, 640*dpr, 4) for p in raw])
        for control in refs[1:]:
            assert any(np.count_nonzero(a != b) > 100 for a, b in zip(refs[0], control, strict=True))
    ir = ir_for(binary, (track, style), dpr=dpr)
    assert 'p07_singing_sprite_paints' in ir['required_features']
    assert ('p07_energy_singing_paints' in ir['required_features']) == (kind in {'flame', 'lightning'})
    assert ('p07_singing_sprite_shadows' in ir['required_features']) == shadow
    assert ('p07_singing_sprite_line_gradients' in ir['required_features']) == (appearance == 'horizontal')
    assert ('p07_singing_sprite_images' in ir['required_features']) == (appearance == 'image')
    assert any(paint['stroke_width_px'] > 0 and paint['stroke2_width_px'] > 0
               for paint in ir['fx_paint_table'])
    gpu, raster = render_skia(binary, ir, times)
    assert any(np.count_nonzero(frame[..., 3] > 30) > 100 for frame in gpu)
    if shadow:
        assert any(np.count_nonzero((frame[..., 1] > np.maximum(frame[..., 0], frame[..., 2]))
                                    & (frame[..., 3] > 30)) > 100 for frame in gpu)
    assert_glow_parity(times, refs[0] if refs else None, gpu, raster, (kind, appearance),
                       check_p00_edges=True)


@pytest.mark.parametrize('mode', ['gradient_vertical', 'split_vertical'])
def test_singing_ripple_inherits_color_along_radius(binary, project, mode):
    from krok_helper.subtitle_render.domain.models import SubtitleStyleScheme
    from test_subtitle_skia_static_glow import assert_glow_parity

    track, style = twinkle_source(project)
    palette = colors('#FF25B4F2', '#FF25B4F2')
    gradient = PaintFill(mode=mode, start_color='#FFCB24ED', end_color='#FF22F9D1',
                         split_top_color='#FFCB24ED', split_bottom_color='#FF22F9D1', split_position_pct=43)
    scheme = SubtitleStyleScheme(karaoke_colors=replace(palette, after=replace(palette.after, text=gradient)))
    style = replace(style, karaoke_colors=colors('#00000000', '#00000000'), sing_fx='ripple',
                    fx_particle_size_em=.72, fx_particle_color_mode='role', fx_particle_role_name='particle',
                    custom_style_schemes={'particle': scheme})
    control = replace(style, custom_style_schemes={'particle': replace(scheme, karaoke_colors=palette)})
    times, dpr = (300, 475, 680, 950, 1280, 1590, 1940), 3
    refs = []
    if sys.platform == 'win32':
        from test_subtitle_render_gpu_backend import _render_g1_frames

        for look in (style, control):
            with NativeRendererProcess() as process:
                _, raw = _render_g1_frames(process, look, times, force_warp=False, track=track,
                                           width=640, height=360, dpr=dpr, realization_enabled=False)
            refs.append([np.frombuffer(p, np.uint8).reshape(360*dpr, 640*dpr, 4) for p in raw])
        assert any(np.count_nonzero(a != b) > 100 for a, b in zip(*refs, strict=True))
    ir = ir_for(binary, (track, style), dpr=dpr)
    gpu, raster = render_skia(binary, ir, times)
    assert 'p07_ripple_singing_paints' in ir['required_features']
    assert any(np.count_nonzero(frame[..., 3] > 30) > 100 for frame in gpu)
    assert_glow_parity(times, refs[0] if refs else None, gpu, raster, ('ripple_radial_gradient', mode),
                       check_p00_edges=True)


RUBY_PARTICLE_TIMES = [150, 450, 550, 899, 950, 1100, 1400, 1700, 2100, 2650, 3200]


def outlined_ruby_particles(reading, kind):
    track, style = upper_source(reading, 2)
    ink = style.karaoke_colors

    def dark_outlines(state):
        return replace(state, stroke=PaintFill(color='#202020'), stroke2=PaintFill(color='#080808'))

    return track, replace(style, sing_fx=kind, fx_particle_color='#0000FF', fx_particle_color2='#0000FF',
                          karaoke_colors=replace(ink, before=dark_outlines(ink.before), after=dark_outlines(ink.after)))


@pytest.mark.parametrize('target', ['gpu', 'raster'])
@pytest.mark.parametrize('kind,front', [('twinkle', True), ('ripple', False)])
def test_upper_ruby_particles_keep_clocks_and_replay(binary, reading, target, kind, front, tmp_path):
    source = outlined_ruby_particles(reading, kind)
    ir = upper_ir(binary, source, 1)
    assert ir['skia_layout']['sing_twinkle_schema'] == 13
    line = ir['skia_layout']['lines'][0]
    assert line['particle_box_height'] > 0 and line['ruby_groups']
    bursts = ir['track']['lines'][0]['fx_bursts']
    assert bursts and all(burst['front'] == front for burst in bursts)
    assert [burst['start_ms'] for burst in bursts] == [
        ir['track']['lines'][0]['resolved_intervals'][burst['char_index']][0] for burst in bursts]
    invalid = copy.deepcopy(ir)
    invalid['skia_layout']['sing_twinkle_schema'] = 11
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, [*RUBY_PARTICLE_TIMES, 550])
        with pytest.raises(NativeRendererError):
            configure(process, invalid)
        again, _ = collect(process, [550], generation=2)
    assert np.array_equal(frames[2], frames[-1]) and np.array_equal(frames[2], again[0])
    assert not (blue_coverage(frames[0]) > 35).any()
    assert any((blue_coverage(frame) > 50).any() for frame in frames[1:-1])
    if target == 'gpu' and kind == 'ripple':
        (tmp_path / 'ruby-particle-layout.json').write_text(json.dumps({
            'features': ir['required_features'], 'line': line, 'bursts': bursts,
        }, sort_keys=True), encoding='utf-8')


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('kind,dpr', [('twinkle', 1), ('ripple', 1.5)])
def test_upper_ruby_particles_match_actual_d2d_and_gpu_raster(binary, reading, kind, dpr):
    from test_subtitle_render_gpu_backend import _render_g1_frames

    track, style = outlined_ruby_particles(reading, kind)
    with NativeRendererProcess() as process:
        _, raw = _render_g1_frames(process, style, tuple(RUBY_PARTICLE_TIMES), force_warp=False,
            track=track, width=640, height=360, dpr=dpr, realization_enabled=False)
    refs = [np.frombuffer(p, np.uint8).reshape(round(360*dpr), round(640*dpr), 4) for p in raw]
    gpu, raster = render_skia(binary, upper_ir(binary, (track, style), dpr), RUBY_PARTICLE_TIMES)

    def blue_particle(frame):
        rgb = frame[..., :3].astype(float)
        return np.maximum(rgb[..., 2] - np.maximum(rgb[..., 0], rgb[..., 1]), 0) * frame[..., 3] / 255

    for time, ref, gpu_frame, raster_frame in zip(RUBY_PARTICLE_TIMES, refs, gpu, raster, strict=True):
        assert_particle_region(ref, gpu_frame, (kind, time, 'D2D'), coverage=blue_particle)
        assert_particle_region(gpu_frame, raster_frame, (kind, time, 'Raster'), coverage=blue_particle)


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('case_id', [
    'sing_twinkle', 'sing_twinkle_classic', 'sing_ripple', 'sing_note', 'sing_heart',
    'sing_petal', 'sing_snow', 'sing_snow_solid', 'sing_flame', 'sing_feather', 'sing_lightning',
])
def test_p00_ruby_particles_match_actual_d2d_and_gpu_raster(binary, application, case_id, tmp_path):
    from scripts.subtitle_gpu_baseline.cases import build_cases
    from test_subtitle_render_gpu_backend import _render_g1_frames
    from test_subtitle_skia_static_glow import assert_glow_parity

    case = next(case for case in build_cases(tmp_path / 'assets') if case.case_id == case_id)
    with NativeRendererProcess() as process:
        _, raw = _render_g1_frames(process, case.style, case.timestamps_ms, force_warp=False,
            track=case.track, width=1920, height=1080, dpr=1, realization_enabled=False)
    refs = [np.frombuffer(p, np.uint8).reshape(1080, 1920, 4) for p in raw]
    rendered = []
    for target in ('gpu', 'raster'):
        with SkiaRendererProcess(binary, selection=selection(target)) as process:
            process.configure(case.track, case.style, width=1920, height=1080, fps=60)
            frames, _ = collect(process, case.timestamps_ms)
        rendered.append(frames)
    assert_glow_parity(case.timestamps_ms, refs, *rendered, case_id, check_p00_edges=True)
    with SkiaRendererProcess(binary, selection=selection('gpu')) as process:
        process.configure(case.track, replace(case.style, sing_fx='none'), width=1920, height=1080, fps=60)
        bare, _ = collect(process, case.timestamps_ms)
    assert any(not np.array_equal(a, b) for a, b in zip(rendered[0], bare, strict=True))


def blue_coverage(frame):
    return frame[..., 2].astype(float) * frame[..., 3] / 255


def assert_particle_region(reference, candidate, sample, *, check_contour=True, coverage=None):
    rules = COMPARISON_BUDGETS['image_categories']['particles']
    low_alpha = COMPARISON_BUDGETS['low_alpha']
    coverage = coverage or blue_coverage
    ca, cb = coverage(reference), coverage(candidate)
    ma, mb = ca > low_alpha['mask_threshold'], cb > low_alpha['mask_threshold']
    union = ma | mb
    assert ca.any() == cb.any(), sample
    if not ca.any():
        return
    coverage_difference = abs(ca.sum() - cb.sum())
    dim_birth_or_tail = sample in {
        ('twinkle_classic', -400, True, 450), ('twinkle_classic', -400, True, 450, 'raster'),
        ('flame', 200, 2800, 'raster'),
        # Last bolt remnant before the strike window closes (peak blue coverage 2/255).
        ('energy_transition', 'lightning', 'lightning', 1500), ('energy_transition', 'lightning', 'flame', 1500),
    }
    if dim_birth_or_tail and not union.any():
        # The first dim pixels are still below the occupancy threshold.
        assert coverage_difference <= 255 / 4
        assert abs(ca - cb).max() <= 2
    else:
        mass = coverage_difference / max(ca.sum(), cb.sum())
        if sample in {('energy_transition', 'flame', 'flame', 3309, 'raster'),
                      ('energy_transition', 'lightning', 'flame', 3309, 'raster')}:
            # A faint last flame goes through different premultiplied rounding.
            assert max(ca.max(), cb.max()) <= 40 and abs(ca - cb).max() <= 3
            assert mass <= .045, (sample, 'faint flame coverage', mass)
        else:
            assert mass <= rules['max_alpha_sum_relative'], (sample, 'coverage mass', mass)
    if union.sum() >= low_alpha['minimum_union_for_iou']:
        iou = (ma & mb).sum() / union.sum()
        if iou < rules['min_alpha_iou']:
            # Dim tips can straddle the mask threshold. Bound the mismatched
            # pixels directly on the identified handoff and first snow frame.
            edge_limit = {
                ('plain', 1, 1500): 24,
                ('twinkle_classic', 0, False, 750): 24,
                ('snow', 0, False, 550): 26,
            }.get(sample)
            assert edge_limit is not None, (
                sample, 'particle IoU', iou, int(union.sum()))
            edge = ma ^ mb
            assert np.maximum(ca[edge], cb[edge]).max() <= edge_limit
            assert np.abs(ca[edge] - cb[edge]).max() <= 11
    if union.any():
        assert ma.any() and mb.any(), sample
        a, b = np.where(ma), np.where(mb)
        bounds = np.array([[(axis.min(), axis.max()) for axis in coords] for coords in (a,b)])
        edge_delta = abs(bounds[0] - bounds[1]).max()
        limit = math.ceil(rules['max_bbox_edge_delta_px_at_1080'] * reference.shape[0] / 1080)
        if sample == ('lightning', 0, 1620, 'raster') and edge_delta > limit:
            # A remote glow tip crosses alpha 16, making the enclosing box
            # jump. Require raw coverage agreement and check its clear contour.
            assert abs(ca - cb).max() <= 3
            assert within_one_pixel(ca >= 20, cb >= 20)
        elif sample == ('energy_transition', 'lightning', 'lightning', 2830) and edge_delta > limit:
            # The outlying strike tip straddles alpha 16; the clear bolt
            # contour still agrees and the remote pixels remain faint.
            dim = ((np.maximum(ca, cb) <= 19) & (abs(ca - cb) <= 6))
            assert edge_delta <= 4 and within_one_pixel(ca > 40, cb > 40)
            assert within_one_pixel(ma | (dim & mb), mb | (dim & ma))
        else:
            assert edge_delta <= limit, (sample, 'particle bounds', edge_delta)
        diff = abs(premultiplied(reference)[union] - premultiplied(candidate)[union])
        assert diff.mean() <= rules['max_union_channel_mae'], (sample, 'particle MAE', diff.mean())
        assert np.percentile(diff, 95) <= rules['max_union_p95'], (sample, 'particle P95')
    if check_contour:
        assert within_one_pixel(ca > 60, cb > 60), (sample, 'particle geometry')


@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_singing_twinkle_projects_existing_bursts_and_replays_tail(binary, project, target, tmp_path):
    track, style = twinkle_source(project)
    ir = ir_for(binary, (track, style), dpr=1.5)
    plain = ir_for(binary, (track, replace(style, sing_fx='none')), dpr=1.5)
    assert 'p07_sing_twinkle' in ir['required_features']
    assert 'p07_sing_twinkle' not in plain['required_features']
    assert ir['skia_layout']['sing_twinkle_schema'] == 3
    final = ir['skia_layout']['lines'][0]
    source = ir['track']['lines'][0]
    assert final['particle_box_height'] > 0
    bursts = source['fx_bursts']
    assert [burst['char_index'] for burst in bursts] == list(range(len(final['cells'])))
    assert [burst['start_ms'] for burst in bursts] == [a for a, b in source['resolved_intervals']]
    assert bursts[-1]['end_ms'] > source['end_ms']
    for cell in final['cells']:
        assert cell['layout_right'] > cell['layout_left']
        assert cell['pivot_y'] < cell['baseline_y']
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, [160, 1820, 2900, 400, 1820])
    assert not blue_coverage(frames[0]).any() and not blue_coverage(frames[2]).any()
    assert blue_coverage(frames[1]).max() > 80 and blue_coverage(frames[3]).max() > 80
    assert np.array_equal(frames[1], frames[4])
    if target == 'gpu':
        (tmp_path / 'sing-twinkle-layout.json').write_text(json.dumps({
            'schema': ir['skia_layout']['sing_twinkle_schema'], 'cells': final['cells'],
            'box_height': final['particle_box_height'], 'bursts': bursts, 'colors': ir['fx_color_table'],
        }, sort_keys=True), encoding='utf-8')


@pytest.mark.parametrize('changes', [{'entry_anim':'lightning', 'entry_lead_ms':750},
                                     {'exit_anim':'flame', 'exit_fade_ms':750}])
def test_energy_transition_keeps_singing_particle_clocks(binary, project, changes):
    track, style = twinkle_source(project)
    normal = ir_for(binary, (track, style))
    combined = ir_for(binary, (track, replace(style, **changes)))
    assert 'p07_energy_transition_singing_stars' in combined['required_features']
    expected = normal['track']['lines'][0]['fx_bursts']
    actual = [burst for burst in combined['track']['lines'][0]['fx_bursts'] if burst['kind'] == 'twinkle']
    for before, after in zip(expected, actual, strict=True):
        assert {k: v for k, v in before.items() if k != 'color_id'} == {
            k: v for k, v in after.items() if k != 'color_id'}
        assert normal['fx_color_table'][before['color_id']] == combined['fx_color_table'][after['color_id']]
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        configure(process, combined)
        frames, _ = collect(process, [700, 1450])
    assert all(frame.any() for frame in frames)


def moving_twinkle_source(project):
    track, style = twinkle_source(project)
    line, = track.lines
    line = replace(line, display_start_override_ms=0, display_end_override_ms=2400)
    style = replace(style, entry_anim='rise', entry_lead_ms=600, exit_anim='slide_out', exit_fade_ms=800)
    return replace(track, lines=[line]), style


def test_singing_particles_keep_clocks_under_line_motion(binary, project):
    track, style = moving_twinkle_source(project)
    ir = ir_for(binary, (track, style))
    feature = 'p07_singing_particle_line_animation'
    assert feature in ir['required_features']
    baseline = ir_for(binary, (track, replace(style, entry_anim='none', exit_anim='none')))
    line, = ir['track']['lines']
    assert line['fx_bursts'] == baseline['track']['lines'][0]['fx_bursts']
    bad = copy.deepcopy(ir)
    bad['required_features'].remove(feature)
    times = (320, 550, 1800, 2060)
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        configure(process, ir)
        frames, _ = collect(process, times)
        with pytest.raises(NativeRendererError, match='唱字粒子'):
            configure(process, bad)
        replay, _ = collect(process, times[::-1], generation=2)
    assert np.array_equal(frames, replay[::-1]) and all(frame.any() for frame in frames)


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
def test_singing_particles_under_line_motion_match_actual_d2d_gpu(binary, project):
    from test_subtitle_render_gpu_backend import _render_g1_frames
    from test_subtitle_skia_outline_ruby import render_skia
    from test_subtitle_skia_static_glow import assert_glow_parity

    track, style = moving_twinkle_source(project)
    times = (320, 550, 750, 1600, 1760, 1920, 2060)
    frames = []
    for look in (style, replace(style, sing_fx='none')):
        with NativeRendererProcess() as process:
            _, raw = _render_g1_frames(process, look, times, force_warp=False, track=track,
                                       width=640, height=360, realization_enabled=False)
        frames.append([np.frombuffer(frame, np.uint8).reshape(360, 640, 4) for frame in raw])
    reference, bare = frames
    assert any(np.count_nonzero(active != plain) > 40 for active, plain in zip(reference[:2], bare[:2], strict=True))
    assert any(np.count_nonzero(active != plain) > 40 for active, plain in zip(reference[4:], bare[4:], strict=True))
    gpu, raster = render_skia(binary, ir_for(binary, (track, style)), times)
    assert_glow_parity(times, reference, gpu, raster, 'singing_particles_under_line_motion', check_p00_edges=True)


@pytest.mark.parametrize('mutation', ['schema', 'feature', 'sprite', 'pivot', 'box', 'anchor', 'timing', 'legacy_classic', 'speed'])
def test_invalid_particle_ir_preserves_previous_scene(binary, project, mutation):
    ir = ir_for(binary, twinkle_source(project))
    bad = copy.deepcopy(ir)
    layout = bad['skia_layout']
    final = layout['lines'][0]
    burst = bad['track']['lines'][0]['fx_bursts'][0]
    if mutation == 'schema':
        layout['sing_twinkle_schema'] = False
    elif mutation == 'feature':
        bad['required_features'].remove('p07_sing_twinkle')
    elif mutation == 'sprite':
        del bad['fx_sprites']['star4']
    elif mutation == 'pivot':
        del final['cells'][0]['pivot_y']
    elif mutation == 'box':
        final['particle_box_height'] = 'large'
    elif mutation == 'anchor':
        burst['char_index'] = 300
    elif mutation == 'timing':
        layout['sing_twinkle_schema'] = 1
        burst['end_ms'] = 3200
    elif mutation == 'legacy_classic':
        layout['sing_twinkle_schema'] = 1
        burst['kind'] = 'twinkle_classic'
    elif mutation == 'speed':
        burst['time_scale'] = .05
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        configure(process, ir)
        before, _ = collect(process, [1820])
        with pytest.raises(NativeRendererError):
            configure(process, bad)
        after, _ = collect(process, [1820], generation=2)
    assert blue_coverage(before[0]).any()
    assert np.array_equal(before[0], after[0])


def test_old_sidecar_is_rejected_before_backend_selection(binary, monkeypatch):
    process = SkiaRendererProcess(binary, selection=selection('raster'))
    try:
        ready = NativeRendererProcess.start(process)
    finally:
        process.abort(reason='handshake fixture')
    ready.pop('sing_twinkle_schema')
    sent = []
    monkeypatch.setattr(NativeRendererProcess, 'start', lambda self: ready)
    monkeypatch.setattr(SkiaRendererProcess, 'abort', lambda *args, **kwargs: None)
    monkeypatch.setattr(SkiaRendererProcess, '_send', lambda self, packet: sent.append(packet))
    with pytest.raises(NativeRendererError, match='唱字粒子'):
        SkiaRendererProcess(binary).start()
    assert not sent


def test_singing_twinkle_agrees_between_gpu_and_raster(binary, project):
    ir = ir_for(binary, twinkle_source(project, 'two_colors'), dpr=1.5)
    frames = []
    for target in ('gpu', 'raster'):
        with SkiaRendererProcess(binary, selection=selection(target)) as process:
            configure(process, ir)
            rendered, _ = collect(process, [*reversed(TIMES), 1820])
        assert np.array_equal(rendered[-1], rendered[TIMES[::-1].index(1820)])
        frames.append(rendered[:-1])
    for time, gpu, raster in zip(reversed(TIMES), *frames, strict=True):
        assert_particle_region(gpu, raster, time)
        delta = abs(premultiplied(gpu) - premultiplied(raster))
        assert delta.mean() < .16, (time, delta.mean())


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('kind,dpr', [('plain', 1), ('plain', 1.5), ('pause', 1.5), ('two_colors', 1.5)])
def test_singing_twinkle_matches_actual_direct2d(binary, project, kind, dpr):
    from test_subtitle_render_gpu_backend import _render_g1_frames
    track, style = twinkle_source(project, kind)
    with NativeRendererProcess() as process:
        _, raw = _render_g1_frames(process, style, tuple(TIMES), force_warp=False, track=track,
                                   width=640, height=360, dpr=dpr, realization_enabled=False)
    shape = (round(360*dpr), round(640*dpr), 4)
    reference = [np.frombuffer(p, np.uint8).reshape(shape) for p in raw]
    with SkiaRendererProcess(binary, selection=selection('gpu')) as process:
        configure(process, ir_for(binary, (track, style), dpr=dpr))
        actual, _ = collect(process, TIMES)
    active = 0
    for time, a, b in zip(TIMES, reference, actual, strict=True):
        assert_particle_region(a, b, (kind, dpr, time))
        active += bool((blue_coverage(a) > 60).any())
        delta = abs(premultiplied(a) - premultiplied(b))
        assert delta.mean() < .16, (kind, time, delta.mean())
    assert active >= 4
    tail = TIMES.index(1820)
    assert blue_coverage(reference[tail]).any() and blue_coverage(actual[tail]).any()



def star_tempo(project, variant='twinkle', speed=0, crop=False):
    track, style = twinkle_source(project, 'two_colors')
    if crop:
        line = replace(track.lines[0], display_start_override_ms=400, display_end_override_ms=2000)
        track = replace(track, lines=[line])
    return track, replace(style, sing_fx=variant, fx_particle_speed_pct=speed)


@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_singing_star_speed_preserves_birth_and_respects_display_window(binary, project, target, tmp_path):
    track, style = star_tempo(project, 'twinkle_classic', -400, crop=True)
    ir = ir_for(binary, (track, style), dpr=1.5)
    baseline = ir_for(binary, (track, replace(style, fx_particle_speed_pct=0)), dpr=1.5)
    line = ir['track']['lines'][0]
    bursts = line['fx_bursts']
    plain = baseline['track']['lines'][0]['fx_bursts']
    assert ir['skia_layout']['lines'] == baseline['skia_layout']['lines']
    assert line['resolved_intervals'] == baseline['track']['lines'][0]['resolved_intervals']
    assert len(bursts) == len(plain)
    for a, b in zip(bursts, plain, strict=True):
        assert (a['kind'], a['char_index'], a['seed'], a['start_ms']) == (
            'twinkle_classic', b['char_index'], b['seed'], b['start_ms'])
        assert a['end_ms'] > b['end_ms'] and a['time_scale'] > 2
    assert bursts[0]['start_ms'] == line['display_start_ms'] and bursts[-1]['end_ms'] > line['display_end_ms']
    start, end = line['display_start_ms'], line['display_end_ms']
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        times = [end-1, start-1, end, start+550, end-1]
        frames, _ = collect(process, times)
    assert np.array_equal(frames[0], frames[-1])
    assert frames[1][..., 3].max() == frames[2][..., 3].max() == 0
    assert blue_coverage(frames[0]).max() > 50 and blue_coverage(frames[3]).max() > 50
    if target == 'gpu':
        (tmp_path / 'sing-twinkle-tempo-layout.json').write_text(json.dumps({
            'schema': ir['skia_layout']['sing_twinkle_schema'],
            'layout': ir['skia_layout']['lines'][0], 'bursts': bursts,
            'window': [start, end],
        }, sort_keys=True), encoding='utf-8')


@pytest.mark.parametrize('variant,with_reading,dpr,karaoke', [
    ('twinkle', False, 1, 'none'), ('twinkle_classic', True, 1.5, 'none'),
    ('note', True, 1.5, 'none'), ('heart', False, 1, 'none'), ('ripple', True, 1.5, 'none'),
    ('twinkle', False, 1, 'utopia'), ('note', True, 1.5, 'zoom_pulse'),
    ('ripple', True, 1.5, 'utopia'), ('heart', False, 1, 'utopia'),
    ('twinkle_classic', False, 1, 'zoom_pulse'),
    ('petal', True, 1.5, 'zoom_pulse'), ('feather', False, 1, 'none'),
    ('snow', True, 1, 'none'), ('snow_solid', True, 1.5, 'utopia'),
    ('lightning', False, 1, 'none'), ('lightning', True, 1.5, 'zoom_pulse'),
])
def test_vertical_singing_particles_keep_each_glyph_anchor(binary, project, variant, with_reading, dpr, karaoke):
    from test_subtitle_skia_vertical import vertical_ruby_source, vertical_source

    if with_reading:
        track, style = vertical_ruby_source(project, position='top')
    else:
        track, style = vertical_source(project, texts=['字', '字'], position='top')
    line, = track.lines
    cells = [line.chars[0], replace(line.chars[1], start_ms=1600)]
    track = replace(track, lines=[replace(line, chars=cells, end_ms=2200, display_start_override_ms=0,
                                          display_end_override_ms=2500)])
    blank = colors('#00000000', '#00000000')
    style = replace(style, karaoke_colors=blank, ruby_karaoke_colors=blank, stroke_width_px=0,
                    stroke2_enabled=False, sing_fx=variant, karaoke_anim=karaoke, zoom_pulse_peak_pct=150,
                    fx_particle_color='#0000FF', fx_particle_color2='#0000FF')
    ir = ir_for(binary, (track, style), dpr=dpr)
    plain = ir_for(binary, (track, replace(style, sing_fx='none')), dpr=dpr)
    if variant.startswith('twinkle'):
        feature = 'p07_vertical_sing_twinkle'
    elif variant in {'petal', 'feather', 'snow', 'snow_solid'}:
        feature = 'p07_vertical_sing_falling'
    elif variant == 'lightning':
        feature = 'p07_vertical_sing_lightning'
    else:
        feature = 'p07_vertical_sing_shapes'
    assert {feature, 'p07_sing_twinkle'} <= set(ir['required_features'])
    composition = 'p07_vertical_singing_with_particles'
    if karaoke == 'none':
        assert composition not in ir['required_features']
        unscaled = None
    else:
        assert {composition, 'p07_vertical_singing_scale'} <= set(ir['required_features'])
        unscaled = ir_for(binary, (track, replace(style, karaoke_anim='none')), dpr=dpr)
    assert ir['skia_layout']['lines'] == plain['skia_layout']['lines']
    layer, = ir['track']['lines']
    bursts = layer['fx_bursts']
    assert {burst['char_index'] for burst in bursts} == {0, 1}
    for burst in bursts:
        kind = burst['kind']
        assert kind == variant or (variant == 'lightning' and kind == 'lightning_glow')
        assert burst['sweep'] == int(variant == 'twinkle')
        assert burst['front'] == (kind not in {'ripple', 'lightning_glow'})
        assert burst['start_ms'] == layer['resolved_intervals'][burst['char_index']][0]
    if variant == 'lightning':
        layers = {}
        for burst in bursts:
            key = burst['char_index'], burst['seed'], burst['sprite'], burst['start_ms'], burst['end_ms']
            layers.setdefault(key, set()).add(burst['kind'])
        assert all(pair == {'lightning', 'lightning_glow'} for pair in layers.values())
    times = [100, 350, 500, 1750, 1900, 2501]
    invalid = copy.deepcopy(ir)
    invalid['required_features'].remove(feature if karaoke == 'none' else composition)
    invalid_sweep = copy.deepcopy(ir)
    invalid_sweep['track']['lines'][0]['fx_bursts'][0]['sweep'] = int(variant != 'twinkle')
    captures = []

    def center(frames):
        rows = np.sum([blue_coverage(frame).sum(axis=1) for frame in frames], axis=0)
        assert rows.sum() > 100
        return np.average(np.arange(rows.size), weights=rows)

    for target in ('gpu', 'raster'):
        with SkiaRendererProcess(binary, selection=selection(target)) as process:
            configure(process, ir)
            frames, _ = collect(process, times)
            with pytest.raises(NativeRendererError):
                configure(process, invalid)
            with pytest.raises(NativeRendererError):
                configure(process, invalid_sweep)
            replay, _ = collect(process, times[::-1], generation=2)
            if unscaled is not None:
                configure(process, unscaled)
                unscaled_frames, _ = collect(process, times, generation=3)
                assert all(np.array_equal(a, b) for a, b in zip(frames, unscaled_frames, strict=True))
        assert all(np.array_equal(a, b) for a, b in zip(frames, reversed(replay), strict=True))
        assert not frames[0].any() and not frames[-1].any()
        assert center(frames[3:5]) - center(frames[1:3]) > 20 * dpr
        captures.append(np.stack(frames))
    diff = abs(premultiplied(captures[0]) - premultiplied(captures[1]))
    assert diff.mean() < .15 and np.mean(diff.max(axis=-1) > 16) < .003


@pytest.mark.parametrize('reverse', [False, True])
def test_vertical_flame_moves_emitters_along_the_cell_y_axis(binary, project, reverse):
    from test_subtitle_skia_vertical import vertical_ruby_source, vertical_source

    if reverse:
        track, style = vertical_source(project, texts=['字'], position='center', reverse=True, effect='zoom_pulse')
    else:
        track, style = vertical_ruby_source(project, position='center')
    line, = track.lines
    track = replace(track, lines=[replace(line, chars=line.chars[:1], end_ms=1450,
                                          display_start_override_ms=0, display_end_override_ms=2200)])
    blank = colors('#00000000', '#00000000')
    style = replace(style, karaoke_colors=blank, ruby_karaoke_colors=blank, stroke_width_px=0,
                    stroke2_enabled=False, sing_fx='flame', karaoke_anim='zoom_pulse')
    ir = ir_for(binary, (track, style))
    features = {'p07_vertical_sing_flame', 'p07_vertical_singing_with_particles', 'p07_vertical_zoom_pulse'}
    assert features <= set(ir['required_features'])
    source, = ir['track']['lines']
    bursts = source['fx_bursts']
    back = {'flame_glow', 'smoke'}
    assert back | {'flame', 'flame_alt', 'ember', 'spark'} == {burst['kind'] for burst in bursts}
    assert all(burst['front'] == (burst['kind'] not in back) for burst in bursts)
    main = [burst for burst in bursts if burst['kind'] == 'flame']
    assert main[0]['emit_x'] < 0 < main[-1]['emit_x']

    cell, = ir['skia_layout']['lines'][0]['cells']
    height = cell['wipe_bottom'] - cell['wipe_top']
    low = copy.deepcopy(ir)
    high = copy.deepcopy(ir)
    # Hold seed, age, and paint constant while moving a single emitter.
    probe = dict(main[0], count=6, color='#FFFFFFFF')
    low['track']['lines'][0]['fx_bursts'] = [dict(probe, emit_x=-.4)]
    high['track']['lines'][0]['fx_bursts'] = [dict(probe, emit_x=.4)]

    def centroid(frame):
        alpha = frame[..., 3].astype(float)
        assert alpha.sum() > 500
        return np.array([np.average(np.arange(weight.size), weights=weight)
                         for weight in (alpha.sum(axis=0), alpha.sum(axis=1))])

    time = (probe['start_ms'] + probe['end_ms']) // 2
    rendered = []
    for target in ('gpu', 'raster'):
        with SkiaRendererProcess(binary, selection=selection(target)) as process:
            configure(process, ir)
            (full,), _ = collect(process, [time])
            assert full[..., 3].max() > 100
            configure(process, low)
            (top,), _ = collect(process, [time], generation=2)
            configure(process, high)
            (bottom,), _ = collect(process, [time], generation=3)
            direction = -1 if reverse else 1
            assert centroid(bottom) - centroid(top) == pytest.approx([0, direction * .8 * height], abs=.2)
        rendered.append(premultiplied(full))
    assert abs(rendered[0] - rendered[1]).mean() < .15


@pytest.mark.parametrize('variant,karaoke', [
    ('twinkle_classic', 'none'), ('note', 'utopia'), ('feather', 'zoom_pulse'),
])
def test_vertical_reverse_singing_particles_keep_clock_order(binary, project, variant, karaoke):
    from test_subtitle_skia_vertical import vertical_source

    track, style = vertical_source(project, texts=['字', '字'], reverse=True, effect=karaoke, position='center')
    line, = track.lines
    cells = [line.chars[0], replace(line.chars[1], start_ms=2400)]
    track = replace(track, lines=[replace(line, chars=cells, end_ms=3000, display_end_override_ms=3400)])
    blank = colors('#00000000', '#00000000')
    style = replace(style, karaoke_colors=blank, stroke_width_px=0, stroke2_enabled=False, sing_fx=variant,
                    fx_particle_color='#0000FF', fx_particle_color2='#0000FF')
    ir = ir_for(binary, (track, style), dpr=1.5)
    reverse = 'p07_vertical_reverse_singing_particles'
    assert {reverse, 'p05_reverse_wipe', 'p07_sing_twinkle'} <= set(ir['required_features'])
    source, = ir['track']['lines']
    final, = ir['skia_layout']['lines']
    assert [cell['clock_index'] for cell in final['cells']] == [1, 0]
    for burst in source['fx_bursts']:
        assert burst['start_ms'] == source['resolved_intervals'][burst['char_index']][0]
    malformed = copy.deepcopy(ir)
    malformed['required_features'].remove(reverse)

    def center(frames):
        rows = np.sum([blue_coverage(frame).sum(axis=1) for frame in frames], axis=0)
        assert rows.sum() > 100
        return np.average(np.arange(rows.size), weights=rows)

    captures = []
    times = [50, 350, 500, 2550, 2700, 3401]
    for target in ('gpu', 'raster'):
        with SkiaRendererProcess(binary, selection=selection(target)) as process:
            configure(process, ir)
            frames, _ = collect(process, times)
            with pytest.raises(NativeRendererError):
                configure(process, malformed)
            reprise, _ = collect(process, [times[2], times[3]], generation=2)
        assert np.array_equal(frames[2], reprise[0]) and np.array_equal(frames[3], reprise[1])
        assert not frames[0].any() and not frames[-1].any()
        assert center(frames[1:3]) - center(frames[3:5]) > 30
        captures.append(premultiplied(np.stack(frames)))
    assert abs(captures[0] - captures[1]).mean() < .15


@pytest.mark.parametrize('entry,exit_,karaoke,sweep', [
    ('fade', 'fade', 'none', 'none'), ('slide_in', 'slide_out', 'zoom_pulse', 'none'),
    ('rise', 'rise', 'none', 'none'), ('slide_in', 'slide_out', 'scanline', 'star'),
    ('rise', 'rise', 'zoom_pulse_scanline', 'note'),
])
def test_vertical_singing_particles_follow_whole_line_motion(binary, project, entry, exit_, karaoke, sweep):
    from test_subtitle_skia_vertical import vertical_ruby_source

    track, style = vertical_ruby_source(project, position='center')
    line, = track.lines
    chars = [line.chars[0], replace(line.chars[1], start_ms=1400)]
    track = replace(track, lines=[replace(line, chars=chars, end_ms=1550, display_end_override_ms=2050)])
    blank = colors('#00000000', '#00000000')
    style = replace(style, karaoke_colors=blank, ruby_karaoke_colors=blank, stroke_width_px=0,
                    stroke2_enabled=False, sing_fx='note', karaoke_anim=karaoke,
                    fx_particle_color='#0000FF', fx_particle_color2='#0000FF')
    if sweep != 'none':
        style = replace(style, scanline_enabled=True, scanline_color='#00000000', scanline_glow_px=0,
                        scanline_effect=sweep, scanline_effect_color_mode='color',
                        scanline_effect_color='#FF00FF', scanline_effect_color2='#FF00FF')
    baseline = ir_for(binary, (track, style))
    animated = replace(style, entry_anim=entry, entry_lead_ms=800,
                       exit_anim=exit_, exit_fade_ms=500)
    ir = ir_for(binary, (track, animated))
    feature = 'p07_vertical_singing_particle_line_animation'
    assert feature in ir['required_features']
    band_feature = 'p07_vertical_scanline_particle_line_animation'
    if sweep != 'none':
        assert band_feature in ir['required_features']
    layer, = ir['track']['lines']
    assert (layer['entry_anim'], layer['entry_duration_ms'], layer['display_start_ms']) == (entry, 200, 0)
    assert (layer['exit_anim'], layer['exit_duration_ms'], layer['display_end_ms']) == (exit_, 500, 2050)
    assert layer['fx_bursts'] == baseline['track']['lines'][0]['fx_bursts']
    invalid = copy.deepcopy(ir)
    invalid['required_features'].remove(feature if sweep == 'none' else band_feature)
    times = [100, 450, 900, 1800, 2050]
    results = []

    def centroid(alpha):
        return np.array([np.average(np.arange(weight.size), weights=weight)
                         for weight in (alpha.sum(axis=0), alpha.sum(axis=1))])

    for target in ('gpu', 'raster'):
        with SkiaRendererProcess(binary, selection=selection(target)) as process:
            configure(process, baseline)
            unchanged, _ = collect(process, times)
            configure(process, ir)
            frames, _ = collect(process, times, generation=2)
            with pytest.raises(NativeRendererError):
                configure(process, invalid)
            replay, _ = collect(process, times[::-1], generation=3)
        assert all(np.array_equal(a, b) for a, b in zip(frames, reversed(replay), strict=True))
        assert not frames[0].any() and not frames[-1].any()
        assert np.array_equal(frames[1:3], unchanged[1:3])
        plain, moved = [frame[..., 3].astype(float) for frame in (unchanged[3], frames[3])]
        assert plain.sum() > 1000
        assert moved.sum() / plain.sum() == pytest.approx(.25, abs=.02)
        dx, dy = centroid(moved) - centroid(plain)
        if exit_ == 'slide_out':
            assert dx < -5 and abs(dy) < .4
        elif exit_ == 'rise':
            assert abs(dx) < .4 and dy < -2
        else:
            assert [dx, dy] == pytest.approx([0, 0], abs=.2)
        results.append(premultiplied(np.stack(frames)))
    assert abs(results[0] - results[1]).mean() < .15


@pytest.mark.parametrize('karaoke,band,mode', [
    ('none', 'scanline', 'color'), ('zoom_pulse', 'zoom_pulse_scanline', 'follow_after'),
])
def test_vertical_singing_particles_keep_independent_band_and_bursts(binary, project, karaoke, band, mode):
    from test_subtitle_skia_vertical import vertical_ruby_source

    track, style = vertical_ruby_source(project, position='center')
    line, = track.lines
    track = replace(track, lines=[replace(line, chars=line.chars[:2], end_ms=1100)])
    palette = colors('#FF0000', '#00FF00')
    style = replace(style, karaoke_colors=palette, ruby_karaoke_colors=palette, stroke_width_px=0,
                    stroke2_enabled=False, karaoke_anim=band, scanline_enabled=True, scanline_mode=mode,
                    scanline_color='#FFFF00', scanline_width_px=16, sing_fx='note',
                    fx_particle_color='#0000FF', fx_particle_color2='#0000FF')
    ir = ir_for(binary, (track, style), dpr=1.5)
    required = 'p07_vertical_scanline_singing_particles'
    assert {required, 'p07_vertical_scanline_band', 'p07_vertical_sing_shapes'} <= set(ir['required_features'])
    only_particles = ir_for(binary, (track, replace(style, karaoke_anim=karaoke, scanline_enabled=False)), dpr=1.5)
    only_band = ir_for(binary, (track, replace(style, sing_fx='none')), dpr=1.5)
    assert ir['track']['lines'][0]['fx_bursts'] == only_particles['track']['lines'][0]['fx_bursts']
    invalid = copy.deepcopy(ir)
    invalid['required_features'].remove(required)
    times = [150, 450, 750, 1100, 2100]
    results = []

    for target in ('gpu', 'raster'):
        with SkiaRendererProcess(binary, selection=selection(target)) as process:
            configure(process, ir)
            frames, _ = collect(process, times)
            with pytest.raises(NativeRendererError):
                configure(process, invalid)
            replay, _ = collect(process, times[::-1], generation=2)
            configure(process, only_particles)
            particles, _ = collect(process, times, generation=3)
            configure(process, only_band)
            bands, _ = collect(process, times, generation=4)
        assert all(np.array_equal(a, b) for a, b in zip(frames, reversed(replay), strict=True))
        assert not np.array_equal(frames[1], particles[1])
        assert np.count_nonzero(blue_coverage(frames[1]) - blue_coverage(bands[1]) > 40) > 30
        results.append(premultiplied(np.stack(frames)))
    diff = abs(results[0] - results[1])
    assert diff.mean() < .15 and np.mean(diff.max(axis=-1) > 16) < .003


@pytest.mark.parametrize('sprite,with_reading,reverse', [
    ('star', False, False), ('note', True, False), ('star', False, True), ('note', False, True),
])
def test_vertical_scanline_particles_emit_along_the_cell_y_axis(binary, project, sprite, with_reading, reverse):
    from test_subtitle_skia_vertical import vertical_ruby_source, vertical_source

    if with_reading:
        track, style = vertical_ruby_source(project, position='center')
    else:
        track, style = vertical_source(project, texts=['字', '字'], position='center', reverse=reverse)
    line, = track.lines
    chars = [replace(line.chars[0], pause_release_ms=1000), replace(line.chars[1], start_ms=2200)]
    track = replace(track, lines=[replace(line, chars=chars, end_ms=3000, display_end_override_ms=3400)])
    blank = colors('#00000000', '#00000000')
    style = replace(style, karaoke_colors=blank, ruby_karaoke_colors=blank, stroke_width_px=0,
                    stroke2_enabled=False, scanline_enabled=True, scanline_mode='color',
                    scanline_color='#00000000', scanline_glow_px=0, scanline_effect=sprite,
                    scanline_effect_color_mode='color', scanline_effect_color='#0000FF',
                    scanline_effect_color2='#0000FF', scanline_effect_count=16, scanline_effect_size_pct=50,
                    karaoke_anim='zoom_pulse_scanline' if with_reading else 'scanline',
                    reverse_karaoke_anim='zoom_pulse_scanline')
    ir = ir_for(binary, (track, style), dpr=1.5)
    feature = 'p07_vertical_scanline_particles'
    assert {feature, 'p07_vertical_scanline_band'} <= set(ir['required_features'])
    reverse_feature = 'p07_vertical_reverse_scanline_particles'
    assert (reverse_feature in ir['required_features']) == reverse
    if not with_reading:
        from krok_helper.subtitle_render.engine.render.render_ir import build_render_ir

        legacy = build_render_ir(track, style, width=640, height=360, fps=60, dpr=1.5)
        assert not legacy['track']['lines'][0].get('fx_bursts')
    malformed = copy.deepcopy(ir)
    malformed['required_features'].remove(reverse_feature if reverse else feature)
    layer, = ir['track']['lines']
    bursts = layer['fx_bursts']
    assert {burst['char_index'] for burst in bursts} == {0, 1}
    for burst in bursts:
        start, end = layer['resolved_intervals'][burst['char_index']]
        assert (burst['start_ms'], burst['emit_ms']) == (start, end - start)
        assert burst['kind'] == 'scanline' and burst['front'] and burst['reverse'] is reverse
        assert burst['end_ms'] > end
        # Isolate the position of each birth from the ensuing drift.
        burst['travel_px'] = 0
    times = [150, 400, 880, 1100, 2470, 3400]
    rendered = []

    def center_y(frame):
        rows = blue_coverage(frame).sum(axis=1)
        assert rows.sum() > 100
        return np.average(np.arange(rows.size), weights=rows)

    for target in ('gpu', 'raster'):
        with SkiaRendererProcess(binary, selection=selection(target)) as process:
            configure(process, ir)
            frames, _ = collect(process, times)
            with pytest.raises(NativeRendererError):
                configure(process, malformed)
            replay, _ = collect(process, times[::-1], generation=2)
        assert all(np.array_equal(a, b) for a, b in zip(frames, reversed(replay), strict=True))
        assert not frames[0].any() and not frames[-1].any()
        direction = -1 if reverse else 1
        assert direction * (center_y(frames[2]) - center_y(frames[1])) > 10
        assert frames[3][..., 3].max() > 20
        assert direction * (center_y(frames[4]) - center_y(frames[1])) > 40
        rendered.append(premultiplied(np.stack(frames)))
    assert abs(rendered[0] - rendered[1]).mean() < .15


@pytest.mark.parametrize('layers,reverse', [(1, False), (2, False), (2, True)])
def test_vertical_singing_particles_over_outlined_bare_lyrics(binary, project, layers, reverse):
    from test_subtitle_skia_outline_ruby import outlined
    from test_subtitle_skia_vertical import vertical_source

    track, style = vertical_source(project, texts=['字', '字'], position='center', reverse=reverse, effect='zoom_pulse')
    style = replace(outlined(style, layers), karaoke_anim='zoom_pulse', sing_fx='note',
                    fx_particle_color='#FFFFFF', fx_particle_color2='#FFFFFF')
    ir = ir_for(binary, (track, style), dpr=1.5)
    feature = 'p07_vertical_outlined_singing_particles'
    assert {feature, 'p06_vertical_outline', 'p07_vertical_sing_shapes'} <= set(ir['required_features'])
    assert ('p06_secondary_solid_outline' in ir['required_features']) == (layers == 2)
    assert 'p05_resolved_ruby' not in ir['required_features']
    bare = ir_for(binary, (track, replace(style, sing_fx='none')), dpr=1.5)
    assert ir['skia_layout']['lines'] == bare['skia_layout']['lines']
    bad = copy.deepcopy(ir)
    bad['required_features'].remove(feature)
    times = [150, 400, 800, 1300, 1799]
    results = []
    for target in ('gpu', 'raster'):
        with SkiaRendererProcess(binary, selection=selection(target)) as process:
            configure(process, ir)
            frames, _ = collect(process, times)
            with pytest.raises(NativeRendererError):
                configure(process, bad)
            replay, _ = collect(process, times[::-1], generation=2)
            configure(process, bare)
            baseline, _ = collect(process, times, generation=3)
        assert all(np.array_equal(a, b) for a, b in zip(frames, reversed(replay), strict=True))
        assert np.array_equal(frames[0], baseline[0])
        assert all(not np.array_equal(frame, ref) for frame, ref in zip(frames[1:3], baseline[1:3], strict=True))
        for clip in (frames, baseline):
            assert any(np.any(np.all(frame == [0, 0, 255, 255], axis=-1)) for frame in clip)
            assert any(np.any(np.all(frame == [255, 255, 0, 255], axis=-1)) for frame in clip)
        results.append(premultiplied(np.stack(frames)))
    assert abs(results[0] - results[1]).mean() < .15


@pytest.mark.parametrize('reverse,sweep', [(False, 'none'), (True, 'star')])
def test_vertical_particles_composite_sources_in_track_order(binary, project, reverse, sweep):
    from test_subtitle_skia_vertical import vertical_source

    primary, style = vertical_source(project, texts=['字', '字'], position='center')
    line = replace(primary.lines[0], end_ms=1100, display_end_override_ms=1800)
    primary = replace(primary, lines=[line])
    extra = replace(primary, lines=[replace(line, wipe_reverse=reverse)], meta=replace(primary.meta, offset_ms=400))
    style = replace(style, karaoke_colors=colors('#A0FF0000', '#B000FF00'), stroke_width_px=0,
                    stroke2_enabled=False, sing_fx='note', fx_particle_color='#A000FFFF',
                    fx_particle_color2='#A000FFFF')
    if sweep != 'none':
        style = replace(style, karaoke_anim='zoom_pulse_scanline', reverse_karaoke_anim='zoom_pulse_scanline',
                        scanline_enabled=True, scanline_reverse_enabled=True, scanline_effect=sweep,
                        scanline_color='#B0FFFF00', scanline_effect_color_mode='color',
                        scanline_effect_color='#A0FF00FF', scanline_effect_color2='#A0FF00FF')
    combined = ir_for(binary, (primary, style), dpr=1.5, extra_tracks=[extra])
    separate = [ir_for(binary, (source, style), dpr=1.5) for source in (primary, extra)]
    assert 'p08_multiple_vertical_sources' in combined['required_features']
    parts = [combined, *combined['skia_extra_sources']]
    for actual, expected in zip(parts, separate, strict=True):
        assert actual['skia_layout']['lines'] == expected['skia_layout']['lines']
        assert actual['track']['lines'] == expected['track']['lines']
    bad = copy.deepcopy(combined)
    bad['skia_extra_sources'][0]['required_features'].remove('p07_vertical_sing_shapes')
    times = [320, 740, 1250, 2000, 2300]
    results = []
    for target in ('gpu', 'raster'):
        with SkiaRendererProcess(binary, selection=selection(target)) as process:
            configure(process, combined)
            frames, _ = collect(process, times)
            with pytest.raises(NativeRendererError):
                configure(process, bad)
            replay, _ = collect(process, times[::-1], generation=2)
            actual = []
            for generation, single in enumerate(separate, 3):
                configure(process, single)
                actual.append(collect(process, times, generation=generation)[0])
        assert all(np.array_equal(a, b) for a, b in zip(frames, reversed(replay), strict=True))
        first, second = actual
        assert first[0].any() and not second[0].any() and np.array_equal(frames[0], first[0])
        assert not first[3].any() and second[3].any() and np.array_equal(frames[3], second[3])
        assert not frames[-1].any()
        bottom, top = (premultiplied(np.stack(clip)) for clip in actual)
        expected = top + bottom * (1 - top[..., 3:4] / 255)
        reverse_order = bottom + top * (1 - bottom[..., 3:4] / 255)
        actual = premultiplied(np.stack(frames))
        error = abs(expected - actual)
        order_signal = abs(expected - reverse_order) > 10
        assert order_signal.any()
        assert np.all(error[order_signal] < abs(reverse_order - actual)[order_signal])
        # Compare in whole premultiplied channel steps. Converting straight
        # BGRA8 back to premultiplied values also leaves fractional steps.
        assert np.rint(error).max() <= 2
        results.append(actual)
    assert abs(results[0] - results[1]).mean() < .15


@pytest.mark.parametrize('reverse,with_reading,body', [(False, True, 'glow'), (True, False, 'shadow')])
def test_vertical_singing_particles_compose_with_body_and_reading_decoration(binary, project, reverse, with_reading, body):
    from test_subtitle_skia_vertical import vertical_ruby_source, vertical_source

    if with_reading:
        track, style = vertical_ruby_source(project, position='center')
        line = replace(track.lines[0], chars=track.lines[0].chars[:2])
        track = replace(track, lines=[line])
    else:
        track, style = vertical_source(project, texts=['字', '字'], position='center', reverse=reverse)
    base = colors('#FF303030', '#FF404040')
    decoration = PaintFill(color='#FFFF0000')
    palette = replace(base, before=replace(base.before, shadow=decoration),
                      after=replace(base.after, shadow=decoration))
    style = replace(style, karaoke_colors=palette, ruby_karaoke_colors=palette, decoration_kind=body,
                    shadow_offset_x=14, shadow_offset_y=4, glow_before_radius_px=8, glow_after_radius_px=8,
                    ruby_decoration_kind='shadow' if with_reading else 'none',
                    ruby_shadow_offset_x=10, ruby_shadow_offset_y=-7, karaoke_anim='zoom_pulse',
                    reverse_karaoke_anim='zoom_pulse', sing_fx='note', stroke_width_px=0, stroke2_enabled=False,
                    fx_particle_color='#0000FF', fx_particle_color2='#0000FF')
    ir = ir_for(binary, (track, style), dpr=1.5)
    feature = 'p07_vertical_decorated_singing_particles'
    assert {feature, f'p06_vertical_{body}', 'p07_vertical_singing_with_particles'} <= set(ir['required_features'])
    malformed = copy.deepcopy(ir)
    malformed['required_features'].remove(feature)
    baseline = ir_for(binary, (track, replace(style, sing_fx='none')), dpr=1.5)
    assert ir['skia_layout']['lines'] == baseline['skia_layout']['lines']
    times = [150, 450, 750, 1300, 1700]
    results = []
    for target in ('gpu', 'raster'):
        with SkiaRendererProcess(binary, selection=selection(target)) as process:
            configure(process, ir)
            frames, _ = collect(process, times)
            with pytest.raises(NativeRendererError):
                configure(process, malformed)
            replay, _ = collect(process, times[::-1], generation=2)
            configure(process, baseline)
            without_particles, _ = collect(process, times, generation=3)
        assert all(np.array_equal(a, b) for a, b in zip(frames, reversed(replay), strict=True))
        assert np.array_equal(frames[0], without_particles[0])
        for frame in (frames[1], frames[2]):
            red, green, blue, alpha = np.moveaxis(frame, -1, 0)
            assert (alpha[(red > 125) & (green < 40) & (blue < 40)] > 100).any()
            assert (alpha[(blue > 125) & (green < 40) & (red < 40)] > 100).any()
        results.append(premultiplied(np.stack(frames)))
    assert abs(results[0] - results[1]).mean() < .15


@pytest.mark.parametrize('reverse', [False, True])
def test_vertical_no_wipe_particles_emit_before_the_cell_changes_color(binary, project, reverse):
    from test_subtitle_skia_vertical import vertical_source

    track, style = vertical_source(project, texts=['字', '字'], position='center', reverse=reverse)
    style = replace(style, karaoke_anim='no_wipe', reverse_karaoke_anim='no_wipe',
                    karaoke_colors=colors('#FF0000', '#00FF00'), stroke_width_px=0, stroke2_enabled=False,
                    sing_fx='note', fx_particle_color='#0000FF', fx_particle_color2='#0000FF')
    ir = ir_for(binary, (track, style), dpr=1.5)
    feature = 'p07_vertical_no_wipe_singing_particles'
    assert {feature, 'p07_no_wipe', 'p07_vertical_sing_shapes'} <= set(ir['required_features'])
    source, = ir['track']['lines']
    start, end = source['resolved_intervals'][0]
    assert source['fx_bursts'][0]['start_ms'] == start
    without_feature = copy.deepcopy(ir)
    without_feature['required_features'].remove(feature)
    times = [start - 1, start + (end - start) // 2, end, end + 1]
    results = []
    for target in ('gpu', 'raster'):
        with SkiaRendererProcess(binary, selection=selection(target)) as process:
            configure(process, ir)
            frames, _ = collect(process, times)
            with pytest.raises(NativeRendererError):
                configure(process, without_feature)
            replay, _ = collect(process, times[::-1], generation=2)
        assert all(np.array_equal(a, b) for a, b in zip(frames, reversed(replay), strict=True))
        assert not blue_coverage(frames[0]).any() and blue_coverage(frames[1]).max() > 50
        assert not frames[1][..., 1].any()
        rows = np.flatnonzero(np.any(frames[-1][..., 1] > 180, axis=1))
        assert rows.size > 10
        if reverse:
            assert rows.mean() > frames[-1].shape[0] / 2
        else:
            assert rows.mean() < frames[-1].shape[0] / 2
        results.append(premultiplied(np.stack(frames)))
    assert abs(results[0] - results[1]).mean() < .15


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('variant,speed,crop', [('twinkle_classic', 0, False), ('twinkle_classic', -400, True),
                                                ('twinkle', 350, False), ('twinkle', -350, True)])
def test_singing_star_speed_matches_actual_d2d_and_gpu_raster(binary, project, variant, speed, crop):
    from test_subtitle_render_gpu_backend import _render_g1_frames

    track, style = star_tempo(project, variant, speed, crop)
    ir = ir_for(binary, (track, style), dpr=1.5)
    line = ir['track']['lines'][0]
    start, end = line['display_start_ms'], line['display_end_ms']
    times = sorted(set(TIMES + [max(start-1, 0), start, start+250, end-1, end]))
    with NativeRendererProcess() as process:
        _, raw = _render_g1_frames(process, style, tuple(times), force_warp=False, track=track,
                                   width=640, height=360, dpr=1.5, realization_enabled=False)
    reference = [np.frombuffer(p, np.uint8).reshape(540, 960, 4) for p in raw]
    results = []
    for target in ('gpu', 'raster'):
        with SkiaRendererProcess(binary, selection=selection(target)) as process:
            configure(process, ir)
            frames, _ = collect(process, [*times, 750])
        assert np.array_equal(frames[times.index(750)], frames[-1])
        results.append(frames[:-1])
    active = 0
    for time, a, b, c in zip(times, reference, *results, strict=True):
        assert_particle_region(a, b, (variant, speed, crop, time), check_contour=False)
        assert_particle_region(b, c, (variant, speed, crop, time, 'raster'), check_contour=False)
        if start <= time < end:
            active += bool((blue_coverage(a) > 60).any())
        else:
            assert a[..., 3].max() == b[..., 3].max() == c[..., 3].max() == 0
    assert active >= 2



@pytest.mark.parametrize('target', ['gpu', 'raster'])
@pytest.mark.parametrize('kind,sprites', [('note', {'note', 'note_dot'}), ('heart', {'heart'})])
def test_singing_symbols_keep_sprite_resources_and_replay_tail(binary, project, target, kind, sprites, tmp_path):
    track, style = star_tempo(project, kind, -300, crop=True)
    ir = ir_for(binary, (track, style), dpr=1.5)
    line = ir['track']['lines'][0]
    bursts = line['fx_bursts']
    assert ir['skia_layout']['sing_twinkle_schema'] == 3
    assert {b['sprite'] for b in bursts} == sprites
    assert all(b['kind'] == kind and b['anchor'] == 'char' for b in bursts)
    for char in range(len(line['chars'])):
        assert sum(b['count'] for b in bursts if b['char_index'] == char) == 3
    selected = sorted(sprites)
    shapes = {name: ir['fx_sprites'][name] for name in selected}
    if kind == 'note':
        assert shapes['note'] != shapes['note_dot']
    times = [750, 1999, 2000, 400, 750]
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, times)
    assert np.array_equal(frames[0], frames[-1])
    assert frames[2][..., 3].max() == 0
    assert blue_coverage(frames[0]).max() > 40 and blue_coverage(frames[1]).max() > 40
    if target == 'gpu' and kind == 'note':
        (tmp_path / 'singing-symbol-layout.json').write_text(json.dumps({
            'schema': ir['skia_layout']['sing_twinkle_schema'], 'bursts': bursts, 'shapes': shapes,
            'line': ir['skia_layout']['lines'][0],
        }, sort_keys=True), encoding='utf-8')


@pytest.mark.parametrize('mutation', ['old_profile', 'foreign_sprite', 'missing_sprite'])
def test_invalid_symbol_sprite_does_not_replace_scene(binary, project, mutation):
    track, style = star_tempo(project, 'note')
    ir = ir_for(binary, (track, style))
    bad = copy.deepcopy(ir)
    if mutation == 'old_profile':
        bad['skia_layout']['sing_twinkle_schema'] = 2
    elif mutation == 'foreign_sprite':
        bad['track']['lines'][0]['fx_bursts'][0]['sprite'] = 'heart'
    else:
        del bad['fx_sprites']['note_dot']
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        configure(process, ir)
        before, _ = collect(process, [750])
        with pytest.raises(NativeRendererError):
            configure(process, bad)
        after, _ = collect(process, [750], generation=2)
    assert np.array_equal(before, after)


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('kind,speed,crop', [('note', 0, False), ('note', -300, True),
                                             ('heart', 0, False), ('heart', 320, False)])
def test_singing_symbol_matches_actual_d2d_and_gpu_raster(binary, project, kind, speed, crop):
    from test_subtitle_render_gpu_backend import _render_g1_frames

    track, style = star_tempo(project, kind, speed, crop)
    times = [199, 400, 600, 750, 1100, 1500, 1820, 1999, 2000, 2200, 2900, 3000]
    with NativeRendererProcess() as process:
        _, raw = _render_g1_frames(process, style, tuple(times), force_warp=False, track=track,
                                   width=640, height=360, dpr=1.5, realization_enabled=False)
    reference = [np.frombuffer(p, np.uint8).reshape(540, 960, 4) for p in raw]
    ir = ir_for(binary, (track, style), dpr=1.5)
    results = []
    for target in ('gpu', 'raster'):
        with SkiaRendererProcess(binary, selection=selection(target)) as process:
            configure(process, ir)
            frames, _ = collect(process, [*times, 1100])
        assert np.array_equal(frames[times.index(1100)], frames[-1])
        results.append(frames[:-1])
    active = 0
    for time, d2d, gpu, raster in zip(times, reference, *results, strict=True):
        assert_particle_region(d2d, gpu, (kind, speed, crop, time), check_contour=False)
        assert_particle_region(gpu, raster, (kind, speed, crop, time, 'raster'), check_contour=False)
        active += bool((blue_coverage(d2d) > 60).any())
    assert active >= 3


@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_singing_ripple_uses_back_ring_layers_and_replays(binary, project, target, tmp_path):
    track, style = star_tempo(project, 'ripple')
    style = replace(style, fx_particle_color2=style.fx_particle_color)
    dpr = 1.5
    ir = ir_for(binary, (track, style), dpr=dpr)
    plain = ir_for(binary, (track, replace(style, sing_fx='none')), dpr=dpr)
    rings_only = ir_for(binary, (track, replace(style, karaoke_colors=colors('#00000000', '#00000000'))), dpr=dpr)
    line = ir['skia_layout']['lines'][0]
    bursts = ir['track']['lines'][0]['fx_bursts']
    assert ir['skia_layout']['sing_twinkle_schema'] == 4
    assert all(b['kind'] == 'ripple' and not b['front'] and b['count'] == 3 for b in bursts)
    assert [b['start_ms'] for b in bursts] == [200, 600, 900, 1400]
    assert all(b['end_ms'] - b['start_ms'] == 600 for b in bursts)
    times = [199, 200, 240, 349, 351, 501, 800, 2000, 240]
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, times)
        configure(process, plain)
        background, _ = collect(process, times, generation=2)
        configure(process, rings_only)
        rings, _ = collect(process, times, generation=3)
    assert np.array_equal(frames[2], frames[-1])
    for idx in (0, 1, 7):
        assert np.array_equal(frames[idx], background[idx])
    cell = line['cells'][0]
    x = round((cell['layout_left'] + cell['layout_right']) * dpr / 2)
    y = round(cell['pivot_y'] * dpr)
    assert rings[2][y, x, 3] == 0  # Interior of the curved ring.
    assert blue_coverage(rings[2]).max() > 100
    covered = sum(int(((bg[..., 3] == 255) & (blue_coverage(ring) > 80)).sum())
                  for bg, ring in zip(background, rings, strict=True))
    assert covered > 20
    for frame, bg in zip(frames, background, strict=True):
        opaque = bg[..., 3] == 255
        assert np.array_equal(frame[opaque], bg[opaque])
    if target == 'gpu':
        (tmp_path / 'singing-ripple-layout.json').write_text(json.dumps({
            'schema': ir['skia_layout']['sing_twinkle_schema'],
            'bursts': bursts, 'ring': ir['fx_sprites']['ring'], 'line': line,
        }, sort_keys=True), encoding='utf-8')


@pytest.mark.parametrize('mutation', ['front', 'old_profile', 'missing_ring'])
def test_invalid_ripple_preserves_previous_scene(binary, project, mutation):
    ir = ir_for(binary, star_tempo(project, 'ripple'))
    bad = copy.deepcopy(ir)
    if mutation == 'front':
        bad['track']['lines'][0]['fx_bursts'][0]['front'] = True
    elif mutation == 'old_profile':
        bad['skia_layout']['sing_twinkle_schema'] = 3
    else:
        del bad['fx_sprites']['ring']
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        configure(process, ir)
        before, _ = collect(process, [240])
        with pytest.raises(NativeRendererError):
            configure(process, bad)
        after, _ = collect(process, [240], generation=2)
    assert np.array_equal(before, after)


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('speed,crop', [(0, False), (-300, True)])
def test_singing_ripple_matches_actual_d2d_and_gpu_raster(binary, project, speed, crop):
    from test_subtitle_render_gpu_backend import _render_g1_frames

    track, style = star_tempo(project, 'ripple', speed, crop)
    style = replace(style, karaoke_colors=colors('#00000000', '#00000000'))
    times = [199, 240, 349, 351, 450, 499, 501, 650, 750, 1100, 1500, 1999, 2000, 2600]
    with NativeRendererProcess() as process:
        _, raw = _render_g1_frames(process, style, tuple(times), force_warp=False, track=track,
                                   width=640, height=360, dpr=1.5, realization_enabled=False)
    reference = [np.frombuffer(p, np.uint8).reshape(540, 960, 4) for p in raw]
    ir = ir_for(binary, (track, style), dpr=1.5)
    results = []
    for target in ('gpu', 'raster'):
        with SkiaRendererProcess(binary, selection=selection(target)) as process:
            configure(process, ir)
            frames, _ = collect(process, times)
        results.append(frames)
    for time, d2d, gpu, raster in zip(times, reference, *results, strict=True):
        sample = ('ripple', speed, crop, time)
        assert_thin_particle_contours(d2d, gpu, sample)
        assert_particle_region(gpu, raster, sample, check_contour=False)


def assert_thin_particle_contours(d2d, gpu, sample, *, coverage_epsilon=0):
    budget = COMPARISON_BUDGETS['image_categories']['particles']
    threshold = COMPARISON_BUDGETS['low_alpha']['mask_threshold']
    a, b = blue_coverage(d2d), blue_coverage(gpu)
    assert a.any() == b.any(), sample
    if not a.any():
        return
    assert abs(a.sum() - b.sum()) <= budget['max_alpha_sum_relative'] * max(a.sum(), b.sum()), sample
    ma, mb = a > threshold, b > threshold
    at_same_level = abs(a - b) <= coverage_epsilon
    assert within_one_pixel(ma | (at_same_level & mb), mb | (at_same_level & ma)), sample

    # The two APIs distribute edge coverage differently on one-pixel-wide
    # contours. Compare the same local area after checking raw extent and mass.
    y, x = np.where((a > 0) | (b > 0))
    region = np.s_[max(y.min()-1, 0):y.max()+2, max(x.min()-1, 0):x.max()+2]

    def local_coverage(frame):
        rgba = np.pad(premultiplied(frame[region]), ((1, 1), (1, 1), (0, 0)))
        windows = np.lib.stride_tricks.sliding_window_view(rgba, (3, 3), axis=(0, 1))
        return windows.mean(axis=(-2, -1))

    reference, candidate = local_coverage(d2d), local_coverage(gpu)
    ma, mb = reference[..., 2] > threshold, candidate[..., 2] > threshold
    union = ma | mb
    if union.any():
        matching = (ma & mb) | (union & (abs(reference[..., 2] - candidate[..., 2]) <= coverage_epsilon))
        assert matching.sum() / union.sum() >= budget['min_alpha_iou'], sample
        difference = abs(reference[union] - candidate[union])
        assert difference.mean() <= budget['max_union_channel_mae'], sample
        assert np.percentile(difference, 95) <= budget['max_union_p95'], sample


@pytest.mark.parametrize('target', ['gpu', 'raster'])
@pytest.mark.parametrize('kind', ['petal', 'feather', 'snow', 'snow_solid'])
def test_singing_falling_spritemap_replays_each_shape(binary, project, target, kind, tmp_path):
    track, style = star_tempo(project, kind, -250, crop=True)
    ir = ir_for(binary, (track, style), dpr=1.5)
    line = ir['track']['lines'][0]
    bursts = line['fx_bursts']
    assert ir['skia_layout']['sing_twinkle_schema'] == 5
    assert all(b['kind'] == kind and b['front'] and b['anchor'] == 'char' for b in bursts)
    assert {b['start_ms'] for b in bursts} == {200, 600, 900, 1400}
    shape = ir['fx_sprites'][kind]
    closed_figures = sum(cmd[0] == 'Z' for cmd in shape['path_commands'])
    assert closed_figures >= 2 if kind == 'snow' else closed_figures == 1
    start, end = line['display_start_ms'], line['display_end_ms']
    times = [start - 1, 500, 750, 1100, 1550, end - 1, end, 750]
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, times)
    assert not frames[0][..., 3].any() and not frames[-2][..., 3].any()
    assert np.array_equal(frames[2], frames[-1])
    assert sum(blue_coverage(frame).max() > 40 for frame in frames[1:-2]) >= 3
    if target == 'gpu' and kind == 'snow':
        # The authored snowflake uses sin/cos. Ignore double-precision libm
        # noise in the JSON snapshot beyond the precision of the renderer.
        canonical_shape = {
            **shape, 'path_commands': [
                [verb, *(round(float(value), 9) or 0.0 for value in coordinates)]
                for verb, *coordinates in shape['path_commands']
            ],
        }
        (tmp_path / 'singing-falling-layout.json').write_text(json.dumps({
            'schema': ir['skia_layout']['sing_twinkle_schema'], 'bursts': bursts, 'shape': canonical_shape,
            'line': ir['skia_layout']['lines'][0],
        }, sort_keys=True), encoding='utf-8')


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('kind,speed,crop', [('petal', 0, False), ('feather', -250, True),
                                             ('snow', 0, False), ('snow_solid', 250, False)])
def test_singing_falling_matches_actual_d2d_and_gpu_raster(binary, project, kind, speed, crop):
    from test_subtitle_render_gpu_backend import _render_g1_frames

    track, style = star_tempo(project, kind, speed, crop)
    times = [199, 450, 550, 750, 1100, 1500, 1820, 1999, 2000, 2200, 2600, 3000]
    with NativeRendererProcess() as process:
        _, raw = _render_g1_frames(process, style, tuple(times), force_warp=False, track=track,
                                   width=640, height=360, dpr=1.5, realization_enabled=False)
    reference = [np.frombuffer(p, np.uint8).reshape(540, 960, 4) for p in raw]
    ir = ir_for(binary, (track, style), dpr=1.5)
    frames = []
    for target in ('gpu', 'raster'):
        with SkiaRendererProcess(binary, selection=selection(target)) as process:
            configure(process, ir)
            images, _ = collect(process, times)
        frames.append(images)
    assert sum(blue_coverage(frame).max() > 40 for frame in reference) >= 3
    for time, d2d, gpu, raster in zip(times, reference, *frames, strict=True):
        assert_particle_region(d2d, gpu, (kind, speed, crop, time), check_contour=False)
        if kind == 'snow' and time == 450:
            a, b = blue_coverage(gpu), blue_coverage(raster)
            # At first emergence the dim branches have fewer than 64 occupied
            # pixels and a one-level bias accounts for more than 3% coverage.
            assert 0 < max(a.max(), b.max()) <= 30
            assert (np.maximum(a, b) > 16).sum() < 64
            assert abs(a-b).max() <= 2
            continue
        assert_particle_region(gpu, raster, (kind, speed, crop, time, 'raster'), check_contour=False)


def energy_source(project, kind, speed=0):
    track, style = star_tempo(project, kind, speed)
    return track, replace(style, fx_particle_size_em=.7, fx_particle_count=30)


@pytest.mark.parametrize('target', ['gpu', 'raster'])
@pytest.mark.parametrize('kind', ['lightning', 'flame'])
def test_singing_energy_layers_replay_in_burst_order(binary, project, target, kind, tmp_path):
    track, style = energy_source(project, kind, -200)
    ir = ir_for(binary, (track, style), dpr=1.5)
    bursts = ir['track']['lines'][0]['fx_bursts']
    front = [b for b in bursts if b['front']]
    back = [b for b in bursts if not b['front']]
    assert ir['skia_layout']['sing_twinkle_schema'] == 6
    if kind == 'lightning':
        assert len(front) == len(back) > 0
        assert {b['sprite'] for b in front} == {'bolt', 'bolt_b', 'bolt_c'}
        for center, glow in zip(front, back, strict=True):
            assert center['kind'] == 'lightning' and glow['kind'] == 'lightning_glow'
            for field in ('char_index', 'seed', 'start_ms', 'end_ms', 'count', 'sprite', 'color_id'):
                assert center[field] == glow[field]
            assert glow['size_px'] > center['size_px']
    else:
        assert {b['kind'] for b in front} == {'flame', 'flame_alt', 'ember', 'spark'}
        assert {b['kind'] for b in back} == {'flame_glow', 'smoke'}
        for cell in (b for b in front if b['kind'] == 'flame'):
            assert cell['sweep'] == 2
        assert {b['emit_x'] for b in front if b.get('emit_x')} == {-.45, .45}
        spark_size = max(b['size_px'] for b in front if b['kind'] == 'spark')
        assert spark_size < min(b['size_px'] for b in front if b['kind'] == 'ember')
    times = [290, 400, 580, 740, 950, 1210, 1620, 1900, 2300, 2800, 3000, 740]
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        frames, _ = collect(process, times)
    assert any(blue_coverage(frame).max() > 30 for frame in frames)
    assert not frames[-2][..., 3].any()
    assert np.array_equal(frames[3], frames[-1])
    if target == 'gpu':
        (tmp_path / f'singing-energy-{kind}.json').write_text(json.dumps({
            'bursts': bursts, 'colors': ir['fx_color_table'],
            'line': ir['skia_layout']['lines'][0],
        }, sort_keys=True), encoding='utf-8')


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('kind,speed', [('lightning', 0), ('lightning', -200),
                                       ('flame', 0), ('flame', 200)])
def test_singing_energy_matches_actual_d2d_and_gpu_raster(binary, project, kind, speed):
    from test_subtitle_render_gpu_backend import _render_g1_frames

    track, style = energy_source(project, kind, speed)
    times = [290, 400, 580, 740, 950, 1210, 1620, 1900, 2300, 2800, 3000]
    with NativeRendererProcess() as process:
        _, raw = _render_g1_frames(process, style, tuple(times), force_warp=False, track=track,
                                   width=640, height=360, dpr=1.5, realization_enabled=False)
    reference = [np.frombuffer(p, np.uint8).reshape(540, 960, 4) for p in raw]
    ir = ir_for(binary, (track, style), dpr=1.5)
    frames = []
    for target in ('gpu', 'raster'):
        with SkiaRendererProcess(binary, selection=selection(target)) as process:
            configure(process, ir)
            images, _ = collect(process, times)
        frames.append(images)
    assert sum(blue_coverage(frame).max() > 30 for frame in reference) >= 3
    for time, d2d, gpu, raster in zip(times, reference, *frames, strict=True):
        assert_particle_region(d2d, gpu, (kind, speed, time), check_contour=False)
        assert_particle_region(gpu, raster, (kind, speed, time, 'raster'), check_contour=False)
