"""Dependency integrity and platform selection checks; no Qt renderer oracle."""
from __future__ import annotations

import json
import hashlib
from pathlib import Path
import re
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from skia_build.configuration import gn_text, validate_selection  # noqa: E402
from skia_build.sources import checkout, download, sha256  # noqa: E402
from skia_build.provenance import native_needs_clean, record_request, renderer_digest  # noqa: E402


@pytest.mark.parametrize("system,arch,api,cpu", [
    ("windows", "x64", "vulkan", False), ("linux", "x64", "vulkan", False),
    ("macos", "arm64", "metal", False), ("macos", "x64", "metal", False),
    ("windows", "x64", None, True), ("linux", "x64", None, True),
    ("macos", "arm64", None, True),
])
def test_supported_selections(system, arch, api, cpu):
    validate_selection(system, arch, api, cpu)


@pytest.mark.parametrize("system,arch,api,cpu", [
    ("windows", "x64", "metal", False), ("macos", "arm64", "vulkan", False),
    ("linux", "x64", "vulkan", True), ("windows", "x64", None, False),
    ("linux", "arm64", "vulkan", False),
])
def test_invalid_selections_fail_closed(system, arch, api, cpu):
    with pytest.raises(ValueError):
        validate_selection(system, arch, api, cpu)


def test_lock_uses_immutable_official_source_and_tool_digests():
    lock = json.loads((ROOT / "native/subtitle_renderer/skia.lock.json").read_text())
    assert lock["skia"]["url"] == "https://skia.googlesource.com/skia.git"
    for repo in [lock["skia"], *lock["dependencies"]]:
        assert re.fullmatch("[0-9a-f]{40}", repo["revision"])
        assert repo["url"].startswith("https://")
    for platform in lock["tools"].values():
        for tool in platform.values():
            assert re.fullmatch("[0-9a-f]{64}", tool["sha256"])
            assert re.fullmatch("[0-9a-f]{64}", tool["binary_sha256"])
    assert lock["toolchains"]["windows-x64"]["crt"] == "MD"
    assert lock["common_args"]["skia_use_gl"] is False
    assert lock["common_args"]["skia_use_freetype"] is True


def test_download_rejects_changed_cached_bytes(tmp_path):
    upstream = tmp_path / "upstream.bin"
    upstream.write_bytes(b"verified dependency")
    spec = {"url": upstream.as_uri(), "sha256": sha256(upstream)}
    cached = download(tmp_path / "cache", "test.bin", spec)
    assert cached.read_bytes() == upstream.read_bytes()
    cached.write_bytes(b"corrupted")
    with pytest.raises(RuntimeError, match="Cached download digest mismatch"):
        download(tmp_path / "cache", "test.bin", spec)


def test_download_never_publishes_unverified_bytes(tmp_path):
    upstream = tmp_path / "upstream.bin"
    upstream.write_bytes(b"unexpected")
    with pytest.raises(RuntimeError, match="Download digest mismatch"):
        download(tmp_path / "cache", "test.bin", {"url": upstream.as_uri(), "sha256": "0" * 64})
    assert not (tmp_path / "cache/test.bin").exists()


def test_checkout_preserves_modified_source(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    def git(*args):
        return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()
    git("init")
    git("remote", "add", "origin", "https://example.invalid/pinned.git")
    (repo / "source.cpp").write_text("original")
    git("add", "source.cpp")
    git("-c", "user.name=Build test", "-c", "user.email=build@example.invalid", "commit", "-m", "seed")
    revision = git("rev-parse", "HEAD")
    (repo / "source.cpp").write_text("local work")
    with pytest.raises(RuntimeError, match="Modified dependency source"):
        checkout(repo, {"url": "https://example.invalid/pinned.git", "revision": revision})
    assert (repo / "source.cpp").read_text() == "local work"


def test_gn_strings_escape_windows_paths_and_quotes():
    result = gn_text({"path": 'C:/Program Files/LLVM', "enabled": False, "flags": ['/DVALUE="x"']})
    assert 'enabled = false\n' in result
    assert 'path = "C:/Program Files/LLVM"\n' in result
    assert r'\"x\"' in result


def test_common_core_and_raster_do_not_include_native_gpu_or_qt():
    forbidden = re.compile(r"^\s*#\s*(?:include|import)\s*[<\"](?:Q[A-Z]|Qt|windows[.]h|vulkan/|Metal/|gpu/)", re.M)
    for directory in ("core", "raster"):
        for path in (ROOT / "native/subtitle_renderer/src/skia" / directory).glob("*"):
            assert not forbidden.search(path.read_text(encoding="utf-8")), path


def test_build_cli_rejects_incompatible_gpu_before_fetching():
    result = subprocess.run([sys.executable, str(ROOT / "scripts/build_skia_renderer.py"),
                             "--cpu-only", "--gpu-api", "vulkan"], capture_output=True, text=True)
    assert result.returncode == 2
    assert "cannot be combined" in result.stderr


def test_failed_dependency_request_still_requires_clean_on_toolchain_change(tmp_path):
    path = tmp_path / "request.json"
    record_request(path, "first", clean=False)
    record_request(path, "first", clean=False)
    with pytest.raises(RuntimeError, match="--clean"):
        record_request(path, "different", clean=False)
    assert json.loads(path.read_text())["fingerprint"] == "first"
    record_request(path, "different", clean=True)


def test_native_source_change_triggers_clean_once(tmp_path):
    path = tmp_path / "source-request.json"
    assert native_needs_clean(path, "headers-v1")
    assert not native_needs_clean(path, "headers-v1")
    assert native_needs_clean(path, "headers-v2")


def test_renderer_digest_has_os_independent_case_sensitive_path_order(tmp_path):
    files = {"native/subtitle_renderer/CMakeLists.txt": b"a",
             "native/subtitle_renderer/cmake/z.cmake": b"b", "scripts/build_skia_renderer.py": b"c"}
    for name, data in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    canonical = (b"native/subtitle_renderer/CMakeLists.txt\0a\0"
                 b"native/subtitle_renderer/cmake/z.cmake\0b\0scripts/build_skia_renderer.py\0c\0")
    assert renderer_digest(tmp_path) == hashlib.sha256(canonical).hexdigest()
