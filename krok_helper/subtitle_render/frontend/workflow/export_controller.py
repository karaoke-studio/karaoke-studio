"""Render-job assembly contracts independent from concrete export widgets."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from krok_helper.errors import ProcessingError
from krok_helper.subtitle_render.domain.background import BackgroundSource
from krok_helper.subtitle_render.engine.export.render_job import (
    OUTPUT_FORMAT_MP4,
    OUTPUT_FORMAT_MOV_QTRLE,
    OUTPUT_FORMAT_MOV_TRANSPARENT,
    RenderJob,
    format_needs_background,
    is_png_sequence,
)
from krok_helper.subtitle_render.engine.export.render_job_policy import (
    ensure_output_is_not_input,
    validate_output_target,
    validate_sequence_output_target,
)
from krok_helper.subtitle_render.engine.timing.timeline import track_duration_ms
from krok_helper.subtitle_render.domain.models import Style
from krok_helper.subtitle_render.domain.timing import TimingTrack


@dataclass(frozen=True)
class ExportJobInputs:
    """Widget-independent snapshot used to assemble one export job."""

    track: TimingTrack | None
    style: Style
    background_video_path: Path | None
    background_source: BackgroundSource | None
    audio_path: Path | None
    output_directory: str
    output_name: str
    default_output_name: str
    extra_tracks: tuple[TimingTrack, ...]
    width: int
    height: int
    fps: int
    duration_ms: int
    include_audio: bool
    encoder_mode: str
    crf: int
    preset: str
    codec: str
    gpu_export_enabled: bool
    render_workers: int | None
    output_format: str = OUTPUT_FORMAT_MP4


@dataclass(frozen=True)
class ExportJobBuildResult:
    """Assembled job plus the output-name decision needed by the host UI."""

    job: RenderJob
    output_name: str
    used_default_name: bool


class ExportJobController:
    """Validate export prerequisites and assemble the stable engine contract."""

    @staticmethod
    def resolve_duration_ms(
        tracks: Any,
        *,
        video_info: Any = None,
        audio_info: Any = None,
    ) -> int:
        candidates = [track_duration_ms(track) for track in tracks]
        for media_info in (video_info, audio_info):
            if media_info is not None and media_info.duration > 0:
                candidates.append(int(round(media_info.duration * 1000)))
        return max(candidates, default=0)

    @staticmethod
    def build(inputs: ExportJobInputs) -> ExportJobBuildResult:
        if inputs.track is None:
            raise ProcessingError("请先加载字幕文件。")
        output_format = inputs.output_format
        if format_needs_background(output_format) and inputs.background_source is None:
            raise ProcessingError("请先选择背景源。")
        directory = inputs.output_directory.strip()
        if not directory:
            raise ProcessingError("请先选择输出文件夹。")

        output_name = inputs.output_name or inputs.default_output_name
        used_default_name = not bool(inputs.output_name)
        if is_png_sequence(output_format):
            # PNG 序列独占以导出名命名的子文件夹，帧为 <名称>_000001.png。
            output_path = Path(directory).expanduser() / output_name
        elif output_format in {OUTPUT_FORMAT_MOV_TRANSPARENT, OUTPUT_FORMAT_MOV_QTRLE}:
            output_path = Path(directory).expanduser() / f"{output_name}.mov"
        else:
            output_path = Path(directory).expanduser() / f"{output_name}.mp4"
        job = RenderJob(
            track=inputs.track,
            style=inputs.style,
            background_video_path=inputs.background_video_path,
            background_source=inputs.background_source,
            audio_path=inputs.audio_path,
            output_path=output_path,
            extra_tracks=inputs.extra_tracks,
            width=inputs.width,
            height=inputs.height,
            fps=inputs.fps,
            duration_ms=inputs.duration_ms,
            include_audio=inputs.include_audio,
            encoder_mode=inputs.encoder_mode,
            crf=inputs.crf,
            preset=inputs.preset,
            codec=inputs.codec,
            output_format=output_format,
            native_export_enabled=False,
            gpu_export_enabled=inputs.gpu_export_enabled,
            render_workers=inputs.render_workers,
        )
        # 组装阶段就拦下「导出名压在素材上」与非法/超长输出路径：留到渲染线程
        # 才报错的话，用户要先走一遍保存工程弹窗、看进度条起跑，才等来一句失败。
        if is_png_sequence(output_format):
            validate_sequence_output_target(output_path, output_name=output_name)
        else:
            validate_output_target(output_path, output_name=output_name)
        ensure_output_is_not_input(job)
        return ExportJobBuildResult(
            job=job,
            output_name=output_name,
            used_default_name=used_default_name,
        )
