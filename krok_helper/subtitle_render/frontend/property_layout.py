"""Responsive layout primitives shared by subtitle property pages."""

from __future__ import annotations

from typing import Any, Optional

from PyQt6.QtCore import QSize, Qt
from PyQt6.QtWidgets import (
    QBoxLayout,
    QFrame,
    QGridLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
    QWIDGETSIZE_MAX,
)

from krok_helper.subtitle_render.frontend.theme import palette, themed


def property_field(label_text: str, control: QWidget) -> QWidget:
    """Wrap one property control with its standard vertical label."""
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


class ResponsivePropertyPair(QWidget):
    """Place two property cards side by side only while they genuinely fit."""

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

    def minimumSizeHint(self) -> QSize:  # noqa: N802
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

    def resizeEvent(self, event: Any) -> None:  # noqa: N802
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


class ResponsiveFieldGrid(QWidget):
    """Reflow property fields into 1–N columns for the available width."""

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
        self.add_widget(property_field(label_text, control))

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

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        base = super().minimumSizeHint()
        if not self._items:
            return base
        width = max(item.minimumSizeHint().width() for item in self._items)
        return QSize(width, base.height())

    def resizeEvent(self, event: Any) -> None:  # noqa: N802
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


class ResponsiveRoleHeader(QWidget):
    """Keep role navigation left-aligned and its preview right-aligned."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._navigation: Optional[QWidget] = None
        self._preview: Optional[QWidget] = None
        self._stacked: Optional[bool] = None
        self._layout = QBoxLayout(QBoxLayout.Direction.LeftToRight, self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(12)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

    def set_widgets(self, navigation: QWidget, preview: QWidget) -> None:
        self._navigation = navigation
        self._preview = preview
        self._layout.addWidget(
            navigation, 0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
        )
        self._layout.addStretch(1)
        self._spacer = self._layout.itemAt(1).spacerItem()
        self._layout.addWidget(
            preview, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop
        )
        self._sync_direction(force=True)

    def is_stacked(self) -> bool:
        return bool(self._stacked)

    def resizeEvent(self, event: Any) -> None:  # noqa: N802
        self._sync_direction()
        super().resizeEvent(event)

    def _sync_direction(self, *, force: bool = False) -> None:
        if self._navigation is None or self._preview is None:
            return
        required = (
            self._navigation.sizeHint().width()
            + self._preview.sizeHint().width()
            + self._layout.spacing()
        )
        stacked = self.width() < required
        if not force and stacked == self._stacked:
            return
        self._stacked = stacked
        self._layout.setDirection(
            QBoxLayout.Direction.TopToBottom
            if stacked
            else QBoxLayout.Direction.LeftToRight
        )
        if self._spacer is not None:
            self._spacer.changeSize(
                0,
                0,
                QSizePolicy.Policy.Minimum
                if stacked
                else QSizePolicy.Policy.Expanding,
                QSizePolicy.Policy.Fixed,
            )
        self._layout.setAlignment(
            self._navigation,
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
        )
        self._layout.setAlignment(
            self._preview,
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop,
        )
        self.updateGeometry()
