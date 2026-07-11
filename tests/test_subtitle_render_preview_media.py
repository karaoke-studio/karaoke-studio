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
