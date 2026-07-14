"""ffmpeg rawvideo pipe renderer for subtitle videos.

A8 MVP renders a transparent subtitle overlay with QPainter and lets ffmpeg
compose it over the background video.  Audio is copied from the background video
when present.
"""

from __future__ import annotations

import math
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np
from PyQt6.QtGui import QColor, QImage, QPainter

from krok_helper.errors import ExportCancelled, ProcessingError
from krok_helper.ffmpeg import _build_subprocess_kwargs, find_tool, terminate_process
from krok_helper.subtitle_render.engine.encoder_select import (
    CPU_PRESETS,
    ENCODER_MODES,
    resolved_encoder_label,
    video_encoder_options,
)
from krok_helper.subtitle_render.engine.native_export import iter_native_rgba_frames
from krok_helper.subtitle_render.engine.painter import (
    frame_content_intervals,
    frame_vertical_bounds,
    frame_has_content,
    paint_frame,
    paint_frame_to_painter,
)
from krok_helper.subtitle_render.engine.timeline import track_duration_ms
from krok_helper.subtitle_render.models import BackgroundSource, Style, TimingTrack
from krok_helper.subtitle_render.native_backend import NativeRendererError, resolve_native_renderer_path

# A2 条带渲染：只把字幕所在窄条喂给 ffmpeg pipe，省每帧 8MB 拷贝 / pipe 带宽。
# 条带 = 整段渲染里所有可见内容纵向范围的并集（单条覆盖，方案 A）。可用环境变量
# KROK_SUBTITLE_RENDER_STRIP=0 关闭退回整帧。
_STRIP_MARGIN_PX = 8  # 安全边：采样可能漏掉单帧动画极值
_STRIP_MIN_GAIN_RATIO = 0.85  # 并集 ≥ 全高的此比例则不值当，退回整帧
_STRIP_MAX_SAMPLES = 200  # 纵向并集预扫的最大采样帧数

# A3 多进程导出：offscreen worker 池并行渲帧，主进程按序喂 ffmpeg。
# KROK_SUBTITLE_RENDER_WORKERS=N 指定进程数（1=关闭，走单进程）。worker 不强制
# offscreen——继承父进程 QT_QPA_PLATFORM，保证字体与预览/单进程一致。
_MULTIPROC_WORKER_CAP = 8  # 进程数上限（每个 worker 一份 QApplication）
_MULTIPROC_MIN_FRAMES = 240  # 帧数低于此不值当 spawn，走单进程
_CHUNK_TARGET_BYTES = 64 * 1024 * 1024  # 单个 chunk 目标字节（控内存 / IPC 粒度）

# 导出预览（仿 N3 出力预览）：把 ffmpeg 内合成后的成品流 split 一路，按视频时间
# 降频 + 缩宽后持续覆盖写入单张 JPG（image2 -update，原子写），UI 轮询该文件即可
# 边导出边看画面。fps=2 → 60fps 输出下约每 30 帧刷新一次。
_PREVIEW_FPS = 2
_PREVIEW_WIDTH = 640
from krok_helper.types import Logger


@dataclass(frozen=True)
class RenderJob:
    track: TimingTrack
    style: Style
    background_video_path: Path | None
    output_path: Path
    background_source: BackgroundSource | None = None
    audio_path: Path | None = None
    width: int = 1920
    height: int = 1080
    fps: int = 60
    duration_ms: int | None = None
    include_audio: bool = True
    encoder_mode: str = "cpu"
    crf: int = 18
    preset: str = "veryfast"
    native_export_enabled: bool | None = None
    extra_tracks: tuple[TimingTrack, ...] = ()
    """副字幕源（N3 多歌词文件，如コーラス轨），与主轨同帧叠绘。"""


def _job_tracks(job: "RenderJob") -> list[TimingTrack]:
    return [job.track, *job.extra_tracks]


