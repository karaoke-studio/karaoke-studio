"""Title-property page construction isolated from the property panel host."""

from __future__ import annotations

from typing import Any, Callable

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QWidget
from qfluentwidgets import PushButton as FluentPushButton

from krok_helper.subtitle_render.frontend.property_inputs import (
    GrowingPlainTextEdit,
    TimecodeEdit,
    WheelFocusedComboBox,
)
from krok_helper.subtitle_render.frontend.property_layout import (
    ResponsiveFieldGrid,
    compact_property_control,
    property_field,
    property_section,
)
from krok_helper.subtitle_render.frontend.property_widgets import subgroup_label


TITLE_TIME_MAX_MS = 5_999_990


class TitlePropertyPageBuilder:
    """Build title controls while leaving state transitions with the host."""

    def __init__(
        self,
        host: Any,
        *,
        timecode_factory: Callable[[int, int], TimecodeEdit] = TimecodeEdit,
    ) -> None:
        self._host = host
        self._timecode_factory = timecode_factory

    def make_text_section(self) -> QFrame:
        host = self._host
        section, layout = property_section("标题", switch=True)
        host._title_enabled_switch = section.header_switch
        host._title_enabled_switch.toggled.connect(host._on_title_enabled_toggled)

        host._title_text_edit = GrowingPlainTextEdit(section)
        host._title_text_edit.setPlaceholderText("{title} / {artist}")
        host._title_text_edit.setToolTip(
            "支持换行；{title} / {artist} 会从字幕元数据中读取。"
        )
        host._title_text_edit.textChanged.connect(host._on_title_text_changed)
        host._title_text_edit.editingFinished.connect(host._commit_title_text_edit)
        layout.addWidget(property_field("标题文字", host._title_text_edit))
        return section

    def make_style_section(self) -> QFrame:
        host = self._host
        section, layout = property_section("外观")
        host._title_appearance_grid = ResponsiveFieldGrid(
            section,
            min_column_width=260,
            max_columns=2,
        )

        host._title_layout_combo = WheelFocusedComboBox(section)
        compact_property_control(host._title_layout_combo)
        host._title_layout_combo.setToolTip(
            "标题引用的布局方案（与布局页管理的是同一份列表）："
            "决定标题的锚点、余白与行间距。"
        )
        host._title_layout_combo.currentIndexChanged.connect(
            host._on_title_layout_changed
        )
        host._title_appearance_grid.add_field(
            "布局方案",
            host._title_layout_combo,
        )

        host._title_scheme_edit_btn = FluentPushButton("编辑标题配色", section)
        host._title_scheme_edit_btn.setMinimumHeight(30)
        host._title_scheme_edit_btn.setToolTip(
            "字体与颜色由字体页的「标题」配色方案决定，点击前往编辑。"
        )
        host._title_scheme_edit_btn.clicked.connect(host._open_title_scheme)
        host._title_appearance_grid.add_field(
            "字体与颜色",
            host._title_scheme_edit_btn,
        )
        layout.addWidget(host._title_appearance_grid)
        return section

    def make_time_section(self) -> QFrame:
        host = self._host
        section, layout = property_section("显示时段")
        host._title_time_grid = ResponsiveFieldGrid(
            section,
            min_column_width=150,
            max_columns=1,
        )

        host._title_mode_combo = WheelFocusedComboBox(section)
        compact_property_control(host._title_mode_combo)
        for label, value in (
            ("全程显示", "whole"),
            ("仅开头", "head"),
            ("仅片尾", "tail"),
            ("开始和片尾", "head_tail"),
        ):
            host._title_mode_combo.addItem(label, value)
        host._title_mode_combo.currentIndexChanged.connect(
            lambda _index: host._update_title(
                show_mode=host._title_mode_combo.currentData()
            )
        )
        host._title_time_grid.add_field("显示模式", host._title_mode_combo)
        layout.addWidget(host._title_time_grid)

        host._title_head_row = QWidget(section)
        head_row_layout = QHBoxLayout(host._title_head_row)
        head_row_layout.setContentsMargins(0, 0, 0, 0)
        head_row_layout.setSpacing(8)
        host._title_head_row_label = subgroup_label("开头")
        host._title_head_row_label.setFixedWidth(42)
        head_row_layout.addWidget(
            host._title_head_row_label,
            0,
            Qt.AlignmentFlag.AlignTop,
        )
        host._title_head_grid = ResponsiveFieldGrid(
            host._title_head_row,
            min_column_width=140,
            max_columns=4,
        )
        head_row_layout.addWidget(host._title_head_grid, 1)
        self._add_timecode(
            host._title_head_grid,
            "_title_fade_in_edit",
            "淡入",
            10_000,
            "fade_in_ms",
        )
        self._add_timecode(
            host._title_head_grid,
            "_title_head_edit",
            "偏移",
            TITLE_TIME_MAX_MS,
            "head_offset_ms",
        )
        self._add_timecode(
            host._title_head_grid,
            "_title_duration_edit",
            "显示时长",
            TITLE_TIME_MAX_MS,
            "duration_ms",
        )
        self._add_timecode(
            host._title_head_grid,
            "_title_fade_out_edit",
            "淡出",
            10_000,
            "fade_out_ms",
        )

        host._title_tail_row = QWidget(section)
        tail_row_layout = QHBoxLayout(host._title_tail_row)
        tail_row_layout.setContentsMargins(0, 0, 0, 0)
        tail_row_layout.setSpacing(8)
        host._title_tail_row_label = subgroup_label("片尾")
        host._title_tail_row_label.setFixedWidth(42)
        tail_row_layout.addWidget(
            host._title_tail_row_label,
            0,
            Qt.AlignmentFlag.AlignTop,
        )
        host._title_tail_grid = ResponsiveFieldGrid(
            host._title_tail_row,
            min_column_width=140,
            max_columns=4,
        )
        tail_row_layout.addWidget(host._title_tail_grid, 1)
        self._add_timecode(
            host._title_tail_grid,
            "_title_tail_fade_in_edit",
            "淡入",
            10_000,
            "tail_fade_in_ms",
        )
        self._add_timecode(
            host._title_tail_grid,
            "_title_tail_edit",
            "偏移",
            TITLE_TIME_MAX_MS,
            "tail_offset_ms",
        )
        self._add_timecode(
            host._title_tail_grid,
            "_title_tail_duration_edit",
            "显示时长",
            TITLE_TIME_MAX_MS,
            "tail_duration_ms",
        )
        self._add_timecode(
            host._title_tail_grid,
            "_title_tail_fade_out_edit",
            "淡出",
            10_000,
            "tail_fade_out_ms",
        )

        layout.addWidget(host._title_head_row)
        layout.addWidget(host._title_tail_row)
        return section

    def _add_timecode(
        self,
        grid: ResponsiveFieldGrid,
        attribute: str,
        label: str,
        maximum: int,
        model_field: str,
    ) -> None:
        host = self._host
        editor = self._timecode_factory(0, maximum)
        setattr(host, attribute, editor)
        editor.valueChanged.connect(
            lambda value, field=model_field: host._update_title(**{field: value})
        )
        grid.add_field(label, editor)
