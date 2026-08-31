"""Guide-symbol batch detection UI and bitmap guide (before/after image) picking."""

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

from krok_helper.subtitle_render.frontend.dialogs.fluent_dialogs import (
    fluent_button_row,
    fluent_error,
)
from krok_helper.subtitle_render.domain.timing import (
    GuideSymbol,
    TimingLine,
    TimingTrack,
    guide_symbol_replaces_prefix,
    guide_symbol_with_role_labels,
)
from krok_helper.subtitle_render.sources.guide_symbols import (
    GUIDE_SYMBOL_FILE_FILTER,
    GuideSymbolImportError,
    import_bitmap_guide_symbol,
    import_svg_guide_symbol,
    is_vector_guide_symbol_file,
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
            # 只占正文前空位、不替代真实字符的行前导唱符（@Emoji 小头像即是）不算
            # 「已有替换」：它与行首标记替换互不冲突，勾选后也不会被顶掉。
            has_replacement = (
                (is_prefix and guide_symbol_replaces_prefix(line.guide_symbol))
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


_BITMAP_SETTINGS_MEMORY: dict[str, object] = {}
"""会话内记忆的上次图片导唱符设置（走字前/后图片 + @Emoji 式选项）。

与 SUG「分色标签设置助手」的记忆首行参数等价，但只在当前进程内生效、
不落盘。键与 :meth:`GuideBitmapOptionsRow.options` 一致，另含
``before_path`` / ``after_path`` 两个字符串。
"""


def remember_bitmap_settings(settings: dict) -> None:
    _BITMAP_SETTINGS_MEMORY.clear()
    _BITMAP_SETTINGS_MEMORY.update(dict(settings or {}))


def last_bitmap_settings() -> dict:
    return dict(_BITMAP_SETTINGS_MEMORY)


def bitmap_options_kwargs(options: dict) -> dict:
    """把 :meth:`GuideBitmapOptionsRow.options` 字典转成导入函数的关键字参数。"""
    options = options or {}
    try:
        zoom_value = max(int(options.get("zoom_value", 100)), 1)
        margin_left = int(options.get("margin_left_px", 0))
        margin_right = int(options.get("margin_right_px", 0))
        margin_bottom = int(options.get("margin_bottom_px", 0))
    except (TypeError, ValueError):
        zoom_value, margin_left, margin_right, margin_bottom = 100, 0, 0, 0
    return {
        "zoom_percent": zoom_value,
        "fix_size": str(options.get("zoom_mode") or "") == GuideBitmapOptionsRow.FIX_MODE,
        "no_decor": bool(options.get("no_decor")),
        "margin_left_px": margin_left,
        "margin_right_px": margin_right,
        "margin_bottom_px": margin_bottom,
    }


class GuideBitmapOptionsRow(QWidget):
    """@Emoji 式选项行：缩放（Zoom% / Fix）· NoDecor · 左右下余白。

    与 SUG「分色标签设置助手」的选项行同构；``NoDecor`` 控制是否给图片套用
    样式方案中的文字装饰（shadow / glow，飾り色随走字前后切换），
    ``ForceWipeDecor`` 渲染端未实现、不暴露。供「图片导唱符设置」与
    「批量识别导唱标记」共用，避免两处控件各自漂移。
    """

    ZOOM_MODE = "Zoom"
    FIX_MODE = "Fix"

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        *,
        defaults: Optional[dict] = None,
    ) -> None:
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)

        self.zoom_mode_combo = ComboBox(self)
        self.zoom_mode_combo.addItems([self.ZOOM_MODE, self.FIX_MODE])
        self.zoom_mode_combo.setMinimumWidth(80)
        self.zoom_mode_combo.setToolTip(
            "Zoom：按字幕行高缩放图片（默认 100%）；Fix：保持原图像素尺寸。"
        )
        row.addWidget(self.zoom_mode_combo)

        self.zoom_value_edit = LineEdit(self)
        self.zoom_value_edit.setMinimumWidth(52)
        self.zoom_value_edit.setPlaceholderText("100")
        self.zoom_value_edit.setToolTip("Zoom 百分比。")
        row.addWidget(self.zoom_value_edit)
        row.addWidget(BodyLabel("%", self))
        self.zoom_mode_combo.currentTextChanged.connect(
            lambda text: self.zoom_value_edit.setEnabled(text == self.ZOOM_MODE)
        )

        self.no_decor_check = CheckBox("NoDecor", self)
        self.no_decor_check.setToolTip(
            "不套用样式方案中的文字装饰（shadow / glow）；默认给图片加装饰。"
        )
        row.addWidget(self.no_decor_check)

        row.addSpacing(8)
        row.addWidget(BodyLabel("余白", self))
        offset_warning = "偏移量过度超出第一个字符可能导致显示异常。"
        for key, label, tooltip in (
            (
                "margin_left_px",
                "L",
                "MarginLeft：图片左侧留白（像素，允许负值）。",
            ),
            (
                "margin_right_px",
                "R",
                "MarginRight：图片右侧留白（像素，允许负值）。",
            ),
            (
                "margin_bottom_px",
                "B",
                "MarginBottom：图片下方留白（像素，允许负值）。",
            ),
        ):
            row.addWidget(BodyLabel(label, self))
            edit = LineEdit(self)
            edit.setMinimumWidth(60)
            edit.setPlaceholderText("0")
            edit.setToolTip(f"{tooltip}\n{offset_warning}")
            setattr(self, f"{key}_edit", edit)
            row.addWidget(edit)
        row.addStretch(1)

        self.set_options(defaults or {})

    def options(self) -> dict:
        return {
            "zoom_mode": self.zoom_mode_combo.currentText(),
            "zoom_value": self._edit_int(self.zoom_value_edit, 100),
            "no_decor": self.no_decor_check.isChecked(),
            "margin_left_px": self._edit_int(self.margin_left_px_edit, 0),
            "margin_right_px": self._edit_int(self.margin_right_px_edit, 0),
            "margin_bottom_px": self._edit_int(self.margin_bottom_px_edit, 0),
        }

    def set_options(self, options: dict) -> None:
        options = options or {}
        self.zoom_mode_combo.setCurrentText(
            self.FIX_MODE
            if str(options.get("zoom_mode") or "") == self.FIX_MODE
            else self.ZOOM_MODE
        )
        self.zoom_value_edit.setText(str(options.get("zoom_value", 100)))
        self.no_decor_check.setChecked(bool(options.get("no_decor")))
        for key in ("margin_left_px", "margin_right_px", "margin_bottom_px"):
            getattr(self, f"{key}_edit").setText(str(options.get(key, 0)))

    @staticmethod
    def _edit_int(edit: LineEdit, default: int) -> int:
        try:
            return int(edit.text().strip())
        except (TypeError, ValueError):
            return default


