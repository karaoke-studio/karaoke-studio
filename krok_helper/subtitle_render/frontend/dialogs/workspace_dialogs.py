"""Workspace-level dialogs shared by the subtitle render frontend."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from PyQt6.QtCore import QPoint, Qt, pyqtSignal as Signal
from PyQt6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PyQt6.QtWidgets import (
    QFormLayout,
    QHBoxLayout,
    QListWidgetItem,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CheckBox,
    ComboBox as FluentComboBox,
    ListWidget as FluentListWidget,
    PrimaryPushButton as FluentPrimaryPushButton,
    PushButton as FluentPushButton,
    SpinBox as FluentSpinBox,
    StrongBodyLabel,
)

from krok_helper.qfluent_compat import ModelessDialog
from krok_helper.subtitle_render.domain.timing import SubtitleLoadingSettings
from krok_helper.subtitle_render.engine.layout.display.diagnostics import (
    LayoutMarginWarning,
    LayoutTimingDiagnostic,
)
from krok_helper.subtitle_render.frontend.dialogs.fluent_dialogs import (
    fluent_button_row,
)
from krok_helper.subtitle_render.frontend.widgets.theme import palette, themed

def layout_issue_icon() -> QIcon:
    """Return the outlined warning-triangle icon used by the issue entry."""
    pixmap = QPixmap(24, 24)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    color = QColor(palette().text_primary)
    painter.setPen(QPen(color, 1.7, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    path = QPainterPath()
    path.moveTo(12, 2.8)
    path.lineTo(22, 20.5)
    path.lineTo(2, 20.5)
    path.closeSubpath()
    painter.drawPath(path)
    painter.drawLine(12, 8, 12, 15)
    painter.setBrush(color)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawEllipse(11, 17, 2, 2)
    painter.end()
    return QIcon(pixmap)

@dataclass(frozen=True)
class LayoutIssue:
    """One persistent layout issue, including its subtitle-source identity."""

    track_index: int
    source_name: str
    warning: LayoutMarginWarning

@dataclass(frozen=True)
class TimingIssue:
    """One timing/page-placement diagnostic for a subtitle source."""

    track_index: int
    source_name: str
    diagnostic: LayoutTimingDiagnostic

class LayoutIssuesDialog(ModelessDialog):
    """Modeless, clickable list of the current lyrics layout issues."""

    issueActivated = Signal(int, int)

    def __init__(
        self,
        issues: list[LayoutIssue | TimingIssue],
        parent: Optional[QWidget] = None,
    ) -> None:
        anchor = parent.window() if parent is not None else None
        super().__init__(anchor)
        self.setObjectName("LayoutIssuesDialog")
        self.setWindowTitle("当前字幕诊断")
        self.setMinimumSize(620, 360)
        self.resize(720, 440)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(10)
        layout.addWidget(StrongBodyLabel("当前字幕渲染计划诊断", self))
        self._summary_label = CaptionLabel("", self)
        self._summary_label.setWordWrap(True)
        layout.addWidget(self._summary_label)

        self._list_widget = FluentListWidget(self)
        self._list_widget.setObjectName("LayoutIssuesList")
        self._list_widget.setWordWrap(False)
        self._list_widget.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._list_widget.itemClicked.connect(self._on_item_clicked)
        layout.addWidget(self._list_widget, 1)

        self._detail = QPlainTextEdit(self)
        self._detail.setObjectName("LayoutIssueDetail")
        self._detail.setReadOnly(True)
        self._detail.setMinimumHeight(150)
        self._detail.setPlaceholderText("选择一项查看最终渲染计划的详细计算过程")
        layout.addWidget(self._detail)

        button_row = QHBoxLayout()
        button_row.addStretch(1)
        close_button = FluentPushButton("关闭", self)
        close_button.clicked.connect(self.close)
        button_row.addWidget(close_button)
        layout.addLayout(button_row)

        themed(
            self,
            lambda: (
                f"#LayoutIssuesDialog {{ background: {palette().panel_bg}; "
                f"color: {palette().text_primary}; }}"
            ),
        )
        self.set_issues(issues)

    def set_issues(self, issues: list[LayoutIssue | TimingIssue]) -> None:
        """Replace the list while the dialog is open."""
        self._list_widget.clear()
        overflow_count = sum(
            isinstance(issue, LayoutIssue) and issue.warning.level == "overflow"
            for issue in issues
        )
        margin_count = sum(
            isinstance(issue, LayoutIssue) and issue.warning.level == "margin"
            for issue in issues
        )
        timing_count = sum(
            isinstance(issue, TimingIssue)
            and issue.diagnostic.kind == "timing"
            for issue in issues
        )
        shift_count = sum(
            isinstance(issue, TimingIssue)
            and issue.diagnostic.kind in {"page_shift", "force_bottom_shift"}
            for issue in issues
        )
        parts = []
        if overflow_count:
            parts.append(f"{overflow_count} 行超出画面")
        if margin_count:
            parts.append(f"{margin_count} 行侵入左右余白")
        if timing_count:
            parts.append(f"{timing_count} 行发生时间压缩")
        if shift_count:
            parts.append(f"{shift_count} 组碰撞触发页面避让")
        summary = "、".join(parts) if parts else "未发现字幕布局或时间问题"
        self._summary_label.setText(f"{summary}。点击任一项可跳转歌词与预览。")
        for issue in issues:
            if isinstance(issue, TimingIssue):
                diagnostic = issue.diagnostic
                primary_line = diagnostic.line_indices[-1]
                display = (
                    f"{issue.source_name} · {diagnostic.title}　"
                    f"{diagnostic.summary}"
                )
                detail = f"{issue.source_name}\n{diagnostic.detail}"
            else:
                warning = issue.warning
                primary_line = warning.line_index
                kind = (
                    "字幕溢出画面"
                    if warning.level == "overflow"
                    else "左右余白无法确保"
                )
                text = " ".join(warning.text.split()) or "（空歌词）"
                display = (
                    f"{issue.source_name} · 第 {warning.line_index + 1} 行　"
                    f"{kind}　{text}"
                )
                detail = (
                    f"{issue.source_name} · 第 {warning.line_index + 1} 行\n"
                    f"{kind}\n{text}\n"
                    f"测得范围：left={warning.left:.1f}, right={warning.right:.1f}"
                )
            item = QListWidgetItem(display)
            item.setData(
                Qt.ItemDataRole.UserRole,
                (issue.track_index, primary_line),
            )
            item.setData(Qt.ItemDataRole.UserRole + 1, detail)
            item.setToolTip(detail)
            self._list_widget.addItem(item)
        if self._list_widget.count():
            first = self._list_widget.item(0)
            self._list_widget.setCurrentItem(first)
            self._detail.setPlainText(
                str(first.data(Qt.ItemDataRole.UserRole + 1) or "")
            )
        else:
            self._detail.clear()

    def _on_item_clicked(self, item: QListWidgetItem) -> None:
        self._detail.setPlainText(
            str(item.data(Qt.ItemDataRole.UserRole + 1) or "")
        )
        target = item.data(Qt.ItemDataRole.UserRole)
        if (
            isinstance(target, tuple)
            and len(target) == 2
            and all(isinstance(value, int) for value in target)
        ):
            self.issueActivated.emit(target[0], target[1])

class GuideSymbolSettingsDialog(ModelessDialog):
    """Configure how many inline guide glyphs precede a lyric line."""

    def __init__(
        self,
        *,
        count: int = 1,
        interval_ms: int = 1000,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent.window() if parent is not None else None)
        self.setWindowTitle("导唱符设置")
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.setMinimumWidth(460)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(10)

        layout.addWidget(BodyLabel("插入的导唱符数量：", self))
        self.count_spin = FluentSpinBox(self)
        self.count_spin.setRange(1, 32)
        self.count_spin.setValue(max(1, min(int(count), 32)))
        layout.addWidget(self.count_spin)

        layout.addWidget(BodyLabel("每个导唱符距离后一个字符多少毫秒：", self))
        self.interval_spin = FluentSpinBox(self)
        self.interval_spin.setRange(0, 10_000)
        self.interval_spin.setSingleStep(50)
        self.interval_spin.setValue(max(0, min(int(interval_ms), 10_000)))
        self.interval_spin.selectAll()
        layout.addWidget(self.interval_spin)

        hint = CaptionLabel(
            "例如数量为 3、间隔为 1000ms，会在歌词首字前 3000、2000、1000ms 依次走字。",
            self,
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)
        button_row, self.ok_button, _cancel_button = fluent_button_row(self)
        layout.addLayout(button_row)

    def settings(self) -> tuple[int, int]:
        return int(self.count_spin.value()), int(self.interval_spin.value())

class SubtitleLoadingSettingsDialog(ModelessDialog):
    """Source-loading settings card, positioned to the right of its gear button."""

    def __init__(
        self,
        *,
        mode: str,
        effective: SubtitleLoadingSettings,
        global_defaults: SubtitleLoadingSettings,
        anchor: Optional[QWidget],
        parent: QWidget,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("加载字幕设置")
        self.setModal(False)
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.setMinimumWidth(390)
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(14)
        title = StrongBodyLabel("加载字幕设置", self)
        root.addWidget(title)
        hint = CaptionLabel(
            "这些设置控制字幕如何分段、分页，以及读取 .sug 项目时是否应用"
            "打轴模块的软件导出补偿；与渲染样式隔离。",
            self,
        )
        hint.setWordWrap(True)
        root.addWidget(hint)

        form = QFormLayout()
        form.setSpacing(10)
        self._mode_combo = FluentComboBox(self)
        self._mode_combo.addItem("使用全局加载设置", userData="global")
        self._mode_combo.addItem("为此字幕源单独设置", userData="custom")
        self._mode_combo.setToolTip(
            "“使用全局加载设置”会修改应用级默认值，并自动刷新所有跟随全局设置的"
            "字幕源；“为此字幕源单独设置”只保存和刷新当前主字幕或副字幕源。"
            "两种模式都只影响分段、分页，不改变字体、颜色和画面布局参数。"
        )
        self._mode_combo.setCurrentIndex(0 if mode == "global" else 1)
        form.addRow("设置范围", self._mode_combo)

        self._gap_enabled = CheckBox("按演唱空隙分段", self)
        self._gap_enabled.setToolTip(
            "比较相邻两句的真实演唱时间。当“下一句开始时间 − 上一句结束时间”"
            "大于设定值时，从下一句开始新段落。提前显示、结束延时和动画时长"
            "不参与计算。保存加载设置或点击刷新后重新计算，并覆盖现有手工分页。"
        )
        form.addRow("", self._gap_enabled)
        self._gap_spin = FluentSpinBox(self)
        self._gap_spin.setRange(0, 120_000)
        self._gap_spin.setSingleStep(100)
        self._gap_spin.setSuffix(" ms")
        self._gap_spin.setToolTip(
            "自动分段使用的演唱空隙阈值，单位为毫秒。例如上一句在 10.000 秒结束、"
            "下一句在 14.500 秒开始，空隙为 4500 毫秒；阈值为 4000 毫秒时会"
            "开始新段落。仅在“按演唱空隙分段”启用时生效。"
        )
        form.addRow("分段间隔", self._gap_spin)
        self._blank_enabled = CheckBox("空行开始新段落", self)
        self._blank_enabled.setToolTip(
            "启用后，字幕源中的一个或多个连续空行会在下一条有效歌词前开始新段落。"
            "空行不占用字幕轨道，也不计入每页行数。关闭后，空行不影响段落和分页，"
            "也不会在歌词表中单独占一行。"
        )
        form.addRow("", self._blank_enabled)
        self._rows_spin = FluentSpinBox(self)
        self._rows_spin.setRange(1, 4)
        self._rows_spin.setToolTip(
            "在每个段落内按照指定行数依次建立页面，范围为 1～4。源文件中的显式"
            "分页仍会提前结束当前页；段落最后一页或显式分页前的页面允许不足指定"
            "行数，但仍使用基础行数对应的项目默认布局。例如基础行数为 3 时，只有"
            "1 行或 2 行的尾页也使用 3 行默认布局。"
        )
        form.addRow("每页基础行数", self._rows_spin)
        self._actual_rows_layout = CheckBox("根据实际行数分配布局", self)
        self._actual_rows_layout.setToolTip(
            "默认关闭。启用后，每页会根据实际歌词行数使用对应的项目默认布局；例如基础行数为 3 时，"
            "只有 1 行或 2 行的尾页会分别使用 1 行或 2 行默认布局。基础行数仍用于限制每页最多行数。"
        )
        form.addRow("", self._actual_rows_layout)
        self._sug_offset_check = CheckBox("读取 .sug 时应用打轴模块的软件导出补偿", self)
        self._sug_offset_check.setToolTip(
            "「软件导出补偿」是打轴模块「设置 → 导出」里的项目，SUG 只在导出"
            "（除 .sug 外的所有格式）时把它叠加到时间戳上，.sug 本体不含此补偿。"
            "启用后读取 .sug 会按打轴模块当前的补偿值平移时间轴，与 SUG 导出"
            " LRC 的结果一致（负值=提前，正值=延后，不早于 0 秒）。补偿叠加在"
            " .sug 记录的导出偏移之上，且不影响 LRC 的 @Offset 标签和渲染属性"
            "里时间轴的「偏移」字段。保存后会重新读取字幕文件并刷新段落和页面；"
            "对 .lrc 字幕源无影响。"
        )
        form.addRow("", self._sug_offset_check)
        root.addLayout(form)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = FluentPushButton("取消", self)
        save = FluentPrimaryPushButton("保存并刷新", self)
        cancel.clicked.connect(self.reject)
        save.clicked.connect(self.accept)
        buttons.addWidget(cancel)
        buttons.addWidget(save)
        root.addLayout(buttons)

        self._global_defaults = global_defaults
        self._custom_draft = effective
        self._mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        self._set_values(global_defaults if mode == "global" else effective)
        self.adjustSize()
        if anchor is not None:
            point = anchor.mapToGlobal(QPoint(anchor.width() + 8, 0))
            self.move(point)

    def _set_values(self, settings: SubtitleLoadingSettings) -> None:
        self._gap_enabled.setChecked(settings.time_gap_section_enabled)
        self._gap_spin.setValue(settings.section_gap_ms)
        self._blank_enabled.setChecked(settings.blank_line_section_enabled)
        self._rows_spin.setValue(settings.rows_per_page)
        self._actual_rows_layout.setChecked(settings.allocate_layout_by_actual_rows)
        self._sug_offset_check.setChecked(settings.apply_sug_export_compensation)

    def _current_values(self) -> SubtitleLoadingSettings:
        return SubtitleLoadingSettings(
            time_gap_section_enabled=self._gap_enabled.isChecked(),
            section_gap_ms=self._gap_spin.value(),
            blank_line_section_enabled=self._blank_enabled.isChecked(),
            rows_per_page=self._rows_spin.value(),
            allocate_layout_by_actual_rows=self._actual_rows_layout.isChecked(),
            apply_sug_export_compensation=self._sug_offset_check.isChecked(),
        )

    def _on_mode_changed(self, _index: int) -> None:
        mode = str(self._mode_combo.currentData() or "global")
        if mode == "custom":
            self._set_values(self._custom_draft)
        else:
            self._custom_draft = self._current_values()
            self._set_values(self._global_defaults)

    def result_value(self) -> tuple[str, SubtitleLoadingSettings]:
        return (
            str(self._mode_combo.currentData() or "global"),
            self._current_values(),
        )

__all__ = [
    "GuideSymbolSettingsDialog",
    "LayoutIssue",
    "LayoutIssuesDialog",
    "SubtitleLoadingSettingsDialog",
    "TimingIssue",
    "layout_issue_icon",
]
