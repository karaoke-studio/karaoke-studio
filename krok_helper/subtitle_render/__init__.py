"""字幕视频渲染模块（工作流第 5 步）。

对标 NicoKaraMaker3 等效功能：把带逐字时间戳的歌词渲染成卡拉ok高亮动画字幕视频。

双模式：
- standalone：``python -m krok_helper.subtitle_render`` 单独跑
- embedded：通过 :meth:`SubtitleRenderWindow.for_embedding` 嵌入工作台第 5 步

设计文档：``C:/Users/18007/.claude/plans/ok-ok-main-bug-merge-merge-main-nicokar-toasty-blum.md``。
"""

from krok_helper.subtitle_render.frontend.main_window import SubtitleRenderWindow

__all__ = ["SubtitleRenderWindow"]
