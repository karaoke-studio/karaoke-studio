"""Shared macOS release metadata; no Qt or native dependencies."""

from __future__ import annotations

import base64
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from krok_helper.updater.sources import build_download_url
from krok_helper.updater.macos_metadata import validate_ed_key

ASSET_NAME = "KaraokeStudio-macos.zip"
FEED_NAME = "KaraokeStudio-macos-appcast.xml"
SPARKLE_NS = "http://www.andymatuschak.org/xml-namespaces/sparkle"
MAX_FEED_SIZE = 1024 * 1024


def macos_versions(version: str) -> tuple[str, str]:
    match = re.fullmatch(r"([0-9]+)\.([0-9]+)\.([0-9]+)(?:\.([0-9]+))?", version)
    if not match:
        raise ValueError(f"Invalid APP_VERSION: {version}")
    major, minor, patch = (int(part) for part in match.groups()[:3])
    revision = int(match.group(4) or 0)
    if revision >= 1000:
        raise ValueError("The fourth APP_VERSION component must be between 0 and 999.")
    return f"{major}.{minor}.{patch}", f"{major}.{minor}.{patch * 1000 + revision}"




@dataclass(frozen=True)
class MacDelta:
    from_version: str
    length: int
    signature: str
    offset: int = 0
    sha256: str = ""


@dataclass(frozen=True)
class MacFeed:
    version: str
    build_version: str
    length: int
    signature: str
    archive_url: str
    deltas: tuple[MacDelta, ...] = ()

    @classmethod
    def from_dict(cls, value: dict) -> MacFeed:
        return cls(**{**value, "deltas": tuple(MacDelta(**d) for d in value.get("deltas", ()))})


def parse_feed(data: bytes, *, tag: str, version: str) -> MacFeed:
    """Accept only the full archive for the release already confirmed by the user.

    Mirror metadata is untrusted. Sparkle still verifies the archive's Ed25519
    signature with the key in the installed application's Info.plist.
    """
    _, build = macos_versions(version)
    if tag != f"v{version}":
        raise ValueError("更新标签与版本号不一致。")
    if len(data) > MAX_FEED_SIZE:
        raise ValueError("macOS 更新清单过大或含不支持的 XML 声明。")
    # UTF-8 only, so UTF-16 cannot conceal a DTD/entity declaration from this guard.
    text = data.decode("utf-8")
    if "<!" in text:
        raise ValueError("macOS 更新清单含不支持的 XML 声明。")
    root = ET.fromstring(text)
    items = root.findall("./channel/item")
    if root.tag != "rss" or len(items) != 1:
        raise ValueError("macOS 更新清单必须只包含本次更新。")
    item = items[0]
    enclosure = item.find("enclosure")
    if enclosure is None or item.findtext(f"{{{SPARKLE_NS}}}version") != build:
        raise ValueError("macOS 更新清单的构建版本不匹配。")
    if item.findtext(f"{{{SPARKLE_NS}}}shortVersionString") != version:
        raise ValueError("macOS 更新清单的显示版本不匹配。")
    url = enclosure.get("url", "")
    if url != build_download_url("github", tag, ASSET_NAME):
        raise ValueError("macOS 更新清单指向了其他更新包。")
    signature = enclosure.get(f"{{{SPARKLE_NS}}}edSignature", "")
    try:
        if len(base64.b64decode(signature, validate=True)) != 64:
            raise ValueError("signature length")
        length = int(enclosure.get("length", "0"))
        if length <= 0:
            raise ValueError("archive length")
    except ValueError as exc:
        raise ValueError("macOS 更新清单缺少有效的签名或文件大小。") from exc
    # Rebuild a minimal feed: do not let a mirror add install-on-quit, package
    # installers, release-notes downloads or other Sparkle policy overrides.
    return MacFeed(version, build, length, signature, url)


def render_feed(feed: MacFeed, *, delta: MacDelta | None = None, delta_url: str = "") -> bytes:
    ET.register_namespace("sparkle", SPARKLE_NS)
    root = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(root, "channel")
    ET.SubElement(channel, "title").text = "Lin-K Lyrics 更新"
    item = ET.SubElement(channel, "item")
    ET.SubElement(item, "title").text = f"Lin-K Lyrics {feed.version}"
    ET.SubElement(item, f"{{{SPARKLE_NS}}}version").text = feed.build_version
    ET.SubElement(item, f"{{{SPARKLE_NS}}}shortVersionString").text = feed.version
    ET.SubElement(item, "enclosure", {
        "url": feed.archive_url, "length": str(feed.length),
        "type": "application/octet-stream",
        f"{{{SPARKLE_NS}}}edSignature": feed.signature,
    })
    if delta is not None:
        ET.SubElement(ET.SubElement(item, f"{{{SPARKLE_NS}}}deltas"), "enclosure", {
            "url": delta_url, "length": str(delta.length),
            "type": "application/octet-stream",
            f"{{{SPARKLE_NS}}}deltaFrom": macos_versions(delta.from_version)[1],
            f"{{{SPARKLE_NS}}}edSignature": delta.signature,
        })
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)
