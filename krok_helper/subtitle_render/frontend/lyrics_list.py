"""左侧歌词列表（Sayatoo 风格）。

展示从 Nicokara LRC 解析出的 :class:`TimingTrack`，每行一记录，包含
行号 / S（演唱者切换标志） / 角色（演唱者名） / 内容。

后续会扩展：

- 行高亮跟随预览 playhead
- 点击行 / 字定位 playhead
- 双击行编辑（？）/ 上下文菜单（按演唱者过滤）

当前只做"加载即填充"。
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QBrush, QColor
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QTableWidget,
    QTableWidgetItem,
)

from krok_helper.subtitle_render.models import TimingLine, TimingTrack


class LyricsListWidget(QTableWidget):
    """歌词行列表，仿 Sayatoo 字幕表。"""

    COL_LINE_NO = 0
    COL_SINGER_FLAG = 1
    COL_SINGER_NAME = 2
    COL_CONTENT = 3

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._init_table()

    def _init_table(self) -> None:
        self.setColumnCount(4)
        self.setHorizontalHeaderLabels(["行号", "S", "角色", "内容"])
        self.verticalHeader().setVisible(False)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setAlternatingRowColors(True)

        header = self.horizontalHeader()
        header.setSectionResizeMode(self.COL_LINE_NO, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(self.COL_SINGER_FLAG, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(self.COL_SINGER_NAME, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(self.COL_CONTENT, QHeaderView.ResizeMode.Stretch)

    # ------------------------------------------------------------------ public

    def set_track(self, track: Optional[TimingTrack]) -> None:
        """填充歌词。``None`` / 空 track 时清空列表。"""
        self.setRowCount(0)
        if track is None or not track.lines:
            return
        self.setRowCount(len(track.lines))
        for row, line in enumerate(track.lines):
            self._populate_row(row, line)

    # ------------------------------------------------------------------ helpers

    def _populate_row(self, row: int, line: TimingLine) -> None:
        line_no = QTableWidgetItem(str(row + 1))
        line_no.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

        # S 列：NicokaraExporter 在演唱者切换处插入 【】 标签
        # 故 line.singer_label 非空即代表"切换点"
        flag_text = "S" if line.singer_label else ""
        flag_item = QTableWidgetItem(flag_text)
        flag_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

        singer_item = QTableWidgetItem(line.singer_label or "")

        content_text = "".join(c.text for c in line.chars)
        content_item = QTableWidgetItem(content_text)

        # 空行视觉淡化
        if line.is_blank:
            gray = QBrush(QColor(150, 150, 150))
            for item in (line_no, flag_item, singer_item, content_item):
                item.setForeground(gray)

        self.setItem(row, self.COL_LINE_NO, line_no)
        self.setItem(row, self.COL_SINGER_FLAG, flag_item)
        self.setItem(row, self.COL_SINGER_NAME, singer_item)
        self.setItem(row, self.COL_CONTENT, content_item)
