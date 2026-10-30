"""Stable value signatures for caches backed by mutable project models."""

from __future__ import annotations

from dataclasses import fields as dataclass_fields, is_dataclass
from typing import Hashable

from krok_helper.subtitle_render.domain.timing import GuideSymbol
from krok_helper.subtitle_render.engine.layout.layout_context import _LAYOUT_PASS
from krok_helper.subtitle_render.engine.render_progress import yield_to_gui


_SIG_FIELD_NAMES_BY_TYPE: dict[type, tuple[str, ...]] = {}
_SIG_EXCLUDED_NAMES_BY_TYPE: dict[Hashable, tuple[str, ...]] = {}

_LYRIC_LAYOUT_TITLE_ONLY_EXCLUDED_STYLE_FIELDS = frozenset({
    "title_overlays",
    "hidden_builtin_layout_ids",
})
_LYRIC_LAYOUT_EXCLUDED_STYLE_FIELDS = _LYRIC_LAYOUT_TITLE_ONLY_EXCLUDED_STYLE_FIELDS | frozenset({
    "base_color",
    "fill_color",
    "fill_gradient_enabled",
    "fill_gradient_start_color",
    "fill_gradient_end_color",
    "fill_gradient_angle_deg",
    "stroke_color",
    "shadow_color",
    "karaoke_colors",
    "ruby_color",
    "ruby_colors_follow_main",
    "ruby_karaoke_colors",
    "lit_fill_color",
    "lit_image_path",
    "lit_stroke_color",
    "volume_fill_color",
    "volume_stroke_color",
    "volume_overlay_fill_color",
    "volume_overlay_stroke_color",
    # 指示灯/音量柱的纯绘制参数（大小/柱宽/闪烁/过渡/对齐/外观模式等）：
    # 灯组是行首叠加层，只在渲染期消费（render/elements/signal.py），碰撞
    # 包络只测纯主字形 path（_display_line_horizontal_ink_rect），不反向
    # 影响排版。时序字段刻意保留在签名里：lit_enabled / volume_enabled
    # 决定段首行是否有信号窗口（lit_signal_active），duration / waiting /
    # time_offset 决定窗口提前量（signal_lead_in_ms）——它们变了显示窗口
    # 与分页输入就真的变了。
    "lit_style",
    "lit_number",
    "lit_size",
    "lit_appearance_mode",
    "lit_role_name",
    "lit_auto_size_ratio_pct",
    "lit_offset_x",
    "lit_offset_y",
    "lit_tracking",
    "lit_stroke_width",
    "lit_stroke_soften",
    "lit_opacity_pct",
    "lit_edge_brightness_pct",
    "lit_shadow",
    "lit_transition_mode",
    "lit_transition_ratio_pct",
    "lit_transition_angle_deg",
    "lit_transition_distance",
    "volume_appearance_mode",
    "volume_role_name",
    "volume_auto_size_ratio_pct",
    "volume_auto_column_ratio_pct",
    "volume_size",
    "volume_offset_x",
    "volume_offset_y",
    "volume_column_width",
    "volume_column_count",
    "volume_column_spacing",
    "volume_align",
    "volume_ratio",
    "volume_stroke_width",
    "volume_opacity_pct",
    "volume_flash_times",
    "volume_flash_duration_ratio",
    "volume_transition_ratio_pct",
    # 渲染专属唱字/扫字线字段：不进入显示窗口、分页、排版任何输入
    # （调度只读出入场动画与时长/保护时间，见 display/schedule.py 与
    # layout/line/style.py）。布局计划缓存命中后由 _rebind_plan_line_styles
    # 逐行重解析动画样式，IR 仍拿到最新 karaoke_anim / 扫字线开关。
    # 注意：include_paint_fields=True 的调用方（CPU 行布局缓存）不用这份
    # 剔除表——fill_segments 内嵌 karaoke_effect，唱字档位变更必须作废。
    "karaoke_anim",
    "reverse_karaoke_anim",
    "scanline_enabled",
    "scanline_reverse_enabled",
    "scanline_forward_enabled",
    "scanline_width_px",
    "scanline_mode",
    "scanline_color",
    "scanline_role_name",
    "scanline_brightness_pct",
    "scanline_glow_px",
    "scanline_effect",
    "scanline_effect_size_pct",
    "scanline_effect_count",
    "scanline_effect_speed_pct",
    "scanline_shape",
    "scanline_image_path",
    "scanline_image_band_enabled",
    "scanline_image_offset_x_px",
    "scanline_image_offset_y_px",
    "scanline_image_color_layers",
    "scanline_effect_color_mode",
    "scanline_effect_color",
    "scanline_effect_color2",
    "scanline_effect_color_layers",
    "scanline_effect_role_name",
    "zoom_pulse_curve_level",
    # 2026-09 描边闪光与装饰粒子：纯绘制参数（IR 里随行 fx_bursts 下发），
    # 不影响显示窗口 / 分页 / 排版任何输入。
    "karaoke_stroke_flash",
    "entry_fx",
    "exit_fx",
    "sing_fx",
    "fx_particle_size_em",
    "fx_particle_count",
    "fx_particle_speed_pct",
    "fx_particle_color",
    "fx_particle_color2",
    "fx_particle_color_layers",
    "fx_particle_color_mode",
    "fx_particle_role_name",
    "fx_apply_to_entry_exit",
    "anim_fx_size_em",
    "anim_fx_speed_pct",
    "anim_fx_color",
    "anim_fx_color2",
    "anim_fx_color_mode",
    "anim_fx_color_layers",
    "anim_fx_role_name",
    # 编辑器 UI 开关（不影响渲染/排版）。
    "decoration_before_sync",
    # 2026-10 行背景带：纯装饰层，几何由行文本框 + 四边留白在渲染期求值，
    # 不参与显示窗口 / 分页 / 排版任何输入（带子永远跟着行盒走）。
    "line_band_enabled",
    "line_band_image",
    "line_band_dynamic_notes_enabled",
    "line_band_static_notes_enabled",
    "line_band_fill_mode",
    "line_band_cap_left_px",
    "line_band_cap_right_px",
    "line_band_mid_mode",
    "line_band_pad_top_px",
    "line_band_pad_bottom_px",
    "line_band_pad_left_px",
    "line_band_pad_right_px",
    "line_band_include_ruby",
    "line_band_offset_y_px",
    "line_band_opacity_pct",
    "line_band_tint",
    "line_band_tint_strength_pct",
    "line_band_line_color",
    "line_band_clef_color",
    "line_band_edge_kind",
    "line_band_shadow_enabled",
    "line_band_shadow_color",
    "line_band_shadow_size_px",
    "line_band_shadow_spread_pct",
    "line_band_shadow_distance_px",
    "line_band_shadow_angle_deg",
    "line_band_stroke_enabled",
    "line_band_stroke_color",
    "line_band_stroke_size_px",
    "line_band_stroke_position",
    "line_band_glow_enabled",
    "line_band_glow_color",
    "line_band_glow_size_px",
    "line_band_glow_spread_pct",
    "line_band_glow_range_pct",
    "line_band_edge_color",
    "line_band_edge_size_px",
    "line_band_edge_offset_x_px",
    "line_band_edge_offset_y_px",
    "line_band_edge_opacity_pct",
})
_LYRIC_LAYOUT_EXCLUDED_SCHEME_FIELDS = frozenset({
    "base_color",
    "fill_color",
    "fill_gradient_enabled",
    "fill_gradient_start_color",
    "fill_gradient_end_color",
    "fill_gradient_angle_deg",
    "stroke_color",
    "shadow_color",
    "ruby_color",
    "karaoke_colors",
    "ruby_colors_follow_main",
    "ruby_karaoke_colors",
    # 装饰（阴影/发光）与注音渐变共享是纯绘制字段：跨页避让的碰撞包络
    # 按纯主字形 path 测量（Ruby、描边、阴影、发光全部刻意排除），
    # 装饰参数不影响排版。
    "decoration_kind",
    "decoration_kind_before",
    "decoration_before_sync",
    "shadow_offset_x",
    "shadow_offset_y",
    "shadow_before_offset_x",
    "shadow_before_offset_y",
    "glow_radius_px",
    "glow_before_radius_px",
    "glow_after_radius_px",
    "glow_concentration_level",
    "glow_before_concentration_level",
    "ruby_decoration_kind",
    "ruby_decoration_kind_before",
    "ruby_shadow_offset_x",
    "ruby_shadow_offset_y",
    "ruby_shadow_before_offset_x",
    "ruby_shadow_before_offset_y",
    "ruby_glow_radius_px",
    "ruby_glow_before_radius_px",
    "ruby_glow_after_radius_px",
    "ruby_glow_concentration_level",
    "ruby_glow_before_concentration_level",
    "ruby_horizontal_gradient_with_main",
})
"""纯绘制字段：不参与歌词行的排版/分页/布局计划。

顶层清单还包含只喂标题层的字段。角色覆盖里的颜色需要递归剔除，否则
顶层颜色虽被忽略，嵌套的 ``SubtitleStyleScheme`` 仍会使整轨缓存失效。
剔除清单必须保守：字体、字号、描边宽和 ruby 尺寸等几何字段仍参与签名。
"""


