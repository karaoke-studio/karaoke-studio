"""Validation and derived values for the subtitle export job contract."""

from __future__ import annotations

from pathlib import Path

from krok_helper.errors import ProcessingError
from krok_helper.subtitle_render.domain.background import BackgroundSource
from krok_helper.subtitle_render.engine.export.encoder_select import (
    CPU_PRESETS,
    ENCODER_MODES,
    VIDEO_CODECS,
)
from krok_helper.subtitle_render.engine.export.render_job import RenderJob
from krok_helper.subtitle_render.engine.timing.timeline import track_duration_ms
from krok_helper.subtitle_render.domain.timing import TimingTrack


def job_tracks(job: RenderJob) -> list[TimingTrack]:
    """Return the primary and additional tracks in compositing order."""

    return [job.track, *job.extra_tracks]


def resolved_background(job: RenderJob) -> BackgroundSource:
    """Resolve legacy video-path input into the current background contract."""

    if job.background_source is not None:
        return job.background_source
    if job.background_video_path is not None:
        return BackgroundSource(kind="video", path=str(job.background_video_path))
    return BackgroundSource(kind="solid", color="#000000")


def validate_render_job(job: RenderJob) -> None:
    """Reject invalid jobs before ffmpeg or a frame renderer is started."""

    if all(track.char_count <= 0 for track in job_tracks(job)):
        raise ProcessingError("请先加载有效的字幕文件。")
    background = resolved_background(job)
    if background.kind == "video" and job.audio_path is not None:
        raise ProcessingError("视频背景不支持独立音频，请使用视频内嵌音轨。")
    if background.kind in {"video", "image", "image_sequence"}:
        if not background.path:
            raise ProcessingError("请先选择背景素材。")
        path = Path(background.path)
        if background.kind != "image_sequence" and not path.is_file():
            raise ProcessingError(f"背景素材不存在: {path}")
        if background.kind == "image_sequence" and not (
            path.exists() or path.parent.exists()
        ):
            raise ProcessingError(f"背景图片序列不存在: {path}")
    if job.audio_path is not None and not job.audio_path.is_file():
        raise ProcessingError(f"独立音频不存在: {job.audio_path}")
    if job.width <= 0 or job.height <= 0:
        raise ProcessingError("输出分辨率无效。")
    if job.width % 2 != 0 or job.height % 2 != 0:
        raise ProcessingError(
            f"输出宽度和高度必须是偶数（当前 {job.width}×{job.height}）："
            "H.264/H.265 编码的 yuv420p 像素格式不支持奇数尺寸。"
        )
    if job.fps <= 0:
        raise ProcessingError("输出 fps 无效。")
    if job.encoder_mode not in ENCODER_MODES:
        raise ProcessingError(f"不支持的编码器: {job.encoder_mode}")
    if job.codec not in VIDEO_CODECS:
        raise ProcessingError(f"不支持的视频编码: {job.codec}")
    if not 0 <= job.crf <= 51:
        raise ProcessingError("CRF 必须在 0 到 51 之间。")
    if job.preset not in CPU_PRESETS:
        raise ProcessingError(f"不支持的 CPU preset: {job.preset}")
    if not str(job.output_path).strip():
        raise ProcessingError("请先选择输出路径。")


def resolve_duration_ms(job: RenderJob) -> int:
    """Resolve the explicit or track-derived export duration."""

    if job.duration_ms is not None and job.duration_ms > 0:
        return job.duration_ms
    duration = max(track_duration_ms(track) for track in job_tracks(job))
    if duration <= 0:
        raise ProcessingError("字幕时长无效，无法导出。")
    return duration
