from __future__ import annotations

import argparse
import csv
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@dataclass(frozen=True)
class ExportRunResult:
    backend: str
    output_path: Path
    elapsed_ms: float
    total_frames: int
    progress_events: int
    file_size: int


def _frame_count(duration_ms: int, fps: int) -> int:
    return max(1, int((max(int(duration_ms), 0) * max(int(fps), 1) + 999) // 1000))


def _write_csv(path: Path, rows: list[dict[str, str | int | float]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _run_checked(command: list[str]) -> None:
    subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def _generate_background(
    *,
    ffmpeg_path: str,
    output_path: Path,
    width: int,
    height: int,
    fps: int,
    duration_ms: int,
) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    duration_s = max(int(duration_ms), 1) / 1000.0
    _run_checked(
        [
            ffmpeg_path,
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c=black:s={int(width)}x{int(height)}:d={duration_s:.6f}:r={int(fps)}",
            "-pix_fmt",
            "yuv420p",
            str(output_path),
        ]
    )
    return output_path


def _with_env(overrides: dict[str, str | None], action: Callable[[], ExportRunResult]) -> ExportRunResult:
    previous = {key: os.environ.get(key) for key in overrides}
    try:
        for key, value in overrides.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        return action()
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _run_export(
    backend: str,
    *,
    track,
    style,
    background_video: Path,
    output_path: Path,
    width: int,
    height: int,
    fps: int,
    duration_ms: int,
    include_audio: bool,
    crf: int,
    preset: str,
    encoder_mode: str,
    native_renderer: Path | None,
    strip_enabled: bool | None,
) -> ExportRunResult:
    from krok_helper.subtitle_render.engine.renderer import RenderJob, render_subtitle_video

    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        output_path.unlink()
    progress: list[tuple[int, int]] = []
    job = RenderJob(
        track=track,
        style=style,
        background_video_path=background_video,
        output_path=output_path,
        width=width,
        height=height,
        fps=fps,
        duration_ms=duration_ms,
        include_audio=include_audio,
        crf=crf,
        preset=preset,
        encoder_mode=encoder_mode,
    )

    env: dict[str, str | None] = {}
    if backend == "native":
        env["KROK_SUBTITLE_NATIVE_EXPORT"] = "1"
        if native_renderer is not None:
            env["KROK_SUBTITLE_NATIVE_RENDERER"] = str(native_renderer)
    else:
        env["KROK_SUBTITLE_NATIVE_EXPORT"] = "0"
    if strip_enabled is not None:
        env["KROK_SUBTITLE_RENDER_STRIP"] = "1" if strip_enabled else "0"

    def action() -> ExportRunResult:
        started = time.perf_counter()
        render_subtitle_video(
            job,
            on_progress=lambda done, total: progress.append((int(done), int(total))),
        )
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        total_frames = progress[-1][1] if progress else _frame_count(duration_ms, fps)
        return ExportRunResult(
            backend=backend,
            output_path=output_path,
            elapsed_ms=elapsed_ms,
            total_frames=total_frames,
            progress_events=len(progress),
            file_size=output_path.stat().st_size if output_path.is_file() else 0,
        )

    return _with_env(env, action)


def _extract_frame(
    *,
    ffmpeg_path: str,
    video_path: Path,
    t_ms: int,
    output_path: Path,
) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    _run_checked(
        [
            ffmpeg_path,
            "-y",
            "-ss",
            f"{max(int(t_ms), 0) / 1000.0:.6f}",
            "-i",
            str(video_path),
            "-frames:v",
            "1",
            str(output_path),
        ]
    )
    return output_path


def _sample_times(duration_ms: int, sample_count: int) -> list[int]:
    count = max(int(sample_count), 0)
    if count <= 0:
        return []
    duration = max(int(duration_ms), 1)
    return [min(duration - 1, int(round(duration * (index + 0.5) / count))) for index in range(count)]


def _image_diff_summary(first: Path, second: Path, *, max_samples: int = 20_000) -> dict[str, int]:
    from PyQt6.QtGui import QImage

    a = QImage(str(first)).convertToFormat(QImage.Format.Format_RGBA8888)
    b = QImage(str(second)).convertToFormat(QImage.Format.Format_RGBA8888)
    width = min(a.width(), b.width())
    height = min(a.height(), b.height())
    if width <= 0 or height <= 0:
        return {"width": width, "height": height, "sampled_pixels": 0, "changed_pixels": 0, "max_channel_delta": 0}
    total = width * height
    stride = max(int(round((total / max(int(max_samples), 1)) ** 0.5)), 1)
    sampled = 0
    changed = 0
    max_delta = 0
    for y in range(0, height, stride):
        for x in range(0, width, stride):
            ca = a.pixelColor(x, y)
            cb = b.pixelColor(x, y)
            delta = max(
                abs(ca.red() - cb.red()),
                abs(ca.green() - cb.green()),
                abs(ca.blue() - cb.blue()),
                abs(ca.alpha() - cb.alpha()),
            )
            if delta:
                changed += 1
                max_delta = max(max_delta, delta)
            sampled += 1
    return {
        "width": width,
        "height": height,
        "sampled_pixels": sampled,
        "changed_pixels": changed,
        "max_channel_delta": max_delta,
    }


def _quality_rows(
    *,
    ffmpeg_path: str,
    python_output: Path,
    native_output: Path,
    samples_dir: Path,
    duration_ms: int,
    sample_count: int,
) -> list[dict[str, str | int]]:
    rows: list[dict[str, str | int]] = []
    for t_ms in _sample_times(duration_ms, sample_count):
        python_frame = _extract_frame(
            ffmpeg_path=ffmpeg_path,
            video_path=python_output,
            t_ms=t_ms,
            output_path=samples_dir / f"python-{t_ms:08d}.png",
        )
        native_frame = _extract_frame(
            ffmpeg_path=ffmpeg_path,
            video_path=native_output,
            t_ms=t_ms,
            output_path=samples_dir / f"native-{t_ms:08d}.png",
        )
        diff = _image_diff_summary(python_frame, native_frame)
        rows.append({"backend": "quality", "t_ms": t_ms, **diff})
    return rows


def _result_row(result: ExportRunResult) -> dict[str, str | int]:
    elapsed_s = max(result.elapsed_ms / 1000.0, 0.001)
    return {
        "backend": result.backend,
        "output": str(result.output_path),
        "elapsed_ms": f"{result.elapsed_ms:.2f}",
        "frames": result.total_frames,
        "export_fps": f"{result.total_frames / elapsed_s:.2f}",
        "progress_events": result.progress_events,
        "file_size": result.file_size,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Compare Python and native full-pipeline subtitle export backends.")
    parser.add_argument("--lrc", required=True, type=Path, help="Nicokara LRC file")
    parser.add_argument("--video", type=Path, default=None, help="Background video; generated when omitted")
    parser.add_argument("--native-renderer", type=Path, default=None, help="Optional native sidecar executable")
    parser.add_argument("--duration-ms", type=int, default=5000, help="Export duration")
    parser.add_argument("--fps", type=int, default=60, help="Export FPS")
    parser.add_argument("--width", type=int, default=1280, help="Export width")
    parser.add_argument("--height", type=int, default=720, help="Export height")
    parser.add_argument("--crf", type=int, default=18, help="Video CRF/CQ")
    parser.add_argument("--preset", default="veryfast", help="CPU encoder preset")
    parser.add_argument("--encoder-mode", default="cpu", choices=("cpu", "nvenc", "qsv", "amf"), help="Encoder mode")
    parser.add_argument("--include-audio", action="store_true", help="Copy optional background audio")
    parser.add_argument("--sample-frames", type=int, default=5, help="Extract this many output frames for diff")
    parser.add_argument("--disable-strip", action="store_true", help="Disable Python strip/band optimization for fair full-frame A/B")
    parser.add_argument("--out-dir", type=Path, default=None, help="Directory for output MP4 files")
    parser.add_argument("--out", type=Path, default=None, help="Summary CSV output path")
    parser.add_argument("--keep-samples", action="store_true", help="Keep extracted PNG frames")
    parser.add_argument("--offscreen", action="store_true", help="Set QT_QPA_PLATFORM=offscreen")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.offscreen:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from PyQt6.QtWidgets import QApplication

    from krok_helper.ffmpeg import find_tool
    from krok_helper.subtitle_render.models import Style
    from krok_helper.subtitle_render.subtitle_sources import load_nicokara_lrc

    _ = QApplication.instance() or QApplication([])
    ffmpeg_path = find_tool("ffmpeg.exe", None)
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    bench_dir = ROOT / ".bench"
    out_dir = args.out_dir or bench_dir / f"export_backends_{timestamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    summary_path = args.out or out_dir / "summary.csv"

    temporary_dir: Path | None = None
    try:
        if args.video is None:
            temporary_dir = Path(tempfile.mkdtemp(prefix="krok-export-backends-"))
            background_video = _generate_background(
                ffmpeg_path=ffmpeg_path,
                output_path=temporary_dir / "background.mp4",
                width=args.width,
                height=args.height,
                fps=args.fps,
                duration_ms=args.duration_ms,
            )
        else:
            background_video = args.video

        track = load_nicokara_lrc(args.lrc)
        style = Style(entry_anim="utopia", exit_anim="utopia", decoration_kind="glow")
        strip_enabled = False if args.disable_strip else None
        common = dict(
            track=track,
            style=style,
            background_video=background_video,
            width=args.width,
            height=args.height,
            fps=args.fps,
            duration_ms=args.duration_ms,
            include_audio=bool(args.include_audio),
            crf=args.crf,
            preset=args.preset,
            encoder_mode=args.encoder_mode,
            native_renderer=args.native_renderer,
            strip_enabled=strip_enabled,
        )
        python_result = _run_export("python", output_path=out_dir / "python.mp4", **common)
        native_result = _run_export("native", output_path=out_dir / "native.mp4", **common)
        rows: list[dict[str, str | int | float]] = [_result_row(python_result), _result_row(native_result)]
        quality = _quality_rows(
            ffmpeg_path=ffmpeg_path,
            python_output=python_result.output_path,
            native_output=native_result.output_path,
            samples_dir=out_dir / "samples",
            duration_ms=args.duration_ms,
            sample_count=args.sample_frames,
        )
        rows.extend(quality)
        _write_csv(summary_path, rows)

        for row in rows:
            if row["backend"] == "quality":
                print(
                    f"quality t={row['t_ms']}ms: changed={row['changed_pixels']}/{row['sampled_pixels']} "
                    f"max_delta={row['max_channel_delta']}"
                )
            else:
                print(
                    f"{row['backend']}: frames={row['frames']} fps={row['export_fps']} "
                    f"elapsed={row['elapsed_ms']}ms size={row['file_size']}"
                )
        print(f"CSV -> {summary_path}")
        print(f"Outputs -> {out_dir}")
        if not args.keep_samples:
            shutil.rmtree(out_dir / "samples", ignore_errors=True)
    finally:
        if temporary_dir is not None:
            shutil.rmtree(temporary_dir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
