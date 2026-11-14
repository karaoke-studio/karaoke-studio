"""P05 final IR: real glyph drawing, timing authority and stale-input rejection."""
import copy
from dataclasses import replace
import json
from pathlib import Path
import sys

import numpy as np
import pytest
from PyQt6.QtGui import QFontDatabase

from test_subtitle_skia_protocol import application, binary, collect, selection, sample
from skia_text_fixture import make_font
from krok_helper.subtitle_render.domain.models import TimingChar, TimingLine, TimingTrack
from krok_helper.subtitle_render.engine.render.skia_ir import build_skia_render_ir
from krok_helper.subtitle_render.engine.text.font_weight import clear_font_weight_cache
from krok_helper.subtitle_render.native.backend import NativeRendererError, NativeRendererProcess
from krok_helper.subtitle_render.native.protocol import apply_resolved_font_faces
from krok_helper.subtitle_render.native.skia_backend import SkiaRendererProcess
from krok_helper.subtitle_render.native.skia_fonts import resolve_skia_font
from krok_helper.subtitle_render.native.skia_text import SkiaTextMetricsProcess


@pytest.fixture
def project(tmp_path, application):
    path = make_font(tmp_path / 'canonical.ttf')
    registered = QFontDatabase.addApplicationFont(str(path))
    assert registered >= 0
    clear_font_weight_cache()
    try:
        family = QFontDatabase.applicationFontFamilies(registered)[0]
        style = replace(sample()[1], font_family=family, font_size_px=50, letter_spacing_px=3)
        texts = ['A', '字', '한', 'a\u0301', 'fi', ' ', 'f']
        track = TimingTrack(lines=[TimingLine(chars=[TimingChar(t, 200 + i * 200) for i, t in enumerate(texts)], end_ms=1800)])
        yield track, style
    finally:
        QFontDatabase.removeApplicationFont(registered)
        clear_font_weight_cache()


def ir_for(binary, project, **kwargs):
    with SkiaTextMetricsProcess(binary) as metrics:
        ir = build_skia_render_ir(*project, measure=metrics.measure, resolve_font=resolve_skia_font,
                                  width=640, height=360, fps=60, **kwargs)
    apply_resolved_font_faces(ir)
    return ir


@pytest.mark.parametrize('target', ['gpu', 'raster'])
def test_canonical_ir_draws_shaped_cells_and_keeps_seek_stable(binary, project, target, monkeypatch, tmp_path):
    from krok_helper.subtitle_render.engine.render import render_ir
    from krok_helper.subtitle_render.engine.layout.line import qt_geometry
    fail = lambda *a, **k: pytest.fail('Qt-backed layout was called')
    monkeypatch.setattr(render_ir, 'build_track_layout_plan', fail)
    monkeypatch.setattr(qt_geometry, 'resolved_char_intervals_for_line', fail)
    with SkiaRendererProcess(binary, selection=selection(target)) as process:
        configured = process.configure(*project, width=640, height=360, fps=60)
        assert configured['backend_info']['profile'] == 'p05_resolved_horizontal'
        frames, _ = collect(process, [100, 800, 1900, 800])
        assert np.array_equal(frames[1], frames[3])
        assert not np.array_equal(frames[1], frames[2])
        assert frames[1][..., 3].max() == 255
    ir = ir_for(binary, project)
    cells = ir['skia_layout']['lines'][0]['cells']
    assert len(cells[4]['metrics']['glyphs']) == 1  # fi remains one authored timing unit.
    assert cells[3]['metrics']['glyphs'][0]['cluster_end'] == 3
    if target == 'gpu':
        (tmp_path / 'resolved-layout.json').write_text(json.dumps({'cells': cells,
            'intervals': ir['track']['lines'][0]['resolved_intervals']}, sort_keys=True), encoding='utf-8')


def test_canonical_metrics_allocate_shared_timing_and_emit_final_geometry(binary, project):
    track, style = project
    chars = [TimingChar(t, 200, source_span_start_ms=200, source_span_end_ms=1400,
                        source_span_index=i, source_span_count=3) for i, t in enumerate(['A', 'i', '字'])]
    track = replace(track, lines=[TimingLine(chars=chars, end_ms=1400)])
    ir = ir_for(binary, (track, style))
    intervals = ir['track']['lines'][0]['resolved_intervals']
    assert intervals[0][0] == 200 and intervals[-1][1] == 1400
    assert intervals[0][1] - intervals[0][0] > intervals[1][1] - intervals[1][0]
    # Timing must use integer cell widths, not raw font advances.
    cells = ir['skia_layout']['lines'][0]['cells']
    from krok_helper.subtitle_render.engine.text.skia_width import cell_geometry
    widths = [cell_geometry(c.text, m['metrics'], style)[0] for c, m in zip(chars, cells)]
    from krok_helper.subtitle_render.engine.timing.timeline import compute_char_intervals
    assert intervals == [list(v) for v in compute_char_intervals(track.lines[0], widths)]
    assert [m['metrics']['advance'] for m in cells] != widths


