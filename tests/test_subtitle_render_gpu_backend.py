from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
import uuid

import pytest
from PyQt6.QtGui import QImage

from krok_helper.subtitle_render.native_backend import (
    NativeRendererError,
    NativeRendererProcess,
    SharedFrameRingReader,
    resolve_native_renderer_path,
)
from krok_helper.subtitle_render.models import Style, TimingChar, TimingLine, TimingTrack


def test_shared_frame_reader_close_tolerates_deleted_qt_wrapper():
    class DeletedSharedMemory:
        def isAttached(self):
            raise RuntimeError("wrapped C/C++ object has been deleted")

    reader = SharedFrameRingReader("deleted-wrapper")
    reader._shared = DeletedSharedMemory()  # noqa: SLF001

    reader.close()
    reader.close()

    assert reader._shared is None  # noqa: SLF001


def _renderer_path() -> Path:
    path = resolve_native_renderer_path(root=Path.cwd())
    if path is None:
        pytest.skip("native subtitle renderer executable is not built")
    return path


def _pixel(slot, x: int, y: int) -> tuple[int, int, int, int]:
    offset = y * slot.stride + x * 4
    return tuple(slot.payload[offset : offset + 4])  # type: ignore[return-value]


def _g1_track() -> TimingTrack:
    return TimingTrack(
        lines=[
            TimingLine(
                chars=[
                    TimingChar("K", 0),
                    TimingChar("a", 500),
                    TimingChar("歌", 1000),
                ],
                end_ms=1500,
            )
        ]
    )


def _g1_style(**changes) -> Style:
    style = Style(
        font_family="Arial",
        font_family_latin="Times New Roman",
        font_size_px=64,
        font_reference_height=360,
        base_color="#FFFFFF",
        fill_color="#FF2030",
        stroke_color="#101010",
        stroke_width_px=3,
        stroke2_enabled=True,
        stroke2_width_px=2,
        decoration_kind="shadow",
        shadow_color="#00FF40",
        line_y_position="center",
        line_horizontal_layout="center",
        line_lead_in_ms=0,
        line_tail_ms=0,
    )
    return replace(style, **changes)


def _alpha_count(payload: bytes) -> int:
    return sum(alpha > 0 for alpha in payload[3::4])


def _alpha_bounds(slot) -> tuple[int, int, int, int]:
    xs: list[int] = []
    ys: list[int] = []
    for y in range(slot.height):
        row = y * slot.stride
        for x in range(slot.width):
            if slot.payload[row + x * 4 + 3] > 0:
                xs.append(x)
                ys.append(y)
    assert xs and ys
    return min(xs), min(ys), max(xs), max(ys)


def _render_g1_frames(
    renderer: NativeRendererProcess,
    style: Style,
    timestamps_ms: tuple[int, ...],
    *,
    force_warp: bool,
) -> tuple[dict, list[bytes]]:
    configured = renderer.configure_gpu(
        _g1_track(),
        style,
        width=640,
        height=360,
        fps=60,
        force_warp=force_warp,
    )
    reader: SharedFrameRingReader | None = None
    frames: list[bytes] = []
    try:
        for frame_index, t_ms in enumerate(timestamps_ms):
            event = renderer.render_gpu_frame(
                t_ms,
                force_warp=force_warp,
                frame_index=frame_index,
            )
            if reader is None:
                reader = SharedFrameRingReader.from_event(event)
                reader.attach()
            image = reader.read_qimage(event).convertToFormat(QImage.Format.Format_RGBA8888)
            bits = image.constBits()
            bits.setsize(image.sizeInBytes())
            frames.append(bytes(bits))
    finally:
        if reader is not None:
            reader.close()
    return configured, frames


