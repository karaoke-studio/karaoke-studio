"""Real upper-ruby source flows through product IR, preview and export."""
from dataclasses import replace
import json
import sys

import numpy as np
import pytest

from test_subtitle_skia_ruby_groups import application, binary, project, mixed, annotated, pages, reading, colors, configure
from test_subtitle_skia_ruby_groups import ir_for as probe_ir
from test_subtitle_skia_resolved import ir_for as product_ir
from test_subtitle_skia_protocol import collect, selection
from test_subtitle_skia_protocol import test_actual_g5_preview_uses_skia_even_when_gpu_off as run_preview
from test_subtitle_skia_protocol import test_product_export_short_mp4_uses_skia as run_mp4
from krok_helper.subtitle_render.domain.models import TimingChar
from krok_helper.subtitle_render.engine.ruby.timing import ruby_visual_units_and_intervals
from krok_helper.subtitle_render.engine.ruby.skia_timing import prepare_ruby_wipe_timing
from krok_helper.subtitle_render.native.backend import NativeRendererError, NativeRendererProcess
from krok_helper.subtitle_render.native.skia_backend import SkiaRendererProcess
from krok_helper.subtitle_render.native.skia_text import SkiaTextMetricsProcess
from krok_helper.subtitle_render.native.skia_fonts import resolve_skia_font
from krok_helper.subtitle_render.engine.render.skia_ir import build_skia_render_ir


@pytest.mark.parametrize('target', ['gpu', 'raster'])
@pytest.mark.parametrize('mode', ['reading_units', 'checkpoint_segments'])
def test_real_source_compiles_to_the_verified_final_group_without_legacy_projection(binary, reading, target, mode, monkeypatch, tmp_path):
    from krok_helper.subtitle_render.engine.ruby import layout, selection as ruby_selection
    from krok_helper.subtitle_render.engine.text import metrics
    fail = lambda *a, **k: pytest.fail('Legacy Qt ruby layout/timing was invoked')
    monkeypatch.setattr(layout, 'QFontMetrics', fail)
    monkeypatch.setattr(metrics, 'QFontMetrics', fail)
    monkeypatch.setattr(ruby_selection, 'compute_char_intervals', fail)
    track, style = reading
    style = replace(style, ruby_main_progress_mode=mode)
    with SkiaTextMetricsProcess(binary) as metrics_client:
        ir = build_skia_render_ir(track, style, measure=metrics_client.measure, resolve_font=resolve_skia_font,
                                  width=641, height=720, fps=60)
    assert ir['track']['rubies'] and 'p05_upper_ruby' in ir['required_features']
    # Product style_for_track materializes inherited fields before planning;
    # its cache identity may differ from the direct preparation probe.
    content = lambda layout: {k: v for k, v in layout.items() if k != 'metrics_identity'}
    assert content(ir['skia_layout']) == content(probe_ir(binary, (track, style))['skia_layout'])
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        process.configure(track, style, width=641, height=720, fps=60)
        frames, _ = collect(process, [100, 550, 1100, 1700, 2300, 550])
        configure(process, probe_ir(binary, (track, style)))
        reference, _ = collect(process, [100, 550, 1100, 1700, 2300, 550], generation=2)
        assert all(np.array_equal(a,b) for a,b in zip(frames, reference, strict=True))
        assert np.array_equal(frames[1], frames[-1])
    if target == 'gpu' and mode == 'reading_units':
        (tmp_path/'upper-ruby-product.json').write_text(json.dumps({
            'source': ir['track']['rubies'], 'lines': ir['skia_layout']['lines'],
            'features': ir['required_features']}, sort_keys=True), encoding='utf-8')


def test_fallback_reading_clock_uses_gpu_half_up_without_changing_the_old_default(binary, reading):
    from test_subtitle_skia_ruby_pages import prepare
    track, style = reading
    line = replace(track.lines[0], chars=[TimingChar('字', 0), TimingChar('字', 3)], end_ms=5)
    ruby = replace(track.rubies[0], pos_start_ms=0, pos_end_ms=5, reading_parts=[], reading_part_ms=[])
    track = replace(track, lines=[line], rubies=[ruby])
    assert ruby_visual_units_and_intervals(ruby) == [('A', (0, 2)), ('A', (2, 5))]
    prepared = prepare(binary, track, style)
    clock = prepare_ruby_wipe_timing(line, prepared.geometry.ruby.lines[0], 'reading_units')
    assert clock.readings[0].intervals == ((0, 3), (3, 5))
    ir = product_ir(binary, (track, style))
    assert [u['interval_ms'] for u in ir['skia_layout']['lines'][0]['ruby_groups'][0]['units']] == [[0, 3], [3, 5]]


