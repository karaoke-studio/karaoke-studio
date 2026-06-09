from __future__ import annotations

from krok_helper.video_download.ytdlp_service import (
    YOUTUBE_DISABLE_COOKIE_HINT,
    YOUTUBE_FALLBACK_EXTRACTOR_ARGS,
    VideoDownloadError,
    YtDlpService,
)


YOUTUBE_URL = "https://www.youtube.com/watch?v=abc"
BILIBILI_URL = "https://www.bilibili.com/video/BV1abc"


def test_returns_true_for_youtube_bot_error() -> None:
    assert YtDlpService()._should_retry_youtube_with_fallback(YOUTUBE_URL, "not a bot") is True


def test_returns_true_for_empty_file_error() -> None:
    assert YtDlpService()._should_retry_youtube_with_fallback(YOUTUBE_URL, "downloaded file is empty") is True


def test_returns_true_for_youtube_unavailable_error() -> None:
    assert YtDlpService()._should_retry_youtube_with_fallback(YOUTUBE_URL, "This video is not available") is True


def test_returns_true_for_requested_format_unavailable_error() -> None:
    message = "Requested format is not available. Use --list-formats for a list of available formats"

    assert YtDlpService()._should_retry_youtube_with_fallback(YOUTUBE_URL, message) is True


def test_extract_retry_drops_cookie_when_cookie_breaks_youtube_formats(monkeypatch) -> None:
    service = YtDlpService()
    calls: list[tuple[str | None, str]] = []

    def fake_extract(_youtube_dl, url, cookie_file, *, extractor_args_hint="", allow_playlist=False):
        del _youtube_dl, url, allow_playlist
        calls.append((cookie_file, extractor_args_hint))
        if cookie_file:
            raise VideoDownloadError("当前清晰度不可用，请重新解析后选择其他格式。")
        return {"title": "ok", "duration": 1, "formats": []}

    monkeypatch.setattr(service, "_extract_info_with_python_api", fake_extract)
    monkeypatch.setattr(service, "_usable_cookie_file", lambda cookie_file: str(cookie_file or ""))

    raw_info, hint = service._extract_info_with_python_retry(object, YOUTUBE_URL, "cookies.txt")

    assert raw_info["title"] == "ok"
    assert hint == f"{YOUTUBE_FALLBACK_EXTRACTOR_ARGS}|{YOUTUBE_DISABLE_COOKIE_HINT}"
    assert calls == [
        ("cookies.txt", ""),
        (None, YOUTUBE_FALLBACK_EXTRACTOR_ARGS),
    ]


def test_returns_false_when_already_using_fallback_args() -> None:
    assert (
        YtDlpService()._should_retry_youtube_with_fallback(
            YOUTUBE_URL,
            "not a bot",
            extractor_args_hint=YOUTUBE_FALLBACK_EXTRACTOR_ARGS,
        )
        is False
    )


def test_returns_false_for_bilibili_url() -> None:
    assert YtDlpService()._should_retry_youtube_with_fallback(BILIBILI_URL, "not a bot") is False


def test_returns_false_for_unrelated_error() -> None:
    assert YtDlpService()._should_retry_youtube_with_fallback(YOUTUBE_URL, "unrelated failure") is False
