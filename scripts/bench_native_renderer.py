"""Compare Python QPainter rendering with the native subtitle renderer sidecar.

This is the first C4-4 harness: it uses the same project/style/track input for
both paths and records per-frame timings plus native cache diagnostics.  The
native path currently writes PNG files through the C1/C4 smoke protocol, so its
timings include PNG encode and disk write overhead.  Treat the result as an
early A/B signal, not the final export or preview throughput number.
"""

from __future__ import annotations

import argparse
import csv
import os
import statistics
import sys
import tempfile
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:  # pragma: no cover - terminal display only
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

DEFAULT_PROJECT = Path(r"D:\カラオケ\songs\A stain\A stain.yurika")


@dataclass(frozen=True)
class TimingSample:
    t_ms: int
    python_ms: float
    native_ms: float
    native_cache_hits: int
    native_cache_misses: int


def _sample_timestamps(*, start_ms: int, frames: int, fps: int) -> list[int]:
    frame_ms = 1000.0 / max(int(fps), 1)
    return [int(round(start_ms + index * frame_ms)) for index in range(max(int(frames), 0))]


def _mean(values: Iterable[float]) -> float:
    items = list(values)
    return statistics.fmean(items) if items else 0.0


def _percentile(values: Iterable[float], q: float) -> float:
    items = sorted(values)
    if not items:
        return 0.0
    index = min(len(items) - 1, max(0, int(round(q * (len(items) - 1)))))
    return items[index]


def _summarize_samples(
    samples: list[TimingSample],
    *,
    scenario: str,
    width: int,
    height: int,
) -> dict[str, str | int]:
    python_values = [sample.python_ms for sample in samples]
    native_values = [sample.native_ms for sample in samples]
    python_mean = _mean(python_values)
    native_mean = _mean(native_values)
    return {
        "scenario": scenario,
        "frames": len(samples),
        "width": width,
        "height": height,
        "python_mean_ms": f"{python_mean:.4f}",
        "python_p50_ms": f"{_percentile(python_values, 0.5):.4f}",
        "python_p95_ms": f"{_percentile(python_values, 0.95):.4f}",
        "native_mean_ms": f"{native_mean:.4f}",
        "native_p50_ms": f"{_percentile(native_values, 0.5):.4f}",
        "native_p95_ms": f"{_percentile(native_values, 0.95):.4f}",
        "speedup": f"{(python_mean / native_mean) if native_mean > 0 else 0.0:.2f}",
        "native_cache_hits": max((sample.native_cache_hits for sample in samples), default=0),
        "native_cache_misses": max((sample.native_cache_misses for sample in samples), default=0),
    }


def _load_project(project_path: Path):
    from krok_helper.subtitle_render.models import style_from_dict
    from krok_helper.subtitle_render.project_store import load_render_project
    from krok_helper.subtitle_render.subtitle_sources import load_nicokara_lrc

    data = load_render_project(project_path)
    style = style_from_dict(data.get("style"))
    track = load_nicokara_lrc(Path(data["subtitle_path"]))
    screen = data.get("screen", {})
    width = int(screen.get("width", 1920))
    height = int(screen.get("height", 1080))
    fps = int(screen.get("fps", 60))
    return data, track, style, width, height, fps


