"""Pure model semantics for resolving and scheduling title overlays."""

from __future__ import annotations

from dataclasses import replace
from typing import Optional

from krok_helper.subtitle_render.engine.style_semantics import (
    effective_karaoke_colors,
    style_for_role,
)
from krok_helper.subtitle_render.engine.timeline import track_duration_ms
from krok_helper.subtitle_render.models import (
    TITLE_SCHEME_NAME,
    Style,
    TimingTrack,
    TitleOverlay,
)


def title_layout_source(style: Style, index: Optional[int]):
    """Resolve a title layout reference; dangling references return ``None``."""
    if index is None:
        return None
    index = int(index)
    if index == 0:
        return style
    if 1 <= index <= len(style.layouts):
        return style.layouts[index - 1]
    return None


def resolve_title_overlay(style: Style) -> Optional[TitleOverlay]:
    """Resolve the title scheme and layout reference into an effective overlay."""
    title = style.title_overlay
    if title is None:
        return None
    changes: dict[str, object] = {}
    if TITLE_SCHEME_NAME in style.custom_style_schemes:
        merged = style_for_role(style, TITLE_SCHEME_NAME)
        colors = effective_karaoke_colors(merged).before
        changes.update(
            font_family=merged.font_family,
            font_family_latin=merged.font_family_latin,
            font_size_px=int(merged.font_size_px),
            font_weight=int(merged.font_weight),
            italic=bool(merged.italic),
            letter_spacing_px=int(merged.letter_spacing_px),
            fill=colors.text,
            stroke=colors.stroke,
            stroke_width_px=int(merged.stroke_width_px),
            stroke2=colors.stroke2,
            stroke2_width_px=(
                int(merged.stroke2_width_px) if merged.stroke2_enabled else 0
            ),
            decoration_kind=merged.decoration_kind,
            glow_radius_px=int(merged.glow_before_radius_px),
            glow_concentration_level=int(merged.glow_concentration_level),
            shadow=colors.shadow,
            shadow_offset_x=int(merged.shadow_offset_x),
            shadow_offset_y=int(merged.shadow_offset_y),
        )
    source = title_layout_source(style, title.layout_index)
    if source is not None:
        alignments = list(source.line_alignments) or ["left"]
        horizontal = alignments[0]
        vertical = source.line_y_position
        letter_spacing = getattr(source, "letter_spacing_px", None)
        if letter_spacing is None:
            letter_spacing = style.letter_spacing_px
        changes.update(
            anchor=(
                "center"
                if (vertical, horizontal) == ("center", "center")
                else f"{vertical}_{horizontal}"
            ),
            align=horizontal,
            offset_x=int(source.horizontal_margin_px),
            offset_y=int(source.line_y_margin_px),
            line_gap_px=int(source.line_gap_px),
            letter_spacing_px=int(letter_spacing),
        )
    if not changes:
        return title
    return replace(title, **changes)


def resolve_title_role_overlay(
    style: Style, base: TitleOverlay, role_label: Optional[str]
) -> TitleOverlay:
    """Resolve a title character role into its static title appearance."""
    if not role_label or role_label not in style.custom_style_schemes:
        return base
    merged = style_for_role(style, role_label)
    colors = effective_karaoke_colors(merged).before
    layout_source = title_layout_source(style, base.layout_index)
    return replace(
        base,
        font_family=merged.font_family,
        font_family_latin=merged.font_family_latin,
        font_size_px=int(merged.font_size_px),
        font_weight=int(merged.font_weight),
        italic=bool(merged.italic),
        letter_spacing_px=(
            int(base.letter_spacing_px)
            if layout_source is not None
            else int(merged.letter_spacing_px)
        ),
        fill=colors.text,
        stroke=colors.stroke,
        stroke_width_px=int(merged.stroke_width_px),
        stroke2=colors.stroke2,
        stroke2_width_px=(
            int(merged.stroke2_width_px) if merged.stroke2_enabled else 0
        ),
        decoration_kind=merged.decoration_kind,
        glow_radius_px=int(merged.glow_before_radius_px),
        glow_concentration_level=int(merged.glow_concentration_level),
        shadow=colors.shadow,
        shadow_offset_x=int(merged.shadow_offset_x),
        shadow_offset_y=int(merged.shadow_offset_y),
    )


