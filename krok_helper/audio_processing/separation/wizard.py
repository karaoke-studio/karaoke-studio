"""首次配置与迁移向导（需求文档 §3.5）。

三个入口：
- 安装 PyMSS 和推荐模型（FLOW_FULL）
- 仅安装 PyMSS，复用 MSST 模型（FLOW_REUSE_MSST）
- 使用已有 PyMSS（FLOW_EXISTING）

向导在当前 Tab 内以步骤页展示：顶部步骤指示、返回、取消与当前步骤主按钮；
取消后回到首次配置页，并通过 ``backend.cleanup_incomplete()`` 清理半成品。
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLineEdit,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    FluentIcon as FIF,
    IconWidget,
    LineEdit,
    PrimaryPushButton,
    ProgressBar,
    PushButton,
    StrongBodyLabel,
    SubtitleLabel,
    TitleLabel,
    ToolButton,
)

from krok_helper.audio_processing.separation.backend import (
    ExternalModelCandidate,
    FLOW_EXISTING,
    FLOW_FULL,
    FLOW_REMAP_MSST,
    FLOW_REUSE_MSST,
    FLOW_UPGRADE,
    SeparationBackend,
)
from krok_helper.audio_processing.separation.states import (
    TASK_SPECS,
    ServiceState,
    TaskType,
    format_size,
)
from krok_helper.audio_processing.separation.widgets import (
    CardWidget,
    HintBox,
    InfoGrid,
    OptionCard,
    PillLabel,
    StateLevel,
    WizardStepper,
)

#: 向导内容列最大宽度：阅读型页面限宽居中，避免宽屏下每行过长。
WIZARD_COLUMN_MAX_WIDTH = 780


def default_install_root() -> Path:
    """「软件根目录」默认安装位置（需求文档 §4.2）：冻结后为 exe 所在目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path.cwd()


