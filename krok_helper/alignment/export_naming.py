"""对齐导出的文件名模板与输出目录解析。

从 ``KrokHelperQtApp`` 上搬下来的纯逻辑：只吃字符串和 ``Path``，不碰控件、
不读 ``self``，因此可以单独测。校验失败一律抛 :class:`ProcessingError`，
由 GUI 侧转成弹窗文案 —— 这是原来就有的约定，搬迁时保持不变。
"""

from __future__ import annotations

from pathlib import Path
from string import Formatter

from krok_helper.errors import ProcessingError

__all__ = [
    "ALIGNMENT_TEMPLATE_FORMATTER",
    "WINDOWS_INVALID_FILENAME_CHARS",
    "render_output_path",
    "resolve_output_dir",
    "validate_name_template",
]

ALIGNMENT_TEMPLATE_FORMATTER = Formatter()
WINDOWS_INVALID_FILENAME_CHARS = '<>:"/\\|?*'


def validate_name_template(
    template: str,
    label: str,
    *,
    allowed_fields: set[str],
    extensions: tuple[str, ...],
) -> str:
    """校验并规范化一条命名模板，返回去掉扩展名的干净模板。"""
    normalized = template.strip()
    for extension in extensions:
        if normalized.lower().endswith(extension):
            normalized = normalized[: -len(extension)].rstrip()
            break
    if not normalized:
        raise ProcessingError(f"{label}模板不能为空。")
    # 覆盖 Windows 不允许的全部字符（\ / : * ? " < > |），而非只挡路径分隔符。
    invalid_chars = sorted({char for char in normalized if char in WINDOWS_INVALID_FILENAME_CHARS})
    if invalid_chars:
        joined = " ".join(invalid_chars)
        raise ProcessingError(f"{label}模板包含非法字符: {joined}")
    # 不配对的大括号会让 parse 抛 ValueError；转成 ProcessingError，避免从只
    # 捕获 ProcessingError 的调用处逃逸导致闪退。
    try:
        fields = list(ALIGNMENT_TEMPLATE_FORMATTER.parse(normalized))
    except ValueError as exc:
        raise ProcessingError(f"{label}模板的大括号不配对，请检查占位符是否写完整。") from exc
    for _, field_name, _, _ in fields:
        if field_name and field_name not in allowed_fields:
            supported = "、".join(f"{{{name}}}" for name in sorted(allowed_fields))
            raise ProcessingError(f"{label}模板包含不支持的占位符 {field_name}。当前支持 {supported}。")
    return normalized


def resolve_output_dir(video_path: Path, *, custom_dir: str | None) -> Path:
    """``custom_dir`` 为空表示"跟随源视频目录"；给了就必须是个存在的目录。"""
    if custom_dir is not None:
        stripped = custom_dir.strip()
        if not stripped:
            raise ProcessingError("请先在波形对齐设置中选择对齐导出的保存目录。")
        output_dir = Path(stripped).expanduser()
        if not output_dir.is_dir():
            raise ProcessingError("波形对齐设置中的保存目录无效，请重新选择。")
        return output_dir
    return video_path.parent


def render_output_path(
    *,
    template: str,
    video_path: Path,
    audio_path: Path,
    extension: str,
    label: str,
    output_dir: Path,
) -> Path:
    """把模板套进文件名，拼出最终导出路径。"""
    try:
        stem = template.format(video_name=video_path.stem, audio_name=audio_path.stem).strip()
    except Exception as exc:  # noqa: BLE001
        raise ProcessingError(f"{label}模板无法生成文件名: {exc}") from exc

    stem = stem.rstrip(". ")
    if not stem:
        raise ProcessingError("导出文件名不能为空。")
    invalid_chars = sorted({char for char in stem if char in WINDOWS_INVALID_FILENAME_CHARS})
    if invalid_chars:
        raise ProcessingError(f"文件名包含非法字符: {' '.join(invalid_chars)}")
    return output_dir / f"{stem}{extension}"
