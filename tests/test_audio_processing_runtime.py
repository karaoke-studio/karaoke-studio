from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import Path

import pytest

from krok_helper.audio_processing.separation.integration import (
    PYMSS_PYTHON_VERSION,
    PYMSS_RUNTIME_VERSION,
    PYMSS_TORCH_VERSION,
    PYMSS_VERSION,
    TORCH_WHEELS,
)
from krok_helper.audio_processing.separation.runtime import (
    ManagedRuntimeInstaller,
    RuntimePackage,
    RuntimeStatus,
    RuntimeValidation,
    fetch_runtime_package,
    preflight_install_destination,
    validate_runtime,
)
from krok_helper.audio_processing.separation import runtime as runtime_module


class _Response:
    def __init__(self, body: bytes, *, status_code: int = 200) -> None:
        self.body = body
        self.status_code = status_code

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def raise_for_status(self) -> None:
        return None

    def iter_content(self, chunk_size: int):
        for offset in range(0, len(self.body), chunk_size):
            yield self.body[offset : offset + chunk_size]


class _Session:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def get(self, _url, **_kwargs):
        return _Response(self.body)


class _PartSession:
    def __init__(self, bodies: dict[str, bytes]) -> None:
        self.bodies = bodies

    def get(self, url, **_kwargs):
        return _Response(self.bodies[url])


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _archive(files: dict[str, bytes]) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as bundle:
        for name, data in files.items():
            bundle.writestr(name, data)
    return stream.getvalue()


def _production_manifest() -> dict:
    return {
        "schema": 1,
        "runtime_version": PYMSS_RUNTIME_VERSION,
        "pymss_version": PYMSS_VERSION,
        "python_version": PYMSS_PYTHON_VERSION,
        "variant": "windows-cpu",
        "archive": {
            "url": "https://example.invalid/base.zip",
            "size": 1,
            "sha256": "1" * 64,
        },
        "torch": {
            "version": PYMSS_TORCH_VERSION,
            "wheel": dict(TORCH_WHEELS["windows-cpu"]),
        },
        "files": [
            {"path": "runtime/python.exe", "size": 1, "sha256": "2" * 64}
        ],
    }


def _package(archive: bytes, files: dict[str, bytes]) -> RuntimePackage:
    return RuntimePackage.from_payload(
        {
            "schema": 1,
            "runtime_version": PYMSS_RUNTIME_VERSION,
            "pymss_version": PYMSS_VERSION,
            "python_version": PYMSS_PYTHON_VERSION,
            "variant": "windows-cpu",
            "archive": {
                "url": "https://example.invalid/runtime.zip",
                "size": len(archive),
                "sha256": _sha(archive),
            },
            "files": [
                {"path": name, "size": len(data), "sha256": _sha(data)}
                for name, data in files.items()
            ],
        }
    )


def test_runtime_package_rejects_paths_outside_runtime() -> None:
    with pytest.raises(ValueError, match="不安全路径"):
        RuntimePackage.from_payload(
            {
                "schema": 1,
                "runtime_version": PYMSS_RUNTIME_VERSION,
                "pymss_version": PYMSS_VERSION,
                "python_version": PYMSS_PYTHON_VERSION,
                "variant": "windows-cpu",
                "archive": {"url": "x", "size": 1, "sha256": "0" * 64},
                "files": [{"path": "../escape.exe", "size": 1, "sha256": "0" * 64}],
            }
        )


def test_fetch_runtime_requires_exact_official_torch_and_torch_free_base() -> None:
    class ManifestResponse:
        def __init__(self, payload) -> None:
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    class ManifestSession:
        def __init__(self, payload) -> None:
            self.payload = payload

        def get(self, *_args, **_kwargs):
            return ManifestResponse(self.payload)

    payload = _production_manifest()
    assert fetch_runtime_package("https://example.invalid/manifest", session=ManifestSession(payload))

    changed = json.loads(json.dumps(payload))
    changed["torch"]["wheel"]["url"] = "https://example.invalid/not-official.whl"
    with pytest.raises(ValueError, match="官方文件不一致"):
        fetch_runtime_package("https://example.invalid/manifest", session=ManifestSession(changed))

    bundled = json.loads(json.dumps(payload))
    bundled["files"].append(
        {
            "path": "runtime/Lib/site-packages/torch/__init__.py",
            "size": 1,
            "sha256": "3" * 64,
        }
    )
    with pytest.raises(ValueError, match="底座清单不得包含 torch"):
        fetch_runtime_package("https://example.invalid/manifest", session=ManifestSession(bundled))


