"""Render IR v1 helpers for the native subtitle renderer sidecar.

C1 keeps the native boundary intentionally boring: Python owns project parsing
and UI state, then sends a JSON-serializable Render IR snapshot to the sidecar.
The first native renderer only uses a small subset of fields for smoke output,
but the IR already carries the full ``style_to_dict`` payload so future C2/C3
work can migrate painter features without changing the process protocol shape.
"""

from __future__ import annotations

from typing import Any

from krok_helper.subtitle_render.models import (
    RubyAnnotation,
    Style,
    TimingChar,
    TimingLine,
    TimingTrack,
    style_to_dict,
    title_overlay_to_dict,
)

RENDER_IR_SCHEMA = 1


def gpu_unsupported_features(
    track: TimingTrack,
    style: Style,
    extra_tracks: list[TimingTrack] | None = None,
) -> tuple[str, ...]:
    """Return project features that require whole-frame Painter fallback."""
    reasons: list[str] = []
    if style.vertical:
        reasons.append("vertical")
    if style.right_to_left:
        reasons.append("right_to_left")
    if style.line_horizontal_layout == "per_row":
        reasons.append("per_row_layout")
    if style.lit_enabled and style.lit_style != "volume":
        reasons.append("signal_lits")
    if style.entry_anim not in {
        "none", "fade", "slide_in", "rise", "char_fade", "spin_flip", "utopia"
    } or (
        style.exit_anim not in {
            "none", "fade", "slide_out", "rise", "char_fade", "spin_flip", "utopia"
        }
    ):
        reasons.append("line_animation")
    if (
        style.viewport_scale_pct != 100
        or style.viewport_rotation_deg != 0
        or style.viewport_offset_x != 0
        or style.viewport_offset_y != 0
    ):
        reasons.append("viewport_transform")
    for source in [track, *(extra_tracks or ())]:
        for line in source.lines:
            if line.layout_index != 0:
                reasons.append("line_layout_override")
            if line.animation_override is not None:
                if line.animation_override.entry_anim not in {
                    "none",
                    "fade",
                    "slide_in",
                    "rise",
                    "char_fade",
                    "spin_flip",
                    "utopia",
                } or line.animation_override.exit_anim not in {
                    "none",
                    "fade",
                    "slide_out",
                    "rise",
                    "char_fade",
                    "spin_flip",
                    "utopia",
                }:
                    reasons.append("line_animation_override")
            if line.guide_symbol is not None or line.inline_guide_symbols:
                reasons.append("guide_symbol")
            if any(
                ch.source_span_count != 1 or ch.source_span_start_ms is not None
                for ch in line.chars
            ):
                reasons.append("shared_timing_span")
    return tuple(dict.fromkeys(reasons))


def title_to_ir(track: TimingTrack, style: Style) -> dict[str, Any] | None:
    """Resolve the Painter title contract into a renderer-ready snapshot."""
    # Keep the resolution logic shared with the CPU oracle.  In particular,
    # title schemes/layout references and metadata template cleanup must not be
    # reimplemented independently in the sidecar.
    from krok_helper.subtitle_render.engine.painter import (
        _resolve_title_role_overlay,
        _resolve_title_text,
        _title_show_window,
        resolve_title_overlay,
    )
    from krok_helper.subtitle_render.models import normalize_title_char_role_labels

    title = resolve_title_overlay(style)
    if title is None or not title.enabled:
        return None
    text = _resolve_title_text(title, track)
    if not any(line.strip() for line in text.split("\n")):
        return None
    payload = title_overlay_to_dict(title)
    payload["text"] = text
    payload["windows"] = [list(window) for window in _title_show_window(title, track)]
    labels = normalize_title_char_role_labels(text, title.char_role_labels)
    payload["resolved_role_labels"] = labels
    payload["role_styles"] = {
        label: title_overlay_to_dict(_resolve_title_role_overlay(style, title, label))
        for row in labels
        for label in row
        if label
    }
    return payload


def timing_char_to_ir(ch: TimingChar) -> dict[str, Any]:
    return {
        "text": ch.text,
        "start_ms": int(ch.start_ms),
        "pause_release_ms": (
            int(ch.pause_release_ms) if ch.pause_release_ms is not None else None
        ),
        "role_label": ch.role_label,
    }


