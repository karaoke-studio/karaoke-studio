"""左侧歌词面板（拖拽 + qfluentwidgets 表格列表）。

UI 设计：

- **空态**：居中显示"拖入字幕文件 / 点击此处选择"，受 :class:`DropPanel` 接管
- **载入后**：``TableWidget``（qfluentwidgets），三列——轨 / 角色 / 内容。

  - **轨**：多行布局下按实际渲染 lane（非空行序号 % 行数）标 T1 / T2 / …，
    同一组行共享一个浅色底，直观呈现"按页贴在一起"的显示分组
  - **角色**：可编辑（下拉选择配色方案），名字前带该方案的颜色色点
  - **内容**：只读；水平对齐跟随布局设置（asymmetric 按每行对齐列表 / center
    居中 / per_row 按行独立对齐），布局改动即时反映到列表
  - 空行渲染成矮分隔行（间奏 ♪），不再占一整行空白
"""

from __future__ import annotations

from collections import Counter
from typing import Optional

from dataclasses import replace as _dataclass_replace

from PyQt6.QtCore import QPoint, Qt, QSize, pyqtSignal as Signal
from PyQt6.QtGui import QBrush, QColor, QIcon, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QMenu,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableWidgetItem,
    QWidget,
)
from qfluentwidgets import ComboBox as FluentComboBox
from qfluentwidgets import TableWidget as FluentTableWidget

from krok_helper.subtitle_render.engine.timeline import (
    assign_lanes,
    paragraph_last_line_flags,
)
from krok_helper.subtitle_render.frontend.drop_panel import DropPanel
from krok_helper.subtitle_render.models import (
    LYRICS_LAYOUT_FIELDS,
    Style,
    TimingLine,
    TimingTrack,
)
from krok_helper.subtitle_render.frontend.theme import palette, themed

COL_LANE = 0
COL_ROLE = 1
COL_CONTENT = 2

_COLUMN_HEADERS = ["轨", "角色", "内容"]

_ROW_HEIGHT = 34
_BLANK_ROW_HEIGHT = 18
_DEFAULT_ROLE_TEXT = "（默认）"


def _swatch_icon(color: Optional[QColor]) -> QIcon:
    """圆形配色色点；``None``（未定义方案）画空心灰圈。"""
    pixmap = QPixmap(24, 24)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    if color is None:
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(148, 163, 184, 200), 2))
    else:
        painter.setBrush(color)
        # 描一圈半透明深边，浅色（白）色点在白底上也能看清
        painter.setPen(QPen(QColor(0, 0, 0, 64), 1.5))
    painter.drawEllipse(5, 5, 14, 14)
    painter.end()
    return QIcon(pixmap)


def _scheme_swatch_color(style: Style, role: str) -> Optional[QColor]:
    """角色名 → 该配色方案的代表色（已唱填充色）；无方案返回 None。"""
    if not role:
        # 全局默认方案
        if style.karaoke_colors is not None:
            return QColor(style.karaoke_colors.after.text.color)
        return QColor(style.fill_color)
    scheme = style.custom_style_schemes.get(role)
    if scheme is None:
        return None
    if scheme.karaoke_colors is not None:
        return QColor(scheme.karaoke_colors.after.text.color)
    if scheme.fill_color:
        return QColor(scheme.fill_color)
    return None


