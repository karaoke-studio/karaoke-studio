"""GitHub Release JSON 与 KS 资产选择契约的独立测试。"""

import sys

from krok_helper.updater.worker import _parse_release


def _payload():
    return {
        "tag_name": "v3.2.0",
        "name": "Karaoke Studio 3.2.0",
        "body": "### 修复\n- 更新器修复",
        "html_url": "https://github.com/karaoke-studio/karaoke-studio/releases/tag/v3.2.0",
        "prerelease": False,
        "published_at": "2026-07-12T00:00:00Z",
        "assets": [
            {"name": "KaraokeStudio-windows.zip", "size": 100, "browser_download_url": "https://x/win"},
            {"name": "KaraokeStudio-macos.zip", "size": 90, "browser_download_url": "https://x/mac"},
            {"name": "KaraokeStudio-windows.json", "size": 10, "browser_download_url": "https://x/json"},
            {"name": "", "size": 1, "browser_download_url": "https://x/ignored"},
        ],
    }


def test_parse_release_fields_and_assets():
    release = _parse_release(_payload())
    assert release.tag == "v3.2.0"
    assert release.version == "3.2.0"
    assert "更新器修复" in release.body
    assert release.prerelease is False
    assert len(release.assets) == 3


def test_primary_asset_prefers_exact_published_name():
    release = _parse_release(_payload())
    assert release.pick_primary_asset("KaraokeStudio-windows.zip").download_url == "https://x/win"


def test_primary_asset_platform_fallback(monkeypatch):
    release = _parse_release(_payload())
    monkeypatch.setattr(sys, "platform", "darwin")
    assert release.pick_primary_asset("missing.zip").name == "KaraokeStudio-macos.zip"


def test_release_name_falls_back_to_tag():
    payload = _payload()
    payload["name"] = ""
    assert _parse_release(payload).name == "v3.2.0"
