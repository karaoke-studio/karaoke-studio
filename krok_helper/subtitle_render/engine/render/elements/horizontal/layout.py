"""Frame-independent glyph geometry for horizontal subtitle lines."""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Callable

from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import QFontMetrics, QPainterPath

from krok_helper.subtitle_render.domain.models import Style, effective_karaoke_animation
from krok_helper.subtitle_render.domain.timing import (
    RubyAnnotation,
    TimingLine,
    TimingTrack,
)
from krok_helper.subtitle_render.engine.guide import (
    bitmap_guide_content_size,
    guide_symbol_is_bitmap,
    render_line_with_guide_symbols,
    vector_glyph_width,
)
from krok_helper.subtitle_render.engine.layout.line.geometry import (
    line_has_role_labels,
)
from krok_helper.subtitle_render.engine.layout.page.pagination import (
    line_center_override,
)
from krok_helper.subtitle_render.engine.layout.line.style import (
    lane_count,
    style_for_line,
)
from krok_helper.subtitle_render.engine.render.effects import (
    glow_concentration_level,
    glow_radius,
    karaoke_state_signature,
    main_stroke2_width,
    ruby_vertical_extra,
    visual_text_padding,
)
from krok_helper.subtitle_render.engine.ruby import (
    active_rubies_for_line,
    build_ruby_font,
    effective_ruby_for_target,
    is_utopia_group_marker,
    ruby_for_char_index,
    ruby_char_gaps,
    ruby_main_text_slot_times,
    ruby_main_uses_base_timing,
    ruby_target_indices,
    ruby_visual_units_and_intervals,
)
from krok_helper.subtitle_render.engine.style.style_semantics import (
    effective_karaoke_colors,
)
from krok_helper.subtitle_render.engine.text.font_weight import embolden_glyph_path
from krok_helper.subtitle_render.engine.text import (
    GlyphLayout,
    TextLayout,
    build_font,
    build_latin_font,
    build_role_text_layout,
    build_text_layout,
    char_left_positions,
    letter_spacing,
    line_text_width,
    make_font_for,
    role_char_geometry_by_index,
)
from krok_helper.subtitle_render.engine.timing.timeline import (
    DisplayLine,
    compute_char_intervals,
)
from krok_helper.subtitle_render.engine.render.elements.horizontal.contracts import (
    FillSegment,
    LineLayout,
    RubyLayout,
)
from krok_helper.subtitle_render.engine.render.elements.horizontal.positioning import (
    line_lane_alignment,
    line_total_width,
    resolve_line_x_smart,
)
from krok_helper.subtitle_render.engine.render.elements.horizontal.wipe import (
    adjust_fill_release_edges,
    n3_char_wipe_ranges_by_index,
)
from krok_helper.subtitle_render.engine.style.style_semantics import (
    appearance_role_source,
)
from krok_helper.subtitle_render.sources.guide_symbols import (
    scaled_guide_symbol_path,
)


def signal_band_left_local(
    track: TimingTrack, line: TimingLine, style: Style
) -> float | None:
    """「真一组」渐变带左缘（行本地，相对文字起点 = 柱组左缘）。

    正文侧拓宽闸门：音量柱启用且装饰档为 auto/role、本行是信号宿主
    （段首行）、非 RTL，且柱体装饰源与正文第一角色**同源**——auto 档，
    或 role 档指向的方案悬空回退到 auto 口径。role 档解析到固定方案时
    正文渐变不参与（柱体画刷仍取并集跨度，见 Sayatoo 布局的
    ``signal_band``）。柱体侧（含 role 档）的跨度在 Painter 的
    ``_resolve_sayatoo_line_layouts`` 同口径计算。
    """
    if (
        not style.volume_enabled
        or style.volume_appearance_mode not in {"auto", "role"}
    ):
        return None
    if style.right_to_left != line.wipe_reverse:
        return None
    # 宿主口径 = 音量柱宿主行（段首基线 + volume_head_override 行级覆盖）：
    # 柱被行级覆盖关掉的段首行不做正文拓宽。
    from krok_helper.subtitle_render.engine.layout.display.signal import (
        volume_signal_head_context,
    )

    heads = volume_signal_head_context(track, style)
    if heads is None:
        return None
    index = next(
        (
            position
            for position, candidate in enumerate(track.lines)
            if candidate is line
        ),
        None,
    )
    if index is None or index not in heads:
        return None
    if (
        style.volume_appearance_mode == "role"
        and appearance_role_source(style, style.volume_role_name) is not None
    ):
        return None
    # 函数内导入：elements.signal 经 horizontal 包 __init__ 反向依赖本模块，
    # 顶层导入成环。
    from krok_helper.subtitle_render.engine.render.elements.signal import (
        signal_auto_basis,
        volume_signal_geometry,
        volume_style,
    )

    # auto 档柱体尺寸与 Painter 布局/绘制同基准（主轨最高频角色方案；
    # paint/边界分析入口已登记，未登记场景回退全局主样式）。
    geometry = volume_signal_geometry(
        volume_style(style, auto_basis=signal_auto_basis(style))
    )
    return (
        float(style.volume_offset_x)
        - geometry.group_width
        + geometry.stroke_extent
    )


