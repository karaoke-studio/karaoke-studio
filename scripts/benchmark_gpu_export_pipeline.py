"""Measure the Windows GPU subtitle export pipeline with one timing ledger."""

from __future__ import annotations

import argparse
import csv
from dataclasses import replace
import json
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_PROJECT = Path(r"C:\Users\18007\Downloads\芽吹の唄 - 大原ゆい子.yurika")


def _load_project(path: Path):
    from krok_helper.subtitle_render.models import BackgroundSource, style_from_dict
    from krok_helper.subtitle_render.project_store import load_render_project
    from krok_helper.subtitle_render.subtitle_sources import load_nicokara_lrc

    data = load_render_project(path)
    subtitle_path = Path(str(data["subtitle_path"]))
    track = load_nicokara_lrc(subtitle_path)
    style = style_from_dict(data.get("style"))
    screen = data.get("screen") if isinstance(data.get("screen"), dict) else {}
    background_data = (
        data.get("background") if isinstance(data.get("background"), dict) else {}
    )
    background = BackgroundSource(
        kind=str(background_data.get("kind") or "solid"),
        path=str(background_data.get("path") or ""),
        color=str(background_data.get("color") or "#000000"),
        source_fps=background_data.get("source_fps"),
        sequence_start_number=int(background_data.get("sequence_start_number") or 0),
        video_offset_ms=int(background_data.get("video_offset_ms") or 0),
    )
    return data, track, style, screen, background


def _git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * fraction))]


def _write_rows(path: Path, rows: list[dict[str, object]]) -> None:
    if not rows:
        return
    fields: list[str] = []
    for row in rows:
        for field in row:
            if field not in fields:
                fields.append(field)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _aggregates(rows: list[dict[str, object]]) -> dict[str, dict[str, float]]:
    result: dict[str, dict[str, float]] = {}
    fields = {
        key
        for row in rows
        for key, value in row.items()
        if isinstance(value, (int, float)) and key not in {"frame_index", "t_ms"}
    }
    for field in sorted(fields):
        values = [float(row[field]) for row in rows if field in row]
        result[field] = {
            "mean": statistics.fmean(values),
            "p50": statistics.median(values),
            "p95": _percentile(values, 0.95),
            "min": min(values),
            "max": max(values),
        }
    return result


def _run_transport(
    *,
    mode: str,
    track,
    style,
    width: int,
    height: int,
    fps: int,
    frames: int,
    workers: int,
    ffmpeg_path: str,
) -> tuple[list[dict[str, object]], dict[str, object], float]:
    from krok_helper.subtitle_render.engine.native_export import iter_gpu_rgba_frames

    rows: list[dict[str, object]] = []
    rows_by_index: dict[int, dict[str, object]] = {}
    diagnostics: dict[str, object] = {}
    process = None
    if mode == "pipe":
        process = subprocess.Popen(
            [
                ffmpeg_path,
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "rawvideo",
                "-pix_fmt",
                "rgba",
                "-s",
                f"{width}x{height}",
                "-r",
                str(fps),
                "-i",
                "pipe:0",
                "-f",
                "null",
                "-",
            ],
            stdin=subprocess.PIPE,
        )
    started = time.perf_counter()

    def record_frame(values: dict[str, object]) -> None:
        row = dict(values)
        rows.append(row)
        rows_by_index[int(row["frame_index"])] = row

    try:
        for frame_index, frame in enumerate(
            iter_gpu_rgba_frames(
                track,
                style,
                width=width,
                height=height,
                fps=fps,
                total_frames=frames,
                worker_count=workers,
                on_diagnostics=diagnostics.update,
                on_frame_diagnostics=record_frame,
            )
        ):
            if process is not None:
                assert process.stdin is not None
                write_started = time.perf_counter()
                process.stdin.write(frame)
                rows_by_index[frame_index]["stdin_block_ms"] = (
                    time.perf_counter() - write_started
                ) * 1000.0
    finally:
        if process is not None:
            assert process.stdin is not None
            process.stdin.close()
            return_code = process.wait()
            if return_code != 0:
                raise RuntimeError(f"ffmpeg null sink failed with exit code {return_code}")
    return rows, diagnostics, (time.perf_counter() - started) * 1000.0