def render_subtitle_video(
    job: RenderJob,
    *,
    ffmpeg_dir: Path | None = None,
    logger: Logger | None = None,
    should_cancel: Callable[[], bool] | None = None,
    on_process_started: Callable[[subprocess.Popen | None], None] | None = None,
    on_progress: Callable[[int, int], None] | None = None,
    preview_image_path: Path | None = None,
) -> Path:
    """Render ``job`` to MP4 using a transparent rawvideo subtitle pipe."""
    logger = logger or (lambda _message: None)
    _validate_job(job)
    ffmpeg_path = find_tool("ffmpeg.exe", ffmpeg_dir)

    duration_ms = _resolve_duration_ms(job)
    total_frames = _frame_count(duration_ms, job.fps)
    native_export_requested = _native_export_requested(job)
    native_renderer_path = resolve_native_renderer_path() if native_export_requested else None
    native_export_active = native_renderer_path is not None
    if native_export_requested and native_renderer_path is None:
        logger("native 导出 sidecar 未找到，已回退到 Python 渲染器")

    # A2：预扫字幕纵向范围只渲染窄条（取消 / 关闭 / 无收益时退回整帧）。
    # 优先方案 B（多条分离带），不适用时退回方案 A（单条并集）。
    cancelled = should_cancel is not None and should_cancel()
    strip: tuple[int, int] | None = None
    bands: list[tuple[int, int]] | None = None
    if _strip_enabled() and not cancelled and not native_export_active:
        if _bands_enabled():
            bands = _compute_content_bands(job, duration_ms, should_cancel=should_cancel)
        if bands is None:
            strip = _compute_subtitle_strip(job, duration_ms, should_cancel=should_cancel)

    if bands is not None:
        packed_h = sum(height for _top, height in bands)
        logger(f"多带渲染: {len(bands)} 条 打包高={packed_h}（全高 {job.height}）{bands}")
        command = build_render_command(
            ffmpeg_path, job, duration_ms=duration_ms, bands=bands,
            preview_image_path=preview_image_path,
        )
    else:
        strip_top, render_h = strip if strip is not None else (0, job.height)
        if strip is not None:
            logger(f"条带渲染: y={strip_top} 高={render_h}（全高 {job.height}）")
        command = build_render_command(
            ffmpeg_path, job, duration_ms=duration_ms, strip=strip,
            preview_image_path=preview_image_path,
        )

    job.output_path.parent.mkdir(parents=True, exist_ok=True)
    logger(f"导出字幕视频: {job.output_path.name}")
    logger(
        f"输出参数: {job.width}x{job.height} / {job.fps}fps / "
        f"{duration_ms / 1000:.3f}s / {resolved_encoder_label(ffmpeg_path, job.encoder_mode)} / CRF {job.crf}"
    )
    logger("执行命令:")
    logger(" ".join(f'"{part}"' if " " in part else part for part in command))

    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        **_build_subprocess_kwargs(),
    )
    if on_process_started is not None:
        on_process_started(process)

    try:
        assert process.stdin is not None
        # A3：帧数够多时多进程并行渲染（offscreen worker 池），主进程按序喂 ffmpeg；
        # 否则走单进程。两条路径逐帧逻辑一致（A4 缓冲复用 + 空帧短路 + A2 条带/多带）。
        worker_count = _resolve_worker_count(total_frames)
        if native_export_active:
            logger(f"native 导出: {native_renderer_path}")
            _write_frames_native(
                process, job, total_frames, native_renderer_path,
                should_cancel, on_progress,
            )
        elif worker_count > 1:
            logger(f"多进程导出: {worker_count} 个 worker")
            if bands is not None:
                _write_frames_multiprocess_bands(
                    process, job, bands, packed_h, total_frames,
                    worker_count, should_cancel, on_progress,
                )
            else:
                _write_frames_multiprocess(
                    process, job, strip_top, render_h, total_frames,
                    worker_count, should_cancel, on_progress,
                )
        elif bands is not None:
            _write_frames_single_bands(
                process, job, bands, packed_h, total_frames,
                should_cancel, on_progress,
            )
        else:
            _write_frames_single(
                process, job, strip_top, render_h, total_frames,
                should_cancel, on_progress,
            )
        process.stdin.close()
        _drain_process_output(process, logger)
        return_code = process.wait()
    except ExportCancelled:
        terminate_process(process)
        _remove_incomplete_output(job.output_path, logger)
        raise
    except NativeRendererError as exc:
        terminate_process(process)
        _remove_incomplete_output(job.output_path, logger)
        raise ProcessingError(f"native 字幕渲染器导出失败: {exc}") from exc
    except (BrokenPipeError, OSError) as exc:
        terminate_process(process)
        _remove_incomplete_output(job.output_path, logger)
        if should_cancel is not None and should_cancel():
            raise ExportCancelled("已停止导出。") from exc
        raise ProcessingError(f"ffmpeg 管道写入失败: {exc}") from exc
    finally:
        if on_process_started is not None:
            on_process_started(None)

    if should_cancel is not None and should_cancel():
        _remove_incomplete_output(job.output_path, logger)
        raise ExportCancelled("已停止导出。")
    if return_code != 0:
        _remove_incomplete_output(job.output_path, logger)
        raise ProcessingError(f"ffmpeg 执行失败，退出码: {return_code}")
    if not job.output_path.is_file() or os.path.getsize(job.output_path) == 0:
        raise ProcessingError(f"导出失败，未生成有效文件: {job.output_path}")

    logger(f"字幕视频导出完成: {job.output_path}")
    return job.output_path


