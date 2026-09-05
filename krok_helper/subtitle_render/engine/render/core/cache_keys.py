"""Stable cache keys for Painter layout data backed by mutable models."""

from __future__ import annotations

from krok_helper.subtitle_render.domain.models import Style
from krok_helper.subtitle_render.domain.timing import TimingLine, TimingTrack
from krok_helper.subtitle_render.engine.layout.page.plan import page_plan_signature
from krok_helper.subtitle_render.engine.layout.plan.cache import layout_cache_enabled
from krok_helper.subtitle_render.engine.value_signature import (
    lyric_layout_style_signature,
    value_signature,
)


def track_layout_signature(track: TimingTrack) -> tuple:
    """Describe track values that affect neighbouring-line layout."""

    return (
        tuple(
            (
                "".join(char.text for char in line.chars),
                tuple(
                    (index, char.role_label)
                    for index, char in enumerate(line.chars)
                    if char.role_label is not None
                ),
                line.singer_id,
                line.is_blank,
                line.layout_index,
                line.break_before,
                value_signature(line.animation_override),
                value_signature(line.guide_symbol),
                value_signature(line.inline_guide_symbols),
            )
            for line in track.lines
        ),
        tuple(
            (
                ruby.kanji,
                ruby.reading,
                tuple(ruby.reading_part_ms),
                ruby.pos_start_ms,
                ruby.pos_end_ms,
                tuple(ruby.reading_parts),
                ruby.target_line_index,
                ruby.target_char_start,
                ruby.target_char_end,
            )
            for ruby in track.rubies
        ),
        page_plan_signature(track),
        value_signature(track.loading_settings_snapshot),
        track.loading_settings_mode,
        (track.meta.silence_ms, track.meta.offset_ms),
    )


def line_layout_signature(line: TimingLine) -> tuple:
    """Describe one target line's timing inputs to cached layout geometry."""

    return (
        tuple(char.start_ms for char in line.chars),
        tuple(
            (index, char.pause_release_ms)
            for index, char in enumerate(line.chars)
            if char.pause_release_ms is not None
        ),
        tuple(
            (
                index,
                char.source_span_start_ms,
                char.source_span_end_ms,
                char.source_span_index,
                char.source_span_count,
            )
            for index, char in enumerate(line.chars)
            if char.source_span_count != 1 or char.source_span_start_ms is not None
        ),
        tuple(
            (index, char.explicit_start, char.explicit_end)
            for index, char in enumerate(line.chars)
            if char.explicit_start or char.explicit_end
        ),
        line.end_ms,
        line.display_start_override_ms,
        line.display_end_override_ms,
        value_signature(line.guide_symbol),
        value_signature(line.inline_guide_symbols),
    )


def layout_cache_signature(
    track: TimingTrack,
    display_style: Style,
) -> tuple | None:
    """Return the per-frame layout key, or disable caching for vertical text."""

    if not layout_cache_enabled() or display_style.vertical:
        return None
    # 样式侧只签歌词布局相关字段：标题属性（title_overlays 等）不进 key，
    # 否则改标题会把全部歌词行布局缓存连带作废、整轨重排。
    return (
        track_layout_signature(track),
        lyric_layout_style_signature(display_style),
    )


__all__ = [
    "layout_cache_signature",
    "line_layout_signature",
    "track_layout_signature",
]
