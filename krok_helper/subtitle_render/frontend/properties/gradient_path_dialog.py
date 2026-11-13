"""HSV route drawing UI; applying a draft emits ordinary gradient stops."""

from __future__ import annotations

import math
from copy import deepcopy
from dataclasses import replace

from PyQt6 import sip
from PyQt6.QtCore import QPointF, QRectF, Qt, QTimer, pyqtSignal as Signal
from PyQt6.QtGui import QColor, QLinearGradient, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import QHBoxLayout, QSizePolicy, QVBoxLayout, QWidget
from qfluentwidgets import CaptionLabel, LineEdit, PushButton, ToolButton

from krok_helper.qfluent_compat import ModelessDialog
from krok_helper.subtitle_render.domain.gradient_path import (
    MAX_PATH_POINTS, clamp_point, disk_point, hue_route, path_segment_indices, replace_path_stops,
    simplify_points, smooth_points,
)
from krok_helper.subtitle_render.domain.paint import GradientPath, PaintFill
from krok_helper.subtitle_render.frontend.dialogs.fluent_dialogs import fluent_button_row
from krok_helper.subtitle_render.frontend.properties.color_picker_widgets import HueSaturationWheel
from krok_helper.subtitle_render.frontend.properties.color_controls import (
    ScreenColorPicker, _eyedropper_icon, _normalize_hex, _parse_hex_color,
)
from krok_helper.subtitle_render.frontend.widgets.theme import palette, themed
from krok_helper.workspace_switcher import WorkspaceSwitcher


