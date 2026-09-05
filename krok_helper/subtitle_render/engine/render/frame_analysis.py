"""Backend-neutral frame content queries with explicit geometry ports."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence

from krok_helper.subtitle_render.domain.models import Style, TitleOverlay
from krok_helper.subtitle_render.domain.timing import TimingTrack


FrameContent = tuple[int, Style, Sequence[object], Sequence[object], Sequence[tuple[TitleOverlay, float]]]


class VisibleContentResolver(Protocol):
    def __call__(
        self,
        track: TimingTrack,
        t_ms: int,
        style: Style,
        *,
        duration_ms: int | None = None,
        logical_w: int | None = None,
        logical_h: int | None = None,
    ) -> FrameContent: ...


class SubtitleBoundsResolver(Protocol):
    def __call__(
        self,
        logical_w: int,
        logical_h: int,
        track: TimingTrack,
        track_t_ms: int,
        style: Style,
        display_lines: Sequence[object],
        signal_lines: Sequence[object],
    ) -> tuple[int, int] | None: ...


class TitleBoundsResolver(Protocol):
    def __call__(
        self,
        logical_w: int,
        logical_h: int,
        track: TimingTrack,
        track_t_ms: int,
        style: Style,
        overlay: TitleOverlay,
        opacity: float,
    ) -> tuple[int, int] | None: ...


@dataclass(frozen=True)
class FrameAnalysisPorts:
    """Painter-specific operations required by frame content analysis."""

    resolve_visible_content: VisibleContentResolver
    subtitle_vertical_bounds: SubtitleBoundsResolver
    title_vertical_bounds: TitleBoundsResolver


def frame_has_content(
    track: TimingTrack | None,
    t_ms: int,
    style: Style,
    extra_tracks: list[TimingTrack] | None = None,
    *,
    duration_ms: int | None = None,
    logical_w: int | None = None,
    logical_h: int | None = None,
    ports: FrameAnalysisPorts,
) -> bool:
    """Return whether any main, extra, signal, or title content is visible."""

    if track is not None:
        _, _, display_lines, signal_lines, title_states = (
            ports.resolve_visible_content(
                track,
                t_ms,
                style,
                duration_ms=duration_ms,
                logical_w=logical_w,
                logical_h=logical_h,
            )
        )
        if display_lines or signal_lines or title_states:
            return True
    for extra in extra_tracks or ():
        _, _, display_lines, signal_lines, _unused = ports.resolve_visible_content(
            extra,
            t_ms,
            style,
            logical_w=logical_w,
            logical_h=logical_h,
        )
        if display_lines or signal_lines:
            return True
    return False


def frame_content_intervals(
    logical_w: int,
    logical_h: int,
    track: TimingTrack | None,
    t_ms: int,
    style: Style,
    extra_tracks: list[TimingTrack] | None = None,
    *,
    duration_ms: int | None = None,
    ports: FrameAnalysisPorts,
) -> list[tuple[int, int]] | None:
    """Return unmerged, clamped vertical intervals for visible frame groups."""

    track_entries: list[tuple[TimingTrack, bool]] = []
    if track is not None:
        track_entries.append((track, True))
    track_entries.extend((extra, False) for extra in extra_tracks or ())
    if not track_entries:
        return None

    intervals: list[tuple[int, int]] = []
    any_content = False
    for entry_track, with_title in track_entries:
        track_t_ms, display_style, display_lines, signal_lines, title_states = (
            ports.resolve_visible_content(
                entry_track,
                t_ms,
                style,
                duration_ms=duration_ms if with_title else None,
                logical_w=logical_w,
                logical_h=logical_h,
            )
        )
        if not with_title:
            title_states = ()
        if not display_lines and not signal_lines and not title_states:
            continue
        any_content = True
        if display_lines:
            lyric_bounds = ports.subtitle_vertical_bounds(
                logical_w,
                logical_h,
                entry_track,
                track_t_ms,
                display_style,
                display_lines,
                signal_lines,
            )
            if lyric_bounds is None:
                return None
            intervals.append(lyric_bounds)

        # 每个可见标题条目一个独立 interval（多标题天然拆组）。
        for overlay, opacity in title_states:
            title_bounds = ports.title_vertical_bounds(
                logical_w,
                logical_h,
                entry_track,
                track_t_ms,
                style,
                overlay,
                opacity,
            )
            if title_bounds is not None:
                intervals.append(title_bounds)

    if not any_content:
        return None
    clamped: list[tuple[int, int]] = []
    for top, bottom in intervals:
        clamped_top = max(0, top)
        clamped_bottom = min(logical_h - 1, bottom)
        if clamped_bottom >= clamped_top:
            clamped.append((clamped_top, clamped_bottom))
    return clamped or None


def frame_vertical_bounds(
    logical_w: int,
    logical_h: int,
    track: TimingTrack | None,
    t_ms: int,
    style: Style,
    extra_tracks: list[TimingTrack] | None = None,
    *,
    duration_ms: int | None = None,
    ports: FrameAnalysisPorts,
) -> tuple[int, int] | None:
    """Return the union of all visible frame content intervals."""

    intervals = frame_content_intervals(
        logical_w,
        logical_h,
        track,
        t_ms,
        style,
        extra_tracks,
        duration_ms=duration_ms,
        ports=ports,
    )
    if not intervals:
        return None
    top = min(item[0] for item in intervals)
    bottom = max(item[1] for item in intervals)
    if bottom < top:
        return None
    return top, bottom


__all__ = [
    "FrameAnalysisPorts",
    "frame_content_intervals",
    "frame_has_content",
    "frame_vertical_bounds",
]
