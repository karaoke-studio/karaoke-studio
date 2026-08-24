"""Vertical subtitle glyph geometry and time-independent line layout."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from PyQt6.QtCore import QRectF
from PyQt6.QtGui import QFont, QFontMetrics, QPainterPath, QTransform

from krok_helper.subtitle_render.engine.guide import guide_symbol_is_bitmap
from krok_helper.subtitle_render.engine.layout.line_style import lane_count
from krok_helper.subtitle_render.engine.ruby import (
    active_rubies_for_line,
    build_ruby_font,
)
from krok_helper.subtitle_render.engine.style.style_semantics import (
    effective_karaoke_colors,
)
from krok_helper.subtitle_render.engine.text import (
    build_font,
    build_latin_font,
    is_emoji_text,
    make_font_for,
)
from krok_helper.subtitle_render.engine.timing.timeline import (
    DisplayLine,
    char_fill_ratio,
    compute_char_intervals,
)
from krok_helper.subtitle_render.models import Style
from krok_helper.subtitle_render.paint import KaraokeColors
from krok_helper.subtitle_render.sources.guide_symbols import scaled_guide_symbol_path
from krok_helper.subtitle_render.timing import RubyAnnotation, TimingLine, TimingTrack


VERTICAL_REFERENCE_CHAR = "永"
VERTICAL_ROTATE_CHARS = set(
    "ーｰ"
    "—―‐‑‒–"
    "〜～"
    "→←"
    "（）()"
    "「」『』"
    "【】〔〕"
    "［］｛｝"
    "〈〉《》"
    "[]{}<>"
)
VERTICAL_CORNER_PUNCT = set("、。，．")
VERTICAL_SMALL_KANA = set(
    "ぁぃぅぇぉっゃゅょゎ"
    "ァィゥェォッャュョヮ"
    "ヵヶ"
)


@dataclass(frozen=True)
class VerticalLineLayout:
    """Time-independent geometry for one vertical subtitle line."""

    font: QFont
    metrics: QFontMetrics
    cell_w: int
    cell_h: int
    ascent: int
    column_x: int
    y_top: int
    block_h: int
    intervals: list[tuple[int, int]]
    cells: list[tuple[int, int]]
    line_rect: QRectF
    text_path: QPainterPath
    colors: KaraokeColors
    active_rubies: list[RubyAnnotation]


class ResolveCharRubyGroups(Protocol):
    def __call__(
        self,
        rubies: list[RubyAnnotation],
        line: TimingLine,
        intervals: list[tuple[int, int]],
    ) -> object: ...


class CharacterFillRatio(Protocol):
    def __call__(
        self,
        line: TimingLine,
        intervals: list[tuple[int, int]],
        fill_ranges: list[tuple[int, int]],
        active_rubies: list[RubyAnnotation],
        index: int,
        t_ms: int,
        *,
        groups: object,
        ruby_main_progress_mode: str,
    ) -> float: ...


@dataclass(frozen=True)
class VerticalProgressPorts:
    resolve_char_ruby_groups: ResolveCharRubyGroups
    character_fill_ratio: CharacterFillRatio


def vertical_orientation(char: str) -> str:
    """Return ``R`` for simplified UTR#50 rotation, otherwise ``U``."""
    return "R" if char in VERTICAL_ROTATE_CHARS else "U"


def vertical_glyph_offset(
    char: str,
    cell_w: int,
    cell_h: int,
) -> tuple[float, float]:
    if char in VERTICAL_CORNER_PUNCT:
        return cell_w * 0.28, -cell_h * 0.28
    if char in VERTICAL_SMALL_KANA:
        return cell_w * 0.10, -cell_h * 0.10
    return 0.0, 0.0


