"""Compose shared Painter layout semantics into the native renderer IR."""

from __future__ import annotations

from typing import Any

from krok_helper.subtitle_render.engine.layout.layout_context import roll_shift_scope
from krok_helper.subtitle_render.engine.layout.plan.semantic import layout_pass
from krok_helper.subtitle_render.engine.render.adapters.layout_plan import (
    build_track_layout_plan,
)
from krok_helper.subtitle_render.engine.style.title_semantics import (
    resolve_title_overlay,
    resolve_title_role_overlay,
    resolve_title_text,
    title_layout_source,
    title_row_alignments,
    title_row_offsets_x,
    title_show_specs,
)
from krok_helper.subtitle_render.domain.band_presets import (
    default_clef_color_for,
    default_line_color_for,
    resolve_band_clef_image,
    resolve_band_lines_image,
    resolve_band_render_image,
)
from krok_helper.subtitle_render.domain.timing import (
    TimingTrack,
    guide_symbol_has_visual,
    guide_symbol_role_labels,
)
from krok_helper.subtitle_render.engine.render.effects.particles import FX_SPRITES
from krok_helper.subtitle_render.engine.render.elements.scanline_image import (
    resolve_scanline_image_path,
    scanline_image_role_geometry,
)
from krok_helper.subtitle_render.engine.render.elements.signal import (
    set_signal_auto_basis,
    signal_auto_basis,
)
from krok_helper.subtitle_render.domain.models import (
    TITLE_SCHEME_NAME,
    Style,
    TitleOverlay,
    normalize_title_char_role_labels,
    normalize_title_guide_symbols,
    resolve_lit_appearance,
    resolve_volume_appearance,
    style_for_track,
    style_to_dict,
    style_with_output_scanline,
    style_with_output_signal_offsets,
)
from krok_helper.subtitle_render.serialization.timing import guide_symbol_to_dict
from krok_helper.subtitle_render.engine.guide.semantics import guide_symbol_is_bitmap
from krok_helper.subtitle_render.native.protocol import (
    RENDER_IR_SCHEMA,
    FxPayloadTable,
    LineLayoutTable,
    VectorGlyphTable,
    bitmap_guide_to_ir,
    lines_style_to_ir,
    title_overlay_to_ir,
    track_to_ir,
)


def _apply_scanline_image_payload(style_payload: dict, style: Style) -> None:
    """自定义图片扫字线（2026-10）：路径两级回退解析 + role 模式几何物化。

    路径解析不到就清空（native 侧整段跳过图片层、回退普通高亮带）；
    role 模式把来源方案的装饰几何按「方案值，None 继承全局」物化成数值
    下发——native 端只有行样式，拿不到方案几何（与 lit/volume 的物化
    口径一致）。
    """

    style_payload["scanline_image_path"] = (
        resolve_scanline_image_path(
            str(style_payload.get("scanline_image_path") or "")
        )
        or ""
    )
    if (
        str(style_payload.get("scanline_shape") or "") == "image"
        and str(style_payload.get("scanline_mode") or "") == "role"
    ):
        geometry = scanline_image_role_geometry(style)
        if geometry is not None:
            style_payload["scanline_image_role_stroke_width_px"] = float(
                geometry["stroke_width_px"]
            )
            style_payload["scanline_image_role_stroke2_width_px"] = float(
                geometry["stroke2_width_px"]
            )
            style_payload["scanline_image_role_font_size_px"] = float(
                geometry["font_size_px"]
            )
            style_payload["scanline_image_role_glow_radius_px"] = float(
                geometry["glow_radius_px"]
            )
            style_payload["scanline_image_role_shadow_offset_x_px"] = float(
                geometry["shadow_offset_x_px"]
            )
            style_payload["scanline_image_role_shadow_offset_y_px"] = float(
                geometry["shadow_offset_y_px"]
            )
            style_payload["scanline_image_role_decoration_kind"] = str(
                geometry["decoration_kind"]
            )


