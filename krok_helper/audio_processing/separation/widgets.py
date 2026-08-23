"""音频分离工作区的 UI 组件。

卡片容器沿用工作台的 ``QFrame[cardWidget="true"]`` 全局样式
（见 ``theme_workbench.py``），与 ``gui_qt.CardWidget`` 同款，保持视觉统一；
此处自带一份最小实现，避免音频分离包反向依赖 9000 行的 gui_qt。
"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QElapsedTimer, Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    ComboBox,
    FluentIcon as FIF,
    IconWidget,
    IndeterminateProgressBar,
    LineEdit,
    PrimaryPushButton,
    ProgressBar,
    PushButton,
    StrongBodyLabel,
    ToolButton,
)

from krok_helper.audio_processing.separation.states import (
    STATE_META,
    TASK_SPECS,
    StateLevel,
    TaskDependency,
    TaskType,
    format_elapsed,
    format_size,
)
from krok_helper.background_throttle import UiActivityGuard

#: 音频素材卡支持的格式（需求文档 §9.1，P0）。
ACCEPTED_AUDIO_EXTENSIONS = {".wav", ".flac", ".mp3", ".m4a", ".aac", ".ape", ".alac"}

_LEVEL_COLORS = {
    StateLevel.INFO: "#3a7bd5",
    StateLevel.BUSY: "#3a7bd5",
    StateLevel.SUCCESS: "#2e9e5b",
    StateLevel.WARNING: "#c07f1a",
    StateLevel.ERROR: "#d64545",
}


class CardWidget(QFrame):
    """与 ``gui_qt.CardWidget`` 同款的圆角卡片（全局 QSS 驱动配色）。"""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        radius: int = 10,
        padding: tuple[int, int, int, int] = (16, 16, 16, 16),
        spacing: int = 12,
    ) -> None:
        super().__init__(parent)
        self.setProperty("cardWidget", True)
        self.setProperty("cardRadius", radius)
        self._default_padding = padding
        self._default_spacing = spacing

    def createVBoxLayout(self) -> QVBoxLayout:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(*self._default_padding)
        layout.setSpacing(self._default_spacing)
        return layout


def _level_color(level: StateLevel) -> str:
    return _LEVEL_COLORS.get(level, _LEVEL_COLORS[StateLevel.INFO])


def _palette():
    """运行时惰性取工作台调色板（theme_workbench 要求 QApplication 已就绪）。"""
    from krok_helper.workspace_switcher import palette

    return palette()


class WizardStepper(QWidget):
    """向导步骤指示器：编号圆点 + 标题 + 连接线，已完成步骤显示对勾。

    替代原先的「第 N 步 / 共 M 步」纯文本，让用户随时看到整条流程的位置。
    """

    _DOT = 22
    _GAP = 8

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._titles: list[str] = []
        self._current = 0
        self.setMinimumHeight(self._DOT + 22)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_steps(self, titles: list[str], current: int = 0) -> None:
        self._titles = list(titles)
        self._current = current
        self.update()

    def set_current(self, index: int) -> None:
        if index != self._current:
            self._current = index
            self.update()

    def paintEvent(self, _event) -> None:
        if not self._titles:
            return
        from PyQt6.QtCore import QRectF
        from PyQt6.QtGui import QColor, QFont, QPainter

        p = _palette()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        accent = QColor(p.accent_primary)
        muted = QColor(p.text_hint)
        track = QColor(p.progress_bg)

        count = len(self._titles)
        cell = self.width() / count
        dot_y = 2.0
        centers: list[float] = [cell * (i + 0.5) for i in range(count)]

        # 连接线（先画，压在圆点下面）
        painter.setPen(Qt.PenStyle.NoPen)
        for i in range(count - 1):
            done = i < self._current
            painter.setBrush(accent if done else track)
            x1 = centers[i] + self._DOT / 2 + self._GAP
            x2 = centers[i + 1] - self._DOT / 2 - self._GAP
            if x2 > x1:
                painter.drawRoundedRect(
                    QRectF(x1, dot_y + self._DOT / 2 - 1.0, x2 - x1, 2.0), 1.0, 1.0
                )

        font = QFont(self.font())
        for i, title in enumerate(self._titles):
            cx = centers[i]
            rect = QRectF(cx - self._DOT / 2, dot_y, self._DOT, self._DOT)
            done = i < self._current
            active = i == self._current

            painter.setPen(Qt.PenStyle.NoPen)
            if done or active:
                painter.setBrush(accent)
            else:
                painter.setBrush(track)
            painter.drawEllipse(rect)

            # 编号 / 对勾
            painter.setPen(QColor("#FFFFFF") if (done or active) else muted)
            font.setPointSizeF(max(8.0, self.font().pointSizeF() - 1))
            font.setWeight(QFont.Weight.DemiBold if active else QFont.Weight.Normal)
            painter.setFont(font)
            painter.drawText(
                rect, Qt.AlignmentFlag.AlignCenter, "✓" if done else str(i + 1)
            )

            # 步骤标题
            label_font = QFont(self.font())
            label_font.setPointSizeF(max(8.0, self.font().pointSizeF() - 1))
            label_font.setWeight(QFont.Weight.DemiBold if active else QFont.Weight.Normal)
            painter.setFont(label_font)
            painter.setPen(QColor(p.text_primary) if active else muted)
            painter.drawText(
                QRectF(cx - cell / 2, dot_y + self._DOT + 3, cell, 16),
                Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
                title,
            )


class OptionCard(CardWidget):
    """单选项卡片：自绘单选圆点 + 标题 + 说明，整卡可点。

    比裸 ``RadioButton`` 有更大的点击热区，也让选项在页面里成为视觉主体。
    """

    selected = pyqtSignal()

    def __init__(
        self,
        title: str,
        description: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent, radius=8, padding=(14, 12, 14, 12), spacing=2)
        self.setObjectName("SeparationOptionCard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._checked = False

        row = QHBoxLayout(self)
        row.setContentsMargins(14, 12, 14, 12)
        row.setSpacing(12)

        self._indicator = _RadioIndicator(self)
        row.addWidget(self._indicator, 0, Qt.AlignmentFlag.AlignTop)

        text_col = QVBoxLayout()
        text_col.setContentsMargins(0, 0, 0, 0)
        text_col.setSpacing(2)
        self._title = StrongBodyLabel(title, self)
        text_col.addWidget(self._title)
        self._description = CaptionLabel(description, self)
        self._description.setWordWrap(True)
        self._description.setVisible(bool(description))
        text_col.addWidget(self._description)
        row.addLayout(text_col, 1)

    def is_checked(self) -> bool:
        return self._checked

    def set_checked(self, checked: bool) -> None:
        if self._checked == checked:
            return
        self._checked = checked
        self._indicator.set_checked(checked)
        self._apply_style()

    def set_description(self, text: str) -> None:
        self._description.setText(text)
        self._description.setVisible(bool(text))

    def _apply_style(self) -> None:
        """选中态自绘边框：全局 QSS 只管普通卡片，没有「选中」这一档。"""
        p = _palette()
        border = p.accent_primary if self._checked else p.card_border
        width = 2 if self._checked else 1
        self.setStyleSheet(
            f"#SeparationOptionCard {{ background: {p.card_bg}; "
            f"border: {width}px solid {border}; border-radius: 8px; }}"
        )

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._apply_style()

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.pos()):
            if not self._checked:
                self.selected.emit()
        super().mouseReleaseEvent(event)


class _RadioIndicator(QWidget):
    """OptionCard 左侧的自绘单选圆点（跟随主题色）。"""

    _SIZE = 18

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(self._SIZE, self._SIZE)
        self._checked = False

    def set_checked(self, checked: bool) -> None:
        self._checked = checked
        self.update()

    def paintEvent(self, _event) -> None:
        from PyQt6.QtCore import QRectF
        from PyQt6.QtGui import QColor, QPainter, QPen

        p = _palette()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(1.0, 1.0, self._SIZE - 2.0, self._SIZE - 2.0)
        if self._checked:
            accent = QColor(p.accent_primary)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(accent)
            painter.drawEllipse(rect)
            painter.setBrush(QColor("#FFFFFF"))
            painter.drawEllipse(rect.adjusted(5.0, 5.0, -5.0, -5.0))
        else:
            painter.setPen(QPen(QColor(p.input_border), 1.4))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(rect)


class InfoGrid(QWidget):
    """「标签 → 值」信息行，替代多行纯文本堆叠。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setHorizontalSpacing(14)
        self._grid.setVerticalSpacing(6)
        self._grid.setColumnStretch(1, 1)

    def set_rows(self, rows: list[tuple[str, str]]) -> None:
        # 整体重建：隐藏旧行会在 QGridLayout 里留下残余行高，导致行数变化时错位。
        while self._grid.count():
            item = self._grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

        for index, (label, value) in enumerate(rows):
            name_label = CaptionLabel(label, self)
            name_label.setMinimumWidth(96)
            value_label = BodyLabel(value, self)
            value_label.setWordWrap(True)
            self._grid.addWidget(name_label, index, 0, Qt.AlignmentFlag.AlignTop)
            self._grid.addWidget(value_label, index, 1, Qt.AlignmentFlag.AlignTop)


