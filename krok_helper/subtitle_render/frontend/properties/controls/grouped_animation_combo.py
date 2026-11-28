"""Categorized entry/exit animation combo box with pinned category filter tabs.

Keeps the underlying ``ComboBox`` item list and index ordering completely
intact (preserving all existing model bindings, ``findData`` lookups, and
index-based tests) while organizing the popup menu into clear semantic groups
with one-click category filtering.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Sequence

from PyQt6 import sip
from PyQt6.QtCore import (
    QEvent,
    QObject,
    QPoint,
    QPointF,
    QRect,
    QRectF,
    QSize,
    QTimer,
    Qt,
    pyqtSignal as Signal,
)
from PyQt6.QtGui import QAction, QColor, QCursor, QFont, QPainter, QPen
from PyQt6.QtWidgets import QHBoxLayout, QSizePolicy, QToolButton, QWidget
from qfluentwidgets.components.widgets.combo_box import ComboBoxMenu
from qfluentwidgets.components.widgets.menu import MenuAnimationType

from krok_helper.subtitle_render.frontend.properties.controls.animation_preview import (
    AnimationPreviewPopover,
)
from krok_helper.subtitle_render.frontend.properties.controls.inputs import (
    WheelFocusedComboBox,
)
from krok_helper.subtitle_render.frontend.widgets.theme import palette


@dataclass(frozen=True)
class AnimationGroupSpec:
    """One semantic category shown in the animation dropdown."""

    key: str
    tab_label: str
    header_label: str
    values: tuple[str, ...]


ENTRY_ANIMATION_GROUPS: tuple[AnimationGroupSpec, ...] = (
    AnimationGroupSpec(
        key="basic",
        tab_label="基础",
        header_label="基础整体",
        values=("fade", "slide_in", "rise"),
    ),
    AnimationGroupSpec(
        key="char",
        tab_label="逐字",
        header_label="逐字动态",
        values=(
            "char_fade",
            "char_drip",
            "spin_flip",
            "utopia",
            "tracking_in",
            "wave_in",
            "stretch_in",
            "random_zoom",
        ),
    ),
    AnimationGroupSpec(
        key="fx",
        tab_label="光效",
        header_label="光效溶解",
        values=("glow_in", "noise", "assemble_in"),
    ),
    AnimationGroupSpec(
        key="particle",
        tab_label="粒子",
        header_label="装饰粒子",
        values=(
            "sparkle",
            "ripple",
            "note",
            "heart",
            "flame",
            "feather",
            "lightning",
            "petal",
            "snow",
            "snow_solid",
        ),
    ),
)

EXIT_ANIMATION_GROUPS: tuple[AnimationGroupSpec, ...] = (
    AnimationGroupSpec(
        key="basic",
        tab_label="基础",
        header_label="基础整体",
        values=("fade", "slide_out", "rise"),
    ),
    AnimationGroupSpec(
        key="char",
        tab_label="逐字",
        header_label="逐字动态",
        values=(
            "char_fade",
            "char_drip",
            "spin_flip",
            "utopia",
            "scatter_out",
            "converge_out",
            "stretch_out",
            "random_zoom",
        ),
    ),
    AnimationGroupSpec(
        key="fx",
        tab_label="光效",
        header_label="光效溶解",
        values=("glow_out", "noise", "dissolve_out"),
    ),
    AnimationGroupSpec(
        key="particle",
        tab_label="粒子",
        header_label="装饰粒子",
        values=(
            "sparkle",
            "ripple",
            "note",
            "heart",
            "flame",
            "feather",
            "lightning",
            "petal",
            "snow",
            "snow_solid",
        ),
    ),
)


class _CategoryTabButton(QToolButton):
    """Self-painted compact category pill following the live theme palette."""

    def __init__(self, key: str, label: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._key = key
        self._label = label
        self.setCheckable(True)
        self.setAutoExclusive(True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(25)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    @property
    def category_key(self) -> str:
        return self._key

    def enterEvent(self, event: Any) -> None:  # noqa: N802
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event: Any) -> None:  # noqa: N802
        self.update()
        super().leaveEvent(event)

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(44, 24)

    def paintEvent(self, event: Any) -> None:  # noqa: N802
        p = palette()
        dark = bool(getattr(p, "is_dark", False))
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        rect = QRectF(0.5, 0.5, self.width() - 1.0, self.height() - 1.0)
        if self.isChecked():
            bg = QColor(p.accent_primary)
            border = QColor(p.accent_primary)
            fg = QColor("#FFFFFF")
        elif self.underMouse():
            bg = QColor(p.secondary_button_hover_bg)
            border = QColor(p.secondary_button_hover_border)
            fg = QColor(p.text_primary)
        else:
            bg = QColor(p.progress_bg if not dark else p.secondary_button_bg)
            border = QColor(p.card_border)
            fg = QColor(p.text_secondary)

        painter.setPen(QPen(border, 1.0))
        painter.setBrush(bg)
        painter.drawRoundedRect(rect, 6.0, 6.0)

        font = QFont("Microsoft YaHei UI")
        font.setPointSizeF(8.2)
        if self.isChecked():
            font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(font)
        painter.setPen(fg)
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, self._label)
        painter.end()


class _CategoryTabStrip(QWidget):
    """Pinned top filter bar inside :class:`GroupedComboBoxMenu`."""

    categoryChanged = Signal(str)

    def __init__(
        self,
        groups: Sequence[AnimationGroupSpec],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._buttons: dict[str, _CategoryTabButton] = {}
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 5)
        layout.setSpacing(5)

        tabs = [("all", "全部")] + [(g.key, g.tab_label) for g in groups]
        for key, label in tabs:
            btn = _CategoryTabButton(key, label, self)
            btn.clicked.connect(lambda _checked=False, k=key: self.categoryChanged.emit(k))
            layout.addWidget(btn, 1)
            self._buttons[key] = btn

        self._buttons["all"].setChecked(True)
        self.setFixedHeight(31)

    def paintEvent(self, event: Any) -> None:  # noqa: N802
        # Crisp hairline separating the pinned category bar from the scrolling list.
        p = palette()
        painter = QPainter(self)
        painter.setPen(QPen(QColor(p.card_border), 1.0))
        y = float(self.height()) - 0.5
        painter.drawLine(QPointF(0.0, y), QPointF(float(self.width()), y))
        painter.end()

    def set_category(self, key: str) -> None:
        btn = self._buttons.get(key)
        if btn is not None:
            btn.setChecked(True)

    def buttons(self) -> dict[str, _CategoryTabButton]:
        return dict(self._buttons)


class _GroupHeaderWidget(QWidget):
    """Non-selectable visual section divider inside the animation popup list."""

    _HEIGHT = 24

    def __init__(self, title: str, count: int, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._title = f"{title} ({count})"
        self.setFixedHeight(self._HEIGHT)
        self.resize(240, self._HEIGHT)

    def paintEvent(self, event: Any) -> None:  # noqa: N802
        p = palette()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        font = QFont("Microsoft YaHei UI")
        font.setPointSizeF(7.8)
        font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(font)
        fm = painter.fontMetrics()

        left = 12.0
        cy = self.height() / 2.0 + 1.0
        # Small accent dot before the group name.
        dot = QColor(p.accent_primary)
        dot.setAlpha(185)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(dot)
        painter.drawEllipse(QPointF(left + 2.5, cy), 2.3, 2.3)

        text_x = left + 9.0
        painter.setPen(QColor(p.text_hint))
        painter.drawText(
            QRectF(text_x, 0.0, self.width() - text_x - 10.0, float(self.height())),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            self._title,
        )

        # Subtle horizontal divider line extending to the right edge.
        line_x = text_x + fm.horizontalAdvance(self._title) + 8.0
        if line_x < self.width() - 10.0:
            divider = QColor(p.card_border)
            painter.setPen(QPen(divider, 1.0))
            painter.drawLine(QPointF(line_x, cy), QPointF(self.width() - 10.0, cy))
        painter.end()


def _infer_animation_direction(groups: Sequence[AnimationGroupSpec]) -> str:
    """Infer ``'exit'`` vs ``'entry'`` from a group specification tuple."""
    values = {val for group in groups for val in group.values}
    if values & {"slide_out", "scatter_out", "converge_out", "stretch_out", "glow_out", "dissolve_out"}:
        return "exit"
    return "entry"


class GroupedComboBoxMenu(ComboBoxMenu):
    """Stable Fluent dropdown menu with pinned category tabs and group headers."""

    _TAB_TOP_INSET = 6
    _TAB_SIDE_INSET = 10
    _TAB_GAP = 6
    _MIN_MENU_WIDTH = 276
    _PREVIEW_SHOW_DELAY_MS = 110
    _PREVIEW_HIDE_DELAY_MS = 60
    _PREVIEW_CURSOR_POLL_MS = 50

    def __init__(
        self,
        groups: Sequence[AnimationGroupSpec],
        parent: Optional[QWidget] = None,
        *,
        direction: Optional[str] = None,
    ) -> None:
        super().__init__(parent=parent)
        self._groups = tuple(groups)
        self._direction = direction or _infer_animation_direction(self._groups)
        self._active_category = "all"
        # Maps list-widget row -> (category_key, is_header).
        self._row_meta: list[tuple[str, bool]] = []
        # Maps list-widget row -> (anim_key, display_label); empty for headers.
        self._row_anim: list[tuple[str, str]] = []
        # Maps combo item index -> QAction so current item can be highlighted.
        self._combo_actions: dict[int, QAction] = {}

        self._preview_popover: Optional[AnimationPreviewPopover] = None
        self._hovered_row: int = -1
        self._pending_preview_row: int = -1
        self._preview_show_timer = QTimer(self)
        self._preview_show_timer.setSingleShot(True)
        self._preview_show_timer.setInterval(self._PREVIEW_SHOW_DELAY_MS)
        self._preview_show_timer.timeout.connect(self._show_pending_preview)

        self._preview_hide_timer = QTimer(self)
        self._preview_hide_timer.setSingleShot(True)
        self._preview_hide_timer.setInterval(self._PREVIEW_HIDE_DELAY_MS)
        self._preview_hide_timer.timeout.connect(self.hide_preview)

        # QMenu popup grabs swallow child viewport Leave events on Windows;
        # poll QCursor.pos() only while a real hover preview is active/pending.
        self._preview_cursor_timer = QTimer(self)
        self._preview_cursor_timer.setInterval(self._PREVIEW_CURSOR_POLL_MS)
        self._preview_cursor_timer.timeout.connect(self._sync_preview_with_cursor)

        self._tab_strip = _CategoryTabStrip(self._groups, self.view)
        self._tab_strip.categoryChanged.connect(self.set_category)
        self._tab_strip.installEventFilter(self)
        self._tab_strip.raise_()
        self._tab_strip.show()

        self.setMouseTracking(True)
        self.view.setMouseTracking(True)
        self.view.viewport().setMouseTracking(True)
        self.view.installEventFilter(self)
        self.view.viewport().installEventFilter(self)
        self.view.itemEntered.connect(self._on_list_item_entered)
        self.view.verticalScrollBar().valueChanged.connect(
            lambda _v: self._reposition_active_preview()
        )
        self.aboutToHide.connect(self.hide_preview)

        top_margin = self._TAB_TOP_INSET + self._tab_strip.height() + self._TAB_GAP
        self.view.setViewportMargins(0, top_margin, 0, 6)
        self.setMaxVisibleItems(12)

    @property
    def direction(self) -> str:
        return self._direction

    @property
    def active_category(self) -> str:
        return self._active_category

    @property
    def tab_strip(self) -> _CategoryTabStrip:
        return self._tab_strip

    def preview_popover(self) -> Optional[AnimationPreviewPopover]:
        if self._preview_popover is not None and sip.isdeleted(self._preview_popover):
            self._preview_popover = None
        return self._preview_popover

    def _ensure_preview_popover(self) -> AnimationPreviewPopover:
        pop = self.preview_popover()
        if pop is None:
            pop = AnimationPreviewPopover(None)
            self._preview_popover = pop

            def _cleanup(_obj: Any = None, target: AnimationPreviewPopover = pop) -> None:
                if not sip.isdeleted(target):
                    target.hide()
                    target.close()
                    target.deleteLater()

            self.destroyed.connect(_cleanup)
        return pop

    def add_group_header(self, category_key: str, title: str, count: int) -> None:
        header = _GroupHeaderWidget(title, count, self)
        self.addWidget(header, selectable=False)
        self._row_meta.append((category_key, True))
        self._row_anim.append(("", ""))

    def add_grouped_action(
        self,
        category_key: str,
        combo_index: int,
        action: QAction,
        *,
        anim_key: str = "",
    ) -> None:
        self.addAction(action)
        self._row_meta.append((category_key, False))
        self._row_anim.append((anim_key, action.text()))
        self._combo_actions[combo_index] = action

    def row_animation_key(self, row: int) -> str:
        if 0 <= row < len(self._row_anim):
            return self._row_anim[row][0]
        return ""

    def _row_global_rect(self, row: int) -> QRect:
        item = self.view.item(row)
        if item is None or item.isHidden():
            return QRect()
        rect = self.view.visualItemRect(item)
        top_left = self.view.viewport().mapToGlobal(rect.topLeft())
        return QRect(top_left, rect.size())

    def _menu_global_rect(self) -> QRect:
        return QRect(self.mapToGlobal(QPoint(0, 0)), self.size())

    def _row_under_cursor(self) -> int:
        """Return the selectable animation row currently under ``QCursor.pos()``, or -1."""
        if not self.isVisible():
            return -1
        viewport = self.view.viewport()
        local_pos = viewport.mapFromGlobal(QCursor.pos())
        if not viewport.rect().contains(local_pos):
            return -1
        item = self.view.itemAt(local_pos)
        if item is None or item.isHidden():
            return -1
        row = self.view.row(item)
        return row if self.row_animation_key(row) else -1

    def preview_for_row(
        self,
        row: int,
        *,
        track_cursor: bool = False,
    ) -> Optional[AnimationPreviewPopover]:
        """Immediately show the hover animation preview for ``row`` (also used in tests)."""
        self._preview_show_timer.stop()
        self._preview_hide_timer.stop()
        if not (0 <= row < len(self._row_anim)):
            self.hide_preview()
            return None
        anim_key, label = self._row_anim[row]
        if not anim_key:
            self.hide_preview()
            return None
        item = self.view.item(row)
        if item is None or item.isHidden():
            self.hide_preview()
            return None
        self._hovered_row = row
        pop = self._ensure_preview_popover()
        pop.set_animation(self._direction, anim_key, display_label=label)
        pop.show_beside(self._menu_global_rect(), self._row_global_rect(row))
        if track_cursor and not self._preview_cursor_timer.isActive():
            self._preview_cursor_timer.start()
        return pop

    def hide_preview(self) -> None:
        self._preview_show_timer.stop()
        self._preview_hide_timer.stop()
        self._preview_cursor_timer.stop()
        self._hovered_row = -1
        self._pending_preview_row = -1
        pop = self.preview_popover()
        if pop is not None and pop.isVisible():
            pop.hide()

    def _clear_viewport_hover(self) -> None:
        viewport = self.view.viewport()
        if viewport is not None and not sip.isdeleted(viewport):
            from PyQt6.QtWidgets import QApplication
            QApplication.sendEvent(viewport, QEvent(QEvent.Type.Leave))
            viewport.update()

    def _schedule_preview_for_row(self, row: int) -> None:
        if not (0 <= row < len(self._row_anim)):
            self._schedule_hide_preview()
            return
        anim_key, _label = self._row_anim[row]
        item = self.view.item(row)
        if not anim_key or item is None or item.isHidden():
            self._schedule_hide_preview()
            return
        self._preview_hide_timer.stop()
        if not self._preview_cursor_timer.isActive():
            self._preview_cursor_timer.start()
        if row == self._hovered_row:
            pop = self.preview_popover()
            if pop is not None and pop.isVisible():
                return
        pop = self.preview_popover()
        if pop is not None and pop.isVisible():
            # Already open: switch immediately without flicker.
            self.preview_for_row(row, track_cursor=True)
            return
        self._pending_preview_row = row
        self._preview_show_timer.start()

    def _schedule_hide_preview(self) -> None:
        self._preview_show_timer.stop()
        self._pending_preview_row = -1
        pop = self.preview_popover()
        if pop is not None and pop.isVisible():
            self._preview_hide_timer.start()
        else:
            self._preview_cursor_timer.stop()

    def _show_pending_preview(self) -> None:
        row = self._pending_preview_row
        self._pending_preview_row = -1
        if row >= 0 and self.isVisible() and self._row_under_cursor() == row:
            self.preview_for_row(row, track_cursor=True)
        else:
            self.hide_preview()

    def _sync_preview_with_cursor(self) -> None:
        row = self._row_under_cursor()
        if row < 0:
            self.hide_preview()
            self._clear_viewport_hover()
            return
        pop = self.preview_popover()
        if pop is not None and pop.isVisible():
            if row != self._hovered_row:
                self.preview_for_row(row, track_cursor=True)
        elif self._pending_preview_row >= 0 and row != self._pending_preview_row:
            self._pending_preview_row = row

    def _reposition_active_preview(self) -> None:
        pop = self.preview_popover()
        if pop is None or not pop.isVisible() or self._hovered_row < 0:
            return
        row_rect = self._row_global_rect(self._hovered_row)
        if row_rect.isNull():
            self.hide_preview()
            return
        pop.move(pop._compute_position(self._menu_global_rect(), row_rect))

    def _on_list_item_entered(self, item: Any) -> None:
        if item is None:
            self._schedule_hide_preview()
            return
        row = self.view.row(item)
        self._schedule_preview_for_row(row)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802
        if watched is self.view.viewport():
            etype = event.type()
            if etype == QEvent.Type.MouseMove:
                item = self.view.itemAt(event.position().toPoint())
                if item is None:
                    self.hide_preview()
                else:
                    self._schedule_preview_for_row(self.view.row(item))
            elif etype in (QEvent.Type.Leave, QEvent.Type.HoverLeave):
                self.hide_preview()
        elif watched in (self.view, self._tab_strip):
            if event.type() in (QEvent.Type.Leave, QEvent.Type.HoverLeave):
                self.hide_preview()
            elif watched is self._tab_strip and event.type() in (
                QEvent.Type.Enter,
                QEvent.Type.HoverEnter,
            ):
                self.hide_preview()
        return super().eventFilter(watched, event)

    def mouseMoveEvent(self, event: Any) -> None:  # noqa: N802
        if self._row_under_cursor() < 0:
            self.hide_preview()
            self._clear_viewport_hover()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event: Any) -> None:  # noqa: N802
        self.hide_preview()
        self._clear_viewport_hover()
        super().leaveEvent(event)

    def hideEvent(self, event: Any) -> None:  # noqa: N802
        self.hide_preview()
        super().hideEvent(event)

    def closeEvent(self, event: Any) -> None:  # noqa: N802
        self.hide_preview()
        super().closeEvent(event)

    def action_for_combo_index(self, combo_index: int) -> Optional[QAction]:
        return self._combo_actions.get(combo_index)

    def visible_row_count(self) -> int:
        return sum(
            1
            for row in range(self.view.count())
            if not self.view.item(row).isHidden()
        )

    def visible_option_texts(self) -> list[str]:
        texts: list[str] = []
        for row, (_cat, is_header) in enumerate(self._row_meta):
            if is_header:
                continue
            item = self.view.item(row)
            if item is not None and not item.isHidden():
                texts.append(item.text())
        return texts

    def set_category(self, category_key: str) -> None:
        self.hide_preview()
        self._active_category = category_key
        self._tab_strip.set_category(category_key)
        show_all = category_key == "all"
        for row, (row_cat, is_header) in enumerate(self._row_meta):
            item = self.view.item(row)
            if item is None:
                continue
            if show_all:
                visible = True
            else:
                # Single-category view hides section headers and unrelated groups.
                visible = (not is_header) and (row_cat == category_key)
            item.setHidden(not visible)
        self.view.verticalScrollBar().setValue(0)
        self.view.viewport().update()

    def _layout_tab_strip(self) -> None:
        width = max(120, self.view.width() - self._TAB_SIDE_INSET * 2)
        self._tab_strip.setGeometry(
            self._TAB_SIDE_INSET,
            self._TAB_TOP_INSET,
            width,
            self._tab_strip.height(),
        )
        self._tab_strip.raise_()

    def resizeEvent(self, event: Any) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._layout_tab_strip()

    def exec(self, pos, ani=True, aniType=MenuAnimationType.DROP_DOWN):  # noqa: A003
        # Use MenuAnimationType.NONE to avoid qfluentwidgets 1.11.2 close-animation race.
        self.view.setMinimumWidth(max(self.view.minimumWidth(), self._MIN_MENU_WIDTH))
        self.view.adjustSize(pos, MenuAnimationType.NONE)
        self.adjustSize()
        self._layout_tab_strip()
        return super().exec(pos, False, MenuAnimationType.NONE)


class GroupedAnimationComboBox(WheelFocusedComboBox):
    """Animation combo box whose popup groups options by animation category.

    Underlying ``self.items`` preserve insertion order so ``count()``,
    ``itemData(i)``, ``findData(...)``, and ``setCurrentIndex(...)`` behave
    identically to :class:`WheelFocusedComboBox`.
    """

    def __init__(
        self,
        groups: Sequence[AnimationGroupSpec],
        parent: Optional[QWidget] = None,
        *,
        direction: Optional[str] = None,
    ) -> None:
        super().__init__(parent)
        self._animation_groups = tuple(groups)
        self._direction = direction or _infer_animation_direction(self._animation_groups)

    @property
    def animation_groups(self) -> tuple[AnimationGroupSpec, ...]:
        return self._animation_groups

    @property
    def direction(self) -> str:
        return self._direction

    def build_grouped_menu(self) -> GroupedComboBoxMenu:
        """Build the categorized popup menu (exposed for testing and reuse)."""
        menu = GroupedComboBoxMenu(
            self._animation_groups,
            self,
            direction=self._direction,
        )
        index_by_data: dict[str, int] = {}
        for i, item in enumerate(self.items):
            if item.userData is not None:
                index_by_data[str(item.userData)] = i

        placed: set[int] = set()

        # Pin "none" (无) directly at the top when present.
        none_idx = index_by_data.get("none")
        if none_idx is not None:
            item = self.items[none_idx]
            action = QAction(
                item.icon,
                item.text,
                menu,
                triggered=lambda _c=False, x=none_idx: self._onItemClicked(x),
            )
            action.setEnabled(item.isEnabled)
            menu.add_grouped_action("none", none_idx, action, anim_key="none")
            placed.add(none_idx)

        # Populate each configured category group.
        for group in self._animation_groups:
            member_indices = [
                index_by_data[val]
                for val in group.values
                if val in index_by_data and index_by_data[val] not in placed
            ]
            if not member_indices:
                continue
            menu.add_group_header(group.key, group.header_label, len(member_indices))
            for idx in member_indices:
                item = self.items[idx]
                action = QAction(
                    item.icon,
                    item.text,
                    menu,
                    triggered=lambda _c=False, x=idx: self._onItemClicked(x),
                )
                action.setEnabled(item.isEnabled)
                menu.add_grouped_action(
                    group.key,
                    idx,
                    action,
                    anim_key=str(item.userData or ""),
                )
                placed.add(idx)

        # Defensive fallback: include any uncategorized item so nothing is ever lost.
        remaining = [i for i in range(len(self.items)) if i not in placed]
        if remaining:
            menu.add_group_header("other", "其他", len(remaining))
            for idx in remaining:
                item = self.items[idx]
                action = QAction(
                    item.icon,
                    item.text,
                    menu,
                    triggered=lambda _c=False, x=idx: self._onItemClicked(x),
                )
                action.setEnabled(item.isEnabled)
                menu.add_grouped_action(
                    "other",
                    idx,
                    action,
                    anim_key=str(item.userData or ""),
                )

        min_w = max(self.width(), GroupedComboBoxMenu._MIN_MENU_WIDTH)
        menu.view.setMinimumWidth(min_w)
        menu.adjustSize()

        current = self.currentIndex()
        if current >= 0:
            default_action = menu.action_for_combo_index(current)
            if default_action is not None:
                menu.setDefaultAction(default_action)
                for row in range(menu.view.count()):
                    row_item = menu.view.item(row)
                    if row_item is not None and row_item.data(Qt.ItemDataRole.UserRole) is default_action:
                        menu.view.scrollToItem(row_item)
                        break

        return menu

    def _showComboMenu(self) -> None:  # noqa: N802 - qfluentwidgets override
        if not self.items:
            return

        menu = self.build_grouped_menu()
        menu.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        menu.closedSignal.connect(self._onDropMenuClosed)
        self.dropMenu = menu

        x = -menu.width() // 2 + menu.layout().contentsMargins().left() + self.width() // 2
        pd = self.mapToGlobal(QPoint(x, self.height()))
        hd = menu.view.heightForAnimation(pd, MenuAnimationType.DROP_DOWN)

        pu = self.mapToGlobal(QPoint(x, 0))
        hu = menu.view.heightForAnimation(pu, MenuAnimationType.PULL_UP)

        if hd >= hu:
            menu.exec(pd, aniType=MenuAnimationType.DROP_DOWN)
        else:
            # Anchor above the combo when space below is tighter.
            menu.view.adjustSize(pu, MenuAnimationType.NONE)
            menu.adjustSize()
            top_pos = QPoint(pu.x(), max(0, pu.y() - menu.height()))
            menu.exec(top_pos, aniType=MenuAnimationType.PULL_UP)


__all__ = [
    "AnimationGroupSpec",
    "ENTRY_ANIMATION_GROUPS",
    "EXIT_ANIMATION_GROUPS",
    "GroupedAnimationComboBox",
    "GroupedComboBoxMenu",
]
