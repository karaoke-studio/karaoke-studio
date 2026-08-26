from __future__ import annotations

import hashlib
import json
import sys
import threading
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

from PyQt6.QtWidgets import QApplication

from krok_helper.audio_processing.separation.page import AudioSeparationPage
from krok_helper.audio_processing.separation import real_backend as real_backend_module
from krok_helper.audio_processing.separation.backend import FLOW_UPGRADE
from krok_helper.audio_processing.separation.integration import (
    PYMSS_PYTHON_VERSION,
    PYMSS_RUNTIME_VERSION,
    PYMSS_VERSION,
)
from krok_helper.audio_processing.separation.real_backend import RealSeparationBackend
from krok_helper.audio_processing.separation.runtime import (
    RuntimeStatus,
    RuntimeValidation,
    validate_runtime,
)
from krok_helper.audio_processing.separation.states import ServiceState, TaskType
from krok_helper.settings import AppSettings


def _wait_until(predicate, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    app = QApplication.instance()
    while time.monotonic() < deadline:
        if predicate():
            return
        if app is not None:
            app.processEvents()
        time.sleep(0.01)
    assert predicate(), "后台操作未在期限内完成"


def _installed_runtime(root: Path) -> None:
    executable = root / "runtime" / "python.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"runtime-python")
    digest = hashlib.sha256(executable.read_bytes()).hexdigest()
    manifest = {
        "schema": 1,
        "complete": True,
        "runtime_version": PYMSS_RUNTIME_VERSION,
        "pymss_version": PYMSS_VERSION,
        "python_version": PYMSS_PYTHON_VERSION,
        "variant": "windows-cpu",
        "files": [
            {
                "path": "runtime/python.exe",
                "size": executable.stat().st_size,
                "sha256": digest,
            }
        ],
    }
    path = root / "manifests" / "runtime-manifest.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(manifest), encoding="utf-8")


def test_external_environment_probes_hide_the_windows_console(
    tmp_path, monkeypatch
) -> None:
    executable = tmp_path / "python.exe"
    executable.write_bytes(b"fake")
    startupinfo = object()
    calls: list[dict] = []

    def run(_command, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(stdout=f"{PYMSS_VERSION}\n", stderr="", returncode=0)

    class Client:
        @staticmethod
        def capability_checks():
            return [("PyMSS 推理环境", True, "正常")]

    class Service:
        client = Client()

        @staticmethod
        def stop():
            return True

    class Factory:
        @staticmethod
        def start(*_args, **_kwargs):
            return Service()

    monkeypatch.setattr(real_backend_module.subprocess, "run", run)
    monkeypatch.setattr(
        real_backend_module,
        "hidden_subprocess_kwargs",
        lambda: {"creationflags": 0x08000000, "startupinfo": startupinfo},
    )
    backend = RealSeparationBackend({}, service_factory=Factory)
    try:
        assert backend._require_external_version(str(executable)) == PYMSS_VERSION
        checks, version = backend._probe_executable(str(executable))
        assert version == PYMSS_VERSION
        assert all(ok for _name, ok, _detail in checks)
    finally:
        backend.shutdown()

    assert len(calls) == 2
    assert all(call["creationflags"] == 0x08000000 for call in calls)
    assert all(call["startupinfo"] is startupinfo for call in calls)


def test_managed_repair_stops_service_and_smokes_new_runtime(
    tmp_path, monkeypatch
) -> None:
    events: list[str] = []

    class OldService:
        running = True

        def stop(self, timeout_seconds=5.0):
            del timeout_seconds
            events.append("old-stop")
            self.running = False
            return True

    class SmokeClient:
        def capability_checks(self):
            events.append("capabilities")
            return [("健康检查", True, "正常"), ("分离端点", True, "正常")]

    class SmokeService:
        client = SmokeClient()

        def stop(self, timeout_seconds=5.0):
            del timeout_seconds
            events.append("smoke-stop")
            return True

    class ServiceFactory:
        @classmethod
        def start(cls, install_dir, **_kwargs):
            assert Path(install_dir) == tmp_path / "pymss"
            events.append("smoke-start")
            return SmokeService()

    class Package:
        download_size = 123
        archive_parts = ()
        torch_wheel = None

    class Installer:
        def install(self, _package, install_dir, **kwargs):
            assert events == ["old-stop"]
            assert Path(install_dir) == tmp_path / "pymss"
            events.append("install")
            kwargs["post_install_check"](Path(install_dir))
            return RuntimeValidation(RuntimeStatus.READY, "正常")

    monkeypatch.setattr(
        "krok_helper.audio_processing.separation.real_backend.preflight_install_destination",
        lambda path: Path(path),
    )
    monkeypatch.setattr(
        "krok_helper.audio_processing.separation.real_backend.fetch_runtime_package",
        lambda _url: Package(),
    )
    backend = RealSeparationBackend(
        {
            "install_dir": str(tmp_path / "pymss"),
            "runtime_variant": "windows-cpu",
        },
        runtime_installer_factory=Installer,
        service_factory=ServiceFactory,
    )
    backend._service = OldService()

    backend.repair_install()
    _wait_until(lambda: events[-1:] == ["smoke-stop"])

    assert events == ["old-stop", "install", "smoke-start", "capabilities", "smoke-stop"]
    assert backend.snapshot().state is ServiceState.INSTALLED_STOPPED
    assert backend._service is None
    assert backend.shutdown()


def test_cancelled_managed_repair_restores_persisted_runtime_state(
    tmp_path, monkeypatch
) -> None:
    root = tmp_path / "pymss"
    _installed_runtime(root)

    class Package:
        download_size = 123
        archive_parts = ()
        torch_wheel = None

    backend = None

    class CancelledInstaller:
        def install(self, *_args, **_kwargs):
            backend._install_cancel.set()
            raise InterruptedError("模拟取消")

    monkeypatch.setattr(
        "krok_helper.audio_processing.separation.real_backend.preflight_install_destination",
        lambda path: Path(path),
    )
    monkeypatch.setattr(
        "krok_helper.audio_processing.separation.real_backend.fetch_runtime_package",
        lambda _url: Package(),
    )
    backend = RealSeparationBackend(
        {"install_dir": str(root), "runtime_variant": "windows-cpu"},
        runtime_installer_factory=CancelledInstaller,
    )

    backend.repair_install()
    _wait_until(lambda: not backend._futures)

    assert backend.snapshot().state is ServiceState.INSTALLED_STOPPED
    assert "保持不变" in "\n".join(backend.recent_logs)
    assert backend.shutdown()


class _FakeClient:
    def __init__(self) -> None:
        self.downloaded = {
            "inst_v1e",
            "model_mel_band_roformer_karaoke_aufr33_viperx_sdr_10.1956",
        }
        self.loaded: list[str] = []
        self.separated: list[str] = []

    def health(self):
        return {"status": "ok", "device": "CPU（测试）"}

    def catalog_model(self, model: str, **_kwargs):
        return {"pymss": {"local": {"complete": model in self.downloaded}}}

    def download_model(self, model: str, **_kwargs):
        self.downloaded.add(model)
        return {"object": "model.download"}

    def load_model(self, model: str, **_kwargs):
        self.loaded.append(model)
        return {"model_loaded": True}

    def separate_pcm(
        self,
        _pcm_path,
        output_zip,
        *,
        stems,
        output_audio_format,
        **_kwargs,
    ) -> None:
        self.separated.append(str(_kwargs.get("model", "")))
        outputs = []
        with zipfile.ZipFile(output_zip, "w") as bundle:
            for index, stem in enumerate(stems):
                filename = f"{index}_{stem}.{output_audio_format}"
                bundle.writestr(filename, b"separated-audio")
                outputs.append({"stem": stem, "filename": filename})
            bundle.writestr("manifest.json", json.dumps({"outputs": outputs}))


class _FakeService:
    def __init__(self, client: _FakeClient) -> None:
        self.client = client
        self.port = 32123
        self.running = True

    def stop(self, timeout_seconds: float = 5.0) -> bool:
        del timeout_seconds
        self.running = False
        return True


class _FakeServiceFactory:
    client = _FakeClient()

    @classmethod
    def start(cls, *_args, **_kwargs):
        return _FakeService(cls.client)


class _BrokenServiceFactory:
    """功能自检必然失败（桥接起不来）：仲裁应维持 DAMAGED 结论。"""

    @classmethod
    def start(cls, *_args, **_kwargs):
        raise OSError("模拟桥接进程无法启动")


def test_restore_detects_deleted_and_damaged_managed_runtime(tmp_path) -> None:
    missing = RealSeparationBackend({"install_dir": str(tmp_path / "deleted")})
    try:
        assert missing.snapshot().state is ServiceState.INSTALL_MISSING
    finally:
        missing.shutdown()

    damaged_root = tmp_path / "damaged"
    damaged_root.mkdir()
    damaged = RealSeparationBackend(
        {"install_dir": str(damaged_root)}, service_factory=_BrokenServiceFactory
    )
    try:
        # 启动仲裁是异步的：等结论落地再断言，避免撞上 VERIFYING 中间态
        _wait_until(lambda: not damaged._futures)
        assert damaged.snapshot().state is ServiceState.INSTALL_DAMAGED
    finally:
        damaged.shutdown()


def _frozen_at(monkeypatch, exe: Path) -> Path:
    """把进程伪装成从 exe 所在目录运行的打包版（便携基准目录）。"""
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_bytes(b"fake-exe")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr("sys.executable", str(exe))
    return exe.parent


def test_install_dir_helpers_roundtrip(tmp_path, monkeypatch) -> None:
    from krok_helper.audio_processing.separation.runtime import (
        portable_base_dir,
        relativize_install_dir,
        resolve_install_dir,
    )

    base = _frozen_at(monkeypatch, tmp_path / "app" / "KaraokeStudio.exe")
    assert str(portable_base_dir()) == str(base)
    # 基准目录内收为相对，读取时按当前基准展开
    assert relativize_install_dir(str(base / "pymss")) == "pymss"
    assert resolve_install_dir("pymss") == str(base / "pymss")
    # 基准目录外（自定义位置）保持绝对路径原样
    custom = str(tmp_path / "elsewhere" / "pymss")
    assert relativize_install_dir(custom) == custom
    assert resolve_install_dir(custom) == custom
    assert resolve_install_dir("") == ""


def test_restore_heals_to_exec_relative_ai_runtime(tmp_path, monkeypatch) -> None:
    """旧版记录被其它副本写坏（指向已删除目录）时，自动认领当前
    exe 目录旁的 ai_runtime 安装并按相对路径落盘（无感过渡）。"""
    base = _frozen_at(monkeypatch, tmp_path / "app" / "KaraokeStudio.exe")
    _installed_runtime(base / "ai_runtime")
    settings = {"install_dir": str(tmp_path / "downloads-copy" / "pymss")}
    saved: list[dict] = []

    def _save() -> None:
        saved.append(dict(settings))

    backend = RealSeparationBackend(settings, _save)
    try:
        snap = backend.snapshot()
        assert snap.state is ServiceState.INSTALLED_STOPPED
        assert snap.install_dir == str(base / "ai_runtime")
        assert settings["install_dir"] == "ai_runtime"  # 新口径持久化
        assert saved, "自愈后应触发一次设置落盘"
    finally:
        backend.shutdown()


def test_restore_heals_to_legacy_pymss_directory(tmp_path, monkeypatch) -> None:
    """旧版目录名的现存安装同样可被认领（兼容过渡）。"""
    base = _frozen_at(monkeypatch, tmp_path / "app" / "KaraokeStudio.exe")
    _installed_runtime(base / "pymss")
    settings = {"install_dir": str(tmp_path / "downloads-copy" / "pymss")}

    backend = RealSeparationBackend(settings)
    try:
        snap = backend.snapshot()
        assert snap.state is ServiceState.INSTALLED_STOPPED
        assert snap.install_dir == str(base / "pymss")
        assert settings["install_dir"] == "pymss"
    finally:
        backend.shutdown()


def test_restore_heal_prefers_ai_runtime_over_legacy(tmp_path, monkeypatch) -> None:
    base = _frozen_at(monkeypatch, tmp_path / "app" / "KaraokeStudio.exe")
    _installed_runtime(base / "ai_runtime")
    _installed_runtime(base / "pymss")
    settings = {"install_dir": str(tmp_path / "gone")}

    backend = RealSeparationBackend(settings)
    try:
        snap = backend.snapshot()
        assert snap.install_dir == str(base / "ai_runtime")
    finally:
        backend.shutdown()


def test_restore_no_heal_when_candidate_invalid(tmp_path, monkeypatch) -> None:
    """exe 旁没有完整可用的安装时不认领，维持原校验结果。"""
    base = _frozen_at(monkeypatch, tmp_path / "app" / "KaraokeStudio.exe")
    (base / "pymss" / "runtime").mkdir(parents=True)  # 空壳
    recorded = str(tmp_path / "downloads-copy" / "pymss")
    settings = {"install_dir": recorded}

    backend = RealSeparationBackend(settings)
    try:
        snap = backend.snapshot()
        assert snap.state is ServiceState.INSTALL_MISSING
        assert snap.install_dir == recorded
        assert settings["install_dir"] == recorded  # 不改写
    finally:
        backend.shutdown()


def test_restore_migrates_legacy_absolute_to_relative(tmp_path, monkeypatch) -> None:
    """旧版绝对路径仍有效且位于基准目录内：校验通过后顺势改为相对存储，
    下次整目录搬移/换副本即自动跟随。"""
    base = _frozen_at(monkeypatch, tmp_path / "app" / "KaraokeStudio.exe")
    absolute = base / "pymss"
    _installed_runtime(absolute)
    settings = {"install_dir": str(absolute)}

    backend = RealSeparationBackend(settings)
    try:
        snap = backend.snapshot()
        assert snap.state is ServiceState.INSTALLED_STOPPED
        assert snap.install_dir == str(absolute)
        assert settings["install_dir"] == "pymss"
    finally:
        backend.shutdown()


def test_note_runtime_changed_resyncs_manifest(tmp_path) -> None:
    """方案 B 增量安装改动共用包后：宿主通知 → 清单再登记 → 校验恢复，
    且重建后端（等价重启）保持绿色。"""
    root = tmp_path / "managed"
    _installed_runtime(root)
    # 内容变化且长度不同（非全量校验只比 size，等长突变检测不到）
    (root / "runtime" / "python.exe").write_bytes(b"mutated-by-pip-longer")

    settings = {"install_dir": str(root)}
    backend = RealSeparationBackend(settings, service_factory=_BrokenServiceFactory)
    try:
        # 等启动期仲裁结束（自检失败维持损坏），避免与宿主通知的结果竞争
        _wait_until(lambda: not backend._futures)
        assert backend.snapshot().state is ServiceState.INSTALL_DAMAGED
        assert backend.note_runtime_changed() is True
        assert backend.snapshot().state is ServiceState.INSTALLED_STOPPED
    finally:
        backend.shutdown()

    again = RealSeparationBackend({"install_dir": str(root)})
    try:
        assert again.snapshot().state is ServiceState.INSTALLED_STOPPED
    finally:
        again.shutdown()


def test_note_runtime_changed_without_install_returns_false(tmp_path) -> None:
    backend = RealSeparationBackend({})
    try:
        assert backend.note_runtime_changed() is False
    finally:
        backend.shutdown()


def test_startup_damage_arbitration_runs_bridge_and_heals_manifest(tmp_path) -> None:
    """清单口径 DAMAGED（如 AI 打轴增量 pip 改动共用包，且宿主通知
    缺席——pip 中途取消、standalone SUG 直指托管解释器等）时：启动即
    起一次真实桥接进程做功能仲裁，通过则按磁盘重登记清单恢复可用，
    不再单凭清单钉死误报「文件缺失或损坏」。"""
    root = tmp_path / "managed"
    _installed_runtime(root)
    # 内容变化且长度不同（非全量校验比 size 即可发现）
    (root / "runtime" / "python.exe").write_bytes(b"mutated-by-pip-longer")

    events: list[str] = []

    class HealingClient:
        def capability_checks(self):
            events.append("capabilities")
            return [("健康检查", True, "正常")]

    class HealingService:
        client = HealingClient()

        def stop(self, timeout_seconds=5.0):
            del timeout_seconds
            events.append("smoke-stop")
            return True

    class HealingFactory:
        @classmethod
        def start(cls, install_dir, **_kwargs):
            assert Path(install_dir) == root
            events.append("smoke-start")
            return HealingService()

    backend = RealSeparationBackend(
        {"install_dir": str(root)}, service_factory=HealingFactory
    )
    try:
        _wait_until(lambda: backend.snapshot().state is ServiceState.INSTALLED_STOPPED)
        assert events == ["smoke-start", "capabilities", "smoke-stop"]
        assert validate_runtime(root).status is RuntimeStatus.READY
        assert any("功能自检通过" in line for line in backend.recent_logs)
    finally:
        assert backend.shutdown()


def test_damage_arbitration_failure_keeps_damaged_state(tmp_path) -> None:
    """功能自检失败（桥接起不来/能力缺失）才维持「文件缺失或损坏」，
    清单不被合法化。"""
    root = tmp_path / "managed"
    _installed_runtime(root)
    (root / "runtime" / "python.exe").write_bytes(b"mutated-by-pip-longer")

    backend = RealSeparationBackend(
        {"install_dir": str(root)}, service_factory=_BrokenServiceFactory
    )
    try:
        _wait_until(
            lambda: any("功能自检未通过" in line for line in backend.recent_logs)
        )
        snap = backend.snapshot()
        assert snap.state is ServiceState.INSTALL_DAMAGED
        assert "损坏" in snap.error
        assert validate_runtime(root).status is RuntimeStatus.DAMAGED
    finally:
        assert backend.shutdown()


def test_restore_keeps_valid_custom_absolute_location(tmp_path, monkeypatch) -> None:
    """自定义安装位置（基准目录外）不受相对化/自愈影响。"""
    _frozen_at(monkeypatch, tmp_path / "app" / "KaraokeStudio.exe")
    custom = tmp_path / "elsewhere" / "pymss"
    _installed_runtime(custom)
    settings = {"install_dir": str(custom)}

    backend = RealSeparationBackend(settings)
    try:
        snap = backend.snapshot()
        assert snap.state is ServiceState.INSTALLED_STOPPED
        assert snap.install_dir == str(custom)
        assert settings["install_dir"] == str(custom)
    finally:
        backend.shutdown()


def test_managed_old_version_enters_upgrade_state_without_losing_location(tmp_path) -> None:
    root = tmp_path / "managed"
    _installed_runtime(root)
    manifest_path = root / "manifests" / "runtime-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["pymss_version"] = "2.0.17"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    backend = RealSeparationBackend({"install_dir": str(root)})
    try:
        assert backend.snapshot().state is ServiceState.VERSION_INCOMPATIBLE
        assert backend.snapshot().install_dir == str(root)
        backend.start_wizard(FLOW_UPGRADE)
        assert backend.snapshot().state is ServiceState.VERSION_INCOMPATIBLE
        assert backend.snapshot().install_dir == str(root)
    finally:
        backend.shutdown()


def test_saved_external_old_version_requires_user_managed_upgrade(tmp_path) -> None:
    executable = tmp_path / "python.exe"
    executable.write_bytes(b"external-python")
    backend = RealSeparationBackend(
        {
            "external_executable": str(executable),
            "probed_pymss_version": "2.0.17",
            "expected_pymss_version": "2.0.17",
        }
    )
    try:
        assert (
            backend.snapshot().state
            is ServiceState.EXTERNAL_VERSION_INCOMPATIBLE
        )
        assert "2.0.17" in backend.snapshot().error
        assert not backend.snapshot().install_dir
    finally:
        backend.shutdown()


def test_switching_from_managed_to_external_environment_persists_external_mode(
    tmp_path,
) -> None:
    managed = tmp_path / "managed"
    _installed_runtime(managed)
    executable = tmp_path / "external" / "python.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"external-python")
    settings = {"install_dir": str(managed)}
    backend = RealSeparationBackend(settings)
    key = (str(executable), "", "")
    backend._existing_check_cache = (key, None, PYMSS_VERSION)

    backend.connect_existing(executable=str(executable))

    assert "install_dir" not in settings
    assert settings["external_executable"] == str(executable.resolve())
    assert backend.snapshot().install_dir == ""
    assert backend.shutdown()

    restored = RealSeparationBackend(settings)
    assert restored.snapshot().state is ServiceState.INSTALLED_STOPPED
    assert restored.snapshot().install_dir == ""
    assert restored.shutdown()


def test_shutdown_does_not_wait_for_uncancellable_external_request() -> None:
    backend = RealSeparationBackend({})
    started = threading.Event()
    release = threading.Event()

    def external_request() -> None:
        started.set()
        release.wait(5)

    backend._submit(external_request, detached=True)
    assert started.wait(1)
    began = time.monotonic()
    try:
        assert backend.shutdown(timeout_ms=50)
        assert time.monotonic() - began < 0.5
    finally:
        release.set()


def test_normal_refresh_skips_full_runtime_hash(tmp_path, monkeypatch) -> None:
    root = tmp_path / "managed"
    _installed_runtime(root)
    executable = root / "runtime" / "python.exe"
    original = executable.read_bytes()
    executable.write_bytes(b"X" * len(original))
    backend = RealSeparationBackend({"install_dir": str(root)})
    calls: list[bool] = []
    original_validate = validate_runtime

    def recording_validate(path, *, full=False):
        calls.append(full)
        return original_validate(path, full=full)

    monkeypatch.setattr(
        "krok_helper.audio_processing.separation.real_backend.validate_runtime",
        recording_validate,
    )
    try:
        backend.refresh()
        _wait_until(lambda: calls == [False])
        assert backend.snapshot().state is ServiceState.INSTALLED_STOPPED
    finally:
        backend.shutdown()


def test_explicit_full_refresh_detects_same_size_runtime_tampering(tmp_path) -> None:
    root = tmp_path / "managed"
    _installed_runtime(root)
    executable = root / "runtime" / "python.exe"
    original = executable.read_bytes()
    executable.write_bytes(b"X" * len(original))
    backend = RealSeparationBackend(
        {"install_dir": str(root)}, service_factory=_BrokenServiceFactory
    )
    try:
        # Startup remains lightweight (presence + size); an explicit manual
        # check requests the asynchronous full digest verification.
        assert backend.snapshot().state is ServiceState.INSTALLED_STOPPED
        backend.refresh(full=True)
        _wait_until(
            lambda: backend.snapshot().state is ServiceState.INSTALL_DAMAGED
            and not backend._futures
        )
        assert "损坏" in backend.snapshot().error
    finally:
        backend.shutdown()


def test_managed_service_start_uses_lightweight_validation_off_the_gui_thread(
    tmp_path, monkeypatch
) -> None:
    root = tmp_path / "managed"
    _installed_runtime(root)
    backend = RealSeparationBackend(
        {"install_dir": str(root)}, service_factory=_FakeServiceFactory
    )
    validation_started = threading.Event()
    release_validation = threading.Event()
    original_validate = validate_runtime

    validation_modes: list[bool] = []

    def slow_validate(path, *, full=False):
        validation_modes.append(full)
        validation_started.set()
        assert release_validation.wait(3.0)
        return original_validate(path, full=full)

    monkeypatch.setattr(
        "krok_helper.audio_processing.separation.real_backend.validate_runtime",
        slow_validate,
    )
    try:
        began = time.monotonic()
        backend.start_service()
        elapsed = time.monotonic() - began

        assert elapsed < 0.2
        assert validation_started.wait(1.0)
        assert backend.snapshot().state is ServiceState.SERVICE_STARTING

        release_validation.set()
        _wait_until(lambda: backend.snapshot().state is ServiceState.SERVICE_READY)
        assert validation_modes == [False]
    finally:
        release_validation.set()
        assert backend.shutdown()


def test_managed_service_failure_full_hashes_runtime_and_detects_damage(
    tmp_path, monkeypatch
) -> None:
    root = tmp_path / "managed"
    _installed_runtime(root)
    executable = root / "runtime" / "python.exe"
    original = executable.read_bytes()
    executable.write_bytes(b"X" * len(original))

    class FailingServiceFactory:
        @classmethod
        def start(cls, *_args, **_kwargs):
            raise RuntimeError("模拟服务启动失败")

    backend = RealSeparationBackend(
        {"install_dir": str(root)}, service_factory=FailingServiceFactory
    )
    modes: list[bool] = []
    original_validate = validate_runtime

    def recording_validate(path, *, full=False):
        modes.append(full)
        return original_validate(path, full=full)

    monkeypatch.setattr(
        "krok_helper.audio_processing.separation.real_backend.validate_runtime",
        recording_validate,
    )
    try:
        backend.start_service()
        _wait_until(lambda: backend.snapshot().state is ServiceState.INSTALL_DAMAGED)
        assert modes == [False, True]
        assert backend.snapshot().pending_task is None
    finally:
        assert backend.shutdown()


def test_model_load_failure_full_hashes_runtime_and_detects_damage(
    tmp_path, monkeypatch
) -> None:
    root = tmp_path / "managed"
    _installed_runtime(root)

    class LoadFailureClient(_FakeClient):
        def load_model(self, model: str, **_kwargs):
            raise RuntimeError(f"模拟模型加载失败：{model}")

    class LoadFailureFactory:
        client = LoadFailureClient()

        @classmethod
        def start(cls, *_args, **_kwargs):
            return _FakeService(cls.client)

    def fake_prepare(_source, work_dir, **_kwargs):
        path = Path(work_dir) / "input.f32le"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"pcm")
        return path, 1.0

    monkeypatch.setattr(
        "krok_helper.audio_processing.separation.real_backend.prepare_pcm", fake_prepare
    )
    backend = RealSeparationBackend(
        {
            "install_dir": str(root),
            "downloaded_models": [TaskType.INSTRUMENTAL.value],
        },
        service_factory=LoadFailureFactory,
    )
    modes: list[bool] = []
    original_validate = validate_runtime

    def recording_validate(path, *, full=False):
        modes.append(full)
        return original_validate(path, full=full)

    monkeypatch.setattr(
        "krok_helper.audio_processing.separation.real_backend.validate_runtime",
        recording_validate,
    )
    source = tmp_path / "song.wav"
    source.write_bytes(b"input")
    try:
        backend.start_service()
        _wait_until(lambda: backend.snapshot().state is ServiceState.SERVICE_READY)

        executable = root / "runtime" / "python.exe"
        original = executable.read_bytes()
        executable.write_bytes(b"X" * len(original))
        backend.request_task(
            TaskType.INSTRUMENTAL,
            input_path=str(source),
            output_dir=str(tmp_path / "output"),
            output_format="wav",
        )

        _wait_until(lambda: backend.snapshot().state is ServiceState.INSTALL_DAMAGED)
        assert modes == [False, True]
        assert backend.snapshot().pending_task is None
    finally:
        assert backend.shutdown()


def test_production_page_uses_real_backend_by_default() -> None:
    page = AudioSeparationPage(AppSettings(), lambda: None)
    try:
        assert isinstance(page._backend, RealSeparationBackend)
        assert page.current_view() == "welcome"
    finally:
        page.shutdown()
        page.deleteLater()


def test_managed_service_and_real_separation_pipeline(tmp_path, monkeypatch) -> None:
    root = tmp_path / "pymss"
    _installed_runtime(root)
    settings = {
        "install_dir": str(root),
        "runtime_variant": "windows-cpu",
        "downloaded_models": [TaskType.VOCAL.value],
    }
    saved: list[bool] = []
    backend = RealSeparationBackend(
        settings,
        lambda: saved.append(True),
        service_factory=_FakeServiceFactory,
    )

    def fake_prepare(_source, work_dir, **_kwargs):
        path = Path(work_dir) / "input.f32le"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"pcm")
        return path, 1.0

    monkeypatch.setattr(
        "krok_helper.audio_processing.separation.real_backend.prepare_pcm", fake_prepare
    )
    source = tmp_path / "song.wav"
    source.write_bytes(b"input")
    output = tmp_path / "output"
    results = []
    backend.resultReady.connect(results.append)
    try:
        assert backend.snapshot().state is ServiceState.INSTALLED_STOPPED
        backend.start_service()
        _wait_until(lambda: backend.snapshot().state is ServiceState.SERVICE_READY)
        assert backend.snapshot().dependencies[TaskType.VOCAL].ready

        backend.request_task(
            TaskType.VOCAL,
            input_path=str(source),
            output_dir=str(output),
            output_format="flac",
        )
        _wait_until(lambda: bool(results))

        assert backend.snapshot().state is ServiceState.SERVICE_READY
        assert results[0].files[0].label == "人声"
        assert Path(results[0].files[0].path).read_bytes() == b"separated-audio"
        assert "inst_v1e" in _FakeServiceFactory.client.loaded
        assert saved
    finally:
        assert backend.shutdown()


