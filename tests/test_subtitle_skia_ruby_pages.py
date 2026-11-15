"""Canonical ruby anchors enter shared paging, SmartHorizon and main-ink ports.

The drawing probe deliberately renders only main text from the prepared plan;
the product assertion also verifies the integrated upper-ruby placement.
"""
from dataclasses import replace
import json
import sys

import numpy as np
import pytest

from test_subtitle_skia_ruby_geometry import application, binary, project, mixed, annotated
from test_subtitle_skia_protocol import collect, selection
from krok_helper.subtitle_render.domain.models import TimingChar, TimingLine
from krok_helper.subtitle_render.domain.timing import RubyAnnotation, TrackPage, TrackSection, TrackPagePlan
from krok_helper.subtitle_render.engine.layout.display.skia_collision import SkiaCollisionGeometry
from krok_helper.subtitle_render.engine.layout.line.skia_geometry import positioned_cells, collision_bounds
from krok_helper.subtitle_render.engine.layout.plan.skia_plan import prepare_skia_layout
from krok_helper.subtitle_render.engine.render.render_ir import build_render_ir
from krok_helper.subtitle_render.engine.render.skia_ir import build_skia_render_ir
from krok_helper.subtitle_render.native.backend import NativeRendererProcess
from krok_helper.subtitle_render.native.protocol import apply_resolved_font_faces
from krok_helper.subtitle_render.native.skia_backend import SkiaRendererProcess
from krok_helper.subtitle_render.native.skia_fonts import resolve_skia_font
from krok_helper.subtitle_render.native.skia_text import SkiaTextMetricsProcess


@pytest.fixture
def pages(annotated):
    track, style = annotated
    style = replace(style, dual_line_layout=True, line_alignments=['left', 'center', 'right'],
        line_offsets_x=[7, -11, 13], line_horizontal_layout='asymmetric', smart_horizontal='equal_margins',
        horizontal_margin_px=30, line_y_margin_px=40, line_gap_px=100,
        allow_inter_page_line_overlap=True, auto_fill_section_time=False, sync_entry=False)
    lines = [TimingLine(chars=[TimingChar(t, 200+j*200) for j, t in enumerate(text)], end_ms=1800, track_line_index=i,
        display_start_override_ms=0, display_end_override_ms=12000)
        for i, text in enumerate(('字', '字A', '字字A'))]
    rubies = [RubyAnnotation('字', text, pos_start_ms=200, pos_end_ms=1800,
        target_line_index=i, target_char_start=0, target_char_end=1)
        for i, text in enumerate(('AAA字AAA', '字A', 'AA字A'))]
    return replace(track, lines=lines, rubies=rubies), style


def prepare(binary, track, style, *, resolve_font=resolve_skia_font, **kwargs):
    with SkiaTextMetricsProcess(binary) as metrics:
        return prepare_skia_layout(track, style, measure=metrics.measure, resolve_font=resolve_font,
                                   logical_w=641, logical_h=720, **kwargs)


def main_probe_ir(track, style, prepared):
    # Test adapter: omit annotations to isolate the final main-text geometry.
    ir = build_render_ir(replace(track, rubies=[]), style, width=641, height=720, fps=60,
                         layout_plan_builder=lambda *a, **k: prepared.plan)
    geometry = prepared.geometry
    ir['required_features'] = ['p05_resolved_horizontal', 'p05_segmented_wipe']
    ir['skia_layout'] = {'version': 2, 'wipe_schema': 1, 'metrics_identity': prepared.measured.identity,
        'fonts': prepared.measured.fonts, 'runs': prepared.measured.runs,
        'lines': [{'cells': positioned_cells(local, geometry.position(item, 641, 720))}
                  for item, local in zip(prepared.plan.lines, geometry.locals, strict=True)]}
    apply_resolved_font_faces(ir)
    return ir


