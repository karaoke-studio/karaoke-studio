"""Merge externally reloaded subtitle sources with renderer-local overlays."""

from __future__ import annotations

from collections.abc import Iterable
from copy import deepcopy
from dataclasses import dataclass
from difflib import SequenceMatcher
import hashlib
from pathlib import Path
from typing import Callable, Protocol

from krok_helper.subtitle_render.domain.timing import (
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


@dataclass(frozen=True)
class TrackReloadPlan:
    """Pure merge plan for every project track backed by one source file."""

    primary_merge: TrackReloadMerge | None
    extra_merges: tuple[tuple[int, TrackReloadMerge], ...]
    conflicts: tuple[str, ...]
    structure_changed: bool
    timing_only: bool


@dataclass(frozen=True)
class PreparedTrackReload:
    """Stable source snapshot and optional merge plan for one file event."""

    digest: str
    candidate: TimingTrack | None
    plan: TrackReloadPlan | None


class TrackReloadTarget(Protocol):
    """Project-facing contract required to install merged timing tracks."""

    def replace_track(self, index: int, track: TimingTrack) -> bool: ...


def source_file_digest(path: Path) -> str:
    """Return the content digest used to suppress duplicate file events."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def plan_reloaded_tracks(
    baseline: TimingTrack,
    candidate: TimingTrack,
    *,
    primary_track: TimingTrack | None = None,
    extra_tracks: Iterable[tuple[int, TimingTrack]] = (),
) -> TrackReloadPlan:
    """Plan a source reload without mutating project or presentation state."""

    primary_merge = (
        merge_reloaded_track(primary_track, baseline, candidate)
        if primary_track is not None
        else None
    )
    extra_merges = tuple(
        (index, merge_reloaded_track(track, baseline, candidate))
        for index, track in extra_tracks
    )
    merges = (
        ((primary_merge,) if primary_merge is not None else ())
        + tuple(merge for _index, merge in extra_merges)
    )
    conflicts = tuple(
        dict.fromkeys(conflict for merge in merges for conflict in merge.conflicts)
    )
    return TrackReloadPlan(
        primary_merge=primary_merge,
        extra_merges=extra_merges,
        conflicts=conflicts,
        structure_changed=any(merge.structure_changed for merge in merges),
        timing_only=bool(merges) and all(merge.timing_only for merge in merges),
    )


def prepare_reloaded_tracks(
    path: Path,
    *,
    seen_digest: str,
    baseline: TimingTrack,
    load_candidate: Callable[[Path], TimingTrack],
    primary_track: TimingTrack | None = None,
    extra_tracks: Iterable[tuple[int, TimingTrack]] = (),
) -> PreparedTrackReload:
    """Read one stable source snapshot and build its project-wide merge plan.

    A candidate of ``None`` means the file content is byte-identical to the
    already-seen source.  A candidate with no plan means parsing produced the
    same source-owned track as the current baseline.
    """
    source_path = Path(path)
    before = source_path.stat()
    digest = source_file_digest(source_path)
    if digest == seen_digest:
        return PreparedTrackReload(digest=digest, candidate=None, plan=None)

    candidate = load_candidate(source_path)
    after = source_path.stat()
    if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
        raise OSError("字幕文件仍在写入")
    if baseline == candidate:
        return PreparedTrackReload(digest=digest, candidate=candidate, plan=None)

    return PreparedTrackReload(
        digest=digest,
        candidate=candidate,
        plan=plan_reloaded_tracks(
            baseline,
            candidate,
            primary_track=primary_track,
            extra_tracks=extra_tracks,
        ),
    )


def apply_reloaded_tracks(
    target: TrackReloadTarget,
    plan: TrackReloadPlan,
) -> tuple[TimingTrack, ...]:
    """Install a merge plan through the project track replacement contract."""
    applied: list[TimingTrack] = []
    if plan.primary_merge is not None:
        track = plan.primary_merge.track
        if not target.replace_track(0, track):
            raise IndexError("primary subtitle track is unavailable")
        applied.append(track)
    for extra_index, merge in plan.extra_merges:
        track = merge.track
        if not target.replace_track(extra_index + 1, track):
            raise IndexError(f"extra subtitle track {extra_index} is unavailable")
        applied.append(track)
    return tuple(applied)


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
                    # 重复份数未增减时，等值块的顺序对齐是唯一的，与文本全等
                    # 快速通道的按行号对齐同语义，可以信任；只有份数变化
                    # （复制/删除了一份重复行）才无法分辨对应关系，拒绝猜测。
                    old_counts[old_texts[old_start + offset]]
                    == new_counts[new_texts[new_start + offset]],
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

    # ``@Emoji`` 等源拥有的行内符号随源解析更新（target 已带上新值），只有
    # 用户在渲染器里改过/新增的符号才算本地覆盖：文本未变时原位保留，文本
    # 已变时报"无法定位"冲突而不是悄悄丢掉。
    local_inline_symbols = {
        index: symbol
        for index, symbol in current.inline_guide_symbols.items()
        if index >= len(baseline.chars)
        or baseline.inline_guide_symbols.get(index) != symbol
    }
    if local_inline_symbols:
        if exact_text:
            merged = dict(target.inline_guide_symbols)
            merged.update(
                (index, deepcopy(symbol))
                for index, symbol in local_inline_symbols.items()
            )
            target.inline_guide_symbols = merged
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


def _has_local_inline_guides(current: TimingLine, baseline: TimingLine) -> bool:
    """行内导唱符是否含渲染器本地编辑（相对源解析基线）。

    与基线完全一致的 ``@Emoji`` 符号是源拥有的状态，不算本地编辑；否则
    结构变化时这些行会被误判为"有本地设置"而拒绝自动迁移。
    """
    if not current.inline_guide_symbols:
        return False
    if len(baseline.chars) != len(current.chars):
        return True
    return any(
        index >= len(baseline.chars)
        or baseline.inline_guide_symbols.get(index) != symbol
        for index, symbol in current.inline_guide_symbols.items()
    )


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
        or _has_local_inline_guides(current, baseline)
    ):
        return True
    return any(
        old.role_label != source.role_label
        for old, source in zip(current.chars, baseline.chars)
    )
