"""Fill-editor pages used by the role color section."""

from __future__ import annotations

from typing import Any, Callable

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QGridLayout, QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
    CheckBox,
    FluentIcon as FIF,
    LineEdit as FluentLineEdit,
    PushButton as FluentPushButton,
    TransparentToolButton as FluentTransparentToolButton,
)

from krok_helper.subtitle_render.frontend.properties.controls.layout import (
    compact_property_control,
    property_field,
)


class RoleFillPagesBuilder:
    """Build fill editors while color mutations remain on the panel host."""

    def __init__(
        self,
        host: Any,
        *,
        gradient_editor_factory: Callable[..., Any] | None = None,
        color_button_factory: Callable[..., Any] | None = None,
        double_spin_factory: Callable[..., Any] | None = None,
        spin_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._host = host
        self._gradient_editor_factory = gradient_editor_factory
        self._color_button_factory = color_button_factory
        self._double_spin_factory = double_spin_factory
        self._spin_factory = spin_factory

    def make_solid_page(self) -> QWidget:
        host = self._host
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        host._paint_solid_btn = host._paint_color_button("color", "#FFFFFF")
        layout.addWidget(host._paint_solid_btn)
        return page

    def make_gradient_page(self) -> QWidget:
        host = self._host
        if any(
            factory is None
            for factory in (
                self._gradient_editor_factory,
                self._color_button_factory,
                self._double_spin_factory,
            )
        ):
            raise RuntimeError("gradient editor factories are required")

        page = QWidget()
        layout = QGridLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setHorizontalSpacing(8)
        layout.setVerticalSpacing(8)
        host._paint_gradient_start_btn = host._paint_color_button(
            "start_color",
            "#FFFFFF",
        )
        host._paint_gradient_end_btn = host._paint_color_button(
            "end_color",
            "#FF5A6F",
        )
        host._paint_gradient_start_btn.hide()
        host._paint_gradient_end_btn.hide()
        host._gradient_editor = self._gradient_editor_factory(page)
        host._gradient_editor.stopsChanged.connect(host._update_gradient_stops)
        host._gradient_editor.selectedChanged.connect(
            lambda _index: host._sync_gradient_stop_controls()
        )
        host._gradient_bar_field = host._gradient_editor

        host._gradient_stop_color_btn = self._color_button_factory("#FFFFFF", page)
        host._wire_color_edit_session(host._gradient_stop_color_btn)
        host._gradient_stop_color_btn.clicked.connect(host._choose_gradient_stop_color)
        host._gradient_stop_color_btn.colorEntered.connect(
            host._gradient_editor.set_selected_color
        )
        host._gradient_stop_color_btn.screenPickRequested.connect(
            lambda: host._choose_gradient_stop_color(screen_pick=True)
        )
        host._gradient_stop_position_spin = self._double_spin_factory(
            0,
            100,
            decimals=3,
            suffix=" %",
        )
        host._gradient_stop_position_spin.valueChanged.connect(
            host._set_gradient_stop_position
        )
        host._gradient_stop_delete_btn = FluentTransparentToolButton(FIF.DELETE, page)
        host._gradient_stop_delete_btn.setToolTip("删除关键点")
        host._gradient_stop_delete_btn.setAccessibleName("删除关键点")
        host._gradient_stop_delete_btn.setFixedSize(30, 30)
        host._gradient_stop_delete_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        host._gradient_stop_delete_btn.clicked.connect(
            host._gradient_editor.delete_selected_stop
        )
        host._gradient_color_field = property_field(
            "关键点颜色",
            host._gradient_stop_color_btn,
        )
        position_row = QWidget(page)
        position_layout = QHBoxLayout(position_row)
        position_layout.setContentsMargins(0, 0, 0, 0)
        position_layout.setSpacing(6)
        position_layout.addWidget(host._gradient_stop_position_spin, 1)
        position_layout.addWidget(
            host._gradient_stop_delete_btn,
            0,
            Qt.AlignmentFlag.AlignBottom,
        )
        host._gradient_position_field = property_field("关键点位置", position_row)
        host._ruby_horizontal_gradient_with_main_check = CheckBox(
            "注音与主文字共享横向渐变",
            page,
        )
        host._ruby_horizontal_gradient_with_main_check.setChecked(True)
        host._ruby_horizontal_gradient_with_main_check.setToolTip(
            "开启后，注音与下方主文字使用同一个整行横向渐变范围，颜色进度保持一致。"
        )
        host._ruby_horizontal_gradient_with_main_check.toggled.connect(
            lambda checked: host._update_style(
                ruby_horizontal_gradient_with_main=checked
            )
        )
        host._gradient_editor_layout = layout
        host._arrange_stop_editor(
            layout,
            host._gradient_bar_field,
            host._gradient_color_field,
            host._gradient_position_field,
            vertical=False,
            footer=host._ruby_horizontal_gradient_with_main_check,
        )
        return page

    def make_split_page(self) -> QWidget:
        host = self._host
        if any(
            factory is None
            for factory in (
                self._gradient_editor_factory,
                self._color_button_factory,
                self._double_spin_factory,
            )
        ):
            raise RuntimeError("split editor factories are required")

        page = QWidget()
        layout = QGridLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setHorizontalSpacing(8)
        layout.setVerticalSpacing(8)
        host._split_editor = self._gradient_editor_factory(page)
        host._split_editor.set_orientation("split_vertical")
        host._split_editor.stopsChanged.connect(host._update_split_stops)
        host._split_editor.selectedChanged.connect(
            lambda _index: host._sync_split_stop_controls()
        )
        host._split_bar_field = host._split_editor

        host._split_stop_color_btn = self._color_button_factory("#FFFFFF", page)
        host._wire_color_edit_session(host._split_stop_color_btn)
        host._split_stop_color_btn.clicked.connect(host._choose_split_stop_color)
        host._split_stop_color_btn.colorEntered.connect(
            host._split_editor.set_selected_color
        )
        host._split_stop_color_btn.screenPickRequested.connect(
            lambda: host._choose_split_stop_color(screen_pick=True)
        )
        host._split_stop_position_spin = self._double_spin_factory(
            0,
            100,
            decimals=3,
            suffix=" %",
        )
        host._split_stop_position_spin.valueChanged.connect(
            host._set_split_stop_position
        )
        host._split_stop_delete_btn = FluentTransparentToolButton(FIF.DELETE, page)
        host._split_stop_delete_btn.setToolTip("删除分段点")
        host._split_stop_delete_btn.setAccessibleName("删除分段点")
        host._split_stop_delete_btn.setFixedSize(30, 30)
        host._split_stop_delete_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        host._split_stop_delete_btn.clicked.connect(
            host._split_editor.delete_selected_stop
        )
        host._split_color_field = property_field(
            "分段颜色",
            host._split_stop_color_btn,
        )
        position_row = QWidget(page)
        position_layout = QHBoxLayout(position_row)
        position_layout.setContentsMargins(0, 0, 0, 0)
        position_layout.setSpacing(6)
        position_layout.addWidget(host._split_stop_position_spin, 1)
        position_layout.addWidget(
            host._split_stop_delete_btn,
            0,
            Qt.AlignmentFlag.AlignBottom,
        )
        host._split_position_field = property_field("分段位置", position_row)
        host._arrange_stop_editor(
            layout,
            host._split_bar_field,
            host._split_color_field,
            host._split_position_field,
            vertical=True,
        )
        return page

    def make_image_page(self) -> QWidget:
        host = self._host
        if self._spin_factory is None:
            raise RuntimeError("image fill spin factory is required")

        page = QWidget()
        layout = QGridLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setHorizontalSpacing(8)
        layout.setVerticalSpacing(8)
        host._paint_image_path_edit = FluentLineEdit(page)
        compact_property_control(host._paint_image_path_edit)
        host._paint_image_path_edit.editingFinished.connect(
            lambda: host._update_current_fill(
                image_path=host._paint_image_path_edit.text()
            )
        )
        host._paint_image_browse_btn = FluentPushButton("浏览...", page)
        host._paint_image_browse_btn.setMinimumHeight(32)
        host._paint_image_browse_btn.clicked.connect(host._choose_paint_image)
        host._paint_image_scale_spin = self._spin_factory(1, 1000, suffix=" %")
        host._paint_image_scale_spin.valueChanged.connect(
            lambda value: host._update_current_fill(image_scale_pct=value)
        )
        path_row = QWidget(page)
        path_layout = QHBoxLayout(path_row)
        path_layout.setContentsMargins(0, 0, 0, 0)
        path_layout.setSpacing(4)
        path_layout.addWidget(host._paint_image_path_edit, 1)
        path_layout.addWidget(host._paint_image_browse_btn)
        layout.addWidget(property_field("图像文件", path_row), 0, 0, 1, 2)
        layout.addWidget(property_field("缩放", host._paint_image_scale_spin), 1, 0)
        layout.setColumnStretch(0, 1)
        layout.setColumnStretch(1, 1)
        return page