def _bands_filter_graph(job: RenderJob, duration_seconds: float, bands: list[tuple[int, int]]) -> str:
    """方案 B 的 filter graph：打包输入 split → 各 band crop → 逐条 overlay 回原始 y。"""
    offsets = _packed_offsets(bands)
    n = len(bands)
    bg = (
        f"[1:v:0]scale={job.width}:{job.height}:force_original_aspect_ratio=decrease,"
        f"pad={job.width}:{job.height}:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"fps={job.fps},trim=duration={duration_seconds:.6f},setpts=PTS-STARTPTS[bg];"
    )
    ov = "[0:v:0]format=rgba,setpts=PTS-STARTPTS[ov];"
    split = "[ov]split=" + str(n) + "".join(f"[p{i}]" for i in range(n)) + ";"
    crops = "".join(
        f"[p{i}]crop={job.width}:{height}:0:{off}[c{i}];"
        for i, ((_top, height), off) in enumerate(zip(bands, offsets))
    )
    chain = ""
    prev = "[bg]"
    for i, (top, _height) in enumerate(bands):
        out = "[v]" if i == n - 1 else f"[o{i}]"
        chain += f"{prev}[c{i}]overlay=0:{top}:format=auto{out};"
        prev = out
    return (bg + ov + split + crops + chain).rstrip(";")


def build_render_command(
    ffmpeg_path: str,
    job: RenderJob,
    *,
    duration_ms: int | None = None,
    strip: tuple[int, int] | None = None,
    bands: list[tuple[int, int]] | None = None,
    preview_image_path: Path | None = None,
) -> list[str]:
    """Build the ffmpeg command used by :func:`render_subtitle_video`.

    ``strip`` = ``(y_top, height)``：仅把该窄条作为 rawvideo 输入，``overlay=0:y_top``
    贴回全幅背景；``None`` 时整帧输入、``overlay=0:0``（原行为）。
    ``bands`` = 多条 ``(y_top, height)``（方案 B）：竖向打包成一条 pipe，split/crop/overlay
    还原到各自原始 y；给定时优先于 ``strip``。
    ``preview_image_path``：导出预览 —— 把合成后的 ``[v]`` 再 split 一路，降频缩宽后
    持续覆盖写入该 JPG（原子写），供 UI 边导出边轮询显示。
    """
    _validate_job(job)
    duration = _resolve_duration_ms(job) if duration_ms is None else duration_ms
    duration_seconds = max(duration / 1000.0, 0.001)
    overlay_y = 0
    pipe_w, pipe_h = job.width, job.height
    if bands is not None:
        pipe_h = sum(height for _top, height in bands)
        filter_graph = _bands_filter_graph(job, duration_seconds, bands)
    else:
        if strip is not None:
            overlay_y, pipe_h = strip
        filter_graph = (
            f"[1:v:0]scale={job.width}:{job.height}:force_original_aspect_ratio=decrease,"
            f"pad={job.width}:{job.height}:(ow-iw)/2:(oh-ih)/2:color=black,"
            f"fps={job.fps},trim=duration={duration_seconds:.6f},setpts=PTS-STARTPTS[bg];"
            "[0:v:0]format=rgba,setpts=PTS-STARTPTS[ov];"
            f"[bg][ov]overlay=0:{overlay_y}:format=auto[v]"
        )
    video_label = "[v]"
    if preview_image_path is not None:
        filter_graph += (
            ";[v]split=2[venc][vpin];"
            f"[vpin]fps={_PREVIEW_FPS},scale={_PREVIEW_WIDTH}:-2[vprev]"
        )
        video_label = "[venc]"
    background = _resolved_background(job)
    background_args = _background_input_args(background, job, duration_seconds)
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
        *background_args,
    ]
    audio_input_index: int | None = None
    if job.include_audio and job.audio_path is not None:
        audio_input_index = 2
        command.extend(["-i", str(job.audio_path)])
    elif job.include_audio and background.kind == "video":
        audio_input_index = 1
    command.extend([
        "-filter_complex",
        filter_graph,
        "-map",
        video_label,
    ])
    if audio_input_index is not None:
        command.extend(["-map", f"{audio_input_index}:a:0?"])
    command.extend(["-t", f"{duration_seconds:.6f}", "-r", str(job.fps), "-fps_mode", "cfr"])
    command.extend(
        video_encoder_options(
            ffmpeg_path,
            job.encoder_mode,
            crf=job.crf,
            preset=job.preset,
        )
    )
    command.extend(["-pix_fmt", "yuv420p"])
    if audio_input_index is not None:
        command.extend(["-c:a", "aac", "-b:a", "192k"])
    command.extend(["-movflags", "+faststart", str(job.output_path)])
    if preview_image_path is not None:
        command.extend([
            "-map", "[vprev]",
            "-f", "image2",
            "-update", "1",
            "-atomic_writing", "1",
            "-q:v", "4",
            str(preview_image_path),
        ])
    return command


def _validate_job(job: RenderJob) -> None:
    if all(track.char_count <= 0 for track in _job_tracks(job)):
        raise ProcessingError("请先加载有效的字幕文件。")
    background = _resolved_background(job)
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
    if job.fps <= 0:
        raise ProcessingError("输出 fps 无效。")
    if job.encoder_mode not in ENCODER_MODES:
        raise ProcessingError(f"不支持的编码器: {job.encoder_mode}")
    if not 0 <= job.crf <= 51:
        raise ProcessingError("CRF 必须在 0 到 51 之间。")
    if job.preset not in CPU_PRESETS:
        raise ProcessingError(f"不支持的 CPU preset: {job.preset}")
    if not str(job.output_path).strip():
        raise ProcessingError("请先选择输出路径。")


