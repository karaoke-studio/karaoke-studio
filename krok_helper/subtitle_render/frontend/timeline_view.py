"""底部字幕轨道（被动显示，不接受拖拽）。

对标 Sayatoo 轨道区：顶部时间刻度尺 + 多行轨道 T1 / T2 / T3…（T1 = 主字幕，
其余为副字幕源）+ 底部缩放滚动条。每行按歌词行渲染演唱区间色块，块内按
字符细分并**显示歌词字符**；按演唱者上色；点击块定位到对应字符起始时间，
拖动刻度尺连续定位；竖线播放头与 ``TransportBar`` 对齐，播放越界时自动翻页。

比例尺：默认视口 15 秒。Ctrl+滚轮以光标为中心缩放；滚轮水平平移；底部
缩放条两端圆形把手拖动改变视口范围，拖中段平移（对标 Sayatoo 同款控件）。

音频源自动取自视频文件（``load_video`` 同时喂给 ``TransportBar``），所以这里
不做独立的"拖入音频"入口。波形图功能已弃用——不做峰值波形展示。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from PyQt6.QtCore import QPointF, QRect, QRectF, Qt, pyqtSignal as Signal
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QIntValidator, QPainter, QPixmap
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QStyle,
    QStyleOption,
    QWidget,
)
from qfluentwidgets import BodyLabel, CardWidget, LineEdit, PrimaryPushButton

from krok_helper.qfluent_compat import hide_fluent_tooltip, show_fluent_tooltip
from krok_helper.subtitle_render.engine.timeline_projection import (
    resolve_utopia_visual_intervals,
    source_char_intervals,
)
from krok_helper.subtitle_render.frontend.theme import palette, themed
from krok_helper.subtitle_render.models import (
    RubyAnnotation,
    Style,
    TimingLine,
    TimingTrack,
    style_with_line_animation,
)

_LABEL_GUTTER_W = 72
"""左侧轨道名列宽（"T1 主字幕"）。"""

_RULER_H = 20
"""顶部时间刻度尺高度。"""

_ZOOMBAR_H = 18
"""底部缩放滚动条区高度。"""

_HANDLE_STRIP_H = 16
"""轨道与缩放条之间的句子显示/隐藏时间把手条高度。"""

_HANDLE_MIN_W = 12
"""虚线把手的最小鼠标命中宽度（像素）；不改变实际绘制宽度。"""

_LANE_MIN_H = 20
_LANE_MAX_H = 44
_LANE_GAP = 4
_PAD_X = 12
_FALLBACK_CHAR_MS = 1000
"""行缺 ``end_ms`` 时最后一个字符的兜底时长，与 ``timeline._line_end_ms`` 一致。"""

_DEFAULT_VIEW_SPAN_MS = 15_000
"""默认比例尺：一屏 15 秒。"""

_MIN_VIEW_SPAN_MS = 2_000
"""最大放大：一屏 2 秒。"""

_TICK_LADDER_MS = (1_000, 2_000, 5_000, 10_000, 15_000, 30_000, 60_000, 120_000, 300_000)
"""刻度主格挡位，按缩放自适应选择。"""

_ENTRY_ANIMATION_LABELS = {
    "none": "无",
    "fade": "淡入",
    "slide_in": "滑入",
    "rise": "上升",
    "char_fade": "逐字淡入",
    "char_drip": "文字垂下",
    "spin_flip": "翻转",
    "utopia": "Utopia",
}
_EXIT_ANIMATION_LABELS = {
    "none": "无",
    "fade": "淡出",
    "slide_out": "滑出",
    "rise": "上升",
    "char_fade": "逐字淡出",
    "char_drip": "文字垂出",
    "spin_flip": "翻转",
    "utopia": "Utopia",
}


@dataclass(frozen=True)
class CharCell:
    """块内单个字符的时间区间。"""

    text: str
    start_ms: int
    end_ms: int


@dataclass(frozen=True)
class LineBlock:
    """一行歌词的演唱区间色块。"""

    line_index: int
    start_ms: int
    end_ms: int
    singer_id: Optional[int]
    singer_label: Optional[str]
    text: str
    cells: tuple[CharCell, ...]


@dataclass(frozen=True)
class Lane:
    """一条轨道（一个字幕源）。"""

    name: str
    blocks: tuple[LineBlock, ...]


def _raw_char_cells(line: TimingLine, end_ms: int) -> list[CharCell]:
    return [
        CharCell(char.text, start_ms, cell_end_ms)
        for char, (start_ms, cell_end_ms) in zip(
            line.chars,
            source_char_intervals(line, end_ms),
        )
    ]


def _visual_utopia_char_cells(
    line: TimingLine,
    end_ms: int,
    style: Style,
    rubies: Sequence[RubyAnnotation],
) -> list[CharCell] | None:
    intervals = resolve_utopia_visual_intervals(
        line,
        end_ms,
        style,
        rubies,
    )
    if intervals is None:
        return None
    return [
        CharCell(char.text, start_ms, end_ms)
        for char, (start_ms, end_ms) in zip(line.chars, intervals)
    ]


def _line_block(
    line: TimingLine,
    line_index: int,
    style: Style | None = None,
    rubies: Sequence[RubyAnnotation] = (),
) -> Optional[LineBlock]:
    if line.is_blank or not line.chars:
        return None
    end_ms = (
        line.end_ms
        if line.end_ms is not None
        else line.chars[-1].start_ms + _FALLBACK_CHAR_MS
    )
    cells = (
        _visual_utopia_char_cells(line, end_ms, style, rubies)
        if style is not None
        else None
    ) or _raw_char_cells(line, end_ms)
    return LineBlock(
        line_index=line_index,
        start_ms=cells[0].start_ms,
        end_ms=max(end_ms, cells[-1].end_ms),
        singer_id=line.singer_id,
        singer_label=line.singer_label,
        text="".join(ch.text for ch in line.chars),
        cells=tuple(cells),
    )


def build_lanes(
    tracks: Sequence[tuple[str, TimingTrack]],
    style: Style | None = None,
) -> tuple[Lane, ...]:
    """把（源名，track）列表转成轨道块结构。"""
    lanes: list[Lane] = []
    for name, track in tracks:
        blocks = []
        for index, line in enumerate(track.lines):
            block = _line_block(line, index, style, track.rubies)
            if block is not None:
                blocks.append(block)
        lanes.append(Lane(name=name, blocks=tuple(blocks)))
    return tuple(lanes)


def _lanes_end_ms(lanes: Sequence[Lane]) -> int:
    return max(
        (block.end_ms for lane in lanes for block in lane.blocks),
        default=0,
    )


def _singer_hue(singer_id: Optional[int]) -> Optional[float]:
    if singer_id is None:
        return None
    return (0.58 + singer_id * 0.618033988749895) % 1.0


class TrackTimelineView(QWidget):
    """字幕轨道视图：刻度尺 + 多轨字符块 + 缩放条；静态内容画进缓存 pixmap。"""

    seekRequested = Signal(int)
    """用户点击 / 拖动轨道请求跳转（毫秒）。"""

    displayWindowEdited = Signal(int, int, object, object)
    """用户拖动把手完成了一次显示/隐藏时间编辑：
    ``(轨道序号, 行索引, 旧 (上屏覆盖, 消失覆盖), 新 (上屏覆盖, 消失覆盖))``。
    新值已直接写在 ``TimingLine`` 上；宿主收到后刷新预览、标脏，并用
    旧值入撤销栈（Ctrl+Z）。拖动无实际变化时不发。"""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("TrackTimelineView")
        self.setMinimumHeight(120)
        self.setMouseTracking(True)
        self._lanes: tuple[Lane, ...] = ()
        self._track_names: list[str] = []
        self._track_refs: list[TimingTrack] = []
        """与 ``_lanes`` 对齐的 TimingTrack 引用，把手拖动直接写覆盖字段。"""
        self._windows: list[dict[int, tuple[int, int]]] = []
        """宿主推送的显示窗口：每轨 ``行索引 → (上屏, 消失)``（含全局提前/延迟）。"""
        self._style = Style()
        self._selected: Optional[tuple[int, int]] = None
        """当前选中句：(轨道序号, ``track.lines`` 行索引)。"""
        self._duration_ms = 0
        self._time_ms = 0
        self._view_start_ms = 0.0
        self._view_span_ms = float(_DEFAULT_VIEW_SPAN_MS)
        self._desired_span_ms = float(_DEFAULT_VIEW_SPAN_MS)
        """用户期望的比例尺；生效值随总时长夹取（时长后到位时恢复期望值）。"""
        self._drag: Optional[tuple] = None
        """进行中的拖动：("scrub",) / ("pan", 抓取点相对视口起点的毫秒差) /
        ("lead|tail", 轨道, 句块, 旧覆盖, 拖动起点毫秒) / ("zoom_left",) /
        ("zoom_right",)。拖动期间暂停播放头自动翻页。"""
        self._margin_editor: Optional[CardWidget] = None
        self._margin_edit: Optional[LineEdit] = None
        self._margin_editor_context: Optional[tuple[int, LineBlock, bool, tuple]] = None
        self._lanes_pixmap: Optional[QPixmap] = None
        self._pixmap_key: Optional[tuple] = None
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        # 裸 QWidget 不设此属性时 QSS 背景/边框不会绘制
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        # themed 回调在主题切换时重设 QSS → 顺带触发重绘；缓存 key 含主题色所以会失效重画
        themed(
            self,
            lambda: (
                f"#TrackTimelineView {{ background: {palette().panel_bg}; "
                f"border: 1px solid {palette().card_border}; "
                f"border-radius: 8px; }}"
            ),
        )

    # ------------------------------------------------------------------ API

    def set_tracks(self, tracks: Sequence[tuple[str, TimingTrack]]) -> None:
        """设置全部字幕源：``[(源名, TimingTrack), ...]``，空列表回空态。

        重置视口到开头、比例尺回默认 15 秒，并清除句子选中态。
        """
        self._lanes = build_lanes(tracks, self._style)
        self._track_names = [name for name, _track in tracks]
        self._track_refs = [track for _name, track in tracks]
        self._windows = []
        self._selected = None
        self._hide_margin_editor()
        self._view_start_ms = 0.0
        self._desired_span_ms = float(_DEFAULT_VIEW_SPAN_MS)
        self._clamp_view()
        self._lanes_pixmap = None
        self.update()

    def set_display_windows(
        self, windows: Sequence[dict[int, tuple[int, int]]]
    ) -> None:
        """宿主推送各轨道的行显示窗口（与预览同一套布局参数算出）。"""
        self._windows = [dict(item) for item in windows]
        self.update()

    def set_style(self, style: Style) -> None:
        """设置用于解析逐行有效入退场特效的当前样式。"""
        if style == self._style:
            return
        self._style = style
        if self._track_refs:
            self._lanes = build_lanes(
                list(zip(self._track_names, self._track_refs)),
                self._style,
            )
            self._lanes_pixmap = None
        self.update()

    def set_duration(self, ms: int) -> None:
        self._duration_ms = max(int(ms), 0)
        self._clamp_view()
        self._lanes_pixmap = None
        self.update()

    def set_time(self, ms: int) -> None:
        ms = max(int(ms), 0)
        if ms == self._time_ms:
            return
        self._time_ms = ms
        if self._follow_playhead(ms):
            self.update()
            return
        self.update()

    @property
    def view_start_ms(self) -> float:
        return self._view_start_ms

    @property
    def view_span_ms(self) -> float:
        return self._view_span_ms

    # ------------------------------------------------------------ viewport

    def _timeline_duration_ms(self) -> int:
        return max(self._duration_ms, _lanes_end_ms(self._lanes), 1)

    def _clamp_view(self) -> None:
        total = float(self._timeline_duration_ms())
        span = min(
            max(self._desired_span_ms, float(_MIN_VIEW_SPAN_MS)),
            max(total, float(_MIN_VIEW_SPAN_MS)),
        )
        start = min(max(self._view_start_ms, 0.0), max(total - span, 0.0))
        self._view_span_ms = span
        self._view_start_ms = start

    def _set_view(self, start_ms: float, span_ms: float) -> None:
        self._view_start_ms = start_ms
        self._desired_span_ms = span_ms
        self._clamp_view()
        self.update()

    def _pan_by(self, delta_ms: float) -> None:
        self._set_view(self._view_start_ms + delta_ms, self._view_span_ms)

    def _zoom_about(self, anchor_ms: float, factor: float) -> None:
        """以 ``anchor_ms`` 为不动点缩放视口（factor < 1 放大）。"""
        old_span = self._view_span_ms
        new_span = old_span * factor
        ratio = (anchor_ms - self._view_start_ms) / old_span
        self._set_view(anchor_ms - ratio * new_span, new_span)

    def _follow_playhead(self, ms: int) -> bool:
        """播放头越出视口时自动翻页（拖动中不打架）。"""
        if not self._lanes or self._drag is not None:
            return False
        start, span = self._view_start_ms, self._view_span_ms
        if start <= ms <= start + span:
            return False
        # 前跳翻新页：播放头落在新页 10% 处；回跳同理
        self._set_view(ms - span * 0.1, span)
        return True

    # ------------------------------------------------------------ geometry

    def _plot_left(self) -> int:
        return _LABEL_GUTTER_W

    def _plot_width(self) -> int:
        return max(self.width() - _LABEL_GUTTER_W - _PAD_X, 1)

    def _x_for_ms(self, ms: float) -> float:
        return (
            self._plot_left()
            + (ms - self._view_start_ms) / self._view_span_ms * self._plot_width()
        )

    def _ms_for_x(self, x: float) -> int:
        ratio = (x - self._plot_left()) / self._plot_width()
        ms = round(self._view_start_ms + ratio * self._view_span_ms)
        return max(0, min(int(ms), self._timeline_duration_ms()))

    def _ruler_rect(self) -> QRectF:
        return QRectF(
            float(self._plot_left()), 0.0, float(self._plot_width()), float(_RULER_H)
        )

    def _zoombar_rect(self) -> QRectF:
        return QRectF(
            float(self._plot_left()),
            float(self.height() - _ZOOMBAR_H),
            float(self._plot_width()),
            float(_ZOOMBAR_H),
        )

    def _zoombar_x_for_ms(self, ms: float) -> float:
        return self._plot_left() + ms / self._timeline_duration_ms() * self._plot_width()

    def _zoombar_ms_for_x(self, x: float) -> float:
        ratio = (x - self._plot_left()) / self._plot_width()
        return max(0.0, min(ratio, 1.0)) * self._timeline_duration_ms()

    def _zoombar_thumb_span(self) -> tuple[float, float]:
        """缩放条滑块的 (x0, x1)。"""
        x0 = self._zoombar_x_for_ms(self._view_start_ms)
        x1 = self._zoombar_x_for_ms(self._view_start_ms + self._view_span_ms)
        return x0, min(x1, self._plot_left() + self._plot_width())

    def _lane_geometry(self) -> list[tuple[Lane, QRectF]]:
        """每条轨道的内容区矩形（不含左侧名称列）。"""
        if not self._lanes:
            return []
        count = len(self._lanes)
        area_top = _RULER_H + 2
        area_bottom = self.height() - _ZOOMBAR_H - _HANDLE_STRIP_H - 2
        avail = area_bottom - area_top - (count - 1) * _LANE_GAP
        lane_h = max(_LANE_MIN_H, min(_LANE_MAX_H, avail // count))
        total_h = count * lane_h + (count - 1) * _LANE_GAP
        result = []
        # 轨道组在刻度尺与缩放条之间垂直居中
        y = max(float(area_top), area_top + (area_bottom - area_top - total_h) / 2)
        for lane in self._lanes:
            rect = QRectF(
                float(self._plot_left()), y, float(self._plot_width()), float(lane_h)
            )
            result.append((lane, rect))
            y += lane_h + _LANE_GAP
        return result

    def _hit_test(
        self, x: float, y: float
    ) -> tuple[Optional[int], Optional[LineBlock], Optional[CharCell]]:
        for lane_index, (lane, rect) in enumerate(self._lane_geometry()):
            if not rect.top() <= y <= rect.bottom():
                continue
            if x < rect.left():
                return lane_index, None, None
            ms = self._ms_for_x(x)
            for block in reversed(lane.blocks):
                if block.start_ms <= ms < block.end_ms:
                    for cell in block.cells:
                        if cell.start_ms <= ms < cell.end_ms:
                            return lane_index, block, cell
                    return lane_index, block, None
            return lane_index, None, None
        return None, None, None

    # ----------------------------------------------------------- selection

    def _selected_block(self) -> Optional[tuple[int, LineBlock]]:
        """当前选中句对应的 (轨道序号, 块)；选中已失效时返回 None。"""
        if self._selected is None:
            return None
        lane_index, line_index = self._selected
        if not 0 <= lane_index < len(self._lanes):
            return None
        for block in self._lanes[lane_index].blocks:
            if block.line_index == line_index:
                return lane_index, block
        return None

    def _selected_window(self, lane_index: int, block: LineBlock) -> tuple[int, int]:
        """选中句的显示窗口；宿主还没推送时退化为演唱区间（零余量）。"""
        if lane_index < len(self._windows):
            window = self._windows[lane_index].get(block.line_index)
            if window is not None:
                return window
        return block.start_ms, block.end_ms

    def _handle_strip_rect(self) -> QRectF:
        return QRectF(
            float(self._plot_left()),
            float(self.height() - _ZOOMBAR_H - _HANDLE_STRIP_H),
            float(self._plot_width()),
            float(_HANDLE_STRIP_H),
        )

    def _handle_rects(self) -> Optional[tuple[QRectF, QRectF, int, LineBlock]]:
        """选中句的两个虚线把手矩形 (左=上屏余量, 右=消失余量, 轨道序号, 块)。"""
        selected = self._selected_block()
        if selected is None:
            return None
        lane_index, block = selected
        show_ms, hide_ms = self._selected_window(lane_index, block)
        strip = self._handle_strip_rect()
        top = strip.top() + 2
        height = strip.height() - 4

        left_x0 = self._x_for_ms(show_ms)
        left_x1 = self._x_for_ms(block.start_ms)
        right_x0 = self._x_for_ms(block.end_ms)
        right_x1 = self._x_for_ms(hide_ms)
        return (
            QRectF(left_x0, top, left_x1 - left_x0, height),
            QRectF(right_x0, top, right_x1 - right_x0, height),
            lane_index,
            block,
        )

    @staticmethod
    def _handle_hit_rect(rect: QRectF, *, entry: bool) -> QRectF:
        """Return a forgiving hit target without falsifying the painted time span."""

        hit = QRectF(rect)
        if hit.width() < _HANDLE_MIN_W:
            if entry:
                hit.setLeft(hit.right() - _HANDLE_MIN_W)
            else:
                hit.setRight(hit.left() + _HANDLE_MIN_W)
        return hit.adjusted(-3, -3, 3, 3)

    def _apply_lead_drag(self, lane_index: int, block: LineBlock, x: float) -> None:
        """拖左把手：改「上屏时刻」覆盖，不晚于开始走字。"""
        ms = min(self._ms_for_x(x), block.start_ms)
        self._set_display_start_override(lane_index, block, ms)
        self.update()

    def _set_display_start_override(
        self, lane_index: int, block: LineBlock, ms: int
    ) -> None:
        track = self._track_refs[lane_index]
        track.lines[block.line_index].display_start_override_ms = ms
        if lane_index < len(self._windows):
            _old_show, hide = self._windows[lane_index].get(
                block.line_index, (block.start_ms, block.end_ms)
            )
            self._windows[lane_index][block.line_index] = (ms, hide)

    def _apply_tail_drag(self, lane_index: int, block: LineBlock, x: float) -> None:
        """拖右把手：改「消失时刻」覆盖，不早于走字结束。"""
        ms = max(self._ms_for_x(x), block.end_ms)
        self._set_display_end_override(lane_index, block, ms)
        self.update()

    def _set_display_end_override(
        self, lane_index: int, block: LineBlock, ms: int
    ) -> None:
        track = self._track_refs[lane_index]
        track.lines[block.line_index].display_end_override_ms = ms
        if lane_index < len(self._windows):
            show, _old_hide = self._windows[lane_index].get(
                block.line_index, (block.start_ms, block.end_ms)
            )
            self._windows[lane_index][block.line_index] = (show, ms)

    def _apply_margin_value(
        self, lane_index: int, block: LineBlock, *, entry: bool, value_ms: int
    ) -> None:
        margin = max(int(value_ms), 0)
        if entry:
            self._set_display_start_override(
                lane_index,
                block,
                max(block.start_ms - margin, 0),
            )
        else:
            self._set_display_end_override(
                lane_index,
                block,
                block.end_ms + margin,
            )
        self.update()

    def _show_margin_editor(
        self,
        lane_index: int,
        block: LineBlock,
        *,
        entry: bool,
        handle_rect: QRectF,
    ) -> None:
        show_ms, hide_ms = self._selected_window(lane_index, block)
        value = (
            max(block.start_ms - show_ms, 0)
            if entry
            else max(hide_ms - block.end_ms, 0)
        )
        old_values = self._line_override_values(lane_index, block.line_index)

        if self._margin_editor is None:
            frame = CardWidget(self)
            frame.setObjectName("TimelineMarginEditor")
            frame.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
            layout = QHBoxLayout(frame)
            layout.setContentsMargins(8, 4, 8, 4)
            layout.setSpacing(6)
            label = BodyLabel("ms", frame)
            label.setObjectName("TimelineMarginEditorLabel")
            edit = LineEdit(frame)
            edit.setValidator(QIntValidator(0, 5_999_990, edit))
            edit.setClearButtonEnabled(False)
            edit.setPlaceholderText("0")
            edit.setFixedWidth(96)
            ok = PrimaryPushButton("确定", frame)
            ok.setDefault(True)
            ok.setAutoDefault(True)
            layout.addWidget(edit, 1)
            layout.addWidget(label)
            layout.addWidget(ok)
            edit.returnPressed.connect(self._commit_margin_editor)
            ok.clicked.connect(self._commit_margin_editor)
            self._margin_editor = frame
            self._margin_edit = edit

        assert self._margin_editor is not None
        assert self._margin_edit is not None
        self._margin_editor_context = (lane_index, block, bool(entry), old_values)
        self._margin_edit.blockSignals(True)
        self._margin_edit.setText(str(int(value)))
        self._margin_edit.blockSignals(False)

        lane_rect = self._lane_geometry()[lane_index][1]
        width = 156
        height = max(32, min(44, int(lane_rect.height())))
        x = int(handle_rect.center().x() - width / 2)
        x = max(self._plot_left(), min(x, self.width() - width - 6))
        y = int(lane_rect.center().y() - height / 2)
        y = max(int(lane_rect.top()), min(y, int(lane_rect.bottom()) - height))
        self._margin_editor.setGeometry(QRect(x, y, width, height))
        self._margin_editor.raise_()
        self._margin_editor.show()
        self._margin_edit.setFocus(Qt.FocusReason.PopupFocusReason)
        self._margin_edit.selectAll()

    def _commit_margin_editor(self) -> None:
        if (
            self._margin_editor_context is None
            or self._margin_edit is None
            or self._margin_editor is None
        ):
            return
        lane_index, block, entry, old_values = self._margin_editor_context
        text = self._margin_edit.text().strip()
        self._apply_margin_value(
            lane_index,
            block,
            entry=entry,
            value_ms=int(text) if text else 0,
        )
        new_values = self._line_override_values(lane_index, block.line_index)
        self._hide_margin_editor()
        if new_values != old_values:
            self.displayWindowEdited.emit(
                lane_index, block.line_index, old_values, new_values
            )

    def _hide_margin_editor(self) -> None:
        if self._margin_editor is not None:
            self._margin_editor.hide()
        self._margin_editor_context = None

    # ------------------------------------------------------------- painting

    def _theme_key(self) -> tuple:
        p = palette()
        return (p.panel_bg, p.accent_primary, p.text_secondary, p.card_border)

    def paintEvent(self, event) -> None:  # noqa: N802 — Qt override
        painter = QPainter(self)
        # 覆写 paintEvent 后 QSS 背景不再自动绘制，手动画 PE_Widget 原语补回
        option = QStyleOption()
        option.initFrom(self)
        self.style().drawPrimitive(
            QStyle.PrimitiveElement.PE_Widget, option, painter, self
        )
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if not self._lanes:
            self._paint_empty_hint(painter)
            painter.end()
            return
        key = (
            self.width(),
            self.height(),
            self._timeline_duration_ms(),
            self._lanes,
            round(self._view_start_ms),
            round(self._view_span_ms),
            self._theme_key(),
        )
        if self._lanes_pixmap is None or self._pixmap_key != key:
            self._lanes_pixmap = self._render_static_pixmap()
            self._pixmap_key = key
        painter.drawPixmap(0, 0, self._lanes_pixmap)
        self._paint_selection(painter)
        self._paint_playhead(painter)
        self._paint_drag_badge(painter)
        painter.end()

    def _paint_empty_hint(self, painter: QPainter) -> None:
        p = palette()
        painter.setPen(QColor(p.text_hint))
        font = QFont("Microsoft YaHei UI")
        font.setPointSizeF(9.5)
        painter.setFont(font)
        painter.drawText(
            self.rect(),
            Qt.AlignmentFlag.AlignCenter,
            "字幕轨道\n加载字幕后按 T1 / T2 / T3 轨道显示字符级时间色块",
        )

    def _render_static_pixmap(self) -> QPixmap:
        ratio = self.devicePixelRatioF()
        pixmap = QPixmap(round(self.width() * ratio), round(self.height() * ratio))
        pixmap.setDevicePixelRatio(ratio)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        self._paint_ruler(painter)
        self._paint_lanes(painter)
        self._paint_zoombar(painter)
        painter.end()
        return pixmap

    def _paint_ruler(self, painter: QPainter) -> None:
        p = palette()
        rect = self._ruler_rect()
        baseline = rect.bottom() - 0.5
        painter.setPen(QColor(p.card_border))
        painter.drawLine(QPointF(rect.left(), baseline), QPointF(rect.right(), baseline))

        # 主格挡位：让相邻主刻度标签间距 ≥ 72px
        px_per_ms = self._plot_width() / self._view_span_ms
        major = next(
            (step for step in _TICK_LADDER_MS if step * px_per_ms >= 72),
            _TICK_LADDER_MS[-1],
        )
        minor = max(major // 5, 200)

        font = QFont("Microsoft YaHei UI")
        font.setPointSizeF(7.5)
        painter.setFont(font)
        metrics = QFontMetrics(font)
        tick_color = QColor(p.text_hint)
        label_color = QColor(p.text_secondary)

        start = int(self._view_start_ms // minor) * minor
        end = int(self._view_start_ms + self._view_span_ms) + minor
        for t in range(start, end + 1, minor):
            if t < 0:
                continue
            x = self._x_for_ms(t)
            if x < rect.left() - 1 or x > rect.right() + 1:
                continue
            is_major = t % major == 0
            tick_h = 6.0 if is_major else 3.0
            painter.setPen(tick_color)
            painter.drawLine(QPointF(x, baseline - tick_h), QPointF(x, baseline))
            if is_major:
                label = _format_ms(t)
                w = metrics.horizontalAdvance(label)
                painter.setPen(label_color)
                painter.drawText(
                    QPointF(
                        min(max(x - w / 2, rect.left()), rect.right() - w),
                        baseline - tick_h - 3,
                    ),
                    label,
                )

    def _paint_lanes(self, painter: QPainter) -> None:
        p = palette()
        dark = bool(getattr(p, "is_dark", False))
        label_font = QFont("Microsoft YaHei UI")
        label_font.setPointSizeF(8.5)
        label_font.setWeight(QFont.Weight.DemiBold)
        metrics = QFontMetrics(label_font)
        for index, (lane, rect) in enumerate(self._lane_geometry()):
            # 左侧轨道名：T1 主字幕
            painter.setFont(label_font)
            painter.setPen(QColor(p.text_secondary))
            label = metrics.elidedText(
                f"T{index + 1} {lane.name}",
                Qt.TextElideMode.ElideRight,
                _LABEL_GUTTER_W - _PAD_X - 6,
            )
            painter.drawText(
                QRectF(_PAD_X, rect.top(), _LABEL_GUTTER_W - _PAD_X - 6, rect.height()),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                label,
            )
            # 轨道底槽
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(p.progress_bg))
            painter.drawRoundedRect(rect, 4.0, 4.0)
            painter.save()
            painter.setClipRect(rect)
            self._paint_lane_blocks(painter, lane, rect, dark)
            painter.restore()

    def _paint_lane_blocks(
        self, painter: QPainter, lane: Lane, rect: QRectF, dark: bool
    ) -> None:
        view_start = self._view_start_ms
        view_end = view_start + self._view_span_ms

        for block in lane.blocks:
            if block.end_ms < view_start or block.start_ms > view_end:
                continue
            self._paint_lane_block(painter, block, rect, dark)

    def _paint_lane_block(
        self,
        painter: QPainter,
        block: LineBlock,
        rect: QRectF,
        dark: bool,
    ) -> None:
        top = rect.top() + 2
        height = rect.height() - 4
        char_font = QFont("Microsoft YaHei UI")
        char_font.setPointSizeF(max(6.5, min(10.5, height * 0.42)))
        char_metrics = QFontMetrics(char_font)
        text_color = QColor("#1F2937")  # 淡色块上固定深色文字（对标 Sayatoo）

        hue = _singer_hue(block.singer_id)
        if hue is None:
            accent = QColor(palette().accent_primary)
            hue = max(accent.hueF(), 0.0)
        # Sayatoo 式外观：淡色底 + 饱和描边 + 字符分隔线
        fill = QColor.fromHsvF(hue, 0.20, 0.97)
        border = QColor.fromHsvF(hue, 0.55, 0.72 if not dark else 0.80)
        separator = QColor.fromHsvF(hue, 0.40, 0.82)

        x0 = self._x_for_ms(block.start_ms)
        x1 = max(self._x_for_ms(block.end_ms), x0 + 2)
        block_rect = QRectF(x0, top, x1 - x0, height)
        painter.setPen(border)
        painter.setBrush(fill)
        painter.drawRoundedRect(block_rect, 2.0, 2.0)

        painter.setFont(char_font)
        for cell in block.cells:
            cx0 = self._x_for_ms(cell.start_ms)
            cx1 = self._x_for_ms(cell.end_ms)
            if cx1 < rect.left() or cx0 > rect.right():
                continue
            cell_w = cx1 - cx0
            if cx0 > x0 + 1:
                painter.setPen(separator)
                painter.drawLine(
                    QPointF(cx0, top + 1), QPointF(cx0, top + height - 1)
                )
            # 单元够宽才画字符，避免缩小后糊成一团
            if cell_w >= char_metrics.horizontalAdvance(cell.text) + 2:
                painter.setPen(text_color)
                painter.drawText(
                    QRectF(cx0, top, cell_w, height),
                    Qt.AlignmentFlag.AlignCenter,
                    cell.text,
                )

    def _paint_zoombar(self, painter: QPainter) -> None:
        p = palette()
        rect = self._zoombar_rect()
        cy = rect.center().y()
        track = QRectF(rect.left(), cy - 3, rect.width(), 6)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(p.progress_bg))
        painter.drawRoundedRect(track, 3.0, 3.0)

        x0, x1 = self._zoombar_thumb_span()
        accent = QColor(p.accent_primary)
        thumb_fill = QColor(accent)
        thumb_fill.setAlpha(90)
        painter.setBrush(thumb_fill)
        painter.drawRoundedRect(QRectF(x0, cy - 3, max(x1 - x0, 2), 6), 3.0, 3.0)
        # 两端圆形把手（对标 Sayatoo 缩放条）
        painter.setPen(accent)
        painter.setBrush(QColor(p.panel_bg))
        for x in (x0, x1):
            painter.drawEllipse(QPointF(x, cy), 5.0, 5.0)

    def _paint_selection(self, painter: QPainter) -> None:
        """选中句高亮 + 把手条上的两个虚线余量框（可拖动）。"""
        handles = self._handle_rects()
        if handles is None:
            return
        left_rect, right_rect, lane_index, block = handles
        accent = QColor(palette().accent_primary)

        # 选中块高亮描边
        geometry = self._lane_geometry()
        if lane_index < len(geometry):
            _lane, lane_rect = geometry[lane_index]
            x0 = self._x_for_ms(block.start_ms)
            x1 = max(self._x_for_ms(block.end_ms), x0 + 2)
            painter.save()
            painter.setClipRect(lane_rect.adjusted(-1, -2, 1, 2))
            self._paint_lane_block(
                painter,
                block,
                lane_rect,
                bool(getattr(palette(), "is_dark", False)),
            )
            pen = painter.pen()
            pen.setColor(accent)
            pen.setWidthF(2.0)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(
                QRectF(x0, lane_rect.top() + 1, x1 - x0, lane_rect.height() - 2),
                2.0,
                2.0,
            )
            painter.restore()

        # 虚线余量框
        painter.save()
        painter.setClipRect(
            QRectF(
                float(self._plot_left()),
                self._handle_strip_rect().top(),
                float(self._plot_width()),
                self._handle_strip_rect().height(),
            )
        )
        fill = QColor(accent)
        fill.setAlpha(28)
        pen = painter.pen()
        pen.setColor(accent)
        pen.setWidthF(1.0)
        pen.setStyle(Qt.PenStyle.DashLine)
        painter.setPen(pen)
        painter.setBrush(fill)
        for rect in (left_rect, right_rect):
            painter.drawRect(rect)
        painter.restore()

    def _paint_playhead(self, painter: QPainter) -> None:
        ms = min(self._time_ms, self._timeline_duration_ms())
        x = self._x_for_ms(ms)
        rect = self._ruler_rect()
        if x < rect.left() or x > rect.right():
            return
        # 一条直线从顶贯穿到底（缩放条是全曲坐标系，不穿过）
        pen_color = QColor(palette().accent_primary)
        pen_color.setAlpha(230)
        painter.setPen(pen_color)
        painter.drawLine(
            QPointF(x, 1.0), QPointF(x, float(self.height() - _ZOOMBAR_H))
        )

    def _drag_badge_content(self) -> Optional[tuple[str, str, int]]:
        """Return the SUG-style delta and absolute time for a margin drag."""

        if self._drag is None or self._drag[0] not in ("lead", "tail"):
            return None
        mode, lane_index, block, _old_values, anchor_ms = self._drag
        show_ms, hide_ms = self._selected_window(lane_index, block)
        current_ms = show_ms if mode == "lead" else hide_ms
        delta = int(current_ms) - int(anchor_ms)
        sign = "+" if delta >= 0 else "−"
        return (
            f"Δ {sign}{abs(delta)} ms",
            f"→ {_format_precise_ms(current_ms)}",
            int(current_ms),
        )

    def _paint_drag_badge(self, painter: QPainter) -> None:
        """Paint the live margin adjustment beside its current time boundary."""

        content = self._drag_badge_content()
        if content is None:
            return
        line1, line2, current_ms = content
        font = QFont("Microsoft YaHei UI")
        font.setPointSizeF(8.0)
        painter.setFont(font)
        metrics = QFontMetrics(font)
        pad = 5
        line_h = metrics.height()
        box_w = max(
            metrics.horizontalAdvance(line1),
            metrics.horizontalAdvance(line2),
        ) + pad * 2
        box_h = line_h * 2 + pad * 2
        x = self._x_for_ms(current_ms) + 8
        y = self._handle_strip_rect().top() - box_h - 6
        x = max(2.0, min(x, float(self.width() - box_w - 2)))
        y = max(float(_RULER_H + 2), y)
        badge = QRectF(x, y, float(box_w), float(box_h))

        painter.save()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(0, 0, 0, 180))
        painter.drawRoundedRect(badge, 4.0, 4.0)
        painter.setPen(QColor(palette().accent_primary))
        baseline = y + pad + metrics.ascent()
        painter.drawText(QPointF(x + pad, baseline), line1)
        painter.drawText(QPointF(x + pad, baseline + line_h), line2)
        painter.restore()

    # ------------------------------------------------------------------ mouse

    def mousePressEvent(self, event) -> None:  # noqa: N802 — Qt override
        if event.button() != Qt.MouseButton.LeftButton or not self._lanes:
            super().mousePressEvent(event)
            return
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        pos = event.position()
        if (
            self._margin_editor is not None
            and self._margin_editor.isVisible()
            and not self._margin_editor.geometry().contains(pos.toPoint())
        ):
            self._hide_margin_editor()
        if pos.x() < self._plot_left():
            super().mousePressEvent(event)
            return
        if self._zoombar_rect().contains(pos):
            self._press_zoombar(pos)
            event.accept()
            return
        handles = self._handle_rects()
        if handles is not None:
            left_rect, right_rect, lane_index, block = handles
            # 按下时记住旧覆盖值，松手时作为一次可撤销的编辑上报
            old_values = self._line_override_values(lane_index, block.line_index)
            if self._handle_hit_rect(left_rect, entry=True).contains(pos):
                show_ms, _hide_ms = self._selected_window(lane_index, block)
                self._drag = ("lead", lane_index, block, old_values, show_ms)
                hide_fluent_tooltip(parent=self)
                event.accept()
                return
            if self._handle_hit_rect(right_rect, entry=False).contains(pos):
                _show_ms, hide_ms = self._selected_window(lane_index, block)
                self._drag = ("tail", lane_index, block, old_values, hide_ms)
                hide_fluent_tooltip(parent=self)
                event.accept()
                return
        if self._ruler_rect().contains(pos):
            self._drag = ("scrub",)
            self.seekRequested.emit(self._ms_for_x(pos.x()))
            event.accept()
            return
        lane_index, block, cell = self._hit_test(pos.x(), pos.y())
        if block is not None:
            # 单击句子 → 选中（出现显示/隐藏余量把手）并跳到点击的字符
            self._selected = (lane_index, block.line_index)
            self.seekRequested.emit(
                cell.start_ms if cell is not None else block.start_ms
            )
        else:
            self._selected = None
            self.seekRequested.emit(self._ms_for_x(pos.x()))
        self.update()
        event.accept()

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802 — Qt override
        if event.button() != Qt.MouseButton.LeftButton or not self._lanes:
            super().mouseDoubleClickEvent(event)
            return
        handles = self._handle_rects()
        if handles is None:
            super().mouseDoubleClickEvent(event)
            return
        pos = event.position()
        left_rect, right_rect, lane_index, block = handles
        if self._handle_hit_rect(left_rect, entry=True).contains(pos):
            self._drag = None
            hide_fluent_tooltip(parent=self)
            self._show_margin_editor(
                lane_index,
                block,
                entry=True,
                handle_rect=left_rect,
            )
            event.accept()
            return
        if self._handle_hit_rect(right_rect, entry=False).contains(pos):
            self._drag = None
            hide_fluent_tooltip(parent=self)
            self._show_margin_editor(
                lane_index,
                block,
                entry=False,
                handle_rect=right_rect,
            )
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def _press_zoombar(self, pos) -> None:
        x0, x1 = self._zoombar_thumb_span()
        if abs(pos.x() - x0) <= 8:
            self._drag = ("zoom_left",)
        elif abs(pos.x() - x1) <= 8:
            self._drag = ("zoom_right",)
        elif x0 <= pos.x() <= x1:
            grab_ms = self._zoombar_ms_for_x(pos.x()) - self._view_start_ms
            self._drag = ("pan", grab_ms)
        else:
            # 点滑块外：视口跳到该处居中，然后转为平移拖动
            center = self._zoombar_ms_for_x(pos.x())
            self._set_view(center - self._view_span_ms / 2, self._view_span_ms)
            self._drag = ("pan", self._view_span_ms / 2)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 — Qt override
        pos = event.position()
        if self._drag is not None:
            mode = self._drag[0]
            if mode == "scrub":
                self.seekRequested.emit(self._ms_for_x(pos.x()))
            elif mode == "lead":
                self._apply_lead_drag(self._drag[1], self._drag[2], pos.x())
            elif mode == "tail":
                self._apply_tail_drag(self._drag[1], self._drag[2], pos.x())
            elif mode == "pan":
                self._set_view(
                    self._zoombar_ms_for_x(pos.x()) - self._drag[1],
                    self._view_span_ms,
                )
            elif mode == "zoom_left":
                end = self._view_start_ms + self._view_span_ms
                new_start = min(
                    self._zoombar_ms_for_x(pos.x()), end - _MIN_VIEW_SPAN_MS
                )
                self._set_view(new_start, end - new_start)
            elif mode == "zoom_right":
                start = self._view_start_ms
                new_end = max(
                    self._zoombar_ms_for_x(pos.x()), start + _MIN_VIEW_SPAN_MS
                )
                self._set_view(start, new_end - start)
            event.accept()
            return
        if not self._lanes:
            super().mouseMoveEvent(event)
            return
        self._update_hover(event, pos)
        super().mouseMoveEvent(event)

    def _update_hover(self, event, pos) -> None:
        handles = self._handle_rects()
        if handles is not None:
            left_rect, right_rect, lane_index, block = handles
            left_hovered = self._handle_hit_rect(left_rect, entry=True).contains(pos)
            right_hovered = self._handle_hit_rect(right_rect, entry=False).contains(pos)
            if left_hovered or right_hovered:
                self.setCursor(Qt.CursorShape.SizeHorCursor)
                tooltip = self._animation_tooltip(
                    lane_index,
                    block,
                    entry=left_hovered,
                )
                show_fluent_tooltip(
                    tooltip,
                    parent=self,
                    global_pos=event.globalPosition().toPoint(),
                )
                return
        if self._zoombar_rect().contains(pos):
            x0, x1 = self._zoombar_thumb_span()
            if abs(pos.x() - x0) <= 8 or abs(pos.x() - x1) <= 8:
                self.setCursor(Qt.CursorShape.SizeHorCursor)
            else:
                self.setCursor(Qt.CursorShape.OpenHandCursor)
            hide_fluent_tooltip(parent=self)
            return
        _lane, block, _cell = self._hit_test(pos.x(), pos.y())
        if block is not None:
            self.setCursor(Qt.CursorShape.PointingHandCursor)
            show_fluent_tooltip(
                _line_block_tooltip(block),
                parent=self,
                global_pos=event.globalPosition().toPoint(),
            )
        else:
            self.unsetCursor()
            hide_fluent_tooltip(parent=self)

    def _animation_tooltip(
        self, lane_index: int, block: LineBlock, *, entry: bool
    ) -> str:
        line = self._track_refs[lane_index].lines[block.line_index]
        effective = style_with_line_animation(self._style, line)
        show_ms, hide_ms = self._selected_window(lane_index, block)
        if entry:
            animation = effective.entry_anim
            label = _ENTRY_ANIMATION_LABELS.get(animation, animation)
            # 余量 = 把手框本身的宽度（上屏 → 开始走字），与特效时长各算各的
            margin_ms = max(block.start_ms - show_ms, 0)
            duration_ms = min(max(int(effective.entry_lead_ms), 0), margin_ms)
            phase = "入场"
            range_start, range_end = show_ms, block.start_ms
        else:
            animation = effective.exit_anim
            label = _EXIT_ANIMATION_LABELS.get(animation, animation)
            margin_ms = max(hide_ms - block.end_ms, 0)
            duration_ms = min(max(int(effective.exit_fade_ms), 0), margin_ms)
            phase = "退场"
            range_start, range_end = block.end_ms, hide_ms
        head = (
            f"{phase}覆盖：{_format_precise_ms(range_start)} → "
            f"{_format_precise_ms(range_end)}（{margin_ms} ms）"
        )
        if animation == "none":
            return f"{head}：{label}"
        return f"{head}：{label}（{duration_ms} ms）"

    def _line_override_values(
        self, lane_index: int, line_index: int
    ) -> tuple[Optional[int], Optional[int]]:
        line = self._track_refs[lane_index].lines[line_index]
        return line.display_start_override_ms, line.display_end_override_ms

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 — Qt override
        if self._drag is not None and self._drag[0] in ("lead", "tail"):
            # 拖动结束才通知宿主刷新预览，避免拖动中反复重渲染
            _mode, lane_index, block, old_values, _anchor_ms = self._drag
            new_values = self._line_override_values(lane_index, block.line_index)
            if new_values != old_values:
                self.displayWindowEdited.emit(
                    lane_index, block.line_index, old_values, new_values
                )
        self._drag = None
        super().mouseReleaseEvent(event)

    def wheelEvent(self, event) -> None:  # noqa: N802 — Qt override
        if not self._lanes:
            super().wheelEvent(event)
            return
        delta = event.angleDelta().y() or event.angleDelta().x()
        if delta == 0:
            super().wheelEvent(event)
            return
        notches = delta / 120.0
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            anchor = self._ms_for_x(event.position().x())
            self._zoom_about(anchor, 0.8**notches)
        else:
            self._pan_by(-notches * self._view_span_ms * 0.15)
        event.accept()

    def leaveEvent(self, event) -> None:  # noqa: N802 — Qt override
        self.unsetCursor()
        hide_fluent_tooltip(parent=self)
        super().leaveEvent(event)


def _format_ms(ms: int) -> str:
    total_seconds = max(int(ms), 0) // 1000
    return f"{total_seconds // 60:02d}:{total_seconds % 60:02d}"


def _format_precise_ms(ms: int) -> str:
    value = max(int(ms), 0)
    total_seconds, millis = divmod(value, 1000)
    return f"{total_seconds // 60:02d}:{total_seconds % 60:02d}.{millis:03d}"


def _line_block_tooltip(block: LineBlock) -> str:
    singer = f"{block.singer_label}：" if block.singer_label else ""
    return (
        f"开始：{_format_precise_ms(block.start_ms)}\n"
        f"结束：{_format_precise_ms(block.end_ms)}\n"
        f"{singer}{block.text}"
    )
