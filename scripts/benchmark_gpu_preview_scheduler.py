"""Exercise the G2 GPU preview scheduler with real-time ticks and churn."""

from __future__ import annotations

import argparse
from collections import Counter
import csv
from dataclasses import replace
import json
import os
from pathlib import Path
import random
import sys
import time

from PyQt6.QtWidgets import QApplication

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from krok_helper.subtitle_render.frontend.preview_async import GpuAsyncSubtitleRenderer
from krok_helper.subtitle_render.models import Style, TimingChar, TimingLine, TimingTrack


def _track(duration_ms: int) -> TimingTrack:
    text = "GPU preview latest wins 走字测试"
    step = max(duration_ms // max(len(text), 1), 1)
    return TimingTrack(
        lines=[
            TimingLine(
                chars=[TimingChar(character, index * step) for index, character in enumerate(text)],
                end_ms=duration_ms,
            )
        ]
    )


def _wait_until(app: QApplication, predicate, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.002)
    app.processEvents()
    return bool(predicate())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--fps", type=int, default=60)
    parser.add_argument("--duration", type=float, default=10.0)
    parser.add_argument("--force-warp", action="store_true")
    parser.add_argument("--glow", action="store_true")
    parser.add_argument("--seek-burst", type=int, default=0)
    parser.add_argument("--resize-churn", type=int, default=0)
    parser.add_argument("--style-churn", type=int, default=0)
    parser.add_argument("--output", type=Path, default=Path("build/gpu-preview-benchmark.csv"))
    args = parser.parse_args()

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ["KROK_SUBTITLE_GPU_FORCE_WARP"] = "1" if args.force_warp else "0"
    app = QApplication.instance() or QApplication([])
    duration_ms = max(int(args.duration * 1000), 1000)
    track = _track(duration_ms + 10_000)
    style = Style(
        font_family="Meiryo",
        font_family_latin="Times New Roman",
        font_size_px=100,
        stroke_width_px=8,
        stroke2_enabled=True,
        stroke2_width_px=4,
        decoration_kind="glow" if args.glow else "shadow",
        glow_radius_px=10,
        glow_before_radius_px=10,
        glow_after_radius_px=10,
        glow_concentration_level=1 if args.glow else 0,
        line_y_position="center",
        line_horizontal_layout="center",
        line_lead_in_ms=0,
        line_tail_ms=0,
    )
    renderer = GpuAsyncSubtitleRenderer(args.width, args.height)
    rows: list[dict[str, float | int | str]] = []
    latest_requested = 0
    current_phase = "playback"
    start = time.monotonic()

    def on_frame(_image, rendered_t_ms: int) -> None:
        now_ms = (time.monotonic() - start) * 1000.0
        rows.append(
            {
                "phase": current_phase,
                "wall_ms": round(now_ms, 3),
                "rendered_t_ms": int(rendered_t_ms),
                "latest_requested_t_ms": int(latest_requested),
                "timeline_lag_ms": int(latest_requested) - int(rendered_t_ms),
            }
        )

    renderer.frame_ready.connect(on_frame)
    try:
        renderer.set_state(track, style)
        # Real preview configures and shows a paused frame before playback.
        # Warm the sidecar/font/surface caches so the playback metric measures
        # steady-state delivery rather than process startup.
        renderer.set_playing(False)
        renderer.request(0)
        _wait_until(app, lambda: bool(rows), 3.0)
        rows.clear()
        start = time.monotonic()
        renderer.set_playing(True)
        frame_count = max(int(round(args.duration * max(args.fps, 1))), 1)
        interval = 1.0 / max(args.fps, 1)
        for frame_index in range(frame_count):
            deadline = start + frame_index * interval
            while time.monotonic() < deadline:
                app.processEvents()
                time.sleep(min(max(deadline - time.monotonic(), 0.0), 0.002))
            latest_requested = int(round(frame_index * 1000.0 / max(args.fps, 1)))
            renderer.request(latest_requested)
            app.processEvents()
        _wait_until(
            app,
            lambda: any(
                row["phase"] == "playback"
                and int(row["rendered_t_ms"]) == latest_requested
                for row in rows
            ),
            2.0,
        )

        if args.seek_burst > 0:
            current_phase = "seek"
            renderer.set_playing(False)
            rng = random.Random(0)
            for _ in range(args.seek_burst):
                latest_requested = rng.randrange(0, duration_ms)
                renderer.request(latest_requested)
            _wait_until(
                app,
                lambda: any(int(row["rendered_t_ms"]) == latest_requested for row in rows),
                3.0,
            )

        for index in range(max(args.resize_churn, 0)):
            current_phase = "resize"
            width, height = ((1280, 720) if index % 2 == 0 else (args.width, args.height))
            renderer.set_render_target(width, height, 1.0)
            latest_requested = (index * 97) % duration_ms
            before = len(rows)
            renderer.request(latest_requested)
            _wait_until(app, lambda: len(rows) > before, 3.0)

        for index in range(max(args.style_churn, 0)):
            current_phase = "style"
            changed = replace(style, stroke_width_px=6 + index % 4)
            renderer.set_state(track, changed)
            latest_requested = (index * 113) % duration_ms
            before = len(rows)
            renderer.request(latest_requested)
            _wait_until(app, lambda: len(rows) > before, 3.0)
    finally:
        renderer.stop()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "phase",
                "wall_ms",
                "rendered_t_ms",
                "latest_requested_t_ms",
                "timeline_lag_ms",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)
    delivered_by_phase = Counter(str(row["phase"]) for row in rows)
    summary = {
        "requested_playback_frames": frame_count,
        "delivered_frames": len(rows),
        "delivered_by_phase": dict(delivered_by_phase),
        "playback_delivery_rate": delivered_by_phase.get("playback", 0) / frame_count,
        "stats": renderer.stats_snapshot(),
        "timings": renderer.timing_snapshot(),
        "csv": str(args.output),
    }
    summary_path = args.output.with_suffix(".json")
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["stats"]["renderer_failures"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
