"""Fill-editor pages used by the role color section."""

from __future__ import annotations

import json
import math
from typing import Any, Callable, Optional

from PyQt6.QtCore import QPointF, QRectF, QSize, Qt, pyqtSignal as Signal
from PyQt6.QtGui import (
    QBrush,
    QColor,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPolygonF,
    QRadialGradient,
    QTransform,
)
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QGridLayout,
    QHBoxLayout,
    QSizePolicy,
    QToolButton,
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
from krok_helper.subtitle_render.domain.color import linear_rgba, rgba_float, linear_color
from krok_helper.subtitle_render.frontend.dialogs.fluent_dialogs import (
    fluent_button_row,
)
from krok_helper.subtitle_render.frontend.properties.color_controls import (
    _normalize_hex,
    _parse_hex_color,
    display_color,
)
from krok_helper.subtitle_render.frontend.properties.controls.inputs import (
    WheelFocusedComboBox,
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
    gradientCopied = Signal()
    infoPasted = Signal()
    clipboardPasteRejected = Signal(str)

    _POINTER_BLUE = "#0B84FF"
    _POINTER_OUTLINE = "#46505F"
    _POINTER_GAP = 3
    _POINTER_ARROW_LENGTH = 6
    _POINTER_BODY_LENGTH = 18
    _POINTER_HALF_THICKNESS = 5
    _BAR_LEFT = 12
    _BAR_WIDTH = 48
    _BAR_MIN_WIDTH = 1
    _BAR_RIGHT_MARGIN = 4
    # Drop-to-merge windows (percent of the bar): releasing within these
    # distances of another marker / of a bar end counts as dropping onto it,
    # because a mouse release can never land pixel-exact. The end windows are
    # narrower so a drop near an edge snaps flush onto the endpoint.
    _DROP_MERGE_INTERIOR_PCT = 1.0
    _DROP_MERGE_ENDPOINT_PCT = 0.5

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._stops: list[tuple[float, str]] = [(0, "#FFFFFF"), (100, "#FF5A6F")]
        self._selected = 0
        self._orientation = "horizontal"
        self._hard_edges = False
        self._dragging = False
        self._drag_moved = False
        self._drag_snapshot: Optional[list[tuple[float, str]]] = None
        self._dragged_snapshot_index: Optional[int] = None
        self._last_drag_position: Optional[float] = None
        self.setMinimumHeight(52)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def sizeHint(self) -> QSize:  # noqa: N802
        height = 140 if self._orientation == "vertical" else 52
        if self._orientation == "vertical":
            # 12 left + 48 bar + 3 gap + 6 arrow + 18 tag + 4 right.
            # Every color bar uses the same external pointer geometry.
            return QSize(self._vertical_total_width(), height)
        return QSize(220, height)

    def _vertical_total_width(self) -> int:
        return (
            self._BAR_LEFT
            + self._BAR_WIDTH
            + self._POINTER_GAP
            + self._POINTER_ARROW_LENGTH
            + self._POINTER_BODY_LENGTH
            + self._BAR_RIGHT_MARGIN
        )

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
        # The panel round-trips stops back into set_stops on every drag frame;
        # among equal-position candidates keep the previously selected marker,
        # otherwise the neighbour would steal the ongoing drag.
        self._selected = min(
            range(len(self._stops)),
            key=lambda index: (
                abs(self._stops[index][0] - selected_position),
                abs(index - self._selected),
            ),
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
        # An explicit value (spin box) is a one-shot drag: move from a fresh
        # baseline, then settle immediately like a drop.
        self._begin_drag()
        self._drag_moved = True
        self._apply_drag_position(position)
        self._end_drag(merge=True)

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
                        display_color(color),
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
                    gradient.setColorAt(position / 100.0, display_color(color))
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
        if self._dragging:
            self._end_drag(merge=False)
        hit_rect = self._bar_rect().adjusted(-8, -8, 8, 8).united(
            self._pointer_lane_rect().adjusted(-10, -10, 10, 10)
        )
        if nearest is not None:
            self._selected = nearest
            self.selectedChanged.emit(self._selected)
            self.update()
            self._begin_drag()
        elif hit_rect.contains(event.position()):
            self.add_stop(pos)
            self._begin_drag()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if not self._dragging:
            return
        self._apply_drag_position(self._position_from_point(event.position()))

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802, ARG002
        if not self._dragging:
            return
        self._end_drag(merge=True)

    def contextMenuEvent(self, event) -> None:  # noqa: N802
        label = "拼色" if self._hard_edges else "渐变"
        menu = RoundMenu(parent=self)
        copy_action = Action(f"复制{label}信息", menu)
        copy_action.triggered.connect(self.copy_gradient_info)
        menu.addAction(copy_action)
        paste_action = Action(f"粘贴{label}信息…", menu)
        paste_action.triggered.connect(self.paste_gradient_info)
        menu.addAction(paste_action)
        menu.exec(event.globalPos())
        event.accept()

    def copy_gradient_info(self) -> str:
        text = _gradient_stops_to_json(self._stops)
        QApplication.clipboard().setText(text)
        self.gradientCopied.emit()
        return text

    def paste_gradient_info(self) -> bool:
        dialog = _GradientStopsPasteDialog(
            QApplication.clipboard().text(),
            parent=self,
            label="拼色" if self._hard_edges else "渐变",
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return False
        self._apply_pasted_stops(dialog.stops())
        return True

    def paste_clipboard_info(self) -> bool:
        """Paste valid clipboard data immediately; offer the editor for invalid data."""
        try:
            stops = _gradient_stops_from_json(QApplication.clipboard().text())
        except ValueError as exc:
            self.clipboardPasteRejected.emit(str(exc))
            return self.paste_gradient_info()
        self._apply_pasted_stops(stops)
        return True

    def _apply_pasted_stops(self, stops: list[tuple[float, str]]) -> None:
        self.set_stops(stops)
        self.stopsChanged.emit(list(self._stops))
        self.infoPasted.emit()

    def _bar_rect(self) -> QRectF:
        if self._orientation == "horizontal":
            return QRectF(15, 8, max(self.width() - 30, 1), 22)
        return QRectF(
            self._BAR_LEFT, 15, self._vertical_bar_width(), max(self.height() - 30, 1)
        )

    def _vertical_bar_width(self) -> float:
        # Under horizontal squeeze the bar shrinks before the right-side
        # pointer lane (tag) does. Floor of 1px matches the other adaptive
        # spans (horizontal bar width / vertical bar height).
        reserved = (
            self._BAR_LEFT
            + self._POINTER_GAP
            + self._POINTER_ARROW_LENGTH
            + self._POINTER_BODY_LENGTH
            + self._BAR_RIGHT_MARGIN
        )
        return max(min(self._BAR_WIDTH, self.width() - reserved), self._BAR_MIN_WIDTH)

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

    def _begin_drag(self) -> None:
        """Freeze the drag baseline; only one marker ever moves per drag."""
        self._dragging = True
        self._drag_moved = False
        self._drag_snapshot = list(self._stops)
        self._dragged_snapshot_index = self._selected
        self._last_drag_position = self._stops[self._selected][0]

    def _end_drag(self, *, merge: bool) -> None:
        self._dragging = False
        moved = self._drag_moved
        self._drag_moved = False
        self._drag_snapshot = None
        self._dragged_snapshot_index = None
        self._last_drag_position = None
        if merge and moved:
            # Dropping a marker onto another one merges (the moved color
            # wins). Exact equality always merges; a mouse drop additionally
            # counts within the drop windows, while merely passing over a
            # marker mid-drag never deletes anything.
            self._merge_selected_collisions(
                interior_pct=self._DROP_MERGE_INTERIOR_PCT,
                endpoint_pct=self._DROP_MERGE_ENDPOINT_PCT,
            )

    def _apply_drag_position(self, position: float) -> None:
        """Rebuild the stops from the frozen baseline with the dragged marker
        at a new position.

        The baseline plus the dragged index fully determine the result, so the
        drag never consults live state that the panel round-trip rewrites, and
        a marker visiting an endpoint mid-drag can never be mistaken for the
        endpoint anchor.
        """
        snapshot = self._drag_snapshot
        dragged_index = self._dragged_snapshot_index
        if snapshot is None or dragged_index is None:
            return
        dragged_position, dragged_color = snapshot[dragged_index]
        pos = _normalized_stop_position(position)
        if pos != self._last_drag_position:
            self._drag_moved = True
            self._last_drag_position = pos
        sole_endpoint_anchor = dragged_position in {0, 100} and not any(
            stop[0] == dragged_position
            for index, stop in enumerate(snapshot)
            if index != dragged_index
        )
        if sole_endpoint_anchor and pos != dragged_position:
            # The endpoint anchor stays put; dragging it off spawns a new stop.
            stops = list(snapshot) + [(pos, dragged_color)]
        else:
            stops = [
                stop
                for index, stop in enumerate(snapshot)
                if index != dragged_index
            ] + [(pos, dragged_color)]
        moved_stop = (pos, dragged_color)
        self._stops = _normalize_gradient_stops(stops)
        # Pin the selection to the dragged marker; an equal-position neighbour
        # must not steal it (value-identical ties are visually equivalent).
        self._selected = self._stops.index(moved_stop)
        self._emit_stops_changed()

    def _merge_selected_collisions(
        self,
        *,
        interior_pct: float = 0.0,
        endpoint_pct: float = 0.0,
    ) -> None:
        """Merge the selected stop onto a nearby stop once its position settles.

        Called when the position settles (mouse release / explicit spin value),
        never per drag frame, so passing over a marker mid-drag cannot delete
        it. Exact position equality always merges. With ``interior_pct`` a
        mouse drop within that window of an interior marker snaps onto it; with
        ``endpoint_pct`` a drop within that window of a bar end snaps flush
        onto the endpoint, so the dragged color lands exactly on the bar edge.
        The dragged color wins. The spin box passes no windows: typed values
        are exact and merge only on exact equality.
        """
        position, color = self._stops[self._selected]
        if endpoint_pct and (
            position < endpoint_pct or position > 100 - endpoint_pct
        ):
            self._snap_selected_onto(0 if position < 50 else 100, color)
            return
        nearest_index: Optional[int] = None
        nearest_delta: Optional[float] = None
        for index, (other_position, _other_color) in enumerate(self._stops):
            if index == self._selected:
                continue
            if endpoint_pct and other_position in {0, 100}:
                # Bar-end stops are governed by the narrower endpoint window.
                continue
            delta = abs(other_position - position)
            if delta > 0 and not (interior_pct and delta < interior_pct):
                continue
            if nearest_delta is None or delta < nearest_delta:
                nearest_index = index
                nearest_delta = delta
        if nearest_index is not None:
            self._snap_selected_onto(self._stops[nearest_index][0], color)

    def _snap_selected_onto(self, position: float, color: str) -> None:
        """Replace any stop at ``position`` with the selected stop moved onto
        it (dragged color wins)."""
        selected_stop = self._stops[self._selected]
        stops = [
            stop
            for stop in self._stops
            if stop[0] != position and stop is not selected_stop
        ]
        stops.append((position, color))
        self._stops = _normalize_gradient_stops(stops)
        self._selected = self._stops.index((position, color))
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
        if linear_rgba(left[1]) is not None or linear_rgba(right[1]) is not None:
            a, b = rgba_float(left[1]), rgba_float(right[1])
            return linear_color(x + (y - x) * ratio for x, y in zip(a, b))
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

    def __init__(
        self, text: str, parent: Optional[QWidget] = None, *, label: str = "渐变"
    ) -> None:
        super().__init__(parent.window() if parent is not None else None)
        self.setWindowTitle(f"粘贴{label}信息")
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.setMinimumSize(520, 360)
        self._stops: list[tuple[float, str]] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(10)

        hint = CaptionLabel(
            f"粘贴 Lin-K Lyrics {label}关键点 JSON。应用后仅替换当前{label}条的颜色和位置。",
            self,
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.text_edit = FluentPlainTextEdit(self)
        self.text_edit.setPlaceholderText(f"在此粘贴{label}信息…")
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




class RadialGradientPad(QWidget):
    """Interactive WYSIWYG pad for dragging radial gradient center and ellipse radii.

    The inner dashed rectangle represents the target text box (0%..100%).
    During a drag the coordinate frame stays frozen for 1:1 jitter-free tracking
    (even if the pointer leaves the widget); on release the viewport auto-fits
    so the text box, full ellipse, and all handles remain visible and grabbable.
    """

    geometryChanged = Signal(float, float, float, float)

    _PAD_MARGIN_X = 12.0
    _PAD_MARGIN_Y = 12.0
    _CENTER_RADIUS = 6.0
    _HANDLE_RADIUS = 5.0
    _CENTER_MIN = -50.0
    _CENTER_MAX = 150.0
    _RADIUS_MIN = 1.0
    _RADIUS_MAX = 200.0

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._stops: list[tuple[float, str]] = [(0.0, "#FFFFFF"), (100.0, "#FF5A6F")]
        self._cx = 50.0
        self._cy = 50.0
        self._rx = 50.0
        self._ry = 50.0
        self._lock_aspect = True
        self._drag_handle: Optional[str] = None
        self._drag_box: Optional[QRectF] = None
        self._hover_handle: Optional[str] = None
        self._hovered = False
        self.setFixedHeight(104)
        self.setMinimumWidth(140)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMouseTracking(True)
        self.setToolTip(
            "拖动蓝色中心点调整位置（可移出文字框外），拖动边缘手柄调整横/纵半径；松开自动适配视野，双击恢复居中"
        )

    @property
    def lock_aspect(self) -> bool:
        return self._lock_aspect

    def set_lock_aspect(self, locked: bool) -> None:
        self._lock_aspect = bool(locked)

    def set_stops(self, stops: list[tuple[float, str]]) -> None:
        normalized = _normalize_gradient_stops(stops)
        if normalized != self._stops:
            self._stops = normalized
            self.update()

    def set_geometry_values(
        self, cx: float, cy: float, rx: float, ry: float
    ) -> None:
        cx = max(self._CENTER_MIN, min(self._CENTER_MAX, float(cx)))
        cy = max(self._CENTER_MIN, min(self._CENTER_MAX, float(cy)))
        rx = max(self._RADIUS_MIN, min(self._RADIUS_MAX, float(rx)))
        ry = max(self._RADIUS_MIN, min(self._RADIUS_MAX, float(ry)))
        if (
            abs(cx - self._cx) < 1e-4
            and abs(cy - self._cy) < 1e-4
            and abs(rx - self._rx) < 1e-4
            and abs(ry - self._ry) < 1e-4
        ):
            return
        self._cx, self._cy, self._rx, self._ry = cx, cy, rx, ry
        self.update()

    def reset_geometry(self) -> None:
        self._drag_handle = None
        self._drag_box = None
        self._cx, self._cy, self._rx, self._ry = 50.0, 50.0, 50.0, 50.0
        self.update()
        self.geometryChanged.emit(self._cx, self._cy, self._rx, self._ry)

    def _pad_rect(self) -> QRectF:
        return QRectF(1.0, 1.0, max(self.width() - 2.0, 1.0), max(self.height() - 2.0, 1.0))

    def _box_rect(self) -> QRectF:
        if self._drag_box is not None:
            return self._drag_box
        pad = self._pad_rect()
        inner = pad.adjusted(
            self._PAD_MARGIN_X,
            self._PAD_MARGIN_Y,
            -self._PAD_MARGIN_X,
            -self._PAD_MARGIN_Y,
        )
        if inner.width() <= 1.0 or inner.height() <= 1.0:
            return inner
        cx = self._cx / 100.0
        cy = self._cy / 100.0
        rx = self._rx / 100.0
        ry = self._ry / 100.0
        # Fit both the [0, 1] text box and the full ellipse + handles inside inner.
        min_x = min(0.0, cx - rx)
        max_x = max(1.0, cx + rx)
        min_y = min(0.0, cy - ry)
        max_y = max(1.0, cy + ry)
        span_x = max(max_x - min_x, 1.0)
        span_y = max(max_y - min_y, 1.0)
        box_w = inner.width() / span_x
        box_h = inner.height() / span_y
        box_left = inner.left() + (0.0 - min_x) * box_w
        box_top = inner.top() + (0.0 - min_y) * box_h
        return QRectF(box_left, box_top, box_w, box_h)

    def _center_point(self) -> QPointF:
        box = self._box_rect()
        return QPointF(
            box.left() + box.width() * (self._cx / 100.0),
            box.top() + box.height() * (self._cy / 100.0),
        )

    def _rx_handle_point(self) -> QPointF:
        box = self._box_rect()
        center = self._center_point()
        return QPointF(center.x() + box.width() * (self._rx / 100.0), center.y())

    def _ry_handle_point(self) -> QPointF:
        box = self._box_rect()
        center = self._center_point()
        return QPointF(center.x(), center.y() + box.height() * (self._ry / 100.0))

    def _hit_test(self, pos: QPointF) -> Optional[str]:
        def dist(a: QPointF, b: QPointF) -> float:
            return math.hypot(a.x() - b.x(), a.y() - b.y())

        if dist(pos, self._center_point()) <= 11.0:
            return "center"
        if dist(pos, self._rx_handle_point()) <= 10.0:
            return "rx"
        if dist(pos, self._ry_handle_point()) <= 10.0:
            return "ry"
        return None

    def _update_cursor(self, handle: Optional[str]) -> None:
        if self._drag_handle == "center":
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
        elif handle == "rx" or self._drag_handle == "rx":
            self.setCursor(Qt.CursorShape.SizeHorCursor)
        elif handle == "ry" or self._drag_handle == "ry":
            self.setCursor(Qt.CursorShape.SizeVerCursor)
        elif handle == "center":
            self.setCursor(Qt.CursorShape.OpenHandCursor)
        else:
            self.setCursor(Qt.CursorShape.CrossCursor)

    def paintEvent(self, event) -> None:  # noqa: N802, ARG002
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            pad = self._pad_rect()
            box = self._box_rect()
            center = self._center_point()
            rx_px = max(box.width() * (self._rx / 100.0), 1.0)
            ry_px = max(box.height() * (self._ry / 100.0), 1.0)

            clip = QPainterPath()
            clip.addRoundedRect(pad, 6.0, 6.0)
            painter.save()
            painter.setClipPath(clip)

            # Subtle checkerboard underneath translucent gradients.
            if any(display_color(c).alpha() < 255 for _p, c in self._stops):
                cell = 8
                c1, c2 = QColor("#E5E7EB"), QColor("#FFFFFF")
                cols = int(math.ceil(pad.width() / cell))
                rows = int(math.ceil(pad.height() / cell))
                for r in range(rows):
                    for col in range(cols):
                        painter.fillRect(
                            QRectF(pad.left() + col * cell, pad.top() + r * cell, cell, cell),
                            c1 if (r + col) % 2 == 0 else c2,
                        )

            gradient = QRadialGradient(QPointF(0.0, 0.0), 1.0)
            for position, color in self._stops:
                gradient.setColorAt(position / 100.0, display_color(color))
            brush = QBrush(gradient)
            transform = QTransform()
            transform.translate(center.x(), center.y())
            transform.scale(rx_px, ry_px)
            brush.setTransform(transform)
            painter.fillRect(pad, brush)

            # Dim area outside the 0%..100% text box lightly so the text region stays unmistakable.
            outside = QPainterPath()
            outside.addRect(pad)
            inner_text = QPainterPath()
            inner_text.addRect(box)
            painter.fillPath(outside.subtracted(inner_text), QColor(17, 24, 39, 34))

            # Inner text-box reference frame (0%..100%).
            frame_pen = QPen(QColor(0, 0, 0, 95), 1.0, Qt.PenStyle.DotLine)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(frame_pen)
            painter.drawRect(box)
            painter.setPen(QPen(QColor(255, 255, 255, 165), 1.0, Qt.PenStyle.DotLine))
            painter.drawRect(box.adjusted(0.5, 0.5, -0.5, -0.5))

            # Dashed ellipse for 100% stop boundary.
            ellipse_rect = QRectF(
                center.x() - rx_px, center.y() - ry_px, rx_px * 2.0, ry_px * 2.0
            )
            painter.setPen(QPen(QColor(0, 0, 0, 120), 2.2, Qt.PenStyle.DashLine))
            painter.drawEllipse(ellipse_rect)
            painter.setPen(QPen(QColor(255, 255, 255, 230), 1.2, Qt.PenStyle.DashLine))
            painter.drawEllipse(ellipse_rect)

            rx_pt = self._rx_handle_point()
            ry_pt = self._ry_handle_point()
            guide_pen = QPen(QColor(255, 255, 255, 175), 1.0)
            painter.setPen(guide_pen)
            painter.drawLine(center, rx_pt)
            painter.drawLine(center, ry_pt)

            for name, pt in (("rx", rx_pt), ("ry", ry_pt)):
                active = self._drag_handle == name or self._hover_handle == name
                r = self._HANDLE_RADIUS + (1.0 if active else 0.0)
                painter.setBrush(QColor("#FFFFFF"))
                painter.setPen(
                    QPen(QColor("#0B84FF" if active else "#1F2937"), 1.8)
                )
                painter.drawEllipse(pt, r, r)

            center_active = self._drag_handle == "center" or self._hover_handle == "center"
            cr = self._CENTER_RADIUS + (1.0 if center_active else 0.0)
            painter.setBrush(QColor("#0B84FF"))
            painter.setPen(QPen(QColor("#FFFFFF"), 2.0))
            painter.drawEllipse(center, cr, cr)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor(0, 0, 0, 110), 0.9))
            painter.drawEllipse(center, cr + 1.0, cr + 1.0)

            if self._hovered or self._drag_handle is not None:
                badge = f"{self._cx:.0f}%, {self._cy:.0f}% · {self._rx:.0f}%×{self._ry:.0f}%"
                font = painter.font()
                font.setPixelSize(10)
                painter.setFont(font)
                fm = painter.fontMetrics()
                tw = fm.horizontalAdvance(badge) + 10
                th = fm.height() + 4
                pill = QRectF(pad.left() + 5.0, pad.top() + 5.0, tw, th)
                painter.setBrush(QColor(17, 24, 39, 170))
                painter.setPen(Qt.PenStyle.NoPen)
                painter.drawRoundedRect(pill, 4.0, 4.0)
                painter.setPen(QColor("#FFFFFF"))
                painter.drawText(pill, Qt.AlignmentFlag.AlignCenter, badge)

            painter.restore()
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QColor(palette().card_border))
            painter.drawRoundedRect(pad, 6.0, 6.0)
        finally:
            painter.end()

    def _apply_drag(self, pos: QPointF, modifiers: Qt.KeyboardModifier) -> None:
        box = self._box_rect()
        if box.width() <= 0.0 or box.height() <= 0.0:
            return
        uniform = self._lock_aspect or bool(modifiers & Qt.KeyboardModifier.ShiftModifier)
        if self._drag_handle == "center":
            self._cx = round(
                max(
                    self._CENTER_MIN,
                    min(self._CENTER_MAX, (pos.x() - box.left()) / box.width() * 100.0),
                ),
                1,
            )
            self._cy = round(
                max(
                    self._CENTER_MIN,
                    min(self._CENTER_MAX, (pos.y() - box.top()) / box.height() * 100.0),
                ),
                1,
            )
        elif self._drag_handle == "rx":
            center = self._center_point()
            rx = round(
                max(
                    self._RADIUS_MIN,
                    min(self._RADIUS_MAX, abs(pos.x() - center.x()) / box.width() * 100.0),
                ),
                1,
            )
            self._rx = rx
            if uniform:
                self._ry = rx
        elif self._drag_handle == "ry":
            center = self._center_point()
            ry = round(
                max(
                    self._RADIUS_MIN,
                    min(self._RADIUS_MAX, abs(pos.y() - center.y()) / box.height() * 100.0),
                ),
                1,
            )
            self._ry = ry
            if uniform:
                self._rx = ry
        self.update()
        self.geometryChanged.emit(self._cx, self._cy, self._rx, self._ry)

    def enterEvent(self, event) -> None:  # noqa: N802
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hovered = False
        self._hover_handle = None
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            return
        pos = event.position()
        hit = self._hit_test(pos)
        self._drag_box = self._box_rect()
        self._drag_handle = hit or "center"
        self._update_cursor(self._drag_handle)
        self._apply_drag(pos, event.modifiers())

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        pos = event.position()
        if self._drag_handle is not None:
            self._apply_drag(pos, event.modifiers())
            return
        hit = self._hit_test(pos)
        if hit != self._hover_handle:
            self._hover_handle = hit
            self.update()
        self._update_cursor(hit)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if self._drag_handle is not None:
            self._drag_handle = None
            self._drag_box = None
            self._hover_handle = self._hit_test(event.position())
            self._update_cursor(self._hover_handle)
            self.update()

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.reset_geometry()


class RoleFillPagesBuilder:
    """Build fill editors while color mutations remain on the panel host."""

    _SPACE_ITEMS = (("整行", "line"), ("单字", "glyph"))

    def __init__(
        self,
        host: Any,
        *,
        gradient_editor_factory: Callable[..., Any] | None = None,
        color_button_factory: Callable[..., Any] | None = None,
        double_spin_factory: Callable[..., Any] | None = None,
        spin_factory: Callable[..., Any] | None = None,
        combo_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._host = host
        self._gradient_editor_factory = gradient_editor_factory
        self._color_button_factory = color_button_factory
        self._double_spin_factory = double_spin_factory
        self._spin_factory = spin_factory
        self._combo_factory = combo_factory or WheelFocusedComboBox

    def _make_space_combo(self, parent: QWidget) -> Any:
        combo = self._combo_factory(parent)
        compact_property_control(combo)
        for label, value in self._SPACE_ITEMS:
            combo.addItem(label, value)
        combo.setToolTip("整行：按同角色整行（含注音）包围盒映射；单字：每个字与每个注音独立映射")
        return combo

    @staticmethod
    def _make_quick_angle_button(label: str, tooltip: str, parent: QWidget) -> QToolButton:
        btn = QToolButton(parent)
        btn.setText(label)
        btn.setToolTip(tooltip)
        btn.setFixedSize(32, 30)
        btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        themed(
            btn,
            lambda: (
                "QToolButton {"
                f" background: {palette().input_bg};"
                f" color: {palette().text_primary};"
                f" border: 1px solid {palette().input_border};"
                " border-radius: 6px; padding: 0px; font-size: 12px; }"
                "QToolButton:hover {"
                f" background: {palette().secondary_button_hover_bg}; }}"
                "QToolButton:pressed {"
                f" background: {palette().secondary_button_pressed_bg}; }}"
            ),
        )
        return btn

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
        host._gradient_editor.gradientCopied.connect(host._show_gradient_copy_success)
        host._gradient_editor.infoPasted.connect(
            lambda: host._show_color_info_paste_success("渐变")
        )
        host._gradient_editor.clipboardPasteRejected.connect(
            lambda reason: host._show_color_info_paste_invalid("渐变", reason)
        )
        host._gradient_bar_field = host._gradient_editor

        host._gradient_actions_row = QWidget(page)
        gradient_actions_layout = QHBoxLayout(host._gradient_actions_row)
        gradient_actions_layout.setContentsMargins(0, 0, 0, 0)
        gradient_actions_layout.setSpacing(4)
        host._gradient_path_btn = FluentPushButton("勾勒渐变", host._gradient_actions_row)
        host._gradient_path_btn.setAccessibleName("勾勒渐变")
        host._gradient_path_btn.setToolTip("在色盘上绘制颜色路线，自动生成渐变关键点")
        host._gradient_path_btn.clicked.connect(host._edit_gradient_path)
        gradient_actions_layout.addWidget(host._gradient_path_btn)
        for name, icon, label, callback in (
            ("_gradient_copy_btn", FIF.COPY, "复制渐变信息", host._gradient_editor.copy_gradient_info),
            ("_gradient_paste_btn", FIF.PASTE, "粘贴渐变信息", host._gradient_editor.paste_clipboard_info),
        ):
            button = FluentTransparentToolButton(icon, host._gradient_actions_row)
            button.setToolTip(label)
            button.setAccessibleName(label)
            button.setFixedSize(26, 26)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(callback)
            setattr(host, name, button)
            gradient_actions_layout.addWidget(button)
        gradient_actions_layout.addStretch(1)

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

        # 极简渐变参数区：线性模式仅 1 行（角度 + 范围），径向模式仅 2 行（中心/半径 + 范围）
        host._gradient_footer = QWidget(page)
        footer_layout = QVBoxLayout(host._gradient_footer)
        footer_layout.setContentsMargins(0, 0, 0, 0)
        footer_layout.setSpacing(6)

        host._gradient_linear_options_row = QWidget(host._gradient_footer)
        linear_opts_layout = QHBoxLayout(host._gradient_linear_options_row)
        linear_opts_layout.setContentsMargins(0, 0, 0, 0)
        linear_opts_layout.setSpacing(8)
        host._gradient_angle_spin = self._double_spin_factory(
            0, 360, decimals=0, suffix="°"
        )
        host._gradient_angle_spin.setWrapping(True)
        host._gradient_angle_spin.setSingleStep(5.0)
        host._gradient_angle_spin.setMinimumWidth(96)
        host._gradient_angle_spin.valueChanged.connect(
            lambda value: (
                host._set_gradient_angle(value)
                if hasattr(host, "_set_gradient_angle")
                else host._update_current_fill(gradient_angle_deg=float(value))
            )
        )
        host._gradient_angle_h_btn = self._make_quick_angle_button(
            "横", "横向渐变 (0°)", host._gradient_linear_options_row
        )
        host._gradient_angle_h_btn.clicked.connect(
            lambda: host._update_current_fill(
                mode="gradient_horizontal", gradient_angle_deg=None
            )
        )
        host._gradient_angle_v_btn = self._make_quick_angle_button(
            "纵", "纵向渐变 (90°)", host._gradient_linear_options_row
        )
        host._gradient_angle_v_btn.clicked.connect(
            lambda: host._update_current_fill(
                mode="gradient_vertical", gradient_angle_deg=None
            )
        )
        angle_row = QWidget(host._gradient_linear_options_row)
        angle_row_layout = QHBoxLayout(angle_row)
        angle_row_layout.setContentsMargins(0, 0, 0, 0)
        angle_row_layout.setSpacing(4)
        angle_row_layout.addWidget(host._gradient_angle_spin, 1)
        angle_row_layout.addWidget(host._gradient_angle_h_btn, 0)
        angle_row_layout.addWidget(host._gradient_angle_v_btn, 0)
        host._gradient_angle_field = property_field("角度", angle_row)
        host._gradient_space_combo = self._make_space_combo(
            host._gradient_linear_options_row
        )
        host._gradient_space_combo.setFixedWidth(96)
        host._gradient_space_combo.currentIndexChanged.connect(
            lambda _index: host._update_current_fill(
                gradient_space=str(host._gradient_space_combo.currentData() or "line")
            )
        )
        host._gradient_space_field = property_field(
            "范围", host._gradient_space_combo
        )
        linear_opts_layout.addWidget(host._gradient_angle_field, 1)
        linear_opts_layout.addWidget(host._gradient_space_field, 0)
        footer_layout.addWidget(host._gradient_linear_options_row)

        host._gradient_radial_options_grid = QWidget(host._gradient_footer)
        radial_layout = QVBoxLayout(host._gradient_radial_options_grid)
        radial_layout.setContentsMargins(0, 0, 0, 0)
        radial_layout.setSpacing(6)
        host._radial_pad = RadialGradientPad(host._gradient_radial_options_grid)
        host._radial_pad.geometryChanged.connect(
            lambda cx, cy, rx, ry: host._update_current_fill(
                radial_center_x_pct=cx,
                radial_center_y_pct=cy,
                radial_radius_x_pct=rx,
                radial_radius_y_pct=ry,
            )
        )
        radial_layout.addWidget(host._radial_pad)

        radial_bottom_row = QWidget(host._gradient_radial_options_grid)
        radial_bottom_layout = QHBoxLayout(radial_bottom_row)
        radial_bottom_layout.setContentsMargins(0, 0, 0, 0)
        radial_bottom_layout.setSpacing(8)
        host._radial_space_combo = self._make_space_combo(radial_bottom_row)
        host._radial_space_combo.currentIndexChanged.connect(
            lambda _index: host._update_current_fill(
                gradient_space=str(host._radial_space_combo.currentData() or "line")
            )
        )
        host._radial_space_field = property_field("范围", host._radial_space_combo)
        host._radial_lock_aspect_check = CheckBox("等比", radial_bottom_row)
        host._radial_lock_aspect_check.setChecked(True)
        host._radial_lock_aspect_check.setToolTip(
            "勾选时拖动手柄保持横纵半径比例一致；取消可自由拉伸椭圆"
        )
        host._radial_lock_aspect_check.toggled.connect(
            host._radial_pad.set_lock_aspect
        )
        host._radial_reset_btn = self._make_quick_angle_button(
            "重置", "恢复中心居中 (50%, 50%) 与默认半径", radial_bottom_row
        )
        host._radial_reset_btn.setFixedWidth(44)
        host._radial_reset_btn.clicked.connect(host._radial_pad.reset_geometry)
        radial_bottom_layout.addWidget(host._radial_space_field, 1)
        radial_bottom_layout.addWidget(
            host._radial_lock_aspect_check, 0, Qt.AlignmentFlag.AlignBottom
        )
        radial_bottom_layout.addWidget(
            host._radial_reset_btn, 0, Qt.AlignmentFlag.AlignBottom
        )
        radial_layout.addWidget(radial_bottom_row)
        host._gradient_radial_options_grid.hide()
        footer_layout.addWidget(host._gradient_radial_options_grid)

        host._ruby_horizontal_gradient_with_main_check = CheckBox(
            "注音与主文字共享横向渐变",
            host._gradient_footer,
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
        footer_layout.addWidget(host._ruby_horizontal_gradient_with_main_check)
        host._gradient_editor_layout = layout
        host._arrange_stop_editor(
            layout,
            host._gradient_bar_field,
            host._gradient_color_field,
            host._gradient_position_field,
            vertical=False,
            footer=host._gradient_footer,
            actions=host._gradient_actions_row,
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
        host._split_editor.gradientCopied.connect(host._show_split_copy_success)
        host._split_editor.infoPasted.connect(
            lambda: host._show_color_info_paste_success("拼色")
        )
        host._split_editor.clipboardPasteRejected.connect(
            lambda reason: host._show_color_info_paste_invalid("拼色", reason)
        )
        host._split_bar_field = host._split_editor

        host._split_actions_row = QWidget(page)
        split_actions_layout = QHBoxLayout(host._split_actions_row)
        split_actions_layout.setContentsMargins(0, 0, 0, 0)
        split_actions_layout.setSpacing(4)
        for name, icon, label, callback in (
            ("_split_copy_btn", FIF.COPY, "复制拼色信息", host._split_editor.copy_gradient_info),
            ("_split_paste_btn", FIF.PASTE, "粘贴拼色信息", host._split_editor.paste_clipboard_info),
        ):
            button = FluentTransparentToolButton(icon, host._split_actions_row)
            button.setToolTip(label)
            button.setAccessibleName(label)
            button.setFixedSize(26, 26)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(callback)
            setattr(host, name, button)
            split_actions_layout.addWidget(button)
        split_actions_layout.addStretch(1)

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

        host._split_options_row = QWidget(page)
        split_opts_layout = QHBoxLayout(host._split_options_row)
        split_opts_layout.setContentsMargins(0, 0, 0, 0)
        split_opts_layout.setSpacing(8)
        host._split_angle_spin = self._double_spin_factory(
            0, 360, decimals=0, suffix="°"
        )
        host._split_angle_spin.setWrapping(True)
        host._split_angle_spin.setSingleStep(5.0)
        host._split_angle_spin.setMinimumWidth(96)
        host._split_angle_spin.setValue(90.0)
        host._split_angle_spin.valueChanged.connect(
            lambda value: (
                host._set_split_angle(value)
                if hasattr(host, "_set_split_angle")
                else host._update_current_fill(gradient_angle_deg=float(value))
            )
        )
        host._split_angle_h_btn = self._make_quick_angle_button(
            "横", "横向拼色 (0°)", host._split_options_row
        )
        host._split_angle_h_btn.clicked.connect(
            lambda: host._update_current_fill(gradient_angle_deg=0.0)
        )
        host._split_angle_v_btn = self._make_quick_angle_button(
            "纵", "纵向拼色 (90°)", host._split_options_row
        )
        host._split_angle_v_btn.clicked.connect(
            lambda: host._update_current_fill(gradient_angle_deg=None)
        )
        split_angle_row = QWidget(host._split_options_row)
        split_angle_layout = QHBoxLayout(split_angle_row)
        split_angle_layout.setContentsMargins(0, 0, 0, 0)
        split_angle_layout.setSpacing(4)
        split_angle_layout.addWidget(host._split_angle_spin, 1)
        split_angle_layout.addWidget(host._split_angle_h_btn, 0)
        split_angle_layout.addWidget(host._split_angle_v_btn, 0)
        host._split_angle_field = property_field("角度", split_angle_row)
        host._split_space_combo = self._make_space_combo(host._split_options_row)
        host._split_space_combo.setFixedWidth(96)
        host._split_space_combo.currentIndexChanged.connect(
            lambda _index: host._update_current_fill(
                gradient_space=str(host._split_space_combo.currentData() or "line")
            )
        )
        host._split_space_field = property_field("范围", host._split_space_combo)
        split_opts_layout.addWidget(host._split_angle_field, 1)
        split_opts_layout.addWidget(host._split_space_field, 0)

        host._split_editor_layout = layout
        host._arrange_stop_editor(
            layout,
            host._split_bar_field,
            host._split_color_field,
            host._split_position_field,
            vertical=True,
            footer=host._split_options_row,
            actions=host._split_actions_row,
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
