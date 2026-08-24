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
    QHBoxLayout,
    QLabel,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from krok_helper.subtitle_render.frontend.widgets.theme import palette, themed


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
        self._hovered: bool = False

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
            # 圆形浅色徽章托住图标，空态视觉重心更聚焦
            self._icon_label = QLabel(empty_icon, self._empty_page)
            self._icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._icon_label.setFixedSize(72, 72)
            icon_font = QFont("Segoe UI Emoji", 26)
            self._icon_label.setFont(icon_font)
            self._icon_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            themed(
                self._icon_label,
                lambda: (
                    f"background: {palette().preview_selection_bg}; "
                    "border-radius: 36px;"
                ),
            )
            empty_layout.addWidget(
                self._icon_label, 0, Qt.AlignmentFlag.AlignHCenter
            )
            empty_layout.addSpacing(4)
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

        self._empty_actions = QHBoxLayout()
        self._empty_actions.setSpacing(6)
        self._empty_actions.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        empty_layout.addLayout(self._empty_actions)
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

    def add_empty_action(self, text: str, callback) -> QPushButton:
        """在空态提示下增加一个可直接发现的素材操作按钮。"""
        button = QPushButton(text, self._empty_page)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.clicked.connect(callback)
        themed(
            button,
            lambda: (
                f"QPushButton {{ color: {palette().text_secondary}; "
                f"background: {palette().input_bg}; border: 1px solid {palette().input_border}; "
                "border-radius: 5px; padding: 4px 9px; } "
                f"QPushButton:hover {{ background: {palette().input_hover_bg}; "
                f"border-color: {palette().input_border_focus}; }}"
            ),
        )
        self._empty_actions.addWidget(button)
        return button

    def set_populated(self, populated: bool) -> None:
        """切换到内容页 / 空态页。"""
        self._populated = populated
        self._stack.setCurrentIndex(1 if populated else 0)
        # 空态整块可点击浏览 → 手型；载入内容后恢复箭头，避免误导
        self.setCursor(
            Qt.CursorShape.ArrowCursor
            if populated
            else Qt.CursorShape.PointingHandCursor
        )
        self._apply_panel_style()

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

    def enterEvent(self, event):  # noqa: N802
        self._hovered = True
        if not self._populated:
            self._apply_panel_style()
        super().enterEvent(event)

    def leaveEvent(self, event):  # noqa: N802
        self._hovered = False
        if not self._populated:
            self._apply_panel_style()
        super().leaveEvent(event)

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

    def _panel_style_spec(self) -> tuple[str, int, str, str, int]:
        """返回 (边框色, 边框宽, 边框线型, 背景色, 圆角)。子类可按状态覆写。"""
        p = palette()
        if self._drag_state == "accept":
            return p.accent_primary, 2, "dashed", p.table_row_hover, 8
        if self._drag_state == "reject":
            return "#E53935", 2, "dashed", p.card_bg, 8
        if not self._populated:
            if self._hovered:
                return p.input_border_focus, 1, "dashed", p.input_hover_bg, 8
            return p.input_border, 1, "dashed", p.card_bg, 8
        return p.card_border, 1, "solid", p.card_bg, 8

    def _panel_qss(self) -> str:
        border_color, border_width, border_style, bg, radius = self._panel_style_spec()
        return (
            f"#{self.objectName()} {{ background-color: {bg}; "
            f"border: {border_width}px {border_style} {border_color}; "
            f"border-radius: {radius}px; }}"
        )
