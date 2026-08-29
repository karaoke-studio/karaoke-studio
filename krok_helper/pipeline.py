from __future__ import annotations

import subprocess
from pathlib import Path
from string import Formatter
from tempfile import TemporaryDirectory
from typing import Callable, Sequence

from krok_helper.config import DURATION_WARNING_SECONDS, MIN_HIRES_SAMPLE_RATE
from krok_helper.errors import ExportCancelled, ProcessingError
from krok_helper.ffmpeg import describe_tool_source, find_tool, probe_media, run_command
from krok_helper.models import MediaInfo
from krok_helper.types import Logger


DEFAULT_AUDIO_TITLE_TEMPLATE = "Hi-Res Audio (FLAC 32bit/{sample_rate}Hz)"
OUTPUT_NAME_MODE_FIXED = "fixed"
OUTPUT_NAME_MODE_TEMPLATE = "template"
OUTPUT_NAME_MODE_VIDEO_NAME = "video_name"
DEFAULT_ON_NAME_TEMPLATE = "{video_name}_on"
DEFAULT_OFF_NAME_TEMPLATE = "{video_name}_off"
#: 模板可用的占位符。``audio_name`` 是这一条音频自己的文件名（不含扩展名），
#: 放多条伴奏时用它区分各自的输出。
SUPPORTED_TEMPLATE_FIELDS = {"video_name", "audio_name"}
WINDOWS_INVALID_FILENAME_CHARS = '<>:"/\\|?*'
FORMATTER = Formatter()


