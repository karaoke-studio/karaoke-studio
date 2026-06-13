from __future__ import annotations

from pathlib import Path

from krok_helper.audio_alignment import (
    ENCODE_MODE_HARDWARE,
    ENCODE_MODE_SOFTWARE,
    _audio_encoding_options,
    _video_encoding_options,
    build_aligned_video_command,
)


def test_hardware_encoding_uses_nvenc_for_h264_source() -> None:
    options = _video_encoding_options({"codec_name": "h264"}, ENCODE_MODE_HARDWARE)

    assert options[:2] == ["-c:v", "h264_nvenc"]


def test_hardware_encoding_defaults_to_h264_nvenc_for_unsupported_source_codec() -> None:
    options = _video_encoding_options({"codec_name": "vp9"}, ENCODE_MODE_HARDWARE)

    assert options[:2] == ["-c:v", "h264_nvenc"]
    assert "libx264" not in options


def test_software_encoding_keeps_cpu_encoder() -> None:
    options = _video_encoding_options({"codec_name": "h264"}, ENCODE_MODE_SOFTWARE)

    assert options[:2] == ["-c:v", "libx264"]


def test_mp4_output_transcodes_pcm_audio_to_aac() -> None:
    options = _audio_encoding_options(
        {"codec_name": "pcm_s24le", "sample_rate": "48000", "channels": 2},
        output_path=Path("aligned.mp4"),
    )

    assert options[:2] == ["-c:a", "aac"]
    assert "pcm_s24le" not in options
    assert options[2:] == ["-ar:a", "48000", "-ac:a", "2", "-b:a", "320k"]


def test_mp4_output_transcodes_flac_audio_to_aac() -> None:
    options = _audio_encoding_options({"codec_name": "flac"}, output_path=Path("aligned.mp4"))

    assert options == ["-c:a", "aac", "-b:a", "320k"]


def test_mkv_output_transcodes_lossless_audio_to_flac() -> None:
    options = _audio_encoding_options({"codec_name": "alac"}, output_path=Path("aligned.mkv"))

    assert options[:2] == ["-c:a", "flac"]


def test_lossy_audio_keeps_existing_encoder_choice() -> None:
    options = _audio_encoding_options(
        {"codec_name": "aac", "bit_rate": "256000"},
        output_path=Path("aligned.mp4"),
    )

    assert options == ["-c:a", "aac", "-b:a", "256000"]


def test_aligned_mp4_command_uses_aac_for_pcm_replacement_audio() -> None:
    command = build_aligned_video_command(
        "ffmpeg",
        Path("subtitle.mp4"),
        Path("voice.wav"),
        Path("aligned.mp4"),
        0,
        source_payload={
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "h264",
                    "avg_frame_rate": "30000/1001",
                    "pix_fmt": "yuv420p",
                }
            ]
        },
        audio_payload={
            "streams": [
                {
                    "codec_type": "audio",
                    "codec_name": "pcm_s24le",
                    "sample_rate": "48000",
                    "channels": 2,
                }
            ]
        },
    )

    audio_codec_index = command.index("-c:a")
    assert command[audio_codec_index + 1] == "aac"
    assert command[audio_codec_index + 2 : audio_codec_index + 8] == [
        "-ar:a",
        "48000",
        "-ac:a",
        "2",
        "-b:a",
        "320k",
    ]