class GradientPathWheel(HueSaturationWheel):
    """Cached HSV disk with route drawing and endpoint color picking."""

    pathChanged = Signal()
    editStarted = Signal()
    endpointPicked = Signal(int, float, float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.points = [(1.0, 0.0), (0.0, 1.0)]
        self.adjusting = False
        self.endpoint_index = None
        self._gesture = False
        self._handle = None
        self._raw = []
        # The disk uses the space left by the controls, including on short or
        # scaled screens. Its preferred 340px height must never force scrolling.
        self.setMinimumSize(100, 100)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Ignored)
        self._last_position = QPointF()
        self.setAccessibleName("渐变路径色盘")
        self.setToolTip("绘制模式下自由画线；调整模式下拖动端点改色或修整中间路径。")

    def surface_rect(self) -> QRectF:
        side = max(min(self.width(), self.height()) - 30, 1)
        return QRectF((self.width() - side) / 2, (self.height() - side) / 2, side, side)

    def to_screen(self, point):
        rect = self.surface_rect()
        return rect.center() + QPointF(point[0], -point[1]) * (rect.width() / 2)

    def from_screen(self, point):
        rect = self.surface_rect()
        radius = rect.width() / 2
        return clamp_point(((point.x() - rect.center().x()) / radius,
                            (rect.center().y() - point.y()) / radius))

    def set_points(self, points):
        self.points = list(points)
        self.update()

    def set_adjusting(self, adjusting):
        self.adjusting = adjusting
        self.update()

    def endpoint_position(self, index):
        first, last = self.to_screen(self.points[0]), self.to_screen(self.points[-1])
        position = first if index == 0 else last
        if math.hypot(first.x() - last.x(), first.y() - last.y()) < 24:
            # Coincident HSV coordinates (e.g. two grays) must remain draggable.
            position += QPointF(0, -13 if index == 0 else 13)
            position.setY(max(12, min(self.height() - 12, position.y())))
        return position

    def _changed(self):
        self.update()
        self.pathChanged.emit()

    def mousePressEvent(self, event):  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            return
        endpoint_distances = []
        for i in (0, 1) if self.adjusting else ():
            delta = event.position() - self.endpoint_position(i)
            endpoint_distances.append((math.hypot(delta.x(), delta.y()), i))
        distance, index = min(endpoint_distances, default=(float("inf"), 0))
        self.endpoint_index = index if distance <= 14 else None
        rect = self.surface_rect()
        delta = event.position() - rect.center()
        if self.endpoint_index is None and math.hypot(delta.x(), delta.y()) > rect.width() / 2 + .5:
            return
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        if self.adjusting and self.endpoint_index is None:
            candidates = [(math.dist((event.position().x(), event.position().y()),
                                    (self.to_screen(p).x(), self.to_screen(p).y())), i)
                          for i, p in enumerate(self.points[1:-1], 1)]
            if not candidates or min(candidates)[0] > 12:
                return
            self._handle = min(candidates)[1]
        self.editStarted.emit()
        self._gesture = True
        self._last_position = event.position()
        if self.endpoint_index is None and not self.adjusting:
            point = self.from_screen(event.position())
            self._raw = [point]
            self.points = [point, point]
            self._changed()
        event.accept()

    def mouseMoveEvent(self, event):  # noqa: N802
        if not self._gesture:
            return
        point = self.from_screen(event.position())
        if self.endpoint_index is not None:
            if event.position() == self._last_position:
                return
            self._last_position = event.position()
            self.endpointPicked.emit(self.endpoint_index, *point)
            event.accept()
            return
        elif self.adjusting:
            self.points[self._handle] = point
        else:
            if math.dist(self._raw[-1], point) < .006:
                return
            if len(self._raw) >= MAX_PATH_POINTS - 2:
                self._raw = [self._raw[0]] + self._raw[1::2]
            self._raw.append(point)
            self.points = list(self._raw)
        self._changed()
        event.accept()

    def mouseReleaseEvent(self, event):  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton or not self._gesture:
            return
        self.mouseMoveEvent(event)
        self._gesture = False
        if not self.adjusting and self.endpoint_index is None:
            point = self.from_screen(event.position())
            if self.points[-1] != point:
                self.points.append(point)
            points = smooth_points(self.points)
            # Large scribbles remain bounded for persistence and sampling.
            tolerance = .003
            while len(points) > MAX_PATH_POINTS:
                tolerance *= 2
                points = simplify_points(points, tolerance)
            self.points = points
        self._handle = None
        if self.endpoint_index is None:
            self._changed()
        self.endpoint_index = None
        self.update()
        event.accept()

    def keyPressEvent(self, event):  # noqa: N802
        # The single-color wheel's hue shortcuts must not alter route endpoints.
        QWidget.keyPressEvent(self, event)

    def paintEvent(self, event):  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.surface_rect()
        painter.drawImage(rect.topLeft(), self.surface_image())
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(palette().card_border), 1))
        painter.drawEllipse(rect)
        path = QPainterPath(self.to_screen(self.points[0]))
        for point in self.points[1:]:
            path.lineTo(self.to_screen(point))
        painter.setPen(QPen(QColor("#242424"), 4))
        painter.drawPath(path)
        painter.setPen(QPen(QColor("#FFFFFF"), 2, Qt.PenStyle.DashLine))
        painter.drawPath(path)
        if self.adjusting:
            painter.setPen(QPen(QColor("#FFFFFF"), 1.5))
            painter.setBrush(QColor("#242424"))
            for point in self.points[1:-1]:
                painter.drawEllipse(self.to_screen(point), 3.5, 3.5)
        if self.adjusting:
            for index, label in enumerate(("起", "终")):
                position = self.endpoint_position(index)
                painter.setBrush(QColor(palette().accent_primary if self.endpoint_index == index else "#242424"))
                painter.setPen(QPen(QColor("#FFFFFF"), 2))
                painter.drawEllipse(position, 11, 11)
                painter.drawText(QRectF(position.x() - 10, position.y() - 10, 20, 20),
                                 Qt.AlignmentFlag.AlignCenter, label)
        painter.end()