def _render_painter_oracle(style: Style, *, t_ms: int = 750) -> bytes:
    from PyQt6.QtGui import QFontDatabase, QImage
    from PyQt6.QtWidgets import QApplication

    from krok_helper.subtitle_render.engine.painter import (
        clear_before_layer_cache,
        paint_frame,
    )

    app = QApplication.instance() or QApplication([])
    assert app is not None
    # Qt's offscreen Windows plugin does not enumerate system fonts by itself.
    # Load deterministic files that DirectWrite resolves to the same families.
    assert QFontDatabase.addApplicationFont(r"C:\Windows\Fonts\times.ttf") >= 0
    assert QFontDatabase.addApplicationFont(r"C:\Windows\Fonts\meiryo.ttc") >= 0
    image = QImage(640, 360, QImage.Format.Format_RGBA8888)
    image.fill(0)
    clear_before_layer_cache()
    paint_frame(image, _g1_track(), t_ms, style)
    bits = image.constBits()
    bits.setsize(image.sizeInBytes())
    return bytes(bits)


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_backend_reports_hardware_and_warp_adapters(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    with NativeRendererProcess(_renderer_path(), response_timeout_s=10.0) as renderer:
        hardware = renderer.backend_info()
        warp = renderer.backend_info(force_warp=True)

    if not hardware.get("available"):
        pytest.skip(f"hardware Direct2D adapter is unavailable: {hardware.get('error')}")
    assert hardware["backend"] == "direct2d"
    assert hardware["hardware"] is True
    assert hardware["warp"] is False
    assert hardware["adapter"]
    assert hardware["feature_level"] in {"11_0", "11_1"}
    assert hardware["transparent_surface"] is True
    assert hardware["staging_readback"] is True
    assert hardware["glyphs"] is True

    assert warp["available"] is True
    assert warp["backend"] == "direct2d"
    assert warp["hardware"] is False
    assert warp["warp"] is True
    assert "Microsoft" in warp["adapter"]


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
@pytest.mark.parametrize("force_warp", [False, True])
def test_gpu_probe_readback_is_straight_rgba_with_transparent_background(
    monkeypatch,
    force_warp: bool,
) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    with NativeRendererProcess(_renderer_path(), response_timeout_s=10.0) as renderer:
        if not force_warp and not renderer.backend_info().get("available"):
            pytest.skip("hardware Direct2D adapter is unavailable")
        event = renderer.render_probe(
            width=64,
            height=48,
            force_warp=force_warp,
            draw_glyph=False,
            rgba=(51, 102, 204, 128),
        )
        with SharedFrameRingReader.from_event(event) as reader:
            slot = reader.read_frame(event)

    assert event["backend"] == "direct2d"
    assert event["warp"] is force_warp
    assert event["render_ms"] >= 0.0
    assert event["readback_ms"] >= 0.0
    assert slot.width == 64
    assert slot.height == 48
    assert slot.stride == 64 * 4
    assert slot.pixel_format == "rgba8888"
    assert _pixel(slot, 0, 0) == (0, 0, 0, 0)
    rectangle_pixel = _pixel(slot, 16, 24)
    assert all(abs(actual - expected) <= 1 for actual, expected in zip(rectangle_pixel, (51, 102, 204, 128)))


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_probe_rejects_invalid_dimensions(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    with NativeRendererProcess(_renderer_path(), response_timeout_s=10.0) as renderer:
        with pytest.raises(NativeRendererError, match="dimensions"):
            renderer.render_probe(width=0, height=48)


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_preview_readback_copies_shared_slot_directly_to_qimage(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    with NativeRendererProcess(_renderer_path(), response_timeout_s=10.0) as renderer:
        event = renderer.render_probe(
            width=64,
            height=48,
            force_warp=True,
            draw_glyph=False,
            rgba=(51, 102, 204, 128),
        )
        with SharedFrameRingReader.from_event(event) as reader:
            image = reader.read_qimage(event)

    assert image.size().width() == 64
    assert image.size().height() == 48
    assert image.pixelColor(0, 0).getRgb() == (0, 0, 0, 0)
    assert all(
        abs(actual - expected) <= 1
        for actual, expected in zip(image.pixelColor(16, 24).getRgb(), (51, 102, 204, 128))
    )


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_preview_uses_native_premultiplied_bgra_without_checksum(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    with NativeRendererProcess(_renderer_path(), response_timeout_s=10.0) as renderer:
        renderer.configure_gpu(
            _g1_track(),
            _g1_style(),
            width=320,
            height=180,
            fps=60,
            force_warp=True,
        )
        event = renderer.render_gpu_frame(
            750,
            force_warp=True,
            include_checksum=False,
        )
        with SharedFrameRingReader.from_event(event) as reader:
            image = reader.read_qimage(event)

    assert event["pixel_format"] == "bgra8888_premultiplied"
    assert "checksum" not in event
    assert image.format() == QImage.Format.Format_ARGB32_Premultiplied
    assert image.width() == 320
    assert image.height() == 180


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_probe_1000_frames_reuses_device_and_shared_ring(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    shm_key = f"krok_gpu_pytest_{os.getpid()}_{uuid.uuid4().hex}"
    checksums: set[str] = set()
    with NativeRendererProcess(_renderer_path(), response_timeout_s=10.0) as renderer:
        info = renderer.backend_info(force_warp=True)
        assert info["available"] is True
        reader: SharedFrameRingReader | None = None
        try:
            for frame_index in range(1000):
                event = renderer.render_probe(
                    width=64,
                    height=48,
                    force_warp=True,
                    draw_glyph=True,
                    generation=7,
                    frame_index=frame_index,
                    shm_key=shm_key,
                )
                if reader is None:
                    reader = SharedFrameRingReader.from_event(event)
                    reader.attach()
                slot = reader.read_frame(event)
                assert slot.frame_index == frame_index
                checksums.add(str(event["checksum"]))
        finally:
            if reader is not None:
                reader.close()
    assert len(checksums) == 1


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_g1_directwrite_wipe_progresses_monotonically(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        configured, frames = _render_g1_frames(
            renderer,
            _g1_style(),
            (0, 750, 1500),
            force_warp=True,
        )

    assert configured["event"] == "gpu_configured"
    assert configured["backend"] == "direct2d"
    assert configured["line_count"] == 1
    red_counts = [
        sum(
            1
            for index in range(0, len(payload), 4)
            if payload[index] > 180
            and payload[index + 1] < 100
            and payload[index + 3] > 0
        )
        for payload in frames
    ]
    assert red_counts[0] < red_counts[1] < red_counts[2]
    assert len({_alpha_count(payload) for payload in frames}) == 1


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_g1_layers_outer_stroke_stroke_and_body(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        _, body = _render_g1_frames(
            renderer,
            _g1_style(stroke_width_px=0, stroke2_enabled=False, stroke2_width_px=0),
            (750,),
            force_warp=True,
        )
        _, stroke = _render_g1_frames(
            renderer,
            _g1_style(stroke_width_px=5, stroke2_enabled=False, stroke2_width_px=0),
            (750,),
            force_warp=True,
        )
        _, stroke2 = _render_g1_frames(
            renderer,
            _g1_style(stroke_width_px=5, stroke2_enabled=True, stroke2_width_px=5),
            (750,),
            force_warp=True,
        )

    assert _alpha_count(body[0]) < _alpha_count(stroke[0]) < _alpha_count(stroke2[0])


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_g1_repeated_configure_hits_geometry_layout_cache(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        first = renderer.configure_gpu(
            _g1_track(),
            _g1_style(),
            width=640,
            height=360,
            fps=60,
            force_warp=True,
        )
        second = renderer.configure_gpu(
            _g1_track(),
            _g1_style(),
            width=640,
            height=360,
            fps=60,
            force_warp=True,
        )

    assert first["cache_hits"] == 0
    assert first["cache_misses"] == 1
    assert second["cache_hits"] == 1
    assert second["cache_misses"] == 1
    assert second["cached_lines"] == 1
    assert second["cached_chars"] == 3
    assert second["cached_geometries"] >= 3
    assert second["estimated_cache_bytes"] > 0
    assert second["configure_ms"] < first["configure_ms"]


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_g1_n3_glow_concentration_adds_blur_passes(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    low = _g1_style(
        decoration_kind="glow",
        glow_radius_px=10,
        glow_before_radius_px=10,
        glow_after_radius_px=10,
        glow_concentration_level=0,
    )
    high = replace(low, glow_concentration_level=2)
    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        _, low_frames = _render_g1_frames(renderer, low, (750,), force_warp=True)
        _, high_frames = _render_g1_frames(renderer, high, (750,), force_warp=True)

    low_alpha = low_frames[0][3::4]
    high_alpha = high_frames[0][3::4]
    assert sum(high_alpha) > sum(low_alpha)
    assert _alpha_count(high_frames[0]) >= _alpha_count(low_frames[0])
    assert any(
        payload[index + 1] > payload[index] + 20 and payload[index + 3] > 0
        for payload in high_frames
        for index in range(0, len(payload), 4)
    )


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_g1_alignment_uses_visible_ink_bounds(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        renderer.configure_gpu(
            _g1_track(),
            _g1_style(dual_line_layout=False),
            width=640,
            height=360,
            fps=60,
            force_warp=True,
        )
        event = renderer.render_gpu_frame(750, force_warp=True)
        with SharedFrameRingReader.from_event(event) as reader:
            slot = reader.read_frame(event)

    left, top, right, bottom = _alpha_bounds(slot)
    assert abs((left + right) / 2.0 - slot.width / 2.0) <= 2.0
    assert abs((top + bottom) / 2.0 - slot.height / 2.0) <= 2.0


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_g1_hardware_and_warp_are_pixel_bounded(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        info = renderer.backend_info()
        if not info.get("available"):
            pytest.skip("hardware Direct2D adapter is unavailable")
        _, hardware = _render_g1_frames(renderer, _g1_style(), (750,), force_warp=False)
        _, warp = _render_g1_frames(renderer, _g1_style(), (750,), force_warp=True)

    hardware_alpha = hardware[0][3::4]
    warp_alpha = warp[0][3::4]
    differing_alpha = sum(a != b for a, b in zip(hardware_alpha, warp_alpha))
    max_alpha_delta = max(abs(a - b) for a, b in zip(hardware_alpha, warp_alpha))
    premultiplied_deltas = [
        abs(
            hardware[0][index + channel] * hardware[0][index + 3] // 255
            - warp[0][index + channel] * warp[0][index + 3] // 255
        )
        for index in range(0, len(hardware[0]), 4)
        for channel in range(3)
    ]
    assert differing_alpha <= len(hardware_alpha) * 0.01
    # Direct2D hardware and WARP use slightly different edge antialiasing at a
    # small number of pixels. Gate the aggregate premultiplied image error,
    # which is stable and meaningful even where straight RGB has tiny alpha.
    assert sum(abs(a - b) for a, b in zip(hardware_alpha, warp_alpha)) / len(hardware_alpha) <= 0.05
    assert sum(premultiplied_deltas) / len(premultiplied_deltas) <= 0.05
    assert max_alpha_delta <= 96


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_g1_basic_frame_matches_python_painter_within_bounded_diff(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    style = _g1_style(
        font_family="Meiryo",
        shadow_color="#00000000",
        shadow_offset_x=0,
        shadow_offset_y=0,
    )
    python_payload = _render_painter_oracle(style)

    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        _, gpu_frames = _render_g1_frames(renderer, style, (750,), force_warp=True)
    gpu_payload = gpu_frames[0]

    assert _alpha_count(python_payload) > 0
    assert _alpha_count(gpu_payload) > 0
    python_slot = type(
        "PainterFrame",
        (),
        {"payload": python_payload, "width": 640, "height": 360, "stride": 640 * 4},
    )()
    gpu_slot = type(
        "GpuFrame",
        (),
        {"payload": gpu_payload, "width": 640, "height": 360, "stride": 640 * 4},
    )()
    python_bounds = _alpha_bounds(python_slot)
    gpu_bounds = _alpha_bounds(gpu_slot)
    assert all(abs(a - b) <= 2 for a, b in zip(python_bounds, gpu_bounds)), (
        python_bounds,
        gpu_bounds,
    )

    channel_deltas = [abs(a - b) for a, b in zip(python_payload, gpu_payload)]
    assert sum(channel_deltas) / len(channel_deltas) < 1.0
    assert sum(delta > 8 for delta in channel_deltas) < 10_000


@pytest.mark.skipif(os.name != "nt", reason="Direct2D GPU backend is Windows-only")
def test_gpu_g1_n3_glow_matches_python_painter_within_bounded_diff(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    style = _g1_style(
        font_family="Meiryo",
        decoration_kind="glow",
        shadow_color="#00FF40",
        glow_radius_px=8,
        glow_before_radius_px=8,
        glow_after_radius_px=8,
        glow_concentration_level=1,
    )
    python_payload = _render_painter_oracle(style)
    with NativeRendererProcess(_renderer_path(), response_timeout_s=15.0) as renderer:
        _, gpu_frames = _render_g1_frames(renderer, style, (750,), force_warp=True)
    gpu_payload = gpu_frames[0]

    python_slot = type(
        "PainterGlowFrame",
        (),
        {"payload": python_payload, "width": 640, "height": 360, "stride": 640 * 4},
    )()
    gpu_slot = type(
        "GpuGlowFrame",
        (),
        {"payload": gpu_payload, "width": 640, "height": 360, "stride": 640 * 4},
    )()
    assert all(
        abs(a - b) <= 2
        for a, b in zip(_alpha_bounds(python_slot), _alpha_bounds(gpu_slot))
    )
    channel_deltas = [abs(a - b) for a, b in zip(python_payload, gpu_payload)]
    # Exact DirectWrite design bearings produce a slightly different glow edge
    # than QPainter's font-engine approximation; geometry and aggregate pixels
    # remain tightly bounded.
    assert sum(channel_deltas) / len(channel_deltas) < 1.3
    assert sum(delta > 8 for delta in channel_deltas) < 15_000
