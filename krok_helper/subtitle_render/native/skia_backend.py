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
from .backend import (
    NativeRendererError,
    NativeRendererExitError,
    NativeRendererLaunchError,
    NativeRendererStallError,
    NativeRendererTimeoutError,
    heartbeat_lease_s,
    skia_requested,
)
from .skia_process import SkiaSidecarProcess, resolve_skia_renderer_path
from .skia_ipc import OwnedPosixRings

_log = logging.getLogger(__name__)


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


class SkiaSceneUnsupportedError(NativeRendererError):
    """Skia could not configure this project; callers fall back to the old renderer."""


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
        started_at = time.monotonic()
        ready = super().start()
        ready_ms = (time.monotonic() - started_at) * 1000.0
        select_started = time.monotonic()
        try:
            if "raster" not in ready.get("execution_targets", []):
                raise NativeRendererError("Skia sidecar 未声明编译执行目标")
            self._send({"cmd": "select_backend", "selection_version": 1, "backend": "skia",
                        "target": self.selection.target, "fallback": self.selection.fallback,
                        "require_hardware": self.selection.require_hardware})
            selected = self._expect_ok(self._read_until_event("backend_selected", timeout_s=self.gpu_configure_timeout_s))
            self.skia_info = selected.get("backend_info", {})
        except BaseException as exc:
            _log.error(
                "Skia select_backend 握手失败: pid=%s ready=%.0fms select_elapsed=%.0fms "
                "timeout=%.1fs selection=%s targets=%s executable=%s err_type=%s err=%s stderr_tail=%r",
                self.process_id,
                ready_ms,
                (time.monotonic() - select_started) * 1000.0,
                self.gpu_configure_timeout_s,
                self.selection,
                ready.get("execution_targets"),
                self.executable_path,
                type(exc).__name__,
                exc,
                self._stderr_excerpt(),
            )
            self.abort(reason="Skia handshake failed")
            raise
        info = self.skia_info
        _log.info(
            "Skia sidecar 启动完成: pid=%s ready=%.0fms select=%.0fms requested=%s/%s(hw=%s) "
            "actual=%s api=%s device=%s hardware=%s fallback_reason=%s executable=%s",
            self.process_id,
            ready_ms,
            (time.monotonic() - select_started) * 1000.0,
            self.selection.target,
            self.selection.fallback,
            self.selection.require_hardware,
            info.get("execution_target"),
            info.get("api"),
            info.get("adapter_name"),
            info.get("hardware"),
            info.get("fallback_reason") or "无",
            self.executable_path.name,
        )
        return ready

    def configure(self, track, style, *, progress=None, **kwargs):
        from .skia_scene import prepare_skia_scene_ir

        started_at = time.monotonic()
        missing_fallbacks: dict[str, str] = {}
        try:
            ir = prepare_skia_scene_ir(
                track,
                style,
                executable=self.executable_path,
                missing_fallbacks=missing_fallbacks,
                **kwargs,
            )
        except (ValueError, NotImplementedError) as exc:
            _log.warning(
                "Skia 场景构建判定不支持: pid=%s elapsed=%.0fms err=%s",
                self.process_id,
                (time.monotonic() - started_at) * 1000.0,
                exc,
            )
            raise SkiaSceneUnsupportedError(f"Skia 暂不支持当前工程：{exc}") from exc
        except NativeRendererError as exc:
            _log.error(
                "Skia 场景准备（含字体度量）失败: pid=%s elapsed=%.0fms err_type=%s err=%s",
                self.process_id,
                (time.monotonic() - started_at) * 1000.0,
                type(exc).__name__,
                exc,
            )
            raise
        ir_ms = (time.monotonic() - started_at) * 1000.0
        if progress is not None:
            progress()
        cfg_started = time.monotonic()
        self._send({"cmd": "configure", "ir": ir})
        try:
            out = self._expect_ok(self._read_until_event("configured", timeout_s=self.configure_timeout_s,
                                                        heartbeat_lease_s=heartbeat_lease_s()))
        except (
            SkiaGpuDeviceError,
            NativeRendererLaunchError,
            NativeRendererTimeoutError,
            NativeRendererExitError,
            NativeRendererStallError,
        ) as exc:
            _log.error(
                "Skia configure RPC 运行失败: pid=%s prepare_ir=%.0fms configure_elapsed=%.0fms "
                "timeout=%.1fs err_type=%s err=%s stderr_tail=%r",
                self.process_id,
                ir_ms,
                (time.monotonic() - cfg_started) * 1000.0,
                self.configure_timeout_s,
                type(exc).__name__,
                exc,
                self._stderr_excerpt(),
            )
            raise
        except NativeRendererError as exc:
            _log.warning(
                "Skia configure 拒绝当前工程: pid=%s prepare_ir=%.0fms configure_elapsed=%.0fms err=%s",
                self.process_id,
                ir_ms,
                (time.monotonic() - cfg_started) * 1000.0,
                exc,
            )
            raise SkiaSceneUnsupportedError(f"Skia 无法配置当前工程：{exc}") from exc
        if out.get('metrics_identity') != ir['skia_layout']['metrics_identity']:
            raise NativeRendererError('Skia 场景未确认规范度量快照')
        self.skia_info = out.get("backend_info", {})
        info = self.skia_info
        target = "GPU" if info.get("execution_target") == "gpu" else "Raster CPU"
        cfg_ms = (time.monotonic() - cfg_started) * 1000.0
        _log.info(
            "Skia 场景配置完成: pid=%s size=%sx%s dpr=%s prepare_ir=%.0fms configure=%.0fms total=%.0fms",
            self.process_id,
            kwargs.get("width"),
            kwargs.get("height"),
            kwargs.get("dpr", 1.0),
            ir_ms,
            cfg_ms,
            (time.monotonic() - started_at) * 1000.0,
        )
        self._on_backend_info(f"Skia 字幕后端：{target}，API={info.get('api')}，设备={info.get('adapter_name')}，"
                              f"硬件={'是' if info.get('hardware') else '否'}，"
                              f"回退原因={info.get('fallback_reason') or '无'}")
        if missing_fallbacks:
            pairs = "、".join(f"「{src}」→「{dst}」" for src, dst in sorted(missing_fallbacks.items()))
            self._on_backend_info(
                f"Skia 字体回退：未找到工程字体，已自动回退使用系统字体（{pairs}）"
            )
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