class _EndpointColorEditor(QWidget):
    """Compact hexadecimal entry for one draggable endpoint."""

    colorChanged = Signal(str)
    entryChanged = Signal()
    screenPickRequested = Signal()

    def __init__(self, label: str, parent=None):
        super().__init__(parent)
        self._color = QColor("#FFFFFF")
        self._editing = False
        self._entry_alpha = 255
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addWidget(CaptionLabel(label, self))
        self.hex_edit = LineEdit(self)
        self.hex_edit.setMinimumWidth(90)
        self.hex_edit.setMaxLength(9)
        self.hex_edit.setAccessibleName(f"{label}十六进制色号")
        self.hex_edit.setPlaceholderText("#RRGGBB")
        self.hex_edit.setToolTip("支持 RGB 色号；#AARRGGBB 可同时设置透明度")
        self.hex_edit.textEdited.connect(self._edit_color)
        self.hex_edit.editingFinished.connect(self._finish_edit)
        layout.addWidget(self.hex_edit, 1)
        self.screen_picker_button = ToolButton(_eyedropper_icon(), self)
        self.screen_picker_button.setFixedSize(30, 30)
        self.screen_picker_button.setToolTip(f"从屏幕拾取{label}颜色")
        self.screen_picker_button.setAccessibleName(f"从屏幕拾取{label}颜色")
        self.screen_picker_button.clicked.connect(self.screenPickRequested.emit)
        layout.addWidget(self.screen_picker_button)

    def set_color(self, color: str):
        self._color = QColor(color)
        if not (self._editing and self.hex_edit.hasFocus()):
            self.hex_edit.setText(_normalize_hex(color))

    def _edit_color(self):
        if not self._editing:
            self._entry_alpha = self._color.alpha()
        self._editing = True
        parsed = _parse_hex_color(self.hex_edit.text())
        if parsed is not None:
            color = QColor(parsed)
            if len(self.hex_edit.text().strip().lstrip("#")) in (3, 6):
                color.setAlpha(self._entry_alpha)
            self.colorChanged.emit(color.name(QColor.NameFormat.HexArgb).upper())
        self.entryChanged.emit()

    def is_valid(self):
        return _parse_hex_color(self.hex_edit.text()) is not None

    def _finish_edit(self):
        if self.is_valid():
            self._edit_color()
            self.hex_edit.setText(_normalize_hex(self._color.name(QColor.NameFormat.HexArgb)))
        self._editing = False


