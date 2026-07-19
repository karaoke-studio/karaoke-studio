"""Non-interactive smoke checks executed by packaged build scripts."""

from __future__ import annotations

import multiprocessing as mp


def _square(value: int) -> int:
    return value * value


def run_spawn_smoke() -> int:
    """Exercise a real spawn Pool from the current (possibly frozen) executable."""
    context = mp.get_context("spawn")
    with context.Pool(2) as pool:
        result = pool.map_async(_square, [1, 2, 3, 4]).get(timeout=30)
    return 0 if result == [1, 4, 9, 16] else 1


def run_gpu_subtitle_smoke() -> int:
    """Exercise the bundled Direct2D sidecar and shared-memory band readback."""
    from PyQt6.QtWidgets import QApplication

    from krok_helper.subtitle_render.models import (
        Style,
        TimingChar,
        TimingLine,
        TimingTrack,
    )
    from krok_helper.subtitle_render.native_backend import (
        NativeRendererProcess,
        SharedFrameRingReader,
    )

    # ``build_render_ir`` resolves Painter-compatible font metrics before it
    # configures DirectWrite, so even this non-interactive path needs a GUI Qt
    # application.  It creates no window and exercises the packaged qwindows
    # plugin as part of the smoke check.
    app = QApplication.instance() or QApplication([])
    track = TimingTrack(
        lines=[TimingLine(chars=[TimingChar("GPU", 0)], end_ms=1_000)]
    )
    style = Style(
        font_family="Meiryo",
        font_size_px=48,
        line_y_position="center",
        line_horizontal_layout="center",
        line_lead_in_ms=0,
    )
    with NativeRendererProcess(response_timeout_s=20.0) as renderer:
        info = renderer.backend_info(force_warp=True)
        if not info.get("available") or not info.get("warp"):
            return 2
        renderer.configure_gpu(
            track,
            style,
            width=320,
            height=180,
            fps=60,
            force_warp=True,
        )
        event = renderer.render_gpu_frame(
            500,
            force_warp=True,
            readback_bands=True,
            include_checksum=False,
        )
        with SharedFrameRingReader.from_event(event) as reader:
            image = reader.read_qimage(event)
        if image.isNull() or image.width() != 320 or image.height() != 180:
            return 3
        bits = image.constBits()
        bits.setsize(image.sizeInBytes())
        result = 0 if any(bits[index] for index in range(3, len(bits), 4)) else 4
    app.processEvents()
    return result