def apply_signal_band_left(
    role_fill_rects: dict[str | None, QRectF],
    text_layout: TextLayout,
    band_left: float,
) -> None:
    """把第一角色（首个非空白字符所属角色）的填充跨度左缘拓宽到带左缘。

    只有横向渐变消费 role 跨度的 x（``fill_brush_rect`` 对
    gradient_horizontal 取 horizontal 跨度），竖向渐变/分段/图片不受
    影响——主文字各消费点与 ruby 的共享横向盒都过这一处拓宽。
    """
    first_role = next(
        (glyph.role_label for glyph in text_layout.glyphs if glyph.text.strip()),
        None,
    )
    rect = role_fill_rects.get(first_role)
    if rect is None:
        return
    rect.setLeft(min(float(rect.left()), float(band_left)))


@dataclass(frozen=True)
class HorizontalLayoutPorts:
    """Painter-owned capabilities needed to build horizontal line layouts."""

    char_layout_width: Callable[..., int]
    layout_rubies: Callable[..., list[RubyLayout]]
    role_ruby_vertical_extra: Callable[..., int]


def measure_display_line_horizontal_bounds(
    track: TimingTrack,
    style: Style,
    display_line: DisplayLine,
    logical_w: int,
) -> tuple[float, float]:
    """Measure one display line's authored horizontal text bounds."""

    line = display_line.line
    line_style = style_for_line(style, line)
    total_w = line_total_width(line, line_style, track.rubies)
    lane = display_line.lane if line_style.dual_line_layout else None
    x0 = resolve_line_x_smart(
        logical_w,
        total_w,
        track,
        line,
        line_style,
        lane,
        center_override=line_center_override(track, line, line_style),
    )
    return float(x0), float(x0 + total_w)


def bitmap_guide_is_no_wipe(symbol: object | None) -> bool:
    return guide_symbol_is_bitmap(symbol) and not bool(
        getattr(symbol, "bitmap_after_path", None)
    )

def karaoke_fill_segments(
    char_widths: list[int],
    intervals: list[tuple[int, int]],
    ink_x_ranges: list[tuple[int, int]],
    active_rubies: list[RubyAnnotation],
    line: TimingLine,
    *,
    release_x_ranges: list[tuple[int, int]] | None = None,
    layout_x_ranges: list[tuple[int, int]] | None = None,
    ruby_main_progress_mode: str = "checkpoint_segments",
    wipe_reverse: bool = False,
    karaoke_effect: str = "none",
) -> list[FillSegment]:
    """构造走字分段。``ink_x_ranges`` 为各字符的墨水边界（非 advance 框），
    扫光锋面据此推进，确保不扫过字形两侧的透明空白（见 :func:`_char_ink_x_ranges`）。

    ``wipe_reverse``（加载入口镜像理顺过的整行逆序行）时按**演唱时间序**返回
    分段：段列表是扫光锋面的迭代序，正常行 = 字符序；逆序行把**时间窗口与
    字符位置反序配对**（位置 i 用窗口 n-1-i）后反转段表——行尾几何段携带
    最早窗口、几何自右向左递降，与 RTL 文本同构，复用 rtl 扫光管线。
    N3 的 ``AdjustWipeEnd`` 重叠收缩在反转**之后**执行，保证相邻对的
    "后继"是时间后继；该公式本身方向感知，无需另改。"""
    segments: list[FillSegment] = []
    release_x_ranges = release_x_ranges or ink_x_ranges
    layout_x_ranges = layout_x_ranges or release_x_ranges
    index = 0
    while index < len(char_widths):
        if index < len(line.chars) and bitmap_guide_is_no_wipe(
            line.chars[index].vector_glyph
        ):
            index += 1
            continue
        ruby = ruby_for_char_index(active_rubies, line, intervals, index)
        ruby_indices = (
            ruby_target_indices(ruby, line, intervals) if ruby is not None else []
        )
        # SUG uses a pause-only ruby over a linked English phrase as a
        # non-rendering group marker.  Utopia must still consume that ruby to
        # drop the whole phrase together, but it is not pronunciation data and
        # must not replace the phrase's real per-syllable TimingChar clock with
        # one linear start-to-end wipe.
        if (
            ruby is None
            or is_utopia_group_marker(ruby)
            or ruby_main_uses_base_timing(line, ruby_indices)
        ):
            left, right = ink_x_ranges[index]
            release_left, release_right = release_x_ranges[index]
            layout_left, layout_right = layout_x_ranges[index]
            start, end = intervals[index]
            segments.append(
                FillSegment(
                    left=left,
                    right=right,
                    release_left=release_left,
                    release_right=release_right,
                    layout_left=layout_left,
                    layout_right=layout_right,
                    start_ms=start,
                    end_ms=end,
                    indices=(index,),
                )
            )
            index += 1
            continue

        indices = [i for i in ruby_indices if 0 <= i < len(ink_x_ranges)]
        if not indices:
            left, right = ink_x_ranges[index]
            release_left, release_right = release_x_ranges[index]
            layout_left, layout_right = layout_x_ranges[index]
            start, end = intervals[index]
            segments.append(
                FillSegment(
                    left=left,
                    right=right,
                    release_left=release_left,
                    release_right=release_right,
                    layout_left=layout_left,
                    layout_right=layout_right,
                    start_ms=start,
                    end_ms=end,
                    indices=(index,),
                )
            )
            index += 1
            continue

        effective_ruby = effective_ruby_for_target(ruby, indices, intervals)
        reading_unit_mode = (
            ruby_main_progress_mode == "reading_units"
            and bool(ruby_visual_units_and_intervals(effective_ruby))
        )
        if reading_unit_mode:
            base_count = len(indices)
            for base_index, target_index in enumerate(indices):
                left, right = ink_x_ranges[target_index]
                release_left, release_right = release_x_ranges[target_index]
                layout_left, layout_right = layout_x_ranges[target_index]
                slot_start, slot_end = ruby_main_text_slot_times(
                    effective_ruby, base_index, base_count
                )
                segments.append(
                    FillSegment(
                        left=left,
                        right=right,
                        release_left=release_left,
                        release_right=release_right,
                        layout_left=layout_left,
                        layout_right=layout_right,
                        start_ms=slot_start,
                        end_ms=slot_end,
                        ruby=effective_ruby,
                        indices=(target_index,),
                        ruby_base_index=base_index,
                        ruby_base_count=base_count,
                    )
                )
        else:
            left = min(ink_x_ranges[i][0] for i in indices)
            right = max(ink_x_ranges[i][1] for i in indices)
            release_left = min(release_x_ranges[i][0] for i in indices)
            release_right = max(release_x_ranges[i][1] for i in indices)
            layout_left = min(layout_x_ranges[i][0] for i in indices)
            layout_right = max(layout_x_ranges[i][1] for i in indices)
            segments.append(
                FillSegment(
                    left=left,
                    right=right,
                    release_left=release_left,
                    release_right=release_right,
                    layout_left=layout_left,
                    layout_right=layout_right,
                    ruby=effective_ruby,
                    indices=tuple(indices),
                )
            )
        index = max(indices) + 1
    if wipe_reverse:
        segments.reverse()
    return [
        replace(segment, karaoke_effect=karaoke_effect)
        for segment in adjust_fill_release_edges(segments)
    ]