def track_signature_for_windows(track) -> Hashable:
    """整轨签名，逐行动画覆盖按「窗口语义」归一化（P5 逐行动画差分）。

    显示窗口只消费覆盖的 ``(类型是否为 none, 时长)``（见
    ``_animation_windows_unchanged`` 与 ``entry_animation_ms``）与反向
    标记；``karaoke_anim`` / ``sing_fx`` 档位是纯渲染字段（不进任何窗口
    输入）。归一化后：

    - 同为非 none 的类型互换（fade↔slide 等，时长不变）→ 签名不变 →
      布局计划/显示窗口/差分闸门全部命中缓存，逐行样式经
      ``_rebind_plan_line_styles`` 从当前轨道重解析；
    - none↔非 none 或时长变化 → 签名变化 → 全量重排（窗口真的会动）。

    实现是浅拷贝行级覆盖（chars 列表按引用共享，签名树走既有的
    layout_pass memoize），不做整轨深拷贝。
    """
    from dataclasses import replace as _dc_replace

    from krok_helper.subtitle_render.domain.timing import TimingTrack

    if not isinstance(track, TimingTrack):
        return value_signature(track)

    def _normalized_override(override):
        if override is None:
            return None
        return _dc_replace(
            override,
            entry_anim=("none" if override.entry_anim == "none" else "anim"),
            exit_anim=("none" if override.exit_anim == "none" else "anim"),
            karaoke_anim="inherit",
            sing_fx="inherit",
        )

    normalized_lines = tuple(
        (
            line
            if line.animation_override is None
            else _dc_replace(
                line, animation_override=_normalized_override(
                    line.animation_override
                )
            )
        )
        for line in track.lines
    )
    return value_signature(_dc_replace(track, lines=list(normalized_lines)))


