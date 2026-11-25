"""Lower geometry/reference preparation, using only GPU/Raster image oracles.

The terminal-group adapter below remains deliberately a probe. Product source
annotations and independent paints are covered in test_subtitle_skia_lower_product.
"""
from dataclasses import asdict, replace
import copy
import json
import sys

import numpy as np
import pytest
from PyQt6.QtGui import QFontDatabase

from test_subtitle_skia_lower import application, binary, prepared_source, prepare
from test_subtitle_skia_protocol import collect, selection, sample
from test_subtitle_skia_roles import colors
from test_subtitle_skia_ruby_groups import configure
from krok_helper.subtitle_render.domain.models import SubtitleStyleScheme
from krok_helper.subtitle_render.engine.layout.plan.skia_plan import prepare_skia_layout
from krok_helper.subtitle_render.engine.layout.line.skia_geometry import positioned_cells
from krok_helper.subtitle_render.engine.render.render_ir import build_render_ir
from krok_helper.subtitle_render.engine.render.skia_ir import build_skia_render_ir
from krok_helper.subtitle_render.engine.render.skia_ruby_ir import prepared_ruby_layout
from krok_helper.subtitle_render.engine.ruby.lower_semantics import lower_style
from krok_helper.subtitle_render.engine.ruby.skia_reference import measure_references
from krok_helper.subtitle_render.engine.ruby.skia_timing import prepare_ruby_wipe_timing, wipe_points_to_ir
from krok_helper.subtitle_render.engine.ruby.skia_overlap import prepare_overlap_wipes
from krok_helper.subtitle_render.engine.text.font_weight import clear_font_weight_cache
from krok_helper.subtitle_render.native.backend import NativeRendererProcess, NativeRendererError
from krok_helper.subtitle_render.native.protocol import apply_resolved_font_faces
from krok_helper.subtitle_render.native.skia_backend import SkiaRendererProcess
from krok_helper.subtitle_render.native.skia_text import SkiaTextMetricsProcess


@pytest.fixture
def source(prepared_source):
    track, appearance, resolve = prepared_source
    ids = [QFontDatabase.addApplicationFont(resolve(replace(appearance, font_family=name))[0]['path'])
           for name in ['Main', 'Latin']]
    assert min(ids) >= 0
    clear_font_weight_cache()
    style = replace(sample()[1], font_family='Main', font_family_latin='Latin', font_size_px=50,
        latin_font_size_px=70, latin_font_stretch_pct=100, letter_spacing_px=-2,
        ruby_font_size_px=24, ruby_latin_font_size_px=30, ruby_stroke_width_px=0, ruby_stroke2_enabled=False,
        ruby_gap_px=10, ruby_interval_px=-3, lower_ruby_enabled=True, lower_ruby_gap_px=6,
        lower_ruby_interval_px=-2, _lower_romaji_options=(False, False, False, False),
        line_y_margin_px=40, line_lead_in_ms=300, line_tail_ms=400,
        ruby_main_progress_mode='reading_units',
        karaoke_colors=colors('#FF0000', '#00FF00'), ruby_colors_follow_main=False,
        ruby_karaoke_colors=colors('#FF0000', '#00FF00'))
    try:
        yield replace(track, lines=[replace(track.lines[0], track_line_index=0)]), style, resolve
    finally:
        for handle in ids: QFontDatabase.removeApplicationFont(handle)
        clear_font_weight_cache()


def prepared(binary, source):
    track, style, resolve = source
    with SkiaTextMetricsProcess(binary) as metrics:
        return prepare_skia_layout(track, style, resolve_font=resolve, measure=metrics.measure,
                                   logical_w=900, logical_h=600)


