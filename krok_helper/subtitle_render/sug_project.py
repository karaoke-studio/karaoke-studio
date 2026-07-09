"""SUG project adapter for the subtitle renderer.

The renderer uses :class:`~krok_helper.subtitle_render.models.TimingTrack` as
its source-neutral timing model.  This module maps StrangeUtaGame ``.sug``
projects directly into that model, avoiding a lossy/temporary Nicokara LRC
export step in the host workflow.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from strange_uta_game.backend.infrastructure.persistence.sug_io import SugProjectParser

from krok_helper.subtitle_render.models import (
    RubyAnnotation,
    TimingChar,
    TimingLine,
    TimingTrack,
    TimingTrackMeta,
)


def load_sug_timing_track(path: str | Path) -> TimingTrack:
    """Load a ``.sug`` file and convert it to :class:`TimingTrack`."""

    project = SugProjectParser.load(str(Path(path)))
    return timing_track_from_sug_project(project)


def timing_track_from_sug_project(project: Any) -> TimingTrack:
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

    lines: list[TimingLine] = []
    rubies: list[RubyAnnotation] = []
    sentences = list(getattr(project, "sentences", []) or [])
    for sentence_index, sentence in enumerate(sentences):
        chars = list(getattr(sentence, "characters", []) or [])
        if _is_blank_sentence(chars):
            lines.append(TimingLine(is_blank=True, singer_label=None, singer_id=None))
            continue

        line_chars: list[TimingChar] = []
        line_end_ms: int | None = None
        line_singer_id = _effective_singer_id(
            getattr(sentence, "singer_id", ""), default_singer_id
        )
        line_singer = singer_by_id.get(line_singer_id or "")
        line_singer_label = _singer_name(line_singer)
        line_singer_index = (
            singer_index_by_id.get(line_singer_id or "") if line_singer_id else None
        )

        for ch in chars:
            timestamps = _offset_timestamps(getattr(ch, "timestamps", []) or [], offset_ms)
            sentence_end_ms = _offset_optional(
                getattr(ch, "sentence_end_ts", None), offset_ms
            )
            if sentence_end_ms is not None:
                line_end_ms = sentence_end_ms
            if not timestamps:
                continue

            ch_singer_id = _effective_singer_id(
                getattr(ch, "singer_id", "") or getattr(sentence, "singer_id", ""),
                default_singer_id,
            )
            ch_singer = singer_by_id.get(ch_singer_id or "")
            ch_singer_label = _singer_name(ch_singer)
            line_chars.append(
                TimingChar(
                    text=str(getattr(ch, "char", "")),
                    start_ms=timestamps[0],
                    pause_release_ms=(
                        sentence_end_ms
                        if bool(getattr(ch, "is_sentence_end", False))
                        else None
                    ),
                    role_label=ch_singer_label,
                )
            )

        rubies.extend(
            _ruby_annotations_for_sentence(chars, offset_ms, project, sentence_index)
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
    return TimingTrack(
        meta=TimingTrackMeta(
            title=_optional_text(getattr(metadata, "title", None)),
            artist=_optional_text(getattr(metadata, "artist", None)),
            album=_optional_text(getattr(metadata, "album", None)),
            offset_ms=offset_ms,
        ),
        lines=lines,
        rubies=rubies,
    )


def _ruby_annotations_for_sentence(
    chars: list[Any], offset_ms: int, project: Any, sentence_index: int
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
        while index < len(chars) and bool(getattr(chars[index - 1], "linked_to_next", False)):
            index += 1
        end = index
        ruby = _ruby_annotation_for_group(
            chars, start, end, offset_ms, project, sentence_index
        )
        if ruby is not None:
            result.append(ruby)
    return result


def _ruby_annotation_for_group(
    chars: list[Any], start: int, end: int, offset_ms: int, project: Any, sentence_index: int
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

    reading_parts: list[str] = []
    part_offsets: list[int] = []
    for ch in group_chars:
        ruby = getattr(ch, "ruby", None)
        if ruby is None:
            continue
        timestamps = _offset_timestamps(getattr(ch, "timestamps", []) or [], offset_ms)
        for part_index, part in enumerate(list(getattr(ruby, "parts", []) or [])):
            reading_parts.append(str(getattr(part, "text", "")))
            if len(reading_parts) <= 1:
                continue
            if part_index < len(timestamps):
                part_offsets.append(max(0, timestamps[part_index] - start_ms))

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
