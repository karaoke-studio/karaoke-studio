"""渲染项目数据模型（骨架占位）。

按设计文档 D 节，最终会包含 RenderProject / SubtitleSource / Style / Background
/ OutputConfig 等 dataclass，并提供 JSON 序列化。MVP 阶段先留占位类型，让 import
不报错；真正字段在 P0-A1 加载字幕源 / A8 输出 MP4 落地时填充。

**字幕源格式**：唯一支持 Nicokara 逐字 LRC（``.lrc``，SUG ``NicokaraExporter``
产物，含 ``@Ruby`` / ``@Offset`` / ``@Title`` / ``@Artist`` / 演唱者标签）。
不支持 ``.ass`` / ``.sug`` / ``.nkm``——SUG 已能把这些信息以 Nicokara LRC 格式
导出，模块只需要写一个 Nicokara LRC 解析器即可。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional

SCHEMA_VERSION = 1
PROJECT_FILE_SUFFIX = ".krrender.json"
STYLE_PRESET_FILE_SUFFIX = ".krstyle.json"
SUBTITLE_SOURCE_SUFFIX = ".lrc"


@dataclass
class SubtitleSource:
    """字幕源占位（Nicokara 逐字 LRC，唯一格式）。"""

    path: str = ""
    singer_filter: Optional[list[int]] = None


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

    subtitle_source: SubtitleSource = field(default_factory=SubtitleSource)
    global_style: Style = field(default_factory=Style)
    background: Background = field(default_factory=Background)
    output: OutputConfig = field(default_factory=OutputConfig)
    audio_path: Optional[str] = None
    schema_version: int = SCHEMA_VERSION
