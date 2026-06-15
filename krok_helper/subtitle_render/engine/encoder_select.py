"""视频编码器选择（骨架占位）。

最终复用 ``krok_helper/audio_alignment.py::export_aligned_video_v2`` 的硬编探测逻辑
（auto / nvenc / qsv / amf / cpu）。在 P0-A8 输出 MP4 时落地。
"""

from __future__ import annotations