def test_fetch_runtime_rejects_old_runtime_revision_and_python_abi() -> None:
    class ManifestResponse:
        def __init__(self, payload) -> None:
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    class ManifestSession:
        def __init__(self, payload) -> None:
            self.payload = payload

        def get(self, *_args, **_kwargs):
            return ManifestResponse(self.payload)

    old_revision = _production_manifest()
    old_revision["runtime_version"] = "obsolete"
    with pytest.raises(ValueError, match="运行时清单修订不匹配"):
        fetch_runtime_package(
            "https://example.invalid/manifest", session=ManifestSession(old_revision)
        )

    wrong_python = _production_manifest()
    wrong_python["python_version"] = "3.11"
    with pytest.raises(ValueError, match="工作台要求"):
        fetch_runtime_package(
            "https://example.invalid/manifest", session=ManifestSession(wrong_python)
        )


def test_validation_marks_old_runtime_contract_incompatible(tmp_path) -> None:
    files = {"runtime/python.exe": b"python-runtime"}
    archive = _archive(files)
    package = _package(archive, files)
    install_dir = tmp_path / "pymss"
    ManagedRuntimeInstaller(_Session(archive)).install(package, install_dir)
    manifest = install_dir / "manifests" / "runtime-manifest.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))

    payload["runtime_version"] = "obsolete"
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    result = validate_runtime(install_dir)
    assert result.status is RuntimeStatus.INCOMPATIBLE
    assert f"r{PYMSS_RUNTIME_VERSION}" in result.message

    payload["runtime_version"] = PYMSS_RUNTIME_VERSION
    payload["python_version"] = "3.11"
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    result = validate_runtime(install_dir)
    assert result.status is RuntimeStatus.INCOMPATIBLE
    assert PYMSS_PYTHON_VERSION in result.message


def test_managed_install_preserves_models_and_writes_complete_manifest(tmp_path) -> None:
    files = {
        "runtime/python.exe": b"python-runtime",
        "runtime/Scripts/pymss.exe": b"pymss-entry",
    }
    archive = _archive(files)
    package = _package(archive, files)
    install_dir = tmp_path / "pymss"
    model = install_dir / "models" / "existing.ckpt"
    model.parent.mkdir(parents=True)
    model.write_bytes(b"user-model")
    external = install_dir / "manifests" / "external-models.json"
    external.parent.mkdir(parents=True)
    external.write_text('{"models": []}', encoding="utf-8")

    result = ManagedRuntimeInstaller(_Session(archive)).install(package, install_dir)

    assert result.status is RuntimeStatus.READY
    assert (install_dir / "runtime" / "python.exe").read_bytes() == b"python-runtime"
    assert model.read_bytes() == b"user-model"
    assert external.read_text(encoding="utf-8") == '{"models": []}'
    payload = json.loads(
        (install_dir / "manifests" / "runtime-manifest.json").read_text(encoding="utf-8")
    )
    assert payload["complete"] is True
    assert payload["pymss_version"] == PYMSS_VERSION


def test_validation_distinguishes_missing_and_damaged_runtime(tmp_path) -> None:
    missing = validate_runtime(tmp_path / "missing")
    assert missing.status is RuntimeStatus.MISSING

    files = {"runtime/python.exe": b"python-runtime"}
    archive = _archive(files)
    package = _package(archive, files)
    install_dir = tmp_path / "pymss"
    ManagedRuntimeInstaller(_Session(archive)).install(package, install_dir)
    (install_dir / "runtime" / "python.exe").write_bytes(b"bad")

    damaged = validate_runtime(install_dir)
    assert damaged.status is RuntimeStatus.DAMAGED
    assert damaged.damaged == ("runtime/python.exe",)


def test_install_destination_preflight_exercises_write_and_rejects_unsafe_paths(
    tmp_path, monkeypatch
) -> None:
    destination = tmp_path / "chosen" / "pymss"
    assert preflight_install_destination(destination) == destination.resolve()
    assert destination.is_dir()
    assert not list(destination.glob(".pymss-write-probe-*"))

    file_target = tmp_path / "not-a-directory"
    file_target.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="指向了文件"):
        preflight_install_destination(file_target)
    with pytest.raises(ValueError, match="_internal"):
        preflight_install_destination(tmp_path / "_internal" / "pymss")

    updater_temp = tmp_path / "KaraokeStudioUpdater"
    monkeypatch.setattr(runtime_module.tempfile, "gettempdir", lambda: str(tmp_path))
    with pytest.raises(ValueError, match="更新器"):
        preflight_install_destination(updater_temp / "pymss")


