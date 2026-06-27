"""左侧歌词面板（拖拽 + qfluentwidgets 表格列表）。

UI 设计：

- **空态**：居中显示"拖入字幕文件 / 点击此处选择"，受 :class:`DropPanel` 接管
- **载入后**：``TableWidget``（qfluentwidgets），三列——角色 / 特效 / 内容，显示水平表头。
  角色列可编辑（下拉选择配色方案），特效列留空占位，内容列只读。
  行距紧凑，仿 SUG line_interface 的 TableWidget 用法。
"""

from __future__ import annotations

from collections import Counter
from typing import Optional

from PyQt6.QtCore import Qt, pyqtSignal as Signal
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QStyledItemDelegate,
    QTableWidgetItem,
    QWidget,
)
from qfluentwidgets import ComboBox as FluentComboBox
from qfluentwidgets import TableWidget as FluentTableWidget

from krok_helper.subtitle_render.frontend.drop_panel import DropPanel
from krok_helper.subtitle_render.models import TimingTrack
from krok_helper.subtitle_render.frontend.theme import palette, themed

COL_ROLE = 0
COL_EFFECT = 1
COL_CONTENT = 2

_COLUMN_HEADERS = ["角色", "特效", "内容"]


class _ReadOnlyDelegate(QStyledItemDelegate):
    """禁止编辑的委托——内容列使用。"""

    def createEditor(self, parent, option, index):  # type: ignore[override]
        return None


class _RoleComboDelegate(QStyledItemDelegate):
    """为角色列提供 qfluentwidgets 风格下拉选择框。"""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._role_options: list[str] = []

    def set_role_options(self, options: list[str]) -> None:
        self._role_options = list(options)

    def createEditor(self, parent, option, index):  # type: ignore[override]
        combo = FluentComboBox(parent)
        combo.setFixedHeight(28)
        combo.addItem("（默认）", "")
        for name in self._role_options:
            combo.addItem(name, name)
        # activated 仅用户交互触发，避免编程填充时 emit commitData
        combo.activated.connect(lambda _idx: self.commitData.emit(combo))  # type: ignore[arg-type]
        return combo

    def setEditorData(self, editor, index):  # type: ignore[override]
        current = index.data(Qt.ItemDataRole.UserRole) or ""
        for i in range(editor.count()):
            if editor.itemData(i) == current:
                editor.setCurrentIndex(i)
                return
        editor.setCurrentIndex(0)

    def setModelData(self, editor, model, index):  # type: ignore[override]
        value = editor.currentData() or ""
        display = value if value else "（默认）"
        model.setData(index, value, Qt.ItemDataRole.UserRole)
        model.setData(index, display, Qt.ItemDataRole.DisplayRole)


