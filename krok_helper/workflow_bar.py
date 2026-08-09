"""顶部工作流步条。

六个步骤的元数据 + 步条控件。控件不碰宿主任何状态：点哪一步通过
``stepClicked`` 抛出去，当前步、状态文案、紧凑模式都由外壳调进来。
"""

from __future__ import annotations

from dataclasses import dataclass

from PyQt6.QtCore import (
    QAbstractAnimation,
    QEasingCurve,
    QPropertyAnimation,
    QRect,
    QTimer,
    Qt,
    pyqtSignal as Signal,
)
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from krok_helper.workflow import (
    WORKFLOW_HIRES_MIX,
    WORKFLOW_LYRICS_SEARCH,
    WORKFLOW_LYRICS_TIMING,
    WORKFLOW_SUBTITLE_RENDER,
    WORKFLOW_VIDEO_DOWNLOAD,
    WORKFLOW_WAVEFORM_ALIGN,
)

__all__ = ["WORKFLOW_STEPS", "WorkflowStepButton", "WorkflowStepItem", "WorkflowStepper"]


@dataclass(frozen=True)
class WorkflowStepItem:
    module_id: str
    number: int
    title: str
    description: str
    implemented: bool


WORKFLOW_STEPS = [
    WorkflowStepItem(WORKFLOW_VIDEO_DOWNLOAD, 1, "视频下载", "下载在线视频", False),
    WorkflowStepItem(WORKFLOW_WAVEFORM_ALIGN, 2, "音视频处理", "波形对齐与音频分离", True),
    WorkflowStepItem(WORKFLOW_LYRICS_SEARCH, 3, "歌词检索", "搜索并获取歌词", True),
    WorkflowStepItem(WORKFLOW_LYRICS_TIMING, 4, "歌词打轴", "逐字 / 逐句打轴", False),
    WorkflowStepItem(WORKFLOW_SUBTITLE_RENDER, 5, "字幕视频生成", "渲染字幕样式", False),
    WorkflowStepItem(WORKFLOW_HIRES_MIX, 6, "Hi-Res 混流", "音视频混流导出", True),
]


