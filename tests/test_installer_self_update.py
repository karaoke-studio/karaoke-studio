"""installer 侧 Updater.exe 自更新（Phase 3）的单元测试。"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from krok_helper.updater import installer
from krok_helper.updater.http_client import HttpResult
from krok_helper.updater.installer import (
    LaunchPlan,
    UpdateCancelledError,
    _content_hash_of_zip,
    _manifest_asset_name,
    _read_pe_subsystem,
    _update_updater_from_remote,
)


def _make_plan(tmp_path: Path) -> LaunchPlan:
    (tmp_path / "_internal").mkdir(parents=True, exist_ok=True)
    return LaunchPlan(
        app_dir=tmp_path,
        app_exe_name="Karaoke Studio.exe",
        target_version="9.9.9",
        target_tag="v9.9.9",
        asset_name="KaraokeStudio-windows.zip",
        download_urls=[
            ("github", "https://example.invalid/download/v9.9.9/KaraokeStudio-windows.zip")
        ],
    )


def _make_app_part_zip(path: Path, updater_bytes: bytes) -> tuple[Path, str]:
    with zipfile.ZipFile(str(path), "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("Karaoke Studio.exe", b"EXE")
        zf.writestr("Updater.exe", updater_bytes)
    return path, _content_hash_of_zip(path)


def _remote_manifest(app_sha: str) -> dict:
    return {
        "version": "9.9.9",
        "schema": 1,
        "parts": {
            "app": {
                "asset": "KaraokeStudio-windows-app.zip",
                "sha256": app_sha,
                "size": 123,
                "targets": ["Karaoke Studio.exe", "Updater.exe"],
            }
        },
    }


def _write_local_manifest(plan: LaunchPlan, app_sha: str) -> None:
    payload = {"version": "1.0.0", "schema": 1, "parts": {"app": {"sha256": app_sha}}}
    (plan.app_dir / "_internal" / ".installed_manifest.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


@pytest.fixture(autouse=True)
def _isolated_parts_dir(tmp_path, monkeypatch):
    """把 %TEMP%/KaraokeStudioUpdater 隔离到测试临时目录。"""
    monkeypatch.setattr(installer.tempfile, "gettempdir", lambda: str(tmp_path / "temp"))


def test_manifest_name_derivation_matches_shipped_updater() -> None:
    assert _manifest_asset_name("KaraokeStudio-windows.zip") == "KaraokeStudio-windows.json"


def test_read_pe_subsystem(tmp_path) -> None:
    exe = tmp_path / "x.exe"
    # 构造最小 PE 头：0x3C 处 PE offset=0x80，0x80+0x5C 处 Subsystem=2
    data = bytearray(0x100)
    data[0x3C:0x40] = (0x80).to_bytes(4, "little")
    data[0x80 + 0x5C : 0x80 + 0x5E] = (2).to_bytes(2, "little")
    exe.write_bytes(bytes(data))
    assert _read_pe_subsystem(exe) == 2
    assert _read_pe_subsystem(tmp_path / "missing.exe") == 0


def test_self_update_degrades_when_manifest_unreachable(tmp_path, monkeypatch) -> None:
    plan = _make_plan(tmp_path)
    monkeypatch.setattr(installer, "_fetch_remote_manifest", lambda p, x: None)
    assert _update_updater_from_remote(plan) is False


def test_self_update_skips_when_sha_matches(tmp_path, monkeypatch) -> None:
    plan = _make_plan(tmp_path)
    _write_local_manifest(plan, "abc123")
    monkeypatch.setattr(installer, "_fetch_remote_manifest", lambda p, x: _remote_manifest("abc123"))

    called = []
    from krok_helper.updater import http_client

    monkeypatch.setattr(http_client, "download", lambda *a, **k: called.append(1))
    assert _update_updater_from_remote(plan) is True
    assert not called  # sha 一致时不发起任何下载


def test_self_update_downloads_and_replaces_updater(tmp_path, monkeypatch) -> None:
    plan = _make_plan(tmp_path)
    (plan.app_dir / "Updater.exe").write_bytes(b"OLD")
    _write_local_manifest(plan, "old-sha")

    src_zip, app_sha = _make_app_part_zip(tmp_path / "src.zip", b"NEW-UPDATER")
    monkeypatch.setattr(installer, "_fetch_remote_manifest", lambda p, x: _remote_manifest(app_sha))

    from krok_helper.updater import http_client

    def fake_download(url, dest, **kw):
        Path(dest).write_bytes(src_zip.read_bytes())
        return HttpResult(ok=True, status=200, file_path=dest)

    monkeypatch.setattr(http_client, "download", fake_download)

    progress: list[str] = []
    assert _update_updater_from_remote(plan, progress_cb=progress.append) is True
    assert (plan.app_dir / "Updater.exe").read_bytes() == b"NEW-UPDATER"
    # part zip 保留在 parts 目录供 Updater 增量复用
    parts_dir = Path(installer.tempfile.gettempdir()) / installer.TMP_DIR_NAME / "parts"
    assert (parts_dir / "KaraokeStudio-windows-app.zip").exists()
    assert any("下载" in t or "提取" in t for t in progress)


def test_self_update_reuses_cached_part_zip_without_download(tmp_path, monkeypatch) -> None:
    plan = _make_plan(tmp_path)
    (plan.app_dir / "Updater.exe").write_bytes(b"OLD")

    parts_dir = Path(installer.tempfile.gettempdir()) / installer.TMP_DIR_NAME / "parts"
    parts_dir.mkdir(parents=True)
    cached, app_sha = _make_app_part_zip(parts_dir / "KaraokeStudio-windows-app.zip", b"NEW")
    monkeypatch.setattr(installer, "_fetch_remote_manifest", lambda p, x: _remote_manifest(app_sha))

    from krok_helper.updater import http_client

    def fail_download(*a, **kw):
        raise AssertionError("本地缓存哈希一致时不应发起下载")

    monkeypatch.setattr(http_client, "download", fail_download)
    assert _update_updater_from_remote(plan) is True
    assert (plan.app_dir / "Updater.exe").read_bytes() == b"NEW"


def test_self_update_rejects_hash_mismatch(tmp_path, monkeypatch) -> None:
    plan = _make_plan(tmp_path)
    (plan.app_dir / "Updater.exe").write_bytes(b"OLD")
    src_zip, _sha = _make_app_part_zip(tmp_path / "src.zip", b"EVIL")
    monkeypatch.setattr(
        installer, "_fetch_remote_manifest", lambda p, x: _remote_manifest("0" * 64)
    )

    from krok_helper.updater import http_client

    def fake_download(url, dest, **kw):
        Path(dest).write_bytes(src_zip.read_bytes())
        return HttpResult(ok=True, status=200, file_path=dest)

    monkeypatch.setattr(http_client, "download", fake_download)
    assert _update_updater_from_remote(plan) is False
    assert (plan.app_dir / "Updater.exe").read_bytes() == b"OLD"  # 未被替换


def test_self_update_cancel_raises(tmp_path, monkeypatch) -> None:
    plan = _make_plan(tmp_path)
    monkeypatch.setattr(
        installer, "_fetch_remote_manifest", lambda p, x: _remote_manifest("f" * 64)
    )
    with pytest.raises(UpdateCancelledError):
        _update_updater_from_remote(plan, cancel_check=lambda: True)


def test_self_update_byte_identical_skips_write(tmp_path, monkeypatch) -> None:
    plan = _make_plan(tmp_path)
    (plan.app_dir / "Updater.exe").write_bytes(b"SAME")
    src_zip, app_sha = _make_app_part_zip(tmp_path / "src.zip", b"SAME")
    monkeypatch.setattr(installer, "_fetch_remote_manifest", lambda p, x: _remote_manifest(app_sha))

    from krok_helper.updater import http_client

    def fake_download(url, dest, **kw):
        Path(dest).write_bytes(src_zip.read_bytes())
        return HttpResult(ok=True, status=200, file_path=dest)

    monkeypatch.setattr(http_client, "download", fake_download)
    assert _update_updater_from_remote(plan) is True
    assert (plan.app_dir / "Updater.exe").read_bytes() == b"SAME"
