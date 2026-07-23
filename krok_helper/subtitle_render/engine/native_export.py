"""Native sidecar frame source for export experiments.

C6 starts with a narrow adapter: Python still owns ffmpeg, progress, cleanup,
and fallback policy, while the native sidecar renders full RGBA frames in
timestamp ranges.  Strip/band export optimizations remain on the Python path
until the full-frame path is proven stable.
"""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Callable, Iterator

from krok_helper.errors import ExportCancelled
from krok_helper.subtitle_render.models import Style, TimingTrack
from krok_helper.subtitle_render.native_backend import (
    NativeRendererError,
    NativeRendererProcess,
    SharedFrameRingReader,
    SharedFrameSlot,
)

_DEFAULT_TARGET_CHUNK_BYTES = 128 * 1024 * 1024
_DEFAULT_RING_SLOTS_CAP = 64
_DEFAULT_GPU_EXPORT_REALIZATION_MEMORY_MB = 256
_GPU_REALIZATION_ESTIMATED_BYTES_PER_TASK = 4096
_GPU_REALIZATION_MIN_TASKS = 8192
_GPU_REALIZATION_MAX_TASKS = 262144


def native_export_timestamps(
    *,
    start_frame: int,
    count: int,
    fps: int,
) -> list[int]:
    """Return export frame timestamps matching the Python renderer cadence."""
    normalized_fps = max(int(fps), 1)
    start = max(int(start_frame), 0)
    frame_count = max(int(count), 0)
    return [int(round((start + index) * 1000 / normalized_fps)) for index in range(frame_count)]


def native_export_chunk_frames(
    *,
    width: int,
    height: int,
    total_frames: int,
    target_bytes: int | None = None,
) -> int:
    """Choose a range size that keeps the shared-memory ring bounded."""
    frame_bytes = max(int(width), 1) * max(int(height), 1) * 4
    try:
        raw_target = (
            int(target_bytes)
            if target_bytes is not None
            else int(os.environ.get("KROK_SUBTITLE_NATIVE_EXPORT_CHUNK_BYTES", _DEFAULT_TARGET_CHUNK_BYTES))
        )
    except ValueError:
        raw_target = _DEFAULT_TARGET_CHUNK_BYTES
    by_bytes = max(raw_target, frame_bytes) // frame_bytes
    capped = min(max(int(by_bytes), 1), _DEFAULT_RING_SLOTS_CAP)
    return max(1, min(capped, max(int(total_frames), 1)))


def native_export_threads() -> int:
    raw = os.environ.get("KROK_SUBTITLE_NATIVE_EXPORT_THREADS") or os.environ.get(
        "KROK_SUBTITLE_NATIVE_THREADS",
        "4",
    )
    try:
        return max(int(raw), 1)
    except ValueError:
        return 4


def gpu_export_realization_capacity() -> int:
    """Return the export realization cap derived from a bounded memory budget."""

    raw = os.environ.get(
        "KROK_SUBTITLE_GPU_EXPORT_REALIZATION_MEMORY_MB",
        str(_DEFAULT_GPU_EXPORT_REALIZATION_MEMORY_MB),
    )
    try:
        budget_mb = max(int(raw), 32)
    except ValueError:
        budget_mb = _DEFAULT_GPU_EXPORT_REALIZATION_MEMORY_MB
    capacity = budget_mb * 1024 * 1024 // _GPU_REALIZATION_ESTIMATED_BYTES_PER_TASK
    return max(
        _GPU_REALIZATION_MIN_TASKS,
        min(int(capacity), _GPU_REALIZATION_MAX_TASKS),
    )


def gpu_export_packed_enabled() -> bool:
    return os.environ.get("KROK_SUBTITLE_GPU_EXPORT_PACKED", "0").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def gpu_export_shared_resources_enabled() -> bool:
    return os.environ.get(
        "KROK_SUBTITLE_GPU_EXPORT_SHARED_RESOURCES", "0"
    ).strip().lower() in {"1", "true", "yes", "on"}


def gpu_export_realization_enabled() -> bool:
    return os.environ.get(
        "KROK_SUBTITLE_GPU_EXPORT_REALIZATION", "0"
    ).strip().lower() in {"1", "true", "yes", "on"}


