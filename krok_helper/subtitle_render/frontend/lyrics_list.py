"""左侧歌词面板（拖拽 + 单列简洁列表）。

UI 设计：

- **空态**：居中显示"拖入字幕文件 / 点击此处选择"，受 :class:`DropPanel` 接管
- **载入后**：单列 ``QListWidget``，每行只显示歌词内容；不显示行号 / 演唱者标志 /
  演唱者名，也不显示表头——按用户审美统一简化

后续接入 playhead 高亮 / 点击跳转 / 右键演唱者过滤等交互。
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QListWidget,
    QListWidgetItem,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QWidget,
)


class _NoFocusDelegate(QStyledItemDelegate):
    """Item delegate that suppresses the focus rectangle.

    Qt 在选中行上会再画一层 ``State_HasFocus`` 焦点框（虚线/实线，Fusion
    style 下尤其明显）。这层走的是 ``QCommonStyle.PE_FrameFocusRect``，
    CSS ``outline:0`` 在部分平台样式下无法压住，所以在 delegate 层把
    ``State_HasFocus`` flag 摘掉再交父类绘制。
    """

    def paint(self, painter, option, index):  # type: ignore[override]
        opt = QStyleOptionViewItem(option)
        opt.state &= ~QStyle.StateFlag.State_HasFocus
        super().paint(painter, opt, index)

from krok_helper.subtitle_render.frontend.drop_panel import DropPanel
from krok_helper.subtitle_render.models import TimingTrack
from krok_helper.theme_workbench import palette, themed


class LyricsPanel(DropPanel):
    """左侧歌词面板（含空态拖拽 + 已加载列表两态）。"""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(
            extensions={".lrc"},
            empty_title="拖入字幕文件",
            empty_hint="拖入 SUG 导出的 Nicokara 逐字 LRC（.lrc）\n或点击此处选择",
            empty_icon="📝",
            parent=parent,
        )
        self._list = QListWidget()
        self._list.setObjectName("LyricsList")
        self._list.setFrameShape(QListWidget.Shape.NoFrame)
        self._list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._list.setUniformItemSizes(False)
        self._list.setSpacing(0)
        self._list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._list.setItemDelegate(_NoFocusDelegate(self._list))
        themed(
            self._list,
            lambda: (
                f"""
                #LyricsList {{
                    background: transparent;
                    color: {palette().text_primary};
                    font-family: "Microsoft YaHei UI";
                    font-size: 10.5pt;
                    padding: 8px 4px;
                }}
                #LyricsList::item {{
                    padding: 6px 12px;
                    border: 0;
                    outline: 0;
                }}
                #LyricsList::item:selected {{
                    background: {palette().preview_selection_bg};
                    color: {palette().preview_selection_text};
                    border-radius: 4px;
                    outline: 0;
                }}
                #LyricsList::item:selected:focus {{
                    background: {palette().preview_selection_bg};
                    color: {palette().preview_selection_text};
                    border-radius: 4px;
                    outline: 0;
                }}
                #LyricsList::item:hover {{
                    background: {palette().table_row_hover};
                    border-radius: 4px;
                }}
                """
            ),
        )
        self.set_content(self._list)

    # ------------------------------------------------------------------ public

    def set_track(self, track: Optional[TimingTrack]) -> None:
        """加载 / 清空字幕。``None`` / 无行时回到空态。"""
        self._list.clear()
        if track is None or not track.lines:
            self.set_populated(False)
            return
        for line in track.lines:
            text = "".join(c.text for c in line.chars)
            item = QListWidgetItem(text if not line.is_blank else "")
            if line.is_blank:
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            self._list.addItem(item)
        self.set_populated(True)

    @property
    def list_widget(self) -> QListWidget:
        return self._list
