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
