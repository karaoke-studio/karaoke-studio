from __future__ import annotations

import pytest

from krok_helper.video_download.download_task import DownloadOptions
from krok_helper.video_download.ytdlp_service import (
    YOUTUBE_FALLBACK_EXTRACTOR_ARGS,
    YOUTUBE_RELOAD_EXTRACTOR_ARGS,
    VideoDownloadError,
    YtDlpService,
)


YOUTUBE_URL = "https://www.youtube.com/watch?v=abc"
BILIBILI_URL = "https://www.bilibili.com/video/BV1abc"


def _download_options(cookie_file: str = "") -> DownloadOptions:
    return DownloadOptions(save_dir=".", cookie_file=cookie_file)


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


def test_reload_profile_failure_can_advance_to_visionos_fallback() -> None:
    service = YtDlpService()
    message = service._normalize_error_message(Exception("The page needs to be reloaded."))

    assert (
        service._should_retry_youtube_with_fallback(
            YOUTUBE_URL,
            message,
            extractor_args_hint=YOUTUBE_RELOAD_EXTRACTOR_ARGS,
        )
        is True
    )


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


def test_extract_reload_retry_advances_to_visionos_without_dropping_cookie(monkeypatch) -> None:
    service = YtDlpService()
    calls: list[tuple[str | None, str]] = []

    def fake_extract(_youtube_dl, url, cookie_file, *, extractor_args_hint="", allow_playlist=False):
        del _youtube_dl, url, allow_playlist
        calls.append((cookie_file, extractor_args_hint))
        if extractor_args_hint != YOUTUBE_FALLBACK_EXTRACTOR_ARGS:
            raise VideoDownloadError("The page needs to be reloaded.")
        return {"title": "ok", "duration": 1, "formats": []}

    monkeypatch.setattr(service, "_extract_info_with_python_api", fake_extract)

    raw_info, hint = service._extract_info_with_python_retry(object, YOUTUBE_URL, "cookies.txt")

    assert raw_info["title"] == "ok"
    assert hint == YOUTUBE_FALLBACK_EXTRACTOR_ARGS
    assert calls == [
        ("cookies.txt", ""),
        ("cookies.txt", YOUTUBE_RELOAD_EXTRACTOR_ARGS),
        ("cookies.txt", YOUTUBE_FALLBACK_EXTRACTOR_ARGS),
    ]


def test_extract_reload_retry_stops_after_all_client_profiles_fail(monkeypatch) -> None:
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
        ("cookies.txt", YOUTUBE_FALLBACK_EXTRACTOR_ARGS),
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


def test_cli_reload_retry_advances_to_visionos_without_dropping_cookie(monkeypatch) -> None:
    service = YtDlpService()
    calls: list[tuple[str | None, str]] = []

    def fake_extract(url, cookie_file, *, extractor_args_hint="", allow_playlist=False):
        del url, allow_playlist
        calls.append((cookie_file, extractor_args_hint))
        if extractor_args_hint != YOUTUBE_FALLBACK_EXTRACTOR_ARGS:
            raise VideoDownloadError("The page needs to be reloaded.")
        return {"title": "ok", "duration": 1, "formats": []}

    monkeypatch.setattr(service, "_extract_info_with_cli", fake_extract)

    raw_info, hint = service._extract_info_with_cli_retry(YOUTUBE_URL, "cookies.txt")

    assert raw_info["title"] == "ok"
    assert hint == YOUTUBE_FALLBACK_EXTRACTOR_ARGS
    assert calls == [
        ("cookies.txt", ""),
        ("cookies.txt", YOUTUBE_RELOAD_EXTRACTOR_ARGS),
        ("cookies.txt", YOUTUBE_FALLBACK_EXTRACTOR_ARGS),
    ]


def test_python_download_reload_failure_advances_to_visionos(monkeypatch, tmp_path) -> None:
    service = YtDlpService()
    task = type("Task", (), {"url": YOUTUBE_URL})()
    calls: list[str] = []

    def fake_download(*args, extractor_args_hint="", **kwargs):
        del args, kwargs
        calls.append(extractor_args_hint)
        if extractor_args_hint == YOUTUBE_RELOAD_EXTRACTOR_ARGS:
            raise VideoDownloadError("The page needs to be reloaded.")

    monkeypatch.setattr(service, "_download_with_python_api", fake_download)

    service._download_with_python_retry(
        object,
        task,
        object(),
        lambda _progress: None,
        save_dir=tmp_path,
        output_stem="video",
        outtmpl=str(tmp_path / "video.%(ext)s"),
        selected_format="best",
        extractor_args_hint=YOUTUBE_RELOAD_EXTRACTOR_ARGS,
    )

    assert calls == [YOUTUBE_RELOAD_EXTRACTOR_ARGS, YOUTUBE_FALLBACK_EXTRACTOR_ARGS]


