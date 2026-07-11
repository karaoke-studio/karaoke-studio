"""字幕视频渲染主窗口（Sayatoo 风格 + 底部 NavigationBar + 拖拽加载）。

照搬 SUG（lyrics_timing/.../frontend/main_window.py）的双模式骨架：

- ``SubtitleRenderWindow(embedded=False)`` — 默认 standalone
- ``SubtitleRenderWindow.for_embedding(parent, settings_provider, workflow_context)``
  — 嵌入工作台

UI 顶层结构（底部左下角 ``NavigationBar``，与工作流区域一致的 24px 左右 margin）：

  ┌──────────────────────────────────────────────────────┐
  │  项目命令栏                                           │
  │  ┌─────────┬──────────────┬──────────────┐          │
  │  │ 左·歌词 │ 中·预览       │ 右·属性 tab │          │
  │  │(拖.sug/.lrc) + transport│              │          │
  │  ├─────────┴──────────────┴──────────────┤          │
  │  │ 底·字幕轨道                            │          │
  │  └─────────────────────────────────────────┘          │
  │  [预览] [导出]                                         │
  └──────────────────────────────────────────────────────┘

三个素材区均接受拖拽 + 点击浏览（详见 :mod:`drop_panel`）。
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path
import subprocess
from typing import Any, Optional

from PyQt6.QtCore import QObject, QPoint, QRect, QSize, QThread, QTimer, Qt, pyqtSignal as Signal
from PyQt6.QtGui import QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    Action,
    CheckBox,
    ComboBox as FluentComboBox,
    DropDownPushButton,
    FluentIcon as FIF,
    InfoBar,
    InfoBarPosition,
    LineEdit as FluentLineEdit,
    PrimaryPushButton as FluentPrimaryPushButton,
    ProgressBar as FluentProgressBar,
    PushButton as FluentPushButton,
    RoundMenu,
    SegmentedWidget,
    SpinBox as FluentSpinBox,
)

from krok_helper.errors import ExportCancelled, ProcessingError
from krok_helper.ffmpeg import find_tool, probe_media, terminate_process
from krok_helper.models import MediaInfo
from krok_helper.qfluent_compat import apply_qfluent_menu_lifetime_patch
from krok_helper.settings import load_app_settings, save_app_settings
from krok_helper.subtitle_render.engine.encoder_select import (
    CPU_PRESETS,
    ENCODER_AMF,
    ENCODER_AUTO,
    ENCODER_CPU,
    ENCODER_NVENC,
    ENCODER_QSV,
)
from krok_helper.subtitle_render.engine.painter import (
    apply_layout_to_page,
    assign_layout_to_all,
    auto_assign_layouts_by_page,
    check_layout_margins,
    display_windows_for_style,
)
from krok_helper.subtitle_render.engine.renderer import RenderJob, render_subtitle_video
from krok_helper.subtitle_render.engine.timeline import track_duration_ms
from krok_helper.subtitle_render.frontend.drop_panel import DropPanel
from krok_helper.subtitle_render.frontend.lyrics_list import LyricsPanel
from krok_helper.subtitle_render.frontend.playback import (
    PlaybackController,
    unified_player_enabled,
)
from krok_helper.subtitle_render.frontend.preview_view import PreviewPanel, TransportBar
from krok_helper.subtitle_render.frontend.property_panel import (
    PropertyPanel,
    ScreenSettings,
    SCREEN_FPS_OPTIONS,
    match_screen_preset_key,
    screen_settings_from_dict,
    screen_settings_to_dict,
)
from krok_helper.subtitle_render.frontend.timeline_view import TrackTimelineView
from krok_helper.subtitle_render.models import (
    LineAnimationOverride,
    PROJECT_FILE_SUFFIX,
    SubtitleStyleScheme,
    Style,
    TimingTrack,
    line_animation_override_from_dict,
    line_animation_override_to_dict,
    rescale_layout_sizes,
    subtitle_style_scheme_from_dict,
    subtitle_style_scheme_to_dict,
    style_from_dict,
    style_to_dict,
)
from krok_helper.subtitle_render.n3proj_import import N3_PROJECT_FILTER, load_n3proj
from krok_helper.subtitle_render.project_store import (
    load_render_project,
    project_output_payload,
    project_payload,
    save_render_project,
    split_project_paths,
)
from krok_helper.subtitle_render.subtitle_sources import load_nicokara_lrc
from krok_helper.subtitle_render.sug_project import (
    load_sug_timing_track,
    timing_track_from_sug_project,
)
from krok_helper.subtitle_render.frontend.theme import palette, themed

apply_qfluent_menu_lifetime_patch()

SUBTITLE_FILTER = "SUG 项目 / Nicokara LRC (*.sug *.lrc);;SUG 项目 (*.sug);;Nicokara 逐字 LRC (*.lrc);;所有文件 (*.*)"

_UNDO_STACK_LIMIT = 200
"""撤销栈上限（字幕轨道显示/隐藏时间编辑）。"""
VIDEO_FILTER = "视频文件 (*.mp4 *.mkv *.mov *.webm *.avi *.flv);;所有文件 (*.*)"
OUTPUT_FILTER = "MP4 视频 (*.mp4);;所有文件 (*.*)"
PROJECT_FILTER = f"字幕渲染项目 (*{PROJECT_FILE_SUFFIX});;所有文件 (*.*)"


class _AspectRatioBox(QWidget):
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
        return QSize(960, 540)

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return QSize(426, 240)

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
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


@dataclass
class ExtraSubtitleSource:
    """一个副字幕源（对标 N3 ``SourceLyricsInfos`` 的コーラス槽位）。"""

    name: str
    path: Path
    track: TimingTrack


class _WindowEdgeGrip(QWidget):
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
    """独立预览窗口：只承载 16:9 的视频预览画面。"""

    def __init__(self, owner: QWidget) -> None:
        super().__init__(
            owner,
            Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint,
        )
        self._owner = owner
        self._drag_origin: Optional[QPoint] = None
        self._suppress_control_show = False
        self.setWindowTitle("字幕视频预览")
        self.setObjectName("SubtitlePreviewPlayerWindow")
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self._preview_panel = PreviewPanel(self)
        self._preview_frame = _AspectRatioBox(self._preview_panel, parent=self)
        self._preview_frame.setMouseTracking(True)
        self._preview_panel.setMouseTracking(True)

        self._top_controls = QWidget(self)
        self._top_controls.setObjectName("PreviewTopControls")
        self._top_controls.setMouseTracking(True)
        top_layout = QHBoxLayout(self._top_controls)
        top_layout.setContentsMargins(12, 0, 8, 0)
        top_layout.setSpacing(8)
        self._title_label = QLabel("字幕视频预览", self._top_controls)
        self._title_label.setObjectName("PreviewTitleLabel")
        top_layout.addWidget(self._title_label, 1)

        self._minimize_button = QPushButton("－", self._top_controls)
        self._maximize_button = QPushButton("□", self._top_controls)
        self._close_button = QPushButton("×", self._top_controls)
        for button in (self._minimize_button, self._maximize_button, self._close_button):
            button.setFixedSize(28, 28)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            top_layout.addWidget(button)
        self._minimize_button.clicked.connect(self.showMinimized)
        self._maximize_button.clicked.connect(self._toggle_maximized)
        self._close_button.clicked.connect(self.close)

        self._transport_bar = TransportBar(self)
        self._transport_bar.setObjectName("PreviewTransportBar")
        self._bottom_controls = self._transport_bar
        self._init_playback_shortcuts()

        self._hide_controls_timer = QTimer(self)
        self._hide_controls_timer.setSingleShot(True)
        self._hide_controls_timer.setInterval(2600)
        self._hide_controls_timer.timeout.connect(self._on_controls_idle_timeout)
        self._apply_player_transport_style()

        self.setMinimumSize(QSize(426, 240))

        # 无边框窗口的八向拖拽调整手柄（边 + 角），叠在最上层。
        edge = Qt.Edge
        self._edge_grips = [
            _WindowEdgeGrip(self, edge.LeftEdge),
            _WindowEdgeGrip(self, edge.RightEdge),
            _WindowEdgeGrip(self, edge.TopEdge),
            _WindowEdgeGrip(self, edge.BottomEdge),
            _WindowEdgeGrip(self, edge.TopEdge | edge.LeftEdge),
            _WindowEdgeGrip(self, edge.TopEdge | edge.RightEdge),
            _WindowEdgeGrip(self, edge.BottomEdge | edge.LeftEdge),
            _WindowEdgeGrip(self, edge.BottomEdge | edge.RightEdge),
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

    def apply_workspace_geometry(self) -> None:
        workspace_size = self._owner.size()
        width = max(426, workspace_size.width() // 2)
        height = max(240, int(round(width * 9 / 16)))
        max_height = max(240, workspace_size.height() // 2)
        if height > max_height:
            height = max_height
            width = max(426, int(round(height * 16 / 9)))
        top_left = self._owner.mapToGlobal(QPoint(0, 0))
        self.setGeometry(QRect(top_left, QSize(width, height)))

    def show_near_workspace(self) -> None:
        if self._is_expanded():
            self._restore_windowed()
        self.apply_workspace_geometry()
        self.show()
        self.show_controls()

    def set_media_title(self, path: Optional[Path]) -> None:
        self._title_label.setText(path.name if path is not None else "字幕视频预览")

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
        self.hide_controls(force=False)

    def hide_controls(self, *, force: bool = False) -> None:
        if self.underMouse() and not force:
            self._hide_controls_timer.start()
            return
        self._hide_controls_timer.stop()
        self._suppress_control_show = True
        try:
            self._top_controls.setVisible(False)
            self._bottom_controls.setVisible(False)
        finally:
            self._suppress_control_show = False

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        self._preview_frame.setGeometry(0, 0, self.width(), self.height())
        self._top_controls.setGeometry(0, 0, self.width(), 42)
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
            grip.setVisible(not maximized)
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
        super().closeEvent(event)

    def _is_expanded(self) -> bool:
        expanded = Qt.WindowState.WindowMaximized | Qt.WindowState.WindowFullScreen
        return bool(self.windowState() & expanded) or self.isMaximized() or self.isFullScreen()

    def _restore_windowed(self) -> None:
        self.setWindowState(Qt.WindowState.WindowNoState)
        self.showNormal()
        self.apply_workspace_geometry()
        self._layout_edge_grips()
        self.show_controls()

    def _toggle_maximized(self) -> None:
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
        for label in (self._transport_bar._timecode, self._transport_bar._fps_label):
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


class _RenderWorker(QObject):
    progressChanged = Signal(int, int)
    logMessage = Signal(str)
    finished = Signal(Path)
    cancelled = Signal(str)
    failed = Signal(str)

    def __init__(self, job: RenderJob, ffmpeg_dir: Optional[Path]) -> None:
        super().__init__()
        self._job = job
        self._ffmpeg_dir = ffmpeg_dir
        self._process: Optional[subprocess.Popen] = None
        self._cancel_requested = False

    def run(self) -> None:
        try:
            output = render_subtitle_video(
                self._job,
                ffmpeg_dir=self._ffmpeg_dir,
                logger=self.logMessage.emit,
                should_cancel=self.should_cancel,
                on_progress=self.progressChanged.emit,
                on_process_started=self._set_process,
            )
        except ExportCancelled as exc:
            self.cancelled.emit(str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))
            return
        self.finished.emit(output)

    def cancel(self) -> None:
        self._cancel_requested = True
        process = self._process
        if process is not None:
            terminate_process(process)

    def should_cancel(self) -> bool:
        return self._cancel_requested

    def _set_process(self, process: Optional[subprocess.Popen]) -> None:
        self._process = process


def _format_warning_lines(warnings: list) -> str:
    """把余白警告压成「第 1、3 行」式的短文案，最多点名 4 行。"""
    numbers = [str(w.line_index + 1) for w in warnings[:4]]
    text = f"第 {'、'.join(numbers)} 行"
    if len(warnings) > 4:
        text += f" 等 {len(warnings)} 行"
    return text


class SubtitleRenderWindow(QWidget):
    """字幕视频渲染模块主 widget。"""

    _embedded: bool = False

    def __init__(
        self,
        embedded: bool = False,
        settings_provider: Optional[Any] = None,
        workflow_context: Optional[Any] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._embedded = embedded
        self._settings_provider = settings_provider
        self._workflow_context = workflow_context

        self._timing_track: Optional[TimingTrack] = None
        # 字幕轨道编辑的撤销/重做栈：兼容显示窗口四元组与逐行动画批量命令。
        self._undo_stack: list[tuple] = []
        self._redo_stack: list[tuple] = []
        self._extra_sources: list[ExtraSubtitleSource] = []
        """副字幕源（N3 多歌词文件，如コーラス轨）：与主字幕同帧叠绘。"""
        self._active_source_index = 0
        """歌词列表当前显示的源：0 = 主字幕，k >= 1 = ``_extra_sources[k-1]``。"""
        self._subtitle_path: Optional[Path] = None
        self._video_path: Optional[Path] = None
        self._video_info: Optional[MediaInfo] = None
        self._audio_path: Optional[Path] = None
        self._audio_info: Optional[MediaInfo] = None
        self._style: Style = Style()
        self._style_presets: dict[str, SubtitleStyleScheme] = {}
        self._screen_settings: ScreenSettings = ScreenSettings()
        self._selected_scheme_key = "global"
        self._project_path: Optional[Path] = None
        self._project_dirty = False
        self._loading_project = False
        self._syncing_screen_controls = False
        self._render_thread: Optional[QThread] = None
        self._render_worker: Optional[_RenderWorker] = None
        # 左右余白检查：属性面板每个 SpinBox tick 都会触发样式变更，
        # 用单发定时器合并成一次检查，提示只在结果变化时弹出。
        self._margin_check_timer = QTimer(self)
        self._margin_check_timer.setSingleShot(True)
        self._margin_check_timer.setInterval(400)
        self._margin_check_timer.timeout.connect(self._check_layout_margins)
        self._last_margin_warning_key = ""
        self._load_persisted_state()

        themed(
            self,
            lambda: f"SubtitleRenderWindow {{ background: {palette().shell_bg}; }}",
        )

        self._init_layout()
        self._init_shortcuts()

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        if hasattr(self, "_preview_window"):
            self._preview_window.apply_workspace_geometry()

    def moveEvent(self, event):  # noqa: N802
        super().moveEvent(event)
        if hasattr(self, "_preview_window"):
            self._preview_window.apply_workspace_geometry()

    def closeEvent(self, event):  # noqa: N802
        if hasattr(self, "_preview_window"):
            self._preview_window.close()
        super().closeEvent(event)

    # ------------------------------------------------------------------ layout

    def _init_layout(self) -> None:
        # 主布局：内容区 + 底部导航条（水平按钮靠左下角）
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # QStackedWidget 承载各页内容
        self._stack = QStackedWidget(self)
        root.addWidget(self._stack, 1)

        self._preview_tab = self._make_preview_tab()
        self._export_tab = self._make_export_tab()
        self._stack.addWidget(self._preview_tab)
        self._stack.addWidget(self._export_tab)
        persisted = self._load_subtitle_settings()
        output = persisted.get("output") if isinstance(persisted.get("output"), dict) else {}
        self._apply_output_settings(output)
        self._set_export_screen_controls(self._screen_settings)
        self._sync_preview_output_size()
        self._export_width_spin.valueChanged.connect(self._sync_preview_output_size)
        self._export_height_spin.valueChanged.connect(self._sync_preview_output_size)
        self._export_width_spin.valueChanged.connect(self._on_export_screen_changed)
        self._export_height_spin.valueChanged.connect(self._on_export_screen_changed)
        self._export_fps_combo.currentIndexChanged.connect(self._on_export_screen_changed)

        # 底部导航：两个水平按钮，距左/下边各 24px
        bottom_bar = QWidget(self)
        bottom_layout = QHBoxLayout(bottom_bar)
        bottom_layout.setContentsMargins(24, 4, 24, 24)
        bottom_layout.setSpacing(8)

        self._bottom_navigation = SegmentedWidget(bottom_bar)
        self._nav_btns: dict[str, QWidget] = {}
        for key, text in [("preview", "预览"), ("export", "导出")]:
            btn = self._bottom_navigation.addItem(
                key,
                text,
                onClick=lambda _checked=False, k=key: self._switch_tab(k),
            )
            btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setFixedHeight(32)
            self._nav_btns[key] = btn
        bottom_layout.addWidget(self._bottom_navigation)
        bottom_layout.addStretch(1)
        root.addWidget(bottom_bar)

        self._bottom_navigation.setCurrentItem("preview")
        self._stack.setCurrentIndex(0)
        self._refresh_project_title()

    def _switch_tab(self, key: str) -> None:
        idx = 0 if key == "preview" else 1
        self._stack.setCurrentIndex(idx)
        self._bottom_navigation.setCurrentItem(key)

    # ----------------------------------------------------------- 项目文件（A11）

    def _make_project_bar(self) -> QWidget:
        bar = QWidget()
        self._project_bar = bar
        bar.setObjectName("SrProjectBar")
        themed(bar, lambda: "#SrProjectBar { background: transparent; }")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        # 「文件管理 ▾」单个下拉，菜单含 新建/打开/保存/另存为（仿 SUG，省横向空间）。
        self._file_menu_btn = DropDownPushButton(FIF.FOLDER, "文件管理")
        self._file_menu_btn.setFixedHeight(30)
        menu = RoundMenu(parent=self._file_menu_btn)
        menu.addAction(Action(FIF.ADD, "新建", triggered=self._new_project))
        menu.addAction(Action(FIF.FOLDER, "打开", triggered=self._open_project))
        menu.addAction(Action(FIF.SAVE, "保存", triggered=self._save_project))
        menu.addAction(Action(FIF.SAVE_AS, "另存为", triggered=self._save_project_as))
        menu.addSeparator()
        menu.addAction(Action(FIF.DOWNLOAD, "导入 N3 项目", triggered=self._import_n3_project))
        self._file_menu_btn.setMenu(menu)
        layout.addWidget(self._file_menu_btn)

        # 项目名：超长用 … 截断（完整名放 tooltip）。
        self._project_name_label = QLabel("")
        self._project_name_label.setMaximumWidth(260)
        themed(
            self._project_name_label,
            lambda: f"color: {palette().text_secondary}; font-size: 9.5pt;",
        )
        layout.addWidget(self._project_name_label)
        layout.addStretch(1)

        # 预览窗口是独立浮窗，被用户关掉后需要一个固定入口重新打开。
        self._show_preview_btn = FluentPushButton("预览窗口", bar)
        self._show_preview_btn.setFixedHeight(30)
        self._show_preview_btn.setToolTip("打开 / 唤起字幕预览窗口")
        self._show_preview_btn.clicked.connect(self._show_preview_window)
        layout.addWidget(self._show_preview_btn)
        return bar

    def _show_preview_window(self) -> None:
        if not hasattr(self, "_preview_window"):
            return
        self._preview_window.show_near_workspace()
        self._preview_window.raise_()
        self._preview_window.activateWindow()

    def _refresh_project_title(self) -> None:
        if not hasattr(self, "_project_name_label"):
            return
        name = self._project_path.name if self._project_path else "未命名项目"
        full = f"{'● ' if self._project_dirty else ''}{name}"
        metrics = self._project_name_label.fontMetrics()
        elided = metrics.elidedText(
            full, Qt.TextElideMode.ElideRight, self._project_name_label.maximumWidth()
        )
        self._project_name_label.setText(elided)
        self._project_name_label.setToolTip(full if elided != full else "")

    def _mark_project_dirty(self) -> None:
        if self._loading_project:
            return
        if not self._project_dirty:
            self._project_dirty = True
            self._refresh_project_title()

    def _current_project_data(self) -> dict:
        independent_audio = (
            self._audio_path
            if self._audio_path is not None and self._audio_path != self._video_path
            else None
        )
        line_layout_indices = (
            [int(getattr(line, "layout_index", 0) or 0) for line in self._timing_track.lines]
            if self._timing_track is not None
            else None
        )
        line_breaks_before = self._line_break_rows(self._timing_track)
        char_role_labels = self._collect_char_role_labels()
        line_display_overrides = self._display_override_rows(self._timing_track)
        line_animation_overrides = self._animation_override_rows(self._timing_track)
        extra_subtitle_sources = [
            {
                "name": source.name,
                "path": str(source.path),
                "line_layout_indices": [
                    int(getattr(line, "layout_index", 0) or 0) for line in source.track.lines
                ],
                "line_breaks_before": self._line_break_rows(source.track),
                "char_role_labels": self._char_role_rows(source.track),
                "line_display_overrides": self._display_override_rows(source.track),
                "line_animation_overrides": self._animation_override_rows(source.track),
            }
            for source in self._extra_sources
        ] or None
        return project_payload(
            subtitle_path=self._subtitle_path,
            video_path=self._video_path,
            audio_path=independent_audio,
            style=style_to_dict(self._style),
            screen=screen_settings_to_dict(self._screen_settings),
            selected_scheme_key=self._selected_scheme_key,
            line_layout_indices=line_layout_indices,
            line_breaks_before=line_breaks_before,
            char_role_labels=char_role_labels,
            line_display_overrides=line_display_overrides,
            line_animation_overrides=line_animation_overrides,
            extra_subtitle_sources=extra_subtitle_sources,
            output=project_output_payload(
                encoder_mode=str(self._export_encoder_combo.currentData() or ENCODER_CPU),
                crf=self._export_crf_spin.value(),
                preset=str(self._export_preset_combo.currentData() or "veryfast"),
                output_path=self._export_output_edit.text().strip(),
                native_export_enabled=False,
            ),
        )

    def _apply_project_data(self, data: dict) -> None:
        self._loading_project = True
        try:
            self._apply_project_data_inner(data)
        finally:
            self._loading_project = False

    def _apply_project_data_inner(self, data: dict) -> None:
        # 1) 样式 / 屏幕 / 配色方案
        self._style = style_from_dict(data.get("style"))
        self._screen_settings = screen_settings_from_dict(data.get("screen"))
        key = data.get("selected_scheme_key")
        if isinstance(key, str) and key:
            self._selected_scheme_key = key
        self._property_panel.set_style(self._style)
        self._property_panel.set_current_scheme_key(self._selected_scheme_key)
        self._selected_scheme_key = self._property_panel.current_scheme_key()
        self._preview_panel.set_style(self._style)
        self._lyrics_panel.set_style(self._style)
        self._set_export_screen_controls(self._screen_settings)
        self._sync_preview_output_size()
        # 2) 导出参数
        output = data.get("output") if isinstance(data.get("output"), dict) else {}
        self._apply_output_settings(output)
        # 3) 素材（存在才加载；缺失静默跳过，不阻塞打开）
        paths = split_project_paths(data)
        if paths["subtitle_path"] is not None and paths["subtitle_path"].is_file():
            self.load_subtitle_source(paths["subtitle_path"])
            self._apply_line_breaks_before(data.get("line_breaks_before"))
            self._apply_line_layout_indices(data.get("line_layout_indices"))
            self._apply_char_role_labels(data.get("char_role_labels"))
            if self._timing_track is not None:
                self._apply_display_override_rows(
                    self._timing_track, data.get("line_display_overrides")
                )
                self._apply_animation_override_rows(
                    self._timing_track, data.get("line_animation_overrides")
                )
            self._apply_extra_subtitle_sources(data.get("extra_subtitle_sources"))
            self._refresh_tracks_view_windows()
        if paths["video_path"] is not None and paths["video_path"].is_file():
            self.load_video(paths["video_path"])
        audio = paths["audio_path"]
        if audio is not None and audio.is_file() and audio != self._video_path:
            self.load_audio(audio)

    def _apply_output_settings(self, output: dict) -> None:
        encoder = output.get("encoder_mode")
        if encoder is not None:
            idx = self._export_encoder_combo.findData(encoder)
            if idx >= 0:
                self._export_encoder_combo.setCurrentIndex(idx)
        preset = output.get("preset")
        if isinstance(preset, str):
            p_idx = self._export_preset_combo.findData(preset)
            if p_idx >= 0:
                self._export_preset_combo.setCurrentIndex(p_idx)
        crf = output.get("crf")
        if isinstance(crf, int):
            self._export_crf_spin.setValue(crf)
        out_path = output.get("output_path")
        if isinstance(out_path, str) and out_path.strip():
            self._export_output_edit.setText(out_path.strip())
        blocked = self._export_native_check.blockSignals(True)
        try:
            self._export_native_check.setChecked(False)
        finally:
            self._export_native_check.blockSignals(blocked)

    def _confirm_discard_changes(self) -> bool:
        """有未保存改动时弹确认；返回 True 表示可以继续（已处理）。"""
        if not self._project_dirty:
            return True
        choice = QMessageBox.question(
            self,
            "未保存的改动",
            "当前项目有未保存的改动，是否先保存？",
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
        )
        if choice == QMessageBox.StandardButton.Cancel:
            return False
        if choice == QMessageBox.StandardButton.Save:
            return self._save_project()
        return True

    def _new_project(self) -> None:
        if not self._confirm_discard_changes():
            return
        self._clear_loaded_media()
        self._apply_project_data(
            {
                "style": style_to_dict(Style()),
                "screen": screen_settings_to_dict(ScreenSettings()),
                "selected_scheme_key": "global",
            }
        )
        self._project_path = None
        self._project_dirty = False
        self._refresh_project_title()

    def _clear_loaded_media(self) -> None:
        """清空已加载的字幕 / 视频 / 音频，把各面板复位到空态（新建项目用）。"""
        self._loading_project = True
        try:
            self._timing_track = None
            self._extra_sources = []
            self._active_source_index = 0
            self._clear_undo_history()
            self._subtitle_path = None
            self._video_path = None
            self._video_info = None
            self._audio_path = None
            self._audio_info = None
            # 歌词列表回空态
            self._lyrics_panel.set_track(None)
            self._lyrics_panel.set_role_options([])
            self._lyrics_panel.set_sources([], 0)
            self._preview_panel.set_extra_tracks([])
            # 预览回空态：清字幕 + 视频 + 取消 populated
            self._preview_panel.set_track(None)
            self._preview_panel.set_video_source(None)
            self._preview_panel.set_populated(False)
            self._video_settings_panel.set_populated(False)
            self._property_panel.set_roles([])
            # 播放条 + 字幕轨道复位
            self._transport_bar.set_audio_source(None)
            self._transport_bar.set_time(0)
            self._transport_bar.set_duration(0)
            self._tracks_view.set_tracks([])
            self._tracks_view.set_duration(0)
            self._tracks_view.set_time(0)
        finally:
            self._loading_project = False

    def _open_project(self) -> None:
        if not self._confirm_discard_changes():
            return
        start_dir = str(self._project_path.parent) if self._project_path else ""
        path_str, _ = QFileDialog.getOpenFileName(
            self, "打开字幕渲染项目", start_dir, PROJECT_FILTER
        )
        if not path_str:
            return
        try:
            data = load_render_project(Path(path_str))
        except (OSError, ValueError) as exc:
            QMessageBox.critical(self, "打开项目失败", f"无法读取项目文件：\n{path_str}\n\n{exc}")
            return
        self._apply_project_data(data)
        self._project_path = Path(path_str)
        self._project_dirty = False
        self._refresh_project_title()

    def _import_n3_project(self) -> None:
        """导入 NicoKaraMaker3 项目（.n3proj）：素材 / 字体配色 / 布局 / 标题 / 输出。"""
        if not self._confirm_discard_changes():
            return
        start_dir = str(self._project_path.parent) if self._project_path else ""
        path_str, _ = QFileDialog.getOpenFileName(
            self, "导入 NicoKaraMaker3 项目", start_dir, N3_PROJECT_FILTER
        )
        if not path_str:
            return
        try:
            result = load_n3proj(Path(path_str))
        except (OSError, ValueError) as exc:
            QMessageBox.critical(
                self, "导入失败", f"无法读取 NicoKaraMaker3 项目文件：\n{path_str}\n\n{exc}"
            )
            return
        self._clear_loaded_media()
        self._apply_project_data(result.project_data)
        # 导入的是外来工程：保存时必须另存为 .yurika，因此视为未命名 + 有改动。
        self._project_path = None
        self._project_dirty = True
        self._refresh_project_title()
        if result.warnings:
            QMessageBox.information(
                self,
                "导入完成（部分设置需注意）",
                "已导入 N3 项目，以下内容请检查：\n\n"
                + "\n".join(f"• {warning}" for warning in result.warnings),
            )
        else:
            InfoBar.success(
                title="N3 项目导入完成",
                content=Path(path_str).name,
                parent=self,
                position=InfoBarPosition.BOTTOM_RIGHT,
                duration=2500,
            )

    def _save_project(self) -> bool:
        if self._project_path is None:
            return self._save_project_as()
        return self._write_project(self._project_path)

    def _save_project_as(self) -> bool:
        start = str(self._project_path) if self._project_path else (
            str((self._subtitle_path or self._video_path or Path.cwd()).with_suffix(""))
            + PROJECT_FILE_SUFFIX
        )
        path_str, _ = QFileDialog.getSaveFileName(
            self, "保存字幕渲染项目", start, PROJECT_FILTER
        )
        if not path_str:
            return False
        if not path_str.endswith(PROJECT_FILE_SUFFIX):
            path_str += PROJECT_FILE_SUFFIX
        return self._write_project(Path(path_str))

    def _write_project(self, path: Path) -> bool:
        try:
            save_render_project(path, self._current_project_data())
        except OSError as exc:
            QMessageBox.critical(self, "保存项目失败", f"无法写入项目文件：\n{path}\n\n{exc}")
            return False
        self._project_path = path
        self._project_dirty = False
        self._refresh_project_title()
        InfoBar.success(
            title="项目已保存",
            content=str(path),
            parent=self,
            position=InfoBarPosition.BOTTOM_RIGHT,
            duration=2500,
        )
        return True

    def _make_preview_tab(self) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(24, 4, 24, 4)
        outer.setSpacing(4)

        # 顶部项目命令栏（新建 / 打开 / 保存 / 另存为 + 当前项目名）
        outer.addWidget(self._make_project_bar())

        body = QSplitter(Qt.Orientation.Vertical)
        body.setChildrenCollapsible(False)
        self._preview_body_splitter = body

        # 上半部：左·歌词 / 右·背景视频拖入，视频加载后右侧切换为属性设置。
        top = QSplitter(Qt.Orientation.Horizontal)
        top.setChildrenCollapsible(False)
        self._preview_splitter = top

        self._preview_window = PreviewPlayerWindow(self)
        self._preview_panel = self._preview_window.preview_panel
        self._preview_panel.set_style(self._style)
        self._preview_panel.pathDropped.connect(self.load_video)
        self._preview_panel.browseRequested.connect(self._browse_video)
        self._transport_bar = self._preview_window.transport_bar

        self._lyrics_panel = LyricsPanel()
        self._lyrics_panel.set_style(self._style)
        self._lyrics_panel.pathDropped.connect(self.load_subtitle_source)
        self._lyrics_panel.browseRequested.connect(self._browse_subtitle)
        self._lyrics_panel.roleChanged.connect(self._on_lyrics_role_changed)
        self._lyrics_panel.animationOverrideRequested.connect(
            self._on_line_animation_override_requested
        )
        self._lyrics_panel.rowClicked.connect(self._on_lyrics_row_clicked)
        self._lyrics_panel.layoutChangeRequested.connect(self._on_layout_change_requested)
        self._lyrics_panel.sourceSelected.connect(self._on_source_selected)
        self._lyrics_panel.sourceAddRequested.connect(self._on_source_add_requested)
        self._lyrics_panel.sourceRemoveRequested.connect(self._on_source_remove_requested)
        top.addWidget(self._lyrics_panel)

        self._transport_bar.set_preview_fps(self._screen_settings.fps)
        self._transport_bar.timeChanged.connect(self._preview_panel.set_time)
        self._transport_bar.playbackStateChanged.connect(self._preview_panel.set_playing)
        self._preview_panel.canvas.framePainted.connect(self._transport_bar.note_preview_frame_painted)
        # 单播放器统一（步骤2，§10.9，flag KROK_SUBTITLE_UNIFIED_PLAYER 默认关）：
        # 视频自带音频时同一文件本不该被音频/视频两个 QMediaPlayer 各自解码。开启后用一个
        # 共享 PlaybackController 同时驱动音视频（A/V 天然锁帧），预览不再自建视频 player。
        # raster 回退画布暂不支持 → use_external_player 返回 False，自动回退旧三播放器路径。
        self._playback: Optional[PlaybackController] = None
        if unified_player_enabled():
            controller = PlaybackController(self)
            if self._preview_panel.use_external_player(controller):
                self._playback = controller
                self._transport_bar.attach_playback_controller(controller)

        self._property_panel = PropertyPanel()
        self._property_panel.set_style(self._style)
        self._property_panel.set_preset_schemes(self._style_presets)
        self._property_panel.styleChanged.connect(self._apply_style)
        self._property_panel.presetSchemesChanged.connect(self._apply_style_presets)
        self._property_panel.schemeSelectionChanged.connect(self._on_scheme_selection_changed)
        self._property_panel.layoutAssignAllRequested.connect(self._on_layout_assign_all)
        self._property_panel.layoutAutoAssignRequested.connect(self._on_layout_auto_assign)
        self._property_panel.layoutDeleted.connect(self._on_layout_deleted)
        self._property_panel.set_current_scheme_key(self._selected_scheme_key)
        self._selected_scheme_key = self._property_panel.current_scheme_key()

        self._video_settings_panel = DropPanel(
            extensions={".mp4", ".mkv", ".mov", ".webm", ".avi", ".flv"},
            empty_title="拖入背景视频",
            empty_hint="支持 .mp4 / .mkv / .mov / .webm 等\n或点击此处选择",
            empty_icon="🎬",
        )
        self._video_settings_panel.pathDropped.connect(self.load_video)
        self._video_settings_panel.browseRequested.connect(self._browse_video)
        self._video_settings_panel.set_content(self._property_panel)
        top.addWidget(self._video_settings_panel)

        top.setStretchFactor(0, 1)
        top.setStretchFactor(1, 2)
        top.setSizes([520, max(520, self._property_panel.minimumWidth())])
        body.addWidget(top)

        # 底部：字幕轨道（波形已移除，不做波形图功能）
        self._tracks_view = TrackTimelineView()
        self._tracks_view.seekRequested.connect(self._transport_bar.set_time)
        self._tracks_view.displayWindowEdited.connect(self._on_display_window_edited)
        self._transport_bar.timeChanged.connect(self._tracks_view.set_time)
        body.addWidget(self._tracks_view)

        body.setStretchFactor(0, 5)
        body.setStretchFactor(1, 2)
        body.setSizes([520, 180])

        outer.addWidget(body, 1)
        return page

    def _init_shortcuts(self) -> None:
        # 空格键播放 / 暂停（窗口范围内有效，避免误伤未来的文本输入）
        self._space_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Space), self)
        self._space_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._space_shortcut.activated.connect(self._transport_bar.toggle_play)

        # 项目文件快捷键。作用域限制在本模块内（WidgetWithChildrenShortcut），
        # 嵌入工作台时不会和宿主的全局快捷键打架。
        self._project_shortcuts = []
        for seq, handler in (
            (QKeySequence.StandardKey.New, self._new_project),
            (QKeySequence.StandardKey.Open, self._open_project),
            (QKeySequence.StandardKey.Save, self._save_project),
            (QKeySequence.StandardKey.SaveAs, self._save_project_as),
            # 撤销/重做：字幕轨道显示/隐藏时间编辑（Ctrl+Z / Ctrl+Y；
            # 另补 Ctrl+Shift+Z，StandardKey.Redo 在 Windows 上只映射 Ctrl+Y）
            (QKeySequence.StandardKey.Undo, self._undo_edit),
            (QKeySequence.StandardKey.Redo, self._redo_edit),
            ("Ctrl+Shift+Z", self._redo_edit),
        ):
            shortcut = QShortcut(QKeySequence(seq), self)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(handler)
            self._project_shortcuts.append(shortcut)

    def _make_export_tab(self) -> QWidget:
        page = QWidget()
        page.setObjectName("SubtitleExportPage")
        themed(
            page,
            lambda: "#SubtitleExportPage { background: transparent; }",
        )
        layout = QVBoxLayout(page)
        layout.setContentsMargins(24, 4, 24, 16)
        layout.setSpacing(10)

        # 顶部项目命令栏（同预览页）
        layout.addWidget(self._make_project_bar())

        title = QLabel("导出 MP4")
        themed(
            title,
            lambda: f"color: {palette().title_text}; font-size: 16pt; font-weight: 700;",
        )
        layout.addWidget(title)

        output_row = QHBoxLayout()
        output_row.setContentsMargins(0, 0, 0, 0)
        output_row.setSpacing(8)
        self._export_output_edit = FluentLineEdit()
        self._export_output_edit.setPlaceholderText("选择输出 MP4 路径")
        self._export_browse_button = FluentPushButton("浏览")
        self._export_browse_button.clicked.connect(self._browse_export_output)
        output_row.addWidget(self._export_output_edit, 1)
        output_row.addWidget(self._export_browse_button)
        layout.addLayout(output_row)

        params_row = QHBoxLayout()
        params_row.setContentsMargins(0, 0, 0, 0)
        params_row.setSpacing(10)
        self._export_width_spin = self._export_spin(160, 7680, 1920, " 宽")
        self._export_height_spin = self._export_spin(90, 4320, 1080, " 高")
        self._export_fps_combo = FluentComboBox()
        self._export_fps_combo.setMinimumHeight(32)
        for fps in SCREEN_FPS_OPTIONS:
            self._export_fps_combo.addItem(f"{fps} fps", userData=fps)
        params_row.addWidget(self._labeled_export_control("宽度", self._export_width_spin))
        params_row.addWidget(self._labeled_export_control("高度", self._export_height_spin))
        params_row.addWidget(self._labeled_export_control("帧率", self._export_fps_combo))
        layout.addLayout(params_row)

        encode_row = QHBoxLayout()
        encode_row.setContentsMargins(0, 0, 0, 0)
        encode_row.setSpacing(10)
        self._export_encoder_combo = FluentComboBox()
        self._export_encoder_combo.setMinimumHeight(32)
        self._export_encoder_combo.addItem("CPU / libx264", userData=ENCODER_CPU)
        self._export_encoder_combo.addItem("自动硬编", userData=ENCODER_AUTO)
        self._export_encoder_combo.addItem("NVIDIA NVENC", userData=ENCODER_NVENC)
        self._export_encoder_combo.addItem("Intel QSV", userData=ENCODER_QSV)
        self._export_encoder_combo.addItem("AMD AMF", userData=ENCODER_AMF)
        self._export_preset_combo = FluentComboBox()
        self._export_preset_combo.setMinimumHeight(32)
        for preset in CPU_PRESETS:
            self._export_preset_combo.addItem(preset, userData=preset)
        self._export_preset_combo.setCurrentText("veryfast")
        self._export_crf_spin = self._export_spin(0, 51, 18, " CRF")
        encode_row.addWidget(self._labeled_export_control("编码器", self._export_encoder_combo))
        encode_row.addWidget(self._labeled_export_control("CPU preset", self._export_preset_combo))
        encode_row.addWidget(self._labeled_export_control("质量", self._export_crf_spin))
        layout.addLayout(encode_row)

        self._export_native_check = CheckBox("实验：使用 native 字幕渲染器导出")
        self._export_native_check.setChecked(False)
        self._export_native_check.setEnabled(False)
        self._export_native_check.setVisible(False)
        self._export_native_check.setToolTip("native 字幕渲染器暂时停用。")
        layout.addWidget(self._export_native_check)

        self._export_progress = FluentProgressBar()
        self._export_progress.setRange(0, 1)
        self._export_progress.setValue(0)
        layout.addWidget(self._export_progress)

        self._export_status_label = QLabel("加载字幕和背景视频后即可导出。")
        themed(self._export_status_label, lambda: f"color: {palette().text_hint}; font-size: 10pt;")
        layout.addWidget(self._export_status_label)

        action_row = QHBoxLayout()
        action_row.setContentsMargins(0, 0, 0, 0)
        action_row.setSpacing(8)
        self._export_start_button = FluentPrimaryPushButton("开始导出")
        self._export_start_button.setMinimumHeight(38)
        self._export_start_button.clicked.connect(self._start_render_export)
        self._export_stop_button = FluentPushButton("停止导出")
        self._export_stop_button.setMinimumHeight(38)
        self._export_stop_button.setEnabled(False)
        self._export_stop_button.clicked.connect(self._stop_render_export)
        action_row.addWidget(self._export_start_button, 1)
        action_row.addWidget(self._export_stop_button)
        layout.addLayout(action_row)

        layout.addStretch(1)
        return page

    @staticmethod
    def _export_spin(
        minimum: int, maximum: int, value: int, suffix: str
    ) -> FluentSpinBox:
        spin = FluentSpinBox()
        spin.setRange(minimum, maximum)
        spin.setValue(value)
        spin.setSuffix(suffix)
        spin.setMinimumHeight(32)
        return spin

    def _labeled_export_control(self, label_text: str, control: QWidget) -> QWidget:
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        label = QLabel(label_text)
        themed(label, lambda: f"color: {palette().text_secondary}; font-size: 9.5pt;")
        layout.addWidget(label)
        layout.addWidget(control)
        return box

    # ------------------------------------------------------------------ browse fallback

    def _browse_subtitle(self) -> None:
        start_dir = str(self._subtitle_path.parent) if self._subtitle_path else ""
        path_str, _ = QFileDialog.getOpenFileName(
            self, "选择 SUG 项目或 Nicokara 逐字 LRC 文件", start_dir, SUBTITLE_FILTER
        )
        if path_str:
            self.load_subtitle_source(Path(path_str))

    def _browse_video(self) -> None:
        start_dir = str(self._video_path.parent) if self._video_path else ""
        path_str, _ = QFileDialog.getOpenFileName(
            self, "选择背景视频", start_dir, VIDEO_FILTER
        )
        if path_str:
            self.load_video(Path(path_str))

    def _browse_export_output(self) -> None:
        start = self._default_export_path()
        path_str, _ = QFileDialog.getSaveFileName(self, "导出字幕视频", str(start), OUTPUT_FILTER)
        if path_str:
            path = Path(path_str)
            if path.suffix.lower() != ".mp4":
                path = path.with_suffix(".mp4")
            self._export_output_edit.setText(str(path))

    # ------------------------------------------------------------------ public

    def load_subtitle_source(self, path: Path) -> Optional[TimingTrack]:
        """加载字幕源文件。支持 SUG 项目（.sug）与 Nicokara 逐字 LRC（.lrc）。"""
        suffix = path.suffix.lower()
        if suffix == ".sug":
            return self.load_from_sug(path)
        return self.load_from_lrc(path)

    def load_from_lrc(self, path: Path) -> Optional[TimingTrack]:
        """加载 Nicokara 逐字 LRC 文件。返回解析结果（失败返回 None 并弹错）。"""
        try:
            track = load_nicokara_lrc(path)
        except Exception as exc:  # noqa: BLE001 — 暴露给用户的统一错误处理
            QMessageBox.critical(
                self, "加载字幕失败", f"无法解析字幕文件：\n{path}\n\n错误：{exc}"
            )
            return None
        self._apply_timing_track(track, path)
        return track

    def load_from_sug(self, path: Path) -> Optional[TimingTrack]:
        """加载 SUG 项目文件，直接读取打轴数据而不导出中间 LRC。"""
        try:
            track = load_sug_timing_track(path)
        except Exception as exc:  # noqa: BLE001 — 暴露给用户的统一错误处理
            QMessageBox.critical(
                self, "加载字幕失败", f"无法解析 SUG 项目：\n{path}\n\n错误：{exc}"
            )
            return None
        self._apply_timing_track(track, path)
        return track

    def load_from_sug_project(
        self, project: object, source_path: Optional[Path] = None
    ) -> Optional[TimingTrack]:
        """加载嵌入式 SUG 当前项目对象，供主工作流第 4 步 → 第 5 步接线使用。"""
        try:
            track = timing_track_from_sug_project(project)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(
                self, "加载字幕失败", f"无法读取打轴项目：\n{exc}"
            )
            return None
        self._apply_timing_track(track, source_path)
        return track

    def _apply_timing_track(
        self, track: TimingTrack, source_path: Optional[Path]
    ) -> None:
        self._timing_track = track
        self._subtitle_path = source_path
        self._active_source_index = 0
        # 换字幕源后旧的行索引全部失效
        self._clear_undo_history()
        self._refresh_source_ui()
        self._lyrics_panel.set_track(track)
        self._lyrics_panel.set_role_options(self._merged_role_options())
        self._property_panel.set_roles(track.role_options)
        self._property_panel.set_current_scheme_key(self._selected_scheme_key)
        self._selected_scheme_key = self._property_panel.current_scheme_key()
        self._preview_panel.set_track(track)
        self._sync_tracks_view()
        self._refresh_transport_duration()
        self._transport_bar.set_time(0)
        self._margin_check_timer.start()
        self._mark_project_dirty()

    def load_video(self, path: Path) -> Optional[MediaInfo]:
        """加载背景视频，调用 ffprobe 读取分辨率 / 帧率 / 时长。

        视频如果含音频流，会自动用作播放音轨——用户不需要再单独选音频。
        """
        info = self._probe(path, "视频")
        if info is None:
            return None
        if info.video_streams == 0:
            QMessageBox.warning(self, "背景视频不可用", f"该文件不含视频流：\n{path}")
            return None
        self._video_path = path
        self._video_info = info
        self._preview_panel.set_video_source(path)
        self._video_settings_panel.set_populated(True)
        self._preview_window.set_media_title(path)
        self._preview_window.show_near_workspace()
        if not self._export_output_edit.text().strip():
            self._export_output_edit.setText(str(self._default_export_path()))
        # 视频自带音频 → 喂给 TransportBar 走 QMediaPlayer 播放
        if info.audio_streams > 0:
            self._audio_path = path
            self._audio_info = info
        if self._playback is not None:
            # 单播放器：视频（无论是否含音频）整体交给共享 controller（同时出视频 + 音频）。
            self._transport_bar.set_audio_source(path)
        elif info.audio_streams > 0:
            self._transport_bar.set_audio_source(path)
        self._refresh_transport_duration()
        self._mark_project_dirty()
        return info

    def load_audio(self, path: Path) -> Optional[MediaInfo]:
        """加载独立音轨（覆盖视频自带音频）。

        当前 UI 不直接暴露此入口；保留为 API，便于将来高级用户 / 测试 /
        嵌入工作流（A10）从外部喂独立音频。
        """
        info = self._probe(path, "音频")
        if info is None:
            return None
        if info.audio_streams == 0:
            QMessageBox.warning(self, "音频不可用", f"该文件不含音频流：\n{path}")
            return None
        self._audio_path = path
        self._audio_info = info
        self._transport_bar.set_audio_source(path)
        self._refresh_transport_duration()
        self._mark_project_dirty()
        return info

    @property
    def timing_track(self) -> Optional[TimingTrack]:
        return self._timing_track

    @property
    def video_info(self) -> Optional[MediaInfo]:
        return self._video_info

    @property
    def audio_info(self) -> Optional[MediaInfo]:
        return self._audio_info

    # ------------------------------------------------------------------ helpers

    def _probe(self, path: Path, label: str) -> Optional[MediaInfo]:
        try:
            ffprobe_path = self._resolve_ffprobe_path()
            return probe_media(ffprobe_path, path)
        except ProcessingError as exc:
            QMessageBox.critical(self, f"加载{label}失败", str(exc))
            return None
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(
                self,
                f"加载{label}失败",
                f"无法读取媒体信息：\n{path}\n\n错误：{exc}",
            )
            return None

    def _resolve_ffprobe_path(self) -> str:
        ffmpeg_dir: Optional[Path] = None
        try:
            settings = load_app_settings()
            raw = (settings.ffmpeg_dir or "").strip()
            if raw:
                ffmpeg_dir = Path(raw)
        except Exception:
            ffmpeg_dir = None
        return find_tool("ffprobe", ffmpeg_dir)

    def _refresh_transport_duration(self) -> None:
        candidates: list[int] = [track_duration_ms(track) for track in self._all_tracks()]
        if self._video_info is not None and self._video_info.duration > 0:
            candidates.append(int(self._video_info.duration * 1000))
        if self._audio_info is not None and self._audio_info.duration > 0:
            candidates.append(int(self._audio_info.duration * 1000))
        duration = max(candidates, default=0)
        self._tracks_view.set_duration(duration)
        if duration > 0:
            self._transport_bar.set_duration(duration)

    def _apply_style(self, style: Style) -> None:
        self._style = style
        self._preview_panel.set_style(style)
        self._lyrics_panel.set_style(style)
        # 提前入场/延迟退场等布局参数会改行显示窗口 → 同步轨道把手数据
        self._refresh_tracks_view_windows()
        self._margin_check_timer.start()
        self._save_persisted_state()
        self._mark_project_dirty()

    def _apply_line_layout_indices(self, payload: object) -> None:
        """把项目文件里的每行布局引用套回刚加载的 track。"""
        track = self._timing_track
        if track is None or not isinstance(payload, list):
            return
        limit = len(self._style.layouts)
        for line, value in zip(track.lines, payload):
            try:
                index = int(value)
            except (TypeError, ValueError):
                continue
            line.layout_index = index if 0 <= index <= limit else 0
        self._lyrics_panel.set_style(self._style)
        self._preview_panel.set_style(self._style)

    @staticmethod
    def _line_break_rows(track: Optional[TimingTrack]) -> Optional[list[str]]:
        if track is None:
            return None
        return [str(getattr(line, "break_before", "none")) for line in track.lines]

    def _apply_line_breaks_before(self, payload: object) -> None:
        """恢复 N3 的显式 PageBreak / ParagraphBreak 页边界。"""
        track = self._timing_track
        if track is None or not isinstance(payload, list):
            return
        for line, value in zip(track.lines, payload):
            kind = str(value)
            line.break_before = kind if kind in {"page", "paragraph"} else "none"
        self._lyrics_panel.set_track(track)
        self._preview_panel.set_track(track)

    @staticmethod
    def _display_override_rows(track: Optional[TimingTrack]) -> Optional[list]:
        """采集逐行显示/隐藏覆盖：与 ``track.lines`` 对齐，无覆盖的行为 None。"""
        if track is None:
            return None
        rows = [
            (
                [line.display_start_override_ms, line.display_end_override_ms]
                if line.display_start_override_ms is not None
                or line.display_end_override_ms is not None
                else None
            )
            for line in track.lines
        ]
        return rows if any(row is not None for row in rows) else None

    @staticmethod
    def _apply_display_override_rows(track: TimingTrack, payload: object) -> None:
        """把项目文件里的逐行显示/隐藏覆盖套回刚加载的 track。"""
        if not isinstance(payload, list):
            return
        for line, row in zip(track.lines, payload):
            if not isinstance(row, (list, tuple)) or len(row) != 2:
                continue
            start, end = row
            line.display_start_override_ms = (
                int(start) if isinstance(start, (int, float)) else None
            )
            line.display_end_override_ms = (
                int(end) if isinstance(end, (int, float)) else None
            )

    @staticmethod
    def _animation_override_rows(track: Optional[TimingTrack]) -> Optional[list]:
        """采集逐行动画覆盖；全部继承全局时不写项目字段。"""
        if track is None:
            return None
        rows = [line_animation_override_to_dict(line.animation_override) for line in track.lines]
        return rows if any(row is not None for row in rows) else None

    @staticmethod
    def _apply_animation_override_rows(track: TimingTrack, payload: object) -> None:
        if not isinstance(payload, list):
            return
        for line, row in zip(track.lines, payload):
            line.animation_override = line_animation_override_from_dict(row)

    def _collect_char_role_labels(self) -> Optional[list]:
        """收集主字幕每行逐字角色标签用于项目持久化；全部为空则返回 None（不写盘）。"""
        if self._timing_track is None:
            return None
        return self._char_role_rows(self._timing_track)

    @staticmethod
    def _char_role_rows(track: TimingTrack) -> Optional[list]:
        rows: list = []
        any_label = False
        for line in track.lines:
            if any(ch.role_label for ch in line.chars):
                any_label = True
                rows.append([ch.role_label for ch in line.chars])
            else:
                rows.append(None)
        return rows if any_label else None

    def _apply_char_role_labels(self, payload: object) -> None:
        """把项目文件 / N3 导入的逐字角色标签套回刚加载的 track。"""
        track = self._timing_track
        if track is None or not isinstance(payload, list):
            return
        changed = False
        for line, labels in zip(track.lines, payload):
            if not isinstance(labels, list):
                continue
            for ch, label in zip(line.chars, labels):
                new_label = str(label) if label else None
                if ch.role_label != new_label:
                    ch.role_label = new_label
                    changed = True
        if not changed:
            return
        self._lyrics_panel.set_track(track)
        self._lyrics_panel.set_role_options(self._merged_role_options())
        self._property_panel.set_roles(track.role_options)
        self._preview_panel.set_track(track)

    # ------------------------------------------------------- 副字幕源（N3 多歌词文件）

    def _apply_extra_subtitle_sources(self, payload: object) -> None:
        """从项目快照 / N3 导入恢复副字幕源（含每行布局与逐字角色）。"""
        self._extra_sources = []
        self._active_source_index = 0
        if isinstance(payload, list):
            layout_limit = len(self._style.layouts)
            for item in payload:
                if not isinstance(item, dict):
                    continue
                path_text = str(item.get("path") or "").strip()
                if not path_text:
                    continue
                path = Path(path_text)
                if not path.is_file():
                    continue
                try:
                    track = self._load_timing_track_file(path)
                except Exception:  # noqa: BLE001 — 单个副源坏了不阻塞项目打开
                    continue
                layout_indices = item.get("line_layout_indices")
                if isinstance(layout_indices, list):
                    for line, value in zip(track.lines, layout_indices):
                        try:
                            index = int(value)
                        except (TypeError, ValueError):
                            continue
                        line.layout_index = index if 0 <= index <= layout_limit else 0
                breaks = item.get("line_breaks_before")
                if isinstance(breaks, list):
                    for line, value in zip(track.lines, breaks):
                        kind = str(value)
                        line.break_before = (
                            kind if kind in {"page", "paragraph"} else "none"
                        )
                role_rows = item.get("char_role_labels")
                if isinstance(role_rows, list):
                    for line, labels in zip(track.lines, role_rows):
                        if not isinstance(labels, list):
                            continue
                        for ch, label in zip(line.chars, labels):
                            ch.role_label = str(label) if label else None
                self._apply_display_override_rows(
                    track, item.get("line_display_overrides")
                )
                self._apply_animation_override_rows(
                    track, item.get("line_animation_overrides")
                )
                name = str(item.get("name") or "").strip() or path.stem
                self._extra_sources.append(
                    ExtraSubtitleSource(name=name, path=path, track=track)
                )
        self._refresh_source_ui()
        self._refresh_lyrics_panel_source()
        self._sync_extra_tracks_to_preview()
        self._refresh_transport_duration()

    def _all_tracks(self) -> list[TimingTrack]:
        tracks = [] if self._timing_track is None else [self._timing_track]
        tracks.extend(source.track for source in self._extra_sources)
        return tracks

    def _extra_track_list(self) -> list[TimingTrack]:
        return [source.track for source in self._extra_sources]

    def _active_track(self) -> Optional[TimingTrack]:
        """歌词列表当前显示的 track（0 = 主字幕）。"""
        index = self._active_source_index
        if index <= 0:
            return self._timing_track
        if index - 1 < len(self._extra_sources):
            return self._extra_sources[index - 1].track
        return self._timing_track

    def _refresh_source_ui(self) -> None:
        """刷新歌词面板的字幕源下拉；无主字幕时隐藏。"""
        if self._timing_track is None:
            self._active_source_index = 0
            self._lyrics_panel.set_sources([], 0)
            return
        names = ["主字幕"] + [source.name for source in self._extra_sources]
        self._active_source_index = max(0, min(self._active_source_index, len(names) - 1))
        self._lyrics_panel.set_sources(names, self._active_source_index)

    def _refresh_lyrics_panel_source(self) -> None:
        """把当前选中源的行喂给歌词列表。"""
        self._lyrics_panel.set_track(self._active_track())
        self._lyrics_panel.set_role_options(self._merged_role_options())

    def _sync_extra_tracks_to_preview(self) -> None:
        self._preview_panel.set_extra_tracks(self._extra_track_list())
        self._sync_tracks_view()

    def _sync_tracks_view(self) -> None:
        """把主 + 副字幕源喂给底部字幕轨道（T1 = 主字幕）。"""
        if self._timing_track is None:
            self._tracks_view.set_tracks([])
            return
        named = [("主字幕", self._timing_track)]
        named.extend((source.name, source.track) for source in self._extra_sources)
        self._tracks_view.set_tracks(named)
        self._refresh_tracks_view_windows()

    def _refresh_tracks_view_windows(self) -> None:
        """按当前样式重算各轨行显示窗口，推给字幕轨道（把手条数据源）。"""
        if self._timing_track is None:
            return
        self._tracks_view.set_display_windows(
            [display_windows_for_style(track, self._style) for track in self._all_tracks()]
        )

    def _on_display_window_edited(
        self, track_index: int, line_index: int, old_values: object, new_values: object
    ) -> None:
        """字幕轨道拖动把手改了某句显示/隐藏时间：入撤销栈 + 刷新预览 + 标脏。"""
        self._undo_stack.append((track_index, line_index, old_values, new_values))
        del self._undo_stack[:-_UNDO_STACK_LIMIT]
        self._redo_stack.clear()
        self._refresh_after_display_edit(track_index)

    def _on_line_animation_override_requested(
        self, rows: list[int], override: Optional[LineAnimationOverride]
    ) -> None:
        """歌词列表批量修改逐行特效：应用、入撤销栈并立即刷新预览。"""
        track_index = self._active_source_index
        track = self._track_by_index(track_index)
        if track is None:
            return
        valid_rows = sorted({int(row) for row in rows if 0 <= int(row) < len(track.lines)})
        if not valid_rows:
            return
        old_values = tuple(track.lines[row].animation_override for row in valid_rows)
        new_values = tuple(override for _row in valid_rows)
        if old_values == new_values:
            return
        for row in valid_rows:
            track.lines[row].animation_override = override
        self._undo_stack.append(
            ("animation", track_index, tuple(valid_rows), old_values, new_values)
        )
        del self._undo_stack[:-_UNDO_STACK_LIMIT]
        self._redo_stack.clear()
        self._refresh_after_display_edit(track_index)
        for row in valid_rows:
            self._lyrics_panel.refresh_row_effect(row)
        if hasattr(self, "_style") and hasattr(self, "_transport_bar"):
            first_window = display_windows_for_style(track, self._style).get(valid_rows[0])
            if first_window is not None:
                self._transport_bar.set_time(max(first_window[0], 0))

    def _refresh_after_display_edit(self, track_index: int) -> None:
        # 覆盖值已直接写在 TimingLine 上；track 是原地修改的，
        # 预览（含异步渲染 worker）不会自己发现——重新喂一次。
        if track_index == 0 and self._timing_track is not None:
            self._preview_panel.set_track(self._timing_track)
        elif track_index > 0:
            # 不走 _sync_extra_tracks_to_preview：它会重建轨道视图、丢掉选中态
            self._preview_panel.set_extra_tracks(self._extra_track_list())
        self._refresh_tracks_view_windows()
        self._mark_project_dirty()

    def _track_by_index(self, track_index: int) -> Optional[TimingTrack]:
        if track_index == 0:
            return self._timing_track
        if 1 <= track_index <= len(self._extra_sources):
            return self._extra_sources[track_index - 1].track
        return None

    def _restore_display_override(
        self, track_index: int, line_index: int, values: object
    ) -> bool:
        track = self._track_by_index(track_index)
        if (
            track is None
            or not isinstance(values, tuple)
            or not 0 <= line_index < len(track.lines)
        ):
            return False
        start, end = values
        line = track.lines[line_index]
        line.display_start_override_ms = start
        line.display_end_override_ms = end
        self._refresh_after_display_edit(track_index)
        return True

    def _restore_animation_overrides(
        self, track_index: int, rows: object, values: object
    ) -> bool:
        track = self._track_by_index(track_index)
        if track is None or not isinstance(rows, tuple) or not isinstance(values, tuple):
            return False
        if len(rows) != len(values) or any(not 0 <= row < len(track.lines) for row in rows):
            return False
        for row, value in zip(rows, values):
            track.lines[row].animation_override = value
        self._refresh_after_display_edit(track_index)
        if track_index == self._active_source_index:
            for row in rows:
                self._lyrics_panel.refresh_row_effect(row)
        return True

    def _undo_edit(self) -> None:
        """Ctrl+Z：撤销最近一次字幕轨道时间或逐行特效编辑。"""
        while self._undo_stack:
            command = self._undo_stack.pop()
            if len(command) == 5 and command[0] == "animation":
                _kind, track_index, rows, old_values, new_values = command
                if self._restore_animation_overrides(track_index, rows, old_values):
                    self._redo_stack.append(command)
                    return
                continue
            track_index, line_index, old_values, new_values = command
            if self._restore_display_override(track_index, line_index, old_values):
                self._redo_stack.append(
                    (track_index, line_index, old_values, new_values)
                )
                return
            # 目标轨道/行已不存在（换源等）→ 丢弃该条继续往下找

    def _redo_edit(self) -> None:
        """Ctrl+Y / Ctrl+Shift+Z：重做被撤销的字幕轨道编辑。"""
        while self._redo_stack:
            command = self._redo_stack.pop()
            if len(command) == 5 and command[0] == "animation":
                _kind, track_index, rows, old_values, new_values = command
                if self._restore_animation_overrides(track_index, rows, new_values):
                    self._undo_stack.append(command)
                    return
                continue
            track_index, line_index, old_values, new_values = command
            if self._restore_display_override(track_index, line_index, new_values):
                self._undo_stack.append(
                    (track_index, line_index, old_values, new_values)
                )
                return

    def _clear_undo_history(self) -> None:
        self._undo_stack.clear()
        self._redo_stack.clear()

    def _on_source_selected(self, index: int) -> None:
        self._active_source_index = max(int(index), 0)
        self._refresh_lyrics_panel_source()

    def _on_source_add_requested(self) -> None:
        if self._timing_track is None:
            QMessageBox.information(self, "先加载主字幕", "请先加载主字幕文件，再添加副字幕源。")
            return
        start_dir = str(self._subtitle_path.parent) if self._subtitle_path else ""
        path_str, _ = QFileDialog.getOpenFileName(
            self, "添加副字幕源（与主字幕同时显示）", start_dir, SUBTITLE_FILTER
        )
        if not path_str:
            return
        path = Path(path_str)
        try:
            track = self._load_timing_track_file(path)
        except Exception as exc:  # noqa: BLE001 — 统一错误弹窗
            QMessageBox.critical(
                self, "加载字幕失败", f"无法解析字幕文件：\n{path}\n\n错误：{exc}"
            )
            return
        self._extra_sources.append(
            ExtraSubtitleSource(name=path.stem, path=path, track=track)
        )
        self._active_source_index = len(self._extra_sources)
        self._refresh_source_ui()
        self._refresh_lyrics_panel_source()
        self._sync_extra_tracks_to_preview()
        self._refresh_transport_duration()
        self._margin_check_timer.start()
        self._mark_project_dirty()

    @staticmethod
    def _load_timing_track_file(path: Path) -> TimingTrack:
        if path.suffix.lower() == ".sug":
            return load_sug_timing_track(path)
        return load_nicokara_lrc(path)

    def _on_source_remove_requested(self, index: int) -> None:
        extra_index = int(index) - 1
        if not 0 <= extra_index < len(self._extra_sources):
            return
        source = self._extra_sources[extra_index]
        choice = QMessageBox.question(
            self,
            "移除副字幕源",
            f"确定移除副字幕源「{source.name}」？\n（不会删除歌词文件本身）",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if choice != QMessageBox.StandardButton.Yes:
            return
        del self._extra_sources[extra_index]
        self._active_source_index = 0
        # 副字幕源序号整体前移，撤销记录里的轨道序号失效
        self._clear_undo_history()
        self._refresh_source_ui()
        self._refresh_lyrics_panel_source()
        self._sync_extra_tracks_to_preview()
        self._refresh_transport_duration()
        self._mark_project_dirty()

    def _rescale_layout_for_height(self, new_height: int) -> None:
        """输出高度变化时按 N3 SizeAndRatio 语义重算布局像素字段。"""
        rescaled = rescale_layout_sizes(self._style, new_height)
        if rescaled is self._style:
            return
        self._style = rescaled
        self._property_panel.set_style(self._style)
        self._apply_style(self._style)

    def _on_layout_change_requested(self, rows: list, layout_index: int) -> None:
        """歌词列表右键应用布局：对每个选中行按页联动写入（作用于当前选中源）。"""
        track = self._active_track()
        if track is None:
            return
        changed: set[int] = set()
        for row in rows:
            if isinstance(row, int) and 0 <= row < len(track.lines):
                changed.update(
                    apply_layout_to_page(track, self._style, row, int(layout_index))
                )
        if changed:
            self._refresh_after_layout_assignment()

    def _on_layout_assign_all(self, layout_index: int) -> None:
        track = self._active_track()
        if track is None:
            return
        if assign_layout_to_all(track, int(layout_index)):
            self._refresh_after_layout_assignment()

    def _on_layout_auto_assign(self) -> None:
        track = self._active_track()
        if track is None:
            return
        if auto_assign_layouts_by_page(track, self._style):
            self._refresh_after_layout_assignment()

    def _on_layout_deleted(self, deleted_index: int) -> None:
        """布局被删除后修正歌词行引用（全部字幕源）：被删的回默认，其后的序号前移。"""
        changed = False
        for track in self._all_tracks():
            for line in track.lines:
                index = int(getattr(line, "layout_index", 0) or 0)
                if index == deleted_index:
                    line.layout_index = 0
                    changed = True
                elif index > deleted_index:
                    line.layout_index = index - 1
                    changed = True
        if changed:
            self._refresh_after_layout_assignment()

    def _refresh_after_layout_assignment(self) -> None:
        # track 是就地修改的，set_style 只为触发预览/列表重绘；副轨需重喂 worker。
        self._preview_panel.set_style(self._style)
        self._lyrics_panel.set_style(self._style)
        self._sync_extra_tracks_to_preview()
        self._margin_check_timer.start()
        self._mark_project_dirty()

    def _check_layout_margins(self) -> None:
        """N3 式左右余白检查（全部字幕源）：溢出画面 → Warning；侵入余白 → Information。"""
        tracks = self._all_tracks()
        if not tracks:
            return
        try:
            warnings = [
                warning
                for track in tracks
                for warning in check_layout_margins(track, self._style, self._screen_settings.width)
            ]
        except Exception:  # noqa: BLE001 — 检查失败不影响正常编辑
            return
        overflow = [w for w in warnings if w.level == "overflow"]
        margin = [w for w in warnings if w.level == "margin"]
        key = (
            f"o:{','.join(str(w.line_index) for w in overflow)}"
            f"|m:{','.join(str(w.line_index) for w in margin)}"
        )
        if key == self._last_margin_warning_key:
            return
        self._last_margin_warning_key = key
        if overflow:
            InfoBar.warning(
                title="字幕溢出画面",
                content=f"{_format_warning_lines(overflow)}超出画面范围，"
                "请调小字号或缩短该行。",
                parent=self,
                position=InfoBarPosition.BOTTOM_RIGHT,
                duration=5000,
            )
        elif margin:
            InfoBar.info(
                title="左右余白无法确保",
                content=f"{_format_warning_lines(margin)}侵入左右余白。",
                parent=self,
                position=InfoBarPosition.BOTTOM_RIGHT,
                duration=4000,
            )

    def _apply_style_presets(self, presets: dict) -> None:
        self._style_presets = _style_presets_from_dict(presets)
        if hasattr(self, "_lyrics_panel") and self._lyrics_panel is not None:
            self._lyrics_panel.set_role_options(self._merged_role_options())
        self._save_persisted_state()

    def _on_export_screen_changed(self) -> None:
        if self._syncing_screen_controls:
            return
        self._screen_settings = ScreenSettings(
            preset_key="custom",
            par=self._screen_settings.par,
            width=self._export_width_spin.value(),
            height=self._export_height_spin.value(),
            fps=self._export_fps_value(),
        )
        self._screen_settings = ScreenSettings(
            preset_key=match_screen_preset_key(
                self._screen_settings.width,
                self._screen_settings.height,
                self._screen_settings.par,
            ),
            par=self._screen_settings.par,
            width=self._screen_settings.width,
            height=self._screen_settings.height,
            fps=self._screen_settings.fps,
        )
        self._transport_bar.set_preview_fps(self._screen_settings.fps)
        self._rescale_layout_for_height(self._screen_settings.height)
        self._margin_check_timer.start()
        self._save_persisted_state()

    def _set_export_screen_controls(self, settings: ScreenSettings) -> None:
        self._syncing_screen_controls = True
        try:
            self._export_width_spin.setValue(settings.width)
            self._export_height_spin.setValue(settings.height)
            self._set_export_fps_value(settings.fps)
        finally:
            self._syncing_screen_controls = False

    def _export_fps_value(self) -> int:
        data = self._export_fps_combo.currentData()
        return int(data) if data in SCREEN_FPS_OPTIONS else 60

    def _set_export_fps_value(self, fps: int) -> None:
        index = self._export_fps_combo.findData(fps)
        self._export_fps_combo.setCurrentIndex(index if index >= 0 else 0)

    def _on_output_settings_changed(self) -> None:
        self._save_persisted_state()
        self._mark_project_dirty()

    def _on_scheme_selection_changed(self, key: str) -> None:
        self._selected_scheme_key = key
        self._save_persisted_state()
        self._mark_project_dirty()

    def _merged_role_options(self) -> list[str]:
        """合并各字幕源的 LRC 角色标签 与 自建配色方案名，供歌词列表角色下拉使用。"""
        options: list[str] = []
        seen: set[str] = set()
        for track in self._all_tracks():
            for name in track.role_options:
                if name not in seen:
                    seen.add(name)
                    options.append(name)
        for name in self._style_presets:
            if name not in seen:
                seen.add(name)
                options.append(name)
        return options

    def _on_lyrics_role_changed(self, row: int, role_name: str) -> None:
        """用户修改了某句歌词的角色时，将角色名写入该行所有字素（当前选中源）。"""
        track = self._active_track()
        if track is None:
            return
        if row < 0 or row >= len(track.lines):
            return
        label = role_name.strip() if role_name else None
        for ch in track.lines[row].chars:
            ch.role_label = label
        # 角色名来自预设库但样式里还没有对应方案时，先把预设物化进
        # custom_style_schemes——否则 painter 解析不到，改了角色颜色毫无变化。
        if (
            label
            and label not in self._style.custom_style_schemes
            and label in self._style_presets
        ):
            schemes = dict(self._style.custom_style_schemes)
            schemes[label] = deepcopy(self._style_presets[label])
            self._style = replace(self._style, custom_style_schemes=schemes)
            self._property_panel.set_style(self._style)
            self._preview_panel.set_style(self._style)
            self._lyrics_panel.set_style(self._style)
            self._save_persisted_state()
        # track 是原地修改的，预览（含异步渲染 worker）不会自己发现——
        # 重新喂一次让当前帧立即按新角色配色重渲染。
        if self._active_source_index == 0:
            self._preview_panel.set_track(track)
        else:
            self._sync_extra_tracks_to_preview()
        self._lyrics_panel.refresh_row_role(row)
        self._mark_project_dirty()

    def _on_lyrics_row_clicked(self, row: int) -> None:
        """点击歌词列表某行 → 预览跳转到该行起始时间（当前选中源）。"""
        track = self._active_track()
        if track is None:
            return
        if row < 0 or row >= len(track.lines):
            return
        line = track.lines[row]
        if line.is_blank or not line.chars:
            return
        start_ms = line.chars[0].start_ms
        self._transport_bar.set_time(start_ms)

    def _load_persisted_state(self) -> None:
        data = self._load_subtitle_settings()
        self._style = style_from_dict(data.get("style"))
        self._style_presets = _style_presets_from_dict(data.get("style_presets"))
        self._screen_settings = screen_settings_from_dict(data.get("screen"))
        key = data.get("selected_scheme_key")
        if isinstance(key, str) and key:
            self._selected_scheme_key = key

    def _save_persisted_state(self) -> None:
        data = self._load_subtitle_settings()
        data["style"] = style_to_dict(self._style)
        data["style_presets"] = _style_presets_to_dict(self._style_presets)
        data["screen"] = screen_settings_to_dict(self._screen_settings)
        data["selected_scheme_key"] = self._selected_scheme_key
        if hasattr(self, "_export_native_check"):
            output = dict(data.get("output")) if isinstance(data.get("output"), dict) else {}
            output["native_export_enabled"] = False
            data["output"] = output
        try:
            if self._settings_provider is not None and hasattr(self._settings_provider, "save"):
                self._settings_provider.save(data)
                return
            settings = load_app_settings()
            settings.subtitle_render = data
            save_app_settings(settings)
        except Exception:
            return

    def _load_subtitle_settings(self) -> dict:
        try:
            if self._settings_provider is not None and hasattr(self._settings_provider, "load"):
                loaded = self._settings_provider.load()
            else:
                loaded = load_app_settings().subtitle_render
            return dict(loaded) if isinstance(loaded, dict) else {}
        except Exception:
            return {}

    def _sync_preview_output_size(self) -> None:
        self._preview_panel.set_output_size(
            self._export_width_spin.value(),
            self._export_height_spin.value(),
        )

    def _default_export_path(self) -> Path:
        base = self._video_path or self._subtitle_path
        if base is None:
            return Path.cwd() / "subtitle_render.mp4"
        return base.with_name(f"{base.stem}_subtitle.mp4")

    def _resolve_ffmpeg_dir(self) -> Optional[Path]:
        try:
            settings = load_app_settings()
            raw = (settings.ffmpeg_dir or "").strip()
            return Path(raw) if raw else None
        except Exception:
            return None

    def _build_render_job(self) -> RenderJob:
        if self._timing_track is None:
            raise ProcessingError("请先加载字幕文件。")
        if self._video_path is None:
            raise ProcessingError("请先加载背景视频。")
        output_text = self._export_output_edit.text().strip()
        if not output_text:
            raise ProcessingError("请先选择输出路径。")
        output_path = Path(output_text).expanduser()
        if output_path.suffix.lower() != ".mp4":
            output_path = output_path.with_suffix(".mp4")
            self._export_output_edit.setText(str(output_path))
        duration_ms = self._current_export_duration_ms()
        return RenderJob(
            track=self._timing_track,
            style=self._style,
            background_video_path=self._video_path,
            output_path=output_path,
            extra_tracks=tuple(self._extra_track_list()),
            width=self._export_width_spin.value(),
            height=self._export_height_spin.value(),
            fps=self._export_fps_value(),
            duration_ms=duration_ms,
            include_audio=bool(self._video_info and self._video_info.audio_streams > 0),
            encoder_mode=str(self._export_encoder_combo.currentData() or ENCODER_CPU),
            crf=self._export_crf_spin.value(),
            preset=str(self._export_preset_combo.currentData() or "veryfast"),
            native_export_enabled=False,
        )

    def _current_export_duration_ms(self) -> int:
        candidates: list[int] = [track_duration_ms(track) for track in self._all_tracks()]
        if self._video_info is not None and self._video_info.duration > 0:
            candidates.append(int(round(self._video_info.duration * 1000)))
        return max(candidates, default=0)

    def _start_render_export(self) -> None:
        if self._render_thread is not None and self._render_thread.isRunning():
            QMessageBox.information(self, "导出中", "当前导出任务还在处理中，请稍等。")
            return
        try:
            job = self._build_render_job()
        except ProcessingError as exc:
            QMessageBox.critical(self, "无法导出", str(exc))
            return

        self._export_start_button.setEnabled(False)
        self._export_stop_button.setEnabled(True)
        self._export_progress.setRange(0, 0)
        self._export_status_label.setText("正在准备导出…")

        thread = QThread(self)
        worker = _RenderWorker(job, self._resolve_ffmpeg_dir())
        worker.moveToThread(thread)
        worker.progressChanged.connect(self._on_render_progress)
        worker.logMessage.connect(self._on_render_log)
        worker.finished.connect(self._finish_render_success)
        worker.cancelled.connect(self._finish_render_cancelled)
        worker.failed.connect(self._finish_render_failure)
        worker.finished.connect(thread.quit)
        worker.cancelled.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(self._clear_render_thread)
        thread.started.connect(worker.run)
        self._render_thread = thread
        self._render_worker = worker
        thread.start()

    def _stop_render_export(self) -> None:
        if self._render_worker is None or self._render_thread is None or not self._render_thread.isRunning():
            return
        self._export_stop_button.setEnabled(False)
        self._export_status_label.setText("正在停止导出…")
        self._render_worker.cancel()

    def _on_render_progress(self, done: int, total: int) -> None:
        self._export_progress.setRange(0, max(total, 1))
        self._export_progress.setValue(done)
        self._export_status_label.setText(f"正在导出… {done}/{total} 帧")

    def _on_render_log(self, message: str) -> None:
        self._export_status_label.setText(message)

    def _finish_render_success(self, output_path: Path) -> None:
        self._export_progress.setRange(0, 1)
        self._export_progress.setValue(1)
        self._export_status_label.setText(f"导出完成: {output_path}")
        self._export_start_button.setEnabled(True)
        self._export_stop_button.setEnabled(False)
        context = self._workflow_context
        if context is not None and hasattr(context, "accept_subtitle_video"):
            context.accept_subtitle_video(output_path)

    def _finish_render_cancelled(self, message: str) -> None:
        self._export_progress.setRange(0, 1)
        self._export_progress.setValue(0)
        self._export_status_label.setText("导出已停止，未完成文件已清理。" if message else "导出已停止。")
        self._export_start_button.setEnabled(True)
        self._export_stop_button.setEnabled(False)

    def _finish_render_failure(self, message: str) -> None:
        self._export_progress.setRange(0, 1)
        self._export_progress.setValue(0)
        self._export_status_label.setText("导出失败")
        self._export_start_button.setEnabled(True)
        self._export_stop_button.setEnabled(False)
        QMessageBox.critical(self, "导出失败", message)

    def _clear_render_thread(self) -> None:
        self._render_thread = None
        self._render_worker = None

    # ------------------------------------------------------------------ embed

    @staticmethod
    def for_embedding(
        parent: Optional[QWidget] = None,
        settings_provider: Optional[Any] = None,
        workflow_context: Optional[Any] = None,
    ) -> "SubtitleRenderWindow":
        """创建嵌入工作台用的实例。"""
        instance = SubtitleRenderWindow(
            embedded=True,
            settings_provider=settings_provider,
            workflow_context=workflow_context,
            parent=parent,
        )
        return instance

    def flush_unsaved(self) -> None:
        """宿主销毁本 widget 前调用的兜底（占位）。"""
        return


def _style_presets_from_dict(payload: object) -> dict[str, SubtitleStyleScheme]:
    if not isinstance(payload, dict):
        return {}
    return {
        str(name): subtitle_style_scheme_from_dict(value)
        for name, value in payload.items()
        if str(name)
    }


def _style_presets_to_dict(
    presets: dict[str, SubtitleStyleScheme],
) -> dict[str, dict]:
    return {
        str(name): subtitle_style_scheme_to_dict(scheme)
        for name, scheme in presets.items()
        if str(name)
    }
