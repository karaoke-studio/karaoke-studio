"""Render IR v1 helpers for the native subtitle renderer sidecar.

C1 keeps the native boundary intentionally boring: Python owns project parsing
and UI state, then sends a JSON-serializable Render IR snapshot to the sidecar.
The first native renderer only uses a small subset of fields for smoke output,
but the IR already carries the full ``style_to_dict`` payload so future C2/C3
work can migrate painter features without changing the process protocol shape.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from krok_helper.subtitle_render.models import (
    RubyAnnotation,
    Style,
    SubtitleStyleScheme,
    TITLE_SCHEME_NAME,
    TimingChar,
    TimingLine,
    TimingTrack,
    TitleOverlay,
    effective_karaoke_animation,
    guide_symbol_to_dict,
    style_to_dict,
    title_overlay_to_dict,
)

RENDER_IR_SCHEMA = 1
GPU_UNSUPPORTED_FEATURE_LABELS = {
    "line_animation": "\u672a\u77e5\u6574\u884c\u52a8\u753b",
    "karaoke_animation": "\u672a\u77e5\u8d70\u5b57\u7279\u6548",
    "line_animation_override": "\u672a\u77e5\u9010\u884c\u7279\u6548",
    "bitmap_guide_symbol": "\u56fe\u7247\u5bfc\u5531\u7b26 / N3 Emoji \u5934\u50cf",
}


def gpu_unsupported_feature_labels(reasons: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(GPU_UNSUPPORTED_FEATURE_LABELS.get(reason, reason) for reason in reasons)

def _title_overlay_to_ir(
    title: TitleOverlay,
    scheme: SubtitleStyleScheme | None,
) -> dict[str, Any]:
    """Serialize a resolved title without inheriting Latin metrics from lyrics."""
    payload = title_overlay_to_dict(title)
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
    return payload


def gpu_unsupported_features(
    track: TimingTrack,
    style: Style,
    extra_tracks: list[TimingTrack] | None = None,
) -> tuple[str, ...]:
    """Return project features that require whole-frame Painter fallback."""
    reasons: list[str] = []
    sources = [track, *(extra_tracks or ())]
    if style.entry_anim not in {
        "none", "fade", "slide_in", "rise", "char_fade", "char_drip", "spin_flip", "utopia"
    } or (
        style.exit_anim not in {
            "none", "fade", "slide_out", "rise", "char_fade", "char_drip", "spin_flip", "utopia"
        }
    ):
        reasons.append("line_animation")
    if style.karaoke_anim not in {"inherit", "none", "utopia"}:
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
                } or line.animation_override.exit_anim not in {
                    "none",
                    "fade",
                    "slide_out",
                    "rise",
                    "char_fade",
                    "char_drip",
                    "spin_flip",
                    "utopia",
                }:
                    reasons.append("line_animation_override")
    return tuple(dict.fromkeys(reasons))


def title_to_ir(
    track: TimingTrack,
    style: Style,
    *,
    duration_ms: int | None = None,
) -> dict[str, Any] | None:
    """Resolve the Painter title contract into a renderer-ready snapshot."""
    # Keep the resolution logic shared with the CPU oracle.  In particular,
    # title schemes/layout references and metadata template cleanup must not be
    # reimplemented independently in the sidecar.
    from krok_helper.subtitle_render.engine.painter import (
        _resolve_title_role_overlay,
        _resolve_title_text,
        _title_show_specs,
        resolve_title_overlay,
    )
    from krok_helper.subtitle_render.models import normalize_title_char_role_labels

    title = resolve_title_overlay(style)
    if title is None or not title.enabled:
        return None
    text = _resolve_title_text(title, track)
    if not any(line.strip() for line in text.split("\n")):
        return None
    payload = _title_overlay_to_ir(
        title,
        style.custom_style_schemes.get(TITLE_SCHEME_NAME),
    )
    payload["text"] = text
    payload["windows"] = [
        list(window)
        for window in _title_show_specs(title, track, duration_ms=duration_ms)
    ]
    labels = normalize_title_char_role_labels(text, title.char_role_labels)
    payload["resolved_role_labels"] = labels
    payload["role_styles"] = {
        label: _title_overlay_to_ir(
            _resolve_title_role_overlay(style, title, label),
            style.custom_style_schemes.get(label),
        )
        for row in labels
        for label in row
        if label
    }
    return payload


def _image_file_signature(path_text: str | None) -> tuple[int, int]:
    if not path_text:
        return (0, 0)
    try:
        stat = Path(path_text).stat()
    except OSError:
        return (0, 0)
    return (max(int(stat.st_mtime_ns // 1_000_000), 0), max(int(stat.st_size), 0))


def bitmap_guide_to_ir(symbol: object | None) -> dict[str, Any] | None:
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
    }


def timing_char_to_ir(ch: TimingChar) -> dict[str, Any]:
    return {
        "text": ch.text,
        "start_ms": int(ch.start_ms),
        "explicit_start": bool(ch.explicit_start),
        "explicit_end": bool(ch.explicit_end),
        "pause_release_ms": (
            int(ch.pause_release_ms) if ch.pause_release_ms is not None else None
        ),
        "role_label": ch.role_label,
        "vector_glyph": guide_symbol_to_dict(ch.vector_glyph),
        "bitmap_guide": bitmap_guide_to_ir(ch.vector_glyph),
    }


def timing_line_to_ir(
    line: TimingLine,
    *,
    render_line: TimingLine | None = None,
    layout_style: Style | None = None,
    resolved_intervals: list[tuple[int, int]] | None = None,
    guide_anchor_bounds: tuple[int, int] | None = None,
    page_index: int = -1,
    page_line_count: int = 0,
    section_index: int = -1,
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
    layout_offset_x: float = 0.0,
    layout_offset_y: float = 0.0,
    layout_offset_windows: list[tuple[int, int, float, float]] | None = None,
) -> dict[str, Any]:
    render_line = render_line or line
    return {
        "chars": [timing_char_to_ir(ch) for ch in render_line.chars],
        "end_ms": int(line.end_ms) if line.end_ms is not None else None,
        "singer_label": line.singer_label,
        "singer_id": line.singer_id,
        "is_blank": bool(line.is_blank),
        "page_index": int(page_index),
        # 页内可渲染行数：Bottom 锚定的短页要从对齐列表末尾往回取（N3
        # ``CalcHorizontalAlignment``），native 侧靠这个值复现同一档对齐。
        "page_line_count": max(int(page_line_count), 0),
        "section_index": int(section_index),
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
            {
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
                "allow_biting": bool(layout_style.allow_biting),
                "ruby_interval_px": int(layout_style.ruby_interval_px),
                "ruby_alignment": layout_style.ruby_alignment,
                "ruby_gap_px": int(layout_style.ruby_gap_px),
            }
            if layout_style is not None
            else None
        ),
        "resolved_intervals": (
            [[int(start), int(end)] for start, end in resolved_intervals]
            if resolved_intervals is not None
            else None
        ),
        "guide_anchor_bounds": (
            [int(guide_anchor_bounds[0]), int(guide_anchor_bounds[1])]
            if guide_anchor_bounds is not None
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
    }


def track_to_ir(
    track: TimingTrack,
    style: Style | None = None,
    *,
    width: int | None = None,
    height: int | None = None,
) -> dict[str, Any]:
    schedule: dict[int, tuple[int, int, int]] = {}
    if style is not None:
        from krok_helper.subtitle_render.engine.painter import (
            _display_style_for_signal_window,
            _lane_count,
            _line_with_guide_symbol,
            _line_center_override,
            _row_count_resolver,
            _style_for_line,
            _style_for_line_display_window,
            display_schedule_for_style,
            resolved_char_intervals_for_line,
            resolved_guide_anchor_bounds_for_line,
            resolved_page_offset_windows_for_style,
        )
        from krok_helper.subtitle_render.engine.page_plan import resolve_page_plan
        display_style = _display_style_for_signal_window(style)
        schedule = display_schedule_for_style(
            track,
            display_style,
            logical_w=width,
            logical_h=height,
        )
        page_offset_windows = (
            resolved_page_offset_windows_for_style(
                max(int(width), 1),
                max(int(height), 1),
                track,
                display_style,
            )
            if width is not None and height is not None
            else {}
        )
        render_lines = [_line_with_guide_symbol(line) for line in track.lines]
        layout_styles = [_style_for_line(style, line) for line in track.lines]
        resolved_intervals = [
            resolved_char_intervals_for_line(line, style) for line in render_lines
        ]
        guide_anchor_bounds = [
            resolved_guide_anchor_bounds_for_line(track, line, style)
            for line in track.lines
        ]
        center_overrides = {
            index: _line_center_override(track, line, layout_styles[index])
            for index, line in enumerate(track.lines)
        }
        animation_styles = [
            _style_for_line_display_window(
                style,
                line,
                schedule[index][1] if index in schedule else None,
                schedule[index][2] if index in schedule else None,
            )
            for index, line in enumerate(track.lines)
        ]
        from krok_helper.subtitle_render.engine.timeline import assign_lanes

        # 页内行数按 Painter 的口径算（``_renderable_page_lines`` 同样只走
        # assign_lanes），保证 native 解出的对齐档与 Painter 一致。
        renderable_lines = [
            (index, line)
            for index, line in enumerate(track.lines)
            if not line.is_blank and line.chars
        ]
        _lanes, lane_page_starts, lane_page_rows = assign_lanes(
            [line for _, line in renderable_lines],
            _lane_count(style),
            _row_count_resolver(style),
            section_gap_ms=style.section_gap_ms,
        )
        page_line_counts = {
            track_index: lane_page_rows[render_index]
            for render_index, (track_index, _) in enumerate(renderable_lines)
        }
        authored_lanes = {
            track_index: _lanes[render_index]
            for render_index, (track_index, _) in enumerate(renderable_lines)
        }
        if track.page_plan is not None:
            resolved_plan = resolve_page_plan(track, style)
            page_indices = {
                item.track_line_index: item.global_page_index
                for item in resolved_plan.lines
            }
            section_indices = {
                item.track_line_index: item.section_index
                for item in resolved_plan.lines
            }
            page_line_counts = {
                item.track_line_index: item.page_line_count
                for item in resolved_plan.lines
            }
            authored_lanes = {
                item.track_line_index: item.lane
                for item in resolved_plan.lines
            }
        else:
            page_indices = {
                track_index: lane_page_starts[render_index]
                for render_index, (track_index, _) in enumerate(renderable_lines)
            }
            section_indices = {}
            renderable_only = [line for _, line in renderable_lines]
            for render_index, (track_index, _line) in enumerate(renderable_lines):
                page_start = lane_page_starts[render_index]
                page_rows = lane_page_rows[render_index]
                page_head = renderable_only[page_start]
                page_style = _style_for_line(style, page_head)
                configured_rows = _lane_count(page_style)
                if page_rows >= configured_rows:
                    continue
                if page_style.line_y_position == "bottom":
                    authored_lanes[track_index] += configured_rows - page_rows
                elif page_style.line_y_position == "center":
                    authored_lanes[track_index] += max(
                        (configured_rows - page_rows + 1) // 2,
                        0,
                    )
    else:
        page_line_counts = {}
        authored_lanes = {}
        center_overrides = {}
        animation_styles = []
        layout_styles = []
        render_lines = []
        resolved_intervals = []
        guide_anchor_bounds = []
        page_indices = {}
        section_indices = {}
        page_offset_windows = {}
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
                render_line=(render_lines[index] if style is not None else None),
                layout_style=(layout_styles[index] if style is not None else None),
                resolved_intervals=(
                    resolved_intervals[index] if style is not None else None
                ),
                guide_anchor_bounds=(
                    guide_anchor_bounds[index] if style is not None else None
                ),
                page_index=page_indices.get(index, -1),
                page_line_count=page_line_counts.get(index, 0),
                section_index=section_indices.get(index, -1),
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
                layout_offset_windows=list(page_offset_windows.get(index, ())),
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
    duration_ms: int | None = None,
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
        "track": track_to_ir(track, style, width=width, height=height),
        # Keep each source separate.  Painter schedules lanes/display windows
        # independently per source and then composites primary -> extras.
        "extra_tracks": [
            track_to_ir(item, style, width=width, height=height)
            for item in extra_tracks or ()
        ],
        "title": title_to_ir(track, style, duration_ms=duration_ms),
    }
