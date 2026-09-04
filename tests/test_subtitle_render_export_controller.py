"""Focused contracts for export-job assembly outside the main window."""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from krok_helper.errors import ProcessingError
from krok_helper.subtitle_render.domain.background import BackgroundSource
from krok_helper.subtitle_render.frontend.workflow.export_controller import (
    ExportJobController,
    ExportJobInputs,
)
from krok_helper.subtitle_render.domain.models import Style
from krok_helper.subtitle_render.domain.timing import TimingChar, TimingLine, TimingTrack


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


def test_export_job_controller_rejects_output_over_the_background_video(
    tmp_path,
) -> None:
    """导出名压在背景视频上时，组装阶段就要报错（别等渲染线程才失败）。"""

    background = tmp_path / "song.mp4"
    background.write_bytes(b"source-video-bytes")
    inputs = replace(
        _inputs(tmp_path),
        background_source=BackgroundSource(kind="video", path=str(background)),
        audio_path=None,
        output_name="song",
    )

    with pytest.raises(ProcessingError, match="不能覆盖素材本身"):
        ExportJobController.build(inputs)

    assert background.read_bytes() == b"source-video-bytes"


def test_export_job_controller_rejects_output_name_that_is_existing_directory(
    tmp_path,
) -> None:
    (tmp_path / "result.mp4").mkdir()

    with pytest.raises(ProcessingError, match="同名文件夹"):
        ExportJobController.build(_inputs(tmp_path))


@pytest.mark.skipif(os.name != "nt", reason="Windows 文件名规则仅限 nt 校验")
@pytest.mark.parametrize(
    "output_name",
    ['a<b', "a:b", 'a"b', "a/b", "a\\b", "a|b", "a?b", "a*b"],
)
def test_export_job_controller_rejects_windows_illegal_output_name(
    tmp_path, output_name
) -> None:
    with pytest.raises(ProcessingError, match="不允许的字符"):
        ExportJobController.build(replace(_inputs(tmp_path), output_name=output_name))


@pytest.mark.skipif(os.name != "nt", reason="Windows 保留设备名仅限 nt 校验")
@pytest.mark.parametrize("output_name", ["CON", "con", "NUL.mp3", "COM1"])
def test_export_job_controller_rejects_windows_reserved_output_name(
    tmp_path, output_name
) -> None:
    with pytest.raises(ProcessingError, match="保留设备名"):
        ExportJobController.build(replace(_inputs(tmp_path), output_name=output_name))


@pytest.mark.skipif(os.name != "nt", reason="MAX_PATH 限制仅限 Windows")
def test_export_job_controller_rejects_overlong_output_path(tmp_path) -> None:
    with pytest.raises(ProcessingError, match="路径过长"):
        ExportJobController.build(
            replace(_inputs(tmp_path), output_name="x" * 300)
        )


def test_export_job_controller_resolves_longest_track_or_media_duration() -> None:
    duration = ExportJobController.resolve_duration_ms(
        [_track(1_500), _track(2_000)],
        video_info=SimpleNamespace(duration=2.3456),
        audio_info=SimpleNamespace(duration=3.4567),
    )

    assert duration == 3_457


# ---------------------------------------------------------------------------
# 输出格式：PNG 序列（透明/含背景）与透明 MOV
# ---------------------------------------------------------------------------


def test_export_job_controller_builds_png_sequence_folder_contract(tmp_path) -> None:
    result = ExportJobController.build(
        replace(_inputs(tmp_path), output_format="png_transparent")
    )

    # PNG 序列独占以导出名命名的子文件夹，帧为 <名称>_000001.png。
    assert result.job.output_path == tmp_path / "result"
    assert result.job.output_format == "png_transparent"


def test_export_job_controller_builds_mov_transparent_file_contract(tmp_path) -> None:
    result = ExportJobController.build(
        replace(_inputs(tmp_path), output_format="mov_transparent")
    )

    assert result.job.output_path == tmp_path / "result.mov"
    assert result.job.output_format == "mov_transparent"


def test_export_job_controller_builds_mov_qtrle_file_contract(tmp_path) -> None:
    result = ExportJobController.build(
        replace(_inputs(tmp_path), output_format="mov_qtrle")
    )

    assert result.job.output_path == tmp_path / "result.mov"
    assert result.job.output_format == "mov_qtrle"


@pytest.mark.parametrize("output_format", ["png_transparent", "mov_transparent"])
def test_transparent_formats_do_not_require_background(
    tmp_path, output_format
) -> None:
    """透明格式只导出字幕层，没有背景源也允许导出。"""

    result = ExportJobController.build(
        replace(
            _inputs(tmp_path),
            background_source=None,
            background_video_path=None,
            output_format=output_format,
        )
    )

    assert result.job.output_format == output_format


def test_png_sequence_rejects_existing_non_empty_folder(tmp_path) -> None:
    """序列目录已存在且非空时拦下，避免新旧两次导出的帧混在一起。"""

    (tmp_path / "result").mkdir()
    (tmp_path / "result" / "old_000001.png").write_bytes(b"x")

    with pytest.raises(ProcessingError, match="不是空的"):
        ExportJobController.build(
            replace(_inputs(tmp_path), output_format="png_composited")
        )


def test_png_sequence_allows_existing_empty_folder(tmp_path) -> None:
    (tmp_path / "result").mkdir()

    result = ExportJobController.build(
        replace(_inputs(tmp_path), output_format="png_transparent")
    )

    assert result.job.output_path == tmp_path / "result"
