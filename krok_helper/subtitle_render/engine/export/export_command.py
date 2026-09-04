"""Build ffmpeg commands from the stable subtitle render-job contract."""

from __future__ import annotations

import re
from pathlib import Path

from PyQt6.QtGui import QColor

from krok_helper.errors import ProcessingError
from krok_helper.subtitle_render.domain.background import BackgroundSource
from krok_helper.subtitle_render.engine.export.encoder_select import video_encoder_options
from krok_helper.subtitle_render.engine.render.render_bands import packed_offsets
from krok_helper.subtitle_render.engine.export.render_job import (
    OUTPUT_FORMAT_MP4,
    OUTPUT_FORMAT_MOV_TRANSPARENT,
    RenderJob,
    format_has_alpha,
    format_needs_background,
    is_png_sequence,
)
from krok_helper.subtitle_render.engine.export.render_job_policy import (
    resolve_duration_ms,
    resolved_background,
    validate_render_job,
)


_PREVIEW_FPS = 2
_PREVIEW_WIDTH = 640
_PREVIEW_MIN_WIDTH = 320


def ffmpeg_sequence_pattern(path: str) -> str:
    """Escape literal ``%`` in an image-sequence path, keeping the number pattern.

    image2 expands the input path with snprintf semantics, so a literal ``%`` in
    a directory name (e.g. ``100%Love``) is consumed as a bogus conversion and
    the input fails to open.  ``%d`` / ``%04d`` stay untouched; everything else
    becomes ``%%``, which snprintf renders back as ``%``.
    """

    return re.sub(r"%(?!0?\d*d)", "%%", path)


def resolved_preview_width(output_width: int, requested_width: int | None) -> int:
    requested = _PREVIEW_WIDTH if requested_width is None else int(requested_width)
    return min(max(int(output_width), 1), max(_PREVIEW_MIN_WIDTH, requested))


def sequence_output_pattern(sequence_dir: Path) -> str:
    """Build the image2 output pattern ``<dir>/<name>_%06d.png`` (1-based).

    The frames live *inside* ``sequence_dir`` and are prefixed with the folder
    name, e.g. ``out/名称/名称_000001.png``.  Unlike
    :func:`ffmpeg_sequence_pattern` this escapes every literal ``%`` in the
    path — the ``%06d`` placeholder is appended afterwards and must stay the
    only conversion in the pattern.
    """

    folder = re.sub("%", "%%", sequence_dir.as_posix())
    name = re.sub("%", "%%", sequence_dir.name)
    return f"{folder}/{name}_%06d.png"


def transparent_background_chain(job: RenderJob, duration_seconds: float) -> str:
    """Fully transparent full-canvas base for alpha-preserving exports.

    Alpha formats (transparent PNG sequence / QuickTime Animation MOV) skip the
    background input entirely; the subtitle layer is overlaid onto this source
    so the strip/bands packing optimizations keep working unchanged.
    """

    return (
        f"color=c=black@0.0:s={job.width}x{job.height}:r={job.fps}"
        f":d={duration_seconds:.6f}[bg];"
    )


def background_scale_chain(job: RenderJob, duration_seconds: float) -> str:
    """Build the background scaling chain with preview-equivalent semantics."""

    if not format_needs_background(job.output_format):
        return transparent_background_chain(job, duration_seconds)
    source = resolved_background(job)
    cover = source.kind in {"image", "image_sequence"} and source.image_fit == "cover"
    if cover:
        return (
            f"[1:v:0]scale={job.width}:{job.height}"
            ":force_original_aspect_ratio=increase:force_divisible_by=2,"
            f"crop={job.width}:{job.height},"
            f"fps={job.fps},trim=duration={duration_seconds:.6f},"
            "setpts=PTS-STARTPTS[bg];"
        )
    return (
        f"[1:v:0]scale={job.width}:{job.height}:force_original_aspect_ratio=decrease,"
        f"pad={job.width}:{job.height}:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"fps={job.fps},trim=duration={duration_seconds:.6f},setpts=PTS-STARTPTS[bg];"
    )