@pytest.mark.parametrize('semantics', ['legacy', 'n3_1074'])
@pytest.mark.parametrize('mode', ['center_position', 'equal_margins'])
def test_page_statistics_and_final_main_positions_use_ruby_anchor(binary, pages, semantics, mode, tmp_path, monkeypatch):
    from krok_helper.subtitle_render.engine.text import metrics as qt_metrics
    monkeypatch.setattr(qt_metrics, 'QFontMetrics', lambda *a: pytest.fail('Qt metric path'))
    monkeypatch.setattr(qt_metrics, 'QPainterPath', lambda *a: pytest.fail('Qt outline path'))
    track, style = pages
    style = replace(style, layout_semantics=semantics, smart_horizontal=mode)
    prepared = prepare(binary, track, style)
    geometry = prepared.geometry
    assert geometry.ruby is not None
    for index, (item, local) in enumerate(zip(prepared.plan.lines, geometry.ruby.lines, strict=True)):
        assert geometry.locals[index] is local.main
        assert geometry.smart.anchor_width(index) == pytest.approx(local.anchor[1]-local.anchor[0])
        assert item.resolved_intervals == local.intervals
        position = geometry.position(item, 641, 720)
        expected = collision_bounds(local.main, position)
        actual = geometry.geometry(item, 641, 720)
        assert (actual.cross_min, actual.axis_min, actual.cross_max, actual.axis_max) == expected
        # Ruby ink affects spacing/anchoring, not the shared MAIN-ink collision envelope.
        ruby_top = min(c['metrics']['ink'][1]+c['baseline_y']+position.baseline_y
                       for r in local.rubies for c in r.cells)
        assert actual.axis_min > ruby_top
    # Unseen siblings are still page members and keep their ruby widths.
    delayed = replace(track, lines=[track.lines[0], *[replace(l, display_start_override_ms=3000) for l in track.lines[1:]]])
    other = prepare(binary, delayed, style)
    positions = lambda p: [p.geometry.position(item, 641, 720) for item in p.plan.lines]
    assert positions(prepared) == positions(other)
    if semantics == 'n3_1074' and mode == 'equal_margins':
        (tmp_path/'ruby-page-layout.json').write_text(json.dumps({
            'anchors': [l.anchor for l in geometry.ruby.lines],
            'main': main_probe_ir(track, style, prepared)['skia_layout']['lines'],
            'schedule': [{'lane': l.lane, 'intervals': l.resolved_intervals,
                          'offsets': l.layout_offset_windows} for l in prepared.plan.lines],
        }, sort_keys=True), encoding='utf-8')


@pytest.mark.parametrize('semantics', ['legacy', 'n3_1074'])
def test_lanes_and_authored_short_pages_invalidate_ruby_page_statistics(binary, pages, semantics):
    track, style = pages
    style = replace(style, layout_semantics=semantics)
    prepared = prepare(binary, track, style)
    geometry = prepared.geometry
    geometry.geometry(prepared.plan.lines[0], 641, 720)
    original = geometry.smart.maxima.copy()
    changed = [*prepared.plan.lines[:2], replace(prepared.plan.lines[2], lane=0)]
    geometry.set_lanes(changed)
    assert not geometry.cache and not geometry.smart.maxima
    geometry.geometry(changed[0], 641, 720)
    assert geometry.smart.maxima != original
    track = replace(track, page_plan=TrackPagePlan(sections=[TrackSection(pages=[TrackPage(2), TrackPage(1)])]))
    short = prepare(binary, track, style)
    assert [l.page_line_count for l in short.plan.lines] == [2, 2, 1]
    assert short.plan.lines[-1].center_override
    assert short.geometry.smart.shift(short.plan.lines[-1], 641) == 0


