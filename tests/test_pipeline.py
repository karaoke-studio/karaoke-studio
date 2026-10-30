"""Hi-Res 混流 ffmpeg 命令构建。

重点盯流映射：MOV 输入带 tmcd 时间码 data 流时，Matroska 输出会拒绝写
header，必须显式丢掉 data 流（2026-08 用户反馈 MOV 封装失败）。
"""

from __future__ import annotations

from pathlib import Path

from krok_helper.pipeline import (
    build_align_audio_command,
    build_audio_normalization_command,
    build_mux_command,
)


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


class TestBuildAlignAudioCommand:
    def test_audio_shorter_than_video_is_padded_to_video_length(
        self, tmp_path: Path
    ) -> None:
        command = build_align_audio_command(
            "ffmpeg",
            tmp_path / "v.mp4",
            tmp_path / "a.flac",
            tmp_path / "out.flac",
            pre_roll_ms=0,
            post_roll_ms=0,
            video_duration_ms=10_000,
            audio_duration_ms=6_000,
            video_has_audio=True,
        )
        graph = command[command.index("-filter_complex") + 1]
        assert "apad=whole_dur=10.000000" in graph
        assert "atrim=start=0:end=10.000000" in graph
        assert "[0:a:0]" not in graph

    def test_roll_audio_is_preserved_and_main_song_starts_after_pre_roll(
        self, tmp_path: Path
    ) -> None:
        command = build_align_audio_command(
            "ffmpeg",
            tmp_path / "v.mp4",
            tmp_path / "a.flac",
            tmp_path / "out.flac",
            pre_roll_ms=2_000,
            post_roll_ms=3_000,
            video_duration_ms=10_000,
            audio_duration_ms=8_000,
            video_has_audio=True,
        )
        graph = command[command.index("-filter_complex") + 1]
        assert "[0:a:0]atrim=start=0:end=2.000000" in graph
        assert "[0:a:0]atrim=start=7.000000:end=10.000000" in graph
        assert "[1:a:0]atrim=start=0:end=5.000000" in graph
        assert "adelay" not in graph
        assert "concat=n=3:v=0:a=1" in graph
        assert "apad=whole_dur=10.000000" in graph

    def test_no_video_audio_still_aligns_stem_without_roll_slices(
        self, tmp_path: Path
    ) -> None:
        command = build_align_audio_command(
            "ffmpeg",
            tmp_path / "v.mp4",
            tmp_path / "a.flac",
            tmp_path / "out.flac",
            pre_roll_ms=1_000,
            post_roll_ms=1_000,
            video_duration_ms=5_000,
            audio_duration_ms=3_000,
            video_has_audio=False,
        )
        graph = command[command.index("-filter_complex") + 1]
        assert "[0:a:0]" not in graph
        assert "anullsrc=" in graph
        assert "[1:a:0]atrim=start=0:end=3.000000" in graph
        assert "apad=whole_dur=5.000000" in graph


class TestBuildAudioNormalizationCommand:
    def test_it_always_excludes_non_audio_streams(self, tmp_path: Path) -> None:
        command = build_audio_normalization_command(
            ffmpeg_path="ffmpeg",
            audio_path=tmp_path / "a.mov",
            output_path=tmp_path / "a.flac",
            sample_rate=192000,
        )
        assert _map_args(command) == ["0:a:0"]
