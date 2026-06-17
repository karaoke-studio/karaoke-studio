"""Tests for A8 rawvideo renderer."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from krok_helper.errors import ProcessingError  # noqa: E402
from krok_helper.subtitle_render.engine.renderer import (  # noqa: E402
    RenderJob,
    _frame_count,
    _render_overlay_frame,
    build_render_command,
)
from krok_helper.subtitle_render.models import (  # noqa: E402
    Style,
    TimingChar,
    TimingLine,
    TimingTrack,
)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _track() -> TimingTrack:
    return TimingTrack(
        lines=[
            TimingLine(
                chars=[TimingChar("a", 0), TimingChar("b", 500)],
                end_ms=1000,
            )
        ]
    )


def _job(tmp_path: Path, *, include_audio: bool = True) -> RenderJob:
    background = tmp_path / "bg.mp4"
    background.write_bytes(b"not-real-video")
    return RenderJob(
        track=_track(),
        style=Style(font_size_px=24),
        background_video_path=background,
        output_path=tmp_path / "out.mp4",
        width=320,
        height=180,
        fps=60,
        duration_ms=1000,
        include_audio=include_audio,
    )


def test_build_render_command_contains_rawvideo_overlay_and_audio(tmp_path):
    job = _job(tmp_path, include_audio=True)

    command = build_render_command("ffmpeg", job)

    assert command[:2] == ["ffmpeg", "-y"]
    assert "-f" in command
    assert "rawvideo" in command
    assert "-pix_fmt" in command
    assert "rgba" in command
    filter_graph = command[command.index("-filter_complex") + 1]
    assert "overlay=0:0" in filter_graph
    assert "scale=320:180" in filter_graph
    assert command[command.index("-map") + 1] == "[v]"
    assert "1:a:0?" in command
    assert str(job.output_path) == command[-1]


def test_build_render_command_can_skip_audio(tmp_path):
    command = build_render_command("ffmpeg", _job(tmp_path, include_audio=False))
    assert "1:a:0?" not in command
    assert "-c:a" not in command


def test_overlay_frame_size_matches_rgba(qapp, tmp_path):
    job = _job(tmp_path)
    raw = _render_overlay_frame(job.track, job.style, 500, job.width, job.height)
    assert len(raw) == job.width * job.height * 4


def test_frame_count_ceil():
    assert _frame_count(1000, 60) == 60
    assert _frame_count(1001, 60) == 61


def test_render_job_validation_requires_subtitles(tmp_path):
    job = RenderJob(
        track=TimingTrack(),
        style=Style(),
        background_video_path=tmp_path / "bg.mp4",
        output_path=tmp_path / "out.mp4",
    )
    with pytest.raises(ProcessingError):
        build_render_command("ffmpeg", job)
