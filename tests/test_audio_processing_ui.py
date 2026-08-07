"""音频处理模块（第 2 步容器 + 音频分离 UI 框架）测试。

后端用 ``MockSeparationBackend(simulate_delays=False)``，全部迁移同步完成。
对应需求文档 docs/音视频处理-PyMSS音频分离需求设计.md §16.4-6。
"""

from __future__ import annotations

from PyQt6.QtCore import QTimer, Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QWidget
from qfluentwidgets import MessageBoxBase

from krok_helper.audio_processing import AudioProcessingPage, AudioSeparationPage
from krok_helper.audio_processing.responsive import ResponsiveGrid
from krok_helper.audio_processing.separation.backend import (
    ExternalModelCandidate,
    FLOW_FULL,
    FLOW_REUSE_MSST,
    FLOW_UPGRADE,
    MockSeparationBackend,
    TaskProgress,
    TaskResult,
    ResultFile,
)
from krok_helper.audio_processing.separation.states import (
    ACTION_REPAIR,
    ACTION_UPDATE_RUNTIME,
    ServiceState,
    TaskType,
)
from krok_helper.audio_processing.separation.settings_dialog import (
    SeparationSettingsDialog,
)
from krok_helper.audio_processing.separation.widgets import ResultsPanel
from krok_helper.qfluent_compat import HostFluentMessageDialog
from krok_helper.settings import AppSettings


def _make_separation_page(settings: AppSettings | None = None) -> AudioSeparationPage:
    settings = settings or AppSettings()
    backend = MockSeparationBackend(settings.pymss, simulate_delays=False)
    return AudioSeparationPage(settings, lambda: None, backend=backend)


class TestContainerPage:
    def test_default_tab_is_alignment(self) -> None:
        settings = AppSettings()
        container = AudioProcessingPage(QWidget(), QWidget(), settings, lambda: None)
        assert container.current_tab() == "alignment"


class TestSeparationSettingsDialog:
    def test_external_address_is_reconfigured_through_capability_wizard(self) -> None:
        settings = {
            "external_server_url": "http://127.0.0.1:8765",
            "download_source": "modelscope",
        }
        backend = MockSeparationBackend(settings, simulate_delays=False)
        saved = []
        dialog = SeparationSettingsDialog(
            backend, settings, lambda: saved.append(True)
        )
        requested = []
        dialog.reconfigureRequested.connect(lambda: requested.append(True))

        assert dialog._url_label.text() == "http://127.0.0.1:8765"
        assert all(not button.isEnabled() for button in dialog._managed_action_buttons)
        dialog._source_combo.setCurrentIndex(1)
        assert settings["download_source"] == "huggingface"
        assert saved
        dialog._request_reconfigure()
        assert requested == [True]


class TestManagedUpgradeFlow:
    def test_host_confirmation_dialog_accepts_mouse_click(self) -> None:
        host = QWidget()
        host.resize(800, 500)
        host.show()
        page = QWidget(host)
        dialog = HostFluentMessageDialog("卡拉OK工作台", "是否继续？", page)

        assert not isinstance(dialog, MessageBoxBase)
        assert dialog.parentWidget() is host
        assert dialog.windowModality() == Qt.WindowModality.ApplicationModal

        QTimer.singleShot(
            0,
            lambda: QTest.mouseClick(
                dialog.yesButton,
                Qt.MouseButton.LeftButton,
                pos=dialog.yesButton.rect().center(),
            ),
        )
        assert dialog.exec() == 1
        assert dialog._dim is None
        host.close()

    def test_incompatible_managed_runtime_uses_confirmed_upgrade_wizard(self) -> None:
        settings = AppSettings()
        settings.pymss["install_dir"] = "D:/demo/pymss"
        page = _make_separation_page(settings)
        page._backend._set_state(ServiceState.VERSION_INCOMPATIBLE)

        page._dispatch_action(ACTION_UPDATE_RUNTIME)

        assert page.current_view() == "wizard"
        assert page._wizard.flow == FLOW_UPGRADE
        assert page._wizard._steps[0].title == "确认安装信息"
        assert page._backend.snapshot().install_dir == "D:/demo/pymss"

    def test_repair_requires_confirmation_before_large_download(
        self, monkeypatch
    ) -> None:
        settings = AppSettings()
        settings.pymss["install_dir"] = "D:/demo/pymss"
        page = _make_separation_page(settings)
        repairs: list[bool] = []
        page._backend.repair_install = lambda: repairs.append(True)

        monkeypatch.setattr(
            "krok_helper.audio_processing.separation.page.ask_fluent_confirm",
            lambda *_args, **_kwargs: False,
        )
        page._dispatch_action(ACTION_REPAIR)
        assert repairs == []

        monkeypatch.setattr(
            "krok_helper.audio_processing.separation.page.ask_fluent_confirm",
            lambda *_args, **_kwargs: True,
        )
        page._dispatch_action(ACTION_REPAIR)
        assert repairs == [True]

    def test_tab_switch_persists_and_restores(self) -> None:
        settings = AppSettings()
        saved: list[bool] = []
        container = AudioProcessingPage(
            QWidget(), QWidget(), settings, lambda: saved.append(True)
        )
        container.switch_tab("separation")
        assert settings.pymss["last_internal_tab"] == "separation"
        assert saved, "切换内部 Tab 应触发设置保存"

        restored = AudioProcessingPage(QWidget(), QWidget(), settings, lambda: None)
        assert restored.current_tab() == "separation"

    def test_invalid_tab_falls_back_to_alignment(self) -> None:
        settings = AppSettings()
        settings.pymss["last_internal_tab"] = "not-a-tab"
        container = AudioProcessingPage(QWidget(), QWidget(), settings, lambda: None)
        assert container.current_tab() == "alignment"


