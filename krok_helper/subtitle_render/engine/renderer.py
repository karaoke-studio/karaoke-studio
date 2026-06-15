"""ffmpeg rawvideo pipe 主循环（骨架占位）。

流程（见设计文档 E 节）：
1. 枚举每帧时间 t
2. QPainter → QImage(Format_RGBA8888) → bytes
3. ``subprocess.Popen`` 写入 ffmpeg stdin（``-f rawvideo -pix_fmt rgba``）
4. ffmpeg 同时读 ``-i background`` 和 ``-i audio`` 做合成与编码

复用 ``krok_helper/ffmpeg.py`` 的 ``run_command`` / ``find_tool`` / ``probe_media``。
"""

from __future__ import annotations