@pytest.mark.parametrize('mutation', ['font_hash', 'glyph', 'cluster', 'advance', 'nan', 'interval', 'missing_layout'])
def test_stale_final_ir_rejected_transactionally(binary, project, mutation):
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        process.configure(*project, width=640, height=360, fps=60)
        before, _ = collect(process, [800])
        ir = ir_for(binary, project)
        cell = ir['skia_layout']['lines'][0]['cells'][0]
        if mutation == 'font_hash': next(iter(ir['skia_layout']['fonts'].values()))['sha256'] = '0' * 64
        elif mutation == 'glyph': cell['metrics']['glyphs'][0]['id'] += 1
        elif mutation == 'cluster': cell['metrics']['glyphs'][0]['cluster_end'] += 1
        elif mutation == 'advance': cell['metrics']['advance'] += 1
        elif mutation == 'nan': cell['origin_x'] = float('nan')
        elif mutation == 'interval': ir['track']['lines'][0]['resolved_intervals'][0] = [300, 200]
        elif mutation == 'missing_layout': del ir['skia_layout']
        process._send({'cmd': 'configure', 'ir': ir})
        with pytest.raises(NativeRendererError): process._expect_ok(process._read_until_event('configured'))
        after, _ = collect(process, [800], generation=2)
        assert np.array_equal(before[0], after[0])


@pytest.mark.parametrize('dpr', [1, 1.5, 2])
def test_same_final_ir_gpu_raster_and_dpr(binary, project, dpr):
    output = []
    for target in ('gpu', 'raster'):
        with SkiaRendererProcess(binary, selection=selection(target)) as process:
            process.configure(*project, width=640, height=360, fps=60, dpr=dpr)
            frames, _ = collect(process, [800])
            assert frames[0].shape == (int(360*dpr), int(640*dpr), 4)
            pixels = frames[0].astype(float)
            pixels[..., :3] *= pixels[..., 3:] / 255
            output.append(pixels)
    delta = np.abs(output[0] - output[1])
    assert delta.mean() < .15
    assert np.mean(np.max(delta, axis=-1) > 16) < .003


@pytest.mark.parametrize('blank_line', [False, True])
def test_empty_tracks_and_blank_lines_are_valid_transparent_scenes(binary, project, blank_line):
    track, style = project
    track = replace(track, lines=[TimingLine(chars=[], end_ms=0)] if blank_line else [])
    with SkiaRendererProcess(binary, selection=selection('raster')) as process:
        process.configure(track, style, width=640, height=360, fps=60)
        frames, _ = collect(process, [800])
    assert not frames[0].any()


def test_final_timing_preserves_source_style_and_roll_offsets(binary, project):
    track, style = project
    outputs = []
    for offset in (False, True):
        source = replace(track, meta=replace(track.meta, offset_ms=137)) if offset else track
        appearance = replace(style, timing_offset_ms=229) if offset else style
        with SkiaRendererProcess(binary, selection=selection('raster')) as process:
            process.configure(source, appearance, width=640, height=360, fps=60, roll_shift_ms=400 if offset else 0)
            frames, _ = collect(process, [t + (766 if offset else 0) for t in (800, 1900)])
            outputs.append(frames)
    assert all(np.array_equal(a,b) for a,b in zip(*outputs))


@pytest.mark.skipif(sys.platform != 'win32', reason='Direct2D GPU comparison')
@pytest.mark.parametrize('font_kind', ['portable', 'system'])
def test_static_font_geometry_matches_existing_d2d_gpu(binary, project, font_kind):
    from test_subtitle_render_gpu_backend import _render_g1_frames
    track, style = project
    # Plain individual glyphs are common to the old D2D path and HarfBuzz.
    texts = ['A', '字', '한'] if font_kind == 'portable' else ['A', 'W', 'i']
    if font_kind == 'system': style = replace(style, font_family='Arial')
    track = replace(track, lines=[TimingLine(chars=[TimingChar(t, 200+i*200) for i,t in enumerate(texts)], end_ms=1800)])
    with NativeRendererProcess() as d2d:
        _, expected = _render_g1_frames(d2d, style, (1900,), force_warp=False, track=track)
    with SkiaRendererProcess(binary, selection=selection('gpu')) as process:
        process.configure(track, style, width=640, height=360, fps=60)
        frames, _ = collect(process, [1900])
    reference = np.frombuffer(expected[0], np.uint8).reshape(360,640,4)
    def bounds(pixels):
        y,x = np.where(pixels[...,3] > 127)
        return np.array([x.min(), y.min(), x.max(), y.max()])
    assert np.max(np.abs(bounds(reference) - bounds(frames[0]))) <= 1
    union = np.maximum(reference[...,3],frames[0][...,3]).sum()
    assert np.minimum(reference[...,3],frames[0][...,3]).sum()/union > .95
