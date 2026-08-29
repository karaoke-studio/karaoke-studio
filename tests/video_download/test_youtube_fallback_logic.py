from __future__ import annotations

import pytest

from krok_helper.video_download.ytdlp_service import (
    YOUTUBE_FALLBACK_EXTRACTOR_ARGS,
    YOUTUBE_RELOAD_EXTRACTOR_ARGS,
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


def test_returns_true_for_youtube_http_403() -> None:
    assert YtDlpService()._should_retry_youtube_with_fallback(YOUTUBE_URL, "HTTP Error 403: Forbidden") is True


def test_youtube_fallback_uses_visionos_before_android_vr() -> None:
    args = YtDlpService()._build_python_extractor_args(YOUTUBE_FALLBACK_EXTRACTOR_ARGS)

    assert args == {"youtube": {"player_client": ["visionos", "android_vr", "web"]}}
    assert "tv" not in args["youtube"]["player_client"]


def test_youtube_reload_fallback_uses_cookie_compatible_clients() -> None:
    args = YtDlpService()._build_python_extractor_args(YOUTUBE_RELOAD_EXTRACTOR_ARGS)

    assert args == {"youtube": {"player_client": ["default", "web_embedded"]}}


def test_youtube_download_preserves_reload_client_hint(monkeypatch) -> None:
    service = YtDlpService()
    monkeypatch.setattr(service, "_ensure_youtube_visionos_client", lambda: True)

    assert service._youtube_download_extractor_args_hint(YOUTUBE_URL, "") == YOUTUBE_FALLBACK_EXTRACTOR_ARGS
    assert service._youtube_download_extractor_args_hint(
        YOUTUBE_URL,
        YOUTUBE_RELOAD_EXTRACTOR_ARGS,
    ) == YOUTUBE_RELOAD_EXTRACTOR_ARGS
    assert service._youtube_download_extractor_args_hint(BILIBILI_URL, "original") == "original"


def test_registers_visionos_client_for_stable_ytdlp() -> None:
    from yt_dlp.extractor.youtube._base import INNERTUBE_CLIENTS

    original = INNERTUBE_CLIENTS.pop("visionos", None)
    try:
        assert YtDlpService()._ensure_youtube_visionos_client() is True
        client = INNERTUBE_CLIENTS["visionos"]
        assert client["INNERTUBE_CONTEXT"]["client"]["clientName"] == "VISIONOS"
        assert client["INNERTUBE_CONTEXT_CLIENT_NAME"] == 101
        assert client["REQUIRE_JS_PLAYER"] is False
    finally:
        INNERTUBE_CLIENTS.pop("visionos", None)
        if original is not None:
            INNERTUBE_CLIENTS["visionos"] = original


def test_returns_true_for_youtube_reload_error() -> None:
    service = YtDlpService()

    assert service._should_retry_youtube_reload(YOUTUBE_URL, "The page needs to be reloaded.") is True
    assert service._should_retry_youtube_reload(YOUTUBE_URL, "Please reload this page.") is True
    normalized = service._normalize_error_message(Exception("The page needs to be reloaded."))
    assert service._should_retry_youtube_reload(YOUTUBE_URL, normalized) is True


def test_youtube_reload_retry_is_limited_to_first_client_profile() -> None:
    service = YtDlpService()

    assert (
        service._should_retry_youtube_reload(
            YOUTUBE_URL,
            "The page needs to be reloaded.",
            extractor_args_hint=YOUTUBE_RELOAD_EXTRACTOR_ARGS,
        )
        is False
    )
    assert service._should_retry_youtube_reload(BILIBILI_URL, "The page needs to be reloaded.") is False


def test_extract_reload_retry_preserves_cookie_and_client_hint(monkeypatch) -> None:
    service = YtDlpService()
    calls: list[tuple[str | None, str]] = []

    def fake_extract(_youtube_dl, url, cookie_file, *, extractor_args_hint="", allow_playlist=False):
        del _youtube_dl, url, allow_playlist
        calls.append((cookie_file, extractor_args_hint))
        if not extractor_args_hint:
            message = service._normalize_error_message(
                Exception("ERROR: [youtube] abc: The page needs to be reloaded.")
            )
            raise VideoDownloadError(message)
        return {"title": "ok", "duration": 1, "formats": []}

    monkeypatch.setattr(service, "_extract_info_with_python_api", fake_extract)

    raw_info, hint = service._extract_info_with_python_retry(object, YOUTUBE_URL, "cookies.txt")

    assert raw_info["title"] == "ok"
    assert hint == YOUTUBE_RELOAD_EXTRACTOR_ARGS
    assert calls == [
        ("cookies.txt", ""),
        ("cookies.txt", YOUTUBE_RELOAD_EXTRACTOR_ARGS),
    ]


def test_extract_generic_fallback_never_drops_cookie(monkeypatch) -> None:
    service = YtDlpService()
    calls: list[tuple[str | None, str]] = []

    def fake_extract(_youtube_dl, url, cookie_file, *, extractor_args_hint="", allow_playlist=False):
        del _youtube_dl, url, allow_playlist
        calls.append((cookie_file, extractor_args_hint))
        if not extractor_args_hint:
            raise VideoDownloadError("This video is not available")
        return {"title": "ok", "duration": 1, "formats": []}

    monkeypatch.setattr(service, "_extract_info_with_python_api", fake_extract)

    raw_info, hint = service._extract_info_with_python_retry(object, YOUTUBE_URL, "cookies.txt")

    assert raw_info["title"] == "ok"
    assert hint == YOUTUBE_FALLBACK_EXTRACTOR_ARGS
    assert calls == [
        ("cookies.txt", ""),
        ("cookies.txt", YOUTUBE_FALLBACK_EXTRACTOR_ARGS),
    ]


def test_extract_reload_retry_stops_after_one_client_switch(monkeypatch) -> None:
    service = YtDlpService()
    calls: list[tuple[str | None, str]] = []

    def fake_extract(_youtube_dl, url, cookie_file, *, extractor_args_hint="", allow_playlist=False):
        del _youtube_dl, url, allow_playlist
        calls.append((cookie_file, extractor_args_hint))
        raise VideoDownloadError("The page needs to be reloaded.")

    monkeypatch.setattr(service, "_extract_info_with_python_api", fake_extract)

    with pytest.raises(VideoDownloadError, match="page needs to be reloaded"):
        service._extract_info_with_python_retry(object, YOUTUBE_URL, "cookies.txt")

    assert calls == [
        ("cookies.txt", ""),
        ("cookies.txt", YOUTUBE_RELOAD_EXTRACTOR_ARGS),
    ]


def test_cli_reload_retry_preserves_cookie_and_client_hint(monkeypatch) -> None:
    service = YtDlpService()
    calls: list[tuple[str | None, str]] = []

    def fake_extract(url, cookie_file, *, extractor_args_hint="", allow_playlist=False):
        del url, allow_playlist
        calls.append((cookie_file, extractor_args_hint))
        if not extractor_args_hint:
            raise VideoDownloadError("The page needs to be reloaded.")
        return {"title": "ok", "duration": 1, "formats": []}

    monkeypatch.setattr(service, "_extract_info_with_cli", fake_extract)

    raw_info, hint = service._extract_info_with_cli_retry(YOUTUBE_URL, "cookies.txt")

    assert raw_info["title"] == "ok"
    assert hint == YOUTUBE_RELOAD_EXTRACTOR_ARGS
    assert calls == [
        ("cookies.txt", ""),
        ("cookies.txt", YOUTUBE_RELOAD_EXTRACTOR_ARGS),
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
