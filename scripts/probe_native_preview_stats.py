from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _playback_times(*, duration_ms: int, fps: int) -> list[int]:
    duration = max(int(duration_ms), 0)
    normalized_fps = max(int(fps), 1)
    step = max(int(round(1000 / normalized_fps)), 1)
    times = list(range(0, duration + 1, step))
    if not times or times[-1] != duration:
        times.append(duration)
    return times


def _format_stats_line(
    *,
    elapsed_ms: int,
    t_ms: int,
    current: Mapping[str, int],
    previous: Mapping[str, int],
) -> str:
    def value(key: str) -> int:
        return int(current.get(key, 0))

    def delta(key: str) -> int:
        return int(current.get(key, 0)) - int(previous.get(key, 0))

    return (
        f"elapsed={elapsed_ms / 1000:.2f}s t={int(t_ms)}ms "
        f"hit={value('cache_hits')}(+{delta('cache_hits')}) "
        f"miss={value('cache_misses')}(+{delta('cache_misses')}) "
        f"future={value('future_frames_cached')}(+{delta('future_frames_cached')}) "
        f"stale={value('stale_frames_dropped')}(+{delta('stale_frames_dropped')}) "
        f"cancel={value('generations_cancelled')}(+{delta('generations_cancelled')})"
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Probe native subtitle preview scheduler stats with a real LRC/video pair.",
    )
    parser.add_argument("--lrc", required=True, type=Path, help="Nicokara LRC file")
    parser.add_argument("--video", required=True, type=Path, help="Background video file")
    parser.add_argument("--duration-ms", type=int, default=10_000, help="Simulation duration")
    parser.add_argument("--fps", type=int, default=60, help="Preview request FPS")
    parser.add_argument("--width", type=int, default=1920, help="Preview render width")
    parser.add_argument("--height", type=int, default=1080, help="Preview render height")
    parser.add_argument(
        "--report-every-ms",
        type=int,
        default=1000,
        help="Stats print interval while requests are being generated",
    )
    parser.add_argument(
        "--seek-every-ms",
        type=int,
        default=0,
        help="If >0, jump forward on this wall-clock interval to stress cancellation",
    )
    parser.add_argument(
        "--seek-step-ms",
        type=int,
        default=2000,
        help="Timeline jump amount used with --seek-every-ms",
    )
    parser.add_argument(
        "--native-renderer",
        type=Path,
        default=None,
        help="Optional krok_subtitle_renderer executable path",
    )
    parser.add_argument(
        "--offscreen",
        action="store_true",
        help="Set QT_QPA_PLATFORM=offscreen before constructing QApplication",
    )
    parser.add_argument(
        "--fast",
        action="store_true",
        help="Generate preview requests without wall-clock pacing",
    )
    return parser


def _preview_stats(window) -> dict[str, int]:
    renderer = getattr(window._preview_panel.canvas, "_async_renderer", None)
    if renderer is None or not hasattr(renderer, "stats_snapshot"):
        return {}
    return renderer.stats_snapshot()


def _run_probe(args: argparse.Namespace) -> int:
    if args.offscreen:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ["KROK_SUBTITLE_ASYNC_PREVIEW"] = "1"
    os.environ["KROK_SUBTITLE_NATIVE_RENDER"] = "1"
    if args.native_renderer is not None:
        os.environ["KROK_SUBTITLE_NATIVE_RENDERER"] = str(args.native_renderer)

    from PyQt6.QtCore import QTimer
    from PyQt6.QtWidgets import QApplication

    from krok_helper.subtitle_render.frontend.main_window import SubtitleRenderWindow

    app = QApplication.instance() or QApplication([])
    window = SubtitleRenderWindow(embedded=False)
    window.load_from_lrc(args.lrc)
    window.load_video(args.video)
    window._preview_panel.set_output_size(args.width, args.height)
    window._preview_panel.set_playing(True)

    times = _playback_times(duration_ms=args.duration_ms, fps=args.fps)
    previous_stats: dict[str, int] = {}
    state = {
        "index": 0,
        "elapsed_ms": 0,
        "last_report_ms": -max(int(args.report_every_ms), 1),
        "last_seek_ms": 0,
        "seek_offset_ms": 0,
        "last_t_ms": 0,
    }
    step_ms = max(int(round(1000 / max(args.fps, 1))), 1)
    report_every = max(int(args.report_every_ms), 1)

    def tick() -> None:
        nonlocal previous_stats
        if state["index"] >= len(times):
            window._preview_panel.set_playing(False)
            print(_format_stats_line(
                elapsed_ms=state["elapsed_ms"],
                t_ms=state["last_t_ms"],
                current=_preview_stats(window),
                previous=previous_stats,
            ))
            app.quit()
            return

        elapsed_ms = state["elapsed_ms"]
        if (
            args.seek_every_ms > 0
            and elapsed_ms > 0
            and elapsed_ms - state["last_seek_ms"] >= args.seek_every_ms
        ):
            state["last_seek_ms"] = elapsed_ms
            state["seek_offset_ms"] += int(args.seek_step_ms)

        raw_t_ms = times[state["index"]] + state["seek_offset_ms"]
        if state["seek_offset_ms"] > 0:
            t_ms = raw_t_ms % (max(int(args.duration_ms), 0) + 1)
        else:
            t_ms = min(raw_t_ms, args.duration_ms)
        state["last_t_ms"] = t_ms
        window._preview_panel.set_time(t_ms)

        if elapsed_ms - state["last_report_ms"] >= report_every:
            current = _preview_stats(window)
            print(
                _format_stats_line(
                    elapsed_ms=elapsed_ms,
                    t_ms=t_ms,
                    current=current,
                    previous=previous_stats,
                ),
                flush=True,
            )
            previous_stats = dict(current)
            state["last_report_ms"] = elapsed_ms

        state["index"] += 1
        state["elapsed_ms"] += step_ms
        QTimer.singleShot(0 if args.fast else step_ms, tick)

    QTimer.singleShot(0, tick)
    return int(app.exec())


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return _run_probe(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
