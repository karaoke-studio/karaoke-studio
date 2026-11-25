"""Inline roles share canonical timing/geometry and per-cell solid paints."""
import copy
from dataclasses import replace
import json
import sys

import numpy as np
import pytest
from PyQt6.QtGui import QColor

from test_subtitle_skia_font_slots import application, binary, project, mixed
from test_subtitle_skia_resolved import ir_for
from test_subtitle_skia_protocol import collect, selection
from krok_helper.subtitle_render.domain.models import RubyAnnotation, SubtitleStyleScheme, TimingChar, TimingLine
from krok_helper.subtitle_render.domain.paint import KaraokeColors, KaraokeColorState, PaintFill
from krok_helper.subtitle_render.engine.render.skia_ir import build_skia_render_ir
from krok_helper.subtitle_render.engine.style.style_semantics import effective_karaoke_colors, style_for_role_in_layout
from krok_helper.subtitle_render.engine.layout.line.style import style_for_line
from krok_helper.subtitle_render.engine.text.metric_snapshot import CellMetric, LineMetric, TextMetricSnapshot
from krok_helper.subtitle_render.native.backend import NativeRendererError, NativeRendererProcess
from krok_helper.subtitle_render.native.skia_backend import SkiaRendererProcess
from krok_helper.subtitle_render.native.skia_fonts import resolve_skia_font
from krok_helper.subtitle_render.native.skia_text import SkiaTextMetricsProcess


def colors(before,after):
    return KaraokeColors(before=KaraokeColorState(text=PaintFill(color=before)),
                         after=KaraokeColorState(text=PaintFill(color=after)))


def role_outline_palette():
    return KaraokeColors(
        before=KaraokeColorState(text=PaintFill(color='#F4E7C0'), stroke=PaintFill(color='#48207C'),
                                 stroke2=PaintFill(color='#C21834')),
        after=KaraokeColorState(text=PaintFill(color='#177FDC'), stroke=PaintFill(color='#15742C'),
                                stroke2=PaintFill(color='#EFAC24')),
    )


def textured_roles(track, style, tmp_path, direction):
    from test_subtitle_skia_image_fills import pattern, write_png

    path = write_png(tmp_path / 'role-paint.png', pattern('transparent'))
    palette = role_outline_palette()
    def role(before, after, size, edge):
        return SubtitleStyleScheme(font_size_px=size, stroke_width_px=edge,
            stroke2_enabled=True, stroke2_width_px=3, karaoke_colors=replace(
                palette, before=replace(palette.before, text=before), after=replace(palette.after, text=after)))
    gradient = PaintFill(mode=direction, start_color='#EE491C', end_color='#11E8BC')
    changed = PaintFill(mode=direction, start_color='#42ADE6', end_color='#DF0CAA')
    image = PaintFill(mode='image', image_path=str(path), image_scale_pct=135, color='#9354A0')
    roles = {'a': role(gradient, changed, 83, 3), 'b': role(image, image, 91, 6)}
    line = track.lines[0]
    chars = [replace(char, role_label=('a', 'b', None)[index % 3]) for index, char in enumerate(line.chars)]
    track = replace(track, lines=[replace(line, chars=chars)], rubies=[])
    return track, replace(style, custom_style_schemes=roles)


def independent_ruby_paints(style):
    palette = colors('#A932FD', '#7895F0')
    roles = {name: replace(scheme, ruby_colors_follow_main=False, ruby_karaoke_colors=palette)
             for name, scheme in style.custom_style_schemes.items()}
    return replace(style, ruby_colors_follow_main=False, ruby_karaoke_colors=palette, custom_style_schemes=roles)


@pytest.mark.parametrize('target', ['gpu', 'raster'])
@pytest.mark.parametrize('ruby', [False, True], ids=['body', 'upper'])
def test_body_role_paints_follow_the_glyphs_and_seek(binary, project, tmp_path, target, ruby):
    track, style = textured_roles(*project, tmp_path, 'gradient_horizontal')
    if ruby:
        chars = track.lines[0].chars
        track = replace(track, rubies=[RubyAnnotation(kanji='A字', reading='AA',
            pos_start_ms=chars[0].start_ms, pos_end_ms=chars[2].start_ms)])
        style = independent_ruby_paints(style)
    ir = ir_for(binary, (track, style))
    assert 'p06_role_body_paints' in ir['required_features']
    if ruby:
        assert 'p05_upper_ruby' in ir['required_features'] and ir['skia_layout']['lines'][0]['ruby_groups']
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        process.configure(track, style, width=640, height=360, fps=60)
        frames, _ = collect(process, [300, 1000, 1500, 300])
        replay, _ = collect(process, [1000], generation=2)
    assert frames[0].any() and np.array_equal(frames[0], frames[-1])
    assert np.array_equal(frames[1], replay[0])
    assert not np.array_equal(frames[0], frames[2])
    assert np.unique(frames[2][frames[2][..., 3] == 255, :3], axis=0).shape[0] > 40


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('direction,ruby', [
    ('gradient_horizontal', 'none'), ('gradient_vertical', 'none'),
    ('gradient_horizontal', 'upper'), ('gradient_vertical', 'both'),
])
def test_body_role_paints_match_actual_d2d_gpu(binary, application, tmp_path, direction, ruby):
    from scripts.subtitle_gpu_baseline.cases import build_cases
    from test_subtitle_skia_character_fade import assert_ruby_transition_gpu_parity

    base = next(case for case in build_cases(tmp_path) if case.case_id == 'n3_grid')
    style = replace(base.style, stroke_width_px=0, stroke2_enabled=False, stroke2_width_px=0,
                    decoration_kind='none', karaoke_anim='none')
    track, style = textured_roles(base.track, style, tmp_path, direction)
    if ruby != 'none':
        track = replace(track, rubies=base.track.rubies)
        style = replace(independent_ruby_paints(style), lower_ruby_enabled=ruby == 'both')
    case = replace(base, case_id=f'role_body_paints_{direction}_{ruby}', track=track, style=style)
    assert_ruby_transition_gpu_parity(binary, case, (299, 300, 1500, 1750, 2600, 4400, 5499, 5500))


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
def test_role_outline_paints_match_actual_d2d_gpu_without_bitmap_guides(binary, application, tmp_path):
    from scripts.subtitle_gpu_baseline.cases import build_cases
    from test_subtitle_skia_character_fade import assert_ruby_transition_gpu_parity
    from test_subtitle_skia_image_fills import pattern, write_png

    base = next(case for case in build_cases(tmp_path, include_real=False) if case.case_id == 'n3_grid')
    style = replace(base.style, stroke_width_px=0, stroke2_enabled=False, decoration_kind='none', karaoke_anim='none')
    track, style = textured_roles(base.track, style, tmp_path, 'gradient_horizontal')
    vertical = PaintFill(mode='gradient_vertical', start_color='#D02050', end_color='#5318D1')
    horizontal = PaintFill(mode='gradient_horizontal', start_color='#126E3D', end_color='#AD1D98')
    split = PaintFill(mode='split_vertical', split_top_color='#6020AC', split_bottom_color='#F4A829')
    path = write_png(tmp_path / 'role-outline.png', pattern('transparent'))
    image = PaintFill(mode='image', image_path=str(path), image_scale_pct=95)
    roles = dict(style.custom_style_schemes)
    palette = roles['a'].karaoke_colors
    palette = replace(palette, before=replace(palette.before, stroke=vertical, stroke2=split),
                      after=replace(palette.after, stroke=horizontal, stroke2=image))
    roles['a'] = replace(roles['a'], karaoke_colors=palette)
    case = replace(base, case_id='role_outline_paints', track=track, style=replace(style, custom_style_schemes=roles))
    assert_ruby_transition_gpu_parity(binary, case, (299, 300, 1500, 1750, 2600, 4400, 5499, 5500))