def title_to_ir(
    track: TimingTrack,
    style: Style,
    *,
    duration_ms: int | None = None,
    overlay: TitleOverlay | None = None,
) -> dict[str, Any] | None:
    """Resolve one title overlay into a renderer-ready snapshot.

    ``overlay`` 缺省取第一条（单标题时期的调用方）。条目 ``scheme_name``
    引用的方案缺失时回落内置「标题」方案，与 Painter 侧解析一致。
    """

    title = resolve_title_overlay(style, overlay)
    if title is None or not title.enabled:
        return None
    text = resolve_title_text(title, track)
    if not any(line.strip() for line in text.split("\n")):
        return None
    scheme_name = title.scheme_name
    if not scheme_name or scheme_name not in style.custom_style_schemes:
        scheme_name = TITLE_SCHEME_NAME
    payload = title_overlay_to_ir(
        title,
        style.custom_style_schemes.get(scheme_name),
    )
    payload["text"] = text
    # 标题块=一页：逐行水平对齐按布局行槽位自上而下解析（不足取末行），
    # C++ 侧按行覆盖锚点缺省对齐，与 Painter 的逐行屏幕定位同口径。旧工程
    # （布局引用缺失）不下发：C++ 维持锚点水平位的既有回落，Painter 维持
    # 整块锚点 + 块内统一对齐的原语义。
    if title_layout_source(style, title.layout_index) is not None:
        payload["row_offsets_x"] = title_row_offsets_x(style, title, len(text.split("\n")))
        payload["row_alignments"] = title_row_alignments(
            style, title, len(text.split("\n"))
        )
    payload["windows"] = [
        list(window)
        for window in title_show_specs(title, track, duration_ms=duration_ms)
    ]
    labels = normalize_title_char_role_labels(text, title.char_role_labels)
    payload["resolved_role_labels"] = labels
    row_symbols, inline_symbols = normalize_title_guide_symbols(
        text, title.guide_symbols, title.inline_guide_symbols
    )
    payload["guide_symbols"] = [
        [row, _title_guide_to_ir(symbol)]
        for row, symbol in sorted(row_symbols.items())
    ]
    payload["inline_guide_symbols"] = [
        [row, index, _title_guide_to_ir(symbol)]
        for (row, index), symbol in sorted(inline_symbols.items())
    ]
    # 行前导唱符的逐个角色标签也要能解析到外观：与正文字符共用 role_styles。
    role_labels = {label for row in labels for label in row if label}
    role_labels.update(
        label
        for symbol in row_symbols.values()
        for label in guide_symbol_role_labels(symbol)
        if label
    )
    payload["role_styles"] = {
        label: title_overlay_to_ir(
            resolve_title_role_overlay(style, title, label),
            style.custom_style_schemes.get(label),
        )
        for label in sorted(role_labels)
    }
    return payload


# 标题动图以轨道时间零点为锚点，与 Python 标题层保持同步。
_TITLE_GUIDE_ANCHOR_MS = 0


def _title_guide_to_ir(symbol: object) -> dict[str, Any] | None:
    """把标题导唱符序列化成 sidecar 可解析的 IR 形态。

    位图复用歌词 ``bitmap_guide_to_ir`` 的键名与文件签名（缓存身份），
    矢量直接内嵌 ``guide_symbol_to_dict``（``parseVectorGlyph`` 同构）。
    """
    if not guide_symbol_has_visual(symbol):
        return None
    count = max(int(getattr(symbol, "count", 1) or 1), 1)
    guide_labels = [
        label or None for label in getattr(symbol, "role_labels", ()) or ()
    ][:count]
    if guide_symbol_is_bitmap(symbol):
        payload = dict(
            bitmap_guide_to_ir(symbol, anim_anchor_ms=_TITLE_GUIDE_ANCHOR_MS)
        )
        payload["kind"] = "bitmap"
        payload["count"] = count
        payload["role_labels"] = guide_labels
        return payload
    payload = {
        "kind": "vector",
        "count": count,
        "role_labels": guide_labels,
        "vector_glyph": guide_symbol_to_dict(symbol),
    }
    return payload


