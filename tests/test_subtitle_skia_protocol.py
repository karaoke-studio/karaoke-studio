"""P04 product IR, shared-memory preview and export acceptance (opt-in native binary)."""
import copy
from dataclasses import replace
import os
import sys
from pathlib import Path
import shutil
import subprocess
import time
import uuid

import numpy as np
import pytest
from PyQt6 import sip
from PyQt6.QtCore import QCoreApplication, QEvent
from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QImage

from krok_helper.subtitle_render.domain.models import (
    GuideSymbol, LyricsLayout, Style, SubtitleStyleScheme, TimingChar, TimingLine, TimingTrack, TitleOverlay, TitleTimeWindow,
)
from krok_helper.subtitle_render.domain.paint import KaraokeColorState, KaraokeColors, PaintFill
from krok_helper.subtitle_render.native.backend import NativeRendererError, NativeRendererProcess, SharedFrameRingReader
from krok_helper.subtitle_render.native.skia_backend import SkiaRendererProcess, SkiaSelection

_APPLICATION = None


@pytest.fixture(scope="module", autouse=True)
def application():
    global _APPLICATION
    _APPLICATION = QApplication.instance() or QApplication([])
    return _APPLICATION


def sample():
    style = Style(font_family="Arial" if sys.platform in {"win32", "darwin"} else "DejaVu Sans", font_family_latin=None,
                  font_size_px=48, stroke_width_px=0, stroke2_width_px=0, decoration_kind="none",
                  dual_line_layout=False, line_horizontal_layout="center", line_alignments=["center"],
                  smart_horizontal="none", entry_anim="none", exit_anim="none", karaoke_anim="none")
    track = TimingTrack(lines=[TimingLine(chars=[TimingChar(c, 200 + i * 100) for i, c in enumerate("Skia P04")], end_ms=1200)])
    return track, style


def solid_title(style):
    return TitleOverlay(enabled=True, text_template='{artist}', layout_index=None, anchor='top_left',
                        font_family=style.font_family, font_family_latin=None, font_size_px=80, font_weight=400,
                        fill=PaintFill(color='#61BEF8'), stroke_width_px=0, stroke2_width_px=0,
                        decoration_kind='none', offset_x=100, offset_y=60,
                        custom_windows=[TitleTimeWindow(300, 1000, 200, 200), TitleTimeWindow(1400, 1800, 0, 0)])


def test_scene_prewarm_includes_title_slots_and_extra_tracks():
    from krok_helper.subtitle_render.native.skia_font_preparation import scene_fallback_requests

    track, style = sample()
    side = TimingTrack(lines=[TimingLine(chars=[TimingChar('字', 0)], end_ms=1000)])
    title = replace(solid_title(style), text_template='Z', font_family_latin='Title Latin')
    style = replace(style, title_overlays=[title], custom_style_schemes={})
    requests = scene_fallback_requests(track, style, [side])
    assert 'Z' in requests['Title Latin', 400, False]
    assert '字' in requests[style.font_family, style.font_weight, style.italic]


@pytest.mark.parametrize('target', ['gpu', 'raster'])
@pytest.mark.parametrize('decorated', [False, True], ids=['plain', 'decorated'])
def test_solid_title_windows_follow_media_clock_and_seek(binary, target, decorated):
    from test_subtitle_skia_resolved import ir_for

    track, style = sample()
    track = replace(track, meta=replace(track.meta, artist='Skia', offset_ms=5000))
    title = solid_title(style)
    if decorated:
        title = replace(title, stroke_width_px=5, decoration_kind='glow', glow_radius_px=3)
    lower = replace(title, fill=PaintFill(color='#E22B22'))
    style = replace(style, title_overlays=[title, lower], custom_style_schemes={},
                    timing_offset_ms=600, viewport_offset_y=120)
    ir = ir_for(binary, (track, style), roll_shift_ms=800)
    assert ir['titles'] == [] and 'p08_solid_title' in ir['required_features']
    invalid = copy.deepcopy(ir)
    invalid['skia_titles'][0]['windows'][0] = [400, 100, 0, 0]
    times = [299, 300, 400, 500, 900, 1000, 1400, 1800, 1801, 500]
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        process._send({'cmd': 'configure', 'ir': ir})
        process._expect_ok(process._read_until_event('configured'))
        frames, _ = collect(process, times)
        process._send({'cmd': 'configure', 'ir': invalid})
        with pytest.raises(NativeRendererError):
            process._expect_ok(process._read_until_event('configured'))
        replay, _ = collect(process, [500], generation=2)
    assert all(not frames[i].any() for i in (0, 1, 5, 8))
    faded, opaque = frames[2][..., 3], frames[3][..., 3]
    assert 0 < faded.sum() < opaque.sum() and np.array_equal(frames[2], frames[4])
    if not decorated:
        assert faded.max() in (191, 192)
    assert np.array_equal(frames[3], frames[6]) and np.array_equal(frames[6], frames[7])
    assert np.array_equal(frames[3], frames[-1]) and np.array_equal(frames[3], replay[0])
    y, x = np.where(frames[3][..., 3] == 255)
    assert y.size and y.max() < 180 and x.min() >= 100
    pixels = frames[3][y, x, :3]
    assert (pixels[:, 2] > 200).sum() > 100


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('decoration', ['none', 'glow', 'shadow'])
def test_solid_title_matches_actual_d2d_gpu(binary, decoration):
    from types import SimpleNamespace
    from test_subtitle_skia_character_fade import assert_ruby_transition_gpu_parity

    track, style = sample()
    track = replace(track, meta=replace(track.meta, artist='Skia', offset_ms=5000))
    title = solid_title(style)
    if decoration != 'none':
        title = replace(title, decoration_kind=decoration, glow_radius_px=3, stroke_width_px=5,
                        stroke2_width_px=2, shadow=PaintFill(color='#F99776'))
    style = replace(style, title_overlays=[title], custom_style_schemes={},
                    timing_offset_ms=600, viewport_offset_y=120)
    assert_ruby_transition_gpu_parity(
        binary, SimpleNamespace(track=track, style=style, case_id=f'solid_title_{decoration}'),
        (299, 300, 400, 500, 900, 1000, 1400, 1800, 1801))