def probe_ir(source, value, *, dpr=1):
    track, style, _resolve = source
    # Use the real prepared plan, but remove source annotations ONLY in this
    # test adapter. Native consumes terminal geometry, not an unfinished lower
    # source profile. Paints are deliberately identical on both reading layers.
    ir = build_render_ir(replace(track, rubies=[]), replace(style, lower_ruby_enabled=False),
        width=900, height=600, fps=60, dpr=dpr, layout_plan_builder=lambda *a, **k: value.plan,
        serialize_source_baselines=False)
    geometry, main, ruby = value.geometry, value.measured, value.geometry.ruby
    fonts = {**main.fonts, **ruby.measured.fonts}
    lines = []
    for item, local in zip(value.plan.lines, ruby.lines, strict=True):
        position = geometry.position(item, 900, 600)
        timing = prepare_ruby_wipe_timing(item.line, local, item.layout_style.ruby_main_progress_mode)
        main_ends, endpoints = prepare_overlap_wipes(local, main.lines[item.track_index], timing, dpr=dpr)
        cells = positioned_cells(local.main, position)
        for cell, curve, end in zip(cells, timing.main, main_ends, strict=True):
            cell['wipe_end_position'] = end
            if curve is not None: cell['wipe_points'] = wipe_points_to_ir(curve)
        groups = []
        for group, clock, ends in zip(local.rubies, timing.readings, endpoints, strict=True):
            units = [{**cell, 'text': text, 'run_index': cell['run_index']+len(main.runs),
                'interval_ms': list(interval), 'wipe_end_position': end, 'baseline_y': cell['baseline_y']+position.baseline_y,
                **{key: cell[key]+position.offset_x for key in ('origin_x', 'wipe_left', 'wipe_right')}}
                for cell, text, interval, end in zip(group.cells, clock.units, clock.intervals, ends, strict=True)]
            groups.append({'style_cell': group.target.indices[0], 'units': units})
        lines.append({'cells': cells, 'ruby_groups': groups})
    ir['required_features'] = ['p05_resolved_horizontal', 'p05_segmented_wipe', 'p05_resolved_ruby', 'p05_overlap_wipe']
    ir['skia_layout'] = {'version': 2, 'wipe_schema': 1, 'ruby_schema': 1, 'overlap_schema': 1, 'metrics_identity': ruby.identity,
        'fonts': fonts, 'runs': [*main.runs, *ruby.measured.runs], 'lines': lines}
    apply_resolved_font_faces(ir)
    return ir


def test_reference_batch_uses_metadata_cap_height_and_stable_ink(binary, source, monkeypatch, tmp_path):
    from krok_helper.subtitle_render.engine.ruby import style as qt_style
    from krok_helper.subtitle_render.engine.render.elements.horizontal import layout
    fail = lambda *a, **k: pytest.fail('Qt font/reference measurement invoked')
    monkeypatch.setattr(qt_style, 'build_ruby_font_for_text', fail)
    monkeypatch.setattr(layout, 'QFontMetrics', fail)
    track, style, resolve = source
    batches = []
    with SkiaTextMetricsProcess(binary) as process:
        def measure(fonts, runs):
            batches.append(runs)
            return process.measure(fonts, runs)
        refs = measure_references([(style, lower_style(style))]*3, resolve_font=resolve, measure=measure)
    assert len(batches) == 1 and len(batches[0]) == 3
    assert sum(len(r['cells']) for r in batches[0]) == 3
    assert refs[0] == refs[1] == refs[2]
    assert refs[0].lower_metrics['cap_height'] == pytest.approx(21, abs=1e-4)
    assert refs[0].lower_baseline(6) == pytest.approx(37, abs=1e-4)
    (tmp_path/'lower-ruby-reference.json').write_text(json.dumps(asdict(refs[0]), sort_keys=True), encoding='utf-8')


def test_reference_cap_height_comes_from_font_metadata_not_glyph_ink(binary, source, tmp_path):
    from fontTools.ttLib import TTFont
    _, style, resolve = source
    path = tmp_path/'cap-metadata.ttf'
    font = TTFont(resolve(replace(style, font_family='Latin'))[0]['path'], recalcTimestamp=False)
    font['OS/2'].sCapHeight = 600  # H outline stays 700 units high.
    font.save(path); font.close()
    def changed(appearance):
        return ({'path': str(path)}, {}) if appearance.font_family == 'Latin' else resolve(appearance)
    with SkiaTextMetricsProcess(binary) as metrics:
        reference, = measure_references([(style, lower_style(style))], resolve_font=changed, measure=metrics.measure)
    assert reference.lower_metrics['cap_height'] == pytest.approx(18, abs=1e-4)
    assert reference.lower_ink[1] == pytest.approx(-21, abs=1e-4)
    assert reference.lower_baseline(6) == pytest.approx(34, abs=1e-4)


