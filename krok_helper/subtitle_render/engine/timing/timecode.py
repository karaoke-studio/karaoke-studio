"""Qt-independent subtitle timecode parsing and formatting."""

from __future__ import annotations


def parse_timecode_ms(text: str) -> int | None:
    """Parse seconds, ``M:SS.mmm``, or ``H:MM:SS`` into milliseconds."""
    normalized = text.strip().replace(",", ".")
    if not normalized:
        return 0
    parts = normalized.split(":")
    if not all(parts):
        return None
    seconds_text, _, fraction = parts[-1].partition(".")
    if not seconds_text.isdigit():
        return None
    if fraction and not fraction.isdigit():
        return None
    millis = int((fraction + "000")[:3])
    total_seconds = int(seconds_text)
    scale = 60
    for part in reversed(parts[:-1]):
        total_seconds += int(part) * scale
        scale *= 60
    return total_seconds * 1000 + millis


def format_timecode_ms(value: int) -> str:
    """Format integer milliseconds as ``M:SS.mmm``."""
    minutes, remainder = divmod(int(value), 60_000)
    seconds, millis = divmod(remainder, 1000)
    return f"{minutes}:{seconds:02d}.{millis:03d}"
