"""封面挑选：打包里的 Qt 只带 ico / jpeg / svg 三个图像插件。

YouTube 2026-08 起把 ``info["thumbnail"]`` 给成 ``vi_webp/…​.webp``，Qt 解不开，
``QPixmap`` 拿到空图，界面上封面直接消失（日志里是
``QPixmap::scaled: Pixmap is a null pixmap``）。
"""

from __future__ import annotations

from krok_helper.video_download.ytdlp_service import YtDlpService


def test_webp_cover_falls_back_to_the_largest_jpg() -> None:
    service = YtDlpService()

    url = service._pick_thumbnail_url(
        {
            "thumbnail": "https://i.ytimg.com/vi_webp/ID/maxresdefault.webp",
            "thumbnails": [
                {"url": "https://i.ytimg.com/vi/ID/hqdefault.jpg", "width": 480, "preference": 0},
                {"url": "https://i.ytimg.com/vi_webp/ID/maxresdefault.webp", "width": 1920, "preference": 1},
                {"url": "https://i.ytimg.com/vi/ID/maxresdefault.jpg", "width": 1920, "preference": 0},
            ],
        }
    )

    assert url == "https://i.ytimg.com/vi/ID/maxresdefault.jpg"


def test_decodable_primary_thumbnail_is_kept() -> None:
    service = YtDlpService()

    url = service._pick_thumbnail_url(
        {
            "thumbnail": "https://i.ytimg.com/vi/ID/maxresdefault.jpg",
            "thumbnails": [{"url": "https://i.ytimg.com/vi/ID/hqdefault.jpg", "width": 480}],
        }
    )

    assert url == "https://i.ytimg.com/vi/ID/maxresdefault.jpg"


def test_query_string_does_not_hide_the_extension() -> None:
    service = YtDlpService()

    url = service._pick_thumbnail_url(
        {
            "thumbnail": "https://i.ytimg.com/vi_webp/ID/maxresdefault.webp?sqp=abc&rs=def",
            "thumbnails": [{"url": "https://i.ytimg.com/vi/ID/maxresdefault.jpg", "width": 1920}],
        }
    )

    assert url.endswith(".jpg")


def test_webp_only_source_keeps_the_original_url() -> None:
    """一张 jpg 都没有时不要把封面弄丢 —— 交出去，让下游自己试。"""
    service = YtDlpService()

    url = service._pick_thumbnail_url(
        {
            "thumbnail": "https://example.com/cover.webp",
            "thumbnails": [{"url": "https://example.com/cover.webp"}],
        }
    )

    assert url == "https://example.com/cover.webp"
