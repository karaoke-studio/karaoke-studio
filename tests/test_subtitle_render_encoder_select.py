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

    assert options == ["-c:v", "libx264", "-preset", "veryfast", "-crf", "51"]
