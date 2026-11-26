"""Non-interactive smoke checks executed by packaged build scripts."""

from __future__ import annotations

import multiprocessing as mp
import threading
import time


def _square(value: int) -> int:
    return value * value


def run_spawn_smoke() -> int:
    """Exercise a real spawn Pool from the current (possibly frozen) executable."""
    context = mp.get_context("spawn")
    with context.Pool(2) as pool:
        result = pool.map_async(_square, [1, 2, 3, 4]).get(timeout=30)
    return 0 if result == [1, 4, 9, 16] else 1


def run_gpu_subtitle_smoke() -> int:
    """Exercise bundled Direct2D G5 readback and G6 native presentation."""
    from PyQt6.QtWidgets import QApplication, QWidget

    from krok_helper.subtitle_render.domain.models import (
        Style,
        TimingChar,
        TimingLine,
        TimingTrack,
    )
    from krok_helper.subtitle_render.native.backend import (
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
        if not info.get("native_preview"):
            return 5
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
    if result != 0:
        return result

    parent = QWidget()
    parent.resize(320, 180)
    parent.show()
    app.processEvents()
    parent_hwnd = int(parent.winId())
    native_result: dict[str, object] = {}

    def present_native() -> None:
        try:
            with NativeRendererProcess(response_timeout_s=20.0) as renderer:
                renderer.configure_gpu(
                    track,
                    style,
                    width=320,
                    height=180,
                    fps=60,
                    force_warp=True,
                )
                native_result["event"] = renderer.present_gpu_frame(
                    500,
                    parent_hwnd=parent_hwnd,
                    x=0,
                    y=0,
                    width=320,
                    height=180,
                    force_warp=True,
                )
                native_result["closed"] = renderer.close_gpu_preview(force_warp=True)
        except BaseException as exc:  # pragma: no cover - packaged diagnostic path
            native_result["error"] = exc

    worker = threading.Thread(target=present_native, name="package-g6-preview-smoke")
    worker.start()
    deadline = time.monotonic() + 30.0
    while worker.is_alive() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)
    worker.join(timeout=1.0)
    parent.close()
    parent.deleteLater()
    app.processEvents()
    if worker.is_alive() or "error" in native_result:
        return 6
    event = native_result.get("event")
    closed = native_result.get("closed")
    if not isinstance(event, dict) or not isinstance(closed, dict):
        return 7
    if (
        event.get("event") != "gpu_frame_presented"
        or event.get("transport") != "direct_composition"
        or float(event.get("readback_ms", -1.0)) != 0.0
        or closed.get("event") != "gpu_preview_closed"
    ):
        return 8
    return 0


def run_skia_subtitle_smoke() -> int:
    """Render one frame through the bundled Skia sidecar on Skia Raster.

    Build machines may have no GPU, so this checks the packaged sidecar, its
    Qt runtime, ``icudtl.dat`` and the shared-memory frame path on the CPU
    target that every user falls back to.
    """
    from functools import partial

    from PyQt6.QtGui import QFontDatabase
    from PyQt6.QtWidgets import QApplication

    from krok_helper.subtitle_render.domain.models import (
        Style,
        TimingChar,
        TimingLine,
        TimingTrack,
    )
    from krok_helper.subtitle_render.engine.export.native_export import (
        iter_native_rgba_frames_at_times,
    )
    from krok_helper.subtitle_render.engine.text.font_preparation import prepare_fonts
    from krok_helper.subtitle_render.native.skia_backend import (
        SkiaRendererProcess,
        SkiaSelection,
    )
    from krok_helper.subtitle_render.native.skia_process import resolve_skia_renderer_path

    app = QApplication.instance() or QApplication([])
    family = QFontDatabase.systemFont(QFontDatabase.SystemFont.GeneralFont).family()
    track = TimingTrack(
        lines=[TimingLine(chars=[TimingChar(c, 100 * i) for i, c in enumerate("Skia")], end_ms=1_000)]
    )
    style = Style(
        font_family=family,
        font_family_latin=None,
        font_size_px=48,
        stroke_width_px=0,
        stroke2_width_px=0,
        decoration_kind="none",
        line_y_position="center",
        line_horizontal_layout="center",
        line_lead_in_ms=0,
        entry_anim="none",
        exit_anim="none",
        karaoke_anim="none",
    )
    path = resolve_skia_renderer_path()
    prepare_fonts(track, style, [], use_skia_backend=True)
    factory = partial(SkiaRendererProcess, selection=SkiaSelection("raster", "none", False))
    frames = list(iter_native_rgba_frames_at_times(
        track, style, [500], width=320, height=180, fps=60, duration_ms=1_000,
        renderer_path=path, process_factory=factory,
    ))
    app.processEvents()
    if len(frames) != 1:
        return 2
    _, pixels = frames[0]
    data = bytes(pixels)
    if len(data) != 320 * 180 * 4:
        return 3
    return 0 if any(data[index] for index in range(3, len(data), 4)) else 4


def run_audio_smoke() -> int:
    """Load PortAudio the way SUG's playback engine does (Linux bundles it)."""
    import sounddevice

    return 0 if sounddevice.get_portaudio_version()[0] > 0 else 1
