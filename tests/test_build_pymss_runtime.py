from __future__ import annotations

import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from krok_helper.audio_processing.separation.runtime import RuntimePackage
from krok_helper.audio_processing.separation.integration import (
    PYMSS_CORE_VERSION,
    PYMSS_EMBEDDED_PYTHON_SHA256,
    PYMSS_EMBEDDED_PYTHON_VERSION,
    PYMSS_PYTHON_VERSION,
    PYMSS_RUNTIME_VERSION,
    PYMSS_TORCH_VERSION,
    PYMSS_VERSION,
)
from scripts.build_pymss_runtime import package_runtime
from scripts.build_parts import APP_TARGETS, compute_runtime_targets


def test_runtime_builder_prints_the_application_contract() -> None:
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, str(root / "scripts" / "build_pymss_runtime.py"), "--print-contract"],
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(result.stdout) == {
        "pymss_version": PYMSS_VERSION,
        "pymss_core_version": PYMSS_CORE_VERSION,
        "runtime_version": PYMSS_RUNTIME_VERSION,
        "python_abi_version": PYMSS_PYTHON_VERSION,
        "embedded_python_version": PYMSS_EMBEDDED_PYTHON_VERSION,
        "embedded_python_sha256": PYMSS_EMBEDDED_PYTHON_SHA256,
        "torch_version": PYMSS_TORCH_VERSION,
    }


def test_runtime_publish_tag_is_derived_from_built_manifests() -> None:
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github" / "workflows" / "pymss-runtime.yml").read_text(
        encoding="utf-8"
    )

    assert "runtime_tag=\"pymss-runtime-v${pymss_version}-r${runtime_version}\"" in workflow
    assert "RUNTIME_TAG: pymss-runtime-v" not in workflow
    assert "--clobber" not in workflow
    assert "RUNTIME_REPOSITORY: karaoke-studio/karaoke-studio-runtime" in workflow
    assert "secrets.PYMSS_RUNTIME_RELEASE_TOKEN" in workflow
    assert '--repo "$RUNTIME_REPOSITORY"' in workflow
    assert "请递增 PYMSS_RUNTIME_VERSION" in workflow


def test_runtime_packager_streams_valid_multipart_zip(tmp_path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "python.exe").write_bytes(b"portable-python")
    library = runtime / "Lib" / "site-packages" / "demo" / "payload.bin"
    library.parent.mkdir(parents=True)
    library.write_bytes(bytes(range(256)) * 20)
    ignored = runtime / "Lib" / "site-packages" / "demo" / "__pycache__" / "x.pyc"
    ignored.parent.mkdir()
    ignored.write_bytes(b"ignored")
    output = tmp_path / "dist"

    manifest_path = package_runtime(
        runtime,
        output,
        variant="windows-cpu",
        release_base="https://example.invalid/release",
        part_size=300,
    )

    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    package = RuntimePackage.from_payload(payload)
    assert len(package.archive_parts) > 1
    assert all(part.size <= 300 for part in package.archive_parts)
    assert not any("__pycache__" in item.path for item in package.files)
    reassembled = tmp_path / "runtime.zip"
    with reassembled.open("wb") as stream:
        for index in range(1, len(package.archive_parts) + 1):
            stream.write(next(output.glob(f"*.zip.{index:03d}")).read_bytes())
    with zipfile.ZipFile(reassembled) as bundle:
        assert bundle.read("runtime/python.exe") == b"portable-python"
        assert bundle.read("runtime/Lib/site-packages/demo/payload.bin") == library.read_bytes()


def test_runtime_packager_rejects_bundled_torch(tmp_path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "python.exe").write_bytes(b"portable-python")
    torch_dir = runtime / "Lib" / "site-packages" / "torch"
    torch_dir.mkdir(parents=True)
    (torch_dir / "__init__.py").write_text("", encoding="utf-8")

    with pytest.raises(ValueError, match="must not contain torch"):
        package_runtime(runtime, tmp_path / "dist", variant="windows-cpu")


def test_main_app_builds_exclude_pymss_torch_and_private_pip() -> None:
    root = Path(__file__).resolve().parents[1]
    for relative in ("scripts/build_windows.bat", "scripts/build_macos.command"):
        content = (root / relative).read_text(encoding="utf-8")
        for module in ("torch", "pymss", "pymss_core", "pip"):
            assert f"--exclude-module {module}" in content or f'--exclude-module "$module"' in content
        assert "torch-" in content
        assert "pymss-" in content
        assert "pip-" in content


def test_managed_pymss_root_is_not_owned_by_application_update_parts(tmp_path) -> None:
    app_dir = tmp_path / "KaraokeStudio"
    internal = app_dir / "_internal"
    internal.mkdir(parents=True)
    (internal / "runtime.dll").write_bytes(b"runtime")
    user_model = app_dir / "pymss" / "models" / "user.ckpt"
    user_model.parent.mkdir(parents=True)
    user_model.write_bytes(b"user-owned")

    targets = [*APP_TARGETS, *compute_runtime_targets(app_dir)]

    assert all(not target.lower().startswith("pymss") for target in targets)
    assert user_model.read_bytes() == b"user-owned"
