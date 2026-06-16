from __future__ import annotations

from collections.abc import Callable

from krok_helper import ensure_sug_src_path


_ORIGINAL_UTATEN_SPLIT = "_krok_helper_original_legacy_utaten_split"
_ORIGINAL_REFERENCE_SPLIT = "_krok_helper_original_split_reading_by_reference"


def _kata_to_hira(text: str) -> str:
    chars: list[str] = []
    for ch in text:
        code = ord(ch)
        if 0x30A1 <= code <= 0x30F6:
            chars.append(chr(code - 0x60))
        else:
            chars.append(ch)
    return "".join(chars)


def _restore_source_kana_script(source_reading: str, split_parts: list[str]) -> list[str]:
    if not source_reading or not split_parts:
        return split_parts

    normalized_source = _kata_to_hira(source_reading)
    normalized_parts = [_kata_to_hira(part) for part in split_parts]
    if "".join(normalized_parts) != normalized_source:
        return split_parts

    restored: list[str] = []
    offset = 0
    for normalized_part, fallback_part in zip(normalized_parts, split_parts):
        next_offset = offset + len(normalized_part)
        source_part = source_reading[offset:next_offset]
        if _kata_to_hira(source_part) == normalized_part:
            restored.append(source_part)
        else:
            restored.append(fallback_part)
        offset = next_offset

    if offset != len(source_reading):
        return split_parts
    return restored


def apply_sug_compat_patches() -> None:
    """Apply host-side compatibility patches for the embedded SUG runtime."""

    ensure_sug_src_path()
    from strange_uta_game.frontend.editor.timing import lyric_loader

    original_utaten_split: Callable[[str, str], tuple[list[str], bool]] | None = getattr(
        lyric_loader,
        _ORIGINAL_UTATEN_SPLIT,
        None,
    )
    if original_utaten_split is None:
        original_utaten_split = lyric_loader._legacy_utaten_split
        setattr(lyric_loader, _ORIGINAL_UTATEN_SPLIT, original_utaten_split)

    def _legacy_utaten_split_preserving_source_kana(
        word: str,
        reading: str,
    ) -> tuple[list[str], bool]:
        parts, is_ateji = original_utaten_split(word, reading)
        return _restore_source_kana_script(reading, parts), is_ateji

    lyric_loader._legacy_utaten_split = _legacy_utaten_split_preserving_source_kana

    original_reference_split: Callable[[str, list[str]], list[str] | None] | None = getattr(
        lyric_loader,
        _ORIGINAL_REFERENCE_SPLIT,
        None,
    )
    if original_reference_split is None:
        original_reference_split = lyric_loader._split_reading_by_reference
        setattr(lyric_loader, _ORIGINAL_REFERENCE_SPLIT, original_reference_split)

    def _split_reading_by_reference_preserving_source_kana(
        reading: str,
        tokens: list[str],
    ) -> list[str] | None:
        split = original_reference_split(reading, tokens)
        if split is not None:
            return split
        if not tokens:
            return None
        normalized_tokens = [_kata_to_hira(token) for token in tokens]
        if "".join(normalized_tokens) != _kata_to_hira(reading):
            return None
        return _restore_source_kana_script(reading, normalized_tokens)

    lyric_loader._split_reading_by_reference = _split_reading_by_reference_preserving_source_kana
