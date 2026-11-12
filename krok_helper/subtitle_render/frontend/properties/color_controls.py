"""Reusable color-entry and screen-picking controls for property editors."""

from __future__ import annotations

import logging
from typing import Optional

from PyQt6 import sip
from PyQt6.QtCore import QPoint, QPointF, QRect, QTimer, Qt, pyqtSignal as Signal
from PyQt6.QtGui import (
    QColor,
    QCursor,
    QIcon,
    QImage,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    Action,
    FluentIcon as FIF,
    FluentTitleBar,
    CaptionLabel,
    LineEdit as FluentLineEdit,
    RoundMenu,
    PrimaryPushButton,
    PushButton as FluentPushButton,
    ScrollArea as FluentScrollArea,
    ToolButton as FluentToolButton,
)
from qframelesswindow import FramelessDialog

from krok_helper.subtitle_render.frontend.widgets.theme import palette, themed
from krok_helper.subtitle_render.settings.recent_colors import RECENT_COLOR_LIMIT, RecentColorStore
from .color_picker_widgets import ColorChannelSlider, ColorTile, HueSaturationPlane


COLOR_COMMIT_DEBOUNCE_MS = 250
"""Delay used to coalesce live hexadecimal color edits."""

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

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        # 控件常在所在页面被 QStackedWidget 隐藏期间灌入项目颜色；首次
        # 切到该页时偶发停留在构造期白色不重绘。显示时强制重申一次样式，
        # 保证「项目已设置的颜色」第一眼就显示，而不需要点一下。
        self._apply()

    def _apply(self) -> None:
        color = QColor(self._color)
        text_color = "#111827" if color.lightness() > 150 else "#FFFFFF"
        self.setText(self._color)
        background = f"rgba({color.red()}, {color.green()}, {color.blue()}, {color.alpha()})"
        stylesheet = (
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
        # setStyleSheet 对完全相同的字符串会判等跳过 repolish，这里手动
        # 强制一次样式重算，确保显示时机上的重申真正触发重绘。
        if self.styleSheet() == stylesheet:
            style = self.style()
            style.unpolish(self)
            style.polish(self)
            self.update()
        else:
            self.setStyleSheet(stylesheet)


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

    _LIVE_APPLY_DELAY_MS = COLOR_COMMIT_DEBOUNCE_MS

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
        self._remember_entered_color()
        self._end_color_entry()
        self.editFinished.emit()

    def _finish_color_entry(self) -> None:
        if self._ending_color_entry:
            return
        self._live_apply_timer.stop()
        if self._swatch_stack.currentWidget() is self._color_edit:
            self._apply_color_entry()
            self._remember_entered_color()
            self._end_color_entry()
            self.editFinished.emit()

    def _remember_entered_color(self) -> None:
        # Only a finished edit counts as "used": live keystrokes preview the
        # color but would otherwise litter the recent colors.
        if self.color != self._entry_original_color:
            record_recent_color(QColor(self.color))

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
        self._hover_timer = QTimer(self)
        self._hover_timer.setInterval(16)
        self._hover_timer.timeout.connect(
            lambda: self._emit_hovered_color(QCursor.pos())
        )
        desktop_geometry = QRect()
        for screen in QApplication.screens():
            desktop_geometry = desktop_geometry.united(screen.geometry())
        # 不在构造时整屏截图：grabWindow(0).toImage() 会把整块帧缓冲深拷贝成
        # QImage，多显示器/4K 下耗时约一秒且阻塞 UI 线程。取色时按需只抓
        # 光标处 1x1 像素即可，开销从整屏降为常数。

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
        self._emit_hovered_color(QCursor.pos())
        # Windows may stop delivering hover-only mouseMoveEvent events after
        # the cursor leaves the application even though the final captured
        # click still arrives. Poll the global cursor while picking so the
        # inline swatch keeps previewing colors across the virtual desktop.
        self._hover_timer.start()

    def color_at(self, global_position: QPoint) -> QColor:
        # 测试会预先往 _screens 塞缓存图像；有覆盖该点的缓存时优先使用。
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
        screen = QApplication.screenAt(global_position)
        if screen is None:
            return QColor()
        geometry = screen.geometry()
        x = global_position.x() - geometry.x()
        y = global_position.y() - geometry.y()
        x = min(max(x, 0), max(geometry.width() - 1, 0))
        y = min(max(y, 0), max(geometry.height() - 1, 0))
        # grabWindow 的区域坐标是屏幕逻辑坐标（Qt 内部处理 DPR 缩放），
        # 1x1 抓取返回的图像即为该点像素。
        image = screen.grabWindow(0, x, y, 1, 1).toImage()
        if image.isNull():
            return QColor()
        return image.pixelColor(0, 0)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            color = self.color_at(event.globalPosition().toPoint())
            if color.isValid():
                record_recent_color(color)
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
        self._hover_timer.stop()
        self.releaseMouse()
        self.releaseKeyboard()
        self.hide()
        self.finished.emit()
        self.deleteLater()


class _RecentColorList(FluentScrollArea):
    """Scrollable RGBA color bars, with color/text drag-and-drop support."""

    colorPicked = Signal(QColor)
    colorDropped = Signal(QColor)
    selectionChanged = Signal(QColor)

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setAccessibleName("最近使用的颜色")
        self.setWidgetResizable(True)
        self.setFrameShape(FluentScrollArea.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setMinimumHeight(140)
        self.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        content = QWidget(self)
        content.setStyleSheet("background: transparent;")
        self.setWidget(content)
        self.tiles: list[ColorTile] = []
        self._selected_color = QColor()
        rows = QVBoxLayout(content)
        rows.setContentsMargins(0, 0, 12, 0)
        rows.setSpacing(8)
        rows.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._empty_hint = CaptionLabel("暂无最近颜色", content)
        self._empty_hint.setWordWrap(True)
        rows.addWidget(self._empty_hint)
        for _index in range(RECENT_COLOR_LIMIT):
            tile = ColorTile(parent=content)
            tile.setFixedHeight(30)
            tile.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            tile.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            tile.setCheckable(True)
            tile.clicked.connect(lambda checked=False, tile=tile: self._pick_color(tile.color))
            rows.addWidget(tile)
            self.tiles.append(tile)

    def set_colors(self, colors: list[QColor]) -> None:
        self._empty_hint.setVisible(not colors)
        for index, tile in enumerate(self.tiles):
            tile.set_color(colors[index] if index < len(colors) else QColor())
            tile.setVisible(index < len(colors))
        self.verticalScrollBar().setValue(0)
        self.set_selected_color(self._selected_color)

    @property
    def selected_color(self) -> QColor:
        return QColor(self._selected_color)

    def set_selected_color(self, color: QColor) -> None:
        selected = QColor()
        for tile in self.tiles:
            matches = color.isValid() and tile.color.isValid() and tile.color.rgba() == color.rgba()
            tile.setChecked(matches)
            if matches:
                selected = QColor(tile.color)
        changed = selected != self._selected_color
        self._selected_color = selected
        if changed:
            self.selectionChanged.emit(QColor(selected))

    def _pick_color(self, color: QColor) -> None:
        self.set_selected_color(color)
        self.colorPicked.emit(QColor(color))

    @staticmethod
    def _drop_color(mime) -> QColor:
        if mime.hasColor():
            color = QColor(mime.colorData())
            if color.isValid():
                return color
        parsed = _parse_hex_color(mime.text()) if mime.hasText() else None
        return QColor(parsed) if parsed else QColor()

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if self._drop_color(event.mimeData()).isValid():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:  # noqa: N802
        color = self._drop_color(event.mimeData())
        if color.isValid():
            self.colorDropped.emit(color)
            event.acceptProposedAction()
        else:
            event.ignore()


class _ColorDialog(FramelessDialog):
    """Fluent color editor with N3-style HS, value and alpha surfaces.

    RGB remains exact when a color is loaded or only alpha changes. Hue and
    saturation are retained while the color is gray/black so moving value back
    up does not unexpectedly reset a chosen hue.
    """

    currentColorChanged = Signal(QColor)
    def __init__(self, current: QColor, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setTitleBar(FluentTitleBar(self))
        self.titleBar.setFixedHeight(36)
        self.titleBar.hBoxLayout.setContentsMargins(16, 0, 0, 0)
        self.titleBar.iconLabel.hide()
        self.titleBar.minBtn.hide()
        self.titleBar.maxBtn.hide()
        self.titleBar.setDoubleClickEnabled(False)
        self.titleBar.closeBtn.setAccessibleName("关闭颜色选择窗口")
        self.setObjectName("SubtitleColorDialog")
        self.setWindowTitle("选择颜色")
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        self.setMinimumWidth(700)
        self._current = QColor("#FFFFFF")
        self._selected = QColor()
        self._hue, self._saturation, self._value = 0.0, 0.0, 1.0
        self._screen_picker: Optional[ScreenColorPicker] = None

        root = QVBoxLayout(self)
        root.setContentsMargins(16, self.titleBar.height() + 12, 16, 16)
        root.setSpacing(12)
        body = QHBoxLayout()
        body.setSpacing(20)
        left = QVBoxLayout()
        left.setSpacing(8)
        self._color_plane = HueSaturationPlane(self)
        self._color_plane.setMinimumSize(340, 250)
        left.addWidget(self._color_plane, 1)
        self._color_plane.colorChanged.connect(self._set_hue_saturation)

        self._value_slider = ColorChannelSlider("value", self)
        self._alpha_slider = ColorChannelSlider("alpha", self)
        left.addWidget(self._value_slider)
        left.addWidget(self._alpha_slider)
        self._value_slider.valueChanged.connect(self._set_value)
        self._alpha_slider.valueChanged.connect(self._set_current_alpha)

        hex_row = QHBoxLayout()
        self._hex_edit = FluentLineEdit(self)
        self._hex_edit.setMaxLength(9)
        self._hex_edit.setAccessibleName("十六进制色号")
        self._hex_edit.setPlaceholderText("#RRGGBB / #AARRGGBB")
        self._hex_edit.setToolTip("支持 RGB / ARGB 色号；8 位格式的前两位为 Alpha")
        self._hex_edit.textEdited.connect(self._edit_hex)
        self._hex_edit.editingFinished.connect(self._finish_hex)
        hex_row.addWidget(self._hex_edit, 1)
        left.addLayout(hex_row)
        self._hex_hint = CaptionLabel("请输入有效色号：#RRGGBB 或 #AARRGGBB", self)
        self._hex_hint.hide()
        left.addWidget(self._hex_hint)
        body.addLayout(left, 1)

        right = QVBoxLayout()
        right.setSpacing(8)
        self._original_swatch = ColorTile(current, self)
        self._current_swatch = ColorTile(current, self)
        self._current_swatch.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._current_swatch.setCursor(Qt.CursorShape.ArrowCursor)
        compare = QHBoxLayout()
        for caption, tile in (("原色", self._original_swatch), ("新色", self._current_swatch)):
            column = QVBoxLayout()
            column.setSpacing(4)
            tile.setFixedHeight(24)
            column.addWidget(CaptionLabel(caption, self))
            column.addWidget(tile)
            compare.addLayout(column, 1)
        right.addLayout(compare)
        self._original_swatch.clicked.connect(lambda: self.setCurrentColor(self._original_swatch.color))
        right.addWidget(CaptionLabel("最近使用", self))
        self._recent_list = _RecentColorList(self)
        self._recent_list.colorPicked.connect(self.setCurrentColor)
        self._recent_list.colorDropped.connect(self._record_dropped_color)
        right.addWidget(self._recent_list, 1)
        self._delete_recent_button = FluentPushButton("删除当前色", self)
        self._delete_recent_button.setAutoDefault(False)
        self._delete_recent_button.setEnabled(False)
        self._delete_recent_button.clicked.connect(self._delete_current_color)
        self._recent_list.selectionChanged.connect(
            lambda color: self._delete_recent_button.setEnabled(color.isValid())
        )
        right.addWidget(self._delete_recent_button)
        right_container = QWidget(self)
        right_container.setFixedWidth(240)
        right_container.setLayout(right)
        right.setContentsMargins(0, 0, 0, 0)
        body.addWidget(right_container)
        root.addLayout(body, 1)

        footer = QHBoxLayout()
        self._screen_button = FluentPushButton(_eyedropper_icon(), "从屏幕取色", self)
        self._screen_button.setAutoDefault(False)
        self._screen_button.clicked.connect(self._pick_screen_color)
        footer.addWidget(self._screen_button)
        footer.addStretch(1)
        self._cancel_button = FluentPushButton("取消", self)
        self._ok_button = PrimaryPushButton("确定", self)
        self._cancel_button.setFixedWidth(96)
        self._ok_button.setFixedWidth(96)
        self._cancel_button.clicked.connect(self.reject)
        self._ok_button.clicked.connect(self.accept)
        # Enter finishes the active field first; it must not accidentally
        # accept a partial hex value.
        self._cancel_button.setAutoDefault(False)
        self._ok_button.setAutoDefault(False)
        footer.addWidget(self._cancel_button)
        footer.addWidget(self._ok_button)
        root.addLayout(footer)
        themed(self, self._theme_stylesheet)
        self.set_initial_color(current)
        self.set_recent_colors(recent_colors())

    def _theme_stylesheet(self) -> str:
        self._screen_button.setIcon(_eyedropper_icon())
        return f"QDialog#SubtitleColorDialog {{ background: {palette().panel_bg}; }}"

    def currentColor(self) -> QColor:  # noqa: N802
        return QColor(self._current)

    def selectedColor(self) -> QColor:  # noqa: N802
        return QColor(self._selected)

    def set_initial_color(self, color: QColor) -> None:
        color = QColor(color) if color.isValid() else QColor("#FFFFFF")
        self._selected = QColor()
        self._deleted_recent_rgba: set[int] = set()
        self._hue, self._saturation = 0.0, 0.0
        self._original_swatch.set_color(color)
        self.setCurrentColor(color)

    def setCurrentColor(self, color: QColor) -> None:  # noqa: N802
        if not color.isValid():
            return
        hue, saturation, value, _alpha = color.getHsvF()
        if hue >= 0:
            self._hue = hue * 360
        if value > 0:
            self._saturation = saturation
        self._value = value
        self._apply_color(color)

    def _apply_color(self, color: QColor) -> None:
        changed = color.rgba() != self._current.rgba()
        self._current = QColor(color)
        self._sync_controls()
        if changed:
            self.currentColorChanged.emit(QColor(color))

    def _sync_controls(self) -> None:
        self._color_plane.set_color(self._hue, self._saturation)
        full_value = QColor.fromHsvF(self._hue / 360, self._saturation, 1)
        self._value_slider.set_color(full_value, round(self._value * 255))
        self._alpha_slider.set_color(self._current)
        self._current_swatch.set_color(self._current)
        self._recent_list.set_selected_color(self._current)
        self._hex_edit.setText(_normalize_hex(self._current.name(QColor.NameFormat.HexArgb)))
        self._hex_hint.hide()
        self._ok_button.setEnabled(True)

    def _set_hue_saturation(self, hue: float, saturation: float) -> None:
        self._hue, self._saturation = hue, saturation
        self._apply_hsv()

    def _apply_hsv(self) -> None:
        self._apply_color(QColor.fromHsvF(self._hue / 360, self._saturation, self._value,
                                         self._current.alphaF()))

    def _set_value(self, value: int) -> None:
        self._value = value / 255
        self._apply_hsv()

    def _set_current_alpha(self, alpha: int) -> None:
        color = self.currentColor()
        color.setAlpha(alpha)
        self._apply_color(color)

    def _edit_hex(self, text: str) -> None:
        parsed = _parse_hex_color(text)
        if parsed is None:
            self._hex_hint.show()
            self._ok_button.setEnabled(False)
            return
        cursor = self._hex_edit.cursorPosition()
        self.setCurrentColor(QColor(parsed))
        # Keep the user's partially typed spelling and cursor while updating
        # the other controls; normalize once focus leaves the editor.
        self._hex_edit.setText(text)
        self._hex_edit.setCursorPosition(cursor)

    def _finish_hex(self) -> None:
        if _parse_hex_color(self._hex_edit.text()) is not None:
            self._sync_controls()

    def set_recent_colors(self, colors: list[QColor]) -> None:
        self._recent_list.set_colors(colors)
        self._recent_list.set_selected_color(self._current)

    def _delete_current_color(self) -> None:
        selected = self._recent_list.selected_color
        if not selected.isValid():
            return
        self._deleted_recent_rgba.add(selected.rgba())
        remove_recent_color(selected)
        self.set_recent_colors(recent_colors())

    def remember_selected_color(self, color: QColor) -> None:
        # Accepting the current preview must not immediately undo a deletion.
        if color.rgba() not in self._deleted_recent_rgba:
            record_recent_color(color)

    def _record_dropped_color(self, color: QColor) -> None:
        self._deleted_recent_rgba.discard(color.rgba())
        record_recent_color(color)
        self.set_recent_colors(recent_colors())

    def _pick_screen_color(self) -> None:
        if self._screen_picker is not None:
            return
        original = self.currentColor()
        picked = False
        picker = ScreenColorPicker()
        picker.setParent(self, picker.windowFlags())
        self._screen_picker = picker

        def confirm(color: QColor) -> None:
            nonlocal picked
            picked = True
            self.setCurrentColor(color)
            self.set_recent_colors(recent_colors())

        def finish() -> None:
            self._screen_picker = None
            if not picked:
                self.setCurrentColor(original)
            self._ensure_active()

        picker.colorHovered.connect(self.setCurrentColor)
        picker.colorPicked.connect(confirm)
        picker.finished.connect(finish)
        picker.start()

    def accept(self) -> None:
        if _parse_hex_color(self._hex_edit.text()) is None:
            self._hex_edit.setFocus()
            return
        self._selected = self.currentColor()
        super().accept()

    def done(self, result: int) -> None:
        if self._screen_picker is not None:
            self._screen_picker.cancel()
        super().done(result)

    def _ensure_active(self) -> None:
        if not sip.isdeleted(self) and self.isVisible():
            self.raise_()
            self.activateWindow()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._ensure_active()
        QTimer.singleShot(0, self._ensure_active)


_shared_color_dialog: Optional[_ColorDialog] = None

_recent_color_store = RecentColorStore()


def recent_colors() -> list[QColor]:
    """Shared user history, newest first, including alpha and across restarts."""
    try:
        return [QColor(color) for color in _recent_color_store.load()]
    except Exception:
        logging.getLogger(__name__).warning("读取最近颜色失败", exc_info=True)
        return []


def clear_recent_colors() -> None:
    _recent_color_store.clear()


def record_recent_color(color: QColor) -> None:
    """Move ``color`` to the front of the recent list, dropping duplicates."""
    if not color.isValid():
        return
    try:
        _recent_color_store.remember(color.name(QColor.NameFormat.HexArgb))
    except Exception:
        logging.getLogger(__name__).warning("保存最近颜色失败", exc_info=True)


def remove_recent_color(color: QColor) -> None:
    """Remove only the exact RGBA entry, leaving same-RGB variants alone."""
    if color.isValid():
        try:
            _recent_color_store.remove(color.name(QColor.NameFormat.HexArgb))
        except Exception:
            logging.getLogger(__name__).warning("删除最近颜色失败", exc_info=True)


def _refresh_recent_colors(dialog: _ColorDialog) -> None:
    """Refresh the dialog history before opening."""
    dialog.set_recent_colors(recent_colors())


def _alive_color_dialog() -> Optional[_ColorDialog]:
    """Return the cached dialog unless Qt already destroyed its C++ object."""
    dialog = _shared_color_dialog
    if dialog is None or sip.isdeleted(dialog):
        return None
    return dialog


def prewarm_color_dialog() -> None:
    """Build the shared color dialog ahead of the first click.

    Building the Fluent inputs and painted surfaces at panel creation time
    moves that cost off the first color click.
    """
    global _shared_color_dialog
    if _alive_color_dialog() is not None:
        return
    dialog = _ColorDialog(QColor("#FFFFFF"))
    # sizeHint() forces the internal layout pass so the later exec() only
    # needs to show the already-built widget tree.
    dialog.sizeHint()
    _shared_color_dialog = dialog


def _adopt_color_dialog_owner(
    dialog: _ColorDialog, parent: Optional[QWidget]
) -> None:
    """Make the cached dialog transient for the window it is opened from.

    Windows only keeps a dialog above the window that owns it. The shared
    instance used to be shown without any owner, so it could come up behind
    the main window; re-own it on every open because it outlives the panel
    (and may be opened from a different top-level window next time).
    """
    anchor = parent.window() if parent is not None else None
    if anchor is None or dialog.parentWidget() is anchor:
        return
    dialog.setParent(anchor, dialog.windowFlags())


def _release_color_dialog_owner(dialog: _ColorDialog) -> None:
    """Detach the cached dialog so a closed window cannot take it with it.

    The dialog is reused across panels. Keeping the owner between opens would
    let Qt destroy the cached instance together with that window and leave
    the module-level cache pointing at a dead C++ object.
    """
    try:
        if dialog.parentWidget() is None:
            return
        dialog.setParent(None, dialog.windowFlags())
    except RuntimeError:
        # exec() 期间父窗口被销毁时，对话框也已经被 Qt 连带删除了。
        pass


def _select_color(current: QColor, parent: QWidget, title: str) -> QColor:
    """Open the regular color dialog used by the palette action.

    The shared instance is re-owned and reset on every open, retaining
    shared user history while keeping the original-color comparison fresh.
    """
    global _shared_color_dialog
    dialog = _alive_color_dialog()
    if dialog is None:
        dialog = _ColorDialog(current, parent)
        _shared_color_dialog = dialog
    _adopt_color_dialog_owner(dialog, parent)
    dialog.setWindowTitle(title)
    dialog.set_initial_color(current)
    try:
        _refresh_recent_colors(dialog)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return QColor()
        selected = dialog.selectedColor()
        dialog.remember_selected_color(selected)
        return selected
    finally:
        _release_color_dialog_owner(dialog)


