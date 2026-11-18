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
from .skia_text import (CELL_GLYPH_ERROR, EMPTY_CELL_ERROR, VERTICAL_METRICS_ERROR, has_cell_glyph_schema,
                        has_empty_cell_schema, has_kana_mark_schema, has_vertical_metrics_schema)


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
            if 'p05_page_collisions' not in ready.get('profiles', []):
                raise NativeRendererError('Skia sidecar 未声明跨页避让能力，请重新构建')
            if 'p05_smart_horizontal' not in ready.get('profiles', []):
                raise NativeRendererError('Skia sidecar 未声明智能水平布局能力，请重新构建')
            if ('p05_segmented_wipe' not in ready.get('profiles', [])
                    or ready.get('resolved_wipe_schema') != 1):
                raise NativeRendererError('Skia sidecar 不支持版本化分段走字，请重新构建')
            if ('p05_resolved_ruby' not in ready.get('profiles', []) or ready.get('resolved_ruby_schema') != 1):
                raise NativeRendererError('Skia sidecar 不支持最终注音组，请重新构建')
            if ('p05_upper_ruby' not in ready.get('profiles', []) or ready.get('upper_ruby_schema') != 1):
                raise NativeRendererError('Skia sidecar 不支持产品上注音入口，请重新构建')
            if ('p05_lower_ruby' not in ready.get('profiles', []) or ready.get('lower_ruby_schema') != 1
                    or 'p05_overlap_wipe' not in ready.get('profiles', []) or ready.get('overlap_schema') != 1):
                raise NativeRendererError('Skia sidecar 不支持产品下方注音与重叠走字，请重新构建')
            if 'p05_ruby_markers' not in ready.get('profiles', []) or ready.get('ruby_marker_schema') != 1:
                raise NativeRendererError('Skia sidecar 不支持空读音与短语标记，请重新构建')
            if 'p05_ruby_units' not in ready.get('profiles', []) or ready.get('ruby_unit_schema') != 1:
                raise NativeRendererError('Skia sidecar 不支持分段注音字形单元，请重新构建')
            if ready.get('shared_memory_lifecycle_schema') != 1:
                raise NativeRendererError('Skia sidecar 不支持共享帧所有权协议，请重新构建')
            if ready.get('font_chain_schema') != 1 or 'p05_font_fallback' not in ready.get('profiles', []):
                raise NativeRendererError('Skia sidecar 不支持独立字体链实例，请重新构建')
            if ready.get('mixed_font_schema') != 1 or 'p05_mixed_fonts' not in ready.get('profiles', []):
                raise NativeRendererError('Skia sidecar 不支持单元内混合字体，请重新构建')
            if ready.get('standalone_mark_schema') != 1 or 'p05_standalone_marks' not in ready.get('profiles', []):
                raise NativeRendererError('Skia sidecar 不支持独立组合标记，请重新构建')
            if ready.get("no_wipe_schema") != 1 or "p07_no_wipe" not in ready.get("profiles", []):
                raise NativeRendererError("Skia sidecar 不支持整字变色，请重新构建")
            if ready.get("reverse_wipe_schema") != 1 or "p05_reverse_wipe" not in ready.get("profiles", []):
                raise NativeRendererError("Skia sidecar 不支持正文反向走字，请重新构建")
            if ready.get("reverse_overlap_schema") != 1 or "p05_reverse_overlap" not in ready.get("profiles", []):
                raise NativeRendererError("Skia sidecar 不支持反向走字重叠终点与注音轨道，请重新构建")
            if (type(ready.get('zero_ruby_no_wipe_schema')) is not int
                    or ready['zero_ruby_no_wipe_schema'] != 1
                    or 'p07_zero_ruby_no_wipe' not in ready.get('profiles', [])):
                raise NativeRendererError('Skia sidecar 不支持零时长注音整字变色，请重新构建')
            if (type(ready.get('horizontal_rtl_schema')) is not int
                    or ready['horizontal_rtl_schema'] != 1
                    or 'p05_horizontal_rtl' not in ready.get('profiles', [])):
                raise NativeRendererError('Skia sidecar 不支持右至左单元排列，请重新构建')
            if (type(ready.get('horizontal_rtl_cell_schema')) is not int
                    or ready['horizontal_rtl_cell_schema'] != 1
                    or 'p05_horizontal_rtl_cells' not in ready.get('profiles', [])):
                raise NativeRendererError('Skia sidecar 不支持右至左多码点单元，请重新构建')
            if (type(ready.get('horizontal_rtl_kana_schema')) is not int
                    or ready['horizontal_rtl_kana_schema'] != 1
                    or 'p05_horizontal_rtl_kana_cells' not in ready.get('profiles', [])):
                raise NativeRendererError('Skia sidecar 不支持右至左假名单元，请重新构建')
            # Overlapping cells are stacked in the existing GPU's N3 phase order.
            if (type(ready.get('body_phase_order_schema')) is not int
                    or ready['body_phase_order_schema'] != 1):
                raise NativeRendererError('Skia sidecar 不支持正文重叠单元的唱字层序，请重新构建')
            if not has_cell_glyph_schema(ready):
                raise NativeRendererError(CELL_GLYPH_ERROR)
            if (not has_kana_mark_schema(ready)
                    or 'p05_horizontal_rtl_kana_marks' not in ready.get('profiles', [])):
                raise NativeRendererError('Skia sidecar 不支持假名浊音字形策略，请重新构建')
            if not has_vertical_metrics_schema(ready):
                raise NativeRendererError(VERTICAL_METRICS_ERROR)
            if not has_empty_cell_schema(ready):
                raise NativeRendererError(EMPTY_CELL_ERROR)
            if (type(ready.get('vertical_solid_schema')) is not int or ready['vertical_solid_schema'] != 2
                    or 'p05_vertical_solid' not in ready.get('profiles', [])):
                raise NativeRendererError('Skia sidecar 不支持竖排实色正文，请重新构建')
            if (type(ready.get('vertical_japanese_glyph_schema')) is not int or ready['vertical_japanese_glyph_schema'] != 1
                    or 'p05_vertical_japanese_glyphs' not in ready.get('profiles', [])):
                raise NativeRendererError('Skia sidecar 不支持竖排日文标点和小假名，请重新构建')
            if (type(ready.get('primary_solid_outline_schema')) is not int or ready['primary_solid_outline_schema'] != 1
                    or 'p06_primary_solid_outline' not in ready.get('profiles', [])):
                raise NativeRendererError('Skia sidecar 不支持实色描边，请重新构建')
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
        from .skia_fonts import SkiaFontResolver
        from .protocol import apply_resolved_font_faces
        try:
            with SkiaTextMetricsProcess(self.executable_path) as metrics:
                ir = build_skia_render_ir(track, style, measure=metrics.measure,
                                          resolve_font=SkiaFontResolver(), **kwargs)
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
