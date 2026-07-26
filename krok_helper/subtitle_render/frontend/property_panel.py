"""右侧字幕属性面板。

窄侧栏里不要使用横向表单布局：标签和输入框会互相挤压，尤其是
字体选择框。这里采用工具软件常见的分组卡片 + 垂直字段，保证
280-320px 宽度下没有横向溢出。
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
import json
import math
from pathlib import Path
from typing import Any, Callable, Optional
from uuid import uuid4

from PyQt6.QtCore import (
    QEvent,
    QPoint,
    QPointF,
    QRect,
    QRectF,
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
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QPolygonF,
    QValidator,
)
from PyQt6.QtWidgets import (
    QAbstractButton,
    QAbstractItemView,
    QApplication,
    QBoxLayout,
    QColorDialog,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QStyle,
    QToolButton,
    QVBoxLayout,
    QWidget,
    QWIDGETSIZE_MAX,
)
from qfluentwidgets import (
    Action,
    BodyLabel,
    CaptionLabel,
    CheckBox,
    ComboBox as FluentComboBox,
    DoubleSpinBox as FluentDoubleSpinBox,
    EditableComboBox as FluentEditableComboBox,
    FluentIcon as FIF,
    InfoBar,
    LineEdit as FluentLineEdit,
    ListWidget as FluentListWidget,
    PlainTextEdit as FluentPlainTextEdit,
    PrimaryPushButton as FluentPrimaryPushButton,
    PushButton as FluentPushButton,
    RoundMenu,
    ScrollArea as FluentScrollArea,
    SegmentedWidget,
    SpinBox as FluentSpinBox,
    SubtitleLabel,
    ToolButton as FluentToolButton,
    TransparentToolButton as FluentTransparentToolButton,
)

from krok_helper.subtitle_render.frontend.fluent_dialogs import (
    fluent_button_row,
    fluent_choice,
    fluent_get_editable_choice,
    fluent_get_text,
    fluent_question,
    fluent_warning,
)
from krok_helper.subtitle_render.frontend.theme import control_qss, palette, themed
from krok_helper.subtitle_render.n3_font_catalog import (
    canonicalize_n3_font_family,
    n3_font_families,
)
from krok_helper.subtitle_render.models import (
    ColorLayerKey,
    ColorStateKey,
    DecorationKind,
    EntryAnimation,
    ExitAnimation,
    HORIZONTAL_ALIGNS,
    HorizontalAlign,
    KaraokeAnimation,
    KaraokeColors,
    KaraokeColorState,
    LineHorizontalLayout,
    LineYPosition,
    LYRICS_LAYOUT_FIELDS,
    LyricsLayout,
    N3_FONT_INHERITANCE_FIELDS,
    PaintFill,
    StylePreset,
    SubtitleStyleScheme,
    Style,
    TITLE_SCHEME_NAME,
    TITLE_SHOW_MODES,
    TitleOverlay,
    effective_karaoke_animation,
    layout_display_name,
    migrate_title_char_role_labels,
    VIEWPORT_ALIGNS,
    ViewportAlign,
)
from krok_helper.subtitle_render.n3_template_import import (
    N3_TEMPLATE_FILTER,
    default_n3_template_directories,
    find_n3_template_files,
    load_n3_font_templates,
    merge_n3_template_presets,
    resolve_n3_template_preset,
)

_SCHEME_FIELDS = {
    "font_family",
    "font_family_latin",
    "font_size_px",
    "latin_font_size_px",
    "latin_font_weight",
    "latin_stroke_width_px",
    "latin_stroke2_enabled",
    "latin_stroke2_width_px",
    "letter_spacing_px",
    "space_width_percent",
    "allow_biting",
    "font_weight",
    "italic",
    "affects_ruby_anchor",
    "base_color",
    "fill_color",
    "fill_gradient_enabled",
    "fill_gradient_start_color",
    "fill_gradient_end_color",
    "fill_gradient_angle_deg",
    "stroke_color",
    "stroke_width_px",
    "stroke2_enabled",
    "stroke2_width_px",
    "decoration_kind",
    "glow_radius_px",
    "glow_before_radius_px",
    "glow_after_radius_px",
    "glow_concentration_level",
    "shadow_color",
    "shadow_offset_x",
    "shadow_offset_y",
    "ruby_font_size_px",
    "ruby_font_family",
    "ruby_font_family_latin",
    "ruby_font_weight",
    "ruby_latin_font_size_px",
    "ruby_latin_font_weight",
    "ruby_font_follow_main",
    "ruby_color",
    "ruby_gap_px",
    "ruby_stroke_width_px",
    "ruby_stroke2_enabled",
    "ruby_stroke2_width_px",
    "ruby_latin_stroke_width_px",
    "ruby_latin_stroke2_enabled",
    "ruby_latin_stroke2_width_px",
    "ruby_decoration_kind",
    "ruby_glow_radius_px",
    "ruby_glow_before_radius_px",
    "ruby_glow_after_radius_px",
    "ruby_glow_concentration_level",
    "ruby_shadow_offset_x",
    "ruby_shadow_offset_y",
    "ruby_colors_follow_main",
    "ruby_horizontal_gradient_with_main",
    "karaoke_colors",
    "ruby_karaoke_colors",
}

_GLOBAL_SCHEME_KEY = "global"
_CUSTOM_SCHEME_PREFIX = "custom:"
_PRESET_NO_GROUP = "\x00ungrouped"
_COMPACT_CONTROL_HEIGHT = 32
_FONT_SIZE_MAX_PX = 4096
_LAYOUT_SIZE_MAX_PX = 16_384
_FILL_MODE_ICON_DIR = (
    Path(__file__).resolve().parents[2] / "assets" / "subtitle_render" / "fill_modes"
)
_COLOR_STATE_SWAP_ICON = (
    Path(__file__).resolve().parents[2]
    / "assets"
    / "subtitle_render"
    / "swap-colors.svg"
)
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


@dataclass(frozen=True)
class ScreenPreset:
    """Sayatoo-compatible screen preset metadata."""

    key: str
    label: str
    width: int
    height: int
    par: str = "1:1"


@dataclass(frozen=True)
class ScreenSettings:
    """Canvas/export screen settings persisted with projects and user settings."""

    preset_key: str = "hdtv_1080"
    par: str = "1:1"
    width: int = 1920
    height: int = 1080
    fps: int = 60


SCREEN_FPS_OPTIONS = (60, 120)
SCREEN_PRESETS: tuple[ScreenPreset, ...] = (
    ScreenPreset("hd_540", "HD 540", 960, 540),
    ScreenPreset("hdv_720", "HDV 720", 1280, 720),
    ScreenPreset("hdtv_720", "HDTV 720", 1280, 720),
    ScreenPreset("hdv_1080", "HDV 1080", 1440, 1080, "4:3"),
    ScreenPreset("hdtv_1080", "HDTV 1080", 1920, 1080),
    ScreenPreset("dvcprohd_720", "DVCPROHD 720", 960, 720, "4:3"),
    ScreenPreset("dvcprohd_1080", "DVCPROHD 1080", 1280, 1080, "3:2"),
    ScreenPreset("d1_dv_ntsc", "D1/DV NTSC", 720, 480, "10:11"),
    ScreenPreset("d1_dv_ntsc_wide", "D1/DV NTSC 宽屏", 720, 480, "40:33"),
    ScreenPreset("d1_dv_pal", "D1/DV PAL", 720, 576, "128:117"),
    ScreenPreset("d1_dv_pal_wide", "D1/DV PAL 宽屏", 720, 576, "512:351"),
    ScreenPreset("uhd_4k", "UHD 4K", 3840, 2160),
    ScreenPreset("uhd_8k", "UHD 8K", 7680, 4320),
    ScreenPreset("hd_540_vertical", "HD 540 竖屏", 540, 960),
    ScreenPreset("hd_720_vertical", "HD 720 竖屏", 720, 1280),
    ScreenPreset("hdtv_1080_vertical", "HDTV 1080 竖屏", 1080, 1920),
)

PAR_OPTIONS: tuple[tuple[str, str], ...] = (
    ("方形像素", "1:1"),
    ("HDV 1080 / DVCPROHD 720（4:3）", "4:3"),
    ("DVCPROHD 1080（3:2）", "3:2"),
    ("D1/DV NTSC（10:11）", "10:11"),
    ("D1/DV NTSC 宽屏（40:33）", "40:33"),
    ("D1/DV PAL（128:117）", "128:117"),
    ("D1/DV PAL 宽屏（512:351）", "512:351"),
)

_SCREEN_PRESET_BY_KEY = {preset.key: preset for preset in SCREEN_PRESETS}
_PAR_VALUES = {value for _label, value in PAR_OPTIONS}


def screen_settings_to_dict(settings: ScreenSettings) -> dict[str, Any]:
    return {
        "preset_key": settings.preset_key,
        "par": settings.par,
        "width": settings.width,
        "height": settings.height,
        "fps": settings.fps,
    }


def screen_settings_from_dict(payload: object) -> ScreenSettings:
    if not isinstance(payload, dict):
        return ScreenSettings()
    width = _int_setting(payload.get("width"), 160, 7680, ScreenSettings.width)
    height = _int_setting(payload.get("height"), 90, 4320, ScreenSettings.height)
    fps = _normalize_screen_fps(payload.get("fps"))
    par = str(payload.get("par") or ScreenSettings.par)
    if par not in _PAR_VALUES:
        par = ScreenSettings.par
    preset_key = str(payload.get("preset_key") or "")
    if preset_key not in _SCREEN_PRESET_BY_KEY and preset_key != "custom":
        preset_key = match_screen_preset_key(width, height, par)
    return ScreenSettings(
        preset_key=preset_key,
        par=par,
        width=width,
        height=height,
        fps=fps,
    )


def _int_setting(value: object, minimum: int, maximum: int, fallback: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return fallback
    return min(max(number, minimum), maximum)


def _normalize_screen_fps(value: object) -> int:
    try:
        fps = int(value)
    except (TypeError, ValueError):
        return ScreenSettings.fps
    return fps if fps in SCREEN_FPS_OPTIONS else ScreenSettings.fps


def match_screen_preset_key(width: int, height: int, par: str) -> str:
    for preset in SCREEN_PRESETS:
        if preset.width == width and preset.height == height and preset.par == par:
            return preset.key
    return "custom"


def _normalize_hex(value: str, fallback: str = "#000000") -> str:
    color = QColor(value)
    if not color.isValid():
        color = QColor(fallback)
    name_format = (
        QColor.NameFormat.HexArgb
        if color.alpha() < 255
        else QColor.NameFormat.HexRgb
    )
    return color.name(name_format).upper()


def _parse_hex_color(value: str) -> Optional[str]:
    """Parse common RGB/ARGB hex input, with or without a leading hash."""
    digits = value.strip()
    if digits.startswith("#"):
        digits = digits[1:]
    if len(digits) not in {3, 4, 6, 8} or any(
        character not in "0123456789abcdefABCDEF" for character in digits
    ):
        return None
    if len(digits) in {3, 4}:
        digits = "".join(character * 2 for character in digits)
    color = QColor(f"#{digits}")
    if not color.isValid():
        return None
    return _normalize_hex(color.name(QColor.NameFormat.HexArgb))


def _eyedropper_icon() -> QIcon:
    """Small theme-aware eyedropper icon without an external bitmap asset."""
    pixmap = QPixmap(20, 20)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        pen = QPen(QColor(palette().text_primary), 1.8)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        path = QPainterPath()
        path.moveTo(5.0, 15.5)
        path.lineTo(7.2, 15.5)
        path.lineTo(15.0, 7.7)
        path.lineTo(12.3, 5.0)
        path.lineTo(4.5, 12.8)
        path.closeSubpath()
        painter.drawPath(path)
        painter.drawLine(QPointF(11.2, 3.8), QPointF(16.2, 8.8))
        painter.drawLine(QPointF(13.1, 5.7), QPointF(15.7, 3.1))
        painter.drawLine(QPointF(4.5, 15.5), QPointF(3.0, 17.0))
    finally:
        painter.end()
    return QIcon(pixmap)


class _ColorSwatchButton(QPushButton):
    """Color value bar used inside :class:`ColorButton`."""

    def __init__(self, color: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._color = _normalize_hex(color)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(30)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._apply()

    @property
    def color(self) -> str:
        return self._color

    def set_color(self, color: str) -> None:
        normalized = _normalize_hex(color, self._color)
        if normalized == self._color:
            return
        self._color = normalized
        self._apply()

    def _apply(self) -> None:
        color = QColor(self._color)
        text_color = "#111827" if color.lightness() > 150 else "#FFFFFF"
        self.setText(self._color)
        background = f"rgba({color.red()}, {color.green()}, {color.blue()}, {color.alpha()})"
        self.setStyleSheet(
            f"""
            QPushButton {{
                background: {background};
                color: {text_color};
                border: 1px solid {palette().card_border};
                border-radius: 6px;
                padding: 0 8px;
                font-family: "Consolas", "Courier New", monospace;
                font-size: 9pt;
            }}
            QPushButton:hover {{
                background: {background};
                border-color: {palette().card_border};
            }}
            """
        )


class _ColorHexEdit(FluentLineEdit):
    """Inline hex editor that lets Escape cancel without changing the color."""

    cancelRequested = Signal()
    finishRequested = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._context_menu_active = False

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            self.cancelRequested.emit()
            event.accept()
            return
        super().keyPressEvent(event)

    def focusOutEvent(self, event) -> None:  # noqa: N802
        super().focusOutEvent(event)
        if not self._context_menu_active:
            self.finishRequested.emit()


class ColorButton(QWidget):
    """Compact color bar with dialog and direct screen-picker actions."""

    _LIVE_APPLY_DELAY_MS = 180

    clicked = Signal()
    screenPickRequested = Signal()
    colorEntered = Signal(str)
    editStarted = Signal()
    editFinished = Signal()
    editCancelled = Signal()

    def __init__(self, color: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(30)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        self._swatch_stack = QStackedWidget(self)
        self._swatch_stack.setFixedHeight(30)
        self._swatch_stack.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )

        self._swatch = _ColorSwatchButton(color, self._swatch_stack)
        self._swatch.clicked.connect(self._begin_color_entry)
        self._swatch.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._swatch.customContextMenuRequested.connect(
            lambda pos: self._show_color_context_menu(self._swatch.mapToGlobal(pos))
        )
        self._color_edit = _ColorHexEdit(self._swatch_stack)
        self._color_edit.setFixedHeight(30)
        self._color_edit.setMaxLength(9)
        self._color_edit.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._color_edit.setPlaceholderText("RRGGBB / AARRGGBB")
        self._color_edit.textEdited.connect(self._schedule_live_color_entry)
        self._color_edit.returnPressed.connect(self._commit_color_entry)
        self._color_edit.cancelRequested.connect(self._cancel_color_entry)
        self._color_edit.finishRequested.connect(self._finish_color_entry)
        self._color_edit.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._color_edit.customContextMenuRequested.connect(
            lambda pos: self._show_color_context_menu(
                self._color_edit.mapToGlobal(pos)
            )
        )
        self._swatch_stack.addWidget(self._swatch)
        self._swatch_stack.addWidget(self._color_edit)
        self._live_apply_timer = QTimer(self)
        self._live_apply_timer.setSingleShot(True)
        self._live_apply_timer.setInterval(self._LIVE_APPLY_DELAY_MS)
        self._live_apply_timer.timeout.connect(self._apply_live_color_entry)
        self._entry_original_color = self.color
        self._ending_color_entry = False

        self.palette_button = FluentToolButton(FIF.PALETTE, self)
        self.palette_button.setFixedSize(30, 30)
        self.palette_button.setToolTip("打开颜色选择窗口")
        self.palette_button.setAccessibleName("打开颜色选择窗口")
        self.palette_button.clicked.connect(self.clicked.emit)

        self.screen_picker_button = FluentToolButton(_eyedropper_icon(), self)
        self.screen_picker_button.setFixedSize(30, 30)
        self.screen_picker_button.setToolTip("从屏幕取色")
        self.screen_picker_button.setAccessibleName("从屏幕取色")
        self.screen_picker_button.clicked.connect(self.screenPickRequested.emit)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(self._swatch_stack, 1)
        layout.addWidget(self.palette_button, 0)
        layout.addWidget(self.screen_picker_button, 0)

    @property
    def color(self) -> str:
        return self._swatch.color

    def set_color(self, color: str) -> None:
        self._swatch.set_color(color)

    def text(self) -> str:
        return self._swatch.text()

    def click(self) -> None:
        self._swatch.click()

    def _show_color_context_menu(self, global_pos: QPoint) -> None:
        menu = RoundMenu(parent=self)
        copy_action = Action("复制色号", menu)
        copy_action.triggered.connect(self._copy_color_to_clipboard)
        menu.addAction(copy_action)

        paste_action = Action("粘贴色号", menu)
        paste_action.setEnabled(
            _parse_hex_color(QApplication.clipboard().text()) is not None
        )
        paste_action.triggered.connect(self._paste_color_from_clipboard)
        menu.addAction(paste_action)

        editing = self._swatch_stack.currentWidget() is self._color_edit
        self._color_edit._context_menu_active = editing
        try:
            menu.exec(global_pos)
        finally:
            self._color_edit._context_menu_active = False
        if self._swatch_stack.currentWidget() is self._color_edit:
            self._color_edit.setFocus(Qt.FocusReason.PopupFocusReason)

    def _copy_color_to_clipboard(self) -> None:
        QApplication.clipboard().setText(self.color)

    def _paste_color_from_clipboard(self) -> bool:
        color = _parse_hex_color(QApplication.clipboard().text())
        if color is None:
            return False
        if self._swatch_stack.currentWidget() is not self._color_edit:
            self._begin_color_entry()
        self._color_edit.setText(color)
        self._commit_color_entry()
        return True

    def _begin_color_entry(self) -> None:
        self._live_apply_timer.stop()
        self._entry_original_color = self.color
        self.editStarted.emit()
        self._color_edit.setText(self.color)
        self._color_edit.setToolTip("输入 RGB 或 ARGB 色号，完成后自动应用")
        self._swatch_stack.setCurrentWidget(self._color_edit)
        self._color_edit.setFocus(Qt.FocusReason.MouseFocusReason)
        self._color_edit.selectAll()

    def _schedule_live_color_entry(self, _text: str) -> None:
        self._color_edit.setToolTip("输入 RGB 或 ARGB 色号，完成后自动应用")
        self._live_apply_timer.start()

    def _apply_color_entry(self) -> bool:
        color = _parse_hex_color(self._color_edit.text())
        if color is None:
            self._color_edit.setToolTip("色号无效，请输入 RGB 或 ARGB 十六进制色号")
            return False
        if color != self.color:
            self.set_color(color)
            self.colorEntered.emit(color)
        self._color_edit.setToolTip("色号已自动应用")
        return True

    def _apply_live_color_entry(self) -> None:
        if self._swatch_stack.currentWidget() is self._color_edit:
            self._apply_color_entry()

    def _commit_color_entry(self) -> None:
        self._live_apply_timer.stop()
        if not self._apply_color_entry():
            self._color_edit.selectAll()
            return
        self._end_color_entry()
        self.editFinished.emit()

    def _finish_color_entry(self) -> None:
        if self._ending_color_entry:
            return
        self._live_apply_timer.stop()
        if self._swatch_stack.currentWidget() is self._color_edit:
            self._apply_color_entry()
            self._end_color_entry()
            self.editFinished.emit()

    def _end_color_entry(self) -> None:
        self._ending_color_entry = True
        try:
            self._swatch_stack.setCurrentWidget(self._swatch)
        finally:
            self._ending_color_entry = False

    def _cancel_color_entry(self) -> None:
        self._live_apply_timer.stop()
        if self._swatch_stack.currentWidget() is self._color_edit:
            original = self._entry_original_color
            if self.color != original:
                self.set_color(original)
                self.colorEntered.emit(original)
            self.editCancelled.emit()
            self._end_color_entry()


class ScreenColorPicker(QWidget):
    """Transparent virtual-desktop overlay for direct screen color picking."""

    colorPicked = Signal(QColor)
    colorHovered = Signal(QColor)
    finished = Signal()

    def __init__(self) -> None:
        super().__init__(
            None,
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint,
        )
        self._active = True
        self._last_hovered_rgba: Optional[int] = None
        self._screens: list[tuple[QRect, QImage]] = []
        desktop_geometry = QRect()
        for screen in QApplication.screens():
            geometry = screen.geometry()
            desktop_geometry = desktop_geometry.united(geometry)
            self._screens.append((geometry, screen.grabWindow(0).toImage()))

        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        if desktop_geometry.isValid():
            self.setGeometry(desktop_geometry)

    def start(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()
        self.setFocus(Qt.FocusReason.OtherFocusReason)
        self.grabMouse()
        self.grabKeyboard()
        QTimer.singleShot(0, lambda: self._emit_hovered_color(QCursor.pos()))

    def color_at(self, global_position: QPoint) -> QColor:
        for geometry, image in self._screens:
            if not geometry.contains(global_position) or image.isNull():
                continue
            x_ratio = image.width() / max(geometry.width(), 1)
            y_ratio = image.height() / max(geometry.height(), 1)
            x = int((global_position.x() - geometry.x()) * x_ratio)
            y = int((global_position.y() - geometry.y()) * y_ratio)
            x = min(max(x, 0), image.width() - 1)
            y = min(max(y, 0), image.height() - 1)
            return image.pixelColor(x, y)
        return QColor()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            color = self.color_at(event.globalPosition().toPoint())
            if color.isValid():
                self.colorPicked.emit(color)
            self._finish()
            event.accept()
            return
        if event.button() == Qt.MouseButton.RightButton:
            self._finish()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        self._emit_hovered_color(event.globalPosition().toPoint())
        event.accept()

    def _emit_hovered_color(self, global_position: QPoint) -> None:
        if not self._active:
            return
        color = self.color_at(global_position)
        if not color.isValid() or color.rgba() == self._last_hovered_rgba:
            return
        self._last_hovered_rgba = color.rgba()
        self.colorHovered.emit(color)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            self._finish()
            event.accept()
            return
        super().keyPressEvent(event)

    def cancel(self) -> None:
        self._finish()

    def _finish(self) -> None:
        if not self._active:
            return
        self._active = False
        self.releaseMouse()
        self.releaseKeyboard()
        self.hide()
        self.finished.emit()
        self.deleteLater()


class _AlphaSlider(QWidget):
    """Vertical checkerboard slider for editing a QColor alpha channel."""

    alphaChanged = Signal(int)

    def __init__(self, color: QColor, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._color = QColor(color)
        self._alpha = color.alpha()
        self.setObjectName("ColorAlphaSlider")
        self.setFixedWidth(42)
        self.setMinimumHeight(80)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName("透明度")
        self._update_tooltip()

    @property
    def alpha(self) -> int:
        return self._alpha

    def set_color(self, color: QColor) -> None:
        if not color.isValid():
            return
        changed = color.rgba() != self._color.rgba()
        self._color = QColor(color)
        self._alpha = color.alpha()
        self._update_tooltip()
        if changed:
            self.update()

    def _set_alpha(self, alpha: int, *, emit: bool) -> None:
        alpha = max(0, min(255, int(alpha)))
        if alpha == self._alpha:
            return
        self._alpha = alpha
        self._color.setAlpha(alpha)
        self._update_tooltip()
        self.update()
        if emit:
            self.alphaChanged.emit(alpha)

    def _set_alpha_from_y(self, y: float) -> None:
        groove = self._groove_rect()
        ratio = 1.0 - (y - groove.top()) / max(groove.height() - 1, 1)
        self._set_alpha(round(max(0.0, min(1.0, ratio)) * 255), emit=True)

    def _groove_rect(self) -> QRect:
        return QRect(9, 4, max(self.width() - 18, 1), max(self.height() - 8, 1))

    def _update_tooltip(self) -> None:
        percent = round(self._alpha * 100 / 255)
        self.setToolTip(f"透明度：{percent}%（Alpha {self._alpha}）")

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._set_alpha_from_y(event.position().y())
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if event.buttons() & Qt.MouseButton.LeftButton:
            self._set_alpha_from_y(event.position().y())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        steps = {
            Qt.Key.Key_Up: 1,
            Qt.Key.Key_Right: 1,
            Qt.Key.Key_Down: -1,
            Qt.Key.Key_Left: -1,
            Qt.Key.Key_PageUp: 16,
            Qt.Key.Key_PageDown: -16,
        }
        step = steps.get(event.key())
        if step is not None:
            self._set_alpha(self._alpha + step, emit=True)
            event.accept()
            return
        super().keyPressEvent(event)

    def paintEvent(self, event) -> None:  # noqa: N802, ARG002
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
            groove = self._groove_rect()
            tile = 6
            light = QColor("#FFFFFF")
            dark = QColor("#C8CDD5")
            for y in range(groove.top(), groove.bottom() + 1, tile):
                for x in range(groove.left(), groove.right() + 1, tile):
                    color = light if ((x // tile) + (y // tile)) % 2 == 0 else dark
                    painter.fillRect(
                        QRect(
                            x,
                            y,
                            min(tile, groove.right() - x + 1),
                            min(tile, groove.bottom() - y + 1),
                        ),
                        color,
                    )

            opaque = QColor(self._color)
            opaque.setAlpha(255)
            transparent = QColor(opaque)
            transparent.setAlpha(0)
            gradient = QLinearGradient(
                float(groove.left()),
                float(groove.top()),
                float(groove.left()),
                float(groove.bottom()),
            )
            gradient.setColorAt(0.0, opaque)
            gradient.setColorAt(1.0, transparent)
            painter.fillRect(groove, gradient)
            painter.setPen(QPen(QColor(palette().input_border), 1))
            painter.drawRect(groove.adjusted(0, 0, -1, -1))

            handle_y = groove.top() + round(
                (255 - self._alpha) * max(groove.height() - 1, 1) / 255
            )
            painter.setPen(QPen(QColor("#FFFFFF"), 3))
            painter.drawLine(3, handle_y, self.width() - 4, handle_y)
            painter.setPen(QPen(QColor("#111827"), 1))
            painter.drawLine(3, handle_y, self.width() - 4, handle_y)
        finally:
            painter.end()


class _ColorDialog(QColorDialog):
    """Qt color dialog with a visible alpha slider beside the hue strip."""

    def __init__(
        self, current: QColor, parent: Optional[QWidget] = None
    ) -> None:
        super().__init__(current, parent)
        self.setOption(QColorDialog.ColorDialogOption.ShowAlphaChannel, True)
        self.setOption(QColorDialog.ColorDialogOption.DontUseNativeDialog, True)
        self.setCurrentColor(current)
        self._alpha_slider = _AlphaSlider(self.currentColor(), self)
        self._alpha_slider.alphaChanged.connect(self._set_current_alpha)
        self.currentColorChanged.connect(self._alpha_slider.set_color)

    def _set_current_alpha(self, alpha: int) -> None:
        color = self.currentColor()
        if color.alpha() == alpha:
            return
        color.setAlpha(alpha)
        self.setCurrentColor(color)

    def _position_alpha_slider(self) -> None:
        candidates = [
            child
            for child in self.children()
            if isinstance(child, QWidget)
            and child is not self._alpha_slider
            and child.width() <= 40
            and child.height() >= 100
        ]
        if not candidates:
            self._alpha_slider.hide()
            return
        hue_picker = max(candidates, key=lambda child: child.height())
        geometry = hue_picker.geometry()
        self._alpha_slider.setGeometry(
            geometry.right() + 10,
            geometry.top(),
            self._alpha_slider.width(),
            geometry.height(),
        )
        self._alpha_slider.show()
        self._alpha_slider.raise_()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._position_alpha_slider()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if hasattr(self, "_alpha_slider"):
            self._position_alpha_slider()


def _select_color(current: QColor, parent: QWidget, title: str) -> QColor:
    """Open the regular color dialog used by the palette action."""
    dialog = _ColorDialog(current, parent)
    dialog.setWindowTitle(title)
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return QColor()
    return dialog.selectedColor()


class ToggleSwitch(QAbstractButton):
    """A compact iOS-style on/off switch used in place of a checkbox."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._track_w = 38
        self._track_h = 22
        self.setFixedSize(self._track_w, self._track_h)

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(self._track_w, self._track_h)

    def paintEvent(self, event) -> None:  # noqa: N802, ARG002
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            p = palette()
            checked = self.isChecked()
            track = QColor(p.accent_primary if checked else p.input_border)
            if not self.isEnabled():
                track.setAlpha(90)
            radius = self.height() / 2
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(track)
            painter.drawRoundedRect(
                QRectF(0, 0, self.width(), self.height()), radius, radius
            )
            knob = self.height() - 6
            x = self.width() - knob - 3 if checked else 3
            painter.setBrush(QColor("#FFFFFF"))
            painter.drawEllipse(QRectF(x, 3, knob, knob))
        finally:
            painter.end()