@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_multiline_title_keeps_blank_row_and_per_row_alignment(binary, target):
    from test_subtitle_skia_resolved import ir_for

    track, style = sample()
    title = replace(solid_title(style), text_template='Skia\n\nGPU', layout_index=1, font_size_px=45)
    layout = LyricsLayout(layout_id='title', line_y_position='top', line_y_margin_px=35, line_gap_px=18,
                          line_alignments=['left', 'center', 'right'], line_offsets_x=[13, 0, -19])
    style = replace(style, title_overlays=[title], layouts=[layout], custom_style_schemes={})
    track = replace(track, meta=replace(track.meta, offset_ms=5000))
    ir = ir_for(binary, (track, style))
    rows = ir['skia_titles'][0]['source']['skia_layout']['lines']
    assert len(rows) == 2
    first, second = (row['cells'][0] for row in rows)
    assert second['baseline_y'] - first['baseline_y'] == pytest.approx(126)
    assert first['origin_x'] < 100 and second['origin_x'] > 440
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        process.configure(track, style, width=640, height=360, fps=60)
        frames, _ = collect(process, [400, 500, 1000, 500])
    assert frames[1][:120, :200, 3].any() and frames[1][140:260, 440:, 3].any()
    assert not frames[1][100:140, :, 3].any()
    assert not frames[2].any() and np.array_equal(frames[1], frames[-1])


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('anchor', ['top', 'center', 'bottom'])
def test_multiline_title_matches_actual_d2d_gpu(binary, anchor):
    from types import SimpleNamespace
    from test_subtitle_skia_character_fade import assert_ruby_transition_gpu_parity

    track, style = sample()
    track = replace(track, meta=replace(track.meta, offset_ms=5000))
    title = replace(solid_title(style), text_template='Skia\n\nGPU\nRenderer', layout_index=1, stroke_width_px=5)
    layout = LyricsLayout(layout_id='title', line_y_position=anchor, line_y_margin_px=55, line_gap_px=19,
                          line_alignments=['left', 'center', 'center', 'right'], line_offsets_x=[7, 0, 19, -13])
    style = replace(style, title_overlays=[title], layouts=[layout], custom_style_schemes={})
    assert_ruby_transition_gpu_parity(
        binary, SimpleNamespace(track=track, style=style, case_id=f'multiline_title_{anchor}'),
        (299, 400, 500, 900, 1000, 1400, 1800, 1801))


def role_title(outlined=False):
    track, style = sample()
    track = replace(track, meta=replace(track.meta, offset_ms=5000))
    title = replace(solid_title(style), text_template='Skia\nGPU', anchor='center',
                    char_role_labels=[[None, 'accent', None, 'accent'], ['accent']])
    accent = KaraokeColorState(text=PaintFill(color='#F78E32'), stroke=PaintFill(color='#AD146C'),
                               stroke2=PaintFill(color='#43B223'))
    scheme = SubtitleStyleScheme(font_size_px=102, latin_font_size_px=104, font_weight=700,
                                 latin_font_weight=400, decoration_kind='none',
                                 stroke_width_px=5 if outlined else 0, stroke2_enabled=outlined,
                                 stroke2_width_px=4 if outlined else 0,
                                 karaoke_colors=KaraokeColors(before=accent, after=accent))
    return track, replace(style, title_overlays=[title], custom_style_schemes={'accent': scheme})


@pytest.mark.parametrize('target', ['gpu', 'raster'])
@pytest.mark.parametrize('outlined', [False, True], ids=['plain', 'outlined'])
def test_title_roles_preserve_fonts_and_static_palette(binary, target, outlined):
    from test_subtitle_skia_resolved import ir_for

    track, style = role_title(outlined)
    ir = ir_for(binary, (track, style))
    layout = ir['skia_titles'][0]['source']['skia_layout']
    runs = layout['runs']
    first, second = [[runs[c['run_index']]['size'] for c in row['cells']] for row in layout['lines']]
    assert first == [80, 104, 80, 104] and second == [104, 104, 104]
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        process.configure(track, style, width=640, height=360, fps=60)
        frames, _ = collect(process, [500, 1400, 1000, 500])
    assert not frames[2].any() and np.array_equal(frames[0], frames[1]) and np.array_equal(frames[0], frames[-1])
    pixels = frames[0][frames[0][..., 3] == 255, :3]
    assert np.any(np.all(pixels == [247, 142, 50], axis=-1))
    assert np.any(np.all(pixels == [97, 190, 248], axis=-1))
    assert np.any(np.all(pixels == [173, 20, 108], axis=-1)) == outlined
    assert np.any(np.all(pixels == [67, 178, 35], axis=-1)) == outlined


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('outlined', [False, True], ids=['plain', 'outlined'])
def test_title_roles_match_actual_d2d_gpu(binary, outlined):
    from types import SimpleNamespace
    from test_subtitle_skia_character_fade import assert_ruby_transition_gpu_parity

    track, style = role_title(outlined)
    assert_ruby_transition_gpu_parity(
        binary, SimpleNamespace(track=track, style=style, case_id=f'title_roles_{outlined}'),
        (299, 400, 500, 900, 1000, 1400, 1800, 1801))


def painted_title(tmp_path, mode):
    from test_subtitle_skia_image_fills import pattern, write_png

    track, style = sample()
    track = replace(track, meta=replace(track.meta, offset_ms=5000))
    if mode == 'image':
        texture = write_png(tmp_path / 'title-texture.png', pattern('transparent'))
        fill = PaintFill(mode='image', image_path=str(texture), color='#483CE1', image_scale_pct=135)
    else:
        fill = PaintFill(mode=mode, start_color='#F83408', end_color='#08A3F8')
    title = replace(solid_title(style), text_template='Skia\nRenderer', anchor='center', fill=fill)
    return track, replace(style, title_overlays=[title], custom_style_schemes={})


@pytest.mark.parametrize('target', ['gpu', 'raster'])
@pytest.mark.parametrize('mode', ['gradient_horizontal', 'image'])
def test_title_paints_keep_each_row_domain_and_replay(binary, tmp_path, target, mode):
    from test_subtitle_skia_resolved import ir_for

    track, style = painted_title(tmp_path, mode)
    ir = ir_for(binary, (track, style))
    source = ir['skia_titles'][0]['source']
    assert ('p06_image_fill' if mode == 'image' else 'p06_body_gradient') in source['required_features']
    if mode != 'image':
        first, second = [row['body_gradient_bounds'] for row in source['skia_layout']['lines']]
        assert first[2] - first[0] < second[2] - second[0] and first[3] < second[1]
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        process.configure(track, style, width=640, height=360, fps=60)
        frames, _ = collect(process, [400, 500, 1400, 1000, 500])
    assert 0 < frames[0][..., 3].sum() < frames[1][..., 3].sum()
    assert not frames[3].any() and np.array_equal(frames[1], frames[2]) and np.array_equal(frames[1], frames[4])


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('mode', ['gradient_horizontal', 'gradient_vertical', 'image'])
def test_title_paints_match_actual_d2d_gpu(binary, tmp_path, mode):
    from types import SimpleNamespace
    from test_subtitle_skia_character_fade import assert_ruby_transition_gpu_parity

    track, style = painted_title(tmp_path, mode)
    assert_ruby_transition_gpu_parity(
        binary, SimpleNamespace(track=track, style=style, case_id=f'title_paint_{mode}'),
        (299, 400, 500, 900, 1000, 1400, 1800, 1801))


def painted_role_title(tmp_path):
    from test_subtitle_skia_image_fills import pattern, write_png

    track, style = role_title(outlined=True)
    title = style.title_overlays[0]
    texture = write_png(tmp_path / 'title-role-texture.png', pattern('transparent'))
    gradient = PaintFill(mode='gradient_horizontal', start_color='#F83408', end_color='#08A3F8')
    image = PaintFill(mode='image', image_path=str(texture), color='#483CE1', image_scale_pct=135)
    accent = style.custom_style_schemes['accent']
    paint = accent.karaoke_colors.before
    colors = KaraokeColors(before=replace(paint, text=gradient), after=replace(paint, text=gradient))
    image_colors = KaraokeColors(before=replace(paint, text=image), after=replace(paint, text=image))
    schemes = {'accent': replace(accent, karaoke_colors=colors),
               'image': replace(accent, font_size_px=96, latin_font_size_px=96, karaoke_colors=image_colors)}
    return track, replace(style, title_overlays=[replace(title, char_role_labels=[
        [None, 'accent', 'image', 'accent'], ['image']
    ])], custom_style_schemes=schemes)