def _wait_for_gpu_export_realizations(
    renderer: NativeRendererProcess,
    configured: dict[str, object],
    *,
    force_warp: bool,
    should_cancel: Callable[[], bool] | None,
    on_progress: Callable[[int, int], None] | None,
) -> dict[str, object]:
    """Wait for export-wide realization prewarm before submitting frame zero."""

    diagnostics = configured
    total = max(int(diagnostics.get("realization_prewarm_tasks", 0) or 0), 0)
    completed = max(int(diagnostics.get("realization_count", 0) or 0), 0)
    if on_progress is not None:
        on_progress(min(completed, total), total)
    while not bool(diagnostics.get("realization_prewarm_complete", True)):
        if should_cancel is not None and should_cancel():
            raise ExportCancelled("已停止导出。")
        time.sleep(0.05)
        diagnostics = renderer.gpu_diagnostics(force_warp=force_warp)
        total = max(
            int(diagnostics.get("realization_prewarm_tasks", total) or total), 0
        )
        completed = max(
            int(diagnostics.get("realization_count", completed) or completed), 0
        )
        if on_progress is not None:
            on_progress(min(completed, total), total)
    if on_progress is not None:
        on_progress(total, total)
    return diagnostics


def shared_slot_rgba_bytes(slot: SharedFrameSlot) -> bytes:
    """Return tightly packed RGBA bytes suitable for ffmpeg rawvideo stdin."""
    row_bytes = max(int(slot.width), 0) * 4
    stride = int(slot.stride)
    height = max(int(slot.height), 0)
    if row_bytes <= 0 or height <= 0:
        return b""
    if stride == row_bytes:
        expected = row_bytes * height
        return bytes(slot.payload[:expected])
    if stride < row_bytes:
        raise NativeRendererError(f"shared frame stride is too small: {stride} < {row_bytes}")
    payload = bytes(slot.payload)
    expected = stride * height
    if len(payload) < expected:
        raise NativeRendererError(f"shared frame payload is truncated: {len(payload)} < {expected}")
    packed = bytearray(row_bytes * height)
    for y in range(height):
        src_start = y * stride
        dst_start = y * row_bytes
        packed[dst_start : dst_start + row_bytes] = payload[src_start : src_start + row_bytes]
    return bytes(packed)


