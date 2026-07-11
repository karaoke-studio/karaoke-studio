from __future__ import annotations

import argparse
import csv
import os
import statistics
import sys
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@dataclass(frozen=True)
class PreviewBackendSample:
    t_ms: int
    latency_ms: float
    phase: str = "request"


@dataclass
class _BackendRunState:
    backend: str
    requested_at: dict[int, float] = field(default_factory=dict)
    samples: list[PreviewBackendSample] = field(default_factory=list)
    images: dict[int, object] = field(default_factory=dict)
    phase: str = "request"


def _playback_times(*, duration_ms: int, fps: int) -> list[int]:
    duration = max(int(duration_ms), 0)
    step = max(int(round(1000 / max(int(fps), 1))), 1)
    times = list(range(0, duration + 1, step))
    if not times or times[-1] != duration:
        times.append(duration)
    return times


def _mean(values: Iterable[float]) -> float:
    items = list(values)
    return statistics.fmean(items) if items else 0.0


def _percentile(values: Iterable[float], q: float) -> float:
    items = sorted(values)
    if not items:
        return 0.0
    index = min(len(items) - 1, max(0, int(round(q * (len(items) - 1)))))
    return items[index]


def _format_t_ms_list(values: Iterable[int], *, limit: int = 24) -> str:
    items = [int(value) for value in values]
    head = items[: max(int(limit), 0)]
    text = ";".join(str(value) for value in head)
    if len(items) > len(head):
        suffix = f"...(+{len(items) - len(head)})"
        return f"{text};{suffix}" if text else suffix
    return text


def _samples_by_t(samples: Iterable[PreviewBackendSample]) -> dict[int, list[PreviewBackendSample]]:
    by_t: dict[int, list[PreviewBackendSample]] = {}
    for sample in samples:
        by_t.setdefault(int(sample.t_ms), []).append(sample)
    return by_t


def _leading_missing_count(requested_t_ms: list[int], ready_t_ms: set[int]) -> int:
    count = 0
    for t_ms in requested_t_ms:
        if t_ms in ready_t_ms:
            break
        count += 1
    return count


def _trailing_missing_count(requested_t_ms: list[int], ready_t_ms: set[int]) -> int:
    count = 0
    for t_ms in reversed(requested_t_ms):
        if t_ms in ready_t_ms:
            break
        count += 1
    return count


def _summarize_backend_samples(
    backend: str,
    *,
    requested_count: int | None = None,
    requested_t_ms: Iterable[int] | None = None,
    duration_ms: int,
    samples: list[PreviewBackendSample],
    extra: dict[str, int] | None = None,
) -> dict[str, str | int]:
    by_t = _samples_by_t(samples)
    first_by_t: dict[int, PreviewBackendSample] = {
        t_ms: events[0] for t_ms, events in by_t.items() if events
    }
    if requested_t_ms is None:
        requested = sorted(first_by_t)
        requested_frames = int(requested_count or 0)
    else:
        requested = [int(t_ms) for t_ms in requested_t_ms]
        requested_frames = len(requested)
    requested_set = set(requested)
    latencies = [sample.latency_ms for sample in first_by_t.values()]
    ready_events = len(samples)
    ready_count = len(first_by_t) if requested_t_ms is None else len(requested_set & set(first_by_t))
    duplicate_t_ms = sorted(t_ms for t_ms, events in by_t.items() if len(events) > 1)
    missing_t_ms = [t_ms for t_ms in requested if t_ms not in first_by_t]
    ready_t_ms = requested_set & set(first_by_t)
    leading_missing = _leading_missing_count(requested, ready_t_ms)
    trailing_missing = _trailing_missing_count(requested, ready_t_ms)
    steady_requested = requested_frames - leading_missing - trailing_missing
    steady_missing = max(len(missing_t_ms) - leading_missing - trailing_missing, 0)
    steady_ready = max(steady_requested - steady_missing, 0)
    settle_ready_t_ms = sorted(
        t_ms
        for t_ms in requested_set
        if t_ms in first_by_t and first_by_t[t_ms].phase == "settle"
    )
    row: dict[str, str | int] = {
        "backend": backend,
        "requested_frames": requested_frames,
        "ready_events": ready_events,
        "ready_frames": ready_count,
        "duplicate_ready_events": max(ready_events - ready_count, 0),
        "dropped_frames": max(requested_frames - ready_count, 0),
        "leading_missing_frames": leading_missing,
        "trailing_missing_frames": trailing_missing,
        "steady_requested_frames": steady_requested,
        "steady_ready_frames": steady_ready,
        "steady_dropped_frames": steady_missing,
        "ready_in_settle_frames": len(settle_ready_t_ms),
        "ready_fps": f"{(ready_count * 1000.0 / max(int(duration_ms), 1)):.2f}",
        "latency_mean_ms": f"{_mean(latencies):.4f}",
        "latency_p50_ms": f"{_percentile(latencies, 0.5):.4f}",
        "latency_p95_ms": f"{_percentile(latencies, 0.95):.4f}",
        "missing_t_ms": _format_t_ms_list(missing_t_ms),
        "duplicate_t_ms": _format_t_ms_list(duplicate_t_ms),
        "settle_ready_t_ms": _format_t_ms_list(settle_ready_t_ms),
    }
    if extra:
        row.update({key: int(value) for key, value in extra.items()})
    return row


