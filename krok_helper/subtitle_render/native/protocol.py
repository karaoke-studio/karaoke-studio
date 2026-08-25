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

from krok_helper.subtitle_render.engine.layout.plan.model import TrackLayoutPlan
from krok_helper.subtitle_render.engine.layout.page.plan import section_head_line_indices
from krok_helper.subtitle_render.domain.timing import (
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
    title_overlay_to_dict,
)
from krok_helper.subtitle_render.serialization.timing import guide_symbol_to_dict

RENDER_IR_SCHEMA = 1
GPU_UNSUPPORTED_FEATURE_LABELS = {
    "line_animation": "\u672a\u77e5\u6574\u884c\u52a8\u753b",
    "karaoke_animation": "\u672a\u77e5\u8d70\u5b57\u7279\u6548",
    "line_animation_override": "\u672a\u77e5\u9010\u884c\u7279\u6548",
    "bitmap_guide_symbol": "\u56fe\u7247\u5bfc\u5531\u7b26 / N3 Emoji \u5934\u50cf",
}


def gpu_unsupported_feature_labels(reasons: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(GPU_UNSUPPORTED_FEATURE_LABELS.get(reason, reason) for reason in reasons)


def title_overlay_to_ir(
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
    signal_head: bool = False,
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
        # 指示灯（SignalsLits 的全部 lit 样式）只画每 S 第一 P 第一行；
        # 旧宿主发的 IR 没有该字段，native 侧缺省按 true 解析保持旧行为。
        "signal_head": bool(signal_head),
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
) -> dict[str, Any]:
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
        guide_anchor_bounds = [item.guide_anchor_bounds for item in layout_plan.lines]
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
        guide_anchor_bounds = []
        page_indices = {}
        section_indices = {}
        page_offset_windows = {}
    signal_heads: frozenset[int] = frozenset()
    if style is not None and style.lit_enabled and not style.vertical:
        signal_heads = section_head_line_indices(
            track, style, section_gap_ms=max(style.section_gap_ms, 0)
        )
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
                signal_head=index in signal_heads,
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
