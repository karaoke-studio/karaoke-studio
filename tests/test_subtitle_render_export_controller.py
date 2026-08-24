"""Focused contracts for export-job assembly outside the main window."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from krok_helper.errors import ProcessingError
from krok_helper.subtitle_render.background import BackgroundSource
from krok_helper.subtitle_render.frontend.export_controller import (
    ExportJobController,
    ExportJobInputs,
)
from krok_helper.subtitle_render.models import Style
from krok_helper.subtitle_render.timing import TimingChar, TimingLine, TimingTrack


def _track(end_ms: int = 1_500) -> TimingTrack:
    return TimingTrack(
        lines=[
            TimingLine(
                chars=[TimingChar(text="a", start_ms=100)],
                end_ms=end_ms,
            )
        ]
    )


def _inputs(tmp_path: Path) -> ExportJobInputs:
    return ExportJobInputs(
        track=_track(),
        style=Style(),
        background_video_path=None,
        background_source=BackgroundSource(kind="solid", color="#000000"),
        audio_path=tmp_path / "song.wav",
        output_directory=str(tmp_path),
        output_name="result",
        default_output_name="fallback",
        extra_tracks=(_track(2_000),),
        width=1280,
        height=720,
        fps=120,
        duration_ms=5_000,
        include_audio=True,
        encoder_mode="nvenc",
        crf=23,
        preset="slow",
        codec="h265",
        gpu_export_enabled=True,
        render_workers=16,
    )


def test_export_job_controller_builds_complete_engine_contract(tmp_path) -> None:
    inputs = _inputs(tmp_path)

    result = ExportJobController.build(inputs)

    job = result.job
    assert result.output_name == "result"
    assert result.used_default_name is False
    assert job.output_path == tmp_path / "result.mp4"
    assert job.track is inputs.track
    assert job.style is inputs.style
    assert job.background_source is inputs.background_source
    assert job.audio_path == inputs.audio_path
    assert job.extra_tracks == inputs.extra_tracks
    assert (job.width, job.height, job.fps, job.duration_ms) == (1280, 720, 120, 5_000)
    assert (job.encoder_mode, job.crf, job.preset, job.codec) == (
        "nvenc",
        23,
        "slow",
        "h265",
    )
    assert job.native_export_enabled is False
    assert job.gpu_export_enabled is True
    assert job.render_workers == 16


def test_export_job_controller_reports_default_name_for_host_backfill(tmp_path) -> None:
    result = ExportJobController.build(replace(_inputs(tmp_path), output_name=""))

    assert result.output_name == "fallback"
    assert result.used_default_name is True
    assert result.job.output_path == tmp_path / "fallback.mp4"


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"track": None}, "请先加载字幕文件。"),
        ({"background_source": None}, "请先选择背景源。"),
        ({"output_directory": "  "}, "请先选择输出文件夹。"),
    ],
)
def test_export_job_controller_preserves_prerequisite_errors(
    tmp_path, changes, message
) -> None:
    with pytest.raises(ProcessingError, match=message):
        ExportJobController.build(replace(_inputs(tmp_path), **changes))


def test_export_job_controller_resolves_longest_track_or_media_duration() -> None:
    duration = ExportJobController.resolve_duration_ms(
        [_track(1_500), _track(2_000)],
        video_info=SimpleNamespace(duration=2.3456),
        audio_info=SimpleNamespace(duration=3.4567),
    )

    assert duration == 3_457
