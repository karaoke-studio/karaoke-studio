"""Process wrapper for the native subtitle renderer sidecar.

The C1 sidecar is deliberately optional.  Callers can probe availability and
fall back to the Python QPainter renderer when the executable has not been
built or when the process fails.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
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

    def __init__(self, executable_path: str | os.PathLike[str] | None = None) -> None:
        resolved = resolve_native_renderer_path(executable_path)
        if resolved is None:
            raise NativeRendererError("native subtitle renderer executable was not found")
        self.executable_path = resolved
        self._process: subprocess.Popen[str] | None = None

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
            text=True,
            encoding="utf-8",
        )
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
                self._read_response()
        finally:
            if process.poll() is None:
                process.terminate()
            self._process = None

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

    def _send(self, payload: dict[str, Any]) -> None:
        process = self._require_process()
        assert process.stdin is not None
        process.stdin.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
        process.stdin.flush()

    def _read_response(self) -> dict[str, Any]:
        process = self._require_process()
        assert process.stdout is not None
        line = process.stdout.readline()
        if not line:
            stderr = ""
            if process.stderr is not None:
                stderr = process.stderr.read()
            raise NativeRendererError(f"native renderer exited without a response: {stderr}")
        try:
            return json.loads(line)
        except json.JSONDecodeError as exc:
            raise NativeRendererError(f"invalid native renderer response: {line!r}") from exc

    def _expect_ok(self, response: dict[str, Any]) -> dict[str, Any]:
        if response.get("ok"):
            return response
        raise NativeRendererError(str(response.get("error") or response))

    def _require_process(self) -> subprocess.Popen[str]:
        if not self.is_running or self._process is None:
            raise NativeRendererError("native renderer process is not running")
        return self._process
