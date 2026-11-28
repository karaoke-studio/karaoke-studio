"""Composition of role color controls."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional

from PyQt6.QtCore import QSize, Qt
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QGridLayout, QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import CheckBox, PushButton as FluentPushButton

from krok_helper.subtitle_render.domain.models import (
    GLOW_STRENGTH_MAX,
    GLOW_STRENGTH_MIN,
)

from krok_helper.subtitle_render.frontend.properties.controls.inputs import (
    DynamicStackedWidget,
    WheelFocusedComboBox,
    WheelFocusedDoubleSpinBox,
)
from krok_helper.subtitle_render.frontend.properties.controls.layout import (
    compact_property_control,
    inline_property_section,
    property_field,
    property_section,
)
from krok_helper.subtitle_render.frontend.properties.pages.timing import timing_spin
from krok_helper.subtitle_render.frontend.properties.controls.widgets import FolderTabPanel, PillSelector
from krok_helper.subtitle_render.frontend.widgets.theme import palette, themed


def _plain_double_spin(minimum: float, maximum: float, *, decimals: int = 2):
    spin = WheelFocusedDoubleSpinBox()
    spin.setRange(minimum, maximum)
    spin.setDecimals(decimals)
    spin.setSingleStep(0.1)
    return spin


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
        double_spin_factory: Optional[Callable[..., Any]] = None,
    ) -> None:
        self._host = host
        self._anchored_action_factory = anchored_action_factory
        self._color_state_swap_icon = color_state_swap_icon
        self._fill_mode_icons_provider = fill_mode_icons_provider
        self._spin_factory = spin_factory
        self._combo_factory = combo_factory
        self._double_spin_factory = double_spin_factory or _plain_double_spin

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
        host._color_layer_combo.addItem("阴影", "shadow")
        host._color_layer_combo.addItem("发光", "glow")
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
                ("shadow", "阴影"),
                ("glow", "发光"),
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
            ("径向渐变", "gradient_radial"),
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
        # 填充方式改竖排按钮列，与隐藏 combo 双向同步：
        # 横/纵渐变合入「渐变」按钮并新增「径向」，保持 5 个对称按钮；
        # 按钮 → combo 触发 _update_current_fill，_sync_color_fill_controls 设 combo → 按钮跟随。
        host._fill_mode_pill = PillSelector(
            (
                ("solid", "全色"),
                ("gradient_linear", "渐变"),
                ("gradient_radial", "径向"),
                ("split_vertical", "拼色"),
                ("image", "图像"),
            ),
            section,
            vertical=True,
            icons=self._fill_mode_icons_provider(),
        )

        def on_fill_mode_pill_changed(mode: str) -> None:
            if mode == "gradient_linear":
                current = str(host._fill_mode_combo.currentData() or "")
                target = (
                    current
                    if current in {"gradient_horizontal", "gradient_vertical"}
                    else "gradient_vertical"
                )
            else:
                target = mode
            host._fill_mode_combo.setCurrentIndex(
                max(0, host._fill_mode_combo.findData(target))
            )

        def on_fill_mode_combo_sync(_index: int) -> None:
            current = str(host._fill_mode_combo.currentData() or "solid")
            pill_key = (
                "gradient_linear"
                if current in {"gradient_horizontal", "gradient_vertical"}
                else current
            )
            host._fill_mode_pill.set_current(pill_key)

        host._fill_mode_pill.changed.connect(on_fill_mode_pill_changed)
        host._fill_mode_combo.currentIndexChanged.connect(on_fill_mode_combo_sync)

        # Keep the old combo as a programmatic compatibility adapter for existing
        # callers; the user-facing controls are independent switches.
        host._decoration_type_combo = self._combo_factory(section)
        for label, value in (("无", "none"), ("阴影", "shadow"), ("发光", "glow")):
            host._decoration_type_combo.addItem(label, value)
        host._decoration_type_combo.hide()
        def legacy_decoration_changed(_index: int) -> None:
            kind = str(host._decoration_type_combo.currentData())
            if not getattr(host, "_syncing", False) and kind in {"shadow", "glow"}:
                host._color_layer_combo.setCurrentIndex(
                    host._color_layer_combo.findData(kind)
                )
            host._update_shared_decoration(decoration_kind=kind)
        host._decoration_type_combo.currentIndexChanged.connect(legacy_decoration_changed)
        host._shadow_enabled_check = CheckBox("阴影", section)
        host._glow_enabled_check = CheckBox("发光", section)
        host._shadow_enabled_check.toggled.connect(
            lambda checked: host._toggle_decoration_layer("shadow", checked)
        )
        host._glow_enabled_check.toggled.connect(
            lambda checked: host._toggle_decoration_layer("glow", checked)
        )
        host._decoration_sync_check = CheckBox("同步", section)
        host._decoration_sync_check.setChecked(True)
        host._decoration_sync_check.setToolTip(
            "勾选时，走字前的装饰类型、发光浓度、阴影 X/Y 与走字后保持一致；"
            "发光宽度始终前后独立。旧工程默认不勾选。"
        )
        host._decoration_sync_check.toggled.connect(
            host._on_decoration_sync_toggled
        )
        host._decoration_type_row = QWidget(section)
        decoration_type_layout = QHBoxLayout(host._decoration_type_row)
        decoration_type_layout.setContentsMargins(0, 0, 0, 0)
        decoration_type_layout.setSpacing(8)
        decoration_type_layout.addWidget(host._shadow_enabled_check)
        decoration_type_layout.addWidget(host._glow_enabled_check)
        decoration_type_layout.addWidget(
            host._decoration_sync_check, 0, Qt.AlignmentFlag.AlignVCenter
        )
        host._decoration_type_field = property_field(
            "文字装饰", host._decoration_type_row
        )

        host._fill_editor_stack = DynamicStackedWidget(section)
        fill_pages = (
            host._make_solid_fill_page(),
            host._make_gradient_fill_page(),
            host._make_split_fill_page(),
            host._make_image_fill_page(),
        )
        for page in fill_pages:
            host._fill_editor_stack.addWidget(page)
        for page in fill_pages:
            page_layout = page.layout()
            if page_layout is not None:
                page_layout.activate()
        # 填充页的 preferred width 必须与当前模式无关：横渐变页的渐变条
        # 明显比全色/纵渐变/图片宽，若 sizeHint 跟着当前页走，颜色/字体并排
        # 的断点与 stretch 会在切换填充方式时跳变。取所有页最小宽度的最大值，
        # 保证每页都能放下，同时提供一个稳定的推荐宽度。
        host._fill_editor_stack.set_stable_width(
            max(
                1,
                max(page.minimumSizeHint().width() for page in fill_pages),
            )
        )

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

        host._shadow_blur_spin = self._spin_factory(0, 120, suffix=" px")
        host._shadow_blur_spin.valueChanged.connect(
            lambda value: host._update_shared_decoration(shadow_blur_px=value)
        )
        host._shadow_blur_field = property_field("阴影柔化", host._shadow_blur_spin)
        detail_layout.addWidget(host._shadow_blur_field, 2, 0)
        host._shadow_spread_spin = self._spin_factory(0, 120, suffix=" px")
        host._shadow_spread_spin.valueChanged.connect(
            lambda value: host._update_shared_decoration(shadow_spread_px=value)
        )
        host._shadow_spread_field = property_field("阴影扩张", host._shadow_spread_spin)
        detail_layout.addWidget(host._shadow_spread_field, 2, 1)

        host._glow_before_radius_spin = self._spin_factory(0, 120, suffix=" px")
        host._glow_before_radius_spin.valueChanged.connect(
            lambda value: host._update_shared_decoration(
                glow_before_radius_px=value,
            )
        )
        host._glow_radius_spin = host._glow_before_radius_spin
        host._glow_radius_field = property_field(
            "走字前发光", host._glow_before_radius_spin
        )

        host._glow_after_radius_spin = self._spin_factory(0, 120, suffix=" px")
        host._glow_after_radius_spin.valueChanged.connect(
            lambda value: host._update_shared_decoration(glow_after_radius_px=value)
        )
        host._glow_after_radius_field = property_field(
            "走字后发光", host._glow_after_radius_spin
        )

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

        # 发光强度只有 Skia 后端会绘制；非 Skia 时由面板隐藏该字段。
        host._glow_strength_spin = self._double_spin_factory(
            GLOW_STRENGTH_MIN, GLOW_STRENGTH_MAX, decimals=1
        )
        host._glow_strength_spin.setPrefix("×")
        host._glow_strength_spin.setToolTip(
            "×1.0 为原样；调高时靠近文字的光晕更亮、核心逐渐发白，调低时变淡，"
            "×0 不发光。光晕范围不随之扩大。走字前后与注音共用；仅 Skia 渲染时生效。"
        )
        host._glow_strength_spin.valueChanged.connect(
            lambda value: host._update_glow_strength(value)
        )
        host._glow_strength_field = property_field(
            "发光强度", host._glow_strength_spin
        )

        # 发光的三个参数共享一行，顺序与预览语义一致：走字前、走字后、浓度。
        host._glow_controls_row = QWidget(detail_grid)
        glow_row_layout = QHBoxLayout(host._glow_controls_row)
        glow_row_layout.setContentsMargins(0, 0, 0, 0)
        glow_row_layout.setSpacing(8)
        glow_row_layout.addWidget(host._glow_radius_field, 1)
        glow_row_layout.addWidget(host._glow_after_radius_field, 1)
        glow_row_layout.addWidget(host._glow_concentration_field, 1)
        glow_row_layout.addWidget(host._glow_strength_field, 1)
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
            "勾选后，注音的文字、描边、描边2、装饰及全部填充参数实时跟随主文字配色；"
            "取消勾选后整卡转为独立设置（当前值自动保留）。"
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
