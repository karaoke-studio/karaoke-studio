"""Process wrapper for the native subtitle renderer sidecar.

The C1 sidecar is deliberately optional.  Callers can probe availability and
fall back to the Python QPainter renderer when the executable has not been
built or when the process fails.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from krok_helper.subtitle_render.models import Style, TimingTrack
from krok_helper.subtitle_render.native_protocol import build_render_ir

_EXE_NAME = "krok_subtitle_renderer.exe" if os.name == "nt" else "krok_subtitle_renderer"


class NativeRendererError(RuntimeError):
    """Raised when the native sidecar reports an error or exits unexpectedly."""


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
    """Resolve a usable sidecar executable, honoring ``KROK_SUBTITLE_NATIVE_RENDERER``."""
    candidates: list[Path] = []
    if executable_path is not None:
        candidates.append(Path(executable_path))
    env_path = os.environ.get("KROK_SUBTITLE_NATIVE_RENDERER")
    if env_path:
        candidates.append(Path(env_path))
    candidates.append(bundled_native_renderer_path(root))
    if root is not None:
        candidates.append(default_native_renderer_path(root))

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
        self._stderr_lock = threading.Lock()
        self._stdout_noise_lock = threading.Lock()
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

    def _send(self, payload: dict[str, Any]) -> None:
        process = self._require_process()
        assert process.stdin is not None
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
