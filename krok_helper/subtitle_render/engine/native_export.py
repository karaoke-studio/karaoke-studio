"""Native sidecar frame source for export experiments.

C6 starts with a narrow adapter: Python still owns ffmpeg, progress, cleanup,
and fallback policy, while the native sidecar renders full RGBA frames in
timestamp ranges.  Strip/band export optimizations remain on the Python path
until the full-frame path is proven stable.
"""

from __future__ import annotations

import os
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
        renderer.configure(track, style, width=width, height=height, fps=fps)
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
    renderer_path: str | os.PathLike[str] | None = None,
    extra_tracks: list[TimingTrack] | None = None,
    force_warp: bool = False,
    should_cancel: Callable[[], bool] | None = None,
) -> Iterator[bytes]:
    """Yield straight RGBA frames from the Direct2D GPU export path.

    Direct2D reads back only the renderer's conservative subtitle bands.  The
    shared-memory reader expands those bands into a transparent QImage before
    converting premultiplied BGRA to ffmpeg's straight RGBA input format.
    """
    from PyQt6.QtGui import QImage

    frame_total = max(int(total_frames), 0)
    if frame_total <= 0:
        return
    shm_key = f"krok-gpu-export-{os.getpid()}-{uuid.uuid4().hex}"
    reader: SharedFrameRingReader | None = None
    try:
        with NativeRendererProcess(
            renderer_path,
            response_timeout_s=15.0,
            close_timeout_s=1.0,
        ) as renderer:
            renderer.configure_gpu(
                track,
                style,
                width=width,
                height=height,
                fps=fps,
                force_warp=force_warp,
                extra_tracks=extra_tracks,
            )
            for frame_index in range(frame_total):
                if should_cancel is not None and should_cancel():
                    raise ExportCancelled("已停止导出。")
                t_ms = int(round(frame_index * 1000 / max(int(fps), 1)))
                event = renderer.render_gpu_frame(
                    t_ms,
                    force_warp=force_warp,
                    generation=1,
                    frame_index=frame_index,
                    shm_key=shm_key,
                    include_checksum=False,
                    readback_bands=True,
                )
                if reader is None:
                    reader = SharedFrameRingReader.from_event(event)
                    reader.attach()
                try:
                    image = reader.read_qimage(event).convertToFormat(
                        QImage.Format.Format_RGBA8888
                    )
                except RuntimeError as exc:
                    raise NativeRendererError(
                        f"failed to consume GPU subtitle frame {frame_index}: {exc}"
                    ) from exc
                bits = image.constBits()
                bits.setsize(image.sizeInBytes())
                yield bytes(bits)
    finally:
        if reader is not None:
            reader.close()