def format_duration(seconds: float) -> str:
    seconds = max(0, seconds)
    whole = int(seconds)
    milliseconds = int(round((seconds - whole) * 1000))
    minutes, sec = divmod(whole, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{sec:02d}.{milliseconds:03d}"
    return f"{minutes:02d}:{sec:02d}.{milliseconds:03d}"


def log_media_summary(logger: Logger, label: str, info: MediaInfo) -> None:
    parts = [
        f"{label}: {info.path.name}",
        f"时长 {format_duration(info.duration)}",
        f"视频流 {info.video_streams}",
        f"音频流 {info.audio_streams}",
        f"字幕流 {info.subtitle_streams}",
    ]
    if info.sample_rate:
        parts.append(f"采样率 {info.sample_rate}Hz")
    if info.channels:
        parts.append(f"声道 {info.channels}")
    logger(" | ".join(parts))


def warn_duration_mismatch(
    logger: Logger,
    video_info: MediaInfo,
    audio_info: MediaInfo,
    label: str,
) -> None:
    delta = abs(video_info.duration - audio_info.duration)
    if delta > DURATION_WARNING_SECONDS:
        logger(
            f"警告: {label} 与字幕视频的时长相差 {delta:.2f} 秒，"
            "程序会继续处理，但建议你确认素材是否对齐。"
        )


def log_audio_format_mismatch(
    logger: Logger,
    on_vocal_info: MediaInfo | None,
    off_vocal_info: MediaInfo | None,
) -> None:
    if on_vocal_info is None or off_vocal_info is None:
        return

    if on_vocal_info.path.suffix.lower() == off_vocal_info.path.suffix.lower():
        return

    logger("检测到原唱和伴奏的文件格式不一致，将先分别标准化为临时 FLAC，再进行封装。")


def validate_output_name_template(
    template: str,
    label: str,
    allowed_fields: set[str] | None = None,
) -> str:
    """校验一条输出命名模板并返回规范化后的字符串。

    ``allowed_fields`` 让不同模块用各自的占位符词表（Hi-Res 用 video_name /
    audio_name，字幕渲染用素材名等），校验规则本身只此一份。
    """
    allowed = SUPPORTED_TEMPLATE_FIELDS if allowed_fields is None else allowed_fields
    normalized = template.strip()
    if normalized.lower().endswith(".mkv"):
        normalized = normalized[:-4].rstrip()

    if not normalized:
        raise ProcessingError(f"{label} 输出模板不能为空。")

    # 模板里的字面字符最终会进入文件名，必须覆盖 Windows 不允许的全部字符
    # （\ / : * ? " < > |），而不仅仅是路径分隔符。占位符用的 {}/ 不在此列。
    invalid_chars = sorted({char for char in normalized if char in WINDOWS_INVALID_FILENAME_CHARS})
    if invalid_chars:
        joined = " ".join(invalid_chars)
        raise ProcessingError(f"{label} 输出模板包含非法字符: {joined}")

    # FORMATTER.parse 在遇到不配对的大括号（如 ``on{vocal``、``a}b``）时会抛
    # ValueError。这类异常必须转成 ProcessingError，否则会从只捕获
    # ProcessingError 的调用处（保存设置 / 开始生成）逃逸，进而在 Qt 槽函数里
    # 触发未捕获异常导致程序闪退。
    try:
        fields = list(FORMATTER.parse(normalized))
    except ValueError as exc:
        raise ProcessingError(
            f"{label} 输出模板的大括号不配对，请检查 {{video_name}} 是否写完整。"
        ) from exc

    for _, field_name, _, _ in fields:
        if field_name and field_name not in allowed:
            supported = "、".join(f"{{{name}}}" for name in sorted(allowed))
            raise ProcessingError(
                f"{label} 输出模板包含不支持的占位符: {field_name}。当前支持 {supported}。"
            )

    return normalized


def template_uses_audio_name(template: str) -> bool:
    """模板里是否写了 ``{audio_name}``。"""
    try:
        fields = list(FORMATTER.parse(template or ""))
    except ValueError:
        return False
    return any(field_name == "audio_name" for _, field_name, _, _ in fields)


def render_name_template(template: str, label: str, values: dict[str, str]) -> str:
    """按给定的占位符取值渲染模板，并做与输出文件名一致的合法性校验。"""
    normalized = validate_output_name_template(template, label, set(values))
    try:
        rendered = normalized.format(**values).strip()
    except Exception as exc:  # noqa: BLE001
        raise ProcessingError(f"{label} 输出模板无法生成文件名: {exc}") from exc
    return sanitize_output_stem(rendered, label)


def render_output_stem(
    template: str,
    video_path: Path,
    label: str,
    audio_path: Path | None = None,
) -> str:
    normalized = validate_output_name_template(template, label)
    try:
        rendered = normalized.format(
            video_name=video_path.stem,
            audio_name=audio_path.stem if audio_path is not None else "",
        ).strip()
    except Exception as exc:  # noqa: BLE001
        raise ProcessingError(f"{label} 输出模板无法生成文件名: {exc}") from exc

    rendered = rendered.rstrip(". ")
    if not rendered:
        raise ProcessingError(f"{label} 输出模板生成的文件名为空。")

    invalid_chars = sorted({char for char in rendered if char in WINDOWS_INVALID_FILENAME_CHARS})
    if invalid_chars:
        joined = " ".join(invalid_chars)
        raise ProcessingError(f"{label} 输出文件名包含非法字符: {joined}")

    return rendered


def build_audio_normalization_command(
    ffmpeg_path: str,
    audio_path: Path,
    output_path: Path,
    sample_rate: int,
) -> list[str]:
    return [
        ffmpeg_path,
        "-y",
        "-hide_banner",
        "-i",
        str(audio_path),
        "-map",
        "0:a:0",
        "-vn",
        "-sn",
        "-dn",
        "-c:a",
        "flac",
        "-compression_level",
        "12",
        "-ar",
        str(sample_rate),
        "-sample_fmt",
        "s32",
        "-ac",
        "2",
        str(output_path),
    ]


def build_mux_command(
    ffmpeg_path: str,
    video_path: Path,
    audio_path: Path,
    output_path: Path,
    audio_title: str,
) -> list[str]:
    return [
        ffmpeg_path,
        "-y",
        "-hide_banner",
        "-i",
        str(video_path),
        "-i",
        str(audio_path),
        "-map",
        "0",
        "-map",
        "-0:a",
        # MOV（相机 / 剪辑软件导出）几乎都带 tmcd 时间码 data 流，而 Matroska
        # 只接受音视频 / 字幕 / 附件，不显式丢掉 data 流 ffmpeg 会在写 header 时
        # 报 "Only audio, video, and subtitles are supported for Matroska" 失败。
        "-map",
        "-0:d",
        "-map",
        "1:a:0",
        "-c:v",
        "copy",
        "-c:s",
        "copy",
        "-c:d",
        "copy",
        "-c:t",
        "copy",
        "-c:a",
        "copy",
        "-map_metadata",
        "-1",
        "-metadata:s:a:0",
        f"title={audio_title}",
        "-movflags",
        "+faststart",
        str(output_path),
    ]


def normalize_audio(
    ffmpeg_path: str,
    logger: Logger,
    audio_info: MediaInfo,
    output_path: Path,
    label: str,
    *,
    should_cancel: Callable[[], bool] | None = None,
    on_process_started: Callable[[subprocess.Popen | None], None] | None = None,
) -> int:
    if should_cancel is not None and should_cancel():
        raise ExportCancelled("生成已取消。")
    target_sample_rate = max(audio_info.sample_rate or 0, MIN_HIRES_SAMPLE_RATE)
    logger(f"开始预处理 {label}: 统一为 Hi-Res FLAC 32bit / {target_sample_rate}Hz / 2ch")

    command = build_audio_normalization_command(
        ffmpeg_path=ffmpeg_path,
        audio_path=audio_info.path,
        output_path=output_path,
        sample_rate=target_sample_rate,
    )
    try:
        run_command(command, logger, should_cancel=should_cancel, on_process_started=on_process_started)
    except ExportCancelled:
        raise
    except ProcessingError as exc:
        raise ProcessingError(f"{label} 预处理失败: {audio_info.path.name}\n{exc}") from exc

    if should_cancel is not None and should_cancel():
        raise ExportCancelled("生成已取消。")
    logger(f"{label} 预处理完成: {output_path.name}")
    return target_sample_rate


def mux_output(
    ffmpeg_path: str,
    logger: Logger,
    video_info: MediaInfo,
    normalized_audio_path: Path,
    output_path: Path,
    label: str,
    sample_rate: int,
    *,
    should_cancel: Callable[[], bool] | None = None,
    on_process_started: Callable[[subprocess.Popen | None], None] | None = None,
) -> Path:
    if should_cancel is not None and should_cancel():
        raise ExportCancelled("生成已取消。")
    logger(f"开始封装 {label}: 写入标准化音频流")

    command = build_mux_command(
        ffmpeg_path=ffmpeg_path,
        video_path=video_info.path,
        audio_path=normalized_audio_path,
        output_path=output_path,
        audio_title=DEFAULT_AUDIO_TITLE_TEMPLATE.format(sample_rate=sample_rate),
    )
    try:
        run_command(command, logger, should_cancel=should_cancel, on_process_started=on_process_started)
    except ExportCancelled:
        raise
    except ProcessingError as exc:
        raise ProcessingError(f"{label} 封装失败: {output_path.name}\n{exc}") from exc

    if should_cancel is not None and should_cancel():
        raise ExportCancelled("生成已取消。")
    logger(f"生成完成: {output_path.name}")
    return output_path


def process_output(
    ffmpeg_path: str,
    logger: Logger,
    video_info: MediaInfo,
    audio_info: MediaInfo,
    output_path: Path,
    temp_audio_path: Path,
    label: str,
    *,
    should_cancel: Callable[[], bool] | None = None,
    on_process_started: Callable[[subprocess.Popen | None], None] | None = None,
) -> Path:
    target_sample_rate = normalize_audio(
        ffmpeg_path=ffmpeg_path,
        logger=logger,
        audio_info=audio_info,
        output_path=temp_audio_path,
        label=label,
        should_cancel=should_cancel,
        on_process_started=on_process_started,
    )
    return mux_output(
        ffmpeg_path=ffmpeg_path,
        logger=logger,
        video_info=video_info,
        normalized_audio_path=temp_audio_path,
        output_path=output_path,
        label=label,
        sample_rate=target_sample_rate,
        should_cancel=should_cancel,
        on_process_started=on_process_started,
    )


def resolve_output_dir(video_path: Path, output_dir: Path | None = None) -> Path:
    return output_dir if output_dir is not None else video_path.parent


def resolve_output_paths(
    video_path: Path,
    output_dir: Path,
    output_name_mode: str,
    on_name_template: str | None = None,
    off_name_template: str | None = None,
    *,
    include_on: bool = True,
    include_off: bool = True,
    on_audio_path: Path | None = None,
    off_audio_path: Path | None = None,
) -> tuple[Path | None, Path | None]:
    if not include_on and not include_off:
        raise ProcessingError("至少需要生成原唱或伴奏中的一个输出文件。")
    if output_name_mode == OUTPUT_NAME_MODE_FIXED:
        return (
            output_dir / "on_vocal.mkv" if include_on else None,
            output_dir / "off_vocal.mkv" if include_off else None,
        )

    if output_name_mode == OUTPUT_NAME_MODE_VIDEO_NAME:
        on_name_template = DEFAULT_ON_NAME_TEMPLATE
        off_name_template = DEFAULT_OFF_NAME_TEMPLATE
        output_name_mode = OUTPUT_NAME_MODE_TEMPLATE

    if output_name_mode == OUTPUT_NAME_MODE_TEMPLATE:
        on_output: Path | None = None
        off_output: Path | None = None

        if include_on:
            on_template = on_name_template or DEFAULT_ON_NAME_TEMPLATE
            on_stem = render_output_stem(on_template, video_path, "原唱", on_audio_path)
            on_output = output_dir / f"{on_stem}.mkv"

        if include_off:
            off_template = off_name_template or DEFAULT_OFF_NAME_TEMPLATE
            off_stem = render_output_stem(off_template, video_path, "伴奏", off_audio_path)
            off_output = output_dir / f"{off_stem}.mkv"

        return on_output, off_output

    raise ProcessingError(f"不支持的输出命名模式: {output_name_mode}")


def resolve_off_output_paths(
    video_path: Path,
    output_dir: Path,
    output_name_mode: str,
    off_name_template: str | None,
    off_vocal_paths: Sequence[Path],
) -> list[Path]:
    """一条伴奏对应一个输出文件。

    只放一条时沿用原来的命名（模板 / 固定名），免得改变已有用户的输出习惯；放了多条
    才需要区分，此时用 ``{video_name}_{伴奏文件名}``——伴奏文件名本身就说明了这是哪
    一条，比在模板结果后面再堆一截更好认。
    """
    if not off_vocal_paths:
        return []

    template = off_name_template
    if output_name_mode == OUTPUT_NAME_MODE_VIDEO_NAME:
        template = DEFAULT_OFF_NAME_TEMPLATE
    elif output_name_mode == OUTPUT_NAME_MODE_TEMPLATE:
        template = template or DEFAULT_OFF_NAME_TEMPLATE
    else:
        template = None  # 固定名模式没有模板可用

    single = len(off_vocal_paths) == 1
    uses_audio = bool(template) and template_uses_audio_name(template)

    # 只有一条、且模板没用到 {audio_name} 时，命名跟以前完全一样。
    if single and not uses_audio:
        _, off_output = resolve_output_paths(
            video_path,
            output_dir,
            output_name_mode,
            off_name_template=off_name_template,
            include_on=False,
            include_off=True,
        )
        assert off_output is not None
        return [off_output]

    outputs: list[Path] = []
    taken: set[str] = set()
    for audio_path in off_vocal_paths:
        if template is None:
            # 固定名模式：off_vocal 区分不了多条，只能退回视频名 + 音频名。
            stem = sanitize_output_stem(f"{video_path.stem}_{audio_path.stem}", "伴奏")
        else:
            stem = render_output_stem(template, video_path, "伴奏", audio_path)
            if not uses_audio:
                # 模板里没写 {audio_name}，多条会重名，末尾补上音频名。
                stem = sanitize_output_stem(f"{stem}_{audio_path.stem}", "伴奏")
        # 不同目录下的同名伴奏仍会撞车，补一个序号而不是互相覆盖。
        candidate, index = stem, 2
        while candidate.lower() in taken:
            candidate = f"{stem}_{index}"
            index += 1
        taken.add(candidate.lower())
        outputs.append(output_dir / f"{candidate}.mkv")
    return outputs


def sanitize_output_stem(stem: str, label: str) -> str:
    """按输出文件名的规则校验一个已经拼好的名字（不经过模板）。"""
    cleaned = stem.strip().rstrip(". ")
    if not cleaned:
        raise ProcessingError(f"{label} 输出文件名为空。")
    invalid_chars = sorted({char for char in cleaned if char in WINDOWS_INVALID_FILENAME_CHARS})
    if invalid_chars:
        joined = " ".join(invalid_chars)
        raise ProcessingError(f"{label} 输出文件名包含非法字符: {joined}")
    return cleaned


def run_pipeline(
    video_path: Path,
    on_vocal_path: Path | None,
    off_vocal_path: Path | None = None,
    output_dir: Path | None = None,
    ffmpeg_dir: Path | None = None,
    output_name_mode: str = OUTPUT_NAME_MODE_VIDEO_NAME,
    on_name_template: str | None = None,
    off_name_template: str | None = None,
    logger: Logger = lambda _message: None,
    should_cancel: Callable[[], bool] | None = None,
    on_process_started: Callable[[subprocess.Popen | None], None] | None = None,
    *,
    off_vocal_paths: Sequence[Path] | None = None,
) -> list[Path]:
    """把字幕视频和音频混流成 Hi-Res 视频。

    ``off_vocal_paths`` 可以给多条伴奏，每条各出一个视频；``off_vocal_path`` 是它的
    单条写法，两者只能给一个。原唱始终最多一条。
    """
    if off_vocal_paths is not None and off_vocal_path is not None:
        raise ProcessingError("off_vocal_path 与 off_vocal_paths 只能提供一个。")
    off_paths: list[Path] = (
        list(off_vocal_paths)
        if off_vocal_paths is not None
        else ([off_vocal_path] if off_vocal_path is not None else [])
    )
    ffmpeg_path = find_tool("ffmpeg.exe", ffmpeg_dir)
    ffprobe_path = find_tool("ffprobe.exe", ffmpeg_dir)

    logger(f"FFmpeg: {ffmpeg_path}")
    logger(f"FFprobe: {ffprobe_path}")
    logger(describe_tool_source(ffmpeg_path, ffmpeg_dir))
    logger("正在分析输入文件...")
    if should_cancel is not None and should_cancel():
        raise ExportCancelled("生成已取消。")

    if on_vocal_path is None and not off_paths:
        raise ProcessingError("至少需要提供原唱音频或伴奏音频中的一个。")

    video_info = probe_media(ffprobe_path, video_path)
    on_vocal_info = probe_media(ffprobe_path, on_vocal_path) if on_vocal_path is not None else None
    off_vocal_infos = [probe_media(ffprobe_path, path) for path in off_paths]

    if video_info.video_streams == 0:
        raise ProcessingError("字幕视频里没有检测到视频流。")
    if on_vocal_info is not None and on_vocal_info.audio_streams == 0:
        raise ProcessingError("原唱无损文件里没有检测到音频流。")
    for info in off_vocal_infos:
        if info.audio_streams == 0:
            raise ProcessingError(f"伴奏无损文件里没有检测到音频流：{info.path.name}")

    log_media_summary(logger, "字幕视频", video_info)
    if on_vocal_info is not None:
        log_media_summary(logger, "原唱无损", on_vocal_info)
    for info in off_vocal_infos:
        log_media_summary(logger, "伴奏无损", info)
    for info in off_vocal_infos:
        log_audio_format_mismatch(logger, on_vocal_info, info)

    if on_vocal_info is not None:
        warn_duration_mismatch(logger, video_info, on_vocal_info, "原唱无损")
    for info in off_vocal_infos:
        warn_duration_mismatch(logger, video_info, info, "伴奏无损")
    if should_cancel is not None and should_cancel():
        raise ExportCancelled("生成已取消。")

    output_dir = resolve_output_dir(video_path, output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    on_output: Path | None = None
    if on_vocal_info is not None:
        on_output, _ = resolve_output_paths(
            video_path,
            output_dir,
            output_name_mode,
            on_name_template=on_name_template,
            include_on=True,
            include_off=False,
            on_audio_path=on_vocal_path,
        )
    off_outputs = resolve_off_output_paths(
        video_path, output_dir, output_name_mode, off_name_template, off_paths
    )
    logger(f"输出命名模式: {output_name_mode}")
    target_names = [path.name for path in ([on_output] if on_output else []) + off_outputs]
    logger(f"目标文件名: {' / '.join(target_names)}")

    with TemporaryDirectory(prefix="krok-helper-") as temp_dir_raw:
        temp_dir = Path(temp_dir_raw)
        outputs: list[Path] = []

        if on_vocal_info is not None and on_output is not None:
            outputs.append(
                process_output(
                    ffmpeg_path,
                    logger,
                    video_info,
                    on_vocal_info,
                    on_output,
                    temp_dir / "on_vocal.normalized.flac",
                    "On Vocal",
                    should_cancel=should_cancel,
                    on_process_started=on_process_started,
                )
            )

        for index, (info, off_output) in enumerate(zip(off_vocal_infos, off_outputs)):
            if len(off_outputs) > 1:
                logger(f"伴奏 {index + 1}/{len(off_outputs)}: {info.path.name}")
            outputs.append(
                process_output(
                    ffmpeg_path,
                    logger,
                    video_info,
                    info,
                    off_output,
                    temp_dir / f"off_vocal.{index}.normalized.flac",
                    "Off Vocal" if len(off_outputs) == 1 else f"Off Vocal {index + 1}",
                    should_cancel=should_cancel,
                    on_process_started=on_process_started,
                )
            )

    if should_cancel is not None and should_cancel():
        raise ExportCancelled("生成已取消。")
    logger(f"输出目录: {output_dir}")
    logger("全部处理完成。")
    return outputs
