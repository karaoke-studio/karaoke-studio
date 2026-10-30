"""Build ffmpeg commands from the stable subtitle render-job contract."""

from __future__ import annotations

import re
from pathlib import Path

from PyQt6.QtGui import QColor

from krok_helper.errors import ProcessingError
from krok_helper.subtitle_render.domain.background import BackgroundSource
from krok_helper.subtitle_render.domain.frame_rate import frame_rate_text
from krok_helper.subtitle_render.domain.roll import (
    ROLL_FILL_ORIGINAL,
    ROLL_FILL_STRETCH,
    ROLL_FILL_TILE,
    ROLL_MAIN_TRANSITION_CROSSFADE,
    ROLL_MAIN_TRANSITION_WHITE,
    ROLL_TILE_MAX_CELLS,
    RollSegmentConfig,
    RollTimelinePlan,
    roll_dip_half_ms,
)
from krok_helper.subtitle_render.engine.export.encoder_select import video_encoder_options
from krok_helper.subtitle_render.engine.render.render_bands import packed_offsets
from krok_helper.subtitle_render.engine.export.render_job import (
    OUTPUT_FORMAT_MP4,
    OUTPUT_FORMAT_MOV_QTRLE,
    OUTPUT_FORMAT_MOV_TRANSPARENT,
    RenderJob,
    format_has_alpha,
    format_needs_background,
    is_png_sequence,
)
from krok_helper.subtitle_render.engine.export.render_job_policy import (
    resolve_duration_ms,
    resolve_roll_timeline,
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
        f"color=c=black@0.0:s={job.width}x{job.height}:r={frame_rate_text(job.fps)}"
        f":d={duration_seconds:.6f}[bg];"
    )


def background_scale_chain(job: RenderJob, duration_seconds: float) -> str:
    """Build the background scaling chain with preview-equivalent semantics."""

    if not format_needs_background(job.output_format):
        return transparent_background_chain(job, duration_seconds)
    source = resolved_background(job)
    # Extend a finished video with black frames rather than repeating its last
    # frame. Trim afterwards so longer videos keep the requested duration.
    tail = "tpad=stop_mode=add:stop=-1:color=black," if source.kind == "video" else ""
    cover = source.kind in {"image", "image_sequence"} and source.image_fit == "cover"
    if cover:
        return (
            f"[1:v:0]scale={job.width}:{job.height}"
            ":force_original_aspect_ratio=increase:force_divisible_by=2,"
            f"crop={job.width}:{job.height},"
            f"fps={frame_rate_text(job.fps)},trim=duration={duration_seconds:.6f},"
            "setpts=PTS-STARTPTS[bg];"
        )
    return (
        f"[1:v:0]scale={job.width}:{job.height}:force_original_aspect_ratio=decrease,"
        f"pad={job.width}:{job.height}:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"fps={frame_rate_text(job.fps)},{tail}trim=duration={duration_seconds:.6f},setpts=PTS-STARTPTS[bg];"
    )


def bands_filter_graph(
    job: RenderJob,
    duration_seconds: float,
    bands: list[tuple[int, int]],
    plan: RollTimelinePlan | None = None,
) -> str:
    """Restore packed subtitle bands to their original vertical positions."""

    offsets = packed_offsets(bands)
    n = len(bands)
    bg, _unused_inputs = background_graph(job, duration_seconds, plan)
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


# ---------------------------------------------------------------------------
# 前黑/后黑：分段背景滤镜图与混音图
# ---------------------------------------------------------------------------

def _roll_xfade(style: str) -> str:
    # 仅交叉溶解还走 xfade；黑场/白场已改为剪切点两侧的 fade+concat。
    return "fade"


def _seconds(ms: float) -> str:
    return f"{max(float(ms), 0.0) / 1000.0:.6f}"


def _fade(direction: str, color: str, start_ms: float, duration_ms: float) -> str:
    if duration_ms <= 0:
        return ""
    return (
        f",fade=t={direction}:st={_seconds(start_ms)}"
        f":d={_seconds(duration_ms)}:color={color}"
    )


