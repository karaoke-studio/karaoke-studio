"""更新源排序与 URL 契约的独立测试。"""

import pytest

from krok_helper.updater.sources import (
    DEFAULT_ORDER,
    REPO_NAME,
    REPO_OWNER,
    SOURCE_IDS,
    SOURCE_LABELS,
    build_api_list_urls,
    build_api_urls,
    build_download_url,
    build_release_urls,
    normalize_order,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ([], list(DEFAULT_ORDER)),
        (["gh-proxy", "github"], ["gh-proxy", "github", "ghproxy", "ghproxy-net"]),
        (["bad", "github", "github"], list(DEFAULT_ORDER)),
    ],
)
def test_normalize_order(value, expected):
    assert normalize_order(value) == expected


def test_download_urls_preserve_published_asset_contract():
    direct = build_download_url("github", "v3.2.0", "KaraokeStudio-windows.zip")
    assert direct == (
        f"https://github.com/{REPO_OWNER}/{REPO_NAME}/releases/download/"
        "v3.2.0/KaraokeStudio-windows.zip"
    )
    for source in SOURCE_IDS[1:]:
        url = build_download_url(source, "v3.2.0", "KaraokeStudio-windows.zip")
        assert "https://github.com/" in url
        assert url.endswith("/v3.2.0/KaraokeStudio-windows.zip")
    assert build_download_url("ghproxy", "v3.2.0", "x.zip").startswith(
        "https://ghfast.top/https://github.com/"
    )


def test_unknown_download_source_raises():
    with pytest.raises(ValueError):
        build_download_url("retired", "v3.2.0", "x.zip")  # type: ignore[arg-type]


def test_release_urls_follow_normalized_user_order():
    urls = build_release_urls(["ghproxy-net", "github"], "v3.2.0", "x.zip")
    assert [source for source, _url in urls] == ["ghproxy-net", "github", "ghproxy", "gh-proxy"]


def test_api_urls_cover_all_sources_and_list_uses_100_items():
    latest = build_api_urls(SOURCE_IDS)
    listing = build_api_list_urls(SOURCE_IDS)
    assert [source for source, _url in latest] == list(SOURCE_IDS)
    assert [source for source, _url in listing] == list(SOURCE_IDS)
    assert all("releases/latest" in url for _source, url in latest)
    assert all("releases?per_page=100" in url for _source, url in listing)
    assert latest[1][1].startswith("https://ghfast.top/https://api.github.com/")
    assert all("mirror.ghproxy.com" not in url for _source, url in [*latest, *listing])


def test_every_source_has_chinese_label():
    assert all(SOURCE_LABELS[source] for source in SOURCE_IDS)
