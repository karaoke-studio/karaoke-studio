"""Stable input contract for subtitle video export."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from krok_helper.subtitle_render.domain.background import BackgroundSource
from krok_helper.subtitle_render.domain.models import Style
from krok_helper.subtitle_render.domain.timing import TimingTrack


@dataclass(frozen=True)
class RenderJob:
    """All user-selected inputs required to render one subtitle video."""

    track: TimingTrack
    style: Style
    background_video_path: Path | None
    output_path: Path
    background_source: BackgroundSource | None = None
    audio_path: Path | None = None
    width: int = 1920
    height: int = 1080
    fps: int = 60
    duration_ms: int | None = None
    include_audio: bool = True
    encoder_mode: str = "cpu"
    crf: int = 18
    preset: str = "medium"
    codec: str = "h264"
    native_export_enabled: bool | None = None
    gpu_export_enabled: bool | None = None
    render_workers: int | None = None
    """Frame-rendering process count; ``None`` selects the automatic policy."""
    extra_tracks: tuple[TimingTrack, ...] = ()
    """Additional subtitle sources composited after the primary track."""
