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
    2: "bgra8888_premultiplied",
    3: "bgra8888_premultiplied_bands",
}


def _sidecar_subprocess_kwargs(platform: str | None = None) -> dict[str, Any]:
    """Return platform-specific flags for launching the renderer sidecar."""
    if (platform or sys.platform) != "win32":
        return {}
    return {
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
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
        if self.pixel_format not in {"rgba8888", "bgra8888_premultiplied"}:
            raise NativeRendererError(f"unsupported shared frame pixel format: {self.pixel_format}")
        from PyQt6.QtGui import QImage

        image_format = (
            QImage.Format.Format_RGBA8888
            if self.pixel_format == "rgba8888"
            else QImage.Format.Format_ARGB32_Premultiplied
        )
        image = QImage(
            self.payload,
            self.width,
            self.height,
            self.stride,
            image_format,
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
        shared = self._shared
        self._shared = None
        if shared is None:
            return
        try:
            if shared.isAttached():
                shared.detach()
        except RuntimeError:
            # During exceptional QApplication teardown Qt may delete the
            # QSharedMemory wrapper before the preview worker reaches its
            # finally block. Closing must remain idempotent in that order.
            pass

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

        if pixel_format == "bgra8888_premultiplied_bands":
            bands = _validated_readback_bands(
                frame_ready_event,
                width=width,
                height=height,
                stride=stride,
                payload_bytes=payload_bytes,
            )
            expanded = bytearray(stride * height)
            for top, band_height, packed_top in bands:
                source_start = packed_top * stride
                source_end = source_start + band_height * stride
                destination_start = top * stride
                expanded[
                    destination_start : destination_start + band_height * stride
                ] = payload[source_start:source_end]
            payload = bytes(expanded)
            pixel_format = "bgra8888_premultiplied"

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

    def read_qimage(self, frame_ready_event: dict[str, Any]):
        """Copy a ready RGBA slot directly into one detached ``QImage``.

        Unlike ``read_frame(...).to_qimage()``, this path performs one full-frame
        copy (shared memory -> QImage) instead of staging through a Python
        ``bytes`` object. It is the G2 preview consumption path.
        """
        from PyQt6.QtGui import QImage

        self._validate_event_payload(frame_ready_event)
        self.attach()
        assert self._shared is not None

        slot_offset = _event_int(frame_ready_event, "slot_offset")
        header_bytes = _event_int(frame_ready_event, "header_bytes")
        payload_offset = _event_int(frame_ready_event, "payload_offset")
        payload_bytes = _event_int(frame_ready_event, "payload_bytes")
        slot_bytes = _event_int(frame_ready_event, "slot_bytes")
        if header_bytes < _SHARED_FRAME_HEADER.size:
            raise NativeRendererError(f"shared frame header is too small: {header_bytes}")
        if slot_offset < 0 or payload_offset < 0 or payload_bytes < 0 or slot_bytes <= 0:
            raise NativeRendererError("shared frame event contains invalid slot bounds")
        if payload_offset < slot_offset + header_bytes:
            raise NativeRendererError("shared frame payload overlaps slot header")
        shared_size = int(self._shared.size())
        if slot_offset + slot_bytes > shared_size or payload_offset + payload_bytes > shared_size:
            raise NativeRendererError("shared frame slot exceeds shared memory size")

        if not self._shared.lock():
            raise NativeRendererError(
                f"failed to lock native shared memory {self.shm_key!r}: "
                f"{self._shared.errorString()}"
            )
        try:
            pointer = self._shared.constData()
            pointer.setsize(shared_size)
            source = memoryview(pointer)
            header = _SHARED_FRAME_HEADER.unpack_from(source, slot_offset)
            (
                state,
                generation,
                _frame_index,
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
            if pixel_format not in {
                "rgba8888",
                "bgra8888_premultiplied",
                "bgra8888_premultiplied_bands",
            }:
                raise NativeRendererError(f"unsupported shared frame pixel format id: {format_id}")
            if slot_offset + header_payload_offset != payload_offset:
                raise NativeRendererError("shared frame payload offset does not match slot header")
            if header_payload_bytes != payload_bytes:
                raise NativeRendererError("shared frame payload byte count does not match slot header")
            self._validate_header_matches_event(
                frame_ready_event,
                generation=generation,
                frame_index=_frame_index,
                t_ms=t_ms,
                width=width,
                height=height,
                stride=stride,
                pixel_format=pixel_format,
            )
            banded = pixel_format == "bgra8888_premultiplied_bands"
            if width <= 0 or height <= 0 or stride < width * 4:
                raise NativeRendererError("shared frame contains invalid RGBA dimensions")
            bands = (
                _validated_readback_bands(
                    frame_ready_event,
                    width=width,
                    height=height,
                    stride=stride,
                    payload_bytes=payload_bytes,
                )
                if banded
                else [(0, height, 0)]
            )
            if not banded and payload_bytes < stride * height:
                raise NativeRendererError("shared frame contains truncated RGBA payload")

            image_format = (
                QImage.Format.Format_RGBA8888
                if pixel_format == "rgba8888"
                else QImage.Format.Format_ARGB32_Premultiplied
            )
            image = QImage(width, height, image_format)
            if image.isNull():
                raise NativeRendererError("failed to allocate QImage for shared frame")
            if banded:
                image.fill(0)
            destination_pointer = image.bits()
            destination_pointer.setsize(image.sizeInBytes())
            destination = memoryview(destination_pointer)
            destination_stride = image.bytesPerLine()
            for top, band_height, packed_top in bands:
                for row in range(band_height):
                    source_start = payload_offset + (packed_top + row) * stride
                    destination_start = (top + row) * destination_stride
                    destination[destination_start : destination_start + width * 4] = source[
                        source_start : source_start + width * 4
                    ]
            return image
        finally:
            self._shared.unlock()

    def _validate_event_payload(self, frame_ready_event: dict[str, Any]) -> None:
        if frame_ready_event.get("event") not in {
            "frame_ready",
            "probe_ready",
            "gpu_frame_ready",
        }:
            raise NativeRendererError(
                "shared frame reader expects a frame_ready, probe_ready, or gpu_frame_ready event"
            )
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


def _validated_readback_bands(
    event: dict[str, Any],
    *,
    width: int,
    height: int,
    stride: int,
    payload_bytes: int,
) -> list[tuple[int, int, int]]:
    raw_bands = event.get("bands")
    if not isinstance(raw_bands, list):
        raise NativeRendererError("banded shared frame is missing bands metadata")
    bands: list[tuple[int, int, int]] = []
    occupied_payload_end = 0
    previous_bottom = 0
    for raw in raw_bands:
        if not isinstance(raw, dict):
            raise NativeRendererError("banded shared frame contains invalid band metadata")
        try:
            top = int(raw["top"])
            band_height = int(raw["height"])
            packed_top = int(raw["packed_top"])
        except (KeyError, TypeError, ValueError) as exc:
            raise NativeRendererError(
                "banded shared frame contains invalid band coordinates"
            ) from exc
        if (
            top < 0
            or band_height <= 0
            or top + band_height > height
            or packed_top < 0
            or top < previous_bottom
        ):
            raise NativeRendererError("banded shared frame band is outside the target")
        packed_end = (packed_top + band_height) * stride
        if packed_top * stride < occupied_payload_end or packed_end > payload_bytes:
            raise NativeRendererError("banded shared frame band exceeds packed payload")
        bands.append((top, band_height, packed_top))
        previous_bottom = top + band_height
        occupied_payload_end = packed_end
    if occupied_payload_end != payload_bytes:
        raise NativeRendererError("banded shared frame payload size does not match bands")
    return bands


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
    """Resolve the sidecar executable: explicit arg > env > bundled > build tree.

    发现顺序恢复为实验期语义（2026-07-19）：路径发现本身不激活任何 native 路径，
    激活仍由 ``native_preview_enabled()`` / export 侧各自的显式开关把关，默认关闭。
    """
    candidates: list[Path] = []
    if executable_path is not None:
        candidates.append(Path(executable_path))
    env_path = os.environ.get("KROK_SUBTITLE_NATIVE_RENDERER")
    if env_path:
        candidates.append(Path(env_path))
    candidates.append(bundled_native_renderer_path(root))
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


def _sidecar_qt_bin_dir(executable_path: Path) -> Path | None:
    """Return the aqt-installed Qt bin dir the sidecar needs on PATH, if any.

    构建树里的 sidecar exe 旁没有 Qt DLL；它链接的是与 PyQt6 同版本、由
    ``run_native_renderer_smoke.ps1`` 安装到 %LOCALAPPDATA% 的 Qt。打包（frozen）
    形态下 DLL 应随包分发在 exe 旁，此时不注入，避免遮蔽打包 DLL。
    """
    if os.name != "nt":
        return None
    package_roots = [executable_path.parent]
    if getattr(sys, "frozen", False):
        package_roots.append(Path(sys.executable).resolve().parent / "_internal")
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            package_roots.append(Path(meipass))
    for root in package_roots:
        for candidate in (root, root / "PyQt6" / "Qt6" / "bin"):
            if (candidate / "Qt6Core.dll").is_file():
                return candidate
    try:
        from PyQt6.QtCore import QT_VERSION_STR
    except ImportError:
        return None
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        return None
    qt_bin = Path(local_app_data) / "krok-helper" / "qt" / QT_VERSION_STR / "msvc2022_64" / "bin"
    if (qt_bin / "Qt6Core.dll").is_file():
        return qt_bin
    return None


def _sidecar_environment(executable_path: Path) -> dict[str, str] | None:
    """Build the child environment for local and PyInstaller Qt layouts."""
    if os.name != "nt":
        return None
    env = dict(os.environ)
    changed = False
    qt_bin = _sidecar_qt_bin_dir(executable_path)
    if qt_bin is not None:
        env["PATH"] = str(qt_bin) + os.pathsep + env.get("PATH", "")
        changed = True
        plugin_candidates = [
            qt_bin.parent / "plugins",
            qt_bin / "plugins",
        ]
        for plugin_root in plugin_candidates:
            if (plugin_root / "platforms" / "qwindows.dll").is_file():
                env["QT_PLUGIN_PATH"] = str(plugin_root)
                changed = True
                break
    return env if changed else None


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

    @property
    def process_id(self) -> int | None:
        """Return the live sidecar PID for diagnostics and stability probes."""
        return self._process.pid if self.is_running and self._process is not None else None

    def start(self) -> dict[str, Any]:
        if self.is_running:
            return {"ok": True, "event": "already_running"}
        env = _sidecar_environment(self.executable_path)
        self._process = subprocess.Popen(
            [str(self.executable_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=1,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            **_sidecar_subprocess_kwargs(),
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
                    self._read_until_event("shutdown")
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
        dpr: float = 1.0,
        extra_tracks: list[TimingTrack] | None = None,
    ) -> dict[str, Any]:
        ir = build_render_ir(
            track,
            style,
            width=width,
            height=height,
            fps=fps,
            dpr=dpr,
            extra_tracks=extra_tracks,
        )
        self._send({"cmd": "configure", "ir": ir})
        return self._expect_ok(self._read_until_event("configured"))

    def backend_info(self, *, force_warp: bool = False) -> dict[str, Any]:
        """Return Direct2D/D3D11 adapter capabilities without touching product UI."""
        self._send({"cmd": "backend_info", "force_warp": bool(force_warp)})
        return self._expect_ok(self._read_until_event("backend_info"))

    def configure_gpu(
        self,
        track: TimingTrack,
        style: Style,
        *,
        width: int,
        height: int,
        fps: int,
        dpr: float = 1.0,
        force_warp: bool = False,
        extra_tracks: list[TimingTrack] | None = None,
        prewarm_t_ms: int = 0,
        worker_count: int = 1,
    ) -> dict[str, Any]:
        """Configure the G1 DirectWrite scene without enabling the product path."""
        self.configure(
            track,
            style,
            width=width,
            height=height,
            fps=fps,
            dpr=dpr,
            extra_tracks=extra_tracks,
        )
        self._send(
            {
                "cmd": "gpu_configure",
                "force_warp": bool(force_warp),
                "prewarm_t_ms": max(int(prewarm_t_ms), 0),
                "worker_count": max(1, min(int(worker_count), 8)),
            }
        )
        return self._expect_ok(self._read_until_event("gpu_configured"))

    def begin_render_gpu_frame(
        self,
        t_ms: int,
        *,
        force_warp: bool = False,
        generation: int = 0,
        frame_index: int = 0,
        shm_key: str | None = None,
        include_checksum: bool = True,
        readback_bands: bool = False,
        slot_count: int = 1,
        request_serial: int | None = None,
    ) -> None:
        """Send a gpu_render_frame request without waiting for its response.

        With ``slot_count`` > 1 the sidecar writes ``frame_index % slot_count``
        into a multi-slot ring, so the caller may keep consuming the previous
        frame's slot while this one renders (G7 export pipelining). Collect the
        response with :meth:`finish_render_gpu_frame`.
        """
        payload: dict[str, Any] = {
            "cmd": "gpu_render_frame",
            "t_ms": int(t_ms),
            "force_warp": bool(force_warp),
            "generation": int(generation),
            "frame_index": int(frame_index),
            "include_checksum": bool(include_checksum),
            "readback_bands": bool(readback_bands),
            "slot_count": max(1, int(slot_count)),
        }
        if request_serial is not None:
            payload["request_serial"] = int(request_serial)
        if shm_key:
            payload["shm_key"] = str(shm_key)
        self._send(payload)

    def finish_render_gpu_frame(self) -> dict[str, Any]:
        """Collect the response for a pending begin_render_gpu_frame call."""
        while True:
            response = self._read_response()
            if response.get("event") in {
                "gpu_frame_ready",
                "gpu_frame_dropped",
                "gpu_queue_full",
            }:
                return self._expect_ok(response)
            self._event_backlog.append(response)

    def render_gpu_frame(
        self,
        t_ms: int,
        *,
        force_warp: bool = False,
        generation: int = 0,
        frame_index: int = 0,
        shm_key: str | None = None,
        include_checksum: bool = True,
        readback_bands: bool = False,
        slot_count: int = 1,
        request_serial: int | None = None,
    ) -> dict[str, Any]:
        """Render one configured G1 frame into a shared-memory RGBA slot."""
        self.begin_render_gpu_frame(
            t_ms,
            force_warp=force_warp,
            generation=generation,
            frame_index=frame_index,
            shm_key=shm_key,
            include_checksum=include_checksum,
            readback_bands=readback_bands,
            slot_count=slot_count,
            request_serial=request_serial,
        )
        return self.finish_render_gpu_frame()

    def present_gpu_frame(
        self,
        t_ms: int,
        *,
        parent_hwnd: int,
        x: int,
        y: int,
        width: int,
        height: int,
        force_warp: bool = False,
        generation: int = 0,
        frame_index: int = 0,
    ) -> dict[str, Any]:
        """Present one GPU frame in a DirectComposition child HWND without readback."""
        self._send(
            {
                "cmd": "gpu_present_frame",
                "t_ms": int(t_ms),
                "force_warp": bool(force_warp),
                "generation": int(generation),
                "frame_index": int(frame_index),
                "parent_hwnd": str(int(parent_hwnd)),
                "x": int(x),
                "y": int(y),
                "width": int(width),
                "height": int(height),
            }
        )
        return self._expect_ok(self._read_response())

    def close_gpu_preview(self, *, force_warp: bool = False) -> dict[str, Any]:
        """Destroy the sidecar-owned native preview child window."""
        self._send({"cmd": "gpu_preview_close", "force_warp": bool(force_warp)})
        return self._expect_ok(self._read_response())

    def gpu_diagnostics(self, *, force_warp: bool = False) -> dict[str, Any]:
        """Read cache and DXGI memory counters without entering the frame hot path."""
        self._send({"cmd": "gpu_diagnostics", "force_warp": bool(force_warp)})
        return self._expect_ok(self._read_until_event("gpu_diagnostics"))

    def render_probe(
        self,
        *,
        width: int = 256,
        height: int = 144,
        force_warp: bool = False,
        draw_glyph: bool = True,
        rgba: tuple[int, int, int, int] = (51, 102, 204, 128),
        generation: int = 0,
        frame_index: int = 0,
        shm_key: str | None = None,
    ) -> dict[str, Any]:
        """Render the G0 transparent Direct2D probe and expose its shared-memory slot."""
        red, green, blue, alpha = (max(0, min(int(value), 255)) for value in rgba)
        payload: dict[str, Any] = {
            "cmd": "render_probe",
            "width": int(width),
            "height": int(height),
            "force_warp": bool(force_warp),
            "draw_glyph": bool(draw_glyph),
            "red": red,
            "green": green,
            "blue": blue,
            "alpha": alpha,
            "generation": int(generation),
            "frame_index": int(frame_index),
        }
        if shm_key:
            payload["shm_key"] = str(shm_key)
        self._send(payload)
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
            if payload.get("event") == event or not payload.get("ok", False):
                self._event_backlog.extendleft(reversed(kept))
                return payload
            kept.append(payload)
        self._event_backlog = kept
        while True:
            payload = self._read_response()
            if payload.get("event") == event or not payload.get("ok", False):
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
