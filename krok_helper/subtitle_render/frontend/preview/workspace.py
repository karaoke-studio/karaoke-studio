"""Composition view for the subtitle preview workspace."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional

from PyQt6.QtCore import QSize, Qt, pyqtSignal as Signal
from PyQt6.QtWidgets import QSplitter, QVBoxLayout, QWidget
from qfluentwidgets import (
    PushButton as FluentPushButton,
    ToolButton as FluentToolButton,
)

from krok_helper.subtitle_render.domain.models import Style, StylePreset
from krok_helper.subtitle_render.frontend.dialogs.workspace_dialogs import (
    layout_issue_icon,
)
from krok_helper.subtitle_render.frontend.editor.lyrics_list import LyricsPanel
from krok_helper.subtitle_render.frontend.editor.timeline_view import TrackTimelineView
from krok_helper.subtitle_render.frontend.preview.player_window import (
    PreviewPlayerWindow,
)
from krok_helper.subtitle_render.frontend.preview.preview_view import (
    PreviewPanel,
    TransportBar,
)
from krok_helper.subtitle_render.frontend.properties.property_panel import PropertyPanel
from krok_helper.subtitle_render.frontend.widgets.drop_panel import DropPanel


@dataclass(frozen=True)
class PreviewWorkspaceControls:
    """Explicit widget contract consumed by the application coordinator."""

    body_splitter: QSplitter
    workspace_splitter: QSplitter
    preview_window: PreviewPlayerWindow
    preview_panel: PreviewPanel
    transport_bar: TransportBar
    lyrics_panel: LyricsPanel
    property_panel: PropertyPanel
    layout_issues_button: FluentToolButton
    show_preview_button: FluentPushButton
    video_settings_panel: DropPanel
    tracks_view: TrackTimelineView


class PreviewWorkspaceView(QWidget):
    """Own the preview workspace composition without application orchestration."""

    layoutIssuesRequested = Signal()
    previewWindowRequested = Signal()
    backgroundVideoRequested = Signal()
    backgroundImageRequested = Signal()
    backgroundSequenceRequested = Signal()
    solidBackgroundRequested = Signal()

    def __init__(
        self,
        *,
        owner: QWidget,
        style: Style,
        style_presets: Mapping[str, StylePreset],
        output_width: int,
        output_height: int,
        preview_fps: int,
        splitter_ratio: float,
        background_extensions: set[str],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 4, 24, 4)
        outer.setSpacing(4)

        body = QSplitter(Qt.Orientation.Vertical)
        body.setChildrenCollapsible(False)
        workspace = QSplitter(Qt.Orientation.Horizontal)
        workspace.setChildrenCollapsible(False)

        preview_window = PreviewPlayerWindow(owner)
        preview_panel = preview_window.preview_panel
        preview_panel.set_style(style)
        self._add_background_empty_actions(preview_panel)
        transport_bar = preview_window.transport_bar

        lyrics_panel = LyricsPanel()
        lyrics_panel.set_style(style)
        workspace.addWidget(lyrics_panel)

        property_panel = PropertyPanel()
        layout_issues_button = FluentToolButton(
            layout_issue_icon(),
            property_panel,
        )
        layout_issues_button.setFixedWidth(34)
        layout_issues_button.setIconSize(QSize(20, 20))
        layout_issues_button.setToolTip("当前字幕诊断")
        layout_issues_button.setAccessibleName("当前字幕诊断")
        layout_issues_button.setCursor(Qt.CursorShape.PointingHandCursor)
        layout_issues_button.clicked.connect(
            lambda _checked=False: self.layoutIssuesRequested.emit()
        )
        layout_issues_button.hide()

        show_preview_button = FluentPushButton("预览窗口", property_panel)
        show_preview_button.setFixedHeight(30)
        show_preview_button.setToolTip("打开 / 唤起字幕预览窗口")
        show_preview_button.clicked.connect(
            lambda _checked=False: self.previewWindowRequested.emit()
        )
        property_panel.set_navigation_actions(
            [layout_issues_button, show_preview_button]
        )
        property_panel.set_style(style)
        property_panel.set_preset_schemes(dict(style_presets))
        property_panel.set_output_size(output_width, output_height)

        video_settings_panel = DropPanel(
            extensions=set(background_extensions),
            empty_title="拖入背景素材",
            empty_hint=(
                "拖入视频、静态图片、Yurika 工程（.yurika）或 N3 项目（.n3proj）；"
                "图片序列与纯色请用下方按钮，也可在「背景/音频」卡片中选择"
            ),
            empty_icon="🎬",
        )
        self._add_background_empty_actions(video_settings_panel)
        video_settings_panel.set_content(property_panel)
        workspace.addWidget(video_settings_panel)

        ratio = float(splitter_ratio)
        workspace.setSizes(
            [round(ratio * 10_000), round((1.0 - ratio) * 10_000)]
        )
        body.addWidget(workspace)

        tracks_view = TrackTimelineView()
        body.addWidget(tracks_view)
        body.setStretchFactor(0, 5)
        body.setStretchFactor(1, 2)
        body.setSizes([520, 180])
        outer.addWidget(body, 1)

        transport_bar.set_preview_fps(preview_fps)
        self.controls = PreviewWorkspaceControls(
            body_splitter=body,
            workspace_splitter=workspace,
            preview_window=preview_window,
            preview_panel=preview_panel,
            transport_bar=transport_bar,
            lyrics_panel=lyrics_panel,
            property_panel=property_panel,
            layout_issues_button=layout_issues_button,
            show_preview_button=show_preview_button,
            video_settings_panel=video_settings_panel,
            tracks_view=tracks_view,
        )

    def _add_background_empty_actions(self, panel: DropPanel) -> None:
        panel.add_empty_action("视频", self.backgroundVideoRequested.emit)
        panel.add_empty_action("静态图", self.backgroundImageRequested.emit)
        panel.add_empty_action(
            "图片序列",
            self.backgroundSequenceRequested.emit,
        )
        panel.add_empty_action("纯色", self.solidBackgroundRequested.emit)



__all__ = [
    "PreviewWorkspaceControls",
    "PreviewWorkspaceView",
]
