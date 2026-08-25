"""Render-job assembly contracts independent from concrete export widgets."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from krok_helper.errors import ProcessingError
from krok_helper.subtitle_render.domain.background import BackgroundSource
from krok_helper.subtitle_render.engine.export.render_job import RenderJob
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
        if inputs.background_source is None:
            raise ProcessingError("请先选择背景源。")
        directory = inputs.output_directory.strip()
        if not directory:
            raise ProcessingError("请先选择输出文件夹。")

        output_name = inputs.output_name or inputs.default_output_name
        used_default_name = not bool(inputs.output_name)
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
            native_export_enabled=False,
            gpu_export_enabled=inputs.gpu_export_enabled,
            render_workers=inputs.render_workers,
        )
        return ExportJobBuildResult(
            job=job,
            output_name=output_name,
            used_default_name=used_default_name,
        )
