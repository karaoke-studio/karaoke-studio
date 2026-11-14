"""Graphics API contract for the Skia Qt Quick subtitle preview.

The shared Skia core drives Vulkan on Windows/Linux and Metal on macOS. Qt Quick picks its
scene graph API once, before the first QQuickWindow exists, and on Windows it would default to
Direct3D 11. ``select_quick_graphics_api`` must therefore run before any Quick window or widget
is created, and ``verify_quick_graphics_api`` rejects a window that ended up elsewhere instead
of letting the preview run silently on an unsupported API.
"""
from __future__ import annotations

import sys

from PyQt6.QtQuick import QQuickWindow, QSGRendererInterface

GraphicsApi = QSGRendererInterface.GraphicsApi


def required_quick_graphics_api(platform: str = sys.platform) -> GraphicsApi:
    return GraphicsApi.Metal if platform == "darwin" else GraphicsApi.Vulkan


def select_quick_graphics_api(platform: str = sys.platform) -> GraphicsApi:
    """Request the platform's Skia GPU API for every Qt Quick window created afterwards."""
    api = required_quick_graphics_api(platform)
    QQuickWindow.setGraphicsApi(api)
    return api


def verify_quick_graphics_api(window: QQuickWindow, platform: str = sys.platform) -> GraphicsApi:
    """Return the window's actual API, raising if it is not the required one.

    Valid only after the window's scene graph is initialized (e.g. from
    ``sceneGraphInitialized`` or after the first frame).
    """
    interface = window.rendererInterface()
    if interface is None:
        raise RuntimeError("Qt Quick 场景图尚未初始化，无法确认字幕预览使用的图形接口")
    actual = interface.graphicsApi()
    required = required_quick_graphics_api(platform)
    if actual != required:
        raise RuntimeError(
            f"字幕预览需要 {required.name} 图形接口，但 Qt Quick 实际使用 {actual.name}；"
            "请在创建任何 Qt Quick 窗口之前调用 select_quick_graphics_api()"
        )
    return actual


__all__ = [
    "required_quick_graphics_api",
    "select_quick_graphics_api",
    "verify_quick_graphics_api",
]