class TestMockBackendFlow:
    def test_active_task_does_not_claim_service_is_stopped(self) -> None:
        page = _make_separation_page()
        backend = page._backend
        backend.confirm_install_location("D:/demo/pymss")
        backend.start_install()
        backend.start_service()
        snapshot = backend.snapshot()
        snapshot.state = ServiceState.MODEL_DOWNLOADING
        snapshot.pending_task = TaskType.INSTRUMENTAL

        page._apply_snapshot(snapshot)

        for card in page._task_cards.values():
            assert card._reason.text() == "当前任务进行中"
            assert "需要先启动" not in card._reason.text()
            assert not card._action_button.isEnabled()

        snapshot.state = ServiceState.ERROR
        snapshot.error = "模拟任务错误"
        page._apply_snapshot(snapshot)
        for card in page._task_cards.values():
            assert card._reason.text() == "请先处理上方错误"
            assert "需要先启动" not in card._reason.text()
            assert not card._action_button.isEnabled()

    def test_full_install_to_result_chain(self) -> None:
        backend = MockSeparationBackend({}, simulate_delays=False)
        results: list[TaskResult] = []
        backend.resultReady.connect(results.append)

        assert backend.snapshot().state == ServiceState.UNCONFIGURED
        backend.start_wizard(FLOW_FULL)
        assert backend.snapshot().state == ServiceState.LOCATION_REQUIRED
        backend.confirm_install_location("D:/demo/pymss")
        backend.start_install()
        assert backend.snapshot().state == ServiceState.INSTALLED_STOPPED
        backend.start_service()
        assert backend.snapshot().state == ServiceState.SERVICE_READY

        # 缺模型任务：MODEL_REQUIRED → 下载 → 加载 → 处理 → 就绪
        backend.request_task(
            TaskType.HARMONY, input_path="D:/in.wav", output_dir="D:/out", output_format="wav"
        )
        assert backend.snapshot().state == ServiceState.MODEL_REQUIRED
        backend.start_model_download()
        assert backend.snapshot().state == ServiceState.SERVICE_READY

        assert len(results) == 1
        labels = [f.label for f in results[0].files]
        assert labels == ["和声伴奏"], "和声伴奏为单阶段任务，只输出一个文件"

    def test_ready_task_runs_without_download(self) -> None:
        backend = MockSeparationBackend({}, simulate_delays=False)
        backend.confirm_install_location("D:/demo/pymss")
        backend.start_install()
        backend.start_service()
        backend.request_task(
            TaskType.VOCAL, input_path="D:/in.wav", output_dir="", output_format="flac"
        )
        backend.start_model_download()  # 完成 vocal 模型下载
        states = []
        backend.snapshotChanged.connect(lambda s: states.append(s.state))
        backend.request_task(
            TaskType.VOCAL, input_path="D:/in.wav", output_dir="", output_format="flac"
        )
        assert ServiceState.MODEL_REQUIRED not in states
        assert backend.snapshot().state == ServiceState.SERVICE_READY

    def test_remove_configuration_returns_to_unconfigured(self) -> None:
        backend = MockSeparationBackend({}, simulate_delays=False)
        backend.confirm_install_location("D:/demo/pymss")
        backend.start_install()
        assert backend.snapshot().state == ServiceState.INSTALLED_STOPPED
        backend.remove_configuration()
        assert backend.snapshot().state == ServiceState.UNCONFIGURED

    def test_msst_scan_uses_async_signal_contract(self) -> None:
        backend = MockSeparationBackend({}, simulate_delays=False)
        started: list[bool] = []
        finished: list[list[ExternalModelCandidate]] = []
        backend.msstScanStarted.connect(lambda: started.append(True))
        backend.msstScanFinished.connect(finished.append)

        backend.start_msst_scan("D:/MSST-WebUI")

        assert started == [True]
        assert len(finished) == 1
        assert all(isinstance(item, ExternalModelCandidate) for item in finished[0])

    def test_existing_check_uses_async_signal_contract(self) -> None:
        backend = MockSeparationBackend({}, simulate_delays=False)
        finished = []
        backend.existingCheckFinished.connect(finished.append)

        backend.start_existing_check(server_url="http://127.0.0.1:8000")

        assert len(finished) == 1
        assert all(ok for _name, ok, _detail in finished[0])