@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_title_role_paints_use_independent_layers(binary, tmp_path, target):
    from test_subtitle_skia_resolved import ir_for

    track, style = painted_role_title(tmp_path)
    ir = ir_for(binary, (track, style))
    assert {'p06_role_body_paints', 'p08_title_role_outlines'} <= set(ir['skia_titles'][0]['source']['required_features'])
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        process.configure(track, style, width=640, height=360, fps=60)
        frames, _ = collect(process, [400, 500, 1800, 500])
    assert 0 < frames[0][..., 3].sum() < frames[1][..., 3].sum()
    assert np.array_equal(frames[1], frames[2]) and np.array_equal(frames[1], frames[3])
    colors = frames[1][frames[1][..., 3] == 255, :3]
    assert np.unique(colors, axis=0).shape[0] > 60


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
def test_title_role_paints_match_actual_d2d_gpu(binary, tmp_path):
    from types import SimpleNamespace
    from test_subtitle_skia_character_fade import assert_ruby_transition_gpu_parity

    track, style = painted_role_title(tmp_path)
    assert_ruby_transition_gpu_parity(
        binary, SimpleNamespace(track=track, style=style, case_id='title_role_paints'),
        (299, 400, 500, 900, 1000, 1400, 1800, 1801))


def vector_guide_title():
    track, style = sample()
    track = replace(track, meta=replace(track.meta, offset_ms=5000))
    triangle = GuideSymbol(path_commands=(('M', 10, -110), ('L', 490, -750), ('L', 770, -110), ('Z',)),
                           advance_width=900, count=2, duration_ms=500)
    title = replace(solid_title(style), text_template='Skia', anchor='top_right',
                    guide_symbols={0: triangle}, inline_guide_symbols={(0, 2): triangle})
    return track, replace(style, title_overlays=[title], custom_style_schemes={})


@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_title_vector_guides_use_prefix_count_and_fixed_frame(binary, target):
    from test_subtitle_skia_resolved import ir_for

    track, style = vector_guide_title()
    source = ir_for(binary, (track, style))['skia_titles'][0]['source']
    assert 'p05_vector_guides' in source['required_features']
    line = source['skia_layout']['lines'][0]['cells']
    assert [index for index, cell in enumerate(line) if cell['metrics'].get('vector_glyph_id')] == [0, 1, 4]
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        process.configure(track, style, width=640, height=360, fps=60)
        frames, _ = collect(process, [500, 1600, 1400, 1000, 500])
    assert frames[0].any() and not frames[3].any()
    assert all(np.array_equal(frames[0], frames[i]) for i in (1, 2, 4))


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
def test_title_vector_guides_match_actual_d2d_gpu(binary):
    from types import SimpleNamespace
    from test_subtitle_skia_character_fade import assert_ruby_transition_gpu_parity

    track, style = vector_guide_title()
    assert_ruby_transition_gpu_parity(
        binary, SimpleNamespace(track=track, style=style, case_id='title_vector_guides'),
        (299, 400, 500, 900, 1000, 1400, 1800, 1801))


def bitmap_guide_title(tmp_path, inline=False):
    from test_subtitle_skia_image_fills import write_png

    track, style = sample()
    track = replace(track, meta=replace(track.meta, offset_ms=5000))
    pixels = np.zeros((27, 35, 4), dtype=np.uint8)
    pixels[3:-3, 3:-3] = [225, 75, 31, 255]
    before = write_png(tmp_path / 'title-before.png', pixels)
    pixels[3:-3, 3:-3] = [56, 245, 85, 255]
    after = write_png(tmp_path / 'title-after.png', pixels)
    guide = GuideSymbol(kind='bitmap', bitmap_before_path=str(before), bitmap_after_path=str(after),
                        count=2, bitmap_fix_size=True, bitmap_margin_left_px=4,
                        bitmap_margin_right_px=7, bitmap_margin_bottom_px=5)
    title = replace(solid_title(style), text_template='Skia',
                    guide_symbols={0: replace(guide, count=1)} if inline else {0: guide},
                    inline_guide_symbols={(0, 2): guide} if inline else {})
    return track, replace(style, title_overlays=[title], custom_style_schemes={})


@pytest.mark.parametrize('target', ['gpu', 'raster'])
@pytest.mark.parametrize('inline', [False, True], ids=['prefix', 'inline'])
def test_title_bitmap_guides_use_before_image_at_every_time(binary, tmp_path, target, inline):
    track, style = bitmap_guide_title(tmp_path, inline)
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        process.configure(track, style, width=640, height=360, fps=60)
        frames, _ = collect(process, [400, 500, 1800, 500, 1000])
        if inline:
            from test_subtitle_skia_resolved import ir_for

            title = ir_for(binary, (track, style))['skia_titles'][0]['source']
            process._send({'cmd': 'configure', 'ir': title})
            with pytest.raises(NativeRendererError, match='整行前置'):
                process._expect_ok(process._read_until_event('configured'))
            replay, _ = collect(process, [500], generation=2)
            assert np.array_equal(replay[0], frames[1])
    assert not frames[4].any()
    assert np.array_equal(frames[1], frames[2]) and np.array_equal(frames[1], frames[3])
    colors = frames[1][frames[1][..., 3] == 255, :3]
    assert np.count_nonzero(np.all(colors == [225, 75, 31], axis=1)) > 900
    assert not np.any(np.all(colors == [56, 245, 85], axis=1))
    assert frames[0][..., 3].sum() < frames[1][..., 3].sum()
    if inline:
        orange = np.all(frames[1][..., :3] == [225, 75, 31], axis=-1).any(axis=0)
        blue = np.all(frames[1][..., :3] == [97, 190, 248], axis=-1).any(axis=0)
        assert np.any(blue[np.flatnonzero(orange).min():np.flatnonzero(orange).max()])


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('inline', [False, True], ids=['prefix', 'inline'])
def test_title_bitmap_guides_match_actual_d2d_gpu(binary, tmp_path, inline):
    from types import SimpleNamespace
    from test_subtitle_skia_character_fade import assert_ruby_transition_gpu_parity

    track, style = bitmap_guide_title(tmp_path, inline)
    assert_ruby_transition_gpu_parity(
        binary, SimpleNamespace(track=track, style=style, case_id=f'title_bitmap_guides_{inline}'),
        (299, 400, 500, 900, 1000, 1400, 1800, 1801))


def mixed_guide_title(tmp_path):
    track, style = bitmap_guide_title(tmp_path)
    _, vector = vector_guide_title()
    title = replace(style.title_overlays[0], inline_guide_symbols=vector.title_overlays[0].inline_guide_symbols)
    return track, replace(style, title_overlays=[title])


