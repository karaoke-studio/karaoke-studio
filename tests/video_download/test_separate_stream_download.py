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
        # 模拟后端已落盘产物：视频轨用原名，音轨带「_音频轨」后缀。
        ext_by_format = {"137": "mp4", "140": "m4a", "137+140": "mp4", "best": "mp4"}
        ext = ext_by_format.get(kwargs["selected_format"], "mp4")
        path = kwargs["save_dir"] / f"{kwargs['output_stem']}.{ext}"
        path.write_bytes(b"x")
        _task.local_file = path

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

    assert [call["selected_format"] for call in calls] == ["137", "140"]
    video_stem = calls[0]["output_stem"]
    assert calls[1]["output_stem"] == f"{video_stem}_音频轨"
    assert task.local_file == tmp_path / f"{video_stem}.mp4"
    assert task.audio_file == tmp_path / f"{video_stem}_音频轨.m4a"


def test_split_audio_pass_skips_thumbnail_and_subtitle(recording_service, tmp_path, make_download_task) -> None:
    service, calls = recording_service
    task = make_download_task()
    task.selected_format = _split_format_option()
    task.merge_video_audio = False

    _run_download(
        service, tmp_path, task,
        _options(save_dir=str(tmp_path), merge_video_audio=False, download_thumbnail=True, download_subtitle=True),
    )

    assert calls[0]["options"].download_thumbnail is True
    assert calls[1]["options"].download_thumbnail is False
    assert calls[1]["options"].download_subtitle is False


def test_merge_enabled_keeps_single_combined_download(recording_service, tmp_path, make_download_task) -> None:
    service, calls = recording_service
    task = make_download_task()
    task.selected_format = _split_format_option()
    task.merge_video_audio = True

    _run_download(service, tmp_path, task, _options(save_dir=str(tmp_path), merge_video_audio=True))

    assert [call["selected_format"] for call in calls] == ["137+140"]
    assert task.audio_file is None
    assert task.local_file is not None and task.local_file.name.endswith(".mp4")


def test_split_download_video_error_aborts_before_audio(recording_service, tmp_path, monkeypatch, make_download_task) -> None:
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

    assert [call["selected_format"] for call in calls] == ["137"]
    assert task.audio_file is None