def ruby_role_paints_case(tmp_path, direction, shared):
    from scripts.subtitle_gpu_baseline.cases import build_cases

    case = next(case for case in build_cases(tmp_path) if case.case_id == 'n3_grid')
    style = replace(case.style, stroke_width_px=0, stroke2_enabled=False, stroke2_width_px=0,
                    decoration_kind='none', karaoke_anim='none', ruby_horizontal_gradient_with_main=shared)
    track, style = textured_roles(case.track, style, tmp_path, direction)
    rubies = [RubyAnnotation(kanji='星光', reading='ほし', pos_start_ms=1200, pos_end_ms=2300),
             *case.track.rubies]
    return replace(case, case_id=f'ruby_role_paints_{direction}_{shared}',
                   track=replace(track, rubies=rubies), style=style)


@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_ruby_role_paints_follow_the_group_and_preserve_seek(binary, project, tmp_path, target):
    track, style = textured_roles(*project, tmp_path, 'gradient_horizontal')
    track = replace(track, rubies=[RubyAnnotation(kanji='A', reading='AA', pos_start_ms=200, pos_end_ms=400),
                                  RubyAnnotation(kanji='字', reading='AA', pos_start_ms=400, pos_end_ms=600)])
    ir = ir_for(binary, (track, style))
    assert 'p06_ruby_role_paints' in ir['required_features']
    assert [group['style_cell'] for group in ir['skia_layout']['lines'][0]['ruby_groups']] == [0, 1]
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        process.configure(track, style, width=640, height=360, fps=60)
        frames, _ = collect(process, [100, 350, 550, 100])
        replay, _ = collect(process, [350], generation=2)
    assert frames[0].any() and np.array_equal(frames[0], frames[-1])
    assert np.array_equal(frames[1], replay[0])
    assert not np.array_equal(frames[1], frames[2])


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('direction,shared', [
    ('gradient_horizontal', True), ('gradient_horizontal', False), ('gradient_vertical', True),
])
def test_ruby_role_paints_match_actual_d2d_gpu(binary, application, tmp_path, direction, shared):
    from test_subtitle_skia_character_fade import assert_ruby_transition_gpu_parity

    case = ruby_role_paints_case(tmp_path, direction, shared)
    assert_ruby_transition_gpu_parity(binary, case, (300, 1200, 1500, 1950, 2600, 3500, 3900, 4499))


def glowing_role_scheme(scheme, before, after, radii, concentration):
    palette = scheme.karaoke_colors
    return replace(scheme, decoration_kind='glow', decoration_kind_before='glow', decoration_before_sync=False,
                   glow_before_radius_px=radii[0], glow_after_radius_px=radii[1],
                   glow_before_concentration_level=0, glow_concentration_level=concentration,
                   karaoke_colors=replace(palette,
                       before=replace(palette.before, shadow=PaintFill(color=before)),
                       after=replace(palette.after, shadow=PaintFill(color=after))))


def role_static_glow_style(style, *, base=False):
    schemes = style.custom_style_schemes
    result = replace(style, decoration_kind='none', custom_style_schemes={
        'a': glowing_role_scheme(schemes['a'], '#B0F02440', '#FF22B355', (7, 11), 1),
        'b': glowing_role_scheme(schemes['b'], '#FF2669F2', '#A0F3BA10', (11, 6), 2),
    })
    return glowing_role_scheme(result, '#90FA21DA', '#C01DAED2', (8, 9), 0) if base else result


def gradient_glow_style(style, mode, *, base=False):
    style = role_static_glow_style(style, base=base)
    gradient = PaintFill(mode=mode, start_color='#B0F02440', end_color='#B0247CE0',
                         split_stops=[(0, '#B0F02440'), (45, '#B0247CE0'), (100, '#B0247CE0')])

    def apply_palette(palette):
        if mode == 'split_vertical':
            return replace(palette, after=replace(palette.after, shadow=gradient))
        return replace(palette, before=replace(palette.before, shadow=gradient))

    def apply(appearance):
        palette = appearance.karaoke_colors
        ruby = appearance.ruby_karaoke_colors or palette
        return replace(appearance,
                       karaoke_colors=apply_palette(palette),
                       ruby_colors_follow_main=False,
                       ruby_karaoke_colors=apply_palette(ruby))

    style = replace(style, custom_style_schemes={
        name: apply(appearance) for name, appearance in style.custom_style_schemes.items()
    })
    return apply(style) if base else style