def animated_guide_title(tmp_path, long_loop=False):
    from test_subtitle_skia_line_motion import animated_alpha_band

    track, style = bitmap_guide_title(tmp_path, inline=True)
    title = style.title_overlays[0]
    before = animated_alpha_band(tmp_path)
    if long_loop:
        path = Path(before)
        gif = path.read_bytes()
        control = bytes.fromhex('21 F9 04')
        first = gif.index(control)
        second = gif.index(control, first + len(control))
        path.write_bytes(gif[:first] + gif[first:-1] * 30 + gif[first:second] + gif[-1:])
        title = replace(title, custom_windows=[TitleTimeWindow(300, 9000, 0, 0)])
    def animate(guide):
        return replace(guide, bitmap_before_path=before, bitmap_fix_size=False, bitmap_zoom_percent=65)
    title = replace(title, guide_symbols={0: animate(title.guide_symbols[0])},
                    inline_guide_symbols={(0, 2): animate(title.inline_guide_symbols[0, 2])})
    return track, replace(style, title_overlays=[title], timing_offset_ms=200)


@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_animated_title_guides_follow_media_time_and_seek(binary, tmp_path, target):
    from test_subtitle_skia_resolved import ir_for

    track, style = animated_guide_title(tmp_path)
    ir = ir_for(binary, (track, style), roll_shift_ms=340)
    source = ir['skia_titles'][0]['source']
    assert 'p08_animated_title_guides' in source['required_features']
    forged = copy.deepcopy(ir)
    forged['skia_titles'][0]['source']['required_features'].remove('p08_animated_title_guides')
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        process._send({'cmd': 'configure', 'ir': ir})
        process._expect_ok(process._read_until_event('configured'))
        frames, _ = collect(process, [499, 500, 599, 600, 750, 1000, 1500, 1600, 500])
        process._send({'cmd': 'configure', 'ir': forged})
        with pytest.raises(NativeRendererError, match='动图'):
            process._expect_ok(process._read_until_event('configured'))
        replay, _ = collect(process, [600], generation=2)
    assert all(np.array_equal(frames[1], frames[i]) for i in (2, 4, 6, 8))
    assert np.array_equal(frames[3], frames[7]) and np.array_equal(frames[3], replay[0])
    assert not frames[5].any()
    assert np.count_nonzero(frames[1][..., 3] != frames[3][..., 3]) > 100


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('long_loop', [False, True], ids=['short', 'over_60_frames'])
def test_animated_title_guides_match_actual_d2d_gpu(binary, tmp_path, long_loop):
    from types import SimpleNamespace
    from test_subtitle_skia_character_fade import assert_ruby_transition_gpu_parity

    track, style = animated_guide_title(tmp_path, long_loop)
    times = ((7499, 7500, 7599, 7600, 7749, 7750, 7600) if long_loop
             else (299, 349, 399, 400, 499, 500, 599, 600, 750, 900, 1000, 1500, 1600))
    assert_ruby_transition_gpu_parity(
        binary, SimpleNamespace(track=track, style=style, case_id=f'animated_title_guides_{long_loop}'), times)


@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_mixed_title_guides_share_a_static_layout(binary, tmp_path, target):
    from test_subtitle_skia_resolved import ir_for

    track, style = mixed_guide_title(tmp_path)
    source = ir_for(binary, (track, style))['skia_titles'][0]['source']
    assert {'p05_vector_guides', 'p08_bitmap_guides'} <= set(source['required_features'])
    cells = source['skia_layout']['lines'][0]['cells']
    assert sum(bool(cell['metrics'].get('vector_glyph_id')) for cell in cells) == 1
    assert sum(bool(cell['metrics'].get('bitmap_guide')) for cell in cells) == 2
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        process.configure(track, style, width=640, height=360, fps=60)
        frames, _ = collect(process, [500, 1500, 1000])
    assert frames[0].any() and np.array_equal(frames[0], frames[1]) and not frames[2].any()


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
def test_mixed_title_guides_match_actual_d2d_gpu(binary, tmp_path):
    from types import SimpleNamespace
    from test_subtitle_skia_character_fade import assert_ruby_transition_gpu_parity

    track, style = mixed_guide_title(tmp_path)
    assert_ruby_transition_gpu_parity(
        binary, SimpleNamespace(track=track, style=style, case_id='mixed_title_guides'),
        (299, 400, 500, 900, 1000, 1400, 1800, 1801))


@pytest.fixture
def supported_project(tmp_path):
    from krok_helper.subtitle_render.domain.models import style_to_dict
    from krok_helper.subtitle_render.project.controller import SubtitleProjectController
    from krok_helper.subtitle_render.project.load import ProjectLoadPlan, apply_track_project_data
    from krok_helper.subtitle_render.project.store import project_payload, save_render_project
    from krok_helper.subtitle_render.sources.loader import SubtitleSourceLoader
    lrc = tmp_path / "P04.lrc"
    lrc.write_text("".join(f"[00:00:{20 + i * 10:02}]" + c for i, c in enumerate("Skia P04")) + "[00:01:20]\n", encoding="utf-8")
    path = tmp_path / "P04.yurika"
    payload = project_payload(subtitle_path=lrc, video_path=None, audio_path=None,
                              style=style_to_dict(sample()[1]), screen={"width": 640, "height": 360, "fps": 60},
                              selected_scheme_key="", output={}, background={"kind": "solid", "color": "#183048"})
    save_render_project(path, payload)
    loaded = SubtitleProjectController.open(path)
    assert not loaded.missing_resources
    plan = ProjectLoadPlan.from_data(loaded.data)
    track = SubtitleSourceLoader.load_file(plan.subtitle_path)
    apply_track_project_data(track, plan.style, plan.source_data)
    return track, plan.style


@pytest.fixture
def binary(monkeypatch):
    path = os.environ.get("KROK_TEST_SKIA_SIDECAR")
    if not path:
        pytest.skip("Set KROK_TEST_SKIA_SIDECAR to the P04 executable")
    assert Path(path).is_file()
    monkeypatch.setenv("KROK_SUBTITLE_SKIA_RENDERER", path)
    return path


def selection(target="gpu", fallback="none"):
    return SkiaSelection(target, fallback, os.environ.get("KROK_SUBTITLE_SKIA_REQUIRE_HARDWARE", "1") != "0")


