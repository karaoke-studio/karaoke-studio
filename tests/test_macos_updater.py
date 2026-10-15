from __future__ import annotations

from types import SimpleNamespace

import pytest

from krok_helper.updater.macos_feed import ASSET_NAME


@pytest.mark.parametrize("platform,host_available,has_feed", [("darwin", False, True), ("darwin", True, False),
                         ("darwin", False, False), ("darwin", True, True), ("win32", False, False)])
def test_update_prompt_uses_installer_only_when_supported(monkeypatch, platform, host_available, has_feed):
    import sys
    from PyQt6.QtWidgets import QApplication, QPushButton
    from krok_helper.updater import dialogs, macos_metadata
    from krok_helper.updater.macos_feed import FEED_NAME

    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(macos_metadata, "is_auto_update_available", lambda: host_available)
    opened = []
    monkeypatch.setattr(dialogs.QDesktopServices, "openUrl", lambda url: opened.append(url.toString()) or True)
    release = SimpleNamespace(version="1.0.1", body="测试更新", published_at="", html_url="https://github.com/karaoke-studio/karaoke-studio/releases/tag/v1.0.1",
                              assets=[SimpleNamespace(name=ASSET_NAME)] + ([SimpleNamespace(name=FEED_NAME)] if has_feed else []))
    dialog = dialogs.WorkbenchUpdateDialog(release, local_version="1.0.0")
    try:
        buttons = {button.text(): button for button in dialog.findChildren(QPushButton)}
        if platform != "darwin" or (host_available and has_feed):
            assert "前往下载" not in buttons
            buttons["立即更新"].click()
            assert opened == []
            assert dialog.user_choice == "update"
        else:
            assert "立即更新" not in buttons
            buttons["前往下载"].click()
            assert opened == [release.html_url]
            assert dialog.user_choice == "later"
    finally:
        dialog.close()


def test_macos_routes_to_native_backend_without_windows_launcher(monkeypatch):
    import sys
    from krok_helper.gui_qt import KrokHelperQtApp
    called = []
    host = SimpleNamespace(_active_update_task_labels=lambda: [], _launch_macos_updater=called.append)
    result = SimpleNamespace(release=object(), download_candidates=[("github", "url")])
    monkeypatch.setattr(sys, "platform", "darwin")
    KrokHelperQtApp._launch_workbench_updater(host, result)
    assert called == [result]



def test_macos_checker_does_not_fall_back_to_windows_zip(monkeypatch):
    import sys
    from krok_helper.updater.worker import LatestRelease, ReleaseAsset
    monkeypatch.setattr(sys, "platform", "darwin")
    release = LatestRelease("v1.0.1", "1.0.1", "", "", "", False, "", [
        ReleaseAsset("KaraokeStudio-windows.zip", 1, "url"),
    ])
    assert release.pick_primary_asset(ASSET_NAME) is None



def test_macos_relaunch_only_saves_resources_without_hard_exit(monkeypatch):
    from krok_helper.gui_qt import KrokHelperQtApp
    calls = []
    host = SimpleNamespace(_prepare_force_quit_for_update=lambda: calls.append("prepared"))
    KrokHelperQtApp._prepare_macos_update_relaunch(host)
    assert host._force_quitting_for_update
    assert calls == ["prepared"]


def test_update_handoff_retains_dirty_sug_recovery(tmp_path):
    from krok_helper.gui_qt import KrokHelperQtApp

    recovery = tmp_path / ".project.sug.temp"
    released = []
    page = SimpleNamespace(
        flush_unsaved=lambda: recovery.write_bytes(b"recoverable project"),
        _store=SimpleNamespace(cleanup_temp_files=lambda: recovery.unlink()),
        editorInterface=SimpleNamespace(release_resources=lambda: released.append(True)),
    )
    host = SimpleNamespace(
        lyrics_timing_page=page,
        audio_merge_page=SimpleNamespace(shutdown=lambda: released.append("merge")),
        _shutdown_audio_separation=lambda **_: True,
        _save_all_settings=lambda: None,
    )
    KrokHelperQtApp._prepare_force_quit_for_update(host)
    KrokHelperQtApp._prepare_force_quit_for_update(host)
    assert recovery.read_bytes() == b"recoverable project"
    assert released == ["merge", True]


