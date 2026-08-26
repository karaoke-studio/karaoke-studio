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
from dataclasses import dataclass
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
    paint_glow_path as _paint_glow_path,
    paint_shadow_silhouette as _paint_shadow_silhouette,
    paint_split_glow_path as _paint_split_glow_path,
    paint_stroke_path as _paint_stroke_path,
    paint_text_layer_stack as _paint_text_layer_stack,
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
)
from krok_helper.subtitle_render.engine.render.core.cache_keys import (
    layout_cache_signature as _layout_cache_sig,
    line_layout_signature as _line_layout_signature,
    track_layout_signature as _track_layout_signature,
)
from krok_helper.subtitle_render.engine.render.frame_analysis import (
    FrameAnalysisPorts,
    frame_content_intervals as _analyze_frame_content_intervals,
    frame_has_content as _analyze_frame_has_content,
    frame_vertical_bounds as _analyze_frame_vertical_bounds,
)
from krok_helper.subtitle_render.engine.value_signature import (
    value_signature as _value_signature,
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
    CollisionLineGeometry,
    DisplayResolutionPorts,
    StyleDisplayResolutionPorts,
    apply_animation_time_guard,
    build_measured_collision_bands as _build_measured_collision_bands,
    clear_display_line_resolution_cache,
    collision_squeeze_pairs as _collision_squeeze_pairs,
    display_line_collision_time_window as _display_line_collision_time_window,
    display_line_compute_kwargs,
    display_line_static_collision_window as _display_line_static_collision_window,
    fill_section_time_from_measurements as _fill_section_time_from_measurements,
    retime_measured_collision_bands as _retime_measured_collision_bands,
    resolve_display_lines_for_style,
    resolve_display_timing,
    secondary_displacement_squeeze_pairs as _resolve_secondary_displacement_pairs,
)
from krok_helper.subtitle_render.sources.guide_symbols import scaled_guide_symbol_path
from krok_helper.subtitle_render.n3.font_catalog import resolve_qt_font_family


# 横排 glyph run 层缓存：普通行与分色行都按连续同 style 的 run 烘焙。
# 每个 run 的「未唱」层（含 before-glow）、「已唱」主体层与 after-glow
# 各烘焙一次；逐帧只按扫光半平面 clip blit。
_TEXT_RUN_LAYER_CACHE = LayerCache(max_items=128)
_TEXT_RUN_COMPOSITOR = LayerCompositor(_TEXT_RUN_LAYER_CACHE)
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


def clear_before_layer_cache() -> None:
    """测试 / 调试用：把字幕层位图缓存全部丢掉。"""
    _clear_fill_caches()
    _TEXT_RUN_LAYER_CACHE.clear()
    _clear_utopia_glow_cache()
    clear_char_metric_cache()
    _RUBY_MEASURE_CACHE.clear()
    _RUBY_UNIT_LAYOUT_CACHE.clear()
    _LINE_LAYOUT_CACHE.clear()
    clear_display_line_resolution_cache()
    clear_track_layout_plan_cache()
    clear_page_offset_cache()