def value_signature(value) -> Hashable:
    """Recursively describe the current value without using object identity.

    同一 :func:`layout_pass` 区间内按对象身份 memoize：整轨签名的深度
    递归要遍历每行每字符，而 display 缓存查找、诊断缓存 key、页偏移等
    会在一个区间里对同一 track/style 反复签名。区间契约保证输入不可变，
    身份键即语义键；区间外（或无区间）退化为每次现算。叶子标量不经
    memoize——查一次缓存 dict 比直接返回值更贵，而签名树几乎全是叶子。
    """

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    cache = getattr(_LAYOUT_PASS, "signatures", None)
    if cache is not None:
        key = ("value", id(value))
        hit = cache.get(key)
        if hit is not None:
            return hit
        result = _value_signature_impl(value)
        cache[key] = result
        # 键里有 id()：存住入参，避免回收后地址被复用。
        _LAYOUT_PASS.signature_refs.append(value)
        return result
    return _value_signature_impl(value)


def _value_signature_impl(value) -> Hashable:
    # 整轨签名是热路径（布局计划缓存每次查找都要重算），长曲目的递归
    # 遍历不释放 GIL 会饿到 GUI 线程；节流后的让出近乎零开销。
    yield_to_gui()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    # GuideSymbol is the only frozen model carrying a potentially very large
    # immutable tuple (the complete SVG outline). Reusing the value as the key
    # avoids recursively copying every path command on every cache lookup.
    if isinstance(value, GuideSymbol):
        return value
    if isinstance(value, (list, tuple)):
        return tuple(value_signature(item) for item in value)
    if isinstance(value, dict):
        return tuple(
            (key, value_signature(item))
            for key, item in sorted(value.items(), key=lambda item: str(item[0]))
        )
    if is_dataclass(value) and not isinstance(value, type):
        value_type = type(value)
        names = _SIG_FIELD_NAMES_BY_TYPE.get(value_type)
        if names is None:
            names = tuple(field.name for field in dataclass_fields(value))
            _SIG_FIELD_NAMES_BY_TYPE[value_type] = names
        return (value_type.__name__,) + tuple(
            value_signature(getattr(value, name)) for name in names
        )
    return repr(value)