def test_failed_reinstall_keeps_previous_runtime(tmp_path) -> None:
    files = {"runtime/python.exe": b"old-good-runtime"}
    good_archive = _archive(files)
    package = _package(good_archive, files)
    install_dir = tmp_path / "pymss"
    ManagedRuntimeInstaller(_Session(good_archive)).install(package, install_dir)

    broken_archive = _archive({"runtime/python.exe": b"different"})
    with pytest.raises(ValueError, match="大小不符|校验"):
        ManagedRuntimeInstaller(_Session(broken_archive)).install(package, install_dir)

    assert (install_dir / "runtime" / "python.exe").read_bytes() == b"old-good-runtime"
    assert validate_runtime(install_dir, full=True).status is RuntimeStatus.READY


def test_post_switch_validation_failure_restores_runtime_and_manifest(
    tmp_path, monkeypatch
) -> None:
    old_files = {"runtime/python.exe": b"old-good-runtime"}
    old_archive = _archive(old_files)
    install_dir = tmp_path / "pymss"
    ManagedRuntimeInstaller(_Session(old_archive)).install(
        _package(old_archive, old_files), install_dir
    )
    old_manifest = (
        install_dir / "manifests" / "runtime-manifest.json"
    ).read_bytes()

    new_files = {"runtime/python.exe": b"new-good-runtime"}
    new_archive = _archive(new_files)
    monkeypatch.setattr(
        runtime_module,
        "validate_runtime",
        lambda *_args, **_kwargs: RuntimeValidation(
            RuntimeStatus.DAMAGED, "模拟最终复检失败"
        ),
    )

    with pytest.raises(RuntimeError, match="切换后复检失败"):
        ManagedRuntimeInstaller(_Session(new_archive)).install(
            _package(new_archive, new_files), install_dir
        )

    assert (install_dir / "runtime" / "python.exe").read_bytes() == b"old-good-runtime"
    assert (
        install_dir / "manifests" / "runtime-manifest.json"
    ).read_bytes() == old_manifest


def test_post_install_service_smoke_failure_restores_runtime_and_manifest(
    tmp_path,
) -> None:
    old_files = {"runtime/python.exe": b"old-good-runtime"}
    old_archive = _archive(old_files)
    install_dir = tmp_path / "pymss"
    ManagedRuntimeInstaller(_Session(old_archive)).install(
        _package(old_archive, old_files), install_dir
    )
    old_manifest = (
        install_dir / "manifests" / "runtime-manifest.json"
    ).read_bytes()

    new_files = {"runtime/python.exe": b"new-good-runtime"}
    new_archive = _archive(new_files)

    def failed_smoke(_install_dir: Path) -> None:
        raise RuntimeError("模拟服务启动冒烟失败")

    with pytest.raises(RuntimeError, match="模拟服务启动冒烟失败"):
        ManagedRuntimeInstaller(_Session(new_archive)).install(
            _package(new_archive, new_files),
            install_dir,
            post_install_check=failed_smoke,
        )

    assert (install_dir / "runtime" / "python.exe").read_bytes() == b"old-good-runtime"
    assert (
        install_dir / "manifests" / "runtime-manifest.json"
    ).read_bytes() == old_manifest


def test_managed_install_reassembles_verified_archive_parts(tmp_path) -> None:
    files = {"runtime/python.exe": b"multipart-runtime"}
    archive = _archive(files)
    midpoint = len(archive) // 2
    parts = [archive[:midpoint], archive[midpoint:]]
    package = RuntimePackage.from_payload(
        {
            "schema": 1,
            "runtime_version": PYMSS_RUNTIME_VERSION,
            "pymss_version": PYMSS_VERSION,
            "python_version": PYMSS_PYTHON_VERSION,
            "variant": "windows-cu128",
            "archive": {
                "size": len(archive),
                "sha256": _sha(archive),
                "parts": [
                    {
                        "url": f"https://example.invalid/part-{index}",
                        "size": len(body),
                        "sha256": _sha(body),
                    }
                    for index, body in enumerate(parts)
                ],
            },
            "files": [
                {"path": name, "size": len(data), "sha256": _sha(data)}
                for name, data in files.items()
            ],
        }
    )
    session = _PartSession(
        {
            f"https://example.invalid/part-{index}": body
            for index, body in enumerate(parts)
        }
    )

    result = ManagedRuntimeInstaller(session).install(package, tmp_path / "pymss")

    assert result.status is RuntimeStatus.READY
    assert (tmp_path / "pymss" / "runtime" / "python.exe").read_bytes() == files[
        "runtime/python.exe"
    ]


