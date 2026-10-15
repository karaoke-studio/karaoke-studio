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
    ASSET_NAME, FEED_NAME, MAX_FEED_SIZE, MacFeed, parse_feed, render_feed,
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
        feed = None
        feeds = feed_candidates if feed_candidates is not None else build_release_urls(settings.source_order, release.tag, FEED_NAME)
        for source, url in feeds:
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
        )
        channel.send(("prepared", asdict(prepared.feed)))
    except Exception as exc:
        try:
            channel.send(("error", str(exc)))
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        channel.close()


class LocalUpdateServer:
    """Only two token-scoped resources, bound to IPv4 loopback, never a proxy."""

    def __init__(self, prepared: PreparedMacUpdate):
        token = secrets.token_urlsafe(32)
        self.feed_path = f"/{token}/{FEED_NAME}"
        self.archive_path = f"/{token}/{ASSET_NAME}"
        feed_data = render_feed(prepared.feed)
        archive = prepared.archive
        feed_path, archive_path = self.feed_path, self.archive_path

        class Handler(BaseHTTPRequestHandler):
            def do_HEAD(self):
                self.serve(False)

            def do_GET(self):
                self.serve(True)

            def serve(self, body):
                if self.path not in (feed_path, archive_path):
                    self.send_error(404)
                    return
                is_feed = self.path == feed_path
                self.send_response(200)
                self.send_header("Content-Type", "application/rss+xml" if is_feed else "application/octet-stream")
                self.send_header("Content-Length", str(len(feed_data) if is_feed else prepared.feed.length))
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

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()
        origin = f"http://127.0.0.1:{self.server.server_port}"
        self.feed_url = origin + self.feed_path
        self.archive_url = origin + self.archive_path

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
