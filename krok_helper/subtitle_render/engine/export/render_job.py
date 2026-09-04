"""Stable input contract for subtitle video export."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from krok_helper.subtitle_render.domain.background import BackgroundSource
from krok_helper.subtitle_render.domain.models import Style
from krok_helper.subtitle_render.domain.timing import TimingTrack


OUTPUT_FORMAT_MP4 = "mp4"
OUTPUT_FORMAT_PNG_TRANSPARENT = "png_transparent"
OUTPUT_FORMAT_PNG_COMPOSITED = "png_composited"
OUTPUT_FORMAT_MOV_TRANSPARENT = "mov_transparent"
"""Export output formats selectable on the export page."""

OUTPUT_FORMATS = (
    OUTPUT_FORMAT_MP4,
    OUTPUT_FORMAT_PNG_TRANSPARENT,
    OUTPUT_FORMAT_PNG_COMPOSITED,
    OUTPUT_FORMAT_MOV_TRANSPARENT,
)


def is_png_sequence(output_format: str) -> bool:
    """Whether the format writes numbered PNG frames into ``output_path``."""

    return output_format in {
        OUTPUT_FORMAT_PNG_TRANSPARENT,
        OUTPUT_FORMAT_PNG_COMPOSITED,
    }


def format_has_alpha(output_format: str) -> bool:
    """Whether the exported frames keep an alpha (transparency) channel."""

    return output_format in {
        OUTPUT_FORMAT_PNG_TRANSPARENT,
        OUTPUT_FORMAT_MOV_TRANSPARENT,
    }


def format_needs_background(output_format: str) -> bool:
    """Whether the format composites the subtitle layer over a background."""

    return output_format in {
        OUTPUT_FORMAT_MP4,
        OUTPUT_FORMAT_PNG_COMPOSITED,
    }


@dataclass(frozen=True)
class RenderJob:
    """All user-selected inputs required to render one subtitle video."""

    track: TimingTrack
    style: Style
    background_video_path: Path
    output_path: Path
    """MP4/MOV: the output file; PNG sequence: the frame folder itself."""
    background_source: BackgroundSource | None = None
    audio_path: Path | None = None
    width: int = 1920
    height: int = 1080
    fps: int = 60
    duration_ms: int | None = None
    include_audio: bool = True
    """MP4 only; PNG sequence and transparent MOV exports carry no audio."""
    encoder_mode: str = "cpu"
    crf: int = 18
    preset: str = "medium"
    codec: str = "h264"
    output_format: str = OUTPUT_FORMAT_MP4
    native_export_enabled: bool | None = None
    gpu_export_enabled: bool | None = None
    render_workers: int | None = None
    """Frame-rendering process count; ``None`` selects the automatic policy."""
    extra_tracks: tuple[TimingTrack, ...] = ()
    """Additional subtitle sources composited after the primary track."""