def vertical_glyph_path(
    text: str,
    font: QFont,
    metrics: QFontMetrics,
    column_x: int,
    cell_top: int,
    cell_w: int,
    cell_h: int,
    ascent: int,
    *,
    vector_glyph=None,
) -> QPainterPath:
    """Build one centered vertical glyph path, including guide symbols."""
    if vector_glyph is not None:
        if guide_symbol_is_bitmap(vector_glyph):
            return QPainterPath()
        path = scaled_guide_symbol_path(
            vector_glyph,
            pixel_size=max(int(font.pixelSize()), 1),
            left=float(column_x - max(int(font.pixelSize()), 1) / 2),
            baseline_y=float(cell_top + ascent),
        )
        bounds = path.boundingRect()
        return QTransform.fromTranslate(
            float(column_x) - bounds.center().x(),
            float(cell_top + cell_h / 2) - bounds.center().y(),
        ).map(path)
    advance = metrics.horizontalAdvance(text)
    baseline = cell_top + ascent
    glyph_x = column_x - advance / 2
    path = QPainterPath()
    if vertical_orientation(text) == "R":
        path.addText(float(glyph_x), float(baseline), font, text)
        center_x = float(column_x)
        center_y = float(cell_top + cell_h / 2)
        transform = QTransform()
        transform.translate(center_x, center_y)
        transform.rotate(90)
        transform.translate(-center_x, -center_y)
        return transform.map(path)
    dx, dy = vertical_glyph_offset(text, cell_w, cell_h)
    path.addText(float(glyph_x + dx), float(baseline + dy), font, text)
    return path


def vertical_cell_width(metrics: QFontMetrics) -> int:
    width = metrics.horizontalAdvance(VERTICAL_REFERENCE_CHAR)
    if width <= 0:
        width = metrics.height()
    return max(width, 1)


def vertical_ruby_allowance(track: TimingTrack, style: Style) -> int:
    if not track.rubies:
        return 0
    ruby_metrics = QFontMetrics(build_ruby_font(style))
    return max(ruby_metrics.height() + int(style.ruby_gap_px), 0)


def resolve_vertical_columns(
    img_w: int,
    track: TimingTrack,
    display_lines: list[DisplayLine],
    style: Style,
) -> dict[int, int]:
    metrics = QFontMetrics(build_font(style))
    cell_w = vertical_cell_width(metrics)
    margin = style.line_y_margin_px
    gap = max(style.line_gap_px, 0)
    ruby_w = vertical_ruby_allowance(track, style)
    right_center = img_w - margin - ruby_w - cell_w / 2
    max_lane = max((item.lane for item in display_lines), default=0)
    return {
        lane: int(round(right_center - lane * (cell_w + ruby_w + gap)))
        for lane in range(max(lane_count(style), max_lane + 1))
    }


