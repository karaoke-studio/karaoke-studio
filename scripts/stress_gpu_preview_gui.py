"""Run a visible, real-video GPU preview stability gate."""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import sys
import time


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _percentile(values: list[float], ratio: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(max(math.ceil(len(ordered) * ratio) - 1, 0), len(ordered) - 1)
    return ordered[index]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", type=Path)
    parser.add_argument("--duration-minutes", type=float, default=30.0)
    parser.add_argument("--duration-seconds", type=float)
    parser.add_argument("--start-ms", type=int, default=92_000)
    parser.add_argument("--loop-ms", type=int, default=120_000)
    parser.add_argument("--window-width", type=int, default=1280)
    parser.add_argument("--window-height", type=int, default=720)
    parser.add_argument("--progress-seconds", type=float, default=30.0)
    parser.add_argument("--force-warp", action="store_true")
    parser.add_argument("--offscreen", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    if args.offscreen:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
    os.environ["KROK_SUBTITLE_ASYNC_PREVIEW"] = "1"
    os.environ["KROK_SUBTITLE_GPU_PREVIEW"] = "1"
    os.environ["KROK_SUBTITLE_GPU_FORCE_WARP"] = "1" if args.force_warp else "0"

    from PyQt6.QtCore import Qt, QTimer
    from PyQt6.QtWidgets import QApplication

    import psutil

    from krok_helper.subtitle_render.frontend.preview_async import GpuAsyncSubtitleRenderer
    from krok_helper.subtitle_render.frontend.preview_graphics import PreviewGraphicsView
    from krok_helper.subtitle_render.models import style_from_dict
    from krok_helper.subtitle_render.n3proj_import import load_n3proj
    from krok_helper.subtitle_render.subtitle_sources import load_nicokara_lrc

    project = args.project.resolve()
    imported = load_n3proj(project)
    data = imported.project_data
    subtitle_path = Path(str(data["subtitle_path"]))
    video_path = Path(str(data["video_path"]))
    track = load_nicokara_lrc(subtitle_path)
    style = style_from_dict(data["style"])
    screen = data.get("screen") if isinstance(data.get("screen"), dict) else {}
    output_width = max(int(screen.get("width", 1920)), 1)
    output_height = max(int(screen.get("height", 1080)), 1)
    fps = max(int(screen.get("fps", 60)), 1)
    duration_s = (
        max(float(args.duration_seconds), 0.1)
        if args.duration_seconds is not None
        else max(float(args.duration_minutes) * 60.0, 0.1)
    )
    loop_ms = max(int(args.loop_ms), 1_000)

    app = QApplication.instance() or QApplication(sys.argv)
    view = PreviewGraphicsView()
    view.setWindowTitle("Karaoke Studio GPU 预览长稳验收")
    view.resize(max(args.window_width, 320), max(args.window_height, 180))
    view.set_output_size(output_width, output_height)
    view.set_style(style)
    view.set_track(track)
    view.set_video_source(video_path)
    view.show()

    renderer = view._async_renderer  # noqa: SLF001
    if not isinstance(renderer, GpuAsyncSubtitleRenderer):
        raise RuntimeError(f"GPU preview renderer was not selected: {type(renderer).__name__}")

    process = psutil.Process()
    rss_start = process.memory_info().rss
    ready_count = 0
    latest_ready_ms = -1
    tick_gaps_ms: list[float] = []
    started_at = 0.0
    last_tick_at = 0.0
    last_frame_index = -1
    last_loop_index = -1
    exit_code = 1
    final_summary: dict[str, object] = {}

    def on_ready(_image, t_ms: int) -> None:
        nonlocal ready_count, latest_ready_ms
        ready_count += 1
        latest_ready_ms = int(t_ms)

    renderer.frame_ready.connect(on_ready)

    drive_timer = QTimer(view)
    drive_timer.setTimerType(Qt.TimerType.PreciseTimer)
    drive_timer.setInterval(4)
    progress_timer = QTimer(view)
    progress_timer.setTimerType(Qt.TimerType.CoarseTimer)
    progress_timer.setInterval(max(int(args.progress_seconds * 1000), 1_000))

    def drive() -> None:
        nonlocal last_tick_at, last_frame_index, last_loop_index
        now = time.monotonic()
        if last_tick_at > 0.0:
            tick_gaps_ms.append((now - last_tick_at) * 1000.0)
        last_tick_at = now
        elapsed = max(now - started_at, 0.0)
        frame_index = int(elapsed * fps)
        if frame_index <= last_frame_index:
            return
        last_frame_index = frame_index
        elapsed_ms = int(round(frame_index * 1000.0 / fps))
        loop_index = elapsed_ms // loop_ms
        t_ms = int(args.start_ms) + elapsed_ms % loop_ms
        if loop_index != last_loop_index and last_loop_index >= 0:
            view.set_playing(False)
            view.set_time(t_ms)
            view.set_playing(True)
        else:
            view.set_time(t_ms)
        last_loop_index = loop_index

    def progress() -> None:
        elapsed = time.monotonic() - started_at if started_at else 0.0
        stats = renderer.stats_snapshot()
        timings = renderer.timing_snapshot()
        rss_mib = process.memory_info().rss / (1024 * 1024)
        print(
            json.dumps(
                {
                    "elapsed_s": round(elapsed, 1),
                    "ready": ready_count,
                    "latest_t_ms": latest_ready_ms,
                    "pending_replaced": stats["pending_replaced"],
                    "stale": stats["stale_frames_dropped"],
                    "failures": stats["renderer_failures"],
                    "fallback": stats["fallback_frames"],
                    "max_pending": stats["max_pending"],
                    "roundtrip_p95_ms": round(float(timings["roundtrip_ms"]["p95"]), 3),
                    "rss_mib": round(rss_mib, 1),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

    def finish() -> None:
        nonlocal exit_code, final_summary
        drive_timer.stop()
        progress_timer.stop()
        view.set_playing(False)
        app.processEvents()
        player = view._video_player  # noqa: SLF001
        stats = renderer.stats_snapshot()
        timings = renderer.timing_snapshot()
        rss_end = process.memory_info().rss
        media_status = player.mediaStatus().name if player is not None else "NoPlayer"
        media_error = player.error().name if player is not None else "NoPlayer"
        final_summary = {
            "project": str(project),
            "video": str(video_path),
            "duration_s": round(time.monotonic() - started_at, 3),
            "output_size": [output_width, output_height],
            "window_size": [view.width(), view.height()],
            "fps": fps,
            "ready_frames": ready_count,
            "latest_ready_ms": latest_ready_ms,
            "stats": stats,
            "timings": timings,
            "drive_gap_ms": {
                "count": len(tick_gaps_ms),
                "p95": round(_percentile(tick_gaps_ms, 0.95), 3),
                "max": round(max(tick_gaps_ms, default=0.0), 3),
            },
            "media_status": media_status,
            "media_error": media_error,
            "rss_start_mib": round(rss_start / (1024 * 1024), 3),
            "rss_end_mib": round(rss_end / (1024 * 1024), 3),
            "rss_growth_mib": round((rss_end - rss_start) / (1024 * 1024), 3),
            "warnings": imported.warnings,
        }
        passed = bool(
            ready_count > 0
            and stats["renderer_failures"] == 0
            and stats["fallback_frames"] == 0
            and stats["max_pending"] <= 1
            and media_error in {"NoError", "NoPlayer"}
        )
        final_summary["passed"] = passed
        exit_code = 0 if passed else 1
        print(json.dumps(final_summary, ensure_ascii=False, indent=2), flush=True)
        if args.output is not None:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(
                json.dumps(final_summary, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        view.close()
        app.quit()

    def begin() -> None:
        nonlocal started_at, last_tick_at
        view.set_time(int(args.start_ms))
        view.set_playing(True)
        started_at = time.monotonic()
        last_tick_at = started_at
        drive_timer.start()
        progress_timer.start()
        QTimer.singleShot(max(int(duration_s * 1000), 1), finish)
        print(
            json.dumps(
                {
                    "event": "started",
                    "duration_s": duration_s,
                    "renderer": type(renderer).__name__,
                    "project": str(project),
                    "video": str(video_path),
                    "visible": not args.offscreen,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

    drive_timer.timeout.connect(drive)
    progress_timer.timeout.connect(progress)
    QTimer.singleShot(2_000, begin)
    app.exec()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
