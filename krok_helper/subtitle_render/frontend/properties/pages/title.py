"""Title-property page: one collapsible card per title entry (multi-title).

卡片头部 = 启用开关 + 名称编辑 + 删除按钮；主体 = 标题文字 / 外观（布局与
配色方案引用）/ 显示时段（四档 + 自定义区间）。控件仍把状态变更回调交给
宿主 :class:`PropertyPanel`，本模块只负责构建与同步控件值。
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget
from qfluentwidgets import (
    FluentIcon as FIF,
    LineEdit as FluentLineEdit,
    PushButton as FluentPushButton,
    TransparentToolButton as FluentTransparentToolButton,
)

from krok_helper.subtitle_render.domain.models import (
    TITLE_SCHEME_NAME,
    TitleOverlay,
    TitleTimeWindow,
)
from krok_helper.subtitle_render.frontend.properties.controls.inputs import (
    GrowingPlainTextEdit,
    NoWheelSpinBox,
    TimecodeEdit,
    WheelFocusedComboBox,
)
from krok_helper.subtitle_render.frontend.properties.controls.layout import (
    ResponsiveFieldGrid,
    compact_property_control,
    inline_property_section,
    property_field,
    property_section,
)
from krok_helper.subtitle_render.frontend.properties.controls.widgets import (
    CollapsibleSection,
    subgroup_label,
)


TITLE_TIME_MAX_MS = 5_999_990

_TITLE_MODE_OPTIONS: tuple[tuple[str, str], ...] = (
    ("全程显示", "whole"),
    ("仅开头", "head"),
    ("仅片尾", "tail"),
    ("开始和片尾", "head_tail"),
    ("自定义", "custom"),
)

_WINDOW_FIELD_SPECS: tuple[tuple[str, str, int, str], ...] = (
    # (标签, 控件类型, 上限, 模型字段)；淡入/淡出是动画持续时长（毫秒），
    # 用纯数字输入并带 " ms" 后缀，起止时间仍是时间码。
    ("开始", "timecode", TITLE_TIME_MAX_MS, "begin_ms"),
    ("结束", "timecode", TITLE_TIME_MAX_MS, "end_ms"),
    ("淡入", "ms", 10_000, "fade_in_ms"),
    ("淡出", "ms", 10_000, "fade_out_ms"),
)


class _TitleWindowRow(QWidget):
    """自定义模式的一个时间段编辑行。"""

    def __init__(
        self,
        host: Any,
        card_index: int,
        window: TitleTimeWindow,
        *,
        timecode_factory: Callable[[int, int], TimecodeEdit],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._host = host
        self._card_index = card_index
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        grid = ResponsiveFieldGrid(self, min_column_width=130, max_columns=4)
        self.edits: dict[str, QWidget] = {}
        for label, kind, maximum, field_name in _WINDOW_FIELD_SPECS:
            if kind == "ms":
                editor: QWidget = _make_ms_spin()
                tooltip = f"{label}动画的持续时长（毫秒），不是起止时间。"
            else:
                editor = timecode_factory(0, maximum)
                tooltip = f"时间段{label}时间（分:秒.毫秒）。"
            editor.setToolTip(tooltip)
            editor.setValue(int(getattr(window, field_name)))  # type: ignore[attr-defined]
            editor.valueChanged.connect(  # type: ignore[attr-defined]
                lambda _value: self._host._on_title_card_windows_changed(self._card_index)
            )
            self.edits[field_name] = editor
            grid.add_field(label, editor)
        layout.addWidget(grid, 1)

        remove_button = FluentTransparentToolButton(FIF.CLOSE, self)
        remove_button.setToolTip("删除此时间段")
        remove_button.clicked.connect(
            lambda: self._host._on_title_card_window_removed(
                self._card_index, self
            )
        )
        layout.addWidget(remove_button, 0, Qt.AlignmentFlag.AlignTop)

    def to_window(self) -> TitleTimeWindow:
        return TitleTimeWindow(
            begin_ms=self.edits["begin_ms"].value(),  # type: ignore[attr-defined]
            end_ms=self.edits["end_ms"].value(),  # type: ignore[attr-defined]
            fade_in_ms=self.edits["fade_in_ms"].value(),  # type: ignore[attr-defined]
            fade_out_ms=self.edits["fade_out_ms"].value(),  # type: ignore[attr-defined]
        )

    def sync(self, window: TitleTimeWindow) -> None:
        for field_name, editor in self.edits.items():
            editor.blockSignals(True)  # type: ignore[attr-defined]
            try:
                editor.setValue(int(getattr(window, field_name)))  # type: ignore[attr-defined]
            finally:
                editor.blockSignals(False)  # type: ignore[attr-defined]


def _make_ms_spin() -> NoWheelSpinBox:
    """毫秒数字输入（带 `` ms`` 后缀），用于淡入/淡出持续时长。"""
    spin = NoWheelSpinBox()
    spin.setRange(0, 10_000)
    spin.setSuffix(" ms")
    spin.setValue(500)
    return spin


class TitleCard:
    """一个标题条目的卡片（构建 + 值同步；状态写回走宿主回调）。"""

    def __init__(
        self,
        host: Any,
        index: int,
        overlay: TitleOverlay,
        *,
        timecode_factory: Callable[[int, int], TimecodeEdit],
    ) -> None:
        self._host = host
        self.index = index
        name = overlay.name or f"标题 {index + 1}"
        section, content = _card_section(name)
        self.section = section

        # 头部只保留：折叠标题（动态显示条目名）+ 启用开关 + 删除按钮。
        header_row = section.header.parentWidget()
        header_layout = header_row.layout()
        self.delete_button = FluentTransparentToolButton(FIF.DELETE, header_row)
        self.delete_button.setToolTip("删除此标题条目")
        self.delete_button.clicked.connect(
            lambda: host._on_title_card_delete_requested(index)
        )
        header_layout.addWidget(self.delete_button)

        section.header_switch.toggled.connect(
            lambda checked: host._on_title_enabled_toggled(index, checked)
        )
        section.set_collapsed_summary(name)

        # ---- 标题信息（名称 + 标题文字 + 可用标签，同一区块）----
        info_section, info_layout = inline_property_section("标题信息", section)

        name_grid = ResponsiveFieldGrid(info_section, min_column_width=260, max_columns=2)
        self.name_edit = FluentLineEdit(info_section)
        compact_property_control(self.name_edit)
        self.name_edit.setText(name)
        self.name_edit.setToolTip("标题条目名称（卡片折叠标题与字幕源下拉共用）")
        self.name_edit.editingFinished.connect(
            lambda: host._on_title_card_renamed(index, self.name_edit.text())
        )
        name_grid.add_field("名称", self.name_edit)
        info_layout.addWidget(name_grid)

        text_row = QWidget(info_section)
        text_row_layout = QHBoxLayout(text_row)
        text_row_layout.setContentsMargins(0, 0, 0, 0)
        text_row_layout.setSpacing(8)
        self.text_edit = GrowingPlainTextEdit(text_row)
        self.text_edit.setPlaceholderText("{title} / {artist}")
        # 宽度策略 Ignored：可收缩到任意列宽（内容按 WidgetWidth 换行），
        # 否则长模板行的最小宽度会把同行的「可用标签」按钮挤没。
        # 注意：同行的按钮必须是 Fixed 策略，两个 Ignored 会触发 Qt
        # 盒式分配把按钮挤出父级。
        self.text_edit.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed
        )
        self.text_edit.setMinimumWidth(0)
        self.text_edit.setToolTip(
            "支持换行；{title} / {artist} 等占位符按字幕源标签替换（点「可用标签」查看全部）。"
        )
        self.text_edit.textChanged.connect(
            lambda: host._on_title_card_text_changed(index)
        )
        self.text_edit.editingFinished.connect(
            lambda: host._commit_title_text_edit()
        )
        text_row_layout.addWidget(self.text_edit, 1)

        self.tags_button = FluentPushButton("可用标签", text_row)
        compact_property_control(self.tags_button)
        self.tags_button.setMinimumWidth(84)
        # 行内不能两个控件都是 Ignored 水平策略：Qt 的盒式分配会把
        # stretch 的文字框撑满整行、把按钮挤出父级（仍占 84px 但完全
        # 不可见，父级裁剪）。按钮改为 Fixed，恒为 84px 靠右。
        self.tags_button.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed
        )
        self.tags_button.setToolTip(
            "查看当前字幕源文件读到的全部标签（@Title、@Artist 与自定义标签），"
            "点击标签把 {占位符} 插入到光标处；插入后不关窗，可连续插入。"
        )
        self.tags_button.clicked.connect(
            lambda: host._on_title_tags_requested(index)
        )
        text_row_layout.addWidget(self.tags_button, 0, Qt.AlignmentFlag.AlignTop)

        info_layout.addWidget(property_field("标题文字", text_row))
        content.addWidget(info_section)

        # ---- 外观（三个控件同一行）----
        style_section, style_layout = inline_property_section("外观", section)
        grid = ResponsiveFieldGrid(style_section, min_column_width=170, max_columns=3)

        self.layout_combo = WheelFocusedComboBox(style_section)
        compact_property_control(self.layout_combo)
        self.layout_combo.setToolTip(
            "标题引用的布局方案（与布局页管理的是同一份列表）："
            "决定标题的锚点、余白与行间距。"
        )
        self.layout_combo.currentIndexChanged.connect(
            lambda _index: host._on_title_card_layout_changed(index)
        )
        grid.add_field("布局方案", self.layout_combo)

        self.scheme_combo = WheelFocusedComboBox(style_section)
        compact_property_control(self.scheme_combo)
        self.scheme_combo.setToolTip(
            "标题的基础字体与颜色：默认用内置「标题」方案，也可引用角色页的配色方案。"
        )
        self.scheme_combo.currentIndexChanged.connect(
            lambda _index: host._on_title_card_scheme_changed(index)
        )
        grid.add_field("配色方案", self.scheme_combo)

        self.scheme_edit_button = FluentPushButton("编辑配色", style_section)
        compact_property_control(self.scheme_edit_button)
        self.scheme_edit_button.setToolTip("前往字体页编辑该配色方案。")
        self.scheme_edit_button.clicked.connect(
            lambda: host._open_title_scheme(index)
        )
        grid.add_field("配色编辑", self.scheme_edit_button)
        style_layout.addWidget(grid)
        content.addWidget(style_section)

        # ---- 显示时段 ----
        time_section, time_layout = inline_property_section("显示时段", section)
        self.mode_combo = WheelFocusedComboBox(section)
        compact_property_control(self.mode_combo)
        for label, value in _TITLE_MODE_OPTIONS:
            self.mode_combo.addItem(label, value)
        self.mode_combo.currentIndexChanged.connect(
            lambda _index: host._update_title(
                index, show_mode=self.mode_combo.currentData()
            )
        )
        time_layout.addWidget(self.mode_combo)

        self.head_row, self.head_row_label, self.head_edits = _build_head_tail_row(
            section,
            host,
            index,
            timecode_factory,
            label="开头",
            fields=(
                ("淡入", 10_000, "fade_in_ms"),
                ("偏移", TITLE_TIME_MAX_MS, "head_offset_ms"),
                ("显示时长", TITLE_TIME_MAX_MS, "duration_ms"),
                ("淡出", 10_000, "fade_out_ms"),
            ),
        )
        time_layout.addWidget(self.head_row)

        self.tail_row, self.tail_row_label, self.tail_edits = _build_head_tail_row(
            section,
            host,
            index,
            timecode_factory,
            label="片尾",
            fields=(
                ("淡入", 10_000, "tail_fade_in_ms"),
                ("偏移", TITLE_TIME_MAX_MS, "tail_offset_ms"),
                ("显示时长", TITLE_TIME_MAX_MS, "tail_duration_ms"),
                ("淡出", 10_000, "tail_fade_out_ms"),
            ),
        )
        time_layout.addWidget(self.tail_row)

        self.windows_container = QWidget(section)
        self.windows_layout = QVBoxLayout(self.windows_container)
        self.windows_layout.setContentsMargins(0, 0, 0, 0)
        self.windows_layout.setSpacing(8)
        time_layout.addWidget(self.windows_container)

        self.add_window_button = FluentPushButton("＋ 添加时间段", section)
        self.add_window_button.setMinimumHeight(30)
        self.add_window_button.setToolTip("为「自定义」模式增加一个显示时间段。")
        self.add_window_button.clicked.connect(
            lambda: host._on_title_card_window_added(index)
        )
        time_layout.addWidget(self.add_window_button)
        content.addWidget(time_section)

        self.window_rows: list[_TitleWindowRow] = []
        self.sync(overlay)

    # ------------------------------------------------------------------
    def insert_tag_placeholder(self, tag: str) -> None:
        """在光标处插入 ``{tag}`` 占位符（走正常文字编辑链路）。"""
        editor = self.text_edit
        cursor = editor.textCursor()
        if cursor.hasSelection():
            cursor.removeSelectedText()
        cursor.insertText(f"{{{tag}}}")
        editor.setTextCursor(cursor)
        editor.setFocus()

    def window_rows_windows(self) -> list[TitleTimeWindow]:
        return [row.to_window() for row in self.window_rows]

    def remove_window_row(self, row: "_TitleWindowRow") -> list[TitleTimeWindow]:
        if row in self.window_rows:
            self.window_rows.remove(row)
            row.setParent(None)
            row.deleteLater()
        return self.window_rows_windows()

    def sync(self, overlay: TitleOverlay) -> None:
        """把条目值回填到控件（阻塞信号，避免打断输入焦点）。"""
        section = self.section
        name = overlay.name or f"标题 {self.index + 1}"
        blocked: list[tuple[QWidget, bool]] = []

        def hold(widget: QWidget) -> None:
            blocked.append((widget, widget.blockSignals(True)))

        hold(section.header_switch)
        section.header_switch.setChecked(overlay.enabled)
        if self.name_edit.text() != name:
            self.name_edit.setText(name)
        # 折叠标题（头部文字）与摘要跟随条目名。
        if section.header.text() != name:
            section.header.setText(name)
        section.set_collapsed_summary(name)

        # 仅在内容不同才回填，避免实时输入时把光标弹到末尾；阻塞信号，
        # 程序化回填（构造/同步）不得向宿主发 textChanged。
        if self.text_edit.toPlainText() != overlay.text_template:
            blocked_text = self.text_edit.blockSignals(True)
            try:
                self.text_edit.setPlainText(overlay.text_template)
            finally:
                self.text_edit.blockSignals(blocked_text)

        hold(self.mode_combo)
        self.mode_combo.setCurrentIndex(
            max(0, self.mode_combo.findData(overlay.show_mode))
        )

        for field_name, editor in self.head_edits.items():
            hold(editor)
            editor.setValue(_effective_time_value(overlay, field_name))
        for field_name, editor in self.tail_edits.items():
            hold(editor)
            editor.setValue(_effective_time_value(overlay, field_name))

        for widget, was_blocked in blocked:
            widget.blockSignals(was_blocked)

        self.sync_time_visibility(overlay)
        self.sync_windows(overlay)

    def sync_layout_combo(self, overlay: TitleOverlay, style: Any) -> None:
        """重建布局下拉条目并选中条目引用（宿主提供 layout_display_name）。"""
        combo = self.layout_combo
        blocked = combo.blockSignals(True)
        try:
            combo.clear()
            combo.addItem(self._host._layout_display_name(style, "default"), 0)
            for index, layout_def in enumerate(style.layouts, start=1):
                combo.addItem(layout_def.name, index)
            target = overlay.layout_index if overlay.layout_index is not None else 0
            combo.setCurrentIndex(max(0, combo.findData(int(target))))
        finally:
            combo.blockSignals(blocked)

    def sync_scheme_combo(self, overlay: TitleOverlay, style: Any) -> None:
        combo = self.scheme_combo
        blocked = combo.blockSignals(True)
        try:
            combo.clear()
            combo.addItem(f"内置「{TITLE_SCHEME_NAME}」", None)
            for name in style.custom_style_schemes:
                if name == TITLE_SCHEME_NAME:
                    continue
                combo.addItem(name, name)
            target = overlay.scheme_name
            if target and combo.findData(target) < 0:
                # 引用的方案已被删除：显示内置回落，模型值不动（渲染同样回落）。
                target = None
            combo.setCurrentIndex(max(0, combo.findData(target)))
        finally:
            combo.blockSignals(blocked)

    def sync_time_visibility(self, overlay: TitleOverlay) -> None:
        mode = overlay.show_mode
        self.head_row.setVisible(mode in {"whole", "head", "head_tail"})
        self.tail_row.setVisible(mode in {"tail", "head_tail"})
        self.head_row_label.setText("全程" if mode == "whole" else "开头")
        custom = mode == "custom"
        self.windows_container.setVisible(custom)
        self.add_window_button.setVisible(custom)

    def sync_windows(self, overlay: TitleOverlay) -> None:
        windows = list(overlay.custom_windows)
        while len(self.window_rows) > len(windows):
            row = self.window_rows.pop()
            row.setParent(None)
            row.deleteLater()
        while len(self.window_rows) < len(windows):
            self._append_window_row(TitleTimeWindow())
        for row, window in zip(self.window_rows, windows):
            row.sync(window)

    def append_window_row(self, window: TitleTimeWindow) -> None:
        self._append_window_row(window)

    def _append_window_row(self, window: TitleTimeWindow) -> None:
        row = _TitleWindowRow(
            self._host,
            self.index,
            window,
            timecode_factory=self._host._title_timecode_factory,
            parent=self.windows_container,
        )
        self.window_rows.append(row)
        self.windows_layout.addWidget(row)


class TitlePropertyPageBuilder:
    """Build the card-list shell while leaving state transitions with the host."""

    def __init__(
        self,
        host: Any,
        *,
        timecode_factory: Callable[[int, int], TimecodeEdit] = TimecodeEdit,
    ) -> None:
        self._host = host
        self._timecode_factory = timecode_factory
        self.cards_layout: Optional[QVBoxLayout] = None
        self.empty_label: Optional[QLabel] = None
        self.add_button: Optional[FluentPushButton] = None

    @property
    def timecode_factory(self) -> Callable[[int, int], TimecodeEdit]:
        return self._timecode_factory

    def make_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        self.empty_label = QLabel("暂无标题条目。")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setVisible(False)
        layout.addWidget(self.empty_label)

        cards_container = QWidget(page)
        self.cards_layout = QVBoxLayout(cards_container)
        self.cards_layout.setContentsMargins(0, 0, 0, 0)
        self.cards_layout.setSpacing(10)
        layout.addWidget(cards_container)

        self.add_button = FluentPushButton("＋ 添加标题", page)
        self.add_button.setMinimumHeight(34)
        self.add_button.setToolTip("新增一个标题条目（文字、外观与显示时段独立配置）。")
        self.add_button.clicked.connect(self._host._on_title_add_requested)
        layout.addWidget(self.add_button)

        layout.addStretch(1)
        return page


def _card_section(name: str) -> tuple[CollapsibleSection, QVBoxLayout]:
    return property_section(name, switch=True)


def _build_head_tail_row(
    section: QWidget,
    host: Any,
    card_index: int,
    timecode_factory: Callable[[int, int], TimecodeEdit],
    *,
    label: str,
    fields: tuple[tuple[str, int, str], ...],
) -> tuple[QWidget, QLabel, dict[str, TimecodeEdit]]:
    row = QWidget(section)
    row_layout = QHBoxLayout(row)
    row_layout.setContentsMargins(0, 0, 0, 0)
    row_layout.setSpacing(8)
    row_label = subgroup_label(label)
    row_label.setFixedWidth(42)
    row_layout.addWidget(row_label, 0, Qt.AlignmentFlag.AlignTop)

    grid = ResponsiveFieldGrid(row, min_column_width=140, max_columns=4)
    row_layout.addWidget(grid, 1)

    edits: dict[str, TimecodeEdit] = {}
    for field_label, maximum, field_name in fields:
        editor = timecode_factory(0, maximum)
        edits[field_name] = editor
        editor.valueChanged.connect(
            lambda value, field=field_name: host._update_title(
                card_index, **{field: value}
            )
        )
        grid.add_field(field_label, editor)
    return row, row_label, edits


def _effective_time_value(overlay: TitleOverlay, field_name: str) -> int:
    """片尾字段的 None 继承开头值：UI 显示有效值，模型保留 None（字节兼容）。"""
    if field_name == "tail_duration_ms":
        value = overlay.tail_duration_ms
        return int(value if value is not None else overlay.duration_ms)
    if field_name == "tail_fade_in_ms":
        value = overlay.tail_fade_in_ms
        return int(value if value is not None else overlay.fade_in_ms)
    if field_name == "tail_fade_out_ms":
        value = overlay.tail_fade_out_ms
        return int(value if value is not None else overlay.fade_out_ms)
    value = getattr(overlay, field_name)
    return int(value if value is not None else 0)