@pytest.mark.parametrize('mutation', ['missing_cap', 'nan_cap', 'negative_cap', 'zero_cap', 'nan_ink', 'missing_outline'])
def test_invalid_reference_metrics_are_explicit(binary, source, mutation):
    _, style, resolve = source
    with SkiaTextMetricsProcess(binary) as process:
        def measure(fonts, runs):
            result = process.measure(fonts, runs)
            for run in result:
                if mutation == 'missing_cap': run['font_metrics'].pop('cap_height')
                elif mutation == 'nan_cap': run['font_metrics']['cap_height'] = float('nan')
                elif mutation == 'negative_cap': run['font_metrics']['cap_height'] = -1
                elif mutation == 'zero_cap': run['font_metrics']['cap_height'] = 0
                elif run['cells'] and mutation == 'nan_ink': run['cells'][0]['ink'][1] = float('nan')
                elif run['cells'] and mutation == 'missing_outline': run['cells'][0]['outline_available'] = False
            return result
        with pytest.raises(ValueError, match='大写高度|非有限|轮廓'):
            measure_references([(style, lower_style(style))], resolve_font=resolve, measure=measure)


def test_lower_geometry_keeps_main_clock_and_centers_main_cells(binary, source, tmp_path):
    value = prepared(binary, source)
    line = value.plan.lines[0]
    local = value.geometry.ruby.lines[0]
    lower = [g for g in local.rubies if g.target.lower]
    assert len(lower) == 3 and all(g.baseline_offset == 37 for g in lower)
    assert any(local.gaps)
    timing = prepare_ruby_wipe_timing(line.line, local, line.layout_style.ruby_main_progress_mode)
    upper_only = replace(local, rubies=tuple(g for g in local.rubies if not g.target.lower))
    assert timing.main == prepare_ruby_wipe_timing(line.line, upper_only, line.layout_style.ruby_main_progress_mode).main
    assert local.intervals == line.resolved_intervals
    assert value.measured.lines[0].row_frame.reference_identity
    # The shared policy widens a reading slot on both sides of its main cells.
    for group in lower:
        center = (min(c['layout_left'] for c in group.cells)+max(c['layout_right'] for c in group.cells))/2
        assert abs(center-sum(group.span)/2) <= .5
    position = value.geometry.position(line, 900, 600)
    collision = value.geometry.geometry(line, 900, 600)
    lower_bottom = max(c['baseline_y']+c['metrics']['ink'][3] for g in lower for c in g.cells)+position.baseline_y
    assert collision.axis_max >= lower_bottom
    payload = {'row': asdict(value.measured.lines[0].row_frame),
        'gaps': local.gaps, 'anchor': local.anchor,
        'groups': [{'lower': g.target.lower, 'span': g.span, 'baseline': g.baseline_offset,
                    'cells': g.cells} for g in local.rubies]}
    payload['overlap'] = {str(dpr): prepare_overlap_wipes(local, value.measured.lines[0], timing, dpr=dpr)
                          for dpr in [.75, 1, 1.5, 2]}
    (tmp_path/'lower-ruby-geometry.json').write_text(json.dumps(payload, sort_keys=True), encoding='utf-8')


def test_lower_role_reference_and_invisible_peer_rows_are_stable(binary, source):
    track, style, resolve = source
    role = SubtitleStyleScheme(font_size_px=80, ruby_font_size_px=32, ruby_latin_font_size_px=40)
    lead = replace(track.lines[0], chars=[replace(c, role_label='lead') for c in track.lines[0].chars],
                   track_line_index=1, display_start_override_ms=5000)
    style = replace(style, custom_style_schemes={'lead': role})
    combined = prepared(binary, (replace(track, lines=[track.lines[0], lead]), style, resolve))
    assert combined.measured.lines[0].row_frame == combined.measured.lines[1].row_frame
    offsets = [[g.baseline_offset for g in l.rubies if g.target.lower] for l in combined.geometry.ruby.lines]
    assert offsets[0] == [37]*3
    assert all(v == 48 for v in offsets[1])
    original = prepared(binary, source)
    assert original.measured.identity != combined.measured.identity


