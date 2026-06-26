from __future__ import annotations

from dataclasses import dataclass

import pytest

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
