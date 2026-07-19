from __future__ import annotations

import os
from pathlib import Path
import uuid

import pytest

from krok_helper.subtitle_render.native_backend import (
    NativeRendererError,
    NativeRendererProcess,
    SharedFrameRingReader,
    resolve_native_renderer_path,
)


def _renderer_path() -> Path:
    path = resolve_native_renderer_path(root=Path.cwd())
    if path is None:
        pytest.skip("native subtitle renderer executable is not built")
    return path


def _pixel(slot, x: int, y: int) -> tuple[int, int, int, int]:
    offset = y * slot.stride + x * 4
    return tuple(slot.payload[offset : offset + 4])  # type: ignore[return-value]


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
