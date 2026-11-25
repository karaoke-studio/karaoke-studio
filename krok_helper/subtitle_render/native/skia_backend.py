"""Explicit experimental selection and the Skia sidecar handshake.

The production Direct2D resolver/preferences remain independent of this module.
"""
from __future__ import annotations

from dataclasses import dataclass
import logging
import os
import time
import subprocess
from typing import Any
from .backend import NativeRendererError, heartbeat_lease_s, skia_requested
from .skia_process import SkiaSidecarProcess, resolve_skia_renderer_path
from .skia_ipc import OwnedPosixRings


@dataclass(frozen=True)
class SkiaSelection:
    target: str = "gpu"
    fallback: str = "raster"
    require_hardware: bool = True

    def __post_init__(self) -> None:
        if self.target not in {"gpu", "raster"} or self.fallback not in {"none", "raster"}:
            raise ValueError("无效的 Skia 执行目标或回退策略")

    @classmethod
    def from_environment(cls, *, gpu_enabled: bool = True) -> SkiaSelection:
        return cls(
            os.environ.get("KROK_SUBTITLE_SKIA_TARGET", "gpu") if gpu_enabled else "raster",
            os.environ.get("KROK_SUBTITLE_SKIA_FALLBACK", "raster"),
            os.environ.get("KROK_SUBTITLE_SKIA_REQUIRE_HARDWARE", "1") != "0",
        )


class SkiaGpuDeviceError(NativeRendererError):
    def __init__(self, message: str, *, raster_allowed: bool):
        super().__init__(message)
        self.raster_allowed = raster_allowed


class SkiaRendererProcess(SkiaSidecarProcess):
    def __init__(self, executable_path=None, *, selection: SkiaSelection | None = None, on_backend_info=None, **kwargs) -> None:
        self._posix_rings = OwnedPosixRings()
        super().__init__(executable_path, **kwargs)
        self.selection = selection or SkiaSelection.from_environment()
        self.skia_info: dict[str, Any] = {}
        self._on_backend_info = on_backend_info or logging.getLogger(__name__).info

    def start(self) -> dict[str, Any]:
        if self.is_running:
            return {"ok": True, "event": "already_running"}
        ready = super().start()
        try:
            if "raster" not in ready.get("execution_targets", []):
                raise NativeRendererError("Skia sidecar 未声明编译执行目标")
            self._send({"cmd": "select_backend", "selection_version": 1, "backend": "skia",
                        "target": self.selection.target, "fallback": self.selection.fallback,
                        "require_hardware": self.selection.require_hardware})
            selected = self._expect_ok(self._read_until_event("backend_selected", timeout_s=self.gpu_configure_timeout_s))
            self.skia_info = selected.get("backend_info", {})
        except BaseException:
            self.abort(reason="Skia handshake failed")
            raise
        return ready

    def configure(self, track, style, *, progress=None, **kwargs):
        from .skia_scene import prepare_skia_scene_ir

        ir = prepare_skia_scene_ir(track, style, executable=self.executable_path, **kwargs)
        if progress is not None:
            progress()
        self._send({"cmd": "configure", "ir": ir})
        out = self._expect_ok(self._read_until_event("configured", timeout_s=self.configure_timeout_s,
                                                    heartbeat_lease_s=heartbeat_lease_s()))
        if out.get('metrics_identity') != ir['skia_layout']['metrics_identity']:
            raise NativeRendererError('Skia 场景未确认规范度量快照')
        self.skia_info = out.get("backend_info", {})
        info = self.skia_info
        target = "GPU" if info.get("execution_target") == "gpu" else "Raster CPU"
        self._on_backend_info(f"Skia 字幕后端：{target}，API={info.get('api')}，设备={info.get('adapter_name')}，"
                              f"硬件={'是' if info.get('hardware') else '否'}，"
                              f"回退原因={info.get('fallback_reason') or '无'}")
        return out

    def _expect_ok(self, response):
        if (not response.get('ok') and response.get('error_code') == 'skia_gpu_device'
                and self.skia_info.get('execution_target') == 'gpu'):
            raise SkiaGpuDeviceError(str(response.get('error') or 'Skia GPU 设备故障'),
                                     raster_allowed=self.selection.fallback == 'raster')
        return super()._expect_ok(response)

    def read_event(self):
        event = super().read_event()
        if event.get('error_code') == 'skia_gpu_device':
            self._expect_ok(event)
        if event.get("event") == "frame_ready" and event.get("payload") == "shared_memory":
            alpha = {"rgba8888": "straight", "bgra8888_premultiplied": "premultiplied"}.get(event.get("pixel_format"))
            if alpha is None or event.get("alpha_type") != alpha or event.get("origin") != "top_left" or event.get("color_space") != "srgb":
                raise NativeRendererError("Skia 帧格式、alpha、方向或色彩空间与声明不符")
        return event

    def _read_response_or_none(self, *, timeout_s=None, waiting_for='protocol response'):
        deadline = time.monotonic()+self._resolved_timeout(timeout_s)
        while True:
            event = super()._read_response_or_none(timeout_s=max(deadline-time.monotonic(), 0), waiting_for=waiting_for)
            if event is None or not self._posix_rings.observe(event, send=self._send, is_running=lambda: self.is_running):
                return event

    def close(self):
        try: super().close()
        finally: self._posix_rings.cleanup()

    def abort(self, **kwargs):
        try:
            super().abort(**kwargs)
            if self._process is not None:
                try: self._process.wait(timeout=1)
                except (OSError, subprocess.TimeoutExpired): pass
        finally:
            if not self.is_running: self._posix_rings.cleanup()
