"""Explicit experimental selection and the Skia sidecar handshake.

The production Direct2D resolver/preferences remain independent of this module.
"""
from __future__ import annotations

from dataclasses import dataclass
import logging
import os
from typing import Any
from .backend import NativeRendererError, heartbeat_lease_s
from .skia_process import SkiaSidecarProcess, resolve_skia_renderer_path


def skia_requested() -> bool:
    return os.environ.get("KROK_SUBTITLE_RENDER_BACKEND", "").strip().lower() == "skia"


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


class SkiaRendererProcess(SkiaSidecarProcess):
    def __init__(self, executable_path=None, *, selection: SkiaSelection | None = None, on_backend_info=None, **kwargs) -> None:
        super().__init__(executable_path, **kwargs)
        self.selection = selection or SkiaSelection.from_environment()
        self.skia_info: dict[str, Any] = {}
        self._on_backend_info = on_backend_info or logging.getLogger(__name__).info

    def start(self) -> dict[str, Any]:
        if self.is_running:
            return {"ok": True, "event": "already_running"}
        ready = super().start()
        try:
            if (ready.get("backend"), ready.get("schema"), ready.get("selection_version")) != ("skia", 3, 1):
                raise NativeRendererError("sidecar 不支持 Skia 版本化选择握手，拒绝发送字幕配置")
            if "p05_resolved_horizontal" not in ready.get("profiles", []):
                raise NativeRendererError("Skia sidecar 未声明 P05 规范布局能力")
            if ready.get('resolved_layout_schema') != 2:
                raise NativeRendererError('Skia sidecar 不支持字体实例表布局协议，请重新构建')
            if 'p05_single_line_placement' not in ready.get('profiles', []):
                raise NativeRendererError('Skia sidecar 未声明单行定位能力，请重新构建')
            if 'p05_inline_roles' not in ready.get('profiles', []):
                raise NativeRendererError('Skia sidecar 未声明行内角色能力，请重新构建')
            if 'p05_fixed_row_grid' not in ready.get('profiles', []):
                raise NativeRendererError('Skia sidecar 未声明多行网格能力，请重新构建')
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
        from krok_helper.subtitle_render.engine.render.skia_ir import build_skia_render_ir
        from .skia_text import SkiaTextMetricsProcess
        from .skia_fonts import resolve_skia_font
        from .protocol import apply_resolved_font_faces
        try:
            with SkiaTextMetricsProcess(self.executable_path) as metrics:
                ir = build_skia_render_ir(track, style, measure=metrics.measure,
                                          resolve_font=resolve_skia_font, **kwargs)
            apply_resolved_font_faces(ir)
        except (ValueError, OSError) as exc:
            raise NativeRendererError(str(exc)) from exc
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

    def read_event(self):
        event = super().read_event()
        if event.get("event") == "frame_ready" and event.get("payload") == "shared_memory":
            alpha = {"rgba8888": "straight", "bgra8888_premultiplied": "premultiplied"}.get(event.get("pixel_format"))
            if alpha is None or event.get("alpha_type") != alpha or event.get("origin") != "top_left" or event.get("color_space") != "srgb":
                raise NativeRendererError("Skia 帧格式、alpha、方向或色彩空间与声明不符")
        return event
