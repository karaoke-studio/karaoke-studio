"""Line-prefix bitmap guides (bitmap_guide_schema=1) against the existing GPU."""
from dataclasses import replace
import copy
import sys

import numpy as np
import pytest
from test_subtitle_skia_lower_geometry import prepared_source, source as lower_source
from PyQt6.QtGui import QColor, QImage

from test_subtitle_skia_rtl_flow import application, binary, collect, configure, ir_for, project, selection
from test_subtitle_skia_roles import colors
from krok_helper.subtitle_render.domain.models import SubtitleStyleScheme
from krok_helper.subtitle_render.domain.paint import PaintFill
from krok_helper.subtitle_render.domain.timing import GuideSymbol, LineAnimationOverride, RubyAnnotation
from krok_helper.subtitle_render.engine.guide.skia_bitmap import bitmap_guide_box
from krok_helper.subtitle_render.native.backend import NativeRendererError, NativeRendererProcess
from krok_helper.subtitle_render.native.skia_backend import SkiaRendererProcess

GPU = pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
TIMES = (250, 600, 900, 1250, 1500, 2600, 3700, 4200)


def image(path, width=12, height=12, color='#30FF80'):
    picture = QImage(width, height, QImage.Format.Format_ARGB32)
    picture.fill(QColor(color))
    for x in range(width // 3):
        for y in range(height):
            picture.setPixelColor(x, y, QColor('#FF3050'))
    assert picture.save(str(path))
    return str(path)


def test_prefix_avatar_uses_existing_line_fade(binary, project, tmp_path):
    from test_subtitle_skia_outline_ruby import render_skia
    from test_subtitle_skia_static_glow import assert_glow_parity
    from test_subtitle_skia_vector_guides import guide_source

    track, style = guide_source(project)
    style = replace(style, stroke_width_px=3, entry_anim='none', exit_anim='none', karaoke_anim='none')
    path = image(tmp_path / 'avatar.png')
    symbol = GuideSymbol(kind='bitmap', bitmap_before_path=path, bitmap_after_path=path, duration_ms=300)
    line, = track.lines
    fade = LineAnimationOverride(entry_anim='fade', entry_duration_ms=600, karaoke_anim='none')
    chars = [replace(char, start_ms=char.start_ms + 1000) for char in line.chars]
    track = replace(track, lines=[replace(line, chars=chars, end_ms=line.end_ms + 1000,
                                         guide_symbol=symbol, animation_override=fade,
                                         display_start_override_ms=0, display_end_override_ms=3500)])
    times = (250, 450, 750, 1100)
    ir = ir_for(binary, (track, style))
    assert {'p08_bitmap_guides', 'p07_line_fade'} <= set(ir['required_features'])
    assert ir['track']['lines'][0]['entry_duration_ms'] == 600
    gpu, raster = render_skia(binary, ir, times)
    assert 0 < gpu[0][..., 3].sum() < gpu[-1][..., 3].sum()
    reference = None
    if sys.platform == 'win32':
        from test_subtitle_render_gpu_backend import _render_g1_frames

        with NativeRendererProcess() as process:
            _, pixels = _render_g1_frames(process, style, times, force_warp=False, track=track,
                                          width=640, height=360, realization_enabled=False)
        reference = [np.frombuffer(frame, np.uint8).reshape(360, 640, 4) for frame in pixels]
    assert_glow_parity(times, reference, gpu, raster, 'avatar_line_fade', check_p00_edges=True)


def p00_guide(tmp_path, *, family=None, **changes):
    """The original P00 guide_bitmap: Utopia singing, upper ruby, double outlines and glow."""
    from scripts.subtitle_gpu_baseline.cases import build_cases
    case = next(case for case in build_cases(tmp_path / 'assets') if case.case_id == 'guide_bitmap')
    line = case.track.lines[0]
    if family is not None:
        # Protocol and seek tests use the same generated font on every OS.
        line = replace(line, chars=[replace(char, text=text)
                                    for char, text in zip(line.chars, 'A字A字字한', strict=True)])
        ruby, = case.track.rubies
        style = replace(case.style, font_family=family, font_family_latin=family,
                        ruby_font_family=family, ruby_font_family_latin=family)
        case = replace(case, style=style, track=replace(case.track, rubies=[replace(ruby, kanji='字한', reading='AAAA')]))
    track = replace(case.track, lines=[replace(line, guide_symbol=replace(line.guide_symbol, **changes))])
    return case, track


def test_guide_box_follows_the_gpu_sizing_contract(tmp_path):
    from krok_helper.subtitle_render.domain.timing import GuideSymbol
    wide = image(tmp_path / 'wide.png', 30, 20)
    tall = image(tmp_path / 'tall.png', 8, 24)
    metrics = {'ascent': 66.0, 'descent': 22.0}
    symbol = GuideSymbol(kind='bitmap', bitmap_before_path=tall, bitmap_after_path=wide, bitmap_zoom_percent=150,
                         bitmap_margin_left_px=3, bitmap_margin_right_px=-5, bitmap_margin_bottom_px=4)
    # The after image sizes the cell; integer height 90*150//100, fractional width.
    advance, ink = bitmap_guide_box(symbol, 90, metrics, 5)
    assert ink == (3.0, 22 + 2 - 4 - 135.0, 3 + 202.5, 22 + 2 - 4)
    assert advance == 202.5 + 3 - 5
    fixed, fixed_ink = bitmap_guide_box(replace(symbol, bitmap_fix_size=True, bitmap_after_path=None), 90, metrics, 0)
    assert fixed == 8 + 3 - 5 and fixed_ink == (3.0, 22 - 4 - 24.0, 11.0, 18.0)
    collapsed, _ = bitmap_guide_box(replace(symbol, bitmap_margin_right_px=-400), 90, metrics, 0)
    assert collapsed == 0


@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_p00_bitmap_guide_ir_and_forgeries_keep_scene(binary, project, tmp_path, target):
    case, track = p00_guide(tmp_path, family=project[1].font_family)
    ir = ir_for(binary, (track, case.style))
    assert 'p08_bitmap_guides' in ir['required_features'] and ir['skia_layout']['bitmap_guide_schema'] == 1
    assert 'p05_vector_guides' not in ir['required_features']
    source, = ir['track']['lines']
    final, = ir['skia_layout']['lines']
    assert source['chars'][0]['text'] == '￼' and 'bitmap_guide' in source['chars'][0]
    assert [c['start_ms'] for c in source['chars'][:2]] == [200, 1200]
    cell = final['cells'][0]
    assert cell['metrics']['bitmap_guide'] and cell['metrics']['glyphs'] == []
    assert cell['metrics']['advance'] == 90 and cell['metrics']['ink'][2] - cell['metrics']['ink'][0] == 90
    # Ruby ownership moves with the prefix cell into the render cells.
    group, = final['ruby_groups']
    assert group['units'][0]['interval_ms'][0] >= source['resolved_intervals'][5][0]
    forgeries = []
    for mutation in ('feature', 'schema', 'ink', 'advance', 'wipe', 'identity'):
        bad = copy.deepcopy(ir)
        forged = bad['skia_layout']['lines'][0]['cells'][0]
        if mutation == 'feature': bad['required_features'].remove('p08_bitmap_guides')
        elif mutation == 'schema': bad['skia_layout']['bitmap_guide_schema'] = 2
        elif mutation == 'ink': forged['metrics']['ink'][3] += 1
        elif mutation == 'advance': forged['metrics']['advance'] += .5
        elif mutation == 'wipe': forged['wipe_right'] += 2
        else: forged['metrics']['font_identity'] = 'vector:x'
        forgeries.append(bad)
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        before, _ = collect(process, [600])
        for bad in forgeries:
            with pytest.raises(NativeRendererError):
                configure(process, bad)
        after, _ = collect(process, [600], generation=2)
        unrelated = copy.deepcopy(ir)
        unrelated['required_features'].append('p06_static_shadow')
        configure(process, unrelated)
        replay, _ = collect(process, [600], generation=3)
    assert before[0].any() and np.array_equal(before[0], after[0])
    assert np.array_equal(before[0], replay[0])


def test_missing_bitmap_guide_resource_stays_gated(binary, project, tmp_path):
    case, track = p00_guide(tmp_path, family=project[1].font_family)
    style, line = case.style, track.lines[0]
    line = replace(line, guide_symbol=replace(line.guide_symbol, bitmap_after_path=str(tmp_path / 'missing.png')))
    with pytest.raises(ValueError):
        ir_for(binary, (replace(track, lines=[line]), style))


def decorated_inline_guide(tmp_path, family=None):
    case, track = p00_guide(tmp_path, family=family)
    line, = track.lines
    guide = replace(line.guide_symbol, count=1)
    prefix = replace(guide, replacement_prefix=(line.chars[0].text,))
    line = replace(line, guide_symbol=prefix, inline_guide_symbols={2: guide})
    return replace(case, track=replace(track, lines=[line]), case_id='decorated_inline_bitmap_guide')


def test_decorated_inline_bitmap_guide_keeps_scene_after_missing_capability(binary, project, tmp_path):
    case = decorated_inline_guide(tmp_path, project[1].font_family)
    ir = ir_for(binary, (case.track, case.style))
    feature = 'p08_inline_bitmap_decoration'
    assert feature in ir['required_features']
    bad = copy.deepcopy(ir)
    bad['required_features'].remove(feature)
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        configure(process, ir)
        first, _ = collect(process, [1400, 2600])
        with pytest.raises(NativeRendererError, match='独立装饰'):
            configure(process, bad)
        replay, _ = collect(process, [2600, 1400], generation=2)
    assert np.array_equal(first, replay[::-1])
    assert all(frame.any() for frame in first)


@GPU
def test_decorated_inline_bitmap_guide_matches_actual_d2d_gpu(binary, application, tmp_path):
    from test_subtitle_skia_character_fade import assert_ruby_transition_gpu_parity

    case = decorated_inline_guide(tmp_path)
    assert_ruby_transition_gpu_parity(binary, case, (1400, 1700, 2600, 3830))


def animated_lyric_guide(tmp_path, family=None):
    from test_subtitle_skia_line_motion import animated_alpha_band

    animation = animated_alpha_band(tmp_path)
    case, track = p00_guide(tmp_path, family=family, bitmap_before_path=animation,
                           bitmap_after_path=animation, bitmap_fix_size=False, bitmap_zoom_percent=100)
    line, = track.lines
    line = replace(line, display_start_override_ms=350, display_end_override_ms=6100)
    return replace(case, track=replace(track, lines=[line]), case_id='animated_lyric_guide')


def animated_guide_times(ir):
    line, = ir['track']['lines']
    guide = next(cell['bitmap_guide'] for cell in line['chars'] if cell.get('bitmap_guide'))
    anchor = guide['anim_anchor_ms']
    last_word_end = max(interval[1] for interval in line['resolved_intervals'])
    after = anchor + (max(last_word_end - anchor, 0) + 499) // 250 * 250
    assert line['display_start_ms'] <= anchor and after + 100 < line['display_end_ms']
    return anchor, anchor + 99, anchor + 100, anchor + 250, after, after + 99, after + 100


def test_animated_lyric_guides_follow_authored_anchor_and_seek(binary, project, tmp_path):
    case = animated_lyric_guide(tmp_path, project[1].font_family)
    ir = ir_for(binary, (case.track, case.style))
    feature = 'p08_animated_bitmap_guides'
    assert feature in ir['required_features']
    times = animated_guide_times(ir)
    bad = copy.deepcopy(ir)
    bad['required_features'].remove(feature)
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        configure(process, ir)
        frames, _ = collect(process, times)
        before, before_tail, during, _, after, after_tail, after_next = frames
        with pytest.raises(NativeRendererError, match='动图'):
            configure(process, bad)
        replay, _ = collect(process, times[::-1], generation=2)
    assert np.array_equal(frames, replay[::-1])
    assert before.any()
    assert np.array_equal(after, after_tail)
    assert np.count_nonzero(np.abs(before_tail[..., 3].astype(int) - during[..., 3]) > 64) > 100
    assert np.count_nonzero(np.abs(after_tail[..., 3].astype(int) - after_next[..., 3]) > 64) > 100


@GPU
def test_animated_lyric_guides_match_actual_d2d_gpu(binary, application, tmp_path):
    from test_subtitle_skia_character_fade import assert_ruby_transition_gpu_parity

    case = animated_lyric_guide(tmp_path)
    ir = ir_for(binary, (case.track, case.style))
    assert_ruby_transition_gpu_parity(binary, case, animated_guide_times(ir))


def rtl_bitmap_guide(project, tmp_path):
    from test_subtitle_skia_rtl_flow import rtl_source
    from krok_helper.subtitle_render.domain.timing import GuideSymbol

    track, style = rtl_source(project)
    line, = track.lines
    guide = GuideSymbol(kind='bitmap', bitmap_no_decor=True, count=1, bitmap_fix_size=True,
                        bitmap_before_path=image(tmp_path / 'before.png', 32, 24, '#F06C23'),
                        bitmap_after_path=image(tmp_path / 'after.png', 32, 24, '#51C9F5'))
    return replace(track, lines=[replace(line, guide_symbol=guide, inline_guide_symbols={2: guide})]), style


def test_rtl_bitmap_guide_keeps_scene_after_missing_capability(binary, project, tmp_path):
    ir = ir_for(binary, rtl_bitmap_guide(project, tmp_path))
    bad = copy.deepcopy(ir)
    bad['required_features'].remove('p08_rtl_bitmap_guides')
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        configure(process, ir)
        frames, _ = collect(process, [250, 750, 1100])
        with pytest.raises(NativeRendererError, match='图片导唱符'):
            configure(process, bad)
        replay, _ = collect(process, [1100, 750, 250], generation=2)
    assert np.array_equal(frames, replay[::-1]) and all(frame.any() for frame in frames)


@GPU
def test_rtl_bitmap_guides_match_actual_d2d_gpu(binary, project, tmp_path):
    from test_subtitle_render_gpu_backend import _render_g1_frames
    from test_subtitle_skia_outline_ruby import render_skia
    from test_subtitle_skia_static_glow import assert_glow_parity

    track, style = rtl_bitmap_guide(project, tmp_path)
    times = (250, 550, 750, 1100, 1550)
    with NativeRendererProcess() as process:
        _, raw = _render_g1_frames(process, style, times, force_warp=False, track=track,
                                   width=640, height=360, realization_enabled=False)
    reference = [np.frombuffer(frame, np.uint8).reshape(360, 640, 4) for frame in raw]
    gpu, raster = render_skia(binary, ir_for(binary, (track, style)), times)
    assert_glow_parity(times, reference, gpu, raster, 'rtl_bitmap_guide', check_p00_edges=True)


def test_bitmap_guide_uses_the_character_opacity_with_its_fixed_rectangle(binary, project, tmp_path):
    from test_subtitle_skia_character_transform import transform_source
    from test_subtitle_skia_outline_ruby import render_skia
    from test_subtitle_skia_static_glow import assert_glow_parity

    track, style = transform_source(project, 'spin_flip', 'spin_flip')
    guide = GuideSymbol(kind='bitmap', bitmap_before_path=image(tmp_path / 'fixed.png', color='#30FF80'),
                        bitmap_no_decor=True, bitmap_fix_size=True, bitmap_margin_bottom_px=18)
    line, = track.lines
    track = replace(track, lines=[replace(line, inline_guide_symbols={1: guide})])
    times, dpr = (85, 180, 260, 480, 700, 2580, 2680, 2750), 3
    reference = None
    if sys.platform == 'win32':
        from test_subtitle_render_gpu_backend import _render_g1_frames

        with NativeRendererProcess() as process:
            _, raw = _render_g1_frames(process, style, times, force_warp=False, track=track,
                                       width=640, height=360, dpr=dpr, realization_enabled=False)
        reference = [np.frombuffer(frame, np.uint8).reshape(1080, 1920, 4) for frame in raw]
    ir = ir_for(binary, (track, style), dpr=dpr)
    gpu, raster = render_skia(binary, ir, times)
    assert_glow_parity(times, reference, gpu, raster, 'bitmap_guide_character_opacity', check_p00_edges=True)
    for frames in (gpu, raster):
        parts = []
        for frame in frames:
            r, g, b, a = np.moveaxis(frame, -1, 0)
            pixels = (a > 10) & (g > 245) & (r < 70) & (b > 90) & (b < 160)
            parts.append((int(a[pixels].max()) if pixels.any() else 0, int(pixels.sum())))
        assert parts[0] == (0, 0)
        assert 0 < parts[1][0] < parts[2][0] < parts[4][0] == 255
        assert parts[1][1] == parts[2][1] == parts[4][1]


@GPU
def test_rtl_bitmap_guide_with_upper_reading_matches_actual_d2d_gpu(binary, project, tmp_path):
    from krok_helper.subtitle_render.domain.timing import RubyAnnotation
    from test_subtitle_render_gpu_backend import _render_g1_frames
    from test_subtitle_skia_outline_ruby import render_skia
    from test_subtitle_skia_static_glow import assert_glow_parity

    track, style = rtl_bitmap_guide(project, tmp_path)
    line = replace(track.lines[0], inline_guide_symbols={})
    reading = RubyAnnotation(line.chars[1].text, '字', target_line_index=0,
                             target_char_start=1, target_char_end=2,
                             pos_start_ms=line.chars[1].start_ms, pos_end_ms=line.chars[2].start_ms)
    track = replace(track, lines=[line], rubies=[reading])
    style = replace(style, ruby_font_family=style.font_family, ruby_font_size_px=24, ruby_gap_px=8,
                    ruby_colors_follow_main=False, ruby_karaoke_colors=colors('#FF5544FF', '#FFFF8C21'))
    times, dpr = (250, 450, 750, 1100, 1550, 1950), 3
    refs = []
    for source in (track, replace(track, rubies=[]), replace(track, lines=[replace(line, guide_symbol=None)])):
        with NativeRendererProcess() as process:
            _, raw = _render_g1_frames(process, style, times, force_warp=False, track=source,
                                       width=640, height=360, dpr=dpr, realization_enabled=False)
        refs.append([np.frombuffer(frame, np.uint8).reshape(360*dpr, 640*dpr, 4) for frame in raw])
    for control in refs[1:]:
        assert any(np.count_nonzero((a[..., 3] > 16) & (b[..., 3] == 0)) > 70
                   for a, b in zip(refs[0], control, strict=True))
    ir = ir_for(binary, (track, style), dpr=dpr)
    assert 'p08_rtl_bitmap_upper_ruby' in ir['required_features']
    gpu, raster = render_skia(binary, ir, times)
    assert_glow_parity(times, refs[0], gpu, raster, 'rtl_bitmap_upper_ruby', check_p00_edges=True)


@GPU
def test_vector_and_bitmap_guides_share_a_line_like_actual_d2d_gpu(binary, project, tmp_path):
    from test_subtitle_render_gpu_backend import _render_g1_frames
    from test_subtitle_skia_outline_ruby import render_skia
    from test_subtitle_skia_static_glow import assert_glow_parity
    from test_subtitle_skia_vector_guides import SQUARE

    bitmaps, style = rtl_bitmap_guide(project, tmp_path)
    line = replace(bitmaps.lines[0], guide_symbol=SQUARE)
    track = replace(bitmaps, lines=[line])
    style = replace(style, right_to_left=False)
    times, dpr = (250, 450, 750, 1100, 1550), 3
    refs = []
    for source in (track, replace(track, lines=[replace(line, guide_symbol=None)]),
                   replace(track, lines=[replace(line, inline_guide_symbols={})])):
        with NativeRendererProcess() as process:
            _, raw = _render_g1_frames(process, style, times, force_warp=False, track=source,
                                       width=640, height=360, dpr=dpr, realization_enabled=False)
        refs.append([np.frombuffer(p, np.uint8).reshape(360*dpr, 640*dpr, 4) for p in raw])
    for control in refs[1:]:
        assert any(np.count_nonzero(a != b) > 100 for a, b in zip(refs[0], control, strict=True))
    ir = ir_for(binary, (track, style), dpr=dpr)
    assert 'p08_mixed_guide_kinds' in ir['required_features']
    gpu, raster = render_skia(binary, ir, times)
    assert_glow_parity(times, refs[0], gpu, raster, 'mixed_guide_kinds', check_p00_edges=True)


@GPU
def test_reverse_singing_keeps_bitmap_guides_unposed_like_actual_d2d_gpu(binary, project, tmp_path):
    from test_subtitle_render_gpu_backend import _render_g1_frames
    from test_subtitle_skia_outline_ruby import render_skia
    from test_subtitle_skia_static_glow import assert_glow_parity

    track, style = rtl_bitmap_guide(project, tmp_path)
    track = replace(track, lines=[replace(track.lines[0], wipe_reverse=True)])
    style = replace(style, right_to_left=False, reverse_karaoke_anim='utopia')
    times, dpr = (250, 450, 650, 750, 1100, 1550), 3
    refs = []
    for look in (style, replace(style, reverse_karaoke_anim='none')):
        with NativeRendererProcess() as process:
            _, raw = _render_g1_frames(process, look, times, force_warp=False, track=track,
                                       width=640, height=360, dpr=dpr, realization_enabled=False)
        refs.append([np.frombuffer(p, np.uint8).reshape(360*dpr, 640*dpr, 4) for p in raw])
    assert any(np.count_nonzero(a != b) > 100 for a, b in zip(*refs, strict=True))
    ir = ir_for(binary, (track, style), dpr=dpr)
    assert 'p08_reverse_bitmap_singing' in ir['required_features']
    gpu, raster = render_skia(binary, ir, times)
    assert_glow_parity(times, refs[0], gpu, raster, 'reverse_singing_bitmap_guides', check_p00_edges=True)


@GPU
@pytest.mark.parametrize('glow', [False, True], ids=['outline', 'outline_and_glow'])
def test_reverse_singing_bitmap_guides_keep_outlines_like_actual_d2d_gpu(binary, application, tmp_path, glow):
    from test_subtitle_render_gpu_backend import _render_g1_frames
    from test_subtitle_skia_outline_ruby import render_skia
    from test_subtitle_skia_static_glow import assert_glow_parity

    case, track = p00_guide(tmp_path, bitmap_no_decor=not glow)
    track = replace(track, lines=[replace(line, wipe_reverse=True) for line in track.lines])
    style = replace(case.style, reverse_karaoke_anim='utopia')
    if not glow:
        style = replace(style, decoration_kind='none', decoration_kind_before='none')
    times, dpr = (600, 1320, 1750, 2850, 3800, 4520, 4980), 3
    refs = []
    for source, look in (
        (track, style),
        (replace(track, rubies=[]), style),
        (track, replace(style, reverse_karaoke_anim='none')),
    ):
        with NativeRendererProcess() as process:
            _, raw = _render_g1_frames(process, look, times, force_warp=False, track=source,
                                       width=640, height=360, dpr=dpr, realization_enabled=False)
        refs.append([np.frombuffer(frame, np.uint8).reshape(360*dpr, 640*dpr, 4) for frame in raw])
    assert np.array_equal(refs[0], refs[1])
    assert any(np.count_nonzero(a != b) > 100 for a, b in zip(refs[0], refs[2], strict=True))
    if glow:
        line, = track.lines
        plain = replace(track, lines=[replace(line, guide_symbol=replace(line.guide_symbol, bitmap_no_decor=True))])
        with NativeRendererProcess() as process:
            _, raw = _render_g1_frames(process, style, times, force_warp=False, track=plain,
                                       width=640, height=360, dpr=dpr, realization_enabled=False)
        assert any(np.count_nonzero(a != np.frombuffer(b, np.uint8).reshape(a.shape)) > 100
                   for a, b in zip(refs[0], raw, strict=True))
    ir = ir_for(binary, (track, style), dpr=dpr)
    assert 'p08_reverse_bitmap_singing_outline' in ir['required_features']
    assert ('p08_reverse_bitmap_singing_glow' in ir['required_features']) == glow
    assert not ir['skia_layout']['lines'][0]['ruby_groups']
    gpu, raster = render_skia(binary, ir, times)
    assert_glow_parity(times, refs[0], gpu, raster, 'reverse_bitmap_singing_outline', check_p00_edges=True)


@GPU
def test_rtl_singing_bitmap_guide_with_glow_and_ruby_matches_d2d_gpu(binary, application, tmp_path):
    from test_subtitle_render_gpu_backend import _render_g1_frames
    from test_subtitle_skia_outline_ruby import render_skia
    from test_subtitle_skia_static_glow import assert_glow_parity

    case, track = p00_guide(tmp_path, bitmap_no_decor=False)
    style = replace(case.style, right_to_left=True)
    line, = track.lines
    undecorated = replace(line, guide_symbol=replace(line.guide_symbol, bitmap_no_decor=True))
    times, dpr = (600, 1320, 1750, 2850, 3800, 4520, 4980), 3
    refs = []
    for source, look in (
        (track, style),
        (replace(track, lines=[undecorated]), style),
        (replace(track, rubies=[]), style),
        (track, replace(style, karaoke_anim='none')),
    ):
        with NativeRendererProcess() as process:
            _, raw = _render_g1_frames(process, look, times, force_warp=False, track=source,
                                       width=640, height=360, dpr=dpr, realization_enabled=False)
        refs.append([np.frombuffer(frame, np.uint8).reshape(360*dpr, 640*dpr, 4) for frame in raw])
    for name, control in zip(('bitmap glow', 'upper ruby', 'utopia'), refs[1:], strict=True):
        assert any(np.count_nonzero(a != b) > 100 for a, b in zip(refs[0], control, strict=True)), name
    ir = ir_for(binary, (track, style), dpr=dpr)
    assert 'p08_rtl_bitmap_singing' in ir['required_features']
    gpu, raster = render_skia(binary, ir, times)
    assert_glow_parity(times, refs[0], gpu, raster, 'rtl_singing_bitmap_guide', check_p00_edges=True)


def shadowed_bitmap_guide(project, tmp_path, *, paint_mode='solid'):
    track, style = rtl_bitmap_guide(project, tmp_path)
    line, = track.lines
    track = replace(track, lines=[replace(line, guide_symbol=replace(line.guide_symbol, bitmap_no_decor=False),
                                          inline_guide_symbols={})])
    palette = colors('#FFB2FF', '#90CAFB')
    before = PaintFill(color='#DDF72839')
    after = PaintFill(color='#DDBF28F7')
    if paint_mode == 'gradient':
        before = PaintFill(mode='gradient_vertical', gradient_stops=[(0, '#DDF72839'), (100, '#DDFF7D28')])
        after = PaintFill(mode='gradient_horizontal', gradient_stops=[(0, '#DDBF28F7'), (100, '#DD2741F0')])
    elif paint_mode == 'image':
        from test_subtitle_skia_image_fills import image_paint, pattern, write_png

        tile = write_png(tmp_path / 'bitmap-shadow.png', pattern('transparent'))
        before, after = image_paint(tile, 137), image_paint(tile, 103)
    palette = replace(palette, before=replace(palette.before, shadow=before), after=replace(palette.after, shadow=after))
    return track, replace(style, right_to_left=False, decoration_before_sync=True, decoration_kind='shadow',
                          karaoke_colors=palette, shadow_offset_x=9, shadow_offset_y=14)


@pytest.mark.parametrize('paint_mode', ['solid', 'gradient', 'image'])
def test_bitmap_guide_shadow_preserves_scene_without_capability(binary, project, tmp_path, paint_mode):
    ir = ir_for(binary, shadowed_bitmap_guide(project, tmp_path, paint_mode=paint_mode))
    bad = copy.deepcopy(ir)
    feature = 'p08_bitmap_guide_shadow' if paint_mode == 'solid' else f'p08_bitmap_guide_{paint_mode}_shadow'
    bad['required_features'].remove(feature)
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        configure(process, ir)
        output, _ = collect(process, [450, 900])
        with pytest.raises(NativeRendererError, match='阴影'):
            configure(process, bad)
        replay, _ = collect(process, [900, 450], generation=2)
    assert np.array_equal(output, replay[::-1])


@GPU
@pytest.mark.parametrize('decoration', ['shadow', 'glow'])
def test_rtl_bitmap_guide_decoration_matches_actual_d2d_gpu(binary, project, tmp_path, decoration):
    from test_subtitle_render_gpu_backend import _render_g1_frames
    from test_subtitle_skia_outline_ruby import render_skia
    from test_subtitle_skia_static_glow import assert_glow_parity

    track, style = shadowed_bitmap_guide(project, tmp_path)
    style = replace(style, right_to_left=True, decoration_kind=decoration, glow_before_radius_px=5,
                    glow_after_radius_px=6)
    line, = track.lines
    plain = replace(track, lines=[replace(line, guide_symbol=replace(line.guide_symbol, bitmap_no_decor=True))])
    times, dpr = (250, 450, 750, 1100, 1550, 1950), 3
    refs = []
    for source in (track, plain):
        with NativeRendererProcess() as process:
            _, raw = _render_g1_frames(process, style, times, force_warp=False, track=source,
                                       width=640, height=360, dpr=dpr, realization_enabled=False)
        refs.append([np.frombuffer(frame, np.uint8).reshape(360*dpr, 640*dpr, 4) for frame in raw])
    assert any(np.count_nonzero((a[..., 3] > 16) & (b[..., 3] == 0)) > 40 for a, b in zip(*refs, strict=True))
    ir = ir_for(binary, (track, style), dpr=dpr)
    assert 'p08_rtl_bitmap_guide_decoration' in ir['required_features']
    gpu, raster = render_skia(binary, ir, times)
    assert_glow_parity(times, refs[0], gpu, raster, ('rtl_bitmap_guide', decoration), check_p00_edges=True)
    gpu, raster = render_skia(binary, ir_for(binary, (plain, style), dpr=dpr), times)
    assert_glow_parity(times, refs[1], gpu, raster, ('rtl_bitmap_guide_no_decor', decoration), check_p00_edges=True)


def gradient_glowing_bitmap_guide(project, tmp_path):
    track, style = shadowed_bitmap_guide(project, tmp_path, paint_mode='gradient')
    style = replace(style, decoration_kind='glow', glow_before_radius_px=6, glow_after_radius_px=7)
    line, = track.lines
    plain = replace(track, lines=[replace(line, guide_symbol=replace(line.guide_symbol, bitmap_no_decor=True))])
    return track, style, plain


def test_bitmap_guide_gradient_glow_requires_capability(binary, project, tmp_path):
    track, style, plain = gradient_glowing_bitmap_guide(project, tmp_path)
    times = [450, 900]
    ir = ir_for(binary, (track, style))
    bad = copy.deepcopy(ir)
    bad['required_features'].remove('p08_bitmap_guide_gradient_glow')
    with SkiaRendererProcess(binary, selection=selection('gpu')) as process:
        configure(process, ir)
        frames, _ = collect(process, times)
        with pytest.raises(NativeRendererError):
            configure(process, bad)
        replay, _ = collect(process, times[::-1], generation=2)
        configure(process, ir_for(binary, (plain, style)))
        unadorned, _ = collect(process, times, generation=3)
    assert np.array_equal(frames, replay[::-1])
    assert any(np.count_nonzero((a[..., 3] > 16) & (b[..., 3] == 0)) > 10
               for a, b in zip(frames, unadorned, strict=True))


@GPU
def test_bitmap_guide_gradient_glow_matches_actual_d2d_gpu(binary, project, tmp_path):
    from test_subtitle_render_gpu_backend import _render_g1_frames
    from test_subtitle_skia_outline_ruby import render_skia
    from test_subtitle_skia_static_glow import assert_glow_parity

    track, style, plain = gradient_glowing_bitmap_guide(project, tmp_path)
    times, dpr = (250, 450, 750, 1100, 1550, 1950), 3
    refs = []
    for source in (track, plain):
        with NativeRendererProcess() as process:
            _, raw = _render_g1_frames(process, style, times, force_warp=False, track=source,
                                       width=640, height=360, dpr=dpr, realization_enabled=False)
        refs.append([np.frombuffer(frame, np.uint8).reshape(360*dpr, 640*dpr, 4) for frame in raw])
    assert any(np.count_nonzero((a[..., 3] > 16) & (b[..., 3] == 0)) > 40 for a, b in zip(*refs, strict=True))
    ir = ir_for(binary, (track, style), dpr=dpr)
    gpu, raster = render_skia(binary, ir, times)
    assert_glow_parity(times, refs[0], gpu, raster, 'bitmap_guide_gradient_glow', check_p00_edges=True)


@GPU
@pytest.mark.parametrize('paint_mode', ['solid', 'gradient', 'image'])
def test_bitmap_guide_shadow_matches_actual_d2d_gpu(binary, project, tmp_path, paint_mode):
    from scripts.subtitle_gpu_baseline.policy import COMPARISON_BUDGETS
    from test_subtitle_render_gpu_backend import _render_g1_frames
    from test_subtitle_skia_outline_ruby import render_skia
    from test_subtitle_skia_static_glow import assert_glow_parity
    from test_subtitle_skia_static_shadow import premultiplied

    track, style = shadowed_bitmap_guide(project, tmp_path, paint_mode=paint_mode)
    line, = track.lines
    plain = replace(track, lines=[replace(line, guide_symbol=replace(line.guide_symbol, bitmap_no_decor=True))])
    times = (250, 450, 750, 1100, 1550)
    refs = []
    for source in (track, plain):
        with NativeRendererProcess() as process:
            _, raw = _render_g1_frames(process, style, times, force_warp=False, track=source,
                                       width=640, height=360, realization_enabled=False)
        refs.append([np.frombuffer(frame, np.uint8).reshape(360, 640, 4) for frame in raw])
    reference, no_guide_shadow = refs
    assert any(np.count_nonzero((a[..., 3] > 16) & (b[..., 3] == 0)) > 40
               for a, b in zip(reference, no_guide_shadow, strict=True))
    gpu, raster = render_skia(binary, ir_for(binary, (track, style)), times)
    assert_glow_parity(times, reference, gpu, raster, 'bitmap_guide_shadow', check_p00_edges=True)
    if paint_mode != 'solid':
        budget = COMPARISON_BUDGETS['image_categories']['decorated_text']
        sampled = 0
        for reference_frame, bare, gpu_frame, raster_frame in zip(reference, no_guide_shadow, gpu, raster, strict=True):
            shadow_only = (reference_frame[..., 3] > 160) & (bare[..., 3] == 0)
            if np.count_nonzero(shadow_only) < 40:
                continue
            expected = premultiplied(reference_frame[shadow_only])
            for rendered in (gpu_frame, raster_frame):
                assert abs(expected - premultiplied(rendered[shadow_only])).mean() <= budget['max_union_channel_mae']
            sampled += 1
        assert sampled >= 2


def mixed_reverse_bitmap_guide(project, tmp_path):
    track, style = shadowed_bitmap_guide(project, tmp_path)
    line, = track.lines
    style = replace(style, decoration_kind='glow', glow_before_radius_px=5, glow_after_radius_px=5,
                    stroke_width_px=3, reverse_karaoke_anim='none')
    ruby = RubyAnnotation(line.chars[-1].text, 'A', target_line_index=0,
                          target_char_start=len(line.chars) - 1, target_char_end=len(line.chars),
                          pos_start_ms=line.chars[-1].start_ms, pos_end_ms=line.end_ms)
    return replace(track, lines=[replace(line, wipe_reverse=True)], rubies=[ruby]), style


def bitmap_lower_reading(track, style, path):
    from krok_helper.subtitle_render.domain.timing import GuideSymbol

    line, = track.lines
    picture = GuideSymbol(kind='bitmap', bitmap_after_path=path, bitmap_before_path=path,
                          bitmap_no_decor=True, duration_ms=300, bitmap_margin_right_px=9)
    return (replace(track, lines=[replace(line, guide_symbol=picture)]),
            replace(style, right_to_left=False, lower_ruby_enabled=True, entry_anim='none',
                    exit_anim='none', karaoke_anim='none'))


def vertical_bitmap_guide(project, tmp_path, reverse=False):
    from krok_helper.subtitle_render.domain.timing import GuideSymbol
    from test_subtitle_skia_vertical import vertical_source

    track, style = vertical_source(project, position='center', reverse=reverse, texts=['字', '한', '字', '한'])
    line, = track.lines
    pictures = GuideSymbol(kind='bitmap', bitmap_before_path=image(tmp_path / 'before.png', 15, 12, '#3870FF'),
                           bitmap_after_path=image(tmp_path / 'after.png', 15, 12, '#30FF80'),
                           bitmap_no_decor=True, bitmap_zoom_percent=80, replacement_prefix=('字',))
    return replace(track, lines=[replace(line, guide_symbol=pictures)]), style


def test_vertical_bitmap_guide_keeps_scene_after_invalid_wipe_box(binary, project, tmp_path):
    ir = ir_for(binary, vertical_bitmap_guide(project, tmp_path))
    bad = copy.deepcopy(ir)
    bad['skia_layout']['lines'][0]['cells'][0]['wipe_bottom'] += 1
    times = [250, 450, 750]
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        configure(process, ir)
        frames, _ = collect(process, times)
        with pytest.raises(NativeRendererError, match='走字范围'):
            configure(process, bad)
        replay, _ = collect(process, times[::-1], generation=2)
    assert all(frame.any() for frame in frames) and np.array_equal(frames, replay[::-1])


@GPU
@pytest.mark.parametrize('reverse', [False, True], ids=['top_to_bottom', 'bottom_to_top'])
def test_vertical_bitmap_guide_matches_actual_d2d_gpu(binary, project, tmp_path, reverse):
    from test_subtitle_render_gpu_backend import _render_g1_frames
    from test_subtitle_skia_outline_ruby import render_skia
    from test_subtitle_skia_static_glow import assert_glow_parity

    track, style = vertical_bitmap_guide(project, tmp_path, reverse)
    times = (250, 450, 750, 1100, 1550, 1750)
    with NativeRendererProcess() as process:
        _, raw = _render_g1_frames(process, style, times, force_warp=False, track=track,
                                   width=640, height=360, realization_enabled=False)
    reference = [np.frombuffer(frame, np.uint8).reshape(360, 640, 4) for frame in raw]
    # Each source has a constant distinct color over most of the image.
    assert any(np.count_nonzero(frame[..., 2] > 240) > 100 for frame in reference)
    assert any(np.count_nonzero(frame[..., 1] > 240) > 100 for frame in reference)
    gpu, raster = render_skia(binary, ir_for(binary, (track, style)), times)
    assert_glow_parity(times, reference, gpu, raster, 'vertical_bitmap_guide', check_p00_edges=True)


def vertical_guide_with_inactive_singing_effects(project, tmp_path):
    track, style = vertical_bitmap_guide(project, tmp_path)
    effects = replace(style, karaoke_anim='zoom_pulse_scanline', scanline_shape='flame', scanline_enabled=True,
                      scanline_effect='star', sing_fx='note')
    return track, style, effects


def test_vertical_guide_keeps_its_clock_with_inactive_singing_effects(binary, project, tmp_path):
    track, style, effects = vertical_guide_with_inactive_singing_effects(project, tmp_path)
    base = ir_for(binary, (track, style))
    ir = ir_for(binary, (track, effects))
    line = ir['track']['lines'][0]
    assert line['karaoke_anim'] == 'none' and not line['scanline'] and not line['zoom_pulse'] and not line['fx_bursts']
    assert line['resolved_intervals'] == base['track']['lines'][0]['resolved_intervals']
    assert ir['skia_layout']['lines'] == base['skia_layout']['lines']
    assert effects.karaoke_anim == 'zoom_pulse_scanline' and effects.sing_fx == 'note'


@GPU
def test_vertical_guide_ignores_singing_effects_like_actual_d2d_gpu(binary, project, tmp_path):
    from test_subtitle_render_gpu_backend import _render_g1_frames
    from test_subtitle_skia_outline_ruby import render_skia
    from test_subtitle_skia_static_glow import assert_glow_parity

    track, base, effects = vertical_guide_with_inactive_singing_effects(project, tmp_path)
    times = (250, 450, 750, 1100, 1550, 1750)
    refs = []
    for look in (base, effects):
        with NativeRendererProcess() as process:
            _, raw = _render_g1_frames(process, look, times, force_warp=False, track=track,
                                       width=640, height=360, realization_enabled=False)
        refs.append([np.frombuffer(frame, np.uint8).reshape(360, 640, 4) for frame in raw])
    assert np.array_equal(*refs)
    gpu, raster = render_skia(binary, ir_for(binary, (track, effects)), times)
    assert_glow_parity(times, refs[0], gpu, raster, 'vertical_guide_inactive_singing', check_p00_edges=True)


@GPU
@pytest.mark.parametrize('decoration,reverse', [('glow', False), ('shadow', True)])
def test_vertical_guide_decoration_matches_actual_d2d_gpu(binary, project, tmp_path, decoration, reverse):
    from test_subtitle_render_gpu_backend import _render_g1_frames
    from test_subtitle_skia_outline_ruby import render_skia
    from test_subtitle_skia_static_glow import assert_glow_parity

    bare, style = vertical_bitmap_guide(project, tmp_path, reverse=reverse)
    line, = bare.lines
    track = replace(bare, lines=[replace(line, guide_symbol=replace(line.guide_symbol, bitmap_no_decor=False))])
    palette = style.karaoke_colors
    palette = replace(palette, before=replace(palette.before, shadow=PaintFill(color='#D0FC4022')),
                      after=replace(palette.after, shadow=PaintFill(color='#D0E030FF')))
    style = replace(style, decoration_before_sync=True, decoration_kind=decoration, line_y_margin_px=60,
                    glow_before_radius_px=5, glow_after_radius_px=5, shadow_offset_x=9, shadow_offset_y=8,
                    karaoke_colors=palette)
    times, dpr = (250, 450, 750, 1100, 1550, 1750), 3
    refs = []
    for source in (track, bare):
        with NativeRendererProcess() as process:
            _, raw = _render_g1_frames(process, style, times, force_warp=False, track=source,
                                       width=640, height=360, dpr=dpr, realization_enabled=False)
        refs.append([np.frombuffer(frame, np.uint8).reshape(360*dpr, 640*dpr, 4) for frame in raw])
    assert any(np.count_nonzero((a[..., 3] > 16) & (b[..., 3] == 0)) > 30 for a, b in zip(*refs, strict=True))
    ir = ir_for(binary, (track, style), dpr=dpr)
    assert 'p08_vertical_bitmap_decoration' in ir['required_features']
    gpu, raster = render_skia(binary, ir, times)
    assert_glow_parity(times, refs[0], gpu, raster, ('vertical_guide', decoration), check_p00_edges=True)


@pytest.mark.parametrize('entry,rtl', [('none', False), ('fade', False), ('none', True)])
def test_bitmap_lower_reading_requires_its_capability(binary, lower_source, tmp_path, entry, rtl):
    from test_subtitle_skia_lower_product import ir_for as lower_ir
    from test_subtitle_skia_outline_ruby import render_skia
    from test_subtitle_skia_static_glow import assert_glow_parity

    track, style, resolve = lower_source
    track, style = bitmap_lower_reading(track, style, image(tmp_path / 'guide.png'))
    if rtl:
        style = replace(style, right_to_left=True)
    if entry == 'fade':
        line, = track.lines
        chars = [replace(char, start_ms=char.start_ms + 1000) for char in line.chars]
        track = replace(track, lines=[replace(line, chars=chars, end_ms=line.end_ms + 1000,
            display_start_override_ms=0, display_end_override_ms=3500)],
            rubies=[replace(ruby, pos_start_ms=ruby.pos_start_ms + 1000, pos_end_ms=ruby.pos_end_ms + 1000)
                    for ruby in track.rubies])
        style = replace(style, entry_anim='fade', entry_lead_ms=600)
    ir = lower_ir(binary, (track, style, resolve))
    final, = ir['skia_layout']['lines']
    assert final['cells'][0]['metrics']['bitmap_guide']
    assert any(group.get('lower') and group['style_cell'] > 0 for group in final['ruby_groups'])
    bad = copy.deepcopy(ir)
    bad['required_features'].remove('p08_bitmap_lower_ruby')
    times = [250, 650] if entry == 'fade' else [400, 950]
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        configure(process, ir)
        frames, _ = collect(process, times)
        with pytest.raises(NativeRendererError, match='下方注音'):
            configure(process, bad)
        replay, _ = collect(process, times[::-1], generation=2)
    assert all(frame.any() for frame in frames) and np.array_equal(frames, replay[::-1])
    if entry == 'fade':
        assert ir['track']['lines'][0]['entry_duration_ms'] == 600
        gpu, raster = render_skia(binary, ir, times)
        assert 0 < gpu[0][..., 3].max() < gpu[1][..., 3].max()
        assert_glow_parity(times, None, gpu, raster, 'bitmap_lower_reading_fade', check_p00_edges=True)


@GPU
@pytest.mark.parametrize('entry,rtl', [('none', False), ('fade', False), ('none', True)])
def test_bitmap_lower_reading_matches_actual_d2d_gpu(binary, application, tmp_path, entry, rtl):
    from scripts.subtitle_gpu_baseline.cases import build_cases
    from test_subtitle_skia_character_fade import assert_ruby_transition_gpu_parity
    from test_subtitle_render_gpu_backend import _render_g1_frames

    base = next(c for c in build_cases(tmp_path / 'assets') if c.case_id == 'lower_ruby')
    track, style = bitmap_lower_reading(base.track, base.style, image(tmp_path / 'guide.png'))
    if rtl:
        style = replace(style, right_to_left=True, decoration_kind='none', decoration_kind_before='none',
                        ruby_decoration_kind='none', ruby_decoration_kind_before='none')
    times = (1000, 1550, 2600, 4110, 4280)
    if entry == 'fade':
        style = replace(style, entry_anim='fade', decoration_kind='none', ruby_decoration_kind='none')
        times = (450, 600, 850, 1000, 1550)
    case = replace(base, track=track, style=style)
    references = []
    for look in (case.style, replace(case.style, lower_ruby_enabled=False)):
        with NativeRendererProcess() as process:
            _, raw = _render_g1_frames(process, look, times, force_warp=False, track=case.track,
                                       realization_enabled=False)
        references.append(raw)
    assert any(a != b for a, b in zip(*references, strict=True))
    assert_ruby_transition_gpu_parity(binary, case, times)


def test_mixed_reverse_bitmap_guide_keeps_scene_after_missing_capability(binary, project, tmp_path):
    ir = ir_for(binary, mixed_reverse_bitmap_guide(project, tmp_path))
    assert ir['track']['rubies'] and ir['skia_layout']['lines'][0]['ruby_groups'] == []
    bad = copy.deepcopy(ir)
    bad['required_features'].remove('p08_mixed_reverse_bitmap_guides')
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        configure(process, ir)
        frames, _ = collect(process, [450, 900, 1500])
        with pytest.raises(NativeRendererError, match='图片导唱符'):
            configure(process, bad)
        replay, _ = collect(process, [1500, 900, 450], generation=2)
    assert np.array_equal(frames, replay[::-1]) and all(frame.any() for frame in frames)


@pytest.mark.parametrize('guide_kind', ['bitmap', 'vector'])
def test_reverse_guide_coexists_with_a_separate_lower_reading(binary, lower_source, tmp_path, guide_kind):
    from test_subtitle_skia_lower_product import ir_for as lower_ir
    from test_subtitle_skia_outline_ruby import render_skia
    from test_subtitle_skia_static_glow import assert_glow_parity
    from test_subtitle_skia_vector_guides import SQUARE

    track, style, resolve = lower_source
    original, = track.lines
    guided, style = bitmap_lower_reading(track, style, image(tmp_path / 'guide.png', color='#30FF80'))
    avatar, = guided.lines
    if guide_kind == 'vector':
        avatar = replace(avatar, guide_symbol=SQUARE)
    shift = 4000
    avatar = replace(avatar, track_line_index=1, wipe_reverse=True,
                     chars=[replace(char, start_ms=char.start_ms + shift) for char in avatar.chars],
                     end_ms=avatar.end_ms + shift, display_start_override_ms=shift,
                     display_end_override_ms=avatar.end_ms + shift + 600)
    track = replace(track, lines=[original, avatar])
    times = (500, 950, 4500, 4980)
    ir = lower_ir(binary, (track, style, resolve))
    reading_row, avatar_row = ir['skia_layout']['lines']
    assert any(group.get('lower') for group in reading_row['ruby_groups'])
    assert avatar_row['ruby_groups'] == []
    key = 'bitmap_guide' if guide_kind == 'bitmap' else 'vector_glyph_id'
    assert avatar_row['cells'][0]['metrics'][key]
    reference = None
    if sys.platform == 'win32':
        from test_subtitle_render_gpu_backend import _render_g1_frames

        with NativeRendererProcess() as process:
            _, raw = _render_g1_frames(process, style, times, force_warp=False, track=track,
                                       width=900, height=600, realization_enabled=False)
        reference = [np.frombuffer(frame, np.uint8).reshape(600, 900, 4) for frame in raw]
    gpu, raster = render_skia(binary, ir, times)
    assert_glow_parity(times, reference, gpu, raster, ('separate_guide_and_lower_reading', guide_kind),
                       check_p00_edges=True)
    if guide_kind == 'vector':
        assert all(frame[..., 3].any() for frame in gpu[2:])
        return
    for frames in (gpu, raster):
        icon = [np.count_nonzero(np.all(frame[..., :3] == [48, 255, 128], axis=-1) & (frame[..., 3] > 240))
                for frame in frames]
        assert icon[:2] == [0, 0] and min(icon[2:]) > 100


@GPU
def test_mixed_reverse_bitmap_guide_matches_actual_d2d_gpu(binary, project, tmp_path):
    from test_subtitle_render_gpu_backend import _render_g1_frames
    from test_subtitle_skia_outline_ruby import render_skia
    from test_subtitle_skia_static_glow import assert_glow_parity

    track, style = mixed_reverse_bitmap_guide(project, tmp_path)
    times = (250, 450, 750, 1100, 1550)
    line, = track.lines
    forward = replace(track, lines=[replace(line, wipe_reverse=False)])
    refs = []
    for source in (track, replace(track, rubies=[]), forward):
        with NativeRendererProcess() as process:
            _, raw = _render_g1_frames(process, style, times, force_warp=False, track=source,
                                       width=640, height=360, realization_enabled=False)
        refs.append([np.frombuffer(frame, np.uint8).reshape(360, 640, 4) for frame in raw])
    reference, no_reading, forward_frames = refs
    assert np.array_equal(reference, no_reading)
    assert any(np.count_nonzero(a != b) > 100 for a, b in zip(reference, forward_frames, strict=True))
    gpu, raster = render_skia(binary, ir_for(binary, (track, style)), times)
    assert_glow_parity(times, reference, gpu, raster, 'mixed_reverse_bitmap_guide', check_p00_edges=True)


@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_bitmap_guide_out_of_order_seek_is_deterministic(binary, project, tmp_path, target):
    case, track = p00_guide(tmp_path, family=project[1].font_family)
    ir = ir_for(binary, (track, case.style))
    order = [2600, 601, 199, 1250, 250, 1501, 900, 4200]
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configure(process, ir)
        ordered, _ = collect(process, sorted(order))
        shuffled, _ = collect(process, order, generation=2)
    by_time = dict(zip(sorted(order), ordered))
    assert all(np.array_equal(by_time[t], f) for t, f in zip(order, shuffled))
    assert not np.array_equal(by_time[250], by_time[2600])


def inline_avatar_case(tmp_path, *, family=None):
    case, track = p00_guide(tmp_path, family=family)
    first = replace(track.lines[0].guide_symbol, bitmap_no_decor=True,
                    bitmap_before_path=image(tmp_path / 'offset-avatar.png', 48, 32, '#41ECFD'),
                    bitmap_after_path=None, bitmap_zoom_percent=80,
                    bitmap_margin_right_px=-999, bitmap_margin_bottom_px=-80)
    second = replace(first, bitmap_margin_right_px=4, bitmap_margin_bottom_px=0,
                     bitmap_before_path=image(tmp_path / 'inline-avatar.png', 28, 42, '#22FA7B'),
                     bitmap_after_path=image(tmp_path / 'clear-avatar.png', 32, 30, '#006faa20'),
                     bitmap_zoom_percent=100)
    line = track.lines[0]
    chars = [replace(c, role_label='avatar') if i in {0, 2} else c for i, c in enumerate(line.chars)]
    track = replace(track, lines=[replace(line, chars=chars, guide_symbol=None, inline_guide_symbols={0: first, 2: second})])
    style = replace(case.style, custom_style_schemes={'avatar': SubtitleStyleScheme(font_size_px=64, stroke_width_px=4)})
    return track, style


def test_role_tagged_inline_avatars_preserve_cells_and_clocks(binary, project, tmp_path):
    track, style = inline_avatar_case(tmp_path, family=project[1].font_family)
    ir = ir_for(binary, (track, style))
    feature = 'p08_inline_bitmap_guides'
    assert feature in ir['required_features']
    source, = ir['track']['lines']
    final, = ir['skia_layout']['lines']
    assert [i for i, c in enumerate(source['chars']) if 'bitmap_guide' in c] == [0, 2]
    assert final['cells'][0]['metrics']['advance'] == 0
    assert final['cells'][2]['metrics']['ink'][3] - final['cells'][2]['metrics']['ink'][1] == 64
    invalid = copy.deepcopy(ir)
    invalid['required_features'].remove(feature)
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        configure(process, ir)
        frames, _ = collect(process, [1250, 2600, 1250])
        with pytest.raises(NativeRendererError):
            configure(process, invalid)
        replay, _ = collect(process, [2600], generation=2)
    assert frames[0][..., 3].any() and np.array_equal(frames[0], frames[-1])
    assert np.array_equal(frames[1], replay[0]) and not np.array_equal(frames[1], frames[0])


def test_reverse_inline_avatars_wipe_from_right_to_left(binary, project, tmp_path):
    track, style = inline_avatar_case(tmp_path, family=project[1].font_family)
    line = track.lines[0]
    before = image(tmp_path / 'visible.png', 32, 32, '#41ECFD')
    after = image(tmp_path / 'transparent.png', 32, 32, '#0041ECFD')
    # Use explicit flat images to make the alpha change and its direction observable.
    transparent = QImage(32, 32, QImage.Format.Format_ARGB32)
    transparent.fill(QColor('#0041ECFD'))
    assert transparent.save(after)
    symbol = replace(line.inline_guide_symbols[2], bitmap_before_path=before, bitmap_after_path=after)
    line = replace(line, chars=line.chars[:3], guide_symbol=None, wipe_reverse=True,
                   inline_guide_symbols={index: symbol for index in range(3)})
    track = replace(track, lines=[line], rubies=[])
    ir = ir_for(binary, (track, style))
    feature = 'p08_reverse_bitmap_guides'
    assert feature in ir['required_features']
    source, = ir['track']['lines']
    final, = ir['skia_layout']['lines']
    assert [c['clock_index'] for c in final['cells']] == [2, 1, 0]
    start, end = source['resolved_intervals'][0]
    invalid = copy.deepcopy(ir)
    invalid['required_features'].remove(feature)
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        configure(process, ir)
        frames, _ = collect(process, [start, (start + end) // 2, start])
        with pytest.raises(NativeRendererError):
            configure(process, invalid)
    assert np.array_equal(frames[0], frames[2])
    removed = np.where((frames[0][..., 3] > 200) & (frames[1][..., 3] < 10))[1]
    remaining = np.where(frames[1][..., 3] > 200)[1]
    assert removed.size > 100 and remaining.size > 100
    assert removed.min() >= remaining.max()


@GPU
@pytest.mark.parametrize('painted', [False, True], ids=['solid', 'gradient'])
def test_role_tagged_inline_avatars_match_d2d_gpu(binary, application, tmp_path, painted):
    from test_subtitle_render_gpu_backend import _render_g1_frames
    from test_subtitle_skia_static_glow import assert_glow_parity

    track, style = inline_avatar_case(tmp_path)
    if painted:
        from test_subtitle_skia_roles import role_outline_palette

        palette = role_outline_palette()
        gradient = PaintFill(mode='gradient_vertical', start_color='#FF6010', end_color='#30CFD0')
        split = PaintFill(mode='split_vertical', split_top_color='#3070A0', split_bottom_color='#AB1080')
        colors = replace(palette, after=replace(palette.after, text=gradient, stroke=gradient, stroke2=split))
        style = replace(style, custom_style_schemes={'avatar': replace(style.custom_style_schemes['avatar'],
                        karaoke_colors=colors, ruby_karaoke_colors=colors, ruby_colors_follow_main=False,
                        stroke2_enabled=True, stroke2_width_px=4, ruby_stroke_width_px=4,
                        ruby_stroke2_enabled=True, ruby_stroke2_width_px=3)},
                        entry_anim='fade', entry_lead_ms=500)
        line = track.lines[0]
        chars = [replace(c, role_label='avatar') if index >= 3 else c for index, c in enumerate(line.chars)]
        track = replace(track, lines=[replace(line, chars=chars)])
        ir = ir_for(binary, (track, style))
        assert {'p06_role_outline_gradient', 'p06_ruby_outline_gradient'} <= set(ir['required_features'])
    times = (1201, 1500, 1900, 2150, 2400, 2750, 3400, 4050)
    with NativeRendererProcess() as process:
        _, raw = _render_g1_frames(process, style, times, force_warp=False, track=track,
                                   width=1920, height=1080, dpr=1, realization_enabled=False)
    refs = [np.frombuffer(p, np.uint8).reshape(1080, 1920, 4) for p in raw]
    rendered = []
    for target in ('gpu', 'raster'):
        with SkiaRendererProcess(binary, selection=selection(target)) as process:
            process.configure(track, style, width=1920, height=1080, fps=60)
            frames, _ = collect(process, times)
            rendered.append(frames)
    assert_glow_parity(times, refs, *rendered, 'inline-avatar', check_p00_edges=True)
    for color in ((65, 236, 253), (34, 250, 123)):
        def avatar(frame):
            return (frame[..., 3] > 230) & (np.abs(frame[..., :3].astype(np.int16) - color).max(axis=-1) < 8)

        seen = False
        for time_ms, ref, gpu in zip(times, refs, rendered[0], strict=True):
            expected = avatar(ref)
            if expected.sum() > 100:
                seen = True
                actual = avatar(gpu)
                assert (expected & actual).sum() / (expected | actual).sum() >= .95, (time_ms, color)
        assert seen, color


def test_bitmap_guide_large_native_halo_keeps_previous_scene(binary, project, tmp_path):
    case, track = p00_guide(tmp_path, family=project[1].font_family)
    valid = ir_for(binary, (track, case.style))
    line = track.lines[0]
    guide = replace(line.guide_symbol, bitmap_zoom_percent=1, bitmap_after_path=None,
                    bitmap_before_path=image(tmp_path / 'magnified-halo.png', 256, 256))
    oversized = ir_for(binary, (replace(track, lines=[replace(line, guide_symbol=guide)]), case.style))
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        configure(process, valid)
        before, _ = collect(process, [600])
        with pytest.raises(NativeRendererError, match='光晕尺寸超限'):
            configure(process, oversized)
        after, _ = collect(process, [600], generation=2)
    assert before[0].any() and np.array_equal(before[0], after[0])


VARIANTS = {
    'original': {},
    'before_only': {'after_path': None},
    'no_decor': {'bitmap_no_decor': True},
    'fixed_margins': {'bitmap_fix_size': True, 'bitmap_margin_left_px': 7, 'bitmap_margin_right_px': -3,
                      'bitmap_margin_bottom_px': 6},
    'zoom_two_images': {'bitmap_zoom_percent': 130, 'after_path': 'after'},
    'anchored': {'prefix_timing': 'anchored'},
    'displaced': {'bitmap_margin_bottom_px': -115, 'bitmap_margin_right_px': -999},
}


@GPU
@pytest.mark.parametrize('variant', list(VARIANTS))
def test_p00_bitmap_guide_matches_actual_gpu(binary, application, tmp_path, variant):
    """Original decorated-text budgets, D2D GPU vs Skia Vulkan and Vulkan vs Raster."""
    from test_subtitle_render_gpu_backend import _render_g1_frames
    from test_subtitle_skia_protocol import collect as collect_frames
    from test_subtitle_skia_static_glow import assert_glow_parity
    changes = dict(VARIANTS[variant])
    after = changes.pop('after_path', ...)
    if after is None:
        changes['bitmap_after_path'] = None
    elif after == 'after':
        changes['bitmap_after_path'] = image(tmp_path / 'after.png', 18, 12, '#3070FF')
    case, track = p00_guide(tmp_path, **changes)
    with NativeRendererProcess() as process:
        _, raw = _render_g1_frames(process, case.style, TIMES, force_warp=False, track=track,
                                   width=1920, height=1080, dpr=1, realization_enabled=False)
    reference = [np.frombuffer(p, np.uint8).reshape(1080, 1920, 4) for p in raw]
    rendered = []
    for target in ('gpu', 'raster'):
        with SkiaRendererProcess(binary, selection=selection(target)) as process:
            process.configure(track, case.style, width=1920, height=1080, fps=60)
            frames, _ = collect_frames(process, TIMES)
            rendered.append(frames)
    assert_glow_parity(TIMES, reference, *rendered, f'guide_bitmap-{variant}', check_p00_edges=True)
    # The whole-frame budget must not hide the guide: compare its own region too.
    region = (slice(440, 820 if variant == 'displaced' else 700), slice(560, 800))
    for t, ref, gpu in zip(TIMES, reference, rendered[0]):
        def premultiplied(frame):
            frame = frame[region].astype(float)
            frame[..., :3] *= frame[..., 3:] / 255
            return frame
        diff = abs(premultiplied(ref) - premultiplied(gpu))
        assert diff.mean() <= 5 and np.percentile(diff, 95) <= 24, (variant, t, diff.mean())
