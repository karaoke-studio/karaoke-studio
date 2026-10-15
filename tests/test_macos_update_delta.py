"""Exact ranged downloads, optional index parsing and full-update fallback."""

import base64
from dataclasses import replace
import hashlib
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
import xml.etree.ElementTree as ET

import pytest
import requests

from krok_helper.updater import http_client, macos_transport
from krok_helper.updater.macos_feed import (
    ASSET_NAME, DELTA_NS, MacDelta, MacFeed, macos_versions, parse_feed, render_feed,
)
from krok_helper.updater.settings import UpdaterSettings
from krok_helper.updater.sources import build_download_url


def make_feed(payload=b'full', patch=b'delta', offset=31):
    signature = base64.b64encode(bytes(range(64))).decode()
    return MacFeed('4.3.7', '4.3.7000', len(payload), signature,
                   build_download_url('github', 'v4.3.7', ASSET_NAME),
                   (MacDelta('4.3.6', len(patch), signature, offset, hashlib.sha256(patch).hexdigest()),),
                   offset + len(patch) + 19)


@pytest.mark.parametrize('attribute,value', [('from', '4.2.99'), ('from', '4.3.7'),
    ('offset', '-1'), ('length', '999999'), ('signature', 'bad'), ('sha256', 'invalid')])
def test_invalid_optional_delta_index_keeps_full_enclosure(attribute, value):
    feed = make_feed()
    root = ET.fromstring(render_feed(feed))
    root.find(f'./channel/item/{{{DELTA_NS}}}deltas/{{{DELTA_NS}}}delta').set(attribute, value)
    parsed = parse_feed(ET.tostring(root), tag='v4.3.7', version='4.3.7')
    assert not parsed.deltas and parsed.signature == feed.signature and parsed.length == feed.length


def test_index_roundtrip_and_duplicate_or_oversized_index_rejection():
    feed = make_feed()
    assert parse_feed(render_feed(feed), tag='v4.3.7', version='4.3.7') == feed
    for deltas in ((feed.deltas[0], feed.deltas[0]), feed.deltas * 21):
        parsed = parse_feed(render_feed(replace(feed, deltas=deltas)), tag='v4.3.7', version='4.3.7')
        assert not parsed.deltas and parsed.signature == feed.signature


@pytest.fixture
def origin(monkeypatch):
    data = bytes(range(256)) * 1024
    hits = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append((self.path, self.headers.get('Range')))
            if self.path == '/ignore':
                self.send_response(200)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                return
            start, end = map(int, self.headers['Range'][6:].split('-'))
            self.send_response(206)
            self.send_header('Content-Range', f'bytes {start + (self.path == "/wrong")}-{end}/{len(data)}')
            self.send_header('Content-Length', str(end - start + 1))
            self.end_headers()
            body = data[start:end + 1]
            self.wfile.write(body[:len(body)//2] if self.path == '/short' else body)
            self.close_connection = True
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .05}, daemon=True)
    thread.start()
    monkeypatch.setattr(http_client, 'DOWNLOAD_RETRY_INTERVAL', 0)
    yield f'http://127.0.0.1:{server.server_port}', data, hits
    server.shutdown()
    server.server_close()
    thread.join()


@pytest.mark.parametrize('offset,length', [(0, 4096), (80000, 100000), (258048, 4096)])
def test_downloads_only_exact_first_middle_or_last_range(origin, tmp_path, offset, length):
    url, data, hits = origin
    with requests.Session() as session:
        session.trust_env = False
        result = http_client.download_range(url, str(tmp_path / 'patch'), offset=offset, length=length,
                                           resource_size=len(data), session=session)
    assert result.ok and (tmp_path / 'patch').read_bytes() == data[offset:offset + length]
    assert hits == [('/', f'bytes={offset}-{offset + length - 1}')]


@pytest.mark.parametrize('route', ['/ignore', '/wrong', '/short'])
def test_range_failures_never_succeed_as_full_downloads(origin, tmp_path, route):
    url, data, hits = origin
    with requests.Session() as session:
        session.trust_env = False
        result = http_client.download_range(url + route, str(tmp_path / 'patch'), offset=3000, length=9000,
                                           resource_size=len(data), session=session)
    assert not result.ok
    assert len(hits) == (http_client.DOWNLOAD_RETRY_COUNT if route == '/short' else 1)


@pytest.mark.parametrize('current_build,range_ok', [('4.3.6000', True), ('4.3.6000', False), ('4.2.9000', True)])
def test_selects_exact_delta_or_falls_back_without_changing_proxy(tmp_path, monkeypatch, current_build, range_ok):
    full, patch = b'full archive', b'signed delta'
    feed = make_feed(full, patch)
    calls = []
    session = SimpleNamespace(close=lambda: None)
    proxies = {'https': 'http://localhost:7890'}
    monkeypatch.setattr(macos_transport, 'requests_session_for_proxy', lambda *a: (session, proxies))
    monkeypatch.setattr(macos_transport, 'build_release_urls', lambda *a: [('mirror1', 'first'), ('mirror2', 'second')])
    def ranged(url, path, **kwargs):
        assert kwargs['session'] is session and kwargs['proxies'] is proxies
        calls.append(url)
        # First mirror returns wrong bytes, proving SHA validation and switching.
        from pathlib import Path
        Path(path).write_bytes(patch if url == 'second' and range_ok else b'wrong bytes!')
        return SimpleNamespace(ok=True)
    def download(url, path, **kwargs):
        from pathlib import Path
        calls.append('full')
        assert kwargs['proxies'] is proxies
        Path(path).write_bytes(full)
        return SimpleNamespace(ok=True)
    monkeypatch.setattr(http_client, 'download_range', ranged)
    monkeypatch.setattr(http_client, 'download', download)
    result = SimpleNamespace(release=SimpleNamespace(tag='v4.3.7', version='4.3.7'), primary_asset_name=ASSET_NAME,
                             primary_sha256=hashlib.sha256(full).hexdigest(), download_candidates=[('github', 'full')])
    prepared = macos_transport.prepare_update(result, UpdaterSettings(), feed=feed, current_build=current_build,
                                              cache_dir=tmp_path, cancelled=lambda: False, progress=lambda _: None)
    if current_build == '4.3.6000' and range_ok:
        assert prepared.delta == feed.deltas[0] and prepared.delta_archive.read_bytes() == patch
        assert calls == ['first', 'second'] and not prepared.archive.exists()
    else:
        assert prepared.delta is None and prepared.archive.read_bytes() == full
        assert calls == (['first', 'second', 'full'] if current_build == '4.3.6000' else ['full'])