class GuideBitmapSettingsDialog(ModelessDialog):
    """图片导唱符设置：走字前/后图片自选槽位 + @Emoji 式选项行。

    与 SUG「分色标签设置助手」同构：两张图片各自带浏览按钮，选哪张放进
    哪个槽位由用户决定（不存在「选图固定进走字前」）；留空的一侧渲染为
    透明，两侧都留空无法应用。走字前选择 SVG 时保持原有矢量替换，此时
    走字后图片与选项无效。未显式传值时按上次记忆预填。
    """

    def __init__(
        self,
        *,
        before_path: str = "",
        after_path: str = "",
        start_dir: str = "",
        parent: Optional[QWidget] = None,
        defaults: Optional[dict] = None,
    ) -> None:
        super().__init__(parent.window() if parent is not None else None)
        self.setWindowTitle("图片导唱符设置")
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.setMinimumWidth(560)
        self._start_dir = start_dir
        remembered = last_bitmap_settings()
        self._before_default = str(before_path or remembered.get("before_path") or "")
        self._after_default = str(after_path or remembered.get("after_path") or "")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(10)
        layout.addWidget(StrongBodyLabel("图片导唱符设置", self))
        hint = CaptionLabel(
            "走字前图片在走字到达前显示，走字后图片在走字经过后显示；"
            "留空的一侧保持透明，两侧都留空则无法应用。"
            "走字前选择 SVG 时按矢量导唱符替换，走字后图片与选项无效。",
            self,
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.before_edit = LineEdit(self)
        self.before_edit.setPlaceholderText("留空 = 走字前透明；可选 SVG（保持矢量替换）")
        before_row = self._path_row(
            "走字前图片（可选）：", self.before_edit, "before_browse_button",
            "before_clear_button",
        )
        layout.addLayout(before_row)

        self.after_edit = LineEdit(self)
        self.after_edit.setPlaceholderText("留空 = 走字后透明")
        after_row = self._path_row(
            "走字后图片（可选）：", self.after_edit, "after_browse_button",
            "after_clear_button",
        )
        layout.addLayout(after_row)

        self.options_row = GuideBitmapOptionsRow(self, defaults=remembered or defaults)
        layout.addWidget(self.options_row)

        button_row, self.ok_button, _cancel_button = fluent_button_row(self)
        layout.addLayout(button_row)

        for button in (
            self.before_browse_button,
            self.before_clear_button,
            self.after_browse_button,
            self.after_clear_button,
            self.ok_button,
            _cancel_button,
        ):
            button.setAutoDefault(False)
        self.before_browse_button.clicked.connect(
            lambda: self._browse(self.before_edit)
        )
        self.after_browse_button.clicked.connect(lambda: self._browse(self.after_edit))
        self.before_clear_button.clicked.connect(lambda: self.before_edit.clear())
        self.after_clear_button.clicked.connect(lambda: self.after_edit.clear())
        self.before_edit.textChanged.connect(self._sync_ok_button)
        self.after_edit.textChanged.connect(self._sync_ok_button)
        self.before_edit.setText(self._before_default)
        self.after_edit.setText(self._after_default)
        self._sync_ok_button()

    def _path_row(
        self, label: str, edit: LineEdit, browse_attr: str, clear_attr: str
    ) -> QHBoxLayout:
        row = QHBoxLayout()
        row.addWidget(BodyLabel(label, self))
        row.addWidget(edit, 1)
        browse = PushButton("浏览…", self)
        clear = PushButton("清空", self)
        setattr(self, browse_attr, browse)
        setattr(self, clear_attr, clear)
        row.addWidget(browse)
        row.addWidget(clear)
        return row

    def _browse(self, edit: LineEdit) -> None:
        path, _selected_filter = QFileDialog.getOpenFileName(
            self, "选择图片", self._start_dir, GUIDE_SYMBOL_FILE_FILTER
        )
        if path:
            edit.setText(path)

    def _sync_ok_button(self, *_args) -> None:
        self.ok_button.setEnabled(bool(self.before_path() or self.after_path()))

    def before_path(self) -> str:
        return self.before_edit.text().strip()

    def after_path(self) -> str:
        return self.after_edit.text().strip()

    def options(self) -> dict:
        return self.options_row.options()

    def settings(self) -> dict:
        return {
            "before_path": self.before_path(),
            "after_path": self.after_path(),
            **self.options(),
        }


def guide_symbol_from_bitmap_dialog(
    dialog: GuideBitmapSettingsDialog,
    *,
    duration_ms: int = 1000,
    count: int = 1,
) -> Optional[GuideSymbol]:
    """把已确认的图片导唱符设置变成 :class:`GuideSymbol`，失败弹错并返回 None。

    走字前是 SVG 时保持原有矢量替换；否则按位图导入并带上缩放 / 文字装饰 /
    余白选项。成功路径上先把这次设置记入会话记忆，供下次打开预填。
    """
    remember_bitmap_settings(dialog.settings())
    before = dialog.before_path()
    after = dialog.after_path()
    try:
        if before and is_vector_guide_symbol_file(before):
            return import_svg_guide_symbol(
                Path(before), duration_ms=duration_ms, count=count
            )
        return import_bitmap_guide_symbol(
            before or None,
            after or None,
            duration_ms=duration_ms,
            count=count,
            **bitmap_options_kwargs(dialog.options()),
        )
    except GuideSymbolImportError as exc:
        fluent_error(dialog, "无法导入导唱符", str(exc))
        return None


class GuidePrefixReplaceDialog(ModelessDialog):
    """Fluent batch-review dialog for guide replacement and marker role batching."""

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
            "默认识别歌词行开头的连续同名打轴单元；可选择搜索句中标记。原始字幕文件不会被修改，导唱符会保留每个标记的原始时间。\n"
            "走字前 / 走字后图片均可留空（留空一侧透明），至少选择一张；"
            "走字前选择 SVG 时按矢量导唱符替换，走字后图片与选项对其无效。",
            self,
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        before_row = QHBoxLayout()
        before_row.addWidget(BodyLabel("走字前图片（可选）：", self))
        self.before_edit = LineEdit(self)
        self.before_edit.setPlaceholderText("留空 = 走字前透明；可选 SVG（保持矢量替换）")
        self.before_browse_button = PushButton("浏览…", self)
        self.before_clear_button = PushButton("清空", self)
        before_row.addWidget(self.before_edit, 1)
        before_row.addWidget(self.before_browse_button)
        before_row.addWidget(self.before_clear_button)
        layout.addLayout(before_row)

        after_row = QHBoxLayout()
        after_row.addWidget(BodyLabel("走字后图片（可选）：", self))
        self.after_edit = LineEdit(self)
        self.after_edit.setPlaceholderText("留空 = 走字后透明")
        self.after_browse_button = PushButton("浏览…", self)
        self.after_clear_button = PushButton("清空", self)
        after_row.addWidget(self.after_edit, 1)
        after_row.addWidget(self.after_browse_button)
        after_row.addWidget(self.after_clear_button)
        layout.addLayout(after_row)

        self.options_row = GuideBitmapOptionsRow(
            self, defaults=last_bitmap_settings()
        )
        layout.addWidget(self.options_row)

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
        # image picker or accept the dialog as a side effect.
        for button in (
            self.before_browse_button,
            self.before_clear_button,
            self.after_browse_button,
            self.after_clear_button,
            self.detect_button,
            self.select_all_button,
            self.select_none_button,
            self.batch_role_button,
            self.ok_button,
            _cancel_button,
        ):
            button.setAutoDefault(False)

        self.before_browse_button.clicked.connect(
            lambda: self._browse_edit(self.before_edit)
        )
        self.after_browse_button.clicked.connect(
            lambda: self._browse_edit(self.after_edit)
        )
        self.before_clear_button.clicked.connect(lambda: self.before_edit.clear())
        self.after_clear_button.clicked.connect(lambda: self.after_edit.clear())
        self.candidate_combo.currentIndexChanged.connect(self._candidate_changed)
        self.detect_button.clicked.connect(self.refresh_matches)
        self.non_prefix_check.toggled.connect(self.refresh_matches)
        self.marker_edit.returnPressed.connect(self._detect_from_marker_input)
        self.marker_edit.textChanged.connect(self._sync_ok_button)
        self.before_edit.textChanged.connect(self._sync_ok_button)
        self.after_edit.textChanged.connect(self._sync_ok_button)
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

    def _browse_edit(self, edit: LineEdit) -> None:
        path, _selected_filter = QFileDialog.getOpenFileName(
            self, "选择图片导唱符", self._start_dir, GUIDE_SYMBOL_FILE_FILTER
        )
        if path:
            edit.setText(path)

    def _detect_from_marker_input(self) -> None:
        self.refresh_matches()
        self.marker_edit.setFocus()

    def set_before_path(self, path: Path | str) -> None:
        self.before_edit.setText(str(path) if path else "")

    def before_path(self) -> Optional[Path]:
        text = self.before_edit.text().strip()
        return Path(text) if text else None

    def set_after_path(self, path: Path | str) -> None:
        self.after_edit.setText(str(path) if path else "")

    def after_path(self) -> Optional[Path]:
        text = self.after_edit.text().strip()
        return Path(text) if text else None

    def bitmap_options(self) -> dict:
        return self.options_row.options()

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
        self.ok_button.setEnabled(
            (self.before_path() is not None or self.after_path() is not None)
            and has_selection
        )
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
