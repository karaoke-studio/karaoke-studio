"""scripts/build_parts.py 的单元测试（Phase 2 增量分包）。"""

from __future__ import annotations

import importlib.util
import json
import sys
import zipfile
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "build_parts", Path(__file__).resolve().parents[1] / "scripts" / "build_parts.py"
)
build_parts = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(build_parts)


def _make_zip(path: Path, entries: dict[str, bytes], reverse: bool = False) -> Path:
    names = sorted(entries, reverse=reverse)
    with zipfile.ZipFile(str(path), "w", zipfile.ZIP_DEFLATED) as zf:
        for name in names:
            zf.writestr(name, entries[name])
    return path


# ── 内容哈希：与 updater_app/main.py 的 _content_hash_of_zip 同一契约 ──


def test_content_hash_ignores_zip_metadata(tmp_path) -> None:
    entries = {"a/x.txt": b"hello", "b/y.bin": b"\x00\x01"}
    z1 = _make_zip(tmp_path / "one.zip", entries)
    z2 = _make_zip(tmp_path / "two.zip", entries, reverse=True)  # 写入顺序不同
    assert build_parts.content_hash_of_zip(z1) == build_parts.content_hash_of_zip(z2)

    z3 = _make_zip(tmp_path / "three.zip", {**entries, "a/x.txt": b"changed"})
    assert build_parts.content_hash_of_zip(z1) != build_parts.content_hash_of_zip(z3)


def test_content_hash_matches_shipped_updater_implementation(tmp_path) -> None:
    """KS 生成端与 SUG Updater 消费端必须逐字节同一算法。"""
    sug_root = Path(__file__).resolve().parents[1] / "krok_helper" / "lyrics_timing"
    sys.path.insert(0, str(sug_root))
    try:
        from updater_app.main import _content_hash_of_zip as updater_hash
    finally:
        sys.path.remove(str(sug_root))
    z = _make_zip(tmp_path / "x.zip", {"_internal/a.dll": b"AA", "app.exe": b"BB"})
    assert build_parts.content_hash_of_zip(z) == updater_hash(z)


# ── targets 计算与 part zip 布局 ─────────────────────────────────────


def _make_app_dir(tmp_path: Path) -> Path:
    app = tmp_path / "Karaoke Studio"
    (app / "_internal" / "krok_helper" / "assets").mkdir(parents=True)
    (app / "_internal" / "strange_uta_game" / "config").mkdir(parents=True)
    (app / "_internal" / "PyQt6").mkdir(parents=True)
    (app / "Karaoke Studio.exe").write_bytes(b"EXE")
    (app / "Updater.exe").write_bytes(b"UPD")
    (app / "_internal" / "base_library.zip").write_bytes(b"LIB")
    (app / "_internal" / "PyQt6" / "Qt6Core.dll").write_bytes(b"QT")
    (app / "_internal" / "krok_helper" / "assets" / "logo.ico").write_bytes(b"ICO")
    (app / "_internal" / "strange_uta_game" / "config" / "c.json").write_bytes(b"{}")
    (app / "_internal" / ".installed_manifest.json").write_text("{}", encoding="utf-8")
    return app


def test_runtime_targets_exclude_app_owned_entries(tmp_path) -> None:
    app = _make_app_dir(tmp_path)
    targets = build_parts.compute_runtime_targets(app)
    assert "_internal/PyQt6" in targets
    assert "_internal/base_library.zip" in targets
    assert "_internal/krok_helper" not in targets
    assert "_internal/strange_uta_game" not in targets
    assert "_internal/.installed_manifest.json" not in targets


def test_pack_part_zip_uses_app_relative_arcnames(tmp_path) -> None:
    app = _make_app_dir(tmp_path)
    zip_path = tmp_path / "app-part.zip"
    build_parts.pack_part_zip(zip_path, app, build_parts.APP_TARGETS)
    with zipfile.ZipFile(str(zip_path)) as zf:
        names = set(zf.namelist())
    assert "Karaoke Studio.exe" in names
    assert "Updater.exe" in names
    assert "_internal/krok_helper/assets/logo.ico" in names
    assert not any(n.startswith("Karaoke Studio/") for n in names)


def test_pack_full_zip_keeps_single_top_dir(tmp_path) -> None:
    app = _make_app_dir(tmp_path)
    zip_path = tmp_path / "full.zip"
    build_parts.pack_full_zip(zip_path, app)
    with zipfile.ZipFile(str(zip_path)) as zf:
        names = zf.namelist()
    assert names and all(n.startswith("Karaoke Studio/") for n in names)


# ── runtime 复用决策 ────────────────────────────────────────────────