def test_pipeline_uses_canonical_model_id_returned_by_load(tmp_path, monkeypatch) -> None:
    root = tmp_path / "pymss"
    _installed_runtime(root)

    class CanonicalClient(_FakeClient):
        requested_stems: list[tuple[str, ...]] = []

        def load_model(self, model: str, **_kwargs):
            self.loaded.append(model)
            return {
                "model_loaded": True,
                "model": {
                    "id": f"{model}.ckpt",
                    "pymss": {"instruments": ["other", "vocals"]},
                },
            }

        def separate_pcm(self, *args, stems=(), **kwargs):
            self.requested_stems.append(tuple(stems))
            return super().separate_pcm(*args, stems=stems, **kwargs)

    class CanonicalFactory:
        client = CanonicalClient()

        @classmethod
        def start(cls, *_args, **_kwargs):
            return _FakeService(cls.client)

    backend = RealSeparationBackend(
        {"install_dir": str(root)}, service_factory=CanonicalFactory
    )

    def fake_prepare(_source, work_dir, **_kwargs):
        path = Path(work_dir) / "input.f32le"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"pcm")
        return path, 1.0

    monkeypatch.setattr(
        "krok_helper.audio_processing.separation.real_backend.prepare_pcm", fake_prepare
    )
    source = tmp_path / "song.wav"
    source.write_bytes(b"input")
    results = []
    backend.resultReady.connect(results.append)
    try:
        backend.start_service()
        _wait_until(lambda: backend.snapshot().state is ServiceState.SERVICE_READY)
        backend.request_task(
            TaskType.INSTRUMENTAL,
            input_path=str(source),
            output_dir=str(tmp_path / "output"),
            output_format="wav",
        )
        _wait_until(lambda: bool(results))

        assert CanonicalFactory.client.loaded[-1] == "inst_v1e"
        assert CanonicalFactory.client.separated[-1] == "inst_v1e.ckpt"
        assert CanonicalFactory.client.requested_stems[-1] == ("other",)
    finally:
        assert backend.shutdown()


