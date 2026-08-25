"""Pure horizontal karaoke wipe geometry and completion queries."""

from __future__ import annotations

from dataclasses import replace

from krok_helper.subtitle_render.engine.render.elements.horizontal.contracts import (
    FillSegment,
)
from krok_helper.subtitle_render.engine.ruby.timing import (
    _main_text_ruby_progress_ratio,
)
from krok_helper.subtitle_render.engine.text import GlyphLayout
from krok_helper.subtitle_render.engine.timing.timeline import char_fill_ratio


def adjust_fill_release_edges(segments: list[FillSegment]) -> list[FillSegment]:
    """Apply N3 ``AdjustWipeEnd`` at overlapping character boxes.

    N3 calculates the adjusted position ratio in DrawLeft/DrawRight layout-box
    coordinates, then applies that ratio to the transformed ink geometry used
    by WipeLeft. Bearings and the primary edge therefore affect the visible end
    point without changing the overlap decision itself.
    """

    adjusted = list(segments)
    for index in range(len(adjusted) - 1):
        current = adjusted[index]
        following = adjusted[index + 1]
        release_left = (
            current.release_left
            if current.release_left is not None
            else current.left
        )
        release_right = (
            current.release_right
            if current.release_right is not None
            else current.right
        )
        layout_left = (
            current.layout_left
            if current.layout_left is not None
            else release_left
        )
        layout_right = (
            current.layout_right
            if current.layout_right is not None
            else release_right
        )
        following_left = (
            following.layout_left
            if following.layout_left is not None
            else (
                following.release_left
                if following.release_left is not None
                else following.left
            )
        )
        following_right = (
            following.layout_right
            if following.layout_right is not None
            else (
                following.release_right
                if following.release_right is not None
                else following.right
            )
        )
        layout_width = max(layout_right - layout_left + 1, 1)
        if layout_left <= following_left:
            if layout_right >= following_left:
                pose = max(
                    0.0,
                    min(1.0, (following_left - layout_left) / layout_width),
                )
                adjusted[index] = replace(
                    current,
                    release_right=(
                        release_left + (release_right - release_left) * pose
                    ),
                )
        elif layout_left <= following_right:
            pose = max(
                0.0,
                min(1.0, (layout_right - following_right) / layout_width),
            )
            adjusted[index] = replace(
                current,
                release_left=(
                    release_right - (release_right - release_left) * pose
                ),
            )
    return adjusted


def offset_fill_segments(
    segments: list[FillSegment],
    dx: int,
) -> list[FillSegment]:
    if dx == 0:
        return segments
    return [
        FillSegment(
            left=segment.left + dx,
            right=segment.right + dx,
            release_left=(
                segment.release_left + dx
                if segment.release_left is not None
                else None
            ),
            release_right=(
                segment.release_right + dx
                if segment.release_right is not None
                else None
            ),
            layout_left=(
                segment.layout_left + dx
                if segment.layout_left is not None
                else None
            ),
            layout_right=(
                segment.layout_right + dx
                if segment.layout_right is not None
                else None
            ),
            start_ms=segment.start_ms,
            end_ms=segment.end_ms,
            ruby=segment.ruby,
            indices=segment.indices,
        )
        for segment in segments
    ]


def fill_extent_start(segments: list[FillSegment]) -> float | None:
    if not segments:
        return None
    first = segments[0]
    return first.release_left if first.release_left is not None else first.left


def segment_wipe_edges(segment: FillSegment) -> tuple[float, float]:
    """Return the N3 drawing edges used by the moving wipe front.

    ``left`` / ``right`` keep glyph-ink bounds for layout and ruby mapping;
    ``release_*`` are the full DrawLeft/DrawRight-style bounds. Characters
    without drawable ink deliberately remain zero-width, so their timing can
    consume time without moving the visible front.
    """

    if segment.right <= segment.left:
        return segment.left, segment.left
    left = (
        segment.release_left
        if segment.release_left is not None
        else segment.left
    )
    right = (
        segment.release_right
        if segment.release_right is not None
        else segment.right
    )
    return left, max(left, right)


def segment_wipe_times(segment: FillSegment) -> tuple[int, int]:
    """Return the effective N3 wipe window for one main-text segment."""

    if segment.ruby_base_index is not None:
        return int(segment.start_ms), int(segment.end_ms)
    if segment.ruby is not None:
        return int(segment.ruby.pos_start_ms), int(segment.ruby.pos_end_ms)
    return int(segment.start_ms), int(segment.end_ms)


def segment_fill_ratio(segment: FillSegment, t_ms: int) -> float:
    if segment.ruby is None:
        return char_fill_ratio(segment.start_ms, segment.end_ms, t_ms)
    if segment.ruby_base_index is not None:
        progress = _main_text_ruby_progress_ratio(
            segment.ruby,
            t_ms,
            mode="reading_units",
        )
        return max(
            0.0,
            min(
                1.0,
                progress * max(segment.ruby_base_count, 1)
                - segment.ruby_base_index,
            ),
        )
    return _main_text_ruby_progress_ratio(segment.ruby, t_ms)


def segment_wipe_band_at(
    segment: FillSegment,
    t_ms: int,
    rtl: bool,
) -> tuple[int, int]:
    """Return one segment's wipe band, including its zero-progress boundary."""

    wipe_left, wipe_right = segment_wipe_edges(segment)
    ratio = segment_fill_ratio(segment, t_ms)
    if rtl:
        boundary = wipe_right - int(round((wipe_right - wipe_left) * ratio))
        return boundary, wipe_right
    boundary = wipe_left + int(round((wipe_right - wipe_left) * ratio))
    return wipe_left, boundary


