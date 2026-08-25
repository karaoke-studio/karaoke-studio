"""Detached subtitle preview player window and sizing primitives."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PyQt6.QtCore import (
    QEvent,
    QObject,
    QPoint,
    QRect,
    QSize,
    QTimer,
    Qt,
    pyqtSignal as Signal,
)
from PyQt6.QtGui import QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QWidget,
)

from krok_helper.subtitle_render.frontend.preview.preview_view import (
    PreviewPanel,
    TransportBar,
)
from krok_helper.subtitle_render.frontend.widgets.theme import themed

def fit_size_to_aspect(box: QSize, aspect_ratio: float) -> QSize:
    """把 ``box`` 缩到给定宽高比的最大内接尺寸（用于跟随画布形状的下限/建议值）。"""
    ratio = max(float(aspect_ratio), 0.1)
    width = max(box.width(), 1)
    height = max(box.height(), 1)
    if width / height >= ratio:
        return QSize(max(int(round(height * ratio)), 1), height)
    return QSize(width, max(int(round(width / ratio)), 1))

class AspectRatioBox(QWidget):
    """Keep one child centered at a fixed aspect ratio."""

    def __init__(
        self,
        child: QWidget,
        *,
        aspect_ratio: float = 16 / 9,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._child = child
        self._aspect_ratio = max(float(aspect_ratio), 0.1)
        self._child.setParent(self)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(self.minimumSizeHint())

    def sizeHint(self) -> QSize:  # noqa: N802
        return fit_size_to_aspect(QSize(960, 540), self._aspect_ratio)

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return fit_size_to_aspect(QSize(426, 240), self._aspect_ratio)

    def set_aspect_ratio(self, width: int, height: int) -> None:
        """Update the child aspect ratio from an output size."""
        if width <= 0 or height <= 0:
            return
        ratio = max(float(width) / float(height), 0.1)
        if ratio == self._aspect_ratio:
            return
        self._aspect_ratio = ratio
        # 竖屏 / 4:3 画布的最小尺寸也要跟着换形，否则窗口被 16:9 的下限撑宽。
        self.setMinimumSize(self.minimumSizeHint())
        self._update_child_geometry()
        self.updateGeometry()

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        self._update_child_geometry()

    def _update_child_geometry(self) -> None:
        w = max(self.width(), 1)
        h = max(self.height(), 1)
        target_w = w
        target_h = int(round(target_w / self._aspect_ratio))
        if target_h > h:
            target_h = h
            target_w = int(round(target_h * self._aspect_ratio))
        x = (w - target_w) // 2
        y = (h - target_h) // 2
        self._child.setGeometry(QRect(x, y, max(target_w, 1), max(target_h, 1)))

class WindowEdgeGrip(QWidget):
    """无边框窗口的边缘/角落拖拽调整手柄。

    覆盖在窗口内容之上的透明细条。缩放为**手动实现**（按下记录起始几何，
    拖动按边计算新几何）：Windows 上 ``startSystemResize`` 对无边框窗口
    会返回成功但实际不进入缩放循环（缺 ``WS_THICKFRAME``），不可依赖。
    """

    _EDGE_CURSORS = {
        Qt.Edge.LeftEdge.value: Qt.CursorShape.SizeHorCursor,
        Qt.Edge.RightEdge.value: Qt.CursorShape.SizeHorCursor,
        Qt.Edge.TopEdge.value: Qt.CursorShape.SizeVerCursor,
        Qt.Edge.BottomEdge.value: Qt.CursorShape.SizeVerCursor,
        (Qt.Edge.TopEdge | Qt.Edge.LeftEdge).value: Qt.CursorShape.SizeFDiagCursor,
        (Qt.Edge.BottomEdge | Qt.Edge.RightEdge).value: Qt.CursorShape.SizeFDiagCursor,
        (Qt.Edge.TopEdge | Qt.Edge.RightEdge).value: Qt.CursorShape.SizeBDiagCursor,
        (Qt.Edge.BottomEdge | Qt.Edge.LeftEdge).value: Qt.CursorShape.SizeBDiagCursor,
    }

    def __init__(self, window: QWidget, edges) -> None:
        super().__init__(window)
        self._window = window
        self._edges = edges
        self._edge_bits = int(edges.value)
        self._drag_start: Optional[QPoint] = None
        self._start_geometry: Optional[QRect] = None
        cursor = self._EDGE_CURSORS.get(edges.value)
        if cursor is not None:
            self.setCursor(cursor)

    def mousePressEvent(self, event):  # noqa: N802
        is_expanded = getattr(self._window, "_is_expanded", self._window.isMaximized)
        if event.button() == Qt.MouseButton.LeftButton and not is_expanded():
            self._drag_start = event.globalPosition().toPoint()
            self._start_geometry = QRect(self._window.geometry())
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):  # noqa: N802
        if self._drag_start is None or self._start_geometry is None:
            super().mouseMoveEvent(event)
            return
        delta = event.globalPosition().toPoint() - self._drag_start
        geometry = QRect(self._start_geometry)
        hint = self._window.minimumSizeHint()
        min_w = max(self._window.minimumWidth(), hint.width(), 1)
        min_h = max(self._window.minimumHeight(), hint.height(), 1)
        if self._edge_bits & Qt.Edge.LeftEdge.value:
            geometry.setLeft(min(geometry.left() + delta.x(), geometry.right() - min_w + 1))
        if self._edge_bits & Qt.Edge.RightEdge.value:
            geometry.setRight(max(geometry.right() + delta.x(), geometry.left() + min_w - 1))
        if self._edge_bits & Qt.Edge.TopEdge.value:
            geometry.setTop(min(geometry.top() + delta.y(), geometry.bottom() - min_h + 1))
        if self._edge_bits & Qt.Edge.BottomEdge.value:
            geometry.setBottom(max(geometry.bottom() + delta.y(), geometry.top() + min_h - 1))
        self._window.setGeometry(geometry)
        event.accept()

    def mouseReleaseEvent(self, event):  # noqa: N802
        if self._drag_start is not None:
            self._drag_start = None
            self._start_geometry = None
            event.accept()
            return
        super().mouseReleaseEvent(event)

class PreviewPlayerWindow(QWidget):
    """独立预览窗口：只承载视频预览画面，形状跟随当前输出画布。"""

    userClosed = Signal()
    _TITLE_BAR_HEIGHT = 42
    _MIN_VIDEO_BOX = QSize(426, 240)
    _COLLAPSED_SIZE = QSize(220, 44)
    _COLLAPSED_CENTER_Y_RATIO = 0.70

    def __init__(self, owner: QWidget) -> None:
        super().__init__(
            owner,
            Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint,
        )
        self._owner = owner
        self._drag_origin: Optional[QPoint] = None
        self._suppress_control_show = False
        self._collapsed = False
        self._output_aspect = 16 / 9
        self._media_title = "字幕视频预览"
        self.setWindowTitle("字幕视频预览")
        self.setObjectName("SubtitlePreviewPlayerWindow")
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self._preview_panel = PreviewPanel(self)
        self._preview_frame = AspectRatioBox(self._preview_panel, parent=self)
        self._preview_frame.setMouseTracking(True)
        self._preview_panel.setMouseTracking(True)
        self._install_video_interaction_filters()

        self._top_controls = QWidget(self)
        self._top_controls.setObjectName("PreviewTopControls")
        self._top_controls.setMouseTracking(True)
        top_layout = QHBoxLayout(self._top_controls)
        top_layout.setContentsMargins(12, 0, 8, 0)
        top_layout.setSpacing(8)
        self._title_label = QLabel("字幕视频预览", self._top_controls)
        self._title_label.setObjectName("PreviewTitleLabel")
        top_layout.addWidget(self._title_label, 1)

        self._transport_bar = TransportBar(self)
        self._transport_bar.setObjectName("PreviewTransportBar")
        self._bottom_controls = self._transport_bar
        transport_layout = self._transport_bar.layout()
        transport_layout.removeWidget(self._transport_bar._preview_quality_label)
        transport_layout.removeWidget(self._transport_bar._preview_quality_combo)
        self._transport_bar._preview_quality_label.setParent(self._top_controls)
        self._transport_bar._preview_quality_combo.setParent(self._top_controls)
        self._transport_bar._preview_quality_label.setFixedWidth(48)
        self._transport_bar._preview_quality_combo.setFixedSize(120, 28)
        self._transport_bar._preview_quality_combo.setObjectName(
            "PreviewQualityCombo"
        )
        top_layout.addWidget(self._transport_bar._preview_quality_label)
        top_layout.addWidget(self._transport_bar._preview_quality_combo)

        self._minimize_button = QPushButton("－", self._top_controls)
        self._maximize_button = QPushButton("□", self._top_controls)
        self._close_button = QPushButton("×", self._top_controls)
        for button in (self._minimize_button, self._maximize_button, self._close_button):
            button.setFixedSize(28, 28)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            top_layout.addWidget(button)
        self._minimize_button.clicked.connect(self._collapse_window)
        self._maximize_button.clicked.connect(self._toggle_maximized)
        self._close_button.clicked.connect(self.close)

        self._init_playback_shortcuts()

        self._hide_controls_timer = QTimer(self)
        self._hide_controls_timer.setSingleShot(True)
        self._hide_controls_timer.setInterval(2600)
        self._hide_controls_timer.timeout.connect(self._on_controls_idle_timeout)
        self._apply_player_transport_style()

        self._apply_minimum_window_size()

        # 无边框窗口的八向拖拽调整手柄（边 + 角），叠在最上层。
        edge = Qt.Edge
        self._edge_grips = [
            WindowEdgeGrip(self, edge.LeftEdge),
            WindowEdgeGrip(self, edge.RightEdge),
            WindowEdgeGrip(self, edge.TopEdge),
            WindowEdgeGrip(self, edge.BottomEdge),
            WindowEdgeGrip(self, edge.TopEdge | edge.LeftEdge),
            WindowEdgeGrip(self, edge.TopEdge | edge.RightEdge),
            WindowEdgeGrip(self, edge.BottomEdge | edge.LeftEdge),
            WindowEdgeGrip(self, edge.BottomEdge | edge.RightEdge),
        ]

        themed(
            self,
            lambda: (
                """
                #SubtitlePreviewPlayerWindow {
                    background: #15171A;
                }
                #PreviewTopControls {
                    background: rgba(0, 0, 0, 178);
                }
                #PreviewTitleLabel {
                    color: #F8FAFC;
                    font-size: 9.5pt;
                    font-family: "Microsoft YaHei UI";
                }
                #PreviewTopControls QPushButton {
                    background: transparent;
                    color: #FFFFFF;
                    border: none;
                    border-radius: 4px;
                    font-size: 13pt;
                }
                #PreviewTopControls QPushButton:hover {
                    background: rgba(255, 255, 255, 48);
                }
                #PreviewTopControls QPushButton:pressed {
                    background: rgba(255, 255, 255, 72);
                }
                #PreviewTopControls QComboBox#PreviewQualityCombo {
                    background: transparent;
                    color: rgba(255, 255, 255, 210);
                    border: 1px solid rgba(255, 255, 255, 36);
                    border-radius: 4px;
                    padding: 0 20px 0 6px;
                    text-align: left;
                    font-size: 9pt;
                    font-family: "Microsoft YaHei UI";
                }
                #PreviewTopControls QComboBox#PreviewQualityCombo:hover {
                    background: rgba(255, 255, 255, 18);
                    border-color: rgba(255, 255, 255, 64);
                }
                #PreviewTopControls QComboBox#PreviewQualityCombo:on {
                    background: rgba(255, 255, 255, 28);
                    border-color: rgba(255, 255, 255, 80);
                }
                #PreviewTopControls QComboBox#PreviewQualityCombo::drop-down {
                    width: 22px;
                    border: none;
                    background: transparent;
                }
                #PreviewTopControls QComboBox#PreviewQualityCombo QAbstractItemView {
                    color: rgba(255, 255, 255, 225);
                    background: #202225;
                    border: 1px solid rgba(255, 255, 255, 42);
                    outline: none;
                    selection-color: #FFFFFF;
                    selection-background-color: #34373B;
                }
                #PreviewTransportBar {
                    background: rgba(0, 0, 0, 0);
                    border-top: none;
                }
                #PreviewTransportBar QLabel {
                    color: #F8FAFC;
                }
                """
            ),
        )
        self._preview_frame.lower()
        self.show_controls()
        self.apply_workspace_geometry()

    @property
    def preview_panel(self) -> PreviewPanel:
        return self._preview_panel

    @property
    def transport_bar(self) -> TransportBar:
        return self._transport_bar

    def _install_video_interaction_filters(self) -> None:
        targets = [self._preview_frame, self._preview_panel]
        canvas = self._preview_panel.canvas
        targets.append(canvas)
        viewport = getattr(canvas, "viewport", lambda: None)()
        if viewport is not None:
            targets.append(viewport)
        for target in targets:
            target.installEventFilter(self)
            target.setMouseTracking(True)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802
        if self._video_interaction_target(watched):
            if event.type() == QEvent.Type.MouseButtonRelease:
                if (
                    event.button() == Qt.MouseButton.LeftButton
                    and self._video_area_contains(watched, event.position().toPoint())
                ):
                    self._toggle_playback()
                    event.accept()
                    return True
            elif event.type() == QEvent.Type.MouseButtonDblClick:
                if (
                    event.button() == Qt.MouseButton.LeftButton
                    and self._video_area_contains(watched, event.position().toPoint())
                ):
                    self._toggle_maximized()
                    self.show_controls()
                    event.accept()
                    return True
        return super().eventFilter(watched, event)

    def _video_interaction_target(self, watched: QObject) -> bool:
        if watched in {self._preview_frame, self._preview_panel}:
            return True
        canvas = self._preview_panel.canvas
        if watched is canvas:
            return True
        viewport = getattr(canvas, "viewport", lambda: None)()
        return watched is viewport

    def _video_area_contains(self, watched: QObject, pos: QPoint) -> bool:
        if self._collapsed or not self._preview_panel.is_populated():
            return False
        widget = watched if isinstance(watched, QWidget) else None
        if widget is None:
            return False
        window_pos = widget.mapTo(self, pos)
        frame_rect = QRect(
            self._preview_frame.mapTo(self, QPoint(0, 0)),
            self._preview_frame.size(),
        )
        return frame_rect.contains(window_pos)

    def min_video_size(self) -> QSize:
        """当前画布形状下的最小画面尺寸（16:9 时仍是原来的 426×240）。"""
        return fit_size_to_aspect(self._MIN_VIDEO_BOX, self._output_aspect)

    def _apply_minimum_window_size(self) -> None:
        min_video = self.min_video_size()
        self.setMinimumSize(
            QSize(
                min_video.width(),
                min_video.height() + self._TITLE_BAR_HEIGHT,
            )
        )

    def set_output_size(self, width: int, height: int) -> None:
        """跟随输出画布换形：非 16:9 的视频不再被补成 16:9 的预览画面。"""
        if width <= 0 or height <= 0:
            return
        aspect = max(float(width) / float(height), 0.1)
        if aspect == self._output_aspect:
            return
        self._output_aspect = aspect
        self._preview_frame.set_aspect_ratio(width, height)
        if self._collapsed:
            return
        self._apply_minimum_window_size()
        if self.isVisible() and not self._is_expanded():
            self.apply_workspace_geometry()
        self._layout_edge_grips()

    def apply_workspace_geometry(self) -> None:
        if self._collapsed:
            self._apply_collapsed_geometry()
            return
        workspace_size = self._owner.size()
        min_video = self.min_video_size()
        width = max(min_video.width(), workspace_size.width() // 2)
        video_height = max(
            min_video.height(), int(round(width / self._output_aspect))
        )
        height = video_height + self._TITLE_BAR_HEIGHT
        max_height = max(
            min_video.height() + self._TITLE_BAR_HEIGHT,
            workspace_size.height() // 2 + self._TITLE_BAR_HEIGHT,
        )
        if height > max_height:
            height = max_height
            video_height = max(
                min_video.height(), height - self._TITLE_BAR_HEIGHT
            )
            width = max(
                min_video.width(), int(round(video_height * self._output_aspect))
            )
        top_left = self._owner.mapToGlobal(QPoint(0, 0))
        self.setGeometry(QRect(top_left, QSize(width, height)))

    def _apply_collapsed_geometry(self) -> None:
        size = self._COLLAPSED_SIZE
        owner_size = self._owner.size()
        owner_top_left = self._owner.mapToGlobal(QPoint(0, 0))
        left = owner_top_left.x() + (owner_size.width() - size.width()) // 2
        center_y = owner_top_left.y() + round(
            owner_size.height() * self._COLLAPSED_CENTER_Y_RATIO
        )
        top = center_y - size.height() // 2
        self.setGeometry(left, top, size.width(), size.height())

    def show_near_workspace(self) -> None:
        if self._collapsed:
            self._restore_from_collapsed()
            return
        if self._is_expanded():
            self._restore_windowed()
        self.apply_workspace_geometry()
        self.show()
        self.show_controls()

    def set_media_title(self, path: Optional[Path]) -> None:
        self._media_title = path.name if path is not None else "字幕视频预览"
        if not self._collapsed:
            self._title_label.setText(self._media_title)

    def _init_playback_shortcuts(self) -> None:
        self._space_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Space), self)
        self._backward_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Z), self)
        self._forward_shortcut = QShortcut(QKeySequence(Qt.Key.Key_X), self)
        for shortcut in (
            self._space_shortcut,
            self._backward_shortcut,
            self._forward_shortcut,
        ):
            shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        self._space_shortcut.activated.connect(self._toggle_playback)
        self._backward_shortcut.activated.connect(lambda: self._seek_relative(-5_000))
        self._forward_shortcut.activated.connect(lambda: self._seek_relative(5_000))

    def _toggle_playback(self) -> None:
        self._transport_bar.toggle_play()
        self.show_controls()

    def _seek_relative(self, delta_ms: int) -> None:
        self._transport_bar.seek_relative(delta_ms)
        self.show_controls()

    def show_controls(self) -> None:
        if self._collapsed:
            self._hide_controls_timer.stop()
            self._top_controls.show()
            self._bottom_controls.hide()
            self._top_controls.raise_()
            return
        if self._suppress_control_show:
            return
        self._top_controls.show()
        self._bottom_controls.show()
        self._top_controls.raise_()
        self._bottom_controls.raise_()
        # 控制栏覆盖窗口上下沿；若最后提升的是控制栏，四个角的透明 resize
        # grip 就收不到鼠标事件。始终让边角手柄位于控制栏之上。
        self._raise_edge_grips()
        self._hide_controls_timer.start()

    def _on_controls_idle_timeout(self) -> None:
        if self._collapsed:
            self._top_controls.show()
            return
        self.hide_controls(force=False)

    def hide_controls(self, *, force: bool = False) -> None:
        if self._collapsed:
            self._hide_controls_timer.stop()
            self._top_controls.show()
            self._bottom_controls.hide()
            return
        if self.underMouse() and not force:
            self._hide_controls_timer.start()
            return
        self._hide_controls_timer.stop()
        self._suppress_control_show = True
        try:
            self._top_controls.setVisible(True)
            self._top_controls.raise_()
            self._bottom_controls.setVisible(False)
        finally:
            self._suppress_control_show = False

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        top_height = self.height() if self._collapsed else self._TITLE_BAR_HEIGHT
        video_top = 0 if self._collapsed else top_height
        self._preview_frame.setGeometry(
            0,
            video_top,
            self.width(),
            max(0, self.height() - video_top),
        )
        self._top_controls.setGeometry(0, 0, self.width(), top_height)
        self._bottom_controls.setGeometry(0, max(0, self.height() - 58), self.width(), 58)
        self._top_controls.raise_()
        self._bottom_controls.raise_()
        self._layout_edge_grips()

    def _layout_edge_grips(self) -> None:
        w, h = self.width(), self.height()
        margin = 6  # 边条厚度
        corner = 14  # 角块边长
        edge = Qt.Edge
        rects = {
            edge.LeftEdge.value: QRect(0, corner, margin, max(h - corner * 2, 0)),
            edge.RightEdge.value: QRect(w - margin, corner, margin, max(h - corner * 2, 0)),
            edge.TopEdge.value: QRect(corner, 0, max(w - corner * 2, 0), margin),
            edge.BottomEdge.value: QRect(corner, h - margin, max(w - corner * 2, 0), margin),
            (edge.TopEdge | edge.LeftEdge).value: QRect(0, 0, corner, corner),
            (edge.TopEdge | edge.RightEdge).value: QRect(w - corner, 0, corner, corner),
            (edge.BottomEdge | edge.LeftEdge).value: QRect(0, h - corner, corner, corner),
            (edge.BottomEdge | edge.RightEdge).value: QRect(w - corner, h - corner, corner, corner),
        }
        maximized = self._is_expanded()
        for grip in self._edge_grips:
            grip.setGeometry(rects[grip._edges.value])
            grip.setVisible(not maximized and not self._collapsed)
        self._raise_edge_grips()

    def _raise_edge_grips(self) -> None:
        if not hasattr(self, "_edge_grips"):
            return
        for grip in self._edge_grips:
            grip.raise_()

    def focusInEvent(self, event):  # noqa: N802
        super().focusInEvent(event)
        self.show_controls()

    def focusOutEvent(self, event):  # noqa: N802
        super().focusOutEvent(event)
        self._hide_controls_timer.start(900)

    def enterEvent(self, event):  # noqa: N802
        super().enterEvent(event)
        self.show_controls()

    def mouseMoveEvent(self, event):  # noqa: N802
        super().mouseMoveEvent(event)
        if self._drag_origin is not None:
            self.move(event.globalPosition().toPoint() - self._drag_origin)
        self.show_controls()

    def mousePressEvent(self, event):  # noqa: N802
        if (
            event.button() == Qt.MouseButton.LeftButton
            and event.position().y() <= self._top_controls.height()
        ):
            # 兜底命中：个别情况下（覆盖层/事件路由异常）标题栏按钮收不到点击，
            # 在窗口层按坐标直接分发，保证 最小化/最大化/关闭 永远可用。
            if self._dispatch_titlebar_button(event.position().toPoint()):
                event.accept()
                return
            if self._is_expanded():
                # 最大化状态下不允许手动拖动（会把窗口拖成"假最大化"状态）。
                event.accept()
                return
            self._drag_origin = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):  # noqa: N802
        # 顶栏双击 = 最大化/还原（与原生窗口一致）；避开按钮区域。
        if (
            event.button() == Qt.MouseButton.LeftButton
            and event.position().y() <= self._top_controls.height()
            and not self._titlebar_button_at(event.position().toPoint())
        ):
            self._toggle_maximized()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def _titlebar_button_at(self, pos: QPoint) -> Optional[QPushButton]:
        if not self._top_controls.isVisible():
            return None
        for button in (self._minimize_button, self._maximize_button, self._close_button):
            rect = QRect(button.mapTo(self, QPoint(0, 0)), button.size())
            if rect.contains(pos):
                return button
        return None

    def _dispatch_titlebar_button(self, pos: QPoint) -> bool:
        button = self._titlebar_button_at(pos)
        if button is None:
            return False
        button.click()
        return True

    def mouseReleaseEvent(self, event):  # noqa: N802
        self._drag_origin = None
        super().mouseReleaseEvent(event)

    def closeEvent(self, event):  # noqa: N802
        self._hide_controls_timer.stop()
        self._transport_bar.stop()
        self.userClosed.emit()
        super().closeEvent(event)

    def _is_expanded(self) -> bool:
        expanded = Qt.WindowState.WindowMaximized | Qt.WindowState.WindowFullScreen
        return bool(self.windowState() & expanded) or self.isMaximized() or self.isFullScreen()

    def is_collapsed(self) -> bool:
        return self._collapsed

    def _collapse_window(self) -> None:
        if self._collapsed:
            return
        self._collapsed = True
        self._hide_controls_timer.stop()
        self.setWindowState(Qt.WindowState.WindowNoState)
        self.showNormal()
        self.setMinimumSize(self._COLLAPSED_SIZE)
        self._preview_frame.hide()
        self._bottom_controls.hide()
        self._transport_bar._preview_quality_label.hide()
        self._transport_bar._preview_quality_combo.hide()
        self._minimize_button.hide()
        self._maximize_button.setToolTip("恢复预览窗口")
        self._title_label.setText("预览窗口")
        self.setWindowTitle("预览窗口")
        self._apply_collapsed_geometry()
        self._top_controls.show()
        self._top_controls.raise_()
        self._layout_edge_grips()
        self.show()
        self.raise_()

    def _restore_from_collapsed(self) -> None:
        if not self._collapsed:
            return
        self._collapsed = False
        self._apply_minimum_window_size()
        self._transport_bar._preview_quality_label.show()
        self._transport_bar._preview_quality_combo.show()
        self._minimize_button.show()
        self._maximize_button.setToolTip("")
        self._title_label.setText(self._media_title)
        self.setWindowTitle("字幕视频预览")
        self._preview_frame.show()
        self.showNormal()
        self.apply_workspace_geometry()
        self._layout_edge_grips()
        self.show_controls()

    def _restore_windowed(self) -> None:
        if self._collapsed:
            self._restore_from_collapsed()
            return
        self.setWindowState(Qt.WindowState.WindowNoState)
        self.showNormal()
        self.apply_workspace_geometry()
        self._layout_edge_grips()
        self.show_controls()

    def _toggle_maximized(self) -> None:
        if self._collapsed:
            self._restore_from_collapsed()
            return
        if self._is_expanded():
            self._restore_windowed()
        else:
            self.showMaximized()

    def _apply_player_transport_style(self) -> None:
        self._transport_bar.setFixedHeight(58)
        self._transport_bar.setStyleSheet(
            """
            #PreviewTransportBar {
                background: rgba(0, 0, 0, 0);
                border-top: none;
            }
            """
        )
        self._transport_bar._play_btn.setFixedSize(36, 36)
        self._transport_bar._play_btn.setStyleSheet(
            """
            QToolButton {
                background: transparent;
                color: #FFFFFF;
                border: none;
                border-radius: 18px;
                font-size: 18pt;
                font-weight: 700;
            }
            QToolButton:hover {
                background: rgba(255, 255, 255, 42);
            }
            QToolButton:pressed {
                background: rgba(255, 255, 255, 66);
            }
            """
        )
        for label in (
            self._transport_bar._timecode,
            self._transport_bar._fps_label,
            self._transport_bar._volume_label,
        ):
            label.setStyleSheet(
                """
                QLabel {
                    color: rgba(255, 255, 255, 210);
                    background: transparent;
                    font-family: "Consolas", "Courier New", monospace;
                    font-size: 9.5pt;
                }
                """
            )
        self._transport_bar._preview_quality_label.setStyleSheet(
            """
            QLabel {
                color: rgba(255, 255, 255, 160);
                background: transparent;
                font-family: "Microsoft YaHei UI";
                font-size: 9pt;
            }
            """
        )

__all__ = [
    "AspectRatioBox",
    "PreviewPlayerWindow",
    "WindowEdgeGrip",
    "fit_size_to_aspect",
]
