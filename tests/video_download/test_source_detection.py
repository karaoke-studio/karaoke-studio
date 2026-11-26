from __future__ import annotations

import pytest

from krok_helper.video_download.download_task import SOURCE_BILIBILI, SOURCE_UNKNOWN, SOURCE_YOUTUBE
from krok_helper.video_download.video_logic import normalize_video_url
from krok_helper.video_download.ytdlp_service import YtDlpService


def test_detects_bilibili_via_url() -> None:
    assert YtDlpService().detect_source("https://www.bilibili.com/video/BV1abc") == SOURCE_BILIBILI


def test_detects_youtube_via_url() -> None:
    assert YtDlpService().detect_source("https://www.youtube.com/watch?v=abc") == SOURCE_YOUTUBE


def test_detects_youtube_short_url() -> None:
    assert YtDlpService().detect_source("https://youtu.be/abc") == SOURCE_YOUTUBE


def test_detects_via_extractor_key() -> None:
    assert YtDlpService().detect_source("https://example.com/watch/abc", "BiliBili") == SOURCE_BILIBILI


def test_returns_unknown_for_unrelated_url() -> None:
    assert YtDlpService().detect_source("https://example.com/foo") == SOURCE_UNKNOWN


@pytest.mark.parametrize("prefix", ["BV", "bv", "Bv", "bV"])
def test_bare_bv_id_is_normalized_and_detected(prefix) -> None:
    value = f" \t{prefix}1tR5g6jE6Y\n"

    assert normalize_video_url(value) == "https://www.bilibili.com/video/BV1tR5g6jE6Y/"
    assert YtDlpService().detect_source(value) == SOURCE_BILIBILI


@pytest.mark.parametrize("value", [
    "https://www.bilibili.com/video/BV1tR5g6jE6Y/?p=2&spm_id_from=333.788",
    "https://www.youtube.com/watch?v=BV1tR5g6jE6Y",
    "https://youtu.be/abc",
    "BV1tR5g6jE6",
    "BV1tR5g6jE6Yx",
    "BV1tR5g6jE6_",
    "BV1tR5g6jE6中",
    "标题 BV1tR5g6jE6Y",
    "",
])
def test_other_input_is_not_rewritten(value) -> None:
    assert normalize_video_url(value) == value


def test_extract_infos_expands_bv_before_backend_and_keeps_canonical_url(monkeypatch) -> None:
    service = YtDlpService()
    calls = []

    def extract(url, cookie_file, *, allow_playlist):
        calls.append((url, cookie_file, allow_playlist))
        return {"title": "测试视频"}, ""

    monkeypatch.setattr(service, "_extract_info_with_best_backend", extract)

    infos = service.extract_infos("BV1tR5g6jE6Y", "bilibili_cookies.txt")

    canonical = "https://www.bilibili.com/video/BV1tR5g6jE6Y/"
    assert calls == [(canonical, "bilibili_cookies.txt", True)]
    assert len(infos) == 1
    assert infos[0].url == infos[0].webpage_url == canonical
    assert infos[0].source == SOURCE_BILIBILI


def _extract_multipart(monkeypatch, part_titles: list[str]) -> list[str]:
    service = YtDlpService()
    parent = "【直播回放】单人卡拉OK"
    entries = [
        {"title": part_title, "formats": [{"format_id": "1"}],
         "webpage_url": f"https://www.bilibili.com/video/BV1tR5g6jE6Y/?p={index}"}
        for index, part_title in enumerate(part_titles, 1)
    ]
    monkeypatch.setattr(
        service, "_extract_info_with_best_backend",
        lambda url, cookie_file, *, allow_playlist: ({"title": parent, "entries": entries}, ""),
    )
    return [info.title for info in service.extract_infos("BV1tR5g6jE6Y")]


def test_multipart_parts_sharing_parent_title_get_distinct_part_numbers(monkeypatch) -> None:
    titles = _extract_multipart(monkeypatch, ["【直播回放】单人卡拉OK", "【直播回放】单人卡拉OK"])

    assert titles == ["【直播回放】单人卡拉OK - P1", "【直播回放】单人卡拉OK - P2"]


def test_multipart_parts_with_own_titles_keep_part_prefix(monkeypatch) -> None:
    titles = _extract_multipart(monkeypatch, ["上半场", "下半场"])

    assert titles == ["【直播回放】单人卡拉OK - P1 上半场", "【直播回放】单人卡拉OK - P2 下半场"]


def test_multipart_parts_with_identical_own_titles_stay_distinct(monkeypatch) -> None:
    titles = _extract_multipart(monkeypatch, ["直播", "直播"])

    assert titles == ["【直播回放】单人卡拉OK - P1 直播", "【直播回放】单人卡拉OK - P2 直播"]