def resolve_vertical_top(img_h: int, block_h: int, style: Style) -> int:
    margin = style.line_y_margin_px
    if style.line_y_position == "top":
        return margin
    if style.line_y_position == "center":
        return max((img_h - block_h) // 2, 0)
    return img_h - margin - block_h


def vertical_after_clip_rect(
    column_x: int,
    cell_w: int,
    y0: int,
    y_scan: int,
    pad: int,
) -> QRectF:
    return QRectF(
        float(column_x - cell_w / 2 - pad),
        float(y0 - pad),
        float(cell_w + pad * 2),
        float((y_scan - y0) + pad),
    )


def vertical_before_clip_rect(
    column_x: float,
    cell_w: float,
    y_scan: float,
    pad: int,
) -> QRectF:
    """Return the complementary unsung layer below the vertical wipe."""
    return QRectF(
        float(column_x - cell_w / 2 - pad),
        float(y_scan),
        float(cell_w + pad * 2),
        1_000_000.0,
    )


def vertical_fill_band(
    cells: list[tuple[int, int]],
    intervals: list[tuple[int, int]],
    t_ms: int,
    *,
    ports: VerticalProgressPorts,
    line: TimingLine | None = None,
    active_rubies: list[RubyAnnotation] | None = None,
    ruby_main_progress_mode: str = "checkpoint_segments",
) -> tuple[int, int] | None:
    """Return the sung vertical band ``(y_top, y_scan)``."""
    if not cells:
        return None
    y_top = cells[0][0]
    scan = float(y_top)
    ruby_groups = (
        ports.resolve_char_ruby_groups(active_rubies, line, intervals)
        if ruby_main_progress_mode == "reading_units"
        and line is not None
        and active_rubies
        else None
    )
    for index, ((cell_top, cell_bottom), (start, end)) in enumerate(
        zip(cells, intervals)
    ):
        ratio = (
            ports.character_fill_ratio(
                line,
                intervals,
                cells,
                active_rubies or [],
                index,
                t_ms,
                groups=ruby_groups,
                ruby_main_progress_mode=ruby_main_progress_mode,
            )
            if ruby_groups is not None and line is not None
            else char_fill_ratio(start, end, t_ms)
        )
        if ratio <= 0.0:
            break
        if ratio >= 1.0:
            scan = cell_bottom
            continue
        scan = cell_top + (cell_bottom - cell_top) * ratio
        break
    if scan <= y_top:
        return None
    return y_top, int(round(scan))


def layout_vertical_line(
    track: TimingTrack,
    line: TimingLine,
    style: Style,
    img_w: int,
    img_h: int,
    *,
    column_x: int | None,
    source_line: TimingLine | None = None,
    resolved_intervals: tuple[tuple[int, int], ...] | None = None,
) -> VerticalLineLayout | None:
    chars = line.chars
    if not chars:
        return None
    font = build_font(style)
    metrics = QFontMetrics(font)
    latin_font = build_latin_font(style)
    font_for = make_font_for(style, font, latin_font)
    latin_metrics = QFontMetrics(latin_font) if font_for is not None else metrics
    cell_w = vertical_cell_width(metrics)
    cell_h = metrics.height()
    ascent = metrics.ascent()
    resolved_column_x = (
        column_x
        if column_x is not None
        else int(round(img_w - style.line_y_margin_px - cell_w / 2))
    )
    block_h = cell_h * len(chars)
    y_top = resolve_vertical_top(img_h, block_h, style)
    intervals = (
        list(resolved_intervals)
        if resolved_intervals is not None
        else compute_char_intervals(line)
    )
    text_path = QPainterPath()
    cells: list[tuple[int, int]] = []
    for index, char in enumerate(chars):
        cell_top = y_top + index * cell_h
        cells.append((cell_top, cell_top + cell_h))
        glyph_font = font_for(char.text) if font_for is not None else font
        glyph_metrics = (
            QFontMetrics(glyph_font)
            if is_emoji_text(char.text)
            else latin_metrics
            if (font_for is not None and char.text and char.text.isascii())
            else metrics
        )
        text_path.addPath(
            vertical_glyph_path(
                char.text,
                glyph_font,
                glyph_metrics,
                resolved_column_x,
                cell_top,
                cell_w,
                cell_h,
                ascent,
                vector_glyph=char.vector_glyph,
            )
        )
    line_rect = QRectF(
        float(resolved_column_x - cell_w / 2),
        float(y_top),
        float(cell_w),
        float(block_h),
    )
    return VerticalLineLayout(
        font=font,
        metrics=metrics,
        cell_w=cell_w,
        cell_h=cell_h,
        ascent=ascent,
        column_x=resolved_column_x,
        y_top=y_top,
        block_h=block_h,
        intervals=intervals,
        cells=cells,
        line_rect=line_rect,
        text_path=text_path,
        colors=effective_karaoke_colors(style),
        active_rubies=active_rubies_for_line(track.rubies, source_line or line),
    )


__all__ = [
    "VerticalLineLayout",
    "VerticalProgressPorts",
    "layout_vertical_line",
    "resolve_vertical_columns",
    "resolve_vertical_top",
    "vertical_after_clip_rect",
    "vertical_before_clip_rect",
    "vertical_cell_width",
    "vertical_glyph_offset",
    "vertical_glyph_path",
    "vertical_fill_band",
    "vertical_orientation",
    "vertical_ruby_allowance",
]