def roll_segment_input_args(
    config: RollSegmentConfig, job: RenderJob, segment_seconds: float
) -> list[str]:
    """Return the ffmpeg input arguments for one roll-segment source.

    与 :func:`background_input_args` 同构，但固体色直接限定段长；动图
    （GIF/WebP）按一图流语义循环播放、由 ``fps`` 滤镜换算到画布帧率。
    """

    source = config.normalized().source
    if source.kind == "solid":
        color = QColor(source.color)
        if not color.isValid():
            raise ProcessingError(f"前黑/后黑背景颜色无效: {source.color}")
        return [
            "-f",
            "lavfi",
            "-i",
            f"color=c={color.name()}:s={job.width}x{job.height}"
            f":r={frame_rate_text(job.fps)}:d={max(segment_seconds, 0.001):.6f}",
        ]
    if source.kind == "image":
        suffix = Path(str(source.path)).suffix.lower()
        if suffix == ".ffconcat":
            return ["-safe", "0", "-f", "concat", "-i", str(source.path)]
        if suffix == ".gif":
            # GIF 自带循环：保留循环、trim 截到段长（一图流动图语义）。
            return ["-ignore_loop", "0", "-i", str(source.path)]
        return ["-loop", "1", "-framerate", frame_rate_text(job.fps), "-i", str(source.path)]
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
    return ["-i", str(source.path)]


def _roll_tile_chain(in_label: str, out_label: str, job: RenderJob, config: RollSegmentConfig) -> str:
    """平铺：同一帧 split 成 cols×rows 网格后裁到画布（左上锚定）。

    行列数来自探针缓存的素材尺寸（未知时调用方回退自适应大小）。位置
    表达式只引用 wN/hN（各输入同尺寸），与预览 drawTiledPixmap 自 (0,0)
    铺起的语义一致，画布边缘由 crop 裁掉。
    """

    cols, rows = _roll_tile_counts(config, job)
    count = cols * rows
    if count == 1:
        return f"[{in_label}]crop={job.width}:{job.height}:0:0[{out_label}];"
    split_labels = [f"{out_label}t{i}" for i in range(count)]
    positions = []
    for index in range(count):
        row, col = divmod(index, cols)
        x_terms = [f"w{i}" for i in range(row * cols, row * cols + col)]
        y_terms = [f"h{i}" for i in range(0, row * cols, cols)]
        positions.append(f"{'+'.join(x_terms) or '0'}_{'+'.join(y_terms) or '0'}")
    layout = "|".join(positions)
    inputs = "".join(f"[{label}]" for label in split_labels)
    return (
        f"[{in_label}]split={count}{inputs};"
        f"{inputs}xstack=inputs={count}:layout={layout}[{in_label}x];"
        f"[{in_label}x]crop={job.width}:{job.height}:0:0[{out_label}];"
    )


