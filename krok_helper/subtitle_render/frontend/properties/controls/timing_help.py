"""Interactive animated help popovers for subtitle timing properties.

Each timing input can display a compact ``?`` badge next to its field title.
Hovering the badge opens a floating card containing a live, theme-aware
QPainter timeline mini-animation modeled after :class:`TimelineView`:
a ruler on top, lyric blocks with row/page badges in the track lane, and
dashed red lead-in / tail-out boxes underneath.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from PyQt6 import sip
from PyQt6.QtCore import (
    QEvent,
    QObject,
    QPoint,
    QPointF,
    QRectF,
    QSize,
    QTimer,
    Qt,
)
from PyQt6.QtGui import (
    QBrush,
    QColor,
    QCursor,
    QFont,
    QFontMetrics,
    QPainter,
    QPainterPath,
    QPen,
    QPolygonF,
)
from PyQt6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from krok_helper.subtitle_render.frontend.widgets.theme import palette, themed


@dataclass(frozen=True)
class TimingHelpSpec:
    """Static metadata describing one timing help animation."""

    key: str
    title: str
    summary: str
    cycle_ms: int = 3800


TIMING_HELP_SPECS: dict[str, TimingHelpSpec] = {
    "line_lead_in_ms": TimingHelpSpec(
        key="line_lead_in_ms",
        title="提前入场",
        summary="控制歌词在开唱前提前多久上屏显示（对应左侧红色虚线框长度）。",
        cycle_ms=3600,
    ),
    "line_tail_ms": TimingHelpSpec(
        key="line_tail_ms",
        title="延迟退场",
        summary="控制歌词在唱完后继续停留多久再消失（对应右侧红色虚线框长度）。",
        cycle_ms=3600,
    ),
    "timing_offset_ms": TimingHelpSpec(
        key="timing_offset_ms",
        title="偏移",
        summary="将整条字幕轴的走字与出入场区间整体提前（负值）或延后（正值）。",
        cycle_ms=4200,
    ),
    "section_ending_mode": TimingHelpSpec(
        key="section_ending_mode",
        title="段落结束",
        summary="「保持」会让段尾字幕挂进间奏；「段末清屏」在段末退场后立即清屏。",
        cycle_ms=4400,
    ),
    "line_lane_gap_ms": TimingHelpSpec(
        key="line_lane_gap_ms",
        title="同轨间隔",
        summary="同一显示行位上，上一页退场与下一页入场之间保留的最小空隙。",
        cycle_ms=3800,
    ),
    "line_protect_ms": TimingHelpSpec(
        key="line_protect_ms",
        title="保护时间",
        summary="同轨相邻页自动避让压缩时，走字两侧至少保留的入/退场余量底线。",
        cycle_ms=4200,
    ),
    "entry_anim_protect_ms": TimingHelpSpec(
        key="entry_anim_protect_ms",
        title="入场动画保护时间",
        summary="发生冲突压缩时，下一页入场动画段最多压到此底线（加速播完不截断）。",
        cycle_ms=4200,
    ),
    "exit_anim_protect_ms": TimingHelpSpec(
        key="exit_anim_protect_ms",
        title="出场动画保护时间",
        summary="发生冲突压缩时，上一页退场动画段至少保留此时长（加速播完不截断）。",
        cycle_ms=4200,
    ),
    "sync_entry": TimingHelpSpec(
        key="sync_entry",
        title="同步入场",
        summary="让同一页内晚唱的句子也提前到本页最早上屏时刻，与首句一起出现。",
        cycle_ms=4000,
    ),
    "sync_ending": TimingHelpSpec(
        key="sync_ending",
        title="同步退场",
        summary="让同一页内先唱完的句子延长停留到本页最晚退场时刻，整页一起消失。",
        cycle_ms=4000,
    ),
    "sync_each_page": TimingHelpSpec(
        key="sync_each_page",
        title="每句同步",
        summary="关闭时仅段首同步入场、段尾同步退场；开启后段内每一页都执行同步。",
        cycle_ms=4400,
    ),
    "ruby_main_progress_mode": TimingHelpSpec(
        key="ruby_main_progress_mode",
        title="正文按注音字符切分（N3 式）",
        summary="正文缺逐字时间时：开启按注音字符数均分，关闭按注音时间段数映射。",
        cycle_ms=4400,
    ),
    "allow_entry_exit_animation_overlap": TimingHelpSpec(
        key="allow_entry_exit_animation_overlap",
        title="允许出入场动画重叠",
        summary="开启后同轨间隔仅约束稳定显示段，上句退场动画与下句入场动画可交叉。",
        cycle_ms=4200,
    ),
    "auto_fill_section_time": TimingHelpSpec(
        key="auto_fill_section_time",
        title="自动填充段内时间",
        summary="开启后段内句子自动延长挂到下一页同位句入场前（隔一个同轨间隔）。",
        cycle_ms=4000,
    ),
    "allow_inter_page_line_overlap": TimingHelpSpec(
        key="allow_inter_page_line_overlap",
        title="启用行间重叠",
        summary="关闭时自动压缩消除跨页冲突；开启后不避让，允许跨页字幕直接叠放。",
        cycle_ms=4200,
    ),
    "overlap_fallback_mode": TimingHelpSpec(
        key="overlap_fallback_mode",
        title="残余冲突处理",
        summary="压缩到保护底线仍冲突时：「吃掉走字」原地顶替；「抬升避让」上移整页。",
        cycle_ms=4600,
    ),
}


def _smoothstep(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return t * t * (3.0 - 2.0 * t)


def _ping_pong_with_holds(
    phase: float,
    *,
    hold_low: float = 0.16,
    rise: float = 0.34,
    hold_high: float = 0.24,
) -> float:
    """Return a smooth 0 -> 1 -> 0 wave with comfortable reading pauses."""
    p = phase % 1.0
    fall = max(1e-6, 1.0 - hold_low - rise - hold_high)
    if p < hold_low:
        return 0.0
    p -= hold_low
    if p < rise:
        return _smoothstep(p / rise)
    p -= rise
    if p < hold_high:
        return 1.0
    p -= hold_high
    return 1.0 - _smoothstep(p / fall)


def _signed_wave_with_holds(phase: float) -> float:
    """Return a smooth 0 -> -1 -> 0 -> +1 -> 0 wave for signed offset demos."""
    p = phase % 1.0
    # 0.00-0.08: 0, 0.08-0.24: 0->-1, 0.24-0.38: -1, 0.38-0.50: -1->0,
    # 0.50-0.58: 0, 0.58-0.74: 0->+1, 0.74-0.88: +1, 0.88-1.00: +1->0
    if p < 0.08:
        return 0.0
    if p < 0.24:
        return -_smoothstep((p - 0.08) / 0.16)
    if p < 0.38:
        return -1.0
    if p < 0.50:
        return -1.0 + _smoothstep((p - 0.38) / 0.12)
    if p < 0.58:
        return 0.0
    if p < 0.74:
        return _smoothstep((p - 0.58) / 0.16)
    if p < 0.88:
        return 1.0
    return 1.0 - _smoothstep((p - 0.88) / 0.12)


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def _ui_font(point_size: float, *, bold: bool = False) -> QFont:
    font = QFont("Microsoft YaHei UI")
    font.setPointSizeF(point_size)
    if bold:
        font.setWeight(QFont.Weight.DemiBold)
    return font


class TimingHelpCanvas(QWidget):
    """Live QPainter timeline diagram demonstrating one timing parameter."""

    _CANVAS_W = 368
    _CANVAS_H = 142
    _FRAME_INTERVAL_MS = 28

    def __init__(
        self,
        key: str,
        parent: Optional[QWidget] = None,
        *,
        on_status_change: Optional[Any] = None,
    ) -> None:
        super().__init__(parent)
        if key not in TIMING_HELP_SPECS:
            raise KeyError(f"Unknown timing help key: {key}")
        self._key = key
        self._spec = TIMING_HELP_SPECS[key]
        self._phase = 0.0
        self._on_status_change = on_status_change
        self._last_status: tuple[str, bool] = ("", False)
        self._timer = QTimer(self)
        self._timer.setInterval(self._FRAME_INTERVAL_MS)
        self._timer.timeout.connect(self._on_tick)
        self.setFixedSize(self._CANVAS_W, self._CANVAS_H)

    @property
    def help_key(self) -> str:
        return self._key

    @property
    def phase(self) -> float:
        return self._phase

    def set_phase(self, phase: float) -> None:
        """Set animation phase in ``[0, 1)`` deterministically (useful for tests)."""
        self._phase = float(phase) % 1.0
        self.update()

    def start_animation(self) -> None:
        self._phase = 0.0
        if not self._timer.isActive():
            self._timer.start()
        self.update()

    def stop_animation(self) -> None:
        if self._timer.isActive():
            self._timer.stop()

    def is_animating(self) -> bool:
        return self._timer.isActive()

    def _on_tick(self) -> None:
        step = self._FRAME_INTERVAL_MS / float(max(self._spec.cycle_ms, 500))
        self._phase = (self._phase + step) % 1.0
        self.update()

    def hideEvent(self, event: Any) -> None:  # noqa: N802
        self.stop_animation()
        super().hideEvent(event)

    # ------------------------------------------------------------------ painting

    def paintEvent(self, event: Any) -> None:  # noqa: N802
        p = palette()
        dark = bool(getattr(p, "is_dark", False))
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        canvas = QRectF(0.5, 0.5, self.width() - 1.0, self.height() - 1.0)
        painter.setPen(QPen(QColor(p.preview_border), 1.0))
        painter.setBrush(QColor(p.preview_bg))
        painter.drawRoundedRect(canvas, 7.0, 7.0)

        painter.save()
        clip_path = QPainterPath()
        clip_path.addRoundedRect(canvas.adjusted(0.5, 0.5, -0.5, -0.5), 6.5, 6.5)
        painter.setClipPath(clip_path)

        # Shared timeline geometry mirroring TimelineView.
        plot_left = 14.0
        plot_right = self.width() - 14.0
        ruler_bottom = 24.0
        lane_top = 45.0
        lane_h = 34.0
        handle_top = 90.0
        handle_h = 15.0

        ctx = {
            "left": plot_left,
            "right": plot_right,
            "width": plot_right - plot_left,
            "ruler_bottom": ruler_bottom,
            "lane_top": lane_top,
            "lane_h": lane_h,
            "handle_top": handle_top,
            "handle_h": handle_h,
            "palette": p,
            "dark": dark,
        }

        dispatch = {
            "line_lead_in_ms": self._paint_lead_in,
            "line_tail_ms": self._paint_tail,
            "timing_offset_ms": self._paint_offset,
            "section_ending_mode": self._paint_section_ending,
            "line_lane_gap_ms": self._paint_lane_gap,
            "line_protect_ms": self._paint_protect,
            "entry_anim_protect_ms": self._paint_entry_anim_protect,
            "exit_anim_protect_ms": self._paint_exit_anim_protect,
            "sync_entry": self._paint_sync_entry,
            "sync_ending": self._paint_sync_ending,
            "sync_each_page": self._paint_sync_each_page,
            "ruby_main_progress_mode": self._paint_ruby_split_mode,
            "allow_entry_exit_animation_overlap": self._paint_anim_overlap,
            "auto_fill_section_time": self._paint_auto_fill,
            "allow_inter_page_line_overlap": self._paint_inter_page_overlap,
            "overlap_fallback_mode": self._paint_overlap_fallback,
        }
        # Custom-canvas scenes skip the standard single-lane background strip.
        custom_lane_keys = {
            "ruby_main_progress_mode",
            "overlap_fallback_mode",
        }
        if self._key not in custom_lane_keys:
            self._paint_ruler(painter, plot_left, plot_right, ruler_bottom, p)
            self._paint_lane_bg(painter, plot_left, plot_right, lane_top, lane_h, p)

        status = dispatch[self._key](painter, ctx)
        if status != self._last_status:
            self._last_status = status
            if self._on_status_change is not None:
                self._on_status_change(status[0], status[1])

        painter.restore()
        painter.end()

    def _x(self, ctx: dict[str, Any], norm: float) -> float:
        return ctx["left"] + ctx["width"] * norm

    def _paint_ruler(
        self,
        painter: QPainter,
        left: float,
        right: float,
        baseline: float,
        p: Any,
    ) -> None:
        painter.setPen(QPen(QColor(p.card_border), 1.0))
        painter.drawLine(QPointF(0.0, baseline), QPointF(float(self.width()), baseline))

        font = _ui_font(7.0)
        painter.setFont(font)
        metrics = QFontMetrics(font)
        tick_color = QColor(p.text_hint)
        label_color = QColor(p.text_secondary)

        major_count = 4
        sub_per_major = 4
        total_steps = major_count * sub_per_major
        span = right - left
        for i in range(total_steps + 1):
            x = left + span * (i / total_steps)
            is_major = i % sub_per_major == 0
            tick_h = 5.5 if is_major else 2.8
            painter.setPen(QPen(tick_color, 1.0))
            painter.drawLine(QPointF(x, baseline - tick_h), QPointF(x, baseline))
            if is_major:
                sec = 14 + (i // sub_per_major) * 2
                text = f"00:{sec:02d}"
                tw = metrics.horizontalAdvance(text)
                tx = max(left, min(right - tw, x - tw / 2.0))
                painter.setPen(label_color)
                painter.drawText(QPointF(tx, baseline - tick_h - 3.0), text)

    def _paint_lane_bg(
        self,
        painter: QPainter,
        left: float,
        right: float,
        top: float,
        height: float,
        p: Any,
    ) -> None:
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(p.progress_bg))
        painter.drawRoundedRect(QRectF(left, top, right - left, height), 4.0, 4.0)

    def _paint_lyric_block(
        self,
        painter: QPainter,
        ctx: dict[str, Any],
        x0: float,
        x1: float,
        *,
        badge: str,
        hue: float = 210 / 360,
        cells: int = 5,
        selected: bool = False,
        dimmed: bool = False,
        label_text: str = "",
    ) -> QRectF:
        p = ctx["palette"]
        dark = ctx["dark"]
        top = ctx["lane_top"] + 2.0
        height = ctx["lane_h"] - 4.0
        rect = QRectF(x0, top, max(4.0, x1 - x0), height)

        if dark:
            fill = QColor.fromHsvF(hue, 0.34, 0.34 if not dimmed else 0.25)
            border = QColor.fromHsvF(hue, 0.62, 0.78 if not dimmed else 0.55)
            sep = QColor.fromHsvF(hue, 0.42, 0.48)
            text_color = QColor("#E5E7EB")
        else:
            fill = QColor.fromHsvF(hue, 0.18 if not dimmed else 0.10, 0.96)
            border = QColor.fromHsvF(hue, 0.55, 0.72 if not dimmed else 0.80)
            sep = QColor.fromHsvF(hue, 0.38, 0.83)
            text_color = QColor("#1F2937")

        painter.setPen(QPen(border, 1.0))
        painter.setBrush(fill)
        painter.drawRoundedRect(rect, 2.5, 2.5)

        # Character cell dividers mirroring TimelineView.
        if cells > 1 and rect.width() > 16.0:
            painter.setPen(QPen(sep, 1.0))
            for i in range(1, cells):
                cx = rect.left() + rect.width() * (i / cells)
                painter.drawLine(QPointF(cx, rect.top() + 1.5), QPointF(cx, rect.bottom() - 1.5))

        if label_text and rect.width() >= 28.0:
            painter.setFont(_ui_font(7.8))
            painter.setPen(text_color)
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, label_text)

        if selected:
            accent = QColor(p.accent_primary)
            painter.setPen(QPen(accent, 1.8))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(rect.adjusted(-0.5, -0.5, 0.5, 0.5), 2.5, 2.5)

        # Row / page badge above the block (same tab style as TimelineView).
        if badge:
            badge_font = _ui_font(7.0, bold=True)
            painter.setFont(badge_font)
            fm = QFontMetrics(badge_font)
            bw = max(fm.horizontalAdvance(badge) + 8.0, 24.0)
            bh = 16.0
            bx = min(rect.left(), ctx["right"] - bw)
            badge_rect = QRectF(bx, top - bh + 1.0, bw, bh)
            painter.setPen(QPen(border, 1.0))
            painter.setBrush(QColor(p.panel_bg))
            painter.drawRoundedRect(badge_rect, 2.5, 2.5)
            painter.setPen(QColor(p.text_primary))
            painter.drawText(badge_rect, Qt.AlignmentFlag.AlignCenter, badge)

        return rect

    def _paint_dashed_box(
        self,
        painter: QPainter,
        ctx: dict[str, Any],
        x0: float,
        x1: float,
        *,
        color: Optional[QColor] = None,
        emphasized: bool = True,
        guide_x: Optional[float] = None,
        y_override: Optional[float] = None,
        h_override: Optional[float] = None,
    ) -> QRectF:
        p = ctx["palette"]
        accent = QColor(color or p.accent_primary)
        y = ctx["handle_top"] if y_override is None else y_override
        h = ctx["handle_h"] if h_override is None else h_override
        rect = QRectF(min(x0, x1), y, max(1.5, abs(x1 - x0)), h)

        # Faint vertical connector from lyric boundary down to the dashed box.
        if guide_x is not None:
            gpen = QPen(QColor(accent.red(), accent.green(), accent.blue(), 110), 1.0, Qt.PenStyle.DotLine)
            painter.setPen(gpen)
            painter.drawLine(
                QPointF(guide_x, ctx["lane_top"] + ctx["lane_h"] - 1.0),
                QPointF(guide_x, y + h),
            )

        fill = QColor(accent)
        fill.setAlpha(46 if emphasized else 24)
        pen = QPen(accent, 1.3 if emphasized else 1.0, Qt.PenStyle.DashLine)
        painter.setPen(pen)
        painter.setBrush(fill)
        painter.drawRect(rect)
        return rect

    def _paint_anim_band(
        self,
        painter: QPainter,
        rect: QRectF,
        *,
        color: QColor,
        locked: bool = False,
    ) -> None:
        if rect.width() <= 1.0:
            return
        painter.save()
        fill = QColor(color)
        fill.setAlpha(155 if locked else 115)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(fill)
        painter.drawRect(rect)

        hatch = QColor(255, 255, 255, 115)
        painter.setBrush(QBrush(hatch, Qt.BrushStyle.BDiagPattern))
        painter.drawRect(rect)

        border = QPen(color, 1.3)
        painter.setPen(border)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(rect)
        painter.restore()

    def _paint_dimension_arrow(
        self,
        painter: QPainter,
        x0: float,
        x1: float,
        y: float,
        label: str,
        color: QColor,
        *,
        label_below: bool = True,
    ) -> None:
        left, right = (x0, x1) if x0 <= x1 else (x1, x0)
        painter.save()
        pen = QPen(color, 1.25)
        painter.setPen(pen)
        painter.setBrush(color)
        if right - left >= 8.0:
            painter.drawLine(QPointF(left + 3.0, y), QPointF(right - 3.0, y))
            ah = 3.2
            aw = 4.5
            painter.drawPolygon(
                QPolygonF([QPointF(left, y), QPointF(left + aw, y - ah), QPointF(left + aw, y + ah)])
            )
            painter.drawPolygon(
                QPolygonF([QPointF(right, y), QPointF(right - aw, y - ah), QPointF(right - aw, y + ah)])
            )
        else:
            painter.drawLine(QPointF(left, y), QPointF(right, y))

        if label:
            font = _ui_font(7.2, bold=True)
            painter.setFont(font)
            fm = QFontMetrics(font)
            tw = fm.horizontalAdvance(label)
            cx = (left + right) / 2.0
            tx = max(6.0, min(self.width() - tw - 6.0, cx - tw / 2.0))
            ty = (y + fm.ascent() + 3.0) if label_below else (y - 4.0)
            painter.setPen(color)
            painter.drawText(QPointF(tx, ty), label)
        painter.restore()

    def _paint_stop_wall(
        self,
        painter: QPainter,
        x: float,
        y0: float,
        y1: float,
        *,
        color: QColor,
        active: bool,
    ) -> None:
        painter.save()
        c = QColor(color)
        c.setAlpha(245 if active else 145)
        pen = QPen(c, 2.0 if active else 1.2)
        painter.setPen(pen)
        painter.drawLine(QPointF(x, y0), QPointF(x, y1))
        # Small top/bottom caps to look like a hard floor barrier.
        cap = 3.5
        painter.drawLine(QPointF(x - cap, y0), QPointF(x + cap, y0))
        painter.drawLine(QPointF(x - cap, y1), QPointF(x + cap, y1))
        painter.restore()

    # ------------------------------------------------------------------ 8 scenes

    def _paint_lead_in(self, painter: QPainter, ctx: dict[str, Any]) -> tuple[str, bool]:
        p = ctx["palette"]
        accent = QColor(p.accent_primary)
        t = _ping_pong_with_holds(self._phase)

        bx0 = self._x(ctx, 0.44)
        bx1 = self._x(ctx, 0.80)
        self._paint_lyric_block(painter, ctx, bx0, bx1, badge="1/2", selected=True, cells=5, label_text="走字演唱")

        # Dynamic left dashed lead-in box + static right tail box.
        lead_w = _lerp(ctx["width"] * 0.10, ctx["width"] * 0.36, t)
        lead_x0 = bx0 - lead_w
        self._paint_dashed_box(painter, ctx, lead_x0, bx0, emphasized=True, guide_x=bx0)
        self._paint_dashed_box(painter, ctx, bx1, bx1 + ctx["width"] * 0.10, emphasized=False)

        # Vertical show-time marker line.
        marker_pen = QPen(accent, 1.2, Qt.PenStyle.DashLine)
        painter.setPen(marker_pen)
        painter.drawLine(QPointF(lead_x0, ctx["ruler_bottom"] + 2.0), QPointF(lead_x0, ctx["handle_top"] + ctx["handle_h"]))

        self._paint_dimension_arrow(
            painter,
            lead_x0,
            bx0,
            ctx["handle_top"] + ctx["handle_h"] + 8.0,
            "提前上屏",
            accent,
        )
        ms = int(round(_lerp(400, 1800, t) / 50.0) * 50)
        return f"提前入场 {ms} ms", t > 0.15

    def _paint_tail(self, painter: QPainter, ctx: dict[str, Any]) -> tuple[str, bool]:
        p = ctx["palette"]
        accent = QColor(p.accent_primary)
        t = _ping_pong_with_holds(self._phase)

        bx0 = self._x(ctx, 0.20)
        bx1 = self._x(ctx, 0.56)
        self._paint_lyric_block(painter, ctx, bx0, bx1, badge="1/2", selected=True, cells=5, label_text="走字演唱")

        # Static left lead-in box + dynamic right tail box.
        self._paint_dashed_box(painter, ctx, bx0 - ctx["width"] * 0.12, bx0, emphasized=False)
        tail_w = _lerp(ctx["width"] * 0.08, ctx["width"] * 0.35, t)
        tail_x1 = bx1 + tail_w
        self._paint_dashed_box(painter, ctx, bx1, tail_x1, emphasized=True, guide_x=bx1)

        marker_pen = QPen(accent, 1.2, Qt.PenStyle.DashLine)
        painter.setPen(marker_pen)
        painter.drawLine(QPointF(tail_x1, ctx["ruler_bottom"] + 2.0), QPointF(tail_x1, ctx["handle_top"] + ctx["handle_h"]))

        self._paint_dimension_arrow(
            painter,
            bx1,
            tail_x1,
            ctx["handle_top"] + ctx["handle_h"] + 8.0,
            "延迟消失",
            accent,
        )
        ms = int(round(_lerp(300, 1500, t) / 50.0) * 50)
        return f"延迟退场 {ms} ms", t > 0.15

    def _paint_offset(self, painter: QPainter, ctx: dict[str, Any]) -> tuple[str, bool]:
        p = ctx["palette"]
        accent = QColor(p.accent_primary)
        wave = _signed_wave_with_holds(self._phase)
        shift_px = wave * ctx["width"] * 0.14

        base_x0 = self._x(ctx, 0.34)
        base_x1 = self._x(ctx, 0.66)

        # Ghost outline at 0 ms reference position.
        ghost_pen = QPen(QColor(p.text_hint), 1.0, Qt.PenStyle.DashLine)
        painter.setPen(ghost_pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(
            QRectF(base_x0, ctx["lane_top"] + 2.0, base_x1 - base_x0, ctx["lane_h"] - 4.0),
            2.5,
            2.5,
        )
        # Reference tick down from ruler.
        ref_color = QColor(p.text_hint)
        ref_color.setAlpha(130)
        painter.setPen(QPen(ref_color, 1.0, Qt.PenStyle.DotLine))
        painter.drawLine(QPointF(base_x0, ctx["ruler_bottom"] + 2.0), QPointF(base_x0, ctx["handle_top"] + ctx["handle_h"]))

        bx0 = base_x0 + shift_px
        bx1 = base_x1 + shift_px
        self._paint_lyric_block(painter, ctx, bx0, bx1, badge="1/2", selected=True, cells=5, label_text="整轨平移")

        lead_w = ctx["width"] * 0.13
        tail_w = ctx["width"] * 0.12
        self._paint_dashed_box(painter, ctx, bx0 - lead_w, bx0, emphasized=True, guide_x=bx0)
        self._paint_dashed_box(painter, ctx, bx1, bx1 + tail_w, emphasized=True, guide_x=bx1)

        if abs(shift_px) >= 4.0:
            direction_label = "整体提前" if wave < 0 else "整体延后"
            self._paint_dimension_arrow(
                painter,
                base_x0,
                bx0,
                ctx["handle_top"] + ctx["handle_h"] + 8.0,
                direction_label,
                accent,
            )
        else:
            painter.setFont(_ui_font(7.2))
            painter.setPen(QColor(p.text_secondary))
            painter.drawText(
                QRectF(ctx["left"], ctx["handle_top"] + ctx["handle_h"] + 3.0, ctx["width"], 16.0),
                Qt.AlignmentFlag.AlignCenter,
                "基准位置（0 ms）",
            )

        ms = int(round(wave * 500.0 / 50.0) * 50)
        sign = "+" if ms > 0 else ""
        return f"偏移 {sign}{ms} ms", abs(ms) > 0

    def _paint_section_ending(self, painter: QPainter, ctx: dict[str, Any]) -> tuple[str, bool]:
        p = ctx["palette"]
        dark = ctx["dark"]
        accent = QColor(p.accent_primary)
        # 0 = hold (keep into interlude), 1 = clear (cut cleanly at section end).
        t = _ping_pong_with_holds(self._phase, hold_low=0.22, rise=0.26, hold_high=0.28)

        # Interlude shaded region on the right.
        section_end_x = self._x(ctx, 0.52)
        next_sec_x = self._x(ctx, 0.90)
        interlude_bg = QColor(p.text_hint)
        interlude_bg.setAlpha(28 if not dark else 40)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(interlude_bg)
        painter.drawRect(
            QRectF(
                section_end_x,
                ctx["ruler_bottom"] + 1.0,
                next_sec_x - section_end_x,
                ctx["handle_top"] + ctx["handle_h"] - ctx["ruler_bottom"] + 1.0,
            )
        )
        painter.setFont(_ui_font(7.2))
        painter.setPen(QColor(p.text_hint))
        painter.drawText(
            QRectF(section_end_x, ctx["lane_top"], next_sec_x - section_end_x, ctx["lane_h"]),
            Qt.AlignmentFlag.AlignCenter,
            "间奏",
        )

        # Section-end divider line.
        cut_color = QColor("#D97706") if not dark else QColor("#FBBF24")
        painter.setPen(QPen(cut_color, 1.3, Qt.PenStyle.DashLine))
        painter.drawLine(
            QPointF(section_end_x, ctx["ruler_bottom"] + 2.0),
            QPointF(section_end_x, ctx["handle_top"] + ctx["handle_h"] + 2.0),
        )

        # Last line of current section + first line of next section.
        bx0 = self._x(ctx, 0.14)
        bx1 = self._x(ctx, 0.42)
        self._paint_lyric_block(painter, ctx, bx0, bx1, badge="段尾 · 1/2", selected=True, cells=4, label_text="段末句")
        self._paint_lyric_block(
            painter,
            ctx,
            next_sec_x,
            self._x(ctx, 0.99),
            badge="下段",
            hue=150 / 360,
            cells=2,
            dimmed=True,
        )

        self._paint_dashed_box(painter, ctx, bx0 - ctx["width"] * 0.08, bx0, emphasized=False)
        # Right dashed box: stretches to next section in "hold", shrinks to section_end in "clear".
        tail_x1 = _lerp(next_sec_x - 6.0, section_end_x, t)
        self._paint_dashed_box(painter, ctx, bx1, tail_x1, emphasized=True, guide_x=bx1)

        is_clear = t >= 0.5
        label = "段末即清屏，不拖进间奏" if is_clear else "保持挂屏延续到间奏"
        arrow_color = cut_color if is_clear else accent
        self._paint_dimension_arrow(
            painter,
            bx1,
            tail_x1,
            ctx["handle_top"] + ctx["handle_h"] + 8.0,
            label,
            arrow_color,
        )
        return ("段落结束：段末清屏" if is_clear else "段落结束：保持"), is_clear

    def _paint_lane_gap(self, painter: QPainter, ctx: dict[str, Any]) -> tuple[str, bool]:
        p = ctx["palette"]
        dark = ctx["dark"]
        accent = QColor(p.accent_primary)
        gap_color = QColor("#2563EB") if not dark else QColor("#60A5FA")
        t = _ping_pong_with_holds(self._phase)

        # Two blocks on the same visual row (1/2) across adjacent pages.
        p1_x0, p1_x1 = self._x(ctx, 0.06), self._x(ctx, 0.34)
        p2_x0, p2_x1 = self._x(ctx, 0.66), self._x(ctx, 0.94)
        self._paint_lyric_block(painter, ctx, p1_x0, p1_x1, badge="页1 · 1/2", selected=True, cells=4)
        self._paint_lyric_block(painter, ctx, p2_x0, p2_x1, badge="页2 · 1/2", selected=True, cells=4)

        mid = (p1_x1 + p2_x0) / 2.0
        half_gap = _lerp(ctx["width"] * 0.02, ctx["width"] * 0.095, t)
        p1_tail_end = mid - half_gap
        p2_lead_start = mid + half_gap

        self._paint_dashed_box(painter, ctx, p1_x1, p1_tail_end, emphasized=True, guide_x=p1_x1)
        self._paint_dashed_box(painter, ctx, p2_lead_start, p2_x0, emphasized=True, guide_x=p2_x0)

        # Highlight gap corridor between the two dashed boxes.
        gap_fill = QColor(gap_color)
        gap_fill.setAlpha(45)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(gap_fill)
        painter.drawRect(
            QRectF(
                p1_tail_end,
                ctx["handle_top"] - 2.0,
                max(2.0, p2_lead_start - p1_tail_end),
                ctx["handle_h"] + 4.0,
            )
        )

        self._paint_dimension_arrow(
            painter,
            p1_tail_end,
            p2_lead_start,
            ctx["handle_top"] + ctx["handle_h"] + 8.0,
            "同轨间隔",
            gap_color,
        )
        ms = int(round(_lerp(100, 600, t) / 50.0) * 50)
        return f"同轨间隔 {ms} ms", t > 0.15

    def _paint_protect(self, painter: QPainter, ctx: dict[str, Any]) -> tuple[str, bool]:
        p = ctx["palette"]
        dark = ctx["dark"]
        accent = QColor(p.accent_primary)
        shield_color = QColor("#D97706") if not dark else QColor("#FBBF24")
        t = _ping_pong_with_holds(self._phase, hold_low=0.14, rise=0.38, hold_high=0.26)

        # Page 1 fixed on left; Page 2 approaches to simulate tight spacing.
        p1_x0, p1_x1 = self._x(ctx, 0.05), self._x(ctx, 0.30)
        p2_x0 = _lerp(self._x(ctx, 0.72), self._x(ctx, 0.43), t)
        p2_w = ctx["width"] * 0.25
        p2_x1 = p2_x0 + p2_w

        self._paint_lyric_block(painter, ctx, p1_x0, p1_x1, badge="页1 · 1/2", selected=True, cells=4)
        self._paint_lyric_block(painter, ctx, p2_x0, p2_x1, badge="页2 · 1/2", selected=True, cells=4)

        protect_px = ctx["width"] * 0.068
        desired_tail = ctx["width"] * 0.16
        desired_lead = ctx["width"] * 0.16
        gap_px = 6.0

        # Resolve compression while respecting the protect floor on both sides.
        avail = max(0.0, (p2_x0 - p1_x1) - gap_px)
        tail_w = min(desired_tail, max(protect_px, avail * 0.5))
        lead_w = min(desired_lead, max(protect_px, avail - tail_w))
        clamped = (tail_w <= protect_px + 0.8) or (lead_w <= protect_px + 0.8)

        # Draw protected floor zones & walls.
        p1_floor_x = p1_x1 + protect_px
        p2_floor_x = p2_x0 - protect_px
        floor_fill = QColor(shield_color)
        floor_fill.setAlpha(46 if clamped else 24)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(floor_fill)
        painter.drawRect(QRectF(p1_x1, ctx["handle_top"], protect_px, ctx["handle_h"]))
        painter.drawRect(QRectF(p2_floor_x, ctx["handle_top"], protect_px, ctx["handle_h"]))

        self._paint_dashed_box(painter, ctx, p1_x1, p1_x1 + tail_w, emphasized=True, guide_x=p1_x1)
        self._paint_dashed_box(painter, ctx, p2_x0 - lead_w, p2_x0, emphasized=True, guide_x=p2_x0)

        self._paint_stop_wall(
            painter,
            p1_floor_x,
            ctx["handle_top"] - 3.0,
            ctx["handle_top"] + ctx["handle_h"] + 3.0,
            color=shield_color,
            active=clamped,
        )
        self._paint_stop_wall(
            painter,
            p2_floor_x,
            ctx["handle_top"] - 3.0,
            ctx["handle_top"] + ctx["handle_h"] + 3.0,
            color=shield_color,
            active=clamped,
        )

        # Render both protective arrows symmetrically with a single centered caption.
        arrow_color = shield_color if clamped else accent
        self._paint_dimension_arrow(
            painter,
            p1_x1,
            p1_floor_x,
            ctx["handle_top"] + ctx["handle_h"] + 6.0,
            "",
            arrow_color,
        )
        self._paint_dimension_arrow(
            painter,
            p2_floor_x,
            p2_x0,
            ctx["handle_top"] + ctx["handle_h"] + 6.0,
            "",
            arrow_color,
        )
        msg = "已达保护底线：两侧保底留住余量" if clamped else "跨页挤压时，两侧余量逐步压缩"
        font = _ui_font(7.2, bold=True)
        painter.setFont(font)
        painter.setPen(arrow_color)
        painter.drawText(
            QRectF(ctx["left"], ctx["handle_top"] + ctx["handle_h"] + 13.0, ctx["width"], 16.0),
            Qt.AlignmentFlag.AlignCenter,
            msg,
        )
        return ("保护底线生效" if clamped else "避让压缩中"), clamped

    def _paint_entry_anim_protect(self, painter: QPainter, ctx: dict[str, Any]) -> tuple[str, bool]:
        p = ctx["palette"]
        dark = ctx["dark"]
        anim_color = QColor("#0EA5E9") if not dark else QColor("#38BDF8")
        floor_color = QColor("#D97706") if not dark else QColor("#FBBF24")
        t = _ping_pong_with_holds(self._phase, hold_low=0.14, rise=0.38, hold_high=0.26)

        # Page 1 approaches from left; Page 2 sits on right so its left entry box is squeezed.
        p2_x0, p2_x1 = self._x(ctx, 0.68), self._x(ctx, 0.94)
        p1_x1 = _lerp(self._x(ctx, 0.24), self._x(ctx, 0.57), t)
        p1_x0 = p1_x1 - ctx["width"] * 0.20

        self._paint_lyric_block(painter, ctx, p1_x0, p1_x1, badge="页1 · 1/2", cells=3, dimmed=True)
        self._paint_lyric_block(painter, ctx, p2_x0, p2_x1, badge="页2 · 1/2", selected=True, cells=4)

        desired_lead = ctx["width"] * 0.34
        desired_anim = ctx["width"] * 0.16
        anim_floor = ctx["width"] * 0.095
        gap = 8.0

        # Available space for page 2's lead-in box (page 1 tail already compressed away).
        avail_lead = max(anim_floor, (p2_x0 - p1_x1) - gap)
        actual_lead = min(desired_lead, avail_lead)
        actual_anim = min(desired_anim, actual_lead)
        clamped = actual_lead <= anim_floor + 0.8

        lead_x0 = p2_x0 - actual_lead
        self._paint_dashed_box(painter, ctx, p1_x1, p1_x1 + 5.0, emphasized=False)
        self._paint_dashed_box(painter, ctx, lead_x0, p2_x0, emphasized=True, guide_x=p2_x0)

        # Entry animation band occupies the left edge of the lead-in dashed box.
        anim_rect = QRectF(lead_x0, ctx["handle_top"] + 1.0, actual_anim, ctx["handle_h"] - 2.0)
        self._paint_anim_band(painter, anim_rect, color=anim_color, locked=clamped)

        # Protect floor barrier measured from p2_x0 leftward.
        floor_x = p2_x0 - anim_floor
        self._paint_stop_wall(
            painter,
            floor_x,
            ctx["handle_top"] - 3.0,
            ctx["handle_top"] + ctx["handle_h"] + 3.0,
            color=floor_color,
            active=clamped,
        )

        label = "入场动画守住保护底线" if clamped else "先压等待余量 → 再加速入场动画"
        self._paint_dimension_arrow(
            painter,
            lead_x0,
            lead_x0 + actual_anim,
            ctx["handle_top"] + ctx["handle_h"] + 8.0,
            label,
            floor_color if clamped else anim_color,
        )
        return ("入场动画保护生效" if clamped else "压缩入场区间"), clamped

    def _paint_exit_anim_protect(self, painter: QPainter, ctx: dict[str, Any]) -> tuple[str, bool]:
        p = ctx["palette"]
        dark = ctx["dark"]
        anim_color = QColor("#8B5CF6") if not dark else QColor("#A78BFA")
        floor_color = QColor("#D97706") if not dark else QColor("#FBBF24")
        t = _ping_pong_with_holds(self._phase, hold_low=0.14, rise=0.38, hold_high=0.26)

        # Page 1 fixed on left; Page 2 approaches from right squeezing Page 1's right tail box.
        p1_x0, p1_x1 = self._x(ctx, 0.06), self._x(ctx, 0.32)
        p2_x0 = _lerp(self._x(ctx, 0.76), self._x(ctx, 0.43), t)
        p2_x1 = p2_x0 + ctx["width"] * 0.20

        self._paint_lyric_block(painter, ctx, p1_x0, p1_x1, badge="页1 · 1/2", selected=True, cells=4)
        self._paint_lyric_block(painter, ctx, p2_x0, p2_x1, badge="页2 · 1/2", cells=3, dimmed=True)

        desired_tail = ctx["width"] * 0.34
        desired_anim = ctx["width"] * 0.16
        anim_floor = ctx["width"] * 0.095
        gap = 8.0

        avail_tail = max(anim_floor, (p2_x0 - p1_x1) - gap)
        actual_tail = min(desired_tail, avail_tail)
        actual_anim = min(desired_anim, actual_tail)
        clamped = actual_tail <= anim_floor + 0.8

        tail_x1 = p1_x1 + actual_tail
        self._paint_dashed_box(painter, ctx, p1_x1, tail_x1, emphasized=True, guide_x=p1_x1)
        self._paint_dashed_box(painter, ctx, p2_x0 - 5.0, p2_x0, emphasized=False)

        # Exit animation band sits on the right edge of the tail dashed box.
        anim_rect = QRectF(tail_x1 - actual_anim, ctx["handle_top"] + 1.0, actual_anim, ctx["handle_h"] - 2.0)
        self._paint_anim_band(painter, anim_rect, color=anim_color, locked=clamped)

        # Protect floor barrier measured from p1_x1 rightward.
        floor_x = p1_x1 + anim_floor
        self._paint_stop_wall(
            painter,
            floor_x,
            ctx["handle_top"] - 3.0,
            ctx["handle_top"] + ctx["handle_h"] + 3.0,
            color=floor_color,
            active=clamped,
        )

        label = "退场动画守住保护底线" if clamped else "先压停留余量 → 再加速退场动画"
        self._paint_dimension_arrow(
            painter,
            tail_x1 - actual_anim,
            tail_x1,
            ctx["handle_top"] + ctx["handle_h"] + 8.0,
            label,
            floor_color if clamped else anim_color,
        )
        return ("退场动画保护生效" if clamped else "压缩退场区间"), clamped

    # -------------------------------------------------------- checkbox scenes

    def _paint_sync_entry(self, painter: QPainter, ctx: dict[str, Any]) -> tuple[str, bool]:
        p = ctx["palette"]
        accent = QColor(p.accent_primary)
        t = _ping_pong_with_holds(self._phase, hold_low=0.18, rise=0.32, hold_high=0.28)

        # Two lines on the same page: 1/2 (blue, earlier) and 2/2 (green, later).
        b1_x0, b1_x1 = self._x(ctx, 0.24), self._x(ctx, 0.52)
        b2_x0, b2_x1 = self._x(ctx, 0.56), self._x(ctx, 0.88)
        self._paint_lyric_block(painter, ctx, b1_x0, b1_x1, badge="1/2", hue=210 / 360, cells=4)
        self._paint_lyric_block(painter, ctx, b2_x0, b2_x1, badge="2/2", hue=150 / 360, selected=True, cells=4)

        row1_y, row2_y, rh = 86.0, 99.0, 10.0
        lead1_x0 = self._x(ctx, 0.08)
        # Row 2's entry box starts near b2 when off, and extends left to align with lead1_x0 when on.
        lead2_x0 = _lerp(self._x(ctx, 0.42), lead1_x0, t)

        self._paint_dashed_box(painter, ctx, lead1_x0, b1_x0, emphasized=False, guide_x=b1_x0, y_override=row1_y, h_override=rh)
        self._paint_dashed_box(painter, ctx, lead2_x0, b2_x0, emphasized=True, guide_x=b2_x0, y_override=row2_y, h_override=rh)

        aligned = t >= 0.85
        # Vertical alignment reference at earliest page show time.
        line_color = accent if aligned else QColor(p.text_hint)
        painter.setPen(QPen(line_color, 1.5 if aligned else 1.0, Qt.PenStyle.DashLine))
        painter.drawLine(QPointF(lead1_x0, ctx["ruler_bottom"] + 2.0), QPointF(lead1_x0, row2_y + rh + 2.0))

        self._paint_dimension_arrow(
            painter,
            lead2_x0,
            b2_x0,
            row2_y + rh + 9.0,
            "2/2 对齐到 1/2 同步上屏" if aligned else "2/2 提前入场向左拉齐",
            accent if aligned else QColor(p.text_secondary),
        )
        return ("同步入场：开启" if t >= 0.5 else "同步入场：关闭"), t >= 0.5

    def _paint_sync_ending(self, painter: QPainter, ctx: dict[str, Any]) -> tuple[str, bool]:
        p = ctx["palette"]
        accent = QColor(p.accent_primary)
        t = _ping_pong_with_holds(self._phase, hold_low=0.18, rise=0.32, hold_high=0.28)

        b1_x0, b1_x1 = self._x(ctx, 0.10), self._x(ctx, 0.40)
        b2_x0, b2_x1 = self._x(ctx, 0.44), self._x(ctx, 0.74)
        self._paint_lyric_block(painter, ctx, b1_x0, b1_x1, badge="1/2", hue=210 / 360, selected=True, cells=4)
        self._paint_lyric_block(painter, ctx, b2_x0, b2_x1, badge="2/2", hue=150 / 360, cells=4)

        row1_y, row2_y, rh = 86.0, 99.0, 10.0
        tail2_x1 = self._x(ctx, 0.90)
        # Row 1's exit box ends shortly after b1 when off, and extends right to tail2_x1 when on.
        tail1_x1 = _lerp(self._x(ctx, 0.54), tail2_x1, t)

        self._paint_dashed_box(painter, ctx, b1_x1, tail1_x1, emphasized=True, guide_x=b1_x1, y_override=row1_y, h_override=rh)
        self._paint_dashed_box(painter, ctx, b2_x1, tail2_x1, emphasized=False, guide_x=b2_x1, y_override=row2_y, h_override=rh)

        aligned = t >= 0.85
        line_color = accent if aligned else QColor(p.text_hint)
        painter.setPen(QPen(line_color, 1.5 if aligned else 1.0, Qt.PenStyle.DashLine))
        painter.drawLine(QPointF(tail2_x1, ctx["ruler_bottom"] + 2.0), QPointF(tail2_x1, row2_y + rh + 2.0))

        self._paint_dimension_arrow(
            painter,
            b1_x1,
            tail1_x1,
            row2_y + rh + 9.0,
            "1/2 延长到 2/2 同步退场" if aligned else "1/2 退场向右拉齐",
            accent if aligned else QColor(p.text_secondary),
        )
        return ("同步退场：开启" if t >= 0.5 else "同步退场：关闭"), t >= 0.5

    def _paint_sync_each_page(self, painter: QPainter, ctx: dict[str, Any]) -> tuple[str, bool]:
        p = ctx["palette"]
        accent = QColor(p.accent_primary)
        t = _ping_pong_with_holds(self._phase, hold_low=0.22, rise=0.26, hold_high=0.28)
        each_on = t >= 0.5

        # Three pages within a section: head page, middle page, tail page.
        pages = (
            ("段首页", 0.05, 0.16, 0.18, 0.28),
            ("中间页", 0.38, 0.49, 0.51, 0.61),
            ("段尾页", 0.71, 0.82, 0.84, 0.94),
        )
        row1_y, row2_y, rh = 86.0, 99.0, 9.0
        for idx, (tag, a0, a1, b0, b1) in enumerate(pages):
            ax0, ax1 = self._x(ctx, a0), self._x(ctx, a1)
            bx0, bx1 = self._x(ctx, b0), self._x(ctx, b1)
            highlight_mid = idx == 1 and each_on
            self._paint_lyric_block(
                painter, ctx, ax0, ax1, badge=tag, hue=210 / 360, cells=2, selected=highlight_mid
            )
            self._paint_lyric_block(
                painter, ctx, bx0, bx1, badge="", hue=150 / 360, cells=2, selected=highlight_mid
            )

            # Entry sync applies to head page always, and to every page when enabled.
            entry_sync = 1.0 if idx == 0 else t
            exit_sync = 1.0 if idx == 2 else t

            entry_anchor = ax0 - ctx["width"] * 0.04
            exit_anchor = bx1 + ctx["width"] * 0.04
            b_entry_x0 = _lerp(bx0 - ctx["width"] * 0.035, entry_anchor, entry_sync)
            a_exit_x1 = _lerp(ax1 + ctx["width"] * 0.035, exit_anchor, exit_sync)

            # Row 1 entry/exit + Row 2 entry/exit
            self._paint_dashed_box(painter, ctx, entry_anchor, ax0, emphasized=(idx == 0 or each_on), y_override=row1_y, h_override=rh)
            self._paint_dashed_box(painter, ctx, ax1, a_exit_x1, emphasized=(idx == 2 or each_on), y_override=row1_y, h_override=rh)
            self._paint_dashed_box(painter, ctx, b_entry_x0, bx0, emphasized=(idx == 0 or each_on), y_override=row2_y, h_override=rh)
            self._paint_dashed_box(painter, ctx, bx1, exit_anchor, emphasized=(idx == 2 or each_on), y_override=row2_y, h_override=rh)

            # Draw vertical sync guides where active.
            if entry_sync >= 0.8:
                painter.setPen(QPen(accent, 1.3, Qt.PenStyle.DashLine))
                painter.drawLine(QPointF(entry_anchor, ctx["lane_top"]), QPointF(entry_anchor, row2_y + rh + 2.0))
            if exit_sync >= 0.8:
                painter.setPen(QPen(accent, 1.3, Qt.PenStyle.DashLine))
                painter.drawLine(QPointF(exit_anchor, ctx["lane_top"]), QPointF(exit_anchor, row2_y + rh + 2.0))

        font = _ui_font(7.3, bold=True)
        painter.setFont(font)
        painter.setPen(accent if each_on else QColor(p.text_secondary))
        caption = "开启：段内每一页都执行同步入/退场" if each_on else "关闭：仅段首页同步入场、段尾页同步退场"
        painter.drawText(
            QRectF(ctx["left"], row2_y + rh + 7.0, ctx["width"], 18.0),
            Qt.AlignmentFlag.AlignCenter,
            caption,
        )
        return ("每句同步：开启" if each_on else "每句同步：关闭"), each_on

    def _paint_ruby_split_mode(self, painter: QPainter, ctx: dict[str, Any]) -> tuple[str, bool]:
        """Visual contrast: split main text by ruby time segments vs. by ruby visible characters."""
        p = ctx["palette"]
        dark = ctx["dark"]
        accent = QColor(p.accent_primary)
        blue = QColor("#2563EB") if not dark else QColor("#60A5FA")
        t = _ping_pong_with_holds(self._phase, hold_low=0.22, rise=0.26, hold_high=0.28)
        n3_mode = t >= 0.5

        left = ctx["left"] + 46.0
        right = ctx["right"] - 8.0
        span = right - left

        # Row labels on left.
        label_font = _ui_font(7.5, bold=True)
        painter.setFont(label_font)
        painter.setPen(QColor(p.text_secondary))
        painter.drawText(QRectF(ctx["left"], 20.0, 42.0, 26.0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, "注音")
        painter.drawText(QRectF(ctx["left"], 62.0, 42.0, 34.0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, "正文")

        # Top bar: 3 ruby characters (あ, い, う) grouped into 2 timing checkpoints: [あ い] (68%) + [う] (32%).
        ruby_top, ruby_h = 20.0, 26.0
        seg_split_x = left + span * 0.68
        char_mid_x = left + span * 0.50

        ruby_fill = QColor.fromHsvF(150 / 360, 0.22 if not dark else 0.35, 0.96 if not dark else 0.34)
        ruby_border = QColor.fromHsvF(150 / 360, 0.55, 0.70 if not dark else 0.78)
        painter.setPen(QPen(ruby_border, 1.1))
        painter.setBrush(ruby_fill)
        painter.drawRoundedRect(QRectF(left, ruby_top, span, ruby_h), 3.0, 3.0)

        # Checkpoint divider between segment 1 ("あ い") and segment 2 ("う").
        painter.setPen(QPen(ruby_border, 1.5))
        painter.drawLine(QPointF(seg_split_x, ruby_top), QPointF(seg_split_x, ruby_top + ruby_h))

        # Faint character divider inside segment 1 ("あ" | "い").
        inner_x = left + (seg_split_x - left) * 0.5
        painter.setPen(QPen(ruby_border, 1.0, Qt.PenStyle.DotLine))
        painter.drawLine(QPointF(inner_x, ruby_top + 2.0), QPointF(inner_x, ruby_top + ruby_h - 2.0))

        painter.setFont(_ui_font(8.2, bold=True))
        text_col = QColor("#E5E7EB" if dark else "#1F2937")
        painter.setPen(text_col)
        painter.drawText(QRectF(left, ruby_top, inner_x - left, ruby_h), Qt.AlignmentFlag.AlignCenter, "あ")
        painter.drawText(QRectF(inner_x, ruby_top, seg_split_x - inner_x, ruby_h), Qt.AlignmentFlag.AlignCenter, "い")
        painter.drawText(QRectF(seg_split_x, ruby_top, right - seg_split_x, ruby_h), Qt.AlignmentFlag.AlignCenter, "う")

        # Small checkpoint badges above ruby bar.
        painter.setFont(_ui_font(6.8))
        painter.setPen(QColor(p.text_hint))
        painter.drawText(QRectF(left, 5.0, seg_split_x - left, 14.0), Qt.AlignmentFlag.AlignCenter, "时间段 1（2 字）")
        painter.drawText(QRectF(seg_split_x, 5.0, right - seg_split_x, 14.0), Qt.AlignmentFlag.AlignCenter, "时间段 2（1 字）")

        # Bottom bar: 2 main kanji characters ("漢", "字") with animated split position.
        main_top, main_h = 62.0, 34.0
        split_x = _lerp(seg_split_x, char_mid_x, t)

        main_fill = QColor.fromHsvF(210 / 360, 0.20 if not dark else 0.34, 0.96 if not dark else 0.34)
        main_border = QColor.fromHsvF(210 / 360, 0.55, 0.72 if not dark else 0.80)
        painter.setPen(QPen(main_border, 1.2))
        painter.setBrush(main_fill)
        painter.drawRoundedRect(QRectF(left, main_top, span, main_h), 3.0, 3.0)

        # Highlight animated split boundary on main text.
        split_color = accent if n3_mode else blue
        painter.setPen(QPen(split_color, 2.0))
        painter.drawLine(QPointF(split_x, main_top), QPointF(split_x, main_top + main_h))
        # Projection dashed line connecting ruby reference down to main split.
        painter.setPen(QPen(split_color, 1.2, Qt.PenStyle.DashLine))
        painter.drawLine(QPointF(split_x, ruby_top + ruby_h), QPointF(split_x, main_top))

        painter.setFont(_ui_font(9.5, bold=True))
        painter.setPen(text_col)
        painter.drawText(QRectF(left, main_top, split_x - left, main_h), Qt.AlignmentFlag.AlignCenter, "漢")
        painter.drawText(QRectF(split_x, main_top, right - split_x, main_h), Qt.AlignmentFlag.AlignCenter, "字")

        # Bottom explanation line.
        painter.setFont(_ui_font(7.3, bold=True))
        painter.setPen(split_color)
        caption = (
            "开启（N3 式）：按 3 个注音字符比例 1.5 : 1.5 切分正文"
            if n3_mode
            else "关闭：按注音的 2 个时间段直接 1 : 1 切分正文"
        )
        painter.drawText(
            QRectF(ctx["left"], 108.0, ctx["width"], 22.0),
            Qt.AlignmentFlag.AlignCenter,
            caption,
        )
        return ("N3 按字符切分" if n3_mode else "按时间段切分"), n3_mode

    def _paint_anim_overlap(self, painter: QPainter, ctx: dict[str, Any]) -> tuple[str, bool]:
        p = ctx["palette"]
        dark = ctx["dark"]
        exit_col = QColor("#8B5CF6") if not dark else QColor("#A78BFA")
        entry_col = QColor("#0EA5E9") if not dark else QColor("#38BDF8")
        accent = QColor(p.accent_primary)
        t = _ping_pong_with_holds(self._phase, hold_low=0.20, rise=0.30, hold_high=0.28)
        allow = t >= 0.5

        p1_x0, p1_x1 = self._x(ctx, 0.05), self._x(ctx, 0.34)
        p2_x0, p2_x1 = self._x(ctx, 0.66), self._x(ctx, 0.95)
        self._paint_lyric_block(painter, ctx, p1_x0, p1_x1, badge="页1 · 1/2", selected=True, cells=4)
        self._paint_lyric_block(painter, ctx, p2_x0, p2_x1, badge="页2 · 1/2", selected=True, cells=4)

        row1_y, row2_y, rh = 86.0, 99.0, 10.0
        anim_w = ctx["width"] * 0.11

        # Closed: animations stay separated by gap around center.
        # Open: exit animation extends right and entry animation extends left so they cross over.
        mid = self._x(ctx, 0.50)
        p1_end = _lerp(mid - 6.0, mid + ctx["width"] * 0.05, t)
        p2_start = _lerp(mid + 6.0, mid - ctx["width"] * 0.05, t)

        self._paint_dashed_box(painter, ctx, p1_x1, p1_end, emphasized=True, guide_x=p1_x1, y_override=row1_y, h_override=rh)
        self._paint_dashed_box(painter, ctx, p2_start, p2_x0, emphasized=True, guide_x=p2_x0, y_override=row2_y, h_override=rh)

        self._paint_anim_band(painter, QRectF(p1_end - anim_w, row1_y + 1.0, anim_w, rh - 2.0), color=exit_col, locked=allow)
        self._paint_anim_band(painter, QRectF(p2_start, row2_y + 1.0, anim_w, rh - 2.0), color=entry_col, locked=allow)

        if p1_end > p2_start + 1.0:
            # Highlight the overlap zone where exit & entry animations cross.
            ov_fill = QColor(accent)
            ov_fill.setAlpha(38)
            painter.setPen(QPen(accent, 1.0, Qt.PenStyle.DashLine))
            painter.setBrush(ov_fill)
            painter.drawRect(QRectF(p2_start, row1_y - 2.0, p1_end - p2_start, (row2_y + rh) - row1_y + 4.0))
            self._paint_dimension_arrow(
                painter,
                p2_start,
                p1_end,
                row2_y + rh + 9.0,
                "退场动画与入场动画可交叉重叠",
                accent,
            )
        else:
            self._paint_dimension_arrow(
                painter,
                p1_end,
                p2_start,
                row2_y + rh + 9.0,
                "关闭：动画区间也必须保留间隔",
                QColor(p.text_secondary),
            )
        return ("允许动画重叠：开启" if allow else "允许动画重叠：关闭"), allow

    def _paint_auto_fill(self, painter: QPainter, ctx: dict[str, Any]) -> tuple[str, bool]:
        p = ctx["palette"]
        accent = QColor(p.accent_primary)
        t = _ping_pong_with_holds(self._phase, hold_low=0.18, rise=0.32, hold_high=0.28)
        fill_on = t >= 0.5

        p1_x0, p1_x1 = self._x(ctx, 0.05), self._x(ctx, 0.28)
        p2_x0, p2_x1 = self._x(ctx, 0.72), self._x(ctx, 0.95)
        self._paint_lyric_block(painter, ctx, p1_x0, p1_x1, badge="页1 · 1/2", selected=True, cells=3)
        self._paint_lyric_block(painter, ctx, p2_x0, p2_x1, badge="页2 · 1/2", cells=3)

        p2_lead_x0 = self._x(ctx, 0.60)
        gap_px = 10.0
        # Off: page 1 only keeps its short tail; On: stretches up to page 2 lead-in minus lane gap.
        p1_tail_x1 = _lerp(self._x(ctx, 0.37), p2_lead_x0 - gap_px, t)

        self._paint_dashed_box(painter, ctx, p1_x1, p1_tail_x1, emphasized=True, guide_x=p1_x1)
        self._paint_dashed_box(painter, ctx, p2_lead_x0, p2_x0, emphasized=False, guide_x=p2_x0)

        self._paint_dimension_arrow(
            painter,
            p1_x1,
            p1_tail_x1,
            ctx["handle_top"] + ctx["handle_h"] + 8.0,
            "自动延长挂到下一页入场前" if fill_on else "关闭：仅保留自身退场窗口",
            accent if fill_on else QColor(p.text_secondary),
        )
        return ("自动填充：开启" if fill_on else "自动填充：关闭"), fill_on

    def _paint_inter_page_overlap(self, painter: QPainter, ctx: dict[str, Any]) -> tuple[str, bool]:
        p = ctx["palette"]
        accent = QColor(p.accent_primary)
        t = _ping_pong_with_holds(self._phase, hold_low=0.20, rise=0.30, hold_high=0.28)
        overlap_on = t >= 0.5

        # Tightly spaced pages on the same visual row.
        p1_x0, p1_x1 = self._x(ctx, 0.06), self._x(ctx, 0.44)
        p2_x0, p2_x1 = self._x(ctx, 0.54), self._x(ctx, 0.94)
        self._paint_lyric_block(painter, ctx, p1_x0, p1_x1, badge="页1 · 1/2", selected=True, cells=4)
        self._paint_lyric_block(painter, ctx, p2_x0, p2_x1, badge="页2 · 1/2", selected=True, cells=4)

        row1_y, row2_y, rh = 86.0, 99.0, 10.0
        mid = (p1_x1 + p2_x0) / 2.0
        # Off: auto-compressed so no overlap; On: full tail & full lead-in freely overlap.
        p1_end = _lerp(mid - 3.0, self._x(ctx, 0.66), t)
        p2_start = _lerp(mid + 3.0, self._x(ctx, 0.34), t)

        self._paint_dashed_box(painter, ctx, p1_x1, p1_end, emphasized=True, guide_x=p1_x1, y_override=row1_y, h_override=rh)
        self._paint_dashed_box(painter, ctx, p2_start, p2_x0, emphasized=True, guide_x=p2_x0, y_override=row2_y, h_override=rh)

        if p1_end > p2_start + 1.0:
            ov = QColor(accent)
            ov.setAlpha(40)
            painter.setPen(QPen(accent, 1.0, Qt.PenStyle.DashLine))
            painter.setBrush(ov)
            painter.drawRect(QRectF(p2_start, row1_y - 2.0, p1_end - p2_start, (row2_y + rh) - row1_y + 4.0))
            self._paint_dimension_arrow(
                painter,
                p2_start,
                p1_end,
                row2_y + rh + 9.0,
                "不执行跨页避让，允许直接叠放",
                accent,
            )
        else:
            font = _ui_font(7.2, bold=True)
            painter.setFont(font)
            painter.setPen(QColor(p.text_secondary))
            painter.drawText(
                QRectF(ctx["left"], row2_y + rh + 6.0, ctx["width"], 16.0),
                Qt.AlignmentFlag.AlignCenter,
                "关闭：自动压缩避让，跨页不重叠",
            )
        return ("行间重叠：开启" if overlap_on else "行间重叠：关闭（自动避让）"), overlap_on

    def _paint_overlap_fallback(self, painter: QPainter, ctx: dict[str, Any]) -> tuple[str, bool]:
        """Split view: mini timeline on left + mini screen preview on right comparing displace vs. lift."""
        p = ctx["palette"]
        dark = ctx["dark"]
        accent = QColor(p.accent_primary)
        amber = QColor("#D97706") if not dark else QColor("#FBBF24")
        t = _ping_pong_with_holds(self._phase, hold_low=0.22, rise=0.26, hold_high=0.28)
        is_lift = t >= 0.5

        # Left side: mini overlapping timeline where page 2 starts while page 1 is still singing.
        tl_left = ctx["left"]
        tl_w = 198.0
        tl_right = tl_left + tl_w
        painter.setFont(_ui_font(7.2, bold=True))
        painter.setPen(QColor(p.text_secondary))
        painter.drawText(QRectF(tl_left, 8.0, tl_w, 16.0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, "残余冲突时刻")

        # Two overlapping track bars.
        bar1 = QRectF(tl_left + 4.0, 32.0, 126.0, 22.0)
        bar2 = QRectF(tl_left + 72.0, 62.0, 122.0, 22.0)
        conflict_x = bar2.left()

        # Page 1 bar: when displacing, its tail after conflict_x is dimmed/cut.
        blue_fill = QColor.fromHsvF(210 / 360, 0.22 if not dark else 0.36, 0.96 if not dark else 0.34)
        blue_border = QColor.fromHsvF(210 / 360, 0.55, 0.72 if not dark else 0.80)
        green_fill = QColor.fromHsvF(150 / 360, 0.22 if not dark else 0.36, 0.96 if not dark else 0.34)
        green_border = QColor.fromHsvF(150 / 360, 0.55, 0.70 if not dark else 0.78)

        painter.setPen(QPen(blue_border, 1.1))
        painter.setBrush(blue_fill)
        if not is_lift:
            # Active portion before takeover + faded eaten portion.
            active_w = max(12.0, conflict_x - bar1.left())
            painter.drawRoundedRect(QRectF(bar1.left(), bar1.top(), active_w, bar1.height()), 3.0, 3.0)
            eaten = QRectF(conflict_x, bar1.top(), bar1.right() - conflict_x, bar1.height())
            painter.setPen(QPen(accent, 1.1, Qt.PenStyle.DashLine))
            eaten_fill = QColor(accent)
            eaten_fill.setAlpha(36)
            painter.setBrush(eaten_fill)
            painter.drawRect(eaten)
        else:
            painter.drawRoundedRect(bar1, 3.0, 3.0)

        painter.setPen(QPen(green_border, 1.2))
        painter.setBrush(green_fill)
        painter.drawRoundedRect(bar2, 3.0, 3.0)

        text_col = QColor("#E5E7EB" if dark else "#1F2937")
        painter.setFont(_ui_font(7.4, bold=True))
        painter.setPen(text_col)
        painter.drawText(QRectF(bar1.left() + 6.0, bar1.top(), 62.0, bar1.height()), Qt.AlignmentFlag.AlignVCenter, "页1 走字")
        painter.drawText(QRectF(bar2.left() + 6.0, bar2.top(), 90.0, bar2.height()), Qt.AlignmentFlag.AlignVCenter, "页2 提前上屏")

        # Vertical playhead at conflict instant.
        playhead_x = conflict_x + 22.0
        painter.setPen(QPen(accent, 1.4))
        painter.drawLine(QPointF(playhead_x, 24.0), QPointF(playhead_x, 92.0))

        # Right side: mini dark screen preview showing actual subtitle placement.
        scr_x = tl_right + 14.0
        scr_w = ctx["right"] - scr_x
        scr = QRectF(scr_x, 22.0, scr_w, 72.0)
        painter.setFont(_ui_font(7.2, bold=True))
        painter.setPen(QColor(p.text_secondary))
        painter.drawText(QRectF(scr_x, 8.0, scr_w, 14.0), Qt.AlignmentFlag.AlignCenter, "画面效果")

        painter.setPen(QPen(QColor(p.card_border), 1.0))
        painter.setBrush(QColor("#11141B"))
        painter.drawRoundedRect(scr, 5.0, 5.0)

        base_row_y = scr.bottom() - 22.0
        lifted_row_y = scr.bottom() - 44.0
        line_w = scr.width() - 22.0
        line_x = scr.left() + 11.0

        # In displace mode (t=0): page 1 fades out at base_row_y while page 2 replaces it in place.
        # In lift mode (t=1): page 1 stays visible at base_row_y and page 2 lifts up to lifted_row_y.
        p1_alpha = int(_lerp(55, 235, t))
        p1_col = QColor("#60A5FA")
        p1_col.setAlpha(p1_alpha)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(p1_col)
        painter.drawRoundedRect(QRectF(line_x, base_row_y, line_w * 0.85, 11.0), 3.0, 3.0)

        p2_y = _lerp(base_row_y, lifted_row_y, t)
        p2_col = QColor("#34D399" if is_lift else "#FF5A6F")
        painter.setBrush(p2_col)
        painter.drawRoundedRect(QRectF(line_x + 8.0, p2_y, line_w * 0.85, 11.0), 3.0, 3.0)

        if is_lift:
            # Upward lift arrow inside screen.
            ax = scr.left() + 6.0
            painter.setPen(QPen(amber, 1.3))
            painter.drawLine(QPointF(ax, base_row_y + 4.0), QPointF(ax, lifted_row_y + 4.0))
            painter.setBrush(amber)
            painter.drawPolygon(
                QPolygonF([QPointF(ax, lifted_row_y + 1.0), QPointF(ax - 2.6, lifted_row_y + 6.0), QPointF(ax + 2.6, lifted_row_y + 6.0)])
            )

        painter.setFont(_ui_font(7.3, bold=True))
        painter.setPen(amber if is_lift else accent)
        caption = (
            "抬升避让：页1照常走完，页2整页上移避让"
            if is_lift
            else "吃掉走字时长：页2原地顶掉页1（页1提前退场）"
        )
        painter.drawText(
            QRectF(ctx["left"], 108.0, ctx["width"], 22.0),
            Qt.AlignmentFlag.AlignCenter,
            caption,
        )
        return ("抬升避让" if is_lift else "吃掉走字时长"), is_lift


class TimingHelpPopover(QFrame):
    """Floating theme-aware card displaying the live animation and caption."""

    def __init__(self, key: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(
            parent,
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.NoDropShadowWindowHint,
        )
        self.setObjectName("TimingHelpPopover")
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._spec = TIMING_HELP_SPECS[key]
        self._hide_callback: Optional[Any] = None
        self._visibility_callback: Optional[Any] = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 9, 10, 9)
        layout.setSpacing(6)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(6)
        self._title_label = QLabel(self._spec.title, self)
        self._title_label.setObjectName("TimingHelpTitle")
        header.addWidget(self._title_label)
        header.addStretch(1)
        self._status_label = QLabel(" ", self)
        self._status_label.setObjectName("TimingHelpStatus")
        self._status_label.setFixedHeight(20)
        self._status_highlighted = False
        header.addWidget(self._status_label)
        layout.addLayout(header)

        self._canvas = TimingHelpCanvas(
            key,
            self,
            on_status_change=self._update_status_badge,
        )
        layout.addWidget(self._canvas)

        self._summary_label = QLabel(self._spec.summary, self)
        self._summary_label.setObjectName("TimingHelpSummary")
        self._summary_label.setWordWrap(True)
        self._summary_label.setFixedWidth(self._canvas.width())
        layout.addWidget(self._summary_label)

        themed(self, self._build_qss)
        self.adjustSize()

    def _build_qss(self) -> str:
        p = palette()
        dark = bool(getattr(p, "is_dark", False))
        if self._status_highlighted:
            accent = QColor(p.accent_primary)
            alpha = 32 if not dark else 52
            pill_bg = f"rgba({accent.red()}, {accent.green()}, {accent.blue()}, {alpha})"
            pill_border = p.accent_primary
            pill_fg = p.accent_primary if not dark else "#FF8A9A"
        else:
            pill_bg = p.preview_bg
            pill_border = p.card_border
            pill_fg = p.text_secondary
        return (
            f"""
            QFrame#TimingHelpPopover {{
                background: transparent;
                border: none;
            }}
            QLabel#TimingHelpTitle {{
                color: {p.title_text};
                font-size: 9.5pt;
                font-weight: 700;
                background: transparent;
            }}
            QLabel#TimingHelpStatus {{
                color: {pill_fg};
                background: {pill_bg};
                border: 1px solid {pill_border};
                border-radius: 9px;
                padding: 1px 8px;
                font-size: 8pt;
                font-weight: 600;
            }}
            QLabel#TimingHelpSummary {{
                color: {p.text_secondary};
                font-size: 8.5pt;
                background: transparent;
            }}
            """
        )

    def _update_status_badge(self, text: str, highlighted: bool) -> None:
        self._status_label.setText(text)
        if highlighted != self._status_highlighted:
            self._status_highlighted = highlighted
            self.setStyleSheet(self._build_qss())

    def paintEvent(self, event: Any) -> None:  # noqa: N802
        p = palette()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Crisp flush rounded card without any outer shadow halo frame.
        card = QRectF(0.5, 0.5, self.width() - 1.0, self.height() - 1.0)
        painter.setPen(QPen(QColor(p.input_border_hover), 1.0))
        painter.setBrush(QColor(p.card_bg))
        painter.drawRoundedRect(card, 10.0, 10.0)
        painter.end()

    @property
    def canvas(self) -> TimingHelpCanvas:
        return self._canvas

    def show_for_anchor(self, anchor: QWidget) -> None:
        self.adjustSize()
        pos = self._compute_position(anchor)
        self.move(pos)
        self.show()
        self._canvas.start_animation()

    def _compute_position(self, anchor: QWidget) -> QPoint:
        anchor_top_left = anchor.mapToGlobal(QPoint(0, 0))
        anchor_center_x = anchor_top_left.x() + anchor.width() // 2
        pop_w = self.width() or self.sizeHint().width()
        pop_h = self.height() or self.sizeHint().height()
        margin = 8

        screen = QApplication.screenAt(anchor_top_left) or QApplication.primaryScreen()
        if screen is not None:
            avail = screen.availableGeometry()
        else:
            avail = QPoint(0, 0)
            return QPoint(anchor_center_x - pop_w // 2, anchor_top_left.y() + anchor.height() + margin)

        # Prefer above the badge so it does not cover the input box directly underneath.
        x = anchor_center_x - pop_w // 2
        x = max(avail.left() + 8, min(avail.right() - pop_w - 8, x))
        above_y = anchor_top_left.y() - pop_h - margin
        below_y = anchor_top_left.y() + anchor.height() + margin
        if above_y >= avail.top() + 8:
            y = above_y
        elif below_y + pop_h <= avail.bottom() - 8:
            y = below_y
        else:
            y = max(avail.top() + 8, min(avail.bottom() - pop_h - 8, above_y))
        return QPoint(x, y)

    def hideEvent(self, event: Any) -> None:  # noqa: N802
        self._canvas.stop_animation()
        if self._visibility_callback is not None:
            self._visibility_callback()
        super().hideEvent(event)

    def enterEvent(self, event: Any) -> None:  # noqa: N802
        if self._hide_callback is not None:
            self._hide_callback(False)
        super().enterEvent(event)

    def leaveEvent(self, event: Any) -> None:  # noqa: N802
        if self._hide_callback is not None:
            self._hide_callback(True)
        super().leaveEvent(event)


class TimingHelpButton(QToolButton):
    """Compact ``?`` icon next to a field label that reveals an animated popover."""

    _SHOW_DELAY_MS = 140
    _HIDE_DELAY_MS = 120

    def __init__(self, key: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        if key not in TIMING_HELP_SPECS:
            raise KeyError(f"Unknown timing help key: {key}")
        self._key = key
        self._spec = TIMING_HELP_SPECS[key]
        self._popover: Optional[TimingHelpPopover] = None
        self._hovered = False

        self.setObjectName(f"TimingHelpButton_{key}")
        self.setAccessibleName(f"{self._spec.title}说明")
        self.setFixedSize(16, 16)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.WhatsThisCursor)
        self.setMouseTracking(True)

        self._show_timer = QTimer(self)
        self._show_timer.setSingleShot(True)
        self._show_timer.setInterval(self._SHOW_DELAY_MS)
        self._show_timer.timeout.connect(self.show_popover)

        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.setInterval(self._HIDE_DELAY_MS)
        self._hide_timer.timeout.connect(self._hide_if_unhovered)

        self.clicked.connect(self._toggle_popover)

    @property
    def help_key(self) -> str:
        return self._key

    def popover(self) -> Optional[TimingHelpPopover]:
        if self._popover is not None and sip.isdeleted(self._popover):
            self._popover = None
        return self._popover

    def _ensure_popover(self) -> TimingHelpPopover:
        pop = self.popover()
        if pop is None:
            # Parentless top-level tooltip keeps host QWidget/QFrame QSS from
            # cascading onto the popup and painting an extra outer frame
            # (same pattern as krok_helper.qfluent_compat).
            pop = TimingHelpPopover(self._key, None)
            pop._hide_callback = self._on_popover_hover_change
            pop._visibility_callback = self._on_popover_hidden
            self._popover = pop

            def _cleanup_popover(_obj: Any = None, target: TimingHelpPopover = pop) -> None:
                if not sip.isdeleted(target):
                    target.hide()
                    target.close()
                    target.deleteLater()

            self.destroyed.connect(_cleanup_popover)
        return pop

    def _is_cursor_inside(self, widget: Optional[QWidget]) -> bool:
        if widget is None or sip.isdeleted(widget) or not widget.isVisible():
            return False
        local_pos = widget.mapFromGlobal(QCursor.pos())
        return widget.rect().contains(local_pos)

    def _is_highlighted(self) -> bool:
        pop = self.popover()
        return self._hovered or (pop is not None and pop.isVisible())

    def _on_popover_hidden(self) -> None:
        self._hovered = self._is_cursor_inside(self)
        self.update()

    def show_popover(self) -> None:
        self._show_timer.stop()
        self._hide_timer.stop()
        if not self.isVisible():
            return
        pop = self._ensure_popover()
        pop.show_for_anchor(self)
        self.update()

    def hide_popover(self) -> None:
        self._show_timer.stop()
        self._hide_timer.stop()
        pop = self.popover()
        if pop is not None and pop.isVisible():
            pop.hide()
        self._hovered = self._is_cursor_inside(self)
        self.update()

    def _toggle_popover(self) -> None:
        pop = self.popover()
        if pop is not None and pop.isVisible():
            self.hide_popover()
        else:
            self.show_popover()

    def _on_popover_hover_change(self, left: bool) -> None:
        if left:
            self._hide_timer.start()
        else:
            self._hide_timer.stop()

    def _hide_if_unhovered(self) -> None:
        if self._is_cursor_inside(self):
            return
        pop = self.popover()
        if self._is_cursor_inside(pop):
            return
        self._hovered = False
        self.hide_popover()

    def enterEvent(self, event: Any) -> None:  # noqa: N802
        self._hovered = True
        self._hide_timer.stop()
        self._show_timer.start()
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event: Any) -> None:  # noqa: N802
        self._hovered = False
        self._show_timer.stop()
        self._hide_timer.start()
        self.update()
        super().leaveEvent(event)

    def hideEvent(self, event: Any) -> None:  # noqa: N802
        self._hovered = False
        self.hide_popover()
        super().hideEvent(event)

    def changeEvent(self, event: Any) -> None:  # noqa: N802
        if event.type() == QEvent.Type.EnabledChange and not self.isEnabled():
            self._hovered = False
            self.hide_popover()
        super().changeEvent(event)

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(16, 16)

    def paintEvent(self, event: Any) -> None:  # noqa: N802
        p = palette()
        hovered = self._is_highlighted()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        rect = QRectF(1.0, 1.0, self.width() - 2.0, self.height() - 2.0)
        if hovered:
            accent = QColor(p.accent_primary)
            fill = QColor(accent)
            fill.setAlpha(30)
            border = accent
            fg = accent
        else:
            border = QColor(p.text_hint)
            border.setAlpha(170)
            fill = QColor(p.panel_bg)
            fill.setAlpha(120)
            fg = QColor(p.text_secondary)

        painter.setPen(QPen(border, 1.1))
        painter.setBrush(fill)
        painter.drawEllipse(rect)

        font = _ui_font(7.4, bold=True)
        painter.setFont(font)
        painter.setPen(fg)
        painter.drawText(rect.adjusted(0.0, -0.4, 0.0, 0.0), Qt.AlignmentFlag.AlignCenter, "?")
        painter.end()


_TIMING_HELP_HOVER_ATTR = "_krok_timing_help_hover_filter"
_FLUENT_TOOLTIP_FILTER_ATTR = "_strange_uta_game_fluent_tooltip_filter"


class TimingHelpHoverFilter(QObject):
    """Show an animated :class:`TimingHelpPopover` directly on widget hover.

    Used by timing checkboxes and switches where an extra ``?`` badge would add
    visual clutter.  Suppresses native and Fluent text tooltips on the target
    widget so only the animated card appears.
    """

    _DEFAULT_SHOW_DELAY_MS = 240
    _HIDE_DELAY_MS = 120

    def __init__(
        self,
        target: QWidget,
        key: str,
        *,
        show_delay_ms: int = _DEFAULT_SHOW_DELAY_MS,
    ) -> None:
        super().__init__(target)
        if key not in TIMING_HELP_SPECS:
            raise KeyError(f"Unknown timing help key: {key}")
        self._target = target
        self._key = key
        self._popover: Optional[TimingHelpPopover] = None

        self._show_timer = QTimer(self)
        self._show_timer.setSingleShot(True)
        self._show_timer.setInterval(max(0, int(show_delay_ms)))
        self._show_timer.timeout.connect(self.show_popover)

        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.setInterval(self._HIDE_DELAY_MS)
        self._hide_timer.timeout.connect(self._hide_if_unhovered)

        target.setMouseTracking(True)
        target.installEventFilter(self)

    @property
    def help_key(self) -> str:
        return self._key

    def popover(self) -> Optional[TimingHelpPopover]:
        if self._popover is not None and sip.isdeleted(self._popover):
            self._popover = None
        return self._popover

    def _is_cursor_inside(self, widget: Optional[QWidget]) -> bool:
        if widget is None or sip.isdeleted(widget) or not widget.isVisible():
            return False
        local_pos = widget.mapFromGlobal(QCursor.pos())
        return widget.rect().contains(local_pos)

    def _ensure_popover(self) -> TimingHelpPopover:
        pop = self.popover()
        if pop is None:
            pop = TimingHelpPopover(self._key, None)
            pop._hide_callback = self._on_popover_hover_change
            self._popover = pop

            def _cleanup(_obj: Any = None, target: TimingHelpPopover = pop) -> None:
                if not sip.isdeleted(target):
                    target.hide()
                    target.close()
                    target.deleteLater()

            self._target.destroyed.connect(_cleanup)
        return pop

    def show_popover(self) -> None:
        self._show_timer.stop()
        self._hide_timer.stop()
        if sip.isdeleted(self._target) or not self._target.isVisible():
            return
        pop = self._ensure_popover()
        pop.show_for_anchor(self._target)

    def hide_popover(self) -> None:
        self._show_timer.stop()
        self._hide_timer.stop()
        pop = self.popover()
        if pop is not None and pop.isVisible():
            pop.hide()

    def _on_popover_hover_change(self, left: bool) -> None:
        if left:
            self._hide_timer.start()
        else:
            self._hide_timer.stop()

    def _hide_if_unhovered(self) -> None:
        if self._is_cursor_inside(self._target):
            return
        if self._is_cursor_inside(self.popover()):
            return
        self.hide_popover()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802
        if watched is not self._target:
            return super().eventFilter(watched, event)
        etype = event.type()
        if etype == QEvent.Type.ToolTip:
            # Block native QToolTip so it never stacks behind the animated card.
            event.accept()
            return True
        if etype in (QEvent.Type.Enter, QEvent.Type.HoverEnter):
            self._hide_timer.stop()
            self._show_timer.start()
        elif etype in (QEvent.Type.Leave, QEvent.Type.HoverLeave):
            self._show_timer.stop()
            self._hide_timer.start()
        elif etype in (QEvent.Type.MouseButtonPress, QEvent.Type.Hide, QEvent.Type.Wheel):
            self.hide_popover()
        return super().eventFilter(watched, event)


def install_timing_help_hover(
    widget: QWidget,
    help_key: str,
    *,
    show_delay_ms: int = TimingHelpHoverFilter._DEFAULT_SHOW_DELAY_MS,
) -> TimingHelpHoverFilter:
    """Attach a hover-triggered animated help popover directly to ``widget``.

    If a qfluentwidgets ``ToolTipFilter`` was previously attached, detach it
    from the widget's event pipeline so the text tooltip never doubles up on
    top of the animated card while preserving its attribute reference.
    """
    existing_fluent = getattr(widget, _FLUENT_TOOLTIP_FILTER_ATTR, None)
    if existing_fluent is not None:
        widget.removeEventFilter(existing_fluent)

    existing_hover = getattr(widget, _TIMING_HELP_HOVER_ATTR, None)
    if isinstance(existing_hover, TimingHelpHoverFilter):
        existing_hover.hide_popover()
        widget.removeEventFilter(existing_hover)

    hover_filter = TimingHelpHoverFilter(
        widget,
        help_key,
        show_delay_ms=show_delay_ms,
    )
    setattr(widget, _TIMING_HELP_HOVER_ATTR, hover_filter)
    return hover_filter


__all__ = [
    "TIMING_HELP_SPECS",
    "TimingHelpButton",
    "TimingHelpCanvas",
    "TimingHelpHoverFilter",
    "TimingHelpPopover",
    "TimingHelpSpec",
    "install_timing_help_hover",
]