class TestSeparationPageStates:
    def test_unconfigured_shows_welcome(self) -> None:
        page = _make_separation_page()
        assert page.current_view() == "welcome"

    def test_installed_stopped_shows_workspace_with_start_action(self) -> None:
        settings = AppSettings()
        settings.pymss["install_dir"] = "D:/demo/pymss"
        page = _make_separation_page(settings)
        assert page.current_view() == "workspace"
        primary = page._status_bar._primary_button
        assert primary.text() == "启动服务"
        # 顶层从未 show()，isVisible() 恒为 False；这里只关心按钮没被隐藏。
        assert not primary.isHidden()

    def test_service_ready_shows_download_badge_on_missing_model(self) -> None:
        settings = AppSettings()
        settings.pymss["install_dir"] = "D:/demo/pymss"
        page = _make_separation_page(settings)
        backend = page._backend
        backend.start_service()
        QApplication.instance().processEvents()
        card = page._task_cards[TaskType.VOCAL]
        assert "需下载" in card._pill.text()
        assert "下载并继续" in card._action_button.text()
        assert card._action_button.isEnabled()

    def test_wizard_cancel_returns_to_welcome(self) -> None:
        page = _make_separation_page()
        page._welcome.flowSelected.emit(FLOW_FULL)
        assert page.current_view() == "wizard"
        page._wizard.cancelled.emit()
        assert page.current_view() == "welcome"
        assert page._backend.snapshot().state == ServiceState.UNCONFIGURED

    def test_results_panel_grouping(self) -> None:
        panel = ResultsPanel()
        assert panel.group_count() == 0
        panel.add_result(
            TaskResult(
                task=TaskType.VOCAL,
                title="分离人声",
                finished_at="12:00:00",
                files=[ResultFile(path="D:/out/歌_人声.wav", label="人声", size_bytes=1024)],
            )
        )
        panel.add_result(
            TaskResult(
                task=TaskType.HARMONY,
                title="提取和声",
                finished_at="12:01:00",
                files=[
                    ResultFile(path="D:/out/歌_主唱.wav", label="主唱", size_bytes=1024),
                    ResultFile(path="D:/out/歌_和声.wav", label="和声", size_bytes=1024),
                ],
            )
        )
        assert panel.group_count() == 2
        panel.clear_results()
        assert panel.group_count() == 0


class TestResponsiveGrid:
    def test_columns_collapse_on_narrow_width(self) -> None:
        grid = ResponsiveGrid(min_column_width=260, max_columns=3)
        cards = [QWidget() for _ in range(3)]
        grid.set_widgets(cards)

        grid.resize(1200, 400)
        grid._relayout()
        assert grid.column_count() == 3

        grid.resize(500, 400)
        grid._relayout()
        assert grid.column_count() == 1


