"""Build ffmpeg commands from the stable subtitle render-job contract."""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtGui import QColor

from krok_helper.errors import ProcessingError
from krok_helper.subtitle_render.domain.background import BackgroundSource
from krok_helper.subtitle_render.engine.export.encoder_select import video_encoder_options
from krok_helper.subtitle_render.engine.render.render_bands import packed_offsets
from krok_helper.subtitle_render.engine.export.render_job import RenderJob
from krok_helper.subtitle_render.engine.export.render_job_policy import (
    resolve_duration_ms,
    resolved_background,
    validate_render_job,
)


_PREVIEW_FPS = 2
_PREVIEW_WIDTH = 640
_PREVIEW_MIN_WIDTH = 320


def resolved_preview_width(output_width: int, requested_width: int | None) -> int:
    requested = _PREVIEW_WIDTH if requested_width is None else int(requested_width)
    return min(max(int(output_width), 1), max(_PREVIEW_MIN_WIDTH, requested))


def background_scale_chain(job: RenderJob, duration_seconds: float) -> str:
    """Build the background scaling chain with preview-equivalent semantics."""

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
            str(source.path),
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
        filter_graph += (
            ";[v]split=2[venc][vpin];"
            f"[vpin]fps={_PREVIEW_FPS},scale={preview_width}:-2[vprev]"
        )
        video_label = "[venc]"
    background = resolved_background(job)
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
        *background_input_args(background, job, duration_seconds),
    ]
    audio_input_index: int | None = None
    if job.include_audio and job.audio_path is not None:
        audio_input_index = 2
        command.extend(["-i", str(job.audio_path)])
    elif job.include_audio and background.kind == "video":
        audio_input_index = 1
    command.extend(["-filter_complex", filter_graph, "-map", video_label])
    if audio_input_index is not None:
        command.extend(["-map", f"{audio_input_index}:a:0?"])
    command.extend(
        ["-t", f"{duration_seconds:.6f}", "-r", str(job.fps), "-fps_mode", "cfr"]
    )
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
