"""宽度自适应网格（需求文档 §3.7 / §14.1）。

可用宽度足够时多列横排（素材+输出双卡、三任务卡），宽度不足时依次退化为单列；
页面统一纵向滚动，不出现嵌套横向滚动条。
"""

from __future__ import annotations

from PyQt6.QtWidgets import QGridLayout, QWidget


class ResponsiveGrid(QWidget):
    """按可用宽度自动调整列数的网格容器。

    列数 = ``max(1, min(max_columns, (width + spacing) // (min_column_width + spacing)))``。
    """

    def __init__(
        self,
        *,
        min_column_width: int,
        max_columns: int,
        spacing: int = 12,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._min_column_width = min_column_width
        self._max_columns = max_columns
        self._spacing = spacing
        self._widgets: list[QWidget] = []
        self._columns = 0

        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setHorizontalSpacing(spacing)
        self._grid.setVerticalSpacing(spacing)

    def set_widgets(self, widgets: list[QWidget]) -> None:
        self._widgets = list(widgets)
        for widget in self._widgets:
            widget.setParent(self)
        self._columns = 0  # 强制重排
        self._relayout()

    def column_count(self) -> int:
        return self._columns

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._relayout()

    def _target_columns(self) -> int:
        width = self.width()
        if width <= 0 or self._min_column_width <= 0:
            return 1
        fit = (width + self._spacing) // (self._min_column_width + self._spacing)
        return int(max(1, min(self._max_columns, len(self._widgets) or 1, fit)))

    def _relayout(self) -> None:
        columns = self._target_columns()
        if columns == self._columns or not self._widgets:
            return
        self._columns = columns
        for widget in self._widgets:
            self._grid.removeWidget(widget)
        # 清掉旧列拉伸，再按新列数均分。
        for column in range(self._grid.columnCount()):
            self._grid.setColumnStretch(column, 0)
        for column in range(columns):
            self._grid.setColumnStretch(column, 1)
        for index, widget in enumerate(self._widgets):
            # 不加 AlignTop：带对齐标志的子件只按自身 sizeHint 取高，同一行的卡片
            # 就会参差不齐。不指定对齐时子件填满单元格，同行卡片自然等高。
            self._grid.addWidget(widget, index // columns, index % columns)
