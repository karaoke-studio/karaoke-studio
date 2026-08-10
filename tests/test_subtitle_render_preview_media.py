from __future__ import annotations

import subprocess
from pathlib import Path

from krok_helper.subtitle_render.frontend import preview_media


def test_qt_playback_source_remuxes_video_with_generated_pts(monkeypatch, tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"not really video")
    monkeypatch.setattr(preview_media, "_resolve_ffmpeg_path", lambda: "ffmpeg")
    proxy = tmp_path / "proxy.mp4"
    monkeypatch.setattr(preview_media, "_proxy_path_for", lambda _path: proxy)
    commands: list[list[str]] = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        Path(command[-1]).write_bytes(b"proxy")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(preview_media.subprocess, "run", fake_run)

    assert preview_media.qt_playback_source(source) == proxy
    assert commands
    command = commands[0]
    assert command[:4] == ["ffmpeg", "-y", "-hide_banner", "-loglevel"]
    assert "-fflags" in command
    assert "+genpts" in command
    assert command[command.index("-i") + 1] == str(source)
    assert "-avoid_negative_ts" in command


def test_qt_playback_source_falls_back_to_original_when_remux_fails(monkeypatch, tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"not really video")
    monkeypatch.setattr(preview_media, "_resolve_ffmpeg_path", lambda: "ffmpeg")
    monkeypatch.setattr(preview_media, "_proxy_path_for", lambda _path: tmp_path / "proxy.mp4")
    monkeypatch.setattr(
        preview_media.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 1, "", "bad"),
    )

    assert preview_media.qt_playback_source(source) == source


def test_low_quality_transcodes_a_cached_540p_preview_proxy(monkeypatch, tmp_path):
    source = tmp_path / "source.mkv"
    source.write_bytes(b"not really video")
    proxy = tmp_path / "proxy-540p.mp4"
    monkeypatch.setattr(preview_media, "_resolve_ffmpeg_path", lambda: "ffmpeg")
    monkeypatch.setattr(
        preview_media,
        "_scaled_proxy_path_for",
        lambda _path, quality: proxy,
    )
    commands: list[list[str]] = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        Path(command[-1]).write_bytes(b"proxy")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(preview_media.subprocess, "run", fake_run)

    assert preview_media.qt_playback_source(source, "low") == proxy
    command = commands[0]
    assert "-vf" in command
    assert "min(ih,540)" in command[command.index("-vf") + 1]
    assert command[command.index("-c:v") + 1] == "libx264"
    assert command[command.index("-c:a") + 1] == "aac"
    assert command[command.index("-fps_mode") + 1] == "passthrough"
    assert "-avoid_negative_ts" not in command


def test_scaled_preview_proxy_cache_key_tracks_quality_and_source_metadata(tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"video-v1")

    low = preview_media._scaled_proxy_path_for(source, "low")
    medium = preview_media._scaled_proxy_path_for(source, "medium")

    assert low != medium
    assert low.name.endswith("-540p.mp4")
    assert medium.name.endswith("-1080p.mp4")

    source.write_bytes(b"video-v2-with-a-different-size")
    assert preview_media._scaled_proxy_path_for(source, "low") != low
