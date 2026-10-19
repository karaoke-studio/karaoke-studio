"""Pure model semantics for resolving subtitle style schemes."""

from __future__ import annotations

from collections import Counter
from dataclasses import replace

from krok_helper.subtitle_render.domain.paint import (
    KaraokeColors,
    KaraokeColorState,
    PaintFill,
)
from krok_helper.subtitle_render.domain.models import (
    N3_FONT_INHERITANCE_FIELDS,
    RUBY_DECORATION_OVERRIDE_FIELDS,
    SCANLINE_GLOBAL_ROLE_KEY,
    Style,
    SubtitleStyleScheme,
)


SUBTITLE_SCHEME_STYLE_FIELDS: tuple[str, ...] = (
    "font_family",
    "font_family_latin",
    "font_size_px",
    "letter_spacing_px",
    "space_width_percent",
    "latin_font_size_px",
    "latin_font_weight",
    "latin_font_stretch_pct",
    "latin_stroke_width_px",
    "latin_stroke2_enabled",
    "latin_stroke2_width_px",
    "allow_biting",
    "font_weight",
    "italic",
    "latin_italic",
    "ruby_italic",
    "ruby_latin_italic",
    "affects_ruby_anchor",
    "base_color",
    "fill_color",
    "fill_gradient_enabled",
    "fill_gradient_start_color",
    "fill_gradient_end_color",
    "fill_gradient_angle_deg",
    "stroke_color",
    "stroke_width_px",
    "stroke2_enabled",
    "stroke2_width_px",
    "decoration_kind",
    "decoration_kind_before",
    "decoration_before_sync",
    "glow_radius_px",
    "glow_before_radius_px",
    "glow_after_radius_px",
    "glow_concentration_level",
    "glow_before_concentration_level",
    "shadow_color",
    "shadow_offset_x",
    "shadow_offset_y",
    "shadow_before_offset_x",
    "shadow_before_offset_y",
    "ruby_font_size_px",
    "ruby_font_family",
    "ruby_font_family_latin",
    "ruby_font_weight",
    "ruby_latin_font_size_px",
    "ruby_latin_font_weight",
    "ruby_latin_font_stretch_pct",
    "ruby_font_follow_main",
    "ruby_color",
    "ruby_gap_px",
    "ruby_stroke_width_px",
    "ruby_stroke2_enabled",
    "ruby_stroke2_width_px",
    "ruby_latin_stroke_width_px",
    "ruby_latin_stroke2_enabled",
    "ruby_latin_stroke2_width_px",
    "ruby_decoration_kind",
    "ruby_decoration_kind_before",
    "ruby_glow_radius_px",
    "ruby_glow_before_radius_px",
    "ruby_glow_after_radius_px",
    "ruby_glow_concentration_level",
    "ruby_glow_before_concentration_level",
    "ruby_shadow_offset_x",
    "ruby_shadow_offset_y",
    "ruby_shadow_before_offset_x",
    "ruby_shadow_before_offset_y",
    "ruby_colors_follow_main",
    "ruby_horizontal_gradient_with_main",
    "karaoke_colors",
    "ruby_karaoke_colors",
)


def style_scheme_changes(scheme: SubtitleStyleScheme) -> dict[str, object]:
    return {
        field: value
        for field in SUBTITLE_SCHEME_STYLE_FIELDS
        if (value := getattr(scheme, field)) is not None
    }


def style_for_role(style: Style, role_label: str | None) -> Style:
    if not role_label:
        return style
    scheme = style.custom_style_schemes.get(role_label)
    if scheme is None:
        return style
    changes = style_scheme_changes(scheme)
    if scheme.n3_font_inheritance:
        changes.update(
            {field: getattr(scheme, field) for field in N3_FONT_INHERITANCE_FIELDS}
        )
    has_legacy_color_changes = any(
        getattr(scheme, field) is not None
        for field in (
            "base_color",
            "fill_color",
            "fill_gradient_enabled",
            "fill_gradient_start_color",
            "fill_gradient_end_color",
            "fill_gradient_angle_deg",
            "stroke_color",
            "shadow_color",
        )
    )
    if scheme.karaoke_colors is None and has_legacy_color_changes:
        changes["karaoke_colors"] = None
    if scheme.ruby_karaoke_colors is None and (
        scheme.karaoke_colors is not None or has_legacy_color_changes
    ):
        changes["ruby_karaoke_colors"] = None
    # 方案显式「默认跟随主文字」时注音整体同步本方案主文字。方案侧的
    # None 槽在上面会被 ``style_scheme_changes`` 过滤掉、合并后落到全局的
    # 独立值上，必须显式清空注音覆盖槽（配色矩阵 + 装饰参数），否则全局
    # 的独立注音外观会顶掉角色自己的跟随语义。只看方案自己的表态：全局
    # follow=True 时全局槽位按迁移不变量本就为 None，无需在这里代劳。
    if changes.get("ruby_colors_follow_main") is True:
        changes["ruby_karaoke_colors"] = None
        changes.update(
            {field_name: None for field_name in RUBY_DECORATION_OVERRIDE_FIELDS}
        )
    if not changes:
        return style
    return replace(style, **changes)