GLOW_PAINT_FEATURES = {'gradient_vertical': 'p06_vertical_gradient_glow',
                       'gradient_horizontal': 'p06_role_gradient_glow',
                       'split_vertical': 'p06_role_gradient_glow'}


@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_role_static_glow_follows_the_local_wipe_and_seek(binary, project, tmp_path, target):
    track, plain = textured_roles(*project, tmp_path, 'gradient_horizontal')
    style = role_static_glow_style(plain)
    ir = ir_for(binary, (track, style))
    assert 'p06_role_static_glow' in ir['required_features']
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        process.configure(track, style, width=640, height=360, fps=60)
        frames, _ = collect(process, [150, 300, 500, 150])
        replay, _ = collect(process, [300], generation=2)
        process.configure(track, replace(plain, decoration_kind='none'), width=640, height=360, fps=60)
        no_glow, _ = collect(process, [150], generation=3)
    assert np.array_equal(frames[0], frames[-1]) and np.array_equal(frames[1], replay[0])
    assert not np.array_equal(frames[1], frames[2])
    assert np.count_nonzero((frames[0][..., 3] > 0) & (no_glow[0][..., 3] == 0)) > 20


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('painted,base_glow', [(False, False), (True, True)])
def test_role_static_glow_matches_actual_d2d_gpu(binary, application, tmp_path, painted, base_glow):
    from scripts.subtitle_gpu_baseline.cases import build_cases
    from test_subtitle_skia_character_fade import assert_ruby_transition_gpu_parity

    baseline = next(case for case in build_cases(tmp_path) if case.case_id == 'n3_grid')
    style = replace(baseline.style, stroke_width_px=0, stroke2_enabled=False, stroke2_width_px=0,
                    decoration_kind='none', karaoke_anim='none')
    track, style = textured_roles(baseline.track, style, tmp_path, 'gradient_horizontal')
    if not painted:
        style = replace(style, custom_style_schemes={name: replace(scheme, karaoke_colors=role_outline_palette())
                                                    for name, scheme in style.custom_style_schemes.items()})
    case = replace(baseline, case_id=f'role_static_glow_{painted}_{base_glow}',
                   track=track, style=role_static_glow_style(style, base=base_glow))
    assert_ruby_transition_gpu_parity(binary, case, (300, 1200, 1500, 1950, 2600, 3500, 3900, 4499))


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('ruby,karaoke', [(False, 'none'), (True, 'none'), (True, 'utopia')])
def test_role_image_glow_matches_actual_d2d_gpu(binary, application, tmp_path, ruby, karaoke):
    """A role glow painted with an image blurs the image brush like the GPU."""
    from test_subtitle_skia_character_fade import assert_ruby_transition_gpu_parity
    from test_subtitle_skia_image_fills import pattern, write_png

    image = str(write_png(tmp_path / 'role_glow.png', pattern('transparent')))
    case = ruby_role_paints_case(tmp_path, 'gradient_horizontal', True)
    style = role_static_glow_style(case.style)

    def textured(appearance):
        palette = appearance.karaoke_colors
        ruby_palette = appearance.ruby_karaoke_colors or palette
        paint = PaintFill(mode='image', image_path=image, image_scale_pct=137, color='#B0F02440')
        return replace(appearance,
                       karaoke_colors=replace(palette, after=replace(palette.after, shadow=paint)),
                       ruby_karaoke_colors=replace(ruby_palette, before=replace(ruby_palette.before, shadow=paint)))

    roles = dict(style.custom_style_schemes)
    roles['a'] = textured(roles['a'])
    style = replace(style, custom_style_schemes=roles, karaoke_anim=karaoke)
    track = case.track if ruby else replace(case.track, rubies=[])
    case = replace(case, case_id=f'role_image_glow_{ruby}_{karaoke}', track=track, style=style)
    assert_ruby_transition_gpu_parity(binary, case, (300, 1200, 1500, 1950, 2600, 3500, 3900, 4499))


@pytest.mark.parametrize('target', ['gpu', 'raster'])
@pytest.mark.parametrize('karaoke', ['none', 'utopia'])
def test_ruby_role_glow_preserves_other_groups_and_seek(binary, project, tmp_path, target, karaoke):
    track, style = textured_roles(*project, tmp_path, 'gradient_horizontal')
    style = replace(role_static_glow_style(style), karaoke_anim=karaoke)
    track = replace(track, rubies=[RubyAnnotation(kanji='A', reading='AA', pos_start_ms=200, pos_end_ms=400),
                                  RubyAnnotation(kanji='字', reading='AA', pos_start_ms=400, pos_end_ms=600)])
    ir = ir_for(binary, (track, style))
    assert {'p06_ruby_role_glow', 'p06_role_static_glow', 'p06_ruby_role_paints'} <= set(ir['required_features'])
    assert ('p07_role_singing_glow' in ir['required_features']) == (karaoke == 'utopia')
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        process.configure(track, style, width=640, height=360, fps=60)
        frames, _ = collect(process, [150, 300, 500, 150])
        replay, _ = collect(process, [500], generation=2)
    assert np.array_equal(frames[0], frames[-1]) and np.array_equal(replay[0], frames[2])
    assert not np.array_equal(frames[1], frames[2])


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('direction', ['gradient_horizontal', 'gradient_vertical'])
def test_ruby_role_glow_matches_actual_d2d_gpu(binary, application, tmp_path, direction):
    from test_subtitle_skia_character_fade import assert_ruby_transition_gpu_parity

    case = ruby_role_paints_case(tmp_path, direction, True)
    case = replace(case, style=role_static_glow_style(case.style))
    assert_ruby_transition_gpu_parity(binary, case, (300, 1200, 1500, 1950, 2600, 3500, 3900, 4499))


def independent_latin_outline(style):
    roles = dict(style.custom_style_schemes)
    roles['a'] = replace(roles['a'], latin_stroke_width_px=9)
    return replace(style, stroke_width_px=6, latin_stroke_width_px=3, custom_style_schemes=roles)


