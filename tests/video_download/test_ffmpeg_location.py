from __future__ import annotations

from pathlib import Path

import pytest

from krok_helper.settings import AppSettings
from krok_helper.video_download.download_task import DownloadOptions
from krok_helper.video_download.ytdlp_service import VideoDownloadError, YtDlpService


def test_python_backend_receives_configured_ffmpeg_location(tmp_path: Path, make_download_task) -> None:
    captured: dict[str, object] = {}

    class FakeYoutubeDL:
        def __init__(self, options: dict[str, object]) -> None:
            captured.update(options)

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def extract_info(self, _url: str, *, download: bool):
            assert download is True
            raise RuntimeError("stop after capturing options")

    ffmpeg_dir = tmp_path / "ffmpeg"
    service = YtDlpService(app_settings=AppSettings(ffmpeg_dir=str(ffmpeg_dir)))
    task = make_download_task()
    options = DownloadOptions(save_dir=str(tmp_path))

    with pytest.raises(VideoDownloadError):
        service._download_with_python_api(
            FakeYoutubeDL,
            task,
            options,
            lambda _payload: None,
            save_dir=tmp_path,
            output_stem="video",
            outtmpl=str(tmp_path / "video.%(ext)s"),
            selected_format="video+audio",
        )

    assert captured["ffmpeg_location"] == str(ffmpeg_dir)


def test_cli_backend_receives_configured_ffmpeg_location(
    tmp_path: Path,
    make_download_task,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeProcess:
        stdout: list[str] = []

        def wait(self, timeout=None) -> int:
            return 0

        def poll(self) -> int:
            return 0

        def terminate(self) -> None:
            pass

        def kill(self) -> None:
            pass

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return FakeProcess()

    ffmpeg_dir = tmp_path / "ffmpeg"
    service = YtDlpService(app_settings=AppSettings(ffmpeg_dir=str(ffmpeg_dir)))
    monkeypatch.setattr(service, "_find_ytdlp_cli", lambda: "yt-dlp")
    monkeypatch.setattr("krok_helper.video_download.ytdlp_service.subprocess.Popen", fake_popen)
    monkeypatch.setattr(service, "_resolve_output_file", lambda *_args, **_kwargs: None)

    service._download_with_cli(
        make_download_task(),
        DownloadOptions(save_dir=str(tmp_path)),
        lambda _payload: None,
        save_dir=tmp_path,
        output_stem="video",
        outtmpl=str(tmp_path / "video.%(ext)s"),
        selected_format="video+audio",
    )

    command = captured["command"]
    location_index = command.index("--ffmpeg-location")
    assert command[location_index + 1] == str(ffmpeg_dir)
