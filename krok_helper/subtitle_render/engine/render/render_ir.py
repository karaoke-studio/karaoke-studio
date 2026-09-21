"""Compose shared Painter layout semantics into the native renderer IR."""

from __future__ import annotations

from typing import Any

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
    title_show_specs,
)
from krok_helper.subtitle_render.domain.timing import TimingTrack
from krok_helper.subtitle_render.domain.models import (
    TITLE_SCHEME_NAME,
    Style,
    TitleOverlay,
    normalize_title_char_role_labels,
    resolve_volume_appearance,
    style_for_track,
    style_to_dict,
)
from krok_helper.subtitle_render.native.protocol import (
    RENDER_IR_SCHEMA,
    VectorGlyphTable,
    title_overlay_to_ir,
    track_to_ir,
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
        payload["row_alignments"] = title_row_alignments(
            style, title, len(text.split("\n"))
        )
    payload["windows"] = [
        list(window)
        for window in title_show_specs(title, track, duration_ms=duration_ms)
    ]
    labels = normalize_title_char_role_labels(text, title.char_role_labels)
    payload["resolved_role_labels"] = labels
    payload["role_styles"] = {
        label: title_overlay_to_ir(
            resolve_title_role_overlay(style, title, label),
            style.custom_style_schemes.get(label),
        )
        for row in labels
        for label in row
        if label
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
    """

    # 局部复用仅对已知 scope 生效；未知值按全量。
    use_plan_cache = relayout_scope in {"titles", "paint"}
    with layout_pass():
        # 主轨与附加轨共用一张轮廓表：同一 SVG 导唱符全片只序列化一次。
        glyph_table = VectorGlyphTable()
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
        ir = {
            "schema": RENDER_IR_SCHEMA,
            "screen": {
                "width": max(int(width), 1),
                "height": max(int(height), 1),
                "fps": max(int(fps), 1),
                "dpr": max(float(dpr or 1.0), 0.01),
            },
            # auto 外观模式的音量柱大小/颜色在序列化前物化成具体数值，
            # native 端只消费数值（与 Painter 的 volume_style 投影同源）。
            "style": style_to_dict(resolve_volume_appearance(style)),
            "track": track_to_ir(
                track,
                primary_style,
                layout_plan=primary_plan,
                glyph_table=glyph_table,
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
        return ir