@pytest.mark.parametrize("failure", ["disk", "busy", "subtitle"])
def test_failed_recovery_keeps_workbench_resources_alive(tmp_path, monkeypatch, failure):
    from krok_helper.errors import ProcessingError
    from krok_helper.gui_qt import KrokHelperQtApp
    from strange_uta_game.backend.infrastructure.persistence.sug_io import SugProjectParser

    recovery = tmp_path / ".project.sug.temp"
    recovery.write_bytes(b"older snapshot")
    def save(*args, **kwargs):
        if failure == "disk":
            raise OSError("disk full")
        recovery.write_bytes(b"latest snapshot")
    monkeypatch.setattr(SugProjectParser, "save", save)
    store = SimpleNamespace(
        dirty=True, project=object(),
        _save_thread=SimpleNamespace(isRunning=lambda: True, wait=lambda _: False) if failure == "busy" else None,
        _bg_save_thread=None, get_temp_path=lambda: recovery,
        _get_nicokara_tags_for_save=lambda: None, get_saveable_media_path=lambda: None,
    )
    host = SimpleNamespace(lyrics_timing_page=SimpleNamespace(_store=store),
                           subtitle_render_page=SimpleNamespace(flush_unsaved=lambda: False))
    with pytest.raises(ProcessingError):
        KrokHelperQtApp._verify_macos_update_recovery(host)
    assert not getattr(host, "_force_quitting_for_update", False)
    assert not getattr(host, "_update_exit_prepared", False)
    assert recovery.read_bytes() == (b"latest snapshot" if failure == "subtitle" else b"older snapshot")



def test_install_handoff_quits_qt_without_reentering_cocoa_termination():
    from krok_helper.updater.macos_controller import MacUpdateController
    # No cancel, worker wait, native terminate or cleanup before the installer
    # observes process exit. The install agent already owns its staged copy.
    MacUpdateController.on_quit(SimpleNamespace(installing=True))


def test_cancelled_download_releases_formal_entry_for_a_new_session(monkeypatch, tmp_path):
    import base64
    import hashlib
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from pathlib import Path
    from PyQt6.QtWidgets import QApplication, QWidget
    from PyQt6 import sip
    from krok_helper.gui_qt import KrokHelperQtApp
    from krok_helper.updater import macos_controller, macos_transport
    from krok_helper.updater.macos_feed import MacFeed, render_feed
    from krok_helper.updater.settings import UpdaterSettings
    from krok_helper.updater.sources import build_download_url

    payload = bytes(range(256)) * 8192
    feed = render_feed(MacFeed("1.0.1", "1.0.1000", len(payload),
                              base64.b64encode(bytes(64)).decode(),
                              build_download_url("github", "v1.0.1", ASSET_NAME)))
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = feed if self.path == "/feed" else payload
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                if self.path == "/feed":
                    self.wfile.write(body)
                else:
                    for offset in range(0, len(body), 65536):
                        self.wfile.write(body[offset:offset+65536])
                        self.wfile.flush()
                        time.sleep(0.1)
            except (BrokenPipeError, ConnectionResetError):
                pass
        def log_message(self, *args):
            pass

    server=ThreadingHTTPServer(("127.0.0.1",0),Handler)
    thread=threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin=f"http://127.0.0.1:{server.server_port}"
    monkeypatch.setattr(macos_transport,"build_release_urls",lambda *args:[("github",origin+"/feed")])
    monkeypatch.setattr(macos_controller,"find_host_bundle",lambda:Path("/unused/Test.app"))
    monkeypatch.setattr(UpdaterSettings,"load",lambda _:UpdaterSettings(proxy_mode="off"))
    host=QWidget()
    host.settings=None
    host._active_update_task_labels=lambda:[]
    host._prepare_macos_update_relaunch=lambda:None
    result=SimpleNamespace(release=SimpleNamespace(tag="v1.0.1",version="1.0.1"),
                           primary_asset_name=ASSET_NAME,
                           primary_sha256=hashlib.sha256(payload).hexdigest(),
                           download_candidates=[("github",origin+"/archive")])
    app=QApplication.instance()
    def until(predicate, seconds):
        deadline=time.monotonic()+seconds
        while time.monotonic()<deadline:
            app.processEvents()
            if predicate():
                return
            time.sleep(0.01)
        raise AssertionError("Update cancellation did not reach expected state")

    controllers=[]
    try:
        for _ in range(2):
            KrokHelperQtApp._launch_macos_updater(host,result)
            controller=host._macos_updater
            controllers.append(controller)
            until(lambda:controller.progress._ring.value()>0,5)
            controller.progress._on_cancel()
            until(lambda:host._macos_updater is None,5)
            until(lambda:sip.isdeleted(controller.progress) or not controller.progress.isVisible(),2)
            assert controller.completed and controller.worker is None
            assert controller.native is None and controller.prepared is None
        assert controllers[0] is not controllers[1]
        # Watch delayed callbacks/teardown after repeated cancellation, rather
        # than ending the check as soon as the dialog disappears.
        deadline=time.monotonic()+12
        while time.monotonic()<deadline:
            app.processEvents()
            assert host._macos_updater is None
            time.sleep(0.02)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        host.close()
