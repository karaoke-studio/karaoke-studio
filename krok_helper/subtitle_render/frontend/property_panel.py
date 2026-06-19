"""右侧字幕属性面板。

窄侧栏里不要使用横向表单布局：标签和输入框会互相挤压，尤其是
``QFontComboBox``。这里采用工具软件常见的分组卡片 + 垂直字段，保证
280-320px 宽度下没有横向溢出。
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
from typing import Any, Optional

from PyQt6.QtCore import QPointF, QRectF, QSize, Qt, pyqtSignal as Signal
from PyQt6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPolygonF
from PyQt6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QComboBox,
    QFileDialog,
    QFontComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from krok_helper.subtitle_render.frontend.theme import control_qss, palette, themed
from krok_helper.subtitle_render.models import (
    ColorLayerKey,
    ColorStateKey,
    DecorationKind,
    EntryAnimation,
    ExitAnimation,
    HORIZONTAL_ALIGNS,
    HorizontalAlign,
    KaraokeColors,
    KaraokeColorState,
    LineHorizontalLayout,
    LineYPosition,
    PaintFill,
    SubtitleStyleScheme,
    Style,
    VIEWPORT_ALIGNS,
    ViewportAlign,
)

_SCHEME_FIELDS = {
    "font_family",
    "font_size_px",
    "font_weight",
    "italic",
    "base_color",
    "fill_color",
    "fill_gradient_enabled",
    "fill_gradient_start_color",
    "fill_gradient_end_color",
    "fill_gradient_angle_deg",
    "stroke_color",
    "stroke_width_px",
    "stroke2_width_px",
    "decoration_kind",
    "glow_radius_px",
    "shadow_color",
    "shadow_offset_x",
    "shadow_offset_y",
    "ruby_font_size_px",
    "ruby_color",
    "ruby_gap_px",
    "karaoke_colors",
}

_SINGER_FILL_PALETTE = ["#FF5A6F", "#0055FF", "#FFAA00", "#00A878", "#9B5CFF"]
_SINGER_RUBY_PALETTE = ["#FF5A6F", "#00AAFF", "#FFCC33", "#40D99A", "#C08CFF"]
_GLOBAL_SCHEME_KEY = "global"
_SINGER_SCHEME_PREFIX = "singer:"
_CUSTOM_SCHEME_PREFIX = "custom:"


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
    """Canvas/export screen settings shared by the property panel and exporter."""

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


def _placeholder_page(text: str) -> QWidget:
    page = QWidget()
    layout = QVBoxLayout(page)
    layout.setContentsMargins(20, 24, 20, 24)
    label = QLabel(text)
    label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    label.setWordWrap(True)
    themed(label, lambda: f"color: {palette().text_hint}; font-size: 10pt;")
    layout.addStretch(1)
    layout.addWidget(label)
    layout.addStretch(2)
    return page


def _normalize_hex(value: str, fallback: str = "#000000") -> str:
    color = QColor(value)
    if not color.isValid():
        color = QColor(fallback)
    return color.name(QColor.NameFormat.HexRgb).upper()


class ColorButton(QPushButton):
    """Compact color swatch button."""

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
        text_color = "#111827" if QColor(self._color).lightness() > 150 else "#FFFFFF"
        self.setText(self._color)
        self.setStyleSheet(
            f"""
            QPushButton {{
                background: {self._color};
                color: {text_color};
                border: 1px solid {palette().card_border};
                border-radius: 6px;
                padding: 0 8px;
                font-family: "Consolas", "Courier New", monospace;
                font-size: 9pt;
            }}
            QPushButton:hover {{
                border-color: {palette().accent_primary};
            }}
            """
        )


class CollapsibleSection(QFrame):
    """A property card with a clickable header and collapsible content."""

    def __init__(self, title: str, parent: Optional[QWidget] = None) -> None:
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
        self._header.clicked.connect(self.set_expanded)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._header)
        root.addWidget(self._content)

        self.content_layout = QVBoxLayout(self._content)
        self.content_layout.setContentsMargins(12, 0, 12, 12)
        self.content_layout.setSpacing(10)

    def set_expanded(self, expanded: bool) -> None:
        self._header.setChecked(expanded)
        self._header.setArrowType(
            Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow
        )
        self._content.setVisible(expanded)

    def is_expanded(self) -> bool:
        return self._content.isVisible()


class GradientStopsEditor(QWidget):
    """Compact gradient stop editor for horizontal/vertical PaintFill gradients."""

    stopsChanged = Signal(list)
    selectedChanged = Signal(int)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._stops: list[tuple[int, str]] = [(0, "#FFFFFF"), (100, "#FF5A6F")]
        self._selected = 0
        self._orientation = "horizontal"
        self._dragging = False
        self.setMinimumHeight(92)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def sizeHint(self) -> QSize:  # noqa: N802
        height = 198 if self._orientation == "vertical" else 94
        return QSize(220, height)

    @property
    def selected_index(self) -> int:
        return self._selected

    @property
    def selected_stop(self) -> tuple[int, str]:
        return self._stops[self._selected]

    def set_orientation(self, mode: str) -> None:
        orientation = "vertical" if mode == "gradient_vertical" else "horizontal"
        if orientation == self._orientation:
            return
        self._orientation = orientation
        self.setMinimumHeight(190 if orientation == "vertical" else 92)
        self.updateGeometry()
        self.update()

    def set_stops(self, stops: list[tuple[int, str]]) -> None:
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
        self._emit_stops_changed()

    def set_selected_position(self, position: int) -> None:
        self._move_selected_stop(position)

    def add_stop(self, position: int, color: Optional[str] = None) -> None:
        pos = max(0, min(100, int(position)))
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
            gradient = (
                QLinearGradient(rect.left(), rect.top(), rect.right(), rect.top())
                if self._orientation == "horizontal"
                else QLinearGradient(rect.left(), rect.top(), rect.left(), rect.bottom())
            )
            for position, color in self._stops:
                gradient.setColorAt(position / 100.0, QColor(color))
            painter.setPen(QColor(palette().card_border))
            painter.setBrush(gradient)
            painter.drawRoundedRect(rect, 4, 4)

            rail = self._rail_rect()
            painter.setPen(QColor(palette().card_border))
            painter.drawRoundedRect(rail, 3, 3)
            for index, (position, color) in enumerate(self._stops):
                center = self._marker_center(position)
                selected = index == self._selected
                painter.setBrush(QColor(color))
                painter.setPen(QColor("#0B84FF" if selected else palette().card_bg))
                points = [
                    QPointF(center.x(), center.y() - 8),
                    QPointF(center.x() + 8, center.y()),
                    QPointF(center.x(), center.y() + 8),
                    QPointF(center.x() - 8, center.y()),
                ]
                painter.drawPolygon(QPolygonF(points))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.setPen(QColor("#0B84FF" if selected else palette().card_border))
                painter.drawPolygon(QPolygonF(points))
        finally:
            painter.end()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            return
        pos = self._position_from_point(event.position())
        nearest = self._nearest_marker_index(event.position())
        self._dragging = False
        hit_rect = self._bar_rect().adjusted(-8, -8, 8, 8).united(
            self._rail_rect().adjusted(-10, -10, 10, 10)
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

    def _bar_rect(self) -> QRectF:
        if self._orientation == "horizontal":
            return QRectF(8, 12, max(self.width() - 16, 1), 30)
        return QRectF(12, 8, 72, max(self.height() - 16, 1))

    def _rail_rect(self) -> QRectF:
        if self._orientation == "horizontal":
            return QRectF(8, 56, max(self.width() - 16, 1), 14)
        return QRectF(102, 8, 14, max(self.height() - 16, 1))

    def _marker_center(self, position: int) -> QPointF:
        pos = max(0.0, min(1.0, position / 100.0))
        if self._orientation == "horizontal":
            rail = self._rail_rect()
            return QPointF(rail.left() + rail.width() * pos, rail.center().y())
        rail = self._rail_rect()
        return QPointF(rail.center().x(), rail.top() + rail.height() * pos)

    def _position_from_point(self, point: QPointF) -> int:
        if self._orientation == "horizontal":
            rect = self._bar_rect()
            ratio = (point.x() - rect.left()) / max(rect.width(), 1.0)
        else:
            rect = self._bar_rect()
            ratio = (point.y() - rect.top()) / max(rect.height(), 1.0)
        return max(0, min(100, int(round(ratio * 100))))

    def _nearest_stop_index(self, position: int) -> Optional[int]:
        if not self._stops:
            return None
        return min(range(len(self._stops)), key=lambda index: abs(self._stops[index][0] - position))

    def _nearest_marker_index(self, point: QPointF) -> Optional[int]:
        if not self._stops:
            return None
        nearest = min(
            range(len(self._stops)),
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
        return nearest if distance_sq <= 16**2 else None

    def _index_for_position(self, position: int) -> int:
        return min(range(len(self._stops)), key=lambda index: abs(self._stops[index][0] - position))

    def _move_selected_stop(self, position: int) -> None:
        old_position, color = self._stops[self._selected]
        pos = max(0, min(100, int(position)))
        if old_position in {0, 100}:
            if pos == old_position:
                return
            self._stops.append((pos, color))
        else:
            self._stops[self._selected] = (pos, color)
        self._stops = _normalize_gradient_stops(self._stops)
        self._selected = self._index_for_position(pos)
        self._emit_stops_changed()

    def _interpolated_color(self, position: int) -> str:
        stops = _normalize_gradient_stops(self._stops)
        pos = max(0, min(100, int(position)))
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
        if left[0] == right[0]:
            return left[1]
        ratio = (pos - left[0]) / max(right[0] - left[0], 1)
        a = QColor(left[1])
        b = QColor(right[1])
        return QColor(
            round(a.red() + (b.red() - a.red()) * ratio),
            round(a.green() + (b.green() - a.green()) * ratio),
            round(a.blue() + (b.blue() - a.blue()) * ratio),
        ).name(QColor.NameFormat.HexRgb).upper()

    def _emit_stops_changed(self) -> None:
        self.update()
        self.selectedChanged.emit(self._selected)
        self.stopsChanged.emit(list(self._stops))


class _WheelFocusedSpinBox(QSpinBox):
    """Only adjust by wheel after the control has explicit focus."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.lineEdit().setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def wheelEvent(self, event):  # noqa: N802 - Qt API
        if not self.hasFocus():
            event.ignore()
            return
        super().wheelEvent(event)


