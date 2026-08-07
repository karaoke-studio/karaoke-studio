"""Production PyMSS backend used by the audio-separation page."""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor, wait
from pathlib import Path
from urllib.parse import unquote, urlsplit

from PyQt6.QtCore import QObject, QTimer

from .audio_io import extract_result_stems, prepare_pcm
from .backend import (
    FLOW_EXISTING,
    FLOW_FULL,
    FLOW_REMAP_MSST,
    FLOW_REUSE_MSST,
    FLOW_UPGRADE,
    CatalogModel,
    ExternalModelCandidate,
    ResultFile,
    SeparationBackend,
    SeparationSnapshot,
    TaskProgress,
    TaskResult,
)
from .client import PyMSSClient
from .cache import IntermediateResultCache, cache_key, input_fingerprint
from .integration import (
    PYMSS_CPU_VARIANT,
    PYMSS_VERSION,
    TORCH_WHEELS,
    managed_runtime_variant,
    runtime_manifest_url,
)
from .msst import ExternalModelRegistry, scan_msst_models
from .presets import (
    SeparationStep,
    TASK_MODEL_OVERRIDES_KEY,
    TASK_PRESETS,
    effective_steps,
    task_override,
)
from .local_import import build_local_candidate
from .local_models import scan_local_models
from .stems import parse_model_stems
from .runtime import (
    ManagedRuntimeInstaller,
    RuntimeStatus,
    fetch_runtime_package,
    preflight_install_destination,
    validate_runtime,
)
from .service import ManagedServiceProcess, build_server_command
from .states import (
    STAGE_DOWNLOAD,
    STAGE_ENCODE,
    STAGE_LOAD,
    STAGE_PREPARE,
    STAGE_SAVE,
    STAGE_SEPARATE,
    TASK_SPECS,
    ServiceState,
    TaskDependency,
    TaskType,
    format_size,
)


class _ExternalModelLoadFailure(RuntimeError):
    def __init__(self, task: TaskType, error: Exception) -> None:
        super().__init__(str(error))
        self.task = task


class _ExternalVersionMismatch(RuntimeError):
    pass