class TestSwitcherAndWidgets:
    """内部 Tab 切换控件与向导 UI 组件。"""

    def test_container_uses_workspace_switcher(self) -> None:
        """第 2 步内部 Tab 复用字幕模块那套药丸分段控件。"""
        from krok_helper.workspace_switcher import WorkspaceSwitcher

        settings = AppSettings()
        container = AudioProcessingPage(QWidget(), QWidget(), settings, lambda: None)
        assert isinstance(container._pivot, WorkspaceSwitcher)
        assert container._pivot.currentRouteKey() == "alignment"

        container.switch_tab("separation")
        assert container._pivot.currentRouteKey() == "separation"

    def test_option_card_selection_is_exclusive(self) -> None:
        from krok_helper.audio_processing.separation.wizard import InstallLocationStep

        page = _make_separation_page()
        page._welcome.flowSelected.emit(FLOW_FULL)
        step = page._wizard._steps[0]
        assert isinstance(step, InstallLocationStep)

        assert step._root_option.is_checked()
        assert not step._custom_option.is_checked()
        assert not step._path_row.isVisibleTo(step)

        step._select(root=False)
        assert not step._root_option.is_checked()
        assert step._custom_option.is_checked()
        # 自定义目录未填写前不能进入下一步。
        assert step.install_path() == ""
        assert not step.can_proceed()

    def test_info_grid_rebuilds_rows(self) -> None:
        from krok_helper.audio_processing.separation.widgets import InfoGrid

        grid = InfoGrid()
        grid.set_rows([("A", "1"), ("B", "2"), ("C", "3")])
        assert grid._grid.count() == 6
        grid.set_rows([("A", "1")])
        assert grid._grid.count() == 2

    def test_current_task_panel_shows_real_audio_processing_progress(self) -> None:
        from krok_helper.audio_processing.separation.widgets import CurrentTaskPanel

        panel = CurrentTaskPanel()
        panel.start("分离伴奏")
        panel.update_progress(
            TaskProgress(
                title="分离伴奏",
                stage_index=3,
                current_file="inst_v1e",
                processing_done=30,
                processing_total=120,
                show_processing=True,
            )
        )

        assert not panel._download_row.isHidden()
        assert panel._busy_bar.isHidden()
        assert panel._download_bar.value() == 250
        assert panel._download_text.text() == "已处理 00:30 / 02:00（25%）"

    def test_stepper_labels_follow_flow(self) -> None:
        page = _make_separation_page()
        page._welcome.flowSelected.emit(FLOW_FULL)
        assert page._wizard._stepper._titles == ["安装位置", "确认", "下载安装", "完成"]
        assert page._wizard._stepper._current == 0

    def test_install_progress_explains_post_download_work(self) -> None:
        from krok_helper.audio_processing.separation.wizard import ProgressStep

        page = _make_separation_page()
        page._welcome.flowSelected.emit(FLOW_FULL)
        step = page._wizard._steps[2]
        assert isinstance(step, ProgressStep)
        assert "音频分离组件" in step.hint
        assert "可能需要几分钟" in step.layout().itemAt(2).widget().text()

        snapshot = page._backend.snapshot()
        snapshot.state = ServiceState.RUNTIME_DOWNLOADING
        snapshot.download_done = 3_273_024_349
        snapshot.download_total = 3_273_024_349
        step.apply_snapshot(snapshot)
        assert "下载完成" in step._status.text()
        assert "正在解压并安装" in step._status.text()

        snapshot.state = ServiceState.RUNTIME_VERIFYING
        step.apply_snapshot(snapshot)
        assert step._status.text() == "正在校验安装并启动服务检查…"

    def test_install_confirmation_freezes_device_and_download_estimate(self) -> None:
        page = _make_separation_page()
        page._welcome.flowSelected.emit(FLOW_FULL)
        page._wizard._go_next()
        step = page._wizard._steps[1]
        texts = [
            item.widget().text()
            for index in range(step._summary._grid.count())
            if (item := step._summary._grid.itemAt(index)).widget() is not None
        ]

        assert "设备方案" in texts
        assert "CPU" in texts
        assert "预计下载" in texts
        assert step.can_proceed()

    def test_delayed_transitions_do_not_raise(self) -> None:
        """回归：``_delay`` 曾用 PyQt6 不存在的 singleShot 重载，实机一点就崩。

        测试默认 ``simulate_delays=False`` 走同步分支，掩盖了这条路径。
        """
        backend = MockSeparationBackend({"install_dir": "D:/demo/pymss"})
        assert backend.snapshot().state == ServiceState.INSTALLED_STOPPED
        backend.start_service()  # 走 _delay 分支，不得抛异常
        assert backend.snapshot().state == ServiceState.SERVICE_STARTING

    def test_msst_mapping_only_binds_verified_candidates(self) -> None:
        page = _make_separation_page()
        page._welcome.flowSelected.emit(FLOW_REUSE_MSST)
        step = page._wizard._steps[3]
        step._root_edit.setText("D:/MSST-WebUI")

        step._scan()

        assert step._combos[TaskType.VOCAL].isEnabled()
        assert step._combos[TaskType.VOCAL].currentData() == "msst:big_beta5e"
        assert not step._combos[TaskType.INSTRUMENTAL].isEnabled()
        assert not step._combos[TaskType.HARMONY].isEnabled()

        step.on_primary()
        assert page._backend._external_bindings == {
            TaskType.VOCAL: "msst:big_beta5e"
        }

    def test_separation_page_busy_and_shutdown_contract(self) -> None:
        settings = AppSettings()
        settings.pymss["install_dir"] = "D:/demo/pymss"
        page = _make_separation_page(settings)
        assert not page.is_busy()

        page._backend.start_service()
        assert not page.is_busy()
        page._backend._set_state(ServiceState.PROCESSING)
        assert page.is_busy()
        assert page.shutdown(timeout_ms=50)
        assert page._backend.snapshot().state == ServiceState.INSTALLED_STOPPED