def latin_role_text(track):
    line, = track.lines
    chars = list(line.chars)
    chars[2] = replace(chars[2], text='A')
    chars[3] = replace(chars[3], text='B')
    return replace(track, lines=[replace(line, chars=chars)])


@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_latin_outline_combines_with_role_paints_and_upper_glow(binary, project, tmp_path, target):
    track, style = textured_roles(*project, tmp_path, 'gradient_horizontal')
    style = independent_latin_outline(role_static_glow_style(style))
    style = replace(style, font_reference_height=360)
    line, = track.lines
    chars = list(line.chars)
    chars[2] = replace(chars[2], text='A')
    track = replace(track, lines=[replace(line, chars=chars)],
                    rubies=[RubyAnnotation(kanji='A字', reading='AA', pos_start_ms=200, pos_end_ms=600)])
    ir = ir_for(binary, (track, style), dpr=1)
    assert {'p06_latin_primary_outline', 'p06_role_body_paints', 'p06_ruby_role_glow'} <= set(ir['required_features'])
    cells = ir['skia_layout']['lines'][0]['cells']
    pads = [cells[i]['origin_x'] + cells[i]['metrics']['ink'][0] - cells[i]['wipe_left'] for i in (0, 2)]
    assert pads == pytest.approx([4, 1])
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        process.configure(track, style, width=640, height=360, fps=60)
        frames, _ = collect(process, [300, 750])
        replay, _ = collect(process, [750, 300], generation=2)
    assert frames[0].any() and frames[1].any() and not np.array_equal(*frames)
    np.testing.assert_array_equal(frames[0], replay[1])
    np.testing.assert_array_equal(frames[1], replay[0])


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('direction,base_glow', [
    ('gradient_horizontal', False),
    ('gradient_horizontal', True),
    ('gradient_vertical', False),
])
def test_latin_outline_with_role_paints_and_upper_glow_matches_actual_gpu(binary, tmp_path, direction, base_glow):
    from test_subtitle_skia_character_fade import assert_ruby_transition_gpu_parity

    case = ruby_role_paints_case(tmp_path, direction, True)
    style = independent_latin_outline(role_static_glow_style(case.style, base=base_glow))
    track = latin_role_text(case.track)
    case = replace(case, case_id='latin_outline_role_paints_ruby_glow', track=track, style=style)
    assert_ruby_transition_gpu_parity(binary, case, (300, 1200, 1500, 1950, 2600, 2800, 3500, 3900, 4499))