def test_managed_model_download_reports_partial_file_progress(tmp_path) -> None:
    root = tmp_path / "pymss"
    _installed_runtime(root)
    download_started = threading.Event()
    release_download = threading.Event()
    completed: list[TaskType] = []

    class DownloadClient:
        def catalog_model(self, model: str, **_kwargs):
            assert model == "inst_v1e"
            return {
                "pymss": {
                    "files": [
                        {"remote_url": "https://models.example/inst_v1e.ckpt"}
                    ]
                }
            }

        def download_model(self, model: str, **_kwargs):
            assert model == "inst_v1e"
            target = (
                root
                / "models"
                / "vocal"
                / "vocal_instrumental_dual"
                / "inst_v1e.ckpt.part"
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"x" * (2 * 1024**2))
            download_started.set()
            assert release_download.wait(3.0)
            target.replace(target.with_suffix(""))
            return {"object": "model.download"}

    backend = RealSeparationBackend({"install_dir": str(root)})
    backend._client = DownloadClient()
    backend._snap.pending_task = TaskType.INSTRUMENTAL
    backend._start_pipeline = completed.append
    try:
        backend.start_model_download()
        assert download_started.wait(1.0)
        _wait_until(lambda: backend.snapshot().download_done == 2 * 1024**2)

        snapshot = backend.snapshot()
        assert snapshot.state is ServiceState.MODEL_DOWNLOADING
        assert 0 < snapshot.download_done < snapshot.download_total

        release_download.set()
        _wait_until(lambda: completed == [TaskType.INSTRUMENTAL])
        assert backend.snapshot().download_done == backend.snapshot().download_total
    finally:
        release_download.set()
        assert backend.shutdown()


