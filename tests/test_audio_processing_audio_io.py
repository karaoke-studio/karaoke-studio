from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from krok_helper.audio_processing.separation.audio_io import (
    ACCEPTED_INPUT_EXTENSIONS,
    build_demux_command,
    build_pcm_command,
    extract_audio_track,
    extract_result_stems,
    is_video_input,
)


def _result_zip(path, outputs: list[dict], files: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w") as bundle:
        bundle.writestr("manifest.json", json.dumps({"outputs": outputs}))
        for name, data in files.items():
            bundle.writestr(name, data)


def test_pcm_command_outputs_stereo_float32_at_model_rate(tmp_path) -> None:
    command = build_pcm_command(
        "ffmpeg.exe", tmp_path / "输入.mp3", tmp_path / "input.f32le.part", 44100
    )
    assert command[0] == "ffmpeg.exe"
    assert command[command.index("-ac") + 1] == "2"
    assert command[command.index("-ar") + 1] == "44100"
    assert command[command.index("-c:a") + 1] == "pcm_f32le"
    assert command[command.index("-f") + 1] == "f32le"


def test_extract_result_stems_uses_chinese_labels_and_unique_names(tmp_path) -> None:
    archive = tmp_path / "result.zip"
    _result_zip(
        archive,
        [
            {"stem": "vocals", "filename": "0001-vocals.wav"},
            {"stem": "instrumental", "filename": "0002-instrumental.wav"},
        ],
        {"0001-vocals.wav": b"vocal", "0002-instrumental.wav": b"backing"},
    )
    output = tmp_path / "out"

    first = extract_result_stems(
        archive,
        output,
        labels={"vocals": "主唱", "instrumental": "和声"},
        base_name="歌曲",
    )
    second = extract_result_stems(
        archive,
        output,
        labels={"vocals": "主唱", "instrumental": "和声"},
        base_name="歌曲",
    )

    assert first["vocals"].name == "歌曲_主唱.wav"
    assert first["instrumental"].read_bytes() == b"backing"
    assert second["vocals"].name == "歌曲_主唱 (2).wav"


def test_extract_result_rejects_traversal_filename(tmp_path) -> None:
    archive = tmp_path / "bad.zip"
    _result_zip(
        archive,
        [{"stem": "vocals", "filename": "../escape.wav"}],
        {"../escape.wav": b"bad"},
    )
    with pytest.raises(ValueError, match="不安全"):
        extract_result_stems(archive, tmp_path / "out")


def test_demux_command_takes_the_first_audio_track_without_resampling(tmp_path) -> None:
    command = build_demux_command(
        "ffmpeg.exe", tmp_path / "MV.mp4", tmp_path / "MV.wav.part"
    )

    assert command[0] == "ffmpeg.exe"
    assert command[command.index("-map") + 1] == "0:a:0"
    assert "-vn" in command
    # 分离器内部会自己重采样，这里再转一次只会白白多一次有损转换。
    assert "-ar" not in command
    assert "-ac" not in command
    # 整数 PCM 才是所有 WAV 读取器都认的格式。
    assert command[command.index("-c:a") + 1] == "pcm_s24le"
    # 临时名是 .part，ffmpeg 推不出容器，必须显式点名。
    assert command[command.index("-f") + 1] == "wav"


def test_video_containers_are_accepted_and_flagged_for_demuxing() -> None:
    for name in ("MV.mp4", "MV.mkv", "MV.webm", "MV.ts", "MV.MOV"):
        assert Path(name).suffix.lower() in ACCEPTED_INPUT_EXTENSIONS
        assert is_video_input(name) is True


def test_audio_inputs_stay_on_the_untouched_pass_through_path() -> None:
    """音频素材不能被拖进解复用：那条路会把 ffmpeg 变成硬依赖。"""

    for name in ("歌.wav", "歌.flac", "歌.mp3", "歌.m4a", "歌.aac"):
        assert Path(name).suffix.lower() in ACCEPTED_INPUT_EXTENSIONS
        assert is_video_input(name) is False


def test_extract_audio_track_rejects_a_video_without_any_audio(tmp_path, monkeypatch) -> None:
    from krok_helper.audio_processing.separation import audio_io
    from krok_helper.models import MediaInfo

    monkeypatch.setattr(audio_io, "find_tool", lambda name, configured=None: name)
    monkeypatch.setattr(
        audio_io,
        "probe_media",
        lambda _probe, path: MediaInfo(
            path=path,
            duration=12.0,
            video_streams=1,
            audio_streams=0,
            subtitle_streams=0,
        ),
    )
    source = tmp_path / "无声.mp4"
    source.write_bytes(b"0")

    with pytest.raises(ValueError, match="不包含音轨"):
        extract_audio_track(source, tmp_path / "work")


def test_extract_audio_track_reports_a_missing_source(tmp_path) -> None:
    with pytest.raises(FileNotFoundError):
        extract_audio_track(tmp_path / "没有这个.mp4", tmp_path / "work")