def auto_appearance_basis(style: Style, track: object) -> Style:
    """「跟随字体」（auto/role 外观档）大小推导的基准样式。

    统计口径与装饰源同构：信号（灯/柱）只挂段首行，auto 档配色取**所在
    段首行第一个非空白字符**的角色方案——大小的推导基准同样只统计
    **信号宿主行**：每个段首行按其第一个非空白字符的 ``role_label`` 投
    一票（未命中 ``custom_style_schemes``/无标签计入全局默认桶），最高频
    票命中真实角色方案时返回 ``style_for_role(style, name)`` 叠加后的完整
    样式，否则原样返回全局样式。平票取先出现的段首行（首遇序），对同一
    轨道稳定。非宿主行的角色分布不参与——段内其他行挂什么角色不影响
    灯/柱该配的字号。

    为什么只数主轨：auto 档大小是场景级单值（native IR 只物化一份、所有
    字幕源共用），副字幕源的角色分布不该劫持主歌词的画面口径。信号模块
    全关、竖排或无宿主行时返回全局样式（旧口径）。
    """
    if track is None:
        return style
    # 函数内导入：display.signal → page.plan 一侧导入面更宽，顶层互引容易
    # 成环（同 horizontal.layout 的 in-function import 处理）。宿主行判定
    # 复用渲染热路径同一入口——排版区间内命中 ``signal_heads`` 缓存；
    # 宿主 = 任一模块（音量柱/指示灯）挂载行并集（含行级覆盖）。
    from krok_helper.subtitle_render.engine.layout.display.signal import (
        signal_host_context,
    )

    heads = signal_host_context(track, style)
    if not heads:
        return style
    schemes = style.custom_style_schemes
    counts: Counter = Counter()
    for index in sorted(heads):
        chars = getattr(track.lines[index], "chars", None) or ()
        first_role = next(
            (
                char.role_label
                for char in chars
                if getattr(char, "text", "") and not char.text.isspace()
            ),
            None,
        )
        counts[first_role if first_role in schemes else None] += 1
    dominant, _ = counts.most_common(1)[0]
    if dominant is None:
        return style
    return style_for_role(style, dominant)


def appearance_role_source(style: Style, role_name: str | None) -> Style | None:
    """Resolve the ``role``-appearance decoration source (global merge).

    与扫字线 ``scanline_role_fill`` 同一套来源口径：保留键
    ``__global__`` 取主样式自身（返回原 style），其余名字必须是
    ``custom_style_schemes`` 的键，返回方案叠加到全局样式后的完整 Style
    （指示灯/音量柱装饰管线消费它：配色矩阵、描边/二重描边宽、发光/
    阴影、整字放大与缩放基准字号）。名字悬空（历史项目/手工 JSON/空串）
    返回 ``None``，调用方回退 auto 档口径（段首行第一个角色）。
    """

    name = str(role_name or "").strip()
    if not name:
        return None
    if name == SCANLINE_GLOBAL_ROLE_KEY:
        return style
    if name not in style.custom_style_schemes:
        return None
    return style_for_role(style, name)


def solid_fill(color: str) -> PaintFill:
    return PaintFill(
        mode="solid",
        color=color,
        start_color=color,
        end_color=color,
        gradient_stops=[(0, color), (100, color)],
        split_top_color=color,
        split_bottom_color=color,
    )


def legacy_after_text_fill(style: Style) -> PaintFill:
    if not style.fill_gradient_enabled:
        return solid_fill(style.fill_color)
    mode = (
        "gradient_vertical"
        if style.fill_gradient_angle_deg in {90, 270}
        else "gradient_horizontal"
    )
    return PaintFill(
        mode=mode,
        color=style.fill_color,
        start_color=style.fill_gradient_start_color,
        end_color=style.fill_gradient_end_color,
        gradient_stops=[
            (0, style.fill_gradient_start_color),
            (100, style.fill_gradient_end_color),
        ],
        split_top_color=style.fill_gradient_start_color,
        split_bottom_color=style.fill_gradient_end_color,
    )


def effective_karaoke_colors(style: Style) -> KaraokeColors:
    if style.karaoke_colors is not None:
        return style.karaoke_colors

    before = KaraokeColorState(
        text=solid_fill(style.base_color),
        stroke=solid_fill(style.stroke_color),
        stroke2=solid_fill("#000000"),
        shadow=solid_fill(style.shadow_color),
    )
    after = KaraokeColorState(
        text=legacy_after_text_fill(style),
        stroke=solid_fill(style.stroke_color),
        stroke2=solid_fill("#000000"),
        shadow=solid_fill(style.shadow_color),
    )
    return KaraokeColors(before=before, after=after)


def effective_ruby_karaoke_colors(style: Style) -> KaraokeColors:
    if style.ruby_karaoke_colors is not None:
        return style.ruby_karaoke_colors
    if style.karaoke_colors is not None:
        return style.karaoke_colors
    before = KaraokeColorState(
        text=solid_fill(style.base_color),
        stroke=solid_fill(style.stroke_color),
        stroke2=solid_fill("#000000"),
        shadow=solid_fill(style.shadow_color),
    )
    after = KaraokeColorState(
        text=solid_fill(style.ruby_color),
        stroke=solid_fill(style.stroke_color),
        stroke2=solid_fill("#000000"),
        shadow=solid_fill(style.shadow_color),
    )
    return KaraokeColors(before=before, after=after)