def fixed_line_geometry(style: Style) -> tuple[int, int, int, int]:
    font = build_font(style)
    metrics = QFontMetrics(font)
    ruby_metrics = QFontMetrics(build_ruby_font(style))
    ruby_extra = ruby_vertical_extra(style, ruby_metrics)
    if style.layout_semantics == "n3_1074":
        font_size = max(int(style.font_size_px), 1)
        edge = max(int(style.stroke_width_px), 0)
        main_h = font_size + edge
        metric_total = max(metrics.ascent() + metrics.descent(), 1)
        main_descent = (
            font_size * max(metrics.descent(), 0) // metric_total
            + edge // 2
        )
        main_descent = min(max(main_descent, 0), main_h)
        main_ascent = main_h - main_descent
        return main_h, main_ascent, main_descent, 0
    pad = visual_text_padding(style)
    main_h = metrics.ascent() + metrics.descent() + pad * 2
    return main_h, metrics.ascent() + pad, metrics.descent() + pad, ruby_extra


def resolve_baseline_y(
    metrics: QFontMetrics,
    img_h: int,
    style: Style,
    ruby_metrics: QFontMetrics | None = None,
) -> int:
    pos = style.line_y_position
    margin = style.line_y_margin_px
    if style.layout_semantics == "n3_1074":
        main_h, main_ascent, main_descent, ruby_extra = fixed_line_geometry(style)
        if pos == "top":
            return margin + ruby_extra + main_ascent
        if pos == "center":
            return (img_h - main_h) // 2 + main_ascent
        return img_h - margin - main_descent
    # 行网格是页级量：legacy 语义下注音预留按样式恒定保留（与双行
    # fixed_line_geometry 一致），与某一行是否实际带注音无关，否则有无
    # 注音的行基线会差一个注音高度，翻页时上下乱跳。
    del ruby_metrics
    pad = visual_text_padding(style)
    ruby_extra = ruby_vertical_extra(
        style, QFontMetrics(build_ruby_font(style))
    )
    if pos == "top":
        return margin + ruby_extra + pad + metrics.ascent()
    if pos == "center":
        block_h = metrics.height() + ruby_extra + pad * 2
        return (img_h - block_h) // 2 + ruby_extra + pad + metrics.ascent()
    return img_h - margin - pad - metrics.descent()


def resolve_display_baselines(
    img_h: int,
    track: TimingTrack,
    display_lines: list[DisplayLine],
    style: Style,
) -> dict[int, int]:
    if not style.dual_line_layout:
        font = build_font(style)
        metrics = QFontMetrics(font)
        # 单行 legacy 的注音预留已由 resolve_baseline_y 按样式恒定保留，
        # 不再依赖当前行是否带注音。
        baseline = resolve_baseline_y(metrics, img_h, style)
        if style.line_horizontal_layout == "per_row":
            baseline += style.row1_offset_y
        return {0: baseline}

    main_h, main_ascent, main_descent, ruby_extra = fixed_line_geometry(style)
    gap = int(style.line_gap_px)
    margin = style.line_y_margin_px
    lanes = lane_count(style)
    step = main_h + gap

    if style.line_y_position == "top":
        first_baseline = margin + ruby_extra + main_ascent
    elif style.line_y_position == "center":
        total_h = main_h * lanes + gap * (lanes - 1)
        first_baseline = (img_h - total_h) // 2 + main_ascent
    else:
        last_baseline = img_h - margin - main_descent
        first_baseline = last_baseline - step * (lanes - 1)
    baselines = {lane: first_baseline + step * lane for lane in range(lanes)}
    if style.line_horizontal_layout == "per_row":
        if 0 in baselines:
            baselines[0] += style.row1_offset_y
        if 1 in baselines:
            baselines[1] += style.row2_offset_y
    return baselines


def bitmap_guide_anchor_descent(glyph: GlyphLayout) -> int:
    if glyph.style.layout_semantics == "n3_1074":
        return fixed_line_geometry(glyph.style)[2]
    return max(int(glyph.metrics.descent()), 0)