class LyricsPanel(DropPanel):
    """左侧歌词面板（含空态拖拽 + 已加载表格两态）。"""

    roleChanged = Signal(int, str)
    rowClicked = Signal(int)  # 用户点击歌词行时发出行号

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(
            extensions={".lrc"},
            empty_title="拖入字幕文件",
            empty_hint="拖入 SUG 导出的 Nicokara 逐字 LRC（.lrc）\n或点击此处选择",
            empty_icon="📝",
            parent=parent,
        )
        self.setObjectName("LyricsPanel")
        themed(self, self._panel_qss)

        # ---- qfluentwidgets TableWidget ----
        self._table = FluentTableWidget(self)
        self._table.setObjectName("LyricsTable")
        # 仿 SUG: 列数 + 表头
        self._table.setColumnCount(3)
        self._table.setHorizontalHeaderLabels(_COLUMN_HEADERS)

        self._table.setFrameShape(FluentTableWidget.Shape.NoFrame)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked
                                    | QAbstractItemView.EditTrigger.EditKeyPressed)
        self._table.setShowGrid(False)
        self._table.setWordWrap(False)

        # 行高紧凑
        self._table.verticalHeader().setVisible(False)
        self._table.verticalHeader().setDefaultSectionSize(28)

        # 列宽：角色 / 特效 固定，内容撑满
        hh = self._table.horizontalHeader()
        hh.setSectionResizeMode(COL_ROLE, QHeaderView.ResizeMode.Fixed)
        hh.setSectionResizeMode(COL_EFFECT, QHeaderView.ResizeMode.Fixed)
        hh.setSectionResizeMode(COL_CONTENT, QHeaderView.ResizeMode.Stretch)
        self._table.setColumnWidth(COL_ROLE, 70)
        self._table.setColumnWidth(COL_EFFECT, 64)

        # 角色列 → FluentComboBox 委托；内容列 → 只读委托
        self._role_delegate = _RoleComboDelegate(self)
        self._readonly_delegate = _ReadOnlyDelegate(self)
        self._table.setItemDelegateForColumn(COL_ROLE, self._role_delegate)
        self._table.setItemDelegateForColumn(COL_CONTENT, self._readonly_delegate)

        self.set_content(self._table)

        # 代理编辑后通知宿主
        self._table.itemChanged.connect(self._on_item_changed)
        # 点击行 → 跳转预览
        self._table.cellClicked.connect(lambda row, _col: self.rowClicked.emit(row))

    # ------------------------------------------------------------------ public

    def set_role_options(self, options: list[str]) -> None:
        """设置可选的配色方案 / 角色名列表。"""
        self._role_delegate.set_role_options(list(options))

    def set_track(self, track: Optional[TimingTrack]) -> None:
        """加载 / 清空字幕。``None`` / 无行时回到空态。"""
        self._table.blockSignals(True)
        try:
            self._table.setRowCount(0)
            if track is None or not track.lines:
                self.set_populated(False)
                return

            num_rows = len(track.lines)
            self._table.setRowCount(num_rows)
            for row, line in enumerate(track.lines):
                text = "".join(c.text for c in line.chars)
                role = _dominant_role(line)

                role_item = QTableWidgetItem(role if role else "（默认）")
                role_item.setData(Qt.ItemDataRole.UserRole, role)
                role_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if line.is_blank:
                    role_item.setFlags(Qt.ItemFlag.NoItemFlags)
                self._table.setItem(row, COL_ROLE, role_item)

                effect_item = QTableWidgetItem("")
                effect_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if line.is_blank:
                    effect_item.setFlags(Qt.ItemFlag.NoItemFlags)
                self._table.setItem(row, COL_EFFECT, effect_item)

                content_item = QTableWidgetItem(text if not line.is_blank else "")
                content_item.setFlags(
                    content_item.flags() & ~Qt.ItemFlag.ItemIsEditable
                )
                if line.is_blank:
                    content_item.setFlags(Qt.ItemFlag.NoItemFlags)
                self._table.setItem(row, COL_CONTENT, content_item)
        finally:
            self._table.blockSignals(False)

        self.set_populated(True)

    @property
    def list_widget(self):
        """向后兼容。"""
        return self._table

    @property
    def table_widget(self) -> FluentTableWidget:
        return self._table

    # ------------------------------------------------------------------ private

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if item.column() != COL_ROLE:
            return
        role_name = str(item.data(Qt.ItemDataRole.UserRole) or "")
        self.roleChanged.emit(item.row(), role_name)

    def _panel_qss(self) -> str:
        p = palette()
        if self._drag_state == "accept":
            border_color = p.accent_primary
            border_width = 2
            border_style = "dashed"
        elif self._drag_state == "reject":
            border_color = "#E53935"
            border_width = 2
            border_style = "dashed"
        elif not self._populated:
            border_color = p.card_border
            border_width = 1
            border_style = "dashed"
        else:
            border_color = "transparent"
            border_width = 0
            border_style = "solid"
        return (
            f"#LyricsPanel {{ background-color: {p.card_bg}; "
            f"border: {border_width}px {border_style} {border_color}; "
            f"border-radius: 0; }}"
        )


def _dominant_role(line) -> str:
    """返回本行出现次数最多的角色标签；无角色时返回空字符串。"""
    roles = [ch.role_label for ch in line.chars if ch.role_label]
    if not roles:
        return ""
    return Counter(roles).most_common(1)[0][0]