def _resolved_background(job: RenderJob) -> BackgroundSource:
    if job.background_source is not None:
        return job.background_source
    if job.background_video_path is not None:
        return BackgroundSource(kind="video", path=str(job.background_video_path))
    return BackgroundSource(kind="solid", color="#000000")


def _background_input_args(
    source: BackgroundSource, job: RenderJob, duration_seconds: float
) -> list[str]:
    """返回始终占据 ffmpeg input #1 的背景输入参数。"""
    if source.kind == "solid":
        color = QColor(source.color)
        if not color.isValid():
            raise ProcessingError(f"背景颜色无效: {source.color}")
        return [
            "-f", "lavfi", "-i",
            f"color=c={color.name()}:s={job.width}x{job.height}:r={job.fps}:d={duration_seconds:.6f}",
        ]
    if source.kind == "image":
        return ["-loop", "1", "-framerate", str(job.fps), "-i", str(source.path)]
    if source.kind == "image_sequence":
        fps = max(int(source.source_fps or job.fps), 1)
        return [
            "-stream_loop", "-1", "-framerate", str(fps),
            "-start_number", str(max(int(source.sequence_start_number), 0)),
            "-i", str(source.path),
        ]
    args: list[str] = []
    if source.video_offset_ms:
        args.extend(["-ss", f"{source.video_offset_ms / 1000.0:.6f}"])
    args.extend(["-i", str(source.path)])
    return args


def _resolve_duration_ms(job: RenderJob) -> int:
    if job.duration_ms is not None and job.duration_ms > 0:
        return job.duration_ms
    duration = max(track_duration_ms(track) for track in _job_tracks(job))
    if duration <= 0:
        raise ProcessingError("字幕时长无效，无法导出。")
    return duration


def _frame_count(duration_ms: int, fps: int) -> int:
    return max(1, int(math.ceil(duration_ms * fps / 1000)))


def _image_bytes(image: QImage) -> bytes:
    """Copy ``image`` 的原始 RGBA 像素为 ``bytes``（喂给 ffmpeg pipe）。"""
    bits = image.constBits()
    bits.setsize(image.sizeInBytes())
    return bytes(bits)


