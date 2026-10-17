from dataclasses import replace
from pathlib import Path
import uuid

import numpy as np
import pytest
from PyQt6.QtGui import QImage
from PyQt6.QtWidgets import QApplication

from krok_helper.subtitle_render.domain.models import (
    Style, LyricsLayout, TimingTrack, TimingLine, TimingChar, TitleOverlay,
    style_to_dict, style_from_dict, rescale_layout_sizes,
)
from krok_helper.subtitle_render.engine.render.elements.horizontal.positioning import resolve_line_x_smart
from krok_helper.subtitle_render.engine.render.adapters.layout_diagnostics import check_layout_margins
from krok_helper.subtitle_render.engine.painter import paint_frame
from krok_helper.subtitle_render.engine.render.render_ir import build_render_ir
from krok_helper.subtitle_render.frontend.properties.property_panel import PropertyPanel
from krok_helper.subtitle_render.native.backend import (
    NativeRendererProcess, SharedFrameRingReader, resolve_native_renderer_path,
)


@pytest.fixture
def qapp():
    return QApplication.instance() or QApplication([])


def track_one():
    return TimingTrack(lines=[TimingLine(chars=[TimingChar("AB", 0)], end_ms=3000)])


def base_style(**changes):
    return replace(Style(), font_family="Arial", font_size_px=40,
                   line_alignments=["left"], smart_horizontal="none",
                   line_y_position="top", line_y_margin_px=100,
                   horizontal_margin_px=80, entry_anim="none", exit_anim="none",
                   **changes)


def test_offsets_roundtrip_old_projects_and_scaling():
    style = Style(line_offsets_x=[27, -45], layouts=[LyricsLayout(line_offsets_x=[-70, 90])])
    restored = style_from_dict(style_to_dict(style))
    assert restored.line_offsets_x == [27, -45]
    assert restored.layouts[0].line_offsets_x == [-70, 90]
    assert style_from_dict({}).line_offsets_x == []
    scaled = rescale_layout_sizes(restored, restored.layout_reference_height * 2)
    assert scaled.line_offsets_x == [54, -90]
    assert scaled.layouts[0].line_offsets_x == [-140, 180]


@pytest.mark.parametrize("mode", ["none", "center_position", "equal_margins"])
@pytest.mark.parametrize("align", ["left", "center", "right"])
def test_offset_is_added_after_smart_and_bottom_short_page(qapp, mode, align):
    track = track_one()
    style = replace(base_style(), smart_horizontal=mode, line_y_position="bottom",
                    line_alignments=["left", align], line_offsets_x=[111, -37])
    baseline = replace(style, line_offsets_x=[])
    x = resolve_line_x_smart(640, 80, track, track.lines[0], style, 0)
    expected = resolve_line_x_smart(640, 80, track, track.lines[0], baseline, 0)
    assert x == expected - 37


@pytest.mark.parametrize("align,offset,level", [("left", -20, "margin"), ("left", -100, "overflow"),
                                               ("right", 20, "margin"), ("right", 100, "overflow")])
def test_margin_diagnostics_use_translated_bounds(qapp, align, offset, level):
    track = track_one()
    style = replace(base_style(), line_alignments=[align])
    assert check_layout_margins(track, style, 640) == []
    warnings = check_layout_margins(track, replace(style, line_offsets_x=[offset]), 640)
    assert len(warnings) == 1
    assert warnings[0].level == level
    assert check_layout_margins(track, style, 640) == []  # cached geometry must recover


def test_offset_editor_catalog_add_remove_and_restore(qapp):
    panel = PropertyPanel()
    original = Style(line_offsets_x=[12, -34])
    panel.set_style(original)
    panel._on_line_offset_changed(1, -55)
    assert panel.subtitle_style.line_offsets_x == [12, -55]
    panel._on_add_line_alignment()  # bottom: new slot at the beginning
    assert panel.subtitle_style.line_offsets_x == [0, 12, -55]
    panel._on_remove_line_alignment(1)
    assert panel.subtitle_style.line_offsets_x == [0, -55]
    panel.set_style(original)
    assert panel._current_layout_offsets() == [12, -34]
    panel.set_style(Style(layouts=[LyricsLayout(name="custom", line_offsets_x=[4, 5])]))
    panel._layout_combo.setCurrentIndex(1)
    panel._on_line_offset_changed(0, -23)
    assert panel.subtitle_style.layouts[0].line_offsets_x == [-23, 5]
    assert panel.subtitle_style.line_offsets_x == []
    assert panel._layout_schematic._bar_specs()[0][1] == -23