def _roll_tile_counts(config: RollSegmentConfig, job: RenderJob) -> tuple[int, int]:
    normalized = config.normalized()
    width = int(normalized.source_width or 0)
    height = int(normalized.source_height or 0)
    if width <= 0 or height <= 0:
        return 1, 1
    cols = max(-(-job.width // width), 1)
    rows = max(-(-job.height // height), 1)
    if cols * rows > ROLL_TILE_MAX_CELLS:
        return 1, 1
    return cols, rows


def _roll_fill_chain(in_label: str, out_label: str, job: RenderJob, config: RollSegmentConfig) -> str:
    """按填充风格把段素材变换到画布（与预览 roll_fill_dest_rect 同口径）。"""

    normalized = config.normalized()
    fill = normalized.fill
    source = normalized.source
    if source.kind == "solid":
        # 固体色输入本身就是画布尺寸，任何填充等价。
        return f"[{in_label}]null[{out_label}];"
    if fill == ROLL_FILL_STRETCH:
        chain = f"scale={job.width}:{job.height}"
    elif fill == ROLL_FILL_ORIGINAL:
        chain = (
            f"crop=w='min(iw,{job.width})':h='min(ih,{job.height})'"
            f":x='(iw-out_w)/2':y='(ih-out_h)/2',"
            f"pad={job.width}:{job.height}:(ow-iw)/2:(oh-ih)/2:color=black"
        )
    elif fill == ROLL_FILL_TILE and (
        int(normalized.source_width or 0) > 0
        and int(normalized.source_height or 0) > 0
        and (
            -(-job.width // int(normalized.source_width))
            * -(-job.height // int(normalized.source_height))
            <= ROLL_TILE_MAX_CELLS
        )
    ):
        return _roll_tile_chain(in_label, out_label, job, config)
    else:  # fit / 平铺无尺寸缓存：等比完整放入 + 黑边（contain）
        chain = (
            f"scale={job.width}:{job.height}:force_original_aspect_ratio=decrease,"
            f"pad={job.width}:{job.height}:(ow-iw)/2:(oh-ih)/2:color=black"
        )
    return f"[{in_label}]{chain}[{out_label}];"


def _roll_segment_chain(
    in_label: str,
    out_label: str,
    job: RenderJob,
    config: RollSegmentConfig,
    segment_ms: int,
    *,
    prepend_clone_ms: int = 0,
    self_fade_in: bool = False,
    self_fade_out: bool = False,
    head_fade_ms: float = 0,
    head_fade_color: str = "black",
    tail_fade_ms: float = 0,
    tail_fade_color: str = "black",
) -> str:
    """单段链：填充 → 画布帧率 → 冻结帧头/黑帧尾延长 → trim 到段长 → 过渡。

    ``prepend_clone_ms``：交叉溶解时入段需要在前段末尾提前播放，克隆首帧
    V ms 保证过渡完成后内容恰好从段起点开始。黑场/白场过渡采用 Adobe
    Premiere 式「剪切点两侧各半」的淡入淡出，不使用克隆帧。

    ``head_fade_ms`` / ``tail_fade_ms``：与相邻段相接的黑/白场淡入淡出；
    只改画面，音频时间轴不动。
    """

    normalized = config.normalized()
    source = normalized.source
    graph = _roll_fill_chain(in_label, f"{in_label}f", job, config)
    chain = f"fps={frame_rate_text(job.fps)},format=yuv420p"
    if prepend_clone_ms > 0:
        chain += f",tpad=start_mode=clone:start_duration={_seconds(prepend_clone_ms)}"
    if source.kind == "video":
        # 段素材比设定时长短时冻结最后一帧补足（过渡窗口内不露黑底）；
        # 主背景视频仍维持既有「播完续黑帧」哲学，不受影响。
        chain += ",tpad=stop_mode=clone:stop=-1"
    chain += f",trim=duration={_seconds(segment_ms + prepend_clone_ms)},setpts=PTS-STARTPTS"
    chain += _fade("in", head_fade_color, 0, head_fade_ms)
    self_ms = min(normalized.self_transition_ms, segment_ms)
    if self_ms > 0:
        color = "white" if normalized.self_transition_style == "white" else "black"
        if self_fade_in:
            chain += _fade("in", color, 0, self_ms)
        if self_fade_out:
            chain += _fade(
                "out", color,
                max(segment_ms + prepend_clone_ms - self_ms, 0), self_ms,
            )
    chain += _fade(
        "out", tail_fade_color,
        max(segment_ms + prepend_clone_ms - tail_fade_ms, 0),
        tail_fade_ms,
    )
    graph += f"[{in_label}f]{chain}[{out_label}];"
    return graph


def _roll_main_chain(
    in_label: str,
    out_label: str,
    job: RenderJob,
    main_ms: int,
    *,
    prepend_clone_ms: int = 0,
    head_fade_ms: float = 0,
    head_fade_color: str = "black",
    tail_fade_ms: float = 0,
    tail_fade_color: str = "black",
) -> str:
    """主段链：沿用主背景既有 cover/contain 语义 + 黑帧尾延长到主段长。"""

    source = resolved_background(job)
    cover = source.kind in {"image", "image_sequence"} and source.image_fit == "cover"
    scale = (
        f"scale={job.width}:{job.height}"
        ":force_original_aspect_ratio=increase:force_divisible_by=2,"
        f"crop={job.width}:{job.height}"
        if cover
        else f"scale={job.width}:{job.height}:force_original_aspect_ratio=decrease,"
        f"pad={job.width}:{job.height}:(ow-iw)/2:(oh-ih)/2:color=black"
    )
    chain = f"{scale},fps={frame_rate_text(job.fps)},format=yuv420p"
    if prepend_clone_ms > 0:
        chain += f",tpad=start_mode=clone:start_duration={_seconds(prepend_clone_ms)}"
    if source.kind == "video":
        chain += ",tpad=stop_mode=add:stop=-1:color=black"
    chain += f",trim=duration={_seconds(main_ms + prepend_clone_ms)},setpts=PTS-STARTPTS"
    chain += _fade("in", head_fade_color, prepend_clone_ms, head_fade_ms)
    chain += _fade(
        "out", tail_fade_color,
        max(prepend_clone_ms + main_ms - tail_fade_ms, 0),
        tail_fade_ms,
    )
    return f"[{in_label}]{chain}[{out_label}];"


def _transition_color(style: str) -> str:
    return "white" if style == ROLL_MAIN_TRANSITION_WHITE else "black"


def roll_background_graph(
    job: RenderJob, plan: RollTimelinePlan, duration_seconds: float
) -> tuple[str, list[list[str]]]:
    """构建产出 ``[bg]`` 的前黑/主段/后黑拼接滤镜图与各输入参数。

    返回 ``(滤镜图（以分号结尾、产出 [bg]）， 按输入顺序的参数列表)``。
    主背景恒为首个背景输入（沿用输入 #1 约定），其后为前黑、后黑素材。

    黑场/白场过渡按 Adobe Premiere 的 Dip to Black / Dip to White 口径：过渡
    分居剪切点两侧，出场段在剪切点前半个窗口淡到过渡色、入场段在剪切点后
    半个窗口从过渡色淡入；两段都保持自己的内容连续播放，不冻结帧，总时长
    不变。交叉溶解仍沿用重叠窗口 + 冻结首帧的 xfade。
    """

    background = resolved_background(job)
    background_args = background_input_args(background, job, plan.total_ms / 1000.0)
    input_arg_lists: list[list[str]] = [background_args]
    graph_parts: list[str] = []

    main_ms = max(plan.main_end_ms - plan.pre_ms, 0)
    pre_style = plan.pre.main_transition_style
    post_style = plan.post.main_transition_style

    if main_ms <= 0:
        # 没有主段内容（纯前黑/后黑工程）：两段直接相邻，过渡窗口被
        # roll_dip_half_ms 钳到 0，与预览 alpha 口径一致。
        if plan.has_pre:
            graph_parts.append(
                _roll_segment_chain(
                    f"{len(input_arg_lists) + 1}:v:0",
                    "rollpre",
                    job,
                    plan.pre,
                    plan.pre_ms,
                    self_fade_in=True,
                )
            )
            input_arg_lists.append(
                roll_segment_input_args(plan.pre, job, plan.pre_ms / 1000.0)
            )
        if plan.has_post:
            graph_parts.append(
                _roll_segment_chain(
                    f"{len(input_arg_lists) + 1}:v:0",
                    "rollpost",
                    job,
                    plan.post,
                    plan.post_ms,
                    self_fade_out=True,
                )
            )
            input_arg_lists.append(
                roll_segment_input_args(plan.post, job, plan.post_ms / 1000.0)
            )
        if plan.has_pre and plan.has_post:
            graph_parts.append("[rollpre][rollpost]concat=n=2:v=1:a=0[rollall];")
        elif plan.has_pre:
            graph_parts.append("[rollpre]null[rollall];")
        elif plan.has_post:
            graph_parts.append("[rollpost]null[rollall];")
        graph_parts.append(
            f"[rollall]tpad=stop_mode=add:stop=-1:color=black,"
            f"trim=duration={max(duration_seconds, 0.001):.6f},setpts=PTS-STARTPTS[bg];"
        )
        return "".join(graph_parts), input_arg_lists

    pre_v = (
        min(plan.pre.main_transition_ms, plan.pre_ms, main_ms)
        if plan.has_pre
        else 0
    )
    post_v = (
        min(plan.post.main_transition_ms, main_ms, plan.post_ms)
        if plan.has_post
        else 0
    )
    pre_half = (
        roll_dip_half_ms(
            pre_style, plan.pre.main_transition_ms, plan.pre_ms, main_ms,
            main_ms=main_ms,
        )
        if plan.has_pre
        else 0.0
    )
    post_half = (
        roll_dip_half_ms(
            post_style, plan.post.main_transition_ms, main_ms, plan.post_ms,
            main_ms=main_ms,
        )
        if plan.has_post
        else 0.0
    )
    if pre_half + post_half > main_ms and pre_half + post_half > 0:
        scale = main_ms / (pre_half + post_half) if main_ms > 0 else 0.0
        pre_half *= scale
        post_half *= scale
    if (
        plan.has_post
        and post_style == ROLL_MAIN_TRANSITION_CROSSFADE
        and pre_half > 0
    ):
        # 与 roll_layer_alphas 同一钳制：前黑 dip 淡入占用的主段开头不再
        # 给后黑 crossfade，避免预览/导出窗口重叠。
        post_v = min(post_v, max(main_ms - pre_half, 0.0))
    pre_cross = plan.has_pre and pre_style == ROLL_MAIN_TRANSITION_CROSSFADE and pre_v > 0
    post_cross = plan.has_post and post_style == ROLL_MAIN_TRANSITION_CROSSFADE and post_v > 0

    # 前黑段。
    if plan.has_pre:
        # 输入编号：字幕管道 #0、主背景 #1，段素材从 #2 起依次追加。
        graph_parts.append(
            _roll_segment_chain(
                f"{len(input_arg_lists) + 1}:v:0",
                "rollpre",
                job,
                plan.pre,
                plan.pre_ms,
                self_fade_in=True,
                tail_fade_ms=pre_half,
                tail_fade_color=_transition_color(pre_style),
            )
        )
        input_arg_lists.append(
            roll_segment_input_args(plan.pre, job, plan.pre_ms / 1000.0)
        )

    # 主段：交叉溶解克隆首帧提前量；黑/白场走剪切点两侧淡入淡出。
    graph_parts.append(
        _roll_main_chain(
            "1:v:0",
            "rollmain",
            job,
            main_ms,
            prepend_clone_ms=pre_v if pre_cross else 0,
            head_fade_ms=pre_half,
            head_fade_color=_transition_color(pre_style),
            tail_fade_ms=post_half,
            tail_fade_color=_transition_color(post_style),
        )
    )

    # 前黑 → 主段拼接。
    if plan.has_pre and pre_cross:
        graph_parts.append(
            f"[rollpre][rollmain]xfade=transition={_roll_xfade(pre_style)}"
            f":duration={_seconds(pre_v)}:offset={_seconds(plan.pre_ms - pre_v)}[rollpm];"
        )
    elif plan.has_pre:
        graph_parts.append("[rollpre][rollmain]concat=n=2:v=1:a=0[rollpm];")
    else:
        graph_parts.append("[rollmain]null[rollpm];")

    # 后黑段。
    if plan.has_post:
        graph_parts.append(
            _roll_segment_chain(
                f"{len(input_arg_lists) + 1}:v:0",
                "rollpost",
                job,
                plan.post,
                plan.post_ms,
                prepend_clone_ms=post_v if post_cross else 0,
                head_fade_ms=post_half,
                head_fade_color=_transition_color(post_style),
                self_fade_out=True,
            )
        )
        input_arg_lists.append(
            roll_segment_input_args(plan.post, job, plan.post_ms / 1000.0)
        )
        if post_cross:
            graph_parts.append(
                f"[rollpm]trim=duration={_seconds(plan.main_end_ms)},setpts=PTS-STARTPTS[rollpmt];"
                f"[rollpmt][rollpost]xfade=transition={_roll_xfade(post_style)}"
                f":duration={_seconds(post_v)}:offset={_seconds(plan.main_end_ms - post_v)}[rollall];"
            )
        else:
            graph_parts.append(
                f"[rollpm]trim=duration={_seconds(plan.main_end_ms)},setpts=PTS-STARTPTS[rollpmt];"
                "[rollpmt][rollpost]concat=n=2:v=1:a=0[rollall];"
            )
    else:
        graph_parts.append("[rollpm]null[rollall];")

    graph_parts.append(
        f"[rollall]tpad=stop_mode=add:stop=-1:color=black,"
        f"trim=duration={max(duration_seconds, 0.001):.6f},setpts=PTS-STARTPTS[bg];"
    )
    return "".join(graph_parts), input_arg_lists


def background_graph(
    job: RenderJob, duration_seconds: float, plan: RollTimelinePlan | None = None
) -> tuple[str, list[list[str]]]:
    """产出 ``[bg]`` 的滤镜段与背景输入参数（前黑/后黑不活跃时与历史图一致）。"""

    if not format_needs_background(job.output_format):
        return transparent_background_chain(job, duration_seconds), []
    if plan is not None and plan.active:
        return roll_background_graph(job, plan, duration_seconds)
    source = resolved_background(job)
    return background_scale_chain(job, duration_seconds), [
        background_input_args(source, job, duration_seconds)
    ]


def _roll_audio_chain(index: int, label: str, start_ms: int, segment_seconds: float) -> str:
    chain = (
        f"[{index}:a:0]atrim=duration={max(segment_seconds, 0.001):.6f},"
        "asetpts=PTS-STARTPTS,"
        "aformat=sample_rates=48000:channel_layouts=stereo"
    )
    if start_ms > 0:
        chain += f",adelay={int(start_ms)}:all=1"
    chain += ",apad"
    return chain + f"[{label}];"


def roll_audio_plan(
    job: RenderJob, plan: RollTimelinePlan
) -> tuple[list[str], list[list[str]]]:
    """前黑/主段/后黑按段起止对齐的音频滤镜图与额外音频输入。

    返回 ``(滤镜图列表（产出 [aout]）， 音频专用输入参数列表)``。段间互不
    重叠，``amix normalize=0`` 直接求和即原样拼接；各段独立截断到自身段长
    （用户手动时长直接截音频）。主音频源沿用既有选择：独立音频优先，否则
    主背景视频内嵌音轨（透明格式没有背景画面输入时补一条仅音频输入）。
    """

    background = resolved_background(job)
    main_ms = max(plan.main_end_ms - plan.pre_ms, 0)
    needs_background = format_needs_background(job.output_format)

    # 输入编号约定与 roll_background_graph 一致：字幕管道 #0，主背景（含背景
    # 格式才有）#1，前黑/后黑素材随后；音频专用输入再往后排。透明格式没有
    # 任何背景画面输入，段音频一律走专用音频输入。
    visual_main = 1 if needs_background else None
    visual_pre = 2 if (plan.has_pre and needs_background) else None
    visual_post = (
        2 + (1 if plan.has_pre else 0) if (plan.has_post and needs_background) else None
    )
    # 音频专用输入排在全部视觉输入（含字幕管道 #0）之后；透明格式没有任何
    # 背景画面输入，从 #1 起就是音频专用。
    next_index = 1 + (1 if needs_background else 0) + (
        1 if (plan.has_pre and needs_background) else 0
    ) + (1 if (plan.has_post and needs_background) else 0)

    extra_inputs: list[list[str]] = []
    chains: list[str] = []
    labels: list[str] = []

    def _add(index: int, path_args: list[str] | None, start_ms: int, segment_ms: int) -> None:
        nonlocal next_index
        if path_args is not None:
            index = next_index
            next_index += 1
            extra_inputs.append(path_args)
        label = f"a{len(labels)}"
        chains.append(_roll_audio_chain(index, label, start_ms, segment_ms / 1000.0))
        labels.append(label)

    def _video_audio_args(path: str, *, offset_ms: int = 0) -> list[str]:
        args: list[str] = []
        if offset_ms:
            args.extend(["-ss", f"{offset_ms / 1000.0:.6f}"])
        args.extend(["-i", path])
        return args

    # 前黑音频：独立音频取代素材声音；否则视频素材内嵌音轨。
    if plan.has_pre:
        pre = plan.pre
        if pre.audio_path:
            _add(-1, ["-i", pre.audio_path], 0, plan.pre_ms)
        elif pre.source.kind == "video" and pre.source_has_audio:
            if visual_pre is not None:
                _add(visual_pre, None, 0, plan.pre_ms)
            else:
                _add(-1, _video_audio_args(str(pre.source.path)), 0, plan.pre_ms)

    # 主段音频：独立音频优先，否则主背景视频内嵌音轨。
    if job.audio_path is not None:
        _add(-1, ["-i", str(job.audio_path)], plan.pre_ms, main_ms)
    elif background.kind == "video" and job.main_background_has_audio:
        if visual_main is not None:
            _add(visual_main, None, plan.pre_ms, main_ms)
        else:
            _add(
                -1,
                _video_audio_args(
                    str(background.path), offset_ms=int(background.video_offset_ms or 0)
                ),
                plan.pre_ms,
                main_ms,
            )

    # 后黑音频同前黑。
    if plan.has_post:
        post = plan.post
        if post.audio_path:
            _add(-1, ["-i", post.audio_path], plan.main_end_ms, plan.post_ms)
        elif post.source.kind == "video" and post.source_has_audio:
            if visual_post is not None:
                _add(visual_post, None, plan.main_end_ms, plan.post_ms)
            else:
                _add(
                    -1,
                    _video_audio_args(str(post.source.path)),
                    plan.main_end_ms,
                    plan.post_ms,
                )

    if not labels:
        return [], []
    if len(labels) == 1:
        return [chains[0].replace(f"[{labels[0]}];", "[aout];")], extra_inputs
    return [
        *chains,
        f"{''.join(f'[{label}]' for label in labels)}"
        f"amix=inputs={len(labels)}:duration=longest:normalize=0[aout];",
    ], extra_inputs


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
            f"color=c={color.name()}:s={job.width}x{job.height}:r={frame_rate_text(job.fps)}:d={duration_seconds:.6f}",
        ]
    if source.kind == "image":
        return ["-loop", "1", "-framerate", frame_rate_text(job.fps), "-i", str(source.path)]
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
    roll_plan = resolve_roll_timeline(job)
    overlay_y = 0
    pipe_w, pipe_h = job.width, job.height
    if bands is not None:
        pipe_h = sum(height for _top, height in bands)
        filter_graph = bands_filter_graph(job, duration_seconds, bands, roll_plan)
    else:
        if strip is not None:
            overlay_y, pipe_h = strip
        filter_graph = (
            background_graph(job, duration_seconds, roll_plan)[0]
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
                f"color=c=0x3F444C:s={job.width}x{job.height}:r={frame_rate_text(job.fps)}"
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
    needs_background = format_needs_background(job.output_format)
    command = [
        ffmpeg_path,
        "-y",
        "-hide_banner",
        "-loglevel",
        "warning",
        # 机器可读心跳：每 ~0.5s 一行 key=value，交给导出看门狗判断
        # “在慢跑”还是“真停了”。-nostats 关掉 \r 刷屏的统计行。
        "-progress",
        "pipe:2",
        "-nostats",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgba",
        "-s:v",
        f"{pipe_w}x{pipe_h}",
        "-r",
        frame_rate_text(job.fps),
        "-i",
        "pipe:0",
    ]
    audio_input_index: int | None = None
    roll_audio_active = (
        roll_plan.active and job.include_audio and not is_png_sequence(job.output_format)
    )
    if roll_plan.active and needs_background:
        # 前黑/后黑生效：主背景、段素材按序占满背景输入位，音频进混音图。
        _, background_input_lists = background_graph(job, duration_seconds, roll_plan)
        for input_args in background_input_lists:
            command.extend(input_args)
    elif needs_background:
        command.extend(
            background_input_args(
                resolved_background(job), job, duration_seconds
            )
        )
    if roll_audio_active:
        audio_chains, audio_inputs = roll_audio_plan(job, roll_plan)
        for input_args in audio_inputs:
            command.extend(input_args)
        if audio_chains:
            filter_graph += ";" + "".join(audio_chains).rstrip(";")
    elif job.include_audio and not is_png_sequence(job.output_format):
        # MP4 与透明 MOV 都携带音频；PNG 序列的 image2 输出装不下音频。
        if job.audio_path is not None:
            # 独立音频：含背景格式排在输入 2；透明格式没有背景输入，排 1。
            audio_input_index = 2 if needs_background else 1
            command.extend(["-i", str(job.audio_path)])
        else:
            background = resolved_background(job)
            if background.kind == "video":
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
    roll_audio_mapped = False
    if roll_audio_active:
        # 混音图产出 [aout]；无任何可用段音频时回落既有 -map 口径。
        if "[aout]" in filter_graph:
            command.extend(["-map", "[aout]"])
            roll_audio_mapped = True
        else:
            audio_input_index = None
    if audio_input_index is not None:
        command.extend(["-map", f"{audio_input_index}:a:0?"])
        # Keep the audio timeline as long as the rendered video. apad emits
        # silence after EOF; the output -t bounds both streams to the job.
        command.extend(["-af", "apad"])
    has_output_audio = audio_input_index is not None or roll_audio_mapped
    command.extend(
        ["-t", f"{duration_seconds:.6f}", "-r", frame_rate_text(job.fps), "-fps_mode", "cfr"]
    )
    if job.output_format == OUTPUT_FORMAT_MP4:
        command.extend(
            video_encoder_options(
                ffmpeg_path,
                job.encoder_mode,
                crf=job.crf,
                preset=job.preset,
                codec=job.codec,
                bitrate_mbps=job.bitrate_mbps,
            )
        )
        command.extend(["-pix_fmt", "yuv420p"])
        if has_output_audio:
            command.extend(["-c:a", "aac", "-b:a", "192k"])
        command.extend(["-movflags", "+faststart", str(job.output_path)])
    elif job.output_format == OUTPUT_FORMAT_MOV_TRANSPARENT:
        # ProRes 4444：NLE（PR/AE/达芬奇）对 4444 的 alpha 解释最成熟可靠，
        # 是透明 MOV 的默认推荐。prores_ks 仅 4444/4444 XQ 两档支持 alpha，
        # 这里固定 4444 + yuva444p10le。追求无损/小体积用 QuickTime 动画档。
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
        if has_output_audio:
            # ProRes 素材惯例配 PCM，避免个别工具对 MOV 内 AAC 的兼容问题。
            command.extend(["-c:a", "pcm_s16le"])
        command.extend([str(job.output_path)])
    elif job.output_format == OUTPUT_FORMAT_MOV_QTRLE:
        # QuickTime Animation：无损真 alpha、行程编码对大面积透明的字幕层
        # 体积友好。注意多数播放器不合成它的 straight alpha（显示为黑底），
        # 导入 PR/AE 等剪辑软件才是正确的预览方式。
        command.extend(["-c:v", "qtrle", "-pix_fmt", "rgba"])
        if has_output_audio:
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