def n3_following_wipe_band(
    segments: list[FillSegment],
    indices: set[int],
    t_ms: int,
    rtl: bool,
) -> tuple[int, int] | None:
    """Keep a completed glyph on N3's shared front until its successor ends.

    N3 continues treating the preceding character as wiping while the next
    character is active. At the hand-off it uses the preceding segment's
    adjusted endpoint, then reuses the following segment's moving boundary.
    This compatibility behavior is intentionally limited to left-to-right
    text; the application's RTL path is independent.
    """

    if rtl or not indices:
        return None
    positions = [
        position
        for position, segment in enumerate(segments)
        if segment.indices and any(index in indices for index in segment.indices)
    ]
    if not positions:
        return None
    current_position = max(positions)
    if current_position >= len(segments) - 1:
        return None
    current = segments[current_position]
    following = segments[current_position + 1]
    if following.right <= following.left:
        return None
    current_start, current_end = segment_wipe_times(current)
    _following_start, following_end = segment_wipe_times(following)
    if not (
        current_start < t_ms < following_end
        and current_start != following_end
        and segment_fill_ratio(current, t_ms) >= 1.0
    ):
        return None
    if t_ms <= current_end:
        return segment_wipe_band_at(current, t_ms, rtl=False)
    return segment_wipe_band_at(following, t_ms, rtl=False)


def fill_extent_end(
    segments: list[FillSegment],
    t_ms: int,
) -> float:
    """Return the current right edge of the continuous karaoke scan.

    Motion uses the adjusted drawing bounds for the complete interval. Waiting
    until the completion frame to switch from ink bounds would cause a visible
    one-frame jump.
    """

    if not segments:
        return 0
    fill_end, _ = segment_wipe_edges(segments[0])
    for segment in segments:
        ratio = segment_fill_ratio(segment, t_ms)
        if ratio <= 0.0:
            break
        if segment.right <= segment.left:
            if ratio < 1.0:
                break
            continue
        wipe_left, wipe_right = segment_wipe_edges(segment)
        if ratio >= 1.0:
            fill_end = max(fill_end, wipe_right)
            continue
        fill_end = max(
            fill_end,
            wipe_left + int(round((wipe_right - wipe_left) * ratio)),
        )
        break
    return fill_end


def fill_extent_left(segments: list[FillSegment], t_ms: int) -> float:
    """Return the moving left edge for a right-to-left karaoke scan.

    This mirrors :func:`fill_extent_end` for the application's RTL extension.
    """

    if not segments:
        return 0
    _, scanline = segment_wipe_edges(segments[0])
    for segment in segments:
        ratio = segment_fill_ratio(segment, t_ms)
        if ratio <= 0.0:
            break
        if segment.right <= segment.left:
            if ratio < 1.0:
                break
            continue
        wipe_left, wipe_right = segment_wipe_edges(segment)
        if ratio >= 1.0:
            scanline = min(scanline, wipe_left)
            continue
        scanline = min(
            scanline,
            wipe_right - int(round((wipe_right - wipe_left) * ratio)),
        )
        break
    return scanline


def fill_clip_band(
    segments: list[FillSegment],
    t_ms: int,
    rtl: bool,
) -> tuple[int, int] | None:
    """Return the horizontal sung-region clip band, or ``None`` if empty.

    Left-to-right keeps the left edge fixed and advances the right edge;
    right-to-left keeps the right edge fixed and advances the left edge.
    """

    if not segments:
        return None
    if rtl:
        left = fill_extent_left(segments, t_ms)
        right = max(segment_wipe_edges(segment)[1] for segment in segments)
    else:
        left = fill_extent_start(segments)
        right = fill_extent_end(segments, t_ms)
    if left is None or right is None or right <= left:
        return None
    return left, right


def fill_clip_band_for_indices(
    segments: list[FillSegment],
    indices: set[int],
    t_ms: int,
    rtl: bool,
) -> tuple[int, int] | None:
    if not indices:
        return fill_clip_band(segments, t_ms, rtl)
    scoped = [
        segment
        for segment in segments
        if segment.indices and any(index in indices for index in segment.indices)
    ]
    while scoped and (
        scoped[0].right <= scoped[0].left
        or segment_fill_ratio(scoped[0], t_ms) <= 0.0
    ):
        scoped = scoped[1:]
    return fill_clip_band(scoped, t_ms, rtl)


def fill_clip_band_for_glyphs(
    segments: list[FillSegment],
    glyphs: list[GlyphLayout],
    t_ms: int,
    rtl: bool,
) -> tuple[int, int] | None:
    return fill_clip_band_for_indices(
        segments,
        {glyph.index for glyph in glyphs},
        t_ms,
        rtl,
    )


def run_fill_complete(
    segments: list[FillSegment],
    indices: set[int],
    t_ms: int,
) -> bool:
    """Return whether all wipe segments belonging to one glyph run are sung.

    Once complete, the sung layer no longer needs clipping at the scanline, so
    outer glow and outlines may extend fully at the line edge.
    """

    if indices:
        scoped = [
            segment
            for segment in segments
            if segment.indices and any(index in indices for index in segment.indices)
        ]
    else:
        scoped = segments
    return bool(scoped) and all(
        segment_fill_ratio(segment, t_ms) >= 1.0 for segment in scoped
    )


__all__ = [
    "adjust_fill_release_edges",
    "fill_clip_band",
    "fill_clip_band_for_glyphs",
    "fill_clip_band_for_indices",
    "fill_extent_end",
    "fill_extent_left",
    "fill_extent_start",
    "n3_following_wipe_band",
    "offset_fill_segments",
    "run_fill_complete",
    "segment_fill_ratio",
    "segment_wipe_band_at",
    "segment_wipe_edges",
    "segment_wipe_times",
]
