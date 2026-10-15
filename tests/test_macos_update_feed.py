from __future__ import annotations

import base64
import subprocess
import sys
from pathlib import Path

import pytest

from krok_helper.updater.macos_feed import (
    ASSET_NAME, MacFeed, macos_versions, parse_feed, render_feed, validate_ed_key,
)
from krok_helper.updater.sources import build_download_url


def make_feed():
    version = "4.3.7.3"
    return MacFeed(
        version, macos_versions(version)[1], 14,
        base64.b64encode(bytes(range(64))).decode(),
        build_download_url("github", f"v{version}", ASSET_NAME),
    )


def test_roundtrip_preserves_four_component_version():
    feed = make_feed()
    assert parse_feed(render_feed(feed), tag="v4.3.7.3", version="4.3.7.3") == feed
    assert feed.build_version == "4.3.7003"


@pytest.mark.parametrize("old,new", [
    (b"4.3.7003", b"4.3.7004"),
    (b"KaraokeStudio-macos.zip", b"KaraokeStudio-windows.zip"),
    (b'length="14"', b'length="0"'),
    (b"AAECAw", b"!AECAw"),
])
def test_rejects_wrong_version_platform_size_or_signature(old, new):
    with pytest.raises(ValueError):
        parse_feed(render_feed(make_feed()).replace(old, new), tag="v4.3.7.3", version="4.3.7.3")


def test_rejects_entities_and_unconfirmed_tag():
    for encoding in ("utf-8", "utf-16"):
        with pytest.raises(ValueError):
            parse_feed('<!DOCTYPE rss [<!ENTITY a "boom">]><rss/>'.encode(encoding), tag="v4.3.7.3", version="4.3.7.3")
    with pytest.raises(ValueError):
        parse_feed(render_feed(make_feed()), tag="v4.3.7.4", version="4.3.7.3")




def test_build_metadata_imports_without_site_packages():
    """Build/signing metadata must not pull in GUI or network dependencies."""
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-S", "-c", """
import sys
from krok_helper.updater.macos_feed import macos_versions, validate_ed_key
from scripts.update_macos_version import read_app_version
assert macos_versions(read_app_version())[1]
assert validate_ed_key('AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=')
assert not any(name.split('.')[0] in ('PyQt6', 'requests', 'qfluentwidgets') for name in sys.modules)
"""],
        cwd=root, text=True, capture_output=True,
    )
    assert result.returncode == 0, result.stderr
