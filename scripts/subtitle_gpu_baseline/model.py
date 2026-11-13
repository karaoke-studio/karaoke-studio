"""Explicit inputs and sampling policies shared by capture and future comparisons."""
from __future__ import annotations

from dataclasses import dataclass
from krok_helper.subtitle_render.domain.models import Style, TimingTrack


@dataclass(frozen=True)
class BaselineCase:
    case_id: str
    features: tuple[str, ...]
    track: TimingTrack
    style: Style
    timestamps_ms: tuple[int, ...]
    extra_tracks: tuple[TimingTrack, ...] = ()
    source: str = "synthetic"
    # Whether at least one sampled frame must contain visible pixels. Empty
    # boundary frames are legitimate; an entirely blank positive case is not.
    require_visible: bool = True
    scope: str = "subtitle_overlay"


def sequence_times(*, start_ms: int, duration_ms: int, fps: int) -> tuple[int, ...]:
    if start_ms < 0 or duration_ms <= 0 or fps not in (60, 120):
        raise ValueError("时间窗口必须有效，帧率仅支持 60/120。")
    count = max(1, (duration_ms * fps + 999) // 1000)
    return tuple(start_ms + round(i * 1000 / fps) for i in range(count))


def seek_order(timestamps: tuple[int, ...], *, seed: int = 20261114) -> tuple[int, ...]:
    import random
    values = list(dict.fromkeys(timestamps))
    random.Random(seed).shuffle(values)
    return tuple(values)