def test_render_ir_preserves_custom_layout_offsets(qapp):
    track = track_one()
    track.lines[0].layout_index = 1
    style = Style(layouts=[LyricsLayout(line_offsets_x=[-12, 42])])
    ir = build_render_ir(track, style, width=640, height=360, fps=60)
    layout = ir["line_layout_table"][ir["track"]["lines"][0]["layout_id"]]
    assert layout["line_offsets_x"] == [-12, 42]


def cpu_alpha(track, style):
    image = QImage(640, 360, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(0)
    paint_frame(image, track, 1000, style, duration_ms=3000)
    return np.frombuffer(image.constBits().asstring(image.sizeInBytes()), dtype=np.uint8).reshape(360, 640, 4)[..., 3].copy()


@pytest.mark.parametrize("case", ["horizontal", "vertical", "bottom_short", "smart_center", "smart_equal", "custom", "title", "legacy_center", "legacy_single", "vertical_short"])
@pytest.mark.parametrize("offset", [-25, 35])
def test_cpu_and_gpu_frames_translate_without_relayout(qapp, case, offset):
    track = track_one()
    style = base_style(vertical=case == "vertical")
    shifted = replace(style, line_offsets_x=[offset])
    if case == "bottom_short":
        style = replace(style, line_y_position="bottom", line_alignments=["left", "right"])
        shifted = replace(style, line_offsets_x=[999, offset])
    elif case in {"smart_center", "smart_equal"}:
        track.lines.append(TimingLine(chars=[TimingChar("CD", 0)], end_ms=3000))
        style = replace(style, line_alignments=["left", "right"], line_gap_px=20,
                        smart_horizontal="center_position" if case == "smart_center" else "equal_margins")
        shifted = replace(style, line_offsets_x=[offset, offset])
    elif case == "custom":
        track.lines[0].layout_index = 1
        style = replace(style, layouts=[replace(style.default_layout, layout_id="custom")])
        shifted = replace(style, layouts=[replace(style.layouts[0], line_offsets_x=[offset])])
    elif case == "legacy_center":
        style = replace(style, line_horizontal_layout="center", line_y_position="bottom",
                        line_alignments=["left", "right"])
        shifted = replace(style, line_offsets_x=[offset, 999])
    elif case == "legacy_single":
        style = replace(style, dual_line_layout=False, line_y_position="bottom",
                        line_alignments=["left", "right"])
        shifted = replace(style, line_offsets_x=[offset, 999])
    elif case == "vertical_short":
        style = replace(style, vertical=True, line_y_position="bottom",
                        line_alignments=["left", "right"])
        shifted = replace(style, line_offsets_x=[999, offset])
    elif case == "title":
        track = TimingTrack(lines=[])
        style = replace(style, title_overlays=[TitleOverlay(enabled=True, text_template="AB", layout_index=0)])
        shifted = replace(style, line_offsets_x=[offset])
    cpu_before = cpu_alpha(track, style)
    cpu_after = cpu_alpha(track, shifted)
    assert cpu_before.max() > 0
    assert np.array_equal(np.roll(cpu_before, offset, axis=1), cpu_after)
    renderer_path = resolve_native_renderer_path(root=Path.cwd())
    if renderer_path is None:
        pytest.skip("native renderer not built")
    frames = []
    with NativeRendererProcess(renderer_path, response_timeout_s=20) as renderer:
        for generation, current in enumerate((style, shifted, style), 1):
            renderer.configure_gpu(track, current, width=640, height=360, fps=60,
                                   force_warp=True, realization_enabled=False, duration_ms=3000)
            event = renderer.render_gpu_frame(1000, force_warp=True, generation=generation,
                                              shm_key=f"offset-{uuid.uuid4().hex}", readback_bands=False)
            with SharedFrameRingReader.from_event(event) as reader:
                slot = reader.read_frame(event)
            rows = np.frombuffer(slot.payload, dtype=np.uint8).reshape(slot.height, slot.stride)
            frames.append(rows[:, :slot.width * 4].reshape(slot.height, slot.width, 4)[..., 3].copy())
    assert frames[0].max() > 0
    assert np.array_equal(np.roll(frames[0], offset, axis=1), frames[1])
    assert np.array_equal(frames[0], frames[2])
