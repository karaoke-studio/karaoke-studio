"""六步工作流的模块 ID。

页面包需要知道"自己是哪一步"（比如快捷键只在本步生效），但不能反向 import
``gui_qt``，所以 ID 落在这层。步条控件与步骤文案仍在 ``gui_qt``。
"""

from __future__ import annotations

__all__ = [
    "WORKFLOW_HIRES_MIX",
    "WORKFLOW_LYRICS_SEARCH",
    "WORKFLOW_LYRICS_TIMING",
    "WORKFLOW_SUBTITLE_RENDER",
    "WORKFLOW_VIDEO_DOWNLOAD",
    "WORKFLOW_WAVEFORM_ALIGN",
]

WORKFLOW_VIDEO_DOWNLOAD = "video_download"
WORKFLOW_WAVEFORM_ALIGN = "waveform_align"
WORKFLOW_LYRICS_SEARCH = "lyrics_search"
WORKFLOW_LYRICS_TIMING = "lyrics_timing"
WORKFLOW_SUBTITLE_RENDER = "subtitle_render"
WORKFLOW_HIRES_MIX = "hires_mix"
