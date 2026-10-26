"""Immutable contracts shared by horizontal layout and render stages."""

from __future__ import annotations

from dataclasses import dataclass, field

from PyQt6.QtCore import QRectF
from PyQt6.QtGui import QFont, QFontMetrics

from krok_helper.subtitle_render.domain.models import Style
from krok_helper.subtitle_render.domain.paint import KaraokeColors
from krok_helper.subtitle_render.domain.timing import RubyAnnotation, TimingLine
from krok_helper.subtitle_render.engine.text import TextLayout


@dataclass(frozen=True)
class FillSegment:
    left: int
    right: int
    start_ms: int = 0
    end_ms: int = 0
    ruby: RubyAnnotation | None = None
    indices: tuple[int, ...] = ()
    release_left: float | None = None
    release_right: float | None = None
    layout_left: int | None = None
    layout_right: int | None = None
    ruby_base_index: int | None = None
    ruby_base_count: int = 1
    karaoke_effect: str = "none"


@dataclass(frozen=True)
class LineCharTransition:
    phase: str
    effect: str
    progress: float
    start_ms: int | None = None
    end_ms: int | None = None


@dataclass(frozen=True)
class SayatooLineLayout:
    baseline_y: int
    text_x: int
    line_style: Style
    metrics: QFontMetrics
    total_w: int
    signal_x: float | None = None
    signal_y: float | None = None
    # 「真一组」渐变带的本地跨度（相对 text_x）：左 = 柱组左缘（volume_
    # offset_x − group_width + stroke_extent），右 = 第一角色推进右缘。
    # 仅 音量柱启用 + auto/role 装饰档 + 段首行 + 非 RTL 时非 None；
    # 消费方：音量柱横向渐变画刷（resolve_signal_lit_groups 换算画布）。
    signal_band: tuple[float, float] | None = None


@dataclass(frozen=True)
class RubyWipeSegment:
    """One timed ruby glyph sweep on the horizontal visual axis."""

    start_ms: int
    end_ms: int
    axis_start: float
    axis_end: float


@dataclass(frozen=True)
class RubyLayout:
    """Frame-independent geometry for one horizontal ruby annotation."""

    ruby: RubyAnnotation
    indices: list[int]
    style: Style
    x: int
    baseline_y: int
    target_width: int
    reading_width: float
    gradient_rect: QRectF
    horizontal_gradient_rect: QRectF | None = None
    wipe_segments: tuple[RubyWipeSegment, ...] = ()
    wipe_left: float = 0.0
    wipe_right: float = 0.0
    geometry_signature: tuple = ()
    unit_font_signature: tuple = ()
    font: QFont | None = field(default=None, compare=False)
    metrics: QFontMetrics | None = field(default=None, compare=False)


@dataclass(frozen=True)
class LineLayout:
    """Frame-independent geometry and font resources for a horizontal line."""

    text_layout: TextLayout
    font: QFont
    metrics: QFontMetrics
    latin_font: QFont
    font_for: object
    active_rubies: list
    ruby_font: QFont
    ruby_metrics: QFontMetrics | None
    char_widths: list[int]
    total_w: int
    x0: int
    baseline_y: int
    intervals: list
    char_lefts: list[int]
    char_x_ranges: list
    fill_segments: list
    line_rect: QRectF
    colors: KaraokeColors
    rtl: bool
    has_inline_styles: bool
    ink_x_ranges: list = field(default_factory=list)
    ruby_layouts: tuple[RubyLayout, ...] = ()
    render_line: TimingLine | None = None
    # 「真一组」渐变带的画布左缘（x0 + 柱组左缘本地值）：柱体装饰源与
    # 正文第一角色同源（auto 档或 role 档悬空回退）时非 None——第一角色
    # 的横向渐变跨度左缘拓宽到这里，柱与文字共用同一条渐变带（role 档
    # 解析到固定方案时为 None：柱体仍取并集跨度，正文渐变不动）。
    signal_band_left: float | None = None
    content_rect: QRectF | None = None

    @property
    def content_bounds(self) -> QRectF:
        """Stable bounds of all inline content, independent of animation/blinking."""
        return QRectF(self.content_rect if self.content_rect is not None else self.line_rect)
