"""字幕视频渲染引擎子包。

技术选型：QPainter 离屏绘制 + ffmpeg rawvideo pipe（见设计文档 E 节）。
骨架阶段各模块仅占位，后续按 P0 优先级实装：

- painter：QPainter 单帧绘制
- animator：入场/退场关键帧插值（P1）
- timeline：时间 → 活跃行/字索引
- renderer：ffmpeg pipe 主循环 + 多线程帧池 + cancel
- encoder_select：硬编探测（复用 ``audio_alignment.py`` 逻辑）
"""
