"""音频分离后端适配层 —— 接口定义 + 测试用模拟实现。

GUI 只依赖本模块的接口，不直接依赖 PyMSS 的 HTTP 字段或模块对象
（需求文档 §1.5）。产品页面默认使用 ``RealSeparationBackend``；本模块保留
``MockSeparationBackend``，仅用于 UI 状态机的确定性测试。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from krok_helper.audio_processing.separation.states import (
    STAGE_DOWNLOAD,
    STAGE_ENCODE,
    STAGE_LOAD,
    STAGE_PREPARE,
    STAGE_SAVE,
    STAGE_SEPARATE,
    ServiceState,
    TaskDependency,
    TaskType,
    format_size,
)

#: 首次配置向导的三个入口（需求文档 §3.5）。
FLOW_FULL = "full"            # 安装 PyMSS 和推荐模型
FLOW_REUSE_MSST = "reuse_msst"  # 仅安装 PyMSS，复用 MSST 模型
FLOW_EXISTING = "existing"    # 使用已有 PyMSS
FLOW_REMAP_MSST = "remap_msst"  # 已安装后重新扫描/映射 MSST 模型
FLOW_UPGRADE = "upgrade"      # 保留目录和模型，升级托管 Runtime


@dataclass
class SeparationSnapshot:
    """后端当前状态的整体快照（状态条与任务卡据此渲染）。"""

    state: ServiceState = ServiceState.UNCONFIGURED
    install_dir: str = ""
    pymss_version: str = ""
    device: str = ""
    current_model: str = ""
    error: str = ""
    download_done: int = 0
    download_total: int = 0
    pending_task: TaskType | None = None
    dependencies: dict[TaskType, TaskDependency] = field(default_factory=dict)


@dataclass
class TaskProgress:
    """当前任务进度（只展示真实阶段，不伪造百分比——需求文档 §9.3）。"""

    title: str = ""
    stage_index: int = 0
    stage_name: str = ""
    current_file: str = ""
    download_done: int = 0
    download_total: int = 0
    show_download: bool = False
    processing_done: int = 0
    processing_total: int = 0
    show_processing: bool = False

    @property
    def is_download_stage(self) -> bool:
        return self.show_download and self.download_total > 0

    @property
    def is_processing_stage(self) -> bool:
        return self.show_processing and self.processing_total > 0


@dataclass
class ResultFile:
    path: str
    label: str
    size_bytes: int = 0


@dataclass
class TaskResult:
    task: TaskType
    title: str
    finished_at: str
    files: list[ResultFile] = field(default_factory=list)


@dataclass(frozen=True)
class CatalogModel:
    """设置里可供选择的一个 catalog 模型。"""

    name: str
    category: str = ""
    architecture: str = ""
    size_bytes: int = 0
    downloaded: bool = False


@dataclass(frozen=True)
class ExternalModelCandidate:
    """One model discovered in an existing MSST installation.

    ``candidate_id`` is a stable backend identifier and must be used for
    binding.  ``display_name`` is presentation-only and may change without
    invalidating saved mappings.  Only candidates with ``bindable=True`` may
    be assigned to a fixed Karaoke Studio task.
    """

    candidate_id: str
    display_name: str
    task: TaskType
    status: str
    architecture: str = ""
    model_path: str = ""
    config_path: str = ""
    detail: str = ""
    bindable: bool = False
    model_type: str = ""
    target_stem: str = ""
    size_bytes: int = 0
    mtime_ns: int = 0
    sha256: str = ""
    config_size_bytes: int = 0
    config_mtime_ns: int = 0
    config_sha256: str = ""


class SeparationBackend(QObject):
    """后端适配层接口。信号驱动 UI；方法由 UI 调用。

    真实实现需要覆盖全部方法；``snapshot()`` 必须始终返回最新状态副本。
    """

    snapshotChanged = pyqtSignal(object)        # SeparationSnapshot
    taskProgressChanged = pyqtSignal(object)    # TaskProgress
    resultReady = pyqtSignal(object)            # TaskResult
    logAppended = pyqtSignal(str)
    msstScanStarted = pyqtSignal()
    msstScanFinished = pyqtSignal(object)        # list[ExternalModelCandidate]
    msstScanFailed = pyqtSignal(str)
    existingCheckStarted = pyqtSignal()
    existingCheckFinished = pyqtSignal(object)  # list[tuple[str, bool, str]]
    existingCheckFailed = pyqtSignal(str)
    #: 设置里挑模型用：catalog 列表与「某模型的真实输出轨」都要联网/读盘，
    #: 一律异步，GUI 只连信号（§1.5：界面不直接依赖 HTTP 字段）。
    catalogModelsFinished = pyqtSignal(object)   # list[CatalogModel]
    catalogModelsFailed = pyqtSignal(str)
    modelStemsFinished = pyqtSignal(str, object)  # model, tuple[str, ...]
    modelStemsFailed = pyqtSignal(str, str)       # model, 中文原因
    localImportFinished = pyqtSignal(object)      # ExternalModelCandidate
    localImportFailed = pyqtSignal(str)           # 中文原因
    folderScanStarted = pyqtSignal()
    folderScanFinished = pyqtSignal(object, object)  # list[候选], dict[TaskType, candidate_id]
    folderScanFailed = pyqtSignal(str)

    def snapshot(self) -> SeparationSnapshot:
        raise NotImplementedError

    def log_directory(self) -> str:
        raise NotImplementedError

    # ── 首次配置向导 ──────────────────────────────────────────────
    def start_wizard(self, flow: str) -> None:
        raise NotImplementedError

    def confirm_install_location(self, path: str) -> None:
        raise NotImplementedError

    def prepare_install_choice(self) -> tuple[str, int]:
        """Select the device runtime before the user confirms a large download."""
        raise NotImplementedError

    def start_install(self) -> None:
        raise NotImplementedError

    def cancel_install(self) -> None:
        raise NotImplementedError

    def start_msst_scan(self, root: str) -> None:
        """Asynchronously scan an MSST root and emit a completion signal."""
        raise NotImplementedError

    def cancel_msst_scan(self) -> None:
        """Cancel an in-progress read-only MSST scan."""
        raise NotImplementedError

    def bind_external_model(self, task: TaskType, candidate_id: str) -> None:
        raise NotImplementedError

    def unbind_external_model(self, task: TaskType) -> None:
        """Remove one app-owned reference without touching the source files."""
        raise NotImplementedError

    # ── 设置：按任务自选模型与输出轨 ──────────────────────────────
    def request_catalog_models(self) -> None:
        """异步列出 catalog 中受支持的模型，完成后发 catalogModelsFinished。"""
        raise NotImplementedError

    def request_model_stems(self, model: str) -> None:
        """异步取某模型真实声明的输出轨名，完成后发 modelStemsFinished。

        必须来自模型自己的配置（``training.instruments``），不能用 catalog 的
        ``target_stem``——后者与实际不符，会让用户选到不存在的轨。
        """
        raise NotImplementedError

    def set_task_model(self, task: TaskType, model: str, stem: str, size_bytes: int) -> None:
        """覆盖某任务使用的模型与输出轨；``model`` 为空表示恢复推荐预设。"""
        raise NotImplementedError

    def import_local_model(
        self,
        task: TaskType,
        *,
        weight_path: str,
        config_path: str,
        model_type: str,
        display_name: str = "",
    ) -> None:
        """把任意目录下的一份权重导入并绑定给某任务（异步）。

        原文件保持原地：只在工作台自己的用户模型清单里登记引用，不复制、不移动、
        不修改（与 MSST 映射同一条约束，§4.5）。
        """
        raise NotImplementedError

    def start_folder_scan(self, folder: str) -> None:
        """一键导入：扫描一个文件夹并为三个任务各匹配一个模型（异步）。

        完成后发 folderScanFinished(候选列表, 建议映射)；候选会进入后端缓存，
        界面确认后按 candidate_id 调 :meth:`bind_external_model` 落定。
        """
        raise NotImplementedError

    def finish_external_mapping(self) -> None:
        """Apply a completed mapping batch and reload the owned service if needed."""
        raise NotImplementedError

    def suggested_msst_root(self) -> str:
        """Return the last successfully scanned MSST root, if any."""
        raise NotImplementedError

    def bound_external_candidate(self, task: TaskType) -> str:
        """Return the stable candidate id currently assigned to a task."""
        raise NotImplementedError

    def start_existing_check(
        self, *, executable: str = "", server_url: str = "", api_key: str = ""
    ) -> None:
        """Asynchronously probe an existing environment or server."""
        raise NotImplementedError

    def connect_existing(
        self, *, executable: str = "", server_url: str = "", api_key: str = ""
    ) -> None:
        raise NotImplementedError

    # ── 服务生命周期 ─────────────────────────────────────────────
    def start_service(self) -> None:
        raise NotImplementedError

    def stop_service(self) -> None:
        raise NotImplementedError

    def cancel_start(self) -> None:
        raise NotImplementedError

    def refresh(self, *, full: bool = False) -> None:
        """重新检测安装/服务/模型状态；完整哈希仅在明确请求时执行。"""
        raise NotImplementedError

    # ── 任务 ────────────────────────────────────────────────────
    def request_task(self, task: TaskType, *, input_path: str, output_dir: str, output_format: str) -> None:
        """任务卡主操作：模型缺失时先下载再继续（需求文档 §8.4）。"""
        raise NotImplementedError

    def start_model_download(self) -> None:
        """Download dependencies for the pending task, then continue it."""
        raise NotImplementedError

    def cancel_task(self) -> None:
        raise NotImplementedError

    # ── 修复与重置 ───────────────────────────────────────────────
    def repair_install(self) -> None:
        raise NotImplementedError

    def reinstall(self) -> None:
        raise NotImplementedError

    def relocate_install(self, path: str) -> None:
        raise NotImplementedError

    def remove_configuration(self) -> None:
        """只清除工作台中的路径与状态，不删除用户文件。"""
        raise NotImplementedError

    def cleanup_incomplete(self) -> None:
        """向导取消钩子：清理半成品，回到未配置。"""
        raise NotImplementedError

    def shutdown(self, timeout_ms: int = 5000) -> bool:
        """Stop owned resources before host exit and report completion."""
        raise NotImplementedError


class MockSeparationBackend(SeparationBackend):
    """模拟后端：用定时器走完整个状态机，供 UI 框架与测试使用。

    ``simulate_delays=False`` 时所有迁移同步完成（测试用）。
    """

    MOCK_VERSION = "2.0.18"
    MOCK_RUNTIME_DOWNLOAD_BYTES = 820 * 1024**2   # 约 820 MB，仅占位
    MOCK_MODEL_SIZES = {
        TaskType.VOCAL: int(1.48 * 1024**3),
        TaskType.INSTRUMENTAL: int(0.91 * 1024**3),
        TaskType.HARMONY: int(1.72 * 1024**3),
    }

    def __init__(
        self,
        settings_ns: dict | None = None,
        *,
        simulate_delays: bool = True,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        from collections import deque

        self._settings = settings_ns if settings_ns is not None else {}
        self._simulate_delays = simulate_delays
        self.recent_logs: deque[str] = deque(maxlen=200)
        self._snap = SeparationSnapshot()
        self._progress = TaskProgress()
        self._downloaded_models: set[TaskType] = set()
        self._external_bindings: dict[TaskType, str] = {}
        self._task_started_at = 0.0
        self._task_context: dict[str, str] = {}
        self._timer = QTimer(self)
        self._timer.setInterval(90)
        self._timer.timeout.connect(self._on_tick)
        self._phase = ""
        # 已保存过安装目录 → 视为已安装未启动（模拟「重启后保留配置」）。
        if str(self._settings.get("install_dir", "")).strip():
            self._snap.state = ServiceState.INSTALLED_STOPPED
            self._snap.install_dir = str(self._settings["install_dir"])
            self._snap.pymss_version = self.MOCK_VERSION
        self._load_downloaded_models()
        self._rebuild_dependencies()

    # ── 内部工具 ──────────────────────────────────────────────────
    def _emit(self) -> None:
        self.snapshotChanged.emit(self.snapshot())

    def snapshot(self) -> SeparationSnapshot:
        snap = SeparationSnapshot(
            state=self._snap.state,
            install_dir=self._snap.install_dir,
            pymss_version=self._snap.pymss_version,
            device=self._snap.device,
            current_model=self._snap.current_model,
            error=self._snap.error,
            download_done=self._snap.download_done,
            download_total=self._snap.download_total,
            pending_task=self._snap.pending_task,
            dependencies=dict(self._snap.dependencies),
        )
        return snap

    def log_directory(self) -> str:
        return str(Path(self._snap.install_dir) / "logs") if self._snap.install_dir else ""

    def _set_state(self, state: ServiceState, *, error: str = "") -> None:
        self._snap.state = state
        self._snap.error = error
        self._emit()

    def _delay(self, ms: int, slot) -> None:
        if not self._simulate_delays:
            slot()
            return
        # PyQt6 没有 singleShot(msec, context, slot) 这个重载；用挂在 self 下的
        # 一次性 QTimer，既能延时又能随后端一起析构，不会在对象销毁后回调。
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(slot)
        timer.timeout.connect(timer.deleteLater)
        timer.start(ms)

    def _log(self, text: str) -> None:
        self.recent_logs.append(time.strftime("%H:%M:%S ") + text)
        self.logAppended.emit(text)

    def _rebuild_dependencies(self) -> None:
        service_on = self._snap.state not in {
            ServiceState.UNCONFIGURED,
            ServiceState.LOCATION_REQUIRED,
        }
        deps: dict[TaskType, TaskDependency] = {}
        for task in TaskType:
            size = self.MOCK_MODEL_SIZES[task]
            if task in self._external_bindings:
                deps[task] = TaskDependency(
                    task, ready=service_on, badge="外部模型",
                    reason="" if service_on else "需要先启动服务",
                    is_external=True,
                )
            elif task in self._downloaded_models:
                deps[task] = TaskDependency(
                    task, ready=service_on, badge="就绪",
                    reason="" if service_on else "需要先启动服务",
                )
            else:
                deps[task] = TaskDependency(
                    task, ready=False,
                    badge=f"需下载 {format_size(size)}",
                    reason="该任务的推荐模型尚未下载" if service_on else "需要先启动服务",
                    download_bytes=size,
                )
        self._snap.dependencies = deps

    def _load_downloaded_models(self) -> None:
        """从设置恢复已下载模型集合。

        安装 Runtime 不附带任何模型（§8.4），所以这里绝不能因为「有安装目录」就
        认为模型齐全；只恢复此前真正下载过的部分。
        """
        raw = self._settings.get("downloaded_models") or []
        valid = {t.value for t in TaskType}
        self._downloaded_models = {
            TaskType(v) for v in raw if isinstance(v, str) and v in valid
        }

    def _persist_downloaded_models(self) -> None:
        self._settings["downloaded_models"] = sorted(
            t.value for t in self._downloaded_models
        )

    # ── 向导 ─────────────────────────────────────────────────────
    def start_wizard(self, flow: str) -> None:
        self._log(f"进入首次配置向导：{flow}")
        if flow in {FLOW_EXISTING, FLOW_REMAP_MSST, FLOW_UPGRADE}:
            # 外部环境无需选目录，向导自行驱动 connect_existing。
            return
        self._set_state(ServiceState.LOCATION_REQUIRED)

    def confirm_install_location(self, path: str) -> None:
        self._snap.install_dir = path
        self._log(f"确认安装目录：{path}")

    def prepare_install_choice(self) -> tuple[str, int]:
        self._settings["runtime_variant"] = "windows-cpu"
        return "CPU", self.MOCK_RUNTIME_DOWNLOAD_BYTES

    def start_install(self) -> None:
        self._snap.download_done = 0
        self._snap.download_total = self.MOCK_RUNTIME_DOWNLOAD_BYTES
        self._phase = "runtime_download"
        self._set_state(ServiceState.RUNTIME_DOWNLOADING)
        self._log("开始下载 PyMSS 托管 Runtime（模拟）")
        if self._simulate_delays:
            self._timer.start()
        else:
            self._finish_runtime_download()

    def _on_tick(self) -> None:
        if self._phase == "runtime_download":
            self._snap.download_done = min(
                self._snap.download_total, self._snap.download_done + 96 * 1024**2
            )
            self._emit()
            if self._snap.download_done >= self._snap.download_total:
                self._timer.stop()
                self._finish_runtime_download()
        elif self._phase == "model_download":
            self._snap.download_done = min(
                self._snap.download_total, self._snap.download_done + 160 * 1024**2
            )
            self._progress.stage_index = STAGE_DOWNLOAD
            self._progress.download_done = self._snap.download_done
            self._progress.download_total = self._snap.download_total
            self._emit()
            self.taskProgressChanged.emit(self._progress)
            if self._snap.download_done >= self._snap.download_total:
                self._timer.stop()
                self._start_model_loading()

    def _finish_runtime_download(self) -> None:
        self._set_state(ServiceState.RUNTIME_VERIFYING)
        self._log("校验 Runtime 文件（模拟）")
        self._delay(500, self._finish_install)

    def _finish_install(self) -> None:
        self._snap.pymss_version = self.MOCK_VERSION
        self._settings["install_dir"] = self._snap.install_dir
        self._settings["expected_pymss_version"] = self.MOCK_VERSION
        self._rebuild_dependencies()
        self._set_state(ServiceState.INSTALLED_STOPPED)
        self._log("安装完成（模拟）")

    def cancel_install(self) -> None:
        self._timer.stop()
        self._phase = ""
        self._snap.download_done = 0
        self._snap.download_total = 0
        self._set_state(ServiceState.LOCATION_REQUIRED)
        self._log("已取消下载")

    def start_msst_scan(self, root: str) -> None:
        self._log(f"扫描 MSST 目录（模拟）：{root}")
        self._settings["legacy_msst_root"] = root
        candidates = [
            ExternalModelCandidate(
                candidate_id="msst:big_beta5e",
                display_name="big_beta5e（人声候选）",
                task=TaskType.VOCAL,
                status="已验证兼容",
                architecture="MDX23C / DrumSep（模拟）",
                model_path=f"{root}/models/big_beta5e.ckpt",
                config_path=f"{root}/configs/big_beta5e.yaml",
                detail="权重、配置与输出 stem 匹配，加载冒烟成功（模拟）。",
                bindable=True,
            ),
            ExternalModelCandidate(
                candidate_id="msst:inst_v1e",
                display_name="inst_v1e（伴奏候选）",
                task=TaskType.INSTRUMENTAL,
                status="等待验证",
                architecture="MDX23C（模拟）",
                model_path=f"{root}/models/inst_v1e.ckpt",
                config_path=f"{root}/configs/inst_v1e.yaml",
                detail="已识别架构与配置，尚未在当前设备加载。",
                bindable=False,
            ),
            ExternalModelCandidate(
                candidate_id="msst:mel_band_roformer_karaoke",
                display_name="mel_band_roformer_karaoke（和声候选）",
                task=TaskType.HARMONY,
                status="配置缺失",
                architecture="Mel-Band Roformer（模拟）",
                model_path=f"{root}/models/mel_band_roformer_karaoke.ckpt",
                detail="缺少执行所需配置，可精确匹配后只下载小型配置。",
                bindable=False,
            ),
        ]
        self.msstScanStarted.emit()
        self._delay(250, lambda: self.msstScanFinished.emit(candidates))

    def cancel_msst_scan(self) -> None:
        self._log("已取消 MSST 模型扫描（模拟）")

    def bind_external_model(self, task: TaskType, candidate_id: str) -> None:
        self._external_bindings[task] = candidate_id
        self._log(f"映射外部模型：{task.value} → {candidate_id}")
        self._rebuild_dependencies()
        self._emit()

    def unbind_external_model(self, task: TaskType) -> None:
        self._external_bindings.pop(task, None)
        raw = self._settings.get("external_bindings")
        if isinstance(raw, dict):
            raw.pop(task.value, None)
        self._rebuild_dependencies()
        self._emit()

    #: 模拟 catalog：覆盖真实世界里四种不同的 stem 命名，供 UI 测试。
    MOCK_CATALOG: tuple[tuple[str, str, tuple[str, ...], int], ...] = (
        ("inst_v1e", "vocal/vocal_instrumental_dual", ("other", "vocals"), 913_102_724),
        ("inst_v1e_plus", "vocal/vocal_instrumental_dual", ("other", "vocals"), 913_102_724),
        (
            "model_mel_band_roformer_karaoke_aufr33_viperx_sdr_10.1956",
            "karaoke",
            ("karaoke", "other"),
            913_096_801,
        ),
        ("mel_band_roformer_karaoke_becruily", "karaoke", ("Vocals", "Instrumental"), 1_719_139_254),
    )

    def request_catalog_models(self) -> None:
        from krok_helper.audio_processing.separation.backend import CatalogModel

        models = [
            CatalogModel(
                name=name,
                category=category,
                architecture="mel_band_roformer",
                size_bytes=size,
                downloaded=name in {step.model for step in self._preset_steps()},
            )
            for name, category, _stems, size in self.MOCK_CATALOG
        ]
        self._delay(120, lambda: self.catalogModelsFinished.emit(models))

    def request_model_stems(self, model: str) -> None:
        name = str(model or "").strip()
        entry = next((row for row in self.MOCK_CATALOG if row[0] == name), None)
        if entry is None:
            self._delay(
                80,
                lambda: self.modelStemsFailed.emit(
                    name, "无法从该模型的配置中读出输出轨，请换一个模型。"
                ),
            )
            return
        self._delay(80, lambda: self.modelStemsFinished.emit(name, entry[2]))

    def _preset_steps(self):
        from krok_helper.audio_processing.separation.presets import TASK_PRESETS

        return [step for preset in TASK_PRESETS.values() for step in preset.steps]

    def set_task_model(self, task: TaskType, model: str, stem: str, size_bytes: int) -> None:
        from krok_helper.audio_processing.separation.presets import (
            TASK_MODEL_OVERRIDES_KEY,
        )

        raw = self._settings.get(TASK_MODEL_OVERRIDES_KEY)
        overrides = dict(raw) if isinstance(raw, dict) else {}
        name, track = str(model or "").strip(), str(stem or "").strip()
        if not name or not track:
            overrides.pop(task.value, None)
        else:
            overrides[task.value] = {
                "model": name,
                "stem": track,
                "size_bytes": max(0, int(size_bytes or 0)),
            }
        self._settings[TASK_MODEL_OVERRIDES_KEY] = overrides
        self._rebuild_dependencies()
        self._emit()

    def start_folder_scan(self, folder: str) -> None:
        from krok_helper.audio_processing.separation.folder_import import (
            match_tasks,
            scan_folder,
        )
        from krok_helper.audio_processing.separation.presets import TASK_PRESETS

        self.folderScanStarted.emit()

        def run() -> None:
            try:
                candidates = scan_folder(folder, install_dir=self._snap.install_dir)
            except Exception as exc:
                self.folderScanFailed.emit(str(exc).strip() or type(exc).__name__)
                return
            preferred = {t: TASK_PRESETS[t].steps[-1].model for t in TaskType}
            matched = match_tasks(candidates, preferred)
            self.folderScanFinished.emit(
                candidates, {t: c.candidate_id for t, c in matched.items()}
            )

        self._delay(80, run)

    def import_local_model(
        self,
        task: TaskType,
        *,
        weight_path: str,
        config_path: str,
        model_type: str,
        display_name: str = "",
    ) -> None:
        from krok_helper.audio_processing.separation.local_import import (
            build_local_candidate,
        )

        def run() -> None:
            try:
                candidate = build_local_candidate(
                    weight_path=weight_path,
                    config_path=config_path or None,
                    model_type=model_type,
                    task=task,
                    display_name=display_name,
                )
            except Exception as exc:
                self.localImportFailed.emit(str(exc).strip() or type(exc).__name__)
                return
            if not candidate.bindable:
                self.localImportFailed.emit(
                    f"{candidate.status}：{candidate.detail}".strip("：")
                )
                return
            self._external_bindings[task] = candidate.candidate_id
            self._rebuild_dependencies()
            self._emit()
            self.localImportFinished.emit(candidate)

        self._delay(80, run)

    def finish_external_mapping(self) -> None:
        self._log("外部模型映射已刷新（模拟）")

    def suggested_msst_root(self) -> str:
        return str(self._settings.get("legacy_msst_root", ""))

    def bound_external_candidate(self, task: TaskType) -> str:
        return str(self._external_bindings.get(task, ""))

    def start_existing_check(
        self, *, executable: str = "", server_url: str = "", api_key: str = ""
    ) -> None:
        del api_key
        target = server_url or executable
        ok = bool(target.strip())
        detail = "模拟检测通过" if ok else "请先填写可执行环境或服务地址"
        results = [
            ("获取 PyMSS 版本", ok, f"{self.MOCK_VERSION}（模拟）" if ok else detail),
            ("支持 serve 命令", ok, detail),
            ("/health 协议兼容", ok, detail),
            ("模型管理与分离端点存在", ok, detail),
            ("版本在兼容范围内", ok, detail),
        ]
        self.existingCheckStarted.emit()
        self._delay(200, lambda: self.existingCheckFinished.emit(results))

    def connect_existing(
        self, *, executable: str = "", server_url: str = "", api_key: str = ""
    ) -> None:
        del api_key
        self._settings["external_executable"] = executable
        self._settings["external_server_url"] = server_url
        self._snap.pymss_version = self.MOCK_VERSION
        self._snap.device = "外部服务（模拟）"
        self._rebuild_dependencies()
        self._set_state(ServiceState.SERVICE_READY)
        self._log("已连接外部 PyMSS（模拟）")

    # ── 服务生命周期 ─────────────────────────────────────────────
    def start_service(self) -> None:
        self._set_state(ServiceState.SERVICE_STARTING)
        self._log("启动托管服务（模拟）：127.0.0.1 随机端口")
        self._delay(700, self._service_ready)

    def _service_ready(self) -> None:
        self._snap.device = "NVIDIA GPU（CUDA · 模拟）"
        self._rebuild_dependencies()
        self._set_state(ServiceState.SERVICE_READY)

    def stop_service(self) -> None:
        self._set_state(ServiceState.SERVICE_STOPPING)
        self._delay(400, self._service_stopped)

    def _service_stopped(self) -> None:
        self._snap.device = ""
        self._snap.current_model = ""
        self._rebuild_dependencies()
        self._set_state(ServiceState.INSTALLED_STOPPED)

    def cancel_start(self) -> None:
        self._rebuild_dependencies()
        self._set_state(ServiceState.INSTALLED_STOPPED)

    def refresh(self, *, full: bool = False) -> None:
        del full
        self._rebuild_dependencies()
        self._emit()

    # ── 任务 ─────────────────────────────────────────────────────
    def request_task(self, task: TaskType, *, input_path: str, output_dir: str, output_format: str) -> None:
        self._task_context = {
            "input": input_path,
            "output_dir": output_dir,
            "format": output_format,
        }
        self._snap.pending_task = task
        dep = self._snap.dependencies.get(task)
        self._progress = TaskProgress(title=f"{task.value}", stage_index=STAGE_PREPARE)
        if dep is not None and not dep.ready and dep.download_bytes > 0:
            # 模型缺失：进入按需下载流程（需求文档 §8.4）。
            self._progress.stage_index = STAGE_DOWNLOAD
            self._progress.show_download = True
            self._set_state(ServiceState.MODEL_REQUIRED)
            self.taskProgressChanged.emit(self._progress)
            return
        self._run_pipeline(task)

    def start_model_download(self) -> None:
        """确认「下载并继续」后由 UI 调用。"""
        task = self._snap.pending_task
        if task is None:
            return
        self._snap.download_done = 0
        self._snap.download_total = self.MOCK_MODEL_SIZES[task]
        self._progress.stage_index = STAGE_DOWNLOAD
        self._progress.download_done = 0
        self._progress.download_total = self._snap.download_total
        self._progress.show_download = True
        self._phase = "model_download"
        self._set_state(ServiceState.MODEL_DOWNLOADING)
        self._log(f"开始下载模型（模拟）：{format_size(self._snap.download_total)}")
        self.taskProgressChanged.emit(self._progress)
        if self._simulate_delays:
            self._timer.start()
        else:
            self._snap.download_done = self._snap.download_total
            self._start_model_loading()

    def _start_model_loading(self) -> None:
        task = self._snap.pending_task
        if task is not None:
            self._downloaded_models.add(task)
            self._persist_downloaded_models()
        self._rebuild_dependencies()
        self._progress.stage_index = STAGE_LOAD
        self._progress.show_download = False
        self._set_state(ServiceState.MODEL_LOADING)
        self._snap.current_model = f"{task.value if task else ''}-preset（模拟）"
        self.taskProgressChanged.emit(self._progress)
        self._delay(500, self._start_separation)

    def _start_separation(self) -> None:
        self._progress.stage_index = STAGE_SEPARATE
        self._set_state(ServiceState.PROCESSING)
        self.taskProgressChanged.emit(self._progress)
        self._task_started_at = time.monotonic()
        self._delay(900, self._finish_separation)

    def _finish_separation(self) -> None:
        self._progress.stage_index = STAGE_ENCODE
        self.taskProgressChanged.emit(self._progress)
        self._delay(300, self._save_results)

    def _save_results(self) -> None:
        self._progress.stage_index = STAGE_SAVE
        self.taskProgressChanged.emit(self._progress)
        self._delay(300, self._finish_task)

    def _finish_task(self) -> None:
        task = self._snap.pending_task or TaskType.VOCAL
        from krok_helper.audio_processing.separation.states import TASK_SPECS

        spec = TASK_SPECS[task]
        output_dir = self._task_context.get("output_dir") or ""
        fmt = self._task_context.get("format") or "wav"
        stem = "示例歌曲"
        files = [
            ResultFile(
                path=f"{output_dir}/{stem}_{label}.{fmt}" if output_dir else f"{stem}_{label}.{fmt}",
                label=label,
                size_bytes=38 * 1024**2,
            )
            for label in spec.output_labels
        ]
        self._snap.pending_task = None
        self._set_state(ServiceState.SERVICE_READY)
        self.resultReady.emit(
            TaskResult(
                task=task,
                title=spec.title,
                finished_at=time.strftime("%H:%M:%S"),
                files=files,
            )
        )
        self._log("任务完成（模拟）")

    def _run_pipeline(self, task: TaskType) -> None:
        self._progress.stage_index = STAGE_PREPARE
        self._set_state(ServiceState.MODEL_LOADING)
        self._progress.stage_index = STAGE_LOAD
        self._snap.current_model = f"{task.value}-preset（模拟）"
        self.taskProgressChanged.emit(self._progress)
        self._delay(400, self._start_separation)

    def cancel_task(self) -> None:
        # 托管服务 P0 停止策略：终止并重启服务（需求文档 §9.3）。
        self._timer.stop()
        self._phase = ""
        self._snap.pending_task = None
        self._snap.download_done = 0
        self._snap.download_total = 0
        self._set_state(ServiceState.SERVICE_STARTING)
        self._log("已停止任务，服务重启中（模拟）")
        self._delay(500, self._service_ready)

    # ── 修复与重置 ───────────────────────────────────────────────
    def repair_install(self) -> None:
        self._log("修复安装（模拟）：补齐缺失/损坏文件")
        self.start_install()

    def reinstall(self) -> None:
        self._log("重新完整安装（模拟）：保留校验通过的模型")
        self.start_install()

    def relocate_install(self, path: str) -> None:
        self._snap.install_dir = path
        self._settings["install_dir"] = path
        self._set_state(ServiceState.INSTALLED_STOPPED)

    def remove_configuration(self) -> None:
        self._settings.pop("install_dir", None)
        self._settings.pop("downloaded_models", None)
        self._snap = SeparationSnapshot()
        self._downloaded_models.clear()
        self._external_bindings.clear()
        self._rebuild_dependencies()
        self._set_state(ServiceState.UNCONFIGURED)
        self._log("已移除配置（未删除任何用户文件）")

    def cleanup_incomplete(self) -> None:
        self._timer.stop()
        self._phase = ""
        if self._snap.state in {
            ServiceState.LOCATION_REQUIRED,
            ServiceState.RUNTIME_DOWNLOADING,
            ServiceState.RUNTIME_VERIFYING,
        }:
            self._set_state(ServiceState.UNCONFIGURED)
        self._log("向导已取消，清理半成品（模拟）")

    def shutdown(self, timeout_ms: int = 5000) -> bool:
        self._timer.stop()
        if self._snap.state in {
            ServiceState.SERVICE_READY,
            ServiceState.SERVICE_STARTING,
            ServiceState.PROCESSING,
            ServiceState.MODEL_DOWNLOADING,
            ServiceState.MODEL_LOADING,
        }:
            self._set_state(ServiceState.INSTALLED_STOPPED)
        return True