class _WheelFocusedComboBox(QComboBox):
    """Avoid accidental option changes while scrolling the property panel."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def wheelEvent(self, event):  # noqa: N802 - Qt API
        if not self.hasFocus():
            event.ignore()
            return
        super().wheelEvent(event)


class _WheelFocusedFontComboBox(QFontComboBox):
    """Font list variant of the focus-gated wheel behavior."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def wheelEvent(self, event):  # noqa: N802 - Qt API
        if not self.hasFocus():
            event.ignore()
            return
        super().wheelEvent(event)


class _DynamicStackedWidget(QStackedWidget):
    """Use the current page height instead of the tallest page height."""

    def sizeHint(self) -> QSize:  # noqa: N802
        widget = self.currentWidget()
        return widget.sizeHint() if widget is not None else super().sizeHint()

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        widget = self.currentWidget()
        return widget.minimumSizeHint() if widget is not None else super().minimumSizeHint()


class PropertyPanel(QTabWidget):
    """字幕样式 / 特效 / 装饰属性面板。"""

    styleChanged = Signal(Style)
    schemeSelectionChanged = Signal(str)
    screenChanged = Signal(object)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._style = Style()
        self._screen = ScreenSettings()
        self._syncing = False
        self._singer_options: list[tuple[int, str]] = []

        self.setObjectName("PropertyPanel")
        self.setMinimumWidth(260)
        self.setDocumentMode(True)
        self.setTabPosition(QTabWidget.TabPosition.North)
        themed(
            self,
            lambda: (
                f"""
                #PropertyPanel {{ background: {palette().panel_bg}; }}
                #PropertyPanel::pane {{
                    border: 1px solid {palette().card_border};
                    border-radius: 6px;
                    background: {palette().panel_bg};
                    top: -1px;
                }}
                #PropertyPanel QTabBar::tab {{
                    min-width: 44px;
                    padding: 7px 10px;
                    color: {palette().text_secondary};
                    background: transparent;
                    border: none;
                    font-size: 9.5pt;
                }}
                #PropertyPanel QTabBar::tab:selected {{
                    color: {palette().title_text};
                    border-bottom: 2px solid {palette().accent_primary};
                }}
                #PropertyPanel QTabBar::tab:hover {{
                    color: {palette().title_text};
                }}
                """
            ),
        )

        self.addTab(self._make_basic_page(), "基本")
        self.addTab(self._make_subtitle_page(), "字幕")
        self.addTab(self._make_effects_page(), "特效")
        self.addTab(_placeholder_page("标题字幕、时段图片（B7 / P2）"), "装饰")
        self.set_singers([])
        self.set_screen_settings(self._screen, emit=False)
        self.set_style(self._style, emit=False)

    @property
    def subtitle_style(self) -> Style:
        return self._style

    @property
    def screen_settings(self) -> ScreenSettings:
        return self._screen

    def set_screen_settings(self, settings: ScreenSettings, *, emit: bool = False) -> None:
        self._screen = screen_settings_from_dict(screen_settings_to_dict(settings))
        if not hasattr(self, "_screen_preset_combo"):
            return
        self._syncing = True
        try:
            preset_index = self._screen_preset_combo.findData(self._screen.preset_key)
            self._screen_preset_combo.setCurrentIndex(max(0, preset_index))
            par_index = self._screen_par_combo.findData(self._screen.par)
            self._screen_par_combo.setCurrentIndex(max(0, par_index))
            self._screen_width_spin.setValue(self._screen.width)
            self._screen_height_spin.setValue(self._screen.height)
            fps_index = self._screen_fps_combo.findData(self._screen.fps)
            self._screen_fps_combo.setCurrentIndex(max(0, fps_index))
        finally:
            self._syncing = False
        if emit:
            self.screenChanged.emit(self._screen)

    def set_style(self, style: Style, *, emit: bool = False) -> None:
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
            self._line_position_combo.setCurrentIndex(
                max(0, self._line_position_combo.findData(self._style.line_y_position))
            )
            self._line_margin_spin.setValue(self._style.line_y_margin_px)
            self._dual_line_check.setChecked(self._style.dual_line_layout)
            self._horizontal_layout_combo.setCurrentIndex(
                max(
                    0,
                    self._horizontal_layout_combo.findData(
                        self._style.line_horizontal_layout
                    ),
                )
            )
            self._line_gap_spin.setValue(self._style.line_gap_px)
            self._upper_left_spin.setValue(self._style.upper_line_left_margin_px)
            self._lower_right_spin.setValue(self._style.lower_line_right_margin_px)
            self._row1_align_combo.setCurrentIndex(
                max(0, self._row1_align_combo.findData(self._style.row1_align))
            )
            self._row1_x_spin.setValue(self._style.row1_offset_x)
            self._row1_y_spin.setValue(self._style.row1_offset_y)
            self._row2_align_combo.setCurrentIndex(
                max(0, self._row2_align_combo.findData(self._style.row2_align))
            )
            self._row2_x_spin.setValue(self._style.row2_offset_x)
            self._row2_y_spin.setValue(self._style.row2_offset_y)
            self._sync_per_row_enabled()
            self._line_lead_spin.setValue(self._style.line_lead_in_ms)
            self._line_tail_spin.setValue(self._style.line_tail_ms)
            self._line_offset_spin.setValue(self._style.timing_offset_ms)
            self._entry_anim_combo.setCurrentIndex(
                max(0, self._entry_anim_combo.findData(self._style.entry_anim))
            )
            self._entry_lead_spin.setValue(self._style.entry_lead_ms)
            self._exit_anim_combo.setCurrentIndex(
                max(0, self._exit_anim_combo.findData(self._style.exit_anim))
            )
            self._exit_fade_spin.setValue(self._style.exit_fade_ms)
            self._sync_subtitle_scheme_controls()
        finally:
            self._syncing = False
        if emit:
            self.styleChanged.emit(self._style)

    def set_singers(self, singers: list[tuple[int, str]]) -> None:
        self._singer_options = list(singers)
        current_key = self._current_scheme_key()
        changed = self._ensure_singer_schemes()
        self._syncing = True
        try:
            self._refresh_scheme_combo(current_key)
        finally:
            self._syncing = False
        self._sync_subtitle_scheme_controls()
        if changed:
            self.styleChanged.emit(self._style)

    # ------------------------------------------------------------------ layout

    def _make_basic_page(self) -> QWidget:
        scroll, layout = _scroll_page()
        layout.addWidget(self._make_screen_section())
        layout.addWidget(self._make_viewport_section())
        layout.addWidget(self._make_position_section())
        layout.addWidget(self._make_timing_section())
        layout.addStretch(1)
        return scroll

    def _make_screen_section(self) -> QFrame:
        section, layout = _section("屏幕")

        self._screen_preset_combo = _WheelFocusedComboBox(section)
        _compact_control(self._screen_preset_combo)
        for preset in SCREEN_PRESETS:
            self._screen_preset_combo.addItem(preset.label, preset.key)
        self._screen_preset_combo.addItem("自定义", "custom")
        self._screen_preset_combo.currentIndexChanged.connect(
            lambda _index: self._on_screen_preset_changed()
        )
        layout.addWidget(_field("预设", self._screen_preset_combo))

        self._screen_par_combo = _WheelFocusedComboBox(section)
        _compact_control(self._screen_par_combo)
        for label, value in PAR_OPTIONS:
            self._screen_par_combo.addItem(label, value)
        self._screen_par_combo.currentIndexChanged.connect(
            lambda _index: self._on_screen_controls_changed()
        )
        layout.addWidget(_field("像素纵横比", self._screen_par_combo))

        row = QWidget(section)
        row_layout = QGridLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setHorizontalSpacing(8)
        row_layout.setVerticalSpacing(8)

        self._screen_width_spin = _spin(160, 7680, suffix=" px")
        self._screen_width_spin.valueChanged.connect(
            lambda _value: self._on_screen_controls_changed()
        )
        row_layout.addWidget(_field("宽度", self._screen_width_spin), 0, 0)

        self._screen_height_spin = _spin(90, 4320, suffix=" px")
        self._screen_height_spin.valueChanged.connect(
            lambda _value: self._on_screen_controls_changed()
        )
        row_layout.addWidget(_field("高度", self._screen_height_spin), 0, 1)

        self._screen_fps_combo = _WheelFocusedComboBox(section)
        _compact_control(self._screen_fps_combo)
        for fps in SCREEN_FPS_OPTIONS:
            self._screen_fps_combo.addItem(f"{fps} fps", fps)
        self._screen_fps_combo.currentIndexChanged.connect(
            lambda _index: self._on_screen_controls_changed()
        )
        row_layout.addWidget(_field("帧率", self._screen_fps_combo), 1, 0)

        row_layout.setColumnStretch(0, 1)
        row_layout.setColumnStretch(1, 1)
        layout.addWidget(row)
        return section

    def _make_subtitle_page(self) -> QWidget:
        scroll, layout = _scroll_page()
        layout.addWidget(self._make_scheme_section())
        layout.addWidget(self._make_font_section())
        layout.addWidget(self._make_ruby_section())
        layout.addWidget(self._make_color_section())
        layout.addStretch(1)
        return scroll

    def _make_font_section(self) -> QFrame:
        section, layout = _section("字体")

        self._font_combo = _WheelFocusedFontComboBox(section)
        _compact_control(self._font_combo)
        self._font_combo.currentFontChanged.connect(
            lambda font: self._update_style(font_family=font.family())
        )
        layout.addWidget(_field("字体", self._font_combo))

        row = QWidget(section)
        row_layout = QGridLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setHorizontalSpacing(8)
        row_layout.setVerticalSpacing(0)

        self._font_size_spin = _spin(12, 180, suffix=" px")
        self._font_size_spin.valueChanged.connect(
            lambda value: self._update_style(font_size_px=value)
        )
        row_layout.addWidget(_field("字号", self._font_size_spin), 0, 0)

        self._font_weight_combo = _WheelFocusedComboBox(section)
        _compact_control(self._font_weight_combo)
        for label, value in [
            ("常规 400", 400),
            ("中等 500", 500),
            ("半粗 600", 600),
            ("粗体 700", 700),
            ("特粗 800", 800),
            ("黑体 900", 900),
        ]:
            self._font_weight_combo.addItem(label, value)
        self._font_weight_combo.currentIndexChanged.connect(
            lambda _index: self._update_style(
                font_weight=int(self._font_weight_combo.currentData())
            )
        )
        row_layout.addWidget(_field("字重", self._font_weight_combo), 0, 1)
        row_layout.setColumnStretch(0, 1)
        row_layout.setColumnStretch(1, 1)
        layout.addWidget(row)

        self._italic_check = QCheckBox("斜体", section)
        self._italic_check.toggled.connect(lambda checked: self._update_style(italic=checked))
        layout.addWidget(self._italic_check)
        return section

    def _make_ruby_section(self) -> QFrame:
        section, layout = _section("注音")

        row = QWidget(section)
        row_layout = QGridLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setHorizontalSpacing(8)
        row_layout.setVerticalSpacing(8)

        self._ruby_font_size_spin = _spin(8, 96, suffix=" px")
        self._ruby_font_size_spin.valueChanged.connect(
            lambda value: self._update_style(ruby_font_size_px=value)
        )
        row_layout.addWidget(_field("字号", self._ruby_font_size_spin), 0, 0)

        self._ruby_gap_spin = _spin(0, 40, suffix=" px")
        self._ruby_gap_spin.valueChanged.connect(
            lambda value: self._update_style(ruby_gap_px=value)
        )
        row_layout.addWidget(_field("间距", self._ruby_gap_spin), 0, 1)

        self._ruby_color_btn = self._color_button("ruby_color", self._style.ruby_color)
        row_layout.addWidget(_field("颜色", self._ruby_color_btn), 1, 0, 1, 2)

        row_layout.setColumnStretch(0, 1)
        row_layout.setColumnStretch(1, 1)
        layout.addWidget(row)
        return section

    def _make_color_section(self) -> QFrame:
        section, layout = _section("颜色")

        target_grid = QWidget(section)
        target_layout = QGridLayout(target_grid)
        target_layout.setContentsMargins(0, 0, 0, 0)
        target_layout.setHorizontalSpacing(8)
        target_layout.setVerticalSpacing(8)

        self._color_state_combo = _WheelFocusedComboBox(section)
        _compact_control(self._color_state_combo)
        self._color_state_combo.addItem("走字前", "before")
        self._color_state_combo.addItem("走字后", "after")
        self._color_state_combo.setCurrentIndex(1)
        self._color_state_combo.currentIndexChanged.connect(
            lambda _index: self._sync_color_fill_controls()
        )

        self._color_layer_combo = _WheelFocusedComboBox(section)
        _compact_control(self._color_layer_combo)
        self._color_layer_combo.addItem("文字", "text")
        self._color_layer_combo.addItem("描边", "stroke")
        self._color_layer_combo.addItem("描边2", "stroke2")
        self._color_layer_combo.addItem("装饰", "shadow")
        self._color_layer_combo.currentIndexChanged.connect(
            lambda _index: self._sync_color_fill_controls()
        )

        target_layout.addWidget(_field("状态", self._color_state_combo), 0, 0)
        target_layout.addWidget(_field("图层", self._color_layer_combo), 0, 1)
        target_layout.setColumnStretch(0, 1)
        target_layout.setColumnStretch(1, 1)
        layout.addWidget(target_grid)

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
        self._fill_mode_combo.currentIndexChanged.connect(
            lambda _index: self._update_current_fill(
                mode=str(self._fill_mode_combo.currentData())
            )
        )
        layout.addWidget(_field("填充方式", self._fill_mode_combo))

        self._decoration_type_combo = _WheelFocusedComboBox(section)
        _compact_control(self._decoration_type_combo)
        self._decoration_type_combo.addItem("阴影", "shadow")
        self._decoration_type_combo.addItem("发光", "glow")
        self._decoration_type_combo.currentIndexChanged.connect(
            lambda _index: self._update_style(
                decoration_kind=str(self._decoration_type_combo.currentData())
            )
        )
        self._decoration_type_field = _field("装饰类型", self._decoration_type_combo)
        layout.addWidget(self._decoration_type_field)

        self._fill_editor_stack = _DynamicStackedWidget(section)
        self._fill_editor_stack.addWidget(self._make_solid_fill_page())
        self._fill_editor_stack.addWidget(self._make_gradient_fill_page())
        self._fill_editor_stack.addWidget(self._make_split_fill_page())
        self._fill_editor_stack.addWidget(self._make_image_fill_page())
        layout.addWidget(self._fill_editor_stack)

        detail_grid = QWidget(section)
        detail_layout = QGridLayout(detail_grid)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        detail_layout.setHorizontalSpacing(8)
        detail_layout.setVerticalSpacing(8)

        self._stroke_width_spin = _spin(0, 24, suffix=" px")
        self._stroke_width_spin.valueChanged.connect(
            lambda value: self._update_style(stroke_width_px=value)
        )
        detail_layout.addWidget(_field("描边宽度", self._stroke_width_spin), 0, 0)

        self._stroke2_width_spin = _spin(0, 48, suffix=" px")
        self._stroke2_width_spin.valueChanged.connect(
            lambda value: self._update_style(stroke2_width_px=value)
        )
        detail_layout.addWidget(_field("描边2宽度", self._stroke2_width_spin), 0, 1)

        self._shadow_x_spin = _spin(-40, 40, suffix=" px")
        self._shadow_x_spin.valueChanged.connect(
            lambda value: self._update_style(shadow_offset_x=value)
        )
        self._shadow_x_field = _field("阴影 X", self._shadow_x_spin)
        detail_layout.addWidget(self._shadow_x_field, 1, 0)

        self._shadow_y_spin = _spin(-40, 40, suffix=" px")
        self._shadow_y_spin.valueChanged.connect(
            lambda value: self._update_style(shadow_offset_y=value)
        )
        self._shadow_y_field = _field("阴影 Y", self._shadow_y_spin)
        detail_layout.addWidget(self._shadow_y_field, 1, 1)

        self._glow_radius_spin = _spin(1, 120, suffix=" px")
        self._glow_radius_spin.valueChanged.connect(
            lambda value: self._update_style(glow_radius_px=value)
        )
        self._glow_radius_field = _field("发光半径", self._glow_radius_spin)
        detail_layout.addWidget(self._glow_radius_field, 1, 0)

        detail_layout.setColumnStretch(0, 1)
        detail_layout.setColumnStretch(1, 1)
        layout.addWidget(detail_grid)
        return section

    def _make_solid_fill_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        self._paint_solid_btn = self._paint_color_button("color", "#FFFFFF")
        layout.addWidget(_field("颜色", self._paint_solid_btn))
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
        layout.addWidget(_field("渐变条", self._gradient_editor), 0, 0, 1, 2)

        self._gradient_stop_color_btn = ColorButton("#FFFFFF", page)
        self._gradient_stop_color_btn.clicked.connect(self._choose_gradient_stop_color)
        self._gradient_stop_position_spin = _spin(0, 100, suffix=" %")
        self._gradient_stop_position_spin.valueChanged.connect(
            self._set_gradient_stop_position
        )
        self._gradient_stop_delete_btn = QPushButton("删除关键点", page)
        self._gradient_stop_delete_btn.setMinimumHeight(30)
        self._gradient_stop_delete_btn.clicked.connect(
            self._gradient_editor.delete_selected_stop
        )
        layout.addWidget(_field("关键点颜色", self._gradient_stop_color_btn), 1, 0)
        layout.addWidget(_field("关键点位置", self._gradient_stop_position_spin), 1, 1)
        layout.addWidget(self._gradient_stop_delete_btn, 2, 0, 1, 2)
        layout.setColumnStretch(0, 1)
        layout.setColumnStretch(1, 1)
        return page

    def _make_split_fill_page(self) -> QWidget:
        page = QWidget()
        layout = QGridLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setHorizontalSpacing(8)
        layout.setVerticalSpacing(8)
        self._paint_split_top_btn = self._paint_color_button("split_top_color", "#FFFFFF")
        self._paint_split_bottom_btn = self._paint_color_button(
            "split_bottom_color", "#FF5A6F"
        )
        self._paint_split_position_spin = _spin(0, 100, suffix=" %")
        self._paint_split_position_spin.valueChanged.connect(
            lambda value: self._update_current_fill(split_position_pct=value)
        )
        layout.addWidget(_field("上色", self._paint_split_top_btn), 0, 0)
        layout.addWidget(_field("下色", self._paint_split_bottom_btn), 0, 1)
        layout.addWidget(_field("分割位置", self._paint_split_position_spin), 1, 0)
        layout.setColumnStretch(0, 1)
        layout.setColumnStretch(1, 1)
        return page

    def _make_image_fill_page(self) -> QWidget:
        page = QWidget()
        layout = QGridLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setHorizontalSpacing(8)
        layout.setVerticalSpacing(8)
        self._paint_image_path_edit = QLineEdit(page)
        _compact_control(self._paint_image_path_edit)
        self._paint_image_path_edit.editingFinished.connect(
            lambda: self._update_current_fill(image_path=self._paint_image_path_edit.text())
        )
        self._paint_image_browse_btn = QPushButton("浏览...", page)
        self._paint_image_browse_btn.setMinimumHeight(32)
        self._paint_image_browse_btn.clicked.connect(self._choose_paint_image)
        self._paint_image_scale_spin = _spin(10, 400, suffix=" %")
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
        button.clicked.connect(
            lambda _checked=False, field=field_name: self._choose_paint_color(field)
        )
        return button

    def _make_scheme_section(self) -> QFrame:
        section, layout = _section("配色方案")

        self._singer_combo = _WheelFocusedComboBox(section)
        _compact_control(self._singer_combo)
        self._singer_combo.currentIndexChanged.connect(self._on_scheme_combo_changed)
        self._add_scheme_button = QPushButton("添加方案", section)
        self._add_scheme_button.setMinimumHeight(32)
        self._add_scheme_button.clicked.connect(
            lambda _checked=False: self._add_custom_scheme()
        )

        row = QWidget(section)
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(8)
        row_layout.addWidget(self._singer_combo, 1)
        row_layout.addWidget(self._add_scheme_button)
        layout.addWidget(_field("当前方案", row))
        return section

    def _make_effects_page(self) -> QWidget:
        scroll, layout = _scroll_page()
        layout.addWidget(self._make_animation_section())
        layout.addStretch(1)
        return scroll

    def _make_animation_section(self) -> QFrame:
        section, layout = _section("入退场动画")

        grid = QWidget(section)
        grid_layout = QGridLayout(grid)
        grid_layout.setContentsMargins(0, 0, 0, 0)
        grid_layout.setHorizontalSpacing(8)
        grid_layout.setVerticalSpacing(8)

        self._entry_anim_combo = _WheelFocusedComboBox(section)
        _compact_control(self._entry_anim_combo)
        for label, value in [
            ("无", "none"),
            ("淡入", "fade"),
            ("滑入", "slide_in"),
            ("上移", "rise"),
            ("逐文字渐显", "char_fade"),
            ("旋转翻转", "spin_flip"),
            ("ユートピア", "utopia"),
        ]:
            self._entry_anim_combo.addItem(label, value)
        self._entry_anim_combo.currentIndexChanged.connect(
            lambda _index: self._update_style(
                entry_anim=self._entry_anim_combo.currentData()
            )
        )
        grid_layout.addWidget(_field("入场", self._entry_anim_combo), 0, 0)

        self._entry_lead_spin = _spin(0, 3000, suffix=" ms")
        self._entry_lead_spin.valueChanged.connect(
            lambda value: self._update_style(entry_lead_ms=value)
        )
        grid_layout.addWidget(_field("入场时长", self._entry_lead_spin), 0, 1)

        self._exit_anim_combo = _WheelFocusedComboBox(section)
        _compact_control(self._exit_anim_combo)
        for label, value in [
            ("无", "none"),
            ("淡出", "fade"),
            ("滑出", "slide_out"),
            ("上移", "rise"),
            ("逐文字渐隐", "char_fade"),
            ("旋转翻转", "spin_flip"),
            ("ユートピア", "utopia"),
        ]:
            self._exit_anim_combo.addItem(label, value)
        self._exit_anim_combo.currentIndexChanged.connect(
            lambda _index: self._update_style(
                exit_anim=self._exit_anim_combo.currentData()
            )
        )
        grid_layout.addWidget(_field("退场", self._exit_anim_combo), 1, 0)

        self._exit_fade_spin = _spin(0, 3000, suffix=" ms")
        self._exit_fade_spin.valueChanged.connect(
            lambda value: self._update_style(exit_fade_ms=value)
        )
        grid_layout.addWidget(_field("退场时长", self._exit_fade_spin), 1, 1)

        grid_layout.setColumnStretch(0, 1)
        grid_layout.setColumnStretch(1, 1)
        layout.addWidget(grid)
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
        layout.addWidget(_field("对齐", self._viewport_align_combo))

        row = QWidget(section)
        row_layout = QGridLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setHorizontalSpacing(8)
        row_layout.setVerticalSpacing(8)

        # 位置 X / Y 为 4 位数值框（含上下箭头），窄面板下两个并排会溢出，
        # 各占整行；缩放 / 旋转较窄，可同行。
        self._viewport_x_spin = _spin(-4000, 4000)
        self._viewport_x_spin.valueChanged.connect(
            lambda value: self._update_style(viewport_offset_x=value)
        )
        row_layout.addWidget(_field("位置 X", self._viewport_x_spin), 0, 0, 1, 2)

        self._viewport_y_spin = _spin(-4000, 4000)
        self._viewport_y_spin.valueChanged.connect(
            lambda value: self._update_style(viewport_offset_y=value)
        )
        row_layout.addWidget(_field("位置 Y", self._viewport_y_spin), 1, 0, 1, 2)

        self._viewport_scale_spin = _spin(10, 400, suffix=" %")
        self._viewport_scale_spin.valueChanged.connect(
            lambda value: self._update_style(viewport_scale_pct=value)
        )
        row_layout.addWidget(_field("缩放", self._viewport_scale_spin), 2, 0)

        self._viewport_rotation_spin = _spin(-180, 180, suffix=" °")
        self._viewport_rotation_spin.valueChanged.connect(
            lambda value: self._update_style(viewport_rotation_deg=value)
        )
        row_layout.addWidget(_field("旋转", self._viewport_rotation_spin), 2, 1)

        row_layout.setColumnStretch(0, 1)
        row_layout.setColumnStretch(1, 1)
        layout.addWidget(row)
        return section

    def _make_position_section(self) -> QFrame:
        section, layout = _section("位置")

        self._dual_line_check = QCheckBox("双行显示", section)
        self._dual_line_check.toggled.connect(
            lambda checked: self._update_style(dual_line_layout=checked)
        )
        layout.addWidget(self._dual_line_check)

        row = QWidget(section)
        row_layout = QGridLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setHorizontalSpacing(8)
        row_layout.setVerticalSpacing(8)
        self._line_position_combo = _WheelFocusedComboBox(section)
        _compact_control(self._line_position_combo)
        for label, value in [("底部", "bottom"), ("居中", "center"), ("顶部", "top")]:
            self._line_position_combo.addItem(label, value)
        self._line_position_combo.currentIndexChanged.connect(
            lambda _index: self._update_style(
                line_y_position=self._line_position_combo.currentData()
            )
        )
        row_layout.addWidget(_field("行位置", self._line_position_combo), 0, 0)

        self._line_margin_spin = _spin(0, 400, suffix=" px")
        self._line_margin_spin.valueChanged.connect(
            lambda value: self._update_style(line_y_margin_px=value)
        )
        row_layout.addWidget(_field("下行底边距", self._line_margin_spin), 0, 1)

        self._horizontal_layout_combo = _WheelFocusedComboBox(section)
        _compact_control(self._horizontal_layout_combo)
        for label, value in [
            ("上左下右", "asymmetric"),
            ("居中", "center"),
            ("逐行独立", "per_row"),
        ]:
            self._horizontal_layout_combo.addItem(label, value)
        self._horizontal_layout_combo.currentIndexChanged.connect(
            lambda _index: self._on_horizontal_layout_changed()
        )
        row_layout.addWidget(_field("水平布局", self._horizontal_layout_combo), 1, 0)

        self._line_gap_spin = _spin(0, 400, suffix=" px")
        self._line_gap_spin.valueChanged.connect(
            lambda value: self._update_style(line_gap_px=value)
        )
        row_layout.addWidget(_field("两行间距", self._line_gap_spin), 1, 1)

        self._upper_left_spin = _spin(0, 800, suffix=" px")
        self._upper_left_spin.valueChanged.connect(
            lambda value: self._update_style(upper_line_left_margin_px=value)
        )
        row_layout.addWidget(_field("上行左边距", self._upper_left_spin), 2, 0)

        self._lower_right_spin = _spin(0, 800, suffix=" px")
        self._lower_right_spin.valueChanged.connect(
            lambda value: self._update_style(lower_line_right_margin_px=value)
        )
        row_layout.addWidget(_field("下行右边距", self._lower_right_spin), 2, 1)

        row_layout.setColumnStretch(0, 1)
        row_layout.setColumnStretch(1, 1)
        layout.addWidget(row)

        layout.addWidget(self._make_per_row_box(section))
        return section

    def _make_per_row_box(self, parent: QWidget) -> QWidget:
        """逐行独立布局控件（仅「水平布局 = 逐行独立」时启用）。"""
        box = self._per_row_box = QWidget(parent)
        grid = QGridLayout(box)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(8)

        # 窄面板（~260px）下两个数值框并排会横向溢出，所以每行最多放
        # 「对齐(窄) + X」，Y 单独整行。对齐下拉很窄，与 X 同行可容纳。
        self._row1_align_combo = self._make_align_combo(box, "row1_align")
        grid.addWidget(_field("一行对齐", self._row1_align_combo), 0, 0)
        self._row1_x_spin = self._make_offset_spin("row1_offset_x")
        grid.addWidget(_field("一行 X", self._row1_x_spin), 0, 1)
        self._row1_y_spin = self._make_offset_spin("row1_offset_y")
        grid.addWidget(_field("一行 Y", self._row1_y_spin), 1, 0, 1, 2)

        self._row2_align_combo = self._make_align_combo(box, "row2_align")
        grid.addWidget(_field("二行对齐", self._row2_align_combo), 2, 0)
        self._row2_x_spin = self._make_offset_spin("row2_offset_x")
        grid.addWidget(_field("二行 X", self._row2_x_spin), 2, 1)
        self._row2_y_spin = self._make_offset_spin("row2_offset_y")
        grid.addWidget(_field("二行 Y", self._row2_y_spin), 3, 0, 1, 2)

        grid.setColumnStretch(0, 0)
        grid.setColumnStretch(1, 1)
        return box

    def _make_align_combo(self, parent: QWidget, field_name: str) -> "_WheelFocusedComboBox":
        combo = _WheelFocusedComboBox(parent)
        _compact_control(combo)
        for label, value in [("左", "left"), ("中", "center"), ("右", "right")]:
            combo.addItem(label, value)
        combo.currentIndexChanged.connect(
            lambda _index: self._update_style(**{field_name: combo.currentData()})
        )
        return combo

    def _make_offset_spin(self, field_name: str) -> QSpinBox:
        # 不加 " px" 后缀：窄面板下两列并排会横向溢出，单位由字段标签隐含。
        spin = _spin(-4000, 4000)
        spin.valueChanged.connect(lambda value: self._update_style(**{field_name: value}))
        return spin

    def _on_horizontal_layout_changed(self) -> None:
        self._update_style(
            line_horizontal_layout=self._horizontal_layout_combo.currentData()
        )
        self._sync_per_row_enabled()

    def _sync_per_row_enabled(self) -> None:
        if not hasattr(self, "_per_row_box"):
            return
        self._per_row_box.setEnabled(self._style.line_horizontal_layout == "per_row")

    def _make_timing_section(self) -> QFrame:
        section, layout = _section("时间")

        row = QWidget(section)
        row_layout = QGridLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setHorizontalSpacing(8)
        row_layout.setVerticalSpacing(8)

        self._line_lead_spin = _spin(0, 10_000, suffix=" ms")
        self._line_lead_spin.valueChanged.connect(
            lambda value: self._update_style(line_lead_in_ms=value)
        )
        row_layout.addWidget(_field("提前入场", self._line_lead_spin), 0, 0)

        self._line_tail_spin = _spin(0, 10_000, suffix=" ms")
        self._line_tail_spin.valueChanged.connect(
            lambda value: self._update_style(line_tail_ms=value)
        )
        row_layout.addWidget(_field("延迟退场", self._line_tail_spin), 0, 1)

        self._line_offset_spin = _spin(-10_000, 10_000, suffix=" ms")
        self._line_offset_spin.valueChanged.connect(
            lambda value: self._update_style(timing_offset_ms=value)
        )
        row_layout.addWidget(_field("偏移", self._line_offset_spin), 1, 0)

        row_layout.setColumnStretch(0, 1)
        row_layout.setColumnStretch(1, 1)
        layout.addWidget(row)
        return section

    def _color_button(self, field_name: str, color: str) -> ColorButton:
        button = ColorButton(color)
        button.clicked.connect(lambda _checked=False, field=field_name: self._choose_color(field))
        return button

    # ------------------------------------------------------------------ update

    def _on_screen_preset_changed(self) -> None:
        if self._syncing:
            return
        key = str(self._screen_preset_combo.currentData() or "custom")
        preset = _SCREEN_PRESET_BY_KEY.get(key)
        if preset is None:
            self.set_screen_settings(replace(self._screen, preset_key="custom"), emit=True)
            return
        self.set_screen_settings(
            ScreenSettings(
                preset_key=preset.key,
                par=preset.par,
                width=preset.width,
                height=preset.height,
                fps=self._screen.fps,
            ),
            emit=True,
        )

    def _on_screen_controls_changed(self) -> None:
        if self._syncing:
            return
        par = str(self._screen_par_combo.currentData() or "1:1")
        width = self._screen_width_spin.value()
        height = self._screen_height_spin.value()
        fps = int(self._screen_fps_combo.currentData() or 60)
        self.set_screen_settings(
            ScreenSettings(
                preset_key=match_screen_preset_key(width, height, par),
                par=par,
                width=width,
                height=height,
                fps=fps,
            ),
            emit=True,
        )

    def _choose_color(self, field_name: str) -> None:
        current = QColor(self._scheme_value(field_name))
        color = QColorDialog.getColor(current, self, "选择颜色")
        if color.isValid():
            self._set_color(field_name, color.name(QColor.NameFormat.HexRgb))

    def _set_color(self, field_name: str, color: str) -> None:
        normalized = _normalize_hex(color, str(self._scheme_value(field_name)))
        changes = {field_name: normalized}
        colors = _apply_legacy_color_to_matrix(
            self._current_karaoke_colors(), field_name, normalized
        )
        if colors is not None:
            changes["karaoke_colors"] = colors
        self._update_style(**changes)

    def _choose_paint_color(self, field_name: str) -> None:
        fill = self._current_paint_fill()
        current = QColor(getattr(fill, field_name))
        color = QColorDialog.getColor(current, self, "选择颜色")
        if color.isValid():
            normalized = color.name(QColor.NameFormat.HexRgb).upper()
            self._update_current_fill(**{field_name: normalized})

    def _choose_paint_image(self) -> None:
        path, _selected_filter = QFileDialog.getOpenFileName(
            self,
            "选择填充图像",
            self._paint_image_path_edit.text(),
            "图像文件 (*.png *.jpg *.jpeg *.bmp *.webp);;所有文件 (*.*)",
        )
        if path:
            self._paint_image_path_edit.setText(path)
            self._update_current_fill(image_path=path)

    def _current_color_state_key(self) -> ColorStateKey:
        data = self._color_state_combo.currentData()
        return data if data in {"before", "after"} else "after"  # type: ignore[return-value]

    def _current_color_layer_key(self) -> ColorLayerKey:
        data = self._color_layer_combo.currentData()
        if data in {"text", "stroke", "stroke2", "shadow"}:
            return data  # type: ignore[return-value]
        return "text"

    def _current_karaoke_colors(self) -> KaraokeColors:
        value = self._scheme_value("karaoke_colors")
        if isinstance(value, KaraokeColors):
            return deepcopy(value)
        return _legacy_colors_from_panel(self)

    def _current_paint_fill(self) -> PaintFill:
        colors = self._current_karaoke_colors()
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
            self._gradient_editor.set_stops(_gradient_stops(fill))
            self._sync_gradient_stop_controls()
            self._paint_split_top_btn.set_color(fill.split_top_color)
            self._paint_split_bottom_btn.set_color(fill.split_bottom_color)
            self._paint_split_position_spin.setValue(fill.split_position_pct)
            self._paint_image_path_edit.setText(fill.image_path)
            self._paint_image_scale_spin.setValue(fill.image_scale_pct)
            self._sync_decoration_visibility()
        finally:
            self._syncing = was_syncing

    def _sync_decoration_visibility(self) -> None:
        if not hasattr(self, "_decoration_type_field"):
            return
        is_decoration = self._current_color_layer_key() == "shadow"
        is_shadow = str(self._scheme_value("decoration_kind")) == "shadow"
        is_glow = str(self._scheme_value("decoration_kind")) == "glow"
        self._decoration_type_field.setVisible(is_decoration)
        self._shadow_x_field.setVisible(is_decoration and is_shadow)
        self._shadow_y_field.setVisible(is_decoration and is_shadow)
        self._glow_radius_field.setVisible(is_decoration and is_glow)

    def _update_current_fill(self, **changes) -> None:
        if self._syncing:
            return
        colors = self._current_karaoke_colors()
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
            )
        state = replace(state, **{layer_key: fill})
        colors = replace(colors, **{state_key: state})
        self._update_style(karaoke_colors=colors)

    def _update_gradient_stops(self, stops: list[tuple[int, str]]) -> None:
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

    def _choose_gradient_stop_color(self) -> None:
        current = QColor(self._gradient_editor.selected_stop[1])
        color = QColorDialog.getColor(current, self, "Select gradient stop color")
        if color.isValid():
            normalized = color.name(QColor.NameFormat.HexRgb).upper()
            self._gradient_editor.set_selected_color(normalized)

    def _set_gradient_stop_position(self, value: int) -> None:
        if self._syncing:
            return
        self._gradient_editor.set_selected_position(value)

    def _refresh_scheme_combo(self, selected_key: Optional[str] = None) -> None:
        self._singer_combo.clear()
        self._singer_combo.addItem("全局默认", _GLOBAL_SCHEME_KEY)
        for singer_id, label in self._singer_options:
            self._singer_combo.addItem(label, f"{_SINGER_SCHEME_PREFIX}{singer_id}")
        for name in self._style.custom_style_schemes:
            self._singer_combo.addItem(name, f"{_CUSTOM_SCHEME_PREFIX}{name}")
        if selected_key is not None:
            index = self._singer_combo.findData(selected_key)
            if index >= 0:
                self._singer_combo.setCurrentIndex(index)

    def _add_custom_scheme(self, name: Optional[str] = None) -> None:
        if name is None or isinstance(name, bool):
            name, ok = QInputDialog.getText(self, "添加配色方案", "方案名称")
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
        self._update_style(custom_style_schemes=schemes)
        self._syncing = True
        try:
            self._refresh_scheme_combo(f"{_CUSTOM_SCHEME_PREFIX}{name}")
        finally:
            self._syncing = False
        self._sync_subtitle_scheme_controls()

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
            return
        self._singer_combo.setCurrentIndex(index)

    def _on_scheme_combo_changed(self, _index: int) -> None:
        self._sync_subtitle_scheme_controls()
        if not self._syncing:
            self.schemeSelectionChanged.emit(self.current_scheme_key())

    def _current_singer_id(self) -> Optional[int]:
        key = self._current_scheme_key()
        if key is None or not key.startswith(_SINGER_SCHEME_PREFIX):
            return None
        try:
            return int(key.removeprefix(_SINGER_SCHEME_PREFIX))
        except ValueError:
            return None

    def _current_custom_scheme_name(self) -> Optional[str]:
        key = self._current_scheme_key()
        if key is None or not key.startswith(_CUSTOM_SCHEME_PREFIX):
            return None
        return key.removeprefix(_CUSTOM_SCHEME_PREFIX)

    def _scheme_value(self, field_name: str):
        custom_name = self._current_custom_scheme_name()
        if custom_name is not None:
            scheme = self._style.custom_style_schemes.get(custom_name)
            value = getattr(scheme, field_name, None) if scheme is not None else None
            if value is not None:
                return value
        singer_id = self._current_singer_id()
        if singer_id is not None:
            scheme = self._style.singer_style_overrides.get(singer_id)
            value = getattr(scheme, field_name, None) if scheme is not None else None
            if value is not None:
                return value
        return getattr(self._style, field_name)

    def _ensure_singer_schemes(self) -> bool:
        overrides = dict(self._style.singer_style_overrides)
        changed = False
        for singer_id, _label in self._singer_options:
            if singer_id in overrides:
                continue
            overrides[singer_id] = _scheme_from_style(self._style, singer_id)
            changed = True
        if changed:
            self._style = replace(self._style, singer_style_overrides=overrides)
        return changed

    def _sync_subtitle_scheme_controls(self) -> None:
        if not hasattr(self, "_singer_combo"):
            return
        was_syncing = self._syncing
        self._syncing = True
        try:
            self._font_combo.setCurrentFont(QFont(str(self._scheme_value("font_family"))))
            self._font_size_spin.setValue(int(self._scheme_value("font_size_px")))
            self._font_weight_combo.setCurrentIndex(
                max(0, self._font_weight_combo.findData(int(self._scheme_value("font_weight"))))
            )
            self._italic_check.setChecked(bool(self._scheme_value("italic")))
            self._stroke_width_spin.setValue(int(self._scheme_value("stroke_width_px")))
            self._stroke2_width_spin.setValue(int(self._scheme_value("stroke2_width_px")))
            self._decoration_type_combo.setCurrentIndex(
                max(
                    0,
                    self._decoration_type_combo.findData(
                        str(self._scheme_value("decoration_kind"))
                    ),
                )
            )
            self._glow_radius_spin.setValue(int(self._scheme_value("glow_radius_px")))
            self._shadow_x_spin.setValue(int(self._scheme_value("shadow_offset_x")))
            self._shadow_y_spin.setValue(int(self._scheme_value("shadow_offset_y")))
            self._ruby_font_size_spin.setValue(int(self._scheme_value("ruby_font_size_px")))
            self._ruby_color_btn.set_color(str(self._scheme_value("ruby_color")))
            self._ruby_gap_spin.setValue(int(self._scheme_value("ruby_gap_px")))
            self._sync_color_fill_controls()
        finally:
            self._syncing = was_syncing

    def _update_style(self, **changes) -> None:
        if self._syncing:
            return
        if changes and set(changes).issubset(_SCHEME_FIELDS):
            custom_name = self._current_custom_scheme_name()
            if custom_name is not None:
                schemes = dict(self._style.custom_style_schemes)
                scheme = schemes.get(custom_name) or _scheme_from_current(self)
                schemes[custom_name] = replace(scheme, **changes)
                changes = {"custom_style_schemes": schemes}
            else:
                singer_id = self._current_singer_id()
                if singer_id is not None:
                    overrides = dict(self._style.singer_style_overrides)
                    scheme = overrides.get(singer_id) or _scheme_from_style(self._style, singer_id)
                    overrides[singer_id] = replace(scheme, **changes)
                    changes = {"singer_style_overrides": overrides}
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
        if "decoration_kind" in changes:
            changes["decoration_kind"] = _normalize_decoration_kind(
                changes["decoration_kind"]
            )
        if "entry_anim" in changes:
            changes["entry_anim"] = _normalize_entry_animation(changes["entry_anim"])
        if "exit_anim" in changes:
            changes["exit_anim"] = _normalize_exit_animation(changes["exit_anim"])
        self._style = replace(self._style, **changes)
        self._syncing = True
        try:
            if set(changes).intersection(
                _SCHEME_FIELDS | {"singer_style_overrides", "custom_style_schemes"}
            ):
                self._sync_subtitle_scheme_controls()
        finally:
            self._syncing = False
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
    if value in {"shadow", "glow"}:
        return value  # type: ignore[return-value]
    return "shadow"


