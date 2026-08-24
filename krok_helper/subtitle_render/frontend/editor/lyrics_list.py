"""左侧歌词面板（拖拽 + qfluentwidgets 表格列表）。

UI 设计：

- **空态**：居中显示"拖入字幕文件 / 点击此处选择"，受 :class:`DropPanel` 接管
- **载入后**：``TableWidget``（qfluentwidgets），五列——轨 / 角色 / 特效 / 内容 / 布局。

  - **轨**：多行布局下按实际渲染 lane（非空行序号 % 行数）标 T1 / T2 / …，
    同一组行共享一个浅色底，直观呈现"按页贴在一起"的显示分组
  - **角色**：可编辑（下拉选择配色方案），名字前带该方案的颜色色点
  - **特效**：双击编辑逐行入场/退场动画；右键可批量设置选中行
  - **内容**：只读；水平对齐跟随布局设置（asymmetric 按每行对齐列表 / center
    居中 / per_row 按行独立对齐），布局改动即时反映到列表
  - 有页计划时只显示段落/分页提示行；无页计划的兼容输入才把空行显示为间奏 ♪
"""

from __future__ import annotations

from collections import Counter, OrderedDict
from pathlib import Path
from typing import Optional

from dataclasses import dataclass, replace as _dataclass_replace

from PyQt6.QtCore import (
    QEvent,
    QItemSelectionModel,
    QPoint,
    QPointF,
    QRectF,
    Qt,
    QSize,
    QTimer,
    pyqtSignal as Signal,
)
from PyQt6.QtGui import QBrush, QColor, QFont, QIcon, QKeySequence, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDialog,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QScrollArea,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    Action,
    CheckBox,
    ComboBox as FluentComboBox,
    FlowLayout,
    FluentIcon as FIF,
    TableWidget as FluentTableWidget,
    PrimaryPushButton as FluentPrimaryPushButton,
    PushButton as FluentPushButton,
    RoundMenu,
    SpinBox as FluentSpinBox,
    ToggleButton as FluentToggleButton,
    TransparentToolButton,
)
from qfluentwidgets.components.widgets.combo_box import ComboBoxMenu
from qfluentwidgets.components.widgets.menu import MenuAnimationType

from krok_helper.qfluent_compat import ModelessDialog, hide_fluent_tooltip, show_fluent_tooltip
from krok_helper.subtitle_render.engine.timing.timeline import (
    assign_lanes,
)
from krok_helper.subtitle_render.engine.layout.page_plan import (
    ResolvedPagePlan,
    resolve_page_plan,
)
from krok_helper.subtitle_render.guide_symbols import (
    GuideSymbolImportError,
    import_svg_guide_symbol,
    scaled_guide_symbol_path,
)
from krok_helper.subtitle_render.frontend.drop_panel import DropPanel
from krok_helper.subtitle_render.frontend.dialogs.fluent_dialogs import (
    fluent_error,
    fluent_get_text,
    fluent_question,
)
from krok_helper.subtitle_render.timing import (
    GuideSymbol,
    LineAnimationOverride,
    TimingChar,
    TimingLine,
    TimingTrack,
    guide_symbol_replacement_count,
    guide_symbol_role_labels,
    line_visible_chars,
)
from krok_helper.subtitle_render.models import (
    LYRICS_LAYOUT_FIELDS,
    Style,
    TITLE_SCHEME_NAME,
    TitleOverlay,
    normalize_title_char_role_labels,
    layout_capacity,
    layout_display_name,
)
from krok_helper.subtitle_render.frontend.theme import palette, themed

COL_LANE = 0
COL_ROLE = 1
COL_EFFECT = 2
COL_CONTENT = 3
COL_LAYOUT = 4

_COLUMN_HEADERS = ["轨", "角色", "特效", "内容", "布局"]

_ROW_HEIGHT = 34
_BLANK_ROW_HEIGHT = 18
_DEFAULT_ROLE_TEXT = "（默认）"


@dataclass(frozen=True)
class LyricsPresentationRow:
    kind: str
    track_line_index: Optional[int] = None
    render_line_index: Optional[int] = None
    section_index: Optional[int] = None
    page_index: Optional[int] = None
    global_page_index: Optional[int] = None
    lane: Optional[int] = None
    layout_id: Optional[str] = None

_ENTRY_EFFECTS = (
    ("none", "无"),
    ("fade", "淡入"),
    ("slide_in", "滑入"),
    ("rise", "上升"),
    ("char_fade", "逐字淡入"),
    ("char_drip", "文字垂下"),
    ("spin_flip", "翻转"),
    ("utopia", "Utopia"),
)
_EXIT_EFFECTS = (
    ("none", "无"),
    ("fade", "淡出"),
    ("slide_out", "滑出"),
    ("rise", "上升"),
    ("char_fade", "逐字淡出"),
    ("char_drip", "文字垂出"),
    ("spin_flip", "翻转"),
    ("utopia", "Utopia"),
)
#: 唱字特效比入退场多一档「跟随全局」——覆盖这一行的入退场时，唱字往往仍想跟着
#: 主字幕走，不该被迫二选一。
_KARAOKE_EFFECTS = (
    ("inherit", "跟随全局"),
    ("none", "无"),
    ("utopia", "Utopia"),
)
_ENTRY_LABELS = dict(_ENTRY_EFFECTS)
_EXIT_LABELS = dict(_EXIT_EFFECTS)
_KARAOKE_LABELS = dict(_KARAOKE_EFFECTS)


def _animation_summary(style: Style, override: Optional[LineAnimationOverride]) -> str:
    prefix = "全局：" if override is None else ""
    entry = style.entry_anim if override is None else override.entry_anim
    exit_ = style.exit_anim if override is None else override.exit_anim
    summary = f"{prefix}{_ENTRY_LABELS.get(entry, entry)} / {_EXIT_LABELS.get(exit_, exit_)}"
    # 唱字只在这一行真的改了它时才标出来，免得每行都拖一截重复文字。
    if override is not None and override.karaoke_anim != "inherit":
        summary += f" · 唱字{_KARAOKE_LABELS.get(override.karaoke_anim, override.karaoke_anim)}"
    return summary


class _LineAnimationDialog(ModelessDialog):
    """歌词列表逐行动画的紧凑编辑弹窗。"""

    def __init__(
        self,
        style: Style,
        override: Optional[LineAnimationOverride],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("逐行特效")
        self.setMinimumWidth(360)
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(14)

        self._inherit_check = CheckBox("继承全局设置", self)
        self._inherit_check.setChecked(override is None)
        root.addWidget(self._inherit_check)

        form = QFormLayout()
        form.setSpacing(10)
        self._preset_combo = _StableFluentComboBox(self)
        for value, label in (
            ("custom", "自定义"),
            ("none", "无特效"),
            ("fade", "淡入淡出"),
            ("slide", "滑入滑出"),
            ("char_fade", "逐字淡入淡出"),
            ("char_drip", "逐字垂落"),
            ("utopia", "Utopia"),
        ):
            self._preset_combo.addItem(label, userData=value)
        self._entry_combo = _StableFluentComboBox(self)
        self._exit_combo = _StableFluentComboBox(self)
        for value, label in _ENTRY_EFFECTS:
            self._entry_combo.addItem(label, userData=value)
        for value, label in _EXIT_EFFECTS:
            self._exit_combo.addItem(label, userData=value)
        self._karaoke_combo = _StableFluentComboBox(self)
        for value, label in _KARAOKE_EFFECTS:
            self._karaoke_combo.addItem(label, userData=value)
        self._entry_duration = FluentSpinBox(self)
        self._exit_duration = FluentSpinBox(self)
        for spin in (self._entry_duration, self._exit_duration):
            spin.setRange(0, 10_000)
            spin.setSuffix(" ms")
            spin.setSingleStep(50)

        entry = style.entry_anim if override is None else override.entry_anim
        exit_ = style.exit_anim if override is None else override.exit_anim
        entry_ms = style.entry_lead_ms if override is None else override.entry_duration_ms
        exit_ms = style.exit_fade_ms if override is None else override.exit_duration_ms
        karaoke = style.karaoke_anim if override is None else override.karaoke_anim
        self._set_combo_value(self._entry_combo, entry)
        self._set_combo_value(self._exit_combo, exit_)
        self._set_combo_value(self._karaoke_combo, karaoke)
        self._entry_duration.setValue(max(int(entry_ms), 0))
        self._exit_duration.setValue(max(int(exit_ms), 0))
        form.addRow("快捷组合", self._preset_combo)
        form.addRow("入场", self._entry_combo)
        form.addRow("入场时长", self._entry_duration)
        form.addRow("退场", self._exit_combo)
        form.addRow("退场时长", self._exit_duration)
        form.addRow("唱字特效", self._karaoke_combo)
        root.addLayout(form)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = FluentPushButton("取消", self)
        confirm = FluentPrimaryPushButton("确定", self)
        cancel.clicked.connect(self.reject)
        confirm.clicked.connect(self.accept)
        buttons.addWidget(cancel)
        buttons.addWidget(confirm)
        root.addLayout(buttons)

        self._inherit_check.toggled.connect(self._sync_enabled)
        self._preset_combo.activated.connect(self._apply_preset)
        self._sync_enabled(self._inherit_check.isChecked())

    @staticmethod
    def _set_combo_value(combo: FluentComboBox, value: str) -> None:
        index = combo.findData(value)
        combo.setCurrentIndex(index if index >= 0 else 0)

    def _sync_enabled(self, inherit: bool) -> None:
        for widget in (
            self._entry_combo,
            self._preset_combo,
            self._entry_duration,
            self._exit_combo,
            self._exit_duration,
            self._karaoke_combo,
        ):
            widget.setEnabled(not inherit)

    def _apply_preset(self, _index: int) -> None:
        preset = str(self._preset_combo.currentData() or "custom")
        mapping = {
            "none": ("none", "none"),
            "fade": ("fade", "fade"),
            "slide": ("slide_in", "slide_out"),
            "char_fade": ("char_fade", "char_fade"),
            "char_drip": ("char_drip", "char_drip"),
            "utopia": ("utopia", "utopia"),
        }
        pair = mapping.get(preset)
        if pair is None:
            return
        self._set_combo_value(self._entry_combo, pair[0])
        self._set_combo_value(self._exit_combo, pair[1])

    def animation_override(self) -> Optional[LineAnimationOverride]:
        if self._inherit_check.isChecked():
            return None
        return LineAnimationOverride(
            entry_anim=str(self._entry_combo.currentData() or "none"),
            entry_duration_ms=self._entry_duration.value(),
            exit_anim=str(self._exit_combo.currentData() or "none"),
            exit_duration_ms=self._exit_duration.value(),
            karaoke_anim=str(self._karaoke_combo.currentData() or "inherit"),
        )


class _StableComboBoxMenu(ComboBoxMenu):
    """避开 qfluentwidgets 1.11.2 的菜单关闭动画销毁竞争。"""

    def exec(self, pos, ani=True, aniType=MenuAnimationType.DROP_DOWN):
        # ComboBoxMenu 带 WA_DeleteOnClose。若用户在 250 ms 弹出动画结束前
        # 选中项目，菜单 view 会先销毁，但动画的 valueChanged 仍会调用
        # MenuAnimationManager._updateMenuViewport()，导致包装 C++ 对象已删除异常。
        # NONE manager 不启动 QPropertyAnimation，因此关闭菜单时没有悬空回调。
        return super().exec(pos, ani, MenuAnimationType.NONE)


class _StableFluentComboBox(FluentComboBox):
    def _createComboMenu(self):
        return _StableComboBoxMenu(self)


class _StableRoundMenu(RoundMenu):
    """Fluent context menu without the close-animation teardown race."""

    def exec(self, pos, ani=True, aniType=MenuAnimationType.DROP_DOWN):
        return super().exec(pos, ani, MenuAnimationType.NONE)


# 色点只由颜色决定。整表刷新会逐行重建它们（68 行 = 68 个新 QPixmap），
# 而全新的图标 Qt 一个都缓存不上，之后那次视口重绘就变得很贵：实测表格平时
# 重绘 9ms，紧接一次编辑之后要 44ms 起，是编辑卡顿的直接来源。按颜色缓存后，
# 一次改色只需要重建真正变了色的那一两个。
_SWATCH_ICON_CACHE: "OrderedDict[tuple, QIcon]" = OrderedDict()
_SWATCH_ICON_CACHE_MAX = 256
_SWATCH_REFRESH_DEBOUNCE_MS = 250


def _swatch_cache_key(color: Optional[QColor]) -> tuple:
    return (None,) if color is None else (color.rgba(),)


def _cached_icon(key: tuple, build) -> QIcon:
    icon = _SWATCH_ICON_CACHE.get(key)
    if icon is None:
        icon = build()
        _SWATCH_ICON_CACHE[key] = icon
        while len(_SWATCH_ICON_CACHE) > _SWATCH_ICON_CACHE_MAX:
            _SWATCH_ICON_CACHE.popitem(last=False)
    else:
        _SWATCH_ICON_CACHE.move_to_end(key)
    return icon


def _swatch_icon(color: Optional[QColor]) -> QIcon:
    """圆形配色色点；``None``（未定义方案）画空心灰圈。"""
    return _cached_icon(("solid", *_swatch_cache_key(color)), lambda: _swatch_icon_uncached(color))


def _swatch_icon_uncached(color: Optional[QColor]) -> QIcon:
    pixmap = QPixmap(24, 24)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    if color is None:
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(148, 163, 184, 200), 2))
    else:
        painter.setBrush(color)
        # 描一圈半透明深边，浅色（白）色点在白底上也能看清
        painter.setPen(QPen(QColor(0, 0, 0, 64), 1.5))
    painter.drawEllipse(5, 5, 14, 14)
    painter.end()
    return QIcon(pixmap)


