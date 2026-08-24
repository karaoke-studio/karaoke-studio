"""Shared filesystem identity and diagnostics for subtitle image resources."""

from __future__ import annotations

import logging
import os
from threading import Lock


_IMAGE_RESOURCE_LOCK = Lock()
_WARNED_PATHS: set[str] = set()
_RESOURCE_LOG = logging.getLogger("krok_helper.subtitle_render.painter")


def warn_image_resource_skipped(path: str, reason: str) -> None:
    """Emit the established once-per-path image resource warning."""
    with _IMAGE_RESOURCE_LOCK:
        if path in _WARNED_PATHS:
            return
        _WARNED_PATHS.add(path)
    _RESOURCE_LOG.warning("字幕图片填充被跳过：%s（%s）", path, reason)


def image_file_signature(path: str) -> tuple[str, int, int] | None:
    """Return a normalized identity that invalidates when a file changes."""
    try:
        normalized = os.path.abspath(os.path.normpath(path))
        stat = os.stat(normalized)
    except OSError as exc:
        warn_image_resource_skipped(path, f"无法读取图片文件：{exc}")
        return None
    return normalized, int(stat.st_mtime_ns), int(stat.st_size)


__all__ = ["image_file_signature", "warn_image_resource_skipped"]
