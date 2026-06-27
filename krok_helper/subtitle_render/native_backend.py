"""Process wrapper for the native subtitle renderer sidecar.

The C1 sidecar is deliberately optional.  Callers can probe availability and
fall back to the Python QPainter renderer when the executable has not been
built or when the process fails.
"""

from __future__ import annotations

import json
import os
import queue
import struct
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from krok_helper.subtitle_render.models import Style, TimingTrack
from krok_helper.subtitle_render.native_protocol import build_render_ir

_EXE_NAME = "krok_subtitle_renderer.exe" if os.name == "nt" else "krok_subtitle_renderer"
_SHARED_FRAME_HEADER = struct.Struct("<10i")
_SHARED_FRAME_READY = 2
_SHARED_FRAME_PIXEL_FORMATS = {
    1: "rgba8888",
}


class NativeRendererError(RuntimeError):
    """Raised when the native sidecar reports an error or exits unexpectedly."""


@dataclass(frozen=True)
class SharedFrameSlot:
    """A copied RGBA frame read from one native shared-memory ring slot."""

    shm_key: str
    slot_index: int
    generation: int
    frame_index: int
    t_ms: int
    width: int
    height: int
    stride: int
    pixel_format: str
    payload: bytes

    def to_qimage(self):
        """Return a detached ``QImage`` backed by this slot payload copy."""
        if self.pixel_format != "rgba8888":
            raise NativeRendererError(f"unsupported shared frame pixel format: {self.pixel_format}")
        from PyQt6.QtGui import QImage

        image = QImage(
            self.payload,
            self.width,
            self.height,
            self.stride,
            QImage.Format.Format_RGBA8888,
        )
        if image.isNull():
            raise NativeRendererError("failed to construct QImage from shared frame payload")
        return image.copy()


