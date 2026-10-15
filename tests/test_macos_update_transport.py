from __future__ import annotations

import base64
import hashlib
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
import requests

from krok_helper.updater import http_client, macos_transport
from krok_helper.updater.macos_feed import ASSET_NAME, MacFeed, macos_versions, parse_feed, render_feed
from krok_helper.updater.macos_transport import LocalUpdateServer, UpdateCancelled, prepare_update
from krok_helper.updater.settings import UpdaterSettings
from krok_helper.updater.sources import build_download_url


def make_feed(payload=b"signed archive", version="4.3.7.3"):
    return MacFeed(
        version, macos_versions(version)[1], len(payload),
        base64.b64encode(bytes(range(64))).decode(),
        build_download_url("github", f"v{version}", ASSET_NAME),
    )



@pytest.fixture
def source(monkeypatch):
    payload = bytes(range(256)) * 1600
    feed = render_feed(make_feed(payload))
    hits = []
    dropped = False

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            nonlocal dropped
            hits.append((self.path, self.headers.get("Range")))
            if self.path == "/fail":
                self.send_error(503)
                return
            if self.path == "/feed":
                body = feed
            elif self.path == "/corrupt":
                body = payload[::-1]
            elif self.path == "/short":
                body = payload[:100]
            elif self.path == "/oversized":
                body = payload + payload
            else:
                body = payload
            offset = int(self.headers.get("Range", "bytes=0-").split("=")[1].split("-")[0])
            self.send_response(206 if offset else 200)
            self.send_header("Content-Length", str(len(body) - offset))
            if offset:
                self.send_header("Content-Range", f"bytes {offset}-{len(body)-1}/{len(body)}")
            self.end_headers()
            if self.path == "/resume" and not dropped:
                dropped = True
                self.wfile.write(body[:100000])
                self.wfile.flush()
                self.close_connection = True
            else:
                self.wfile.write(body[offset:])

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    monkeypatch.setattr(macos_transport, "build_release_urls", lambda *args: [("github", origin + "/feed")])
    monkeypatch.setattr(http_client, "DOWNLOAD_RETRY_INTERVAL", 0)
    yield origin, payload, hits
    server.shutdown()
    server.server_close()
    thread.join()



def result_for(origin, payload, routes):
    return SimpleNamespace(
        release=SimpleNamespace(tag="v4.3.7.3", version="4.3.7.3"),
        primary_asset_name=ASSET_NAME,
        primary_sha256=hashlib.sha256(payload).hexdigest(),
        download_candidates=[("github", origin + route) for route in routes],
    )



def test_download_fails_over_checks_digest_and_resumes_then_serves_only_cache(source):
    origin, payload, hits = source
    prepared = prepare_update(
        result_for(origin, payload, ["/fail", "/short", "/corrupt", "/oversized", "/resume"]),
        UpdaterSettings(proxy_mode="off"), cancelled=lambda: False, progress=lambda _: None,
    )
    server = LocalUpdateServer(prepared)
    try:
        assert prepared.archive.read_bytes() == payload
        assert ("/resume", "bytes=65536-") in hits
        assert server.server.server_address[0] == "127.0.0.1"
        http = requests.Session()
        http.trust_env = False
        assert http.get(server.archive_url).content == payload
        assert http.head(server.archive_url).headers["Content-Length"] == str(len(payload))
        assert parse_feed(http.get(server.feed_url).content, tag="v4.3.7.3", version="4.3.7.3") == prepared.feed
        root = server.feed_url.rsplit("/", 2)[0]
        for route in ("/", "/" + ASSET_NAME, "/../../etc/passwd", "/?url=https://example.com"):
            assert http.get(root + route).status_code == 404
        http.close()
    finally:
        server.close()
        prepared.close()
    assert not prepared.archive.parent.exists()



def test_cancel_does_not_try_next_source(source):
    origin, payload, hits = source
    stop = threading.Event()
    with pytest.raises(UpdateCancelled):
        prepare_update(
            result_for(origin, payload, ["/resume", "/unused"]),
            UpdaterSettings(proxy_mode="off"), cancelled=stop.is_set,
            progress=lambda text: stop.set() if "下载更新包" in text else None,
        )
    assert not any(path == "/unused" for path, _ in hits)



