"""右侧字幕属性面板。

窄侧栏里不要使用横向表单布局：标签和输入框会互相挤压，尤其是
字体选择框。这里采用工具软件常见的分组卡片 + 垂直字段，保证
280-320px 宽度下没有横向溢出。
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
import math
from pathlib import Path
from typing import Any, Optional

from PyQt6.QtCore import (
    QEvent,
    QPoint,
    QRect,
    QSize,
    Qt,
    QTimer,
    pyqtSignal as Signal,
)
from PyQt6.QtGui import (
    QColor,
    QCursor,
    QFont,
    QFontDatabase,
    QFontInfo,
    QIcon,
)
from PyQt6.QtWidgets import (
    QBoxLayout,
    QColorDialog,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
    QWIDGETSIZE_MAX,
)
from qfluentwidgets import (
    CaptionLabel,
    CheckBox,
    ComboBox as FluentComboBox,
    FluentIcon as FIF,
    InfoBar,
    LineEdit as FluentLineEdit,
    PushButton as FluentPushButton,
    RadioButton,
    ScrollArea as FluentScrollArea,
    SegmentedWidget,
    SpinBox as FluentSpinBox,
    ToolButton as FluentToolButton,
    TransparentToolButton as FluentTransparentToolButton,
)
from krok_helper.qfluent_compat import install_fluent_tooltip

from krok_helper.subtitle_render.domain.background import BackgroundSource
from krok_helper.subtitle_render.domain.paint import (
    ColorLayerKey,
    ColorStateKey,
    KaraokeColors,
    PaintFill,
)
from krok_helper.subtitle_render.frontend.dialogs.fluent_dialogs import (
    fluent_button_row,
    fluent_choice,
    fluent_get_editable_choice,
    fluent_get_text,
    fluent_question,
    fluent_warning,
)
from krok_helper.subtitle_render.frontend.properties.pages.registry import (
    PROPERTY_PAGE_SPECS,
    build_property_pages,
    property_page_index,
)
from krok_helper.subtitle_render.frontend.properties.controls.layout import (
    _ALIGN_SEGMENT_OPTIONS,
    _POSITION_SEGMENT_OPTIONS,
    _GlyphSegment,
    _LayoutSchematic,
    _SchematicBoard,
    ResponsiveFieldGrid as _ResponsiveFieldGrid,
    ResponsivePropertyPair as _ResponsivePropertyPair,
    ResponsiveRoleHeader as _ResponsiveRoleHeader,
    compact_property_control as _compact_control,
    inline_property_section as _inline_section,
    plain_property_card as _plain_card,
    property_field as _field,
    property_section as _section,
    property_section_pair as _section_pair,
)
from krok_helper.subtitle_render.frontend.properties.controls.inputs import (
    DynamicStackedWidget as _DynamicStackedWidget,
    GrowingPlainTextEdit as _GrowingPlainTextEdit,
    NoWheelSpinBox as _NoWheelSpinBox,
    TimecodeEdit,
    WheelFocusedDoubleSpinBox,
    WheelFocusedComboBox as _WheelFocusedComboBox,
    WheelFocusedFontComboBox,
    WheelFocusedSpinBox,
)
from krok_helper.subtitle_render.frontend.properties.controls.font_preview import (
    _FontPreviewWidget,
    _FontSampleCanvas,
)
from krok_helper.subtitle_render.frontend.properties.pages.background import (
    BACKGROUND_KIND_PAGES,
    BackgroundPropertyPageBuilder,
)
from krok_helper.subtitle_render.frontend.properties.pages.effects import (
    EffectsPropertyPageBuilder,
)
from krok_helper.subtitle_render.frontend.properties.pages.layout import (
    LayoutPropertyPageBuilder,
)
from krok_helper.subtitle_render.frontend.properties.roles.page import (
    RolePropertyPageBuilder,
)
from krok_helper.subtitle_render.frontend.properties.roles.font import (
    RoleFontSettingsPageBuilder,
)
from krok_helper.subtitle_render.frontend.properties.roles.color import (
    RoleColorPropertyPageBuilder,
)
from krok_helper.subtitle_render.frontend.properties.roles.fills import (
    GradientStopsEditor,
    RoleFillPagesBuilder,
    _GradientStopsPasteDialog,
    _gradient_stops,
    _gradient_stops_from_json,
    _gradient_stops_to_json,
    _normalize_gradient_stops,
    _normalized_stop_position,
    _split_stops,
)
from krok_helper.subtitle_render.frontend.properties.controls.widgets import (
    ClickableRow as _ClickableRow,
    CollapsibleSection,
    FolderTabPanel as _FolderTabPanel,
    PillSelector as _PillSelector,
    SubGroup as _SubGroup,
    ToggleSwitch,
    subgroup_label as _subgroup_label,
)
from krok_helper.subtitle_render.frontend.properties.pages.title import (
    TitlePropertyPageBuilder,
)
from krok_helper.subtitle_render.frontend.properties.pages.timing import (
    TimingPropertyPageBuilder,
)
from krok_helper.subtitle_render.frontend import SUBTITLE_RENDER_ASSET_DIR
from krok_helper.subtitle_render.frontend.widgets.theme import palette, themed
from krok_helper.subtitle_render.n3.font_catalog import (
    canonicalize_n3_font_family,
    n3_font_families,
)
from krok_helper.subtitle_render.domain.models import (
    N3_FONT_INHERITANCE_FIELDS,
    StylePreset,
    SubtitleStyleScheme,
    Style,
    TITLE_SCHEME_NAME,
    TitleOverlay,
    effective_karaoke_animation,
    layout_display_name,
)
from krok_helper.subtitle_render.settings.property_controllers import (
    LayoutCatalogController,
    PropertyStyleController,
    RoleSchemeController,
    SCHEME_FIELDS as _SCHEME_FIELDS,
    SCHEME_ONLY_FIELDS as _SCHEME_ONLY_FIELDS,
    TitleOverlayController,
    normalize_decoration_kind as _normalize_decoration_kind,
    normalize_entry_animation as _normalize_entry_animation,
    normalize_exit_animation as _normalize_exit_animation,
    normalize_horizontal_align as _normalize_horizontal_align,
    normalize_horizontal_layout as _normalize_horizontal_layout,
    normalize_karaoke_animation as _normalize_karaoke_animation,
    normalize_line_position as _normalize_line_position,
    normalize_lit_style as _normalize_lit_style,
    normalize_lit_transition_mode as _normalize_lit_transition_mode,
    normalize_viewport_align as _normalize_viewport_align,
)
from krok_helper.subtitle_render.settings.screen import (
    PAR_OPTIONS,
    SCREEN_FPS_OPTIONS,
    SCREEN_PRESETS,
    ScreenPreset,
    ScreenSettings,
    match_screen_preset_key,
    screen_settings_from_dict,
    screen_settings_to_dict,
)
from krok_helper.subtitle_render.engine.timing.timecode import format_timecode_ms, parse_timecode_ms
from krok_helper.subtitle_render.n3.template_import import (
    resolve_n3_template_preset,
)

_GLOBAL_SCHEME_KEY = "global"
_CUSTOM_SCHEME_PREFIX = "custom:"
EDIT_COMMIT_DEBOUNCE_MS = 200
"""数值框 / 标题文字「还在连打」的判定窗口。

停手这么久才把编辑提交给宿主（重建预览、写撤销栈、标脏）。

这个值曾经被抬到 400ms，理由是「每次提交都很贵，少触发几次」。那个前提已经
不成立——提交路径本身快了一个数量级——而它的代价是：用户改完一个值，要先干
等这么久预览才开始动。等待期间界面本来就不好用，再往前加一段纯粹的空等只会
更难受。降回 200ms：仍然覆盖得住正常打字的字符间隔，又不显眼。
"""
_FONT_SIZE_MAX_PX = 4096
_LAYOUT_SIZE_MAX_PX = 16_384
_FILL_MODE_ICON_DIR = SUBTITLE_RENDER_ASSET_DIR / "fill_modes"
_COLOR_STATE_SWAP_ICON = SUBTITLE_RENDER_ASSET_DIR / "swap-colors.svg"
_AUTO_ROLE_COLORS = (
    "#FF5A6F",
    "#00A6FF",
    "#FFCC00",
    "#22C55E",
    "#A855F7",
    "#F97316",
    "#14B8A6",
    "#EC4899",
)
_DEFAULT_FONT_WEIGHTS = (400, 500, 600, 700, 800, 900)
_FONT_WEIGHT_LABELS = {
    100: "极细",
    200: "特细",
    300: "细体",
    400: "常规",
    500: "中等",
    600: "半粗",
    700: "粗体",
    800: "特粗",
    900: "黑体",
}


def _available_font_weights(family: str) -> tuple[int, ...]:
    """Return the distinct weights Qt can actually resolve for a font family."""
    try:
        styles = QFontDatabase.styles(str(family))
        weights = {
            int(QFontDatabase.weight(str(family), style))
            for style in styles
        }
    except (RuntimeError, TypeError, ValueError):
        weights = set()
    normalized = tuple(sorted(weight for weight in weights if 1 <= weight <= 1000))
    # Missing project fonts and headless Qt platforms expose no style metadata.
    # Keep the legacy choices in that case instead of forcing an arbitrary value.
    return normalized or _DEFAULT_FONT_WEIGHTS


def _font_weight_label(weight: int) -> str:
    label = _FONT_WEIGHT_LABELS.get(int(weight), "字重")
    return f"{label} {int(weight)}"


def _supports_synthetic_bold(family: str, physical_weights: tuple[int, ...]) -> bool:
    """Whether Qt resolves a non-physical 700 face as synthetic bold."""
    if 700 in physical_weights:
        return False
    try:
        physical_styles = {
            style.casefold() for style in QFontDatabase.styles(str(family))
        }
        if not physical_styles:
            return False
        requested = QFont(str(family))
        requested.setWeight(QFont.Weight.Bold)
        resolved = QFontInfo(requested)
    except (RuntimeError, TypeError, ValueError):
        return False
    if resolved.family().casefold() != str(family).casefold():
        return False
    resolved_style = resolved.styleName().casefold()
    return "bold" in resolved_style and resolved_style not in physical_styles


_LIT_FIELDS = {
    "lit_enabled",
    "lit_style",
    "lit_number",
    "lit_size",
    "lit_offset_x",
    "lit_offset_y",
    "lit_tracking",
    "lit_fill_color",
    "lit1_fill_color",
    "lit2_fill_color",
    "lit3_fill_color",
    "lit_stroke_color",
    "lit_stroke_width",
    "lit_stroke_soften",
    "lit_opacity_pct",
    "lit_edge_brightness_pct",
    "lit_shadow",
    "lit_time_offset_ms",
    "lit_waiting_time_ms",
    "lit_transition_mode",
    "lit_transition_ratio_pct",
    "lit_transition_angle_deg",
    "lit_transition_distance",
    "signals_duration_ms",
    "volume_size",
    "volume_offset_x",
    "volume_offset_y",
    "volume_column_width",
    "volume_column_count",
    "volume_column_spacing",
    "volume_align",
    "volume_ratio",
    "volume_fill_color",
    "volume_stroke_color",
    "volume_overlay_fill_color",
    "volume_overlay_stroke_color",
    "volume_flash_times",
    "volume_flash_duration_ratio",
    "volume_transition_ratio_pct",
}


from krok_helper.subtitle_render.frontend.properties.color_controls import (
    COLOR_COMMIT_DEBOUNCE_MS,
    ColorButton,
    ScreenColorPicker,
    _ColorDialog,
    _normalize_hex,
    _parse_hex_color,
    _select_color,
)
from krok_helper.subtitle_render.frontend.properties.preset_manager import (
    StylePresetManagerDialog,
    _RolePresetGroupDialog,
    _StylePresetDetailsDialog,
    _new_preset_id,
    _normalize_style_presets,
    _preset_ids_for_pair,
)
def _fill_mode_icons() -> dict[str, QIcon]:
    """Load packaged SVG icons for the five PaintFill modes."""
    return {
        key: QIcon(str(_FILL_MODE_ICON_DIR / filename))
        for key, filename in (
            ("solid", "solid.svg"),
            ("gradient_horizontal", "gradient-horizontal.svg"),
            ("gradient_vertical", "gradient-vertical.svg"),
            ("split_vertical", "split-vertical.svg"),
            ("image", "image.svg"),
        )
    }


class _AnchoredTabActionButton(QToolButton):
    """Overlay action whose bottom edge follows the top seam of two tabs."""

    _REPOSITION_EVENTS = {
        QEvent.Type.LayoutRequest,
        QEvent.Type.Move,
        QEvent.Type.Resize,
        QEvent.Type.Show,
    }

    def __init__(
        self,
        panel: _FolderTabPanel,
        first_tab: tuple[str, str],
        second_tab: tuple[str, str],
        parent: QWidget,
    ) -> None:
        super().__init__(parent)
        self._panel = panel
        self._first_tab = panel._buttons[first_tab]
        self._second_tab = panel._buttons[second_tab]
        for watched in (parent, panel, self._first_tab, self._second_tab):
            watched.installEventFilter(self)
        QTimer.singleShot(0, self.reposition)

    def eventFilter(self, watched, event) -> bool:  # noqa: N802, ARG002
        if event.type() in self._REPOSITION_EVENTS:
            QTimer.singleShot(0, self.reposition)
        return super().eventFilter(watched, event)

    def reposition(self) -> None:
        parent = self.parentWidget()
        if parent is None:
            return
        first_origin = self._first_tab.mapTo(parent, QPoint(0, 0))
        second_origin = self._second_tab.mapTo(parent, QPoint(0, 0))
        seam_x = (
            first_origin.x()
            + self._first_tab.width()
            - 1
            + second_origin.x()
        ) / 2
        tab_top = min(first_origin.y(), second_origin.y())
        self.move(
            int(round(seam_x - (self.width() - 1) / 2)),
            tab_top - self.height(),
        )
        self.raise_()


_BACKGROUND_KIND_PAGES = BACKGROUND_KIND_PAGES


class _WheelFocusedSpinBox(WheelFocusedSpinBox):
    """Bind the shared integer input to this panel's commit debounce."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent, commit_delay_ms=EDIT_COMMIT_DEBOUNCE_MS)


