"""Reusable color-entry and screen-picking controls for property editors."""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import QPoint, QPointF, QRect, QTimer, Qt, pyqtSignal as Signal
from PyQt6.QtGui import (
    QColor,
    QCursor,
    QIcon,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PyQt6.QtWidgets import (
    QApplication,
    QColorDialog,
    QDialog,
    QHBoxLayout,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QWidget,
)
from qfluentwidgets import (
    Action,
    FluentIcon as FIF,
    LineEdit as FluentLineEdit,
    RoundMenu,
    ToolButton as FluentToolButton,
)

from krok_helper.subtitle_render.frontend.widgets.theme import palette


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
        self._hover_timer = QTimer(self)
        self._hover_timer.setInterval(16)
        self._hover_timer.timeout.connect(
            lambda: self._emit_hovered_color(QCursor.pos())
        )
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
        self._emit_hovered_color(QCursor.pos())
        # Windows may stop delivering hover-only mouseMoveEvent events after
        # the cursor leaves the application even though the final captured
        # click still arrives. Poll the global cursor while picking so the
        # inline swatch keeps previewing colors across the virtual desktop.
        self._hover_timer.start()

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
        self._hover_timer.stop()
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


