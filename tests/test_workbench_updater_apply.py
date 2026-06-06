from __future__ import annotations

import functools
import hashlib
import http.server
import threading
import zipfile
from pathlib import Path

from krok_helper.updater_app import main as workbench_updater


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return


def _serve_directory(root: Path):
    handler = functools.partial(_QuietHandler, directory=str(root))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_port}"


def _write_release_zip(path: Path) -> str:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("Karaoke Studio/Karaoke Studio.exe", "new exe\n")
        zf.writestr("Karaoke Studio/Updater.exe", "new updater\n")
        zf.writestr("Karaoke Studio/_internal/version.txt", "3.0.1\n")
        zf.writestr("Karaoke Studio/_internal/runtime/new.txt", "new runtime\n")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_workbench_updater_applies_full_zip_from_local_http(tmp_path, monkeypatch) -> None:
    app_dir = tmp_path / "installed" / "Karaoke Studio"
    internal_dir = app_dir / "_internal"
    internal_dir.mkdir(parents=True)
    (app_dir / "Karaoke Studio.exe").write_text("old exe\n", encoding="utf-8")
    (app_dir / "Updater.exe").write_text("old updater\n", encoding="utf-8")
    (internal_dir / "version.txt").write_text("3.0.0\n", encoding="utf-8")
    (internal_dir / "runtime").mkdir()
    (internal_dir / "runtime" / "old.txt").write_text("old runtime\n", encoding="utf-8")

    release_dir = tmp_path / "release"
    release_dir.mkdir()
    asset_name = "KaraokeStudio-windows.zip"
    digest = _write_release_zip(release_dir / asset_name)
    (release_dir / f"{asset_name}.sha256").write_text(f"{digest}  {asset_name}\n", encoding="ascii")

    updater_temp = tmp_path / "temp"
    monkeypatch.setattr(workbench_updater.updater_main.tempfile, "gettempdir", lambda: str(updater_temp))
    monkeypatch.setattr(workbench_updater.updater_main, "POST_EXIT_GRACE_SECONDS", 0.0)
    monkeypatch.setattr(workbench_updater.updater_main.sys, "platform", "linux")

    server, base_url = _serve_directory(release_dir)
    try:
        rc = workbench_updater.main(
            [
                "--app-dir",
                str(app_dir),
                "--app-exe",
                "Karaoke Studio.exe",
                "--target-version",
                "3.0.1",
                "--target-tag",
                "v3.0.1",
                "--asset-name",
                asset_name,
                "--internal-name",
                "_internal",
                "--pid",
                "0",
                "--url",
                f"local|{base_url}/{asset_name}",
                "--sha256",
                digest,
                "--no-launch",
            ]
        )
    finally:
        server.shutdown()
        server.server_close()

    assert rc == 0
    assert (app_dir / "Karaoke Studio.exe").read_text(encoding="utf-8") == "new exe\n"
    assert (app_dir / "Updater.exe").read_text(encoding="utf-8") == "new updater\n"
    assert (internal_dir / "version.txt").read_text(encoding="utf-8") == "3.0.1\n"
    assert (internal_dir / "runtime" / "new.txt").read_text(encoding="utf-8") == "new runtime\n"
    assert not (internal_dir / "runtime" / "old.txt").exists()
    assert not (app_dir / "_internal.old").exists()
    assert not (app_dir / "Karaoke Studio.exe.old").exists()