def lyric_layout_style_signature(style, *, include_paint_fields: bool = False) -> Hashable:
    """Style signature restricted to fields that can affect lyric layout.

    与 :func:`value_signature` 的区别：剔除 ``_LYRIC_LAYOUT_EXCLUDED_STYLE_FIELDS``
    列出的纯标题字段。用于歌词行布局 / 显示行解析 / 页偏移 / 布局计划等
    缓存的 key——标题属性编辑不再整份作废这些缓存。
    局部复用永远以本签名为准（签名不匹配即回退重建），调用方传入的
    「只改了标题/颜色」只是性能提示，不是正确性依据。

    ``include_paint_fields=True`` 时保留颜色/填充类字段（仅剔除标题字段）：
    供**缓存值携带样式引用**的消费者使用（如 CPU 行布局缓存的
    ``_LineLayout.glyphs`` 各自带解析后的有效样式）——它们的 key 必须能
    区分任何改变绘制结果的输入，否则换色命中旧布局会用旧样式绘制。
    布局计划 / 显示行解析 / 页偏移缓存的值是纯几何与时间结构，用默认
    的颜色剔除版即可在颜色编辑时安全复用。
    """
    cache = getattr(_LAYOUT_PASS, "signatures", None)
    if cache is not None:
        key = ("layout-style", id(style), bool(include_paint_fields))
        hit = cache.get(key)
        if hit is not None:
            return hit
        result = _lyric_layout_value_signature(
            style, root=True, include_paint_fields=include_paint_fields
        )
        cache[key] = result
        _LAYOUT_PASS.signature_refs.append(style)
        return result
    return _lyric_layout_value_signature(
        style, root=True, include_paint_fields=include_paint_fields
    )


def _lyric_layout_value_signature(
    value, *, root: bool = False, include_paint_fields: bool = False
) -> Hashable:
    """Recursively sign layout inputs while omitting nested paint-only fields."""

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, GuideSymbol):
        return value
    if isinstance(value, (list, tuple)):
        return tuple(
            _lyric_layout_value_signature(
                item, include_paint_fields=include_paint_fields
            )
            for item in value
        )
    if isinstance(value, dict):
        return tuple(
            (
                key,
                _lyric_layout_value_signature(
                    item, include_paint_fields=include_paint_fields
                ),
            )
            for key, item in sorted(value.items(), key=lambda item: str(item[0]))
        )
    if is_dataclass(value) and not isinstance(value, type):
        value_type = type(value)
        if include_paint_fields:
            excluded = (
                _LYRIC_LAYOUT_TITLE_ONLY_EXCLUDED_STYLE_FIELDS if root else frozenset()
            )
        else:
            excluded = (
                _LYRIC_LAYOUT_EXCLUDED_STYLE_FIELDS
                if root
                else _LYRIC_LAYOUT_EXCLUDED_SCHEME_FIELDS
                if value_type.__name__ == "SubtitleStyleScheme"
                else frozenset()
            )
        cache_key = (value_type, excluded)
        names = _SIG_EXCLUDED_NAMES_BY_TYPE.get(cache_key)
        if names is None:
            names = tuple(
                field.name
                for field in dataclass_fields(value)
                if field.name not in excluded
            )
            _SIG_EXCLUDED_NAMES_BY_TYPE[cache_key] = names
        return (value_type.__name__,) + tuple(
            _lyric_layout_value_signature(
                getattr(value, name), include_paint_fields=include_paint_fields
            )
            for name in names
        )
    return repr(value)


__all__ = [
    "lyric_layout_style_signature",
    "value_signature",
]