def timing_line_to_ir(
    line: TimingLine,
    *,
    lane: int = 0,
    display_start_ms: int | None = None,
    display_end_ms: int | None = None,
    center_override: bool = False,
    entry_anim: str = "none",
    entry_duration_ms: int = 0,
    exit_anim: str = "none",
    exit_duration_ms: int = 0,
) -> dict[str, Any]:
    return {
        "chars": [timing_char_to_ir(ch) for ch in line.chars],
        "end_ms": int(line.end_ms) if line.end_ms is not None else None,
        "singer_label": line.singer_label,
        "singer_id": line.singer_id,
        "is_blank": bool(line.is_blank),
        "lane": int(lane),
        "display_start_ms": (
            int(display_start_ms) if display_start_ms is not None else None
        ),
        "display_end_ms": int(display_end_ms) if display_end_ms is not None else None,
        "center_override": bool(center_override),
        "entry_anim": str(entry_anim),
        "entry_duration_ms": max(int(entry_duration_ms), 0),
        "exit_anim": str(exit_anim),
        "exit_duration_ms": max(int(exit_duration_ms), 0),
    }


def ruby_to_ir(ruby: RubyAnnotation) -> dict[str, Any]:
    return {
        "kanji": ruby.kanji,
        "reading": ruby.reading,
        "reading_part_ms": [int(item) for item in ruby.reading_part_ms],
        "reading_parts": list(ruby.reading_parts),
        "pos_start_ms": int(ruby.pos_start_ms),
        "pos_end_ms": int(ruby.pos_end_ms),
    }


def track_to_ir(track: TimingTrack, style: Style | None = None) -> dict[str, Any]:
    schedule: dict[int, tuple[int, int, int]] = {}
    if style is not None:
        from krok_helper.subtitle_render.engine.painter import (
            _display_style_for_signal_window,
            _line_center_override,
            display_schedule_for_style,
        )
        from krok_helper.subtitle_render.models import style_with_line_animation

        display_style = _display_style_for_signal_window(style)
        schedule = display_schedule_for_style(track, display_style)
        center_overrides = {
            index: _line_center_override(track, line, style)
            for index, line in enumerate(track.lines)
        }
        animation_styles = [style_with_line_animation(style, line) for line in track.lines]
    else:
        center_overrides = {}
        animation_styles = []
    return {
        "meta": {
            "title": track.meta.title,
            "artist": track.meta.artist,
            "album": track.meta.album,
            "tagging_by": track.meta.tagging_by,
            "silence_ms": int(track.meta.silence_ms),
            "offset_ms": int(track.meta.offset_ms),
            "custom": list(track.meta.custom),
        },
        "lines": [
            timing_line_to_ir(
                line,
                lane=schedule.get(index, (0, 0, 0))[0],
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
            )
            for index, line in enumerate(track.lines)
        ],
        "rubies": [ruby_to_ir(ruby) for ruby in track.rubies],
    }


def build_render_ir(
    track: TimingTrack,
    style: Style,
    *,
    width: int,
    height: int,
    fps: int,
    dpr: float = 1.0,
    extra_tracks: list[TimingTrack] | None = None,
) -> dict[str, Any]:
    """Build a JSON-friendly Render IR v1 snapshot for the native sidecar.

    ``dpr`` 是预览缩放系数：布局仍在 ``width``/``height`` 逻辑坐标系计算，
    native 光栅化画布为 ``round(width*dpr) x round(height*dpr)``,
    与 Python 预览 ``preview_render_target_size`` + ``setDevicePixelRatio`` 语义一致。
    """
    return {
        "schema": RENDER_IR_SCHEMA,
        "screen": {
            "width": max(int(width), 1),
            "height": max(int(height), 1),
            "fps": max(int(fps), 1),
            "dpr": max(float(dpr or 1.0), 0.01),
        },
        "style": style_to_dict(style),
        "track": track_to_ir(track, style),
        # Keep each source separate.  Painter schedules lanes/display windows
        # independently per source and then composites primary -> extras.
        "extra_tracks": [track_to_ir(item, style) for item in extra_tracks or ()],
        "title": title_to_ir(track, style),
    }
