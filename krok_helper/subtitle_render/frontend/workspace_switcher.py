"""主工作区切换控件（预览 / 导出）：药丸滑块 + 图标 + 滑移动画。

替换默认 ``SegmentedWidget`` 的自绘分段控件。设计目标：

* 总高固定 32px，与旧控件一致，不改变项目命令栏纵向布局；
* 选中项用品牌主色实心药丸 + 白字 + 图标，一眼可辨当前工作区，
  并与属性面板的二级分类导航（小号 ``SegmentedWidget``）拉开层级；
* 切换时药丸带滑移动画（``QVariantAnimation`` 对矩形插值）；
* 配色全部在 ``paintEvent`` 里实时读 :func:`palette`，跟随主题切换。

对外 API 与 ``SegmentedWidget`` 对齐：``addItem`` / ``setCurrentItem`` /
``currentRouteKey`` / ``currentItemChanged``，宿主可无感替换。
"""

from __future__ import annotations

from typing import Callable, Optional

from PyQt6.QtCore import (
    QAbstractAnimation,
    QEasingCurve,
    QRectF,
    QSize,
    Qt,
    QVariantAnimation,
    pyqtSignal as Signal,
)
from PyQt6.QtGui import QColor, QFont, QIcon, QPainter
from PyQt6.QtWidgets import QAbstractButton, QButtonGroup, QHBoxLayout, QWidget

from qfluentwidgets import FluentIconBase

from krok_helper.subtitle_render.frontend.theme import palette

_WIDGET_HEIGHT = 32
_TRACK_MARGIN = 3
_ITEM_SPACING = 2
_ITEM_MIN_WIDTH = 96
_ITEM_H_PADDING = 18
_ICON_SIZE = 15
_ICON_TEXT_GAP = 6
_ANIM_DURATION_MS = 180


class _WorkspaceSwitchItem(QAbstractButton):
    """单个切换项：透明底，自绘图标 + 文字（选中白字 / 未选中次级灰）。"""

    def __init__(
        self,
        text: str,
        icon: Optional[FluentIconBase],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._text = text
        self._icon = icon
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)

    def sizeHint(self) -> QSize:
        width = _ITEM_H_PADDING * 2 + self.fontMetrics().horizontalAdvance(self._text)
        if self._icon is not None:
            width += _ICON_SIZE + _ICON_TEXT_GAP
        return QSize(max(_ITEM_MIN_WIDTH, width), _WIDGET_HEIGHT - _TRACK_MARGIN * 2)

    def paintEvent(self, _event) -> None:
        p = palette()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        checked = self.isChecked()
        if checked:
            foreground = QColor("#FFFFFF")
        else:
            foreground = QColor(p.text_primary if self.underMouse() else p.text_secondary)
            if self.underMouse():
                hover = (
                    QColor(255, 255, 255, 160)
                    if not p.is_dark
                    else QColor(255, 255, 255, 24)
                )
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(hover)
                radius = self.height() / 2.0
                painter.drawRoundedRect(QRectF(self.rect()), radius, radius)

        metrics = self.fontMetrics()
        text_width = metrics.horizontalAdvance(self._text)
        content_width = text_width
        if self._icon is not None:
            content_width += _ICON_SIZE + _ICON_TEXT_GAP
        x = (self.width() - content_width) / 2.0

        if self._icon is not None:
            icon_rect = QRectF(
                x, (self.height() - _ICON_SIZE) / 2.0, _ICON_SIZE, _ICON_SIZE
            )
            self._icon.icon(color=foreground).paint(
                painter,
                icon_rect.toRect(),
                Qt.AlignmentFlag.AlignCenter,
                QIcon.Mode.Normal,
                QIcon.State.On,
            )
            x += _ICON_SIZE + _ICON_TEXT_GAP

        font = QFont(self.font())
        if checked:
            font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(font)
        painter.setPen(foreground)
        painter.drawText(
            QRectF(x, 0, text_width + 2, self.height()),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            self._text,
        )


