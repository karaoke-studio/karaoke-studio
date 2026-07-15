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
from math import isfinite
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Optional

from PyQt6.QtCore import QObject, QPoint, QRect, QSize, QThread, QTimer, QUrl, Qt, pyqtSignal as Signal
from PyQt6.QtGui import QColor, QDesktopServices, QImage, QKeySequence, QPixmap, QShortcut
from PyQt6.QtWidgets import (
    QFileDialog,
    QColorDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    Action,
    CaptionLabel,
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
    SimpleCardWidget,
    SpinBox as FluentSpinBox,
    StrongBodyLabel,
    TitleLabel,
)

from krok_helper.errors import ExportCancelled, ProcessingError
from krok_helper.ffmpeg import find_tool, probe_media, terminate_process
from krok_helper.models import MediaInfo
from krok_helper.qfluent_compat import apply_qfluent_menu_lifetime_patch
from krok_helper.settings import load_app_settings, save_app_settings
from krok_helper.subtitle_render.engine.encoder_select import (
    CODEC_H264,
    CODEC_HEVC,
    CPU_PRESETS,
    ENCODER_AMF,
    ENCODER_AUTO,
    ENCODER_CPU,
    ENCODER_NVENC,
    ENCODER_QSV,
)
from krok_helper.subtitle_render.engine.painter import (
    _resolve_title_text,
    apply_layout_to_page,
    assign_layout_to_all,
    auto_assign_layouts_by_page,
    check_layout_margins,
    display_windows_for_style,
)
from krok_helper.subtitle_render.engine.renderer import RenderJob, render_subtitle_video
from krok_helper.subtitle_render.engine.timeline import track_duration_ms
from krok_helper.subtitle_render.frontend.drop_panel import DropPanel
from krok_helper.subtitle_render.frontend.fluent_dialogs import (
    fluent_choice,
    fluent_error,
    fluent_info,
    fluent_question,
    fluent_warning,
)
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
    BackgroundSource,
    DEFAULT_OUTPUT_NAME_SUFFIX,
    LineAnimationOverride,
    PROJECT_FILE_SUFFIX,
    StylePreset,
    SubtitleStyleScheme,
    Style,
    TITLE_SCHEME_NAME,
    TimingTrack,
    background_sequence_frame_path,
    line_animation_override_from_dict,
    line_animation_override_to_dict,
    migrate_legacy_app_title_default,
    rescale_layout_sizes,
    subtitle_style_scheme_from_dict,
    subtitle_style_scheme_to_dict,
    style_from_dict,
    style_to_dict,
    infer_image_sequence_pattern,
)
from krok_helper.subtitle_render.n3_font_catalog import (
    get_n3_font_catalog,
    normalize_scheme_font_families,
    normalize_style_font_families,
)
from krok_helper.subtitle_render.n3proj_import import N3_PROJECT_FILTER, load_n3proj
from krok_helper.subtitle_render.project_store import (
    background_payload,
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
from krok_helper.subtitle_render.frontend.theme import palette, stage_bg, themed

apply_qfluent_menu_lifetime_patch()

SUBTITLE_FILTER = "SUG 项目 / Nicokara LRC (*.sug *.lrc);;SUG 项目 (*.sug);;Nicokara 逐字 LRC (*.lrc);;所有文件 (*.*)"

_UNDO_STACK_LIMIT = 200
"""撤销栈上限（字幕轨道显示/隐藏时间编辑）。"""
VIDEO_FILTER = "视频文件 (*.mp4 *.mkv *.mov *.webm *.avi *.flv);;所有文件 (*.*)"
IMAGE_FILTER = "图片文件 (*.png *.jpg *.jpeg *.bmp *.webp *.tif *.tiff);;所有文件 (*.*)"
AUDIO_FILTER = "音频文件 (*.wav *.flac *.mp3 *.m4a *.aac *.ogg *.opus);;所有文件 (*.*)"
BACKGROUND_MEDIA_FILTER = (
    "背景素材 (*.mp4 *.mkv *.mov *.webm *.avi *.flv *.png *.jpg *.jpeg *.bmp *.webp *.tif *.tiff);;"
    + VIDEO_FILTER + ";;" + IMAGE_FILTER
)
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff"}
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

    def set_aspect_ratio(self, width: int, height: int) -> None:
        """Update the child aspect ratio from an output size."""
        if width <= 0 or height <= 0:
            return
        self._aspect_ratio = max(float(width) / float(height), 0.1)
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


_EXPORT_PREVIEW_DEFAULT_WIDTH = 640
_EXPORT_PREVIEW_MIN_WIDTH = 320


def _export_preview_width(
    view_size: QSize,
    device_pixel_ratio: float,
    output_width: int,
    output_height: int,
) -> int:
    """Return the fitted preview width in physical pixels."""
    safe_output_width = max(int(output_width), 1)
    fallback = min(safe_output_width, _EXPORT_PREVIEW_DEFAULT_WIDTH)
    if (
        view_size.width() <= 0
        or view_size.height() <= 0
        or output_width <= 0
        or output_height <= 0
        or not isfinite(device_pixel_ratio)
        or device_pixel_ratio <= 0
    ):
        return fallback
    fitted_logical_width = min(
        float(view_size.width()),
        float(view_size.height()) * output_width / output_height,
    )
    physical_width = int(round(fitted_logical_width * device_pixel_ratio))
    return min(safe_output_width, max(_EXPORT_PREVIEW_MIN_WIDTH, physical_width))


def _physical_preview_size(size: QSize, device_pixel_ratio: float) -> QSize:
    """Convert a logical widget size to a positive physical-pixel size."""
    dpr = device_pixel_ratio if isfinite(device_pixel_ratio) and device_pixel_ratio > 0 else 1.0
    return QSize(
        max(int(round(size.width() * dpr)), 1),
        max(int(round(size.height() * dpr)), 1),
    )


def _scaled_preview_pixmap(
    frame: QPixmap,
    logical_size: QSize,
    device_pixel_ratio: float,
) -> QPixmap:
    """Scale a frame for a logical widget while retaining physical pixels."""
    dpr = device_pixel_ratio if isfinite(device_pixel_ratio) and device_pixel_ratio > 0 else 1.0
    target_size = _physical_preview_size(logical_size, dpr)
    pixmap = frame.scaled(
        target_size,
        Qt.AspectRatioMode.KeepAspectRatioByExpanding,
        Qt.TransformationMode.SmoothTransformation,
    )
    if pixmap.size() != target_size:
        pixmap = pixmap.copy(
            max((pixmap.width() - target_size.width()) // 2, 0),
            max((pixmap.height() - target_size.height()) // 2, 0),
            target_size.width(),
            target_size.height(),
        )
    pixmap.setDevicePixelRatio(dpr)
    return pixmap


class _RenderWorker(QObject):
    progressChanged = Signal(int, int)
    logMessage = Signal(str)
    finished = Signal(Path)
    cancelled = Signal(str)
    failed = Signal(str)

    def __init__(
        self,
        job: RenderJob,
        ffmpeg_dir: Optional[Path],
        preview_image_path: Optional[Path] = None,
        preview_width: Optional[int] = None,
    ) -> None:
        super().__init__()
        self._job = job
        self._ffmpeg_dir = ffmpeg_dir
        self._preview_image_path = preview_image_path
        self._preview_width = preview_width
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
                preview_image_path=self._preview_image_path,
                preview_width=self._preview_width,
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


class _ExportMonitorView(QLabel):
    """导出预览画面（仿 N3 出力预览）：保持纵横比缩放显示最近合成帧。

    无帧时显示占位文案；有帧后 resize 会用原图重新缩放，避免累积模糊。
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._frame: Optional[QPixmap] = None
        self.setMinimumSize(1, 1)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        themed(
            self,
            lambda: (
                f"background: {stage_bg()}; border-radius: 8px;"
                f" color: {palette().text_hint}; font-size: 10pt;"
            ),
        )
        self.clear_frame()

    def set_frame(self, image: QImage) -> None:
        self._frame = QPixmap.fromImage(image)
        self.setText("")
        self._rescale()

    def clear_frame(self) -> None:
        self._frame = None
        self.setPixmap(QPixmap())
        self.setText("准备开始导出")

    def _rescale(self) -> None:
        if self._frame is None or self._frame.isNull():
            return
        self.setPixmap(
            _scaled_preview_pixmap(
                self._frame,
                self.size(),
                float(self.devicePixelRatioF()),
            )
        )

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._rescale()


def _format_eta_seconds(seconds: float) -> str:
    """导出剩余时间的短文案：1 小时 5 分 / 3 分 20 秒 / 45 秒。"""
    total = max(int(round(seconds)), 0)
    if total >= 3600:
        return f"{total // 3600} 小时 {total % 3600 // 60} 分"
    if total >= 60:
        return f"{total // 60} 分 {total % 60} 秒"
    return f"{total} 秒"


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
        self._title_source_active = False
        """左侧列表当前是否显示末位的特殊「标题」源。"""
        self._subtitle_path: Optional[Path] = None
        self._video_path: Optional[Path] = None
        self._video_info: Optional[MediaInfo] = None
        self._background_source: Optional[BackgroundSource] = None
        self._audio_menu_actions: list[Action] = []
        self._audio_path: Optional[Path] = None
        self._audio_info: Optional[MediaInfo] = None
        self._style: Style = Style()
        self._style_presets: dict[str, StylePreset] = {}
        self._screen_settings: ScreenSettings = ScreenSettings()
        self._selected_scheme_key = "global"
        self._project_path: Optional[Path] = None
        self._project_dirty = False
        self._loading_project = False
        self._syncing_screen_controls = False
        self._render_thread: Optional[QThread] = None
        self._render_worker: Optional[_RenderWorker] = None
        self._suppress_next_render_command_log = False
        # 左右余白检查：属性面板每个 SpinBox tick 都会触发样式变更，
        # 用单发定时器合并成一次检查，提示只在结果变化时弹出。
        self._margin_check_timer = QTimer(self)
        self._margin_check_timer.setSingleShot(True)
        self._margin_check_timer.setInterval(400)
        self._margin_check_timer.timeout.connect(self._check_layout_margins)
        self._last_margin_warning_key = ""
        # 歌词 / 属性面板分割比例：默认 4:6，用户拖动后记忆。拖动过程中
        # splitterMoved 连续触发，用单发定时器合并成一次落盘。
        self._preview_splitter_ratio = 0.4
        self._splitter_save_timer = QTimer(self)
        self._splitter_save_timer.setSingleShot(True)
        self._splitter_save_timer.setInterval(400)
        self._splitter_save_timer.timeout.connect(self._save_persisted_state)
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

        self._background_menu_btn = DropDownPushButton("背景与音频")
        self._background_menu_btn.setFixedHeight(30)
        background_menu = RoundMenu(parent=self._background_menu_btn)
        background_menu.addAction(Action("背景视频…", triggered=self._browse_video))
        background_menu.addAction(Action("静态图片…", triggered=self._browse_background_image))
        background_menu.addAction(Action("图片序列首帧…", triggered=self._browse_background_sequence))
        background_menu.addAction(Action("纯色背景…", triggered=self._choose_solid_background))
        background_menu.addSeparator()
        audio_action = Action("独立音频…", triggered=self._browse_audio)
        background_menu.addAction(audio_action)
        self._audio_menu_actions.append(audio_action)
        self._background_menu_btn.setMenu(background_menu)
        layout.addWidget(self._background_menu_btn)

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
            background=background_payload(
                kind=self._background_source.kind,
                path=Path(self._background_source.path) if self._background_source.path else None,
                color=self._background_source.color,
                source_fps=self._background_source.source_fps,
                sequence_start_number=self._background_source.sequence_start_number,
                video_offset_ms=self._background_source.video_offset_ms,
            ) if self._background_source is not None else None,
            style=style_to_dict(self._style),
            screen=screen_settings_to_dict(self._screen_settings),
            selected_scheme_key=self._selected_scheme_key,
            line_layout_indices=line_layout_indices,
            line_breaks_before=line_breaks_before,
            char_role_labels=char_role_labels,
            line_display_overrides=line_display_overrides,
            line_animation_overrides=line_animation_overrides,
            extra_subtitle_sources=extra_subtitle_sources,
            project_role_names=self._property_panel.role_names,
            output=project_output_payload(
                encoder_mode=str(self._export_encoder_combo.currentData() or ENCODER_CPU),
                crf=self._export_crf_spin.value(),
                preset=str(self._export_preset_combo.currentData() or "medium"),
                codec=self._export_codec_value(),
                output_path=self._export_output_text(),
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
        # 项目内容整体替换，旧的样式/轨道撤销记录全部失效
        self._clear_undo_history()
        # 1) 样式 / 屏幕 / 配色方案
        self._style, _font_names_changed = normalize_style_font_families(
            style_from_dict(data.get("style")), get_n3_font_catalog()
        )
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
        background = data.get("background") if isinstance(data.get("background"), dict) else None
        if background is not None:
            self._load_background_payload(background)
        elif paths["video_path"] is not None and paths["video_path"].is_file():
            self.load_video(paths["video_path"])
        audio = paths["audio_path"]
        if audio is not None and audio.is_file() and audio != self._video_path:
            self.load_audio(audio)
        # Project/N3 role payloads are authoritative.  Populate missing role
        # schemes only after those payloads have replaced source-LRC markers;
        # otherwise a transient ``【アクア】`` marker can auto-create an unrelated
        # palette before FontIndex=0 clears it back to the global N3 scheme.
        content_roles = self._content_role_options()
        project_roles = data.get("project_role_names")
        if isinstance(project_roles, list):
            seen = set(content_roles)
            for value in project_roles:
                name = str(value or "").strip()
                if (
                    name
                    and name != TITLE_SCHEME_NAME
                    and name in self._style.custom_style_schemes
                    and name not in seen
                ):
                    seen.add(name)
                    content_roles.append(name)
        self._property_panel.set_roles(content_roles)
        self._lyrics_panel.set_role_options(self._merged_role_options())

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
        codec = output.get("codec")
        if isinstance(codec, str):
            c_idx = self._export_codec_combo.findData(codec)
            if c_idx >= 0:
                self._export_codec_combo.setCurrentIndex(c_idx)
        out_path = output.get("output_path")
        if isinstance(out_path, str) and out_path.strip():
            path = Path(out_path.strip())
            self._export_dir_edit.setText(str(path.parent))
            self._export_name_edit.setText(path.stem)
        blocked = self._export_native_check.blockSignals(True)
        try:
            self._export_native_check.setChecked(False)
        finally:
            self._export_native_check.blockSignals(blocked)

    def _confirm_discard_changes(self) -> bool:
        """有未保存改动时弹确认；返回 True 表示可以继续（已处理）。"""
        if not self._project_dirty:
            return True
        choice = fluent_choice(
            self,
            "未保存的改动",
            "当前项目有未保存的改动，是否先保存？",
            ["保存", "放弃", "取消"],
            default=2,
        )
        if choice not in (0, 1):
            return False
        if choice == 0:
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
            self._title_source_active = False
            self._clear_undo_history()
            self._subtitle_path = None
            self._property_panel.set_n3_template_lyrics_directory(None)
            self._video_path = None
            self._video_info = None
            self._background_source = None
            self._audio_path = None
            self._audio_info = None
            self._sync_audio_action_enabled()
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
        self._open_project_path(Path(path_str), confirm_discard=False)

    def _open_project_path(
        self,
        path: Path,
        *,
        confirm_discard: bool = True,
    ) -> bool:
        """Open a ``.yurika`` project selected from the menu or dropped."""
        if confirm_discard and not self._confirm_discard_changes():
            return False
        try:
            data = load_render_project(path)
        except (OSError, ValueError) as exc:
            fluent_error(self, "打开项目失败", f"无法读取项目文件：\n{path}\n\n{exc}")
            return False
        missing_resources = self._missing_project_resources(data)
        self._clear_loaded_media()
        self._apply_project_data(data)
        self._project_path = path
        self._project_dirty = False
        self._refresh_project_title()
        if missing_resources:
            fluent_warning(
                self,
                "项目已打开，但部分素材未找到",
                "以下素材路径无效，已跳过加载：\n\n"
                + "\n".join(
                    f"• {label}：{path}" for label, path in missing_resources
                ),
                copyable=True,
            )
        return True

    @staticmethod
    def _missing_project_resources(data: dict) -> list[tuple[str, Path]]:
        """Collect missing project assets without blocking project loading."""
        missing: list[tuple[str, Path]] = []
        seen: set[str] = set()

        def add(label: str, path: Optional[Path], *, exists: Optional[bool] = None) -> None:
            if path is None:
                return
            key = str(path)
            if key in seen or (path.is_file() if exists is None else exists):
                return
            seen.add(key)
            missing.append((label, path))

        paths = split_project_paths(data)
        add("主字幕", paths["subtitle_path"])

        background = (
            data.get("background") if isinstance(data.get("background"), dict) else None
        )
        if background is not None:
            kind = str(background.get("kind") or "solid")
            raw_path = str(background.get("path") or "").strip()
            path = Path(raw_path) if raw_path else None
            if kind == "video":
                add("背景视频", path)
            elif kind == "image":
                add("背景图片", path)
            elif kind == "image_sequence" and path is not None:
                try:
                    sequence_start = max(
                        int(background.get("sequence_start_number") or 0), 0
                    )
                except (TypeError, ValueError):
                    sequence_start = 0
                source = BackgroundSource(
                    kind="image_sequence",
                    path=str(path),
                    sequence_start_number=sequence_start,
                )
                first_frame = background_sequence_frame_path(source, 0)
                add(
                    "背景图片序列",
                    path,
                    exists=first_frame is not None and first_frame.is_file(),
                )
        else:
            add("背景视频", paths["video_path"])

        add("独立音频", paths["audio_path"])

        extras = data.get("extra_subtitle_sources")
        if isinstance(extras, list):
            for index, item in enumerate(extras, start=1):
                if not isinstance(item, dict):
                    continue
                path_text = str(item.get("path") or "").strip()
                if not path_text:
                    continue
                name = str(item.get("name") or "").strip() or str(index)
                add(f"副字幕「{name}」", Path(path_text))
        return missing

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
            fluent_error(
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
            fluent_info(
                self,
                "导入完成（部分设置需注意）",
                "已导入 N3 项目，以下内容请检查：\n\n"
                + "\n".join(f"• {warning}" for warning in result.warnings),
                copyable=True,
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
            fluent_error(self, "保存项目失败", f"无法写入项目文件：\n{path}\n\n{exc}")
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
        self._preview_panel.pathDropped.connect(self._load_dropped_background)
        self._preview_panel.browseRequested.connect(self._browse_background_media)
        self._add_background_empty_actions(self._preview_panel)
        self._transport_bar = self._preview_window.transport_bar

        self._lyrics_panel = LyricsPanel()
        self._lyrics_panel.set_style(self._style)
        self._lyrics_panel.pathDropped.connect(self._load_dropped_subtitle)
        self._lyrics_panel.browseRequested.connect(self._browse_subtitle)
        self._lyrics_panel.roleChanged.connect(self._on_lyrics_role_changed)
        self._lyrics_panel.roleChangeRequested.connect(
            self._on_lyrics_roles_changed
        )
        self._lyrics_panel.charRolesChanged.connect(self._on_lyrics_char_roles_changed)
        self._lyrics_panel.titleEditRequested.connect(
            self._freeze_title_template_for_character_edit
        )
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
        self._property_panel.set_n3_template_target_height(self._screen_settings.height)
        self._property_panel.styleChanged.connect(self._apply_style)
        self._property_panel.presetSchemesChanged.connect(self._apply_style_presets)
        self._property_panel.schemeSelectionChanged.connect(self._on_scheme_selection_changed)
        self._property_panel.layoutAssignAllRequested.connect(self._on_layout_assign_all)
        self._property_panel.layoutAutoAssignRequested.connect(self._on_layout_auto_assign)
        self._property_panel.layoutDeleted.connect(self._on_layout_deleted)
        self._property_panel.set_current_scheme_key(self._selected_scheme_key)
        self._selected_scheme_key = self._property_panel.current_scheme_key()

        self._video_settings_panel = DropPanel(
            extensions={
                ".mp4", ".mkv", ".mov", ".webm", ".avi", ".flv",
                *IMAGE_EXTENSIONS,
                PROJECT_FILE_SUFFIX,
            },
            empty_title="拖入背景素材",
            empty_hint="拖入视频、静态图片或 Yurika 工程（.yurika）\n图片序列与纯色请用下方按钮",
            empty_icon="🎬",
        )
        self._video_settings_panel.pathDropped.connect(self._load_dropped_background)
        self._video_settings_panel.browseRequested.connect(self._browse_background_media)
        self._add_background_empty_actions(self._video_settings_panel)
        self._video_settings_panel.set_content(self._property_panel)
        top.addWidget(self._video_settings_panel)

        # 不设 stretch factor：QSplitter 默认按当前尺寸比例分配新增空间，
        # 窗口缩放时能保持用户拖出的比例。传大数值让 setSizes 按比例缩放
        # 到实际宽度（面板各自的最小宽仍然优先）。
        ratio = self._preview_splitter_ratio
        top.setSizes([round(ratio * 10_000), round((1.0 - ratio) * 10_000)])
        top.splitterMoved.connect(self._on_preview_splitter_moved)
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
            # 撤销/重做：样式（字体/布局等）与字幕轨道编辑（Ctrl+Z / Ctrl+Y；
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
        outer = QVBoxLayout(page)
        outer.setContentsMargins(24, 4, 24, 16)
        outer.setSpacing(10)

        # 顶部项目命令栏（同预览页）
        outer.addWidget(self._make_project_bar())

        # 内容列限制最大宽度并水平居中，宽屏下表单不再拉满整行。
        column = QWidget()
        column.setObjectName("SrExportColumn")
        themed(column, lambda: "#SrExportColumn { background: transparent; }")
        column.setMaximumWidth(1200)
        column.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        layout = QVBoxLayout(column)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        center_row = QHBoxLayout()
        center_row.setContentsMargins(0, 0, 0, 0)
        center_row.addStretch(1)
        center_row.addWidget(column)
        center_row.addStretch(1)
        outer.addLayout(center_row, 1)

        # qfluentwidgets 语义标签自行跟随主题；保留实例引用，防止被 GC 移出
        # styleSheetManager 的 WeakKeyDictionary 后主题失效（同 SUG 导出页的教训）。
        self._export_theme_labels: list[QWidget] = []
        # 主体两栏：左·设置卡片列（定宽），右·导出预览（吃掉剩余空间）
        body_row = QHBoxLayout()
        body_row.setContentsMargins(0, 0, 0, 0)
        body_row.setSpacing(16)
        settings_col = QWidget()
        settings_col.setObjectName("SrExportSettingsCol")
        themed(settings_col, lambda: "#SrExportSettingsCol { background: transparent; }")
        settings_col.setFixedWidth(430)
        self._export_settings_col = settings_col
        settings_layout = QVBoxLayout(settings_col)
        settings_layout.setContentsMargins(0, 0, 0, 0)
        settings_layout.setSpacing(12)

        # 卡片 1：输出文件（第一行选文件夹，第二行文件名，扩展名固定 .mp4）
        output_card, output_layout = self._make_export_card("输出文件")
        dir_row = QHBoxLayout()
        dir_row.setContentsMargins(0, 0, 0, 0)
        dir_row.setSpacing(8)
        self._export_dir_edit = FluentLineEdit()
        self._export_dir_edit.setPlaceholderText("选择输出文件夹")
        self._export_browse_button = FluentPushButton(FIF.FOLDER, "浏览")
        self._export_browse_button.clicked.connect(self._browse_export_output)
        dir_row.addWidget(self._export_dir_edit, 1)
        dir_row.addWidget(self._export_browse_button)
        output_layout.addLayout(dir_row)

        name_row = QHBoxLayout()
        name_row.setContentsMargins(0, 0, 0, 0)
        name_row.setSpacing(8)
        self._export_name_edit = FluentLineEdit()
        self._export_name_edit.setPlaceholderText("文件名（默认：视频文件名_yurika出力）")
        name_suffix = CaptionLabel(".mp4")
        self._export_theme_labels.append(name_suffix)
        name_row.addWidget(self._export_name_edit, 1)
        name_row.addWidget(name_suffix)
        output_layout.addLayout(name_row)
        # 最近一次自动生成的文件名——用户没改过就跟随视频切换更新
        self._export_auto_name = ""
        settings_layout.addWidget(output_card)

        # 卡片 2：画面与编码
        params_card, params_layout = self._make_export_card("画面与编码")
        sync_hint = CaptionLabel("宽度 / 高度 / 帧率与预览页的「画面」设置双向联动。")
        self._export_theme_labels.append(sync_hint)
        params_layout.addWidget(sync_hint)

        params_row = QHBoxLayout()
        params_row.setContentsMargins(0, 0, 0, 0)
        params_row.setSpacing(10)
        # 字段上方已有 CaptionLabel 标签，SpinBox 不再重复「宽/高」后缀
        self._export_width_spin = self._export_spin(160, 7680, 1920, "")
        self._export_height_spin = self._export_spin(90, 4320, 1080, "")
        self._export_fps_combo = FluentComboBox()
        self._export_fps_combo.setMinimumHeight(32)
        for fps in SCREEN_FPS_OPTIONS:
            self._export_fps_combo.addItem(f"{fps} fps", userData=fps)
        params_row.addWidget(self._labeled_export_control("宽度", self._export_width_spin))
        params_row.addWidget(self._labeled_export_control("高度", self._export_height_spin))
        params_row.addWidget(self._labeled_export_control("帧率", self._export_fps_combo))
        params_layout.addLayout(params_row)

        encode_row = QHBoxLayout()
        encode_row.setContentsMargins(0, 0, 0, 0)
        encode_row.setSpacing(10)
        self._export_encoder_combo = FluentComboBox()
        self._export_encoder_combo.setMinimumHeight(32)
        self._export_encoder_combo.addItem("CPU 软编", userData=ENCODER_CPU)
        self._export_encoder_combo.addItem("自动硬编", userData=ENCODER_AUTO)
        self._export_encoder_combo.addItem("NVIDIA NVENC", userData=ENCODER_NVENC)
        self._export_encoder_combo.addItem("Intel QSV", userData=ENCODER_QSV)
        self._export_encoder_combo.addItem("AMD AMF", userData=ENCODER_AMF)
        self._export_encoder_combo.currentIndexChanged.connect(
            self._update_export_preset_enabled
        )
        self._export_codec_combo = FluentComboBox()
        self._export_codec_combo.setMinimumHeight(32)
        self._export_codec_combo.addItem("H.264 (AVC)", userData=CODEC_H264)
        self._export_codec_combo.addItem("H.265 (HEVC)", userData=CODEC_HEVC)
        self._export_codec_combo.setToolTip(
            "H.265 同画质体积更小，但编码更慢、老设备兼容性略差。"
        )
        self._export_codec_combo.currentIndexChanged.connect(
            self._refresh_export_format_label
        )
        encode_row.addWidget(self._labeled_export_control("编码器", self._export_encoder_combo))
        encode_row.addWidget(self._labeled_export_control("视频编码", self._export_codec_combo))
        params_layout.addLayout(encode_row)

        quality_row = QHBoxLayout()
        quality_row.setContentsMargins(0, 0, 0, 0)
        quality_row.setSpacing(10)
        self._export_preset_combo = FluentComboBox()
        self._export_preset_combo.setMinimumHeight(32)
        for preset in CPU_PRESETS:
            self._export_preset_combo.addItem(preset, userData=preset)
        self._export_preset_combo.setCurrentText("medium")
        self._export_crf_spin = self._export_spin(0, 51, 18, "")
        self._export_crf_spin.setToolTip("CRF 质量：数值越小画质越高、文件越大；18 约为视觉无损。")
        quality_row.addWidget(self._labeled_export_control("CPU preset", self._export_preset_combo))
        quality_row.addWidget(self._labeled_export_control("质量 (CRF)", self._export_crf_spin))
        params_layout.addLayout(quality_row)
        settings_layout.addWidget(params_card)

        self._export_native_check = CheckBox("实验：使用 native 字幕渲染器导出")
        self._export_native_check.setChecked(False)
        self._export_native_check.setEnabled(False)
        self._export_native_check.setVisible(False)
        self._export_native_check.setToolTip("native 字幕渲染器暂时停用。")
        settings_layout.addWidget(self._export_native_check)
        settings_layout.addStretch(1)

        # 右栏：导出预览（仿 N3 出力预览——边导出边显示 ffmpeg 合成帧）
        monitor_card = SimpleCardWidget()
        self._export_monitor_card = monitor_card
        monitor_layout = QVBoxLayout(monitor_card)
        self._export_monitor_layout = monitor_layout
        monitor_layout.setContentsMargins(20, 14, 20, 16)
        monitor_layout.setSpacing(10)
        monitor_header = QHBoxLayout()
        monitor_header.setContentsMargins(0, 0, 0, 0)
        monitor_title = StrongBodyLabel("导出预览")
        self._export_theme_labels.append(monitor_title)
        self._export_eta_label = CaptionLabel("")
        monitor_header.addWidget(monitor_title)
        monitor_header.addStretch(1)
        monitor_header.addWidget(self._export_eta_label)
        self._export_monitor_header = monitor_header
        monitor_layout.addLayout(monitor_header)
        self._export_monitor_view = _ExportMonitorView()
        self._export_monitor_frame = _AspectRatioBox(
            self._export_monitor_view,
            aspect_ratio=(
                self._export_width_spin.value() / self._export_height_spin.value()
            ),
        )
        self._export_monitor_frame.setMinimumSize(240, 135)
        # 比例容器占用全部可用区域，画面按导出比例尽量吃满卡片宽度。
        monitor_layout.addWidget(self._export_monitor_frame, 1)
        self._export_format_label = CaptionLabel("输出格式: MP4 · H.264 (AVC)")
        monitor_layout.addWidget(self._export_format_label)

        body_row.addWidget(settings_col, 0, Qt.AlignmentFlag.AlignTop)
        body_row.addWidget(monitor_card, 0, Qt.AlignmentFlag.AlignTop)
        body_row.addStretch(1)
        layout.addStretch(1)
        layout.addLayout(body_row)
        layout.addStretch(1)

        # 底部横贯操作区：进度 + 状态 + 开始/停止
        self._export_progress = FluentProgressBar()
        self._export_progress.setRange(0, 1)
        self._export_progress.setValue(0)
        layout.addWidget(self._export_progress)

        self._export_status_label = CaptionLabel("")
        self._export_status_label.setWordWrap(True)
        self._export_status_label.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        layout.addWidget(self._export_status_label)

        action_row = QHBoxLayout()
        action_row.setContentsMargins(0, 0, 0, 0)
        action_row.setSpacing(8)
        self._export_start_button = FluentPrimaryPushButton(FIF.PLAY, "开始导出")
        self._export_start_button.setMinimumHeight(38)
        self._export_start_button.clicked.connect(self._start_render_export)
        self._export_stop_button = FluentPushButton(FIF.CLOSE, "停止导出")
        self._export_stop_button.setMinimumHeight(38)
        self._export_stop_button.setEnabled(False)
        self._export_stop_button.clicked.connect(self._stop_render_export)
        action_row.addWidget(self._export_start_button, 1)
        action_row.addWidget(self._export_stop_button)
        layout.addLayout(action_row)

        # 导出预览轮询：ffmpeg 持续覆盖写预览 JPG，定时读文件 mtime 变化后刷新
        self._export_preview_timer = QTimer(self)
        self._export_preview_timer.setInterval(500)
        self._export_preview_timer.timeout.connect(self._poll_export_preview)
        self._export_preview_dir: Optional[Path] = None
        self._export_preview_file: Optional[Path] = None
        self._export_preview_mtime_ns = 0
        self._export_started_monotonic = 0.0

        self._update_export_preset_enabled()
        return page

    def _make_export_card(self, title_text: str) -> tuple[SimpleCardWidget, QVBoxLayout]:
        card = SimpleCardWidget()
        layout = QVBoxLayout(card)
        layout.setContentsMargins(20, 14, 20, 16)
        layout.setSpacing(10)
        header = StrongBodyLabel(title_text)
        self._export_theme_labels.append(header)
        layout.addWidget(header)
        return card, layout

    def _update_export_preset_enabled(self) -> None:
        # CPU preset 只影响 libx264；「自动硬编」可能回退 CPU，保持可编辑。
        mode = str(self._export_encoder_combo.currentData() or ENCODER_CPU)
        cpu_possible = mode in (ENCODER_CPU, ENCODER_AUTO)
        self._export_preset_combo.setEnabled(cpu_possible)
        self._export_preset_combo.setToolTip(
            "" if cpu_possible else "CPU preset 仅在 CPU / libx264 编码时生效。"
        )

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
        # 工作台全局 QSS 会给裸 QWidget 刷底色，在白色卡片里会显出灰块
        box.setObjectName("SrExportFieldBox")
        themed(box, lambda: "#SrExportFieldBox { background: transparent; }")
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        label = CaptionLabel(label_text)
        self._export_theme_labels.append(label)
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

    def _browse_background_media(self) -> None:
        current = (
            Path(self._background_source.path)
            if self._background_source is not None and self._background_source.path
            else self._video_path
        )
        start_dir = str(current.parent) if current is not None else ""
        path_str, _ = QFileDialog.getOpenFileName(
            self, "选择背景视频或静态图片", start_dir, BACKGROUND_MEDIA_FILTER
        )
        if path_str:
            self._load_dropped_background(Path(path_str))

    def _load_dropped_background(self, path: Path) -> None:
        if path.suffix.lower() == PROJECT_FILE_SUFFIX:
            self._open_project_path(path)
            return
        if path.suffix.lower() in IMAGE_EXTENSIONS:
            self.load_background_image(path)
        else:
            self.load_video(path)

    def _load_dropped_subtitle(self, path: Path) -> None:
        if path.suffix.lower() == PROJECT_FILE_SUFFIX:
            self._open_project_path(path)
            return
        self.load_subtitle_source(path)

    def _add_background_empty_actions(self, panel: DropPanel) -> None:
        panel.add_empty_action("视频", self._browse_video)
        panel.add_empty_action("静态图", self._browse_background_image)
        panel.add_empty_action("图片序列", self._browse_background_sequence)
        panel.add_empty_action("纯色", self._choose_solid_background)

    def _browse_background_image(self) -> None:
        start_dir = str(Path(self._background_source.path).parent) if self._background_source and self._background_source.path else ""
        path_str, _ = QFileDialog.getOpenFileName(self, "选择静态背景图片", start_dir, IMAGE_FILTER)
        if path_str:
            self.load_background_image(Path(path_str))

    def _browse_background_sequence(self) -> None:
        start_dir = str(Path(self._background_source.path).parent) if self._background_source and self._background_source.path else ""
        path_str, _ = QFileDialog.getOpenFileName(self, "选择图片序列首帧", start_dir, IMAGE_FILTER)
        if path_str:
            fps, ok = QInputDialog.getInt(
                self,
                "图片序列帧率",
                "源帧率（每秒图片数）",
                int(self._background_source.source_fps or self._screen_settings.fps)
                if self._background_source is not None else self._screen_settings.fps,
                1,
                240,
            )
            if ok:
                self.load_background_sequence(Path(path_str), fps)

    def _choose_solid_background(self) -> None:
        initial = self._background_source.color if self._background_source else "#000000"
        color = QColorDialog.getColor(initial=QColor(initial), parent=self, title="选择纯色背景")
        if color.isValid():
            self.set_solid_background(color.name())

    def _browse_audio(self) -> None:
        start_dir = str(self._audio_path.parent) if self._audio_path else ""
        path_str, _ = QFileDialog.getOpenFileName(self, "选择独立音频", start_dir, AUDIO_FILTER)
        if path_str:
            self.load_audio(Path(path_str))

    def _browse_export_output(self) -> None:
        start = self._export_dir_edit.text().strip() or str(self._default_export_dir())
        path_str = QFileDialog.getExistingDirectory(self, "选择输出文件夹", start)
        if path_str:
            self._export_dir_edit.setText(path_str)

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
            fluent_error(
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
            fluent_error(
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
            fluent_error(
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
        self._property_panel.set_n3_template_lyrics_directory(
            source_path.parent if source_path is not None else None
        )
        self._active_source_index = 0
        self._title_source_active = False
        # 换字幕源后旧的行索引全部失效
        self._clear_undo_history()
        self._refresh_source_ui()
        self._lyrics_panel.set_track(track)
        if not self._loading_project:
            self._lyrics_panel.set_role_options(self._merged_role_options())
            self._property_panel.set_roles(self._content_role_options())
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
            fluent_warning(self, "背景视频不可用", f"该文件不含视频流：\n{path}")
            return None
        old_video = self._video_path
        had_independent_audio = (
            self._audio_path is not None and self._audio_path != old_video
        )
        if had_independent_audio:
            self._audio_path = None
            self._audio_info = None
        self._video_path = path
        self._video_info = info
        self._background_source = BackgroundSource(kind="video", path=str(path))
        self._preview_panel.set_background_source(self._background_source)
        self._video_settings_panel.set_populated(True)
        self._preview_window.set_media_title(path)
        self._preview_window.show_near_workspace()
        self._prefill_export_output()
        # 视频自带音频 → 喂给 TransportBar 走 QMediaPlayer 播放
        if info.audio_streams > 0:
            self._audio_path = path
            self._audio_info = info
        elif self._audio_path == old_video:
            self._audio_path = None
            self._audio_info = None
        if self._playback is not None:
            # 单播放器：视频（无论是否含音频）整体交给共享 controller（同时出视频 + 音频）。
            self._transport_bar.set_audio_source(path)
        elif info.audio_streams > 0:
            self._transport_bar.set_audio_source(path)
        else:
            self._transport_bar.set_audio_source(None)
        self._sync_audio_action_enabled()
        if had_independent_audio:
            InfoBar.warning(
                title="已移除独立音频",
                content="视频背景只使用内嵌音轨，避免双时钟造成音画不同步。",
                parent=self,
                position=InfoBarPosition.BOTTOM_RIGHT,
                duration=3500,
            )
        self._refresh_transport_duration()
        self._mark_project_dirty()
        return info

    def load_background_image(self, path: Path) -> bool:
        image = QImage(str(path))
        if image.isNull():
            fluent_warning(self, "背景图片不可用", f"无法读取图片：\n{path}")
            return False
        self._set_non_video_background(BackgroundSource(kind="image", path=str(path)))
        return True

    def load_background_sequence(self, first_frame: Path, source_fps: int = 60) -> bool:
        image = QImage(str(first_frame))
        if image.isNull():
            fluent_warning(self, "图片序列不可用", f"无法读取首帧：\n{first_frame}")
            return False
        pattern, start_number = infer_image_sequence_pattern(first_frame)
        if pattern == first_frame:
            fluent_warning(
                self,
                "图片序列命名无效",
                "首帧文件名需要以连续编号结尾，例如 frame_0001.png。",
            )
            return False
        self._set_non_video_background(
            BackgroundSource(
                kind="image_sequence", path=str(pattern), source_fps=max(int(source_fps), 1),
                sequence_start_number=start_number,
            )
        )
        return True

    def set_solid_background(self, color: str) -> None:
        self._set_non_video_background(BackgroundSource(kind="solid", color=color))

    def _set_non_video_background(self, source: BackgroundSource) -> None:
        old_video = self._video_path
        self._video_path = None
        self._video_info = None
        if self._audio_path == old_video:
            self._audio_path = None
            self._audio_info = None
            self._transport_bar.set_audio_source(None)
        self._background_source = source
        self._preview_panel.set_background_source(source)
        self._video_settings_panel.set_populated(True)
        self._sync_audio_action_enabled()
        if source.path:
            self._preview_window.set_media_title(Path(source.path))
        self._preview_window.show_near_workspace()
        self._prefill_export_output()
        self._refresh_transport_duration()
        self._mark_project_dirty()

    def _load_background_payload(self, payload: dict) -> None:
        kind = str(payload.get("kind") or "solid")
        path = Path(str(payload.get("path"))) if payload.get("path") else None
        source = BackgroundSource(
            kind=kind if kind in {"video", "image", "image_sequence", "solid"} else "solid",
            path=str(path) if path is not None else None,
            color=str(payload.get("color") or "#000000"),
            source_fps=(int(payload["source_fps"]) if payload.get("source_fps") else None),
            sequence_start_number=max(int(payload.get("sequence_start_number") or 0), 0),
            video_offset_ms=int(payload.get("video_offset_ms") or 0),
        )
        if kind == "video" and path is not None and path.is_file():
            self.load_video(path)
            self._background_source = source
            self._preview_panel.set_background_source(source)
        elif kind == "image" and path is not None and path.is_file():
            self._set_non_video_background(source)
        elif kind == "image_sequence" and path is not None:
            self._set_non_video_background(source)
        elif kind == "solid":
            self._set_non_video_background(source)

    def load_audio(self, path: Path) -> Optional[MediaInfo]:
        """为图片/图片序列/纯色背景加载独立音轨。

        视频背景严格使用内嵌音轨，避免预览形成两个媒体时钟。
        """
        if self._background_source is not None and self._background_source.kind == "video":
            fluent_warning(
                self,
                "无法添加独立音频",
                "视频背景只使用视频内嵌音轨，以避免双时钟造成音画不同步。",
            )
            return None
        info = self._probe(path, "音频")
        if info is None:
            return None
        if info.audio_streams == 0:
            fluent_warning(self, "音频不可用", f"该文件不含音频流：\n{path}")
            return None
        self._audio_path = path
        self._audio_info = info
        self._transport_bar.set_audio_source(path)
        self._refresh_transport_duration()
        self._mark_project_dirty()
        return info

    def _sync_audio_action_enabled(self) -> None:
        enabled = not (
            self._background_source is not None
            and self._background_source.kind == "video"
        )
        for action in self._audio_menu_actions:
            action.setEnabled(enabled)

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
            fluent_error(self, f"加载{label}失败", str(exc))
            return None
        except Exception as exc:  # noqa: BLE001
            fluent_error(
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
        previous = self._style
        self._style = style
        self._preview_panel.set_style(style)
        self._lyrics_panel.set_style(style)
        # 角色在属性面板中新建 / 重命名 / 删除时，同步逐字符编辑器的可选项。
        self._lyrics_panel.set_role_options(self._merged_role_options())
        if (
            bool(previous.title_overlay and previous.title_overlay.enabled)
            != bool(style.title_overlay and style.title_overlay.enabled)
        ):
            if not (style.title_overlay and style.title_overlay.enabled):
                if self._title_source_active:
                    self._active_source_index = 0
                self._title_source_active = False
            self._refresh_source_ui()
        if self._title_source_active:
            self._refresh_lyrics_panel_source()
        # 提前入场/延迟退场等布局参数会改行显示窗口 → 同步轨道把手数据
        self._refresh_tracks_view_windows()
        self._margin_check_timer.start()
        self._save_persisted_state()
        self._mark_project_dirty()
        # 调用方预先改写过 self._style 的路径（如导出高度重算）不入撤销栈。
        if previous is not style:
            self._record_style_undo(previous, style)

    _STYLE_UNDO_MERGE_WINDOW_S = 1.2
    """同一批字段的连续样式微调（spin 连点 / 文本逐字输入）合并为一条撤销记录。"""

    @staticmethod
    def _style_diff_paths(old: object, new: object, prefix: str = "", depth: int = 3) -> set[str]:
        """两份样式快照的差异路径（如 ``custom_style_schemes.标题.font_size_px``）。

        只下钻 dict（层数受限），列表等按叶子整体比较——签名用于「同一控件的
        连续微调」合并判定，精确到字段即可。
        """
        if not (isinstance(old, dict) and isinstance(new, dict) and depth > 0):
            return {prefix} if old != new else set()
        paths: set[str] = set()
        for key in set(old) | set(new):
            if old.get(key) == new.get(key):
                continue
            child_prefix = f"{prefix}.{key}" if prefix else str(key)
            paths |= SubtitleRenderWindow._style_diff_paths(
                old.get(key), new.get(key), child_prefix, depth - 1
            )
        return paths

    def _record_style_undo(self, previous: Style, current: Style) -> None:
        """字体/布局等属性面板编辑入撤销栈（Ctrl+Z / Ctrl+Y）。"""
        old_payload = style_to_dict(previous)
        new_payload = style_to_dict(current)
        if old_payload == new_payload:
            return
        changed = frozenset(self._style_diff_paths(old_payload, new_payload))
        now = time.monotonic()
        top = self._undo_stack[-1] if self._undo_stack else None
        if (
            top is not None
            and top[0] == "style"
            and top[3] == changed
            and now - top[4] <= self._STYLE_UNDO_MERGE_WINDOW_S
        ):
            # 合并：保留最早的旧值，滚动更新新值与时间戳。
            self._undo_stack[-1] = ("style", top[1], new_payload, changed, now)
        else:
            self._undo_stack.append(("style", old_payload, new_payload, changed, now))
            del self._undo_stack[:-_UNDO_STACK_LIMIT]
        self._redo_stack.clear()

    def _restore_style(self, payload: object) -> bool:
        """把撤销/重做快照套回全局样式（不再录制新的撤销记录）。"""
        if not isinstance(payload, dict):
            return False
        style = style_from_dict(payload)
        self._style = style
        self._property_panel.set_style(style)
        self._preview_panel.set_style(style)
        self._lyrics_panel.set_style(style)
        if not (style.title_overlay and style.title_overlay.enabled):
            if self._title_source_active:
                self._active_source_index = 0
            self._title_source_active = False
        self._refresh_source_ui()
        if self._title_source_active:
            self._refresh_lyrics_panel_source()
        self._refresh_tracks_view_windows()
        self._margin_check_timer.start()
        self._save_persisted_state()
        self._mark_project_dirty()
        return True

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
        self._property_panel.set_roles(self._content_role_options())
        self._preview_panel.set_track(track)

    # ------------------------------------------------------- 副字幕源（N3 多歌词文件）

    def _apply_extra_subtitle_sources(self, payload: object) -> None:
        """从项目快照 / N3 导入恢复副字幕源（含每行布局与逐字角色）。"""
        self._extra_sources = []
        self._active_source_index = 0
        self._title_source_active = False
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
        self._property_panel.set_roles(self._content_role_options())
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

    def _title_source_index(self) -> Optional[int]:
        title = self._style.title_overlay
        if title is None or not title.enabled or self._timing_track is None:
            return None
        return len(self._extra_sources) + 1

    def _refresh_source_ui(self) -> None:
        """刷新歌词面板的字幕源下拉；无主字幕时隐藏。"""
        if self._timing_track is None:
            self._active_source_index = 0
            self._title_source_active = False
            self._lyrics_panel.set_sources([], 0)
            return
        names = ["主字幕"] + [source.name for source in self._extra_sources]
        title_index = self._title_source_index()
        if title_index is not None:
            names.append("标题")
        self._active_source_index = max(
            0, min(self._active_source_index, len(self._extra_sources))
        )
        active_index = title_index if self._title_source_active and title_index is not None else self._active_source_index
        self._lyrics_panel.set_sources(
            names,
            active_index,
            removable_indices=set(range(1, len(self._extra_sources) + 1)),
        )

    def _refresh_lyrics_panel_source(self) -> None:
        """把当前选中源的行喂给歌词列表。"""
        if self._title_source_active:
            title = self._style.title_overlay
            if title is not None and self._timing_track is not None:
                title = replace(
                    title,
                    text_template=_resolve_title_text(title, self._timing_track),
                )
            self._lyrics_panel.set_title(title)
        else:
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
        """Ctrl+Z：撤销最近一次样式（字体/布局等）、轨道时间或逐行特效编辑。"""
        while self._undo_stack:
            command = self._undo_stack.pop()
            if command[0] == "style":
                if self._restore_style(command[1]):
                    self._redo_stack.append(command)
                    return
                continue
            if command[0] == "char_roles":
                _kind, track_index, row, old_labels, _new_labels = command
                if self._restore_char_roles(track_index, row, old_labels):
                    self._redo_stack.append(command)
                    return
                continue
            if command[0] == "char_roles_batch":
                _kind, track_index, rows, old_values, _new_values = command
                if self._restore_char_role_rows(track_index, rows, old_values):
                    self._redo_stack.append(command)
                    return
                continue
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
        """Ctrl+Y / Ctrl+Shift+Z：重做被撤销的样式或字幕轨道编辑。"""
        while self._redo_stack:
            command = self._redo_stack.pop()
            if command[0] == "style":
                if self._restore_style(command[2]):
                    self._undo_stack.append(command)
                    return
                continue
            if command[0] == "char_roles":
                _kind, track_index, row, _old_labels, new_labels = command
                if self._restore_char_roles(track_index, row, new_labels):
                    self._undo_stack.append(command)
                    return
                continue
            if command[0] == "char_roles_batch":
                _kind, track_index, rows, _old_values, new_values = command
                if self._restore_char_role_rows(track_index, rows, new_values):
                    self._undo_stack.append(command)
                    return
                continue
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
        index = max(int(index), 0)
        title_index = self._title_source_index()
        self._title_source_active = title_index is not None and index == title_index
        if not self._title_source_active:
            self._active_source_index = min(index, len(self._extra_sources))
        self._refresh_lyrics_panel_source()

    def _on_source_add_requested(self) -> None:
        if self._timing_track is None:
            fluent_info(self, "先加载主字幕", "请先加载主字幕文件，再添加副字幕源。")
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
            fluent_error(
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
        confirmed = fluent_question(
            self,
            "移除副字幕源",
            f"确定移除副字幕源「{source.name}」？\n（不会删除歌词文件本身）",
            yes_text="移除",
            no_text="取消",
            default_cancel=True,
        )
        if not confirmed:
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
        self._property_panel.set_n3_template_target_height(self._screen_settings.height)
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
        if hasattr(self, "_property_panel"):
            self._property_panel.set_n3_template_target_height(settings.height)

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
        """返回属性面板当前可分配角色，供歌词列表与逐字符编辑器使用。

        预设库只是可复用模板，不代表当前项目里的角色；但用户通过预设库
        “导入为项目角色”的方案已经进入属性面板角色导航，必须在首次分配前
        同步到左侧歌词表格。直接读取 ``role_names`` 还能排除 N3 覆盖后残留的
        旧 LRC 标签，因为 ``set_roles`` 会以当前内容角色重建该列表。
        """
        if hasattr(self, "_property_panel"):
            options = self._property_panel.role_names
            seen = set(options)
            for name in self._content_role_options():
                if name not in seen:
                    seen.add(name)
                    options.append(name)
            return options
        return self._content_role_options()

    def _content_role_options(self) -> list[str]:
        """歌词与标题实际引用的角色名；不混入历史预设。"""
        options: list[str] = []
        seen: set[str] = set()
        for track in self._all_tracks():
            for name in track.role_options:
                if name and name != TITLE_SCHEME_NAME and name not in seen:
                    seen.add(name)
                    options.append(name)
        title = self._style.title_overlay
        if title is not None:
            for row in title.char_role_labels:
                for label in row:
                    name = str(label or "").strip()
                    if name and name != TITLE_SCHEME_NAME and name not in seen:
                        seen.add(name)
                        options.append(name)
        return options

    def _freeze_title_template_for_character_edit(self) -> None:
        """首次逐字编辑前把标题元数据模板展开为当前固定文字。"""
        title = self._style.title_overlay
        if (
            title is None
            or self._timing_track is None
            or ("{title}" not in title.text_template and "{artist}" not in title.text_template)
        ):
            return
        resolved = _resolve_title_text(title, self._timing_track)
        if not resolved:
            InfoBar.warning(
                title="标题为空",
                content="请先在标题页输入文字，再进行逐字符角色分配。",
                parent=self,
                position=InfoBarPosition.BOTTOM_RIGHT,
                duration=3000,
            )
            return
        fixed = replace(
            title,
            text_template=resolved,
            char_role_labels=[[None] * len(line) for line in resolved.split("\n")],
        )
        self._property_panel.set_style(
            replace(self._style, title_overlay=fixed), emit=True
        )
        InfoBar.info(
            title="标题模板已固定",
            content="已按当前歌曲信息展开为固定文字，可逐字符分配角色。",
            parent=self,
            position=InfoBarPosition.BOTTOM_RIGHT,
            duration=2500,
        )

    def _on_lyrics_role_changed(self, row: int, role_name: str) -> None:
        """用户修改了某句歌词的角色时，将角色名写入该行所有字素（当前选中源）。"""
        if self._title_source_active:
            title = self._style.title_overlay
            if title is None:
                return
            lines = title.text_template.split("\n")
            if not 0 <= row < len(lines):
                return
            label = role_name.strip() if role_name else None
            self._set_title_role_labels(row, [label] * len(lines[row]))
            return
        track = self._active_track()
        if track is None:
            return
        if row < 0 or row >= len(track.lines):
            return
        label = role_name.strip() if role_name else None
        self._set_line_role_labels(
            track, row, [label for _ch in track.lines[row].chars]
        )

    def _on_lyrics_roles_changed(self, rows: list[int], role_name: str) -> None:
        """把一个角色方案批量覆盖到所选歌词行，并作为一条命令撤销/重做。"""
        if self._title_source_active:
            return
        track_index = self._active_source_index
        track = self._track_by_index(track_index)
        if track is None:
            return
        valid_rows = tuple(
            sorted(
                {
                    int(row)
                    for row in rows
                    if 0 <= int(row) < len(track.lines)
                    and track.lines[int(row)].chars
                    and not track.lines[int(row)].is_blank
                }
            )
        )
        if not valid_rows:
            return
        label = role_name.strip() if role_name else None
        old_values = tuple(
            tuple(ch.role_label for ch in track.lines[row].chars)
            for row in valid_rows
        )
        new_values = tuple(
            tuple(label for _ch in track.lines[row].chars)
            for row in valid_rows
        )
        if old_values == new_values:
            return
        for row, labels in zip(valid_rows, new_values):
            for ch, value in zip(track.lines[row].chars, labels):
                ch.role_label = value
        if label:
            self._materialize_role_schemes({label})
        self._undo_stack.append(
            ("char_roles_batch", track_index, valid_rows, old_values, new_values)
        )
        del self._undo_stack[:-_UNDO_STACK_LIMIT]
        self._redo_stack.clear()
        self._refresh_after_role_labels_changed(valid_rows)

    def _on_lyrics_char_roles_changed(self, row: int, labels: list) -> None:
        """行内逐字符角色编辑器确定后写回（当前选中源）。"""
        if self._title_source_active:
            self._set_title_role_labels(row, labels)
            return
        track = self._active_track()
        if track is None:
            return
        if row < 0 or row >= len(track.lines):
            return
        line = track.lines[row]
        if len(labels) != len(line.chars):
            return
        normalized = [str(label).strip() or None if label else None for label in labels]
        self._set_line_role_labels(track, row, normalized)

    def _set_title_role_labels(self, row: int, labels: list) -> None:
        """写回标题某行逐字符角色，作为 Style 修改进入统一撤销栈。"""
        title = self._style.title_overlay
        if title is None:
            return
        rows = [list(values) for values in title.char_role_labels]
        lines = title.text_template.split("\n")
        if not 0 <= row < len(lines) or len(labels) != len(lines[row]):
            return
        while len(rows) < len(lines):
            rows.append([None] * len(lines[len(rows)]))
        normalized = [str(label).strip() or None if label else None for label in labels]
        if rows[row] == normalized:
            return
        rows[row] = normalized
        self._materialize_role_schemes({label for label in normalized if label})
        self._property_panel.set_style(
            replace(
                self._style,
                title_overlay=replace(title, char_role_labels=rows),
            ),
            emit=True,
        )

    def _set_line_role_labels(
        self, track: TimingTrack, row: int, labels: list[Optional[str]]
    ) -> None:
        """逐字符写回角色标签：物化方案 + 入撤销栈 + 刷新（整行/逐字共用）。"""
        line = track.lines[row]
        old_labels = tuple(ch.role_label for ch in line.chars)
        new_labels = tuple(labels)
        if new_labels == old_labels:
            return
        for ch, label in zip(line.chars, labels):
            ch.role_label = label
        self._materialize_role_schemes({label for label in labels if label})
        self._undo_stack.append(
            ("char_roles", self._active_source_index, row, old_labels, new_labels)
        )
        del self._undo_stack[:-_UNDO_STACK_LIMIT]
        self._redo_stack.clear()
        self._refresh_after_role_labels_changed(row)

    def _materialize_role_schemes(self, labels: set[str]) -> None:
        """把还没有配色方案的角色名物化进 custom_style_schemes。

        预设库命中的深拷贝预设；全新名字（对话框「＋新建」）交给
        ``set_roles`` → ``_ensure_role_schemes`` 按当前面板值自动建。
        不物化 painter 就解析不到，改了角色毫无视觉变化。
        """
        missing = [label for label in labels if label not in self._style.custom_style_schemes]
        if not missing:
            return
        from_presets = {
            label: deepcopy(self._style_presets[label].scheme)
            for label in missing
            if label in self._style_presets
        }
        if from_presets:
            schemes = dict(self._style.custom_style_schemes)
            schemes.update(from_presets)
            self._style = replace(self._style, custom_style_schemes=schemes)
            self._property_panel.set_style(self._style)
            self._preview_panel.set_style(self._style)
            self._lyrics_panel.set_style(self._style)
            self._save_persisted_state()
        if any(label not in from_presets for label in missing):
            track = self._active_track()
            if track is not None:
                # 触发属性面板为新角色自动建方案（styleChanged 回流 _apply_style）
                self._property_panel.set_roles(
                    self._content_role_options()
                    + [label for label in missing if label not in self._content_role_options()]
                )
                self._lyrics_panel.set_role_options(self._merged_role_options())

    def _refresh_after_role_labels_changed(self, rows: int | tuple[int, ...]) -> None:
        # track 是就地修改的，预览（含异步渲染 worker）不会自己发现——
        # 重新喂一次让当前帧立即按新角色配色重渲染。
        if self._active_source_index == 0 and self._timing_track is not None:
            self._preview_panel.set_track(self._timing_track)
        else:
            self._sync_extra_tracks_to_preview()
        affected_rows = (rows,) if isinstance(rows, int) else rows
        for row in affected_rows:
            self._lyrics_panel.refresh_row_role(row)
        self._mark_project_dirty()

    def _restore_char_roles(
        self, track_index: int, row: int, labels: object
    ) -> bool:
        """撤销/重做：直接写回整行角色标签（不经信号，不再入栈）。"""
        track = self._track_by_index(track_index)
        if (
            track is None
            or not isinstance(labels, tuple)
            or not 0 <= row < len(track.lines)
            or len(labels) != len(track.lines[row].chars)
        ):
            return False
        for ch, label in zip(track.lines[row].chars, labels):
            ch.role_label = label
        if track_index == 0 and self._timing_track is not None:
            self._preview_panel.set_track(self._timing_track)
        else:
            self._sync_extra_tracks_to_preview()
        if track_index == self._active_source_index:
            self._lyrics_panel.refresh_row_role(row)
        self._mark_project_dirty()
        return True

    def _restore_char_role_rows(
        self, track_index: int, rows: object, values: object
    ) -> bool:
        """撤销/重做一次批量整行角色覆盖。"""
        track = self._track_by_index(track_index)
        if (
            track is None
            or not isinstance(rows, tuple)
            or not isinstance(values, tuple)
            or len(rows) != len(values)
        ):
            return False
        for row, labels in zip(rows, values):
            if (
                not isinstance(row, int)
                or not isinstance(labels, tuple)
                or not 0 <= row < len(track.lines)
                or len(labels) != len(track.lines[row].chars)
            ):
                return False
        for row, labels in zip(rows, values):
            for ch, label in zip(track.lines[row].chars, labels):
                ch.role_label = label
        if track_index == 0 and self._timing_track is not None:
            self._preview_panel.set_track(self._timing_track)
        else:
            self._sync_extra_tracks_to_preview()
        if track_index == self._active_source_index:
            for row in rows:
                self._lyrics_panel.refresh_row_role(row)
        self._mark_project_dirty()
        return True

    def _on_lyrics_row_clicked(self, row: int) -> None:
        """点击歌词列表某行 → 预览跳转到该行起始时间（当前选中源）。"""
        if self._title_source_active:
            return
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
        # 应用级旧默认曾错误使用“游明朝 100px / 15px 描边”。只在加载
        # 应用默认时迁移到 N3「情報小」；打开 .yurika / .n3proj 时保留项目
        # 明确选择的标题方案。
        loaded_style = migrate_legacy_app_title_default(
            style_from_dict(data.get("style"))
        )
        catalog = get_n3_font_catalog()
        self._style, style_changed = normalize_style_font_families(
            loaded_style, catalog
        )
        loaded_presets = _style_presets_from_dict(data.get("style_presets"))
        self._style_presets = {}
        presets_changed = False
        for name, preset in loaded_presets.items():
            scheme, changed = normalize_scheme_font_families(preset.scheme, catalog)
            self._style_presets[name] = (
                replace(preset, scheme=scheme) if changed else preset
            )
            presets_changed |= changed
        self._screen_settings = screen_settings_from_dict(data.get("screen"))
        key = data.get("selected_scheme_key")
        if isinstance(key, str) and key:
            self._selected_scheme_key = key
        ratio = data.get("preview_splitter_ratio")
        if isinstance(ratio, (int, float)):
            # 钳到两侧都还能正常操作的区间，坏数据回落默认 4:6
            self._preview_splitter_ratio = min(max(float(ratio), 0.15), 0.85)
        if style_changed or presets_changed:
            self._save_persisted_state()

    def _on_preview_splitter_moved(self, _pos: int, _index: int) -> None:
        sizes = self._preview_splitter.sizes()
        total = sum(sizes)
        if total <= 0:
            return
        self._preview_splitter_ratio = sizes[0] / total
        self._splitter_save_timer.start()

    def _save_persisted_state(self) -> None:
        data = self._load_subtitle_settings()
        data["style"] = style_to_dict(self._style)
        data["style_presets"] = _style_presets_to_dict(self._style_presets)
        data["screen"] = screen_settings_to_dict(self._screen_settings)
        data["selected_scheme_key"] = self._selected_scheme_key
        data["preview_splitter_ratio"] = round(self._preview_splitter_ratio, 4)
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
        width = self._export_width_spin.value()
        height = self._export_height_spin.value()
        self._preview_panel.set_output_size(width, height)
        if hasattr(self, "_export_monitor_frame"):
            self._export_monitor_frame.set_aspect_ratio(width, height)
            self._sync_export_monitor_card_size(width, height)

    def _sync_export_monitor_card_size(self, width: int, height: int) -> None:
        if width <= 0 or height <= 0 or not hasattr(self, "_export_monitor_card"):
            return
        target_height = max(self._export_settings_col.sizeHint().height(), 1)
        margins = self._export_monitor_layout.contentsMargins()
        spacing = max(self._export_monitor_layout.spacing(), 0)
        chrome_height = (
            margins.top()
            + margins.bottom()
            + self._export_monitor_header.sizeHint().height()
            + self._export_format_label.sizeHint().height()
            + spacing * 2
        )
        frame_height = max(target_height - chrome_height, 1)
        frame_width = int(round(frame_height * width / height))
        target_width = max(frame_width + margins.left() + margins.right(), 1)
        self._export_monitor_card.setFixedHeight(target_height)
        self._export_monitor_card.setMaximumWidth(target_width)

    def _export_output_base(self) -> Optional[Path]:
        """默认输出目录 / 文件名的来源素材：视频 > 背景素材 > 字幕文件。"""
        background_path = (
            Path(self._background_source.path)
            if self._background_source is not None and self._background_source.path
            else None
        )
        return self._video_path or background_path or self._subtitle_path

    def _default_export_dir(self) -> Path:
        base = self._export_output_base()
        return base.parent if base is not None else Path.cwd()

    def _default_export_name(self) -> str:
        base = self._export_output_base()
        stem = base.stem if base is not None else "subtitle_render"
        return f"{stem}{DEFAULT_OUTPUT_NAME_SUFFIX}"

    def _normalized_export_name(self) -> str:
        """文件名输入框内容（用户手滑带上 .mp4 时剥掉，扩展名由拼装统一补）。"""
        name = self._export_name_edit.text().strip()
        if name.lower().endswith(".mp4"):
            name = name[:-4].strip()
        return name

    def _export_output_text(self) -> str:
        """当前输出全路径文本；目录或文件名为空时返回空串（存项目用）。"""
        directory = self._export_dir_edit.text().strip()
        name = self._normalized_export_name()
        if not directory or not name:
            return ""
        return str(Path(directory) / f"{name}.mp4")

    def _prefill_export_output(self) -> None:
        """素材就位后预填输出目录 / 文件名；用户自定义过的文件名不覆盖。"""
        if not self._export_dir_edit.text().strip():
            self._export_dir_edit.setText(str(self._default_export_dir()))
        current = self._export_name_edit.text().strip()
        if not current or current == self._export_auto_name:
            name = self._default_export_name()
            self._export_name_edit.setText(name)
            self._export_auto_name = name

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
        if self._background_source is None:
            raise ProcessingError("请先选择背景源。")
        directory = self._export_dir_edit.text().strip()
        if not directory:
            raise ProcessingError("请先选择输出文件夹。")
        name = self._normalized_export_name()
        if not name:
            name = self._default_export_name()
            self._export_name_edit.setText(name)
            self._export_auto_name = name
        output_path = Path(directory).expanduser() / f"{name}.mp4"
        duration_ms = self._current_export_duration_ms()
        return RenderJob(
            track=self._timing_track,
            style=self._style,
            background_video_path=self._video_path,
            background_source=self._background_source,
            audio_path=(
                self._audio_path
                if self._audio_path is not None and self._audio_path != self._video_path
                else None
            ),
            output_path=output_path,
            extra_tracks=tuple(self._extra_track_list()),
            width=self._export_width_spin.value(),
            height=self._export_height_spin.value(),
            fps=self._export_fps_value(),
            duration_ms=duration_ms,
            include_audio=bool(self._audio_info and self._audio_info.audio_streams > 0),
            encoder_mode=str(self._export_encoder_combo.currentData() or ENCODER_CPU),
            crf=self._export_crf_spin.value(),
            preset=str(self._export_preset_combo.currentData() or "medium"),
            codec=self._export_codec_value(),
            native_export_enabled=False,
        )

    def _current_export_duration_ms(self) -> int:
        candidates: list[int] = [track_duration_ms(track) for track in self._all_tracks()]
        if self._video_info is not None and self._video_info.duration > 0:
            candidates.append(int(round(self._video_info.duration * 1000)))
        if self._audio_info is not None and self._audio_info.duration > 0:
            candidates.append(int(round(self._audio_info.duration * 1000)))
        return max(candidates, default=0)

    def _start_render_export(self) -> None:
        if self._render_thread is not None and self._render_thread.isRunning():
            fluent_info(self, "导出中", "当前导出任务还在处理中，请稍等。")
            return
        try:
            job = self._build_render_job()
        except ProcessingError as exc:
            fluent_error(self, "无法导出", str(exc))
            return

        self._export_start_button.setEnabled(False)
        self._export_stop_button.setEnabled(True)
        self._export_progress.setPaused(False)
        self._export_progress.setError(False)
        self._export_progress.setRange(0, 0)
        self._export_status_label.setText("正在准备导出…")

        # 导出预览：临时目录承接 ffmpeg 持续覆盖写入的合成帧
        self._cleanup_export_preview_dir()
        try:
            self._export_preview_dir = Path(tempfile.mkdtemp(prefix="krok_export_preview_"))
            self._export_preview_file = self._export_preview_dir / "frame.jpg"
        except OSError:
            self._export_preview_dir = None
            self._export_preview_file = None
        self._export_preview_mtime_ns = 0
        self._export_monitor_view.clear_frame()
        self._export_eta_label.setText("正在准备…")
        self._export_format_label.setText(self._export_format_text(job))
        self._export_started_monotonic = time.monotonic()
        self._export_preview_timer.start()

        thread = QThread(self)
        preview_width = _export_preview_width(
            self._export_monitor_view.size(),
            float(self._export_monitor_view.devicePixelRatioF()),
            job.width,
            job.height,
        )
        worker = _RenderWorker(
            job,
            self._resolve_ffmpeg_dir(),
            self._export_preview_file,
            preview_width,
        )
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
        confirmed = fluent_question(
            self,
            "停止导出",
            "确定要停止当前导出吗？\n未完成文件将被清理。",
            yes_text="停止导出",
            no_text="继续导出",
            default_cancel=True,
        )
        if not confirmed:
            return
        self._export_stop_button.setEnabled(False)
        self._export_status_label.setText("正在停止导出…")
        self._render_worker.cancel()

    def _on_render_progress(self, done: int, total: int) -> None:
        self._export_progress.setRange(0, max(total, 1))
        self._export_progress.setValue(done)
        self._export_status_label.setText(f"正在导出… {done}/{total} 帧")
        elapsed = time.monotonic() - self._export_started_monotonic
        if done > 0 and elapsed >= 1.0:
            rate = done / elapsed
            remaining = max(total - done, 0) / max(rate, 1e-6)
            self._export_eta_label.setText(
                f"剩余约 {_format_eta_seconds(remaining)} · {rate:.0f} 帧/秒"
            )

    def _on_render_log(self, message: str) -> None:
        if message == "执行命令:":
            self._suppress_next_render_command_log = True
            return
        if self._suppress_next_render_command_log:
            self._suppress_next_render_command_log = False
            return
        self._export_status_label.setText(message)

    def _export_codec_value(self) -> str:
        return str(self._export_codec_combo.currentData() or CODEC_H264)

    @staticmethod
    def _codec_display(codec: str) -> str:
        return "H.265 (HEVC)" if codec == CODEC_HEVC else "H.264 (AVC)"

    def _refresh_export_format_label(self) -> None:
        # 导出进行中标签由 _export_format_text 的完整信息占据，不在此覆盖
        if not self._export_start_button.isEnabled():
            return
        self._export_format_label.setText(
            f"输出格式: MP4 · {self._codec_display(self._export_codec_value())}"
        )

    def _export_format_text(self, job: RenderJob) -> str:
        text = (
            f"输出格式: MP4 · {self._codec_display(job.codec)}"
            f" · {job.width}×{job.height} @ {job.fps}fps"
        )
        try:
            parent = job.output_path.parent
            probe = parent if parent.exists() else Path(job.output_path.anchor or ".")
            free_gb = shutil.disk_usage(probe).free / 1024**3
            text += f" · 磁盘可用 {free_gb:.0f} GB"
        except OSError:
            pass
        return text

    def _poll_export_preview(self) -> None:
        path = self._export_preview_file
        if path is None:
            return
        try:
            mtime_ns = path.stat().st_mtime_ns
        except OSError:
            return
        if mtime_ns == self._export_preview_mtime_ns:
            return
        image = QImage(str(path))
        if image.isNull():
            return  # 极少数情况下文件尚未写完，下个周期再试
        self._export_preview_mtime_ns = mtime_ns
        self._export_monitor_view.set_frame(image)

    def _stop_export_preview_polling(self) -> None:
        self._export_preview_timer.stop()
        self._poll_export_preview()  # 收尾再读一次，保住最后写入的帧

    def _cleanup_export_preview_dir(self) -> None:
        directory = self._export_preview_dir
        self._export_preview_dir = None
        self._export_preview_file = None
        if directory is not None:
            shutil.rmtree(directory, ignore_errors=True)

    def _finish_render_success(self, output_path: Path) -> None:
        self._stop_export_preview_polling()
        self._export_eta_label.setText("已完成")
        self._export_progress.setRange(0, 1)
        self._export_progress.setValue(1)
        self._export_status_label.setText(f"导出完成: {output_path}")
        self._export_start_button.setEnabled(True)
        self._export_stop_button.setEnabled(False)
        bar = InfoBar.success(
            title="导出完成",
            content=output_path.name,
            parent=self,
            position=InfoBarPosition.BOTTOM_RIGHT,
            duration=6000,
        )
        open_button = FluentPushButton("打开所在文件夹")
        open_button.clicked.connect(
            lambda _=False, p=output_path: self._open_export_folder(p)
        )
        bar.addWidget(open_button)
        context = self._workflow_context
        if context is not None and hasattr(context, "accept_subtitle_video"):
            context.accept_subtitle_video(output_path)

    def _open_export_folder(self, output_path: Path) -> None:
        # Windows 下用资源管理器直接选中导出文件，其余平台退回打开所在目录。
        if sys.platform == "win32" and output_path.exists():
            subprocess.Popen(["explorer", "/select,", str(output_path)])
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(output_path.parent)))

    def _finish_render_cancelled(self, message: str) -> None:
        self._stop_export_preview_polling()
        self._export_eta_label.setText("已停止")
        # 保留已完成的进度并转入「暂停」黄色态；若仍是忙碌态才重置。
        if self._export_progress.maximum() <= 0:
            self._export_progress.setRange(0, 1)
            self._export_progress.setValue(0)
        self._export_progress.setPaused(True)
        self._export_status_label.setText("导出已停止，未完成文件已清理。" if message else "导出已停止。")
        self._export_start_button.setEnabled(True)
        self._export_stop_button.setEnabled(False)

    def _finish_render_failure(self, message: str) -> None:
        self._stop_export_preview_polling()
        self._export_eta_label.setText("")
        if self._export_progress.maximum() <= 0:
            self._export_progress.setRange(0, 1)
            self._export_progress.setValue(0)
        self._export_progress.setError(True)
        self._export_status_label.setText("导出失败")
        self._export_start_button.setEnabled(True)
        self._export_stop_button.setEnabled(False)
        fluent_error(self, "导出失败", message)

    def _clear_render_thread(self) -> None:
        self._render_thread = None
        self._render_worker = None
        self._cleanup_export_preview_dir()

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


def _style_presets_from_dict(payload: object) -> dict[str, StylePreset]:
    """Load grouped presets and migrate the legacy ``name -> scheme`` mapping."""
    if not isinstance(payload, dict):
        return {}
    result: dict[str, StylePreset] = {}
    for raw_name, value in payload.items():
        name = str(raw_name).strip()
        if not name:
            continue
        if isinstance(value, StylePreset):
            result[name] = StylePreset(
                name=name,
                group=str(value.group).strip(),
                scheme=deepcopy(value.scheme),
                source_type=str(value.source_type).strip(),
                source_data=deepcopy(value.source_data),
            )
            continue
        if isinstance(value, SubtitleStyleScheme):
            result[name] = StylePreset(name=name, scheme=deepcopy(value))
            continue
        source_type = ""
        source_data: dict = {}
        if isinstance(value, dict) and isinstance(value.get("scheme"), dict):
            group = str(value.get("group") or "").strip()
            scheme_payload = value["scheme"]
            source_type = str(value.get("source_type") or "").strip()
            if isinstance(value.get("source_data"), dict):
                source_data = deepcopy(value["source_data"])
        else:
            group = ""
            scheme_payload = value
        result[name] = StylePreset(
            name=name,
            group=group,
            scheme=subtitle_style_scheme_from_dict(scheme_payload),
            source_type=source_type,
            source_data=source_data,
        )
    return result


def _style_presets_to_dict(
    presets: dict[str, StylePreset],
) -> dict[str, dict]:
    return {
        str(name): {
            "group": str(preset.group).strip(),
            "scheme": subtitle_style_scheme_to_dict(preset.scheme),
            "source_type": str(preset.source_type).strip(),
            "source_data": deepcopy(preset.source_data),
        }
        for name, preset in presets.items()
        if str(name)
    }
