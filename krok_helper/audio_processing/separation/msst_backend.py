"""用已有 MSST 环境执行分离的后端（``SeparationBackend`` 的第三个实现）。

与 :class:`RealSeparationBackend` 的关键区别：

* **没有 HTTP 服务**：分离由常驻桥接进程完成（:mod:`msst_service`）；
* **没有模型下载**：MSST 用自己的 DownloadManager，工作台只使用目录里已有的模型；
* **没有 Runtime 安装与校验**：环境是用户的，工作台只读探测、绝不修改（§4.4）。

因此这里也不使用 PyMSS 的用户模型注册表——模型路径与配置路径直接交给
``MSSeparator``，绑定信息存在设置里即可。
"""

from __future__ import annotations

import copy
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

from PyQt6.QtCore import QObject, QTimer

from .audio_io import _unique_output_path
from .backend import (
    ResultFile,
    SeparationBackend,
    SeparationSnapshot,
    TaskProgress,
    TaskResult,
)
from .msst import scan_msst_models
from .msst_env import check_environment, locate_root, probe_runtime
from .msst_service import MsstWorker
from .states import (
    STAGE_LOAD,
    STAGE_PREPARE,
    STAGE_SAVE,
    STAGE_SEPARATE,
    ServiceState,
    TaskDependency,
    TaskSpec,
    TaskType,
)
from .states import TASK_SPECS
from .stems import choose_stem_for_task, parse_model_stems

#: 设置里 MSST 模式使用的键。
MSST_ROOT_KEY = "msst_root"
MSST_BINDINGS_KEY = "msst_bindings"


