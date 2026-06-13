from __future__ import annotations

from krok_helper.video_download.download_task import SOURCE_YOUTUBE
from krok_helper.video_download.ytdlp_service import YOUTUBE_RECOMMENDED_DOWNLOAD_FORMAT, YtDlpService


def test_youtube_recommended_prefers_h264_video_and_m4a_audio() -> None:
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
                    "filesize": 3_542_951,
                },
                {
                    "format_id": "399",
                    "ext": "mp4",
                    "width": 1920,
                    "height": 1080,
                    "vcodec": "av01.0.08M.08",
                    "acodec": "none",
                    "tbr": 563,
                    "filesize": 15_403_745,
                },
                {
                    "format_id": "137",
                    "ext": "mp4",
                    "width": 1920,
                    "height": 1080,
                    "vcodec": "avc1.640028",
                    "acodec": "none",
                    "tbr": 581,
                    "filesize": 15_904_872,
                },
            ],
        },
        "https://www.youtube.com/watch?v=0wGvSmOMeNU",
        "",
    )

    recommended = info.formats[0]

    assert info.source == SOURCE_YOUTUBE
    assert recommended.is_recommended is True
    assert recommended.resolution == "1080p"
    assert recommended.video_codec == "avc1"
    assert recommended.audio_codec == "mp4a"
    assert recommended.download_format == YOUTUBE_RECOMMENDED_DOWNLOAD_FORMAT
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
