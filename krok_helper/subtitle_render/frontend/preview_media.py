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
import uuid
from dataclasses import dataclass
from pathlib import Path

from krok_helper.ffmpeg import _build_subprocess_kwargs, find_tool
from krok_helper.settings import load_app_settings


_VIDEO_CONTAINER_SUFFIXES = {".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi", ".flv"}
_PREVIEW_CACHE_DIR = "KaraokeStudioPreviewCache"
_VIDEO_PROXY_MAX_HEIGHT = {"low": 540, "medium": 1080}
_VIDEO_PROXY_PROFILE_VERSION = 2


@dataclass(frozen=True)
class QtPlaybackPreparation:
    """One cacheable ffmpeg job that prepares a Qt preview source."""

    source: Path
    target: Path
    temporary: Path
    command: tuple[str, ...]


def qt_playback_source(path: Path, preview_quality: object = "high") -> Path:
    """Return a Qt-friendly preview source for ``path`` when possible."""
    path = Path(path)
    ready = prepared_qt_playback_source(path, preview_quality)
    if ready is not None:
        return ready
    preparation = qt_playback_preparation(path, preview_quality)
    if preparation is None:
        return path
    try:
        result = subprocess.run(
            list(preparation.command),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            **_build_subprocess_kwargs(),
        )
        if result.returncode != 0 or not finalize_qt_playback_preparation(preparation):
            discard_qt_playback_preparation(preparation)
            return path
        return preparation.target
    except Exception:
        discard_qt_playback_preparation(preparation)
        return path


def prepared_qt_playback_source(
    path: Path,
    preview_quality: object = "high",
) -> Path | None:
    """Return an already prepared source, the original non-video, or ``None``."""
    path = Path(path)
    if not _should_prepare_proxy(path):
        return path
    target = _playback_target_for(path, preview_quality)
    try:
        return target if target.is_file() and target.stat().st_size > 0 else None
    except OSError:
        return None


def qt_playback_preparation(
    path: Path,
    preview_quality: object = "high",
) -> QtPlaybackPreparation | None:
    """Build an ffmpeg preparation job without executing it."""
    path = Path(path)
    if not _should_prepare_proxy(path):
        return None
    ready = prepared_qt_playback_source(path, preview_quality)
    if ready is not None:
        return None
    ffmpeg_path = _resolve_ffmpeg_path()
    if ffmpeg_path is None:
        return None
    quality = normalize_preview_media_quality(preview_quality)
    target = _playback_target_for(path, quality)
    temporary = target.with_name(
        f"{target.stem}.{uuid.uuid4().hex}.tmp{target.suffix}"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    command = _build_qt_playback_command(
        ffmpeg_path,
        path,
        temporary,
        quality,
    )
    return QtPlaybackPreparation(path, target, temporary, tuple(command))


def finalize_qt_playback_preparation(preparation: QtPlaybackPreparation) -> bool:
    """Publish a completed temporary proxy into the shared preview cache."""
    temporary = preparation.temporary
    target = preparation.target
    try:
        if not temporary.is_file() or temporary.stat().st_size <= 0:
            return False
        if target.is_file() and target.stat().st_size > 0:
            temporary.unlink()
        else:
            temporary.replace(target)
        return True
    except OSError:
        return False


def discard_qt_playback_preparation(preparation: QtPlaybackPreparation) -> None:
    """Remove an incomplete temporary proxy, leaving any shared cache intact."""
    try:
        if preparation.temporary.exists():
            preparation.temporary.unlink()
    except OSError:
        pass


def _build_qt_playback_command(
    ffmpeg_path: str,
    source: Path,
    output: Path,
    quality: str,
) -> list[str]:
    command = [
        ffmpeg_path,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-fflags",
        "+genpts",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
    ]
    max_height = _VIDEO_PROXY_MAX_HEIGHT.get(quality)
    if max_height is None:
        command.extend(
            [
                "-c",
                "copy",
                "-avoid_negative_ts",
                "make_zero",
            ]
        )
    else:
        max_width = max_height * 16 // 9
        scale = (
            f"scale=w='min(iw,{max_width})':h='min(ih,{max_height})':"
            "force_original_aspect_ratio=decrease:force_divisible_by=2"
        )
        command.extend(
            [
                "-vf",
                scale,
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "23",
                "-pix_fmt",
                "yuv420p",
                "-fps_mode",
                "passthrough",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
            ]
        )
    command.extend(
        [
            "-movflags",
            "+faststart",
            str(output),
        ]
    )
    return command


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


def _scaled_proxy_path_for(path: Path, quality: str) -> Path:
    stat = path.stat()
    max_height = _VIDEO_PROXY_MAX_HEIGHT[quality]
    key = (
        f"{path.resolve()}|{stat.st_size}|{stat.st_mtime_ns}|"
        f"scaled-v{_VIDEO_PROXY_PROFILE_VERSION}|{max_height}p"
    ).encode("utf-8", "surrogatepass")
    digest = hashlib.sha256(key).hexdigest()[:24]
    cache_dir = Path(tempfile.gettempdir()) / _PREVIEW_CACHE_DIR
    return cache_dir / f"{path.stem}-{digest}-{max_height}p.mp4"


def _playback_target_for(path: Path, preview_quality: object) -> Path:
    quality = normalize_preview_media_quality(preview_quality)
    if quality in _VIDEO_PROXY_MAX_HEIGHT:
        return _scaled_proxy_path_for(path, quality)
    return _proxy_path_for(path)


def normalize_preview_media_quality(value: object) -> str:
    quality = str(value or "").strip().lower()
    return quality if quality in {"low", "medium", "high"} else "high"
