from __future__ import annotations

import pytest

from krok_helper.video_download import ytdlp_service
from krok_helper.video_download.download_task import DownloadOptions, FormatOption
from krok_helper.video_download.ytdlp_service import YtDlpService


def _service() -> YtDlpService:
    return YtDlpService()


def _split_format_option() -> FormatOption:
    return FormatOption(
        option_id="137:1080p",
        download_format="137+140",
        format_label="MP4 + 音频",
        resolution="1080p",
        video_codec="h264",
        audio_codec="m4a",
        ext="mp4",
        requires_merge=True,
    )


def _options(**overrides) -> DownloadOptions:
    base = {"save_dir": "out", "retry_count": 3}
    base.update(overrides)
    return DownloadOptions(**base)


@pytest.fixture
def recording_service(monkeypatch):
    service = _service()
    calls = []

    def attempt(_ydl, _task, options, _callback, **kwargs):
        calls.append({"options": options, **kwargs})
        # yt-dlp's comma selector produces independent files in one extraction.
        ext_by_format = {"137": "mp4", "140": "m4a", "137+140": "mp4", "best": "mp4"}
        paths = []
        for format_id in kwargs["selected_format"].split(","):
            ext = ext_by_format.get(format_id, "mp4")
            path = kwargs["save_dir"] / (
                f"{kwargs['output_stem']}.f{format_id}.{ext}"
                if "," in kwargs["selected_format"] else f"{kwargs['output_stem']}.{ext}"
            )
            path.write_bytes(b"x")
            paths.append(path)
        _task.local_file = paths[0]

    monkeypatch.setattr(service, "_import_ytdlp", lambda: object)
    monkeypatch.setattr(service, "_should_prefer_cli_backend", lambda _url: False)
    monkeypatch.setattr(service, "_download_with_python_retry", attempt)
    monkeypatch.setattr(ytdlp_service.time, "sleep", lambda _seconds: None)
    clock = iter(range(100))
    monkeypatch.setattr(ytdlp_service.time, "monotonic", lambda: next(clock))
    return service, calls


def _run_download(service, tmp_path, task, options):
    service.download(task, options, lambda _payload: None)


def test_merge_disabled_downloads_two_separate_streams(
    tmp_path, monkeypatch, make_download_task, recording_service
) -> None:
    service, calls = recording_service
    task = make_download_task()
    task.selected_format = _split_format_option()
    task.merge_video_audio = False

    _run_download(service, tmp_path, task, _options(save_dir=str(tmp_path), merge_video_audio=False))

    assert [call["selected_format"] for call in calls] == ["137,140"]
    video_stem = calls[0]["output_stem"]
    assert calls[0]["outtmpl"] == str(tmp_path / f"{video_stem}.f%(format_id)s.%(ext)s")
    assert task.local_file == tmp_path / f"{video_stem}.f137.mp4"
    assert (tmp_path / f"{video_stem}.f140.m4a").is_file()
    assert len(list(tmp_path.iterdir())) == 2


def test_split_download_requests_thumbnail_and_subtitle_once(recording_service, tmp_path, make_download_task) -> None:
    service, calls = recording_service
    task = make_download_task()
    task.selected_format = _split_format_option()
    task.merge_video_audio = False

    _run_download(
        service, tmp_path, task,
        _options(save_dir=str(tmp_path), merge_video_audio=False, download_thumbnail=True, download_subtitle=True),
    )

    assert len(calls) == 1
    assert calls[0]["options"].download_thumbnail is True
    assert calls[0]["options"].download_subtitle is True


def test_merge_enabled_keeps_single_combined_download(recording_service, tmp_path, make_download_task) -> None:
    service, calls = recording_service
    task = make_download_task()
    task.selected_format = _split_format_option()
    task.merge_video_audio = True

    _run_download(service, tmp_path, task, _options(save_dir=str(tmp_path), merge_video_audio=True))

    assert [call["selected_format"] for call in calls] == ["137+140"]
    assert task.audio_file is None
    assert task.local_file is not None and task.local_file.name.endswith(".mp4")


def test_split_download_error_leaves_no_output(recording_service, tmp_path, monkeypatch, make_download_task) -> None:
    service, calls = recording_service
    task = make_download_task()
    task.selected_format = _split_format_option()
    task.merge_video_audio = False

    def failing_attempt(_ydl, _task, _options, _callback, **kwargs):
        calls.append(kwargs)
        raise ytdlp_service.VideoDownloadError("Connection reset by peer")

    monkeypatch.setattr(service, "_download_with_python_retry", failing_attempt)
    with pytest.raises(ytdlp_service.VideoDownloadError):
        _run_download(service, tmp_path, task, _options(save_dir=str(tmp_path), merge_video_audio=False, retry_count=0))

    assert [call["selected_format"] for call in calls] == ["137,140"]
    assert task.local_file is None
    assert task.audio_file is None
    assert not list(tmp_path.iterdir())