def titles_to_ir(
    track: TimingTrack,
    style: Style,
    *,
    duration_ms: int | None = None,
) -> list[dict[str, Any]]:
    """Resolve every enabled title overlay, preserving entry list order."""

    payloads: list[dict[str, Any]] = []
    for overlay in style.title_overlays:
        payload = title_to_ir(track, style, duration_ms=duration_ms, overlay=overlay)
        if payload is not None:
            payloads.append(payload)
    return payloads


def _apply_band_src_caps(payload: dict[str, Any], image_spec: str) -> None:
    """把**中段取图区间**下发给 sidecar（三份切法，切点=用户端点）。

    三段式口径：图片按端点切成 0/1/2 三份，0、2 画在带子左右两端，1 填缝。
    所以 ``line_band_src_cap_left_px`` 就是 ``line_band_cap_left_px``；单独
    下发是为了让 sidecar 不必自己推断，端点改了下发值跟着变（预拼条重建）。
    """
    try:
        left = int(payload.get("line_band_cap_left_px") or 0)
    except (TypeError, ValueError):
        left = 0
    try:
        right = int(payload.get("line_band_cap_right_px") or 0)
    except (TypeError, ValueError):
        right = 0
    payload["line_band_src_cap_left_px"] = max(left, 0)
    payload["line_band_src_cap_right_px"] = max(right, 0)