@pytest.mark.parametrize('target', ['gpu', 'raster'])
@pytest.mark.parametrize('glow', ['none', 'solid', *GLOW_PAINT_FEATURES])
def test_role_line_fade_composes_body_paints_outlines_and_ruby(binary, project, tmp_path, target, glow):
    from test_subtitle_skia_line_fade import premultiplied
    from test_subtitle_skia_ruby_groups import configure

    track, style = textured_roles(*project, tmp_path, 'gradient_horizontal')
    track = replace(track, rubies=[RubyAnnotation(kanji='A字', reading='AA', pos_start_ms=200, pos_end_ms=600)])
    if glow == 'solid':
        style = role_static_glow_style(style)
    elif glow in GLOW_PAINT_FEATURES:
        style = gradient_glow_style(style, glow)
    fade = replace(style, entry_anim='fade', entry_lead_ms=200, exit_anim='fade', exit_fade_ms=400)
    ir = ir_for(binary, (track, fade), dpr=1.5)
    opaque = ir_for(binary, (track, style), dpr=1.5)
    features = set(ir['required_features'])
    assert {'p06_role_body_paints', 'p06_ruby_role_paints', 'p07_role_line_fade'} <= features
    assert ('p06_ruby_role_glow' in features) == (glow != 'none')
    glow_feature = GLOW_PAINT_FEATURES.get(glow)
    assert features.intersection(GLOW_PAINT_FEATURES.values()) == ({glow_feature} if glow_feature else set())
    line, = ir['track']['lines']
    entry, exit_ = line['entry_duration_ms'], line['exit_duration_ms']
    assert entry > 0 and exit_ > 0
    start, end = line['display_start_ms'], line['display_end_ms']
    times = [start + entry // 2, start + entry, end - exit_ // 2, end - 1]
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        process._send({'cmd': 'configure', 'ir': ir})
        process._expect_ok(process._read_until_event('configured'))
        frames, _ = collect(process, times)
        replay, _ = collect(process, list(reversed(times)), generation=2)
        process._send({'cmd': 'configure', 'ir': opaque})
        process._expect_ok(process._read_until_event('configured'))
        full, _ = collect(process, times, generation=3)
    for actual, again in zip(frames, reversed(replay), strict=True):
        np.testing.assert_array_equal(actual, again)
    opacity = [0.75, 1.0, 0.25, 1.0 / exit_**2]
    for time_ms, actual, baseline, strength in zip(times, frames, full, opacity, strict=True):
        assert baseline[..., 3].max() == 255
        difference = np.abs(premultiplied(actual) - premultiplied(baseline) * strength)
        assert difference.max() <= 2, (time_ms, strength, difference.max())


@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_role_line_fade_accepts_a_glowing_line_without_roles_in_the_same_source(binary, project, tmp_path, target):
    from test_subtitle_skia_ruby_groups import configure

    track, style = textured_roles(*project, tmp_path, 'gradient_horizontal')
    first, = track.lines
    second = replace(first, chars=[replace(cell, start_ms=cell.start_ms + 3000, role_label=None)
                                   for cell in first.chars], end_ms=first.end_ms + 3000,
                     display_start_override_ms=3000, display_end_override_ms=first.end_ms + 3500)
    track = replace(track, lines=[first, second])
    style = replace(style, karaoke_colors=colors('#FF0000', '#00FF00'))
    style = gradient_glow_style(style, 'gradient_vertical', base=True)
    style = replace(style, entry_anim='fade', entry_lead_ms=200, exit_anim='fade', exit_fade_ms=300)
    ir = ir_for(binary, (track, style))
    features = set(ir['required_features'])
    assert {'p06_role_body_paints', 'p06_vertical_gradient_glow', 'p07_role_line_fade'} <= features
    assert 'p07_horizontal_glow_line_animation' not in features
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        process._send({'cmd': 'configure', 'ir': ir})
        process._expect_ok(process._read_until_event('configured'))
        frames, _ = collect(process, [450, 3450, 450])
    assert all(frame[..., 3].max() == 255 for frame in frames)
    assert np.array_equal(frames[0], frames[2]) and not np.array_equal(frames[0], frames[1])


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('direction,glow,latin,shared', [
    ('gradient_horizontal', 'none', False, True), ('gradient_vertical', 'none', False, True),
    ('gradient_horizontal', 'solid', False, True), ('gradient_vertical', 'solid', False, True),
    ('gradient_horizontal', 'solid', True, True), ('gradient_vertical', 'gradient_vertical', False, True),
    ('gradient_horizontal', 'gradient_horizontal', False, False),
    ('gradient_horizontal', 'gradient_horizontal', True, True),
    ('gradient_vertical', 'gradient_vertical', True, True),
    ('gradient_vertical', 'split_vertical', False, True),
])
def test_role_line_fade_with_body_and_ruby_paints_matches_actual_gpu(binary, tmp_path, direction, glow, latin, shared):
    from test_subtitle_skia_character_fade import assert_ruby_transition_gpu_parity

    case = ruby_role_paints_case(tmp_path, direction, shared)
    style = replace(case.style, entry_anim='fade', entry_lead_ms=600, exit_anim='fade', exit_fade_ms=600)
    if glow == 'solid':
        style = role_static_glow_style(style, base=latin)
    elif glow in GLOW_PAINT_FEATURES:
        style = gradient_glow_style(style, glow, base=True)
    if latin:
        style = independent_latin_outline(style)
        case = replace(case, track=latin_role_text(case.track))
    case = replace(case, case_id=f'role_paint_line_fade_{direction}_{glow}_{latin}_{shared}', style=style)
    assert_ruby_transition_gpu_parity(binary, case, (300, 420, 650, 900, 1950, 3900, 5100, 5450, 5500))


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
def test_ruby_role_glow_with_static_title_and_extra_track(binary, application, tmp_path):
    from scripts.subtitle_gpu_baseline.cases import build_cases
    from test_subtitle_skia_character_fade import assert_ruby_transition_gpu_parity

    original = next(case for case in build_cases(tmp_path) if case.case_id == 'g3_heavy_roles_fills_title_multisource')
    case = replace(original, case_id='static_heavy_role_glow', style=replace(original.style, karaoke_anim='none'))
    assert_ruby_transition_gpu_parity(binary, case, case.timestamps_ms, extra_tracks=case.extra_tracks)


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('case_id', ['tactic_like_three_glow_blue_white', 'g3_heavy_roles_fills_title_multisource'])
def test_role_singing_glow_original_p00_matches_gpu(binary, application, tmp_path, case_id):
    from scripts.subtitle_gpu_baseline.cases import build_cases
    from test_subtitle_skia_character_fade import assert_ruby_transition_gpu_parity

    case = next(case for case in build_cases(tmp_path) if case.case_id == case_id)
    assert case.style.karaoke_anim == 'utopia'
    assert_ruby_transition_gpu_parity(binary, case, case.timestamps_ms, extra_tracks=case.extra_tracks)


@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_role_singing_scale_replays_with_local_paints(binary, project, tmp_path, target):
    track, style = textured_roles(*project, tmp_path, 'gradient_horizontal')
    style = replace(style, karaoke_anim='utopia')
    ir = ir_for(binary, (track, style))
    assert {'p07_role_singing_scale', 'p06_role_body_paints'} <= set(ir['required_features'])
    undeclared = copy.deepcopy(ir)
    undeclared['required_features'].remove('p07_role_singing_scale')
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        process.configure(track, style, width=640, height=360, fps=60)
        frames, _ = collect(process, [150, 300, 500, 150])
        process._send({'cmd': 'configure', 'ir': undeclared})
        process._expect_ok(process._read_until_event('configured'))
        replay, _ = collect(process, [500], generation=2)
    assert np.array_equal(frames[0], frames[-1]) and np.array_equal(replay[0], frames[2])
    assert not np.array_equal(frames[1], frames[2])


def test_faded_scanline_with_role_gradients_and_reading_glow(binary, project, tmp_path):
    track, style = textured_roles(*project, tmp_path, 'gradient_vertical')
    track = replace(track, rubies=[RubyAnnotation(kanji='字', reading='AA', pos_start_ms=400, pos_end_ms=600)])
    roles = style.custom_style_schemes
    palette = roles['a'].karaoke_colors
    style = replace(style, karaoke_colors=role_outline_palette(),
                    custom_style_schemes={**roles, 'b': replace(roles['b'], karaoke_colors=palette)})
    style = replace(role_static_glow_style(style, base=True), karaoke_anim='utopia',
                    scanline_enabled=True, scanline_forward_enabled=True, scanline_mode='color',
                    scanline_color='#FFFFFF', scanline_width_px=21, scanline_glow_px=4,
                    entry_anim='fade', exit_anim='fade', entry_lead_ms=600, exit_fade_ms=600)
    ir = ir_for(binary, (track, style))
    feature = 'p07_inline_role_faded_scanline'
    assert {feature, 'p07_role_singing_glow', 'p06_ruby_role_paints'} <= set(ir['required_features'])
    invalid = copy.deepcopy(ir)
    invalid['required_features'].remove(feature)
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        process.configure(track, style, width=640, height=360, fps=60)
        frames, _ = collect(process, [300, 500, 300])
        process._send({'cmd': 'configure', 'ir': invalid})
        process._expect_ok(process._read_until_event('configured'))
        replay, _ = collect(process, [500], generation=2)
    assert np.array_equal(frames[0], frames[-1]) and np.array_equal(frames[1], replay[0])
    assert frames[0][..., 3].any() and not np.array_equal(frames[0], frames[1])


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('ruby', [False, True], ids=['body', 'upper'])
def test_role_singing_scale_matches_actual_d2d_gpu(binary, application, tmp_path, ruby):
    from test_subtitle_skia_character_fade import assert_ruby_transition_gpu_parity

    case = ruby_role_paints_case(tmp_path, 'gradient_horizontal', True)
    track = case.track if ruby else replace(case.track, rubies=[])
    case = replace(case, track=track, style=replace(case.style, karaoke_anim='utopia'))
    assert_ruby_transition_gpu_parity(binary, case, (300, 1200, 1500, 1950, 2600, 3500, 3900, 4499))


@pytest.mark.parametrize('target', ['gpu', 'raster'])
@pytest.mark.parametrize('with_ruby', [False, True], ids=['body', 'ruby'])
def test_body_role_outlines_follow_their_cell_clock(binary, project, target, with_ruby):
    track, style = project
    line = track.lines[0]
    chars = [replace(char, role_label='edge' if i in {0, 2, 4} else None)
             for i, char in enumerate(line.chars)]
    line = replace(line, chars=chars, display_start_override_ms=0, display_end_override_ms=3000)
    track = replace(track, lines=[line])
    if with_ruby:
        track = replace(track, rubies=[RubyAnnotation(kanji='A字', reading='AA',
            pos_start_ms=chars[0].start_ms, pos_end_ms=chars[2].start_ms)])
    style = replace(style, custom_style_schemes={'edge': SubtitleStyleScheme(
        stroke_width_px=5, stroke2_width_px=3, stroke2_enabled=True, karaoke_colors=role_outline_palette())})
    ir = ir_for(binary, (track, style))
    assert {'p06_role_solid_outlines', 'p06_secondary_solid_outline'} <= set(ir['required_features'])
    if with_ruby:
        assert 'p06_outline_ruby' in ir['required_features'] and ir['skia_layout']['lines'][0]['ruby_groups']
    plain = copy.deepcopy(ir)
    plain['required_features'].remove('p06_role_solid_outlines')
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        process.configure(track, style, width=640, height=360, fps=60)
        frames, _ = collect(process, [100, 1400, 2700, 3000, 1400])
        process._send({'cmd': 'configure', 'ir': plain})
        process._expect_ok(process._read_until_event('configured'))
        replay, _ = collect(process, [1400], generation=2)
    assert not frames[3].any() and np.array_equal(frames[1], frames[-1])
    assert np.array_equal(frames[1], replay[0])
    for frame, color in ((frames[0], [194, 24, 52]), (frames[2], [239, 172, 36])):
        colors = frame[frame[..., 3] == 255, :3]
        assert np.any(np.all(colors == color, axis=-1))


def test_disabled_role_outline_paints_do_not_affect_active_guides(binary, project):
    from test_subtitle_skia_outline_ruby import render_skia
    from test_subtitle_skia_static_glow import assert_glow_parity
    from test_subtitle_skia_vector_guides import SQUARE, guide_source

    track, style = guide_source(project, 'inline')
    line, = track.lines
    chars = [replace(char, role_label='plain') if index == 1 else char for index, char in enumerate(line.chars)]
    track = replace(track, lines=[replace(line, chars=chars)])
    palette = role_outline_palette()
    paint = PaintFill(mode='gradient_horizontal', start_color='#FF102060', end_color='#FFE0E0E0')
    stored = replace(palette, before=replace(palette.before, stroke=paint, stroke2=paint),
                     after=replace(palette.after, stroke=paint, stroke2=paint))
    role = SubtitleStyleScheme(stroke_width_px=0, stroke2_enabled=False, karaoke_colors=stored)
    style = replace(style, stroke_width_px=4, stroke2_enabled=True, stroke2_width_px=3,
                    karaoke_colors=palette, custom_style_schemes={'plain': role})
    clean = replace(style, custom_style_schemes={'plain': replace(role, karaoke_colors=palette)})
    times = (250, 750, 1150, 1750)
    ir = ir_for(binary, (track, style), dpr=1.5)
    assert 'p06_role_body_paints' not in ir['required_features']
    assert ir['track']['lines'][0]['chars'][1]['role_label'] == 'plain'
    gpu, raster = render_skia(binary, ir, times)
    clean_gpu, clean_raster = render_skia(binary, ir_for(binary, (track, clean), dpr=1.5), times)
    assert np.array_equal(gpu, clean_gpu) and np.array_equal(raster, clean_raster)
    reference = None
    if sys.platform == 'win32':
        from test_subtitle_render_gpu_backend import _render_g1_frames

        with NativeRendererProcess() as process:
            _, raw = _render_g1_frames(process, style, times, force_warp=False, track=track,
                                       width=640, height=360, dpr=1.5, realization_enabled=False)
        reference = [np.frombuffer(frame, np.uint8).reshape(540, 960, 4) for frame in raw]
    assert_glow_parity(times, reference, gpu, raster, 'disabled_role_outline_paints', check_p00_edges=True)


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('ruby', ['none', 'upper', 'both'])
def test_body_role_outlines_match_actual_d2d_gpu(binary, application, tmp_path, ruby):
    from scripts.subtitle_gpu_baseline.cases import build_cases
    from test_subtitle_skia_character_fade import assert_ruby_transition_gpu_parity

    base = next(case for case in build_cases(tmp_path) if case.case_id == 'viewport')
    line = base.track.lines[0]
    chars = [replace(char, role_label='edge' if i % 2 == 0 else None) for i, char in enumerate(line.chars)]
    track = replace(base.track, lines=[replace(line, chars=chars)], rubies=[] if ruby == 'none' else base.track.rubies)
    palette = role_outline_palette()
    role = SubtitleStyleScheme(font_size_px=97, stroke_width_px=5, stroke2_width_px=2,
                               stroke2_enabled=True, karaoke_colors=palette)
    style = replace(base.style, viewport_rotation_deg=0, viewport_scale_pct=100, viewport_offset_x=0,
                    stroke_width_px=0, stroke2_enabled=False, stroke2_width_px=0,
                    decoration_kind='none', karaoke_anim='none', lower_ruby_enabled=ruby == 'both',
                    custom_style_schemes={'edge': role})
    case = replace(base, case_id=f'body_role_outlines_{ruby}', track=track, style=style)
    assert_ruby_transition_gpu_parity(binary, case, (299, 300, 1500, 1750, 2600, 4400, 5499, 5500))


@pytest.fixture
def roles(mixed):
    track,style=mixed
    style=replace(style,custom_style_schemes={
        'lead':SubtitleStyleScheme(font_family=style.font_family_latin,font_family_latin=style.font_family,
            font_size_px=70,latin_font_size_px=35,latin_font_stretch_pct=125,
            letter_spacing_px=99,space_width_percent=100,allow_biting=True,
            karaoke_colors=colors('#8033AA11','#AACC1177')),
        'back':SubtitleStyleScheme(karaoke_colors=colors('#FF1122EE','#FFEEAA22'))})
    units=[('字',None),('A','lead'),('字','lead'),('한','back'),('A','missing'),('字','lead')]
    line=TimingLine(chars=[TimingChar(text,200+i*200,role_label=role) for i,(text,role) in enumerate(units)],end_ms=1800)
    return replace(track,lines=[line]),style


def test_role_layout_inheritance_preserves_active_spacing(roles):
    _,style=roles
    result=style_for_role_in_layout(style,'lead')
    assert result.font_size_px==70 and result.latin_font_size_px==35
    for key in ('letter_spacing_px','space_width_percent','allow_biting','ruby_gap_px'):
        assert getattr(result,key)==getattr(style,key)
    assert style_for_role_in_layout(style,'missing')==style


def test_role_instances_are_deduplicated_in_one_batch(binary,roles,tmp_path):
    track,style=roles
    batches=[]
    with SkiaTextMetricsProcess(binary) as process:
        def measure(fonts,runs):
            batches.append(runs)
            return process.measure(fonts,runs)
        ir=build_skia_render_ir(track,style,measure=measure,resolve_font=resolve_skia_font,width=640,height=360,fps=60)
    assert len(batches)==1 and len(batches[0])==4
    assert len(ir['skia_layout']['fonts'])==2
    cells=ir['skia_layout']['lines'][0]['cells']
    assert cells[2]['run_index']==cells[5]['run_index']
    assert cells[0]['run_index']==cells[3]['run_index']
    assert cells[1]['metrics']['advance']==pytest.approx(26.25,abs=1e-4)
    assert cells[2]['metrics']['advance']==pytest.approx(70,abs=1e-4)
    (tmp_path/'roles-layout.json').write_text(json.dumps({'cells':cells,
        'runs':[{k:v for k,v in r.items() if k!='font_ids'} for r in ir['skia_layout']['runs']],
        'intervals':ir['track']['lines'][0]['resolved_intervals']},sort_keys=True),encoding='utf-8')


def test_same_text_different_roles_do_not_collide_in_interval_snapshot(binary,roles):
    track,style=roles
    units=[TimingChar('A',200,source_span_start_ms=200,source_span_end_ms=1000,source_span_index=0,source_span_count=2),
           TimingChar('字',200,source_span_start_ms=200,source_span_end_ms=1000,source_span_index=1,source_span_count=2)]
    plain=TimingLine(chars=units,end_ms=1000)
    lead=replace(plain,chars=[replace(c,role_label='lead') for c in units])
    def capture(line):
        ir=ir_for(binary,(replace(track,lines=[line]),style))
        cells=ir['skia_layout']['lines'][0]['cells']
        from krok_helper.subtitle_render.engine.text.skia_width import cell_geometry
        from krok_helper.subtitle_render.engine.text.font_slots import font_style_for_cell
        widths=[cell_geometry(c.text,m['metrics'],font_style_for_cell(style_for_role_in_layout(style,c.role_label),c.text)[0])[0]
                for c,m in zip(line.chars,cells)]
        return LineMetric.capture(line,style,[CellMetric.from_payload(c.text,m['metrics']) for c,m in zip(line.chars,cells)],layout_widths=widths)
    a,b=capture(plain),capture(lead)
    snapshot=TextMetricSnapshot((a,b))
    assert snapshot.resolve_intervals(plain,style)!=snapshot.resolve_intervals(lead,style)
    with pytest.raises(ValueError,match='快照'):
        snapshot.resolve_intervals(replace(plain,chars=[replace(c,role_label='other') for c in units]),style)
    # Exercise the real plan as well: two identical-text lines use different role widths.
    ir=ir_for(binary,(replace(track,lines=[plain,replace(lead,chars=[replace(c,start_ms=c.start_ms+2000,
        source_span_start_ms=2200,source_span_end_ms=3000) for c in lead.chars],end_ms=3000)]),style))
    assert len(ir['skia_layout']['lines'])==2


@pytest.mark.parametrize('semantics',['legacy','n3_1074'])
def test_role_size_and_first_character_do_not_move_line_baseline(binary,roles,semantics):
    track,style=roles
    style=replace(style,layout_semantics=semantics,singer_style_overrides={1:SubtitleStyleScheme(font_size_px=65)})
    source=track.lines[0]
    line=replace(source,singer_id=1,chars=[replace(source.chars[0],role_label='lead'),*source.chars[1:]])
    decorated=ir_for(binary,(replace(track,lines=[line]),style))
    plain=ir_for(binary,(replace(track,lines=[replace(line,chars=[replace(c,role_label=None) for c in line.chars])]),style))
    for a,b in zip(decorated['skia_layout']['lines'][0]['cells'],plain['skia_layout']['lines'][0]['cells']):
        assert a['baseline_y']==b['baseline_y']


def test_n3_role_can_clear_the_global_latin_font_override(binary,roles):
    track,style=roles
    lead=replace(style.custom_style_schemes['lead'],n3_font_inheritance=True,
                 font_family_latin=None,latin_font_size_px=None)
    style=replace(style,custom_style_schemes={**style.custom_style_schemes,'lead':lead})
    ir=ir_for(binary,(track,style))
    cells=ir['skia_layout']['lines'][0]['cells']
    runs=ir['skia_layout']['runs']
    a,b=(runs[c['run_index']] for c in cells[1:3])
    assert a['font_ids']==b['font_ids'] and a['size']==b['size']==70
    assert cells[1]['metrics']['advance']==pytest.approx(70*.8*1.25,abs=1e-4)


def test_role_synthetic_instances_are_resolved_before_drawing(binary,roles):
    track,style=roles
    lead=replace(style.custom_style_schemes['lead'],font_weight=700,italic=True,latin_font_weight=700,latin_italic=True)
    ir=ir_for(binary,(track,replace(style,custom_style_schemes={**style.custom_style_schemes,'lead':lead})))
    cells=ir['skia_layout']['lines'][0]['cells']
    for cell in cells[1:3]:
        run=ir['skia_layout']['runs'][cell['run_index']]
        assert run['synthetic_bold'] and run['synthetic_italic']


@pytest.mark.parametrize('semantics',['legacy','n3_1074'])
def test_singer_and_role_inheritance_share_gpu_raster_geometry(binary,roles,semantics):
    track,style=roles
    source=replace(track,lines=[replace(track.lines[0],singer_id=1)])
    style=replace(style,layout_semantics=semantics,singer_style_overrides={1:SubtitleStyleScheme(
        font_size_px=60,karaoke_colors=colors('#80446688','#BBDD5522'))})
    cells=ir_for(binary,(source,style))['skia_layout']['lines'][0]['cells']
    output=[]
    for target in ('gpu','raster'):
        with SkiaRendererProcess(binary,selection=selection(target)) as process:
            process.configure(source,style,width=640,height=360,fps=60,dpr=1.5)
            frames,_=collect(process,[800,1900,100,800])
            assert np.array_equal(frames[0],frames[3])
            for char,cell in zip(source.lines[0].chars,cells):
                ink=cell['metrics']['ink']
                x=int((cell['origin_x']+(ink[0]+ink[2])/2)*1.5)
                y=int((cell['baseline_y']+(ink[1]+ink[3])/2)*1.5)
                appearance=style_for_role_in_layout(style_for_line(style,source.lines[0]),char.role_label)
                expected=QColor(effective_karaoke_colors(appearance).after.text.color).getRgb()
                assert np.max(np.abs(frames[1][y,x].astype(int)-expected))<=2
            pixels=np.stack(frames).astype(float);pixels[...,:3]*=pixels[...,3:]/255
            output.append(pixels)
    delta=np.abs(output[0]-output[1])
    assert delta.mean()<.15 and np.mean(np.max(delta,axis=-1)>16)<.003


@pytest.mark.parametrize('semantics',['legacy','n3_1074'])
@pytest.mark.parametrize('target',['gpu','raster'])
def test_role_paints_and_seek_are_per_cell(binary,roles,target,semantics):
    track,style=roles
    style=replace(style,layout_semantics=semantics)
    ir=ir_for(binary,(track,style))
    cells=ir['skia_layout']['lines'][0]['cells']
    with SkiaRendererProcess(binary,selection=selection(target)) as process:
        process.configure(track,style,width=640,height=360,fps=60)
        frames,_=collect(process,[100,800,1900,800])
        assert np.array_equal(frames[1],frames[3])
    for char,cell in zip(track.lines[0].chars,cells):
        ink=cell['metrics']['ink']
        x=int(cell['origin_x']+(ink[0]+ink[2])/2)
        y=int(cell['baseline_y']+(ink[1]+ink[3])/2)
        paint=effective_karaoke_colors(style_for_role_in_layout(style,char.role_label))
        for frame,state in ((frames[0],paint.before),(frames[2],paint.after)):
            expected=QColor(state.text.color).getRgb()
            assert np.max(np.abs(frame[y,x].astype(int)-expected))<=2,(char.role_label,frame[y,x],expected)


@pytest.mark.parametrize('mutation',['stroke'])
def test_unsupported_role_paint_rejected_without_replacing_scene(binary,roles,mutation):
    with SkiaRendererProcess(binary,selection=selection('raster')) as process:
        process.configure(*roles,width=640,height=360,fps=60)
        before,_=collect(process,[800])
        ir=ir_for(binary,roles)
        scheme=ir['style']['custom_style_schemes']['lead']
        if mutation=='stroke': scheme['stroke_width_px']=5
        elif mutation=='decoration': scheme['decoration_kind']='glow'
        else: scheme['karaoke_colors']['before']['text']['mode']='gradient_vertical'
        process._send({'cmd':'configure','ir':ir})
        with pytest.raises(NativeRendererError):process._expect_ok(process._read_until_event('configured'))
        after,_=collect(process,[800],generation=2)
        assert np.array_equal(before[0],after[0])


@pytest.mark.skipif(sys.platform!='win32',reason='Direct2D GPU comparison')
@pytest.mark.parametrize('semantics',['legacy','n3_1074'])
def test_role_geometry_matches_d2d_gpu(binary,roles,semantics):
    from test_subtitle_render_gpu_backend import _render_g1_frames
    track,style=roles
    # Give the D2D compatibility adapter an explicit physical app-font hint:
    # color-only schemes otherwise clear that old adapter's font_hint slot.
    back=replace(style.custom_style_schemes['back'],font_family=style.font_family,
                 font_family_latin=style.font_family_latin,font_weight=style.font_weight)
    style=replace(style,layout_semantics=semantics,custom_style_schemes={**style.custom_style_schemes,'back':back})
    with NativeRendererProcess() as d2d:
        _,raw=_render_g1_frames(d2d,style,(1900,),force_warp=False,track=track)
    with SkiaRendererProcess(binary,selection=selection('gpu')) as skia:
        skia.configure(track,style,width=640,height=360,fps=60)
        frames,_=collect(skia,[1900])
    expected=np.frombuffer(raw[0],np.uint8).reshape(360,640,4)
    def bounds(p):
        y,x=np.where(p[...,3]>127)
        return np.array([x.min(),y.min(),x.max(),y.max()])
    assert np.max(np.abs(bounds(expected)-bounds(frames[0])))<=1