def glyph_path(glyph: GlyphLayout, baseline_y: int) -> QPainterPath:
    if glyph.vector_glyph is not None:
        if guide_symbol_is_bitmap(glyph.vector_glyph):
            return QPainterPath()
        return scaled_guide_symbol_path(
            glyph.vector_glyph,
            pixel_size=max(int(glyph.font.pixelSize()), 1),
            left=float(glyph.left),
            baseline_y=float(baseline_y),
        )
    path = QPainterPath()
    path.addText(
        float(glyph.left + glyph.path_offset_x),
        float(baseline_y),
        glyph.font,
        glyph.text,
    )
    return embolden_glyph_path(path, glyph.font)


def role_visual_text_padding(layout: TextLayout) -> int:
    if not layout.glyphs:
        return 0
    return max(visual_text_padding(glyph.style) for glyph in layout.glyphs)


def resolve_role_baseline_y(
    layout: TextLayout,
    img_h: int,
    style: Style,
    ruby_extra: int = 0,
) -> int:
    pos = style.line_y_position
    margin = style.line_y_margin_px
    pad = role_visual_text_padding(layout)
    ruby_extra = max(int(ruby_extra), 0)
    if pos == "top":
        return margin + ruby_extra + pad + layout.ascent
    if pos == "center":
        block_h = layout.height + ruby_extra + pad * 2
        return (img_h - block_h) // 2 + ruby_extra + pad + layout.ascent
    return img_h - margin - pad - layout.descent


def clamp_role_baseline_y(
    baseline_y: int,
    layout: TextLayout,
    img_h: int,
    style: Style,
    ruby_extra: int = 0,
) -> int:
    pad = role_visual_text_padding(layout)
    ruby_extra = max(int(ruby_extra), 0)
    min_y = ruby_extra + pad + layout.ascent
    max_y = img_h - pad - layout.descent
    if max_y < min_y:
        return min_y
    return max(min_y, min(max_y, baseline_y))


def glyph_run_signature(glyph: GlyphLayout) -> tuple:
    colors = effective_karaoke_colors(glyph.style)
    return (
        getattr(glyph, "role_label", None),
        karaoke_state_signature(colors.before),
        karaoke_state_signature(colors.after),
        glyph.style.shadow_offset_x,
        glyph.style.shadow_offset_y,
        glyph.style.stroke_width_px,
        glyph.style.stroke2_width_px,
        glyph.style.decoration_kind,
        glow_radius(glyph.style, after=False),
        glow_radius(glyph.style, after=True),
        glow_concentration_level(glyph.style),
    )


def glyph_runs(layout: TextLayout) -> list[list[GlyphLayout]]:
    runs: list[list[GlyphLayout]] = []
    current: list[GlyphLayout] = []
    current_signature: tuple | None = None
    signature_cache: dict[int, tuple] = {}
    for glyph in layout.glyphs:
        style_id = id(glyph.style)
        style_signature = signature_cache.get(style_id)
        if style_signature is None:
            style_signature = glyph_run_signature(glyph)[1:]
            signature_cache[style_id] = style_signature
        signature = (glyph.role_label, *style_signature)
        if not current or signature == current_signature:
            current.append(glyph)
            current_signature = signature
            continue
        runs.append(current)
        current = [glyph]
        current_signature = signature
    if current:
        runs.append(current)
    return runs


def role_glyphs_by_index(
    line: TimingLine,
    layout: TextLayout,
) -> list[GlyphLayout | None]:
    """Map source character indices to their horizontal glyph layouts."""

    glyphs: list[GlyphLayout | None] = [None for _ in line.chars]
    for glyph in layout.glyphs:
        if 0 <= glyph.index < len(glyphs):
            glyphs[glyph.index] = glyph
    return glyphs


def glyph_runs_for_indices(
    glyphs_by_index: list[GlyphLayout | None],
    indices: list[int],
) -> list[list[GlyphLayout]]:
    """Group selected glyphs by the same paint-state contract as full lines."""

    runs: list[list[GlyphLayout]] = []
    current: list[GlyphLayout] = []
    current_signature: tuple | None = None
    signature_cache: dict[int, tuple] = {}
    for index in indices:
        if not (0 <= index < len(glyphs_by_index)):
            continue
        glyph = glyphs_by_index[index]
        if glyph is None:
            continue
        style_id = id(glyph.style)
        style_signature = signature_cache.get(style_id)
        if style_signature is None:
            style_signature = glyph_run_signature(glyph)[1:]
            signature_cache[style_id] = style_signature
        signature = (glyph.role_label, *style_signature)
        if current and signature != current_signature:
            runs.append(current)
            current = []
        current.append(glyph)
        current_signature = signature
    if current:
        runs.append(current)
    return runs