def _normalize_entry_animation(value: object) -> EntryAnimation:
    if value in {"none", "fade", "slide_in", "rise", "char_fade", "spin_flip", "utopia"}:
        return value  # type: ignore[return-value]
    return "none"


def _normalize_exit_animation(value: object) -> ExitAnimation:
    if value in {"none", "fade", "slide_out", "rise", "char_fade", "spin_flip", "utopia"}:
        return value  # type: ignore[return-value]
    return "none"


def _fill_stack_index(mode: str) -> int:
    if mode in {"gradient_horizontal", "gradient_vertical"}:
        return 1
    if mode == "split_vertical":
        return 2
    if mode == "image":
        return 3
    return 0


def _normalize_gradient_stops(stops: list[tuple[int, str]]) -> list[tuple[int, str]]:
    normalized: dict[int, str] = {}
    for position, color in stops:
        pos = max(0, min(100, int(position)))
        normalized[pos] = _normalize_hex(str(color), "#FFFFFF")
    if 0 not in normalized:
        first = next(iter(normalized.values()), "#FFFFFF")
        normalized[0] = first
    if 100 not in normalized:
        last = next(reversed(normalized.values()), normalized[0])
        normalized[100] = last
    return sorted(normalized.items())


def _gradient_stops(fill: PaintFill) -> list[tuple[int, str]]:
    if fill.gradient_stops:
        return _normalize_gradient_stops(fill.gradient_stops)
    return _normalize_gradient_stops([(0, fill.start_color), (100, fill.end_color)])


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