def _strip_enabled() -> bool:
    return os.environ.get("KROK_SUBTITLE_RENDER_STRIP", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


def _native_export_enabled() -> bool:
    return False


def _native_export_requested(job: RenderJob) -> bool:
    # Keep the job field for project-file compatibility, but never activate the
    # sidecar until its layout matches the Python renderer again.
    return False


def _paint_overlay_strip(
    buffer: QImage,
    track: TimingTrack,
    style: Style,
    t_ms: int,
    *,
    logical_w: int,
    logical_h: int,
    strip_top: int,
    transparent: QColor,
    extra_tracks: tuple[TimingTrack, ...] = (),
) -> None:
    """把整帧字幕布局画进只有条带高的 ``buffer``。

    布局仍按整帧逻辑尺寸（``logical_h`` = 全高）计算，画笔整体上移 ``strip_top``，
    于是只有 ``[strip_top, strip_top + buffer 高)`` 这条会落进 buffer。``strip_top=0``
    且 buffer 高=全高时即等价于整帧渲染。
    """
    buffer.fill(transparent)
    painter = QPainter(buffer)
    try:
        if strip_top:
            painter.translate(0, -strip_top)
        paint_frame_to_painter(
            painter, logical_w, logical_h, track, t_ms, style, list(extra_tracks or ())
        )
    finally:
        painter.end()


def _strip_sample_times(
    tracks: list[TimingTrack], style: Style, duration_ms: int, total_frames: int
) -> list[int]:
    """纵向并集预扫的采样时刻：均匀网格 + 每轨每行起止（含 lead-in/tail 动画极值）。"""
    times: set[int] = set()
    grid = min(total_frames, _STRIP_MAX_SAMPLES)
    for i in range(grid):
        times.add(int(round(i * duration_ms / max(grid - 1, 1))))
    lead = max(getattr(style, "line_lead_in_ms", 0) or 0, 0)
    tail = max(getattr(style, "line_tail_ms", 0) or 0, 0)
    for line in [line for track in tracks for line in track.lines]:
        if not line.chars:
            continue
        start = line.chars[0].start_ms
        end = line.end_ms or start
        for tt in (start - lead, start, end, end + tail):
            times.add(tt)
    return sorted(t for t in times if 0 <= t <= duration_ms)


def _content_row_bounds(image: QImage) -> tuple[int, int] | None:
    """返回 ``image`` 里 alpha>0 的最上 / 最下行；全透明返回 ``None``。"""
    width = image.width()
    height = image.height()
    bpl = image.bytesPerLine()
    ptr = image.constBits()
    ptr.setsize(image.sizeInBytes())
    arr = np.frombuffer(ptr, dtype=np.uint8, count=bpl * height).reshape(height, bpl)
    alpha = arr[:, 3 : width * 4 : 4]  # Format_RGBA8888：每像素第 4 字节为 A
    rows = np.nonzero(alpha.any(axis=1))[0]
    if rows.size == 0:
        return None
    return int(rows[0]), int(rows[-1])


def _compute_subtitle_strip(
    job: RenderJob,
    duration_ms: int,
    *,
    should_cancel: Callable[[], bool] | None = None,
) -> tuple[int, int] | None:
    """预扫整段，求所有可见字幕内容的纵向并集 ``(y_top, height)``。

    单条覆盖（方案 A）：有几行 / 几个来源 / 带不带信号注音，都由实际渲染像素的并集
    决定，不假设固定行数。并集 ≥ 全高 ``_STRIP_MIN_GAIN_RATIO`` 或全空时返回 ``None``
    退回整帧。yuv420p 友好：top 下取偶、height 上取偶。
    """
    width, height = job.width, job.height
    total_frames = _frame_count(duration_ms, job.fps)
    times = _strip_sample_times(_job_tracks(job), job.style, duration_ms, total_frames)
    if not times:
        return None

    extras = list(job.extra_tracks)
    scratch: QImage | None = None
    transparent = QColor(0, 0, 0, 0)
    top = height
    bottom = -1
    for t_ms in times:
        if should_cancel is not None and should_cancel():
            return None
        if not frame_has_content(job.track, t_ms, job.style, extras):
            continue
        bounds = frame_vertical_bounds(width, height, job.track, t_ms, job.style, extras)
        if bounds is None:
            if scratch is None:
                scratch = QImage(width, height, QImage.Format.Format_RGBA8888)
            scratch.fill(transparent)
            paint_frame(scratch, job.track, t_ms, job.style, extras)
            bounds = _content_row_bounds(scratch)
        if bounds is None:
            continue
        top = min(top, bounds[0])
        bottom = max(bottom, bounds[1])

    if bottom < top:
        return None  # 整段无可见内容

    top = max(0, top - _STRIP_MARGIN_PX)
    bottom = min(height - 1, bottom + _STRIP_MARGIN_PX)
    top -= top % 2  # 下取偶
    strip_h = bottom - top + 1
    if strip_h % 2:
        strip_h += 1
    strip_h = min(strip_h, height - top)
    if strip_h >= height * _STRIP_MIN_GAIN_RATIO:
        return None  # 并集太高，省不了多少，退回整帧
    return top, strip_h


# ---------------------------------------------------------------------------
# A2 方案 B：多条分离带（顶部标题 + 底部歌词等互不相交的内容块各开一条）
# ---------------------------------------------------------------------------
# 单条并集（方案 A）在内容劈成相隔很远的两块时会退化成近全高、不省。方案 B 把
# 互不相交的内容块拆成多条，竖向打包进**同一条** rawvideo pipe（省去多输入/多
# pipe 的复杂度），再用 ffmpeg split/crop/overlay 还原到各自原始 y。
_BAND_MERGE_GAP_PX = 64  # 相邻内容块间隔 ≤ 此值则并成一条（拆开不值当）


def _bands_enabled() -> bool:
    return os.environ.get("KROK_SUBTITLE_RENDER_BANDS", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


def _merge_intervals(intervals: list[tuple[int, int]], gap: int) -> list[tuple[int, int]]:
    """把 ``(top, bottom)`` 区间按间隔 ``gap`` 合并；返回排序后的不相交区间。"""
    if not intervals:
        return []
    ordered = sorted(intervals)
    merged = [ordered[0]]
    for top, bottom in ordered[1:]:
        last_top, last_bottom = merged[-1]
        if top - last_bottom <= gap:
            merged[-1] = (last_top, max(last_bottom, bottom))
        else:
            merged.append((top, bottom))
    return merged


def _packed_offsets(bands: list[tuple[int, int]]) -> list[int]:
    """各 band 在打包缓冲里的纵向起点（高度累加）。"""
    offsets: list[int] = []
    cursor = 0
    for _top, height in bands:
        offsets.append(cursor)
        cursor += height
    return offsets


def _compute_content_bands(
    job: RenderJob,
    duration_ms: int,
    *,
    should_cancel: Callable[[], bool] | None = None,
) -> list[tuple[int, int]] | None:
    """预扫整段，求互不相交的内容块 → 多条 ``(y_top, height)``（方案 B）。

    用 :func:`frame_content_intervals` 拿每帧的逐源（歌词+信号 / 标题）分段区间，
    跨帧汇总后按 ``_BAND_MERGE_GAP_PX`` 合并成不相交带；任一帧落在未迁移路径
    （竖排 / viewport 旋转 / 逐字 transition）→ 返回 ``None`` 让上层退回方案 A / 整帧。
    少于 2 条或打包高度省不下来时也返回 ``None``（交给方案 A 单条）。
    """
    width, height = job.width, job.height
    total_frames = _frame_count(duration_ms, job.fps)
    times = _strip_sample_times(_job_tracks(job), job.style, duration_ms, total_frames)
    if not times:
        return None

    extras = list(job.extra_tracks)
    collected: list[tuple[int, int]] = []
    for t_ms in times:
        if should_cancel is not None and should_cancel():
            return None
        if not frame_has_content(job.track, t_ms, job.style, extras):
            continue
        intervals = frame_content_intervals(width, height, job.track, t_ms, job.style, extras)
        if intervals is None:
            return None  # 未迁移路径，方案 B 无法保证不漏像素
        collected.extend(intervals)
    if not collected:
        return None

    merged = _merge_intervals(collected, _BAND_MERGE_GAP_PX)
    padded = [
        (max(0, top - _STRIP_MARGIN_PX), min(height - 1, bottom + _STRIP_MARGIN_PX))
        for top, bottom in merged
    ]
    remerged = _merge_intervals(padded, 0)  # 加边后可能首尾相接，再并一次
    bands: list[tuple[int, int]] = []
    for top, bottom in remerged:
        top -= top % 2  # 下取偶
        band_h = bottom - top + 1
        if band_h % 2:
            band_h += 1
        band_h = min(band_h, height - top)
        bands.append((top, band_h))

    if len(bands) < 2:
        return None  # 单块 → 方案 A 单条更简单
    packed_h = sum(band_h for _top, band_h in bands)
    if packed_h >= height * _STRIP_MIN_GAIN_RATIO:
        return None  # 打包后省不了多少，退回方案 A / 整帧
    return bands


def _paint_overlay_bands(
    buffer: QImage,
    track: TimingTrack,
    style: Style,
    t_ms: int,
    *,
    logical_w: int,
    logical_h: int,
    bands: list[tuple[int, int]],
    transparent: QColor,
    extra_tracks: tuple[TimingTrack, ...] = (),
) -> None:
    """把整帧字幕布局画进竖向打包的 ``buffer``（高 = 各 band 高之和）。

    每条 band 占 buffer 里 ``[packed_off, packed_off + height)``，画笔裁到该槽位、
    上移 ``band_top - packed_off``，于是只有该 band 的原始行落进对应槽位。
    """
    buffer.fill(transparent)
    offsets = _packed_offsets(bands)
    painter = QPainter(buffer)
    try:
        for (band_top, band_h), packed_off in zip(bands, offsets):
            painter.save()
            try:
                painter.setClipRect(0, packed_off, logical_w, band_h)
                painter.translate(0, packed_off - band_top)
                paint_frame_to_painter(
                    painter, logical_w, logical_h, track, t_ms, style, list(extra_tracks)
                )
            finally:
                painter.restore()
    finally:
        painter.end()


def _frame_bytes_bands(
    job: RenderJob,
    t_ms: int,
    bands: list[tuple[int, int]],
    transparent: QColor,
    buffer: QImage,
    empty_frame: bytes,
) -> bytes:
    """渲染一帧为打包 RGBA 字节：有内容画进复用 ``buffer``，否则返回预存全透明帧。"""
    if frame_has_content(job.track, t_ms, job.style, list(job.extra_tracks)):
        _paint_overlay_bands(
            buffer, job.track, job.style, t_ms,
            logical_w=job.width, logical_h=job.height,
            bands=bands, transparent=transparent,
            extra_tracks=job.extra_tracks,
        )
        return _image_bytes(buffer)
    return empty_frame


# ---------------------------------------------------------------------------
# 帧写出：单进程 / 多进程（A3）
# ---------------------------------------------------------------------------


def _frame_bytes(
    job: RenderJob,
    t_ms: int,
    strip_top: int,
    transparent: QColor,
    buffer: QImage,
    empty_frame: bytes,
) -> bytes:
    """渲染一帧为 RGBA 字节：有内容则画进（复用的）``buffer``，否则返回预存全透明帧。"""
    if frame_has_content(job.track, t_ms, job.style, list(job.extra_tracks)):
        _paint_overlay_strip(
            buffer, job.track, job.style, t_ms,
            logical_w=job.width, logical_h=job.height,
            strip_top=strip_top, transparent=transparent,
            extra_tracks=job.extra_tracks,
        )
        return _image_bytes(buffer)
    return empty_frame


def _write_frames_native(
    process: subprocess.Popen,
    job: RenderJob,
    total_frames: int,
    renderer_path: Path,
    should_cancel: Callable[[], bool] | None,
    on_progress: Callable[[int, int], None] | None,
) -> None:
    """Write full-frame RGBA frames rendered by the native sidecar."""
    written = 0
    for frame in iter_native_rgba_frames(
        job.track,
        job.style,
        width=job.width,
        height=job.height,
        fps=job.fps,
        total_frames=total_frames,
        renderer_path=renderer_path,
        should_cancel=should_cancel,
    ):
        if should_cancel is not None and should_cancel():
            raise ExportCancelled("已停止导出。")
        process.stdin.write(frame)
        written += 1
        if on_progress is not None:
            on_progress(written, total_frames)


def _write_frames_single(
    process: subprocess.Popen,
    job: RenderJob,
    strip_top: int,
    render_h: int,
    total_frames: int,
    should_cancel: Callable[[], bool] | None,
    on_progress: Callable[[int, int], None] | None,
) -> None:
    """单进程逐帧渲染并按序写入 ffmpeg stdin。"""
    buffer = QImage(job.width, render_h, QImage.Format.Format_RGBA8888)
    transparent = QColor(0, 0, 0, 0)
    empty_frame = bytes(job.width * render_h * 4)
    for index in range(total_frames):
        if should_cancel is not None and should_cancel():
            terminate_process(process)
            raise ExportCancelled("已停止导出。")
        t_ms = int(round(index * 1000 / job.fps))
        process.stdin.write(_frame_bytes(job, t_ms, strip_top, transparent, buffer, empty_frame))
        if on_progress is not None:
            on_progress(index + 1, total_frames)


def _resolve_worker_count(total_frames: int) -> int:
    """决定导出 worker 进程数：env 优先，否则 CPU 核数（封顶）；帧数太少退回单进程。"""
    env = os.environ.get("KROK_SUBTITLE_RENDER_WORKERS")
    if env is not None and env.strip():
        try:
            count = int(env)
        except ValueError:
            count = 1
    else:
        count = os.cpu_count() or 1
    count = max(1, min(count, _MULTIPROC_WORKER_CAP))
    if total_frames < _MULTIPROC_MIN_FRAMES:
        return 1
    return count


def _resolve_chunk_size(job: RenderJob, render_h: int, total_frames: int, worker_count: int) -> int:
    """每个 worker 任务的帧数：按目标字节封顶（控内存/IPC），且每 worker 至少几块以均衡。"""
    frame_bytes = max(job.width * render_h * 4, 1)
    by_bytes = max(1, _CHUNK_TARGET_BYTES // frame_bytes)
    by_balance = max(1, total_frames // (worker_count * 4))
    return max(1, min(by_bytes, by_balance))


# worker 进程内的渲染上下文（spawn 后由 _render_worker_init 一次性建立）。
_W_CTX: dict = {}


def _render_worker_init(job: RenderJob, strip_top: int, render_h: int) -> None:
    """worker 初始化：建本进程 QApplication（继承父 QT_QPA_PLATFORM，字体一致）+ 复用缓冲。"""
    from PyQt6.QtWidgets import QApplication

    _W_CTX["app"] = QApplication.instance() or QApplication([])
    _W_CTX["job"] = job
    _W_CTX["strip_top"] = strip_top
    _W_CTX["buffer"] = QImage(job.width, render_h, QImage.Format.Format_RGBA8888)
    _W_CTX["transparent"] = QColor(0, 0, 0, 0)
    _W_CTX["empty_frame"] = bytes(job.width * render_h * 4)


def _render_worker_chunk(task: tuple[int, int]) -> bytes:
    """渲染连续一段帧 ``[start, start+count)`` 为拼接的 RGBA 字节。"""
    start, count = task
    job = _W_CTX["job"]
    strip_top = _W_CTX["strip_top"]
    buffer = _W_CTX["buffer"]
    transparent = _W_CTX["transparent"]
    empty_frame = _W_CTX["empty_frame"]
    out = bytearray()
    for index in range(start, start + count):
        t_ms = int(round(index * 1000 / job.fps))
        out += _frame_bytes(job, t_ms, strip_top, transparent, buffer, empty_frame)
    return bytes(out)


def _write_frames_multiprocess(
    process: subprocess.Popen,
    job: RenderJob,
    strip_top: int,
    render_h: int,
    total_frames: int,
    worker_count: int,
    should_cancel: Callable[[], bool] | None,
    on_progress: Callable[[int, int], None] | None,
) -> None:
    """多进程并行渲染：worker 池各渲一段，主进程用 imap 按序收回并写入 ffmpeg stdin。

    imap 保序，慢块会让后续完成块在结果队列里短暂积压；chunk 按 _CHUNK_TARGET_BYTES
    封顶以控内存。取消 / 异常时 terminate 整池。
    """
    import multiprocessing as mp

    chunk = _resolve_chunk_size(job, render_h, total_frames, worker_count)
    tasks = [(start, min(chunk, total_frames - start)) for start in range(0, total_frames, chunk)]
    ctx = mp.get_context("spawn")
    pool = ctx.Pool(
        worker_count,
        initializer=_render_worker_init,
        initargs=(job, strip_top, render_h),
    )
    written = 0
    try:
        for (_start, count), blob in zip(tasks, pool.imap(_render_worker_chunk, tasks)):
            if should_cancel is not None and should_cancel():
                terminate_process(process)
                raise ExportCancelled("已停止导出。")
            process.stdin.write(blob)
            written += count
            if on_progress is not None:
                on_progress(written, total_frames)
    finally:
        # 无论正常完成 / 取消 / 异常，都强制收掉 workers（imap 可能仍在后台渲染）。
        pool.terminate()
        pool.join()


def _write_frames_single_bands(
    process: subprocess.Popen,
    job: RenderJob,
    bands: list[tuple[int, int]],
    packed_h: int,
    total_frames: int,
    should_cancel: Callable[[], bool] | None,
    on_progress: Callable[[int, int], None] | None,
) -> None:
    """单进程逐帧渲染（方案 B 打包多带）并按序写入 ffmpeg stdin。"""
    buffer = QImage(job.width, packed_h, QImage.Format.Format_RGBA8888)
    transparent = QColor(0, 0, 0, 0)
    empty_frame = bytes(job.width * packed_h * 4)
    for index in range(total_frames):
        if should_cancel is not None and should_cancel():
            terminate_process(process)
            raise ExportCancelled("已停止导出。")
        t_ms = int(round(index * 1000 / job.fps))
        process.stdin.write(
            _frame_bytes_bands(job, t_ms, bands, transparent, buffer, empty_frame)
        )
        if on_progress is not None:
            on_progress(index + 1, total_frames)


# worker 进程内的多带渲染上下文（spawn 后由 _render_worker_init_bands 建立）。
_WB_CTX: dict = {}


def _render_worker_init_bands(
    job: RenderJob, bands: list[tuple[int, int]], packed_h: int
) -> None:
    from PyQt6.QtWidgets import QApplication

    _WB_CTX["app"] = QApplication.instance() or QApplication([])
    _WB_CTX["job"] = job
    _WB_CTX["bands"] = bands
    _WB_CTX["buffer"] = QImage(job.width, packed_h, QImage.Format.Format_RGBA8888)
    _WB_CTX["transparent"] = QColor(0, 0, 0, 0)
    _WB_CTX["empty_frame"] = bytes(job.width * packed_h * 4)


def _render_worker_chunk_bands(task: tuple[int, int]) -> bytes:
    start, count = task
    job = _WB_CTX["job"]
    bands = _WB_CTX["bands"]
    buffer = _WB_CTX["buffer"]
    transparent = _WB_CTX["transparent"]
    empty_frame = _WB_CTX["empty_frame"]
    out = bytearray()
    for index in range(start, start + count):
        t_ms = int(round(index * 1000 / job.fps))
        out += _frame_bytes_bands(job, t_ms, bands, transparent, buffer, empty_frame)
    return bytes(out)


def _write_frames_multiprocess_bands(
    process: subprocess.Popen,
    job: RenderJob,
    bands: list[tuple[int, int]],
    packed_h: int,
    total_frames: int,
    worker_count: int,
    should_cancel: Callable[[], bool] | None,
    on_progress: Callable[[int, int], None] | None,
) -> None:
    """多进程并行渲染（方案 B 打包多带），主进程用 imap 按序收回写入 ffmpeg stdin。"""
    import multiprocessing as mp

    chunk = _resolve_chunk_size(job, packed_h, total_frames, worker_count)
    tasks = [(start, min(chunk, total_frames - start)) for start in range(0, total_frames, chunk)]
    ctx = mp.get_context("spawn")
    pool = ctx.Pool(
        worker_count,
        initializer=_render_worker_init_bands,
        initargs=(job, bands, packed_h),
    )
    written = 0
    try:
        for (_start, count), blob in zip(tasks, pool.imap(_render_worker_chunk_bands, tasks)):
            if should_cancel is not None and should_cancel():
                terminate_process(process)
                raise ExportCancelled("已停止导出。")
            process.stdin.write(blob)
            written += count
            if on_progress is not None:
                on_progress(written, total_frames)
    finally:
        pool.terminate()
        pool.join()


def _render_overlay_frame(
    track: TimingTrack,
    style: Style,
    t_ms: int,
    width: int,
    height: int,
) -> bytes:
    image = QImage(width, height, QImage.Format.Format_RGBA8888)
    image.fill(QColor(0, 0, 0, 0))
    paint_frame(image, track, t_ms, style)
    return _image_bytes(image)


def _drain_process_output(process: subprocess.Popen, logger: Logger) -> None:
    if process.stdout is None:
        return
    for raw in process.stdout:
        if isinstance(raw, bytes):
            line = raw.decode("utf-8", errors="replace").strip()
        else:
            line = str(raw).strip()
        if line:
            logger(line)


def _remove_incomplete_output(output_path: Path, logger: Logger) -> None:
    if not output_path.exists():
        return
    try:
        output_path.unlink()
        logger(f"已清理未完成的输出文件: {output_path}")
    except OSError as exc:
        logger(f"清理未完成的输出文件失败: {output_path} ({exc})")
