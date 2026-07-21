from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager

import pytest
from PyQt6.QtGui import QColor, QImage

from krok_helper.errors import ExportCancelled
from krok_helper.subtitle_render.engine import native_export as ne
from krok_helper.subtitle_render.models import Style, TimingChar, TimingLine, TimingTrack


def _track() -> TimingTrack:
    return TimingTrack(lines=[TimingLine(chars=[TimingChar("a", 0)], end_ms=1000)])


def test_native_export_timestamps_match_python_frame_cadence() -> None:
    assert ne.native_export_timestamps(start_frame=0, count=4, fps=2) == [0, 500, 1000, 1500]
    assert ne.native_export_timestamps(start_frame=2, count=3, fps=60) == [33, 50, 67]


def test_native_export_chunk_frames_respects_memory_target() -> None:
    assert ne.native_export_chunk_frames(width=100, height=100, total_frames=100, target_bytes=80_000) == 2
    assert ne.native_export_chunk_frames(width=100, height=100, total_frames=1, target_bytes=1) == 1


def test_native_export_chunk_frames_ignores_invalid_env(monkeypatch) -> None:
    monkeypatch.setenv("KROK_SUBTITLE_NATIVE_EXPORT_CHUNK_BYTES", "bad")

    assert ne.native_export_chunk_frames(width=100, height=100, total_frames=100) == 64


def test_gpu_export_realization_capacity_uses_bounded_memory_budget(monkeypatch) -> None:
    monkeypatch.delenv("KROK_SUBTITLE_GPU_EXPORT_REALIZATION_MEMORY_MB", raising=False)
    assert ne.gpu_export_realization_capacity() == 65_536

    monkeypatch.setenv("KROK_SUBTITLE_GPU_EXPORT_REALIZATION_MEMORY_MB", "32")
    assert ne.gpu_export_realization_capacity() == 8_192

    monkeypatch.setenv("KROK_SUBTITLE_GPU_EXPORT_REALIZATION_MEMORY_MB", "4096")
    assert ne.gpu_export_realization_capacity() == 262_144

    monkeypatch.setenv("KROK_SUBTITLE_GPU_EXPORT_REALIZATION_MEMORY_MB", "invalid")
    assert ne.gpu_export_realization_capacity() == 65_536


class _FakeRealizationRenderer:
    def __init__(self, diagnostics: list[dict[str, object]]) -> None:
        self.diagnostics = list(diagnostics)
        self.force_warp: list[bool] = []

    def gpu_diagnostics(self, *, force_warp: bool = False) -> dict[str, object]:
        self.force_warp.append(force_warp)
        return self.diagnostics.pop(0)


def test_gpu_export_waits_for_realization_prewarm_and_reports_progress(
    monkeypatch,
) -> None:
    monkeypatch.setattr(ne.time, "sleep", lambda _seconds: None)
    renderer = _FakeRealizationRenderer(
        [
            {
                "realization_prewarm_complete": False,
                "realization_prewarm_tasks": 10,
                "realization_count": 4,
            },
            {
                "realization_prewarm_complete": True,
                "realization_prewarm_tasks": 10,
                "realization_count": 10,
            },
        ]
    )
    progress: list[tuple[int, int]] = []

    ne._wait_for_gpu_export_realizations(
        renderer,
        {
            "realization_prewarm_complete": False,
            "realization_prewarm_tasks": 10,
            "realization_count": 0,
        },
        force_warp=False,
        should_cancel=lambda: False,
        on_progress=lambda done, total: progress.append((done, total)),
    )

    assert progress == [(0, 10), (4, 10), (10, 10), (10, 10)]
    assert renderer.force_warp == [False, False]


def test_gpu_export_realization_wait_is_cancellable(monkeypatch) -> None:
    monkeypatch.setattr(
        ne.time,
        "sleep",
        lambda _seconds: pytest.fail("cancel must be checked before sleeping"),
    )

    with pytest.raises(ExportCancelled):
        ne._wait_for_gpu_export_realizations(
            _FakeRealizationRenderer([]),
            {
                "realization_prewarm_complete": False,
                "realization_prewarm_tasks": 10,
                "realization_count": 0,
            },
            force_warp=False,
            should_cancel=lambda: True,
            on_progress=None,
        )


@dataclass(frozen=True)
class _FakeSlot:
    width: int
    height: int
    stride: int
    payload: bytes
    frame_index: int = 0


def test_shared_slot_rgba_bytes_packs_strided_rows() -> None:
    slot = _FakeSlot(
        width=2,
        height=2,
        stride=12,
        payload=bytes(
            [
                1, 2, 3, 4, 5, 6, 7, 8, 99, 99, 99, 99,
                9, 10, 11, 12, 13, 14, 15, 16, 88, 88, 88, 88,
            ]
        ),
    )

    assert ne.shared_slot_rgba_bytes(slot) == bytes(
        [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16]
    )


