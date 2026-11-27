"""Shared Skia executable/runtime launch boundary, without a rendering policy."""
from __future__ import annotations
import logging
import os
from pathlib import Path
import platform
import sys
from PyQt6.QtCore import QLibraryInfo
from .backend import NativeRendererError, NativeRendererProcess, repository_root

_log = logging.getLogger(__name__)

# Windows 杀软扫描、冷启动加载 Qt/Vulkan DLL 与显卡初始化可能超过 5s；
# 放宽启动/握手/配置超时（向旧 D2D 预览 10s/30s 对齐），单帧读响应仍由
# response_timeout_s（默认 5s）把关以保持卡死检测灵敏度。
SKIA_STARTUP_TIMEOUT_S = 15.0
SKIA_GPU_SELECT_TIMEOUT_S = 30.0
SKIA_CONFIGURE_TIMEOUT_S = 30.0
SKIA_METRICS_TIMEOUT_S = 15.0


def resolve_skia_renderer_path() -> Path:
    override = os.environ.get("KROK_SUBTITLE_SKIA_RENDERER")
    suffix = ".exe" if sys.platform == "win32" else ""
    name = "krok_skia_subtitle_renderer" + suffix
    system = "windows" if sys.platform == "win32" else "macos" if sys.platform == "darwin" else "linux"
    arch = "arm64" if platform.machine().lower() in {"arm64", "aarch64"} else "x64"
    api = "metal" if sys.platform == "darwin" else "vulkan"
    root = Path.home() / ".cache/krok-helper/skia-deps/native"
    candidates = [Path(override)] if override else [
        Path(sys.executable).parent / name,
        repository_root() / "build/skia-deps/native" / f"{system}-{arch}-{api}" / name,
        root / f"{system}-{arch}-{api}" / name,
        root / f"{system}-{arch}-raster" / name,
    ]
    for candidate in candidates:
        if candidate.is_file(): return candidate.resolve()
    _log.warning(
        "未找到 Skia sidecar 可执行文件: frozen=%s override=%r candidates=%s",
        getattr(sys, "frozen", False),
        override,
        [str(c) for c in candidates],
    )
    raise NativeRendererError("未找到 Skia 字幕 sidecar，请先用 --qt-root 构建或设置 KROK_SUBTITLE_SKIA_RENDERER")


# The sidecar and this client ship from one source tree. Any change to the IR,
# measurement or frame protocol bumps this single version on both sides.
SKIA_PROTOCOL = 4


class SkiaSidecarProcess(NativeRendererProcess):
    def __init__(self, executable_path=None, **kwargs):
        kwargs.setdefault("startup_timeout_s", SKIA_STARTUP_TIMEOUT_S)
        kwargs.setdefault("configure_timeout_s", SKIA_CONFIGURE_TIMEOUT_S)
        kwargs.setdefault("gpu_configure_timeout_s", SKIA_GPU_SELECT_TIMEOUT_S)
        super().__init__(executable_path or resolve_skia_renderer_path(), **kwargs)

    def start(self):
        if self.is_running:
            return {"ok": True, "event": "already_running"}
        ready = super().start()
        if ready.get("backend") != "skia" or ready.get("skia_protocol") != SKIA_PROTOCOL:
            _log.error(
                "Skia sidecar 协议握手不匹配: executable=%s pid=%s expected_protocol=%s ready=%r",
                self.executable_path,
                self.process_id,
                SKIA_PROTOCOL,
                ready,
            )
            self.abort(reason="Skia protocol handshake failed")
            raise NativeRendererError("Skia sidecar 的协议版本与当前程序不一致，请重新构建")
        return ready

    def _launch_environment(self):
        env = super()._launch_environment() or os.environ.copy()
        qt_lib = QLibraryInfo.path(QLibraryInfo.LibraryPath.LibrariesPath)
        key = "DYLD_FRAMEWORK_PATH" if sys.platform == "darwin" else "LD_LIBRARY_PATH"
        if sys.platform != "win32":
            env[key] = qt_lib + (os.pathsep + env[key] if env.get(key) else "")
        return env
