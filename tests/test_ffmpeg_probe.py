"""``probe_media`` 解析 ffprobe JSON 输出的覆盖测试。

通过 monkeypatch ``subprocess.run`` 注入预制 JSON，避免依赖系统 ffprobe。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from krok_helper import ffmpeg as ffmpeg_mod
from krok_helper.errors import ProcessingError


class _FakeCompleted:
    def __init__(self, stdout: str, returncode: int = 0, stderr: str = ""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


def _patch_ffprobe(monkeypatch, payload, returncode: int = 0, stderr: str = ""):
    text = json.dumps(payload)
    monkeypatch.setattr(
        ffmpeg_mod.subprocess,
        "run",
        lambda *args, **kwargs: _FakeCompleted(text, returncode, stderr),
    )


def test_probe_media_extracts_video_dims_and_fps(monkeypatch, tmp_path):
    _patch_ffprobe(
        monkeypatch,
        {
            "format": {"duration": "12.345"},
            "streams": [
                {
                    "codec_type": "video",
                    "width": 1920,
                    "height": 1080,
                    "avg_frame_rate": "60000/1001",
                    "r_frame_rate": "60/1",
                },
                {
                    "codec_type": "audio",
                    "sample_rate": "48000",
                    "channels": 2,
                },
            ],
        },
    )

    info = ffmpeg_mod.probe_media("ffprobe", tmp_path / "demo.mp4")
    assert info.video_streams == 1
    assert info.audio_streams == 1
    assert info.video_width == 1920
    assert info.video_height == 1080
    assert info.video_fps == pytest.approx(60000 / 1001)
    assert info.sample_rate == 48000
    assert info.channels == 2
    assert info.duration == pytest.approx(12.345)


def test_probe_media_falls_back_to_r_frame_rate(monkeypatch, tmp_path):
    _patch_ffprobe(
        monkeypatch,
        {
            "format": {"duration": "60"},
            "streams": [
                {
                    "codec_type": "video",
                    "width": 1280,
                    "height": 720,
                    "avg_frame_rate": "0/0",
                    "r_frame_rate": "30/1",
                }
            ],
        },
    )

    info = ffmpeg_mod.probe_media("ffprobe", tmp_path / "demo.mp4")
    assert info.video_fps == pytest.approx(30.0)


def test_probe_media_handles_missing_video_fields(monkeypatch, tmp_path):
    # 仅音频 → 视频字段保持 None
    _patch_ffprobe(
        monkeypatch,
        {
            "format": {"duration": "180"},
            "streams": [{"codec_type": "audio", "sample_rate": "44100", "channels": 2}],
        },
    )

    info = ffmpeg_mod.probe_media("ffprobe", tmp_path / "song.flac")
    assert info.video_streams == 0
    assert info.video_width is None
    assert info.video_height is None
    assert info.video_fps is None


def test_probe_media_raises_on_ffprobe_error(monkeypatch, tmp_path):
    _patch_ffprobe(monkeypatch, {}, returncode=1, stderr="Invalid data")
    with pytest.raises(ProcessingError):
        ffmpeg_mod.probe_media("ffprobe", tmp_path / "broken.mp4")


def test_probe_media_tolerates_na_video_fps(monkeypatch, tmp_path):
    _patch_ffprobe(
        monkeypatch,
        {
            "format": {"duration": "1"},
            "streams": [
                {
                    "codec_type": "video",
                    "width": 640,
                    "height": 480,
                    "avg_frame_rate": "N/A",
                    "r_frame_rate": "N/A",
                }
            ],
        },
    )

    info = ffmpeg_mod.probe_media("ffprobe", tmp_path / "demo.mp4")
    assert info.video_width == 640
    assert info.video_height == 480
    assert info.video_fps is None
