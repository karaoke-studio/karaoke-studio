"""Title-overlay font selection and immutable layout contracts."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Hashable, Optional

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QFont, QFontMetrics, QImage, QPainter, QPainterPath

from krok_helper.subtitle_render.engine.render.effects import (
    brush_for_fill,
    fill_is_alpha,
    glow_blur_radii,
    glow_extent,
    paint_fill_path,
    paint_glow_path,
    paint_shadow_silhouette,
    paint_stroke_path,
    stroke2_pen_width,
    stroke_pen_width,
)
from krok_helper.subtitle_render.engine.render.core.raster_blur import blur_image
from krok_helper.subtitle_render.engine.guide.metrics import (
    bitmap_guide_content_size,
    bitmap_guide_frame_at,
    vector_glyph_width,
)
from krok_helper.subtitle_render.engine.guide.semantics import (
    guide_symbol_is_bitmap,
)
from krok_helper.subtitle_render.sources.guide_symbols import (
    scaled_guide_symbol_path,
)
from krok_helper.subtitle_render.engine.render.core.layers import (
    BakedLayer,
    LayerAnimation,
    LayerCompositor,
    LayerContext,
    SCOPE_LINE,
)
from krok_helper.subtitle_render.engine.style.title_semantics import (
    resolve_title_overlay,
    resolve_title_role_overlay,
    resolve_title_text,
    title_layout_source,
    title_row_alignments,
)
from krok_helper.subtitle_render.engine.text import (
    char_layout_width,
    char_path_left_offset,
    n3_char_box_ascent,
    n3_char_box_descent,
)
from krok_helper.subtitle_render.engine.text.font_weight import (
    build_weight_font,
    embolden_glyph_path,
)
from krok_helper.subtitle_render.domain.models import (
    Style,
    TitleOverlay,
    normalize_title_char_role_labels,
    normalize_title_guide_symbols,
    title_line_units,
)
from krok_helper.subtitle_render.n3.font_catalog import resolve_qt_font_family
from krok_helper.subtitle_render.domain.paint import PaintFill
from krok_helper.subtitle_render.domain.timing import (
    GuideSymbol,
    TimingTrack,
    guide_symbol_role_labels,
)


class _TitleInkMetricsStyle:
    """把 TitleOverlay 适配成 ink-cell 度量所需的 Style 字段子集。

    C++ 把标题投影进歌词管线（强制 n3_1074）后，公式输入的 edge 取每字
    解析外观自己的描边宽、空格百分比与咬字许可继承全局 Style
    （``resolvedStyleFromTitle`` 以 sourceStyle 为底）——本适配器按同一
    取值来源供 :func:`char_layout_width` / :func:`char_path_left_offset`
    鸭子类型读取，不构造完整 Style。
    """

    def __init__(self, glyph_title, global_style, space_percent: int) -> None:
        self.font_size_px = max(int(glyph_title.font_size_px), 1)
        self.stroke_width_px = max(int(glyph_title.stroke_width_px), 0)
        self.latin_font_size_px = None
        self.space_width_percent = int(space_percent)
        self.allow_biting = bool(
            global_style.allow_biting if global_style is not None else False
        )


@dataclass(frozen=True)
class TitleGlyphLayout:
    text: str
    x: float
    advance: float
    font: QFont
    metrics: QFontMetrics
    title: TitleOverlay
    guide_symbol: GuideSymbol | None = None
    """行前导唱符或行内图片替换；非 ``None`` 时不按 ``text`` 画字形。

    矢量符号作为路径并入所在文字 run（共享同一套描边 / 填充 / 发光），
    位图符号恒取「走字前」一侧图片——标题永不走字，没有走字后态。"""
    path_offset: float = 0.0
    """字形绘制相对 ``x`` 的水平偏移（ink-cell 语义的 ``pathOffset``）。

    与 D2D 侧 configure 的 ``(-inkLeft + geometryLeft + edge/2)`` 同式
    （:func:`char_path_left_offset`）；导唱符与空格恒为 0。"""


@dataclass(frozen=True)
class TitleOverlayLayout:
    """Time-independent geometry for one title overlay."""

    lines: list[str]
    widths: list[float]
    block_w: float
    block_h: float
    line_h: float
    gap: int
    x0: float
    y_top: float
    font: QFont
    metrics: QFontMetrics
    latin_font: QFont
    latin_metrics: QFontMetrics
    font_for: Callable[[str], QFont] | None
    glyph_rows: list[list[TitleGlyphLayout]]
    line_heights: list[float]
    line_ascents: list[float]
    row_x: list[float]
    """Per-row screen-space left x; rows align independently (title block = one page)."""


@dataclass(frozen=True)
class TitleRenderPorts:
    """Painter-backend services consumed by title-layer rasterization."""

    fill_signature: Callable[[PaintFill], tuple]
    make_raster_image: Callable[[int, int, float], QImage]
    paint_text_stack: Callable[[QPainter, QPainterPath, QRectF, TitleOverlay], None]
    raster_scale_key: Callable[[float], int]
    visual_padding: Callable[[TitleOverlay], int]


def build_title_font(title: TitleOverlay) -> QFont:
    font = build_weight_font(
        resolve_qt_font_family(title.font_family),
        title.font_size_px,
        title.font_weight,
    )
    font.setItalic(title.italic)
    return font


def build_title_latin_font(title: TitleOverlay) -> QFont:
    family = title.font_family_latin or title.font_family
    font = build_weight_font(
        resolve_qt_font_family(family),
        title.font_size_px,
        title.font_weight,
    )
    font.setItalic(title.italic)
    if int(title.latin_font_stretch_pct) != 100:
        font.setStretch(max(50, min(200, int(title.latin_font_stretch_pct))))
    return font


def make_title_font_for(
    title: TitleOverlay,
    jp_font: QFont,
    latin_font: QFont,
) -> Callable[[str], QFont] | None:
    if (not title.font_family_latin or latin_font.family() == jp_font.family()) and (
        latin_font.stretch() == jp_font.stretch()
    ):
        return None

    def font_for(text: str) -> QFont:
        return latin_font if (text and text.isascii()) else jp_font

    return font_for


def paint_title_text_stack(
    painter: QPainter,
    path: QPainterPath,
    rect: QRectF,
    title: TitleOverlay,
) -> None:
    """Paint one static title state with its complete decoration stack."""

    if title.decoration_kind == "glow":
        paint_glow_path(
            painter,
            path,
            title.shadow,
            rect,
            max(int(title.glow_radius_px), 0),
            title.stroke_width_px,
            title.stroke2_width_px,
            concentration_level=title.glow_concentration_level,
        )
    elif (
        title.decoration_kind == "shadow"
        and (title.shadow_offset_x or title.shadow_offset_y)
    ):
        paint_shadow_silhouette(
            painter,
            path,
            title.shadow,
            rect,
            title.shadow_offset_x,
            title.shadow_offset_y,
            title.stroke_width_px,
            title.stroke2_width_px,
        )
    if title.stroke2_width_px > 0:
        paint_stroke_path(
            painter,
            path,
            title.stroke2,
            rect,
            stroke2_pen_width(
                title.stroke_width_px,
                title.stroke2_width_px,
            ),
        )
    if title.stroke_width_px > 0:
        paint_stroke_path(
            painter,
            path,
            title.stroke,
            rect,
            stroke_pen_width(title.stroke_width_px),
            protect_body=fill_is_alpha(title.fill),
        )
    paint_fill_path(painter, path, title.fill, rect)


def title_block_origin(
    img_w: int,
    img_h: int,
    block_w: float,
    block_h: float,
    title: TitleOverlay,
    *,
    edge_px: float = 0.0,
) -> tuple[float, float]:
    """Place a title block on its nine-grid anchor."""
    anchor = title.anchor
    half_edge = max(float(edge_px), 0.0) / 2.0
    if anchor.endswith("left"):
        x0 = title.offset_x + half_edge
    elif anchor.endswith("right"):
        x0 = img_w - block_w - title.offset_x - half_edge
    else:
        x0 = (img_w - block_w) / 2.0 + title.offset_x
    if anchor.startswith("top"):
        y_top = float(title.offset_y)
    elif anchor.startswith("bottom"):
        y_top = img_h - block_h - title.offset_y
    else:
        y_top = (img_h - block_h) / 2.0 + title.offset_y
    return x0, y_top


def title_row_screen_x(
    img_w: int,
    row_w: float,
    align: str,
    offset_x: int,
    half_edge: float,
) -> float:
    """Place one title row by its page-row alignment against the screen edges.

    N3 的 ``SetOneLineX`` 里 Center 行以整行自然宽度居中、不以左右余白锚定
    （与歌词 ``_resolve_line_x`` 同口径），余白只贴 Left / Right 行。
    """
    if align == "left":
        return float(offset_x) + half_edge
    if align == "right":
        return float(img_w) - float(offset_x) - half_edge - row_w
    return (float(img_w) - row_w) / 2.0


def title_anchor_block_x0(
    img_w: int,
    block_w: float,
    title: TitleOverlay,
    half_edge: float,
) -> float:
    """Legacy anchor-side x of the whole block (widest row) on the nine grid.

    居中锚点不带余白偏移：旧标题迁移时居中锚点的正负偏移本就「按 0 余白
    近似」（``_layout_from_title_position``），这里保持同一口径。
    """
    if title.anchor.endswith("left"):
        return float(title.offset_x) + half_edge
    if title.anchor.endswith("right"):
        return float(img_w) - block_w - float(title.offset_x) - half_edge
    return (float(img_w) - block_w) / 2.0


def title_row_offset_in_block(block_w: float, row_w: float, align: str) -> float:
    """Legacy within-block row offset driven by the single ``align`` field."""
    if align == "center":
        return (block_w - row_w) / 2.0
    if align == "right":
        return block_w - row_w
    return 0.0


def title_block_y_top(img_h: int, block_h: float, title: TitleOverlay) -> float:
    """Vertical anchor of the title block (nine-grid top edge).

    与歌词 ``_resolve_baseline_y`` 同口径：Top 用上余白、Bottom 用下余白、
    Middle 整体垂直居中（N3 Middle 忽略上下余白）。
    """
    if title.anchor.startswith("top"):
        return float(title.offset_y)
    if title.anchor.startswith("bottom"):
        return float(img_h) - block_h - title.offset_y
    return (float(img_h) - block_h) / 2.0


def layout_title_overlay(
    img_w: int,
    img_h: int,
    track: TimingTrack,
    title: TitleOverlay,
    *,
    style: Style | None = None,
) -> TitleOverlayLayout | None:
    text = resolve_title_text(title, track)
    lines = text.split("\n")
    if not any(line.strip() for line in lines):
        return None
    font = build_title_font(title)
    metrics = QFontMetrics(font)
    latin_font = build_title_latin_font(title)
    font_for = make_title_font_for(title, font, latin_font)
    latin_metrics = QFontMetrics(latin_font) if font_for is not None else metrics
    labels = normalize_title_char_role_labels(text, title.char_role_labels)
    title_space_percent = max(
        10,
        min(
            int(style.space_width_percent if style is not None else Style.space_width_percent),
            100,
        ),
    )
    glyph_rows: list[list[TitleGlyphLayout]] = []
    widths: list[float] = []
    line_heights: list[float] = []
    line_ascents: list[float] = []
    max_edge = 0.0
    # 导唱符键位与展示文字（已解析占位符）对齐：模板未冻结时越界条目在此丢弃。
    row_symbols, inline_symbols = normalize_title_guide_symbols(
        text, title.guide_symbols, title.inline_guide_symbols
    )
    fallback_ascent = n3_char_box_ascent(
        metrics,
        title.font_size_px,
        title.stroke_width_px,
    )
    fallback_descent = n3_char_box_descent(
        metrics,
        title.font_size_px,
        title.stroke_width_px,
    )
    for row_index, text_line in enumerate(lines):
        # 行前导唱符（count 个）+ 正文逐字符；行内被替换的字符画导唱符图片。
        # 角色口径与歌词一致：行前导唱符用符号自带逐个角色标签，行内替换
        # 沿用被替换字符自己的角色标签。
        units: list[tuple[str, Optional[str], GuideSymbol | None]] = []
        row_symbol = row_symbols.get(row_index)
        if row_symbol is not None:
            units.extend(
                ("\uFFFC", label, row_symbol)
                for label in guide_symbol_role_labels(row_symbol)
            )
        units.extend(
            (
                char,
                labels[row_index][char_index],
                inline_symbols.get((row_index, char_index)),
            )
            for char_index, char in enumerate(title_line_units(text_line))
        )
        glyphs: list[TitleGlyphLayout] = []
        cursor = 0.0
        max_ascent = 0.0
        max_descent = 0.0
        for unit_index, (unit_text, role_label, unit_symbol) in enumerate(units):
            glyph_title = (
                resolve_title_role_overlay(
                    style,
                    title,
                    role_label,
                )
                if style is not None
                else title
            )
            glyph_jp_font = build_title_font(glyph_title)
            glyph_latin_font = build_title_latin_font(glyph_title)
            glyph_font_for = make_title_font_for(
                glyph_title,
                glyph_jp_font,
                glyph_latin_font,
            )
            glyph_font = (
                glyph_font_for(unit_text) if glyph_font_for is not None else glyph_jp_font
            )
            glyph_metrics = QFontMetrics(glyph_font)
            glyph_latin_metrics = (
                QFontMetrics(glyph_latin_font)
                if glyph_font_for is not None
                else glyph_metrics
            )
            if unit_symbol is not None:
                # vector_glyph_width 只读 font_size_px：标题的逐字解析外观与
                # 歌词 Style 同名同义，直接复用同一套导唱符宽度契约。
                advance = float(vector_glyph_width(unit_symbol, glyph_title))
                path_offset = 0.0
            elif unit_text == " ":
                space_unit = glyph_font.pixelSize()
                if space_unit <= 0:
                    space_unit = max(int(glyph_title.font_size_px), 1)
                advance = float(space_unit * title_space_percent // 100)
                path_offset = 0.0
            else:
                # 与 D2D 侧对齐（2026-10 用户拍板「对齐 GPU」）：sidecar 把
                # 标题投影进歌词同一条 TextLine 管线（强制 n3_1074），逐字
                # advance 走 ink-cell 公式（墨迹宽 × 轴承比 + 描边宽），而
                # 不是 QFontMetrics 的裸 advance——裸 advance 会随字体在
                # Qt/DWrite 的度量差逐字累积（实测 4K 标题行末端偏 ~90px）。
                ink_style = _TitleInkMetricsStyle(
                    glyph_title, style, title_space_percent
                )
                advance = float(
                    char_layout_width(
                        unit_text,
                        glyph_jp_font,
                        glyph_metrics,
                        glyph_latin_metrics,
                        glyph_font_for,
                        ink_style,
                    )
                )
                path_offset = char_path_left_offset(
                    unit_text,
                    glyph_jp_font,
                    glyph_metrics,
                    glyph_latin_metrics,
                    glyph_font_for,
                    ink_style,
                )
            glyphs.append(
                TitleGlyphLayout(
                    text=unit_text,
                    x=cursor,
                    advance=advance,
                    font=glyph_font,
                    metrics=glyph_metrics,
                    title=glyph_title,
                    guide_symbol=unit_symbol,
                    path_offset=path_offset,
                )
            )
            cursor += advance
            if unit_index + 1 < len(units):
                cursor += int(glyph_title.letter_spacing_px)
            max_ascent = max(
                max_ascent,
                n3_char_box_ascent(
                    glyph_metrics,
                    glyph_title.font_size_px,
                    glyph_title.stroke_width_px,
                ),
            )
            max_descent = max(
                max_descent,
                n3_char_box_descent(
                    glyph_metrics,
                    glyph_title.font_size_px,
                    glyph_title.stroke_width_px,
                ),
            )
            max_edge = max(max_edge, float(max(glyph_title.stroke_width_px, 0)))
        if not glyphs:
            max_ascent = fallback_ascent
            max_descent = fallback_descent
        glyph_rows.append(glyphs)
        widths.append(cursor)
        line_ascents.append(max_ascent)
        line_heights.append(max_ascent + max_descent)
    line_h = max(line_heights, default=metrics.height())
    gap = max(int(title.line_gap_px), 0)
    block_h = sum(line_heights) + gap * max(len(lines) - 1, 0)
    if block_h <= 0:
        return None

    half_edge = max_edge / 2.0
    if style is not None and title_layout_source(style, title.layout_index) is not None:
        # 标题块=一页：每行按引用布局的行对齐槽位自上而下各自贴屏定位
        # （left 贴左余白、right 贴右余白、center 居中），块盒取各行屏幕位置
        # 的并集。统一对齐时与整块锚点定位逐像素等价（同 left/center/right
        # 的算式）。
        row_aligns = title_row_alignments(style, title, len(lines))
        row_x = [
            title_row_screen_x(img_w, width, align, title.offset_x, half_edge)
            for align, width in zip(row_aligns, widths)
        ]
        x0 = min(row_x)
        block_w = max(x + w for x, w in zip(row_x, widths)) - x0
    else:
        # 布局引用缺失（旧工程显式锚点/对齐字段）：保持整块九宫格锚点定位
        # + 块内按 ``title.align`` 统一对齐的原语义。
        block_w = max(widths) if widths else 0.0
        x0 = title_anchor_block_x0(img_w, block_w, title, half_edge)
        row_x = [
            x0 + title_row_offset_in_block(block_w, width, title.align)
            for width in widths
        ]
    if block_w <= 0:
        return None
    y_top = title_block_y_top(img_h, block_h, title)
    return TitleOverlayLayout(
        lines=lines,
        widths=widths,
        block_w=block_w,
        block_h=float(block_h),
        line_h=line_h,
        gap=gap,
        x0=x0,
        y_top=y_top,
        font=font,
        metrics=metrics,
        latin_font=latin_font,
        latin_metrics=latin_metrics,
        font_for=font_for,
        glyph_rows=glyph_rows,
        line_heights=line_heights,
        line_ascents=line_ascents,
        row_x=row_x,
    )


def title_overlay_layer_key(
    layout: TitleOverlayLayout,
    title: TitleOverlay,
    *,
    fill_signature: Callable[[PaintFill], tuple],
) -> tuple:
    return (
        tuple(layout.lines),
        tuple(round(width, 3) for width in layout.widths),
        tuple(round(x, 3) for x in layout.row_x),
        round(layout.block_w, 3),
        round(layout.block_h, 3),
        round(layout.line_h, 3),
        layout.gap,
        title.align,
        layout.font.family(),
        layout.font.pixelSize(),
        int(layout.font.weight()),
        layout.font.italic(),
        layout.latin_font.family(),
        layout.latin_font.pixelSize(),
        int(layout.latin_font.weight()),
        layout.latin_font.italic(),
        layout.latin_font.stretch(),
        title.letter_spacing_px,
        fill_signature(title.fill),
        fill_signature(title.stroke),
        title.stroke_width_px,
        fill_signature(title.stroke2),
        title.stroke2_width_px,
        title.decoration_kind,
        title.glow_radius_px,
        title.glow_concentration_level,
        fill_signature(title.shadow),
        title.shadow_offset_x,
        title.shadow_offset_y,
        tuple(
            (
                glyph.text,
                round(glyph.x, 3),
                round(glyph.advance, 3),
                glyph.font.family(),
                glyph.font.pixelSize(),
                int(glyph.font.weight()),
                glyph.font.italic(),
                glyph.font.stretch(),
                glyph.guide_symbol,
                fill_signature(glyph.title.fill),
                fill_signature(glyph.title.stroke),
                glyph.title.stroke_width_px,
                fill_signature(glyph.title.stroke2),
                glyph.title.stroke2_width_px,
                glyph.title.decoration_kind,
                glyph.title.glow_radius_px,
                glyph.title.glow_concentration_level,
                fill_signature(glyph.title.shadow),
                glyph.title.shadow_offset_x,
                glyph.title.shadow_offset_y,
            )
            for row in layout.glyph_rows
            for glyph in row
        ),
    )


def _title_bitmap_guide_rects(
    layout: TitleOverlayLayout,
) -> list[tuple[TitleGlyphLayout, QRectF]]:
    """位图导唱符图片在标题块坐标系（``x0`` / 块顶为原点）里的目标矩形。

    垂直锚定与歌词 n3_1074 语义、D2D sidecar 的 ``anchorDescent`` 同式：
    图片底缘贴 ``基线 + 字号×descent/(ascent+descent) + 描边宽/2``
    （可被 ``bitmap_margin_bottom_px`` 上移），高度按导唱符自身缩放。
    """
    rects: list[tuple[TitleGlyphLayout, QRectF]] = []
    row_top = 0.0
    for row_x, glyphs, line_height, line_ascent in zip(
        layout.row_x,
        layout.glyph_rows,
        layout.line_heights,
        layout.line_ascents,
    ):
        for glyph in glyphs:
            symbol = glyph.guide_symbol
            if not guide_symbol_is_bitmap(symbol):
                continue
            width, height = bitmap_guide_content_size(symbol, glyph.title)
            left = row_x + glyph.x + int(symbol.bitmap_margin_left_px) - layout.x0
            anchor_descent = n3_char_box_descent(
                glyph.metrics,
                glyph.title.font_size_px,
                glyph.title.stroke_width_px,
            )
            bottom = (
                row_top
                + line_ascent
                + anchor_descent
                - int(symbol.bitmap_margin_bottom_px)
            )
            rects.append(
                (
                    glyph,
                    QRectF(
                        float(left),
                        float(bottom - height),
                        float(max(width, 1)),
                        float(max(height, 1)),
                    ),
                )
            )
        row_top += line_height + layout.gap
    return rects


def _tinted_title_guide_silhouette(
    image: QImage, fill: PaintFill, rect: QRectF
) -> QImage:
    """把导唱符图片的 Alpha 剪影按飾り画刷染色（标题单态，无走字渐变跨度）。"""
    width = max(int(math.ceil(rect.width())), 1)
    height = max(int(math.ceil(rect.height())), 1)
    silhouette = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
    silhouette.fill(0)
    painter = QPainter(silhouette)
    try:
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.translate(-rect.left(), -rect.top())
        painter.drawImage(rect, image)
        painter.setCompositionMode(
            QPainter.CompositionMode.CompositionMode_SourceIn
        )
        painter.fillRect(rect, brush_for_fill(fill, rect))
    finally:
        painter.end()
    return silhouette


def _paint_title_bitmap_guide_decor(
    painter: QPainter,
    glyph: TitleGlyphLayout,
    image: QImage,
    rect: QRectF,
) -> None:
    """标题位图导唱符的飾り（shadow / glow）剪影。

    与歌词 :func:`paint_bitmap_guide_decor` 同一套几何；标题永不走字，
    恒用「走字前」单态配色（标题装饰本就单态），也没有 wipe 裁切。标题层
    按 static key 整块烘焙一次，这里不需要歌词侧的帧级缓存。
    """
    symbol = glyph.guide_symbol
    title = glyph.title
    if symbol is None or symbol.bitmap_no_decor:
        return
    if title.decoration_kind not in {"shadow", "glow"}:
        return
    fill = title.shadow
    if not fill.color:
        return
    if title.decoration_kind == "glow":
        radius = max(int(title.glow_radius_px), 0)
        if radius <= 0:
            return
        pad = glow_extent(0, 0, radius) + 2
        silhouette = _tinted_title_guide_silhouette(image, fill, rect)
        source = QImage(
            max(int(math.ceil(rect.width())) + pad * 2, 1),
            max(int(math.ceil(rect.height())) + pad * 2, 1),
            QImage.Format.Format_ARGB32_Premultiplied,
        )
        source.fill(0)
        source_painter = QPainter(source)
        try:
            source_painter.setRenderHint(
                QPainter.RenderHint.SmoothPixmapTransform, True
            )
            source_painter.drawImage(QPointF(pad, pad), silhouette)
        finally:
            source_painter.end()
        painter.save()
        try:
            for blur_value in glow_blur_radii(
                radius, title.glow_concentration_level
            ):
                painter.drawImage(
                    QPointF(rect.left() - pad, rect.top() - pad),
                    blur_image(source, blur_value),
                )
        finally:
            painter.restore()
        return
    shadow_dx = int(title.shadow_offset_x or 0)
    shadow_dy = int(title.shadow_offset_y or 0)
    if not (shadow_dx or shadow_dy):
        return
    painter.drawImage(
        rect.translated(shadow_dx, shadow_dy),
        _tinted_title_guide_silhouette(image, fill, rect),
    )


def build_title_overlay_layer(
    layout: TitleOverlayLayout,
    title: TitleOverlay,
    *,
    ports: TitleRenderPorts,
    device_pixel_ratio: float = 1.0,
) -> tuple[QImage, int, int]:
    glyph_titles = [glyph.title for row in layout.glyph_rows for glyph in row] or [
        title
    ]
    extent = max(ports.visual_padding(item) for item in glyph_titles) + 4
    pad_left = max(max(0, -item.shadow_offset_x) for item in glyph_titles) + extent
    pad_right = max(max(0, item.shadow_offset_x) for item in glyph_titles) + extent
    pad_top = max(max(0, -item.shadow_offset_y) for item in glyph_titles) + extent
    pad_bottom = max(max(0, item.shadow_offset_y) for item in glyph_titles) + extent
    # 位图导唱符可能高出块顶 / 超出块宽（大倍率缩放、负余白）：烘焙图必须
    # 先把这些矩形包进来，否则图片会被自己的图层边界裁掉。
    bitmap_units = _title_bitmap_guide_rects(layout)
    if bitmap_units:
        left = min(rect.left() for _glyph, rect in bitmap_units)
        right = max(rect.right() for _glyph, rect in bitmap_units)
        top = min(rect.top() for _glyph, rect in bitmap_units)
        bottom = max(rect.bottom() for _glyph, rect in bitmap_units)
        pad_left = max(pad_left, int(math.ceil(max(0.0, -left))))
        pad_right = max(pad_right, int(math.ceil(max(0.0, right - layout.block_w))))
        pad_top = max(pad_top, int(math.ceil(max(0.0, -top))))
        pad_bottom = max(pad_bottom, int(math.ceil(max(0.0, bottom - layout.block_h))))
    img_w = max(int(math.ceil(pad_left + layout.block_w + pad_right)), 1)
    img_h = max(int(math.ceil(pad_top + layout.block_h + pad_bottom)), 1)
    image = ports.make_raster_image(img_w, img_h, device_pixel_ratio)
    image.fill(0)

    painter = QPainter(image)
    try:
        painter.setRenderHints(
            QPainter.RenderHint.Antialiasing
            | QPainter.RenderHint.TextAntialiasing
            | QPainter.RenderHint.SmoothPixmapTransform
        )
        line_top = float(pad_top)
        for row_x, glyphs, line_height, line_ascent in zip(
            layout.row_x,
            layout.glyph_rows,
            layout.line_heights,
            layout.line_ascents,
        ):
            if glyphs:
                line_x = pad_left + (row_x - layout.x0)
                baseline = line_top + line_ascent
                run_start = 0
                while run_start < len(glyphs):
                    if guide_symbol_is_bitmap(glyphs[run_start].guide_symbol):
                        run_start += 1
                        continue
                    run_end = run_start + 1
                    run_title = glyphs[run_start].title
                    while run_end < len(glyphs) and (
                        glyphs[run_end].title == run_title
                        and not guide_symbol_is_bitmap(glyphs[run_end].guide_symbol)
                    ):
                        run_end += 1
                    run = glyphs[run_start:run_end]
                    path = QPainterPath()
                    path.setFillRule(Qt.FillRule.WindingFill)
                    for glyph in run:
                        if glyph.guide_symbol is not None:
                            # 矢量导唱符：作为路径并入本 run，共享同一套
                            # 描边 / 填充 / 发光装饰。
                            path.addPath(
                                scaled_guide_symbol_path(
                                    glyph.guide_symbol,
                                    pixel_size=max(glyph.font.pixelSize(), 1),
                                    left=float(line_x + glyph.x),
                                    baseline_y=float(baseline),
                                )
                            )
                        else:
                            glyph_path = QPainterPath()
                            glyph_path.addText(
                                float(line_x + glyph.x + glyph.path_offset),
                                baseline,
                                glyph.font,
                                glyph.text,
                            )
                            path.addPath(embolden_glyph_path(glyph_path, glyph.font))
                    left = float(line_x + run[0].x + run[0].path_offset)
                    right = float(line_x + run[-1].x + run[-1].advance)
                    ascent = max(glyph.metrics.ascent() for glyph in run)
                    descent = max(glyph.metrics.descent() for glyph in run)
                    rect = QRectF(
                        left,
                        float(baseline - ascent),
                        max(right - left, 1.0),
                        float(ascent + descent),
                    )
                    ports.paint_text_stack(painter, path, rect, run_title)
                    run_start = run_end
            line_top += line_height + layout.gap
        # 位图导唱符叠在文字之上（与歌词「先文字、后导唱符图片」的次序一致）；
        # 标题静态烘焙 → 动图固定取首帧。
        for glyph, rect in bitmap_units:
            symbol = glyph.guide_symbol
            frame = bitmap_guide_frame_at(
                symbol.bitmap_before_path if symbol is not None else None,
                None,
            )
            if frame is None or frame.image.isNull():
                continue
            target = rect.translated(float(pad_left), float(pad_top))
            _paint_title_bitmap_guide_decor(painter, glyph, frame.image, target)
            painter.drawImage(target, frame.image)
    finally:
        painter.end()
    return image, -pad_left, -pad_top


@dataclass(frozen=True)
class TitleOverlayLayer:
    """Layer-compositor adapter for one static title overlay block."""

    title_layout: TitleOverlayLayout
    title: TitleOverlay
    opacity: float
    ports: TitleRenderPorts = field(repr=False, compare=False)
    z_index: int = 0
    scope: str = SCOPE_LINE

    def active_window(self, ctx: LayerContext) -> list[tuple[int, int]]:
        return []

    def layout(self, ctx: LayerContext) -> TitleOverlayLayer:
        return self

    def static_key(self, ctx: LayerContext, layout: object) -> tuple:
        return (
            *title_overlay_layer_key(
                self.title_layout,
                self.title,
                fill_signature=self.ports.fill_signature,
            ),
            self.ports.raster_scale_key(ctx.device_pixel_ratio),
        )

    def bake(self, ctx: LayerContext, layout: object, key: Hashable) -> BakedLayer:
        image, dx, dy = build_title_overlay_layer(
            self.title_layout,
            self.title,
            ports=self.ports,
            device_pixel_ratio=ctx.device_pixel_ratio,
        )
        return BakedLayer(image=image, offset=QPointF(float(dx), float(dy)))

    def animate(self, ctx: LayerContext, layout: object) -> LayerAnimation:
        return LayerAnimation(
            top_left=QPointF(float(self.title_layout.x0), float(self.title_layout.y_top)),
            opacity=max(0.0, min(1.0, self.opacity)),
        )

    def paint_dynamic(self, painter: QPainter, ctx: LayerContext, layout: object) -> None:
        return

    def vertical_bounds(self, ctx: LayerContext, layout: object) -> tuple[int, int]:
        pad = max(
            (
                self.ports.visual_padding(glyph.title)
                for row in self.title_layout.glyph_rows
                for glyph in row
            ),
            default=self.ports.visual_padding(self.title),
        )
        top = -pad
        bottom = self.title_layout.block_h + pad
        for _glyph, rect in _title_bitmap_guide_rects(self.title_layout):
            top = min(top, rect.top())
            bottom = max(bottom, rect.bottom())
        return (
            int(math.floor(self.title_layout.y_top + top)),
            int(math.ceil(self.title_layout.y_top + bottom)),
        )


def make_title_overlay_layer(
    layout: TitleOverlayLayout,
    title: TitleOverlay,
    opacity: float,
    *,
    ports: TitleRenderPorts,
) -> TitleOverlayLayer:
    return TitleOverlayLayer(layout, title, opacity, ports)


def paint_title_overlay(
    painter: QPainter,
    img_w: int,
    img_h: int,
    track: TimingTrack,
    style: Style,
    overlay: Optional[TitleOverlay],
    opacity: float,
    *,
    compositor: LayerCompositor,
    ports: TitleRenderPorts,
) -> None:
    title = resolve_title_overlay(style, overlay)
    if title is None:
        return
    layout = layout_title_overlay(img_w, img_h, track, title, style=style)
    if layout is None:
        return
    compositor.paint_ordered(
        painter,
        LayerContext(t_ms=0, logical_w=img_w, logical_h=img_h),
        [make_title_overlay_layer(layout, title, opacity, ports=ports)],
    )


__all__ = [
    "TitleGlyphLayout",
    "TitleOverlayLayout",
    "TitleOverlayLayer",
    "TitleRenderPorts",
    "build_title_overlay_layer",
    "build_title_font",
    "build_title_latin_font",
    "layout_title_overlay",
    "make_title_font_for",
    "make_title_overlay_layer",
    "paint_title_text_stack",
    "paint_title_overlay",
    "title_block_origin",
    "title_block_y_top",
    "title_overlay_layer_key",
    "title_row_screen_x",
]