def test_cancel_before_http_retry_does_not_open_connection(tmp_path):
    def forbidden(*args, **kwargs):
        raise AssertionError("Cancelled downloads must not open another connection")
    result = http_client.download(
        "https://example.invalid/archive", str(tmp_path / "archive"),
        session=SimpleNamespace(get=forbidden), cancel_check=lambda: True,
    )
    assert not result.ok and result.error == "用户取消"



def test_manual_proxy_session_is_used_for_feed_and_archive(source, monkeypatch):
    origin, payload, hits = source
    calls = []
    session = requests.Session()
    session.trust_env = False
    original_get = session.get

    def get(url, **kwargs):
        calls.append((url, kwargs["proxies"]))
        kwargs["proxies"] = None  # This spy routes to our fixture instead of a real proxy.
        return original_get(url, **kwargs)

    session.get = get
    seen = []
    def configured(mode, url):
        seen.append((mode, url))
        return session, {"https": "http://127.0.0.1:7890"}
    monkeypatch.setattr(macos_transport, "requests_session_for_proxy", configured)
    prepared = prepare_update(
        result_for(origin, payload, ["/archive"]),
        UpdaterSettings(proxy_mode="manual", proxy_manual_url="http://127.0.0.1:7890"),
        cancelled=lambda: False, progress=lambda _: None,
    )
    prepared.close()
    assert seen == [("manual", "http://127.0.0.1:7890")]
    assert [url for url, _ in calls] == [origin + "/feed", origin + "/archive"]
    assert all(proxies == {"https": "http://127.0.0.1:7890"} for _, proxies in calls)


def test_native_delta_feed_and_lazy_full_resource_are_separate(tmp_path):
    import xml.etree.ElementTree as ET
    from concurrent.futures import ThreadPoolExecutor
    from krok_helper.updater.macos_feed import MacDelta, SPARKLE_NS
    from krok_helper.updater.macos_transport import PreparedMacUpdate

    delta_file = tmp_path / "update.delta"
    delta_file.write_bytes(b"signed delta")
    archive = tmp_path / ASSET_NAME
    delta = MacDelta("4.3.7.2", delta_file.stat().st_size, make_feed().signature)
    prepared = PreparedMacUpdate(None, archive, make_feed(b"full archive"), delta, delta_file)
    requested = threading.Event()
    server = LocalUpdateServer(prepared, request_full=requested.set)
    http = requests.Session()
    http.trust_env = False
    try:
        item = ET.fromstring(http.get(server.feed_url).content).find("./channel/item")
        enclosure = item.find(f"{{{SPARKLE_NS}}}deltas/enclosure")
        assert enclosure.get("url") == server.delta_url
        assert enclosure.get(f"{{{SPARKLE_NS}}}deltaFrom") == macos_versions("4.3.7.2")[1]
        assert http.get(server.delta_url).content == b"signed delta"
        assert not requested.is_set()
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(http.get, server.archive_url, timeout=5)
            assert requested.wait(2)
            assert not result.done()
            archive.write_bytes(b"full archive")
            server.supply_full(archive)
            assert result.result(timeout=2).content == b"full archive"
    finally:
        server.close()
        http.close()


def test_closing_server_unblocks_pending_native_full_request(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from krok_helper.updater.macos_feed import MacDelta
    from krok_helper.updater.macos_transport import PreparedMacUpdate

    requested = threading.Event()
    prepared = PreparedMacUpdate(None, tmp_path / ASSET_NAME, make_feed(),
                                MacDelta("4.3.7.2", 1, make_feed().signature), tmp_path / "a.delta")
    server = LocalUpdateServer(prepared, request_full=requested.set)
    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(requests.get, server.archive_url, timeout=5, proxies={"http": None})
        assert requested.wait(2)
        server.close()
        assert result.result(timeout=2).status_code == 503