def _scheme_swatch_color(style: Style, role: str) -> Optional[QColor]:
    """角色名 → 该配色方案的代表色（已唱填充色）；无方案返回 None。"""
    if not role:
        # 全局默认方案
        if style.karaoke_colors is not None:
            return QColor(style.karaoke_colors.after.text.color)
        return QColor(style.fill_color)
    scheme = style.custom_style_schemes.get(role)
    if scheme is None:
        return None
    if scheme.karaoke_colors is not None:
        return QColor(scheme.karaoke_colors.after.text.color)
    if scheme.fill_color:
        return QColor(scheme.fill_color)
    return None


def _mixed_swatch_icon(colors: list[Optional[QColor]]) -> QIcon:
    """混合角色行的双色点（左右各半圆，取行内前两种代表色）。"""
    key = ("mixed",) + tuple(
        _swatch_cache_key(color)[0] for color in (colors + [None, None])[:2]
    )
    return _cached_icon(key, lambda: _mixed_swatch_icon_uncached(colors))


def _mixed_swatch_icon_uncached(colors: list[Optional[QColor]]) -> QIcon:
    pixmap = QPixmap(24, 24)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    halves = (colors + [None, None])[:2]
    for index, color in enumerate(halves):
        painter.setBrush(color if color is not None else QColor(148, 163, 184, 120))
        painter.setPen(Qt.PenStyle.NoPen)
        start = (90 if index == 0 else 270) * 16
        painter.drawPie(5, 5, 14, 14, start, 180 * 16)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.setPen(QPen(QColor(0, 0, 0, 64), 1.5))
    painter.drawEllipse(5, 5, 14, 14)
    painter.end()
    return QIcon(pixmap)


def _guide_symbol_icon(symbol: GuideSymbol) -> QIcon:
    pixmap = QPixmap(24, 24)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    path = scaled_guide_symbol_path(
        symbol, pixel_size=18, left=3, baseline_y=21
    )
    painter.fillPath(path, QColor(palette().title_text))
    painter.end()
    return QIcon(pixmap)


def _line_role_labels(line) -> list[str]:
    """行内非空白字符的角色标签序列（无角色为空串）。"""
    labels = []
    if line.guide_symbol is not None:
        labels.extend(
            str(label or "") for label in guide_symbol_role_labels(line.guide_symbol)
        )
    labels.extend(
        str(ch.role_label or "") for ch in line_visible_chars(line) if ch.text.strip()
    )
    return labels


def _line_content_text(line: TimingLine) -> str:
    """Preview source text while making persisted inline SVG replacements visible."""
    start = guide_symbol_replacement_count(line)
    return "".join(
        "◆"
        if (
            (symbol := line.inline_guide_symbols.get(index)) is not None
            and symbol.path_commands
        )
        else char.text
        for index, char in enumerate(line.chars[start:], start=start)
    )


def _line_guide_tooltip(line: TimingLine) -> str:
    parts: list[str] = []
    if line.guide_symbol is not None:
        if line.guide_symbol.replacement_prefix:
            marker = "".join(line.guide_symbol.replacement_prefix)
            parts.append(
                f"行首标记替换：{marker} → {line.guide_symbol.name} · "
                f"{line.guide_symbol.count} 个 · 保留原打轴时间"
            )
        else:
            parts.append(
                f"行前导唱符：{line.guide_symbol.name} · "
                f"{line.guide_symbol.count} 个 · 间隔 {line.guide_symbol.duration_ms} ms"
            )
    inline_count = sum(
        1
        for index, symbol in line.inline_guide_symbols.items()
        if 0 <= index < len(line.chars) and symbol.path_commands
    )
    if inline_count:
        parts.append(f"行内 SVG 导唱符：{inline_count} 个 · 保留原字符打轴时间")
    return "\n".join(parts)


def _line_guide_icon_symbol(line: TimingLine) -> Optional[GuideSymbol]:
    """Return the representative outline shown beside the lyrics preview text."""
    if line.guide_symbol is not None and line.guide_symbol.path_commands:
        return line.guide_symbol
    start = guide_symbol_replacement_count(line)
    for index in range(start, len(line.chars)):
        symbol = line.inline_guide_symbols.get(index)
        if symbol is not None and symbol.path_commands:
            return symbol
    return None


def _line_role_mixed(line) -> bool:
    """行内是否存在 ≥2 种不同角色（含「无角色」与具名角色混合）。"""
    return len(set(_line_role_labels(line))) > 1


