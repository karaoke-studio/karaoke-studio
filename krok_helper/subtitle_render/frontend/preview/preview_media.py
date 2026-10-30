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
import uuid
import threading
import tempfile
from dataclasses import dataclass
from pathlib import Path

from krok_helper.app_paths import temp_dir
from krok_helper.ffmpeg import _build_subprocess_kwargs, find_tool
from krok_helper.settings import load_app_settings


_VIDEO_CONTAINER_SUFFIXES = {".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi", ".flv"}
#: 代理缓存子目录（%TEMP%\LinKLyrics\preview-cache，统一临时根见 app_paths）。
_PREVIEW_CACHE_DIR_NAME = "preview-cache"
_VIDEO_PROXY_MAX_HEIGHT = {"low": 540, "medium": 1080}
_VIDEO_PROXY_PROFILE_VERSION = 2
#: 当前播放器正在使用的普通背景/前黑/后黑源文件及引用数；清理时保留其全部画质代理。
_ACTIVE_PREVIEW_SOURCES: dict[Path, int] = {}
#: 正在写入的临时代理；缓存清扫不能删掉别的线程正在生成的文件。
_ACTIVE_TEMPORARIES: set[Path] = set()
_PREPARATION_LOCKS: dict[tuple[str, str], threading.Lock] = {}
_PREPARATION_LOCK_GUARD = threading.Lock()


@dataclass(frozen=True)
class QtPlaybackPreparation:
    """One cacheable ffmpeg job that prepares a Qt preview source."""

    source: Path
    target: Path
    temporary: Path
    command: tuple[str, ...]


def _preview_cache_dir() -> Path:
    return temp_dir(_PREVIEW_CACHE_DIR_NAME)


def protect_preview_source(path: Path | None) -> None:
    """登记正在使用的源文件；缓存清扫保留其 low/medium/high 全部代理。"""

    if path is None:
        return
    try:
        source = Path(path)
    except TypeError:
        return
    if _should_prepare_proxy(source):
        _ACTIVE_PREVIEW_SOURCES[source] = _ACTIVE_PREVIEW_SOURCES.get(source, 0) + 1


def unprotect_preview_source(path: Path | None) -> None:
    if path is None:
        return
    try:
        source = Path(path)
    except TypeError:
        return
    count = _ACTIVE_PREVIEW_SOURCES.get(source, 0)
    if count <= 1:
        _ACTIVE_PREVIEW_SOURCES.pop(source, None)
    else:
        _ACTIVE_PREVIEW_SOURCES[source] = count - 1


def purge_preview_cache(keep: tuple[Path, ...] | set[Path] = ()) -> None:
    """清扫视频代理缓存目录：删除所有代理（含 ``*.tmp.mp4`` 半成品）。

    ``keep`` 里的路径豁免——用于「加载新素材」场景下保留**同一源文件**全部画质
    的代理（画质来回切换不重转）。当前普通背景/前黑/后黑正在使用的源文件也
    通过 :func:`protect_preview_source` 登记，其全部画质代理一并豁免。代理可
    再生——删了下次预览按源文件重新转码一次即可，所以不做保留期。正被本进程
    或另一实例的播放器 / ffmpeg 占用的文件在 Windows 上删除会失败，静默跳过。
    非 ``.mp4`` 条目不动。
    """
    protected = set(keep)
    for source in tuple(_ACTIVE_PREVIEW_SOURCES):
        for quality in ("low", "medium", "high"):
            try:
                protected.add(_playback_target_for(source, quality))
            except (OSError, ValueError, TypeError):
                continue
    try:
        entries = list(_preview_cache_dir().iterdir())
    except OSError:
        return
    for entry in entries:
        try:
            if (
                entry.is_file()
                and entry.suffix == ".mp4"
                and entry not in protected
                and entry not in _ACTIVE_TEMPORARIES
            ):
                entry.unlink()
        except OSError:
            continue