def collect(process, times, generation=1, pixel_format="rgba8888"):
    process._send({"cmd": "render_range", "t_ms": times, "generation": generation, "threads": 1,
                   "ring_slots": len(times), "shm_key": "krok-p04-test-" + uuid.uuid4().hex,
                   "pixel_format": pixel_format})
    process._expect_ok(process._read_until_event("range_started"))
    frames = []
    events = []
    while True:
        event = process.read_event()
        assert event["ok"], event
        if event.get("generation") != generation:
            continue
        if event["event"] == "frame_ready":
            with SharedFrameRingReader.from_event(event) as reader:
                slot = reader.read_frame(event)
                frames.append(np.frombuffer(slot.payload, np.uint8).reshape(slot.height, slot.stride // 4, 4)[:, :slot.width].copy())
                stale = dict(event, generation=generation + 1)
                with pytest.raises(NativeRendererError):
                    reader.read_frame(stale)
            events.append(event)
        elif event["event"] == "range_done":
            return frames, events


def test_selection_gpu_off_means_skia_raster(monkeypatch):
    monkeypatch.setenv("KROK_SUBTITLE_SKIA_TARGET", "gpu")
    assert SkiaSelection.from_environment(gpu_enabled=False).target == "raster"
    with pytest.raises(ValueError):
        SkiaSelection("qpainter")


def test_old_sidecar_is_rejected_before_selection_or_configure(monkeypatch, tmp_path):
    exe = tmp_path / "old-sidecar"
    exe.touch()
    monkeypatch.setattr(NativeRendererProcess, "start", lambda self: {"ok": True, "event": "ready", "schema": 3})
    sent = []
    monkeypatch.setattr(SkiaRendererProcess, "_send", lambda self, payload: sent.append(payload))
    monkeypatch.setattr(SkiaRendererProcess, "abort", lambda self, **kwargs: None)
    with pytest.raises(NativeRendererError, match="握手"):
        SkiaRendererProcess(exe).start()
    assert not sent


@pytest.mark.parametrize("target", ["gpu", "raster"])
def test_product_ir_ring_wipe_seek_and_transparency(binary, target):
    track, style = sample()
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        assert process.skia_info["execution_target"] == target
        configured = process.configure(track, style, width=641, height=360, fps=60, duration_ms=1600)
        assert configured["backend_info"]["profile"] == "p05_resolved_horizontal"
        frames, events = collect(process, [100, 550, 1250, 550])
        assert all(f.shape == (360, 641, 4) for f in frames)
        assert np.array_equal(frames[1], frames[3]), "random seek must be deterministic"
        assert not np.array_equal(frames[0], frames[1])
        assert not np.array_equal(frames[1], frames[2])
        assert frames[1][:50, :, 3].max() == 0
        assert frames[1][..., 3].max() > 200
        assert all(e["origin"] == "top_left" and e["alpha_type"] == "straight" and e["stride"] == 641 * 4 for e in events)


def test_same_ir_gpu_and_skia_raster(binary):
    track, style = sample()
    output = []
    for target in ("gpu", "raster"):
        with SkiaRendererProcess(binary, selection=selection(target)) as process:
            process.configure(track, style, width=640, height=360, fps=60, duration_ms=1600)
            frames, _ = collect(process, [550, 1250])
            output.append(np.stack(frames).astype(float))
    # Compare premultiplied colors: RGB under alpha=0 is not visible evidence.
    for pixels in output:
        pixels[..., :3] *= pixels[..., 3:] / 255
    delta = np.abs(output[0] - output[1])
    assert delta.mean() < 0.15
    assert np.mean(np.max(delta, axis=-1) > 16) < 0.003


@pytest.mark.parametrize("target", ["gpu", "raster"])
def test_bgra_premultiplied_and_rgba_straight_agree(binary, target):
    from krok_helper.subtitle_render.engine.render.render_ir import build_render_ir
    from krok_helper.subtitle_render.native.protocol import apply_resolved_font_faces
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        process.configure(*sample(), width=641, height=360, fps=60, duration_ms=1600)
        ir = build_render_ir(*sample(), width=641, height=360, fps=60, duration_ms=1600)
        apply_resolved_font_faces(ir)
        ir["style"]["base_color"] = "#80FF8040"
        ir["style"]["fill_color"] = "#400080FF"
        process._send({"cmd": "configure", "ir": ir})
        process._expect_ok(process._read_until_event("configured"))
        rgba, _ = collect(process, [550])
        bgra, events = collect(process, [550], generation=2, pixel_format="bgra8888_premultiplied")
        expected = rgba[0][..., [2, 1, 0, 3]].astype(np.uint16)
        expected[..., :3] = (expected[..., :3] * expected[..., 3:] + 127) // 255
        assert np.array_equal(bgra[0], expected.astype(np.uint8))
        assert 0 < int(rgba[0][..., 3].max()) <= 128
        assert events[0]["alpha_type"] == "premultiplied"


def test_missing_gpu_loader_raster_fallback_is_explicit(binary, monkeypatch, tmp_path):
    if os.sys.platform == "darwin":
        pytest.skip("Vulkan loader failure injection")
    monkeypatch.setenv("KROK_SKIA_VULKAN_LIBRARY", str(tmp_path / "missing-vulkan"))
    with pytest.raises(NativeRendererError, match="Vulkan loader"):
        with SkiaRendererProcess(binary, selection=selection("gpu", "none")):
            pass
    with SkiaRendererProcess(binary, selection=selection("gpu", "raster")) as process:
        assert process.skia_info["execution_target"] == "raster"
        assert not process.skia_info["hardware"]
        assert "Vulkan loader" in process.skia_info["fallback_reason"]
        process.configure(*sample(), width=640, height=360, fps=60, duration_ms=1600)
        frames, _ = collect(process, [550])
        assert frames[0][..., 3].max() > 0


def test_cpu_only_sidecar_rejects_gpu_and_reports_raster_fallback(binary, monkeypatch, tmp_path):
    directory = Path(binary).parent
    cpu = directory.parent / (directory.name.rsplit("-", 1)[0] + "-raster") / Path(binary).name
    if not cpu.is_file():
        pytest.skip("Build --cpu-only --qt-root to verify the CPU-only executable")
    monkeypatch.setenv("KROK_SKIA_VULKAN_LIBRARY", str(tmp_path / "absent"))
    with SkiaRendererProcess(cpu, selection=selection("raster")) as process:
        assert process.skia_info["execution_target"] == "raster"
        process.configure(*sample(), width=640, height=360, fps=60, duration_ms=1600)
        frames, _ = collect(process, [550])
        assert frames[0][..., 3].max() > 0
    with pytest.raises(NativeRendererError, match="GPU"):
        with SkiaRendererProcess(cpu, selection=selection("gpu")):
            pass
    with SkiaRendererProcess(cpu, selection=selection("gpu", "raster")) as process:
        assert process.skia_info["execution_target"] == "raster"
        assert process.skia_info["fallback_reason"]


@pytest.mark.parametrize("target", ["gpu", "raster"])
def test_full_hd_shared_frame_does_not_depend_on_legacy_sysv_size_limit(binary, target):
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        process.configure(*sample(), width=1920, height=1080, fps=60, duration_ms=1600)
        frames, events = collect(process, [550])
        assert frames[0].shape == (1080, 1920, 4)
        assert frames[0][..., 3].max() > 0
        if os.name != "nt":
            assert events[0]["shm_key"].startswith("posix:")


def test_unicode_font_resource_path(binary, tmp_path):
    from krok_helper.subtitle_render.engine.render.render_ir import build_render_ir
    from krok_helper.subtitle_render.native.protocol import apply_resolved_font_faces
    with SkiaRendererProcess(binary, selection=selection("raster")) as process:
        process.configure(*sample(), width=640, height=360, fps=60, duration_ms=1600)
        ir = build_render_ir(*sample(), width=640, height=360, fps=60, duration_ms=1600)
        apply_resolved_font_faces(ir)
        font = tmp_path / "字幕-font-🎵.otf"
        shutil.copy2(ir["style"]["font_resolved"]["resource_path"], font)
        for slot in ("font_resolved", "latin_font_resolved"):
            ir["style"][slot]["resource_path"] = str(font)
        process._send({"cmd": "configure", "ir": ir})
        process._expect_ok(process._read_until_event("configured"))
        frames, _ = collect(process, [550])
        assert frames[0][..., 3].max() > 0


@pytest.mark.parametrize("mutation", ["schema", "required", "dimensions", "effect", "text", "font", "missing_guide", "bitmap_guide"])
def test_unsupported_ir_fails_without_replacing_live_scene(binary, mutation):
    from krok_helper.subtitle_render.engine.render.render_ir import build_render_ir
    from krok_helper.subtitle_render.native.protocol import apply_resolved_font_faces
    track, style = sample()
    with SkiaRendererProcess(binary, selection=selection("raster")) as process:
        process.configure(track, style, width=640, height=360, fps=60, duration_ms=1600)
        before, _ = collect(process, [550])
        ir = build_render_ir(track, style, width=640, height=360, fps=60, duration_ms=1600)
        apply_resolved_font_faces(ir)
        ir = copy.deepcopy(ir)
        if mutation == "schema": ir["schema"] = 999
        elif mutation == "required": ir["required_features"] = ["future-shaping"]
        elif mutation == "dimensions": ir["screen"]["width"] = "640"
        elif mutation == "effect": ir["style"]["stroke_width_px"] = 7
        elif mutation == "text": ir["track"]["lines"][0]["chars"][0]["text"] = "字"
        elif mutation == "font": ir["style"]["font_resolved"]["resource_path"] = "missing.ttf"
        elif mutation == "missing_guide": ir["track"]["lines"][0]["chars"][0]["vector_glyph_id"] = "unretained-glyph"
        elif mutation == "bitmap_guide":
            ir["track"]["lines"][0]["chars"][0]["bitmap_guide"] = {
                "before_path": "avatar.png", "no_decor": True,
                "margin_right_px": -999, "margin_bottom_px": -80,
            }
        process._send({"cmd": "configure", "ir": ir})
        with pytest.raises(NativeRendererError):
            process._expect_ok(process._read_until_event("configured"))
        after, _ = collect(process, [550], generation=2)
        assert np.array_equal(before[0], after[0])


def test_cancel_and_reconfigure_remain_usable(binary):
    with SkiaRendererProcess(binary, selection=selection()) as process:
        process.configure(*sample(), width=640, height=360, fps=60, duration_ms=1600)
        process.start_render_range(list(range(0, 1200, 10)), generation=1, threads=1,
                                   shm_key="krok-p04-cancel-" + uuid.uuid4().hex, ring_slots=120)
        process.send_cancel_generation(1)
        cancelled = False
        while True:
            event = process.read_event()
            assert event["ok"], event
            if event["event"] == "generation_cancelled": cancelled = True
            elif cancelled and event["event"] == "frame_ready": pytest.fail("frame published after cancel acknowledgement")
            elif event["event"] == "range_done": break
        assert cancelled
        process.configure(*sample(), width=320, height=180, fps=60, duration_ms=1600)
        frames, _ = collect(process, [550], generation=2)
        assert frames[0].shape == (180, 320, 4)


@pytest.mark.parametrize("gpu,missing_loader", [(True, False), (False, False), (True, True)])
def test_actual_g5_preview_uses_skia_even_when_gpu_off(binary, monkeypatch, tmp_path, supported_project, gpu, missing_loader,
                                                     *, side_track=None):
    from krok_helper.subtitle_render.frontend.preview import preview_graphics as pg
    from krok_helper.subtitle_render.frontend.preview.skia_async import SkiaAsyncSubtitleRenderer
    monkeypatch.setenv("KROK_SUBTITLE_RENDER_BACKEND", "skia")
    monkeypatch.setenv("KROK_SUBTITLE_GPU_PREVIEW", "1" if gpu else "0")
    if missing_loader:
        if sys.platform == "darwin": pytest.skip("Vulkan loader failure injection")
        monkeypatch.setenv("KROK_SKIA_VULKAN_LIBRARY", str(tmp_path / "missing"))
    monkeypatch.setattr(pg, "paint_frame_to_painter", lambda *a, **k: pytest.fail("subtitle QPainter was called"))
    canvas = pg.PreviewGraphicsView()
    frames, errors = [], []
    try:
        assert isinstance(canvas._async_renderer, SkiaAsyncSubtitleRenderer)
        assert canvas._async_renderer.selection.target == ("gpu" if gpu else "raster")
        canvas._async_renderer.frame_ready.connect(lambda image, t: frames.append((image, t)))
        canvas.gpuFallback.connect(errors.append)
        canvas.resize(640, 360)
        canvas.show()
        canvas.set_output_size(640, 360)
        canvas.set_style(supported_project[1])
        if side_track is not None:
            canvas.set_extra_tracks([side_track])
        # Installing a track queues work immediately. Give the fixture its
        # explicit font first, rather than briefly rendering the user's style.
        canvas.set_track(supported_project[0])
        canvas.set_time(550)
        deadline = time.monotonic() + 30
        while (not frames or frames[-1][1] != 550 or canvas._subtitle_item._async_image is None) and not errors and time.monotonic() < deadline:
            QApplication.processEvents()
            time.sleep(0.01)
        assert not errors, errors
        assert frames and frames[-1][1] == 550 and not frames[-1][0].isNull()
        assert canvas._subtitle_item._async_image is not None
        if side_track is not None:
            image = frames[-1][0].convertToFormat(QImage.Format.Format_RGBA8888)
            pixels = np.frombuffer(image.constBits().asstring(image.sizeInBytes()), dtype=np.uint8)
            rows = pixels.reshape(image.height(), image.bytesPerLine())[:, :image.width() * 4]
            top, bottom = np.split(rows.reshape(image.height(), image.width(), 4)[..., 3], [image.height() // 2])
            assert top.max() > 200 and bottom.max() > 200
        assert "Skia" in canvas.render_backend_label()
        first = frames[-1][0]
        first_bytes = first.constBits().asstring(first.sizeInBytes())
        canvas.set_time(850)
        deadline = time.monotonic() + 5
        while frames[-1][1] != 850 and not errors and time.monotonic() < deadline:
            QApplication.processEvents()
            time.sleep(0.01)
        assert not errors, errors
        second, second_time = frames[-1]
        assert second_time == 850 and not second.isNull()
        assert second.constBits().asstring(second.sizeInBytes()) != first_bytes
        assert first.constBits().asstring(first.sizeInBytes()) == first_bytes
        canvas.set_time(550)
        retired = canvas._async_renderer
        assert retired.current_backend_mode == ("gpu" if gpu and not missing_loader else "cpu")
        stale = QImage(2, 2, QImage.Format.Format_RGBA8888)
        stale.fill(0)
        stale.setText("preview_backend", "skia_" + retired.current_backend_mode)
        delivered = []
        deliver = canvas._on_async_frame

        def record_delivery(image, time_ms):
            delivered.append((image.size(), time_ms, image.text("preview_backend")))
            deliver(image, time_ms)

        monkeypatch.setattr(canvas, "_on_async_frame", record_delivery)
        retired.frame_ready.emit(stale, 550)
        assert canvas.set_gpu_preview_enabled(not gpu)
        assert canvas._async_renderer.selection.target == ("raster" if gpu else "gpu")
        canvas.set_time(550)
        deadline = time.monotonic() + 5
        while (not delivered or not sip.isdeleted(retired)) and not errors and time.monotonic() < deadline:
            QApplication.processEvents()
            if not sip.isdeleted(retired):
                QCoreApplication.sendPostedEvents(retired, QEvent.Type.DeferredDelete)
            time.sleep(0.01)
        assert not errors, errors
        assert all(size != stale.size() for size, _, _ in delivered)
        assert delivered and delivered[-1][1:] == (550, "skia_gpu" if not gpu else "skia_cpu")
        assert sip.isdeleted(retired)
    finally:
        canvas._stop_async_renderer()
        canvas.close()
        canvas.deleteLater()


def export_ffmpeg(tmp_path):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        import imageio_ffmpeg
        tools = tmp_path / "ffmpeg-bin"
        tools.mkdir(exist_ok=True)
        ffmpeg = str(tools / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg"))
        shutil.copy2(imageio_ffmpeg.get_ffmpeg_exe(), ffmpeg)
    return Path(ffmpeg)


@pytest.mark.parametrize("gpu,missing_loader", [(True, False), (False, False), (True, True)])
def test_product_export_short_mp4_uses_skia(binary, monkeypatch, tmp_path, supported_project, gpu, missing_loader,
                                            *, side_track=None):
    from krok_helper.subtitle_render.engine import renderer
    from krok_helper.subtitle_render.engine.export.render_job import RenderJob
    from krok_helper.subtitle_render.domain.background import BackgroundSource
    ffmpeg = export_ffmpeg(tmp_path)
    monkeypatch.setenv("KROK_SUBTITLE_RENDER_BACKEND", "skia")
    if missing_loader:
        if sys.platform == "darwin": pytest.skip("Vulkan loader failure injection")
        monkeypatch.setenv("KROK_SKIA_VULKAN_LIBRARY", str(tmp_path / "missing"))
    monkeypatch.setattr(renderer, "paint_frame", lambda *a, **k: pytest.fail("subtitle QPainter export was called"))
    monkeypatch.setattr(renderer, "paint_frame_to_painter", lambda *a, **k: pytest.fail("subtitle QPainter export was called"))
    track, style = supported_project
    job = RenderJob(track, style, tmp_path / "unused", tmp_path / "p04.mp4", width=640, height=360,
                    background_source=BackgroundSource(kind="solid", color="#183048"), fps=60,
                    duration_ms=1500, include_audio=False, gpu_export_enabled=gpu, preset="ultrafast",
                    extra_tracks=() if side_track is None else (side_track,))
    logs = []
    result = renderer.render_subtitle_video(job, ffmpeg_dir=Path(ffmpeg).parent, logger=logs.append)
    assert any("Skia 字幕后端" in line and ("GPU" if gpu and not missing_loader else "Raster CPU") in line for line in logs)
    assert result.is_file() and result.stat().st_size > 1000
    decoded = subprocess.run([ffmpeg, "-v", "error", "-i", str(result), "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                             capture_output=True, timeout=30)
    assert decoded.returncode == 0, decoded.stderr
    frames = np.frombuffer(decoded.stdout, np.uint8).reshape(-1, 360, 640, 3)
    assert len(frames) == 90
    assert np.mean(frames[33].max(axis=-1) > 150) > 0.003
    if side_track is not None:
        bright = frames[33].max(axis=-1) > 150
        assert bright[:180].sum() > 150 and bright[180:].sum() > 150


@pytest.mark.parametrize('gpu', [True, False], ids=['gpu', 'raster'])
@pytest.mark.parametrize('surface', ['preview', 'mp4'])
def test_multitrack_product_preview_and_export(binary, monkeypatch, tmp_path, supported_project, gpu, surface):
    track, style = supported_project
    track = replace(track, lines=[replace(line, display_start_override_ms=0, display_end_override_ms=1200)
                                  for line in track.lines])
    side = replace(track, lines=[replace(line, layout_index=1) for line in track.lines],
                   meta=replace(track.meta, offset_ms=100))
    style = replace(style, line_y_position='bottom', line_y_margin_px=80,
                    layouts=[LyricsLayout(layout_id='top', line_y_position='top', line_y_margin_px=80)])
    check = (test_actual_g5_preview_uses_skia_even_when_gpu_off if surface == 'preview'
             else test_product_export_short_mp4_uses_skia)
    check(binary, monkeypatch, tmp_path, (track, style), gpu, False, side_track=side)


@pytest.mark.parametrize('target', ['gpu', 'raster'])
@pytest.mark.parametrize('fps', [60, 120, 60000 / 1001], ids=['60', '120', 'ntsc60'])
def test_skia_export_pipe_uses_exact_frame_times(binary, monkeypatch, tmp_path, supported_project, target, fps):
    from hashlib import sha256
    from types import SimpleNamespace
    from krok_helper.subtitle_render.domain.frame_rate import frame_timestamp
    from krok_helper.subtitle_render.engine.export.render_job import RenderJob
    from krok_helper.subtitle_render.engine.renderer import _write_frames_native

    monkeypatch.setenv('KROK_SUBTITLE_RENDER_BACKEND', 'skia')
    monkeypatch.setenv('KROK_SUBTITLE_SKIA_TARGET', target)
    monkeypatch.setenv('KROK_SUBTITLE_SKIA_FALLBACK', 'none')
    monkeypatch.setenv('KROK_SUBTITLE_NATIVE_EXPORT_CHUNK_BYTES', str(320 * 180 * 4 * 17))
    track, style = supported_project
    job = RenderJob(track, style, tmp_path / 'unused', tmp_path / 'unused.mp4', width=320, height=180,
                    fps=fps, duration_ms=1500, gpu_export_enabled=target == 'gpu')
    count = round(fps * 1.5)
    committed = []
    borrowed = []

    def write_frame(frame):
        assert isinstance(frame, memoryview)
        if not committed:
            borrowed.append(frame)
        committed.append(sha256(frame).digest())

    pipe = SimpleNamespace(stdin=SimpleNamespace(write=write_frame))
    _write_frames_native(pipe, job, count, binary, None, None)
    assert len(committed) == count
    with pytest.raises(ValueError, match='released memoryview'):
        bytes(borrowed[0])

    indices = list(range(0, count, 5))
    times = [frame_timestamp(index, fps) for index in indices]
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        process.configure(track, style, width=320, height=180, fps=fps, duration_ms=1500)
        reference, _ = collect(process, times)
    assert len(set(committed)) > 3
    assert [committed[index] for index in indices] == [sha256(frame).digest() for frame in reference]


def transparent_sequence_job(tmp_path, project, *, gpu):
    from krok_helper.subtitle_render.engine.export.render_job import OUTPUT_FORMAT_PNG_TRANSPARENT, RenderJob

    track, style = project
    return RenderJob(track, style, tmp_path / 'unused', tmp_path / '字幕', width=320, height=180,
                     fps=60, duration_ms=1200, include_audio=False, gpu_export_enabled=gpu,
                     output_format=OUTPUT_FORMAT_PNG_TRANSPARENT)


@pytest.mark.parametrize('policy,failure', [('raster', 'skia_gpu_device'), ('none', 'skia_gpu_device'), ('raster', 'invalid_scene')])
def test_product_export_retry_only_for_gpu_device_failure(binary, monkeypatch, tmp_path, supported_project, policy, failure):
    from krok_helper.errors import ProcessingError
    from krok_helper.subtitle_render.engine.renderer import render_subtitle_video

    monkeypatch.setenv('KROK_SUBTITLE_RENDER_BACKEND', 'skia')
    monkeypatch.setenv('KROK_SUBTITLE_SKIA_TARGET', 'gpu')
    monkeypatch.setenv('KROK_SUBTITLE_SKIA_FALLBACK', policy)
    ffmpeg = export_ffmpeg(tmp_path)
    job = transparent_sequence_job(tmp_path, supported_project, gpu=True)
    progress, processes, logs = [], [], []
    live_event = NativeRendererProcess.read_event
    injected = False

    def next_event(process):
        nonlocal injected
        event = live_event(process)
        if (isinstance(process, SkiaRendererProcess) and not injected and progress == list(range(1, 13))
                and process.skia_info.get('execution_target') == 'gpu' and event.get('event') == 'frame_ready'):
            injected = True
            return {'ok': False, 'event': 'range_done', 'generation': event['generation'],
                    'error_code': failure, 'error': 'synthetic failure after 12 GPU frames'}
        return event

    monkeypatch.setattr(NativeRendererProcess, 'read_event', next_event)
    def render(job):
        return render_subtitle_video(job, ffmpeg_dir=ffmpeg.parent, logger=logs.append,
            on_progress=lambda done, total: progress.append(done), on_process_started=processes.append)

    if policy == 'raster' and failure == 'skia_gpu_device':
        output = render(job)
        assert progress == [*range(1, 13), *range(1, 73)]
        assert processes[1::2] == [None, None] and len(processes) == 4
        assert all(process.poll() is not None for process in processes[::2])
        assert any('Skia GPU 导出设备故障' in line for line in logs)
        assert any('Skia 字幕后端：Raster CPU' in line for line in logs)
        reference = render(replace(job, gpu_export_enabled=False, output_path=tmp_path / 'CPU参考'))
        actual = sorted(output.glob('*.png'))
        expected = sorted(reference.glob('*.png'))
        assert len(actual) == len(expected) == 72
        assert [path.read_bytes() for path in actual] == [path.read_bytes() for path in expected]
    else:
        with pytest.raises(ProcessingError, match='synthetic failure'):
            render(job)
        assert len(processes) == 2 and processes[-1] is None and processes[0].poll() is not None
        assert progress == list(range(1, 13))
        assert not job.output_path.exists()
    assert injected


@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_product_export_cancel_removes_partial_pngs(binary, monkeypatch, tmp_path, supported_project, target):
    from krok_helper.errors import ExportCancelled
    from krok_helper.subtitle_render.engine.renderer import render_subtitle_video

    monkeypatch.setenv('KROK_SUBTITLE_RENDER_BACKEND', 'skia')
    monkeypatch.setenv('KROK_SUBTITLE_SKIA_TARGET', target)
    monkeypatch.setenv('KROK_SUBTITLE_SKIA_FALLBACK', 'none')
    ffmpeg = export_ffmpeg(tmp_path)
    job = transparent_sequence_job(tmp_path, supported_project, gpu=target == 'gpu')
    progress, processes = [], []
    with pytest.raises(ExportCancelled):
        render_subtitle_video(job, ffmpeg_dir=ffmpeg.parent, should_cancel=lambda: len(progress) >= 12,
            on_progress=lambda done, total: progress.append(done), on_process_started=processes.append)
    assert progress == list(range(1, 13))
    assert len(processes) == 2 and processes[-1] is None and processes[0].poll() is not None
    assert not job.output_path.exists()


def test_product_export_reports_sidecar_exit_and_cleans_earlier_frames(binary, monkeypatch, tmp_path, supported_project):
    from krok_helper.errors import ProcessingError
    from krok_helper.subtitle_render.engine.renderer import render_subtitle_video

    monkeypatch.setenv('KROK_SUBTITLE_RENDER_BACKEND', 'skia')
    monkeypatch.setenv('KROK_SUBTITLE_SKIA_TARGET', 'gpu')
    monkeypatch.setenv('KROK_SUBTITLE_SKIA_FALLBACK', 'raster')
    # Let the first range finish so the export has real frames in FFmpeg.
    # Kill the sidecar just before it accepts the next range.
    monkeypatch.setenv('KROK_SUBTITLE_NATIVE_EXPORT_CHUNK_BYTES', str(320 * 180 * 4 * 12))
    start_range = SkiaRendererProcess.start_render_range
    failures = []

    def start_or_exit(process, timestamps, **kwargs):
        if kwargs['generation'] == 2:
            child = process._process
            assert child is not None and child.poll() is None
            child.kill()
            failures.append(child.wait(timeout=5))
        return start_range(process, timestamps, **kwargs)

    monkeypatch.setattr(SkiaRendererProcess, 'start_render_range', start_or_exit)
    ffmpeg = export_ffmpeg(tmp_path)
    job = transparent_sequence_job(tmp_path, supported_project, gpu=True)
    progress, processes = [], []
    with pytest.raises(ProcessingError, match='native 字幕渲染器导出失败'):
        render_subtitle_video(job, ffmpeg_dir=ffmpeg.parent,
                              on_progress=lambda done, total: progress.append(done), on_process_started=processes.append)
    assert len(failures) == 1 and failures[0] != 0
    assert progress == list(range(1, 13))
    assert len(processes) == 2 and processes[-1] is None and processes[0].poll() is not None
    assert not job.output_path.exists()


def test_unsupported_product_export_fails_and_cleans_partial_output(binary, monkeypatch, tmp_path, supported_project):
    from krok_helper.errors import ProcessingError
    from krok_helper.subtitle_render.engine import renderer
    from krok_helper.subtitle_render.engine.export.render_job import RenderJob
    from krok_helper.subtitle_render.domain.background import BackgroundSource
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg: pytest.skip("Covered by the MP4 fixture on hosts without a system ffmpeg")
    monkeypatch.setenv("KROK_SUBTITLE_RENDER_BACKEND", "skia")
    monkeypatch.setattr(renderer, "paint_frame", lambda *a, **k: pytest.fail("subtitle QPainter fallback was called"))
    track, style = supported_project
    job = RenderJob(track, replace(style, stroke_width_px=7, stroke2_width_px=6, stroke2_enabled=True,
                                  right_to_left=True),
                    tmp_path / "unused", tmp_path / "unsupported.mp4",
                    background_source=BackgroundSource(kind="solid"), width=640, height=360, fps=60,
                    duration_ms=1500, include_audio=False, gpu_export_enabled=False)
    with pytest.raises(ProcessingError):
        renderer.render_subtitle_video(job, ffmpeg_dir=Path(ffmpeg).parent)
    assert not job.output_path.exists()