def test_small_kana_fallback_uses_gpu_graphemes_and_keeps_old_mora_default(binary, reading, tmp_path):
    from skia_text_fixture import make_font
    from fontTools.ttLib import TTFont
    path = make_font(tmp_path/'kana.ttf')
    font = TTFont(path, recalcTimestamp=False)
    for cmap in font['cmap'].tables:
        if cmap.isUnicode(): cmap.cmap.update({ord('き'): 'cjk', ord('ゃ'): 'cjk'})
    font.save(path); font.close()
    track, style = reading
    line = replace(track.lines[0], chars=[TimingChar('字', 0), TimingChar('字', 3)], end_ms=5)
    ruby = replace(track.rubies[0], reading='きゃ', pos_start_ms=0, pos_end_ms=5,
                   reading_parts=[], reading_part_ms=[])
    assert ruby_visual_units_and_intervals(ruby) == [('き', (0,2)), ('ゃ', (2,5))]
    with SkiaTextMetricsProcess(binary) as metrics:
        ir = build_skia_render_ir(replace(track, lines=[line], rubies=[ruby]), style,
            measure=metrics.measure, resolve_font=lambda _: ({'path': str(path)}, {}), width=640, height=360, fps=60)
    units = ir['skia_layout']['lines'][0]['ruby_groups'][0]['units']
    assert [u['interval_ms'] for u in units] == [[0,3],[3,5]]
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        configure(process, ir)
        frames, _ = collect(process, [0,2,3,5])
    assert all(frame.any() for frame in frames)


@pytest.mark.parametrize('semantics', ['legacy', 'n3_1074'])
@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_real_multiline_upper_ruby_uses_prepared_page_positions(binary, pages, semantics, target):
    track, style = pages
    style = replace(style, layout_semantics=semantics)
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        process.configure(track, style, width=641, height=720, fps=60)
        frames, _ = collect(process, [550, 1900, 550])
        configure(process, probe_ir(binary, (track, style)))
        expected, _ = collect(process, [550, 1900, 550], generation=2)
    assert all(np.array_equal(a,b) for a,b in zip(frames, expected, strict=True))


@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_product_upper_ruby_preserves_all_time_offsets_and_dpr(binary, reading, target):
    track, style = reading
    results = []
    for shifted in [False, True]:
        source = replace(track, meta=replace(track.meta, offset_ms=137)) if shifted else track
        appearance = replace(style, timing_offset_ms=229) if shifted else style
        with SkiaRendererProcess(binary, selection=selection(target)) as process:
            process.configure(source, appearance, width=640, height=360, fps=60, dpr=1.5,
                              roll_shift_ms=400 if shifted else 0)
            frames, _ = collect(process, [t+(766 if shifted else 0) for t in [100, 550, 1100, 1700, 2300]])
        results.append(frames)
    assert all(np.array_equal(a,b) for a,b in zip(*results))


def test_real_source_preserves_explicit_main_boundaries(binary, reading):
    track, style = reading
    line = replace(track.lines[0], chars=[track.lines[0].chars[0], replace(track.lines[0].chars[1], explicit_start=True)])
    ir = product_ir(binary, (replace(track, lines=[line]), style))
    assert ir['track']['lines'][0]['resolved_intervals'] == [[300,900],[900,2100]]
    assert all('wipe_points' not in c for c in ir['skia_layout']['lines'][0]['cells'])
    assert ir['skia_layout']['lines'][0]['ruby_groups'][0]['units'][1]['interval_ms'] == [1400,2100]


@pytest.mark.parametrize('gpu,missing_loader', [(True, False), (False, False), (True, True)])
def test_real_upper_ruby_g5_preview_uses_skia(binary, reading, monkeypatch, tmp_path, gpu, missing_loader):
    run_preview(binary, monkeypatch, tmp_path, reading, gpu, missing_loader)


@pytest.mark.parametrize('gpu,missing_loader', [(True, False), (False, False), (True, True)])
def test_real_upper_ruby_short_mp4_uses_skia(binary, reading, monkeypatch, tmp_path, gpu, missing_loader):
    track, style = reading
    style = replace(style, karaoke_colors=colors('#FFFFFF', '#00FF00'),
                    ruby_karaoke_colors=colors('#FFFFFF', '#00FF00'))
    run_mp4(binary, monkeypatch, tmp_path, (track, style), gpu, missing_loader)


