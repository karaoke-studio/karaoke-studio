"""``.yurika`` 项目文件读写（A11，standalone 专用）。

项目文件是一份带 ``schema_version`` 的 JSON 快照，存放当前 standalone 会话的
全部可复现状态：字幕 / 背景视频 / 音频路径、全局样式、屏幕设置、配色方案选择、
导出参数。嵌入模式不用项目文件（由工作流上下文管理）。

序列化沿用字段驱动的 :func:`style_to_dict` 等——以后 ``Style`` 加字段，项目文件
自动跟着长，且旧文件用新代码打开会缺字段取默认、新文件用旧代码打开会忽略未知
key（前后兼容）。

路径目前按**绝对路径**存；移动项目文件到别处后素材链接会失效（后续可加
相对路径便携支持）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from krok_helper.subtitle_render.models import PROJECT_FILE_SUFFIX

PROJECT_SCHEMA_VERSION = 1


def save_render_project(path: Path, data: dict) -> None:
    """把项目快照 ``data`` 写入 ``path``（覆盖）。自动补 ``schema_version``。"""
    payload = {"schema_version": PROJECT_SCHEMA_VERSION}
    payload.update(data)
    path = Path(path)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_render_project(path: Path) -> dict:
    """读取并解析 ``.yurika``，返回项目快照 dict。

    解析失败（非法 JSON / 非 dict）抛 :class:`ValueError`，由调用方弹错处理。
    """
    raw = Path(path).read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"项目文件不是合法 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("项目文件内容不是对象")
    return data


def _clean_path(value: object) -> Optional[str]:
    return str(value) if isinstance(value, str) and value.strip() else None


def project_payload(
    *,
    subtitle_path: Optional[Path],
    video_path: Optional[Path],
    audio_path: Optional[Path],
    style: dict,
    screen: dict,
    selected_scheme_key: str,
    output: dict,
) -> dict:
    """组装项目快照 dict（纯数据，不碰 UI）。便于单测与复用。"""
    return {
        "subtitle_path": str(subtitle_path) if subtitle_path else None,
        "video_path": str(video_path) if video_path else None,
        "audio_path": str(audio_path) if audio_path else None,
        "style": style,
        "screen": screen,
        "selected_scheme_key": selected_scheme_key,
        "output": output,
    }


def split_project_paths(data: dict) -> dict[str, Optional[Path]]:
    """从项目快照里取出三个素材路径（清洗后转 ``Path``，空则 None）。"""
    return {
        "subtitle_path": _as_path(data.get("subtitle_path")),
        "video_path": _as_path(data.get("video_path")),
        "audio_path": _as_path(data.get("audio_path")),
    }


def _as_path(value: object) -> Optional[Path]:
    cleaned = _clean_path(value)
    return Path(cleaned) if cleaned else None


def is_project_file(path: object) -> bool:
    return isinstance(path, (str, Path)) and str(path).endswith(PROJECT_FILE_SUFFIX)


def project_output_payload(
    *, encoder_mode: str, crf: int, preset: str, output_path: str
) -> dict[str, Any]:
    return {
        "encoder_mode": encoder_mode,
        "crf": int(crf),
        "preset": preset,
        "output_path": output_path,
    }