def role_char_ink_ranges_by_index(
    line: TimingLine,
    layout: TextLayout,
    char_x_ranges: list[tuple[int, int]],
) -> list[tuple[int, int]]:
    """Return each role-styled character's horizontal ink bounds.

    Missing and whitespace glyphs fall back to a zero-width range at the
    character advance box's left edge, matching the plain-text wipe contract.
    Bitmap guide glyphs have no outline path, so their ink is the avatar's
    content rectangle (``left + margin_left`` .. ``+ content width``) — the
    same rect the painter draws and the GPU backend uses as ``bitmapRect``.
    Without it the avatar's wipe segment is zero-width and its after-image
    reveal degenerates to the following text char's scanline (or never
    appears on avatar-only lines).
    ``line`` remains in the signature for compatibility with the established
    layout call boundary.
    """

    ranges: list[tuple[int, int]] = [(left, left) for left, _ in char_x_ranges]
    for glyph in layout.glyphs:
        if not (0 <= glyph.index < len(ranges)):
            continue
        text = glyph.text
        left = glyph.left
        symbol = glyph.vector_glyph
        if symbol is not None and guide_symbol_is_bitmap(symbol):
            content_left = left + int(symbol.bitmap_margin_left_px)
            content_width, _content_height = bitmap_guide_content_size(
                symbol, glyph.style
            )
            content_right = content_left + max(int(content_width), 1)
            ranges[glyph.index] = (content_left, max(content_right, content_left))
            continue
        if symbol is not None and not guide_symbol_is_bitmap(symbol):
            # 矢量导唱符：墨迹是缩放后的符号 path，量少直接构造。
            bounds = glyph_path(glyph, 0).boundingRect()
            if bounds.isEmpty():
                ranges[glyph.index] = (left, left)
            else:
                ranges[glyph.index] = (
                    int(math.floor(bounds.left())),
                    int(math.ceil(bounds.right())),
                )
            continue
        if not text or text.isspace():
            ranges[glyph.index] = (left, left)
            continue
        # 文本墨迹盒按字体签名进程级缓存（GlyphLayout.ink_box），平移合成
        # 与 glyph_path(...).boundingRect() 逐位同值，免逐字符 path 构造。
        ink_box = glyph.ink_box
        if ink_box is None:
            ranges[glyph.index] = (left, left)
        else:
            box_left = glyph.left + glyph.path_offset_x + ink_box[0]
            box_right = glyph.left + glyph.path_offset_x + ink_box[2]
            ranges[glyph.index] = (
                int(math.floor(box_left)),
                int(math.ceil(box_right)),
            )
    return ranges


def glyph_is_bitmap_guide(glyph: GlyphLayout) -> bool:
    return guide_symbol_is_bitmap(glyph.vector_glyph)


def text_glyph_runs(
    layout: TextLayout,
    has_inline_styles: bool,
) -> list[list[GlyphLayout]]:
    runs = [layout.glyphs] if not has_inline_styles else glyph_runs(layout)
    result: list[list[GlyphLayout]] = []
    for run in runs:
        text_run = [glyph for glyph in run if not glyph_is_bitmap_guide(glyph)]
        if text_run:
            result.append(text_run)
    return result


def bitmap_guide_glyphs(layout: TextLayout) -> list[GlyphLayout]:
    return [glyph for glyph in layout.glyphs if glyph_is_bitmap_guide(glyph)]


def glyph_run_path(glyphs: list[GlyphLayout], baseline_y: int) -> QPainterPath:
    path = QPainterPath()
    path.setFillRule(Qt.FillRule.WindingFill)
    for glyph in glyphs:
        path.addPath(glyph_path(glyph, baseline_y))
    return path


def glyph_run_rect(glyphs: list[GlyphLayout], baseline_y: int) -> QRectF:
    left = min(glyph.left for glyph in glyphs)
    right = max(glyph.left + glyph.width for glyph in glyphs)
    ascent = max(glyph.metrics.ascent() for glyph in glyphs)
    descent = max(glyph.metrics.descent() for glyph in glyphs)
    return QRectF(
        float(left),
        float(baseline_y - ascent),
        float(max(right - left, 1)),
        float(max(ascent + descent, 1)),
    )


def glyph_ink_bounds(
    glyphs: list[GlyphLayout], baseline_y: int
) -> tuple[float, float, float, float] | None:
    """Return the visible ink union (left, right, top, bottom) for glyphs."""

    ink_left: float | None = None
    ink_right: float | None = None
    ink_top: float | None = None
    ink_bottom: float | None = None
    for glyph in glyphs:
        if glyph_is_bitmap_guide(glyph):
            symbol = glyph.vector_glyph
            content_width, content_height = bitmap_guide_content_size(
                symbol, glyph.style
            )
            left = float(glyph.left + int(symbol.bitmap_margin_left_px))
            right = left + float(max(int(content_width), 1))
            bottom = float(
                baseline_y
                + bitmap_guide_anchor_descent(glyph)
                - int(symbol.bitmap_margin_bottom_px)
            )
            top = bottom - float(max(int(content_height), 1))
        elif glyph.ink_box is not None:
            # 文本墨迹盒（字体签名进程级缓存）平移合成，免逐字符 path。
            base_x = glyph.left + glyph.path_offset_x
            left = base_x + glyph.ink_box[0]
            right = base_x + glyph.ink_box[2]
            top = baseline_y + glyph.ink_box[1]
            bottom = baseline_y + glyph.ink_box[3]
        else:
            if glyph.vector_glyph is None:
                continue
            bounds = glyph_path(glyph, baseline_y).boundingRect()
            if bounds.isEmpty():
                continue
            left = float(bounds.left())
            right = float(bounds.right())
            top = float(bounds.top())
            bottom = float(bounds.bottom())
        ink_left = left if ink_left is None else min(ink_left, left)
        ink_right = right if ink_right is None else max(ink_right, right)
        ink_top = top if ink_top is None else min(ink_top, top)
        ink_bottom = bottom if ink_bottom is None else max(ink_bottom, bottom)
    if (
        ink_left is None
        or ink_right is None
        or ink_top is None
        or ink_bottom is None
        or ink_right <= ink_left
    ):
        return None
    return ink_left, ink_right, ink_top, ink_bottom