class TestAcceptanceCriteria:
    """逐条覆盖需求文档 §14.1「页面与原功能」验收标准。"""

    def test_workflow_step2_is_renamed(self) -> None:
        """§14.1：顶部第 2 步显示「音视频处理」，模块 ID 不变。"""
        from krok_helper.gui_qt import WORKFLOW_WAVEFORM_ALIGN, WORKFLOW_STEPS

        step2 = next(s for s in WORKFLOW_STEPS if s.number == 2)
        assert step2.title == "音视频处理"
        assert step2.description == "波形对齐与音频分离"
        assert step2.module_id == WORKFLOW_WAVEFORM_ALIGN

    def test_welcome_page_offers_three_entries(self) -> None:
        """§14.1：首次配置页明确提供三个入口。"""
        page = _make_separation_page()
        assert page.current_view() == "welcome"
        titles = [card._title.text() for card in page._welcome._cards]
        assert titles == [
            "安装 PyMSS 和推荐模型",
            "仅安装 PyMSS，复用 MSST 模型",
            "使用已有 PyMSS",
        ]

    def test_state_changes_preserve_inputs_and_results(self, tmp_path) -> None:
        """§14.1：服务状态变化与安装损坏修复不得清空输入、输出目录和历史结果。"""
        audio = tmp_path / "示例歌曲.wav"
        audio.write_bytes(b"RIFF")
        out_dir = tmp_path / "out"
        out_dir.mkdir()

        settings = AppSettings()
        settings.pymss["install_dir"] = "D:/demo/pymss"
        page = _make_separation_page(settings)
        backend = page._backend

        page._input_card.set_path(str(audio))
        page._output_card.set_output_dir(str(out_dir))
        page._results_panel.add_result(
            TaskResult(
                task=TaskType.VOCAL,
                title="分离人声",
                finished_at="12:00:00",
                files=[ResultFile(path=str(out_dir / "歌_人声.wav"), label="人声", size_bytes=1)],
            )
        )

        # 服务启停
        backend.start_service()
        backend.stop_service()
        backend.start_service()
        # 安装损坏 → 修复
        backend._set_state(ServiceState.INSTALL_DAMAGED)
        assert page.current_view() == "workspace"
        backend.repair_install()

        assert page._input_card.path() == str(audio)
        assert page._output_card.output_dir() == str(out_dir)
        assert page._results_panel.group_count() == 1

    def test_downloaded_models_persist_but_install_alone_does_not(self) -> None:
        """§8.4：安装底座不附带模型；已下载的模型在重建后仍然有效。"""
        ns: dict = {}
        first = MockSeparationBackend(ns, simulate_delays=False)
        first.start_wizard(FLOW_FULL)
        first.confirm_install_location("D:/demo/pymss")
        first.start_install()
        first.start_service()

        # 刚装完底座：三个任务都还缺模型。
        deps = first.snapshot().dependencies
        assert all(not d.ready for d in deps.values())
        assert "需下载" in deps[TaskType.VOCAL].badge

        first.request_task(
            TaskType.VOCAL, input_path="D:/a.wav", output_dir="D:/out", output_format="wav"
        )
        first.start_model_download()

        # 重建后端（模拟重启）：只有下过的人声模型保留为就绪。
        second = MockSeparationBackend(ns, simulate_delays=False)
        second.start_service()
        deps2 = second.snapshot().dependencies
        assert deps2[TaskType.VOCAL].ready
        assert not deps2[TaskType.INSTRUMENTAL].ready
        assert not deps2[TaskType.HARMONY].ready

    def test_workspace_grids_collapse_to_single_column(self) -> None:
        """§14.1：标准宽度双列素材区 + 三任务横排；窄窗口单列。"""
        settings = AppSettings()
        settings.pymss["install_dir"] = "D:/demo/pymss"
        page = _make_separation_page(settings)

        page._materials_grid.resize(1200, 400)
        page._materials_grid._relayout()
        page._tasks_grid.resize(1200, 400)
        page._tasks_grid._relayout()
        assert page._materials_grid.column_count() == 2
        assert page._tasks_grid.column_count() == 3

        page._materials_grid.resize(420, 400)
        page._materials_grid._relayout()
        page._tasks_grid.resize(420, 400)
        page._tasks_grid._relayout()
        assert page._materials_grid.column_count() == 1
        assert page._tasks_grid.column_count() == 1

    def test_workspace_has_no_horizontal_scrollbar(self) -> None:
        """§3.7 / §14.1：页面统一纵向滚动，不出现横向滚动条。"""
        from PyQt6.QtCore import Qt

        settings = AppSettings()
        settings.pymss["install_dir"] = "D:/demo/pymss"
        page = _make_separation_page(settings)
        assert (
            page._workspace.horizontalScrollBarPolicy()
            == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )


class TestCardHeightStability:
    """卡片等高与跨状态高度稳定（避免状态切换时卡片忽高忽低）。"""

    _GB = 1024 ** 3

    #: 覆盖 TaskCard.set_dependency 的全部分支，徽标与原因文案各不相同。
    _SCENARIOS = [
        ("服务未启动", {"service_ready": False}, (False, "", "", int(1.38 * _GB), False)),
        ("缺模型需下载", {"service_ready": True}, (False, "需下载 1.38 GB", "推荐模型尚未下载", int(1.38 * _GB), False)),
        ("已就绪", {"service_ready": True}, (True, "就绪", "", 0, False)),
        ("外部模型可用", {"service_ready": True}, (True, "外部模型", "", 0, True)),
        ("外部模型失效", {"service_ready": True}, (False, "不可用", "所选外部模型路径失效", 0, False)),
        ("任务进行中", {"service_ready": True, "unavailable_reason": "当前任务进行中"}, (True, "处理中", "", 0, False)),
        ("需先处理错误", {"service_ready": True, "unavailable_reason": "请先处理上方错误"}, (False, "不可用", "", 0, False)),
    ]

    def _build(self):
        from krok_helper.audio_processing.separation.widgets import (
            AudioInputCard,
            OutputSettingsCard,
            TaskCard,
        )

        host = QWidget()
        host.resize(1460, 900)

        materials = ResponsiveGrid(min_column_width=360, max_columns=2, parent=host)
        input_card, output_card = AudioInputCard(host), OutputSettingsCard(host)
        materials.set_widgets([input_card, output_card])
        materials.resize(1420, 300)
        materials._relayout()

        tasks = ResponsiveGrid(min_column_width=260, max_columns=3, parent=host)
        cards = {t: TaskCard(t, host) for t in TaskType}
        tasks.set_widgets(list(cards.values()))
        tasks.resize(1420, 300)
        tasks._relayout()
        return host, (input_card, output_card), cards, materials, tasks

    def test_material_and_output_cards_are_equal_height(self) -> None:
        host, (input_card, output_card), _, materials, _ = self._build()
        host.show()
        QApplication.instance().processEvents()
        materials._relayout()
        QApplication.instance().processEvents()
        assert input_card.height() == output_card.height()

    def test_task_cards_equal_height_and_stable_across_states(self) -> None:
        from krok_helper.audio_processing.separation.states import TaskDependency

        host, _, cards, _, tasks = self._build()
        host.show()
        app = QApplication.instance()
        app.processEvents()

        seen_heights: set[int] = set()
        seen_reasons: set[str] = set()
        for _label, kwargs, (ready, badge, reason, size, external) in self._SCENARIOS:
            for task, card in cards.items():
                card.set_dependency(
                    TaskDependency(task, ready, badge, reason, size, external),
                    **kwargs,
                )
            tasks._relayout()
            app.processEvents()

            heights = [c.height() for c in cards.values()]
            assert len(set(heights)) == 1, f"{_label}: 同行三卡不等高 {heights}"
            seen_heights.add(heights[0])
            seen_reasons.add(cards[TaskType.VOCAL]._reason.text())

        # 场景确实各不相同（否则「高度稳定」是假通过）。
        assert len(seen_reasons) > 1, "各场景原因文案没有区别，测试无效"
        assert len(seen_heights) == 1, f"任务卡高度随状态变化: {sorted(seen_heights)}"

    def test_reason_label_always_reserves_space(self) -> None:
        """原因行常驻：为空时也占位，否则切换状态会让卡片跳动。"""
        from krok_helper.audio_processing.separation.states import TaskDependency
        from krok_helper.audio_processing.separation.widgets import TaskCard

        card = TaskCard(TaskType.VOCAL)
        card.set_dependency(
            TaskDependency(TaskType.VOCAL, True, "就绪", ""), service_ready=True
        )
        assert card._reason.text() == ""
        assert not card._reason.isHidden(), "原因行不应被隐藏，否则卡片高度会跳"
        assert card._reason.minimumHeight() > 0


