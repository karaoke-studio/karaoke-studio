"""单帧 QPainter 绘制（骨架占位）。

最终 API：``paint_frame(image, t_ms, timing, style) -> QImage``。
预览路径与渲染路径共用本函数，保证所见即所得。
"""

from __future__ import annotations


def paint_frame(*args, **kwargs):
    """占位实现。P0-A4 卡拉ok逐字高亮时填充。"""
    raise NotImplementedError("paint_frame 尚未实现")
