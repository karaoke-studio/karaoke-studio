"""Tests for A8 rawvideo renderer."""

from __future__ import annotations

from collections import deque
from dataclasses import replace
import json
import math
import os
from pathlib import Path
import threading

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from krok_helper.errors import ExportCancelled, ProcessingError  # noqa: E402
from krok_helper.subtitle_render.engine import painter as subtitle_painter  # noqa: E402
from krok_helper.subtitle_render.engine import renderer  # noqa: E402
import numpy as np  # noqa: E402
from PyQt6.QtGui import QColor, QImage  # noqa: E402

from krok_helper.subtitle_render.engine.painter import paint_frame  # noqa: E402
import krok_helper.subtitle_render.engine.export.parallel_schedule as parallel_schedule  # noqa: E402
from krok_helper.subtitle_render.engine.export.export_command import (  # noqa: E402
    background_input_args,
    background_scale_chain,
    bands_filter_graph,
    build_render_command as build_render_command_contract,
    resolved_preview_width,
)
from krok_helper.subtitle_render.engine.export.render_job import (  # noqa: E402
    RenderJob as RenderJobContract,
)
from krok_helper.subtitle_render.engine.export.render_job_policy import (  # noqa: E402
    job_tracks,
    resolve_duration_ms,
    resolved_background,
    validate_render_job,
)
from krok_helper.subtitle_render.engine.render.render_bands import (  # noqa: E402
    merge_intervals,
    packed_offsets,
)
from krok_helper.subtitle_render.engine.render.frame_analysis import (  # noqa: E402
    FrameAnalysisPorts,
    frame_content_intervals as analyze_frame_content_intervals,
    frame_has_content as analyze_frame_has_content,
    frame_vertical_bounds as analyze_frame_vertical_bounds,
)
from krok_helper.subtitle_render.engine.render.elements.title import (  # noqa: E402
    TitleOverlayLayer,
)
from krok_helper.subtitle_render.engine.renderer import (  # noqa: E402
    RenderJob,
    _compute_content_bands,
    _compute_subtitle_strip,
    _drain_pending_head,
    _frame_count,
    _image_bytes,
    _iter_ordered_pool_results,
    _max_guide_span_em,
    _max_project_font_size,
    _merge_intervals,
    _packed_offsets,
    _paint_overlay_bands,
    _paint_overlay_strip,
    _render_overlay_frame,
    _resolve_chunk_size,
    _resolve_effective_worker_count,
    _resolve_pending_memory_budget,
    _resolve_pending_window,
    _resolve_stall_timeout_s,
    _resolve_worker_count,
    _strip_safety_margin,
    _write_frames_multiprocess,
    _write_frames_multiprocess_bands,
    _write_frames_single,
    _write_frames_single_bands,
    build_render_command,
    render_subtitle_video,
)
from krok_helper.subtitle_render.domain.models import (  # noqa: E402
    BackgroundSource,
    GuideSymbol,
    LineAnimationOverride,
    Style,
    SubtitleStyleScheme,
    TimingChar,
    TimingLine,
    TimingTrack,
    TitleOverlay,
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


def test_frame_analysis_aggregates_main_title_and_extra_bounds() -> None:
    main = TimingTrack()
    extra = TimingTrack()
    style = Style(title_overlays=[TitleOverlay(enabled=True)])
    durations: list[int | None] = []

    def resolve_visible_content(track, _t_ms, resolved_style, **kwargs):
        durations.append(kwargs.get("duration_ms"))
        top = 10 if track is main else 40
        # 第 5 项是 (overlay, opacity) 标题条目序列（多标题模型）
        title_states = [(resolved_style.title_overlays[0], 0.5)]
        return 0, resolved_style, [top], [], title_states

    def subtitle_bounds(
        _logical_w,
        _logical_h,
        _track,
        _track_t_ms,
        _style,
        display_lines,
        _signal_lines,
    ):
        top = display_lines[0]
        return (-5, 30) if top == 10 else (40, 60)

    ports = FrameAnalysisPorts(
        resolve_visible_content=resolve_visible_content,
        subtitle_vertical_bounds=subtitle_bounds,
        title_vertical_bounds=lambda *_args: (80, 120),
    )

    assert analyze_frame_has_content(
        main,
        500,
        style,
        [extra],
        duration_ms=2000,
        logical_w=160,
        logical_h=100,
        ports=ports,
    )
    durations.clear()
    intervals = analyze_frame_content_intervals(
        160,
        100,
        main,
        500,
        style,
        [extra],
        duration_ms=2000,
        ports=ports,
    )
    assert intervals == [(0, 30), (80, 99), (40, 60)]
    assert durations == [2000, None]
    assert analyze_frame_vertical_bounds(
        160,
        100,
        main,
        500,
        style,
        [extra],
        duration_ms=2000,
        ports=ports,
    ) == (0, 99)


def _static_style(**changes) -> Style:
    """条带 / 几何用例的基准样式：动画默认值不该左右这些断言。

    出厂默认带入场淡入与 utopia 唱字动画，安全边必须覆盖动画行程，条带因此从
    142px 涨到 358px（1080p）—— 校条带几何的用例先把动画钉死，测动画行程的用例
    自己显式打开。
    """

    return Style(
        entry_anim="none",
        exit_anim="none",
        karaoke_anim="inherit",
        sync_entry=False,
        sync_ending=False,
        sync_each_page=False,
        **changes,
    )


def _job(tmp_path: Path, *, include_audio: bool = True) -> RenderJob:
    background = tmp_path / "bg.mp4"
    background.write_bytes(b"not-real-video")
    return RenderJob(
        track=_track(),
        style=_static_style(font_size_px=24),
        background_video_path=background,
        output_path=tmp_path / "out.mp4",
        width=320,
        height=180,
        fps=60,
        duration_ms=1000,
        include_audio=include_audio,
        gpu_export_enabled=False,
    )


def test_renderer_keeps_render_job_compatibility_export() -> None:
    assert RenderJob is RenderJobContract
    assert renderer._job_tracks is job_tracks
    assert renderer._resolve_duration_ms is resolve_duration_ms
    assert renderer._resolved_background is resolved_background
    assert renderer._validate_job is validate_render_job
    assert renderer._merge_intervals is merge_intervals
    assert renderer._packed_offsets is packed_offsets
    assert renderer._background_input_args is background_input_args
    assert renderer._background_scale_chain is background_scale_chain
    assert renderer._bands_filter_graph is bands_filter_graph
    assert renderer._resolved_preview_width is resolved_preview_width
    assert build_render_command is build_render_command_contract
    schedule_names = (
        "_available_system_memory_bytes",
        "_resolve_chunk_size",
        "_resolve_effective_worker_count",
        "_resolve_pending_memory_budget",
        "_resolve_pending_window",
        "_resolve_stall_timeout_s",
        "_resolve_worker_count",
    )
    for name in schedule_names:
        assert getattr(renderer, name) is getattr(parallel_schedule, name)


def test_validate_render_job_rejects_output_path_that_is_a_directory(tmp_path):
    job = replace(_job(tmp_path), output_path=tmp_path)

    with pytest.raises(ProcessingError, match="同名文件夹"):
        validate_render_job(job)


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
    assert command[command.index("-c:v") + 1] == "libx264"
    assert command[command.index("-preset") + 1] == "medium"
    assert command[command.index("-crf") + 1] == "18"
    assert command.count("-r") == 2
    assert command[command.index("-fps_mode") + 1] == "cfr"


def test_build_render_command_can_skip_audio(tmp_path):
    command = build_render_command("ffmpeg", _job(tmp_path, include_audio=False))
    assert "1:a:0?" not in command
    assert "-c:a" not in command


def test_build_render_command_uses_independent_audio(tmp_path):
    audio = tmp_path / "song.wav"
    audio.write_bytes(b"fake")
    job = replace(
        _job(tmp_path),
        background_video_path=None,
        background_source=BackgroundSource(kind="solid", color="#000000"),
        audio_path=audio,
    )

    command = build_render_command("ffmpeg", job)

    assert command[command.index(str(audio)) - 1] == "-i"
    assert "2:a:0?" in command
    assert "1:a:0?" not in command


def test_render_job_rejects_independent_audio_with_video_background(tmp_path):
    audio = tmp_path / "song.wav"
    audio.write_bytes(b"fake")
    job = replace(_job(tmp_path), audio_path=audio)

    with pytest.raises(ProcessingError, match="视频背景不支持独立音频"):
        build_render_command("ffmpeg", job)


def test_build_render_command_rejects_odd_dimensions(tmp_path):
    """H.264/H.265 的 yuv420p 不支持奇数尺寸，必须在渲染前拦下。"""
    with pytest.raises(ProcessingError, match="偶数"):
        build_render_command("ffmpeg", replace(_job(tmp_path), width=321))
    with pytest.raises(ProcessingError, match="偶数"):
        build_render_command("ffmpeg", replace(_job(tmp_path), height=181))


def _image_job(tmp_path: Path, image_fit: str) -> RenderJob:
    image = tmp_path / "bg.png"
    image.write_bytes(b"fake")
    return replace(
        _job(tmp_path, include_audio=False),
        background_video_path=None,
        background_source=BackgroundSource(
            kind="image", path=str(image), image_fit=image_fit
        ),
    )


def _filter_graph(command: list[str]) -> str:
    return command[command.index("-filter_complex") + 1]


def test_background_filter_video_is_always_contain(tmp_path):
    """视频背景固定等比缩放 + 黑边，与预览 KeepAspectRatio + 纯黑底一致。"""
    graph = _filter_graph(build_render_command("ffmpeg", _job(tmp_path, include_audio=False)))
    assert "force_original_aspect_ratio=decrease" in graph
    assert "pad=320:180" in graph


def test_background_filter_image_fit_cover_and_contain(tmp_path):
    cover = _filter_graph(build_render_command("ffmpeg", _image_job(tmp_path, "cover")))
    assert "force_original_aspect_ratio=increase" in cover
    assert "crop=320:180" in cover

    contain = _filter_graph(
        build_render_command("ffmpeg", _image_job(tmp_path, "contain"))
    )
    assert "force_original_aspect_ratio=decrease" in contain
    assert "pad=320:180" in contain


def test_build_render_command_supports_solid_background(tmp_path):
    job = replace(
        _job(tmp_path),
        background_video_path=None,
        background_source=BackgroundSource(kind="solid", color="#123456"),
        include_audio=False,
    )

    command = build_render_command("ffmpeg", job)

    assert "lavfi" in command
    assert any("color=c=#123456:s=320x180:r=60" in part for part in command)
    filter_graph = command[command.index("-filter_complex") + 1]
    assert "[1:v:0]scale=320:180" in filter_graph


def test_build_render_command_supports_static_image_and_audio(tmp_path):
    image = tmp_path / "background.png"
    image.write_bytes(b"fake")
    audio = tmp_path / "song.flac"
    audio.write_bytes(b"fake")
    job = replace(
        _job(tmp_path),
        background_video_path=None,
        background_source=BackgroundSource(kind="image", path=str(image)),
        audio_path=audio,
    )

    command = build_render_command("ffmpeg", job)

    assert command[command.index(str(image)) - 5 : command.index(str(image)) + 1] == [
        "-loop", "1", "-framerate", "60", "-i", str(image)
    ]
    assert "2:a:0?" in command


def test_build_render_command_supports_image_sequence(tmp_path):
    first = tmp_path / "frame_%04d.png"
    job = replace(
        _job(tmp_path),
        background_video_path=None,
        background_source=BackgroundSource(
            kind="image_sequence", path=str(first), source_fps=24
        ),
        include_audio=False,
    )

    command = build_render_command("ffmpeg", job)

    assert command[command.index(str(first)) - 7 : command.index(str(first)) + 1] == [
        "-stream_loop", "-1", "-framerate", "24", "-start_number", "0", "-i", str(first)
    ]


def test_build_render_command_escapes_literal_percent_in_sequence_path(tmp_path):
    """目录名里的字面 % 会破坏 image2 的 snprintf 展开，只保留编号模板。"""

    sequence_dir = tmp_path / "100%Love"
    sequence_dir.mkdir()
    pattern = sequence_dir / "frame_%04d.png"
    job = replace(
        _job(tmp_path),
        background_video_path=None,
        background_source=BackgroundSource(
            kind="image_sequence", path=str(pattern), source_fps=24
        ),
        include_audio=False,
    )

    command = build_render_command("ffmpeg", job)

    input_index = command.index("-i", command.index("-start_number")) + 1
    assert command[input_index].endswith(os.path.join("100%%Love", "frame_%04d.png"))
    assert "%04d" in command[input_index]


def test_build_render_command_honors_cpu_quality_settings(tmp_path):
    job = replace(_job(tmp_path), crf=23, preset="slow")

    command = build_render_command("ffmpeg", job)

    assert command[command.index("-c:v") + 1] == "libx264"
    assert command[command.index("-preset") + 1] == "slow"
    assert command[command.index("-crf") + 1] == "23"


def test_build_render_command_honors_hevc_codec(tmp_path):
    job = replace(_job(tmp_path), codec="hevc")
    command = build_render_command("ffmpeg", job)
    assert command[command.index("-c:v") + 1] == "libx265"
    assert command[command.index("-tag:v") + 1] == "hvc1"


def test_build_render_command_rejects_bad_codec(tmp_path):
    with pytest.raises(ProcessingError, match="视频编码"):
        build_render_command("ffmpeg", replace(_job(tmp_path), codec="av1"))


def test_build_render_command_honors_nvenc_encoder(tmp_path):
    job = replace(_job(tmp_path), encoder_mode="nvenc", crf=20)

    command = build_render_command("ffmpeg", job)

    assert command[command.index("-c:v") + 1] == "h264_nvenc"
    assert command[command.index("-preset") + 1] == "p4"
    assert command[command.index("-cq") + 1] == "20"
    assert "-crf" not in command


def test_overlay_frame_size_matches_rgba(qapp, tmp_path):
    job = _job(tmp_path)
    raw = _render_overlay_frame(job.track, job.style, 500, job.width, job.height)
    assert len(raw) == job.width * job.height * 4


def test_build_render_command_strip_offsets_overlay_and_pipe_size(tmp_path):
    command = build_render_command("ffmpeg", _job(tmp_path), strip=(20, 40))
    filter_graph = command[command.index("-filter_complex") + 1]
    assert "overlay=0:20" in filter_graph
    assert "scale=320:180" in filter_graph  # 背景仍全幅
    assert command[command.index("-s:v") + 1] == "320x40"  # pipe 只喂窄条


def test_build_render_command_preview_taps_composited_stream(tmp_path):
    preview = tmp_path / "frame.jpg"
    command = build_render_command("ffmpeg", _job(tmp_path), preview_image_path=preview)

    filter_graph = command[command.index("-filter_complex") + 1]
    # 合成后的 [v] split 出编码路 + 降频缩宽的预览路
    assert "[v]split=2[venc][vpin]" in filter_graph
    assert "[vpin]fps=" in filter_graph and "[vprev]" in filter_graph
    assert command[command.index("-map") + 1] == "[venc]"
    # 预览输出：image2 持续覆盖 + 原子写，且排在主输出之后
    assert command[-1] == str(preview)
    assert command.index(str(_job(tmp_path).output_path)) < command.index("[vprev]")
    assert "-update" in command and "-atomic_writing" in command
    assert command[command.index("-q:v") + 1] == "2"


def test_build_render_command_preview_defaults_to_640_for_wide_output(tmp_path):
    preview = tmp_path / "frame.jpg"
    job = replace(_job(tmp_path), width=1920, height=1080)

    command = build_render_command("ffmpeg", job, preview_image_path=preview)

    filter_graph = command[command.index("-filter_complex") + 1]
    assert "[vpin]fps=2,scale=640:-2[vprev]" in filter_graph
    assert command[command.index("-s:v") + 1] == "1920x1080"


@pytest.mark.parametrize(
    ("requested_width", "expected_width"),
    [(900, 900), (100, 320), (4000, 1920)],
)
def test_build_render_command_preview_clamps_requested_width(
    tmp_path, requested_width, expected_width
):
    preview = tmp_path / "frame.jpg"
    job = replace(_job(tmp_path), width=1920, height=1080)

    command = build_render_command(
        "ffmpeg",
        job,
        preview_image_path=preview,
        preview_width=requested_width,
    )

    filter_graph = command[command.index("-filter_complex") + 1]
    assert f"[vpin]fps=2,scale={expected_width}:-2[vprev]" in filter_graph
    assert command[command.index("-s:v") + 1] == "1920x1080"


def test_build_render_command_without_preview_keeps_single_output(tmp_path):
    job = _job(tmp_path)
    command = build_render_command("ffmpeg", job)
    assert command[-1] == str(job.output_path)
    assert "split=2" not in command[command.index("-filter_complex") + 1]


def test_compute_subtitle_strip_returns_subband_for_centered_line(qapp, tmp_path):
    job = replace(_job(tmp_path), style=_static_style(font_size_px=24, line_y_position="center"))
    strip = _compute_subtitle_strip(job, 1000)
    assert strip is not None
    top, height = strip
    assert 0 <= top
    assert top + height <= job.height
    assert height < job.height  # 比全高矮
    assert top % 2 == 0 and height % 2 == 0  # yuv420p 友好


def test_compute_subtitle_strip_uses_layer_bounds_without_alpha_scan(qapp, tmp_path, monkeypatch):
    job = replace(_job(tmp_path), style=_static_style(font_size_px=24, line_y_position="center"))

    def fail_paint_frame(*_args, **_kwargs):
        raise AssertionError("layer-bound path should not paint a full scratch frame")

    monkeypatch.setattr(renderer, "paint_frame", fail_paint_frame)
    assert _compute_subtitle_strip(job, 1000) is not None


def test_compute_subtitle_strip_uses_signal_layer_bounds_without_alpha_scan(qapp, tmp_path, monkeypatch):
    style = _static_style(
        font_size_px=24,
        line_y_position="center",
        lit_enabled=True,
        lit_style="circle",
        lit_size=12,
        lit_stroke_width=0,
        lit_shadow=False,
        signals_duration_ms=500,
    )
    job = replace(_job(tmp_path), style=style)

    def fail_paint_frame(*_args, **_kwargs):
        raise AssertionError("signal layer-bound path should not paint a full scratch frame")

    monkeypatch.setattr(renderer, "paint_frame", fail_paint_frame)
    assert _compute_subtitle_strip(job, 1000) is not None


def test_compute_subtitle_strip_falls_back_when_content_fills_height(qapp, tmp_path):
    # 矮帧 + 大字：内容纵向并集 ≥ 85% 全高 → 退回整帧（None）。
    # 阴影偏移固定为小值：默认 (10,10) 会把该退化场景的 bounds 推到帧外，
    # 触发不了本用例要测的“占满高度回退”分支。
    job = replace(
        _job(tmp_path),
        style=Style(
            font_size_px=72,
            line_y_position="center",
            shadow_offset_x=0,
            shadow_offset_y=1,
        ),
        height=80,
    )
    assert _compute_subtitle_strip(job, 1000) is None


def test_strip_render_is_pixel_identical_to_full_frame_region(qapp, tmp_path):
    job = replace(_job(tmp_path), style=_static_style(font_size_px=24, line_y_position="center"))
    t_ms = 800
    strip = _compute_subtitle_strip(job, 1000)
    assert strip is not None
    top, height = strip

    full = QImage(job.width, job.height, QImage.Format.Format_RGBA8888)
    full.fill(QColor(0, 0, 0, 0))
    paint_frame(full, job.track, t_ms, job.style)

    buf = QImage(job.width, height, QImage.Format.Format_RGBA8888)
    _paint_overlay_strip(
        buf, job.track, job.style, t_ms,
        logical_w=job.width, logical_h=job.height,
        strip_top=top, transparent=QColor(0, 0, 0, 0),
    )

    full_arr = np.frombuffer(_image_bytes(full), dtype=np.uint8).reshape(job.height, job.width * 4)
    buf_arr = np.frombuffer(_image_bytes(buf), dtype=np.uint8).reshape(height, job.width * 4)
    # 条带就是整帧 [top, top+height) 行的精确切片
    assert np.array_equal(full_arr[top : top + height], buf_arr)


def test_frame_count_ceil():
    assert _frame_count(1000, 60) == 60
    assert _frame_count(1001, 60) == 61


def test_resolve_worker_count_respects_env_and_min_frames(monkeypatch):
    monkeypatch.setenv("KROK_SUBTITLE_RENDER_WORKERS", "4")
    assert _resolve_worker_count(10_000) == 4  # 帧数够多 → 用指定数
    assert _resolve_worker_count(10) == 1       # 帧数太少 → 退回单进程
    monkeypatch.setenv("KROK_SUBTITLE_RENDER_WORKERS", "1")
    assert _resolve_worker_count(10_000) == 1   # 显式 1 = 关闭


def test_resolve_worker_count_keeps_auto_cap_and_allows_manual_16(monkeypatch):
    monkeypatch.delenv("KROK_SUBTITLE_RENDER_WORKERS", raising=False)
    monkeypatch.setattr(renderer.os, "cpu_count", lambda: 32)

    assert _resolve_worker_count(10_000) == 8
    assert _resolve_worker_count(10_000, 12) == 12
    assert _resolve_worker_count(10_000, 16) == 16
    assert _resolve_worker_count(10_000, 32) == 16
    assert _resolve_worker_count(10, 16) == 1


def test_resolve_chunk_size_is_positive_and_balanced(tmp_path):
    job = replace(_job(tmp_path), width=1920, height=1080)
    chunk = _resolve_chunk_size(job, 1080, total_frames=10_000, worker_count=4)
    assert chunk >= 1
    # 每 worker 至少几块以均衡（不会一块独吞）
    assert chunk <= 10_000 // 4


def test_resolve_chunk_size_scales_target_with_worker_count(tmp_path):
    # 固定 64MiB chunk + 256MiB 窗口只能容纳 ~4 块，8 worker 会饿死一半；
    # chunk 目标随 worker 缩放后，窗口 ≥ worker 数，喂满全部 worker。
    job_1080p = replace(_job(tmp_path), width=1920, height=1080)
    # 8 worker：目标 256MiB/10 = 25.6MiB → 1080p 全幅（~7.9MiB/帧）3 帧/块
    assert _resolve_chunk_size(job_1080p, 1080, total_frames=10_000, worker_count=8) == 3
    # 2 worker：目标仍为 64MiB 上限 → 8 帧/块
    assert _resolve_chunk_size(job_1080p, 1080, total_frames=10_000, worker_count=2) == 8
    # 4K 全幅单帧 ~31.6MiB > 25.6MiB 目标 → 1 帧/块（帧不可再分）
    job_4k = replace(_job(tmp_path), width=3840, height=2160)
    assert _resolve_chunk_size(job_4k, 2160, total_frames=10_000, worker_count=8) == 1


class _CollectStdin:
    def __init__(self):
        self.data = bytearray()

    def write(self, payload):
        self.data += payload

    def close(self):
        return None


class _CollectProcess:
    def __init__(self):
        self.stdin = _CollectStdin()


def test_multiprocess_output_is_byte_identical_to_single_process(qapp, tmp_path):
    # 多进程并行渲染的拼接输出必须与单进程逐帧逐字节一致（含 worker 间字体一致性）。
    job = replace(
        _job(tmp_path),
        style=_static_style(font_size_px=24, line_y_position="center"),
        width=160,
        height=90,
        duration_ms=1000,
    )
    total = _frame_count(job.duration_ms, job.fps)
    strip = _compute_subtitle_strip(job, job.duration_ms)
    strip_top, render_h = strip if strip is not None else (0, job.height)

    single = _CollectProcess()
    _write_frames_single(single, job, strip_top, render_h, total, None, None)

    multi = _CollectProcess()
    _write_frames_multiprocess(multi, job, strip_top, render_h, total, 2, None, None)

    assert len(single.stdin.data) == total * job.width * render_h * 4
    assert bytes(multi.stdin.data) == bytes(single.stdin.data)


def test_render_job_validation_requires_subtitles(tmp_path):
    job = RenderJob(
        track=TimingTrack(),
        style=Style(),
        background_video_path=tmp_path / "bg.mp4",
        output_path=tmp_path / "out.mp4",
    )
    with pytest.raises(ProcessingError):
        build_render_command("ffmpeg", job)


def test_render_job_validation_rejects_bad_encoder_settings(tmp_path):
    with pytest.raises(ProcessingError, match="CRF"):
        build_render_command("ffmpeg", replace(_job(tmp_path), crf=99))
    with pytest.raises(ProcessingError, match="编码器"):
        build_render_command("ffmpeg", replace(_job(tmp_path), encoder_mode="bad"))
    with pytest.raises(ProcessingError, match="preset"):
        build_render_command("ffmpeg", replace(_job(tmp_path), preset="turbo"))


def test_render_cancel_removes_incomplete_output(monkeypatch, tmp_path):
    job = _job(tmp_path)
    job.output_path.write_bytes(b"partial")

    class FakeStdin:
        def write(self, _data):
            return None

        def close(self):
            return None

    class FakeProcess:
        def __init__(self):
            self.stdin = FakeStdin()
            self.stdout = []
            self.returncode = None
            self.terminated = False

        def poll(self):
            return self.returncode

        def terminate(self):
            self.terminated = True
            self.returncode = -15

        def kill(self):
            self.returncode = -9

        def wait(self, timeout=None):
            if self.returncode is None:
                self.returncode = 0
            return self.returncode

    fake_process = FakeProcess()
    monkeypatch.setattr(renderer, "find_tool", lambda _name, _ffmpeg_dir=None: "ffmpeg")
    monkeypatch.setattr(renderer.subprocess, "Popen", lambda *args, **kwargs: fake_process)

    with pytest.raises(ExportCancelled):
        render_subtitle_video(job, should_cancel=lambda: True)

    assert fake_process.terminated is True
    assert not job.output_path.exists()


def test_render_rejects_output_that_is_the_background_video(tmp_path):
    """导出名和背景视频同名时必须提前报错，且绝不能删掉那个源文件。"""

    background = tmp_path / "song.mp4"
    background.write_bytes(b"source-video-bytes")
    job = replace(_job(tmp_path), background_video_path=background, output_path=background)

    with pytest.raises(ProcessingError, match="不能覆盖素材本身"):
        render_subtitle_video(job)

    assert background.read_bytes() == b"source-video-bytes"


def test_render_rejects_output_that_is_the_separate_audio(tmp_path):
    audio = tmp_path / "vocal.wav"
    audio.write_bytes(b"source-audio-bytes")
    job = replace(
        _job(tmp_path),
        background_source=BackgroundSource(kind="solid", color="#000000"),
        background_video_path=None,
        audio_path=audio,
        output_path=audio,
    )

    with pytest.raises(ProcessingError, match="不能覆盖素材本身"):
        render_subtitle_video(job)

    assert audio.read_bytes() == b"source-audio-bytes"


def test_output_input_check_ignores_filename_case(tmp_path):
    """Windows 上大小写不同仍是同一个文件，别放过去。"""

    background = tmp_path / "song.mp4"
    background.write_bytes(b"source-video-bytes")
    job = replace(
        _job(tmp_path),
        background_video_path=background,
        output_path=tmp_path / "SONG.MP4",
    )

    if os.path.normcase("A") == "A":  # 大小写敏感的文件系统上这条不成立
        pytest.skip("case-sensitive filesystem")
    with pytest.raises(ProcessingError, match="不能覆盖素材本身"):
        render_subtitle_video(job)


def test_incomplete_output_cleanup_never_deletes_a_source_file(tmp_path):
    """最后一道闸：清理半成品时撞上素材路径要跳过，不是照删不误。"""

    background = tmp_path / "song.mp4"
    background.write_bytes(b"source-video-bytes")
    job = replace(_job(tmp_path), background_video_path=background, output_path=background)
    logs: list[str] = []

    renderer._remove_incomplete_output(job, logs.append)

    assert background.read_bytes() == b"source-video-bytes"
    assert any("跳过清理" in line for line in logs)


def test_incomplete_output_cleanup_still_removes_a_real_half_written_file(tmp_path):
    job = _job(tmp_path)
    job.output_path.write_bytes(b"partial")
    logs: list[str] = []

    renderer._remove_incomplete_output(job, logs.append)

    assert not job.output_path.exists()
    assert any("已清理未完成的输出文件" in line for line in logs)


# ---------------------------------------------------------------------------
# A2 方案 B：多条分离带
# ---------------------------------------------------------------------------


class _FakeRenderStdin:
    def __init__(self):
        self.data = bytearray()
        self.closed = False

    def write(self, payload):
        self.data += payload

    def close(self):
        self.closed = True


class _FakeRenderProcess:
    def __init__(self, output_path: Path):
        self.output_path = output_path
        self.stdin = _FakeRenderStdin()
        self.stdout = []
        self.returncode = None
        self.terminated = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.returncode = -9

    def wait(self, timeout=None):
        if self.returncode is None:
            self.output_path.write_bytes(b"ok")
            self.returncode = 0
        return self.returncode


def test_render_ignores_native_enable_and_uses_python(monkeypatch, tmp_path):
    job = replace(
        _job(tmp_path),
        width=2,
        height=2,
        fps=2,
        duration_ms=1000,
        native_export_enabled=True,
    )
    native_path = tmp_path / "krok_subtitle_renderer.exe"
    writes = []
    progress = []

    def fail_native(*_args, **_kwargs):
        raise AssertionError("hard-disabled native export must not be called")

    def fake_write_frames_single(process, _job, strip_top, render_h, total_frames, should_cancel, on_progress):
        writes.append((strip_top, render_h, total_frames))
        process.stdin.write(b"p" * (_job.width * render_h * 4 * total_frames))
        if on_progress is not None:
            on_progress(total_frames, total_frames)

    fake_process = _FakeRenderProcess(job.output_path)
    monkeypatch.setenv("KROK_SUBTITLE_NATIVE_EXPORT", "1")
    monkeypatch.setenv("KROK_SUBTITLE_RENDER_STRIP", "0")
    monkeypatch.setattr(renderer, "find_tool", lambda _name, _ffmpeg_dir=None: "ffmpeg")
    monkeypatch.setattr(renderer, "resolve_native_renderer_path", lambda: native_path)
    monkeypatch.setattr(renderer, "iter_native_rgba_frames", fail_native)
    monkeypatch.setattr(renderer, "_write_frames_single", fake_write_frames_single)
    monkeypatch.setattr(renderer.subprocess, "Popen", lambda *args, **kwargs: fake_process)

    assert render_subtitle_video(job, on_progress=lambda done, total: progress.append((done, total))) == job.output_path

    assert writes == [(0, 2, 2)]
    assert bytes(fake_process.stdin.data) == b"p" * 32
    assert progress == [(2, 2)]


def test_render_drains_ffmpeg_output_while_writing_frames(monkeypatch, tmp_path):
    job = replace(
        _job(tmp_path),
        width=2,
        height=2,
        fps=2,
        duration_ms=1000,
    )
    fake_process = _FakeRenderProcess(job.output_path)
    drain_started = threading.Event()
    allow_drain_to_finish = threading.Event()

    def fake_drain(_process, _logger, _output_tail=None):
        drain_started.set()
        assert allow_drain_to_finish.wait(timeout=1.0)

    def fake_write_frames_single(
        process,
        _job,
        _strip_top,
        render_h,
        total_frames,
        _should_cancel,
        _on_progress,
    ):
        assert drain_started.wait(timeout=1.0)
        process.stdin.write(b"p" * (_job.width * render_h * 4 * total_frames))
        allow_drain_to_finish.set()

    monkeypatch.setenv("KROK_SUBTITLE_RENDER_STRIP", "0")
    monkeypatch.setattr(renderer, "find_tool", lambda _name, _ffmpeg_dir=None: "ffmpeg")
    monkeypatch.setattr(renderer, "_drain_process_output", fake_drain)
    monkeypatch.setattr(renderer, "_write_frames_single", fake_write_frames_single)
    monkeypatch.setattr(renderer.subprocess, "Popen", lambda *args, **kwargs: fake_process)

    assert render_subtitle_video(job) == job.output_path
    assert not fake_process.terminated


def test_render_job_native_export_flag_and_environment_are_ignored(monkeypatch, tmp_path):
    monkeypatch.setenv("KROK_SUBTITLE_NATIVE_EXPORT", "1")

    assert renderer._native_export_requested(replace(_job(tmp_path), native_export_enabled=False)) is False
    assert renderer._native_export_requested(replace(_job(tmp_path), native_export_enabled=True)) is False

    monkeypatch.setenv("KROK_SUBTITLE_NATIVE_EXPORT", "0")

    assert renderer._native_export_requested(replace(_job(tmp_path), native_export_enabled=None)) is False


def test_gpu_export_request_defaults_to_gpu_on_windows(monkeypatch, tmp_path):
    monkeypatch.delenv("KROK_SUBTITLE_GPU_EXPORT", raising=False)

    assert renderer._gpu_export_requested(
        replace(_job(tmp_path), gpu_export_enabled=None)
    ) is (os.name == "nt")
    assert renderer._gpu_export_requested(
        replace(_job(tmp_path), gpu_export_enabled=True)
    ) is True
    assert renderer._gpu_export_requested(
        replace(_job(tmp_path), gpu_export_enabled=False)
    ) is False

    monkeypatch.setenv("KROK_SUBTITLE_GPU_EXPORT", "1")
    assert renderer._gpu_export_requested(
        replace(_job(tmp_path), gpu_export_enabled=None)
    ) is True
    monkeypatch.setenv("KROK_SUBTITLE_GPU_EXPORT", "0")
    assert renderer._gpu_export_requested(
        replace(_job(tmp_path), gpu_export_enabled=None)
    ) is False


def test_gpu_export_worker_count_is_bounded_and_warp_stays_serial(monkeypatch):
    monkeypatch.delenv("KROK_SUBTITLE_GPU_EXPORT_WORKERS", raising=False)
    monkeypatch.setattr(renderer.os, "cpu_count", lambda: 32)
    assert renderer._gpu_export_worker_count(force_warp=False) == 4

    monkeypatch.setattr(renderer.os, "cpu_count", lambda: 12)
    assert renderer._gpu_export_worker_count(force_warp=False) == 4

    monkeypatch.setenv("KROK_SUBTITLE_GPU_EXPORT_WORKERS", "2")
    assert renderer._gpu_export_worker_count(force_warp=False) == 2

    monkeypatch.setenv("KROK_SUBTITLE_GPU_EXPORT_WORKERS", "8")
    assert renderer._gpu_export_worker_count(force_warp=False) == 4
    assert renderer._gpu_export_worker_count(force_warp=True) == 1

    monkeypatch.setenv("KROK_SUBTITLE_GPU_EXPORT_WORKERS", "invalid")
    assert renderer._gpu_export_worker_count(force_warp=False) == 4


def test_gpu_export_diagnostics_flag(monkeypatch):
    monkeypatch.delenv("KROK_SUBTITLE_GPU_EXPORT_DIAGNOSTICS", raising=False)
    assert renderer._gpu_export_diagnostics_enabled() is False
    monkeypatch.setenv("KROK_SUBTITLE_GPU_EXPORT_DIAGNOSTICS", "true")
    assert renderer._gpu_export_diagnostics_enabled() is True


def test_persist_gpu_export_diagnostics_writes_summary_and_frames(
    monkeypatch, tmp_path
):
    output_dir = tmp_path / "diagnostics"
    monkeypatch.setenv(
        "KROK_SUBTITLE_GPU_EXPORT_DIAGNOSTICS_DIR", str(output_dir)
    )
    messages = []

    renderer._persist_gpu_export_diagnostics(
        _job(tmp_path),
        export_run_id="run-1",
        total_wall_ms=42.0,
        worker_count=2,
        force_warp=False,
        gpu_diagnostics={"adapter": "test gpu"},
        frame_diagnostics=[
            {"frame_index": 0, "native_render_ms": 2.0, "stdin_block_ms": 3.0},
            {"frame_index": 1, "native_render_ms": 4.0, "stdin_block_ms": 5.0},
        ],
        crop=(10, 20),
        bands=None,
        logger=messages.append,
    )

    summary = json.loads((output_dir / "run-1-summary.json").read_text("utf-8"))
    assert summary["export_run_id"] == "run-1"
    assert summary["frames"] == 2
    assert summary["crop"] == [10, 20]
    assert summary["aggregates"]["native_render_ms"]["mean"] == 3.0
    assert (output_dir / "run-1-frames.csv").is_file()
    assert messages and "run-1-summary.json" in messages[-1]


def test_render_uses_gpu_subtitle_export_without_changing_encoder(monkeypatch, tmp_path):
    job = replace(
        _job(tmp_path),
        width=2,
        height=2,
        fps=2,
        duration_ms=1_000,
        gpu_export_enabled=True,
        encoder_mode="cpu",
    )
    gpu_path = tmp_path / "krok_subtitle_renderer.exe"
    gpu_path.write_bytes(b"exe")
    writes = []
    fake_process = _FakeRenderProcess(job.output_path)

    def fake_gpu(
        process,
        active_job,
        total_frames,
        path,
        should_cancel,
        on_progress,
        logger,
        crop=None,
        bands=None,
    ):
        writes.append((active_job.encoder_mode, total_frames, path, crop, bands))
        process.stdin.write(b"g" * (active_job.width * active_job.height * 4 * total_frames))

    monkeypatch.setenv("KROK_SUBTITLE_RENDER_STRIP", "1")
    monkeypatch.setenv("KROK_SUBTITLE_GPU_EXPORT_PACKED", "1")
    monkeypatch.setattr(
        renderer, "_compute_subtitle_strip", lambda *_args, **_kwargs: (0, 1)
    )
    monkeypatch.setattr(renderer, "find_tool", lambda _name, _ffmpeg_dir=None: "ffmpeg")
    monkeypatch.setattr(renderer, "resolve_native_renderer_path", lambda: gpu_path)
    monkeypatch.setattr(renderer, "_write_frames_gpu", fake_gpu)
    monkeypatch.setattr(
        renderer,
        "_write_frames_single",
        lambda *_args, **_kwargs: pytest.fail("Painter writer must not run"),
    )
    monkeypatch.setattr(renderer.subprocess, "Popen", lambda *args, **kwargs: fake_process)

    assert render_subtitle_video(job) == job.output_path
    assert writes == [("cpu", 2, gpu_path, (0, 1), None)]
    assert bytes(fake_process.stdin.data) == b"g" * 32


def test_gpu_export_runtime_failure_restarts_with_painter(monkeypatch, tmp_path):
    job = replace(
        _job(tmp_path),
        width=2,
        height=2,
        fps=2,
        duration_ms=1_000,
        gpu_export_enabled=True,
    )
    gpu_path = tmp_path / "krok_subtitle_renderer.exe"
    gpu_path.write_bytes(b"exe")
    processes = [_FakeRenderProcess(job.output_path), _FakeRenderProcess(job.output_path)]
    logs = []
    painter_writes = []

    def fail_gpu(*_args, **_kwargs):
        raise renderer.NativeRendererError("device removed")

    def fake_painter(process, active_job, strip_top, render_h, total_frames, should_cancel, on_progress):
        painter_writes.append((active_job.gpu_export_enabled, strip_top, render_h))
        process.stdin.write(b"p" * (active_job.width * render_h * 4 * total_frames))

    monkeypatch.setenv("KROK_SUBTITLE_RENDER_STRIP", "0")
    monkeypatch.setattr(renderer, "find_tool", lambda _name, _ffmpeg_dir=None: "ffmpeg")
    monkeypatch.setattr(renderer, "resolve_native_renderer_path", lambda: gpu_path)
    monkeypatch.setattr(renderer, "_write_frames_gpu", fail_gpu)
    monkeypatch.setattr(renderer, "_write_frames_single", fake_painter)
    monkeypatch.setattr(
        renderer.subprocess,
        "Popen",
        lambda *args, **kwargs: processes.pop(0),
    )

    assert render_subtitle_video(job, logger=logs.append) == job.output_path
    assert painter_writes == [(False, 0, 2)]
    assert any("已自动回退到 CPU Painter" in message for message in logs)
    assert any("进度会重新从 0 开始计数" in message for message in logs)


def test_render_retries_amf_initialization_failure_with_cpu(monkeypatch, tmp_path):
    job = replace(
        _job(tmp_path),
        width=2,
        height=2,
        fps=2,
        duration_ms=1_000,
        gpu_export_enabled=False,
        encoder_mode="amf",
    )
    failed = _FakeRenderProcess(job.output_path)
    failed.returncode = 1
    failed.stdout = [
        b"[h264_amf] CreateComponent failed with error AMF_OUT_OF_MEMORY\n"
    ]
    succeeded = _FakeRenderProcess(job.output_path)
    processes = [failed, succeeded]
    commands: list[list[str]] = []
    logs: list[str] = []

    def fake_popen(command, **_kwargs):
        commands.append(command)
        return processes.pop(0)

    def fake_writer(process, active_job, _top, render_h, total, *_args):
        process.stdin.write(b"p" * (active_job.width * render_h * 4 * total))

    monkeypatch.setenv("KROK_SUBTITLE_RENDER_STRIP", "0")
    monkeypatch.setattr(renderer, "find_tool", lambda *_args, **_kwargs: "ffmpeg")
    monkeypatch.setattr(renderer, "_write_frames_single", fake_writer)
    monkeypatch.setattr(renderer.subprocess, "Popen", fake_popen)

    assert render_subtitle_video(job, logger=logs.append) == job.output_path
    assert "h264_amf" in commands[0]
    assert "libx264" in commands[1]
    assert any("已自动切换 CPU 编码" in message for message in logs)
    assert any("进度会重新从 0 开始" in message for message in logs)


def test_render_retries_amf_broken_pipe_with_cpu(monkeypatch, tmp_path):
    class _BrokenAmfStdin(_FakeRenderStdin):
        def write(self, _payload):
            raise BrokenPipeError("AMF encoder exited during initialization")

    job = replace(
        _job(tmp_path),
        width=2,
        height=2,
        fps=2,
        duration_ms=1_000,
        gpu_export_enabled=False,
        encoder_mode="amf",
    )
    failed = _FakeRenderProcess(job.output_path)
    failed.stdin = _BrokenAmfStdin()
    failed.returncode = 1
    failed.stdout = [b"[h264_amf] Error while opening encoder\n"]
    succeeded = _FakeRenderProcess(job.output_path)
    processes = [failed, succeeded]
    commands: list[list[str]] = []

    def fake_popen(command, **_kwargs):
        commands.append(command)
        return processes.pop(0)

    def fake_writer(process, active_job, _top, render_h, total, *_args):
        process.stdin.write(b"p" * (active_job.width * render_h * 4 * total))

    monkeypatch.setenv("KROK_SUBTITLE_RENDER_STRIP", "0")
    monkeypatch.setattr(renderer, "find_tool", lambda *_args, **_kwargs: "ffmpeg")
    monkeypatch.setattr(renderer, "_write_frames_single", fake_writer)
    monkeypatch.setattr(renderer.subprocess, "Popen", fake_popen)

    assert render_subtitle_video(job) == job.output_path
    assert "h264_amf" in commands[0]
    assert "libx264" in commands[1]


def test_amf_retry_filter_rejects_unrelated_ffmpeg_failure():
    command = ["ffmpeg", "-c:v", "h264_amf", "out.mp4"]
    assert renderer._should_retry_amf_with_cpu(
        command, deque(["No space left on device"])
    ) is False
    assert renderer._should_retry_amf_with_cpu(
        command, deque(["Error while opening encoder"])
    ) is True
    assert renderer._should_retry_amf_with_cpu(
        ["ffmpeg", "-c:v", "libx264", "out.mp4"],
        deque(["Error while opening encoder"]),
    ) is False


def test_ffmpeg_failure_hint_translates_common_markers():
    assert "磁盘空间不足" in renderer._ffmpeg_failure_hint(
        deque(["av_interleaved_write_fd: No space left on device"])
    )
    assert "占用" in renderer._ffmpeg_failure_hint(
        deque(["out.mp4: Permission denied"])
    )
    assert "素材" in renderer._ffmpeg_failure_hint(
        deque(["bg.mp4: No such file or directory"])
    )
    assert renderer._ffmpeg_failure_hint(deque(["some unrelated warning"])) == ""
    assert renderer._ffmpeg_failure_hint(deque([])) == ""


def test_render_failure_message_includes_translated_hint(monkeypatch, tmp_path):
    job = replace(
        _job(tmp_path), width=2, height=2, fps=2, duration_ms=1_000
    )
    failed = _FakeRenderProcess(job.output_path)
    failed.returncode = 1
    failed.stdout = [b"out.mp4: Permission denied\n"]

    def fake_writer(process, active_job, _top, render_h, total, *_args):
        process.stdin.write(b"p" * (active_job.width * render_h * 4 * total))

    monkeypatch.setenv("KROK_SUBTITLE_RENDER_STRIP", "0")
    monkeypatch.setattr(renderer, "find_tool", lambda *_args, **_kwargs: "ffmpeg")
    monkeypatch.setattr(renderer, "_write_frames_single", fake_writer)
    monkeypatch.setattr(
        renderer.subprocess, "Popen", lambda *args, **kwargs: failed
    )

    with pytest.raises(ProcessingError) as excinfo:
        render_subtitle_video(job)

    assert "退出码: 1" in str(excinfo.value)
    assert "占用" in str(excinfo.value)


def test_render_broken_pipe_message_includes_translated_hint(monkeypatch, tmp_path):
    class _BrokenStdin(_FakeRenderStdin):
        def write(self, _payload):
            raise BrokenPipeError("ffmpeg exited while consuming rawvideo")

    job = replace(
        _job(tmp_path), width=2, height=2, fps=2, duration_ms=1_000
    )
    failed = _FakeRenderProcess(job.output_path)
    failed.stdin = _BrokenStdin()
    failed.returncode = 1
    failed.stdout = [b"av_interleaved_write_fd: No space left on device\n"]

    def fake_writer(process, active_job, _top, render_h, total, *_args):
        process.stdin.write(b"p" * (active_job.width * render_h * 4 * total))

    monkeypatch.setenv("KROK_SUBTITLE_RENDER_STRIP", "0")
    monkeypatch.setattr(renderer, "find_tool", lambda *_args, **_kwargs: "ffmpeg")
    monkeypatch.setattr(renderer, "_write_frames_single", fake_writer)
    monkeypatch.setattr(
        renderer.subprocess, "Popen", lambda *args, **kwargs: failed
    )

    with pytest.raises(ProcessingError) as excinfo:
        render_subtitle_video(job)

    assert "管道写入失败" in str(excinfo.value)
    assert "磁盘空间不足" in str(excinfo.value)


def test_render_falls_back_to_python_when_native_export_sidecar_missing(monkeypatch, tmp_path):
    job = replace(_job(tmp_path), width=2, height=2, fps=2, duration_ms=1000)
    fake_process = _FakeRenderProcess(job.output_path)
    writes = []

    def fake_write_frames_single(process, _job, strip_top, render_h, total_frames, should_cancel, on_progress):
        writes.append((strip_top, render_h, total_frames))
        process.stdin.write(b"p" * (_job.width * render_h * 4 * total_frames))
        if on_progress is not None:
            on_progress(total_frames, total_frames)

    monkeypatch.setenv("KROK_SUBTITLE_NATIVE_EXPORT", "1")
    monkeypatch.setenv("KROK_SUBTITLE_RENDER_STRIP", "0")
    monkeypatch.setattr(renderer, "find_tool", lambda _name, _ffmpeg_dir=None: "ffmpeg")
    monkeypatch.setattr(renderer, "resolve_native_renderer_path", lambda: None)
    monkeypatch.setattr(renderer, "_write_frames_single", fake_write_frames_single)
    monkeypatch.setattr(renderer.subprocess, "Popen", lambda *args, **kwargs: fake_process)

    render_subtitle_video(job)

    assert writes == [(0, 2, 2)]
    assert bytes(fake_process.stdin.data) == b"p" * 32


def _band_job(tmp_path: Path) -> RenderJob:
    """顶部标题 + 底部歌词 的两块分离场景（中间大片空白 → 适合方案 B）。"""
    background = tmp_path / "bg.mp4"
    background.write_bytes(b"not-real-video")
    style = Style(
        font_size_px=48,
        line_y_position="bottom",
        title_overlays=[TitleOverlay(
            enabled=True,
            text_template="标题",
            anchor="top_center",
            font_size_px=48,
            offset_y=20,
            show_mode="whole",
        )],
    )
    return RenderJob(
        track=_track(),
        style=style,
        background_video_path=background,
        output_path=tmp_path / "out.mp4",
        width=320,
        height=720,
        fps=60,
        duration_ms=1000,
    )


def _img_rows(image: QImage) -> np.ndarray:
    """QImage(RGBA8888) → (height, width*4) uint8 视图（按 bytesPerLine 切齐）。"""
    h = image.height()
    w = image.width()
    bpl = image.bytesPerLine()
    ptr = image.constBits()
    ptr.setsize(image.sizeInBytes())
    arr = np.frombuffer(ptr, dtype=np.uint8, count=bpl * h).reshape(h, bpl)
    return arr[:, : w * 4].copy()


def test_merge_intervals_groups_by_gap():
    assert _merge_intervals([(0, 10), (12, 20), (200, 210)], 8) == [(0, 20), (200, 210)]
    assert _merge_intervals([(0, 10), (200, 210)], 8) == [(0, 10), (200, 210)]
    assert _merge_intervals([], 8) == []


def test_build_render_command_bands_packs_split_crop_overlay(tmp_path):
    job = _band_job(tmp_path)
    command = build_render_command("ffmpeg", job, bands=[(0, 40), (600, 60)])
    # 打包 pipe 高 = 各 band 高之和。
    assert f"{job.width}x100" in command
    fg = command[command.index("-filter_complex") + 1]
    assert "split=2[p0][p1]" in fg
    assert f"crop={job.width}:40:0:0[c0]" in fg
    assert f"crop={job.width}:60:0:40[c1]" in fg  # 第二条打包偏移 = 第一条高 40
    assert "[bg][c0]overlay=0:0" in fg
    assert "[c1]overlay=0:600" in fg
    assert fg.rstrip().endswith("[v]")


def test_compute_content_bands_splits_title_and_lyrics(qapp, tmp_path):
    bands = _compute_content_bands(_band_job(tmp_path), 1000)
    assert bands is not None
    assert len(bands) >= 2
    # 第一条在顶部（标题），最后一条在底部（歌词），中间有明显空白。
    tops = [top for top, _h in bands]
    assert tops == sorted(tops)
    first_top, first_h = bands[0]
    last_top, _last_h = bands[-1]
    assert first_top < 200
    # 380：歌词条带顶随阴影剪影 pad（描边半宽 + 偏移）略上移。
    assert last_top > 380
    assert last_top - (first_top + first_h) > renderer._BAND_MERGE_GAP_PX


def test_title_overlay_bake_uses_target_device_pixel_ratio(qapp):
    subtitle_painter.clear_before_layer_cache()
    image = QImage(640, 360, QImage.Format.Format_ARGB32_Premultiplied)
    image.setDevicePixelRatio(2.0)
    image.fill(QColor(0, 0, 0, 0))
    style = Style(
        title_overlays=[TitleOverlay(
            enabled=True,
            text_template="Title",
            font_size_px=48,
            show_mode="whole",
        )])

    paint_frame(image, _track(), 200, style)

    title_layers = [
        baked
        for key, baked in subtitle_painter._TEXT_RUN_LAYER_CACHE._items.items()
        if key[0] is TitleOverlayLayer
    ]
    assert title_layers
    assert title_layers[-1].image.devicePixelRatioF() == 2.0


def test_packed_offsets_are_cumulative_heights():
    assert _packed_offsets([(0, 40), (600, 60), (700, 10)]) == [0, 40, 100]


def test_bands_render_is_pixel_identical_to_full_frame_regions(qapp, tmp_path):
    job = _band_job(tmp_path)
    bands = _compute_content_bands(job, 1000)
    assert bands is not None and len(bands) >= 2
    packed_h = sum(h for _t, h in bands)

    t_ms = 600  # 歌词与标题同时可见
    full = QImage(job.width, job.height, QImage.Format.Format_RGBA8888)
    full.fill(QColor(0, 0, 0, 0))
    paint_frame(full, job.track, t_ms, job.style)
    full_rows = _img_rows(full)

    buffer = QImage(job.width, packed_h, QImage.Format.Format_RGBA8888)
    renderer._paint_overlay_bands(
        buffer, job.track, job.style, t_ms,
        logical_w=job.width, logical_h=job.height,
        bands=bands, transparent=QColor(0, 0, 0, 0),
    )
    packed_rows = _img_rows(buffer)

    offsets = _packed_offsets(bands)
    for (top, h), off in zip(bands, offsets):
        np.testing.assert_array_equal(
            packed_rows[off : off + h], full_rows[top : top + h]
        )


def test_multiprocess_bands_is_byte_identical_to_single_process(qapp, tmp_path):
    job = _band_job(tmp_path)
    bands = _compute_content_bands(job, 1000)
    assert bands is not None and len(bands) >= 2
    packed_h = sum(h for _t, h in bands)
    total = _frame_count(job.duration_ms, job.fps)

    single = _CollectProcess()
    _write_frames_single_bands(single, job, bands, packed_h, total, None, None)

    multi = _CollectProcess()
    _write_frames_multiprocess_bands(multi, job, bands, packed_h, total, 2, None, None)

    assert len(single.stdin.data) == total * job.width * packed_h * 4
    assert bytes(multi.stdin.data) == bytes(single.stdin.data)


# ---------------------------------------------------------------------------
# 条带/多带安全边：动画纵向行程上界（4K 丢字幕修复）
# ---------------------------------------------------------------------------


def test_strip_safety_margin_scales_with_output_height(tmp_path):
    job = _job(tmp_path)  # 320x180：低于 1080p 基准，保持基础边 8px
    assert _strip_safety_margin(job) == 8
    big = replace(job, height=2160)
    assert _strip_safety_margin(big) == 16  # 4K：8 × 2160/1080


def test_strip_safety_margin_includes_animation_excursion(tmp_path):
    # utopia 退场 y_travel 上界 = height/15 + 1.5×字号
    job = replace(
        _job(tmp_path),
        height=2160,
        style=Style(font_size_px=24, exit_anim="utopia", exit_fade_ms=750),
    )
    expected = 2160 / 15.0 + 24 * 1.5
    assert _strip_safety_margin(job) >= math.ceil(expected)

    # rise 入场行程 = max(字号×0.35, 18)
    job_rise = replace(
        _job(tmp_path),
        style=Style(font_size_px=200, entry_anim="rise", entry_lead_ms=300),
    )
    assert _strip_safety_margin(job_rise) >= math.ceil(max(200 * 0.35, 18.0))


def test_strip_safety_margin_respects_line_animation_overrides(tmp_path):
    # 全局 none，但单行覆盖 utopia：行程上界必须按行生效
    line = TimingLine(
        chars=[TimingChar("a", 0), TimingChar("b", 500)],
        end_ms=1000,
        animation_override=LineAnimationOverride(exit_anim="utopia"),
    )
    track = TimingTrack(lines=[line])
    job = replace(
        _job(tmp_path),
        track=track,
        height=2160,
    )
    assert _strip_safety_margin(job) >= math.ceil(2160 / 15.0)


def test_strip_safety_margin_unbounded_for_shear_animations(tmp_path):
    # char_drip / spin_flip 的剪切包络随首帧 tan 发散、且依赖行内字形宽度，
    # 无可靠上界 → 返回 None，条带/多带优化必须禁用退回整帧
    job_drip = replace(
        _job(tmp_path),
        style=Style(font_size_px=24, entry_anim="char_drip", entry_lead_ms=250),
    )
    assert _strip_safety_margin(job_drip) is None
    job_flip = replace(
        _job(tmp_path),
        style=Style(font_size_px=24, exit_anim="spin_flip", exit_fade_ms=250),
    )
    assert _strip_safety_margin(job_flip) is None


def test_strip_safety_margin_uses_max_referenced_font_size(tmp_path):
    # 角色方案 / 行内配色 / 注音可把字号覆盖到远超全局样式；utopia 的
    # 1.3×放大与 rise 行程按字形尺寸缩放，安全边必须按实际引用的最大字号估算
    style = Style(
        font_size_px=24,
        exit_anim="utopia",
        exit_fade_ms=750,
        singer_style_overrides={1: SubtitleStyleScheme(font_size_px=200)},
        custom_style_schemes={
            "custom": SubtitleStyleScheme(ruby_font_size_px=120),
            "unused": SubtitleStyleScheme(font_size_px=4096),
        },
    )
    referenced_line = TimingLine(
        chars=[TimingChar("a", 0, role_label="custom"), TimingChar("b", 500)],
        end_ms=1000,
        singer_id=1,
    )
    referenced_track = TimingTrack(lines=[referenced_line])
    job = replace(
        _job(tmp_path), style=style, track=referenced_track, height=2160
    )
    assert _max_project_font_size(style, [referenced_track]) == 200.0
    expected = 2160 / 15.0 + 200 * 1.5
    assert _strip_safety_margin(job) >= math.ceil(expected)

    # 未被引用的方案不参与估算：无角色标签 / 歌手不匹配 / 标题未启用时，
    # N3 导入或编辑遗留的 4096px 方案不应把安全边撑大、让条带优化失效
    #（45 = Style 默认全局注音字号 ruby_font_size_px，始终生效）
    plain_track = TimingTrack(
        lines=[TimingLine(chars=[TimingChar("a", 0)], end_ms=1000)]
    )
    assert _max_project_font_size(style, [plain_track]) == 45.0

    # 全局样式自身的拉丁/注音字号同样要进最大值：不使用角色方案、
    # 只调大全局 latin/ruby 字号的工程不能按主字号算安全边
    plain_big_latin = Style(
        font_size_px=48,
        latin_font_size_px=4096,
        ruby_font_size_px=3000,
    )
    assert _max_project_font_size(plain_big_latin, [plain_track]) == 4096.0
    plain_big_ruby = Style(font_size_px=48, ruby_font_size_px=3000)
    assert _max_project_font_size(plain_big_ruby, [plain_track]) == 3000.0
    plain_utopia = replace(
        plain_big_latin,
        exit_anim="utopia",
        exit_fade_ms=750,
    )
    utopia_job = replace(
        _job(tmp_path), style=plain_utopia, track=plain_track, height=2160
    )
    assert _strip_safety_margin(utopia_job) >= math.ceil(2160 / 15.0 + 4096 * 1.5)

    # rise 同样按最大字号缩放（custom 方案经角色标签引用）
    rise_style = Style(
        font_size_px=24,
        entry_anim="rise",
        entry_lead_ms=300,
        custom_style_schemes={
            "custom": SubtitleStyleScheme(font_size_px=300),
        },
    )
    rise_track = TimingTrack(
        lines=[
            TimingLine(
                chars=[TimingChar("a", 0, role_label="custom")],
                end_ms=1000,
            )
        ]
    )
    rise_job = replace(_job(tmp_path), style=rise_style, track=rise_track)
    assert _strip_safety_margin(rise_job) >= math.ceil(max(300 * 0.35, 18.0))


def test_strip_safety_margin_includes_vector_guide_span(tmp_path):
    # SVG 导入的矢量导唱符可远宽于 1em；utopia 旋转包络按「advance 枢轴到
    # 路径四角的最大距离 ×2」估算；位图导唱符走独立渲染路径，不参与该包络
    wide_vector = GuideSymbol(
        name="wide",
        path_commands=(
            ("M", 0.0, 0.0),
            ("L", 10000.0, 0.0),
            ("L", 0.0, 1000.0),
            ("Z",),
        ),
        units_per_em=1000,
        advance_width=10000.0,
    )
    bitmap = replace(
        wide_vector,
        kind="bitmap",
        bitmap_before_path="before.png",
        bitmap_after_path="after.png",
    )
    style = Style(font_size_px=48, exit_anim="utopia", exit_fade_ms=750)

    vector_track = TimingTrack(
        lines=[
            TimingLine(chars=[TimingChar("a", 0)], end_ms=1000, guide_symbol=wide_vector)
        ]
    )
    vector_job = replace(
        _job(tmp_path), style=style, track=vector_track, height=2160
    )
    # 枢轴 (advance/2, 基线) = (5em, 0)；最远角 (0,1em) → 半径 hypot(5,1)
    span_em = 2 * math.hypot(5.0, 1.0)
    expected = 2160 / 15.0 + 48 * max(1.5, span_em * 1.3)
    assert _strip_safety_margin(vector_job) >= math.ceil(expected)

    bitmap_track = TimingTrack(
        lines=[TimingLine(chars=[TimingChar("a", 0)], end_ms=1000, guide_symbol=bitmap)]
    )
    assert _max_guide_span_em([bitmap_track]) == 0.0
    assert _max_guide_span_em([vector_track]) == pytest.approx(span_em)

    # 轮廓本身只有 1em 宽、advance 却有 10em：旋转枢轴距轮廓 ~5em，
    # 只按路径宽高会把它估成 ~1.4em 而严重低估
    narrow_wide_advance = GuideSymbol(
        name="narrow",
        path_commands=(
            ("M", 0.0, 0.0),
            ("L", 1000.0, 0.0),
            ("L", 0.0, 1000.0),
            ("Z",),
        ),
        units_per_em=1000,
        advance_width=10000.0,
    )
    narrow_track = TimingTrack(
        lines=[
            TimingLine(
                chars=[TimingChar("a", 0)], end_ms=1000, guide_symbol=narrow_wide_advance
            )
        ]
    )
    assert _max_guide_span_em([narrow_track]) == pytest.approx(2 * math.hypot(5.0, 1.0))

    # 小轮廓悬离基线：枢轴在基线上，位置主导旋转半径
    floating = GuideSymbol(
        name="floating",
        path_commands=(
            ("M", 0.0, 8000.0),
            ("L", 1000.0, 8000.0),
            ("L", 0.0, 9000.0),
            ("Z",),
        ),
        units_per_em=1000,
        advance_width=1000.0,
    )
    floating_track = TimingTrack(
        lines=[
            TimingLine(chars=[TimingChar("a", 0)], end_ms=1000, guide_symbol=floating)
        ]
    )
    assert _max_guide_span_em([floating_track]) == pytest.approx(
        2 * math.hypot(0.5, 9.0)
    )


def test_strip_safety_margin_counts_guide_role_labels(tmp_path):
    # 行首导唱符可通过自身 role_label/role_labels 引用自定义方案并按该方案
    # 字号绘制；4096px 方案经导唱符引用时必须进入最大字号聚合
    guide = GuideSymbol(
        name="role_guide",
        path_commands=(
            ("M", 0.0, 0.0),
            ("L", 1000.0, 0.0),
            ("L", 0.0, 1000.0),
            ("Z",),
        ),
        units_per_em=1000,
        advance_width=1000.0,
        role_labels=("big", None),
        count=2,
    )
    style = Style(
        font_size_px=48,
        exit_anim="utopia",
        exit_fade_ms=750,
        custom_style_schemes={
            "big": SubtitleStyleScheme(font_size_px=4096),
        },
    )
    guide_track = TimingTrack(
        lines=[TimingLine(chars=[TimingChar("a", 0)], end_ms=1000, guide_symbol=guide)]
    )
    assert _max_project_font_size(style, [guide_track]) == 4096.0
    job = replace(_job(tmp_path), style=style, track=guide_track, height=2160)
    assert _strip_safety_margin(job) >= math.ceil(2160 / 15.0 + 4096 * 1.5)

    # 无导唱符、无角色标签引用时该方案不参与
    plain_track = TimingTrack(
        lines=[TimingLine(chars=[TimingChar("a", 0)], end_ms=1000)]
    )
    assert _max_project_font_size(style, [plain_track]) == 48.0


def test_compute_subtitle_strip_disabled_for_char_drip(tmp_path):
    job = replace(
        _job(tmp_path),
        style=Style(font_size_px=24, entry_anim="char_drip", entry_lead_ms=250),
    )
    logs: list[str] = []
    assert _compute_subtitle_strip(job, 1000, logger=logs.append) is None
    assert _compute_content_bands(job, 1000, logger=logs.append) is None
    assert any("条带渲染已禁用" in message for message in logs)
    assert any("多带渲染已禁用" in message for message in logs)


def test_compute_subtitle_strip_covers_all_frame_content(qapp, tmp_path):
    # 预扫并集按采样时刻取值；含动画行程的安全边必须把任意帧的可见像素
    # 全部罩进条带，否则条带裁剪会静默丢字幕。
    style = Style(
        font_size_px=24,
        entry_anim="utopia",
        entry_lead_ms=300,
        exit_anim="utopia",
        exit_fade_ms=750,
    )
    job = replace(_job(tmp_path), style=style, height=1080)
    strip = _compute_subtitle_strip(job, 1000)
    assert strip is not None
    top, height = strip

    for index in range(_frame_count(1000, job.fps)):
        t_ms = int(round(index * 1000 / job.fps))
        image = QImage(job.width, job.height, QImage.Format.Format_RGBA8888)
        image.fill(QColor(0, 0, 0, 0))
        paint_frame(image, job.track, t_ms, job.style)
        bounds = renderer._content_row_bounds(image)
        if bounds is None:
            continue
        assert bounds[0] >= top, f"t={t_ms}ms 内容顶部 {bounds[0]} 越出条带顶 {top}"
        assert bounds[1] < top + height, (
            f"t={t_ms}ms 内容底部 {bounds[1]} 越出条带底 {top + height - 1}"
        )


# ---------------------------------------------------------------------------
# 多进程内存护栏：有界在飞窗口 + 停滞超时
# ---------------------------------------------------------------------------


def test_resolve_pending_window_bounds_by_bytes_and_workers():
    frame_bytes_4k = 3840 * 2160 * 4  # ~31.6MiB
    # 256MB 在飞上限 → 8 条（8×31.6MiB=253MiB）；worker+2 更小时取 worker+2
    assert _resolve_pending_window(8, 1, frame_bytes_4k) == 8
    assert _resolve_pending_window(2, 1, frame_bytes_4k) == 4
    # 1080p + 8 worker（chunk 缩放为 3 帧 ≈ 23.6MiB）：窗口 10，喂满全部 worker
    assert _resolve_pending_window(8, 3, 1920 * 1080 * 4) == 10
    # 小帧不受字节约束，只受 worker 数约束
    assert _resolve_pending_window(8, 1, 1024) == 10
    # 单帧巨大（8K ~127MiB）：窗口被字节上限压到 2，绝不突破 256MiB 预算；
    # 并行度由 _resolve_effective_worker_count 同步降 worker 保住
    assert _resolve_pending_window(8, 1, 7680 * 4320 * 4) == 2


def test_resolve_effective_worker_count_caps_by_budget():
    frame_bytes_4k = 3840 * 2160 * 4
    # 8K 自动 8 worker：256MiB/127MiB = 2 → 降为 2，窗口与预算同时成立
    assert _resolve_effective_worker_count(8, 1, 7680 * 4320 * 4) == 2
    # 4K 手动 16 worker：256MiB/31.6MiB = 8 → 降为 8
    assert _resolve_effective_worker_count(16, 1, frame_bytes_4k) == 8
    # 4K 自动 8 worker：预算内放得下，不降
    assert _resolve_effective_worker_count(8, 1, frame_bytes_4k) == 8
    # 1080p + 8 worker（chunk ≈ 23.6MiB）：不降，高性能机器并行度不受影响
    assert _resolve_effective_worker_count(8, 3, 1920 * 1080 * 4) == 8
    # 极端下限：预算只容 1 块时保底单 worker
    assert _resolve_effective_worker_count(8, 1, 512 * 1024 * 1024) == 1


def test_resolve_effective_worker_count_caps_4k_by_available_system_memory():
    frame_bytes_4k = 3840 * 2160 * 4
    gib = 1024 * 1024 * 1024
    # 只剩 2GiB 可用时，保留 UI/ffmpeg 与主进程结果窗口后，
    # 4K 全幅 worker 按 QImage + empty + chunk 多份副本估算只容 3 个。
    assert _resolve_effective_worker_count(
        8,
        1,
        frame_bytes_4k,
        available_memory_bytes=2 * gib,
    ) == 3
    # 高内存机器保持原有自动 8 worker，不牺牲性能。
    assert _resolve_effective_worker_count(
        8,
        1,
        frame_bytes_4k,
        available_memory_bytes=16 * gib,
    ) == 8
    # 无法查询系统内存时保持旧的在飞结果护栏。
    assert _resolve_effective_worker_count(
        8,
        1,
        frame_bytes_4k,
        available_memory_bytes=None,
    ) == 8


def test_high_memory_4k_manual_16_workers_are_not_capped_to_8():
    frame_bytes_4k = 3840 * 2160 * 4
    gib = 1024 * 1024 * 1024
    available = 16 * gib
    pending_budget = _resolve_pending_memory_budget(available)

    assert pending_budget == gib
    effective = _resolve_effective_worker_count(
        16,
        1,
        frame_bytes_4k,
        available_memory_bytes=available,
        pending_budget_bytes=pending_budget,
    )
    assert effective == 16
    # worker+2 的窗口能同时喂满 16 worker，且约 570MiB < 1GiB。
    assert _resolve_pending_window(
        effective,
        1,
        frame_bytes_4k,
        pending_budget_bytes=pending_budget,
    ) == 18


def test_low_memory_pending_budget_stays_conservative():
    mib = 1024 * 1024
    assert _resolve_pending_memory_budget(None) == 256 * mib
    assert _resolve_pending_memory_budget(2 * 1024 * mib) == 256 * mib


def test_resolve_stall_timeout_scales_with_chunk_bytes(monkeypatch):
    monkeypatch.delenv("KROK_SUBTITLE_RENDER_STALL_TIMEOUT_S", raising=False)
    # 4K 全幅单帧（~31.6MiB）：60s 基础 + 每 512KiB 1s ≈ 123s
    assert _resolve_stall_timeout_s(1, 3840 * 2160 * 4) == pytest.approx(123.3, abs=1.0)
    # 窄条带多帧（42×1.5MiB）：按字节估算约 183s，而非按帧数的 42×60s
    assert _resolve_stall_timeout_s(42, 1920 * 200 * 4) == pytest.approx(183.0, abs=1.0)
    monkeypatch.setenv("KROK_SUBTITLE_RENDER_STALL_TIMEOUT_S", "5")
    assert _resolve_stall_timeout_s(10, 1024) == 5.0
    monkeypatch.setenv("KROK_SUBTITLE_RENDER_STALL_TIMEOUT_S", "bad")
    assert _resolve_stall_timeout_s(1, 1024) == pytest.approx(60.0, abs=0.1)


def test_iter_ordered_pool_results_bounds_in_flight_and_keeps_order():
    class _AsyncResult:
        def __init__(self, pool, value):
            self._pool = pool
            self._value = value

        def get(self, timeout=None):
            self._pool.live -= 1
            return self._value

    class _FakePool:
        def __init__(self):
            self.live = 0
            self.peak = 0

        def apply_async(self, fn, args):
            self.live += 1
            self.peak = max(self.peak, self.live)
            return _AsyncResult(self, fn(*args))

    tasks = [(index, 1) for index in range(20)]
    pool = _FakePool()
    out = list(
        _iter_ordered_pool_results(
            pool,
            lambda task: task[0] * 2,
            tasks,
            window=3,
            stall_timeout_s=5.0,
        )
    )
    assert out == [index * 2 for index in range(20)]
    assert pool.peak <= 3  # 派发从不超过窗口


def test_drain_pending_head_reports_stalled_worker(monkeypatch):
    import multiprocessing as mp

    monkeypatch.setattr(renderer, "_MULTIPROC_POLL_INTERVAL_S", 0.01)

    class _StalledResult:
        def get(self, timeout=None):
            raise mp.TimeoutError()

    pending = deque([_StalledResult()])
    with pytest.raises(ProcessingError) as excinfo:
        _drain_pending_head(pending, 0.05)
    assert "无响应" in str(excinfo.value)
    assert not pending


def test_drain_pending_head_responds_to_cancel(monkeypatch):
    import multiprocessing as mp

    monkeypatch.setattr(renderer, "_MULTIPROC_POLL_INTERVAL_S", 0.01)

    class _StalledResult:
        def get(self, timeout=None):
            raise mp.TimeoutError()

    cancel_calls = {"count": 0}

    def should_cancel() -> bool:
        cancel_calls["count"] += 1
        return cancel_calls["count"] >= 2

    pending = deque([_StalledResult()])
    # 等待期间取消应立即生效，而不是等到停滞截止才被外层发现
    with pytest.raises(ExportCancelled):
        _drain_pending_head(pending, 60.0, should_cancel=should_cancel)
    assert cancel_calls["count"] >= 2


def test_drain_pending_head_polls_until_result_arrives(monkeypatch):
    import multiprocessing as mp

    monkeypatch.setattr(renderer, "_MULTIPROC_POLL_INTERVAL_S", 0.01)

    class _LateResult:
        def __init__(self):
            self.calls = 0

        def get(self, timeout=None):
            self.calls += 1
            if self.calls < 3:
                raise mp.TimeoutError()
            return b"blob"

    result = _LateResult()
    pending = deque([result])
    assert _drain_pending_head(pending, 60.0) == b"blob"
    assert result.calls == 3


def test_drain_pending_head_wraps_worker_exceptions(monkeypatch):
    monkeypatch.setattr(renderer, "_MULTIPROC_POLL_INTERVAL_S", 0.01)

    class _MemoryErrorResult:
        def get(self, timeout=None):
            raise MemoryError("cannot allocate QImage")

    pending = deque([_MemoryErrorResult()])
    # worker 内原样冒泡的 MemoryError 不是 ProcessingError/OSError，会绕过
    # ffmpeg 清理分支；必须包装成 ProcessingError 走统一清理
    with pytest.raises(ProcessingError) as excinfo:
        _drain_pending_head(pending, 60.0)
    assert "渲染 worker 异常" in str(excinfo.value)
    assert "MemoryError" in str(excinfo.value)
    assert isinstance(excinfo.value.__cause__, MemoryError)


def test_multiprocess_writer_releases_blob_before_next_dispatch(monkeypatch, tmp_path):
    # 消费者侧 blob 引用必须在下一次取结果前释放，否则瞬态峰值是
    # (window+1)×chunk 而不是声明的 window×chunk（8K 下差 ~127MiB）
    import gc
    import weakref

    class _Blob:
        """支持 buffer 协议（可直接 += 进 bytearray）且可 weakref 的 blob 替身。"""

        def __init__(self, payload: bytes):
            self._payload = payload

        def __buffer__(self, flags: int = 0) -> memoryview:
            return memoryview(self._payload)

    job = replace(_job(tmp_path), width=2, height=2, fps=2, duration_ms=1000)
    total = _frame_count(job.duration_ms, job.fps)
    alive: dict[int, weakref.ref] = {}
    violations: list[int] = []

    def make_blob(index: int) -> "_Blob":
        blob = _Blob(b"x" * 8)
        alive[index] = weakref.ref(blob)
        return blob

    def fake_iter(pool, task_fn, tasks, *, window, stall_timeout_s, should_cancel=None):
        for index, _task in enumerate(tasks):
            if index:
                gc.collect()
                if alive[index - 1]() is not None:
                    violations.append(index - 1)
            # 直接 yield 表达式：生成器帧不能持有 blob 局部引用，否则
            # 消费者释放后仍会被本测试误报为泄漏
            yield make_blob(index)

    monkeypatch.setattr(renderer, "_iter_ordered_pool_results", fake_iter)
    proc = _CollectProcess()
    renderer._write_frames_multiprocess(proc, job, 0, job.height, total, 2, None, None)
    assert violations == []
    assert len(proc.stdin.data) > 0  # 假 blob 为 8 字节，只验证确有数据流过


def test_render_processing_error_cleans_up_ffmpeg_and_output(monkeypatch, tmp_path):
    # 帧生产阶段的 ProcessingError（worker 停滞 / 异常退出）必须走完整清理：
    # 终止 ffmpeg、删除半成品，否则 ffmpeg 会一直等 stdin 并锁住输出文件
    job = replace(_job(tmp_path), width=2, height=2, fps=2, duration_ms=1000)
    fake_process = _FakeRenderProcess(job.output_path)
    job.output_path.write_bytes(b"partial")

    def stall_writer(process, *_args, **_kwargs):
        process.stdin.write(b"partial")
        raise ProcessingError("渲染 worker 超过 60s 无响应")

    monkeypatch.setenv("KROK_SUBTITLE_RENDER_STRIP", "0")
    monkeypatch.setattr(renderer, "find_tool", lambda _name, _ffmpeg_dir=None: "ffmpeg")
    monkeypatch.setattr(renderer, "_write_frames_single", stall_writer)
    monkeypatch.setattr(
        renderer.subprocess,
        "Popen",
        lambda *args, **kwargs: fake_process,
    )

    with pytest.raises(ProcessingError, match="无响应"):
        render_subtitle_video(job)

    assert fake_process.terminated is True
    assert not job.output_path.exists()


def test_render_progress_callback_error_cleans_up(monkeypatch, tmp_path):
    # 进度回调抛 RuntimeError（Qt 对象销毁/线程退出竞态）不属于任何既有
    # 清理分支，也必须终止 ffmpeg、删除半成品后原样上抛
    job = replace(_job(tmp_path), width=2, height=2, fps=2, duration_ms=1000)
    fake_process = _FakeRenderProcess(job.output_path)
    job.output_path.write_bytes(b"partial")

    def writer(process, _job, strip_top, render_h, total_frames, should_cancel, on_progress):
        process.stdin.write(b"partial")
        if on_progress is not None:
            on_progress(1, total_frames)

    def failing_progress(_done, _total):
        raise RuntimeError("Qt 对象已销毁")

    monkeypatch.setenv("KROK_SUBTITLE_RENDER_STRIP", "0")
    monkeypatch.setattr(renderer, "find_tool", lambda _name, _ffmpeg_dir=None: "ffmpeg")
    monkeypatch.setattr(renderer, "_write_frames_single", writer)
    monkeypatch.setattr(
        renderer.subprocess,
        "Popen",
        lambda *args, **kwargs: fake_process,
    )

    with pytest.raises(RuntimeError, match="Qt 对象已销毁"):
        render_subtitle_video(job, on_progress=failing_progress)

    assert fake_process.terminated is True
    assert not job.output_path.exists()


# ---------------------------------------------------------------------------
# 输出格式：PNG 序列（透明/含背景）与透明 MOV（QuickTime 动画）
# ---------------------------------------------------------------------------


def _format_job(tmp_path: Path, output_format: str, output_path: Path | None = None):
    job = _job(tmp_path)
    if output_path is None:
        if output_format in {"png_transparent", "png_composited"}:
            output_path = tmp_path / "seq"
        elif output_format == "mov_transparent":
            output_path = tmp_path / "alpha.mov"
        else:
            output_path = job.output_path
    return replace(job, output_format=output_format, output_path=output_path)


def test_build_render_command_png_transparent_skips_background(tmp_path):
    job = _format_job(tmp_path, "png_transparent")

    command = build_render_command_contract("ffmpeg", job)

    # 透明格式没有背景输入：唯一输入就是字幕层 rawvideo pipe。
    assert command.count("-i") == 1
    fg = command[command.index("-filter_complex") + 1]
    assert "color=c=black@0.0:s=320x180:r=60" in fg
    assert "overlay=0:0:format=auto" in fg
    assert "[1:v:0]" not in fg
    assert command[command.index("-c:v") + 1] == "png"
    pix_fmts = [command[i + 1] for i, part in enumerate(command) if part == "-pix_fmt"]
    assert pix_fmts == ["rgba", "rgba"]  # 输入 pipe 与输出 PNG 都保留 alpha
    assert command[command.index("image2") - 1] == "-f"
    assert command[command.index("-start_number") + 1] == "1"
    pattern = (tmp_path / "seq" / "seq_%06d.png").as_posix()
    assert pattern in command
    # PNG 序列不携带音频，也不该出现 MP4 专属参数。
    assert "1:a:0?" not in command
    assert "-movflags" not in command
    assert "libx264" not in command


def test_build_render_command_png_composited_keeps_background(tmp_path):
    job = _format_job(tmp_path, "png_composited")

    command = build_render_command_contract("ffmpeg", job)

    assert command.count("-i") == 2  # pipe + 背景
    fg = command[command.index("-filter_complex") + 1]
    assert "[1:v:0]scale=320:180" in fg
    assert command[command.index("-c:v") + 1] == "png"
    pix_fmts = [command[i + 1] for i, part in enumerate(command) if part == "-pix_fmt"]
    assert pix_fmts == ["rgba", "rgb24"]  # 合成画面不透明，输出用 rgb24
    assert "1:a:0?" not in command


def test_build_render_command_mov_transparent_uses_prores4444(tmp_path):
    job = _format_job(tmp_path, "mov_transparent")

    command = build_render_command_contract("ffmpeg", job)

    # 透明 MOV 不解码背景画面，但音频与 MP4 同源（背景视频仅作音频输入）。
    assert command.count("-i") == 2
    assert "1:a:0?" in command
    assert command[command.index("-c:v") + 1] == "prores_ks"
    assert command[command.index("-profile:v") + 1] == "4444"
    # prores_ks 只有 4444/4444 XQ 档支持 alpha，像素格式必须带 A。
    assert command[command.index("-pix_fmt", command.index("-profile:v")) + 1] == (
        "yuva444p10le"
    )
    assert command[command.index("-c:a") + 1] == "pcm_s16le"
    assert str(job.output_path) == command[-1]
    assert "-movflags" not in command
    assert "libx264" not in command


def test_build_render_command_mov_transparent_can_skip_audio(tmp_path):
    job = replace(_format_job(tmp_path, "mov_transparent"), include_audio=False)

    command = build_render_command_contract("ffmpeg", job)

    assert command.count("-i") == 1
    assert "1:a:0?" not in command
    assert "-c:a" not in command


def test_build_render_command_mov_transparent_uses_independent_audio(tmp_path):
    audio = tmp_path / "song.wav"
    audio.write_bytes(b"fake")
    job = replace(
        _format_job(tmp_path, "mov_transparent"),
        background_video_path=None,
        background_source=BackgroundSource(kind="solid", color="#000000"),
        audio_path=audio,
    )

    command = build_render_command_contract("ffmpeg", job)

    assert command[command.index(str(audio)) - 1] == "-i"
    assert "1:a:0?" in command  # 透明格式没有背景输入，独立音频排输入 1


def test_validate_render_job_transparent_requires_audio_source_video(tmp_path):
    missing = tmp_path / "gone.mp4"
    job = replace(
        _format_job(tmp_path, "mov_transparent"),
        background_video_path=missing,
        background_source=BackgroundSource(kind="video", path=str(missing)),
    )

    with pytest.raises(ProcessingError, match="背景素材不存在"):
        validate_render_job(job)


def test_build_render_command_transparent_strip_keeps_full_canvas_base(tmp_path):
    job = _format_job(tmp_path, "mov_transparent")

    command = build_render_command_contract("ffmpeg", job, strip=(40, 60))

    # 条带优化只缩小 pipe 高度；透明底图仍是全画布，overlay 摆回原位。
    assert "320x60" in command
    fg = command[command.index("-filter_complex") + 1]
    assert "color=c=black@0.0:s=320x180:r=60" in fg
    assert "overlay=0:40:format=auto" in fg


def test_build_render_command_transparent_bands_drop_background_input(tmp_path):
    job = replace(
        _band_job(tmp_path), output_format="png_transparent", output_path=tmp_path / "seq"
    )

    command = build_render_command_contract("ffmpeg", job, bands=[(0, 40), (600, 60)])

    fg = command[command.index("-filter_complex") + 1]
    assert "[1:v:0]" not in fg
    assert "color=c=black@0.0:s=320x720" in fg
    assert "split=2[p0][p1]" in fg
    assert "[bg][c0]overlay=0:0" in fg


def test_sequence_output_pattern_escapes_literal_percent():
    from krok_helper.subtitle_render.engine.export.export_command import (
        sequence_output_pattern,
    )

    assert (
        sequence_output_pattern(Path("out") / "100%Love")
        == "out/100%%Love/100%%Love_%06d.png"
    )


def test_validate_render_job_rejects_unknown_output_format(tmp_path):
    with pytest.raises(ProcessingError, match="不支持的输出格式"):
        validate_render_job(replace(_job(tmp_path), output_format="avi"))


def test_validate_render_job_allows_missing_background_for_transparent_formats(
    tmp_path,
):
    job = replace(
        _format_job(tmp_path, "mov_transparent"),
        background_video_path=None,
        background_source=None,
    )
    validate_render_job(job)  # 不抛错：透明格式不需要背景


def test_validate_render_job_rejects_non_empty_sequence_folder(tmp_path):
    seq = tmp_path / "seq"
    seq.mkdir()
    (seq / "seq_000001.png").write_bytes(b"old frame")

    with pytest.raises(ProcessingError, match="不是空的"):
        validate_render_job(_format_job(tmp_path, "png_transparent", output_path=seq))


def test_validate_render_job_allows_missing_sequence_folder(tmp_path):
    validate_render_job(_format_job(tmp_path, "png_composited"))


def test_incomplete_sequence_cleanup_removes_pattern_frames_and_folder(tmp_path):
    job = _format_job(tmp_path, "png_transparent")
    seq = job.output_path
    seq.mkdir()
    (seq / "seq_000001.png").write_bytes(b"a")
    (seq / "seq_000002.png").write_bytes(b"b")
    logs: list[str] = []

    renderer._remove_incomplete_output(job, logs.append)

    assert not list(seq.glob("seq_*.png"))
    assert not seq.exists()  # 帧删完后目录已空，顺手移除
    assert any("已清理未完成的 PNG 序列输出" in line for line in logs)


def test_incomplete_sequence_cleanup_keeps_unrelated_files(tmp_path):
    job = _format_job(tmp_path, "png_transparent")
    seq = job.output_path
    seq.mkdir()
    (seq / "seq_000001.png").write_bytes(b"a")
    (seq / "notes.txt").write_bytes(b"keep me")
    logs: list[str] = []

    renderer._remove_incomplete_output(job, logs.append)

    assert (seq / "notes.txt").exists()
    assert seq.is_dir()  # 目录里还有用户文件，保留目录本身


def _patch_render_single(monkeypatch, fake_process, writer=None):
    monkeypatch.setenv("KROK_SUBTITLE_RENDER_STRIP", "0")
    monkeypatch.setattr(renderer, "find_tool", lambda _name, _ffmpeg_dir=None: "ffmpeg")
    if writer is not None:
        monkeypatch.setattr(renderer, "_write_frames_single", writer)
    monkeypatch.setattr(
        renderer.subprocess, "Popen", lambda *args, **kwargs: fake_process
    )


class _FakeSequenceProcess(_FakeRenderProcess):
    """ffmpeg 假进程：wait() 时按 fps/时长往序列目录里写帧。"""

    def __init__(self, sequence_dir: Path, frame_count: int, *, write_frames: bool = True):
        super().__init__(sequence_dir)
        self.frame_count = frame_count
        self.write_frames = write_frames

    def wait(self, timeout=None):
        if self.returncode is None:
            if self.write_frames:
                for index in range(1, self.frame_count + 1):
                    name = f"{self.output_path.name}_{index:06d}.png"
                    (self.output_path / name).write_bytes(b"png")
            self.returncode = 0
        return self.returncode


def test_render_png_sequence_success_requires_frames(monkeypatch, tmp_path):
    job = replace(
        _format_job(tmp_path, "png_transparent"), width=2, height=2, fps=2, duration_ms=1000
    )
    fake_process = _FakeSequenceProcess(job.output_path, frame_count=2)
    _patch_render_single(monkeypatch, fake_process)

    result = render_subtitle_video(job)

    assert result == job.output_path
    assert len(list(job.output_path.glob("seq_*.png"))) == 2


def test_render_png_sequence_without_frames_is_failure(monkeypatch, tmp_path):
    job = replace(
        _format_job(tmp_path, "png_transparent"), width=2, height=2, fps=2, duration_ms=1000
    )
    fake_process = _FakeSequenceProcess(job.output_path, frame_count=2, write_frames=False)
    _patch_render_single(monkeypatch, fake_process)

    with pytest.raises(ProcessingError, match="未生成 PNG 序列帧"):
        render_subtitle_video(job)

    assert not job.output_path.exists()  # 空目录也要清掉


def test_render_png_sequence_cancel_cleans_partial_frames(monkeypatch, tmp_path):
    job = replace(
        _format_job(tmp_path, "png_transparent"), width=2, height=2, fps=2, duration_ms=1000
    )

    def writer(process, _job, strip_top, render_h, total_frames, should_cancel, on_progress):
        process.stdin.write(b"p")
        # 模拟 ffmpeg 已经落了两帧半成品
        for index in (1, 2):
            name = f"{job.output_path.name}_{index:06d}.png"
            (job.output_path / name).write_bytes(b"partial")

    fake_process = _FakeSequenceProcess(job.output_path, frame_count=0)
    _patch_render_single(monkeypatch, fake_process, writer=writer)

    with pytest.raises(ExportCancelled):
        render_subtitle_video(job, should_cancel=lambda: True)

    # 假 ffmpeg 已正常退出（终止标记不适用），但半成品帧与目录必须清掉。
    assert not job.output_path.exists()


def test_render_mov_transparent_success_uses_file_check(monkeypatch, tmp_path):
    job = replace(
        _format_job(tmp_path, "mov_transparent"), width=2, height=2, fps=2, duration_ms=1000
    )
    fake_process = _FakeRenderProcess(job.output_path)
    _patch_render_single(monkeypatch, fake_process)

    result = render_subtitle_video(job)

    assert result == job.output_path
    assert job.output_path.is_file()


def test_build_render_command_mov_qtrle_uses_animation_codec(tmp_path):
    job = _format_job(tmp_path, "mov_qtrle", output_path=tmp_path / "q.mov")

    command = build_render_command_contract("ffmpeg", job)

    # QuickTime 动画档：无损 qtrle + rgba，音频同样取背景视频内嵌轨。
    assert command.count("-i") == 2
    assert "1:a:0?" in command
    assert command[command.index("-c:v") + 1] == "qtrle"
    pix_fmts = [command[i + 1] for i, part in enumerate(command) if part == "-pix_fmt"]
    assert pix_fmts == ["rgba", "rgba"]
    assert command[command.index("-c:a") + 1] == "pcm_s16le"
    assert str(job.output_path) == command[-1]


def test_render_reports_stage_progress_logs(monkeypatch, tmp_path, qapp):
    """准备/收尾各阶段要有状态日志，长导出不能只显示「正在准备…」。"""

    monkeypatch.setenv("KROK_SUBTITLE_RENDER_STRIP", "1")
    monkeypatch.setenv("KROK_SUBTITLE_RENDER_BANDS", "0")
    job = replace(
        _format_job(tmp_path, "mov_transparent"),
        width=320,
        height=180,
        duration_ms=1000,
    )
    fake_process = _FakeRenderProcess(job.output_path)
    logs: list[str] = []
    monkeypatch.setattr(renderer, "find_tool", lambda _name, _ffmpeg_dir=None: "ffmpeg")
    monkeypatch.setattr(
        renderer.subprocess, "Popen", lambda *args, **kwargs: fake_process
    )

    render_subtitle_video(job, logger=logs.append)

    assert any("准备：预扫字幕渲染范围" in line for line in logs)
    assert any("预扫字幕渲染范围（" in line for line in logs)  # 百分比进度行
    assert any("单进程渲染" in line for line in logs)
    assert any("帧已全部提交" in line for line in logs)


def test_preview_tap_composites_gray_base_for_transparent_formats(tmp_path):
    """透明格式的导出监视 jpg 垫中灰底：jpg 无 alpha，黑底看起来像渲染失败。"""

    job = _format_job(tmp_path, "mov_transparent")
    command = build_render_command_contract(
        "ffmpeg", job, preview_image_path=tmp_path / "frame.jpg", preview_width=320
    )

    fg = command[command.index("-filter_complex") + 1]
    assert "color=c=0x3F444C:s=320x180:r=60" in fg
    assert "[vpbg][vpin]overlay=0:0:format=auto[vpcomp]" in fg
    assert "[vpcomp]fps=2,scale=320:-2[vprev]" in fg


def test_preview_tap_keeps_plain_split_for_background_formats(tmp_path):
    job = _job(tmp_path, include_audio=False)
    command = build_render_command_contract(
        "ffmpeg", job, preview_image_path=tmp_path / "frame.jpg"
    )

    fg = command[command.index("-filter_complex") + 1]
    assert "[vpbg]" not in fg
    assert ";[v]split=2[venc][vpin];" in fg


def test_sequence_cleanup_retries_transient_file_locks(tmp_path, monkeypatch):
    """杀软短暂锁定刚写完的帧时，清理要重试而不是留下半成品目录。"""

    job = _format_job(tmp_path, "png_transparent")
    seq = job.output_path
    seq.mkdir()
    (seq / "seq_000001.png").write_bytes(b"a")
    calls = {"n": 0}
    real_unlink = Path.unlink

    def flaky_unlink(self, missing_ok=False):  # noqa: ANN001
        if self.name.endswith(".png") and calls["n"] == 0:
            calls["n"] += 1
            raise PermissionError(13, "file locked by scanner")
        return real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)
    logs: list[str] = []
    try:
        renderer._remove_incomplete_output(job, logs.append)
    finally:
        monkeypatch.undo()

    assert calls["n"] == 1  # 第一次被锁、重试成功
    assert not seq.exists()