@pytest.mark.parametrize('mutation', ['feature', 'groups', 'schema', 'group_type', 'lower'])
def test_raw_annotations_without_complete_final_layout_are_rejected_and_keep_scene(binary, reading, mutation):
    ir = product_ir(binary, reading)
    if mutation == 'feature': ir['required_features'].remove('p05_upper_ruby')
    elif mutation == 'groups': del ir['skia_layout']['lines'][0]['ruby_groups']
    elif mutation == 'schema': ir['skia_layout']['ruby_schema'] = 2
    elif mutation == 'group_type': ir['skia_layout']['lines'][0]['ruby_groups'] = None
    elif mutation == 'lower': ir['track']['rubies'][0]['lower'] = True
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        configure(process, product_ir(binary, reading))
        before, _ = collect(process, [550])
        with pytest.raises(NativeRendererError): configure(process, ir)
        after, _ = collect(process, [550], generation=2)
    assert np.array_equal(before[0], after[0])


@pytest.mark.parametrize('change', [
    {'entry_anim': 'noise'}, {'karaoke_anim': 'zoom_pulse'},
    {'right_to_left': True}, {'vertical': True},
    {'scanline_enabled': True, 'scanline_shape': 'flame'}])
def test_unsupported_upper_ruby_combinations_fail_and_keep_previous_scene(binary, reading, change):
    track, style = reading
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        process.configure(track, style, width=640, height=360, fps=60)
        before, _ = collect(process, [550])
        with pytest.raises(NativeRendererError):
            process.configure(track, replace(style, **change), width=640, height=360, fps=60)
        after, _ = collect(process, [550], generation=2)
    assert np.array_equal(before[0], after[0])


def test_old_group_only_sidecar_cannot_silently_accept_product_upper_ruby(monkeypatch, tmp_path):
    path = tmp_path/'old'; path.touch()
    profiles = ['p05_resolved_horizontal', 'p05_single_line_placement', 'p05_inline_roles', 'p05_fixed_row_grid',
                'p05_page_collisions', 'p05_smart_horizontal', 'p05_segmented_wipe', 'p05_resolved_ruby']
    monkeypatch.setattr(NativeRendererProcess, 'start', lambda _: {'backend': 'skia', 'schema': 3,
        'selection_version': 1, 'resolved_layout_schema': 2, 'resolved_wipe_schema': 1,
        'resolved_ruby_schema': 1, 'profiles': profiles})
    monkeypatch.setattr(SkiaRendererProcess, 'abort', lambda *a, **k: None)
    sent = []; monkeypatch.setattr(SkiaRendererProcess, '_send', lambda self, value: sent.append(value))
    with pytest.raises(NativeRendererError, match='上注音'): SkiaRendererProcess(path).start()
    assert not sent


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('semantics', ['legacy', 'n3_1074'])
@pytest.mark.parametrize('mode', ['reading_units', 'checkpoint_segments'])
@pytest.mark.parametrize('fallback_parts', [False, True])
def test_real_product_ruby_matches_d2d_gpu(binary, reading, semantics, mode, fallback_parts):
    from test_subtitle_render_gpu_backend import _render_g1_frames
    track, style = reading
    if fallback_parts:
        track = replace(track, rubies=[replace(track.rubies[0], reading_parts=[], reading_part_ms=[])])
    style = replace(style, layout_semantics=semantics, ruby_main_progress_mode=mode,
                    karaoke_colors=colors('#FF0000', '#00FF00'), ruby_karaoke_colors=colors('#FF0000', '#00FF00'))
    times = [299, 300, 550, 899, 900, 1100, 1400, 1700, 2100, 2300]
    with NativeRendererProcess() as d2d:
        _, raw = _render_g1_frames(d2d, style, tuple(times), force_warp=False, track=track, width=640, height=360)
    with SkiaRendererProcess(binary, selection=selection('gpu')) as skia:
        skia.configure(track, style, width=640, height=360, fps=60)
        frames, _ = collect(skia, times)
    def bounds(p, channel):
        p = p.astype(np.int16)
        y, x = np.where((p[..., 3] > 200) & (p[..., channel] > p[..., 1-channel]+50))
        return None if not len(x) else np.array([x.min(), y.min(), x.max(), y.max()])
    for t, reference, actual in zip(times, raw, frames, strict=True):
        reference = np.frombuffer(reference, np.uint8).reshape(360, 640, 4)
        for channel in [0, 1]:
            a, b = bounds(reference, channel), bounds(actual, channel)
            assert (a is None) == (b is None), (t, a, b)
            if a is not None: assert np.max(np.abs(a-b)) <= 1, (t, a, b)