class HintBox(QWidget):
    """浅底提示块：左侧图标 + 若干条中文说明。"""

    def __init__(self, lines: list[str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("SeparationHintBox")
        # 裸 QWidget 不会绘制样式表里的 background，必须显式开这个属性。
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        row = QHBoxLayout(self)
        row.setContentsMargins(12, 10, 12, 10)
        row.setSpacing(10)

        self._icon = IconWidget(FIF.INFO.icon(), self)
        self._icon.setFixedSize(15, 15)
        row.addWidget(self._icon, 0, Qt.AlignmentFlag.AlignTop)

        column = QVBoxLayout()
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(4)
        self._labels: list[CaptionLabel] = []
        for line in lines:
            label = CaptionLabel(line, self)
            label.setWordWrap(True)
            column.addWidget(label)
            self._labels.append(label)
        row.addLayout(column, 1)
        self._apply_style()

    def _apply_style(self) -> None:
        p = _palette()
        self.setStyleSheet(
            f"#SeparationHintBox {{ background: {p.progress_bg}; "
            f"border: 1px solid {p.card_border}; border-radius: 8px; }}"
        )

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._apply_style()


class PillLabel(QLabel):
    """状态徽标（任务卡右上角的小胶囊）。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("SeparationPill")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)

    def set_state(self, text: str, level: StateLevel) -> None:
        color = _level_color(level)
        self.setText(text)
        self.setStyleSheet(
            f"#SeparationPill {{ color: {color}; border: 1px solid {color}; "
            f"border-radius: 9px; padding: 1px 10px; background: transparent; }}"
        )
        self.setVisible(bool(text))


class StatusActionBar(CardWidget):
    """顶部「状态与操作条」：归一化状态 + 单一主操作 + 详细信息折叠区。

    合并了需求文档 §3.3 的服务状态卡与 §3.4 的状态与操作条。
    """

    primaryRequested = pyqtSignal(str)
    secondaryRequested = pyqtSignal(str)
    settingsRequested = pyqtSignal(str)  # 携带当前状态值，供设置对话框定位

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent, padding=(16, 12, 16, 12), spacing=8)
        layout = self.createVBoxLayout()

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(12)

        self._icon = IconWidget(FIF.INFO.icon(), self)
        self._icon.setFixedSize(22, 22)
        top.addWidget(self._icon, 0, Qt.AlignmentFlag.AlignVCenter)

        text_col = QVBoxLayout()
        text_col.setContentsMargins(0, 0, 0, 0)
        text_col.setSpacing(2)
        self._state_label = StrongBodyLabel("未配置", self)
        self._detail_label = CaptionLabel("", self)
        self._detail_label.setWordWrap(True)
        text_col.addWidget(self._state_label)
        text_col.addWidget(self._detail_label)
        top.addLayout(text_col, 1)

        self._secondary_button = PushButton("", self)
        self._secondary_button.setVisible(False)
        self._secondary_button.clicked.connect(
            lambda: self.secondaryRequested.emit(self._secondary_action)
        )
        top.addWidget(self._secondary_button, 0, Qt.AlignmentFlag.AlignVCenter)

        self._primary_button = PrimaryPushButton("开始配置", self)
        self._primary_button.clicked.connect(
            lambda: self.primaryRequested.emit(self._primary_action)
        )
        top.addWidget(self._primary_button, 0, Qt.AlignmentFlag.AlignVCenter)

        self._details_button = ToolButton(FIF.CARE_DOWN_SOLID, self)
        self._details_button.setToolTip("详细信息")
        self._details_button.clicked.connect(self._toggle_details)
        top.addWidget(self._details_button, 0, Qt.AlignmentFlag.AlignVCenter)

        self._settings_button = ToolButton(FIF.SETTING, self)
        self._settings_button.setToolTip("音频分离设置")
        self._settings_button.clicked.connect(
            lambda: self.settingsRequested.emit("")
        )
        top.addWidget(self._settings_button, 0, Qt.AlignmentFlag.AlignVCenter)

        layout.addLayout(top)

        # ── 详细信息折叠区（安装位置 / 版本 / 设备 / 当前模型）────────
        self._details_panel = QFrame(self)
        details = QGridLayout(self._details_panel)
        details.setContentsMargins(34, 4, 0, 0)
        details.setHorizontalSpacing(18)
        details.setVerticalSpacing(4)
        self._detail_fields: dict[str, CaptionLabel] = {}
        for row, key in enumerate(("安装位置", "PyMSS 版本", "当前设备", "当前模型")):
            title = CaptionLabel(key, self._details_panel)
            value = CaptionLabel("—", self._details_panel)
            value.setWordWrap(True)
            details.addWidget(title, row, 0, Qt.AlignmentFlag.AlignTop)
            details.addWidget(value, row, 1)
            self._detail_fields[key] = value
        details.setColumnStretch(1, 1)
        self._details_panel.setVisible(False)
        layout.addWidget(self._details_panel)

        self._primary_action = ""
        self._secondary_action = ""

    def _toggle_details(self) -> None:
        show = not self._details_panel.isVisible()
        self._details_panel.setVisible(show)
        self._details_button.setIcon(
            FIF.CARE_UP_SOLID.icon() if show else FIF.CARE_DOWN_SOLID.icon()
        )

    def apply_snapshot(self, snapshot) -> None:
        from krok_helper.audio_processing.separation.backend import SeparationSnapshot

        assert isinstance(snapshot, SeparationSnapshot)
        meta = STATE_META[snapshot.state]
        self._state_label.setText(meta.label)
        detail = snapshot.error or meta.detail
        self._detail_label.setText(detail)
        self._detail_label.setVisible(bool(detail))

        color = _level_color(meta.level)
        self._state_label.setStyleSheet(f"color: {color};")

        self._primary_action = meta.primary_action or ""
        self._primary_button.setText(meta.primary_label or "")
        self._primary_button.setVisible(bool(meta.primary_action))
        self._secondary_action = meta.secondary_action or ""
        self._secondary_button.setText(meta.secondary_label or "")
        self._secondary_button.setVisible(bool(meta.secondary_action))

        self._detail_fields["安装位置"].setText(snapshot.install_dir or "—")
        self._detail_fields["PyMSS 版本"].setText(snapshot.pymss_version or "—")
        self._detail_fields["当前设备"].setText(snapshot.device or "—")
        self._detail_fields["当前模型"].setText(snapshot.current_model or "—")


def _tint(color: str, alpha: float) -> str:
    """把调色板里的颜色调成半透明底色，用于悬停/拖拽经过的填充。"""
    from PyQt6.QtGui import QColor

    c = QColor(color)
    return f"rgba({c.red()}, {c.green()}, {c.blue()}, {alpha:.2f})"


class _DropZoneFrame(QFrame):
    """素材卡内的拖放区：可点击选择，也可拖入音频文件。

    四档视觉状态各自可辨（§9.1）：空闲是灰色虚线；鼠标悬停加一层浅色底并把边框
    提到强调色；拖拽经过时边框加粗、底色加深，明确「松手就收」；已载入则换成实线
    强调色边框，与「还是空的」一眼可分。
    """

    clicked = pyqtSignal()
    fileDropped = pyqtSignal(str)
    #: 拖拽经过 / 离开，供素材卡同步换图标与文案。
    dragActiveChanged = pyqtSignal(bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("SeparationDropZone")
        self.setAcceptDrops(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(96)
        # 卡片被拉高时由拖放区吸收多余高度，而不是把标题和提示撑散。
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        self._hovered = False
        self._dragging = False
        self._filled = False
        self._apply_style()

    # ── 视觉状态 ────────────────────────────────────────────────
    def set_filled(self, filled: bool) -> None:
        if filled != self._filled:
            self._filled = filled
            self._apply_style()

    def _apply_style(self) -> None:
        p = _palette()
        if self._dragging:
            border, width, style = p.accent_primary, 2, "dashed"
            background = _tint(p.accent_primary, 0.16)
        elif self._filled:
            border, width, style = p.accent_primary, 1, "solid"
            background = _tint(p.accent_primary, 0.07)
        elif self._hovered:
            border, width, style = p.accent_hover, 1, "dashed"
            background = _tint(p.accent_primary, 0.07)
        else:
            # 用输入框的悬停描边色：card_border 太淡，虚线框会几乎看不见。
            border, width, style = p.input_border_hover, 1, "dashed"
            background = "transparent"
        self.setStyleSheet(
            f"#SeparationDropZone {{ border: {width}px {style} {border}; "
            f"border-radius: 8px; background: {background}; }}"
        )

    # ── 鼠标 ────────────────────────────────────────────────────
    def enterEvent(self, event) -> None:
        self._hovered = True
        self._apply_style()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hovered = False
        self._apply_style()
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)

    # ── 拖放 ────────────────────────────────────────────────────
    @staticmethod
    def _first_audio(mime) -> str:
        for url in mime.urls():
            local = url.toLocalFile()
            if local and Path(local).suffix.lower() in ACCEPTED_AUDIO_EXTENSIONS:
                return local
        return ""

    def _set_dragging(self, dragging: bool) -> None:
        if dragging == self._dragging:
            return
        self._dragging = dragging
        self._apply_style()
        self.dragActiveChanged.emit(dragging)

    def dragEnterEvent(self, event) -> None:
        if not self._first_audio(event.mimeData()):
            # 明确拒绝：光标显示禁止符号，而不是让用户以为能放。
            event.ignore()
            return
        event.acceptProposedAction()
        self._set_dragging(True)

    def dragLeaveEvent(self, event) -> None:
        self._set_dragging(False)
        super().dragLeaveEvent(event)

    def dropEvent(self, event) -> None:
        self._set_dragging(False)
        local = self._first_audio(event.mimeData())
        if local:
            event.acceptProposedAction()
            self.fileDropped.emit(local)


class AudioInputCard(CardWidget):
    """音频素材卡：点击或拖入待处理音频（需求文档 §3.4-2 / §9.1）。"""

    fileSelected = pyqtSignal(str)
    cleared = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._path = ""
        layout = self.createVBoxLayout()

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.addWidget(StrongBodyLabel("音频素材", self))
        header.addStretch(1)
        self._clear_button = ToolButton(FIF.CLOSE, self)
        self._clear_button.setToolTip("清除已选音频")
        self._clear_button.setVisible(False)
        # 隐藏时仍然占位：否则标题行会在 14px（只有文字）和 32px（按钮出现）之间跳，
        # 选中音频的一瞬间整张卡片跟着长高 18px。
        policy = self._clear_button.sizePolicy()
        policy.setRetainSizeWhenHidden(True)
        self._clear_button.setSizePolicy(policy)
        self._clear_button.clicked.connect(self.clear)
        header.addWidget(self._clear_button)
        layout.addLayout(header)

        self._drop_zone = _DropZoneFrame(self)
        zone_layout = QVBoxLayout(self._drop_zone)
        zone_layout.setContentsMargins(12, 12, 12, 12)
        zone_layout.setSpacing(4)
        self._zone_icon = IconWidget(FIF.MUSIC.icon(), self._drop_zone)
        self._zone_icon.setFixedSize(26, 26)
        zone_layout.addWidget(self._zone_icon, 0, Qt.AlignmentFlag.AlignHCenter)
        self._zone_label = BodyLabel(self._EMPTY_LABEL, self._drop_zone)
        self._zone_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        zone_layout.addWidget(self._zone_label)
        self._zone_hint = CaptionLabel(self._FORMAT_HINT, self._drop_zone)
        self._zone_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        zone_layout.addWidget(self._zone_hint)
        layout.addWidget(self._drop_zone)

        # 图标与文字不吃鼠标事件：否则鼠标划过它们时拖放区会收到 Leave，悬停态闪烁。
        for child in (self._zone_icon, self._zone_label, self._zone_hint):
            child.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        self._drop_zone.clicked.connect(self._browse)
        self._drop_zone.fileDropped.connect(self.set_path)
        self._drop_zone.dragActiveChanged.connect(
            lambda active: self._refresh_zone(dragging=active)
        )
        # 立刻刷一次：标签默认是不透明的，不抹成透明底，悬停时拖放区的浅色底会被
        # 文字行的白块盖出两道横条。
        self._refresh_zone()

        # 主题切换要重跑一次 —— ``FIF.MUSIC.icon()`` 是按调用时的主题烘好的位图，
        # 不重新取一次的话，浅色下建出来的深色音符切到深色主题后几乎看不见
        # （深色图标压在深色卡片上），得等拖拽或载入文件触发 ``_refresh_zone``
        # 才恢复。延迟调度是为了避开 SUG 主题刷新链上的 polish 重入窗口。
        from krok_helper.theme_workbench import theme as _wb_theme

        _wb_theme.changed.connect(self._on_theme_changed)

    def _on_theme_changed(self) -> None:
        from krok_helper.theme_workbench import schedule_theme_refresh

        schedule_theme_refresh(self, self._apply_theme_refresh)

    def _apply_theme_refresh(self) -> None:
        try:
            self._refresh_zone()
        except RuntimeError:  # C++ 侧已销毁
            pass

    def path(self) -> str:
        return self._path

    _EMPTY_LABEL = "点击选择文件，或拖拽音频文件到此处"
    _FORMAT_HINT = "支持 wav / flac / mp3 / m4a / aac / ape / alac"

    def _refresh_zone(self, *, dragging: bool = False) -> None:
        """按「拖拽经过 / 已载入 / 空」三种情形刷新图标与文案。

        三者都只改一个图标和两行文字、不增删控件，卡片高度因此不会跳。
        """
        from PyQt6.QtGui import QColor

        accent = _palette().accent_primary
        # 一给标签设 QSS，qfluentwidgets 原本的透明底就没了，会露出一块白底；
        # 因此每一档都显式写上 background: transparent。
        emphasis = f"color: {accent}; font-weight: 600; background: transparent;"
        plain = "background: transparent;"
        self._zone_hint.setStyleSheet(plain)
        if dragging:
            self._zone_icon.setIcon(FIF.DOWNLOAD.icon(color=QColor(accent)))
            self._zone_label.setText("松开即可载入")
            self._zone_label.setStyleSheet(emphasis)
            self._zone_hint.setText(self._FORMAT_HINT)
        elif self._path:
            # 载入后整块换装：对勾图标 + 强调色文件名，跟「还是空的」一眼可分。
            self._zone_icon.setIcon(FIF.COMPLETED.icon(color=QColor(accent)))
            self._zone_label.setText(Path(self._path).name)
            self._zone_label.setStyleSheet(emphasis)
            self._zone_hint.setText(self._path)
        else:
            self._zone_icon.setIcon(FIF.MUSIC.icon())
            self._zone_label.setText(self._EMPTY_LABEL)
            self._zone_label.setStyleSheet(plain)
            self._zone_hint.setText(self._FORMAT_HINT)

    def set_path(self, path: str, *, emit: bool = True) -> None:
        self._path = path
        has = bool(path)
        self._clear_button.setVisible(has)
        self._drop_zone.set_filled(has)
        self._refresh_zone()
        if emit and has:
            self.fileSelected.emit(path)

    def clear(self) -> None:
        if not self._path:
            return
        self.set_path("", emit=False)
        self.cleared.emit()

    def _browse(self) -> None:
        filters = "音频文件 (*.wav *.flac *.mp3 *.m4a *.aac *.ape *.alac)"
        path, _ = QFileDialog.getOpenFileName(self, "选择待处理音频", "", filters)
        if path:
            self.set_path(path)


class OutputSettingsCard(CardWidget):
    """输出设置卡：输出目录与输出格式（需求文档 §3.4-2 / §9.2）。"""

    outputDirChanged = pyqtSignal(str)
    formatChanged = pyqtSignal(str)

    FORMATS = (("WAV（无损）", "wav"), ("FLAC（无损）", "flac"))

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = self.createVBoxLayout()

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.addWidget(StrongBodyLabel("输出设置", self))
        header.addStretch(1)
        self._open_dir_button = ToolButton(FIF.FOLDER, self)
        self._open_dir_button.setToolTip("打开输出目录")
        self._open_dir_button.setEnabled(False)
        self._open_dir_button.clicked.connect(self._open_output_dir)
        header.addWidget(self._open_dir_button)
        layout.addLayout(header)

        dir_row = QHBoxLayout()
        dir_row.setContentsMargins(0, 0, 0, 0)
        dir_row.setSpacing(8)
        self._dir_edit = LineEdit(self)
        self._dir_edit.setReadOnly(True)
        self._dir_edit.setPlaceholderText("选择输出目录（默认同素材目录）")
        dir_row.addWidget(self._dir_edit, 1)
        browse = PushButton(FIF.FOLDER, "浏览", self)
        browse.clicked.connect(self._browse)
        dir_row.addWidget(browse)
        layout.addLayout(dir_row)

        format_row = QHBoxLayout()
        format_row.setContentsMargins(0, 0, 0, 0)
        format_row.setSpacing(8)
        format_row.addWidget(CaptionLabel("输出格式", self))
        self._format_combo = ComboBox(self)
        for label, _value in self.FORMATS:
            self._format_combo.addItem(label)
        self._format_combo.setMinimumWidth(140)
        self._format_combo.currentIndexChanged.connect(
            lambda _i: self.formatChanged.emit(self.output_format())
        )
        format_row.addWidget(self._format_combo)
        format_row.addStretch(1)
        layout.addLayout(format_row)
        # 与素材卡等高后多出来的高度收在底部，内容保持顶对齐。
        layout.addStretch(1)

    def output_dir(self) -> str:
        return self._dir_edit.text().strip()

    def set_output_dir(self, path: str, *, emit: bool = True) -> None:
        self._dir_edit.setText(path)
        self._open_dir_button.setEnabled(bool(path))
        if emit:
            self.outputDirChanged.emit(path)

    def output_format(self) -> str:
        index = self._format_combo.currentIndex()
        return self.FORMATS[index][1] if 0 <= index < len(self.FORMATS) else "wav"

    def set_output_format(self, fmt: str) -> None:
        for i, (_label, value) in enumerate(self.FORMATS):
            if value == fmt:
                self._format_combo.setCurrentIndex(i)
                return

    def _browse(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择输出目录")
        if path:
            self.set_output_dir(path)

    def _open_output_dir(self) -> None:
        if self.output_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(self.output_dir()))


class _CheckIndicator(QWidget):
    """任务卡左侧的自绘复选框（跟随主题色）。"""

    _SIZE = 18

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(self._SIZE, self._SIZE)
        self._checked = False
        self._enabled = True

    def set_checked(self, checked: bool) -> None:
        self._checked = checked
        self.update()

    def set_enabled_look(self, enabled: bool) -> None:
        self._enabled = enabled
        self.update()

    def paintEvent(self, _event) -> None:
        from PyQt6.QtCore import QRectF
        from PyQt6.QtGui import QColor, QPainter, QPen

        p = _palette()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(1.0, 1.0, self._SIZE - 2.0, self._SIZE - 2.0)
        radius = 4.0
        if self._checked and self._enabled:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(p.accent_primary))
            painter.drawRoundedRect(rect, radius, radius)
            pen = QPen(QColor("#FFFFFF"), 2.0)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)
            painter.drawLine(
                int(rect.left() + 4), int(rect.center().y()),
                int(rect.center().x() - 1), int(rect.bottom() - 5),
            )
            painter.drawLine(
                int(rect.center().x() - 1), int(rect.bottom() - 5),
                int(rect.right() - 4), int(rect.top() + 5),
            )
        else:
            painter.setPen(QPen(QColor(p.input_border if self._enabled else p.text_disabled), 1.4))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(rect, radius, radius)


class TaskCard(CardWidget):
    """任务卡（需求文档 §3.4-3）：可勾选，多选后由底部操作栏统一执行。

    卡片本体即选择区；能否勾选与原因一律直接用中文说明，不只是置灰。
    """

    selectionChanged = pyqtSignal()

    def __init__(self, task: TaskType, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.task = task
        spec = TASK_SPECS[task]
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._selected = False
        self._selectable = False
        self._download_bytes = 0

        layout = self.createVBoxLayout()
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(10)
        self._check = _CheckIndicator(self)
        header.addWidget(self._check, 0, Qt.AlignmentFlag.AlignVCenter)
        icon = IconWidget(spec.icon.icon(), self)
        icon.setFixedSize(26, 26)
        header.addWidget(icon, 0, Qt.AlignmentFlag.AlignVCenter)
        header.addWidget(StrongBodyLabel(spec.title, self), 0, Qt.AlignmentFlag.AlignVCenter)
        header.addStretch(1)
        self._pill = PillLabel(self)
        header.addWidget(self._pill, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addLayout(header)

        desc = BodyLabel(spec.description, self)
        desc.setWordWrap(True)
        layout.addWidget(desc)

        outputs = CaptionLabel(spec.expected_outputs, self)
        outputs.setWordWrap(True)
        layout.addWidget(outputs)

        # 原因行始终占位（哪怕没内容）：否则状态切换会让整张卡忽高忽低。
        self._reason = CaptionLabel("", self)
        self._reason.setWordWrap(True)
        self._reason.setMinimumHeight(self._reason.fontMetrics().lineSpacing() * 2)
        self._reason.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        layout.addWidget(self._reason)
        layout.addStretch(1)
        self._apply_look()

    # ── 选择状态 ─────────────────────────────────────────────────
    def is_selected(self) -> bool:
        return self._selected and self._selectable

    def is_selectable(self) -> bool:
        return self._selectable

    def download_bytes(self) -> int:
        return self._download_bytes if self.is_selected() else 0

    def set_selected(self, selected: bool, *, emit: bool = True) -> None:
        target = bool(selected) and self._selectable
        if target == self._selected:
            return
        self._selected = target
        self._apply_look()
        if emit:
            self.selectionChanged.emit()

    def _apply_look(self) -> None:
        self._check.set_checked(self._selected)
        self._check.set_enabled_look(self._selectable)
        self.setCursor(
            Qt.CursorShape.PointingHandCursor
            if self._selectable
            else Qt.CursorShape.ArrowCursor
        )
        p = _palette()
        border = p.accent_primary if self.is_selected() else p.card_border
        width = 2 if self.is_selected() else 1
        self.setObjectName("SeparationTaskCard")
        self.setStyleSheet(
            f"#SeparationTaskCard {{ background: {p.card_bg}; "
            f"border: {width}px solid {border}; border-radius: 10px; }}"
        )

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._apply_look()

    def mouseReleaseEvent(self, event) -> None:
        if (
            self._selectable
            and event.button() == Qt.MouseButton.LeftButton
            and self.rect().contains(event.pos())
        ):
            self.set_selected(not self._selected)
        super().mouseReleaseEvent(event)

    def _set_reason(self, text: str, *, level: StateLevel | None = None) -> None:
        """设置卡片底部那行说明；``level`` 非空时按状态色强调。

        必须走同一个入口：只在某一条分支里 setStyleSheet，红色会残留到后面的状态上。
        另外给 qfluent 标签设 QSS 会顶掉它原本的透明底，得显式写回去。
        """
        self._reason.setText(text)
        if level is None:
            self._reason.setStyleSheet("background: transparent;")
        else:
            self._reason.setStyleSheet(
                f"color: {_level_color(level)}; background: transparent;"
            )

    # ── 依赖与队列状态 ───────────────────────────────────────────
    def set_dependency(
        self,
        dep: TaskDependency,
        *,
        service_ready: bool,
        unavailable_reason: str = "",
        queue_label: str = "",
    ) -> None:
        """刷新卡片。``queue_label`` 非空表示这张卡正在跑或已排队，禁止改选。"""
        self._download_bytes = 0
        if queue_label:
            self._pill.set_state(queue_label, StateLevel.BUSY)
            self._set_reason("任务进行中，无法更改选择")
            self._selectable = False
            self._apply_look()
            return
        if not service_ready:
            self._pill.set_state("", StateLevel.INFO)
            # 服务没起时三张卡全都点不动，这行是唯一的解释，用错误色让它显眼。
            self._set_reason("需要先启动服务", level=StateLevel.ERROR)
            self._selectable = False
            self._selected = False
            self._apply_look()
            return
        if unavailable_reason:
            self._pill.set_state(dep.badge or "不可用", StateLevel.INFO)
            self._set_reason(unavailable_reason)
            self._selectable = False
            self._selected = False
            self._apply_look()
            return
        if dep.ready:
            self._pill.set_state(dep.badge or "就绪", StateLevel.SUCCESS)
            self._set_reason("")
            self._selectable = True
        elif dep.download_bytes > 0:
            self._pill.set_state(dep.badge, StateLevel.WARNING)
            self._set_reason(dep.reason or "开始时会先下载所需模型")
            self._download_bytes = dep.download_bytes
            self._selectable = True
        else:
            self._pill.set_state(dep.badge or "不可用", StateLevel.ERROR)
            self._set_reason(dep.reason or "当前不可用")
            self._selectable = False
            self._selected = False
        self._apply_look()


class CurrentTaskPanel(CardWidget):
    """当前任务区（需求文档 §3.4-4）：真实阶段 + 已用时间 + 取消/停止。"""

    cancelRequested = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        from krok_helper.audio_processing.separation.states import TASK_STAGES

        layout = self.createVBoxLayout()

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        self._title = StrongBodyLabel("当前任务", self)
        header.addWidget(self._title)
        header.addStretch(1)
        self._elapsed_label = CaptionLabel("已用时 00:00", self)
        header.addWidget(self._elapsed_label)
        self._cancel_button = PushButton(FIF.CLOSE, "停止任务", self)
        self._cancel_button.clicked.connect(self.cancelRequested.emit)
        header.addWidget(self._cancel_button)
        layout.addLayout(header)

        self._stage_labels: list[BodyLabel] = []
        self._stage_dots: list[QLabel] = []
        self._stages_col = QVBoxLayout()
        self._stages_col.setContentsMargins(2, 0, 2, 0)
        self._stages_col.setSpacing(4)
        layout.addLayout(self._stages_col)
        self.set_stage_names(TASK_STAGES)

        self._busy_bar = IndeterminateProgressBar(self)
        layout.addWidget(self._busy_bar)

        self._download_row = QWidget(self)
        download_layout = QHBoxLayout(self._download_row)
        download_layout.setContentsMargins(0, 0, 0, 0)
        download_layout.setSpacing(8)
        self._download_bar = ProgressBar(self._download_row)
        self._download_bar.setRange(0, 1000)
        download_layout.addWidget(self._download_bar, 1)
        self._download_text = CaptionLabel("", self._download_row)
        download_layout.addWidget(self._download_text)
        self._download_row.setVisible(False)
        layout.addWidget(self._download_row)

        self._file_label = CaptionLabel("", self)
        self._file_label.setWordWrap(True)
        self._file_label.setVisible(False)
        layout.addWidget(self._file_label)

        self._elapsed = QElapsedTimer()
        self._elapsed_timer = QTimer(self)
        self._elapsed_timer.setInterval(500)
        self._elapsed_timer.timeout.connect(self._update_elapsed)
        self._current_stage = -1

        # qfluentwidgets 的 IndeterminateProgressBar 构造即启动无限循环动画；
        # 空闲（无任务）时必须停掉，任务/面板可见性再交给节流 guard。
        self._busy_bar.stop()
        self._ui_guard = UiActivityGuard(self)
        self._busy_animation = self._ui_guard.manage_animation(self._busy_bar)
        self._elapsed_activity = self._ui_guard.manage(
            self._elapsed_timer, on_resume=self._update_elapsed
        )

    def set_stage_names(self, names) -> None:
        """重建阶段行（分离任务为六阶段；修复安装等复用时可传入自定义阶段）。"""
        while self._stages_col.count():
            item = self._stages_col.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._stage_dots = []
        self._stage_labels = []
        for name in names:
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(8)
            dot = QLabel("○", self)
            dot.setFixedWidth(16)
            dot.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label = BodyLabel(name, self)
            row.addWidget(dot)
            row.addWidget(label)
            row.addStretch(1)
            container = QWidget(self)
            container.setLayout(row)
            self._stages_col.addWidget(container)
            self._stage_dots.append(dot)
            self._stage_labels.append(label)
        self._current_stage = -1

    def set_title(self, title: str) -> None:
        """只换标题，不重置阶段与计时——队列换到下一个任务时用。"""
        self._title.setText(f"当前任务：{title}")

    def start(self, title: str) -> None:
        self._title.setText(f"当前任务：{title}")
        self._current_stage = -1
        self._busy_bar.setVisible(True)
        self._download_row.setVisible(False)
        self._elapsed.start()
        self._elapsed_activity.set_desired(True)
        self._busy_animation.set_desired(True)
        self._update_elapsed()
        self._refresh_stages(0)
        self.show()

    def update_progress(self, progress) -> None:
        self._refresh_stages(progress.stage_index)
        determinate = progress.is_download_stage or progress.is_processing_stage
        self._busy_animation.set_desired(not determinate)
        self._busy_bar.setVisible(not determinate)
        self._download_row.setVisible(determinate)
        if progress.is_download_stage and progress.download_total > 0:
            ratio = progress.download_done / progress.download_total
            self._download_bar.setValue(int(ratio * 1000))
            self._download_text.setText(
                f"{format_size(progress.download_done)} / {format_size(progress.download_total)}"
            )
        elif progress.is_processing_stage and progress.processing_total > 0:
            ratio = progress.processing_done / progress.processing_total
            self._download_bar.setValue(int(ratio * 1000))
            self._download_text.setText(
                f"已处理 {format_elapsed(progress.processing_done)} / "
                f"{format_elapsed(progress.processing_total)}（{ratio:.0%}）"
            )
        self._file_label.setVisible(bool(progress.current_file))
        if progress.current_file:
            self._file_label.setText(f"当前文件：{progress.current_file}")

    def stop(self) -> None:
        self._elapsed_activity.set_desired(False)
        self._busy_animation.set_desired(False)

    def _update_elapsed(self) -> None:
        self._elapsed_label.setText(f"已用时 {format_elapsed(self._elapsed.elapsed() / 1000)}")

    def _refresh_stages(self, current: int) -> None:
        if current == self._current_stage:
            return
        self._current_stage = current
        for i, (dot, label) in enumerate(zip(self._stage_dots, self._stage_labels)):
            if i < current:
                dot.setText("●")
                dot.setStyleSheet("color: #2e9e5b;")
                label.setStyleSheet("")
            elif i == current:
                dot.setText("●")
                dot.setStyleSheet("color: #3a7bd5;")
                label.setStyleSheet("font-weight: 600;")
            else:
                dot.setText("○")
                dot.setStyleSheet("color: rgba(128,128,128,0.8);")
                label.setStyleSheet("")


class ResultsPanel(CardWidget):
    """结果区（需求文档 §3.4-5）：按任务分组，提供试听/打开/复制路径。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._groups: list[QFrame] = []
        self._player = None
        self._audio_output = None
        self._playing_button: PushButton | None = None

        layout = self.createVBoxLayout()
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.addWidget(StrongBodyLabel("分离结果", self))
        header.addStretch(1)
        self._clear_button = PushButton(FIF.DELETE, "清空", self)
        self._clear_button.clicked.connect(self.clear_results)
        header.addWidget(self._clear_button)
        layout.addLayout(header)

        self._list_col = QVBoxLayout()
        self._list_col.setContentsMargins(0, 0, 0, 0)
        self._list_col.setSpacing(10)
        layout.addLayout(self._list_col)

        self._empty_hint = CaptionLabel("暂无结果，完成的任务会显示在这里。", self)
        self._list_col.addWidget(self._empty_hint)

    def add_result(self, result) -> None:
        self._empty_hint.setVisible(False)
        group = QFrame(self)
        group_layout = QVBoxLayout(group)
        group_layout.setContentsMargins(0, 0, 0, 0)
        group_layout.setSpacing(4)

        failed = bool(getattr(result, "error", ""))
        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        mark = "✗" if failed else "✓"
        head.addWidget(BodyLabel(f"{mark} {result.title} · {result.finished_at}", group))
        head.addStretch(1)
        remove_button = ToolButton(FIF.CLOSE, group)
        remove_button.setToolTip("移除该组结果（不删除文件）")
        remove_button.clicked.connect(lambda: self._remove_group(group))
        head.addWidget(remove_button)
        group_layout.addLayout(head)

        if failed:
            # 队列里失败的任务也记一条，否则用户只看到「少了一个结果」。
            reason = CaptionLabel(str(result.error), group)
            reason.setWordWrap(True)
            reason.setStyleSheet(f"color: {_level_color(StateLevel.ERROR)};")
            group_layout.addWidget(reason)
        for file in result.files:
            group_layout.addLayout(self._build_file_row(group, file))
        self._list_col.addWidget(group)
        self._groups.append(group)

    def clear_results(self) -> None:
        for group in list(self._groups):
            self._remove_group(group)
        self._empty_hint.setVisible(True)

    def group_count(self) -> int:
        return len(self._groups)

    def _remove_group(self, group: QFrame) -> None:
        if group in self._groups:
            self._groups.remove(group)
        self._list_col.removeWidget(group)
        group.deleteLater()
        if not self._groups:
            self._empty_hint.setVisible(True)

    def _build_file_row(self, parent: QWidget, file) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setContentsMargins(8, 0, 0, 0)
        row.setSpacing(6)

        pill = PillLabel(parent)
        pill.set_state(file.label, StateLevel.INFO)
        row.addWidget(pill)

        name = BodyLabel(Path(file.path).name, parent)
        name.setWordWrap(False)
        row.addWidget(name, 1)

        size = CaptionLabel(format_size(file.size_bytes), parent)
        row.addWidget(size)

        play_button = PushButton(FIF.PLAY, "试听", parent)
        play_button.clicked.connect(lambda: self._toggle_play(file.path, play_button))
        row.addWidget(play_button)

        open_file = ToolButton(FIF.DOCUMENT, parent)
        open_file.setToolTip("打开文件")
        open_file.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(file.path)))
        row.addWidget(open_file)

        open_dir = ToolButton(FIF.FOLDER, parent)
        open_dir.setToolTip("打开所在目录")
        open_dir.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(file.path).parent)))
        )
        row.addWidget(open_dir)

        copy_path = ToolButton(FIF.COPY, parent)
        copy_path.setToolTip("复制路径")
        copy_path.clicked.connect(
            lambda: QApplication.clipboard().setText(file.path)
        )
        row.addWidget(copy_path)
        return row

    def _toggle_play(self, path: str, button: PushButton) -> None:
        if self._playing_button is button and self._player is not None:
            self._stop_play()
            return
        self._stop_play()
        try:
            from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer
        except Exception:
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))
            return
        if self._player is None:
            self._audio_output = QAudioOutput(self)
            self._player = QMediaPlayer(self)
            self._player.setAudioOutput(self._audio_output)
            self._player.playbackStateChanged.connect(self._on_playback_state)
        self._player.setSource(QUrl.fromLocalFile(path))
        self._player.play()
        self._playing_button = button
        button.setText("停止")
        button.setIcon(FIF.PAUSE.icon())

    def _on_playback_state(self, state) -> None:
        from PyQt6.QtMultimedia import QMediaPlayer

        if state == QMediaPlayer.PlaybackState.StoppedState:
            self._reset_play_button()

    def _stop_play(self) -> None:
        if self._player is not None:
            self._player.stop()
        self._reset_play_button()

    def _reset_play_button(self) -> None:
        if self._playing_button is not None:
            self._playing_button.setText("试听")
            self._playing_button.setIcon(FIF.PLAY.icon())
            self._playing_button = None
