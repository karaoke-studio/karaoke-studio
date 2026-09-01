"""Hi-Res 混流页（工作流第 6 步）。

**这一页已经是独立对象**，不再是混进主窗口的 mixin：它自己的 15 个属性挂在
自己身上，和外壳的全部往来只有一条路 —— 构造时注入的 :class:`HiResHost`。

本页是工作流的产物终点：``set_video_path`` / ``set_on_vocal_path`` /
``add_off_vocal_paths`` 是其他步骤把素材交过来的入口，外壳的
:mod:`krok_helper.workflow_host` 契约转调它们。

后台任务由本页自己持有（``running_tasks`` / ``is_busy`` 供外壳关窗前查询），
不再往宿主身上挂 ``hires_task`` 槽位。
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Callable, Protocol, Sequence, runtime_checkable

from PyQt6.QtCore import (
    QAbstractAnimation,
    QEasingCurve,
    QPropertyAnimation,
    QRectF,
    Qt,
    pyqtProperty,
    pyqtSignal as Signal,
)
from PyQt6.QtGui import QColor, QFont, QPainter, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    PlainTextEdit as QPlainTextEdit,
    PrimaryPushButton,
    ProgressBar as QProgressBar,
    PushButton as QPushButton,
)

from krok_helper.qfluent_compat import show_fluent_error, show_fluent_info
from krok_helper.background import BackgroundTask
from krok_helper.config import FFMPEG_DIR_PLACEHOLDER
from krok_helper.errors import ProcessingError
from krok_helper.ffmpeg import terminate_process
from krok_helper.media_formats import HIRES_AUDIO_EXTENSIONS, VIDEO_EXTENSIONS
from krok_helper.notifications import play_completion_sound
from krok_helper.pipeline import (
    OUTPUT_NAME_MODE_TEMPLATE,
    resolve_off_output_paths,
    resolve_output_dir,
    resolve_output_paths,
    run_pipeline,
)
from krok_helper.ui_kit import (
    CardWidget,
    ControlBar,
    apply_card_shadow,
    apply_safe_label_metrics,
)
from krok_helper.windows import open_in_explorer

__all__ = ["CardFlipOverlay", "CornerBadge", "DropZoneCard", "HiResHost", "HiResPage"]


@runtime_checkable
class HiResHost(Protocol):
    """Hi-Res 页需要外壳提供的全部能力 —— 这一页只能碰到这些。

    这是页面对象化之后 host 面的完整形态：清单从"测试描述现状"变成了真接口，
    页面拿不到接口之外的任何东西。
    """

    def track_background_task(self, task: BackgroundTask) -> BackgroundTask:
        """登记后台任务，让外壳在关窗/强退时统一收尾。"""
        ...

    def resolve_ffmpeg_dir(self) -> Path | None:
        """全局设置里的 ffmpeg 目录（未设置时返回 None，走系统 PATH）。"""
        ...

    def resolve_output_name_mode(self) -> str:
        """输出命名模式：固定名 / 模板。"""
        ...

    def resolve_output_name_templates(self, *, require_valid: bool = False) -> tuple[str, str]:
        """原唱 / 伴奏两个输出文件名模板。

        ``require_valid=True`` 时会校验模板，非法直接抛 ``ProcessingError``。
        """
        ...

    def notify_handoff(self, title: str, content: str) -> None:
        """右下角提示：素材从别的步骤转交过来了。"""
        ...

    def open_settings_window(self, context: str) -> None:
        """打开全局设置对话框的指定分页。"""
        ...


class CornerBadge(QLabel):
    """卡片右上角的小角标；左右键分别发不同的信号。"""

    clicked = Signal()
    rightClicked = Signal()

    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
            event.accept()
            return
        if event.button() == Qt.MouseButton.RightButton:
            self.rightClicked.emit()
            event.accept()
            return
        super().mousePressEvent(event)

    def contextMenuEvent(self, event) -> None:  # noqa: N802
        # 右键已经用来翻上一条了，别再弹系统菜单。
        event.accept()


class CardFlipOverlay(QWidget):
    """卡片翻页时的翻转覆盖层。

    把切换前的样子拍成位图，横向压扁到 0 再展开成新的一张，看起来像卡片翻了个面。
    覆盖层自己铺满整张卡并填上卡片底色，压住下面真实的控件——否则位图缩窄时会露出
    底下没变的内容，动画就白做了。
    """

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self._pixmap = QPixmap()
        self._scale = 1.0
        self._background = QColor("#ffffff")
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.hide()

    def prepare(self, pixmap: QPixmap, background: QColor) -> None:
        self._pixmap = pixmap
        self._background = background

    def _get_scale(self) -> float:
        return self._scale

    def _set_scale(self, value: float) -> None:
        self._scale = float(value)
        self.update()

    scale = pyqtProperty(float, fget=_get_scale, fset=_set_scale)

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.fillRect(self.rect(), self._background)
        if self._pixmap.isNull() or self._scale <= 0.001:
            return
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        ratio = self._pixmap.devicePixelRatio() or 1.0
        width = self._pixmap.width() / ratio
        height = self._pixmap.height() / ratio
        painter.translate(self.width() / 2.0, 0.0)
        painter.scale(max(0.0, self._scale), 1.0)
        painter.drawPixmap(QRectF(-width / 2.0, 0.0, width, height), self._pixmap,
                           QRectF(0, 0, self._pixmap.width(), self._pixmap.height()))


class DropZoneCard(CardWidget):
    pathChanged = Signal(Path)
    #: 多文件模式下条目增删（翻页不发）。
    pathsChanged = Signal()
    browseRequested = Signal()

    def __init__(
        self,
        *,
        title: str,
        hint: str,
        extensions: set[str],
        min_height: int = 220,
        icon_text: str = "",
        placeholder_icon: str = "",
        accent_bg: str = "#f6f8fb",
        multiple: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.extensions = {ext.lower() for ext in extensions}
        self.accent_bg = accent_bg
        #: 唯一真相；单文件模式下最多一个元素，``path`` 只是它的视图。
        self.paths: list[Path] = []
        self.multiple = multiple
        self._index = 0
        self._hovered = False
        self._drag_state = "idle"
        self._default_action_text = "点击选择文件，或直接拖进这个区域"

        self.setObjectName("DropZoneCard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAcceptDrops(True)
        self.setMinimumHeight(min_height)

        layout = self.createVBoxLayout()
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(8)

        title_row = QHBoxLayout()
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.setSpacing(8)

        # 带 self 作 parent：无父 QLabel 在 setVisible(True) 时会作为独立
        # 顶层窗口闪现，随后 addWidget reparent 才把它收回（启动闪小窗根因）。
        self.icon_label = QLabel(icon_text, self)
        self.icon_label.setObjectName("DropZoneIcon")
        self.icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.icon_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.icon_label.setVisible(bool(icon_text))

        self.title_label = QLabel(title)
        self.title_label.setObjectName("DropZoneTitle")
        self.title_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        title_font = QFont("Microsoft YaHei UI", 12)
        title_font.setBold(True)
        apply_safe_label_metrics(self.title_label, title_font)
        title_row.addWidget(self.icon_label, 0, Qt.AlignmentFlag.AlignVCenter)
        title_row.addWidget(self.title_label, 1, Qt.AlignmentFlag.AlignVCenter)

        self._base_hint = hint
        self.hint_label = QLabel(hint)
        self.hint_label.setObjectName("DropZoneHint")
        self.hint_label.setWordWrap(True)
        self.hint_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        self.placeholder_label = QLabel(placeholder_icon)
        self.placeholder_label.setObjectName("DropZonePlaceholder")
        self.placeholder_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.placeholder_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        self.path_label = QLabel("未选择文件")
        self.path_label.setObjectName("DropZonePath")
        self.path_label.setWordWrap(True)
        self.path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.path_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        self.action_label = QLabel(self._default_action_text)
        self.action_label.setObjectName("DropZoneAction")
        self.action_label.setWordWrap(True)
        self.action_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        self._status_badge = QLabel("✓", self)
        self._status_badge.setObjectName("DropZoneStatusBadge")
        self._status_badge.setFixedSize(22, 22)
        self._status_badge.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._status_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._status_badge.setStyleSheet(
            """
            QLabel#DropZoneStatusBadge {
                background: #10B981;
                color: white;
                border-radius: 11px;
                font-size: 13pt;
                font-weight: 700;
                qproperty-alignment: AlignCenter;
            }
            """
        )
        self._status_badge.hide()

        # ── 多文件：序号角标 + 移除按钮 ───────────────────────────
        self._page_badge = CornerBadge("1 / 1", self)
        self._page_badge.setObjectName("DropZonePageBadge")
        self._page_badge.setFixedHeight(22)
        self._page_badge.setMinimumWidth(46)
        self._page_badge.clicked.connect(self.show_next)
        self._page_badge.rightClicked.connect(self.show_previous)
        self._page_badge.hide()

        self._remove_badge = CornerBadge("✕", self)
        self._remove_badge.setObjectName("DropZoneRemoveBadge")
        self._remove_badge.setFixedSize(22, 22)
        self._remove_badge.setToolTip("移除当前这条")
        self._remove_badge.clicked.connect(self._on_remove_clicked)
        self._remove_badge.hide()

        self._flip_overlay = CardFlipOverlay(self)
        self._flip_anim: QPropertyAnimation | None = None

        layout.addLayout(title_row)
        layout.addWidget(self.hint_label)
        layout.addStretch(1)
        layout.addWidget(self.placeholder_label, 0, Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.path_label)
        layout.addWidget(self.action_label)
        self._refresh_style()
        # 跟随主题切换重刷颜色（延迟到下个 event loop iter，参见
        # WorkflowStepButton 同名说明）。
        from krok_helper.theme_workbench import schedule_theme_refresh, theme as _wb_theme
        _wb_theme.changed.connect(lambda: schedule_theme_refresh(self, self._refresh_style_safe))

    def _refresh_style_safe(self) -> None:
        try:
            self._refresh_style()
        except RuntimeError:
            pass

    def _current_background(self) -> str:
        return getattr(self, "_background_color", "#FFFFFF")

    @property
    def path(self) -> Path | None:
        """当前显示的那条；多文件模式下随序号变化。"""
        if not self.paths:
            return None
        return self.paths[min(self._index, len(self.paths) - 1)]

    def accepts(self, path: Path) -> bool:
        return path.is_file() and path.suffix.lower() in self.extensions

    def set_path(self, path: Path) -> None:
        """设为唯一一条（多文件模式下等于清空后重放）。"""
        self.paths = [path]
        self._index = 0
        self._drag_state = "idle"
        self._refresh_paths_ui()

    def add_paths(self, paths: Sequence[Path]) -> list[Path]:
        """追加若干条并跳到第一条新加入的；返回真正加进去的（去重后）。"""
        if not self.multiple:
            if paths:
                self.set_path(Path(paths[-1]))
                return [Path(paths[-1])]
            return []
        existing = {str(item).lower() for item in self.paths}
        added = [Path(item) for item in paths if str(item).lower() not in existing]
        if not added:
            return []
        first_new = len(self.paths)
        self.paths.extend(added)
        self._drag_state = "idle"
        self._go_to(first_new, animate=bool(first_new))
        return added

    def remove_current(self) -> None:
        """移除当前显示的这条，停在原位（即自动落到下一条）。"""
        if not self.paths:
            return
        index = min(self._index, len(self.paths) - 1)
        self.paths.pop(index)
        self._index = min(index, max(0, len(self.paths) - 1))
        self._refresh_paths_ui()

    def clear_path(self) -> None:
        self.paths = []
        self._index = 0
        self._drag_state = "idle"
        self._refresh_paths_ui()

    # ── 多文件翻页 ────────────────────────────────────────────────
    def _go_to(self, index: int, *, animate: bool = True) -> None:
        if not self.paths:
            return
        index %= len(self.paths)
        if index == self._index:
            self._refresh_paths_ui()
            return
        if not animate or not self.isVisible():
            self._index = index
            self._refresh_paths_ui()
            return
        self._flip_to(index)

    def show_next(self) -> None:
        self._go_to(self._index + 1)

    def show_previous(self) -> None:
        self._go_to(self._index - 1)

    def _refresh_paths_ui(self) -> None:
        current = self.path
        self.path_label.setText(str(current) if current is not None else "未选择文件")
        total = len(self.paths)
        show_pager = self.multiple and total > 1
        self._page_badge.setVisible(show_pager)
        if show_pager:
            self._page_badge.setText(f"{min(self._index, total - 1) + 1} / {total}")
            self._page_badge.setToolTip("左键下一条，右键上一条")
        self._remove_badge.setVisible(self.multiple and total > 0)
        if self.multiple and total > 1:
            self.hint_label.setText(f"已放入 {total} 个伴奏音频，将各生成一个混流视频。")
        else:
            self.hint_label.setText(self._base_hint)
        self._refresh_style()
        self._position_status_badge()

    def _flip_to(self, index: int) -> None:
        anim, self._flip_anim = self._flip_anim, None
        try:
            # 动画用的是 DeleteWhenStopped，跑完 C++ 对象就没了；这里可能拿到一个
            # 已经析构的壳子（连点翻页就会撞上），所以要兜住 RuntimeError。
            if anim is not None and anim.state() == QAbstractAnimation.State.Running:
                anim.stop()
        except RuntimeError:
            pass

        overlay = self._flip_overlay
        overlay.setGeometry(self.rect())
        overlay.prepare(self.grab(), QColor(self._current_background()))
        overlay.show()
        overlay.raise_()

        forward = QPropertyAnimation(overlay, b"scale", self)
        forward.setDuration(110)
        forward.setStartValue(1.0)
        forward.setEndValue(0.0)
        forward.setEasingCurve(QEasingCurve.Type.InCubic)

        def _swap() -> None:
            self._index = index
            self._refresh_paths_ui()
            overlay.hide()  # 抓图时别把覆盖层自己也抓进去
            overlay.prepare(self.grab(), QColor(self._current_background()))
            overlay.show()
            overlay.raise_()
            back = QPropertyAnimation(overlay, b"scale", self)
            back.setDuration(130)
            back.setStartValue(0.0)
            back.setEndValue(1.0)
            back.setEasingCurve(QEasingCurve.Type.OutCubic)

            def _done() -> None:
                overlay.hide()
                self._flip_anim = None  # 别留下指向已析构对象的引用

            back.finished.connect(_done)
            self._flip_anim = back
            back.start(QAbstractAnimation.DeletionPolicy.DeleteWhenStopped)

        forward.finished.connect(_swap)
        self._flip_anim = forward
        forward.start(QAbstractAnimation.DeletionPolicy.DeleteWhenStopped)

    def enterEvent(self, event) -> None:  # noqa: N802
        self._hovered = True
        self._refresh_style()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hovered = False
        self._refresh_style()
        super().leaveEvent(event)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._position_status_badge()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.browseRequested.emit()
            event.accept()
            return
        super().mousePressEvent(event)

    def _accepted_drops(self, mime) -> list[Path]:
        """拖进来的东西里挑出能收的；单文件模式只取第一个。"""
        paths = [Path(url.toLocalFile()).expanduser() for url in mime.urls()]
        usable = [path for path in paths if str(path) and self.accepts(path)]
        if not self.multiple:
            return usable[:1]
        return usable

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if self._accepted_drops(event.mimeData()):
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
        accepted = self._accepted_drops(event.mimeData())
        self._drag_state = "idle"
        if not accepted:
            self._refresh_style()
            event.ignore()
            return
        added = self.add_paths(accepted)
        event.acceptProposedAction()
        if added:
            self.pathChanged.emit(added[0])

    def _on_remove_clicked(self) -> None:
        self.remove_current()
        self.pathsChanged.emit()

    def _position_status_badge(self) -> None:
        right = max(0, self.width() - 32)
        self._status_badge.move(right, 10)
        self._status_badge.raise_()
        # 角标从右往左排：✓ 之后是序号，再往左是移除按钮。
        cursor = right
        if self._status_badge.isVisibleTo(self):
            cursor -= 6
        if self._page_badge.isVisibleTo(self):
            cursor -= self._page_badge.width()
            self._page_badge.move(max(0, cursor), 10)
            self._page_badge.raise_()
            cursor -= 6
        if self._remove_badge.isVisibleTo(self):
            cursor -= self._remove_badge.width()
            self._remove_badge.move(max(0, cursor), 10)
            self._remove_badge.raise_()

    def _refresh_style(self) -> None:
        from krok_helper.theme_workbench import palette
        p = palette()
        selected = bool(getattr(self, "_path", None) or self.path)
        border_width = "1.5"
        border_style = "dashed"
        # 拖拽/选中/idle 各态色板：light/dark 各一套
        if p.is_dark:
            _accept_bg, _accept_border, _accept_accent = "#1F2C40", "#5B9DFF", "#A6C8FF"
            _reject_bg, _reject_border, _reject_accent = "#3A1A1A", "#EF5A5A", "#FF9C9C"
            _hover_bg, _idle_bg = p.input_bg, p.card_bg
            _hover_border, _hover_accent = "#5B9DFF", "#5B9DFF"
            _selected_bg, _selected_border, _selected_accent = p.card_bg, "#3DB37D", "#6FE3A4"
            _idle_border, _idle_accent = "#3E3E3E", "#5B9DFF"
            _title_color, _hint_color, _placeholder_color, _path_color = (
                p.text_primary, p.text_secondary, "#525252", p.text_primary,
            )
        else:
            _accept_bg, _accept_border, _accept_accent = "#dbeafe", "#2f6fed", "#1d4ed8"
            _reject_bg, _reject_border, _reject_accent = "#fef2f2", "#ef4444", "#b91c1c"
            _hover_bg, _idle_bg = self.accent_bg, self.accent_bg
            _hover_border, _hover_accent = "#2f6fed", "#2f6fed"
            _selected_bg, _selected_border, _selected_accent = "#FFFFFF", "#10B981", "#177245"
            _idle_border, _idle_accent = "#C2CAD8", "#2f6fed"
            _title_color, _hint_color, _placeholder_color, _path_color = (
                "#1f2937", "#5b6677", "#C2CAD8", "#111827",
            )

        if self._drag_state == "accept":
            background = _accept_bg
            border = _accept_border
            accent = _accept_accent
            border_width = "2"
            border_style = "solid"
            action_text = "松开鼠标即可导入这个文件"
        elif self._drag_state == "reject":
            background = _reject_bg
            border = _reject_border
            accent = _reject_accent
            border_width = "2"
            border_style = "solid"
            action_text = "这个文件类型不支持，请换一个文件"
        elif self._hovered:
            background = _hover_bg
            border = _hover_border
            accent = _hover_accent
            border_width = "2"
            border_style = "solid"
            action_text = self._default_action_text
        elif selected:
            background = _selected_bg
            border = _selected_border
            accent = _selected_accent
            border_style = "solid"
            action_text = self._default_action_text
        else:
            background = _idle_bg
            border = _idle_border
            accent = _idle_accent
            action_text = self._default_action_text

        self.action_label.setText(action_text)
        self.placeholder_label.setVisible(self.path is None and bool(self.placeholder_label.text()))
        self._status_badge.setVisible(selected)
        # 翻转覆盖层要用卡片当前底色铺满，才能压住下面没变的内容。
        self._background_color = background
        total = len(self.paths)
        self._page_badge.setVisible(self.multiple and total > 1)
        self._remove_badge.setVisible(self.multiple and total > 0)
        self._page_badge.setStyleSheet(
            f"""
            QLabel#DropZonePageBadge {{
                background: {accent};
                color: white;
                border-radius: 11px;
                padding: 0 8px;
                font-family: "Microsoft YaHei UI";
                font-size: 9.5pt;
                font-weight: 700;
            }}
            """
        )
        self._remove_badge.setStyleSheet(
            f"""
            QLabel#DropZoneRemoveBadge {{
                background: transparent;
                border: 1px solid {border};
                border-radius: 11px;
                color: {_hint_color};
                font-size: 10pt;
                font-weight: 700;
            }}
            QLabel#DropZoneRemoveBadge:hover {{
                background: {_reject_bg};
                border-color: {_reject_border};
                color: {_reject_accent};
            }}
            """
        )

        self.setStyleSheet(
            f"""
            QFrame#DropZoneCard {{
                background: {background};
                border: {border_width}px {border_style} {border};
                border-radius: 10px;
            }}
            QLabel#DropZoneIcon {{
                background: transparent;
                border: 0;
                font-size: 16pt;
            }}
            QLabel#DropZoneTitle {{
                background: transparent;
                border: 0;
                color: {_title_color};
                font-family: "Microsoft YaHei UI";
                font-size: 12pt;
                font-weight: 700;
            }}
            QLabel#DropZoneHint {{
                background: transparent;
                border: 0;
                color: {_hint_color};
                font-family: "Microsoft YaHei UI";
                font-size: 10pt;
            }}
            QLabel#DropZonePlaceholder {{
                background: transparent;
                border: 0;
                color: {_placeholder_color};
                font-family: "Microsoft YaHei UI";
                font-size: 48px;
            }}
            QLabel#DropZonePath {{
                background: transparent;
                border: 0;
                color: {_path_color};
                font-family: "Consolas";
                font-size: 10pt;
            }}
            QLabel#DropZoneAction {{
                background: transparent;
                border: 0;
                color: {accent};
                font-family: "Microsoft YaHei UI";
                font-size: 10pt;
                font-weight: 700;
            }}
            """
        )
        self._position_status_badge()


class HiResPage(QWidget):
    """Hi-Res 混流页 —— 独立控件，不再混进主窗口。

    与外壳的全部往来都经过 :class:`HiResHost`；除此之外它只碰自己的成员。
    """

    def __init__(self, *, host: HiResHost, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._host = host
        self._task: BackgroundTask | None = None
        self._hires_cancel_requested = False
        self._hires_process: subprocess.Popen | None = None
        self._hires_expected_outputs: list[Path] = []
        self._hires_completed_outputs: list[Path] = []
        self._hires_preexisting_outputs: set[Path] = set()
        self._status_color: str | None = None
        self._build_ui()

    def _register_task(self, task: BackgroundTask) -> BackgroundTask:
        self._task = task
        task.finished.connect(self._forget_task)
        return self._host.track_background_task(task)

    def _forget_task(self) -> None:
        self._task = None

    def set_ffmpeg_dir_text(self, text: str) -> None:
        """外壳改了 ffmpeg 目录后调一次 —— 页面上那行说明文字跟着更新。"""
        self.hires_ffmpeg_label.setText(text or FFMPEG_DIR_PLACEHOLDER)

    def is_busy(self) -> bool:
        """本页是否有活儿在跑 —— 外壳关窗前会问。"""
        return self._is_hires_running()

    def running_tasks(self) -> list[BackgroundTask]:
        return [self._task] if self._task is not None and self._task.isRunning() else []

    def update_blocking_labels(self) -> list[str]:
        return ["Hi-Res 混流－混流处理中"] if self.is_busy() else []

    def accept_separated_accompaniment(self, paths: Sequence[Path]) -> list[Path]:
        """第 2 步分离出的伴奏放进第 6 步的伴奏卡（追加，不顶掉已有的）。

        只放素材、不切页面：用户往往还要在第 2 步接着分下一首，跳走反而打断。
        """
        accepted = self.add_off_vocal_paths(paths)
        if accepted:
            detail = (
                f"「{accepted[0].name}」已放入第 6 步 Hi-Res 混流的伴奏卡。"
                if len(accepted) == 1
                else f"{len(accepted)} 个伴奏已放入第 6 步 Hi-Res 混流的伴奏卡。"
            )
            self._host.notify_handoff("伴奏已交给下一步", detail)
        return accepted

    def accept_source_as_on_vocal(self, path: Path) -> bool:
        """第 2 步分离用的那份原始音频放进原唱卡。

        原唱只有一张卡，所以这里是**覆盖**而不是追加 —— 用户是在分离完成的
        对话框里明确勾选了才会走到这条路，覆盖了什么在提示里说清楚。
        同样只放素材、不切页面（跟伴奏转交一致）。
        """
        path = Path(path)
        if not path.is_file():
            return False
        previous = self.on_vocal_zone.path
        self.set_on_vocal_path(path)
        detail = f"「{path.name}」已放入第 6 步 Hi-Res 混流的原唱卡。"
        if previous is not None and previous != path:
            detail += f"\n原先的「{previous.name}」已被替换。"
        self._host.notify_handoff("原唱已交给下一步", detail)
        return True

    def _build_ui(self) -> None:
        shell = QVBoxLayout(self)
        shell.setContentsMargins(20, 20, 20, 20)
        shell.setSpacing(16)

        header = QVBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(6)
        title = QLabel("卡拉 OK 字幕视频一键 Hi-Res 生成")
        title.setObjectName("PageTitle")
        desc = QLabel("把字幕视频拖进下方卡片，再按需放入原唱音频和 / 或伴奏音频。至少提供一条音频就可以开始生成。")
        desc.setWordWrap(True)
        from krok_helper.theme_workbench import palette as _wb_pal, themed as _wb_th
        _wb_th(desc, lambda: f"color: {_wb_pal().text_secondary}; font-size: 10.5pt;")
        header.addWidget(title)
        header.addWidget(desc)
        shell.addLayout(header)

        settings_card = CardWidget(radius=10, padding=(16, 16, 16, 16), spacing=10)
        apply_card_shadow(settings_card)
        settings_layout = settings_card.createGridLayout()
        settings_layout.setHorizontalSpacing(14)
        settings_layout.setVerticalSpacing(10)
        output_label = QLabel("输出目录")
        _wb_th(output_label, lambda: f'font-size: 11pt; font-weight: 400; color: {_wb_pal().text_secondary};')
        self.output_dir_label = QLabel("跟随字幕视频所在目录")
        self.output_dir_label.setWordWrap(True)
        _wb_th(self.output_dir_label, lambda: f'font-size: 11pt; color: {_wb_pal().text_primary}; font-weight: 500;')
        ffmpeg_title = QLabel("FFmpeg 目录 ⓘ")
        ffmpeg_title.setToolTip('FFmpeg 目录、输出命名等偏好设置可在"设置"窗口中调整并保存到本地。')
        _wb_th(ffmpeg_title, lambda: f'font-size: 11pt; font-weight: 400; color: {_wb_pal().text_secondary};')
        self.hires_ffmpeg_label = QLabel(FFMPEG_DIR_PLACEHOLDER)
        self.hires_ffmpeg_label.setWordWrap(True)
        settings_button = QPushButton("⚙ 设置")
        _wb_th(settings_button, lambda: (
            "QPushButton {{"
            " background: transparent;"
            " border: 1px solid {border};"
            " border-radius: 6px;"
            " padding: 6px 14px;"
            " color: {color};"
            " font-size: 10.5pt;"
            "}}"
            "QPushButton:hover {{"
            " background: {hover};"
            "}}"
        ).format(
            border=_wb_pal().input_border,
            color=_wb_pal().text_secondary,
            hover=_wb_pal().secondary_button_hover_bg,
        ))
        settings_button.clicked.connect(lambda: self._host.open_settings_window("hires"))
        settings_layout.addWidget(output_label, 0, 0)
        settings_layout.addWidget(self.output_dir_label, 0, 1)
        settings_layout.addWidget(settings_button, 0, 2)
        settings_layout.setColumnStretch(1, 1)
        shell.addWidget(settings_card)

        card_row = QHBoxLayout()
        card_row.setContentsMargins(0, 0, 0, 0)
        card_row.setSpacing(12)
        self.video_zone = DropZoneCard(
            title="字幕视频",
            hint="支持 mkv / mp4 / mov / avi\n这里会决定输出文件名和输出目录。",
            extensions=VIDEO_EXTENSIONS,
            min_height=190,
            icon_text="🎬",
            placeholder_icon="🎞",
            accent_bg="#EEF4FF",
        )
        self.video_zone.browseRequested.connect(self._choose_video)
        self.video_zone.pathChanged.connect(self.set_video_path)

        self.on_vocal_zone = DropZoneCard(
            title="原唱音频",
            hint="支持 flac / wav / mp3 / m4a / aac / ape / alac / mkv / mp4\n可单独生成原唱 Hi-Res 视频，也可和伴奏一起生成。",
            extensions=HIRES_AUDIO_EXTENSIONS,
            min_height=190,
            icon_text="🎤",
            placeholder_icon="🎙",
            accent_bg="#F3EEFF",
        )
        self.on_vocal_zone.browseRequested.connect(self._choose_on_audio)
        self.on_vocal_zone.pathChanged.connect(self.set_on_vocal_path)

        self.off_vocal_zone = DropZoneCard(
            title="伴奏音频",
            hint="支持 flac / wav / mp3 / m4a / aac / ape / alac / mkv / mp4\n可放入多条伴奏，每条各出一个视频；也可只放原唱。",
            extensions=HIRES_AUDIO_EXTENSIONS,
            min_height=190,
            icon_text="🎵",
            placeholder_icon="♪",
            accent_bg="#EAF7F4",
            multiple=True,
        )
        self.off_vocal_zone.browseRequested.connect(self._choose_off_audio)
        # 不接 pathChanged -> set_off_vocal_path：多文件卡在拖放里已经把自己更新好了，
        # 再回写一次等于 set_path，会把整份列表塌成一条。
        for drop_zone in (self.video_zone, self.on_vocal_zone, self.off_vocal_zone):
            apply_card_shadow(drop_zone)

        card_row.addWidget(self.video_zone, 1)
        card_row.addWidget(self.on_vocal_zone, 1)
        card_row.addWidget(self.off_vocal_zone, 1)
        shell.addLayout(card_row)

        log_panel = CardWidget(radius=10, padding=(16, 16, 16, 16), spacing=12)
        apply_card_shadow(log_panel)
        log_layout = log_panel.createGridLayout()
        log_layout.setVerticalSpacing(12)
        log_title = QLabel("处理日志")
        log_title.setObjectName("PanelTitle")
        def _log_button_qss():
            p = _wb_pal()
            return (
                "QPushButton {{"
                " background: transparent; border: 0; border-radius: 6px;"
                " color: {color}; font-size: 12pt;"
                "}}"
                "QPushButton:hover {{ background: {hover}; }}"
            ).format(color=p.text_secondary, hover=p.secondary_button_hover_bg)
        copy_log_btn = QPushButton("📋")
        copy_log_btn.setFixedSize(28, 28)
        copy_log_btn.setToolTip("复制全部日志")
        _wb_th(copy_log_btn, _log_button_qss)
        copy_log_btn.clicked.connect(self._copy_hires_log)
        clear_log_btn = QPushButton("🗑")
        clear_log_btn.setFixedSize(28, 28)
        clear_log_btn.setToolTip("清空日志")
        _wb_th(clear_log_btn, _log_button_qss)
        self.hires_log = QPlainTextEdit()
        self.hires_log.setObjectName("LogText")
        self.hires_log.setReadOnly(True)
        clear_log_btn.clicked.connect(self.hires_log.clear)
        self.hires_log.setPlaceholderText("运行后将在此显示 FFmpeg 输出与处理进度...")
        _wb_th(self.hires_log, lambda: (
            "QPlainTextEdit#LogText {{"
            " background: {bg};"
            " border: 1px solid {border};"
            " border-radius: 8px;"
            " color: {color};"
            ' font-family: "Consolas", "JetBrains Mono", monospace;'
            " font-size: 10pt;"
            " padding: 10px;"
            "}}"
        ).format(
            bg=_wb_pal().log_bg,
            border=_wb_pal().input_border,
            color=_wb_pal().log_text,
        ))
        log_layout.addWidget(log_title, 0, 0)
        log_layout.addWidget(copy_log_btn, 0, 1)
        log_layout.addWidget(clear_log_btn, 0, 2)
        log_layout.addWidget(self.hires_log, 1, 0, 1, 3)
        log_layout.setColumnStretch(0, 1)
        log_layout.setRowStretch(1, 1)
        shell.addWidget(log_panel, 1)

        controls_bar = ControlBar()
        controls = controls_bar.createHBoxLayout()
        self.hires_start_button = PrimaryPushButton("▶  开始生成")
        self.hires_start_button.clicked.connect(self._start_hires)
        self.hires_cancel_button = QPushButton("■  取消生成")
        self.hires_cancel_button.setEnabled(False)
        self.hires_cancel_button.clicked.connect(self._stop_hires)
        clear_button = QPushButton("✕  清空已选文件")
        clear_button.clicked.connect(self._clear_hires_inputs)
        open_output_button = QPushButton("📁  打开输出目录")
        open_output_button.clicked.connect(self._open_hires_output_dir)
        self.hires_progress = QProgressBar()
        self.hires_progress.setRange(0, 1)
        self.hires_progress.setValue(0)
        self.hires_progress.setFixedWidth(220)
        self.hires_progress.setFixedHeight(10)
        self.hires_progress.setTextVisible(True)
        _wb_th(self.hires_progress, lambda: (
            "QProgressBar {{"
            " border: 0; border-radius: 5px;"
            " background: {bg}; text-align: center; color: transparent;"
            "}}"
            "QProgressBar::chunk {{ background: #2f6fed; border-radius: 5px; }}"
        ).format(bg=_wb_pal().progress_bg))
        self.hires_status_label = QLabel("准备就绪")
        # 由 ``_set_hires_status_color`` 在状态变化时单独驱动 —— 不挂 themed()，
        # 否则会把动态 success/error/processing 颜色覆盖回 idle 文字色。
        self._set_hires_status_color(None)
        controls.addWidget(self.hires_start_button)
        controls.addWidget(self.hires_cancel_button)
        controls.addWidget(clear_button)
        controls.addWidget(open_output_button)
        controls.addStretch(1)
        controls.addWidget(self.hires_progress)
        controls.addSpacing(12)
        controls.addWidget(self.hires_status_label)
        controls_bar.apply_button_metrics(self.hires_start_button, self.hires_cancel_button, clear_button, open_output_button)
        shell.addWidget(controls_bar)

    def set_video_path(self, path: Path) -> None:
        self.video_zone.set_path(path)
        self.output_dir_label.setText(str(resolve_output_dir(path)))

    def set_on_vocal_path(self, path: Path) -> None:
        self.on_vocal_zone.set_path(path)

    def set_off_vocal_path(self, path: Path) -> None:
        self.off_vocal_zone.set_path(path)

    def add_off_vocal_paths(self, paths: Sequence[Path]) -> list[Path]:
        """追加伴奏（不覆盖已有的）；音频分离的转交也走这里。"""
        return self.off_vocal_zone.add_paths(list(paths))

    def _choose_video(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择字幕视频", "", "视频文件 (*.mkv *.mp4 *.mov *.avi);;所有文件 (*.*)")
        if path:
            self.set_video_path(Path(path))

    def _choose_on_audio(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择原唱音频",
            "",
            "音频文件 (*.flac *.wav *.mp3 *.m4a *.aac *.ape *.alac *.mkv *.mp4);;所有文件 (*.*)",
        )
        if path:
            self.set_on_vocal_path(Path(path))

    def _choose_off_audio(self) -> None:
        # 伴奏可以有多条，每条各出一个混流视频，所以这里允许多选。
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "选择伴奏音频（可多选）",
            "",
            "音频文件 (*.flac *.wav *.mp3 *.m4a *.aac *.ape *.alac *.mkv *.mp4);;所有文件 (*.*)",
        )
        if paths:
            self.add_off_vocal_paths([Path(item) for item in paths])

    def _validate_hires_inputs(
        self,
    ) -> tuple[Path, Path | None, list[Path], Path, Path | None, str, str | None, str | None]:
        video_path = self.video_zone.path
        on_vocal_path = self.on_vocal_zone.path
        off_vocal_paths = list(self.off_vocal_zone.paths)
        ffmpeg_dir = self._host.resolve_ffmpeg_dir()
        output_name_mode = self._host.resolve_output_name_mode()

        missing: list[str] = []
        if video_path is None or not video_path.is_file():
            missing.append("字幕视频")
        if on_vocal_path is not None and not on_vocal_path.is_file():
            missing.append("原唱音频")
        if any(not path.is_file() for path in off_vocal_paths):
            missing.append("伴奏音频")
        if missing:
            raise ProcessingError(f"请先选择有效的文件: {', '.join(missing)}")
        assert video_path is not None

        if on_vocal_path is None and not off_vocal_paths:
            raise ProcessingError("请至少选择原唱音频或伴奏音频中的一个。")
        if on_vocal_path is not None:
            on_resolved = on_vocal_path.resolve()
            if any(path.resolve() == on_resolved for path in off_vocal_paths):
                raise ProcessingError("原唱音频和伴奏音频不能是同一个文件。")

        output_dir = resolve_output_dir(video_path)
        if output_name_mode == OUTPUT_NAME_MODE_TEMPLATE:
            on_template, off_template = self._host.resolve_output_name_templates(require_valid=True)
        else:
            on_template, off_template = None, None

        return (
            video_path,
            on_vocal_path,
            off_vocal_paths,
            output_dir,
            ffmpeg_dir,
            output_name_mode,
            on_template,
            off_template,
        )

    def collect_settings(self) -> None:
        """本页没有自己的持久化项 —— 输出命名/目录都归全局设置。

        明写成空实现，是为了让外壳能一视同仁地遍历各页，不必按方法名去猜。
        """

    def rerender_after_theme_change(self) -> None:
        """主题切换后重上状态色 —— idle 那一档是跟着配色走的，不重上会留在旧主题。"""
        self._set_hires_status_color(self._status_color)

    def _set_hires_status_color(self, color: str | None) -> None:
        # ``None`` 表示 idle —— 用当前主题的 ``text_secondary``；其它显式色
        # （success/error/processing）按原值传给 QSS，深色背景下这些状态色
        # 都有足够对比度。
        self._status_color = color
        if color is None:
            from krok_helper.theme_workbench import palette as _wb_pal
            color = _wb_pal().text_secondary
        self.hires_status_label.setStyleSheet(
            f'font-family: "Microsoft YaHei UI"; font-size: 10pt; font-weight: 400; color: {color};'
        )

    def _copy_hires_log(self) -> None:
        QApplication.clipboard().setText(self.hires_log.toPlainText())

    def _is_hires_running(self) -> bool:
        return self._task is not None and self._task.isRunning()

    def _register_hires_process(self, process: subprocess.Popen | None) -> None:
        self._hires_process = process

    def _cleanup_incomplete_hires_outputs(self) -> None:
        completed = set(self._hires_completed_outputs)
        for path in self._hires_expected_outputs:
            if path in completed or path in self._hires_preexisting_outputs or not path.exists():
                continue
            try:
                path.unlink()
                self._append_hires_log(f"已清理未完成的输出文件: {path}")
            except OSError as exc:
                self._append_hires_log(f"清理未完成的输出文件失败: {path} ({exc})")

    def _reset_hires_cancel_state(self) -> None:
        self._hires_cancel_requested = False
        self._hires_process = None
        self._hires_expected_outputs = []
        self._hires_completed_outputs = []
        self._hires_preexisting_outputs = set()

    def _stop_hires(self) -> None:
        if not self._is_hires_running():
            return
        if not self._hires_cancel_requested:
            self._hires_cancel_requested = True
            self.hires_cancel_button.setEnabled(False)
            self.hires_status_label.setText("正在取消…")
            self._set_hires_status_color(None)
            self._append_hires_log("正在取消生成…")
        process = self._hires_process
        if process is not None:
            terminate_process(process)

    def _start_hires(self) -> None:
        if self._is_hires_running():
            show_fluent_info(self, "当前任务还在处理中，请稍等。")
            return

        try:
            args = self._validate_hires_inputs()
        except ProcessingError as exc:
            show_fluent_error(self, str(exc))
            return

        self.hires_log.clear()
        (
            video_path,
            on_vocal_path,
            off_vocal_paths,
            output_dir,
            _ffmpeg_dir,
            output_name_mode,
            on_template,
            off_template,
        ) = args
        # 这里才会用真实的视频文件名渲染模板，可能因文件名导致生成的输出名为空
        # 等情况抛 ProcessingError；必须在主线程上兜住，否则异常会逃逸出 Qt 槽。
        try:
            on_output: Path | None = None
            if on_vocal_path is not None:
                on_output, _ = resolve_output_paths(
                    video_path,
                    output_dir,
                    output_name_mode,
                    on_name_template=on_template,
                    include_on=True,
                    include_off=False,
                    on_audio_path=on_vocal_path,
                )
            off_outputs = resolve_off_output_paths(
                video_path, output_dir, output_name_mode, off_template, off_vocal_paths
            )
        except ProcessingError as exc:
            show_fluent_error(self, str(exc))
            return
        self._hires_cancel_requested = False
        self._hires_process = None
        self._hires_expected_outputs = ([on_output] if on_output is not None else []) + off_outputs
        self._hires_completed_outputs = []
        self._hires_preexisting_outputs = {path for path in self._hires_expected_outputs if path.exists()}
        self.hires_start_button.setEnabled(False)
        self.hires_cancel_button.setEnabled(True)
        self.hires_progress.setRange(0, 0)
        total = len(self._hires_expected_outputs)
        self.hires_status_label.setText("处理中…" if total < 2 else f"处理中…（共 {total} 个输出）")
        self._set_hires_status_color("#2f6fed")

        def runner(logger: Callable[[str], None]) -> list[Path]:
            (
                video_path,
                on_vocal_path,
                off_vocal_paths,
                output_dir,
                ffmpeg_dir,
                output_name_mode,
                on_template,
                off_template,
            ) = args
            outputs = run_pipeline(
                video_path=video_path,
                on_vocal_path=on_vocal_path,
                off_vocal_paths=off_vocal_paths,
                output_dir=output_dir,
                ffmpeg_dir=ffmpeg_dir,
                output_name_mode=output_name_mode,
                on_name_template=on_template,
                off_name_template=off_template,
                logger=logger,
                should_cancel=lambda: self._hires_cancel_requested,
                on_process_started=self._register_hires_process,
            )
            self._hires_completed_outputs.extend(outputs)
            return outputs

        task = self._register_task(BackgroundTask(runner))
        task.log_message.connect(self._append_hires_log)
        task.task_succeeded.connect(self._finish_hires_success)
        task.task_failed.connect(self._finish_hires_failure)
        task.start()

    def _append_hires_log(self, message: str) -> None:
        timestamp = time.strftime("%H:%M:%S")
        self.hires_log.appendPlainText(f"[{timestamp}] {message}")

    def _finish_hires_success(self, outputs: object) -> None:
        was_cancelled = self._hires_cancel_requested
        self._hires_process = None
        self.hires_progress.setRange(0, 1)
        self.hires_progress.setValue(0 if was_cancelled else 1)
        self.hires_start_button.setEnabled(True)
        self.hires_cancel_button.setEnabled(False)
        if was_cancelled:
            self._cleanup_incomplete_hires_outputs()
            self.hires_status_label.setText("生成已取消")
            self._set_hires_status_color(None)
            self._append_hires_log("生成已取消，临时文件和未完成输出已清理。")
            self._reset_hires_cancel_state()
            return
        self.hires_status_label.setText("完成")
        self._set_hires_status_color("#10B981")
        self._reset_hires_cancel_state()
        lines = "\n".join(str(path) for path in outputs) if isinstance(outputs, list) else str(outputs)
        play_completion_sound()
        from krok_helper.subtitle_render.frontend.dialogs.fluent_dialogs import fluent_info

        fluent_info(
            self,
            "Hi-Res 导出完成",
            f"文件已成功导出：\n{lines}",
            ok_text="确定",
            copyable=True,
        )

    def _finish_hires_failure(self, message: str) -> None:
        was_cancelled = self._hires_cancel_requested
        self._hires_process = None
        self.hires_progress.setRange(0, 1)
        self.hires_progress.setValue(0)
        self.hires_start_button.setEnabled(True)
        self.hires_cancel_button.setEnabled(False)
        if was_cancelled:
            self._cleanup_incomplete_hires_outputs()
            self.hires_status_label.setText("生成已取消")
            self._set_hires_status_color(None)
            self._append_hires_log("生成已取消，临时文件和未完成输出已清理。")
            self._reset_hires_cancel_state()
            return
        self.hires_status_label.setText("失败")
        self._set_hires_status_color("#EF4444")
        self._reset_hires_cancel_state()
        self._append_hires_log(f"处理失败: {message}")
        show_fluent_error(self, message)

    def _clear_hires_inputs(self) -> None:
        if self._task is not None and self._task.isRunning():
            show_fluent_info(self, "当前生成任务还在处理中，请稍等。")
            return
        self.video_zone.clear_path()
        self.on_vocal_zone.clear_path()
        self.off_vocal_zone.clear_path()
        self.output_dir_label.setText("跟随字幕视频所在目录")
        self.hires_status_label.setText("已清空已选文件")
        self._set_hires_status_color(None)

    def _open_hires_output_dir(self) -> None:
        video_path = self.video_zone.path
        if video_path is None:
            show_fluent_info(self, "请先选择字幕视频。")
            return
        output_dir = resolve_output_dir(video_path)
        output_dir.mkdir(parents=True, exist_ok=True)
        open_in_explorer(output_dir)