def test_local_separation_progress_file_is_forwarded_to_ui(tmp_path) -> None:
    root = tmp_path / "pymss"
    _installed_runtime(root)
    backend = RealSeparationBackend({"install_dir": str(root)})
    progress_path = root / "logs" / "separation-progress.json"
    progress_path.parent.mkdir(parents=True, exist_ok=True)
    stopped = threading.Event()
    updates = []
    backend.taskProgressChanged.connect(updates.append)
    monitor = threading.Thread(
        target=backend._monitor_separation_progress,
        args=(progress_path, stopped),
        daemon=True,
    )
    try:
        monitor.start()
        progress_path.write_text(
            json.dumps(
                {
                    "done": 42,
                    "total": 120,
                    "message": "Processing audio",
                    "status": "running",
                    "updated_at": time.time(),
                }
            ),
            encoding="utf-8",
        )
        _wait_until(lambda: bool(updates))

        assert updates[-1].show_processing
        assert updates[-1].processing_done == 42
        assert updates[-1].processing_total == 120
    finally:
        stopped.set()
        monitor.join(timeout=1.0)
        assert backend.shutdown()


def test_async_msst_scan_can_bind_without_touching_source(tmp_path) -> None:
    root = tmp_path / "managed"
    _installed_runtime(root)
    msst = tmp_path / "MSST-WebUI"
    config = msst / "configs" / "model.yaml"
    config.parent.mkdir(parents=True)
    config.write_text("training:\n  instruments: [vocals, instrumental]\n", encoding="utf-8")
    weight = msst / "pretrain" / "vocal_models" / "reuse.ckpt"
    weight.parent.mkdir(parents=True)
    weight.write_bytes(b"reuse-model")
    model_map = msst / "data" / "msst_model_map.json"
    model_map.parent.mkdir(parents=True)
    model_map.write_text(
        json.dumps(
            {
                "vocal_models": [
                    {
                        "name": "reuse.ckpt",
                        "config_path": "configs/model.yaml",
                        "model_type": "mel_band_roformer",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    before = {path: path.read_bytes() for path in msst.rglob("*") if path.is_file()}
    backend = RealSeparationBackend({"install_dir": str(root)})
    scans = []
    backend.msstScanFinished.connect(scans.append)
    try:
        backend.start_msst_scan(str(msst))
        _wait_until(lambda: bool(scans))
        candidate = next(
            item for item in scans[0] if item.task is TaskType.VOCAL and item.bindable
        )
        backend.bind_external_model(TaskType.VOCAL, candidate.candidate_id)

        registry = root / "manifests" / "external-models.json"
        assert registry.is_file()
        assert backend.snapshot().dependencies[TaskType.VOCAL].is_external
        assert {path: path.read_bytes() for path in msst.rglob("*") if path.is_file()} == before
    finally:
        backend.shutdown()


def test_msst_scan_cancellation_stops_hash_work_without_emitting_failure(
    tmp_path, monkeypatch
) -> None:
    started = threading.Event()

    def slow_scan(_root, *, cancelled):
        started.set()
        while not cancelled.wait(0.01):
            pass
        raise InterruptedError("MSST 模型扫描已取消。")

    monkeypatch.setattr(
        "krok_helper.audio_processing.separation.real_backend.scan_msst_models",
        slow_scan,
    )
    backend = RealSeparationBackend({})
    finished = []
    failed = []
    backend.msstScanFinished.connect(finished.append)
    backend.msstScanFailed.connect(failed.append)
    try:
        backend.start_msst_scan(str(tmp_path))
        assert started.wait(1.0)
        backend.cancel_msst_scan()
        _wait_until(lambda: not backend._futures)
        assert not finished
        assert not failed
    finally:
        backend.shutdown()


def test_harmony_pipeline_runs_a_single_karaoke_pass(tmp_path, monkeypatch) -> None:
    """和声伴奏改为单阶段：karaoke 模型直接处理原曲，取 other 残余轨。"""
    root = tmp_path / "pymss"
    _installed_runtime(root)
    backend = RealSeparationBackend(
        {"install_dir": str(root), "downloaded_models": [TaskType.HARMONY.value]},
        service_factory=_FakeServiceFactory,
    )

    def fake_prepare(_source, work_dir, **_kwargs):
        path = Path(work_dir) / "input.f32le"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"pcm")
        return path, 1.0

    monkeypatch.setattr(
        "krok_helper.audio_processing.separation.real_backend.prepare_pcm", fake_prepare
    )
    source = tmp_path / "duet.wav"
    source.write_bytes(b"input")
    results = []
    backend.resultReady.connect(results.append)
    try:
        backend.start_service()
        _wait_until(lambda: backend.snapshot().state is ServiceState.SERVICE_READY)
        start = len(_FakeServiceFactory.client.loaded)
        separate_start = len(_FakeServiceFactory.client.separated)
        backend.request_task(
            TaskType.HARMONY,
            input_path=str(source),
            output_dir=str(tmp_path / "result"),
            output_format="wav",
        )
        _wait_until(lambda: bool(results))

        karaoke = "model_mel_band_roformer_karaoke_aufr33_viperx_sdr_10.1956"
        assert [item.label for item in results[0].files] == ["和声伴奏"]
        assert _FakeServiceFactory.client.loaded[start:] == [karaoke]
        assert _FakeServiceFactory.client.separated[separate_start:] == [karaoke]
        assert all(Path(item.path).is_file() for item in results[0].files)
    finally:
        backend.shutdown()


def test_saved_external_service_is_monitored_for_recovery_and_disconnect(
    monkeypatch,
) -> None:
    class HealthClient:
        online = True

        def __init__(self, base_url, **_kwargs) -> None:
            self.base_url = base_url

        def health(self):
            if not self.online:
                raise ConnectionError("offline")
            return {"status": "ok", "device": "远程 GPU"}

    monkeypatch.setattr(
        "krok_helper.audio_processing.separation.real_backend.PyMSSClient", HealthClient
    )
    backend = RealSeparationBackend({"external_server_url": "http://127.0.0.1:8765"})
    try:
        assert backend.snapshot().state is ServiceState.EXTERNAL_OFFLINE
        backend._schedule_health_check()
        _wait_until(lambda: backend.snapshot().state is ServiceState.SERVICE_READY)
        assert backend.snapshot().device == "远程 GPU"

        HealthClient.online = False
        backend._schedule_health_check()
        _wait_until(lambda: backend.snapshot().state is ServiceState.EXTERNAL_OFFLINE)
    finally:
        HealthClient.online = True
        backend.shutdown()


def test_existing_service_probe_is_reused_and_api_key_stays_in_memory(
    monkeypatch,
) -> None:
    class CheckedClient:
        instances = []
        capability_calls = 0

        def __init__(self, base_url, *, api_key="", **_kwargs) -> None:
            self.base_url = base_url.rstrip("/")
            self.api_key = api_key
            self.__class__.instances.append(self)

        def capability_checks(self):
            self.__class__.capability_calls += 1
            return [("服务能力", True, "协议兼容")]

        def health(self):
            return {"status": "ok", "device": "远程 GPU"}

    monkeypatch.setattr(
        "krok_helper.audio_processing.separation.real_backend.PyMSSClient", CheckedClient
    )
    settings = {}
    backend = RealSeparationBackend(settings)
    checks = []
    backend.existingCheckFinished.connect(checks.append)
    try:
        backend.start_existing_check(
            server_url="http://127.0.0.1:9876", api_key="session-secret"
        )
        _wait_until(lambda: bool(checks))
        backend.connect_existing(
            server_url="http://127.0.0.1:9876", api_key="session-secret"
        )

        assert CheckedClient.capability_calls == 1
        assert backend.snapshot().state is ServiceState.SERVICE_READY
        assert backend._client is CheckedClient.instances[0]
        assert backend._client.api_key == "session-secret"
        assert settings["external_server_url"] == "http://127.0.0.1:9876"
        assert "api_key" not in json.dumps(settings)

        backend.stop_service()
        backend._schedule_health_check()
        _wait_until(lambda: backend.snapshot().state is ServiceState.SERVICE_READY)
        assert CheckedClient.instances[-1].api_key == "session-secret"
    finally:
        backend.shutdown()


def test_external_msst_model_is_verified_on_first_real_load(
    tmp_path, monkeypatch
) -> None:
    managed = tmp_path / "managed"
    _installed_runtime(managed)
    msst = tmp_path / "MSST-WebUI"
    config = msst / "configs" / "model.yaml"
    config.parent.mkdir(parents=True)
    config.write_text("training:\n  instruments: [vocals, instrumental]\n", encoding="utf-8")
    weight = msst / "pretrain" / "vocal_models" / "reuse.ckpt"
    weight.parent.mkdir(parents=True)
    weight.write_bytes(b"reuse-model")
    model_map = msst / "data" / "msst_model_map.json"
    model_map.parent.mkdir(parents=True)
    model_map.write_text(
        json.dumps(
            {
                "vocal_models": [
                    {
                        "name": "reuse.ckpt",
                        "config_path": "configs/model.yaml",
                        "model_type": "mel_band_roformer",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    backend = RealSeparationBackend(
        {"install_dir": str(managed)}, service_factory=_FakeServiceFactory
    )
    scans = []
    results = []
    backend.msstScanFinished.connect(scans.append)
    backend.resultReady.connect(results.append)

    def fake_prepare(_source, work_dir, **_kwargs):
        path = Path(work_dir) / "input.f32le"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"pcm")
        return path, 1.0

    monkeypatch.setattr(
        "krok_helper.audio_processing.separation.real_backend.prepare_pcm", fake_prepare
    )
    source = tmp_path / "song.wav"
    source.write_bytes(b"input")
    try:
        backend.start_msst_scan(str(msst))
        _wait_until(lambda: bool(scans))
        candidate = next(
            item for item in scans[0] if item.task is TaskType.VOCAL and item.bindable
        )
        backend.bind_external_model(TaskType.VOCAL, candidate.candidate_id)
        assert backend._registry().validate()[TaskType.VOCAL] == "pending"

        backend.start_service()
        _wait_until(lambda: backend.snapshot().state is ServiceState.SERVICE_READY)
        assert backend.snapshot().dependencies[TaskType.VOCAL].ready
        assert "待首次加载" in backend.snapshot().dependencies[TaskType.VOCAL].badge
        backend.request_task(
            TaskType.VOCAL,
            input_path=str(source),
            output_dir=str(tmp_path / "output"),
            output_format="wav",
        )
        _wait_until(lambda: bool(results))

        assert backend._registry().validate()[TaskType.VOCAL] == "ready"
        assert backend.snapshot().state is ServiceState.EXTERNAL_MODEL_READY
    finally:
        backend.shutdown()


def test_external_msst_model_load_failure_is_persisted_as_unsupported(
    tmp_path, monkeypatch
) -> None:
    managed = tmp_path / "managed"
    _installed_runtime(managed)
    msst = tmp_path / "MSST-WebUI"
    config = msst / "configs" / "model.yaml"
    config.parent.mkdir(parents=True)
    config.write_text("training:\n  instruments: [vocals]\n", encoding="utf-8")
    weight = msst / "pretrain" / "vocal_models" / "bad.ckpt"
    weight.parent.mkdir(parents=True)
    weight.write_bytes(b"bad-model")
    model_map = msst / "data" / "msst_model_map.json"
    model_map.parent.mkdir(parents=True)
    model_map.write_text(
        json.dumps(
            {
                "vocal_models": [
                    {
                        "name": "bad.ckpt",
                        "config_path": "configs/model.yaml",
                        "model_type": "mel_band_roformer",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    class RejectingClient(_FakeClient):
        def load_model(self, model: str, **_kwargs):
            if model.startswith("krok_msst_"):
                raise RuntimeError("配置张量尺寸不兼容")
            return super().load_model(model, **_kwargs)

    class RejectingFactory:
        client = RejectingClient()

        @classmethod
        def start(cls, *_args, **_kwargs):
            return _FakeService(cls.client)

    def fake_prepare(_source, work_dir, **_kwargs):
        path = Path(work_dir) / "input.f32le"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"pcm")
        return path, 1.0

    monkeypatch.setattr(
        "krok_helper.audio_processing.separation.real_backend.prepare_pcm", fake_prepare
    )
    backend = RealSeparationBackend(
        {"install_dir": str(managed)}, service_factory=RejectingFactory
    )
    scans = []
    backend.msstScanFinished.connect(scans.append)
    source = tmp_path / "song.wav"
    source.write_bytes(b"input")
    try:
        backend.start_msst_scan(str(msst))
        _wait_until(lambda: bool(scans))
        candidate = next(item for item in scans[0] if item.bindable)
        backend.bind_external_model(TaskType.VOCAL, candidate.candidate_id)
        backend.start_service()
        _wait_until(lambda: backend.snapshot().state is ServiceState.SERVICE_READY)
        backend.request_task(
            TaskType.VOCAL,
            input_path=str(source),
            output_dir=str(tmp_path / "output"),
            output_format="wav",
        )
        _wait_until(
            lambda: backend.snapshot().state is ServiceState.EXTERNAL_MODEL_UNSUPPORTED
        )

        assert backend._registry().validate()[TaskType.VOCAL] == "unsupported"
        dependency = backend.snapshot().dependencies[TaskType.VOCAL]
        assert not dependency.ready
        assert "配置张量尺寸不兼容" in dependency.reason
    finally:
        backend.shutdown()


def test_external_binding_uses_the_models_declared_stem(tmp_path) -> None:
    """回归：外部模型的 stem 曾写死为 vocals/instrumental，被服务拒绝。

    实机报错：Invalid stem 'instrumental'. Valid stems: ['other', 'vocals']
    """
    import json as _json

    root = tmp_path / "pymss"
    _installed_runtime(root)
    registry_path = root / "manifests" / "external-models.json"
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(
        _json.dumps(
            {
                "version": 1,
                "models": [
                    {
                        "name": "krok_local_instrumental_x",
                        "model_type": "mel_band_roformer",
                        "model_path": str(tmp_path / "m.ckpt"),
                        "config_path": str(tmp_path / "m.yaml"),
                        "target_stem": "other/vocals",
                        "krok": {"task": "instrumental", "source": "local"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    backend = RealSeparationBackend(
        {
            "install_dir": str(root),
            "external_bindings": {"instrumental": "krok_local_instrumental_x"},
        },
        service_factory=_FakeServiceFactory,
    )
    try:
        steps = backend._steps_for_task(TaskType.INSTRUMENTAL)
        assert steps[0].stems == ("other",), "必须用模型声明的 other，而不是写死的 instrumental"
        assert steps[0].output_labels == ("伴奏",)
    finally:
        backend.shutdown()


def test_unresolvable_external_stem_explains_instead_of_guessing(tmp_path) -> None:
    import json as _json

    root = tmp_path / "pymss"
    _installed_runtime(root)
    registry_path = root / "manifests" / "external-models.json"
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(
        _json.dumps(
            {
                "version": 1,
                "models": [
                    {
                        "name": "krok_local_weird",
                        "model_type": "mel_band_roformer",
                        "target_stem": "a/b/c",
                        "krok": {"task": "instrumental", "source": "local"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    backend = RealSeparationBackend(
        {
            "install_dir": str(root),
            "external_bindings": {"instrumental": "krok_local_weird"},
        },
        service_factory=_FakeServiceFactory,
    )
    try:
        # 不抛异常：依赖计算与后端构造都会走到这里，抛出会让整页报错。
        assert backend._steps_for_task(TaskType.INSTRUMENTAL) == ()
        message = backend._external_stem_problem(TaskType.INSTRUMENTAL)
        assert "无法确定" in message
        assert "a、b、c" in message, "应把该模型真实声明的轨列给用户"

        # 任务卡上直接说明原因，而不是让用户提交后被服务拒绝。
        dep = backend.snapshot().dependencies[TaskType.INSTRUMENTAL]
        assert not dep.ready
        assert "无法确定" in dep.reason
    finally:
        backend.shutdown()


def test_healthy_service_clears_a_stuck_error_state(tmp_path) -> None:
    """回归：任务失败后界面永久停在「出现错误」，点重试也没有反应。"""
    root = tmp_path / "pymss"
    _installed_runtime(root)
    backend = RealSeparationBackend(
        {"install_dir": str(root), "downloaded_models": [TaskType.VOCAL.value]},
        service_factory=_FakeServiceFactory,
    )
    try:
        backend.start_service()
        _wait_until(lambda: backend.snapshot().state is ServiceState.SERVICE_READY)

        backend._set_state(ServiceState.ERROR, error="模拟任务失败")
        assert backend.snapshot().state is ServiceState.ERROR

        backend.refresh()  # 界面上的「重试」
        _wait_until(lambda: backend.snapshot().state is not ServiceState.ERROR)
        assert backend.snapshot().state is ServiceState.SERVICE_READY
        assert not backend.snapshot().error
    finally:
        backend.shutdown()


def test_local_import_reloads_the_service_registry(tmp_path, monkeypatch) -> None:
    """回归：绑定后不重载清单，PyMSS 会报 Unknown pymss model: krok_local_...

    用户模型清单是 PyMSS 进程内缓存的，只有 finish_external_mapping 会让它重新加载。
    此前只有 MSST 向导调用它，设置里的导入路径没有，导致导入完立刻用就失败。
    """
    root = tmp_path / "pymss"
    _installed_runtime(root)
    weight = tmp_path / "third_party.ckpt"
    weight.write_bytes(b"w" * 128)
    (tmp_path / "third_party.yaml").write_text(
        "training:\n  instruments:\n  - other\n  - vocals\n", encoding="utf-8"
    )

    backend = RealSeparationBackend(
        {"install_dir": str(root)}, service_factory=_FakeServiceFactory
    )
    reloaded: list[bool] = []
    monkeypatch.setattr(
        type(backend),
        "finish_external_mapping",
        lambda self: reloaded.append(True),
    )
    done: list = []
    backend.localImportFinished.connect(done.append)
    try:
        backend.import_local_model(
            TaskType.INSTRUMENTAL,
            weight_path=str(weight),
            config_path=str(tmp_path / "third_party.yaml"),
            model_type="mel_band_roformer",
        )
        _wait_until(lambda: bool(done))
        assert reloaded, "导入绑定后必须让服务重新加载用户模型清单"
    finally:
        backend.shutdown()


def test_queue_continues_after_a_failed_task(tmp_path, monkeypatch) -> None:
    """队列中一个任务失败只记这一项，剩余任务继续跑（用户选定的行为）。"""
    root = tmp_path / "pymss"
    _installed_runtime(root)
    backend = RealSeparationBackend(
        {
            "install_dir": str(root),
            "downloaded_models": [t.value for t in TaskType],
        },
        service_factory=_FakeServiceFactory,
    )

    def fake_prepare(_source, work_dir, **_kwargs):
        path = Path(work_dir) / "input.f32le"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"pcm")
        return path, 1.0

    monkeypatch.setattr(
        "krok_helper.audio_processing.separation.real_backend.prepare_pcm", fake_prepare
    )
    source = tmp_path / "song.wav"
    source.write_bytes(b"input")

    results: list = []
    backend.resultReady.connect(results.append)
    try:
        backend.start_service()
        _wait_until(lambda: backend.snapshot().state is ServiceState.SERVICE_READY)
        backend.request_tasks(
            [TaskType.VOCAL, TaskType.INSTRUMENTAL, TaskType.HARMONY],
            input_path=str(source),
            output_dir=str(tmp_path / "out"),
            output_format="wav",
        )
        _wait_until(lambda: len(results) == 3, timeout=15.0)

        assert [item.task for item in results] == [
            TaskType.VOCAL,
            TaskType.INSTRUMENTAL,
            TaskType.HARMONY,
        ], "必须按固定顺序跑完全部选中任务"
        assert backend.snapshot().state is ServiceState.SERVICE_READY
        assert backend.snapshot().queued_tasks == ()
    finally:
        backend.shutdown()


def test_single_request_still_counts_as_a_one_item_batch(tmp_path, monkeypatch) -> None:
    root = tmp_path / "pymss"
    _installed_runtime(root)
    backend = RealSeparationBackend(
        {"install_dir": str(root), "downloaded_models": [TaskType.VOCAL.value]},
        service_factory=_FakeServiceFactory,
    )

    def fake_prepare(_source, work_dir, **_kwargs):
        path = Path(work_dir) / "input.f32le"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"pcm")
        return path, 1.0

    monkeypatch.setattr(
        "krok_helper.audio_processing.separation.real_backend.prepare_pcm", fake_prepare
    )
    source = tmp_path / "song.wav"
    source.write_bytes(b"input")
    results: list = []
    backend.resultReady.connect(results.append)
    try:
        backend.start_service()
        _wait_until(lambda: backend.snapshot().state is ServiceState.SERVICE_READY)
        backend.request_task(
            TaskType.VOCAL,
            input_path=str(source),
            output_dir=str(tmp_path / "out"),
            output_format="wav",
        )
        _wait_until(lambda: bool(results))
        assert backend.snapshot().queue_total == 1
        assert backend.snapshot().state is ServiceState.SERVICE_READY
    finally:
        backend.shutdown()


def test_external_executable_can_register_models(tmp_path) -> None:
    """回归：选「使用已有 PyMSS」后一键导入报「请先安装 PyMSS 底座」。

    外部可执行环境的服务同样由工作台启动，用的是 service.py 传给它的
    PYMSS_USER_MODELS，因此工作台完全可以往同一份清单里注册模型。
    """
    executable = tmp_path / "python.exe"
    executable.write_bytes(b"external-python")
    user_models = tmp_path / "user_models.json"
    backend = RealSeparationBackend(
        {
            "external_executable": str(executable),
            "external_user_models": str(user_models),
        }
    )
    try:
        assert backend._registry_path() == user_models
        assert backend._registry() is not None, "外部环境必须支持注册模型"
    finally:
        backend.shutdown()


def test_registry_path_matches_what_the_service_is_told_to_read(tmp_path) -> None:
    """注册文件必须与传给服务的 PYMSS_USER_MODELS 是同一个，否则服务读不到。"""
    executable = tmp_path / "python.exe"
    executable.write_bytes(b"external-python")
    user_models = tmp_path / "user_models.json"
    backend = RealSeparationBackend(
        {
            "external_executable": str(executable),
            "external_user_models": str(user_models),
        }
    )
    try:
        assert backend._registry_path() == backend._existing_user_models_path()
    finally:
        backend.shutdown()


def test_remote_only_service_explains_why_it_cannot_register(tmp_path) -> None:
    """直接连一个已在运行的服务：进程不归工作台管，说明原因而不是让用户去装底座。"""
    backend = RealSeparationBackend({"external_server_url": "http://127.0.0.1:9999"})
    try:
        assert backend._registry_path() is None
        reason = backend._registry_unavailable_reason()
        assert "无法为它注册模型" in reason
        assert "请先安装 PyMSS 底座" not in reason
    finally:
        backend.shutdown()


def test_video_input_is_demuxed_once_per_batch_and_cleaned_up(tmp_path, monkeypatch):
    """视频只抽一次音轨，整批复用，批次结束后临时文件不留。"""

    backend = RealSeparationBackend({})
    calls: list[Path] = []

    def fake_extract(source, work_dir, **_kwargs):
        calls.append(Path(source))
        work = Path(work_dir)
        work.mkdir(parents=True, exist_ok=True)
        produced = work / f"{Path(source).stem}.wav"
        produced.write_bytes(b"RIFF")
        return produced

    monkeypatch.setattr(
        "krok_helper.audio_processing.separation.real_backend.extract_audio_track",
        fake_extract,
    )
    video = tmp_path / "MV.mp4"
    video.write_bytes(b"0")

    first = backend._resolve_media_input(video, lambda *_args, **_kwargs: None)
    second = backend._resolve_media_input(video, lambda *_args, **_kwargs: None)

    assert first == second
    assert first.is_file()
    # 一批里的第二、第三个任务必须复用同一份音轨，而不是各抽一遍。
    assert calls == [video]

    work_dir = first.parent
    backend._release_demux()
    assert not work_dir.exists()

    # 释放之后再要，就该重新抽一份。
    third = backend._resolve_media_input(video, lambda *_args, **_kwargs: None)
    assert calls == [video, video]
    assert third.is_file()
    backend._release_demux()


def test_audio_input_never_reaches_the_demuxer(tmp_path, monkeypatch):
    """音频素材必须原样透传，否则没配 ffmpeg 的用户会被这条路拖下水。"""

    backend = RealSeparationBackend({})

    def explode(*_args, **_kwargs):
        raise AssertionError("音频输入不应触发解复用")

    monkeypatch.setattr(
        "krok_helper.audio_processing.separation.real_backend.extract_audio_track",
        explode,
    )
    audio = tmp_path / "歌.wav"
    audio.write_bytes(b"RIFF")

    assert backend._resolve_media_input(
        audio, lambda *_args, **_kwargs: None
    ) == audio
    backend._release_demux()
