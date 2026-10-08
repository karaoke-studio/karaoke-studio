"""Render IR v1 helpers for the native subtitle renderer sidecar.

C1 keeps the native boundary intentionally boring: Python owns project parsing
and UI state, then sends a JSON-serializable Render IR snapshot to the sidecar.
The first native renderer only uses a small subset of fields for smoke output,
but the IR already carries the full ``style_to_dict`` payload so future C2/C3
work can migrate painter features without changing the process protocol shape.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from krok_helper.subtitle_render.engine.layout.line.style import line_end_ms, line_start_ms
from krok_helper.subtitle_render.engine.render.effects.particles import plan_line_bursts
from krok_helper.subtitle_render.engine.layout.plan.model import TrackLayoutPlan
from krok_helper.subtitle_render.engine.layout.display.signal import (
    lit_signal_head_context,
    volume_signal_head_context,
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
    SubtitleStyleScheme,
    TitleOverlay,
    effective_karaoke_animation,
    effective_karaoke_scanline,
    effective_karaoke_zoom_pulse,
    title_overlay_to_dict,
)
from krok_helper.subtitle_render.serialization.timing import guide_symbol_to_dict
from krok_helper.subtitle_render.engine.style.style_semantics import (
    appearance_role_source,
    style_for_role,
)

# Schema 3：发射边界去重表（fx_color_table / fx_paint_table / line_layout_table）、
# 字符缺省字段省发、导唱符轮廓坐标 2 位小数、可选 vector_glyphs_hash 门
# （哈希相符时省发轮廓表，sidecar 解析前注回）。
RENDER_IR_SCHEMA = 3


class VectorGlyphTable:
    """按轮廓去重的矢量导唱符表（一份 IR 内全轨共享）。

    SVG 导唱符的 ``path_commands`` 可达数千条，而一条 IR 会为每一行的每一个
    导唱虚拟字符重复内嵌整份轮廓——N 行 × K 命令的体积与序列化成本会让
    ``gpu_configure`` 远超超时上限。表键取**渲染投影**（轮廓命令 + em +
    advance）：sidecar 只消费这三个字段，逐行不同的走字时长 / 角色标签 /
    替换前缀不影响字形几何，因此替换型导唱符（每行 duration_ms 各异）也能
    共享同一份轮廓。字符侧通过 ``vector_glyph_id`` 引用；sidecar 据此按
    符号建一次 D2D geometry。
    """

    def __init__(self) -> None:
        self._ids: dict[tuple, str] = {}
        self.payload: dict[str, dict[str, Any]] = {}

    def reference(self, symbol: GuideSymbol) -> str | None:
        """把符号登记进表并返回其 ID；位图导唱符返回 None（走 bitmap_guide）。"""
        if getattr(symbol, "kind", "vector") != "vector" or not symbol.path_commands:
            return None
        units = max(int(symbol.units_per_em), 1)
        advance = max(float(symbol.advance_width), 0.0)
        normalized_commands = tuple(
            (
                str(command[0]),
                *(
                    (0.0 if float(value) == 0.0 else round(float(value), 2))
                    for value in command[1:]
                ),
            )
            for command in symbol.path_commands
        )
        key = (normalized_commands, units, advance)
        glyph_id = self._ids.get(key)
        if glyph_id is None:
            payload = {
                "path_commands": [list(command) for command in normalized_commands],
                "units_per_em": units,
                "advance_width": advance,
            }
            canonical = json.dumps(
                payload,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("ascii")
            # Content-derived IDs stay stable when an unrelated symbol is inserted
            # earlier in a later IR.  The full digest also makes an accidental ID
            # collision unsuitable as a stale native resource-cache alias.
            glyph_id = "g_" + hashlib.sha256(canonical).hexdigest()
            self._ids[key] = glyph_id
            self.payload[glyph_id] = payload
        return glyph_id

    @property
    def empty(self) -> bool:
        return not self.payload


class FxPayloadTable:
    """fx_bursts 载荷去重表（IR 发射边界专用）。

    burst 里的 ``color`` / ``paint`` 规格 / ``char_colors`` 是全工程仅几
    个唯一值、却逐行逐字重复数百次的内容（实测 3 唯一 / 614 次），是
    IR 体积与解析成本的大头。发射时换成 id 引用 + 根级表，sidecar 解析
    时展开回 ``FxBurst`` 原字段——内存结构与渲染零变化；CPU Painter
    继续直接消费 ``plan_line_bursts`` 的内联结果，不经本表。
    """

    def __init__(self) -> None:
        self.colors: list[str] = []
        self._color_ids: dict[str, int] = {}
        self.paints: list[dict[str, Any]] = []
        self._paint_ids: dict[str, int] = {}

    def _color_id(self, color: object) -> int:
        key = str(color)
        index = self._color_ids.get(key)
        if index is None:
            index = len(self.colors)
            self._color_ids[key] = index
            self.colors.append(key)
        return index

    def _paint_id(self, paint: dict[str, Any]) -> int:
        key = json.dumps(paint, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"))
        index = self._paint_ids.get(key)
        if index is None:
            index = len(self.paints)
            self._paint_ids[key] = index
            self.paints.append(paint)
        return index

    def tabulate_burst(self, burst: dict[str, Any]) -> dict[str, Any]:
        out = {
            key: value
            for key, value in burst.items()
            if key not in ("color", "paint", "char_colors")
        }
        out["color_id"] = self._color_id(burst.get("color"))
        paint = burst.get("paint")
        if isinstance(paint, dict) and paint:
            out["paint_id"] = self._paint_id(paint)
        char_colors = burst.get("char_colors")
        if isinstance(char_colors, list) and char_colors:
            out["char_color_ids"] = [
                self._color_id(color) for color in char_colors
            ]
        return out

    def payload(self) -> dict[str, Any]:
        return {
            "fx_color_table": list(self.colors),
            "fx_paint_table": [dict(paint) for paint in self.paints],
        }


def tabulate_fx_bursts(
    bursts: list[dict[str, object]] | None,
    fx_table: FxPayloadTable | None,
) -> list[dict[str, object]]:
    """bursts 载荷发射：表在手走 id 引用，无表保持内联（对照/旧路径）。"""
    if fx_table is None:
        return list(bursts or [])
    return [fx_table.tabulate_burst(burst) for burst in (bursts or [])]


class LineLayoutTable:
    """每行 ``layout`` 参数快照的去重表（实测 2 唯一 / 66 行）。"""

    def __init__(self) -> None:
        self.layouts: list[dict[str, Any]] = []
        self._ids: dict[str, int] = {}

    def layout_id(self, layout: dict[str, Any]) -> int:
        key = json.dumps(layout, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"))
        index = self._ids.get(key)
        if index is None:
            index = len(self.layouts)
            self._ids[key] = index
            self.layouts.append(layout)
        return index

    def payload(self) -> list[dict[str, Any]]:
        return [dict(layout) for layout in self.layouts]
GPU_UNSUPPORTED_FEATURE_LABELS = {
    "line_animation": "\u672a\u77e5\u6574\u884c\u52a8\u753b",
    "karaoke_animation": "\u672a\u77e5\u5531\u5b57\u7279\u6548",
    "line_animation_override": "\u672a\u77e5\u9010\u884c\u7279\u6548",
    "bitmap_guide_symbol": "\u56fe\u7247\u5bfc\u5531\u7b26 / N3 Emoji \u5934\u50cf",
}


def gpu_unsupported_feature_labels(reasons: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(GPU_UNSUPPORTED_FEATURE_LABELS.get(reason, reason) for reason in reasons)


def _font_face_slot_overrides(payload: dict[str, Any]) -> None:
    """为单个样式字典追加统一字重解析的生效结果。

    不改动既有 ``*_font_weight`` 键（C++ 侧行内覆盖启发式与 sidecar 内置
    Qt 后端继续消费原始请求值），只追加两类键：

    - ``*_font_face_weight``：该槽实际渲染的 face 字重（静态=钉扎/模拟基
      face；可变=轴值），GPU 端据此精确建 face；
    - ``*_sim_bold``：该槽是否走合成粗体（Qt 实测指纹判定）。

    回退链与 C++ 解析端一一对应：latin→main、ruby→main、ruby_latin→
    ruby_latin→ruby→main。
    """
    from krok_helper.subtitle_render.engine.text.font_weight import resolve_weight_plan

    if "font_family" not in payload or "font_weight" not in payload:
        return
    italic = bool(payload.get("italic"))
    main_weight = int(payload["font_weight"] or 400)

    def resolved(family: Any, weight: Any) -> tuple[int, int]:
        if weight is None:
            weight = main_weight
        plan = resolve_weight_plan(str(family or ""), int(weight), italic)
        return plan.base_weight, plan.embolden_delta

    main_family = payload.get("font_family")
    plan = resolve_weight_plan(str(main_family or ""), main_weight, italic)
    payload["font_face_weight"] = plan.base_weight
    payload["font_sim_bold"] = False
    payload["font_axis"] = plan.render_mode == "axis"
    payload["font_embolden"] = plan.embolden_delta

    latin_family = payload.get("latin_font_family") or main_family
    payload["latin_font_axis"] = (
        resolve_weight_plan(
            str(latin_family or ""),
            int(payload.get("latin_font_weight") or main_weight),
            italic,
        ).axis_value
        is not None
    )
    latin_plan = resolve_weight_plan(
        str(latin_family or ""),
        int(payload.get("latin_font_weight") or main_weight),
        italic,
    )
    payload["latin_font_embolden"] = latin_plan.embolden_delta
    face_weight, _delta = resolved(latin_family, payload.get("latin_font_weight"))
    payload["latin_font_face_weight"] = face_weight
    payload["latin_font_sim_bold"] = False

    ruby_family = payload.get("ruby_font_family") or main_family
    payload["ruby_font_axis"] = (
        resolve_weight_plan(
            str(ruby_family or ""),
            int(payload.get("ruby_font_weight") or main_weight),
            italic,
        ).axis_value
        is not None
    )
    ruby_plan = resolve_weight_plan(
        str(ruby_family or ""),
        int(payload.get("ruby_font_weight") or main_weight),
        italic,
    )
    payload["ruby_font_embolden"] = ruby_plan.embolden_delta
    face_weight, _delta = resolved(ruby_family, payload.get("ruby_font_weight"))
    payload["ruby_font_face_weight"] = face_weight
    payload["ruby_font_sim_bold"] = False

    ruby_latin_family = payload.get("ruby_latin_font_family") or ruby_family
    ruby_latin_weight = payload.get("ruby_latin_font_weight")
    if ruby_latin_weight is None:
        ruby_latin_weight = payload.get("ruby_font_weight")
    payload["ruby_latin_font_axis"] = (
        resolve_weight_plan(
            str(ruby_latin_family or ""),
            int(ruby_latin_weight or main_weight),
            italic,
        ).axis_value
        is not None
    )
    ruby_latin_plan = resolve_weight_plan(
        str(ruby_latin_family or ""),
        int(ruby_latin_weight or main_weight),
        italic,
    )
    payload["ruby_latin_font_embolden"] = ruby_latin_plan.embolden_delta
    face_weight, _delta = resolved(ruby_latin_family, ruby_latin_weight)
    payload["ruby_latin_font_face_weight"] = face_weight
    payload["ruby_latin_font_sim_bold"] = False


def apply_resolved_font_faces(node: Any) -> None:
    """整棵渲染 IR 递归注入统一字重解析结果（configure 发送前调用一次）。

    命中所有携带 ``font_family``+``font_weight`` 的样式字典：全局 style、
    逐行/逐字符样式、标题与角色样式——它们共用同一组字段名，且都由
    C++ 渲染端按 (family, weight) 解析 face。
    """
    if isinstance(node, dict):
        _font_face_slot_overrides(node)
        for value in node.values():
            apply_resolved_font_faces(value)
    elif isinstance(node, list):
        for item in node:
            apply_resolved_font_faces(item)


def title_overlay_to_ir(
    title: TitleOverlay,
    scheme: SubtitleStyleScheme | None,
) -> dict[str, Any]:
    """Serialize a resolved title without inheriting Latin metrics from lyrics."""
    payload = title_overlay_to_dict(title)
    # 导唱符不随外观载荷下发：role_styles 里的解析结果会原样带着 base 的
    # 符号字典（含体积可观的矢量轮廓），而 sidecar 只在主载荷消费 IR 形态
    # （render_ir._title_guide_to_ir），这里先剥掉原始形态。
    payload.pop("guide_symbols", None)
    payload.pop("inline_guide_symbols", None)
    payload["latin_font_size_px"] = max(
        int(
            scheme.latin_font_size_px
            if scheme is not None and scheme.latin_font_size_px is not None
            else title.font_size_px
        ),
        1,
    )
    payload["latin_font_weight"] = max(
        1,
        min(
            int(
                scheme.latin_font_weight
                if scheme is not None and scheme.latin_font_weight is not None
                else title.font_weight
            ),
            999,
        ),
    )
    payload["latin_font_stretch_pct"] = max(
        50,
        min(
            200,
            int(
                scheme.latin_font_stretch_pct
                if scheme is not None and scheme.latin_font_stretch_pct is not None
                else title.latin_font_stretch_pct
            ),
        ),
    )
    return payload


def gpu_unsupported_features(
    track: TimingTrack,
    style: Style,
    extra_tracks: list[TimingTrack] | None = None,
) -> tuple[str, ...]:
    """Return project features that require whole-frame Painter fallback."""
    reasons: list[str] = []
    sources = [track, *(extra_tracks or ())]
    # 2026-09 新增逐字几何特效（tracking_in / wave_in / scatter_out /
    # converge_out）与粒子/描边闪光由 GPU sidecar 原生渲染（C++ 镜像实现），
    # 不触发整帧 Painter 回退。
    if style.entry_anim not in {
        "none", "fade", "slide_in", "rise", "char_fade", "char_drip", "spin_flip", "utopia",
        "tracking_in", "wave_in", "stretch_in", "glow_in", "assemble_in",
        "sparkle", "ripple", "note", "petal", "snow", "snow_solid",
    } or (
        style.exit_anim not in {
            "none", "fade", "slide_out", "rise", "char_fade", "char_drip", "spin_flip", "utopia",
            "scatter_out", "converge_out", "stretch_out", "glow_out", "dissolve_out",
            "sparkle", "ripple", "note", "petal", "snow", "snow_solid",
        }
    ):
        reasons.append("line_animation")
    # 扫字线档位（scanline / utopia_scanline / zoom_pulse_scanline）由 GPU
    # sidecar 原生渲染：主文字与 ruby 的锋面高亮带在 d2d_backend_render 里与
    # Wipe/Utopia 同路绘制。整字放大（zoom_pulse / zoom_pulse_scanline）同样
    # 原生渲染：本体按 utopia 逐字变换管线走，曲线/原点由行级 zoom_pulse
    # 标记在 C++ 侧切换。
    karaoke_effects = {
        "inherit", "none", "no_wipe", "utopia", "scanline", "utopia_scanline",
        "zoom_pulse", "zoom_pulse_scanline"
    }
    if (
        style.karaoke_anim not in karaoke_effects
        or style.reverse_karaoke_anim not in karaoke_effects
    ):
        reasons.append("karaoke_animation")
    for source in sources:
        for line in source.lines:
            if line.animation_override is not None:
                if line.animation_override.entry_anim not in {
                    "none",
                    "fade",
                    "slide_in",
                    "rise",
                    "char_fade",
                    "char_drip",
                    "spin_flip",
                    "utopia",
                    "tracking_in",
                    "wave_in",
                    "glow_in",
                    "stretch_in",
                    "assemble_in",
                } or line.animation_override.exit_anim not in {
                    "none",
                    "fade",
                    "slide_out",
                    "rise",
                    "char_fade",
                    "char_drip",
                    "spin_flip",
                    "utopia",
                    "scatter_out",
                    "converge_out",
                    "glow_out",
                    "stretch_out",
                    "dissolve_out",
                    "sparkle",
                    "ripple",
                    "note",
                    "petal",
                    "snow",
                    "snow_solid",
                }:
                    reasons.append("line_animation_override")
    # 标题图片导唱符（2026-09 新增）由 GPU sidecar 原生渲染（gpu_scene_projection
    # 按字符挂载 bitmap/vector 管线，标题永不走字 → 恒取「走字前」一侧），
    # 与 Painter 同一口径，不触发整帧回退。
    return tuple(dict.fromkeys(reasons))


def _image_file_signature(path_text: str | None) -> tuple[int, int]:
    if not path_text:
        return (0, 0)
    try:
        stat = Path(path_text).stat()
    except OSError:
        return (0, 0)
    return (max(int(stat.st_mtime_ns // 1_000_000), 0), max(int(stat.st_size), 0))


def bitmap_guide_to_ir(
    symbol: object | None,
    anim_anchor_ms: int | None = None,
) -> dict[str, Any] | None:
    if symbol is None or getattr(symbol, "kind", "vector") != "bitmap":
        return None
    before_path = str(getattr(symbol, "bitmap_before_path", "") or "")
    after_path = str(getattr(symbol, "bitmap_after_path", "") or "")
    before_modified, before_size = _image_file_signature(before_path)
    after_modified, after_size = _image_file_signature(after_path)
    return {
        "before_path": before_path,
        "after_path": after_path,
        "zoom_percent": max(int(getattr(symbol, "bitmap_zoom_percent", 100)), 1),
        "fix_size": bool(getattr(symbol, "bitmap_fix_size", False)),
        "no_decor": bool(getattr(symbol, "bitmap_no_decor", False)),
        "force_wipe_decor": bool(getattr(symbol, "bitmap_force_wipe_decor", False)),
        "margin_left_px": int(getattr(symbol, "bitmap_margin_left_px", 0)),
        "margin_right_px": int(getattr(symbol, "bitmap_margin_right_px", 0)),
        "margin_bottom_px": int(getattr(symbol, "bitmap_margin_bottom_px", 0)),
        "before_modified_ms": before_modified,
        "before_size": before_size,
        "after_modified_ms": after_modified,
        "after_size": after_size,
        # 动图循环锚点（行显示窗口起点；schedule 缺窗口时回退行起点）。
        # Python painter 与 GPU sidecar 都以该值为 t=0 选帧，单一事实源。
        "anim_anchor_ms": int(anim_anchor_ms if anim_anchor_ms is not None else 0),
    }


def timing_char_to_ir(
    ch: TimingChar,
    glyph_table: VectorGlyphTable | None = None,
    anim_anchor_ms: int | None = None,
) -> dict[str, Any]:
    # 「缺省即空」的字段值为默认时整个键不发（C++ 解析器对缺 key 本就按
    # 默认处理，scanline/zoom_pulse 等字段一直是这个惯例）：624 字里上千
    # 个 false/null 字段是纯键名开销。
    char: dict[str, Any] = {
        "text": ch.text,
        "start_ms": int(ch.start_ms),
    }
    if ch.explicit_start:
        char["explicit_start"] = True
    if ch.explicit_end:
        char["explicit_end"] = True
    if ch.pause_release_ms is not None:
        char["pause_release_ms"] = int(ch.pause_release_ms)
    if ch.role_label:
        char["role_label"] = ch.role_label
    guide = bitmap_guide_to_ir(ch.vector_glyph, anim_anchor_ms)
    if guide is not None:
        char["bitmap_guide"] = guide
    if glyph_table is not None:
        if ch.vector_glyph is not None:
            glyph_id = glyph_table.reference(ch.vector_glyph)
            if glyph_id is not None:
                char["vector_glyph_id"] = glyph_id
    elif ch.vector_glyph is not None:
        # Legacy inline form：无符号表的调用方（旧测试 / 探针）仍可整包内嵌。
        char["vector_glyph"] = guide_symbol_to_dict(ch.vector_glyph)
    return char


def _line_layout_dict(layout_style: Style) -> dict[str, Any]:
    """每行 ``layout`` 参数快照（发射边界表化的载荷单元）。"""
    return {
                "line_y_position": layout_style.line_y_position,
                "line_y_margin_px": int(layout_style.line_y_margin_px),
                "line_gap_px": int(layout_style.line_gap_px),
                "smart_horizontal": layout_style.smart_horizontal,
                "horizontal_margin_px": int(layout_style.horizontal_margin_px),
                "line_alignments": list(layout_style.line_alignments),
                "dual_line_layout": bool(layout_style.dual_line_layout),
                "line_horizontal_layout": layout_style.line_horizontal_layout,
                "row1_align": layout_style.row1_align,
                "row1_offset_x": int(layout_style.row1_offset_x),
                "row1_offset_y": int(layout_style.row1_offset_y),
                "row2_align": layout_style.row2_align,
                "row2_offset_x": int(layout_style.row2_offset_x),
                "row2_offset_y": int(layout_style.row2_offset_y),
                "letter_spacing_px": int(layout_style.letter_spacing_px),
                "space_width_percent": int(layout_style.space_width_percent),
                "allow_biting": bool(layout_style.allow_biting),
                "ruby_interval_px": int(layout_style.ruby_interval_px),
                "ruby_alignment": layout_style.ruby_alignment,
                "ruby_gap_px": int(layout_style.ruby_gap_px),
            }


def timing_line_to_ir(
    line: TimingLine,
    *,
    render_line: TimingLine | None = None,
    layout_style: Style | None = None,
    resolved_intervals: list[tuple[int, int]] | None = None,
    page_index: int = -1,
    page_line_count: int = 0,
    section_index: int = -1,
    signal_head: bool = False,
    volume_head: bool = False,
    lit_head: bool = False,
    signal_band_join: bool = False,
    lane: int = 0,
    layout_lane: int | None = None,
    display_start_ms: int | None = None,
    display_end_ms: int | None = None,
    center_override: bool = False,
    entry_anim: str = "none",
    entry_duration_ms: int = 0,
    exit_anim: str = "none",
    exit_duration_ms: int = 0,
    karaoke_anim: str = "none",
    scanline: bool = False,
    zoom_pulse: bool = False,
    stroke_flash: bool = False,
    fx_bursts: list[dict[str, object]] | None = None,
    layout_offset_x: float = 0.0,
    layout_offset_y: float = 0.0,
    layout_offset_windows: list[tuple[int, int, float, float]] | None = None,
    glyph_table: VectorGlyphTable | None = None,
    fx_table: "FxPayloadTable | None" = None,
    layout_table: "LineLayoutTable | None" = None,
) -> dict[str, Any]:
    render_line = render_line or line
    # 与 painter._paint_line_static 的动图锚点同一公式：行显示窗口起点，
    # schedule 缺窗口时回退行起点。锚点在 IR 里是唯一事实源，sidecar 不再
    # 自行推导，避免两后端选帧错位。
    guide_anim_anchor_ms = (
        int(display_start_ms)
        if display_start_ms is not None
        else line_start_ms(render_line)
    )
    return {
        "chars": [
            timing_char_to_ir(ch, glyph_table, guide_anim_anchor_ms)
            for ch in render_line.chars
        ],
        "end_ms": int(line.end_ms) if line.end_ms is not None else None,
        # 整行逆序已理顺的标记：sidecar 据此对齐 Painter 的反向走字
        # （横排 rtl 翻转 / 竖排自下而上），缺省 false 兼容旧 IR。
        "wipe_reverse": bool(getattr(render_line, "wipe_reverse", False)),
        "singer_label": line.singer_label,
        "singer_id": line.singer_id,
        "is_blank": bool(line.is_blank),
        # Loader-stamped line identity; -1 when unknown.  Ruby ownership is
        # compared against this, not the IR array position, so a caller that
        # builds a sub-track (single line, extra subtitle source) still matches.
        "track_line_index": (
            -1 if line.track_line_index is None else int(line.track_line_index)
        ),
        "page_index": int(page_index),
        # 页内可渲染行数：Bottom 锚定的短页要从对齐列表末尾往回取（N3
        # ``CalcHorizontalAlignment``），native 侧靠这个值复现同一档对齐。
        "page_line_count": max(int(page_line_count), 0),
        "section_index": int(section_index),
        # 本行是否挂着任一信号模块（音量柱/指示灯宿主行并集；含特效行
        # 开关的行级覆盖）；旧宿主发的 IR 没有该字段，native 侧缺省按 true
        # 解析保持旧行为。
        "signal_head": bool(signal_head),
        # 分模块宿主旗标（特效行开关）：native 分别门控柱组与形状灯；
        # 旧 IR 缺 key 时 native 回退 signal_head（等价旧行为）。
        "volume_head": bool(volume_head),
        "lit_head": bool(lit_head),
        # 「真一组」渐变带正文侧拓宽闸门（音量柱 auto/role 且装饰源与正文
        # 第一角色同源 + 柱宿主行 + 非 RTL）：native configure 据此把第一
        # 角色（及 ruby 共享盒）的横向渐变跨度左缘拓宽到柱组左缘——柱体
        # 与正文共用同一条渐变带，与 Painter 的 signal_band_left 同口径。
        # 旧 IR 缺省 false 兼容。
        "signal_band_join": bool(signal_band_join),
        "lane": int(lane),
        "layout_lane": int(lane if layout_lane is None else layout_lane),
        "display_start_ms": (
            int(display_start_ms) if display_start_ms is not None else None
        ),
        "display_end_ms": int(display_end_ms) if display_end_ms is not None else None,
        "center_override": bool(center_override),
        "entry_anim": str(entry_anim),
        "entry_duration_ms": max(int(entry_duration_ms), 0),
        "exit_anim": str(exit_anim),
        "exit_duration_ms": max(int(exit_duration_ms), 0),
        "karaoke_anim": str(karaoke_anim),
        # 扫字线叠加开关（仅显式档位为 True）；参数（粗细/颜色/发光）走 style IR。
        "scanline": bool(scanline),
        # 整字放大开关：本体 karaoke_anim 仍发降维后的 "utopia"，C++ 侧靠这个
        # 行级标记切换缩放曲线与原点（字符中心）。
        "zoom_pulse": bool(zoom_pulse),
        # 唱字描边闪光开关（与唱字档位正交）；参数走 style IR。
        "stroke_flash": bool(stroke_flash),
        # 装饰粒子（入场/退场/唱字）：Python 侧规划（与 painter 同一
        # plan_line_bursts），锚点坐标由 native 按自身布局解析。
        # 发射边界表化（color/paint/char_colors → id + 根级表）；表为
        # None 时保持内联（旧调用方 / 对照路径）。
        "fx_bursts": (
            [fx_table.tabulate_burst(burst) for burst in (fx_bursts or [])]
            if fx_table is not None
            else list(fx_bursts or [])
        ),
        "layout_offset_x": float(layout_offset_x),
        "layout_offset_y": float(layout_offset_y),
        "layout_offset_windows": [
            {
                "start_ms": int(start_ms),
                "end_ms": int(end_ms),
                "offset_x": float(offset_x),
                "offset_y": float(offset_y),
            }
            for start_ms, end_ms, offset_x, offset_y in (
                layout_offset_windows or []
            )
            if int(end_ms) > int(start_ms)
        ],
        "layout": (
            _line_layout_dict(layout_style)
            if layout_table is None and layout_style is not None
            else None
        ),
        **(
            {"layout_id": layout_table.layout_id(_line_layout_dict(layout_style))}
            if layout_table is not None and layout_style is not None
            else {}
        ),
        "resolved_intervals": (
            [[int(start), int(end)] for start, end in resolved_intervals]
            if resolved_intervals is not None
            else None
        ),
    }


def ruby_to_ir(ruby: RubyAnnotation) -> dict[str, Any]:
    return {
        "kanji": ruby.kanji,
        "reading": ruby.reading,
        "reading_part_ms": [int(item) for item in ruby.reading_part_ms],
        "reading_parts": list(ruby.reading_parts),
        "pos_start_ms": int(ruby.pos_start_ms),
        "pos_end_ms": int(ruby.pos_end_ms),
        # Loader-resolved target; -1 means "not resolved, search by text" so the
        # sidecar keeps Painter's fallback for projects saved before this field.
        "target_line_index": (
            -1 if ruby.target_line_index is None else int(ruby.target_line_index)
        ),
        "target_char_start": (
            -1 if ruby.target_char_start is None else int(ruby.target_char_start)
        ),
        "target_char_end": (
            -1 if ruby.target_char_end is None else int(ruby.target_char_end)
        ),
    }


def track_to_ir(
    track: TimingTrack,
    style: Style | None = None,
    *,
    layout_plan: TrackLayoutPlan | None = None,
    glyph_table: VectorGlyphTable | None = None,
    fx_table: FxPayloadTable | None = None,
    layout_table: LineLayoutTable | None = None,
    time_offset_delta_ms: int = 0,
) -> dict[str, Any]:
    """Serialize one track; ``time_offset_delta_ms`` folds a per-track style
    timing offset into the per-source ``meta.offset_ms`` channel.

    C++ 侧每行窗口偏移 = 全局 ``style.timing_offset_ms`` + 每源
    ``meta.offset_ms``（gpu_scene_projection.cpp:483）。主轨/跟随副轨
    传 0（IR 逐字节不变）；非跟随副轴传 ``轴偏移 − 全局偏移``，使 GPU
    的有效偏移与 CPU painter 的 ``meta + 轴偏移`` 一致。协议零改动。
    """
    schedule: dict[int, tuple[int, int, int]] = {}
    if style is not None:
        if layout_plan is None:
            raise ValueError("style serialization requires a resolved layout_plan")
        schedule = {
            item.track_index: (
                item.lane,
                item.display_start_ms,
                item.display_end_ms,
            )
            for item in layout_plan.lines
            if item.display_start_ms is not None and item.display_end_ms is not None
        }
        page_line_counts = {
            item.track_index: item.page_line_count for item in layout_plan.lines
        }
        authored_lanes = {
            item.track_index: item.layout_lane for item in layout_plan.lines
        }
        center_overrides = {
            item.track_index: item.center_override for item in layout_plan.lines
        }
        animation_styles = [item.animation_style for item in layout_plan.lines]
        layout_styles = [item.layout_style for item in layout_plan.lines]
        render_lines = [item.render_line for item in layout_plan.lines]
        resolved_intervals = [list(item.resolved_intervals) for item in layout_plan.lines]
        page_indices = {item.track_index: item.page_index for item in layout_plan.lines}
        section_indices = {
            item.track_index: item.section_index for item in layout_plan.lines
        }
        page_offset_windows = {
            item.track_index: item.layout_offset_windows for item in layout_plan.lines
        }
    else:
        page_line_counts = {}
        authored_lanes = {}
        center_overrides = {}
        animation_styles = []
        layout_styles = []
        render_lines = []
        resolved_intervals = []
        page_indices = {}
        section_indices = {}
        page_offset_windows = {}
    # 分模块宿主行（段首基线 + volume_head_override/lit_head_override 行级
    # 覆盖）：signal_head = 并集（任一模块挂载），volume_head/lit_head 供
    # native 分别门控柱组与形状灯（特效行开关）。
    volume_heads: frozenset[int] = frozenset()
    lit_heads: frozenset[int] = frozenset()
    if style is not None and (style.lit_enabled or style.volume_enabled) and not style.vertical:
        volume_heads = volume_signal_head_context(track, style) or frozenset()
        lit_heads = lit_signal_head_context(track, style) or frozenset()
    signal_heads = volume_heads | lit_heads
    # 「真一组」正文侧拓宽闸门（样式级部分）：音量柱启用 + auto/role 档，
    # 且装饰源与正文第一角色同源（auto，或 role 档方案悬空回退）——
    # role 档解析到固定方案时正文渐变不参与（柱体画刷仍取并集跨度）。
    signal_band_style_joins = (
        style is not None
        and style.volume_enabled
        and style.volume_appearance_mode in {"auto", "role"}
        and not (
            style.volume_appearance_mode == "role"
            and appearance_role_source(style, style.volume_role_name) is not None
        )
    )
    return {
        "meta": {
            "title": track.meta.title,
            "artist": track.meta.artist,
            "album": track.meta.album,
            "tagging_by": track.meta.tagging_by,
            "silence_ms": int(track.meta.silence_ms),
            "offset_ms": int(track.meta.offset_ms) + int(time_offset_delta_ms),
            "custom": list(track.meta.custom),
        },
        "lines": [
            timing_line_to_ir(
                line,
                render_line=(render_lines[index] if style is not None else None),
                layout_style=(layout_styles[index] if style is not None else None),
                resolved_intervals=(
                    resolved_intervals[index] if style is not None else None
                ),
                page_index=page_indices.get(index, -1),
                page_line_count=page_line_counts.get(index, 0),
                section_index=section_indices.get(index, -1),
                signal_head=index in signal_heads,
                volume_head=index in volume_heads,
                lit_head=index in lit_heads,
                signal_band_join=(
                    signal_band_style_joins
                    and index in volume_heads
                    and line is not None
                    and style is not None
                    and style.right_to_left == line.wipe_reverse
                ),
                lane=schedule.get(index, (0, 0, 0))[0],
                layout_lane=authored_lanes.get(index),
                display_start_ms=(schedule[index][1] if index in schedule else None),
                display_end_ms=(schedule[index][2] if index in schedule else None),
                center_override=center_overrides.get(index, False),
                entry_anim=(
                    animation_styles[index].entry_anim
                    if style is not None
                    else "none"
                ),
                entry_duration_ms=(
                    animation_styles[index].entry_lead_ms
                    if style is not None
                    else 0
                ),
                exit_anim=(
                    animation_styles[index].exit_anim
                    if style is not None
                    else "none"
                ),
                exit_duration_ms=(
                    animation_styles[index].exit_fade_ms
                    if style is not None
                    else 0
                ),
                karaoke_anim=(
                    effective_karaoke_animation(animation_styles[index])
                    if style is not None
                    else "none"
                ),
                scanline=(
                    effective_karaoke_scanline(animation_styles[index])
                    if style is not None
                    else False
                ),
                zoom_pulse=(
                    effective_karaoke_zoom_pulse(animation_styles[index])
                    if style is not None
                    else False
                ),
                stroke_flash=(
                    bool(animation_styles[index].karaoke_stroke_flash)
                    if style is not None
                    else False
                ),
                fx_bursts=(
                    plan_line_bursts(
                        animation_styles[index],
                        index,
                        schedule[index][1] if index in schedule else None,
                        schedule[index][2] if index in schedule else None,
                        (
                            line_end_ms(render_lines[index])
                            if style is not None
                            else None
                        ),
                        resolved_intervals[index],
                        char_visible=[
                            not str(getattr(ch, "text", "") or "").isspace()
                            for ch in render_lines[index].chars
                        ],
                        # 「跟随字体」逐字取角色配色：与 CPU painter 同一解析
                        # 式（行样式已并入歌手方案，字符 role_label 再叠加）。
                        char_styles=[
                            style_for_role(
                                animation_styles[index],
                                getattr(ch, "role_label", None),
                            )
                            for ch in render_lines[index].chars
                        ],
                    )
                    if style is not None
                    else []
                ),
                layout_offset_windows=list(page_offset_windows.get(index, ())),
                glyph_table=glyph_table,
                fx_table=fx_table,
                layout_table=layout_table,
            )
            for index, line in enumerate(track.lines)
        ],
        "rubies": [ruby_to_ir(ruby) for ruby in track.rubies],
    }


def lines_style_to_ir(
    track: TimingTrack,
    style: Style,
    layout_plan: TrackLayoutPlan,
    *,
    source_index: int = 0,
    fx_table: FxPayloadTable | None = None,
    include_placement: bool = False,
    layout_table: "LineLayoutTable | None" = None,
) -> list[dict[str, object]]:
    """逐行「样式派生字段」载荷（``gpu_configure_style`` 差分更新用）。

    与 :func:`track_to_ir` 的行级样式字段同口径：出入场/唱字动画档位、
    信号旗标、装饰粒子 bursts——bursts 嵌有逐字解析色（实色回退）与动画
    时长缩放，**改色也必须随差分更新行数据**，这正是本载荷存在的原因。
    ``source_index`` 与 sidecar 解析 IR 时的 ``sourceIndex`` 对齐（主轨 0、
    副轨 1..N）；行号用源内下标，两边一一对应。测试逐字段对照
    :func:`track_to_ir` 的输出防两处漂移。
    """
    schedule: dict[int, tuple[int, int, int]] = {
        item.track_index: (
            item.lane,
            item.display_start_ms,
            item.display_end_ms,
        )
        for item in layout_plan.lines
        if item.display_start_ms is not None and item.display_end_ms is not None
    }
    animation_styles = [item.animation_style for item in layout_plan.lines]
    render_lines = [item.render_line for item in layout_plan.lines]
    resolved_intervals = [list(item.resolved_intervals) for item in layout_plan.lines]
    # 分模块宿主行同 track_to_ir：signal_head = 并集，band 门按柱宿主行。
    volume_heads: frozenset[int] = frozenset()
    lit_heads: frozenset[int] = frozenset()
    if (style.lit_enabled or style.volume_enabled) and not style.vertical:
        volume_heads = volume_signal_head_context(track, style) or frozenset()
        lit_heads = lit_signal_head_context(track, style) or frozenset()
    signal_heads = volume_heads | lit_heads
    signal_band_style_joins = (
        style.volume_enabled
        and style.volume_appearance_mode in {"auto", "role"}
        and not (
            style.volume_appearance_mode == "role"
            and appearance_role_source(style, style.volume_role_name) is not None
        )
    )
    # 布局摆放视图（P6：layout scope 差分附带）：lane / 显示窗口 /
    # 页号与页内行数 / 行偏移与摆放窗口 / 居中覆写。改字号/边距/行数后
    # 这些字段变化而字符文本与时间不变——sidecar 在既有行数据上原位更新。
    placement_by_index: dict[int, dict[str, object]] = {}
    if include_placement:
        placement_by_index = {
            item.track_index: {
                **(
                    {"layout_id": layout_table.layout_id(
                        _line_layout_dict(item.layout_style)
                    )}
                    if layout_table is not None
                    else {}
                ),
                "lane": int(item.lane),
                "display_start_ms": (
                    int(item.display_start_ms)
                    if item.display_start_ms is not None
                    else None
                ),
                "display_end_ms": (
                    int(item.display_end_ms)
                    if item.display_end_ms is not None
                    else None
                ),
                "page_index": int(item.page_index),
                "page_line_count": int(item.page_line_count),
                # 与 track_to_ir 同口径：静态基偏移恒 0，页偏移全部由
                # layout_offset_windows 的时间窗口承载（native 按帧取当前窗）。
                "layout_offset_x": 0.0,
                "layout_offset_y": 0.0,
                "center_override": bool(item.center_override),
                "layout_offset_windows": [
                    {
                        "start_ms": int(start_ms),
                        "end_ms": int(end_ms),
                        "offset_x": float(offset_x),
                        "offset_y": float(offset_y),
                    }
                    for start_ms, end_ms, offset_x, offset_y in (
                        item.layout_offset_windows or ()
                    )
                    if int(end_ms) > int(start_ms)
                ],
            }
            for item in layout_plan.lines
        }
    entries: list[dict[str, object]] = []
    for index, line in enumerate(track.lines):
        entries.append(
            {
                "source_index": int(source_index),
                "source_line_index": index,
                **(placement_by_index.get(index) or {}),
                "signal_head": index in signal_heads,
                "volume_head": index in volume_heads,
                "lit_head": index in lit_heads,
                "signal_band_join": (
                    signal_band_style_joins
                    and index in volume_heads
                    and line is not None
                    and style.right_to_left == line.wipe_reverse
                ),
                "entry_anim": animation_styles[index].entry_anim,
                "entry_duration_ms": animation_styles[index].entry_lead_ms,
                "exit_anim": animation_styles[index].exit_anim,
                "exit_duration_ms": animation_styles[index].exit_fade_ms,
                "karaoke_anim": effective_karaoke_animation(
                    animation_styles[index]
                ),
                "scanline": effective_karaoke_scanline(animation_styles[index]),
                "zoom_pulse": effective_karaoke_zoom_pulse(
                    animation_styles[index]
                ),
                "stroke_flash": bool(
                    animation_styles[index].karaoke_stroke_flash
                ),
                "fx_bursts": tabulate_fx_bursts(
                    plan_line_bursts(
                        animation_styles[index],
                        index,
                        schedule[index][1] if index in schedule else None,
                        schedule[index][2] if index in schedule else None,
                        line_end_ms(render_lines[index]),
                        resolved_intervals[index],
                        char_visible=[
                            not str(getattr(ch, "text", "") or "").isspace()
                            for ch in render_lines[index].chars
                        ],
                        char_styles=[
                            style_for_role(
                                animation_styles[index],
                                getattr(ch, "role_label", None),
                            )
                            for ch in render_lines[index].chars
                        ],
                    ),
                    fx_table,
                ),
            }
        )
    return entries