def test_torch_is_installed_by_private_runtime_pip_and_added_to_integrity_manifest(
    tmp_path, monkeypatch
) -> None:
    files = {
        "runtime/python.exe": b"private-python",
        "runtime/Lib/site-packages/pip/__init__.py": b"private-pip",
    }
    archive = _archive(files)
    wheel = b"official-torch-wheel"
    payload = {
        "schema": 1,
        "runtime_version": PYMSS_RUNTIME_VERSION,
        "pymss_version": PYMSS_VERSION,
        "python_version": PYMSS_PYTHON_VERSION,
        "variant": "windows-cpu",
        "archive": {
            "url": "https://example.invalid/base.zip",
            "size": len(archive),
            "sha256": _sha(archive),
        },
        "torch": {
            "version": PYMSS_TORCH_VERSION,
            "wheel": {
                "url": "https://download.pytorch.org/fake.whl",
                "filename": "torch-2.7.1+cpu-cp312-cp312-win_amd64.whl",
                "size": len(wheel),
                "sha256": _sha(wheel),
            },
        },
        "files": [
            {"path": name, "size": len(data), "sha256": _sha(data)}
            for name, data in files.items()
        ],
    }
    package = RuntimePackage.from_payload(payload)

    class FakePopen:
        returncode = 0

        def __init__(self, command, **_kwargs) -> None:
            target = Path(command[command.index("--target") + 1])
            installed = target / "torch" / "version.py"
            installed.parent.mkdir(parents=True)
            installed.write_bytes(b"__version__ = '2.7.1+cpu'")

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = 1

        def kill(self):
            self.returncode = -9

        def wait(self, timeout=None):
            del timeout
            return self.returncode

    class Completed:
        returncode = 0

    monkeypatch.setattr(runtime_module.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(runtime_module.subprocess, "run", lambda *_a, **_k: Completed())
    real_unlink = Path.unlink

    def keep_download_wheel_locked(path, *args, **kwargs):
        if path.name == "torch-2.7.1+cpu-cp312-cp312-win_amd64.whl":
            raise PermissionError(32, "file is in use", str(path))
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", keep_download_wheel_locked)
    monkeypatch.setattr(runtime_module.time, "sleep", lambda _seconds: None)
    session = _PartSession(
        {
            "https://example.invalid/base.zip": archive,
        }
    )
    install_dir = tmp_path / "pymss"
    staging = install_dir / "staging"
    staging.mkdir(parents=True)
    cached_wheel = staging / "torch-2.7.1+cpu-cp312-cp312-win_amd64.whl"
    cached_wheel.write_bytes(wheel)
    duplicate_part = cached_wheel.with_name(f"{cached_wheel.name}.part")
    duplicate_part.write_bytes(wheel)

    result = ManagedRuntimeInstaller(session).install(package, install_dir)

    assert result.status is RuntimeStatus.READY
    assert (
        install_dir
        / "staging"
        / cached_wheel.name
    ).is_file()
    assert not duplicate_part.exists()
    torch_file = install_dir / "runtime" / "Lib" / "site-packages" / "torch" / "version.py"
    assert torch_file.is_file()
    torch_file.unlink()
    damaged = validate_runtime(install_dir)
    assert damaged.status is RuntimeStatus.DAMAGED
    assert "runtime/Lib/site-packages/torch/version.py" in damaged.missing


def test_torch_wheel_download_resumes_a_verified_partial_file(tmp_path) -> None:
    files = {"runtime/python.exe": b"private-python"}
    archive = _archive(files)
    wheel = b"official-torch-wheel-payload"
    package = RuntimePackage.from_payload(
        {
            "schema": 1,
            "runtime_version": PYMSS_RUNTIME_VERSION,
            "pymss_version": PYMSS_VERSION,
            "python_version": PYMSS_PYTHON_VERSION,
            "variant": "windows-cpu",
            "archive": {
                "url": "https://example.invalid/base.zip",
                "size": len(archive),
                "sha256": _sha(archive),
            },
            "torch": {
                "version": PYMSS_TORCH_VERSION,
                "wheel": {
                    "url": "https://download.pytorch.org/fake.whl",
                    "filename": "torch-2.7.1+cpu-cp312-cp312-win_amd64.whl",
                    "size": len(wheel),
                    "sha256": _sha(wheel),
                },
            },
            "files": [
                {"path": name, "size": len(data), "sha256": _sha(data)}
                for name, data in files.items()
            ],
        }
    )
    split = 11
    destination = tmp_path / "torch.whl.part"
    destination.write_bytes(wheel[:split])

    class ResumeSession:
        headers = None

        def get(self, _url, **kwargs):
            self.headers = kwargs.get("headers")
            return _Response(wheel[split:], status_code=206)

    session = ResumeSession()
    installer = ManagedRuntimeInstaller(session)
    installer._download_wheel(package, destination)

    assert session.headers == {"Range": f"bytes={split}-"}
    assert destination.read_bytes() == wheel
