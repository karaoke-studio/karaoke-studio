"""三种素材区共用的"空态拖拽面板"基类。

每个素材面板有两个状态：

- **empty**：居中显示拖入提示 + 图标，点击 / 拖入都触发加载
- **populated**：显示真实内容（歌词列表 / 预览画面 / 波形）

子类只需要在 ``_init_content`` 里实例化"内容 widget"并塞进 ``content_layout``，
基类负责拖拽接受 / 拒绝 / 点击浏览 / 切页 / 主题刷新。

风格沿用 :class:`krok_helper.gui_qt.DropZoneCard`，但更轻量——本模块的卡片是
"嵌入工作台 / 嵌在 splitter 里"的子面板，不是顶层投放区。
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Optional

from PyQt6.QtCore import Qt, pyqtSignal as Signal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QFrame,
    QLabel,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from krok_helper.subtitle_render.frontend.theme import palette, themed


class DropPanel(QFrame):
    """单文件拖拽接收面板，含 empty / populated 双状态。"""

    pathDropped = Signal(Path)
    """文件被拖入或选中（路径校验通过）时发出。"""

    browseRequested = Signal()
    """点击空态区时发出，宿主用 QFileDialog 选文件，再调 :meth:`set_populated`。"""

    def __init__(
        self,
        *,
        extensions: Iterable[str],
        empty_title: str,
        empty_hint: str,
        empty_icon: str = "",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._extensions = {ext.lower() for ext in extensions}
        self._drag_state: str = "idle"  # idle / accept / reject
        self._populated: bool = False

        self.setObjectName("DropPanel")
        self.setAcceptDrops(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        self._stack = QStackedWidget(self)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._stack)

        # 第 0 页：空态提示
        self._empty_page = QWidget(self._stack)
        empty_layout = QVBoxLayout(self._empty_page)
        empty_layout.setContentsMargins(20, 20, 20, 20)
        empty_layout.setSpacing(8)
        empty_layout.addStretch(1)

        if empty_icon:
            self._icon_label = QLabel(empty_icon, self._empty_page)
            self._icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            icon_font = QFont("Segoe UI Emoji", 28)
            self._icon_label.setFont(icon_font)
            self._icon_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            empty_layout.addWidget(self._icon_label)
        else:
            self._icon_label = None  # type: ignore[assignment]

        self._title_label = QLabel(empty_title, self._empty_page)
        self._title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title_font = QFont("Microsoft YaHei UI", 11)
        title_font.setBold(True)
        self._title_label.setFont(title_font)
        self._title_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        themed(
            self._title_label,
            lambda: f"color: {palette().title_text};",
        )
        empty_layout.addWidget(self._title_label)

        self._hint_label = QLabel(empty_hint, self._empty_page)
        self._hint_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._hint_label.setWordWrap(True)
        self._hint_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        themed(
            self._hint_label,
            lambda: f'color: {palette().text_hint}; font-size: 9pt;',
        )
        empty_layout.addWidget(self._hint_label)
        empty_layout.addStretch(2)

        # 第 1 页：真实内容（由子类 / 调用方塞进来）
        self._content_page = QWidget(self._stack)
        self._content_layout = QVBoxLayout(self._content_page)
        self._content_layout.setContentsMargins(0, 0, 0, 0)
        self._content_layout.setSpacing(0)

        self._stack.addWidget(self._empty_page)
        self._stack.addWidget(self._content_page)
        self._stack.setCurrentIndex(0)

        themed(self, self._panel_qss)

    # ------------------------------------------------------------------ public

    def set_content(self, widget: QWidget) -> None:
        """嵌入真实内容 widget。调用 :meth:`set_populated(True)` 才会切到这一页。"""
        # 清掉之前的
        while self._content_layout.count():
            item = self._content_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
        self._content_layout.addWidget(widget)

    def set_populated(self, populated: bool) -> None:
        """切换到内容页 / 空态页。"""
        self._populated = populated
        self._stack.setCurrentIndex(1 if populated else 0)

    def is_populated(self) -> bool:
        return self._populated

    def accepts(self, path: Path) -> bool:
        try:
            return path.is_file() and path.suffix.lower() in self._extensions
        except OSError:
            return False

    # ------------------------------------------------------------------ events

    def mousePressEvent(self, event):  # noqa: N802 — Qt API
        if event.button() == Qt.MouseButton.LeftButton and not self._populated:
            self.browseRequested.emit()
            event.accept()
            return
        super().mousePressEvent(event)

    def dragEnterEvent(self, event):  # noqa: N802
        urls = event.mimeData().urls()
        if not urls:
            event.ignore()
            return
        path = Path(urls[0].toLocalFile()).expanduser()
        if self.accepts(path):
            self._drag_state = "accept"
            self._apply_panel_style()
            event.acceptProposedAction()
        else:
            self._drag_state = "reject"
            self._apply_panel_style()
            event.ignore()

    def dragLeaveEvent(self, event):  # noqa: N802
        self._drag_state = "idle"
        self._apply_panel_style()
        super().dragLeaveEvent(event)

    def dropEvent(self, event):  # noqa: N802
        urls = event.mimeData().urls()
        self._drag_state = "idle"
        self._apply_panel_style()
        if not urls:
            event.ignore()
            return
        path = Path(urls[0].toLocalFile()).expanduser()
        if not self.accepts(path):
            event.ignore()
            return
        self.pathDropped.emit(path)
        event.acceptProposedAction()

    # ------------------------------------------------------------------ style

    def _apply_panel_style(self) -> None:
        self.setStyleSheet(self._panel_qss())

    def _panel_qss(self) -> str:
        p = palette()
        if self._drag_state == "accept":
            border_color = p.accent_primary
            border_width = 2
            border_style = "dashed"
        elif self._drag_state == "reject":
            border_color = "#E53935"
            border_width = 2
            border_style = "dashed"
        elif not self._populated:
            border_color = p.card_border
            border_width = 1
            border_style = "dashed"
        else:
            border_color = p.card_border
            border_width = 1
            border_style = "solid"
        return (
            f"#DropPanel {{ background-color: {p.card_bg}; "
            f"border: {border_width}px {border_style} {border_color}; "
            f"border-radius: 8px; }}"
        )