class TestTaskModelSelection:
    """设置里按任务自选模型与输出轨（输出轨只能从模型真实声明的名字里选）。"""

    KARAOKE = "model_mel_band_roformer_karaoke_aufr33_viperx_sdr_10.1956"

    def _dialog(self, settings: AppSettings, backend):
        from krok_helper.audio_processing.separation.settings_dialog import (
            SeparationSettingsDialog,
        )

        return SeparationSettingsDialog(backend, settings.pymss, lambda: None)

    def test_override_changes_effective_steps(self) -> None:
        from krok_helper.audio_processing.separation.presets import (
            TASK_PRESETS,
            effective_steps,
        )

        ns: dict = {}
        assert effective_steps(ns, TaskType.VOCAL) == TASK_PRESETS[TaskType.VOCAL].steps

        ns["task_model_overrides"] = {
            "vocal": {"model": self.KARAOKE, "stem": "karaoke", "size_bytes": 913_096_801}
        }
        steps = effective_steps(ns, TaskType.VOCAL)
        assert len(steps) == 1
        assert steps[0].model == self.KARAOKE
        assert steps[0].stems == ("karaoke",)
        # 输出文件名仍沿用任务自己的中文标签。
        assert steps[0].output_labels == ("人声",)

    def test_incomplete_override_is_ignored(self) -> None:
        """只有模型没有输出轨的记录不能拿来跑任务，必须退回推荐预设。"""
        from krok_helper.audio_processing.separation.presets import (
            TASK_PRESETS,
            effective_steps,
            task_override,
        )

        for broken in ({"model": self.KARAOKE}, {"stem": "karaoke"}, {}, "nonsense"):
            ns = {"task_model_overrides": {"vocal": broken}}
            assert task_override(ns, TaskType.VOCAL) is None
            assert effective_steps(ns, TaskType.VOCAL) == TASK_PRESETS[TaskType.VOCAL].steps

    def test_backend_records_and_clears_override(self) -> None:
        settings = AppSettings()
        backend = MockSeparationBackend(settings.pymss, simulate_delays=False)
        backend.set_task_model(TaskType.VOCAL, self.KARAOKE, "karaoke", 913_096_801)
        assert settings.pymss["task_model_overrides"]["vocal"]["stem"] == "karaoke"

        backend.set_task_model(TaskType.VOCAL, "", "", 0)
        assert "vocal" not in settings.pymss["task_model_overrides"]

    def test_settings_page_shows_recommended_then_custom(self) -> None:
        settings = AppSettings()
        backend = MockSeparationBackend(settings.pymss, simulate_delays=False)
        dialog = self._dialog(settings, backend)

        card, reset = dialog._model_cards[TaskType.VOCAL]
        assert "推荐模型" in card.contentLabel.text()
        assert "inst_v1e" in card.contentLabel.text()
        assert not reset.isEnabled(), "没有自定义时「恢复推荐」应不可用"

        backend.set_task_model(TaskType.VOCAL, self.KARAOKE, "karaoke", 913_096_801)
        dialog._refresh_model_cards()
        card, reset = dialog._model_cards[TaskType.VOCAL]
        assert "自定义" in card.contentLabel.text()
        assert "karaoke" in card.contentLabel.text()
        assert reset.isEnabled()

    def test_picker_offers_only_real_stems_and_blocks_until_chosen(self) -> None:
        """核心诉求：输出轨是选出来的，不是填出来的。"""
        from krok_helper.audio_processing.separation.settings_dialog import (
            ModelPickerDialog,
        )

        settings = AppSettings()
        backend = MockSeparationBackend(settings.pymss, simulate_delays=False)
        picker = ModelPickerDialog(backend, TaskType.HARMONY, "", "")
        QApplication.instance().processEvents()

        assert picker._list.count() > 0
        assert not picker._ok.isEnabled(), "未选模型时不能确定"

        # 选中 karaoke 模型 → 下拉里只出现它自己声明的两个轨
        for index in range(picker._list.count()):
            if picker._list.item(index).data(Qt.ItemDataRole.UserRole) == self.KARAOKE:
                picker._list.setCurrentRow(index)
                break
        QApplication.instance().processEvents()

        stems = [picker._stem_combo.itemText(i) for i in range(picker._stem_combo.count())]
        assert stems == ["karaoke", "other"], f"应只列出模型真实的输出轨，实际 {stems}"
        assert "vocals" not in stems, "不能出现该模型没有的轨名"
        assert picker._ok.isEnabled()
        assert picker.selection()[0] == self.KARAOKE

    def test_picker_refuses_when_stems_cannot_be_read(self) -> None:
        """读不出输出轨时必须挡住，而不是让用户手填。"""
        from krok_helper.audio_processing.separation.settings_dialog import (
            ModelPickerDialog,
        )

        settings = AppSettings()
        backend = MockSeparationBackend(settings.pymss, simulate_delays=False)
        picker = ModelPickerDialog(backend, TaskType.VOCAL, "", "")
        QApplication.instance().processEvents()

        picker._selected_model = "unknown-model"
        backend.request_model_stems("unknown-model")
        QApplication.instance().processEvents()

        assert picker._stem_combo.count() == 0
        assert not picker._stem_combo.isEnabled()
        assert not picker._ok.isEnabled()
        assert "换一个模型" in picker._hint.text()


class TestLocalModelImportDialog:
    """从文件导入模型的界面流程。"""

    def _dialog(self, tmp_path, *, with_config=True):
        from krok_helper.audio_processing.separation.settings_dialog import (
            LocalModelImportDialog,
        )

        settings = AppSettings()
        backend = MockSeparationBackend(settings.pymss, simulate_delays=False)
        dialog = LocalModelImportDialog(backend, TaskType.HARMONY)

        folder = tmp_path / "models"
        folder.mkdir(parents=True, exist_ok=True)
        weight = folder / "third_party.ckpt"
        weight.write_bytes(b"w" * 128)
        if with_config:
            (folder / "third_party.yaml").write_text(
                "training:\n  instruments:\n  - karaoke\n  - other\n", encoding="utf-8"
            )
        return dialog, backend, settings, weight

    def test_blocks_until_a_weight_is_chosen(self, tmp_path) -> None:
        dialog, _backend, _settings, _weight = self._dialog(tmp_path)
        assert not dialog._ok.isEnabled()

    def test_shows_declared_stems_and_enables_import(self, tmp_path) -> None:
        dialog, _backend, _settings, weight = self._dialog(tmp_path)
        dialog._weight_edit.setText(str(weight))
        dialog._config_edit.setText(str(weight.with_suffix(".yaml")))
        dialog._refresh()

        assert "karaoke" in dialog._stems_label.text()
        assert "other" in dialog._stems_label.text()
        assert dialog._ok.isEnabled()

    def test_config_required_arch_without_config_is_blocked(self, tmp_path) -> None:
        """缺配置时不能导入——读不出输出轨就无法确定模型产出什么。"""
        dialog, _backend, _settings, weight = self._dialog(tmp_path, with_config=False)
        dialog._weight_edit.setText(str(weight))
        dialog._config_edit.setText("")
        dialog._type_combo.setCurrentText("mel_band_roformer")
        dialog._refresh()

        assert not dialog._ok.isEnabled()
        assert "需要 YAML 配置" in dialog._hint.text()

    def test_import_binds_the_task(self, tmp_path) -> None:
        dialog, backend, _settings, weight = self._dialog(tmp_path)
        dialog._weight_edit.setText(str(weight))
        dialog._config_edit.setText(str(weight.with_suffix(".yaml")))
        dialog._type_combo.setCurrentText("mel_band_roformer")
        dialog._refresh()

        done: list = []
        backend.localImportFinished.connect(done.append)
        dialog._submit()
        QApplication.instance().processEvents()

        assert done, "导入成功应发出 localImportFinished"
        assert done[0].candidate_id.startswith("local:harmony:")
        assert backend.bound_external_candidate(TaskType.HARMONY) == done[0].candidate_id

    def test_failure_is_surfaced_and_button_restored(self, tmp_path) -> None:
        dialog, backend, _settings, weight = self._dialog(tmp_path)
        dialog._weight_edit.setText(str(weight))
        dialog._config_edit.setText(str(weight.with_suffix(".yaml")))
        dialog._refresh()

        backend.localImportFailed.emit("模拟失败原因")
        QApplication.instance().processEvents()
        assert dialog._hint.text() == "模拟失败原因"
        assert dialog._ok.isEnabled(), "失败后应允许改参数重试"


