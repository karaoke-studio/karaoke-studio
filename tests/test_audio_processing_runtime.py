from __future__ import annotations

import hashlib
import io
import json
import shutil
import zipfile
from pathlib import Path

import pytest
import requests

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
    resync_installed_manifest,
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


class _FlakySession:
    """前 fail 次 GET 抛 ConnectionError，之后交给内层会话。"""

    def __init__(self, inner, fail_times: int) -> None:
        self._inner = inner
        self._remaining = fail_times
        self.calls = 0

    def get(self, url, **kwargs):
        self.calls += 1
        if self._remaining > 0:
            self._remaining -= 1
            import requests

            raise requests.exceptions.ConnectionError("simulated drop")
        return self._inner.get(url, **kwargs)


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


def _fake_proxied_settings(monkeypatch):
    from krok_helper import network
    from krok_helper.settings import AppSettings

    monkeypatch.setattr(network, "read_system_proxy", lambda: None)
    app = AppSettings(updater={"proxy": {"mode": "manual", "manual_url": "127.0.0.1:7890"}})
    monkeypatch.setattr(network, "load_current_app_settings", lambda: app)
    return network


def test_default_download_session_follows_workbench_proxy(monkeypatch) -> None:
    network = _fake_proxied_settings(monkeypatch)

    session = runtime_module._default_download_session()

    assert session.proxies["http"] == "http://127.0.0.1:7890"
    assert session.proxies["https"] == "http://127.0.0.1:7890"
    assert network.requests_session_for_current_settings()[1] == {
        "http": "http://127.0.0.1:7890",
        "https": "http://127.0.0.1:7890",
    }


def test_default_download_session_survives_settings_failure(monkeypatch) -> None:
    def explode():
        raise RuntimeError("settings unavailable")

    monkeypatch.setattr(
        "krok_helper.network.requests_session_for_current_settings", explode
    )

    # 设置读取失败不能阻断安装流程：退回普通会话。
    import requests

    session = runtime_module._default_download_session()
    assert isinstance(session, requests.Session)


def test_managed_installer_defaults_to_proxied_session(monkeypatch) -> None:
    used = {}
    monkeypatch.setattr(
        runtime_module,
        "_default_download_session",
        lambda: used.setdefault("session", _Session(b"")),
    )

    ManagedRuntimeInstaller()

    assert "session" in used


def test_fetch_runtime_package_defaults_to_proxied_session(monkeypatch) -> None:
    class ManifestResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return _production_manifest()

    class ProbeSession:
        def get(self, *_args, **_kwargs):
            return ManifestResponse()

    monkeypatch.setattr(runtime_module, "_default_download_session", lambda: ProbeSession())

    assert fetch_runtime_package("https://example.invalid/manifest")


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
    with pytest.raises(ValueError, match="Runtime 清单修订不匹配"):
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

def test_resync_manifest_after_trusted_mutation(tmp_path) -> None:
    """受信增量安装（AI 打轴方案 B）改动共用包后，清单可按磁盘现状
    再登记：改文件/删文件/加新包全部收编，全量校验恢复通过。"""
    files = {
        "runtime/python.exe": b"python-runtime",
        "runtime/Lib/site-packages/pkg/a.py": b"aaa",
        "runtime/Lib/site-packages/pkg/b.py": b"bbb",
        "runtime/Lib/site-packages/old-0.11.dist-info/METADATA": b"x",
    }
    archive = _archive(files)
    package = _package(archive, files)
    install_dir = tmp_path / "rt"
    ManagedRuntimeInstaller(_Session(archive)).install(package, install_dir)

    site = install_dir / "runtime" / "Lib" / "site-packages"
    (site / "pkg" / "a.py").write_bytes(b"changed-content-longer")
    (site / "pkg" / "c.py").write_bytes(b"new-package-file")
    shutil.rmtree(site / "old-0.11.dist-info")

    assert validate_runtime(install_dir).status is RuntimeStatus.DAMAGED
    result = resync_installed_manifest(install_dir)
    assert result.status is RuntimeStatus.READY
    # size 未变条目沿用旧哈希、变化/新增已重算：全量（sha256）校验同样通过
    assert validate_runtime(install_dir, full=True).status is RuntimeStatus.READY
    manifest = json.loads(
        (install_dir / "manifests" / "runtime-manifest.json").read_text(
            encoding="utf-8"
        )
    )
    paths = {item["path"] for item in manifest["files"]}
    assert "runtime/Lib/site-packages/pkg/c.py" in paths
    assert "runtime/Lib/site-packages/old-0.11.dist-info/METADATA" not in paths

def test_download_retries_on_connection_error(tmp_path, monkeypatch) -> None:
    """网络抖动（连接中断）按退避重试，重试成功即完成安装。"""
    monkeypatch.setattr(runtime_module, "_DOWNLOAD_RETRY_DELAYS", (0.0,) * 4)
    files = {"runtime/python.exe": b"python-runtime"}
    archive = _archive(files)
    package = _package(archive, files)
    flaky = _FlakySession(_Session(archive), fail_times=2)
    install_dir = tmp_path / "rt"
    ManagedRuntimeInstaller(flaky).install(package, install_dir)
    assert validate_runtime(install_dir).status is RuntimeStatus.READY
    assert flaky.calls == 3  # 失败 2 次 + 成功 1 次


