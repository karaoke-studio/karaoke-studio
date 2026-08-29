"""Validation and derived values for the subtitle export job contract."""

from __future__ import annotations

import os
from pathlib import Path

from krok_helper.errors import ProcessingError
from krok_helper.subtitle_render.domain.background import BackgroundSource
from krok_helper.subtitle_render.engine.export.encoder_select import (
    CPU_PRESETS,
    ENCODER_MODES,
    VIDEO_CODECS,
)
from krok_helper.subtitle_render.engine.export.render_job import RenderJob
from krok_helper.subtitle_render.engine.timing.timeline import track_duration_ms
from krok_helper.subtitle_render.domain.timing import TimingTrack


def job_tracks(job: RenderJob) -> list[TimingTrack]:
    """Return the primary and additional tracks in compositing order."""

    return [job.track, *job.extra_tracks]


def resolved_background(job: RenderJob) -> BackgroundSource:
    """Resolve legacy video-path input into the current background contract."""

    if job.background_source is not None:
        return job.background_source
    if job.background_video_path is not None:
        return BackgroundSource(kind="video", path=str(job.background_video_path))
    return BackgroundSource(kind="solid", color="#000000")


def job_input_paths(job: RenderJob) -> list[Path]:
    """导出过程中会被读取的素材路径（背景素材 + 独立音频）。"""

    paths: list[Path] = []
    background = resolved_background(job)
    if background.kind in {"video", "image", "image_sequence"} and background.path:
        paths.append(Path(background.path))
    if job.audio_path is not None:
        paths.append(Path(job.audio_path))
    return paths


def is_same_path(left: Path, right: Path) -> bool:
    """两个路径是否指向同一个文件（Windows 大小写不敏感、可能走短名/链接）。"""

    try:
        return os.path.samefile(left, right)
    except OSError:
        # 导出前输出文件通常还不存在，samefile 拿不到 inode，退回归一化比较。
        return os.path.normcase(os.path.abspath(left)) == os.path.normcase(
            os.path.abspath(right)
        )


def ensure_output_is_not_input(job: RenderJob) -> None:
    """导出目标不许压在素材本身上。

    ffmpeg 自己会拒绝「输出即输入」并直接退出（源文件原样留着），但紧接着的
    失败清理会把这个路径当成半成品删掉 —— 删掉的其实是用户的源视频。所以在
    任何东西跑起来之前就拦下来。
    """

    output = Path(job.output_path)
    for source in job_input_paths(job):
        if is_same_path(output, source):
            raise ProcessingError(
                f"导出文件不能覆盖素材本身（{source}），"
                "请换一个导出文件名，或者换个输出文件夹。"
            )


_WINDOWS_ILLEGAL_NAME_CHARS = '<>:"/\\|?*'
_WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)
_WINDOWS_MAX_PATH = 260


def validate_output_target(output_path: Path, *, output_name: str | None = None) -> None:
    """输出侧前置校验：非法文件名 / 保留名 / 已存在同名目录 / 超长路径。

    这些情况 ffmpeg 都要等到创建输出文件那一刻才失败，用户看到的是断管或
    裸退出码；在这里提前用中文拦下。``output_name`` 传入用户原始输入
    （拼 ``.mp4`` 之前）以捕获 ``/``、``\\`` 这类会被 Path 静默并入路径的字符；
    缺省时从 ``output_path`` 反推文件名（引擎侧兜底）。
    """

    name = (output_name if output_name is not None else output_path.stem).strip()
    if not name:
        raise ProcessingError("请填写导出文件名。")
    if output_path.is_dir():
        raise ProcessingError(
            f"输出位置已存在同名文件夹，请换一个导出文件名：{output_path}"
        )
    if os.name != "nt":
        return
    illegal = sorted(
        {
            ch
            for ch in name
            if ch in _WINDOWS_ILLEGAL_NAME_CHARS or ord(ch) < 0x20
        }
    )
    if illegal:
        raise ProcessingError(
            f"导出文件名包含 Windows 不允许的字符：{' '.join(illegal)}，请换一个文件名。"
        )
    if name.split(".", 1)[0].strip().upper() in _WINDOWS_RESERVED_NAMES:
        raise ProcessingError(f"「{name}」是 Windows 保留设备名，请换一个导出文件名。")
    if len(str(output_path)) >= _WINDOWS_MAX_PATH:
        raise ProcessingError(
            f"输出路径过长（{len(str(output_path))} 字符，Windows 上限约 "
            f"{_WINDOWS_MAX_PATH}），请缩短输出文件夹层级或文件名。"
        )


def validate_render_job(job: RenderJob) -> None:
    """Reject invalid jobs before ffmpeg or a frame renderer is started."""

    if all(track.char_count <= 0 for track in job_tracks(job)):
        raise ProcessingError("请先加载有效的字幕文件。")
    background = resolved_background(job)
    if background.kind == "video" and job.audio_path is not None:
        raise ProcessingError("视频背景不支持独立音频，请使用视频内嵌音轨。")
    if background.kind in {"video", "image", "image_sequence"}:
        if not background.path:
            raise ProcessingError("请先选择背景素材。")
        path = Path(background.path)
        if background.kind != "image_sequence" and not path.is_file():
            raise ProcessingError(f"背景素材不存在: {path}")
        if background.kind == "image_sequence" and not (
            path.exists() or path.parent.exists()
        ):
            raise ProcessingError(f"背景图片序列不存在: {path}")
    if job.audio_path is not None and not job.audio_path.is_file():
        raise ProcessingError(f"独立音频不存在: {job.audio_path}")
    if job.width <= 0 or job.height <= 0:
        raise ProcessingError("输出分辨率无效。")
    if job.width % 2 != 0 or job.height % 2 != 0:
        raise ProcessingError(
            f"输出宽度和高度必须是偶数（当前 {job.width}×{job.height}）："
            "H.264/H.265 编码的 yuv420p 像素格式不支持奇数尺寸。"
        )
    if job.fps <= 0:
        raise ProcessingError("输出 fps 无效。")
    if job.encoder_mode not in ENCODER_MODES:
        raise ProcessingError(f"不支持的编码器: {job.encoder_mode}")
    if job.codec not in VIDEO_CODECS:
        raise ProcessingError(f"不支持的视频编码: {job.codec}")
    if not 0 <= job.crf <= 51:
        raise ProcessingError("CRF 必须在 0 到 51 之间。")
    if job.preset not in CPU_PRESETS:
        raise ProcessingError(f"不支持的 CPU preset: {job.preset}")
    if not str(job.output_path).strip():
        raise ProcessingError("请先选择输出路径。")
    validate_output_target(job.output_path)
    ensure_output_is_not_input(job)


def resolve_duration_ms(job: RenderJob) -> int:
    """Resolve the explicit or track-derived export duration."""

    if job.duration_ms is not None and job.duration_ms > 0:
        return job.duration_ms
    duration = max(track_duration_ms(track) for track in job_tracks(job))
    if duration <= 0:
        raise ProcessingError("字幕时长无效，无法导出。")
    return duration