class _GroupBackgroundDelegate(QStyledItemDelegate):
    """在 QSS item 背景之上自绘"双行组"底色的基类。

    qfluentwidgets 的 TableWidget QSS 会压制 item 的 ``BackgroundRole``，
    所以组底色不能走 ``setBackground``，改在这里 fillRect。
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._group_bg_provider = None

    def set_group_bg_provider(self, provider) -> None:
        """``provider(row) -> Optional[QColor]``，返回该行的组底色。"""
        self._group_bg_provider = provider

    def paint(self, painter, option, index):  # type: ignore[override]
        # 选中态自己画：qfluentwidgets 表格 QSS 的 ::item:selected 会把文字刷成
        # 白色，压在浅底上完全看不清。剥掉 State_Selected（QSS 选中规则不再命中，
        # 文字保持正常深色），底色换成主题浅粉。
        opt = QStyleOptionViewItem(option)
        selected = bool(opt.state & QStyle.StateFlag.State_Selected)
        if selected:
            opt.state &= ~QStyle.StateFlag.State_Selected
        if self._group_bg_provider is not None:
            color = self._group_bg_provider(index.row())
            if color is not None:
                painter.fillRect(opt.rect, color)
        if selected:
            painter.fillRect(opt.rect, QColor(palette().preview_selection_bg))
        super().paint(painter, opt, index)


class _ReadOnlyDelegate(_GroupBackgroundDelegate):
    """禁止编辑的委托——轨 / 内容列使用。"""

    def createEditor(self, parent, option, index):  # type: ignore[override]
        return None


class _RoleComboDelegate(_GroupBackgroundDelegate):
    """为角色列提供 qfluentwidgets 风格下拉选择框（带配色色点）。"""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._role_options: list[str] = []
        self._style: Style = Style()

    def set_role_options(self, options: list[str]) -> None:
        self._role_options = list(options)

    def set_style(self, style: Style) -> None:
        self._style = style

    def createEditor(self, parent, option, index):  # type: ignore[override]
        combo = FluentComboBox(parent)
        combo.setFixedHeight(30)
        combo.addItem(
            _DEFAULT_ROLE_TEXT,
            icon=_swatch_icon(_scheme_swatch_color(self._style, "")),
            userData="",
        )
        for name in self._role_options:
            combo.addItem(
                name,
                icon=_swatch_icon(_scheme_swatch_color(self._style, name)),
                userData=name,
            )
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
        display = value if value else _DEFAULT_ROLE_TEXT
        model.setData(index, value, Qt.ItemDataRole.UserRole)
        model.setData(index, display, Qt.ItemDataRole.DisplayRole)


def _effective_layout_style(style: Style, line: TimingLine) -> Style:
    """行引用的布局套用到 style 上（与渲染端 ``_layout_style_for_line`` 同语义）。"""
    index = int(getattr(line, "layout_index", 0) or 0)
    if index <= 0 or index > len(style.layouts):
        return style
    layout = style.layouts[index - 1]
    return _dataclass_replace(
        style, **{name: getattr(layout, name) for name in LYRICS_LAYOUT_FIELDS}
    )


class LyricsPanel(DropPanel):
    """左侧歌词面板（含空态拖拽 + 已加载表格两态）。"""

    roleChanged = Signal(int, str)
    rowClicked = Signal(int)  # 用户点击歌词行时发出行号
    layoutChangeRequested = Signal(list, int)
    """右键菜单选择布局：(选中的 track.lines 行号列表, 布局 index)。宿主按页联动应用。"""

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

        self._style: Style = Style()
        self._track: Optional[TimingTrack] = None
        # 每行元数据：(是否空行, 可渲染序号)；空行序号取 -1。
        # lane / 组号由序号 + 当前 Style 的行数动态推出（行数可随布局变化）。
        self._row_meta: list[tuple[bool, int]] = []
        # 段落最后一行标记（与渲染端 NKM3 式段落划分一致），随 style 阈值重算
        self._paragraph_last: list[bool] = []
        # 每个可渲染行的 lane / 页序号缓存（页首行布局定行数，与渲染端一致）
        self._render_lanes: list[int] = []
        self._render_groups: list[int] = []

        # ---- qfluentwidgets TableWidget ----
        self._table = FluentTableWidget(self)
        self._table.setObjectName("LyricsTable")
        self._table.setColumnCount(3)
        self._table.setHorizontalHeaderLabels(_COLUMN_HEADERS)

        self._table.setFrameShape(FluentTableWidget.Shape.NoFrame)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._show_context_menu)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked
                                    | QAbstractItemView.EditTrigger.EditKeyPressed)
        self._table.setShowGrid(False)
        self._table.setWordWrap(False)
        self._table.setIconSize(QSize(14, 14))

        self._table.verticalHeader().setVisible(False)
        self._table.verticalHeader().setDefaultSectionSize(_ROW_HEIGHT)

        # 列宽：轨 / 角色 固定，内容撑满。
        # 注意 qfluentwidgets 的表格 QSS 给 item 加了左右各 16px padding，
        # 列宽必须把这 32px 算进去，否则文本会被省略号吃掉。
        hh = self._table.horizontalHeader()
        hh.setSectionResizeMode(COL_LANE, QHeaderView.ResizeMode.Fixed)
        hh.setSectionResizeMode(COL_ROLE, QHeaderView.ResizeMode.Fixed)
        hh.setSectionResizeMode(COL_CONTENT, QHeaderView.ResizeMode.Stretch)
        self._table.setColumnWidth(COL_LANE, 64)
        self._table.setColumnWidth(COL_ROLE, 148)

        # 角色列 → FluentComboBox 委托；轨 / 内容列 → 只读委托
        self._role_delegate = _RoleComboDelegate(self)
        self._readonly_delegate = _ReadOnlyDelegate(self)
        for delegate in (self._role_delegate, self._readonly_delegate):
            delegate.set_group_bg_provider(self._group_bg_for_row)
        self._table.setItemDelegateForColumn(COL_LANE, self._readonly_delegate)
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

    def set_style(self, style: Style) -> None:
        """样式（布局 / 配色方案）变化时刷新色点、对齐与双行分组。"""
        self._style = style
        self._role_delegate.set_style(style)
        if self._populated:
            self._refresh_presentation()

    def set_track(self, track: Optional[TimingTrack]) -> None:
        """加载 / 清空字幕。``None`` / 无行时回到空态。"""
        self._track = track
        self._table.blockSignals(True)
        try:
            self._table.clearSpans()
            self._table.setRowCount(0)
            self._row_meta = []
            if track is None or not track.lines:
                self.set_populated(False)
                return

            num_rows = len(track.lines)
            self._table.setRowCount(num_rows)
            render_index = 0
            for row, line in enumerate(track.lines):
                blank = bool(line.is_blank or not line.chars)
                if blank:
                    self._row_meta.append((True, -1))
                else:
                    self._row_meta.append((False, render_index))
                    render_index += 1

                lane_item = QTableWidgetItem("")
                lane_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                lane_item.setFlags(
                    Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
                    if not blank
                    else Qt.ItemFlag.NoItemFlags
                )
                self._table.setItem(row, COL_LANE, lane_item)

                role = _dominant_role(line)
                role_item = QTableWidgetItem(role if role else _DEFAULT_ROLE_TEXT)
                role_item.setData(Qt.ItemDataRole.UserRole, role)
                if blank:
                    role_item.setText("")
                    role_item.setFlags(Qt.ItemFlag.NoItemFlags)
                self._table.setItem(row, COL_ROLE, role_item)

                content_item = QTableWidgetItem(
                    "".join(c.text for c in line.chars) if not blank else ""
                )
                content_item.setFlags(
                    content_item.flags() & ~Qt.ItemFlag.ItemIsEditable
                )
                if blank:
                    content_item.setFlags(Qt.ItemFlag.NoItemFlags)
                self._table.setItem(row, COL_CONTENT, content_item)

                if blank:
                    # 间奏 / 段落分隔：矮行 + 跨三列的居中提示
                    self._table.setSpan(row, 0, 1, 3)
                    self._table.setRowHeight(row, _BLANK_ROW_HEIGHT)
                    lane_item.setText("♪")
                else:
                    self._table.setRowHeight(row, _ROW_HEIGHT)
        finally:
            self._table.blockSignals(False)

        self.set_populated(True)
        self._refresh_presentation()

    def refresh_row_role(self, row: int) -> None:
        """宿主改完 role_label 后刷新该行的色点（角色文本已由委托更新）。"""
        self._refresh_presentation(rows=[row])

    @property
    def list_widget(self):
        """向后兼容。"""
        return self._table

    @property
    def table_widget(self) -> FluentTableWidget:
        return self._table

    # ------------------------------------------------------------------ private

    def _lane_count(self) -> int:
        """多行显示的默认行数（与渲染端 lane 分配一致）。"""
        if not self._style.dual_line_layout:
            return 1
        return max(len(self._style.line_alignments), 1)

    def _recompute_render_lanes(self) -> None:
        """按当前布局分配（页首行布局定行数）算出每个可渲染行的 lane / 页序号。"""
        self._render_lanes = []
        self._render_groups = []
        if self._track is None:
            return
        render_lines = [
            line for line in self._track.lines if not line.is_blank and line.chars
        ]
        row_count_of = None
        if self._style.dual_line_layout and self._style.layouts:
            style = self._style
            row_count_of = lambda line: max(  # noqa: E731
                len(_effective_layout_style(style, line).line_alignments), 1
            )
        lanes, page_starts, _page_rows = assign_lanes(
            render_lines, self._lane_count(), row_count_of
        )
        self._render_lanes = lanes
        ordinal = -1
        seen_start: Optional[int] = None
        for start in page_starts:
            if start != seen_start:
                ordinal += 1
                seen_start = start
            self._render_groups.append(ordinal)

    def _group_bg_for_row(self, row: int) -> Optional[QColor]:
        """行组斑马纹：奇数页一层主题色薄底，同页行"贴在一起"。"""
        if not self._style.dual_line_layout:
            return None
        if row < 0 or row >= len(self._row_meta):
            return None
        blank, render_index = self._row_meta[row]
        group = (
            self._render_groups[render_index]
            if 0 <= render_index < len(self._render_groups)
            else -1
        )
        if blank or group % 2 != 1:
            return None
        color = QColor(palette().accent_primary)
        color.setAlpha(38 if getattr(palette(), "is_dark", False) else 24)
        return color

    def _refresh_presentation(self, rows: Optional[list[int]] = None) -> None:
        """按当前 Style 刷新：轨标 / 内容对齐 / 角色色点 / 段落末行居中。"""
        style = self._style
        dual = bool(style.dual_line_layout)
        lane_color = QColor(palette().text_hint)

        if self._track is not None:
            threshold = (
                max(style.line_lead_in_ms, 0)
                + max(style.line_tail_ms, 0)
                + max(style.line_lane_gap_ms, 0)
            )
            self._paragraph_last = paragraph_last_line_flags(
                self._track, threshold_ms=threshold
            )
        else:
            self._paragraph_last = []

        self._table.setColumnHidden(COL_LANE, not dual)
        self._recompute_render_lanes()

        self._table.blockSignals(True)
        try:
            target_rows = rows if rows is not None else range(self._table.rowCount())
            for row in target_rows:
                if row < 0 or row >= len(self._row_meta):
                    continue
                blank, render_index = self._row_meta[row]
                lane = (
                    self._render_lanes[render_index]
                    if 0 <= render_index < len(self._render_lanes)
                    else 0
                )
                lane_item = self._table.item(row, COL_LANE)
                role_item = self._table.item(row, COL_ROLE)
                content_item = self._table.item(row, COL_CONTENT)
                if lane_item is None or role_item is None or content_item is None:
                    continue
                if blank:
                    continue

                line = (
                    self._track.lines[row]
                    if self._track is not None and row < len(self._track.lines)
                    else None
                )
                line_style = (
                    _effective_layout_style(style, line) if line is not None else style
                )
                lane_item.setText(f"T{lane + 1}" if dual else "")
                layout_ref = int(getattr(line, "layout_index", 0) or 0) if line else 0
                if 1 <= layout_ref <= len(style.layouts):
                    lane_item.setToolTip(f"布局：{style.layouts[layout_ref - 1].name}")
                else:
                    lane_item.setToolTip("布局：默认布局")
                lane_item.setForeground(QBrush(lane_color))
                lane_font = lane_item.font()
                lane_font.setPointSizeF(8.0)
                lane_item.setFont(lane_font)
                paragraph_last = (
                    row < len(self._paragraph_last) and self._paragraph_last[row]
                )
                content_item.setTextAlignment(
                    self._content_alignment(line_style, lane, dual, paragraph_last)
                )

                role = str(role_item.data(Qt.ItemDataRole.UserRole) or "")
                role_item.setIcon(_swatch_icon(_scheme_swatch_color(style, role)))
        finally:
            self._table.blockSignals(False)
        # 组底色由委托绘制，Style 变化后要触发一次重绘
        self._table.viewport().update()

    @staticmethod
    def _content_alignment(
        style: Style, lane: int, dual: bool, paragraph_last: bool = False
    ) -> Qt.AlignmentFlag:
        vertical = Qt.AlignmentFlag.AlignVCenter
        if not dual:
            return Qt.AlignmentFlag.AlignLeft | vertical
        layout = style.line_horizontal_layout
        if layout == "center":
            return Qt.AlignmentFlag.AlignHCenter | vertical
        if layout == "per_row":
            align = style.row1_align if lane == 0 else style.row2_align
            mapping = {
                "left": Qt.AlignmentFlag.AlignLeft,
                "center": Qt.AlignmentFlag.AlignHCenter,
                "right": Qt.AlignmentFlag.AlignRight,
            }
            return mapping.get(align, Qt.AlignmentFlag.AlignLeft) | vertical
        # asymmetric（默认）：按每行对齐列表；段落最后一行居中（与渲染端一致，
        # 智能水平 = 不调整 时同步关闭）
        if paragraph_last and style.smart_horizontal != "none":
            return Qt.AlignmentFlag.AlignHCenter | vertical
        alignments = style.line_alignments or ["left"]
        align = alignments[min(max(lane, 0), len(alignments) - 1)]
        mapping = {
            "left": Qt.AlignmentFlag.AlignLeft,
            "center": Qt.AlignmentFlag.AlignHCenter,
            "right": Qt.AlignmentFlag.AlignRight,
        }
        return mapping.get(align, Qt.AlignmentFlag.AlignLeft) | vertical

    def _show_context_menu(self, pos: QPoint) -> None:
        """右键菜单：把布局应用到选中行（宿主按页联动扩散到同页行）。"""
        if self._track is None:
            return
        rows = sorted({item.row() for item in self._table.selectedItems()})
        clicked = self._table.rowAt(pos.y())
        if clicked >= 0 and clicked not in rows:
            rows = [clicked]
        rows = [
            row
            for row in rows
            if 0 <= row < len(self._row_meta) and not self._row_meta[row][0]
        ]
        if not rows:
            return
        menu = QMenu(self._table)
        layout_menu = menu.addMenu("应用布局")
        current_indices = {
            int(getattr(self._track.lines[row], "layout_index", 0) or 0)
            for row in rows
            if row < len(self._track.lines)
        }
        names = ["默认布局"] + [layout.name for layout in self._style.layouts]
        for index, name in enumerate(names):
            action = layout_menu.addAction(name)
            action.setCheckable(True)
            action.setChecked(current_indices == {index})
            action.triggered.connect(
                lambda _checked=False, idx=index, rs=list(rows): (
                    self.layoutChangeRequested.emit(rs, idx)
                )
            )
        menu.exec(self._table.viewport().mapToGlobal(pos))

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if item.column() != COL_ROLE:
            return
        role_name = str(item.data(Qt.ItemDataRole.UserRole) or "")
        self.roleChanged.emit(item.row(), role_name)

    def _panel_style_spec(self) -> tuple[str, int, str, str, int]:
        # 载入后表格铺满面板：去边框、去圆角；空态 / 拖拽态沿用基类
        if self._populated and self._drag_state == "idle":
            return "transparent", 0, "solid", palette().card_bg, 0
        return super()._panel_style_spec()


def _dominant_role(line) -> str:
    """返回本行出现次数最多的角色标签；无角色时返回空字符串。"""
    roles = [ch.role_label for ch in line.chars if ch.role_label]
    if not roles:
        return ""
    return Counter(roles).most_common(1)[0][0]