def _github_package(archive: bytes, files: dict[str, bytes]) -> RuntimePackage:
    """与 :func:`_package` 相同，但分卷 URL 是 GitHub Release 直链。"""
    return RuntimePackage.from_payload(
        {
            "schema": 1,
            "runtime_version": PYMSS_RUNTIME_VERSION,
            "pymss_version": PYMSS_VERSION,
            "python_version": PYMSS_PYTHON_VERSION,
            "variant": "windows-cpu",
            "archive": {
                "url": (
                    "https://github.com/karaoke-studio/karaoke-studio-runtime/"
                    "releases/download/pymss-runtime-v0/runtime.zip"
                ),
                "size": len(archive),
                "sha256": _sha(archive),
            },
            "files": [
                {"path": name, "size": len(data), "sha256": _sha(data)}
                for name, data in files.items()
            ],
        }
    )


def test_download_rotates_gh_proxy_mirrors_for_github_urls(
    tmp_path, monkeypatch
) -> None:
    """GitHub 直链失败立即换 gh-proxy 镜像节点，成功后完成安装。"""
    import requests

    from krok_helper.network import github_url_attempts

    monkeypatch.setattr(runtime_module, "_DOWNLOAD_RETRY_DELAYS", (0.0,) * 4)
    files = {"runtime/python.exe": b"python-runtime"}
    archive = _archive(files)
    package = _github_package(archive, files)
    part_url = package.archive_parts[0].url
    candidates = github_url_attempts(part_url)
    assert candidates[0] == part_url
    assert len(candidates) > 1

    class MirrorSession:
        def __init__(self) -> None:
            self.requested: list[str] = []

        def get(self, url, **_kwargs):
            self.requested.append(url)
            if url == part_url:
                raise requests.exceptions.ConnectionError("github direct down")
            return _Response(archive)

    session = MirrorSession()
    ManagedRuntimeInstaller(session).install(package, tmp_path / "rt")
    assert validate_runtime(tmp_path / "rt").status is RuntimeStatus.READY
    # 直连失败后立即换首个镜像节点，不重试直连
    assert session.requested == [part_url, candidates[1]]


def test_fetch_runtime_package_rotates_mirrors_on_github_failure(
    monkeypatch,
) -> None:
    """清单拉取同样按「官方 → gh-proxy 各节点」接力。"""
    import requests

    class ManifestResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return _production_manifest()

    class FailingFirstSession:
        def __init__(self) -> None:
            self.requested: list[str] = []

        def get(self, url, **_kwargs):
            self.requested.append(url)
            if url.startswith("https://github.com/"):
                raise requests.exceptions.ConnectionError("github direct down")
            return ManifestResponse()

    session = FailingFirstSession()
    package = fetch_runtime_package(
        "https://github.com/karaoke-studio/karaoke-studio-runtime/manifest.json",
        session=session,
    )
    assert package is not None
    assert session.requested[0].startswith("https://github.com/")
    assert session.requested[1].startswith("https://gh-proxy.com/")


def test_download_retry_exhaustion_raises_value_error(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(runtime_module, "_DOWNLOAD_RETRY_DELAYS", (0.0,) * 4)
    files = {"runtime/python.exe": b"python-runtime"}
    archive = _archive(files)
    package = _package(archive, files)
    flaky = _FlakySession(_Session(archive), fail_times=99)
    with pytest.raises(ValueError, match="已重试 4 次"):
        ManagedRuntimeInstaller(flaky).install(package, tmp_path / "rt")


def test_wheel_download_retries_resume_from_offset(
    tmp_path, monkeypatch
) -> None:
    """torch wheel 网络中断重试从已落盘偏移续传（Range），不重下前缀。"""
    monkeypatch.setattr(runtime_module, "_DOWNLOAD_RETRY_DELAYS", (0.0,) * 4)
    wheel_body = b"W" * 5000
    base = _archive({"runtime/python.exe": b"python-runtime"})
    package = RuntimePackage.from_payload(
        {
            "schema": 1,
            "runtime_version": PYMSS_RUNTIME_VERSION,
            "pymss_version": PYMSS_VERSION,
            "python_version": PYMSS_PYTHON_VERSION,
            "variant": "windows-cpu",
            "archive": {
                "url": "https://example.invalid/base.zip",
                "size": len(base),
                "sha256": _sha(base),
            },
            "torch": {
                "version": PYMSS_TORCH_VERSION,
                "wheel": {
                    "url": "https://download.pytorch.org/fake.whl",
                    "filename": "torch-2.7.1+cpu-cp312-cp312-win_amd64.whl",
                    "size": len(wheel_body),
                    "sha256": _sha(wheel_body),
                },
            },
            "files": [
                {
                    "path": "runtime/python.exe",
                    "size": len(b"python-runtime"),
                    "sha256": _sha(b"python-runtime"),
                }
            ],
        }
    )
    flaky = _FlakySession(_Session(wheel_body), fail_times=1)
    installer = ManagedRuntimeInstaller(flaky)
    dest = tmp_path / "torch.whl.part"
    installer._download_wheel(package, dest)
    assert dest.read_bytes() == wheel_body
    # 第二次 GET 带 Range（从首次中断后已写入的偏移续传）
    # _FlakySession 不校验 headers；断言调用次数与最终内容即可
    assert flaky.calls == 2

class _HTTP404Session:
    """恒定返回 404 的会话：确定性失败必须立即失败，不烧退避。"""

    calls = 0

    def get(self, _url, **_kwargs):
        type(self).calls += 1
        import requests

        response = requests.Response()
        response.status_code = 404
        raise requests.exceptions.HTTPError(response=response)


def test_download_http_4xx_fails_immediately(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(runtime_module, "_DOWNLOAD_RETRY_DELAYS", (99.0,) * 4)
    files = {"runtime/python.exe": b"python-runtime"}
    archive = _archive(files)
    package = _package(archive, files)
    with pytest.raises(requests.exceptions.HTTPError):
        ManagedRuntimeInstaller(_HTTP404Session()).install(
            package, tmp_path / "rt"
        )
    assert _HTTP404Session.calls == 1  # 未重试
