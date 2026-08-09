"""波形对齐页的素材拖放卡片。

原本是 ``gui_qt._build_alignment_page()`` 方法体内的一个 502 行嵌套类 ——
没法单独构造、没法写测试，改一行要在几千行的构建方法里翻。这里原样搬出来，
只把"回调宿主私有方法"换成信号：

* 时长文案变化 → :attr:`AlignmentDropCard.durationTextChanged`，宿主接上
  ``_refresh_alignment_export_panels`` 即可，卡片不再持有宿主引用。
"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QSize, Qt, pyqtSignal as Signal
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import BodyLabel, FluentIcon as FIF, StrongBodyLabel, ToolButton

from krok_helper.ui_kit import CardWidget, build_app_ui_font

__all__ = ["AlignmentDropCard"]


class _DurationLabel(BodyLabel):
    """时长文案标签：文案一变就通知卡片，让导出面板跟着刷新。

    ``QLabel.setText`` 不是虚函数、Qt 侧调用不会走到这里，因此构造时用
    ``QLabel.setText`` 直接写初值，避免构造期就触发一次刷新。
    """

    def __init__(self, card: "AlignmentDropCard", text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._card = card
        QLabel.setText(self, text)

    def setText(self, text: str) -> None:  # noqa: N802
        super().setText(text)
        self._card.durationTextChanged.emit()


class AlignmentDropCard(CardWidget):
    pathChanged = Signal(Path)
    browseRequested = Signal()
    removeRequested = Signal()
    durationTextChanged = Signal()

    def __init__(
        self,
        *,
        title: str,
        media_label: str,
        hint: str,
        extensions: set[str],
        icon: FIF,
        theme: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent, radius=18, padding=(16, 16, 16, 16), spacing=12)
        self.extensions = {ext.lower() for ext in extensions}
        self.path: Path | None = None
        self._hovered = False
        self._drag_state = "idle"
        self._theme = theme
        self._display_mode = "empty"
        self._balanced_height: int | None = None
        self._missing_text = ""
        self._media_label = media_label
        self._icon = icon
        self._default_action_text = "点击选择文件，或直接拖拽进入区域"
        self._empty_detail_text = f"{media_label}: 时长未知"
        self._theme_palette = self._build_theme_palette(theme)

        self.setObjectName("AlignmentDropCard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAcceptDrops(True)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.setMinimumHeight(158)

        layout = self.createVBoxLayout()
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)
        self._main_layout = layout

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(14)

        self.icon_button = ToolButton(self)
        self.icon_button.setIcon(icon.icon())
        self.icon_button.setIconSize(QSize(34, 34))
        self.icon_button.setFixedSize(68, 68)
        self.icon_button.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.icon_button.setStyleSheet("ToolButton { background: transparent; border: 0; padding: 0; }")

        text_layout = QVBoxLayout()
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.setSpacing(4)

        self.title_label = StrongBodyLabel(title)
        self.title_label.setMinimumWidth(0)
        self.title_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.title_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        self.hint_label = BodyLabel(hint)
        self.hint_label.setMinimumWidth(0)
        self.hint_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.hint_label.setWordWrap(True)
        from krok_helper.theme_workbench import palette as _wb_pal, themed as _wb_th
        _wb_th(self.hint_label, lambda: f"color: {_wb_pal().text_secondary};")
        self.hint_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        text_layout.addWidget(self.title_label)
        text_layout.addWidget(self.hint_label)
        header.addWidget(self.icon_button, 0, Qt.AlignmentFlag.AlignTop)
        header.addLayout(text_layout, 1)
        self.header_actions = QHBoxLayout()
        self.header_actions.setContentsMargins(0, 0, 0, 0)
        self.header_actions.setSpacing(8)
        header.addLayout(self.header_actions, 0)
        layout.addLayout(header)

        file_info_row = QHBoxLayout()
        file_info_row.setContentsMargins(0, 0, 0, 0)
        file_info_row.setSpacing(10)

        self.file_name_label = BodyLabel("未选择文件")
        self.file_name_label.setMinimumWidth(0)
        self.file_name_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        from krok_helper.theme_workbench import palette as _wb_pal, themed as _wb_th
        _wb_th(self.file_name_label, lambda: f"color: {_wb_pal().text_primary}; font-weight: 400;")
        self.file_name_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        file_info_row.addWidget(self.file_name_label, 1)
        self.file_state_badge = QLabel("已选择")
        self.file_state_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.file_state_badge.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.file_state_badge.hide()
        file_info_row.addWidget(self.file_state_badge, 0, Qt.AlignmentFlag.AlignVCenter)

        self.ready_duration_label = BodyLabel("")
        self.ready_duration_label.setMinimumWidth(0)
        from krok_helper.theme_workbench import palette as _wb_pal, themed as _wb_th
        _wb_th(self.ready_duration_label, lambda: f"color: {_wb_pal().text_secondary};")
        self.ready_duration_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.ready_duration_label.hide()
        file_info_row.addWidget(self.ready_duration_label, 0, Qt.AlignmentFlag.AlignRight)

        self.detail_label = _DurationLabel(self, self._empty_detail_text, self)
        self.detail_label.setMinimumWidth(0)
        from krok_helper.theme_workbench import palette as _wb_pal, themed as _wb_th
        _wb_th(self.detail_label, lambda: f"color: {_wb_pal().text_secondary};")
        self.detail_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.detail_label.setWordWrap(False)
        self.detail_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        file_info_row.addWidget(self.detail_label, 0, Qt.AlignmentFlag.AlignRight)
        layout.addLayout(file_info_row)

        self.action_frame = QFrame(self)
        self.action_frame.setObjectName("AlignmentDropAction")
        action_layout = QHBoxLayout(self.action_frame)
        action_layout.setContentsMargins(16, 12, 16, 12)
        action_layout.setSpacing(10)

        self.action_icon = ToolButton(self.action_frame)
        self.action_icon.setIcon(FIF.UP.icon())
        self.action_icon.setIconSize(QSize(20, 20))
        self.action_icon.setFixedSize(28, 28)
        self.action_icon.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.action_icon.setStyleSheet("ToolButton { background: transparent; border: 0; padding: 0; }")

        self.action_label = BodyLabel(self._default_action_text)
        self.action_label.setCursor(Qt.CursorShape.PointingHandCursor)
        self.action_label.setMinimumHeight(42)
        self.action_label.setStyleSheet("font-weight: 400;")
        self.action_label.mousePressEvent = lambda _event: self.browseRequested.emit()

        action_layout.addStretch(1)
        action_layout.addWidget(self.action_icon)
        action_layout.addWidget(self.action_label)
        action_layout.addStretch(1)
        layout.addWidget(self.action_frame)

        self.replace_button = QPushButton("更换")
        self.replace_button.clicked.connect(self.browseRequested.emit)
        self.remove_button = QPushButton("移除")
        self.remove_button.clicked.connect(self.removeRequested.emit)
        for button in (self.replace_button, self.remove_button):
            button.setMinimumHeight(34)
            button.setMinimumWidth(76)
            button.hide()
            self.header_actions.addWidget(button)
        self.file_name_label.setText("未选择文件")
        self.detail_label.setText(self._empty_detail_text)
        # 下面这些 Fluent 子控件的 QSS 全部由 ``_refresh_style_modern``
        # 自绘。若继续留在 qfluentwidgets 的 styleSheetManager 里，本页
        # 隐藏期间被打上的 ``dirty-qss`` 会在首次 paint 时把自绘样式抹回
        # Fluent 默认（标题变黑、图标底色丢失），直到鼠标移入重跑一次
        # ``_refresh_style`` 才恢复 —— 注销托管即可根治。
        from krok_helper.theme_workbench import detach_fluent_qss
        detach_fluent_qss(
            self.icon_button,
            self.action_icon,
            self.action_label,
            self.title_label,
            self.hint_label,
            self.file_name_label,
            self.ready_duration_label,
            self.detail_label,
        )
        self._refresh_style()
        # 主题切换：重算 variant×is_dark palette + 重 apply QSS。
        from krok_helper.theme_workbench import theme as _wb_theme
        _wb_theme.changed.connect(self._on_theme_changed)

    def _on_theme_changed(self) -> None:
        # 延迟到下个 event loop iter，避开 SUG ``_refresh_all_widgets``
        # 同步链上的重入窗口（参见 KrokHelperQtApp._on_theme_changed
        # 的同名 docstring）。
        from krok_helper.theme_workbench import schedule_theme_refresh
        schedule_theme_refresh(self, self._apply_theme_refresh)

    def _apply_theme_refresh(self) -> None:
        try:
            self._theme_palette = self._build_theme_palette(self._theme)
            self._refresh_style()
        except RuntimeError:
            pass

    def _build_theme_palette(self, variant: str):
        """获取功能配色（blue/red）× 当前主题 (light/dark) 的 palette。

        返回 :class:`DropCardPalette` —— 原代码用 dict 访问
        ``palette["accent"]``，下面 ``_refresh_style_modern`` 已有
        兜底 ``__getitem__``-like 适配（仍用 ``palette["x"]`` 形式，
        因为 dataclass 不支持下标，所以包一层 dict 视图）。
        """
        from krok_helper.theme_workbench import drop_card_palette
        dcp = drop_card_palette(variant)
        # 把 dataclass 转成 dict，原 ``palette["x"]`` 访问无需改写。
        return {
            "accent": dcp.accent,
            "accent_border": dcp.accent_border,
            "icon_background": dcp.icon_background,
            "action_background": dcp.action_background,
            "hover_background": dcp.hover_background,
            "selected_background": dcp.selected_background,
            "selected_icon_background": dcp.selected_icon_background,
            "selected_action_background": dcp.selected_action_background,
        }

    def accepts(self, path: Path) -> bool:
        return path.is_file() and path.suffix.lower() in self.extensions

    def set_path(self, path: Path) -> None:
        self.path = path
        self.file_name_label.setText(path.name)
        self._drag_state = "idle"
        self._refresh_style()

    def clear_path(self) -> None:
        self.path = None
        self.file_name_label.setText("未选择文件")
        self.detail_label.setText(self._empty_detail_text)
        self._drag_state = "idle"
        self._refresh_style()

    def set_display_mode(self, mode: str, *, missing_text: str = "") -> None:
        self._display_mode = mode if mode in {"empty", "ready", "chip"} else "empty"
        self._missing_text = missing_text
        self._refresh_style()

    def set_balanced_height(self, height: int | None) -> None:
        self._balanced_height = height
        self._refresh_style()

    def enterEvent(self, event) -> None:  # noqa: N802
        self._hovered = True
        self._refresh_style()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hovered = False
        self._refresh_style()
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.browseRequested.emit()
            event.accept()
            return
        super().mousePressEvent(event)

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        urls = event.mimeData().urls()
        if not urls:
            self._drag_state = "reject"
            self._refresh_style()
            event.ignore()
            return
        path = Path(urls[0].toLocalFile()).expanduser()
        if self.accepts(path):
            self._drag_state = "accept"
            self._refresh_style()
            event.acceptProposedAction()
            return
        self._drag_state = "reject"
        self._refresh_style()
        event.ignore()

    def dragLeaveEvent(self, event) -> None:  # noqa: N802
        self._drag_state = "idle"
        self._refresh_style()
        super().dragLeaveEvent(event)

    def dropEvent(self, event) -> None:  # noqa: N802
        urls = event.mimeData().urls()
        if not urls:
            self._drag_state = "idle"
            self._refresh_style()
            event.ignore()
            return
        path = Path(urls[0].toLocalFile()).expanduser()
        if not self.accepts(path):
            self._drag_state = "reject"
            self._refresh_style()
            event.ignore()
            return
        self.set_path(path)
        self.pathChanged.emit(path)
        event.acceptProposedAction()

    def _refresh_style(self) -> None:
        self._refresh_style_modern()
        return

    def _refresh_style_modern(self) -> None:
        palette = self._theme_palette
        is_selected = self.path is not None
        is_chip = self._display_mode == "chip" and is_selected
        is_ready = self._display_mode == "ready" and is_selected
        if not is_selected:
            self.file_name_label.setText("未选择文件")
        from krok_helper.theme_workbench import palette as _wb_pal_state
        _p_state = _wb_pal_state()
        if self._drag_state == "accept":
            background = _p_state.card_bg
            border = palette["accent"]
            accent = palette["accent"]
            border_width = 2
            action_background = palette["action_background"]
            action_border = palette["accent_border"]
            action_text = "松开鼠标即可导入这个文件"
        elif self._drag_state == "reject":
            if _p_state.is_dark:
                background = "#3A1A1A"
                border = accent = "#FF7A8C"
                action_background = "#2D1518"
                action_border = "#5A3A40"
            else:
                background = "#fff1f2"
                border = accent = "#ff4d5e"
                action_background = "#fff5f6"
                action_border = "#ffc7d0"
            border_width = 2
            action_text = "文件类型不支持，请重新选择"
        elif is_selected:
            background = palette["selected_background"]
            border = palette["accent"]
            accent = palette["accent"]
            border_width = 2
            action_background = palette["selected_action_background"]
            action_border = palette["accent_border"]
            action_text = "点击更换文件，或拖入新文件覆盖"
        elif self._hovered:
            background = palette["hover_background"]
            border = palette["accent_border"]
            accent = palette["accent"]
            border_width = 1
            action_background = palette["action_background"]
            action_border = palette["accent_border"]
            action_text = self._default_action_text
        else:
            background = _p_state.card_bg
            border = _p_state.card_border
            accent = palette["accent"]
            border_width = 1
            action_background = palette["action_background"]
            action_border = palette["accent_border"] if _p_state.is_dark else ("#E7EEF8" if self._theme == "blue" else "#F2E8EB")
            action_text = self._default_action_text

        if is_chip:
            background = palette["selected_background"]
            border = palette["accent_border"]
            border_width = 1
            action_text = "点击更换"
        elif is_ready:
            background = palette["selected_background"]
            border = palette["accent"]
            border_width = 1
            action_text = "点击更换文件，或拖入新文件覆盖"

        compact = is_chip or is_ready
        if is_chip:
            self._main_layout.setContentsMargins(14, 8, 14, 8)
            self._main_layout.setSpacing(0)
        else:
            self._main_layout.setContentsMargins(16, 16, 16, 16)
            self._main_layout.setSpacing(8 if is_ready else 12)
        self.setMinimumHeight(54 if is_chip else 158)
        self.setMaximumHeight(64 if is_chip else 16777215)
        if self._balanced_height is not None:
            self.setMinimumHeight(self._balanced_height)
            self.setMaximumHeight(self._balanced_height)
        self.icon_button.setFixedSize(28 if is_chip else (34 if is_ready else 68), 28 if is_chip else (34 if is_ready else 68))
        self.icon_button.setIconSize(QSize(16 if is_chip else (18 if is_ready else 34), 16 if is_chip else (18 if is_ready else 34)))
        self.hint_label.setVisible(not is_selected)
        self.file_name_label.setVisible(not is_chip)
        self.detail_label.setVisible(False)
        clean_duration = self.detail_label.text().split(": ", 1)[-1]
        self.ready_duration_label.setText(clean_duration if is_ready and clean_duration != self._empty_detail_text else "")
        self.ready_duration_label.setVisible(is_ready)
        self.file_state_badge.setVisible(is_ready)
        self.file_state_badge.setText("✓ 已就绪")
        self.action_frame.setVisible(not is_chip)
        if is_ready:
            self.action_frame.hide()
        self.replace_button.setVisible(is_ready or is_chip)
        self.remove_button.setVisible(False)
        self.detail_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        self.action_label.setText(action_text)
        self.action_icon.setIcon(
            (FIF.UPDATE if is_selected and self._drag_state == "idle" else FIF.UP).icon()
        )
        self.title_label.setText(
            f"{self.path.name} · {self.detail_label.text().split(': ', 1)[-1]}"
            if is_chip and self.path is not None
            else self._media_label
        )
        if not is_chip:
            self.title_label.setText(self._media_label)
        self.title_label.setStyleSheet(
            f"color: {palette['accent']}; font-size: {'11.5pt' if is_chip else '16pt'}; background: transparent; border: 0;"
        )
        self.title_label.setFont(build_app_ui_font(point_size=11.5 if is_chip else 16, bold=True))
        # 直接 setStyleSheet（不再用 _wb_th —— 本方法每次拖拽/hover 都跑，
        # 在循环里加 connect 会泄漏 listener）；颜色取当前主题 palette。
        from krok_helper.theme_workbench import palette as _wb_pal_inner
        _p2 = _wb_pal_inner()
        self.hint_label.setStyleSheet(
            f"color: {_p2.text_secondary}; font-size: 11pt; background: transparent; border: 0;"
        )
        # 文件名 selected/idle —— 深色下用 text_primary，浅色保留原灰阶
        if _p2.is_dark:
            _filename_color = _p2.text_primary
        else:
            _filename_color = "#182230" if is_selected else "#344054"
        self.file_name_label.setStyleSheet(
            f"color: {_filename_color}; font-size: {'10.5pt' if is_chip else '12pt'};"
            " font-weight: 400; background: transparent; border: 0;"
        )
        # "已就绪"绿色徽章 —— 深色下用更亮的绿
        _ready_color = "#6FE3A4" if _p2.is_dark else "#16803D"
        self.file_state_badge.setStyleSheet(
            f"""
            QLabel {{
                background: transparent;
                color: {_ready_color};
                border: 0;
                border-radius: 10px;
                padding: 3px 10px;
                font-size: 9.5pt;
            }}
            """
        )
        self.file_state_badge.setFont(build_app_ui_font(point_size=9.5, bold=True))
        self.ready_duration_label.setStyleSheet(
            f'color: {_p2.text_secondary}; font-family: "Microsoft YaHei UI"; font-size: 11pt;'
            ' font-weight: 400; background: transparent; border: 0;'
        )
        self.detail_label.setStyleSheet(
            f'color: {_p2.text_hint}; font-family: "Microsoft YaHei UI"; font-size: 11pt;'
            ' font-weight: 400; background: transparent; border: 0;'
        )
        self.icon_button.setStyleSheet(
            f"""
            ToolButton {{
                background: {'transparent' if is_chip else (palette["selected_icon_background"] if is_selected else palette["icon_background"])};
                border: 1px solid {'transparent' if is_chip else (palette["accent_border"] if is_selected else 'transparent')};
                border-radius: {17 if is_chip else 24}px;
                padding: 0;
                color: {accent};
            }}
            """
        )
        self.action_icon.setStyleSheet(
            f"""
            ToolButton {{
                background: transparent;
                border: 0;
                padding: 0;
                color: {accent};
            }}
            """
        )
        self.action_label.setStyleSheet(f"color: {accent}; font-size: 12pt; font-weight: 400;")
        for button in (self.replace_button, self.remove_button):
            button.setStyleSheet(
                f"""
                QPushButton {{
                    background: {_p2.input_bg};
                    color: {_p2.text_primary};
                    border: 1px solid {_p2.input_border};
                    border-radius: 8px;
                    padding: 6px 14px;
                }}
                QPushButton:hover {{
                    border-color: {palette["accent"]};
                    color: {palette["accent"]};
                }}
                """
            )
        if self._display_mode == "empty" and self._missing_text:
            self.action_label.setText(f"{self._default_action_text}\n{self._missing_text}")
        self.setStyleSheet(
            f"""
            QFrame#AlignmentDropCard {{
                background: {background};
                border: {border_width}px solid {border};
                border-radius: 18px;
            }}
            QFrame#AlignmentDropAction {{
                background: {action_background};
                border: 1px solid {action_border};
                border-radius: 14px;
            }}
            QFrame#AlignmentDropCard QLabel {{
                background: transparent;
                border: 0;
            }}
            """
        )
        return