from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class MediaInfo:
    path: Path
    duration: float
    video_streams: int
    audio_streams: int
    subtitle_streams: int
    sample_rate: int | None = None
    channels: int | None = None
    # 主视频流的几何参数（仅当 video_streams > 0 时有值）。subtitle_render 等
    # 下游模块用来决定渲染分辨率 / 帧率默认值。
    video_width: int | None = None
    video_height: int | None = None
    video_fps: float | None = None