class WorkspaceSwitcher(QWidget):
    """主工作区分段切换控件：浅灰轨道 + 主色药丸滑块（带滑移动画）。"""

    currentItemChanged = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(_WIDGET_HEIGHT)
        self.setAccessibleName("工作区切换")

        self._items: dict[str, _WorkspaceSwitchItem] = {}
        self._current_key: Optional[str] = None
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)

        self._thumb_from = QRectF()
        self._thumb_to = QRectF()
        self._thumb_progress = 1.0
        self._animation = QVariantAnimation(self)
        self._animation.setDuration(_ANIM_DURATION_MS)
        self._animation.setStartValue(0.0)
        self._animation.setEndValue(1.0)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._animation.valueChanged.connect(self._on_animation_value)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(
            _TRACK_MARGIN, _TRACK_MARGIN, _TRACK_MARGIN, _TRACK_MARGIN
        )
        layout.setSpacing(_ITEM_SPACING)

    # ------------------------------------------------------------------
    # SegmentedWidget 兼容 API
    # ------------------------------------------------------------------
    def addItem(
        self,
        routeKey: str,
        text: str,
        onClick: Optional[Callable[[bool], None]] = None,
        icon: Optional[FluentIconBase] = None,
    ) -> _WorkspaceSwitchItem:
        if routeKey in self._items:
            return self._items[routeKey]
        item = _WorkspaceSwitchItem(text, icon, self)
        if onClick is not None:
            item.clicked.connect(onClick)
        item.clicked.connect(lambda _checked=False, k=routeKey: self.setCurrentItem(k))
        self.layout().addWidget(item)
        self._group.addButton(item)
        self._items[routeKey] = item
        if self._current_key is None:
            self._apply_current(routeKey, animate=False)
        return item

    def setCurrentItem(self, routeKey: Optional[str]) -> None:
        if routeKey is None or routeKey not in self._items:
            return
        if routeKey == self._current_key:
            return
        self._apply_current(routeKey, animate=self.isVisible())

    def currentRouteKey(self) -> Optional[str]:
        return self._current_key

    # ------------------------------------------------------------------
    # 内部状态 / 动画
    # ------------------------------------------------------------------
    def _apply_current(self, routeKey: str, *, animate: bool) -> None:
        previous_key = self._current_key
        self._current_key = routeKey
        item = self._items[routeKey]
        item.setChecked(True)

        target = QRectF(item.geometry())
        if animate and previous_key is not None:
            self._thumb_from = self._current_thumb_rect()
            self._thumb_to = target
            self._animation.stop()
            self._animation.start()
        else:
            self._animation.stop()
            self._thumb_to = target
            self._thumb_progress = 1.0

        for child in self._items.values():
            child.update()
        self.update()
        self.currentItemChanged.emit(routeKey)

    def _current_thumb_rect(self) -> QRectF:
        if self._thumb_progress >= 1.0 or not self._thumb_from.isValid():
            return QRectF(self._thumb_to)
        t = self._thumb_progress
        a, b = self._thumb_from, self._thumb_to
        return QRectF(
            a.x() + (b.x() - a.x()) * t,
            a.y() + (b.y() - a.y()) * t,
            a.width() + (b.width() - a.width()) * t,
            a.height() + (b.height() - a.height()) * t,
        )

    def _on_animation_value(self, value) -> None:
        self._thumb_progress = float(value)
        self.update()

    def _snap_thumb_to_current(self) -> None:
        """布局尺寸变化后把药丸对齐到当前项（不做动画）。"""
        if self._animation.state() == QAbstractAnimation.State.Running:
            return
        if self._current_key is None:
            return
        target = QRectF(self._items[self._current_key].geometry())
        if target == self._thumb_to and self._thumb_progress >= 1.0:
            return
        self._thumb_to = target
        self._thumb_progress = 1.0
        self.update()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._snap_thumb_to_current()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._snap_thumb_to_current()

    def paintEvent(self, _event) -> None:
        self._snap_thumb_to_current()
        p = palette()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # 轨道底：浅灰药丸 + 1px 描边
        track = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        track_radius = self.height() / 2.0
        painter.setPen(QColor(p.card_border))
        painter.setBrush(QColor(p.progress_bg))
        painter.drawRoundedRect(track, track_radius, track_radius)

        # 选中 thumb：主色实心药丸 + 底部一层轻投影
        thumb = self._current_thumb_rect()
        if thumb.isValid() and thumb.width() > 0 and thumb.height() > 0:
            thumb_radius = thumb.height() / 2.0
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(0, 0, 0, 26))
            painter.drawRoundedRect(
                thumb.adjusted(0.0, 1.0, 0.0, 1.5), thumb_radius, thumb_radius
            )
            painter.setBrush(QColor(p.accent_primary))
            painter.drawRoundedRect(thumb, thumb_radius, thumb_radius)
