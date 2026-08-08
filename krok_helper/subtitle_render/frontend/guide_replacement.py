"""Batch detection UI for replacing timed marker runs with SVG guides."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path
import unicodedata
from typing import Callable, Optional

from PyQt6.QtCore import Qt, pyqtSignal as Signal
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CheckBox,
    ComboBox,
    LineEdit,
    PushButton,
    StrongBodyLabel,
    TableWidget,
)
from krok_helper.qfluent_compat import ModelessDialog

from krok_helper.subtitle_render.frontend.fluent_dialogs import fluent_button_row
from krok_helper.subtitle_render.models import (
    GuideSymbol,
    TimingLine,
    TimingTrack,
    guide_symbol_with_role_labels,
)


@dataclass(frozen=True)
class GuidePrefixMatch:
    row: int
    marker: str
    prefix: tuple[str, ...]
    source_text: str
    lyric_text: str
    start_ms: int
    intervals_ms: tuple[int, ...]
    start_index: int = 0
    replacement_text: str = ""
    has_guide_symbol: bool = False

    @property
    def count(self) -> int:
        return len(self.prefix)

    @property
    def is_prefix(self) -> bool:
        return self.start_index == 0 and bool(self.lyric_text)


def _prefix_run_count(line: TimingLine, marker: str) -> int:
    count = 0
    for char in line.chars:
        if char.text != marker:
            break
        count += 1
    return count if count < len(line.chars) else 0


def _marker_runs(
    line: TimingLine, marker: str, *, include_non_prefix: bool
) -> list[tuple[int, int]]:
    if not include_non_prefix:
        count = _prefix_run_count(line, marker)
        return [(0, count)] if count > 0 else []
    runs: list[tuple[int, int]] = []
    index = 0
    while index < len(line.chars):
        if line.chars[index].text != marker:
            index += 1
            continue
        end = index + 1
        while end < len(line.chars) and line.chars[end].text == marker:
            end += 1
        runs.append((index, end))
        index = end
    return runs


def detect_guide_prefix_matches(
    track: TimingTrack, marker: str, *, include_non_prefix: bool = False
) -> list[GuidePrefixMatch]:
    """Find consecutive marker runs at line start, or anywhere when requested."""
    marker = str(marker).strip()
    if not marker:
        return []
    matches: list[GuidePrefixMatch] = []
    for row, line in enumerate(track.lines):
        if line.is_blank or not line.chars:
            continue
        for start, end in _marker_runs(
            line, marker, include_non_prefix=include_non_prefix
        ):
            marker_chars = line.chars[start:end]
            remaining = [*line.chars[:start], *line.chars[end:]]
            intervals = tuple(
                max(
                    int(line.chars[index + 1].start_ms) - int(char.start_ms),
                    0,
                )
                if index + 1 < len(line.chars)
                else max(int(line.end_ms or char.start_ms) - int(char.start_ms), 0)
                for index, char in enumerate(marker_chars, start=start)
            )
            target_indices = range(start, end)
            is_prefix = start == 0 and end < len(line.chars)
            has_replacement = (
                (is_prefix and line.guide_symbol is not None)
                or any(index in line.inline_guide_symbols for index in target_indices)
            )
            source_text = "".join(char.text for char in line.chars)
            matches.append(
                GuidePrefixMatch(
                    row=row,
                    marker=marker,
                    prefix=tuple(char.text for char in marker_chars),
                    source_text=source_text,
                    lyric_text="".join(char.text for char in remaining),
                    start_ms=int(marker_chars[0].start_ms),
                    intervals_ms=intervals,
                    start_index=start,
                    replacement_text=(
                        "".join(char.text for char in line.chars[:start])
                        + "◆" * len(marker_chars)
                        + "".join(char.text for char in line.chars[end:])
                    ),
                    has_guide_symbol=has_replacement,
                )
            )
    return matches


def replacement_symbol_for_match(
    base_symbol: GuideSymbol,
    line: TimingLine,
    match: GuidePrefixMatch,
) -> Optional[GuideSymbol]:
    """Build a replacement placement after revalidating the live source line."""
    prefix_count = len(match.prefix)
    if (
        match.start_index != 0
        or prefix_count <= 0
        or len(line.chars) <= prefix_count
        or tuple(char.text for char in line.chars[:prefix_count]) != match.prefix
    ):
        return None
    prefix_chars = line.chars[:prefix_count]
    intervals = [
        max(int(line.chars[index + 1].start_ms) - int(char.start_ms), 0)
        for index, char in enumerate(prefix_chars)
    ]
    symbol = replace(
        base_symbol,
        count=prefix_count,
        duration_ms=intervals[-1] if intervals else 0,
        replacement_prefix=match.prefix,
    )
    return guide_symbol_with_role_labels(
        symbol, [char.role_label for char in prefix_chars]
    )


def _looks_like_marker(text: str) -> bool:
    if not text or len(text) > 4 or any(char.isspace() for char in text):
        return False
    if text.isascii():
        return True
    return all(
        unicodedata.category(char).startswith(("P", "S"))
        or 0xE000 <= ord(char) <= 0xF8FF
        for char in text
    )


def guide_marker_options(track: TimingTrack, *, limit: int = 30) -> list[tuple[str, int]]:
    """Return likely prefix markers, ranking ASCII/symbol candidates first."""
    first_counts: Counter[str] = Counter(
        line.chars[0].text
        for line in track.lines
        if not line.is_blank
        and len(line.chars) >= 2
        and line.chars[0].text
        and line.chars[0].source_span_count == 1
    )
    counts: Counter[str] = Counter()
    gap_totals: Counter[str] = Counter()
    for line in track.lines:
        if line.is_blank or len(line.chars) < 2:
            continue
        if line.chars[0].source_span_count != 1:
            continue
        marker = line.chars[0].text
        if not marker or marker.isspace():
            continue
        if _looks_like_marker(marker) or first_counts[marker] >= 2:
            counts[marker] += 1
            gap_totals[marker] += max(
                int(line.chars[1].start_ms) - int(line.chars[0].start_ms), 0
            )
    ordered = sorted(
        counts.items(),
        key=lambda item: (
            0 if _looks_like_marker(item[0]) else 1,
            -(gap_totals[item[0]] / max(item[1], 1)),
            -item[1],
            item[0],
        ),
    )
    return ordered[: max(int(limit), 1)]


def _format_time(ms: int) -> str:
    centiseconds = max(int(ms), 0) // 10
    minutes, remainder = divmod(centiseconds, 6000)
    seconds, cs = divmod(remainder, 100)
    return f"{minutes:02d}:{seconds:02d}.{cs:02d}"


class GuideRoleSchemeDialog(ModelessDialog):
    """Choose an existing project role scheme for selected guide candidates."""

    def __init__(
        self,
        role_names: list[str],
        *,
        prompt: str,
        cancel_text: str = "取消",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent.window() if parent is not None else None)
        self.setWindowTitle("批量应用角色方案")
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.setMinimumWidth(460)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(10)
        layout.addWidget(StrongBodyLabel("批量应用角色方案", self))
        message = BodyLabel(prompt, self)
        message.setWordWrap(True)
        layout.addWidget(message)

        role_row = QHBoxLayout()
        role_row.addWidget(BodyLabel("角色方案：", self))
        self.role_combo = ComboBox(self)
        self.role_combo.setMinimumWidth(260)
        for name in role_names:
            self.role_combo.addItem(name, userData=name)
        role_row.addWidget(self.role_combo, 1)
        layout.addLayout(role_row)
        if not role_names:
            empty_hint = CaptionLabel(
                "当前项目没有可用的角色方案，请先在字体页新建角色方案。",
                self,
            )
            empty_hint.setWordWrap(True)
            layout.addWidget(empty_hint)

        button_row, self.ok_button, self.cancel_button = fluent_button_row(
            self,
            ok_text="应用角色方案",
            cancel_text=cancel_text,
        )
        layout.addLayout(button_row)
        self.ok_button.setEnabled(self.role_combo.count() > 0)
        self.ok_button.setAutoDefault(False)
        self.cancel_button.setAutoDefault(False)

    def role_name(self) -> Optional[str]:
        value = self.role_combo.currentData()
        return str(value).strip() if value else None


def choose_guide_role_scheme(
    role_names: list[str],
    *,
    prompt: str,
    cancel_text: str = "取消",
    parent: Optional[QWidget] = None,
) -> Optional[str]:
    """Return a selected existing role scheme, or ``None`` when skipped."""
    unique_names: list[str] = []
    seen: set[str] = set()
    for value in role_names:
        name = str(value or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        unique_names.append(name)
    dialog = GuideRoleSchemeDialog(
        unique_names,
        prompt=prompt,
        cancel_text=cancel_text,
        parent=parent,
    )
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return None
    return dialog.role_name()


class GuidePrefixReplaceDialog(ModelessDialog):
    """Fluent batch-review dialog for SVG replacement and marker role batching."""

    roleSchemeApplyRequested = Signal(object, str)

    def __init__(
        self,
        track: TimingTrack,
        *,
        start_dir: str = "",
        role_options: Optional[list[str]] = None,
        role_options_provider: Optional[Callable[[], list[str]]] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent.window() if parent is not None else None)
        self._track = track
        self._start_dir = start_dir
        self._role_options = list(role_options or ())
        self._role_options_provider = role_options_provider
        self._matches: list[GuidePrefixMatch] = []
        self._row_checks: list[QTableWidgetItem] = []
        self.setWindowTitle("批量识别导唱标记")
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.setMinimumSize(820, 560)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(10)
        layout.addWidget(StrongBodyLabel("批量识别导唱标记", self))
        hint = CaptionLabel(
            "默认识别歌词行开头的连续同名打轴单元；可选择搜索句中标记。原始字幕文件不会被修改，导唱符会保留每个标记的原始时间。",
            self,
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        svg_row = QHBoxLayout()
        svg_row.addWidget(BodyLabel("SVG 导唱符：", self))
        self.svg_path_edit = LineEdit(self)
        self.svg_path_edit.setReadOnly(True)
        self.svg_path_edit.setPlaceholderText("请选择一个 SVG 文件")
        self.svg_browse_button = PushButton("浏览…", self)
        svg_row.addWidget(self.svg_path_edit, 1)
        svg_row.addWidget(self.svg_browse_button)
        layout.addLayout(svg_row)

        detect_row = QHBoxLayout()
        detect_row.addWidget(BodyLabel("自动检测：", self))
        self.candidate_combo = ComboBox(self)
        self.candidate_combo.setMinimumWidth(190)
        detect_row.addWidget(self.candidate_combo)
        detect_row.addWidget(BodyLabel("标记字符：", self))
        self.marker_edit = LineEdit(self)
        self.marker_edit.setPlaceholderText("例如 h")
        self.marker_edit.setFixedWidth(105)
        self.non_prefix_check = CheckBox("允许搜索非行首字符", self)
        self.detect_button = PushButton("检测", self)
        detect_row.addWidget(self.marker_edit)
        detect_row.addWidget(self.non_prefix_check)
        detect_row.addWidget(self.detect_button)
        detect_row.addStretch(1)
        layout.addLayout(detect_row)

        self.summary_label = CaptionLabel("", self)
        layout.addWidget(self.summary_label)
        self.table = TableWidget(self)
        self.table.setColumnCount(7)
        self.table.setHorizontalHeaderLabels(
            ["替换", "时间", "检测标记", "原歌词", "替换后", "原间隔", "状态"]
        )
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        for column in (0, 1, 2, 5, 6):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.table, 1)

        selection_row = QHBoxLayout()
        self.select_all_button = PushButton("全选可替换项", self)
        self.select_none_button = PushButton("取消全选", self)
        selection_row.addWidget(self.select_all_button)
        selection_row.addWidget(self.select_none_button)
        selection_row.addStretch(1)
        layout.addLayout(selection_row)
        button_row, self.ok_button, _cancel_button = fluent_button_row(
            self, ok_text="应用替换"
        )
        self.batch_role_button = PushButton("批量应用角色方案", self)
        button_row.insertWidget(1, self.batch_role_button)
        layout.addLayout(button_row)

        # QDialog treats an auto-default push button as the target of Enter.
        # The marker input owns Enter exclusively, so it must never open the
        # SVG picker or accept the dialog as a side effect.
        for button in (
            self.svg_browse_button,
            self.detect_button,
            self.select_all_button,
            self.select_none_button,
            self.batch_role_button,
            self.ok_button,
            _cancel_button,
        ):
            button.setAutoDefault(False)

        self.svg_browse_button.clicked.connect(self._browse_svg)
        self.candidate_combo.currentIndexChanged.connect(self._candidate_changed)
        self.detect_button.clicked.connect(self.refresh_matches)
        self.non_prefix_check.toggled.connect(self.refresh_matches)
        self.marker_edit.returnPressed.connect(self._detect_from_marker_input)
        self.marker_edit.textChanged.connect(self._sync_ok_button)
        self.svg_path_edit.textChanged.connect(self._sync_ok_button)
        self.table.itemChanged.connect(self._sync_ok_button)
        self.select_all_button.clicked.connect(lambda: self._set_all_checked(True))
        self.select_none_button.clicked.connect(lambda: self._set_all_checked(False))
        self.batch_role_button.clicked.connect(self._request_role_scheme)
        self._load_candidate_options()
        self._sync_ok_button()

    def _load_candidate_options(self) -> None:
        options = guide_marker_options(self._track)
        self.candidate_combo.clear()
        for marker, count in options:
            self.candidate_combo.addItem(f"{marker}（{count} 行）", userData=marker)
        if options:
            self.marker_edit.setText(options[0][0])
            self.refresh_matches()
        else:
            self.summary_label.setText("没有自动发现明显标记；可以手动输入标记字符检测。")

    def _candidate_changed(self, index: int) -> None:
        marker = self.candidate_combo.itemData(index)
        if marker:
            self.marker_edit.setText(str(marker))
            self.refresh_matches()

    def _browse_svg(self) -> None:
        path, _selected_filter = QFileDialog.getOpenFileName(
            self, "选择 SVG 导唱符", self._start_dir, "SVG 文件 (*.svg)"
        )
        if path:
            self.svg_path_edit.setText(path)

    def _detect_from_marker_input(self) -> None:
        self.refresh_matches()
        self.marker_edit.setFocus()

    def set_svg_path(self, path: Path | str) -> None:
        self.svg_path_edit.setText(str(path))

    def svg_path(self) -> Optional[Path]:
        text = self.svg_path_edit.text().strip()
        return Path(text) if text else None

    def refresh_matches(self) -> None:
        self._matches = detect_guide_prefix_matches(
            self._track,
            self.marker_edit.text(),
            include_non_prefix=self.non_prefix_check.isChecked(),
        )
        self._row_checks = []
        self.table.setRowCount(len(self._matches))
        for table_row, match in enumerate(self._matches):
            check_item = QTableWidgetItem()
            check_item.setFlags(
                Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable
            )
            check_item.setCheckState(
                Qt.CheckState.Unchecked
                if match.has_guide_symbol
                else Qt.CheckState.Checked
            )
            check_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            check_item.setData(Qt.ItemDataRole.UserRole, True)
            self.table.setItem(table_row, 0, check_item)
            self._row_checks.append(check_item)
            interval_text = " / ".join(str(value) for value in match.intervals_ms)
            values = (
                _format_time(match.start_ms),
                f"{match.marker} × {match.count}",
                match.source_text,
                match.replacement_text,
                f"{interval_text} ms",
                "已有导唱符，可重新替换" if match.has_guide_symbol else "可替换",
            )
            for column, value in enumerate(values, start=1):
                self.table.setItem(table_row, column, QTableWidgetItem(value))
        marker = self.marker_edit.text().strip()
        matched_rows = len({match.row for match in self._matches})
        self.summary_label.setText(
            f"检测到导唱候选：{len(self._matches)} 处（{matched_rows} 行）；可替换 {len(self._matches)} 处"
            if marker
            else "请输入要检测的标记字符。"
        )
        self._sync_ok_button()

    def _set_all_checked(self, checked: bool) -> None:
        for item in self._row_checks:
            if bool(item.data(Qt.ItemDataRole.UserRole)):
                item.setCheckState(
                    Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
                )
        self._sync_ok_button()

    def selected_matches(self) -> list[GuidePrefixMatch]:
        return [
            match
            for match, item in zip(self._matches, self._row_checks)
            if bool(item.data(Qt.ItemDataRole.UserRole))
            and item.checkState() == Qt.CheckState.Checked
        ]

    def _sync_ok_button(self, *_args) -> None:
        if not hasattr(self, "ok_button"):
            return
        has_selection = bool(self.selected_matches())
        self.ok_button.setEnabled(self.svg_path() is not None and has_selection)
        self.batch_role_button.setEnabled(has_selection)

    def _available_role_options(self) -> list[str]:
        if self._role_options_provider is not None:
            return list(self._role_options_provider())
        return list(self._role_options)

    def _request_role_scheme(self) -> None:
        selected = self.selected_matches()
        if not selected:
            return
        role_name = choose_guide_role_scheme(
            self._available_role_options(),
            prompt=(
                "将当前勾选候选位置批量应用为以下角色方案。"
                "无论该位置是否已经替换为导唱符，都只修改对应标记字符。"
            ),
            parent=self,
        )
        if role_name:
            self.roleSchemeApplyRequested.emit(selected, role_name)