_TITLE_SEPARATOR_CHARS = " \t/|・-–—~　"


def resolve_title_text(title: TitleOverlay, track: TimingTrack) -> str:
    """Replace title metadata placeholders and clean orphan separators."""
    template = title.text_template or ""
    if "{title}" not in template and "{artist}" not in template:
        return template.strip("\n")
    meta_title = (track.meta.title or "").strip()
    meta_artist = (track.meta.artist or "").strip()
    text = template.replace("{title}", meta_title).replace("{artist}", meta_artist)
    lines = [
        raw.strip().strip(_TITLE_SEPARATOR_CHARS).strip()
        for raw in text.split("\n")
    ]
    return "\n".join(lines).strip("\n")


def title_show_window(
    title: TitleOverlay,
    track: TimingTrack,
    *,
    duration_ms: Optional[int] = None,
) -> list[tuple[int, int]]:
    """Return title visibility windows on the project timeline."""
    total = max(
        (
            int(duration_ms)
            if duration_ms is not None and int(duration_ms) > 0
            else track_duration_ms(track)
        ),
        0,
    )
    head_start = max(int(title.head_offset_ms), 0)
    duration = max(int(title.duration_ms), 0)
    tail_duration = max(
        int(title.tail_duration_ms)
        if title.tail_duration_ms is not None
        else duration,
        0,
    )
    tail_off = max(int(title.tail_offset_ms), 0)
    tail_base_end = max(total - tail_off, 0)
    tail_end = tail_base_end + (10 if total > 0 and tail_off == 0 else 0)
    if title.show_mode == "whole":
        return [(0, tail_end)]
    if title.show_mode == "head":
        return [(head_start, head_start + duration)]
    if title.show_mode == "tail":
        return [(max(tail_base_end - tail_duration, 0), tail_end)]
    return [
        (head_start, head_start + duration),
        (max(tail_base_end - tail_duration, 0), tail_end),
    ]


def title_show_specs(
    title: TitleOverlay,
    track: TimingTrack,
    *,
    duration_ms: Optional[int] = None,
) -> list[tuple[int, int, int, int]]:
    """Return visibility windows with independent head/tail transitions."""
    windows = title_show_window(title, track, duration_ms=duration_ms)
    head_fade_in = max(int(title.fade_in_ms), 0)
    head_fade_out = max(int(title.fade_out_ms), 0)
    tail_fade_in = max(
        int(title.tail_fade_in_ms)
        if title.tail_fade_in_ms is not None
        else head_fade_in,
        0,
    )
    tail_fade_out = max(
        int(title.tail_fade_out_ms)
        if title.tail_fade_out_ms is not None
        else head_fade_out,
        0,
    )
    if title.show_mode == "tail":
        return [(begin, end, tail_fade_in, tail_fade_out) for begin, end in windows]
    if title.show_mode == "head_tail" and len(windows) > 1:
        return [
            (*windows[0], head_fade_in, head_fade_out),
            (*windows[1], tail_fade_in, tail_fade_out),
        ]
    return [(begin, end, head_fade_in, head_fade_out) for begin, end in windows]


def title_overlay_opacity(
    title: Optional[TitleOverlay],
    track: TimingTrack,
    t_ms: int,
    *,
    duration_ms: Optional[int] = None,
) -> float:
    """Return title opacity at ``t_ms``, including fade transitions."""
    if title is None or not title.enabled:
        return 0.0
    best = 0.0
    for begin, end, fade_in, fade_out in title_show_specs(
        title, track, duration_ms=duration_ms
    ):
        if end <= begin or t_ms < begin or t_ms > end:
            continue
        alpha = 1.0
        if fade_in > 0 and t_ms < begin + fade_in:
            alpha = min(alpha, (t_ms - begin) / fade_in)
        if fade_out > 0 and t_ms > end - fade_out:
            alpha = min(alpha, (end - t_ms) / fade_out)
        best = max(best, max(0.0, min(1.0, alpha)))
    return best
