"""音频分离（PyMSS）一页式工作区（需求文档 §3.3 / §3.4 / §3.6）。

状态驱动：``UNCONFIGURED`` 显示首次配置欢迎页；向导状态显示向导页；其余状态
显示主工作区。工作区自上而下：状态与操作条 → 素材与输出双卡 → 三张任务卡 →
当前任务区（有任务时展开）→ 结果区（有结果时展开）。
"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QFrame, QStackedWidget, QVBoxLayout, QWidget
from qfluentwidgets import ScrollArea as FluentScrollArea

from krok_helper.audio_processing.responsive import ResponsiveGrid
from krok_helper.audio_processing.separation.backend import (
    FLOW_EXISTING,
    FLOW_FULL,
    FLOW_REMAP_MSST,
    FLOW_REUSE_MSST,
    FLOW_UPGRADE,
    SeparationBackend,
    TaskProgress,
)
from krok_helper.audio_processing.separation.settings_dialog import SeparationSettingsDialog
from krok_helper.audio_processing.separation.states import (
    ACTION_CANCEL_DOWNLOAD,
    ACTION_CANCEL_INSTALL,
    ACTION_CANCEL_START,
    ACTION_CONFIGURE,
    ACTION_RECONNECT,
    ACTION_RELOCATE_MODEL,
    ACTION_REPAIR,
    ACTION_RESCAN_MODEL,
    ACTION_RESELECT_ENV,
    ACTION_RETRY,
    ACTION_START_SERVICE,
    ACTION_STOP_SERVICE,
    ACTION_STOP_TASK,
    ACTION_UPDATE_RUNTIME,
    BUSY_STATES,
    TASK_CAPABLE_STATES,
    TASK_SPECS,
    TASK_STAGES,
    ServiceState,
    TaskType,
    format_size,
)
from krok_helper.audio_processing.separation.widgets import (
    AudioInputCard,
    CurrentTaskPanel,
    OutputSettingsCard,
    ResultsPanel,
    StatusActionBar,
    TaskCard,
)
from krok_helper.audio_processing.separation.wizard import WelcomeView, WizardView
from krok_helper.qfluent_compat import ask_fluent_confirm, show_fluent_info

_VIEW_WELCOME = "welcome"
_VIEW_WIZARD = "wizard"
_VIEW_WORKSPACE = "workspace"

#: 当前任务区展开的状态（模型下载/加载/推理）。
_TASK_PANEL_STATES = frozenset(
    {
        ServiceState.MODEL_DOWNLOADING,
        ServiceState.MODEL_LOADING,
        ServiceState.PROCESSING,
    }
)

#: 修复安装在工作区内展示进度的状态。
_RUNTIME_PANEL_STATES = frozenset(
    {
        ServiceState.RUNTIME_DOWNLOADING,
        ServiceState.RUNTIME_VERIFYING,
    }
)

_RUNTIME_STAGES = ("下载 Runtime", "校验并完成安装")


class AudioSeparationPage(QWidget):
    """音频分离 Tab。``settings.pymss`` namespace 就地读写并持久化。"""

    def __init__(
        self,
        settings,
        save_settings,
        parent: QWidget | None = None,
        backend: SeparationBackend | None = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._save_settings = save_settings
        settings_ns = getattr(settings, "pymss", None)
        if not isinstance(settings_ns, dict):
            settings_ns = {}
            setattr(settings, "pymss", settings_ns)
        self._settings_ns = settings_ns
        if backend is None:
            from krok_helper.audio_processing.separation.real_backend import (
                RealSeparationBackend,
            )

            backend = RealSeparationBackend(
                self._settings_ns,
                self._save_settings,
                ffmpeg_dir=str(getattr(settings, "ffmpeg_dir", "")),
                parent=self,
            )
        self._backend = backend
        self._wizard_active = False
        self._panel_mode: str | None = None  # None / "task" / "runtime"
        self._first_show_checked = False

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self._view_stack = QStackedWidget(self)
        root.addWidget(self._view_stack)

        # ── 视图 1：首次配置欢迎页 ─────────────────────────────────
        self._welcome = WelcomeView(self)
        self._welcome.flowSelected.connect(self._start_flow)
        self._view_stack.addWidget(self._welcome)

        # ── 视图 2：向导 ──────────────────────────────────────────
        self._wizard = WizardView(self._backend, self)
        self._wizard.finished.connect(self._on_wizard_finished)
        self._wizard.cancelled.connect(self._on_wizard_cancelled)
        self._view_stack.addWidget(self._wizard)

        # ── 视图 3：主工作区 ──────────────────────────────────────
        self._workspace = self._build_workspace()
        self._view_stack.addWidget(self._workspace)

        # ── 后端信号 ──────────────────────────────────────────────
        self._backend.snapshotChanged.connect(self._apply_snapshot)
        self._backend.taskProgressChanged.connect(self._on_task_progress)
        self._backend.resultReady.connect(self._on_result_ready)

        self._restore_workspace_inputs()
        self._apply_snapshot(self._backend.snapshot())

    # ── 工作区组装 ────────────────────────────────────────────────
    def _build_workspace(self) -> QWidget:
        scroll = FluentScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.enableTransparentBackground()
        # §3.7：页面统一纵向滚动，任何宽度下都不出现横向滚动条。
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        content = QWidget(scroll)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(2, 2, 10, 8)
        layout.setSpacing(12)

        self._status_bar = StatusActionBar(content)
        self._status_bar.primaryRequested.connect(self._dispatch_action)
        self._status_bar.secondaryRequested.connect(self._dispatch_action)
        self._status_bar.settingsRequested.connect(self._open_settings_dialog)
        layout.addWidget(self._status_bar)

        self._input_card = AudioInputCard(content)
        self._input_card.fileSelected.connect(self._on_input_selected)
        self._input_card.cleared.connect(self._persist_inputs)
        self._output_card = OutputSettingsCard(content)
        self._output_card.outputDirChanged.connect(self._on_output_dir_changed)
        self._output_card.formatChanged.connect(self._on_format_changed)

        self._materials_grid = ResponsiveGrid(min_column_width=360, max_columns=2, parent=content)
        self._materials_grid.set_widgets([self._input_card, self._output_card])
        layout.addWidget(self._materials_grid)

        self._task_cards: dict[TaskType, TaskCard] = {}
        cards = []
        for task in TaskType:
            card = TaskCard(task, content)
            card.triggered.connect(lambda t=task: self._on_task_triggered(t))
            self._task_cards[task] = card
            cards.append(card)
        self._tasks_grid = ResponsiveGrid(min_column_width=260, max_columns=3, parent=content)
        self._tasks_grid.set_widgets(cards)
        layout.addWidget(self._tasks_grid)

        self._task_panel = CurrentTaskPanel(content)
        self._task_panel.cancelRequested.connect(self._on_task_cancel)
        self._task_panel.setVisible(False)
        layout.addWidget(self._task_panel)

        self._results_panel = ResultsPanel(content)
        self._results_panel.setVisible(False)
        layout.addWidget(self._results_panel)

        layout.addStretch(1)
        scroll.setWidget(content)
        return scroll

    # ── 视图切换 ──────────────────────────────────────────────────
    def current_view(self) -> str:
        widget = self._view_stack.currentWidget()
        if widget is self._welcome:
            return _VIEW_WELCOME
        if widget is self._wizard:
            return _VIEW_WIZARD
        return _VIEW_WORKSPACE

    def _show_view(self, name: str) -> None:
        widget = {
            _VIEW_WELCOME: self._welcome,
            _VIEW_WIZARD: self._wizard,
            _VIEW_WORKSPACE: self._workspace,
        }[name]
        self._view_stack.setCurrentWidget(widget)

    # ── 状态应用（需求文档 §3.6） ──────────────────────────────────
    def _apply_snapshot(self, snapshot) -> None:
        if snapshot.state == ServiceState.UNCONFIGURED:
            self._wizard_active = False
            self._show_view(_VIEW_WELCOME)
        elif self._wizard_active:
            self._show_view(_VIEW_WIZARD)
        else:
            self._show_view(_VIEW_WORKSPACE)

        self._status_bar.apply_snapshot(snapshot)

        tasks_operable = snapshot.state in TASK_CAPABLE_STATES
        task_active = snapshot.state in _TASK_PANEL_STATES
        task_error = (
            snapshot.state is ServiceState.ERROR and snapshot.pending_task is not None
        )
        for task, card in self._task_cards.items():
            dep = snapshot.dependencies.get(task)
            if dep is not None:
                card.set_dependency(
                    dep,
                    service_ready=tasks_operable or task_active or task_error,
                    unavailable_reason=(
                        "当前任务进行中"
                        if task_active
                        else "请先处理上方错误"
                        if task_error
                        else ""
                    ),
                )

        self._sync_task_panel(snapshot)

    def _sync_task_panel(self, snapshot) -> None:
        if snapshot.state in _TASK_PANEL_STATES:
            if self._panel_mode != "task":
                self._panel_mode = "task"
                self._task_panel.set_stage_names(TASK_STAGES)
                title = TASK_SPECS[snapshot.pending_task].title if snapshot.pending_task else ""
                self._task_panel.start(title)
            self._task_panel.setVisible(True)
            return
        if snapshot.state in _RUNTIME_PANEL_STATES and not self._wizard_active:
            if self._panel_mode != "runtime":
                self._panel_mode = "runtime"
                self._task_panel.set_stage_names(_RUNTIME_STAGES)
                self._task_panel.start("修复安装")
            stage = 0 if snapshot.state == ServiceState.RUNTIME_DOWNLOADING else 1
            self._task_panel.update_progress(
                TaskProgress(
                    title="修复安装",
                    stage_index=stage,
                    download_done=snapshot.download_done,
                    download_total=snapshot.download_total,
                    show_download=snapshot.state == ServiceState.RUNTIME_DOWNLOADING,
                )
            )
            self._task_panel.setVisible(True)
            return
        if self._panel_mode is not None:
            self._panel_mode = None
            self._task_panel.stop()
            self._task_panel.setVisible(False)

    # ── 任务触发 ──────────────────────────────────────────────────
    def _on_task_triggered(self, task: TaskType) -> None:
        snapshot = self._backend.snapshot()
        if snapshot.state not in TASK_CAPABLE_STATES:
            show_fluent_info(self, "请先启动 PyMSS 服务。")
            return
        dep = snapshot.dependencies.get(task)
        if dep is None:
            return
        if not dep.ready and dep.download_bytes > 0:
            spec = TASK_SPECS[task]
            if not ask_fluent_confirm(
                self,
                f"「{spec.title}」需要下载推荐模型（{format_size(dep.download_bytes)}），"
                "下载完成后自动开始任务。是否继续？",
                yes_text="下载并继续",
            ):
                return
        elif not dep.ready:
            show_fluent_info(
                self,
                (dep.reason or "该任务当前不可用。")
                + "\n可在设置对话框的「修复与重置」页处理，或重新运行迁移向导。",
            )
            return

        input_path = self._input_card.path()
        if not input_path:
            show_fluent_info(self, "请先选择待处理的音频素材。")
            return
        output_dir = self._output_card.output_dir() or str(Path(input_path).parent)
        self._backend.request_task(
            task,
            input_path=input_path,
            output_dir=output_dir,
            output_format=self._output_card.output_format(),
        )
        if dep.download_bytes > 0 and not dep.ready:
            # 用户已确认体积，直接开始按需下载（需求文档 §8.4）。
            self._backend.start_model_download()

    def _on_task_cancel(self) -> None:
        snapshot = self._backend.snapshot()
        if snapshot.state in _RUNTIME_PANEL_STATES:
            self._backend.cancel_install()
        else:
            self._backend.cancel_task()

    def _on_task_progress(self, progress: TaskProgress) -> None:
        if self._panel_mode == "task":
            self._task_panel.update_progress(progress)

    def _on_result_ready(self, result) -> None:
        self._results_panel.add_result(result)
        self._results_panel.setVisible(True)

    # ── 状态条动作分发 ─────────────────────────────────────────────
    def _dispatch_action(self, action: str) -> None:
        backend = self._backend
        if action == ACTION_CONFIGURE:
            self._wizard_active = False
            self._show_view(_VIEW_WELCOME)
        elif action == ACTION_START_SERVICE:
            backend.start_service()
        elif action == ACTION_CANCEL_START:
            backend.cancel_start()
        elif action == ACTION_STOP_SERVICE:
            backend.stop_service()
        elif action == ACTION_REPAIR:
            if ask_fluent_confirm(
                self,
                "修复会根据当前设备重新获取缺失或损坏的 Runtime 文件（CPU 约数百 MB；"
                "NVIDIA CUDA 最多约 3–4 GB），不会删除模型、MSST 映射或缓存。是否继续？",
                yes_text="修复安装",
            ):
                backend.repair_install()
        elif action == ACTION_RESELECT_ENV:
            self._start_flow(FLOW_EXISTING)
        elif action == ACTION_UPDATE_RUNTIME:
            self._start_flow(FLOW_UPGRADE)
        elif action == ACTION_CANCEL_INSTALL:
            backend.cancel_install()
        elif action in (ACTION_CANCEL_DOWNLOAD, ACTION_STOP_TASK):
            backend.cancel_task()
        elif action in (ACTION_RELOCATE_MODEL, ACTION_RESCAN_MODEL):
            self._start_flow(FLOW_REMAP_MSST)
        elif action in (ACTION_RECONNECT, ACTION_RETRY):
            backend.refresh()

    # ── 向导 ─────────────────────────────────────────────────────
    def _start_flow(self, flow: str) -> None:
        self._wizard_active = True
        self._wizard.start_flow(flow)
        self._backend.start_wizard(flow)
        self._show_view(_VIEW_WIZARD)

    def _on_wizard_finished(self) -> None:
        self._wizard_active = False
        self._persist_inputs()
        self._apply_snapshot(self._backend.snapshot())

    def _on_wizard_cancelled(self) -> None:
        self._wizard_active = False
        self._backend.cleanup_incomplete()
        self._apply_snapshot(self._backend.snapshot())

    # ── 设置对话框 ────────────────────────────────────────────────
    def _open_settings_dialog(self) -> None:
        dialog = SeparationSettingsDialog(
            self._backend, self._settings_ns, self._save_settings, self
        )
        dialog.reconfigureRequested.connect(lambda: self._start_flow(FLOW_EXISTING))
        dialog.exec()
        # 对话框可能改了输出目录等，回来后同步一次工作区。
        self._restore_workspace_inputs()
        self._apply_snapshot(self._backend.snapshot())

    # ── 输入/输出持久化（§3.4：两张卡都保留最近一次有效选择） ──────
    def _restore_workspace_inputs(self) -> None:
        last_input = str(self._settings_ns.get("last_input", ""))
        if last_input and Path(last_input).is_file():
            self._input_card.set_path(last_input, emit=False)
        output_dir = str(self._settings_ns.get("output_dir", ""))
        if output_dir:
            self._output_card.set_output_dir(output_dir, emit=False)
        output_format = str(self._settings_ns.get("output_format", "wav"))
        self._output_card.set_output_format(output_format)

    def _persist_inputs(self) -> None:
        self._settings_ns["last_input"] = self._input_card.path()
        self._settings_ns["output_dir"] = self._output_card.output_dir()
        self._settings_ns["output_format"] = self._output_card.output_format()
        self._save_settings()

    def _on_input_selected(self, _path: str) -> None:
        self._persist_inputs()

    def _on_output_dir_changed(self, _path: str) -> None:
        self._persist_inputs()

    def _on_format_changed(self, _fmt: str) -> None:
        self._persist_inputs()

    # ── 退出清理（工作台关闭时由宿主调用） ─────────────────────────
    def is_busy(self) -> bool:
        """Return whether closing now would interrupt installation or a task."""
        return self._backend.snapshot().state in BUSY_STATES

    def shutdown(self, timeout_ms: int = 5000) -> bool:
        """Stop the managed backend before the host exits or updates."""
        return self._backend.shutdown(timeout_ms=timeout_ms)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        if self._first_show_checked:
            return
        self._first_show_checked = True
        # Normal visits perform only the cheap manifest/version/size check.
        # Full hashing is reserved for explicit diagnostics and failures.
        self._backend.refresh(full=False)