def _bench_project(args: argparse.Namespace) -> tuple[dict[str, str | int], list[TimingSample]]:
    from PyQt6.QtGui import QImage
    from PyQt6.QtWidgets import QApplication

    from krok_helper.subtitle_render.engine.painter import clear_before_layer_cache, paint_frame
    from krok_helper.subtitle_render.native_backend import NativeRendererProcess, resolve_native_renderer_path

    project_path = Path(args.project)
    if not project_path.is_file():
        raise FileNotFoundError(f"项目文件不存在：{project_path}")

    _data, track, style, width, height, fps = _load_project(project_path)
    if args.width:
        width = int(args.width)
    if args.height:
        height = int(args.height)
    if args.fps:
        fps = int(args.fps)
    if not args.keep_project_style:
        style = replace(style, entry_anim="utopia", exit_anim="utopia", decoration_kind="glow")

    renderer_path = resolve_native_renderer_path(root=_REPO_ROOT)
    if renderer_path is None:
        raise FileNotFoundError("native subtitle renderer executable was not found; run scripts/run_native_renderer_smoke.ps1 first")

    _ = QApplication.instance() or QApplication([])
    timestamps = _sample_timestamps(start_ms=args.start, frames=args.frames, fps=fps)
    warmup_timestamps = _sample_timestamps(start_ms=args.start, frames=args.warmup, fps=fps)

    clear_before_layer_cache()
    for t_ms in warmup_timestamps:
        image = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(0)
        paint_frame(image, track, t_ms, style)

    output_dir = Path(args.png_dir) if args.png_dir else Path(tempfile.mkdtemp(prefix="krok-native-bench-"))
    output_dir.mkdir(parents=True, exist_ok=True)

    samples: list[TimingSample] = []
    with NativeRendererProcess(renderer_path, response_timeout_s=args.timeout, close_timeout_s=2.0) as renderer:
        renderer.configure(track, style, width=width, height=height, fps=fps)
        for index, t_ms in enumerate(warmup_timestamps):
            renderer.render_frame_png(t_ms, output_dir / f"warmup-{index:04d}.png")

        for index, t_ms in enumerate(timestamps):
            image = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
            image.fill(0)
            py_start = time.perf_counter()
            paint_frame(image, track, t_ms, style)
            python_ms = (time.perf_counter() - py_start) * 1000.0

            native_start = time.perf_counter()
            response = renderer.render_frame_png(t_ms, output_dir / f"frame-{index:04d}.png")
            native_ms = (time.perf_counter() - native_start) * 1000.0

            samples.append(
                TimingSample(
                    t_ms=t_ms,
                    python_ms=python_ms,
                    native_ms=native_ms,
                    native_cache_hits=int(response.get("glow_cache_hits", 0)),
                    native_cache_misses=int(response.get("glow_cache_misses", 0)),
                )
            )

    summary = _summarize_samples(samples, scenario=project_path.stem, width=width, height=height)
    if not args.keep_png and not args.png_dir:
        for path in output_dir.glob("*.png"):
            path.unlink(missing_ok=True)
        output_dir.rmdir()
    return summary, samples


_CSV_FIELDS = [
    "scenario",
    "frames",
    "width",
    "height",
    "python_mean_ms",
    "python_p50_ms",
    "python_p95_ms",
    "native_mean_ms",
    "native_p50_ms",
    "native_p95_ms",
    "speedup",
    "native_cache_hits",
    "native_cache_misses",
]


def _write_csv(path: Path, rows: list[dict[str, str | int]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=_CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def _print_summary(row: dict[str, str | int]) -> None:
    print("scenario          :", row["scenario"])
    print("frames / size     :", row["frames"], f"{row['width']}x{row['height']}")
    print("python mean / p95 :", f"{row['python_mean_ms']} ms", f"/ {row['python_p95_ms']} ms")
    print("native mean / p95 :", f"{row['native_mean_ms']} ms", f"/ {row['native_p95_ms']} ms")
    print("speedup           :", f"{row['speedup']}x")
    print("native glow cache :", f"hits={row['native_cache_hits']}", f"misses={row['native_cache_misses']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="C4-4 native-vs-Python subtitle renderer benchmark")
    parser.add_argument("project", nargs="?", default=str(DEFAULT_PROJECT), help=".yurika 项目文件，默认 A stain")
    parser.add_argument("--start", type=int, default=92000, help="起始时间 ms")
    parser.add_argument("--frames", type=int, default=60, help="测量帧数")
    parser.add_argument("--warmup", type=int, default=10, help="预热帧数")
    parser.add_argument("--width", type=int, default=None, help="覆盖项目宽度")
    parser.add_argument("--height", type=int, default=None, help="覆盖项目高度")
    parser.add_argument("--fps", type=int, default=None, help="覆盖项目 fps")
    parser.add_argument("--timeout", type=float, default=10.0, help="native 单帧响应超时秒数")
    parser.add_argument("--keep-project-style", action="store_true", help="不强制覆盖为 utopia + glow")
    parser.add_argument("--png-dir", type=Path, default=None, help="保留 native PNG 输出到指定目录")
    parser.add_argument("--keep-png", action="store_true", help="保留临时 native PNG 输出")
    parser.add_argument("--out", type=Path, default=None, help="CSV 输出路径，默认 .bench/native_renderer_<时间>.csv")
    args = parser.parse_args(argv)

    summary, _samples = _bench_project(args)
    _print_summary(summary)

    out = args.out
    if out is None:
        bench_dir = _REPO_ROOT / ".bench"
        bench_dir.mkdir(exist_ok=True)
        out = bench_dir / f"native_renderer_{time.strftime('%Y%m%d_%H%M%S')}.csv"
    _write_csv(out, [summary])
    print(f"CSV -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