def bands_filter_graph(
    job: RenderJob, duration_seconds: float, bands: list[tuple[int, int]]
) -> str:
    """Restore packed subtitle bands to their original vertical positions."""

    offsets = packed_offsets(bands)
    n = len(bands)
    bg = background_scale_chain(job, duration_seconds)
    ov = "[0:v:0]format=rgba,setpts=PTS-STARTPTS[ov];"
    split = "[ov]split=" + str(n) + "".join(f"[p{i}]" for i in range(n)) + ";"
    crops = "".join(
        f"[p{i}]crop={job.width}:{height}:0:{off}[c{i}];"
        for i, ((_top, height), off) in enumerate(zip(bands, offsets))
    )
    chain = ""
    previous = "[bg]"
    for index, (top, _height) in enumerate(bands):
        output = "[v]" if index == n - 1 else f"[o{index}]"
        chain += f"{previous}[c{index}]overlay=0:{top}:format=auto{output};"
        previous = output
    return (bg + ov + split + crops + chain).rstrip(";")


def background_input_args(
    source: BackgroundSource, job: RenderJob, duration_seconds: float
) -> list[str]:
    """Return arguments for the background, which always occupies input #1."""

    if source.kind == "solid":
        color = QColor(source.color)
        if not color.isValid():
            raise ProcessingError(f"背景颜色无效: {source.color}")
        return [
            "-f",
            "lavfi",
            "-i",
            f"color=c={color.name()}:s={job.width}x{job.height}:r={job.fps}:d={duration_seconds:.6f}",
        ]
    if source.kind == "image":
        return ["-loop", "1", "-framerate", str(job.fps), "-i", str(source.path)]
    if source.kind == "image_sequence":
        fps = max(int(source.source_fps or job.fps), 1)
        return [
            "-stream_loop",
            "-1",
            "-framerate",
            str(fps),
            "-start_number",
            str(max(int(source.sequence_start_number), 0)),
            "-i",
            ffmpeg_sequence_pattern(str(source.path)),
        ]
    args: list[str] = []
    if source.video_offset_ms:
        args.extend(["-ss", f"{source.video_offset_ms / 1000.0:.6f}"])
    args.extend(["-i", str(source.path)])
    return args


