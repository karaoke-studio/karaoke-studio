"""Hi-Res 混流 ffmpeg 命令构建。

重点盯流映射：MOV 输入带 tmcd 时间码 data 流时，Matroska 输出会拒绝写
header，必须显式丢掉 data 流（2026-08 用户反馈 MOV 封装失败）。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from krok_helper.ffmpeg import find_tool
from krok_helper.pipeline import (
    build_align_audio_command,
    build_audio_normalization_command,
    build_mux_command,
    run_pipeline,
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
        assert "[0:a:0]atrim=duration=2.000000" in graph
        assert (
            "[0:a:0]apad=whole_dur=10.000000,"
            "atrim=start=7.000000:end=10.000000" in graph
        )
        assert "[1:a:0]atrim=duration=5.000000" in graph
        assert "adelay" not in graph
        assert "concat=n=3:v=0:a=1" in graph
        assert "apad=whole_dur=10.000000" in graph

    def test_roll_audio_prefers_original_files_over_final_video_audio(
        self, tmp_path: Path
    ) -> None:
        pre_audio = tmp_path / "pre.flac"
        post_audio = tmp_path / "post.flac"
        pre_audio.write_bytes(b"pre")
        post_audio.write_bytes(b"post")
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
            pre_audio_path=pre_audio,
            post_audio_path=post_audio,
        )
        graph = command[command.index("-filter_complex") + 1]
        assert "[2:a:0]atrim=duration=2.000000" in graph
        assert "[3:a:0]atrim=duration=3.000000" in graph
        assert "[0:a:0]" not in graph
        # 原始音频短于段长时，每段先 pad 再 concat，不能把后段整体前移。
        assert graph.count("apad=whole_dur=2.000000") == 1
        assert graph.count("apad=whole_dur=3.000000") == 1
        inputs = [
            command[index + 1]
            for index, token in enumerate(command)
            if token == "-i"
        ]
        assert inputs == [
            str(tmp_path / "v.mp4"),
            str(tmp_path / "a.flac"),
            str(pre_audio),
            str(post_audio),
        ]

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
        assert "[1:a:0]atrim=duration=3.000000" in graph
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


@pytest.mark.skipif(
    find_tool("ffmpeg") is None or find_tool("ffprobe") is None,
    reason="本机无 ffmpeg/ffprobe",
)
def test_original_roll_audio_exports_both_outputs_with_high_sample_rate(
    tmp_path: Path,
) -> None:
    ffmpeg = Path(find_tool("ffmpeg"))
    ffprobe = Path(find_tool("ffprobe"))
    video = tmp_path / "final.mp4"
    on = tmp_path / "on.flac"
    off = tmp_path / "off.flac"
    pre = tmp_path / "pre.flac"
    post = tmp_path / "post.flac"
    subprocess.run(
        [
            str(ffmpeg), "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc=size=160x90:rate=30:duration=2",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
            "-shortest", str(video),
        ],
        check=True,
    )
    for path, frequency in ((on, 880), (off, 220)):
        subprocess.run(
            [
                str(ffmpeg), "-y", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", f"sine=frequency={frequency}:duration=0.8",
                "-c:a", "flac", str(path),
            ],
            check=True,
        )
    for path, frequency in ((pre, 660), (post, 770)):
        subprocess.run(
            [
                str(ffmpeg), "-y", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi",
                "-i", f"sine=frequency={frequency}:duration=0.5:sample_rate=96000",
                "-c:a", "flac", "-sample_fmt", "s32", str(path),
            ],
            check=True,
        )
    outputs = run_pipeline(
        video_path=video,
        on_vocal_path=on,
        off_vocal_paths=[off],
        output_dir=tmp_path / "out",
        ffmpeg_dir=ffmpeg.parent,
        pre_roll_ms=500,
        post_roll_ms=500,
        pre_audio_path=pre,
        post_audio_path=post,
    )
    assert len(outputs) == 2
    for output in outputs:
        assert output.is_file()
        probe = json.loads(
            subprocess.check_output(
                [str(ffprobe), "-v", "error", "-show_streams", "-show_format", "-of", "json", str(output)]
            )
        )
        audio = next(s for s in probe["streams"] if s["codec_type"] == "audio")
        assert int(audio["sample_rate"]) == 96000
        assert abs(float(probe["format"]["duration"]) - 2.0) < 0.05