def glyph_ink_x_bounds(
    glyphs: list[GlyphLayout], baseline_y: int
) -> tuple[float, float] | None:
    """Return the visible horizontal ink union for a glyph collection."""

    bounds = glyph_ink_bounds(glyphs, baseline_y)
    if bounds is None:
        return None
    return bounds[0], bounds[1]


def n3_main_fill_rect(layout: TextLayout, baseline_y: int) -> QRectF:
    """Return the shared brush area for one main-text line.

    Both extents follow the union of visible glyph ink so vertical gradients
    and MilleFeuille splits stay aligned with the drawn glyphs: anchoring to
    the em-box metric box systematically displaced the bands for faces whose
    ink is taller than the em (Meiryo, Noto Sans JP, ...), and the wrapping
    band texture then painted the glyph tops in the bottom band colour.  The
    union is padded by the maximal symmetric stroke extent so widened
    outlines do not wrap the band texture; advances, whitespace, and letter
    spacing stay excluded, and bitmap guides participate through their
    visible content box.  Lines without visible ink fall back to the N3
    ``DrawLineInfo`` metric box.
    """
    glyphs = layout.glyphs
    if not glyphs:
        return QRectF(layout.line_rect)

    ink = glyph_ink_bounds(glyphs, baseline_y)
    if ink is None:
        return _n3_main_metrics_fill_rect(layout, baseline_y)
    ink_left, ink_right, ink_top, ink_bottom = ink
    pad = role_visual_text_padding(layout)
    return QRectF(
        ink_left,
        ink_top - pad,
        max(ink_right - ink_left, 1.0),
        float(max(ink_bottom - ink_top + pad * 2, 1.0)),
    )


