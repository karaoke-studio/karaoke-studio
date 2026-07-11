"""Qt Multimedia playback helpers for subtitle preview.

Qt's FFmpeg backend is stricter than ffmpeg itself about packet timestamps.
Some downloaded videos contain packets with ``AV_NOPTS_VALUE`` and trigger
``Demuxing failed -22`` during preview playback.  For preview only, remux such
containers through ffmpeg with generated timestamps and keep the project/export
source path unchanged.
"""

from __future__ import annotations

import hashlib
import subprocess
import tempfile
from pathlib import Path

from krok_helper.ffmpeg import _build_subprocess_kwargs, find_tool
from krok_helper.settings import load_app_settings


_VIDEO_CONTAINER_SUFFIXES = {".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi", ".flv"}
_PREVIEW_CACHE_DIR = "KaraokeStudioPreviewCache"


def qt_playback_source(path: Path) -> Path:
    """Return a Qt-friendly preview source for ``path`` when possible."""
    path = Path(path)
    if not _should_prepare_proxy(path):
        return path
    ffmpeg_path = _resolve_ffmpeg_path()
    if ffmpeg_path is None:
        return path
    proxy = _proxy_path_for(path)
    if proxy.is_file() and proxy.stat().st_size > 0:
        return proxy
    tmp = proxy.with_suffix(".tmp.mp4")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    if tmp.exists():
        try:
            tmp.unlink()
        except OSError:
            return path
    command = [
        ffmpeg_path,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-fflags",
        "+genpts",
        "-i",
        str(path),
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-c",
        "copy",
        "-avoid_negative_ts",
        "make_zero",
        "-movflags",
        "+faststart",
        str(tmp),
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            **_build_subprocess_kwargs(),
        )
        if result.returncode != 0 or not tmp.is_file() or tmp.stat().st_size <= 0:
            return path
        tmp.replace(proxy)
        return proxy
    except Exception:
        return path


def _should_prepare_proxy(path: Path) -> bool:
    try:
        return (
            path.suffix.lower() in _VIDEO_CONTAINER_SUFFIXES
            and path.is_file()
            and path.stat().st_size > 0
        )
    except OSError:
        return False


def _resolve_ffmpeg_path() -> str | None:
    ffmpeg_dir: Path | None = None
    try:
        raw = (load_app_settings().ffmpeg_dir or "").strip()
        if raw:
            ffmpeg_dir = Path(raw)
    except Exception:
        ffmpeg_dir = None
    try:
        return find_tool("ffmpeg", ffmpeg_dir)
    except Exception:
        try:
            return find_tool("ffmpeg.exe", ffmpeg_dir)
        except Exception:
            return None


def _proxy_path_for(path: Path) -> Path:
    stat = path.stat()
    key = f"{path.resolve()}|{stat.st_size}|{stat.st_mtime_ns}".encode("utf-8", "surrogatepass")
    digest = hashlib.sha256(key).hexdigest()[:24]
    cache_dir = Path(tempfile.gettempdir()) / _PREVIEW_CACHE_DIR
    return cache_dir / f"{path.stem}-{digest}.mp4"