class _GroupBackgroundDelegate(QStyledItemDelegate):
    """在 QSS item 背景之上自绘"双行组"底色的基类。

    qfluentwidgets 的 TableWidget QSS 会压制 item 的 ``BackgroundRole``，
    所以组底色不能走 ``setBackground``，改在这里 fillRect。
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._group_bg_provider = None

    def set_group_bg_provider(self, provider) -> None:
        """``provider(row) -> Optional[QColor]``，返回该行的组底色。"""
        self._group_bg_provider = provider

    def paint(self, painter, option, index):  # type: ignore[override]
        # 选中态自己画：qfluentwidgets 表格 QSS 的 ::item:selected 会把文字刷成
        # 白色，压在浅底上完全看不清。剥掉 State_Selected（QSS 选中规则不再命中，
        # 文字保持正常深色），底色换成主题浅粉。
        opt = QStyleOptionViewItem(option)
        selected = bool(opt.state & QStyle.StateFlag.State_Selected)
        if selected:
            opt.state &= ~QStyle.StateFlag.State_Selected
        if self._group_bg_provider is not None:
            color = self._group_bg_provider(index.row())
            if color is not None:
                painter.fillRect(opt.rect, color)
        if selected:
            painter.fillRect(opt.rect, QColor(palette().preview_selection_bg))
        # 角色 / 特效列把「色点图标 + 文本」整组水平居中。setTextAlignment
        # 只居中文字、图标仍钉在左缘，所以改为把绘制矩形收窄到内容理想宽
        # 后居中放置，再按默认左对齐绘制；空间不足时保持原样走省略号。
        if index.column() in (COL_ROLE, COL_EFFECT):
            hint = self.sizeHint(option, index).width()
            if 0 < hint < opt.rect.width():
                dx = (opt.rect.width() - hint) // 2
                opt.rect = opt.rect.adjusted(dx, 0, -dx, 0)
        super().paint(painter, opt, index)


class _ReadOnlyDelegate(_GroupBackgroundDelegate):
    """禁止编辑的委托——轨 / 内容列使用。"""

    def createEditor(self, parent, option, index):  # type: ignore[override]
        return None


_LAYOUT_PRESENTATION_FIELDS: tuple[str, ...] = (
    # lane / 分页
    "dual_line_layout",
    "line_alignments",
    "section_gap_ms",
    # 内容列对齐
    "line_horizontal_layout",
    "row1_align",
    "row2_align",
    "smart_horizontal",
    # 布局列名称 + 逐行布局套用（_effective_layout_style）
    "layouts",
)
"""会改变行内容或列宽的字段——只有它们需要重新量列宽。"""

_SWATCH_PRESENTATION_FIELDS: tuple[str, ...] = (
    "fill_color",
    "karaoke_colors",
    "custom_style_schemes",
)
"""只改角色色点颜色的字段。"""


def _layout_presentation_signature(style: Style) -> tuple:
    """影响行内容 / 列宽的取值签名。

    角色名的集合也算在内：新增或改名会改变角色列里的文字，进而改变列宽；但同一
    个方案里换个颜色不会。
    """
    return tuple(getattr(style, name) for name in _LAYOUT_PRESENTATION_FIELDS) + (
        tuple(sorted(style.custom_style_schemes)),
    )


def _swatch_presentation_signature(style: Style) -> tuple:
    """只影响角色色点颜色的取值签名。"""
    return tuple(getattr(style, name) for name in _SWATCH_PRESENTATION_FIELDS)


def _effective_layout_style(style: Style, line: TimingLine) -> Style:
    """行引用的布局套用到 style 上（与渲染端 ``_layout_style_for_line`` 同语义）。"""
    index = int(getattr(line, "layout_index", 0) or 0)
    if index <= 0 or index > len(style.layouts):
        return style
    layout = style.layouts[index - 1]
    return _dataclass_replace(
        style, **{name: getattr(layout, name) for name in LYRICS_LAYOUT_FIELDS}
    )


class _CharChipsView(QWidget):
    """逐字符角色对话框的字符块视图：流式排布 + 拖选 / Ctrl 点选 / Shift 范围。

    每字一块，底色 = 该字符角色方案的代表色（浅），底缘一条实色角色条；
    无角色为中性块。选中块以主题色描边。纯自绘，块序即字符序。
    """

    selectionChanged = Signal()
    assignmentChanged = Signal()

    _CHIP_W = 46
    _CHIP_H = 56
    _GAP = 6

    def __init__(
        self,
        texts: list[str],
        labels: list[Optional[str]],
        style: Style,
        default_swatch_role: str = "",
        parent: Optional[QWidget] = None,
        *,
        default_role_text: str = _DEFAULT_ROLE_TEXT,
        vector_symbols: Optional[list[Optional[GuideSymbol]]] = None,
    ) -> None:
        super().__init__(parent)
        self._texts = list(texts)
        self._labels: list[Optional[str]] = [label or None for label in labels]
        self._style = style
        self._default_swatch_role = default_swatch_role
        self._default_role_text = default_role_text
        self._vector_symbols = list(vector_symbols or [None] * len(self._texts))
        self._vector_symbols.extend(
            [None] * max(len(self._texts) - len(self._vector_symbols), 0)
        )
        self._selected: set[int] = set()
        self._anchor: Optional[int] = None
        self._dragging = False
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    # ------------------------------------------------------------- 数据接口

    def labels(self) -> list[Optional[str]]:
        return list(self._labels)

    def vector_symbols(self) -> list[Optional[GuideSymbol]]:
        return list(self._vector_symbols[: len(self._texts)])

    def selected_indices(self) -> set[int]:
        return set(self._selected)

    def selected_role_labels(self) -> set[Optional[str]]:
        return {
            self._labels[index]
            for index in self._selected
            if 0 <= index < len(self._labels)
        }

    def selected_has_vector_symbol(self, *, minimum_index: int = 0) -> bool:
        return any(
            minimum_index <= index < len(self._vector_symbols)
            and self._vector_symbols[index] is not None
            for index in self._selected
        )

    def role_tooltip_text(self, index: int) -> str:
        if not 0 <= index < len(self._labels):
            return ""
        prefix = "导唱符 · " if self._vector_symbols[index] is not None else ""
        return f"{prefix}角色方案：{self._labels[index] or self._default_role_text}"

    def apply_role(self, label: Optional[str]) -> None:
        for index in self._selected:
            if 0 <= index < len(self._labels):
                self._labels[index] = label or None
        self.assignmentChanged.emit()
        self.update()

    def replace_selected_vector_symbol(
        self, symbol: GuideSymbol, *, minimum_index: int = 0
    ) -> None:
        for index in self._selected:
            if minimum_index <= index < len(self._vector_symbols):
                self._vector_symbols[index] = symbol
        self.assignmentChanged.emit()
        self.update()

    def clear_selected_vector_symbols(self, *, minimum_index: int = 0) -> None:
        changed = False
        for index in self._selected:
            if (
                minimum_index <= index < len(self._vector_symbols)
                and self._vector_symbols[index] is not None
            ):
                self._vector_symbols[index] = None
                changed = True
        if changed:
            self.assignmentChanged.emit()
            self.update()

    def select_all(self) -> None:
        self._selected = set(range(len(self._texts)))
        self.selectionChanged.emit()
        self.update()

    # ------------------------------------------------------------- 布局几何

    def _columns(self) -> int:
        return max(1, (self.width() + self._GAP) // (self._CHIP_W + self._GAP))

    def hasHeightForWidth(self) -> bool:  # noqa: N802 - Qt API
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802 - Qt API
        columns = max(1, (width + self._GAP) // (self._CHIP_W + self._GAP))
        rows = max(1, -(-len(self._texts) // columns))
        return rows * (self._CHIP_H + self._GAP) - self._GAP

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt API
        return QSize(self._CHIP_W * 6 + self._GAP * 5, self._CHIP_H)

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt API
        width = self._CHIP_W * 12 + self._GAP * 11
        return QSize(width, self.heightForWidth(width))

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(event)
        # 在 QScrollArea 里 heightForWidth 不会自动传导，按当前宽度钉住最小高。
        self.setMinimumHeight(self.heightForWidth(self.width()))
        self.updateGeometry()

    def _chip_rect(self, index: int) -> QRectF:
        columns = self._columns()
        row, col = divmod(index, columns)
        return QRectF(
            col * (self._CHIP_W + self._GAP),
            row * (self._CHIP_H + self._GAP),
            self._CHIP_W,
            self._CHIP_H,
        )

    def _index_at(self, pos) -> Optional[int]:
        columns = self._columns()
        col = int(pos.x()) // (self._CHIP_W + self._GAP)
        row = int(pos.y()) // (self._CHIP_H + self._GAP)
        if col >= columns:
            col = columns - 1
        index = row * columns + col
        if 0 <= index < len(self._texts) and self._chip_rect(index).adjusted(
            -self._GAP / 2, -self._GAP / 2, self._GAP / 2, self._GAP / 2
        ).contains(pos):
            return index
        return None

    # ------------------------------------------------------------- 交互

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt API
        index = self._index_at(event.position())
        modifiers = event.modifiers()
        if index is None:
            if not modifiers & Qt.KeyboardModifier.ControlModifier:
                self._selected.clear()
                self.selectionChanged.emit()
                self.update()
            return
        if modifiers & Qt.KeyboardModifier.ControlModifier:
            if index in self._selected:
                self._selected.discard(index)
            else:
                self._selected.add(index)
            self._anchor = index
        elif modifiers & Qt.KeyboardModifier.ShiftModifier and self._anchor is not None:
            low, high = sorted((self._anchor, index))
            self._selected = set(range(low, high + 1))
        else:
            self._anchor = index
            self._selected = {index}
            self._dragging = True
        self.selectionChanged.emit()
        self.update()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt API
        if not self._dragging or self._anchor is None:
            return
        index = self._index_at(event.position())
        if index is None:
            return
        low, high = sorted((self._anchor, index))
        new_selection = set(range(low, high + 1))
        if new_selection != self._selected:
            self._selected = new_selection
            self.selectionChanged.emit()
            self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt API
        self._dragging = False
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt API
        if event.matches(QKeySequence.StandardKey.SelectAll):
            self.select_all()
            return
        super().keyPressEvent(event)

    def event(self, event) -> bool:  # noqa: A003 - Qt API
        if event.type() == QEvent.Type.ToolTip:
            index = self._index_at(QPointF(event.pos()))
            text = self.role_tooltip_text(index) if index is not None else ""
            if text:
                show_fluent_tooltip(text, parent=self, global_pos=event.globalPos())
            else:
                hide_fluent_tooltip(parent=self)
            return True
        return super().event(event)

    # ------------------------------------------------------------- 绘制

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt API
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        text_color = QColor(palette().title_text)
        neutral_border = QColor(148, 163, 184, 150)
        accent = QColor(palette().accent_primary)
        font = QFont(self.font())
        font.setPixelSize(24)
        font.setBold(True)
        painter.setFont(font)
        for index, text in enumerate(self._texts):
            rect = self._chip_rect(index)
            label = self._labels[index] or ""
            swatch_role = label or self._default_swatch_role
            swatch = _scheme_swatch_color(self._style, swatch_role) if swatch_role else None
            # 底色：有角色 → 代表色浅化；无角色 → 透明中性块
            if swatch is not None:
                bg = QColor(swatch)
                bg.setAlpha(46)
                painter.setBrush(bg)
            else:
                painter.setBrush(Qt.BrushStyle.NoBrush)
            if index in self._selected:
                painter.setPen(QPen(accent, 2))
            else:
                painter.setPen(QPen(neutral_border, 1))
            painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 6, 6)
            # 底缘角色条（实色），无角色不画
            if swatch is not None:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(swatch)
                painter.drawRoundedRect(
                    QRectF(rect.left() + 5, rect.bottom() - 8, rect.width() - 10, 4), 2, 2
                )
            painter.setPen(text_color)
            symbol = self._vector_symbols[index]
            if symbol is not None:
                glyph_path = scaled_guide_symbol_path(
                    symbol,
                    pixel_size=30,
                    left=rect.center().x() - 15,
                    baseline_y=rect.center().y() + 13,
                )
                painter.fillPath(glyph_path, text_color)
            else:
                painter.drawText(
                    rect.adjusted(0, 2, 0, -8),
                    Qt.AlignmentFlag.AlignCenter,
                    text if text.strip() else "␣",
                )
        painter.end()


class _CharRoleDialog(ModelessDialog):
    """行内逐字符角色编辑器：拖选字符块 → 点角色按钮应用 → 确定写回。"""

    def __init__(
        self,
        row: int,
        texts: list[str],
        labels: list[Optional[str]],
        role_options: list[str],
        style: Style,
        parent: Optional[QWidget] = None,
        *,
        default_role_text: str = _DEFAULT_ROLE_TEXT,
        default_swatch_role: str = "",
        vector_symbols: Optional[list[Optional[GuideSymbol]]] = None,
        protected_prefix_count: int = 0,
        allow_svg_replacement: bool = True,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"逐字符角色 · 第 {row + 1} 行")
        self._style = style
        self._role_buttons: list[FluentToggleButton] = []
        self._default_role_text = default_role_text
        self._protected_prefix_count = max(int(protected_prefix_count), 0)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 12)
        layout.setSpacing(10)

        hint = QLabel("拖动选择字符（Ctrl 点选 / Shift 连选 / Ctrl+A 全选），再点下方角色应用。", self)
        hint.setWordWrap(True)
        themed(hint, lambda: f"color: {palette().text_secondary}; font-size: 9pt;")
        layout.addWidget(hint)

        self._chips = _CharChipsView(
            texts,
            labels,
            style,
            default_swatch_role,
            self,
            default_role_text=default_role_text,
            vector_symbols=vector_symbols,
        )
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(self._chips)
        scroll.setMinimumHeight(self._chips._CHIP_H + 24)
        scroll.setMaximumHeight((self._chips._CHIP_H + self._chips._GAP) * 4)
        layout.addWidget(scroll, 1)

        self._roles_label = QLabel("分配为：", self)
        themed(
            self._roles_label,
            lambda: f"color: {palette().text_secondary}; font-size: 9pt;",
        )
        layout.addWidget(self._roles_label)

        roles_host = QWidget(self)
        self._roles_flow = FlowLayout(roles_host, needAni=False)
        self._roles_flow.setContentsMargins(0, 0, 0, 0)
        self._default_swatch_role = default_swatch_role
        self._add_role_button("", default_role_text)
        for name in role_options:
            self._add_role_button(name, name)
        new_button = FluentPushButton("＋新建角色", roles_host)
        new_button.clicked.connect(self._create_role)
        self._roles_flow.addWidget(new_button)
        layout.addWidget(roles_host)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self._replace_svg_button = FluentPushButton("替换为 SVG 导唱符…", self)
        self._replace_svg_button.setAutoDefault(False)
        self._replace_svg_button.clicked.connect(self._replace_selected_with_svg)
        self._replace_svg_button.setVisible(bool(allow_svg_replacement))
        buttons.addWidget(self._replace_svg_button)
        self._restore_svg_button = FluentPushButton("还原 SVG 为原字符", self)
        self._restore_svg_button.setAutoDefault(False)
        self._restore_svg_button.clicked.connect(self._restore_selected_svg)
        self._restore_svg_button.setVisible(bool(allow_svg_replacement))
        buttons.addWidget(self._restore_svg_button)
        cancel = FluentPushButton("取消", self)
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        ok = FluentPrimaryPushButton("确定", self)
        ok.clicked.connect(self.accept)
        buttons.addWidget(ok)
        layout.addLayout(buttons)

        self._chips.selectionChanged.connect(self._sync_role_buttons_enabled)
        self._chips.assignmentChanged.connect(self._sync_role_buttons_enabled)
        self._sync_role_buttons_enabled()
        self.resize(720, 380)

    def _add_role_button(self, label: str, text: str) -> None:
        button = FluentToggleButton(text, self)
        button.setProperty("roleLabel", label)
        swatch_role = label or self._default_swatch_role
        button.setIcon(_swatch_icon(_scheme_swatch_color(self._style, swatch_role)))
        button.clicked.connect(
            lambda _checked=False, l=label: self._chips.apply_role(l or None)
        )
        self._roles_flow.insertWidget(len(self._role_buttons), button)
        self._role_buttons.append(button)

    def _sync_role_buttons_enabled(self) -> None:
        enabled = bool(self._chips.selected_indices())
        assigned = self._chips.selected_role_labels()
        current = next(iter(assigned)) if len(assigned) == 1 else None
        if not enabled:
            self._roles_label.setText("分配为：")
        elif len(assigned) == 1:
            self._roles_label.setText(
                f"当前分配：{current or self._default_role_text}"
            )
        else:
            self._roles_label.setText(f"当前分配：混合（{len(assigned)} 种方案）")
        for button in self._role_buttons:
            button.setEnabled(enabled)
            button.setChecked(
                enabled
                and len(assigned) == 1
                and str(button.property("roleLabel") or "") == str(current or "")
            )
        self._replace_svg_button.setEnabled(
            any(
                index >= self._protected_prefix_count
                for index in self._chips.selected_indices()
            )
        )
        self._restore_svg_button.setEnabled(
            self._chips.selected_has_vector_symbol(
                minimum_index=self._protected_prefix_count
            )
        )

    def _replace_selected_with_svg(self) -> None:
        path_text, _ = QFileDialog.getOpenFileName(
            self, "选择 SVG 导唱符", "", "SVG 文件 (*.svg)"
        )
        if not path_text:
            return
        try:
            symbol = import_svg_guide_symbol(Path(path_text), count=1)
        except GuideSymbolImportError as exc:
            fluent_error(self, "无法导入导唱符", str(exc))
            return
        self._chips.replace_selected_vector_symbol(
            symbol, minimum_index=self._protected_prefix_count
        )

    def _restore_selected_svg(self) -> None:
        self._chips.clear_selected_vector_symbols(
            minimum_index=self._protected_prefix_count
        )

    def _create_role(self) -> None:
        name, ok = fluent_get_text(self, "新建角色", "角色名称")
        if not ok:
            return
        name = name.strip()
        if not name:
            return
        existing = {button.text() for button in self._role_buttons}
        if name not in existing:
            self._add_role_button(name, name)
            self._sync_role_buttons_enabled()
        if self._chips.selected_indices():
            self._chips.apply_role(name)

    def char_labels(self) -> list[Optional[str]]:
        return self._chips.labels()

    def char_vector_symbols(self) -> list[Optional[GuideSymbol]]:
        return self._chips.vector_symbols()


class LyricsPanel(DropPanel):
    """左侧歌词面板（含空态拖拽 + 已加载表格两态）。"""

    roleChanged = Signal(int, str)
    roleChangeRequested = Signal(list, str)
    """批量整行角色覆盖：所选 track.lines 行号列表 + 角色名（空串为全局默认）。"""
    charRolesChanged = Signal(int, list)
    """逐字符角色编辑：track.lines 行号 + 与该行字符数等长的标签列表（str | None）。"""
    guideCharRolesChanged = Signal(int, object, list)
    """含导唱符行的逐字符结果：行号、各导唱符角色列表、可见歌词角色列表。"""
    inlineCharEditChanged = Signal(int, object, list, object)
    """逐字符 SVG/角色结果：行号、行前导唱角色、可见字符角色、可见字符 SVG。"""
    guideSymbolImportRequested = Signal(list)
    guideSymbolRemoveRequested = Signal(list)
    guidePrefixReplaceRequested = Signal()
    #: 右键「自动识别和声…」——整源操作，不带行号。
    autoChorusRequested = Signal()
    titleEditRequested = Signal()
    """标题模式打开逐字符编辑器前，请宿主把动态模板固定为当前实际文字。"""
    animationOverrideRequested = Signal(list, object)
    """逐行动画编辑请求：track.lines 行号列表 + ``LineAnimationOverride | None``。"""
    rowClicked = Signal(int)  # 用户点击歌词行时发出行号
    layoutChangeRequested = Signal(list, int)
    """右键菜单选择布局：(选中的 track.lines 行号列表, 布局 index)。宿主按页联动应用。"""
    sourceSelected = Signal(int)
    """字幕源下拉切换：0 = 主字幕，k >= 1 = 第 k 个副字幕源。"""
    sourceAddRequested = Signal()
    """「＋」按钮：请求添加副字幕源（N3 多歌词文件，如コーラス轨）。"""
    sourceRemoveRequested = Signal(int)
    """「－」按钮：请求移除当前选中的副字幕源（主字幕不可移除）。"""
    sourceReplaceRequested = Signal(int)
    """「文件」按钮：请求换掉当前选中源的歌词文件（0 = 主字幕）。"""
    sourceRefreshRequested = Signal()
    sourceSettingsRequested = Signal(object)
    pageBoundaryRequested = Signal(str, int)
    """insert_page/delete_page/insert_section/delete_section, track line index."""
    pageMoveRequested = Signal(int, int, int)
    """section index, page index, direction; lyric order is never changed."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(
            extensions={".sug", ".lrc", ".yurika", ".n3proj"},
            empty_title="拖入字幕文件",
            empty_hint="拖入 SUG 项目（.sug）、Nicokara 逐字 LRC（.lrc）\nYurika 工程（.yurika）或 N3 项目（.n3proj）",
            empty_icon="📝",
            parent=parent,
        )
        # 色点只是配色的一个指示，不必跟住拖动配色时的每一个中间值。整表换色要
        # 重写 68 个单元格并让 Qt 重绘整个视口（平时 9ms，改色后 37ms），全在界面
        # 线程上。攒到停手后刷一次：连续调色期间界面不再被打断，视觉结果一致。
        self._swatch_refresh_timer = QTimer(self)
        self._swatch_refresh_timer.setSingleShot(True)
        self._swatch_refresh_timer.setInterval(_SWATCH_REFRESH_DEBOUNCE_MS)
        self._swatch_refresh_timer.timeout.connect(self._flush_swatch_refresh)
        self.setObjectName("LyricsPanel")
        themed(self, self._panel_qss)

        self._style: Style = Style()
        self._track: Optional[TimingTrack] = None
        self._title_mode = False
        self._role_options: list[str] = []
        # 每行元数据：(是否空行, 可渲染序号)；空行序号取 -1。
        # lane / 组号由序号 + 当前 Style 的行数动态推出（行数可随布局变化）。
        self._row_meta: list[tuple[bool, int]] = []
        self._presentation_rows: list[LyricsPresentationRow] = []
        self._resolved_page_plan: Optional[ResolvedPagePlan] = None
        # 段落最后一行标记（与渲染端 NKM3 式段落划分一致），随 style 阈值重算
        # 每个可渲染行的 lane / 页序号缓存（页首行布局定行数，与渲染端一致）
        self._render_lanes: list[int] = []
        self._render_groups: list[int] = []
        self._page_drag_row: Optional[int] = None
        self._page_drag_start = QPoint()
        self._page_drag_active = False
        self._header_drag: Optional[tuple[int, int]] = None
        """内容|布局边界把手的进行中拖动：(按下 x, 按下时布局列宽)。"""

        # ---- qfluentwidgets TableWidget ----
        self._table = FluentTableWidget(self)
        self._table.setObjectName("LyricsTable")
        self._table.setColumnCount(5)
        self._table.setHorizontalHeaderLabels(_COLUMN_HEADERS)

        self._table.setFrameShape(FluentTableWidget.Shape.NoFrame)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._show_context_menu)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked
                                    | QAbstractItemView.EditTrigger.EditKeyPressed)
        self._table.setShowGrid(False)
        self._table.setWordWrap(False)
        self._table.setTextElideMode(Qt.TextElideMode.ElideRight)
        # 列宽在下方被钳制到永不超出 viewport（内容列 Stretch 吸收余量、
        # 拖拽有上限），横向滚动没有存在意义；关掉以保证任何状态下
        # 都不会把内容列滚出可视区。
        self._table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._table.setIconSize(QSize(14, 14))
        self._page_drag_indicator = QFrame(self._table.viewport())
        self._page_drag_indicator.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents
        )
        self._page_drag_indicator.setStyleSheet(
            f"background: {palette().accent_primary}; border: none;"
        )
        self._page_drag_indicator.hide()

        self._table.verticalHeader().setVisible(False)
        self._table.verticalHeader().setDefaultSectionSize(_ROW_HEIGHT)

        # 两级宽度策略：轨列按内容紧凑测量，角色 / 特效允许用户调整，内容列
        # Stretch 填满 viewport 剩余空间。这样拖宽角色 / 特效时由内容列吸收，
        # 不会把表格的绘制区域推到右侧属性面板下面。
        hh = self._table.horizontalHeader()
        hh.setSectionResizeMode(COL_LANE, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(COL_ROLE, QHeaderView.ResizeMode.Interactive)
        hh.setSectionResizeMode(COL_EFFECT, QHeaderView.ResizeMode.Interactive)
        hh.setSectionResizeMode(COL_CONTENT, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(COL_LAYOUT, QHeaderView.ResizeMode.Interactive)
        # QHeaderView 只提供全局 minimumSectionSize。这里仅设置由当前 style
        # header margin 推导出的 DPI 感知技术下限；各列语义下限由下方字体与
        # 实际单元格 sizeHint 测量分别控制，不共享固定像素值。
        header_margin = self._table.style().pixelMetric(QStyle.PixelMetric.PM_HeaderMargin)
        hh.setMinimumSectionSize(max(1, header_margin * 2))
        self._setting_column_widths = False
        self._user_sized_columns: set[int] = set()
        hh.sectionResized.connect(self._on_column_resized)
        # viewport 尺寸变化（拖 splitter / 缩放窗口 / 竖直滚动条出现）时重测列宽，
        # 是响应式的唯一可靠信号——resizeEvent 时机早于子布局落位。
        self._table.viewport().installEventFilter(self)
        # 内容列（Stretch）右缘的把手 Qt 会映射给内容列自己，而 Stretch 列不可
        # 拖，这条边界就成了死把手。接管它的把手区拖动，改为调整右邻的布局列。
        self._table.horizontalHeader().viewport().installEventFilter(self)

        # 表格内只展示角色，不再叠加半透明 ComboBox 编辑器。单击角色列时
        # 在单元格下方弹出 Fluent RoundMenu，避免底层文字 / 色点透出重影。
        self._role_delegate = _ReadOnlyDelegate(self)
        self._readonly_delegate = _ReadOnlyDelegate(self)
        for delegate in (self._role_delegate, self._readonly_delegate):
            delegate.set_group_bg_provider(self._group_bg_for_row)
        self._table.setItemDelegateForColumn(COL_LANE, self._readonly_delegate)
        self._table.setItemDelegateForColumn(COL_ROLE, self._role_delegate)
        self._table.setItemDelegateForColumn(COL_EFFECT, self._readonly_delegate)
        self._table.setItemDelegateForColumn(COL_CONTENT, self._readonly_delegate)
        self._table.setItemDelegateForColumn(COL_LAYOUT, self._readonly_delegate)

        # ---- 字幕源工具条（对标 N3 多歌词文件：メイン / コーラス1 / …）----
        self._syncing_sources = False
        self._removable_source_indices: set[int] = set()
        self._replaceable_source_indices: set[int] = set()
        self._source_bar = QWidget(self)
        source_layout = QHBoxLayout(self._source_bar)
        source_layout.setContentsMargins(8, 6, 8, 0)
        source_layout.setSpacing(6)
        self._source_combo = _StableFluentComboBox(self._source_bar)
        self._source_combo.setToolTip("切换列表显示的字幕源（预览与导出始终同时渲染全部源）")
        self._source_combo.currentIndexChanged.connect(self._on_source_combo_changed)
        self._add_source_btn = TransparentToolButton(FIF.ADD, self._source_bar)
        self._add_source_btn.setToolTip("添加副字幕源（如コーラス .sug/.lrc，与主字幕同时显示）")
        self._add_source_btn.clicked.connect(self.sourceAddRequested)
        self._remove_source_btn = TransparentToolButton(FIF.REMOVE, self._source_bar)
        self._remove_source_btn.setToolTip("移除当前副字幕源")
        self._remove_source_btn.clicked.connect(self._on_remove_source_clicked)
        self._replace_source_btn = TransparentToolButton(FIF.FOLDER, self._source_bar)
        self._replace_source_btn.setToolTip(
            "换掉当前选中字幕源的歌词文件（保留该源的其他设置槽位）。"
            "把文件直接拖到歌词列表上等效。"
        )
        self._replace_source_btn.clicked.connect(self._on_replace_source_clicked)
        self._refresh_source_btn = TransparentToolButton(FIF.SYNC, self._source_bar)
        self._refresh_source_btn.setToolTip(
            "使用已保存的加载设置重新读取当前字幕源，并重新生成段落、页面和按行数布局。"
            "歌词角色、逐行特效、导唱符及手动显示时间会在能够可靠匹配时保留；"
            "手工调整的分页将被覆盖。"
        )
        self._refresh_source_btn.clicked.connect(
            lambda: self.sourceRefreshRequested.emit()
        )
        self._settings_source_btn = TransparentToolButton(FIF.SETTING, self._source_bar)
        self._settings_source_btn.setToolTip(
            "设置字幕源的分段和分页方式。加载设置独立于字体、颜色、位置等渲染样式；"
            "保存后会自动重新加载受影响的字幕源。"
        )
        self._settings_source_btn.clicked.connect(
            lambda: self.sourceSettingsRequested.emit(self._settings_source_btn)
        )
        for button in (
            self._add_source_btn,
            self._remove_source_btn,
            self._replace_source_btn,
            self._refresh_source_btn,
            self._settings_source_btn,
        ):
            button.setFixedSize(26, 26)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
        source_layout.addWidget(self._source_combo, 1)
        source_layout.addWidget(self._add_source_btn)
        source_layout.addWidget(self._remove_source_btn)
        source_layout.addWidget(self._replace_source_btn)
        source_layout.addWidget(self._refresh_source_btn)
        source_layout.addWidget(self._settings_source_btn)
        self._source_bar.setVisible(False)

        container = QWidget(self)
        content_layout = QVBoxLayout(container)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(2)
        content_layout.addWidget(self._source_bar)
        content_layout.addWidget(self._table, 1)
        self.set_content(container)

        # 代理编辑后通知宿主
        self._table.itemChanged.connect(self._on_item_changed)
        # 点击行 → 跳转预览
        self._table.cellClicked.connect(self._on_cell_clicked)
        self._table.cellDoubleClicked.connect(self._on_cell_double_clicked)

        # 各列语义下限之和向上传给 splitter，面板不再能被压到布局散架
        self._update_minimum_width()

    # ------------------------------------------------------------------ public

    def set_sources(
        self,
        names: list[str],
        active_index: int,
        removable_indices: Optional[set[int]] = None,
        replaceable_indices: Optional[set[int]] = None,
    ) -> None:
        """刷新字幕源下拉（首项 = 主字幕）；只有一个源时也显示，便于发现「＋」入口。

        ``replaceable_indices`` 是能换文件的源；「标题」这类没有歌词文件的伪源
        不在其中，宿主必须显式排除。
        """
        self._syncing_sources = True
        self._removable_source_indices = (
            set(removable_indices)
            if removable_indices is not None
            else set(range(1, len(names)))
        )
        self._replaceable_source_indices = (
            set(replaceable_indices)
            if replaceable_indices is not None
            else set(range(len(names)))
        )
        try:
            self._source_combo.clear()
            self._source_combo.addItems(list(names))
            if names:
                self._source_combo.setCurrentIndex(
                    max(0, min(int(active_index), len(names) - 1))
                )
        finally:
            self._syncing_sources = False
        self._source_bar.setVisible(bool(names))
        self._sync_source_buttons(self._source_combo.currentIndex())

    def current_source_index(self) -> int:
        return max(self._source_combo.currentIndex(), 0)

    def _sync_source_buttons(self, index: int) -> None:
        self._remove_source_btn.setEnabled(index in self._removable_source_indices)
        self._replace_source_btn.setEnabled(index in self._replaceable_source_indices)

    def _on_source_combo_changed(self, index: int) -> None:
        self._sync_source_buttons(index)
        if self._syncing_sources or index < 0:
            return
        self.sourceSelected.emit(index)

    def _on_remove_source_clicked(self) -> None:
        index = self._source_combo.currentIndex()
        if index in self._removable_source_indices:
            self.sourceRemoveRequested.emit(index)

    def _on_replace_source_clicked(self) -> None:
        index = self._source_combo.currentIndex()
        if index in self._replaceable_source_indices:
            self.sourceReplaceRequested.emit(index)

    def set_role_options(self, options: list[str]) -> None:
        """设置可选的配色方案 / 角色名列表。"""
        self._role_options = list(options)

    def set_style(self, style: Style) -> None:
        """样式（布局 / 配色方案）变化时刷新色点、对齐与双行分组。"""
        previous = self._style
        self._style = style
        if not self._populated:
            return
        # 字号、描边、发光这类只影响画面的字段占了属性面板绝大多数控件，
        # 但对本表毫无影响，整表刷新不能被它们带着跑。
        layout_changed = _layout_presentation_signature(
            previous
        ) != _layout_presentation_signature(style)
        swatch_changed = _swatch_presentation_signature(
            previous
        ) != _swatch_presentation_signature(style)
        if not layout_changed and not swatch_changed:
            return
        if layout_changed:
            # 行内容/列语义变了：立刻刷新，并把待处理的色点刷新一并做掉。
            self._swatch_refresh_timer.stop()
            self._swatch_refresh_pending = False
            self._refresh_presentation(update_widths=True)
            return
        # 只有色点变了：攒起来，停手后再刷一次。
        self._swatch_refresh_pending = True
        self._swatch_refresh_timer.start()

    def _flush_swatch_refresh(self) -> None:
        """停手后把攒下的色点刷新做掉。"""
        if not getattr(self, "_swatch_refresh_pending", False):
            return
        self._swatch_refresh_pending = False
        if self._populated:
            self._refresh_presentation(update_widths=False)

    def _layout_name_for_id(self, layout_id: Optional[str]) -> str:
        return layout_display_name(self._style, str(layout_id or "default"))

    def _build_presentation_rows(
        self, track: TimingTrack
    ) -> list[LyricsPresentationRow]:
        if track.page_plan is None:
            rows: list[LyricsPresentationRow] = []
            render_index = 0
            for track_index, line in enumerate(track.lines):
                if line.is_blank or not line.chars:
                    rows.append(
                        LyricsPresentationRow(
                            "source_blank", track_line_index=track_index
                        )
                    )
                else:
                    rows.append(
                        LyricsPresentationRow(
                            "lyric",
                            track_line_index=track_index,
                            render_line_index=render_index,
                        )
                    )
                    render_index += 1
            self._resolved_page_plan = None
            return rows

        resolved = resolve_page_plan(track, self._style)
        self._resolved_page_plan = resolved
        by_track = {item.track_line_index: item for item in resolved.lines}
        rows = []
        seen_sections: set[int] = set()
        seen_pages: set[int] = set()
        for track_index, _line in enumerate(track.lines):
            item = by_track.get(track_index)
            if item is None:
                # Once a page plan exists, section/page marker rows are the
                # sole structural presentation.  Source blanks remain in the
                # track for round-tripping but must not become operable table
                # rows or interfere with selection and page dragging.
                continue
            if item.section_index not in seen_sections:
                seen_sections.add(item.section_index)
                rows.append(
                    LyricsPresentationRow(
                        "section_marker",
                        section_index=item.section_index,
                    )
                )
            if item.global_page_index not in seen_pages:
                seen_pages.add(item.global_page_index)
                rows.append(
                    LyricsPresentationRow(
                        "page_marker",
                        section_index=item.section_index,
                        page_index=item.page_index_in_section,
                        global_page_index=item.global_page_index,
                        layout_id=item.layout_id,
                    )
                )
            rows.append(
                LyricsPresentationRow(
                    "lyric",
                    track_line_index=track_index,
                    render_line_index=item.render_line_index,
                    section_index=item.section_index,
                    page_index=item.page_index_in_section,
                    global_page_index=item.global_page_index,
                    lane=item.lane,
                    layout_id=item.layout_id,
                )
            )
        return rows

    def set_track(self, track: Optional[TimingTrack]) -> None:
        """加载 / 清空字幕。``None`` / 无行时回到空态。"""
        self._title_mode = False
        self._table.setHorizontalHeaderLabels(_COLUMN_HEADERS)
        self._table.setColumnHidden(COL_EFFECT, False)
        self._track = track
        self._table.blockSignals(True)
        try:
            self._table.clearSpans()
            self._table.setRowCount(0)
            self._row_meta = []
            self._presentation_rows = []
            self._resolved_page_plan = None
            if track is None or not track.lines:
                self.set_populated(False)
                return

            self._presentation_rows = self._build_presentation_rows(track)
            num_rows = len(self._presentation_rows)
            self._table.setRowCount(num_rows)
            for row, presentation in enumerate(self._presentation_rows):
                line = (
                    track.lines[presentation.track_line_index]
                    if presentation.track_line_index is not None
                    else None
                )
                lyric = presentation.kind == "lyric" and line is not None
                blank = not lyric
                self._row_meta.append(
                    (
                        blank,
                        (
                            int(presentation.render_line_index)
                            if presentation.render_line_index is not None
                            else -1
                        ),
                    )
                )

                lane_item = QTableWidgetItem("")
                lane_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                lane_item.setFlags(
                    Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
                    if presentation.kind
                    in {"lyric", "page_marker", "section_marker"}
                    else Qt.ItemFlag.NoItemFlags
                )
                self._table.setItem(row, COL_LANE, lane_item)

                role = _dominant_role(line) if line is not None else ""
                mixed = lyric and line is not None and _line_role_mixed(line)
                role_item = QTableWidgetItem(
                    "混合" if mixed else (role if role else _DEFAULT_ROLE_TEXT)
                )
                role_item.setData(Qt.ItemDataRole.UserRole, role)
                role_item.setFlags(
                    role_item.flags() & ~Qt.ItemFlag.ItemIsEditable
                )
                if blank:
                    role_item.setText("")
                    role_item.setFlags(Qt.ItemFlag.NoItemFlags)
                self._table.setItem(row, COL_ROLE, role_item)

                effect_item = QTableWidgetItem(
                    _animation_summary(self._style, line.animation_override)
                    if line is not None
                    else ""
                )
                effect_item.setFlags(effect_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                if blank:
                    effect_item.setFlags(Qt.ItemFlag.NoItemFlags)
                self._table.setItem(row, COL_EFFECT, effect_item)

                content_item = QTableWidgetItem(
                    _line_content_text(line) if lyric and line is not None else ""
                )
                icon_symbol = _line_guide_icon_symbol(line) if lyric else None
                if icon_symbol is not None:
                    content_item.setIcon(_guide_symbol_icon(icon_symbol))
                if not blank:
                    content_item.setToolTip(_line_guide_tooltip(line))
                content_item.setFlags(
                    content_item.flags() & ~Qt.ItemFlag.ItemIsEditable
                )
                if blank:
                    content_item.setFlags(Qt.ItemFlag.NoItemFlags)
                self._table.setItem(row, COL_CONTENT, content_item)
                layout_item = QTableWidgetItem(
                    self._layout_name_for_id(presentation.layout_id) if lyric else ""
                )
                layout_item.setTextAlignment(
                    Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignVCenter
                )
                layout_item.setFlags(
                    layout_item.flags() & ~Qt.ItemFlag.ItemIsEditable
                    if lyric
                    else Qt.ItemFlag.NoItemFlags
                )
                self._table.setItem(row, COL_LAYOUT, layout_item)

                if presentation.kind != "lyric":
                    self._table.setSpan(row, 0, 1, 5)
                    self._table.setRowHeight(row, _BLANK_ROW_HEIGHT)
                    if presentation.kind == "section_marker":
                        lane_item.setText(
                            f"♪♪♪ S{int(presentation.section_index or 0) + 1} ♪♪♪"
                        )
                    elif presentation.kind == "page_marker":
                        lane_item.setText(
                            "♪ "
                            f"S{int(presentation.section_index or 0) + 1} · "
                            f"P{int(presentation.page_index or 0) + 1} ♪"
                        )
                    else:
                        lane_item.setText("♪")
                else:
                    self._table.setRowHeight(row, _ROW_HEIGHT)
        finally:
            self._table.blockSignals(False)

        self.set_populated(True)
        self._refresh_presentation()
        self._apply_measured_column_widths()
        self._clamp_columns_to_viewport()

    def set_title(self, title: Optional[TitleOverlay]) -> None:
        """以无时间语义的行适配器显示标题，复用角色与逐字符编辑 UI。"""
        if title is None:
            self.set_track(None)
            return
        labels = normalize_title_char_role_labels(
            title.text_template, title.char_role_labels
        )
        lines = [
            TimingLine(
                chars=[
                    TimingChar(text=char, start_ms=0, role_label=labels[row][index])
                    for index, char in enumerate(text)
                ],
                end_ms=0,
                layout_index=int(title.layout_index or 0),
            )
            for row, text in enumerate(title.text_template.split("\n"))
        ]
        self.set_track(TimingTrack(lines=lines))
        self._title_mode = True
        self._table.setHorizontalHeaderLabels(["行", "角色", "", "内容", "布局"])
        self._refresh_presentation()
        self._apply_measured_column_widths()

    def refresh_row_role(self, row: int) -> None:
        """宿主改完 role_label 后刷新该行的色点（角色文本已由委托更新）。"""
        display_row = self._display_row_for_track_line(row)
        if display_row is not None:
            self._refresh_presentation(rows=[display_row])

    def refresh_layout_assignments(self) -> None:
        """行的布局变了之后刷新「布局」列。

        布局是写在 **track**（``line.layout_index``）上的，不是 ``Style``。宿主原先
        靠 ``set_style(self._style)`` 顺带触发重绘，但 ``set_style`` 后来加了"样式
        签名没变就直接返回"的优化 —— 样式本来就没变，于是这一列一直停在旧值：
        右键菜单里勾的已经是新布局、预览也按新布局渲染，只有表格还写着旧的。
        """
        if not self._populated:
            return
        self._refresh_presentation(update_widths=True)

    def refresh_row_effect(self, row: int) -> None:
        """逐行动画覆盖变化后刷新该行摘要。"""
        display_row = self._display_row_for_track_line(row)
        if display_row is not None:
            self._refresh_presentation(rows=[display_row])

    @property
    def list_widget(self):
        """向后兼容。"""
        return self._table

    @property
    def table_widget(self) -> FluentTableWidget:
        return self._table

    def select_row(self, row: int) -> bool:
        """Select and reveal one lyrics row without emitting a click action."""
        display_row = self._display_row_for_track_line(int(row))
        if display_row is None:
            return False
        row = display_row
        item = self._table.item(row, COL_CONTENT) or self._table.item(row, COL_LANE)
        if item is None:
            return False
        self._table.clearSelection()
        self._table.selectRow(row)
        self._table.setCurrentItem(item)
        self._table.scrollToItem(
            item,
            QAbstractItemView.ScrollHint.PositionAtCenter,
        )
        return True

    def _display_row_for_track_line(self, track_line_index: int) -> Optional[int]:
        return next(
            (
                row
                for row, item in enumerate(self._presentation_rows)
                if item.kind == "lyric"
                and item.track_line_index == int(track_line_index)
            ),
            None,
        )

    def _track_rows_for_presentation_row(self, row: int) -> list[int]:
        if not 0 <= row < len(self._presentation_rows):
            return []
        item = self._presentation_rows[row]
        if item.kind == "lyric" and item.track_line_index is not None:
            return [item.track_line_index]
        resolved = self._resolved_page_plan
        if resolved is None:
            return []
        if item.kind == "page_marker" and item.global_page_index is not None:
            if 0 <= item.global_page_index < len(resolved.pages):
                return list(
                    resolved.pages[item.global_page_index].track_line_indices
                )
        if item.kind == "section_marker" and item.section_index is not None:
            if 0 <= item.section_index < len(resolved.sections):
                return [
                    track_index
                    for page in resolved.sections[item.section_index].pages
                    for track_index in page.track_line_indices
                ]
        return []

    # ------------------------------------------------------------------ private

    def _visible_rows(self) -> range:
        """Return rows intersecting the viewport, falling back to all rows.

        Qt delegate size hints include the active font, DPI, item padding and icons,
        so measuring only visible rows keeps widths responsive without scanning a
        large subtitle project or letting one distant outlier dominate the layout.
        """
        count = self._table.rowCount()
        if count <= 0:
            return range(0)
        viewport = self._table.viewport()
        first = self._table.rowAt(0)
        last = self._table.rowAt(max(viewport.height() - 1, 0))
        if first < 0:
            return range(count)
        if last < first:
            last = count - 1
        return range(first, min(last + 1, count))

    def _header_width_hint(self, column: int) -> int:
        return max(self._table.horizontalHeader().sectionSizeHint(column), 1)

    def _cell_width_hint(self, row: int, column: int) -> int:
        index = self._table.model().index(row, column)
        hint = self._table.sizeHintForIndex(index)
        return max(hint.width(), 0)

    def _visible_column_width_hint(self, column: int) -> int:
        return max(
            [self._header_width_hint(column)]
            + [self._cell_width_hint(row, column) for row in self._visible_rows()]
        )

    def _role_minimum_width(self) -> int:
        """Compact role width: header plus the color swatch affordance.

        Long role names may elide, but the column must retain room for its localized
        header and role color icon. Both values come from the live Qt style/DPI.
        """
        return max(
            self._header_width_hint(COL_ROLE),
            self._table.iconSize().width()
            + 2 * self._table.style().pixelMetric(QStyle.PixelMetric.PM_FocusFrameHMargin)
            + self._table.fontMetrics().horizontalAdvance(_DEFAULT_ROLE_TEXT),
        )

    def _effect_minimum_width(self) -> int:
        """Measure the most common visible effect summary as the semantic minimum."""
        candidates: list[tuple[str, int]] = []
        for row in self._visible_rows():
            item = self._table.item(row, COL_EFFECT)
            if item is not None and item.text():
                candidates.append((item.text(), row))
        if not candidates:
            return self._header_width_hint(COL_EFFECT)
        typical = Counter(text for text, _row in candidates).most_common(1)[0][0]
        typical_width = max(
            self._cell_width_hint(row, COL_EFFECT)
            for text, row in candidates
            if text == typical
        )
        return max(self._header_width_hint(COL_EFFECT), typical_width)

    def _content_minimum_width(self) -> int:
        """内容列语义下限：表头 + 至少能看清几个 CJK 字，宽度随当前字体缩放。"""
        return max(
            self._header_width_hint(COL_CONTENT),
            self._table.fontMetrics().horizontalAdvance("中") * 6,
        )

    def _column_minimum_width(self, column: int) -> int:
        if column == COL_ROLE:
            return self._role_minimum_width()
        if column == COL_EFFECT:
            return self._effect_minimum_width()
        if column == COL_LANE:
            return self._visible_column_width_hint(COL_LANE)
        if column == COL_LAYOUT:
            return max(
                self._header_width_hint(COL_LAYOUT),
                self._table.fontMetrics().horizontalAdvance("2 行布局（默认）") + 20,
            )
        return self._content_minimum_width()

    def _column_maximum_width(self, column: int) -> int:
        """交互列上限：拖宽到内容列只剩语义下限为止，表格总宽永不超 viewport。"""
        available = self._table.viewport().width()
        if available <= 0:
            return 16_777_215  # 布局未落位时不设限（QWIDGETSIZE_MAX）
        header = self._table.horizontalHeader()
        lane = 0 if header.isSectionHidden(COL_LANE) else header.sectionSize(COL_LANE)
        if column == COL_LAYOUT:
            maximum = (
                available
                - lane
                - header.sectionSize(COL_ROLE)
                - header.sectionSize(COL_EFFECT)
                - self._content_minimum_width()
            )
            return max(maximum, self._column_minimum_width(column))
        other = COL_EFFECT if column == COL_ROLE else COL_ROLE
        maximum = (
            available
            - lane
            - header.sectionSize(COL_LAYOUT)
            - header.sectionSize(other)
            - self._content_minimum_width()
        )
        return max(maximum, self._column_minimum_width(column))

    def _update_minimum_width(self) -> int:
        """把"各列语义下限 + 竖直滚动条"设为表格最小宽，经布局传给 splitter。"""
        header = self._table.horizontalHeader()
        lane = (
            0
            if header.isSectionHidden(COL_LANE)
            else self._visible_column_width_hint(COL_LANE)
        )
        minimum = (
            lane
            + self._role_minimum_width()
            + self._effect_minimum_width()
            + self._column_minimum_width(COL_LAYOUT)
            + self._content_minimum_width()
            + self._table.verticalScrollBar().sizeHint().width()
            + 2 * self._table.frameWidth()
        )
        self._table.setMinimumWidth(minimum)
        return minimum

    def _apply_measured_column_widths(self) -> None:
        """Choose initial fixed-column widths from live content and viewport space.

        Role/effect ideals come from visible delegate size hints. If both ideals do
        not fit, only their space above independently measured semantic minima is
        compressed; the content column keeps the remainder through Stretch mode.
        When even minima cannot fit, QTableView's own horizontal scrollbar handles
        the shortage while its viewport clips painting to the lyrics panel.
        """
        if self._table.rowCount() <= 0:
            return
        header = self._table.horizontalHeader()
        available = self._table.viewport().width()
        if available <= 0:
            return

        role_min = self._role_minimum_width()
        effect_min = self._effect_minimum_width()
        role_ideal = max(role_min, self._visible_column_width_hint(COL_ROLE))
        effect_ideal = max(effect_min, self._visible_column_width_hint(COL_EFFECT))
        lane_width = 0 if header.isSectionHidden(COL_LANE) else header.sectionSize(COL_LANE)
        layout_width = max(
            self._column_minimum_width(COL_LAYOUT),
            self._visible_column_width_hint(COL_LAYOUT),
        )
        content_min = self._column_minimum_width(COL_CONTENT)
        budget = max(available - lane_width - layout_width - content_min, 0)

        role_width, effect_width = role_ideal, effect_ideal
        minimum_total = role_min + effect_min
        ideal_total = role_ideal + effect_ideal
        if minimum_total < ideal_total and budget < ideal_total:
            distributable = max(budget - minimum_total, 0)
            extra_total = ideal_total - minimum_total
            role_width = role_min + round(
                distributable * (role_ideal - role_min) / extra_total
            )
            effect_width = effect_min + round(
                distributable * (effect_ideal - effect_min) / extra_total
            )

        self._setting_column_widths = True
        try:
            if COL_ROLE not in self._user_sized_columns:
                header.resizeSection(COL_ROLE, role_width)
            if COL_EFFECT not in self._user_sized_columns:
                header.resizeSection(COL_EFFECT, effect_width)
            if COL_LAYOUT not in self._user_sized_columns:
                header.resizeSection(COL_LAYOUT, layout_width)
        finally:
            self._setting_column_widths = False

    def _on_column_resized(self, column: int, _old: int, new: int) -> None:
        """交互列双向钳制：下限是语义最小宽，上限是 viewport 预算。

        上限保证拖宽角色 / 特效时内容列最多缩到自己的语义下限就顶住，
        表格总宽从不超过 viewport——内容列不可能被推到右侧属性面板后面。
        """
        if self._setting_column_widths or column not in (
            COL_ROLE,
            COL_EFFECT,
            COL_LAYOUT,
        ):
            return
        self._user_sized_columns.add(column)
        clamped = min(
            max(new, self._column_minimum_width(column)),
            self._column_maximum_width(column),
        )
        if clamped == new:
            return
        self._setting_column_widths = True
        try:
            self._table.horizontalHeader().resizeSection(column, clamped)
        finally:
            self._setting_column_widths = False

    def _clamp_columns_to_viewport(self) -> None:
        """面板变窄后按"先压特效、再压角色"回收超预算宽度，保住内容列下限。"""
        if self._table.rowCount() <= 0:
            return
        available = self._table.viewport().width()
        if available <= 0:
            return
        header = self._table.horizontalHeader()
        lane = 0 if header.isSectionHidden(COL_LANE) else header.sectionSize(COL_LANE)
        layout = header.sectionSize(COL_LAYOUT)
        budget = available - lane - layout - self._content_minimum_width()
        overflow = (
            header.sectionSize(COL_ROLE) + header.sectionSize(COL_EFFECT) - budget
        )
        for column in (COL_EFFECT, COL_ROLE):
            if overflow <= 0:
                return
            width = header.sectionSize(column)
            take = min(overflow, max(width - self._column_minimum_width(column), 0))
            if take <= 0:
                continue
            self._setting_column_widths = True
            try:
                header.resizeSection(column, width - take)
            finally:
                self._setting_column_widths = False
            overflow -= take

    def eventFilter(self, obj, event):  # noqa: N802 - Qt API
        if obj is self._table.viewport() and event.type() == QEvent.Type.Resize:
            # 自动列跟随新宽度重测；用户手调列不重测，但重新钳制，
            # 变宽时把多出的空间自然还给 Stretch 的内容列。
            self._apply_measured_column_widths()
            self._clamp_columns_to_viewport()
        elif obj is self._table.viewport():
            if (
                event.type() == QEvent.Type.MouseButtonPress
                and event.button() == Qt.MouseButton.LeftButton
            ):
                point = event.position().toPoint()
                row = self._table.rowAt(point.y())
                if self._consume_press_for_batch_picker(point, event.modifiers()):
                    return True
                self._page_drag_row = (
                    row
                    if 0 <= row < len(self._presentation_rows)
                    and self._presentation_rows[row].kind == "lyric"
                    and self._resolved_page_plan is not None
                    and not self._title_mode
                    else None
                )
                self._page_drag_start = point
                self._page_drag_active = False
            elif (
                event.type() == QEvent.Type.MouseMove
                and self._page_drag_row is not None
                and event.buttons() & Qt.MouseButton.LeftButton
            ):
                if (
                    event.position().toPoint() - self._page_drag_start
                ).manhattanLength() >= QApplication.startDragDistance():
                    self._page_drag_active = True
                    self._table.viewport().setCursor(
                        Qt.CursorShape.ClosedHandCursor
                    )
                if self._page_drag_active:
                    self._update_page_drag_preview(
                        self._table.rowAt(event.position().toPoint().y()),
                        event.globalPosition().toPoint(),
                    )
                    return True
            elif (
                event.type() == QEvent.Type.MouseButtonRelease
                and event.button() == Qt.MouseButton.LeftButton
            ):
                source_row = self._page_drag_row
                was_dragging = self._page_drag_active
                self._page_drag_row = None
                self._page_drag_active = False
                self._table.viewport().unsetCursor()
                self._page_drag_indicator.hide()
                if was_dragging and source_row is not None:
                    target_row = self._table.rowAt(event.position().toPoint().y())
                    move = self._page_boundary_move_for_drag(
                        source_row, target_row
                    )
                    if move is not None:
                        self.pageMoveRequested.emit(*move)
                    else:
                        show_fluent_tooltip(
                            "拖动只调整分页：请将一页的首行拖入上一页，"
                            "或将一页的末行拖入下一页。",
                            parent=self._table,
                            global_pos=event.globalPosition().toPoint(),
                        )
                    return True
        elif obj is self._table.horizontalHeader().viewport():
            if self._handle_header_boundary_drag(event):
                return True
        return super().eventFilter(obj, event)

    def _layout_handle_hit(self, x: float) -> bool:
        """x 是否落在 内容|布局 边界的把手区内（不窄于 Qt 原生把手宽）。"""
        header = self._table.horizontalHeader()
        if header.isSectionHidden(COL_LAYOUT):
            return False
        boundary = (
            header.sectionViewportPosition(COL_CONTENT)
            + header.sectionSize(COL_CONTENT)
        )
        grip = max(
            int(
                header.style().pixelMetric(
                    QStyle.PixelMetric.PM_HeaderGripMargin, None, header
                )
            ),
            4,
        )
        return boundary - grip <= x <= boundary + grip

    def _handle_header_boundary_drag(self, event) -> bool:
        """接管 内容|布局 边界把手的按下 / 拖动 / 松开与悬停光标。

        QHeaderView 的 ``sectionHandleAt`` 非虚函数，无法覆写映射；在这里对
        表头 viewport 过滤事件：把手区内的左键按下改由面板自己拖，宽度直接
        写到布局列（Interactive），内容列作为 Stretch 自动吸收差值。
        """
        header = self._table.horizontalHeader()
        if (
            event.type() == QEvent.Type.MouseButtonPress
            and event.button() == Qt.MouseButton.LeftButton
        ):
            x = int(event.position().x())
            if self._layout_handle_hit(x):
                self._header_drag = (x, header.sectionSize(COL_LAYOUT))
                self._user_sized_columns.add(COL_LAYOUT)
                return True
        elif event.type() == QEvent.Type.MouseMove:
            if self._header_drag is not None:
                if not event.buttons() & Qt.MouseButton.LeftButton:
                    self._header_drag = None
                    return True
                start_x, original = self._header_drag
                # 边界右移 = 压窄布局列，左移 = 放宽；与"把手属于左列"的
                # 直觉一致：拖的是布局列的左缘。
                desired = original - (int(event.position().x()) - start_x)
                header.resizeSection(
                    COL_LAYOUT,
                    min(
                        max(desired, self._column_minimum_width(COL_LAYOUT)),
                        self._column_maximum_width(COL_LAYOUT),
                    ),
                )
                return True
            if self._layout_handle_hit(event.position().x()):
                header.viewport().setCursor(Qt.CursorShape.SplitHCursor)
            else:
                header.viewport().unsetCursor()
        elif (
            event.type() == QEvent.Type.MouseButtonRelease
            and self._header_drag is not None
        ):
            self._header_drag = None
            return True
        elif event.type() == QEvent.Type.Leave:
            header.viewport().unsetCursor()
        return False

    def _update_page_drag_preview(self, target_row: int, global_pos: QPoint) -> None:
        source_row = self._page_drag_row
        move = (
            self._page_boundary_move_for_drag(source_row, target_row)
            if source_row is not None
            else None
        )
        if move is None or self._resolved_page_plan is None:
            self._page_drag_indicator.hide()
            show_fluent_tooltip(
                "此处不能调整分页",
                parent=self._table,
                global_pos=global_pos,
            )
            return
        section_index, page_index, direction = move
        page = self._resolved_page_plan.sections[section_index].pages[page_index]
        item = self._table.item(target_row, COL_CONTENT)
        if item is None:
            self._page_drag_indicator.hide()
            return
        rect = self._table.visualItemRect(item)
        y = rect.bottom() - 1 if direction > 0 else rect.top() - 1
        self._page_drag_indicator.setGeometry(
            0,
            max(y, 0),
            self._table.viewport().width(),
            3,
        )
        self._page_drag_indicator.show()
        self._page_drag_indicator.raise_()
        show_fluent_tooltip(
            f"目标页将变为 {page.line_count + direction} 行",
            parent=self._table,
            global_pos=global_pos,
        )

    def _page_boundary_move_for_drag(
        self, source_row: int, target_row: int
    ) -> Optional[tuple[int, int, int]]:
        """Translate a boundary-line drag into one deterministic page move."""

        resolved = self._resolved_page_plan
        if (
            resolved is None
            or not 0 <= source_row < len(self._presentation_rows)
            or not 0 <= target_row < len(self._presentation_rows)
        ):
            return None
        source_item = self._presentation_rows[source_row]
        if source_item.track_line_index is None:
            return None
        source = resolved.line_for_track_index(source_item.track_line_index)
        target_rows = self._track_rows_for_presentation_row(target_row)
        target = (
            resolved.line_for_track_index(target_rows[0])
            if target_rows
            else None
        )
        if source is None or target is None:
            return None
        page = resolved.pages[source.global_page_index]
        if (
            page.track_line_indices
            and source.track_line_index == page.track_line_indices[0]
            and source.page_index_in_section > 0
            and target.page_index_in_section == source.page_index_in_section - 1
        ):
            return source.section_index, target.page_index_in_section, 1
        if (
            page.track_line_indices
            and source.track_line_index == page.track_line_indices[0]
            and source.page_index_in_section == 0
            and source.section_index > 0
            and target.section_index == source.section_index - 1
            and target.page_index_in_section
            == len(resolved.sections[target.section_index].pages) - 1
        ):
            return target.section_index, target.page_index_in_section, 1
        if (
            page.track_line_indices
            and source.track_line_index == page.track_line_indices[-1]
            and source.section_index == target.section_index
            and target.page_index_in_section == source.page_index_in_section + 1
        ):
            return source.section_index, source.page_index_in_section, -1
        if (
            page.track_line_indices
            and source.track_line_index == page.track_line_indices[-1]
            and source.page_index_in_section
            == len(resolved.sections[source.section_index].pages) - 1
            and source.section_index + 1 < len(resolved.sections)
            and target.section_index == source.section_index + 1
            and target.page_index_in_section == 0
        ):
            return source.section_index, source.page_index_in_section, -1
        return None

    def changeEvent(self, event: QEvent) -> None:  # noqa: N802 - Qt API
        super().changeEvent(event)
        if not hasattr(self, "_table") or not hasattr(self, "_setting_column_widths"):
            return
        if event.type() in (
            QEvent.Type.FontChange,
            QEvent.Type.ApplicationFontChange,
            QEvent.Type.StyleChange,
        ):
            # Re-measure with the new font/style. User-selected widths remain unless
            # the new semantic minimum requires clamping.
            self._update_minimum_width()
            self._apply_measured_column_widths()
            header = self._table.horizontalHeader()
            for column in (COL_ROLE, COL_EFFECT, COL_LAYOUT):
                minimum = self._column_minimum_width(column)
                if header.sectionSize(column) < minimum:
                    self._setting_column_widths = True
                    try:
                        header.resizeSection(column, minimum)
                    finally:
                        self._setting_column_widths = False
            self._clamp_columns_to_viewport()

    def _lane_count(self) -> int:
        """多行显示的默认行数（与渲染端 lane 分配一致）。"""
        if not self._style.dual_line_layout:
            return 1
        return max(len(self._style.line_alignments), 1)

    def _recompute_render_lanes(self) -> None:
        """按当前布局分配（页首行布局定行数）算出每个可渲染行的 lane / 页序号。"""
        self._render_lanes = []
        self._render_groups = []
        if self._track is None:
            return
        if self._track.page_plan is not None:
            resolved = resolve_page_plan(self._track, self._style)
            self._resolved_page_plan = resolved
            self._render_lanes = [0] * len(resolved.lines)
            self._render_groups = [0] * len(resolved.lines)
            for item in resolved.lines:
                self._render_lanes[item.render_line_index] = item.lane
                self._render_groups[item.render_line_index] = item.global_page_index
            return
        render_lines = [
            line for line in self._track.lines if not line.is_blank and line.chars
        ]
        row_count_of = None
        if self._style.dual_line_layout and self._style.layouts:
            style = self._style
            row_count_of = lambda line: max(  # noqa: E731
                len(_effective_layout_style(style, line).line_alignments), 1
            )
        lanes, page_starts, _page_rows = assign_lanes(
            render_lines,
            self._lane_count(),
            row_count_of,
            section_gap_ms=self._style.section_gap_ms,
        )
        self._render_lanes = lanes
        ordinal = -1
        seen_start: Optional[int] = None
        for start in page_starts:
            if start != seen_start:
                ordinal += 1
                seen_start = start
            self._render_groups.append(ordinal)

    def _group_bg_for_row(self, row: int) -> Optional[QColor]:
        """行组斑马纹：奇数页一层主题色薄底，同页行"贴在一起"。"""
        if not self._style.dual_line_layout:
            return None
        if row < 0 or row >= len(self._row_meta):
            return None
        presentation = self._presentation_rows[row]
        group = (
            int(presentation.global_page_index)
            if presentation.global_page_index is not None
            else -1
        )
        if presentation.kind != "lyric" or group % 2 != 1:
            return None
        color = QColor(palette().accent_primary)
        color.setAlpha(38 if getattr(palette(), "is_dark", False) else 24)
        return color

    def _refresh_presentation(
        self, rows: Optional[list[int]] = None, *, update_widths: bool = True
    ) -> None:
        """按当前 Style 刷新：轨标 / 内容对齐 / 角色色点 / 单行页居中。"""
        style = self._style
        dual = bool(style.dual_line_layout)
        lane_color = QColor(palette().text_hint)

        self._table.setColumnHidden(COL_LANE, False if self._title_mode else not dual)
        self._table.setColumnHidden(COL_EFFECT, self._title_mode)
        self._recompute_render_lanes()
        page_sizes = Counter(self._render_groups)

        self._table.blockSignals(True)
        try:
            target_rows = rows if rows is not None else range(self._table.rowCount())
            for row in target_rows:
                if row < 0 or row >= len(self._row_meta):
                    continue
                blank, render_index = self._row_meta[row]
                lane = (
                    self._render_lanes[render_index]
                    if 0 <= render_index < len(self._render_lanes)
                    else 0
                )
                lane_item = self._table.item(row, COL_LANE)
                role_item = self._table.item(row, COL_ROLE)
                content_item = self._table.item(row, COL_CONTENT)
                effect_item = self._table.item(row, COL_EFFECT)
                layout_item = self._table.item(row, COL_LAYOUT)
                if (
                    lane_item is None
                    or role_item is None
                    or effect_item is None
                    or content_item is None
                    or layout_item is None
                ):
                    continue
                if blank:
                    continue

                presentation = self._presentation_rows[row]
                track_index = presentation.track_line_index
                line = (
                    self._track.lines[track_index]
                    if self._track is not None
                    and track_index is not None
                    and 0 <= track_index < len(self._track.lines)
                    else None
                )
                line_style = (
                    _effective_layout_style(style, line) if line is not None else style
                )
                lane_item.setText(
                    str((track_index or 0) + 1)
                    if self._title_mode
                    else (f"T{lane + 1}" if dual else "")
                )
                layout_ref = int(getattr(line, "layout_index", 0) or 0) if line else 0
                layout_name = (
                    style.layouts[layout_ref - 1].name
                    if 1 <= layout_ref <= len(style.layouts)
                    else layout_display_name(style, "default")
                )
                lane_item.setToolTip(f"布局：{layout_name}")
                layout_item.setText(layout_name)
                layout_item.setToolTip(
                    f"当前页面使用“{layout_item.text()}”。单击可选择同容量或更大容量的布局。"
                )
                lane_item.setForeground(QBrush(lane_color))
                lane_font = lane_item.font()
                lane_font.setPointSizeF(8.0)
                lane_item.setFont(lane_font)
                single_line_page = (
                    0 <= render_index < len(self._render_groups)
                    and page_sizes[self._render_groups[render_index]] == 1
                )
                content_item.setTextAlignment(
                    (Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
                    if self._title_mode
                    else self._content_alignment(line_style, lane, dual, single_line_page)
                )
                if line is not None:
                    content_item.setText(_line_content_text(line))
                icon_symbol = _line_guide_icon_symbol(line) if line is not None else None
                if icon_symbol is not None:
                    content_item.setIcon(_guide_symbol_icon(icon_symbol))
                else:
                    content_item.setIcon(QIcon())
                content_item.setToolTip(
                    _line_guide_tooltip(line) if line is not None else ""
                )

                # ``role_label`` is the source of truth.  Whole-row/batch edits
                # mutate the TimingTrack in place, so keeping the old item
                # UserRole here makes the table continue displaying the role
                # that was present when set_track() first built the row.
                role = (
                    _dominant_role(line)
                    if line is not None
                    else str(role_item.data(Qt.ItemDataRole.UserRole) or "")
                )
                line_role_mixed = line is not None and _line_role_mixed(line)
                if line is not None:
                    # A mixed row has no single selected value.  Keeping its
                    # dominant role as the editor value would make choosing
                    # that same role a no-op instead of an explicit whole-row
                    # overwrite.
                    role_item.setData(
                        Qt.ItemDataRole.UserRole,
                        "" if line_role_mixed else role,
                    )
                if line_role_mixed:
                    # 行内混合角色：显示「混合」+ 双色点（前两种代表色）
                    seen: list[str] = []
                    for label in _line_role_labels(line):
                        if label not in seen:
                            seen.append(label)
                    role_item.setText("混合")
                    role_item.setIcon(
                        _mixed_swatch_icon(
                            [
                                _scheme_swatch_color(
                                    style,
                                    label or (TITLE_SCHEME_NAME if self._title_mode else ""),
                                )
                                for label in seen[:2]
                            ]
                        )
                    )
                    role_item.setToolTip(
                        "该行包含逐字符角色分配（双击内容列或右键「逐字符分配角色…」编辑）。"
                    )
                else:
                    if line is not None:
                        role_item.setText(
                            role
                            if role
                            else ("标题默认" if self._title_mode else _DEFAULT_ROLE_TEXT)
                        )
                        role_item.setToolTip("")
                    role_item.setIcon(
                        _swatch_icon(
                            _scheme_swatch_color(
                                style,
                                role or (TITLE_SCHEME_NAME if self._title_mode else ""),
                            )
                        )
                    )
                if line is not None and not self._title_mode:
                    effect_item.setText(_animation_summary(style, line.animation_override))
                    if line.animation_override is None:
                        effect_item.setIcon(QIcon())
                        effect_item.setForeground(QBrush(QColor(palette().text_hint)))
                    else:
                        effect_item.setIcon(_swatch_icon(QColor(palette().accent_primary)))
                        effect_item.setForeground(QBrush(QColor(palette().accent_primary)))
                    effect_item.setToolTip(
                        "双击编辑该行特效；右键可批量设置选中行。"
                    )
        finally:
            self._table.blockSignals(False)
        # 轨列显隐 / 特效摘要变化都会改变各列语义下限 → 重算表格最小宽。
        # 纯改色不会动任何列宽，跳过这一步（它会逐单元格量尺寸，很贵）。
        if update_widths:
            self._update_minimum_width()
        # 组底色由委托绘制，Style 变化后要触发一次重绘
        self._table.viewport().update()

    @staticmethod
    def _content_alignment(
        style: Style, lane: int, dual: bool, single_line_page: bool = False
    ) -> Qt.AlignmentFlag:
        vertical = Qt.AlignmentFlag.AlignVCenter
        if not dual:
            return Qt.AlignmentFlag.AlignLeft | vertical
        layout = style.line_horizontal_layout
        if layout == "center":
            return Qt.AlignmentFlag.AlignHCenter | vertical
        if layout == "per_row":
            align = style.row1_align if lane == 0 else style.row2_align
            mapping = {
                "left": Qt.AlignmentFlag.AlignLeft,
                "center": Qt.AlignmentFlag.AlignHCenter,
                "right": Qt.AlignmentFlag.AlignRight,
            }
            return mapping.get(align, Qt.AlignmentFlag.AlignLeft) | vertical
        # asymmetric（默认）：按每行对齐列表；仅单行页居中（与 N3
        # SmartHorizonOnePage 的 topLineIndex == bottomLineIndex 条件一致）。
        if single_line_page and style.smart_horizontal != "none":
            return Qt.AlignmentFlag.AlignHCenter | vertical
        alignments = style.line_alignments or ["left"]
        align = alignments[min(max(lane, 0), len(alignments) - 1)]
        mapping = {
            "left": Qt.AlignmentFlag.AlignLeft,
            "center": Qt.AlignmentFlag.AlignHCenter,
            "right": Qt.AlignmentFlag.AlignRight,
        }
        return mapping.get(align, Qt.AlignmentFlag.AlignLeft) | vertical

    def _show_context_menu(self, pos: QPoint) -> None:
        """右键菜单：把布局应用到选中行（宿主按页联动扩散到同页行）。"""
        if self._track is None:
            return
        display_rows = sorted({item.row() for item in self._table.selectedItems()})
        clicked = self._table.rowAt(pos.y())
        if clicked >= 0 and clicked not in display_rows:
            display_rows = [clicked]
        rows = list(
            dict.fromkeys(
                track_row
                for display_row in display_rows
                for track_row in self._track_rows_for_presentation_row(display_row)
            )
        )
        if not rows:
            return
        menu = _StableRoundMenu(parent=self._table)
        if len(rows) == 1:
            char_role_action = Action("逐字符分配角色…", menu)
            char_role_action.triggered.connect(
                lambda _checked=False, r=rows[0]: self._edit_char_roles(r)
            )
            menu.addAction(char_role_action)
            menu.addSeparator()
        if not self._title_mode:
            guide_action = Action("导入 SVG 导唱符…", menu)
            guide_action.triggered.connect(
                lambda _checked=False, rs=list(rows): self.guideSymbolImportRequested.emit(rs)
            )
            menu.addAction(guide_action)
            replace_prefix_action = Action("批量识别导唱标记…", menu)
            replace_prefix_action.triggered.connect(
                lambda _checked=False: self.guidePrefixReplaceRequested.emit()
            )
            menu.addAction(replace_prefix_action)
            if any(self._track.lines[row].guide_symbol is not None for row in rows):
                remove_guide_action = Action("移除所选行导唱符", menu)
                remove_guide_action.triggered.connect(
                    lambda _checked=False, rs=list(rows): self.guideSymbolRemoveRequested.emit(rs)
                )
                menu.addAction(remove_guide_action)
            menu.addSeparator()
            anchor_row = rows[0]
            insert_page = Action("在此行前插入分页", menu)
            insert_page.triggered.connect(
                lambda _checked=False, r=anchor_row: self.pageBoundaryRequested.emit(
                    "insert_page", r
                )
            )
            menu.addAction(insert_page)
            insert_section = Action("在此行前插入分段", menu)
            insert_section.triggered.connect(
                lambda _checked=False, r=anchor_row: self.pageBoundaryRequested.emit(
                    "insert_section", r
                )
            )
            menu.addAction(insert_section)
            if self._resolved_page_plan is not None:
                resolved_line = self._resolved_page_plan.line_for_track_index(anchor_row)
                if resolved_line is not None and resolved_line.render_line_index > 0:
                    if resolved_line.page_index_in_section > 0:
                        delete_page = Action("删除此分页", menu)
                        delete_page.triggered.connect(
                            lambda _checked=False, r=anchor_row: (
                                self.pageBoundaryRequested.emit("delete_page", r)
                            )
                        )
                        menu.addAction(delete_page)
                    elif resolved_line.section_index > 0:
                        delete_section = Action("删除此分段", menu)
                        delete_section.triggered.connect(
                            lambda _checked=False, r=anchor_row: (
                                self.pageBoundaryRequested.emit("delete_section", r)
                            )
                        )
                        menu.addAction(delete_section)
            menu.addSeparator()
        role_menu = _StableRoundMenu("应用角色方案", menu)
        current_roles = {
            _dominant_role(self._track.lines[row]) for row in rows
        }
        has_mixed_rows = any(_line_role_mixed(self._track.lines[row]) for row in rows)
        default_role_text = "标题默认" if self._title_mode else _DEFAULT_ROLE_TEXT
        role_choices = [(default_role_text, "")] + [
            (name, name) for name in self._role_options
        ]
        for display, name in role_choices:
            action = Action(display, role_menu)
            action.setCheckable(True)
            action.setChecked(not has_mixed_rows and current_roles == {name})
            action.triggered.connect(
                lambda _checked=False, rs=list(rows), value=name: (
                    self._request_role_change(rs, value)
                )
            )
            role_menu.addAction(action)
        menu.addMenu(role_menu)
        if not self._title_mode:
            auto_chorus_action = Action("自动识别和声…", menu)
            auto_chorus_action.setToolTip("按括号把和声部分整源分配到一个角色方案")
            auto_chorus_action.triggered.connect(
                lambda _checked=False: self.autoChorusRequested.emit()
            )
            menu.addAction(auto_chorus_action)
        menu.addSeparator()
        if not self._title_mode:
            effect_action = Action("设置所选行特效…", menu)
            effect_action.triggered.connect(
                lambda _checked=False, rs=list(rows): self._edit_animation_rows(rs)
            )
            menu.addAction(effect_action)
            reset_action = Action("所选行恢复全局特效", menu)
            reset_action.triggered.connect(
                lambda _checked=False, rs=list(rows): self.animationOverrideRequested.emit(rs, None)
            )
            menu.addAction(reset_action)
            menu.addSeparator()
        layout_menu = _StableRoundMenu("应用布局", menu)
        current_indices = {
            int(getattr(self._track.lines[row], "layout_index", 0) or 0)
            for row in rows
            if row < len(self._track.lines)
        }
        names = [layout_display_name(self._style, "default")] + [
            layout.name for layout in self._style.layouts
        ]
        selected_page_counts: list[int] = []
        selected_page_indices: set[int] = set()
        if self._resolved_page_plan is not None:
            selected_page_indices = {
                line.global_page_index
                for row in rows
                for line in [self._resolved_page_plan.line_for_track_index(row)]
                if line is not None
            }
            selected_page_counts = [
                self._resolved_page_plan.pages[index].line_count
                for index in selected_page_indices
            ]
        selected_layouts = (
            {
                self._resolved_page_plan.pages[index].layout_id
                for index in selected_page_indices
            }
            if self._resolved_page_plan is not None
            else set()
        )
        if len(selected_layouts) > 1:
            mixed = Action("当前：多个布局", layout_menu)
            mixed.setEnabled(False)
            layout_menu.addAction(mixed)
            layout_menu.addSeparator()
        max_page_rows = max(selected_page_counts, default=1)
        affected_lines = sum(selected_page_counts)
        for index, name in enumerate(names):
            action = (
                Action(_swatch_icon(QColor("#FF5A6F")), name, layout_menu)
                if index in current_indices
                else Action(name, layout_menu)
            )
            layout_id = (
                "default"
                if index == 0
                else self._style.layouts[index - 1].layout_id
            )
            if layout_capacity(self._style, layout_id) < max_page_rows:
                action.setEnabled(False)
                action.setToolTip(
                    f"所选页面最多有 {max_page_rows} 行，此布局无法容纳。"
                )
            else:
                action.setToolTip(
                    f"应用到 {max(len(selected_page_indices), 1)} 页，"
                    f"共 {max(affected_lines, len(rows))} 行。"
                )
            action.triggered.connect(
                lambda _checked=False, idx=index, rs=list(rows): (
                    self.layoutChangeRequested.emit(rs, idx)
                )
            )
            layout_menu.addAction(action)
        menu.addMenu(layout_menu)
        menu.exec(self._table.viewport().mapToGlobal(pos))

    def _request_role_change(self, rows: list[int], role_name: str) -> None:
        """确认可能的逐字符覆盖后，发出一次批量整行角色修改。"""
        if self._track is None or not rows:
            return
        mixed_count = sum(
            1
            for row in rows
            if 0 <= row < len(self._track.lines)
            and _line_role_mixed(self._track.lines[row])
        )
        if mixed_count:
            display = role_name or (
                "标题默认" if self._title_mode else _DEFAULT_ROLE_TEXT
            )
            confirmed = fluent_question(
                self,
                "整行覆盖角色",
                f"所选行中有 {mixed_count} 行包含逐字符角色分配，"
                f"确定全部整行覆盖为“{display}”吗？",
                yes_text="覆盖",
                no_text="取消",
                default_cancel=True,
            )
            if not confirmed:
                return
        # 整行角色与字符数无关，标题不必先把 {title} / {artist} 固定成纯文字；
        # 只有逐字符编辑（_edit_char_roles）才需要展开模板。
        self.roleChangeRequested.emit(list(rows), role_name)

    def _on_cell_clicked(self, row: int, column: int) -> None:
        """跳转到歌词行；角色列单击直接弹出 Fluent 角色菜单。"""
        track_rows = self._track_rows_for_presentation_row(row)
        if not track_rows:
            return
        if (
            0 <= row < len(self._presentation_rows)
            and self._presentation_rows[row].kind
            in {"page_marker", "section_marker"}
        ):
            selection = self._table.selectionModel()
            selection.clearSelection()
            flags = (
                QItemSelectionModel.SelectionFlag.Select
                | QItemSelectionModel.SelectionFlag.Rows
            )
            display_rows = {
                display_row
                for track_row in track_rows
                for display_row in [self._display_row_for_track_line(track_row)]
                if display_row is not None
            }
            display_rows.add(row)
            for display_row in sorted(display_rows):
                selection.select(
                    self._table.model().index(display_row, COL_LANE),
                    flags,
                )
        track_row = track_rows[0]
        self.rowClicked.emit(track_row)
        if column == COL_ROLE:
            self._show_role_picker(track_row)
        elif column == COL_LAYOUT and not self._title_mode:
            self._show_layout_picker(track_row, row)

    def _consume_press_for_batch_picker(self, point, modifiers) -> bool:
        """选中多行后再点「角色 / 布局」列时，吃掉这次按下、保住选区。

        QTableWidget 是在**按下**时就把选区重置成点中的那一行的，``cellClicked``
        要等到松开才发 —— 等我们的代码跑起来多选早没了。所以在这里拦：无修饰键的
        左键、落在这两列、而且该行本来就在选区里，就不交给表格处理，自己把选择器
        弹出来，作用于整个选区。

        其余情况一律放行：点选区外的行、Ctrl / Shift 点选、其它列的框选起点，
        行为和以前完全一样。
        """
        if self._track is None:
            return False
        if modifiers & (
            Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier
        ):
            return False
        column = self._table.columnAt(point.x())
        if column == COL_ROLE:
            pass
        elif column == COL_LAYOUT and not self._title_mode:
            pass
        else:
            return False
        display_row = self._table.rowAt(point.y())
        if display_row < 0:
            return False
        track_rows = self._track_rows_for_presentation_row(display_row)
        if not track_rows:
            return False
        selected = self._selected_track_rows()
        # 只有"点在已选中的多行里"才需要特殊对待；单行还是走原来的路。
        if len(selected) <= 1 or track_rows[0] not in selected:
            return False
        track_row = track_rows[0]
        self.rowClicked.emit(track_row)
        # 菜单不能在事件派发中途 exec —— 等这一拍走完再弹。
        QTimer.singleShot(
            0,
            lambda: (
                self._show_role_picker(track_row)
                if column == COL_ROLE
                else self._show_layout_picker(track_row, display_row)
            ),
        )
        return True

    def _selected_track_rows(self) -> list[int]:
        """当前选中的歌词行（按 track 下标去重）。"""
        display_rows = sorted({item.row() for item in self._table.selectedItems()})
        return list(
            dict.fromkeys(
                track_row
                for display_row in display_rows
                for track_row in self._track_rows_for_presentation_row(display_row)
            )
        )

    def _picker_target_rows(self, track_row: int) -> list[int]:
        """单击「角色 / 布局」单元格时，这一下要作用到哪几行。

        点的是**已选中**的行 → 整个选区一起改（多选批量的入口）；点的是选区外的行
        → 只改它自己，和以前一样，否则"点一行却改了别处"会很怪。
        """
        rows = self._selected_track_rows()
        return rows if track_row in rows else [track_row]

    def _show_role_picker(self, row: int) -> None:
        if self._track is None or not 0 <= row < len(self._track.lines):
            return
        line = self._track.lines[row]
        if line.is_blank or not line.chars:
            return
        rows = self._picker_target_rows(row)
        menu = _StableRoundMenu(parent=self._table)
        current_roles = {
            _dominant_role(self._track.lines[r])
            for r in rows
            if r < len(self._track.lines)
        }
        current = next(iter(current_roles)) if len(current_roles) == 1 else None
        mixed = len(current_roles) > 1 or any(
            _line_role_mixed(self._track.lines[r])
            for r in rows
            if r < len(self._track.lines)
        )
        default_text = "标题默认" if self._title_mode else _DEFAULT_ROLE_TEXT
        default_swatch = TITLE_SCHEME_NAME if self._title_mode else ""
        choices = [(default_text, "", default_swatch)] + [
            (name, name, name) for name in self._role_options
        ]
        if len(rows) > 1:
            scope = Action(f"应用到所选 {len(rows)} 行", menu)
            scope.setEnabled(False)
            menu.addAction(scope)
            menu.addSeparator()
        for display, name, swatch_name in choices:
            action = Action(
                _swatch_icon(_scheme_swatch_color(self._style, swatch_name)),
                display,
                menu,
            )
            action.setCheckable(True)
            action.setChecked(not mixed and current == name)
            action.triggered.connect(
                lambda _checked=False, value=name, rs=list(rows): (
                    self._request_role_change(rs, value)
                )
            )
            menu.addAction(action)
        display_row = self._display_row_for_track_line(row)
        if display_row is None:
            return
        item = self._table.item(display_row, COL_ROLE)
        if item is None:
            return
        rect = self._table.visualItemRect(item)
        popup_pos = self._table.viewport().mapToGlobal(
            QPoint(rect.left(), rect.bottom() + 1)
        )
        menu.exec(popup_pos)

    def _show_layout_picker(self, track_row: int, display_row: int) -> None:
        if self._track is None:
            return
        rows = self._picker_target_rows(track_row)
        # 容量按**选区里最大的那一页**判断，和右键菜单同一套口径 —— 否则会放出一个
        # 装不下其它选中页的布局。
        page_counts: list[int] = []
        if self._resolved_page_plan is not None:
            page_indices = {
                line.global_page_index
                for row in rows
                for line in [self._resolved_page_plan.line_for_track_index(row)]
                if line is not None
            }
            page_counts = [
                self._resolved_page_plan.pages[index].line_count
                for index in page_indices
            ]
        page_rows = max(page_counts, default=1)
        current_indices = {
            int(getattr(self._track.lines[row], "layout_index", 0) or 0)
            for row in rows
            if row < len(self._track.lines)
        }
        menu = _StableRoundMenu(parent=self._table)
        if len(rows) > 1:
            scope = Action(f"应用到所选 {len(rows)} 行", menu)
            scope.setEnabled(False)
            menu.addAction(scope)
            if len(current_indices) > 1:
                mixed = Action("当前：多个布局", menu)
                mixed.setEnabled(False)
                menu.addAction(mixed)
            menu.addSeparator()
        names = [layout_display_name(self._style, "default")] + [
            layout.name for layout in self._style.layouts
        ]
        for index, name in enumerate(names):
            action = Action(name, menu)
            action.setCheckable(True)
            action.setChecked(len(current_indices) == 1 and index in current_indices)
            layout_id = (
                "default"
                if index == 0
                else self._style.layouts[index - 1].layout_id
            )
            if layout_capacity(self._style, layout_id) < page_rows:
                action.setEnabled(False)
                action.setToolTip(
                    f"所选页面最多有 {page_rows} 行，此布局无法容纳。"
                    if len(rows) > 1
                    else f"当前页面有 {page_rows} 行，此布局无法容纳。"
                )
            action.triggered.connect(
                lambda _checked=False, idx=index, rs=list(rows): (
                    self.layoutChangeRequested.emit(rs, idx)
                )
            )
            menu.addAction(action)
        item = self._table.item(display_row, COL_LAYOUT)
        if item is None:
            return
        rect = self._table.visualItemRect(item)
        menu.exec(
            self._table.viewport().mapToGlobal(
                QPoint(rect.left(), rect.bottom() + 1)
            )
        )

    def _on_cell_double_clicked(self, row: int, column: int) -> None:
        if column not in (COL_EFFECT, COL_CONTENT) or self._track is None:
            return
        track_rows = self._track_rows_for_presentation_row(row)
        if not track_rows:
            return
        track_row = track_rows[0]
        line = self._track.lines[track_row]
        if line.is_blank or not line.chars:
            return
        if column == COL_EFFECT and not self._title_mode:
            self._edit_animation_rows([track_row])
        elif column == COL_CONTENT:
            self._edit_char_roles(track_row)

    def _edit_animation_rows(self, rows: list[int]) -> None:
        if self._track is None or not rows:
            return
        first = self._track.lines[rows[0]].animation_override
        dialog = _LineAnimationDialog(self._style, first, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.animationOverrideRequested.emit(list(rows), dialog.animation_override())

    def _edit_char_roles(self, row: int) -> None:
        """打开行内逐字符角色编辑器，确定后按整行标签列表发给宿主。"""
        if self._title_mode:
            self.titleEditRequested.emit()
        if self._track is None or not 0 <= row < len(self._track.lines):
            return
        line = self._track.lines[row]
        if line.is_blank or not line.chars:
            return
        guide = line.guide_symbol if not self._title_mode else None
        replacement_count = guide_symbol_replacement_count(line)
        visible_chars = line.chars[replacement_count:]
        texts = [ch.text for ch in visible_chars]
        labels = [ch.role_label for ch in visible_chars]
        vector_symbols: list[Optional[GuideSymbol]] = [
            line.inline_guide_symbols.get(index)
            for index in range(replacement_count, len(line.chars))
        ]
        original_visible_vectors = list(vector_symbols)
        guide_count = 0
        if guide is not None:
            guide_count = max(int(guide.count), 1)
            texts[0:0] = ["导"] * guide_count
            labels[0:0] = list(guide_symbol_role_labels(guide))
            vector_symbols[0:0] = [guide] * guide_count
        dialog = _CharRoleDialog(
            row,
            texts,
            labels,
            list(self._role_options),
            self._style,
            self,
            default_role_text="标题默认" if self._title_mode else _DEFAULT_ROLE_TEXT,
            default_swatch_role=TITLE_SCHEME_NAME if self._title_mode else "",
            vector_symbols=vector_symbols,
            protected_prefix_count=guide_count,
            allow_svg_replacement=not self._title_mode,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        labels = dialog.char_labels()
        vectors = dialog.char_vector_symbols()
        guide_labels = labels[:guide_count] if guide is not None else None
        char_labels = labels[guide_count:]
        char_vectors = vectors[guide_count:]
        if char_vectors != original_visible_vectors:
            self.inlineCharEditChanged.emit(
                row, guide_labels, char_labels, char_vectors
            )
            return
        if guide is not None:
            if guide_labels == list(guide_symbol_role_labels(guide)) and char_labels == [
                ch.role_label for ch in visible_chars
            ]:
                return
            self.guideCharRolesChanged.emit(row, guide_labels, char_labels)
            return
        if labels == [ch.role_label for ch in line.chars]:
            return
        self.charRolesChanged.emit(row, labels)

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if item.column() != COL_ROLE:
            return
        role_name = str(item.data(Qt.ItemDataRole.UserRole) or "")
        display_row = item.row()
        rows = self._track_rows_for_presentation_row(display_row)
        if not rows:
            return
        row = rows[0]
        # 混合行整行覆盖前确认，避免误抹掉行内逐字符分配。
        if (
            self._track is not None
            and 0 <= row < len(self._track.lines)
            and _line_role_mixed(self._track.lines[row])
        ):
            display = role_name if role_name else (
                "标题默认" if self._title_mode else _DEFAULT_ROLE_TEXT
            )
            confirmed = fluent_question(
                self,
                "整行覆盖角色",
                f"该行包含逐字符角色分配，确定整行覆盖为“{display}”吗？",
                yes_text="覆盖",
                no_text="取消",
                default_cancel=True,
            )
            if not confirmed:
                # 还原显示为混合态（数据未写回，行内标签保持原样）
                self._refresh_presentation(rows=[display_row])
                return
        self.roleChanged.emit(row, role_name)

    def _panel_style_spec(self) -> tuple[str, int, str, str, int]:
        # 载入后表格铺满面板：去边框、去圆角；空态 / 拖拽态沿用基类
        if self._populated and self._drag_state == "idle":
            return "transparent", 0, "solid", palette().card_bg, 0
        return super()._panel_style_spec()


def _dominant_role(line) -> str:
    """返回本行出现次数最多的角色标签；无角色时返回空字符串。"""
    roles = []
    if line.guide_symbol is not None:
        roles.extend(
            label for label in guide_symbol_role_labels(line.guide_symbol) if label
        )
    roles.extend(ch.role_label for ch in line_visible_chars(line) if ch.role_label)
    if not roles:
        return ""
    return Counter(roles).most_common(1)[0][0]
