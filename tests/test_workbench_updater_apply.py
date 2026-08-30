from __future__ import annotations

import functools
import hashlib
import http.server
import logging
import threading
import zipfile
from pathlib import Path
from types import SimpleNamespace

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
        zf.writestr("Lin-K Lyrics/Lin-K Lyrics.exe", "new exe\n")
        zf.writestr("Lin-K Lyrics/Karaoke Studio.exe", "new exe\n")
        zf.writestr("Lin-K Lyrics/Updater.exe", "new updater\n")
        zf.writestr("Lin-K Lyrics/_internal/version.txt", "3.0.1\n")
        zf.writestr("Lin-K Lyrics/_internal/runtime/new.txt", "new runtime\n")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_workbench_cleanup_preserves_handed_off_app_part(tmp_path) -> None:
    work_dir = tmp_path / "KaraokeStudioUpdater"
    parts_dir = work_dir / "parts"
    stale_dir = work_dir / "download"
    parts_dir.mkdir(parents=True)
    stale_dir.mkdir()
    app_part = parts_dir / "KaraokeStudio-windows-app.zip"
    app_part.write_bytes(b"cached app part")
    (stale_dir / "partial.zip").write_bytes(b"stale")

    workbench_updater._configure_product()
    workbench_updater.updater_main._cleanup_temp_workdir(work_dir)

    assert app_part.read_bytes() == b"cached app part"
    assert not stale_dir.exists()


def test_invalid_cached_runtime_part_is_removed_and_redownloaded(
    tmp_path, monkeypatch
) -> None:
    release_dir = tmp_path / "release"
    release_dir.mkdir()
    asset_name = "KaraokeStudio-windows-runtime.zip"
    release_zip = release_dir / asset_name
    with zipfile.ZipFile(release_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("_internal/runtime.txt", "valid runtime\n")
    expected_hash = workbench_updater.updater_main._content_hash_of_zip(release_zip)

    work_dir = tmp_path / "KaraokeStudioUpdater"
    parts_dir = work_dir / "parts"
    parts_dir.mkdir(parents=True)
    cached_zip = parts_dir / asset_name
    cached_zip.write_bytes(b"incomplete download")

    server, base_url = _serve_directory(release_dir)
    logger = logging.getLogger("test.invalid-runtime-cache")
    args = SimpleNamespace(
        proxy_url="",
        urls=[("local", f"{base_url}/KaraokeStudio-windows.zip")],
    )
    manifest = {
        "parts": {
            "runtime": {
                "asset": asset_name,
                "sha256": expected_hash,
                "size": release_zip.stat().st_size,
            }
        }
    }
    workbench_updater._configure_product()
    monkeypatch.setattr(workbench_updater.updater_main, "DOWNLOAD_RETRY_INTERVAL", 0)
    try:
        result = workbench_updater.updater_main._download_part(
            args, manifest, "runtime", work_dir, logger
        )
    finally:
        server.shutdown()
        server.server_close()

    assert result == cached_zip
    assert workbench_updater.updater_main._content_hash_of_zip(result) == expected_hash
    assert not (parts_dir / f"{asset_name}.part").exists()


def test_interrupted_part_download_never_publishes_final_zip(
    tmp_path, monkeypatch
) -> None:
    destination = tmp_path / "runtime.zip"
    partial = tmp_path / "runtime.zip.part"

    def interrupted(_url, dest, _proxies, _log):
        assert dest == partial
        dest.write_bytes(b"partial bytes")
        return False, "connection reset"

    monkeypatch.setattr(workbench_updater, "_original_download_one", interrupted)

    ok, error = workbench_updater._download_one_workbench(
        "https://example.invalid/runtime.zip",
        destination,
        None,
        logging.getLogger("test.interrupted-part-download"),
    )

    assert ok is False
    assert error == "connection reset"
    assert not destination.exists()
    assert not partial.exists()


def test_non_zip_http_success_is_rejected_before_publish(tmp_path, monkeypatch) -> None:
    destination = tmp_path / "runtime.zip"
    partial = tmp_path / "runtime.zip.part"

    def html_response(_url, dest, _proxies, _log):
        dest.write_bytes(b"<html>temporary proxy error</html>")
        return True, ""

    monkeypatch.setattr(workbench_updater, "_original_download_one", html_response)

    ok, error = workbench_updater._download_one_workbench(
        "https://example.invalid/runtime.zip",
        destination,
        None,
        logging.getLogger("test.non-zip-http-success"),
    )

    assert ok is False
    assert "不是有效 ZIP" in error
    assert not destination.exists()
    assert not partial.exists()


def test_old_install_applies_renamed_full_zip_from_local_http(tmp_path, monkeypatch) -> None:
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
            ],
            use_gui=False,
        )
    finally:
        server.shutdown()
        server.server_close()

    assert rc == 0
    assert (app_dir / "Karaoke Studio.exe").read_text(encoding="utf-8") == "new exe\n"
    assert (app_dir / "Lin-K Lyrics.exe").read_text(encoding="utf-8") == "new exe\n"
    assert (app_dir / "Updater.exe").read_text(encoding="utf-8") == "new updater\n"
    assert (internal_dir / "version.txt").read_text(encoding="utf-8") == "3.0.1\n"
    assert (internal_dir / "runtime" / "new.txt").read_text(encoding="utf-8") == "new runtime\n"
    assert not (internal_dir / "runtime" / "old.txt").exists()
    assert not (app_dir / "_internal.old").exists()
    assert not (app_dir / "Karaoke Studio.exe.old").exists()