def test_cli_download_reload_failure_advances_to_visionos(monkeypatch, tmp_path) -> None:
    service = YtDlpService()
    task = type("Task", (), {"url": YOUTUBE_URL})()
    calls: list[str] = []

    def fake_download(*args, extractor_args_hint="", **kwargs):
        del args, kwargs
        calls.append(extractor_args_hint)
        if extractor_args_hint == YOUTUBE_RELOAD_EXTRACTOR_ARGS:
            raise VideoDownloadError("The page needs to be reloaded.")

    monkeypatch.setattr(service, "_download_with_cli", fake_download)

    service._download_with_cli_retry(
        task,
        object(),
        lambda _progress: None,
        save_dir=tmp_path,
        output_stem="video",
        outtmpl=str(tmp_path / "video.%(ext)s"),
        selected_format="best",
        extractor_args_hint=YOUTUBE_RELOAD_EXTRACTOR_ARGS,
    )

    assert calls == [YOUTUBE_RELOAD_EXTRACTOR_ARGS, YOUTUBE_FALLBACK_EXTRACTOR_ARGS]


def test_python_download_403_clears_partial_and_retries_without_cookie(monkeypatch, tmp_path) -> None:
    service = YtDlpService()
    task = type("Task", (), {"url": YOUTUBE_URL})()
    options = _download_options("cookies.txt")
    part = tmp_path / "video.f315.webm.part"
    part.write_bytes(b"stale")
    calls: list[tuple[str, bool]] = []

    def fake_download(_youtube_dl, _task, current_options, _progress, **kwargs):
        del _youtube_dl, _task, _progress, kwargs
        calls.append((current_options.cookie_file, part.exists()))
        if current_options.cookie_file:
            raise VideoDownloadError("YouTube 视频流访问被拒绝")

    monkeypatch.setattr(service, "_usable_cookie_file", lambda cookie_file: str(cookie_file or ""))
    monkeypatch.setattr(service, "_download_with_python_api", fake_download)

    service._download_with_python_retry(
        object,
        task,
        options,
        lambda _progress: None,
        save_dir=tmp_path,
        output_stem="video",
        outtmpl=str(tmp_path / "video.%(ext)s"),
        selected_format="315+140",
        extractor_args_hint=YOUTUBE_FALLBACK_EXTRACTOR_ARGS,
    )

    assert calls == [("cookies.txt", True), ("", False)]


def test_cli_download_fallback_403_clears_partial_and_retries_without_cookie(monkeypatch, tmp_path) -> None:
    service = YtDlpService()
    task = type("Task", (), {"url": YOUTUBE_URL})()
    options = _download_options("cookies.txt")
    part = tmp_path / "video.f315.webm.part"
    part.write_bytes(b"stale")
    calls: list[tuple[str, str, bool]] = []

    def fake_download(_task, current_options, _progress, *, extractor_args_hint="", **kwargs):
        del _task, _progress, kwargs
        calls.append((current_options.cookie_file, extractor_args_hint, part.exists()))
        if len(calls) <= 2:
            raise VideoDownloadError("HTTP Error 403: Forbidden")

    monkeypatch.setattr(service, "_usable_cookie_file", lambda cookie_file: str(cookie_file or ""))
    monkeypatch.setattr(service, "_download_with_cli", fake_download)

    service._download_with_cli_retry(
        task,
        options,
        lambda _progress: None,
        save_dir=tmp_path,
        output_stem="video",
        outtmpl=str(tmp_path / "video.%(ext)s"),
        selected_format="315+140",
        extractor_args_hint=YOUTUBE_RELOAD_EXTRACTOR_ARGS,
    )

    # 换 fallback extractor args、以及换「无 cookie」重试之前都会先清掉上一轮
    # 残留分片（0ba88c9e「clean fragments before fallback」），所以只有首次
    # 调用还能看到测试预埋的 stale part。
    assert calls == [
        ("cookies.txt", YOUTUBE_RELOAD_EXTRACTOR_ARGS, True),
        ("cookies.txt", YOUTUBE_FALLBACK_EXTRACTOR_ARGS, False),
        ("", YOUTUBE_FALLBACK_EXTRACTOR_ARGS, False),
    ]


def test_youtube_cookie_retry_requires_403_and_usable_cookie(monkeypatch) -> None:
    service = YtDlpService()
    monkeypatch.setattr(service, "_usable_cookie_file", lambda cookie_file: str(cookie_file or ""))

    assert service._should_retry_youtube_without_cookies(
        YOUTUBE_URL, _download_options("cookies.txt"), "HTTP Error 403: Forbidden"
    )
    assert not service._should_retry_youtube_without_cookies(
        YOUTUBE_URL, _download_options(), "HTTP Error 403: Forbidden"
    )
    assert not service._should_retry_youtube_without_cookies(
        YOUTUBE_URL, _download_options("cookies.txt"), "network timeout"
    )
    assert not service._should_retry_youtube_without_cookies(
        BILIBILI_URL, _download_options("cookies.txt"), "HTTP Error 403: Forbidden"
    )


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
