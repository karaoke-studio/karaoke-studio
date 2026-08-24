"""ffmpeg rawvideo pipe renderer for subtitle videos.

A8 MVP renders a transparent subtitle overlay with QPainter and lets ffmpeg
compose it over the background video.  Audio is copied from the background video
when present.
"""

from __future__ import annotations

import csv
import json
import math
import os
import platform
import statistics
import subprocess
import threading
import time
import uuid
from collections import deque
from dataclasses import replace
from pathlib import Path
from typing import Callable

import numpy as np
from PyQt6.QtGui import QColor, QImage, QPainter

from krok_helper.errors import ExportCancelled, ProcessingError
from krok_helper.ffmpeg import _build_subprocess_kwargs, find_tool, terminate_process
from krok_helper.subtitle_render.background import BackgroundSource
from krok_helper.subtitle_render.engine.encoder_select import (
    resolved_encoder_label,
    video_encoder_options,
)
from krok_helper.subtitle_render.engine.native_export import (
    gpu_export_packed_enabled,
    iter_gpu_rgba_frames,
    iter_native_rgba_frames,
)
from krok_helper.subtitle_render.engine.render_job import RenderJob
from krok_helper.subtitle_render.engine.render_job_policy import (
    job_tracks as _job_tracks,
    resolve_duration_ms as _resolve_duration_ms,
    resolved_background as _resolved_background,
    validate_render_job as _validate_job,
)
from krok_helper.subtitle_render.engine.render_bands import (
    merge_intervals as _merge_intervals,
    packed_offsets as _packed_offsets,
)
from krok_helper.subtitle_render.engine.animator import max_line_animation_excursion
from krok_helper.subtitle_render.engine.painter import (
    frame_content_intervals,
    frame_vertical_bounds,
    frame_has_content,
    paint_frame,
    paint_frame_to_painter,
)
from krok_helper.subtitle_render.guide_symbols import guide_symbol_path
from krok_helper.subtitle_render.timing import (
    TimingTrack,
    guide_symbol_role_labels,
    timing_line_start_ms,
)
from krok_helper.subtitle_render.models import (
    TITLE_SCHEME_NAME,
    Style,
    style_with_line_animation,
)
from krok_helper.subtitle_render.native_backend import NativeRendererError, resolve_native_renderer_path
from krok_helper.subtitle_render.native_protocol import (
    gpu_unsupported_feature_labels,
    gpu_unsupported_features,
)

# A2 条带渲染：只把字幕所在窄条喂给 ffmpeg pipe，省每帧 8MB 拷贝 / pipe 带宽。
# 条带 = 整段渲染里所有可见内容纵向范围的并集（单条覆盖，方案 A）。可用环境变量
# KROK_SUBTITLE_RENDER_STRIP=0 关闭退回整帧。
_STRIP_MARGIN_PX = 8  # 基础安全边（1080p 基准，随输出高度等比放大）
_STRIP_MIN_GAIN_RATIO = 0.85  # 并集 ≥ 全高的此比例则不值当，退回整帧
_STRIP_MAX_SAMPLES = 200  # 纵向并集预扫的最大采样帧数


def _referenced_style_sources(style: Style, tracks: list[TimingTrack]) -> list[object]:
    """聚合当前工程实际生效的字号来源：全局样式 + 被引用的方案。

    歌手方案只保留 tracks 里出现的 ``singer_id``；行内配色只保留任一字符
    ``role_label`` 引用到的名字（标签跨行延续时，延续源头的字符必带标签）；
    标题方案只在 ``title_overlay.enabled`` 时计入。N3 导入或编辑遗留的
    未使用超大字号方案不应把安全边撑到数千像素、让整个 4K 导出退回全帧。
    """
    sources: list[object] = [style]
    used_singers = {
        line.singer_id
        for track in tracks
        for line in track.lines
        if getattr(line, "singer_id", None) is not None
    }
    for singer_id, scheme in (getattr(style, "singer_style_overrides", None) or {}).items():
        if singer_id in used_singers:
            sources.append(scheme)
    used_roles: set[str] = set()
    for track in tracks:
        for line in track.lines:
            for char in line.chars:
                if getattr(char, "role_label", None):
                    used_roles.add(char.role_label)
            # 行首导唱符自带角色标签（Painter 写入虚拟字符并按对应方案字号
            # 绘制）；行内导唱符沿用被替换字符的标签，由上面的扫描覆盖。
            if line.guide_symbol is not None:
                for label in guide_symbol_role_labels(line.guide_symbol):
                    if label:
                        used_roles.add(label)
    title = getattr(style, "title_overlay", None)
    title_active = title is not None and bool(getattr(title, "enabled", False))
    for name, scheme in (getattr(style, "custom_style_schemes", None) or {}).items():
        if name in used_roles or (title_active and name == TITLE_SCHEME_NAME):
            sources.append(scheme)
    return sources


def _max_project_font_size(style: Style, tracks: list[TimingTrack]) -> float:
    """工程内实际可能出现的最大主/拉丁/注音字号（像素）。

    全局样式自身的 ``latin_font_size_px`` / ``ruby_font_size_px`` /
    ``ruby_latin_font_size_px``，以及被引用方案（歌手 / 行内配色 / 标题）的
    同名字段，都可把字形放大到远超全局主字号；utopia 放大 / rise 行程按
    字形尺寸缩放，安全边必须按全项目最大字号估算，否则大字号字符会被裁。
    """
    sizes: list[float] = []
    for source in _referenced_style_sources(style, tracks):
        for field_name in (
            "font_size_px",
            "latin_font_size_px",
            "ruby_font_size_px",
            "ruby_latin_font_size_px",
        ):
            value = getattr(source, field_name, None)
            if value:
                sizes.append(float(value))
    return max(sizes) if sizes else 0.0


def _max_guide_span_em(tracks: list[TimingTrack]) -> float:
    """行内矢量导唱符的最大 utopia 旋转包络（em 单位）。

    位图导唱符走独立渲染路径（不进 utopia 旋转）；矢量导唱符是行内虚拟
    字符，会进入 utopia 旋转/缩放。包络按「旋转枢轴到路径四角的最大距离
    ×2」估算：枢轴取 advance box 水平中心（``advance_width/2``，SVG 可远宽
    于轮廓本身，枢轴距离主导）、竖直方向按基线保守估计（轮廓可整体悬离
    基线，位置主导）——项目反序列化对 advance 与路径坐标均无上限。
    """
    span_em = 0.0
    seen: set[int] = set()
    for track in tracks:
        for line in track.lines:
            guides = [line.guide_symbol, *(line.inline_guide_symbols or {}).values()]
            for guide in guides:
                if guide is None or id(guide) in seen:
                    continue
                seen.add(id(guide))
                if getattr(guide, "kind", "vector") != "vector":
                    continue
                if not getattr(guide, "path_commands", None):
                    continue
                rect = guide_symbol_path(guide).boundingRect()
                if rect.isEmpty():
                    continue
                units = max(int(getattr(guide, "units_per_em", 1) or 1), 1)
                advance_em = max(
                    float(getattr(guide, "advance_width", 0.0) or 0.0) / units, 0.0
                )
                pivot_x = advance_em / 2.0
                radius_em = max(
                    math.hypot(x / units - pivot_x, y / units)
                    for x in (rect.left(), rect.right())
                    for y in (rect.top(), rect.bottom())
                )
                span_em = max(span_em, 2.0 * radius_em)
    return span_em