from krok_helper.subtitle_render.engine.timing.timeline import (
    DisplayLine,
    assign_lanes,
    char_fill_ratio,
    compute_char_intervals,
    compute_display_lines,
    track_duration_ms,
)
from krok_helper.subtitle_render.engine.layout.page.plan import (
    resolve_page_plan,
)
from krok_helper.subtitle_render.engine.layout.page.placement import (
    LineVisualBand,
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
    character_fill_ratio as _character_fill_ratio,
    is_utopia_group_marker as _is_utopia_group_marker,
    resolve_char_ruby_groups as _resolve_char_ruby_groups,
    ruby_for_char_index as _ruby_for_char_index,
    ruby_main_uses_base_timing as _ruby_main_uses_base_timing,
    utopia_main_group_for_index as _utopia_main_group_for_index,
    utopia_wipe_window_for_index as _utopia_wipe_window_for_index,
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
    paint_title_text_stack as _paint_title_text_stack,
    paint_title_overlay as _paint_title_overlay_with_ports,
    title_block_origin as _title_block_origin,
)
from krok_helper.subtitle_render.engine.render.elements.horizontal import (
    BitmapGuideLayer as _HorizontalBitmapGuideLayer,
    BitmapGuidePorts,
    HORIZONTAL_GLYPH_LAYER_PORTS as _GLYPH_LAYER_PORTS,
    LayerStackPorts,
    TransitionLayerStackPorts,
    GlyphRunAfterGlowLayer as _HorizontalGlyphRunAfterGlowLayer,
    GlyphRunBeforeGlowLayer as _HorizontalGlyphRunBeforeGlowLayer,
    GlyphRunLayer as _HorizontalGlyphRunLayer,
    GlyphRunSplitGlowLayer as _HorizontalGlyphRunSplitGlowLayer,
    ScopeBoundsLayer as _ScopeBoundsLayer,
    UTOPIA_RUN_GLOW_CACHE as _RUN_GLOW_CACHE,
    CHAR_FADE_IN_TIME_MS as _CHAR_FADE_IN_TIME_MS,
    CHAR_FADE_INTRO_DELAY_MS as _CHAR_FADE_INTRO_DELAY_MS,
    CHAR_FADE_OUT_TIME_MS as _CHAR_FADE_OUT_TIME_MS,
    FillSegment as _FillSegment,
    HorizontalLayoutPorts,
    LineCharTransition as _LineCharTransition,
    LineLayout as _LineLayout,
    RubyLayout as _RubyLayout,
    RubyGlowLayer as _HorizontalRubyGlowLayer,
    RubyLayerPorts,
    RubyLayoutPorts,
    RubyStackPorts,
    RubySplitGlowLayer as _HorizontalRubySplitGlowLayer,
    RubyTextLayer as _HorizontalRubyTextLayer,
    RubyWipeSegment as _RubyWipeSegment,
    SayatooLineLayout as _SayatooLineLayout,
    after_glow_loose_clip_rect as _after_glow_loose_clip_rect,
    after_glow_source_clip_rect as _after_glow_source_clip_rect,
    afterglow_strip_enabled as _afterglow_strip_enabled,
    adjust_fill_release_edges as _adjust_fill_release_edges,
    build_glyph_run_after_glow_layer as _build_glyph_run_after_glow_layer,
    build_glyph_run_glow_layer as _build_glyph_run_glow_layer,
    build_glyph_run_layer as _build_glyph_run_layer,
    blit_cached_run_glow as _blit_cached_run_glow,
    blit_tinted_run_glow_mask as _blit_tinted_run_glow_mask,
    char_fade_opacity as _char_fade_opacity,
    char_drip_char_transform as _char_drip_char_transform,
    character_transform as _character_transform,
    line_char_transition_context as _line_char_transition_context,
    aligned_x0 as _aligned_x0,
    bitmap_guide_anchor_descent as _bitmap_guide_anchor_descent,
    bitmap_guide_band_for_glyph as _horizontal_bitmap_guide_band_for_glyph,
    bitmap_guide_band_for_segments as _horizontal_bitmap_guide_band_for_segments,
    bitmap_guide_glyphs as _bitmap_guide_glyphs,
    bitmap_guide_is_no_wipe as _bitmap_guide_is_no_wipe,
    bitmap_guide_target_rect as _horizontal_bitmap_guide_target_rect,
    bottom_short_page_alignment as _bottom_short_page_alignment,
    before_glow_source_clip_rect as _before_glow_source_clip_rect,
    char_transition_layer_stack as _build_char_transition_layer_stack,
    clear_utopia_glow_cache as _clear_utopia_glow_cache,
    clamp_role_baseline_y as _clamp_role_baseline_y,
    glyph_is_bitmap_guide as _glyph_is_bitmap_guide,
    glyph_path as _glyph_path,
    glyph_run_path as _glyph_run_path,
    glyph_run_rect as _glyph_run_rect,
    glyph_run_signature as _glyph_run_signature,
    glyph_run_after_glow_key as _glyph_run_after_glow_key,
    glyph_run_layer_key as _glyph_run_layer_key,
    glyph_run_can_combine_split_glow as _glyph_run_can_combine_split_glow,
    glyph_run_needs_after_glow as _glyph_run_needs_after_glow,
    glyph_run_needs_before_glow_split as _glyph_run_needs_before_glow_split,
    get_or_build_run_glow as _get_or_build_run_glow,
    get_or_build_run_glow_mask as _get_or_build_run_glow_mask,
    glyph_runs as _glyph_runs,
    glyph_runs_for_indices as _glyph_runs_for_indices,
    fixed_line_geometry as _fixed_line_geometry,
    fill_clip_band as _fill_clip_band,
    fill_clip_band_for_glyphs as _fill_clip_band_for_glyphs,
    fill_clip_band_for_indices as _fill_clip_band_for_indices,
    fill_extent_end as _fill_extent_end,
    fill_extent_left as _fill_extent_left,
    fill_extent_start as _fill_extent_start,
    lane_alignment as _lane_alignment,
    karaoke_fill_segments as _build_horizontal_karaoke_fill_segments,
    layout_page_lines as _layout_page_lines,
    layout_line_uncached as _build_horizontal_line_uncached,
    layout_plain_line as _build_horizontal_plain_line,
    layout_role_line as _build_horizontal_role_line,
    layout_rubies as _build_horizontal_ruby_layouts,
    measure_display_line_horizontal_bounds,
    line_total_width as _line_total_width,
    horizontal_after_clip_rect as _horizontal_after_clip_rect,
    horizontal_before_clip_rect as _horizontal_before_clip_rect,
    inflate_rect as _inflate_rect,
    karaoke_glow_states_differ as _karaoke_glow_states_differ,
    karaoke_state_uses_image as _karaoke_state_uses_image,
    line_lane_alignment as _line_lane_alignment,
    line_layer_stack as _build_horizontal_line_layer_stack,
    n3_smart_font_size as _n3_smart_font_size,
    n3_char_wipe_ranges_by_index as _n3_char_wipe_ranges_by_index,
    n3_main_fill_rect as _n3_main_fill_rect,
    n3_following_wipe_band as _n3_following_wipe_band,
    n3_transformed_wipe_span as _n3_transformed_wipe_span,
    n3_ruby_fill_rect as _n3_ruby_fill_rect,
    paint_ruby_karaoke_fragment as _paint_horizontal_ruby_karaoke_fragment,
    paint_glyph_run_after_glow_direct as _paint_glyph_run_after_glow_direct,
    paint_glyph_run_direct as _paint_glyph_run_direct,
    paint_line_direct as _paint_horizontal_line_direct,
    paint_bitmap_guide_glyph as _paint_horizontal_bitmap_guide_glyph,
    paint_bitmap_guide_glyphs as _paint_horizontal_bitmap_guide_glyphs,
    paint_bitmap_guide_transition_glyph as _paint_horizontal_bitmap_guide_transition_glyph,
    paint_cached_run_glow_source_wipe as _paint_cached_run_glow_source_wipe,
    paint_cached_run_split_glow_source_wipe as _paint_cached_run_split_glow_source_wipe,
    paint_char_karaoke_stack as _paint_char_karaoke_stack,
    paint_full_glow_source_wipe as _paint_full_glow_source_wipe,
    paint_glyph_run_after_glow_wipe as _paint_glyph_run_after_glow_wipe,
    paint_glyph_run_before_glow_direct as _paint_glyph_run_before_glow_direct,
    paint_glyph_run_combined_glow as _paint_glyph_run_combined_glow,
    offset_fill_segments as _offset_fill_segments,
    resolve_line_x as _resolve_line_x,
    resolve_line_x_smart as _resolve_line_x_smart,
    resolve_role_baseline_y as _resolve_role_baseline_y,
    relative_fill_rect_signature as _relative_fill_rect_signature,
    resolve_baseline_y as _resolve_baseline_y,
    resolve_display_baselines as _resolve_display_baselines,
    role_char_ink_ranges_by_index as _role_char_ink_ranges_by_index,
    role_glyphs_by_index as _role_glyphs_by_index,
    role_visual_text_padding as _role_visual_text_padding,
    role_ruby_vertical_extra as _role_ruby_vertical_extra,
    row_layout_params as _row_layout_params,
    run_fill_complete as _run_fill_complete,
    smart_horizontal_dx as _smart_horizontal_dx,
    segment_fill_ratio as _segment_fill_ratio,
    segment_wipe_band_at as _segment_wipe_band_at,
    segment_wipe_edges as _segment_wipe_edges,
    segment_wipe_times as _segment_wipe_times,
    spin_flip_skew as _spin_flip_skew,
    spin_flip_char_transform as _spin_flip_char_transform,
    ruby_after_clip_rect as _ruby_after_clip_rect,
    ruby_after_clip_rect_at_time as _ruby_after_clip_rect_at_time,
    ruby_before_clip_rect_at_time as _ruby_before_clip_rect_at_time,
    ruby_glow_layer_key as _ruby_glow_layer_key,
    ruby_glow_can_combine_split as _ruby_glow_can_combine_split,
    ruby_glow_states_differ as _ruby_glow_states_differ,
    ruby_glow_layers as _build_horizontal_ruby_glow_layers,
    ruby_horizontal_gradient_rect_signature as _ruby_horizontal_gradient_rect_signature,
    ruby_segment_wipe_state as _ruby_segment_wipe_state,
    ruby_text_path_and_rect as _ruby_text_path_and_rect,
    ruby_text_rect as _ruby_text_rect,
    ruby_text_layers as _build_horizontal_ruby_text_layers,
    ruby_text_layer_key as _ruby_text_layer_key,
    ruby_wipe_geometry as _ruby_wipe_geometry,
    ruby_wipe_state as _ruby_wipe_state,
    ruby_layer_stack as _build_horizontal_ruby_layer_stack,
    text_glyph_runs as _text_glyph_runs,
    transition_char_state as _transition_char_state,
    utopia_glow_cache_enabled as _glow_cache_enabled,
    utopia_main_scope_layers as _utopia_main_scope_layers,
    utopia_following_done_time as _utopia_following_done_time,
    utopia_ruby_scope_layers as _utopia_ruby_scope_layers,
    utopia_ruby_scope_rect as _utopia_ruby_scope_rect,
    utopia_scope_id as _utopia_scope_id,
)
from krok_helper.subtitle_render.engine.render.elements.vertical import (
    VerticalCachePorts,
    VerticalLayerPorts,
    VerticalLineLayout as _VerticalLineLayout,
    VerticalProgressPorts,
    VerticalRasterPorts,
    VerticalRubyPorts,
    layout_vertical_line as _layout_vertical_line,
    paint_line_vertical_direct as _paint_line_vertical_direct_with_ports,
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
    effective_ruby_karaoke_colors as _effective_ruby_karaoke_colors,
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


def _frame_title_vertical_bounds(
    logical_w: int,
    logical_h: int,
    track: TimingTrack,
    track_t_ms: int,
    style: Style,
    title_opacity: float,
) -> tuple[int, int] | None:
    resolved_title = resolve_title_overlay(style)
    title_layout = _layout_title_overlay(
        logical_w,
        logical_h,
        track,
        resolved_title,
        style=style,
    )
    if title_layout is None:
        return None
    return _TEXT_RUN_COMPOSITOR.vertical_bounds(
        LayerContext(
            t_ms=track_t_ms,
            logical_w=logical_w,
            logical_h=logical_h,
        ),
        [
            _make_title_overlay_layer(
                title_layout,
                resolved_title,
                title_opacity,
                ports=_TITLE_RENDER_PORTS,
            )
        ],
    )


def _frame_analysis_ports() -> FrameAnalysisPorts:
    """Bind frame queries to the current Painter adapters."""

    return FrameAnalysisPorts(
        resolve_visible_content=_resolve_visible_content,
        subtitle_vertical_bounds=_subtitle_lines_vertical_bounds,
        title_vertical_bounds=_frame_title_vertical_bounds,
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
    return _analyze_frame_has_content(
        track,
        t_ms,
        style,
        extra_tracks,
        duration_ms=duration_ms,
        logical_w=logical_w,
        logical_h=logical_h,
        ports=_frame_analysis_ports(),
    )


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
    return _analyze_frame_content_intervals(
        logical_w,
        logical_h,
        track,
        t_ms,
        style,
        extra_tracks,
        duration_ms=duration_ms,
        ports=_frame_analysis_ports(),
    )


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
    return _analyze_frame_vertical_bounds(
        logical_w,
        logical_h,
        track,
        t_ms,
        style,
        extra_tracks,
        duration_ms=duration_ms,
        ports=_frame_analysis_ports(),
    )


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

    geometries: list[CollisionLineGeometry | None] = []
    for display_line in display_lines:
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
            geometries.append(None)
            continue
        assert cross_bounds is not None
        geometries.append(
            CollisionLineGeometry(
                axis_min=float(axis_bounds[0]),
                axis_max=float(axis_bounds[1]),
                cross_min=float(cross_bounds[0]),
                cross_max=float(cross_bounds[1]),
                axis_anchor=(None if axis_anchor is None else float(axis_anchor)),
                gap_px=float(line_style.line_gap_px),
            )
        )
    return _build_measured_collision_bands(
        display_lines,
        style,
        geometries,
        time_window=time_window,
    )


def pixel_collision_squeeze_pairs(
    logical_w: int,
    logical_h: int,
    track: TimingTrack,
    style: Style,
    display_lines: list[DisplayLine],
) -> tuple[tuple[int, int], ...]:
    """Return pairs conflicting in the configured time window and pixel axis."""

    return _collision_squeeze_pairs(
        measure_collision_bands(
            logical_w,
            logical_h,
            track,
            style,
            display_lines,
        )
    )


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
    return _resolve_secondary_displacement_pairs(
        measured,
        display_lines,
        style,
        viewport_max=float(logical_w if style.vertical else logical_h),
    )


def _apply_measured_section_time_fill(
    logical_w: int,
    logical_h: int,
    track: TimingTrack,
    style: Style,
    display_lines: list[DisplayLine],
) -> list[DisplayLine]:
    """Bind Painter geometry to the layout-owned section-fill policy."""

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
    return _fill_section_time_from_measurements(
        display_lines,
        style,
        measured,
        viewport_max=float(logical_w if style.vertical else logical_h),
        time_window=time_window,
    )


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
    """Bind the legacy direct path to the vertical render owner."""

    _paint_line_vertical_direct_with_ports(
        painter,
        layout,
        line,
        t_ms,
        style,
        ports=_VERTICAL_LAYER_PORTS,
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
    return _build_horizontal_line_uncached(
        track,
        line,
        style,
        img_w,
        img_h,
        _HORIZONTAL_LAYOUT_PORTS,
        baseline_y=baseline_y,
        line_x=line_x,
        lane=lane,
        line_plan=line_plan,
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
    return _build_horizontal_plain_line(
        track,
        line,
        style,
        img_w,
        img_h,
        _HORIZONTAL_LAYOUT_PORTS,
        baseline_y=baseline_y,
        line_x=line_x,
        lane=lane,
        source_line=source_line,
        resolved_intervals=resolved_intervals,
        center_override=center_override,
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
    return _build_horizontal_role_line(
        track,
        line,
        style,
        img_w,
        img_h,
        _HORIZONTAL_LAYOUT_PORTS,
        baseline_y=baseline_y,
        line_x=line_x,
        lane=lane,
        source_line=source_line,
        resolved_intervals=resolved_intervals,
        center_override=center_override,
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
    """Bind the vector oracle to the horizontal layer owner."""

    _paint_horizontal_line_direct(
        painter,
        layout,
        t_ms,
        glyph_ports=_GLYPH_LAYER_PORTS,
        bitmap_ports=_BITMAP_GUIDE_PORTS,
    )


def _line_layer_stack(layout: _LineLayout, t_ms: int) -> list:
    return _build_horizontal_line_layer_stack(
        layout,
        t_ms,
        _HORIZONTAL_LAYER_STACK_PORTS,
    )


def _bitmap_guide_target_rect(
    glyph: _GlyphLayout,
    baseline_y: int,
) -> QRectF | None:
    return _horizontal_bitmap_guide_target_rect(glyph, baseline_y)


def _paint_bitmap_guide_glyph(
    painter: QPainter,
    glyph: _GlyphLayout,
    baseline_y: int,
    *,
    after: bool,
    band: tuple[int, int] | None,
    rtl: bool,
) -> None:
    _paint_horizontal_bitmap_guide_glyph(
        painter,
        glyph,
        baseline_y,
        after=after,
        band=band,
        rtl=rtl,
    )


def _bitmap_guide_band_for_segments(
    fill_segments: list[_FillSegment],
    glyph: _GlyphLayout,
    t_ms: int,
    rtl: bool,
) -> tuple[int, int] | None:
    return _horizontal_bitmap_guide_band_for_segments(
        fill_segments,
        glyph,
        t_ms,
        rtl,
        _BITMAP_GUIDE_PORTS,
    )


def _bitmap_guide_band_for_glyph(
    layout: _LineLayout,
    glyph: _GlyphLayout,
    t_ms: int,
) -> tuple[int, int] | None:
    return _horizontal_bitmap_guide_band_for_glyph(
        layout,
        glyph,
        t_ms,
        _BITMAP_GUIDE_PORTS,
    )


def _paint_bitmap_guide_glyphs(
    painter: QPainter,
    layout: _LineLayout,
    t_ms: int,
    *,
    after: bool,
) -> None:
    _paint_horizontal_bitmap_guide_glyphs(
        painter,
        layout,
        t_ms,
        _BITMAP_GUIDE_PORTS,
        after=after,
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
    _paint_horizontal_bitmap_guide_transition_glyph(
        painter,
        glyph,
        fill_segments,
        baseline_y,
        line,
        intervals,
        index,
        t_ms,
        transition,
        style,
        _BITMAP_GUIDE_PORTS,
        rtl=rtl,
    )


def _BitmapGuideLayer(
    glyph: _GlyphLayout,
    baseline_y: int,
    fill_segments: list,
    t_ms: int,
    rtl: bool,
    after: bool,
    z_index: int = 0,
    scope: str = SCOPE_LINE,
) -> _HorizontalBitmapGuideLayer:
    return _HorizontalBitmapGuideLayer(
        glyph=glyph,
        baseline_y=baseline_y,
        fill_segments=fill_segments,
        t_ms=t_ms,
        rtl=rtl,
        after=after,
        ports=_BITMAP_GUIDE_PORTS,
        z_index=z_index,
        scope=scope,
    )




def _char_transition_layer_stack(
    layout: _LineLayout,
    t_ms: int,
    transition: _LineCharTransition,
    char_count: int,
) -> list:
    return _build_char_transition_layer_stack(
        layout,
        t_ms,
        transition,
        char_count,
        _CHAR_TRANSITION_LAYER_STACK_PORTS,
    )


class _GlyphRunLayer(_HorizontalGlyphRunLayer):
    """Compatibility adapter that injects Painter raster ports."""

    def __init__(
        self,
        glyphs: list[_GlyphLayout],
        baseline_y: int,
        fill_segments: list[_FillSegment],
        t_ms: int,
        rtl: bool,
        after: bool,
        clip_band: tuple[int, int] | None = None,
        z_index: int = 0,
        scope: str = SCOPE_LINE,
        fade_opacity: float = 1.0,
        transform: QTransform | None = None,
        fill_rect: QRectF | None = None,
    ) -> None:
        super().__init__(
            glyphs=glyphs,
            baseline_y=baseline_y,
            fill_segments=fill_segments,
            t_ms=t_ms,
            rtl=rtl,
            after=after,
            ports=_GLYPH_LAYER_PORTS,
            clip_band=clip_band,
            z_index=z_index,
            scope=scope,
            fade_opacity=fade_opacity,
            transform=transform,
            fill_rect=fill_rect,
        )


class _GlyphRunBeforeGlowLayer(_HorizontalGlyphRunBeforeGlowLayer):
    """Compatibility adapter that injects Painter raster ports."""

    def __init__(
        self,
        glyphs: list[_GlyphLayout],
        baseline_y: int,
        fill_segments: list[_FillSegment],
        t_ms: int,
        rtl: bool,
        z_index: int = 0,
        scope: str = SCOPE_LINE,
        fade_opacity: float = 1.0,
        transform: QTransform | None = None,
        fill_rect: QRectF | None = None,
    ) -> None:
        super().__init__(
            glyphs=glyphs,
            baseline_y=baseline_y,
            fill_segments=fill_segments,
            t_ms=t_ms,
            rtl=rtl,
            ports=_GLYPH_LAYER_PORTS,
            z_index=z_index,
            scope=scope,
            fade_opacity=fade_opacity,
            transform=transform,
            fill_rect=fill_rect,
        )


class _GlyphRunSplitGlowLayer(_HorizontalGlyphRunSplitGlowLayer):
    """Compatibility adapter that injects Painter raster ports."""

    def __init__(
        self,
        glyphs: list[_GlyphLayout],
        baseline_y: int,
        fill_segments: list[_FillSegment],
        t_ms: int,
        rtl: bool,
        z_index: int = 0,
        scope: str = SCOPE_LINE,
        fill_rect: QRectF | None = None,
    ) -> None:
        super().__init__(
            glyphs=glyphs,
            baseline_y=baseline_y,
            fill_segments=fill_segments,
            t_ms=t_ms,
            rtl=rtl,
            ports=_GLYPH_LAYER_PORTS,
            z_index=z_index,
            scope=scope,
            fill_rect=fill_rect,
        )


class _GlyphRunAfterGlowLayer(_HorizontalGlyphRunAfterGlowLayer):
    """Compatibility adapter that injects Painter raster ports."""

    def __init__(
        self,
        glyphs: list[_GlyphLayout],
        baseline_y: int,
        fill_segments: list[_FillSegment],
        t_ms: int,
        rtl: bool,
        clip_band: tuple[int, int] | None = None,
        z_index: int = 0,
        scope: str = SCOPE_LINE,
        fade_opacity: float = 1.0,
        transform: QTransform | None = None,
        fill_rect: QRectF | None = None,
    ) -> None:
        super().__init__(
            glyphs=glyphs,
            baseline_y=baseline_y,
            fill_segments=fill_segments,
            t_ms=t_ms,
            rtl=rtl,
            ports=_GLYPH_LAYER_PORTS,
            clip_band=clip_band,
            z_index=z_index,
            scope=scope,
            fade_opacity=fade_opacity,
            transform=transform,
            fill_rect=fill_rect,
        )


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
    return _build_horizontal_karaoke_fill_segments(
        char_widths,
        intervals,
        ink_x_ranges,
        active_rubies,
        line,
        release_x_ranges=release_x_ranges,
        layout_x_ranges=layout_x_ranges,
        ruby_main_progress_mode=ruby_main_progress_mode,
    )


_BITMAP_GUIDE_PORTS = BitmapGuidePorts(
    fill_clip_band=lambda *args, **kwargs: _fill_clip_band(*args, **kwargs),
    fill_clip_band_for_glyphs=lambda *args, **kwargs: (
        _fill_clip_band_for_glyphs(*args, **kwargs)
    ),
    n3_following_wipe_band=lambda *args, **kwargs: (
        _n3_following_wipe_band(*args, **kwargs)
    ),
)


_HORIZONTAL_LAYER_STACK_PORTS = LayerStackPorts(
    bitmap_guide_layer=lambda *args, **kwargs: _BitmapGuideLayer(*args, **kwargs),
    fill_clip_band_for_glyphs=lambda *args, **kwargs: (
        _fill_clip_band_for_glyphs(*args, **kwargs)
    ),
    glyph_run_after_glow_layer=lambda *args, **kwargs: (
        _GlyphRunAfterGlowLayer(*args, **kwargs)
    ),
    glyph_run_before_glow_layer=lambda *args, **kwargs: (
        _GlyphRunBeforeGlowLayer(*args, **kwargs)
    ),
    glyph_run_layer=lambda *args, **kwargs: _GlyphRunLayer(*args, **kwargs),
    glyph_run_split_glow_layer=lambda *args, **kwargs: (
        _GlyphRunSplitGlowLayer(*args, **kwargs)
    ),
)


_CHAR_TRANSITION_LAYER_STACK_PORTS = TransitionLayerStackPorts(
    fill_clip_band_for_glyphs=lambda *args, **kwargs: (
        _fill_clip_band_for_glyphs(*args, **kwargs)
    ),
    glyph_run_after_glow_layer=lambda *args, **kwargs: (
        _GlyphRunAfterGlowLayer(*args, **kwargs)
    ),
    glyph_run_before_glow_layer=lambda *args, **kwargs: (
        _GlyphRunBeforeGlowLayer(*args, **kwargs)
    ),
    glyph_run_layer=lambda *args, **kwargs: _GlyphRunLayer(*args, **kwargs),
)


_VERTICAL_PROGRESS_PORTS = VerticalProgressPorts(
    resolve_char_ruby_groups=_resolve_char_ruby_groups,
    character_fill_ratio=_character_fill_ratio,
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


_RUBY_LAYOUT_PORTS = RubyLayoutPorts(
    ruby_wipe_geometry=lambda *args, **kwargs: (
        _ruby_wipe_geometry(*args, **kwargs)
    ),
)


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
    return _build_horizontal_ruby_layouts(
        ruby_metrics,
        line,
        intervals,
        char_x_ranges,
        main_baseline_y,
        rubies,
        style,
        _RUBY_LAYOUT_PORTS,
        main_ascent_px=main_ascent_px,
        text_layout=text_layout,
        ruby_font=ruby_font,
    )


_HORIZONTAL_LAYOUT_PORTS = HorizontalLayoutPorts(
    char_layout_width=lambda *args, **kwargs: _char_layout_width(*args, **kwargs),
    layout_rubies=_layout_rubies,
    role_ruby_vertical_extra=_role_ruby_vertical_extra,
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
    return _build_horizontal_ruby_text_layers(
        layouts,
        ruby_font,
        ruby_metrics,
        t_ms,
        style,
        rtl,
        _RUBY_STACK_PORTS,
        draw_glow=draw_glow,
    )


def _ruby_glow_layers(
    layouts: list[_RubyLayout],
    ruby_font: QFont,
    ruby_metrics: QFontMetrics,
    t_ms: int,
    style: Style,
    rtl: bool,
) -> list:
    return _build_horizontal_ruby_glow_layers(
        layouts,
        ruby_font,
        ruby_metrics,
        t_ms,
        style,
        rtl,
        _RUBY_STACK_PORTS,
    )


def _ruby_layer_stack(
    layout: _LineLayout,
    line: TimingLine,
    t_ms: int,
    style: Style,
) -> list:
    return _build_horizontal_ruby_layer_stack(
        layout,
        line,
        t_ms,
        style,
        _RUBY_STACK_PORTS,
    )


class _RubySplitGlowLayer(_HorizontalRubySplitGlowLayer):
    """Compatibility adapter that injects Painter ruby raster ports."""

    def __init__(
        self,
        ruby_layout: _RubyLayout,
        ruby_font: QFont,
        ruby_metrics: QFontMetrics,
        t_ms: int,
        style: Style,
        rtl: bool,
        z_index: int = 0,
        scope: str = SCOPE_LINE,
    ) -> None:
        super().__init__(
            ruby_layout=ruby_layout,
            ruby_font=ruby_font,
            ruby_metrics=ruby_metrics,
            t_ms=t_ms,
            style=style,
            rtl=rtl,
            ports=_RUBY_LAYER_PORTS,
            z_index=z_index,
            scope=scope,
        )


class _RubyGlowLayer(_HorizontalRubyGlowLayer):
    """Compatibility adapter that injects Painter ruby raster ports."""

    def __init__(
        self,
        ruby_layout: _RubyLayout,
        ruby_font: QFont,
        ruby_metrics: QFontMetrics,
        t_ms: int,
        style: Style,
        rtl: bool,
        after: bool,
        z_index: int = 0,
        scope: str = SCOPE_LINE,
    ) -> None:
        super().__init__(
            ruby_layout=ruby_layout,
            ruby_font=ruby_font,
            ruby_metrics=ruby_metrics,
            t_ms=t_ms,
            style=style,
            rtl=rtl,
            after=after,
            ports=_RUBY_LAYER_PORTS,
            z_index=z_index,
            scope=scope,
        )


class _RubyTextLayer(_HorizontalRubyTextLayer):
    """Compatibility adapter that injects Painter ruby raster ports."""

    def __init__(
        self,
        ruby_layout: _RubyLayout,
        ruby_font: QFont,
        ruby_metrics: QFontMetrics,
        t_ms: int,
        style: Style,
        rtl: bool,
        after: bool,
        z_index: int = 0,
        scope: str = SCOPE_LINE,
        draw_glow: bool = True,
    ) -> None:
        super().__init__(
            ruby_layout=ruby_layout,
            ruby_font=ruby_font,
            ruby_metrics=ruby_metrics,
            t_ms=t_ms,
            style=style,
            rtl=rtl,
            after=after,
            ports=_RUBY_LAYER_PORTS,
            z_index=z_index,
            scope=scope,
            draw_glow=draw_glow,
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


_RUBY_LAYER_PORTS = RubyLayerPorts(
    blit_cached_ruby_glow=lambda *args, **kwargs: (
        _blit_cached_ruby_glow(*args, **kwargs)
    ),
    build_ruby_glow_layer=lambda *args, **kwargs: (
        _build_ruby_glow_layer(*args, **kwargs)
    ),
    build_ruby_text_layer=lambda *args, **kwargs: (
        _build_ruby_text_layer(*args, **kwargs)
    ),
    paint_split_glow_path=lambda *args, **kwargs: (
        _paint_split_glow_path(*args, **kwargs)
    ),
    paint_text_layer_stack=lambda *args, **kwargs: (
        _paint_text_layer_stack(*args, **kwargs)
    ),
    ruby_text_path_and_rect=lambda *args, **kwargs: (
        _ruby_text_path_and_rect(*args, **kwargs)
    ),
)


_RUBY_STACK_PORTS = RubyStackPorts(
    ruby_glow_layer=lambda *args, **kwargs: _RubyGlowLayer(*args, **kwargs),
    ruby_split_glow_layer=lambda *args, **kwargs: (
        _RubySplitGlowLayer(*args, **kwargs)
    ),
    ruby_text_layer=lambda *args, **kwargs: _RubyTextLayer(*args, **kwargs),
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
    _paint_horizontal_ruby_karaoke_fragment(
        painter,
        path,
        rect,
        ratio,
        style,
        _RUBY_LAYER_PORTS,
        rtl=rtl,
        fill_rect=fill_rect,
        horizontal_fill_rect=horizontal_fill_rect,
        after_clip_rect=after_clip_rect,
        before_glow_clip_rect=before_glow_clip_rect,
    )




_VERTICAL_RASTER_PORTS = VerticalRasterPorts(
    paint_text_layer_stack=_paint_text_layer_stack,
    visual_stroke_extent=_visual_stroke_extent,
    glow_extent=_glow_extent,
)

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