class _WheelFocusedDoubleSpinBox(WheelFocusedDoubleSpinBox):
    """Bind the shared decimal input to this panel's commit debounce."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent, commit_delay_ms=EDIT_COMMIT_DEBOUNCE_MS)


#: 标题「显示时段」几个时刻字段的上限，取 N3 的时间标签上限
#: ``Nkm3Constants.TIME_TAG_TIME_MAX``（``[99:59:99]``，约 100 分钟）。
#:
#: 原来写死 600000（10 分钟），那个数没有来源；N3 里 ``TitleShowTime`` 的
#: HeadOffset / HeadEnd / TailOffset 都直接落在这条时间轴上，没有更窄的限制。
TITLE_TIME_MAX_MS = 5_999_990

class _TimecodeEdit(TimecodeEdit):
    """Bind the shared timecode editor to this panel's commit debounce."""

    def __init__(
        self,
        minimum: int,
        maximum: int,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(
            minimum,
            maximum,
            parent,
            commit_delay_ms=EDIT_COMMIT_DEBOUNCE_MS,
        )


class _WheelFocusedFontComboBox(WheelFocusedFontComboBox):
    """Bind the shared font input to this module's patchable catalog providers."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(
            parent,
            font_families_provider=n3_font_families,
            canonicalize_family=canonicalize_n3_font_family,
        )


class PropertyPanel(QWidget):
    """字体 / 布局 / 特效 / 标题属性面板。"""

    _PAGE_SPECS = PROPERTY_PAGE_SPECS

    styleChanged = Signal(Style)
    pageChanged = Signal(int)
    rolesChanged = Signal(list)
    schemeSelectionChanged = Signal(str)
    presetSchemesChanged = Signal(dict)
    defaultSchemeSaveRequested = Signal(str)
    defaultLayoutSaveRequested = Signal(int)
    layoutAssignAllRequested = Signal(int)
    """「应用到全部页」：参数为布局 index（0 = 默认布局）。"""
    layoutAutoAssignRequested = Signal()
    """「各页按行数自动布局」：不重新分页，只恢复同行数映射布局。"""
    layoutDeleted = Signal(int)
    """布局被删除：参数为被删布局 index（>= 1），宿主需修正歌词行引用。"""
    backgroundBrowseRequested = Signal(str)
    """点击某张背景卡请求选择素材；参数为 kind（video/image/image_sequence/solid）。"""
    backgroundClearRequested = Signal()
    """请求清除背景素材（回到纯色黑）。"""
    backgroundSolidColorChanged = Signal(str)
    """纯色背景色变化（色值输入 / 取色 / 选色对话框）；参数为 #RRGGBB。"""
    imageFitChanged = Signal(str)
    """图片缩放策略变化；参数为 cover（铺满）/ contain（黑边）。"""
    audioBrowseRequested = Signal()
    audioClearRequested = Signal()
    screenSizeChanged = Signal()
    """面板内宽 / 高 / 帧率被用户改动（宿主回写导出页与预览）。"""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._style = Style()
        self._syncing = False
        # 控件是否已按 _style 完整同步过一次；未同步前不能走 set_style 的等值快路径。
        self._style_synced = False
        self._title_text_change_pending = False
        self._title_text_change_timer = QTimer(self)
        self._title_text_change_timer.setSingleShot(True)
        self._title_text_change_timer.setInterval(EDIT_COMMIT_DEBOUNCE_MS)
        self._title_text_change_timer.timeout.connect(self._commit_title_text_edit)
        self._role_controller = RoleSchemeController()
        self._layout_controller = LayoutCatalogController()
        self._title_controller = TitleOverlayController()
        self._style_controller = PropertyStyleController()
        self._title_page_builder = TitlePropertyPageBuilder(
            self,
            timecode_factory=_TimecodeEdit,
        )
        self._timing_page_builder = TimingPropertyPageBuilder(
            self,
            spin_factory=_spin,
            tooltip_installer=install_fluent_tooltip,
        )
        self._background_page_builder = BackgroundPropertyPageBuilder(
            self,
            fps_options=SCREEN_FPS_OPTIONS,
            size_spin_factory=_NoWheelSpinBox,
            fps_combo_factory=FluentComboBox,
            color_button_factory=ColorButton,
            kind_pages=_BACKGROUND_KIND_PAGES,
        )
        self._effects_page_builder = EffectsPropertyPageBuilder(
            self,
            spin_factory=_spin,
        )
        self._layout_page_builder = LayoutPropertyPageBuilder(
            self,
            spin_factory=_spin,
            plain_card_factory=_plain_card,
            glyph_segment_factory=_GlyphSegment,
            layout_schematic_factory=_LayoutSchematic,
            schematic_board_factory=_SchematicBoard,
        )
        self._role_page_builder = RolePropertyPageBuilder(
            self,
            plain_card_factory=_plain_card,
            role_header_factory=_ResponsiveRoleHeader,
            font_preview_factory=_FontPreviewWidget,
            property_pair_factory=_ResponsivePropertyPair,
        )
        self._role_font_page_builder = RoleFontSettingsPageBuilder(
            self,
            spin_factory=_spin,
            combo_factory=_WheelFocusedComboBox,
            font_combo_factory=_WheelFocusedFontComboBox,
        )
        self._role_color_page_builder = RoleColorPropertyPageBuilder(
            self,
            anchored_action_factory=_AnchoredTabActionButton,
            color_state_swap_icon=_COLOR_STATE_SWAP_ICON,
            fill_mode_icons_provider=_fill_mode_icons,
            spin_factory=_spin,
            combo_factory=_WheelFocusedComboBox,
        )
        self._role_fill_pages_builder = RoleFillPagesBuilder(
            self,
            gradient_editor_factory=GradientStopsEditor,
            color_button_factory=ColorButton,
            double_spin_factory=_double_spin,
            spin_factory=_spin,
        )
        self._preset_schemes: dict[str, StylePreset] = {}
        self._pages: list[QWidget] = []
        self._color_edit_style_snapshot: Optional[Style] = None
        self._screen_color_picker: Optional[ScreenColorPicker] = None
        self._n3_template_target_height = 1080
        self._n3_template_lyrics_dir: Optional[Path] = None

        self.setObjectName("PropertyPanel")
        self.setMinimumWidth(320)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        navigation_row = QWidget(self)
        navigation_row.setObjectName("PropertyNavigationRow")
        navigation_layout = QHBoxLayout(navigation_row)
        navigation_layout.setContentsMargins(8, 4, 8, 4)
        navigation_layout.setSpacing(0)
        self._navigation_row = navigation_row
        self._navigation_layout = navigation_layout
        self._navigation_action: Optional[QWidget] = None
        self._navigation_actions: list[QWidget] = []

        self._navigation = SegmentedWidget(navigation_row)
        self._navigation.setObjectName("PropertyNavigation")
        self._navigation.setAccessibleName("字幕属性分类")
        self._navigation.currentItemChanged.connect(self._on_navigation_changed)
        navigation_layout.addWidget(self._navigation, 0)
        navigation_layout.addStretch(1)
        root.addWidget(navigation_row, 0)

        self._stack = QStackedWidget(self)
        self._stack.setObjectName("PropertyPanelStack")
        self._stack.currentChanged.connect(self._on_page_changed)
        root.addWidget(self._stack, 1)
        themed(
            self,
            lambda: (
                f"""
                #PropertyPanel {{ background: {palette().panel_bg}; }}
                #PropertyPanelStack {{
                    border: 1px solid {palette().card_border};
                    border-radius: 6px;
                    background: {palette().panel_bg};
                }}
                """
            ),
        )

        pages = build_property_pages(
            self,
            scroll_page_factory=_scroll_page,
            section_pair_factory=_section_pair,
        )
        for page, (route_key, label) in zip(pages, self._PAGE_SPECS):
            self._add_navigation_page(page, route_key, label)
        self.setCurrentIndex(0)
        self.set_roles([])
        self.set_style(self._style, emit=False)

    def set_navigation_action(self, widget: Optional[QWidget]) -> None:
        """Place one host action at the far right of the property tab row."""
        self.set_navigation_actions([] if widget is None else [widget])

    def set_navigation_actions(self, widgets: list[QWidget]) -> None:
        """Place host actions at the far right of the property tab row.

        Actions are laid out from left to right in the supplied order.  The
        singular ``set_navigation_action`` API remains available for existing
        embedders.
        """
        if self._navigation_actions == widgets:
            return
        for previous in self._navigation_actions:
            self._navigation_layout.removeWidget(previous)
            if previous not in widgets:
                previous.setParent(None)
        self._navigation_actions = list(widgets)
        self._navigation_action = widgets[-1] if widgets else None
        if not widgets:
            return
        first_route = self._PAGE_SPECS[0][0]
        first_tab = self._navigation.widget(first_route)
        height = max(first_tab.sizeHint().height(), 1) if first_tab is not None else 1
        for widget in widgets:
            widget.setParent(self._navigation_row)
            widget.setFixedHeight(height)
            self._navigation_layout.addWidget(
                widget,
                0,
                Qt.AlignmentFlag.AlignVCenter,
            )

    def _add_navigation_page(
        self,
        page: QWidget,
        route_key: str,
        label: str,
    ) -> None:
        self._pages.append(page)
        self._stack.addWidget(page)
        self._navigation.addItem(
            route_key,
            label,
        )
        item = self._navigation.widget(route_key)
        item.setAccessibleName(label)
        page.setAccessibleName(label)

    def _on_navigation_changed(self, route_key: str) -> None:
        index = property_page_index(route_key)
        if index is not None:
            self._stack.setCurrentIndex(index)

    def _on_page_changed(self, index: int) -> None:
        if hasattr(self, "_font_preview_widget"):
            self._font_preview_widget.setVisible(
                index == 0 and self._font_preview_requested
            )
            if index == 0 and self._font_preview_requested:
                self._sync_font_preview()
        self.pageChanged.emit(index)

    def count(self) -> int:
        return len(self._pages)

    def widget(self, index: int) -> Optional[QWidget]:
        return self._stack.widget(index)

    def currentIndex(self) -> int:  # noqa: N802
        return self._stack.currentIndex()

    def setCurrentIndex(self, index: int) -> None:  # noqa: N802
        if not 0 <= index < self.count():
            return
        self._stack.setCurrentIndex(index)
        self._navigation.setCurrentItem(self._PAGE_SPECS[index][0])

    @property
    def subtitle_style(self) -> Style:
        return self._style

    @property
    def preset_schemes(self) -> dict[str, StylePreset]:
        return _normalize_style_presets(self._preset_schemes)

    @property
    def role_names(self) -> list[str]:
        """当前角色导航中可分配给歌词的角色名（不含全局默认与标题）。"""
        return self._role_controller.names

    def set_preset_schemes(
        self, schemes: dict[str, StylePreset | SubtitleStyleScheme]
    ) -> None:
        self._preset_schemes = _normalize_style_presets(schemes)

    def choose_role_presets_for_import(
        self, role_names: list[str]
    ) -> dict[str, SubtitleStyleScheme]:
        """Ask for one preset group per imported role whose name is ambiguous."""

        candidates: dict[str, list[StylePreset]] = {}
        resolved_by_id: dict[str, StylePreset] = {}
        seen: set[str] = set()
        for raw_name in role_names:
            name = str(raw_name).strip()
            if (
                not name
                or name in seen
                or name in self._style.custom_style_schemes
            ):
                continue
            seen.add(name)
            by_group: dict[str, StylePreset] = {}
            for preset_id, preset in self._preset_schemes.items():
                if preset.name != name or preset.group in by_group:
                    continue
                resolved, _warnings = resolve_n3_template_preset(
                    preset,
                    target_height=self._n3_template_target_height,
                    lyrics_dir=self._n3_template_lyrics_dir,
                )
                resolved.preset_id = preset_id
                by_group[preset.group] = resolved
                resolved_by_id[preset_id] = resolved
            if len(by_group) > 1:
                candidates[name] = list(by_group.values())

        if not candidates:
            return {}
        dialog = _RolePresetGroupDialog(candidates, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return {}
        selected: dict[str, SubtitleStyleScheme] = {}
        for role_name, preset_id in dialog.selected_preset_ids().items():
            preset = resolved_by_id.get(preset_id)
            if preset is not None:
                selected[role_name] = deepcopy(preset.scheme)
        return selected

    def set_n3_template_target_height(self, height: int) -> None:
        """Set the output height used when an N3 template preset is applied."""
        self._n3_template_target_height = max(1, int(height))

    def set_output_size(self, width: int, height: int) -> None:
        """Update output-dependent preset resolution and layout schematic."""
        width = int(width)
        height = int(height)
        if width <= 0 or height <= 0:
            return
        self.set_n3_template_target_height(height)
        if hasattr(self, "_layout_schematic"):
            self._layout_schematic.set_output_size(width, height)
        if hasattr(self, "_schematic_board"):
            self._schematic_board._sync(force=True)
            self._schematic_board.updateGeometry()

    def set_n3_template_lyrics_directory(self, path: Optional[Path]) -> None:
        """Set the project lyrics directory used for delayed bitmap lookup."""
        self._n3_template_lyrics_dir = Path(path) if path is not None else None

    def set_style(self, style: Style, *, emit: bool = False) -> None:
        self._title_text_change_timer.stop()
        self._title_text_change_pending = False
        if self._style_synced and not emit and style == self._style:
            # 宿主把面板自己发出的样式原样回流是常态（`styleChanged` →
            # `_apply_style` → `set_style`）。重新灌一遍全部控件要跑几百次
            # setValue/setCurrentIndex 和四个 _sync_* 分支，值没变时纯属白烧，
            # 还会把用户正在输入的那个框改写掉。
            self._style = replace(style)
            self._sync_font_preview()
            return
        self._style = replace(style)
        current_key = self._current_scheme_key()
        self._syncing = True
        try:
            self._refresh_scheme_combo(current_key)
            self._viewport_align_combo.setCurrentIndex(
                max(0, self._viewport_align_combo.findData(self._style.viewport_align))
            )
            self._viewport_x_spin.setValue(self._style.viewport_offset_x)
            self._viewport_y_spin.setValue(self._style.viewport_offset_y)
            self._viewport_scale_spin.setValue(self._style.viewport_scale_pct)
            self._viewport_rotation_spin.setValue(self._style.viewport_rotation_deg)
            self._rtl_check.setChecked(self._style.right_to_left)
            self._vertical_check.setChecked(self._style.vertical)
            self._allow_inter_page_line_overlap_check.setChecked(
                self._style.allow_inter_page_line_overlap
            )
            self._refresh_layout_combo()
            self._sync_layout_editor_controls()
            timing = self._style.timing
            self._line_lead_spin.setValue(timing.line_lead_in_ms)
            self._line_tail_spin.setValue(timing.line_tail_ms)
            self._line_offset_spin.setValue(timing.timing_offset_ms)
            self._ruby_main_reading_units_check.setChecked(
                self._style.ruby_main_progress_mode == "reading_units"
            )
            self._section_gap_spin.setValue(timing.section_gap_ms)
            self._lane_gap_spin.setValue(timing.line_lane_gap_ms)
            self._section_ending_combo.setCurrentIndex(
                max(0, self._section_ending_combo.findData(timing.section_ending_mode))
            )
            self._sync_entry_check.setChecked(timing.sync_entry)
            self._sync_ending_check.setChecked(timing.sync_ending)
            self._allow_animation_overlap_check.setChecked(
                timing.allow_entry_exit_animation_overlap
            )
            self._sync_each_page_check.setChecked(timing.sync_each_page)
            self._auto_fill_section_time_check.setChecked(
                timing.auto_fill_section_time
            )
            self._sync_sync_each_page_enabled()
            self._entry_anim_combo.setCurrentIndex(
                max(0, self._entry_anim_combo.findData(timing.entry_anim))
            )
            self._entry_lead_spin.setValue(timing.entry_lead_ms)
            self._exit_anim_combo.setCurrentIndex(
                max(0, self._exit_anim_combo.findData(timing.exit_anim))
            )
            self._exit_fade_spin.setValue(timing.exit_fade_ms)
            self._karaoke_anim_combo.setCurrentIndex(
                max(
                    0,
                    self._karaoke_anim_combo.findData(
                        effective_karaoke_animation(self._style)
                    ),
                )
            )
            self._sync_lit_controls()
            self._sync_subtitle_scheme_controls()
            self._sync_title_controls()
        finally:
            self._syncing = False
        self._style_synced = True
        self._sync_font_preview()
        if emit:
            self.styleChanged.emit(self._style)

    def set_roles(self, role_names: list[str]) -> None:
        """Replace the current project's role registry and refresh navigation."""
        previous = self._role_controller.names
        self._role_controller.replace(role_names)
        self._sync_role_registry(previous)

    def _sync_role_registry(self, previous: list[str]) -> None:
        """Refresh role-backed controls and publish one model change."""
        self._ensure_role_schemes()
        current_key = self._current_scheme_key()
        self._syncing = True
        try:
            self._refresh_scheme_combo(current_key)
        finally:
            self._syncing = False
        self._sync_subtitle_scheme_controls()
        self._sync_font_preview()
        if self._role_controller.names != previous:
            self.rolesChanged.emit(self._role_controller.names)

    def merge_roles(self, role_names: list[str]) -> None:
        """Add newly discovered roles without dropping earlier project roles."""
        previous = self._role_controller.names
        self._role_controller.merge(role_names)
        self._sync_role_registry(previous)

    # ------------------------------------------------------------------ layout

    def _make_font_color_section(self) -> QFrame:
        return self._role_page_builder.make_font_color_section()

    def _make_font_section(
        self, parent: Optional[QWidget] = None, *, inline: bool = False
    ) -> QWidget:
        return self._role_page_builder.make_font_section(parent, inline=inline)

    def _on_font_script_changed(self, script: str) -> None:
        self._sync_font_settings_page()
        self._sync_font_preview()

    def current_font_script(self) -> str:
        """Return the script selected in the role page's font editor."""
        if not hasattr(self, "_font_tab_panel"):
            return "japanese"
        return self._font_tab_panel.current_left() or "japanese"

    def _sync_font_preview(self) -> None:
        if not hasattr(self, "_font_preview_widget"):
            return
        if not self._font_preview_requested or self.currentIndex() != 0:
            return
        self._font_preview_widget.set_preview_state(
            self._style,
            self.current_scheme_key(),
            self.current_font_script(),
        )

    def _toggle_font_preview(self) -> None:
        self._font_preview_requested = not self._font_preview_requested
        self._font_preview_widget.setVisible(
            self.currentIndex() == 0 and self._font_preview_requested
        )
        if self._font_preview_requested:
            self._sync_font_preview()

    def _make_font_settings_page(
        self, subject: str, script: str, parent: QWidget
    ) -> QWidget:
        return self._role_font_page_builder.make_page(subject, script, parent)

    #: 英数字号跟随谁：勾选框字段 -> (写回的字段名, 被跟随的字段名)。
    _FONT_SIZE_FOLLOW_FIELDS = {
        ("main", "latin"): ("latin_font_size_px", "font_size_px"),
        ("ruby", "latin"): ("ruby_latin_font_size_px", "ruby_font_size_px"),
    }

    def _on_font_size_follow_toggled(self, slot: tuple[str, str], checked: bool) -> None:
        if self._syncing:
            return
        field, source_field = self._FONT_SIZE_FOLLOW_FIELDS[slot]
        # 取消跟随时把当前**实际生效**的字号写死，外观不跳变 —— 和注音配色那个
        # 跟随勾选框同一个套路。
        value = None if checked else int(self._scheme_value(source_field))
        changes: dict[str, object] = {field: value}
        if checked:
            changes.update(self._scheme_local_inheritance_changes(field))
        if slot[0] == "ruby":
            self._update_ruby_font_override(**changes)
        else:
            self._update_style(**changes)

    def _scheme_local_inheritance_changes(self, follow_field: str) -> dict[str, object]:
        """让角色方案里的空槽在**方案内**解析，而不是回头去继承全局。

        角色方案的 ``None`` 默认是"这一槽没设定，用全局的"，只有
        ``n3_font_inheritance`` 打开后才变成"在本方案里往上找"。用户勾
        「字号跟随主文字日文」要的正是后者：全局要是写死过英数字号，不打开
        这个标志的话，勾了也还是按全局那个数渲染。

        标志是整套子槽共用的，直接打开会连带把其他还空着的槽（英数字体、注音
        描边…）一起改成方案内解析，外观会跟着跳。所以打开之前先把那些槽当前
        **实际生效**的值固化进方案，只让用户刚勾的这一项空着。
        """
        if self._current_custom_scheme_name() is None:
            return {}  # 全局默认自己的 None 本来就是「跟随」，不需要标志
        changes: dict[str, object] = {"n3_font_inheritance": True}
        for name in N3_FONT_INHERITANCE_FIELDS:
            if name == follow_field or self._scheme_own_value(name) is not None:
                continue
            changes[name] = self._scheme_value(name)
        return changes

    def _font_size_follows(self, field: str) -> bool:
        """这一槽现在是不是**真的**跟随上一级（渲染也照这个走）。

        不能只看方案自己是不是 ``None``：角色方案的空槽默认继承全局，全局要是
        写死过英数字号，这个空槽渲染出来就是全局那个数、并没有跟着本角色的日
        文字号走 —— 勾选框这时候就不该显示成已勾选。只有 ``n3_font_inheritance``
        打开（空槽在方案内解析），或者全局自己也空着，才算真跟随。
        """
        if self._scheme_own_value(field) is not None:
            return False
        role_name = self._current_custom_scheme_name()
        if role_name is None:
            return True  # 全局默认自己的 None 就是「跟随」
        scheme = self._style.custom_style_schemes.get(role_name)
        if scheme is not None and scheme.n3_font_inheritance:
            return True
        return getattr(self._style, field, None) is None

    def _sync_font_size_follow_controls(self) -> None:
        """勾选框与字号输入框跟着方案走：``None`` = 跟随，输入框置灰。"""
        for slot, check in self._font_size_follow_checks.items():
            field, source_field = self._FONT_SIZE_FOLLOW_FIELDS[slot]
            follows = self._font_size_follows(field)
            check.blockSignals(True)
            try:
                check.setChecked(follows)
            finally:
                check.blockSignals(False)
            spin = (
                self._font_latin_size_spin
                if slot == ("main", "latin")
                else self._ruby_font_latin_size_spin
            )
            # 跟随时输入框置灰、留在 0：整个面板里 0 都表示"跟随上一级"
            # （字体下拉写着"（0）"、字重与描边宽度同理），这里不另立一套。
            spin.setEnabled(not follows)

    def _sync_font_settings_page(self) -> None:
        subject_index = 0 if self._font_tab_panel.current_right() == "main" else 1
        script_index = 0 if self._font_tab_panel.current_left() == "japanese" else 1
        self._font_tab_stack.setCurrentIndex(subject_index * 2 + script_index)

    def _on_font_stroke2_toggled(
        self, field_name: str, width_spin: FluentSpinBox, checked: bool
    ) -> None:
        width_spin.setEnabled(checked)
        self._update_style(**{field_name: checked})

    def _on_font_stroke2_state_changed(
        self, field_name: str, width_spin: FluentSpinBox, state: int
    ) -> None:
        check_state = Qt.CheckState(state)
        inherited = check_state == Qt.CheckState.PartiallyChecked
        checked = check_state == Qt.CheckState.Checked
        width_spin.setEnabled(inherited or checked)
        self._update_style(**{field_name: None if inherited else checked})

    def _update_ruby_font_override(self, **changes) -> None:
        changes["ruby_font_follow_main"] = False
        self._update_style(**changes)

    def _effective_font_family(self, slot: tuple[str, str]) -> str:
        font_combo, _weight_combo, _inheritance_label = self._font_controls[slot]
        if not font_combo.is_inherited():
            return font_combo.currentFont().family()
        parent_slot = {
            ("main", "latin"): ("main", "japanese"),
            ("ruby", "japanese"): ("main", "japanese"),
            ("ruby", "latin"): ("ruby", "japanese"),
        }.get(slot)
        if parent_slot is None:
            return font_combo.currentFont().family()
        return self._effective_font_family(parent_slot)

    def _refresh_font_weight_combo(
        self,
        slot: tuple[str, str],
        *,
        preferred_weight: Optional[int] = None,
    ) -> int:
        _font_combo, weight_combo, inheritance_label = self._font_controls[slot]
        if preferred_weight is None:
            current_data = weight_combo.currentData()
            preferred_weight = int(current_data) if current_data is not None else 400
        family = self._effective_font_family(slot)
        physical_weights = _available_font_weights(family)
        synthetic_bold = _supports_synthetic_bold(family, physical_weights)
        weights = tuple(
            sorted(set(physical_weights) | ({700} if synthetic_bold else set()))
        )
        if preferred_weight == 0 and inheritance_label is not None:
            selected_weight = 0
        else:
            selected_weight = min(
                weights,
                key=lambda candidate: (abs(candidate - int(preferred_weight)), candidate),
            )

        was_blocked = weight_combo.blockSignals(True)
        try:
            weight_combo.clear()
            if inheritance_label is not None:
                weight_combo.addItem(inheritance_label, 0)
            for weight in weights:
                label = _font_weight_label(weight)
                if synthetic_bold and weight == 700:
                    label += "（合成）"
                weight_combo.addItem(label, weight)
            weight_combo.setCurrentIndex(
                max(0, weight_combo.findData(selected_weight))
            )
        finally:
            weight_combo.blockSignals(was_blocked)
        return selected_weight

    def _refresh_font_weight_combos(
        self,
        preferred_weights: Optional[dict[tuple[str, str], int]] = None,
    ) -> dict[tuple[str, str], int]:
        preferred_weights = preferred_weights or {}
        resolved: dict[tuple[str, str], int] = {}
        for slot in (
            ("main", "japanese"),
            ("main", "latin"),
            ("ruby", "japanese"),
            ("ruby", "latin"),
        ):
            resolved[slot] = self._refresh_font_weight_combo(
                slot,
                preferred_weight=preferred_weights.get(slot),
            )
        return resolved

    @staticmethod
    def _font_weight_change(slot: tuple[str, str], weight: int) -> tuple[str, object]:
        field_name = {
            ("main", "japanese"): "font_weight",
            ("main", "latin"): "latin_font_weight",
            ("ruby", "japanese"): "ruby_font_weight",
            ("ruby", "latin"): "ruby_latin_font_weight",
        }[slot]
        value: object = int(weight)
        if slot != ("main", "japanese") and weight == 0:
            value = None
        return field_name, value

    def _on_font_family_changed(
        self, slot: tuple[str, str], font: QFont
    ) -> None:
        previous_weights = {
            current_slot: int(controls[1].currentData())
            for current_slot, controls in self._font_controls.items()
            if controls[1].currentData() is not None
        }
        resolved_weights = self._refresh_font_weight_combos(previous_weights)
        if self._syncing:
            return

        font_combo, _weight_combo, _inheritance_label = self._font_controls[slot]
        family_field = {
            ("main", "japanese"): "font_family",
            ("main", "latin"): "font_family_latin",
            ("ruby", "japanese"): "ruby_font_family",
            ("ruby", "latin"): "ruby_font_family_latin",
        }[slot]
        changes: dict[str, object] = {
            family_field: None if font_combo.is_inherited() else font.family()
        }
        for current_slot, resolved_weight in resolved_weights.items():
            if (
                current_slot == slot
                or resolved_weight != previous_weights.get(current_slot)
            ):
                field_name, value = self._font_weight_change(
                    current_slot, resolved_weight
                )
                changes[field_name] = value
        if slot[0] == "ruby":
            changes["ruby_font_follow_main"] = False
        self._update_style(**changes)

    def _make_character_layout_group(self, parent: QWidget) -> QWidget:
        """左侧紧凑字符排版组，与智能水平共享幕布侧边区域。"""
        group = QWidget(parent)
        self._character_layout_section = group
        layout = QVBoxLayout(group)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        layout.addWidget(_subgroup_label("字符排版"))

        fields = QWidget(group)
        fields_layout = QHBoxLayout(fields)
        fields_layout.setContentsMargins(0, 0, 0, 0)
        fields_layout.setSpacing(8)

        self._letter_spacing_spin = _spin(
            -_LAYOUT_SIZE_MAX_PX, _LAYOUT_SIZE_MAX_PX, suffix=" px"
        )
        self._letter_spacing_spin.setFixedWidth(120)
        self._letter_spacing_spin.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed
        )
        self._letter_spacing_spin.valueChanged.connect(
            lambda value: self._update_layout_field(letter_spacing_px=value)
        )
        fields_layout.addWidget(_field("字间距", self._letter_spacing_spin))

        self._space_width_spin = _spin(10, 100, suffix=" %")
        self._space_width_spin.setFixedWidth(120)
        self._space_width_spin.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed
        )
        self._space_width_spin.valueChanged.connect(
            lambda value: self._update_style(space_width_percent=value)
        )
        fields_layout.addWidget(_field("空格宽度", self._space_width_spin))

        layout.addWidget(fields)
        return group

    def _make_ruby_section(
        self, parent: Optional[QWidget] = None, *, inline: bool = False
    ) -> QWidget:
        return self._layout_page_builder.make_ruby_section(parent, inline=inline)

    def _apply_main_colors_to_ruby(self) -> None:
        if self._syncing:
            return
        # 只复制颜色和填充。字体卡片里的描边尺寸、以及方案共用的装饰参数
        # 都不属于这个按钮的职责。
        self._update_style(
            ruby_colors_follow_main=False,
            ruby_karaoke_colors=deepcopy(self._current_karaoke_colors()),
        )

    def _on_ruby_colors_follow_main_toggled(self, checked: bool) -> None:
        if self._syncing:
            return
        # None 是模型层的完整继承语义：文字、描边、描边2、装饰及其填充
        # 参数都会实时读取主文字矩阵。关闭时复制当前值，保证外观不跳变。
        self._update_style(
            ruby_colors_follow_main=checked,
            ruby_karaoke_colors=(
                None if checked else deepcopy(self._current_karaoke_colors())
            )
        )

    def _sync_ruby_color_follow_controls(self) -> None:
        if not hasattr(self, "_ruby_colors_follow_main_check"):
            return
        follows_main = bool(self._scheme_value("ruby_colors_follow_main"))
        self._ruby_colors_follow_main_check.blockSignals(True)
        try:
            self._ruby_colors_follow_main_check.setChecked(follows_main)
        finally:
            self._ruby_colors_follow_main_check.blockSignals(False)
        # 跟随时已经是实时同步；一次性复制按钮只在独立配色时有意义。
        self._ruby_apply_main_btn.setEnabled(not follows_main)

    def _set_ruby_color_controls_visible(self, visible: bool) -> None:
        if not hasattr(self, "_ruby_color_actions_row"):
            return
        self._ruby_color_actions_row.setVisible(visible)
        self._ruby_colors_follow_main_check.setVisible(visible)
        self._ruby_apply_main_btn.setVisible(visible)

    def _make_color_section(
        self, parent: Optional[QWidget] = None, *, inline: bool = False
    ) -> QWidget:
        return self._role_color_page_builder.make_section(parent, inline=inline)

    def _update_shared_decoration(self, **changes) -> None:
        """装饰是配色方案级参数；编辑时清除旧工程遗留的 ruby 独立覆盖。"""
        ruby_fields = {
            "decoration_kind": "ruby_decoration_kind",
            "glow_radius_px": "ruby_glow_radius_px",
            "glow_before_radius_px": "ruby_glow_before_radius_px",
            "glow_after_radius_px": "ruby_glow_after_radius_px",
            "glow_concentration_level": "ruby_glow_concentration_level",
            "shadow_offset_x": "ruby_shadow_offset_x",
            "shadow_offset_y": "ruby_shadow_offset_y",
        }
        shared = dict(changes)
        shared.update(
            {ruby_fields[field]: None for field in changes if field in ruby_fields}
        )
        self._update_style(**shared)

    def _make_solid_fill_page(self) -> QWidget:
        return self._role_fill_pages_builder.make_solid_page()

    def _make_gradient_fill_page(self) -> QWidget:
        return self._role_fill_pages_builder.make_gradient_page()

    def _make_split_fill_page(self) -> QWidget:
        return self._role_fill_pages_builder.make_split_page()
    @staticmethod
    def _arrange_stop_editor(
        layout: QGridLayout,
        bar_field: QWidget,
        color_field: QWidget,
        position_field: QWidget,
        *,
        vertical: bool,
        footer: QWidget | None = None,
    ) -> None:
        """Place vertical bars left with two stacked editors on the right."""
        while layout.count():
            layout.takeAt(0)
        if vertical:
            bar_field.setSizePolicy(
                QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred
            )
            layout.addWidget(bar_field, 0, 0, 2, 1, Qt.AlignmentFlag.AlignTop)
            # Keep label → control spacing identical to ordinary fields such as
            # “描边宽度”. Without AlignTop, the tall color bar stretches both grid
            # rows and Qt expands the field label into the surplus height.
            layout.addWidget(color_field, 0, 1, Qt.AlignmentFlag.AlignTop)
            layout.addWidget(position_field, 1, 1, Qt.AlignmentFlag.AlignTop)
            layout.setColumnStretch(0, 0)
            layout.setColumnStretch(1, 1)
        else:
            bar_field.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
            )
            layout.addWidget(bar_field, 0, 0, 1, 2)
            layout.addWidget(color_field, 1, 0)
            layout.addWidget(position_field, 1, 1)
            layout.setColumnStretch(0, 1)
            layout.setColumnStretch(1, 1)
        if footer is not None:
            layout.addWidget(footer, 2, 0, 1, 2)

    def _make_image_fill_page(self) -> QWidget:
        return self._role_fill_pages_builder.make_image_page()

    def _paint_color_button(self, field_name: str, color: str) -> ColorButton:
        button = ColorButton(color)
        self._wire_color_edit_session(button)
        button.clicked.connect(
            lambda _checked=False, field=field_name: self._choose_paint_color(field)
        )
        button.colorEntered.connect(
            lambda color, field=field_name: self._update_current_fill(
                **{field: color}
            )
        )
        button.screenPickRequested.connect(
            lambda field=field_name: self._choose_paint_color(
                field, screen_pick=True, preview_button=button
            )
        )
        return button

    def _make_scheme_navigation(self, parent: QWidget) -> QFrame:
        """颜色/字体卡片的角色导航条。

        角色决定下方正在编辑的样式方案，因此它是卡片导航，而不是一张
        独立属性卡。导航条用轻微凸起的内层背景和边框与编辑区区分。
        """
        nav = QFrame(parent)
        self._role_navigation = nav
        nav.setObjectName("SubtitleRoleNavigation")
        nav.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed
        )
        row_layout = QHBoxLayout(nav)
        self._role_navigation_layout = row_layout
        self._role_navigation_action: Optional[QWidget] = None
        row_layout.setContentsMargins(6, 6, 6, 6)
        row_layout.setSpacing(4)

        # 单行排布：角色下拉吸收剩余宽度，操作收成紧凑图标按钮。
        # 内部仍叫 _singer_combo（少改动），但现在装的是「角色」：全局默认 + 各角色名。
        self._singer_combo = _WheelFocusedComboBox(nav)
        _compact_control(self._singer_combo)
        self._singer_combo.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed
        )
        self._singer_combo.currentIndexChanged.connect(self._on_scheme_combo_changed)
        row_layout.addWidget(self._singer_combo, 1)

        self._add_scheme_button = FluentTransparentToolButton(FIF.ADD, nav)
        self._add_scheme_button.setToolTip("新建角色")
        self._add_scheme_button.clicked.connect(lambda _checked=False: self._add_custom_scheme())
        self._rename_role_button = FluentTransparentToolButton(FIF.EDIT, nav)
        self._rename_role_button.setToolTip("重命名当前角色")
        self._rename_role_button.clicked.connect(lambda _checked=False: self._rename_current_role())
        self._delete_role_button = FluentTransparentToolButton(FIF.DELETE, nav)
        self._delete_role_button.setToolTip("删除当前角色")
        self._delete_role_button.clicked.connect(lambda _checked=False: self._delete_current_role())
        self._manage_presets_button = FluentTransparentToolButton(FIF.PALETTE, nav)
        self._manage_presets_button.setToolTip("管理样式预设库")
        self._manage_presets_button.clicked.connect(lambda _checked=False: self._open_preset_manager())
        self._save_scheme_button = FluentTransparentToolButton(FIF.SAVE, nav)
        self._save_scheme_button.setToolTip("保存当前方案")
        self._save_scheme_button.clicked.connect(
            lambda _checked=False: self._save_current_scheme()
        )
        for btn in (
            self._add_scheme_button,
            self._rename_role_button,
            self._delete_role_button,
            self._manage_presets_button,
            self._save_scheme_button,
        ):
            btn.setFixedSize(30, 30)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            # 图标按钮无文字，可访问名沿用中文提示
            btn.setAccessibleName(btn.toolTip())
            row_layout.addWidget(btn, 0)

        self._font_preview_button = FluentPushButton("显示/隐藏预览", nav)
        self._font_preview_button.setFixedSize(
            self._font_preview_button.sizeHint().width(), 30
        )
        self._font_preview_button.setToolTip("显示/隐藏预览")
        self._font_preview_button.clicked.connect(self._toggle_font_preview)
        row_layout.addWidget(self._font_preview_button, 0)

        themed(
            nav,
            lambda: (
                "QFrame#SubtitleRoleNavigation { "
                f"background: {palette().secondary_button_bg}; "
                f"border: 1px solid {palette().card_border}; "
                "border-radius: 7px; "
                "}"
            ),
        )

        return nav

    # ----------------------------------------------------------------- 标题（B7）

    # ------------------------------------------------------------------ background / audio page

    def _make_background_source_section(self) -> QFrame:
        return self._background_page_builder.make_source_section()

    def _make_background_detail_page(
        self, kind: str, label: str, placeholder: str, section: QWidget
    ) -> QWidget:
        return self._background_page_builder.make_detail_page(
            kind,
            label,
            placeholder,
            section,
        )

    def _choose_solid_color_dialog(self) -> None:
        color = _select_color(
            QColor(self._solid_color_btn.color), self, "选择纯色背景"
        )
        if color.isValid():
            self._apply_solid_color(color)

    def _apply_solid_color(self, color: object) -> None:
        text = color if isinstance(color, str) else None
        if text is None:
            qcolor = color if isinstance(color, QColor) else QColor()
            if not qcolor.isValid():
                return
            text = qcolor.name()
        self.backgroundSolidColorChanged.emit(text)

    def _on_background_kind_pill_changed(self, kind: str) -> None:
        """胶囊只切换查看的类型页，不改动当前背景数据。"""
        index = {
            item[0]: position for position, item in enumerate(_BACKGROUND_KIND_PAGES)
        }.get(kind, 0)
        self._background_detail_stack.setCurrentIndex(index)
        self._image_fit_group.setVisible(kind in {"image", "image_sequence"})

    def _make_screen_size_section(self) -> QFrame:
        return self._background_page_builder.make_screen_size_section()

    def _on_panel_screen_size_changed(self, *_args) -> None:
        if self._syncing:
            return
        self.screenSizeChanged.emit()

    def screen_size(self) -> tuple[int, int, int]:
        data = self._screen_size_fps_combo.currentData()
        fps = int(data) if data in SCREEN_FPS_OPTIONS else 60
        return (
            self._screen_size_width_spin.value(),
            self._screen_size_height_spin.value(),
            fps,
        )

    def set_screen_size(self, width: int, height: int, fps: int) -> None:
        self._syncing = True
        try:
            self._screen_size_width_spin.setValue(max(int(width), 1))
            self._screen_size_height_spin.setValue(max(int(height), 1))
            index = self._screen_size_fps_combo.findData(int(fps))
            self._screen_size_fps_combo.setCurrentIndex(
                index if index >= 0 else 0
            )
        finally:
            self._syncing = False

    def set_background_state(self, source: BackgroundSource) -> None:
        """宿主在背景源变化后回填：类型胶囊、详情页、图片策略与音频可用态。"""
        self._background_state_kind = source.kind
        self._background_kind_pill.set_current(source.kind)
        index = {
            item[0]: position for position, item in enumerate(_BACKGROUND_KIND_PAGES)
        }.get(source.kind, 0)
        self._background_detail_stack.setCurrentIndex(index)
        self._image_fit_group.setVisible(
            source.kind in {"image", "image_sequence"}
        )
        if source.path:
            path_edit = self._background_path_edits.get(source.kind)
            if path_edit is not None:
                path_edit.setText(source.path)
        else:
            for path_edit in self._background_path_edits.values():
                path_edit.clear()
        if source.kind == "solid":
            color = QColor(source.color)
            if color.isValid():
                current = QColor(self._solid_color_btn.color)
                if current.name() != color.name():
                    self._solid_color_btn.set_color(color.name())

        image_like = source.kind in {"image", "image_sequence"}
        self._image_fit_cover_radio.setEnabled(image_like)
        self._image_fit_contain_radio.setEnabled(image_like)
        self._syncing = True
        try:
            if source.image_fit == "contain":
                self._image_fit_contain_radio.setChecked(True)
            else:
                self._image_fit_cover_radio.setChecked(True)
        finally:
            self._syncing = False

    def set_audio_state(self, path: Optional[Path]) -> None:
        text = str(path) if path is not None else ""
        for audio_edit in self._audio_path_edits:
            audio_edit.setText(text)

    def _make_title_text_section(self) -> QFrame:
        return self._title_page_builder.make_text_section()

    def _make_title_style_section(self) -> QFrame:
        return self._title_page_builder.make_style_section()

    def _on_title_layout_changed(self, _index: int) -> None:
        if self._syncing:
            return
        data = self._title_layout_combo.currentData()
        self._update_title(layout_index=int(data) if data is not None else 0)

    def _open_title_scheme(self) -> None:
        """跳到字体页并选中「标题」方案。"""
        self.setCurrentIndex(0)
        self.set_current_scheme_key(f"{_CUSTOM_SCHEME_PREFIX}{TITLE_SCHEME_NAME}")

    def _refresh_title_layout_combo(self) -> None:
        if not hasattr(self, "_title_layout_combo"):
            return
        combo = self._title_layout_combo
        blocked = combo.blockSignals(True)
        try:
            combo.clear()
            combo.addItem(layout_display_name(self._style, "default"), 0)
            for index, layout_def in enumerate(self._style.layouts, start=1):
                combo.addItem(layout_def.name, index)
            title = self._current_title()
            target = title.layout_index if title.layout_index is not None else 0
            combo.setCurrentIndex(max(0, combo.findData(int(target))))
        finally:
            combo.blockSignals(blocked)

    def _make_title_time_section(self) -> QFrame:
        return self._title_page_builder.make_time_section()

    def _current_title(self) -> TitleOverlay:
        return self._title_controller.current(self._style)

    def _on_title_enabled_toggled(self, checked: bool) -> None:
        self._update_title(enabled=checked)

    def _on_title_text_changed(self) -> None:
        if self._syncing:
            return
        new_text = self._title_text_edit.toPlainText()
        self._style = self._title_controller.update(
            self._style,
            {"text_template": new_text},
        )
        # Keep typing responsive by coalescing the expensive host-side preview,
        # source-table, undo snapshot and settings updates.
        self._title_text_change_pending = True
        self._title_text_change_timer.start()

    def _commit_title_text_edit(self) -> None:
        if self._syncing or not self._title_text_change_pending:
            return
        self._title_text_change_timer.stop()
        self._title_text_change_pending = False
        self.styleChanged.emit(self._style)

    def _update_title(self, **changes) -> None:
        if self._syncing:
            return
        self._title_text_change_timer.stop()
        self._title_text_change_pending = False
        self._style = self._title_controller.update(self._style, changes)
        self._syncing = True
        try:
            self._sync_title_controls()
        finally:
            self._syncing = False
        self.styleChanged.emit(self._style)

    def _sync_title_controls(self) -> None:
        if not hasattr(self, "_title_enabled_switch"):
            return
        title = self._current_title()
        self._title_enabled_switch.setChecked(title.enabled)
        # 仅在内容不同才回填，避免实时输入时把光标弹到末尾。
        if self._title_text_edit.toPlainText() != title.text_template:
            self._title_text_edit.setPlainText(title.text_template)
        self._refresh_title_layout_combo()
        self._title_mode_combo.setCurrentIndex(
            max(0, self._title_mode_combo.findData(title.show_mode))
        )
        self._title_head_edit.setValue(title.head_offset_ms)
        self._title_duration_edit.setValue(title.duration_ms)
        self._title_tail_edit.setValue(title.tail_offset_ms)
        self._title_fade_in_edit.setValue(title.fade_in_ms)
        self._title_fade_out_edit.setValue(title.fade_out_ms)
        self._title_tail_duration_edit.setValue(
            title.duration_ms
            if title.tail_duration_ms is None
            else title.tail_duration_ms
        )
        self._title_tail_fade_in_edit.setValue(
            title.fade_in_ms
            if title.tail_fade_in_ms is None
            else title.tail_fade_in_ms
        )
        self._title_tail_fade_out_edit.setValue(
            title.fade_out_ms
            if title.tail_fade_out_ms is None
            else title.tail_fade_out_ms
        )
        self._sync_title_time_visibility()

    def _sync_title_time_visibility(self) -> None:
        mode = self._current_title().show_mode
        self._title_head_row.setVisible(mode in {"whole", "head", "head_tail"})
        self._title_tail_row.setVisible(mode in {"tail", "head_tail"})
        self._title_head_row_label.setText("全程" if mode == "whole" else "开头")

    def _make_lit_section(self) -> QFrame:
        return self._effects_page_builder.make_lit_section()

    def _sync_lit_style_visibility(self) -> None:
        """按当前指示灯样式整组显隐：音量柱组只在音量柱样式下显示，形状灯组反之。"""
        if not hasattr(self, "_lit_volume_groups"):
            return
        is_volume = self._style.lit_style == "volume"
        for box in self._lit_volume_groups:
            box.setVisible(is_volume)
        for box in self._lit_shape_groups:
            box.setVisible(not is_volume)

    def _make_animation_section(self) -> QFrame:
        return self._effects_page_builder.make_animation_section()

    def _make_viewport_section(self) -> QFrame:
        return self._layout_page_builder.make_viewport_section()

    def _make_layout_navigation(self, parent: QWidget) -> QFrame:
        """行结构卡片顶部的布局方案导航条。"""
        nav = QFrame(parent)
        self._layout_navigation = nav
        nav.setObjectName("SubtitleLayoutNavigation")
        layout = QVBoxLayout(nav)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)

        self._layout_combo = _WheelFocusedComboBox(nav)
        _compact_control(self._layout_combo)
        self._layout_combo.setToolTip(
            "布局方案（N3 レイアウト設定）：下方「行位置 / 行距 / 余白 / 行布局」"
            "编辑的是当前选中的布局；歌词行可分别引用不同布局（歌词列表右键应用）。"
        )
        self._layout_combo.currentIndexChanged.connect(
            lambda _index: self._on_layout_combo_changed()
        )

        # 与角色卡片同款：下拉吸收剩余宽度，管理操作收成单行紧凑图标按钮；
        # 不加「当前布局」字段标签——卡片标题「布局方案」已经说明语义。
        combo_row = QWidget(nav)
        combo_row_layout = QHBoxLayout(combo_row)
        combo_row_layout.setContentsMargins(0, 0, 0, 0)
        combo_row_layout.setSpacing(3)
        combo_row_layout.addWidget(self._layout_combo, 1)

        self._add_layout_btn = FluentTransparentToolButton(FIF.ADD, nav)
        self._add_layout_btn.setToolTip("新建布局（以当前布局的值复制）")
        self._add_layout_btn.clicked.connect(lambda _checked=False: self._on_add_layout())
        self._rename_layout_btn = FluentTransparentToolButton(FIF.EDIT, nav)
        self._rename_layout_btn.setToolTip("重命名当前布局")
        self._rename_layout_btn.clicked.connect(
            lambda _checked=False: self._on_rename_layout()
        )
        self._delete_layout_btn = FluentTransparentToolButton(FIF.DELETE, nav)
        self._delete_layout_btn.setToolTip("删除当前布局")
        self._delete_layout_btn.clicked.connect(
            lambda _checked=False: self._on_delete_layout()
        )
        self._save_layout_btn = FluentTransparentToolButton(FIF.SAVE, nav)
        self._save_layout_btn.setToolTip(
            "将当前布局参数保存为软件级新建项目默认值；不会应用到当前页面，"
            "也不会改变各行数的自动布局映射。"
        )
        self._save_layout_btn.clicked.connect(
            lambda _checked=False: self._save_current_layout_default()
        )
        for btn in (
            self._add_layout_btn,
            self._rename_layout_btn,
            self._delete_layout_btn,
            self._save_layout_btn,
        ):
            btn.setFixedSize(24, 24)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            # 图标按钮无文字，可访问名沿用中文提示
            btn.setAccessibleName(btn.toolTip())
            combo_row_layout.addWidget(btn, 0)
        layout.addWidget(combo_row)

        themed(
            nav,
            lambda: (
                "QFrame#SubtitleLayoutNavigation { "
                f"background: {palette().secondary_button_bg}; "
                f"border: 1px solid {palette().card_border}; "
                "border-radius: 7px; "
                "}"
            ),
        )
        return nav

    def _make_smart_horizontal_field(self, parent: QWidget) -> QWidget:
        """布局方案导航下方的智能水平配置。"""
        self._smart_horizontal_combo = _WheelFocusedComboBox(parent)
        _compact_control(self._smart_horizontal_combo)
        self._smart_horizontal_combo.setFixedWidth(180)
        for label, value in [
            ("左右余白对齐", "equal_margins"),
            ("中心位置对齐", "center_position"),
            ("不调整", "none"),
        ]:
            self._smart_horizontal_combo.addItem(label, value)
        self._smart_horizontal_combo.setToolTip(
            "智能水平配置：短行自动向画面中央收拢。左右余白对齐 = "
            "按页整体判断（N3 默认）；中心位置对齐 = 逐行判断；"
            "不调整 = 行永远贴左右边距，同时关闭单行页居中。"
        )
        self._smart_horizontal_combo.currentIndexChanged.connect(
            lambda _index: self._update_layout_field(
                smart_horizontal=self._smart_horizontal_combo.currentData()
            )
        )
        return _field("智能水平", self._smart_horizontal_combo)

    def _make_layout_assignment_actions(self, parent: QWidget) -> QWidget:
        """行结构工作区右上角的批量布局操作。"""
        assign_btn_row = QWidget(parent)
        self._layout_assignment_actions = assign_btn_row
        assign_btn_layout = QHBoxLayout(assign_btn_row)
        assign_btn_layout.setContentsMargins(0, 0, 0, 0)
        assign_btn_layout.setSpacing(6)
        self._assign_all_btn = FluentPushButton("应用到全部页", assign_btn_row)
        self._assign_all_btn.setToolTip(
            "让当前字幕源的所有页面统一使用当前布局；若该布局无法容纳某一页，"
            "则不会修改。"
        )
        self._assign_all_btn.clicked.connect(
            lambda _checked=False: self.layoutAssignAllRequested.emit(
                self._current_layout_index()
            )
        )
        self._auto_assign_btn = FluentPushButton(
            "各页按行数自动布局", assign_btn_row
        )
        self._auto_assign_btn.setToolTip(
            "不改变段落和分页；每一页按实际行数使用项目中对应的 1～8 行布局映射。"
        )
        self._auto_assign_btn.clicked.connect(
            lambda _checked=False: self.layoutAutoAssignRequested.emit()
        )
        for btn in (self._assign_all_btn, self._auto_assign_btn):
            btn.setMinimumHeight(30)
            assign_btn_layout.addWidget(btn, 1)
        return assign_btn_row

    def _make_row_structure_section(self) -> QFrame:
        return self._layout_page_builder.make_row_structure_section()

    def _make_vertical_layout_section(self) -> QFrame:
        return self._layout_page_builder.make_vertical_section()

    def _on_line_position_changed(self, _value: str = "") -> None:
        self._update_layout_field(
            line_y_position=self._line_position_seg.value()
        )
        self._sync_vertical_margin_enabled()

    def _sync_vertical_margin_enabled(self) -> None:
        """余白标签跟随锚定方向；居中时不参与排版并隐藏。"""
        if not hasattr(self, "_line_margin_spin"):
            return
        position = self._current_layout_values().get("line_y_position")
        if hasattr(self, "_vertical_margin_label"):
            self._vertical_margin_label.setText("上余白" if position == "top" else "下余白")
        if hasattr(self, "_vertical_margin_field"):
            self._vertical_margin_field.setVisible(position != "center")

    def _on_horizontal_margin_changed(self, value: int) -> None:
        if self._current_layout_index() > 0:
            self._update_layout_field(horizontal_margin_px=value)
            return
        # 旧字段跟随镜像：native 后端（C++）仍读取上/下行边距两个键。
        self._update_style(
            horizontal_margin_px=value,
            upper_line_left_margin_px=value,
            lower_line_right_margin_px=value,
        )

    # ------------------------------------------------------------- 布局方案

    def _current_layout_index(self) -> int:
        if not hasattr(self, "_layout_combo"):
            return 0
        try:
            index = int(self._layout_combo.currentData())
        except (TypeError, ValueError):
            return 0
        return index if 0 <= index <= len(self._style.layouts) else 0

    def _current_layout_source(self):
        return self._layout_controller.source(
            self._style,
            self._current_layout_index(),
        )

    def _current_layout_values(self) -> dict:
        return self._layout_controller.resolved_values(
            self._style,
            self._current_layout_index(),
        )

    def _update_layout_field(self, **changes) -> None:
        if self._syncing:
            return
        index = self._current_layout_index()
        if index <= 0:
            self._update_style(_force_global=True, **changes)
            return
        self._update_style(
            **self._layout_controller.field_changes(self._style, index, changes)
        )

    def _refresh_layout_combo(self, selected: Optional[int] = None) -> None:
        if selected is None:
            selected = self._current_layout_index()
        selected = min(max(selected, 0), len(self._style.layouts))
        combo = self._layout_combo
        blocked = combo.blockSignals(True)
        try:
            combo.clear()
            combo.addItem(layout_display_name(self._style, "default"), 0)
            for index, layout_def in enumerate(self._style.layouts, start=1):
                combo.addItem(layout_def.name, index)
            combo.setCurrentIndex(max(0, combo.findData(selected)))
        finally:
            combo.blockSignals(blocked)
        editable = self._current_layout_index() > 0
        self._rename_layout_btn.setEnabled(editable)
        self._delete_layout_btn.setEnabled(editable)
        self._sync_layout_combo_width()
        # 标题页的布局引用下拉与本列表同源，布局增删改名后一起刷新。
        self._refresh_title_layout_combo()

    def _sync_layout_combo_width(self) -> None:
        """布局方案下拉框按最长方案名自然展开，不铺满行结构卡片。"""
        metrics = self._layout_combo.fontMetrics()
        text_width = max(
            (
                metrics.horizontalAdvance(str(self._layout_combo.itemText(index)))
                for index in range(self._layout_combo.count())
            ),
            default=0,
        )
        # Four management buttons share this row.  A 110px floor keeps the
        # complete navigation inside the supported 320px panel width.
        self._layout_combo.setFixedWidth(max(110, min(280, text_width + 32)))

    def _sync_layout_editor_controls(self) -> None:
        values = self._current_layout_values()
        was_syncing = self._syncing
        self._syncing = True
        try:
            self._line_position_seg.setValue(values["line_y_position"])
            self._line_margin_spin.setValue(int(values["line_y_margin_px"]))
            self._line_gap_spin.setValue(int(values["line_gap_px"]))
            self._smart_horizontal_combo.setCurrentIndex(
                max(
                    0,
                    self._smart_horizontal_combo.findData(values["smart_horizontal"]),
                )
            )
            self._horizontal_margin_spin.setValue(int(values["horizontal_margin_px"]))
            self._letter_spacing_spin.setValue(int(values["letter_spacing_px"]))
            self._allow_biting_check.setChecked(bool(values["allow_biting"]))
            self._ruby_interval_spin.setValue(int(values["ruby_interval_px"]))
            self._ruby_alignment_combo.setCurrentIndex(
                max(0, self._ruby_alignment_combo.findData(values["ruby_alignment"]))
            )
            self._ruby_gap_spin.setValue(int(values["ruby_gap_px"]))
            self._rebuild_line_alignment_rows()
            self._sync_vertical_margin_enabled()
        finally:
            self._syncing = was_syncing
        self._refresh_layout_schematic()

    def _on_layout_combo_changed(self) -> None:
        if self._syncing:
            return
        self._refresh_layout_combo()
        self._sync_layout_editor_controls()

    def _on_add_layout(self) -> None:
        changes, selected = self._layout_controller.add_changes(
            self._style,
            self._current_layout_index(),
        )
        self._update_style(**changes)
        self._refresh_layout_combo(selected=selected)
        self._sync_layout_editor_controls()

    def _on_rename_layout(self) -> None:
        index = self._current_layout_index()
        if index <= 0:
            return
        old = self._style.layouts[index - 1].name
        new, ok = fluent_get_text(self, "重命名布局", "布局名称", text=old)
        if not ok:
            return
        new = new.strip()
        if not new or new == old:
            return
        self._update_style(
            **self._layout_controller.rename_changes(self._style, index, new)
        )
        self._refresh_layout_combo(selected=index)

    def _on_delete_layout(self) -> None:
        index = self._current_layout_index()
        if index <= 0:
            return
        name = self._style.layouts[index - 1].name
        fallback_name = layout_display_name(self._style, "default")
        confirmed = fluent_question(
            self,
            "删除布局",
            f"确定要删除布局“{name}”吗？\n"
            f"使用它的页面（和标题）会回到“{fallback_name}”。",
            yes_text="删除",
            no_text="取消",
            default_cancel=True,
        )
        if not confirmed:
            return
        self._update_style(
            **self._layout_controller.delete_changes(self._style, index)
        )
        self.layoutDeleted.emit(index)
        self._refresh_layout_combo(selected=0)
        self._sync_layout_editor_controls()

    def _save_current_layout_default(self) -> None:
        """Ask before persisting the selected project layout as an app default."""

        index = self._current_layout_index()
        name = (
            layout_display_name(self._style, "default")
            if index == 0
            else self._style.layouts[index - 1].name
        )
        if not fluent_question(
            self,
            "保存为软件默认布局",
            f"是否将布局“{name}”的当前参数保存到软件级新建项目默认值？\n"
            "这只影响以后新建的项目，不会应用到当前页面，也不会更改各行数的"
            "自动布局选择。",
            yes_text="保存",
            no_text="取消",
            default_cancel=True,
        ):
            return
        self.defaultLayoutSaveRequested.emit(index)

    def _make_line_alignments_box(self, parent: QWidget) -> QWidget:
        """行布局编辑器（示意图右侧列）：每行一个对齐按钮组，自上而下即
        第 1、2、…行——顺序即语义，不再需要「第N行」标签（N3 同款）。"""
        box = self._line_alignments_box = QWidget(parent)
        root = QVBoxLayout(box)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(6)
        root.addWidget(_subgroup_label("行布局"))

        self._line_alignment_rows_host = QWidget(box)
        self._line_alignment_rows = QVBoxLayout(self._line_alignment_rows_host)
        self._line_alignment_rows.setContentsMargins(0, 0, 0, 0)
        self._line_alignment_rows.setSpacing(6)
        root.addWidget(self._line_alignment_rows_host)

        self._add_line_alignment_btn = FluentPushButton("添加一行", box)
        self._add_line_alignment_btn.setMinimumHeight(30)
        self._add_line_alignment_btn.setToolTip(
            "底部锚定时在上方插入一行（复制第一行对齐），顶部/居中锚定时在下方"
            "追加一行（复制最后一行对齐）——与 N3 一致。"
        )
        self._add_line_alignment_btn.clicked.connect(
            lambda _checked=False: self._on_add_line_alignment()
        )
        root.addWidget(self._add_line_alignment_btn)
        root.addStretch(1)
        self._rebuild_line_alignment_rows()
        return box

    def _current_layout_alignments(self) -> list:
        source = self._current_layout_source()
        alignments = list(source.line_alignments) or ["left"]
        # 旧项目可能仍带「单行 / 居中 / 逐行独立」字段。新界面不再
        # 暴露这两个模式开关，因此把它们投影成右侧「行布局」的直接行列表。
        if not self._style.dual_line_layout:
            if self._style.line_horizontal_layout == "center":
                return ["center"]
            if self._style.line_horizontal_layout == "per_row":
                return [self._style.row1_align]
            return alignments[:1]
        if self._style.line_horizontal_layout == "center":
            return ["center"] * len(alignments)
        if self._style.line_horizontal_layout == "per_row":
            return [self._style.row1_align, self._style.row2_align]
        return alignments

    def _update_direct_line_alignments(self, alignments: list[str]) -> None:
        """行列表是新 UI 的唯一水平布局来源，写入时将旧模式归一化。"""
        normalized = [_normalize_horizontal_align(value) for value in alignments]
        index = self._current_layout_index()
        if index <= 0:
            self._update_style(
                line_alignments=normalized,
                dual_line_layout=True,
                line_horizontal_layout="asymmetric",
            )
            return
        layouts = list(self._style.layouts)
        layouts[index - 1] = replace(
            layouts[index - 1], line_alignments=normalized
        )
        self._update_style(
            layouts=layouts,
            dual_line_layout=True,
            line_horizontal_layout="asymmetric",
        )

    def _rebuild_line_alignment_rows(self) -> None:
        while self._line_alignment_rows.count():
            item = self._line_alignment_rows.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        alignments = self._current_layout_alignments()
        removable = len(alignments) > 1
        for index, align in enumerate(alignments):
            row = QWidget(self._line_alignment_rows_host)
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(6)
            seg = _GlyphSegment(_ALIGN_SEGMENT_OPTIONS, row)
            seg.setToolTip(f"第 {index + 1} 行对齐")
            seg.setValue(align)  # 先设值再连信号，避免建行时误触更新
            seg.valueChanged.connect(
                lambda value, idx=index: self._on_line_alignment_changed(idx, value)
            )
            remove_btn = FluentTransparentToolButton(FIF.CLOSE, row)
            remove_btn.setToolTip("删除此行")
            remove_btn.setEnabled(removable)
            remove_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            remove_btn.clicked.connect(
                lambda _checked=False, idx=index: self._on_remove_line_alignment(idx)
            )
            row_layout.addWidget(seg, 0)
            row_layout.addStretch(1)
            row_layout.addWidget(remove_btn, 0)
            self._line_alignment_rows.addWidget(row)
        self._add_line_alignment_btn.setEnabled(len(alignments) < 8)

    def _on_line_alignment_changed(self, index: int, value: str) -> None:
        if self._syncing:
            return
        alignments = self._current_layout_alignments()
        if not (0 <= index < len(alignments)) or alignments[index] == value:
            return
        alignments[index] = value
        self._update_direct_line_alignments(alignments)

    def _on_add_line_alignment(self) -> None:
        alignments = self._current_layout_alignments()
        if len(alignments) >= 8:
            return
        if self._current_layout_source().line_y_position == "bottom":
            alignments.insert(0, alignments[0])
        else:
            alignments.append(alignments[-1])
        self._update_direct_line_alignments(alignments)
        self._rebuild_line_alignment_rows()

    def _on_remove_line_alignment(self, index: int) -> None:
        alignments = self._current_layout_alignments()
        if len(alignments) <= 1 or not (0 <= index < len(alignments)):
            return
        del alignments[index]
        self._update_direct_line_alignments(alignments)
        self._rebuild_line_alignment_rows()

    def _refresh_layout_schematic(self) -> None:
        if not hasattr(self, "_layout_schematic"):
            return
        values = self._current_layout_values()
        style = self._style
        self._layout_schematic.set_state(
            mode="asymmetric",
            dual_line=True,
            alignments=self._current_layout_alignments(),
            y_position=values["line_y_position"],
            y_margin=int(values["line_y_margin_px"]),
            gap=int(values["line_gap_px"]),
            h_margin=int(values["horizontal_margin_px"]),
            font_px=int(style.font_size_px or 70),
            vertical=bool(style.vertical),
        )

    def _make_timing_section(self) -> QFrame:
        return self._timing_page_builder.make_section()

    def _sync_sync_each_page_enabled(self) -> None:
        """Enable the child option only while either synchronization mode is active."""

        if not hasattr(self, "_sync_each_page_check"):
            return
        self._sync_each_page_check.setEnabled(
            self._sync_entry_check.isChecked()
            or self._sync_ending_check.isChecked()
        )

    def _color_button(self, field_name: str, color: str) -> ColorButton:
        button = ColorButton(color)
        self._wire_color_edit_session(button)
        button.clicked.connect(lambda _checked=False, field=field_name: self._choose_color(field))
        button.colorEntered.connect(
            lambda color, field=field_name: self._set_color(field, color)
        )
        button.screenPickRequested.connect(
            lambda field=field_name: self._choose_color(
                field, screen_pick=True, preview_button=button
            )
        )
        return button

    def _wire_color_edit_session(self, button: ColorButton) -> None:
        button.editStarted.connect(self._begin_color_edit_session)
        button.editFinished.connect(self._finish_color_edit_session)
        button.editCancelled.connect(self._cancel_color_edit_session)

    def _begin_color_edit_session(self) -> None:
        self._color_edit_style_snapshot = deepcopy(self._style)

    def _finish_color_edit_session(self) -> None:
        self._color_edit_style_snapshot = None

    def _cancel_color_edit_session(self) -> None:
        snapshot = self._color_edit_style_snapshot
        self._color_edit_style_snapshot = None
        if snapshot is not None and snapshot != self._style:
            self.set_style(snapshot, emit=True)

    # ------------------------------------------------------------------ update

    def _begin_screen_color_pick(
        self, preview_button: ColorButton, apply_color
    ) -> None:
        if self._screen_color_picker is not None:
            self._screen_color_picker.cancel()

        original_color = preview_button.color
        accepted = False
        picker = ScreenColorPicker()
        self._screen_color_picker = picker

        def preview_color(color: QColor) -> None:
            preview_button.set_color(color.name(QColor.NameFormat.HexArgb))

        def accept_color(color: QColor) -> None:
            nonlocal accepted
            accepted = True
            preview_color(color)
            apply_color(color)

        def clear_picker() -> None:
            if not accepted:
                preview_button.set_color(original_color)
            if self._screen_color_picker is picker:
                self._screen_color_picker = None

        picker.colorHovered.connect(preview_color)
        picker.colorPicked.connect(accept_color)
        picker.finished.connect(clear_picker)
        picker.start()

    def _choose_color(
        self,
        field_name: str,
        *,
        screen_pick: bool = False,
        preview_button: Optional[ColorButton] = None,
    ) -> None:
        if screen_pick:
            if preview_button is None:
                return
            self._begin_screen_color_pick(
                preview_button,
                lambda color: self._set_color(
                    field_name, color.name(QColor.NameFormat.HexArgb)
                )
            )
            return
        current = QColor(self._scheme_value(field_name))
        color = _select_color(current, self, "选择颜色")
        if color.isValid():
            self._set_color(field_name, color.name(QColor.NameFormat.HexArgb))

    def _set_color(self, field_name: str, color: str) -> None:
        normalized = _normalize_hex(color, str(self._scheme_value(field_name)))
        changes = {field_name: normalized}
        if field_name == "ruby_color":
            # 选了单色就退出"跟随主文字"模式，让单色重新生效。
            changes["ruby_colors_follow_main"] = False
            changes["ruby_karaoke_colors"] = None
        else:
            colors = _apply_legacy_color_to_matrix(
                self._current_karaoke_colors(), field_name, normalized
            )
            if colors is not None:
                changes["karaoke_colors"] = colors
                if bool(self._scheme_value("ruby_colors_follow_main")):
                    changes["ruby_karaoke_colors"] = None
        self._update_style(**changes)

    def _choose_paint_color(
        self,
        field_name: str,
        *,
        screen_pick: bool = False,
        preview_button: Optional[ColorButton] = None,
    ) -> None:
        if screen_pick:
            if preview_button is None:
                return
            self._begin_screen_color_pick(
                preview_button,
                lambda color: self._update_current_fill(
                    **{
                        field_name: _normalize_hex(
                            color.name(QColor.NameFormat.HexArgb)
                        )
                    }
                )
            )
            return
        fill = self._current_paint_fill()
        current = QColor(getattr(fill, field_name))
        color = _select_color(current, self, "选择颜色")
        if color.isValid():
            normalized = _normalize_hex(color.name(QColor.NameFormat.HexArgb))
            self._update_current_fill(**{field_name: normalized})

    def _choose_paint_image(self) -> None:
        path, _selected_filter = QFileDialog.getOpenFileName(
            self,
            "选择填充图像",
            self._paint_image_path_edit.text(),
            "图像文件 (*.bmp *.gif *.ico *.jpeg *.jpg *.png *.tif *.tiff *.webp);;"
            "所有文件 (*.*)",
        )
        if path:
            self._paint_image_path_edit.setText(path)
            self._update_current_fill(image_path=path)

    def _on_color_subject_tab_changed(self, subject: str) -> None:
        # 不阻塞信号：combo 变化要驱动 _on_color_subject_changed 的完整同步
        # （注音按钮显隐 + 宽度/装饰/填充重灌）
        self._color_subject_combo.setCurrentIndex(
            max(0, self._color_subject_combo.findData(subject))
        )

    def _on_color_state_tab_changed(self, state: str) -> None:
        self._color_state_combo.blockSignals(True)
        try:
            self._color_state_combo.setCurrentIndex(
                max(0, self._color_state_combo.findData(state))
            )
        finally:
            self._color_state_combo.blockSignals(False)
        self._sync_color_fill_controls()

    def _on_color_layer_pill_changed(self, layer: str) -> None:
        self._color_layer_combo.blockSignals(True)
        try:
            self._color_layer_combo.setCurrentIndex(
                max(0, self._color_layer_combo.findData(layer))
            )
        finally:
            self._color_layer_combo.blockSignals(False)
        self._sync_color_fill_controls()

    def _on_color_subject_changed(self) -> None:
        if hasattr(self, "_color_tab_panel"):
            self._color_tab_panel.set_right(self._current_color_subject_key())
        self._set_ruby_color_controls_visible(
            self._current_color_subject_key() == "ruby"
        )
        self._sync_ruby_color_follow_controls()
        self._sync_color_subject_style_controls()
        self._sync_color_fill_controls()

    def _on_color_target_combo_changed(self) -> None:
        if hasattr(self, "_color_tab_panel"):
            self._color_tab_panel.set_left(self._current_color_state_key())
        if hasattr(self, "_color_layer_pill"):
            self._color_layer_pill.set_current(self._current_color_layer_key())
        self._sync_color_fill_controls()

    def _current_color_state_key(self) -> ColorStateKey:
        data = self._color_state_combo.currentData()
        return data if data in {"before", "after"} else "after"  # type: ignore[return-value]

    def _current_color_layer_key(self) -> ColorLayerKey:
        data = self._color_layer_combo.currentData()
        if data in {"text", "stroke", "stroke2", "shadow"}:
            return data  # type: ignore[return-value]
        return "text"

    def _current_color_subject_key(self) -> str:
        if not hasattr(self, "_color_subject_combo"):
            return "main"
        data = self._color_subject_combo.currentData()
        return "ruby" if data == "ruby" else "main"

    def _scheme_glow_radius(self, *, after: bool) -> int:
        value = int(
            self._scheme_value("glow_after_radius_px" if after else "glow_before_radius_px")
        )
        return max(value, 0)

    def _current_karaoke_colors(self) -> KaraokeColors:
        return self._style_controller.current_karaoke_colors(
            self._style,
            self._current_custom_scheme_name(),
        )

    def _current_scheme_snapshot(self) -> SubtitleStyleScheme:
        return self._style_controller.snapshot(
            self._style,
            self._current_custom_scheme_name(),
        )

    def _current_ruby_karaoke_colors(self) -> KaraokeColors:
        if bool(self._scheme_value("ruby_colors_follow_main")):
            return self._current_karaoke_colors()
        value = self._scheme_value("ruby_karaoke_colors")
        if isinstance(value, KaraokeColors):
            return deepcopy(value)
        return self._current_karaoke_colors()

    def _current_editing_karaoke_colors(self) -> KaraokeColors:
        if self._current_color_subject_key() == "ruby":
            return self._current_ruby_karaoke_colors()
        return self._current_karaoke_colors()

    def _current_paint_fill(self) -> PaintFill:
        colors = self._current_editing_karaoke_colors()
        state = getattr(colors, self._current_color_state_key())
        return deepcopy(getattr(state, self._current_color_layer_key()))

    def _sync_color_fill_controls(self) -> None:
        """把当前填充回灌到颜色编辑区的控件。

        每改一次配色字段都会走到这里，而改一个纯色只需要动一个按钮：填充模式没
        变就不必重排栅格布局、不必重设渐变方向和可见性，渐变/分段色标没变也不必
        重建。这些结构性动作在真实工程上要几十毫秒，而且全在 GUI 线程上，是一次
        编辑里最大的一块阻塞。按各自的输入分别判重。
        """
        if not hasattr(self, "_fill_mode_combo"):
            return
        fill = self._current_paint_fill()
        # 判重必须带上「当前编辑的是哪一格颜色」（方案 / before-after / 图层）：
        # 换格子时新旧填充可能恰好相等，但可见性等仍要按新格子重算。
        slot = (
            self._current_scheme_key(),
            self._current_color_state_key(),
            self._current_color_layer_key(),
        )
        last = getattr(self, "_last_synced_fill", None)
        previous = last[1] if last is not None and last[0] == slot else None
        if previous is not None and previous == fill:
            return
        mode_changed = previous is None or previous.mode != fill.mode
        gradient_stops = _gradient_stops(fill)
        split_stops = _split_stops(fill)
        was_syncing = self._syncing
        self._syncing = True
        try:
            if mode_changed:
                mode_index = max(0, self._fill_mode_combo.findData(fill.mode))
                self._fill_mode_combo.setCurrentIndex(mode_index)
                self._fill_editor_stack.setCurrentIndex(_fill_stack_index(fill.mode))
                self._fill_editor_stack.updateGeometry()
                self._gradient_editor.set_orientation(fill.mode)
                self._arrange_stop_editor(
                    self._gradient_editor_layout,
                    self._gradient_bar_field,
                    self._gradient_color_field,
                    self._gradient_position_field,
                    vertical=fill.mode == "gradient_vertical",
                    footer=self._ruby_horizontal_gradient_with_main_check,
                )
                self._ruby_horizontal_gradient_with_main_check.setVisible(
                    fill.mode == "gradient_horizontal"
                )
            self._paint_solid_btn.set_color(fill.color)
            self._paint_gradient_start_btn.set_color(fill.start_color)
            self._paint_gradient_end_btn.set_color(fill.end_color)
            self._ruby_horizontal_gradient_with_main_check.setChecked(
                bool(self._scheme_value("ruby_horizontal_gradient_with_main"))
            )
            if mode_changed or previous is None or _gradient_stops(previous) != gradient_stops:
                self._gradient_editor.set_stops(gradient_stops)
                self._sync_gradient_stop_controls()
            if mode_changed or previous is None or _split_stops(previous) != split_stops:
                self._split_editor.set_stops(split_stops)
                self._sync_split_stop_controls()
            if previous is None or previous.image_path != fill.image_path:
                self._paint_image_path_edit.setText(fill.image_path)
            if previous is None or previous.image_scale_pct != fill.image_scale_pct:
                self._paint_image_scale_spin.setValue(fill.image_scale_pct)
            if mode_changed:
                self._sync_decoration_visibility()
        finally:
            self._syncing = was_syncing
        self._last_synced_fill = (slot, fill)

    def _sync_decoration_visibility(self) -> None:
        if not hasattr(self, "_decoration_type_field"):
            return
        is_decoration = self._current_color_layer_key() == "shadow"
        decoration_kind = str(self._scheme_value("decoration_kind"))
        is_shadow = decoration_kind == "shadow"
        is_glow = decoration_kind == "glow"
        self._decoration_type_field.setVisible(is_decoration)
        self._shadow_x_field.setVisible(is_decoration and is_shadow)
        self._shadow_y_field.setVisible(is_decoration and is_shadow)
        self._glow_controls_row.setVisible(is_decoration and is_glow)
        self._glow_radius_field.setVisible(is_decoration and is_glow)
        self._glow_after_radius_field.setVisible(is_decoration and is_glow)
        self._glow_concentration_field.setVisible(is_decoration and is_glow)

    def _sync_color_subject_style_controls(self) -> None:
        if not hasattr(self, "_decoration_type_combo"):
            return
        was_syncing = self._syncing
        self._syncing = True
        try:
            self._decoration_type_combo.setCurrentIndex(
                max(
                    0,
                    self._decoration_type_combo.findData(
                        str(self._scheme_value("decoration_kind"))
                    ),
                )
            )
            self._glow_radius_spin.setValue(
                int(self._scheme_value("glow_before_radius_px"))
            )
            self._glow_after_radius_spin.setValue(
                int(self._scheme_value("glow_after_radius_px"))
            )
            self._glow_concentration_combo.setCurrentIndex(
                max(
                    0,
                    self._glow_concentration_combo.findData(
                        int(self._scheme_value("glow_concentration_level"))
                    ),
                )
            )
            self._shadow_x_spin.setValue(
                int(self._scheme_value("shadow_offset_x"))
            )
            self._shadow_y_spin.setValue(
                int(self._scheme_value("shadow_offset_y"))
            )
            self._sync_decoration_visibility()
        finally:
            self._syncing = was_syncing

    def _update_current_fill(self, **changes) -> None:
        if self._syncing:
            return
        colors = self._current_editing_karaoke_colors()
        state_key = self._current_color_state_key()
        layer_key = self._current_color_layer_key()
        state = deepcopy(getattr(colors, state_key))
        fill = _replace_fill(getattr(state, layer_key), **changes)
        if "color" in changes:
            fill = _replace_fill(
                fill,
                start_color=changes["color"],
                end_color=changes["color"],
                gradient_stops=[(0, changes["color"]), (100, changes["color"])],
                split_top_color=changes["color"],
                split_bottom_color=changes["color"],
                split_stops=[
                    (0, changes["color"]),
                    (50, changes["color"]),
                    (100, changes["color"]),
                ],
            )
        state = replace(state, **{layer_key: fill})
        colors = replace(colors, **{state_key: state})
        if self._current_color_subject_key() == "ruby":
            self._update_style(
                ruby_colors_follow_main=False,
                ruby_karaoke_colors=colors,
            )
        else:
            style_changes: dict[str, object] = {"karaoke_colors": colors}
            if bool(self._scheme_value("ruby_colors_follow_main")):
                style_changes["ruby_karaoke_colors"] = None
            self._update_style(**style_changes)

    def _swap_karaoke_color_states(self) -> None:
        if self._syncing:
            return
        colors = self._current_editing_karaoke_colors()
        swapped = replace(
            colors,
            before=deepcopy(colors.after),
            after=deepcopy(colors.before),
        )
        if self._current_color_subject_key() == "ruby":
            self._update_style(
                ruby_colors_follow_main=False,
                ruby_karaoke_colors=swapped,
            )
        else:
            style_changes: dict[str, object] = {"karaoke_colors": swapped}
            if bool(self._scheme_value("ruby_colors_follow_main")):
                style_changes["ruby_karaoke_colors"] = None
            self._update_style(**style_changes)

    def _update_gradient_stops(self, stops: list[tuple[float, str]]) -> None:
        if self._syncing:
            return
        normalized = _normalize_gradient_stops(stops)
        self._update_current_fill(
            gradient_stops=normalized,
            start_color=normalized[0][1],
            end_color=normalized[-1][1],
        )

    def _sync_gradient_stop_controls(self) -> None:
        if not hasattr(self, "_gradient_stop_color_btn"):
            return
        was_syncing = self._syncing
        self._syncing = True
        try:
            position, color = self._gradient_editor.selected_stop
            self._gradient_stop_color_btn.set_color(color)
            self._gradient_stop_position_spin.setValue(position)
            self._gradient_stop_delete_btn.setEnabled(
                len(_gradient_stops(self._current_paint_fill())) > 2
                and position not in {0, 100}
            )
        finally:
            self._syncing = was_syncing

    def _choose_gradient_stop_color(self, *, screen_pick: bool = False) -> None:
        if screen_pick:
            self._begin_screen_color_pick(
                self._gradient_stop_color_btn,
                lambda color: self._gradient_editor.set_selected_color(
                    _normalize_hex(color.name(QColor.NameFormat.HexArgb))
                )
            )
            return
        current = QColor(self._gradient_editor.selected_stop[1])
        color = _select_color(current, self, "选择颜色")
        if color.isValid():
            normalized = _normalize_hex(color.name(QColor.NameFormat.HexArgb))
            self._gradient_editor.set_selected_color(normalized)

    def _set_gradient_stop_position(self, value: float) -> None:
        if self._syncing:
            return
        self._gradient_editor.set_selected_position(value)

    def _update_split_stops(self, stops: list[tuple[float, str]]) -> None:
        if self._syncing:
            return
        normalized = _normalize_gradient_stops(stops)
        interior = [position for position, _color in normalized if position not in {0, 100}]
        self._update_current_fill(
            split_stops=normalized,
            split_top_color=normalized[0][1],
            split_bottom_color=normalized[-2][1] if len(normalized) > 1 else normalized[-1][1],
            split_position_pct=interior[0] if interior else 50,
        )

    def _sync_split_stop_controls(self) -> None:
        if not hasattr(self, "_split_stop_color_btn"):
            return
        was_syncing = self._syncing
        self._syncing = True
        try:
            position, color = self._split_editor.selected_stop
            self._split_stop_color_btn.set_color(color)
            self._split_stop_position_spin.setValue(position)
            self._split_stop_delete_btn.setEnabled(
                len(_split_stops(self._current_paint_fill())) > 2
                and position not in {0, 100}
            )
        finally:
            self._syncing = was_syncing

    def _choose_split_stop_color(self, *, screen_pick: bool = False) -> None:
        if screen_pick:
            self._begin_screen_color_pick(
                self._split_stop_color_btn,
                lambda color: self._split_editor.set_selected_color(
                    _normalize_hex(color.name(QColor.NameFormat.HexArgb))
                )
            )
            return
        current = QColor(self._split_editor.selected_stop[1])
        color = _select_color(current, self, "选择分段颜色")
        if color.isValid():
            normalized = _normalize_hex(color.name(QColor.NameFormat.HexArgb))
            self._split_editor.set_selected_color(normalized)

    def _set_split_stop_position(self, value: float) -> None:
        if self._syncing:
            return
        self._split_editor.set_selected_position(value)

    def _refresh_scheme_combo(self, selected_key: Optional[str] = None) -> None:
        self._singer_combo.clear()
        self._singer_combo.addItem("全局默认", _GLOBAL_SCHEME_KEY)
        # 「标题」是内置方案：恒在列表中（标题外观由它描述），不参与角色分配。
        self._singer_combo.addItem(
            TITLE_SCHEME_NAME, f"{_CUSTOM_SCHEME_PREFIX}{TITLE_SCHEME_NAME}"
        )
        # 这里只显示当前字幕里出现过、或用户手动新建的角色目标。
        # 用户保存过的可复用预设由「样式预设库」维护，避免未分色文件自动套用历史方案。
        seen: set[str] = {TITLE_SCHEME_NAME}
        for name in self._role_controller.names:
            if name in seen:
                continue
            seen.add(name)
            self._singer_combo.addItem(name, f"{_CUSTOM_SCHEME_PREFIX}{name}")
        if selected_key is not None:
            index = self._singer_combo.findData(selected_key)
            if index >= 0:
                self._singer_combo.setCurrentIndex(index)
        self._sync_scheme_combo_width()

    def _sync_scheme_combo_width(self) -> None:
        """按最长角色名设置下拉框宽度，不占用导航条的全部剩余空间。"""
        if not hasattr(self, "_singer_combo"):
            return
        metrics = self._singer_combo.fontMetrics()
        text_width = max(
            (
                metrics.horizontalAdvance(str(self._singer_combo.itemText(index)))
                for index in range(self._singer_combo.count())
            ),
            default=0,
        )
        # 为文字左右留白和下拉箭头预留 48px；超长角色名交给
        # ComboBox 内部省略，不让导航条再次铺满整张卡片。
        self._singer_combo.setFixedWidth(max(120, min(280, text_width + 48)))

    def _add_custom_scheme(self, name: Optional[str] = None) -> None:
        if name is None or isinstance(name, bool):
            name, ok = fluent_get_text(self, "新建角色", "角色名称")
            if not ok:
                return
        name = name.strip()
        if not name:
            return
        previous_roles = self._role_controller.names
        changes, name = self._role_controller.add_scheme_changes(
            self._style,
            name,
            self._current_scheme_snapshot(),
        )
        self._update_style(**changes)
        if self._role_controller.names != previous_roles:
            self.rolesChanged.emit(self._role_controller.names)
        self._syncing = True
        try:
            self._refresh_scheme_combo(f"{_CUSTOM_SCHEME_PREFIX}{name}")
        finally:
            self._syncing = False
        self._sync_subtitle_scheme_controls()

    def _open_preset_manager(self) -> None:
        dialog = StylePresetManagerDialog(
            presets=self._preset_schemes,
            current_scheme=self._current_scheme_snapshot(),
            target_label=self._current_target_label(),
            existing_role_names=set(self._role_controller.names)
            | {TITLE_SCHEME_NAME},
            target_height=self._n3_template_target_height,
            lyrics_dir=self._n3_template_lyrics_dir,
            parent=self,
        )
        dialog.presetLibraryChanged.connect(self._set_preset_schemes_from_dialog)
        dialog.exec()
        schemes = dialog.preset_schemes()
        imported = dialog.imported_schemes()
        applied = dialog.applied_scheme()
        if schemes != self._preset_schemes:
            self._set_preset_schemes_from_dialog(schemes)
        if applied is not None:
            self._apply_preset_to_current_target(applied)
        if imported:
            self._import_preset_schemes(imported)

    def _save_current_scheme(self) -> None:
        """Persist the selected built-in default or copy a project role to the library."""
        key = self.current_scheme_key()
        role_name = self._current_custom_scheme_name()
        if key == _GLOBAL_SCHEME_KEY or role_name == TITLE_SCHEME_NAME:
            target = "全局默认" if key == _GLOBAL_SCHEME_KEY else "标题"
            if not fluent_question(
                self,
                f"保存{target}",
                f"将当前“{target}”方案保存为软件默认值？\n"
                "新建项目时将自动使用该方案。",
            ):
                return
            self.defaultSchemeSaveRequested.emit(key)
            return

        if not role_name:
            return
        groups = sorted(
            {preset.group for preset in self._preset_schemes.values() if preset.group}
        )
        dialog = _StylePresetDetailsDialog(
            name=role_name,
            group="",
            groups=groups,
            parent=self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        name, group = dialog.details()
        if not name:
            return

        presets = _normalize_style_presets(self._preset_schemes)
        matching_ids = _preset_ids_for_pair(presets, name, group)
        preset_id = matching_ids[0] if matching_ids else _new_preset_id(presets)
        for duplicate_id in matching_ids[1:]:
            presets.pop(duplicate_id, None)
        presets[preset_id] = StylePreset(
            name=name,
            group=group,
            scheme=deepcopy(self._current_scheme_snapshot()),
            preset_id=preset_id,
        )
        self._preset_schemes = presets
        self.presetSchemesChanged.emit(self.preset_schemes)
        InfoBar.success(
            title="已保存到预设库",
            content=(
                f"已更新“{group} / {name}”。"
                if matching_ids and group
                else f"已更新“{name}”。"
                if matching_ids
                else f"已保存“{group} / {name}”。"
                if group
                else f"已保存“{name}”。"
            ),
            parent=self,
            duration=2500,
        )

    def _set_preset_schemes_from_dialog(
        self, schemes: dict[str, StylePreset | SubtitleStyleScheme]
    ) -> None:
        self._preset_schemes = _normalize_style_presets(schemes)
        self.presetSchemesChanged.emit(self.preset_schemes)

    def _import_preset_schemes(
        self, schemes: dict[str, StylePreset | SubtitleStyleScheme]
    ) -> None:
        if not schemes:
            return
        previous_roles = self._role_controller.names
        changes = self._role_controller.import_preset_changes(
            self._style,
            list(_normalize_style_presets(schemes).values()),
            reserved_name=TITLE_SCHEME_NAME,
        )
        self._update_style(**changes)
        if self._role_controller.names != previous_roles:
            self.rolesChanged.emit(self._role_controller.names)
        self._syncing = True
        try:
            self._refresh_scheme_combo(self._current_scheme_key())
        finally:
            self._syncing = False
        self._sync_subtitle_scheme_controls()

    def _ensure_role_schemes(self) -> None:
        base_scheme = self._current_scheme_snapshot()
        self._style, changed = self._role_controller.ensure_style_schemes(
            self._style,
            self._preset_schemes,
            lambda index: _auto_role_scheme(base_scheme, index),
        )
        if changed:
            self._sync_font_preview()
            self.styleChanged.emit(self._style)

    def _current_target_label(self) -> str:
        role_name = self._current_custom_scheme_name()
        return role_name if role_name is not None else "全局默认"

    def _apply_preset_to_current_target(self, scheme: SubtitleStyleScheme) -> None:
        role_name = self._current_custom_scheme_name()
        if role_name is not None:
            previous_roles = self._role_controller.names
            changes = self._role_controller.apply_scheme_changes(
                self._style,
                role_name,
                scheme,
            )
            self._update_style(**changes)
            if self._role_controller.names != previous_roles:
                self.rolesChanged.emit(self._role_controller.names)
            return
        changes = self._style_controller.changes_from_scheme(scheme)
        if changes:
            self._update_style(**changes)

    def _current_scheme_key(self) -> Optional[str]:
        if not hasattr(self, "_singer_combo"):
            return None
        data = self._singer_combo.currentData()
        return str(data) if data is not None else _GLOBAL_SCHEME_KEY

    def current_scheme_key(self) -> str:
        return self._current_scheme_key() or _GLOBAL_SCHEME_KEY

    def set_current_scheme_key(self, key: str) -> None:
        if not hasattr(self, "_singer_combo"):
            return
        index = self._singer_combo.findData(key)
        if index < 0:
            index = self._singer_combo.findData(_GLOBAL_SCHEME_KEY)
        if index < 0:
            return
        self._singer_combo.setCurrentIndex(index)

    def _on_scheme_combo_changed(self, _index: int) -> None:
        name = self._current_custom_scheme_name()
        editable = name is not None and name != TITLE_SCHEME_NAME
        if hasattr(self, "_rename_role_button"):
            self._rename_role_button.setEnabled(editable)
            self._delete_role_button.setEnabled(editable)
        self._sync_subtitle_scheme_controls()
        self._sync_font_preview()
        if not self._syncing:
            self.schemeSelectionChanged.emit(self.current_scheme_key())

    def _current_custom_scheme_name(self) -> Optional[str]:
        key = self._current_scheme_key()
        if key is None or not key.startswith(_CUSTOM_SCHEME_PREFIX):
            return None
        return key.removeprefix(_CUSTOM_SCHEME_PREFIX)

    def _scheme_value(self, field_name: str):
        return self._style_controller.value(
            self._style,
            self._current_custom_scheme_name(),
            field_name,
        )

    def _scheme_own_value(self, field_name: str):
        """方案**自己**存的值；``None`` 表示这一槽没设定（跟随上一级）。

        与 :meth:`_scheme_value` 的分工：那个给最终生效值（角色没设就回退全
        局），填输入框用；这个用来判断"这一槽是不是空着"。
        """
        return self._style_controller.own_value(
            self._style,
            self._current_custom_scheme_name(),
            field_name,
        )

    def _rename_current_role(self) -> None:
        old = self._current_custom_scheme_name()
        if old is None or old == TITLE_SCHEME_NAME:
            return  # 全局默认 / 内置「标题」方案不能重命名（标题按名字引用它）
        new, ok = fluent_get_text(self, "重命名角色", "角色名称", text=old)
        if not ok:
            return
        new = new.strip()
        if not new or new == old:
            return
        if (
            new in self._role_controller.names
            or new in self._style.custom_style_schemes
        ):
            InfoBar.warning(
                title="名称已存在",
                content=f"项目中已经存在角色“{new}”。",
                parent=self,
                duration=2000,
            )
            return
        previous_roles = self._role_controller.names
        changes = self._role_controller.rename_changes(
            self._style,
            old,
            new,
            self._current_scheme_snapshot(),
        )
        self._update_style(**changes)
        if self._role_controller.names != previous_roles:
            self.rolesChanged.emit(self._role_controller.names)
        self._syncing = True
        try:
            self._refresh_scheme_combo(f"{_CUSTOM_SCHEME_PREFIX}{new}")
        finally:
            self._syncing = False
        self._sync_subtitle_scheme_controls()

    def _delete_current_role(self) -> None:
        name = self._current_custom_scheme_name()
        if name is None or name == TITLE_SCHEME_NAME:
            return  # 全局默认 / 内置「标题」方案不能删
        previous_roles = self._role_controller.names
        changes = self._role_controller.delete_changes(self._style, name)
        self._update_style(**changes)
        if self._role_controller.names != previous_roles:
            self.rolesChanged.emit(self._role_controller.names)
        self._syncing = True
        try:
            self._refresh_scheme_combo(_GLOBAL_SCHEME_KEY)
        finally:
            self._syncing = False
        self._sync_subtitle_scheme_controls()

    def _sync_subtitle_scheme_controls(self) -> None:
        if not hasattr(self, "_singer_combo"):
            return
        was_syncing = self._syncing
        self._syncing = True
        try:
            self._font_combo.setCurrentFont(QFont(str(self._scheme_value("font_family"))))
            latin_family = self._scheme_value("font_family_latin")
            if latin_family is None:
                self._font_latin_combo.setInherited()
            else:
                self._font_latin_combo.setCurrentFont(QFont(str(latin_family)))
            self._font_size_spin.setValue(int(self._scheme_value("font_size_px")))
            latin_size = self._scheme_value("latin_font_size_px")
            self._font_latin_size_spin.setValue(
                0 if latin_size is None else int(latin_size)
            )
            self._space_width_spin.setValue(int(self._scheme_value("space_width_percent")))
            latin_weight = self._scheme_value("latin_font_weight")
            self._italic_check.setChecked(bool(self._scheme_value("italic")))
            self._ruby_anchor_check.setChecked(
                bool(self._scheme_value("affects_ruby_anchor"))
            )
            ruby_family = self._scheme_value("ruby_font_family")
            ruby_size = self._scheme_value("ruby_font_size_px")
            ruby_weight = self._scheme_value("ruby_font_weight")
            if ruby_family is None:
                self._ruby_font_combo.setInherited()
            else:
                self._ruby_font_combo.setCurrentFont(QFont(str(ruby_family)))
            self._ruby_font_size_spin.setValue(int(ruby_size))
            ruby_latin_family = self._scheme_value("ruby_font_family_latin")
            ruby_latin_size = self._scheme_value("ruby_latin_font_size_px")
            ruby_latin_weight = self._scheme_value("ruby_latin_font_weight")
            if ruby_latin_family is None:
                self._ruby_font_latin_combo.setInherited()
            else:
                self._ruby_font_latin_combo.setCurrentFont(QFont(str(ruby_latin_family)))
            self._ruby_font_latin_size_spin.setValue(
                0 if ruby_latin_size is None else int(ruby_latin_size)
            )
            self._sync_font_size_follow_controls()
            self._refresh_font_weight_combos(
                {
                    ("main", "japanese"): int(self._scheme_value("font_weight")),
                    ("main", "latin"): (
                        0 if latin_weight is None else int(latin_weight)
                    ),
                    ("ruby", "japanese"): (
                        0 if ruby_weight is None else int(ruby_weight)
                    ),
                    ("ruby", "latin"): (
                        0 if ruby_latin_weight is None else int(ruby_latin_weight)
                    ),
                }
            )
            self._sync_font_stroke_controls()
            self._sync_color_subject_style_controls()
            self._set_ruby_color_controls_visible(
                self._current_color_subject_key() == "ruby"
            )
            self._sync_ruby_color_follow_controls()
            self._sync_color_fill_controls()
        finally:
            self._syncing = was_syncing

    def _sync_font_stroke_controls(self) -> None:
        if not hasattr(self, "_font_stroke_controls"):
            return
        main_width = int(self._scheme_value("stroke_width_px"))
        main_enabled = bool(self._scheme_value("stroke2_enabled"))
        main_width2 = int(self._scheme_value("stroke2_width_px"))

        latin_width_value = self._scheme_value("latin_stroke_width_px")
        latin_enabled_value = self._scheme_value("latin_stroke2_enabled")
        latin_width2_value = self._scheme_value("latin_stroke2_width_px")
        latin_width = 0 if latin_width_value is None else int(latin_width_value)
        latin_enabled = (
            False if latin_enabled_value is None else bool(latin_enabled_value)
        )
        latin_width2 = (
            0 if latin_width2_value is None else int(latin_width2_value)
        )

        ruby_width_value = self._scheme_value("ruby_stroke_width_px")
        ruby_enabled_value = self._scheme_value("ruby_stroke2_enabled")
        ruby_width2_value = self._scheme_value("ruby_stroke2_width_px")
        scale = max(int(self._scheme_value("ruby_font_size_px")), 1) / max(
            int(self._scheme_value("font_size_px")), 1
        )
        ruby_width = (
            _scaled_panel_px(main_width, scale)
            if ruby_width_value is None
            else int(ruby_width_value)
        )
        ruby_enabled = (
            main_enabled if ruby_enabled_value is None else bool(ruby_enabled_value)
        )
        ruby_width2 = (
            _scaled_panel_px(main_width2, scale)
            if ruby_width2_value is None
            else int(ruby_width2_value)
        )

        ruby_latin_width_value = self._scheme_value("ruby_latin_stroke_width_px")
        ruby_latin_enabled_value = self._scheme_value("ruby_latin_stroke2_enabled")
        ruby_latin_width2_value = self._scheme_value("ruby_latin_stroke2_width_px")
        values = {
            ("main", "japanese"): (main_width, main_enabled, main_width2),
            ("main", "latin"): (latin_width, latin_enabled, latin_width2),
            ("ruby", "japanese"): (ruby_width, ruby_enabled, ruby_width2),
            ("ruby", "latin"): (
                0 if ruby_latin_width_value is None else int(ruby_latin_width_value),
                False
                if ruby_latin_enabled_value is None
                else bool(ruby_latin_enabled_value),
                0
                if ruby_latin_width2_value is None
                else int(ruby_latin_width2_value),
            ),
        }
        inherited_enabled = {
            ("main", "latin"): latin_enabled_value is None,
            ("ruby", "japanese"): ruby_enabled_value is None,
            ("ruby", "latin"): ruby_latin_enabled_value is None,
        }
        for key, (width, enabled, width2) in values.items():
            width_spin, enabled_check, width2_spin = self._font_stroke_controls[key]
            width_spin.setValue(width)
            inherited = inherited_enabled.get(key, False)
            if inherited:
                enabled_check.setCheckState(Qt.CheckState.PartiallyChecked)
            else:
                enabled_check.setChecked(enabled)
            width2_spin.setValue(width2)
            width2_spin.setEnabled(inherited or enabled)

    def _sync_lit_controls(self) -> None:
        if not hasattr(self, "_lit_enabled_switch"):
            return
        self._lit_enabled_switch.setChecked(self._style.lit_enabled)
        self._lit_style_combo.setCurrentIndex(
            max(0, self._lit_style_combo.findData(self._style.lit_style))
        )
        self._lit_number_spin.setValue(self._style.lit_number)
        self._lit_size_spin.setValue(self._style.lit_size)
        self._lit_x_spin.setValue(self._style.lit_offset_x)
        self._lit_y_spin.setValue(self._style.lit_offset_y)
        self._lit_tracking_spin.setValue(self._style.lit_tracking)
        self._lit_duration_spin.setValue(self._style.signals_duration_ms)
        self._lit_stroke_width_spin.setValue(self._style.lit_stroke_width)
        self._lit_fill_btn.set_color(self._style.lit_fill_color)
        self._lit_stroke_btn.set_color(self._style.lit_stroke_color)
        self._lit_stroke_soften_spin.setValue(self._style.lit_stroke_soften)
        self._lit_opacity_spin.setValue(self._style.lit_opacity_pct)
        self._lit_edge_brightness_spin.setValue(self._style.lit_edge_brightness_pct)
        self._lit_shadow_check.setChecked(self._style.lit_shadow)
        self._lit_waiting_time_spin.setValue(self._style.lit_waiting_time_ms)
        self._lit_transition_mode_combo.setCurrentIndex(
            max(0, self._lit_transition_mode_combo.findData(self._style.lit_transition_mode))
        )
        self._lit_transition_ratio_spin.setValue(self._style.lit_transition_ratio_pct)
        self._lit_transition_angle_spin.setValue(self._style.lit_transition_angle_deg)
        self._lit_transition_distance_spin.setValue(self._style.lit_transition_distance)
        self._volume_size_spin.setValue(self._style.volume_size)
        self._volume_x_spin.setValue(self._style.volume_offset_x)
        self._volume_y_spin.setValue(self._style.volume_offset_y)
        self._volume_column_width_spin.setValue(self._style.volume_column_width)
        self._volume_column_count_spin.setValue(self._style.volume_column_count)
        self._volume_column_spacing_spin.setValue(self._style.volume_column_spacing)
        self._volume_ratio_spin.setValue(int(round(self._style.volume_ratio)))
        self._volume_align_combo.setCurrentIndex(
            max(0, self._volume_align_combo.findData(self._style.volume_align))
        )
        self._volume_flash_times_spin.setValue(self._style.volume_flash_times)
        self._volume_flash_duration_spin.setValue(
            int(round(self._style.volume_flash_duration_ratio * 100))
        )
        self._volume_transition_ratio_spin.setValue(self._style.volume_transition_ratio_pct)
        self._volume_fill_btn.set_color(self._style.volume_fill_color)
        self._volume_stroke_btn.set_color(self._style.volume_stroke_color)
        self._volume_overlay_fill_btn.set_color(self._style.volume_overlay_fill_color)
        self._volume_overlay_stroke_btn.set_color(self._style.volume_overlay_stroke_color)
        self._sync_lit_style_visibility()

    def _update_style(self, _force_global: bool = False, **changes) -> None:
        if self._syncing:
            return
        result = self._style_controller.update(
            self._style,
            changes,
            role_name=self._current_custom_scheme_name(),
            force_global=_force_global,
            scheme_factory=self._current_scheme_snapshot,
        )
        self._style = result.style
        changes = result.changed_fields
        self._syncing = True
        try:
            if (
                self._style.karaoke_anim == "inherit"
                and {"entry_anim", "exit_anim"}.intersection(changes)
            ):
                self._karaoke_anim_combo.setCurrentIndex(
                    max(
                        0,
                        self._karaoke_anim_combo.findData(
                            effective_karaoke_animation(self._style)
                        ),
                    )
                )
            if set(changes).intersection(
                _SCHEME_FIELDS | {"singer_style_overrides", "custom_style_schemes"}
            ):
                self._sync_subtitle_scheme_controls()
            if set(changes).intersection(_LIT_FIELDS):
                self._sync_lit_controls()
        finally:
            self._syncing = False
        # 布局示意图跟随所有影响排版的修改（set_state 内部有变更判重）
        self._refresh_layout_schematic()
        self._sync_font_preview()
        self.styleChanged.emit(self._style)


def _scaled_panel_px(value: int, scale: float) -> int:
    if value <= 0:
        return 0
    return max(1, int(round(value * scale)))


def _scaled_panel_signed_px(value: int, scale: float) -> int:
    if value == 0:
        return 0
    sign = 1 if value > 0 else -1
    return sign * max(1, int(round(abs(value) * scale)))


def _fill_stack_index(mode: str) -> int:
    if mode in {"gradient_horizontal", "gradient_vertical"}:
        return 1
    if mode == "split_vertical":
        return 2
    if mode == "image":
        return 3
    return 0


def _replace_fill(fill: PaintFill, **changes) -> PaintFill:
    if "start_color" in changes or "end_color" in changes:
        stops = _gradient_stops(fill)
        if "start_color" in changes:
            stops = [(0, changes["start_color"])] + [(p, c) for p, c in stops if p != 0]
        if "end_color" in changes:
            stops = [(p, c) for p, c in stops if p != 100] + [(100, changes["end_color"])]
        changes.setdefault("gradient_stops", _normalize_gradient_stops(stops))
    if "gradient_stops" in changes:
        stops = _normalize_gradient_stops(changes["gradient_stops"])
        changes["gradient_stops"] = stops
        changes.setdefault("start_color", stops[0][1])
        changes.setdefault("end_color", stops[-1][1])
    if "split_stops" in changes:
        stops = _normalize_gradient_stops(changes["split_stops"])
        changes["split_stops"] = stops
        changes.setdefault("split_top_color", stops[0][1])
        changes.setdefault(
            "split_bottom_color", stops[-2][1] if len(stops) > 1 else stops[-1][1]
        )
        interior = [position for position, _color in stops if position not in {0, 100}]
        changes.setdefault("split_position_pct", interior[0] if interior else 50)
    return replace(fill, **changes)


def _apply_legacy_color_to_matrix(
    colors: KaraokeColors, field_name: str, color: str
) -> Optional[KaraokeColors]:
    colors = deepcopy(colors)
    if field_name == "base_color":
        colors.before.text = _solid_fill(color)
        return colors
    if field_name == "fill_color":
        colors.after.text = _replace_fill(colors.after.text, color=color)
        return colors
    if field_name == "fill_gradient_start_color":
        colors.after.text = _replace_fill(
            colors.after.text,
            start_color=color,
            split_top_color=color,
        )
        return colors
    if field_name == "fill_gradient_end_color":
        colors.after.text = _replace_fill(
            colors.after.text,
            end_color=color,
            split_bottom_color=color,
        )
        return colors
    if field_name == "stroke_color":
        colors.before.stroke = _solid_fill(color)
        colors.after.stroke = _solid_fill(color)
        return colors
    if field_name == "shadow_color":
        colors.before.shadow = _solid_fill(color)
        colors.after.shadow = _solid_fill(color)
        return colors
    return None


def _solid_fill(color: str) -> PaintFill:
    return PaintFill(
        mode="solid",
        color=color,
        start_color=color,
        end_color=color,
        gradient_stops=[(0, color), (100, color)],
        split_top_color=color,
        split_bottom_color=color,
    )


def _auto_role_scheme(base: SubtitleStyleScheme, index: int) -> SubtitleStyleScheme:
    color = _AUTO_ROLE_COLORS[index % len(_AUTO_ROLE_COLORS)]
    colors = deepcopy(base.karaoke_colors) if base.karaoke_colors is not None else KaraokeColors()
    colors = _apply_legacy_color_to_matrix(colors, "fill_color", color) or colors
    return replace(
        deepcopy(base),
        fill_color=color,
        fill_gradient_enabled=False,
        fill_gradient_start_color=color,
        fill_gradient_end_color=color,
        karaoke_colors=colors,
    )


def _spin(
    minimum: int,
    maximum: int,
    *,
    suffix: str = "",
    cls: type[_WheelFocusedSpinBox] = _WheelFocusedSpinBox,
) -> Any:
    spin = cls()
    spin.setRange(minimum, maximum)
    spin.setSuffix(suffix)
    spin.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    _compact_control(spin)
    spin._sync_text_minimum()
    return spin


def _double_spin(
    minimum: float,
    maximum: float,
    *,
    decimals: int = 2,
    suffix: str = "",
) -> _WheelFocusedDoubleSpinBox:
    spin = _WheelFocusedDoubleSpinBox()
    spin.setRange(minimum, maximum)
    spin.setDecimals(decimals)
    spin.setSingleStep(0.1)
    spin.setSuffix(suffix)
    spin.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    _compact_control(spin)
    spin._sync_text_minimum()
    return spin


def _scroll_page() -> tuple[FluentScrollArea, QVBoxLayout]:
    scroll = FluentScrollArea()
    scroll.setObjectName("SubtitlePropertyScroll")
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(FluentScrollArea.Shape.NoFrame)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    page = QWidget()
    page.setObjectName("SubtitlePropertyPage")
    page.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
    themed(page, lambda: "#SubtitlePropertyPage { background: transparent; }")
    layout = QVBoxLayout(page)
    layout.setContentsMargins(10, 10, 10, 12)
    layout.setSpacing(10)
    scroll.setWidget(page)
    return scroll, layout


def _grid_adder(grid: QGridLayout):
    """Return an ``add(label, control)`` that fills a 2-column grid left→right."""
    pos = [0, 0]

    def add(label: Optional[str], control: QWidget) -> None:
        widget = _field(label, control) if label is not None else control
        grid.addWidget(widget, pos[0], pos[1])
        pos[1] += 1
        if pos[1] >= 2:
            pos[0] += 1
            pos[1] = 0

    return add


def _solid_paint_fill(color: str) -> PaintFill:
    normalized = _normalize_hex(color)
    return PaintFill(
        mode="solid",
        color=normalized,
        start_color=normalized,
        end_color=normalized,
        gradient_stops=[(0, normalized), (100, normalized)],
        split_top_color=normalized,
        split_bottom_color=normalized,
    )
