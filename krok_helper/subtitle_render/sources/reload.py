"""Merge externally reloaded subtitle sources with renderer-local overlays."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from difflib import SequenceMatcher
from krok_helper.subtitle_render.timing import (
    TimingLine,
    TimingTrack,
    guide_symbol_replacement_count,
)


@dataclass(frozen=True)
class TrackReloadMerge:
    """Result of merging a newly parsed source with renderer-local state."""

    track: TimingTrack
    conflicts: tuple[str, ...] = ()
    structure_changed: bool = False
    timing_only: bool = False


def track_structure_signature(track: TimingTrack) -> tuple:
    """Return the source text structure, excluding all timing information."""

    return tuple(
        (line.is_blank, tuple(char.text for char in line.chars))
        for line in track.lines
    )


def source_content_signature(track: TimingTrack) -> tuple:
    """Return source-owned semantic content while excluding timestamps."""

    meta = track.meta
    return (
        meta.title,
        meta.artist,
        meta.album,
        meta.tagging_by,
        tuple(meta.custom),
        tuple(
            (
                line.is_blank,
                line.singer_label,
                line.singer_id,
                tuple((char.text, char.role_label) for char in line.chars),
            )
            for line in track.lines
        ),
        tuple(
            (
                ruby.kanji,
                ruby.reading,
                tuple(ruby.reading_parts),
            )
            for ruby in track.rubies
        ),
    )


def merge_reloaded_track(
    current: TimingTrack,
    baseline: TimingTrack,
    candidate: TimingTrack,
    *,
    preserve_page_structure: bool = True,
) -> TrackReloadMerge:
    """Apply local renderer edits from ``current`` onto a fresh source parse.

    ``baseline`` is the source parse used before renderer-local edits.  Comparing
    it with ``current`` lets role assignments made in the renderer remain sparse
    overrides while fresh LRC/SUG role labels continue to come from ``candidate``.
    """

    structure_changed = track_structure_signature(baseline) != track_structure_signature(
        candidate
    )
    timing_only = (
        not structure_changed
        and source_content_signature(baseline) == source_content_signature(candidate)
        and baseline != candidate
    )
    merged = deepcopy(candidate)
    merged.page_plan = deepcopy(current.page_plan) if preserve_page_structure else None
    merged.loading_settings_mode = current.loading_settings_mode
    merged.loading_settings = deepcopy(current.loading_settings)
    merged.loading_settings_snapshot = deepcopy(current.loading_settings_snapshot)
    conflicts: list[str] = []

    line_matches = _match_lines(baseline, candidate)
    for old_index, new_index, exact_text, reliable in line_matches:
        if old_index >= len(current.lines):
            continue
        if not reliable and _line_has_local_state(
            current.lines[old_index],
            baseline.lines[old_index],
            include_page_projection=preserve_page_structure,
        ):
            conflicts.append(f"第 {old_index + 1} 行歌词重复，原有字幕设置无法唯一定位")
            continue
        _merge_line_overlays(
            current.lines[old_index],
            baseline.lines[old_index],
            merged.lines[new_index],
            old_index=old_index,
            exact_text=exact_text,
            conflicts=conflicts,
            preserve_page_projection=preserve_page_structure,
        )

    matched_old = {old for old, _new, _exact, _reliable in line_matches}
    for old_index, old_source in enumerate(baseline.lines):
        if old_index in matched_old or old_index >= len(current.lines):
            continue
        if _line_has_local_state(
            current.lines[old_index],
            old_source,
            include_page_projection=preserve_page_structure,
        ):
            conflicts.append(f"第 {old_index + 1} 行已被删除，原有字幕设置无法定位")

    return TrackReloadMerge(
        track=merged,
        conflicts=tuple(conflicts),
        structure_changed=structure_changed,
        timing_only=timing_only,
    )


def _line_text(line: TimingLine) -> tuple[str, ...]:
    return tuple(char.text for char in line.chars)


def _match_lines(
    old_track: TimingTrack, new_track: TimingTrack
) -> list[tuple[int, int, bool, bool]]:
    old_texts = [_line_text(line) for line in old_track.lines]
    new_texts = [_line_text(line) for line in new_track.lines]
    if old_texts == new_texts:
        return [(index, index, True, True) for index in range(len(old_texts))]
    old_counts = {text: old_texts.count(text) for text in set(old_texts)}
    new_counts = {text: new_texts.count(text) for text in set(new_texts)}
    matcher = SequenceMatcher(a=old_texts, b=new_texts, autojunk=False)
    pairs: list[tuple[int, int, bool, bool]] = []
    for tag, old_start, old_end, new_start, new_end in matcher.get_opcodes():
        if tag == "equal":
            pairs.extend(
                (
                    old_start + offset,
                    new_start + offset,
                    True,
                    old_counts[old_texts[old_start + offset]] == 1
                    and new_counts[new_texts[new_start + offset]] == 1,
                )
                for offset in range(old_end - old_start)
            )
        elif tag == "replace":
            pairs.extend(
                (old_start + offset, new_start + offset, False, True)
                for offset in range(min(old_end - old_start, new_end - new_start))
            )
    return pairs


def _merge_line_overlays(
    current: TimingLine,
    baseline: TimingLine,
    target: TimingLine,
    *,
    old_index: int,
    exact_text: bool,
    conflicts: list[str],
    preserve_page_projection: bool = True,
) -> None:
    if preserve_page_projection:
        target.layout_index = current.layout_index
        target.break_before = current.break_before
    target.display_start_override_ms = current.display_start_override_ms
    target.display_end_override_ms = current.display_end_override_ms
    target.animation_override = deepcopy(current.animation_override)

    if current.guide_symbol is not None:
        symbol = deepcopy(current.guide_symbol)
        if symbol.replacement_prefix and guide_symbol_replacement_count(target, symbol) == 0:
            conflicts.append(f"第 {old_index + 1} 行的前缀导唱符与新歌词不匹配")
        else:
            target.guide_symbol = symbol

    if current.inline_guide_symbols:
        if exact_text:
            target.inline_guide_symbols = deepcopy(current.inline_guide_symbols)
        else:
            conflicts.append(f"第 {old_index + 1} 行的行内导唱符无法可靠定位")

    if exact_text:
        for char_index, (old_char, source_char, new_char) in enumerate(
            zip(current.chars, baseline.chars, target.chars)
        ):
            if old_char.role_label != source_char.role_label:
                new_char.role_label = old_char.role_label
        return

    old_chars = [char.text for char in baseline.chars]
    new_chars = [char.text for char in target.chars]
    char_matcher = SequenceMatcher(a=old_chars, b=new_chars, autojunk=False)
    mapped: dict[int, int] = {}
    for block in char_matcher.get_matching_blocks():
        for offset in range(block.size):
            mapped[block.a + offset] = block.b + offset
    for char_index, (old_char, source_char) in enumerate(zip(current.chars, baseline.chars)):
        if old_char.role_label == source_char.role_label:
            continue
        new_index = mapped.get(char_index)
        if new_index is None:
            conflicts.append(
                f"第 {old_index + 1} 行第 {char_index + 1} 字的角色覆盖无法定位"
            )
            continue
        target.chars[new_index].role_label = old_char.role_label


def _line_has_local_state(
    current: TimingLine,
    baseline: TimingLine,
    *,
    include_page_projection: bool = True,
) -> bool:
    if (
        (
            include_page_projection
            and (current.layout_index or current.break_before != "none")
        )
        or current.display_start_override_ms is not None
        or current.display_end_override_ms is not None
        or current.animation_override is not None
        or current.guide_symbol is not None
        or current.inline_guide_symbols
    ):
        return True
    return any(
        old.role_label != source.role_label
        for old, source in zip(current.chars, baseline.chars)
    )
