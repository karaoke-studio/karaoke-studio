"""音频分离（PyMSS）一页式工作区（需求文档 §3.3 / §3.4 / §3.6）。

状态驱动：``UNCONFIGURED`` 显示首次配置欢迎页；向导状态显示向导页；其余状态
显示主工作区。工作区自上而下：状态与操作条 → 素材与输出双卡 → 三张任务卡 →
当前任务区（有任务时展开）→ 结果区（有结果时展开）。
"""

from __future__ import annotations

import logging
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    CaptionLabel,
    PrimaryPushButton,
    ScrollArea as FluentScrollArea,
)

from krok_helper.media_formats import HIRES_AUDIO_EXTENSIONS
from krok_helper.workflow_host import AccompanimentSink, OnVocalSink
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

log = logging.getLogger(__name__)

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
        workflow_context: AccompanimentSink | None = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._save_settings = save_settings
        #: 主窗口（用来把产物交给后续模块）；独立运行分离页时可以是 None。
        self._workflow_context = workflow_context
        #: 本批任务已产出的结果，整批结束时用来问要不要转交 Hi-Res。
        self._batch_results: list = []
        self._batch_input_path: Path | None = None
        settings_ns = getattr(settings, "pymss", None)
        if not isinstance(settings_ns, dict):
            settings_ns = {}
            setattr(settings, "pymss", settings_ns)
        self._settings_ns = settings_ns
        if backend is None:
            backend = self._create_backend(str(self._settings_ns.get("mode", "")))
        self._backend = backend
        self._wizard_active = False
        self._panel_mode: str | None = None  # None / "task" / "runtime"
        self._panel_title = ""
        self._first_show_checked = False
        self._was_busy = False

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

    def _create_backend(self, mode: str) -> SeparationBackend:
        """按模式建后端。MSST 模式驱动用户已有环境，与 PyMSS 是并列实现。"""
        if mode == "msst":
            from krok_helper.audio_processing.separation.msst_backend import (
                MsstSeparationBackend,
            )

            return MsstSeparationBackend(self._settings_ns, self._save_settings, parent=self)
        from krok_helper.audio_processing.separation.real_backend import (
            RealSeparationBackend,
        )

        return RealSeparationBackend(
            self._settings_ns,
            self._save_settings,
            ffmpeg_dir=str(getattr(self._settings, "ffmpeg_dir", "")),
            parent=self,
        )

    def _switch_backend(self, mode: str) -> None:
        """切换后端实现：拆掉旧的信号与进程，再把新后端接到同一套界面上。"""
        old = self._backend
        try:
            old.snapshotChanged.disconnect(self._apply_snapshot)
            old.taskProgressChanged.disconnect(self._on_task_progress)
            old.resultReady.disconnect(self._on_result_ready)
        except TypeError:
            pass
        try:
            old.shutdown()
        except Exception:
            pass

        try:
            self._backend = self._create_backend(mode)
        except Exception:
            # 旧后端已经断连并 shutdown 了，界面此刻挂在一个半死的后端上；至少把原因
            # 留进日志，否则只能看到「选了 PyMSS 却在检测 MSST」这种转了几道弯的表象。
            log.exception("切换音频分离后端失败 mode=%r，仍在使用 %s", mode, type(old).__name__)
            raise
        self._wizard.rebind(self._backend)
        self._backend.snapshotChanged.connect(self._apply_snapshot)
        self._backend.taskProgressChanged.connect(self._on_task_progress)
        self._backend.resultReady.connect(self._on_result_ready)

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
            card.selectionChanged.connect(lambda: self._refresh_run_bar())
            self._task_cards[task] = card
            cards.append(card)
        self._tasks_grid = ResponsiveGrid(min_column_width=260, max_columns=3, parent=content)
        self._tasks_grid.set_widgets(cards)
        layout.addWidget(self._tasks_grid)

        # 底部统一操作栏：勾选若干任务后一次提交，按顺序执行。
        run_row = QHBoxLayout()
        run_row.setContentsMargins(2, 0, 2, 0)
        run_row.setSpacing(10)
        self._run_hint = CaptionLabel("", content)
        self._run_hint.setWordWrap(True)
        run_row.addWidget(self._run_hint, 1)
        self._run_button = PrimaryPushButton("开始分离", content)
        self._run_button.setMinimumWidth(190)
        self._run_button.clicked.connect(self._start_selected_tasks)
        run_row.addWidget(self._run_button, 0)
        layout.addLayout(run_row)

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
        queued = list(snapshot.queued_tasks or ())
        for task, card in self._task_cards.items():
            dep = snapshot.dependencies.get(task)
            if dep is not None:
                if task_active and snapshot.pending_task is task:
                    queue_label = "进行中"
                elif task in queued:
                    queue_label = f"排队中 · 第 {queued.index(task) + 1} 位"
                else:
                    queue_label = ""
                card.set_dependency(
                    dep,
                    service_ready=tasks_operable or task_active or task_error,
                    unavailable_reason=(
                        "当前有任务在进行"
                        if task_active
                        else "请先处理上方错误"
                        if task_error
                        else ""
                    ),
                    queue_label=queue_label,
                )

        # 整批结束时清空勾选：否则残留哪张卡取决于它在队列里的位置（最后一个会留下），
        # 结果随时序而定。跑完就重新选，状态也更确定。
        busy = snapshot.state in _TASK_PANEL_STATES
        if busy and not self._was_busy:
            self._batch_results = []
        if self._was_busy and not busy:
            for card in self._task_cards.values():
                card.set_selected(False, emit=False)
            self._offer_accompaniment_handoff()
        self._was_busy = busy

        self._refresh_run_bar(snapshot)
        self._sync_task_panel(snapshot)

    def _sync_task_panel(self, snapshot) -> None:
        if snapshot.state in _TASK_PANEL_STATES:
            title = TASK_SPECS[snapshot.pending_task].title if snapshot.pending_task else ""
            if snapshot.queue_total > 1:
                title = f"{title}（第 {snapshot.queue_done + 1} / 共 {snapshot.queue_total} 个）"
            if self._panel_mode != "task":
                self._panel_mode = "task"
                self._task_panel.set_stage_names(TASK_STAGES)
                self._task_panel.start(title)
            elif title != self._panel_title:
                # 队列换到下一个任务：标题要跟着走，之前只在首次进入时设过一次。
                self._task_panel.set_title(title)
            self._panel_title = title
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
    # ── 多选与批量执行 ────────────────────────────────────────────
    def _selected_tasks(self) -> list[TaskType]:
        return [task for task in TaskType if self._task_cards[task].is_selected()]

    def _refresh_run_bar(self, snapshot=None) -> None:
        # 必须用本次渲染的同一份快照，否则底部栏会和任务卡显示的状态打架。
        if snapshot is None:
            snapshot = self._backend.snapshot()
        busy = snapshot.state in _TASK_PANEL_STATES
        if busy:
            done, total = snapshot.queue_done, max(1, snapshot.queue_total)
            self._run_button.setEnabled(False)
            self._run_button.setText("正在分离…")
            self._run_hint.setText(f"正在执行第 {min(done + 1, total)} / 共 {total} 个任务")
            return

        selected = self._selected_tasks()
        self._run_button.setEnabled(bool(selected))
        self._run_button.setText(
            f"开始分离（已选 {len(selected)} 项）" if selected else "开始分离"
        )
        if not selected:
            self._run_hint.setText(
                "勾选要执行的任务；可以多选，将按人声 → 伴奏 → 和声伴奏的顺序依次完成。"
            )
            return
        pending = sum(self._task_cards[task].download_bytes() for task in selected)
        self._run_hint.setText(
            f"将依次执行 {len(selected)} 个任务；需要先下载 {format_size(pending)} 模型。"
            if pending
            else f"将依次执行 {len(selected)} 个任务。"
        )

    def _start_selected_tasks(self) -> None:
        selected = self._selected_tasks()
        if not selected:
            return
        snapshot = self._backend.snapshot()
        if snapshot.state in _TASK_PANEL_STATES:
            show_fluent_info(self, "当前已有分离任务在进行，请等待完成或先停止。")
            return
        if snapshot.state not in TASK_CAPABLE_STATES:
            show_fluent_info(self, "请先启动 PyMSS 服务。")
            return

        input_path = self._input_card.path()
        if not input_path:
            show_fluent_info(self, "请先选择待处理的音频或视频素材。")
            return

        pending = sum(self._task_cards[task].download_bytes() for task in selected)
        if pending and not ask_fluent_confirm(
            self,
            f"所选任务需要先下载 {format_size(pending)} 的模型，"
            "下载完成后会自动继续。是否继续？",
            yes_text="下载并继续",
        ):
            return

        output_dir = self._output_card.output_dir() or str(Path(input_path).parent)
        # 记下这一批真正送进去的那份音频：转交对话框要拿它当"原唱"，
        # 而卡片里的路径在跑的过程中随时可能被用户换掉。
        self._batch_input_path = Path(input_path)
        self._backend.request_tasks(
            selected,
            input_path=input_path,
            output_dir=output_dir,
            output_format=self._output_card.output_format(),
        )
        # 模型缺失时后端会停在 MODEL_REQUIRED，等这里确认后再开始下载（§8.4）。
        if self._backend.snapshot().state is ServiceState.MODEL_REQUIRED:
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

    # ── 与后续模块的联动 ──────────────────────────────────────────
    def _offer_accompaniment_handoff(self) -> None:
        """整批分离结束后，问用户要不要把伴奏交给第 6 步 Hi-Res 混流。"""
        from krok_helper.audio_processing.separation.handoff import (
            AccompanimentHandoffDialog,
            collect_accompaniments,
        )

        results, self._batch_results = self._batch_results, []
        source, self._batch_input_path = self._batch_input_path, None
        host = self._workflow_context
        if host is None:
            # 分离页被单独拉起来跑（没有工作台外壳），没有下一步可交。
            return
        if not isinstance(host, AccompanimentSink):
            # 有宿主却不满足契约 —— 多半是宿主侧改了方法名。以前这里是
            # ``getattr(..., None)`` 静默返回，用户点完转交什么也不会发生
            # 且无迹可寻；记一条日志好歹能查。
            logging.getLogger(__name__).warning(
                "工作台宿主缺少 accept_separated_accompaniment，伴奏无法转交下一步"
            )
            return
        candidates = collect_accompaniments(results)
        if not candidates:
            return

        # 宿主不收原唱时就别把那条勾选项摆出来 —— 勾了也没地方去。
        # 视频素材同理：解复用出来的音轨是任务临时文件、这时已经清掉了，能交出去的
        # 只有原始容器，而下一步只认音频与 .mp4。
        offer_source = source if isinstance(host, OnVocalSink) else None
        if offer_source is not None and (
            offer_source.suffix.lower() not in HIRES_AUDIO_EXTENSIONS
        ):
            offer_source = None
        dialog = AccompanimentHandoffDialog(candidates, self.window(), source_audio=offer_source)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        selected = dialog.selected_paths()
        if selected:
            host.accept_separated_accompaniment(selected)
        on_vocal = dialog.source_as_on_vocal()
        if on_vocal is not None:
            host.accept_source_as_on_vocal(on_vocal)


    @property
    def backend(self) -> SeparationBackend:
        """当前分离后端（宿主 AI 打轴等外部能力使用，只读）。"""
        return self._backend

    def _on_result_ready(self, result) -> None:
        self._results_panel.add_result(result)
        self._results_panel.setVisible(True)
        self._batch_results.append(result)

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
    def _backend_matches(self, mode: str) -> bool:
        """当前后端是不是这个模式该用的实现。"""
        from krok_helper.audio_processing.separation.msst_backend import (
            MsstSeparationBackend,
        )

        is_msst = isinstance(self._backend, MsstSeparationBackend)
        return is_msst if mode == "msst" else not is_msst

    def _start_flow(self, flow: str) -> None:
        from krok_helper.audio_processing.separation.backend import FLOW_MSST

        # MSST 是另一套后端实现，必须在向导开始前换掉，否则向导操作的是 PyMSS 后端。
        #
        # 判断依据必须是**当前后端本身**，不能拿设置里的 mode 字符串当替身：那只是
        # 真实状态的一个副本，一旦两者不同步（mode 已经是目标值、后端却还是旧的），
        # 这里就会跳过切换，向导挂在错的后端上——选「使用已有 PyMSS」却跑出 MSST 的
        # 能力检测。更糟的是它自我维持：mode 已等于目标值，之后每次重进都照样跳过。
        wanted = "msst" if flow == FLOW_MSST else ""
        self._settings_ns["mode"] = wanted
        if not self._backend_matches(wanted):
            self._switch_backend(wanted)
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
        # 音频素材是「这一首歌」的临时选择，不是设置：重开软件应当是空的。
        # 输出目录与格式是偏好，继续保留。
        self._settings_ns.pop("last_input", None)
        output_dir = str(self._settings_ns.get("output_dir", ""))
        if output_dir:
            self._output_card.set_output_dir(output_dir, emit=False)
        output_format = str(self._settings_ns.get("output_format", "wav"))
        self._output_card.set_output_format(output_format)

    def _persist_inputs(self) -> None:
        # 只持久化偏好类设置；音频素材不写盘，重开软件从空白开始。
        self._settings_ns.pop("last_input", None)
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