def build_render_ir(
    track: TimingTrack,
    style: Style,
    *,
    width: int,
    height: int,
    fps: int,
    dpr: float = 1.0,
    extra_tracks: list[TimingTrack] | None = None,
    duration_ms: int | None = None,
    relayout_scope: str | None = None,
    roll_shift_ms: int = 0,
) -> dict[str, Any]:
    """Build one JSON-friendly native snapshot from shared layout plans.

    ``relayout_scope``（新增入参，默认 None = 全量）：
    - ``None``：全轨重排——各源布局计划绕过缓存直接重建（时间/布局等
      变化走这条，行为与历史版本一致），结果写回缓存；
    - ``"titles"``：局部重排——只改了标题属性时，各源布局计划按
      (轨道值签名, 歌词布局样式签名) 命中缓存复用，签名不匹配的源自动
      回退重建（分轴粒度：主轨/副轨各自校验各自命中），标题部分始终
      重新序列化。签名是正确性闸门，scope 只是性能提示。
    - ``"paint"``：只改颜色/填充时复用同一布局计划；完整样式仍重新
        序列化给渲染后端，布局签名不匹配时同样自动回退重建。
    其余取值一律按全量处理（防御）。

    ``roll_shift_ms``：前黑开启时的歌词展示层整体后移量。只平移序列化
    payload 里的全局 ``timing_offset_ms``，每源 ``time_offset_delta_ms``
    仍从未平移的 style 计算——C++ 侧 ``payload全局 + meta + delta`` 对
    跟随 / 非跟随副轨都恰好 +shift，与 CPU Painter
    ``_effective_track_time_ms(track, t - roll_shift, style)`` 同口径。
    标题窗口本就是展示坐标，不受影响。
    """

    # 扫字线像素字段与指示灯/音量柱偏移存储恒为 1080 基准:发给 sidecar
    # 前一次性换算成本 IR 输出高度下的实画值(C++ 端继续直读,不感知基准
    # 语义)。
    style = style_with_output_signal_offsets(
        style_with_output_scanline(style, height), height
    )
    # 局部复用仅对已知 scope 生效;未知值按全量。
    use_plan_cache = relayout_scope in {"titles", "paint"}
    # 前黑偏移期间构建布局：入场/信号窗口的钳制下界随之放宽（与 CPU painter
    # 同口径；C++ 侧行窗口 = start - lead 无钳制，天然一致）。
    with layout_pass(), roll_shift_scope(roll_shift_ms):
        # 「跟随字体」大小推导基准（主轨信号宿主行最高频首角色方案）在
        # pass 顶部登记：IR 内不只 style 段物化尺寸，行布局（正文拓宽带/
        # union 摆放）同样消费信号尺寸，必须与 style 段同一基准；native
        # 只拿物化后的数值。主轨恒为本函数 track 参数（与 CPU
        # paint_frame_to_painter 同口径）。
        set_signal_auto_basis(style, track)
        # 主轨与附加轨共用一张轮廓表：同一 SVG 导唱符全片只序列化一次。
        glyph_table = VectorGlyphTable()
        fx_table = FxPayloadTable()
        layout_table = LineLayoutTable()
        # 按轴样式：主轨恒为全局 style；非跟随副轨叠加该轴时间 overrides。
        # 每源布局计划与 IR 序列化只用该源自己的 effective style；偏移差值
        # 经每源 meta.offset_ms 通道折算（C++ 侧窗口偏移 = 全局
        # timing_offset_ms + meta.offset_ms，见 gpu_scene_projection.cpp:483），
        # 使 GPU 的每轴有效偏移与 CPU painter 的 meta + 轴偏移一致。
        primary_style = style_for_track(style, track)
        primary_plan = build_track_layout_plan(
            track,
            primary_style,
            logical_w=width,
            logical_h=height,
            use_cache=use_plan_cache,
        )
        extra_sources = list(extra_tracks or ())
        extra_styles = [style_for_track(style, source) for source in extra_sources]
        extra_plans = [
            build_track_layout_plan(
                source,
                source_style,
                logical_w=width,
                logical_h=height,
                use_cache=use_plan_cache,
            )
            for source, source_style in zip(extra_sources, extra_styles, strict=True)
        ]
        # 行背景带：``preset:`` 说法在 Python 侧解析成绝对路径再下发（sidecar
        # 只认文件路径）；解析不到就清空，native 侧整段跳过。
        style_payload = style_to_dict(
            resolve_lit_appearance(
                resolve_volume_appearance(
                    style, auto_basis=signal_auto_basis(style)
                ),
                auto_basis=signal_auto_basis(style),
            )
        )
        band_path = resolve_band_render_image(
            str(style_payload.get("line_band_image") or ""),
            dynamic_notes_enabled=bool(
                getattr(style, "line_band_dynamic_notes_enabled", True)
            ),
            static_notes_enabled=bool(
                getattr(style, "line_band_static_notes_enabled", True)
            ),
        )
        style_payload["line_band_image"] = (
            str(band_path) if band_path is not None else ""
        )
        # 预设的独立谱线层（五线谱）：sidecar 只认文件路径。注意用原始
        # ``preset:`` 说法解析——上面已经把 line_band_image 换成绝对路径了。
        band_lines_path = resolve_band_lines_image(
            str(style.line_band_image or "")
        )
        style_payload["line_band_lines_image"] = (
            str(band_lines_path) if band_lines_path is not None else ""
        )
        band_clef_path = resolve_band_clef_image(
            str(style.line_band_image or "")
        )
        style_payload["line_band_clef_image"] = (
            str(band_clef_path) if band_clef_path is not None else ""
        )
        if band_clef_path is not None and not style_payload.get(
            "line_band_clef_color"
        ):
            style_payload["line_band_clef_color"] = default_clef_color_for(
                str(style.line_band_image or "")
            )
        if band_lines_path is not None and not style_payload.get(
            "line_band_line_color"
        ):
            style_payload["line_band_line_color"] = default_line_color_for(
                str(style.line_band_image or "")
            )
        _apply_band_src_caps(style_payload, str(style.line_band_image or ""))
        _apply_scanline_image_payload(style_payload, style)
        if roll_shift_ms:
            # 见 build_render_ir docstring：payload 全局偏移 +P，每源 delta
            # 不动，C++ 侧全源窗口恰好 +P（标题窗口不受全局偏移影响）。
            style_payload["timing_offset_ms"] = (
                int(style_payload.get("timing_offset_ms") or 0) + int(roll_shift_ms)
            )
        ir = {
            "schema": RENDER_IR_SCHEMA,
            "screen": {
                "width": max(int(width), 1),
                "height": max(int(height), 1),
                "fps": max(int(fps), 1),
                "dpr": max(float(dpr or 1.0), 0.01),
            },
            # auto 外观模式的音量柱/指示灯大小/颜色在序列化前物化成具体
            # 数值，native 端只消费数值（与 Painter 的 volume_style /
            # resolve_lit_appearance 投影同源）。
            "style": style_payload,
            # 装饰粒子 sprite 轮廓常量表（Python 单一事实源，native 不内置副本）。
            "fx_sprites": dict(FX_SPRITES),
            "track": track_to_ir(
                track,
                primary_style,
                layout_plan=primary_plan,
                glyph_table=glyph_table,
                fx_table=fx_table,
                layout_table=layout_table,
                time_offset_delta_ms=(
                    primary_style.timing_offset_ms - style.timing_offset_ms
                ),
            ),
            # Each source retains independent page/lane scheduling before the
            # renderer composites primary then extras.
            "extra_tracks": [
                track_to_ir(
                    source,
                    source_style,
                    layout_plan=plan,
                    glyph_table=glyph_table,
                    fx_table=fx_table,
                    layout_table=layout_table,
                    time_offset_delta_ms=(
                        source_style.timing_offset_ms - style.timing_offset_ms
                    ),
                )
                for source, source_style, plan in zip(
                    extra_sources, extra_styles, extra_plans, strict=True
                )
            ],
            "titles": titles_to_ir(track, style, duration_ms=duration_ms),
        }
        if not glyph_table.empty:
            ir["vector_glyphs"] = glyph_table.payload
        # 发射边界去重表：bursts 颜色/规格 + 行布局快照（sidecar 解析时
        # 展开回原字段，内存结构与渲染零变化）。
        ir.update(fx_table.payload())
        if layout_table.layouts:
            ir["line_layout_table"] = layout_table.payload()
        return ir