def _run_full(
    *,
    track,
    style,
    background,
    output_path: Path,
    width: int,
    height: int,
    fps: int,
    duration_ms: int,
    encoder: str,
) -> float:
    from krok_helper.subtitle_render.engine.renderer import RenderJob, render_subtitle_video

    job = RenderJob(
        track=track,
        style=style,
        background_video_path=None,
        background_source=background,
        output_path=output_path,
        width=width,
        height=height,
        fps=fps,
        duration_ms=duration_ms,
        include_audio=False,
        encoder_mode=encoder,
        preset="veryfast",
        gpu_export_enabled=True,
    )
    started = time.perf_counter()
    render_subtitle_video(job)
    return (time.perf_counter() - started) * 1000.0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--project",
        type=Path,
        default=DEFAULT_PROJECT if DEFAULT_PROJECT.is_file() else None,
        required=not DEFAULT_PROJECT.is_file(),
    )
    parser.add_argument("--mode", choices=("renderer", "pipe", "full"), default="pipe")
    parser.add_argument("--width", type=int, default=0)
    parser.add_argument("--height", type=int, default=0)
    parser.add_argument("--fps", type=int, default=0)
    parser.add_argument("--seconds", type=float, default=3.0)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--encoder", choices=("cpu", "nvenc", "qsv", "amf"), default="cpu")
    parser.add_argument(
        "--animation",
        choices=("project", "none", "fade", "char_fade", "spin_flip", "utopia"),
        default="project",
    )
    parser.add_argument(
        "--decoration", choices=("project", "none", "shadow", "glow"), default="project"
    )
    parser.add_argument("--solid-background", action="store_true")
    parser.add_argument("--disable-title", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "build" / "gpu-export-stage0")
    args = parser.parse_args(argv)

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication
    from krok_helper.ffmpeg import find_tool
    from krok_helper.subtitle_render.models import BackgroundSource

    _ = QApplication.instance() or QApplication([])
    data, track, style, screen, background = _load_project(args.project)
    if args.animation != "project":
        style = replace(style, entry_anim=args.animation, exit_anim=args.animation)
    if args.decoration != "project":
        style = replace(
            style,
            decoration_kind=args.decoration,
            ruby_decoration_kind=args.decoration,
        )
    if args.disable_title:
        style = replace(style, title_overlay=None)
    width = args.width if args.width > 0 else int(screen.get("width", 1920))
    height = args.height if args.height > 0 else int(screen.get("height", 1080))
    fps = args.fps if args.fps > 0 else int(screen.get("fps", 60))
    duration_ms = max(1, int(round(args.seconds * 1000.0)))
    frames = max(1, int((duration_ms * fps + 999) // 1000))
    if args.solid_background:
        background = BackgroundSource(kind="solid", color="#000000")
    ffmpeg_path = find_tool("ffmpeg.exe", None)
    args.output_dir.mkdir(parents=True, exist_ok=True)

    for run_index in range(max(args.runs, 1)):
        run_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
        summary: dict[str, object] = {
            "export_run_id": run_id,
            "mode": args.mode,
            "project": str(args.project),
            "project_size_bytes": args.project.stat().st_size,
            "git_commit": _git_commit(),
            "platform": platform.platform(),
            "cpu": platform.processor(),
            "width": width,
            "height": height,
            "fps": fps,
            "duration_ms": duration_ms,
            "frames": frames,
            "workers": args.workers,
            "encoder": args.encoder,
            "animation": args.animation,
            "decoration": args.decoration,
            "title_enabled": not args.disable_title,
            "background_kind": background.kind,
            "run_index": run_index,
        }
        if args.mode == "full":
            os.environ["KROK_SUBTITLE_GPU_EXPORT_DIAGNOSTICS"] = "1"
            os.environ["KROK_SUBTITLE_GPU_EXPORT_DIAGNOSTICS_DIR"] = str(
                args.output_dir
            )
            summary["total_wall_ms"] = _run_full(
                track=track,
                style=style,
                background=background,
                output_path=args.output_dir / f"{run_id}.mp4",
                width=width,
                height=height,
                fps=fps,
                duration_ms=duration_ms,
                encoder=args.encoder,
            )
        else:
            rows, diagnostics, wall_ms = _run_transport(
                mode=args.mode,
                track=track,
                style=style,
                width=width,
                height=height,
                fps=fps,
                frames=frames,
                workers=args.workers,
                ffmpeg_path=ffmpeg_path,
            )
            rows_path = args.output_dir / f"{run_id}-frames.csv"
            _write_rows(rows_path, rows)
            summary.update(
                total_wall_ms=wall_ms,
                export_fps=frames / max(wall_ms / 1000.0, 1e-9),
                gpu=diagnostics,
                aggregates=_aggregates(rows),
                frame_csv=str(rows_path),
            )
        summary_path = args.output_dir / f"{run_id}-summary.json"
        summary_path.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        print(json.dumps(summary, ensure_ascii=False, default=str))
        print(f"summary={summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
