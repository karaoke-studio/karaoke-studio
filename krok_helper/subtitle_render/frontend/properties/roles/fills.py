"""Fill-editor pages used by the role color section."""

from __future__ import annotations

import json
import math
from typing import Any, Callable, Optional

from PyQt6.QtCore import QPointF, QRectF, QSize, Qt, pyqtSignal as Signal
from PyQt6.QtGui import (
    QColor,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPolygonF,
)
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QGridLayout,
    QHBoxLayout,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    Action,
    CaptionLabel,
    CheckBox,
    FluentIcon as FIF,
    LineEdit as FluentLineEdit,
    PlainTextEdit as FluentPlainTextEdit,
    PushButton as FluentPushButton,
    RoundMenu,
    TransparentToolButton as FluentTransparentToolButton,
)

from krok_helper.qfluent_compat import ModelessDialog
from krok_helper.subtitle_render.domain.paint import PaintFill
from krok_helper.subtitle_render.frontend.dialogs.fluent_dialogs import (
    fluent_button_row,
)
from krok_helper.subtitle_render.frontend.properties.color_controls import (
    _normalize_hex,
    _parse_hex_color,
)
from krok_helper.subtitle_render.frontend.properties.controls.layout import (
    compact_property_control,
    property_field,
)
from krok_helper.subtitle_render.frontend.widgets.theme import palette, themed


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


class _GradientStopsPasteDialog(ModelessDialog):
    """Import portable gradient-stop JSON into the current gradient bar."""

    def __init__(self, text: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent.window() if parent is not None else None)
        self.setWindowTitle("粘贴渐变信息")
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.setMinimumSize(520, 360)
        self._stops: list[tuple[float, str]] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(10)

        hint = CaptionLabel(
            "粘贴 Lin-K Lyrics 渐变关键点 JSON。应用后仅替换当前渐变条的颜色和位置。",
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