def test_ruby_geometry_identity_and_clock_reject_stale_preparation(binary, pages):
    track, style = pages
    style = replace(style, layout_semantics='n3_1074')
    prepared = prepare(binary, track, style)
    geometry = prepared.geometry
    styles = geometry.styles
    with pytest.raises(ValueError, match='度量'):
        SkiaCollisionGeometry(track, style, styles, replace(prepared.measured, identity='stale'),
                              ruby_geometry=geometry.ruby)
    changed = replace(track, lines=[replace(track.lines[0], end_ms=2500), *track.lines[1:]])
    with pytest.raises(ValueError, match='时间'):
        SkiaCollisionGeometry(changed, style, styles, prepared.measured, ruby_geometry=geometry.ruby)
    altered = replace(track, rubies=[replace(track.rubies[0], reading='A'), *track.rubies[1:]])
    second = prepare(binary, altered, style)
    assert second.measured.identity == prepared.measured.identity
    assert second.geometry.identity != geometry.identity
    assert second.geometry.smart.anchor_width(0) != geometry.smart.anchor_width(0)


@pytest.mark.parametrize('semantics', ['legacy', 'n3_1074'])
def test_adjacent_reading_interference_enters_main_ink_and_page_width(binary, pages, semantics):
    track, style = pages
    style = replace(style, layout_semantics=semantics)
    line = replace(track.lines[0], chars=[TimingChar('字', 200), TimingChar('字', 800)])
    track = replace(track, lines=[line, *track.lines[1:]], rubies=[
        replace(track.rubies[0], pos_end_ms=800),
        replace(track.rubies[0], target_char_start=1, target_char_end=2, pos_start_ms=800)])
    prepared = prepare(binary, track, style)
    plain = prepare(binary, replace(track, rubies=[]), style)
    assert prepared.geometry.ruby.lines[0].gaps[1] > 0
    assert prepared.geometry.smart.anchor_width(0) > plain.geometry.smart.anchor_width(0)
    assert prepared.plan.lines[0].resolved_intervals == plain.plan.lines[0].resolved_intervals
    assert prepared.geometry.locals[0].cells[1]['origin_x'] > plain.geometry.locals[0].cells[1]['origin_x']


@pytest.mark.parametrize('semantics', ['legacy', 'n3_1074'])
def test_shared_page_lift_uses_prepared_positions_without_changing_main_clock(binary, pages, semantics):
    track, style = pages
    style = replace(style, layout_semantics=semantics, allow_inter_page_line_overlap=False,
                    overlap_fallback_mode='lift')
    track = replace(track, lines=[*track.lines, *[replace(l, track_line_index=i+3) for i, l in enumerate(track.lines)]],
        rubies=[*track.rubies, *[replace(r, target_line_index=i+3) for i, r in enumerate(track.rubies)]],
        page_plan=TrackPagePlan(sections=[TrackSection(pages=[TrackPage(3), TrackPage(3)])]))
    prepared = prepare(binary, track, style)
    assert any(item.layout_offset_windows for item in prepared.plan.lines)
    assert any(y != 0 for item in prepared.plan.lines for _start, _end, _x, y in item.layout_offset_windows)
    for item in prepared.plan.lines:
        assert item.resolved_intervals == prepared.geometry.ruby.lines[item.track_index].intervals
        assert all(start < end for start, end, _x, y in item.layout_offset_windows)


def test_replacing_only_ruby_font_bytes_invalidates_page_geometry_and_shared_cache(binary, pages, tmp_path):
    from skia_text_fixture import make_font
    track, style = pages
    style = replace(style, layout_semantics='n3_1074', ruby_font_family_latin='Probe Ruby', smart_horizontal='none')
    path = tmp_path/'ruby.ttf'
    def resolve(appearance):
        return ({'path': str(path)}, {}) if appearance.font_family == 'Probe Ruby' else resolve_skia_font(appearance)
    results = []
    for width in (700, 1100):
        make_font(path, family='Probe Ruby', a_width=width, ps_name='Probe-Ruby')
        results.append(prepare(binary, track, style, resolve_font=resolve))
    a, b = results
    assert a.measured.identity == b.measured.identity
    assert a.geometry.identity != b.geometry.identity
    assert a.geometry.smart.anchor_width(0) != b.geometry.smart.anchor_width(0)
    assert a.geometry.position(a.plan.lines[0], 641, 720) != b.geometry.position(b.plan.lines[0], 641, 720)