class SharedFrameRingReader:
    """Attach to the native renderer's ``QSharedMemory`` ring and copy ready slots."""

    def __init__(self, shm_key: str) -> None:
        if not shm_key:
            raise NativeRendererError("shared memory key is empty")
        self.shm_key = str(shm_key)
        self._shared = None

    @classmethod
    def from_event(cls, frame_ready_event: dict[str, Any]) -> "SharedFrameRingReader":
        return cls(str(frame_ready_event.get("shm_key") or ""))

    @property
    def is_attached(self) -> bool:
        return bool(self._shared is not None and self._shared.isAttached())

    def attach(self) -> None:
        if self.is_attached:
            return
        from PyQt6.QtCore import QSharedMemory

        shared = QSharedMemory(self.shm_key)
        if not shared.attach(QSharedMemory.AccessMode.ReadOnly):
            raise NativeRendererError(
                f"failed to attach native shared memory {self.shm_key!r}: {shared.errorString()}"
            )
        self._shared = shared

    def close(self) -> None:
        if self._shared is not None and self._shared.isAttached():
            self._shared.detach()
        self._shared = None

    def __enter__(self) -> "SharedFrameRingReader":
        self.attach()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def read_frame(self, frame_ready_event: dict[str, Any]) -> SharedFrameSlot:
        """Copy and validate the slot described by a ``frame_ready`` event."""
        self._validate_event_payload(frame_ready_event)
        self.attach()
        assert self._shared is not None

        slot_offset = _event_int(frame_ready_event, "slot_offset")
        header_bytes = _event_int(frame_ready_event, "header_bytes")
        payload_offset = _event_int(frame_ready_event, "payload_offset")
        payload_bytes = _event_int(frame_ready_event, "payload_bytes")
        slot_index = _event_int(frame_ready_event, "slot_index")
        slot_bytes = _event_int(frame_ready_event, "slot_bytes")
        if header_bytes < _SHARED_FRAME_HEADER.size:
            raise NativeRendererError(f"shared frame header is too small: {header_bytes}")
        if slot_offset < 0 or payload_offset < 0 or payload_bytes < 0 or slot_bytes <= 0:
            raise NativeRendererError("shared frame event contains invalid slot bounds")
        if payload_offset < slot_offset + header_bytes:
            raise NativeRendererError("shared frame payload overlaps slot header")

        shared_size = int(self._shared.size())
        required_size = payload_offset + payload_bytes
        header_end = slot_offset + header_bytes
        slot_end = slot_offset + slot_bytes
        if header_end > shared_size or slot_end > shared_size:
            raise NativeRendererError("shared frame slot exceeds shared memory size")
        if required_size > shared_size:
            raise NativeRendererError(
                f"shared frame payload exceeds shared memory size: {required_size} > {shared_size}"
            )

        if not self._shared.lock():
            raise NativeRendererError(
                f"failed to lock native shared memory {self.shm_key!r}: {self._shared.errorString()}"
            )
        try:
            pointer = self._shared.constData()
            pointer.setsize(shared_size)
            view = memoryview(pointer)
            header_snapshot = bytes(view[slot_offset : slot_offset + _SHARED_FRAME_HEADER.size])
            payload = bytes(view[payload_offset:required_size])
        finally:
            self._shared.unlock()

        header = _SHARED_FRAME_HEADER.unpack(header_snapshot)
        (
            state,
            generation,
            frame_index,
            t_ms,
            width,
            height,
            stride,
            format_id,
            header_payload_offset,
            header_payload_bytes,
        ) = header
        if state != _SHARED_FRAME_READY:
            raise NativeRendererError(f"shared frame slot is not ready: state={state}")
        pixel_format = _SHARED_FRAME_PIXEL_FORMATS.get(format_id)
        if pixel_format is None:
            raise NativeRendererError(f"unsupported shared frame pixel format id: {format_id}")
        if slot_offset + header_payload_offset != payload_offset:
            raise NativeRendererError("shared frame payload offset does not match slot header")
        if header_payload_bytes != payload_bytes:
            raise NativeRendererError("shared frame payload byte count does not match slot header")
        self._validate_header_matches_event(
            frame_ready_event,
            generation=generation,
            frame_index=frame_index,
            t_ms=t_ms,
            width=width,
            height=height,
            stride=stride,
            pixel_format=pixel_format,
        )

        return SharedFrameSlot(
            shm_key=self.shm_key,
            slot_index=slot_index,
            generation=generation,
            frame_index=frame_index,
            t_ms=t_ms,
            width=width,
            height=height,
            stride=stride,
            pixel_format=pixel_format,
            payload=payload,
        )

    def _validate_event_payload(self, frame_ready_event: dict[str, Any]) -> None:
        if frame_ready_event.get("event") != "frame_ready":
            raise NativeRendererError("shared frame reader expects a frame_ready event")
        if frame_ready_event.get("payload") != "shared_memory":
            raise NativeRendererError("frame_ready event does not describe a shared memory payload")
        event_key = str(frame_ready_event.get("shm_key") or "")
        if event_key != self.shm_key:
            raise NativeRendererError(
                f"frame_ready shared memory key mismatch: {event_key!r} != {self.shm_key!r}"
            )

    def _validate_header_matches_event(
        self,
        frame_ready_event: dict[str, Any],
        *,
        generation: int,
        frame_index: int,
        t_ms: int,
        width: int,
        height: int,
        stride: int,
        pixel_format: str,
    ) -> None:
        expected = {
            "generation": generation,
            "frame_index": frame_index,
            "t_ms": t_ms,
            "width": width,
            "height": height,
            "stride": stride,
        }
        for key, actual in expected.items():
            if _event_int(frame_ready_event, key) != actual:
                raise NativeRendererError(f"shared frame slot no longer matches event field {key}")
        event_format = str(frame_ready_event.get("pixel_format") or "")
        if event_format and event_format != pixel_format:
            raise NativeRendererError(
                f"shared frame pixel format mismatch: {event_format!r} != {pixel_format!r}"
            )


def _event_int(event: dict[str, Any], key: str) -> int:
    value = event.get(key)
    if isinstance(value, bool) or value is None:
        raise NativeRendererError(f"shared frame event is missing integer field {key!r}")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise NativeRendererError(f"shared frame event field {key!r} is not an integer") from exc


def repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def default_native_renderer_path(root: Path | None = None) -> Path:
    """Return the default build-tree sidecar path used by local C1 smoke tests."""
    base = root or repository_root()
    return base / "build" / "native-renderer" / _EXE_NAME


