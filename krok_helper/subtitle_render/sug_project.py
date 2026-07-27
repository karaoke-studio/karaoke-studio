"""SUG project adapter for the subtitle renderer.

The renderer uses :class:`~krok_helper.subtitle_render.models.TimingTrack` as
its source-neutral timing model.  This module maps StrangeUtaGame ``.sug``
projects directly into that model, avoiding a lossy/temporary Nicokara LRC
export step in the host workflow.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from strange_uta_game.backend.domain.models import (
    RUBY_PAUSE_SENTINEL,
    get_ruby_pause_char,
    pause_char_variants,
)
from strange_uta_game.backend.infrastructure.persistence.sug_io import SugProjectParser

from krok_helper.subtitle_render.models import (
    RubyAnnotation,
    TimingChar,
    TimingLine,
    TimingTrack,
    TimingTrackMeta,
)

_DEFAULT_PLACEHOLDER_SINGER_NAMES = {"未命名", "Untitled"}


def load_sug_timing_track(path: str | Path) -> TimingTrack:
    """Load a ``.sug`` file and convert it to :class:`TimingTrack`."""

    source_path = Path(path)
    project = SugProjectParser.load(str(source_path))
    extras = SugProjectParser.load_extras(str(source_path))
    tags = extras.get("nicokara_tags") if isinstance(extras, dict) else None
    return timing_track_from_sug_project(project, nicokara_tags=tags)


def timing_track_from_sug_project(
    project: Any,
    *,
    nicokara_tags: Mapping[str, Any] | None = None,
) -> TimingTrack:
    """Convert a StrangeUtaGame ``Project`` object to ``TimingTrack``.

    The conversion is intentionally semantic rather than text-based:

    - SUG ``Character.timestamps`` become ``TimingChar.start_ms``.
    - SUG ``sentence_end_ts`` becomes line end / pause-release timing.
    - SUG ``Ruby.parts`` become ``RubyAnnotation.reading_parts``.
    - SUG singers become both line singer labels and per-char role labels so
      the existing role styling path can address them.
    """

    offset_ms = int(getattr(project, "global_offset_ms", 0) or 0)
    singers = list(getattr(project, "singers", []) or [])
    singer_by_id = {str(getattr(singer, "id")): singer for singer in singers}
    singer_index_by_id = {
        str(getattr(singer, "id")): index for index, singer in enumerate(singers)
    }
    default_singer_id = _default_singer_id(singers)
    ruby_pause_texts = _ruby_pause_texts()

    lines: list[TimingLine] = []
    rubies: list[RubyAnnotation] = []
    sentences = list(getattr(project, "sentences", []) or [])
    for sentence_index, sentence in enumerate(sentences):
        chars = list(getattr(sentence, "characters", []) or [])
        if _is_blank_sentence(chars):
            lines.append(TimingLine(is_blank=True, singer_label=None, singer_id=None))
            continue

        line_chars: list[TimingChar] = []
        line_singer_id = _effective_singer_id(
            getattr(sentence, "singer_id", ""), default_singer_id
        )
        line_singer = singer_by_id.get(line_singer_id or "")
        line_singer_label = _singer_name(line_singer)
        line_singer_index = (
            singer_index_by_id.get(line_singer_id or "")
            if line_singer_id and line_singer_label is not None
            else None
        )

        line_chars = _timing_chars_for_sentence(
            chars=chars,
            offset_ms=offset_ms,
            project=project,
            sentence_index=sentence_index,
            sentence_singer_id=getattr(sentence, "singer_id", ""),
            default_singer_id=default_singer_id,
            singer_by_id=singer_by_id,
        )
        line_end_ms = _group_end_ms(
            chars, len(chars), offset_ms, project, sentence_index
        )

        rubies.extend(
            _ruby_annotations_for_sentence(
                chars,
                offset_ms,
                project,
                sentence_index,
                ruby_pause_texts,
            )
        )
        lines.append(
            TimingLine(
                chars=line_chars,
                end_ms=line_end_ms,
                singer_label=line_singer_label,
                singer_id=line_singer_index,
                is_blank=not line_chars,
            )
        )

    metadata = getattr(project, "metadata", None)
    tags = nicokara_tags if isinstance(nicokara_tags, Mapping) else {}
    return TimingTrack(
        meta=TimingTrackMeta(
            # Nicokara 标签是导出时的显式元数据；有值时优先于项目属性。
            # 这也覆盖“从 LRC 导入后标签已解析、但 ProjectMetadata 仍为空”
            # 的常见路径。标签为空时则保留 SUG 项目自己的元数据。
            title=_tag_text(tags, "title")
            or _optional_text(getattr(metadata, "title", None)),
            artist=_tag_text(tags, "artist")
            or _optional_text(getattr(metadata, "artist", None)),
            album=_tag_text(tags, "album")
            or _optional_text(getattr(metadata, "album", None)),
            tagging_by=_tag_text(tags, "tagging_by"),
            silence_ms=_tag_int(tags, "silence_ms"),
            # All SUG checkpoints, line ends and ruby positions were already
            # normalized with ``global_offset_ms`` while building this track.
            offset_ms=0,
            custom=_custom_tag_lines(tags.get("custom")),
        ),
        lines=lines,
        rubies=rubies,
    )


def _tag_text(tags: Mapping[str, Any], key: str) -> str | None:
    return _optional_text(tags.get(key))


def _tag_int(tags: Mapping[str, Any], key: str) -> int:
    try:
        return int(tags.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def _custom_tag_lines(value: object) -> list[str]:
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, (list, tuple)):
        values = value
    else:
        return []
    return [text for item in values if (text := str(item).strip())]


def _timing_chars_for_sentence(
    *,
    chars: list[Any],
    offset_ms: int,
    project: Any,
    sentence_index: int,
    sentence_singer_id: object,
    default_singer_id: str | None,
    singer_by_id: dict[str, Any],
) -> list[TimingChar]:
    timed_indices = [
        index
        for index, ch in enumerate(chars)
        if _offset_timestamps(getattr(ch, "timestamps", []) or [], offset_ms)
    ]
    if not timed_indices:
        return []

    result: list[TimingChar] = []
    for timed_index_position, timed_index in enumerate(timed_indices):
        group_start = 0 if timed_index_position == 0 else timed_index
        group_end = (
            timed_indices[timed_index_position + 1]
            if timed_index_position + 1 < len(timed_indices)
            else len(chars)
        )
        anchor_start_ms = _first_timestamp(chars[timed_index], offset_ms)
        if anchor_start_ms is None:
            continue
        has_following_anchor = timed_index_position + 1 < len(timed_indices)

        cursor = group_start
        span_start_ms = anchor_start_ms
        while cursor < group_end:
            span_end_index = group_end
            span_end_ms = _group_end_ms(
                chars, group_end, offset_ms, project, sentence_index
            )
            ended_by_sentence = False
            for boundary_index in range(cursor, group_end):
                boundary_ch = chars[boundary_index]
                boundary_end_ms = _offset_optional(
                    getattr(boundary_ch, "sentence_end_ts", None), offset_ms
                )
                if (
                    bool(getattr(boundary_ch, "is_sentence_end", False))
                    and boundary_end_ms is not None
                ):
                    span_end_index = boundary_index + 1
                    span_end_ms = boundary_end_ms
                    ended_by_sentence = True
                    break

            group_items = [
                (index, ch, text)
                for index, ch in enumerate(chars[cursor:span_end_index], start=cursor)
                if (text := str(getattr(ch, "char", "")))
            ]
            if not group_items:
                cursor = span_end_index
                if ended_by_sentence and span_end_ms is not None:
                    span_start_ms = span_end_ms
                    continue
                break

            starts = _spread_text_starts(span_start_ms, span_end_ms, len(group_items))
            shared_span = (
                len(group_items) > 1
                and span_end_ms is not None
                and span_end_ms > span_start_ms
            )

            for local_index, (_index, ch, text) in enumerate(group_items):
                sentence_end_ms = _offset_optional(
                    getattr(ch, "sentence_end_ts", None), offset_ms
                )
                ch_singer_id = _effective_singer_id(
                    getattr(ch, "singer_id", "") or sentence_singer_id,
                    default_singer_id,
                )
                ch_singer = singer_by_id.get(ch_singer_id or "")
                result.append(
                    TimingChar(
                        text=text,
                        start_ms=starts[local_index],
                        explicit_start=bool(
                            _offset_timestamps(
                                getattr(ch, "timestamps", []) or [], offset_ms
                            )
                        )
                        or (local_index == 0 and cursor > group_start),
                        explicit_end=(
                            bool(getattr(ch, "is_sentence_end", False))
                            and sentence_end_ms is not None
                        )
                        or (local_index == len(group_items) - 1 and has_following_anchor),
                        pause_release_ms=(
                            sentence_end_ms
                            if bool(getattr(ch, "is_sentence_end", False))
                            else None
                        ),
                        role_label=_singer_name(ch_singer),
                        source_span_start_ms=span_start_ms if shared_span else None,
                        source_span_end_ms=span_end_ms if shared_span else None,
                        source_span_index=local_index if shared_span else 0,
                        source_span_count=len(group_items) if shared_span else 1,
                    )
                )

            cursor = span_end_index
            if ended_by_sentence and span_end_ms is not None:
                span_start_ms = span_end_ms
                continue
            break
    return result


def _ruby_annotations_for_sentence(
    chars: list[Any],
    offset_ms: int,
    project: Any,
    sentence_index: int,
    ruby_pause_texts: tuple[str, ...],
) -> list[RubyAnnotation]:
    result: list[RubyAnnotation] = []
    index = 0
    while index < len(chars):
        ch = chars[index]
        if getattr(ch, "ruby", None) is None:
            index += 1
            continue
        start = index
        index += 1
        while index < len(chars) and bool(
            getattr(chars[index - 1], "linked_to_next", False)
        ):
            index += 1
        end = index
        ruby = _ruby_annotation_for_group(
            chars,
            start,
            end,
            offset_ms,
            project,
            sentence_index,
            ruby_pause_texts,
        )
        if ruby is not None:
            result.append(ruby)
    return result


def _ruby_annotation_for_group(
    chars: list[Any],
    start: int,
    end: int,
    offset_ms: int,
    project: Any,
    sentence_index: int,
    ruby_pause_texts: tuple[str, ...],
) -> RubyAnnotation | None:
    group_chars = chars[start:end]
    start_ms = _first_timestamp(group_chars[0], offset_ms)
    if start_ms is None:
        start_ms = _nearest_previous_timestamp(chars, start, offset_ms)
    if start_ms is None:
        return None

    end_ms = _group_end_ms(chars, end, offset_ms, project, sentence_index)
    if end_ms is None:
        end_ms = start_ms

    reading_parts, part_offsets = _ruby_reading_parts_for_group(
        group_chars,
        start_ms,
        offset_ms,
        ruby_pause_texts,
    )
    reading = "".join(reading_parts)
    if not reading and reading_parts:
        reading_parts = [" " for _part in reading_parts]
        reading = "".join(reading_parts)
    if not reading:
        return None
    return RubyAnnotation(
        kanji="".join(str(getattr(ch, "char", "")) for ch in group_chars),
        reading=reading,
        reading_part_ms=part_offsets,
        pos_start_ms=start_ms,
        pos_end_ms=end_ms,
        reading_parts=reading_parts,
    )


def _ruby_reading_parts_for_group(
    group_chars: list[Any],
    start_ms: int,
    offset_ms: int,
    ruby_pause_texts: tuple[str, ...],
) -> tuple[list[str], list[int]]:
    reading_parts: list[str] = []
    current_part: list[str] = []
    part_offsets: list[int] = []
    has_part = False
    for ch in group_chars:
        ruby = getattr(ch, "ruby", None)
        if ruby is None:
            continue
        timestamps = _offset_timestamps(getattr(ch, "timestamps", []) or [], offset_ms)
        for part_index, part in enumerate(list(getattr(ruby, "parts", []) or [])):
            part_text = str(getattr(part, "text", ""))
            for pause_text in ruby_pause_texts:
                part_text = part_text.replace(pause_text, "")
            if part_index < len(timestamps):
                timestamp = max(0, timestamps[part_index] - start_ms)
                if has_part:
                    reading_parts.append("".join(current_part))
                    current_part = []
                    part_offsets.append(timestamp)
            current_part.append(part_text)
            has_part = True
    if has_part:
        reading_parts.append("".join(current_part))
    return reading_parts, part_offsets


def _ruby_pause_texts() -> tuple[str, ...]:
    pause_char = get_ruby_pause_char()
    values = pause_char_variants(pause_char) | {RUBY_PAUSE_SENTINEL}
    return tuple(sorted((value for value in values if value), key=len, reverse=True))


def _group_end_ms(
    chars: list[Any], end: int, offset_ms: int, project: Any, sentence_index: int
) -> int | None:
    if end > 0:
        last = chars[end - 1]
        if bool(getattr(last, "is_sentence_end", False)):
            sentence_end = _offset_optional(getattr(last, "sentence_end_ts", None), offset_ms)
            if sentence_end is not None:
                return sentence_end
    for ch in chars[end:]:
        timestamp = _first_timestamp(ch, offset_ms)
        if timestamp is not None:
            return timestamp
    for sentence in list(getattr(project, "sentences", []) or [])[sentence_index + 1 :]:
        sentence_chars = list(getattr(sentence, "characters", []) or [])
        for ch in sentence_chars:
            timestamp = _first_timestamp(ch, offset_ms)
            if timestamp is not None:
                return timestamp
    return None


def _nearest_previous_timestamp(
    chars: list[Any], start: int, offset_ms: int
) -> int | None:
    for ch in reversed(chars[:start]):
        sentence_end = _offset_optional(getattr(ch, "sentence_end_ts", None), offset_ms)
        if sentence_end is not None:
            return sentence_end
        timestamps = _offset_timestamps(getattr(ch, "timestamps", []) or [], offset_ms)
        if timestamps:
            return timestamps[-1]
    return None


def _default_singer_id(singers: list[Any]) -> str | None:
    for singer in singers:
        if bool(getattr(singer, "is_default", False)):
            return str(getattr(singer, "id"))
    if singers:
        return str(getattr(singers[0], "id"))
    return None


def _effective_singer_id(value: object, default_singer_id: str | None) -> str | None:
    text = str(value or "").strip()
    if text and text not in {"?", "未知"}:
        return text
    return default_singer_id


def _singer_name(singer: Any | None) -> str | None:
    if singer is None:
        return None
    name = str(getattr(singer, "name", "") or "").strip()
    if bool(getattr(singer, "is_default", False)) and (
        bool(getattr(singer, "is_placeholder", False))
        or name in _DEFAULT_PLACEHOLDER_SINGER_NAMES
    ):
        return None
    return name or None


def _first_timestamp(ch: Any, offset_ms: int) -> int | None:
    timestamps = _offset_timestamps(getattr(ch, "timestamps", []) or [], offset_ms)
    return timestamps[0] if timestamps else None


def _offset_timestamps(values: list[Any], offset_ms: int) -> list[int]:
    return [max(0, int(value) + offset_ms) for value in values]


def _offset_optional(value: Any, offset_ms: int) -> int | None:
    if value is None:
        return None
    return max(0, int(value) + offset_ms)


def _spread_text_starts(
    start_ms: int,
    next_ts_ms: int | None,
    char_count: int,
) -> list[int]:
    if char_count <= 0:
        return []
    if char_count == 1 or next_ts_ms is None or next_ts_ms <= start_ms:
        return [start_ms] * char_count
    duration = next_ts_ms - start_ms
    return [start_ms + (duration * index) // char_count for index in range(char_count)]


def _is_blank_sentence(chars: list[Any]) -> bool:
    if not chars:
        return True
    return all(
        not str(getattr(ch, "char", "")).strip()
        and not (getattr(ch, "timestamps", []) or [])
        and getattr(ch, "sentence_end_ts", None) is None
        for ch in chars
    )


def _optional_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None
