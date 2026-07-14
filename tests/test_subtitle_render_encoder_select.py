"""Tests for subtitle render video encoder selection."""

from __future__ import annotations

from krok_helper.subtitle_render.engine import encoder_select as enc


def test_auto_encoder_uses_available_hardware(monkeypatch):
    monkeypatch.setattr(enc, "_available_encoders", lambda _ffmpeg_path: frozenset({"h264_qsv"}))

    options = enc.video_encoder_options("ffmpeg", "auto", crf=21, preset="slow")

    assert options[:2] == ["-c:v", "h264_qsv"]
    assert "-global_quality" in options


def test_auto_encoder_falls_back_to_cpu(monkeypatch):
    monkeypatch.setattr(enc, "_available_encoders", lambda _ffmpeg_path: frozenset())

    options = enc.video_encoder_options("ffmpeg", "auto", crf=21, preset="slow")

    assert options == ["-c:v", "libx264", "-preset", "slow", "-crf", "21"]


def test_encoder_options_clamp_crf_and_normalize_bad_values():
    options = enc.video_encoder_options("ffmpeg", "bad", crf=99, preset="turbo")

    assert options == ["-c:v", "libx264", "-preset", "medium", "-crf", "51"]


def test_hevc_cpu_uses_libx265_with_hvc1_tag():
    options = enc.video_encoder_options("ffmpeg", "cpu", crf=20, preset="medium", codec="hevc")

    assert options == [
        "-c:v", "libx265", "-preset", "medium", "-crf", "20", "-tag:v", "hvc1",
    ]


def test_hevc_hardware_encoders_map_names():
    nvenc = enc.video_encoder_options("ffmpeg", "nvenc", crf=20, preset="medium", codec="hevc")
    assert nvenc[:2] == ["-c:v", "hevc_nvenc"]
    assert nvenc[-2:] == ["-tag:v", "hvc1"]

    qsv = enc.video_encoder_options("ffmpeg", "qsv", crf=20, preset="medium", codec="hevc")
    assert qsv[:2] == ["-c:v", "hevc_qsv"]

    amf = enc.video_encoder_options("ffmpeg", "amf", crf=20, preset="medium", codec="hevc")
    assert amf[:2] == ["-c:v", "hevc_amf"]
    assert "-qp_b" not in amf  # hevc_amf 无 B 帧 QP 选项


def test_auto_hevc_picks_hevc_hardware_and_falls_back_to_x265(monkeypatch):
    monkeypatch.setattr(
        enc, "_available_encoders", lambda _ffmpeg_path: frozenset({"hevc_nvenc"})
    )
    options = enc.video_encoder_options("ffmpeg", "auto", crf=21, preset="slow", codec="hevc")
    assert options[:2] == ["-c:v", "hevc_nvenc"]

    monkeypatch.setattr(enc, "_available_encoders", lambda _ffmpeg_path: frozenset())
    fallback = enc.video_encoder_options("ffmpeg", "auto", crf=21, preset="slow", codec="hevc")
    assert fallback[:2] == ["-c:v", "libx265"]


def test_normalize_video_codec_falls_back_to_h264():
    assert enc.normalize_video_codec("hevc") == "hevc"
    assert enc.normalize_video_codec("av1") == "h264"