def _detail_rows(
    backend: str,
    *,
    requested_t_ms: Iterable[int],
    samples: list[PreviewBackendSample],
) -> list[dict[str, str | int]]:
    by_t = _samples_by_t(samples)
    rows: list[dict[str, str | int]] = []
    for index, t_ms in enumerate(int(value) for value in requested_t_ms):
        events = by_t.get(t_ms, [])
        first = events[0] if events else None
        rows.append(
            {
                "backend": backend,
                "request_index": index,
                "t_ms": t_ms,
                "ready_events": len(events),
                "duplicate_ready_events": max(len(events) - 1, 0),
                "missing": 0 if first is not None else 1,
                "first_latency_ms": f"{first.latency_ms:.4f}" if first is not None else "",
                "first_ready_phase": first.phase if first is not None else "",
            }
        )
    return rows


def _image_diff_summary(first, second, *, max_samples: int = 20_000) -> dict[str, int | str]:
    from PyQt6.QtGui import QImage

    a = first.convertToFormat(QImage.Format.Format_RGBA8888)
    b = second.convertToFormat(QImage.Format.Format_RGBA8888)
    width = min(a.width(), b.width())
    height = min(a.height(), b.height())
    if width <= 0 or height <= 0:
        return {
            "width": width,
            "height": height,
            "sampled_pixels": 0,
            "changed_pixels": 0,
            "max_channel_delta": 0,
        }
    total = width * height
    stride = max(int(round((total / max(int(max_samples), 1)) ** 0.5)), 1)
    sampled = 0
    changed = 0
    max_delta = 0
    for y in range(0, height, stride):
        for x in range(0, width, stride):
            ca = a.pixelColor(x, y)
            cb = b.pixelColor(x, y)
            deltas = (
                abs(ca.red() - cb.red()),
                abs(ca.green() - cb.green()),
                abs(ca.blue() - cb.blue()),
                abs(ca.alpha() - cb.alpha()),
            )
            channel_delta = max(deltas)
            if channel_delta > 0:
                changed += 1
                max_delta = max(max_delta, channel_delta)
            sampled += 1
    return {
        "width": width,
        "height": height,
        "sampled_pixels": sampled,
        "changed_pixels": changed,
        "max_channel_delta": max_delta,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Compare Python and native async subtitle preview backends.")
    parser.add_argument("--lrc", required=True, type=Path, help="Nicokara LRC file")
    parser.add_argument("--duration-ms", type=int, default=5000, help="Playback simulation duration")
    parser.add_argument("--fps", type=int, default=60, help="Preview request FPS")
    parser.add_argument("--width", type=int, default=1920, help="Preview render width")
    parser.add_argument("--height", type=int, default=1080, help="Preview render height")
    parser.add_argument("--sample-images", type=int, default=5, help="Images to retain per backend for diff")
    parser.add_argument("--settle-ms", type=int, default=1500, help="Extra wait after last request")
    parser.add_argument("--native-renderer", type=Path, default=None, help="Optional native sidecar executable")
    parser.add_argument("--out", type=Path, default=None, help="Summary CSV output path")
    parser.add_argument("--details-out", type=Path, default=None, help="Per-request detail CSV output path")
    parser.add_argument("--offscreen", action="store_true", help="Set QT_QPA_PLATFORM=offscreen")
    return parser


def _run_backend(
    backend: str,
    *,
    track,
    style,
    width: int,
    height: int,
    times: list[int],
    fps: int,
    sample_images: int,
    settle_ms: int,
) -> tuple[dict[str, str | int], dict[int, object], list[dict[str, str | int]]]:
    from PyQt6.QtCore import QTimer
    from PyQt6.QtWidgets import QApplication

    from krok_helper.subtitle_render.frontend.preview_async import (
        AsyncSubtitleRenderer,
        NativeAsyncSubtitleRenderer,
    )

    app = QApplication.instance() or QApplication([])
    renderer_cls = NativeAsyncSubtitleRenderer if backend == "native" else AsyncSubtitleRenderer
    renderer = renderer_cls(width, height)
    state = _BackendRunState(backend=backend)
    step_ms = max(int(round(1000 / max(int(fps), 1))), 1)
    retain_every = max(len(times) // max(int(sample_images), 1), 1)
    requested_count = len(times)

    def on_frame_ready(image, t_ms: int) -> None:
        now = time.perf_counter()
        requested_at = state.requested_at.get(int(t_ms))
        if requested_at is None:
            return
        state.samples.append(
            PreviewBackendSample(
                t_ms=int(t_ms),
                latency_ms=(now - requested_at) * 1000.0,
                phase=state.phase,
            )
        )
        index = times.index(int(t_ms)) if int(t_ms) in times else -1
        if index >= 0 and index % retain_every == 0 and len(state.images) < sample_images:
            state.images[int(t_ms)] = image.copy()

    renderer.frame_ready.connect(on_frame_ready)
    renderer.set_state(track, style)
    renderer.set_render_target(width, height, 1.0)
    renderer.set_playing(True)

    state_index = {"value": 0}

    def tick() -> None:
        index = state_index["value"]
        if index >= len(times):
            renderer.set_playing(False)
            state.phase = "settle"
            QTimer.singleShot(max(int(settle_ms), 0), app.quit)
            return
        t_ms = int(times[index])
        state.requested_at[t_ms] = time.perf_counter()
        renderer.request(t_ms)
        state_index["value"] = index + 1
        QTimer.singleShot(step_ms, tick)

    QTimer.singleShot(0, tick)
    app.exec()
    extra = {}
    if hasattr(renderer, "stats_snapshot"):
        extra = renderer.stats_snapshot()
    renderer.stop()
    summary = _summarize_backend_samples(
        backend,
        requested_t_ms=times,
        duration_ms=max(times[-1] if times else 0, 1),
        samples=state.samples,
        extra=extra,
    )
    details = _detail_rows(backend, requested_t_ms=times, samples=state.samples)
    return summary, state.images, details


def _write_csv(path: Path, rows: list[dict[str, str | int]]) -> None:
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


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.offscreen:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    if args.native_renderer is not None:
        os.environ["KROK_SUBTITLE_NATIVE_RENDERER"] = str(args.native_renderer)

    from PyQt6.QtWidgets import QApplication

    from krok_helper.subtitle_render.models import Style
    from krok_helper.subtitle_render.subtitle_sources import load_nicokara_lrc

    _ = QApplication.instance() or QApplication([])
    track = load_nicokara_lrc(args.lrc)
    style = replace(Style(), entry_anim="utopia", exit_anim="utopia", decoration_kind="glow")
    times = _playback_times(duration_ms=args.duration_ms, fps=args.fps)

    rows: list[dict[str, str | int]] = []
    detail_rows: list[dict[str, str | int]] = []
    images_by_backend: dict[str, dict[int, object]] = {}
    for backend in ("python", "native"):
        summary, images, details = _run_backend(
            backend,
            track=track,
            style=style,
            width=args.width,
            height=args.height,
            times=times,
            fps=args.fps,
            sample_images=args.sample_images,
            settle_ms=args.settle_ms,
        )
        rows.append(summary)
        detail_rows.extend(details)
        images_by_backend[backend] = images
        print(
            f"{backend}: ready={summary['ready_frames']}/{summary['requested_frames']} "
            f"events={summary['ready_events']} dup={summary['duplicate_ready_events']} "
            f"leading_miss={summary['leading_missing_frames']} "
            f"steady_drop={summary['steady_dropped_frames']} "
            f"settle={summary['ready_in_settle_frames']} fps={summary['ready_fps']} "
            f"latency_p95={summary['latency_p95_ms']}ms"
        )
        if summary.get("missing_t_ms"):
            print(f"{backend} missing_t_ms: {summary['missing_t_ms']}")
        if summary.get("duplicate_t_ms"):
            print(f"{backend} duplicate_t_ms: {summary['duplicate_t_ms']}")

    common_times = sorted(set(images_by_backend["python"]) & set(images_by_backend["native"]))
    for t_ms in common_times:
        diff = _image_diff_summary(images_by_backend["python"][t_ms], images_by_backend["native"][t_ms])
        row = {"backend": "quality", "t_ms": t_ms}
        row.update(diff)
        rows.append(row)
        print(
            f"quality t={t_ms}ms: changed={diff['changed_pixels']}/{diff['sampled_pixels']} "
            f"max_delta={diff['max_channel_delta']}"
        )

    if args.out is None:
        out = ROOT / ".bench" / f"preview_backends_{time.strftime('%Y%m%d_%H%M%S')}.csv"
    else:
        out = args.out
    _write_csv(out, rows)
    print(f"CSV -> {out}")
    if args.details_out is None:
        details_out = out.with_name(f"{out.stem}_details{out.suffix}")
    else:
        details_out = args.details_out
    _write_csv(details_out, detail_rows)
    print(f"Details CSV -> {details_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