def _strip_safety_margin(job: RenderJob) -> int | None:
    """条带/多带的纵向安全边：基础边随分辨率缩放 + 动画纵向行程上界。

    预扫并集按采样时刻取值，动画峰值（utopia 弹跳、rise 抬升等）可能落在
    采样间隙之间；把行程上界并进安全边后，条带对任意帧都不会裁掉可见像素
    （越出画布的部分在整帧路径同样被画布裁掉，因此 clamp 到画布即无损）。
    行级动画覆盖（N3 逐行动画）逐行解析后取最大值；字号按全项目最大
    （含被引用的角色方案 / 行内配色 / 注音）估算；utopia 的字形放大量
    额外计入矢量导唱符的旋转对角线跨度（SVG 轮廓可远宽于 1em）。

    返回 ``None`` 表示存在无可靠纵向上界的动画（char_drip / spin_flip 的
    逐字剪切随首帧 ``tan`` 发散，且行内混合字号使字形宽度不可由样式字号
    约束），条带/多带优化必须禁用、退回整帧渲染。
    """
    margin = max(_STRIP_MARGIN_PX, _STRIP_MARGIN_PX * job.height / 1080.0)
    tracks = _job_tracks(job)
    font_px = _max_project_font_size(job.style, tracks)
    glyph_span_em = max(1.5, _max_guide_span_em(tracks) * 1.3)  # 1.3 = utopia intro 放大
    styles: list[Style] = [job.style]
    styles.extend(
        style_with_line_animation(job.style, line)
        for track in tracks
        for line in track.lines
    )
    for candidate in styles:
        excursion = max_line_animation_excursion(
            candidate, job.height, font_size_px=font_px, glyph_span_em=glyph_span_em
        )
        if excursion is None:
            return None
        margin = max(margin, excursion)
    return int(math.ceil(margin))

# A3 多进程导出：offscreen worker 池并行渲帧，主进程按序喂 ffmpeg。
# KROK_SUBTITLE_RENDER_WORKERS=N 指定自动模式进程数（1=关闭，走单进程）。worker
# 不强制 offscreen——继承父进程 QT_QPA_PLATFORM，保证字体与预览/单进程一致。
_MULTIPROC_AUTO_WORKER_CAP = 8  # 自动模式保持保守上限
_MULTIPROC_WORKER_CAP = 16  # 手动 / 环境变量上限（每个 worker 一份 QApplication）
_MULTIPROC_MIN_FRAMES = 240  # 帧数低于此不值当 spawn，走单进程
_CHUNK_TARGET_BYTES = 64 * 1024 * 1024  # 单个 chunk 目标字节上限（控内存 / IPC 粒度）
_CHUNK_MIN_TARGET_BYTES = 4 * 1024 * 1024  # 单个 chunk 目标字节下限（避免 IPC 过碎）

# 导出预览（仿 N3 出力预览）：把 ffmpeg 内合成后的成品流 split 一路，按视频时间
# 降频 + 缩宽后持续覆盖写入单张 JPG（image2 -update，原子写），UI 轮询该文件即可
# 边导出边看画面。fps=2 → 60fps 输出下约每 30 帧刷新一次。
_PREVIEW_FPS = 2
_PREVIEW_WIDTH = 640
_PREVIEW_MIN_WIDTH = 320
from krok_helper.types import Logger


def _resolved_preview_width(output_width: int, requested_width: int | None) -> int:
    requested = _PREVIEW_WIDTH if requested_width is None else int(requested_width)
    return min(max(int(output_width), 1), max(_PREVIEW_MIN_WIDTH, requested))


