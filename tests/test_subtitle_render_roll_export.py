"""前黑/后黑导出命令构建测试（滤镜图结构 + 时长口径 + 可选实跑冒烟）。"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from krok_helper.subtitle_render.domain.background import BackgroundSource
from krok_helper.subtitle_render.domain.models import Style
from krok_helper.subtitle_render.domain.roll import (
    ROLL_FILL_FIT,
    ROLL_FILL_STRETCH,
    ROLL_FILL_TILE,
    RollSegmentConfig,
)
from krok_helper.subtitle_render.domain.timing import TimingChar, TimingLine, TimingTrack
from krok_helper.subtitle_render.engine.export.export_command import (
    build_render_command,
    roll_audio_plan,
    roll_background_graph,
)
from krok_helper.subtitle_render.engine.export.render_job import (
    OUTPUT_FORMAT_MP4,
    OUTPUT_FORMAT_MOV_TRANSPARENT,
    RenderJob,
)
from krok_helper.subtitle_render.engine.export.render_job_policy import (
    resolve_duration_ms,
    resolve_roll_timeline,
)


def _track(end_ms: int = 6000) -> TimingTrack:
    return TimingTrack(
        lines=[
            TimingLine(
                chars=[TimingChar(text="测", start_ms=1000), TimingChar(text="试", start_ms=1500)],
                end_ms=end_ms,
            )
        ]
    )


def _pre(tmp_path: Path, duration_ms=4000, **kwargs) -> RollSegmentConfig:
    source_path = tmp_path / "intro.mp4"
    source_path.touch()
    return RollSegmentConfig(
        enabled=True,
        source=BackgroundSource(kind="video", path=str(source_path)),
        source_has_audio=True,
        duration_ms=duration_ms,
        **kwargs,
    )


def _post(tmp_path: Path, duration_ms=2000, **kwargs) -> RollSegmentConfig:
    return RollSegmentConfig(
        enabled=True,
        source=BackgroundSource(kind="solid", color="#101010"),
        duration_ms=duration_ms,
        **kwargs,
    )


def _job(tmp_path: Path, *, pre=None, post=None, **overrides) -> RenderJob:
    main_video = tmp_path / "main.mp4"
    main_video.touch()
    values = dict(
        track=_track(),
        style=None,
        background_video_path=main_video,
        output_path=tmp_path / "out.mp4",
        background_source=BackgroundSource(kind="video", path=str(main_video)),
        width=320,
        height=180,
        fps=30,
        duration_ms=6000,
        output_format=OUTPUT_FORMAT_MP4,
        pre_roll=pre,
        post_roll=post,
        main_media_end_ms=6000,
        main_background_has_audio=True,
    )
    values.update(overrides)
    return RenderJob(**values)


def _filter_graph(command: list[str]) -> str:
    return command[command.index("-filter_complex") + 1]


def test_roll_duration_takes_max_formula(tmp_path):
    job = _job(tmp_path, pre=_pre(tmp_path, 4000), post=_post(tmp_path, 2000))
    # max(前黑+字幕轴 10s, 前黑+视频+后黑 12s) = 12s。
    assert resolve_duration_ms(job) == 12_000
    plan = resolve_roll_timeline(job)
    assert plan.lyric_shift_ms == 4000


def test_roll_graph_uses_xfade_and_segment_inputs(tmp_path):
    job = _job(
        tmp_path,
        pre=_pre(tmp_path, 4000, main_transition_ms=800, main_transition_style="crossfade",
                 self_transition_ms=300),
        post=_post(tmp_path, 2000, main_transition_ms=500, main_transition_style="black",
                   self_transition_ms=400, self_transition_style="white"),
    )
    graph, inputs = roll_background_graph(job, resolve_roll_timeline(job), 12.0)
    # 主背景 #1、前黑 #2、后黑 #3。
    assert "[1:v:0]" in graph and "[2:v:0]" in graph and "[3:v:0]" in graph
    # 前黑交叉溶解仍走 xfade（重叠窗口 + 克隆首帧）。
    assert "xfade=transition=fade:duration=0.800000:offset=3.200000" in graph
    assert "tpad=start_mode=clone:start_duration=0.800000" in graph
    # 后黑黑场按 Adobe 式居中：主段在剪切点前淡出 250ms，后黑在剪切点后淡入 250ms，
    # 两段直接 concat，不再有 xfade/克隆首帧。
    assert "fade=t=out:st=6.550000:d=0.250000:color=black" in graph
    assert "fade=t=in:st=0.000000:d=0.250000:color=black" in graph
    assert "xfade=transition=custom" not in graph
    assert "[rollpmt][rollpost]concat=n=2:v=1:a=0[rollall]" in graph
    # 自我过渡落到 fade 滤镜（前黑淡入黑 / 后黑淡出白）。
    assert "fade=t=in:st=0.000000:d=0.300000:color=black" in graph
    assert "fade=t=out:st=1.600000:d=0.400000:color=white" in graph
    # 主段黑帧尾延长；终裁到总长。
    assert graph.rstrip().endswith("trim=duration=12.000000,setpts=PTS-STARTPTS[bg];")
    assert len(inputs) == 3


def test_roll_graph_without_main_content_concats_pre_and_post(tmp_path):
    """主段时长为 0（纯前黑/后黑工程）时不能生成空的主段链。"""
    from krok_helper.subtitle_render.domain.roll import build_roll_timeline

    pre = _pre(tmp_path, 1000, main_transition_ms=600, main_transition_style="black")
    post = _post(tmp_path, 1000, main_transition_ms=600, main_transition_style="white")
    plan = build_roll_timeline(
        pre, post, track_end_ms=0, background_kind="solid"
    )
    assert plan.main_end_ms == plan.pre_ms
    job = RenderJob(
        track=TimingTrack(lines=[]),
        style=Style(),
        background_video_path=tmp_path / "main.mp4",
        output_path=tmp_path / "out.mp4",
        background_source=BackgroundSource(kind="solid", color="#000000"),
        width=320,
        height=180,
        fps=30,
        duration_ms=plan.total_ms,
        output_format=OUTPUT_FORMAT_MP4,
        include_audio=False,
        pre_roll=pre,
        post_roll=post,
    )
    graph, inputs = roll_background_graph(job, plan, plan.total_ms / 1000.0)
    assert "rollmain" not in graph
    assert "xfade" not in graph
    assert "[rollpre][rollpost]concat=n=2:v=1:a=0[rollall]" in graph
    assert len(inputs) == 3  # 主背景占位 + 前黑 + 后黑


def test_roll_graph_concat_when_no_transition(tmp_path):
    job = _job(tmp_path, pre=_pre(tmp_path, 2000, main_transition_ms=0))
    graph, _ = roll_background_graph(job, resolve_roll_timeline(job), 8.0)
    assert "concat=n=2:v=1:a=0[rollpm]" in graph
    assert "xfade" not in graph


def test_roll_graph_tile_fill_builds_xstack(tmp_path):
    job = _job(
        tmp_path,
        pre=RollSegmentConfig(
            enabled=True,
            source=BackgroundSource(kind="image", path="D:/img/tile.png"),
            source_width=100,
            source_height=80,
            duration_ms=1500,
            fill=ROLL_FILL_TILE,
        ),
    )
    graph, _ = roll_background_graph(job, resolve_roll_timeline(job), 7.5)
    # 320x180 画布 / 100x80 素材 → 4x3 网格 12 份。
    assert "split=12" in graph
    assert "xstack=inputs=12" in graph
    assert "crop=320:180:0:0" in graph


def test_roll_graph_falls_back_to_fit_without_tile_dimensions(tmp_path):
    job = _job(
        tmp_path,
        pre=RollSegmentConfig(
            enabled=True,
            source=BackgroundSource(kind="image", path="D:/img/tile.png"),
            duration_ms=1500,
            fill=ROLL_FILL_TILE,
        ),
    )
    graph, _ = roll_background_graph(job, resolve_roll_timeline(job), 7.5)
    assert "xstack" not in graph
    assert "force_original_aspect_ratio=decrease" in graph


def test_roll_graph_tile_falls_back_when_cell_count_explodes(tmp_path):
    job = _job(
        tmp_path,
        pre=RollSegmentConfig(
            enabled=True,
            source=BackgroundSource(kind="image", path="D:/img/dot.png"),
            source_width=16,
            source_height=16,
            duration_ms=1500,
            fill=ROLL_FILL_TILE,
        ),
    )
    graph, _ = roll_background_graph(job, resolve_roll_timeline(job), 7.5)
    # 320x180 画布 / 16x16 → 20x12=240 格（未超限）仍应 tile；
    # 换成 1x1 素材 → 320*180 超限，回退 fit。
    job_small = _job(
        tmp_path,
        pre=RollSegmentConfig(
            enabled=True,
            source=BackgroundSource(kind="image", path="D:/img/dot.png"),
            source_width=1,
            source_height=1,
            duration_ms=1500,
            fill=ROLL_FILL_TILE,
        ),
    )
    graph_small, _ = roll_background_graph(
        job_small, resolve_roll_timeline(job_small), 7.5
    )
    assert "xstack" in graph
    assert "xstack" not in graph_small
    assert "force_original_aspect_ratio=decrease" in graph_small


def test_roll_graph_stretch_fill(tmp_path):
    job = _job(
        tmp_path,
        pre=RollSegmentConfig(
            enabled=True,
            source=BackgroundSource(kind="image", path="D:/img/a.png"),
            source_width=640,
            source_height=480,
            duration_ms=1500,
            fill=ROLL_FILL_STRETCH,
        ),
    )
    graph, _ = roll_background_graph(job, resolve_roll_timeline(job), 7.5)
    assert "scale=320:180[2:v:0f]" in graph


def test_roll_graph_gif_input_keeps_loop(tmp_path):
    job = _job(
        tmp_path,
        pre=RollSegmentConfig(
            enabled=True,
            source=BackgroundSource(kind="image", path="D:/img/anim.gif"),
            duration_ms=1500,
            fill=ROLL_FILL_FIT,
        ),
    )
    _, inputs = roll_background_graph(job, resolve_roll_timeline(job), 7.5)
    assert inputs[1] == ["-ignore_loop", "0", "-i", "D:/img/anim.gif"]


def test_inactive_rolls_keep_legacy_graph_and_inputs(tmp_path):
    job = _job(tmp_path)
    command = build_render_command("ffmpeg", job)
    graph = _filter_graph(command)
    # 历史图：背景占输入 #1、-map 1:a:0? + apad。
    assert "scale=320:180:force_original_aspect_ratio=decrease" in graph
    assert "xfade" not in graph and "tpad=start_mode" not in graph
    inputs = [value for index, value in enumerate(command) if command[index - 1] == "-i"]
    assert inputs == ["pipe:0", str(tmp_path / "main.mp4")]
    assert "-map" in command and "1:a:0?" in command


def test_roll_audio_mix_delays_main_and_maps_aout(tmp_path):
    job = _job(tmp_path, pre=_pre(tmp_path, 4000), post=_post(tmp_path, 2000))
    command = build_render_command("ffmpeg", job)
    graph = _filter_graph(command)
    # 主音频（输入 1 内嵌）延后 4000ms、截到主段 6s；前黑音频 0 起点；
    # 后黑为纯色（无音频）不参与混音 → amix 2 路。
    assert "[1:a:0]atrim=duration=6.000000" in graph
    assert "[2:a:0]atrim=duration=4.000000" in graph
    assert "adelay=4000:all=1" in graph
    assert "amix=inputs=2:duration=longest:normalize=0[aout]" in graph
    assert "[aout]" in command
    # roll 音频走 [aout] 时仍要显式带上主路径的编码参数（MP4 192k AAC）。
    assert command[command.index("-c:a") + 1] == "aac"
    assert command[command.index("-b:a") + 1] == "192k"
    # 旧 -af apad 口径不应出现。
    assert "apad" not in command or "[aout]" in graph


def test_roll_audio_only_segment_keeps_audio_and_encoder(tmp_path):
    """主工程无音频、只有后黑独立音频时，导出仍须带音轨并保留编码参数。"""
    audio = tmp_path / "post.wav"
    audio.write_bytes(b"fake")
    job = _job(
        tmp_path,
        post=RollSegmentConfig(
            enabled=True,
            duration_ms=1500,
            source=BackgroundSource(kind="solid", color="#101010"),
            audio_path=str(audio),
        ),
        main_background_has_audio=False,
    )
    command = build_render_command("ffmpeg", job)
    graph = _filter_graph(command)
    assert "[aout]" in command
    assert "amix" not in graph  # 唯一一路音频不需要 amix
    assert command[command.index("-c:a") + 1] == "aac"
    assert command[command.index("-b:a") + 1] == "192k"


def test_roll_audio_independent_replaces_segment_audio(tmp_path):
    job = _job(
        tmp_path,
        pre=RollSegmentConfig(
            enabled=True,
            source=BackgroundSource(kind="video", path=str(tmp_path / "intro.mp4")),
            source_has_audio=True,
            audio_path=str(tmp_path / "intro.wav"),
            duration_ms=3000,
        ),
    )
    chains, inputs = roll_audio_plan(job, resolve_roll_timeline(job))
    text = "".join(chains)
    # 独立音频作为专用输入；不再引用前黑素材的 2:a。
    assert "2:a:0" not in text
    assert "[3:a:0]atrim=duration=3.000000" in text
    assert inputs == [["-i", str(tmp_path / "intro.wav")]]


def test_roll_audio_skips_video_without_stream(tmp_path):
    (tmp_path / "silent.mp4").touch()
    job = _job(
        tmp_path,
        pre=RollSegmentConfig(
            enabled=True,
            source=BackgroundSource(
                kind="video", path=str(tmp_path / "silent.mp4")
            ),
            source_has_audio=False,
            duration_ms=3000,
        ),
        main_background_has_audio=False,
    )
    chains, inputs = roll_audio_plan(job, resolve_roll_timeline(job))
    assert chains == [] and inputs == []
    # 命令里没有可映射音频 → 不带 -map 音频参数。
    command = build_render_command("ffmpeg", job)
    assert "a:0?" not in command


def test_roll_transparent_mov_audio_only_inputs(tmp_path):
    job = _job(
        tmp_path,
        pre=_pre(tmp_path, 3000),
        output_format=OUTPUT_FORMAT_MOV_TRANSPARENT,
    )
    command = build_render_command("ffmpeg", job)
    graph = _filter_graph(command)
    # 透明格式无背景画面输入：主视频与前黑素材都按仅音频输入（#1、#2）。
    inputs = [value for index, value in enumerate(command) if command[index - 1] == "-i"]
    assert inputs == ["pipe:0", str(tmp_path / "intro.mp4"), str(tmp_path / "main.mp4")]
    assert "[1:a:0]" in graph and "[2:a:0]" in graph
    assert "amix=inputs=2" in graph
    # 透明格式仍是透明底，且 roll 混音也要保留 PCM 口径。
    assert "black@0.0" in graph
    assert command[command.index("-c:a") + 1] == "pcm_s16le"


def test_roll_png_sequence_keeps_no_audio(tmp_path):
    job = _job(
        tmp_path,
        pre=_pre(tmp_path, 3000),
        output_format="png_transparent",
        output_path=tmp_path / "seq",
    )
    command = build_render_command("ffmpeg", job)
    assert "a:0" not in _filter_graph(command)
    assert "[aout]" not in command


# --------------------------------------------------------------------------- 实跑冒烟

def _ffmpeg_path() -> str | None:
    return shutil.which("ffmpeg") or (
        r"C:\ffmpeg\bin\ffmpeg.exe" if Path(r"C:\ffmpeg\bin\ffmpeg.exe").exists() else None
    )


@pytest.mark.skipif(_ffmpeg_path() is None, reason="本机无 ffmpeg，跳过实跑冒烟")
def test_roll_graph_runs_real_ffmpeg(tmp_path):
    """合成素材实跑一次前黑+后黑导出命令，校验滤镜图可执行与总时长。"""
    ffmpeg = _ffmpeg_path()

    def run(args: list[str]) -> None:
        subprocess.run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", *args], check=True)

    main_video = tmp_path / "main.mp4"
    pre_video = tmp_path / "pre.mp4"
    run(["-f", "lavfi", "-i", "color=c=green:s=320x180:r=30:d=4",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(main_video)])
    run(["-f", "lavfi", "-i", "color=c=red:s=320x180:r=30:d=2", "-c:v", "libx264",
         "-pix_fmt", "yuv420p", str(pre_video)])
    job = RenderJob(
        track=_track(end_ms=3500),
        style=None,
        background_video_path=main_video,
        output_path=tmp_path / "out.mp4",
        background_source=BackgroundSource(kind="video", path=str(main_video)),
        width=320,
        height=180,
        fps=30,
        duration_ms=4000,
        output_format=OUTPUT_FORMAT_MP4,
        pre_roll=RollSegmentConfig(
            enabled=True,
            source=BackgroundSource(kind="video", path=str(pre_video)),
            source_has_audio=False,
            duration_ms=1500,
            main_transition_ms=300,
            main_transition_style="crossfade",
        ),
        post_roll=_post(tmp_path, 1000, main_transition_ms=300, main_transition_style="black"),
        main_media_end_ms=4000,
        main_background_has_audio=True,
    )
    command = build_render_command(ffmpeg, job)
    process = subprocess.Popen(command, stdin=subprocess.PIPE)
    frame = bytes(320 * 180 * 4)
    # 期望 total = max(1500+3500, 1500+4000+1000) = 6500ms → 195 帧。
    for _ in range(195):
        process.stdin.write(frame)
    process.stdin.close()
    assert process.wait(timeout=60) == 0
    probe = subprocess.run(
        [ffmpeg, "-i", str(tmp_path / "out.mp4")],
        capture_output=True, text=True,
    ).stderr
    duration_line = next(row for row in probe.splitlines() if "Duration:" in row)
    token = duration_line.split("Duration:")[1].split(",")[0].strip()
    hours, minutes, seconds = token.split(":")
    total = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    assert abs(total - 6.5) < 0.05


@pytest.mark.skipif(_ffmpeg_path() is None, reason="本机无 ffmpeg，跳过实跑冒烟")
def test_roll_transition_does_not_eat_duration_and_freezes_last_frame(tmp_path):
    """帧级验证：过渡不吞主段时长（主内容恰好从 pre_ms 起），段素材不足冻结末帧。

    前黑素材 1.5s、设定 3s、交叉溶解 600ms：
    - t=3.05s（过渡完成后）帧中心应为纯主段绿色；
    - t=2.85s（过渡中点）应为红绿混合而非黑主导（末帧冻结而非续黑）；
    - t=1.8s（素材已尽、过渡未开始）应为纯红（冻结的末帧）。
    """
    ffmpeg = _ffmpeg_path()

    def run(args: list[str]) -> None:
        subprocess.run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", *args], check=True)

    main_video = tmp_path / "main.mp4"
    pre_video = tmp_path / "pre.mp4"
    run(["-f", "lavfi", "-i", "color=c=0x00CC00:s=320x180:r=30:d=4",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(main_video)])
    run(["-f", "lavfi", "-i", "color=c=0xCC0000:s=320x180:r=30:d=1.5",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(pre_video)])
    job = RenderJob(
        track=_track(end_ms=3500),
        style=None,
        background_video_path=main_video,
        output_path=tmp_path / "out.mp4",
        background_source=BackgroundSource(kind="video", path=str(main_video)),
        width=320,
        height=180,
        fps=30,
        duration_ms=4000,
        output_format=OUTPUT_FORMAT_MP4,
        include_audio=False,
        pre_roll=RollSegmentConfig(
            enabled=True,
            source=BackgroundSource(kind="video", path=str(pre_video)),
            source_has_audio=False,
            duration_ms=3000,  # 素材 1.5s → 冻结末帧补到 3s
            main_transition_ms=600,
            main_transition_style="crossfade",
        ),
        main_media_end_ms=4000,
        main_background_has_audio=False,
    )
    command = build_render_command(ffmpeg, job)
    process = subprocess.Popen(command, stdin=subprocess.PIPE)
    blank = bytes(320 * 180 * 4)
    try:  # ffmpeg 在 -t 7s 达成后即退出，多余帧写入会断管
        for _ in range(30 * 8):
            process.stdin.write(blank)
        process.stdin.close()
    except BrokenPipeError:
        pass
    assert process.wait(timeout=60) == 0
    output = tmp_path / "out.mp4"

    def center_rgb_at(seconds: float) -> tuple[int, int, int]:
        png = tmp_path / f"frame_{int(seconds * 1000)}.png"
        run(["-ss", f"{seconds:.3f}", "-i", str(output), "-frames:v", "1", str(png)])
        from PyQt6.QtGui import QImage

        image = QImage(str(png))
        assert not image.isNull()
        color = image.pixelColor(image.width() // 2, image.height() // 2)
        return color.red(), color.green(), color.blue()

    # 素材已尽（1.5s）但过渡未开始（2.4s 起）→ 冻结的红色末帧。
    r, g, b = center_rgb_at(1.8)
    assert r > 140 and g < 80 and b < 80, f"expected frozen red frame, got {(r, g, b)}"
    # 过渡中点（2.7s）→ 红绿混合，绿分量显著存在（末帧冻结而非黑）。
    r, g, b = center_rgb_at(2.7)
    assert g > 60 and r > 60, f"expected red-green blend, got {(r, g, b)}"
    # 过渡完成（3.0s）后 → 主段内容恰好从 3s 开始：纯绿。
    r, g, b = center_rgb_at(3.05)
    assert g > 140 and r < 80 and b < 80, f"main should start exactly at 3s, got {(r, g, b)}"


@pytest.mark.skipif(_ffmpeg_path() is None, reason="本机无 ffmpeg")
@pytest.mark.parametrize("style", ["black", "white"])
def test_review_dip_transition_pixels_match_preview(style):
    """Adobe 式 Dip to Black/White：剪切点两侧各半，像素与预览 alpha 同口径。"""
    from krok_helper.subtitle_render.domain.roll import build_roll_timeline, roll_layer_alphas

    color = "white" if style == "white" else "black"
    config = RollSegmentConfig(
        enabled=True, duration_ms=1000,
        main_transition_ms=1000, main_transition_style=style,
    )
    plan = build_roll_timeline(config, None, track_end_ms=2000)
    command = [
        _ffmpeg_path(), "-v", "error",
        "-f", "lavfi", "-i", "color=red:s=16x16:r=60:d=1",
        "-f", "lavfi", "-i", "color=blue:s=16x16:r=60:d=2",
        "-filter_complex",
        f"[0:v]fade=t=out:st=0.5:d=0.5:color={color}[a];"
        f"[1:v]fade=t=in:st=0:d=0.5:color={color}[b];"
        "[a][b]concat=n=2:v=1:a=0,format=rgb24",
        "-t", "3", "-f", "rawvideo", "pipe:1",
    ]
    result = subprocess.run(command, capture_output=True, check=True, timeout=20)
    for t_ms in (750, 1000, 1250):
        alpha = roll_layer_alphas(plan, t_ms)
        expected = [
            255 * alpha.pre_alpha * (1 - alpha.main_alpha),
            0,
            255 * alpha.main_alpha,
        ]
        expected = [
            v * (1 - alpha.flash_alpha) + 255 * alpha.flash_alpha
            for v in expected
        ]
        index = t_ms * 60 // 1000
        actual = result.stdout[index * 768:index * 768 + 3]
        assert max(abs(v - e) for v, e in zip(actual, expected)) < 8, (
            t_ms, actual, expected,
        )


@pytest.mark.skipif(_ffmpeg_path() is None, reason="本机无 ffmpeg")
def test_dip_incoming_content_starts_at_cut_not_before(tmp_path):
    """居中 dip 只是把淡入放到剪切点之后，主段内容不得提前半窗播放。

    这一点直接决定字幕补偿：主段第 0 帧必须出现在 display=pre_ms，歌词
    +pre_ms 才对齐。用主视频前 0.2s 白、之后蓝来区分：
    剪切点后 150ms 若仍是白（灰白淡入），说明内容从第 0 帧开始；
    若提前了 300ms，这里会变成蓝。
    """
    ffmpeg = _ffmpeg_path()
    assert ffmpeg is not None

    def run(args: list[str]) -> None:
        subprocess.run(
            [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", *args],
            check=True,
        )

    main_video = tmp_path / "main.mp4"
    run([
        "-f", "lavfi", "-i", "color=c=white:s=64x64:r=30:d=0.2",
        "-f", "lavfi", "-i", "color=c=0x0000CC:s=64x64:r=30:d=2",
        "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]",
        "-map", "[v]", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(main_video),
    ])
    job = RenderJob(
        track=_track(end_ms=100),
        style=Style(),
        background_video_path=main_video,
        output_path=tmp_path / "out.mp4",
        background_source=BackgroundSource(kind="video", path=str(main_video)),
        width=64,
        height=64,
        fps=30,
        duration_ms=3000,
        include_audio=False,
        pre_roll=RollSegmentConfig(
            enabled=True,
            source=BackgroundSource(kind="solid", color="#000000"),
            duration_ms=1000,
            main_transition_ms=600,
            main_transition_style="black",
        ),
        main_media_end_ms=2200,
        main_background_has_audio=False,
    )
    command = build_render_command(ffmpeg, job)
    process = subprocess.Popen(command, stdin=subprocess.PIPE)
    try:
        for _ in range(30 * 4):
            process.stdin.write(bytes(64 * 64 * 4))
        process.stdin.close()
    except BrokenPipeError:
        pass
    assert process.wait(timeout=60) == 0

    def center_rgb_at(seconds: float) -> tuple[int, int, int]:
        png = tmp_path / f"frame_{int(seconds * 1000)}.png"
        run(["-ss", f"{seconds:.3f}", "-i", str(job.output_path), "-frames:v", "1", str(png)])
        from PyQt6.QtGui import QImage

        image = QImage(str(png))
        color = image.pixelColor(image.width() // 2, image.height() // 2)
        return color.red(), color.green(), color.blue()

    # 剪切点全黑。
    assert max(center_rgb_at(1.0)) < 20
    # 剪切点后 150ms：主段第 150ms 仍是白（前面 0.2s 白），淡入 alpha≈0.5。
    r, g, b = center_rgb_at(1.15)
    assert r > 90 and g > 90 and abs(r - b) < 30, (
        "main content must start at cut (frame 0), got", (r, g, b)
    )
    # 再往后主视频进入蓝色段，画面应明显偏蓝。
    r, g, b = center_rgb_at(1.30)
    assert b > 140 and r < 80 and g < 80, (r, g, b)


@pytest.mark.skipif(_ffmpeg_path() is None, reason="本机无 ffmpeg")
def test_review_dip_keeps_incoming_video_playing(tmp_path):
    """黑场淡入期间主视频必须继续前进（Adobe 行为），不能冻结在首帧。"""
    ffmpeg = _ffmpeg_path()

    def run(args: list[str]) -> None:
        subprocess.run(
            [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", *args],
            check=True,
        )

    main_video = tmp_path / "main.mp4"
    # 0~0.5s 绿，0.5s 之后蓝：过渡结束后若主视频冻结在首帧就会一直是绿。
    run([
        "-f", "lavfi", "-i", "color=c=0x00CC00:s=64x64:r=30:d=0.5",
        "-f", "lavfi", "-i", "color=c=0x0000CC:s=64x64:r=30:d=2",
        "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]",
        "-map", "[v]", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(main_video),
    ])
    job = RenderJob(
        track=_track(end_ms=500),
        style=Style(),
        background_video_path=main_video,
        output_path=tmp_path / "out.mp4",
        background_source=BackgroundSource(kind="video", path=str(main_video)),
        width=64,
        height=64,
        fps=30,
        duration_ms=3000,
        output_format=OUTPUT_FORMAT_MP4,
        include_audio=False,
        pre_roll=RollSegmentConfig(
            enabled=True,
            source=BackgroundSource(kind="solid", color="#000000"),
            duration_ms=1000,
            main_transition_ms=600,
            main_transition_style="black",
        ),
        main_media_end_ms=2500,
        main_background_has_audio=False,
    )
    command = build_render_command(ffmpeg, job)
    process = subprocess.Popen(command, stdin=subprocess.PIPE)
    try:
        for _ in range(30 * 4):
            process.stdin.write(bytes(64 * 64 * 4))
        process.stdin.close()
    except BrokenPipeError:
        pass
    assert process.wait(timeout=60) == 0

    def center_rgb_at(seconds: float) -> tuple[int, int, int]:
        png = tmp_path / f"frame_{int(seconds * 1000)}.png"
        run(["-ss", f"{seconds:.3f}", "-i", str(job.output_path), "-frames:v", "1", str(png)])
        from PyQt6.QtGui import QImage

        image = QImage(str(png))
        color = image.pixelColor(image.width() // 2, image.height() // 2)
        return color.red(), color.green(), color.blue()

    # 主段 0.45s（仍绿）且淡入已完成。
    r, g, b = center_rgb_at(1.45)
    assert g > 140 and r < 80 and b < 80, f"main should be playing green: {(r, g, b)}"
    # 主段 0.9s（应已切到蓝）：冻结首帧会仍是绿。
    r, g, b = center_rgb_at(1.90)
    assert b > 140 and r < 80 and g < 80, f"main should have advanced to blue: {(r, g, b)}"
