"""Composition of role color controls."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional

from PyQt6.QtCore import QSize, Qt
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QGridLayout, QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import CheckBox, PushButton as FluentPushButton

from krok_helper.subtitle_render.frontend.properties.property_inputs import (
    DynamicStackedWidget,
    WheelFocusedComboBox,
)
from krok_helper.subtitle_render.frontend.properties.property_layout import (
    compact_property_control,
    inline_property_section,
    property_field,
    property_section,
)
from krok_helper.subtitle_render.frontend.properties.property_timing_page import timing_spin
from krok_helper.subtitle_render.frontend.properties.property_widgets import FolderTabPanel, PillSelector
from krok_helper.subtitle_render.frontend.widgets.theme import palette, themed


class RoleColorPropertyPageBuilder:
    """Build color targets and editors while mutations stay on the host."""

    def __init__(
        self,
        host: Any,
        *,
        anchored_action_factory: Callable[..., Any],
        color_state_swap_icon: Path,
        fill_mode_icons_provider: Callable[[], dict[str, QIcon]],
        spin_factory: Callable[..., Any] = timing_spin,
        combo_factory: Callable[..., Any] = WheelFocusedComboBox,
    ) -> None:
        self._host = host
        self._anchored_action_factory = anchored_action_factory
        self._color_state_swap_icon = color_state_swap_icon
        self._fill_mode_icons_provider = fill_mode_icons_provider
        self._spin_factory = spin_factory
        self._combo_factory = combo_factory

    def make_section(
        self, parent: Optional[QWidget] = None, *, inline: bool = False
    ) -> QWidget:
        host = self._host
        section, layout = (
            inline_property_section("颜色", parent)
            if inline
            else property_section("颜色")
        )

        # 编辑对象 / 走字前后 / 图层 / 填充方式的下拉全部转为隐藏取值后端，
        # 界面换成文件夹式 tab + 竖排按钮列；依赖 currentData 的取值 / 同步
        # 逻辑与测试都无需改动。
        host._color_subject_combo = self._combo_factory(section)
        host._color_subject_combo.addItem("主文字", "main")
        host._color_subject_combo.addItem("注音", "ruby")
        host._color_subject_combo.hide()
        host._color_subject_combo.currentIndexChanged.connect(
            lambda _index: host._on_color_subject_changed()
        )

        host._color_state_combo = self._combo_factory(section)
        host._color_state_combo.addItem("走字前", "before")
        host._color_state_combo.addItem("走字后", "after")
        host._color_state_combo.setCurrentIndex(1)
        host._color_state_combo.hide()
        host._color_state_combo.currentIndexChanged.connect(
            lambda _index: host._on_color_target_combo_changed()
        )

        host._color_layer_combo = self._combo_factory(section)
        host._color_layer_combo.addItem("文字", "text")
        host._color_layer_combo.addItem("描边", "stroke")
        host._color_layer_combo.addItem("描边2", "stroke2")
        host._color_layer_combo.addItem("装饰", "shadow")
        host._color_layer_combo.hide()
        host._color_layer_combo.currentIndexChanged.connect(
            lambda _index: host._on_color_target_combo_changed()
        )

        # 文件夹式 tab 面板：左上走字后/走字前，右上主文字/注音。
        host._color_tab_panel = FolderTabPanel(
            (("after", "走字后"), ("before", "走字前")),
            (("main", "主文字"), ("ruby", "注音")),
            section,
        )
        host._color_tab_panel.leftChanged.connect(host._on_color_state_tab_changed)
        host._color_tab_panel.rightChanged.connect(host._on_color_subject_tab_changed)
        host._color_state_swap_button = self._anchored_action_factory(
            host._color_tab_panel,
            ("left", "after"),
            ("left", "before"),
            section,
        )
        host._color_state_swap_button.setObjectName("ColorStateSwapButton")
        host._color_state_swap_button.setIcon(QIcon(str(self._color_state_swap_icon)))
        host._color_state_swap_button.setIconSize(QSize(16, 16))
        host._color_state_swap_button.setFixedSize(22, 22)
        host._color_state_swap_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        host._color_state_swap_button.setCursor(Qt.CursorShape.PointingHandCursor)
        host._color_state_swap_button.setToolTip("交换走字前后配色")
        host._color_state_swap_button.clicked.connect(
            host._swap_karaoke_color_states
        )
        themed(
            host._color_state_swap_button,
            lambda: (
                "QToolButton#ColorStateSwapButton {"
                f" background: {palette().card_bg};"
                f" border: 1px solid {palette().input_border};"
                " border-radius: 11px; padding: 2px; }"
                "QToolButton#ColorStateSwapButton:hover {"
                f" background: {palette().secondary_button_hover_bg}; }}"
                "QToolButton#ColorStateSwapButton:pressed {"
                f" background: {palette().secondary_button_pressed_bg}; }}"
            ),
        )
        layout.addWidget(host._color_tab_panel)

        host._color_layer_pill = PillSelector(
            (
                ("text", "文字"),
                ("stroke", "描边"),
                ("stroke2", "描边2"),
                ("shadow", "装饰"),
            ),
            section,
            vertical=True,
        )
        host._color_layer_pill.changed.connect(host._on_color_layer_pill_changed)

        host._fill_mode_combo = self._combo_factory(section)
        compact_property_control(host._fill_mode_combo)
        for label, value in [
            ("全色", "solid"),
            ("横向渐变", "gradient_horizontal"),
            ("纵向渐变", "gradient_vertical"),
            ("纵向拼色", "split_vertical"),
            ("图像", "image"),
        ]:
            host._fill_mode_combo.addItem(label, value)
        host._fill_mode_combo.hide()
        host._fill_mode_combo.currentIndexChanged.connect(
            lambda _index: host._update_current_fill(
                mode=str(host._fill_mode_combo.currentData())
            )
        )
        # 填充方式改竖排按钮列，与隐藏 combo 双向同步：按钮 → combo 触发
        # _update_current_fill；_sync_color_fill_controls 设 combo → 按钮跟随
        host._fill_mode_pill = PillSelector(
            (
                ("solid", "全色"),
                ("gradient_horizontal", "横渐变"),
                ("gradient_vertical", "纵渐变"),
                ("split_vertical", "拼色"),
                ("image", "图像"),
            ),
            section,
            vertical=True,
            icons=self._fill_mode_icons_provider(),
        )
        host._fill_mode_pill.changed.connect(
            lambda mode: host._fill_mode_combo.setCurrentIndex(
                max(0, host._fill_mode_combo.findData(mode))
            )
        )
        host._fill_mode_combo.currentIndexChanged.connect(
            lambda _index: host._fill_mode_pill.set_current(
                str(host._fill_mode_combo.currentData())
            )
        )

        host._decoration_type_combo = self._combo_factory(section)
        compact_property_control(host._decoration_type_combo)
        host._decoration_type_combo.addItem("无", "none")
        host._decoration_type_combo.addItem("阴影", "shadow")
        host._decoration_type_combo.addItem("发光", "glow")
        host._decoration_type_combo.currentIndexChanged.connect(
            lambda _index: host._update_shared_decoration(
                decoration_kind=str(host._decoration_type_combo.currentData())
            )
        )
        host._decoration_type_field = property_field("装饰类型", host._decoration_type_combo)

        host._fill_editor_stack = DynamicStackedWidget(section)
        host._fill_editor_stack.addWidget(host._make_solid_fill_page())
        host._fill_editor_stack.addWidget(host._make_gradient_fill_page())
        host._fill_editor_stack.addWidget(host._make_split_fill_page())
        host._fill_editor_stack.addWidget(host._make_image_fill_page())

        detail_grid = QWidget(section)
        detail_layout = QGridLayout(detail_grid)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        detail_layout.setHorizontalSpacing(8)
        detail_layout.setVerticalSpacing(8)

        host._shadow_x_spin = self._spin_factory(-40, 40, suffix=" px")
        host._shadow_x_spin.valueChanged.connect(
            lambda value: host._update_shared_decoration(shadow_offset_x=value)
        )
        host._shadow_x_field = property_field("阴影 X", host._shadow_x_spin)
        detail_layout.addWidget(host._shadow_x_field, 1, 0)

        host._shadow_y_spin = self._spin_factory(-40, 40, suffix=" px")
        host._shadow_y_spin.valueChanged.connect(
            lambda value: host._update_shared_decoration(shadow_offset_y=value)
        )
        host._shadow_y_field = property_field("阴影 Y", host._shadow_y_spin)
        detail_layout.addWidget(host._shadow_y_field, 1, 1)

        host._glow_before_radius_spin = self._spin_factory(0, 120, suffix=" px")
        host._glow_before_radius_spin.valueChanged.connect(
            lambda value: host._update_shared_decoration(
                glow_before_radius_px=value,
            )
        )
        host._glow_radius_spin = host._glow_before_radius_spin
        host._glow_radius_field = property_field("走字前发光", host._glow_before_radius_spin)

        host._glow_after_radius_spin = self._spin_factory(0, 120, suffix=" px")
        host._glow_after_radius_spin.valueChanged.connect(
            lambda value: host._update_shared_decoration(glow_after_radius_px=value)
        )
        host._glow_after_radius_field = property_field("走字后发光", host._glow_after_radius_spin)

        host._glow_concentration_combo = self._combo_factory(section)
        compact_property_control(host._glow_concentration_combo)
        for label, value in [("无", -1), ("低", 0), ("中", 1), ("高", 2)]:
            host._glow_concentration_combo.addItem(label, value)
        host._glow_concentration_combo.currentIndexChanged.connect(
            lambda _index: host._update_shared_decoration(
                glow_concentration_level=int(
                    host._glow_concentration_combo.currentData() or 0
                )
            )
        )
        host._glow_concentration_field = property_field(
            "发光浓度", host._glow_concentration_combo
        )

        # 发光的三个参数共享一行，顺序与预览语义一致：走字前、走字后、浓度。
        host._glow_controls_row = QWidget(detail_grid)
        glow_row_layout = QHBoxLayout(host._glow_controls_row)
        glow_row_layout.setContentsMargins(0, 0, 0, 0)
        glow_row_layout.setSpacing(8)
        glow_row_layout.addWidget(host._glow_radius_field, 1)
        glow_row_layout.addWidget(host._glow_after_radius_field, 1)
        glow_row_layout.addWidget(host._glow_concentration_field, 1)
        detail_layout.addWidget(host._glow_controls_row, 1, 0, 1, 2)

        detail_layout.setColumnStretch(0, 1)
        detail_layout.setColumnStretch(1, 1)

        host._ruby_color_actions_row = QWidget(section)
        ruby_color_actions_layout = QHBoxLayout(host._ruby_color_actions_row)
        ruby_color_actions_layout.setContentsMargins(0, 0, 0, 0)
        ruby_color_actions_layout.setSpacing(10)
        host._ruby_colors_follow_main_check = CheckBox(
            "默认跟随主文字", host._ruby_color_actions_row
        )
        host._ruby_colors_follow_main_check.setChecked(True)
        host._ruby_colors_follow_main_check.setToolTip(
            "勾选后，注音的文字、描边、描边2、装饰及全部填充参数实时跟随主文字配色。"
        )
        host._ruby_colors_follow_main_check.toggled.connect(
            host._on_ruby_colors_follow_main_toggled
        )
        host._ruby_apply_main_btn = FluentPushButton(
            "应用主文字配色", host._ruby_color_actions_row
        )
        host._ruby_apply_main_btn.setMinimumHeight(32)
        host._ruby_apply_main_btn.clicked.connect(host._apply_main_colors_to_ruby)
        ruby_color_actions_layout.addWidget(host._ruby_colors_follow_main_check, 0)
        ruby_color_actions_layout.addWidget(host._ruby_apply_main_btn, 1)
        host._set_ruby_color_controls_visible(False)

        # tab 内容区：左·图层列 + 填充方式列（竖排按钮），右·填充编辑和
        # 整个配色方案共用的装饰参数。描边尺寸已经归入字体卡片。
        columns = QHBoxLayout()
        columns.setContentsMargins(0, 0, 0, 0)
        columns.setSpacing(10)
        columns.addWidget(host._color_layer_pill, 0, Qt.AlignmentFlag.AlignTop)
        columns.addWidget(host._fill_mode_pill, 0, Qt.AlignmentFlag.AlignTop)
        editors = QVBoxLayout()
        editors.setContentsMargins(0, 0, 0, 0)
        editors.setSpacing(10)
        editors.addWidget(host._decoration_type_field)
        editors.addWidget(host._fill_editor_stack)
        editors.addWidget(detail_grid)
        editors.addStretch(1)
        columns.addLayout(editors, 1)
        host._color_tab_panel.content_layout.addLayout(columns)
        host._color_tab_panel.content_layout.addWidget(host._ruby_color_actions_row)
        return section