def qt_playback_source(path: Path, preview_quality: object = "high", *,
                       progress_callback=None, duration_seconds: float = 0) -> Path:
    """Coalesce concurrent requests for the same preview into one preparation."""
    key = (str(Path(path).resolve()), normalize_preview_media_quality(preview_quality))
    with _PREPARATION_LOCK_GUARD:
        lock = _PREPARATION_LOCKS.setdefault(key, threading.Lock())
    with lock:
        return _prepare_qt_playback_source(path, preview_quality,
            progress_callback=progress_callback, duration_seconds=duration_seconds)


def _prepare_qt_playback_source(path: Path, preview_quality: object = "high", *,
                                progress_callback=None, duration_seconds: float = 0) -> Path:
    """Return a Qt-friendly preview source for ``path`` when possible."""
    path = Path(path)
    ready = prepared_qt_playback_source(path, preview_quality)
    if ready is not None:
        return ready
    preparation = qt_playback_preparation(path, preview_quality)
    if preparation is None:
        return path
    try:
        if progress_callback is None:
            result = subprocess.run(
                list(preparation.command), capture_output=True, text=True,
                encoding="utf-8", errors="replace", check=False,
                **_build_subprocess_kwargs(),
            )
            returncode = result.returncode
        else:
            command = list(preparation.command)
            command[1:1] = ["-progress", "pipe:1", "-nostats"]
            with tempfile.TemporaryFile() as errors:
                process = subprocess.Popen(command, stdout=subprocess.PIPE,
                    stderr=errors, text=True, encoding="utf-8", errors="replace",
                    **_build_subprocess_kwargs())
                try:
                    for line in process.stdout:
                        if line.startswith("out_time_us=") and duration_seconds > 0:
                            try:
                                percent = min(99, max(0, int(int(line.strip().split("=", 1)[1]) / (duration_seconds * 10000))))
                            except ValueError:
                                continue
                            progress_callback(percent)
                    returncode = process.wait()
                finally:
                    process.stdout.close()
                    if process.poll() is None:
                        process.kill()
                        process.wait()
        if returncode != 0 or not finalize_qt_playback_preparation(preparation):
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
    # 走到这里说明该画质缓存未命中 = 加载了新/变动的素材（或切换画质）：建新
    # 代理前先清掉**其它源文件**的旧代理；同一源文件全部画质的代理豁免，
    # 画质来回切换不必重转。正被占用的文件删除失败自然跳过。
    purge_preview_cache(
        keep={
            _playback_target_for(path, proxy_quality)
            for proxy_quality in ("low", "medium", "high")
        }
    )
    temporary = target.with_name(
        f"{target.stem}.{uuid.uuid4().hex}.tmp{target.suffix}"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    _ACTIVE_TEMPORARIES.add(temporary)
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
    finally:
        _ACTIVE_TEMPORARIES.discard(temporary)


def discard_qt_playback_preparation(preparation: QtPlaybackPreparation) -> None:
    """Remove an incomplete temporary proxy, leaving any shared cache intact."""
    temporary = preparation.temporary
    _ACTIVE_TEMPORARIES.discard(temporary)
    try:
        if temporary.exists():
            temporary.unlink()
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
    return _preview_cache_dir() / f"{path.stem}-{digest}.mp4"


def _scaled_proxy_path_for(path: Path, quality: str) -> Path:
    stat = path.stat()
    max_height = _VIDEO_PROXY_MAX_HEIGHT[quality]
    key = (
        f"{path.resolve()}|{stat.st_size}|{stat.st_mtime_ns}|"
        f"scaled-v{_VIDEO_PROXY_PROFILE_VERSION}|{max_height}p"
    ).encode("utf-8", "surrogatepass")
    digest = hashlib.sha256(key).hexdigest()[:24]
    return _preview_cache_dir() / f"{path.stem}-{digest}-{max_height}p.mp4"


def _playback_target_for(path: Path, preview_quality: object) -> Path:
    quality = normalize_preview_media_quality(preview_quality)
    if quality in _VIDEO_PROXY_MAX_HEIGHT:
        return _scaled_proxy_path_for(path, quality)
    return _proxy_path_for(path)


def normalize_preview_media_quality(value: object) -> str:
    quality = str(value or "").strip().lower()
    return quality if quality in {"low", "medium", "high"} else "high"