class RealSeparationBackend(SeparationBackend):
    """Threaded adapter around the pinned PyMSS v2 HTTP server.

    The backend owns only services it starts. A URL supplied by the user is
    probed and used as-is, and is never stopped by Karaoke Studio.
    """

    def __init__(
        self,
        settings_ns: dict | None = None,
        save_settings=None,
        *,
        parent: QObject | None = None,
        executor: ThreadPoolExecutor | None = None,
        runtime_installer_factory=ManagedRuntimeInstaller,
        service_factory=ManagedServiceProcess,
        ffmpeg_dir: str = "",
    ) -> None:
        super().__init__(parent)
        self._settings = settings_ns if settings_ns is not None else {}
        self._save_settings = save_settings or (lambda: None)
        self._executor = executor or ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="pymss-backend"
        )
        self._owns_executor = executor is None
        self._runtime_installer_factory = runtime_installer_factory
        self._service_factory = service_factory
        self._ffmpeg_dir = str(ffmpeg_dir or "")
        self._lock = threading.RLock()
        self._futures: set[Future] = set()
        self._detached_futures: set[Future] = set()
        self._shutdown_requested = False
        self._install_cancel = threading.Event()
        self._task_cancel = threading.Event()
        self._service_cancel = threading.Event()
        self._scan_cancel = threading.Event()
        self._service: ManagedServiceProcess | None = None
        self._client: PyMSSClient | None = None
        self._external_url = False
        self._external_api_key = ""
        self._existing_check_cache: tuple[tuple[str, str, str], PyMSSClient | None, str] | None = None
        self._scan_candidates: dict[str, ExternalModelCandidate] = {}
        self._task_context: dict[str, str] = {}
        self._progress = TaskProgress()
        self._health_future: Future | None = None
        self._runtime_check_future: Future | None = None
        self.recent_logs: deque[str] = deque(maxlen=200)
        self._snap = SeparationSnapshot()
        self._restore_configuration()
        self._health_timer = QTimer(self)
        self._health_timer.setInterval(3000)
        self._health_timer.timeout.connect(self._schedule_health_check)
        self._health_timer.start()

    # ---- common state -------------------------------------------------
    def snapshot(self) -> SeparationSnapshot:
        with self._lock:
            return copy.deepcopy(self._snap)

    def log_directory(self) -> str:
        if self._snap.install_dir:
            return str(Path(self._snap.install_dir) / "logs")
        if str(self._settings.get("external_executable", "")).strip():
            return str(self._external_work_root() / "logs")
        return ""

    def _emit(self) -> None:
        if not self._shutdown_requested:
            self.snapshotChanged.emit(self.snapshot())

    def _set_state(self, state: ServiceState, *, error: str = "") -> None:
        with self._lock:
            self._snap.state = state
            self._snap.error = error
        self._emit()

    def _log(self, text: str) -> None:
        line = time.strftime("%H:%M:%S ") + text
        self.recent_logs.append(line)
        if not self._shutdown_requested:
            self.logAppended.emit(text)

    def _persist(self) -> None:
        try:
            self._save_settings()
        except Exception as exc:
            self._log(f"保存 PyMSS 设置失败：{exc}")

    def _submit(
        self,
        operation,
        on_success=None,
        on_error=None,
        *,
        detached: bool = False,
    ) -> Future | None:
        if self._shutdown_requested:
            return None
        if detached:
            future = Future()

            def run_detached() -> None:
                if not future.set_running_or_notify_cancel():
                    return
                try:
                    future.set_result(operation())
                except BaseException as exc:
                    future.set_exception(exc)

            threading.Thread(
                target=run_detached,
                name="pymss-external-request",
                daemon=True,
            ).start()
        else:
            future = self._executor.submit(operation)
        with self._lock:
            self._futures.add(future)
            if detached:
                self._detached_futures.add(future)

        def finished(done: Future) -> None:
            with self._lock:
                self._futures.discard(done)
                self._detached_futures.discard(done)
            if self._shutdown_requested:
                return
            try:
                result = done.result()
            except InterruptedError as exc:
                if on_error is not None:
                    on_error(exc)
                else:
                    self._log(str(exc))
            except Exception as exc:  # surfaced to the UI state/signal
                if on_error is not None:
                    on_error(exc)
                else:
                    self._fail(exc)
            else:
                if on_success is not None:
                    try:
                        on_success(result)
                    except Exception as exc:
                        if on_error is not None:
                            on_error(exc)
                        else:
                            self._fail(exc)

        future.add_done_callback(finished)
        return future

    def _fail(self, error: Exception | str) -> None:
        message = str(error).strip() or type(error).__name__
        self._log(f"操作失败：{message}")
        self._set_state(ServiceState.ERROR, error=message)

    def _restore_configuration(self) -> None:
        install_dir = str(self._settings.get("install_dir", "")).strip()
        server_url = str(self._settings.get("external_server_url", "")).strip()
        executable = str(self._settings.get("external_executable", "")).strip()
        if install_dir:
            self._snap.install_dir = install_dir
            self._apply_runtime_validation(validate_runtime(install_dir))
        elif server_url:
            self._snap.state = ServiceState.EXTERNAL_OFFLINE
            self._snap.pymss_version = str(self._settings.get("expected_pymss_version", ""))
        elif executable:
            recorded = str(
                self._settings.get("probed_pymss_version")
                or self._settings.get("expected_pymss_version")
                or ""
            )
            self._snap.pymss_version = recorded
            if recorded and recorded != PYMSS_VERSION:
                self._snap.state = ServiceState.EXTERNAL_VERSION_INCOMPATIBLE
                self._snap.error = (
                    f"外部环境为 PyMSS {recorded}，当前工作台需要 {PYMSS_VERSION}。"
                )
            else:
                self._snap.state = ServiceState.INSTALLED_STOPPED
        self._rebuild_dependencies()

    def _apply_runtime_validation(self, result) -> None:
        mapping = {
            RuntimeStatus.READY: ServiceState.INSTALLED_STOPPED,
            RuntimeStatus.MISSING: ServiceState.INSTALL_MISSING,
            RuntimeStatus.DAMAGED: ServiceState.INSTALL_DAMAGED,
            RuntimeStatus.INCOMPATIBLE: ServiceState.VERSION_INCOMPATIBLE,
        }
        self._snap.state = mapping[result.status]
        self._snap.error = "" if result.status is RuntimeStatus.READY else result.message
        self._snap.pymss_version = result.package.pymss_version if result.package else ""

    def _registry(self) -> ExternalModelRegistry | None:
        install_dir = str(self._snap.install_dir or "").strip()
        if not install_dir:
            return None
        return ExternalModelRegistry(Path(install_dir) / "manifests" / "external-models.json")

    def _external_bindings(self) -> dict[TaskType, str]:
        if not str(self._snap.install_dir or "").strip():
            return {}
        raw = self._settings.get("external_bindings")
        if not isinstance(raw, dict):
            return {}
        bindings: dict[TaskType, str] = {}
        for key, value in raw.items():
            try:
                bindings[TaskType(str(key))] = str(value)
            except ValueError:
                pass
        return bindings

    def _downloaded_tasks(self) -> set[TaskType]:
        names = self._downloaded_model_names()
        return {
            task
            for task, preset in TASK_PRESETS.items()
            if all(step.model in names for step in preset.steps)
        }

    def _downloaded_model_names(self) -> set[str]:
        names = {
            str(value)
            for value in (self._settings.get("downloaded_model_names") or [])
            if str(value).strip()
        }
        # Migrate the UI-only P0-C cache written before model-level tracking.
        for value in self._settings.get("downloaded_models") or []:
            try:
                preset = TASK_PRESETS[TaskType(str(value))]
            except (KeyError, ValueError):
                continue
            names.update(step.model for step in preset.steps)
        names.update(self._locally_present_models())
        return names

    def _locally_present_models(self) -> set[str]:
        """用户手动放进 models/ 的模型也算已下载（自动导入）。

        离线判断，服务没启动时也有效——否则明明有权重，任务卡仍会显示「需下载」。
        外部服务/外部环境不由工作台管理目录，不做此扫描。
        """
        if self._external_url or str(self._settings.get("external_executable", "")).strip():
            return set()
        install_dir = str(self._snap.install_dir or "").strip()
        if not install_dir:
            return set()
        wanted = {step.model for task in TaskType for step in self._steps_for_task(task)}
        try:
            return scan_local_models(install_dir, wanted)
        except Exception:  # 扫描永远不能拖垮状态刷新
            return set()

    def _save_downloaded_model_names(self, names: set[str]) -> None:
        clean = {str(name) for name in names if str(name).strip()}
        self._settings["downloaded_model_names"] = sorted(clean)
        self._settings["downloaded_models"] = sorted(
            task.value
            for task, preset in TASK_PRESETS.items()
            if all(step.model in clean for step in preset.steps)
        )

    def _service_available(self) -> bool:
        if self._external_url:
            return self._client is not None
        return self._service is not None and self._service.running and self._client is not None

    def _rebuild_dependencies(self) -> None:
        service_on = self._service_available()
        downloaded_models = self._downloaded_model_names()
        bindings = self._external_bindings()
        registry = self._registry()
        try:
            external_status = registry.validate() if registry else {}
        except (OSError, ValueError):
            external_status = {task: "missing" for task in bindings}
        dependencies: dict[TaskType, TaskDependency] = {}
        for task in TaskType:
            if task in bindings:
                status = external_status.get(task, "ready" if self._external_url else "missing")
                ready = service_on and status in {"pending", "ready"}
                reason = ""
                badge = "外部模型"
                if status == "missing":
                    reason, badge = "已映射的 MSST 模型文件缺失", "外部模型缺失"
                elif status == "changed":
                    reason, badge = "已映射的 MSST 模型发生变化，请重新扫描", "外部模型已变化"
                elif status == "unsupported":
                    reason = (
                        registry.validation_error(task)
                        if registry is not None
                        else "该外部模型未通过真实加载验证"
                    )
                    reason = reason or "该外部模型未通过真实加载验证"
                    badge = "外部模型不兼容"
                elif status == "pending":
                    badge = "外部模型 · 待首次加载"
                elif not service_on:
                    reason = "需要先启动 PyMSS 服务"
                # 和声伴奏任务改为单阶段（karaoke 模型直接处理原曲取 other），
                # 不再依赖人声模型做二级分离，因此没有额外的前置下载。
                download_bytes = 0
                dependencies[task] = TaskDependency(
                    task,
                    ready,
                    badge,
                    reason,
                    download_bytes=download_bytes,
                    is_external=True,
                )
            else:
                missing_steps: dict[str, SeparationStep] = {
                    step.model: step
                    for step in self._steps_for_task(task)
                    if step.model not in downloaded_models
                }
                missing_size = sum(step.size_bytes for step in missing_steps.values())
                if not missing_steps:
                    dependencies[task] = TaskDependency(
                        task,
                        service_on,
                        "就绪" if service_on else "已下载",
                        "" if service_on else "需要先启动 PyMSS 服务",
                    )
                    continue
                dependencies[task] = TaskDependency(
                    task,
                    False,
                    f"需下载 {format_size(missing_size)}",
                    "推荐模型尚未下载" if service_on else "需要先启动 PyMSS 服务",
                    download_bytes=missing_size,
                )
        self._snap.dependencies = dependencies

    def _ready_state(self) -> ServiceState:
        """Combine service health with the most actionable external-model state."""
        registry = self._registry()
        bindings = self._external_bindings()
        if not registry or not bindings:
            return ServiceState.SERVICE_READY
        try:
            statuses = registry.validate()
        except (OSError, ValueError):
            return ServiceState.EXTERNAL_MODEL_MISSING
        values = {statuses.get(task, "missing") for task in bindings}
        if "missing" in values:
            return ServiceState.EXTERNAL_MODEL_MISSING
        if "changed" in values:
            return ServiceState.EXTERNAL_MODEL_CHANGED
        if "unsupported" in values:
            return ServiceState.EXTERNAL_MODEL_UNSUPPORTED
        if values and values <= {"ready"}:
            return ServiceState.EXTERNAL_MODEL_READY
        return ServiceState.SERVICE_READY

    def _set_ready_state(self) -> None:
        with self._lock:
            self._rebuild_dependencies()
        self._set_state(self._ready_state())

    # ---- wizard and installation -------------------------------------
    def start_wizard(self, flow: str) -> None:
        if flow not in {
            FLOW_FULL,
            FLOW_REUSE_MSST,
            FLOW_EXISTING,
            FLOW_REMAP_MSST,
            FLOW_UPGRADE,
        }:
            raise ValueError(f"未知配置流程：{flow}")
        if flow not in {FLOW_EXISTING, FLOW_REMAP_MSST, FLOW_UPGRADE}:
            self._set_state(ServiceState.LOCATION_REQUIRED)

    def confirm_install_location(self, path: str) -> None:
        raw = str(path or "").strip()
        if not raw:
            raise ValueError("请选择 PyMSS 安装目录。")
        target = Path(raw).expanduser()
        with self._lock:
            self._snap.install_dir = str(target.resolve())
        self._emit()

    def prepare_install_choice(self) -> tuple[str, int]:
        """Freeze this confirmation's hardware variant before any large transfer."""
        variant = managed_runtime_variant(
            prefer_cuda=(
                bool(self._settings["prefer_cuda"])
                if "prefer_cuda" in self._settings
                else None
            )
        )
        self._settings["runtime_variant"] = variant
        # The base is currently about 153 MB. Keep conservative headroom while
        # the exact package size remains authoritative after fetching its small
        # manifest; the large torch wheel size is pinned exactly.
        estimated_base = 160 * 1024**2
        total = estimated_base + int(TORCH_WHEELS[variant]["size"])
        label = "CPU" if variant == PYMSS_CPU_VARIANT else "NVIDIA CUDA 12.8"
        return label, total

    def start_install(self) -> None:
        if not self._snap.install_dir:
            self._fail("尚未选择 PyMSS 安装目录。")
            return
        self._install_cancel = threading.Event()
        variant = str(self._settings.get("runtime_variant", "")).strip()
        if variant not in TORCH_WHEELS:
            try:
                variant = managed_runtime_variant(
                    prefer_cuda=(
                        bool(self._settings["prefer_cuda"])
                        if "prefer_cuda" in self._settings
                        else None
                    )
                )
            except Exception as exc:
                self._fail(exc)
                return
        self._settings["runtime_variant"] = variant
        self._snap.download_done = 0
        self._snap.download_total = 0
        self._set_state(ServiceState.RUNTIME_DOWNLOADING)
        install_dir = self._snap.install_dir

        def operation():
            preflight_install_destination(install_dir)
            if not self._stop_owned_service(5.0):
                raise RuntimeError("无法停止正在运行的 PyMSS 服务，请稍后重试。")
            manifest_url = runtime_manifest_url(variant)
            self._log(f"PyMSS Runtime 清单：{manifest_url}")
            package = fetch_runtime_package(manifest_url)
            for index, part in enumerate(package.archive_parts, start=1):
                self._log(f"PyMSS 底座分片 {index}：{part.url}")
            if package.torch_wheel is not None:
                self._log(f"PyTorch wheel：{package.torch_wheel.url}")
            with self._lock:
                self._snap.download_total = package.download_size
            self._emit()

            def progress(done: int, total: int) -> None:
                with self._lock:
                    self._snap.download_done = done
                    self._snap.download_total = total
                self._emit()

            installer = self._runtime_installer_factory()
            result = installer.install(
                package,
                install_dir,
                progress=progress,
                cancelled=self._install_cancel,
                post_install_check=self._post_install_smoke,
            )
            return result

        def success(result) -> None:
            with self._lock:
                self._snap.download_done = self._snap.download_total
            self._set_state(ServiceState.RUNTIME_VERIFYING)
            self._settings["install_dir"] = install_dir
            self._settings["expected_pymss_version"] = PYMSS_VERSION
            self._settings.pop("external_server_url", None)
            self._settings.pop("external_executable", None)
            self._persist()
            with self._lock:
                self._apply_runtime_validation(result)
                self._rebuild_dependencies()
            self._emit()
            self._log(f"PyMSS {PYMSS_VERSION} 托管 Runtime 安装完成。")

        def failure(exc: Exception) -> None:
            if self._install_cancel.is_set():
                persisted = str(self._settings.get("install_dir", "")).strip()
                if persisted:
                    with self._lock:
                        self._apply_runtime_validation(
                            validate_runtime(persisted, full=True)
                        )
                        self._rebuild_dependencies()
                    self._emit()
                else:
                    self._set_state(ServiceState.LOCATION_REQUIRED)
                self._log("PyMSS Runtime 安装已取消，原有安装保持不变。")
            else:
                self._fail(exc)

        self._log(f"开始下载 PyMSS 托管 Runtime（{variant}）。")
        self._submit(operation, success, failure)

    def _post_install_smoke(self, install_dir: Path) -> None:
        self._set_state(ServiceState.RUNTIME_VERIFYING)
        self._smoke_managed_runtime(str(install_dir))

    def _smoke_managed_runtime(self, install_dir: str) -> None:
        """Start the freshly installed server once and verify its API contract."""
        service = self._service_factory.start(
            install_dir,
            source=self._download_source(),
            device=str(self._settings.get("device", "auto")),
            cancelled=self._install_cancel,
        )
        try:
            checks = service.client.capability_checks()
            failed = [name for name, ok, _detail in checks if not ok]
            if not checks or failed:
                detail = "、".join(failed) if failed else "未返回能力检测结果"
                raise RuntimeError(f"PyMSS 安装后服务能力检查失败：{detail}。")
        finally:
            if not service.stop(timeout_seconds=5.0):
                raise RuntimeError("PyMSS 安装冒烟服务未能正常停止。")

    def cancel_install(self) -> None:
        self._install_cancel.set()
        self._log("正在取消 PyMSS Runtime 下载……")

    def cleanup_incomplete(self) -> None:
        self._install_cancel.set()
        self._scan_cancel.set()
        persisted = str(self._settings.get("install_dir", "")).strip()
        if persisted:
            self._snap.install_dir = persisted
            with self._lock:
                self._apply_runtime_validation(validate_runtime(persisted))
                self._rebuild_dependencies()
            self._emit()
            return
        self._snap = SeparationSnapshot()
        with self._lock:
            self._rebuild_dependencies()
        self._emit()

    def repair_install(self) -> None:
        self.start_install()

    def reinstall(self) -> None:
        self.start_install()

    def relocate_install(self, path: str) -> None:
        self.confirm_install_location(path)
        self._settings["install_dir"] = self._snap.install_dir
        result = validate_runtime(self._snap.install_dir)
        with self._lock:
            self._apply_runtime_validation(result)
            self._rebuild_dependencies()
        self._persist()
        self._emit()

    def remove_configuration(self) -> None:
        self._install_cancel.set()
        self._task_cancel.set()
        self._stop_owned_service(3.0)
        for key in (
            "install_dir",
            "external_executable",
            "external_server_url",
            "expected_pymss_version",
            "probed_pymss_version",
            "downloaded_models",
            "downloaded_model_names",
            "external_bindings",
        ):
            self._settings.pop(key, None)
        with self._lock:
            self._snap = SeparationSnapshot()
            self._client = None
            self._external_url = False
            self._rebuild_dependencies()
        self._persist()
        self._emit()

    # ---- old MSST discovery ------------------------------------------
    def start_msst_scan(self, root: str) -> None:
        self._scan_cancel.set()
        self._scan_cancel = threading.Event()
        self.msstScanStarted.emit()
        cancelled = self._scan_cancel

        def success(candidates: list[ExternalModelCandidate]) -> None:
            if cancelled.is_set():
                return
            self._scan_candidates = {item.candidate_id: item for item in candidates}
            self._settings["legacy_msst_root"] = str(Path(root).resolve())
            self._persist()
            self.msstScanFinished.emit(candidates)
            self._log(f"MSST 模型扫描完成，发现 {len(candidates)} 个任务候选。")

        def failure(exc: Exception) -> None:
            if not cancelled.is_set():
                self.msstScanFailed.emit(str(exc))

        self._submit(lambda: scan_msst_models(root, cancelled=cancelled), success, failure)

    def cancel_msst_scan(self) -> None:
        self._scan_cancel.set()

    def bind_external_model(self, task: TaskType, candidate_id: str) -> None:
        candidate = self._scan_candidates.get(candidate_id)
        if candidate is None or candidate.task is not task or not candidate.bindable:
            raise ValueError("所选 MSST 模型不是当前任务的有效候选。")
        registry = self._registry()
        if registry is None:
            raise RuntimeError("请先安装 PyMSS 底座，再映射 MSST 模型。")
        name = registry.bind(task, candidate)
        raw = self._settings.setdefault("external_bindings", {})
        raw[task.value] = name
        self._persist()
        with self._lock:
            self._rebuild_dependencies()
        self._emit()
        self._log(f"已将 {candidate.display_name} 映射到{TASK_SPECS[task].title}。")

    def import_local_model(
        self,
        task: TaskType,
        *,
        weight_path: str,
        config_path: str,
        model_type: str,
        display_name: str = "",
    ) -> None:
        registry = self._registry()
        if registry is None:
            self.localImportFailed.emit("请先安装 PyMSS 底座，再导入本地模型。")
            return

        def operation() -> ExternalModelCandidate:
            # 哈希大文件可能要几秒，放到工作线程，别卡住 GUI。
            return build_local_candidate(
                weight_path=weight_path,
                config_path=config_path or None,
                model_type=model_type,
                task=task,
                display_name=display_name,
            )

        def success(candidate: ExternalModelCandidate) -> None:
            if not candidate.bindable:
                self.localImportFailed.emit(
                    f"{candidate.status}：{candidate.detail}".strip("：")
                )
                return
            name = registry.bind(task, candidate)
            raw = self._settings.setdefault("external_bindings", {})
            raw[task.value] = name
            # 本地导入的模型直接顶掉该任务的 catalog 覆盖，避免两套配置打架。
            self._scan_candidates[candidate.candidate_id] = candidate
            self._persist()
            with self._lock:
                self._rebuild_dependencies()
            self._emit()
            self._log(
                f"已导入本地模型 {candidate.display_name} 并绑定到"
                f"{TASK_SPECS[task].title}；原文件未被复制或修改。"
            )
            self.localImportFinished.emit(candidate)

        self._submit(
            operation,
            success,
            lambda exc: self.localImportFailed.emit(str(exc).strip() or type(exc).__name__),
        )

    def unbind_external_model(self, task: TaskType) -> None:
        registry = self._registry()
        if registry is not None:
            registry.unbind(task)
        raw = self._settings.get("external_bindings")
        removed = False
        if isinstance(raw, dict):
            removed = raw.pop(task.value, None) is not None
        if removed:
            self._persist()
            self._log(f"已移除{TASK_SPECS[task].title}的外部模型引用；原文件未被修改。")
        with self._lock:
            self._rebuild_dependencies()
        self._emit()

    # ── 设置：按任务自选模型与输出轨 ──────────────────────────────
    def request_catalog_models(self) -> None:
        client = self._client
        if client is None:
            self.catalogModelsFailed.emit("请先启动 PyMSS 服务，再选择模型。")
            return
        def operation() -> list[CatalogModel]:
            rows = client.catalog_models(supported=True)
            models: list[CatalogModel] = []
            for row in rows:
                info = row.get("pymss") if isinstance(row, dict) else None
                if not isinstance(info, dict):
                    continue
                name = str(info.get("name") or "").strip()
                if not name:
                    continue
                local = info.get("local") if isinstance(info.get("local"), dict) else {}
                models.append(
                    CatalogModel(
                        name=name,
                        category=str(info.get("category") or ""),
                        architecture=str(info.get("architecture") or ""),
                        size_bytes=int(info.get("size_bytes") or 0),
                        downloaded=bool(local.get("complete")),
                    )
                )
            models.sort(key=lambda item: (item.category, item.name))
            return models

        self._submit(
            operation,
            lambda models: self.catalogModelsFinished.emit(models),
            lambda exc: self.catalogModelsFailed.emit(
                f"读取模型列表失败：{exc}".strip()
            ),
            detached=bool(self._external_url),
        )

    def request_model_stems(self, model: str) -> None:
        name = str(model or "").strip()
        if not name:
            return
        client = self._client
        if client is None:
            self.modelStemsFailed.emit(name, "请先启动 PyMSS 服务。")
            return
        source = self._download_source()
        model_dir = str(Path(self._snap.install_dir) / "models") if self._snap.install_dir else ""

        def operation() -> tuple[str, ...]:
            text = client.model_config_text(name, source=source, model_dir=model_dir or None)
            return parse_model_stems(text)

        def success(stems: tuple[str, ...]) -> None:
            if stems:
                self.modelStemsFinished.emit(name, stems)
            else:
                # 宁可让用户换一个模型，也不给出可能错误的轨名。
                self.modelStemsFailed.emit(
                    name, "无法从该模型的配置中读出输出轨，请换一个模型。"
                )

        self._submit(
            operation,
            success,
            lambda exc: self.modelStemsFailed.emit(name, f"读取模型配置失败：{exc}".strip()),
            detached=bool(self._external_url),
        )

    def set_task_model(self, task: TaskType, model: str, stem: str, size_bytes: int) -> None:
        raw = self._settings.get(TASK_MODEL_OVERRIDES_KEY)
        overrides = dict(raw) if isinstance(raw, dict) else {}
        name, track = str(model or "").strip(), str(stem or "").strip()
        if not name or not track:
            if overrides.pop(task.value, None) is None:
                return
            self._log(f"{TASK_SPECS[task].title}已恢复为推荐模型。")
        else:
            overrides[task.value] = {
                "model": name,
                "stem": track,
                "size_bytes": max(0, int(size_bytes or 0)),
            }
            self._log(f"{TASK_SPECS[task].title}改用模型 {name}（输出轨 {track}）。")
        self._settings[TASK_MODEL_OVERRIDES_KEY] = overrides
        self._persist()
        self._refresh_remote_model_status()

    def finish_external_mapping(self) -> None:
        """Make PyMSS reload its process-local user-model registry cache."""
        self._persist()
        if self._service is None or not self._service.running:
            with self._lock:
                self._rebuild_dependencies()
            self._emit()
            return
        self._set_state(ServiceState.SERVICE_STOPPING)

        def restart(_stopped: bool) -> None:
            if not self._shutdown_requested:
                self.start_service()

        self._submit(lambda: self._stop_owned_service(5.0), restart)
        self._log("外部模型映射已更新，正在重启托管服务以加载新清单。")

    def suggested_msst_root(self) -> str:
        return str(self._settings.get("legacy_msst_root", ""))

    def bound_external_candidate(self, task: TaskType) -> str:
        registry = self._registry()
        return registry.candidate_id(task) if registry is not None else ""

    # ---- existing environment ---------------------------------------
    def _external_work_root(self) -> Path:
        configured = str(self._settings.get("external_work_dir", "")).strip()
        if configured:
            return Path(configured)
        appdata = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        root = Path(appdata) if appdata else Path.cwd()
        return root / "Karaoke Studio" / "PyMSSExternal"

    def _download_source(self) -> str:
        source = str(self._settings.get("download_source", "modelscope")).strip()
        if source == "github":
            source = "huggingface"
            self._settings["download_source"] = source
        if source not in {"modelscope", "huggingface", "hf-mirror"}:
            source = "modelscope"
        return source

    def _download_sources(self) -> tuple[str, ...]:
        selected = self._download_source()
        return tuple(
            dict.fromkeys((selected, "modelscope", "huggingface", "hf-mirror"))
        )

    def _local_model_dir(self) -> Path | None:
        """Return the model directory only when it is visible on this computer."""
        if self._external_url:
            return None
        if str(self._settings.get("external_executable", "")).strip():
            return self._existing_model_dir()
        install_dir = str(self._snap.install_dir or "").strip()
        return Path(install_dir) / "models" if install_dir else None

    @staticmethod
    def _catalog_file_names(files: object) -> set[str]:
        names: set[str] = set()
        if not isinstance(files, list):
            return names
        for item in files:
            if not isinstance(item, dict):
                continue
            for key in ("filename", "path", "local_path", "remote_url"):
                value = str(item.get(key, "")).strip()
                if not value:
                    continue
                path = unquote(urlsplit(value).path) if "://" in value else value
                name = Path(path.replace("\\", "/")).name
                if name:
                    names.add(name.removesuffix(".part"))
        return names

    @staticmethod
    def _downloaded_file_bytes(model_dir: Path, file_names: set[str]) -> int:
        """Measure the currently retained bytes for one catalog model."""
        if not model_dir.is_dir():
            return 0
        total = 0
        try:
            paths = model_dir.rglob("*")
            for path in paths:
                try:
                    if not path.is_file():
                        continue
                    base_name = path.name.removesuffix(".part")
                    if file_names and base_name not in file_names:
                        continue
                    if not file_names and not path.name.endswith(".part"):
                        continue
                    total += path.stat().st_size
                except OSError:
                    continue
        except OSError:
            return 0
        return total

    def _set_model_download_progress(self, done: int, total: int) -> None:
        with self._lock:
            if self._snap.state is not ServiceState.MODEL_DOWNLOADING:
                return
            bounded = min(max(0, int(done)), max(0, int(total)))
            if bounded == self._snap.download_done:
                return
            self._snap.download_done = bounded
            self._progress.download_done = bounded
            progress = copy.deepcopy(self._progress)
        self._emit()
        if not self._shutdown_requested:
            self.taskProgressChanged.emit(progress)

    def _monitor_model_download(
        self,
        *,
        model_dir: Path,
        file_names: set[str],
        completed_before: int,
        expected_bytes: int,
        total_bytes: int,
        stopped: threading.Event,
    ) -> None:
        while not stopped.wait(0.2):
            retained = self._downloaded_file_bytes(model_dir, file_names)
            current = completed_before + min(retained, expected_bytes)
            self._set_model_download_progress(current, total_bytes)

    def _separation_progress_path(self) -> Path | None:
        if self._external_url:
            return None
        service_root = getattr(self._service, "install_dir", None)
        if service_root:
            return Path(service_root) / "logs" / "separation-progress.json"
        install_dir = str(self._snap.install_dir or "").strip()
        return (
            Path(install_dir) / "logs" / "separation-progress.json"
            if install_dir
            else None
        )

    def _set_separation_progress(self, done: int, total: int) -> None:
        with self._lock:
            bounded_total = max(1, int(total))
            bounded_done = min(max(0, int(done)), bounded_total)
            if (
                bounded_done == self._progress.processing_done
                and bounded_total == self._progress.processing_total
                and self._progress.show_processing
            ):
                return
            self._progress.processing_done = bounded_done
            self._progress.processing_total = bounded_total
            self._progress.show_processing = True
            progress = copy.deepcopy(self._progress)
        if not self._shutdown_requested:
            self.taskProgressChanged.emit(progress)

    def _monitor_separation_progress(
        self, progress_path: Path, stopped: threading.Event
    ) -> None:
        last_updated = 0.0
        while not stopped.wait(0.2):
            try:
                payload = json.loads(progress_path.read_text(encoding="utf-8"))
                updated = float(payload.get("updated_at", 0))
                if updated <= last_updated or payload.get("status") != "running":
                    continue
                last_updated = updated
                self._set_separation_progress(
                    int(payload.get("done", 0)), int(payload.get("total", 1))
                )
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue

    def _existing_model_dir(self) -> Path:
        configured = str(self._settings.get("external_model_dir", "")).strip()
        environment = os.environ.get("PYMSS_MODEL_DIR", "").strip()
        return Path(configured or environment or (Path.home() / ".cache" / "pymss" / "models"))

    def _existing_user_models_path(self) -> Path:
        configured = str(self._settings.get("external_user_models", "")).strip()
        environment = os.environ.get("PYMSS_USER_MODELS", "").strip()
        return Path(
            configured
            or environment
            or (Path.home() / ".cache" / "pymss" / "user_models.json")
        )

    def _intermediate_cache(self) -> IntermediateResultCache | None:
        """Return a cache only where model identity can be proven locally."""
        if self._external_url or str(self._settings.get("external_executable", "")).strip():
            return None
        if not str(self._snap.install_dir or "").strip():
            return None
        return IntermediateResultCache(Path(self._snap.install_dir) / "cache" / "intermediate")

    def _intermediate_cache_metadata(
        self,
        task: TaskType,
        step_index: int,
        step: SeparationStep,
        input_path: Path,
        output_format: str,
    ) -> dict | None:
        cache = self._intermediate_cache()
        if cache is None:
            return None
        model_identity: dict[str, object] = {"name": step.model}
        external_task = next(
            (
                bound_task
                for bound_task, model_name in self._external_bindings().items()
                if model_name == step.model
            ),
            None,
        )
        if external_task is not None:
            registry = self._registry()
            if registry is None:
                return None
            record = next(
                (
                    model
                    for model in registry.load()["models"]
                    if isinstance(model, dict) and model.get("name") == step.model
                ),
                None,
            )
            krok = record.get("krok", {}) if isinstance(record, dict) else {}
            if not str(krok.get("sha256", "")):
                return None
            model_identity.update(
                {
                    "weight_sha256": str(krok.get("sha256", "")),
                    "config_sha256": str(krok.get("config_sha256", "")),
                    "candidate_id": str(krok.get("candidate_id", "")),
                }
            )
        preset = TASK_PRESETS[task]
        return {
            "schema": 1,
            "input": input_fingerprint(input_path, cancelled=self._task_cancel),
            "pymss_version": PYMSS_VERSION,
            "preset_id": preset.preset_id,
            "preset_version": preset.version,
            "task": task.value,
            "step_index": step_index,
            "step": {
                "model": model_identity,
                "stems": list(step.stems),
                "input_from_previous": step.input_from_previous,
                "inference_params": list(step.inference_params),
            },
            "output_format": output_format,
        }

    @staticmethod
    def _version_command(executable: str) -> list[str]:
        path = Path(executable)
        python = path
        if not path.name.lower().startswith("python"):
            candidate = path.parent.parent / "python.exe"
            if candidate.is_file():
                python = candidate
        if python.name.lower().startswith("python"):
            return [
                str(python),
                "-c",
                "from importlib.metadata import version; print(version('pymss'))",
            ]
        return [str(path), "--help"]

    def _require_external_version(self, executable: str) -> str:
        completed = subprocess.run(
            self._version_command(executable),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        output = (completed.stdout + " " + completed.stderr).strip()
        match = re.search(r"(?<!\d)(\d+\.\d+\.\d+)(?!\d)", output)
        version = match.group(1) if match else ""
        if completed.returncode != 0 or not version:
            raise _ExternalVersionMismatch("无法获取外部 PyMSS 环境版本，请重新检测环境。")
        if version != PYMSS_VERSION:
            raise _ExternalVersionMismatch(
                f"外部环境为 PyMSS {version}，当前工作台需要 {PYMSS_VERSION}。"
            )
        return version

    def _probe_executable(self, executable: str) -> tuple[list[tuple[str, bool, str]], str]:
        path = Path(executable)
        if not path.is_file():
            return [("PyMSS 可执行环境", False, "所选文件不存在")], ""
        completed = subprocess.run(
            self._version_command(str(path)),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        output = (completed.stdout + " " + completed.stderr).strip()
        match = re.search(r"(?<!\d)(\d+\.\d+\.\d+)(?!\d)", output)
        version = match.group(1) if match else ""
        checks = [
            ("获取 PyMSS 版本", completed.returncode == 0 and bool(version), version or output),
            ("版本在兼容范围内", version == PYMSS_VERSION, version or "无法识别版本"),
        ]
        if not all(ok for _name, ok, _detail in checks):
            return checks, version
        service = self._service_factory.start(
            self._external_work_root(),
            executable=path,
            model_dir=self._existing_model_dir(),
            user_models_path=self._existing_user_models_path(),
            source=self._download_source(),
            device=str(self._settings.get("device", "auto")),
        )
        try:
            capability = service.client.capability_checks()
        finally:
            service.stop()
        return [*checks, *capability], version

    def start_existing_check(
        self, *, executable: str = "", server_url: str = "", api_key: str = ""
    ) -> None:
        self.existingCheckStarted.emit()
        key = (str(executable).strip(), str(server_url).strip(), str(api_key))
        self._existing_check_cache = None

        def operation():
            if server_url:
                client = PyMSSClient(server_url, api_key=api_key)
                return client.capability_checks(), "", client
            checks, version = self._probe_executable(executable)
            return checks, version, None

        def success(result) -> None:
            checks, version, client = result
            if checks and all(ok for _name, ok, _detail in checks):
                self._existing_check_cache = (key, client, version)
            if version:
                self._settings["probed_pymss_version"] = version
            self.existingCheckFinished.emit(checks)

        self._submit(
            operation,
            success,
            lambda exc: self.existingCheckFailed.emit(str(exc)),
        )

    def connect_existing(
        self, *, executable: str = "", server_url: str = "", api_key: str = ""
    ) -> None:
        key = (str(executable).strip(), str(server_url).strip(), str(api_key))
        cached = self._existing_check_cache
        if cached is None or cached[0] != key:
            raise RuntimeError("连接信息尚未通过能力检测，请重新检测。")
        if server_url:
            client = cached[1]
            if client is None:
                raise RuntimeError("外部 PyMSS 服务检测结果无效，请重新检测。")
            self._client = client
            self._external_url = True
            self._external_api_key = api_key
            self._settings["external_server_url"] = client.base_url
            self._settings.pop("external_executable", None)
            self._settings.pop("install_dir", None)
            self._snap.install_dir = ""
            self._snap.pymss_version = "API v1"
            info = client.health()
            self._snap.device = str(info.get("device") or "外部服务")
            self._set_ready_state()
        elif executable:
            self._settings["external_executable"] = str(Path(executable).resolve())
            self._settings.pop("external_server_url", None)
            self._settings.pop("install_dir", None)
            self._snap.install_dir = ""
            self._snap.pymss_version = str(
                cached[2] or self._settings.get("probed_pymss_version", PYMSS_VERSION)
            )
            self._external_api_key = ""
            self._set_state(ServiceState.INSTALLED_STOPPED)
        else:
            raise ValueError("请选择 PyMSS 环境或填写服务地址。")
        self._settings["expected_pymss_version"] = PYMSS_VERSION
        self._existing_check_cache = None
        with self._lock:
            self._rebuild_dependencies()
        self._persist()
        self._emit()

    # ---- service lifecycle -------------------------------------------
    def start_service(self) -> None:
        if self._external_url and self._client is not None:
            self.refresh()
            return
        executable = str(self._settings.get("external_executable", "")).strip() or None
        root = self._snap.install_dir if not executable else str(self._external_work_root())
        if not root:
            self._fail("尚未配置可启动的 PyMSS 环境。")
            return
        self._service_cancel = threading.Event()
        self._set_state(ServiceState.SERVICE_STARTING)

        def operation():
            validation = None
            if not executable:
                # Normal startup checks the manifest, versions, presence and
                # sizes only. Full hashing of multi-GB Torch files is reserved
                # for explicit diagnostics and failure recovery.
                validation = validate_runtime(root, full=False)
                if self._service_cancel.is_set():
                    raise InterruptedError("PyMSS 服务启动已取消。")
                if validation.status is not RuntimeStatus.READY:
                    return None, "", validation
            external_version = self._require_external_version(executable) if executable else ""
            service = self._service_factory.start(
                root,
                executable=executable,
                model_dir=self._existing_model_dir() if executable else None,
                user_models_path=self._existing_user_models_path() if executable else None,
                source=self._download_source(),
                device=str(self._settings.get("device", "auto")),
                cancelled=self._service_cancel,
            )
            if self._service_cancel.is_set():
                service.stop(timeout_seconds=2.0)
                raise InterruptedError("PyMSS 服务启动已取消。")
            return service, external_version, validation

        def success(result) -> None:
            service, external_version, validation = result
            if validation is not None and validation.status is not RuntimeStatus.READY:
                with self._lock:
                    self._apply_runtime_validation(validation)
                    self._rebuild_dependencies()
                self._emit()
                return
            if self._service_cancel.is_set():
                service.stop(timeout_seconds=2.0)
                self._set_state(ServiceState.INSTALLED_STOPPED)
                return
            self._service = service
            self._client = service.client
            self._external_url = False
            health = service.client.health()
            self._snap.device = str(health.get("device") or "自动选择")
            self._snap.pymss_version = external_version or self._snap.pymss_version or PYMSS_VERSION
            if external_version:
                self._settings["probed_pymss_version"] = external_version
                self._settings["expected_pymss_version"] = PYMSS_VERSION
                self._persist()
            self._refresh_remote_model_status()
            self._set_ready_state()
            self._log(f"PyMSS 服务已启动：http://127.0.0.1:{service.port}")

        def failure(exc: Exception) -> None:
            if self._service_cancel.is_set():
                self._set_state(ServiceState.INSTALLED_STOPPED)
                self._log("PyMSS 服务启动已取消。")
            elif isinstance(exc, _ExternalVersionMismatch):
                self._set_state(ServiceState.EXTERNAL_VERSION_INCOMPATIBLE, error=str(exc))
            elif not executable:
                self._diagnose_managed_runtime_failure(exc)
            else:
                self._fail(exc)

        self._submit(operation, success, failure)

    def _stop_owned_service(self, timeout_seconds: float = 5.0) -> bool:
        service = self._service
        self._service = None
        self._client = None
        if service is None:
            return True
        return service.stop(timeout_seconds=timeout_seconds)

    def stop_service(self) -> None:
        if self._external_url:
            self._client = None
            self._external_url = False
            self._set_state(ServiceState.EXTERNAL_OFFLINE)
            return
        self._set_state(ServiceState.SERVICE_STOPPING)

        def success(_result) -> None:
            self._snap.device = ""
            self._snap.current_model = ""
            with self._lock:
                self._rebuild_dependencies()
            self._set_state(ServiceState.INSTALLED_STOPPED)

        self._submit(self._stop_owned_service, success)

    def cancel_start(self) -> None:
        self._service_cancel.set()
        self._submit(
            self._stop_owned_service,
            lambda _r: self._set_state(ServiceState.INSTALLED_STOPPED),
        )

    def refresh(self, *, full: bool = False) -> None:
        service_available = self._service_available()
        managed_service = (
            service_available
            and not self._external_url
            and not str(self._settings.get("external_executable", "")).strip()
        )
        if (
            (self._external_url and self._client is not None)
            or (service_available and not (full and managed_service))
            or self._settings.get("external_server_url")
        ):
            # Never perform network health checks on the Qt GUI thread.
            self._schedule_health_check()
            return
        if self._snap.install_dir:
            if self._runtime_check_future is not None and not self._runtime_check_future.done():
                return
            install_dir = self._snap.install_dir
            if full:
                self._set_state(ServiceState.RUNTIME_VERIFYING)

            def success(result) -> None:
                with self._lock:
                    self._apply_runtime_validation(result)
                    self._rebuild_dependencies()
                    if result.status is RuntimeStatus.READY and managed_service:
                        self._snap.state = self._ready_state()
                self._emit()

            self._runtime_check_future = self._submit(
                lambda: validate_runtime(install_dir, full=full), success, self._fail
            )

    def _diagnose_managed_runtime_failure(self, error: Exception | str) -> None:
        """Hash the managed Runtime after a real execution failure."""
        install_dir = str(self._snap.install_dir or "").strip()
        if (
            not install_dir
            or self._external_url
            or str(self._settings.get("external_executable", "")).strip()
        ):
            self._fail(error)
            return
        active_check = self._runtime_check_future
        if active_check is not None and not active_check.done():
            original_error = str(error).strip() or type(error).__name__
            self._log("已有 Runtime 检查正在进行，完成后继续执行故障完整校验。")
            self._set_state(ServiceState.RUNTIME_VERIFYING)
            active_check.add_done_callback(
                lambda _future: self._diagnose_managed_runtime_failure(original_error)
            )
            return

        original_error = str(error).strip() or type(error).__name__
        self._log(f"运行失败，正在完整校验 PyMSS Runtime：{original_error}")
        self._set_state(ServiceState.RUNTIME_VERIFYING)

        def success(result) -> None:
            if result.status is RuntimeStatus.READY:
                self._log("PyMSS Runtime 完整校验通过，保留原始运行错误。")
                self._fail(original_error)
                return
            with self._lock:
                self._snap.pending_task = None
                self._apply_runtime_validation(result)
                self._rebuild_dependencies()
            self._log(f"PyMSS Runtime 完整校验失败：{result.message}")
            self._emit()

        def failure(diagnostic_error: Exception) -> None:
            self._log(f"PyMSS Runtime 诊断失败：{diagnostic_error}")
            self._fail(original_error)

        self._runtime_check_future = self._submit(
            lambda: validate_runtime(install_dir, full=True), success, failure
        )

    def _refresh_remote_model_status(self) -> None:
        if self._client is None:
            return
        downloaded_models: set[str] = set()
        model_names = {
            step.model
            for preset in TASK_PRESETS.values()
            for step in preset.steps
        }
        # 用户覆盖的模型同样要查本地状态，否则它的下载徽标永远不更新。
        for task in TaskType:
            model_names.update(step.model for step in self._steps_for_task(task))
        for model in model_names:
            try:
                detail = self._client.catalog_model(
                    model,
                    source=self._download_source(),
                )
                local = detail.get("pymss", {}).get("local", {})
                if bool(local.get("complete")):
                    downloaded_models.add(model)
            except Exception:
                pass
        self._save_downloaded_model_names(downloaded_models)
        with self._lock:
            self._rebuild_dependencies()
        self._persist()

    def _schedule_health_check(self) -> None:
        if self._shutdown_requested:
            return
        if self._health_future is not None and not self._health_future.done():
            return
        state = self._snap.state
        if state in {
            ServiceState.PROCESSING,
            ServiceState.MODEL_DOWNLOADING,
            ServiceState.MODEL_LOADING,
            ServiceState.RUNTIME_DOWNLOADING,
            ServiceState.RUNTIME_VERIFYING,
            ServiceState.SERVICE_STARTING,
            ServiceState.SERVICE_STOPPING,
        }:
            return
        client = self._client
        reconnect_url = ""
        if client is None and state is ServiceState.EXTERNAL_OFFLINE:
            reconnect_url = str(self._settings.get("external_server_url", "")).strip()
            if reconnect_url:
                client = PyMSSClient(reconnect_url, api_key=self._external_api_key)
        if client is None:
            return
        if self._service is not None and not self._service.running:
            self._service = None
            self._client = None
            with self._lock:
                self._rebuild_dependencies()
            self._set_state(ServiceState.INSTALLED_STOPPED, error="PyMSS 服务进程已退出。")
            return

        def operation():
            return client, client.health(), bool(reconnect_url)

        def success(result) -> None:
            checked_client, health, reconnected = result
            if health.get("status") != "ok":
                raise RuntimeError("PyMSS 健康检查状态不是 ok。")
            if reconnected:
                self._client = checked_client
                self._external_url = True
                self._log("检测到外部 PyMSS 服务已恢复。")
            self._snap.device = str(health.get("device") or self._snap.device or "自动选择")
            if self._snap.state is ServiceState.EXTERNAL_OFFLINE:
                self._set_ready_state()

        def failure(exc: Exception) -> None:
            if self._external_url or reconnect_url:
                self._client = None
                self._external_url = False
                with self._lock:
                    self._rebuild_dependencies()
                self._set_state(ServiceState.EXTERNAL_OFFLINE, error=str(exc))
            elif self._service is not None:
                self._fail(exc)

        self._health_future = self._submit(operation, success, failure)

    # ---- model download and separation -------------------------------
    def request_task(
        self,
        task: TaskType,
        *,
        input_path: str,
        output_dir: str,
        output_format: str,
    ) -> None:
        if not Path(input_path).is_file():
            self._fail("所选音频素材不存在。")
            return
        if output_format not in {"wav", "flac"}:
            self._fail("当前只支持 WAV 或 FLAC 输出。")
            return
        if not self._service_available():
            self._fail("PyMSS 服务尚未启动。")
            return
        try:
            with Path(input_path).open("rb") as stream:
                stream.read(1)
            target_output = Path(output_dir or Path(input_path).parent)
            target_output.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                prefix=".karaoke-pymss-write-",
                suffix=".tmp",
                dir=target_output,
                delete=True,
            ):
                pass
        except OSError as exc:
            self._fail(f"输入文件或输出目录不可读写：{exc}")
            return
        self._task_context = {
            "input_path": input_path,
            "output_dir": output_dir or str(Path(input_path).parent),
            "output_format": output_format,
        }
        self._task_cancel = threading.Event()
        self._snap.pending_task = task
        self._progress = TaskProgress(
            title=TASK_SPECS[task].title, stage_index=STAGE_PREPARE
        )
        with self._lock:
            self._rebuild_dependencies()
        dependency = self._snap.dependencies.get(task)
        if dependency is not None and not dependency.ready and dependency.download_bytes:
            self._set_state(ServiceState.MODEL_REQUIRED)
            self.taskProgressChanged.emit(copy.deepcopy(self._progress))
            return
        if dependency is None or not dependency.ready:
            self._fail(dependency.reason if dependency else "任务依赖不可用。")
            return
        self._verify_task_models_then_start(task)

    def _verify_task_models_then_start(self, task: TaskType) -> None:
        client = self._client
        if client is None:
            self._fail("PyMSS 服务连接已断开。")
            return
        external_names = set(self._external_bindings().values())
        model_names = tuple(
            dict.fromkeys(
                step.model
                for step in self._steps_for_task(task)
                if step.model not in external_names
            )
        )
        if not model_names:
            self._start_pipeline(task)
            return
        self._progress.stage_index = STAGE_PREPARE
        self._progress.current_file = "检查任务模型完整性"
        self._set_state(ServiceState.MODEL_LOADING)
        self.taskProgressChanged.emit(copy.deepcopy(self._progress))

        def operation() -> dict[str, bool]:
            status: dict[str, bool] = {}
            for model in model_names:
                if self._task_cancel.is_set():
                    raise InterruptedError("任务模型检查已取消。")
                detail = client.catalog_model(model, source=self._download_source())
                local = detail.get("pymss", {}).get("local", {})
                status[model] = bool(local.get("complete"))
            return status

        def success(status: dict[str, bool]) -> None:
            if self._task_cancel.is_set():
                return
            installed = self._downloaded_model_names()
            for model, complete in status.items():
                if complete:
                    installed.add(model)
                else:
                    installed.discard(model)
            self._save_downloaded_model_names(installed)
            self._persist()
            with self._lock:
                self._rebuild_dependencies()
            dependency = self._snap.dependencies.get(task)
            if dependency is not None and not dependency.ready and dependency.download_bytes:
                self._set_state(ServiceState.MODEL_REQUIRED)
                return
            if dependency is None or not dependency.ready:
                self._fail(dependency.reason if dependency else "任务依赖不可用。")
                return
            self._start_pipeline(task)

        def failure(exc: Exception) -> None:
            if self._task_cancel.is_set():
                self._log("任务模型检查已取消。")
            else:
                self._fail(exc)

        self._submit(operation, success, failure)

    def start_model_download(self) -> None:
        task = self._snap.pending_task
        client = self._client
        if task is None or client is None:
            return
        steps = self._steps_for_task(task)
        external_names = set(self._external_bindings().values())
        installed_names = self._downloaded_model_names()
        downloadable_by_name = {
            step.model: step
            for step in steps
            if step.model not in external_names and step.model not in installed_names
        }
        downloadable = tuple(downloadable_by_name.values())
        total_bytes = sum(step.size_bytes for step in downloadable)
        if not downloadable:
            self._start_pipeline(task)
            return
        self._task_cancel = threading.Event()
        self._snap.download_done = 0
        self._snap.download_total = total_bytes
        self._progress.stage_index = STAGE_DOWNLOAD
        self._progress.show_download = True
        self._progress.download_done = 0
        self._progress.download_total = total_bytes
        self._set_state(ServiceState.MODEL_DOWNLOADING)
        self.taskProgressChanged.emit(copy.deepcopy(self._progress))
        sources = self._download_sources()

        def operation():
            done = 0
            seen: set[str] = set()
            for step in downloadable:
                if step.model in seen:
                    continue
                if self._task_cancel.is_set():
                    raise InterruptedError("模型下载已取消。")
                last_error: Exception | None = None
                for source in sources:
                    if self._task_cancel.is_set():
                        raise InterruptedError("模型下载已取消。")
                    try:
                        detail = client.catalog_model(step.model, source=source)
                        files = detail.get("pymss", {}).get("files", [])
                        urls = [
                            str(item.get("remote_url", "")).strip()
                            for item in files
                            if isinstance(item, dict)
                            and str(item.get("remote_url", "")).strip()
                        ]
                        self._log(f"模型 {step.model} 下载源：{source}")
                        for url in urls:
                            self._log(f"模型 {step.model} 文件：{url}")
                        monitor_stop = threading.Event()
                        model_dir = self._local_model_dir()
                        monitor = None
                        if model_dir is not None:
                            monitor = threading.Thread(
                                target=self._monitor_model_download,
                                kwargs={
                                    "model_dir": model_dir,
                                    "file_names": self._catalog_file_names(files),
                                    "completed_before": done,
                                    "expected_bytes": step.size_bytes,
                                    "total_bytes": total_bytes,
                                    "stopped": monitor_stop,
                                },
                                name=f"pymss-model-progress-{step.model}",
                                daemon=True,
                            )
                            monitor.start()
                        try:
                            client.download_model(
                                step.model,
                                source=source,
                                verify=True,
                                timeout_seconds=1800,
                            )
                        finally:
                            monitor_stop.set()
                            if monitor is not None:
                                monitor.join(timeout=1.0)
                        if self._task_cancel.is_set():
                            raise InterruptedError("模型下载已取消。")
                        self._log(f"模型 {step.model} 已通过 {source} 下载并校验。")
                        last_error = None
                        break
                    except Exception as exc:
                        last_error = exc
                        self._log(f"模型源 {source} 下载失败，正在尝试下一来源：{exc}")
                if last_error is not None:
                    raise last_error
                seen.add(step.model)
                done += step.size_bytes
                self._set_model_download_progress(done, total_bytes)
            return task

        def success(completed_task: TaskType) -> None:
            downloaded_models = self._downloaded_model_names()
            downloaded_models.update(step.model for step in downloadable)
            self._save_downloaded_model_names(downloaded_models)
            self._persist()
            with self._lock:
                self._rebuild_dependencies()
            self._start_pipeline(completed_task)

        def failure(exc: Exception) -> None:
            if self._task_cancel.is_set():
                self._log("模型下载已取消。")
                return
            self._fail(exc)

        self._submit(operation, success, failure, detached=self._external_url)

    def _steps_for_task(self, task: TaskType) -> tuple[SeparationStep, ...]:
        binding = self._external_bindings().get(task)
        if not binding:
            return effective_steps(self._settings, task)
        if task is TaskType.VOCAL:
            return (SeparationStep(binding, ("vocals",), ("人声",), 0),)
        if task is TaskType.INSTRUMENTAL:
            return (SeparationStep(binding, ("instrumental",), ("伴奏",), 0),)
        # 和声伴奏：karaoke 模型直接处理原曲，残余轨即「去掉主唱、保留和声」的伴奏。
        return (SeparationStep(binding, ("other",), ("和声伴奏",), 0),)

    def _start_pipeline(self, task: TaskType) -> None:
        client = self._client
        if client is None:
            self._fail("PyMSS 服务连接已断开。")
            return
        self._task_cancel = threading.Event()
        self._progress.show_download = False
        self._progress.show_processing = False
        self._progress.processing_done = 0
        self._progress.processing_total = 0
        self._set_state(ServiceState.MODEL_LOADING)

        def progress(stage: int, model: str = "") -> None:
            self._progress.stage_index = stage
            self._progress.current_file = model
            if stage != STAGE_SEPARATE:
                self._progress.show_processing = False
            self.taskProgressChanged.emit(copy.deepcopy(self._progress))

        def operation():
            context = dict(self._task_context)
            source_path = Path(context["input_path"])
            output_dir = Path(context["output_dir"])
            output_format = context["output_format"]
            steps = self._steps_for_task(task)
            external_tasks = {
                model_name: bound_task
                for bound_task, model_name in self._external_bindings().items()
            }
            with tempfile.TemporaryDirectory(prefix="karaoke-pymss-") as temporary:
                work = Path(temporary)
                current_input = source_path
                collected: dict[str, Path] = {}
                for index, step in enumerate(steps):
                    if self._task_cancel.is_set():
                        raise InterruptedError("音频分离已取消。")
                    cache = self._intermediate_cache() if index < len(steps) - 1 else None
                    cache_metadata = None
                    cached_archive = None
                    if cache is not None:
                        progress(STAGE_PREPARE, "检查可复用的中间结果")
                        try:
                            cache_metadata = self._intermediate_cache_metadata(
                                task, index, step, current_input, output_format
                            )
                            if cache_metadata is not None:
                                cached_archive = cache.lookup(
                                    cache_key(cache_metadata), cancelled=self._task_cancel
                                )
                        except InterruptedError:
                            raise
                        except Exception as exc:
                            self._log(f"中间结果缓存检查失败，改为重新处理：{exc}")

                    if cached_archive is not None:
                        archive = cached_archive
                        self._log(f"已复用 {TASK_SPECS[task].title} 的已校验中间结果。")
                        if index == 0:
                            estimated_output = max(
                                64 * 1024**2,
                                archive.stat().st_size * len(TASK_SPECS[task].output_labels),
                            )
                            if shutil.disk_usage(output_dir).free < estimated_output:
                                raise OSError("输出目录空间不足，无法安全保存分离结果。")
                    else:
                        progress(STAGE_PREPARE, current_input.name)
                        pcm, _duration = prepare_pcm(
                            current_input,
                            work / f"step-{index}-pcm",
                            ffmpeg_dir=self._ffmpeg_dir or None,
                            cancelled=self._task_cancel,
                        )
                        if index == 0:
                            estimated_output = (
                                int(
                                    _duration
                                    * 44100
                                    * 2
                                    * 4
                                    * len(TASK_SPECS[task].output_labels)
                                    * 1.1
                                )
                                + 64 * 1024**2
                            )
                            if shutil.disk_usage(output_dir).free < estimated_output:
                                raise OSError("输出目录空间不足，无法安全保存分离结果。")
                        progress(STAGE_LOAD, step.model)
                        external_task = external_tasks.get(step.model)
                        try:
                            load_result = client.load_model(
                                step.model,
                                source=self._download_source(),
                                inference_params=step.params(),
                            )
                        except Exception as exc:
                            if external_task is not None:
                                raise _ExternalModelLoadFailure(external_task, exc) from exc
                            raise
                        if external_task is not None:
                            registry = self._registry()
                            if registry is not None:
                                registry.mark_verified(external_task)
                        model_card = (
                            load_result.get("model", {})
                            if isinstance(load_result, dict)
                            else {}
                        )
                        loaded_model_id = str(
                            model_card.get("id", "")
                            if isinstance(model_card, dict)
                            else ""
                        ).strip() or step.model
                        if loaded_model_id != step.model:
                            self._log(
                                f"模型 {step.model} 已加载为服务内部 ID：{loaded_model_id}"
                            )
                        self._snap.current_model = loaded_model_id
                        self._emit()
                        progress(STAGE_SEPARATE, step.model)
                        archive = work / f"step-{index}.zip"
                        progress_path = self._separation_progress_path()
                        monitor_stop = threading.Event()
                        monitor = None
                        if progress_path is not None:
                            try:
                                progress_path.unlink(missing_ok=True)
                            except OSError:
                                pass
                            processing_total = max(1, int(_duration + 0.999))
                            self._set_separation_progress(0, processing_total)
                            monitor = threading.Thread(
                                target=self._monitor_separation_progress,
                                args=(progress_path, monitor_stop),
                                name="pymss-separation-progress",
                                daemon=True,
                            )
                            monitor.start()
                        try:
                            client.separate_pcm(
                                pcm,
                                archive,
                                model=loaded_model_id,
                                sample_rate=44100,
                                channels=2,
                                stems=step.stems,
                                output_audio_format=output_format,
                                cancelled=self._task_cancel,
                            )
                            if progress_path is not None:
                                self._set_separation_progress(
                                    processing_total, processing_total
                                )
                        finally:
                            monitor_stop.set()
                            if monitor is not None:
                                monitor.join(timeout=1.0)
                        if cache is not None and cache_metadata is not None:
                            try:
                                cache.store(
                                    cache_key(cache_metadata),
                                    archive,
                                    cache_metadata,
                                    cancelled=self._task_cancel,
                                )
                            except InterruptedError:
                                raise
                            except Exception as exc:
                                self._log(f"中间结果缓存保存失败，本次结果仍会继续输出：{exc}")
                    progress(STAGE_ENCODE, archive.name)
                    labels = {
                        stem: label
                        for stem, label in zip(step.stems, step.output_labels)
                        if label
                    }
                    extracted = extract_result_stems(
                        archive,
                        work / f"step-{index}-out",
                        labels=labels,
                        base_name=f"step-{index}",
                    )
                    for stem, label in zip(step.stems, step.output_labels):
                        if label:
                            collected[label] = self._stem_path(extracted, stem)
                    if index < len(steps) - 1:
                        wanted = step.input_from_previous or steps[index + 1].input_from_previous
                        current_input = self._stem_path(extracted, wanted)
                progress(STAGE_SAVE)
                output_dir.mkdir(parents=True, exist_ok=True)
                return self._publish_outputs(
                    collected,
                    output_dir,
                    source_path.stem,
                    TASK_SPECS[task].output_labels,
                )

        def success(files: list[ResultFile]) -> None:
            self._snap.pending_task = None
            self._snap.download_done = 0
            self._snap.download_total = 0
            self._set_ready_state()
            self.resultReady.emit(
                TaskResult(
                    task=task,
                    title=TASK_SPECS[task].title,
                    finished_at=time.strftime("%H:%M:%S"),
                    files=files,
                )
            )
            self._log(f"{TASK_SPECS[task].title}完成。")

        def failure(exc: Exception) -> None:
            if self._task_cancel.is_set():
                self._log("音频分离已取消。")
                return
            if isinstance(exc, _ExternalModelLoadFailure):
                registry = self._registry()
                if registry is not None:
                    registry.mark_unsupported(exc.task, str(exc))
                with self._lock:
                    self._rebuild_dependencies()
                self._log(
                    f"外部模型未通过真实加载验证：{TASK_SPECS[exc.task].title}：{exc}"
                )
                self._set_state(ServiceState.EXTERNAL_MODEL_UNSUPPORTED, error=str(exc))
                return
            if self._progress.stage_index in {STAGE_LOAD, STAGE_SEPARATE}:
                self._diagnose_managed_runtime_failure(exc)
                return
            self._fail(exc)

        self._submit(operation, success, failure, detached=self._external_url)

    @staticmethod
    def _stem_path(outputs: dict[str, Path], stem: str) -> Path:
        wanted = str(stem or "").lower()
        for key, path in outputs.items():
            if key.lower() == wanted:
                return path
        raise ValueError(f"PyMSS 结果中缺少所需音轨：{stem}")

    @staticmethod
    def _unique_output_path(source: Path, output_dir: Path, base_name: str) -> Path:
        candidate = output_dir / f"{base_name}{source.suffix.lower()}"
        if candidate.exists():
            for index in range(2, 10000):
                alternative = output_dir / f"{base_name} ({index}){source.suffix.lower()}"
                if not alternative.exists():
                    candidate = alternative
                    break
            else:
                raise FileExistsError("输出目录中存在过多同名音频文件。")
        return candidate

    @classmethod
    def _publish_outputs(
        cls,
        collected: dict[str, Path],
        output_dir: Path,
        source_stem: str,
        labels: tuple[str, ...],
    ) -> list[ResultFile]:
        prepared: list[tuple[str, Path, Path]] = []
        published: list[Path] = []
        try:
            reserved: set[Path] = set()
            for label in labels:
                source = collected.get(label)
                if source is None:
                    raise ValueError(f"分离结果中缺少用户输出：{label}")
                candidate = cls._unique_output_path(
                    source, output_dir, f"{source_stem}_{label}"
                )
                while candidate in reserved:
                    candidate = cls._unique_output_path(
                        source, output_dir, f"{candidate.stem} (2)"
                    )
                reserved.add(candidate)
                partial = candidate.with_suffix(candidate.suffix + ".part")
                prepared.append((label, partial, candidate))
                shutil.copyfile(source, partial)
            for _label, partial, candidate in prepared:
                os.replace(partial, candidate)
                published.append(candidate)
            return [
                ResultFile(str(candidate), label, candidate.stat().st_size)
                for label, _partial, candidate in prepared
            ]
        except Exception:
            for path in published:
                path.unlink(missing_ok=True)
            raise
        finally:
            for _label, partial, _candidate in prepared:
                partial.unlink(missing_ok=True)

    def cancel_task(self) -> None:
        self._task_cancel.set()
        self._snap.pending_task = None
        if self._external_url:
            self._set_ready_state()
            self._log("已请求取消任务；外部服务中的请求可能仍会继续到当前响应结束。")
            return
        self._set_state(ServiceState.SERVICE_STARTING)

        def restart(_stopped=True) -> None:
            if not self._shutdown_requested:
                self.start_service()

        self._submit(lambda: self._stop_owned_service(2.0), restart)
        self._log("已停止当前任务，正在重启 PyMSS 服务。")

    # ---- host shutdown ------------------------------------------------
    def shutdown(self, timeout_ms: int = 5000) -> bool:
        self._health_timer.stop()
        self._install_cancel.set()
        self._task_cancel.set()
        self._service_cancel.set()
        self._scan_cancel.set()
        self._shutdown_requested = True
        deadline = time.monotonic() + max(0, timeout_ms) / 1000.0
        stopped = self._stop_owned_service(max(0.5, deadline - time.monotonic()))
        with self._lock:
            futures = tuple(self._futures - self._detached_futures)
        if futures:
            wait(futures, timeout=max(0.0, deadline - time.monotonic()))
        complete = stopped and all(future.done() for future in futures)
        if self._owns_executor:
            self._executor.shutdown(wait=False, cancel_futures=True)
        return complete


__all__ = ["RealSeparationBackend"]
