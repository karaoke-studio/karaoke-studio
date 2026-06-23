"""字幕渲染管线基准测量脚手架（报告 §2 / P0.0）。

目的：在动手优化（P0.1 烘焙已唱层 / P0.2 条带 / P0.3 多进程 / P1 解耦）**之前**，
建立一个可复现、无 GUI、可 A/B 的单帧光栅化基准，用来：

- 量化每帧 ``paint_frame`` 的光栅化耗时与 ``constBits`` 拷贝耗时（导出热点，见报告 §1.1/§2）；
- 覆盖全部特效族（普通 / 分色 roles / glow / ruby / Sayatoo 信号 / 全家桶），
  这样任何一处优化都能立刻看到收益、也能防止改完更慢（性能门禁）。

用法::

    python scripts/bench_render.py                 # 全场景 1080p，输出 CSV + 表格
    python scripts/bench_render.py -s full roles   # 仅跑指定场景
    python scripts/bench_render.py -W 960 -H 540    # 预览分辨率档
    python scripts/bench_render.py --frames 240 --out my.csv

输出每个场景：alloc+fill / paint / copy / total 的均值与 p50/p95，以及隐含的"导出
上限 fps"（= 1000 / total_mean）。对照同一命令在优化前后的 CSV 即可评估收益。

注意：本脚本只测 CPU 光栅化热点（不含 ffmpeg 编码 / pipe 带宽），这正是报告判定的
最大、最易摘的果子。端到端导出 fps 留给集成测试（需背景视频 + ffmpeg）。
"""

from __future__ import annotations

import argparse
import csv
import os
import statistics
import sys
import time
from dataclasses import dataclass
from pathlib import Path

# 字体光栅化需要 QGuiApplication；offscreen 平台让脚本能在无显示器 / CI 下跑。
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# Windows 控制台默认 GBK，表格里的中文 note 会乱码；尽力切到 UTF-8（失败则忽略）。
try:  # pragma: no cover - 仅影响终端显示
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

# 允许从仓库根目录直接 `python scripts/bench_render.py` 运行。
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from PyQt6.QtGui import QColor, QImage  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from krok_helper.subtitle_render.engine.painter import (  # noqa: E402
    clear_before_layer_cache,
    paint_frame,
)
from krok_helper.subtitle_render.models import (  # noqa: E402
    KaraokeColors,
    KaraokeColorState,
    PaintFill,
    RubyAnnotation,
    Style,
    SubtitleStyleScheme,
    TimingChar,
    TimingLine,
    TimingTrack,
)

# 一句歌词的字符数（贴近真实卡拉OK，影响行宽 → 光栅化量）。
_LINE_CHARS = "君と見た夏の空を今でも覚えている"
_CHAR_MS = 280  # 每字演唱时长
_LINE_START_MS = 1000


def _solid(color: str) -> PaintFill:
    return PaintFill(
        mode="solid",
        color=color,
        start_color=color,
        end_color=color,
        split_top_color=color,
        split_bottom_color=color,
    )


def _line(
    text: str = _LINE_CHARS,
    *,
    start_ms: int = _LINE_START_MS,
    roles: tuple[str, ...] | None = None,
    singer_id: int = 0,
) -> TimingLine:
    chars: list[TimingChar] = []
    t = start_ms
    for index, ch in enumerate(text):
        role = roles[index % len(roles)] if roles else None
        chars.append(TimingChar(text=ch, start_ms=t, role_label=role))
        t += _CHAR_MS
    return TimingLine(chars=chars, end_ms=t, singer_id=singer_id)


def _base_style(**overrides) -> Style:
    style = Style(
        font_family="Yu Gothic",
        font_family_latin="Arial",
        font_size_px=72,
        line_y_position="center",
        line_lead_in_ms=1000,
    )
    return style if not overrides else _replace(style, **overrides)


def _replace(style: Style, **overrides) -> Style:
    from dataclasses import replace

    return replace(style, **overrides)


def _glow_colors() -> KaraokeColors:
    glow = _solid("#FF8A00")
    return KaraokeColors(
        before=KaraokeColorState(text=_solid("#FFFFFF"), stroke=_solid("#222222"), shadow=glow),
        after=KaraokeColorState(text=_solid("#FFE08A"), stroke=_solid("#222222"), shadow=glow),
    )


@dataclass(frozen=True)
class Scenario:
    name: str
    track: TimingTrack
    style: Style
    note: str


