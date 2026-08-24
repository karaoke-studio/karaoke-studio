"""Exercise the G2 GPU preview scheduler with real-time ticks and churn."""

from __future__ import annotations

import argparse
from collections import Counter
import csv
from dataclasses import replace
import json
import math
import os
from pathlib import Path
import random
import sys
import time

from PyQt6.QtWidgets import QApplication, QWidget

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from krok_helper.subtitle_render.frontend.preview import preview_async as preview_async_module
from krok_helper.subtitle_render.frontend.preview.preview_async import GpuAsyncSubtitleRenderer
from krok_helper.subtitle_render.models import Style, TimingChar, TimingLine, TimingTrack
from krok_helper.subtitle_render.native_backend import NativeRendererProcess


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
    parser.add_argument(
        "--native-preview",
        action="store_true",
        help="Use the G6 DirectComposition child HWND instead of readback/QImage.",
    )
    parser.add_argument("--glow", action="store_true")
    parser.add_argument("--stroke-width", type=int, default=14)
    parser.add_argument("--stroke2-width", type=int, default=7)
    parser.add_argument(
        "--worker-count",
        type=int,
        default=2,
        choices=(1, 2, 3, 4, 8),
        help="Bounded sidecar GPU preview workers (WARP is always clamped to 1).",
    )
    parser.add_argument("--seek-burst", type=int, default=0)
    parser.add_argument("--resize-churn", type=int, default=0)
    parser.add_argument("--style-churn", type=int, default=0)
    parser.add_argument(
        "--kill-sidecar",
        action="store_true",
        help="Kill the live sidecar once and verify Painter fallback plus GPU restart.",
    )
    parser.add_argument(
        "--inject-slow-frame-ms",
        type=int,
        default=0,
        help="Inject one end-to-end worker stall and measure latest-frame recovery.",
    )
    parser.add_argument("--output", type=Path, default=Path("build/gpu-preview-benchmark.csv"))
    args = parser.parse_args()

    if args.native_preview:
        if os.name != "nt":
            parser.error("--native-preview is Windows-only")
        os.environ.setdefault("QT_QPA_PLATFORM", "windows")
        os.environ["KROK_SUBTITLE_GPU_NATIVE_PREVIEW"] = "1"
    else:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        os.environ["KROK_SUBTITLE_GPU_NATIVE_PREVIEW"] = "0"
    os.environ["KROK_SUBTITLE_GPU_FORCE_WARP"] = "1" if args.force_warp else "0"
    os.environ["KROK_SUBTITLE_GPU_WORKERS"] = str(args.worker_count)
    app = QApplication.instance() or QApplication([])
    slow_state: dict[str, float | bool] = {
        "armed": False,
        "consumed": False,
        "released_at": 0.0,
    }
    slow_frame_ms = max(int(args.inject_slow_frame_ms), 0)
    if slow_frame_ms:
        class DelayedNativeRendererProcess(NativeRendererProcess):
            def begin_render_gpu_frame(self, *render_args, **render_kwargs):
                if slow_state["armed"] and not slow_state["consumed"]:
                    slow_state["consumed"] = True
                    time.sleep(slow_frame_ms / 1000.0)
                    slow_state["released_at"] = time.monotonic()
                return super().begin_render_gpu_frame(*render_args, **render_kwargs)

            def render_gpu_frame(self, *render_args, **render_kwargs):
                if slow_state["armed"] and not slow_state["consumed"]:
                    slow_state["consumed"] = True
                    time.sleep(slow_frame_ms / 1000.0)
                    slow_state["released_at"] = time.monotonic()
                return super().render_gpu_frame(*render_args, **render_kwargs)

            def present_gpu_frame(self, *render_args, **render_kwargs):
                if slow_state["armed"] and not slow_state["consumed"]:
                    slow_state["consumed"] = True
                    time.sleep(slow_frame_ms / 1000.0)
                    slow_state["released_at"] = time.monotonic()
                return super().present_gpu_frame(*render_args, **render_kwargs)

        preview_async_module.NativeRendererProcess = DelayedNativeRendererProcess
    duration_ms = max(int(args.duration * 1000), 1000)
    track = _track(duration_ms + 10_000)
    style = Style(
        font_family="Meiryo",
        font_family_latin="Times New Roman",
        font_size_px=100,
        stroke_width_px=max(int(args.stroke_width), 0),
        stroke2_enabled=True,
        stroke2_width_px=max(int(args.stroke2_width), 0),
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
    native_host: QWidget | None = None
    if args.native_preview:
        native_host = QWidget()
        native_host.setWindowTitle("Karaoke Studio G6 Preview Benchmark")
        native_host.setStyleSheet("background: #101010")
        native_host.resize(args.width, args.height)
        native_host.show()
        app.processEvents()
        renderer.set_native_target(
            int(native_host.winId()), 0, 0, args.width, args.height
        )
    rows: list[dict[str, float | int | str]] = []
    delivered_at: dict[int, float] = {}
    latest_requested = 0
    current_phase = "playback"
    start = time.monotonic()

    def note_frame(rendered_t_ms: int) -> None:
        delivered_at[int(rendered_t_ms)] = time.monotonic()
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

    renderer.frame_ready.connect(lambda _image, rendered_t_ms: note_frame(rendered_t_ms))
    renderer.frame_presented.connect(note_frame)
    native_diagnostics: dict[str, object] = {}
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
            if native_host is not None:
                native_host.resize(width, height)
                app.processEvents()
                renderer.set_native_target(
                    int(native_host.winId()), 0, 0, width, height
                )
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

        kill_recovery: dict[str, float | int | bool] = {
            "requested": bool(args.kill_sidecar),
            "pid": 0,
            "fallback_delivered": False,
            "gpu_restarted": False,
            "recovery_ms": 0.0,
            "passed": not args.kill_sidecar,
        }
        if args.kill_sidecar:
            import psutil

            current_phase = "kill_recovery"
            renderer.set_playing(False)
            native = renderer._renderer  # noqa: SLF001 - deliberate fault injection
            pid = native.process_id if native is not None else None
            if pid is None:
                raise RuntimeError("GPU sidecar was not running before kill injection")
            sidecar_process = psutil.Process(pid)
            sidecar_process.kill()
            sidecar_process.wait(timeout=3.0)
            failure_t = duration_ms + 30_000
            latest_requested = failure_t
            renderer.request(failure_t)
            fallback_delivered = _wait_until(
                app,
                lambda: (
                    failure_t in delivered_at
                    and renderer.stats_snapshot()["renderer_failures"] >= 1
                    and renderer.stats_snapshot()["fallback_frames"] >= 1
                ),
                3.0,
            )
            # The product worker uses a one-second bounded retry cooldown.
            cooldown_deadline = time.monotonic() + 1.05
            while time.monotonic() < cooldown_deadline:
                app.processEvents()
                time.sleep(0.005)
            recovery_t = failure_t + 1_000
            latest_requested = recovery_t
            recovery_started = time.monotonic()
            renderer.request(recovery_t)
            gpu_restarted = _wait_until(
                app,
                lambda: (
                    recovery_t in delivered_at
                    and renderer.stats_snapshot()["renderer_restarts"] >= 1
                    and renderer.stats_snapshot()["configure_count"] >= 2
                ),
                3.0,
            )
            recovery_ms = (time.monotonic() - recovery_started) * 1000.0
            kill_recovery = {
                "requested": True,
                "pid": int(pid),
                "fallback_delivered": fallback_delivered,
                "gpu_restarted": gpu_restarted,
                "recovery_ms": round(recovery_ms, 3),
                "passed": bool(fallback_delivered and gpu_restarted),
            }

        slow_recovery: dict[str, float | int | bool] = {
            "requested_delay_ms": slow_frame_ms,
            "delay_consumed": False,
            "latest_delivered": False,
            "after_release_ms": 0.0,
            "passed": slow_frame_ms == 0,
        }
        if slow_frame_ms:
            current_phase = "slow_recovery"
            renderer.set_playing(False)
            settle_t = duration_ms + 20_000
            latest_requested = settle_t
            renderer.request(settle_t)
            _wait_until(app, lambda: settle_t in delivered_at, 3.0)

            slow_state["armed"] = True
            burst_frames = max(
                int(math.ceil((slow_frame_ms + 120) / (1000.0 / max(args.fps, 1)))),
                4,
            )
            burst_start = time.monotonic()
            for index in range(burst_frames):
                deadline = burst_start + index / max(args.fps, 1)
                while time.monotonic() < deadline:
                    app.processEvents()
                    time.sleep(min(max(deadline - time.monotonic(), 0.0), 0.002))
                latest_requested = settle_t + 1_000 + int(
                    round(index * 1000.0 / max(args.fps, 1))
                )
                renderer.request(latest_requested)
                app.processEvents()
            latest_delivered = _wait_until(
                app, lambda: latest_requested in delivered_at, 3.0
            )
            released_at = float(slow_state["released_at"])
            after_release_ms = (
                (delivered_at[latest_requested] - released_at) * 1000.0
                if latest_delivered and released_at > 0.0
                else 0.0
            )
            slow_recovery = {
                "requested_delay_ms": slow_frame_ms,
                "delay_consumed": bool(slow_state["consumed"]),
                "latest_delivered": latest_delivered,
                "after_release_ms": round(after_release_ms, 3),
                "passed": bool(
                    slow_state["consumed"]
                    and latest_delivered
                    and after_release_ms <= 250.0
                ),
            }
        renderer.set_playing(False)
        settle_deadline = time.monotonic() + 0.25
        while time.monotonic() < settle_deadline:
            app.processEvents()
            time.sleep(0.005)
        process = renderer._renderer
        if process is not None and process.is_running:
            selected_warp = bool(
                renderer.stats_snapshot().get("warp_selected", args.force_warp)
            )
            native_diagnostics = process.gpu_diagnostics(
                force_warp=selected_warp
            )
            try:
                import psutil
            except ImportError:
                pass
            else:
                try:
                    native_diagnostics["sidecar_rss_bytes"] = psutil.Process(
                        int(process.process_id or 0)
                    ).memory_info().rss
                except (OSError, psutil.Error):
                    pass
    finally:
        renderer.stop()
        if native_host is not None:
            native_host.close()
            native_host.deleteLater()
            app.processEvents()

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
    playback_timestamps = [
        int(row["rendered_t_ms"])
        for row in rows
        if str(row["phase"]) == "playback"
    ]
    monotonic_violations = sum(
        current < previous
        for previous, current in zip(playback_timestamps, playback_timestamps[1:])
    )
    stats = renderer.stats_snapshot()
    scheduler_gate_passed = (
        monotonic_violations == 0
        and int(stats["max_in_flight"]) <= int(stats["worker_count"])
        and int(stats["max_pending"]) <= 1
    )
    summary = {
        "requested_playback_frames": frame_count,
        "worker_count_requested": args.worker_count,
        "transport": "direct_composition" if args.native_preview else "shared_memory_qimage",
        "delivered_frames": len(rows),
        "delivered_by_phase": dict(delivered_by_phase),
        "playback_delivery_rate": delivered_by_phase.get("playback", 0) / frame_count,
        "stats": stats,
        "playback_monotonic_violations": monotonic_violations,
        "scheduler_gate_passed": scheduler_gate_passed,
        "timings": renderer.timing_snapshot(),
        "native_diagnostics": native_diagnostics,
        "kill_recovery": kill_recovery,
        "slow_recovery": slow_recovery,
        "csv": str(args.output),
    }
    summary_path = args.output.with_suffix(".json")
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return (
        0
        if summary["stats"]["renderer_failures"] == (1 if args.kill_sidecar else 0)
        and summary["scheduler_gate_passed"]
        and summary["kill_recovery"]["passed"]
        and summary["slow_recovery"]["passed"]
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
