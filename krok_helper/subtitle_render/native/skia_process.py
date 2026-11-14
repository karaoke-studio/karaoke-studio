"""Shared Skia executable/runtime launch boundary, without a rendering policy."""
from __future__ import annotations
import os
from pathlib import Path
import platform
import sys
from PyQt6.QtCore import QLibraryInfo
from .backend import NativeRendererError, NativeRendererProcess, repository_root


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
    raise NativeRendererError("未找到 Skia 字幕 sidecar，请先用 --qt-root 构建或设置 KROK_SUBTITLE_SKIA_RENDERER")


class SkiaSidecarProcess(NativeRendererProcess):
    def __init__(self, executable_path=None, **kwargs):
        super().__init__(executable_path or resolve_skia_renderer_path(), **kwargs)

    def _launch_environment(self):
        env = super()._launch_environment() or os.environ.copy()
        qt_lib = QLibraryInfo.path(QLibraryInfo.LibraryPath.LibrariesPath)
        key = "DYLD_FRAMEWORK_PATH" if sys.platform == "darwin" else "LD_LIBRARY_PATH"
        if sys.platform != "win32":
            env[key] = qt_lib + (os.pathsep + env[key] if env.get(key) else "")
        return env