class WorkflowStepButton(QWidget):
    clicked = Signal(int)

    def __init__(self, step: WorkflowStepItem, index: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.step = step
        self.index = index
        self._active = False
        self._hovered = False
        self._compact = False
        # 宿主 WorkflowStepper 置 True 后，活跃下划线由 stepper 的共享滑块绘制，
        # 本按钮内的静态 bottom_line 不再显示（避免双线）。
        self._shared_underline = False
        # 由宿主写入的瞬时状态文本（如打轴步骤的「当前 .sug 文件名 + 未保存」），
        # None 时回退到步骤的默认描述。
        self._status_text: str | None = None
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(60)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setObjectName("WorkflowStepItem")

        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        outer_layout.setSpacing(0)

        self._content_layout = QHBoxLayout()
        self._content_layout.setContentsMargins(18, 10, 18, 8)
        self._content_layout.setSpacing(10)

        self.number_label = QLabel(str(step.number))
        self.number_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.number_label.setFixedSize(32, 32)
        self.number_label.setObjectName("WorkflowStepNumber")

        self._text_layout = QVBoxLayout()
        self._text_layout.setContentsMargins(0, 0, 0, 0)
        self._text_layout.setSpacing(1)

        self.title_label = QLabel(step.title)
        self.title_label.setObjectName("WorkflowStepTitle")
        self.desc_label = QLabel(step.description)
        self.desc_label.setObjectName("WorkflowStepDescription")
        self.desc_label.setWordWrap(False)
        self.bottom_line = QFrame(self)
        self.bottom_line.setObjectName("WorkflowStepUnderline")
        self.bottom_line.setFixedHeight(2)
        self.bottom_line.hide()

        self._text_layout.addWidget(self.title_label)
        self._text_layout.addWidget(self.desc_label)

        self._content_layout.addWidget(self.number_label, 0, Qt.AlignmentFlag.AlignVCenter)
        self._content_layout.addLayout(self._text_layout, 1)
        outer_layout.addLayout(self._content_layout)
        outer_layout.addWidget(self.bottom_line)
        self._refresh_style()
        # 跟随主题切换重刷颜色 —— 延迟到下个 event loop iter 避免与 SUG
        # ``_refresh_all_widgets`` 同步链上的 polish 操作重入（Win11 上
        # 与 Mica + qfluentwidgets lazy QSS 共同时序敏感）。
        from krok_helper.theme_workbench import schedule_theme_refresh, theme as _wb_theme
        _wb_theme.changed.connect(lambda: schedule_theme_refresh(self, self._refresh_style_safe))

    def _refresh_style_safe(self) -> None:
        try:
            self._refresh_style()
        except RuntimeError:
            pass

    def setActive(self, active: bool) -> None:
        if self._active == active:
            return
        self._active = active
        self._refresh_style()

    def setSharedUnderline(self, shared: bool) -> None:
        """开关共享滑动下划线模式（由 WorkflowStepper 注入）。"""
        if self._shared_underline == shared:
            return
        self._shared_underline = shared
        self._refresh_style()

    def set_status_text(self, text: str | None) -> None:
        """展示瞬时状态（如当前 .sug 文件名 / 未保存）。

        ``None`` 或空串恢复步骤的默认描述。非紧凑模式显示在描述行；紧凑模式
        描述行被隐藏，故并入标题行展示，保证收紧后状态依然可见。
        """
        self._status_text = text or None
        self._render_text()

    def _render_text(self) -> None:
        """根据紧凑态 + 状态文本决定标题/描述两行的内容与可见性。

        - 非紧凑：标题=步骤名；描述=状态文本（无则默认描述），可见。
        - 紧凑：仅一行（编号+标题），描述行隐藏，状态并入标题行
          （``步骤名 · 状态``），无状态时回到纯步骤名。
        """
        status = self._status_text
        if self._compact:
            self.desc_label.hide()
            self.title_label.setText(
                f"{self.step.title} · {status}" if status else self.step.title
            )
        else:
            self.title_label.setText(self.step.title)
            self.desc_label.setText(status or self.step.description)
            self.desc_label.show()

    def setCompact(self, compact: bool) -> None:
        if self._compact == compact:
            return
        self._compact = compact
        self._apply_compact_layout()
        self._refresh_style()

    def _apply_compact_layout(self) -> None:
        # 紧凑模式：编号 + 标题保留 → 整条像 ①视频下载 ②波形对齐 …；副标题（含
        # 状态）则由 _render_text 决定——非紧凑显示在描述行，紧凑并入标题行。
        if self._compact:
            self.setFixedHeight(32)
            self._content_layout.setContentsMargins(10, 2, 10, 2)
            self._content_layout.setSpacing(6)
            self.number_label.setFixedSize(22, 22)
        else:
            self.setFixedHeight(60)
            self._content_layout.setContentsMargins(18, 10, 18, 8)
            self._content_layout.setSpacing(10)
            self.number_label.setFixedSize(32, 32)
        self.title_label.setVisible(True)
        self._render_text()

    def enterEvent(self, event) -> None:  # noqa: N802
        self._hovered = True
        self._refresh_style()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hovered = False
        self._refresh_style()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit(self.index)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _refresh_style(self) -> None:
        from krok_helper.theme_workbench import palette
        p = palette()
        # 工作流步骤色板 —— light/dark 各一套，状态分 active/hover/idle
        if p.is_dark:
            _active_bg     = "#3A2A2C"
            _active_title  = "#FFB3BE"
            _active_desc   = "#C58A92"
            _hover_bg      = "#2A2A2A"
            _idle_bg       = "transparent"
            _idle_title    = p.text_primary
            _idle_desc     = p.text_hint
            _num_bg        = "#2D2D2D"
            _num_border    = "#3E3E3E"
            _num_text      = p.text_secondary
        else:
            _active_bg     = "#FFF6F7"
            _active_title  = "#BC495A"
            _active_desc   = "#8F5B64"
            _hover_bg      = "#F6F8FB"
            _idle_bg       = "transparent"
            _idle_title    = "#1F2937"
            _idle_desc     = "#64748B"
            _num_bg        = "#FFFFFF"
            _num_border    = "#CBD5E1"
            _num_text      = "#64748B"

        if self._active:
            background = _active_bg
            title_color = _active_title
            desc_color = _active_desc
            number_background = p.accent_search
            number_color = "#FFFFFF"
            number_border = p.accent_search
        elif self._hovered:
            background = _hover_bg
            title_color = _idle_title
            desc_color = _idle_desc
            number_background = _num_bg
            number_color = _num_text
            number_border = _num_border
        else:
            background = _idle_bg
            title_color = _idle_title
            desc_color = _idle_desc
            number_background = _num_bg
            number_color = _num_text
            number_border = _num_border

        # 紧凑模式下编号圆点缩到 22px（对应 radius 11、字号 11），同时把活跃步标题字号收一档
        number_radius = 11 if self._compact else 16
        number_font_size = 11 if self._compact else 12
        title_font_size = 13 if self._compact else 14

        self.setStyleSheet(
            f"""
            QWidget#WorkflowStepItem {{
                background: {background};
                border: 0;
                border-radius: 10px;
            }}
            QLabel#WorkflowStepNumber {{
                background: {number_background};
                border: 1px solid {number_border};
                border-radius: {number_radius}px;
                color: {number_color};
                font-size: {number_font_size}px;
                font-weight: 700;
            }}
            QLabel#WorkflowStepTitle {{
                color: {title_color};
                font-size: {title_font_size}px;
                font-weight: 700;
            }}
            QLabel#WorkflowStepDescription {{
                color: {desc_color};
                font-size: 11px;
            }}
            QFrame#WorkflowStepUnderline {{
                background: {p.accent_search};
                border: 0;
                border-radius: 1px;
            }}
            """
        )
        # 紧凑模式下不显示底部下划线，避免活跃步的下划线把窄行撑出 2px 错位；
        # 共享滑块模式下静态线让位给 stepper 的滑动下划线
        self.bottom_line.setVisible(self._active and not self._compact and not self._shared_underline)


class WorkflowStepper(QWidget):
    currentChanged = Signal(int)
    stepClicked = Signal(int)

    def __init__(self, steps: list[WorkflowStepItem], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._steps = steps
        self._items: list[WorkflowStepButton] = []
        self._separators: list[QLabel] = []
        self._current_index = 0
        self._compact = False
        self.setObjectName("WorkflowStepper")

        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)

        for index, _step in enumerate(steps):
            item = self.createStepItem(index)
            self._items.append(item)
            self._layout.addWidget(item, 1)
            if index < len(steps) - 1:
                separator = QLabel("›")
                separator.setAlignment(Qt.AlignmentFlag.AlignCenter)
                from krok_helper.theme_workbench import palette as _wb_palette, themed as _wb_themed
                # 用闭包 + self 引用，避免主题切换覆盖紧凑模式下缩小的字号
                _wb_themed(
                    separator,
                    lambda _s=self: f"color: {_wb_palette().text_disabled}; "
                    f"font-size: {12 if _s._compact else 18}px; font-weight: 500;",
                )
                separator.setFixedWidth(24)
                self._layout.addWidget(separator, 0, Qt.AlignmentFlag.AlignVCenter)
                self._separators.append(separator)

        # 共享滑动下划线：替代各按钮内部的静态下划线，切换步骤时在按钮间平滑滑动。
        # 浮在 stepper 上绘制，不拦截鼠标；几何位置与按钮 bottom_line 完全一致。
        self._underline_anim: QPropertyAnimation | None = None
        self._underline = QFrame(self)
        self._underline.setObjectName("WorkflowStepUnderlineSlider")
        self._underline.setFixedHeight(2)
        self._underline.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        from krok_helper.theme_workbench import palette as _wb_palette, themed as _wb_themed
        _wb_themed(
            self._underline,
            lambda: f"background: {_wb_palette().accent_search}; border: 0; border-radius: 1px;",
        )
        self._underline.hide()
        for item in self._items:
            item.setSharedUnderline(True)

        self.updateStepStyles()

    def createStepItem(self, index: int) -> WorkflowStepButton:
        item = WorkflowStepButton(self._steps[index], index, self)
        item.clicked.connect(self._handleStepClicked)
        return item

    def currentIndex(self) -> int:
        return self._current_index

    def setCurrentIndex(self, index: int) -> None:
        if index < 0 or index >= len(self._steps):
            return
        if self._current_index == index:
            self.updateStepStyles()
            return
        previous_index = self._current_index
        self._current_index = index
        self.updateStepStyles()
        self._slide_underline(previous_index, index)
        self.currentChanged.emit(index)

    def _underline_rect(self, index: int) -> QRect:
        """第 ``index`` 个按钮对应的下划线矩形（stepper 自身坐标系）。"""
        geo = self._items[index].geometry()
        return QRect(geo.x(), geo.y() + geo.height() - 2, geo.width(), 2)

    def _stop_underline_anim(self) -> None:
        anim, self._underline_anim = self._underline_anim, None
        if anim is not None:
            try:
                anim.stop()
            except RuntimeError:
                pass

    def _snap_underline(self) -> None:
        """无动画地把滑动下划线贴到当前步骤（布局未就绪时先隐藏）。"""
        try:
            if self._compact:
                self._underline.hide()
                return
            target = self._underline_rect(self._current_index)
            if target.width() <= 0:
                return
            self._underline.setGeometry(target)
            self._underline.show()
        except RuntimeError:
            pass

    def _slide_underline(self, from_index: int, to_index: int) -> None:
        """切换步骤时让下划线从旧按钮平滑滑到新按钮。"""
        try:
            if self._compact:
                self._underline.hide()
                return
            target = self._underline_rect(to_index)
            if target.width() <= 0:
                # 布局尚未排布（如构造期），等 showEvent/resizeEvent 再 snap
                self._underline.hide()
                return
            start = (
                self._underline.geometry()
                if self._underline.isVisible() and self._underline.geometry().width() > 0
                else self._underline_rect(from_index)
            )
            if start.width() <= 0:
                start = target
            self._stop_underline_anim()
            if start == target or not self.isVisible():
                self._underline.setGeometry(target)
                self._underline.show()
                return
            self._underline.setGeometry(start)
            self._underline.show()
            anim = QPropertyAnimation(self._underline, b"geometry", self)
            anim.setDuration(260)
            anim.setStartValue(start)
            anim.setEndValue(target)
            anim.setEasingCurve(QEasingCurve.Type.OutCubic)
            # 动画期间若按钮几何因 stylesheet polish 等原因微调，收尾时贴准最终位置
            anim.finished.connect(self._snap_underline)
            anim.start(QAbstractAnimation.DeletionPolicy.DeleteWhenStopped)
            self._underline_anim = anim
        except RuntimeError:
            pass

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        # 等布局排完后把下划线贴到当前步骤（构造期按钮几何还是 0 宽）
        QTimer.singleShot(0, self._snap_underline)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        # 窗口拉伸改变按钮几何：停掉进行中的滑动，直接贴到新位置
        self._stop_underline_anim()
        self._snap_underline()

    def setCurrentModule(self, module_id: str) -> None:
        for index, step in enumerate(self._steps):
            if step.module_id == module_id:
                self.setCurrentIndex(index)
                return

    def moduleIdAt(self, index: int) -> str:
        return self._steps[index].module_id

    def setStepStatus(self, module_id: str, text: str | None) -> None:
        """把某一步的描述行替换为瞬时状态文本（None 恢复默认描述）。"""
        for index, step in enumerate(self._steps):
            if step.module_id == module_id:
                self._items[index].set_status_text(text)
                return

    def updateStepStyles(self) -> None:
        for index, item in enumerate(self._items):
            item.setActive(index == self._current_index)

    def updateStyles(self) -> None:
        self.updateStepStyles()

    def isCompact(self) -> bool:
        return self._compact

    def setCompact(self, compact: bool) -> None:
        if self._compact == compact:
            return
        self._compact = compact
        for item in self._items:
            item.setCompact(compact)
        # 分隔符 › 的字号在它的 themed 闭包里读 self._compact，这里只要重跑 QSS
        from krok_helper.theme_workbench import palette as _wb_palette
        sep_width = 12 if compact else 24
        for sep in self._separators:
            sep.setFixedWidth(sep_width)
            sep.setStyleSheet(
                f"color: {_wb_palette().text_disabled}; "
                f"font-size: {12 if compact else 18}px; font-weight: 500;"
            )
        # 滑动下划线遵循原按钮下划线语义：紧凑模式隐藏；展开时等布局排完后贴回
        self._stop_underline_anim()
        if compact:
            self._underline.hide()
        else:
            QTimer.singleShot(0, self._snap_underline)

    def _handleStepClicked(self, index: int) -> None:
        self.stepClicked.emit(index)