def _scheme_from_style(style: Style, singer_id: int) -> SubtitleStyleScheme:
    fill = _SINGER_FILL_PALETTE[singer_id % len(_SINGER_FILL_PALETTE)]
    ruby = _SINGER_RUBY_PALETTE[singer_id % len(_SINGER_RUBY_PALETTE)]
    colors = deepcopy(style.karaoke_colors) if style.karaoke_colors is not None else None
    if colors is None:
        colors = KaraokeColors(
            before=KaraokeColorState(
                text=_solid_fill(style.base_color),
                stroke=_solid_fill(style.stroke_color),
                stroke2=_solid_fill("#000000"),
                shadow=_solid_fill(style.shadow_color),
            ),
            after=KaraokeColorState(
                text=_solid_fill(fill),
                stroke=_solid_fill(style.stroke_color),
                stroke2=_solid_fill("#000000"),
                shadow=_solid_fill(style.shadow_color),
            ),
        )
    else:
        colors.after.text = replace(
            colors.after.text,
            color=fill,
            start_color=fill,
            gradient_stops=[(0, fill), (100, colors.after.text.end_color)],
            split_top_color=fill,
        )
    return SubtitleStyleScheme(
        font_family=style.font_family,
        font_size_px=style.font_size_px,
        font_weight=style.font_weight,
        italic=style.italic,
        base_color=style.base_color,
        fill_color=fill,
        fill_gradient_enabled=style.fill_gradient_enabled,
        fill_gradient_start_color=fill,
        fill_gradient_end_color=style.fill_gradient_end_color,
        fill_gradient_angle_deg=style.fill_gradient_angle_deg,
        stroke_color=style.stroke_color,
        stroke_width_px=style.stroke_width_px,
        stroke2_width_px=style.stroke2_width_px,
        decoration_kind=style.decoration_kind,
        glow_radius_px=style.glow_radius_px,
        shadow_color=style.shadow_color,
        shadow_offset_x=style.shadow_offset_x,
        shadow_offset_y=style.shadow_offset_y,
        ruby_font_size_px=style.ruby_font_size_px,
        ruby_color=ruby,
        ruby_gap_px=style.ruby_gap_px,
        karaoke_colors=colors,
    )


