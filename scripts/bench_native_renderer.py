"""Compare Python QPainter rendering with the native subtitle renderer sidecar.

The default native path uses the C4-4 ``render_frame_stats`` protocol, which
renders in the sidecar and returns diagnostics without PNG encoding or disk
I/O.  ``--native-mode png`` keeps the older smoke path available for regression
checks against saved images.
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
from contextlib import contextmanager
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
    native_render_ms: float = 0.0
    frame_index: int = 0
    cache_hit_delta: int = 0
    cache_miss_delta: int = 0
    cache_shape_miss_delta: int = 0
    cache_content_variant_miss_delta: int = 0
    cache_evicted_key_miss_delta: int = 0
    native_cache_shape_misses: int = 0
    native_cache_content_variant_misses: int = 0
    native_cache_evicted_key_misses: int = 0
    cache_scope_miss_delta: str = ""
    native_cache_misses_by_scope: str = ""
    cache_mode: str = "on"
    native_mode: str = "stats"


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


@contextmanager
def _native_glow_cache_mode(cache_mode: str):
    names = ("KROK_SUBTITLE_NATIVE_GLOW_CACHE", "KROK_SUBTITLE_GLOW_CACHE")
    previous = {name: os.environ.get(name) for name in names}
    if cache_mode == "off":
        os.environ["KROK_SUBTITLE_NATIVE_GLOW_CACHE"] = "0"
    else:
        for name in names:
            os.environ.pop(name, None)
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _int_map(value: object) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, int] = {}
    for key, item in value.items():
        if isinstance(key, str):
            try:
                out[key] = int(item)
            except (TypeError, ValueError):
                continue
    return out


def _map_delta(current: dict[str, int], previous: dict[str, int]) -> dict[str, int]:
    keys = set(current) | set(previous)
    return {key: delta for key in sorted(keys) if (delta := current.get(key, 0) - previous.get(key, 0)) > 0}


def _format_counts(values: dict[str, int]) -> str:
    return ";".join(f"{key}={values[key]}" for key in sorted(values))


def _summarize_samples(
    samples: list[TimingSample],
    *,
    scenario: str,
    width: int,
    height: int,
    cache_mode: str = "on",
    native_mode: str = "stats",
) -> dict[str, str | int]:
    python_values = [sample.python_ms for sample in samples]
    native_values = [sample.native_ms for sample in samples]
    native_render_values = [sample.native_render_ms for sample in samples if sample.native_render_ms > 0.0]
    python_mean = _mean(python_values)
    native_mean = _mean(native_values)
    native_render_mean = _mean(native_render_values)
    return {
        "scenario": scenario,
        "cache_mode": cache_mode,
        "native_mode": native_mode,
        "frames": len(samples),
        "width": width,
        "height": height,
        "python_mean_ms": f"{python_mean:.4f}",
        "python_p50_ms": f"{_percentile(python_values, 0.5):.4f}",
        "python_p95_ms": f"{_percentile(python_values, 0.95):.4f}",
        "native_mean_ms": f"{native_mean:.4f}",
        "native_p50_ms": f"{_percentile(native_values, 0.5):.4f}",
        "native_p95_ms": f"{_percentile(native_values, 0.95):.4f}",
        "native_render_mean_ms": f"{native_render_mean:.4f}",
        "native_render_p95_ms": f"{_percentile(native_render_values, 0.95):.4f}",
        "speedup": f"{(python_mean / native_mean) if native_mean > 0 else 0.0:.2f}",
        "render_speedup": f"{(python_mean / native_render_mean) if native_render_mean > 0 else 0.0:.2f}",
        "native_cache_hits": max((sample.native_cache_hits for sample in samples), default=0),
        "native_cache_misses": max((sample.native_cache_misses for sample in samples), default=0),
        "native_cache_hit_delta": sum(sample.cache_hit_delta for sample in samples),
        "native_cache_miss_delta": sum(sample.cache_miss_delta for sample in samples),
        "native_cache_shape_misses": max((sample.native_cache_shape_misses for sample in samples), default=0),
        "native_cache_content_variant_misses": max(
            (sample.native_cache_content_variant_misses for sample in samples),
            default=0,
        ),
        "native_cache_evicted_key_misses": max((sample.native_cache_evicted_key_misses for sample in samples), default=0),
        "native_cache_shape_miss_delta": sum(sample.cache_shape_miss_delta for sample in samples),
        "native_cache_content_variant_miss_delta": sum(sample.cache_content_variant_miss_delta for sample in samples),
        "native_cache_evicted_key_miss_delta": sum(sample.cache_evicted_key_miss_delta for sample in samples),
        "native_cache_misses_by_scope": samples[-1].native_cache_misses_by_scope if samples else "",
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


def _bench_project(args: argparse.Namespace, *, cache_mode: str) -> tuple[dict[str, str | int], list[TimingSample]]:
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
    with _native_glow_cache_mode(cache_mode):
        with NativeRendererProcess(renderer_path, response_timeout_s=args.timeout, close_timeout_s=2.0) as renderer:
            renderer.configure(track, style, width=width, height=height, fps=fps)
            previous_hits = 0
            previous_misses = 0
            previous_shape_misses = 0
            previous_content_variant_misses = 0
            previous_evicted_key_misses = 0
            previous_scope_misses: dict[str, int] = {}
            for index, t_ms in enumerate(warmup_timestamps):
                if args.native_mode == "png":
                    warmup_response = renderer.render_frame_png(t_ms, output_dir / f"{cache_mode}-warmup-{index:04d}.png")
                else:
                    warmup_response = renderer.render_frame_stats(t_ms)
                previous_hits = int(warmup_response.get("glow_cache_hits", previous_hits))
                previous_misses = int(warmup_response.get("glow_cache_misses", previous_misses))
                previous_shape_misses = int(warmup_response.get("glow_cache_shape_misses", previous_shape_misses))
                previous_content_variant_misses = int(
                    warmup_response.get("glow_cache_content_variant_misses", previous_content_variant_misses)
                )
                previous_evicted_key_misses = int(
                    warmup_response.get("glow_cache_evicted_key_misses", previous_evicted_key_misses)
                )
                previous_scope_misses = _int_map(warmup_response.get("glow_cache_misses_by_scope"))

            python_values: list[float] = []
            for t_ms in timestamps:
                image = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
                image.fill(0)
                py_start = time.perf_counter()
                paint_frame(image, track, t_ms, style)
                python_values.append((time.perf_counter() - py_start) * 1000.0)

            if args.native_mode == "range":
                native_start = time.perf_counter()
                response = renderer.render_range_stats(timestamps, threads=args.range_threads)
                native_ms_total = (time.perf_counter() - native_start) * 1000.0
                frame_stats = response.get("frame_stats")
                if not isinstance(frame_stats, list):
                    frame_stats = []
                native_ms_per_frame = native_ms_total / max(len(timestamps), 1)
                cache_hits = int(response.get("glow_cache_hits", 0))
                cache_misses = int(response.get("glow_cache_misses", 0))
                shape_misses = int(response.get("glow_cache_shape_misses", 0))
                content_variant_misses = int(response.get("glow_cache_content_variant_misses", 0))
                evicted_key_misses = int(response.get("glow_cache_evicted_key_misses", 0))
                scope_misses = _int_map(response.get("glow_cache_misses_by_scope"))
                scope_miss_delta = _map_delta(scope_misses, previous_scope_misses)
                for index, t_ms in enumerate(timestamps):
                    item = frame_stats[index] if index < len(frame_stats) and isinstance(frame_stats[index], dict) else {}
                    is_last = index == len(timestamps) - 1
                    samples.append(
                        TimingSample(
                            t_ms=t_ms,
                            python_ms=python_values[index],
                            native_ms=native_ms_per_frame,
                            native_render_ms=float(item.get("render_ms", 0.0)),
                            native_cache_hits=cache_hits,
                            native_cache_misses=cache_misses,
                            frame_index=index,
                            cache_hit_delta=max(0, cache_hits - previous_hits) if is_last else 0,
                            cache_miss_delta=max(0, cache_misses - previous_misses) if is_last else 0,
                            cache_shape_miss_delta=max(0, shape_misses - previous_shape_misses) if is_last else 0,
                            cache_content_variant_miss_delta=max(
                                0,
                                content_variant_misses - previous_content_variant_misses,
                            )
                            if is_last
                            else 0,
                            cache_evicted_key_miss_delta=max(0, evicted_key_misses - previous_evicted_key_misses)
                            if is_last
                            else 0,
                            native_cache_shape_misses=shape_misses,
                            native_cache_content_variant_misses=content_variant_misses,
                            native_cache_evicted_key_misses=evicted_key_misses,
                            cache_scope_miss_delta=_format_counts(scope_miss_delta) if is_last else "",
                            native_cache_misses_by_scope=_format_counts(scope_misses),
                            cache_mode=cache_mode,
                            native_mode=f"range:{int(response.get('threads', args.range_threads))}",
                        )
                    )
            else:
                for index, t_ms in enumerate(timestamps):
                    native_start = time.perf_counter()
                    if args.native_mode == "png":
                        response = renderer.render_frame_png(t_ms, output_dir / f"{cache_mode}-frame-{index:04d}.png")
                    else:
                        response = renderer.render_frame_stats(t_ms)
                    native_ms = (time.perf_counter() - native_start) * 1000.0

                    cache_hits = int(response.get("glow_cache_hits", 0))
                    cache_misses = int(response.get("glow_cache_misses", 0))
                    shape_misses = int(response.get("glow_cache_shape_misses", 0))
                    content_variant_misses = int(response.get("glow_cache_content_variant_misses", 0))
                    evicted_key_misses = int(response.get("glow_cache_evicted_key_misses", 0))
                    scope_misses = _int_map(response.get("glow_cache_misses_by_scope"))
                    scope_miss_delta = _map_delta(scope_misses, previous_scope_misses)
                    samples.append(
                        TimingSample(
                            t_ms=t_ms,
                            python_ms=python_values[index],
                            native_ms=native_ms,
                            native_render_ms=float(response.get("render_ms", 0.0)),
                            native_cache_hits=cache_hits,
                            native_cache_misses=cache_misses,
                            frame_index=index,
                            cache_hit_delta=max(0, cache_hits - previous_hits),
                            cache_miss_delta=max(0, cache_misses - previous_misses),
                            cache_shape_miss_delta=max(0, shape_misses - previous_shape_misses),
                            cache_content_variant_miss_delta=max(
                                0,
                                content_variant_misses - previous_content_variant_misses,
                            ),
                            cache_evicted_key_miss_delta=max(0, evicted_key_misses - previous_evicted_key_misses),
                            native_cache_shape_misses=shape_misses,
                            native_cache_content_variant_misses=content_variant_misses,
                            native_cache_evicted_key_misses=evicted_key_misses,
                            cache_scope_miss_delta=_format_counts(scope_miss_delta),
                            native_cache_misses_by_scope=_format_counts(scope_misses),
                            cache_mode=cache_mode,
                            native_mode=args.native_mode,
                        )
                    )
                    previous_hits = cache_hits
                    previous_misses = cache_misses
                    previous_shape_misses = shape_misses
                    previous_content_variant_misses = content_variant_misses
                    previous_evicted_key_misses = evicted_key_misses
                    previous_scope_misses = scope_misses

    summary = _summarize_samples(
        samples,
        scenario=project_path.stem,
        width=width,
        height=height,
        cache_mode=cache_mode,
        native_mode=samples[-1].native_mode if samples else args.native_mode,
    )
    if not args.keep_png and not args.png_dir:
        if output_dir.exists():
            for path in output_dir.glob("*.png"):
                path.unlink(missing_ok=True)
            try:
                output_dir.rmdir()
            except OSError:
                pass
    return summary, samples


_CSV_FIELDS = [
    "scenario",
    "cache_mode",
    "native_mode",
    "frames",
    "width",
    "height",
    "python_mean_ms",
    "python_p50_ms",
    "python_p95_ms",
    "native_mean_ms",
    "native_p50_ms",
    "native_p95_ms",
    "native_render_mean_ms",
    "native_render_p95_ms",
    "speedup",
    "render_speedup",
    "native_cache_hits",
    "native_cache_misses",
    "native_cache_hit_delta",
    "native_cache_miss_delta",
    "native_cache_shape_misses",
    "native_cache_content_variant_misses",
    "native_cache_evicted_key_misses",
    "native_cache_shape_miss_delta",
    "native_cache_content_variant_miss_delta",
    "native_cache_evicted_key_miss_delta",
    "native_cache_misses_by_scope",
]

_SAMPLE_CSV_FIELDS = [
    "scenario",
    "cache_mode",
    "native_mode",
    "frame_index",
    "t_ms",
    "python_ms",
    "native_ms",
    "native_render_ms",
    "cache_hit_delta",
    "cache_miss_delta",
    "cache_shape_miss_delta",
    "cache_content_variant_miss_delta",
    "cache_evicted_key_miss_delta",
    "cache_scope_miss_delta",
    "native_cache_hits",
    "native_cache_misses",
    "native_cache_shape_misses",
    "native_cache_content_variant_misses",
    "native_cache_evicted_key_misses",
    "native_cache_misses_by_scope",
]


def _write_csv(path: Path, rows: list[dict[str, str | int]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=_CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def _sample_rows(samples: list[TimingSample], *, scenario: str) -> list[dict[str, str | int]]:
    return [
        {
            "scenario": scenario,
            "cache_mode": sample.cache_mode,
            "native_mode": sample.native_mode,
            "frame_index": sample.frame_index,
            "t_ms": sample.t_ms,
            "python_ms": f"{sample.python_ms:.4f}",
            "native_ms": f"{sample.native_ms:.4f}",
            "native_render_ms": f"{sample.native_render_ms:.4f}",
            "cache_hit_delta": sample.cache_hit_delta,
            "cache_miss_delta": sample.cache_miss_delta,
            "cache_shape_miss_delta": sample.cache_shape_miss_delta,
            "cache_content_variant_miss_delta": sample.cache_content_variant_miss_delta,
            "cache_evicted_key_miss_delta": sample.cache_evicted_key_miss_delta,
            "cache_scope_miss_delta": sample.cache_scope_miss_delta,
            "native_cache_hits": sample.native_cache_hits,
            "native_cache_misses": sample.native_cache_misses,
            "native_cache_shape_misses": sample.native_cache_shape_misses,
            "native_cache_content_variant_misses": sample.native_cache_content_variant_misses,
            "native_cache_evicted_key_misses": sample.native_cache_evicted_key_misses,
            "native_cache_misses_by_scope": sample.native_cache_misses_by_scope,
        }
        for sample in samples
    ]


def _write_sample_csv(path: Path, rows: list[dict[str, str | int]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=_SAMPLE_CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def _print_summary(row: dict[str, str | int]) -> None:
    print("scenario          :", row["scenario"])
    print("cache / native    :", row["cache_mode"], "/", row["native_mode"])
    print("frames / size     :", row["frames"], f"{row['width']}x{row['height']}")
    print("python mean / p95 :", f"{row['python_mean_ms']} ms", f"/ {row['python_p95_ms']} ms")
    print("native roundtrip  :", f"{row['native_mean_ms']} ms", f"/ {row['native_p95_ms']} ms")
    print("native render     :", f"{row['native_render_mean_ms']} ms", f"/ {row['native_render_p95_ms']} ms")
    print("speedup           :", f"{row['speedup']}x", f"(render-only {row['render_speedup']}x)")
    print(
        "native glow cache :",
        f"hits={row['native_cache_hits']}",
        f"misses={row['native_cache_misses']}",
        f"(sample delta +{row['native_cache_hit_delta']}/+{row['native_cache_miss_delta']})",
    )
    print(
        "miss breakdown    :",
        f"shape={row['native_cache_shape_miss_delta']}",
        f"content={row['native_cache_content_variant_miss_delta']}",
        f"evicted={row['native_cache_evicted_key_miss_delta']}",
    )
    print("miss scopes       :", row["native_cache_misses_by_scope"] or "-")


def _cache_modes(value: str) -> list[str]:
    return ["on", "off"] if value == "both" else [value]


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
    parser.add_argument(
        "--native-mode",
        choices=("stats", "png", "range"),
        default="stats",
        help="native 计时模式：stats 单帧不落盘，range 使用 sidecar 线程池，png 保留旧 smoke 输出",
    )
    parser.add_argument("--range-threads", type=int, default=4, help="--native-mode range 使用的 native worker 数")
    parser.add_argument(
        "--cache",
        choices=("on", "off", "both"),
        default="on",
        help="native glow cache 模式；both 会连续跑 on/off 对照",
    )
    parser.add_argument("--keep-project-style", action="store_true", help="不强制覆盖为 utopia + glow")
    parser.add_argument("--png-dir", type=Path, default=None, help="保留 native PNG 输出到指定目录")
    parser.add_argument("--keep-png", action="store_true", help="保留临时 native PNG 输出")
    parser.add_argument("--out", type=Path, default=None, help="summary CSV 输出路径，默认 .bench/native_renderer_<时间>.csv")
    parser.add_argument("--samples-out", type=Path, default=None, help="逐帧 CSV 输出路径，默认与 summary 同名加 _samples")
    args = parser.parse_args(argv)

    out = args.out
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    if out is None:
        bench_dir = _REPO_ROOT / ".bench"
        bench_dir.mkdir(exist_ok=True)
        out = bench_dir / f"native_renderer_{timestamp}.csv"
    samples_out = args.samples_out
    if samples_out is None:
        samples_out = out.with_name(f"{out.stem}_samples{out.suffix}")

    summaries: list[dict[str, str | int]] = []
    sample_rows: list[dict[str, str | int]] = []
    for index, cache_mode in enumerate(_cache_modes(args.cache)):
        if index:
            print("")
        summary, samples = _bench_project(args, cache_mode=cache_mode)
        _print_summary(summary)
        summaries.append(summary)
        sample_rows.extend(_sample_rows(samples, scenario=str(summary["scenario"])))

    _write_csv(out, summaries)
    _write_sample_csv(samples_out, sample_rows)
    print(f"CSV -> {out}")
    print(f"Samples CSV -> {samples_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
