"""布局预设库管理对话框（列表风格，与「样式预设库」一致）。

与样式预设库完全同一种画法：**列表 + ListItemDelegate 子类**绘制裸行
（不用行控件）。因此行高、垂直居中、灰色 hover 底色、左侧勾选框的样式
与位置都由同一套 Fluent 绘制逻辑决定，不会出现「行高与控件高度不一致
导致的错位」。

列表上方是列标题行：

    [选中]     名称 · 布局参数（行数 / 行间距 / 位置 / 余白 / 对齐 …）    [默认加载]

- **左侧勾选 = 本次选中**（批量「载入选中项」，不落盘；沿用 Fluent 的
  条目 CheckState，外观与样式预设库一致）；
- **右侧勾选 = 默认加载**（持久化：新建工程是否带入该布局），表头标注；
- 单击行内任意空白/文字区域切换「本次选中」；点右侧勾选只改默认加载；
- 单行参数省略显示，完整参数在每行悬浮提示里逐条列出。
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from typing import Optional

from PyQt6.QtCore import QEvent, QRect, QRectF, QSize, Qt, pyqtSignal as Signal
from PyQt6.QtGui import QColor, QFontMetrics, QIcon, QMouseEvent, QPainter, QPixmap
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    Action,
    BodyLabel,
    CaptionLabel,
    ComboBox as FluentComboBox,
    LineEdit as FluentLineEdit,
    ListWidget as FluentListWidget,
    PrimaryPushButton as FluentPrimaryPushButton,
    PushButton as FluentPushButton,
    RoundMenu,
    SubtitleLabel,
)
from qfluentwidgets.common.color import autoFallbackThemeColor
from qfluentwidgets.components.widgets.check_box import CheckBoxIcon
from qfluentwidgets.common.style_sheet import isDarkTheme
from qfluentwidgets.components.widgets.list_view import ListItemDelegate

from krok_helper.qfluent_compat import ModelessDialog
from krok_helper.subtitle_render.domain.models import LyricsLayout
from krok_helper.subtitle_render.frontend.dialogs.fluent_dialogs import (
    dont_ask_active,
    fluent_confirm_dont_ask,
    fluent_get_text,
    fluent_info,
    fluent_question,
)
from krok_helper.subtitle_render.frontend.widgets.theme import palette, themed
from krok_helper.subtitle_render.settings.layout_presets import (
    LEGACY_PRESET_REFERENCE_HEIGHT,
    LayoutPreset,
    factory_layout_definitions,
    make_layout_id,
    rescale_preset_layout,
)

_PRESET_ID_ROLE = Qt.ItemDataRole.UserRole
_DEFAULT_LOAD_ROLE = Qt.ItemDataRole.UserRole + 1

#: 行高与样式预设库对齐（其 delegate 行高约 39；取 40 留一点余量）。
_ROW_HEIGHT = 40
#: Fluent delegate 的勾选框：x = rect.x()+15，19×19（见 ListItemDelegate）。
_CHECK_LEFT = 15
_CHECK_SIZE = 19
_ICON_WIDTH = 34
_ICON_HEIGHT = 20
_PAD_RIGHT = 15

_POSITION_LABELS = {"top": "顶部", "center": "居中", "bottom": "底部"}
_SMART_LABELS = {
    "equal_margins": "左右余白对齐",
    "center_position": "中心位置对齐",
    "none": "不调整",
}
_ALIGN_LABELS = {"left": "左", "center": "中", "right": "右"}
_RUBY_ALIGN_LABELS = {"auto": "自动", "center": "居中", "equal_space": "等分"}


def _rows_of(preset: LayoutPreset) -> int:
    return max(1, min(len(preset.layout.line_alignments), 8))


def _normalized_presets(presets: list[LayoutPreset]) -> list[LayoutPreset]:
    """保证每条都有合法且唯一的 ``layout_id``（列表按 id 反查条目）。"""

    used: set[str] = set()
    normalized: list[LayoutPreset] = []
    for preset in presets:
        current_id = str(preset.layout.layout_id or "").strip()
        preset_id = make_layout_id(current_id, used)
        used.add(preset_id)
        if preset_id != current_id:
            layout = deepcopy(preset.layout)
            layout.layout_id = preset_id
            preset = replace(preset, layout=layout)
        normalized.append(preset)
    return normalized


def _optional_text(value: Optional[int], suffix: str = "") -> str:
    return "跟随全局" if value is None else f"{int(value)}{suffix}"


def _detail_parts(preset: LayoutPreset) -> list[str]:
    """按「行数、行间距…」顺序平铺该布局的全部排版参数。"""

    layout = preset.layout
    alignments = "/".join(
        _ALIGN_LABELS.get(alignment, alignment)
        for alignment in (layout.line_alignments or ["left"])
    )
    parts = [
        f"{_rows_of(preset)} 行",
        f"行间距 {int(layout.line_gap_px)}",
        f"位置 {_POSITION_LABELS.get(layout.line_y_position, layout.line_y_position)}",
        (
            f"上余白 {int(layout.line_y_margin_px)}"
            if layout.line_y_position == "top"
            else f"下余白 {int(layout.line_y_margin_px)}"
            if layout.line_y_position == "bottom"
            else f"余白 {int(layout.line_y_margin_px)}"
        ),
        f"左右余白 {int(layout.horizontal_margin_px)}",
        _SMART_LABELS.get(layout.smart_horizontal, layout.smart_horizontal),
        f"对齐 {alignments}",
    ]
    if layout.line_offsets_x:
        parts.append(
            "X 偏移 " + "/".join(str(int(value)) for value in layout.line_offsets_x)
        )
    parts.extend(
        [
            f"字间距 {_optional_text(layout.letter_spacing_px)}",
            f"空格宽 {_optional_text(layout.space_width_percent, '%')}",
            (
                "咬合 跟随全局"
                if layout.allow_biting is None
                else f"咬合 {'开' if layout.allow_biting else '关'}"
            ),
            f"注音间距 {_optional_text(layout.ruby_interval_px)}",
            f"注音排布 {_RUBY_ALIGN_LABELS.get(layout.ruby_alignment or 'auto', '自动')}",
            f"注音与正文 {_optional_text(layout.ruby_gap_px)}",
        ]
    )
    if layout.force_top_bottom_n3 is not None:
        parts.append(
            f"强制顶底 {'开' if layout.force_top_bottom_n3 else '关'}"
        )
    return parts


def _usage_text(usage: Optional[tuple[int, int]]) -> str:
    if not usage:
        return ""
    pages, lines = usage
    parts = []
    if pages:
        parts.append(f"{pages} 个页面")
    if lines:
        parts.append(f"{lines} 行歌词")
    return "、".join(parts)


def _preset_icon(preset: LayoutPreset) -> QIcon:
    """对齐示意图标：每行一根横条，按对齐方式落位。"""

    pixmap = QPixmap(_ICON_WIDTH, _ICON_HEIGHT)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    try:
        alignments = (list(preset.layout.line_alignments) or ["left"])[:5]
        bar_width, bar_height, gap = 18, 4, 1
        total = len(alignments) * bar_height + (len(alignments) - 1) * gap
        position = preset.layout.line_y_position
        top = (
            2
            if position == "top"
            else _ICON_HEIGHT - total - 2
            if position == "bottom"
            else (_ICON_HEIGHT - total) // 2
        )
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(140, 145, 152))
        for offset, alignment in enumerate(alignments):
            y = top + offset * (bar_height + gap)
            if alignment == "center":
                x = (_ICON_WIDTH - bar_width) // 2
            elif alignment == "right":
                x = _ICON_WIDTH - bar_width - 2
            else:
                x = 2
            painter.drawRoundedRect(x, y, bar_width, bar_height, 1, 1)
    finally:
        painter.end()
    return QIcon(pixmap)


class _LayoutRowDelegate(ListItemDelegate):
    """在 Fluent 原生行绘制之上补：示意图标、名称、参数、右侧默认加载勾选。

    左侧「选中」直接用条目的 ``CheckStateRole``，由 Fluent delegate 原样
    绘制并处理点击——外观、位置（x+15、19×19）与样式预设库完全一致。
    """

    defaultLoadToggled = Signal(str, bool)

    def __init__(self, view) -> None:
        super().__init__(view)
        self._icons: dict[str, QIcon] = {}
        self._texts: dict[str, tuple[str, str]] = {}

    def set_row_content(
        self, preset_id: str, icon: QIcon, name: str, details: str
    ) -> None:
        self._icons[preset_id] = icon
        self._texts[preset_id] = (name, details)

    def sizeHint(self, option, index) -> QSize:  # noqa: N802 - Qt 命名
        size = super().sizeHint(option, index)
        return QSize(size.width(), _ROW_HEIGHT)

    # ------------------------------------------------------------------ 绘制

    @staticmethod
    def _default_rect(rect: QRect) -> QRect:
        return QRect(
            rect.right() - _PAD_RIGHT - _CHECK_SIZE,
            rect.center().y() - _CHECK_SIZE // 2,
            _CHECK_SIZE,
            _CHECK_SIZE,
        )

    @staticmethod
    def _select_rect(rect: QRect) -> QRect:
        return QRect(
            rect.x() + _CHECK_LEFT,
            rect.center().y() - _CHECK_SIZE // 2,
            _CHECK_SIZE,
            _CHECK_SIZE,
        )

    def paint(self, painter, option, index) -> None:  # noqa: N802 - Qt 命名
        # 基类：灰色 hover 圆角底、按下态、左侧选中勾选（CheckStateRole）。
        super().paint(painter, option, index)

        preset_id = str(index.data(_PRESET_ID_ROLE) or "")
        icon = self._icons.get(preset_id)
        name, details = self._texts.get(preset_id, ("", ""))
        rect = option.rect
        painter.save()
        try:
            # 示意图标（垂直居中）
            icon_x = rect.x() + _CHECK_LEFT + _CHECK_SIZE + 12
            if icon is not None:
                painter.drawPixmap(
                    QRect(
                        icon_x,
                        rect.center().y() - _ICON_HEIGHT // 2,
                        _ICON_WIDTH,
                        _ICON_HEIGHT,
                    ),
                    icon.pixmap(_ICON_WIDTH, _ICON_HEIGHT),
                )
            text_x = icon_x + _ICON_WIDTH + 8
            default_rect = self._default_rect(rect)
            metrics = QFontMetrics(option.font)

            painter.setPen(option.palette.color(option.palette.ColorRole.Text))
            name_text = metrics.elidedText(
                name, Qt.TextElideMode.ElideRight, max(0, default_rect.left() - text_x)
            )
            name_rect = QRectF(
                text_x, rect.y(), max(0, metrics.horizontalAdvance(name_text)), rect.height()
            )
            painter.drawText(
                name_rect,
                int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                name_text,
            )

            details_x = text_x + metrics.horizontalAdvance(name_text) + 10
            details_width = default_rect.left() - 10 - details_x
            if details and details_width > 20:
                painter.setPen(QColor(palette().text_secondary))
                details_text = metrics.elidedText(
                    details, Qt.TextElideMode.ElideRight, details_width
                )
                painter.drawText(
                    QRectF(details_x, rect.y(), details_width, rect.height()),
                    int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                    details_text,
                )

            # 右侧「默认加载」勾选（与左侧同尺寸、垂直居中）。
            self._draw_checkbox(
                painter,
                default_rect,
                bool(index.data(_DEFAULT_LOAD_ROLE)),
                option,
            )
        finally:
            painter.restore()

    def _draw_checkbox(
        self, painter: QPainter, rect: QRect, checked: bool, option
    ) -> None:
        """与 Fluent ``ListItemDelegate._drawCheckBox`` 完全同款（圆角/配色/对勾）。"""

        painter.save()
        try:
            is_dark = isDarkTheme()
            radius = 4.5
            box = QRectF(rect)
            if not checked:
                painter.setBrush(
                    QColor(0, 0, 0, 26) if is_dark else QColor(0, 0, 0, 6)
                )
                painter.setPen(
                    QColor(255, 255, 255, 142)
                    if is_dark
                    else QColor(0, 0, 0, 122)
                )
                painter.drawRoundedRect(box, radius, radius)
            else:
                color = autoFallbackThemeColor(
                    self.lightCheckedColor, self.darkCheckedColor
                )
                painter.setPen(color)
                painter.setBrush(color)
                painter.drawRoundedRect(box, radius, radius)
                CheckBoxIcon.ACCEPT.render(painter, box)
        finally:
            painter.restore()

    # ------------------------------------------------------------------ 交互

    def editorEvent(self, event, model, option, index):  # noqa: N802 - Qt 命名
        # 与样式预设库同口径：不可勾选的条目不吃行点击（避免写出「全部取消」
        # 清不掉的勾选）。
        if not (index.flags() & Qt.ItemFlag.ItemIsUserCheckable):
            return super().editorEvent(event, model, option, index)
        if (
            event.type() == QEvent.Type.MouseButtonRelease
            and isinstance(event, QMouseEvent)
            and event.button() == Qt.MouseButton.LeftButton
        ):
            position = event.position().toPoint()
            preset_id = str(index.data(_PRESET_ID_ROLE) or "")
            if self._default_rect(option.rect).contains(position):
                self.defaultLoadToggled.emit(
                    preset_id, not bool(index.data(_DEFAULT_LOAD_ROLE))
                )
                return True
            # 左勾选交给 Fluent/Qt 原生处理。
            if self._select_rect(option.rect).contains(position):
                return super().editorEvent(event, model, option, index)
            # 行内空白/文字区域：切换本次选中。
            model.setData(
                index,
                (
                    Qt.CheckState.Unchecked
                    if index.data(Qt.ItemDataRole.CheckStateRole)
                    == Qt.CheckState.Checked
                    else Qt.CheckState.Checked
                ),
                Qt.ItemDataRole.CheckStateRole,
            )
            return True
        return super().editorEvent(event, model, option, index)


class LayoutLibraryDialog(ModelessDialog):
    """布局预设库：左勾选=本次选中，右勾选=默认加载（带表头）。"""

    layoutPresetsChanged = Signal(list)
    #: 载入当前工程：参数是选中的预设列表（可批量）。
    layoutImportRequested = Signal(list)

    def __init__(
        self,
        presets: list[LayoutPreset],
        project_layout_names: Optional[set[str]] = None,
        library_reference_height: int = LEGACY_PRESET_REFERENCE_HEIGHT,
        project_usage: Optional[dict[str, tuple[int, int]]] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("布局预设库")
        self.resize(780, 540)
        self._presets = _normalized_presets(deepcopy(list(presets)))
        self._project_names = set(project_layout_names or set())
        self._library_height = max(int(library_reference_height), 1)
        self._usage = dict(project_usage or {})
        #: 本次「选中」（只影响批量载入，不落盘）。
        self._selected_ids: set[str] = set()
        self._items: dict[str, QListWidgetItem] = {}
        self._syncing = False

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 18, 18, 18)
        root.setSpacing(10)

        title = SubtitleLabel("布局预设库", self)
        themed(
            title,
            lambda: (
                f"color: {palette().title_text};"
                "font-size: 15pt;"
                "font-weight: 600;"
            ),
        )
        root.addWidget(title)
        root.addWidget(
            BodyLabel(
                "「选中」只用于本次批量载入；「默认加载」决定该布局是否带入"
                "以后新建的工程（已保存工程与当前工程不受影响）。",
                self,
            )
        )

        filter_row = QHBoxLayout()
        filter_row.setContentsMargins(0, 0, 0, 0)
        filter_row.setSpacing(8)
        filter_row.addWidget(BodyLabel("过滤:", self))
        self._filter_edit = FluentLineEdit(self)
        self._filter_edit.setPlaceholderText("输入名称搜索...")
        self._filter_edit.textChanged.connect(self._apply_filter)
        filter_row.addWidget(self._filter_edit, 1)
        filter_row.addWidget(BodyLabel("行数:", self))
        self._rows_filter = FluentComboBox(self)
        self._rows_filter.setMinimumWidth(96)
        # Fluent ComboBox 的第二位置参数是 icon：数据必须走 userData。
        self._rows_filter.addItem("全部", userData=None)
        for rows in range(1, 9):
            self._rows_filter.addItem(f"{rows} 行", userData=rows)
        self._rows_filter.currentIndexChanged.connect(self._apply_filter)
        filter_row.addWidget(self._rows_filter)
        root.addLayout(filter_row)

        self._header = self._make_header()
        root.addWidget(self._header)

        self._list = FluentListWidget(self)
        self._list.setSelectionMode(
            QAbstractItemView.SelectionMode.NoSelection
        )
        self._list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._list.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._list.setMouseTracking(True)
        self._delegate = _LayoutRowDelegate(self._list)
        self._delegate.defaultLoadToggled.connect(self._set_default_load)
        self._list.setItemDelegate(self._delegate)
        self._list.itemChanged.connect(self._on_item_changed)
        root.addWidget(self._list, 1)

        self._empty_label = BodyLabel(
            "布局库为空。可在布局页点「保存到布局库」，或恢复出厂预设。",
            self,
        )
        root.addWidget(self._empty_label)

        selection_row = QHBoxLayout()
        selection_row.setContentsMargins(0, 0, 0, 0)
        self._select_all_btn = FluentPushButton("全选当前结果", self)
        self._select_all_btn.clicked.connect(
            lambda _checked=False: self._set_all_selected(True)
        )
        self._clear_checks_btn = FluentPushButton("全部取消", self)
        self._clear_checks_btn.clicked.connect(
            lambda _checked=False: self._set_all_selected(False)
        )
        self._selection_stats = CaptionLabel("", self)
        self._default_stats = CaptionLabel("", self)
        selection_row.addWidget(self._select_all_btn)
        selection_row.addWidget(self._clear_checks_btn)
        selection_row.addStretch(1)
        selection_row.addWidget(self._default_stats)
        selection_row.addWidget(self._selection_stats)
        root.addLayout(selection_row)

        edit_row = QHBoxLayout()
        edit_row.setContentsMargins(0, 0, 0, 0)
        edit_row.setSpacing(8)
        self._rename_btn = FluentPushButton("重命名", self)
        self._rename_btn.setToolTip("勾选单个自定义布局后可重命名")
        self._rename_btn.clicked.connect(self._on_rename)
        self._delete_btn = FluentPushButton("删除", self)
        self._delete_btn.setToolTip("勾选单个布局后可删除")
        self._delete_btn.clicked.connect(self._on_delete)
        self._restore_btn = FluentPushButton("恢复出厂预设", self)
        self._restore_btn.clicked.connect(self._on_restore_factory)
        for button in (self._rename_btn, self._delete_btn, self._restore_btn):
            edit_row.addWidget(button)
        root.addLayout(edit_row)

        button_row = QHBoxLayout()
        button_row.setContentsMargins(0, 4, 0, 0)
        button_row.addStretch(1)
        self._import_btn = FluentPrimaryPushButton(
            "载入选中项到当前工程", self
        )
        self._import_btn.clicked.connect(self._on_import)
        close_btn = FluentPushButton("关闭", self)
        close_btn.clicked.connect(self.accept)
        button_row.addWidget(self._import_btn)
        button_row.addWidget(close_btn)
        root.addLayout(button_row)

        self._rebuild()

    def _make_header(self) -> QWidget:
        """列标题：与行内元素同一组偏移量，标注两个勾选列。"""

        header = QWidget(self)
        column_row = QHBoxLayout(header)
        column_row.setContentsMargins(0, 2, 0, 2)
        column_row.setSpacing(0)

        select_label = CaptionLabel("选中", header)
        select_label.setFixedWidth(_CHECK_LEFT + _CHECK_SIZE + 12)
        column_row.addWidget(select_label)
        column_row.addSpacing(_ICON_WIDTH + 8)

        name_label = CaptionLabel(
            "名称 · 布局参数（行数 / 行间距 / 位置 / 余白 / 对齐 …）", header
        )
        column_row.addWidget(name_label, 1)

        default_label = CaptionLabel("默认加载", header)
        default_label.setFixedWidth(_CHECK_SIZE + 8)
        default_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        default_label.setToolTip(
            "勾选后新建工程自动带入该布局；不改变已保存工程。"
        )
        column_row.addWidget(default_label)
        column_row.addSpacing(_PAD_RIGHT - 6)
        return header

    # ------------------------------------------------------------------ 数据

    def presets(self) -> list[LayoutPreset]:
        return deepcopy(self._presets)

    def _find(self, layout_id: str) -> Optional[LayoutPreset]:
        return next(
            (
                preset
                for preset in self._presets
                if str(preset.layout.layout_id or "") == layout_id
            ),
            None,
        )

    def selected_presets(self) -> list[LayoutPreset]:
        return [
            preset
            for preset in self._presets
            if str(preset.layout.layout_id or "") in self._selected_ids
        ]

    # ------------------------------------------------------------------ 列表

    def _rebuild(self) -> None:
        self._syncing = True
        try:
            self._list.clear()
            self._items = {}
            for preset in self._presets:
                preset_id = str(preset.layout.layout_id or "")
                item = QListWidgetItem()
                item.setData(_PRESET_ID_ROLE, preset_id)
                item.setData(_DEFAULT_LOAD_ROLE, bool(preset.default_load))
                item.setFlags(
                    Qt.ItemFlag.ItemIsEnabled
                    | Qt.ItemFlag.ItemIsSelectable
                    | Qt.ItemFlag.ItemIsUserCheckable
                )
                item.setCheckState(
                    Qt.CheckState.Checked
                    if preset_id in self._selected_ids
                    else Qt.CheckState.Unchecked
                )
                in_project = preset.layout.name in self._project_names
                name_text = preset.layout.name
                if preset.is_builtin:
                    name_text += " [出厂]"
                if in_project:
                    name_text += "（工程内已有同名）"
                details = " · ".join(_detail_parts(preset))
                item.setToolTip(
                    "\n".join(
                        [
                            preset.layout.name,
                            *_detail_parts(preset),
                            "",
                            "默认加载："
                            + (
                                "开（新建工程会带入）"
                                if preset.default_load
                                else "关"
                            ),
                            (
                                "工程内已有同名布局：载入会按名覆盖工程内条目；"
                                "删除只移除布局库条目，工程内保留。"
                                if in_project
                                else ""
                            ),
                        ]
                    ).strip()
                )
                item.setSizeHint(QSize(0, _ROW_HEIGHT))
                self._delegate.set_row_content(
                    preset_id, _preset_icon(preset), name_text, details
                )
                self._list.addItem(item)
                self._items[preset_id] = item
        finally:
            self._syncing = False
        has_items = bool(self._presets)
        self._list.setVisible(has_items)
        self._empty_label.setVisible(not has_items)
        self._apply_filter()

    def _apply_filter(self) -> None:
        keyword = self._filter_edit.text().strip().lower()
        rows_filter = self._rows_filter.currentData()
        for preset_id, item in self._items.items():
            preset = self._find(preset_id)
            if preset is None:
                item.setHidden(True)
                continue
            haystack = " ".join(
                [preset.layout.name, *_detail_parts(preset)]
            ).lower()
            matches_text = not keyword or keyword in haystack
            matches_rows = (
                rows_filter is None or _rows_of(preset) == rows_filter
            )
            item.setHidden(not (matches_text and matches_rows))
        self._sync_buttons()

    def _on_item_changed(self, item: QListWidgetItem) -> None:
        if self._syncing:
            return
        preset_id = str(item.data(_PRESET_ID_ROLE) or "")
        if not preset_id:
            return
        if item.checkState() == Qt.CheckState.Checked:
            self._selected_ids.add(preset_id)
        else:
            self._selected_ids.discard(preset_id)
        self._sync_buttons()

    # 供 delegate / 测试使用的状态入口。
    def _set_selected(self, preset_id: str, checked: bool) -> None:
        item = self._items.get(str(preset_id))
        if item is None:
            return
        item.setCheckState(
            Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        )

    def _set_default_load(self, preset_id: str, checked: bool) -> None:
        preset = self._find(str(preset_id))
        if preset is None or checked == preset.default_load:
            return
        self._commit(
            replace(
                preset,
                default_load=checked,
                # 取消默认加载同时放弃行数接线（重新勾选不自动恢复）。
                default_for_rows=preset.default_for_rows if checked else None,
            )
        )

    def _set_all_selected(self, checked: bool) -> None:
        visible = [
            preset
            for preset in self._presets
            if not self._items[
                str(preset.layout.layout_id or "")
            ].isHidden()
        ]
        if not visible:
            return
        for preset in visible:
            self._set_selected(str(preset.layout.layout_id or ""), checked)
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        selected = self.selected_presets()
        self._import_btn.setEnabled(bool(selected))
        self._import_btn.setText(
            f"载入选中项到当前工程（{len(selected)}）"
            if selected
            else "载入选中项到当前工程"
        )
        single = selected[0] if len(selected) == 1 else None
        self._rename_btn.setEnabled(
            single is not None and not single.is_builtin
        )
        self._delete_btn.setEnabled(single is not None)
        self._restore_btn.setEnabled(True)
        self._select_all_btn.setEnabled(bool(self._presets))
        self._clear_checks_btn.setEnabled(bool(self._presets))
        default_count = sum(
            1 for preset in self._presets if preset.default_load
        )
        self._default_stats.setText(
            f"默认加载 {default_count} / {len(self._presets)}"
        )
        self._selection_stats.setText(
            f"已选 {len(selected)} / {len(self._presets)}"
        )

    def _commit(self, preset: LayoutPreset) -> None:
        preset_id = str(preset.layout.layout_id or "")
        self._presets = [
            preset
            if str(item.layout.layout_id or "") == preset_id
            else item
            for item in self._presets
        ]
        self._rebuild()
        self.layoutPresetsChanged.emit(self.presets())

    # ------------------------------------------------------------------ 操作

    def _on_import(self) -> None:
        selected = self.selected_presets()
        if not selected:
            return
        self.layoutImportRequested.emit(selected)

    def _on_rename(self) -> None:
        selected = self.selected_presets()
        if len(selected) != 1:
            return
        preset = selected[0]
        if preset.is_builtin:
            return
        new_name, ok = fluent_get_text(
            self, "重命名布局预设", "布局名称", text=preset.layout.name
        )
        if not ok:
            return
        new_name = new_name.strip()
        if not new_name or new_name == preset.layout.name:
            return
        if any(item.layout.name == new_name for item in self._presets):
            fluent_info(
                self,
                "重命名布局预设",
                f"布局库中已存在同名布局“{new_name}”。",
                ok_text="知道了",
            )
            return
        layout = deepcopy(preset.layout)
        layout.name = new_name
        self._commit(replace(preset, layout=layout))

    def _on_delete(self) -> None:
        selected = self.selected_presets()
        if len(selected) != 1:
            return
        preset = selected[0]
        name = preset.layout.name
        kind = "出厂预设" if preset.is_builtin else "自定义布局"
        usage = _usage_text(self._usage.get(name))
        usage_line = (
            f"· 当前工程正在使用同名布局（{usage}；工程内是独立副本，"
            "删除库条目不受影响）；\n"
            if usage
            else "· 当前工程不受影响（工程内的同名布局作为独立副本保留）；\n"
        )
        # 第一步：列影响清单（每次都给，避免误删）。
        if not dont_ask_active("layout_library_delete") and not fluent_question(
            self,
            "删除布局预设（1/2）",
            f"确定要从布局库删除{kind}“{name}”吗？\n"
            f"{usage_line}"
            "· 之后新建工程不再带入该布局；\n"
            "· 该布局接线的「N 行页默认」会回退出厂选择。",
            yes_text="下一步",
            no_text="取消",
            default_cancel=True,
        ):
            return
        # 第二步：最终确认，勾选「本次运行不再提示」后本会话不再询问。
        if not fluent_confirm_dont_ask(
            self,
            "删除布局预设（2/2）",
            f"再次确认：从布局库删除“{name}”？\n"
            "工程内的同名布局不受影响。",
            key="layout_library_delete",
            yes_text="删除",
            no_text="取消",
        ):
            return
        preset_id = str(preset.layout.layout_id or "")
        self._presets = [
            item
            for item in self._presets
            if str(item.layout.layout_id or "") != preset_id
        ]
        self._selected_ids.discard(preset_id)
        self._rebuild()
        self.layoutPresetsChanged.emit(self.presets())

    def _on_restore_factory(self) -> None:
        known_ids = {
            str(preset.layout.layout_id or "") for preset in self._presets
        }
        menu = RoundMenu(parent=self)
        for factory in factory_layout_definitions():
            factory_id = str(factory.layout_id)
            label = (
                f"重置『{factory.name}』为出厂参数"
                if factory_id in known_ids
                else f"恢复『{factory.name}』"
            )
            menu.addAction(
                Action(
                    label,
                    triggered=(
                        lambda _checked=False, layout=factory: (
                            self._restore_factory(layout)
                        )
                    ),
                )
            )
        menu.exec(
            self._restore_btn.mapToGlobal(
                self._restore_btn.rect().bottomLeft()
            )
        )

    def _restore_factory(self, factory: LyricsLayout) -> None:
        """恢复/重置一条出厂预设；出厂条目必须持有规范名。"""

        factory_id = str(factory.layout_id)
        restored_layout = rescale_preset_layout(
            deepcopy(factory),
            LEGACY_PRESET_REFERENCE_HEIGHT,
            self._library_height,
        )
        restored_layout.layout_id = factory_id

        existing = self._find(factory_id)
        presets = list(self._presets)
        if existing is not None:
            restored = replace(
                existing,
                layout=restored_layout,
                reference_height=self._library_height,
            )
            presets = [
                restored
                if str(item.layout.layout_id or "") == factory_id
                else item
                for item in presets
            ]
        else:
            presets.append(
                LayoutPreset(
                    layout=restored_layout,
                    default_load=True,
                    reference_height=self._library_height,
                )
            )
        presets = self._release_name(
            presets, restored_layout.name, keep_id=factory_id
        )
        self._presets = presets
        self._selected_ids.add(factory_id)
        self._rebuild()
        self.layoutPresetsChanged.emit(self.presets())

    @staticmethod
    def _release_name(
        presets: list[LayoutPreset], canonical: str, *, keep_id: str
    ) -> list[LayoutPreset]:
        """把 ``canonical`` 名字让给 ``keep_id``（其他占用者加后缀）。"""

        taken = {
            item.layout.name
            for item in presets
            if item.layout.name != canonical
        }
        released: list[LayoutPreset] = []
        for item in presets:
            item_id = str(item.layout.layout_id or "")
            if item.layout.name != canonical or item_id == keep_id:
                released.append(item)
                continue
            suffix = 2
            while f"{canonical} {suffix}" in taken:
                suffix += 1
            new_name = f"{canonical} {suffix}"
            taken.add(new_name)
            layout = deepcopy(item.layout)
            layout.name = new_name
            released.append(replace(item, layout=layout))
        return released
