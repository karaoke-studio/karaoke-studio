"""Style-preset library dialogs and N3 template import workflow."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from PyQt6.QtCore import QRect, QSize, Qt, pyqtSignal as Signal
from PyQt6.QtGui import QColor, QIcon, QPainter, QPixmap
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    ComboBox as FluentComboBox,
    EditableComboBox as FluentEditableComboBox,
    InfoBar,
    LineEdit as FluentLineEdit,
    ListWidget as FluentListWidget,
    PrimaryPushButton as FluentPrimaryPushButton,
    PushButton as FluentPushButton,
    SubtitleLabel,
)

from krok_helper.qfluent_compat import ModelessDialog
from krok_helper.subtitle_render.domain.models import (
    PRESET_REFERENCE_HEIGHT,
    StylePreset,
    SubtitleStyleScheme,
    rescale_scheme_font_sizes,
)
from krok_helper.subtitle_render.frontend.dialogs.fluent_dialogs import (
    fluent_button_row,
    fluent_choice,
    fluent_get_editable_choice,
    fluent_get_text,
    fluent_question,
    fluent_warning,
)
from krok_helper.subtitle_render.frontend.widgets.theme import palette, themed
from krok_helper.subtitle_render.n3.template_import import (
    N3_TEMPLATE_FILTER,
    N3_TEMPLATE_SOURCE_TYPE,
    default_n3_template_directories,
    find_n3_template_files,
    load_n3_font_templates,
    merge_n3_template_presets,
    resolve_n3_template_preset,
)


_PRESET_NO_GROUP = "\x00ungrouped"


def resolve_preset_for_target(
    preset: StylePreset,
    *,
    target_height: int,
    lyrics_dir: str | Path | None = None,
) -> StylePreset:
    """把预设解析到目标输出高度，供应用到工程时使用。

    N3 模板预设优先按保留的 payload 重新解析（精确）；其余预设按记录的
    ``reference_height`` 等比换算。返回的预设身份字段原样保留，仅 scheme
    换算到目标高度，``reference_height`` 同步改写为 ``target_height``。
    """

    if preset.source_type == N3_TEMPLATE_SOURCE_TYPE:
        resolved, _warnings = resolve_n3_template_preset(
            preset,
            target_height=target_height,
            lyrics_dir=lyrics_dir,
        )
        return resolved
    return StylePreset(
        name=preset.name,
        group=preset.group,
        scheme=rescale_scheme_font_sizes(
            deepcopy(preset.scheme),
            preset.reference_height,
            target_height,
        ),
        preset_id=preset.preset_id,
        source_type=preset.source_type,
        source_data=deepcopy(preset.source_data),
        reference_height=max(1, int(target_height)),
    )


def _normalize_style_presets(
    presets: dict[str, StylePreset | SubtitleStyleScheme],
) -> dict[str, StylePreset]:
    """Normalize preset mappings while preserving stable preset identities.

    Legacy mappings use the preset name as the key.  New mappings use a stable
    ``preset_id`` so equal names can coexist in different groups.
    """
    result: dict[str, StylePreset] = {}
    for raw_id, value in presets.items():
        fallback = str(raw_id).strip()
        if isinstance(value, StylePreset):
            name = str(value.name).strip() or fallback
            if not name:
                continue
            preset_id = str(value.preset_id).strip() or fallback
            if not preset_id or preset_id in result:
                preset_id = uuid4().hex
            result[preset_id] = StylePreset(
                name=name,
                group=str(value.group).strip(),
                scheme=deepcopy(value.scheme),
                preset_id=preset_id,
                source_type=str(value.source_type).strip(),
                source_data=deepcopy(value.source_data),
                reference_height=value.reference_height,
            )
        elif isinstance(value, SubtitleStyleScheme):
            name = fallback
            if not name:
                continue
            preset_id = fallback if fallback not in result else uuid4().hex
            result[preset_id] = StylePreset(
                name=name,
                scheme=deepcopy(value),
                preset_id=preset_id,
            )
    return result


def _new_preset_id(presets: dict[str, StylePreset], preferred: str = "") -> str:
    candidate = str(preferred).strip()
    if candidate and candidate not in presets:
        return candidate
    while True:
        candidate = uuid4().hex
        if candidate not in presets:
            return candidate


def _preset_ids_for_pair(
    presets: dict[str, StylePreset], name: str, group: str
) -> list[str]:
    normalized_name = str(name).strip()
    normalized_group = str(group).strip()
    return [
        preset_id
        for preset_id, preset in presets.items()
        if preset.name == normalized_name and preset.group == normalized_group
    ]


class _StylePresetDetailsDialog(ModelessDialog):
    """Fluent form for a preset name and optional organizational group."""

    def __init__(
        self,
        *,
        name: str,
        group: str,
        groups: list[str],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent.window() if parent is not None else None)
        self.setWindowTitle("保存到软件预设库")
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.setMinimumWidth(440)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(10)
        hint = CaptionLabel("保存后可在其他项目中复用该角色方案。", self)
        hint.setWordWrap(True)
        layout.addWidget(hint)

        layout.addWidget(BodyLabel("预设名称", self))
        self.name_edit = FluentLineEdit(self)
        self.name_edit.setText(name)
        self.name_edit.setPlaceholderText("输入唯一的预设名称")
        self.name_edit.selectAll()
        layout.addWidget(self.name_edit)

        layout.addWidget(BodyLabel("分组", self))
        self.group_combo = FluentEditableComboBox(self)
        self.group_combo.setClearButtonEnabled(True)
        self.group_combo.setPlaceholderText("留空则归入未分组")
        for value in groups:
            if value:
                self.group_combo.addItem(value)
        self.group_combo.setText(group)
        layout.addWidget(self.group_combo)

        button_row, self.ok_button, _cancel_button = fluent_button_row(
            self, ok_text="保存", cancel_text="取消"
        )
        layout.addLayout(button_row)
        self.name_edit.textChanged.connect(
            lambda text: self.ok_button.setEnabled(bool(text.strip()))
        )
        self.ok_button.setEnabled(bool(name.strip()))

    def details(self) -> tuple[str, str]:
        return self.name_edit.text().strip(), self.group_combo.text().strip()


class _RolePresetGroupDialog(ModelessDialog):
    """Resolve cross-group preset-name collisions one imported role at a time."""

    def __init__(
        self,
        candidates: dict[str, list[StylePreset]],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent.window() if parent is not None else None)
        self.setWindowTitle("选择角色预设分组")
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.setMinimumWidth(520)
        self._combos: dict[str, FluentComboBox] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)
        layout.addWidget(SubtitleLabel("选择角色预设分组", self))
        prompt = BodyLabel(
            "多个分组中存在以下角色，请分别选择想要应用的分组。", self
        )
        prompt.setWordWrap(True)
        layout.addWidget(prompt)

        rows = QWidget(self)
        grid = QGridLayout(rows)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(10)
        grid.addWidget(CaptionLabel("角色", rows), 0, 0)
        grid.addWidget(CaptionLabel("应用分组", rows), 0, 1)
        for row, (role_name, presets) in enumerate(candidates.items(), start=1):
            grid.addWidget(BodyLabel(role_name, rows), row, 0)
            combo = FluentComboBox(rows)
            combo.setMinimumWidth(260)
            combo.addItem("请选择分组", userData=None)
            for preset in presets:
                combo.addItem(
                    preset.group or "（未分组）",
                    userData=preset.preset_id,
                )
            combo.currentIndexChanged.connect(self._sync_apply_enabled)
            grid.addWidget(combo, row, 1)
            self._combos[role_name] = combo
        grid.setColumnStretch(1, 1)
        layout.addWidget(rows)

        button_row, self.apply_button, _cancel_button = fluent_button_row(
            self, ok_text="应用", cancel_text="取消"
        )
        layout.addLayout(button_row)
        self._sync_apply_enabled()

    def _sync_apply_enabled(self, *_args: Any) -> None:
        self.apply_button.setEnabled(
            bool(self._combos)
            and all(combo.currentData() is not None for combo in self._combos.values())
        )

    def selected_preset_ids(self) -> dict[str, str]:
        return {
            role_name: str(combo.currentData())
            for role_name, combo in self._combos.items()
            if combo.currentData() is not None
        }


class StylePresetManagerDialog(ModelessDialog):
    """Manage independent, grouped subtitle style presets."""

    presetLibraryChanged = Signal(dict)

    def __init__(
        self,
        presets: dict[str, StylePreset | SubtitleStyleScheme],
        current_scheme: SubtitleStyleScheme,
        target_label: str,
        existing_role_names: Optional[set[str]] = None,
        target_height: int = 1080,
        lyrics_dir: Optional[Path] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("样式预设库")
        self.resize(640, 520)
        self._target_height = max(1, int(target_height))
        self._lyrics_dir = Path(lyrics_dir) if lyrics_dir is not None else None
        self._presets = {}
        for preset_id, preset in _normalize_style_presets(presets).items():
            resolved, _warnings = resolve_n3_template_preset(
                preset,
                target_height=self._target_height,
                lyrics_dir=self._lyrics_dir,
            )
            resolved.preset_id = preset_id
            self._presets[preset_id] = resolved
        self._current_scheme = deepcopy(current_scheme)
        self._target_label = str(target_label)
        self._existing_role_names = set(existing_role_names or set())
        self._applied_scheme: Optional[SubtitleStyleScheme] = None
        self._imported_schemes: dict[str, StylePreset] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(10)

        title = SubtitleLabel("样式预设库", self)
        themed(
            title,
            lambda: (
                f"color: {palette().title_text};"
                "font-size: 15pt;"
                "font-weight: 600;"
            ),
        )
        layout.addWidget(title)
        layout.addWidget(BodyLabel(f"当前目标：{target_label}", self))

        filter_row = QHBoxLayout()
        filter_row.setContentsMargins(0, 0, 0, 0)
        filter_row.setSpacing(8)
        filter_row.addWidget(BodyLabel("过滤:", self))
        self._filter_edit = FluentLineEdit(self)
        self._filter_edit.setPlaceholderText("输入名称搜索...")
        self._filter_edit.textChanged.connect(self._apply_filter)
        filter_row.addWidget(self._filter_edit, 1)
        filter_row.addWidget(BodyLabel("分组:", self))
        self._group_filter = FluentComboBox(self)
        self._group_filter.setMinimumWidth(120)
        self._group_filter.currentIndexChanged.connect(self._apply_filter)
        filter_row.addWidget(self._group_filter)
        layout.addLayout(filter_row)

        self._preset_list = FluentListWidget(self)
        self._preset_list.setIconSize(QSize(34, 20))
        self._preset_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._preset_list.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._preset_list.setTextElideMode(Qt.TextElideMode.ElideRight)
        self._preset_list.currentItemChanged.connect(lambda _cur, _old: self._sync_buttons())
        self._preset_list.itemChanged.connect(self._on_preset_item_changed)
        layout.addWidget(self._preset_list, 1)

        selection_row = QHBoxLayout()
        selection_row.setContentsMargins(0, 0, 0, 0)
        self._select_all_btn = FluentPushButton("全选当前结果", self)
        self._select_all_btn.clicked.connect(self._select_all_visible)
        self._clear_selection_btn = FluentPushButton("全部取消", self)
        self._clear_selection_btn.clicked.connect(self._clear_checks)
        self._selection_stats = CaptionLabel("", self)
        selection_row.addWidget(self._select_all_btn)
        selection_row.addWidget(self._clear_selection_btn)
        selection_row.addStretch(1)
        selection_row.addWidget(self._selection_stats)
        layout.addLayout(selection_row)

        self._empty_label = BodyLabel("暂无预设。可以把当前样式保存为新预设。", self)
        layout.addWidget(self._empty_label)

        edit_row = QHBoxLayout()
        edit_row.setContentsMargins(0, 0, 0, 0)
        edit_row.setSpacing(8)
        self._import_n3_btn = FluentPushButton("从 N3 导入", self)
        self._import_n3_btn.clicked.connect(self._on_import_n3)
        self._save_current_btn = FluentPushButton("保存当前样式为预设", self)
        self._save_current_btn.clicked.connect(self._on_save_current)
        self._rename_btn = FluentPushButton("重命名", self)
        self._rename_btn.clicked.connect(self._on_rename)
        self._set_group_btn = FluentPushButton("设置分组", self)
        self._set_group_btn.clicked.connect(self._on_set_group)
        self._delete_btn = FluentPushButton("删除", self)
        self._delete_btn.clicked.connect(self._on_delete)
        edit_row.addWidget(self._import_n3_btn)
        edit_row.addWidget(self._save_current_btn)
        edit_row.addWidget(self._rename_btn)
        edit_row.addWidget(self._set_group_btn)
        edit_row.addWidget(self._delete_btn)
        layout.addLayout(edit_row)

        button_row = QHBoxLayout()
        button_row.setContentsMargins(0, 4, 0, 0)
        button_row.addStretch(1)
        self._apply_btn = FluentPushButton("应用到当前目标", self)
        self._apply_btn.clicked.connect(self._on_apply)
        self._import_btn = FluentPrimaryPushButton("导入选中项为项目角色", self)
        self._import_btn.clicked.connect(self._on_import_selected)
        close_btn = FluentPushButton("关闭", self)
        close_btn.clicked.connect(self.accept)
        button_row.addWidget(self._apply_btn)
        button_row.addWidget(self._import_btn)
        button_row.addWidget(close_btn)
        layout.addLayout(button_row)

        self._populate_list()

    def preset_schemes(self) -> dict[str, StylePreset]:
        return _normalize_style_presets(self._presets)

    def _emit_preset_library_changed(self) -> None:
        """Publish library edits immediately instead of waiting for dialog close."""
        self.presetLibraryChanged.emit(self.preset_schemes())

    def applied_scheme(self) -> Optional[SubtitleStyleScheme]:
        if self._applied_scheme is None:
            return None
        return deepcopy(self._applied_scheme)

    def imported_schemes(self) -> dict[str, StylePreset]:
        return _normalize_style_presets(self._imported_schemes)

    def add_preset(self, name: str, group: str = "", *, overwrite: bool = False) -> bool:
        name = str(name).strip()
        group = str(group).strip()
        if not name:
            return False
        matches = _preset_ids_for_pair(self._presets, name, group)
        if matches and not overwrite:
            return False
        preset_id = matches[0] if matches else _new_preset_id(self._presets, name)
        self._presets[preset_id] = StylePreset(
            name=name,
            group=group,
            # 预设库统一存基准高度的值：库是跨项目共享的，记录像素对应的
            # 高度后，应用到任意输出高度的项目都能等比还原。
            scheme=rescale_scheme_font_sizes(
                deepcopy(self._current_scheme),
                self._target_height,
                PRESET_REFERENCE_HEIGHT,
            ),
            preset_id=preset_id,
            reference_height=PRESET_REFERENCE_HEIGHT,
        )
        self._populate_list(selected=preset_id)
        self._emit_preset_library_changed()
        return True

    def _populate_list(
        self,
        selected: Optional[str] = None,
    ) -> None:
        checked = set(self._checked_names()) if self._preset_list.count() else set()
        current = selected or self._selected_name()
        self._refresh_group_filter()
        self._preset_list.blockSignals(True)
        self._preset_list.clear()
        for preset_id, preset in self._presets.items():
            group_text = f" [{preset.group}]" if preset.group else ""
            existing_text = (
                "  （项目中已存在）"
                if preset.name in self._existing_role_names
                else ""
            )
            item = QListWidgetItem()
            item.setText(
                f"{preset.name}{group_text}{existing_text}    "
                f"{self._scheme_summary(preset.scheme)}"
            )
            item.setIcon(_scheme_icon(preset.scheme))
            item.setData(Qt.ItemDataRole.UserRole, preset_id)
            item.setData(Qt.ItemDataRole.UserRole + 1, preset.group)
            if preset.name not in self._existing_role_names:
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(
                    Qt.CheckState.Checked
                    if preset_id in checked
                    else Qt.CheckState.Unchecked
                )
            else:
                item.setCheckState(Qt.CheckState.Unchecked)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
            self._preset_list.addItem(item)
            if current == preset_id:
                self._preset_list.setCurrentItem(item)
        self._preset_list.blockSignals(False)
        if self._preset_list.currentItem() is None and self._preset_list.count() > 0:
            self._preset_list.setCurrentRow(0)
        self._apply_filter()
        self._sync_buttons()

    def _refresh_group_filter(self) -> None:
        current = self._group_filter.currentData()
        groups = sorted({preset.group for preset in self._presets.values() if preset.group})
        has_ungrouped = any(not preset.group for preset in self._presets.values())
        blocked = self._group_filter.blockSignals(True)
        self._group_filter.clear()
        self._group_filter.addItem("全部分组", userData=None)
        if has_ungrouped:
            self._group_filter.addItem("（未分组）", userData=_PRESET_NO_GROUP)
        for group in groups:
            self._group_filter.addItem(group, userData=group)
        index = self._group_filter.findData(current)
        self._group_filter.setCurrentIndex(index if index >= 0 else 0)
        self._group_filter.blockSignals(blocked)

    def _apply_filter(self, *_args: Any) -> None:
        needle = self._filter_edit.text().strip().lower()
        group_filter = self._group_filter.currentData()
        visible_count = 0
        for i in range(self._preset_list.count()):
            item = self._preset_list.item(i)
            preset_id = str(item.data(Qt.ItemDataRole.UserRole) or "")
            preset = self._presets.get(preset_id)
            name = preset.name if preset is not None else ""
            group = str(item.data(Qt.ItemDataRole.UserRole + 1) or "")
            name_matches = not needle or needle in name.lower()
            if group_filter == _PRESET_NO_GROUP:
                group_matches = not group
            else:
                group_matches = group_filter is None or group == str(group_filter)
            visible = name_matches and group_matches
            item.setHidden(not visible)
            if visible:
                visible_count += 1
        current = self._preset_list.currentItem()
        if current is not None and current.isHidden():
            replacement = next(
                (
                    self._preset_list.item(index)
                    for index in range(self._preset_list.count())
                    if not self._preset_list.item(index).isHidden()
                ),
                None,
            )
            self._preset_list.setCurrentItem(replacement)
        self._empty_label.setVisible(visible_count == 0)
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        current = self._selected_name()
        checked_count = len(self._checked_names())
        has_batch_target = checked_count > 0 or current is not None
        self._apply_btn.setEnabled(current is not None)
        self._import_btn.setEnabled(checked_count > 0)
        self._rename_btn.setEnabled(current is not None)
        self._set_group_btn.setEnabled(has_batch_target)
        self._delete_btn.setEnabled(has_batch_target)
        self._selection_stats.setText(
            f"已选 {checked_count}/{self._preset_list.count()}"
        )

    def _on_preset_item_changed(self, item: QListWidgetItem) -> None:
        """勾选预设时同步「应用」所使用的当前行。

        复选框原本只服务于批量导入，而「应用到当前目标」只读
        ``currentItem()``。Qt 点击复选框不会自动切换 current item，
        导致界面勾选 B 却应用了旧高亮项 A。
        """
        if item.checkState() == Qt.CheckState.Checked:
            self._preset_list.setCurrentItem(item)
        self._sync_buttons()

    def _selected_name(self) -> Optional[str]:
        item = self._preset_list.currentItem()
        if item is None:
            return None
        name = item.data(Qt.ItemDataRole.UserRole)
        return str(name) if name is not None else None

    def _checked_names(self) -> list[str]:
        names: list[str] = []
        for index in range(self._preset_list.count()):
            item = self._preset_list.item(index)
            if item.checkState() == Qt.CheckState.Checked:
                name = item.data(Qt.ItemDataRole.UserRole)
                if name is not None:
                    names.append(str(name))
        return names

    def _batch_names(self) -> list[str]:
        checked = self._checked_names()
        if checked:
            return checked
        current = self._selected_name()
        return [current] if current is not None else []

    def _select_all_visible(self) -> None:
        self._preset_list.blockSignals(True)
        for index in range(self._preset_list.count()):
            item = self._preset_list.item(index)
            if not item.isHidden() and item.flags() & Qt.ItemFlag.ItemIsUserCheckable:
                item.setCheckState(Qt.CheckState.Checked)
        self._preset_list.blockSignals(False)
        self._sync_buttons()

    def _clear_checks(self) -> None:
        self._preset_list.blockSignals(True)
        for index in range(self._preset_list.count()):
            item = self._preset_list.item(index)
            if item.flags() & Qt.ItemFlag.ItemIsUserCheckable:
                item.setCheckState(Qt.CheckState.Unchecked)
        self._preset_list.blockSignals(False)
        self._sync_buttons()

    def _scheme_summary(self, scheme: SubtitleStyleScheme) -> str:
        fill = scheme.fill_color or "#FFFFFF"
        base = scheme.base_color or "#FFFFFF"
        font_size = scheme.font_size_px
        font = scheme.font_family or "继承字体"
        size_text = f" {font_size}px" if font_size is not None else ""
        return f"{font}{size_text} · 已唱 {fill} / 未唱 {base}"

    def _on_save_current(self) -> None:
        suggested_name = self._target_label if self._target_label != "全局默认" else ""
        suggested_group = ""
        while True:
            details = self._prompt_preset_details(suggested_name, suggested_group)
            if details is None:
                return
            name, group = details
            if not name:
                InfoBar.warning(
                    title="未保存", content="请输入预设名称。", parent=self, duration=2000
                )
                suggested_group = group
                continue
            matches = _preset_ids_for_pair(self._presets, name, group)
            if not matches:
                self.add_preset(name, group)
                return
            decision = self._confirm_overwrite(matches[0])
            if decision == "overwrite":
                self.add_preset(name, group, overwrite=True)
                return
            if decision == "rename":
                suggested_name, suggested_group = name, group
                continue
            return

    def _on_import_n3(self) -> None:
        discovered = find_n3_template_files()
        if discovered:
            choice = fluent_choice(
                self,
                "从 N3 导入字体模板",
                f"已自动发现 {len(discovered)} 个有效扩展名的 N3 字体模板。请选择导入来源。",
                ("导入已发现模板", "选择模板文件", "选择模板目录", "取消"),
                default=0,
            )
            if choice == 0:
                selected: list[Path] = discovered
            elif choice == 1:
                selected = self._select_n3_template_files()
            elif choice == 2:
                selected = self._select_n3_template_directory()
            else:
                return
        else:
            choice = fluent_choice(
                self,
                "从 N3 导入字体模板",
                "没有自动发现 N3 的 TemplateFont 目录，请手动选择模板文件或目录。",
                ("选择模板文件", "选择模板目录", "取消"),
                default=0,
            )
            if choice == 0:
                selected = self._select_n3_template_files()
            elif choice == 1:
                selected = self._select_n3_template_directory()
            else:
                return
        if not selected:
            return

        batch = load_n3_font_templates(
            selected,
            target_height=self._target_height,
            lyrics_dir=self._lyrics_dir,
        )
        if not batch.templates and not batch.failed:
            InfoBar.warning(
                title="没有可导入模板",
                content="所选位置没有同步状态的 N3 字体模板。",
                parent=self,
                duration=3500,
            )
            return

        conflicts = sorted(
            {
                (item.name, str(item.preset.group).strip())
                for item in batch.templates
                if _preset_ids_for_pair(
                    self._presets, item.name, str(item.preset.group).strip()
                )
            }
        )
        policy = "overwrite"
        if conflicts:
            shown = "、".join(
                f"{name} [{group}]" if group else name
                for name, group in conflicts[:5]
            )
            if len(conflicts) > 5:
                shown += f" 等 {len(conflicts)} 项"
            decision = fluent_choice(
                self,
                "同名 N3 字体模板",
                f"以下模板与预设库重名：{shown}\n请选择本批次统一处理方式。",
                ("覆盖", "自动改名", "跳过", "取消导入"),
                default=2,
            )
            if decision == 0:
                policy = "overwrite"
            elif decision == 1:
                policy = "rename"
            elif decision == 2:
                policy = "skip"
            else:
                return

        merged = merge_n3_template_presets(
            self._presets, batch.templates, conflict_policy=policy
        )
        self._presets = merged.presets
        selected_id = merged.imported_ids[-1] if merged.imported_ids else None
        # Keep newly imported templates unchecked. Importing templates updates the
        # reusable preset library; creating project roles remains an explicit choice.
        self._populate_list(selected=selected_id)
        if merged.imported_ids:
            self._emit_preset_library_changed()

        warning_count = sum(len(item.warnings) for item in batch.templates)
        summary = (
            f"成功 {len(merged.imported_names)} 个，跳过 "
            f"{len(batch.skipped) + len(merged.skipped_names)} 个，失败 {len(batch.failed)} 个"
        )
        if batch.failed:
            details = "\n".join(f"{path.name}：{reason}" for path, reason in batch.failed[:8])
            fluent_warning(
                self,
                "N3 模板导入完成",
                f"{summary}\n\n{details}",
                copyable=True,
            )
        elif warning_count:
            InfoBar.warning(
                title="N3 模板已导入",
                content=f"{summary}；另有 {warning_count} 条素材或字段提示。",
                parent=self,
                duration=5000,
            )
        else:
            InfoBar.success(
                title="N3 模板已导入", content=summary, parent=self, duration=3500
            )

    def _n3_dialog_start_directory(self) -> str:
        directories = default_n3_template_directories()
        return str(directories[0]) if directories else str(Path.home())

    def _select_n3_template_files(self) -> list[Path]:
        paths, _filter = QFileDialog.getOpenFileNames(
            self,
            "选择 N3 字体模板",
            self._n3_dialog_start_directory(),
            N3_TEMPLATE_FILTER,
        )
        return [Path(path) for path in paths]

    def _select_n3_template_directory(self) -> list[Path]:
        path = QFileDialog.getExistingDirectory(
            self,
            "选择 N3 字体模板目录",
            self._n3_dialog_start_directory(),
        )
        return [Path(path)] if path else []

    def _prompt_preset_details(
        self, suggested_name: str, suggested_group: str
    ) -> Optional[tuple[str, str]]:
        groups = [""] + sorted(
            {preset.group for preset in self._presets.values() if preset.group}
        )
        dialog = _StylePresetDetailsDialog(
            name=suggested_name,
            group=suggested_group,
            groups=groups,
            parent=self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return dialog.details()

    def _confirm_overwrite(self, preset_id: str) -> str:
        existing = self._presets[preset_id]
        name = existing.name
        group_text = existing.group or "未分组"
        choice = fluent_choice(
            self,
            "同名样式预设",
            f"样式预设“{name}”已存在，位于“{group_text}”。\n"
            f"是否用当前目标“{self._target_label}”的样式覆盖它？",
            ("覆盖现有预设", "返回修改名称", "取消"),
            default=2,
        )
        if choice == 0:
            return "overwrite"
        if choice == 1:
            return "rename"
        return "cancel"

    def _on_apply(self) -> None:
        preset_id = self._selected_name()
        if preset_id is None:
            InfoBar.warning(title="未选择", content="请先选择一个预设。", parent=self, duration=2000)
            return
        self._applied_scheme = resolve_preset_for_target(
            self._presets[preset_id],
            target_height=self._target_height,
            lyrics_dir=self._lyrics_dir,
        ).scheme
        self.accept()

    def _on_import_selected(self) -> None:
        preset_ids = self._checked_names()
        if not preset_ids:
            InfoBar.warning(title="未选择", content="请先选择一个预设。", parent=self, duration=2000)
            return
        selected_names = [
            self._presets[preset_id].name
            for preset_id in preset_ids
            if preset_id in self._presets
        ]
        duplicate_names = sorted(
            {name for name in selected_names if selected_names.count(name) > 1}
        )
        if duplicate_names:
            InfoBar.warning(
                title="存在同名预设",
                content=(
                    "同名预设不能同时导入为项目角色，请只选择其中一个："
                    + "、".join(duplicate_names)
                ),
                parent=self,
                duration=3000,
            )
            return
        self._imported_schemes = {
            preset_id: resolve_preset_for_target(
                self._presets[preset_id],
                target_height=self._target_height,
                lyrics_dir=self._lyrics_dir,
            )
            for preset_id in preset_ids
            if preset_id in self._presets
        }
        self.accept()

    def _on_rename(self) -> None:
        preset_id = self._selected_name()
        if preset_id is None:
            return
        preset = self._presets[preset_id]
        old = preset.name
        new, ok = fluent_get_text(
            self,
            "重命名样式预设",
            "预设名称",
            text=old,
            placeholder="输入唯一的预设名称",
        )
        if not ok:
            return
        new = new.strip()
        if not new or new == old:
            return
        if _preset_ids_for_pair(self._presets, new, preset.group):
            InfoBar.warning(
                title="名称已存在",
                content=(
                    f"分组“{preset.group or '未分组'}”中已经存在样式预设“{new}”。"
                ),
                parent=self,
                duration=2000,
            )
            return
        self._presets[preset_id] = StylePreset(
            name=new,
            group=preset.group,
            scheme=deepcopy(preset.scheme),
            preset_id=preset_id,
            source_type=preset.source_type,
            source_data=deepcopy(preset.source_data),
            reference_height=preset.reference_height,
        )
        self._populate_list(selected=preset_id)
        self._emit_preset_library_changed()

    def _on_set_group(self) -> None:
        preset_ids = self._batch_names()
        if not preset_ids:
            return
        groups = [""] + sorted(
            {preset.group for preset in self._presets.values() if preset.group}
        )
        current_group = (
            self._presets[preset_ids[0]].group if len(preset_ids) == 1 else ""
        )
        group, ok = fluent_get_editable_choice(
            self,
            "设置分组",
            "分组（留空则移到未分组）",
            groups,
            text=current_group,
            placeholder="留空则移到未分组",
        )
        if not ok:
            return
        group = str(group).strip()
        selected = set(preset_ids)
        moved_names = [self._presets[preset_id].name for preset_id in preset_ids]
        duplicate_names = {name for name in moved_names if moved_names.count(name) > 1}
        conflicts = set(duplicate_names)
        for preset_id in preset_ids:
            preset = self._presets[preset_id]
            if any(
                other_id not in selected
                and other.name == preset.name
                and other.group == group
                for other_id, other in self._presets.items()
            ):
                conflicts.add(preset.name)
        if conflicts:
            InfoBar.warning(
                title="分组中存在同名预设",
                content="、".join(sorted(conflicts)),
                parent=self,
                duration=2500,
            )
            return
        for preset_id in preset_ids:
            preset = self._presets.get(preset_id)
            if preset is not None:
                preset.group = group
        self._populate_list(selected=preset_ids[0])
        self._emit_preset_library_changed()

    def _on_delete(self) -> None:
        preset_ids = self._batch_names()
        if not preset_ids:
            return
        names = [self._presets[preset_id].name for preset_id in preset_ids]
        name_text = "、".join(names[:5])
        if len(names) > 5:
            name_text += f" 等 {len(names)} 个"
        confirmed = fluent_question(
            self,
            "删除预设",
            f"确定要从样式预设库删除“{name_text}”吗？\n"
            "当前工程中的同名角色和样式不会受到影响。",
            yes_text="删除",
            no_text="取消",
            default_cancel=True,
        )
        if not confirmed:
            return
        for preset_id in preset_ids:
            self._presets.pop(preset_id, None)
        self._populate_list()
        self._emit_preset_library_changed()


def _scheme_icon(scheme: SubtitleStyleScheme) -> QIcon:
    before = QColor(scheme.base_color or "#FFFFFF")
    after = QColor(scheme.fill_color or "#FF5A6F")
    pixmap = QPixmap(34, 20)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.fillRect(QRect(0, 0, 17, 20), before)
    painter.fillRect(QRect(17, 0, 17, 20), after)
    painter.setPen(QColor("#000000"))
    painter.drawRect(0, 0, 33, 19)
    painter.end()
    return QIcon(pixmap)

