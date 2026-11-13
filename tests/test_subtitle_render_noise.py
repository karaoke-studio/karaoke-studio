from __future__ import annotations

from dataclasses import replace
import os
from typing import get_args

import numpy as np
import pytest
from PyQt6.QtGui import QColor, QImage, QPainter

from krok_helper.subtitle_render.domain.models import (
    Style, TimingChar, TimingLine, TimingTrack, style_from_dict, style_to_dict,
)
from krok_helper.subtitle_render.domain.timing import EntryAnimation, ExitAnimation, LineAnimationOverride
from krok_helper.subtitle_render.engine.painter import paint_frame, _TEXT_RUN_COMPOSITOR
from krok_helper.subtitle_render.engine.renderer import _paint_overlay_strip, _paint_overlay_bands
from krok_helper.subtitle_render.engine.render.effects.noise import (
    noise_level, noise_mask, noise_rank, paint_noise,
)
from krok_helper.subtitle_render.native.protocol import gpu_unsupported_features
from krok_helper.subtitle_render.serialization.timing import (
    line_animation_override_from_dict, line_animation_override_to_dict,
)
from krok_helper.subtitle_render.settings.property_controllers import PropertyStyleController


def pixels(image):
    image = image.convertToFormat(QImage.Format.Format_RGBA8888)
    bits = image.constBits()
    bits.setsize(image.sizeInBytes())
    return np.frombuffer(bytes(bits), np.uint8).reshape(image.height(), image.width(), 4)


def test_noise_timing_has_exact_endpoints_and_respects_singing_end():
    def level(t, end=4000):
        return noise_level("noise", "noise", 600, 600, 0, end, 3000, t)

    assert [level(t) for t in (0, 300, 600, 2000, 3400, 3700, 4000)] == [0, 32, 64, 64, 64, 32, 0]
    # A compressed tail starts at singing end and still completes at display end.
    assert level(3000, 3200) == 64
    assert level(3100, 3200) == 32
    assert level(3200, 3200) == 0
    assert noise_level("noise", "noise", 0, 0, 0, 4000, 3000, 10) == 64


def test_noise_mask_is_monotone_cached_and_contains_horizontal_and_fine_cells():
    low, high = noise_mask(16), noise_mask(48)
    assert low is noise_mask(16)
    assert np.all(pixels(low)[:, :, 3] <= pixels(high)[:, :, 3])
    assert not pixels(noise_mask(0)).any()
    assert np.all(pixels(noise_mask(64)) == 255)
    # The immutable field can be sampled in arbitrary order (seeking).
    assert [noise_rank(x, 19) for x in range(8)] == [noise_rank(x, 19) for x in range(8)]
    streaks = pixels(noise_mask(0, True))[:, :, 3]
    assert np.array_equal(streaks[0], streaks[-1])
    assert 0 < np.count_nonzero(streaks[0]) < 128


def test_noise_composites_premultiplied_source_and_downward_streaks():
    source = QImage(128, 128, QImage.Format.Format_ARGB32_Premultiplied)
    source.fill(0)
    painter = QPainter(source)
    painter.fillRect(16, 20, 96, 40, QColor("#FF3000"))
    painter.end()
    result = QImage(source.size(), source.format())
    result.fill(0)
    scratch = QImage(source.size(), source.format())
    painter = QPainter(result)
    paint_noise(painter, source, scratch, 32, 80, 1)
    painter.end()
    actual = pixels(result)
    assert np.any(actual[60:80, :, 3] > 0), "vertical streaks must extend below the source"
    assert np.count_nonzero(actual[20:60, 16:112, 3] == 0) > 500
    assert np.all(actual[actual[:, :, 3] > 0, 0] >= 250), "noise must retain source color"


def test_noise_global_and_line_override_roundtrip_and_native_capability():
    style = Style(entry_anim="noise", exit_anim="noise")
    restored = style_from_dict(style_to_dict(style))
    assert (restored.entry_anim, restored.exit_anim) == ("noise", "noise")
    override = LineAnimationOverride(entry_anim="noise", exit_anim="noise")
    assert line_animation_override_from_dict(line_animation_override_to_dict(override)) == override
    track = TimingTrack(lines=[TimingLine(chars=[TimingChar("A", 1000)], end_ms=2000,
                                        animation_override=override)])
    assert gpu_unsupported_features(track, style) == ()


@pytest.mark.parametrize("field, kinds", [
    ("entry_anim", EntryAnimation), ("exit_anim", ExitAnimation),
    ("section_head_anim", EntryAnimation), ("section_tail_anim", ExitAnimation),
])
def test_property_style_controller_accepts_every_registered_animation(field, kinds):
    controller = PropertyStyleController()
    for value in get_args(kinds):
        result = controller.update(Style(), {field: value}, force_global=True)
        assert getattr(result.style, field) == value