@pytest.mark.parametrize('semantics', ['legacy', 'n3_1074'])
def test_ruby_anchor_preparation_keeps_shared_source_span_timing(binary, pages, semantics):
    track, style = pages
    chars = [TimingChar(text, 200, source_span_start_ms=200, source_span_end_ms=1400,
                        source_span_index=i, source_span_count=3) for i, text in enumerate(('A', 'i', '字'))]
    track = replace(track, lines=[TimingLine(chars=chars, end_ms=1400)], rubies=[track.rubies[0]])
    prepared = prepare(binary, track, replace(style, layout_semantics=semantics))
    expected = tuple(prepared.measured.snapshot.resolve_intervals(track.lines[0], replace(style, layout_semantics=semantics)))
    assert prepared.plan.lines[0].resolved_intervals == prepared.geometry.ruby.lines[0].intervals == expected
    assert expected[0][1]-expected[0][0] > expected[1][1]-expected[1][0]


@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_prepared_main_geometry_draws_and_product_ruby_uses_same_placement(binary, pages, target):
    track, style = pages
    prepared = prepare(binary, track, style)
    ir = main_probe_ir(track, style, prepared)
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        process._send({'cmd': 'configure', 'ir': ir})
        process._expect_ok(process._read_until_event('configured'))
        frames, _ = collect(process, [100, 10000, 100, 13000])
    assert np.array_equal(frames[0], frames[2]) and frames[0].any() and not frames[3].any()
    with SkiaTextMetricsProcess(binary) as metrics:
        product = build_skia_render_ir(track, style, measure=metrics.measure, resolve_font=resolve_skia_font,
                                      width=641, height=720, fps=60)
    assert 'p05_upper_ruby' in product['required_features']
    assert all(l['ruby_groups'] for l in product['skia_layout']['lines'])
    for expected, actual in zip(ir['skia_layout']['lines'], product['skia_layout']['lines'], strict=True):
        assert [(c['origin_x'], c['baseline_y']) for c in expected['cells']] == [
            (c['origin_x'], c['baseline_y']) for c in actual['cells']]


@pytest.mark.skipif(sys.platform != 'win32', reason='Direct2D GPU reference is Windows-only')
@pytest.mark.parametrize('semantics', ['legacy', 'n3_1074'])
@pytest.mark.parametrize('mode', ['none', 'center_position', 'equal_margins'])
def test_ruby_page_main_ink_matches_actual_d2d_gpu(binary, pages, semantics, mode):
    from test_subtitle_render_gpu_backend import _render_g1_frames
    from krok_helper.subtitle_render.domain.paint import KaraokeColors, KaraokeColorState, PaintFill
    track, style = pages
    transparent = KaraokeColorState(text=PaintFill(color='#00000000'))
    style = replace(style, layout_semantics=semantics, smart_horizontal=mode, ruby_colors_follow_main=False,
                    ruby_karaoke_colors=KaraokeColors(before=transparent, after=transparent))
    prepared = prepare(binary, track, style)
    with NativeRendererProcess() as d2d:
        _, reference = _render_g1_frames(d2d, style, (10000,), force_warp=False,
                                        track=track, width=641, height=720)
    with SkiaRendererProcess(binary, selection=selection('gpu')) as process:
        process._send({'cmd': 'configure', 'ir': main_probe_ir(track, style, prepared)})
        process._expect_ok(process._read_until_event('configured'))
        frames, _ = collect(process, [10000])
    def boxes(p):
        ys = np.flatnonzero((p[..., 3] > 127).any(axis=1))
        result = []
        for band in np.split(ys, np.flatnonzero(np.diff(ys) > 1)+1):
            if len(band):
                xs = np.flatnonzero((p[band[0]:band[-1]+1, :, 3] > 127).any(axis=0))
                result.append([xs[0], band[0], xs[-1], band[-1]])
        return result
    a = boxes(np.frombuffer(reference[0], np.uint8).reshape(720, 641, 4))
    b = boxes(frames[0])
    assert len(a) == len(b) == 3, (semantics, mode, a, b)
    assert np.max(np.abs(np.array(a)-np.array(b))) <= 1, (semantics, mode, a, b)
