"""渲染项目数据模型（骨架占位）。

按设计文档 D 节，最终会包含 RenderProject / Style / Background / OutputConfig
等 dataclass，并提供 JSON 序列化。MVP 阶段先留占位类型，让 import 不报错；
真正字段在 P0-A1 加载字幕源 / A8 输出 MP4 落地时填充。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional

SCHEMA_VERSION = 1
PROJECT_FILE_SUFFIX = ".krrender.json"
STYLE_PRESET_FILE_SUFFIX = ".krstyle.json"


@dataclass
class Style:
    """字幕样式占位。MVP 实装时填字段，见设计文档 D 节。"""


@dataclass
class Background:
    """背景层占位。kind 取值：solid / image / video / loop_video。"""

    kind: Literal["solid", "image", "video", "loop_video"] = "solid"
    color: str = "#000000"
    path: Optional[str] = None
    video_offset_ms: int = 0


@dataclass
class OutputConfig:
    """输出参数占位。"""

    width: int = 1920
    height: int = 1080
    fps: int = 60
    output_path: str = ""


@dataclass
class RenderProject:
    """渲染项目根对象占位。"""

    global_style: Style = field(default_factory=Style)
    background: Background = field(default_factory=Background)
    output: OutputConfig = field(default_factory=OutputConfig)
    audio_path: Optional[str] = None
    schema_version: int = SCHEMA_VERSION
