"""Shared rational frame cadence for encoding and CPU/GPU frame timestamps."""

from fractions import Fraction
from functools import lru_cache
from math import ceil


@lru_cache(maxsize=64)
def frame_rate(fps: int | float) -> Fraction:
    # Recover ffprobe ratios such as 30000/1001 from their full-precision float.
    rate = Fraction(str(fps)).limit_denominator(1_000_000)
    if rate <= 0:
        raise ValueError("帧率必须大于零。")
    return rate


def frame_rate_text(fps: int | float) -> str:
    return str(frame_rate(fps))


def frame_timestamp(index: int, fps: int | float) -> int:
    return round(index * 1000 / frame_rate(fps))


def frame_count(duration_ms: int, fps: int | float) -> int:
    return max(1, ceil(duration_ms * frame_rate(fps) / 1000))