@pytest.mark.parametrize("field, combo_name", [
    ("entry_anim", "_entry_anim_combo"),
    ("exit_anim", "_exit_anim_combo"),
    ("section_head_anim", "_section_head_anim_combo"),
    ("section_tail_anim", "_section_tail_anim_combo"),
])
def test_selecting_noise_in_real_panel_survives_project_save_and_renders(tmp_path, field, combo_name):
    from krok_helper.subtitle_render.frontend.properties.property_panel import PropertyPanel
    from krok_helper.subtitle_render.project.store import (
        project_payload, save_render_project, load_render_project,
    )

    def save_style(path, style):
        data = project_payload(subtitle_path=None, video_path=None, audio_path=None,
            style=style_to_dict(style), screen={"width": 640, "height": 360, "fps": 60},
            selected_scheme_key="", output={})
        save_render_project(path, data)
        return style_from_dict(load_render_project(path)["style"])

    path = tmp_path / "noise.yurika"
    initial = save_style(path, Style(
        entry_anim="none", exit_anim="none", entry_lead_ms=600, exit_fade_ms=600,
        section_edge_anim_enabled=field.startswith("section_"),
        section_head_anim="none", section_tail_anim="none",
        font_family="Arial", font_family_latin="Arial", font_size_px=60,
        font_reference_height=360, karaoke_anim="none", dual_line_layout=field.startswith("section_"),
        title_overlays=[], volume_enabled=True, volume_appearance_mode="auto",
        volume_duration_ms=2000,
    ))
    panel = PropertyPanel()
    try:
        panel.set_style(initial)
        emitted = []
        panel.styleChanged.connect(emitted.append)
        combo = getattr(panel, combo_name)
        combo.setCurrentIndex(combo.findData("noise"))
        assert emitted
        edited = emitted[-1]
        assert getattr(edited, field) == "noise"
        restored = save_style(path, edited)
        assert getattr(restored, field) == "noise"
        track = TimingTrack(lines=[TimingLine(chars=[TimingChar("TEST", 2000)], end_ms=3000,
            display_start_override_ms=0, display_end_override_ms=4000)])
        t = 300 if field in {"entry_anim", "section_head_anim"} else 3700
        images = []
        for style in (restored, replace(restored, **{field: "none"})):
            image = QImage(640, 360, QImage.Format.Format_ARGB32_Premultiplied)
            image.fill(0)
            paint_frame(image, track, t, style)
            images.append(pixels(image))
        assert np.any(images[0][:, :, 3])
        assert not np.array_equal(*images), "the actual UI edit must reach the renderer"
        if os.name == "nt":
            from krok_helper.subtitle_render.native.backend import (
                NativeRendererProcess, SharedFrameRingReader, resolve_native_renderer_path,
            )
            executable = resolve_native_renderer_path()
            if executable is not None:
                gpu_images = []
                with NativeRendererProcess(executable, response_timeout_s=30) as renderer:
                    for style in (restored, replace(restored, **{field: "none"})):
                        renderer.configure_gpu(track, style, width=640, height=360, fps=60)
                        event = renderer.render_gpu_frame(t)
                        with SharedFrameRingReader.from_event(event) as reader:
                            gpu_images.append(pixels(reader.read_qimage(event)))
                assert np.any(gpu_images[0][:, :, 3])
                assert not np.array_equal(*gpu_images), "the saved UI edit must reach G5 too"
    finally:
        panel.close()
        panel.deleteLater()


@pytest.mark.parametrize("vertical", [False, True])
def test_cpu_noise_seeking_keeps_existing_baked_layer_cache(vertical):
    track = TimingTrack(lines=[TimingLine(chars=[TimingChar("TEST", 2000)], end_ms=3000)])
    style = Style(font_family="Arial", font_family_latin="Arial", font_size_px=60,
                  entry_anim="noise", exit_anim="noise", karaoke_anim="none",
                  entry_lead_ms=600, exit_fade_ms=600, line_tail_ms=600,
                  dual_line_layout=False, vertical=vertical, title_overlays=[])

    def render(t, current=style):
        image = QImage(640, 360, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(0)
        paint_frame(image, track, t, current)
        return pixels(image)

    first = render(300)
    cache_size = len(_TEXT_RUN_COMPOSITOR.cache)
    for t in (100, 450, 200, 500, 300):
        render(t)
    assert np.array_equal(first, render(300))
    assert np.count_nonzero(first[:, :, 3]) > 0
    if not vertical:
        assert cache_size > 0
        assert len(_TEXT_RUN_COMPOSITOR.cache) == cache_size
    complete = render(1000)
    static = render(1000, replace(style, entry_anim="none", exit_anim="none"))
    assert np.array_equal(complete, static)


@pytest.mark.parametrize("t", [300, 3700])
@pytest.mark.parametrize("volume", [False, True])
def test_noise_cpu_strip_and_packed_bands_equal_full_preview(t, volume):
    track = TimingTrack(lines=[TimingLine(chars=[TimingChar("TEST", 2000)], end_ms=3000,
        display_start_override_ms=0, display_end_override_ms=4000)])
    style = Style(font_family="Arial", font_family_latin="Arial", font_size_px=60,
        font_reference_height=360, entry_anim="noise", exit_anim="noise",
        karaoke_anim="none", entry_lead_ms=600, exit_fade_ms=600,
        dual_line_layout=False, title_overlays=[], volume_enabled=volume,
        volume_appearance_mode="auto", volume_duration_ms=2000,
        viewport_rotation_deg=8, viewport_scale_pct=90)
    full = QImage(640, 360, QImage.Format.Format_RGBA8888)
    full.fill(0)
    paint_frame(full, track, t, style)
    strip = QImage(640, 270, full.format())
    _paint_overlay_strip(strip, track, style, t, logical_w=640, logical_h=360,
                         strip_top=53, transparent=QColor(0, 0, 0, 0))
    assert np.array_equal(pixels(strip), pixels(full)[53:323])
    bands = QImage(640, 240, full.format())
    _paint_overlay_bands(bands, track, style, t, logical_w=640, logical_h=360,
                         bands=[(35, 120), (180, 120)], transparent=QColor(0, 0, 0, 0))
    assert np.array_equal(pixels(bands), np.concatenate([pixels(full)[35:155], pixels(full)[180:300]]))
