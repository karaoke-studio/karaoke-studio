"""单帧 QPainter 绘制（A4 阶段）。

入口 :func:`paint_frame` 把一行已唱 / 未唱字符渲染到给定 ``QImage`` 上；
预览路径可用 :func:`paint_frame_to_painter` 直接画到已有 ``QPainter``，避免每帧
额外分配整张离屏图。

绘制顺序（自底向上）：

1. **阴影**：整行文本按 ``shadow_offset_*`` 偏移绘一份阴影色
2. **描边**：用 ``QPainterPath.addText`` 取字形轮廓，``strokePath`` 描宽线
3. **底色**：整行字符（``base_color``）
4. **Ruby 注音**：按 ``@Ruby`` 时间区间映射到主歌词字符范围，画在主行上方
5. **填充层**：同样字符以 ``fill_color`` 重绘，但用 ``setClipRect`` 把每个字符
   裁切到"已唱比例"（左→右扫光）

预览路径与渲染路径**共用本函数**——预览给到的 image 是缩放后的 QImage、
渲染管线给的是 1080p QImage，绘制逻辑一致。

**性能优化**：1~3 步（阴影 + 描边 + 底色）每帧的内容 *完全不依赖* ``t_ms``，
只随 line text + font + style 变化。横排文本会按连续同 style 的 glyph run
烘焙成透明 QImage 缓存，绘制时一次 ``drawImage`` blit；每帧只重画 5 步的逐字 clip。1080p 双行场景下，单帧
``paintEvent`` 工作量从 ~2× ``QPainterPath.addText + strokePath`` 降到一次
位图 blit，CPU 时间降幅 3~5×。缓存按 line/font/style 哈希索引，LRU 退役，
样式实时改动会自动 invalidate。

P1 阶段会在本函数基础上加：渐变填充（B3）、入场退场动画（B4）、
多歌手分色（B2）。
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, replace
from threading import local as thread_local
from typing import Hashable, Optional

import numpy as np

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetrics,
    QImage,
    QPainter,
    QPainterPath,
    QTransform,
)

from krok_helper.subtitle_render.engine.render.core.layers import (
    BakedLayer,
    LayerAnimation,
    LayerCache,
    LayerCompositor,
    LayerContext,
    SCOPE_GROUP,
    SCOPE_LINE,
)
from krok_helper.subtitle_render.engine.render.effects import (
    HARD_BAND_BRUSH_CACHE as _HARD_BAND_BRUSH_CACHE,
    IMAGE_BRUSH_CACHE as _IMAGE_BRUSH_CACHE,
    IMAGE_FILL_CACHE as _IMAGE_FILL_CACHE,
    IMAGE_FILL_LOCK as _IMAGE_FILL_LOCK,
    anchor_texture_brush as _anchor_texture_brush,
    brush_for_fill as _brush_for_fill,
    cached_fill_image as _cached_fill_image,
    cached_image_brush as _cached_image_brush,
    clear_fill_caches as _clear_fill_caches,
    fill_brush_rect as _fill_brush_rect,
    fill_is_alpha as _fill_is_alpha,
    fill_signature as _fill_signature,
    glow_blur_radii as _glow_blur_radii,
    glow_concentration_level as _glow_concentration_level,
    glow_extent as _glow_extent,
    glow_pen_width as _glow_pen_width,
    glow_radius as _glow_radius,
    gradient_stop_position as _gradient_stop_position,
    gradient_stops as _gradient_stops,
    karaoke_state_signature as _karaoke_state_signature,
    linear_gradient_brush as _linear_gradient_brush,
    main_stroke2_width as _main_stroke2_width,
    paint_fill_path as _paint_fill_path,
    paint_shadow_silhouette as _paint_shadow_silhouette,
    paint_stroke_path as _paint_stroke_path,
    ruby_baseline_y as _ruby_baseline_y,
    ruby_decoration_kind as _ruby_decoration_kind,
    ruby_glow_concentration_level as _ruby_glow_concentration_level,
    ruby_glow_radius as _ruby_glow_radius,
    ruby_paint_style as _ruby_paint_style,
    ruby_shadow_dx as _ruby_shadow_dx,
    ruby_shadow_dy as _ruby_shadow_dy,
    ruby_stroke_extent as _ruby_stroke_extent,
    ruby_vertical_extra as _ruby_vertical_extra,
    ruby_visual_padding as _ruby_visual_padding,
    scaled_glow_radius as _scaled_glow_radius,
    split_gradient_stops as _split_gradient_stops,
    split_vertical_brush as _split_vertical_brush,
    stroke2_pen_width as _stroke2_pen_width,
    stroke_pen_width as _stroke_pen_width,
    text_visual_padding as _text_visual_padding,
    title_visual_padding as _title_visual_padding,
    visual_stroke_extent as _visual_stroke_extent,
    visual_text_padding as _visual_text_padding,
    valid_color as _valid_color,
)
from krok_helper.subtitle_render.engine.layout.layout_context import (
    _LAYOUT_PASS,
    layout_pass,
)
from krok_helper.subtitle_render.engine.guide import (
    guide_symbol_is_bitmap as _guide_symbol_is_bitmap,
    render_line_with_guide_symbols as _line_with_guide_symbol,
)
from krok_helper.subtitle_render.engine.guide import (
    bitmap_guide_content_size as _bitmap_guide_content_size,
    bitmap_guide_image as _bitmap_guide_image,
    vector_glyph_width as _vector_glyph_width,
)
from krok_helper.subtitle_render.engine.render.image_resource import (
    image_file_signature as _image_file_signature,
    warn_image_resource_skipped as _warn_image_fill_skipped,
)
from krok_helper.subtitle_render.engine.layout.plan.cache import (
    clear_track_layout_plan_cache,
    layout_cache_enabled as _layout_cache_enabled,
)
from krok_helper.subtitle_render.engine.layout.plan.semantic import (
    LayoutPlanResolvers,
    build_track_layout_plan as _build_semantic_layout_plan,
)
from krok_helper.subtitle_render.engine.layout.plan.projection import (
    active_page_offsets_from_layout_plan as _active_page_offsets_from_layout_plan,
    visible_lines_from_layout_plan as _visible_lines_from_layout_plan,
)
from krok_helper.subtitle_render.engine.layout.line.style import (
    entry_animation_ms as _entry_animation_ms,
    exit_animation_ms as _exit_animation_ms,
    lane_count as _lane_count,
    layout_style_for_line as _layout_style_for_line,
    line_end_ms as _line_end_ms,
    line_start_ms as _line_start_ms,
    row_count_resolver as _row_count_resolver,
    style_for_line as _style_for_line,
    style_for_line_display_window as _style_for_line_display_window,
)
from krok_helper.subtitle_render.engine.layout.page.pagination import (
    line_center_override as _line_center_override,
    renderable_page_lines as _renderable_page_lines,
    renderable_page_map as _renderable_page_map,
)
from krok_helper.subtitle_render.engine.layout.line.geometry import (
    line_has_role_labels as _line_has_role_labels,
)
from krok_helper.subtitle_render.engine.layout.display.signal import (
    display_style_for_signal_window as _display_style_for_signal_window,
    lit_signal_active as _lit_signal_active,
    resolve_signal_display_lines as _resolve_signal_display_lines,
    signal_head_context as _signal_head_context,
    signal_lead_in_ms as _signal_lead_in_ms,
)
from krok_helper.subtitle_render.engine.ruby import (
    active_rubies_for_line as _active_rubies_for_line,
    effective_ruby_for_target as _effective_ruby_for_target,
    find_ruby_text_indices as _find_ruby_text_indices,
    find_ruby_text_span as _find_ruby_text_span,
    ruby_explicit_target_indices as _ruby_explicit_target_indices,
    ruby_owns_line as _ruby_owns_line,
    ruby_target_indices as _ruby_target_indices,
    ruby_target_x_range as _ruby_target_x_range,
    ruby_text_span_x_range as _ruby_text_span_x_range,
    ruby_time_indices as _ruby_time_indices,
    text_span_indices as _text_span_indices,
)
from krok_helper.subtitle_render.engine.ruby import (
    _RUBY_MEASURE_CACHE,
    _RUBY_UNIT_LAYOUT_CACHE,
    build_ruby_font as _build_ruby_font,
    build_ruby_font_for_text as _build_ruby_font_for_text,
    resolve_ruby_alignment as _resolve_ruby_alignment,
    ruby_font_size as _ruby_font_size,
    ruby_char_gaps as _ruby_char_gaps,
    ruby_interval_px as _ruby_interval_px,
    ruby_layout_draw_bounds as _ruby_layout_draw_bounds,
    ruby_layout_gap as _ruby_layout_gap,
    ruby_layout_left_offset as _ruby_layout_left_offset,
    ruby_layout_left_overhang as _ruby_layout_left_overhang,
    ruby_layout_origins as _ruby_layout_origins,
    ruby_layout_units as _ruby_layout_units,
    ruby_layout_width as _ruby_layout_width,
    ruby_scale as _ruby_scale,
    ruby_script_stroke_style as _ruby_script_stroke_style,
    ruby_style_for_target_indices as _ruby_style_for_target_indices,
    ruby_stroke2_enabled as _ruby_stroke2_enabled,
    ruby_stroke2_width as _ruby_stroke2_width,
    ruby_stroke2_width_value as _ruby_stroke2_width_value,
    ruby_stroke_width as _ruby_stroke_width,
    ruby_uses_main_font as _ruby_uses_main_font,
    ruby_unit_layouts as _ruby_unit_layouts,
    scaled_px as _scaled_px,
    scaled_signed_px as _scaled_signed_px,
)
from krok_helper.subtitle_render.engine.text import (
    build_font as _build_font,
    build_latin_font as _build_latin_font,
    char_advance as _char_advance,
    char_layout_width as _char_layout_width,
    char_path_left_offset as _char_path_left_offset,
    clamp_weight as _clamp_weight,
    clear_char_metric_cache,
    is_emoji_text as _is_emoji_text,
    is_n3_latin_text as _is_n3_latin_text,
    latin_font_size as _latin_font_size,
    latin_font_weight as _latin_font_weight,
    letter_spacing as _letter_spacing,
    line_text_width as _line_text_width,
    make_font_for as _make_font_for,
    n3_char_box_descent as _n3_char_box_descent,
    n3_char_box_ascent as _n3_char_box_ascent,
    nicokara_char_geometry_left_offset as _nicokara_char_geometry_left_offset,
    nicokara_layout_width as _nicokara_layout_width,
    truncate_div as _truncate_div,
)
from krok_helper.subtitle_render.engine.text import (
    GlyphLayout as _GlyphLayout,
    TextLayout as _TextLayout,
    build_role_text_layout as _build_role_text_layout,
    build_text_layout as _build_text_layout,
    char_left_positions as _char_left_positions,
    main_script_stroke_style as _main_script_stroke_style,
    role_char_geometry_by_index as _role_char_geometry_by_index,
    style_for_role_in_layout as _style_for_role_in_layout,
)
from krok_helper.subtitle_render.engine.layout.line.qt_geometry import (
    resolved_char_intervals_for_line,
    resolved_guide_anchor_bounds_for_line,
)
from krok_helper.subtitle_render.engine.layout.plan.page_offsets import (
    MeasuredPageLine,
    PageOffsetResolvers,
    clear_page_offset_cache,
    page_offsets_at_time,
    resolve_page_offset_windows,
)
from krok_helper.subtitle_render.engine.layout.display.schedule import (
    DisplayScheduleResolvers,
    extend_page_display_boundary as _extend_page_display_boundary,
    resolve_display_schedule,
    resolve_visible_display_lines,
    resolve_display_windows,
)
from krok_helper.subtitle_render.engine.layout.display.resolver import (
    AnimationGuardPorts,
    DisplayResolutionPorts,
    StyleDisplayResolutionPorts,
    apply_animation_time_guard,
    clear_display_line_resolution_cache,
    display_line_compute_kwargs,
    resolve_display_lines_for_style,
    resolve_display_timing,
)
from krok_helper.subtitle_render.engine.value_signature import (
    value_signature as _value_signature,
)
from krok_helper.subtitle_render.sources.guide_symbols import scaled_guide_symbol_path
from krok_helper.subtitle_render.n3.font_catalog import resolve_qt_font_family


# 横排 glyph run 层缓存：普通行与分色行都按连续同 style 的 run 烘焙。
# 每个 run 的「未唱」层（含 before-glow）、「已唱」主体层与 after-glow
# 各烘焙一次；逐帧只按扫光半平面 clip blit。
_TEXT_RUN_LAYER_CACHE = LayerCache(max_items=128)
_TEXT_RUN_COMPOSITOR = LayerCompositor(_TEXT_RUN_LAYER_CACHE)
# A3（§9.7）：utopia transition 路径每帧重算 glow 高斯（实测 18ms 主因）。把 glow 按
# **上正 glyph 身份**烘焙一次进此缓存（before/after 各一条），逐帧在 utopia 变换下 blit。
# glow 是软晕、对 bitmap-transform 不敏感 → 复用无明显软化；body 仍逐帧矢量保持锐利（B 档再缓存）。
_RUN_GLOW_CACHE = LayerCache(max_items=128)
# 行级布局缓存：_LineLayout（纯几何 + 字体资源）与 t_ms 无关，但此前每帧重算
# （full 场景约 30% paint 时间）。key = (整 track 值签名, display_style 值签名,
# 行索引, 画布尺寸, baseline/line_x/lane)——签名每帧从当前值重建（models 是可变
# dataclass、前端不调失效接口），track/style 就地改动下一帧自然 miss，不会取脏值。
# 行索引而非行内容进 key：SmartHorizon 的页定位用 `item is line` 身份判断，
# 值相同的两行也可能落在不同页。
# 一次排版会把每行的布局问上三遍，但三遍是分批扫全轨的：48 项装不下一条曲目，
# 等第二遍回到第一行时它早被挤掉了，长曲目命中率直接归零。按整轨都能留住来定容量。
_LINE_LAYOUT_CACHE = LayerCache(max_items=2048)
# Scratch buffers for N3-style opacity layers; see _paint_through_opacity_layer.
_OPACITY_LAYER_LOCAL = thread_local()


def _glow_cache_enabled() -> bool:
    """A3：utopia 路径复用 glow 烘焙缓存（默认开）。``KROK_SUBTITLE_GLOW_CACHE=0`` 退回
    逐帧 ``_paint_glow_path``（A/B 验收 / 紧急回退用）。"""
    return os.environ.get("KROK_SUBTITLE_GLOW_CACHE", "1").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _vertical_layer_enabled() -> bool:
    """竖排（縦書き）整条路径（主文本 + ruby）走 LayerCompositor + bake 缓存。

    默认开启：与横排一致地把 before/after/ruby 烘焙成位图缓存，逐帧只 blit + clip，
    省掉每帧重光栅化。``KROK_SUBTITLE_VERTICAL_LAYER=0`` 回退到旧的逐帧直绘路径
    （亦作像素一致性 A/B oracle）。
    """
    return os.environ.get("KROK_SUBTITLE_VERTICAL_LAYER", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


def _horizontal_layer_enabled() -> bool:
    """横排主文本走 LayerCompositor + bake 缓存。

    默认开启；``KROK_SUBTITLE_HORIZONTAL_LAYER=0`` 保留同 layout 的矢量直绘
    oracle，供 direct-vs-bake 像素回归使用。
    """
    return os.environ.get("KROK_SUBTITLE_HORIZONTAL_LAYER", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


_UTOPIA_INTRO_TIME_MS = 700
_UTOPIA_INTRO_DELAY_MS = 200
_UTOPIA_INTRO_ENLARGE_MS = 400
_UTOPIA_INTRO_CONDENSE_MS = 100
_UTOPIA_INTRO_OVER_RATIO = 1.3
_UTOPIA_WIPE_OVER_RATIO = 1.15
_UTOPIA_WIPE_OVER_TIME_RATIO = 0.25
_UTOPIA_WIPE_OVER_TIME_LIMIT_MS = 100
_UTOPIA_FADE_OUT_TIME_MS = 750
_CHAR_FADE_INTRO_DELAY_MS = 350
_CHAR_FADE_IN_TIME_MS = 250
_CHAR_FADE_OUT_TIME_MS = 250


def clear_before_layer_cache() -> None:
    """测试 / 调试用：把字幕层位图缓存全部丢掉。"""
    _clear_fill_caches()
    _TEXT_RUN_LAYER_CACHE.clear()
    _RUN_GLOW_CACHE.clear()
    clear_char_metric_cache()
    _RUBY_MEASURE_CACHE.clear()
    _RUBY_UNIT_LAYOUT_CACHE.clear()
    _LINE_LAYOUT_CACHE.clear()
    clear_display_line_resolution_cache()
    clear_track_layout_plan_cache()
    clear_page_offset_cache()


def _track_layout_signature(track: TimingTrack) -> tuple:
    """track 中影响**邻行可见**布局的值（手写快速版）。

    邻行只通过 SmartHorizon 分页 / 页宽参与本行布局（``assign_lanes`` 与
    ``_line_total_width`` 都不读邻行计时），所以帧级签名只收每行文本 / 布局
    结构字段 + 全部注音 + meta 偏移；目标行自己的逐字符计时细节由
    :func:`_line_layout_signature` 按行补充，避免大轨每帧走全量计时元组。"""
    return (
        tuple(
            (
                "".join(c.text for c in line.chars),
                tuple(
                    (index, c.role_label)
                    for index, c in enumerate(line.chars)
                    if c.role_label is not None
                ),
                line.singer_id,
                line.is_blank,
                line.layout_index,
                line.break_before,
                _value_signature(line.animation_override),
                _value_signature(line.guide_symbol),
                _value_signature(line.inline_guide_symbols),
            )
            for line in track.lines
        ),
        tuple(
            (
                ruby.kanji,
                ruby.reading,
                tuple(ruby.reading_part_ms),
                ruby.pos_start_ms,
                ruby.pos_end_ms,
                tuple(ruby.reading_parts),
                ruby.target_line_index,
                ruby.target_char_start,
                ruby.target_char_end,
            )
            for ruby in track.rubies
        ),
        page_plan_signature(track),
        _value_signature(track.loading_settings_snapshot),
        track.loading_settings_mode,
        (track.meta.silence_ms, track.meta.offset_ms),
    )


def _line_layout_signature(line: TimingLine) -> tuple:
    """目标行的逐字符计时细节（intervals / fill_segments / ruby 时窗的输入）。"""
    return (
        tuple(c.start_ms for c in line.chars),
        tuple(
            (index, c.pause_release_ms)
            for index, c in enumerate(line.chars)
            if c.pause_release_ms is not None
        ),
        tuple(
            (
                index,
                c.source_span_start_ms,
                c.source_span_end_ms,
                c.source_span_index,
                c.source_span_count,
            )
            for index, c in enumerate(line.chars)
            if c.source_span_count != 1 or c.source_span_start_ms is not None
        ),
        tuple(
            (index, c.explicit_start, c.explicit_end)
            for index, c in enumerate(line.chars)
            if c.explicit_start or c.explicit_end
        ),
        line.end_ms,
        line.display_start_override_ms,
        line.display_end_override_ms,
        _value_signature(line.guide_symbol),
        _value_signature(line.inline_guide_symbols),
    )


def _layout_cache_sig(track: TimingTrack, display_style: Style) -> tuple | None:
    """每帧一次的布局缓存基础签名；关闭开关或竖排时返回 None（竖排走独立路径）。"""
    if not _layout_cache_enabled() or display_style.vertical:
        return None
    return (_track_layout_signature(track), _value_signature(display_style))

from krok_helper.subtitle_render.engine.timing.timeline import (
    DisplayLine,
    assign_lanes,
    char_fill_ratio,
    compute_char_intervals,
    compute_display_lines,
    track_duration_ms,
)
from krok_helper.subtitle_render.engine.layout.page.plan import (
    page_plan_signature,
    resolve_page_plan,
)
from krok_helper.subtitle_render.engine.layout.page.placement import (
    LineVisualBand,
    PageVisualBands,
    bands_require_separation,
    solve_page_axis_offsets,
    time_windows_overlap,
)
from krok_helper.subtitle_render.engine.layout.plan.model import (
    LineLayoutPlan,
    TrackLayoutPlan,
)
from krok_helper.subtitle_render.engine.layout.page.assignment import (
    apply_layout_to_page,
    assign_layout_to_all,
    auto_assign_layouts_by_page,
)
from krok_helper.subtitle_render.engine.render.core.animator import line_animation_state
from krok_helper.subtitle_render.engine.ruby.timing import (
    _main_text_ruby_progress_ratio,
    _main_text_ruby_progress_time_at_ratio,
    _reading_unit_progress_ratio,
    _ruby_main_text_slot_times,
    _ruby_progress_parts_and_intervals,
    _ruby_progress_ratio,
    _ruby_progress_time_at_ratio,
    _ruby_reading_boundaries,
    _ruby_reading_intervals,
    _ruby_reading_intervals_with_pauses,
    _ruby_reading_unit_progress_points,
    _ruby_reading_units,
    _ruby_utopia_reading_units_and_intervals,
    _ruby_utopia_visual_units,
    _ruby_visual_units_and_intervals,
)
from krok_helper.subtitle_render.engine.render.core.raster_blur import (
    _blur_image,
    _gaussian_blur_image,
    _n3_gaussian_kernel_1d,
)
from krok_helper.subtitle_render.engine.render.elements.signal import (
    SignalLayoutMetrics as _SignalLayoutMetrics,
    SignalLineMeasurement,
    SignalLitGroup as _SignalLitGroup,
    VolumeSignalGeometry as _VolumeSignalGeometry,
    active_lit_indices as _resolve_active_lit_indices,
    line_has_active_signal as _line_has_active_signal,
    lit_extinguish_transition_state as _lit_extinguish_transition_state,
    lit_transition_state as _lit_transition_state,
    paint_signal_lits as _paint_signal_lits_with_ports,
    resolve_signal_layers as _resolve_signal_layers_with_ports,
    resolve_signal_lit_groups as _resolve_signal_lit_groups,
    shape_active_index_and_phase as _shape_active_index_and_phase,
    signal_layout_metrics as _signal_layout_metrics,
    signal_lit_x as _signal_lit_x,
    signal_lit_y as _signal_lit_y,
    signal_local_x as _signal_local_x,
    signal_offset_x as _signal_offset_x,
    signal_stroke_extent as _signal_stroke_extent,
    volume_active_index_and_phase as _volume_active_index_and_phase,
    volume_flash_alpha as _volume_flash_alpha,
    volume_signal_column_rects as _volume_signal_column_rects,
    volume_signal_geometry as _volume_signal_geometry,
    volume_signal_state as _volume_signal_state,
)
from krok_helper.subtitle_render.engine.render.elements.title import (
    TitleGlyphLayout as _TitleGlyphLayout,
    TitleOverlayLayout as _TitleOverlayLayout,
    TitleRenderPorts,
    build_title_font as _build_title_font,
    build_title_latin_font as _build_title_latin_font,
    layout_title_overlay as _layout_title_overlay,
    make_title_font_for as _make_title_font_for,
    make_title_overlay_layer as _make_title_overlay_layer,
    paint_title_overlay as _paint_title_overlay_with_ports,
    title_block_origin as _title_block_origin,
)
from krok_helper.subtitle_render.engine.render.elements.horizontal import (
    FillSegment as _FillSegment,
    LineCharTransition as _LineCharTransition,
    LineLayout as _LineLayout,
    RubyLayout as _RubyLayout,
    RubyWipeSegment as _RubyWipeSegment,
    SayatooLineLayout as _SayatooLineLayout,
)
from krok_helper.subtitle_render.engine.render.elements.vertical import (
    VerticalCachePorts,
    VerticalLayerPorts,
    VerticalLineLayout as _VerticalLineLayout,
    VerticalProgressPorts,
    VerticalRasterPorts,
    VerticalRubyPorts,
    layout_vertical_line as _layout_vertical_line,
    paint_line_vertical_layers as _paint_line_vertical_layers_with_ports,
    paint_rubies_vertical as _paint_rubies_vertical_with_ports,
    resolve_vertical_columns as _resolve_vertical_columns,
    resolve_vertical_top as _resolve_vertical_top,
    vertical_after_clip_rect as _vertical_after_clip_rect,
    vertical_after_clip_pad as _vertical_after_clip_pad_with_ports,
    vertical_before_clip_rect as _vertical_before_clip_rect,
    vertical_before_clip_pad as _vertical_before_clip_pad_with_ports,
    vertical_cell_width as _vertical_cell_width,
    vertical_glyph_offset as _vertical_glyph_offset,
    vertical_glyph_path as _vertical_glyph_path,
    vertical_fill_band as _vertical_fill_band_with_ports,
    vertical_orientation as _vertical_orientation,
    vertical_main_path_signature as _vertical_main_path_sig,
    vertical_layer_stack as _vertical_layer_stack_with_ports,
    vertical_ruby_allowance as _vertical_ruby_allowance,
)
from krok_helper.subtitle_render.engine.style.style_semantics import (
    effective_karaoke_colors as _effective_karaoke_colors,
    legacy_after_text_fill as _legacy_after_text_fill,
    solid_fill as _solid_fill,
    style_for_role as _style_for_role,
)
from krok_helper.subtitle_render.engine.style.title_semantics import (
    resolve_title_overlay,
    resolve_title_role_overlay as _resolve_title_role_overlay,
    resolve_title_text as _resolve_title_text,
    title_layout_source as _title_layout_source,
    title_overlay_opacity as _title_overlay_opacity,
    title_show_specs as _title_show_specs,
    title_show_window as _title_show_window,
)
from krok_helper.subtitle_render.domain.paint import (
    KaraokeColors,
    KaraokeColorState,
    PaintFill,
)
from krok_helper.subtitle_render.domain.timing import (
    GuideSymbol,
    RubyAnnotation,
    TimingChar,
    TimingLine,
    TimingTrack,
)
from krok_helper.subtitle_render.domain.models import (
    Style,
    TitleOverlay,
    effective_karaoke_animation,
    normalize_glow_concentration_level,
)


def _resolve_visible_content(
    track: TimingTrack,
    t_ms: int,
    style: Style,
    *,
    duration_ms: Optional[int] = None,
    logical_w: int | None = None,
    logical_h: int | None = None,
):
    """计算某帧的可见内容元组：``(track_t_ms, display_style, display_lines,
    signal_lines, title_opacity)``。

    :func:`paint_frame_to_painter` 的早退判断与 :func:`frame_has_content` 共用本函数，
    保证"是否有可见内容"两处口径一致（A4 空帧短路用）。
    """
    resolved = _resolve_visible_content_with_plan(
        track,
        t_ms,
        style,
        duration_ms=duration_ms,
        logical_w=logical_w,
        logical_h=logical_h,
    )
    return resolved[:5]


def _resolve_visible_content_with_plan(
    track: TimingTrack,
    t_ms: int,
    style: Style,
    *,
    duration_ms: Optional[int] = None,
    logical_w: int | None = None,
    logical_h: int | None = None,
):
    """Resolve visible content and retain its shared dual-line layout plan."""
    track_t_ms = _effective_track_time_ms(track, t_ms, style)
    display_style = _display_style_for_signal_window(style)
    layout_plan: TrackLayoutPlan | None = None
    if display_style.dual_line_layout:
        layout_plan = build_track_layout_plan(
            track,
            display_style,
            logical_w=logical_w,
            logical_h=logical_h,
        )
        display_lines = _visible_lines_from_layout_plan(layout_plan, track_t_ms)
    else:
        # Single-line mode deliberately selects one best live/lead/tail line
        # when authored windows overlap; that policy is frame-dependent and is
        # kept separate from the frame-independent track plan.
        display_lines = _visible_lines_for_style(
            track,
            track_t_ms,
            display_style,
            logical_w=logical_w,
            logical_h=logical_h,
        )
    signal_lines = _signal_display_lines_for_style(
        track,
        track_t_ms,
        display_style,
        logical_w=logical_w,
        logical_h=logical_h,
    )
    # Lyrics follow the track/global offset; the project title does not. N3
    # anchors title show times to the background movie timeline.
    title_opacity = _title_overlay_opacity(
        style.title_overlay,
        track,
        t_ms,
        duration_ms=duration_ms,
    )
    return (
        track_t_ms,
        display_style,
        display_lines,
        signal_lines,
        title_opacity,
        layout_plan,
    )


def frame_has_content(
    track: Optional[TimingTrack],
    t_ms: int,
    style: Style,
    extra_tracks: Optional[list[TimingTrack]] = None,
    *,
    duration_ms: Optional[int] = None,
    logical_w: int | None = None,
    logical_h: int | None = None,
) -> bool:
    """该帧是否会画出任何字幕内容（行 / 信号 / 标题）。

    用于导出 / 预览的"空帧短路"：返回 ``False`` 时可直接写全透明帧，省去
    ``fill`` + 光栅化 + 字节拷贝。与 :func:`paint_frame_to_painter` 的早退条件同源。

    ``extra_tracks``：副字幕源（N3 多歌词文件，如コーラス轨）；任一轨有内容即为真。
    标题只随主轨。
    """
    if track is not None:
        _, _, display_lines, signal_lines, title_opacity = _resolve_visible_content(
            track,
            t_ms,
            style,
            duration_ms=duration_ms,
            logical_w=logical_w,
            logical_h=logical_h,
        )
        if display_lines or signal_lines or title_opacity > 0.0:
            return True
    for extra in extra_tracks or ():
        _, _, display_lines, signal_lines, _unused = _resolve_visible_content(
            extra,
            t_ms,
            style,
            logical_w=logical_w,
            logical_h=logical_h,
        )
        if display_lines or signal_lines:
            return True
    return False


def frame_content_intervals(
    logical_w: int,
    logical_h: int,
    track: Optional[TimingTrack],
    t_ms: int,
    style: Style,
    extra_tracks: Optional[list[TimingTrack]] = None,
    *,
    duration_ms: Optional[int] = None,
) -> list[tuple[int, int]] | None:
    """Return per-source (lyric / title) vertical content intervals, **unmerged**.

    Each entry is a clamped ``(top, bottom)`` for one content group (the lyric +
    signal group, and the title overlay group).  Disjoint groups stay separate so
    the export pipeline can pack them into multiple strips (A2 方案 B).  Returns
    ``None`` for paths not yet migrated to layer bounds (竖排 / viewport 旋转 /
    逐字 transition)，调用方应回退到整帧 / alpha 扫描。
    """
    track_entries: list[tuple[TimingTrack, bool]] = []
    if track is not None:
        track_entries.append((track, True))
    track_entries.extend((extra, False) for extra in extra_tracks or ())
    if not track_entries:
        return None

    intervals: list[tuple[int, int]] = []
    any_content = False
    for entry_track, with_title in track_entries:
        track_t_ms, display_style, display_lines, signal_lines, title_opacity = (
            _resolve_visible_content(
                entry_track,
                t_ms,
                style,
                duration_ms=duration_ms if with_title else None,
                logical_w=logical_w,
                logical_h=logical_h,
            )
        )
        if not with_title:
            title_opacity = 0.0
        if not display_lines and not signal_lines and title_opacity <= 0.0:
            continue
        any_content = True
        if display_lines:
            lyric_bounds = _subtitle_lines_vertical_bounds(
                logical_w,
                logical_h,
                entry_track,
                track_t_ms,
                display_style,
                display_lines,
                signal_lines,
            )
            if lyric_bounds is None:
                return None
            intervals.append(lyric_bounds)

        if with_title and title_opacity > 0.0 and style.title_overlay is not None:
            resolved_title = resolve_title_overlay(style)
            title_layout = _layout_title_overlay(
                logical_w, logical_h, entry_track, resolved_title, style=style
            )
            if title_layout is not None:
                title_bounds = _TEXT_RUN_COMPOSITOR.vertical_bounds(
                    LayerContext(t_ms=track_t_ms, logical_w=logical_w, logical_h=logical_h),
                    [
                        _make_title_overlay_layer(
                            title_layout,
                            resolved_title,
                            title_opacity,
                            ports=_TITLE_RENDER_PORTS,
                        )
                    ],
                )
                if title_bounds is not None:
                    intervals.append(title_bounds)

    if not any_content:
        return None
    clamped: list[tuple[int, int]] = []
    for top, bottom in intervals:
        ct = max(0, top)
        cb = min(logical_h - 1, bottom)
        if cb >= ct:
            clamped.append((ct, cb))
    return clamped or None


def frame_vertical_bounds(
    logical_w: int,
    logical_h: int,
    track: Optional[TimingTrack],
    t_ms: int,
    style: Style,
    extra_tracks: Optional[list[TimingTrack]] = None,
    *,
    duration_ms: Optional[int] = None,
) -> tuple[int, int] | None:
    """Return conservative vertical content bounds (union) for the current frame.

    This is the P1.b layer-bounds query used by export strip selection and
    preview dirty updates.  It deliberately returns ``None`` for render paths
    that have not migrated to layer bounds yet; callers should then fall back to
    the existing pixel scan / full repaint path.
    """
    intervals = frame_content_intervals(
        logical_w,
        logical_h,
        track,
        t_ms,
        style,
        extra_tracks,
        duration_ms=duration_ms,
    )
    if not intervals:
        return None
    top = min(item[0] for item in intervals)
    bottom = max(item[1] for item in intervals)
    if bottom < top:
        return None
    return top, bottom


def paint_frame(
    image: QImage,
    track: Optional[TimingTrack],
    t_ms: int,
    style: Style,
    extra_tracks: Optional[list[TimingTrack]] = None,
    *,
    duration_ms: Optional[int] = None,
) -> QImage:
    """把 ``track`` 在 ``t_ms`` 时刻的活跃行渲染到 ``image``（原地修改）。

    若无活跃行则不画任何字（image 不变）。返回同一个 image 以便链式调用。
    ``extra_tracks`` 为副字幕源（N3 多歌词文件），在主轨之上依次叠绘。
    """
    painter = QPainter(image)
    try:
        # QImage 上 setDevicePixelRatio 后，QPainter 在该 image 上的坐标系
        # 自动按 dpr 缩放——绘制坐标用"逻辑像素"，而 image.width()/height()
        # 返回的是物理像素。这里取逻辑尺寸，让上层布局算居中等都按屏幕
        # 实际可见尺寸来。
        dpr = image.devicePixelRatioF() or 1.0
        logical_w = max(int(round(image.width() / dpr)), 1)
        logical_h = max(int(round(image.height() / dpr)), 1)
        paint_frame_to_painter(
            painter,
            logical_w,
            logical_h,
            track,
            t_ms,
            style,
            extra_tracks,
            duration_ms=duration_ms,
        )
    finally:
        painter.end()
    return image


def paint_frame_to_painter(
    painter: QPainter,
    logical_w: int,
    logical_h: int,
    track: Optional[TimingTrack],
    t_ms: int,
    style: Style,
    extra_tracks: Optional[list[TimingTrack]] = None,
    *,
    duration_ms: Optional[int] = None,
) -> None:
    """把当前字幕帧直接绘制到已打开的 ``QPainter``。

    ``logical_w`` / ``logical_h`` 使用 Qt 逻辑像素；调用方负责先绘制背景。

    ``extra_tracks``：副字幕源（对标 N3 ``SourceLyricsInfos`` 多歌词文件，
    如コーラス轨）。每轨独立分页 / 分 lane / 计算显示窗口，依次叠绘到同一帧；
    标题 overlay 只随主轨绘制一次。
    """
    with layout_pass():
        if track is not None:
            _paint_track_to_painter(
                painter,
                logical_w,
                logical_h,
                track,
                t_ms,
                style,
                draw_title=True,
                duration_ms=duration_ms,
            )
        for extra in extra_tracks or ():
            _paint_track_to_painter(
                painter, logical_w, logical_h, extra, t_ms, style, draw_title=False
            )


def _paint_track_to_painter(
    painter: QPainter,
    logical_w: int,
    logical_h: int,
    track: TimingTrack,
    t_ms: int,
    style: Style,
    *,
    draw_title: bool,
    duration_ms: Optional[int] = None,
) -> None:
    (
        track_t_ms,
        display_style,
        display_lines,
        signal_lines,
        title_opacity,
        layout_plan,
    ) = _resolve_visible_content_with_plan(
            track,
            t_ms,
            style,
            duration_ms=duration_ms if draw_title else None,
            logical_w=logical_w,
            logical_h=logical_h,
    )
    if not draw_title:
        title_opacity = 0.0
    if not display_lines and not signal_lines and title_opacity <= 0.0:
        return

    # 标题字幕 overlay（B7）：静态文字，画在屏幕坐标系（不随「视图」变换 / 行布局），
    # 外观由「标题」配色方案与布局引用解析。**钉在最下层**——先画标题，本轨歌词与
    # 随后叠绘的副字幕源都压在它之上（GPU 侧对应 compositeOrder 最小）。
    if title_opacity > 0.0 and style.title_overlay is not None:
        _paint_title_overlay(
            painter, logical_w, logical_h, track, style, title_opacity
        )

    painter.save()
    try:
        painter.setRenderHints(
            QPainter.RenderHint.Antialiasing
            | QPainter.RenderHint.TextAntialiasing
            | QPainter.RenderHint.SmoothPixmapTransform
        )
        _apply_viewport_transform(painter, logical_w, logical_h, display_style)
        # 竖排时 baselines 字典里存的是每 lane 的「列中心 x」，横排时存基线 y；
        # 含义由 style.vertical 区分，_paint_line_static 据此走对应几何。
        line_plans = (
            {id(item.line): item for item in layout_plan.lines}
            if layout_plan is not None
            else {}
        )
        if display_style.vertical:
            baselines = _resolve_vertical_columns(logical_w, track, display_lines, display_style)
            line_layouts = {}
            per_line_axes = {
                id(display_line.line): _resolve_vertical_columns(
                    logical_w,
                    track,
                    [display_line],
                    (
                        line_plans[id(display_line.line)].layout_style
                        if id(display_line.line) in line_plans
                        else _style_for_line(display_style, display_line.line)
                    ),
                ).get(display_line.lane)
                for display_line in display_lines
            }
        else:
            baselines = (
                _resolve_display_baselines(logical_h, track, display_lines, display_style)
                if display_lines
                else {}
            )
            line_layouts = _resolve_sayatoo_line_layouts(
                logical_w,
                logical_h,
                track,
                display_lines,
                baselines,
                track_t_ms,
                display_style,
            )
            per_line_axes = {}
        layout_cache_sig = (
            _layout_cache_sig(track, display_style) if display_lines else None
        )
        track_offsets = (
            _active_page_offsets_from_layout_plan(layout_plan, track_t_ms)
            if layout_plan is not None
            else resolved_page_offsets_for_style(
                logical_w, logical_h, track, display_style, t_ms=track_t_ms
            )
        )
        line_offsets = {
            id(line): track_offsets.get(index, (0.0, 0.0))
            for index, line in enumerate(track.lines)
        }
        for display_line in display_lines:
            line_plan = line_plans.get(id(display_line.line))
            line_layout = line_layouts.get(id(display_line.line))
            has_role_labels = _line_has_role_labels(display_line.line)
            line_x = None
            if line_layout is not None and not has_role_labels:
                line_x = line_layout.text_x
            offset_x, offset_y = line_offsets.get(
                id(display_line.line), (0.0, 0.0)
            )
            painter.save()
            try:
                if offset_x or offset_y:
                    painter.translate(offset_x, offset_y)
                _paint_line(
                    painter,
                    logical_w,
                    logical_h,
                    track,
                    display_line.line,
                    track_t_ms,
                    display_style,
                    baseline_y=(
                        per_line_axes.get(id(display_line.line))
                        if display_style.vertical
                        else line_layout.baseline_y
                        if line_layout is not None
                        else baselines.get(
                            display_line.lane,
                            next(iter(baselines.values()), logical_h // 2),
                        )
                    ),
                    line_x=line_x,
                    lane=display_line.lane if display_style.dual_line_layout else None,
                    display_start_ms=display_line.display_start_ms,
                    display_end_ms=display_line.display_end_ms,
                    layout_cache_sig=layout_cache_sig,
                    resolved_style=(
                        line_plan.animation_style if line_plan is not None else None
                    ),
                    line_plan=line_plan,
                )
            finally:
                painter.restore()
        if not display_style.vertical and signal_lines:
            _paint_signal_lits(
                painter,
                logical_w,
                logical_h,
                track,
                signal_lines,
                baselines,
                track_t_ms,
                display_style,
                line_layouts=line_layouts,
                line_offsets=line_offsets,
            )
    finally:
        painter.restore()


# ---------------------------------------------------------------------------
# 标题字幕 overlay（B7）
# ---------------------------------------------------------------------------


def _paint_title_overlay(
    painter: QPainter,
    img_w: int,
    img_h: int,
    track: TimingTrack,
    style: Style,
    opacity: float,
) -> None:
    _paint_title_overlay_with_ports(
        painter,
        img_w,
        img_h,
        track,
        style,
        opacity,
        compositor=_TEXT_RUN_COMPOSITOR,
        ports=_TITLE_RENDER_PORTS,
    )


def _raster_scale_key(device_pixel_ratio: float) -> int:
    return max(int(round(max(float(device_pixel_ratio or 1.0), 0.01) * 1000)), 1)


def _make_raster_image(logical_w: int, logical_h: int, device_pixel_ratio: float) -> QImage:
    dpr = max(float(device_pixel_ratio or 1.0), 0.01)
    physical_w = max(int(math.ceil(max(int(logical_w), 1) * dpr)), 1)
    physical_h = max(int(math.ceil(max(int(logical_h), 1) * dpr)), 1)
    image = QImage(physical_w, physical_h, QImage.Format.Format_ARGB32_Premultiplied)
    image.setDevicePixelRatio(dpr)
    return image


def _title_line_path(
    line: str,
    font: QFont,
    x0: float,
    baseline: float,
    metrics: QFontMetrics,
    latin_metrics: QFontMetrics,
    font_for,
    spacing: int,
) -> QPainterPath:
    path = QPainterPath()
    cursor = float(x0)
    for ch in line:
        glyph_font = font_for(ch) if font_for is not None else font
        path.addText(cursor, float(baseline), glyph_font, ch)
        cursor += _char_advance(ch, metrics, latin_metrics, font_for) + spacing
    return path


def _paint_title_text_stack(
    painter: QPainter, path: QPainterPath, rect: QRectF, title: TitleOverlay
) -> None:
    """静态标题文字的装饰 + 二重描边 + 描边 + 填充（单态，不走字）。"""
    if title.decoration_kind == "glow":
        _paint_glow_path(
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
        _paint_shadow_silhouette(
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
        _paint_stroke_path(
            painter, path, title.stroke2, rect,
            _stroke2_pen_width(title.stroke_width_px, title.stroke2_width_px),
        )
    if title.stroke_width_px > 0:
        _paint_stroke_path(
            painter,
            path,
            title.stroke,
            rect,
            _stroke_pen_width(title.stroke_width_px),
            protect_body=_fill_is_alpha(title.fill),
        )
    _paint_fill_path(painter, path, title.fill, rect)


# ---------------------------------------------------------------------------
# 内部
# ---------------------------------------------------------------------------


def _effective_track_time_ms(track: TimingTrack, t_ms: int, style: Style) -> int:
    """Convert playback time to subtitle time after LRC and UI offsets.

    Positive offsets delay subtitles: at playback ``t_ms`` the renderer samples an
    earlier subtitle timestamp.
    """
    return t_ms - (track.meta.offset_ms + style.timing_offset_ms)


# 九宫格锚点在画布上的相对坐标（横向, 纵向），用于缩放 / 旋转的轴心。
_VIEWPORT_PIVOT_FRACTIONS: dict[str, tuple[float, float]] = {
    "top_left": (0.0, 0.0),
    "top_center": (0.5, 0.0),
    "top_right": (1.0, 0.0),
    "center_left": (0.0, 0.5),
    "center": (0.5, 0.5),
    "center_right": (1.0, 0.5),
    "bottom_left": (0.0, 1.0),
    "bottom_center": (0.5, 1.0),
    "bottom_right": (1.0, 1.0),
}


def _apply_viewport_transform(
    painter: QPainter, logical_w: int, logical_h: int, style: Style
) -> None:
    """对整体字幕层套用 Sayatoo「视图」组的 2D 变换。

    位移直接平移；缩放与旋转围绕 ``viewport_align`` 指定的九宫格锚点。
    默认值（位移 0、缩放 100%、旋转 0）下不改动 painter 坐标系。
    """
    scale = max(style.viewport_scale_pct, 1) / 100.0
    angle = style.viewport_rotation_deg
    offset_x = style.viewport_offset_x
    offset_y = style.viewport_offset_y
    if offset_x == 0 and offset_y == 0 and scale == 1.0 and angle == 0:
        return
    frac_x, frac_y = _VIEWPORT_PIVOT_FRACTIONS.get(
        style.viewport_align, _VIEWPORT_PIVOT_FRACTIONS["center"]
    )
    pivot_x = logical_w * frac_x
    pivot_y = logical_h * frac_y
    if offset_x or offset_y:
        painter.translate(offset_x, offset_y)
    if scale != 1.0 or angle:
        painter.translate(pivot_x, pivot_y)
        if angle:
            painter.rotate(angle)
        if scale != 1.0:
            painter.scale(scale, scale)
        painter.translate(-pivot_x, -pivot_y)


def _subtitle_lines_vertical_bounds(
    logical_w: int,
    logical_h: int,
    track: TimingTrack,
    track_t_ms: int,
    style: Style,
    display_lines: list[DisplayLine],
    signal_lines: list[DisplayLine],
) -> tuple[int, int] | None:
    """Aggregate migrated layer bounds for the lyric layer.

    ``None`` means the current frame uses a path whose visual extent still needs
    the older pixel-scan fallback.
    """
    if style.vertical or style.viewport_rotation_deg:
        return None

    baselines = (
        _resolve_display_baselines(logical_h, track, display_lines, style)
        if display_lines
        else {}
    )
    line_layouts = (
        _resolve_sayatoo_line_layouts(
            logical_w,
            logical_h,
            track,
            display_lines,
            baselines,
            track_t_ms,
            style,
        )
        if display_lines
        else {}
    )
    layout_cache_sig = _layout_cache_sig(track, style) if display_lines else None
    track_offsets = resolved_page_offsets_for_style(
        logical_w, logical_h, track, style, t_ms=track_t_ms
    )
    line_offsets = {
        id(line): track_offsets.get(index, (0.0, 0.0))
        for index, line in enumerate(track.lines)
    }
    bounds: list[tuple[int, int]] = []
    for display_line in display_lines:
        line_bounds = _display_line_vertical_bounds(
            logical_w,
            logical_h,
            track,
            track_t_ms,
            style,
            display_line,
            baselines,
            line_layouts,
            layout_cache_sig=layout_cache_sig,
        )
        if line_bounds is None:
            return None
        _offset_x, offset_y = line_offsets.get(
            id(display_line.line), (0.0, 0.0)
        )
        bounds.append(
            (
                int(math.floor(line_bounds[0] + offset_y)),
                int(math.ceil(line_bounds[1] + offset_y)),
            )
        )
    if signal_lines:
        signal_bounds = _TEXT_RUN_COMPOSITOR.vertical_bounds(
            LayerContext(t_ms=track_t_ms, logical_w=logical_w, logical_h=logical_h),
                _resolve_signal_layers_with_ports(
                    track,
                signal_lines,
                baselines,
                logical_w,
                logical_h,
                    track_t_ms,
                    style,
                    measure_line=_measure_signal_line,
                    line_layouts=line_layouts,
                line_offsets=line_offsets,
            ),
        )
        if signal_bounds is not None:
            bounds.append(signal_bounds)
    if not bounds:
        return None

    top = min(item[0] for item in bounds)
    bottom = max(item[1] for item in bounds)
    return _transform_vertical_bounds(top, bottom, logical_h, style)


def _display_line_vertical_bounds(
    logical_w: int,
    logical_h: int,
    track: TimingTrack,
    track_t_ms: int,
    style: Style,
    display_line: DisplayLine,
    baselines: dict[int, int],
    line_layouts: dict[int, _SayatooLineLayout],
    layout_cache_sig: tuple | None = None,
) -> tuple[int, int] | None:
    line = display_line.line
    line_style = _style_for_line_display_window(
        style,
        line,
        display_line.display_start_ms,
        display_line.display_end_ms,
    )
    animation = line_animation_state(
        line_style,
        t_ms=track_t_ms,
        display_start_ms=display_line.display_start_ms
        if display_line.display_start_ms is not None
        else _line_start_ms(line),
        display_end_ms=display_line.display_end_ms
        if display_line.display_end_ms is not None
        else _line_end_ms(line),
        lane=display_line.lane if line_style.dual_line_layout else None,
    )
    if animation.opacity <= 0.0:
        return None

    line_layout = line_layouts.get(id(display_line.line))
    has_role_labels = _line_has_role_labels(line)
    line_x = line_layout.text_x if line_layout is not None and not has_role_labels else None
    layout = _layout_line(
        track,
        line,
        line_style,
        logical_w,
        logical_h,
        baseline_y=line_layout.baseline_y if line_layout is not None else baselines[display_line.lane],
        line_x=line_x,
        lane=display_line.lane if line_style.dual_line_layout else None,
        cache_sig=layout_cache_sig,
    )
    if layout is None:
        return None

    transition = _line_char_transition_context(
        line_style,
        line,
        track_t_ms,
        display_line.display_start_ms,
        display_line.display_end_ms,
        len(line.chars),
        intervals=layout.intervals,
    )
    if transition is not None:
        if transition.effect == "utopia":
            ctx = LayerContext(t_ms=track_t_ms, logical_w=logical_w, logical_h=logical_h)
            line_bounds = _TEXT_RUN_COMPOSITOR.vertical_bounds(
                ctx,
                _utopia_transition_scope_layers(
                    layout,
                    line,
                    line_style,
                    track_t_ms,
                    transition,
                    logical_h,
                ),
            )
            if line_bounds is not None:
                dy = int(math.floor(animation.dy)) if animation.dy < 0 else int(math.ceil(animation.dy))
                return line_bounds[0] + dy, line_bounds[1] + dy
        return None

    ctx = LayerContext(t_ms=track_t_ms, logical_w=logical_w, logical_h=logical_h)
    layers = _line_layer_stack(layout, track_t_ms)
    if layout.active_rubies and layout.ruby_metrics is not None:
        layers.extend(_ruby_layer_stack(layout, line, track_t_ms, line_style))
    line_bounds = _TEXT_RUN_COMPOSITOR.vertical_bounds(ctx, layers)
    if line_bounds is None:
        return None
    dy = int(math.floor(animation.dy)) if animation.dy < 0 else int(math.ceil(animation.dy))
    return line_bounds[0] + dy, line_bounds[1] + dy


def resolved_page_offset_windows_for_style(
    logical_w: int,
    logical_h: int,
    track: TimingTrack,
    style: Style,
) -> dict[int, tuple[tuple[int, int, float, float], ...]]:
    """Return one persistent per-line page translation window.

    Each tuple is ``(start_ms, end_ms, offset_x, offset_y)`` in the pre-viewport
    logical canvas.  Once a page is displaced, the same translation remains
    active until that page finishes.  The solver accepts only positions whose
    complete static ink envelope remains inside the canvas; it searches the
    authored anchor direction first, then the opposite direction, and falls
    back to the authored position when neither side can contain the page.
    """

    return resolve_page_offset_windows(
        logical_w,
        logical_h,
        track,
        style,
        PageOffsetResolvers(
            display_lines=display_lines_for_style,
            measure_lines=_measure_page_offset_lines,
        ),
    )


def _measure_page_offset_lines(
    logical_w: int,
    logical_h: int,
    track: TimingTrack,
    style: Style,
    display_lines: list[DisplayLine],
) -> list[MeasuredPageLine]:
    """Measure Painter ink geometry required by the page-offset policy."""

    index_of = {id(line): index for index, line in enumerate(track.lines)}
    measurements: list[MeasuredPageLine] = []

    if style.vertical:
        baselines: dict[int, int] = {}
        line_layouts: dict[int, _SayatooLineLayout] = {}
        layout_cache_sig = None
    else:
        baselines = _resolve_display_baselines(
            logical_h, track, display_lines, style
        )
        # The map is keyed by TimingLine identity, not lane.  Several pages can
        # be visible at once and may legitimately reuse the same lane number.
        line_layouts = _resolve_sayatoo_line_layouts(
            logical_w,
            logical_h,
            track,
            display_lines,
            baselines,
            0,
            style,
        )
        layout_cache_sig = _layout_cache_sig(track, style)

    for display_line in display_lines:
        track_index = index_of.get(id(display_line.line))
        if track_index is None:
            continue
        page_id = (int(display_line.section_index), int(display_line.page_index))
        line_style = _style_for_line(style, display_line.line)
        if style.vertical:
            ink_rect = _display_line_vertical_ink_rect(
                logical_w,
                logical_h,
                track,
                display_line,
                line_style,
                # Collision avoidance protects only undecorated main glyphs.
                include_glow=False,
            )
            axis_bounds = (
                None if ink_rect is None else (ink_rect[0], ink_rect[2])
            )
            cross_bounds = (
                None if ink_rect is None else (ink_rect[1], ink_rect[3])
            )
            axis_anchor = _resolve_vertical_columns(
                logical_w, track, [display_line], line_style
            ).get(display_line.lane)
        else:
            ink_rect = _display_line_horizontal_ink_rect(
                logical_w,
                logical_h,
                track,
                display_line,
                style,
                baselines,
                line_layouts,
                layout_cache_sig=layout_cache_sig,
                # Collision avoidance protects only undecorated main glyphs.
                include_glow=False,
            )
            axis_bounds = (
                None if ink_rect is None else (ink_rect[1], ink_rect[3])
            )
            cross_bounds = (
                None if ink_rect is None else (ink_rect[0], ink_rect[2])
            )
            line_layout = line_layouts.get(id(display_line.line))
            axis_anchor = (
                line_layout.baseline_y
                if line_layout is not None
                else baselines.get(display_line.lane)
            )
        collision_window = (
            _display_line_static_collision_window(display_line, style)
            if axis_bounds is not None
            else None
        )
        measurements.append(
            MeasuredPageLine(
                track_index=track_index,
                page_id=page_id,
                display_start_ms=int(display_line.display_start_ms),
                display_end_ms=int(display_line.display_end_ms),
                page_style=line_style,
                collision_start_ms=(
                    None if collision_window is None else collision_window[0]
                ),
                collision_end_ms=(
                    None if collision_window is None else collision_window[1]
                ),
                axis_bounds=axis_bounds,
                cross_bounds=cross_bounds,
                axis_anchor=(
                    None if axis_anchor is None else float(axis_anchor)
                ),
            )
        )

    return measurements


def resolved_page_offsets_for_style(
    logical_w: int,
    logical_h: int,
    track: TimingTrack,
    style: Style,
    *,
    t_ms: int | None = None,
) -> dict[int, tuple[float, float]]:
    """Resolve the page translation active at ``t_ms`` for each track line.

    ``t_ms=None`` keeps the older inspection API useful by returning the first
    placement interval for each line.  Rendering consumers always pass the
    current display time.
    """

    return page_offsets_at_time(
        resolved_page_offset_windows_for_style(
            logical_w,
            logical_h,
            track,
            style,
        ),
        t_ms=t_ms,
    )


def _display_line_vertical_envelope(
    logical_w: int,
    logical_h: int,
    track: TimingTrack,
    display_line: DisplayLine,
    style: Style,
    baselines: dict[int, int],
    line_layouts: dict[int, _SayatooLineLayout],
    *,
    layout_cache_sig: tuple | None,
) -> tuple[int, int] | None:
    """Return the final static Y ink interval used for placement checks."""

    bounds = _display_line_horizontal_ink_rect(
        logical_w,
        logical_h,
        track,
        display_line,
        style,
        baselines,
        line_layouts,
        layout_cache_sig=layout_cache_sig,
    )
    if bounds is None:
        return None
    return bounds[1], bounds[3]


def _display_line_horizontal_ink_rect(
    logical_w: int,
    logical_h: int,
    track: TimingTrack,
    display_line: DisplayLine,
    style: Style,
    baselines: dict[int, int],
    line_layouts: dict[int, _SayatooLineLayout],
    *,
    layout_cache_sig: tuple | None,
    include_glow: bool = True,
) -> tuple[int, int, int, int] | None:
    """Return the undecorated main-glyph rectangle used by collision checks.

    Ruby, glow, shadow, both strokes, layout cells and animation trajectories
    are deliberately excluded.  Both layout semantics therefore collide on
    the same simple quantity: the actual main-text glyph paths.
    """

    line = display_line.line
    line_style = _style_for_line(style, display_line.line)
    line_layout = line_layouts.get(id(line))
    has_role_labels = _line_has_role_labels(line)
    line_x = (
        line_layout.text_x
        if line_layout is not None and not has_role_labels
        else None
    )
    baseline_y = (
        line_layout.baseline_y
        if line_layout is not None
        else baselines.get(display_line.lane, logical_h // 2)
    )
    lane = display_line.lane if line_style.dual_line_layout else None
    # 碰撞避让要反复收缩、反复重量，同一行的墨迹在一轮里会被问上十来遍（真实工程
    # 56 行问出 627 次）。结果只取决于下面这几项，排版区间内它们不变，缓存住。
    cache = getattr(_LAYOUT_PASS, "ink_rects", None)
    cache_key = None
    if cache is not None:
        cache_key = (
            logical_w,
            logical_h,
            id(track),
            id(line),
            id(line_style),
            baseline_y,
            line_x,
            lane,
            layout_cache_sig,
            include_glow,
        )
        if cache_key in cache:
            return cache[cache_key]
    layout = _layout_line(
        track,
        line,
        line_style,
        logical_w,
        logical_h,
        baseline_y=baseline_y,
        line_x=line_x,
        lane=lane,
        cache_sig=layout_cache_sig,
    )
    rect = None if layout is None else _line_main_text_ink_rect(layout)
    if cache_key is not None:
        cache[cache_key] = rect
        # 键里有 id()：对象被回收后地址会被复用，这里把它们按住。
        _LAYOUT_PASS.tracks.append(track)
        _LAYOUT_PASS.lines.append(line)
        _LAYOUT_PASS.styles.append(line_style)
    return rect


def _line_main_text_ink_rect(
    layout: _LineLayout,
) -> tuple[int, int, int, int] | None:
    """Union of main-text glyph paths, before every painted decoration."""

    bounds: list[QRectF] = []
    for run in _text_glyph_runs(layout.text_layout, layout.has_inline_styles):
        rect = _glyph_run_path(run, layout.baseline_y).boundingRect()
        if not rect.isEmpty():
            bounds.append(rect)
    if not bounds:
        return None
    return (
        int(math.floor(min(rect.left() for rect in bounds))),
        int(math.floor(min(rect.top() for rect in bounds))),
        int(math.ceil(max(rect.right() for rect in bounds))),
        int(math.ceil(max(rect.bottom() for rect in bounds))),
    )


def _line_static_ink_rect(
    layout: _LineLayout,
    *,
    include_glow: bool = True,
) -> tuple[int, int, int, int] | None:
    """Return the painted main/Ruby rectangle without layout line spacing."""

    bounds: list[tuple[float, float, float, float]] = []
    for run in _text_glyph_runs(layout.text_layout, layout.has_inline_styles):
        path = _glyph_run_path(run, layout.baseline_y)
        visual = _painted_path_ink_rect(
            path,
            run[0].style,
            ruby=False,
            include_glow=include_glow,
        )
        if visual is not None:
            bounds.append(visual)
    for glyph in _bitmap_guide_glyphs(layout.text_layout):
        rect = _bitmap_guide_target_rect(glyph, layout.baseline_y)
        if rect is not None and not rect.isEmpty():
            bounds.append(
                (
                    float(rect.left()),
                    float(rect.top()),
                    float(rect.right()),
                    float(rect.bottom()),
                )
            )

    for ruby_layout in layout.ruby_layouts:
        ruby_style = ruby_layout.style
        ruby_font = ruby_layout.font or layout.ruby_font
        ruby_metrics = ruby_layout.metrics or layout.ruby_metrics
        if ruby_metrics is None:
            continue
        reading = (
            "".join(reversed(_ruby_utopia_visual_units(ruby_layout.ruby.reading)))
            if layout.rtl
            else ruby_layout.ruby.reading
        )
        path, _rect = _ruby_text_path_and_rect(
            reading,
            ruby_font,
            ruby_metrics,
            ruby_layout.x,
            ruby_layout.baseline_y,
            ruby_layout.target_width,
            ruby_style,
            base_text=ruby_layout.ruby.kanji,
        )
        visual = _painted_path_ink_rect(
            path,
            ruby_style,
            ruby=True,
            include_glow=include_glow,
        )
        if visual is not None:
            bounds.append(visual)

    if not bounds:
        return None
    return (
        int(math.floor(min(left for left, _top, _right, _bottom in bounds))),
        int(math.floor(min(top for _left, top, _right, _bottom in bounds))),
        int(math.ceil(max(right for _left, _top, right, _bottom in bounds))),
        int(math.ceil(max(bottom for _left, _top, _right, bottom in bounds))),
    )


def _line_static_vertical_ink_bounds(
    layout: _LineLayout,
    *,
    include_glow: bool = True,
) -> tuple[int, int] | None:
    """Return the static painted Y envelope without layout line spacing.

    Placement collisions use actual main-text/Ruby glyph geometry plus stroke,
    shadow and glow extents.  Font metric cells, ``line_gap_px`` and every
    entry/exit or per-character animation trajectory are deliberately absent.
    The solver adds the overlapped layout's line gap exactly once, after these
    per-line ink bounds have been measured.
    """

    bounds = _line_static_ink_rect(layout, include_glow=include_glow)
    if bounds is None:
        return None
    return bounds[1], bounds[3]


def _painted_path_ink_rect(
    path: QPainterPath,
    style: Style,
    *,
    ruby: bool,
    include_glow: bool = True,
) -> tuple[float, float, float, float] | None:
    """Return one path's static painted rectangle for both colour states."""

    rect = path.boundingRect()
    if rect.isEmpty():
        return None
    if ruby:
        stroke_width = _ruby_stroke_width(style)
        stroke2_width = _ruby_stroke2_width(style)
        decoration = _ruby_decoration_kind(style)
        shadow_dx = _ruby_shadow_dx(style)
        shadow_dy = _ruby_shadow_dy(style)
        glow_extent = max(
            _glow_extent(
                stroke_width,
                stroke2_width,
                _ruby_glow_radius(style, after=after),
            )
            for after in (False, True)
        )
    else:
        stroke_width = max(int(style.stroke_width_px), 0)
        stroke2_width = _main_stroke2_width(style)
        decoration = style.decoration_kind
        shadow_dx = int(style.shadow_offset_x)
        shadow_dy = int(style.shadow_offset_y)
        glow_extent = max(
            _glow_extent(
                stroke_width,
                stroke2_width,
                _glow_radius(style, after=after),
            )
            for after in (False, True)
        )

    stroke_extent = _visual_stroke_extent(stroke_width, stroke2_width)
    extent = max(
        stroke_extent,
        glow_extent if include_glow and decoration == "glow" else 0,
    )
    left = float(rect.left()) - extent
    top = float(rect.top()) - extent
    right = float(rect.right()) + extent
    bottom = float(rect.bottom()) + extent
    if decoration == "shadow":
        left += min(shadow_dx, 0)
        right += max(shadow_dx, 0)
        top += min(shadow_dy, 0)
        bottom += max(shadow_dy, 0)
    return left, top, right, bottom


def _painted_path_vertical_bounds(
    path: QPainterPath,
    style: Style,
    *,
    ruby: bool,
    include_glow: bool = True,
) -> tuple[float, float] | None:
    """Return one path's painted static Y bounds for both karaoke colour states."""

    rect = path.boundingRect()
    if rect.isEmpty():
        return None
    if ruby:
        stroke_width = _ruby_stroke_width(style)
        stroke2_width = _ruby_stroke2_width(style)
        decoration = _ruby_decoration_kind(style)
        shadow_dy = _ruby_shadow_dy(style)
        glow_extent = max(
            _glow_extent(
                stroke_width,
                stroke2_width,
                _ruby_glow_radius(style, after=after),
            )
            for after in (False, True)
        )
    else:
        stroke_width = max(int(style.stroke_width_px), 0)
        stroke2_width = _main_stroke2_width(style)
        decoration = style.decoration_kind
        shadow_dy = int(style.shadow_offset_y)
        glow_extent = max(
            _glow_extent(
                stroke_width,
                stroke2_width,
                _glow_radius(style, after=after),
            )
            for after in (False, True)
        )

    stroke_extent = _visual_stroke_extent(stroke_width, stroke2_width)
    extent = max(
        stroke_extent,
        glow_extent if include_glow and decoration == "glow" else 0,
    )
    top = float(rect.top()) - extent
    bottom = float(rect.bottom()) + extent
    if decoration == "shadow" and shadow_dy:
        top += min(shadow_dy, 0)
        bottom += max(shadow_dy, 0)
    return top, bottom


def _display_line_horizontal_envelope(
    logical_w: int,
    logical_h: int,
    track: TimingTrack,
    display_line: DisplayLine,
    line_style: Style,
) -> tuple[int, int] | None:
    """Return the final static X ink interval for a vertical lyric line."""

    bounds = _display_line_vertical_ink_rect(
        logical_w,
        logical_h,
        track,
        display_line,
        line_style,
    )
    if bounds is None:
        return None
    return bounds[0], bounds[2]


def _display_line_vertical_ink_rect(
    logical_w: int,
    logical_h: int,
    track: TimingTrack,
    display_line: DisplayLine,
    line_style: Style,
    *,
    include_glow: bool = True,
) -> tuple[int, int, int, int] | None:
    """Return the undecorated main-glyph rectangle for a vertical line."""

    column = _resolve_vertical_columns(
        logical_w, track, [display_line], line_style
    ).get(display_line.lane)
    render_line = _line_with_guide_symbol(display_line.line)
    layout = _layout_vertical_line(
        track,
        render_line,
        line_style,
        logical_w,
        logical_h,
        column_x=column,
        source_line=display_line.line,
    )
    if layout is None:
        return None
    path_bounds = layout.text_path.boundingRect()
    if path_bounds.isEmpty():
        return None
    return (
        int(math.floor(path_bounds.left())),
        int(math.floor(path_bounds.top())),
        int(math.ceil(path_bounds.right())),
        int(math.ceil(path_bounds.bottom())),
    )


def _line_effect_extent(
    style: Style,
    *,
    vertical_axis: bool,
    include_glow: bool = True,
) -> int:
    stroke2 = _main_stroke2_width(style)
    extent = _visual_stroke_extent(style.stroke_width_px, stroke2)
    if include_glow and style.decoration_kind == "glow":
        extent = max(
            extent,
            _glow_extent(
                style.stroke_width_px,
                stroke2,
                max(
                    _glow_radius(style, after=False),
                    _glow_radius(style, after=True),
                ),
            ),
        )
    elif style.decoration_kind == "shadow":
        shadow = style.shadow_offset_y if vertical_axis else style.shadow_offset_x
        extent += abs(int(shadow))
    return max(int(extent), 0)


def _line_envelope_sample_times(
    display_line: DisplayLine, style: Style
) -> tuple[int, ...]:
    start = int(display_line.display_start_ms)
    end = max(int(display_line.display_end_ms), start + 1)
    sing_start = _line_start_ms(display_line.line)
    sing_end = _line_end_ms(display_line.line)
    candidates = {
        start,
        start + 1,
        start + max(int(style.entry_lead_ms), 0) // 2,
        start + max(int(style.entry_lead_ms), 0),
        sing_start,
        (sing_start + sing_end) // 2,
        max(sing_end - 1, start),
        end - max(int(style.exit_fade_ms), 0),
        end - max(int(style.exit_fade_ms), 0) // 2,
        end - 1,
    }
    return tuple(sorted(min(max(value, start), end - 1) for value in candidates))


def _transform_vertical_bounds(
    top: int,
    bottom: int,
    logical_h: int,
    style: Style,
) -> tuple[int, int]:
    scale = max(style.viewport_scale_pct, 1) / 100.0
    offset_y = style.viewport_offset_y
    if scale == 1.0 and offset_y == 0:
        return top, bottom
    _frac_x, frac_y = _VIEWPORT_PIVOT_FRACTIONS.get(
        style.viewport_align, _VIEWPORT_PIVOT_FRACTIONS["center"]
    )
    pivot_y = logical_h * frac_y
    mapped_top = offset_y + pivot_y + (top - pivot_y) * scale
    mapped_bottom = offset_y + pivot_y + (bottom - pivot_y) * scale
    return int(math.floor(mapped_top)), int(math.ceil(mapped_bottom))


def _resolve_sayatoo_line_layouts(
    img_w: int,
    img_h: int,
    track: TimingTrack,
    display_lines: list[DisplayLine],
    baselines: dict[int, int],
    t_ms: int,
    style: Style,
) -> dict[int, _SayatooLineLayout]:
    """Resolve row-local union bounds before applying row alignment.

    Sayatoo's CoreSuites aligns the complete ``LineDrawingData``.  Signal modules
    therefore contribute to the line width before ``row1/row2`` alignment is
    applied, instead of being painted later in screen coordinates.
    """
    # 每次要量整轨墨迹都会先解一遍行布局（真实工程一次 28ms），而碰撞避让一轮里
    # 要量好几遍。同样的入参在排版区间内答案不变，缓存住。
    cache = getattr(_LAYOUT_PASS, "sayatoo_layouts", None)
    cache_key = None
    if cache is not None:
        cache_key = (
            img_w,
            img_h,
            id(track),
            id(style),
            int(t_ms),
            tuple(
                (id(dl.line), dl.lane) for dl in display_lines
            ),
            tuple(sorted(baselines.items())),
        )
        hit = cache.get(cache_key)
        if hit is not None:
            return hit
    layouts: dict[int, _SayatooLineLayout] = {}
    signal_metrics = _signal_layout_metrics(style) if style.lit_enabled else None
    signal_head_ids = _signal_head_context(track, style) if signal_metrics else None
    index_of_signal_lines = (
        {id(line): index for index, line in enumerate(track.lines)}
        if signal_head_ids is not None
        else None
    )
    for display_line in display_lines:
        line = display_line.line
        if line.is_blank or not line.chars:
            continue
        line_style = _style_for_line(style, line)
        render_line = _line_with_guide_symbol(line)
        font = _build_font(line_style)
        metrics = QFontMetrics(font)
        latin_font = _build_latin_font(line_style)
        font_for = _make_font_for(line_style, font, latin_font)
        latin_metrics = QFontMetrics(latin_font) if font_for is not None else metrics
        active_rubies = _active_rubies_for_line(track.rubies, line)
        ruby_metrics = QFontMetrics(_build_ruby_font(line_style)) if active_rubies else None
        if _line_has_role_labels(render_line):
            measure_layout = _build_role_text_layout(
                render_line, line_style, x0=0, baseline_y=0
            )
            char_widths, _measure_ranges = _role_char_geometry_by_index(
                render_line, measure_layout
            )
        else:
            char_widths = [
                (
                    _vector_glyph_width(
                        c.vector_glyph,
                        _style_for_role_in_layout(line_style, c.role_label),
                    )
                    if c.vector_glyph is not None
                    else _char_layout_width(
                        c.text, font, metrics, latin_metrics, font_for, line_style
                    )
                )
                for c in render_line.chars
            ]
        char_gaps, ruby_left, ruby_right = _ruby_char_gaps(
            render_line, char_widths, active_rubies, line_style
        )
        text_w = _line_text_width(char_widths, line_style) + sum(char_gaps)
        # 行盒左右不给描边留位（见 _line_total_width），只让 ruby 溢出撑开。
        left_ext = ruby_left
        right_ext = ruby_right
        text_line_w = max(int(round(text_w)) + left_ext + right_ext, 1)
        center_line = _line_center_override(track, line, line_style)
        signal_x: float | None = None
        if (
            signal_metrics is not None
            and _line_has_active_signal(
                line,
                t_ms,
                line_style,
                is_signal_head=(
                    signal_head_ids is None
                    or index_of_signal_lines.get(id(line)) in signal_head_ids
                ),
            )
        ):
            # Sayatoo CoreSuites aligns the *union* of the lyric text box and the
            # signal-module bounds (the LineDrawingData width), then applies
            # row1/row2 alignment to that union.  So an enabled guide cue widens
            # the line: under left/centre alignment the signal takes the row
            # anchor and the lyric text shifts right by the group width; under
            # right alignment the text stays put and the signal extends left.
            #
            # The union uses the indicator's *offset-free* span so that the
            # volume/lit X offset nudges only the indicator, not the text layout:
            # ``volume_offset_x`` therefore moves the bars (``signal_x``) while
            # ``text_x`` stays put, which is what the offset control should do.
            draw_left = _signal_local_x(signal_metrics, line_style)
            natural_left = draw_left - _signal_offset_x(line_style)
            natural_right = natural_left + signal_metrics.group_width
            union_left = min(-float(left_ext), natural_left)
            union_right = max(float(text_w) + right_ext, natural_right)
            union_w = max(int(round(union_right - union_left)), 1)
            union_x = _resolve_line_x_smart(
                img_w, union_w, track, line, line_style, display_line.lane,
                center_override=center_line,
            )
            text_x = float(union_x) - union_left
            signal_x = text_x + draw_left
        else:
            text_x = float(
                _resolve_line_x_smart(
                    img_w, text_line_w, track, line, line_style, display_line.lane,
                    center_override=center_line,
                )
                + left_ext
            )
        if int(getattr(line, "layout_index", 0) or 0) > 0:
            # 行引用了额外布局 → 垂直几何（锚点/余白/行距/行数）按该布局单独解析。
            baseline_y = _resolve_display_baselines(
                img_h, track, [display_line], line_style
            ).get(display_line.lane)
        else:
            baseline_y = baselines.get(display_line.lane)
        if baseline_y is None:
            baseline_y = _resolve_baseline_y(metrics, img_h, line_style, ruby_metrics)
        resolved = _SayatooLineLayout(
            baseline_y=baseline_y,
            text_x=int(round(text_x)),
            line_style=line_style,
            metrics=metrics,
            total_w=text_w,
            signal_x=signal_x,
            signal_y=(
                _signal_lit_y(
                    baseline_y,
                    metrics,
                    signal_metrics.size,
                    line_style,
                    signal_metrics.stroke_extent,
                )
                if signal_metrics is not None and signal_x is not None
                else None
            ),
        )
        # Identity is authoritative for rendering because overlapping pages can
        # reuse a lane.  Keep the lane alias for older helpers/tests that inspect
        # one page at a time; the renderer itself never consumes that alias.
        layouts[id(display_line.line)] = resolved
        layouts[display_line.lane] = resolved
    if cache_key is not None:
        cache[cache_key] = layouts
        # 键里有 id()：对象被回收后地址会被复用，这里把它们按住。
        _LAYOUT_PASS.tracks.append(track)
        _LAYOUT_PASS.styles.append(style)
        _LAYOUT_PASS.lines.extend(dl.line for dl in display_lines)
    return layouts


def _measure_signal_line(
    track: TimingTrack,
    display_line: DisplayLine,
    baselines: dict[int, int],
    img_h: int,
    style: Style,
) -> SignalLineMeasurement:
    line = display_line.line
    line_style = _style_for_line(style, line)
    font = _build_font(line_style)
    metrics = QFontMetrics(font)
    latin_font = _build_latin_font(line_style)
    font_for = _make_font_for(line_style, font, latin_font)
    latin_metrics = QFontMetrics(latin_font) if font_for is not None else metrics
    active_rubies = _active_rubies_for_line(track.rubies, line)
    ruby_metrics = QFontMetrics(_build_ruby_font(line_style)) if active_rubies else None
    render_line = _line_with_guide_symbol(line)
    char_widths = [
        (
            _vector_glyph_width(
                char.vector_glyph,
                _style_for_role_in_layout(line_style, char.role_label),
            )
            if char.vector_glyph is not None
            else _char_layout_width(
                char.text,
                font,
                metrics,
                latin_metrics,
                font_for,
                line_style,
            )
        )
        for char in render_line.chars
    ]
    baseline_y = baselines.get(display_line.lane)
    if baseline_y is None:
        baseline_y = _resolve_baseline_y(metrics, img_h, line_style, ruby_metrics)
    return SignalLineMeasurement(
        baseline_y=baseline_y,
        line_style=line_style,
        metrics=metrics,
        total_w=_line_text_width(char_widths, line_style),
    )


def _paint_signal_lits(
    painter: QPainter,
    img_w: int,
    img_h: int,
    track: TimingTrack,
    display_lines: list[DisplayLine],
    baselines: dict[int, int],
    t_ms: int,
    style: Style,
    *,
    line_layouts: dict[int, _SayatooLineLayout] | None = None,
    line_offsets: dict[int, tuple[float, float]] | None = None,
) -> None:
    _paint_signal_lits_with_ports(
        painter,
        img_w,
        img_h,
        track,
        display_lines,
        baselines,
        t_ms,
        style,
        compositor=_TEXT_RUN_COMPOSITOR,
        measure_line=_measure_signal_line,
        line_layouts=line_layouts,
        line_offsets=line_offsets,
    )


def _signal_lit_groups(
    track: TimingTrack,
    display_lines: list[DisplayLine],
    baselines: dict[int, int],
    img_w: int,
    img_h: int,
    t_ms: int,
    style: Style,
    count: int,
    size: int,
    item_width: int,
    tracking: int,
    stroke_extent: float = 0.0,
    *,
    line_layouts: dict[int, _SayatooLineLayout] | None = None,
    line_offsets: dict[int, tuple[float, float]] | None = None,
) -> list[_SignalLitGroup]:
    return _resolve_signal_lit_groups(
        track,
        display_lines,
        baselines,
        img_w,
        img_h,
        t_ms,
        style,
        count,
        size,
        item_width,
        tracking,
        stroke_extent,
        measure_line=_measure_signal_line,
        line_layouts=line_layouts,
        line_offsets=line_offsets,
    )


def _active_lit_indices(
    track: TimingTrack,
    display_lines: list[DisplayLine],
    t_ms: int,
    style: Style,
    count: int,
) -> set[int]:
    return _resolve_active_lit_indices(
        track,
        display_lines,
        t_ms,
        style,
        count,
        measure_line=_measure_signal_line,
    )


def measure_collision_bands(
    logical_w: int,
    logical_h: int,
    track: TimingTrack,
    style: Style,
    display_lines: list[DisplayLine],
    *,
    time_window: str | None = None,
) -> list[tuple[int, tuple[int, int], LineVisualBand, float]]:
    """Measure ink bands without changing display-time behaviour."""

    if not display_lines:
        return []
    if time_window is None:
        time_window = (
            "stable" if style.allow_entry_exit_animation_overlap else "display"
        )
    if style.vertical:
        baselines: dict[int, int] = {}
        line_layouts: dict[int, _SayatooLineLayout] = {}
        layout_cache_sig = None
    else:
        baselines = _resolve_display_baselines(
            logical_h, track, display_lines, style
        )
        line_layouts = _resolve_sayatoo_line_layouts(
            logical_w,
            logical_h,
            track,
            display_lines,
            baselines,
            0,
            style,
        )
        layout_cache_sig = _layout_cache_sig(track, style)

    measured: list[tuple[int, tuple[int, int], LineVisualBand, float]] = []
    for render_index, display_line in enumerate(display_lines):
        line_style = _style_for_line(style, display_line.line)
        if style.vertical:
            ink_rect = _display_line_vertical_ink_rect(
                logical_w,
                logical_h,
                track,
                display_line,
                line_style,
                # Collision avoidance protects only undecorated main glyphs.
                include_glow=False,
            )
            axis_bounds = (
                None if ink_rect is None else (ink_rect[0], ink_rect[2])
            )
            cross_bounds = (
                None if ink_rect is None else (ink_rect[1], ink_rect[3])
            )
            axis_anchor = _resolve_vertical_columns(
                logical_w, track, [display_line], line_style
            ).get(display_line.lane)
        else:
            ink_rect = _display_line_horizontal_ink_rect(
                logical_w,
                logical_h,
                track,
                display_line,
                style,
                baselines,
                line_layouts,
                layout_cache_sig=layout_cache_sig,
                # Collision avoidance protects only undecorated main glyphs.
                include_glow=False,
            )
            axis_bounds = (
                None if ink_rect is None else (ink_rect[1], ink_rect[3])
            )
            cross_bounds = (
                None if ink_rect is None else (ink_rect[0], ink_rect[2])
            )
            line_layout = line_layouts.get(id(display_line.line))
            axis_anchor = (
                line_layout.baseline_y
                if line_layout is not None
                else baselines.get(display_line.lane)
            )
        if axis_bounds is None:
            continue
        assert cross_bounds is not None
        collision_start, collision_end = _display_line_collision_time_window(
            display_line, style, time_window=time_window
        )
        if collision_end <= collision_start:
            continue
        page_id = (
            int(display_line.section_index),
            int(display_line.page_index),
        )
        measured.append(
            (
                render_index,
                page_id,
                LineVisualBand(
                    line_id=render_index,
                    page_id=page_id,
                    display_start_ms=collision_start,
                    display_end_ms=collision_end,
                    axis_min=float(axis_bounds[0]),
                    axis_max=float(axis_bounds[1]),
                    entry_start_ms=int(display_line.display_start_ms),
                    axis_anchor=(
                        None if axis_anchor is None else float(axis_anchor)
                    ),
                    cross_min=float(cross_bounds[0]),
                    cross_max=float(cross_bounds[1]),
                ),
                max(float(line_style.line_gap_px), 0.0),
            )
        )
    return measured


def pixel_collision_squeeze_pairs(
    logical_w: int,
    logical_h: int,
    track: TimingTrack,
    style: Style,
    display_lines: list[DisplayLine],
) -> tuple[tuple[int, int], ...]:
    """Return pairs conflicting in the configured time window and pixel axis."""

    measured = measure_collision_bands(
        logical_w, logical_h, track, style, display_lines
    )
    conflicts: list[tuple[int, int]] = []
    for incoming_pos, (
        incoming_index,
        incoming_page,
        incoming_band,
        _incoming_gap,
    ) in enumerate(measured):
        for previous_index, previous_page, previous_band, _previous_gap in measured[
            :incoming_pos
        ]:
            if previous_page == incoming_page:
                continue
            if not time_windows_overlap(incoming_band, previous_band):
                continue
            if not bands_require_separation(
                incoming_band, previous_band, 0.0
            ):
                continue
            pair = (previous_index, incoming_index)
            if pair not in conflicts:
                conflicts.append(pair)
    return tuple(conflicts)


def _secondary_displacement_squeeze_pairs(
    logical_w: int,
    logical_h: int,
    track: TimingTrack,
    style: Style,
    display_lines: list[DisplayLine],
) -> tuple[tuple[int, int], ...]:
    """Return cascade dependencies created by rigid inter-page displacement.

    This is deliberately a discovery-only pass.  It never edits display
    windows or offsets; callers may feed the returned pairs through the
    existing measured-pair squeeze path, which preserves wipe bounds, manual
    overrides, animation reserves and page entry order.
    """

    measured = measure_collision_bands(
        logical_w, logical_h, track, style, display_lines
    )
    if not measured:
        return ()

    page_order: list[tuple[int, int]] = []
    page_entries: dict[
        tuple[int, int], list[tuple[int, LineVisualBand, float]]
    ] = {}
    page_styles: dict[tuple[int, int], Style] = {}
    for render_index, page_id, band, gap in measured:
        if page_id not in page_entries:
            page_order.append(page_id)
            page_entries[page_id] = []
        page_entries[page_id].append((render_index, band, gap))
        page_styles.setdefault(
            page_id, _style_for_line(style, display_lines[render_index].line)
        )

    pages: list[PageVisualBands] = []
    for page_id in page_order:
        page_style = page_styles[page_id]
        position = page_style.line_y_position
        anchor = (
            "start"
            if position == "top"
            else "center"
            if position == "center"
            else "end"
        )
        if style.vertical:
            anchor = "end"
        pages.append(
            PageVisualBands(
                page_id=page_id,
                bands=tuple(
                    band for _render_index, band, _gap in page_entries[page_id]
                ),
                gap_px=max(float(page_style.line_gap_px), 0.0),
                anchor=anchor,
            )
        )

    offsets = solve_page_axis_offsets(
        pages,
        viewport_min=0.0,
        viewport_max=float(logical_w if style.vertical else logical_h),
    )
    if not any(float(offset) != 0.0 for offset in offsets.values()):
        return ()

    conflicts: list[tuple[int, int]] = []
    for incoming_pos, (
        incoming_index,
        incoming_page,
        incoming_band,
        _incoming_gap,
    ) in enumerate(measured):
        incoming_offset = float(offsets.get(incoming_page, 0.0))
        if incoming_offset == 0.0:
            continue
        for (
            previous_index,
            previous_page,
            previous_band,
            _previous_gap,
        ) in measured[:incoming_pos]:
            if previous_page == incoming_page:
                continue
            if not time_windows_overlap(incoming_band, previous_band):
                continue
            previous_offset = float(offsets.get(previous_page, 0.0))
            if previous_offset == 0.0:
                continue
            # Authored-position conflicts already went through the primary
            # squeeze pass.  This pass only covers an incoming page that would
            # have been clear before an earlier page moved into its position.
            if bands_require_separation(incoming_band, previous_band, 0.0):
                continue
            shifted_previous = previous_band.shifted(
                previous_offset
            )
            if not bands_require_separation(incoming_band, shifted_previous, 0.0):
                continue
            pair = (previous_index, incoming_index)
            if pair not in conflicts:
                conflicts.append(pair)
    return tuple(conflicts)


def _retime_measured_collision_bands(
    measured: list[tuple[int, tuple[int, int], LineVisualBand, float]],
    display_lines: list[DisplayLine],
    style: Style,
    changed_indices: tuple[int, ...],
    *,
    time_window: str = "stable",
) -> list[tuple[int, tuple[int, int], LineVisualBand, float]] | None:
    """Reuse measured X/Y rectangles when only display boundaries changed.

    ``None`` requests a full remeasurement when a changed line had no stable
    band before.  That rare case can gain one after synchronization; skipping
    it would miss a collision.
    """

    changed = set(changed_indices)
    measured_indices = {render_index for render_index, _page, _band, _gap in measured}
    if not changed.issubset(measured_indices):
        return None
    retimed: list[tuple[int, tuple[int, int], LineVisualBand, float]] = []
    for render_index, page_id, band, gap in measured:
        if render_index not in changed:
            retimed.append((render_index, page_id, band, gap))
            continue
        collision_start, collision_end = _display_line_collision_time_window(
            display_lines[render_index],
            style,
            time_window=time_window,
        )
        if collision_end <= collision_start:
            continue
        retimed.append(
            (
                render_index,
                page_id,
                replace(
                    band,
                    display_start_ms=int(collision_start),
                    display_end_ms=int(collision_end),
                    entry_start_ms=int(
                        display_lines[render_index].display_start_ms
                    ),
                ),
                gap,
            )
        )
    return retimed


def _apply_measured_section_time_fill(
    logical_w: int,
    logical_h: int,
    track: TimingTrack,
    style: Style,
    display_lines: list[DisplayLine],
) -> list[DisplayLine]:
    """Extend automatic exits toward the nearest-height line on the next page.

    N3's TopLong rule only special-cases the first line and uses the next
    page's first line as its boundary.  The product rule is geometry-driven:
    every line on a non-tail page matches the undecorated main-text box whose
    vertical (or vertical-text horizontal) position is closest on the next
    page.  Tail-page lines share the page's natural final boundary.
    """

    if not style.auto_fill_section_time or not display_lines:
        return display_lines
    time_window = (
        "stable" if style.allow_entry_exit_animation_overlap else "display"
    )
    measured = measure_collision_bands(
        logical_w,
        logical_h,
        track,
        style,
        display_lines,
        time_window=time_window,
    )
    bands = {
        render_index: band
        for render_index, _page_id, band, _gap in measured
    }
    if not bands:
        return display_lines

    page_order: list[tuple[int, int]] = []
    page_indices: dict[tuple[int, int], list[int]] = {}
    for index, item in enumerate(display_lines):
        page_id = (int(item.section_index), int(item.page_index))
        if page_id not in page_indices:
            page_order.append(page_id)
            page_indices[page_id] = []
        page_indices[page_id].append(index)
    next_page: dict[tuple[int, int], tuple[int, int] | None] = {}
    for position, page_id in enumerate(page_order):
        following = page_order[position + 1] if position + 1 < len(page_order) else None
        next_page[page_id] = (
            following
            if following is not None and following[0] == page_id[0]
            else None
        )

    # Match against the final page placement, not merely the authored row.
    # A whole incoming page may have moved while resolving an earlier visual
    # collision.  Reconstruct that same rigid page translation from the now
    # stable display windows before comparing main-text boxes.
    page_entries: dict[tuple[int, int], list[tuple[LineVisualBand, float]]] = {}
    for _render_index, page_id, band, gap in measured:
        page_entries.setdefault(page_id, []).append((band, gap))
    pages: list[PageVisualBands] = []
    for page_id in page_order:
        entries = page_entries.get(page_id, [])
        if not entries:
            continue
        page_style = _style_for_line(
            style, display_lines[page_indices[page_id][0]].line
        )
        position = page_style.line_y_position
        anchor = "start" if position == "top" else "center" if position == "center" else "end"
        if style.vertical:
            anchor = "end"
        pages.append(
            PageVisualBands(
                page_id=page_id,
                bands=tuple(band for band, _gap in entries),
                gap_px=max((gap for _band, gap in entries), default=0.0),
                anchor=anchor,
            )
        )
    page_offsets = solve_page_axis_offsets(
        pages,
        viewport_min=0.0,
        viewport_max=float(logical_w if style.vertical else logical_h),
    )
    bands = {
        index: band.shifted(float(page_offsets.get(band.page_id, 0.0)))
        for index, band in bands.items()
    }

    def match_page_bands(
        source_indices: list[int], candidate_indices: list[int]
    ) -> dict[int, int]:
        """Return a validated one-to-one minimum-distance box assignment."""

        sources = [index for index in source_indices if index in bands]
        candidates = [index for index in candidate_indices if index in bands]
        if not sources or not candidates:
            return {}
        costs: dict[tuple[int, int], float] = {}
        for source_pos, source_index in enumerate(sources):
            source = bands[source_index]
            source_height = max(float(source.axis_max - source.axis_min), 1.0)
            source_center = (source.axis_min + source.axis_max) / 2.0
            for candidate_pos, candidate_index in enumerate(candidates):
                candidate = bands[candidate_index]
                candidate_height = max(
                    float(candidate.axis_max - candidate.axis_min), 1.0
                )
                candidate_center = (candidate.axis_min + candidate.axis_max) / 2.0
                center_distance = abs(candidate_center - source_center)
                tolerance = max(source_height, candidate_height)
                # A corresponding row may shift or change font size, but its
                # centre must remain within one complete main-text box height.
                # Anything farther away is an adjacent/different row, not a
                # merely imperfect match.
                if center_distance > tolerance:
                    continue
                height_delta = abs(candidate_height - source_height)
                costs[(source_pos, candidate_pos)] = (
                    center_distance / tolerance
                    + 0.25 * height_delta / tolerance
                )

        memo: dict[
            tuple[int, int], tuple[int, float, tuple[tuple[int, int], ...]]
        ] = {}

        def solve(
            source_pos: int, used_mask: int
        ) -> tuple[int, float, tuple[tuple[int, int], ...]]:
            key = (source_pos, used_mask)
            cached = memo.get(key)
            if cached is not None:
                return cached
            if source_pos >= len(sources):
                return 0, 0.0, ()
            best = solve(source_pos + 1, used_mask)
            for candidate_pos in range(len(candidates)):
                bit = 1 << candidate_pos
                cost = costs.get((source_pos, candidate_pos))
                if used_mask & bit or cost is None:
                    continue
                count, total, pairs = solve(source_pos + 1, used_mask | bit)
                proposal = (
                    count + 1,
                    total + cost,
                    ((source_pos, candidate_pos),) + pairs,
                )
                if (
                    proposal[0] > best[0]
                    or (
                        proposal[0] == best[0]
                        and proposal[1] < best[1] - 1e-9
                    )
                    or (
                        proposal[0] == best[0]
                        and abs(proposal[1] - best[1]) <= 1e-9
                        and proposal[2] < best[2]
                    )
                ):
                    best = proposal
            memo[key] = best
            return best

        _count, _cost, pairs = solve(0, 0)
        return {
            sources[source_pos]: candidates[candidate_pos]
            for source_pos, candidate_pos in pairs
        }

    changed = list(display_lines)
    gap_ms = max(int(style.line_lane_gap_ms), 0)
    for page_id in page_order:
        indices = page_indices[page_id]
        following = next_page[page_id]
        if following is None:
            page_collision_end = max(
                (int(bands[index].display_end_ms) for index in indices if index in bands),
                default=None,
            )
            if page_collision_end is None:
                continue
            targets = {index: page_collision_end for index in indices}
        else:
            candidates = [index for index in page_indices[following] if index in bands]
            if not candidates:
                continue
            matches = match_page_bands(indices, candidates)
            targets = {}
            for index, matched in matches.items():
                targets[index] = int(bands[matched].display_start_ms) - gap_ms

        for index, collision_end in targets.items():
            item = changed[index]
            if item.line.display_end_override_ms is not None:
                continue
            full_end = int(collision_end)
            if time_window == "stable":
                full_end += _exit_animation_ms(style, item.line)
            new_end = max(int(item.display_end_ms), full_end)
            if new_end != item.display_end_ms:
                changed[index] = replace(item, display_end_ms=new_end)
    return changed


def animation_guard_ports_for_style(
    logical_w: int,
    logical_h: int,
    track: TimingTrack,
    style: Style,
) -> AnimationGuardPorts:
    """Expose Painter collision measurements through the layout guard ports."""

    return AnimationGuardPorts(
        entry_animation_ms=lambda line: _entry_animation_ms(style, line),
        exit_animation_ms=lambda line: _exit_animation_ms(style, line),
        measure=lambda items, time_window: measure_collision_bands(
            logical_w,
            logical_h,
            track,
            style,
            items,
            time_window=time_window,
        ),
        retime=lambda measured, items, indices, time_window: (
            _retime_measured_collision_bands(
                measured,
                items,
                style,
                indices,
                time_window=time_window,
            )
        ),
    )



def display_lines_for_style(
    track: TimingTrack,
    style: Style,
    *,
    logical_w: int | None = None,
    logical_h: int | None = None,
) -> list[DisplayLine]:
    """Resolve display windows, squeezing only measured per-line collisions.

    Both modes run one timing pipeline.  ``allow_inter_page_line_overlap``
    decides only whether cross-page *avoidance* runs -- the measured squeeze and
    the inter-page animation gap.  Lead-in, tail, lane gap and page sync are user
    settings computed by the identical passes either way, so the two modes can
    only differ on windows that avoidance would actually have consumed.
    """

    return resolve_display_lines_for_style(
        track,
        style,
        display_line_compute_kwargs(style),
        StyleDisplayResolutionPorts(
            build=lambda width, height, base_kwargs: DisplayResolutionPorts(
                compute=lambda **overrides: compute_display_lines(
                    track,
                    **base_kwargs,
                    **overrides,
                ),
                resolve_timing=lambda items, enforce_gap: resolve_display_timing(
                    style,
                    items,
                    animation_guard_ports_for_style(
                        width,
                        height,
                        track,
                        style,
                    ),
                    enforce_inter_page_gap=enforce_gap,
                ),
                collision_pairs=lambda items: pixel_collision_squeeze_pairs(
                    width, height, track, style, items
                ),
                secondary_collision_pairs=lambda items: (
                    _secondary_displacement_squeeze_pairs(
                        width, height, track, style, items
                    )
                ),
                fill_section_time=lambda items: _apply_measured_section_time_fill(
                    width, height, track, style, items
                ),
                apply_animation_guard=lambda items, enforce_gap: (
                    apply_animation_time_guard(
                        style,
                        items,
                        animation_guard_ports_for_style(
                            width,
                            height,
                            track,
                            style,
                        ),
                        enforce_inter_page_gap=enforce_gap,
                    )
                ),
            ),
        ),
        logical_w=logical_w,
        logical_h=logical_h,
    )


def measure_display_line_horizontal_bounds(
    track: TimingTrack,
    style: Style,
    display_line: DisplayLine,
    logical_w: int,
) -> tuple[float, float]:
    """Measure one display line's authored horizontal text bounds."""

    line = display_line.line
    line_style = _style_for_line(style, line)
    total_w = _line_total_width(line, line_style, track.rubies)
    lane = display_line.lane if line_style.dual_line_layout else None
    x0 = _resolve_line_x_smart(
        logical_w,
        total_w,
        track,
        line,
        line_style,
        lane,
        center_override=_line_center_override(track, line, line_style),
    )
    return float(x0), float(x0 + total_w)


def _visible_lines_for_style(
    track: TimingTrack,
    t_ms: int,
    style: Style,
    *,
    logical_w: int | None = None,
    logical_h: int | None = None,
) -> list[DisplayLine]:
    return resolve_visible_display_lines(
        track,
        t_ms,
        style,
        DisplayScheduleResolvers(display_lines=display_lines_for_style),
        logical_w=logical_w,
        logical_h=logical_h,
    )


def display_windows_for_style(
    track: TimingTrack,
    style: Style,
    *,
    logical_w: int | None = None,
    logical_h: int | None = None,
) -> dict[int, tuple[int, int]]:
    """全部可渲染行的显示窗口：``track.lines`` 索引 → (上屏, 消失) 毫秒。

    与预览/导出使用同一套布局参数（含逐行手动覆盖），供字幕轨道 UI
    展示与编辑句子的显示/隐藏时间。
    """
    return resolve_display_windows(
        track,
        style,
        DisplayScheduleResolvers(display_lines=display_lines_for_style),
        logical_w=logical_w,
        logical_h=logical_h,
    )


def display_schedule_for_style(
    track: TimingTrack,
    style: Style,
    *,
    logical_w: int | None = None,
    logical_h: int | None = None,
) -> dict[int, tuple[int, int, int]]:
    """Return ``line index -> (lane, display start, display end)``.

    Native/GPU backends consume this resolved schedule so pagination, manual
    display overrides and lane protection remain owned by the Painter oracle.
    """
    return resolve_display_schedule(
        track,
        style,
        DisplayScheduleResolvers(display_lines=display_lines_for_style),
        logical_w=logical_w,
        logical_h=logical_h,
    )


def build_track_layout_plan(
    track: TimingTrack,
    style: Style,
    *,
    logical_w: int | None = None,
    logical_h: int | None = None,
) -> TrackLayoutPlan:
    """Resolve one plan through Painter's remaining geometry adapters."""
    return _build_semantic_layout_plan(
        track,
        style,
        LayoutPlanResolvers(
            display_lines=display_lines_for_style,
            page_offset_windows=resolved_page_offset_windows_for_style,
        ),
        logical_w=logical_w,
        logical_h=logical_h,
    )



def _signal_display_lines_for_style(
    track: TimingTrack,
    t_ms: int,
    style: Style,
    *,
    logical_w: int | None = None,
    logical_h: int | None = None,
) -> list[DisplayLine]:
    return _resolve_signal_display_lines(
        track,
        t_ms,
        style,
        _visible_lines_for_style,
        logical_w=logical_w,
        logical_h=logical_h,
    )


def _resolve_baseline_y(
    metrics: QFontMetrics,
    img_h: int,
    style: Style,
    ruby_metrics: QFontMetrics | None = None,
) -> int:
    pos = style.line_y_position
    margin = style.line_y_margin_px
    if style.layout_semantics == "n3_1074":
        main_h, main_ascent, main_descent, ruby_extra = _fixed_line_geometry(style)
        if pos == "top":
            return margin + ruby_extra + main_ascent
        if pos == "center":
            return (img_h - main_h) // 2 + main_ascent
        return img_h - margin - main_descent
    pad = _visual_text_padding(style)
    ruby_extra = 0
    if ruby_metrics is not None:
        ruby_extra = _ruby_vertical_extra(style, ruby_metrics)
    if pos == "top":
        return margin + ruby_extra + pad + metrics.ascent()
    if pos == "center":
        block_h = metrics.height() + ruby_extra + pad * 2
        return (img_h - block_h) // 2 + ruby_extra + pad + metrics.ascent()
    # bottom（默认）
    return img_h - margin - pad - metrics.descent()


def _fixed_line_geometry(style: Style) -> tuple[int, int, int, int]:
    font = _build_font(style)
    metrics = QFontMetrics(font)
    ruby_metrics = QFontMetrics(_build_ruby_font(style))
    ruby_extra = _ruby_vertical_extra(style, ruby_metrics)
    if style.layout_semantics == "n3_1074":
        # N3 DrawCharInfo.Height is font size + the first edge only.  Keep the
        # product's larger guide/role glyphs out of the shared lane box; their
        # visual geometry and ruby-anchor participation remain independent.
        font_size = max(int(style.font_size_px), 1)
        edge = max(int(style.stroke_width_px), 0)
        main_h = font_size + edge
        metric_total = max(metrics.ascent() + metrics.descent(), 1)
        main_descent = font_size * max(metrics.descent(), 0) // metric_total + edge // 2
        main_descent = min(max(main_descent, 0), main_h)
        main_ascent = main_h - main_descent
        # N3 anchors top/middle/bottom against the main DrawLine box. Ruby is
        # positioned above DrawTop afterwards and may extend into the margin.
        return main_h, main_ascent, main_descent, 0
    pad = _visual_text_padding(style)
    main_h = metrics.ascent() + metrics.descent() + pad * 2
    return main_h, metrics.ascent() + pad, metrics.descent() + pad, ruby_extra


def _resolve_display_baselines(
    img_h: int,
    track: TimingTrack,
    display_lines: list[DisplayLine],
    style: Style,
) -> dict[int, int]:
    if not style.dual_line_layout:
        font = _build_font(style)
        metrics = QFontMetrics(font)
        line = display_lines[0].line if display_lines else None
        ruby_metrics = (
            QFontMetrics(_build_ruby_font(style))
            if line is not None and _active_rubies_for_line(track.rubies, line)
            else None
        )
        baseline = _resolve_baseline_y(metrics, img_h, style, ruby_metrics)
        if style.line_horizontal_layout == "per_row":
            baseline += style.row1_offset_y
        return {0: baseline}

    main_h, main_ascent, main_descent, ruby_extra = _fixed_line_geometry(style)
    gap = int(style.line_gap_px)
    margin = style.line_y_margin_px
    lanes = _lane_count(style)
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
        # per_row 是 Sayatoo 双行遗留：Y 偏移只定义了前两行。
        if 0 in baselines:
            baselines[0] += style.row1_offset_y
        if 1 in baselines:
            baselines[1] += style.row2_offset_y
    return baselines


# ---------------------------------------------------------------------------
# 竖排（縦書き）
# ---------------------------------------------------------------------------

def _paint_line_vertical(
    painter: QPainter,
    img_w: int,
    img_h: int,
    track: TimingTrack,
    line: TimingLine,
    t_ms: int,
    style: Style,
    *,
    column_x: int | None,
    lane: int | None = None,
    line_plan: LineLayoutPlan | None = None,
) -> None:
    """竖排单列渲染：字符上→下堆叠、卡拉ok 扫光上→下。

    默认走 :func:`_paint_line_vertical_layers`（整条路径迁入 LayerCompositor + bake 缓存，
    与横排一致）；``KROK_SUBTITLE_VERTICAL_LAYER=0`` 回退到 :func:`_paint_line_vertical_direct`
    逐帧直绘（亦作像素一致性 oracle）。两条路径像素一致。
    """
    source_line = line
    line = line_plan.render_line if line_plan is not None else _line_with_guide_symbol(line)
    layout = _layout_vertical_line(
        track, line, style, img_w, img_h,
        column_x=column_x, source_line=source_line,
        resolved_intervals=(
            line_plan.resolved_intervals if line_plan is not None else None
        ),
    )
    if layout is None:
        return
    if _vertical_layer_enabled():
        _paint_line_vertical_layers(painter, layout, line, t_ms, style)
    else:
        _paint_line_vertical_direct(painter, layout, line, t_ms, style)


def _paint_line_vertical_direct(
    painter: QPainter,
    layout: _VerticalLineLayout,
    line: TimingLine,
    t_ms: int,
    style: Style,
) -> None:
    """竖排逐帧直绘（旧路径，A/B oracle + env 回退）。"""
    stroke2_width = _main_stroke2_width(style)
    band = _vertical_fill_band(
        layout.cells,
        layout.intervals,
        t_ms,
        line=line,
        active_rubies=layout.active_rubies,
        ruby_main_progress_mode=style.ruby_main_progress_mode,
    )
    # 「未唱」层。N3 硬分割：发光存在且已唱层会覆盖已唱带时，未唱层整体裁到
    # 扫光线之下（见 _vertical_before_clip_rect 注释）。
    before_clip = None
    if (
        band is not None
        and style.decoration_kind == "glow"
        and _glow_radius(style, after=False) > 0
    ):
        before_clip = _vertical_before_clip_rect(
            layout.column_x,
            layout.cell_w,
            band[1],
            _vertical_before_clip_pad(
                style.stroke_width_px,
                stroke2_width,
                _glow_radius(style, after=False),
                style.shadow_offset_x,
                style.shadow_offset_y,
            ),
        )
    painter.save()
    try:
        if before_clip is not None:
            painter.setClipRect(before_clip)
        _paint_text_layer_stack(
            painter,
            layout.text_path,
            layout.line_rect,
            layout.colors.before,
            style,
            stroke_width=style.stroke_width_px,
            stroke2_width=stroke2_width,
            shadow_dx=style.shadow_offset_x,
            shadow_dy=style.shadow_offset_y,
            glow_radius=_glow_radius(style, after=False),
        )
    finally:
        painter.restore()

    # 「已唱」层：纵向裁剪带 [y_top, scan]
    if band is not None:
        y0, y_scan = band
        pad = _vertical_after_clip_pad(style)
        painter.save()
        try:
            painter.setClipRect(
                _vertical_after_clip_rect(layout.column_x, layout.cell_w, y0, y_scan, pad)
            )
            _paint_text_layer_stack(
                painter,
                layout.text_path,
                layout.line_rect,
                layout.colors.after,
                style,
                stroke_width=style.stroke_width_px,
                stroke2_width=stroke2_width,
                shadow_dx=style.shadow_offset_x,
                shadow_dy=style.shadow_offset_y,
                glow_radius=_glow_radius(style, after=True),
            )
        finally:
            painter.restore()

    # 注音：排在基字列右侧、上→下扫光
    if layout.active_rubies:
        ruby_font = _build_ruby_font(style)
        _paint_rubies_vertical(
            painter,
            ruby_font,
            QFontMetrics(ruby_font),
            line,
            layout.intervals,
            layout.cells,
            layout.column_x,
            layout.cell_w,
            t_ms,
            layout.active_rubies,
            style,
        )


def _vertical_after_clip_pad(style: Style) -> int:
    return _vertical_after_clip_pad_with_ports(
        style,
        ports=_VERTICAL_LAYER_PORTS,
    )


def _vertical_before_clip_pad(
    stroke_width: int,
    stroke2_width: int,
    before_glow_radius: int,
    shadow_dx: int,
    shadow_dy: int,
) -> int:
    return _vertical_before_clip_pad_with_ports(
        stroke_width,
        stroke2_width,
        before_glow_radius,
        shadow_dx,
        shadow_dy,
        raster=_VERTICAL_RASTER_PORTS,
    )


def _paint_line_vertical_layers(
    painter: QPainter,
    layout: _VerticalLineLayout,
    line: TimingLine,
    t_ms: int,
    style: Style,
) -> None:
    _paint_line_vertical_layers_with_ports(
        painter,
        layout,
        line,
        t_ms,
        style,
        compositor=_TEXT_RUN_COMPOSITOR,
        ports=_VERTICAL_LAYER_PORTS,
    )


def _paint_rubies_vertical(
    painter: QPainter,
    ruby_font: QFont,
    ruby_metrics: QFontMetrics,
    line: TimingLine,
    intervals: list[tuple[int, int]],
    cells: list[tuple[int, int]],
    base_column_x: int,
    cell_w: int,
    t_ms: int,
    rubies: list[RubyAnnotation],
    style: Style,
) -> None:
    _paint_rubies_vertical_with_ports(
        painter,
        ruby_font,
        ruby_metrics,
        line,
        intervals,
        cells,
        base_column_x,
        cell_w,
        t_ms,
        rubies,
        style,
        ports=_VERTICAL_RUBY_PORTS,
    )


def _vertical_fill_band(
    cells: list[tuple[int, int]],
    intervals: list[tuple[int, int]],
    t_ms: int,
    *,
    line: TimingLine | None = None,
    active_rubies: list[RubyAnnotation] | None = None,
    ruby_main_progress_mode: str = "checkpoint_segments",
) -> tuple[int, int] | None:
    return _vertical_fill_band_with_ports(
        cells,
        intervals,
        t_ms,
        ports=_VERTICAL_PROGRESS_PORTS,
        line=line,
        active_rubies=active_rubies,
        ruby_main_progress_mode=ruby_main_progress_mode,
    )


def _paint_line(
    painter: QPainter,
    img_w: int,
    img_h: int,
    track: TimingTrack,
    line: TimingLine,
    t_ms: int,
    style: Style,
    *,
    baseline_y: int | None = None,
    line_x: int | None = None,
    lane: int | None = None,
    display_start_ms: int | None = None,
    display_end_ms: int | None = None,
    layout_cache_sig: tuple | None = None,
    resolved_style: Style | None = None,
    line_plan: LineLayoutPlan | None = None,
) -> None:
    style = resolved_style or _style_for_line_display_window(
        style, line, display_start_ms, display_end_ms
    )
    animation = line_animation_state(
        style,
        t_ms=t_ms,
        display_start_ms=display_start_ms if display_start_ms is not None else _line_start_ms(line),
        display_end_ms=display_end_ms if display_end_ms is not None else _line_end_ms(line),
        lane=lane,
    )
    if animation.opacity <= 0.0:
        return

    def draw(target: QPainter) -> None:
        target.save()
        try:
            if animation.dx or animation.dy:
                target.translate(animation.dx, animation.dy)
            _paint_line_static(
                target,
                img_w,
                img_h,
                track,
                line,
                t_ms,
                style,
                baseline_y=baseline_y,
                line_x=line_x,
                lane=lane,
                display_start_ms=display_start_ms,
                display_end_ms=display_end_ms,
                layout_cache_sig=layout_cache_sig,
                line_plan=line_plan,
            )
        finally:
            target.restore()

    if animation.opacity < 1.0:
        _paint_through_opacity_layer(painter, animation.opacity, draw)
        return
    draw(painter)


def _paint_through_opacity_layer(
    painter: QPainter,
    opacity: float,
    draw,
) -> None:
    """Compose ``draw`` at full opacity, then blend the result once.

    Mirrors N3's ``LineFade`` → ``PushOpacityLayer`` (Direct2D
    ``PushLayer(LayerParameters{Opacity})``).  Setting the opacity on the painter
    instead applies it to every single draw call, so a glyph body stops covering
    its own edge, glow and shadow: those lower layers show through the
    semi-transparent body and the line appears to change colour mid-animation.

    The scratch buffer matches the target's device resolution and carries the
    same world transform, so the rasterization is identical to drawing straight
    onto the target -- only the final alpha blend differs.
    """

    device = painter.device()
    physical_w = max(int(device.width()), 1)
    physical_h = max(int(device.height()), 1)
    buffer = _opacity_layer_buffer(physical_w, physical_h)
    if buffer is None:
        # Nothing sane to compose into; keep drawing rather than dropping the
        # line, accepting the legacy per-call blending for this frame.
        painter.save()
        try:
            painter.setOpacity(painter.opacity() * opacity)
            draw(painter)
        finally:
            painter.restore()
        return
    buffer.setDevicePixelRatio(_device_pixel_ratio(device))
    buffer.fill(0)
    inner = QPainter(buffer)
    try:
        inner.setRenderHints(painter.renderHints())
        inner.setTransform(painter.transform())
        draw(inner)
    finally:
        inner.end()
    painter.save()
    try:
        painter.setOpacity(painter.opacity() * opacity)
        painter.resetTransform()
        painter.drawImage(QPointF(0.0, 0.0), buffer)
    finally:
        painter.restore()


def _device_pixel_ratio(device) -> float:
    getter = getattr(device, "devicePixelRatioF", None)
    if getter is None:
        getter = getattr(device, "devicePixelRatio", None)
    try:
        return max(float(getter()), 0.01) if getter is not None else 1.0
    except (TypeError, ValueError):
        return 1.0


def _opacity_layer_buffer(physical_w: int, physical_h: int) -> QImage | None:
    """Return a reusable scratch image, avoiding a per-frame allocation.

    Entry/exit animations run for a few hundred milliseconds at a time, so one
    cached buffer per size per thread keeps the steady state allocation-free.
    """

    cache = getattr(_OPACITY_LAYER_LOCAL, "buffers", None)
    if cache is None:
        cache = {}
        _OPACITY_LAYER_LOCAL.buffers = cache
    key = (physical_w, physical_h)
    buffer = cache.get(key)
    if buffer is None:
        buffer = QImage(
            physical_w, physical_h, QImage.Format.Format_ARGB32_Premultiplied
        )
        if buffer.isNull():
            return None
        if len(cache) >= 4:
            cache.clear()
        cache[key] = buffer
    return buffer


def _paint_line_static(
    painter: QPainter,
    img_w: int,
    img_h: int,
    track: TimingTrack,
    line: TimingLine,
    t_ms: int,
    style: Style,
    *,
    baseline_y: int | None = None,
    line_x: int | None = None,
    lane: int | None = None,
    display_start_ms: int | None = None,
    display_end_ms: int | None = None,
    layout_cache_sig: tuple | None = None,
    line_plan: LineLayoutPlan | None = None,
) -> None:
    if style.vertical:
        _paint_line_vertical(
            painter,
            img_w,
            img_h,
            track,
            line,
            t_ms,
            style,
            column_x=baseline_y,
            lane=lane,
            line_plan=line_plan,
        )
        return
    # layout 段（纯几何，不依赖 t_ms）：算字符几何 / 基线 / fill_segments。
    layout = _layout_line(
        track, line, style, img_w, img_h,
        baseline_y=baseline_y, line_x=line_x, lane=lane,
        cache_sig=layout_cache_sig,
        line_plan=line_plan,
    )
    if layout is None:
        return
    render_line = layout.render_line or line
    # animation 段（依赖 t_ms）：逐字入退场上下文。
    transition = _line_char_transition_context(
        style, render_line, t_ms, display_start_ms, display_end_ms, len(render_line.chars),
        intervals=layout.intervals,
    )
    def paint_ruby_glow_under_main() -> None:
        if (
            transition is not None
            or not layout.active_rubies
            or layout.ruby_metrics is None
        ):
            return
        _paint_ruby_glow_layers(
            painter,
            list(layout.ruby_layouts),
            layout.ruby_font,
            layout.ruby_metrics,
            t_ms,
            style,
            layout.rtl,
        )

    def paint_rubies_on_top() -> None:
        if not layout.active_rubies or layout.ruby_metrics is None:
            return
        # N3 renders main text decoration first, then ruby on top.  Painting
        # ruby before the main glyphs lets a large main glow bleed over the
        # reading stroke/fill, which makes ruby look submerged.
        _paint_rubies(
            painter, layout.ruby_font, layout.ruby_metrics, render_line,
            layout.intervals, layout.char_x_ranges, layout.baseline_y,
            t_ms, layout.active_rubies, style, transition,
            main_ascent_px=layout.text_layout.ascent if layout.has_inline_styles else None,
            text_layout=layout.text_layout,
            draw_glow=transition is not None,
            precomputed_layouts=layout.ruby_layouts,
        )

    if transition is not None:
        if transition.effect in ("char_fade", "char_drip", "spin_flip"):
            # A1/A2（§9.7）：逐字入退场 → 走 LayerCompositor 烘焙缓存，不再每帧
            # _paint_char_karaoke_stack 重栅（含 glow 复用）。普通行/分色行同路。
            # char_fade 仅 opacity（无损）；char_drip / spin_flip 加 N3 对应的
            # skew（spin_flip 还带 scale）残差（D2 软化可接受）。
            _TEXT_RUN_COMPOSITOR.paint_ordered(
                painter,
                LayerContext(t_ms=t_ms, logical_w=0, logical_h=0),
                _char_transition_layer_stack(layout, t_ms, transition, max(len(render_line.chars), 1)),
            )
            paint_rubies_on_top()
            return
        if layout.has_inline_styles:
            _paint_role_line_with_character_transition(
                painter, render_line, layout.text_layout, layout.char_x_ranges, layout.intervals,
                layout.active_rubies, layout.baseline_y, t_ms, transition, style,
                rtl=layout.rtl, ink_x_ranges=layout.ink_x_ranges,
                fill_segments=layout.fill_segments,
            )
        else:
            _paint_line_with_character_transition(
                painter, render_line, layout.char_widths, layout.char_x_ranges, layout.intervals,
                layout.active_rubies, layout.font, layout.baseline_y, layout.metrics,
                style, layout.colors, layout.line_rect, t_ms, transition,
                rtl=layout.rtl, font_for=layout.font_for, ink_x_ranges=layout.ink_x_ranges,
                glyphs_by_index=_role_glyphs_by_index(render_line, layout.text_layout),
                fill_rect=_n3_main_fill_rect(
                    layout.text_layout, layout.baseline_y
                ),
                fill_segments=layout.fill_segments,
            )
        paint_rubies_on_top()
        return

    # paint 段：消费 layout。默认 blit 未唱层 + 已唱层；测试/调试可回退同 layout 直绘。
    paint_ruby_glow_under_main()
    if _horizontal_layer_enabled():
        _paint_line_layers(painter, layout, t_ms)
    else:
        _paint_line_direct(painter, layout, t_ms)
    paint_rubies_on_top()


def _track_line_index(track: TimingTrack, line: TimingLine) -> int:
    """行在轨道中的下标；找不到返回 ``-1``。

    每次行布局查缓存都要它，而线性扫描是 O(行数)——在长曲目上又是一处
    O(行数²)。排版区间内行表不会变，整轨建一次索引即可。
    """
    cache = getattr(_LAYOUT_PASS, "line_indices", None)
    if cache is None:
        for index, item in enumerate(track.lines):
            if item is line:
                return index
        return -1
    index_map = cache.get(id(track))
    if index_map is None:
        index_map = {id(item): index for index, item in enumerate(track.lines)}
        cache[id(track)] = index_map
        # 键里有 id()：存住 track，避免回收后地址被复用。
        _LAYOUT_PASS.tracks.append(track)
    return index_map.get(id(line), -1)


def _layout_line(
    track: TimingTrack,
    line: TimingLine,
    style: Style,
    img_w: int,
    img_h: int,
    *,
    baseline_y: int | None = None,
    line_x: int | None = None,
    lane: int | None = None,
    cache_sig: tuple | None = None,
    line_plan: LineLayoutPlan | None = None,
) -> _LineLayout | None:
    if cache_sig is None:
        return _layout_line_uncached(
            track, line, style, img_w, img_h,
            baseline_y=baseline_y, line_x=line_x, lane=lane,
            line_plan=line_plan,
        )
    line_index = _track_line_index(track, line)
    if line_index < 0:
        return _layout_line_uncached(
            track, line, style, img_w, img_h,
            baseline_y=baseline_y, line_x=line_x, lane=lane,
            line_plan=line_plan,
        )
    key = (
        cache_sig,
        line_index,
        _line_layout_signature(line),
        img_w,
        img_h,
        baseline_y,
        line_x,
        lane,
    )
    return _LINE_LAYOUT_CACHE.get_or_build(
        key,
        lambda: _layout_line_uncached(
            track, line, style, img_w, img_h,
            baseline_y=baseline_y, line_x=line_x, lane=lane,
            line_plan=line_plan,
        ),
    )


def _layout_line_uncached(
    track: TimingTrack,
    line: TimingLine,
    style: Style,
    img_w: int,
    img_h: int,
    *,
    baseline_y: int | None = None,
    line_x: int | None = None,
    lane: int | None = None,
    line_plan: LineLayoutPlan | None = None,
) -> _LineLayout | None:
    render_line = (
        line_plan.render_line if line_plan is not None else _line_with_guide_symbol(line)
    )
    resolved_intervals = (
        line_plan.resolved_intervals if line_plan is not None else None
    )
    center_override = line_plan.center_override if line_plan is not None else None
    if _line_has_role_labels(render_line):
        return _layout_role_line(
            track, render_line, style, img_w, img_h,
            baseline_y=baseline_y, line_x=line_x, lane=lane,
            source_line=line,
            resolved_intervals=resolved_intervals,
            center_override=center_override,
        )
    return _layout_plain_line(
        track, render_line, style, img_w, img_h,
        baseline_y=baseline_y, line_x=line_x, lane=lane,
        source_line=line,
        resolved_intervals=resolved_intervals,
        center_override=center_override,
    )


def _layout_plain_line(
    track: TimingTrack,
    line: TimingLine,
    style: Style,
    img_w: int,
    img_h: int,
    *,
    baseline_y: int | None = None,
    line_x: int | None = None,
    lane: int | None = None,
    source_line: TimingLine | None = None,
    resolved_intervals: tuple[tuple[int, int], ...] | None = None,
    center_override: bool | None = None,
) -> _LineLayout:
    """layout 段：算普通行的纯几何 + 字体资源（不依赖 t_ms，可缓存）。"""
    font = _build_font(style)
    metrics = QFontMetrics(font)
    latin_font = _build_latin_font(style)
    font_for = _make_font_for(style, font, latin_font)
    latin_metrics = QFontMetrics(latin_font) if font_for is not None else metrics
    source_line = source_line or line
    active_rubies = _active_rubies_for_line(track.rubies, source_line)
    ruby_font = _build_ruby_font(style)
    ruby_metrics = QFontMetrics(ruby_font) if active_rubies else None

    # 整行宽度 → 水平居中起点（英数字符用英数字体的步进）。
    # 演唱计时用原始字宽；ruby 避让间隙只改几何。
    char_widths = [
        (
            _vector_glyph_width(c.vector_glyph, style)
            if c.vector_glyph is not None
            else _char_layout_width(c.text, font, metrics, latin_metrics, font_for, style)
        )
        for c in line.chars
    ]
    intervals = (
        list(resolved_intervals)
        if resolved_intervals is not None
        else compute_char_intervals(line, char_widths)
    )
    char_gaps, ruby_left_ext, ruby_right_ext = _ruby_char_gaps(
        line, char_widths, active_rubies, style, intervals
    )
    total_w = _line_text_width(char_widths, style) + sum(char_gaps)
    # N3 anchors the logical DrawLineLeft/Right.  Secondary stroke, glow and
    # shadow may extend outside the requested horizontal margin.
    left_ext = ruby_left_ext
    right_ext = ruby_right_ext
    if center_override is None:
        center_override = _line_center_override(track, source_line, style)
    n3_main_center = (
        style.layout_semantics == "n3_1074"
        and not center_override
        and style.line_horizontal_layout == "asymmetric"
        and _line_lane_alignment(track, source_line, style, lane) == "center"
    )
    x0 = (
        line_x
        if line_x is not None
        else _resolve_line_x_smart(
            img_w, total_w, track, source_line, style, lane,
            center_override=False,
        )
        if n3_main_center
        else _resolve_line_x_smart(
            img_w, total_w + left_ext + right_ext, track, source_line, style, lane,
            center_override=center_override,
        )
        + left_ext
    )
    y = (
        baseline_y
        if baseline_y is not None
        else _resolve_baseline_y(metrics, img_h, style, ruby_metrics)
    )
    rtl = style.right_to_left
    char_lefts = _char_left_positions(
        char_widths,
        x0,
        rtl,
        _letter_spacing(style),
        char_gaps=char_gaps,
        n3_no_backtracking=style.layout_semantics == "n3_1074",
    )
    char_x_ranges: list[tuple[int, int]] = [
        (left, left + w) for left, w in zip(char_lefts, char_widths)
    ]
    text_layout = _build_text_layout(
        line, style, x0=x0, baseline_y=y, inline_styles=False, char_gaps=char_gaps
    )
    ink_x_ranges = _role_char_ink_ranges_by_index(line, text_layout, char_x_ranges)
    wipe_x_ranges = _n3_char_wipe_ranges_by_index(
        line, text_layout, char_x_ranges, ink_x_ranges
    )
    fill_segments = _karaoke_fill_segments(
        char_widths, intervals, ink_x_ranges, active_rubies, line,
        release_x_ranges=wipe_x_ranges,
        layout_x_ranges=char_x_ranges,
        ruby_main_progress_mode=style.ruby_main_progress_mode,
    )
    line_rect = QRectF(
        float(x0), float(y - metrics.ascent()), float(total_w), float(metrics.height()),
    )
    colors = _effective_karaoke_colors(style)
    ruby_layouts = tuple(
        _layout_rubies(
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
        )
        if ruby_metrics is not None
        else ()
    )
    return _LineLayout(
        text_layout=text_layout,
        font=font, metrics=metrics, latin_font=latin_font, font_for=font_for,
        active_rubies=active_rubies, ruby_font=ruby_font, ruby_metrics=ruby_metrics,
        char_widths=char_widths, total_w=total_w, x0=x0, baseline_y=y,
        intervals=intervals, char_lefts=char_lefts, char_x_ranges=char_x_ranges,
        fill_segments=fill_segments, line_rect=line_rect, colors=colors, rtl=rtl,
        has_inline_styles=False, ink_x_ranges=ink_x_ranges,
        ruby_layouts=ruby_layouts,
        render_line=line,
    )


def _bitmap_guide_is_no_wipe(symbol: object | None) -> bool:
    return _guide_symbol_is_bitmap(symbol) and not bool(
        getattr(symbol, "bitmap_after_path", None)
    )


def _bitmap_guide_anchor_descent(glyph: _GlyphLayout) -> int:
    style = glyph.style
    if style.layout_semantics == "n3_1074":
        return _fixed_line_geometry(style)[2]
    return max(int(glyph.metrics.descent()), 0)


def _glyph_path(glyph: _GlyphLayout, baseline_y: int) -> QPainterPath:
    if glyph.vector_glyph is not None:
        if _guide_symbol_is_bitmap(glyph.vector_glyph):
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
    return path


def _role_visual_text_padding(layout: _TextLayout) -> int:
    if not layout.glyphs:
        return 0
    return max(_visual_text_padding(glyph.style) for glyph in layout.glyphs)


def _resolve_role_baseline_y(
    layout: _TextLayout,
    img_h: int,
    style: Style,
    ruby_extra: int = 0,
) -> int:
    pos = style.line_y_position
    margin = style.line_y_margin_px
    pad = _role_visual_text_padding(layout)
    ruby_extra = max(int(ruby_extra), 0)
    if pos == "top":
        return margin + ruby_extra + pad + layout.ascent
    if pos == "center":
        block_h = layout.height + ruby_extra + pad * 2
        return (img_h - block_h) // 2 + ruby_extra + pad + layout.ascent
    return img_h - margin - pad - layout.descent


def _clamp_role_baseline_y(
    baseline_y: int,
    layout: _TextLayout,
    img_h: int,
    style: Style,
    ruby_extra: int = 0,
) -> int:
    pad = _role_visual_text_padding(layout)
    ruby_extra = max(int(ruby_extra), 0)
    min_y = ruby_extra + pad + layout.ascent
    max_y = img_h - pad - layout.descent
    if max_y < min_y:
        return min_y
    return max(min_y, min(max_y, baseline_y))


def _glyph_run_signature(glyph: _GlyphLayout) -> tuple:
    colors = _effective_karaoke_colors(glyph.style)
    return (
        _karaoke_state_signature(colors.before),
        _karaoke_state_signature(colors.after),
        glyph.style.shadow_offset_x,
        glyph.style.shadow_offset_y,
        glyph.style.stroke_width_px,
        glyph.style.stroke2_width_px,
        glyph.style.decoration_kind,
        _glow_radius(glyph.style, after=False),
        _glow_radius(glyph.style, after=True),
        _glow_concentration_level(glyph.style),
    )


def _glyph_runs(layout: _TextLayout) -> list[list[_GlyphLayout]]:
    runs: list[list[_GlyphLayout]] = []
    current: list[_GlyphLayout] = []
    current_signature: tuple | None = None
    signature_cache: dict[int, tuple] = {}
    for glyph in layout.glyphs:
        style_id = id(glyph.style)
        signature = signature_cache.get(style_id)
        if signature is None:
            signature = _glyph_run_signature(glyph)
            signature_cache[style_id] = signature
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


def _glyph_is_bitmap_guide(glyph: _GlyphLayout) -> bool:
    return _guide_symbol_is_bitmap(glyph.vector_glyph)


def _text_glyph_runs(
    layout: _TextLayout, has_inline_styles: bool
) -> list[list[_GlyphLayout]]:
    runs = [layout.glyphs] if not has_inline_styles else _glyph_runs(layout)
    result: list[list[_GlyphLayout]] = []
    for run in runs:
        text_run = [glyph for glyph in run if not _glyph_is_bitmap_guide(glyph)]
        if text_run:
            result.append(text_run)
    return result


def _bitmap_guide_glyphs(layout: _TextLayout) -> list[_GlyphLayout]:
    return [glyph for glyph in layout.glyphs if _glyph_is_bitmap_guide(glyph)]


def _glyph_run_path(glyphs: list[_GlyphLayout], baseline_y: int) -> QPainterPath:
    path = QPainterPath()
    for glyph in glyphs:
        path.addPath(_glyph_path(glyph, baseline_y))
    return path


def _glyph_run_rect(glyphs: list[_GlyphLayout], baseline_y: int) -> QRectF:
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


def _n3_main_fill_rect(layout: _TextLayout, baseline_y: int) -> QRectF:
    """Return N3's shared vertical brush area for one main-text line.

    ``DrawLineInfo.DrawTop/DrawBottom`` use the tallest ``FontSize + EdgeSize``
    character box.  ``SetMultiColorAreas`` then moves both gradient endpoints
    inward by half of ``EdgeSize + EdgeSize2`` from the *first* character's
    font slot.  All divisions in N3 are integer divisions.  The resulting
    rectangle is shared by every character and visual layer in the line; it is
    not the individual glyph ink/advance box.
    """
    glyphs = layout.glyphs
    if not glyphs:
        return QRectF(layout.line_rect)

    first = glyphs[0]
    font_size = max(int(first.font.pixelSize()), 1)
    metric_total = max(first.metrics.ascent() + first.metrics.descent(), 1)
    descent = font_size * max(first.metrics.descent(), 0) // metric_total
    brush_style = first.brush_style or first.style
    draw_edge = max(int(first.style.stroke_width_px), 0)
    anchor_edge = max(int(brush_style.stroke_width_px), 0)
    anchor_edge2 = _main_stroke2_width(brush_style)
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
        float(max(layout.line_rect.width(), 1.0)),
        float(max(bottom - top, 1.0)),
    )


def _layout_role_line(
    track: TimingTrack,
    line: TimingLine,
    style: Style,
    img_w: int,
    img_h: int,
    *,
    baseline_y: int | None = None,
    line_x: int | None = None,
    lane: int | None = None,
    source_line: TimingLine | None = None,
    resolved_intervals: tuple[tuple[int, int], ...] | None = None,
    center_override: bool | None = None,
) -> _LineLayout | None:
    """layout 段：算分色行的纯几何（逐段多字体）+ 基线 + fill_segments（不依赖 t_ms）。"""
    has_shared_baseline = baseline_y is not None
    source_line = source_line or line
    active_rubies = _active_rubies_for_line(track.rubies, source_line)
    ruby_font = _build_ruby_font(style)
    ruby_metrics = QFontMetrics(ruby_font) if active_rubies else None
    measure_layout = _build_role_text_layout(line, style, x0=0, baseline_y=0)
    if not measure_layout.glyphs:
        return None
    char_widths, _measure_ranges = _role_char_geometry_by_index(line, measure_layout)
    intervals = (
        list(resolved_intervals)
        if resolved_intervals is not None
        else compute_char_intervals(line, char_widths)
    )
    ruby_extra = _role_ruby_vertical_extra(
        line, active_rubies, intervals, style
    )
    char_gaps, ruby_left_ext, ruby_right_ext = _ruby_char_gaps(
        line, char_widths, active_rubies, style, intervals
    )
    # 行盒左右不给描边留位（见 _line_total_width），只让 ruby 溢出撑开。
    left_ext = ruby_left_ext
    right_ext = ruby_right_ext
    total_w = measure_layout.total_width + sum(char_gaps)
    if center_override is None:
        center_override = _line_center_override(track, source_line, style)
    n3_main_center = (
        style.layout_semantics == "n3_1074"
        and not center_override
        and style.line_horizontal_layout == "asymmetric"
        and _line_lane_alignment(track, source_line, style, lane) == "center"
    )
    x0 = (
        line_x
        if line_x is not None
        else _resolve_line_x_smart(
            img_w, total_w, track, source_line, style, lane,
            center_override=False,
        )
        if n3_main_center
        else _resolve_line_x_smart(
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
        else _resolve_role_baseline_y(measure_layout, img_h, style, ruby_extra)
    )
    # A display lane owns its baseline.  Clamping that shared value against a
    # large inline role/guide glyph would move only this line and mutate the
    # configured inter-line gap.  Self-positioned diagnostic callers still get
    # the historical canvas clamp.
    if not has_shared_baseline:
        y = _clamp_role_baseline_y(y, measure_layout, img_h, style, ruby_extra)
    text_layout = _build_role_text_layout(
        line,
        style,
        x0=x0,
        baseline_y=y,
        char_gaps=char_gaps,
    )
    char_widths, char_x_ranges = _role_char_geometry_by_index(line, text_layout)
    intervals = (
        list(resolved_intervals)
        if resolved_intervals is not None
        else compute_char_intervals(line, char_widths)
    )
    ink_x_ranges = _role_char_ink_ranges_by_index(line, text_layout, char_x_ranges)
    wipe_x_ranges = _n3_char_wipe_ranges_by_index(
        line, text_layout, char_x_ranges, ink_x_ranges
    )
    fill_segments = _karaoke_fill_segments(
        char_widths, intervals, ink_x_ranges, active_rubies, line,
        release_x_ranges=wipe_x_ranges,
        layout_x_ranges=char_x_ranges,
        ruby_main_progress_mode=style.ruby_main_progress_mode,
    )
    ruby_layouts = tuple(
        _layout_rubies(
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
        )
        if ruby_metrics is not None
        else ()
    )
    return _LineLayout(
        text_layout=text_layout, active_rubies=active_rubies,
        font=text_layout.glyphs[0].font, metrics=text_layout.glyphs[0].metrics,
        latin_font=_build_latin_font(style), font_for=None,
        ruby_font=ruby_font, ruby_metrics=ruby_metrics,
        char_widths=char_widths, total_w=text_layout.total_width,
        x0=int(text_layout.line_rect.left()), baseline_y=y,
        intervals=intervals,
        char_lefts=[rng[0] for rng in char_x_ranges],
        char_x_ranges=char_x_ranges,
        fill_segments=fill_segments, line_rect=text_layout.line_rect,
        colors=_effective_karaoke_colors(style), rtl=style.right_to_left,
        has_inline_styles=True, ink_x_ranges=ink_x_ranges,
        ruby_layouts=ruby_layouts,
        render_line=line,
    )


def _paint_line_layers(
    painter: QPainter,
    layout: _LineLayout,
    t_ms: int,
) -> None:
    """paint 段：消费 :class:`_LineLayout`，逐 run blit 未唱层 + 已唱层。"""
    _TEXT_RUN_COMPOSITOR.paint_ordered(
        painter,
        LayerContext(t_ms=t_ms, logical_w=0, logical_h=0),
        _line_layer_stack(layout, t_ms),
    )


def _paint_line_direct(
    painter: QPainter,
    layout: _LineLayout,
    t_ms: int,
) -> None:
    """Vector oracle for horizontal static lines, sharing the baked path layout."""
    runs = _text_glyph_runs(layout.text_layout, layout.has_inline_styles)
    y = layout.baseline_y
    fill_rect = _n3_main_fill_rect(layout.text_layout, y)
    combined_glow_runs = [
        run for run in runs if _glyph_run_can_combine_split_glow(run)
    ]
    combined_run_ids = {id(run) for run in combined_glow_runs}

    # N3 builds both glow colours from outline sources clipped at WipeLeft,
    # blurs that combined decoration, and only then paints body/edges.
    for run in combined_glow_runs:
        _paint_glyph_run_combined_glow(
            painter,
            run,
            y,
            layout.fill_segments,
            t_ms,
            layout.rtl,
            fill_rect=fill_rect,
        )
    for run in runs:
        if id(run) in combined_run_ids:
            continue
        if not _glyph_run_needs_before_glow_split(run):
            continue
        before_band = _fill_clip_band_for_glyphs(
            layout.fill_segments, run, t_ms, layout.rtl
        )
        complete = _run_fill_complete(
            layout.fill_segments, {glyph.index for glyph in run}, t_ms
        )
        _paint_glyph_run_before_glow_direct(
            painter,
            run,
            y,
            before_band,
            rtl=layout.rtl,
            complete=complete,
            fill_rect=fill_rect,
        )

    for run in runs:
        if id(run) in combined_run_ids:
            continue
        for glyph in run:
            glyph_run = [glyph]
            glyph_band = _fill_clip_band_for_glyphs(
                layout.fill_segments, glyph_run, t_ms, layout.rtl
            )
            if glyph_band is None or glyph.text.isspace():
                continue
            glyph_complete = _run_fill_complete(
                layout.fill_segments, {glyph.index}, t_ms
            )
            following_band = _n3_following_wipe_band(
                layout.fill_segments, {glyph.index}, t_ms, layout.rtl
            )
            if following_band is not None:
                glyph_band = following_band
            glyph_released = glyph_complete and following_band is None
            if _glyph_run_needs_after_glow(glyph_run):
                _paint_glyph_run_after_glow_direct(
                    painter,
                    glyph_run,
                    y,
                    glyph_band,
                    rtl=layout.rtl,
                    complete=glyph_released,
                    fill_rect=fill_rect,
                )

    for run in runs:
        split_glow = _glyph_run_needs_before_glow_split(run)
        if not split_glow:
            _paint_glyph_run_direct(
                painter, run, y, after=False, fill_rect=fill_rect
            )
            continue
        before_band = _fill_clip_band_for_glyphs(
            layout.fill_segments, run, t_ms, layout.rtl
        )
        complete = _run_fill_complete(
            layout.fill_segments, {glyph.index for glyph in run}, t_ms
        )
        if complete:
            continue
        if before_band is None:
            _paint_glyph_run_direct(
                painter,
                run,
                y,
                after=False,
                fill_rect=fill_rect,
                draw_glow=not split_glow,
            )
            continue
        painter.save()
        try:
            painter.setClipRect(
                _horizontal_before_clip_rect(before_band, layout.rtl)
            )
            _paint_glyph_run_direct(
                painter,
                run,
                y,
                after=False,
                fill_rect=fill_rect,
                draw_glow=not split_glow,
            )
        finally:
            painter.restore()

    _paint_bitmap_guide_glyphs(painter, layout, t_ms, after=False)

    for run in runs:
        for glyph in run:
            glyph_run = [glyph]
            glyph_band = _fill_clip_band_for_glyphs(
                layout.fill_segments, glyph_run, t_ms, layout.rtl
            )
            if glyph_band is None or glyph.text.isspace():
                continue
            glyph_complete = _run_fill_complete(
                layout.fill_segments, {glyph.index}, t_ms
            )
            following_band = _n3_following_wipe_band(
                layout.fill_segments, {glyph.index}, t_ms, layout.rtl
            )
            if following_band is not None:
                glyph_band = following_band
            glyph_released = glyph_complete and following_band is None
            if glyph_released:
                _paint_glyph_run_direct(
                    painter, glyph_run, y, after=True, fill_rect=fill_rect
                )
                continue
            painter.save()
            try:
                painter.setClipRect(_horizontal_after_clip_rect(glyph_band, layout.rtl))
                _paint_glyph_run_direct(
                    painter, glyph_run, y, after=True, fill_rect=fill_rect
                )
            finally:
                painter.restore()

    _paint_bitmap_guide_glyphs(painter, layout, t_ms, after=True)


def _line_layer_stack(layout: _LineLayout, t_ms: int) -> list:
    runs = _text_glyph_runs(layout.text_layout, layout.has_inline_styles)
    y = layout.baseline_y
    fill_rect = _n3_main_fill_rect(layout.text_layout, y)
    combined_glow_runs = [
        run for run in runs if _glyph_run_can_combine_split_glow(run)
    ]
    combined_run_ids = {id(run) for run in combined_glow_runs}
    combined_glow_layers = [
        _GlyphRunSplitGlowLayer(
            run,
            y,
            layout.fill_segments,
            t_ms,
            layout.rtl,
            fill_rect=fill_rect,
        )
        for run in combined_glow_runs
    ]
    before_glow_layers = [
        _GlyphRunBeforeGlowLayer(
            run,
            y,
            layout.fill_segments,
            t_ms,
            layout.rtl,
            fill_rect=fill_rect,
        )
        for run in runs
        if id(run) not in combined_run_ids
        and _glyph_run_needs_before_glow_split(run)
    ]
    before_layers = [
        _GlyphRunLayer(
            run,
            y,
            layout.fill_segments,
            t_ms,
            layout.rtl,
            after=False,
            fill_rect=fill_rect,
        )
        for run in runs
    ]
    bitmap_before_layers = [
        _BitmapGuideLayer(
            glyph,
            y,
            layout.fill_segments,
            t_ms,
            layout.rtl,
            after=False,
            z_index=len(runs) * 2,
        )
        for glyph in _bitmap_guide_glyphs(layout.text_layout)
    ]
    after_glow_layers = []
    after_body_layers = []
    bitmap_after_layers = []
    z_index = len(runs) * 2 + len(bitmap_before_layers)
    for run in runs:
        combined_glow = id(run) in combined_run_ids
        for glyph in run:
            glyph_run = [glyph]
            after_band = _fill_clip_band_for_glyphs(
                layout.fill_segments, glyph_run, t_ms, layout.rtl
            )
            if after_band is None or glyph.text.isspace():
                continue
            if not combined_glow and _glyph_run_needs_after_glow(glyph_run):
                after_glow_layers.append(
                    _GlyphRunAfterGlowLayer(
                        glyph_run,
                        y,
                        layout.fill_segments,
                        t_ms,
                        layout.rtl,
                        clip_band=after_band,
                        z_index=z_index,
                        fill_rect=fill_rect,
                    )
                )
                z_index += 1
            after_body_layers.append(
                _GlyphRunLayer(
                    glyph_run,
                    y,
                    layout.fill_segments,
                    t_ms,
                    layout.rtl,
                    after=True,
                    clip_band=after_band,
                    z_index=z_index,
                    fill_rect=fill_rect,
                )
            )
            z_index += 1
    for glyph in _bitmap_guide_glyphs(layout.text_layout):
        bitmap_after_layers.append(
            _BitmapGuideLayer(
                glyph,
                y,
                layout.fill_segments,
                t_ms,
                layout.rtl,
                after=True,
                z_index=z_index,
            )
        )
        z_index += 1
    # N3 composites the blurred decoration below every body/edge layer.
    return (
        combined_glow_layers
        + before_glow_layers
        + after_glow_layers
        + before_layers
        + bitmap_before_layers
        + after_body_layers
        + bitmap_after_layers
    )


def _horizontal_after_clip_rect(band: tuple[int, int], rtl: bool) -> QRectF:
    band_left, band_right = band
    if rtl:
        return QRectF(float(band_left), -1_000_000.0, 1_000_000.0, 2_000_000.0)
    return QRectF(-1_000_000.0, -1_000_000.0, float(band_right) + 1_000_000.0, 2_000_000.0)


def _horizontal_before_clip_rect(band: tuple[int, int], rtl: bool) -> QRectF:
    """Keep the before layer only on the unsung side of the wipe front."""
    band_left, band_right = band
    if rtl:
        return QRectF(
            -1_000_000.0,
            -1_000_000.0,
            float(band_left) + 1_000_000.0,
            2_000_000.0,
        )
    return QRectF(
        float(band_right),
        -1_000_000.0,
        1_000_000.0,
        2_000_000.0,
    )


def _bitmap_guide_target_rect(glyph: _GlyphLayout, baseline_y: int) -> QRectF | None:
    symbol = glyph.vector_glyph
    if not _guide_symbol_is_bitmap(symbol):
        return None
    width, height = _bitmap_guide_content_size(symbol, glyph.style)
    left = float(glyph.left + int(symbol.bitmap_margin_left_px))
    bottom = (
        baseline_y
        + _bitmap_guide_anchor_descent(glyph)
        - int(symbol.bitmap_margin_bottom_px)
    )
    top = float(bottom - height)
    return QRectF(left, top, float(max(width, 1)), float(max(height, 1)))


def _paint_bitmap_guide_glyph(
    painter: QPainter,
    glyph: _GlyphLayout,
    baseline_y: int,
    *,
    after: bool,
    band: tuple[int, int] | None,
    rtl: bool,
) -> None:
    symbol = glyph.vector_glyph
    if not _guide_symbol_is_bitmap(symbol):
        return
    image_path = (
        symbol.bitmap_after_path
        if after and symbol.bitmap_after_path
        else symbol.bitmap_before_path
    )
    image = _bitmap_guide_image(image_path)
    rect = _bitmap_guide_target_rect(glyph, baseline_y)
    if image is None or rect is None or image.isNull():
        return
    painter.save()
    try:
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        if after and band is not None:
            painter.setClipRect(_horizontal_after_clip_rect(band, rtl))
        painter.drawImage(rect, image)
    finally:
        painter.restore()


def _bitmap_guide_band_for_segments(
    fill_segments: list[_FillSegment],
    glyph: _GlyphLayout,
    t_ms: int,
    rtl: bool,
) -> tuple[int, int] | None:
    band = _fill_clip_band_for_glyphs(
        fill_segments, [glyph], t_ms, rtl
    )
    if band is None:
        band = _fill_clip_band(fill_segments, t_ms, rtl)
    following_band = _n3_following_wipe_band(
        fill_segments, {glyph.index}, t_ms, rtl
    )
    if following_band is not None:
        return following_band
    return band


def _bitmap_guide_band_for_glyph(
    layout: _LineLayout,
    glyph: _GlyphLayout,
    t_ms: int,
) -> tuple[int, int] | None:
    return _bitmap_guide_band_for_segments(
        layout.fill_segments, glyph, t_ms, layout.rtl
    )


def _paint_bitmap_guide_glyphs(
    painter: QPainter,
    layout: _LineLayout,
    t_ms: int,
    *,
    after: bool,
) -> None:
    for glyph in _bitmap_guide_glyphs(layout.text_layout):
        band = _bitmap_guide_band_for_glyph(layout, glyph, t_ms)
        if after and band is None:
            continue
        _paint_bitmap_guide_glyph(
            painter,
            glyph,
            layout.baseline_y,
            after=after,
            band=band,
            rtl=layout.rtl,
        )


def _paint_bitmap_guide_transition_glyph(
    painter: QPainter,
    glyph: _GlyphLayout,
    fill_segments: list[_FillSegment],
    baseline_y: int,
    line: TimingLine,
    intervals: list[tuple[int, int]],
    index: int,
    t_ms: int,
    transition: _LineCharTransition,
    style: Style,
    *,
    rtl: bool,
) -> None:
    if not _glyph_is_bitmap_guide(glyph):
        return
    following_done_ms = (
        _utopia_following_done_time(line, intervals, index, style)
        if transition.effect == "utopia"
        else None
    )
    char_start_ms = intervals[index][0] if index < len(intervals) else glyph.index
    char_end_ms = intervals[index][1] if index < len(intervals) else char_start_ms
    opacity = _transition_char_state(
        style,
        transition,
        index,
        max(len(line.chars), 1),
        char_start_ms=char_start_ms,
        char_end_ms=char_end_ms,
        t_ms=t_ms,
        frame_height=painter.device().height(),
        following_done_ms=following_done_ms,
    )[0]
    if opacity <= 0.0:
        return
    band = _bitmap_guide_band_for_segments(fill_segments, glyph, t_ms, rtl)
    painter.save()
    try:
        painter.setOpacity(painter.opacity() * opacity)
        _paint_bitmap_guide_glyph(
            painter, glyph, baseline_y, after=False, band=band, rtl=rtl
        )
        if band is not None:
            _paint_bitmap_guide_glyph(
                painter, glyph, baseline_y, after=True, band=band, rtl=rtl
            )
    finally:
        painter.restore()


@dataclass(frozen=True)
class _BitmapGuideLayer:
    glyph: _GlyphLayout
    baseline_y: int
    fill_segments: list
    t_ms: int
    rtl: bool
    after: bool
    z_index: int = 0
    scope: str = SCOPE_LINE

    def active_window(self, ctx: LayerContext) -> list[tuple[int, int]]:
        return []

    def layout(self, ctx: LayerContext) -> "_BitmapGuideLayer":
        return self

    def static_key(self, ctx: LayerContext, layout: object) -> None:
        return None

    def bake(self, ctx: LayerContext, layout: object, key: Hashable) -> BakedLayer:
        return BakedLayer(image=QImage(), offset=QPointF())

    def animate(self, ctx: LayerContext, layout: object) -> LayerAnimation:
        return LayerAnimation()

    def paint_dynamic(self, painter: QPainter, ctx: LayerContext, layout: object) -> None:
        band = _bitmap_guide_band_for_segments(
            self.fill_segments, self.glyph, self.t_ms, self.rtl
        )
        if self.after and band is None:
            return
        _paint_bitmap_guide_glyph(
            painter,
            self.glyph,
            self.baseline_y,
            after=self.after,
            band=band,
            rtl=self.rtl,
        )

    def vertical_bounds(self, ctx: LayerContext, layout: object) -> tuple[int, int] | None:
        rect = _bitmap_guide_target_rect(self.glyph, self.baseline_y)
        if rect is None:
            return None
        return int(math.floor(rect.top())), int(math.ceil(rect.bottom()))


def _paint_glyph_run_direct(
    painter: QPainter,
    glyphs: list[_GlyphLayout],
    baseline_y: int,
    *,
    after: bool,
    fill_rect: QRectF | None = None,
    draw_glow: bool | None = None,
) -> None:
    role_style = glyphs[0].style
    colors = _effective_karaoke_colors(role_style)
    state = colors.after if after else colors.before
    path = _glyph_run_path(glyphs, baseline_y)
    rect = _glyph_run_rect(glyphs, baseline_y)
    if draw_glow is None:
        draw_glow = not (after and role_style.decoration_kind == "glow")
    _paint_text_layer_stack(
        painter,
        path,
        rect,
        state,
        role_style,
        stroke_width=role_style.stroke_width_px,
        stroke2_width=role_style.stroke2_width_px,
        shadow_dx=role_style.shadow_offset_x,
        shadow_dy=role_style.shadow_offset_y,
        glow_radius=_glow_radius(role_style, after=after),
        draw_glow=draw_glow,
        fill_rect=fill_rect,
    )


def _after_glow_loose_clip_rect(
    band: tuple[int, int],
    rect: QRectF,
    glow_pad: int,
    rtl: bool,
    complete: bool,
) -> QRectF:
    """已唱发光的宽松裁切矩形（理由见 ``_paint_char_karaoke_stack`` 内注释）。

    N3 的 WipeLeft 用字形轮廓加一半一重描边计算锋面，不包含二重描边或 glow/blur
    半径；N3 随后才对已裁剪的描边源做 blur。这里的 after-glow 是已经预先 blur 好的
    位图，所以走字中的前缘必须严格停在扫光线，避免把未唱侧的 glow 位图切进来形成
    粗亮竖边。``glow_pad`` 只用于尾缘、上下和唱完后的边缘释放。
    """
    band_left, band_right = band
    glow_pad_f = float(glow_pad)
    left = float(band_left) - (0.0 if rtl and not complete else glow_pad_f)
    right = float(band_right) + (glow_pad_f if rtl or complete else 0.0)
    return QRectF(
        left,
        rect.top() - glow_pad,
        right - left,
        rect.height() + glow_pad * 2,
    )


def _paint_glyph_run_after_glow_direct(
    painter: QPainter,
    glyphs: list[_GlyphLayout],
    baseline_y: int,
    band: tuple[int, int],
    *,
    rtl: bool,
    complete: bool,
    fill_rect: QRectF | None = None,
) -> None:
    role_style = glyphs[0].style
    colors = _effective_karaoke_colors(role_style)
    path = _glyph_run_path(glyphs, baseline_y)
    rect = _glyph_run_rect(glyphs, baseline_y)
    pad = _glow_extent(
        role_style.stroke_width_px,
        role_style.stroke2_width_px,
        _glow_radius(role_style, after=True),
    )
    _paint_glow_path(
        painter,
        path,
        colors.after.shadow,
        fill_rect if fill_rect is not None else rect,
        _glow_radius(role_style, after=True),
        role_style.stroke_width_px,
        role_style.stroke2_width_px,
        source_clip=_after_glow_source_clip_rect(band, rect, pad, rtl, complete),
        concentration_level=_glow_concentration_level(role_style),
    )


def _paint_glyph_run_before_glow_direct(
    painter: QPainter,
    glyphs: list[_GlyphLayout],
    baseline_y: int,
    band: tuple[int, int] | None,
    *,
    rtl: bool,
    complete: bool,
    fill_rect: QRectF | None = None,
) -> None:
    """Paint N3's before-glow by clipping the stroke source before blur."""
    if complete:
        return
    role_style = glyphs[0].style
    colors = _effective_karaoke_colors(role_style)
    path = _glyph_run_path(glyphs, baseline_y)
    rect = _glyph_run_rect(glyphs, baseline_y)
    pad = _glow_extent(
        role_style.stroke_width_px,
        role_style.stroke2_width_px,
        _glow_radius(role_style, after=False),
    )
    if band is not None and _glow_cache_enabled():
        front = float(band[0] if rtl else band[1])
        _paint_cached_run_glow_source_wipe(
            painter,
            path,
            rect,
            glyphs,
            baseline_y,
            role_style,
            colors,
            after=False,
            front=front,
            rtl=rtl,
            transform=None,
            fill_rect=fill_rect,
        )
        return
    _paint_glow_path(
        painter,
        path,
        colors.before.shadow,
        fill_rect if fill_rect is not None else rect,
        _glow_radius(role_style, after=False),
        role_style.stroke_width_px,
        role_style.stroke2_width_px,
        source_clip=(
            _before_glow_source_clip_rect(band, rect, pad, rtl)
            if band is not None
            else None
        ),
        concentration_level=_glow_concentration_level(role_style),
    )


def _paint_full_glow_source_wipe(
    painter: QPainter,
    path: QPainterPath,
    rect: QRectF,
    role_style: Style,
    colors: KaraokeColors,
    *,
    front: float,
    rtl: bool,
    fill_rect: QRectF | None,
) -> None:
    """Paint an entire source-clipped glow after geometry transforms."""
    before_radius = _glow_radius(role_style, after=False)
    after_radius = _glow_radius(role_style, after=True)
    stroke2_width = _main_stroke2_width(role_style)
    if before_radius > 0 and before_radius == after_radius:
        pad = _glow_extent(
            role_style.stroke_width_px, stroke2_width, before_radius
        )
        top = rect.top() - pad
        height = rect.height() + pad * 2
        if rtl:
            before_source_clip = QRectF(
                -1_000_000.0, top, front + 1_000_000.0, height
            )
            after_source_clip = QRectF(front, top, 1_000_000.0, height)
        else:
            before_source_clip = QRectF(front, top, 1_000_000.0, height)
            after_source_clip = QRectF(
                -1_000_000.0, top, front + 1_000_000.0, height
            )
        _paint_split_glow_path(
            painter,
            path,
            colors.before.shadow,
            colors.after.shadow,
            fill_rect if fill_rect is not None else rect,
            before_radius,
            role_style.stroke_width_px,
            stroke2_width,
            before_source_clip=before_source_clip,
            after_source_clip=after_source_clip,
            concentration_level=_glow_concentration_level(role_style),
        )
        return

    for after in (False, True):
        radius = _glow_radius(role_style, after=after)
        pad = _glow_extent(
            role_style.stroke_width_px, stroke2_width, radius
        )
        source_is_right = rtl == after
        source_clip = (
            QRectF(
                front,
                rect.top() - pad,
                1_000_000.0,
                rect.height() + pad * 2,
            )
            if source_is_right
            else QRectF(
                -1_000_000.0,
                rect.top() - pad,
                front + 1_000_000.0,
                rect.height() + pad * 2,
            )
        )
        state = colors.after if after else colors.before
        _paint_glow_path(
            painter,
            path,
            state.shadow,
            fill_rect if fill_rect is not None else rect,
            radius,
            role_style.stroke_width_px,
            stroke2_width,
            source_clip=source_clip,
            concentration_level=_glow_concentration_level(role_style),
        )


def _paint_cached_run_glow_source_wipe(
    painter: QPainter,
    path: QPainterPath,
    rect: QRectF,
    glyphs: list[_GlyphLayout],
    baseline_y: int,
    role_style: Style,
    colors: KaraokeColors,
    *,
    after: bool,
    front: float,
    rtl: bool,
    transform: QTransform | None,
    fill_rect: QRectF | None,
) -> None:
    """Blur only the moving front strip and reuse cached full glow elsewhere.

    Source-clipped blur differs from the full cached halo only within one
    Gaussian support radius of ``front``.  N3's source-before-blur result is
    therefore reproduced by rasterising that narrow strip dynamically and
    clipping the cached full halo to the unaffected far side.
    """
    radius = _glow_radius(role_style, after=after)
    if radius <= 0:
        return
    state = colors.after if after else colors.before
    stroke2_width = _main_stroke2_width(role_style)
    pad = _glow_extent(role_style.stroke_width_px, stroke2_width, radius)
    top = rect.top() - pad
    height = rect.height() + pad * 2
    source_is_right = rtl == after
    if source_is_right:
        source_clip = QRectF(front, top, 1_000_000.0, height)
        baked_clip = QRectF(
            front + pad,
            -1_000_000.0,
            1_000_000.0,
            2_000_000.0,
        )
    else:
        source_clip = QRectF(
            -1_000_000.0,
            top,
            front + 1_000_000.0,
            height,
        )
        baked_clip = QRectF(
            -1_000_000.0,
            -1_000_000.0,
            front - pad + 1_000_000.0,
            2_000_000.0,
        )
    strip_clip = QRectF(
        front - pad,
        -1_000_000.0,
        float(pad * 2),
        2_000_000.0,
    )
    _paint_glow_path(
        painter,
        path,
        state.shadow,
        fill_rect if fill_rect is not None else rect,
        radius,
        role_style.stroke_width_px,
        stroke2_width,
        source_clip=source_clip,
        concentration_level=_glow_concentration_level(role_style),
        target_clip=strip_clip,
    )
    painter.save()
    try:
        painter.setClipRect(baked_clip)
        _blit_cached_run_glow(
            painter,
            glyphs,
            baseline_y,
            role_style,
            colors,
            after=after,
            transform=transform,
            fill_rect=fill_rect,
        )
    finally:
        painter.restore()


def _paint_cached_run_split_glow_source_wipe(
    painter: QPainter,
    path: QPainterPath,
    rect: QRectF,
    glyphs: list[_GlyphLayout],
    baseline_y: int,
    role_style: Style,
    colors: KaraokeColors,
    *,
    front: float,
    rtl: bool,
    transform: QTransform | None,
    fill_rect: QRectF | None,
) -> bool:
    """N3 fast path: one dynamic blur for both colours plus cached far halos."""
    before_radius = _glow_radius(role_style, after=False)
    after_radius = _glow_radius(role_style, after=True)
    if before_radius <= 0 or before_radius != after_radius:
        return False
    stroke2_width = _main_stroke2_width(role_style)
    pad = _glow_extent(
        role_style.stroke_width_px, stroke2_width, before_radius
    )
    top = rect.top() - pad
    height = rect.height() + pad * 2
    if rtl:
        before_source_clip = QRectF(
            -1_000_000.0, top, front + 1_000_000.0, height
        )
        after_source_clip = QRectF(front, top, 1_000_000.0, height)
        before_baked_clip = QRectF(
            -1_000_000.0,
            -1_000_000.0,
            front - pad + 1_000_000.0,
            2_000_000.0,
        )
        after_baked_clip = QRectF(
            front + pad,
            -1_000_000.0,
            1_000_000.0,
            2_000_000.0,
        )
    else:
        before_source_clip = QRectF(front, top, 1_000_000.0, height)
        after_source_clip = QRectF(
            -1_000_000.0, top, front + 1_000_000.0, height
        )
        before_baked_clip = QRectF(
            front + pad,
            -1_000_000.0,
            1_000_000.0,
            2_000_000.0,
        )
        after_baked_clip = QRectF(
            -1_000_000.0,
            -1_000_000.0,
            front - pad + 1_000_000.0,
            2_000_000.0,
        )
    strip_clip = QRectF(
        front - pad,
        -1_000_000.0,
        float(pad * 2),
        2_000_000.0,
    )
    _paint_split_glow_path(
        painter,
        path,
        colors.before.shadow,
        colors.after.shadow,
        fill_rect if fill_rect is not None else rect,
        before_radius,
        role_style.stroke_width_px,
        stroke2_width,
        before_source_clip=before_source_clip,
        after_source_clip=after_source_clip,
        concentration_level=_glow_concentration_level(role_style),
        target_clip=strip_clip,
    )
    for after, clip in (
        (False, before_baked_clip),
        (True, after_baked_clip),
    ):
        painter.save()
        try:
            painter.setClipRect(clip)
            _blit_cached_run_glow(
                painter,
                glyphs,
                baseline_y,
                role_style,
                colors,
                after=after,
                transform=transform,
                fill_rect=fill_rect,
            )
        finally:
            painter.restore()
    return True


def _glyph_run_can_combine_split_glow(glyphs: list[_GlyphLayout]) -> bool:
    if not _glyph_run_needs_before_glow_split(glyphs):
        return False
    style = glyphs[0].style
    before_radius = _glow_radius(style, after=False)
    return before_radius > 0 and before_radius == _glow_radius(style, after=True)


def _paint_glyph_run_combined_glow(
    painter: QPainter,
    glyphs: list[_GlyphLayout],
    baseline_y: int,
    fill_segments: list[_FillSegment],
    t_ms: int,
    rtl: bool,
    *,
    fill_rect: QRectF | None,
) -> None:
    """Paint one static run's N3 before/after decoration with one blur."""
    style = glyphs[0].style
    colors = _effective_karaoke_colors(style)
    indices = {glyph.index for glyph in glyphs}
    band = _fill_clip_band_for_glyphs(fill_segments, glyphs, t_ms, rtl)
    complete = _run_fill_complete(fill_segments, indices, t_ms)
    if band is None:
        _blit_cached_run_glow(
            painter,
            glyphs,
            baseline_y,
            style,
            colors,
            after=False,
            transform=None,
            fill_rect=fill_rect,
        )
        return
    if complete:
        _blit_cached_run_glow(
            painter,
            glyphs,
            baseline_y,
            style,
            colors,
            after=True,
            transform=None,
            fill_rect=fill_rect,
        )
        return
    front = float(band[0] if rtl else band[1])
    path = _glyph_run_path(glyphs, baseline_y)
    rect = _glyph_run_rect(glyphs, baseline_y)
    _paint_cached_run_split_glow_source_wipe(
        painter,
        path,
        rect,
        glyphs,
        baseline_y,
        style,
        colors,
        front=front,
        rtl=rtl,
        transform=None,
        fill_rect=fill_rect,
    )


def _afterglow_strip_enabled() -> bool:
    """走字中 after-glow 只逐帧模糊扫光前沿窄带（默认开）。

    ``KROK_SUBTITLE_AFTERGLOW_STRIP=0`` 退回整行逐帧
    ``_paint_glyph_run_after_glow_direct``（A/B 像素 oracle / 紧急回退用）。
    """
    return os.environ.get("KROK_SUBTITLE_AFTERGLOW_STRIP", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


def _paint_glyph_run_after_glow_wipe(
    painter: QPainter,
    glyphs: list[_GlyphLayout],
    baseline_y: int,
    band: tuple[int, int],
    *,
    rtl: bool,
    complete: bool,
    fill_rect: QRectF | None = None,
) -> None:
    """走字中的已唱发光：前沿窄带逐帧模糊 + 其余贴整段烘焙位图。

    N3 语义要求「先按扫光线裁源、再模糊」让前沿保持柔和，因此该层无法整层烘焙。
    但 blur(裁切源) 与 blur(完整源) 只在扫光线 ±支撑半径（``_glow_extent``，≥3×radius）
    内不同：seam（前沿 - pad）之前两者逐像素一致 → 直接贴 ``_RUN_GLOW_CACHE`` 里
    整段 after-glow 烘焙；seam 之后仅对 2×pad 宽的窄带做逐帧 stroke+blur。模糊成本
    随画布面积线性，长行收益一个数量级。"""
    role_style = glyphs[0].style
    if complete or not _afterglow_strip_enabled() or not _glow_cache_enabled():
        _paint_glyph_run_after_glow_direct(
            painter,
            glyphs,
            baseline_y,
            band,
            rtl=rtl,
            complete=complete,
            fill_rect=fill_rect,
        )
        return
    colors = _effective_karaoke_colors(role_style)
    path = _glyph_run_path(glyphs, baseline_y)
    rect = _glyph_run_rect(glyphs, baseline_y)
    radius = _glow_radius(role_style, after=True)
    pad = _glow_extent(role_style.stroke_width_px, role_style.stroke2_width_px, radius)
    band_left, band_right = band
    front = float(band_left) if rtl else float(band_right)
    # 前沿窄带 [front-pad, front+pad]；seam 在其已唱侧边缘。
    if rtl:
        seam = front + pad
        strip_clip = QRectF(front - pad, -1_000_000.0, 2.0 * pad, 2_000_000.0)
        baked_clip = QRectF(seam, -1_000_000.0, 1_000_000.0, 2_000_000.0)
    else:
        seam = front - pad
        strip_clip = QRectF(seam, -1_000_000.0, 2.0 * pad, 2_000_000.0)
        baked_clip = QRectF(-1_000_000.0, -1_000_000.0, seam + 1_000_000.0, 2_000_000.0)
    painter.save()
    try:
        painter.setClipRect(strip_clip)
        _paint_glow_path(
            painter,
            path,
            colors.after.shadow,
            fill_rect if fill_rect is not None else rect,
            radius,
            role_style.stroke_width_px,
            role_style.stroke2_width_px,
            source_clip=_after_glow_source_clip_rect(band, rect, pad, rtl, complete),
            concentration_level=_glow_concentration_level(role_style),
            target_clip=strip_clip,
        )
    finally:
        painter.restore()
    baked = _get_or_build_run_glow(
        glyphs,
        role_style,
        colors,
        after=True,
        fill_rect=fill_rect,
        baseline_y=baseline_y,
    )
    if baked.image.isNull():
        return
    run_left = min(glyph.left for glyph in glyphs)
    anchor = QPointF(float(run_left) + baked.offset.x(), float(baseline_y) + baked.offset.y())
    painter.save()
    try:
        painter.setClipRect(baked_clip)
        painter.drawImage(anchor, baked.image)
    finally:
        painter.restore()


def _after_glow_source_clip_rect(
    band: tuple[int, int],
    rect: QRectF,
    glow_pad: int,
    rtl: bool,
    complete: bool,
) -> QRectF | None:
    """Source clip for N3-style glow wiping.

    N3 clips the edge source by ``WipeLeft`` and then blurs the resulting
    work bitmap.  Returning this as ``source_clip`` keeps the visible glow front
    soft; clipping the already-blurred bitmap would create a hard vertical edge.
    """
    if complete:
        return None
    band_left, band_right = band
    top = rect.top() - glow_pad
    height = rect.height() + glow_pad * 2
    if rtl:
        return QRectF(float(band_left), top, 1_000_000.0, height)
    return QRectF(-1_000_000.0, top, float(band_right) + 1_000_000.0, height)


def _before_glow_source_clip_rect(
    band: tuple[int, int],
    rect: QRectF,
    glow_pad: int,
    rtl: bool,
) -> QRectF:
    """Unsing side of N3's source geometry, before Gaussian blur."""
    band_left, band_right = band
    top = rect.top() - glow_pad
    height = rect.height() + glow_pad * 2
    if rtl:
        return QRectF(-1_000_000.0, top, float(band_left) + 1_000_000.0, height)
    return QRectF(float(band_right), top, 1_000_000.0, height)


def _spin_flip_char_transform(
    glyph: _GlyphLayout,
    baseline_y: int,
    transition: _LineCharTransition,
    opacity: float,
) -> QTransform | None:
    """A2：spin_flip 逐字的 scale(opacity)+skew 残差变换，绕字心枢轴。

    复用 ``_character_transform``（与旧 ``_apply_character_transform`` 同一构造、
    几何完全一致），把枢轴烘焙进矩阵；compositor 把它作为残差套在烘焙位图上
    （bitmap-transform，短窗口软化可接受，见 §9.7 D2）。返回恒等时给 ``None``。
    """
    direction = 1.0 if transition.phase == "exit" else -1.0
    skew_y = direction * _spin_flip_skew(opacity)
    center_x = glyph.left + glyph.width / 2
    center_y = baseline_y - glyph.metrics.ascent() + glyph.metrics.height() / 2
    transform = _character_transform(
        center_x=center_x,
        center_y=center_y,
        scale_x=opacity,
        scale_y=opacity,
        skew_y=skew_y,
    )
    return None if transform.isIdentity() else transform


def _char_drip_char_transform(
    glyph: _GlyphLayout,
    baseline_y: int,
    transition: _LineCharTransition,
    progress: float,
) -> QTransform | None:
    """N3 ``CharDrip``: shear around the glyph's right-bottom/right-top corner.

    N3 uses ``(drawWidth, 0)`` for intro and ``(drawWidth, -height)`` for
    outro in glyph-local coordinates.  Unlike ``CharFadeInFadeOut`` it uses
    the inherited opacity value only as transform progress; visible geometry
    itself remains opaque once progress is non-zero.
    """
    direction = 1.0 if transition.phase == "entry" else -1.0
    skew_y = direction * _spin_flip_skew(progress)
    pivot_x = glyph.left + glyph.width
    pivot_y = (
        baseline_y
        if transition.phase == "entry"
        else baseline_y - glyph.metrics.height()
    )
    transform = _character_transform(
        center_x=pivot_x,
        center_y=pivot_y,
        skew_y=skew_y,
    )
    return None if transform.isIdentity() else transform


def _char_transition_layer_stack(
    layout: _LineLayout,
    t_ms: int,
    transition: _LineCharTransition,
    char_count: int,
) -> list:
    """A1/A2（§9.7）：逐字入退场走 LayerCompositor。

    每个 glyph 复用静态路径的 ``_GlyphRunLayer`` / ``_GlyphRunAfterGlowLayer``
    烘焙缓存（直立烘焙一次、跨帧复用），逐帧只补该字的残差：
    - **char_fade**：仅淡入/淡出 opacity（无损）；
    - **char_drip**：按 N3 从字的右下/右上枢轴纵向剪切，几何保持不透明；
    - **spin_flip**：opacity + scale(opacity)+skew 残差变换（绕字心枢轴，
      bitmap-transform 软化可接受，§9.7 D2）。
    glow 也因此并入烘焙缓存、不再每帧重算高斯。与旧逐帧
    ``_paint_char_karaoke_stack`` 路径同口径：逐字独立栈、按 glyph 顺序交错绘制
    （后字覆盖前字），扫光带取整行 ``fill_segments``（与静态路径同一来源），
    同一字的 before/after/glow 三层套同一残差变换。
    适用于普通行与分色行（per-glyph ``style``/``metrics`` 已携带角色样式）。
    """
    y = layout.baseline_y
    rtl = layout.rtl
    fill_rect = _n3_main_fill_rect(layout.text_layout, y)
    is_spin = transition.effect == "spin_flip"
    is_drip = transition.effect == "char_drip"
    before_glow_layers: list = []
    after_glow_layers: list = []
    body_layers: list = []
    z = 0
    for glyph in layout.text_layout.glyphs:
        progress = _char_fade_opacity(transition, glyph.index, char_count, t_ms=t_ms)
        if progress <= 0.0:
            continue
        opacity = 1.0 if is_drip else progress
        if is_spin:
            transform = _spin_flip_char_transform(glyph, y, transition, progress)
        elif is_drip:
            transform = _char_drip_char_transform(glyph, y, transition, progress)
        else:
            transform = None
        run = [glyph]
        if _glyph_run_needs_before_glow_split(run):
            before_glow_layers.append(
                _GlyphRunBeforeGlowLayer(
                    run, y, layout.fill_segments, t_ms, rtl,
                    z_index=z, fade_opacity=opacity, transform=transform,
                    fill_rect=fill_rect,
                )
            )
        body_layers.append(
            _GlyphRunLayer(
                run, y, layout.fill_segments, t_ms, rtl,
                after=False, z_index=z, fade_opacity=opacity, transform=transform,
                fill_rect=fill_rect,
            )
        )
        z += 1
        after_band = _fill_clip_band_for_glyphs(layout.fill_segments, run, t_ms, rtl)
        if after_band is None:
            continue
        if _glyph_run_needs_after_glow(run):
            after_glow_layers.append(
                _GlyphRunAfterGlowLayer(
                    run, y, layout.fill_segments, t_ms, rtl,
                    clip_band=after_band, z_index=z, fade_opacity=opacity, transform=transform,
                    fill_rect=fill_rect,
                )
            )
            z += 1
        body_layers.append(
            _GlyphRunLayer(
                run, y, layout.fill_segments, t_ms, rtl,
                after=True, clip_band=after_band, z_index=z, fade_opacity=opacity, transform=transform,
                fill_rect=fill_rect,
            )
        )
        z += 1
    return before_glow_layers + after_glow_layers + body_layers


@dataclass(frozen=True)
class _GlyphRunLayer:
    """Layer wrapper for a horizontal text glyph run body."""

    glyphs: list[_GlyphLayout]
    baseline_y: int
    fill_segments: list[_FillSegment]
    t_ms: int
    rtl: bool
    after: bool
    clip_band: tuple[int, int] | None = None
    z_index: int = 0
    scope: str = SCOPE_LINE
    fade_opacity: float = 1.0
    transform: QTransform | None = None
    fill_rect: QRectF | None = None

    def active_window(self, ctx: LayerContext) -> list[tuple[int, int]]:
        return []

    def layout(self, ctx: LayerContext) -> "_GlyphRunLayer":
        return self

    def static_key(self, ctx: LayerContext, layout: object) -> tuple:
        role_style = self.glyphs[0].style
        colors = _effective_karaoke_colors(role_style)
        state = colors.after if self.after else colors.before
        return (
            _glyph_run_layer_key(self.glyphs, role_style, colors, after=self.after),
            _relative_fill_rect_signature(
                self.glyphs,
                self.baseline_y,
                self.fill_rect,
                global_anchor=_karaoke_state_uses_image(state),
            ),
        )

    def bake(self, ctx: LayerContext, layout: object, key: Hashable) -> BakedLayer:
        role_style = self.glyphs[0].style
        colors = _effective_karaoke_colors(role_style)
        image, dx, dy = _build_glyph_run_layer(
            self.glyphs,
            role_style,
            colors,
            after=self.after,
            fill_rect=self.fill_rect,
            baseline_y=self.baseline_y,
        )
        return BakedLayer(image=image, offset=QPointF(float(dx), float(dy)))

    def animate(self, ctx: LayerContext, layout: object) -> LayerAnimation:
        run_left = min(glyph.left for glyph in self.glyphs)
        clip_rect = None
        role_style = self.glyphs[0].style
        # N3 硬分割：前后发光不同（颜色或半径）时，未唱层（含发光）整体裁到
        # 扫光线未唱侧，已唱侧由已唱层 + 已唱发光层负责；相同时未唱层整行铺满、
        # 已唱发光层被跳过（结果等价）。
        if (
            not self.after
            and _glow_radius(role_style, after=False) > 0
            and _karaoke_glow_states_differ(
                role_style, _effective_karaoke_colors(role_style)
            )
        ):
            band = _fill_clip_band_for_glyphs(
                self.fill_segments, self.glyphs, self.t_ms, self.rtl
            )
            if band is not None:
                if _run_fill_complete(
                    self.fill_segments,
                    {glyph.index for glyph in self.glyphs},
                    self.t_ms,
                ):
                    return LayerAnimation(opacity=0.0)
                clip_rect = _horizontal_before_clip_rect(band, self.rtl)
        elif self.after:
            indices = {glyph.index for glyph in self.glyphs}
            following_band = _n3_following_wipe_band(
                self.fill_segments, indices, self.t_ms, self.rtl
            )
            band = following_band or self.clip_band or _fill_clip_band(
                self.fill_segments, self.t_ms, self.rtl
            )
            if band is None:
                return LayerAnimation(opacity=0.0)
            band_left, band_right = band
            if (
                _run_fill_complete(self.fill_segments, indices, self.t_ms)
                and following_band is None
            ):
                # 唱完后不裁切：带缘停在墨水边界，再裁会把行缘的描边/阴影硬截掉。
                clip_rect = None
            elif self.rtl:
                clip_rect = QRectF(float(band_left), -1_000_000.0, 1_000_000.0, 2_000_000.0)
            else:
                clip_rect = QRectF(-1_000_000.0, -1_000_000.0, float(band_right) + 1_000_000.0, 2_000_000.0)
        return LayerAnimation(
            top_left=QPointF(float(run_left), float(self.baseline_y)),
            clip_rect=clip_rect,
            opacity=self.fade_opacity,
            transform=self.transform,
        )

    def paint_dynamic(self, painter: QPainter, ctx: LayerContext, layout: object) -> None:
        return

    def vertical_bounds(self, ctx: LayerContext, layout: object) -> tuple[int, int] | None:
        rect = _glyph_run_rect(self.glyphs, self.baseline_y)
        pad = _text_visual_padding(self.glyphs[0].style, after=self.after)
        return int(math.floor(rect.top() - pad)), int(math.ceil(rect.bottom() + pad))


@dataclass(frozen=True)
class _GlyphRunBeforeGlowLayer:
    """N3 before-glow: split the outline source at WipeLeft, then blur it."""

    glyphs: list[_GlyphLayout]
    baseline_y: int
    fill_segments: list[_FillSegment]
    t_ms: int
    rtl: bool
    z_index: int = 0
    scope: str = SCOPE_LINE
    fade_opacity: float = 1.0
    transform: QTransform | None = None
    fill_rect: QRectF | None = None

    def active_window(self, ctx: LayerContext) -> list[tuple[int, int]]:
        return []

    def layout(self, ctx: LayerContext) -> "_GlyphRunBeforeGlowLayer":
        return self

    def _state(self) -> tuple[tuple[int, int] | None, bool]:
        indices = {glyph.index for glyph in self.glyphs}
        following_band = _n3_following_wipe_band(
            self.fill_segments, indices, self.t_ms, self.rtl
        )
        band = following_band or _fill_clip_band_for_glyphs(
            self.fill_segments, self.glyphs, self.t_ms, self.rtl
        )
        complete = (
            _run_fill_complete(self.fill_segments, indices, self.t_ms)
            and following_band is None
        )
        return band, complete

    def static_key(self, ctx: LayerContext, layout: object) -> tuple | None:
        band, complete = self._state()
        if complete or band is not None:
            return None
        role_style = self.glyphs[0].style
        colors = _effective_karaoke_colors(role_style)
        return (
            _glyph_run_layer_key(self.glyphs, role_style, colors, after=False),
            "before-glow",
            _relative_fill_rect_signature(
                self.glyphs,
                self.baseline_y,
                self.fill_rect,
                global_anchor=colors.before.shadow.mode == "image",
            ),
        )

    def bake(self, ctx: LayerContext, layout: object, key: Hashable) -> BakedLayer:
        role_style = self.glyphs[0].style
        colors = _effective_karaoke_colors(role_style)
        image, dx, dy = _build_glyph_run_glow_layer(
            self.glyphs,
            role_style,
            colors,
            after=False,
            fill_rect=self.fill_rect,
            baseline_y=self.baseline_y,
        )
        return BakedLayer(image=image, offset=QPointF(float(dx), float(dy)))

    def animate(self, ctx: LayerContext, layout: object) -> LayerAnimation:
        run_left = min(glyph.left for glyph in self.glyphs)
        return LayerAnimation(
            top_left=QPointF(float(run_left), float(self.baseline_y)),
            opacity=self.fade_opacity,
            transform=self.transform,
        )

    def paint_dynamic(self, painter: QPainter, ctx: LayerContext, layout: object) -> None:
        band, complete = self._state()
        if complete:
            return
        painter.save()
        try:
            painter.setOpacity(painter.opacity() * self.fade_opacity)
            if self.transform is not None:
                painter.setTransform(self.transform, combine=True)
            _paint_glyph_run_before_glow_direct(
                painter,
                self.glyphs,
                self.baseline_y,
                band,
                rtl=self.rtl,
                complete=False,
                fill_rect=self.fill_rect,
            )
        finally:
            painter.restore()

    def vertical_bounds(self, ctx: LayerContext, layout: object) -> tuple[int, int] | None:
        rect = _glyph_run_rect(self.glyphs, self.baseline_y)
        role_style = self.glyphs[0].style
        pad = _glow_extent(
            role_style.stroke_width_px,
            role_style.stroke2_width_px,
            _glow_radius(role_style, after=False),
        )
        return int(math.floor(rect.top() - pad)), int(math.ceil(rect.bottom() + pad))


@dataclass(frozen=True)
class _GlyphRunSplitGlowLayer:
    """Combined before/after decoration for equal-radius N3 glow wipes."""

    glyphs: list[_GlyphLayout]
    baseline_y: int
    fill_segments: list[_FillSegment]
    t_ms: int
    rtl: bool
    z_index: int = 0
    scope: str = SCOPE_LINE
    fill_rect: QRectF | None = None

    def active_window(self, ctx: LayerContext) -> list[tuple[int, int]]:
        return []

    def layout(self, ctx: LayerContext) -> "_GlyphRunSplitGlowLayer":
        return self

    def static_key(self, ctx: LayerContext, layout: object) -> None:
        return None

    def bake(self, ctx: LayerContext, layout: object, key: Hashable) -> BakedLayer:
        raise AssertionError("combined split glow is painted dynamically")

    def animate(self, ctx: LayerContext, layout: object) -> LayerAnimation:
        return LayerAnimation()

    def paint_dynamic(self, painter: QPainter, ctx: LayerContext, layout: object) -> None:
        _paint_glyph_run_combined_glow(
            painter,
            self.glyphs,
            self.baseline_y,
            self.fill_segments,
            self.t_ms,
            self.rtl,
            fill_rect=self.fill_rect,
        )

    def vertical_bounds(self, ctx: LayerContext, layout: object) -> tuple[int, int] | None:
        rect = _glyph_run_rect(self.glyphs, self.baseline_y)
        style = self.glyphs[0].style
        pad = max(
            _glow_extent(
                style.stroke_width_px,
                style.stroke2_width_px,
                _glow_radius(style, after=after),
            )
            for after in (False, True)
        )
        return int(math.floor(rect.top() - pad)), int(math.ceil(rect.bottom() + pad))


@dataclass(frozen=True)
class _GlyphRunAfterGlowLayer:
    """Layer wrapper for the after-glow bitmap of a horizontal glyph run."""

    glyphs: list[_GlyphLayout]
    baseline_y: int
    fill_segments: list[_FillSegment]
    t_ms: int
    rtl: bool
    clip_band: tuple[int, int] | None = None
    z_index: int = 0
    scope: str = SCOPE_LINE
    fade_opacity: float = 1.0
    transform: QTransform | None = None
    fill_rect: QRectF | None = None

    def active_window(self, ctx: LayerContext) -> list[tuple[int, int]]:
        return []

    def layout(self, ctx: LayerContext) -> "_GlyphRunAfterGlowLayer":
        return self

    def static_key(self, ctx: LayerContext, layout: object) -> tuple | None:
        role_style = self.glyphs[0].style
        colors = _effective_karaoke_colors(role_style)
        if role_style.decoration_kind != "glow":
            return None
        before_radius = _glow_radius(role_style, after=False)
        after_radius = _glow_radius(role_style, after=True)
        if after_radius == 0:
            return None
        need_after_glow = (
            _fill_signature(colors.before.shadow) != _fill_signature(colors.after.shadow)
            or before_radius != after_radius
        )
        band = self.clip_band or _fill_clip_band(self.fill_segments, self.t_ms, self.rtl)
        if not need_after_glow or band is None:
            return None
        indices = {glyph.index for glyph in self.glyphs}
        if not _run_fill_complete(
            self.fill_segments, indices, self.t_ms
        ) or _n3_following_wipe_band(
            self.fill_segments, indices, self.t_ms, self.rtl
        ) is not None:
            return None
        return (
            _glyph_run_after_glow_key(self.glyphs, role_style, colors),
            _relative_fill_rect_signature(
                self.glyphs,
                self.baseline_y,
                self.fill_rect,
                global_anchor=colors.after.shadow.mode == "image",
            ),
        )

    def bake(self, ctx: LayerContext, layout: object, key: Hashable) -> BakedLayer:
        role_style = self.glyphs[0].style
        colors = _effective_karaoke_colors(role_style)
        image, dx, dy = _build_glyph_run_after_glow_layer(
            self.glyphs,
            role_style,
            colors,
            fill_rect=self.fill_rect,
            baseline_y=self.baseline_y,
        )
        return BakedLayer(image=image, offset=QPointF(float(dx), float(dy)))

    def animate(self, ctx: LayerContext, layout: object) -> LayerAnimation:
        run_left = min(glyph.left for glyph in self.glyphs)
        indices = {glyph.index for glyph in self.glyphs}
        following_band = _n3_following_wipe_band(
            self.fill_segments, indices, self.t_ms, self.rtl
        )
        band = following_band or self.clip_band or _fill_clip_band(
            self.fill_segments, self.t_ms, self.rtl
        )
        if band is None:
            return LayerAnimation(opacity=0.0)
        rect = _glyph_run_rect(self.glyphs, self.baseline_y)
        role_style = self.glyphs[0].style
        pad = _glow_extent(
            role_style.stroke_width_px,
            role_style.stroke2_width_px,
            _glow_radius(role_style, after=True),
        )
        complete = (
            _run_fill_complete(self.fill_segments, indices, self.t_ms)
            and following_band is None
        )
        clip_rect = None if complete else _after_glow_loose_clip_rect(
            band,
            rect,
            pad,
            self.rtl,
            complete,
        )
        return LayerAnimation(
            top_left=QPointF(float(run_left), float(self.baseline_y)),
            clip_rect=clip_rect,
            opacity=self.fade_opacity,
            transform=self.transform,
        )

    def paint_dynamic(self, painter: QPainter, ctx: LayerContext, layout: object) -> None:
        indices = {glyph.index for glyph in self.glyphs}
        following_band = _n3_following_wipe_band(
            self.fill_segments, indices, self.t_ms, self.rtl
        )
        band = following_band or self.clip_band or _fill_clip_band(
            self.fill_segments, self.t_ms, self.rtl
        )
        if band is None:
            return
        opacity = max(0.0, min(float(self.fade_opacity), 1.0))
        if opacity <= 0.0:
            return
        complete = (
            _run_fill_complete(self.fill_segments, indices, self.t_ms)
            and following_band is None
        )
        painter.save()
        try:
            painter.setOpacity(painter.opacity() * opacity)
            if self.transform is not None:
                painter.setTransform(self.transform, combine=True)
            _paint_glyph_run_after_glow_wipe(
                painter,
                self.glyphs,
                self.baseline_y,
                band,
                rtl=self.rtl,
                complete=complete,
                fill_rect=self.fill_rect,
            )
        finally:
            painter.restore()

    def vertical_bounds(self, ctx: LayerContext, layout: object) -> tuple[int, int] | None:
        rect = _glyph_run_rect(self.glyphs, self.baseline_y)
        role_style = self.glyphs[0].style
        pad = _glow_extent(
            role_style.stroke_width_px,
            role_style.stroke2_width_px,
            _glow_radius(role_style, after=True),
        )
        return int(math.floor(rect.top() - pad)), int(math.ceil(rect.bottom() + pad))


@dataclass(frozen=True)
class _ScopeBoundsLayer:
    """Bounds-only layer used while a dynamic effect is not yet fully layerized."""

    rect: QRectF
    scope_id: Hashable
    z_index: int = 0
    scope: str = SCOPE_GROUP

    def active_window(self, ctx: LayerContext) -> list[tuple[int, int]]:
        return []

    def layout(self, ctx: LayerContext) -> "_ScopeBoundsLayer":
        return self

    def static_key(self, ctx: LayerContext, layout: object) -> Hashable | None:
        return None

    def bake(self, ctx: LayerContext, layout: object, key: Hashable) -> BakedLayer:
        raise AssertionError("bounds-only layers are never baked")

    def animate(self, ctx: LayerContext, layout: object) -> LayerAnimation:
        return LayerAnimation(clip_rect=self.rect)

    def paint_dynamic(self, painter: QPainter, ctx: LayerContext, layout: object) -> None:
        return

    def vertical_bounds(self, ctx: LayerContext, layout: object) -> tuple[int, int] | None:
        return int(math.floor(self.rect.top())), int(math.ceil(self.rect.bottom()))


def _utopia_transition_scope_layers(
    layout: _LineLayout,
    line: TimingLine,
    style: Style,
    t_ms: int,
    transition: _LineCharTransition,
    frame_height: int,
) -> list[_ScopeBoundsLayer]:
    """Return conservative group-scope bounds for the existing utopia dynamic path."""
    if transition.effect != "utopia":
        return []
    layers = _utopia_main_scope_layers(layout, line, style, t_ms, transition, frame_height)
    if layout.active_rubies and layout.ruby_metrics is not None:
        layers.extend(
            _utopia_ruby_scope_layers(layout, line, style, t_ms, transition, frame_height)
        )
    return layers


def _utopia_main_scope_layers(
    layout: _LineLayout,
    line: TimingLine,
    style: Style,
    t_ms: int,
    transition: _LineCharTransition,
    frame_height: int,
) -> list[_ScopeBoundsLayer]:
    glyphs_by_index = _role_glyphs_by_index(line, layout.text_layout)
    count = max(len(line.chars), 1)
    layers: list[_ScopeBoundsLayer] = []
    handled_indices: set[int] = set()
    ruby_groups = _resolve_char_ruby_groups(layout.active_rubies, line, layout.intervals)
    for index in range(len(line.chars)):
        if index in handled_indices:
            continue
        if index >= len(layout.intervals) or index >= len(layout.char_x_ranges):
            continue
        if index >= len(glyphs_by_index) or glyphs_by_index[index] is None:
            continue
        group = _utopia_main_group_for_index(layout.active_rubies, line, layout.intervals, index, groups=ruby_groups)
        group_ruby: RubyAnnotation | None = None
        group_scope_indices: list[int] | None = None
        group_done_ms: int | None = None
        if group is not None:
            group_scope_indices, group_ruby = group
            group_done_ms = _utopia_following_done_time(
                line, layout.intervals, group_scope_indices[-1], style
            )
            group_exiting = t_ms > group_done_ms
            if group_exiting and index != group_scope_indices[0]:
                continue
            if group_exiting:
                indices = [
                    i
                    for i in group_scope_indices
                    if i < len(layout.intervals)
                    and i < len(layout.char_x_ranges)
                    and i < len(glyphs_by_index)
                    and glyphs_by_index[i] is not None
                ]
                handled_indices.update(indices[1:])
            else:
                indices = [index]
        else:
            indices = [index]
            group_scope_indices = indices

        if not indices:
            continue
        first_index = indices[0]
        last_index = indices[-1]
        following_done_ms = (
            group_done_ms
            if group_done_ms is not None
            else _utopia_following_done_time(line, layout.intervals, last_index, style)
        )
        char_start, char_end = _utopia_wipe_window_for_index(
            line,
            layout.intervals,
            layout.char_x_ranges,
            ruby_groups,
            first_index,
            style,
            fallback_start=layout.intervals[first_index][0],
            fallback_end=layout.intervals[last_index][1],
        )
        opacity, dx, dy, rotation, scale_x, scale_y, skew_y = _transition_char_state(
            style,
            transition,
            first_index,
            count,
            char_start_ms=char_start,
            char_end_ms=char_end,
            t_ms=t_ms,
            frame_height=frame_height,
            following_done_ms=following_done_ms,
        )
        if opacity <= 0.0:
            continue
        group_glyphs = [glyphs_by_index[i] for i in indices if glyphs_by_index[i] is not None]
        if not group_glyphs:
            continue
        left = min(layout.char_x_ranges[i][0] for i in indices)
        right = max(layout.char_x_ranges[i][1] for i in indices)
        width = max(right - left, 1)
        group_rect = _glyph_run_rect(group_glyphs, layout.baseline_y)
        transform = _character_transform(
            center_x=left + width / 2,
            center_y=group_rect.top() + group_rect.height() / 2,
            dx=dx,
            dy=dy,
            rotation=rotation,
            scale_x=scale_x,
            scale_y=scale_y,
            skew_y=skew_y,
            scale_origin_x=left,
            scale_origin_y=layout.baseline_y,
        )
        rect = transform.map(_glyph_run_path(group_glyphs, layout.baseline_y)).boundingRect()
        pad = max(
            _text_visual_padding(glyph.style, after=False) for glyph in group_glyphs
        )
        pad = max(
            pad,
            max(_text_visual_padding(glyph.style, after=True) for glyph in group_glyphs),
        )
        layers.append(
            _ScopeBoundsLayer(
                _inflate_rect(rect, pad),
                _utopia_scope_id(line, group_scope_indices, group_ruby, "main"),
                z_index=index,
            )
        )
    return layers


def _utopia_ruby_scope_layers(
    layout: _LineLayout,
    line: TimingLine,
    style: Style,
    t_ms: int,
    transition: _LineCharTransition,
    frame_height: int,
) -> list[_ScopeBoundsLayer]:
    if layout.ruby_metrics is None:
        return []
    layers: list[_ScopeBoundsLayer] = []
    for index, ruby_layout in enumerate(layout.ruby_layouts):
        if not ruby_layout.indices:
            continue
        target_ruby_font = ruby_layout.font or layout.ruby_font
        target_ruby_metrics = ruby_layout.metrics or layout.ruby_metrics
        rect = _utopia_ruby_scope_rect(
            ruby_layout,
            target_ruby_font,
            target_ruby_metrics,
            line,
            layout.intervals,
            layout.rtl,
            ruby_layout.style,
            t_ms,
            transition,
            frame_height,
        )
        if rect is None:
            continue
        pad = max(
            _ruby_visual_padding(ruby_layout.style, after=False),
            _ruby_visual_padding(ruby_layout.style, after=True),
        )
        layers.append(
            _ScopeBoundsLayer(
                _inflate_rect(rect, pad),
                _utopia_scope_id(line, ruby_layout.indices, ruby_layout.ruby, "ruby"),
                z_index=10_000 + index,
            )
        )
    return layers


def _utopia_ruby_scope_rect(
    layout: _RubyLayout,
    ruby_font: QFont,
    ruby_metrics: QFontMetrics,
    line: TimingLine,
    intervals: list[tuple[int, int]],
    rtl: bool,
    style: Style,
    t_ms: int,
    transition: _LineCharTransition,
    frame_height: int,
) -> QRectF | None:
    first_index = min(layout.indices)
    last_index = max(layout.indices)
    if first_index >= len(intervals) or last_index >= len(intervals):
        return None
    following_done_ms = _utopia_following_done_time(line, intervals, last_index, style)
    ruby_groups = _resolve_char_ruby_groups([layout.ruby], line, intervals)
    char_x_ranges = [(layout.x, layout.x + layout.target_width) for _index in line.chars]
    char_start, char_end = _utopia_wipe_window_for_index(
        line,
        intervals,
        char_x_ranges,
        ruby_groups,
        first_index,
        style,
        fallback_start=intervals[first_index][0],
        fallback_end=intervals[last_index][1],
    )
    opacity, dx, dy, rotation, scale_x, scale_y, skew_y = _transition_char_state(
        style,
        transition,
        first_index,
        max(len(line.chars), 1),
        char_start_ms=char_start,
        char_end_ms=char_end,
        t_ms=t_ms,
        frame_height=frame_height,
        following_done_ms=following_done_ms,
    )
    if opacity <= 0.0:
        return None
    group_exiting = len(layout.indices) > 1 and t_ms > following_done_ms
    if group_exiting:
        reading = (
            "".join(reversed(_ruby_utopia_visual_units(layout.ruby.reading)))
            if rtl
            else layout.ruby.reading
        )
        path, _ = _ruby_text_path_and_rect(
            reading,
            ruby_font,
            ruby_metrics,
            layout.x,
            layout.baseline_y,
            layout.target_width,
            style,
            base_text=layout.ruby.kanji,
        )
        transform = _character_transform(
            center_x=layout.x + layout.reading_width / 2,
            center_y=layout.baseline_y - ruby_metrics.ascent() + ruby_metrics.height() / 2,
            dx=dx,
            dy=dy,
            rotation=rotation,
            scale_x=scale_x,
            scale_y=scale_y,
            skew_y=skew_y,
            scale_origin_x=layout.x,
            scale_origin_y=layout.baseline_y,
        )
        return transform.map(path).boundingRect()

    visual_units = _ruby_utopia_reading_units_and_intervals(layout.ruby)
    if rtl:
        visual_units = list(reversed(visual_units))
    units = [unit for unit, _interval in visual_units]
    unit_intervals = [interval for _unit, interval in visual_units]
    if not units or len(units) != len(unit_intervals):
        path, _ = _ruby_text_path_and_rect(
            layout.ruby.reading,
            ruby_font,
            ruby_metrics,
            layout.x,
            layout.baseline_y,
            layout.target_width,
            style,
            base_text=layout.ruby.kanji,
        )
        return path.boundingRect()

    rect: QRectF | None = None
    for (unit, unit_x, unit_width), (start_ms, end_ms) in zip(
        _ruby_layout_units(
            units,
            ruby_metrics,
            layout.x,
            layout.target_width,
            style=style,
            base_text=layout.ruby.kanji,
        ),
        unit_intervals,
    ):
        opacity, dx, dy, rotation, scale_x, scale_y, skew_y = _transition_char_state(
            style,
            transition,
            first_index,
            max(len(line.chars), 1),
            char_start_ms=start_ms,
            char_end_ms=end_ms,
            t_ms=t_ms,
            frame_height=frame_height,
            following_done_ms=following_done_ms,
        )
        if opacity <= 0.0:
            continue
        path = QPainterPath()
        path.addText(float(unit_x), float(layout.baseline_y), ruby_font, unit)
        transform = _character_transform(
            center_x=unit_x + unit_width / 2,
            center_y=layout.baseline_y - ruby_metrics.ascent() + ruby_metrics.height() / 2,
            dx=dx,
            dy=dy,
            rotation=rotation,
            scale_x=scale_x,
            scale_y=scale_y,
            skew_y=skew_y,
            scale_origin_x=unit_x,
            scale_origin_y=layout.baseline_y,
        )
        unit_rect = transform.map(path).boundingRect()
        rect = unit_rect if rect is None else rect.united(unit_rect)
    return rect


def _utopia_scope_id(
    line: TimingLine,
    indices: list[int],
    ruby: RubyAnnotation | None,
    kind: str,
) -> tuple:
    return (
        "utopia",
        kind,
        _line_start_ms(line),
        _line_end_ms(line),
        tuple(indices),
        ruby.kanji if ruby is not None else "",
        ruby.reading if ruby is not None else "",
    )


def _inflate_rect(rect: QRectF, pad: int | float) -> QRectF:
    pad_f = float(max(pad, 0))
    return rect.adjusted(-pad_f, -pad_f, pad_f, pad_f)


def _glyph_run_layer_key(
    glyphs: list[_GlyphLayout],
    role_style: Style,
    colors: KaraokeColors,
    *,
    after: bool,
) -> tuple:
    """run 层缓存 key：run 内逐字形（文本/字体/相对 x/宽）+ 角色样式签名 + 状态。

    扫光带不进 key（blit 时半平面 clip 处理）；run 绝对位置不进 key（blit offset 复位）。
    """
    run_left = min(glyph.left for glyph in glyphs)
    glyph_sig = tuple(
        (
            glyph.text,
            glyph.font.family(),
            glyph.font.pixelSize(),
            int(glyph.font.weight()),
            glyph.font.italic(),
            glyph.left - run_left,
            round(float(glyph.path_offset_x), 3),
            glyph.width,
            _value_signature(glyph.vector_glyph),
        )
        for glyph in glyphs
    )
    state = colors.after if after else colors.before
    return (
        glyph_sig,
        _karaoke_state_signature(state),
        role_style.shadow_offset_x,
        role_style.shadow_offset_y,
        role_style.stroke_width_px,
        _main_stroke2_width(role_style),
        role_style.decoration_kind,
        _glow_radius(role_style, after=False),
        _glow_concentration_level(role_style),
        after,
    )


def _relative_fill_rect_signature(
    glyphs: list[_GlyphLayout],
    baseline_y: int,
    fill_rect: QRectF | None,
    *,
    global_anchor: bool = False,
) -> tuple[float, float, float, float] | None:
    """Return the brush coordinates that affect a cached glyph run."""
    run_left = min(glyph.left for glyph in glyphs)
    if global_anchor:
        if fill_rect is None:
            return (
                round(float(run_left), 3),
                round(float(baseline_y), 3),
                0.0,
                0.0,
            )
        return (
            round(float(fill_rect.left()), 3),
            round(float(fill_rect.top()), 3),
            round(float(fill_rect.width()), 3),
            round(float(fill_rect.height()), 3),
        )
    if fill_rect is None:
        return None
    return (
        round(float(fill_rect.left()) - run_left, 3),
        round(float(fill_rect.top()) - baseline_y, 3),
        round(float(fill_rect.width()), 3),
        round(float(fill_rect.height()), 3),
    )


def _karaoke_state_uses_image(state: KaraokeColorState) -> bool:
    return any(
        fill.mode == "image"
        for fill in (state.text, state.stroke, state.stroke2, state.shadow)
    )


def _glyph_run_after_glow_key(
    glyphs: list[_GlyphLayout],
    role_style: Style,
    colors: KaraokeColors,
) -> tuple:
    run_left = min(glyph.left for glyph in glyphs)
    glyph_sig = tuple(
        (
            glyph.text,
            glyph.font.family(),
            glyph.font.pixelSize(),
            int(glyph.font.weight()),
            glyph.font.italic(),
            glyph.left - run_left,
            round(float(glyph.path_offset_x), 3),
            glyph.width,
            _value_signature(glyph.vector_glyph),
        )
        for glyph in glyphs
    )
    return (
        "after_glow",
        glyph_sig,
        _fill_signature(colors.after.shadow),
        role_style.stroke_width_px,
        _main_stroke2_width(role_style),
        _glow_radius(role_style, after=True),
        _glow_concentration_level(role_style),
        role_style.decoration_kind,
    )


def _karaoke_glow_states_differ(style: Style, colors: KaraokeColors) -> bool:
    """前后发光状态（颜色签名 + 半径）是否不同。

    N3 在 ``WipeLeft`` 两侧互补裁剪前后描边源，再对合成源做 blur；因此锋线
    只硬分割字形墨水/描边，模糊后的两色 halo 可以跨线混合。状态相同时无需
    拆源，整字画一次未唱发光即可。
    """
    if style.decoration_kind != "glow":
        return False
    return (
        _fill_signature(colors.before.shadow) != _fill_signature(colors.after.shadow)
        or _glow_radius(style, after=False) != _glow_radius(style, after=True)
    )


def _glyph_run_needs_after_glow(glyphs: list[_GlyphLayout]) -> bool:
    if not glyphs:
        return False
    role_style = glyphs[0].style
    if _glow_radius(role_style, after=True) == 0:
        return False
    return _karaoke_glow_states_differ(role_style, _effective_karaoke_colors(role_style))


def _glyph_run_needs_before_glow_split(glyphs: list[_GlyphLayout]) -> bool:
    if not glyphs:
        return False
    role_style = glyphs[0].style
    if _glow_radius(role_style, after=False) == 0:
        return False
    return _karaoke_glow_states_differ(
        role_style, _effective_karaoke_colors(role_style)
    )


def _ruby_glow_states_differ(style: Style) -> bool:
    """注音前后发光状态是否不同（语义同主字的裁源后模糊判据）。"""
    if _ruby_decoration_kind(style) != "glow":
        return False
    colors = _effective_ruby_karaoke_colors(style)
    return (
        _fill_signature(colors.before.shadow) != _fill_signature(colors.after.shadow)
        or _ruby_glow_radius(style, after=False) != _ruby_glow_radius(style, after=True)
    )


def _ruby_glow_can_combine_split(style: Style) -> bool:
    """Whether one source bitmap can represent both ruby glow colours."""
    if not _ruby_glow_states_differ(style):
        return False
    before_radius = _ruby_glow_radius(style, after=False)
    return before_radius > 0 and before_radius == _ruby_glow_radius(style, after=True)


def _build_glyph_run_layer(
    glyphs: list[_GlyphLayout],
    role_style: Style,
    colors: KaraokeColors,
    *,
    after: bool,
    supersample: float = 1.0,
    fill_rect: QRectF | None = None,
    baseline_y: int = 0,
) -> tuple[QImage, int, int]:
    """把一个角色 run 的某状态烘焙成透明 QImage。

    ``after=False``（未唱层）：glow(before) 或 阴影(before) + stroke2 + stroke + 底色。
    ``after=True``（已唱主体）：阴影(after，仅非 glow) + stroke2 + stroke + 底色，
    **不含 glow 模糊**（已唱 glow 由 :class:`_GlyphRunAfterGlowLayer` 单独烘焙）。

    run 内逐字形可有不同字体/字号，故按 glyph 各自的 ``font`` 排版。返回 ``(image, dx, dy)``，
    blit 时画在 ``(run_left + dx, baseline_y + dy)``。
    """
    state = colors.after if after else colors.before
    run_left = min(glyph.left for glyph in glyphs)
    run_right = max(glyph.left + glyph.width for glyph in glyphs)
    run_ascent = max(glyph.metrics.ascent() for glyph in glyphs)
    run_descent = max(glyph.metrics.descent() for glyph in glyphs)
    run_w = max(run_right - run_left, 1)
    run_h = max(run_ascent + run_descent, 1)
    stroke2_width = _main_stroke2_width(role_style)

    is_glow = role_style.decoration_kind == "glow"
    bake_glow = (
        is_glow
        and not after
        and not _karaoke_glow_states_differ(role_style, colors)
    )
    has_shadow = (
        role_style.decoration_kind == "shadow"
        and bool(role_style.shadow_color)
        and bool(role_style.shadow_offset_x or role_style.shadow_offset_y)
    )

    stroke_extent = _visual_stroke_extent(
        role_style.stroke_width_px, stroke2_width
    )
    glow_extra = (
        _glow_extent(
            role_style.stroke_width_px,
            stroke2_width,
            _glow_radius(role_style, after=False),
        )
        if bake_glow
        else 0
    )
    extent = max(stroke_extent, glow_extra, 0) + 4
    shadow_dx = role_style.shadow_offset_x if has_shadow else 0
    shadow_dy = role_style.shadow_offset_y if has_shadow else 0
    pad_left = max(0, -shadow_dx) + extent
    pad_right = max(0, shadow_dx) + extent
    pad_top = max(0, -shadow_dy) + extent
    pad_bottom = max(0, shadow_dy) + extent

    img_w = max(pad_left + run_w + pad_right, 1)
    img_h = max(pad_top + run_h + pad_bottom, 1)

    # supersample：把同一份「自然坐标」绘制逻辑渲染进 S× 分辨率位图（``p.scale(S,S)``），
    # 调用方再以 1/S 缩放贴出 → utopia 入场放大相位不糊。offset 仍以自然坐标返回。
    s = max(float(supersample), 1.0)
    image = QImage(
        max(int(round(img_w * s)), 1),
        max(int(round(img_h * s)), 1),
        QImage.Format.Format_ARGB32_Premultiplied,
    )
    image.fill(0)

    target_origin_x = float(run_left - pad_left)
    target_origin_y = float(baseline_y - run_ascent - pad_top)
    path = _glyph_run_path(glyphs, baseline_y)
    rect = QRectF(
        float(run_left),
        float(baseline_y - run_ascent),
        float(run_w),
        float(run_h),
    )
    brush_rect = fill_rect if fill_rect is not None else rect

    p = QPainter(image)
    try:
        if s != 1.0:
            p.scale(s, s)
        p.translate(-target_origin_x, -target_origin_y)
        p.setRenderHints(
            QPainter.RenderHint.Antialiasing
            | QPainter.RenderHint.TextAntialiasing
            | QPainter.RenderHint.SmoothPixmapTransform
        )
        # 1) glow（仅未唱层）/ 阴影（仅非 glow）
        if bake_glow:
            _paint_glow_path(
                p,
                path,
                state.shadow,
                brush_rect,
                _glow_radius(role_style, after=False),
                role_style.stroke_width_px,
                stroke2_width,
                concentration_level=_glow_concentration_level(role_style),
            )
        elif has_shadow:
            _paint_shadow_silhouette(
                p,
                path,
                state.shadow,
                brush_rect,
                role_style.shadow_offset_x,
                role_style.shadow_offset_y,
                role_style.stroke_width_px,
                stroke2_width,
            )
        # 2) stroke2
        if stroke2_width > 0:
            _paint_stroke_path(
                p,
                path,
                state.stroke2,
                brush_rect,
                _stroke2_pen_width(role_style.stroke_width_px, stroke2_width),
            )
        # 3) stroke
        if role_style.stroke_color and role_style.stroke_width_px > 0:
            _paint_stroke_path(
                p,
                path,
                state.stroke,
                brush_rect,
                _stroke_pen_width(role_style.stroke_width_px),
                protect_body=_fill_is_alpha(state.text),
            )
        # 4) 底色文字
        _paint_fill_path(p, path, state.text, brush_rect)
    finally:
        p.end()

    offset_x = -pad_left
    offset_y = -(pad_top + run_ascent)
    return (image, offset_x, offset_y)


def _build_glyph_run_glow_layer(
    glyphs: list[_GlyphLayout],
    role_style: Style,
    colors: KaraokeColors,
    *,
    after: bool,
    fill_rect: QRectF | None = None,
    baseline_y: int = 0,
) -> tuple[QImage, int, int]:
    """Bake the full unclipped glow image (before/after state) for a glyph run."""
    state = colors.after if after else colors.before
    run_left = min(glyph.left for glyph in glyphs)
    run_right = max(glyph.left + glyph.width for glyph in glyphs)
    run_ascent = max(glyph.metrics.ascent() for glyph in glyphs)
    run_descent = max(glyph.metrics.descent() for glyph in glyphs)
    run_w = max(run_right - run_left, 1)
    run_h = max(run_ascent + run_descent, 1)
    radius = _glow_radius(role_style, after=after)
    stroke2_width = _main_stroke2_width(role_style)
    extent = _glow_extent(
        role_style.stroke_width_px, stroke2_width, radius
    ) + 4

    img_w = max(extent + run_w + extent, 1)
    img_h = max(extent + run_h + extent, 1)
    image = QImage(img_w, img_h, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(0)

    target_origin_x = float(run_left - extent)
    target_origin_y = float(baseline_y - run_ascent - extent)
    path = _glyph_run_path(glyphs, baseline_y)
    rect = QRectF(
        float(run_left),
        float(baseline_y - run_ascent),
        float(run_w),
        float(run_h),
    )
    brush_rect = fill_rect if fill_rect is not None else rect

    p = QPainter(image)
    try:
        p.translate(-target_origin_x, -target_origin_y)
        p.setRenderHints(
            QPainter.RenderHint.Antialiasing
            | QPainter.RenderHint.TextAntialiasing
            | QPainter.RenderHint.SmoothPixmapTransform
        )
        _paint_glow_path(
            p,
            path,
            state.shadow,
            brush_rect,
            radius,
            role_style.stroke_width_px,
            stroke2_width,
            concentration_level=_glow_concentration_level(role_style),
        )
    finally:
        p.end()

    offset_x = -extent
    offset_y = -(extent + run_ascent)
    return (image, offset_x, offset_y)


def _build_glyph_run_after_glow_layer(
    glyphs: list[_GlyphLayout],
    role_style: Style,
    colors: KaraokeColors,
    *,
    fill_rect: QRectF | None = None,
    baseline_y: int = 0,
) -> tuple[QImage, int, int]:
    """Bake the full unclipped after-glow image for a glyph run."""
    return _build_glyph_run_glow_layer(
        glyphs,
        role_style,
        colors,
        after=True,
        fill_rect=fill_rect,
        baseline_y=baseline_y,
    )


def _get_or_build_run_glow(
    glyphs: list[_GlyphLayout],
    role_style: Style,
    colors: KaraokeColors,
    *,
    after: bool,
    fill_rect: QRectF | None = None,
    baseline_y: int = 0,
) -> BakedLayer:
    """A3：按上正 glyph 身份缓存 glow 烘焙位图（before/after 各一条）。"""
    key = (
        _glyph_run_layer_key(glyphs, role_style, colors, after=after),
        "glow",
        after,
        _relative_fill_rect_signature(
            glyphs,
            baseline_y,
            fill_rect,
            global_anchor=(colors.after if after else colors.before).shadow.mode
            == "image",
        ),
    )
    return _RUN_GLOW_CACHE.get_or_build(
        key,
        lambda: _baked_run_glow(
            glyphs,
            role_style,
            colors,
            after=after,
            fill_rect=fill_rect,
            baseline_y=baseline_y,
        ),
    )


def _baked_run_glow(
    glyphs: list[_GlyphLayout],
    role_style: Style,
    colors: KaraokeColors,
    *,
    after: bool,
    fill_rect: QRectF | None = None,
    baseline_y: int = 0,
) -> BakedLayer:
    image, dx, dy = _build_glyph_run_glow_layer(
        glyphs,
        role_style,
        colors,
        after=after,
        fill_rect=fill_rect,
        baseline_y=baseline_y,
    )
    return BakedLayer(image=image, offset=QPointF(float(dx), float(dy)))


def _get_or_build_run_glow_mask(
    glyphs: list[_GlyphLayout],
    role_style: Style,
    *,
    after: bool,
) -> BakedLayer:
    """Cache an opaque-white glow alpha mask for spatial brush fills."""
    mask_fill = PaintFill(mode="solid", color="#FFFFFF")
    mask_state = KaraokeColorState(shadow=mask_fill)
    mask_colors = KaraokeColors(before=mask_state, after=mask_state)
    key = (
        _glyph_run_layer_key(
            glyphs, role_style, mask_colors, after=after
        ),
        "glow-mask",
        after,
    )
    return _RUN_GLOW_CACHE.get_or_build(
        key,
        lambda: _baked_run_glow(
            glyphs,
            role_style,
            mask_colors,
            after=after,
        ),
    )


def _blit_tinted_run_glow_mask(
    painter: QPainter,
    glyphs: list[_GlyphLayout],
    baseline_y: int,
    role_style: Style,
    fill: PaintFill,
    *,
    after: bool,
    transform: QTransform | None,
    fill_rect: QRectF,
) -> None:
    """Transform a cached glow mask, then colour it in fixed line space."""
    baked = _get_or_build_run_glow_mask(
        glyphs, role_style, after=after
    )
    if baked.image.isNull():
        return
    run_left = min(glyph.left for glyph in glyphs)
    anchor = QPointF(
        float(run_left) + baked.offset.x(),
        float(baseline_y) + baked.offset.y(),
    )
    source_rect = QRectF(
        anchor.x(),
        anchor.y(),
        float(baked.image.width()),
        float(baked.image.height()),
    )
    effective_transform = transform or QTransform()
    mapped = effective_transform.mapRect(source_rect)
    left = math.floor(mapped.left())
    top = math.floor(mapped.top())
    right = math.ceil(mapped.right())
    bottom = math.ceil(mapped.bottom())
    width = max(right - left, 1)
    height = max(bottom - top, 1)
    tinted = QImage(
        width, height, QImage.Format.Format_ARGB32_Premultiplied
    )
    tinted.fill(0)

    mask_painter = QPainter(tinted)
    try:
        mask_painter.setRenderHint(
            QPainter.RenderHint.SmoothPixmapTransform, True
        )
        local_transform = QTransform(effective_transform)
        local_transform *= QTransform.fromTranslate(-float(left), -float(top))
        mask_painter.setTransform(local_transform)
        mask_painter.drawImage(anchor, baked.image)
        mask_painter.resetTransform()
        mask_painter.setCompositionMode(
            QPainter.CompositionMode.CompositionMode_SourceIn
        )
        local_fill_rect = fill_rect.translated(-float(left), -float(top))
        mask_painter.fillRect(
            QRectF(0.0, 0.0, float(width), float(height)),
            _brush_for_fill(fill, local_fill_rect),
        )
    finally:
        mask_painter.end()
    painter.drawImage(QPointF(float(left), float(top)), tinted)


def _blit_cached_run_glow(
    painter: QPainter,
    glyphs: list[_GlyphLayout],
    baseline_y: int,
    role_style: Style,
    colors: KaraokeColors,
    *,
    after: bool,
    transform: QTransform | None,
    fill_rect: QRectF | None = None,
) -> None:
    """A3：在 ``transform`` 下贴出缓存的上正 glow 位图（替代逐帧 ``_paint_glow_path``）。

    glow 在上正坐标烘焙、自然 anchor ``(run_left+dx, baseline_y+dy)`` 贴出；``transform``
    把它送到与逐帧矢量 body 相同的变换位置。调用方在贴前已设好设备空间 clip（扫光带），
    本函数 ``setTransform(combine=True)`` 不影响该 clip（Qt clip 存于设备坐标）。
    ``fill_rect`` is the shared line brush area used by N3 gradients and
    MilleFeuille fills.
    """
    if _glow_radius(role_style, after=after) == 0:
        return
    state = colors.after if after else colors.before
    if (
        state.shadow.mode != "solid"
        and fill_rect is not None
        and transform is not None
        and not transform.isIdentity()
    ):
        _blit_tinted_run_glow_mask(
            painter,
            glyphs,
            baseline_y,
            role_style,
            state.shadow,
            after=after,
            transform=transform,
            fill_rect=fill_rect,
        )
        return
    baked = _get_or_build_run_glow(
        glyphs, role_style, colors, after=after,
        fill_rect=fill_rect, baseline_y=baseline_y,
    )
    if baked.image.isNull():
        return
    run_left = min(glyph.left for glyph in glyphs)
    anchor = QPointF(float(run_left) + baked.offset.x(), float(baseline_y) + baked.offset.y())
    painter.save()
    try:
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        if transform is not None:
            painter.setTransform(transform, combine=True)
        painter.drawImage(anchor, baked.image)
    finally:
        painter.restore()


def _paint_role_line_with_character_transition(
    painter: QPainter,
    line: TimingLine,
    layout: _TextLayout,
    char_x_ranges: list[tuple[int, int]],
    intervals: list[tuple[int, int]],
    active_rubies: list[RubyAnnotation],
    baseline_y: int,
    t_ms: int,
    transition: _LineCharTransition,
    style: Style,
    *,
    rtl: bool = False,
    ink_x_ranges: list[tuple[int, int]] | None = None,
    fill_segments: list[_FillSegment] | None = None,
) -> None:
    # 走字 ratio 按墨水边界算（与静态路径一致）；缺省回退 advance 框。
    fill_ranges = ink_x_ranges if ink_x_ranges is not None else char_x_ranges
    fill_rect = _n3_main_fill_rect(layout, baseline_y)
    glyphs_by_index = _role_glyphs_by_index(line, layout)
    count = max(len(line.chars), 1)
    ruby_groups = _resolve_char_ruby_groups(active_rubies, line, intervals)
    for index in range(len(line.chars)):
        if index >= len(intervals) or index >= len(char_x_ranges):
            continue
        layout_glyph = glyphs_by_index[index]
        if layout_glyph is None:
            continue
        if _glyph_is_bitmap_guide(layout_glyph):
            _paint_bitmap_guide_transition_glyph(
                painter,
                layout_glyph,
                fill_segments or [],
                baseline_y,
                line,
                intervals,
                index,
                t_ms,
                transition,
                style,
                rtl=rtl,
            )
            continue

        group = _utopia_main_group_for_index(active_rubies, line, intervals, index, groups=ruby_groups) if transition.effect == "utopia" else None
        group_done_ms: int | None = None
        if group is not None:
            group_indices, _group_ruby = group
            group_done_ms = _utopia_following_done_time(line, intervals, group_indices[-1], style)
        indices = [index]
        group_ruby = None

        if not indices:
            continue
        left = min(char_x_ranges[i][0] for i in indices)
        right = max(char_x_ranges[i][1] for i in indices)
        width = max(right - left, 1)
        first_index = indices[0]
        last_index = indices[-1]
        char_start, char_end = _utopia_wipe_window_for_index(
            line,
            intervals,
            fill_ranges,
            ruby_groups,
            index,
            style,
            fallback_start=intervals[first_index][0],
            fallback_end=intervals[last_index][1],
        )
        following_done_ms = (
            group_done_ms
            if group_done_ms is not None
            else _utopia_following_done_time(line, intervals, last_index, style)
            if transition.effect == "utopia"
            else None
        )
        opacity, dx, dy, rotation, scale_x, scale_y, skew_y = _transition_char_state(
            style,
            transition,
            first_index,
            count,
            char_start_ms=char_start,
            char_end_ms=char_end,
            t_ms=t_ms,
            frame_height=painter.device().height(),
            following_done_ms=following_done_ms,
        )
        if opacity <= 0.0:
            continue

        group_glyphs = [glyphs_by_index[i] for i in indices if glyphs_by_index[i] is not None]
        group_rect = _glyph_run_rect(group_glyphs, baseline_y)
        group_center_x = left + width / 2
        group_center_y = group_rect.top() + group_rect.height() / 2
        group_transform = QTransform()
        group_clip_rect: QRectF | None = None
        paint_left = left
        paint_width = width
        if transition.effect == "utopia":
            group_transform = _character_transform(
                center_x=group_center_x,
                center_y=group_center_y,
                dx=dx,
                dy=dy,
                rotation=rotation,
                scale_x=scale_x,
                scale_y=scale_y,
                skew_y=skew_y,
                scale_origin_x=left,
                scale_origin_y=baseline_y,
            )
            group_path = _glyph_run_path(group_glyphs, baseline_y)
            transformed_group_path = group_transform.map(group_path)
            group_clip_rect = transformed_group_path.boundingRect()
            paint_left, paint_width = _n3_transformed_wipe_span(
                transformed_group_path,
                group_glyphs[0].style.stroke_width_px,
            )

        # utopia 退场阶段整词早已唱完：强制 ratio=1.0，避免对已旋转/翻转的字形再按设备空间
        # 水平带裁切已唱层而把部分着色裁掉（详见 _paint_line_with_character_transition 同处注释）。
        in_utopia_exit = (
            transition.effect == "utopia"
            and style.exit_anim == "utopia"
            and following_done_ms is not None
            and t_ms > following_done_ms
        )
        if in_utopia_exit:
            ratio = 1.0
        elif group_ruby is not None:
            ratio = _main_text_ruby_progress_ratio(
                group_ruby, t_ms, mode=style.ruby_main_progress_mode
            )
        else:
            ratio = _character_fill_ratio(
                line,
                intervals,
                fill_ranges,
                active_rubies,
                index,
                t_ms,
                groups=ruby_groups,
                ruby_main_progress_mode=style.ruby_main_progress_mode,
            )
        for run in _glyph_runs_for_indices(glyphs_by_index, indices):
            role_style = run[0].style
            colors = _effective_karaoke_colors(role_style)
            run_path = _glyph_run_path(run, baseline_y)
            run_rect = _glyph_run_rect(run, baseline_y)
            run_metrics = max(run, key=lambda glyph: glyph.metrics.ascent() + glyph.metrics.descent()).metrics
            painter.save()
            try:
                painter.setOpacity(painter.opacity() * opacity)
                paint_path = run_path
                paint_rect = run_rect
                clip_rect = group_clip_rect
                if transition.effect == "utopia":
                    paint_path = group_transform.map(run_path)
                    paint_rect = paint_path.boundingRect()
                else:
                    _apply_character_transform(
                        painter,
                        center_x=group_center_x,
                        center_y=group_center_y,
                        dx=dx,
                        dy=dy,
                        rotation=rotation,
                        scale_x=scale_x,
                        scale_y=scale_y,
                        skew_y=skew_y,
                    )
                    clip_rect = None
                use_glow_cache = transition.effect == "utopia" and _glow_cache_enabled()
                _paint_char_karaoke_stack(
                    painter,
                    paint_path,
                    paint_rect,
                    char_x=paint_left,
                    char_width=paint_width,
                    baseline_y=baseline_y,
                    metrics=run_metrics,
                    colors=colors,
                    style=role_style,
                    ratio=ratio,
                    rtl=rtl,
                    clip_rect=clip_rect,
                    glow_run=run if use_glow_cache else None,
                    glow_transform=group_transform if use_glow_cache else None,
                    geometry_transform=(
                        group_transform if transition.effect == "utopia" else None
                    ),
                    fill_rect=fill_rect,
                )
            finally:
                painter.restore()


def _role_glyphs_by_index(
    line: TimingLine,
    layout: _TextLayout,
) -> list[_GlyphLayout | None]:
    glyphs: list[_GlyphLayout | None] = [None for _ in line.chars]
    for glyph in layout.glyphs:
        if 0 <= glyph.index < len(glyphs):
            glyphs[glyph.index] = glyph
    return glyphs


def _glyph_runs_for_indices(
    glyphs_by_index: list[_GlyphLayout | None],
    indices: list[int],
) -> list[list[_GlyphLayout]]:
    runs: list[list[_GlyphLayout]] = []
    current: list[_GlyphLayout] = []
    current_signature: tuple | None = None
    signature_cache: dict[int, tuple] = {}
    for index in indices:
        if not (0 <= index < len(glyphs_by_index)):
            continue
        glyph = glyphs_by_index[index]
        if glyph is None:
            continue
        style_id = id(glyph.style)
        signature = signature_cache.get(style_id)
        if signature is None:
            signature = _glyph_run_signature(glyph)
            signature_cache[style_id] = signature
        if current and signature != current_signature:
            runs.append(current)
            current = []
        current.append(glyph)
        current_signature = signature
    if current:
        runs.append(current)
    return runs


def _role_char_ink_ranges_by_index(
    line: TimingLine,
    layout: _TextLayout,
    char_x_ranges: list[tuple[int, int]],
) -> list[tuple[int, int]]:
    """分色行各字符的墨水边界（逐 glyph 用各自字体），用于走字扫光。

    缺失/空白字符回退为 advance 框左缘的零宽 ``(left, left)``，与
    :func:`_char_ink_x_ranges` 同口径（见其 docstring）。
    """
    ranges: list[tuple[int, int]] = [(left, left) for left, _ in char_x_ranges]
    for glyph in layout.glyphs:
        if not (0 <= glyph.index < len(ranges)):
            continue
        text = glyph.text
        left = glyph.left
        if not text or text.isspace():
            ranges[glyph.index] = (left, left)
            continue
        path = _glyph_path(glyph, 0)
        br = path.boundingRect()
        if br.isEmpty():
            ranges[glyph.index] = (left, left)
        else:
            ranges[glyph.index] = (int(math.floor(br.left())), int(math.ceil(br.right())))
    return ranges


def _n3_char_wipe_ranges_by_index(
    line: TimingLine,
    layout: _TextLayout,
    char_x_ranges: list[tuple[int, int]],
    ink_x_ranges: list[tuple[int, int]],
) -> list[tuple[int, int]]:
    """Return N3 ``WipeLeft`` bounds for each main-text glyph.

    N3 interpolates across the transformed glyph geometry expanded by half of
    the primary edge size.  The character advance box is only layout geometry;
    using it as the wipe range spends singing time in transparent side bearings
    and creates a visible pause at otherwise contiguous character timestamps.
    Empty glyphs (notably timed spaces) deliberately keep zero-width geometry.
    """
    ranges = list(ink_x_ranges)
    for glyph in layout.glyphs:
        if not (0 <= glyph.index < len(ranges)):
            continue
        ink_left, ink_right = ranges[glyph.index]
        if not glyph.text or glyph.text.isspace() or ink_right <= ink_left:
            ranges[glyph.index] = (char_x_ranges[glyph.index][0],) * 2
            continue
        # N3 truncates the scaled EdgeSize first, then performs integer / 2.
        edge_half = max(int(glyph.style.stroke_width_px), 0) // 2
        ranges[glyph.index] = (ink_left - edge_half, ink_right + edge_half)
    return ranges


def _n3_transformed_wipe_span(
    path: QPainterPath,
    stroke_width: int,
) -> tuple[int, int]:
    """Return N3 WipeLeft's transformed ink bounds plus half primary edge."""
    bounds = path.boundingRect()
    edge_half = max(int(stroke_width), 0) // 2
    left = int(math.floor(bounds.left())) - edge_half
    right = int(math.ceil(bounds.right())) + edge_half
    return left, max(right - left, 1)


def _line_text_path(
    line: TimingLine,
    char_widths: list[int],
    font: QFont,
    x: int,
    y: int,
    char_lefts: list[int] | None = None,
    font_for=None,
    char_path_offsets: list[float] | None = None,
) -> QPainterPath:
    path = QPainterPath()
    if char_lefts is None:
        char_lefts = _char_left_positions(char_widths, x, False)
    if char_path_offsets is None:
        char_path_offsets = [0.0 for _ in char_lefts]
    for ch, left, path_offset_x in zip(line.chars, char_lefts, char_path_offsets):
        glyph_font = font_for(ch.text) if font_for is not None else font
        path.addText(float(left + path_offset_x), float(y), glyph_font, ch.text)
    return path


def _line_char_transition_context(
    style: Style,
    line: TimingLine,
    t_ms: int,
    display_start_ms: int | None,
    display_end_ms: int | None,
    char_count: int,
    *,
    intervals: list[tuple[int, int]] | None = None,
) -> _LineCharTransition | None:
    if char_count <= 0:
        return None
    start = display_start_ms if display_start_ms is not None else _line_start_ms(line)
    end = display_end_ms if display_end_ms is not None else _line_end_ms(line)

    if style.exit_anim in {"char_fade", "char_drip", "spin_flip"} and style.exit_fade_ms > 0:
        exit_start = max(_line_end_ms(line), end - _CHAR_FADE_INTRO_DELAY_MS - _CHAR_FADE_OUT_TIME_MS)
        if t_ms >= exit_start:
            return _LineCharTransition(
                phase="exit",
                effect=style.exit_anim,
                progress=1.0,
                start_ms=exit_start,
                end_ms=end,
            )

    if style.entry_anim in {"char_fade", "char_drip", "spin_flip"} and style.entry_lead_ms > 0:
        entry_end = start + _CHAR_FADE_INTRO_DELAY_MS + _CHAR_FADE_IN_TIME_MS
        if t_ms <= entry_end:
            return _LineCharTransition(
                phase="entry",
                effect=style.entry_anim,
                progress=1.0,
                start_ms=start,
                end_ms=entry_end,
            )

    if (
        style.entry_anim == "utopia"
        or style.exit_anim == "utopia"
        or effective_karaoke_animation(style) == "utopia"
    ):
        intervals = intervals if intervals is not None else compute_char_intervals(line)
        # Utopia 的入场、唱中弹跳和退场原本只在各自的活动窗口切进逐字符
        # 矢量路径，其余时刻回到整行静态路径。两条路径在发光叠加与边缘
        # 抗锯齿上存在细微差异，切换瞬间即使配色完全相同也会产生色闪。
        # 行可见期间始终保留 Utopia 上下文；但若另一侧正在执行逐字
        # 入/退场，上面的活动过渡必须优先，避免 Utopia 吞掉其效果。
        if start <= t_ms <= end:
            return _LineCharTransition(
                phase="utopia",
                effect="utopia",
                progress=1.0,
                start_ms=start,
                end_ms=end,
            )
    return None


def _paint_line_with_character_transition(
    painter: QPainter,
    line: TimingLine,
    char_widths: list[int],
    char_x_ranges: list[tuple[int, int]],
    intervals: list[tuple[int, int]],
    active_rubies: list[RubyAnnotation],
    font: QFont,
    baseline_y: int,
    metrics: QFontMetrics,
    style: Style,
    colors: KaraokeColors,
    line_rect: QRectF,
    t_ms: int,
    transition: _LineCharTransition,
    rtl: bool = False,
    font_for=None,
    ink_x_ranges: list[tuple[int, int]] | None = None,
    glyphs_by_index: list[_GlyphLayout | None] | None = None,
    fill_rect: QRectF | None = None,
    fill_segments: list[_FillSegment] | None = None,
) -> None:
    # 走字 ratio 按墨水边界算（与静态路径一致）；缺省回退 advance 框。
    fill_ranges = ink_x_ranges if ink_x_ranges is not None else char_x_ranges
    count = max(len(line.chars), 1)
    ruby_groups = _resolve_char_ruby_groups(active_rubies, line, intervals)
    if glyphs_by_index is None:
        glyphs_by_index = [None for _ in line.chars]
    for index, (ch, width) in enumerate(zip(line.chars, char_widths)):
        if index >= len(intervals) or index >= len(char_x_ranges):
            continue
        layout_glyph = (
            glyphs_by_index[index] if index < len(glyphs_by_index) else None
        )
        if layout_glyph is not None and _glyph_is_bitmap_guide(layout_glyph):
            _paint_bitmap_guide_transition_glyph(
                painter,
                layout_glyph,
                fill_segments or [],
                baseline_y,
                line,
                intervals,
                index,
                t_ms,
                transition,
                style,
                rtl=rtl,
            )
            continue
        group = _utopia_main_group_for_index(active_rubies, line, intervals, index, groups=ruby_groups) if transition.effect == "utopia" else None
        group_done_ms: int | None = None
        if group is not None:
            group_indices, _group_ruby = group
            group_done_ms = _utopia_following_done_time(line, intervals, group_indices[-1], style)
        indices = [index]
        group_ruby = None

        left = min(char_x_ranges[i][0] for i in indices)
        right = max(char_x_ranges[i][1] for i in indices)
        width = max(right - left, 1)
        first_index = indices[0]
        last_index = indices[-1]
        char_start, char_end = _utopia_wipe_window_for_index(
            line,
            intervals,
            fill_ranges,
            ruby_groups,
            index,
            style,
            fallback_start=intervals[first_index][0],
            fallback_end=intervals[last_index][1],
        )
        following_done_ms = (
            group_done_ms
            if group_done_ms is not None
            else _utopia_following_done_time(line, intervals, last_index, style)
            if transition.effect == "utopia"
            else None
        )
        opacity, dx, dy, rotation, scale_x, scale_y, skew_y = _transition_char_state(
            style,
            transition,
            first_index,
            count,
            char_start_ms=char_start,
            char_end_ms=char_end,
            t_ms=t_ms,
            frame_height=painter.device().height(),
            following_done_ms=following_done_ms,
        )
        if opacity <= 0.0:
            continue

        # utopia 退场阶段整词早已唱完：强制 fill_ratio=1.0。否则 _paint_char_karaoke_stack 会按
        # 设备空间的水平带裁切「已唱(after)层」，而退场时字形已被旋转/翻转（rotation 最大 -180°、
        # x_flip），水平带与字形朝向脱钩，会把部分笔画的着色裁掉（着色被褪掉一部分的 bug）。
        # 退场时卡拉ok扫光本无意义，整词应作为「已唱」整体淡出/旋出。
        in_utopia_exit = (
            transition.effect == "utopia"
            and style.exit_anim == "utopia"
            and following_done_ms is not None
            and t_ms > following_done_ms
        )
        if in_utopia_exit:
            fill_ratio = 1.0
        elif group_ruby is not None:
            fill_ratio = _main_text_ruby_progress_ratio(
                group_ruby, t_ms, mode=style.ruby_main_progress_mode
            )
        else:
            fill_ratio = _character_fill_ratio(
                line,
                intervals,
                fill_ranges,
                active_rubies,
                index,
                t_ms,
                groups=ruby_groups,
                ruby_main_progress_mode=style.ruby_main_progress_mode,
            )

        path = QPainterPath()
        for char_index in indices:
            layout_glyph = glyphs_by_index[char_index] if char_index < len(glyphs_by_index) else None
            glyph = line.chars[char_index]
            if layout_glyph is not None:
                path.addPath(_glyph_path(layout_glyph, baseline_y))
                continue
            glyph_font = layout_glyph.font if layout_glyph is not None else (font_for(glyph.text) if font_for is not None else font)
            glyph_left = layout_glyph.left if layout_glyph is not None else char_x_ranges[char_index][0]
            path_offset_x = layout_glyph.path_offset_x if layout_glyph is not None else 0.0
            if glyph.vector_glyph is not None:
                path.addPath(
                    scaled_guide_symbol_path(
                        glyph.vector_glyph,
                        pixel_size=max(int(glyph_font.pixelSize()), 1),
                        left=float(glyph_left),
                        baseline_y=float(baseline_y),
                    )
                )
            else:
                path.addText(float(glyph_left + path_offset_x), float(baseline_y), glyph_font, glyph.text)
        painter.save()
        try:
            painter.setOpacity(painter.opacity() * opacity)
            paint_path = path
            paint_rect = line_rect
            paint_left = left
            paint_width = width
            paint_clip_rect: QRectF | None = None
            glow_run: list[_GlyphLayout] | None = None
            glow_transform: QTransform | None = None
            geometry_transform: QTransform | None = None
            if transition.effect == "utopia":
                transform = _character_transform(
                    center_x=left + width / 2,
                    center_y=baseline_y - metrics.ascent() + metrics.height() / 2,
                    dx=dx,
                    dy=dy,
                    rotation=rotation,
                    scale_x=scale_x,
                    scale_y=scale_y,
                    skew_y=skew_y,
                    scale_origin_x=left,
                    scale_origin_y=baseline_y,
                )
                paint_path = transform.map(path)
                paint_rect = paint_path.boundingRect()
                paint_left, paint_width = _n3_transformed_wipe_span(
                    paint_path, style.stroke_width_px
                )
                paint_clip_rect = paint_rect
                geometry_transform = transform
                # 上正 glyph 列表：bake 路径与 A3 glow 缓存共用。
                group_glyphs = []
                for ci in indices:
                    layout_glyph = glyphs_by_index[ci] if ci < len(glyphs_by_index) else None
                    if layout_glyph is not None:
                        group_glyphs.append(layout_glyph)
                        continue
                    group_glyphs.append(
                        _GlyphLayout(
                            index=ci,
                            text=line.chars[ci].text,
                            role_label=None,
                            style=style,
                            font=(font_for(line.chars[ci].text) if font_for is not None else font),
                            metrics=metrics,
                            left=char_x_ranges[ci][0],
                            width=char_x_ranges[ci][1] - char_x_ranges[ci][0],
                            vector_glyph=line.chars[ci].vector_glyph,
                        )
                    )
                if _glow_cache_enabled():
                    glow_run = group_glyphs
                    glow_transform = transform
            else:
                _apply_character_transform(
                    painter,
                    center_x=left + width / 2,
                    center_y=baseline_y - metrics.ascent() + metrics.height() / 2,
                    dx=dx,
                    dy=dy,
                    rotation=rotation,
                    scale_x=scale_x,
                    scale_y=scale_y,
                    skew_y=skew_y,
                )
            _paint_char_karaoke_stack(
                painter,
                paint_path,
                paint_rect,
                char_x=paint_left,
                char_width=paint_width,
                baseline_y=baseline_y,
                metrics=metrics,
                colors=colors,
                style=style,
                ratio=fill_ratio,
                rtl=rtl,
                clip_rect=paint_clip_rect,
                glow_run=glow_run,
                glow_transform=glow_transform,
                geometry_transform=geometry_transform,
                fill_rect=fill_rect,
            )
        finally:
            painter.restore()


def _utopia_main_group_for_index(
    rubies: list[RubyAnnotation],
    line: TimingLine,
    intervals: list[tuple[int, int]],
    index: int,
    *,
    groups: dict[int, tuple[list[int], RubyAnnotation]] | None = None,
) -> tuple[list[int], RubyAnnotation] | None:
    # groups 由 _resolve_char_ruby_groups 预建（每行一次）；缺省回退逐字查找。
    if groups is not None:
        entry = groups.get(index)
        if entry is None:
            return None
        raw_indices, ruby = entry
    else:
        ruby = _ruby_for_char_index(rubies, line, intervals, index)
        if ruby is None:
            return None
        raw_indices = _ruby_target_indices(ruby, line, intervals)
    indices = [candidate for candidate in raw_indices if 0 <= candidate < len(line.chars)]
    if len(indices) <= 1:
        return None
    return indices, ruby


def _utopia_wipe_window_for_index(
    line: TimingLine,
    intervals: list[tuple[int, int]],
    char_x_ranges: list[tuple[int, int]],
    groups: dict[int, tuple[list[int], RubyAnnotation]],
    index: int,
    style: Style,
    *,
    fallback_start: int,
    fallback_end: int,
) -> tuple[int, int]:
    if effective_karaoke_animation(style) != "utopia":
        return fallback_start, fallback_end
    entry = groups.get(index)
    if entry is None:
        return fallback_start, fallback_end
    raw_indices, ruby = entry
    if _is_utopia_group_marker(ruby):
        return fallback_start, fallback_end
    indices = [candidate for candidate in raw_indices if 0 <= candidate < len(char_x_ranges)]
    if index not in indices or _ruby_main_uses_base_timing(line, indices):
        return fallback_start, fallback_end

    effective_ruby = _effective_ruby_for_target(ruby, indices, intervals)
    if (
        style.ruby_main_progress_mode == "reading_units"
        and _ruby_visual_units_and_intervals(effective_ruby)
    ):
        base_index = indices.index(index)
        return _ruby_main_text_slot_times(effective_ruby, base_index, len(indices))

    group_left = min(char_x_ranges[candidate][0] for candidate in indices)
    group_right = max(char_x_ranges[candidate][1] for candidate in indices)
    if group_right <= group_left:
        return fallback_start, fallback_end
    char_left, char_right = char_x_ranges[index]
    group_width = group_right - group_left
    start_ratio = (char_left - group_left) / group_width
    end_ratio = (char_right - group_left) / group_width
    start = _main_text_ruby_progress_time_at_ratio(
        effective_ruby,
        start_ratio,
        mode=style.ruby_main_progress_mode,
        plateau_side="right",
    )
    end = _main_text_ruby_progress_time_at_ratio(
        effective_ruby,
        end_ratio,
        mode=style.ruby_main_progress_mode,
        plateau_side="left",
    )
    return start, max(start, end)


def _transition_char_state(
    style: Style,
    transition: _LineCharTransition,
    index: int,
    count: int,
    *,
    char_start_ms: int | None = None,
    char_end_ms: int | None = None,
    t_ms: int | None = None,
    frame_height: int | None = None,
    following_done_ms: int | None = None,
) -> tuple[float, float, float, float, float, float, float]:
    if transition.effect == "utopia" and transition.phase == "utopia":
        if (
            style.entry_anim == "utopia"
            and t_ms is not None
            and transition.start_ms is not None
            and t_ms <= transition.start_ms + _UTOPIA_INTRO_TIME_MS
        ):
            intro_transition = _LineCharTransition(
                phase="entry",
                effect="utopia",
                progress=_clamped_ratio(t_ms - transition.start_ms, _UTOPIA_INTRO_TIME_MS),
                start_ms=transition.start_ms,
                end_ms=transition.start_ms + _UTOPIA_INTRO_TIME_MS,
            )
            return _transition_char_state(
                style,
                intro_transition,
                index,
                count,
                char_start_ms=char_start_ms,
                char_end_ms=char_end_ms,
                t_ms=t_ms,
                frame_height=frame_height,
                following_done_ms=following_done_ms,
            )
        if (
            style.exit_anim == "utopia"
            and t_ms is not None
            and following_done_ms is not None
            and t_ms > following_done_ms
        ):
            outro_transition = _LineCharTransition(phase="exit", effect="utopia", progress=1.0)
            return _transition_char_state(
                style,
                outro_transition,
                index,
                count,
                char_start_ms=char_start_ms,
                char_end_ms=char_end_ms,
                t_ms=t_ms,
                frame_height=frame_height,
                following_done_ms=following_done_ms,
            )
        if (
            effective_karaoke_animation(style) == "utopia"
            and t_ms is not None
            and char_start_ms is not None
            and char_end_ms is not None
            and _is_utopia_wiping(t_ms, char_start_ms, char_end_ms)
        ):
            wipe_transition = _LineCharTransition(phase="wipe", effect="utopia", progress=1.0)
            return _transition_char_state(
                style,
                wipe_transition,
                index,
                count,
                char_start_ms=char_start_ms,
                char_end_ms=char_end_ms,
                t_ms=t_ms,
                frame_height=frame_height,
                following_done_ms=following_done_ms,
            )
        return 1.0, 0.0, 0.0, 0.0, 1.0, 1.0, 0.0

    if transition.effect == "utopia" and transition.phase == "entry":
        if t_ms is None or transition.start_ms is None:
            local = _staggered_char_progress(transition.progress, index, count)
            opacity = min(max(local, 0.0), 1.0)
            return opacity, 0.0, 0.0, 0.0, opacity, opacity, 0.0
        delay = _utopia_intro_delay_step(count) * index
        elapsed = t_ms - transition.start_ms - delay
        if elapsed < 0:
            return 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0
        opacity = min(elapsed / _UTOPIA_INTRO_ENLARGE_MS, 1.0)
        if elapsed < _UTOPIA_INTRO_ENLARGE_MS:
            scale = _UTOPIA_INTRO_OVER_RATIO * elapsed / _UTOPIA_INTRO_ENLARGE_MS
        elif elapsed < _UTOPIA_INTRO_ENLARGE_MS + _UTOPIA_INTRO_CONDENSE_MS:
            remaining = _UTOPIA_INTRO_ENLARGE_MS + _UTOPIA_INTRO_CONDENSE_MS - elapsed
            scale = 1.0 + (_UTOPIA_INTRO_OVER_RATIO - 1.0) * remaining / _UTOPIA_INTRO_CONDENSE_MS
        else:
            scale = 1.0
        return opacity, 0.0, 0.0, 0.0, scale, scale, 0.0

    if transition.phase == "exit" and transition.effect == "utopia":
        if t_ms is None:
            local = transition.progress
        else:
            done_ms = following_done_ms if following_done_ms is not None else char_end_ms
            if done_ms is None:
                local = transition.progress
            else:
                local = (t_ms - done_ms) / _UTOPIA_FADE_OUT_TIME_MS
        local = min(max(local, 0.0), 1.0)
        opacity = max(0.0, 1.0 - local)
        shrink = 1.0 - local
        height = frame_height if frame_height and frame_height > 0 else 1080
        amp = height / 15.0
        if local <= 0.5:
            x_travel = math.sin(math.pi * local) * amp
        else:
            x_travel = amp + math.sin((local - 0.5) * math.pi) * amp
        y_travel = math.sin(math.pi * local / 2.0) * amp
        x_flip = math.cos(math.pi * local)
        rotation = -180.0 * local
        return opacity, -x_travel, y_travel, rotation, shrink * x_flip, shrink, 0.0

    if transition.phase == "wipe" and transition.effect == "utopia":
        if char_start_ms is None or char_end_ms is None or t_ms is None:
            return 1.0, 0.0, 0.0, 0.0, 1.0, 1.0, 0.0
        scale = _utopia_wipe_scale(t_ms, char_start_ms, char_end_ms)
        return 1.0, 0.0, 0.0, 0.0, scale, scale, 0.0

    if transition.effect in {"char_fade", "char_drip", "spin_flip"}:
        progress = _char_fade_opacity(
            transition,
            index,
            count,
            t_ms=t_ms,
        )
        if transition.effect == "spin_flip":
            direction = 1.0 if transition.phase == "exit" else -1.0
            skew_y = direction * _spin_flip_skew(progress)
            return progress, 0.0, 0.0, 0.0, progress, progress, skew_y
        if transition.effect == "char_drip":
            direction = 1.0 if transition.phase == "entry" else -1.0
            skew_y = direction * _spin_flip_skew(progress)
            return float(progress > 0.0), 0.0, 0.0, 0.0, 1.0, 1.0, skew_y
        return progress, 0.0, 0.0, 0.0, 1.0, 1.0, 0.0

    local = _staggered_char_progress(transition.progress, index, count)
    eased = 1.0 - (1.0 - local) * (1.0 - local)
    if transition.phase == "entry":
        opacity = 0.22 + 0.78 * eased
        return opacity, 0.0, 0.0, 0.0, 1.0, 1.0, 0.0

    opacity = 1.0 - eased
    if transition.effect == "utopia":
        return 0.0, 0.0, 0.0, 0.0, 1.0, 1.0, 0.0
    return opacity, 0.0, 0.0, 0.0, 1.0, 1.0, 0.0


def _apply_character_transform(
    painter: QPainter,
    *,
    center_x: float,
    center_y: float,
    dx: float,
    dy: float,
    rotation: float,
    scale_x: float = 1.0,
    scale_y: float = 1.0,
    skew_y: float = 0.0,
    scale_origin_x: float | None = None,
    scale_origin_y: float | None = None,
) -> None:
    transform = _character_transform(
        center_x=center_x,
        center_y=center_y,
        dx=dx,
        dy=dy,
        rotation=rotation,
        scale_x=scale_x,
        scale_y=scale_y,
        skew_y=skew_y,
        scale_origin_x=scale_origin_x,
        scale_origin_y=scale_origin_y,
    )
    if transform.isIdentity():
        return
    painter.setTransform(transform, combine=True)


def _character_transform(
    *,
    center_x: float,
    center_y: float,
    dx: float = 0.0,
    dy: float = 0.0,
    rotation: float = 0.0,
    scale_x: float = 1.0,
    scale_y: float = 1.0,
    skew_y: float = 0.0,
    scale_origin_x: float | None = None,
    scale_origin_y: float | None = None,
) -> QTransform:
    transform = QTransform()
    if not dx and not dy and not rotation and scale_x == 1.0 and scale_y == 1.0 and not skew_y:
        return transform
    if scale_origin_x is not None and scale_origin_y is not None:
        transform.translate(scale_origin_x + dx, scale_origin_y + dy)
        if skew_y:
            transform.shear(0.0, skew_y)
        if scale_x != 1.0 or scale_y != 1.0:
            transform.scale(scale_x, scale_y)
        transform.translate(center_x - scale_origin_x, center_y - scale_origin_y)
        if rotation:
            transform.rotate(rotation)
        transform.translate(-center_x, -center_y)
        return transform
    transform.translate(center_x + dx, center_y + dy)
    if rotation:
        transform.rotate(rotation)
    if skew_y:
        transform.shear(0.0, skew_y)
    if scale_x != 1.0 or scale_y != 1.0:
        transform.scale(scale_x, scale_y)
    transform.translate(-center_x, -center_y)
    return transform


def _utopia_intro_delay_step(count: int) -> int:
    if count <= 1:
        return 0
    return _UTOPIA_INTRO_DELAY_MS // (count - 1)


def _is_utopia_wiping(t_ms: int, char_start_ms: int, char_end_ms: int) -> bool:
    return char_start_ms < t_ms < char_end_ms and char_start_ms != char_end_ms


def _utopia_wipe_scale(t_ms: int, char_start_ms: int, char_end_ms: int) -> float:
    if not _is_utopia_wiping(t_ms, char_start_ms, char_end_ms):
        return 1.0
    over_ms = min(int((char_end_ms - char_start_ms) * _UTOPIA_WIPE_OVER_TIME_RATIO), _UTOPIA_WIPE_OVER_TIME_LIMIT_MS)
    if over_ms <= 0:
        return 1.0
    peak_ms = char_start_ms + over_ms
    if t_ms <= peak_ms:
        progress = (t_ms - char_start_ms) / over_ms
    else:
        release_ms = max(char_end_ms - peak_ms, 1)
        progress = (char_end_ms - t_ms) / release_ms
    return 1.0 + (_UTOPIA_WIPE_OVER_RATIO - 1.0) * min(max(progress, 0.0), 1.0)


def _utopia_following_done_time(
    line: TimingLine,
    intervals: list[tuple[int, int]],
    index: int,
    style: Style,
) -> int:
    if not intervals:
        return _line_end_ms(line)
    index = min(max(index, 0), len(intervals) - 1)
    current_end = intervals[index][1]
    next_index = _next_valid_char_index(line, index + 1)
    if next_index is not None and next_index < len(intervals):
        next_end = intervals[next_index][1]
        if current_end <= next_end:
            return next_end
    return current_end + _utopia_tail_delay_ms(style)


def _next_valid_char_index(line: TimingLine, start_index: int) -> int | None:
    for index in range(start_index, len(line.chars)):
        text = line.chars[index].text
        if text and not text.isspace():
            return index
    return None


def _utopia_tail_delay_ms(style: Style) -> int:
    return max(0, style.line_tail_ms - _UTOPIA_FADE_OUT_TIME_MS)


def _char_fade_delay_step(count: int) -> int:
    if count <= 1:
        return 0
    return _CHAR_FADE_INTRO_DELAY_MS // (count - 1)


def _char_fade_opacity(
    transition: _LineCharTransition,
    index: int,
    count: int,
    *,
    t_ms: int | None,
) -> float:
    if t_ms is None:
        return transition.progress
    if transition.phase == "entry":
        start_ms = (transition.start_ms or 0) + _char_fade_delay_step(count) * index
        return _clamped_ratio(t_ms - start_ms, _CHAR_FADE_IN_TIME_MS)
    if transition.phase == "exit":
        end_ms = (transition.end_ms or t_ms) - _char_fade_delay_step(count) * (count - index - 1)
        if t_ms > end_ms:
            return 0.0
        if t_ms < end_ms - _CHAR_FADE_OUT_TIME_MS:
            return 1.0
        return _clamped_ratio(end_ms - t_ms, _CHAR_FADE_OUT_TIME_MS)
    return 1.0


def _spin_flip_skew(opacity: float) -> float:
    opacity = max(0.0, min(1.0, opacity))
    if opacity <= 0.0:
        return 0.0
    angle = (math.pi / 2.0) * (1.0 - opacity)
    return math.tan(min(angle, math.radians(89.0)))


def _paint_char_karaoke_stack(
    painter: QPainter,
    path: QPainterPath,
    rect: QRectF,
    *,
    char_x: int,
    char_width: int,
    baseline_y: int,
    metrics: QFontMetrics,
    colors: KaraokeColors,
    style: Style,
    ratio: float,
    rtl: bool = False,
    clip_rect: QRectF | None = None,
    glow_run: list[_GlyphLayout] | None = None,
    glow_transform: QTransform | None = None,
    geometry_transform: QTransform | None = None,
    fill_rect: QRectF | None = None,
) -> None:
    # A3（§9.7）：``glow_run`` 给定（utopia 路径）时，glow 走上正烘焙缓存 + 变换 blit，
    # 不再每帧 _paint_glow_path 重算高斯；body 仍逐帧矢量（锐利）。``glow_run`` 为 None
    # 时退回原逐帧 glow 路径（保留旧行为，可回退）。
    def _use_cached_glow(after: bool) -> bool:
        del after
        # N3 transforms the glyph geometry before drawing the shared
        # decoration source and applying Gaussian blur.  Reusing an already
        # blurred upright bitmap would also scale/rotate the blur kernel,
        # producing a visibly different Utopia outline during entry, wipe and
        # outro.  Keep the cache for identity frames; transformed frames must
        # blur the transformed source.
        return (
            glow_run is not None
            and style.decoration_kind == "glow"
            and (
                geometry_transform is None
                or geometry_transform.isIdentity()
            )
        )
    stroke2_width = _main_stroke2_width(style)

    def _blit_glow(after: bool) -> None:
        _blit_cached_run_glow(
            painter, glow_run, baseline_y, style, colors,
            after=after, transform=glow_transform, fill_rect=fill_rect,
        )

    if ratio <= 0.0:
        use_cached_glow = _use_cached_glow(after=False)
        if use_cached_glow:
            _blit_glow(after=False)
        _paint_text_layer_stack(
            painter,
            path,
            rect,
            colors.before,
            style,
            stroke_width=style.stroke_width_px,
            stroke2_width=stroke2_width,
            shadow_dx=style.shadow_offset_x,
            shadow_dy=style.shadow_offset_y,
            glow_radius=_glow_radius(style, after=False),
            draw_glow=not use_cached_glow,
            fill_rect=fill_rect,
        )
        return

    if ratio < 1.0:
        clip_bounds = clip_rect if clip_rect is not None else QRectF(
            float(char_x),
            float(baseline_y - metrics.ascent()),
            float(char_width),
            float(metrics.height()),
        )
        glow_states_differ = _karaoke_glow_states_differ(style, colors)
        use_cached_before_glow = (
            _use_cached_glow(after=False) and not glow_states_differ
        )
        front = char_x + char_width * (1.0 - ratio if rtl else ratio)
        if glow_states_differ:
            transformed_geometry = (
                geometry_transform is not None
                and not geometry_transform.isIdentity()
            )
            if transformed_geometry:
                _paint_full_glow_source_wipe(
                    painter,
                    path,
                    clip_bounds,
                    style,
                    colors,
                    front=front,
                    rtl=rtl,
                    fill_rect=fill_rect,
                )
            elif glow_run is not None and _glow_cache_enabled():
                combined = _paint_cached_run_split_glow_source_wipe(
                    painter,
                    path,
                    clip_bounds,
                    glow_run,
                    baseline_y,
                    style,
                    colors,
                    front=front,
                    rtl=rtl,
                    transform=glow_transform,
                    fill_rect=fill_rect,
                )
                if not combined:
                    for after in (False, True):
                        _paint_cached_run_glow_source_wipe(
                            painter,
                            path,
                            clip_bounds,
                            glow_run,
                            baseline_y,
                            style,
                            colors,
                            after=after,
                            front=front,
                            rtl=rtl,
                            transform=glow_transform,
                            fill_rect=fill_rect,
                        )
            else:
                _paint_full_glow_source_wipe(
                    painter,
                    path,
                    clip_bounds,
                    style,
                    colors,
                    front=front,
                    rtl=rtl,
                    fill_rect=fill_rect,
                )
        elif use_cached_before_glow:
            _blit_glow(after=False)
        utopia_shadow_split = (
            geometry_transform is not None
            and style.decoration_kind == "shadow"
            and bool(style.shadow_offset_x or style.shadow_offset_y)
        )
        if utopia_shadow_split:
            shadow_front = front + style.shadow_offset_x
            shadow_states_differ = (
                _fill_signature(colors.before.shadow)
                != _fill_signature(colors.after.shadow)
            )
            if not shadow_states_differ:
                _paint_shadow_silhouette(
                    painter,
                    path,
                    colors.before.shadow,
                    fill_rect if fill_rect is not None else rect,
                    style.shadow_offset_x,
                    style.shadow_offset_y,
                    style.stroke_width_px,
                    stroke2_width,
                )
            else:
                for after in (False, True):
                    source_is_right = rtl == after
                    output_clip = (
                        QRectF(
                            shadow_front,
                            -1_000_000.0,
                            1_000_000.0,
                            2_000_000.0,
                        )
                        if source_is_right
                        else QRectF(
                            -1_000_000.0,
                            -1_000_000.0,
                            shadow_front + 1_000_000.0,
                            2_000_000.0,
                        )
                    )
                    painter.save()
                    try:
                        painter.setClipRect(output_clip)
                        state = colors.after if after else colors.before
                        _paint_shadow_silhouette(
                            painter,
                            path,
                            state.shadow,
                            fill_rect if fill_rect is not None else rect,
                            style.shadow_offset_x,
                            style.shadow_offset_y,
                            style.stroke_width_px,
                            stroke2_width,
                        )
                    finally:
                        painter.restore()
        _paint_text_layer_stack(
            painter,
            path,
            rect,
            colors.before,
            style,
            stroke_width=style.stroke_width_px,
            stroke2_width=stroke2_width,
            shadow_dx=style.shadow_offset_x,
            shadow_dy=style.shadow_offset_y,
            glow_radius=_glow_radius(style, after=False),
            draw_glow=not use_cached_before_glow and not glow_states_differ,
            fill_rect=fill_rect,
            draw_shadow=not utopia_shadow_split,
        )
        stroke_pad = _visual_text_padding(style)
        # RTL：单字内扫光从右向左，已唱区贴字符右缘。
        clip_x = char_x + (char_width * (1.0 - ratio) if rtl else 0.0)
        # 已唱描边 + 填充：保持卡拉ok 走字的硬边（按字框紧裁），发光已单独画过。
        painter.save()
        try:
            painter.setClipRect(
                QRectF(
                    float(clip_x - stroke_pad),
                    float(clip_bounds.top() - stroke_pad),
                    float(char_width * ratio + stroke_pad),
                    float(clip_bounds.height() + stroke_pad * 2),
                )
            )
            _paint_text_layer_stack(
                painter,
                path,
                rect,
                colors.after,
                style,
                stroke_width=style.stroke_width_px,
                stroke2_width=stroke2_width,
                shadow_dx=style.shadow_offset_x,
                shadow_dy=style.shadow_offset_y,
                glow_radius=_glow_radius(style, after=True),
                draw_glow=False,
                fill_rect=fill_rect,
                draw_shadow=not utopia_shadow_split,
            )
        finally:
            painter.restore()
        return

    use_cached_glow = _use_cached_glow(after=True)
    if use_cached_glow:
        _blit_glow(after=True)
    _paint_text_layer_stack(
        painter,
        path,
        rect,
        colors.after,
        style,
        stroke_width=style.stroke_width_px,
        stroke2_width=stroke2_width,
        shadow_dx=style.shadow_offset_x,
        shadow_dy=style.shadow_offset_y,
        glow_radius=_glow_radius(style, after=True),
        draw_glow=not use_cached_glow,
        fill_rect=fill_rect,
    )


def _staggered_char_progress(progress: float, index: int, count: int) -> float:
    if count <= 1:
        return progress
    span = 0.68
    window = 1.0 - span
    offset = (index / max(count - 1, 1)) * span
    return max(0.0, min(1.0, (progress - offset) / window))


def _clamped_ratio(elapsed_ms: int, duration_ms: int) -> float:
    if duration_ms <= 0:
        return 1.0
    return max(0.0, min(1.0, elapsed_ms / duration_ms))


def _paint_glow_path(
    painter: QPainter,
    path: QPainterPath,
    fill: PaintFill,
    rect: QRectF,
    radius: int,
    stroke_width: int,
    stroke2_width: int,
    source_clip: QRectF | None = None,
    concentration_level: int = 0,
    target_clip: QRectF | None = None,
) -> None:
    if normalize_glow_concentration_level(concentration_level) < 0:
        return
    radius = max(int(radius), 0)
    if radius == 0:
        return
    width = _glow_pen_width(stroke_width, stroke2_width, radius)
    bounds = path.boundingRect()
    if bounds.isEmpty():
        return
    pad = _glow_extent(stroke_width, stroke2_width, radius) + 2
    layer_rect = bounds.adjusted(-pad, -pad, pad, pad)
    if target_clip is not None:
        # 调用方只消费 target_clip 内的输出：把 stroke/blur 画布水平裁到
        # target ± pad（pad ≥ 模糊支撑半径），窄带模糊代替整行模糊。裁剪量取整，
        # 保留 layer_rect 原有的小数相位——drawImage 的亚像素重采样必须与整行
        # 路径逐位一致，否则扫光前沿的陡坡会产生半像素偏移。
        needed_left = float(target_clip.left()) - pad
        needed_right = float(target_clip.right()) + pad
        if needed_left > layer_rect.left():
            layer_rect.setLeft(layer_rect.left() + math.floor(needed_left - layer_rect.left()))
        if needed_right < layer_rect.right():
            layer_rect.setRight(layer_rect.right() - math.floor(layer_rect.right() - needed_right))
        if layer_rect.isEmpty():
            return
    image_w = max(1, math.ceil(layer_rect.width()))
    image_h = max(1, math.ceil(layer_rect.height()))
    source = QImage(image_w, image_h, QImage.Format.Format_ARGB32_Premultiplied)
    source.fill(0)

    local_path = QPainterPath(path)
    local_path.translate(-layer_rect.left(), -layer_rect.top())
    local_rect = rect.translated(-layer_rect.left(), -layer_rect.top())
    p = QPainter(source)
    try:
        p.setRenderHints(
            QPainter.RenderHint.Antialiasing
            | QPainter.RenderHint.TextAntialiasing
            | QPainter.RenderHint.SmoothPixmapTransform
        )
        if source_clip is not None:
            p.setClipRect(source_clip.translated(-layer_rect.left(), -layer_rect.top()))
        _paint_stroke_path(p, local_path, fill, local_rect, width)
    finally:
        p.end()

    target = QPointF(layer_rect.left(), layer_rect.top())
    painter.save()
    try:
        if target_clip is not None:
            painter.setClipRect(target_clip)
        for blur_radius in _glow_blur_radii(radius, concentration_level):
            painter.drawImage(target, _blur_image(source, blur_radius))
    finally:
        painter.restore()


def _paint_split_glow_path(
    painter: QPainter,
    path: QPainterPath,
    before_fill: PaintFill,
    after_fill: PaintFill,
    rect: QRectF,
    radius: int,
    stroke_width: int,
    stroke2_width: int,
    *,
    before_source_clip: QRectF,
    after_source_clip: QRectF,
    concentration_level: int = 0,
    target_clip: QRectF | None = None,
    horizontal_fill_rect: QRectF | None = None,
) -> None:
    """Paint both WipeLeft source colours into one bitmap, then blur once."""
    if normalize_glow_concentration_level(concentration_level) < 0:
        return
    radius = max(int(radius), 0)
    if radius == 0:
        return
    width = _glow_pen_width(stroke_width, stroke2_width, radius)
    bounds = path.boundingRect()
    if bounds.isEmpty():
        return
    pad = _glow_extent(stroke_width, stroke2_width, radius) + 2
    layer_rect = bounds.adjusted(-pad, -pad, pad, pad)
    if target_clip is not None:
        needed_left = float(target_clip.left()) - pad
        needed_right = float(target_clip.right()) + pad
        if needed_left > layer_rect.left():
            layer_rect.setLeft(
                layer_rect.left() + math.floor(needed_left - layer_rect.left())
            )
        if needed_right < layer_rect.right():
            layer_rect.setRight(
                layer_rect.right()
                - math.floor(layer_rect.right() - needed_right)
            )
        if layer_rect.isEmpty():
            return
    image_w = max(1, math.ceil(layer_rect.width()))
    image_h = max(1, math.ceil(layer_rect.height()))
    source = QImage(image_w, image_h, QImage.Format.Format_ARGB32_Premultiplied)
    source.fill(0)
    local_path = QPainterPath(path)
    local_path.translate(-layer_rect.left(), -layer_rect.top())
    local_rect = rect.translated(-layer_rect.left(), -layer_rect.top())
    local_horizontal_rect = (
        horizontal_fill_rect.translated(-layer_rect.left(), -layer_rect.top())
        if horizontal_fill_rect is not None
        else None
    )
    source_painter = QPainter(source)
    try:
        source_painter.setRenderHints(
            QPainter.RenderHint.Antialiasing
            | QPainter.RenderHint.TextAntialiasing
            | QPainter.RenderHint.SmoothPixmapTransform
        )
        for fill, clip in (
            (before_fill, before_source_clip),
            (after_fill, after_source_clip),
        ):
            source_painter.save()
            try:
                source_painter.setClipRect(
                    clip.translated(-layer_rect.left(), -layer_rect.top())
                )
                _paint_stroke_path(
                    source_painter,
                    local_path,
                    fill,
                    _fill_brush_rect(fill, local_rect, local_horizontal_rect),
                    width,
                )
            finally:
                source_painter.restore()
    finally:
        source_painter.end()
    target = QPointF(layer_rect.left(), layer_rect.top())
    painter.save()
    try:
        if target_clip is not None:
            painter.setClipRect(target_clip)
        for blur_radius in _glow_blur_radii(radius, concentration_level):
            painter.drawImage(target, _blur_image(source, blur_radius))
    finally:
        painter.restore()




def _paint_after_fill_path(
    painter: QPainter,
    path: QPainterPath,
    fill: PaintFill,
    rect: QRectF,
    fill_segments: list[_FillSegment],
    y: int,
    metrics: QFontMetrics,
    t_ms: int,
    rtl: bool = False,
) -> None:
    _paint_after_path(
        painter, path, fill, rect, None, fill_segments, y, metrics, t_ms, rtl
    )


def _paint_after_stroke_path(
    painter: QPainter,
    path: QPainterPath,
    fill: PaintFill,
    rect: QRectF,
    width: int,
    fill_segments: list[_FillSegment],
    y: int,
    metrics: QFontMetrics,
    t_ms: int,
    rtl: bool = False,
) -> None:
    _paint_after_path(
        painter, path, fill, rect, width, fill_segments, y, metrics, t_ms, rtl
    )


def _paint_after_path(
    painter: QPainter,
    path: QPainterPath,
    fill: PaintFill,
    rect: QRectF,
    stroke_width: int | None,
    fill_segments: list[_FillSegment],
    y: int,
    metrics: QFontMetrics,
    t_ms: int,
    rtl: bool = False,
) -> None:
    # 卡拉ok填色是连续扫光，已唱字符总是连续从一侧开始；把 N 个相邻 char
    # clip 合并成单 clip rect → 整 line path 只画一次，不再 N 次重复绘制。
    band = _fill_clip_band(fill_segments, t_ms, rtl)
    if band is None:
        return
    clip = _horizontal_after_path_clip_rect(
        fill_segments, y, metrics, t_ms, rtl, stroke_width
    )
    if clip is None:
        return
    painter.save()
    try:
        painter.setClipRect(clip)
        if stroke_width is None:
            _paint_fill_path(painter, path, fill, rect)
        else:
            _paint_stroke_path(painter, path, fill, rect, stroke_width)
    finally:
        painter.restore()


def _horizontal_after_path_clip_rect(
    fill_segments: list[_FillSegment],
    y: int,
    metrics: QFontMetrics,
    t_ms: int,
    rtl: bool,
    stroke_width: int | None,
) -> QRectF | None:
    band = _fill_clip_band(fill_segments, t_ms, rtl)
    if band is None:
        return None
    fill_start, fill_end = band
    stroke_pad = 0 if stroke_width is None else math.ceil(stroke_width / 2)
    return QRectF(
        float(fill_start - stroke_pad),
        float(y - metrics.ascent() - stroke_pad),
        float((fill_end - fill_start) + stroke_pad),
        float(metrics.height() + stroke_pad * 2),
    )


def _legacy_fill_extent_end(
    char_widths: list[int],
    intervals: list[tuple[int, int]],
    x0: int,
    t_ms: int,
) -> int:
    """Return rightmost x of the karaoke-filled extent at ``t_ms``.

    卡拉ok填色按字符顺序左→右推进，给定 ``t_ms`` 时一定形如
    "前 k 个字符全填 + 第 k+1 个字符部分填 + 之后全空"。本函数返回填色
    末端的 x 坐标；与 ``x0`` 相等表示当前没有字符被填到（直接早退）。
    """
    fill_end = x0
    cursor_x = x0
    for w, (cs, ce) in zip(char_widths, intervals):
        ratio = char_fill_ratio(cs, ce, t_ms)
        if ratio <= 0.0:
            break
        if ratio >= 1.0:
            cursor_x += w
            fill_end = cursor_x
            continue
        # 部分填色——也是最后一个被填到的字符
        fill_end = cursor_x + int(round(w * ratio))
        break
    return fill_end


def _char_ink_x_ranges(
    texts: list[str],
    fonts: list[QFont],
    char_lefts: list[int],
    char_path_offsets: list[float] | None = None,
) -> list[tuple[int, int]]:
    """每个字符的墨水水平边界（绝对坐标 ``(ink_left, ink_right)``）。

    走字（卡拉ok 扫光）严格按字形**墨水**推进，而非按 advance 框。advance 含字形
    左右两侧的 side bearing 与字间空隙，纯按 advance 走会让扫光锋面与字形墨水错位
    （字头偏慢——锋面停在左侧空白上墨水迟迟不染；字尾悬空——墨水早已染满而锋面还在
    右侧空白里推进）。这里用 ``QPainterPath.addText`` 的矢量包围盒取墨水边界：与实际
    ``fillPath`` 绘制同源、与 DPR/点阵 strike 无关。空白字符无墨水 → 零宽 ``(left, left)``。
    与 SUG ``karaoke_preview.py`` 的 ``_ink_bounds``（``tightBoundingRect``）同口径。
    """
    if char_path_offsets is None:
        char_path_offsets = [0.0 for _ in char_lefts]
    ranges: list[tuple[int, int]] = []
    for text, font, left, path_offset_x in zip(texts, fonts, char_lefts, char_path_offsets):
        if not text or text.isspace():
            ranges.append((left, left))
            continue
        path = QPainterPath()
        path.addText(float(left + path_offset_x), 0.0, font, text)
        br = path.boundingRect()
        if br.isEmpty():
            ranges.append((left, left))
        else:
            ranges.append((int(math.floor(br.left())), int(math.ceil(br.right()))))
    return ranges


def _karaoke_fill_segments(
    char_widths: list[int],
    intervals: list[tuple[int, int]],
    ink_x_ranges: list[tuple[int, int]],
    active_rubies: list[RubyAnnotation],
    line: TimingLine,
    *,
    release_x_ranges: list[tuple[int, int]] | None = None,
    layout_x_ranges: list[tuple[int, int]] | None = None,
    ruby_main_progress_mode: str = "checkpoint_segments",
) -> list[_FillSegment]:
    """构造走字分段。``ink_x_ranges`` 为各字符的墨水边界（非 advance 框），
    扫光锋面据此推进，确保不扫过字形两侧的透明空白（见 :func:`_char_ink_x_ranges`）。"""
    segments: list[_FillSegment] = []
    release_x_ranges = release_x_ranges or ink_x_ranges
    layout_x_ranges = layout_x_ranges or release_x_ranges
    index = 0
    while index < len(char_widths):
        if index < len(line.chars) and _bitmap_guide_is_no_wipe(
            line.chars[index].vector_glyph
        ):
            index += 1
            continue
        ruby = _ruby_for_char_index(active_rubies, line, intervals, index)
        ruby_indices = (
            _ruby_target_indices(ruby, line, intervals) if ruby is not None else []
        )
        # SUG uses a pause-only ruby over a linked English phrase as a
        # non-rendering group marker.  Utopia must still consume that ruby to
        # drop the whole phrase together, but it is not pronunciation data and
        # must not replace the phrase's real per-syllable TimingChar clock with
        # one linear start-to-end wipe.
        if (
            ruby is None
            or _is_utopia_group_marker(ruby)
            or _ruby_main_uses_base_timing(line, ruby_indices)
        ):
            left, right = ink_x_ranges[index]
            release_left, release_right = release_x_ranges[index]
            layout_left, layout_right = layout_x_ranges[index]
            start, end = intervals[index]
            segments.append(
                _FillSegment(
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
                _FillSegment(
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

        effective_ruby = _effective_ruby_for_target(ruby, indices, intervals)
        reading_unit_mode = (
            ruby_main_progress_mode == "reading_units"
            and bool(_ruby_visual_units_and_intervals(effective_ruby))
        )
        if reading_unit_mode:
            base_count = len(indices)
            for base_index, target_index in enumerate(indices):
                left, right = ink_x_ranges[target_index]
                release_left, release_right = release_x_ranges[target_index]
                layout_left, layout_right = layout_x_ranges[target_index]
                slot_start, slot_end = _ruby_main_text_slot_times(
                    effective_ruby, base_index, base_count
                )
                segments.append(
                    _FillSegment(
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
                _FillSegment(
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
    return _adjust_fill_release_edges(segments)


def _adjust_fill_release_edges(segments: list[_FillSegment]) -> list[_FillSegment]:
    """Apply N3 ``AdjustWipeEnd`` at overlapping character boxes.

    N3 calculates the adjusted *position ratio* in DrawLeft/DrawRight layout-box
    coordinates, then applies that ratio to the transformed ink geometry used by
    WipeLeft.  Bearings and the primary edge therefore affect the visible end
    point without changing the overlap decision itself.
    """
    adjusted = list(segments)
    for index in range(len(adjusted) - 1):
        current = adjusted[index]
        following = adjusted[index + 1]
        release_left = current.release_left if current.release_left is not None else current.left
        release_right = current.release_right if current.release_right is not None else current.right
        layout_left = current.layout_left if current.layout_left is not None else release_left
        layout_right = current.layout_right if current.layout_right is not None else release_right
        following_left = (
            following.layout_left
            if following.layout_left is not None
            else (following.release_left if following.release_left is not None else following.left)
        )
        following_right = (
            following.layout_right
            if following.layout_right is not None
            else (following.release_right if following.release_right is not None else following.right)
        )
        layout_width = max(layout_right - layout_left + 1, 1)
        if layout_left <= following_left:
            if layout_right >= following_left:
                pose = max(0.0, min(1.0, (following_left - layout_left) / layout_width))
                adjusted[index] = replace(
                    current,
                    release_right=release_left + (release_right - release_left) * pose,
                )
        elif layout_left <= following_right:
            pose = max(0.0, min(1.0, (layout_right - following_right) / layout_width))
            adjusted[index] = replace(
                current,
                release_left=release_right - (release_right - release_left) * pose,
            )
    return adjusted


def _ruby_for_char_index(
    rubies: list[RubyAnnotation],
    line: TimingLine,
    intervals: list[tuple[int, int]],
    index: int,
) -> RubyAnnotation | None:
    for ruby in rubies:
        if index in _ruby_target_indices(ruby, line, intervals):
            return ruby
    return None


def _resolve_char_ruby_groups(
    rubies: list[RubyAnnotation],
    line: TimingLine,
    intervals: list[tuple[int, int]],
) -> dict[int, tuple[list[int], RubyAnnotation]]:
    """预解析 ``char index -> (该字所属 ruby 的 target indices, ruby)``，每行算一次。

    等价于逐字调用 ``_ruby_for_char_index`` + ``_ruby_target_indices``，但这些查找是
    **布局静态**（不依赖 ``t_ms``，只取决于 rubies/line/intervals）。原本在 transition
    逐字逐帧循环里反复重算（实测每帧数百次 ``_find_ruby_text_span``/``_text_span_indices``），
    在此一次性建表。``setdefault`` 实现「rubies 顺序中首个命中者胜」，与 ``_ruby_for_char_index``
    一致；消费方（``_utopia_main_group_for_index`` / ``_character_fill_ratio``）各自对返回的
    indices 施加自己的范围过滤，故行为逐像素不变。
    """
    groups: dict[int, tuple[list[int], RubyAnnotation]] = {}
    for ruby in rubies:
        indices = _ruby_target_indices(ruby, line, intervals)
        for index in indices:
            groups.setdefault(index, (indices, ruby))
    return groups


def _ruby_main_uses_base_timing(
    line: TimingLine,
    indices: list[int],
) -> bool:
    """是否保留 ruby 目标正文已有的逐字时间边界。

    ``DrawDataGenerator.SetOneLineWipe`` 只在 ruby 组内部没有显式正文边界时调用
    ``RubyTimesToKanjiTimes``。组首的 begin 和组尾的 end 不算内部边界；任一后续正文
    字符有显式 begin，或任一非末正文字符有显式 end，整组都改用正文自己的时钟。
    """
    valid = [index for index in indices if 0 <= index < len(line.chars)]
    if len(valid) <= 1:
        return False
    last_offset = len(valid) - 1
    for offset, index in enumerate(valid):
        char = line.chars[index]
        if offset > 0 and char.explicit_start:
            return True
        if offset < last_offset and char.explicit_end:
            return True
    return False


def _offset_fill_segments(segments: list[_FillSegment], dx: int) -> list[_FillSegment]:
    if dx == 0:
        return segments
    return [
        _FillSegment(
            left=segment.left + dx,
            right=segment.right + dx,
            release_left=(
                segment.release_left + dx if segment.release_left is not None else None
            ),
            release_right=(
                segment.release_right + dx if segment.release_right is not None else None
            ),
            layout_left=(
                segment.layout_left + dx if segment.layout_left is not None else None
            ),
            layout_right=(
                segment.layout_right + dx if segment.layout_right is not None else None
            ),
            start_ms=segment.start_ms,
            end_ms=segment.end_ms,
            ruby=segment.ruby,
            indices=segment.indices,
        )
        for segment in segments
    ]


def _fill_extent_start(segments: list[_FillSegment]) -> float | None:
    if not segments:
        return None
    first = segments[0]
    return first.release_left if first.release_left is not None else first.left


def _segment_wipe_edges(segment: _FillSegment) -> tuple[float, float]:
    """Return the N3 drawing edges used by the moving wipe front.

    ``left`` / ``right`` keep the glyph-ink bounds for layout and ruby mapping,
    while ``release_*`` are the full DrawLeft/DrawRight-style bounds (including
    the primary edge).  NicoKaraMaker3 interpolates across those full drawing
    bounds for the *whole* character interval; ``AdjustWipeEnd`` only changes
    the destination edge before interpolation.  Characters without drawable
    ink (spaces) deliberately remain zero-width: their timing consumes time
    without moving the visible front.  Falling back to ink bounds keeps
    synthetic/legacy segments compatible.
    """
    if segment.right <= segment.left:
        return segment.left, segment.left
    left = (
        segment.release_left if segment.release_left is not None else segment.left
    )
    right = (
        segment.release_right if segment.release_right is not None else segment.right
    )
    return left, max(left, right)


def _segment_wipe_times(segment: _FillSegment) -> tuple[int, int]:
    """Return the effective N3 wipe window for one main-text segment."""
    if segment.ruby_base_index is not None:
        return int(segment.start_ms), int(segment.end_ms)
    if segment.ruby is not None:
        return int(segment.ruby.pos_start_ms), int(segment.ruby.pos_end_ms)
    return int(segment.start_ms), int(segment.end_ms)


def _segment_wipe_band_at(
    segment: _FillSegment,
    t_ms: int,
    rtl: bool,
) -> tuple[int, int]:
    """Return one segment's wipe band, including its zero-progress boundary."""
    wipe_left, wipe_right = _segment_wipe_edges(segment)
    ratio = _segment_fill_ratio(segment, t_ms)
    if rtl:
        boundary = wipe_right - int(round((wipe_right - wipe_left) * ratio))
        return boundary, wipe_right
    boundary = wipe_left + int(round((wipe_right - wipe_left) * ratio))
    return wipe_left, boundary


def _n3_following_wipe_band(
    segments: list[_FillSegment],
    indices: set[int],
    t_ms: int,
    rtl: bool,
) -> tuple[int, int] | None:
    """Keep a completed glyph on N3's shared front until its successor ends.

    N3 continues treating the preceding character as ``IsWiping`` while the
    following character is active.  At the exact hand-off it still uses the
    preceding segment's adjusted endpoint; afterwards it reuses the following
    segment's moving boundary.  This is what releases overlapping outlines
    continuously instead of fully opening each glyph at its own end.

    N3's implementation is left-to-right.  Keep this compatibility behavior
    scoped away from the application's independent RTL extension.
    """
    if rtl or not indices:
        return None
    positions = [
        position
        for position, segment in enumerate(segments)
        if segment.indices and any(index in indices for index in segment.indices)
    ]
    if not positions:
        return None
    current_position = max(positions)
    if current_position >= len(segments) - 1:
        return None
    current = segments[current_position]
    following = segments[current_position + 1]
    # N3 cannot reuse a following space/no-wipe glyph because it has no
    # transformed geometry.  Its WipeLeft fallback leaves the completed glyph
    # fully released instead of holding its outline/glow through the pause.
    if following.right <= following.left:
        return None
    current_start, current_end = _segment_wipe_times(current)
    _following_start, following_end = _segment_wipe_times(following)
    if not (
        current_start < t_ms < following_end
        and current_start != following_end
        and _segment_fill_ratio(current, t_ms) >= 1.0
    ):
        return None
    if t_ms <= current_end:
        return _segment_wipe_band_at(current, t_ms, rtl=False)
    return _segment_wipe_band_at(following, t_ms, rtl=False)


def _fill_extent_end(
    segments: list[_FillSegment],
    t_ms: int,
) -> float:
    """Return the current right edge of the continuous karaoke scan.

    Motion follows the N3 drawing bounds for the complete interval.  In
    particular, the destination is the AdjustWipeEnd-clamped DrawRight from
    the first partial frame onward; switching from ink ``right`` to DrawRight
    only on the completion frame causes a visible one-frame jump.
    """
    if not segments:
        return 0
    fill_end, _ = _segment_wipe_edges(segments[0])
    for segment in segments:
        ratio = _segment_fill_ratio(segment, t_ms)
        if ratio <= 0.0:
            break
        if segment.right <= segment.left:
            if ratio < 1.0:
                break
            continue
        wipe_left, wipe_right = _segment_wipe_edges(segment)
        if ratio >= 1.0:
            fill_end = max(fill_end, wipe_right)
            continue
        fill_end = max(
            fill_end,
            wipe_left + int(round((wipe_right - wipe_left) * ratio)),
        )
        break
    return fill_end


def _fill_extent_left(segments: list[_FillSegment], t_ms: int) -> float:
    """RTL：返回已唱区的左缘 x（扫光从右向左推进时的移动边）。

    句中停顿的间隙中点推进与 :func:`_fill_extent_end` 镜像。
    """
    if not segments:
        return 0
    _, scanline = _segment_wipe_edges(segments[0])
    for segment in segments:
        ratio = _segment_fill_ratio(segment, t_ms)
        if ratio <= 0.0:
            break
        if segment.right <= segment.left:
            if ratio < 1.0:
                break
            continue
        wipe_left, wipe_right = _segment_wipe_edges(segment)
        if ratio >= 1.0:
            scanline = min(scanline, wipe_left)
            continue
        scanline = min(
            scanline,
            wipe_right - int(round((wipe_right - wipe_left) * ratio)),
        )
        break
    return scanline


def _fill_clip_band(
    segments: list[_FillSegment],
    t_ms: int,
    rtl: bool,
) -> tuple[int, int] | None:
    """已唱区水平裁剪带 ``(left, right)``；空带返回 ``None``。

    LTR：左缘固定在首字符左缘，右缘随扫光右移；
    RTL：右缘固定在首字符（最右）右缘，左缘随扫光左移。
    """
    if not segments:
        return None
    if rtl:
        left = _fill_extent_left(segments, t_ms)
        right = max(_segment_wipe_edges(segment)[1] for segment in segments)
    else:
        left = _fill_extent_start(segments)
        right = _fill_extent_end(segments, t_ms)
    if left is None or right is None or right <= left:
        return None
    return left, right


def _fill_clip_band_for_indices(
    segments: list[_FillSegment],
    indices: set[int],
    t_ms: int,
    rtl: bool,
) -> tuple[int, int] | None:
    if not indices:
        return _fill_clip_band(segments, t_ms, rtl)
    scoped = [
        segment
        for segment in segments
        if segment.indices and any(index in indices for index in segment.indices)
    ]
    while scoped and (
        scoped[0].right <= scoped[0].left
        or _segment_fill_ratio(scoped[0], t_ms) <= 0.0
    ):
        scoped = scoped[1:]
    return _fill_clip_band(scoped, t_ms, rtl)


def _fill_clip_band_for_glyphs(
    segments: list[_FillSegment],
    glyphs: list[_GlyphLayout],
    t_ms: int,
    rtl: bool,
) -> tuple[int, int] | None:
    return _fill_clip_band_for_indices(
        segments,
        {glyph.index for glyph in glyphs},
        t_ms,
        rtl,
    )


def _run_fill_complete(
    segments: list[_FillSegment],
    indices: set[int],
    t_ms: int,
) -> bool:
    """run 覆盖的走字分段是否已全部唱完（扫光线已越过 run 前缘）。

    唱完后已唱层不再需要在扫光线处裁切，行缘的发光/描边可完整外扩。
    """
    if indices:
        scoped = [
            segment
            for segment in segments
            if segment.indices and any(index in indices for index in segment.indices)
        ]
    else:
        scoped = segments
    return bool(scoped) and all(
        _segment_fill_ratio(segment, t_ms) >= 1.0 for segment in scoped
    )


def _segment_fill_ratio(segment: _FillSegment, t_ms: int) -> float:
    if segment.ruby is None:
        return char_fill_ratio(segment.start_ms, segment.end_ms, t_ms)
    if segment.ruby_base_index is not None:
        progress = _main_text_ruby_progress_ratio(
            segment.ruby, t_ms, mode="reading_units"
        )
        return max(
            0.0,
            min(
                1.0,
                progress * max(segment.ruby_base_count, 1)
                - segment.ruby_base_index,
            ),
        )
    return _main_text_ruby_progress_ratio(segment.ruby, t_ms)


def _character_fill_ratio(
    line: TimingLine,
    intervals: list[tuple[int, int]],
    char_x_ranges: list[tuple[int, int]],
    active_rubies: list[RubyAnnotation],
    index: int,
    t_ms: int,
    *,
    groups: dict[int, tuple[list[int], RubyAnnotation]] | None = None,
    ruby_main_progress_mode: str = "checkpoint_segments",
) -> float:
    # groups 由 _resolve_char_ruby_groups 预建（每行一次）；缺省回退逐字查找。
    if groups is not None:
        entry = groups.get(index)
        ruby = entry[1] if entry is not None else None
        raw_indices = entry[0] if entry is not None else None
    else:
        ruby = _ruby_for_char_index(active_rubies, line, intervals, index)
        raw_indices = _ruby_target_indices(ruby, line, intervals) if ruby is not None else None
    # Keep SUG's pause-only linked-phrase marker available to
    # ``_utopia_main_group_for_index`` while letting the main-text wipe follow
    # the underlying syllable/character intervals.
    if ruby is not None and _is_utopia_group_marker(ruby):
        ruby = None
        raw_indices = None
    if ruby is not None:
        indices = [
            candidate
            for candidate in raw_indices
            if 0 <= candidate < len(char_x_ranges)
        ]
        if indices and not _ruby_main_uses_base_timing(line, indices):
            effective_ruby = _effective_ruby_for_target(ruby, indices, intervals)
            if (
                ruby_main_progress_mode == "reading_units"
                and _ruby_visual_units_and_intervals(effective_ruby)
            ):
                base_index = indices.index(index)
                progress = _main_text_ruby_progress_ratio(
                    effective_ruby, t_ms, mode="reading_units"
                )
                return max(
                    0.0,
                    min(1.0, progress * len(indices) - base_index),
                )
            group_left = min(char_x_ranges[candidate][0] for candidate in indices)
            group_right = max(char_x_ranges[candidate][1] for candidate in indices)
            fill_end = group_left + (group_right - group_left) * _main_text_ruby_progress_ratio(
                effective_ruby, t_ms
            )
            char_left, char_right = char_x_ranges[index]
            width = max(char_right - char_left, 1)
            return max(0.0, min(1.0, (fill_end - char_left) / width))
    if index >= len(intervals):
        return 0.0
    start, end = intervals[index]
    return char_fill_ratio(start, end, t_ms)


_VERTICAL_PROGRESS_PORTS = VerticalProgressPorts(
    resolve_char_ruby_groups=_resolve_char_ruby_groups,
    character_fill_ratio=_character_fill_ratio,
)


def _is_utopia_group_marker(ruby: RubyAnnotation) -> bool:
    """Return whether ``ruby`` is SUG's linked-phrase-only pause marker."""
    return ruby.reading.strip() in {"", "^"} and all(
        not part.strip() or part.strip() == "^" for part in ruby.reading_parts
    )


# ---------------------------------------------------------------------------
# Before-layer 缓存：构建 / 查询
# ---------------------------------------------------------------------------


_TITLE_RENDER_PORTS = TitleRenderPorts(
    fill_signature=_fill_signature,
    make_raster_image=_make_raster_image,
    paint_text_stack=_paint_title_text_stack,
    raster_scale_key=_raster_scale_key,
    visual_padding=_title_visual_padding,
)


_VERTICAL_CACHE_PORTS = VerticalCachePorts(
    karaoke_state_signature=_karaoke_state_signature,
)


def _lane_alignment(
    style: Style, lane: int | None, page_line_count: int | None = None
) -> str:
    """lane（0 = 页内最上行）对应的水平对齐（N3 ``CalcHorizontalAlignment``）。

    取值方向跟着上下配置走：Top / Middle 从对齐列表**开头**往下数，Bottom 从
    **末尾**往回数。满页时两者等价，段末短页（页内行数 < 列表长度）不等价——
    Bottom 锚定的短页取列表末尾那几项，例如 3 行布局 ``[左, 中, 右]`` 里的两行页
    是「中 + 右」而不是「左 + 中」。``page_line_count`` 为 ``None`` 时按满页处理。
    越界钳到端项（对应 N3 两个分支各自的 while 上下限）。
    """
    alignments = style.line_alignments or ["left"]
    count = len(alignments)
    index = 0 if lane is None else max(int(lane), 0)
    if (
        page_line_count is not None
        and style.line_y_position == "bottom"
        and 0 < int(page_line_count) < count
    ):
        index = max(count - int(page_line_count) + index, 0)
    return alignments[min(index, count - 1)]


def _line_lane_alignment(
    track: TimingTrack, line: TimingLine, style: Style, lane: int | None
) -> str:
    """某一行的水平对齐；Bottom 短页会改取对齐列表末尾（见 ``_lane_alignment``）。"""
    if not _bottom_short_page_alignment(style):
        return _lane_alignment(style, lane)
    page = _renderable_page_lines(track, line, style)
    return _lane_alignment(style, lane, len(page) if page else None)


def _bottom_short_page_alignment(style: Style) -> bool:
    """Bottom 锚定 + 多行对齐列表：短页会改取对齐列表的末尾项。"""
    return (
        style.dual_line_layout
        and style.line_horizontal_layout == "asymmetric"
        and style.line_y_position == "bottom"
        and len(style.line_alignments or []) > 1
    )


def _layout_page_lines(
    track: TimingTrack, line: TimingLine, style: Style
) -> list[tuple[TimingLine, int]] | None:
    """水平布局要用到的页成员；SmartHorizon 与 Bottom 短页对齐都不需要时返回 None。

    两个特性共用同一趟页定位，避免多算一次 ``assign_lanes``。
    """
    if not style.dual_line_layout or style.line_horizontal_layout != "asymmetric":
        return None
    needs_smart = style.smart_horizontal != "none" and not style.vertical
    if not needs_smart and not _bottom_short_page_alignment(style):
        return None
    return _renderable_page_lines(track, line, style)


def _resolve_line_x(
    img_w: int,
    total_w: int,
    style: Style,
    lane: int | None,
    *,
    center_override: bool = False,
    page_line_count: int | None = None,
) -> int:
    if center_override:
        return (img_w - total_w) // 2
    if style.line_horizontal_layout == "per_row":
        align, offset_x, _ = _row_layout_params(style, lane)
        return _aligned_x0(img_w, total_w, align) + offset_x
    if style.line_horizontal_layout == "center":
        return (img_w - total_w) // 2
    if style.dual_line_layout and lane is not None:
        align = _lane_alignment(style, lane, page_line_count)
        margin = style.horizontal_margin_px
        if align == "left":
            return margin
        if align == "right":
            return img_w - margin - total_w
        return (img_w - total_w) // 2
    return (img_w - total_w) // 2


def _aligned_x0(img_w: int, total_w: int, align: str) -> int:
    """根据水平锚点返回行左边缘 x0：left=贴左，center=居中，right=贴右。"""
    if align == "center":
        return (img_w - total_w) // 2
    if align == "right":
        return img_w - total_w
    return 0


def _row_layout_params(style: Style, lane: int | None) -> tuple[str, int, int]:
    """逐行布局参数 (对齐, offset_x, offset_y)。lane 1 取第二行，其余取第一行。"""
    if lane == 1:
        return style.row2_align, style.row2_offset_x, style.row2_offset_y
    return style.row1_align, style.row1_offset_x, style.row1_offset_y


def _line_total_width(
    line: TimingLine,
    style: Style,
    rubies: list[RubyAnnotation] | None = None,
) -> int:
    """行盒宽度（给了 rubies 时含 ruby 推移间隙与行缘溢出）。

    与绘制路径同一套测量，供 SmartHorizon 页宽与余白警告使用。

    对齐 N3 ``DrawLineInfo.DrawLineLeft/DrawLineRight``：行盒就是字形几何的左右
    边界，左边不留任何描边余量，右边那一次描边已经含在末字 ``DrawWidth`` 里
    （我们的字步进同样已经含首层描边）。次级描边、发光、阴影允许溢出到左右余白
    之外——N3 就是这么画的，只有余白警告会提示。

    这里以前在 ``legacy`` 语义下额外左右各加 ``ceil((描边 + 次级描边) / 2)``，
    于是行宽比 N3 宽一整个描边宽，落点与 SmartHorizon 的阈值 / slack 判定都跟着
    偏；现在两种语义同口径。
    """
    source_line = line
    line = _line_with_guide_symbol(line)
    if _line_has_role_labels(line):
        role_layout = _build_role_text_layout(line, style, x0=0, baseline_y=0)
        char_widths, _ranges = _role_char_geometry_by_index(line, role_layout)
        text_width = role_layout.total_width
    else:
        font = _build_font(style)
        metrics = QFontMetrics(font)
        latin_font = _build_latin_font(style)
        font_for = _make_font_for(style, font, latin_font)
        latin_metrics = QFontMetrics(latin_font) if font_for is not None else metrics
        char_widths = [
            (
                _vector_glyph_width(
                    char.vector_glyph,
                    _style_for_role_in_layout(style, char.role_label),
                )
                if char.vector_glyph is not None
                else _char_layout_width(
                    char.text, font, metrics, latin_metrics, font_for, style
                )
            )
            for char in line.chars
        ]
        text_width = _line_text_width(char_widths, style)
    left_ext = right_ext = 0
    gap_total = 0
    if rubies:
        active = _active_rubies_for_line(rubies, source_line)
        if active:
            gaps, ruby_left, ruby_right = _ruby_char_gaps(
                line, char_widths, active, style
            )
            gap_total = sum(gaps)
            left_ext = ruby_left
            right_ext = ruby_right
    return max(
        int(
            round(
                text_width
                + gap_total
                + left_ext
                + right_ext
            )
        ),
        1,
    )


def _smart_horizontal_dx(
    img_w: int,
    total_w: int,
    track: TimingTrack,
    line: TimingLine,
    style: Style,
    lane: int | None,
    *,
    center_override: bool,
    page: list[tuple[TimingLine, int]] | None = None,
) -> int:
    """SmartHorizon 二次水平修正（逆向 N3 ``SetOneLineX``）。

    仅作用于 ``asymmetric`` 多行布局：``center_position`` 逐行判断短行是否
    从画面中心附近开始/结束；``equal_margins`` 以页为单位，页内同时存在
    Left 与 Right 行且有空隙时，把空隙对半分给 Left/Right 行。Center 行
    （含被居中覆盖的行）不修正。

    ``page`` 是调用方已经定好的页成员（``_layout_page_lines``），省一趟页定位。
    """
    mode = style.smart_horizontal
    if mode == "none" or style.vertical or center_override:
        return 0
    if not style.dual_line_layout or style.line_horizontal_layout != "asymmetric":
        return 0
    if page is None:
        page = _renderable_page_lines(track, line, style)
    page_rows = len(page) if page else None
    own_align = _lane_alignment(style, lane, page_rows)
    if own_align == "center":
        return 0
    margin = style.horizontal_margin_px
    font = _n3_smart_font_size(line, style)
    base_x = _resolve_line_x(
        img_w, total_w, style, lane, center_override=False, page_line_count=page_rows
    )
    if page is not None and len(page) <= 1:
        # 单行页：SmartHorizon != None 时整行居中。
        return (img_w - total_w) // 2 - base_x

    if mode == "center_position":
        threshold = img_w // 2 + font // 2 - total_w
        if threshold <= margin:
            return 0
        if own_align == "right":
            return (img_w // 2 - font // 2) - base_x
        return threshold - base_x

    # equal_margins：按页内 Left / Center / Right 各自最大宽度计算空隙。
    if page is None:
        return 0
    if page:
        page_head, _page_head_lane = page[0]
        font = _n3_smart_font_size(
            page_head, _style_for_line(style, page_head)
        )
    max_widths = {"left": 0, "center": 0, "right": 0}
    for page_line, page_lane in page:
        page_style = _style_for_line(style, page_line)
        if _line_center_override(track, page_line, page_style):
            align = "center"
        else:
            align = _lane_alignment(page_style, page_lane, page_rows)
        width = (
            total_w
            if page_line is line
            else _line_total_width(page_line, page_style, track.rubies)
        )
        max_widths[align] = max(max_widths[align], width)
    if max_widths["left"] == 0 or max_widths["right"] == 0:
        return 0
    slack = (
        img_w
        - margin * 2
        - max_widths["left"]
        - max_widths["center"]
        - max_widths["right"]
        + font
    )
    if slack <= 0:
        return 0
    return -(slack // 2) if own_align == "right" else slack // 2


def _n3_smart_font_size(line: TimingLine, style: Style) -> int:
    """Font-size term used by N3's SmartHorizon formulas.

    N3 reads the Japanese font slot belonging to the first rendered character,
    even when that character is assigned a non-default color/font scheme.
    N3 has only one layout semantics, so this holds for ``legacy`` styles too.
    """
    render_line = _line_with_guide_symbol(line)
    if not render_line.chars:
        return max(int(style.font_size_px), 1)
    first_style = _style_for_role_in_layout(
        style, render_line.chars[0].role_label
    )
    return max(int(first_style.font_size_px), 1)


def _resolve_line_x_smart(
    img_w: int,
    total_w: int,
    track: TimingTrack,
    line: TimingLine,
    style: Style,
    lane: int | None,
    *,
    center_override: bool = False,
) -> int:
    page = _layout_page_lines(track, line, style)
    x = _resolve_line_x(
        img_w,
        total_w,
        style,
        lane,
        center_override=center_override,
        page_line_count=len(page) if page else None,
    )
    return x + _smart_horizontal_dx(
        img_w,
        total_w,
        track,
        line,
        style,
        lane,
        center_override=center_override,
        page=page,
    )



def _display_line_static_collision_window(
    display_line: DisplayLine,
    style: Style,
) -> tuple[int, int]:
    """Return the non-animation interval used for page placement collisions."""

    line_style = _style_for_line_display_window(
        style,
        display_line.line,
        display_line.display_start_ms,
        display_line.display_end_ms,
    )
    start = int(display_line.display_start_ms)
    end = int(display_line.display_end_ms)
    if line_style.entry_anim != "none":
        start += max(int(line_style.entry_lead_ms), 0)
    if line_style.exit_anim != "none":
        end -= max(int(line_style.exit_fade_ms), 0)
    return start, max(start, end)


def _display_line_collision_time_window(
    display_line: DisplayLine,
    style: Style,
    *,
    time_window: str,
) -> tuple[int, int]:
    if time_window == "display":
        start = int(display_line.display_start_ms)
        end = int(display_line.display_end_ms)
        return start, max(start, end)
    if time_window != "stable":
        raise ValueError(f"Unsupported collision time window: {time_window}")
    return _display_line_static_collision_window(display_line, style)


def _paint_rubies(
    painter: QPainter,
    ruby_font: QFont,
    ruby_metrics: QFontMetrics,
    line: TimingLine,
    intervals: list[tuple[int, int]],
    char_x_ranges: list[tuple[int, int]],
    main_baseline_y: int,
    t_ms: int,
    rubies: list[RubyAnnotation],
    style: Style,
    transition: _LineCharTransition | None = None,
    main_ascent_px: int | None = None,
    text_layout: _TextLayout | None = None,
    draw_glow: bool = True,
    precomputed_layouts: tuple[_RubyLayout, ...] | None = None,
) -> None:
    rtl = style.right_to_left
    painter.save()
    try:
        painter.setFont(ruby_font)
        layouts = list(precomputed_layouts) if precomputed_layouts is not None else _layout_rubies(
            ruby_metrics,
            line,
            intervals,
            char_x_ranges,
            main_baseline_y,
            rubies,
            style,
            main_ascent_px=main_ascent_px,
            text_layout=text_layout,
            ruby_font=ruby_font,
        )
        if transition is None:
            _paint_ruby_layers(
                painter,
                layouts,
                ruby_font,
                ruby_metrics,
                t_ms,
                style,
                rtl,
                draw_glow=draw_glow,
            )
            return
        for layout in layouts:
            ruby_style = layout.style
            target_ruby_font = layout.font or ruby_font
            target_ruby_metrics = layout.metrics or ruby_metrics
            indices = layout.indices
            paint_ruby = layout.ruby
            x = layout.x
            ruby_baseline_y = layout.baseline_y
            target_width = layout.target_width
            reading_w = layout.reading_width
            opacity, dx, dy, rotation, scale_x, scale_y, skew_y = 1.0, 0.0, 0.0, 0.0, 1.0, 1.0, 0.0
            if transition is not None:
                first_index = min(indices)
                last_index = max(indices)
                following_done_ms = (
                    _utopia_following_done_time(line, intervals, last_index, style)
                    if transition.effect == "utopia"
                    else None
                )
                opacity, dx, dy, rotation, scale_x, scale_y, skew_y = _transition_char_state(
                    style,
                    transition,
                    first_index,
                    max(len(line.chars), 1),
                    char_start_ms=intervals[first_index][0],
                    char_end_ms=intervals[last_index][1],
                    t_ms=t_ms,
                    frame_height=painter.device().height(),
                    following_done_ms=following_done_ms,
                )
            if opacity <= 0.0:
                continue
            painter.save()
            try:
                use_utopia_origin = transition is not None and transition.effect == "utopia"
                if use_utopia_origin:
                    _paint_ruby_text_units_with_transition(
                        painter,
                        paint_ruby,
                        target_ruby_font,
                        target_ruby_metrics,
                        x,
                        ruby_baseline_y,
                        t_ms,
                        ruby_style,
                        transition,
                        first_index,
                        max(len(line.chars), 1),
                        following_done_ms,
                        rtl,
                        target_width=target_width,
                        gradient_rect=layout.gradient_rect,
                        horizontal_gradient_rect=layout.horizontal_gradient_rect,
                    )
                else:
                    painter.setOpacity(painter.opacity() * opacity)
                    is_char_drip = transition.effect == "char_drip"
                    if is_char_drip:
                        center_x = x + reading_w
                        center_y = (
                            ruby_baseline_y
                            if transition.phase == "entry"
                            else ruby_baseline_y - target_ruby_metrics.height()
                        )
                    else:
                        center_x = x + reading_w / 2
                        center_y = (
                            ruby_baseline_y
                            - target_ruby_metrics.ascent()
                            + target_ruby_metrics.height() / 2
                        )
                    _apply_character_transform(
                        painter,
                        center_x=center_x,
                        center_y=center_y,
                        dx=dx,
                        dy=dy,
                        rotation=rotation,
                        scale_x=scale_x,
                        scale_y=scale_y,
                        skew_y=skew_y,
                    )
                    _paint_ruby_text(
                        painter,
                        paint_ruby,
                        target_ruby_font,
                        target_ruby_metrics,
                        x,
                        ruby_baseline_y,
                        t_ms,
                        ruby_style,
                        rtl,
                        target_width=target_width,
                        gradient_rect=layout.gradient_rect,
                        horizontal_gradient_rect=layout.horizontal_gradient_rect,
                        wipe_layout=layout,
                    )
            finally:
                painter.restore()
    finally:
        painter.restore()


def _layout_rubies(
    ruby_metrics: QFontMetrics,
    line: TimingLine,
    intervals: list[tuple[int, int]],
    char_x_ranges: list[tuple[int, int]],
    main_baseline_y: int,
    rubies: list[RubyAnnotation],
    style: Style,
    *,
    main_ascent_px: int | None = None,
    text_layout: _TextLayout | None = None,
    ruby_font: QFont | None = None,
) -> list[_RubyLayout]:
    """layout 段：算横排 ruby 的目标字符范围、基线与排布宽度。"""
    if not rubies:
        return []
    main_box_ascent: Optional[float] = None
    if main_ascent_px is not None and text_layout is not None and text_layout.glyphs:
        # N3 行盒顶 = 参与注音高度计算的字符盒顶最高者；空白无墨水不算。
        height_glyphs = [
            glyph
            for glyph in text_layout.glyphs
            if glyph.text.strip() and glyph.style.affects_ruby_anchor
        ]
        if not height_glyphs:
            # If every visible glyph opted out, keep ruby attached to its own
            # base characters instead of falling back to an unrelated global
            # font metric.  This also gives an all-decoration line a safe floor.
            ruby_target_indices = {
                index
                for ruby in rubies
                for index in _ruby_target_indices(ruby, line, intervals)
            }
            height_glyphs = [
                glyph
                for glyph in text_layout.glyphs
                if glyph.text.strip() and glyph.index in ruby_target_indices
            ]
        candidates = [
            _n3_char_box_ascent(
                glyph.metrics, glyph.style.font_size_px, glyph.style.stroke_width_px
            )
            for glyph in height_glyphs
        ]
        if candidates:
            main_box_ascent = max(candidates)
    if main_box_ascent is None:
        main_box_ascent = _n3_char_box_ascent(
            QFontMetrics(_build_font(style)), style.font_size_px, style.stroke_width_px
        )
    layouts: list[_RubyLayout] = []
    for ruby in rubies:
        indices = _ruby_target_indices(ruby, line, intervals)
        if not indices:
            continue
        paint_ruby = _effective_ruby_for_target(ruby, indices, intervals)
        target_range = _ruby_target_x_range(ruby, line, intervals, char_x_ranges)
        if target_range is None:
            continue
        ruby_brush_style = _ruby_style_for_target_indices(style, line, indices)
        ruby_style = _ruby_script_stroke_style(
            ruby_brush_style, paint_ruby.reading
        )
        target_ruby_font = _build_ruby_font_for_text(
            ruby_style, paint_ruby.reading
        )
        target_ruby_metrics = QFontMetrics(target_ruby_font)
        target_ruby_size = max(target_ruby_font.pixelSize(), 1)
        ruby_baseline_y = _ruby_baseline_y(
            main_baseline_y,
            main_box_ascent,
            target_ruby_metrics,
            ruby_style,
            font_size_px=target_ruby_size,
        )
        left, right = target_range
        target_width = max(right - left, 1)
        gradient_rect = _n3_ruby_fill_rect(
            left,
            target_width,
            ruby_baseline_y,
            target_ruby_metrics,
            ruby_style,
            brush_style=ruby_brush_style,
            font_size_px=target_ruby_size,
        )
        reading_width = _ruby_layout_width(
            paint_ruby.reading,
            target_ruby_metrics,
            target_width,
            style=ruby_style,
            base_text=paint_ruby.kanji,
        )
        wipe_segments, wipe_left, wipe_right, geometry_signature = _ruby_wipe_geometry(
            paint_ruby,
            target_ruby_font,
            target_ruby_metrics,
            left,
            ruby_baseline_y,
            target_width,
            ruby_style,
            rtl=style.right_to_left,
        )
        layouts.append(
            _RubyLayout(
                ruby=paint_ruby,
                indices=indices,
                style=ruby_style,
                x=left,
                baseline_y=ruby_baseline_y,
                target_width=target_width,
                reading_width=reading_width,
                gradient_rect=gradient_rect,
                wipe_segments=wipe_segments,
                wipe_left=wipe_left,
                wipe_right=wipe_right,
                geometry_signature=geometry_signature,
                font=target_ruby_font,
                metrics=target_ruby_metrics,
            )
        )
    if text_layout is not None and layouts:
        main_rect = _n3_main_fill_rect(text_layout, main_baseline_y)
        top = min(
            float(main_rect.top()),
            *(float(layout.gradient_rect.top()) for layout in layouts),
        )
        bottom = max(
            float(main_rect.bottom()),
            *(float(layout.gradient_rect.bottom()) for layout in layouts),
        )
        shared_rect = QRectF(
            float(main_rect.left()),
            top,
            float(max(main_rect.width(), 1.0)),
            float(max(bottom - top, 1.0)),
        )
        layouts = [
            replace(
                layout,
                horizontal_gradient_rect=(
                    shared_rect
                    if layout.style.ruby_horizontal_gradient_with_main
                    else None
                ),
            )
            for layout in layouts
        ]
    return layouts


def _ruby_wipe_geometry(
    ruby: RubyAnnotation,
    ruby_font: QFont,
    ruby_metrics: QFontMetrics,
    x: int,
    baseline_y: int,
    target_width: int,
    style: Style,
    *,
    rtl: bool,
) -> tuple[tuple[_RubyWipeSegment, ...], float, float, tuple]:
    """Build N3-style timed glyph geometry independently from the layout box.

    The target-width box is still used for Center/EqualSpace placement.  Wipe
    fronts follow each visible glyph's primary-stroke drawing bounds, matching
    N3 ``WipeLeft`` (geometry bounds expanded by half ``EdgeSize``).  Using the
    fill-only ink bounds makes the next ruby glyph's left half-stroke visible at
    ratio zero and leaves the previous half-stroke unfinished until the exact
    boundary frame, which reads as a jump.  The layout box's centered leading /
    trailing blank area still does not consume singing time.
    """
    logical_units = _ruby_visual_units_and_intervals(ruby)
    if not logical_units:
        return (), float(x), float(x), ()
    visual_units = list(reversed(logical_units)) if rtl else logical_units
    unit_layouts = _ruby_layout_units(
        [unit for unit, _interval in visual_units],
        ruby_metrics,
        x,
        target_width,
        style=style,
        base_text=ruby.kanji,
    )
    segments: list[_RubyWipeSegment] = []
    signature: list[tuple] = []
    bounds: list[tuple[float, float]] = []
    # N3 performs integer ``EdgeSize / 2`` before converting to float.
    edge_half = float(max(int(_ruby_stroke_width(style)), 0) // 2)
    for (unit, interval), (_draw_unit, unit_x, unit_width) in zip(
        visual_units, unit_layouts
    ):
        path = QPainterPath()
        path.addText(float(unit_x), float(baseline_y), ruby_font, unit)
        ink = path.boundingRect()
        if ink.isEmpty():
            ink_left = float(unit_x)
            ink_right = float(unit_x + max(unit_width, 0.0))
        else:
            ink_left = float(ink.left())
            ink_right = float(ink.right())
        if ink_right < ink_left:
            ink_left, ink_right = ink_right, ink_left
        draw_left = ink_left - edge_half
        draw_right = ink_right + edge_half
        start_ms, end_ms = interval
        segments.append(
            _RubyWipeSegment(
                int(start_ms),
                max(int(start_ms), int(end_ms)),
                draw_right if rtl else draw_left,
                draw_left if rtl else draw_right,
            )
        )
        bounds.append((draw_left, draw_right))
        signature.append(
            (
                unit,
                round(float(unit_x) - float(x), 3),
                round(float(unit_width), 3),
                round(ink_left - float(x), 3),
                round(ink_right - float(x), 3),
            )
        )
    if not bounds:
        return (), float(x), float(x), tuple(signature)
    segments.sort(key=lambda segment: (segment.start_ms, segment.end_ms))
    adjusted = list(segments)
    for index in range(len(adjusted) - 1):
        current = adjusted[index]
        following = adjusted[index + 1]
        overlaps = (
            current.axis_end <= following.axis_start
            if rtl
            else current.axis_end >= following.axis_start
        )
        if overlaps:
            adjusted[index] = replace(current, axis_end=following.axis_start)
    return (
        tuple(adjusted),
        min(left for left, _right in bounds),
        max(right for _left, right in bounds),
        tuple(signature),
    )


def _role_ruby_vertical_extra(
    line: TimingLine,
    rubies: list[RubyAnnotation],
    intervals: list[tuple[int, int]],
    style: Style,
) -> int:
    """Reserve enough vertical space for the largest role-specific ruby."""
    extra = 0
    for ruby in rubies:
        indices = _ruby_target_indices(ruby, line, intervals)
        if not indices:
            continue
        paint_ruby = _effective_ruby_for_target(ruby, indices, intervals)
        ruby_style = _ruby_script_stroke_style(
            _ruby_style_for_target_indices(style, line, indices),
            paint_ruby.reading,
        )
        font = _build_ruby_font_for_text(ruby_style, paint_ruby.reading)
        metrics = QFontMetrics(font)
        extra = max(
            extra,
            _ruby_vertical_extra(
                ruby_style,
                metrics,
                font_size_px=max(font.pixelSize(), 1),
            ),
        )
    return extra


def _n3_ruby_fill_rect(
    left: int,
    width: int,
    baseline_y: int,
    ruby_metrics: QFontMetrics,
    style: Style,
    *,
    brush_style: Style | None = None,
    font_size_px: int | None = None,
) -> QRectF:
    """Return the ruby ``DrawLineInfo`` gradient area used by N3."""
    font_size = (
        _ruby_font_size(style)
        if font_size_px is None
        else max(int(font_size_px), 1)
    )
    metric_total = max(ruby_metrics.ascent() + ruby_metrics.descent(), 1)
    descent = font_size * max(ruby_metrics.descent(), 0) // metric_total
    draw_edge = _ruby_stroke_width(style)
    anchor_style = brush_style or style
    anchor_edge = _ruby_stroke_width(anchor_style)
    anchor_edge2 = _ruby_stroke2_width(anchor_style)
    draw_bottom = float(baseline_y + descent + draw_edge // 2)
    draw_top = draw_bottom - float(font_size + draw_edge)
    inset = float((anchor_edge + anchor_edge2) // 2)
    top = draw_top + inset
    bottom = draw_bottom - inset
    return QRectF(
        float(left),
        top,
        float(max(width, 1)),
        float(max(bottom - top, 1.0)),
    )


def _paint_ruby_layers(
    painter: QPainter,
    layouts: list[_RubyLayout],
    ruby_font: QFont,
    ruby_metrics: QFontMetrics,
    t_ms: int,
    style: Style,
    rtl: bool,
    *,
    draw_glow: bool = True,
) -> None:
    _TEXT_RUN_COMPOSITOR.paint_ordered(
        painter,
        LayerContext(t_ms=t_ms, logical_w=0, logical_h=0),
        _ruby_text_layers(
            layouts, ruby_font, ruby_metrics, t_ms, style, rtl, draw_glow=draw_glow
        ),
    )


def _paint_ruby_glow_layers(
    painter: QPainter,
    layouts: list[_RubyLayout],
    ruby_font: QFont,
    ruby_metrics: QFontMetrics,
    t_ms: int,
    style: Style,
    rtl: bool,
) -> None:
    _TEXT_RUN_COMPOSITOR.paint_ordered(
        painter,
        LayerContext(t_ms=t_ms, logical_w=0, logical_h=0),
        _ruby_glow_layers(layouts, ruby_font, ruby_metrics, t_ms, style, rtl),
    )


def _ruby_text_layers(
    layouts: list[_RubyLayout],
    ruby_font: QFont,
    ruby_metrics: QFontMetrics,
    t_ms: int,
    style: Style,
    rtl: bool,
    *,
    draw_glow: bool = True,
) -> list:
    layers = []
    for index, layout in enumerate(layouts):
        target_ruby_font = layout.font or ruby_font
        target_ruby_metrics = layout.metrics or ruby_metrics
        layers.append(
            _RubyTextLayer(
                layout,
                target_ruby_font,
                target_ruby_metrics,
                t_ms,
                layout.style,
                rtl,
                after=False,
                z_index=index * 2,
                draw_glow=draw_glow,
            )
        )
        layers.append(
            _RubyTextLayer(
                layout,
                target_ruby_font,
                target_ruby_metrics,
                t_ms,
                layout.style,
                rtl,
                after=True,
                z_index=index * 2 + 1,
                draw_glow=draw_glow,
            )
        )
    return layers


def _ruby_glow_layers(
    layouts: list[_RubyLayout],
    ruby_font: QFont,
    ruby_metrics: QFontMetrics,
    t_ms: int,
    style: Style,
    rtl: bool,
) -> list:
    layers = []
    for index, layout in enumerate(layouts):
        target_ruby_font = layout.font or ruby_font
        target_ruby_metrics = layout.metrics or ruby_metrics
        if _ruby_glow_can_combine_split(layout.style):
            layers.append(
                _RubySplitGlowLayer(
                    layout,
                    target_ruby_font,
                    target_ruby_metrics,
                    t_ms,
                    layout.style,
                    rtl,
                    z_index=index,
                )
            )
            continue
        layers.append(
            _RubyGlowLayer(
                layout,
                target_ruby_font,
                target_ruby_metrics,
                t_ms,
                layout.style,
                rtl,
                after=False,
                z_index=index * 2,
            )
        )
        layers.append(
            _RubyGlowLayer(
                layout,
                target_ruby_font,
                target_ruby_metrics,
                t_ms,
                layout.style,
                rtl,
                after=True,
                z_index=index * 2 + 1,
            )
        )
    return layers


def _ruby_layer_stack(
    layout: _LineLayout,
    line: TimingLine,
    t_ms: int,
    style: Style,
) -> list:
    if layout.ruby_metrics is None:
        return []
    return _ruby_text_layers(
        list(layout.ruby_layouts),
        layout.ruby_font,
        layout.ruby_metrics,
        t_ms,
        style,
        layout.rtl,
    )


@dataclass(frozen=True)
class _RubySplitGlowLayer:
    """Combined before/after ruby glow with a cached moving-front strip."""

    ruby_layout: _RubyLayout
    ruby_font: QFont
    ruby_metrics: QFontMetrics
    t_ms: int
    style: Style
    rtl: bool
    z_index: int = 0
    scope: str = SCOPE_LINE

    def active_window(self, ctx: LayerContext) -> list[tuple[int, int]]:
        return []

    def layout(self, ctx: LayerContext) -> "_RubySplitGlowLayer":
        return self

    def static_key(self, ctx: LayerContext, layout: object) -> None:
        # The layer moves every frame, but its two full halos are cached below.
        return None

    def bake(self, ctx: LayerContext, layout: object, key: Hashable) -> BakedLayer:
        raise AssertionError("combined ruby split glow is painted dynamically")

    def animate(self, ctx: LayerContext, layout: object) -> LayerAnimation:
        return LayerAnimation()

    def paint_dynamic(self, painter: QPainter, ctx: LayerContext, layout: object) -> None:
        visible, complete, front = _ruby_wipe_state(
            self.ruby_layout, self.t_ms
        )
        if not visible:
            _blit_cached_ruby_glow(
                painter,
                self.ruby_layout,
                self.ruby_font,
                self.ruby_metrics,
                self.style,
                self.rtl,
                after=False,
            )
            return
        if complete:
            _blit_cached_ruby_glow(
                painter,
                self.ruby_layout,
                self.ruby_font,
                self.ruby_metrics,
                self.style,
                self.rtl,
                after=True,
            )
            return

        reading = (
            "".join(
                reversed(_ruby_utopia_visual_units(self.ruby_layout.ruby.reading))
            )
            if self.rtl
            else self.ruby_layout.ruby.reading
        )
        path, rect = _ruby_text_path_and_rect(
            reading,
            self.ruby_font,
            self.ruby_metrics,
            self.ruby_layout.x,
            self.ruby_layout.baseline_y,
            self.ruby_layout.target_width,
            self.style,
            base_text=self.ruby_layout.ruby.kanji,
        )
        radius = _ruby_glow_radius(self.style, after=False)
        stroke_width = _ruby_stroke_width(self.style)
        stroke2_width = _ruby_stroke2_width(self.style)
        pad = _glow_extent(stroke_width, stroke2_width, radius)
        top = rect.top() - pad
        height = rect.height() + pad * 2
        if self.rtl:
            before_source_clip = QRectF(
                -1_000_000.0, top, front + 1_000_000.0, height
            )
            after_source_clip = QRectF(front, top, 1_000_000.0, height)
            before_baked_clip = QRectF(
                -1_000_000.0,
                -1_000_000.0,
                front - pad + 1_000_000.0,
                2_000_000.0,
            )
            after_baked_clip = QRectF(
                front + pad,
                -1_000_000.0,
                1_000_000.0,
                2_000_000.0,
            )
        else:
            before_source_clip = QRectF(front, top, 1_000_000.0, height)
            after_source_clip = QRectF(
                -1_000_000.0, top, front + 1_000_000.0, height
            )
            before_baked_clip = QRectF(
                front + pad,
                -1_000_000.0,
                1_000_000.0,
                2_000_000.0,
            )
            after_baked_clip = QRectF(
                -1_000_000.0,
                -1_000_000.0,
                front - pad + 1_000_000.0,
                2_000_000.0,
            )

        strip_clip = QRectF(
            front - pad,
            -1_000_000.0,
            float(pad * 2),
            2_000_000.0,
        )
        colors = _effective_ruby_karaoke_colors(self.style)
        _paint_split_glow_path(
            painter,
            path,
            colors.before.shadow,
            colors.after.shadow,
            self.ruby_layout.gradient_rect,
            radius,
            stroke_width,
            stroke2_width,
            before_source_clip=before_source_clip,
            after_source_clip=after_source_clip,
            concentration_level=_ruby_glow_concentration_level(self.style),
            target_clip=strip_clip,
            horizontal_fill_rect=self.ruby_layout.horizontal_gradient_rect,
        )
        for after, clip in (
            (False, before_baked_clip),
            (True, after_baked_clip),
        ):
            painter.save()
            try:
                painter.setClipRect(clip)
                _blit_cached_ruby_glow(
                    painter,
                    self.ruby_layout,
                    self.ruby_font,
                    self.ruby_metrics,
                    self.style,
                    self.rtl,
                    after=after,
                )
            finally:
                painter.restore()

    def vertical_bounds(self, ctx: LayerContext, layout: object) -> tuple[int, int] | None:
        rect = _ruby_text_rect(self.ruby_layout, self.ruby_metrics)
        pad = max(
            _ruby_visual_padding(self.style, after=after)
            for after in (False, True)
        )
        return int(math.floor(rect.top() - pad)), int(math.ceil(rect.bottom() + pad))


@dataclass(frozen=True)
class _RubyGlowLayer:
    """Glow-only layer for one horizontal ruby reading.

    N3 paints ruby/main decorations first, then paints the solid strokes/bodies.
    Splitting ruby glow from ruby body prevents the ruby halo from covering the
    main glyph body while still keeping the ruby stroke/fill above the main glow.
    """

    ruby_layout: _RubyLayout
    ruby_font: QFont
    ruby_metrics: QFontMetrics
    t_ms: int
    style: Style
    rtl: bool
    after: bool
    z_index: int = 0
    scope: str = SCOPE_LINE

    def active_window(self, ctx: LayerContext) -> list[tuple[int, int]]:
        return []

    def layout(self, ctx: LayerContext) -> "_RubyGlowLayer":
        return self

    def static_key(self, ctx: LayerContext, layout: object) -> tuple | None:
        if _ruby_decoration_kind(self.style) != "glow":
            return None
        if _ruby_glow_radius(self.style, after=self.after) == 0:
            return None
        visible, complete, _front = _ruby_wipe_state(
            self.ruby_layout, self.t_ms
        )
        if self.after:
            if not visible:
                return None
            if not _ruby_glow_states_differ(self.style):
                return None
            if not complete:
                return None
        elif _ruby_glow_states_differ(self.style) and visible:
            return None
        return _ruby_glow_layer_key(
            self.ruby_layout,
            self.ruby_font,
            self.style,
            self.rtl,
            after=self.after,
        )

    def bake(self, ctx: LayerContext, layout: object, key: Hashable) -> BakedLayer:
        image, dx, dy = _build_ruby_glow_layer(
            self.ruby_layout,
            self.ruby_font,
            self.ruby_metrics,
            self.style,
            self.rtl,
            after=self.after,
        )
        return BakedLayer(image=image, offset=QPointF(float(dx), float(dy)))

    def animate(self, ctx: LayerContext, layout: object) -> LayerAnimation:
        return LayerAnimation(
            top_left=QPointF(
                float(self.ruby_layout.x),
                float(self.ruby_layout.baseline_y),
            ),
        )

    def paint_dynamic(self, painter: QPainter, ctx: LayerContext, layout: object) -> None:
        if not _ruby_glow_states_differ(self.style):
            return
        visible, complete, front = _ruby_wipe_state(
            self.ruby_layout, self.t_ms
        )
        if not visible or complete:
            return
        reading = (
            "".join(reversed(_ruby_utopia_visual_units(self.ruby_layout.ruby.reading)))
            if self.rtl
            else self.ruby_layout.ruby.reading
        )
        path, rect = _ruby_text_path_and_rect(
            reading,
            self.ruby_font,
            self.ruby_metrics,
            self.ruby_layout.x,
            self.ruby_layout.baseline_y,
            self.ruby_layout.target_width,
            self.style,
            base_text=self.ruby_layout.ruby.kanji,
        )
        radius = _ruby_glow_radius(self.style, after=self.after)
        pad = _glow_extent(
            _ruby_stroke_width(self.style),
            _ruby_stroke2_width(self.style),
            radius,
        )
        source_clip = (
            QRectF(
                front,
                rect.top() - pad,
                1_000_000.0,
                rect.height() + pad * 2,
            )
            if self.rtl == self.after
            else QRectF(
                -1_000_000.0,
                rect.top() - pad,
                front + 1_000_000.0,
                rect.height() + pad * 2,
            )
        )
        colors = _effective_ruby_karaoke_colors(self.style)
        state = colors.after if self.after else colors.before
        _paint_glow_path(
            painter,
            path,
            state.shadow,
            _fill_brush_rect(
                state.shadow,
                self.ruby_layout.gradient_rect,
                self.ruby_layout.horizontal_gradient_rect,
            ),
            radius,
            _ruby_stroke_width(self.style),
            _ruby_stroke2_width(self.style),
            source_clip=source_clip,
            concentration_level=_ruby_glow_concentration_level(self.style),
        )

    def vertical_bounds(self, ctx: LayerContext, layout: object) -> tuple[int, int] | None:
        rect = _ruby_text_rect(self.ruby_layout, self.ruby_metrics)
        pad = _ruby_visual_padding(self.style, after=self.after)
        return int(math.floor(rect.top() - pad)), int(math.ceil(rect.bottom() + pad))


@dataclass(frozen=True)
class _RubyTextLayer:
    """Layer wrapper for one horizontal ruby reading."""

    ruby_layout: _RubyLayout
    ruby_font: QFont
    ruby_metrics: QFontMetrics
    t_ms: int
    style: Style
    rtl: bool
    after: bool
    z_index: int = 0
    scope: str = SCOPE_LINE
    draw_glow: bool = True

    def active_window(self, ctx: LayerContext) -> list[tuple[int, int]]:
        return []

    def layout(self, ctx: LayerContext) -> "_RubyTextLayer":
        return self

    def static_key(self, ctx: LayerContext, layout: object) -> tuple | None:
        if self.after:
            visible, _complete, _front = _ruby_wipe_state(
                self.ruby_layout, self.t_ms
            )
            if not visible:
                return None
        return _ruby_text_layer_key(
            self.ruby_layout,
            self.ruby_font,
            self.style,
            self.rtl,
            after=self.after,
            draw_glow=self.draw_glow,
        )

    def bake(self, ctx: LayerContext, layout: object, key: Hashable) -> BakedLayer:
        image, dx, dy = _build_ruby_text_layer(
            self.ruby_layout,
            self.ruby_font,
            self.ruby_metrics,
            self.style,
            self.rtl,
            after=self.after,
            draw_glow=self.draw_glow,
        )
        return BakedLayer(image=image, offset=QPointF(float(dx), float(dy)))

    def animate(self, ctx: LayerContext, layout: object) -> LayerAnimation:
        clip_rect = None
        if self.after:
            visible, complete, _front = _ruby_wipe_state(
                self.ruby_layout, self.t_ms
            )
            if not visible:
                return LayerAnimation(opacity=0.0)
            if not complete:
                # 唱完（>= 1.0）不再裁剪：裁剪带右缘恰好压在字框右缘，
                # 会把末字形的描边外扩留在走字前状态。
                clip_rect = _ruby_after_clip_rect_at_time(
                    self.ruby_layout,
                    self.ruby_metrics,
                    self.style,
                    self.rtl,
                    self.t_ms,
                )
        return LayerAnimation(
            top_left=QPointF(
                float(self.ruby_layout.x),
                float(self.ruby_layout.baseline_y),
            ),
            clip_rect=clip_rect,
        )

    def paint_dynamic(self, painter: QPainter, ctx: LayerContext, layout: object) -> None:
        return

    def vertical_bounds(self, ctx: LayerContext, layout: object) -> tuple[int, int] | None:
        rect = _ruby_text_rect(self.ruby_layout, self.ruby_metrics)
        pad = _ruby_visual_padding(self.style, after=self.after)
        return int(math.floor(rect.top() - pad)), int(math.ceil(rect.bottom() + pad))


def _ruby_text_layer_key(
    layout: _RubyLayout,
    ruby_font: QFont,
    style: Style,
    rtl: bool,
    *,
    after: bool,
    draw_glow: bool = True,
) -> tuple:
    colors = _effective_ruby_karaoke_colors(style)
    state = colors.after if after else colors.before
    return (
        layout.ruby.reading,
        layout.target_width,
        round(layout.reading_width, 3),
        layout.geometry_signature,
        (
            round(layout.gradient_rect.left() - layout.x, 3),
            round(layout.gradient_rect.top() - layout.baseline_y, 3),
            round(layout.gradient_rect.width(), 3),
            round(layout.gradient_rect.height(), 3),
        ),
        _ruby_horizontal_gradient_rect_signature(layout),
        rtl,
        ruby_font.family(),
        ruby_font.pixelSize(),
        int(ruby_font.weight()),
        ruby_font.italic(),
        _karaoke_state_signature(state),
        _ruby_stroke_width(style),
        _ruby_stroke2_width(style),
        _ruby_shadow_dx(style),
        _ruby_shadow_dy(style),
        _ruby_decoration_kind(style),
        _ruby_glow_radius(style, after=after),
        _ruby_glow_concentration_level(style),
        after,
        draw_glow,
    )


def _ruby_glow_layer_key(
    layout: _RubyLayout,
    ruby_font: QFont,
    style: Style,
    rtl: bool,
    *,
    after: bool,
) -> tuple:
    colors = _effective_ruby_karaoke_colors(style)
    state = colors.after if after else colors.before
    return (
        "ruby_glow",
        layout.ruby.reading,
        layout.target_width,
        round(layout.reading_width, 3),
        layout.geometry_signature,
        (
            round(layout.gradient_rect.left() - layout.x, 3),
            round(layout.gradient_rect.top() - layout.baseline_y, 3),
            round(layout.gradient_rect.width(), 3),
            round(layout.gradient_rect.height(), 3),
        ),
        _ruby_horizontal_gradient_rect_signature(layout),
        rtl,
        ruby_font.family(),
        ruby_font.pixelSize(),
        int(ruby_font.weight()),
        ruby_font.italic(),
        _fill_signature(state.shadow),
        _ruby_stroke_width(style),
        _ruby_stroke2_width(style),
        _ruby_glow_radius(style, after=after),
        _ruby_glow_concentration_level(style),
        after,
    )


def _ruby_horizontal_gradient_rect_signature(
    layout: _RubyLayout,
) -> tuple[float, float, float, float] | None:
    rect = layout.horizontal_gradient_rect
    if rect is None:
        return None
    return (
        round(rect.left() - layout.x, 3),
        round(rect.top() - layout.baseline_y, 3),
        round(rect.width(), 3),
        round(rect.height(), 3),
    )


def _get_or_build_ruby_glow(
    layout: _RubyLayout,
    ruby_font: QFont,
    ruby_metrics: QFontMetrics,
    style: Style,
    rtl: bool,
    *,
    after: bool,
) -> BakedLayer:
    key = (
        "ruby_full_glow",
        _ruby_glow_layer_key(layout, ruby_font, style, rtl, after=after),
    )

    def _build() -> BakedLayer:
        image, dx, dy = _build_ruby_glow_layer(
            layout,
            ruby_font,
            ruby_metrics,
            style,
            rtl,
            after=after,
        )
        return BakedLayer(image=image, offset=QPointF(float(dx), float(dy)))

    return _TEXT_RUN_LAYER_CACHE.get_or_build(key, _build)


def _blit_cached_ruby_glow(
    painter: QPainter,
    layout: _RubyLayout,
    ruby_font: QFont,
    ruby_metrics: QFontMetrics,
    style: Style,
    rtl: bool,
    *,
    after: bool,
) -> None:
    if _ruby_glow_radius(style, after=after) <= 0:
        return
    baked = _get_or_build_ruby_glow(
        layout,
        ruby_font,
        ruby_metrics,
        style,
        rtl,
        after=after,
    )
    if baked.image.isNull():
        return
    anchor = QPointF(
        float(layout.x) + baked.offset.x(),
        float(layout.baseline_y) + baked.offset.y(),
    )
    painter.save()
    try:
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.drawImage(anchor, baked.image)
    finally:
        painter.restore()


def _build_ruby_text_layer(
    layout: _RubyLayout,
    ruby_font: QFont,
    ruby_metrics: QFontMetrics,
    style: Style,
    rtl: bool,
    *,
    after: bool,
    draw_glow: bool = True,
) -> tuple[QImage, int, int]:
    colors = _effective_ruby_karaoke_colors(style)
    state = colors.after if after else colors.before
    paint_style = _ruby_paint_style(style)
    stroke_width = _ruby_stroke_width(style)
    stroke2_width = _ruby_stroke2_width(style)
    shadow_dx = _ruby_shadow_dx(style)
    shadow_dy = _ruby_shadow_dy(style)
    glow_radius = _ruby_glow_radius(style, after=after)
    stroke_extent = _visual_stroke_extent(stroke_width, stroke2_width)
    glow_extra = (
        _glow_extent(stroke_width, stroke2_width, glow_radius)
        if _ruby_decoration_kind(style) == "glow"
        else 0
    )
    extent = max(
        stroke_extent,
        glow_extra,
        stroke_extent + abs(shadow_dx),
        stroke_extent + abs(shadow_dy),
        2,
    ) + 4
    layout_overhang_left = int(math.ceil(_ruby_layout_left_overhang(
        layout.ruby.reading,
        ruby_metrics,
        layout.target_width,
        style,
        layout.ruby.kanji,
    )))
    pad_left = max(0, -shadow_dx) + extent + layout_overhang_left
    pad_right = max(0, shadow_dx) + extent
    pad_top = max(0, -shadow_dy) + extent
    pad_bottom = max(0, shadow_dy) + extent

    ruby_w = max(int(math.ceil(layout.reading_width)), 1)
    ruby_h = max(ruby_metrics.height(), 1)
    img_w = max(pad_left + ruby_w + pad_right, 1)
    img_h = max(pad_top + ruby_h + pad_bottom, 1)
    image = QImage(img_w, img_h, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(0)

    reading = (
        "".join(reversed(_ruby_utopia_visual_units(layout.ruby.reading)))
        if rtl
        else layout.ruby.reading
    )
    local_baseline = pad_top + ruby_metrics.ascent()
    path, rect = _ruby_text_path_and_rect(
        reading,
        ruby_font,
        ruby_metrics,
        pad_left,
        local_baseline,
        layout.target_width,
        style,
        base_text=layout.ruby.kanji,
    )
    fill_rect = layout.gradient_rect.translated(
        -float(layout.x) + float(pad_left),
        -float(layout.baseline_y) + float(local_baseline),
    )
    horizontal_fill_rect = (
        layout.horizontal_gradient_rect.translated(
            -float(layout.x) + float(pad_left),
            -float(layout.baseline_y) + float(local_baseline),
        )
        if layout.horizontal_gradient_rect is not None
        else None
    )

    p = QPainter(image)
    try:
        p.setRenderHints(
            QPainter.RenderHint.Antialiasing
            | QPainter.RenderHint.TextAntialiasing
            | QPainter.RenderHint.SmoothPixmapTransform
        )
        _paint_text_layer_stack(
            p,
            path,
            rect,
            state,
            paint_style,
            stroke_width=stroke_width,
            stroke2_width=stroke2_width,
            shadow_dx=shadow_dx,
            shadow_dy=shadow_dy,
            glow_radius=glow_radius,
            draw_glow=draw_glow,
            fill_rect=fill_rect,
            horizontal_fill_rect=horizontal_fill_rect,
        )
    finally:
        p.end()

    return image, -pad_left, -(pad_top + ruby_metrics.ascent())


def _build_ruby_glow_layer(
    layout: _RubyLayout,
    ruby_font: QFont,
    ruby_metrics: QFontMetrics,
    style: Style,
    rtl: bool,
    *,
    after: bool,
) -> tuple[QImage, int, int]:
    colors = _effective_ruby_karaoke_colors(style)
    state = colors.after if after else colors.before
    stroke_width = _ruby_stroke_width(style)
    stroke2_width = _ruby_stroke2_width(style)
    glow_radius = _ruby_glow_radius(style, after=after)
    extent = _glow_extent(stroke_width, stroke2_width, glow_radius) + 4
    layout_overhang_left = int(math.ceil(_ruby_layout_left_overhang(
        layout.ruby.reading,
        ruby_metrics,
        layout.target_width,
        style,
        layout.ruby.kanji,
    )))
    pad_left = extent + layout_overhang_left
    pad_right = extent
    pad_top = extent
    pad_bottom = extent

    ruby_w = max(int(math.ceil(layout.reading_width)), 1)
    ruby_h = max(ruby_metrics.height(), 1)
    img_w = max(pad_left + ruby_w + pad_right, 1)
    img_h = max(pad_top + ruby_h + pad_bottom, 1)
    image = QImage(img_w, img_h, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(0)

    reading = (
        "".join(reversed(_ruby_utopia_visual_units(layout.ruby.reading)))
        if rtl
        else layout.ruby.reading
    )
    local_baseline = pad_top + ruby_metrics.ascent()
    path, rect = _ruby_text_path_and_rect(
        reading,
        ruby_font,
        ruby_metrics,
        pad_left,
        local_baseline,
        layout.target_width,
        style,
        base_text=layout.ruby.kanji,
    )
    fill_rect = layout.gradient_rect.translated(
        -float(layout.x) + float(pad_left),
        -float(layout.baseline_y) + float(local_baseline),
    )
    horizontal_fill_rect = (
        layout.horizontal_gradient_rect.translated(
            -float(layout.x) + float(pad_left),
            -float(layout.baseline_y) + float(local_baseline),
        )
        if layout.horizontal_gradient_rect is not None
        else None
    )

    p = QPainter(image)
    try:
        p.setRenderHints(
            QPainter.RenderHint.Antialiasing
            | QPainter.RenderHint.TextAntialiasing
            | QPainter.RenderHint.SmoothPixmapTransform
        )
        _paint_glow_path(
            p,
            path,
            state.shadow,
            _fill_brush_rect(state.shadow, fill_rect, horizontal_fill_rect),
            glow_radius,
            stroke_width,
            stroke2_width,
            concentration_level=_ruby_glow_concentration_level(style),
        )
    finally:
        p.end()

    return image, -pad_left, -(pad_top + ruby_metrics.ascent())


def _ruby_text_rect(layout: _RubyLayout, ruby_metrics: QFontMetrics) -> QRectF:
    left_offset = _ruby_layout_left_offset(
        layout.ruby.reading,
        ruby_metrics,
        layout.target_width,
        layout.style,
        layout.ruby.kanji,
    )
    return QRectF(
        float(layout.x + left_offset),
        float(layout.baseline_y - ruby_metrics.ascent()),
        float(layout.reading_width),
        float(ruby_metrics.height()),
    )


def _ruby_after_clip_rect(
    layout: _RubyLayout,
    ruby_metrics: QFontMetrics,
    style: Style,
    rtl: bool,
    ratio: float,
) -> QRectF:
    rect = _ruby_text_rect(layout, ruby_metrics)
    stroke_width = _ruby_stroke_width(style)
    stroke2_width = _ruby_stroke2_width(style)
    shadow_dx = _ruby_shadow_dx(style)
    shadow_dy = _ruby_shadow_dy(style)
    after_glow_radius = _ruby_glow_radius(style, after=True)
    stroke_extent = _visual_stroke_extent(stroke_width, stroke2_width)
    pad = max(
        stroke_extent,
        _glow_extent(stroke_width, stroke2_width, after_glow_radius)
        if _ruby_decoration_kind(style) == "glow"
        else 0,
        stroke_extent + abs(shadow_dx),
        stroke_extent + abs(shadow_dy),
        2,
    )
    ratio_c = min(ratio, 1.0)
    clip_left = rect.left() + (rect.width() * (1.0 - ratio_c) if rtl else 0.0) - pad
    return QRectF(
        clip_left,
        rect.top() - pad,
        rect.width() * ratio_c + pad,
        rect.height() + pad * 2,
    )


def _ruby_wipe_state(
    layout: _RubyLayout,
    t_ms: int,
) -> tuple[bool, bool, float]:
    """Return ``(visible, complete, front)`` for glyph-geometry ruby wipe."""
    segments = layout.wipe_segments
    if not segments:
        ratio = _ruby_progress_ratio(layout.ruby, t_ms)
        front = layout.wipe_left + (layout.wipe_right - layout.wipe_left) * ratio
        return ratio > 0.0, ratio >= 1.0, front
    return _ruby_segment_wipe_state(segments, layout.ruby.pos_end_ms, t_ms)


def _ruby_segment_wipe_state(
    segments: tuple[_RubyWipeSegment, ...],
    pos_end_ms: int,
    t_ms: int,
) -> tuple[bool, bool, float]:
    """Evaluate timed glyph-axis segments, including empty-part pauses."""
    first = segments[0]
    if t_ms <= first.start_ms:
        return False, False, first.axis_start
    previous_front = first.axis_start
    for segment in segments:
        if t_ms < segment.start_ms:
            return True, False, previous_front
        if t_ms < segment.end_ms:
            duration = segment.end_ms - segment.start_ms
            local = (t_ms - segment.start_ms) / duration if duration > 0 else 1.0
            front = segment.axis_start + (segment.axis_end - segment.axis_start) * local
            return True, False, front
        previous_front = segment.axis_end
    complete = t_ms >= max(int(pos_end_ms), segments[-1].end_ms)
    return True, complete, previous_front


def _ruby_after_clip_rect_at_time(
    layout: _RubyLayout,
    ruby_metrics: QFontMetrics,
    style: Style,
    rtl: bool,
    t_ms: int,
) -> QRectF:
    """Clip the after layer at the current ruby glyph front, not its box ratio."""
    _visible, _complete, front = _ruby_wipe_state(layout, t_ms)
    rect = _ruby_text_rect(layout, ruby_metrics)
    stroke_width = _ruby_stroke_width(style)
    stroke2_width = _ruby_stroke2_width(style)
    shadow_dx = _ruby_shadow_dx(style)
    shadow_dy = _ruby_shadow_dy(style)
    after_glow_radius = _ruby_glow_radius(style, after=True)
    stroke_extent = _visual_stroke_extent(stroke_width, stroke2_width)
    pad = max(
        stroke_extent,
        _glow_extent(stroke_width, stroke2_width, after_glow_radius)
        if _ruby_decoration_kind(style) == "glow"
        else 0,
        stroke_extent + abs(shadow_dx),
        stroke_extent + abs(shadow_dy),
        2,
    )
    wipe_left = layout.wipe_left if layout.wipe_segments else rect.left()
    wipe_right = layout.wipe_right if layout.wipe_segments else rect.right()
    if rtl:
        left = min(max(front, wipe_left), wipe_right)
        return QRectF(
            left - pad,
            rect.top() - pad,
            max(wipe_right - left, 0.0) + pad,
            rect.height() + pad * 2,
        )
    right = min(max(front, wipe_left), wipe_right)
    return QRectF(
        wipe_left - pad,
        rect.top() - pad,
        max(right - wipe_left, 0.0) + pad,
        rect.height() + pad * 2,
    )


def _ruby_before_clip_rect_at_time(
    layout: _RubyLayout,
    ruby_metrics: QFontMetrics,
    style: Style,
    rtl: bool,
    t_ms: int,
) -> QRectF:
    """Keep the before ruby glow only on the unsung side of the wipe front."""
    _visible, _complete, front = _ruby_wipe_state(layout, t_ms)
    rect = _ruby_text_rect(layout, ruby_metrics)
    stroke_width = _ruby_stroke_width(style)
    stroke2_width = _ruby_stroke2_width(style)
    pad = max(
        _visual_stroke_extent(stroke_width, stroke2_width),
        _glow_extent(
            stroke_width,
            stroke2_width,
            _ruby_glow_radius(style, after=False),
        ),
        2,
    )
    wipe_left = layout.wipe_left if layout.wipe_segments else rect.left()
    wipe_right = layout.wipe_right if layout.wipe_segments else rect.right()
    front = min(max(front, wipe_left), wipe_right)
    if rtl:
        return QRectF(
            wipe_left - pad,
            rect.top() - pad,
            max(front - wipe_left, 0.0) + pad,
            rect.height() + pad * 2,
        )
    return QRectF(
        front,
        rect.top() - pad,
        max(wipe_right - front, 0.0) + pad,
        rect.height() + pad * 2,
    )


def _paint_ruby_text_units_with_transition(
    painter: QPainter,
    ruby: RubyAnnotation,
    ruby_font: QFont,
    ruby_metrics: QFontMetrics,
    x: int,
    baseline_y: int,
    t_ms: int,
    style: Style,
    transition: _LineCharTransition,
    char_index: int,
    char_count: int,
    following_done_ms: int | None,
    rtl: bool = False,
    target_width: int | float | None = None,
    gradient_rect: QRectF | None = None,
    horizontal_gradient_rect: QRectF | None = None,
) -> None:
    visual_units = _ruby_visual_units_and_intervals(ruby)
    # RTL：按音节反转排布顺序，使首音节落在最右；各音节计时不变。
    if rtl:
        visual_units = list(reversed(visual_units))
    units = [unit for unit, _interval in visual_units]
    intervals = [interval for _unit, interval in visual_units]
    if not units or len(units) != len(intervals):
        _paint_ruby_text(
            painter,
            ruby,
            ruby_font,
            ruby_metrics,
            x,
            baseline_y,
            t_ms,
            style,
            rtl,
            target_width=target_width,
            gradient_rect=gradient_rect,
            horizontal_gradient_rect=horizontal_gradient_rect,
        )
        return

    layout_units = _ruby_layout_units(
        units, ruby_metrics, x, target_width, style=style, base_text=ruby.kanji
    )
    for (unit, unit_x, unit_width), (start_ms, end_ms) in zip(layout_units, intervals):
        opacity, dx, dy, rotation, scale_x, scale_y, skew_y = _transition_char_state(
            style,
            transition,
            char_index,
            char_count,
            char_start_ms=start_ms,
            char_end_ms=end_ms,
            t_ms=t_ms,
            frame_height=painter.device().height(),
            following_done_ms=following_done_ms,
        )
        if opacity > 0.0:
            painter.save()
            try:
                painter.setOpacity(painter.opacity() * opacity)
                transform = _character_transform(
                    center_x=unit_x + unit_width / 2,
                    center_y=baseline_y - ruby_metrics.ascent() + ruby_metrics.height() / 2,
                    dx=dx,
                    dy=dy,
                    rotation=rotation,
                    scale_x=scale_x,
                    scale_y=scale_y,
                    skew_y=skew_y,
                    scale_origin_x=unit_x,
                    scale_origin_y=baseline_y,
                )
                _paint_ruby_text_fragment(
                    painter,
                    unit,
                    ruby_font,
                    ruby_metrics,
                    unit_x,
                    baseline_y,
                    char_fill_ratio(start_ms, end_ms, t_ms),
                    style,
                    rtl,
                    transform=transform,
                    gradient_rect=gradient_rect,
                    horizontal_gradient_rect=horizontal_gradient_rect,
                )
            finally:
                painter.restore()


def _paint_ruby_text(
    painter: QPainter,
    ruby: RubyAnnotation,
    ruby_font: QFont,
    ruby_metrics: QFontMetrics,
    x: int,
    baseline_y: int,
    t_ms: int,
    style: Style,
    rtl: bool = False,
    target_width: int | float | None = None,
    gradient_rect: QRectF | None = None,
    horizontal_gradient_rect: QRectF | None = None,
    wipe_layout: _RubyLayout | None = None,
) -> None:
    # RTL：按可见字形反转读音——小书き假名(ゃゅょ等)是独立字形，也要反过来；
    # 只有零宽浊点/半浊点(゙゚)留在基字后。直接 reading[::-1] 会让浊点
    # 漂移，所以用 _ruby_utopia_visual_units 切分后反转。
    reading = (
        "".join(reversed(_ruby_utopia_visual_units(ruby.reading))) if rtl else ruby.reading
    )
    path, rect = _ruby_text_path_and_rect(
        reading,
        ruby_font,
        ruby_metrics,
        x,
        baseline_y,
        target_width,
        style,
        base_text=ruby.kanji,
    )
    _paint_ruby_karaoke_path(
        painter,
        path,
        rect,
        ruby,
        t_ms,
        style,
        rtl,
        ruby_metrics,
        gradient_rect=gradient_rect,
        horizontal_gradient_rect=horizontal_gradient_rect,
        wipe_layout=wipe_layout,
    )


def _ruby_text_path_and_rect(
    reading: str,
    ruby_font: QFont,
    ruby_metrics: QFontMetrics,
    x: int | float,
    baseline_y: int | float,
    target_width: int | float | None,
    style: Style | None = None,
    base_text: str | None = None,
) -> tuple[QPainterPath, QRectF]:
    path = QPainterPath()
    if target_width is None:
        path.addText(float(x), float(baseline_y), ruby_font, reading)
        width = ruby_metrics.horizontalAdvance(reading)
        return path, QRectF(
            float(x),
            float(baseline_y - ruby_metrics.ascent()),
            float(width),
            float(ruby_metrics.height()),
        )

    units = _ruby_utopia_visual_units(reading)
    layout_units = _ruby_layout_units(
        units, ruby_metrics, x, target_width, style=style, base_text=base_text
    )
    for unit, unit_x, _unit_width in layout_units:
        path.addText(float(unit_x), float(baseline_y), ruby_font, unit)
    layout_width = _ruby_layout_width(
        reading, ruby_metrics, target_width, style=style, base_text=base_text
    )
    layout_left = float(x) + _ruby_layout_left_offset(
        reading,
        ruby_metrics,
        target_width,
        style,
        base_text,
    )
    return path, QRectF(
        layout_left,
        float(baseline_y - ruby_metrics.ascent()),
        float(layout_width),
        float(ruby_metrics.height()),
    )


def _paint_ruby_text_fragment(
    painter: QPainter,
    text: str,
    ruby_font: QFont,
    ruby_metrics: QFontMetrics,
    x: int | float,
    baseline_y: int | float,
    ratio: float,
    style: Style,
    rtl: bool = False,
    transform: QTransform | None = None,
    gradient_rect: QRectF | None = None,
    horizontal_gradient_rect: QRectF | None = None,
) -> None:
    path = QPainterPath()
    path.addText(float(x), float(baseline_y), ruby_font, text)
    rect = QRectF(
        float(x),
        float(baseline_y - ruby_metrics.ascent()),
        float(ruby_metrics.horizontalAdvance(text)),
        float(ruby_metrics.height()),
    )
    if transform is not None and not transform.isIdentity():
        path = transform.map(path)
        rect = path.boundingRect()
    _paint_ruby_karaoke_fragment(
        painter,
        path,
        rect,
        ratio,
        style,
        rtl,
        fill_rect=gradient_rect,
        horizontal_fill_rect=horizontal_gradient_rect,
    )


def _paint_ruby_karaoke_path(
    painter: QPainter,
    path: QPainterPath,
    rect: QRectF,
    ruby: RubyAnnotation,
    t_ms: int,
    style: Style,
    rtl: bool = False,
    ruby_metrics: QFontMetrics | None = None,
    gradient_rect: QRectF | None = None,
    horizontal_gradient_rect: QRectF | None = None,
    wipe_layout: _RubyLayout | None = None,
) -> None:
    after_clip_rect = None
    before_glow_clip_rect = None
    if wipe_layout is not None and ruby_metrics is not None:
        visible, complete, _front = _ruby_wipe_state(wipe_layout, t_ms)
        if not visible:
            ratio = 0.0
        elif complete:
            ratio = 1.0
        else:
            # 走字进行中：before / after 两层都要画（此前强制 ratio=1.0 会把
            # before 层整个跳过，未唱读音在过渡窗口内消失）。实际几何完全由
            # 段式 front 的两个 clip rect 决定，中间 ratio 只用于让 fragment
            # 同时走两层的分支。
            ratio = 0.5
            after_clip_rect = _ruby_after_clip_rect_at_time(
                wipe_layout, ruby_metrics, style, rtl, t_ms
            )
            if _ruby_glow_states_differ(style):
                before_glow_clip_rect = _ruby_before_clip_rect_at_time(
                    wipe_layout, ruby_metrics, style, rtl, t_ms
                )
    else:
        ratio = _ruby_progress_ratio(ruby, t_ms, ruby_metrics)
    _paint_ruby_karaoke_fragment(
        painter,
        path,
        rect,
        ratio,
        style,
        rtl,
        fill_rect=gradient_rect,
        horizontal_fill_rect=horizontal_gradient_rect,
        after_clip_rect=after_clip_rect,
        before_glow_clip_rect=before_glow_clip_rect,
    )


def _paint_ruby_karaoke_fragment(
    painter: QPainter,
    path: QPainterPath,
    rect: QRectF,
    ratio: float,
    style: Style,
    rtl: bool = False,
    fill_rect: QRectF | None = None,
    horizontal_fill_rect: QRectF | None = None,
    after_clip_rect: QRectF | None = None,
    before_glow_clip_rect: QRectF | None = None,
) -> None:
    colors = _effective_ruby_karaoke_colors(style)
    paint_style = _ruby_paint_style(style)
    stroke_width = _ruby_stroke_width(style)
    stroke2_width = _ruby_stroke2_width(style)
    shadow_dx = _ruby_shadow_dx(style)
    shadow_dy = _ruby_shadow_dy(style)
    before_glow_radius = _ruby_glow_radius(style, after=False)
    after_glow_radius = _ruby_glow_radius(style, after=True)
    glow_states_differ = _ruby_glow_states_differ(style)

    # N3 clips before/after outline sources at WipeLeft and blurs afterwards.
    # The sharp colour boundary therefore stays on the ruby ink/edge while the
    # two soft halos may blend across it.
    clip_before_glow = ratio > 0.0 and glow_states_differ and (
        before_glow_radius > 0 or after_glow_radius > 0
    )
    if clip_before_glow and ratio < 1.0:
        if before_glow_clip_rect is not None:
            front = (
                before_glow_clip_rect.right()
                if rtl
                else before_glow_clip_rect.left()
            )
        else:
            front = rect.left() + rect.width() * (1.0 - ratio if rtl else ratio)
        before_pad = _glow_extent(
            stroke_width, stroke2_width, before_glow_radius
        )
        before_source_clip = (
            QRectF(
                -1_000_000.0,
                rect.top() - before_pad,
                front + 1_000_000.0,
                rect.height() + before_pad * 2,
            )
            if rtl
            else QRectF(
                front,
                rect.top() - before_pad,
                1_000_000.0,
                rect.height() + before_pad * 2,
            )
        )
        _paint_glow_path(
            painter,
            path,
            colors.before.shadow,
            _fill_brush_rect(
                colors.before.shadow,
                fill_rect if fill_rect is not None else rect,
                horizontal_fill_rect,
            ),
            before_glow_radius,
            stroke_width,
            stroke2_width,
            source_clip=before_source_clip,
            concentration_level=_glow_concentration_level(paint_style),
        )
        after_pad = _glow_extent(
            stroke_width, stroke2_width, after_glow_radius
        )
        after_source_clip = (
            QRectF(
                front,
                rect.top() - after_pad,
                1_000_000.0,
                rect.height() + after_pad * 2,
            )
            if rtl
            else QRectF(
                -1_000_000.0,
                rect.top() - after_pad,
                front + 1_000_000.0,
                rect.height() + after_pad * 2,
            )
        )
        _paint_glow_path(
            painter,
            path,
            colors.after.shadow,
            _fill_brush_rect(
                colors.after.shadow,
                fill_rect if fill_rect is not None else rect,
                horizontal_fill_rect,
            ),
            after_glow_radius,
            stroke_width,
            stroke2_width,
            source_clip=after_source_clip,
            concentration_level=_glow_concentration_level(paint_style),
        )

    if ratio < 1.0:
        _paint_text_layer_stack(
            painter,
            path,
            rect,
            colors.before,
            paint_style,
            stroke_width=stroke_width,
            stroke2_width=stroke2_width,
            shadow_dx=shadow_dx,
            shadow_dy=shadow_dy,
            glow_radius=before_glow_radius,
            draw_glow=not clip_before_glow,
            fill_rect=fill_rect,
            horizontal_fill_rect=horizontal_fill_rect,
        )

    if ratio <= 0.0:
        return

    painter.save()
    try:
        if ratio < 1.0 or after_clip_rect is not None:
            stroke_extent = _visual_stroke_extent(stroke_width, stroke2_width)
            pad = max(
                stroke_extent,
                _glow_extent(stroke_width, stroke2_width, after_glow_radius)
                if _ruby_decoration_kind(style) == "glow"
                else 0,
                stroke_extent + abs(shadow_dx),
                stroke_extent + abs(shadow_dy),
                2,
            )
            # RTL：已唱区贴读音右缘，左缘（扫光线）随进度左移。前缘必须停在
            # 扫光线本身，pad 只外扩尾缘/上下缘（LTR 尾缘在左，RTL 在右）。
            if after_clip_rect is None:
                if rtl:
                    front = rect.left() + rect.width() * (1.0 - ratio)
                    after_clip_rect = QRectF(
                        front,
                        rect.top() - pad,
                        rect.width() * ratio + pad,
                        rect.height() + pad * 2,
                    )
                else:
                    after_clip_rect = QRectF(
                        rect.left() - pad,
                        rect.top() - pad,
                        rect.width() * ratio + pad,
                        rect.height() + pad * 2,
                    )
            painter.setClipRect(after_clip_rect)
        # ratio >= 1.0：唱完不再裁剪——裁剪带右缘恰好压在字框右缘，
        # 会把末字形的描边外扩留在走字前状态。
        _paint_text_layer_stack(
            painter,
            path,
            rect,
            colors.after,
            paint_style,
            stroke_width=stroke_width,
            stroke2_width=stroke2_width,
            shadow_dx=shadow_dx,
            shadow_dy=shadow_dy,
            glow_radius=after_glow_radius,
            # 前后发光相同时未唱发光已铺满整读音，再叠只会加亮已唱带；
            # ratio>=1 时未唱层未画，已唱发光必须自己画。
            draw_glow=ratio >= 1.0,
            fill_rect=fill_rect,
            horizontal_fill_rect=horizontal_fill_rect,
        )
    finally:
        painter.restore()


def _paint_text_layer_stack(
    painter: QPainter,
    path: QPainterPath,
    rect: QRectF,
    colors: KaraokeColorState,
    style: Style,
    *,
    stroke_width: int,
    stroke2_width: int,
    shadow_dx: int,
    shadow_dy: int,
    glow_radius: int,
    draw_glow: bool = True,
    fill_rect: QRectF | None = None,
    horizontal_fill_rect: QRectF | None = None,
    draw_shadow: bool = True,
) -> None:
    brush_rect = fill_rect if fill_rect is not None else rect
    if style.decoration_kind == "glow":
        # ``draw_glow=False`` 让调用方把发光单独按「发光级」宽松裁切处理（卡拉ok 走字
        # 时发光软晕不能跟描边/填充一样按字框硬裁，否则会被裁成方框）。
        if draw_glow:
            _paint_glow_path(
                painter,
                path,
                colors.shadow,
                _fill_brush_rect(colors.shadow, brush_rect, horizontal_fill_rect),
                glow_radius,
                stroke_width,
                stroke2_width,
                concentration_level=_glow_concentration_level(style),
            )
    elif (
        style.decoration_kind == "shadow"
        and draw_shadow
        and (shadow_dx or shadow_dy)
    ):
        _paint_shadow_silhouette(
            painter,
            path,
            colors.shadow,
            _fill_brush_rect(colors.shadow, brush_rect, horizontal_fill_rect),
            shadow_dx,
            shadow_dy,
            stroke_width,
            stroke2_width,
        )

    if stroke2_width > 0:
        _paint_stroke_path(
            painter,
            path,
            colors.stroke2,
            _fill_brush_rect(colors.stroke2, brush_rect, horizontal_fill_rect),
            _stroke2_pen_width(stroke_width, stroke2_width),
        )
    if stroke_width > 0:
        _paint_stroke_path(
            painter,
            path,
            colors.stroke,
            _fill_brush_rect(colors.stroke, brush_rect, horizontal_fill_rect),
            _stroke_pen_width(stroke_width),
            protect_body=_fill_is_alpha(colors.text),
        )
    _paint_fill_path(
        painter,
        path,
        colors.text,
        _fill_brush_rect(colors.text, brush_rect, horizontal_fill_rect),
    )


_VERTICAL_RASTER_PORTS = VerticalRasterPorts(
    paint_text_layer_stack=_paint_text_layer_stack,
    visual_stroke_extent=_visual_stroke_extent,
    glow_extent=_glow_extent,
)

def _effective_ruby_karaoke_colors(style: Style) -> KaraokeColors:
    if style.ruby_karaoke_colors is not None:
        return style.ruby_karaoke_colors
    if style.karaoke_colors is not None:
        return style.karaoke_colors
    before = KaraokeColorState(
        text=_solid_fill(style.base_color),
        stroke=_solid_fill(style.stroke_color),
        stroke2=_solid_fill("#000000"),
        shadow=_solid_fill(style.shadow_color),
    )
    after = KaraokeColorState(
        text=_solid_fill(style.ruby_color),
        stroke=_solid_fill(style.stroke_color),
        stroke2=_solid_fill("#000000"),
        shadow=_solid_fill(style.shadow_color),
    )
    return KaraokeColors(before=before, after=after)


_VERTICAL_RUBY_PORTS = VerticalRubyPorts(
    raster=_VERTICAL_RASTER_PORTS,
    cache=_VERTICAL_CACHE_PORTS,
    paint_style=_ruby_paint_style,
    shadow_dx=_ruby_shadow_dx,
    shadow_dy=_ruby_shadow_dy,
    glow_radius=_ruby_glow_radius,
    decoration_kind=_ruby_decoration_kind,
    effective_colors=_effective_ruby_karaoke_colors,
)

_VERTICAL_LAYER_PORTS = VerticalLayerPorts(
    progress=_VERTICAL_PROGRESS_PORTS,
    raster=_VERTICAL_RASTER_PORTS,
    cache=_VERTICAL_CACHE_PORTS,
    main_stroke2_width=_main_stroke2_width,
    glow_radius=_glow_radius,
    ruby=_VERTICAL_RUBY_PORTS,
)