def _n3_main_metrics_fill_rect(layout: TextLayout, baseline_y: int) -> QRectF:
    """Metric fallback of :func:`n3_main_fill_rect` for ink-free lines."""

    glyphs = layout.glyphs
    first = glyphs[0]
    font_size = max(int(first.font.pixelSize()), 1)
    metric_total = max(first.metrics.ascent() + first.metrics.descent(), 1)
    descent = font_size * max(first.metrics.descent(), 0) // metric_total
    brush_style = first.brush_style or first.style
    draw_edge = max(int(first.style.stroke_width_px), 0)
    anchor_edge = max(int(brush_style.stroke_width_px), 0)
    anchor_edge2 = main_stroke2_width(brush_style)
    draw_bottom = float(baseline_y + descent + draw_edge // 2)
    draw_height = max(
        max(int(glyph.font.pixelSize()), 1)
        + max(int(glyph.style.stroke_width_px), 0)
        for glyph in glyphs
    )
    draw_top = draw_bottom - float(draw_height)
    inset = float((anchor_edge + anchor_edge2) // 2)
    top = draw_top + inset
    bottom = draw_bottom - inset

    return QRectF(
        float(layout.line_rect.left()),
        top,
        max(float(layout.line_rect.width()), 1.0),
        float(max(bottom - top, 1.0)),
    )


def role_main_fill_rects(
    layout: TextLayout, baseline_y: int
) -> dict[str | None, QRectF]:
    """Return one shared horizontal gradient box per role in the line.

    Non-contiguous glyphs carrying the same role label deliberately share a
    single ink union.  This lets every role's independent paint traverse its
    complete 0..100% gradient instead of sampling only its position within the
    whole line.  The N3 vertical brush extent remains common to all roles.
    """

    line_rect = n3_main_fill_rect(layout, baseline_y)
    grouped: dict[str | None, list[GlyphLayout]] = {}
    for glyph in layout.glyphs:
        grouped.setdefault(glyph.role_label, []).append(glyph)
    result: dict[str | None, QRectF] = {}
    for role_label, glyphs in grouped.items():
        bounds = glyph_ink_x_bounds(glyphs, baseline_y)
        rect = QRectF(line_rect)
        if bounds is not None:
            left, right = bounds
            rect.setLeft(left)
            rect.setRight(right)
        result[role_label] = rect
    return result


def layout_line_uncached(
    track: TimingTrack,
    line: TimingLine,
    style: Style,
    img_w: int,
    img_h: int,
    ports: HorizontalLayoutPorts,
    *,
    baseline_y: int | None = None,
    line_x: int | None = None,
    lane: int | None = None,
    line_plan: object | None = None,
) -> LineLayout | None:
    render_line = (
        line_plan.render_line
        if line_plan is not None
        else render_line_with_guide_symbols(line)
    )
    resolved_intervals = (
        line_plan.resolved_intervals if line_plan is not None else None
    )
    center_override = line_plan.center_override if line_plan is not None else None
    if line_has_role_labels(render_line):
        return layout_role_line(
            track,
            render_line,
            style,
            img_w,
            img_h,
            ports,
            baseline_y=baseline_y,
            line_x=line_x,
            lane=lane,
            source_line=line,
            resolved_intervals=resolved_intervals,
            center_override=center_override,
        )
    return layout_plain_line(
        track,
        render_line,
        style,
        img_w,
        img_h,
        ports,
        baseline_y=baseline_y,
        line_x=line_x,
        lane=lane,
        source_line=line,
        resolved_intervals=resolved_intervals,
        center_override=center_override,
    )


def layout_plain_line(
    track: TimingTrack,
    line: TimingLine,
    style: Style,
    img_w: int,
    img_h: int,
    ports: HorizontalLayoutPorts,
    *,
    baseline_y: int | None = None,
    line_x: int | None = None,
    lane: int | None = None,
    source_line: TimingLine | None = None,
    resolved_intervals: tuple[tuple[int, int], ...] | None = None,
    center_override: bool | None = None,
) -> LineLayout:
    font = build_font(style)
    metrics = QFontMetrics(font)
    latin_font = build_latin_font(style)
    font_for = make_font_for(style, font, latin_font)
    latin_metrics = QFontMetrics(latin_font) if font_for is not None else metrics
    source_line = source_line or line
    active_rubies = active_rubies_for_line(track.rubies, source_line)
    ruby_font = build_ruby_font(style)
    ruby_metrics = QFontMetrics(ruby_font) if active_rubies else None

    char_widths = [
        (
            vector_glyph_width(char.vector_glyph, style)
            if char.vector_glyph is not None
            else ports.char_layout_width(
                char.text,
                font,
                metrics,
                latin_metrics,
                font_for,
                style,
            )
        )
        for char in line.chars
    ]
    intervals = (
        list(resolved_intervals)
        if resolved_intervals is not None
        else compute_char_intervals(line, char_widths)
    )
    if line.wipe_reverse:
        intervals.reverse()
    char_gaps, ruby_left_ext, ruby_right_ext = ruby_char_gaps(
        line,
        char_widths,
        active_rubies,
        style,
        intervals,
    )
    total_w = line_text_width(char_widths, style) + sum(char_gaps)
    left_ext = ruby_left_ext
    right_ext = ruby_right_ext
    if center_override is None:
        center_override = line_center_override(track, source_line, style)
    n3_main_center = (
        style.layout_semantics == "n3_1074"
        and not center_override
        and style.line_horizontal_layout == "asymmetric"
        and line_lane_alignment(track, source_line, style, lane) == "center"
    )
    x0 = (
        line_x
        if line_x is not None
        else resolve_line_x_smart(
            img_w,
            total_w,
            track,
            source_line,
            style,
            lane,
            center_override=False,
        )
        if n3_main_center
        else resolve_line_x_smart(
            img_w,
            total_w + left_ext + right_ext,
            track,
            source_line,
            style,
            lane,
            center_override=center_override,
        )
        + left_ext
    )
    y = (
        baseline_y
        if baseline_y is not None
        else resolve_baseline_y(metrics, img_h, style, ruby_metrics)
    )
    text_rtl = style.right_to_left
    # 加载入口已理顺的整行逆序行：摆放仍按文本方向，仅走字方向反向（XOR）。
    char_lefts = char_left_positions(
        char_widths,
        x0,
        text_rtl,
        letter_spacing(style),
        char_gaps=char_gaps,
        n3_no_backtracking=style.layout_semantics == "n3_1074",
    )
    char_x_ranges = [
        (left, left + width)
        for left, width in zip(char_lefts, char_widths)
    ]
    text_layout = build_text_layout(
        line,
        style,
        x0=x0,
        baseline_y=y,
        inline_styles=False,
        char_gaps=char_gaps,
    )
    ink_x_ranges = role_char_ink_ranges_by_index(
        line,
        text_layout,
        char_x_ranges,
    )
    wipe_x_ranges = n3_char_wipe_ranges_by_index(
        line,
        text_layout,
        char_x_ranges,
        ink_x_ranges,
    )
    fill_segments = karaoke_fill_segments(
        char_widths,
        intervals,
        ink_x_ranges,
        active_rubies,
        line,
        release_x_ranges=wipe_x_ranges,
        layout_x_ranges=char_x_ranges,
        ruby_main_progress_mode=style.ruby_main_progress_mode,
        wipe_reverse=line.wipe_reverse,
        karaoke_effect=effective_karaoke_animation(style),
    )
    line_rect = QRectF(
        float(x0),
        float(y - metrics.ascent()),
        float(total_w),
        float(metrics.height()),
    )
    colors = effective_karaoke_colors(style)
    band_left_local = signal_band_left_local(track, source_line, style)
    ruby_layouts = tuple(
        ports.layout_rubies(
            ruby_metrics,
            line,
            intervals,
            char_x_ranges,
            y,
            active_rubies,
            style,
            main_ascent_px=text_layout.ascent,
            text_layout=text_layout,
            ruby_font=ruby_font,
            signal_band_left=(
                float(x0) + band_left_local
                if band_left_local is not None
                else None
            ),
        )
        if ruby_metrics is not None
        else ()
    )
    return LineLayout(
        text_layout=text_layout,
        font=font,
        metrics=metrics,
        latin_font=latin_font,
        font_for=font_for,
        active_rubies=active_rubies,
        ruby_font=ruby_font,
        ruby_metrics=ruby_metrics,
        char_widths=char_widths,
        total_w=total_w,
        x0=x0,
        baseline_y=y,
        intervals=intervals,
        char_lefts=char_lefts,
        char_x_ranges=char_x_ranges,
        fill_segments=fill_segments,
        line_rect=line_rect,
        colors=colors,
        rtl=text_rtl != line.wipe_reverse,
        has_inline_styles=False,
        ink_x_ranges=ink_x_ranges,
        ruby_layouts=ruby_layouts,
        render_line=line,
        signal_band_left=(
            float(x0) + band_left_local
            if band_left_local is not None
            else None
        ),
    )


def layout_role_line(
    track: TimingTrack,
    line: TimingLine,
    style: Style,
    img_w: int,
    img_h: int,
    ports: HorizontalLayoutPorts,
    *,
    baseline_y: int | None = None,
    line_x: int | None = None,
    lane: int | None = None,
    source_line: TimingLine | None = None,
    resolved_intervals: tuple[tuple[int, int], ...] | None = None,
    center_override: bool | None = None,
) -> LineLayout | None:
    has_shared_baseline = baseline_y is not None
    source_line = source_line or line
    active_rubies = active_rubies_for_line(track.rubies, source_line)
    ruby_font = build_ruby_font(style)
    ruby_metrics = QFontMetrics(ruby_font) if active_rubies else None
    measure_layout = build_role_text_layout(line, style, x0=0, baseline_y=0)
    if not measure_layout.glyphs:
        return None
    char_widths, _measure_ranges = role_char_geometry_by_index(line, measure_layout)
    intervals = (
        list(resolved_intervals)
        if resolved_intervals is not None
        else compute_char_intervals(line, char_widths)
    )
    if line.wipe_reverse:
        intervals.reverse()
    ruby_extra = ports.role_ruby_vertical_extra(
        line,
        active_rubies,
        intervals,
        style,
    )
    if style.layout_semantics != "n3_1074":
        # 与单行普通行一致：注音预留按样式兜底，无注音的 role 行也保留
        # 同样的高度，避免有无注音的行基线来回跳。
        ruby_extra = max(
            ruby_extra,
            ruby_vertical_extra(style, QFontMetrics(build_ruby_font(style))),
        )
    char_gaps, ruby_left_ext, ruby_right_ext = ruby_char_gaps(
        line,
        char_widths,
        active_rubies,
        style,
        intervals,
    )
    left_ext = ruby_left_ext
    right_ext = ruby_right_ext
    total_w = measure_layout.total_width + sum(char_gaps)
    if center_override is None:
        center_override = line_center_override(track, source_line, style)
    n3_main_center = (
        style.layout_semantics == "n3_1074"
        and not center_override
        and style.line_horizontal_layout == "asymmetric"
        and line_lane_alignment(track, source_line, style, lane) == "center"
    )
    x0 = (
        line_x
        if line_x is not None
        else resolve_line_x_smart(
            img_w,
            total_w,
            track,
            source_line,
            style,
            lane,
            center_override=False,
        )
        if n3_main_center
        else resolve_line_x_smart(
            img_w,
            total_w + left_ext + right_ext,
            track,
            source_line,
            style,
            lane,
            center_override=center_override,
        )
        + left_ext
    )
    y = (
        baseline_y
        if baseline_y is not None
        else resolve_role_baseline_y(measure_layout, img_h, style, ruby_extra)
    )
    if not has_shared_baseline:
        y = clamp_role_baseline_y(y, measure_layout, img_h, style, ruby_extra)
    text_layout = build_role_text_layout(
        line,
        style,
        x0=x0,
        baseline_y=y,
        char_gaps=char_gaps,
    )
    char_widths, char_x_ranges = role_char_geometry_by_index(line, text_layout)
    intervals = (
        list(resolved_intervals)
        if resolved_intervals is not None
        else compute_char_intervals(line, char_widths)
    )
    if line.wipe_reverse:
        intervals.reverse()
    ink_x_ranges = role_char_ink_ranges_by_index(
        line,
        text_layout,
        char_x_ranges,
    )
    wipe_x_ranges = n3_char_wipe_ranges_by_index(
        line,
        text_layout,
        char_x_ranges,
        ink_x_ranges,
    )
    fill_segments = karaoke_fill_segments(
        char_widths,
        intervals,
        ink_x_ranges,
        active_rubies,
        line,
        release_x_ranges=wipe_x_ranges,
        layout_x_ranges=char_x_ranges,
        ruby_main_progress_mode=style.ruby_main_progress_mode,
        wipe_reverse=line.wipe_reverse,
        karaoke_effect=effective_karaoke_animation(style),
    )
    band_left_local = signal_band_left_local(track, source_line, style)
    ruby_layouts = tuple(
        ports.layout_rubies(
            ruby_metrics,
            line,
            intervals,
            char_x_ranges,
            y,
            active_rubies,
            style,
            main_ascent_px=text_layout.ascent,
            text_layout=text_layout,
            ruby_font=ruby_font,
            signal_band_left=(
                float(x0) + band_left_local
                if band_left_local is not None
                else None
            ),
        )
        if ruby_metrics is not None
        else ()
    )
    return LineLayout(
        text_layout=text_layout,
        active_rubies=active_rubies,
        font=text_layout.glyphs[0].font,
        metrics=text_layout.glyphs[0].metrics,
        latin_font=build_latin_font(style),
        font_for=None,
        ruby_font=ruby_font,
        ruby_metrics=ruby_metrics,
        char_widths=char_widths,
        total_w=text_layout.total_width,
        x0=int(text_layout.line_rect.left()),
        baseline_y=y,
        intervals=intervals,
        char_lefts=[char_range[0] for char_range in char_x_ranges],
        char_x_ranges=char_x_ranges,
        fill_segments=fill_segments,
        line_rect=text_layout.line_rect,
        colors=effective_karaoke_colors(style),
        rtl=style.right_to_left != line.wipe_reverse,
        has_inline_styles=True,
        ink_x_ranges=ink_x_ranges,
        ruby_layouts=ruby_layouts,
        render_line=line,
        signal_band_left=(
            float(text_layout.line_rect.left()) + band_left_local
            if band_left_local is not None
            else None
        ),
    )
