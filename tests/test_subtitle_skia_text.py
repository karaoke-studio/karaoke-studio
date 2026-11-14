"""P05 font resources and GPU-independent Skia/HarfBuzz metrics."""
import math
import os
from pathlib import Path
import shutil
import sys

import pytest

from krok_helper.subtitle_render.native.backend import NativeRendererError
from krok_helper.subtitle_render.native.skia_text import SkiaTextMetricsProcess

_TEXT_APPLICATION = None


@pytest.fixture(scope='module', autouse=True)
def application():
    global _TEXT_APPLICATION
    from PyQt6.QtWidgets import QApplication
    _TEXT_APPLICATION = QApplication.instance() or QApplication([])
    return _TEXT_APPLICATION


@pytest.fixture
def binary():
    path = os.environ.get("KROK_TEST_SKIA_SIDECAR")
    if not path: pytest.skip("Set KROK_TEST_SKIA_SIDECAR to the P05 sidecar")
    return path


@pytest.fixture
def font():
    candidates = [os.environ.get("KROK_SKIA_NATIVE_TEST_FONT", ""),
                  "C:/Windows/Fonts/arial.ttf", "/System/Library/Fonts/Supplemental/Arial.ttf",
                  "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]
    return next(Path(p) for p in candidates if p and Path(p).is_file())


def run(process, path, cells, **options):
    return process.measure({"main": {"path": str(path)}},
        [{"font_ids": ["main"], "size": 48, "cells": cells, **options}])[0]["cells"]


def test_measurement_never_initializes_gpu(binary, font, monkeypatch, tmp_path):
    monkeypatch.setenv("KROK_SKIA_VULKAN_LIBRARY", str(tmp_path / "absent"))
    monkeypatch.setenv("KROK_SUBTITLE_SKIA_TARGET", "invalid-render-target")
    with SkiaTextMetricsProcess(binary) as process:
        cells = run(process, font, ["A", "ffi", "a\u0301", " "])
        assert cells[0]["advance"] > 0 and cells[0]["ascent"] > 0
        assert not cells[3]["ink"][2] and cells[3]["advance"] > 0
        combining = cells[2]["glyphs"]
        assert combining and {g["cluster_start"] for g in combining} == {0}
        assert {g["cluster_end"] for g in combining} == {len("a\u0301".encode())}
        assert all(math.isfinite(c[key]) for c in cells for key in ("advance", "ascent", "descent"))
        # Batch/cell order, repeated calls and previous requests cannot change a snapshot.
        assert run(process, font, ["a\u0301", "A"]) == [cells[2], cells[0]]


def test_font_content_identity_survives_rename_and_invalidates_replacement(binary, font, tmp_path):
    copy = tmp_path / "字体-🎵.otf"
    shutil.copyfile(font, copy)
    with SkiaTextMetricsProcess(binary) as process:
        a = run(process, font, ["Test"])[0]
        b = run(process, copy, ["Test"])[0]
        assert a == b
        # Updating an ignored table still changes resource identity, even at the same path.
        from fontTools.ttLib import TTFont
        modified = TTFont(copy)
        modified['head'].fontRevision += .125
        modified.save(copy)
        c = run(process, copy, ["Test"])[0]
        assert c['font_identity'] != b['font_identity']
        assert c['glyphs'] == b['glyphs'] and c['advance'] == b['advance']


def test_simulations_and_horizontal_scale_are_explicit_instances(binary, font):
    with SkiaTextMetricsProcess(binary) as process:
        plain = run(process, font, ["f"])[0]
        wide = run(process, font, ["f"], scale_x=1.5)[0]
        bold = run(process, font, ["f"], synthetic_bold=True)[0]
        italic = run(process, font, ["f"], synthetic_italic=True)[0]
        assert wide['advance'] == pytest.approx(plain['advance'] * 1.5, abs=.01)
        assert len({x['font_identity'] for x in (plain, wide, bold, italic)}) == 4
        assert bold['ink'] != plain['ink'] and italic['ink'] != plain['ink']


@pytest.mark.parametrize("change", ["hash", "face", "axis", "nan_axis", "size", "unknown_font", "control"])
def test_invalid_resources_and_requests_fail_without_poisoning_service(binary, font, change):
    fonts = {"main": {"path": str(font)}}
    request = {"font_ids": ["main"], "size": 48, "cells": ["A"]}
    if change == "hash": fonts['main']['sha256'] = '0' * 64
    elif change == "face": fonts['main']['face_index'] = 1000
    elif change == "axis": fonts['main']['axes'] = {'NOPE': 1}
    elif change == "nan_axis": fonts['main']['axes'] = {'wght': float('nan')}
    elif change == "size": request['size'] = -1
    elif change == "unknown_font": request['font_ids'] = ['missing']
    elif change == "control": request['cells'] = ['A\nB']
    with SkiaTextMetricsProcess(binary) as process:
        with pytest.raises(NativeRendererError): process.measure(fonts, [request])
        assert run(process, font, ["A"])[0]['advance'] > 0


def test_missing_glyph_does_not_use_an_unreported_system_font(binary, font):
    with SkiaTextMetricsProcess(binary) as process:
        with pytest.raises(NativeRendererError): run(process, font, ["\U0010ffff"])


def test_variable_font_axes_are_applied_and_invalidate_metrics(binary):
    from fontTools.ttLib import TTFont
    candidates = [Path("C:/Windows/Fonts/SegUIVar.ttf"), Path("C:/Windows/Fonts/segoeui.ttf")]
    path = next((p for p in candidates if p.is_file() and 'fvar' in TTFont(p)), None)
    if path is None: pytest.skip("No local variable font fixture")
    with SkiaTextMetricsProcess(binary) as process:
        output = []
        for weight in (300, 700):
            output.append(process.measure({'main': {'path': str(path), 'axes': {'wght': weight}}},
                [{'font_ids': ['main'], 'size': 48, 'cells': ['Weight']}])[0]['cells'][0])
        assert output[0]['font_identity'] != output[1]['font_identity']
        assert output[0]['ink'] != output[1]['ink']


def test_explicit_cjk_korean_emoji_fallback_resources(binary, font):
    fixtures = [("字幕かな", Path("C:/Windows/Fonts/meiryo.ttc")),
                ("한글", Path("C:/Windows/Fonts/malgun.ttf")),
                ("👩\u200d💻", Path("C:/Windows/Fonts/seguiemj.ttf"))]
    if not all(p.is_file() for _, p in fixtures): pytest.skip("Windows installed multilingual fixtures")
    with SkiaTextMetricsProcess(binary) as process:
        for text, fallback in fixtures:
            cells = process.measure({'main': {'path': str(font)}, 'fallback': {'path': str(fallback)}},
                [{'font_ids': ['main', 'fallback'], 'size': 48, 'cells': [text]}])[0]['cells']
            cell = cells[0]
            assert cell['selected_font'] == 1 and cell['advance'] > 0
            assert all(0 <= g['cluster_start'] < g['cluster_end'] <= len(text.encode()) for g in cell['glyphs'])


def test_native_snapshot_drives_semantic_plan_without_qt_widths(binary, font, monkeypatch):
    from dataclasses import replace
    from krok_helper.subtitle_render.domain.models import Style, TimingChar, TimingLine, TimingTrack
    from krok_helper.subtitle_render.engine.text.metric_snapshot import CellMetric, LineMetric, TextMetricSnapshot
    from krok_helper.subtitle_render.engine.layout.plan.orchestrator import LayoutPlanResolvers, resolve_track_layout_plan
    from krok_helper.subtitle_render.engine.layout.line import qt_geometry
    style = Style(dual_line_layout=False, entry_anim='none', exit_anim='none', karaoke_anim='none')
    line = TimingLine(chars=[TimingChar(c, 100, source_span_start_ms=100, source_span_end_ms=900,
                                      source_span_index=i, source_span_count=2) for i, c in enumerate('Wi')], end_ms=900)
    with SkiaTextMetricsProcess(binary) as process:
        payloads = run(process, font, ['W', 'i'])
    cells = tuple(CellMetric.from_payload(c.text, p) for c, p in zip(line.chars, payloads))
    measured = LineMetric.capture(line, style, cells, layout_widths=[math.ceil(c.advance) for c in cells])
    snapshot = TextMetricSnapshot((measured,))
    monkeypatch.setattr(qt_geometry, 'resolved_char_intervals_for_line', lambda *a: pytest.fail('implicit Qt metrics'))
    display = lambda *a, **kw: []
    offsets = lambda *a: {}
    ports = LayoutPlanResolvers(display_lines=display, page_offset_windows=offsets,
                                char_intervals=snapshot.resolve_intervals, metrics_identity=snapshot.identity)
    track = TimingTrack(lines=[line])
    plan = resolve_track_layout_plan(track, style, ports)
    assert plan.lines[0].resolved_intervals == tuple(snapshot.resolve_intervals(line, style))
    assert plan.lines[0].resolved_intervals[0][1] > 500
    newer = TextMetricSnapshot((replace(measured, layout_widths=(1, 100)),))
    plan2 = resolve_track_layout_plan(track, style, replace(ports, char_intervals=newer.resolve_intervals,
                                                         metrics_identity=newer.identity))
    assert plan2.lines[0].resolved_intervals != plan.lines[0].resolved_intervals
    with pytest.raises(ValueError, match='快照'):
        snapshot.resolve_intervals(line, replace(style, font_weight=700))


def test_portable_font_has_exact_metrics_and_cell_cluster_boundaries(binary, tmp_path):
    import hashlib
    import json
    from skia_text_fixture import make_font
    path = make_font(tmp_path / 'canonical.ttf')
    duplicate = make_font(tmp_path / 'canonical-copy.ttf')
    assert path.read_bytes() == duplicate.read_bytes()
    with SkiaTextMetricsProcess(binary) as process:
        cells = run(process, path, ['A', '字', '한', 'a\u0301', 'fi', 'f', 'i'], size=50)
        # FreeType's normalized fixed-point scale may round by < 1e-4 pixel.
        assert [cells[0][k] for k in ('advance', 'ascent', 'descent')] == pytest.approx([30, 40, 10], abs=1e-4)
        # SkFont paths normalize at 64px and FreeType uses 26.6 coordinates.
        # The returned bounds describe that actual path, within one quantum.
        assert cells[0]['ink'] == pytest.approx([2.5, -35, 27.5, 0], abs=50 / (64 * 64) + 1e-4)
        assert [cells[1]['advance'], cells[2]['advance']] == pytest.approx([50, 50], abs=1e-4)
        assert len(cells[4]['glyphs']) == 1 and cells[4]['glyphs'][0]['cluster_end'] == 2
        assert len(cells[5]['glyphs']) + len(cells[6]['glyphs']) == 2
    (tmp_path / 'canonical-metrics.json').write_text(json.dumps({'font_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                                                              'cells': cells}, sort_keys=True), encoding='utf-8')


def test_portable_variable_font_interpolates_and_rejects_out_of_range_axes(binary, tmp_path):
    from skia_text_fixture import make_variable_font
    font = make_variable_font(tmp_path)
    with SkiaTextMetricsProcess(binary) as process:
        result = []
        for weight in (100, 500, 900):
            cells = process.measure({'font': {'path': str(font), 'axes': {'wght': weight}}},
                                    [{'font_ids': ['font'], 'size': 50, 'cells': ['A']}])[0]['cells']
            result.append(cells[0])
        assert [cell['advance'] for cell in result] == pytest.approx([30, 40, 50], abs=1e-4)
        assert len({cell['font_identity'] for cell in result}) == 3
        with pytest.raises(NativeRendererError):
            process.measure({'font': {'path': str(font), 'axes': {'wght': 901}}},
                            [{'font_ids': ['font'], 'size': 50, 'cells': ['A']}])


def test_shaping_checks_composed_glyph_coverage_before_choosing_fallback(binary, tmp_path):
    from skia_text_fixture import make_font
    from fontTools.ttLib import TTFont
    path = make_font(tmp_path / 'composed.ttf')
    font = TTFont(path)
    for cmap in font['cmap'].tables: cmap.cmap.pop(0x301, None)
    font.save(path)
    with SkiaTextMetricsProcess(binary) as process:
        composed = run(process, path, ['á', 'a\u0301'])
    assert composed[0]['advance'] == composed[1]['advance']
    assert composed[0]['glyphs'][0]['id'] == composed[1]['glyphs'][0]['id']
    assert composed[1]['glyphs'][0]['cluster_end'] == len('a\u0301'.encode())


def test_registered_unicode_family_exports_physical_snapshot_and_invalidates_on_registry_change(binary, tmp_path):
    from PyQt6.QtWidgets import QApplication
    from PyQt6.QtGui import QFontDatabase
    from skia_text_fixture import make_font
    from krok_helper.subtitle_render.engine.text.font_weight import clear_font_weight_cache, resolve_font_instance
    app = QApplication.instance() or QApplication([])
    path = make_font(tmp_path / 'application-only.ttf')
    previous = None
    for width in (600, 800):
        make_font(path, a_width=width)
        font_id = QFontDatabase.addApplicationFont(str(path))
        assert font_id >= 0
        try:
            clear_font_weight_cache()
            family = QFontDatabase.applicationFontFamilies(font_id)[0]
            assert 'P05' in family
            instance = resolve_font_instance(family, 400)
            assert instance.resource_path
            with SkiaTextMetricsProcess(binary) as process:
                cell = run(process, Path(instance.resource_path), ['A'], size=50)[0]
            assert cell['advance'] == pytest.approx(width / 20, abs=1e-4)
            if previous is not None: assert cell['font_identity'] != previous
            previous = cell['font_identity']
        finally:
            QFontDatabase.removeApplicationFont(font_id)
            clear_font_weight_cache()


def test_cpu_only_and_gpu_builds_produce_identical_font_snapshots(binary, font):
    directory = Path(binary).parent
    cpu = directory.parent / (directory.name.rsplit('-', 1)[0] + '-raster') / Path(binary).name
    if not cpu.is_file(): pytest.skip('Build the CPU-only sidecar')
    results = []
    for path in (binary, cpu):
        with SkiaTextMetricsProcess(path) as process:
            results.append(run(process, font, ['A', 'fi', 'a\u0301']))
    assert results[0] == results[1]


@pytest.mark.parametrize('value', [float('nan'), float('inf'), float('-inf')])
def test_numeric_sample_comparison_rejects_nonfinite_values(value):
    from scripts.compare_skia_text_metrics import compare
    with pytest.raises(ValueError, match='non-finite'): compare({'advance': value}, {'advance': value})


def test_sample_comparison_keeps_resource_and_cluster_identity_exact():
    from scripts.compare_skia_text_metrics import compare
    assert compare({'advance': 20}, {'advance': 20.00001}) < 1e-4
    for a, b in (({'id': 1}, {'id': 2}), ({'cluster_end': 1}, {'cluster_end': 2}),
                 ({'font_identity': 'old'}, {'font_identity': 'new'})):
        with pytest.raises(ValueError): compare(a, b)