@pytest.mark.parametrize('semantics', ['legacy', 'n3_1074'])
def test_lower_geometry_probe_gpu_raster_seek_and_pause(binary, source, semantics):
    track, style, resolve = source
    source = track, replace(style, layout_semantics=semantics), resolve
    value = prepared(binary, source)
    ir = probe_ir(source, value)
    outputs = []
    for target in ['gpu', 'raster']:
        with SkiaRendererProcess(binary, selection=selection(target)) as process:
            configure(process, ir)
            frames, _ = collect(process, [100, 350, 400, 450, 800, 1900, 400])
        assert np.array_equal(frames[2], frames[-1])
        assert np.array_equal(frames[1], frames[2])
        outputs.append(frames)
    for a, b in zip(*outputs):
        def bounds(p):
            ys, xs = np.where(p[..., 3] > 127)
            return np.array([xs.min(), ys.min(), xs.max(), ys.max()])
        assert np.max(np.abs(bounds(a)-bounds(b))) <= 1


def test_lower_final_compiler_requires_source_mapping(binary, source):
    with pytest.raises(ValueError, match='完整来源映射'):
        prepared_ruby_layout(prepared(binary, source), width=900, height=600)


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual Direct2D GPU reference')
@pytest.mark.parametrize('semantics', ['legacy', 'n3_1074'])
@pytest.mark.parametrize('independent', [False, True])
@pytest.mark.parametrize('dpr', [1, 1.5])
def test_lower_geometry_matches_actual_d2d_gpu(binary, source, semantics, independent, dpr):
    from test_subtitle_render_gpu_backend import _render_g1_frames
    track, style, resolve = source
    style = replace(style, layout_semantics=semantics, lower_ruby_follow_upper=not independent,
        lower_ruby_style={'ruby_latin_font_size_px': 40} if independent else {})
    source = track, style, resolve
    times = [199, 200, 350, 400, 450, 699, 800, 1200, 1900]
    with NativeRendererProcess() as process:
        _, raw = _render_g1_frames(process, style, tuple(times), force_warp=False, track=track, width=900, height=600, dpr=dpr)
    with SkiaRendererProcess(binary, selection=selection('gpu')) as process:
        configure(process, probe_ir(source, prepared(binary, source), dpr=dpr))
        frames, _ = collect(process, times)
    references = [np.frombuffer(a, np.uint8).reshape(round(600*dpr), round(900*dpr), 4) for a in raw]
    def boxes(p):
        mask = p[..., 3] > 127
        ys = np.flatnonzero(mask.any(axis=1))
        bands = [v for v in np.split(ys, np.flatnonzero(np.diff(ys)>1)+1) if len(v)]
        return [np.array([np.flatnonzero(mask[b[0]:b[-1]+1].any(axis=0)).min(), b[0],
                          np.flatnonzero(mask[b[0]:b[-1]+1].any(axis=0)).max(), b[-1]]) for b in bands]
    templates = boxes(references[-1])
    assert len(templates) == 3  # Separate upper, main and lower ink, not one union.
    for t, a, b in zip(times, references, frames, strict=True):
        left, right = boxes(a), boxes(b)
        assert len(left) == len(right) == 3
        assert np.max(np.abs(np.array(left)-right)) <= 1, (t, left, right)
        for template in templates:
            lo, hi = max(template[1]-2, 0), template[3]+3
            def coverage(p):
                return (p[lo:hi, :, 1].astype(float)*p[lo:hi, :, 3]/255**2).sum(axis=0)
            ca, cb = coverage(a), coverage(b)
            full_a, full_b = coverage(references[-1]).sum(), coverage(frames[-1]).sum()
            # Controlled fonts have flat rectangular ink and one active front
            # per band. Premultiplied sung coverage gives its subpixel travel;
            # a thresholded color bbox can jump across a glyph gap when only
            # one partially covered unsung pixel remains at t=end-1ms.
            height = template[3]-template[1]+1
            width = (full_a+full_b)/(2*height)
            assert abs(ca.sum()/full_a-cb.sum()/full_b)*width <= 1, (t, template, ca.sum(), cb.sum())
            assert (ca.sum() == 0) == (cb.sum() == 0), (t, template)