def _build_scenarios() -> dict[str, Scenario]:
    plain_style = _base_style()
    plain_track = TimingTrack(lines=[_line()])

    roles_schemes = {
        "主唱": SubtitleStyleScheme(
            font_size_px=84,
            karaoke_colors=KaraokeColors(
                before=KaraokeColorState(text=_solid("#FFFFFF")),
                after=KaraokeColorState(text=_solid("#FF3366")),
            ),
        ),
        "和声": SubtitleStyleScheme(
            font_size_px=60,
            karaoke_colors=KaraokeColors(
                before=KaraokeColorState(text=_solid("#CFE8FF")),
                after=KaraokeColorState(text=_solid("#3399FF")),
            ),
        ),
    }
    roles_track = TimingTrack(lines=[_line(roles=("主唱", "和声"))])
    roles_style = _base_style(custom_style_schemes=roles_schemes)

    glow_style = _base_style(decoration_kind="glow", glow_radius_px=14, karaoke_colors=_glow_colors())
    glow_track = TimingTrack(lines=[_line()])

    ruby_track = TimingTrack(
        lines=[_line("漢字練習")],
        rubies=[
            RubyAnnotation(kanji="漢字", reading="かんじ", pos_start_ms=_LINE_START_MS, pos_end_ms=_LINE_START_MS + 2 * _CHAR_MS),
            RubyAnnotation(kanji="練習", reading="れんしゅう", pos_start_ms=_LINE_START_MS + 2 * _CHAR_MS, pos_end_ms=_LINE_START_MS + 4 * _CHAR_MS),
        ],
    )
    ruby_style = _base_style(ruby_font_size_px=28)

    signals_style = _base_style(
        lit_enabled=True,
        signals_duration_ms=1500,
        dual_line_layout=False,
    )
    signals_track = TimingTrack(lines=[_line(singer_id=0)])

    # 全家桶：分色 + glow + ruby + 信号 + 双行，最接近"重特效"真实负载。
    full_track = TimingTrack(
        lines=[_line(roles=("主唱", "和声"), singer_id=0), _line("次の行も同時に表示中", start_ms=_LINE_START_MS)],
        rubies=[
            RubyAnnotation(kanji="夏", reading="なつ", pos_start_ms=_LINE_START_MS + 3 * _CHAR_MS, pos_end_ms=_LINE_START_MS + 4 * _CHAR_MS),
        ],
    )
    full_style = _base_style(
        decoration_kind="glow",
        glow_radius_px=12,
        karaoke_colors=_glow_colors(),
        custom_style_schemes=roles_schemes,
        lit_enabled=True,
        signals_duration_ms=1500,
        ruby_font_size_px=26,
    )

    scenarios = [
        Scenario("plain", plain_track, plain_style, "单字体整行（基准）"),
        Scenario("roles", roles_track, roles_style, "句内分色逐段（混排字号）"),
        Scenario("glow", glow_track, glow_style, "发光装饰"),
        Scenario("ruby", ruby_track, ruby_style, "ふりがな注音"),
        Scenario("signals", signals_track, signals_style, "Sayatoo 信号倒计时"),
        Scenario("full", full_track, full_style, "分色+glow+ruby+信号+双行"),
    ]
    return {s.name: s for s in scenarios}


def _sample_timestamps(track: TimingTrack, frames: int) -> list[int]:
    """在 track 的可见窗口里均匀取样，覆盖未唱/走字中/已唱三态。"""
    starts = [c.start_ms for line in track.lines for c in line.chars]
    ends = [line.end_ms for line in track.lines if line.end_ms]
    lo = (min(starts) if starts else 0) - 1500
    hi = (max(ends) if ends else 3000) + 1500
    span = max(hi - lo, 1)
    return [int(lo + span * i / max(frames - 1, 1)) for i in range(frames)]


@dataclass
class FrameTimes:
    alloc_fill: list[float]
    paint: list[float]
    copy: list[float]
    total: list[float]


def _bench_scenario(scenario: Scenario, width: int, height: int, frames: int) -> FrameTimes:
    timestamps = _sample_timestamps(scenario.track, frames)
    times = FrameTimes([], [], [], [])

    # 预热：建满 before-layer / glow / 图片填充缓存，测的是稳态（A1 优化对比的就是稳态）。
    clear_before_layer_cache()
    for t_ms in timestamps:
        warm = QImage(width, height, QImage.Format.Format_RGBA8888)
        warm.fill(QColor(0, 0, 0, 0))
        paint_frame(warm, scenario.track, t_ms, scenario.style)

    transparent = QColor(0, 0, 0, 0)
    for t_ms in timestamps:
        t0 = time.perf_counter()
        image = QImage(width, height, QImage.Format.Format_RGBA8888)
        image.fill(transparent)
        t1 = time.perf_counter()
        paint_frame(image, scenario.track, t_ms, scenario.style)
        t2 = time.perf_counter()
        bits = image.constBits()
        bits.setsize(image.sizeInBytes())
        _ = bytes(bits)
        t3 = time.perf_counter()
        times.alloc_fill.append((t1 - t0) * 1000.0)
        times.paint.append((t2 - t1) * 1000.0)
        times.copy.append((t3 - t2) * 1000.0)
        times.total.append((t3 - t0) * 1000.0)
    return times