class _FakeRingReader:
    def __init__(self, event):
        self._event = event

    @classmethod
    def from_event(cls, event):
        return cls(event)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return None

    def read_frame(self, event):
        frame_index = int(event["frame_index"])
        return _FakeSlot(
            width=1,
            height=1,
            stride=4,
            payload=bytes([frame_index, frame_index, frame_index, 255]),
            frame_index=frame_index,
        )


class _FakeNativeRendererProcess:
    instances = []

    def __init__(self, *args, **kwargs):
        self.configures = []
        self.ranges = []
        self.events = []
        self.cancels = []
        _FakeNativeRendererProcess.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return None

    def configure(self, *args, **kwargs):
        self.configures.append((args, kwargs))
        return {"ok": True, "event": "configured"}

    def start_render_range(self, timestamps, *, generation, threads, shm_key=None, ring_slots=3):
        index = len(self.ranges)
        self.ranges.append(
            {
                "timestamps": list(timestamps),
                "generation": generation,
                "threads": threads,
                "shm_key": shm_key,
                "ring_slots": ring_slots,
            }
        )
        for frame_index, t_ms in reversed(list(enumerate(timestamps))):
            self.events.append(
                {
                    "ok": True,
                    "event": "frame_ready",
                    "generation": generation,
                    "frame_index": frame_index,
                    "t_ms": int(t_ms),
                    "payload": "shared_memory",
                    "shm_key": shm_key,
                }
            )
        self.events.append(
            {
                "ok": True,
                "event": "range_done",
                "generation": generation,
                "frames": len(timestamps),
                "chunk": index,
            }
        )
        return {"ok": True, "event": "range_started"}

    def read_event(self):
        return self.events.pop(0)

    def send_cancel_generation(self, generation):
        self.cancels.append(int(generation))


def test_iter_native_rgba_frames_yields_ordered_bytes_across_chunks(monkeypatch) -> None:
    _FakeNativeRendererProcess.instances.clear()
    monkeypatch.setattr(ne, "NativeRendererProcess", _FakeNativeRendererProcess)
    monkeypatch.setattr(ne, "SharedFrameRingReader", _FakeRingReader)

    frames = list(
        ne.iter_native_rgba_frames(
            _track(),
            Style(),
            width=1,
            height=1,
            fps=2,
            total_frames=3,
            threads=2,
            chunk_frames=2,
        )
    )

    assert frames == [
        bytes([0, 0, 0, 255]),
        bytes([1, 1, 1, 255]),
        bytes([0, 0, 0, 255]),
    ]
    process = _FakeNativeRendererProcess.instances[-1]
    assert process.ranges[0]["timestamps"] == [0, 500]
    assert process.ranges[0]["ring_slots"] == 2
    assert process.ranges[1]["timestamps"] == [1000]
    assert process.configures[0][1] == {"width": 1, "height": 1, "fps": 2}


def test_iter_native_rgba_frames_sends_cancel_when_cancelled_mid_range(monkeypatch) -> None:
    _FakeNativeRendererProcess.instances.clear()
    monkeypatch.setattr(ne, "NativeRendererProcess", _FakeNativeRendererProcess)
    monkeypatch.setattr(ne, "SharedFrameRingReader", _FakeRingReader)
    calls = {"count": 0}

    def should_cancel() -> bool:
        calls["count"] += 1
        return calls["count"] >= 2

    with pytest.raises(ExportCancelled):
        list(
            ne.iter_native_rgba_frames(
                _track(),
                Style(),
                width=1,
                height=1,
                fps=2,
                total_frames=2,
                threads=1,
                chunk_frames=2,
                should_cancel=should_cancel,
            )
        )

    assert _FakeNativeRendererProcess.instances[-1].cancels == [1]


def test_iter_native_rgba_frames_at_times_uses_explicit_timestamps(monkeypatch) -> None:
    _FakeNativeRendererProcess.instances.clear()
    monkeypatch.setattr(ne, "NativeRendererProcess", _FakeNativeRendererProcess)
    monkeypatch.setattr(ne, "SharedFrameRingReader", _FakeRingReader)

    frames = list(
        ne.iter_native_rgba_frames_at_times(
            _track(),
            Style(),
            [167, 500, 833],
            width=1,
            height=1,
            fps=60,
            threads=2,
            chunk_frames=3,
        )
    )

    assert frames == [
        (167, bytes([0, 0, 0, 255])),
        (500, bytes([1, 1, 1, 255])),
        (833, bytes([2, 2, 2, 255])),
    ]
    process = _FakeNativeRendererProcess.instances[-1]
    assert process.ranges[0]["timestamps"] == [167, 500, 833]


