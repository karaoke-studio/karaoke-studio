"""Numeric option compatibility shared by LRC/SUG and N3 Emoji imports."""

import re


_INTEGRAL_DECIMAL = re.compile(r"([+-]?\d+)\.0+")


def parse_emoji_margin(value: str, default: int = 0) -> int:
    """Accept ``-80.0`` without changing the integer-pixel margin model."""
    match = _INTEGRAL_DECIMAL.fullmatch(value.strip())
    if match is not None:
        value = match.group(1)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