class CollapsibleSection(QFrame):
    """A property card with a clickable header and collapsible content."""

    def __init__(
        self,
        title: str,
        parent: Optional[QWidget] = None,
        *,
        switch: bool = False,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("SubtitlePropertySection")
        self._content = QWidget(self)
        self._content.setObjectName("SubtitlePropertySectionContent")
        self._header = QToolButton(self)
        self._header.setObjectName("SubtitlePropertySectionHeader")
        self._header.setText(title)
        self._header.setCheckable(True)
        self._header.setChecked(True)
        self._header.setArrowType(Qt.ArrowType.DownArrow)
        self._header.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self._header.setCursor(Qt.CursorShape.PointingHandCursor)
        self._header.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._header.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        self._header.clicked.connect(self.set_expanded)

        header_row = QWidget(self)
        header_row.setObjectName("SubtitlePropertySectionHeaderRow")
        header_layout = QHBoxLayout(header_row)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(0)
        header_layout.addWidget(self._header, 0)
        # 折叠摘要：内容收起后把关键状态（如当前角色名）留在标题栏。
        # 展开时隐藏——正文自身可见，标题栏不重复。
        self._summary_text = ""
        self._summary_label = QLabel(header_row)
        self._summary_label.setObjectName("SubtitlePropertySectionSummary")
        self._summary_label.setVisible(False)
        themed(
            self._summary_label,
            lambda: f"color: {palette().text_secondary}; font-size: 9.5pt;",
        )
        header_layout.addWidget(self._summary_label, 0, Qt.AlignmentFlag.AlignVCenter)
        header_layout.addStretch(1)

        self.header_switch: Optional[ToggleSwitch] = None
        if switch:
            self.header_switch = ToggleSwitch(header_row)
            header_layout.addWidget(
                self.header_switch, 0, Qt.AlignmentFlag.AlignVCenter
            )
            header_layout.addSpacing(12)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(header_row)
        root.addWidget(self._content)

        self.content_layout = QVBoxLayout(self._content)
        self.content_layout.setContentsMargins(12, 0, 12, 12)
        self.content_layout.setSpacing(10)

    @property
    def header(self) -> QToolButton:
        return self._header

    def set_expanded(self, expanded: bool) -> None:
        self._header.setChecked(expanded)
        self._header.setArrowType(
            Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow
        )
        self._content.setVisible(expanded)
        self._refresh_summary()

    def is_expanded(self) -> bool:
        return self._content.isVisible()

    def set_collapsed_summary(self, text: str) -> None:
        """设置折叠态显示在标题栏的摘要文本；空串表示无摘要。"""
        self._summary_text = str(text)
        self._refresh_summary()

    def _refresh_summary(self) -> None:
        # 用 header 的 checked 态判断展开与否——widget 未 show 前
        # isVisible() 恒为 False，不能作为展开依据。
        collapsed = not self._header.isChecked()
        self._summary_label.setText(self._summary_text)
        self._summary_label.setVisible(collapsed and bool(self._summary_text))


class _ClickableRow(QWidget):
    """A bare row widget that emits ``clicked`` on a left mouse press."""

    clicked = Signal()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


class _SubGroup(QWidget):
    """A collapsible sub-section inside a property card (accent-bar heading + grid)."""

    def __init__(
        self,
        title: str,
        *,
        collapsed: bool = False,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 4, 0, 0)
        root.setSpacing(6)

        self._header = _ClickableRow(self)
        self._header.setCursor(Qt.CursorShape.PointingHandCursor)
        header_layout = QHBoxLayout(self._header)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(6)
        label = _subgroup_label(title)
        label.setParent(self._header)
        header_layout.addWidget(label, 0)
        header_layout.addStretch(1)
        self._chevron = QLabel(self._header)
        themed(
            self._chevron,
            lambda: f"color: {palette().text_secondary}; font-size: 9pt;",
        )
        header_layout.addWidget(self._chevron, 0, Qt.AlignmentFlag.AlignVCenter)

        self._host = QWidget(self)
        self.grid = QGridLayout(self._host)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setHorizontalSpacing(8)
        self.grid.setVerticalSpacing(8)
        self.grid.setColumnStretch(0, 1)
        self.grid.setColumnStretch(1, 1)

        root.addWidget(self._header)
        root.addWidget(self._host)

        self._header.clicked.connect(lambda: self.set_collapsed(self._host.isVisible()))
        self.set_collapsed(collapsed)

    def set_collapsed(self, collapsed: bool) -> None:
        self._host.setVisible(not collapsed)
        self._chevron.setText("▸" if collapsed else "▾")

    def is_collapsed(self) -> bool:
        return not self._host.isVisible()


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


class _PillSelector(QWidget):
    """一行紧凑胶囊单选组：走字前/后切换与图层选择共用。

    取代旧的 2×4 状态×图层矩阵——前/后两列按钮完全重复，一个双段开关 +
    一行图层按钮就够了。按钮按文字紧凑测宽、靠左排布，不再撑满整列。
    """

    changed = Signal(str)

    def __init__(
        self,
        options: tuple[tuple[str, str], ...],
        parent: Optional[QWidget] = None,
        *,
        vertical: bool = False,
        icons: Optional[dict[str, QIcon]] = None,
    ) -> None:
        super().__init__(parent)
        self._current = options[0][0] if options else ""
        self._buttons: dict[str, QPushButton] = {}

        row = QVBoxLayout(self) if vertical else QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)
        for key, label in options:
            btn = QPushButton(label, self)
            btn.setObjectName("ColorPillCell")
            btn.setCheckable(True)
            btn.setMinimumHeight(28)
            btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            icon = (icons or {}).get(key)
            if icon is not None:
                # 图标模式与项目紧凑控件使用同一逻辑尺寸；Qt 会按 DPR 生成
                # 对应设备像素，不依赖某张截图的物理像素。
                btn.setText("")
                btn.setIcon(icon)
                icon_extent = _COMPACT_CONTROL_HEIGHT * 3 // 4
                btn.setIconSize(QSize(icon_extent, icon_extent))
                btn.setFixedSize(_COMPACT_CONTROL_HEIGHT, _COMPACT_CONTROL_HEIGHT)
                btn.setToolTip(label)
                btn.setAccessibleName(label)
            # 竖排列内按钮统一拉到列宽（列宽 = 最宽文字），横排按文字紧凑测宽
            if icon is None:
                btn.setSizePolicy(
                    QSizePolicy.Policy.Expanding if vertical else QSizePolicy.Policy.Maximum,
                    QSizePolicy.Policy.Fixed,
                )
            btn.clicked.connect(lambda _checked=False, k=key: self._select(k))
            self._buttons[key] = btn
            row.addWidget(btn, 0)
        row.addStretch(1)
        themed(
            self,
            lambda: (
                f"""
                QPushButton#ColorPillCell {{
                    background: {palette().secondary_button_bg};
                    color: {palette().secondary_button_text};
                    border: 1px solid {palette().secondary_button_border};
                    border-radius: 6px;
                    padding: 0 10px;
                    font-size: 9.5pt;
                }}
                QPushButton#ColorPillCell:hover {{
                    border-color: {palette().accent_primary};
                }}
                QPushButton#ColorPillCell:checked {{
                    background: {palette().accent_primary};
                    color: #FFFFFF;
                    border-color: {palette().accent_primary};
                    font-weight: 600;
                }}
                """
            ),
        )
        self._refresh_checked()

    def current(self) -> str:
        return self._current

    def set_current(self, key: str) -> None:
        if key == self._current or key not in self._buttons:
            self._refresh_checked()
            return
        self._current = key
        self._refresh_checked()

    def _select(self, key: str) -> None:
        if key != self._current:
            self._current = key
            self._refresh_checked()
            self.changed.emit(key)
        else:
            self._refresh_checked()  # 点当前项也保持选中态

    def _refresh_checked(self) -> None:
        for key, btn in self._buttons.items():
            btn.setChecked(key == self._current)


