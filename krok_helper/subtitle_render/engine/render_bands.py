"""Pure geometry helpers for packed subtitle render bands."""

from __future__ import annotations


def merge_intervals(
    intervals: list[tuple[int, int]], gap: int
) -> list[tuple[int, int]]:
    """Merge sorted vertical intervals separated by at most ``gap`` pixels."""

    if not intervals:
        return []
    ordered = sorted(intervals)
    merged = [ordered[0]]
    for top, bottom in ordered[1:]:
        last_top, last_bottom = merged[-1]
        if top - last_bottom <= gap:
            merged[-1] = (last_top, max(last_bottom, bottom))
        else:
            merged.append((top, bottom))
    return merged


def packed_offsets(bands: list[tuple[int, int]]) -> list[int]:
    """Return each band's vertical origin in the packed frame buffer."""

    offsets: list[int] = []
    cursor = 0
    for _top, height in bands:
        offsets.append(cursor)
        cursor += height
    return offsets