def build_style_patch_ir(
    track: TimingTrack,
    style: Style,
    *,
    width: int,
    height: int,
    fps: int,
    dpr: float = 1.0,
    extra_tracks: list[TimingTrack] | None = None,
    duration_ms: int | None = None,
    include_lines_style: bool = True,
    include_placement: bool = False,
    roll_shift_ms: int = 0,
) -> dict[str, Any]:
    """paint scope 差分更新载荷（``gpu_configure_style``）。

    轨道内容与布局签名不变、只有样式变化时，完整 ``configure`` 里 99% 的
    字节（逐字 text/时间 IR + 大 JSON 解析）都是重复劳动。本函数只产出会
    变的段：style / titles / fx_sprites / screen，外加**逐行样式派生字段**
    （动画档位 + 信号旗标 + 粒子 bursts——bursts 嵌有逐字解析色，改色也会
    变）。sidecar 在既有行数据上重放这些段，等价于一次全量重配。

    正确性闸门在调用方（GpuAsyncSubtitleRenderer）：布局签名/轨道签名/画面
    尺寸任一变化都不得走差分。布局计划按缓存复用（签名命中即几何不变）。
    """
    style = style_with_output_signal_offsets(
        style_with_output_scanline(style, height), height
    )
    with layout_pass(), roll_shift_scope(roll_shift_ms):
        # 推导基准同 build_render_ir：差分载荷与全量 configure 必须物化出
        # 同一组数值（含行布局的信号消费）。
        set_signal_auto_basis(style, track)
        fx_table = FxPayloadTable()
        layout_table = LineLayoutTable() if include_placement else None
        primary_style = style_for_track(style, track)
        primary_plan = build_track_layout_plan(
            track,
            primary_style,
            logical_w=width,
            logical_h=height,
            use_cache=True,
        )
        extra_sources = list(extra_tracks or ())
        extra_styles = [style_for_track(style, source) for source in extra_sources]
        extra_plans = [
            build_track_layout_plan(
                source,
                source_style,
                logical_w=width,
                logical_h=height,
                use_cache=True,
            )
            for source, source_style in zip(extra_sources, extra_styles, strict=True)
        ]
        lines_style: list[dict[str, Any]] = []
        if include_lines_style:
            # "titles" scope：标题属性编辑不动轨道与布局签名，行数据完全
            # 不变——省掉逐行 bursts 重规划（Python 侧 ~35ms 大头）。
            # "layout" scope（include_placement）：改字号/边距/行数后行级
            # 摆放字段变化，随 lines_style 附带原位更新，字符文本/时间
            # 不重发。
            lines_style.extend(lines_style_to_ir(
                track, primary_style, primary_plan, source_index=0,
                fx_table=fx_table, include_placement=include_placement,
                layout_table=layout_table,
            ))
            for source_index, (source, source_style, plan) in enumerate(
                zip(extra_sources, extra_styles, extra_plans, strict=True), start=1
            ):
                lines_style.extend(
                    lines_style_to_ir(
                        source, source_style, plan, source_index=source_index,
                        fx_table=fx_table, include_placement=include_placement,
                        layout_table=layout_table,
                    )
                )
        # 推导基准同 build_render_ir：主轨信号宿主行口径（pass 顶部登记），
        # 差分载荷与全量 configure 物化出同一组数值。
        patch_style_payload = style_to_dict(
            resolve_lit_appearance(
                resolve_volume_appearance(
                    style, auto_basis=signal_auto_basis(style)
                ),
                auto_basis=signal_auto_basis(style),
            )
        )
        patch_band_path = resolve_band_render_image(
            str(patch_style_payload.get("line_band_image") or ""),
            dynamic_notes_enabled=bool(
                getattr(style, "line_band_dynamic_notes_enabled", True)
            ),
            static_notes_enabled=bool(
                getattr(style, "line_band_static_notes_enabled", True)
            ),
        )
        patch_style_payload["line_band_image"] = (
            str(patch_band_path) if patch_band_path is not None else ""
        )
        patch_lines_path = resolve_band_lines_image(
            str(style.line_band_image or "")
        )
        patch_style_payload["line_band_lines_image"] = (
            str(patch_lines_path) if patch_lines_path is not None else ""
        )
        patch_clef_path = resolve_band_clef_image(
            str(style.line_band_image or "")
        )
        patch_style_payload["line_band_clef_image"] = (
            str(patch_clef_path) if patch_clef_path is not None else ""
        )
        if patch_clef_path is not None and not patch_style_payload.get(
            "line_band_clef_color"
        ):
            patch_style_payload["line_band_clef_color"] = default_clef_color_for(
                str(style.line_band_image or "")
            )
        if patch_lines_path is not None and not patch_style_payload.get(
            "line_band_line_color"
        ):
            patch_style_payload["line_band_line_color"] = default_line_color_for(
                str(style.line_band_image or "")
            )
        _apply_band_src_caps(
            patch_style_payload, str(style.line_band_image or "")
        )
        _apply_scanline_image_payload(patch_style_payload, style)
        if roll_shift_ms:
            # 与 build_render_ir 同口径：差分 style 段也带 +P 的全局偏移。
            patch_style_payload["timing_offset_ms"] = (
                int(patch_style_payload.get("timing_offset_ms") or 0)
                + int(roll_shift_ms)
            )
        return {
            "schema": RENDER_IR_SCHEMA,
            "screen": {
                "width": max(int(width), 1),
                "height": max(int(height), 1),
                "fps": max(int(fps), 1),
                "dpr": max(float(dpr or 1.0), 0.01),
            },
            "style": patch_style_payload,
            "titles": titles_to_ir(track, style, duration_ms=duration_ms),
            "fx_sprites": dict(FX_SPRITES),
            **({"lines_style": lines_style} if include_lines_style else {}),
            **fx_table.payload(),
            **(
                {"line_layout_table": layout_table.payload()}
                if layout_table is not None and layout_table.layouts
                else {}
            ),
        }
