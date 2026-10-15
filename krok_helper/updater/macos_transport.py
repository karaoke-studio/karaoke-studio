"""Download using the existing proxy/source settings, then serve a fixed cache."""

from __future__ import annotations

import hashlib
import secrets
import tempfile
import threading
from dataclasses import asdict, dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Callable

from krok_helper.network import requests_session_for_proxy
from krok_helper.updater import http_client
from krok_helper.updater.macos_feed import (
    ASSET_NAME, DELTA_ASSET_NAME, FEED_NAME, MAX_FEED_SIZE, MacDelta, MacFeed, macos_versions, parse_feed, render_feed,
)
from krok_helper.updater.sources import build_release_urls

if TYPE_CHECKING:
    from krok_helper.updater.settings import UpdaterSettings
    from krok_helper.updater.worker import CheckResult


class UpdateCancelled(Exception):
    pass


@dataclass
class PreparedMacUpdate:
    temporary: tempfile.TemporaryDirectory | None
    archive: Path
    feed: MacFeed
    delta: MacDelta | None = None
    delta_archive: Path | None = None

    def close(self) -> None:
        if self.temporary is not None:
            self.temporary.cleanup()


def prepare_update(
    result: CheckResult,
    settings: UpdaterSettings,
    *,
    cancelled: Callable[[], bool],
    progress: Callable[[str], None],
    cache_dir: Path | None = None,
    feed_candidates: list[tuple[str, str]] | None = None,
    feed: MacFeed | None = None,
    current_build: str = "",
    delta_candidates: list[tuple[str, str]] | None = None,
) -> PreparedMacUpdate:
    release = result.release
    if release is None or result.primary_asset_name != ASSET_NAME:
        raise ValueError("本次发布没有可用于 macOS 自动更新的完整包。")
    # A supervised download process writes into a directory owned by its host.
    temporary = tempfile.TemporaryDirectory(prefix="Lin-K-macos-update-") if cache_dir is None else None
    archive = (Path(temporary.name) if temporary else cache_dir) / ASSET_NAME
    session, proxies = requests_session_for_proxy(settings.proxy_mode, settings.proxy_manual_url)

    def check_cancel():
        if cancelled():
            raise UpdateCancelled()

    errors = []
    try:
        feeds = feed_candidates if feed_candidates is not None else build_release_urls(settings.source_order, release.tag, FEED_NAME)
        for source, url in ([] if feed is not None else feeds):
            check_cancel()
            progress(f"正在获取 macOS 更新签名（{source}）…")
            try:
                with session.get(url, proxies=proxies, stream=True, timeout=http_client.DEFAULT_TIMEOUT) as response:
                    response.raise_for_status()
                    data = bytearray()
                    for chunk in response.iter_content(16384):
                        check_cancel()
                        data.extend(chunk)
                        if len(data) > MAX_FEED_SIZE:
                            raise ValueError("更新清单过大。")
                feed = parse_feed(bytes(data), tag=release.tag, version=release.version)
                break
            except UpdateCancelled:
                raise
            except Exception as exc:
                errors.append(f"{source}: {exc}")
        if feed is None:
            raise ValueError("无法获取 macOS 更新签名，请稍后重试。\n" + "\n".join(errors))

        delta = next((d for d in feed.deltas if macos_versions(d.from_version)[1] == current_build), None)
        if delta is not None:
            patch = archive.with_name("update.delta")
            candidates = delta_candidates if delta_candidates is not None else build_release_urls(settings.source_order, release.tag, DELTA_ASSET_NAME)
            for source, url in candidates:
                check_cancel()
                progress(f"正在连接增量更新源（{source}）…")
                downloaded = http_client.download_range(
                    url, str(patch), offset=delta.offset, length=delta.length,
                    resource_size=feed.delta_container_length, session=session, proxies=proxies,
                    progress_cb=lambda done, total: progress(f"正在下载增量包：{min(100, done * 100 // total)}%"),
                    cancel_check=cancelled,
                )
                check_cancel()
                if downloaded.ok:
                    with patch.open("rb") as stream:
                        digest = hashlib.file_digest(stream, "sha256").hexdigest()
                    check_cancel()
                    if digest == delta.sha256:
                        return PreparedMacUpdate(temporary, archive, feed, delta, patch)
            patch.unlink(missing_ok=True)
            progress("增量包下载不可用，正在改用完整包…")

        def download_progress(done, total):
            progress(f"正在下载更新包：{min(100, done * 100 // feed.length)}%")

        errors.clear()
        for source, url in result.download_candidates:
            check_cancel()
            # Resume within the same source. A different mirror starts a new file
            # so bytes from two different archives can never be spliced together.
            archive.unlink(missing_ok=True)
            progress(f"正在连接更新源（{source}）…")
            downloaded = http_client.download(
                url, str(archive), session=session, proxies=proxies,
                progress_cb=download_progress, cancel_check=cancelled,
                expected_size=feed.length,
            )
            check_cancel()
            if not downloaded.ok:
                errors.append(f"{source}: {downloaded.error}")
                continue
            if archive.stat().st_size != feed.length:
                errors.append(f"{source}: 更新包大小与签名清单不一致")
                continue
            if result.primary_sha256:
                progress("正在校验更新包…")
                with archive.open("rb") as stream:
                    digest = hashlib.file_digest(stream, "sha256").hexdigest()
                check_cancel()
                if digest != result.primary_sha256:
                    errors.append(f"{source}: 更新包 SHA256 不匹配")
                    continue
            return PreparedMacUpdate(temporary, archive, feed)
        raise ValueError("所有更新源均下载失败。\n" + "\n".join(errors))
    except BaseException:
        if temporary is not None:
            temporary.cleanup()
        raise
    finally:
        session.close()


