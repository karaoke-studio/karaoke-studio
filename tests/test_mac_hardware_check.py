"""Platform-independent parts of scripts/mac_hardware_check.py."""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import stat
import sys
import tarfile
import zipfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import mac_hardware_check as check  # noqa: E402


def make_bundle(tmp_path: Path, *, corrupt: bool = False) -> Path:
    tool = b"#!/bin/sh\necho ok\n"
    manifest = {"commit": "abc", "arch": "arm64",
                "files": {"native/macos-arm64-metal/krok_skia_gpu_smoke": hashlib.sha256(tool).hexdigest()}}
    tar_path = tmp_path / "mac-validation-arm64.tar.gz"
    with tarfile.open(tar_path, "w:gz") as bundle:
        for name, data, mode in (("native/macos-arm64-metal/krok_skia_gpu_smoke",
                                  tool + (b"x" if corrupt else b""), 0o755),
                                 ("manifest.json", json.dumps(manifest).encode(), 0o644)):
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), mode
            bundle.addfile(info, io.BytesIO(data))
    # GitHub Actions wraps uploaded files in its own zip, which drops permissions.
    outer = tmp_path / "mac-validation-arm64.zip"
    with zipfile.ZipFile(outer, "w") as archive:
        archive.write(tar_path, tar_path.name)
    return outer


def test_unpacks_the_github_zip_and_keeps_tools_executable(tmp_path):
    bundle = check.unpack(make_bundle(tmp_path), tmp_path / "work")
    tool = bundle / "native/macos-arm64-metal/krok_skia_gpu_smoke"
    assert check.verify(bundle)["arch"] == "arm64"
    if sys.platform != "win32":
        assert tool.stat().st_mode & stat.S_IXUSR


def test_rejects_a_modified_bundle(tmp_path):
    bundle = check.unpack(make_bundle(tmp_path, corrupt=True), tmp_path / "work")
    with pytest.raises(SystemExit, match="corrupted or modified"):
        check.verify(bundle)


def test_gpu_summary_keeps_only_whitelisted_fields():
    displays = {"SPDisplaysDataType": [{"sppci_model": "Apple M2", "sppci_cores": "10",
                                         "spdisplays_mtlgpufamilysupport": "spdisplays_metal3",
                                         "spdisplays_serial_number": "SECRET", "_name": "x"}]}
    assert check.gpu_summary(json.dumps(displays)) == [
        {"sppci_model": "Apple M2", "sppci_cores": "10", "spdisplays_mtlgpufamilysupport": "spdisplays_metal3"}]
    assert "error" in check.gpu_summary("not json")[0]
