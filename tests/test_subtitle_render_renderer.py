"""Tests for A8 rawvideo renderer."""

from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from krok_helper.errors import ExportCancelled, ProcessingError  # noqa: E402
from krok_helper.subtitle_render.engine import renderer  # noqa: E402
import numpy as np  # noqa: E402
from PyQt6.QtGui import QColor, QImage  # noqa: E402

from krok_helper.subtitle_render.engine.painter import paint_frame  # noqa: E402
from krok_helper.subtitle_render.engine.renderer import (  # noqa: E402
    RenderJob,
    _compute_content_bands,
    _compute_subtitle_strip,
    _frame_count,
    _image_bytes,
    _merge_intervals,
    _packed_offsets,
    _paint_overlay_bands,
    _paint_overlay_strip,
    _render_overlay_frame,
    _resolve_chunk_size,
    _resolve_worker_count,
    _write_frames_multiprocess,
    _write_frames_multiprocess_bands,
    _write_frames_single,
    _write_frames_single_bands,
    build_render_command,
    render_subtitle_video,
)
from krok_helper.subtitle_render.models import (  # noqa: E402
    Style,
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
    assert command[command.index("-c:v") + 1] == "libx264"
    assert command[command.index("-preset") + 1] == "veryfast"
    assert command[command.index("-crf") + 1] == "18"
    assert command.count("-r") == 2
    assert command[command.index("-fps_mode") + 1] == "cfr"


def test_build_render_command_can_skip_audio(tmp_path):
    command = build_render_command("ffmpeg", _job(tmp_path, include_audio=False))
    assert "1:a:0?" not in command
    assert "-c:a" not in command


def test_build_render_command_honors_cpu_quality_settings(tmp_path):
    job = replace(_job(tmp_path), crf=23, preset="slow")

    command = build_render_command("ffmpeg", job)

    assert command[command.index("-c:v") + 1] == "libx264"
    assert command[command.index("-preset") + 1] == "slow"
    assert command[command.index("-crf") + 1] == "23"


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


def test_compute_subtitle_strip_returns_subband_for_centered_line(qapp, tmp_path):
    job = replace(_job(tmp_path), style=Style(font_size_px=24, line_y_position="center"))
    strip = _compute_subtitle_strip(job, 1000)
    assert strip is not None
    top, height = strip
    assert 0 <= top
    assert top + height <= job.height
    assert height < job.height  # 比全高矮
    assert top % 2 == 0 and height % 2 == 0  # yuv420p 友好


def test_compute_subtitle_strip_uses_layer_bounds_without_alpha_scan(qapp, tmp_path, monkeypatch):
    job = replace(_job(tmp_path), style=Style(font_size_px=24, line_y_position="center"))

    def fail_paint_frame(*_args, **_kwargs):
        raise AssertionError("layer-bound path should not paint a full scratch frame")

    monkeypatch.setattr(renderer, "paint_frame", fail_paint_frame)
    assert _compute_subtitle_strip(job, 1000) is not None


def test_compute_subtitle_strip_uses_signal_layer_bounds_without_alpha_scan(qapp, tmp_path, monkeypatch):
    style = Style(
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
    job = replace(_job(tmp_path), style=Style(font_size_px=72, line_y_position="center"), height=80)
    assert _compute_subtitle_strip(job, 1000) is None


def test_strip_render_is_pixel_identical_to_full_frame_region(qapp, tmp_path):
    job = replace(_job(tmp_path), style=Style(font_size_px=24, line_y_position="center"))
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


def test_resolve_chunk_size_is_positive_and_balanced(tmp_path):
    job = replace(_job(tmp_path), width=1920, height=1080)
    chunk = _resolve_chunk_size(job, 1080, total_frames=10_000, worker_count=4)
    assert chunk >= 1
    # 每 worker 至少几块以均衡（不会一块独吞）
    assert chunk <= 10_000 // 4


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
        style=Style(font_size_px=24, line_y_position="center"),
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
        height=1,
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

    assert writes == [(0, 1, 2)]
    assert bytes(fake_process.stdin.data) == b"p" * 16
    assert progress == [(2, 2)]


def test_render_job_native_export_flag_and_environment_are_ignored(monkeypatch, tmp_path):
    monkeypatch.setenv("KROK_SUBTITLE_NATIVE_EXPORT", "1")

    assert renderer._native_export_requested(replace(_job(tmp_path), native_export_enabled=False)) is False
    assert renderer._native_export_requested(replace(_job(tmp_path), native_export_enabled=True)) is False

    monkeypatch.setenv("KROK_SUBTITLE_NATIVE_EXPORT", "0")

    assert renderer._native_export_requested(replace(_job(tmp_path), native_export_enabled=None)) is False
def test_render_falls_back_to_python_when_native_export_sidecar_missing(monkeypatch, tmp_path):
    job = replace(_job(tmp_path), width=2, height=1, fps=2, duration_ms=1000)
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

    assert writes == [(0, 1, 2)]
    assert bytes(fake_process.stdin.data) == b"p" * 16


def _band_job(tmp_path: Path) -> RenderJob:
    """顶部标题 + 底部歌词 的两块分离场景（中间大片空白 → 适合方案 B）。"""
    background = tmp_path / "bg.mp4"
    background.write_bytes(b"not-real-video")
    style = Style(
        font_size_px=48,
        line_y_position="bottom",
        title_overlay=TitleOverlay(
            enabled=True,
            text_template="标题",
            anchor="top_center",
            font_size_px=48,
            offset_y=20,
            show_mode="whole",
        ),
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
    assert last_top > 400
    assert last_top - (first_top + first_h) > renderer._BAND_MERGE_GAP_PX


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