def iter_native_rgba_frames(
    track: TimingTrack,
    style: Style,
    *,
    width: int,
    height: int,
    fps: int,
    total_frames: int,
    duration_ms: int | None = None,
    renderer_path: str | os.PathLike[str] | None = None,
    threads: int | None = None,
    chunk_frames: int | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> Iterator[bytes]:
    """Yield full-frame RGBA bytes rendered by the native sidecar in order."""
    frame_total = max(int(total_frames), 0)
    if frame_total <= 0:
        return
    timestamps = native_export_timestamps(start_frame=0, count=frame_total, fps=fps)
    for _t_ms, frame in iter_native_rgba_frames_at_times(
        track,
        style,
        timestamps,
        width=width,
        height=height,
        fps=fps,
        duration_ms=duration_ms,
        renderer_path=renderer_path,
        threads=threads,
        chunk_frames=chunk_frames,
        should_cancel=should_cancel,
    ):
        yield frame


def iter_native_rgba_frames_at_times(
    track: TimingTrack,
    style: Style,
    timestamps_ms: list[int],
    *,
    width: int,
    height: int,
    fps: int,
    duration_ms: int | None = None,
    renderer_path: str | os.PathLike[str] | None = None,
    threads: int | None = None,
    chunk_frames: int | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> Iterator[tuple[int, bytes]]:
    """Yield ``(t_ms, rgba_bytes)`` for explicit timestamps from the sidecar."""
    timestamps_all = [int(t_ms) for t_ms in timestamps_ms]
    frame_total = len(timestamps_all)
    if frame_total <= 0:
        return
    worker_threads = native_export_threads() if threads is None else max(int(threads), 1)
    chunk_size = (
        native_export_chunk_frames(width=width, height=height, total_frames=frame_total)
        if chunk_frames is None
        else max(int(chunk_frames), 1)
    )
    generation = 1
    with NativeRendererProcess(renderer_path, response_timeout_s=5.0, close_timeout_s=1.0) as renderer:
        configure_kwargs = {"width": width, "height": height, "fps": fps}
        if duration_ms is not None:
            configure_kwargs["duration_ms"] = max(int(duration_ms), 0)
        renderer.configure(track, style, **configure_kwargs)
        start_frame = 0
        while start_frame < frame_total:
            if should_cancel is not None and should_cancel():
                raise ExportCancelled("已停止导出。")
            count = min(chunk_size, frame_total - start_frame)
            timestamps = timestamps_all[start_frame : start_frame + count]
            shm_key = f"krok-export-{os.getpid()}-{uuid.uuid4().hex}"
            renderer.start_render_range(
                timestamps,
                generation=generation,
                threads=worker_threads,
                shm_key=shm_key,
                ring_slots=count,
            )
            pending: dict[int, bytes] = {}
            next_emit = 0
            range_done = False
            try:
                while not range_done:
                    if should_cancel is not None and should_cancel():
                        renderer.send_cancel_generation(generation)
                        raise ExportCancelled("已停止导出。")
                    event = renderer.read_event()
                    if int(event.get("generation", generation)) != generation:
                        continue
                    kind = event.get("event")
                    if kind == "frame_ready":
                        with SharedFrameRingReader.from_event(event) as reader:
                            slot = reader.read_frame(event)
                        frame_index = int(event.get("frame_index", slot.frame_index))
                        pending[frame_index] = shared_slot_rgba_bytes(slot)
                        while next_emit in pending:
                            yield timestamps[next_emit], pending.pop(next_emit)
                            next_emit += 1
                    elif kind == "range_done":
                        range_done = True
                    elif kind == "generation_cancelled":
                        continue
                if next_emit != count:
                    raise NativeRendererError(
                        f"native export range ended before all frames were emitted: {next_emit}/{count}"
                    )
            finally:
                generation += 1
            start_frame += count


def iter_gpu_rgba_frames(
    track: TimingTrack,
    style: Style,
    *,
    width: int,
    height: int,
    fps: int,
    total_frames: int,
    duration_ms: int | None = None,
    start_t_ms: int = 0,
    renderer_path: str | os.PathLike[str] | None = None,
    extra_tracks: list[TimingTrack] | None = None,
    force_warp: bool = False,
    worker_count: int = 1,
    shared_resources: bool | None = None,
    realization_enabled: bool | None = None,
    packed_rgba: bool | None = None,
    crop: tuple[int, int] | None = None,
    bands: list[tuple[int, int]] | None = None,
    should_cancel: Callable[[], bool] | None = None,
    on_prepare_progress: Callable[[int, int], None] | None = None,
    on_diagnostics: Callable[[dict[str, object]], None] | None = None,
    on_frame_diagnostics: Callable[[dict[str, object]], None] | None = None,
) -> Iterator[bytes | memoryview]:
    """Yield straight RGBA frames from the Direct2D GPU export path.

    Direct2D reads back only the renderer's conservative subtitle bands.  The
    shared-memory reader expands those bands into a transparent QImage before
    converting premultiplied BGRA to ffmpeg's straight RGBA input format.

    One worker keeps the original one-frame-deep two-slot pipeline. The
    opt-in packed path converts full-frame premultiplied BGRA to straight RGBA
    in the sidecar and lends the shared-memory slot directly to ffmpeg without
    constructing a QImage. Hardware
    exports may use up to four independent Direct2D workers; completed frames
    are held in a ring-sized reorder window and yielded strictly in timestamp
    order, so ffmpeg never observes out-of-order input.
    """
    from PyQt6.QtGui import QImage

    frame_total = max(int(total_frames), 0)
    if frame_total <= 0:
        return
    shm_key = f"krok-gpu-export-{os.getpid()}-{uuid.uuid4().hex}"
    slot_count = 2
    packed = gpu_export_packed_enabled() if packed_rgba is None else bool(packed_rgba)
    shared = (
        gpu_export_shared_resources_enabled()
        if shared_resources is None
        else bool(shared_resources)
    )
    realizations = (
        gpu_export_realization_enabled()
        if realization_enabled is None
        else bool(realization_enabled)
    )
    packed_bands: list[tuple[int, int]] = []
    if packed:
        for raw_top, raw_height in bands or []:
            top = max(0, min(int(raw_top), max(int(height) - 1, 0)))
            band_height = max(1, min(int(raw_height), int(height) - top))
            packed_bands.append((top, band_height))
    crop_top, crop_height = crop if packed and crop is not None else (0, height)
    crop_top = max(0, min(int(crop_top), max(int(height) - 1, 0)))
    crop_height = max(1, min(int(crop_height), int(height) - crop_top))
    packed_height = (
        sum(max(int(band_height), 1) for _top, band_height in packed_bands)
        if packed_bands
        else crop_height
    )
    reader: SharedFrameRingReader | None = None
    try:
        with NativeRendererProcess(
            renderer_path,
            response_timeout_s=300.0 if realizations else 15.0,
            close_timeout_s=1.0,
        ) as renderer:
            configure_started = time.perf_counter()
            configure_kwargs = {
                "width": width,
                "height": height,
                "fps": fps,
                "force_warp": force_warp,
                "extra_tracks": extra_tracks,
                "worker_count": max(1, min(int(worker_count), 4)),
                "realization_enabled": realizations,
                "shared_resources": shared,
                "wait_realizations": realizations,
                "realization_capacity": gpu_export_realization_capacity(),
                "export_crop_top": crop_top if packed else 0,
                "export_crop_height": crop_height if packed and not packed_bands else 0,
                "export_bands": packed_bands,
            }
            if duration_ms is not None:
                configure_kwargs["duration_ms"] = max(int(duration_ms), 0)
            configured = renderer.configure_gpu(
                track,
                style,
                **configure_kwargs,
            )
            configured = dict(configured)
            configured["prepare_layout_ms"] = (
                time.perf_counter() - configure_started
            ) * 1000.0
            if on_diagnostics is not None:
                on_diagnostics(configured)

            active_workers = max(
                1, min(int(configured.get("worker_count", 1) or 1), 4)
            )
            slot_count = 2 if active_workers == 1 else active_workers

            request_started: dict[int, float] = {}

            def begin_frame(frame_index: int) -> None:
                t_ms = max(int(start_t_ms), 0) + int(
                    round(frame_index * 1000 / max(int(fps), 1))
                )
                request_started[frame_index] = time.perf_counter()
                renderer.begin_render_gpu_frame(
                    t_ms,
                    force_warp=force_warp,
                    generation=1,
                    frame_index=frame_index,
                    shm_key=shm_key,
                    include_checksum=False,
                    readback_bands=not packed,
                    packed_rgba=packed,
                    packed_height=packed_height if packed else 0,
                    slot_count=slot_count,
                    request_serial=frame_index,
                )

            def record_frame_diagnostics(
                event: dict[str, object],
                frame_index: int,
                *,
                expand_ms: float,
                convert_ms: float,
                bytes_ms: float,
                frame_bytes: int,
                copies_per_frame: int,
            ) -> None:
                if on_frame_diagnostics is None:
                    return
                roundtrip_ms = (
                    time.perf_counter()
                    - request_started.pop(frame_index, time.perf_counter())
                ) * 1000.0
                native_accounted_ms = sum(
                    float(event.get(field, 0.0) or 0.0)
                    for field in (
                        "render_ms",
                        "readback_ms",
                        "native_pack_ms",
                        "shm_copy_ms",
                    )
                )
                on_frame_diagnostics(
                    {
                        "frame_index": frame_index,
                        "t_ms": int(event.get("t_ms", 0) or 0),
                        "worker_index": int(event.get("worker_index", 0) or 0),
                        "native_render_ms": float(event.get("render_ms", 0.0) or 0.0),
                        "animation_layout_ms": float(
                            event.get("animation_layout_ms", 0.0) or 0.0
                        ),
                        "geometry_ms": float(event.get("geometry_ms", 0.0) or 0.0),
                        "stroke_ms": float(event.get("stroke_ms", 0.0) or 0.0),
                        "glow_ms": float(event.get("glow_ms", 0.0) or 0.0),
                        "end_draw_wait_ms": float(
                            event.get("end_draw_wait_ms", 0.0) or 0.0
                        ),
                        "end_draw_glow_source_ms": float(
                            event.get("end_draw_glow_source_ms", 0.0) or 0.0
                        ),
                        "end_draw_ruby_glow_source_ms": float(
                            event.get("end_draw_ruby_glow_source_ms", 0.0) or 0.0
                        ),
                        "end_draw_inline_glow_source_ms": float(
                            event.get("end_draw_inline_glow_source_ms", 0.0) or 0.0
                        ),
                        "end_draw_frame_layers_ms": float(
                            event.get("end_draw_frame_layers_ms", 0.0) or 0.0
                        ),
                        "end_draw_empty_frame_ms": float(
                            event.get("end_draw_empty_frame_ms", 0.0) or 0.0
                        ),
                        "end_draw_count": int(event.get("end_draw_count", 0) or 0),
                        "end_draw_glow_source_count": int(
                            event.get("end_draw_glow_source_count", 0) or 0
                        ),
                        "end_draw_ruby_glow_source_count": int(
                            event.get("end_draw_ruby_glow_source_count", 0) or 0
                        ),
                        "end_draw_inline_glow_source_count": int(
                            event.get("end_draw_inline_glow_source_count", 0) or 0
                        ),
                        "end_draw_frame_layers_count": int(
                            event.get("end_draw_frame_layers_count", 0) or 0
                        ),
                        "end_draw_empty_frame_count": int(
                            event.get("end_draw_empty_frame_count", 0) or 0
                        ),
                        "glow_source_area_px": int(
                            event.get("glow_source_area_px", 0) or 0
                        ),
                        "layer_push": int(event.get("layer_push", 0) or 0),
                        "gpu_wait_ms": float(event.get("gpu_wait_ms", 0.0) or 0.0),
                        "readback_copy_ms": float(
                            event.get("readback_copy_ms", 0.0) or 0.0
                        ),
                        "readback_ms": float(event.get("readback_ms", 0.0) or 0.0),
                        "native_pack_ms": float(
                            event.get("native_pack_ms", 0.0) or 0.0
                        ),
                        "shm_copy_ms": float(event.get("shm_copy_ms", 0.0) or 0.0),
                        "protocol_roundtrip_ms": roundtrip_ms,
                        "protocol_wait_ms": max(
                            roundtrip_ms - native_accounted_ms, 0.0
                        ),
                        "python_expand_ms": expand_ms,
                        "python_convert_ms": convert_ms,
                        "python_bytes_ms": bytes_ms,
                        "bytes_per_frame": frame_bytes,
                        "copies_per_frame": copies_per_frame,
                        "readback_ratio": float(
                            event.get("readback_ratio", 1.0) or 0.0
                        ),
                    }
                )

            def consume_event(event: dict[str, object], frame_index: int) -> bytes:
                nonlocal reader
                if event.get("event") != "gpu_frame_ready":
                    raise NativeRendererError(
                        "GPU export frame was not delivered: "
                        f"{event.get('event', 'unknown')}"
                    )
                if reader is None:
                    reader = SharedFrameRingReader.from_event(event)
                    reader.attach()
                expand_started = time.perf_counter()
                try:
                    image = reader.read_qimage(event)
                except RuntimeError as exc:
                    raise NativeRendererError(
                        f"failed to consume GPU subtitle frame {frame_index}: {exc}"
                    ) from exc
                expand_ms = (time.perf_counter() - expand_started) * 1000.0
                convert_started = time.perf_counter()
                image = image.convertToFormat(QImage.Format.Format_RGBA8888)
                convert_ms = (time.perf_counter() - convert_started) * 1000.0
                bytes_started = time.perf_counter()
                bits = image.constBits()
                bits.setsize(image.sizeInBytes())
                frame = bytes(bits)
                bytes_ms = (time.perf_counter() - bytes_started) * 1000.0
                record_frame_diagnostics(
                    event,
                    frame_index,
                    expand_ms=expand_ms,
                    convert_ms=convert_ms,
                    bytes_ms=bytes_ms,
                    frame_bytes=len(frame),
                    copies_per_frame=4,
                )
                return frame

            if active_workers == 1:
                begin_frame(0)
                for frame_index in range(frame_total):
                    if should_cancel is not None and should_cancel():
                        raise ExportCancelled("已停止导出。")
                    event = renderer.finish_render_gpu_frame()
                    if frame_index + 1 < frame_total:
                        begin_frame(frame_index + 1)
                    if packed:
                        if reader is None:
                            reader = SharedFrameRingReader.from_event(event)
                            reader.attach()
                        with reader.borrow_packed_rgba_view(event) as frame:
                            record_frame_diagnostics(
                                event,
                                frame_index,
                                expand_ms=0.0,
                                convert_ms=0.0,
                                bytes_ms=0.0,
                                frame_bytes=len(frame),
                                copies_per_frame=2,
                            )
                            yield frame
                    else:
                        yield consume_event(event, frame_index)
            else:
                # Each slot owns the arithmetic sequence ``slot + n * slots``.
                # A slot is reused only after its previous QImage has been
                # copied, while a one-window reorder bound prevents a fast
                # worker from buffering an unbounded number of 4K frames.
                in_flight: set[int] = set()
                free_slots: set[int] = set()
                next_for_slot = [slot for slot in range(slot_count)]
                pending: dict[int, bytes | dict[str, object]] = {}
                next_emit = 0

                def submit_slot(slot: int) -> None:
                    frame_index = next_for_slot[slot]
                    begin_frame(frame_index)
                    in_flight.add(frame_index)
                    next_for_slot[slot] += slot_count

                for slot in range(min(slot_count, frame_total)):
                    submit_slot(slot)

                while next_emit < frame_total:
                    if should_cancel is not None and should_cancel():
                        raise ExportCancelled("已停止导出。")
                    event = renderer.finish_render_gpu_frame()
                    frame_index = int(event.get("frame_index", -1) or 0)
                    if frame_index not in in_flight or frame_index in pending:
                        raise NativeRendererError(
                            f"unexpected GPU export frame index: {frame_index}"
                        )
                    in_flight.remove(frame_index)
                    slot = frame_index % slot_count
                    if packed:
                        if reader is None:
                            reader = SharedFrameRingReader.from_event(event)
                            reader.attach()
                        # Keep the slot reserved until its borrowed view has
                        # been consumed by ffmpeg. This allows out-of-order GPU
                        # completion without copying packed RGBA in Python or
                        # letting a worker overwrite a still-pending slot.
                        pending[frame_index] = event
                    else:
                        pending[frame_index] = consume_event(event, frame_index)
                        free_slots.add(slot)

                    while next_emit in pending:
                        completed = pending.pop(next_emit)
                        completed_slot = next_emit % slot_count
                        if packed:
                            assert isinstance(completed, dict)
                            assert reader is not None
                            with reader.borrow_packed_rgba_view(completed) as frame:
                                record_frame_diagnostics(
                                    completed,
                                    next_emit,
                                    expand_ms=0.0,
                                    convert_ms=0.0,
                                    bytes_ms=0.0,
                                    frame_bytes=len(frame),
                                    copies_per_frame=2,
                                )
                                yield frame
                            free_slots.add(completed_slot)
                        else:
                            assert isinstance(completed, bytes)
                            yield completed
                        next_emit += 1

                    reorder_limit = next_emit + slot_count
                    for free_slot in sorted(tuple(free_slots)):
                        candidate = next_for_slot[free_slot]
                        if candidate >= frame_total:
                            free_slots.remove(free_slot)
                            continue
                        if candidate >= reorder_limit:
                            continue
                        submit_slot(free_slot)
                        free_slots.remove(free_slot)
            if on_diagnostics is not None:
                on_diagnostics(renderer.gpu_diagnostics(force_warp=force_warp))
    finally:
        if reader is not None:
            reader.close()
