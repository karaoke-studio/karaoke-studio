"""Analytic outline checks: overlaps are ink, counters are empty at every axis value."""
from __future__ import annotations

import os

import numpy as np
import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFontDatabase, QImage, QPainter, QPainterPath


@pytest.fixture
def overlap_variable_font(qapp, tmp_path):
    from fontTools.fontBuilder import FontBuilder
    from fontTools.pens.ttGlyphPen import TTGlyphPen
    from fontTools.ttLib import newTable
    from fontTools.ttLib.tables.TupleVariation import TupleVariation

    family = "LinK Overlap VF"
    pen = TTGlyphPen(None)
    # Clockwise outer ring, counterclockwise counter, clockwise crossing stroke.
    for points in (
        [(100, 100), (100, 800), (700, 800), (700, 100)],
        [(200, 200), (500, 200), (500, 700), (200, 700)],
        [(600, 300), (600, 500), (900, 500), (900, 300)],
    ):
        pen.moveTo(points[0])
        for point in points[1:]:
            pen.lineTo(point)
        pen.closePath()
    builder = FontBuilder(1000, isTTF=True)
    builder.setupGlyphOrder([".notdef", "A"])
    builder.setupCharacterMap({ord("A"): "A"})
    builder.setupGlyf({".notdef": TTGlyphPen(None).glyph(), "A": pen.glyph()})
    builder.setupHorizontalMetrics({".notdef": (1000, 0), "A": (1000, 100)})
    builder.setupHorizontalHeader(ascent=900, descent=-100)
    builder.setupNameTable({"familyName": family, "styleName": "Regular",
                           "uniqueFontIdentifier": family, "fullName": family,
                           "psName": "LinKOverlapVF"})
    builder.setupOS2(sTypoAscender=900, sTypoDescender=-100,
                    usWinAscent=900, usWinDescent=100)
    builder.setupPost()
    builder.setupFvar([("wght", 400, 400, 900, "Weight")], [])
    variations = newTable("gvar")
    # Grow only the crossing stroke: bounds/advance remain constant.
    deltas = [(0, 0)] * 8 + [(0, -100), (0, 100), (0, 100), (0, -100)]
    variations.variations = {".notdef": [], "A": [
        TupleVariation({"wght": (0, 1, 1)}, deltas + [(0, 0)] * 4)
    ]}
    builder.font["gvar"] = variations
    path = tmp_path / "overlap.ttf"
    builder.save(path)
    font_id = QFontDatabase.addApplicationFont(str(path))
    assert font_id >= 0
    assert family in QFontDatabase.applicationFontFamilies(font_id)
    try:
        yield family
    finally:
        QFontDatabase.removeApplicationFont(font_id)


def _assert_outline_pixels(image, weight):
    rgba = image.convertToFormat(QImage.Format.Format_RGBA8888)
    pixels = np.frombuffer(rgba.constBits().asstring(rgba.sizeInBytes()), np.uint8)
    alpha = pixels.reshape(rgba.height(), rgba.bytesPerLine())[:, :rgba.width()*4]
    alpha = alpha.reshape(rgba.height(), rgba.width(), 4)[:, :, 3]
    ys, xs = np.where(alpha > 127)
    assert len(xs), "Font produced no visible outline"
    left, right, top, bottom = xs.min(), xs.max(), ys.min(), ys.max()

    def at(x, y):
        px = round(left + (x - 100) / 800 * (right - left))
        py = round(top + (800 - y) / 700 * (bottom - top))
        return int(alpha[py, px])

    assert at(650, 400) == 255, "Overlapping strokes became a hole"
    assert at(350, 400) == 0, "A true counter was filled"
    assert at(850, 400) == 255, "Crossing stroke is missing"
    # Interior points avoid antialiasing; test intermediate-axis interpolation too.
    for y in (525, 575):
        expected = 255 if y < 500 + (weight - 400) / 5 else 0
        assert at(850, y) == expected, (weight, y, at(850, y))


@pytest.mark.parametrize("weight", [400, 650, 900])
def test_variable_outline_pixels_cpu(overlap_variable_font, weight):
    from krok_helper.subtitle_render.engine.text.font_weight import (
        build_weight_font, winding_glyph_path,
    )
    font = build_weight_font(overlap_variable_font, 300, weight)
    path = QPainterPath()
    path.addText(0, 280, font, "A")
    image = QImage(340, 320, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.fillPath(winding_glyph_path(path), Qt.GlobalColor.white)
    painter.end()
    _assert_outline_pixels(image, weight)


@pytest.mark.skipif(os.name != "nt", reason="DirectWrite requires Windows")
@pytest.mark.parametrize("force_warp", [False, True])
def test_variable_outline_pixels_gpu(overlap_variable_font, force_warp):
    from krok_helper.subtitle_render.domain.models import Style
    from krok_helper.subtitle_render.domain.timing import TimingChar, TimingLine, TimingTrack
    from krok_helper.subtitle_render.native.backend import (
        NativeRendererProcess, SharedFrameRingReader, resolve_native_renderer_path,
    )
    from krok_helper.subtitle_render.engine.render.render_ir import build_style_patch_ir
    if resolve_native_renderer_path() is None:
        pytest.skip("Native renderer is not built")
    track = TimingTrack(lines=[TimingLine(chars=[TimingChar("A", 0)], end_ms=2000)])
    with NativeRendererProcess(response_timeout_s=30) as renderer:
        for index, weight in enumerate((400, 650, 900, 400)):
            style = Style(font_family=overlap_variable_font,
                          font_family_latin=overlap_variable_font,
                          font_size_px=200, font_reference_height=360,
                          font_weight=weight, stroke_width_px=0,
                          line_y_position="center", line_horizontal_layout="center",
                          decoration_kind="none", line_lead_in_ms=0, line_tail_ms=0)
            if index == 0:
                renderer.configure_gpu(track, style, width=640, height=360, fps=60,
                                       duration_ms=2000, worker_count=1,
                                       defer_followers=True, force_warp=force_warp)
            else:
                renderer.configure_style_gpu(build_style_patch_ir(
                    track, style, width=640, height=360, fps=60, duration_ms=2000,
                ), worker_count=1, force_warp=force_warp)
            event = renderer.render_gpu_frame(1000, force_warp=force_warp)
            with SharedFrameRingReader.from_event(event) as reader:
                image = reader.read_qimage(event).copy()
            _assert_outline_pixels(image, weight)
