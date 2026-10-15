from __future__ import annotations

import base64
import hashlib
import select
import socket
import threading
import time
import plistlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
from PyQt6 import sip
from PyQt6.QtWidgets import QApplication, QWidget

from krok_helper.updater import macos_controller, macos_transport
from krok_helper.updater.macos_feed import ASSET_NAME, FEED_NAME, MacDelta, MacFeed, render_feed
from krok_helper.updater.progress_window import UpdateProgressWindow
from krok_helper.updater.settings import UpdaterSettings
from krok_helper.updater.sources import build_download_url


def pump_until(predicate, timeout):
    app = QApplication.instance()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("Update did not reach the expected state")


@pytest.mark.parametrize("stage", ["headers", "trickle", "delta_headers", "delta_trickle"])
def test_cancel_interrupts_blocked_network_and_reaps_objects(stage, monkeypatch, tmp_path):
    payload = bytes(1024 * 1024)
    delta = MacDelta('1.0.0', len(payload), base64.b64encode(bytes(64)).decode(), 16, hashlib.sha256(payload).hexdigest())
    feed = render_feed(MacFeed(
        "1.0.1", "1.0.1000", len(payload), base64.b64encode(bytes(64)).decode(),
        build_download_url("github", "v1.0.1", ASSET_NAME),
        (delta,) if stage.startswith('delta') else (), len(payload) + 32,
    ))
    started, disconnected, stop = threading.Event(), threading.Event(), threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/feed":
                self.send_response(200)
                self.send_header("Content-Length", str(len(feed)))
                self.end_headers()
                self.wfile.write(feed)
                return
            started.set()
            try:
                if stage.endswith("headers"):
                    # No response headers: requests.get itself is blocked.
                    while not stop.is_set():
                        readable, _, _ = select.select([self.connection], [], [], 0.05)
                        if readable and self.connection.recv(1, socket.MSG_PEEK) == b"":
                            disconnected.set()
                            return
                else:
                    self.send_response(206 if stage.startswith('delta') else 200)
                    self.send_header("Content-Length", str(len(payload)))
                    if stage.startswith('delta'):
                        self.send_header('Content-Range', f'bytes 16-{len(payload) + 15}/{len(payload) + 32}')
                    self.end_headers()
                    # Keep the read timeout alive but never produce a 64 KiB
                    # chunk. An Event-only cancellation could wait indefinitely.
                    while not stop.wait(0.05):
                        self.wfile.write(b"x")
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                disconnected.set()
        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    monkeypatch.setattr(macos_transport, "build_release_urls", lambda *args: [("github", origin + ("/feed" if args[-1] == FEED_NAME else "/delta"))])
    bundle = tmp_path / 'Test.app'
    (bundle / 'Contents').mkdir(parents=True)
    (bundle / 'Contents/Info.plist').write_bytes(plistlib.dumps({'CFBundleVersion': '1.0.0'}))
    monkeypatch.setattr(macos_controller, "find_host_bundle", lambda: bundle)
    original_temporary = macos_controller.tempfile.TemporaryDirectory
    directories = []
    def temporary(**kwargs):
        value = original_temporary(dir=tmp_path, **kwargs)
        directories.append(Path(value.name))
        return value
    monkeypatch.setattr(macos_controller.tempfile, "TemporaryDirectory", temporary)
    result = SimpleNamespace(
        release=SimpleNamespace(tag="v1.0.1", version="1.0.1"),
        primary_asset_name=ASSET_NAME, primary_sha256=hashlib.sha256(payload).hexdigest(),
        download_candidates=[("github", origin + "/archive")],
    )
    parent = QWidget()
    controller = macos_controller.MacUpdateController(parent, can_install=lambda: True, prepare_relaunch=lambda: None)
    completed = []
    controller.finished.connect(completed.append)
    try:
        controller.start(result, UpdaterSettings(proxy_mode="off"))
        pump_until(started.is_set, 10)
        # Reproduce an already-running stalled request, including continuous
        # slow traffic, rather than cancelling before the first connection.
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            QApplication.processEvents()
            assert not completed
            time.sleep(0.02)
        began = time.monotonic()
        controller.progress._on_cancel()
        pump_until(lambda: bool(completed), 2)
        assert time.monotonic() - began < 1.5
        assert completed == [""]
        assert controller.worker is None and controller.prepared is None
        pump_until(disconnected.is_set, 2)
        pump_until(lambda: sip.isdeleted(controller), 2)
        assert not [child for child in parent.children()
                    if isinstance(child, (macos_controller.MacUpdateController, UpdateProgressWindow))]
        assert directories and all(not path.exists() for path in directories)
    finally:
        stop.set()
        if not sip.isdeleted(controller) and not controller.completed:
            controller.cancel()
            pump_until(lambda: controller.completed, 3)
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        parent.close()


def test_cache_creation_failure_is_reported_without_uncaught_worker_error(monkeypatch):
    parent = QWidget()
    worker = macos_controller._PrepareWorker(None, UpdaterSettings(), parent)
    def denied(**kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(macos_controller.tempfile, "TemporaryDirectory", denied)
    worker.run()
    assert worker.prepared is None and worker.error == "disk full"
