from __future__ import annotations

import json
import zipfile

import pytest

from krok_helper.audio_processing.separation.audio_io import (
    build_pcm_command,
    extract_result_stems,
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
