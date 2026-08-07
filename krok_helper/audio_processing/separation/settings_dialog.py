"""音频分离设置对话框（需求文档 §3.3「设置入口」/ §3.7）。

独立对话框 + Pivot 四页：安装与 Runtime / 服务与下载 / 诊断与日志 / 修复与重置。
写法对齐全局设置（``gui_qt._open_global_settings_window``），但不 import gui_qt，
保持音频分离包可独立测试。
"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QListWidget,
    QListWidgetItem,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    ComboBox,
    FluentIcon as FIF,
    LineEdit,
    Pivot,
    PrimaryPushButton,
    PlainTextEdit,
    PushButton,
    ScrollArea as FluentScrollArea,
    SettingCard,
    SettingCardGroup,
    SubtitleLabel,
    ToolButton,
)

from krok_helper.audio_processing.separation.backend import SeparationBackend
from krok_helper.audio_processing.separation.local_import import (
    WEIGHT_SUFFIXES,
    guess_config_path,
    read_stems,
    requires_config,
    supported_model_types,
)
from krok_helper.audio_processing.separation.presets import TASK_PRESETS, task_override
from krok_helper.audio_processing.separation.states import STATE_META, TASK_SPECS, TaskType
from krok_helper.qfluent_compat import ask_fluent_confirm, show_fluent_info

_DOWNLOAD_SOURCES = (
    ("ModelScope（推荐）", "modelscope"),
    ("Hugging Face", "huggingface"),
    ("HF Mirror", "hf-mirror"),
)


def _add_card_actions(card: SettingCard, *widgets: QWidget, spacing: int = 8) -> None:
    """把控件依次挂到 SettingCard 右侧（对齐 gui_qt.add_setting_card_actions）。"""
    for widget in widgets:
        card.hBoxLayout.addWidget(widget, 0, Qt.AlignmentFlag.AlignRight)
        card.hBoxLayout.addSpacing(spacing)
    if widgets:
        card.hBoxLayout.addSpacing(max(0, 16 - spacing))


def _build_settings_page(parent: QWidget, groups: list[SettingCardGroup]) -> FluentScrollArea:
    """把若干 SettingCardGroup 装进透明背景的纵向滚动页（对齐 gui_qt 同款 helper）。"""
    page = FluentScrollArea(parent)
    page.setWidgetResizable(True)
    page.setFrameShape(QFrame.Shape.NoFrame)
    page.enableTransparentBackground()
    content = QWidget(page)
    layout = QVBoxLayout(content)
    layout.setContentsMargins(2, 8, 10, 4)
    layout.setSpacing(18)
    for group in groups:
        layout.addWidget(group)
    layout.addStretch(1)
    page.setWidget(content)
    return page


class ModelPickerDialog(QDialog):
    """给一个任务挑模型与输出轨。

    输出轨永远来自所选模型自己声明的名字（后端读 ``training.instruments``），
    用户只能从下拉里选，不能手填——同一个概念在不同模型里可能叫 ``vocals`` /
    ``other`` / ``karaoke`` / ``Instrumental``，手填必错。
    """

    def __init__(
        self,
        backend: SeparationBackend,
        task,
        current_model: str,
        current_stem: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._backend = backend
        self._task = task
        self._models: list = []
        self._selected_model = ""
        self._stems: tuple[str, ...] = ()

        self.setWindowTitle(f"{TASK_SPECS[task].title} · 选择模型")
        self.resize(620, 560)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(10)

        layout.addWidget(SubtitleLabel(f"{TASK_SPECS[task].title} 使用的模型", self))

        self._search = LineEdit(self)
        self._search.setPlaceholderText("搜索模型名或分类")
        self._search.textChanged.connect(self._apply_filter)
        layout.addWidget(self._search)

        self._list = QListWidget(self)
        self._list.currentItemChanged.connect(self._on_model_selected)
        layout.addWidget(self._list, 1)

        stem_row = QHBoxLayout()
        stem_row.setContentsMargins(0, 0, 0, 0)
        stem_row.setSpacing(8)
        stem_row.addWidget(BodyLabel("输出轨", self))
        self._stem_combo = ComboBox(self)
        self._stem_combo.setMinimumWidth(220)
        self._stem_combo.setEnabled(False)
        self._stem_combo.currentIndexChanged.connect(lambda _i: self._refresh_ok())
        stem_row.addWidget(self._stem_combo)
        stem_row.addStretch(1)
        layout.addLayout(stem_row)

        self._hint = CaptionLabel("请选择一个模型。", self)
        self._hint.setWordWrap(True)
        layout.addWidget(self._hint)

        footer = QHBoxLayout()
        footer.setContentsMargins(0, 0, 0, 0)
        footer.addStretch(1)
        cancel = PushButton("取消", self)
        cancel.clicked.connect(self.reject)
        footer.addWidget(cancel)
        self._ok = PrimaryPushButton("确定", self)
        self._ok.setEnabled(False)
        self._ok.clicked.connect(self.accept)
        footer.addWidget(self._ok)
        layout.addLayout(footer)

        self._pending_model = current_model
        self._pending_stem = current_stem
        backend.catalogModelsFinished.connect(self._on_models)
        backend.catalogModelsFailed.connect(self._on_models_failed)
        backend.modelStemsFinished.connect(self._on_stems)
        backend.modelStemsFailed.connect(self._on_stems_failed)
        self._hint.setText("正在读取模型列表…")
        backend.request_catalog_models()

    # ── 结果 ─────────────────────────────────────────────────────
    def selection(self) -> tuple[str, str, int]:
        size = next(
            (m.size_bytes for m in self._models if m.name == self._selected_model), 0
        )
        return self._selected_model, self._stem_combo.currentText(), size

    # ── 列表 ─────────────────────────────────────────────────────
    def _on_models(self, models) -> None:
        self._models = list(models)
        self._apply_filter()
        if not self._models:
            self._hint.setText("模型列表为空。")
            return
        self._hint.setText("请选择一个模型。")
        if self._pending_model:
            for index in range(self._list.count()):
                if self._list.item(index).data(Qt.ItemDataRole.UserRole) == self._pending_model:
                    self._list.setCurrentRow(index)
                    break

    def _on_models_failed(self, reason: str) -> None:
        self._hint.setText(reason)

    def _apply_filter(self) -> None:
        keyword = self._search.text().strip().lower()
        selected = self._selected_model
        self._list.blockSignals(True)
        self._list.clear()
        for model in self._models:
            haystack = f"{model.name} {model.category} {model.architecture}".lower()
            if keyword and keyword not in haystack:
                continue
            size = f"{model.size_bytes / 1024 ** 3:.2f} GB" if model.size_bytes else "—"
            mark = "✓ 已下载" if model.downloaded else size
            item = QListWidgetItem(f"{model.name}\n{model.category} · {mark}")
            item.setData(Qt.ItemDataRole.UserRole, model.name)
            self._list.addItem(item)
        self._list.blockSignals(False)
        if selected:
            for index in range(self._list.count()):
                if self._list.item(index).data(Qt.ItemDataRole.UserRole) == selected:
                    self._list.setCurrentRow(index)
                    break

    def _on_model_selected(self, current, _previous) -> None:
        if current is None:
            return
        self._selected_model = str(current.data(Qt.ItemDataRole.UserRole) or "")
        self._stems = ()
        self._stem_combo.clear()
        self._stem_combo.setEnabled(False)
        self._refresh_ok()
        self._hint.setText("正在读取该模型的输出轨…")
        self._backend.request_model_stems(self._selected_model)

    # ── 输出轨 ───────────────────────────────────────────────────
    def _on_stems(self, model: str, stems) -> None:
        if model != self._selected_model:
            return
        self._stems = tuple(stems)
        self._stem_combo.clear()
        for stem in self._stems:
            self._stem_combo.addItem(stem)
        self._stem_combo.setEnabled(bool(self._stems))
        if self._pending_stem in self._stems:
            self._stem_combo.setCurrentIndex(self._stems.index(self._pending_stem))
            self._pending_stem = ""
        self._hint.setText(
            "输出轨来自该模型自己的配置，只能从上面这几个里选。"
            if self._stems
            else "该模型没有可用的输出轨。"
        )
        self._refresh_ok()

    def _on_stems_failed(self, model: str, reason: str) -> None:
        if model != self._selected_model:
            return
        self._stems = ()
        self._stem_combo.clear()
        self._stem_combo.setEnabled(False)
        self._hint.setText(reason)
        self._refresh_ok()

    def _refresh_ok(self) -> None:
        self._ok.setEnabled(bool(self._selected_model) and bool(self._stem_combo.count()))


class LocalModelImportDialog(QDialog):
    """从任意文件夹导入一个模型并绑定给某个任务。

    原文件保持原地，只在工作台自己的清单里登记引用（§4.5）。架构无法从配置推断，
    因此让用户从 PyMSS 支持的列表里选——同样是选而不是填。
    """

    def __init__(self, backend: SeparationBackend, task, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._backend = backend
        self._task = task

        self.setWindowTitle(f"{TASK_SPECS[task].title} · 导入本地模型")
        self.resize(640, 420)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(10)
        layout.addWidget(SubtitleLabel("导入本地模型", self))

        weight_row = QHBoxLayout()
        weight_row.setContentsMargins(0, 0, 0, 0)
        weight_row.setSpacing(8)
        weight_row.addWidget(BodyLabel("权重文件", self))
        self._weight_edit = LineEdit(self)
        self._weight_edit.setReadOnly(True)
        self._weight_edit.setPlaceholderText("选择 .ckpt / .pth 等权重文件")
        weight_row.addWidget(self._weight_edit, 1)
        browse = PushButton(FIF.FOLDER, "浏览", self)
        browse.clicked.connect(self._browse_weight)
        weight_row.addWidget(browse)
        layout.addLayout(weight_row)

        config_row = QHBoxLayout()
        config_row.setContentsMargins(0, 0, 0, 0)
        config_row.setSpacing(8)
        config_row.addWidget(BodyLabel("配置文件", self))
        self._config_edit = LineEdit(self)
        self._config_edit.setReadOnly(True)
        self._config_edit.setPlaceholderText("留空表示该架构不需要配置")
        config_row.addWidget(self._config_edit, 1)
        pick_config = PushButton(FIF.FOLDER, "浏览", self)
        pick_config.clicked.connect(self._browse_config)
        config_row.addWidget(pick_config)
        layout.addLayout(config_row)

        type_row = QHBoxLayout()
        type_row.setContentsMargins(0, 0, 0, 0)
        type_row.setSpacing(8)
        type_row.addWidget(BodyLabel("模型架构", self))
        self._type_combo = ComboBox(self)
        self._type_combo.setMinimumWidth(240)
        for name in supported_model_types():
            self._type_combo.addItem(name)
        self._type_combo.setCurrentIndex(
            max(0, [*supported_model_types()].index("mel_band_roformer"))
            if "mel_band_roformer" in supported_model_types()
            else 0
        )
        self._type_combo.currentIndexChanged.connect(lambda _i: self._refresh())
        type_row.addWidget(self._type_combo)
        type_row.addStretch(1)
        layout.addLayout(type_row)

        self._stems_label = BodyLabel("", self)
        self._stems_label.setWordWrap(True)
        layout.addWidget(self._stems_label)

        self._hint = CaptionLabel("先选择权重文件；同目录下的同名 .yaml 会被自动识别。", self)
        self._hint.setWordWrap(True)
        layout.addWidget(self._hint)
        layout.addStretch(1)

        footer = QHBoxLayout()
        footer.setContentsMargins(0, 0, 0, 0)
        footer.addStretch(1)
        cancel = PushButton("取消", self)
        cancel.clicked.connect(self.reject)
        footer.addWidget(cancel)
        self._ok = PrimaryPushButton("导入并绑定", self)
        self._ok.setEnabled(False)
        self._ok.clicked.connect(self._submit)
        footer.addWidget(self._ok)
        layout.addLayout(footer)

        backend.localImportFinished.connect(self._on_finished)
        backend.localImportFailed.connect(self._on_failed)

    # ── 选择文件 ─────────────────────────────────────────────────
    def _browse_weight(self) -> None:
        patterns = " ".join(f"*{suffix}" for suffix in WEIGHT_SUFFIXES)
        path, _ = QFileDialog.getOpenFileName(self, "选择模型权重", "", f"模型权重 ({patterns})")
        if not path:
            return
        self._weight_edit.setText(path)
        guessed = guess_config_path(path)
        self._config_edit.setText(str(guessed) if guessed else "")
        self._refresh()

    def _browse_config(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择模型配置", "", "YAML 配置 (*.yaml *.yml)")
        if path:
            self._config_edit.setText(path)
            self._refresh()

    # ── 状态 ─────────────────────────────────────────────────────
    def _refresh(self) -> None:
        weight = self._weight_edit.text().strip()
        config = self._config_edit.text().strip()
        model_type = self._type_combo.currentText()

        if not weight:
            self._stems_label.setText("")
            self._ok.setEnabled(False)
            return

        stems = read_stems(config) if config else ()
        if stems:
            self._stems_label.setText("该模型声明的输出轨：" + "、".join(stems))
        elif config:
            self._stems_label.setText("未能从配置中读出输出轨。")
        else:
            self._stems_label.setText("")

        if requires_config(model_type) and not config:
            self._hint.setText(f"{model_type} 架构需要 YAML 配置文件，请选择。")
            self._ok.setEnabled(False)
            return
        if requires_config(model_type) and not stems:
            self._hint.setText("配置里读不出输出轨，无法确定该模型能产出什么，请检查配置文件。")
            self._ok.setEnabled(False)
            return
        self._hint.setText("导入后会登记为本地模型引用；原文件不会被复制或修改。")
        self._ok.setEnabled(True)

    def _submit(self) -> None:
        self._ok.setEnabled(False)
        self._hint.setText("正在校验模型…")
        self._backend.import_local_model(
            self._task,
            weight_path=self._weight_edit.text().strip(),
            config_path=self._config_edit.text().strip(),
            model_type=self._type_combo.currentText(),
        )

    def _on_finished(self, _candidate) -> None:
        self.accept()

    def _on_failed(self, reason: str) -> None:
        self._hint.setText(reason)
        self._ok.setEnabled(True)


class FolderImportDialog(QDialog):
    """一键导入：选一个文件夹，自动为三个任务各匹配一个模型。

    支持 MSST-WebUI 的 ``pretrain``（架构来自 MSST 自己的映射文件）和按 catalog
    结构摆放的 ``models/``（按文件名反查 catalog）。匹配结果可逐行改选或不绑定。
    """

    def __init__(self, backend: SeparationBackend, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._backend = backend
        self._candidates: list = []

        self.setWindowTitle("一键导入模型文件夹")
        self.resize(700, 480)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(10)
        layout.addWidget(SubtitleLabel("一键导入模型文件夹", self))

        folder_row = QHBoxLayout()
        folder_row.setContentsMargins(0, 0, 0, 0)
        folder_row.setSpacing(8)
        folder_row.addWidget(BodyLabel("文件夹", self))
        self._folder_edit = LineEdit(self)
        self._folder_edit.setReadOnly(True)
        self._folder_edit.setPlaceholderText("PyMSS 的 models 目录，或 MSST 的 pretrain 目录")
        folder_row.addWidget(self._folder_edit, 1)
        browse = PushButton(FIF.FOLDER, "浏览", self)
        browse.clicked.connect(self._browse)
        folder_row.addWidget(browse)
        layout.addLayout(folder_row)

        group = SettingCardGroup("匹配到的模型", self)
        self._rows: dict = {}
        for task in TaskType:
            card = SettingCard(FIF.LIBRARY, TASK_SPECS[task].title, "尚未扫描", group)
            combo = ComboBox(card)
            combo.setMinimumWidth(260)
            combo.setEnabled(False)
            _add_card_actions(card, combo)
            group.addSettingCard(card)
            self._rows[task] = (card, combo)
        layout.addWidget(group)

        self._hint = CaptionLabel("选择文件夹后自动扫描；原文件保持原地，只登记引用。", self)
        self._hint.setWordWrap(True)
        layout.addWidget(self._hint)
        layout.addStretch(1)

        footer = QHBoxLayout()
        footer.setContentsMargins(0, 0, 0, 0)
        footer.addStretch(1)
        cancel = PushButton("取消", self)
        cancel.clicked.connect(self.reject)
        footer.addWidget(cancel)
        self._ok = PrimaryPushButton("导入并绑定", self)
        self._ok.setEnabled(False)
        self._ok.clicked.connect(self._apply)
        footer.addWidget(self._ok)
        layout.addLayout(footer)

        backend.folderScanStarted.connect(self._on_started)
        backend.folderScanFinished.connect(self._on_finished)
        backend.folderScanFailed.connect(self._on_failed)

    def _browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "选择模型文件夹")
        if not folder:
            return
        self._folder_edit.setText(folder)
        self._backend.start_folder_scan(folder)

    def _on_started(self) -> None:
        self._hint.setText("正在扫描…")
        self._ok.setEnabled(False)

    def _on_finished(self, candidates, matched) -> None:
        self._candidates = list(candidates)
        any_match = False
        for task, (card, combo) in self._rows.items():
            options = [
                item
                for item in self._candidates
                if item.task is task and item.bindable
            ]
            combo.blockSignals(True)
            combo.clear()
            # qfluentwidgets 的 ComboBox 用 addItem(..., userData=) 带数据，
            # 其 setItemData/currentData 不接受 Qt 的 role 参数。
            combo.addItem("不绑定", userData="")
            for item in options:
                combo.addItem(item.display_name, userData=item.candidate_id)
            combo.blockSignals(False)
            combo.setEnabled(bool(options))

            chosen = matched.get(task, "")
            if chosen:
                for index in range(combo.count()):
                    if combo.itemData(index) == chosen:
                        combo.setCurrentIndex(index)
                        break
                picked = next(
                    (item for item in options if item.candidate_id == chosen), None
                )
                card.setContent(
                    f"{picked.display_name} · {picked.model_type} · 输出轨 {picked.target_stem}"
                    if picked
                    else "已匹配"
                )
                any_match = True
            else:
                card.setContent("这个文件夹里没有适合该任务的模型")
        self._ok.setEnabled(any_match)
        total = len({item.candidate_id for item in self._candidates if item.bindable})
        self._hint.setText(
            f"扫描完成：可用候选 {total} 个。原文件保持原地，只登记引用。"
            if total
            else "扫描完成，但没有找到可用的模型。"
        )

    def _on_failed(self, reason: str) -> None:
        self._hint.setText(reason)
        self._ok.setEnabled(False)

    def selection(self) -> dict:
        """返回用户确认后的 {任务: candidate_id}（不含「不绑定」）。"""
        result = {}
        for task, (_card, combo) in self._rows.items():
            candidate_id = combo.currentData()
            if candidate_id:
                result[task] = str(candidate_id)
        return result

    def _apply(self) -> None:
        chosen = self.selection()
        if not chosen:
            self._hint.setText("请至少为一个任务选择模型。")
            return
        for task, candidate_id in chosen.items():
            try:
                self._backend.bind_external_model(task, candidate_id)
            except Exception as exc:
                self._hint.setText(f"绑定失败：{exc}")
                return
        # 整批绑完再收尾一次：PyMSS 在进程内缓存用户模型清单，不重载会报
        # "Unknown pymss model"。逐个重启服务没有必要，这里只做一次。
        self._backend.finish_external_mapping()
        self.accept()


class SeparationSettingsDialog(QDialog):
    """音频分离设置。所有改动直接写 settings_ns 并触发 save_settings。"""

    reconfigureRequested = pyqtSignal()

    def __init__(
        self,
        backend: SeparationBackend,
        settings_ns: dict,
        save_settings,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._backend = backend
        self._settings = settings_ns
        self._save_settings = save_settings

        self.setWindowTitle("音频分离设置")
        self.resize(720, 560)
        self.setMinimumSize(640, 480)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 18, 24, 16)
        outer.setSpacing(8)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.addWidget(SubtitleLabel("音频分离设置", self), 0, Qt.AlignmentFlag.AlignVCenter)
        header.addStretch(1)
        close_button = PushButton("关闭", self)
        close_button.clicked.connect(self.close)
        header.addWidget(close_button, 0, Qt.AlignmentFlag.AlignVCenter)
        outer.addLayout(header)

        self._pivot = Pivot(self)
        outer.addWidget(self._pivot)
        self._stack = QStackedWidget(self)
        outer.addWidget(self._stack, 1)

        pages = [
            ("runtime", "安装与 Runtime", self._build_runtime_page()),
            ("models", "模型与输出轨", self._build_models_page()),
            ("service", "服务与下载", self._build_service_page()),
            ("diagnostics", "诊断与日志", self._build_diagnostics_page()),
            ("repair", "修复与重置", self._build_repair_page()),
        ]
        for index, (key, text, page) in enumerate(pages):
            self._stack.addWidget(page)
            self._pivot.addItem(
                routeKey=key,
                text=text,
                onClick=lambda _checked=False, i=index: self._stack.setCurrentIndex(i),
            )
        self._pivot.setCurrentItem(pages[0][0])

        self._backend.logAppended.connect(self._append_log)
        self._backend.snapshotChanged.connect(self._apply_snapshot)
        self._apply_snapshot(self._backend.snapshot())

    # ── 安装与 Runtime ──────────────────────────────────────────────
    def _build_runtime_page(self) -> QWidget:
        group = SettingCardGroup("安装与 Runtime", self)

        self._install_dir_card = SettingCard(
            FIF.FOLDER, "安装位置", "PyMSS 托管 Runtime 的安装目录", group
        )
        self._install_dir_label = CaptionLabel("—", self._install_dir_card)
        self._install_dir_label.setMaximumWidth(260)
        self._change_install_button = PushButton("更改…", self._install_dir_card)
        self._change_install_button.clicked.connect(self._change_install_dir)
        self._open_install_button = ToolButton(FIF.FOLDER, self._install_dir_card)
        self._open_install_button.setToolTip("打开安装目录")
        self._open_install_button.clicked.connect(self._open_install_dir)
        _add_card_actions(
            self._install_dir_card,
            self._install_dir_label,
            self._change_install_button,
            self._open_install_button,
        )
        group.addSettingCard(self._install_dir_card)

        self._mode_card = SettingCard(FIF.IOT, "运行方式", "当前使用的 PyMSS 来源", group)
        self._mode_label = CaptionLabel("—", self._mode_card)
        _add_card_actions(self._mode_card, self._mode_label)
        group.addSettingCard(self._mode_card)

        self._version_card = SettingCard(
            FIF.INFO, "PyMSS 版本", "兼容范围以需求文档冻结值为准", group
        )
        self._version_label = CaptionLabel("—", self._version_card)
        refresh_button = PushButton(FIF.SYNC, "重新检测", self._version_card)
        refresh_button.clicked.connect(
            lambda _checked=False: self._backend.refresh(full=True)
        )
        _add_card_actions(self._version_card, self._version_label, refresh_button)
        group.addSettingCard(self._version_card)

        return _build_settings_page(self, [group])

    # ── 模型与输出轨 ────────────────────────────────────────────
    def _build_models_page(self) -> QWidget:
        quick = SettingCardGroup("批量导入", self)
        quick_card = SettingCard(
            FIF.FOLDER,
            "一键导入模型文件夹",
            "选 PyMSS 的 models 目录或 MSST 的 pretrain 目录，自动为三个任务匹配模型",
            quick,
        )
        quick_button = PushButton("选择文件夹…", quick_card)
        quick_button.clicked.connect(self._import_folder)
        _add_card_actions(quick_card, quick_button)
        quick.addSettingCard(quick_card)

        group = SettingCardGroup("每个任务使用的模型", self)
        self._model_cards: dict = {}
        for task in TaskType:
            card = SettingCard(FIF.LIBRARY, TASK_SPECS[task].title, "", group)
            change = PushButton("更换…", card)
            change.clicked.connect(lambda _c=False, t=task: self._pick_model(t))
            imp = PushButton("从文件导入…", card)
            imp.clicked.connect(lambda _c=False, t=task: self._import_local(t))
            reset = PushButton("恢复推荐", card)
            reset.clicked.connect(lambda _c=False, t=task: self._reset_model(t))
            _add_card_actions(card, change, imp, reset)
            group.addSettingCard(card)
            self._model_cards[task] = (card, reset)
        self._refresh_model_cards()
        return _build_settings_page(self, [quick, group])

    def _refresh_model_cards(self) -> None:
        for task, (card, reset) in self._model_cards.items():
            override = task_override(self._settings, task)
            if override is None:
                step = TASK_PRESETS[task].steps[-1]
                text = f"推荐模型：{step.model} · 输出轨 {step.stems[-1]}"
                reset.setEnabled(False)
            else:
                text = f"自定义：{override['model']} · 输出轨 {override['stem']}"
                reset.setEnabled(True)
            card.setContent(text)
            # catalog 模型名很长，卡片一行放不下会被裁掉，补一个完整内容的悬浮提示。
            card.setToolTip(text)

    def _pick_model(self, task) -> None:
        override = task_override(self._settings, task)
        step = TASK_PRESETS[task].steps[-1]
        dialog = ModelPickerDialog(
            self._backend,
            task,
            override["model"] if override else step.model,
            override["stem"] if override else step.stems[-1],
            self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        model, stem, size = dialog.selection()
        if not model or not stem:
            return
        self._backend.set_task_model(task, model, stem, size)
        self._save_settings()
        self._refresh_model_cards()

    def _import_folder(self) -> None:
        dialog = FolderImportDialog(self._backend, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self._save_settings()
        self._refresh_model_cards()

    def _import_local(self, task) -> None:
        dialog = LocalModelImportDialog(self._backend, task, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self._save_settings()
        self._refresh_model_cards()

    def _reset_model(self, task) -> None:
        self._backend.set_task_model(task, "", "", 0)
        self._save_settings()
        self._refresh_model_cards()

    # ── 服务与下载 ────────────────────────────────────────────────
    def _build_service_page(self) -> QWidget:
        group = SettingCardGroup("服务与下载", self)

        source_card = SettingCard(FIF.CLOUD_DOWNLOAD, "模型下载源", "推荐模型的官方下载来源", group)
        self._source_combo = ComboBox(source_card)
        for label, _value in _DOWNLOAD_SOURCES:
            self._source_combo.addItem(label)
        self._source_combo.setMinimumWidth(190)
        current = str(self._settings.get("download_source", "modelscope"))
        if current == "github":
            current = "huggingface"
            self._settings["download_source"] = current
        for i, (_label, value) in enumerate(_DOWNLOAD_SOURCES):
            if value == current:
                self._source_combo.setCurrentIndex(i)
                break
        self._source_combo.currentIndexChanged.connect(self._on_source_changed)
        _add_card_actions(source_card, self._source_combo)
        group.addSettingCard(source_card)

        url_card = SettingCard(
            FIF.GLOBE, "外部服务地址", "仅「使用已有 PyMSS」的服务模式生效", group
        )
        self._url_label = CaptionLabel(
            str(self._settings.get("external_server_url", "")) or "未使用外部服务",
            url_card,
        )
        self._url_label.setMaximumWidth(260)
        reconfigure = PushButton("重新配置…", url_card)
        reconfigure.clicked.connect(self._request_reconfigure)
        _add_card_actions(url_card, self._url_label, reconfigure)
        group.addSettingCard(url_card)

        output_card = SettingCard(FIF.SAVE, "默认输出目录", "音频分离结果的保存位置", group)
        self._output_label = CaptionLabel(
            str(self._settings.get("output_dir", "")) or "同素材目录", output_card
        )
        self._output_label.setMaximumWidth(240)
        change_output = PushButton("更改…", output_card)
        change_output.clicked.connect(self._change_output_dir)
        _add_card_actions(output_card, self._output_label, change_output)
        group.addSettingCard(output_card)

        return _build_settings_page(self, [group])

    # ── 诊断与日志 ────────────────────────────────────────────────
    def _build_diagnostics_page(self) -> QWidget:
        group = SettingCardGroup("诊断与日志", self)

        device_card = SettingCard(
            FIF.GAME, "设备诊断", "查看当前设备、PyMSS 版本与模型状态", group
        )
        device_button = PushButton(FIF.SEARCH, "查看诊断", device_card)
        device_button.clicked.connect(self._show_device_diagnostics)
        _add_card_actions(device_card, device_button)
        group.addSettingCard(device_card)

        log_dir_card = SettingCard(
            FIF.FOLDER, "日志目录", "服务启动、模型加载与推理日志", group
        )
        open_log = PushButton(FIF.FOLDER, "打开日志目录", log_dir_card)
        open_log.clicked.connect(self._open_log_dir)
        _add_card_actions(log_dir_card, open_log)
        group.addSettingCard(log_dir_card)

        self._log_view = PlainTextEdit(group)
        self._log_view.setReadOnly(True)
        self._log_view.setMinimumHeight(180)
        recent = getattr(self._backend, "recent_logs", None)
        if recent:
            self._log_view.setPlainText("\n".join(recent))
        group.addSettingCard(self._wrap_log_card(group))

        return _build_settings_page(self, [group])

    def _wrap_log_card(self, group: SettingCardGroup) -> SettingCard:
        card = SettingCard(FIF.DOCUMENT, "运行日志", "最近 200 条后端运行记录", group)
        card.hBoxLayout.addWidget(self._log_view, 1)
        return card

    # ── 修复与重置 ────────────────────────────────────────────────
    def _build_repair_page(self) -> QWidget:
        group = SettingCardGroup("修复与重置", self)

        actions = [
            (
                FIF.BROOM,
                "修复安装",
                "在当前安装路径补齐缺失或损坏的文件，保留已下载模型",
                "修复安装",
                self._on_repair,
            ),
            (
                FIF.FOLDER,
                "重新选择位置",
                "指向现有安装目录或选择新目录重新安装",
                "选择目录…",
                self._on_relocate,
            ),
            (
                FIF.UPDATE,
                "重新完整安装",
                "重新下载 Runtime；默认保留校验通过的模型",
                "重新安装",
                self._on_reinstall,
            ),
            (
                FIF.DELETE,
                "移除配置",
                "只清除工作台中的路径与状态，不删除任何用户文件",
                "移除配置",
                self._on_remove_config,
            ),
        ]
        self._managed_action_buttons: list[PushButton] = []
        for icon, title, desc, button_text, handler in actions:
            card = SettingCard(icon, title, desc, group)
            button = PushButton(button_text, card)
            button.clicked.connect(handler)
            if handler in {self._on_repair, self._on_reinstall}:
                self._managed_action_buttons.append(button)
            _add_card_actions(card, button)
            group.addSettingCard(card)

        return _build_settings_page(self, [group])

    # ── 状态同步 ──────────────────────────────────────────────────
    def _apply_snapshot(self, snapshot) -> None:
        self._install_dir_label.setText(snapshot.install_dir or "—")
        self._version_label.setText(snapshot.pymss_version or "—")
        self._url_label.setText(
            str(self._settings.get("external_server_url", "")) or "未使用外部服务"
        )
        managed = bool(snapshot.install_dir)
        self._open_install_button.setEnabled(managed)
        for button in self._managed_action_buttons:
            button.setEnabled(managed)
        if snapshot.install_dir:
            self._mode_label.setText("托管 Runtime")
        elif self._settings.get("external_server_url"):
            self._mode_label.setText("外部服务")
        elif self._settings.get("external_executable"):
            self._mode_label.setText("外部环境")
        else:
            self._mode_label.setText("未配置")

    def _append_log(self, text: str) -> None:
        self._log_view.appendPlainText(text)

    # ── 服务与下载页动作 ───────────────────────────────────────────
    def _on_source_changed(self, index: int) -> None:
        if 0 <= index < len(_DOWNLOAD_SOURCES):
            self._settings["download_source"] = _DOWNLOAD_SOURCES[index][1]
            self._save_settings()

    def _request_reconfigure(self) -> None:
        self.reconfigureRequested.emit()
        self.close()

    def _change_output_dir(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "选择默认输出目录")
        if directory:
            self._settings["output_dir"] = directory
            self._output_label.setText(directory)
            self._save_settings()

    # ── 安装页动作 ────────────────────────────────────────────────
    def _change_install_dir(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "选择新的安装目录")
        if directory:
            self._backend.relocate_install(str(Path(directory) / "pymss"))
            self._save_settings()

    def _open_install_dir(self) -> None:
        install_dir = self._backend.snapshot().install_dir
        if install_dir and Path(install_dir).is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(install_dir))
        else:
            show_fluent_info(self, "安装目录不存在或尚未配置。")

    # ── 诊断页动作 ────────────────────────────────────────────────
    def _show_device_diagnostics(self) -> None:
        snap = self._backend.snapshot()
        dependency_lines = [
            f"{TASK_SPECS[task].title}：{dependency.badge or ('就绪' if dependency.ready else '不可用')}"
            for task, dependency in snap.dependencies.items()
        ]
        state_text = STATE_META[snap.state].label
        show_fluent_info(
            self,
            f"当前状态：{state_text}\n"
            f"PyMSS 版本：{snap.pymss_version or '—'}\n"
            f"Runtime 类型：{self._settings.get('runtime_variant', '外部/自动')}\n"
            f"当前设备：{snap.device or '—'}\n"
            f"当前模型：{snap.current_model or '—'}\n"
            f"安装位置：{snap.install_dir or '—'}\n"
            + ("\n".join(dependency_lines) if dependency_lines else "任务依赖：尚未检测")
            + (f"\n最近错误：{snap.error}" if snap.error else ""),
        )

    def _open_log_dir(self) -> None:
        configured = self._backend.log_directory()
        if configured:
            log_dir = Path(configured)
            log_dir.mkdir(parents=True, exist_ok=True)
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(log_dir)))
        else:
            show_fluent_info(self, "远程服务日志不在本机，暂无可打开的日志目录。")

    # ── 修复页动作 ────────────────────────────────────────────────
    def _on_repair(self) -> None:
        if ask_fluent_confirm(
            self,
            "修复会根据当前设备重新获取缺失或损坏的 Runtime 文件（CPU 约数百 MB；"
            "NVIDIA CUDA 最多约 3–4 GB），不会删除模型、MSST 映射或缓存。是否继续？",
            yes_text="修复安装",
        ):
            self._backend.repair_install()
            self.close()

    def _on_relocate(self) -> None:
        self._change_install_dir()
        self.close()

    def _on_reinstall(self) -> None:
        if ask_fluent_confirm(
            self,
            "将根据当前设备重新下载 PyMSS Runtime（CPU 约数百 MB；NVIDIA CUDA 约 3–4 GB），"
            "已下载且校验通过的模型会保留。是否继续？",
            yes_text="重新安装",
        ):
            self._backend.reinstall()
            self.close()

    def _on_remove_config(self) -> None:
        if ask_fluent_confirm(
            self,
            "只清除工作台中保存的安装路径与配置状态，不会删除磁盘上的任何文件。是否继续？",
            yes_text="移除配置",
        ):
            self._backend.remove_configuration()
            self._save_settings()
            self.close()
