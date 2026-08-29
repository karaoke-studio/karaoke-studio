"""Hi-Res 混流 ffmpeg 命令构建。

重点盯流映射：MOV 输入带 tmcd 时间码 data 流时，Matroska 输出会拒绝写
header，必须显式丢掉 data 流（2026-08 用户反馈 MOV 封装失败）。
"""

from __future__ import annotations

from pathlib import Path

from krok_helper.pipeline import build_audio_normalization_command, build_mux_command


def _map_args(command: list[str]) -> list[str]:
    args = []
    index = 0
    while index < len(command):
        if command[index] == "-map":
            args.append(command[index + 1])
            index += 2
        else:
            index += 1
    return args


class TestBuildMuxCommand:
    def test_data_streams_are_dropped_from_the_video_input(self, tmp_path: Path) -> None:
        command = build_mux_command(
            ffmpeg_path="ffmpeg",
            video_path=tmp_path / "v.mov",
            audio_path=tmp_path / "a.flac",
            output_path=tmp_path / "o.mkv",
            audio_title="Hi-Res Audio",
        )
        assert _map_args(command) == ["0", "-0:a", "-0:d", "1:a:0"]


class TestBuildAudioNormalizationCommand:
    def test_it_always_excludes_non_audio_streams(self, tmp_path: Path) -> None:
        command = build_audio_normalization_command(
            ffmpeg_path="ffmpeg",
            audio_path=tmp_path / "a.mov",
            output_path=tmp_path / "a.flac",
            sample_rate=192000,
        )
        assert _map_args(command) == ["0:a:0"]