def prepare_in_process(spec: dict, cache_dir: str, channel) -> None:
    """Spawn entry point: plain data only, no Qt objects or native updater state.

    The parent owns the directory and may terminate this process to interrupt
    DNS, response headers, body reads, hashing or retry sleep immediately.
    """
    result = SimpleNamespace(
        release=SimpleNamespace(tag=spec["tag"], version=spec["version"]),
        primary_asset_name=spec["asset"], primary_sha256=spec["sha256"],
        download_candidates=spec["candidates"],
    )
    settings = SimpleNamespace(
        source_order=spec["sources"], proxy_mode=spec["proxy_mode"],
        proxy_manual_url=spec["proxy_url"],
    )
    try:
        prepared = prepare_update(
            result, settings, cancelled=lambda: False,
            progress=lambda status: channel.send(("progress", status)),
            cache_dir=Path(cache_dir),
            feed_candidates=spec["feeds"],
            feed=MacFeed.from_dict(spec["feed"]) if spec.get("feed") else None,
            current_build=spec.get("current_build", ""),
            delta_candidates=spec.get("delta_candidates"),
        )
        channel.send(("prepared", {
            "feed": asdict(prepared.feed),
            "delta": asdict(prepared.delta) if prepared.delta else None,
            "delta_archive": prepared.delta_archive.name if prepared.delta_archive else None,
        }))
    except Exception as exc:
        try:
            channel.send(("error", str(exc)))
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        channel.close()


class LocalUpdateServer:
    """Serve only prepared resources; an unavailable full archive ends the delta cycle."""

    def __init__(self, prepared: PreparedMacUpdate):
        token = secrets.token_urlsafe(32)
        self.feed_path = f"/{token}/{FEED_NAME}"
        self.archive_path = f"/{token}/{ASSET_NAME}"
        self.delta_path = f"/{token}/update.delta"
        feed_path, archive_path = self.feed_path, self.archive_path
        delta_path = self.delta_path
        # Bind before rendering: native Sparkle must see the .delta extension.
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
        origin = f"http://127.0.0.1:{self.server.server_port}"
        self.feed_url = origin + feed_path
        self.archive_url = origin + archive_path
        self.delta_url = origin + delta_path if prepared.delta is not None else None
        feed_data = render_feed(prepared.feed, delta=prepared.delta, delta_url=self.delta_url or "", include_index=False)

        class Handler(BaseHTTPRequestHandler):
            def do_HEAD(self):
                self.serve(False)

            def do_GET(self):
                self.serve(True)

            def serve(self, body):
                if self.path not in (feed_path, archive_path, delta_path):
                    self.send_error(404)
                    return
                is_feed = self.path == feed_path
                is_delta = self.path == delta_path
                if is_delta and (prepared.delta is None or prepared.delta_archive is None):
                    self.send_error(404)
                    return
                if self.path == archive_path and prepared.delta is not None:
                    # End Sparkle's automatic fallback promptly. The host
                    # prepares the full archive before starting a new cycle.
                    self.send_error(503)
                    return
                archive = prepared.delta_archive if is_delta else prepared.archive
                length = len(feed_data) if is_feed else (prepared.delta.length if is_delta else prepared.feed.length)
                self.send_response(200)
                self.send_header("Content-Type", "application/rss+xml" if is_feed else "application/octet-stream")
                self.send_header("Content-Length", str(length))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                if body:
                    try:
                        if is_feed:
                            self.wfile.write(feed_data)
                        else:
                            with archive.open("rb") as stream:
                                while chunk := stream.read(256 * 1024):
                                    self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError):
                        pass

            def log_message(self, *args):
                pass

        self.server.RequestHandlerClass = Handler
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