class _FakeGpuRingReader:
    instances = []

    def __init__(self, event):
        self.event = event
        self.attached = False
        self.closed = False
        _FakeGpuRingReader.instances.append(self)

    @classmethod
    def from_event(cls, event):
        return cls(event)

    def attach(self):
        self.attached = True

    def close(self):
        self.closed = True

    def read_qimage(self, event):
        image = QImage(1, 1, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(QColor(20 + int(event["frame_index"]), 40, 60, 128))
        return image

    @contextmanager
    def borrow_packed_rgba_view(self, event):
        frame_index = int(event["frame_index"])
        payload = memoryview(bytes([20 + frame_index, 40, 60, 128]))
        try:
            yield payload
        finally:
            payload.release()


class _FakeGpuRendererProcess:
    instances = []
    reverse_completions = False

    def __init__(self, *args, **kwargs):
        self.configures = []
        self.frames = []
        self.pending = []
        self.max_pending = 0
        _FakeGpuRendererProcess.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return None

    def configure_gpu(self, *args, **kwargs):
        self.configures.append((args, kwargs))
        return {
            "ok": True,
            "event": "gpu_configured",
            "realization_prewarm_complete": True,
            "realization_prewarm_tasks": 0,
            "realization_count": 0,
            "worker_count": int(kwargs.get("worker_count", 1)),
        }

    def begin_render_gpu_frame(self, t_ms, **kwargs):
        self.frames.append((t_ms, kwargs))
        self.pending.append(
            {
                "ok": True,
                "event": "gpu_frame_ready",
                "frame_index": kwargs["frame_index"],
                "shm_key": kwargs["shm_key"],
            }
        )
        self.max_pending = max(self.max_pending, len(self.pending))

    def finish_render_gpu_frame(self):
        return self.pending.pop(-1 if self.reverse_completions else 0)

    def gpu_diagnostics(self, *, force_warp=False):
        return {
            "ok": True,
            "event": "gpu_diagnostics",
            "force_warp": force_warp,
            "local_video_memory_usage_bytes": 123,
        }

    def render_gpu_frame(self, t_ms, **kwargs):
        self.begin_render_gpu_frame(t_ms, **kwargs)
        return self.finish_render_gpu_frame()


def test_iter_gpu_rgba_frames_uses_banded_readback_and_straight_rgba(monkeypatch) -> None:
    _FakeGpuRendererProcess.instances.clear()
    _FakeGpuRingReader.instances.clear()
    monkeypatch.setattr(ne, "NativeRendererProcess", _FakeGpuRendererProcess)
    monkeypatch.setattr(ne, "SharedFrameRingReader", _FakeGpuRingReader)
    prepare_progress: list[tuple[int, int]] = []
    frame_diagnostics: list[dict[str, object]] = []

    frames = list(
        ne.iter_gpu_rgba_frames(
            _track(),
            Style(),
            width=1,
            height=1,
            fps=2,
            total_frames=2,
            extra_tracks=[_track()],
            force_warp=True,
            on_prepare_progress=lambda done, total: prepare_progress.append(
                (done, total)
            ),
            on_frame_diagnostics=frame_diagnostics.append,
        )
    )

    assert frames == [bytes([20, 40, 60, 128]), bytes([22, 40, 60, 128])]
    process = _FakeGpuRendererProcess.instances[-1]
    assert process.configures[0][1]["extra_tracks"] == [_track()]
    assert process.configures[0][1]["realization_enabled"] is False
    assert prepare_progress == []
    assert [item[0] for item in process.frames] == [0, 500]
    assert all(item[1]["readback_bands"] is True for item in process.frames)
    assert all(item[1]["include_checksum"] is False for item in process.frames)
    # G7 pipelining: two-slot ring, at most one request in flight beyond the
    # frame currently being consumed, and no leftover pending responses.
    assert all(item[1]["slot_count"] == 2 for item in process.frames)
    assert process.max_pending == 1
    assert process.pending == []
    assert _FakeGpuRingReader.instances[-1].attached is True
    assert _FakeGpuRingReader.instances[-1].closed is True
    assert [row["frame_index"] for row in frame_diagnostics] == [0, 1]
    assert all(row["bytes_per_frame"] == 4 for row in frame_diagnostics)
    assert all(row["copies_per_frame"] == 4 for row in frame_diagnostics)


def test_iter_gpu_rgba_frames_reorders_bounded_multiworker_results(monkeypatch) -> None:
    _FakeGpuRendererProcess.instances.clear()
    _FakeGpuRingReader.instances.clear()
    monkeypatch.setattr(ne, "NativeRendererProcess", _FakeGpuRendererProcess)
    monkeypatch.setattr(ne, "SharedFrameRingReader", _FakeGpuRingReader)

    _FakeGpuRendererProcess.reverse_completions = False
    serial = list(
        ne.iter_gpu_rgba_frames(
            _track(), Style(), width=1, height=1, fps=4, total_frames=6
        )
    )
    _FakeGpuRendererProcess.reverse_completions = True
    diagnostics = []
    try:
        parallel = list(
            ne.iter_gpu_rgba_frames(
                _track(),
                Style(),
                width=1,
                height=1,
                fps=4,
                total_frames=6,
                worker_count=2,
                on_diagnostics=diagnostics.append,
            )
        )
    finally:
        _FakeGpuRendererProcess.reverse_completions = False

    assert parallel == serial
    process = _FakeGpuRendererProcess.instances[-1]
    assert process.configures[0][1]["worker_count"] == 2
    assert process.configures[0][1]["realization_enabled"] is False
    assert process.max_pending <= 2
    assert process.pending == []
    assert all(item[1]["slot_count"] == 2 for item in process.frames)
    assert [item[1]["request_serial"] for item in process.frames] == list(range(6))
    assert diagnostics[-1]["local_video_memory_usage_bytes"] == 123


def test_iter_gpu_rgba_frames_packed_path_borrows_rgba_without_qimage(
    monkeypatch,
) -> None:
    _FakeGpuRendererProcess.instances.clear()
    _FakeGpuRingReader.instances.clear()
    monkeypatch.setattr(ne, "NativeRendererProcess", _FakeGpuRendererProcess)
    monkeypatch.setattr(ne, "SharedFrameRingReader", _FakeGpuRingReader)
    rows = []

    frames = [
        bytes(frame)
        for frame in ne.iter_gpu_rgba_frames(
            _track(),
            Style(),
            width=1,
            height=1,
            fps=2,
            total_frames=2,
            worker_count=4,
            packed_rgba=True,
            bands=[(0, 1)],
            on_frame_diagnostics=rows.append,
        )
    ]

    assert frames == [bytes([20, 40, 60, 128]), bytes([21, 40, 60, 128])]
    process = _FakeGpuRendererProcess.instances[-1]
    assert process.configures[0][1]["worker_count"] == 4
    assert process.configures[0][1]["export_bands"] == [(0, 1)]
    assert all(item[1]["packed_rgba"] is True for item in process.frames)
    assert all(item[1]["readback_bands"] is False for item in process.frames)
    assert all(row["python_expand_ms"] == 0.0 for row in rows)
    assert all(row["python_convert_ms"] == 0.0 for row in rows)
    assert all(row["python_bytes_ms"] == 0.0 for row in rows)
    assert all(row["copies_per_frame"] == 2 for row in rows)


def test_iter_gpu_rgba_frames_packed_multiworker_reorders_without_python_copy(
    monkeypatch,
) -> None:
    _FakeGpuRendererProcess.instances.clear()
    _FakeGpuRingReader.instances.clear()
    monkeypatch.setattr(ne, "NativeRendererProcess", _FakeGpuRendererProcess)
    monkeypatch.setattr(ne, "SharedFrameRingReader", _FakeGpuRingReader)
    _FakeGpuRendererProcess.reverse_completions = True
    rows = []
    try:
        frames = [
            bytes(frame)
            for frame in ne.iter_gpu_rgba_frames(
                _track(),
                Style(),
                width=1,
                height=1,
                fps=4,
                total_frames=6,
                worker_count=2,
                packed_rgba=True,
                bands=[(0, 1)],
                on_frame_diagnostics=rows.append,
            )
        ]
    finally:
        _FakeGpuRendererProcess.reverse_completions = False

    assert frames == [bytes([20 + index, 40, 60, 128]) for index in range(6)]
    process = _FakeGpuRendererProcess.instances[-1]
    assert process.configures[0][1]["worker_count"] == 2
    assert process.max_pending <= 2
    assert process.pending == []
    assert [row["frame_index"] for row in rows] == list(range(6))
    assert all(row["copies_per_frame"] == 2 for row in rows)


def test_iter_gpu_rgba_frames_multiworker_cancel_closes_transport(monkeypatch) -> None:
    _FakeGpuRendererProcess.instances.clear()
    _FakeGpuRingReader.instances.clear()
    monkeypatch.setattr(ne, "NativeRendererProcess", _FakeGpuRendererProcess)
    monkeypatch.setattr(ne, "SharedFrameRingReader", _FakeGpuRingReader)

    with pytest.raises(ExportCancelled):
        list(
            ne.iter_gpu_rgba_frames(
                _track(),
                Style(),
                width=1,
                height=1,
                fps=4,
                total_frames=6,
                worker_count=4,
                should_cancel=lambda: True,
            )
        )

    process = _FakeGpuRendererProcess.instances[-1]
    assert process.max_pending == 4
    assert _FakeGpuRingReader.instances == []
