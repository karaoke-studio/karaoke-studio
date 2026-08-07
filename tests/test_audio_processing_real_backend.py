from __future__ import annotations

import hashlib
import json
import threading
import time
import zipfile
from pathlib import Path

from PyQt6.QtWidgets import QApplication

from krok_helper.audio_processing.separation.page import AudioSeparationPage
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
            "big_beta5e",
            "inst_v1e",
            "mel_band_roformer_karaoke_becruily",
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


def test_restore_detects_deleted_and_damaged_managed_runtime(tmp_path) -> None:
    missing = RealSeparationBackend({"install_dir": str(tmp_path / "deleted")})
    try:
        assert missing.snapshot().state is ServiceState.INSTALL_MISSING
    finally:
        missing.shutdown()

    damaged_root = tmp_path / "damaged"
    damaged_root.mkdir()
    damaged = RealSeparationBackend({"install_dir": str(damaged_root)})
    try:
        assert damaged.snapshot().state is ServiceState.INSTALL_DAMAGED
    finally:
        damaged.shutdown()


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


def test_first_full_refresh_detects_same_size_runtime_tampering(tmp_path) -> None:
    root = tmp_path / "managed"
    _installed_runtime(root)
    executable = root / "runtime" / "python.exe"
    original = executable.read_bytes()
    executable.write_bytes(b"X" * len(original))
    backend = RealSeparationBackend({"install_dir": str(root)})
    try:
        # Startup remains lightweight (presence + size); the first page visit
        # requests an asynchronous full digest check.
        assert backend.snapshot().state is ServiceState.INSTALLED_STOPPED
        backend.refresh()
        _wait_until(lambda: backend.snapshot().state is ServiceState.INSTALL_DAMAGED)
        assert "损坏" in backend.snapshot().error
    finally:
        backend.shutdown()


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
        assert "big_beta5e" in _FakeServiceFactory.client.loaded
        assert saved
    finally:
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


def test_harmony_pipeline_combines_lead_from_first_step_and_harmony_from_second(
    tmp_path, monkeypatch
) -> None:
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

        assert [item.label for item in results[0].files] == ["主唱", "和声"]
        assert _FakeServiceFactory.client.loaded[start:] == [
            "mel_band_roformer_karaoke_becruily",
            "inst_v1e",
        ]
        assert _FakeServiceFactory.client.separated[separate_start:] == [
            "mel_band_roformer_karaoke_becruily",
            "inst_v1e",
        ]
        assert all(Path(item.path).is_file() for item in results[0].files)

        # The completed first stage is persisted with an input/preset/model
        # fingerprint.  Running the same request again safely skips only that
        # intermediate stage and still performs the final harmony extraction.
        backend.request_task(
            TaskType.HARMONY,
            input_path=str(source),
            output_dir=str(tmp_path / "result"),
            output_format="wav",
        )
        _wait_until(lambda: len(results) == 2)
        assert _FakeServiceFactory.client.loaded[start:] == [
            "mel_band_roformer_karaoke_becruily",
            "inst_v1e",
            "inst_v1e",
        ]
        assert _FakeServiceFactory.client.separated[separate_start:] == [
            "mel_band_roformer_karaoke_becruily",
            "inst_v1e",
            "inst_v1e",
        ]
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
