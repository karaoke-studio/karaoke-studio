"""图片序列小数/分数源帧率的真实 ffmpeg 换帧边界集成测试。

预览按 ``background_sequence_frame_index`` 的有理数向下取整选图；导出侧
image2 demuxer + ``fps`` 滤镜重采样必须给出同一边界。``fps`` 滤镜默认
``round=near`` 会把源帧起点映射到最近的输出 tick——29.97@60 下第二张本应
33.3667ms 出现却提前到 33.3333ms（实测缺陷）——因此图片序列的重采样统一
``round=up``（源帧 k 首现于 ``ceil(k·r_o/r_s)`` tick，与预览一致）。本文件
用真实 ffmpeg 解码输出帧取色验证，而不只检查 ``-framerate`` 参数。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from krok_helper.subtitle_render.domain.background import (
    BackgroundSource,
    background_sequence_frame_index,
)
from krok_helper.subtitle_render.domain.models import Style
from krok_helper.subtitle_render.domain.roll import RollSegmentConfig
from krok_helper.subtitle_render.domain.timing import TimingTrack
from krok_helper.subtitle_render.engine.export.export_command import (
    background_input_args,
    background_resample_filter,
    roll_segment_input_args,
)
from krok_helper.subtitle_render.engine.export.render_job import RenderJob

FFMPEG = shutil.which("ffmpeg")

pytestmark = pytest.mark.skipif(FFMPEG is None, reason="需要 PATH 上的真实 ffmpeg")

# 3 色序列（红/绿/蓝）；断言按 ffmpeg rawvideo 的 bgr24 主通道分类。
_SEQUENCE_HEX = ("0xC00000", "0x00C000", "0x0000C0")
_SIZE = 8
_FRAME_BYTES = _SIZE * _SIZE * 3


def _write_color_sequence(directory: Path, count: int = 3) -> Path:
    for index in range(count):
        subprocess.run(
            [
                FFMPEG, "-y", "-loglevel", "error",
                "-f", "lavfi", "-i", f"color=c={_SEQUENCE_HEX[index]}:s={_SIZE}x{_SIZE}",
                "-frames:v", "1", str(directory / f"frame_{index:04d}.png"),
            ],
            check=True,
        )
    return directory / "frame_%04d.png"


def _decode_ticks(input_args: list[str], fps_filter: str, ticks: list[int]) -> dict[int, int]:
    """按生产侧输入参数 + 重采样滤镜跑真实 ffmpeg，返回 {tick: 颜色序号}。"""
    total = max(ticks) + 1
    raw = subprocess.run(
        [
            FFMPEG, "-loglevel", "error", *input_args,
            "-vf", fps_filter, "-pix_fmt", "bgr24", "-f", "rawvideo",
            "-frames:v", str(total), "-",
        ],
        capture_output=True,
        check=True,
    ).stdout
    assert len(raw) >= total * _FRAME_BYTES
    decoded: dict[int, int] = {}
    for tick in ticks:
        # PNG 编解码对 0xC0 有 ±1 舍入，按主通道分类而非精确字节比较。
        pixel = raw[tick * _FRAME_BYTES : tick * _FRAME_BYTES + 3]
        blue, green, red = pixel[0], pixel[1], pixel[2]
        decoded[tick] = max(range(3), key=lambda i: (red, green, blue)[i])
    return decoded


def _expected_preview_color(source: BackgroundSource, tick: int, frame_count: int) -> int:
    """预览在输出 tick i（时刻 i/60s，毫秒向下取整）应显示的颜色序号。

    注意：floor-ms 口径只对「源帧率 ≤ 输出帧率」的用例公平（本文件全部
    如此）；源帧率高于输出帧率（如 120fps 源 @60fps 输出的抽帧）时，毫秒
    截断会落到另一抽帧相位上，扩展用例前勿直接沿用本约定。
    """
    t_ms = tick * 1000 // 60
    return background_sequence_frame_index(source, t_ms) % frame_count


def _job(fps: int | float) -> RenderJob:
    return RenderJob(
        track=TimingTrack(lines=[]),
        style=Style(),
        background_video_path=None,
        output_path=Path("out.mp4"),
        fps=fps,
    )


def test_fractional_sequence_switch_boundary_matches_preview(tmp_path: Path) -> None:
    """29.97@60：near 的「提前一帧换图」缺陷必须被 round=up 修正。"""
    pattern = _write_color_sequence(tmp_path)
    source = BackgroundSource(
        kind="image_sequence", path=str(pattern), source_fps=29.97
    )
    job = _job(60)
    input_args = background_input_args(source, job, 10.0)

    decoded = _decode_ticks(
        input_args, background_resample_filter(job, "image_sequence"), [0, 1, 2, 3, 4]
    )

    # 用户实测场景：33.3333ms（tick 2）仍应是第一张，50ms（tick 3）起第二张。
    assert decoded[2] == 0
    assert decoded[3] == 1
    for tick, color in decoded.items():
        assert color == _expected_preview_color(source, tick, 3), (
            f"tick {tick} 导出颜色 {color} 与预览 "
            f"{_expected_preview_color(source, tick, 3)} 不一致"
        )


def test_slow_sequence_exact_boundary_and_loop(tmp_path: Path) -> None:
    """0.5@60 整除边界（tick 120 恰好换张）与 3 张循环不回归。"""
    pattern = _write_color_sequence(tmp_path)
    source = BackgroundSource(kind="image_sequence", path=str(pattern), source_fps=0.5)
    job = _job(60)

    decoded = _decode_ticks(
        input_args := background_input_args(source, job, 20.0),
        background_resample_filter(job, "image_sequence"),
        [119, 120, 239, 240, 360],
    )
    assert input_args[input_args.index("-framerate") + 1] == "1/2"

    assert decoded[119] == 0 and decoded[120] == 1  # 2000ms 整除边界
    assert decoded[239] == 1 and decoded[240] == 2
    assert decoded[360] == 0  # 6000ms：3 张序列循环回第一张
    for tick, color in decoded.items():
        assert color == _expected_preview_color(source, tick, 3)


def test_roll_segment_sequence_boundary_matches_preview(tmp_path: Path) -> None:
    """前黑/后黑段序列与主背景走同一 round=up 边界。"""
    pattern = _write_color_sequence(tmp_path)
    config = RollSegmentConfig(
        source=BackgroundSource(
            kind="image_sequence", path=str(pattern), source_fps=23.976
        )
    )
    job = _job(60)

    decoded = _decode_ticks(
        roll_segment_input_args(config, job, 4.0),
        background_resample_filter(job, "image_sequence"),
        [0, 1, 2, 3, 4, 5],
    )

    for tick, color in decoded.items():
        assert color == _expected_preview_color(config.source, tick, 3), (
            f"roll 段 tick {tick} 导出颜色 {color} 与预览不一致"
        )


def test_default_near_rounding_would_switch_one_frame_early(tmp_path: Path) -> None:
    """反向钉住缺陷根因：默认 round=near 在 29.97@60 下 tick 2 就换第二张。

    该用例固定「为什么需要 round=up」的证据；若未来 ffmpeg 改默认舍入，
    这里失败即提示可以重新评估统一方式。
    """
    pattern = _write_color_sequence(tmp_path)
    source = BackgroundSource(
        kind="image_sequence", path=str(pattern), source_fps=29.97
    )
    job = _job(60)

    decoded = _decode_ticks(background_input_args(source, job, 10.0), "fps=60", [2])

    assert decoded[2] == 1  # near：33.3333ms 提前换图（与预览的 0 不一致）
