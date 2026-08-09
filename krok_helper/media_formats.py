"""各页共用的素材格式白名单与时长文案。

原本长在 ``gui_qt`` 里，页面包搬出去之后不能反向 import 它（循环依赖），
所以落到这层。
"""

from __future__ import annotations

__all__ = [
    "ALIGN_AUDIO_EXTENSIONS",
    "AUDIO_EXTENSIONS",
    "HIRES_AUDIO_EXTENSIONS",
    "VIDEO_EXTENSIONS",
    "format_media_duration",
]

VIDEO_EXTENSIONS = {".mkv", ".mp4", ".mov", ".avi"}
AUDIO_EXTENSIONS = {".flac", ".wav", ".mp3", ".m4a", ".aac", ".ape", ".alac", ".mkv"}
#: Hi-Res 与波形对齐都额外收 mp4：用户常直接拖 MV 当音源。
HIRES_AUDIO_EXTENSIONS = AUDIO_EXTENSIONS | {".mp4"}
ALIGN_AUDIO_EXTENSIONS = AUDIO_EXTENSIONS | {".mp4"}


def format_media_duration(seconds: float | None) -> str:
    if seconds is None or seconds <= 0:
        return "时长未知"

    minutes = int(seconds // 60)
    remainder = seconds - minutes * 60
    if minutes:
        return f"{minutes}:{remainder:06.3f}"
    return f"{seconds:.3f}s"