class _FolderTabPanel(QWidget):
    """文件夹式 tab 面板：上圆角 tab，选中 tab 底色与灰色内容区连成一片。

    左右两组都是「点一下切换」，不再用下拉。tab 普通态白底、选中态与内容区
    同灰底且底边不画线，面板轮廓用灰描边连贯勾出（对应用户手绘的路径）。
    tab 形状与底色全部在 paintEvent 里画，按钮本体透明、只出文字和点击。
    """

    leftChanged = Signal(str)
    rightChanged = Signal(str)

    _TAB_HEIGHT = 32
    _RADIUS = 8.0
    _TAB_RADIUS = 6.0

    def __init__(
        self,
        left_options: tuple[tuple[str, str], ...],
        right_options: tuple[tuple[str, str], ...],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        # 允许右组为空（只有左上角一组 tab 的面板，如字体的日文/英数）
        self._current = {
            "left": left_options[0][0] if left_options else "",
            "right": right_options[0][0] if right_options else "",
        }
        self._buttons: dict[tuple[str, str], QPushButton] = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        tab_row = QHBoxLayout()
        # tab 离面板左右边缘留出比圆角略大的缩进，转角处不打架
        tab_row.setContentsMargins(12, 0, 12, 0)
        tab_row.setSpacing(0)
        for key, label in left_options:
            tab_row.addWidget(self._make_tab("left", key, label), 0)
        tab_row.addStretch(1)
        for key, label in right_options:
            tab_row.addWidget(self._make_tab("right", key, label), 0)
        root.addLayout(tab_row)

        self._content = QWidget(self)
        self.content_layout = QVBoxLayout(self._content)
        self.content_layout.setContentsMargins(10, 10, 10, 10)
        self.content_layout.setSpacing(10)
        root.addWidget(self._content)

        themed(self, self._tab_qss)
        self._refresh_checked()

    def _make_tab(self, side: str, key: str, label: str) -> QPushButton:
        btn = QPushButton(label, self)
        btn.setObjectName("FolderTabButton")
        btn.setCheckable(True)
        btn.setFixedHeight(self._TAB_HEIGHT)
        btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        btn.clicked.connect(lambda _checked=False, s=side, k=key: self._select(s, k))
        self._buttons[(side, key)] = btn
        return btn

    @staticmethod
    def _tab_qss() -> str:
        return (
            "QPushButton#FolderTabButton {"
            " background: transparent; border: none; padding: 0 14px;"
            f" font-size: 9.5pt; color: {palette().text_secondary}; }}"
            "QPushButton#FolderTabButton:checked {"
            f" color: {palette().title_text}; font-weight: 600; }}"
        )

    def current_left(self) -> str:
        return self._current["left"]

    def current_right(self) -> str:
        return self._current["right"]

    def set_left(self, key: str) -> None:
        self._set_current("left", key)

    def set_right(self, key: str) -> None:
        self._set_current("right", key)

    def _set_current(self, side: str, key: str) -> None:
        if key != self._current[side] and (side, key) in self._buttons:
            self._current[side] = key
        self._refresh_checked()

    def _select(self, side: str, key: str) -> None:
        if key != self._current[side]:
            self._current[side] = key
            self._refresh_checked()
            (self.leftChanged if side == "left" else self.rightChanged).emit(key)
        else:
            self._refresh_checked()

    def _refresh_checked(self) -> None:
        for (side, key), btn in self._buttons.items():
            btn.setChecked(key == self._current[side])
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802, ARG002
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            pal = palette()
            panel_bg = QColor(pal.shell_bg)
            tab_bg = QColor(pal.card_bg)
            border = QPen(QColor(pal.input_border), 1)

            content = QRectF(self._content.geometry()).adjusted(0.5, 0.5, -0.5, -0.5)
            painter.setPen(border)
            painter.setBrush(panel_bg)
            painter.drawRoundedRect(content, self._RADIUS, self._RADIUS)

            for (side, key), btn in self._buttons.items():
                selected = key == self._current[side]
                # 未选中 tab 底部收 1px，保住其下方的内容区顶边线
                rect = QRectF(btn.geometry()).adjusted(
                    0.5, 0.5, -0.5, 0.0 if selected else -1.0
                )
                radius = self._TAB_RADIUS
                path = QPainterPath()
                path.moveTo(rect.left(), rect.bottom())
                path.lineTo(rect.left(), rect.top() + radius)
                path.quadTo(rect.left(), rect.top(), rect.left() + radius, rect.top())
                path.lineTo(rect.right() - radius, rect.top())
                path.quadTo(rect.right(), rect.top(), rect.right(), rect.top() + radius)
                path.lineTo(rect.right(), rect.bottom())
                painter.setPen(border)
                painter.setBrush(panel_bg if selected else tab_bg)
                painter.drawPath(path)
                if selected:
                    # 抹掉选中 tab 底下那段内容区顶边线，灰底连成一片
                    painter.fillRect(
                        QRectF(
                            rect.left() + 0.5,
                            rect.bottom() - 0.5,
                            rect.width() - 1.0,
                            2.0,
                        ),
                        panel_bg,
                    )
        finally:
            painter.end()


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


class GradientStopsEditor(QWidget):
    """Compact gradient stop editor for horizontal/vertical PaintFill gradients."""

    stopsChanged = Signal(list)
    selectedChanged = Signal(int)

    _POINTER_BLUE = "#0B84FF"
    _POINTER_OUTLINE = "#46505F"
    _POINTER_GAP = 3
    _POINTER_ARROW_LENGTH = 6
    _POINTER_BODY_LENGTH = 18
    _POINTER_HALF_THICKNESS = 5

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._stops: list[tuple[float, str]] = [(0, "#FFFFFF"), (100, "#FF5A6F")]
        self._selected = 0
        self._orientation = "horizontal"
        self._hard_edges = False
        self._dragging = False
        self.setMinimumHeight(52)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def sizeHint(self) -> QSize:  # noqa: N802
        height = 140 if self._orientation == "vertical" else 52
        if self._orientation == "vertical":
            # 12 left + 48 bar + 3 gap + 6 arrow + 18 tag + 4 right.
            # Every color bar uses the same external pointer geometry.
            return QSize(
                12
                + 48
                + self._POINTER_GAP
                + self._POINTER_ARROW_LENGTH
                + self._POINTER_BODY_LENGTH
                + 4,
                height,
            )
        return QSize(220, height)

    @property
    def selected_index(self) -> int:
        return self._selected

    @property
    def selected_stop(self) -> tuple[float, str]:
        return self._stops[self._selected]

    def set_orientation(self, mode: str) -> None:
        orientation = (
            "vertical"
            if mode in {"gradient_vertical", "split_vertical"}
            else "horizontal"
        )
        hard_edges = mode == "split_vertical"
        if orientation == self._orientation and hard_edges == self._hard_edges:
            return
        self._orientation = orientation
        self._hard_edges = hard_edges
        self.setMinimumHeight(132 if orientation == "vertical" else 52)
        self.updateGeometry()
        self.update()

    def set_stops(self, stops: list[tuple[float, str]]) -> None:
        selected_position = self._stops[self._selected][0] if self._stops else 0
        self._stops = _normalize_gradient_stops(stops)
        self._selected = min(
            range(len(self._stops)),
            key=lambda index: abs(self._stops[index][0] - selected_position),
        )
        self.update()
        self.selectedChanged.emit(self._selected)

    def set_selected_color(self, color: str) -> None:
        position, old = self._stops[self._selected]
        normalized = _normalize_hex(color, old)
        self._stops[self._selected] = (position, normalized)
        if self._hard_edges and position == 100 and self._selected > 0:
            previous_position, _previous_color = self._stops[self._selected - 1]
            self._stops[self._selected - 1] = (previous_position, normalized)
        self._emit_stops_changed()

    def set_selected_position(self, position: float) -> None:
        self._move_selected_stop(position)

    def add_stop(self, position: float, color: Optional[str] = None) -> None:
        pos = _normalized_stop_position(position)
        color = _normalize_hex(color or self._interpolated_color(pos))
        self._stops.append((pos, color))
        self._stops = _normalize_gradient_stops(self._stops)
        self._selected = self._index_for_position(pos)
        self._emit_stops_changed()

    def delete_selected_stop(self) -> None:
        if len(self._stops) <= 2:
            return
        position, _color = self._stops[self._selected]
        if position in {0, 100}:
            return
        del self._stops[self._selected]
        self._selected = max(0, min(self._selected, len(self._stops) - 1))
        self._emit_stops_changed()

    def paintEvent(self, event) -> None:  # noqa: N802, ARG002
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            rect = self._bar_rect()
            painter.setPen(QColor(palette().card_border))
            if self._hard_edges:
                # Clip all bands to the same rounded outline used by gradients.
                clip = QPainterPath()
                clip.addRoundedRect(rect, 4, 4)
                painter.save()
                painter.setClipPath(clip)
                for index, (position, color) in enumerate(self._stops[:-1]):
                    next_position = self._stops[index + 1][0]
                    top = rect.top() + rect.height() * position / 100.0
                    bottom = rect.top() + rect.height() * next_position / 100.0
                    painter.fillRect(
                        QRectF(rect.left(), top, rect.width(), max(bottom - top, 0.0)),
                        QColor(color),
                    )
                painter.restore()
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRoundedRect(rect, 4, 4)
            else:
                gradient = (
                    QLinearGradient(rect.left(), rect.top(), rect.right(), rect.top())
                    if self._orientation == "horizontal"
                    else QLinearGradient(rect.left(), rect.top(), rect.left(), rect.bottom())
                )
                for position, color in self._stops:
                    gradient.setColorAt(position / 100.0, QColor(color))
                painter.setBrush(gradient)
                painter.drawRoundedRect(rect, 4, 4)

            # Draw the selected pointer last so clustered stops never cover it.
            indices = [
                index for index in range(len(self._stops)) if index != self._selected
            ]
            indices.append(self._selected)
            for index in indices:
                position, _color = self._stops[index]
                selected = index == self._selected
                marker = self._marker_polygon(position, selected=selected)
                painter.setBrush(
                    QColor(self._POINTER_BLUE if selected else palette().input_bg)
                )
                painter.setPen(
                    QPen(QColor(self._POINTER_OUTLINE), 1.5)
                )
                painter.drawPolygon(marker)
        finally:
            painter.end()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            return
        pos = self._position_from_point(event.position())
        nearest = self._nearest_marker_index(event.position())
        self._dragging = False
        hit_rect = self._bar_rect().adjusted(-8, -8, 8, 8).united(
            self._pointer_lane_rect().adjusted(-10, -10, 10, 10)
        )
        if nearest is not None:
            self._selected = nearest
            self.selectedChanged.emit(self._selected)
            self.update()
            self._dragging = True
        elif hit_rect.contains(event.position()):
            self.add_stop(pos)
            self._dragging = True

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if not self._dragging:
            return
        self._move_selected_stop(self._position_from_point(event.position()))

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802, ARG002
        self._dragging = False

    def contextMenuEvent(self, event) -> None:  # noqa: N802
        if self._hard_edges:
            super().contextMenuEvent(event)
            return
        menu = RoundMenu(parent=self)
        copy_action = Action("复制渐变信息", menu)
        copy_action.triggered.connect(self.copy_gradient_info)
        menu.addAction(copy_action)
        paste_action = Action("粘贴渐变信息…", menu)
        paste_action.triggered.connect(self.paste_gradient_info)
        menu.addAction(paste_action)
        menu.exec(event.globalPos())
        event.accept()

    def copy_gradient_info(self) -> str:
        text = _gradient_stops_to_json(self._stops)
        QApplication.clipboard().setText(text)
        return text

    def paste_gradient_info(self) -> bool:
        dialog = _GradientStopsPasteDialog(
            QApplication.clipboard().text(),
            parent=self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return False
        self.set_stops(dialog.stops())
        self.stopsChanged.emit(list(self._stops))
        return True

    def _bar_rect(self) -> QRectF:
        if self._orientation == "horizontal":
            return QRectF(15, 8, max(self.width() - 30, 1), 22)
        return QRectF(12, 15, 48, max(self.height() - 30, 1))

    def _pointer_lane_rect(self) -> QRectF:
        if self._orientation == "horizontal":
            top = self._bar_rect().bottom() + self._POINTER_GAP + self._POINTER_ARROW_LENGTH
            return QRectF(
                15,
                top,
                max(self.width() - 30, 1),
                self._POINTER_HALF_THICKNESS * 2,
            )
        left = self._bar_rect().right() + self._POINTER_GAP + self._POINTER_ARROW_LENGTH
        return QRectF(
            left,
            15,
            self._POINTER_BODY_LENGTH,
            max(self.height() - 30, 1),
        )

    def _marker_center(self, position: float) -> QPointF:
        pos = max(0.0, min(1.0, position / 100.0))
        if self._orientation == "horizontal":
            lane = self._pointer_lane_rect()
            return QPointF(lane.left() + lane.width() * pos, lane.center().y())
        lane = self._pointer_lane_rect()
        return QPointF(lane.center().x(), lane.top() + lane.height() * pos)

    def _marker_tip(self, position: float) -> QPointF:
        pos = max(0.0, min(1.0, position / 100.0))
        bar = self._bar_rect()
        if self._orientation == "horizontal":
            return QPointF(
                bar.left() + bar.width() * pos,
                bar.bottom() + self._POINTER_GAP,
            )
        return QPointF(
            bar.right() + self._POINTER_GAP,
            bar.top() + bar.height() * pos,
        )

    def _marker_polygon(self, position: float, *, selected: bool) -> QPolygonF:
        """Return an external pointer aimed at the exact stop position."""
        tip = self._marker_tip(position)
        del selected
        half_thickness = self._POINTER_HALF_THICKNESS
        if self._orientation == "horizontal":
            body_top = tip.y() + self._POINTER_ARROW_LENGTH
            body_bottom = self._pointer_lane_rect().bottom()
            points = [
                tip,
                QPointF(tip.x() - half_thickness, body_top),
                QPointF(tip.x() - half_thickness, body_bottom),
                QPointF(tip.x() + half_thickness, body_bottom),
                QPointF(tip.x() + half_thickness, body_top),
            ]
        else:
            body_left = tip.x() + self._POINTER_ARROW_LENGTH
            body_right = self._pointer_lane_rect().right()
            points = [
                tip,
                QPointF(body_left, tip.y() - half_thickness),
                QPointF(body_right, tip.y() - half_thickness),
                QPointF(body_right, tip.y() + half_thickness),
                QPointF(body_left, tip.y() + half_thickness),
            ]
        return QPolygonF(points)

    def _position_from_point(self, point: QPointF) -> float:
        if self._orientation == "horizontal":
            rect = self._bar_rect()
            ratio = (point.x() - rect.left()) / max(rect.width(), 1.0)
        else:
            rect = self._bar_rect()
            ratio = (point.y() - rect.top()) / max(rect.height(), 1.0)
        return _normalized_stop_position(round(ratio * 100, 3))

    def _nearest_stop_index(self, position: float) -> Optional[int]:
        if not self._stops:
            return None
        return min(range(len(self._stops)), key=lambda index: abs(self._stops[index][0] - position))

    def _nearest_marker_index(self, point: QPointF) -> Optional[int]:
        if not self._stops:
            return None
        containing = [
            index
            for index, (position, _color) in enumerate(self._stops)
            if self._marker_polygon(
                position,
                selected=index == self._selected,
            ).containsPoint(point, Qt.FillRule.WindingFill)
        ]
        candidates = containing or list(range(len(self._stops)))
        nearest = min(
            candidates,
            key=lambda index: (
                self._marker_center(self._stops[index][0]).x() - point.x()
            )
            ** 2
            + (
                self._marker_center(self._stops[index][0]).y() - point.y()
            )
            ** 2,
        )
        center = self._marker_center(self._stops[nearest][0])
        distance_sq = (center.x() - point.x()) ** 2 + (center.y() - point.y()) ** 2
        return nearest if containing or distance_sq <= 20**2 else None

    def _index_for_position(self, position: float) -> int:
        return min(range(len(self._stops)), key=lambda index: abs(self._stops[index][0] - position))

    def _move_selected_stop(self, position: float) -> None:
        old_position, color = self._stops[self._selected]
        pos = _normalized_stop_position(position)
        if old_position in {0, 100}:
            if pos == old_position:
                return
            self._stops.append((pos, color))
            moved_index = len(self._stops) - 1
        else:
            self._stops[self._selected] = (pos, color)
            moved_index = self._selected
        # The persisted model retains equal-position stops, but an explicit UI
        # drag onto an existing marker means "merge" (the moved marker wins).
        self._stops = [
            stop
            for index, stop in enumerate(self._stops)
            if stop[0] != pos or index == moved_index
        ]
        self._stops = _normalize_gradient_stops(self._stops)
        self._selected = self._index_for_position(pos)
        self._emit_stops_changed()

    def _interpolated_color(self, position: float) -> str:
        stops = _normalize_gradient_stops(self._stops)
        pos = _normalized_stop_position(position)
        left = stops[0]
        right = stops[-1]
        for index, stop in enumerate(stops):
            if stop[0] <= pos:
                left = stop
            if stop[0] >= pos:
                right = stop
                break
            if index == len(stops) - 1:
                right = stop
        if self._hard_edges or left[0] == right[0]:
            return left[1]
        ratio = (pos - left[0]) / max(right[0] - left[0], 1e-9)
        a = QColor(left[1])
        b = QColor(right[1])
        return QColor(
            round(a.red() + (b.red() - a.red()) * ratio),
            round(a.green() + (b.green() - a.green()) * ratio),
            round(a.blue() + (b.blue() - a.blue()) * ratio),
            round(a.alpha() + (b.alpha() - a.alpha()) * ratio),
        ).name(QColor.NameFormat.HexArgb).upper()

    def _emit_stops_changed(self) -> None:
        self.update()
        self.selectedChanged.emit(self._selected)
        self.stopsChanged.emit(list(self._stops))


class _GradientStopsPasteDialog(QDialog):
    """Import portable gradient-stop JSON into the current gradient bar."""

    def __init__(self, text: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent.window() if parent is not None else None)
        self.setWindowTitle("粘贴渐变信息")
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setMinimumSize(520, 360)
        self._stops: list[tuple[float, str]] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(10)

        hint = CaptionLabel(
            "粘贴 Karaoke Studio 渐变关键点 JSON。应用后仅替换当前渐变条的颜色和位置。",
            self,
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.text_edit = FluentPlainTextEdit(self)
        self.text_edit.setPlaceholderText("在此粘贴渐变信息…")
        self.text_edit.setPlainText(text)
        layout.addWidget(self.text_edit, 1)

        self.error_label = CaptionLabel("", self)
        self.error_label.setWordWrap(True)
        themed(self.error_label, lambda: "color: #D13438;")
        layout.addWidget(self.error_label)

        button_row, self.apply_button, _cancel_button = fluent_button_row(
            self, ok_text="应用", cancel_text="取消"
        )
        layout.addLayout(button_row)
        self.text_edit.textChanged.connect(self._validate)
        self._validate()

    def stops(self) -> list[tuple[float, str]]:
        return list(self._stops)

    def _validate(self) -> bool:
        try:
            self._stops = _gradient_stops_from_json(self.text_edit.toPlainText())
        except ValueError as exc:
            self._stops = []
            self.error_label.setText(str(exc))
            self.error_label.show()
            self.apply_button.setEnabled(False)
            return False
        self.error_label.clear()
        self.error_label.hide()
        self.apply_button.setEnabled(True)
        return True


class _UnitProtectedSpinBoxMixin:
    """Keep spin-box prefixes and suffixes outside the editable selection."""

    def _install_debounced_keyboard_commit(self) -> None:
        """Coalesce direct numeric typing before emitting ``valueChanged``.

        Property changes rebuild the subtitle preview and update undo/settings
        state.  With Qt's default keyboard tracking that work runs after every
        digit, which makes the editor itself visibly pause.  Keep wheel/step
        changes immediate, but commit direct text entry after the same short
        idle window used by the title editor (or immediately on focus loss).

        Only complete, in-range text is committed, and the typed string is
        never rewritten while the caret is still in the field — see
        ``_commit_keyboard_edit``.
        """
        self.setKeyboardTracking(False)
        self._keyboard_commit_pending = False
        self._keyboard_commit_timer = QTimer(self)
        self._keyboard_commit_timer.setSingleShot(True)
        self._keyboard_commit_timer.setInterval(150)
        self._keyboard_commit_timer.timeout.connect(self._commit_keyboard_edit)
        self.lineEdit().textEdited.connect(self._queue_keyboard_commit)
        self.editingFinished.connect(self._flush_keyboard_edit)

    def _queue_keyboard_commit(self, _text: str) -> None:
        self._keyboard_commit_pending = True
        self._keyboard_commit_timer.start()

    def _commit_keyboard_edit(self) -> None:
        if not self._keyboard_commit_pending:
            return
        editor = self.lineEdit()
        text = editor.text()
        state, _fixed, _pos = self.validate(text, editor.cursorPosition())
        if state != QValidator.State.Acceptable:
            # 半成品文本（清空、只剩单位、暂时越界的 "2" → "20"）不能提交：
            # interpretText 对不可接受的文本会把上一个值写回输入框，正在输入
            # 的用户会被打断——删一位数字后旧值自己长回来正是这么来的。
            # 留给下一次按键或失焦时（由 Qt 自己按常规规则收敛）处理。
            return
        self._keyboard_commit_timer.stop()
        self._keyboard_commit_pending = False
        cursor = editor.cursorPosition()
        selection_start = editor.selectionStart()
        selection_length = len(editor.selectedText())
        self.interpretText()
        if editor.text() != text:
            # 值已提交，但 interpretText 顺手把文本规范化了（"007" → "7"、
            # "12.5" → "12.50"）。编辑途中不能改用户正在敲的字符串，原样还原。
            self._restore_editor_text(text, cursor, selection_start, selection_length)

    def _flush_keyboard_edit(self) -> None:
        """Editing ended: let Qt's own interpretation stand and drop the debounce.

        Focus loss and Enter already run ``interpret()`` inside Qt, including
        the out-of-range clamping and text normalisation that must not happen
        while the field still has the caret.
        """
        self._keyboard_commit_timer.stop()
        self._keyboard_commit_pending = False
        self.interpretText()

    def _restore_editor_text(
        self,
        text: str,
        cursor: int,
        selection_start: int,
        selection_length: int,
    ) -> None:
        editor = self.lineEdit()
        self._protecting_unit_selection = True
        try:
            editor.setText(text)
            if selection_start >= 0 and selection_length > 0:
                editor.setSelection(selection_start, selection_length)
            else:
                editor.setCursorPosition(min(cursor, len(text)))
        finally:
            self._protecting_unit_selection = False

    def _install_unit_selection_guard(self) -> None:
        self._protecting_unit_selection = False
        editor = self.lineEdit()
        editor.selectionChanged.connect(self._keep_units_out_of_selection)
        editor.cursorPositionChanged.connect(self._keep_cursor_out_of_units)

    def _editable_text_bounds(self) -> tuple[int, int]:
        editor_text = self.lineEdit().text()
        prefix = self.prefix()
        suffix = self.suffix()
        start = len(prefix) if prefix and editor_text.startswith(prefix) else 0
        end = (
            len(editor_text) - len(suffix)
            if suffix and editor_text.endswith(suffix)
            else len(editor_text)
        )
        return start, max(start, end)

    def _keep_units_out_of_selection(self) -> None:
        if self._protecting_unit_selection:
            return
        editor = self.lineEdit()
        selection_start = editor.selectionStart()
        if selection_start < 0:
            return
        selection_end = selection_start + len(editor.selectedText())
        value_start, value_end = self._editable_text_bounds()
        protected_start = max(selection_start, value_start)
        protected_end = min(selection_end, value_end)
        if (
            protected_start == selection_start
            and protected_end == selection_end
        ):
            return

        self._protecting_unit_selection = True
        try:
            if protected_start >= protected_end:
                editor.deselect()
                editor.setCursorPosition(
                    value_start if selection_end <= value_start else value_end
                )
            elif editor.cursorPosition() <= selection_start:
                # Preserve the anchor/cursor direction for a backwards drag.
                editor.setSelection(
                    protected_end, protected_start - protected_end
                )
            else:
                editor.setSelection(
                    protected_start, protected_end - protected_start
                )
        finally:
            self._protecting_unit_selection = False

    def _keep_cursor_out_of_units(self, _old: int, current: int) -> None:
        if self._protecting_unit_selection:
            return
        editor = self.lineEdit()
        if editor.hasSelectedText():
            return
        value_start, value_end = self._editable_text_bounds()
        protected_position = min(max(current, value_start), value_end)
        if protected_position == current:
            return
        self._protecting_unit_selection = True
        try:
            editor.setCursorPosition(protected_position)
        finally:
            self._protecting_unit_selection = False


class _WheelFocusedSpinBox(_UnitProtectedSpinBoxMixin, FluentSpinBox):
    """Direct-entry Fluent spin box without overlapping step controls."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        # InlineSpinBox's two permanent arrow buttons consume most of a narrow
        # two-column field. Hide them entirely: click focuses the line edit, while
        # focused wheel input remains available without any popup/flyout.
        self.setSymbolVisible(False)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.lineEdit().setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.lineEdit().textChanged.connect(self._sync_text_minimum)
        self.valueChanged.connect(
            lambda _value: QTimer.singleShot(0, self._sync_text_minimum)
        )
        self._install_debounced_keyboard_commit()
        self._install_unit_selection_guard()

    def showEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().showEvent(event)
        # The final application font may only be resolved when the page is shown.
        self._sync_text_minimum()

    def _sync_text_minimum(self) -> None:
        """Derive a DPI-aware minimum that keeps the current value readable."""
        style = self.style()
        text_width = self.lineEdit().fontMetrics().horizontalAdvance(
            self.lineEdit().text()
        )
        text_chrome = (
            2 * style.pixelMetric(QStyle.PixelMetric.PM_LayoutLeftMargin)
            + 2 * style.pixelMetric(QStyle.PixelMetric.PM_FocusFrameHMargin)
        )
        self.setMinimumWidth(max(text_width + text_chrome, 1))

    def wheelEvent(self, event):  # noqa: N802 - Qt API
        if not (self.hasFocus() or self.lineEdit().hasFocus()):
            event.ignore()
            return
        delta = event.angleDelta().y()
        if event.inverted():
            delta = -delta
        if delta:
            steps = int(delta / 120) or (1 if delta > 0 else -1)
            self.stepBy(steps)
            event.accept()
            return
        super().wheelEvent(event)


class _WheelFocusedDoubleSpinBox(_UnitProtectedSpinBoxMixin, FluentDoubleSpinBox):
    """Floating-point counterpart used for exact gradient stop positions."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setSymbolVisible(False)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.lineEdit().setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.lineEdit().textChanged.connect(self._sync_text_minimum)
        self.valueChanged.connect(
            lambda _value: QTimer.singleShot(0, self._sync_text_minimum)
        )
        self._install_debounced_keyboard_commit()
        self._install_unit_selection_guard()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._sync_text_minimum()

    def _sync_text_minimum(self) -> None:
        style = self.style()
        text_width = self.lineEdit().fontMetrics().horizontalAdvance(
            self.lineEdit().text()
        )
        text_chrome = (
            2 * style.pixelMetric(QStyle.PixelMetric.PM_LayoutLeftMargin)
            + 2 * style.pixelMetric(QStyle.PixelMetric.PM_FocusFrameHMargin)
        )
        self.setMinimumWidth(max(text_width + text_chrome, 1))

    def wheelEvent(self, event):  # noqa: N802
        if not (self.hasFocus() or self.lineEdit().hasFocus()):
            event.ignore()
            return
        delta = event.angleDelta().y()
        if event.inverted():
            delta = -delta
        if delta:
            steps = int(delta / 120) or (1 if delta > 0 else -1)
            self.stepBy(steps)
            event.accept()
            return
        super().wheelEvent(event)


class _MillisPartSpinBox(_WheelFocusedSpinBox):
    """秒/毫秒复合框里的毫秒位：0–999，步进越界时向秒位进位 / 借位。

    没有进位的话滚轮会死在 999 或 0 上，用户得手工切到秒位再切回来。
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._carry_handler: Optional[Callable[[int, int], bool]] = None

    def set_carry_handler(self, handler: Callable[[int, int], bool]) -> None:
        self._carry_handler = handler

    def stepBy(self, steps: int) -> None:  # noqa: N802 - Qt API
        target = self.value() + steps
        if self._carry_handler is not None and not 0 <= target <= 999:
            carry = math.floor(target / 1000)
            # 秒位越界时 handler 返回 False，退回默认的原地钳值。
            if self._carry_handler(carry, target - carry * 1000):
                return
        super().stepBy(steps)


class _DurationSpin(QWidget):
    """秒 + 毫秒并排的时长输入；对外仍然是一个「毫秒整数」控件。

    ``value()`` / ``setValue()`` / ``valueChanged`` 全部以毫秒为单位，与被它
    替换掉的单个 ms spin 完全同构，所以模型层（``TitleOverlay`` 等）继续存
    整数毫秒，工程文件与 N3 导入的 10ms 粒度都不会被这层 UI 改写。

    毫秒位恒为 0–999，取值上下限由秒位动态收窄，因此任何可输入的组合都落在
    ``[minimum, maximum]`` 内——不需要事后钳值，也就不会在用户还在打字时把
    输入框里的文本改掉（见 ``_UnitProtectedSpinBoxMixin._commit_keyboard_edit``）。
    """

    valueChanged = Signal(int)

    def __init__(
        self,
        minimum: int,
        maximum: int,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        if minimum < 0:
            # 负值的符号该归秒位还是毫秒位没有直观答案（-0.3s = 0s + -300ms？），
            # 需要负偏移的字段请继续用单个 ms spin。
            raise ValueError("_DurationSpin 只支持非负范围")
        if maximum < minimum:
            raise ValueError("_DurationSpin 的 maximum 不能小于 minimum")
        self._minimum = int(minimum)
        self._maximum = int(maximum)
        self._value = self._minimum
        self._syncing = False

        # 焦点直接落到两个子框上；容器自身不参与 Tab 链。
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(_COMPACT_CONTROL_HEIGHT)

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)
        self.seconds_spin = _spin(self._minimum // 1000, self._maximum // 1000, suffix=" s")
        self.millis_spin = _spin(0, 999, suffix=" ms", cls=_MillisPartSpinBox)
        self.millis_spin.set_carry_handler(self._carry_millis)
        hint = "两格相加即为总时长（1 s + 230 ms = 1230 毫秒）；毫秒位满 1000 自动进位到秒。"
        self.seconds_spin.setToolTip(hint)
        self.millis_spin.setToolTip(hint)
        # 毫秒位固定三位数（"999 ms"），比秒位宽，按比例分配剩余空间。
        row.addWidget(self.seconds_spin, 3)
        row.addWidget(self.millis_spin, 4)

        self.seconds_spin.valueChanged.connect(self._on_part_changed)
        self.millis_spin.valueChanged.connect(self._on_part_changed)
        self._apply_split(self._value)

    # -------------------------------------------------------------- 对外接口

    def value(self) -> int:
        return self._value

    def setValue(self, value: int) -> None:  # noqa: N802 - QSpinBox API
        clamped = self._clamp(value)
        changed = clamped != self._value
        self._value = clamped
        self._apply_split(clamped)
        if changed:
            # 与 QSpinBox.setValue 一致：值真的变了才发信号。
            self.valueChanged.emit(clamped)

    def minimum(self) -> int:
        return self._minimum

    def maximum(self) -> int:
        return self._maximum

    # ---------------------------------------------------------------- 内部实现

    def _clamp(self, value: Any) -> int:
        return int(max(self._minimum, min(self._maximum, int(value))))

    def _apply_split(self, value: int) -> None:
        seconds, millis = divmod(value, 1000)
        self._syncing = True
        try:
            self.seconds_spin.setValue(seconds)
            self._sync_millis_range()
            self.millis_spin.setValue(millis)
        finally:
            self._syncing = False

    def _sync_millis_range(self) -> None:
        """按当前秒位收窄毫秒位的取值范围，让越界组合根本敲不出来。"""
        base = self.seconds_spin.value() * 1000
        self.millis_spin.setMaximum(max(0, min(999, self._maximum - base)))
        self.millis_spin.setMinimum(max(0, min(999, self._minimum - base)))

    def _carry_millis(self, carry: int, millis: int) -> bool:
        seconds = self.seconds_spin.value() + carry
        if not self.seconds_spin.minimum() <= seconds <= self.seconds_spin.maximum():
            return False
        if not self._minimum <= seconds * 1000 + millis <= self._maximum:
            return False
        self.setValue(seconds * 1000 + millis)
        return True

    def _on_part_changed(self, _value: int) -> None:
        if self._syncing:
            return
        # 秒位变了 → 毫秒位的可用区间跟着变；这一步可能反过来钳住毫秒位并
        # 递归回到本函数，递归里已经发过信号，外层因为值相等不会重复发。
        self._sync_millis_range()
        value = self._clamp(self.seconds_spin.value() * 1000 + self.millis_spin.value())
        if value == self._value:
            return
        self._value = value
        self.valueChanged.emit(value)


class _WheelFocusedComboBox(FluentComboBox):
    """Avoid accidental option changes while scrolling the property panel."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def addItem(self, text: str, userData=None) -> None:  # noqa: N802 - Qt API
        """Keep QComboBox's positional ``userData`` convention."""
        super().addItem(text, userData=userData)

    def wheelEvent(self, event):  # noqa: N802 - Qt API
        if not self.hasFocus():
            event.ignore()
            return
        super().wheelEvent(event)


class _WheelFocusedFontComboBox(_WheelFocusedComboBox):
    """Fluent font picker preserving QFontComboBox's small public contract."""

    currentFontChanged = Signal(QFont)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._inheritance_label: Optional[str] = None
        self.addItems(n3_font_families())
        self.currentIndexChanged.connect(
            lambda _index: self.currentFontChanged.emit(self.currentFont())
        )

    def enable_inheritance(self, label: str) -> None:
        """Add an explicit N3-style zero slot before installed families."""
        if self._inheritance_label is not None:
            return
        self._inheritance_label = str(label)
        self.insertItem(0, self._inheritance_label, 0)

    def is_inherited(self) -> bool:
        return self._inheritance_label is not None and self.currentIndex() == 0

    def setInherited(self) -> None:  # noqa: N802 - Qt-style helper
        if self._inheritance_label is not None:
            self.setCurrentIndex(0)

    def currentFont(self) -> QFont:  # noqa: N802 - QFontComboBox compatibility
        return QFont(self.currentText())

    def setCurrentFont(self, font: QFont) -> None:  # noqa: N802
        family = canonicalize_n3_font_family(font.family())
        index = self.findText(family) if family is not None else -1
        if index < 0:
            if self._inheritance_label is not None:
                self.setInherited()
            return
        if index == self.currentIndex():
            self.currentFontChanged.emit(self.currentFont())
            return
        self.setCurrentIndex(index)


class _GrowingPlainTextEdit(FluentPlainTextEdit):
    """多行文本框：随内容行数自动增高，背景卡片随之变高（回车即变高）。"""

    editingFinished = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setLineWrapMode(FluentPlainTextEdit.LineWrapMode.WidgetWidth)
        self.textChanged.connect(self._adjust_height)
        self._adjust_height()

    def _adjust_height(self) -> None:
        # 按段落数（回车数 + 1）× 行高估算，不依赖控件是否可见 / 已布局。
        blocks = max(1, self.document().blockCount())
        line_height = self.fontMetrics().lineSpacing()
        frame = int(self.frameWidth()) * 2
        margins = self.contentsMargins()
        doc_margin = int(self.document().documentMargin()) * 2
        height = blocks * line_height + frame + margins.top() + margins.bottom() + doc_margin + 4
        self.setFixedHeight(max(32, height))

    def wheelEvent(self, event):  # noqa: N802 - Qt API
        if not self.hasFocus():
            event.ignore()
            return
        super().wheelEvent(event)

    def focusOutEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().focusOutEvent(event)
        self.editingFinished.emit()


class _DynamicStackedWidget(QStackedWidget):
    """Use the current page height instead of the tallest page height."""

    def sizeHint(self) -> QSize:  # noqa: N802
        widget = self.currentWidget()
        return widget.sizeHint() if widget is not None else super().sizeHint()

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        widget = self.currentWidget()
        return widget.minimumSizeHint() if widget is not None else super().minimumSizeHint()


class _StylePresetDetailsDialog(QDialog):
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
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
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


class _RolePresetGroupDialog(QDialog):
    """Resolve cross-group preset-name collisions one imported role at a time."""

    def __init__(
        self,
        candidates: dict[str, list[StylePreset]],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent.window() if parent is not None else None)
        self.setWindowTitle("选择角色预设分组")
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
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


class StylePresetManagerDialog(QDialog):
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
        self._preset_list.itemChanged.connect(lambda _item: self._sync_buttons())
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
            scheme=deepcopy(self._current_scheme),
            preset_id=preset_id,
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
        self._applied_scheme = deepcopy(self._presets[preset_id].scheme)
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
            preset_id: deepcopy(self._presets[preset_id])
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


class _GlyphToggleButton(QToolButton):
    """Self-painted icon toggle: glyph colors always follow the live palette,
    so no pixmap regeneration is needed on theme switch."""

    def __init__(self, glyph: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._glyph = glyph
        self.setCheckable(True)
        self.setAutoExclusive(True)
        self.setFixedSize(38, 30)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def enterEvent(self, event: Any) -> None:
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event: Any) -> None:
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event: Any) -> None:
        p = palette()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        if self.isChecked():
            bg = QColor(p.accent_primary)
            border = QColor(p.accent_primary)
            fg = QColor("#FFFFFF")
        elif self.underMouse():
            bg = QColor(p.secondary_button_hover_bg)
            border = QColor(p.secondary_button_hover_border)
            fg = QColor(p.text_secondary)
        else:
            bg = QColor(p.secondary_button_bg)
            border = QColor(p.secondary_button_border)
            fg = QColor(p.text_secondary)
        painter.setPen(QPen(border, 1))
        painter.setBrush(bg)
        painter.drawRoundedRect(rect, 6, 6)
        self._draw_glyph(painter, fg)
        painter.end()

    def _draw_glyph(self, painter: QPainter, color: QColor) -> None:
        inner = QRectF(self.rect()).adjusted(11, 9, -11, -9)
        pen = QPen(color, 2)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        if self._glyph.startswith("align_"):
            # 三条横线，按左/中/右对齐——文本对齐的通用隐喻
            painter.setPen(pen)
            widths = (1.0, 0.6, 0.85)
            for index, ratio in enumerate(widths):
                y = inner.top() + inner.height() * index / (len(widths) - 1)
                line_w = inner.width() * ratio
                if self._glyph == "align_left":
                    x = inner.left()
                elif self._glyph == "align_right":
                    x = inner.right() - line_w
                else:
                    x = inner.center().x() - line_w / 2
                painter.drawLine(QPointF(x, y), QPointF(x + line_w, y))
        else:
            # pos_top / pos_middle / pos_bottom：屏幕框 + 一条粗线标出位置
            frame_pen = QPen(color, 1.2)
            painter.setPen(frame_pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            frame = inner.adjusted(-2, -2, 2, 2)
            painter.drawRoundedRect(frame, 2, 2)
            painter.setPen(pen)
            if self._glyph == "pos_top":
                y = inner.top() + 1
            elif self._glyph == "pos_middle":
                y = inner.center().y()
            else:
                y = inner.bottom() - 1
            painter.drawLine(
                QPointF(inner.left() + 1, y), QPointF(inner.right() - 1, y)
            )


_ALIGN_SEGMENT_OPTIONS: tuple[tuple[str, str, str], ...] = (
    ("left", "align_left", "左对齐"),
    ("center", "align_center", "居中"),
    ("right", "align_right", "右对齐"),
)

_POSITION_SEGMENT_OPTIONS: tuple[tuple[str, str, str], ...] = (
    ("top", "pos_top", "顶部"),
    ("center", "pos_middle", "居中"),
    ("bottom", "pos_bottom", "底部"),
)


class _GlyphSegment(QWidget):
    """互斥图标按钮组（借鉴 N3 的对齐按钮）：三值枚举用下拉要点开才能看到
    选项，图标组当前值一眼可见、切换只要一次点击。

    ``setValue`` 在值变化时发射 ``valueChanged``，与 ComboBox 的
    ``setCurrentIndex`` 语义一致（面板同步路径靠 ``_syncing`` 防环）。
    """

    valueChanged = Signal(str)

    def __init__(
        self,
        options: tuple[tuple[str, str, str], ...],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self._buttons: dict[str, _GlyphToggleButton] = {}
        self._value = options[0][0]
        for value, glyph, tooltip in options:
            btn = _GlyphToggleButton(glyph, self)
            btn.setToolTip(tooltip)
            btn.setAccessibleName(tooltip)
            btn.clicked.connect(lambda _checked=False, v=value: self._on_clicked(v))
            layout.addWidget(btn, 0)
            self._buttons[value] = btn
        layout.addStretch(1)
        self._buttons[self._value].setChecked(True)

    def value(self) -> str:
        return self._value

    def setValue(self, value: str) -> None:  # noqa: N802 (Qt 风格)
        if value not in self._buttons or value == self._value:
            return
        self._value = value
        self._buttons[value].setChecked(True)
        self.valueChanged.emit(value)

    def _on_clicked(self, value: str) -> None:
        # autoExclusive 保证选中态互斥；重复点击已选中项不发信号
        if value == self._value:
            return
        self._value = value
        self.valueChanged.emit(value)


class _LayoutSchematic(QWidget):
    """布局示意图（借鉴 N3）：微缩屏幕 + 色条，不读数字也能看懂当前布局的
    行数、对齐、锚定和余白。跟随属性修改实时刷新。"""

    _DEFAULT_VIRTUAL_W = 1920.0
    _DEFAULT_VIRTUAL_H = 1080.0
    _DISPLAY_HEIGHT = 150
    # 色条宽度做些许长短变化，模拟真实歌词行（N3 同款处理）
    _BAR_RATIOS = (0.58, 0.46, 0.53, 0.42, 0.5, 0.44, 0.55, 0.47)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._state: dict = {}
        self._virtual_width = self._DEFAULT_VIRTUAL_W
        self._virtual_height = self._DEFAULT_VIRTUAL_H
        self.setFixedHeight(self._DISPLAY_HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_output_size(self, width: int, height: int) -> None:
        """Use the real output canvas for pixel mapping and screen aspect ratio."""
        width = int(width)
        height = int(height)
        if width <= 0 or height <= 0:
            return
        if width == self._virtual_width and height == self._virtual_height:
            return
        self._virtual_width = float(width)
        self._virtual_height = float(height)
        self.setFixedWidth(round(self._DISPLAY_HEIGHT * width / height))
        self.updateGeometry()
        self.update()

    def set_state(self, **state: Any) -> None:
        if state != self._state:
            self._state = state
            self.update()

    def _bar_specs(self) -> list[tuple[str, float, float]]:
        """Return ``(align, offset_x, offset_y)`` per displayed row."""
        state = self._state
        mode = state.get("mode", "asymmetric")
        alignments = list(state.get("alignments") or ["left"])
        if not state.get("dual_line", True):
            alignments = alignments[:1]
        if mode == "per_row":
            return [
                (align, float(dx), float(dy))
                for align, dx, dy in state.get("rows", [("left", 0, 0)])
            ]
        if mode == "center":
            return [("center", 0.0, 0.0) for _ in alignments]
        return [(align, 0.0, 0.0) for align in alignments]

    def paintEvent(self, event: Any) -> None:
        state = self._state
        p = palette()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        scale = min(
            self.width() / self._virtual_width,
            self.height() / self._virtual_height,
        )
        screen_w = self._virtual_width * scale
        screen_h = self._virtual_height * scale
        screen = QRectF(
            (self.width() - screen_w) / 2,
            (self.height() - screen_h) / 2,
            screen_w,
            screen_h,
        )
        painter.setPen(QPen(QColor(p.card_border), 1))
        painter.setBrush(QColor("#0B0D12"))
        painter.drawRoundedRect(screen, 4, 4)
        if not state:
            painter.end()
            return
        painter.setClipRect(screen)

        font_px = max(20.0, min(200.0, float(state.get("font_px", 70))))
        gap = float(state.get("gap", 0))
        y_margin = float(state.get("y_margin", 0))
        h_margin = float(state.get("h_margin", 0))
        y_position = state.get("y_position", "bottom")
        specs = self._bar_specs()

        guide_color = QColor(p.text_secondary)
        guide_color.setAlphaF(0.55)
        guide_pen = QPen(guide_color, 1, Qt.PenStyle.DashLine)

        bar_color = QColor(p.accent_primary)

        if state.get("vertical"):
            self._paint_vertical(
                painter, screen, scale, specs, font_px, gap, y_margin, h_margin,
                y_position, guide_pen, bar_color,
            )
        else:
            self._paint_horizontal(
                painter, screen, scale, specs, font_px, gap, y_margin, h_margin,
                y_position, guide_pen, bar_color,
            )
        painter.end()

    def _paint_horizontal(
        self, painter, screen, scale, specs, font_px, gap, y_margin, h_margin,
        y_position, guide_pen, bar_color,
    ) -> None:
        count = len(specs)
        bar_h = font_px
        block_h = count * bar_h + (count - 1) * gap
        if y_position == "top":
            y0 = y_margin
        elif y_position == "center":
            y0 = (self._virtual_height - block_h) / 2
        else:
            y0 = self._virtual_height - y_margin - block_h

        painter.setPen(guide_pen)
        if h_margin > 0:
            for x in (h_margin, self._virtual_width - h_margin):
                painter.drawLine(
                    QPointF(screen.left() + x * scale, screen.top()),
                    QPointF(screen.left() + x * scale, screen.bottom()),
                )
        if y_margin > 0 and y_position != "center":
            guide_y = (
                y_margin if y_position == "top" else self._virtual_height - y_margin
            )
            painter.drawLine(
                QPointF(screen.left(), screen.top() + guide_y * scale),
                QPointF(screen.right(), screen.top() + guide_y * scale),
            )

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(bar_color)
        usable_w = max(100.0, self._virtual_width - 2 * h_margin)
        for index, (align, dx, dy) in enumerate(specs):
            bar_w = usable_w * self._BAR_RATIOS[index % len(self._BAR_RATIOS)]
            if align == "left":
                x = h_margin
            elif align == "right":
                x = self._virtual_width - h_margin - bar_w
            else:
                x = (self._virtual_width - bar_w) / 2
            y = y0 + index * (bar_h + gap)
            painter.drawRect(
                QRectF(
                    screen.left() + (x + dx) * scale,
                    screen.top() + (y + dy) * scale,
                    bar_w * scale,
                    bar_h * scale,
                )
            )

    def _paint_vertical(
        self, painter, screen, scale, specs, font_px, gap, y_margin, h_margin,
        y_position, guide_pen, bar_color,
    ) -> None:
        """竖排近似示意：行变成从右往左排的竖条，对齐映射到上/中/下。"""
        count = len(specs)
        col_w = font_px
        block_w = count * col_w + (count - 1) * gap
        if y_position == "top":
            x0 = y_margin
        elif y_position == "center":
            x0 = (self._virtual_width - block_w) / 2
        else:
            x0 = self._virtual_width - y_margin - block_w

        painter.setPen(guide_pen)
        if h_margin > 0:
            for y in (h_margin, self._virtual_height - h_margin):
                painter.drawLine(
                    QPointF(screen.left(), screen.top() + y * scale),
                    QPointF(screen.right(), screen.top() + y * scale),
                )

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(bar_color)
        usable_h = max(100.0, self._virtual_height - 2 * h_margin)
        for index, (align, dx, dy) in enumerate(specs):
            col_h = usable_h * self._BAR_RATIOS[index % len(self._BAR_RATIOS)]
            if align == "left":
                y = h_margin
            elif align == "right":
                y = self._virtual_height - h_margin - col_h
            else:
                y = (self._virtual_height - col_h) / 2
            # 第1行在最右（竖排从右往左读）
            x = x0 + (count - 1 - index) * (col_w + gap)
            painter.drawRect(
                QRectF(
                    screen.left() + (x + dx) * scale,
                    screen.top() + (y + dy) * scale,
                    col_w * scale,
                    col_h * scale,
                )
            )


class _SchematicBoard(QWidget):
    """N3 式空间编排：示意图居中，锚定贴左上，左右余白在左侧垂直
    居中，上/下余白贴下边，行布局贴右边。窄面板退化为竖向堆叠。"""

    def __init__(
        self,
        left: QWidget,
        center: QWidget,
        bottom: QWidget,
        right: QWidget,
        parent: Optional[QWidget] = None,
        *,
        header_left: Optional[QWidget] = None,
        header_right: Optional[QWidget] = None,
        top_left: Optional[QWidget] = None,
        top_center: Optional[QWidget] = None,
        bottom_left: Optional[QWidget] = None,
        bottom_right: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._left = left
        self._center = center
        self._bottom = bottom
        self._right = right
        self._header_left = header_left
        self._header_right = header_right
        self._top_left = top_left
        self._top_center = top_center
        self._bottom_left = bottom_left
        self._bottom_right = bottom_right
        self._wide: Optional[bool] = None
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setHorizontalSpacing(8)
        self._grid.setVerticalSpacing(8)
        for child in (
            left,
            center,
            bottom,
            right,
            header_left,
            header_right,
            top_left,
            top_center,
            bottom_left,
            bottom_right,
        ):
            if child is not None:
                child.setParent(self)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._sync(force=True)

    @staticmethod
    def _side_width(widget: QWidget) -> int:
        return max(widget.minimumSizeHint().width(), widget.sizeHint().width())

    def _wide_width_hint(self) -> int:
        # 中列现在通常是固定 16:9 幕布；断点必须使用它的真实宽度，
        # 否则 600px 左右的面板会误判为三列并导致行布局覆盖幕布。
        center_width = max(
            180,
            self._center.minimumWidth(),
            self._center.minimumSizeHint().width(),
            self._center.sizeHint().width(),
            self._side_width(self._top_center)
            if self._top_center is not None
            else 0,
        )
        return (
            max(
                self._side_width(self._left),
                self._side_width(self._top_left) if self._top_left is not None else 0,
            )
            + max(
                self._side_width(self._right),
                self._side_width(self._bottom_right)
                if self._bottom_right is not None
                else 0,
            )
            + center_width
            + self._grid.horizontalSpacing() * 2
        )

    def minimumSizeHint(self) -> QSize:
        # 只汇报竖向堆叠的最小宽：宽模式的三列行宽会卡住父级收窄，
        # 收窄不发生就永远不会切回堆叠（与 _ResponsiveFieldGrid 同款死锁）
        base = super().minimumSizeHint()
        width = max(
            self._left.minimumSizeHint().width(),
            self._right.minimumSizeHint().width(),
            self._header_left.minimumSizeHint().width()
            if self._header_left is not None
            else 0,
            self._header_right.minimumSizeHint().width()
            if self._header_right is not None
            else 0,
            self._bottom.minimumSizeHint().width(),
            self._center.minimumSizeHint().width(),
            self._top_left.minimumSizeHint().width()
            if self._top_left is not None
            else 0,
            self._top_center.minimumSizeHint().width()
            if self._top_center is not None
            else 0,
            self._bottom_left.minimumSizeHint().width()
            if self._bottom_left is not None
            else 0,
            self._bottom_right.minimumSizeHint().width()
            if self._bottom_right is not None
            else 0,
        )
        return QSize(width, base.height())

    def resizeEvent(self, event: Any) -> None:
        self._sync()
        super().resizeEvent(event)

    def _sync(self, *, force: bool = False) -> None:
        wide = self.width() >= self._wide_width_hint()
        if not force and wide == self._wide:
            return
        self._wide = wide
        while self._grid.count():
            self._grid.takeAt(0)
        if wide:
            if self._header_left is not None:
                self._grid.addWidget(
                    self._header_left,
                    0,
                    0,
                    1,
                    2,
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
                )
            if self._header_right is not None:
                self._grid.addWidget(
                    self._header_right,
                    0,
                    1,
                    1,
                    2,
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop,
                )
            self._grid.addWidget(
                self._left,
                1,
                0,
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            )
            if self._top_left is not None:
                self._grid.addWidget(
                    self._top_left,
                    1,
                    0,
                    Qt.AlignmentFlag.AlignTop,
                )
            if self._top_center is not None:
                self._grid.addWidget(
                    self._top_center,
                    0,
                    1,
                    Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom,
                )
            if self._bottom_left is not None:
                self._grid.addWidget(
                    self._bottom_left,
                    1,
                    0,
                    2,
                    1,
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom,
                )
            self._grid.addWidget(self._center, 1, 1)
            self._grid.addWidget(
                self._right,
                1,
                2,
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
            )
            self._grid.addWidget(
                self._bottom,
                2,
                1,
                Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
            )
            if self._bottom_right is not None:
                self._grid.addWidget(
                    self._bottom_right,
                    2,
                    2,
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                )
            # 幕布是固定 16:9 的紧凑中心列；两侧列均分剩余空间。
            # 左右余白在左列右对齐，因而紧贴幕布而不是贴中间列边界。
            self._grid.setColumnStretch(0, 1)
            self._grid.setColumnStretch(1, 0)
            self._grid.setColumnStretch(2, 1)
        else:
            next_row = 0
            if self._header_left is not None:
                self._grid.addWidget(
                    self._header_left,
                    next_row,
                    0,
                    Qt.AlignmentFlag.AlignLeft,
                )
                next_row += 1
            if self._header_right is not None:
                self._grid.addWidget(
                    self._header_right,
                    next_row,
                    0,
                    Qt.AlignmentFlag.AlignLeft,
                )
                next_row += 1
            if self._top_center is not None:
                self._grid.addWidget(
                    self._top_center, next_row, 0, Qt.AlignmentFlag.AlignHCenter
                )
                next_row += 1
            self._grid.addWidget(self._center, next_row, 0)
            next_row += 1
            self._grid.addWidget(
                self._bottom, next_row, 0, Qt.AlignmentFlag.AlignHCenter
            )
            next_row += 1
            if self._top_left is not None:
                self._grid.addWidget(self._top_left, next_row, 0)
                next_row += 1
            self._grid.addWidget(self._left, next_row, 0)
            next_row += 1
            if self._bottom_left is not None:
                self._grid.addWidget(self._bottom_left, next_row, 0)
                next_row += 1
            self._grid.addWidget(self._right, next_row, 0)
            next_row += 1
            if self._bottom_right is not None:
                self._grid.addWidget(self._bottom_right, next_row, 0)
            self._grid.setColumnStretch(0, 1)
            self._grid.setColumnStretch(1, 0)
            self._grid.setColumnStretch(2, 0)
        self.updateGeometry()
        # 换行后子控件的 sizeHint 可能改变；Qt 不一定会再派发一次
        # resizeEvent。下一轮事件再核对一次，消除断点附近的迟滞。
        QTimer.singleShot(0, self._sync)


class _ResponsivePropertyPair(QWidget):
    """Lay two property cards side by side when they genuinely fit.

    The property page intentionally has no horizontal scrollbar.  A regular
    ``QHBoxLayout`` therefore lets its children extend beyond the viewport
    when their combined preferred width no longer fits.  This container uses
    the cards' own size hints (font, DPI and translated labels included) as
    the breakpoint and stacks them instead of relying on a screen-pixel
    threshold.

    ``divider`` may be ``None``: section cards carry their own borders, so
    pairing two cards needs no separator line.  ``min_side_width`` raises the
    side-by-side breakpoint for children whose controls use ``Ignored``
    horizontal policy (their size hints are too small to be an honest
    breakpoint on their own).
    """

    def __init__(
        self, parent: Optional[QWidget] = None, *, min_side_width: int = 0
    ) -> None:
        super().__init__(parent)
        self._first: Optional[QWidget] = None
        self._divider: Optional[QFrame] = None
        self._second: Optional[QWidget] = None
        self._stacked: Optional[bool] = None
        self._min_side_width = min_side_width

        self._layout = QBoxLayout(QBoxLayout.Direction.LeftToRight, self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(12)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

    def set_widgets(
        self, first: QWidget, divider: Optional[QFrame], second: QWidget
    ) -> None:
        self._first = first
        self._divider = divider
        self._second = second
        self._layout.addWidget(first)
        if divider is not None:
            self._layout.addWidget(divider)
        self._layout.addWidget(second)
        # Equal horizontal stretch must not imply equal card heights.  Both
        # cards keep their own preferred height and simply share the same top.
        self._layout.setAlignment(first, Qt.AlignmentFlag.AlignTop)
        self._layout.setAlignment(second, Qt.AlignmentFlag.AlignTop)
        self._sync_direction(force=True)

    def is_stacked(self) -> bool:
        return bool(self._stacked)

    def _divider_width(self) -> int:
        if self._divider is None:
            return 0
        return max(1, self._divider.sizeHint().width())

    def _spacing_count(self) -> int:
        return 2 if self._divider is not None else 1

    def horizontal_width_hint(self) -> int:
        if self._first is None or self._second is None:
            return 0
        first_width = max(
            self._first.minimumSizeHint().width(), self._first.sizeHint().width()
        )
        second_width = max(
            self._second.minimumSizeHint().width(), self._second.sizeHint().width()
        )
        chrome = self._divider_width() + self._layout.spacing() * self._spacing_count()
        return max(
            first_width + second_width + chrome,
            self._min_side_width * 2 + chrome,
        )

    def minimumSizeHint(self) -> QSize:
        if self._first is None or self._second is None:
            return super().minimumSizeHint()
        width = max(
            self._first.minimumSizeHint().width(),
            self._second.minimumSizeHint().width(),
        )
        if self.is_stacked():
            divider_height = 1 if self._divider is not None else 0
            height = (
                self._first.minimumSizeHint().height()
                + self._second.minimumSizeHint().height()
                + divider_height
                + self._layout.spacing() * self._spacing_count()
            )
        else:
            height = max(
                self._first.minimumSizeHint().height(),
                self._second.minimumSizeHint().height(),
            )
        return QSize(width, height)

    def resizeEvent(self, event: Any) -> None:
        self._sync_direction()
        super().resizeEvent(event)

    def _sync_direction(self, *, force: bool = False) -> None:
        if self._first is None or self._second is None:
            return
        stacked = self.width() < self.horizontal_width_hint()
        if not force and stacked == self._stacked:
            return
        self._stacked = stacked

        if stacked:
            self._layout.setDirection(QBoxLayout.Direction.TopToBottom)
            self._layout.setStretchFactor(self._first, 0)
            self._layout.setStretchFactor(self._second, 0)
            if self._divider is not None:
                self._divider.setMinimumWidth(0)
                self._divider.setMaximumWidth(QWIDGETSIZE_MAX)
                self._divider.setFixedHeight(1)
                self._divider.setFrameShape(QFrame.Shape.HLine)
                self._divider.setSizePolicy(
                    QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
                )
        else:
            self._layout.setDirection(QBoxLayout.Direction.LeftToRight)
            self._layout.setStretchFactor(self._first, 1)
            self._layout.setStretchFactor(self._second, 1)
            if self._divider is not None:
                self._divider.setMinimumHeight(0)
                self._divider.setMaximumHeight(QWIDGETSIZE_MAX)
                self._divider.setFixedWidth(1)
                self._divider.setFrameShape(QFrame.Shape.VLine)
                self._divider.setSizePolicy(
                    QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding
                )
        self.updateGeometry()


class _ResponsiveFieldGrid(QWidget):
    """Reflow property fields into 1–N columns based on the available width.

    The fixed two-column ``QGridLayout`` blocks waste half a row whenever the
    panel is wide (the screenshot case) and still overflow when it is very
    narrow.  This container re-buckets its children on every resize:
    ``columns = clamp(width // min_column_width, 1, max_columns)``.
    """

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        *,
        min_column_width: int = 150,
        max_columns: int = 4,
    ) -> None:
        super().__init__(parent)
        self._min_column_width = max(1, min_column_width)
        self._max_columns = max(1, max_columns)
        self._items: list[QWidget] = []
        self._columns = 0
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setHorizontalSpacing(8)
        self._grid.setVerticalSpacing(8)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

    def add_field(self, label_text: str, control: QWidget) -> None:
        self.add_widget(_field(label_text, control))

    def add_widget(self, widget: QWidget) -> None:
        widget.setParent(self)
        self._items.append(widget)
        self._relayout(force=True)

    def clear(self) -> None:
        while self._grid.count():
            self._grid.takeAt(0)
        for widget in self._items:
            widget.deleteLater()
        self._items.clear()
        self._columns = 0

    def _target_columns(self) -> int:
        spacing = self._grid.horizontalSpacing()
        fit = (max(self.width(), 1) + spacing) // (self._min_column_width + spacing)
        return int(max(1, min(fit, self._max_columns, max(1, len(self._items)))))

    def minimumSizeHint(self) -> QSize:
        # 必须汇报「单列」最小宽：多列时网格自身的最小宽会阻止父级收窄，
        # 而收窄不发生 resizeEvent 就永远不会降列数（死锁）。
        base = super().minimumSizeHint()
        if not self._items:
            return base
        width = max(item.minimumSizeHint().width() for item in self._items)
        return QSize(width, base.height())

    def resizeEvent(self, event: Any) -> None:
        self._relayout()
        super().resizeEvent(event)

    def _relayout(self, *, force: bool = False) -> None:
        columns = self._target_columns()
        if not force and columns == self._columns:
            return
        previous = max(self._columns, columns)
        self._columns = columns
        while self._grid.count():
            self._grid.takeAt(0)
        for index, widget in enumerate(self._items):
            self._grid.addWidget(widget, index // columns, index % columns)
        for column in range(previous):
            self._grid.setColumnStretch(column, 1 if column < columns else 0)
        self.updateGeometry()


class PropertyPanel(QWidget):
    """字体 / 布局 / 特效 / 标题属性面板。"""

    _PAGE_SPECS = (
        ("font", "角色"),
        ("layout", "布局"),
        ("timing", "时间"),
        ("effects", "特效"),
        ("title", "标题"),
    )

    styleChanged = Signal(Style)
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

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._style = Style()
        self._syncing = False
        self._title_text_change_pending = False
        self._title_text_change_timer = QTimer(self)
        self._title_text_change_timer.setSingleShot(True)
        self._title_text_change_timer.setInterval(150)
        self._title_text_change_timer.timeout.connect(self._commit_title_text_edit)
        self._role_names: list[str] = []
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

        pages = (
            self._make_subtitle_page(),
            self._make_basic_page(),
            self._make_timing_page(),
            self._make_effects_page(),
            self._make_title_page(),
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
        for index, (candidate, _label) in enumerate(self._PAGE_SPECS):
            if candidate == route_key:
                self._stack.setCurrentIndex(index)
                return

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
        return list(self._role_names)

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
            self._line_lead_spin.setValue(self._style.line_lead_in_ms)
            self._line_tail_spin.setValue(self._style.line_tail_ms)
            self._line_offset_spin.setValue(self._style.timing_offset_ms)
            self._ruby_main_reading_units_check.setChecked(
                self._style.ruby_main_progress_mode == "reading_units"
            )
            self._section_gap_spin.setValue(self._style.section_gap_ms)
            self._lane_gap_spin.setValue(self._style.line_lane_gap_ms)
            self._section_ending_combo.setCurrentIndex(
                max(0, self._section_ending_combo.findData(self._style.section_ending_mode))
            )
            self._sync_entry_check.setChecked(self._style.sync_entry)
            self._sync_ending_check.setChecked(self._style.sync_ending)
            self._entry_anim_combo.setCurrentIndex(
                max(0, self._entry_anim_combo.findData(self._style.entry_anim))
            )
            self._entry_lead_spin.setValue(self._style.entry_lead_ms)
            self._exit_anim_combo.setCurrentIndex(
                max(0, self._exit_anim_combo.findData(self._style.exit_anim))
            )
            self._exit_fade_spin.setValue(self._style.exit_fade_ms)
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
        if emit:
            self.styleChanged.emit(self._style)

    def set_roles(self, role_names: list[str]) -> None:
        """Replace the current project's role registry and refresh navigation."""
        self._role_names = [str(n) for n in role_names if n]
        self._ensure_role_schemes()
        current_key = self._current_scheme_key()
        self._syncing = True
        try:
            self._refresh_scheme_combo(current_key)
        finally:
            self._syncing = False
        self._sync_subtitle_scheme_controls()

    def merge_roles(self, role_names: list[str]) -> None:
        """Add newly discovered roles without dropping earlier project roles."""
        merged = list(self._role_names)
        seen = set(merged)
        for value in role_names:
            name = str(value or "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            merged.append(name)
        self.set_roles(merged)

    # ------------------------------------------------------------------ layout

    def _make_basic_page(self) -> QWidget:
        scroll, layout = _scroll_page()
        # 布局方案、行结构与字符排版共享同一张无标题工作区卡片。
        layout.addWidget(self._make_row_structure_section())
        # 注音的字号/间距/排布是排版参数，归布局页（配色仍在字体页的颜色列）
        self._ruby_section = self._make_ruby_section()
        layout.addWidget(
            _section_pair(self._ruby_section, self._make_vertical_layout_section())
        )
        viewport = self._make_viewport_section()
        viewport.set_expanded(False)  # 本项目特有的整体变换，低频使用默认折叠
        layout.addWidget(viewport)
        layout.addStretch(1)
        return scroll

    def _make_timing_page(self) -> QWidget:
        scroll, layout = _scroll_page()
        layout.addWidget(self._make_timing_section())
        layout.addStretch(1)
        return scroll

    def _make_subtitle_page(self) -> QWidget:
        scroll, layout = _scroll_page()
        self._font_color_section = self._make_font_color_section()
        # 角色选择是颜色/字体的编辑上下文，放在同一卡片的
        # 顶部导航条，不再单独占一张可折叠卡片。
        self._role_section = self._font_color_section
        layout.addWidget(self._font_color_section)

        layout.addStretch(1)
        return scroll

    def _make_font_color_section(self) -> QFrame:
        section, layout = _plain_card()
        self._scheme_section = section
        layout.addWidget(
            self._make_scheme_navigation(section),
            0,
            Qt.AlignmentFlag.AlignLeft,
        )

        row = _ResponsivePropertyPair(section)
        self._font_color_row = row

        self._color_section = self._make_color_section(parent=row, inline=True)
        self._font_section = self._make_font_section(parent=row, inline=True)
        self._color_section.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred,
        )
        self._font_section.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred,
        )

        divider = QFrame(row)
        divider.setObjectName("SubtitlePropertyInnerDivider")
        divider.setFrameShape(QFrame.Shape.VLine)
        divider.setFixedWidth(1)
        divider.setSizePolicy(
            QSizePolicy.Policy.Fixed,
            QSizePolicy.Policy.Expanding,
        )
        themed(
            divider,
            lambda: (
                "QFrame#SubtitlePropertyInnerDivider { "
                f"background: {palette().card_border}; "
                "border: 0; "
                "}"
            ),
        )

        row.set_widgets(self._color_section, divider, self._font_section)
        layout.addWidget(row)
        return section

    def _make_font_section(
        self, parent: Optional[QWidget] = None, *, inline: bool = False
    ) -> QWidget:
        section, layout = _inline_section("字体", parent) if inline else _section("字体")

        # 与颜色面板保持一致：变化维度放左侧，编辑对象放右侧。
        self._font_tab_panel = _FolderTabPanel(
            (("japanese", "日文"), ("latin", "英数")),
            (("main", "主文字"), ("ruby", "注音")),
            section,
        )
        self._font_tab_stack = QStackedWidget(self._font_tab_panel)
        self._font_stroke_controls: dict[
            tuple[str, str], tuple[FluentSpinBox, CheckBox, FluentSpinBox]
        ] = {}
        self._font_controls: dict[
            tuple[str, str],
            tuple[
                _WheelFocusedFontComboBox,
                _WheelFocusedComboBox,
                Optional[str],
            ],
        ] = {}
        for subject, script in (
            ("main", "japanese"),
            ("main", "latin"),
            ("ruby", "japanese"),
            ("ruby", "latin"),
        ):
            self._font_tab_stack.addWidget(
                self._make_font_settings_page(subject, script, self._font_tab_stack)
            )
        self._font_tab_panel.content_layout.addWidget(self._font_tab_stack)
        self._font_tab_panel.leftChanged.connect(
            lambda _key: self._sync_font_settings_page()
        )
        self._font_tab_panel.rightChanged.connect(
            lambda _key: self._sync_font_settings_page()
        )
        layout.addWidget(self._font_tab_panel)

        self._italic_check = CheckBox("斜体", section)
        self._italic_check.toggled.connect(lambda checked: self._update_style(italic=checked))
        self._ruby_anchor_check = CheckBox("参与注音高度计算", section)
        self._ruby_anchor_check.setToolTip(
            "关闭后，使用当前角色的字符仍正常绘制和占位，但不会把整行注音向上顶高。"
        )
        self._ruby_anchor_check.toggled.connect(
            lambda checked: self._update_style(affects_ruby_anchor=checked)
        )
        flags_row = QHBoxLayout()
        flags_row.setContentsMargins(0, 0, 0, 0)
        flags_row.setSpacing(12)
        flags_row.addWidget(self._italic_check)
        flags_row.addWidget(self._ruby_anchor_check)
        flags_row.addStretch(1)
        layout.addLayout(flags_row)

        return section

    def _make_font_settings_page(
        self, subject: str, script: str, parent: QWidget
    ) -> QWidget:
        page = QWidget(parent)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        font_combo = _WheelFocusedFontComboBox(page)
        _compact_control(font_combo)
        inherits_script = script == "latin"
        inheritance_label: Optional[str] = None
        if (subject, script) == ("main", "latin"):
            inheritance_label = "跟随主文字日文（0）"
        elif (subject, script) == ("ruby", "japanese"):
            inheritance_label = "跟随主文字（0）"
        elif (subject, script) == ("ruby", "latin"):
            inheritance_label = "跟随注音日文（0）"
        if inheritance_label is not None:
            font_combo.enable_inheritance(inheritance_label)
        size_spin = _spin(
            0 if inherits_script else (8 if subject == "ruby" else 12),
            _FONT_SIZE_MAX_PX,
            suffix=" px",
        )
        weight_combo = _WheelFocusedComboBox(page)
        _compact_control(weight_combo)
        slot = (subject, script)
        self._font_controls[slot] = (
            font_combo,
            weight_combo,
            inheritance_label,
        )
        self._refresh_font_weight_combo(
            slot, preferred_weight=0 if inheritance_label is not None else 400
        )
        font_combo.currentFontChanged.connect(
            lambda font, current_slot=slot: self._on_font_family_changed(
                current_slot, font
            )
        )

        if (subject, script) == ("main", "japanese"):
            self._font_combo = font_combo
            self._font_size_spin = size_spin
            self._font_weight_combo = weight_combo
            size_spin.valueChanged.connect(
                lambda value: self._update_style(font_size_px=value)
            )
            weight_combo.currentIndexChanged.connect(
                lambda _index: self._update_style(
                    font_weight=int(weight_combo.currentData())
                )
            )
        elif (subject, script) == ("main", "latin"):
            self._font_latin_combo = font_combo
            self._font_latin_size_spin = size_spin
            self._font_latin_weight_combo = weight_combo
            size_spin.valueChanged.connect(
                lambda value: self._update_style(
                    latin_font_size_px=None if value == 0 else value
                )
            )
            weight_combo.currentIndexChanged.connect(
                lambda _index: self._update_style(
                    latin_font_weight=(
                        None
                        if int(weight_combo.currentData()) == 0
                        else int(weight_combo.currentData())
                    )
                )
            )
        elif (subject, script) == ("ruby", "japanese"):
            self._ruby_font_combo = font_combo
            self._ruby_font_size_spin = size_spin
            self._ruby_font_weight_combo = weight_combo
            size_spin.valueChanged.connect(
                lambda value: self._update_ruby_font_override(
                    ruby_font_size_px=value
                )
            )
            weight_combo.currentIndexChanged.connect(
                lambda _index: self._update_ruby_font_override(
                    ruby_font_weight=(
                        None
                        if int(weight_combo.currentData()) == 0
                        else int(weight_combo.currentData())
                    )
                )
            )
        else:
            self._ruby_font_latin_combo = font_combo
            self._ruby_font_latin_size_spin = size_spin
            self._ruby_font_latin_weight_combo = weight_combo
            size_spin.valueChanged.connect(
                lambda value: self._update_ruby_font_override(
                    ruby_latin_font_size_px=None if value == 0 else value
                )
            )
            weight_combo.currentIndexChanged.connect(
                lambda _index: self._update_ruby_font_override(
                    ruby_latin_font_weight=(
                        None
                        if int(weight_combo.currentData()) == 0
                        else int(weight_combo.currentData())
                    )
                )
            )

        stroke_fields = {
            ("main", "japanese"): (
                "stroke_width_px", "stroke2_enabled", "stroke2_width_px"
            ),
            ("main", "latin"): (
                "latin_stroke_width_px",
                "latin_stroke2_enabled",
                "latin_stroke2_width_px",
            ),
            ("ruby", "japanese"): (
                "ruby_stroke_width_px",
                "ruby_stroke2_enabled",
                "ruby_stroke2_width_px",
            ),
            ("ruby", "latin"): (
                "ruby_latin_stroke_width_px",
                "ruby_latin_stroke2_enabled",
                "ruby_latin_stroke2_width_px",
            ),
        }[(subject, script)]
        stroke_width_field, stroke2_enabled_field, stroke2_width_field = stroke_fields
        stroke_width_spin = _spin(0, 120, suffix=" px")
        stroke2_enabled_check = CheckBox("", page)
        stroke2_enabled_check.setToolTip("启用或关闭描边 2")
        inherits_stroke2 = inherits_script or subject == "ruby"
        if inherits_stroke2:
            stroke2_enabled_check.setTristate(True)
            stroke2_enabled_check.setToolTip("半选表示跟随上一级字体槽（0）")
        stroke2_enabled_check.setFixedWidth(28)
        stroke2_enabled_check.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed
        )
        stroke2_width_spin = _spin(0, 120, suffix=" px")
        stroke_width_spin.valueChanged.connect(
            lambda value, field=stroke_width_field, inherit=inherits_script:
            self._update_style(**{field: None if inherit and value == 0 else value})
        )
        if inherits_stroke2:
            stroke2_enabled_check.stateChanged.connect(
                lambda state, field=stroke2_enabled_field, spin=stroke2_width_spin:
                self._on_font_stroke2_state_changed(field, spin, state)
            )
        else:
            stroke2_enabled_check.toggled.connect(
                lambda checked, field=stroke2_enabled_field, spin=stroke2_width_spin:
                self._on_font_stroke2_toggled(field, spin, checked)
            )
        stroke2_width_spin.valueChanged.connect(
            lambda value, field=stroke2_width_field, inherit=inherits_script:
            self._update_style(**{field: None if inherit and value == 0 else value})
        )
        self._font_stroke_controls[(subject, script)] = (
            stroke_width_spin,
            stroke2_enabled_check,
            stroke2_width_spin,
        )
        attr_prefix = {
            ("main", "japanese"): "",
            ("main", "latin"): "latin_",
            ("ruby", "japanese"): "ruby_",
            ("ruby", "latin"): "ruby_latin_",
        }[(subject, script)]
        setattr(self, f"_{attr_prefix}stroke_width_spin", stroke_width_spin)
        setattr(self, f"_{attr_prefix}stroke2_enabled_check", stroke2_enabled_check)
        setattr(self, f"_{attr_prefix}stroke2_width_spin", stroke2_width_spin)

        layout.addWidget(_field("字体", font_combo))
        row = QWidget(page)
        row_layout = QGridLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setHorizontalSpacing(8)
        row_layout.addWidget(_field("字号", size_spin), 0, 0)
        row_layout.addWidget(_field("字重", weight_combo), 0, 1)
        row_layout.setColumnStretch(0, 1)
        row_layout.setColumnStretch(1, 1)
        layout.addWidget(row)

        stroke_row = QWidget(page)
        stroke_layout = QGridLayout(stroke_row)
        stroke_layout.setContentsMargins(0, 0, 0, 0)
        stroke_layout.setHorizontalSpacing(8)
        stroke_width_widget = _field("描边宽度", stroke_width_spin)
        stroke2_control = QWidget(stroke_row)
        stroke2_control_layout = QHBoxLayout(stroke2_control)
        stroke2_control_layout.setContentsMargins(0, 0, 0, 0)
        stroke2_control_layout.setSpacing(2)
        stroke2_control_layout.addWidget(stroke2_enabled_check, 0)
        stroke2_control_layout.addWidget(stroke2_width_spin, 1)
        stroke2_widget = _field("描边 2", stroke2_control)
        setattr(self, f"_{attr_prefix}stroke_width_field", stroke_width_widget)
        setattr(self, f"_{attr_prefix}stroke2_field", stroke2_widget)
        # 兼容现有内部引用；开关与宽度现在属于同一个组合字段。
        setattr(self, f"_{attr_prefix}stroke2_enabled_field", stroke2_widget)
        setattr(self, f"_{attr_prefix}stroke2_width_field", stroke2_widget)
        stroke_layout.addWidget(stroke_width_widget, 0, 0)
        stroke_layout.addWidget(stroke2_widget, 0, 1)
        for column in range(2):
            stroke_layout.setColumnStretch(column, 1)
        layout.addWidget(stroke_row)
        return page

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
        section, layout = _inline_section("注音", parent) if inline else _section("注音")

        # 三个控件都很窄（数值框 / 短下拉），列宽阈值放低让常规面板宽度下单行放下。
        grid = _ResponsiveFieldGrid(section, min_column_width=90, max_columns=3)

        self._ruby_gap_spin = _spin(
            -_LAYOUT_SIZE_MAX_PX, _LAYOUT_SIZE_MAX_PX, suffix=" px"
        )
        self._ruby_gap_spin.valueChanged.connect(
            lambda value: self._update_layout_field(ruby_gap_px=value)
        )
        grid.add_field("与正文间距", self._ruby_gap_spin)

        self._ruby_interval_spin = _spin(
            -_LAYOUT_SIZE_MAX_PX, _LAYOUT_SIZE_MAX_PX, suffix=" px"
        )
        self._ruby_interval_spin.setToolTip(
            "注音字符之间的最小间距（N3 ルビ間隔），可为负让注音字符收紧。\n"
            "注意这是「下限」：注音比正文窄、均等分布摊出的间距大于此值时，"
            "调整它看不到变化；对超出正文宽度的长注音效果最明显。"
        )
        self._ruby_interval_spin.valueChanged.connect(
            lambda value: self._update_layout_field(ruby_interval_px=value)
        )
        grid.add_field("字间距", self._ruby_interval_spin)

        self._ruby_alignment_combo = _WheelFocusedComboBox(section)
        _compact_control(self._ruby_alignment_combo)
        for label, value in [
            ("自动", "auto"),
            ("居中", "center"),
            ("均等分布", "equal_space"),
        ]:
            self._ruby_alignment_combo.addItem(label, value)
        self._ruby_alignment_combo.setToolTip(
            "注音相对正文范围的排布（N3 ルビ配置）：自动 = 正文或注音全为英数时居中、"
            "否则均等分布。"
        )
        self._ruby_alignment_combo.currentIndexChanged.connect(
            lambda _index: self._update_layout_field(
                ruby_alignment=self._ruby_alignment_combo.currentData()
            )
        )
        grid.add_field("排布", self._ruby_alignment_combo)

        layout.addWidget(grid)
        return section

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
        section, layout = _inline_section("颜色", parent) if inline else _section("颜色")

        # 编辑对象 / 走字前后 / 图层 / 填充方式的下拉全部转为隐藏取值后端，
        # 界面换成文件夹式 tab + 竖排按钮列；依赖 currentData 的取值 / 同步
        # 逻辑与测试都无需改动。
        self._color_subject_combo = _WheelFocusedComboBox(section)
        self._color_subject_combo.addItem("主文字", "main")
        self._color_subject_combo.addItem("注音", "ruby")
        self._color_subject_combo.hide()
        self._color_subject_combo.currentIndexChanged.connect(
            lambda _index: self._on_color_subject_changed()
        )

        self._color_state_combo = _WheelFocusedComboBox(section)
        self._color_state_combo.addItem("走字前", "before")
        self._color_state_combo.addItem("走字后", "after")
        self._color_state_combo.setCurrentIndex(1)
        self._color_state_combo.hide()
        self._color_state_combo.currentIndexChanged.connect(
            lambda _index: self._on_color_target_combo_changed()
        )

        self._color_layer_combo = _WheelFocusedComboBox(section)
        self._color_layer_combo.addItem("文字", "text")
        self._color_layer_combo.addItem("描边", "stroke")
        self._color_layer_combo.addItem("描边2", "stroke2")
        self._color_layer_combo.addItem("装饰", "shadow")
        self._color_layer_combo.hide()
        self._color_layer_combo.currentIndexChanged.connect(
            lambda _index: self._on_color_target_combo_changed()
        )

        # 文件夹式 tab 面板：左上走字后/走字前，右上主文字/注音。
        self._color_tab_panel = _FolderTabPanel(
            (("after", "走字后"), ("before", "走字前")),
            (("main", "主文字"), ("ruby", "注音")),
            section,
        )
        self._color_tab_panel.leftChanged.connect(self._on_color_state_tab_changed)
        self._color_tab_panel.rightChanged.connect(self._on_color_subject_tab_changed)
        self._color_state_swap_button = _AnchoredTabActionButton(
            self._color_tab_panel,
            ("left", "after"),
            ("left", "before"),
            section,
        )
        self._color_state_swap_button.setObjectName("ColorStateSwapButton")
        self._color_state_swap_button.setIcon(QIcon(str(_COLOR_STATE_SWAP_ICON)))
        self._color_state_swap_button.setIconSize(QSize(16, 16))
        self._color_state_swap_button.setFixedSize(22, 22)
        self._color_state_swap_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._color_state_swap_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._color_state_swap_button.setToolTip("交换走字前后配色")
        self._color_state_swap_button.clicked.connect(
            self._swap_karaoke_color_states
        )
        themed(
            self._color_state_swap_button,
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
        layout.addWidget(self._color_tab_panel)

        self._color_layer_pill = _PillSelector(
            (
                ("text", "文字"),
                ("stroke", "描边"),
                ("stroke2", "描边2"),
                ("shadow", "装饰"),
            ),
            section,
            vertical=True,
        )
        self._color_layer_pill.changed.connect(self._on_color_layer_pill_changed)

        self._fill_mode_combo = _WheelFocusedComboBox(section)
        _compact_control(self._fill_mode_combo)
        for label, value in [
            ("全色", "solid"),
            ("横向渐变", "gradient_horizontal"),
            ("纵向渐变", "gradient_vertical"),
            ("纵向拼色", "split_vertical"),
            ("图像", "image"),
        ]:
            self._fill_mode_combo.addItem(label, value)
        self._fill_mode_combo.hide()
        self._fill_mode_combo.currentIndexChanged.connect(
            lambda _index: self._update_current_fill(
                mode=str(self._fill_mode_combo.currentData())
            )
        )
        # 填充方式改竖排按钮列，与隐藏 combo 双向同步：按钮 → combo 触发
        # _update_current_fill；_sync_color_fill_controls 设 combo → 按钮跟随
        self._fill_mode_pill = _PillSelector(
            (
                ("solid", "全色"),
                ("gradient_horizontal", "横渐变"),
                ("gradient_vertical", "纵渐变"),
                ("split_vertical", "拼色"),
                ("image", "图像"),
            ),
            section,
            vertical=True,
            icons=_fill_mode_icons(),
        )
        self._fill_mode_pill.changed.connect(
            lambda mode: self._fill_mode_combo.setCurrentIndex(
                max(0, self._fill_mode_combo.findData(mode))
            )
        )
        self._fill_mode_combo.currentIndexChanged.connect(
            lambda _index: self._fill_mode_pill.set_current(
                str(self._fill_mode_combo.currentData())
            )
        )

        self._decoration_type_combo = _WheelFocusedComboBox(section)
        _compact_control(self._decoration_type_combo)
        self._decoration_type_combo.addItem("无", "none")
        self._decoration_type_combo.addItem("阴影", "shadow")
        self._decoration_type_combo.addItem("发光", "glow")
        self._decoration_type_combo.currentIndexChanged.connect(
            lambda _index: self._update_shared_decoration(
                decoration_kind=str(self._decoration_type_combo.currentData())
            )
        )
        self._decoration_type_field = _field("装饰类型", self._decoration_type_combo)

        self._fill_editor_stack = _DynamicStackedWidget(section)
        self._fill_editor_stack.addWidget(self._make_solid_fill_page())
        self._fill_editor_stack.addWidget(self._make_gradient_fill_page())
        self._fill_editor_stack.addWidget(self._make_split_fill_page())
        self._fill_editor_stack.addWidget(self._make_image_fill_page())

        detail_grid = QWidget(section)
        detail_layout = QGridLayout(detail_grid)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        detail_layout.setHorizontalSpacing(8)
        detail_layout.setVerticalSpacing(8)

        self._shadow_x_spin = _spin(-40, 40, suffix=" px")
        self._shadow_x_spin.valueChanged.connect(
            lambda value: self._update_shared_decoration(shadow_offset_x=value)
        )
        self._shadow_x_field = _field("阴影 X", self._shadow_x_spin)
        detail_layout.addWidget(self._shadow_x_field, 1, 0)

        self._shadow_y_spin = _spin(-40, 40, suffix=" px")
        self._shadow_y_spin.valueChanged.connect(
            lambda value: self._update_shared_decoration(shadow_offset_y=value)
        )
        self._shadow_y_field = _field("阴影 Y", self._shadow_y_spin)
        detail_layout.addWidget(self._shadow_y_field, 1, 1)

        self._glow_before_radius_spin = _spin(0, 120, suffix=" px")
        self._glow_before_radius_spin.valueChanged.connect(
            lambda value: self._update_shared_decoration(
                glow_before_radius_px=value,
            )
        )
        self._glow_radius_spin = self._glow_before_radius_spin
        self._glow_radius_field = _field("走字前发光", self._glow_before_radius_spin)

        self._glow_after_radius_spin = _spin(0, 120, suffix=" px")
        self._glow_after_radius_spin.valueChanged.connect(
            lambda value: self._update_shared_decoration(glow_after_radius_px=value)
        )
        self._glow_after_radius_field = _field("走字后发光", self._glow_after_radius_spin)

        self._glow_concentration_combo = _WheelFocusedComboBox(section)
        _compact_control(self._glow_concentration_combo)
        for label, value in [("无", -1), ("低", 0), ("中", 1), ("高", 2)]:
            self._glow_concentration_combo.addItem(label, value)
        self._glow_concentration_combo.currentIndexChanged.connect(
            lambda _index: self._update_shared_decoration(
                glow_concentration_level=int(
                    self._glow_concentration_combo.currentData() or 0
                )
            )
        )
        self._glow_concentration_field = _field(
            "发光浓度", self._glow_concentration_combo
        )

        # 发光的三个参数共享一行，顺序与预览语义一致：走字前、走字后、浓度。
        self._glow_controls_row = QWidget(detail_grid)
        glow_row_layout = QHBoxLayout(self._glow_controls_row)
        glow_row_layout.setContentsMargins(0, 0, 0, 0)
        glow_row_layout.setSpacing(8)
        glow_row_layout.addWidget(self._glow_radius_field, 1)
        glow_row_layout.addWidget(self._glow_after_radius_field, 1)
        glow_row_layout.addWidget(self._glow_concentration_field, 1)
        detail_layout.addWidget(self._glow_controls_row, 1, 0, 1, 2)

        detail_layout.setColumnStretch(0, 1)
        detail_layout.setColumnStretch(1, 1)

        self._ruby_color_actions_row = QWidget(section)
        ruby_color_actions_layout = QHBoxLayout(self._ruby_color_actions_row)
        ruby_color_actions_layout.setContentsMargins(0, 0, 0, 0)
        ruby_color_actions_layout.setSpacing(10)
        self._ruby_colors_follow_main_check = CheckBox(
            "默认跟随主文字", self._ruby_color_actions_row
        )
        self._ruby_colors_follow_main_check.setChecked(True)
        self._ruby_colors_follow_main_check.setToolTip(
            "勾选后，注音的文字、描边、描边2、装饰及全部填充参数实时跟随主文字配色。"
        )
        self._ruby_colors_follow_main_check.toggled.connect(
            self._on_ruby_colors_follow_main_toggled
        )
        self._ruby_apply_main_btn = FluentPushButton(
            "应用主文字配色", self._ruby_color_actions_row
        )
        self._ruby_apply_main_btn.setMinimumHeight(32)
        self._ruby_apply_main_btn.clicked.connect(self._apply_main_colors_to_ruby)
        ruby_color_actions_layout.addWidget(self._ruby_colors_follow_main_check, 0)
        ruby_color_actions_layout.addWidget(self._ruby_apply_main_btn, 1)
        self._set_ruby_color_controls_visible(False)

        # tab 内容区：左·图层列 + 填充方式列（竖排按钮），右·填充编辑和
        # 整个配色方案共用的装饰参数。描边尺寸已经归入字体卡片。
        columns = QHBoxLayout()
        columns.setContentsMargins(0, 0, 0, 0)
        columns.setSpacing(10)
        columns.addWidget(self._color_layer_pill, 0, Qt.AlignmentFlag.AlignTop)
        columns.addWidget(self._fill_mode_pill, 0, Qt.AlignmentFlag.AlignTop)
        editors = QVBoxLayout()
        editors.setContentsMargins(0, 0, 0, 0)
        editors.setSpacing(10)
        editors.addWidget(self._decoration_type_field)
        editors.addWidget(self._fill_editor_stack)
        editors.addWidget(detail_grid)
        editors.addStretch(1)
        columns.addLayout(editors, 1)
        self._color_tab_panel.content_layout.addLayout(columns)
        self._color_tab_panel.content_layout.addWidget(self._ruby_color_actions_row)
        return section

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
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        self._paint_solid_btn = self._paint_color_button("color", "#FFFFFF")
        layout.addWidget(self._paint_solid_btn)
        return page

    def _make_gradient_fill_page(self) -> QWidget:
        page = QWidget()
        layout = QGridLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setHorizontalSpacing(8)
        layout.setVerticalSpacing(8)
        self._paint_gradient_start_btn = self._paint_color_button("start_color", "#FFFFFF")
        self._paint_gradient_end_btn = self._paint_color_button("end_color", "#FF5A6F")
        self._paint_gradient_start_btn.hide()
        self._paint_gradient_end_btn.hide()
        self._gradient_editor = GradientStopsEditor(page)
        self._gradient_editor.stopsChanged.connect(self._update_gradient_stops)
        self._gradient_editor.selectedChanged.connect(
            lambda _index: self._sync_gradient_stop_controls()
        )
        self._gradient_bar_field = self._gradient_editor

        self._gradient_stop_color_btn = ColorButton("#FFFFFF", page)
        self._wire_color_edit_session(self._gradient_stop_color_btn)
        self._gradient_stop_color_btn.clicked.connect(self._choose_gradient_stop_color)
        self._gradient_stop_color_btn.colorEntered.connect(
            self._gradient_editor.set_selected_color
        )
        self._gradient_stop_color_btn.screenPickRequested.connect(
            lambda: self._choose_gradient_stop_color(screen_pick=True)
        )
        self._gradient_stop_position_spin = _double_spin(
            0, 100, decimals=3, suffix=" %"
        )
        self._gradient_stop_position_spin.valueChanged.connect(
            self._set_gradient_stop_position
        )
        # 删除收成图标按钮，和颜色 / 位置挤在关键点行里，省一整行
        self._gradient_stop_delete_btn = FluentTransparentToolButton(FIF.DELETE, page)
        self._gradient_stop_delete_btn.setToolTip("删除关键点")
        self._gradient_stop_delete_btn.setAccessibleName("删除关键点")
        self._gradient_stop_delete_btn.setFixedSize(30, 30)
        self._gradient_stop_delete_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._gradient_stop_delete_btn.clicked.connect(
            self._gradient_editor.delete_selected_stop
        )
        self._gradient_color_field = _field(
            "关键点颜色", self._gradient_stop_color_btn
        )
        position_row = QWidget(page)
        position_layout = QHBoxLayout(position_row)
        position_layout.setContentsMargins(0, 0, 0, 0)
        position_layout.setSpacing(6)
        position_layout.addWidget(self._gradient_stop_position_spin, 1)
        position_layout.addWidget(
            self._gradient_stop_delete_btn, 0, Qt.AlignmentFlag.AlignBottom
        )
        self._gradient_position_field = _field("关键点位置", position_row)
        self._ruby_horizontal_gradient_with_main_check = CheckBox(
            "注音与主文字共享横向渐变", page
        )
        self._ruby_horizontal_gradient_with_main_check.setChecked(True)
        self._ruby_horizontal_gradient_with_main_check.setToolTip(
            "开启后，注音与下方主文字使用同一个整行横向渐变范围，颜色进度保持一致。"
        )
        self._ruby_horizontal_gradient_with_main_check.toggled.connect(
            lambda checked: self._update_style(
                ruby_horizontal_gradient_with_main=checked
            )
        )
        self._gradient_editor_layout = layout
        self._arrange_stop_editor(
            layout,
            self._gradient_bar_field,
            self._gradient_color_field,
            self._gradient_position_field,
            vertical=False,
            footer=self._ruby_horizontal_gradient_with_main_check,
        )
        return page

    def _make_split_fill_page(self) -> QWidget:
        page = QWidget()
        layout = QGridLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setHorizontalSpacing(8)
        layout.setVerticalSpacing(8)
        self._split_editor = GradientStopsEditor(page)
        self._split_editor.set_orientation("split_vertical")
        self._split_editor.stopsChanged.connect(self._update_split_stops)
        self._split_editor.selectedChanged.connect(
            lambda _index: self._sync_split_stop_controls()
        )
        self._split_bar_field = self._split_editor

        self._split_stop_color_btn = ColorButton("#FFFFFF", page)
        self._wire_color_edit_session(self._split_stop_color_btn)
        self._split_stop_color_btn.clicked.connect(self._choose_split_stop_color)
        self._split_stop_color_btn.colorEntered.connect(
            self._split_editor.set_selected_color
        )
        self._split_stop_color_btn.screenPickRequested.connect(
            lambda: self._choose_split_stop_color(screen_pick=True)
        )
        self._split_stop_position_spin = _double_spin(
            0, 100, decimals=3, suffix=" %"
        )
        self._split_stop_position_spin.valueChanged.connect(
            self._set_split_stop_position
        )
        self._split_stop_delete_btn = FluentTransparentToolButton(FIF.DELETE, page)
        self._split_stop_delete_btn.setToolTip("删除分段点")
        self._split_stop_delete_btn.setAccessibleName("删除分段点")
        self._split_stop_delete_btn.setFixedSize(30, 30)
        self._split_stop_delete_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._split_stop_delete_btn.clicked.connect(
            self._split_editor.delete_selected_stop
        )
        self._split_color_field = _field("分段颜色", self._split_stop_color_btn)
        position_row = QWidget(page)
        position_layout = QHBoxLayout(position_row)
        position_layout.setContentsMargins(0, 0, 0, 0)
        position_layout.setSpacing(6)
        position_layout.addWidget(self._split_stop_position_spin, 1)
        position_layout.addWidget(
            self._split_stop_delete_btn, 0, Qt.AlignmentFlag.AlignBottom
        )
        self._split_position_field = _field("分段位置", position_row)
        self._arrange_stop_editor(
            layout,
            self._split_bar_field,
            self._split_color_field,
            self._split_position_field,
            vertical=True,
        )
        return page

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
        page = QWidget()
        layout = QGridLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setHorizontalSpacing(8)
        layout.setVerticalSpacing(8)
        self._paint_image_path_edit = FluentLineEdit(page)
        _compact_control(self._paint_image_path_edit)
        self._paint_image_path_edit.editingFinished.connect(
            lambda: self._update_current_fill(image_path=self._paint_image_path_edit.text())
        )
        self._paint_image_browse_btn = FluentPushButton("浏览...", page)
        self._paint_image_browse_btn.setMinimumHeight(32)
        self._paint_image_browse_btn.clicked.connect(self._choose_paint_image)
        self._paint_image_scale_spin = _spin(1, 1000, suffix=" %")
        self._paint_image_scale_spin.valueChanged.connect(
            lambda value: self._update_current_fill(image_scale_pct=value)
        )
        path_row = QWidget(page)
        path_layout = QHBoxLayout(path_row)
        path_layout.setContentsMargins(0, 0, 0, 0)
        path_layout.setSpacing(4)
        path_layout.addWidget(self._paint_image_path_edit, 1)
        path_layout.addWidget(self._paint_image_browse_btn)
        layout.addWidget(_field("图像文件", path_row), 0, 0, 1, 2)
        layout.addWidget(_field("缩放", self._paint_image_scale_spin), 1, 0)
        layout.setColumnStretch(0, 1)
        layout.setColumnStretch(1, 1)
        return page

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
        row_layout = QHBoxLayout(nav)
        row_layout.setContentsMargins(6, 6, 6, 6)
        row_layout.setSpacing(4)

        # 单行排布：角色下拉吸收剩余宽度，操作收成紧凑图标按钮。
        # 内部仍叫 _singer_combo（少改动），但现在装的是「角色」：全局默认 + 各角色名。
        self._singer_combo = _WheelFocusedComboBox(nav)
        _compact_control(self._singer_combo)
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

    def _make_effects_page(self) -> QWidget:
        scroll, layout = _scroll_page()
        layout.addWidget(self._make_animation_section())
        layout.addWidget(self._make_lit_section())
        layout.addStretch(1)
        return scroll

    # ----------------------------------------------------------------- 标题（B7）

    def _make_title_page(self) -> QWidget:
        scroll, layout = _scroll_page()
        layout.addWidget(self._make_title_text_section())
        layout.addWidget(self._make_title_style_section())
        layout.addWidget(self._make_title_time_section())
        layout.addStretch(1)
        return scroll

    def _make_title_text_section(self) -> QFrame:
        section, layout = _section("标题", switch=True)
        self._title_enabled_switch = section.header_switch
        self._title_enabled_switch.toggled.connect(self._on_title_enabled_toggled)

        # 多行文本框：回车换行后自动增高（背景卡片随之变高）。
        self._title_text_edit = _GrowingPlainTextEdit(section)
        self._title_text_edit.setPlaceholderText("{title} / {artist}")
        self._title_text_edit.setToolTip(
            "支持换行；{title} / {artist} 会从字幕元数据中读取。"
        )
        self._title_text_edit.textChanged.connect(self._on_title_text_changed)
        self._title_text_edit.editingFinished.connect(self._commit_title_text_edit)
        layout.addWidget(_field("标题文字", self._title_text_edit))
        return section

    def _make_title_style_section(self) -> QFrame:
        section, layout = _section("外观")
        self._title_appearance_grid = _ResponsiveFieldGrid(
            section, min_column_width=260, max_columns=2
        )

        self._title_layout_combo = _WheelFocusedComboBox(section)
        _compact_control(self._title_layout_combo)
        self._title_layout_combo.setToolTip(
            "标题引用的布局方案（与布局页管理的是同一份列表）："
            "决定标题的锚点、余白与行间距。"
        )
        self._title_layout_combo.currentIndexChanged.connect(self._on_title_layout_changed)
        self._title_appearance_grid.add_field("布局方案", self._title_layout_combo)

        self._title_scheme_edit_btn = FluentPushButton("编辑标题配色", section)
        self._title_scheme_edit_btn.setMinimumHeight(30)
        self._title_scheme_edit_btn.setToolTip(
            "字体与颜色由字体页的「标题」配色方案决定，点击前往编辑。"
        )
        self._title_scheme_edit_btn.clicked.connect(self._open_title_scheme)
        self._title_appearance_grid.add_field("字体与颜色", self._title_scheme_edit_btn)
        layout.addWidget(self._title_appearance_grid)
        return section

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
        section, layout = _section("显示时段")
        self._title_time_grid = _ResponsiveFieldGrid(
            section, min_column_width=150, max_columns=1
        )

        self._title_mode_combo = _WheelFocusedComboBox(section)
        _compact_control(self._title_mode_combo)
        for label, value in [
            ("全程显示", "whole"),
            ("仅开头", "head"),
            ("仅片尾", "tail"),
            ("开始和片尾", "head_tail"),
        ]:
            self._title_mode_combo.addItem(label, value)
        self._title_mode_combo.currentIndexChanged.connect(
            lambda _i: self._update_title(show_mode=self._title_mode_combo.currentData())
        )
        self._title_time_grid.add_field("显示模式", self._title_mode_combo)
        layout.addWidget(self._title_time_grid)

        self._title_head_row = QWidget(section)
        head_row_layout = QHBoxLayout(self._title_head_row)
        head_row_layout.setContentsMargins(0, 0, 0, 0)
        head_row_layout.setSpacing(8)
        self._title_head_row_label = _subgroup_label("开头")
        self._title_head_row_label.setFixedWidth(42)
        head_row_layout.addWidget(
            self._title_head_row_label, 0, Qt.AlignmentFlag.AlignTop
        )
        # 秒 + 毫秒两个输入框比原来的单个 ms 框宽，列宽下限跟着抬高，
        # 否则窄面板下两列会把 "999 ms" 挤到显示不全。
        self._title_head_grid = _ResponsiveFieldGrid(
            self._title_head_row, min_column_width=160, max_columns=4
        )
        head_row_layout.addWidget(self._title_head_grid, 1)

        self._title_fade_in_spin = _DurationSpin(0, 10_000)
        self._title_fade_in_spin.valueChanged.connect(
            lambda value: self._update_title(fade_in_ms=value)
        )
        self._title_head_grid.add_field("淡入", self._title_fade_in_spin)
        self._title_head_spin = _DurationSpin(0, 600_000)
        self._title_head_spin.valueChanged.connect(
            lambda value: self._update_title(head_offset_ms=value)
        )
        self._title_head_grid.add_field("偏移", self._title_head_spin)
        self._title_duration_spin = _DurationSpin(0, 600_000)
        self._title_duration_spin.valueChanged.connect(
            lambda value: self._update_title(duration_ms=value)
        )
        self._title_head_grid.add_field("显示时长", self._title_duration_spin)
        self._title_fade_out_spin = _DurationSpin(0, 10_000)
        self._title_fade_out_spin.valueChanged.connect(
            lambda value: self._update_title(fade_out_ms=value)
        )
        self._title_head_grid.add_field("淡出", self._title_fade_out_spin)

        self._title_tail_row = QWidget(section)
        tail_row_layout = QHBoxLayout(self._title_tail_row)
        tail_row_layout.setContentsMargins(0, 0, 0, 0)
        tail_row_layout.setSpacing(8)
        self._title_tail_row_label = _subgroup_label("片尾")
        self._title_tail_row_label.setFixedWidth(42)
        tail_row_layout.addWidget(
            self._title_tail_row_label, 0, Qt.AlignmentFlag.AlignTop
        )
        self._title_tail_grid = _ResponsiveFieldGrid(
            self._title_tail_row, min_column_width=160, max_columns=4
        )
        tail_row_layout.addWidget(self._title_tail_grid, 1)

        self._title_tail_fade_in_spin = _DurationSpin(0, 10_000)
        self._title_tail_fade_in_spin.valueChanged.connect(
            lambda value: self._update_title(tail_fade_in_ms=value)
        )
        self._title_tail_grid.add_field("淡入", self._title_tail_fade_in_spin)
        self._title_tail_spin = _DurationSpin(0, 600_000)
        self._title_tail_spin.valueChanged.connect(
            lambda value: self._update_title(tail_offset_ms=value)
        )
        self._title_tail_grid.add_field("偏移", self._title_tail_spin)
        self._title_tail_duration_spin = _DurationSpin(0, 600_000)
        self._title_tail_duration_spin.valueChanged.connect(
            lambda value: self._update_title(tail_duration_ms=value)
        )
        self._title_tail_grid.add_field("显示时长", self._title_tail_duration_spin)
        self._title_tail_fade_out_spin = _DurationSpin(0, 10_000)
        self._title_tail_fade_out_spin.valueChanged.connect(
            lambda value: self._update_title(tail_fade_out_ms=value)
        )
        self._title_tail_grid.add_field("淡出", self._title_tail_fade_out_spin)

        layout.addWidget(self._title_head_row)
        layout.addWidget(self._title_tail_row)
        return section

    def _current_title(self) -> TitleOverlay:
        return self._style.title_overlay if self._style.title_overlay is not None else TitleOverlay()

    def _on_title_enabled_toggled(self, checked: bool) -> None:
        self._update_title(enabled=checked)

    def _on_title_text_changed(self) -> None:
        if self._syncing:
            return
        title = self._current_title()
        new_text = self._title_text_edit.toPlainText()
        new_title = replace(
            title,
            text_template=new_text,
            char_role_labels=migrate_title_char_role_labels(
                title.text_template,
                title.char_role_labels,
                new_text,
            ),
        )
        # Keep typing responsive by coalescing the expensive host-side preview,
        # source-table, undo snapshot and settings updates.
        self._style = replace(self._style, title_overlay=new_title)
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
        title = self._current_title()
        if "text_template" in changes:
            new_text = str(changes["text_template"])
            changes["char_role_labels"] = migrate_title_char_role_labels(
                title.text_template,
                title.char_role_labels,
                new_text,
            )
        if "show_mode" in changes and changes["show_mode"] not in TITLE_SHOW_MODES:
            changes["show_mode"] = "whole"
        new_title = replace(title, **changes)
        self._style = replace(self._style, title_overlay=new_title)
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
        self._title_head_spin.setValue(title.head_offset_ms)
        self._title_duration_spin.setValue(title.duration_ms)
        self._title_tail_spin.setValue(title.tail_offset_ms)
        self._title_fade_in_spin.setValue(title.fade_in_ms)
        self._title_fade_out_spin.setValue(title.fade_out_ms)
        self._title_tail_duration_spin.setValue(
            title.duration_ms
            if title.tail_duration_ms is None
            else title.tail_duration_ms
        )
        self._title_tail_fade_in_spin.setValue(
            title.fade_in_ms
            if title.tail_fade_in_ms is None
            else title.tail_fade_in_ms
        )
        self._title_tail_fade_out_spin.setValue(
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
        section, layout = _section("指示灯", switch=True)
        self._lit_section = section

        self._lit_enabled_switch = section.header_switch
        self._lit_enabled_switch.toggled.connect(
            lambda checked: self._update_style(lit_enabled=checked)
        )

        # 形状灯（圆/方/圆角）与音量柱用的是两套互不相干的字段：形状灯读 lit.*，
        # 音量柱读 volume.*。控件按种类分组成小节，再按当前 lit_style 整组显隐，
        # 既不会出现「调了没反应」的死控件，也不会留下空网格。
        self._lit_volume_groups: list[QWidget] = []
        self._lit_shape_groups: list[QWidget] = []

        self._lit_group_grids: dict[str, _ResponsiveFieldGrid] = {}

        def group(
            title: str,
            category: str | None,
            *,
            collapsed: bool = False,
            min_column_width: int = 135,
            max_columns: int = 4,
        ):
            box = _SubGroup(title, collapsed=collapsed, parent=section)
            layout.addWidget(box)
            if category == "volume":
                self._lit_volume_groups.append(box)
            elif category == "shape":
                self._lit_shape_groups.append(box)
            fields = _ResponsiveFieldGrid(
                box,
                min_column_width=min_column_width,
                max_columns=max_columns,
            )
            box.grid.addWidget(fields, 0, 0, 1, 2)
            self._lit_group_grids[title] = fields

            def add(label: str | None, control: QWidget) -> None:
                fields.add_widget(_field(label, control) if label is not None else control)

            return add

        # ---- 样式（始终可见，决定下面显示哪一组） -------------------------------
        self._lit_style_combo = _WheelFocusedComboBox(section)
        _compact_control(self._lit_style_combo)
        for label, value in [
            ("音量柱", "volume"),
            ("圆形", "circle"),
            ("方形", "square"),
            ("圆角", "rounded"),
        ]:
            self._lit_style_combo.addItem(label, value)
        self._lit_style_combo.currentIndexChanged.connect(
            lambda _index: self._update_style(lit_style=self._lit_style_combo.currentData())
        )
        # ---- 通用（两种样式共用） ----------------------------------------------
        add = group("通用", None, min_column_width=130, max_columns=5)
        add("样式", self._lit_style_combo)
        self._lit_duration_spin = _spin(0, 60_000, suffix=" ms")
        self._lit_duration_spin.valueChanged.connect(
            lambda value: self._update_style(signals_duration_ms=value)
        )
        add("持续", self._lit_duration_spin)

        self._lit_waiting_time_spin = _spin(0, 60_000, suffix=" ms")
        self._lit_waiting_time_spin.valueChanged.connect(
            lambda value: self._update_style(lit_waiting_time_ms=value)
        )
        add("等待", self._lit_waiting_time_spin)

        self._lit_stroke_width_spin = _spin(0, 40, suffix=" px")
        self._lit_stroke_width_spin.valueChanged.connect(
            lambda value: self._update_style(lit_stroke_width=value)
        )
        add("描边宽度", self._lit_stroke_width_spin)

        self._lit_opacity_spin = _spin(0, 100, suffix=" %")
        self._lit_opacity_spin.valueChanged.connect(
            lambda value: self._update_style(lit_opacity_pct=value)
        )
        add("透明度", self._lit_opacity_spin)

        # ---- 音量柱 · 尺寸 ------------------------------------------------------
        add = group("音量柱 · 布局", "volume", max_columns=4)
        self._volume_size_spin = _spin(4, 240, suffix=" px")
        self._volume_size_spin.valueChanged.connect(
            lambda value: self._update_style(volume_size=value)
        )
        add("整体大小", self._volume_size_spin)

        self._volume_column_width_spin = _spin(1, 120, suffix=" px")
        self._volume_column_width_spin.valueChanged.connect(
            lambda value: self._update_style(volume_column_width=value)
        )
        add("柱条宽度", self._volume_column_width_spin)

        self._volume_column_count_spin = _spin(1, 16)
        self._volume_column_count_spin.valueChanged.connect(
            lambda value: self._update_style(volume_column_count=value)
        )
        add("柱条数量", self._volume_column_count_spin)

        self._volume_column_spacing_spin = _spin(0, 120, suffix=" px")
        self._volume_column_spacing_spin.valueChanged.connect(
            lambda value: self._update_style(volume_column_spacing=value)
        )
        add("柱条间距", self._volume_column_spacing_spin)

        self._volume_ratio_spin = _spin(1, 20)
        self._volume_ratio_spin.valueChanged.connect(
            lambda value: self._update_style(volume_ratio=float(value))
        )
        add("前后比率", self._volume_ratio_spin)

        self._volume_align_combo = _WheelFocusedComboBox(section)
        _compact_control(self._volume_align_combo)
        for label, value in [("顶部", 0), ("居中", 1), ("底部", 2)]:
            self._volume_align_combo.addItem(label, value)
        self._volume_align_combo.currentIndexChanged.connect(
            lambda _index: self._update_style(volume_align=int(self._volume_align_combo.currentData()))
        )
        add("柱条对齐", self._volume_align_combo)

        # 位置与尺寸同属几何域，放在同一个响应式布局组中。
        self._volume_x_spin = _spin(-4000, 4000)
        self._volume_x_spin.valueChanged.connect(
            lambda value: self._update_style(volume_offset_x=value)
        )
        add("X", self._volume_x_spin)

        self._volume_y_spin = _spin(-4000, 4000)
        self._volume_y_spin.valueChanged.connect(
            lambda value: self._update_style(volume_offset_y=value)
        )
        add("Y", self._volume_y_spin)

        # ---- 音量柱 · 闪烁 ------------------------------------------------------
        add = group("音量柱 · 动画", "volume", collapsed=True, max_columns=3)
        self._volume_flash_times_spin = _spin(1, 20)
        self._volume_flash_times_spin.valueChanged.connect(
            lambda value: self._update_style(volume_flash_times=value)
        )
        add("闪烁次数", self._volume_flash_times_spin)

        self._volume_flash_duration_spin = _spin(0, 100, suffix=" %")
        self._volume_flash_duration_spin.valueChanged.connect(
            lambda value: self._update_style(volume_flash_duration_ratio=value / 100.0)
        )
        add("闪烁占比", self._volume_flash_duration_spin)

        self._volume_transition_ratio_spin = _spin(0, 100, suffix=" %")
        self._volume_transition_ratio_spin.valueChanged.connect(
            lambda value: self._update_style(volume_transition_ratio_pct=value)
        )
        add("覆盖过渡", self._volume_transition_ratio_spin)

        # ---- 音量柱 · 颜色 ------------------------------------------------------
        add = group("音量柱 · 颜色", "volume", max_columns=4)
        self._volume_fill_btn = self._color_button("volume_fill_color", self._style.volume_fill_color)
        self._volume_stroke_btn = self._color_button(
            "volume_stroke_color", self._style.volume_stroke_color
        )
        self._volume_overlay_fill_btn = self._color_button(
            "volume_overlay_fill_color", self._style.volume_overlay_fill_color
        )
        self._volume_overlay_stroke_btn = self._color_button(
            "volume_overlay_stroke_color", self._style.volume_overlay_stroke_color
        )
        add("柱填充色", self._volume_fill_btn)
        add("柱描边色", self._volume_stroke_btn)
        add("覆盖填充色", self._volume_overlay_fill_btn)
        add("覆盖描边色", self._volume_overlay_stroke_btn)

        # ---- 形状灯 · 尺寸 ------------------------------------------------------
        add = group("形状灯 · 布局", "shape", max_columns=5)
        self._lit_number_spin = _spin(1, 8)
        self._lit_number_spin.valueChanged.connect(
            lambda value: self._update_style(lit_number=value)
        )
        add("数量", self._lit_number_spin)

        self._lit_size_spin = _spin(4, 160, suffix=" px")
        self._lit_size_spin.valueChanged.connect(
            lambda value: self._update_style(lit_size=value)
        )
        add("大小", self._lit_size_spin)

        self._lit_tracking_spin = _spin(0, 200, suffix=" px")
        self._lit_tracking_spin.valueChanged.connect(
            lambda value: self._update_style(lit_tracking=value)
        )
        add("间距", self._lit_tracking_spin)

        # 位置与数量/大小/间距合并，避免两个短字段单独占一整小节。
        self._lit_x_spin = _spin(-4000, 4000)
        self._lit_x_spin.valueChanged.connect(
            lambda value: self._update_style(lit_offset_x=value)
        )
        add("X", self._lit_x_spin)

        self._lit_y_spin = _spin(-4000, 4000)
        self._lit_y_spin.valueChanged.connect(
            lambda value: self._update_style(lit_offset_y=value)
        )
        add("Y", self._lit_y_spin)

        # ---- 形状灯 · 外观 ------------------------------------------------------
        add = group("形状灯 · 外观", "shape", max_columns=5)
        self._lit_fill_btn = self._color_button("lit_fill_color", self._style.lit_fill_color)
        add("填充颜色", self._lit_fill_btn)

        self._lit_stroke_btn = self._color_button("lit_stroke_color", self._style.lit_stroke_color)
        add("描边颜色", self._lit_stroke_btn)

        self._lit_edge_brightness_spin = _spin(0, 100, suffix=" %")
        self._lit_edge_brightness_spin.valueChanged.connect(
            lambda value: self._update_style(lit_edge_brightness_pct=value)
        )
        add("边缘亮度", self._lit_edge_brightness_spin)

        self._lit_stroke_soften_spin = _spin(0, 40, suffix=" px")
        self._lit_stroke_soften_spin.valueChanged.connect(
            lambda value: self._update_style(lit_stroke_soften=value)
        )
        add("描边柔化", self._lit_stroke_soften_spin)

        self._lit_shadow_check = CheckBox("启用", section)
        self._lit_shadow_check.toggled.connect(
            lambda checked: self._update_style(lit_shadow=checked)
        )
        add("阴影", self._lit_shadow_check)

        # ---- 形状灯 · 转场 ------------------------------------------------------
        add = group("形状灯 · 转场", "shape", collapsed=True, max_columns=4)
        self._lit_transition_mode_combo = _WheelFocusedComboBox(section)
        _compact_control(self._lit_transition_mode_combo)
        for label, value in [("无", "none"), ("淡入淡出", "fade"), ("滑动", "slide")]:
            self._lit_transition_mode_combo.addItem(label, value)
        self._lit_transition_mode_combo.currentIndexChanged.connect(
            lambda _index: self._update_style(
                lit_transition_mode=self._lit_transition_mode_combo.currentData()
            )
        )
        add("类型", self._lit_transition_mode_combo)

        self._lit_transition_ratio_spin = _spin(0, 100, suffix=" %")
        self._lit_transition_ratio_spin.valueChanged.connect(
            lambda value: self._update_style(lit_transition_ratio_pct=value)
        )
        add("时长比例", self._lit_transition_ratio_spin)

        self._lit_transition_angle_spin = _spin(-360, 360, suffix=" °")
        self._lit_transition_angle_spin.valueChanged.connect(
            lambda value: self._update_style(lit_transition_angle_deg=value)
        )
        add("角度", self._lit_transition_angle_spin)

        self._lit_transition_distance_spin = _spin(0, 800, suffix=" px")
        self._lit_transition_distance_spin.valueChanged.connect(
            lambda value: self._update_style(lit_transition_distance=value)
        )
        add("距离", self._lit_transition_distance_spin)

        self._sync_lit_style_visibility()
        section.set_expanded(False)
        return section

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
        section, layout = _section("入退场动画")

        self._animation_grid = _ResponsiveFieldGrid(
            section, min_column_width=260, max_columns=2
        )

        self._entry_anim_combo = _WheelFocusedComboBox(section)
        _compact_control(self._entry_anim_combo)
        for label, value in [
            ("无", "none"),
            ("淡入", "fade"),
            ("滑入", "slide_in"),
            ("上移", "rise"),
            ("逐文字渐显", "char_fade"),
            ("文字垂下", "char_drip"),
            ("旋转翻转", "spin_flip"),
            ("utopia", "utopia"),
        ]:
            self._entry_anim_combo.addItem(label, value)
        self._entry_anim_combo.currentIndexChanged.connect(
            lambda _index: self._update_style(
                entry_anim=self._entry_anim_combo.currentData()
            )
        )
        self._entry_lead_spin = _spin(0, 3000, suffix=" ms")
        self._entry_lead_spin.valueChanged.connect(
            lambda value: self._update_style(entry_lead_ms=value)
        )
        self._entry_animation_row = QWidget(section)
        entry_row_layout = QHBoxLayout(self._entry_animation_row)
        entry_row_layout.setContentsMargins(0, 0, 0, 0)
        entry_row_layout.setSpacing(6)
        entry_row_layout.addWidget(self._entry_anim_combo, 2)
        self._entry_lead_spin.setToolTip("入场动画时长")
        entry_row_layout.addWidget(self._entry_lead_spin, 1)
        self._animation_grid.add_field("入场动画 / 时长", self._entry_animation_row)

        self._exit_anim_combo = _WheelFocusedComboBox(section)
        _compact_control(self._exit_anim_combo)
        for label, value in [
            ("无", "none"),
            ("淡出", "fade"),
            ("滑出", "slide_out"),
            ("上移", "rise"),
            ("逐文字渐隐", "char_fade"),
            ("文字垂出", "char_drip"),
            ("旋转翻转", "spin_flip"),
            ("utopia", "utopia"),
        ]:
            self._exit_anim_combo.addItem(label, value)
        self._exit_anim_combo.currentIndexChanged.connect(
            lambda _index: self._update_style(
                exit_anim=self._exit_anim_combo.currentData()
            )
        )
        self._exit_fade_spin = _spin(0, 3000, suffix=" ms")
        self._exit_fade_spin.valueChanged.connect(
            lambda value: self._update_style(exit_fade_ms=value)
        )
        self._exit_animation_row = QWidget(section)
        exit_row_layout = QHBoxLayout(self._exit_animation_row)
        exit_row_layout.setContentsMargins(0, 0, 0, 0)
        exit_row_layout.setSpacing(6)
        exit_row_layout.addWidget(self._exit_anim_combo, 2)
        self._exit_fade_spin.setToolTip("退场动画时长")
        exit_row_layout.addWidget(self._exit_fade_spin, 1)
        self._animation_grid.add_field("退场动画 / 时长", self._exit_animation_row)

        self._karaoke_anim_combo = _WheelFocusedComboBox(section)
        _compact_control(self._karaoke_anim_combo)
        for label, value in [
            ("无", "none"),
            ("utopia", "utopia"),
        ]:
            self._karaoke_anim_combo.addItem(label, value)
        self._karaoke_anim_combo.setToolTip(
            "控制歌词正在着色时的逐字动画；旧项目的 Utopia 入退场会自动兼容"
        )
        self._karaoke_anim_combo.currentIndexChanged.connect(
            lambda _index: self._update_style(
                karaoke_anim=self._karaoke_anim_combo.currentData()
            )
        )
        self._animation_grid.add_field("唱字特效", self._karaoke_anim_combo)

        layout.addWidget(self._animation_grid)
        return section

    def _make_viewport_section(self) -> QFrame:
        section, layout = _section("视图")

        self._viewport_align_combo = _WheelFocusedComboBox(section)
        _compact_control(self._viewport_align_combo)
        for label, value in [
            ("左上", "top_left"),
            ("中上", "top_center"),
            ("右上", "top_right"),
            ("左中", "center_left"),
            ("居中", "center"),
            ("右中", "center_right"),
            ("左下", "bottom_left"),
            ("中下", "bottom_center"),
            ("右下", "bottom_right"),
        ]:
            self._viewport_align_combo.addItem(label, value)
        self._viewport_align_combo.currentIndexChanged.connect(
            lambda _index: self._update_style(
                viewport_align=self._viewport_align_combo.currentData()
            )
        )

        grid = _ResponsiveFieldGrid(section, min_column_width=110, max_columns=5)
        grid.add_field("对齐", self._viewport_align_combo)

        self._viewport_x_spin = _spin(-4000, 4000)
        self._viewport_x_spin.valueChanged.connect(
            lambda value: self._update_style(viewport_offset_x=value)
        )
        grid.add_field("位置 X", self._viewport_x_spin)

        self._viewport_y_spin = _spin(-4000, 4000)
        self._viewport_y_spin.valueChanged.connect(
            lambda value: self._update_style(viewport_offset_y=value)
        )
        grid.add_field("位置 Y", self._viewport_y_spin)

        self._viewport_scale_spin = _spin(10, 400, suffix=" %")
        self._viewport_scale_spin.valueChanged.connect(
            lambda value: self._update_style(viewport_scale_pct=value)
        )
        grid.add_field("缩放", self._viewport_scale_spin)

        self._viewport_rotation_spin = _spin(-180, 180, suffix=" °")
        self._viewport_rotation_spin.valueChanged.connect(
            lambda value: self._update_style(viewport_rotation_deg=value)
        )
        grid.add_field("旋转", self._viewport_rotation_spin)

        layout.addWidget(grid)
        return section

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
        """行结构：示意图居中，锚定/余白/行布局按空间语义贴边（N3 式编排）。"""
        section, layout = _plain_card()
        self._layout_section = section
        navigation = self._make_layout_navigation(section)
        assignment_actions = self._make_layout_assignment_actions(section)

        # 上下配置贴幕布中上；智能水平占用原来的左上位置。
        self._line_position_seg = _GlyphSegment(_POSITION_SEGMENT_OPTIONS, section)
        self._line_position_seg.setValue("bottom")
        self._line_position_seg.valueChanged.connect(self._on_line_position_changed)
        self._line_position_field = _field(
            "上下配置", self._line_position_seg
        )

        self._horizontal_margin_spin = _spin(
            0, _LAYOUT_SIZE_MAX_PX, suffix=" px"
        )
        # 三位数 + 单位足够；必须改回 Fixed 策略——_compact_control 的
        # Ignored 策略在带对齐的网格单元里会让 sizeHint 归零挤扁控件
        self._horizontal_margin_spin.setFixedWidth(120)
        self._horizontal_margin_spin.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed
        )
        self._horizontal_margin_spin.setToolTip(
            "左右余白（N3 左右余白）：左对齐行的左缘贴此值，右对齐行的右缘贴"
            "「画面宽 − 此值」。"
        )
        self._horizontal_margin_spin.valueChanged.connect(
            self._on_horizontal_margin_changed
        )
        self._horizontal_margin_field = _field(
            "左右余白", self._horizontal_margin_spin
        )

        self._smart_horizontal_field = self._make_smart_horizontal_field(section)
        self._left_layout_controls = QWidget(section)
        self._left_layout_controls.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        left_controls_layout = QVBoxLayout(self._left_layout_controls)
        left_controls_layout.setContentsMargins(0, 0, 0, 0)
        left_controls_layout.setSpacing(8)
        left_controls_layout.addWidget(
            self._smart_horizontal_field, 0, Qt.AlignmentFlag.AlignLeft
        )
        left_controls_layout.addWidget(
            self._horizontal_margin_field, 0, Qt.AlignmentFlag.AlignRight
        )
        self._character_layout_group = self._make_character_layout_group(section)

        self._allow_biting_check = CheckBox("启用文字咬合", section)
        self._allow_biting_check.setToolTip(
            "允许斜体和部分标点使用负字形边距，效果更接近 NicokaraMaker3。"
        )
        self._allow_biting_check.toggled.connect(
            lambda checked: self._update_layout_field(allow_biting=checked)
        )

        # 中列：布局示意图；下方用单行表单贴上/下余白。
        self._layout_schematic = _LayoutSchematic(section)
        # 初始固定宽度必须在父面板构建阶段设置；若提前到子控件构造器中，
        # QScrollArea 会缓存过大的最小宽度，320px 窄面板将产生横向溢出。
        self._layout_schematic.setFixedWidth(round(150 * 16 / 9))

        self._line_margin_spin = _spin(0, _LAYOUT_SIZE_MAX_PX, suffix=" px")
        self._line_margin_spin.setFixedWidth(120)
        self._line_margin_spin.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed
        )
        self._line_margin_spin.setToolTip(
            "顶部锚定 = 画面上端到最上行的余白；底部锚定 = 画面下端到最下行的"
            "余白；居中时忽略（N3 上/下余白）。"
        )
        self._line_margin_spin.valueChanged.connect(
            lambda value: self._update_layout_field(line_y_margin_px=value)
        )

        self._vertical_margin_field = QWidget(section)
        # 居中锚定时余白字段隐藏但保留占位，避免示意图与左下的字符排版
        # 随行高塌缩整体上移（位置稳定 > 空行观感）。
        retain_policy = self._vertical_margin_field.sizePolicy()
        retain_policy.setRetainSizeWhenHidden(True)
        self._vertical_margin_field.setSizePolicy(retain_policy)
        vertical_margin_layout = QHBoxLayout(self._vertical_margin_field)
        vertical_margin_layout.setContentsMargins(0, 0, 0, 0)
        vertical_margin_layout.setSpacing(8)
        self._vertical_margin_label = QLabel("下余白", self._vertical_margin_field)
        themed(
            self._vertical_margin_label,
            lambda: f"color: {palette().text_secondary}; font-size: 9pt;",
        )
        vertical_margin_layout.addWidget(self._vertical_margin_label)
        vertical_margin_layout.addWidget(self._line_margin_spin)

        self._schematic_board = _SchematicBoard(
            QWidget(section),
            self._layout_schematic,
            self._vertical_margin_field,
            self._make_line_alignments_box(section),
            section,
            header_left=navigation,
            header_right=assignment_actions,
            top_left=self._left_layout_controls,
            top_center=self._line_position_field,
            bottom_left=self._character_layout_group,
            bottom_right=self._allow_biting_check,
        )
        layout.addWidget(self._schematic_board)
        return section

    def _make_vertical_layout_section(self) -> QFrame:
        """垂直与方向：行间距 + 书写方向开关。

        上下配置/上下余白已随布局示意图移入「行结构」（位置即语义）；
        书写方向只有两个复选框，独立成卡片浪费一整个卡片头。
        """
        section, layout = _section("垂直与方向")

        self._line_gap_spin = _spin(
            -_LAYOUT_SIZE_MAX_PX, _LAYOUT_SIZE_MAX_PX, suffix=" px"
        )
        self._line_gap_spin.setFixedWidth(120)
        # _spin 默认水平 Ignored，会被 HBox 压到最小宽把「px」单位裁掉。
        self._line_gap_spin.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed
        )
        self._line_gap_spin.setToolTip(
            "相邻两行主文字行盒之间的间距（N3 行間），可为负让行盒重叠；"
            "不包含注音高度。"
        )
        self._line_gap_spin.valueChanged.connect(
            lambda value: self._update_layout_field(line_gap_px=value)
        )
        compact_row = QWidget(section)
        compact_layout = QHBoxLayout(compact_row)
        compact_layout.setContentsMargins(0, 0, 0, 0)
        compact_layout.setSpacing(12)
        compact_layout.addWidget(_field("行间距", self._line_gap_spin), 0)
        self._vertical_check = CheckBox("竖排", compact_row)
        self._vertical_check.toggled.connect(
            lambda checked: self._update_style(vertical=checked)
        )
        compact_layout.addWidget(self._vertical_check, 0, Qt.AlignmentFlag.AlignBottom)
        self._rtl_check = CheckBox("从右到左", compact_row)
        self._rtl_check.toggled.connect(
            lambda checked: self._update_style(right_to_left=checked)
        )
        compact_layout.addWidget(self._rtl_check, 0, Qt.AlignmentFlag.AlignBottom)
        self._allow_inter_page_line_overlap_check = CheckBox(
            "启用行间重叠", compact_row
        )
        self._allow_inter_page_line_overlap_check.setToolTip(
            "关闭时，系统会根据字幕正文、注音、描边、阴影和发光在布局完成后的"
            "绘制范围，自动移动后进入的整页字幕，避免不同页面互相遮挡；不会"
            "改变演唱时间，入场、退场和字符动画允许互相穿越。开启后保留旧的"
            "行位判断和过渡时间处理，允许跨页字幕"
            "重叠，适合需要刻意叠放的特殊效果。同一页内部的负行间距或手工重叠"
            "不受此开关影响。"
        )
        self._allow_inter_page_line_overlap_check.toggled.connect(
            lambda checked: self._update_style(
                allow_inter_page_line_overlap=checked
            )
        )
        compact_layout.addWidget(
            self._allow_inter_page_line_overlap_check,
            0,
            Qt.AlignmentFlag.AlignBottom,
        )
        compact_layout.addStretch(1)
        layout.addWidget(compact_row)
        return section

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
        index = self._current_layout_index()
        return self._style if index == 0 else self._style.layouts[index - 1]

    def _current_layout_values(self) -> dict:
        source = self._current_layout_source()
        values: dict[str, Any] = {}
        for name in LYRICS_LAYOUT_FIELDS:
            value = getattr(source, name)
            if value is None:
                value = getattr(self._style, name)
            values[name] = deepcopy(value)
        return values

    def _update_layout_field(self, **changes) -> None:
        if self._syncing:
            return
        index = self._current_layout_index()
        if index <= 0:
            self._update_style(_force_global=True, **changes)
            return
        layouts = list(self._style.layouts)
        layouts[index - 1] = replace(layouts[index - 1], **changes)
        self._update_style(layouts=layouts)

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
        values = self._current_layout_values()
        existing = {layout.name for layout in self._style.layouts}
        number = len(self._style.layouts) + 1
        name = f"布局 {number}"
        while name in existing:
            number += 1
            name = f"布局 {number}"
        layouts = list(self._style.layouts) + [LyricsLayout(name=name, **values)]
        self._update_style(layouts=layouts)
        self._refresh_layout_combo(selected=len(layouts))
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
        layouts = list(self._style.layouts)
        layouts[index - 1] = replace(layouts[index - 1], name=new)
        self._update_style(layouts=layouts)
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
        layouts = list(self._style.layouts)
        del layouts[index - 1]
        changes: dict[str, Any] = {"layouts": layouts}
        # 标题的布局引用与歌词行一样修正：被删的回默认，其后的序号前移。
        title = self._style.title_overlay
        if title is not None and title.layout_index is not None:
            title_index = int(title.layout_index)
            if title_index == index:
                changes["title_overlay"] = replace(title, layout_index=0)
            elif title_index > index:
                changes["title_overlay"] = replace(title, layout_index=title_index - 1)
        self._update_style(**changes)
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
        section, layout = _section("时间")

        grid = _ResponsiveFieldGrid(section, min_column_width=130, max_columns=4)

        self._line_lead_spin = _spin(0, 10_000, suffix=" ms")
        self._line_lead_spin.valueChanged.connect(
            lambda value: self._update_style(line_lead_in_ms=value)
        )
        grid.add_field("提前入场", self._line_lead_spin)

        self._line_tail_spin = _spin(0, 10_000, suffix=" ms")
        self._line_tail_spin.valueChanged.connect(
            lambda value: self._update_style(line_tail_ms=value)
        )
        grid.add_field("延迟退场", self._line_tail_spin)

        self._line_offset_spin = _spin(-10_000, 10_000, suffix=" ms")
        self._line_offset_spin.valueChanged.connect(
            lambda value: self._update_style(timing_offset_ms=value)
        )
        grid.add_field("偏移", self._line_offset_spin)

        self._section_gap_spin = _spin(0, 60_000, suffix=" ms")
        self._section_gap_spin.valueChanged.connect(
            lambda value: self._update_style(section_gap_ms=value)
        )
        # 分段间隔属于字幕源加载规则，已移至歌词列表工具栏的齿轮面板。
        self._section_gap_spin.setVisible(False)

        self._section_ending_combo = _WheelFocusedComboBox(section)
        _compact_control(self._section_ending_combo)
        for label, value in [("保持", "hold"), ("段末清屏", "clear")]:
            self._section_ending_combo.addItem(label, value)
        self._section_ending_combo.currentIndexChanged.connect(
            lambda _index: self._update_style(
                section_ending_mode=self._section_ending_combo.currentData()
            )
        )
        grid.add_field("段落结束", self._section_ending_combo)

        self._lane_gap_spin = _spin(0, 5_000, suffix=" ms")
        self._lane_gap_spin.setToolTip("同一显示轨上相邻两句之间保留的时间间隔。")
        self._lane_gap_spin.valueChanged.connect(
            lambda value: self._update_style(line_lane_gap_ms=value)
        )
        grid.add_field("同轨间隔", self._lane_gap_spin)

        layout.addWidget(grid)

        sync_row = QHBoxLayout()
        sync_row.setContentsMargins(0, 0, 0, 0)
        self._sync_entry_check = CheckBox("同步入场", section)
        self._sync_entry_check.toggled.connect(
            lambda checked: self._update_style(sync_entry=checked)
        )
        sync_row.addWidget(self._sync_entry_check)

        self._sync_ending_check = CheckBox("同步退场", section)
        self._sync_ending_check.toggled.connect(
            lambda checked: self._update_style(sync_ending=checked)
        )
        sync_row.addWidget(self._sync_ending_check)
        sync_row.addStretch(1)
        layout.addLayout(sync_row)

        self._ruby_main_reading_units_check = CheckBox(
            "正文按注音字符切分（N3 式）", section
        )
        self._ruby_main_reading_units_check.setToolTip(
            "正文内部已有时间点时，两种模式都会保留正文逐字时钟；"
            "缺失时，开启按注音可视字符数映射，"
            "关闭按注音内部时间点形成的时间段数均分正文。"
        )
        self._ruby_main_reading_units_check.toggled.connect(
            lambda checked: self._update_style(
                ruby_main_progress_mode=(
                    "reading_units" if checked else "checkpoint_segments"
                )
            )
        )
        layout.addWidget(self._ruby_main_reading_units_check)
        return section

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
        value = self._scheme_value("karaoke_colors")
        if isinstance(value, KaraokeColors):
            return deepcopy(value)
        return _legacy_colors_from_panel(self)

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
        if not hasattr(self, "_fill_mode_combo"):
            return
        fill = self._current_paint_fill()
        was_syncing = self._syncing
        self._syncing = True
        try:
            mode_index = max(0, self._fill_mode_combo.findData(fill.mode))
            self._fill_mode_combo.setCurrentIndex(mode_index)
            self._fill_editor_stack.setCurrentIndex(_fill_stack_index(fill.mode))
            self._fill_editor_stack.updateGeometry()
            self._paint_solid_btn.set_color(fill.color)
            self._paint_gradient_start_btn.set_color(fill.start_color)
            self._paint_gradient_end_btn.set_color(fill.end_color)
            self._gradient_editor.set_orientation(fill.mode)
            self._arrange_stop_editor(
                self._gradient_editor_layout,
                self._gradient_bar_field,
                self._gradient_color_field,
                self._gradient_position_field,
                vertical=fill.mode == "gradient_vertical",
                footer=self._ruby_horizontal_gradient_with_main_check,
            )
            self._ruby_horizontal_gradient_with_main_check.setChecked(
                bool(self._scheme_value("ruby_horizontal_gradient_with_main"))
            )
            self._ruby_horizontal_gradient_with_main_check.setVisible(
                fill.mode == "gradient_horizontal"
            )
            self._gradient_editor.set_stops(_gradient_stops(fill))
            self._sync_gradient_stop_controls()
            self._split_editor.set_stops(_split_stops(fill))
            self._sync_split_stop_controls()
            self._paint_image_path_edit.setText(fill.image_path)
            self._paint_image_scale_spin.setValue(fill.image_scale_pct)
            self._sync_decoration_visibility()
        finally:
            self._syncing = was_syncing

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
        for name in self._role_names:
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
        schemes = dict(self._style.custom_style_schemes)
        original = name
        suffix = 2
        while name in schemes:
            name = f"{original} {suffix}"
            suffix += 1
        schemes[name] = _scheme_from_current(self)
        if name not in self._role_names:
            self._role_names.append(name)
        self._update_style(custom_style_schemes=schemes)
        self._syncing = True
        try:
            self._refresh_scheme_combo(f"{_CUSTOM_SCHEME_PREFIX}{name}")
        finally:
            self._syncing = False
        self._sync_subtitle_scheme_controls()

    def _open_preset_manager(self) -> None:
        dialog = StylePresetManagerDialog(
            presets=self._preset_schemes,
            current_scheme=_scheme_from_current(self),
            target_label=self._current_target_label(),
            existing_role_names=set(self._role_names) | {TITLE_SCHEME_NAME},
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
            scheme=deepcopy(_scheme_from_current(self)),
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
        role_names = list(self._role_names)
        style_schemes = dict(self._style.custom_style_schemes)
        for preset in _normalize_style_presets(schemes).values():
            name = str(preset.name).strip()
            if not name or name == TITLE_SCHEME_NAME or name in role_names:
                continue
            style_schemes[name] = deepcopy(preset.scheme)
            role_names.append(name)
        self._role_names = role_names
        self._update_style(custom_style_schemes=style_schemes)
        self._syncing = True
        try:
            self._refresh_scheme_combo(self._current_scheme_key())
        finally:
            self._syncing = False
        self._sync_subtitle_scheme_controls()

    def _ensure_role_schemes(self) -> None:
        schemes = dict(self._style.custom_style_schemes)
        changed = False
        for index, name in enumerate(self._role_names):
            if name in schemes:
                continue
            matches = [
                preset
                for preset in self._preset_schemes.values()
                if preset.name == name
            ]
            if len(matches) == 1:
                schemes[name] = deepcopy(matches[0].scheme)
            else:
                schemes[name] = _auto_role_scheme(_scheme_from_current(self), index)
            changed = True
        if changed:
            self._style = replace(self._style, custom_style_schemes=schemes)
        if changed:
            self.styleChanged.emit(self._style)

    def _current_target_label(self) -> str:
        role_name = self._current_custom_scheme_name()
        return role_name if role_name is not None else "全局默认"

    def _apply_preset_to_current_target(self, scheme: SubtitleStyleScheme) -> None:
        role_name = self._current_custom_scheme_name()
        if role_name is not None:
            schemes = dict(self._style.custom_style_schemes)
            schemes[role_name] = deepcopy(scheme)
            if role_name not in self._role_names:
                self._role_names.append(role_name)
            self._update_style(custom_style_schemes=schemes)
            return
        changes = _style_scheme_changes(scheme)
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
        if not self._syncing:
            self.schemeSelectionChanged.emit(self.current_scheme_key())

    def _current_custom_scheme_name(self) -> Optional[str]:
        key = self._current_scheme_key()
        if key is None or not key.startswith(_CUSTOM_SCHEME_PREFIX):
            return None
        return key.removeprefix(_CUSTOM_SCHEME_PREFIX)

    def _scheme_value(self, field_name: str):
        role_name = self._current_custom_scheme_name()
        if role_name is not None:
            scheme = self._style.custom_style_schemes.get(role_name)
            if (
                field_name == "karaoke_colors"
                and scheme is not None
                and scheme.karaoke_colors is None
                and _scheme_has_legacy_color_values(scheme)
            ):
                return None
            if field_name == "ruby_karaoke_colors" and scheme is not None:
                return scheme.ruby_karaoke_colors
            value = getattr(scheme, field_name, None) if scheme is not None else None
            if (
                scheme is not None
                and scheme.n3_font_inheritance
                and field_name in N3_FONT_INHERITANCE_FIELDS
            ):
                return value
            if value is not None:
                return value
        return getattr(self._style, field_name)

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
        if new in self._role_names or new in self._style.custom_style_schemes:
            InfoBar.warning(
                title="名称已存在",
                content=f"项目中已经存在角色“{new}”。",
                parent=self,
                duration=2000,
            )
            return
        schemes = dict(self._style.custom_style_schemes)
        scheme = schemes.get(old) or _scheme_from_current(self)
        if old in schemes:
            del schemes[old]
        schemes[new] = scheme
        # 角色名来自字幕标签（role_names）的，重命名后也从可选名单里替换掉旧名。
        if old in self._role_names:
            self._role_names = [new if n == old else n for n in self._role_names]
        self._update_style(custom_style_schemes=schemes)
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
        schemes = dict(self._style.custom_style_schemes)
        schemes.pop(name, None)
        if name in self._role_names:
            self._role_names = [n for n in self._role_names if n != name]
        self._update_style(custom_style_schemes=schemes)
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
        if not _force_global and changes and set(changes).issubset(_SCHEME_FIELDS):
            role_name = self._current_custom_scheme_name()
            if role_name is not None:
                # 当前选中某个角色 → 编辑进该角色（按名字存进 custom_style_schemes）。
                schemes = dict(self._style.custom_style_schemes)
                scheme = schemes.get(role_name) or _scheme_from_current(self)
                schemes[role_name] = replace(scheme, **changes)
                changes = {"custom_style_schemes": schemes}
        if "line_y_position" in changes:
            changes["line_y_position"] = _normalize_line_position(changes["line_y_position"])
        if "line_horizontal_layout" in changes:
            changes["line_horizontal_layout"] = _normalize_horizontal_layout(
                changes["line_horizontal_layout"]
            )
        for align_field in ("row1_align", "row2_align"):
            if align_field in changes:
                changes[align_field] = _normalize_horizontal_align(changes[align_field])
        if "viewport_align" in changes:
            changes["viewport_align"] = _normalize_viewport_align(changes["viewport_align"])
        if "section_ending_mode" in changes:
            changes["section_ending_mode"] = (
                changes["section_ending_mode"]
                if changes["section_ending_mode"] in {"hold", "clear"}
                else "hold"
            )
        if "decoration_kind" in changes:
            changes["decoration_kind"] = _normalize_decoration_kind(
                changes["decoration_kind"]
            )
        if "ruby_decoration_kind" in changes:
            changes["ruby_decoration_kind"] = (
                None
                if changes["ruby_decoration_kind"] is None
                else _normalize_decoration_kind(changes["ruby_decoration_kind"])
            )
        if "entry_anim" in changes:
            changes["entry_anim"] = _normalize_entry_animation(changes["entry_anim"])
        if "exit_anim" in changes:
            changes["exit_anim"] = _normalize_exit_animation(changes["exit_anim"])
        if "karaoke_anim" in changes:
            changes["karaoke_anim"] = _normalize_karaoke_animation(
                changes["karaoke_anim"]
            )
        if "lit_style" in changes:
            changes["lit_style"] = _normalize_lit_style(changes["lit_style"])
        if "lit_transition_mode" in changes:
            changes["lit_transition_mode"] = _normalize_lit_transition_mode(
                changes["lit_transition_mode"]
            )
        self._style = replace(self._style, **changes)
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
        self.styleChanged.emit(self._style)


def _normalize_line_position(value: object) -> LineYPosition:
    if value in {"top", "center", "bottom"}:
        return value  # type: ignore[return-value]
    return "bottom"


def _normalize_horizontal_layout(value: object) -> LineHorizontalLayout:
    if value in {"asymmetric", "center", "per_row"}:
        return value  # type: ignore[return-value]
    return "asymmetric"


def _normalize_horizontal_align(value: object) -> HorizontalAlign:
    if value in HORIZONTAL_ALIGNS:
        return value  # type: ignore[return-value]
    return "left"


def _normalize_viewport_align(value: object) -> ViewportAlign:
    if value in VIEWPORT_ALIGNS:
        return value  # type: ignore[return-value]
    return "center"


def _normalize_decoration_kind(value: object) -> DecorationKind:
    if value in {"none", "shadow", "glow"}:
        return value  # type: ignore[return-value]
    return "shadow"


def _scaled_panel_px(value: int, scale: float) -> int:
    if value <= 0:
        return 0
    return max(1, int(round(value * scale)))


def _scaled_panel_signed_px(value: int, scale: float) -> int:
    if value == 0:
        return 0
    sign = 1 if value > 0 else -1
    return sign * max(1, int(round(abs(value) * scale)))


def _normalize_entry_animation(value: object) -> EntryAnimation:
    if value in {"none", "fade", "slide_in", "rise", "char_fade", "char_drip", "spin_flip", "utopia"}:
        return value  # type: ignore[return-value]
    return "none"


def _normalize_exit_animation(value: object) -> ExitAnimation:
    if value in {"none", "fade", "slide_out", "rise", "char_fade", "char_drip", "spin_flip", "utopia"}:
        return value  # type: ignore[return-value]
    return "none"


def _normalize_karaoke_animation(value: object) -> KaraokeAnimation:
    if value in {"inherit", "none", "utopia"}:
        return value  # type: ignore[return-value]
    return "inherit"


def _normalize_lit_style(value: object):
    if value in {"volume", "circle", "square", "rounded"}:
        return value
    return "volume"


def _normalize_lit_transition_mode(value: object) -> str:
    if value in {"none", "fade", "slide"}:
        return str(value)
    return "fade"


def _fill_stack_index(mode: str) -> int:
    if mode in {"gradient_horizontal", "gradient_vertical"}:
        return 1
    if mode == "split_vertical":
        return 2
    if mode == "image":
        return 3
    return 0


def _normalized_stop_position(value: object) -> float:
    try:
        position = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        position = 0.0
    if not math.isfinite(position):
        position = 0.0
    position = round(max(0.0, min(100.0, position)), 6)
    return int(position) if position.is_integer() else position


_GRADIENT_STOPS_FORMAT = "karaoke-studio/gradient-stops"
_GRADIENT_STOPS_VERSION = 1


def _gradient_stops_to_json(stops: list[tuple[float, str]]) -> str:
    normalized = _normalize_gradient_stops(stops)
    payload = {
        "format": _GRADIENT_STOPS_FORMAT,
        "version": _GRADIENT_STOPS_VERSION,
        "stops": [
            {"position": position, "color": color}
            for position, color in normalized
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _gradient_stops_from_json(text: str) -> list[tuple[float, str]]:
    if not text.strip():
        raise ValueError("请输入渐变信息。")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"JSON 格式错误：第 {exc.lineno} 行，第 {exc.colno} 列。"
        ) from exc
    if not isinstance(payload, dict):
        raise ValueError("渐变信息必须是 JSON 对象。")
    if payload.get("format") != _GRADIENT_STOPS_FORMAT:
        raise ValueError("无法识别该渐变信息格式。")
    version = payload.get("version")
    if type(version) is not int or version != _GRADIENT_STOPS_VERSION:
        raise ValueError(f"不支持的渐变信息版本：{version!r}。")
    raw_stops = payload.get("stops")
    if not isinstance(raw_stops, list) or len(raw_stops) < 2:
        raise ValueError("渐变信息至少需要两个关键点。")

    stops: list[tuple[float, str]] = []
    for index, item in enumerate(raw_stops, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"第 {index} 个关键点必须是 JSON 对象。")
        raw_position = item.get("position")
        if isinstance(raw_position, bool) or not isinstance(
            raw_position, (int, float)
        ):
            raise ValueError(f"第 {index} 个关键点的位置必须是数字。")
        position = float(raw_position)
        if not math.isfinite(position) or not 0 <= position <= 100:
            raise ValueError(f"第 {index} 个关键点的位置必须在 0 到 100 之间。")
        raw_color = item.get("color")
        color = _parse_hex_color(raw_color) if isinstance(raw_color, str) else None
        if color is None:
            raise ValueError(f"第 {index} 个关键点的色号无效。")
        stops.append((_normalized_stop_position(position), color))
    return _normalize_gradient_stops(stops)


def _normalize_gradient_stops(
    stops: list[tuple[float, str]],
) -> list[tuple[float, str]]:
    normalized: list[tuple[float, str]] = []
    for position, color in stops:
        normalized.append(
            (_normalized_stop_position(position), _normalize_hex(str(color), "#FFFFFF"))
        )
    normalized.sort(key=lambda item: item[0])
    if not normalized:
        return [(0, "#FFFFFF"), (100, "#FFFFFF")]
    positions = {position for position, _color in normalized}
    if 0 not in positions:
        normalized.insert(0, (0, normalized[0][1]))
    if 100 not in positions:
        normalized.append((100, normalized[-1][1]))
    return normalized


def _gradient_stops(fill: PaintFill) -> list[tuple[float, str]]:
    if fill.gradient_stops:
        return _normalize_gradient_stops(fill.gradient_stops)
    return _normalize_gradient_stops([(0, fill.start_color), (100, fill.end_color)])


def _split_stops(fill: PaintFill) -> list[tuple[float, str]]:
    if fill.split_stops:
        return _normalize_gradient_stops(fill.split_stops)
    return _normalize_gradient_stops(
        [
            (0, fill.split_top_color),
            (fill.split_position_pct, fill.split_bottom_color),
            (100, fill.split_bottom_color),
        ]
    )


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


def _legacy_colors_from_panel(panel: PropertyPanel) -> KaraokeColors:
    before = KaraokeColorState(
        text=_solid_fill(str(panel._scheme_value("base_color"))),
        stroke=_solid_fill(str(panel._scheme_value("stroke_color"))),
        stroke2=_solid_fill("#000000"),
        shadow=_solid_fill(str(panel._scheme_value("shadow_color"))),
    )
    after = KaraokeColorState(
        text=_legacy_after_text_fill(panel),
        stroke=_solid_fill(str(panel._scheme_value("stroke_color"))),
        stroke2=_solid_fill("#000000"),
        shadow=_solid_fill(str(panel._scheme_value("shadow_color"))),
    )
    return KaraokeColors(before=before, after=after)


def _legacy_after_text_fill(panel: PropertyPanel) -> PaintFill:
    fill_color = str(panel._scheme_value("fill_color"))
    if not bool(panel._scheme_value("fill_gradient_enabled")):
        return _solid_fill(fill_color)
    mode = (
        "gradient_vertical"
        if int(panel._scheme_value("fill_gradient_angle_deg")) in {90, 270}
        else "gradient_horizontal"
    )
    return PaintFill(
        mode=mode,
        color=fill_color,
        start_color=str(panel._scheme_value("fill_gradient_start_color")),
        end_color=str(panel._scheme_value("fill_gradient_end_color")),
        gradient_stops=[
            (0, str(panel._scheme_value("fill_gradient_start_color"))),
            (100, str(panel._scheme_value("fill_gradient_end_color"))),
        ],
        split_top_color=str(panel._scheme_value("fill_gradient_start_color")),
        split_bottom_color=str(panel._scheme_value("fill_gradient_end_color")),
    )


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


def _scheme_from_current(panel: PropertyPanel) -> SubtitleStyleScheme:
    current_name = panel._current_custom_scheme_name()
    current_scheme = (
        panel._style.custom_style_schemes.get(current_name)
        if current_name is not None
        else None
    )
    return SubtitleStyleScheme(
        font_family=str(panel._scheme_value("font_family")),
        font_family_latin=panel._scheme_value("font_family_latin"),
        font_size_px=int(panel._scheme_value("font_size_px")),
        latin_font_size_px=panel._scheme_value("latin_font_size_px"),
        latin_font_weight=panel._scheme_value("latin_font_weight"),
        latin_stroke_width_px=panel._scheme_value("latin_stroke_width_px"),
        latin_stroke2_enabled=panel._scheme_value("latin_stroke2_enabled"),
        latin_stroke2_width_px=panel._scheme_value("latin_stroke2_width_px"),
        letter_spacing_px=int(panel._scheme_value("letter_spacing_px")),
        space_width_percent=int(panel._scheme_value("space_width_percent")),
        allow_biting=bool(panel._scheme_value("allow_biting")),
        font_weight=int(panel._scheme_value("font_weight")),
        italic=bool(panel._scheme_value("italic")),
        affects_ruby_anchor=bool(panel._scheme_value("affects_ruby_anchor")),
        base_color=str(panel._scheme_value("base_color")),
        fill_color=str(panel._scheme_value("fill_color")),
        fill_gradient_enabled=bool(panel._scheme_value("fill_gradient_enabled")),
        fill_gradient_start_color=str(panel._scheme_value("fill_gradient_start_color")),
        fill_gradient_end_color=str(panel._scheme_value("fill_gradient_end_color")),
        fill_gradient_angle_deg=int(panel._scheme_value("fill_gradient_angle_deg")),
        stroke_color=str(panel._scheme_value("stroke_color")),
        stroke_width_px=int(panel._scheme_value("stroke_width_px")),
        stroke2_enabled=bool(panel._scheme_value("stroke2_enabled")),
        stroke2_width_px=int(panel._scheme_value("stroke2_width_px")),
        decoration_kind=_normalize_decoration_kind(panel._scheme_value("decoration_kind")),
        glow_radius_px=int(panel._scheme_value("glow_before_radius_px")),
        glow_before_radius_px=int(panel._scheme_value("glow_before_radius_px")),
        glow_after_radius_px=int(panel._scheme_value("glow_after_radius_px")),
        glow_concentration_level=int(panel._scheme_value("glow_concentration_level")),
        shadow_color=str(panel._scheme_value("shadow_color")),
        shadow_offset_x=int(panel._scheme_value("shadow_offset_x")),
        shadow_offset_y=int(panel._scheme_value("shadow_offset_y")),
        ruby_font_size_px=int(panel._scheme_value("ruby_font_size_px")),
        ruby_font_family=panel._scheme_value("ruby_font_family"),
        ruby_font_family_latin=panel._scheme_value("ruby_font_family_latin"),
        ruby_font_weight=panel._scheme_value("ruby_font_weight"),
        ruby_latin_font_size_px=panel._scheme_value("ruby_latin_font_size_px"),
        ruby_latin_font_weight=panel._scheme_value("ruby_latin_font_weight"),
        ruby_font_follow_main=bool(panel._scheme_value("ruby_font_follow_main")),
        ruby_color=str(panel._scheme_value("ruby_color")),
        ruby_gap_px=int(panel._scheme_value("ruby_gap_px")),
        ruby_stroke_width_px=panel._scheme_value("ruby_stroke_width_px"),
        ruby_stroke2_enabled=panel._scheme_value("ruby_stroke2_enabled"),
        ruby_stroke2_width_px=panel._scheme_value("ruby_stroke2_width_px"),
        ruby_latin_stroke_width_px=panel._scheme_value(
            "ruby_latin_stroke_width_px"
        ),
        ruby_latin_stroke2_enabled=panel._scheme_value(
            "ruby_latin_stroke2_enabled"
        ),
        ruby_latin_stroke2_width_px=panel._scheme_value(
            "ruby_latin_stroke2_width_px"
        ),
        ruby_decoration_kind=panel._scheme_value("ruby_decoration_kind"),
        ruby_glow_radius_px=panel._scheme_value("ruby_glow_radius_px"),
        ruby_glow_before_radius_px=panel._scheme_value("ruby_glow_before_radius_px"),
        ruby_glow_after_radius_px=panel._scheme_value("ruby_glow_after_radius_px"),
        ruby_glow_concentration_level=panel._scheme_value(
            "ruby_glow_concentration_level"
        ),
        ruby_shadow_offset_x=panel._scheme_value("ruby_shadow_offset_x"),
        ruby_shadow_offset_y=panel._scheme_value("ruby_shadow_offset_y"),
        ruby_colors_follow_main=bool(
            panel._scheme_value("ruby_colors_follow_main")
        ),
        ruby_horizontal_gradient_with_main=bool(
            panel._scheme_value("ruby_horizontal_gradient_with_main")
        ),
        karaoke_colors=panel._current_karaoke_colors(),
        ruby_karaoke_colors=panel._scheme_value("ruby_karaoke_colors"),
        n3_font_inheritance=bool(
            current_scheme is not None and current_scheme.n3_font_inheritance
        ),
    )


def _style_scheme_changes(scheme: SubtitleStyleScheme) -> dict[str, object]:
    return {
        field: value
        for field in _SCHEME_FIELDS
        if (value := getattr(scheme, field)) is not None
    }


def _scheme_has_legacy_color_values(scheme: SubtitleStyleScheme) -> bool:
    return any(
        getattr(scheme, field) is not None
        for field in (
            "base_color",
            "fill_color",
            "fill_gradient_enabled",
            "fill_gradient_start_color",
            "fill_gradient_end_color",
            "fill_gradient_angle_deg",
            "stroke_color",
            "shadow_color",
        )
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


def _compact_control(widget: QWidget) -> None:
    widget.setMinimumWidth(0)
    widget.setFixedHeight(_COMPACT_CONTROL_HEIGHT)
    widget.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    widget.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)


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


def _field(label_text: str, control: QWidget) -> QWidget:
    box = QWidget()
    box.setObjectName("SubtitlePropertyField")
    themed(box, lambda: "#SubtitlePropertyField { background: transparent; }")
    layout = QVBoxLayout(box)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(4)
    label = QLabel(label_text)
    themed(label, lambda: f"color: {palette().text_secondary}; font-size: 9pt;")
    control.setParent(box)
    layout.addWidget(label)
    layout.addWidget(control)
    return box


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


def _subgroup_label(text: str) -> QLabel:
    """A sub-section heading: accent bar + bold dark text, distinct from field labels."""
    label = QLabel(text)
    label.setObjectName("SubtitlePropertySubheading")
    themed(
        label,
        lambda: (
            f"color: {palette().title_text};"
            "font-size: 9.5pt;"
            "font-weight: 700;"
            f"border-left: 3px solid {palette().accent_primary};"
            "padding: 0 0 0 8px;"
        ),
    )
    return label


def _section(
    title: str, *, switch: bool = False
) -> tuple[CollapsibleSection, QVBoxLayout]:
    section = CollapsibleSection(title, switch=switch)
    themed(
        section,
        lambda: (
            f"""
            QFrame#SubtitlePropertySection {{
                background: {palette().card_bg};
                border: 1px solid {palette().card_border};
                border-radius: 8px;
            }}
            QToolButton#SubtitlePropertySectionHeader {{
                color: {palette().title_text};
                border: 0;
                padding: 10px 12px;
                font-size: 10.5pt;
                font-weight: 700;
                text-align: left;
            }}
            QToolButton#SubtitlePropertySectionHeader:hover {{
                color: {palette().accent_primary};
            }}
            QFrame#SubtitlePropertySection QWidget {{
                background: transparent;
            }}
            {control_qss("QFrame#SubtitlePropertySection")}
            """
        ),
    )
    return section, section.content_layout


def _plain_card() -> tuple[QFrame, QVBoxLayout]:
    """无标题、不折叠的属性卡片，用于顶部导航已表明编辑上下文的页面。"""
    card = QFrame()
    card.setObjectName("SubtitlePropertyCard")
    layout = QVBoxLayout(card)
    layout.setContentsMargins(12, 12, 12, 12)
    layout.setSpacing(10)
    themed(
        card,
        lambda: (
            f"""
            QFrame#SubtitlePropertyCard {{
                background: {palette().card_bg};
                border: 1px solid {palette().card_border};
                border-radius: 8px;
            }}
            QFrame#SubtitlePropertyCard QWidget {{
                background: transparent;
            }}
            {control_qss("QFrame#SubtitlePropertyCard")}
            """
        ),
    )
    return card, layout


def _section_pair(first: QWidget, second: QWidget) -> _ResponsivePropertyPair:
    """Pair two section cards side by side on wide panels, stacked when narrow.

    Cards carry their own borders, so no divider; the explicit
    ``min_side_width`` keeps each card wide enough to stay usable (the cards'
    own hints are unreliable because compact controls use ``Ignored`` width).
    """
    pair = _ResponsivePropertyPair(min_side_width=270)
    pair._layout.setSpacing(10)  # 与 _scroll_page 的卡片间距一致
    for card in (first, second):
        card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
    pair.set_widgets(first, None, second)
    return pair


def _inline_section(
    title: str, parent: Optional[QWidget] = None
) -> tuple[QWidget, QVBoxLayout]:
    section = QWidget(parent)
    layout = QVBoxLayout(section)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(10)
    layout.addWidget(_subgroup_label(title))
    return section, layout