def render_subtitle_video(
    job: RenderJob,
    *,
    ffmpeg_dir: Path | None = None,
    logger: Logger | None = None,
    should_cancel: Callable[[], bool] | None = None,
    on_process_started: Callable[[subprocess.Popen | None], None] | None = None,
    on_progress: Callable[[int, int], None] | None = None,
    preview_image_path: Path | None = None,
    preview_width: int | None = None,
) -> Path:
    """Render ``job`` to MP4 using a transparent rawvideo subtitle pipe."""
    logger = logger or (lambda _message: None)
    _validate_job(job)
    ffmpeg_path = find_tool("ffmpeg.exe", ffmpeg_dir)

    duration_ms = _resolve_duration_ms(job)
    # Freeze the resolved project/media duration into the job so every CPU,
    # multiprocessing and GPU path anchors title tail timing to the same clock.
    job = replace(job, duration_ms=duration_ms)
    total_frames = _frame_count(duration_ms, job.fps)
    native_export_requested = _native_export_requested(job)
    native_renderer_path = resolve_native_renderer_path() if native_export_requested else None
    native_export_active = native_renderer_path is not None
    if native_export_requested and native_renderer_path is None:
        logger("native 导出 sidecar 未找到，已回退到 Python 渲染器")
    gpu_export_requested = _gpu_export_requested(job)
    gpu_renderer_path = resolve_native_renderer_path() if gpu_export_requested else None
    gpu_fallback_reasons = (
        gpu_unsupported_features(job.track, job.style, list(job.extra_tracks))
        if gpu_export_requested
        else ()
    )
    gpu_export_active = (
        gpu_export_requested
        and gpu_renderer_path is not None
        and not gpu_fallback_reasons
    )
    gpu_packed_active = gpu_export_active and gpu_export_packed_enabled()
    if gpu_export_requested and gpu_renderer_path is None:
        logger("GPU 字幕渲染器未找到，已回退到 Painter 导出")
    elif gpu_fallback_reasons:
        logger(
            "当前工程包含 GPU 尚不识别的功能，已回退到 Painter 导出："
            + ", ".join(gpu_unsupported_feature_labels(gpu_fallback_reasons))
        )

    # A2：预扫字幕纵向范围只渲染窄条（取消 / 关闭 / 无收益时退回整帧）。
    # 优先方案 B（多条分离带），不适用时退回方案 A（单条并集）。
    cancelled = should_cancel is not None and should_cancel()
    strip: tuple[int, int] | None = None
    bands: list[tuple[int, int]] | None = None
    if (
        _strip_enabled()
        and not cancelled
        and not native_export_active
        and (not gpu_export_active or gpu_packed_active)
    ):
        if _bands_enabled():
            bands = _compute_content_bands(
                job, duration_ms, should_cancel=should_cancel, logger=logger
            )
        if bands is None:
            strip = _compute_subtitle_strip(
                job, duration_ms, should_cancel=should_cancel, logger=logger
            )

    if bands is not None:
        packed_h = sum(height for _top, height in bands)
        logger(f"多带渲染: {len(bands)} 条 打包高={packed_h}（全高 {job.height}）{bands}")
        command = build_render_command(
            ffmpeg_path, job, duration_ms=duration_ms, bands=bands,
            preview_image_path=preview_image_path,
            preview_width=preview_width,
        )
    else:
        strip_top, render_h = strip if strip is not None else (0, job.height)
        if strip is not None:
            logger(f"条带渲染: y={strip_top} 高={render_h}（全高 {job.height}）")
        command = build_render_command(
            ffmpeg_path, job, duration_ms=duration_ms, strip=strip,
            preview_image_path=preview_image_path,
            preview_width=preview_width,
        )

    job.output_path.parent.mkdir(parents=True, exist_ok=True)
    logger(f"导出字幕视频: {job.output_path.name}")
    logger(
        f"输出参数: {job.width}x{job.height} / {job.fps}fps / "
        f"{duration_ms / 1000:.3f}s / {resolved_encoder_label(ffmpeg_path, job.encoder_mode, job.codec)} / CRF {job.crf}"
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

    # ffmpeg can emit recurring decoder warnings while it is still consuming
    # rawvideo from stdin.  Drain its merged stdout/stderr concurrently;
    # otherwise the small Windows pipe buffer can fill and deadlock both sides
    # before all subtitle frames have been written.
    ffmpeg_output_tail: deque[str] = deque(maxlen=80)
    output_drain_thread = threading.Thread(
        target=_drain_process_output,
        args=(process, logger, ffmpeg_output_tail),
        name="subtitle-ffmpeg-output",
        daemon=True,
    )
    output_drain_thread.start()

    return_code: int | None = None
    amf_pipe_failure = False
    try:
        assert process.stdin is not None
        # A3：帧数够多时多进程并行渲染（offscreen worker 池），主进程按序喂 ffmpeg；
        # 否则走单进程。两条路径逐帧逻辑一致（A4 缓冲复用 + 空帧短路 + A2 条带/多带）。
        worker_count = _resolve_worker_count(total_frames, job.render_workers)
        if gpu_export_active:
            logger(f"GPU 字幕导出: {gpu_renderer_path}（Direct2D 条带回读）")
            _write_frames_gpu(
                process,
                job,
                total_frames,
                gpu_renderer_path,
                should_cancel,
                on_progress,
                logger,
                strip if gpu_packed_active else None,
                bands if gpu_packed_active else None,
            )
        elif native_export_active:
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
                    worker_count, should_cancel, on_progress, logger=logger,
                )
            else:
                _write_frames_multiprocess(
                    process, job, strip_top, render_h, total_frames,
                    worker_count, should_cancel, on_progress, logger=logger,
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
        return_code = process.wait()
        output_drain_thread.join()
    except ExportCancelled:
        terminate_process(process)
        _remove_incomplete_output(job.output_path, logger)
        raise
    except ProcessingError:
        # 帧生产阶段的停滞 / worker 异常退出（如 _drain_pending_head）也要走完整
        # 清理：终止 ffmpeg、删半成品，否则 ffmpeg 会一直等 stdin 并锁住输出文件。
        terminate_process(process)
        _remove_incomplete_output(job.output_path, logger)
        raise
    except NativeRendererError as exc:
        terminate_process(process)
        _remove_incomplete_output(job.output_path, logger)
        if gpu_export_active and not (should_cancel is not None and should_cancel()):
            logger(
                "GPU 字幕导出失败，已自动回退到 CPU Painter 从头渲染"
                "（进度会重新从 0 开始计数，导出仍会正常完成），原因："
                f"{exc}"
            )
            return render_subtitle_video(
                replace(job, gpu_export_enabled=False),
                ffmpeg_dir=ffmpeg_dir,
                logger=logger,
                should_cancel=should_cancel,
                on_process_started=on_process_started,
                on_progress=on_progress,
                preview_image_path=preview_image_path,
                preview_width=preview_width,
            )
        raise ProcessingError(f"native 字幕渲染器导出失败: {exc}") from exc
    except (BrokenPipeError, OSError) as exc:
        terminate_process(process)
        _remove_incomplete_output(job.output_path, logger)
        if should_cancel is not None and should_cancel():
            raise ExportCancelled("已停止导出。") from exc
        # AMF 初始化失败通常在第一批 rawvideo 写入时就关闭管道，
        # 比最终 return_code 更早到达。等日志线程读完后判断；
        # CPU 重试放在 finally 之后，避免新旧 ffmpeg 进程重叠。
        output_drain_thread.join(timeout=1.0)
        if _should_retry_amf_with_cpu(command, ffmpeg_output_tail):
            amf_pipe_failure = True
        else:
            raise ProcessingError(f"ffmpeg 管道写入失败: {exc}") from exc
    except Exception:
        # 其余帧生产异常（如进度回调抛 RuntimeError / Qt 对象竞态）同样不得
        # 泄漏 ffmpeg：终止进程、删半成品后原样上抛。放在各具体分支之后，
        # 不影响 ExportCancelled / NativeRendererError（GPU→CPU 回退）等既有路径。
        terminate_process(process)
        _remove_incomplete_output(job.output_path, logger)
        raise
    finally:
        if process.poll() is not None and output_drain_thread.is_alive():
            output_drain_thread.join(timeout=1.0)
        if on_process_started is not None:
            on_process_started(None)

    if should_cancel is not None and should_cancel():
        _remove_incomplete_output(job.output_path, logger)
        raise ExportCancelled("已停止导出。")
    retry_amf_with_cpu = amf_pipe_failure or (
        return_code is not None
        and return_code != 0
        and _should_retry_amf_with_cpu(command, ffmpeg_output_tail)
    )
    if retry_amf_with_cpu:
        _remove_incomplete_output(job.output_path, logger)
        logger(
            "AMD AMF 编码器初始化/显存失败，已自动切换 CPU 编码"
            "，从头重试（进度会重新从 0 开始计数）"
        )
        return render_subtitle_video(
            replace(job, encoder_mode="cpu"),
            ffmpeg_dir=ffmpeg_dir,
            logger=logger,
            should_cancel=should_cancel,
            on_process_started=on_process_started,
            on_progress=on_progress,
            preview_image_path=preview_image_path,
            preview_width=preview_width,
        )
    if return_code is None:
        raise ProcessingError("ffmpeg 管道异常关闭，未取得退出码")
    if return_code != 0:
        _remove_incomplete_output(job.output_path, logger)
        raise ProcessingError(f"ffmpeg 执行失败，退出码: {return_code}")
    if not job.output_path.is_file() or os.path.getsize(job.output_path) == 0:
        raise ProcessingError(f"导出失败，未生成有效文件: {job.output_path}")

    logger(f"字幕视频导出完成: {job.output_path}")
    return job.output_path


def _background_scale_chain(job: RenderJob, duration_seconds: float) -> str:
    """背景缩放链（输出 label 为 ``[bg]``），预览与导出严格同语义。

    - 视频背景固定 contain：等比缩小完整放入、不足处补纯黑边（与预览
      ``KeepAspectRatio`` + 纯黑底一致）。
    - 图片 / 图片序列按 ``image_fit``：``"cover"`` 等比放大铺满并居中裁掉
      超出（与预览 ``KeepAspectRatioByExpanding`` 一致）；``"contain"`` 同视频。
    - 纯色背景由 lavfi 直接生成目标尺寸，缩放链是无害的恒等变换。
    """
    source = _resolved_background(job)
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


def _bands_filter_graph(job: RenderJob, duration_seconds: float, bands: list[tuple[int, int]]) -> str:
    """方案 B 的 filter graph：打包输入 split → 各 band crop → 逐条 overlay 回原始 y。"""
    offsets = _packed_offsets(bands)
    n = len(bands)
    bg = _background_scale_chain(job, duration_seconds)
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
    preview_width: int | None = None,
) -> list[str]:
    """Build the ffmpeg command used by :func:`render_subtitle_video`.

    ``strip`` = ``(y_top, height)``：仅把该窄条作为 rawvideo 输入，``overlay=0:y_top``
    贴回全幅背景；``None`` 时整帧输入、``overlay=0:0``（原行为）。
    ``bands`` = 多条 ``(y_top, height)``（方案 B）：竖向打包成一条 pipe，split/crop/overlay
    还原到各自原始 y；给定时优先于 ``strip``。
    ``preview_image_path``：导出预览 —— 把合成后的 ``[v]`` 再 split 一路，降频缩宽后
    持续覆盖写入该 JPG（原子写），供 UI 边导出边轮询显示。``preview_width``
    指定该支路的目标物理像素宽度；省略时使用兼容默认值。
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
            _background_scale_chain(job, duration_seconds)
            + "[0:v:0]format=rgba,setpts=PTS-STARTPTS[ov];"
            + f"[bg][ov]overlay=0:{overlay_y}:format=auto[v]"
        )
    video_label = "[v]"
    if preview_image_path is not None:
        resolved_preview_width = _resolved_preview_width(job.width, preview_width)
        filter_graph += (
            ";[v]split=2[venc][vpin];"
            f"[vpin]fps={_PREVIEW_FPS},scale={resolved_preview_width}:-2[vprev]"
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
            codec=job.codec,
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
            "-q:v", "2",
            str(preview_image_path),
        ])
    return command


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


def _gpu_export_enabled() -> bool:
    default = "1" if os.name == "nt" else "0"
    return os.environ.get("KROK_SUBTITLE_GPU_EXPORT", default).strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _gpu_export_requested(job: RenderJob) -> bool:
    if os.name != "nt":
        return False
    if job.gpu_export_enabled is not None:
        return bool(job.gpu_export_enabled)
    return _gpu_export_enabled()


def _gpu_force_warp() -> bool:
    return os.environ.get("KROK_SUBTITLE_GPU_FORCE_WARP", "0").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _gpu_export_worker_count(*, force_warp: bool) -> int:
    if force_warp:
        return 1
    raw = os.environ.get("KROK_SUBTITLE_GPU_EXPORT_WORKERS")
    if raw is not None:
        try:
            return max(1, min(int(raw), 4))
        except ValueError:
            pass
    # GPU workers still need CPU threads to submit Direct2D work and consume
    # readbacks.  The worker pool itself supports eight threads, but the shared
    # frame transport has four slots, so export concurrency must stay within
    # four to prevent slot aliasing.  Avoid needless contention on low-core
    # systems.  The GPU
    # configure preflight applies the independent VRAM/budget limit afterward.
    logical_cpus = max(int(os.cpu_count() or 1), 1)
    return max(1, min(logical_cpus // 2, 4))


def _gpu_export_diagnostics_enabled() -> bool:
    return os.environ.get("KROK_SUBTITLE_GPU_EXPORT_DIAGNOSTICS", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


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
    duration_ms: int | None = None,
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
            painter,
            logical_w,
            logical_h,
            track,
            t_ms,
            style,
            list(extra_tracks or ()),
            duration_ms=duration_ms,
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
        start = timing_line_start_ms(line)
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
    logger: Logger | None = None,
) -> tuple[int, int] | None:
    """预扫整段，求所有可见字幕内容的纵向并集 ``(y_top, height)``。

    单条覆盖（方案 A）：有几行 / 几个来源 / 带不带信号注音，都由实际渲染像素的并集
    决定，不假设固定行数。并集 ≥ 全高 ``_STRIP_MIN_GAIN_RATIO`` 或全空时返回 ``None``
    退回整帧。yuv420p 友好：top 下取偶、height 上取偶。
    """
    margin = _strip_safety_margin(job)
    if margin is None:
        if logger is not None:
            logger(
                "检测到 char_drip / spin_flip 逐字剪切动画，纵向包络无可靠上界，"
                "条带渲染已禁用（改用整帧，剪切轨迹不会被裁）"
            )
        return None
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
        if not frame_has_content(
            job.track,
            t_ms,
            job.style,
            extras,
            duration_ms=job.duration_ms,
            logical_w=width,
            logical_h=height,
        ):
            continue
        bounds = frame_vertical_bounds(
            width,
            height,
            job.track,
            t_ms,
            job.style,
            extras,
            duration_ms=job.duration_ms,
        )
        if bounds is None:
            if scratch is None:
                scratch = QImage(width, height, QImage.Format.Format_RGBA8888)
            scratch.fill(transparent)
            paint_frame(
                scratch,
                job.track,
                t_ms,
                job.style,
                extras,
                duration_ms=job.duration_ms,
            )
            bounds = _content_row_bounds(scratch)
        if bounds is None:
            continue
        top = min(top, bounds[0])
        bottom = max(bottom, bounds[1])

    if bottom < top:
        return None  # 整段无可见内容

    top = max(0, top - margin)
    bottom = min(height - 1, bottom + margin)
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


def _compute_content_bands(
    job: RenderJob,
    duration_ms: int,
    *,
    should_cancel: Callable[[], bool] | None = None,
    logger: Logger | None = None,
) -> list[tuple[int, int]] | None:
    """预扫整段，求互不相交的内容块 → 多条 ``(y_top, height)``（方案 B）。

    用 :func:`frame_content_intervals` 拿每帧的逐源（歌词+信号 / 标题）分段区间，
    跨帧汇总后按 ``_BAND_MERGE_GAP_PX`` 合并成不相交带；任一帧落在未迁移路径
    （竖排 / viewport 旋转 / 逐字 transition）→ 返回 ``None`` 让上层退回方案 A / 整帧。
    少于 2 条或打包高度省不下来时也返回 ``None``（交给方案 A 单条）。
    """
    margin = _strip_safety_margin(job)
    if margin is None:
        if logger is not None:
            logger(
                "检测到 char_drip / spin_flip 逐字剪切动画，纵向包络无可靠上界，"
                "多带渲染已禁用（改用整帧，剪切轨迹不会被裁）"
            )
        return None
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
        if not frame_has_content(
            job.track,
            t_ms,
            job.style,
            extras,
            duration_ms=job.duration_ms,
            logical_w=width,
            logical_h=height,
        ):
            continue
        intervals = frame_content_intervals(
            width,
            height,
            job.track,
            t_ms,
            job.style,
            extras,
            duration_ms=job.duration_ms,
        )
        if intervals is None:
            return None  # 未迁移路径，方案 B 无法保证不漏像素
        collected.extend(intervals)
    if not collected:
        return None

    merged = _merge_intervals(collected, _BAND_MERGE_GAP_PX)
    padded = [
        (max(0, top - margin), min(height - 1, bottom + margin))
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
    duration_ms: int | None = None,
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
                    painter,
                    logical_w,
                    logical_h,
                    track,
                    t_ms,
                    style,
                    list(extra_tracks),
                    duration_ms=duration_ms,
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
    if frame_has_content(
        job.track,
        t_ms,
        job.style,
        list(job.extra_tracks),
        duration_ms=job.duration_ms,
        logical_w=job.width,
        logical_h=job.height,
    ):
        _paint_overlay_bands(
            buffer, job.track, job.style, t_ms,
            logical_w=job.width, logical_h=job.height,
            bands=bands, transparent=transparent,
            extra_tracks=job.extra_tracks,
            duration_ms=job.duration_ms,
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
    if frame_has_content(
        job.track,
        t_ms,
        job.style,
        list(job.extra_tracks),
        duration_ms=job.duration_ms,
        logical_w=job.width,
        logical_h=job.height,
    ):
        _paint_overlay_strip(
            buffer, job.track, job.style, t_ms,
            logical_w=job.width, logical_h=job.height,
            strip_top=strip_top, transparent=transparent,
            extra_tracks=job.extra_tracks,
            duration_ms=job.duration_ms,
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
        duration_ms=job.duration_ms,
        renderer_path=renderer_path,
        should_cancel=should_cancel,
    ):
        if should_cancel is not None and should_cancel():
            raise ExportCancelled("已停止导出。")
        process.stdin.write(frame)
        written += 1
        if on_progress is not None:
            on_progress(written, total_frames)


def _write_frames_gpu(
    process: subprocess.Popen,
    job: RenderJob,
    total_frames: int,
    renderer_path: Path,
    should_cancel: Callable[[], bool] | None,
    on_progress: Callable[[int, int], None] | None,
    logger: Logger,
    crop: tuple[int, int] | None = None,
    bands: list[tuple[int, int]] | None = None,
) -> None:
    """Write Direct2D band-readback frames to the existing ffmpeg pipe."""
    written = 0
    last_prepare_bucket = -1
    force_warp = _gpu_force_warp()
    packed_rgba = gpu_export_packed_enabled()
    worker_count = _gpu_export_worker_count(force_warp=force_warp)
    logger(
        f"GPU 字幕导出流水线: {worker_count} 个 worker"
        + ("（packed RGBA）" if packed_rgba else "")
    )
    diagnostics_enabled = _gpu_export_diagnostics_enabled()
    export_run_id = uuid.uuid4().hex
    export_started = time.perf_counter()
    ffmpeg_wait_seconds = 0.0
    gpu_diagnostics: dict[str, object] = {}
    frame_diagnostics_by_index: dict[int, dict[str, object]] = {}

    def on_prepare_progress(done: int, total: int) -> None:
        nonlocal last_prepare_bucket
        percent = 100 if total <= 0 else min(100, max(0, done * 100 // total))
        bucket = percent // 5
        if bucket == last_prepare_bucket:
            return
        last_prepare_bucket = bucket
        logger(f"正在准备 GPU 字幕资源… {done}/{total}（{percent}%）")

    def on_diagnostics(values: dict[str, object]) -> None:
        gpu_diagnostics.update(values)

    def on_frame_diagnostics(values: dict[str, object]) -> None:
        frame_diagnostics_by_index[int(values.get("frame_index", -1) or 0)] = dict(
            values
        )

    for frame in iter_gpu_rgba_frames(
        job.track,
        job.style,
        width=job.width,
        height=job.height,
        fps=job.fps,
        total_frames=total_frames,
        duration_ms=job.duration_ms,
        renderer_path=renderer_path,
        extra_tracks=list(job.extra_tracks),
        force_warp=force_warp,
        worker_count=worker_count,
        packed_rgba=packed_rgba,
        crop=crop,
        bands=bands,
        should_cancel=should_cancel,
        on_prepare_progress=on_prepare_progress,
        on_diagnostics=on_diagnostics if diagnostics_enabled else None,
        on_frame_diagnostics=on_frame_diagnostics if diagnostics_enabled else None,
        logger=logger,
    ):
        if should_cancel is not None and should_cancel():
            raise ExportCancelled("已停止导出。")
        write_started = time.perf_counter()
        process.stdin.write(frame)
        stdin_block_ms = (time.perf_counter() - write_started) * 1000.0
        ffmpeg_wait_seconds += stdin_block_ms / 1000.0
        if diagnostics_enabled and written in frame_diagnostics_by_index:
            frame_diagnostics_by_index[written]["stdin_block_ms"] = stdin_block_ms
        written += 1
        if on_progress is not None:
            on_progress(written, total_frames)
    if diagnostics_enabled:
        local_usage_mb = int(
            gpu_diagnostics.get("local_video_memory_usage_bytes", 0) or 0
        ) / (1024 * 1024)
        local_budget_mb = int(
            gpu_diagnostics.get("local_video_memory_budget_bytes", 0) or 0
        ) / (1024 * 1024)
        logger(
            "GPU 导出诊断: "
            f"ffmpeg 管道等待 {ffmpeg_wait_seconds:.3f}s；"
            f"本地显存 {local_usage_mb:.1f}/{local_budget_mb:.1f} MiB"
        )
        _persist_gpu_export_diagnostics(
            job,
            export_run_id=export_run_id,
            total_wall_ms=(time.perf_counter() - export_started) * 1000.0,
            worker_count=worker_count,
            force_warp=force_warp,
            gpu_diagnostics=gpu_diagnostics,
            frame_diagnostics=[
                frame_diagnostics_by_index[index]
                for index in sorted(frame_diagnostics_by_index)
            ],
            crop=crop,
            bands=bands,
            logger=logger,
        )


def _persist_gpu_export_diagnostics(
    job: RenderJob,
    *,
    export_run_id: str,
    total_wall_ms: float,
    worker_count: int,
    force_warp: bool,
    gpu_diagnostics: dict[str, object],
    frame_diagnostics: list[dict[str, object]],
    crop: tuple[int, int] | None,
    bands: list[tuple[int, int]] | None,
    logger: Logger,
) -> None:
    raw_dir = os.environ.get("KROK_SUBTITLE_GPU_EXPORT_DIAGNOSTICS_DIR", "").strip()
    if not raw_dir:
        return
    output_dir = Path(raw_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / f"{export_run_id}-frames.csv"
    json_path = output_dir / f"{export_run_id}-summary.json"
    if frame_diagnostics:
        fieldnames: list[str] = []
        for row in frame_diagnostics:
            for key in row:
                if key not in fieldnames:
                    fieldnames.append(key)
        with csv_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(frame_diagnostics)

    aggregates: dict[str, dict[str, float]] = {}
    numeric_fields = {
        key
        for row in frame_diagnostics
        for key, value in row.items()
        if isinstance(value, (int, float)) and key not in {"frame_index", "t_ms"}
    }
    for field in sorted(numeric_fields):
        values = [float(row[field]) for row in frame_diagnostics if field in row]
        if not values:
            continue
        ordered = sorted(values)
        p95_index = min(len(ordered) - 1, int(len(ordered) * 0.95))
        aggregates[field] = {
            "mean": statistics.fmean(values),
            "p50": statistics.median(values),
            "p95": ordered[p95_index],
            "min": min(values),
            "max": max(values),
        }
    summary = {
        "export_run_id": export_run_id,
        "git_commit": _git_commit_for_diagnostics(),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "width": job.width,
        "height": job.height,
        "fps": job.fps,
        "duration_ms": job.duration_ms,
        "encoder_mode": job.encoder_mode,
        "codec": job.codec,
        "preset": job.preset,
        "crf": job.crf,
        "background_kind": _resolved_background(job).kind,
        "preview_enabled": False,
        "worker_count": worker_count,
        "force_warp": force_warp,
        "crop": list(crop) if crop is not None else None,
        "bands": [list(band) for band in bands] if bands is not None else None,
        "frames": len(frame_diagnostics),
        "total_wall_ms": total_wall_ms,
        "gpu": gpu_diagnostics,
        "aggregates": aggregates,
        "frame_csv": str(csv_path),
    }
    json_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    logger(f"GPU 导出诊断已写入: {json_path}")


def _git_commit_for_diagnostics() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parents[3],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


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


def _resolve_worker_count(
    total_frames: int, requested_workers: int | None = None
) -> int:
    """Resolve manual, environment, or automatic worker count safely."""
    if requested_workers is not None:
        try:
            count = int(requested_workers)
        except (TypeError, ValueError):
            count = 1
        cap = _MULTIPROC_WORKER_CAP
    else:
        env = os.environ.get("KROK_SUBTITLE_RENDER_WORKERS")
        if env is not None and env.strip():
            try:
                count = int(env)
            except ValueError:
                count = 1
            cap = _MULTIPROC_WORKER_CAP
        else:
            count = os.cpu_count() or 1
            cap = _MULTIPROC_AUTO_WORKER_CAP
    count = max(1, min(count, cap))
    if total_frames < _MULTIPROC_MIN_FRAMES:
        return 1
    return count


def _resolve_chunk_size(job: RenderJob, render_h: int, total_frames: int, worker_count: int) -> int:
    """每个 worker 任务的帧数：按目标字节封顶（控内存/IPC），且每 worker 至少几块以均衡。

    chunk 目标随 worker 数缩放：``在飞内存上限 / (worker+2)``，夹在 4~64MiB——
    固定 64MiB 时 256MiB 窗口只能容纳 ~4 块，8 worker 的高核机器会有约一半
    worker 无任务可做（并行度 8→4）。缩放后 1080p 全幅 8 worker 每块 ~24MiB、
    窗口 10，喂满全部 worker。
    """
    frame_bytes = max(job.width * render_h * 4, 1)
    target_bytes = max(
        _CHUNK_MIN_TARGET_BYTES,
        min(
            _CHUNK_TARGET_BYTES,
            _MULTIPROC_MAX_PENDING_BYTES // max(worker_count + 2, 1),
        ),
    )
    by_bytes = max(1, target_bytes // frame_bytes)
    by_balance = max(1, total_frames // (worker_count * 4))
    return max(1, min(by_bytes, by_balance))


# A3 内存护栏：imap 会把全部任务一次性派发、已完成结果在主进程无界积压，
# 4K 全幅帧（31.6MiB/帧）下渲染快于编码时几分钟即可吃掉数 GB 内存。
# 改用 apply_async + 有界在飞窗口：同时未消费的 chunk 结果条数与字节数都封顶。
_MULTIPROC_MAX_PENDING_BYTES = 256 * 1024 * 1024  # 在飞 chunk 结果的保守下限
_MULTIPROC_PENDING_HARD_CAP_BYTES = 1024 * 1024 * 1024  # 高内存机也最多 1GiB
_MULTIPROC_SYSTEM_RESERVE_BYTES = 1024 * 1024 * 1024  # 留给 UI / 背景解码 / ffmpeg
_MULTIPROC_WORKER_OVERHEAD_BYTES = 64 * 1024 * 1024  # QApplication / 字体与栅格缓存
_MULTIPROC_MIN_STALL_TIMEOUT_S = 60.0  # 单 chunk 结果等待的基础时间
_MULTIPROC_STALL_BYTES_PER_SLOW_S = 512 * 1024  # 假设的最慢渲染速率（字节/秒）
_MULTIPROC_POLL_INTERVAL_S = 0.5  # 等待结果的单次轮询时长（期间响应取消）


def _resolve_pending_memory_budget(available_memory_bytes: int | None) -> int:
    """在飞结果预算：256MiB 保守下限，有余量时用可用内存的 1/8。

    上限 1GiB，足以喂满 16 个 4K 全幅 worker（约 506MiB），
    又不会因高内存机器而允许主进程无节制积压。
    """

    if available_memory_bytes is None or available_memory_bytes <= 0:
        return _MULTIPROC_MAX_PENDING_BYTES
    return max(
        _MULTIPROC_MAX_PENDING_BYTES,
        min(
            _MULTIPROC_PENDING_HARD_CAP_BYTES,
            int(available_memory_bytes) // 8,
        ),
    )


def _resolve_pending_window(
    worker_count: int,
    chunk: int,
    frame_bytes: int,
    *,
    pending_budget_bytes: int = _MULTIPROC_MAX_PENDING_BYTES,
) -> int:
    """同时在飞（已派发未消费）的 chunk 数上限。

    调用方需先经 :func:`_resolve_effective_worker_count` 把 worker 数压回
    预算内（worker ≤ budget/chunk），此后窗口 = min(worker+2, 字节上限)
    恒 ≥ worker 数——既不饿死 worker，也绝不突破内存预算。
    """
    chunk_bytes = max(chunk * max(frame_bytes, 1), 1)
    by_bytes = max(1, max(int(pending_budget_bytes), 1) // chunk_bytes)
    return max(1, min(worker_count + 2, by_bytes))


def _resolve_effective_worker_count(
    worker_count: int,
    chunk: int,
    frame_bytes: int,
    *,
    available_memory_bytes: int | None = None,
    pending_budget_bytes: int = _MULTIPROC_MAX_PENDING_BYTES,
) -> int:
    """按主进程在飞结果和系统可用内存限制 worker 数。

    单帧巨大时，worker 数同时受动态在飞结果预算和估算的
    worker 峰值内存限制。高内存机器的在飞预算可从 256MiB
    扩到 1GiB，因此手动 16 worker 的 4K 不会因固定上限被错压成 8；
    低内存机器仍会降 worker 防止 OOM。
    """
    chunk_bytes = max(chunk * max(frame_bytes, 1), 1)
    by_bytes = max(1, max(int(pending_budget_bytes), 1) // chunk_bytes)
    resolved = max(1, min(worker_count, by_bytes))
    if available_memory_bytes is None or available_memory_bytes <= 0:
        return resolved

    # worker 常驻 QImage + empty_frame；渲染 chunk 时同时存在 bytearray、
    # 返回 bytes 及 multiprocessing 序列化副本。额外保留 QApplication /
    # 字体缓存的经验上限，避免 4K 全幅回退一次拉起 8 个大进程。
    per_worker_peak = (
        2 * max(frame_bytes, 1)
        + 3 * chunk_bytes
        + _MULTIPROC_WORKER_OVERHEAD_BYTES
    )
    # available 已排除当前已占用内存；再保留至少 1GiB 与在飞
    # 结果窗口。高内存机器不会触发降级，低内存/UMA 压力机器降并发。
    worker_budget = max(
        int(available_memory_bytes)
        - _MULTIPROC_SYSTEM_RESERVE_BYTES
        - max(int(pending_budget_bytes), 1),
        0,
    )
    by_system_memory = max(1, worker_budget // max(per_worker_peak, 1))
    return max(1, min(resolved, by_system_memory))


def _available_system_memory_bytes() -> int | None:
    """返回当前可用物理内存；查询失败时保留旧的并发策略。"""

    try:
        import psutil

        available = int(psutil.virtual_memory().available)
    except (ImportError, AttributeError, OSError, TypeError, ValueError):
        return None
    return available if available > 0 else None


def _resolve_stall_timeout_s(chunk_frames: int, frame_bytes: int) -> float:
    """单 chunk 结果的等待上限：超时视为 worker 已异常（如被系统 OOM 终止）。

    按数据量而非帧数估算——条带/多带让单帧更小、帧数更多，按帧数线性放大
    会等到数十分钟：基础 60s + 每 512KiB 给 1s（4K 全幅 33MiB ≈ 125s）。
    环境变量 KROK_SUBTITLE_RENDER_STALL_TIMEOUT_S 覆盖。
    """
    raw = os.environ.get("KROK_SUBTITLE_RENDER_STALL_TIMEOUT_S")
    if raw and raw.strip():
        try:
            value = float(raw)
        except ValueError:
            value = 0.0
        if value > 0:
            return value
    chunk_bytes = max(int(chunk_frames), 1) * max(int(frame_bytes), 1)
    return _MULTIPROC_MIN_STALL_TIMEOUT_S + chunk_bytes / _MULTIPROC_STALL_BYTES_PER_SLOW_S


def _drain_pending_head(
    pending: "deque",
    stall_timeout_s: float,
    *,
    should_cancel: Callable[[], bool] | None = None,
) -> bytes:
    """按序取队头 chunk 结果；等待期间以短轮询响应取消，并维护绝对截止时间。

    单次 ``AsyncResult.get`` 长阻塞期间既无法取消也探测不到停滞，这里每
    ``_MULTIPROC_POLL_INTERVAL_S`` 秒醒来一次：用户停止导出立即抛
    ``ExportCancelled``；超过绝对截止仍无结果则抛 ``ProcessingError``。
    """
    import multiprocessing as mp

    result = pending.popleft()
    deadline = time.monotonic() + max(stall_timeout_s, _MULTIPROC_POLL_INTERVAL_S)
    while True:
        try:
            return result.get(timeout=_MULTIPROC_POLL_INTERVAL_S)
        except mp.TimeoutError:
            pass
        except (ExportCancelled, ProcessingError):
            raise
        except Exception as exc:
            # worker 内的 MemoryError / MaybeEncodingError / 渲染异常原样冒泡会
            # 绕过外层 ffmpeg 清理分支（不是 ProcessingError/OSError），统一
            # 包装后再抛。
            raise ProcessingError(
                f"渲染 worker 异常（{type(exc).__name__}: {exc}），导出中止；"
                "可尝试调低导出分辨率或在环境变量 KROK_SUBTITLE_RENDER_WORKERS 中减少进程数"
            ) from exc
        if should_cancel is not None and should_cancel():
            raise ExportCancelled("已停止导出。")
        if time.monotonic() >= deadline:
            raise ProcessingError(
                f"渲染 worker 超过 {stall_timeout_s:.0f}s 无响应"
                "（可能被系统因内存不足终止），导出中止；"
                "可尝试调低导出分辨率或在环境变量 KROK_SUBTITLE_RENDER_WORKERS 中减少进程数"
            )


def _iter_ordered_pool_results(
    pool,
    task_fn,
    tasks,
    *,
    window: int,
    stall_timeout_s: float,
    should_cancel: Callable[[], bool] | None = None,
):
    """apply_async 按序产出 chunk 结果，同时在飞条数不超过 ``window``。

    严格按提交顺序 ``.get()`` 队头结果以保序；窗口堵满时暂停派发新任务，
    从而把主进程内已完成未消费结果的内存峰值钉在 window×chunk 字节内。
    """
    pending: deque = deque()
    try:
        for task in tasks:
            while len(pending) >= window:
                yield _drain_pending_head(
                    pending, stall_timeout_s, should_cancel=should_cancel
                )
            pending.append(pool.apply_async(task_fn, (task,)))
        while pending:
            yield _drain_pending_head(
                pending, stall_timeout_s, should_cancel=should_cancel
            )
    finally:
        # 提前退出（取消 / 异常）时丢弃剩余句柄；池由调用方 terminate。
        pending.clear()


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
    logger: Logger | None = None,
) -> None:
    """多进程并行渲染：worker 池各渲一段，主进程按序收回并写入 ffmpeg stdin。

    apply_async + 有界在飞窗口保序消费（替代 imap 的无界结果积压），4K 下
    已完成未消费的 chunk 结果内存峰值封顶在 _MULTIPROC_MAX_PENDING_BYTES；
    单帧巨大时按预算降低实际 worker 数（不放大窗口）。取消 / 异常时
    terminate 整池。
    """
    import multiprocessing as mp

    chunk = _resolve_chunk_size(job, render_h, total_frames, worker_count)
    tasks = [(start, min(chunk, total_frames - start)) for start in range(0, total_frames, chunk)]
    frame_bytes = job.width * render_h * 4
    available_memory = _available_system_memory_bytes()
    pending_budget = _resolve_pending_memory_budget(available_memory)
    effective_workers = _resolve_effective_worker_count(
        worker_count,
        chunk,
        frame_bytes,
        available_memory_bytes=available_memory,
        pending_budget_bytes=pending_budget,
    )
    if effective_workers < worker_count and logger is not None:
        logger(
            f"内存预算限制：渲染进程数从 {worker_count} 降至 "
            f"{effective_workers}（在飞结果预算 {pending_budget / 1048576:.0f} MiB）"
        )
    window = _resolve_pending_window(
        effective_workers,
        chunk,
        frame_bytes,
        pending_budget_bytes=pending_budget,
    )
    stall_timeout_s = _resolve_stall_timeout_s(chunk, frame_bytes)
    ctx = mp.get_context("spawn")
    pool = ctx.Pool(
        effective_workers,
        initializer=_render_worker_init,
        initargs=(job, strip_top, render_h),
    )
    written = 0
    try:
        results = _iter_ordered_pool_results(
            pool,
            _render_worker_chunk,
            tasks,
            window=window,
            stall_timeout_s=stall_timeout_s,
            should_cancel=should_cancel,
        )
        # 不用 zip：CPython 的 zip 会复用结果元组，旧 blob 在生成器推进补交
        # 任务期间仍被元组持有；手动配对 + del 让已完成结果的存活峰值严格
        # 等于 window×chunk。
        task_iter = iter(tasks)
        for blob in results:
            _start, count = next(task_iter)
            try:
                if should_cancel is not None and should_cancel():
                    terminate_process(process)
                    raise ExportCancelled("已停止导出。")
                process.stdin.write(blob)
                written += count
                if on_progress is not None:
                    on_progress(written, total_frames)
            finally:
                # 写入/进度回调（可能抛异常）结束后都立即释放消费者侧引用，
                # 下一次 next() 补交任务时存活峰值严格等于 window×chunk
                del blob
    finally:
        # 无论正常完成 / 取消 / 异常，都强制收掉 workers（可能仍有在飞任务）。
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
    logger: Logger | None = None,
) -> None:
    """多进程并行渲染（方案 B 打包多带），主进程按序收回写入 ffmpeg stdin。

    与 _write_frames_multiprocess 相同的有界在飞窗口 + 预算内降 worker +
    停滞超时护栏。"""
    import multiprocessing as mp

    chunk = _resolve_chunk_size(job, packed_h, total_frames, worker_count)
    tasks = [(start, min(chunk, total_frames - start)) for start in range(0, total_frames, chunk)]
    frame_bytes = job.width * packed_h * 4
    available_memory = _available_system_memory_bytes()
    pending_budget = _resolve_pending_memory_budget(available_memory)
    effective_workers = _resolve_effective_worker_count(
        worker_count,
        chunk,
        frame_bytes,
        available_memory_bytes=available_memory,
        pending_budget_bytes=pending_budget,
    )
    if effective_workers < worker_count and logger is not None:
        logger(
            f"内存预算限制：渲染进程数从 {worker_count} 降至 "
            f"{effective_workers}（在飞结果预算 {pending_budget / 1048576:.0f} MiB）"
        )
    window = _resolve_pending_window(
        effective_workers,
        chunk,
        frame_bytes,
        pending_budget_bytes=pending_budget,
    )
    stall_timeout_s = _resolve_stall_timeout_s(chunk, frame_bytes)
    ctx = mp.get_context("spawn")
    pool = ctx.Pool(
        effective_workers,
        initializer=_render_worker_init_bands,
        initargs=(job, bands, packed_h),
    )
    written = 0
    try:
        results = _iter_ordered_pool_results(
            pool,
            _render_worker_chunk_bands,
            tasks,
            window=window,
            stall_timeout_s=stall_timeout_s,
            should_cancel=should_cancel,
        )
        # 同 _write_frames_multiprocess：手动配对 + del，避免 zip 元组在生成器
        # 推进期间持有旧 blob。
        task_iter = iter(tasks)
        for blob in results:
            _start, count = next(task_iter)
            try:
                if should_cancel is not None and should_cancel():
                    terminate_process(process)
                    raise ExportCancelled("已停止导出。")
                process.stdin.write(blob)
                written += count
                if on_progress is not None:
                    on_progress(written, total_frames)
            finally:
                # 写入/进度回调（可能抛异常）结束后都立即释放消费者侧引用，
                # 下一次 next() 补交任务时存活峰值严格等于 window×chunk
                del blob
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


def _drain_process_output(
    process: subprocess.Popen,
    logger: Logger,
    output_tail: deque[str] | None = None,
) -> None:
    if process.stdout is None:
        return
    for raw in process.stdout:
        if isinstance(raw, bytes):
            line = raw.decode("utf-8", errors="replace").strip()
        else:
            line = str(raw).strip()
        if line:
            if output_tail is not None:
                output_tail.append(line)
            logger(line)


def _should_retry_amf_with_cpu(
    command: list[str], output_tail: deque[str],
) -> bool:
    """仅对 AMF 初始化/设备/内存类错误用 CPU 编码重试一次。

    输出目录不可写、输入损坏、磁盘满等与 AMF 无关的失败不重试，
    避免长视频在必然失败时白跑第二遍。
    """

    if not any(part in {"h264_amf", "hevc_amf"} for part in command):
        return False
    output = "\n".join(output_tail).lower()
    markers = (
        "amf_",
        "createcomponent",
        "no capable devices",
        "out of memory",
        "cannot allocate memory",
        "failed to initialise",
        "failed to initialize",
        "error while opening encoder",
        "error initializing output stream",
        "device removed",
    )
    return any(marker in output for marker in markers)


def _remove_incomplete_output(output_path: Path, logger: Logger) -> None:
    if not output_path.exists():
        return
    try:
        output_path.unlink()
        logger(f"已清理未完成的输出文件: {output_path}")
    except OSError as exc:
        logger(f"清理未完成的输出文件失败: {output_path} ({exc})")