class EntryCard(CardWidget):
    """欢迎页入口卡。"""

    clicked = pyqtSignal()

    def __init__(self, icon: FIF, title: str, description: str, parent: QWidget | None = None) -> None:
        super().__init__(parent, padding=(18, 16, 18, 16), spacing=6)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        layout = self.createVBoxLayout()

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(12)
        icon_widget = IconWidget(icon.icon(), self)
        icon_widget.setFixedSize(30, 30)
        header.addWidget(icon_widget, 0, Qt.AlignmentFlag.AlignVCenter)
        self._title = StrongBodyLabel(title, self)
        header.addWidget(self._title, 0, Qt.AlignmentFlag.AlignVCenter)
        header.addStretch(1)
        arrow = IconWidget(FIF.CHEVRON_RIGHT_MED.icon(), self)
        arrow.setFixedSize(16, 16)
        header.addWidget(arrow, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addLayout(header)

        desc = BodyLabel(description, self)
        desc.setWordWrap(True)
        layout.addWidget(desc)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.pos()):
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class WelcomeView(QWidget):
    """UNCONFIGURED 状态的首次配置页（需求文档 §3.5）。"""

    flowSelected = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # 与向导页一致的限宽居中列，避免入口卡在宽屏上拉成长条。
        outer = QHBoxLayout(self)
        outer.setContentsMargins(24, 32, 24, 24)
        outer.addStretch(1)
        column = QWidget(self)
        column.setMaximumWidth(WIZARD_COLUMN_MAX_WIDTH)
        outer.addWidget(column, 1)
        outer.addStretch(1)

        layout = QVBoxLayout(column)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        icon = IconWidget(FIF.MIX_VOLUMES.icon(), column)
        icon.setFixedSize(40, 40)
        layout.addWidget(icon, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(TitleLabel("音频分离（PyMSS）", column), 0, Qt.AlignmentFlag.AlignHCenter)
        intro = BodyLabel(
            "把歌曲拆成人声、伴奏与和声。首次使用请先选择一种方式：",
            column,
        )
        intro.setWordWrap(True)
        intro.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(intro)
        layout.addSpacing(14)

        entries = (
            EntryCard(
                FIF.CLOUD_DOWNLOAD,
                "安装 PyMSS 和推荐模型",
                "首次使用。由工作台下载并管理 PyMSS Runtime。",
                self,
            ),
            EntryCard(
                FIF.LIBRARY,
                "仅安装 PyMSS，复用 MSST 模型",
                "已经在用 MSST-WebUI，想原地复用已下载的模型。",
                self,
            ),
            EntryCard(
                FIF.LINK,
                "使用已有 PyMSS",
                "电脑上已有兼容环境，或服务已在运行。",
                self,
            ),
        )
        flows = (FLOW_FULL, FLOW_REUSE_MSST, FLOW_EXISTING)
        self._cards: list[EntryCard] = []
        for card, flow in zip(entries, flows):
            card.clicked.connect(lambda f=flow: self.flowSelected.emit(f))
            layout.addWidget(card)
            self._cards.append(card)
        layout.addStretch(1)


class WizardStep(QWidget):
    """向导步骤基类。"""

    title = ""
    #: 步骤指示器上的短标签（留空则回退到 ``title``）。
    step_label = ""
    #: 标题下方的一句话说明；留空则不占位。
    hint = ""
    primary_label = "下一步"

    def __init__(self, wizard: "WizardView") -> None:
        super().__init__(wizard)
        self.wizard = wizard

    @property
    def backend(self) -> SeparationBackend:
        return self.wizard.backend

    def on_enter(self) -> None:
        """进入该步骤时刷新内容。"""

    def can_proceed(self) -> bool:
        return True

    def on_primary(self) -> bool:
        """主按钮点击；返回 True 表示进入下一步。"""
        return True


class InstallLocationStep(WizardStep):
    """安装位置选择（需求文档 §4.2）。"""

    title = "选择安装位置"
    step_label = "安装位置"
    primary_label = "下一步"

    def __init__(self, wizard: "WizardView") -> None:
        super().__init__(wizard)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.setSpacing(10)

        self._root_option = OptionCard(
            "软件根目录（推荐）",
            f"安装到 {default_install_root() / 'pymss'}",
            self,
        )
        self._custom_option = OptionCard(
            "其他目录",
            "自行指定，仍会创建独立的 pymss 子目录。",
            self,
        )
        self._root_option.selected.connect(lambda: self._select(root=True))
        self._custom_option.selected.connect(lambda: self._select(root=False))
        layout.addWidget(self._root_option)
        layout.addWidget(self._custom_option)

        self._path_row = QWidget(self)
        path_row = QHBoxLayout(self._path_row)
        path_row.setContentsMargins(30, 0, 0, 0)
        path_row.setSpacing(8)
        self._path_edit = LineEdit(self._path_row)
        self._path_edit.setReadOnly(True)
        self._path_edit.setPlaceholderText("尚未选择目录")
        path_row.addWidget(self._path_edit, 1)
        self._browse_button = PushButton(FIF.FOLDER, "浏览", self._path_row)
        self._browse_button.clicked.connect(self._browse)
        path_row.addWidget(self._browse_button)
        self._path_row.setVisible(False)
        layout.addWidget(self._path_row)

        layout.addSpacing(2)
        self._info = InfoGrid(self)
        layout.addWidget(self._info)

        layout.addWidget(
            HintBox(
                [
                    "模型按需下载：首次执行某任务时才获取它需要的权重。",
                    "非 NVIDIA 显卡可能只能用 CPU，速度较慢。",
                    "工作台自动更新不会删除该目录。",
                ],
                self,
            )
        )
        layout.addStretch(1)

        self._root_option.set_checked(True)
        self._refresh()

    def install_path(self) -> str:
        return self._path_edit.text().strip()

    def _select(self, *, root: bool) -> None:
        self._root_option.set_checked(root)
        self._custom_option.set_checked(not root)
        self._refresh()

    def _refresh(self) -> None:
        use_root = self._root_option.is_checked()
        self._path_row.setVisible(not use_root)
        if use_root:
            self._path_edit.setText(str(default_install_root() / "pymss"))
        else:
            current = self._path_edit.text().strip()
            default = str(default_install_root() / "pymss")
            if not current or current == default:
                self._path_edit.setText("")
        self._refresh_info()
        self.wizard.refresh_footer()

    def _refresh_info(self) -> None:
        path = self.install_path()
        if not path:
            self._info.set_rows([("最终路径", "请选择安装目录")])
            return
        try:
            usage = shutil.disk_usage(Path(path).anchor or path)
            free = format_size(usage.free)
        except Exception:
            free = "未知"
        self._info.set_rows(
            [
                ("最终路径", path),
                ("可用磁盘空间", free),
                ("预计下载", "CPU 约数百 MB；NVIDIA CUDA 约 3–4 GB"),
                ("解压后占用", "根据设备与依赖版本约 1–7 GB"),
            ]
        )

    def _browse(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "选择安装目录")
        if directory:
            self._path_edit.setText(str(Path(directory) / "pymss"))
        self._refresh_info()
        self.wizard.refresh_footer()

    def can_proceed(self) -> bool:
        return bool(self.install_path())

    def on_primary(self) -> bool:
        self.backend.confirm_install_location(self.install_path())
        return True


class ConfirmStep(WizardStep):
    """下载前确认（需求文档 §3.5：大型下载前展示体积与说明）。"""

    title = "确认安装信息"
    step_label = "确认"
    primary_label = "开始下载"

    def __init__(self, wizard: "WizardView") -> None:
        super().__init__(wizard)
        self._choice_ready = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.setSpacing(12)
        self._summary = InfoGrid(self)
        layout.addWidget(self._summary)
        layout.addWidget(
            HintBox(
                ["下载后自动校验；校验失败不会覆盖现有安装。"],
                self,
            )
        )
        layout.addStretch(1)

    def on_enter(self) -> None:
        snap = self.backend.snapshot()
        reuse = self.wizard.flow == FLOW_REUSE_MSST
        try:
            device_label, download_bytes = self.backend.prepare_install_choice()
            self._choice_ready = True
            choice_rows = [
                ("设备方案", device_label),
                ("预计下载", f"约 {format_size(download_bytes)}"),
            ]
        except Exception as exc:
            self._choice_ready = False
            choice_rows = [
                ("设备方案", "检测失败"),
                ("无法继续", str(exc)),
            ]
        rows = [
            ("安装目录", snap.install_dir),
            ("安装内容", "PyMSS 托管 Runtime（独立 Python，不影响系统环境）"),
            *choice_rows,
        ]
        if reuse:
            rows.append(("模型", "稍后扫描并复用已有 MSST 模型"))
        else:
            rows.append(("模型", "按需下载，此步骤不下载"))
        self._summary.set_rows(rows)

    def can_proceed(self) -> bool:
        return self._choice_ready

    def on_primary(self) -> bool:
        self.backend.start_install()
        return True


class ProgressStep(WizardStep):
    """下载与校验进度（RUNTIME_DOWNLOADING / RUNTIME_VERIFYING）。"""

    title = "下载与安装"
    step_label = "下载安装"
    hint = "正在下载并安装音频分离组件。完成后会自动进入下一步。"
    primary_label = "正在安装…"

    def __init__(self, wizard: "WizardView") -> None:
        super().__init__(wizard)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(8)
        self._bar = ProgressBar(self)
        self._bar.setRange(0, 1000)
        layout.addWidget(self._bar)
        self._status = CaptionLabel("准备下载…", self)
        self._status.setWordWrap(True)
        layout.addWidget(self._status)
        hint = CaptionLabel(
            "下载完成后还需要解压、安装和校验，可能需要几分钟；"
            "点击底部「取消」可中止安装。",
            self,
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)
        layout.addStretch(1)

    def on_enter(self) -> None:
        self._apply(self.backend.snapshot())

    def can_proceed(self) -> bool:
        return False  # 安装完成后由向导自动前进

    def apply_snapshot(self, snapshot) -> None:
        self._apply(snapshot)

    def _apply(self, snapshot) -> None:
        if snapshot.state == ServiceState.RUNTIME_DOWNLOADING and snapshot.download_total:
            ratio = snapshot.download_done / snapshot.download_total
            self._bar.setValue(int(ratio * 1000))
            if snapshot.download_done >= snapshot.download_total:
                self._status.setText(
                    "下载完成，正在解压并安装音频分离组件，请耐心等待…"
                )
            else:
                self._status.setText(
                    f"正在下载音频分离组件：{format_size(snapshot.download_done)} / "
                    f"{format_size(snapshot.download_total)}"
                )
        elif snapshot.state == ServiceState.RUNTIME_VERIFYING:
            self._bar.setValue(1000)
            self._status.setText("正在校验安装并启动服务检查…")
        elif snapshot.state == ServiceState.LOCATION_REQUIRED:
            self._status.setText("下载已取消。")
        elif snapshot.state == ServiceState.ERROR:
            self._bar.setValue(0)
            self._status.setText(f"安装失败：{snapshot.error or '请检查日志后重试。'}")


class MsstMappingStep(WizardStep):
    """扫描旧 MSST 目录并按任务映射模型（需求文档 §3.5 / §4.5）。"""

    title = "复用 MSST 模型"
    step_label = "复用模型"
    hint = "原地引用旧 MSST 目录中的权重，不复制、不移动、不修改任何文件。"
    primary_label = "完成映射"

    def __init__(self, wizard: "WizardView") -> None:
        super().__init__(wizard)
        self._candidates: list[ExternalModelCandidate] = []
        self._scanning = False
        self._combos: dict[TaskType, QComboBox] = {}
        self._status_pills: dict[TaskType, PillLabel] = {}
        self._detail_labels: dict[TaskType, CaptionLabel] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(8)

        root_row = QHBoxLayout()
        root_row.setContentsMargins(0, 0, 0, 0)
        root_row.setSpacing(8)
        self._root_edit = LineEdit(self)
        self._root_edit.setPlaceholderText("选择 MSST-WebUI 根目录")
        root_row.addWidget(self._root_edit, 1)
        browse = PushButton(FIF.FOLDER, "浏览", self)
        browse.clicked.connect(self._browse)
        root_row.addWidget(browse)
        self._scan_button = PrimaryPushButton(FIF.SEARCH, "开始扫描", self)
        self._scan_button.clicked.connect(self._scan)
        root_row.addWidget(self._scan_button)
        layout.addLayout(root_row)

        self._rows_host = QVBoxLayout()
        self._rows_host.setContentsMargins(0, 4, 0, 0)
        self._rows_host.setSpacing(6)
        layout.addLayout(self._rows_host)

        for task, spec in TASK_SPECS.items():
            row_card = CardWidget(self, padding=(12, 10, 12, 10), spacing=6)
            row_layout = row_card.createVBoxLayout()
            head = QHBoxLayout()
            head.setContentsMargins(0, 0, 0, 0)
            head.setSpacing(8)
            head.addWidget(StrongBodyLabel(spec.title, row_card))
            pill = PillLabel(row_card)
            pill.set_state("未扫描", StateLevel.INFO)
            head.addWidget(pill)
            head.addStretch(1)
            details_button = ToolButton(FIF.CARE_DOWN_SOLID, row_card)
            details_button.setToolTip("查看详情")
            head.addWidget(details_button)
            row_layout.addLayout(head)

            combo = QComboBox(row_card)
            combo.setEnabled(False)
            combo.currentIndexChanged.connect(
                lambda _index, t=task: self._update_candidate_detail(t)
            )
            row_layout.addWidget(combo)

            detail = CaptionLabel("", row_card)
            detail.setWordWrap(True)
            detail.setVisible(False)
            row_layout.addWidget(detail)

            details_button.clicked.connect(
                lambda _c=False, d=detail, b=details_button: self._toggle_detail(d, b)
            )
            self._rows_host.addWidget(row_card)
            self._combos[task] = combo
            self._status_pills[task] = pill
            self._detail_labels[task] = detail

        note = CaptionLabel(
            "三个任务不必一次全部映射；未映射的任务以后按需下载推荐模型即可。", self
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addStretch(1)

        self.backend.msstScanStarted.connect(self._on_scan_started)
        self.backend.msstScanFinished.connect(self._on_scan_finished)
        self.backend.msstScanFailed.connect(self._on_scan_failed)

    def on_enter(self) -> None:
        root = self.backend.suggested_msst_root().strip()
        if root and not self._root_edit.text().strip():
            self._root_edit.setText(root)
            self._scan()

    def _toggle_detail(self, label: CaptionLabel, button: ToolButton) -> None:
        show = not label.isVisible()
        label.setVisible(show)
        button.setIcon(FIF.CARE_UP_SOLID.icon() if show else FIF.CARE_DOWN_SOLID.icon())

    def _browse(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "选择 MSST-WebUI 根目录")
        if directory:
            self._root_edit.setText(directory)

    def _scan(self) -> None:
        root = self._root_edit.text().strip()
        if not root:
            return
        self.backend.start_msst_scan(root)

    def _on_scan_started(self) -> None:
        self._scanning = True
        self._scan_button.setEnabled(False)
        self._scan_button.setText("扫描中…")
        for task, combo in self._combos.items():
            combo.clear()
            combo.setEnabled(False)
            self._status_pills[task].set_state("扫描中", StateLevel.BUSY)
            self._detail_labels[task].setText("")
        self.wizard.refresh_footer()

    def _on_scan_finished(self, candidates) -> None:
        self._scanning = False
        self._scan_button.setEnabled(True)
        self._scan_button.setText("重新扫描")
        self._candidates = [
            candidate
            for candidate in candidates
            if isinstance(candidate, ExternalModelCandidate)
        ]
        for task, combo in self._combos.items():
            combo.clear()
            task_candidates = [c for c in self._candidates if c.task is task]
            bindable = [candidate for candidate in task_candidates if candidate.bindable]
            combo.addItem("不映射（以后按需下载推荐模型）", "")
            for candidate in bindable:
                combo.addItem(candidate.display_name, candidate.candidate_id)
            if bindable:
                combo.setEnabled(True)
                current_id = self.backend.bound_external_candidate(task)
                current_index = combo.findData(current_id) if current_id else -1
                combo.setCurrentIndex(current_index if current_index >= 1 else 1)
                self._update_candidate_detail(task)
            elif task_candidates:
                combo.setEnabled(False)
                first = task_candidates[0]
                self._status_pills[task].set_state(first.status, StateLevel.WARNING)
                self._set_candidate_detail(task, first)
            else:
                combo.setEnabled(False)
                self._status_pills[task].set_state("未发现候选", StateLevel.INFO)
                self._detail_labels[task].setText("")
        self.wizard.refresh_footer()

    def _on_scan_failed(self, message: str) -> None:
        self._scanning = False
        self._scan_button.setEnabled(True)
        self._scan_button.setText("重试扫描")
        for task, combo in self._combos.items():
            combo.clear()
            combo.setEnabled(False)
            self._status_pills[task].set_state("扫描失败", StateLevel.ERROR)
            self._detail_labels[task].setText(message)
        self.wizard.refresh_footer()

    def _candidate_by_id(self, candidate_id: str) -> ExternalModelCandidate | None:
        return next(
            (candidate for candidate in self._candidates if candidate.candidate_id == candidate_id),
            None,
        )

    def _set_candidate_detail(self, task: TaskType, candidate: ExternalModelCandidate) -> None:
        self._detail_labels[task].setText(
            f"架构：{candidate.architecture or '未知'}\n"
            f"权重：{candidate.model_path or '—'}\n"
            f"配置：{candidate.config_path or '—'}\n"
            f"{candidate.detail}"
        )

    def _update_candidate_detail(self, task: TaskType) -> None:
        combo = self._combos[task]
        candidate_id = str(combo.currentData() or "")
        candidate = self._candidate_by_id(candidate_id)
        if candidate is None:
            if combo.isEnabled():
                self._status_pills[task].set_state("未映射", StateLevel.INFO)
                self._detail_labels[task].setText("该任务以后按需下载工作台推荐模型。")
            return
        self._status_pills[task].set_state(candidate.status, StateLevel.SUCCESS)
        self._set_candidate_detail(task, candidate)

    def can_proceed(self) -> bool:
        return not self._scanning  # 允许部分映射（需求文档 §3.5）

    def on_primary(self) -> bool:
        for task, combo in self._combos.items():
            candidate_id = str(combo.currentData() or "") if combo.isEnabled() else ""
            candidate = self._candidate_by_id(candidate_id)
            if candidate is not None and candidate.bindable:
                self.backend.bind_external_model(task, candidate.candidate_id)
            else:
                self.backend.unbind_external_model(task)
        self.backend.finish_external_mapping()
        return True


class ConnectStep(WizardStep):
    """使用已有 PyMSS：可执行环境或服务地址（需求文档 §4.4）。"""

    title = "连接已有 PyMSS"
    step_label = "连接方式"
    primary_label = "下一步"

    def __init__(self, wizard: "WizardView") -> None:
        super().__init__(wizard)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.setSpacing(10)

        self._exe_option = OptionCard(
            "选择已有的 PyMSS 环境",
            "由工作台负责启动与停止服务。",
            self,
        )
        self._url_option = OptionCard(
            "连接正在运行的服务",
            "工作台只连接，不管理该进程。",
            self,
        )
        self._exe_option.selected.connect(lambda: self._select(exe=True))
        self._url_option.selected.connect(lambda: self._select(exe=False))
        layout.addWidget(self._exe_option)

        self._exe_row = QWidget(self)
        exe_row = QHBoxLayout(self._exe_row)
        exe_row.setContentsMargins(30, 0, 0, 0)
        exe_row.setSpacing(8)
        self._exe_edit = LineEdit(self._exe_row)
        self._exe_edit.setPlaceholderText("例如 D:/tools/pymss/runtime/python.exe")
        self._exe_edit.textChanged.connect(lambda _t: self.wizard.refresh_footer())
        exe_row.addWidget(self._exe_edit, 1)
        browse = PushButton(FIF.FOLDER, "浏览", self._exe_row)
        browse.clicked.connect(self._browse_exe)
        exe_row.addWidget(browse)
        layout.addWidget(self._exe_row)

        layout.addWidget(self._url_option)

        self._url_row = QWidget(self)
        url_row = QHBoxLayout(self._url_row)
        url_row.setContentsMargins(30, 0, 0, 0)
        url_row.setSpacing(8)
        self._url_edit = LineEdit(self._url_row)
        self._url_edit.setPlaceholderText("例如 http://127.0.0.1:8000")
        self._url_edit.textChanged.connect(lambda _t: self.wizard.refresh_footer())
        url_row.addWidget(self._url_edit, 1)
        layout.addWidget(self._url_row)

        self._api_key_row = QWidget(self)
        api_key_row = QHBoxLayout(self._api_key_row)
        api_key_row.setContentsMargins(30, 0, 0, 0)
        api_key_row.setSpacing(8)
        self._api_key_edit = LineEdit(self._api_key_row)
        self._api_key_edit.setPlaceholderText("API key（服务未启用认证时可留空）")
        self._api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        api_key_row.addWidget(self._api_key_edit, 1)
        layout.addWidget(self._api_key_row)

        layout.addWidget(
            HintBox(
                [
                    "工作台不会修改、覆盖或删除外部环境中的任何文件。",
                ],
                self,
            )
        )
        layout.addStretch(1)

        self._exe_option.set_checked(True)
        self._select(exe=True)

    def _select(self, *, exe: bool) -> None:
        self._exe_option.set_checked(exe)
        self._url_option.set_checked(not exe)
        self._exe_row.setVisible(exe)
        self._url_row.setVisible(not exe)
        self._api_key_row.setVisible(not exe)
        self.wizard.refresh_footer()

    def _browse_exe(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择 PyMSS 可执行环境")
        if path:
            self._exe_edit.setText(path)

    def target(self) -> dict[str, str]:
        if self._exe_option.is_checked():
            return {"executable": self._exe_edit.text().strip(), "server_url": "", "api_key": ""}
        return {
            "executable": "",
            "server_url": self._url_edit.text().strip(),
            "api_key": self._api_key_edit.text(),
        }

    def can_proceed(self) -> bool:
        target = self.target()
        return bool(target["executable"] or target["server_url"])


class CapabilityStep(WizardStep):
    """能力检测（需求文档 §4.4 的五项验证）。"""

    title = "能力检测"
    step_label = "能力检测"
    primary_label = "完成接入"

    def __init__(self, wizard: "WizardView") -> None:
        super().__init__(wizard)
        self._all_ok = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(6)
        self._rows = QVBoxLayout()
        self._rows.setContentsMargins(0, 0, 0, 0)
        self._rows.setSpacing(4)
        layout.addLayout(self._rows)
        retry = PushButton(FIF.SYNC, "重新检测", self)
        retry.clicked.connect(self.on_enter)
        layout.addWidget(retry, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addStretch(1)

        self.backend.existingCheckStarted.connect(self._on_check_started)
        self.backend.existingCheckFinished.connect(self._on_check_finished)
        self.backend.existingCheckFailed.connect(self._on_check_failed)

    def on_enter(self) -> None:
        self._all_ok = False
        self._clear_rows()
        target = self.wizard.step_connect.target() if self.wizard.step_connect else {}
        self.backend.start_existing_check(**target)
        self.wizard.refresh_footer()

    def _clear_rows(self) -> None:
        def clear_layout(layout) -> None:
            while layout.count():
                child = layout.takeAt(0)
                if child.widget() is not None:
                    child.widget().deleteLater()
                elif child.layout() is not None:
                    nested = child.layout()
                    clear_layout(nested)
                    nested.deleteLater()

        while self._rows.count():
            item = self._rows.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
            elif item.layout() is not None:
                nested = item.layout()
                clear_layout(nested)
                nested.deleteLater()

    def _on_check_started(self) -> None:
        self._clear_rows()
        row = QHBoxLayout()
        row.addWidget(BodyLabel("正在检测 PyMSS 环境与服务能力…", self))
        row.addStretch(1)
        self._rows.addLayout(row)

    def _on_check_finished(self, results) -> None:
        self._clear_rows()
        self._all_ok = all(ok for _name, ok, _detail in results)
        for name, ok, detail in results:
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(8)
            icon = IconWidget((FIF.COMPLETED if ok else FIF.CLOSE).icon(), self)
            icon.setFixedSize(16, 16)
            row.addWidget(icon)
            row.addWidget(BodyLabel(name, self))
            row.addStretch(1)
            row.addWidget(CaptionLabel(detail, self))
            self._rows.addLayout(row)
        self.wizard.refresh_footer()

    def _on_check_failed(self, message: str) -> None:
        self._clear_rows()
        self._all_ok = False
        row = QHBoxLayout()
        row.addWidget(IconWidget(FIF.CLOSE.icon(), self))
        label = BodyLabel(message or "能力检测失败", self)
        label.setWordWrap(True)
        row.addWidget(label, 1)
        self._rows.addLayout(row)
        self.wizard.refresh_footer()

    def can_proceed(self) -> bool:
        return self._all_ok

    def on_primary(self) -> bool:
        target = self.wizard.step_connect.target() if self.wizard.step_connect else {}
        self.backend.connect_existing(**target)
        return True


class DoneStep(WizardStep):
    """完成页。"""

    title = "完成"
    step_label = "完成"
    primary_label = "开始使用"

    def __init__(self, wizard: "WizardView") -> None:
        super().__init__(wizard)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(8)
        icon = IconWidget(FIF.COMPLETED.icon(), self)
        icon.setFixedSize(40, 40)
        layout.addWidget(icon, 0, Qt.AlignmentFlag.AlignHCenter)
        self._text = BodyLabel("", self)
        self._text.setWordWrap(True)
        self._text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._text)
        layout.addStretch(1)

    def on_enter(self) -> None:
        snap = self.backend.snapshot()
        if self.wizard.flow == FLOW_EXISTING:
            self._text.setText("已连接已有 PyMSS 环境，服务就绪。")
        elif self.wizard.flow == FLOW_REMAP_MSST:
            self._text.setText("MSST 模型映射已更新；托管服务会重新加载模型清单。")
        elif self.wizard.flow == FLOW_UPGRADE:
            self._text.setText(
                f"PyMSS 托管 Runtime 已更新：{snap.install_dir}\n"
                "已有模型、MSST 映射、缓存与日志均已保留。"
            )
        else:
            self._text.setText(
                f"安装完成：{snap.install_dir}\n接下来在工作区启动服务即可。"
            )


class WizardView(QWidget):
    """页内向导容器：步骤指示 + 返回/取消/主按钮。"""

    finished = pyqtSignal()
    cancelled = pyqtSignal()

    def __init__(self, backend: SeparationBackend, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.backend = backend
        self.flow = ""
        self.step_connect: ConnectStep | None = None
        self._steps: list[WizardStep] = []
        self._index = 0

        # 内容限宽居中：向导是阅读型页面，铺满 1800px 宽屏会让每行过长且重心失衡。
        outer = QHBoxLayout(self)
        outer.setContentsMargins(24, 18, 24, 18)
        outer.addStretch(1)
        column = QWidget(self)
        column.setMaximumWidth(WIZARD_COLUMN_MAX_WIDTH)
        outer.addWidget(column, 1)
        outer.addStretch(1)

        layout = QVBoxLayout(column)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)

        self._stepper = WizardStepper(column)
        layout.addWidget(self._stepper)

        card = CardWidget(column, padding=(24, 20, 24, 22), spacing=14)
        card_layout = card.createVBoxLayout()
        self._step_title = SubtitleLabel("", card)
        card_layout.addWidget(self._step_title)
        self._step_hint = BodyLabel("", card)
        self._step_hint.setWordWrap(True)
        self._step_hint.setVisible(False)
        card_layout.addWidget(self._step_hint)

        self._stack = QStackedWidget(card)
        card_layout.addWidget(self._stack, 1)
        # 卡片贴合内容高度，不拉满整页；页面剩余空间留到底部。
        layout.addWidget(card, 0)

        footer = QHBoxLayout()
        footer.setContentsMargins(0, 0, 0, 0)
        footer.setSpacing(8)
        self._back_button = PushButton(FIF.RETURN, "返回", column)
        self._back_button.clicked.connect(self._go_back)
        self._cancel_button = PushButton(FIF.CLOSE, "取消", column)
        self._cancel_button.clicked.connect(self._cancel)
        self._primary_button = PrimaryPushButton("下一步", column)
        self._primary_button.setMinimumWidth(120)
        self._primary_button.clicked.connect(self._go_next)
        footer.addWidget(self._back_button)
        footer.addStretch(1)
        footer.addWidget(self._cancel_button)
        footer.addWidget(self._primary_button)
        layout.addLayout(footer)
        layout.addStretch(1)

        backend.snapshotChanged.connect(self._on_snapshot)

    # ── 流程组装 ──────────────────────────────────────────────────
    def start_flow(self, flow: str) -> None:
        self.flow = flow
        for step in self._steps:
            self._stack.removeWidget(step)
            step.deleteLater()
        self._steps = []
        self.step_connect = None

        if flow == FLOW_EXISTING:
            self.step_connect = ConnectStep(self)
            self._steps = [self.step_connect, CapabilityStep(self), DoneStep(self)]
        elif flow == FLOW_REUSE_MSST:
            self._steps = [
                InstallLocationStep(self),
                ConfirmStep(self),
                ProgressStep(self),
                MsstMappingStep(self),
                DoneStep(self),
            ]
        elif flow == FLOW_REMAP_MSST:
            self._steps = [MsstMappingStep(self), DoneStep(self)]
        elif flow == FLOW_UPGRADE:
            self._steps = [ConfirmStep(self), ProgressStep(self), DoneStep(self)]
        else:
            self._steps = [
                InstallLocationStep(self),
                ConfirmStep(self),
                ProgressStep(self),
                DoneStep(self),
            ]
        for step in self._steps:
            self._stack.addWidget(step)
        self._stepper.set_steps([s.step_label or s.title for s in self._steps], 0)
        self._index = 0
        self._show_current()

    # ── 导航 ─────────────────────────────────────────────────────
    def _show_current(self) -> None:
        step = self._steps[self._index]
        self._stack.setCurrentWidget(step)
        self._stepper.set_current(self._index)
        self._step_title.setText(step.title)
        self._step_hint.setText(step.hint)
        self._step_hint.setVisible(bool(step.hint))
        step.on_enter()
        self.refresh_footer()

    def refresh_footer(self) -> None:
        # 步骤在自己的构造函数里就会回调这里，此时 _steps 还没装配好。
        if not self._steps or self._index >= len(self._steps):
            return
        step = self._steps[self._index]
        snapshot = self.backend.snapshot()
        runtime_busy = (
            isinstance(step, ProgressStep)
            and snapshot.state
            in {ServiceState.RUNTIME_DOWNLOADING, ServiceState.RUNTIME_VERIFYING}
        )
        self._back_button.setEnabled(self._index > 0 and not runtime_busy)
        self._primary_button.setText(step.primary_label)
        self._primary_button.setEnabled(step.can_proceed())

    def _go_next(self) -> None:
        step = self._steps[self._index]
        if not step.can_proceed() or not step.on_primary():
            self.refresh_footer()
            return
        if self._index >= len(self._steps) - 1:
            self.finished.emit()
            return
        self._index += 1
        self._show_current()

    def _go_back(self) -> None:
        current = self._steps[self._index] if self._steps else None
        if isinstance(current, ProgressStep) and self.backend.snapshot().state in {
            ServiceState.RUNTIME_DOWNLOADING,
            ServiceState.RUNTIME_VERIFYING,
        }:
            return
        if isinstance(current, MsstMappingStep):
            self.backend.cancel_msst_scan()
        if self._index > 0:
            self._index -= 1
            self._show_current()

    def _cancel(self) -> None:
        snap = self.backend.snapshot()
        if snap.state == ServiceState.RUNTIME_DOWNLOADING:
            self.backend.cancel_install()
        self.backend.cancel_msst_scan()
        self.cancelled.emit()

    # ── 后端状态联动 ──────────────────────────────────────────────
    def _on_snapshot(self, snapshot) -> None:
        current = self._steps[self._index] if self._steps else None
        if isinstance(current, ProgressStep):
            current.apply_snapshot(snapshot)
            if snapshot.state == ServiceState.INSTALLED_STOPPED:
                # 安装完成 → 自动进入下一步（完成页或 MSST 映射页）。
                self._index += 1
                self._show_current()
