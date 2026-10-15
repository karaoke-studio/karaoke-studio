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
controller = MacUpdateController(parent, can_install=lambda: True, prepare_relaunch=preparing)
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk-dir", required=True, type=Path)
    args = parser.parse_args()
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