def _scheme_from_current(panel: PropertyPanel) -> SubtitleStyleScheme:
    return SubtitleStyleScheme(
        font_family=str(panel._scheme_value("font_family")),
        font_size_px=int(panel._scheme_value("font_size_px")),
        font_weight=int(panel._scheme_value("font_weight")),
        italic=bool(panel._scheme_value("italic")),
        base_color=str(panel._scheme_value("base_color")),
        fill_color=str(panel._scheme_value("fill_color")),
        fill_gradient_enabled=bool(panel._scheme_value("fill_gradient_enabled")),
        fill_gradient_start_color=str(panel._scheme_value("fill_gradient_start_color")),
        fill_gradient_end_color=str(panel._scheme_value("fill_gradient_end_color")),
        fill_gradient_angle_deg=int(panel._scheme_value("fill_gradient_angle_deg")),
        stroke_color=str(panel._scheme_value("stroke_color")),
        stroke_width_px=int(panel._scheme_value("stroke_width_px")),
        stroke2_width_px=int(panel._scheme_value("stroke2_width_px")),
        decoration_kind=_normalize_decoration_kind(panel._scheme_value("decoration_kind")),
        glow_radius_px=int(panel._scheme_value("glow_radius_px")),
        shadow_color=str(panel._scheme_value("shadow_color")),
        shadow_offset_x=int(panel._scheme_value("shadow_offset_x")),
        shadow_offset_y=int(panel._scheme_value("shadow_offset_y")),
        ruby_font_size_px=int(panel._scheme_value("ruby_font_size_px")),
        ruby_color=str(panel._scheme_value("ruby_color")),
        ruby_gap_px=int(panel._scheme_value("ruby_gap_px")),
        karaoke_colors=panel._current_karaoke_colors(),
    )


