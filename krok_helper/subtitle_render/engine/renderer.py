"""ffmpeg rawvideo pipe renderer for subtitle videos.

A8 MVP renders a transparent subtitle overlay with QPainter and lets ffmpeg
compose it over the background video.  Audio is copied from the background video
when present.
"""

from __future__ import annotations

import math
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from PyQt6.QtGui import QColor, QImage

from krok_helper.errors import ExportCancelled, ProcessingError
from krok_helper.ffmpeg import _build_subprocess_kwargs, find_tool, terminate_process
from krok_helper.subtitle_render.engine.painter import paint_frame
from krok_helper.subtitle_render.engine.timeline import track_duration_ms
from krok_helper.subtitle_render.models import Style, TimingTrack
from krok_helper.types import Logger


@dataclass(frozen=True)
class RenderJob:
    track: TimingTrack
    style: Style
    background_video_path: Path
    output_path: Path
    width: int = 1920
    height: int = 1080
    fps: int = 60
    duration_ms: int | None = None
    include_audio: bool = True


def render_subtitle_video(
    job: RenderJob,
    *,
    ffmpeg_dir: Path | None = None,
    logger: Logger | None = None,
    should_cancel: Callable[[], bool] | None = None,
    on_process_started: Callable[[subprocess.Popen | None], None] | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> Path:
    """Render ``job`` to MP4 using a transparent rawvideo subtitle pipe."""
    logger = logger or (lambda _message: None)
    _validate_job(job)
    ffmpeg_path = find_tool("ffmpeg.exe", ffmpeg_dir)

    duration_ms = _resolve_duration_ms(job)
    total_frames = _frame_count(duration_ms, job.fps)
    command = build_render_command(ffmpeg_path, job, duration_ms=duration_ms)

    job.output_path.parent.mkdir(parents=True, exist_ok=True)
    logger(f"导出字幕视频: {job.output_path.name}")
    logger(f"输出参数: {job.width}x{job.height} / {job.fps}fps / {duration_ms / 1000:.3f}s")
    logger("执行命令:")
    logger(" ".join(f'"{part}"' if " " in part else part for part in command))

    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        **_build_subprocess_kwargs(),
    )
    if on_process_started is not None:
        on_process_started(process)

    try:
        assert process.stdin is not None
        for index in range(total_frames):
            if should_cancel is not None and should_cancel():
                terminate_process(process)
                raise ExportCancelled("已停止导出。")
            t_ms = int(round(index * 1000 / job.fps))
            process.stdin.write(_render_overlay_frame(job.track, job.style, t_ms, job.width, job.height))
            if on_progress is not None:
                on_progress(index + 1, total_frames)
        process.stdin.close()
        _drain_process_output(process, logger)
        return_code = process.wait()
    finally:
        if on_process_started is not None:
            on_process_started(None)

    if should_cancel is not None and should_cancel():
        _remove_incomplete_output(job.output_path, logger)
        raise ExportCancelled("已停止导出。")
    if return_code != 0:
        _remove_incomplete_output(job.output_path, logger)
        raise ProcessingError(f"ffmpeg 执行失败，退出码: {return_code}")
    if not job.output_path.is_file() or os.path.getsize(job.output_path) == 0:
        raise ProcessingError(f"导出失败，未生成有效文件: {job.output_path}")

    logger(f"字幕视频导出完成: {job.output_path}")
    return job.output_path


def build_render_command(ffmpeg_path: str, job: RenderJob, *, duration_ms: int | None = None) -> list[str]:
    """Build the ffmpeg command used by :func:`render_subtitle_video`."""
    duration = _resolve_duration_ms(job) if duration_ms is None else duration_ms
    duration_seconds = max(duration / 1000.0, 0.001)
    filter_graph = (
        f"[1:v:0]scale={job.width}:{job.height}:force_original_aspect_ratio=decrease,"
        f"pad={job.width}:{job.height}:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"fps={job.fps},trim=duration={duration_seconds:.6f},setpts=PTS-STARTPTS[bg];"
        "[0:v:0]format=rgba,setpts=PTS-STARTPTS[ov];"
        "[bg][ov]overlay=0:0:format=auto[v]"
    )
    command = [
        ffmpeg_path,
        "-y",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgba",
        "-s:v",
        f"{job.width}x{job.height}",
        "-r",
        str(job.fps),
        "-i",
        "pipe:0",
        "-i",
        str(job.background_video_path),
        "-filter_complex",
        filter_graph,
        "-map",
        "[v]",
    ]
    if job.include_audio:
        command.extend(["-map", "1:a:0?"])
    command.extend(
        [
            "-t",
            f"{duration_seconds:.6f}",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
        ]
    )
    if job.include_audio:
        command.extend(["-c:a", "aac", "-b:a", "192k"])
    command.extend(["-movflags", "+faststart", str(job.output_path)])
    return command


def _validate_job(job: RenderJob) -> None:
    if job.track.char_count <= 0:
        raise ProcessingError("请先加载有效的字幕文件。")
    if not job.background_video_path.is_file():
        raise ProcessingError(f"请先加载背景视频: {job.background_video_path}")
    if job.width <= 0 or job.height <= 0:
        raise ProcessingError("输出分辨率无效。")
    if job.fps <= 0:
        raise ProcessingError("输出 fps 无效。")
    if not str(job.output_path).strip():
        raise ProcessingError("请先选择输出路径。")


def _resolve_duration_ms(job: RenderJob) -> int:
    if job.duration_ms is not None and job.duration_ms > 0:
        return job.duration_ms
    duration = track_duration_ms(job.track)
    if duration <= 0:
        raise ProcessingError("字幕时长无效，无法导出。")
    return duration


def _frame_count(duration_ms: int, fps: int) -> int:
    return max(1, int(math.ceil(duration_ms * fps / 1000)))


def _render_overlay_frame(
    track: TimingTrack,
    style: Style,
    t_ms: int,
    width: int,
    height: int,
) -> bytes:
    image = QImage(width, height, QImage.Format.Format_RGBA8888)
    image.fill(QColor(0, 0, 0, 0))
    paint_frame(image, track, t_ms, style)
    bits = image.constBits()
    bits.setsize(image.sizeInBytes())
    return bytes(bits)


def _drain_process_output(process: subprocess.Popen, logger: Logger) -> None:
    if process.stdout is None:
        return
    for raw in process.stdout:
        if isinstance(raw, bytes):
            line = raw.decode("utf-8", errors="replace").strip()
        else:
            line = str(raw).strip()
        if line:
            logger(line)


def _remove_incomplete_output(output_path: Path, logger: Logger) -> None:
    if not output_path.exists():
        return
    try:
        output_path.unlink()
        logger(f"已清理未完成的输出文件: {output_path}")
    except OSError as exc:
        logger(f"清理未完成的输出文件失败: {output_path} ({exc})")