def _p(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))
    return ordered[idx]


@dataclass
class Row:
    scenario: str
    note: str
    frames: int
    width: int
    height: int
    alloc_fill_mean: float
    paint_mean: float
    paint_p50: float
    paint_p95: float
    copy_mean: float
    total_mean: float
    total_p95: float
    implied_fps: float


def _summarize(scenario: Scenario, times: FrameTimes, width: int, height: int) -> Row:
    total_mean = statistics.fmean(times.total) if times.total else 0.0
    return Row(
        scenario=scenario.name,
        note=scenario.note,
        frames=len(times.total),
        width=width,
        height=height,
        alloc_fill_mean=statistics.fmean(times.alloc_fill) if times.alloc_fill else 0.0,
        paint_mean=statistics.fmean(times.paint) if times.paint else 0.0,
        paint_p50=_p(times.paint, 0.5),
        paint_p95=_p(times.paint, 0.95),
        copy_mean=statistics.fmean(times.copy) if times.copy else 0.0,
        total_mean=total_mean,
        total_p95=_p(times.total, 0.95),
        implied_fps=(1000.0 / total_mean) if total_mean > 0 else 0.0,
    )


_CSV_FIELDS = [
    "scenario", "note", "frames", "width", "height",
    "alloc_fill_mean_ms", "paint_mean_ms", "paint_p50_ms", "paint_p95_ms",
    "copy_mean_ms", "total_mean_ms", "total_p95_ms", "implied_fps",
]


def _row_dict(row: Row) -> dict:
    return {
        "scenario": row.scenario,
        "note": row.note,
        "frames": row.frames,
        "width": row.width,
        "height": row.height,
        "alloc_fill_mean_ms": f"{row.alloc_fill_mean:.4f}",
        "paint_mean_ms": f"{row.paint_mean:.4f}",
        "paint_p50_ms": f"{row.paint_p50:.4f}",
        "paint_p95_ms": f"{row.paint_p95:.4f}",
        "copy_mean_ms": f"{row.copy_mean:.4f}",
        "total_mean_ms": f"{row.total_mean:.4f}",
        "total_p95_ms": f"{row.total_p95:.4f}",
        "implied_fps": f"{row.implied_fps:.1f}",
    }


def _print_table(rows: list[Row]) -> None:
    header = f"{'scenario':<9} {'paint':>9} {'p95':>9} {'copy':>8} {'total':>9} {'fps':>7}  note"
    print(header)
    print("-" * len(header))
    for r in rows:
        print(
            f"{r.scenario:<9} {r.paint_mean:>8.3f}m {r.paint_p95:>8.3f}m {r.copy_mean:>7.3f}m "
            f"{r.total_mean:>8.3f}m {r.implied_fps:>7.1f}  {r.note}"
        )


def main(argv: list[str] | None = None) -> int:
    scenarios = _build_scenarios()
    parser = argparse.ArgumentParser(description="字幕渲染单帧光栅化基准（报告 §2 / P0.0）")
    parser.add_argument("-s", "--scenarios", nargs="+", choices=list(scenarios), default=list(scenarios),
                        help="要跑的场景（默认全部）")
    parser.add_argument("-W", "--width", type=int, default=1920)
    parser.add_argument("-H", "--height", type=int, default=1080)
    parser.add_argument("--frames", type=int, default=120, help="每场景取样帧数（覆盖未唱/走字/已唱）")
    parser.add_argument("--out", type=Path, default=None, help="CSV 输出路径（默认 .bench/bench_render_<时间戳>.csv）")
    args = parser.parse_args(argv)

    _ = QApplication.instance() or QApplication([])

    rows: list[Row] = []
    for name in args.scenarios:
        scenario = scenarios[name]
        times = _bench_scenario(scenario, args.width, args.height, args.frames)
        rows.append(_summarize(scenario, times, args.width, args.height))

    _print_table(rows)

    out = args.out
    if out is None:
        bench_dir = _REPO_ROOT / ".bench"
        bench_dir.mkdir(exist_ok=True)
        out = bench_dir / f"bench_render_{time.strftime('%Y%m%d_%H%M%S')}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=_CSV_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow(_row_dict(row))
    print(f"\nCSV -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
