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
    title_show_specs,
)
from krok_helper.subtitle_render.domain.timing import TimingTrack
from krok_helper.subtitle_render.domain.models import (
    TITLE_SCHEME_NAME,
    Style,
    TitleOverlay,
    normalize_title_char_role_labels,
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
) -> dict[str, Any]:
    """Build one JSON-friendly native snapshot from shared layout plans."""

    with layout_pass():
        # 主轨与附加轨共用一张轮廓表：同一 SVG 导唱符全片只序列化一次。
        glyph_table = VectorGlyphTable()
        primary_plan = build_track_layout_plan(
            track,
            style,
            logical_w=width,
            logical_h=height,
        )
        extra_sources = list(extra_tracks or ())
        extra_plans = [
            build_track_layout_plan(
                source,
                style,
                logical_w=width,
                logical_h=height,
            )
            for source in extra_sources
        ]
        ir = {
            "schema": RENDER_IR_SCHEMA,
            "screen": {
                "width": max(int(width), 1),
                "height": max(int(height), 1),
                "fps": max(int(fps), 1),
                "dpr": max(float(dpr or 1.0), 0.01),
            },
            "style": style_to_dict(style),
            "track": track_to_ir(track, style, layout_plan=primary_plan, glyph_table=glyph_table),
            # Each source retains independent page/lane scheduling before the
            # renderer composites primary then extras.
            "extra_tracks": [
                track_to_ir(source, style, layout_plan=plan, glyph_table=glyph_table)
                for source, plan in zip(extra_sources, extra_plans, strict=True)
            ],
            "titles": titles_to_ir(track, style, duration_ms=duration_ms),
        }
        if not glyph_table.empty:
            ir["vector_glyphs"] = glyph_table.payload
        return ir
