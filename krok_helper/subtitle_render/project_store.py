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
    background: Optional[dict] = None,
    line_layout_indices: Optional[list[int]] = None,
    line_breaks_before: Optional[list[str]] = None,
    char_role_labels: Optional[list] = None,
    line_display_overrides: Optional[list] = None,
    line_animation_overrides: Optional[list] = None,
    extra_subtitle_sources: Optional[list] = None,
    project_role_names: Optional[list[str]] = None,
) -> dict:
    """组装项目快照 dict（纯数据，不碰 UI）。便于单测与复用。

    ``line_layout_indices`` 与 ``track.lines`` 对齐（含空行），记录每行引用的
    布局（0 = 默认布局）。LRC 本身不含布局信息，只能存在项目文件里。

    ``line_breaks_before`` 同样与 ``track.lines`` 对齐，保存 N3 等价算法生成或
    N3 项目显式恢复的 ``none/page/paragraph`` 分隔类型。

    ``char_role_labels`` 同样与 ``track.lines`` 对齐：每项为 None（整行无角色）
    或与该行字符对齐的角色名列表。覆盖 UI 手动分配与 N3 导入的逐字配色
    （LRC 内的 ``【N配色】`` 标签解析后也会落到这里，重复应用无害）。

    ``line_display_overrides`` 同样与 ``track.lines`` 对齐：每项为 None（该行
    无手动覆盖）或 ``[上屏覆盖毫秒或 None, 消失覆盖毫秒或 None]``（字幕轨道
    把手拖动写入的逐行显示/隐藏时间）。

    ``line_animation_overrides`` 同样与 ``track.lines`` 对齐：每项为 None（继承
    全局特效）或逐行动画覆盖字典。

    ``extra_subtitle_sources``：副字幕源列表（N3 多歌词文件，如コーラス轨），
    每项为 ``{"name", "path", "line_layout_indices", "char_role_labels",
    "line_display_overrides", "line_animation_overrides"}``。

    ``project_role_names`` 保存已导入为项目角色、但尚未分配到歌词的方案名；
    它与应用级预设库分离，避免历史预设污染当前项目的角色菜单。
    """
    payload = {
        "subtitle_path": str(subtitle_path) if subtitle_path else None,
        "video_path": str(video_path) if video_path else None,
        "audio_path": str(audio_path) if audio_path else None,
        "style": style,
        "screen": screen,
        "selected_scheme_key": selected_scheme_key,
        "output": output,
    }
    if background is not None:
        payload["background"] = dict(background)
    if line_layout_indices is not None:
        payload["line_layout_indices"] = [int(v) for v in line_layout_indices]
    if line_breaks_before is not None:
        payload["line_breaks_before"] = [
            str(value) if str(value) in {"page", "paragraph"} else "none"
            for value in line_breaks_before
        ]
    if char_role_labels is not None:
        payload["char_role_labels"] = [
            [str(label) if label else None for label in row] if isinstance(row, list) else None
            for row in char_role_labels
        ]
    if line_display_overrides is not None:
        payload["line_display_overrides"] = [
            list(row) if isinstance(row, (list, tuple)) else None
            for row in line_display_overrides
        ]
    if line_animation_overrides is not None:
        payload["line_animation_overrides"] = [
            dict(row) if isinstance(row, dict) else None
            for row in line_animation_overrides
        ]
    if extra_subtitle_sources is not None:
        payload["extra_subtitle_sources"] = [
            dict(item) for item in extra_subtitle_sources if isinstance(item, dict)
        ]
    if project_role_names is not None:
        payload["project_role_names"] = [
            str(name).strip() for name in project_role_names if str(name).strip()
        ]
    return payload


def split_project_paths(data: dict) -> dict[str, Optional[Path]]:
    """从项目快照里取出三个素材路径（清洗后转 ``Path``，空则 None）。"""
    return {
        "subtitle_path": _as_path(data.get("subtitle_path")),
        "video_path": _as_path(data.get("video_path")),
        "audio_path": _as_path(data.get("audio_path")),
    }


def background_payload(
    *,
    kind: str,
    path: Optional[Path] = None,
    color: str = "#000000",
    source_fps: Optional[int] = None,
    sequence_start_number: int = 0,
    video_offset_ms: int = 0,
) -> dict[str, object]:
    """组装可写入 ``.yurika`` 的背景源快照。"""
    return {
        "kind": kind if kind in {"video", "image", "image_sequence", "solid"} else "solid",
        "path": str(path) if path else None,
        "color": str(color or "#000000"),
        "source_fps": int(source_fps) if source_fps is not None else None,
        "sequence_start_number": int(sequence_start_number),
        "video_offset_ms": int(video_offset_ms),
    }


def _as_path(value: object) -> Optional[Path]:
    cleaned = _clean_path(value)
    return Path(cleaned) if cleaned else None


def is_project_file(path: object) -> bool:
    return isinstance(path, (str, Path)) and str(path).endswith(PROJECT_FILE_SUFFIX)


def project_output_payload(
    *,
    encoder_mode: str,
    crf: int,
    preset: str,
    output_path: str,
    native_export_enabled: bool = False,
) -> dict[str, Any]:
    return {
        "encoder_mode": encoder_mode,
        "crf": int(crf),
        "preset": preset,
        "output_path": output_path,
        "native_export_enabled": bool(native_export_enabled),
    }