class MsstSeparationBackend(SeparationBackend):
    """驱动用户已有 MSST 环境的后端。"""

    def __init__(self, settings_ns: dict, save_settings=None, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._settings = settings_ns if settings_ns is not None else {}
        self._save_settings = save_settings
        self._lock = threading.RLock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="krok-msst")
        self._futures: set[Future] = set()
        self._worker: MsstWorker | None = None
        self._shutdown_requested = False
        self._task_cancel = threading.Event()
        self._task_context: dict[str, str] = {}
        self._task_queue: list[TaskType] = []
        self._queue_active = False
        self._scan_candidates: dict[str, object] = {}
        self.recent_logs: list[str] = []

        self._snap = SeparationSnapshot()
        root = str(self._settings.get(MSST_ROOT_KEY, "")).strip()
        if root and locate_root(root) is not None:
            self._snap.install_dir = str(locate_root(root))
            self._snap.state = ServiceState.INSTALLED_STOPPED
        self._rebuild_dependencies()

    # ── 基础 ─────────────────────────────────────────────────────
    def snapshot(self) -> SeparationSnapshot:
        with self._lock:
            return copy.deepcopy(self._snap)

    def log_directory(self) -> str:
        return str(self._work_root())

    def _work_root(self) -> Path:
        """工作台自己的 MSST 工作目录（桥接脚本与日志都放这里，不碰用户目录）。"""
        from krok_helper.settings import get_settings_path

        try:
            base = Path(get_settings_path()).parent
        except Exception:
            base = Path.home() / ".karaoke-studio"
        return base / "msst"

    def _emit(self) -> None:
        if not self._shutdown_requested:
            self.snapshotChanged.emit(self.snapshot())

    def _set_state(self, state: ServiceState, *, error: str = "") -> None:
        with self._lock:
            self._snap.state = state
            self._snap.error = error
        self._emit()

    def _log(self, text: str) -> None:
        self.recent_logs.append(time.strftime("%H:%M:%S ") + text)
        del self.recent_logs[:-200]
        if not self._shutdown_requested:
            self.logAppended.emit(text)

    def _persist(self) -> None:
        if self._save_settings is not None:
            try:
                self._save_settings()
            except Exception:
                pass

    def _submit(self, operation, on_success=None, on_error=None) -> Future | None:
        if self._shutdown_requested:
            return None
        future = self._executor.submit(operation)
        with self._lock:
            self._futures.add(future)

        def finished(done: Future) -> None:
            with self._lock:
                self._futures.discard(done)
            if self._shutdown_requested:
                return
            try:
                result = done.result()
            except Exception as exc:
                if on_error is not None:
                    on_error(exc)
                else:
                    self._fail(exc)
            else:
                if on_success is not None:
                    on_success(result)

        future.add_done_callback(finished)
        return future

    def _fail(self, error) -> None:
        message = str(error).strip() or type(error).__name__
        self._set_state(ServiceState.ERROR, error=message)
        self._log(message)

    # ── 绑定与依赖 ───────────────────────────────────────────────
    def _bindings(self) -> dict[TaskType, dict]:
        raw = self._settings.get(MSST_BINDINGS_KEY)
        if not isinstance(raw, dict):
            return {}
        result: dict[TaskType, dict] = {}
        for key, value in raw.items():
            if not isinstance(value, dict):
                continue
            try:
                task = TaskType(str(key))
            except ValueError:
                continue
            if str(value.get("model_path", "")).strip():
                result[task] = value
        return result

    def _rebuild_dependencies(self) -> None:
        service_on = self._worker is not None and self._worker.running
        bindings = self._bindings()
        dependencies: dict[TaskType, TaskDependency] = {}
        for task in TaskType:
            binding = bindings.get(task)
            if binding is None:
                dependencies[task] = TaskDependency(
                    task,
                    False,
                    "未指定模型",
                    "请在设置的「模型与输出轨」里为该任务选择 MSST 模型。",
                )
                continue
            weight = Path(str(binding.get("model_path", "")))
            config = Path(str(binding.get("config_path", ""))) if binding.get("config_path") else None
            if not weight.is_file():
                dependencies[task] = TaskDependency(
                    task, False, "模型文件缺失", f"找不到模型文件：{weight}"
                )
                continue
            if config is not None and not config.is_file():
                dependencies[task] = TaskDependency(
                    task, False, "配置缺失", f"找不到模型配置：{config}"
                )
                continue
            if not str(binding.get("stem", "")).strip():
                dependencies[task] = TaskDependency(
                    task, False, "输出轨未确定", "无法确定该模型上要保存哪条输出轨，请另选模型。"
                )
                continue
            dependencies[task] = TaskDependency(
                task,
                service_on,
                "MSST 模型" if service_on else "已就绪",
                "" if service_on else "需要先启动 MSST 环境",
                is_external=True,
            )
        with self._lock:
            self._snap.dependencies = dependencies

    # ── 环境配置 ─────────────────────────────────────────────────
    def start_existing_check(self, *, executable: str = "", server_url: str = "", api_key: str = "") -> None:
        root = str(executable or server_url or "").strip()
        self.existingCheckStarted.emit()

        def operation() -> list[tuple[str, bool, str]]:
            checks = check_environment(root)
            if all(ok for _n, ok, _d in checks):
                ok, detail = probe_runtime(root)
                checks.append(("推理环境探测", ok, detail))
            return checks

        self._submit(
            operation,
            lambda checks: self.existingCheckFinished.emit(checks),
            lambda exc: self.existingCheckFailed.emit(str(exc)),
        )

    def connect_existing(self, *, executable: str = "", server_url: str = "", api_key: str = "") -> None:
        root = locate_root(str(executable or server_url or "").strip())
        if root is None:
            self._fail("所选目录不是可用的 MSST 安装。")
            return
        self._settings[MSST_ROOT_KEY] = str(root)
        self._settings["mode"] = "msst"
        with self._lock:
            self._snap.install_dir = str(root)
        self._persist()
        self._auto_bind_from_root(root)
        self._set_state(ServiceState.INSTALLED_STOPPED)
        self._log(f"已连接 MSST 环境：{root}")

    def _auto_bind_from_root(self, root: Path) -> None:
        """首次连接时按推荐模型自动映射三个任务，用户可在设置里改。"""
        try:
            candidates = scan_msst_models(root)
        except Exception as exc:
            self._log(f"扫描 MSST 模型失败：{exc}")
            return
        self._scan_candidates = {c.candidate_id: c for c in candidates}
        from .folder_import import match_tasks
        from .presets import TASK_PRESETS

        preferred = {task: TASK_PRESETS[task].steps[-1].model for task in TaskType}
        matched = match_tasks(candidates, preferred)
        bindings = dict(self._settings.get(MSST_BINDINGS_KEY) or {})
        for task, candidate in matched.items():
            if task.value in bindings:
                continue  # 不覆盖用户已有选择
            bindings[task.value] = self._binding_from_candidate(task, candidate)
        self._settings[MSST_BINDINGS_KEY] = bindings
        self._persist()
        with self._lock:
            self._rebuild_dependencies()
        self._emit()

    def _binding_from_candidate(self, task: TaskType, candidate) -> dict:
        stems = tuple(part for part in str(candidate.target_stem or "").split("/") if part)
        if not stems and candidate.config_path:
            try:
                stems = parse_model_stems(Path(candidate.config_path).read_text(encoding="utf-8-sig"))
            except OSError:
                stems = ()
        return {
            "name": candidate.display_name,
            "model_type": candidate.model_type,
            "model_path": candidate.model_path,
            "config_path": candidate.config_path,
            "stem": choose_stem_for_task(task.value, stems),
        }

    def bind_external_model(self, task: TaskType, candidate_id: str) -> None:
        candidate = self._scan_candidates.get(candidate_id)
        if candidate is None:
            raise ValueError("所选模型不是当前任务的有效候选。")
        bindings = dict(self._settings.get(MSST_BINDINGS_KEY) or {})
        bindings[task.value] = self._binding_from_candidate(task, candidate)
        self._settings[MSST_BINDINGS_KEY] = bindings
        self._persist()
        with self._lock:
            self._rebuild_dependencies()
        self._emit()
        self._log(f"{TASK_SPECS[task].title}改用 MSST 模型 {candidate.display_name}。")

    def unbind_external_model(self, task: TaskType) -> None:
        bindings = dict(self._settings.get(MSST_BINDINGS_KEY) or {})
        if bindings.pop(task.value, None) is not None:
            self._settings[MSST_BINDINGS_KEY] = bindings
            self._persist()
            with self._lock:
                self._rebuild_dependencies()
            self._emit()

    def bound_external_candidate(self, task: TaskType) -> str:
        return str(self._bindings().get(task, {}).get("name", ""))

    def suggested_msst_root(self) -> str:
        return str(self._settings.get(MSST_ROOT_KEY, ""))

    def start_msst_scan(self, root: str) -> None:
        self.msstScanStarted.emit()

        def operation():
            return scan_msst_models(root)

        def success(candidates) -> None:
            self._scan_candidates = {c.candidate_id: c for c in candidates}
            self.msstScanFinished.emit(candidates)

        self._submit(operation, success, lambda exc: self.msstScanFailed.emit(str(exc)))

    def start_folder_scan(self, folder: str) -> None:
        self.folderScanStarted.emit()
        from .folder_import import match_tasks, scan_folder
        from .presets import TASK_PRESETS

        preferred = {task: TASK_PRESETS[task].steps[-1].model for task in TaskType}

        def operation():
            candidates = scan_folder(folder)
            return candidates, match_tasks(candidates, preferred)

        def success(result) -> None:
            candidates, matched = result
            self._scan_candidates.update({c.candidate_id: c for c in candidates})
            self.folderScanFinished.emit(
                candidates, {task: c.candidate_id for task, c in matched.items()}
            )

        self._submit(operation, success, lambda exc: self.folderScanFailed.emit(str(exc)))

    # ── 环境生命周期 ─────────────────────────────────────────────
    def start_service(self) -> None:
        if self._worker is not None and self._worker.running:
            return
        root = str(self._snap.install_dir or "").strip()
        if not root:
            self._fail("尚未选择 MSST 安装目录。")
            return
        self._set_state(ServiceState.SERVICE_STARTING)
        work = self._work_root()

        def operation() -> MsstWorker:
            return MsstWorker.start(root, work)

        def success(worker: MsstWorker) -> None:
            self._worker = worker
            with self._lock:
                self._snap.device = "由 MSST 自行选择"
                self._rebuild_dependencies()
            self._set_state(ServiceState.SERVICE_READY)
            self._log("MSST 推理环境已就绪。")

        self._submit(operation, success, self._fail)

    def stop_service(self) -> None:
        worker, self._worker = self._worker, None
        if worker is not None:
            self._set_state(ServiceState.SERVICE_STOPPING)
            self._submit(lambda: worker.stop(5.0), lambda _ok: self._after_stop())
        else:
            self._after_stop()

    def _after_stop(self) -> None:
        with self._lock:
            self._rebuild_dependencies()
        self._set_state(ServiceState.INSTALLED_STOPPED)
        self._log("MSST 推理环境已停止。")

    def cancel_start(self) -> None:
        self.stop_service()

    def refresh(self, *, full: bool = False) -> None:
        with self._lock:
            self._rebuild_dependencies()
            if self._snap.state is ServiceState.ERROR:
                self._snap.state = (
                    ServiceState.SERVICE_READY
                    if self._worker is not None and self._worker.running
                    else ServiceState.INSTALLED_STOPPED
                )
                self._snap.error = ""
        self._emit()

    # ── 任务执行 ─────────────────────────────────────────────────
    def request_tasks(self, tasks, *, input_path: str, output_dir: str, output_format: str) -> None:
        ordered = [task for task in TaskType if task in set(tasks)]
        if not ordered:
            return
        self._queue_active = True
        self._task_queue = ordered[1:]
        with self._lock:
            self._snap.queue_total = len(ordered)
            self._snap.queue_done = 0
            self._snap.queued_tasks = tuple(self._task_queue)
        self.request_task(
            ordered[0], input_path=input_path, output_dir=output_dir, output_format=output_format
        )

    def request_task(self, task: TaskType, *, input_path: str, output_dir: str, output_format: str) -> None:
        if not self._queue_active:
            self._task_queue = []
            with self._lock:
                self._snap.queue_total = 1
                self._snap.queue_done = 0
                self._snap.queued_tasks = ()
        if not Path(input_path).is_file():
            self._fail("所选音频素材不存在。")
            return
        if output_format not in {"wav", "flac"}:
            self._fail("当前只支持 WAV 或 FLAC 输出。")
            return
        worker = self._worker
        if worker is None or not worker.running:
            self._fail("MSST 推理环境尚未启动。")
            return
        binding = self._bindings().get(task)
        dependency = self._snap.dependencies.get(task)
        if binding is None or dependency is None or not dependency.ready:
            self._report_failure(task, dependency.reason if dependency else "该任务未配置模型。")
            return

        self._task_cancel = threading.Event()
        self._task_context = {
            "input_path": input_path,
            "output_dir": output_dir or str(Path(input_path).parent),
            "output_format": output_format,
        }
        with self._lock:
            self._snap.pending_task = task
            self._snap.current_model = str(binding.get("name", ""))
        self._progress = TaskProgress(title=TASK_SPECS[task].title, stage_index=STAGE_PREPARE)
        self._set_state(ServiceState.MODEL_LOADING)
        self.taskProgressChanged.emit(copy.deepcopy(self._progress))

        spec: TaskSpec = TASK_SPECS[task]
        target_dir = Path(self._task_context["output_dir"])
        source = Path(input_path)

        def stage(name: str) -> None:
            if name == "load":
                self._progress.stage_index = STAGE_SEPARATE
                self._set_state(ServiceState.PROCESSING)
                self.taskProgressChanged.emit(copy.deepcopy(self._progress))

        def progress(done: float, total: float) -> None:
            """MSST 的分块进度（秒）。与 PyMSS 同单位，面板直接给确定进度条。"""
            if total <= 0:
                return
            self._progress.show_processing = True
            self._progress.processing_done = done
            self._progress.processing_total = total
            self.taskProgressChanged.emit(copy.deepcopy(self._progress))

        def operation() -> list[ResultFile]:
            staging = target_dir / ".krok-msst-staging"
            produced = worker.separate(
                {
                    "id": int(time.time() * 1000) % 1_000_000,
                    "model_type": binding["model_type"],
                    "model_path": binding["model_path"],
                    "config_path": binding.get("config_path", ""),
                    "stem": binding["stem"],
                    "input": str(source),
                    "output_dir": str(staging),
                    "format": output_format,
                    "device": "auto",
                },
                on_stage=stage,
                on_progress=progress,
            )
            self._progress.show_processing = False
            self._progress.stage_index = STAGE_SAVE
            self.taskProgressChanged.emit(copy.deepcopy(self._progress))
            label = spec.output_labels[-1]
            final = _unique_output_path(target_dir, f"{source.stem}_{label}{Path(produced).suffix}")
            target_dir.mkdir(parents=True, exist_ok=True)
            Path(produced).replace(final)
            try:
                staging.rmdir()
            except OSError:
                pass
            return [ResultFile(path=str(final), label=label, size_bytes=final.stat().st_size)]

        def success(files: list[ResultFile]) -> None:
            with self._lock:
                self._snap.pending_task = None
            self.resultReady.emit(
                TaskResult(
                    task=task,
                    title=spec.title,
                    finished_at=time.strftime("%H:%M:%S"),
                    files=files,
                )
            )
            self._log(f"{spec.title}完成。")
            if not self._advance_queue():
                self._set_state(ServiceState.SERVICE_READY)

        def failure(exc: Exception) -> None:
            if self._task_cancel.is_set():
                self._log("分离已取消。")
                return
            self._report_failure(task, str(exc).strip() or type(exc).__name__)

        self._submit(operation, success, failure)

    def _report_failure(self, task: TaskType, reason: str) -> None:
        with self._lock:
            self._snap.pending_task = None
        self.resultReady.emit(
            TaskResult(
                task=task,
                title=TASK_SPECS[task].title,
                finished_at=time.strftime("%H:%M:%S"),
                files=[],
                error=reason,
            )
        )
        self._log(f"{TASK_SPECS[task].title}失败：{reason}")
        if not self._advance_queue():
            self._set_state(ServiceState.SERVICE_READY)

    def _advance_queue(self) -> bool:
        with self._lock:
            self._snap.queue_done += 1
            remaining = list(self._task_queue)
        if not remaining or self._task_cancel.is_set():
            self._task_queue = []
            self._queue_active = False
            with self._lock:
                self._snap.queued_tasks = ()
            return False
        nxt, self._task_queue = remaining[0], remaining[1:]
        with self._lock:
            self._snap.queued_tasks = tuple(self._task_queue)
        context = dict(self._task_context)
        self.request_task(
            nxt,
            input_path=context.get("input_path", ""),
            output_dir=context.get("output_dir", ""),
            output_format=context.get("output_format", "wav"),
        )
        return True

    def cancel_task(self) -> None:
        self._task_cancel.set()
        with self._lock:
            self._snap.pending_task = None
            self._task_queue = []
            self._snap.queued_tasks = ()
        self._queue_active = False
        # 桥接进程正在跑一次不可中断的推理，只能整体重启来真正释放显存。
        self.stop_service()
        self._log("已停止任务；MSST 推理环境将重新启动。")

    # ── 该模式下不适用的能力 ─────────────────────────────────────
    def start_wizard(self, flow: str) -> None:
        return

    def confirm_install_location(self, path: str) -> None:
        return

    def prepare_install_choice(self) -> tuple[str, int]:
        return ("msst", 0)

    def start_install(self) -> None:
        self._fail("MSST 模式使用你已有的环境，不需要安装 Runtime。")

    def cancel_install(self) -> None:
        return

    def cancel_msst_scan(self) -> None:
        return

    def request_catalog_models(self) -> None:
        self.catalogModelsFailed.emit(
            "MSST 模式不提供在线模型目录；请用「一键导入模型文件夹」从本地选择。"
        )

    def request_model_stems(self, model: str) -> None:
        self.modelStemsFailed.emit(model, "MSST 模式请通过导入本地模型来选择输出轨。")

    def set_task_model(self, task: TaskType, model: str, stem: str, size_bytes: int) -> None:
        return

    def import_local_model(self, task: TaskType, *, weight_path: str, config_path: str, model_type: str, display_name: str = "") -> None:
        from .local_import import build_local_candidate

        def operation():
            return build_local_candidate(
                weight_path=weight_path,
                config_path=config_path or None,
                model_type=model_type,
                task=task,
                display_name=display_name,
            )

        def success(candidate) -> None:
            if not candidate.bindable:
                self.localImportFailed.emit(f"{candidate.status}：{candidate.detail}".strip("："))
                return
            self._scan_candidates[candidate.candidate_id] = candidate
            self.bind_external_model(task, candidate.candidate_id)
            self.localImportFinished.emit(candidate)

        self._submit(operation, success, lambda exc: self.localImportFailed.emit(str(exc)))

    def finish_external_mapping(self) -> None:
        # MSST 模式没有需要重载的进程内清单：模型路径每次任务直接传给桥接进程。
        self._persist()
        with self._lock:
            self._rebuild_dependencies()
        self._emit()

    def start_model_download(self) -> None:
        self._fail("MSST 模式不下载模型；请用 MSST 自带的下载器获取后再导入。")

    def repair_install(self) -> None:
        self.refresh(full=True)

    def reinstall(self) -> None:
        self.refresh(full=True)

    def relocate_install(self, path: str) -> None:
        self.connect_existing(executable=path)

    def remove_configuration(self) -> None:
        for key in (MSST_ROOT_KEY, MSST_BINDINGS_KEY, "mode"):
            self._settings.pop(key, None)
        self._persist()
        with self._lock:
            self._snap = SeparationSnapshot()
            self._rebuild_dependencies()
        self._set_state(ServiceState.UNCONFIGURED)
        self._log("已移除 MSST 配置（未修改你的 MSST 目录）。")

    def cleanup_incomplete(self) -> None:
        return

    def shutdown(self, timeout_ms: int = 5000) -> bool:
        self._shutdown_requested = True
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.stop(max(0.5, timeout_ms / 1000.0))
        self._executor.shutdown(wait=False, cancel_futures=True)
        return True


__all__ = ["MSST_BINDINGS_KEY", "MSST_ROOT_KEY", "MsstSeparationBackend"]
