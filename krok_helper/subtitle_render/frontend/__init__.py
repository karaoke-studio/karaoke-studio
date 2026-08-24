"""字幕视频渲染模块前端子包（PyQt6 + qfluentwidgets）。"""

from pathlib import Path


SUBTITLE_RENDER_ASSET_DIR = (
    Path(__file__).resolve().parents[2] / "assets" / "subtitle_render"
)


__all__ = ["SUBTITLE_RENDER_ASSET_DIR"]