def _spin(minimum: int, maximum: int, *, suffix: str = "") -> QSpinBox:
    spin = _WheelFocusedSpinBox()
    spin.setRange(minimum, maximum)
    spin.setSuffix(suffix)
    spin.setButtonSymbols(QSpinBox.ButtonSymbols.UpDownArrows)
    _compact_control(spin)
    return spin


def _compact_control(widget: QWidget) -> None:
    widget.setMinimumWidth(0)
    widget.setFixedHeight(32)
    widget.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)


def _scroll_page() -> tuple[QScrollArea, QVBoxLayout]:
    scroll = QScrollArea()
    scroll.setObjectName("SubtitlePropertyScroll")
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QScrollArea.Shape.NoFrame)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    themed(
        scroll,
        lambda: (
            """
            QScrollArea#SubtitlePropertyScroll {
                background: transparent;
                border: 0;
            }
            QScrollArea#SubtitlePropertyScroll > QWidget > QWidget {
                background: transparent;
            }
            """
        ),
    )

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


def _section(title: str) -> tuple[CollapsibleSection, QVBoxLayout]:
    section = CollapsibleSection(title)
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
            QFrame#SubtitlePropertySection QCheckBox {{
                color: {palette().text_primary};
                font-size: 9.5pt;
                background: transparent;
            }}
            QFrame#SubtitlePropertySection QComboBox QAbstractItemView,
            QFrame#SubtitlePropertySection QFontComboBox QAbstractItemView {{
                background: {palette().card_bg};
                color: {palette().text_primary};
                border: 1px solid {palette().card_border};
                selection-background-color: {palette().preview_selection_bg};
                selection-color: {palette().preview_selection_text};
            }}
            {control_qss("QFrame#SubtitlePropertySection")}
            """
        ),
    )
    return section, section.content_layout
