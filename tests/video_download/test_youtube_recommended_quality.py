from __future__ import annotations

from krok_helper.video_download.download_task import SOURCE_YOUTUBE
from krok_helper.video_download.ytdlp_service import YtDlpService


def test_youtube_recommended_downloads_same_best_format_shown_in_ui() -> None:
    service = YtDlpService()

    info = service._build_video_info(
        {
            "title": "Video",
            "duration": 1,
            "extractor_key": "Youtube",
            "formats": [
                {
                    "format_id": "251",
                    "ext": "webm",
                    "vcodec": "none",
                    "acodec": "opus",
                    "abr": 133,
                    "tbr": 133,
                    "filesize": 3_636_523,
                },
                {
                    "format_id": "140",
                    "ext": "m4a",
                    "vcodec": "none",
                    "acodec": "mp4a.40.2",
                    "abr": 129,
                    "tbr": 129,
                    "filesize": 2_504_695,
                },
                {
                    "format_id": "299",
                    "ext": "mp4",
                    "width": 1920,
                    "height": 864,
                    "fps": 60,
                    "vcodec": "avc1.64002a",
                    "acodec": "none",
                    "format_note": "1080p60",
                    "tbr": 2787,
                    "filesize": 53_885_235,
                },
                {
                    "format_id": "401",
                    "ext": "mp4",
                    "width": 3840,
                    "height": 1728,
                    "fps": 60,
                    "vcodec": "av01.0.13M.08",
                    "acodec": "none",
                    "format_note": "2160p60",
                    "tbr": 7677,
                    "filesize": 148_396_650,
                },
                {
                    "format_id": "315",
                    "ext": "webm",
                    "width": 3840,
                    "height": 1728,
                    "fps": 60,
                    "vcodec": "vp9",
                    "acodec": "none",
                    "format_note": "2160p60",
                    "tbr": 17995,
                    "filesize": 347_869_871,
                },
            ],
        },
        "https://www.youtube.com/watch?v=YpajI_fnrsI",
        "",
    )

    recommended = info.formats[0]

    assert info.source == SOURCE_YOUTUBE
    assert recommended.is_recommended is True
    assert recommended.resolution == "2160p"
    assert recommended.video_codec == "vp9"
    assert recommended.audio_codec == "mp4a"
    assert recommended.download_format == "315+140"
    assert recommended.filesize == 350_374_566
    assert recommended.requires_merge is True


def test_cli_backend_is_preferred_only_when_cli_is_newer(monkeypatch) -> None:
    service = YtDlpService()
    monkeypatch.setattr(service, "_find_ytdlp_cli_or_none", lambda: "yt-dlp")
    monkeypatch.setattr(service, "_python_ytdlp_version", lambda: "2026.03.17")
    monkeypatch.setattr(service, "_read_ytdlp_cli_version", lambda _cli: "2026.06.09")

    assert service._should_prefer_cli_backend("https://www.youtube.com/watch?v=abc") is True
    assert service._should_prefer_cli_backend("https://www.bilibili.com/video/BV1abc") is False


def test_cli_backend_is_not_preferred_when_cli_is_older(monkeypatch) -> None:
    service = YtDlpService()
    monkeypatch.setattr(service, "_find_ytdlp_cli_or_none", lambda: "yt-dlp")
    monkeypatch.setattr(service, "_python_ytdlp_version", lambda: "2026.06.09")
    monkeypatch.setattr(service, "_read_ytdlp_cli_version", lambda _cli: "2026.03.17")

    assert service._should_prefer_cli_backend("https://www.youtube.com/watch?v=abc") is False