def bundled_native_renderer_path(root: Path | None = None) -> Path:
    """Return the expected PyInstaller-bundled sidecar path."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / _EXE_NAME
    return default_native_renderer_path(root)


def resolve_native_renderer_path(
    executable_path: str | os.PathLike[str] | None = None,
    *,
    root: Path | None = None,
) -> Path | None:
    """Resolve a sidecar only for explicit protocol/test use.

    Runtime auto-discovery is intentionally disabled.  Passing an explicit
    executable keeps the protocol harness usable without allowing preview or
    export code to activate native rendering through environment variables or
    a bundled binary.
    """
    if executable_path is None:
        return None
    candidates: list[Path] = []
    candidates.append(Path(executable_path))

    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.expanduser()
        if resolved in seen:
            continue
        seen.add(resolved)
        if resolved.is_file():
            return resolved
    return None


class NativeRendererProcess:
    """Small JSON-lines client for ``krok_subtitle_renderer``."""

    def __init__(
        self,
        executable_path: str | os.PathLike[str] | None = None,
        *,
        response_timeout_s: float = 5.0,
        close_timeout_s: float = 2.0,
    ) -> None:
        resolved = resolve_native_renderer_path(executable_path)
        if resolved is None:
            raise NativeRendererError("native subtitle renderer executable was not found")
        self.executable_path = resolved
        self.response_timeout_s = max(0.1, float(response_timeout_s))
        self.close_timeout_s = max(0.1, float(close_timeout_s))
        self._process: subprocess.Popen[str] | None = None
        self._stdout_queue: queue.Queue[str | None] = queue.Queue()
        self._stderr_tail: deque[str] = deque(maxlen=80)
        self._stdout_noise_tail: deque[str] = deque(maxlen=20)
        self._event_backlog: deque[dict[str, Any]] = deque()
        self._stderr_lock = threading.Lock()
        self._stdout_noise_lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._pipe_threads: list[threading.Thread] = []

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def start(self) -> dict[str, Any]:
        if self.is_running:
            return {"ok": True, "event": "already_running"}
        self._process = subprocess.Popen(
            [str(self.executable_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=1,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        self._start_pipe_threads(self._process)
        ready = self._read_response()
        if not ready.get("ok"):
            raise NativeRendererError(f"native renderer did not become ready: {ready}")
        return ready

    def close(self) -> None:
        process = self._process
        if process is None:
            return
        try:
            if process.poll() is None:
                self._send({"cmd": "shutdown"})
                try:
                    self._read_response()
                except NativeRendererError:
                    pass
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=self.close_timeout_s)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=self.close_timeout_s)
            else:
                process.wait(timeout=0)
            self._process = None
            self._pipe_threads.clear()

    def __enter__(self) -> "NativeRendererProcess":
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def configure(
        self,
        track: TimingTrack,
        style: Style,
        *,
        width: int,
        height: int,
        fps: int,
    ) -> dict[str, Any]:
        ir = build_render_ir(track, style, width=width, height=height, fps=fps)
        self._send({"cmd": "configure", "ir": ir})
        return self._expect_ok(self._read_response())

    def render_frame_png(self, t_ms: int, output_path: str | os.PathLike[str]) -> dict[str, Any]:
        self._send(
            {
                "cmd": "render_frame",
                "t_ms": int(t_ms),
                "output_path": str(Path(output_path)),
            }
        )
        return self._expect_ok(self._read_response())

    def render_frame_stats(self, t_ms: int) -> dict[str, Any]:
        self._send(
            {
                "cmd": "render_frame_stats",
                "t_ms": int(t_ms),
            }
        )
        return self._expect_ok(self._read_response())

    def render_range_stats(self, timestamps_ms: list[int], *, threads: int) -> dict[str, Any]:
        self._send(
            {
                "cmd": "render_range_stats",
                "t_ms": [int(t_ms) for t_ms in timestamps_ms],
                "threads": int(threads),
            }
        )
        return self._expect_ok(self._read_response())

    def start_render_range(
        self,
        timestamps_ms: list[int],
        *,
        generation: int,
        threads: int,
        shm_key: str | None = None,
        ring_slots: int = 3,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "cmd": "render_range",
            "t_ms": [int(t_ms) for t_ms in timestamps_ms],
            "generation": int(generation),
            "threads": int(threads),
            "ring_slots": int(ring_slots),
        }
        if shm_key:
            payload["shm_key"] = shm_key
        self._send(
            payload
        )
        return self._expect_ok(self._read_until_event("range_started"))

    def cancel_generation(self, generation: int) -> dict[str, Any]:
        self._send({"cmd": "cancel_generation", "generation": int(generation)})
        return self._expect_ok(self._read_until_event("generation_cancelled"))

    def send_cancel_generation(self, generation: int) -> None:
        """Send cancellation without consuming stdout events.

        Preview uses this from the GUI/request side while the worker thread is
        still the sole protocol event reader.
        """
        self._send({"cmd": "cancel_generation", "generation": int(generation)})

    def read_event(self) -> dict[str, Any]:
        if self._event_backlog:
            return self._event_backlog.popleft()
        return self._read_response()

    def _send(self, payload: dict[str, Any]) -> None:
        process = self._require_process()
        assert process.stdin is not None
        with self._send_lock:
            process.stdin.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
            process.stdin.flush()

    def _read_response(self) -> dict[str, Any]:
        process = self._current_process()
        deadline = time.monotonic() + self.response_timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise NativeRendererError(self._format_timeout_error(process))
            try:
                line = self._stdout_queue.get(timeout=remaining)
            except queue.Empty as exc:
                raise NativeRendererError(self._format_timeout_error(process)) from exc

            if line is None:
                raise NativeRendererError(self._format_exit_error(process))

            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                self._remember_stdout_noise(line)
                continue
            if isinstance(payload, dict) and isinstance(payload.get("event"), str):
                return payload
            self._remember_stdout_noise(line)

    def _read_until_event(self, event: str) -> dict[str, Any]:
        kept: deque[dict[str, Any]] = deque()
        while self._event_backlog:
            payload = self._event_backlog.popleft()
            if payload.get("event") == event:
                self._event_backlog.extendleft(reversed(kept))
                return payload
            kept.append(payload)
        self._event_backlog = kept
        while True:
            payload = self._read_response()
            if payload.get("event") == event:
                return payload
            self._event_backlog.append(payload)

    def _expect_ok(self, response: dict[str, Any]) -> dict[str, Any]:
        if response.get("ok"):
            return response
        raise NativeRendererError(str(response.get("error") or response))

    def _require_process(self) -> subprocess.Popen[str]:
        if not self.is_running or self._process is None:
            raise NativeRendererError("native renderer process is not running")
        return self._process

    def _current_process(self) -> subprocess.Popen[str]:
        if self._process is None:
            raise NativeRendererError("native renderer process is not running")
        return self._process

    def _start_pipe_threads(self, process: subprocess.Popen[str]) -> None:
        assert process.stdout is not None
        assert process.stderr is not None
        self._stdout_queue = queue.Queue()
        self._stderr_tail.clear()
        self._stdout_noise_tail.clear()
        self._pipe_threads = [
            threading.Thread(
                target=self._enqueue_stdout,
                args=(process.stdout,),
                name="native-renderer-stdout",
                daemon=True,
            ),
            threading.Thread(
                target=self._drain_stderr,
                args=(process.stderr,),
                name="native-renderer-stderr",
                daemon=True,
            ),
        ]
        for thread in self._pipe_threads:
            thread.start()

    def _enqueue_stdout(self, stream: Any) -> None:
        try:
            for line in iter(stream.readline, ""):
                self._stdout_queue.put(line)
        finally:
            self._stdout_queue.put(None)

    def _drain_stderr(self, stream: Any) -> None:
        for line in iter(stream.readline, ""):
            with self._stderr_lock:
                self._stderr_tail.append(line.rstrip())

    def _remember_stdout_noise(self, line: str) -> None:
        with self._stdout_noise_lock:
            self._stdout_noise_tail.append(line.rstrip())

    def _stderr_excerpt(self) -> str:
        with self._stderr_lock:
            return "\n".join(self._stderr_tail)

    def _stdout_noise_excerpt(self) -> str:
        with self._stdout_noise_lock:
            return "\n".join(self._stdout_noise_tail)

    def _format_timeout_error(self, process: subprocess.Popen[str]) -> str:
        return (
            f"native renderer response timed out after {self.response_timeout_s:.1f}s "
            f"(returncode={process.poll()}); stderr_tail={self._stderr_excerpt()!r}; "
            f"stdout_noise={self._stdout_noise_excerpt()!r}"
        )

    def _format_exit_error(self, process: subprocess.Popen[str]) -> str:
        return (
            f"native renderer exited without a protocol response "
            f"(returncode={process.poll()}); stderr_tail={self._stderr_excerpt()!r}; "
            f"stdout_noise={self._stdout_noise_excerpt()!r}"
        )