def _write_prev(prev_dir: Path, runtime_zip_entries: dict[str, bytes], pkgs: dict, freeze: str) -> None:
    prev_dir.mkdir(parents=True, exist_ok=True)
    rt = _make_zip(prev_dir / "KaraokeStudio-windows-runtime.zip", runtime_zip_entries)
    manifest = {
        "version": "3.1.7.4",
        "schema": 1,
        "parts": {
            "app": {"asset": "a", "sha256": "x", "size": 1, "targets": []},
            "runtime": {
                "asset": rt.name,
                "sha256": build_parts.content_hash_of_zip(rt),
                "size": rt.stat().st_size,
                "targets": ["_internal/PyQt6"],
            },
        },
        "build": {"dist_packages": pkgs, "freeze_hash": freeze},
    }
    (prev_dir / "KaraokeStudio-windows.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )


def test_reuse_runtime_when_fingerprints_match(tmp_path) -> None:
    prev = tmp_path / "prev"
    entries = {"_internal/PyQt6/Qt6Core.dll": b"QT"}
    pkgs = {"numpy": "2.4.6"}
    _write_prev(prev, entries, pkgs, "abc")
    out = tmp_path / "KaraokeStudio-windows-runtime.zip"
    got = build_parts.try_reuse_runtime(prev, out, pkgs, "abc", require_reuse=True)
    assert got is not None
    assert out.exists()
    assert build_parts.content_hash_of_zip(out) == got


@pytest.mark.parametrize(
    ("pkgs_now", "freeze_now"),
    [
        ({"numpy": "2.4.7"}, "abc"),  # dist-info 版本变化
        ({"numpy": "2.4.6"}, "zzz"),  # freeze 指纹变化
        ({"numpy": "2.4.6"}, ""),  # freeze 缺失（保守重打）
    ],
)
def test_rebuild_runtime_when_fingerprint_differs(tmp_path, pkgs_now, freeze_now) -> None:
    prev = tmp_path / "prev"
    _write_prev(prev, {"a": b"1"}, {"numpy": "2.4.6"}, "abc")
    out = tmp_path / "rt.zip"
    assert build_parts.try_reuse_runtime(prev, out, pkgs_now, freeze_now, require_reuse=False) is None


def test_reuse_without_prev_manifest_is_fresh_build(tmp_path) -> None:
    out = tmp_path / "rt.zip"
    assert build_parts.try_reuse_runtime(tmp_path / "prev", out, {"n": "1"}, "abc", require_reuse=True) is None


def test_require_reuse_fails_when_zip_missing(tmp_path) -> None:
    prev = tmp_path / "prev"
    _write_prev(prev, {"a": b"1"}, {"numpy": "2.4.6"}, "abc")
    (prev / "KaraokeStudio-windows-runtime.zip").unlink()
    with pytest.raises(SystemExit):
        build_parts.try_reuse_runtime(prev, tmp_path / "rt.zip", {"numpy": "2.4.6"}, "abc", require_reuse=True)


def test_require_reuse_fails_on_corrupted_prev_zip(tmp_path) -> None:
    prev = tmp_path / "prev"
    _write_prev(prev, {"a": b"1"}, {"numpy": "2.4.6"}, "abc")
    _make_zip(prev / "KaraokeStudio-windows-runtime.zip", {"a": b"tampered"})
    with pytest.raises(SystemExit):
        build_parts.try_reuse_runtime(prev, tmp_path / "rt.zip", {"numpy": "2.4.6"}, "abc", require_reuse=True)


# ── 清单结构 ────────────────────────────────────────────────────────


def test_manifest_and_installed_manifest_contract(tmp_path) -> None:
    app = _make_app_dir(tmp_path)
    app_zip = _make_zip(tmp_path / "KaraokeStudio-windows-app.zip", {"Karaoke Studio.exe": b"E"})
    parts = {
        "app": build_parts.part_payload(app_zip, build_parts.APP_TARGETS, "h1"),
        "runtime": build_parts.part_payload(app_zip, ["_internal/PyQt6"], "h2"),
    }
    local = build_parts.write_installed_manifest(app, "9.9.9", parts)
    payload = json.loads(local.read_text(encoding="utf-8"))
    assert payload["schema"] == 1
    assert payload["parts"]["app"]["sha256"] == "h1"
    assert payload["parts"]["app"]["targets"] == build_parts.APP_TARGETS
    assert "installed_at" in payload

    full = _make_zip(tmp_path / "KaraokeStudio-windows.zip", {"Karaoke Studio/x": b"1"})
    manifest_path = build_parts.write_release_manifest(
        tmp_path, "9.9.9", parts, full, {"numpy": "2.4.6"}, "abc"
    )
    assert manifest_path.name == "KaraokeStudio-windows.json"
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert data["schema"] == 1
    assert data["full"]["asset"] == "KaraokeStudio-windows.zip"
    assert data["parts"]["runtime"]["sha256"] == "h2"
    assert data["build"]["dist_packages"] == {"numpy": "2.4.6"}


def test_derived_manifest_name_matches_shipped_updater_rule() -> None:
    """存量 Updater 的派生规则算出的名字必须等于我们上传的 manifest 名。"""
    asset = "KaraokeStudio-windows.zip"
    derived = asset.replace("StrangeUtaGame", "manifest", 1).replace(".zip", ".json")
    assert derived == f"{build_parts.ASSET_BASE}.json"