class _GradientPreview(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.stops = []
        self.setFixedHeight(40)
        self.setAccessibleName("勾勒渐变预览")

    def paintEvent(self, event):  # noqa: N802
        painter = QPainter(self)
        rect = self.rect()
        for y in range(0, rect.height(), 8):
            for x in range(0, rect.width(), 8):
                painter.fillRect(x, y, 8, 8, QColor("#CCCCCC" if (x // 8 + y // 8) % 2 else "#FFFFFF"))
        gradient = QLinearGradient(0, 0, rect.width(), 0)
        for position, color in self.stops:
            gradient.setColorAt(position / 100, QColor(color))
        painter.fillRect(rect, gradient)
        painter.end()


class GradientPathDialog(ModelessDialog):
    """Keep editing after publishing a fill; undo also updates its live host."""

    applyRequested = Signal(object)

    def __init__(self, fill: PaintFill, parent=None):
        super().__init__(parent.window() if parent is not None else None)
        self.setObjectName("GradientPathDialog")
        themed(self, lambda: f"QDialog#GradientPathDialog {{ background: {palette().panel_bg}; }}")
        self.setWindowTitle("勾勒渐变")
        available_height = self.screen().availableGeometry().height() if self.screen() else 800
        self.resize(480, min(660, max(360, available_height - 60)))
        self._original_fill = deepcopy(fill)
        self._applied_fill = deepcopy(fill)
        self._applied_once = False
        self._apply_error = ""
        self._screen_picker: ScreenColorPicker | None = None
        self.finished.connect(self._cancel_screen_picker)
        self._original = sorted(fill.gradient_stops or [(0, fill.start_color), (100, fill.end_color)], key=lambda stop: stop[0])
        self._base = list(self._original)
        self._stops = list(self._original)
        self._dirty = False
        self._route_mode = None
        self._history = []
        self._saved_route = deepcopy(fill.gradient_path)
        if self._saved_route and (self._saved_route.start_pct, self._saved_route.end_pct) != (0, 100):
            # Older editing metadata may describe a segment. Its baked colors
            # remain intact; this editor always starts from the whole gradient.
            self._saved_route = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._refresh)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 12, 16, 14)
        root.setSpacing(8)
        self.mode_switch = WorkspaceSwitcher(self)
        self.mode_switch.setAccessibleName("渐变编辑模式")
        for mode, label in (("adjust", "调整模式"), ("draw", "绘制模式")):
            self.mode_switch.addItem(mode, label).setAccessibleName(label)
        root.addWidget(self.mode_switch, 0, Qt.AlignmentFlag.AlignHCenter)
        body = QWidget(self)
        root.addWidget(body, 1)
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.endpoint_editors = []
        endpoint_row = QHBoxLayout()
        endpoint_row.setSpacing(12)
        for i, label in enumerate(("起点", "终点")):
            editor = _EndpointColorEditor(label, self)
            editor.colorChanged.connect(lambda color, index=i: self._set_endpoint_color(index, color))
            editor.entryChanged.connect(self._validate_color_entries)
            editor.screenPickRequested.connect(lambda index=i: self._begin_screen_color_pick(index))
            endpoint_row.addWidget(editor, 1)
            self.endpoint_editors.append(editor)
        layout.addLayout(endpoint_row)
        self.hint_label = CaptionLabel("按住鼠标自由画线，新线会完整替换当前路径。", self)
        self.hint_label.setWordWrap(True)
        layout.addWidget(self.hint_label)
        self.wheel = GradientPathWheel(self)
        layout.addWidget(self.wheel, 1)
        tools = QHBoxLayout()
        self.short_button = PushButton("短路线", self)
        self.long_button = PushButton("长路线", self)
        self.short_button.clicked.connect(lambda: self._generate(False))
        self.long_button.clicked.connect(lambda: self._generate(True))
        tools.addWidget(self.short_button)
        tools.addWidget(self.long_button)
        layout.addLayout(tools)
        actions = QHBoxLayout()
        self.undo_button = PushButton("撤销", self)
        self.undo_button.clicked.connect(self._undo)
        self.reverse_button = PushButton("反转渐变", self)
        self.reverse_button.clicked.connect(self._reverse)
        actions.addWidget(self.undo_button)
        actions.addWidget(self.reverse_button)
        layout.addLayout(actions)
        footer = QWidget(self)
        footer_layout = QVBoxLayout(footer)
        footer_layout.setContentsMargins(0, 6, 0, 0)
        footer_layout.setSpacing(8)
        root.addWidget(footer)
        self.preview = _GradientPreview(self)
        footer_layout.addWidget(self.preview)
        self.status_label = CaptionLabel("", self)
        self.status_label.setWordWrap(True)
        footer_layout.addWidget(self.status_label)
        row, self.apply_button, self.close_button = fluent_button_row(
            self, ok_text="应用渐变", cancel_text="关闭"
        )
        footer_layout.addLayout(row)
        self.wheel.editStarted.connect(self._remember)
        self.wheel.pathChanged.connect(self._schedule_refresh)
        self.wheel.endpointPicked.connect(self._pick_endpoint)
        self.mode_switch.currentItemChanged.connect(self._change_edit_mode)
        self.mode_switch.setCurrentItem("draw")
        self._load_initial()

    def _colors(self):
        left, right = 0, 100
        return (next(c for p, c in reversed(self._base) if p == left),
                next(c for p, c in self._base if p == right))

    def _load_initial(self):
        self._timer.stop()
        self._base = list(self._original)
        self._stops = list(self._original)
        self._history.clear()
        self._dirty = False
        self._route_mode = None
        left, right = 0, 100
        route = self._saved_route
        if route and (left, right) == (route.start_pct, route.end_pct):
            self.wheel.set_points(route.points)
        else:
            first, last = path_segment_indices(self._base, left, right)
            self.wheel.set_points([disk_point(c) for p, c in self._base[first:last + 1]])
        self._refresh()

    def _remember(self):
        self._history.append((list(self.wheel.points), list(self._base), self._dirty, self._route_mode))
        self._history = self._history[-20:]
        self.undo_button.setEnabled(True)

    def _undo(self):
        if self._history:
            points, self._base, self._dirty, self._route_mode = self._history.pop()
            self.wheel.set_points(points)
            self._refresh()
            if self._applied_once and not self._apply_error and self._validate_color_entries():
                self.applyRequested.emit(self._draft_fill())

    def _schedule_refresh(self, *, preserve_route_mode=False):
        self._dirty = True
        if not preserve_route_mode:
            self._route_mode = None
        if not self.wheel.adjusting:
            # A new stroke samples the displayed HSV disk, including its own
            # endpoints. Previous endpoint brightness must not anchor the line.
            first, last = path_segment_indices(self._base, 0, 100)
            for stop_index, point in ((first, self.wheel.points[0]), (last, self.wheel.points[-1])):
                color = QColor(self._base[stop_index][1])
                hue = math.atan2(point[1], point[0]) / math.tau % 1
                picked = QColor.fromHsvF(hue, min(1, math.hypot(*point)), 1, color.alphaF())
                self._base[stop_index] = (self._base[stop_index][0], picked.name(QColor.NameFormat.HexArgb).upper())
        if not self._timer.isActive():
            self._timer.start()

    def _generate(self, long):
        self._remember()
        self._route_mode = "long" if long else "short"
        self.wheel.set_points(simplify_points(hue_route(*self._colors(), long=long), .006))
        self.mode_switch.setCurrentItem("adjust")
        self._dirty = True
        self._refresh()

    def _reverse(self):
        self._remember()
        left, right = 0, 100
        first, last = path_segment_indices(self._base, left, right)
        self._base[first:last + 1] = [(left if p == right else right if p == left else round(left + right - p, 6), c)
                                       for p, c in reversed(self._base[first:last + 1])]
        self.wheel.set_points(list(reversed(self.wheel.points)))
        self._dirty = True
        self._refresh()

    def route(self) -> GradientPath:
        return GradientPath(list(self.wheel.points))

    def _change_edit_mode(self, mode):
        self.wheel.set_adjusting(mode == "adjust")
        self.hint_label.setText("拖动起点、终点改色，拖动中间控制点修整路径。" if mode == "adjust"
                                else "按住鼠标自由画线，新线会完整替换当前路径。")

    def _pick_endpoint(self, index, x, y):
        previous = QColor(self._colors()[index])
        hue = math.atan2(y, x) / math.tau % 1
        color = QColor.fromHsvF(hue, min(1, math.hypot(x, y)),
                               previous.valueF() or 1.0, previous.alphaF())
        self._set_endpoint_color(index, color.name(QColor.NameFormat.HexArgb),
                                 remember=False, point=(x, y))

    def _set_endpoint_color(self, index, color, *, remember=True, point=None):
        old = QColor(self._colors()[index])
        current = QColor(color)
        if current == old and point is None:
            return
        if remember:
            self._remember()
        first, last = path_segment_indices(self._base, 0, 100)
        position = first if index == 0 else last
        self._base[position] = (self._base[position][0], current.name(QColor.NameFormat.HexArgb).upper())
        points = list(self.wheel.points)
        points[0 if index == 0 else -1] = point if point is not None else disk_point(color)
        if self._route_mode is not None:
            points = simplify_points(hue_route(*self._colors(), long=self._route_mode == "long"), .006)
        self.wheel.set_points(points)
        if remember:
            self.mode_switch.setCurrentItem("adjust")
            self._refresh_endpoint_controls()
        self._schedule_refresh(preserve_route_mode=True)

    def _refresh_endpoint_controls(self):
        for editor, color in zip(self.endpoint_editors, self._colors()):
            editor.set_color(color)

    def _begin_screen_color_pick(self, index: int) -> None:
        self._cancel_screen_picker()
        editor = self.endpoint_editors[index]
        original = QColor(self._colors()[index])
        original_text = editor.hex_edit.text()
        accepted = False
        picker = ScreenColorPicker()
        self._screen_picker = picker

        def picked_color(color: QColor) -> QColor:
            result = QColor(color)
            # Desktop pixels supply RGB, not the endpoint's opacity.
            result.setAlpha(original.alpha())
            return result

        def preview(color: QColor) -> None:
            editor.set_color(picked_color(color).name(QColor.NameFormat.HexArgb))

        def accept(color: QColor) -> None:
            nonlocal accepted
            if not color.isValid():
                return
            accepted = True
            chosen = picked_color(color).name(QColor.NameFormat.HexArgb)
            editor.set_color(chosen)
            self._set_endpoint_color(index, chosen)

        def finish() -> None:
            if self._screen_picker is picker:
                self._screen_picker = None
            if sip.isdeleted(self) or sip.isdeleted(editor):
                return
            if not accepted:
                editor.set_color(original.name(QColor.NameFormat.HexArgb))
                editor.hex_edit.setText(original_text)
            self._validate_color_entries()
            if self.isVisible():
                self.activateWindow()

        picker.colorHovered.connect(preview)
        picker.colorPicked.connect(accept)
        picker.finished.connect(finish)
        self.destroyed.connect(picker.cancel)
        picker.start()

    def _cancel_screen_picker(self) -> None:
        if self._screen_picker is not None:
            self._screen_picker.cancel()

    def closeEvent(self, event) -> None:  # noqa: N802
        self._cancel_screen_picker()
        super().closeEvent(event)

    def _validate_color_entries(self):
        valid = all(editor.is_valid() for editor in self.endpoint_editors)
        self.apply_button.setEnabled(self._dirty and valid and not self._apply_error
                                     and self._draft_fill() != self._applied_fill)
        if self._apply_error:
            self.status_label.setText(self._apply_error)
        elif not valid:
            self.status_label.setText("请输入有效的十六进制色号，如 #FF8800。")
        self.status_label.setVisible(bool(self.status_label.text()))
        return valid

    def stops(self) -> list[tuple[float, str]]:
        self._refresh()
        return list(self._stops)

    def _refresh(self):
        self._timer.stop()
        self._stops = replace_path_stops(self._base, self.route()) if self._dirty else list(self._original)
        self.preview.stops = self._stops
        self.preview.update()
        self._refresh_endpoint_controls()
        self.apply_button.setEnabled(self._dirty)
        self.undo_button.setEnabled(bool(self._history))
        self.status_label.setText("已应用到字幕，可继续调整或撤销。" if self._applied_once and self._draft_fill() == self._applied_fill
                                  else f"已自动生成 {len(self._stops)} 个色标，可继续调整路径。" if self._dirty
                                  else "")
        self._validate_color_entries()

    def _draft_fill(self) -> PaintFill:
        if not self._dirty:
            return deepcopy(self._original_fill)
        return replace(self._original_fill, gradient_stops=list(self._stops),
                       start_color=self._stops[0][1], end_color=self._stops[-1][1],
                       gradient_path=self.route())

    def confirm_applied(self, fill: PaintFill) -> None:
        self._applied_fill = deepcopy(fill)
        self._applied_once = True
        self._refresh()

    def set_apply_error(self, message: str) -> None:
        self._apply_error = message
        self._validate_color_entries()

    def accept(self):
        self._refresh()
        if self.apply_button.isEnabled():
            self.applyRequested.emit(self._draft_fill())