def build_render_command(
    ffmpeg_path: str,
    job: RenderJob,
    *,
    duration_ms: int | None = None,
    strip: tuple[int, int] | None = None,
    bands: list[tuple[int, int]] | None = None,
    preview_image_path: Path | None = None,
    preview_width: int | None = None,
) -> list[str]:
    """Build the ffmpeg command used by the subtitle export executor."""

    validate_render_job(job)
    duration = resolve_duration_ms(job) if duration_ms is None else duration_ms
    duration_seconds = max(duration / 1000.0, 0.001)
    overlay_y = 0
    pipe_w, pipe_h = job.width, job.height
    if bands is not None:
        pipe_h = sum(height for _top, height in bands)
        filter_graph = bands_filter_graph(job, duration_seconds, bands)
    else:
        if strip is not None:
            overlay_y, pipe_h = strip
        filter_graph = (
            background_scale_chain(job, duration_seconds)
            + "[0:v:0]format=rgba,setpts=PTS-STARTPTS[ov];"
            + f"[bg][ov]overlay=0:{overlay_y}:format=auto[v]"
        )
    video_label = "[v]"
    if preview_image_path is not None:
        preview_width = resolved_preview_width(job.width, preview_width)
        if format_has_alpha(job.output_format):
            # 透明格式的 [v] 是无背景的透明字幕层，而 jpg 预览承载不了 alpha
            # （直接编码会变成「黑底白字」，看起来像渲染失败）。给预览支路垫
            # 一层中灰底，明确表达「这是透明字幕层、画面里没有背景」。
            filter_graph += (
                ";[v]split=2[venc][vpin];"
                f"color=c=0x3F444C:s={job.width}x{job.height}:r={job.fps}"
                f":d={duration_seconds:.6f}[vpbg];"
                "[vpbg][vpin]overlay=0:0:format=auto[vpcomp];"
                f"[vpcomp]fps={_PREVIEW_FPS},scale={preview_width}:-2[vprev]"
            )
        else:
            filter_graph += (
                ";[v]split=2[venc][vpin];"
                f"[vpin]fps={_PREVIEW_FPS},scale={preview_width}:-2[vprev]"
            )
        video_label = "[venc]"
    background = resolved_background(job)
    needs_background = format_needs_background(job.output_format)
    command = [
        ffmpeg_path,
        "-y",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgba",
        "-s:v",
        f"{pipe_w}x{pipe_h}",
        "-r",
        str(job.fps),
        "-i",
        "pipe:0",
        *(
            background_input_args(background, job, duration_seconds)
            if needs_background
            else []
        ),
    ]
    audio_input_index: int | None = None
    if job.include_audio and not is_png_sequence(job.output_format):
        # MP4 与透明 MOV 都携带音频；PNG 序列的 image2 输出装不下音频。
        if job.audio_path is not None:
            # 独立音频：含背景格式排在输入 2；透明格式没有背景输入，排 1。
            audio_input_index = 2 if needs_background else 1
            command.extend(["-i", str(job.audio_path)])
        elif background.kind == "video":
            if needs_background:
                audio_input_index = 1
            else:
                # 透明 MOV 不解码背景画面，但音频与 MP4 同源：把背景视频
                # 作为「仅音频」输入（视频流未被引用，不参与解码）。
                audio_input_index = 1
                if background.video_offset_ms:
                    command.extend(
                        ["-ss", f"{background.video_offset_ms / 1000.0:.6f}"]
                    )
                command.extend(["-i", str(background.path)])
    command.extend(["-filter_complex", filter_graph, "-map", video_label])
    if audio_input_index is not None:
        command.extend(["-map", f"{audio_input_index}:a:0?"])
    command.extend(
        ["-t", f"{duration_seconds:.6f}", "-r", str(job.fps), "-fps_mode", "cfr"]
    )
    if job.output_format == OUTPUT_FORMAT_MP4:
        command.extend(
            video_encoder_options(
                ffmpeg_path,
                job.encoder_mode,
                crf=job.crf,
                preset=job.preset,
                codec=job.codec,
            )
        )
        command.extend(["-pix_fmt", "yuv420p"])
        if audio_input_index is not None:
            command.extend(["-c:a", "aac", "-b:a", "192k"])
        command.extend(["-movflags", "+faststart", str(job.output_path)])
    elif job.output_format == OUTPUT_FORMAT_MOV_TRANSPARENT:
        # ProRes 4444：NLE（PR/AE/达芬奇）对 4444 的 alpha 解释最成熟可靠；
        # qtrle 虽然无损且更小，但消费端常把 straight alpha 按预乘处理，
        # 发光/描边的半透明渐变会出现明显脏边。prores_ks 仅 4444/4444 XQ
        # 两档支持 alpha，这里固定 4444 + yuva444p10le。
        command.extend(
            [
                "-c:v",
                "prores_ks",
                "-profile:v",
                "4444",
                "-pix_fmt",
                "yuva444p10le",
            ]
        )
        if audio_input_index is not None:
            # ProRes 素材惯例配 PCM，避免个别工具对 MOV 内 AAC 的兼容问题。
            command.extend(["-c:a", "pcm_s16le"])
        command.extend([str(job.output_path)])
    else:
        assert is_png_sequence(job.output_format)
        command.extend(
            [
                "-c:v",
                "png",
                "-pix_fmt",
                "rgba" if format_has_alpha(job.output_format) else "rgb24",
                "-start_number",
                "1",
                "-f",
                "image2",
                sequence_output_pattern(job.output_path),
            ]
        )
    if preview_image_path is not None:
        command.extend(
            [
                "-map",
                "[vprev]",
                "-f",
                "image2",
                "-update",
                "1",
                "-atomic_writing",
                "1",
                "-q:v",
                "2",
                str(preview_image_path),
            ]
        )
    return command