class TestFolderImportDialog:
    """一键导入文件夹的界面流程。"""

    def _fixture(self, tmp_path):
        import json as _json

        from krok_helper.audio_processing.separation.settings_dialog import (
            FolderImportDialog,
        )

        root = tmp_path / "MSST"
        (root / "data").mkdir(parents=True)
        (root / "configs").mkdir(parents=True)
        (root / "pretrain" / "vocal_models").mkdir(parents=True)
        (root / "configs" / "inst.yaml").write_text(
            "training:\n  instruments:\n  - other\n  - vocals\n", encoding="utf-8"
        )
        (root / "configs" / "kara.yaml").write_text(
            "training:\n  instruments:\n  - karaoke\n  - other\n", encoding="utf-8"
        )
        (root / "pretrain" / "vocal_models" / "inst_v1e.ckpt").write_bytes(b"w" * 64)
        (root / "pretrain" / "vocal_models" / "mel_band_roformer_karaoke_x.ckpt").write_bytes(
            b"w" * 64
        )
        (root / "data" / "msst_model_map.json").write_text(
            _json.dumps(
                {
                    "vocal_models": [
                        {
                            "name": "inst_v1e.ckpt",
                            "model_type": "mel_band_roformer",
                            "config_path": "configs/inst.yaml",
                        },
                        {
                            "name": "mel_band_roformer_karaoke_x.ckpt",
                            "model_type": "mel_band_roformer",
                            "config_path": "configs/kara.yaml",
                        },
                    ]
                }
            ),
            encoding="utf-8",
        )

        settings = AppSettings()
        backend = MockSeparationBackend(settings.pymss, simulate_delays=False)
        return FolderImportDialog(backend), backend, root

    def test_blocks_before_scanning(self, tmp_path) -> None:
        dialog, _backend, _root = self._fixture(tmp_path)
        assert not dialog._ok.isEnabled()

    def test_scan_fills_every_task_row(self, tmp_path) -> None:
        dialog, backend, root = self._fixture(tmp_path)
        backend.start_folder_scan(str(root / "pretrain"))
        QApplication.instance().processEvents()

        vocal_card, vocal_combo = dialog._rows[TaskType.VOCAL]
        harmony_card, _ = dialog._rows[TaskType.HARMONY]
        assert "inst_v1e" in vocal_card.contentLabel.text()
        assert "karaoke" in harmony_card.contentLabel.text()
        assert vocal_combo.isEnabled()
        assert vocal_combo.itemText(0) == "不绑定", "必须允许某个任务不绑定"
        assert dialog._ok.isEnabled()

    def test_selection_skips_unbound_rows(self, tmp_path) -> None:
        dialog, backend, root = self._fixture(tmp_path)
        backend.start_folder_scan(str(root / "pretrain"))
        QApplication.instance().processEvents()

        dialog._rows[TaskType.INSTRUMENTAL][1].setCurrentIndex(0)  # 不绑定
        chosen = dialog.selection()
        assert TaskType.INSTRUMENTAL not in chosen
        assert TaskType.VOCAL in chosen

    def test_apply_binds_selected_tasks(self, tmp_path) -> None:
        dialog, backend, root = self._fixture(tmp_path)
        backend.start_folder_scan(str(root / "pretrain"))
        QApplication.instance().processEvents()

        dialog._apply()
        QApplication.instance().processEvents()
        assert backend.bound_external_candidate(TaskType.VOCAL)
        assert backend.bound_external_candidate(TaskType.HARMONY)

    def test_scan_failure_is_surfaced(self, tmp_path) -> None:
        dialog, backend, _root = self._fixture(tmp_path)
        backend.start_folder_scan(str(tmp_path / "does-not-exist"))
        QApplication.instance().processEvents()
        assert "不存在" in dialog._hint.text()
        assert not dialog._ok.isEnabled()
