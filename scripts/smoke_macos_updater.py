"""Exercise signed replacement/relaunch in an isolated disposable macOS app.

Requires the build environment plus cryptography. Never targets an installed
Lin-K Lyrics app and never stores keys in the login Keychain.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.build_macos_appcast import sign_archive
from krok_helper.updater.macos_feed import parse_feed
from scripts.prepare_macos_updater import configure_bundle, sign_bundle

CLIENT = '''
from multiprocessing import freeze_support
freeze_support()
import json, plistlib, sys
from pathlib import Path
from types import SimpleNamespace
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication, QWidget
from krok_helper.updater import macos_transport
from krok_helper.updater.macos_controller import MacUpdateController
from krok_helper.updater.settings import UpdaterSettings

app = QApplication([])
bundle = Path(sys.executable).parent.parent.parent
metadata = plistlib.loads((bundle / 'Contents/Info.plist').read_bytes())
control = Path(metadata['LKSmokeControl'])
config = json.loads(control.read_text())
if metadata['CFBundleVersion'] == '1.0.1000':
    Path(config['marker']).write_text('restarted new version')
    raise SystemExit(0)
parent = QWidget()
parent.show()
app.aboutToQuit.connect(lambda: print('Qt aboutToQuit', flush=True))
macos_transport.build_release_urls = lambda *args: [('github', config['origin'] + '/feed')]
def preparing():
    Path(config['prepared']).write_text('installer ready')
def verify_recovery():
    if config.get('reject_recovery'):
        raise OSError('simulated recovery disk full')
controller = MacUpdateController(parent, can_install=lambda: True, prepare_relaunch=preparing,
                                 verify_recovery=verify_recovery)
status = controller.progress.set_status
def trace_status(text):
    print(text, flush=True)
    status(text)
controller.progress.set_status = trace_status
def finished(error):
    if error:
        Path(config['error']).write_text(error)
        app.exit(1)
controller.finished.connect(finished)
result = SimpleNamespace(
    release=SimpleNamespace(tag='v1.0.1', version='1.0.1'),
    primary_asset_name='KaraokeStudio-macos.zip', primary_sha256=config['sha256'],
    download_candidates=[('github', config['origin'] + '/archive')],
)
QTimer.singleShot(0, lambda: controller.start(result, UpdaterSettings(proxy_mode='off')))
QTimer.singleShot(90000, lambda: app.exit(2))
raise SystemExit(app.exec())
'''


def run_workbench_exit_smoke() -> int:
    """Manually exercise the source workbench's update exit with active media.

    Run with isolated KARAOKE_STUDIO_SETTINGS_DIR, KARAOKE_STUDIO_LOG_DIR and
    KARAOKE_STUDIO_TEMP_DIR on the native Qt platform, never offscreen: this
    check includes interpreter and multimedia teardown.
    """
    import os
    import tempfile
    import traceback
    import wave
    from pathlib import Path

    for name in ("SETTINGS", "LOG", "TEMP"):
        if not os.environ.get(f"KARAOKE_STUDIO_{name}_DIR"):
            raise ValueError(f"Set an isolated KARAOKE_STUDIO_{name}_DIR for this check.")
    isolated = Path(os.environ["KARAOKE_STUDIO_SETTINGS_DIR"])
    for name in ("CONFIG", "CACHE", "BACKUP", "LOGS"):
        os.environ[f"SUG_{name}_DIR"] = str(isolated / f"sug-{name.lower()}")
    from PyQt6.QtCore import QTimer, QUrl
    from PyQt6.QtWidgets import QApplication
    from krok_helper.gui_qt import KrokHelperQtApp
    from strange_uta_game.backend.application import ProjectService

    app = QApplication.instance() or QApplication([])
    with tempfile.TemporaryDirectory(prefix="lin-k-update-exit-") as directory:
        audio = Path(directory) / "silence.wav"
        with wave.open(str(audio), "wb") as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(44100)
            stream.writeframes(bytes(44100 * 2 * 30))
        window = KrokHelperQtApp()
        window.show()
        page = window.lyrics_timing_page
        subtitle = window.subtitle_render_page
        engine = page._audio_engine
        playback = subtitle._playback
        merge = window.audio_merge_page
        observed: dict[str, bool] = {}

        def fail() -> None:
            traceback.print_exc()
            window._prepare_macos_update_relaunch()
            app.exit(1)

        def start() -> None:
            try:
                page._store.load_project(ProjectService().create_project())
                engine.load(str(audio))
                engine.set_volume(0)
                engine.play()
                assert playback is not None, "Subtitle playback is unavailable"
                playback.set_volume(0)
                playback.set_media(audio)
                playback.play()
                merge_player = merge._ensure_player()
                merge._audio_output.setVolume(0)
                merge_player.setSource(QUrl.fromLocalFile(str(audio)))
                merge_player.play()
                QTimer.singleShot(3000, observe)
                QTimer.singleShot(15000, finish)
            except Exception:
                fail()

        def observe() -> None:
            try:
                observed["sug"] = engine.is_playing()
                observed["qt"] = playback.is_playing()
                observed["merge"] = merge._player_playback_active()
                assert all(observed.values()), observed
            except Exception:
                fail()

        def finish() -> None:
            try:
                assert observed == {"sug": True, "qt": True, "merge": True}, observed
                # Mark dirty immediately before handoff so periodic autosave
                # cannot conceal a missing or deleted synchronous exit snapshot.
                page._store.mark_dirty()
                subtitle._project_dirty = True
                window._verify_macos_update_recovery()
                window._prepare_macos_update_relaunch()
                assert page._store.get_temp_path().is_file(), "SUG recovery was lost"
                assert subtitle._recovery_path().is_file(), "Subtitle recovery was lost"
                assert not engine.is_playing(), "SUG audio still plays after handoff"
                assert not playback.is_playing(), "Qt media still plays after handoff"
                assert playback.media_player.source().isEmpty(), "Decoder source retained"
                assert subtitle._host_exit_shutdown
                assert merge._player is None and merge._audio_output is None
                window.close()
                print("PASS: workbench update exit retained recovery and released SUG/subtitle/merge audio.")
                app.quit()
            except Exception:
                fail()

        QTimer.singleShot(0, start)
        result = app.exec()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk-dir", type=Path)
    parser.add_argument("--workbench-exit", action="store_true", help="手工验证完整工作台更新退出，需设置独立数据目录")
    args = parser.parse_args()
    if args.workbench_exit:
        raise SystemExit(run_workbench_exit_smoke())
    if args.sdk_dir is None:
        parser.error("原生安装验证需要 --sdk-dir。")
    sdk = args.sdk_dir.resolve()
    root = Path(__file__).resolve().parents[1]
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, PublicFormat, NoEncryption

    key = Ed25519PrivateKey.generate()
    public = base64.b64encode(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()
    private = base64.b64encode(key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())).decode()
    # Preserve failed fixtures for native installer diagnostics; no private keys
    # are written into the fixture, its plist contains only the ephemeral public key.
    with tempfile.TemporaryDirectory(prefix="Lin-K-Sparkle-Smoke-", delete=False) as tmp:
        directory = Path(tmp)
        print(f"Disposable fixture: {directory}", flush=True)
        control, marker, prepared, error = (directory / name for name in ("control.json", "restarted", "prepared", "error"))
        client = directory / "client.py"
        client.write_text(CLIENT)
        app_name = "Lin-K Lyrics"
        command = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--windowed", "--onedir",
                   "--name", app_name, "--osx-bundle-identifier", "io.github.karaoke-studio.smoke." + uuid.uuid4().hex,
                   "--distpath", str(directory / "dist"), "--workpath", str(directory / "build"),
                   "--specpath", str(directory), "--paths", str(root), "--hidden-import", "objc",
                   "--hidden-import", "Foundation", "--hidden-import", "AppKit", str(client)]
        with (directory / "build.log").open("w") as log:
            built = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT,
                                   env={**os.environ, "PYINSTALLER_CONFIG_DIR": str(directory / "cache")})
        if built.returncode:
            print((directory / "build.log").read_text()[-5000:])
            raise RuntimeError("Failed to freeze the disposable test client")
        new_app = directory / "dist" / f"{app_name}.app"
        configure_bundle(new_app, sdk, public)
        plist = new_app / "Contents/Info.plist"
        metadata = plistlib.loads(plist.read_bytes())
        metadata.update({"CFBundleVersion": "1.0.1000", "CFBundleShortVersionString": "1.0.1", "LKSmokeControl": str(control)})
        plist.write_bytes(plistlib.dumps(metadata))
        sign_bundle(new_app)
        old_app = directory / "installed" / f"{app_name}.app"
        old_app.parent.mkdir()
        subprocess.run(["ditto", str(new_app), str(old_app)], check=True)
        old_plist = old_app / "Contents/Info.plist"
        metadata.update({"CFBundleVersion": "1.0.0", "CFBundleShortVersionString": "1.0.0"})
        old_plist.write_bytes(plistlib.dumps(metadata))
        subprocess.run(["codesign", "--force", "--sign", "-", str(old_app)], check=True)
        archive = directory / "KaraokeStudio-macos.zip"
        subprocess.run(["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", str(new_app), str(archive)], check=True)
        feed = sign_archive(archive, sdk, "1.0.1", public, private)

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", str(len(feed) if self.path == "/feed" else archive.stat().st_size))
                self.end_headers()
                if self.path == "/feed":
                    self.wfile.write(feed)
                else:
                    with archive.open("rb") as stream:
                        while chunk := stream.read(256 * 1024):
                            self.wfile.write(chunk)
            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        with archive.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        control.write_text(json.dumps({"origin": f"http://127.0.0.1:{server.server_port}", "sha256": digest,
                                      "marker": str(marker), "prepared": str(prepared), "error": str(error)}))
        # A syntactically valid but incorrect signature must fail inside Sparkle
        # before host preparation, installation or relaunch.
        valid_feed = feed
        signature = parse_feed(feed, tag="v1.0.1", version="1.0.1").signature
        corrupted = bytearray(base64.b64decode(signature))
        corrupted[0] ^= 1
        feed = feed.replace(signature.encode(), base64.b64encode(corrupted))
        runtime_env = {k: v for k, v in os.environ.items() if k != "QT_QPA_PLATFORM"}
        with (directory / "rejected.log").open("w") as rejection_log:
            rejected = subprocess.Popen([str(old_app / "Contents/MacOS" / app_name)],
                                        stdout=rejection_log, stderr=subprocess.STDOUT, env=runtime_env)
            try:
                rejected.wait(timeout=60)
            except subprocess.TimeoutExpired:
                rejected.terminate()
                rejected.wait(timeout=15)
                raise RuntimeError("Sparkle did not finish rejecting an invalid signature") from None
        server_version = plistlib.loads(old_plist.read_bytes())["CFBundleVersion"]
        if rejected.returncode != 1 or not error.exists() or prepared.exists() or marker.exists() or server_version != "1.0.0":
            print((directory / "rejected.log").read_text(errors="replace")[-5000:])
            raise RuntimeError("Invalid signature was not safely rejected")
        print("PASS: invalid Ed25519 signature rejected; old app retained and host not terminated.", flush=True)
        error.unlink()
        feed = valid_feed
        config = json.loads(control.read_text())
        config['reject_recovery'] = True
        control.write_text(json.dumps(config))
        with (directory / "recovery-rejected.log").open("w") as recovery_log:
            recovery_rejected = subprocess.Popen(
                [str(old_app / "Contents/MacOS" / app_name)],
                stdout=recovery_log, stderr=subprocess.STDOUT, env=runtime_env,
            )
            try:
                recovery_rejected.wait(timeout=60)
            except subprocess.TimeoutExpired:
                recovery_rejected.terminate()
                recovery_rejected.wait(timeout=15)
                raise RuntimeError("Sparkle did not cancel after recovery failure") from None
        retained_version = plistlib.loads(old_plist.read_bytes())["CFBundleVersion"]
        if (recovery_rejected.returncode != 1 or not error.exists()
                or 'simulated recovery disk full' not in error.read_text()
                or prepared.exists() or marker.exists() or retained_version != "1.0.0"):
            print((directory / "recovery-rejected.log").read_text(errors="replace")[-5000:])
            raise RuntimeError("Recovery failure did not safely prevent installation")
        print("PASS: failed recovery rejected installation before host resource release.", flush=True)
        error.unlink()
        config['reject_recovery'] = False
        control.write_text(json.dumps(config))
        # Read native progress continuously into a file. Undrained PIPE output
        # can block the GUI before it processes the install/termination callback.
        output = (directory / "client.log").open("w")
        process = subprocess.Popen([str(old_app / "Contents/MacOS" / app_name)], stdout=output, stderr=subprocess.STDOUT,
                                   env=runtime_env)
        try:
            deadline = time.monotonic() + 100
            while time.monotonic() < deadline and not marker.exists() and not error.exists():
                # Wait for the installer agent after the prepared host exits.
                if process.poll() is not None and process.returncode != 0 and not prepared.exists():
                    break
                time.sleep(0.2)
            if not marker.exists():
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=15)
                output.flush()
                print((directory / "client.log").read_text(errors="replace")[-6000:])
                if error.exists():
                    print(error.read_text())
                raise RuntimeError("Disposable app did not replace and relaunch")
            assert prepared.exists(), "Host must prepare recovery data before relaunch"
            assert process.wait(timeout=15) == 0, "The old host must exit cleanly after installer handoff"
            installed = plistlib.loads(old_plist.read_bytes())
            assert installed["CFBundleVersion"] == "1.0.1000"
            print("PASS: signed cached update installed and relaunched; host prepared before termination (host exit 0).")
        finally:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=15)
            output.close()
            server.shutdown()
            server.server_close()
        shutil.rmtree(directory)


if __name__ == "__main__":
    main()
