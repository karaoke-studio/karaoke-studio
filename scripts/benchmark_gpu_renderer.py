"""Benchmark the configured G1 Direct2D subtitle path and optionally emit CSV."""

from __future__ import annotations

import argparse
import csv
import json
import os
from pathlib import Path
import statistics
import sys
import time
import uuid

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from krok_helper.subtitle_render.models import (  # noqa: E402
    RubyAnnotation,
    Style,
    TimingChar,
    TimingLine,
    TimingTrack,
)
from krok_helper.subtitle_render.native_backend import (  # noqa: E402
    NativeRendererProcess,
    SharedFrameRingReader,
)


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * fraction))]


def _scene(
    duration_ms: int,
    *,
    glow: bool,
    animation: str = "none",
    ruby: bool = False,
    signals: bool = False,
    signal_style: str = "volume",
    vertical: bool = False,
) -> tuple[TimingTrack, Style]:
    text = "縦書きGPU" if vertical else "Karaoke Studio GPU"
    chars = [
        TimingChar(char, index * duration_ms // len(text))
        for index, char in enumerate(text)
    ]
    track = TimingTrack(lines=[TimingLine(chars=chars, end_ms=duration_ms)])
    if ruby:
        if vertical:
            ruby_annotation = RubyAnnotation(
                kanji=text[:2],
                reading="たてがき",
                reading_parts=["たて", "がき"],
                reading_part_ms=[duration_ms // len(text)],
                pos_start_ms=0,
                pos_end_ms=duration_ms * 2 // len(text),
            )
        else:
            ruby_annotation = RubyAnnotation(
                kanji="Karaoke",
                reading="カラオケ",
                reading_parts=["カ", "ラ", "オ", "ケ"],
                reading_part_ms=[
                    duration_ms * 2 // len(text),
                    duration_ms * 4 // len(text),
                    duration_ms * 6 // len(text),
                ],
                pos_start_ms=0,
                pos_end_ms=duration_ms * 7 // len(text),
            )
        track.rubies = [ruby_annotation]
    style = Style(
        font_family="Meiryo",
        font_family_latin="Segoe UI",
        font_size_px=100,
        font_reference_height=1080,
        base_color="#FFFFFF",
        fill_color="#2F8BFF",
        stroke_color="#181818",
        stroke_width_px=5,
        stroke2_enabled=True,
        stroke2_width_px=5,
        decoration_kind="glow" if glow else "shadow",
        shadow_color="#2F8BFF" if glow else "#00000000",
        shadow_offset_x=0,
        shadow_offset_y=0,
        glow_radius_px=10,
        glow_before_radius_px=10,
        glow_after_radius_px=10,
        glow_concentration_level=1,
        line_y_position="center",
        line_horizontal_layout="center",
        dual_line_layout=False,
        line_lead_in_ms=0,
        line_tail_ms=0,
        entry_anim=animation,
        entry_lead_ms=1_000,
        exit_anim=animation,
        exit_fade_ms=1_000,
        ruby_decoration_kind="glow" if glow else "shadow",
        ruby_glow_before_radius_px=6,
        ruby_glow_after_radius_px=6,
        ruby_glow_concentration_level=1,
        lit_enabled=signals,
        lit_style=signal_style,
        signals_duration_ms=4_000,
        lit_opacity_pct=85,
        lit_stroke_width=3,
        volume_size=52,
        volume_column_width=14,
        volume_column_count=4,
        volume_column_spacing=3,
        volume_ratio=3.0,
        volume_fill_color="#FFFFFF",
        volume_stroke_color="#2F8BFF",
        volume_overlay_fill_color="#2F8BFF",
        volume_overlay_stroke_color="#FFFFFF",
        vertical=vertical,
    )
    return track, style


def run_benchmark(
    *,
    width: int,
    height: int,
    fps: int,
    seconds: float,
    force_warp: bool,
    glow: bool,
    bands: bool = False,
    reconfigure_cycles: int = 0,
    animation: str = "none",
    ruby: bool = False,
    signals: bool = False,
    signal_style: str = "volume",
    vertical: bool = False,
) -> tuple[dict, list[dict]]:
    import psutil

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    frames = max(1, int(round(seconds * fps)))
    duration_ms = max(1, int(round(seconds * 1000.0)))
    track, style = _scene(
        duration_ms,
        glow=glow,
        animation=animation,
        ruby=ruby,
        signals=signals,
        signal_style=signal_style,
        vertical=vertical,
    )
    shm_key = f"krok_gpu_g1_benchmark_{os.getpid()}_{uuid.uuid4().hex}"
    rows: list[dict] = []
    reader: SharedFrameRingReader | None = None
    warm_diagnostics: dict | None = None
    warm_rss_bytes = 0

    with NativeRendererProcess(response_timeout_s=30.0, close_timeout_s=2.0) as renderer:
        configured = renderer.configure_gpu(
            track,
            style,
            width=width,
            height=height,
            fps=fps,
            force_warp=force_warp,
        )
        assert renderer.process_id is not None
        sidecar_process = psutil.Process(renderer.process_id)
        cache_hits_before_reconfigure = int(configured["cache_hits"])
        for _ in range(max(int(reconfigure_cycles), 0)):
            configured = renderer.configure_gpu(
                track,
                style,
                width=width,
                height=height,
                fps=fps,
                force_warp=force_warp,
            )
        cache_hits_after_reconfigure = int(configured["cache_hits"])
        start = time.perf_counter()
        try:
            for frame_index in range(frames):
                t_ms = frame_index * 1000 // fps
                request_start = time.perf_counter()
                event = renderer.render_gpu_frame(
                    t_ms,
                    force_warp=force_warp,
                    generation=1,
                    frame_index=frame_index,
                    shm_key=shm_key,
                    readback_bands=bands,
                )
                if reader is None:
                    reader = SharedFrameRingReader.from_event(event)
                    reader.attach()
                image = reader.read_qimage(event)
                if image.width() != width or image.height() != height:
                    raise AssertionError(
                        f"frame dimensions mismatch: {image.width()}x{image.height()} != "
                        f"{width}x{height}"
                    )
                rows.append(
                    {
                        "backend": "warp" if force_warp else "hardware",
                        "frame_index": frame_index,
                        "t_ms": t_ms,
                        "render_ms": float(event["render_ms"]),
                        "readback_ms": float(event["readback_ms"]),
                        "roundtrip_ms": (time.perf_counter() - request_start) * 1000.0,
                        "checksum": str(event["checksum"]),
                        "payload_bytes": int(event["payload_bytes"]),
                        "readback_ratio": float(event.get("readback_ratio", 1.0)),
                    }
                )
                if warm_diagnostics is None and frame_index >= min(fps, frames - 1):
                    warm_diagnostics = renderer.gpu_diagnostics(force_warp=force_warp)
                    warm_rss_bytes = sidecar_process.memory_info().rss
        finally:
            if reader is not None:
                reader.close()
        if warm_diagnostics is None:
            warm_diagnostics = renderer.gpu_diagnostics(force_warp=force_warp)
            warm_rss_bytes = sidecar_process.memory_info().rss
        end_diagnostics = renderer.gpu_diagnostics(force_warp=force_warp)
        end_rss_bytes = sidecar_process.memory_info().rss
        elapsed = time.perf_counter() - start

    render_times = [row["render_ms"] for row in rows]
    readback_times = [row["readback_ms"] for row in rows]
    roundtrip_times = [row["roundtrip_ms"] for row in rows]
    summary = {
        "adapter": configured["adapter"],
        "backend": "warp" if force_warp else "hardware",
        "cached_chars": configured["cached_chars"],
        "cached_geometries": configured["cached_geometries"],
        "elapsed_ms": round(elapsed * 1000.0, 3),
        "estimated_cache_bytes": configured["estimated_cache_bytes"],
        "feature_level": configured["feature_level"],
        "fps": round(frames / elapsed, 3),
        "frames": frames,
        "glow": glow,
        "animation": animation,
        "ruby": ruby,
        "signals": signals,
        "signal_style": signal_style if signals else "none",
        "vertical": vertical,
        "bands": bands,
        "height": height,
        "readback_mean_ms": round(statistics.fmean(readback_times), 4),
        "readback_p95_ms": round(_percentile(readback_times, 0.95), 4),
        "readback_ratio_mean": round(
            statistics.fmean(row["readback_ratio"] for row in rows), 4
        ),
        "render_mean_ms": round(statistics.fmean(render_times), 4),
        "render_p95_ms": round(_percentile(render_times, 0.95), 4),
        "roundtrip_mean_ms": round(statistics.fmean(roundtrip_times), 4),
        "roundtrip_p95_ms": round(_percentile(roundtrip_times, 0.95), 4),
        "width": width,
        "cache_hits_reconfigure_delta": (
            cache_hits_after_reconfigure - cache_hits_before_reconfigure
        ),
        "cache_bytes_warmup": int(warm_diagnostics["estimated_cache_bytes"]),
        "cache_bytes_end": int(end_diagnostics["estimated_cache_bytes"]),
        "sidecar_rss_warmup_bytes": warm_rss_bytes,
        "sidecar_rss_end_bytes": end_rss_bytes,
        "sidecar_rss_growth_bytes": end_rss_bytes - warm_rss_bytes,
        "video_memory_info_available": bool(
            end_diagnostics["video_memory_info_available"]
        ),
        "local_video_memory_warmup_bytes": int(
            warm_diagnostics["local_video_memory_usage_bytes"]
        ),
        "local_video_memory_end_bytes": int(
            end_diagnostics["local_video_memory_usage_bytes"]
        ),
        "local_video_memory_growth_bytes": int(
            end_diagnostics["local_video_memory_usage_bytes"]
        )
        - int(warm_diagnostics["local_video_memory_usage_bytes"]),
        "non_local_video_memory_warmup_bytes": int(
            warm_diagnostics["non_local_video_memory_usage_bytes"]
        ),
        "non_local_video_memory_end_bytes": int(
            end_diagnostics["non_local_video_memory_usage_bytes"]
        ),
        "non_local_video_memory_growth_bytes": int(
            end_diagnostics["non_local_video_memory_usage_bytes"]
        )
        - int(warm_diagnostics["non_local_video_memory_usage_bytes"]),
    }
    return summary, rows


def main() -> int:
    parser = argparse.ArgumentParser(description="G1 Direct2D subtitle stability benchmark")
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--fps", type=int, default=60)
    parser.add_argument("--seconds", type=float, default=10.0)
    parser.add_argument("--warp", action="store_true", help="force Microsoft WARP")
    parser.add_argument("--both", action="store_true", help="run hardware then WARP")
    parser.add_argument("--glow", action="store_true", help="enable N3 medium glow")
    parser.add_argument("--ruby", action="store_true", help="include timed ruby units")
    parser.add_argument(
        "--signals", action="store_true", help="include Sayatoo signal indicators"
    )
    parser.add_argument(
        "--signal-style",
        choices=("volume", "circle", "square", "rounded"),
        default="volume",
        help="signal geometry used with --signals",
    )
    parser.add_argument("--vertical", action="store_true", help="stack main glyphs vertically")
    parser.add_argument(
        "--animation",
        choices=("none", "fade", "char_fade", "spin_flip", "utopia"),
        default="none",
        help="exercise a supported entry/exit animation",
    )
    parser.add_argument(
        "--bands",
        action="store_true",
        help="read back only packed subtitle bands and reconstruct the full frame",
    )
    parser.add_argument(
        "--reconfigure-cycles",
        type=int,
        default=0,
        help="repeat identical configure calls to verify bounded cache hits",
    )
    parser.add_argument("--output-csv", type=Path)
    args = parser.parse_args()

    all_rows: list[dict] = []
    for force_warp in ([False, True] if args.both else [bool(args.warp)]):
        summary, rows = run_benchmark(
            width=max(args.width, 1),
            height=max(args.height, 1),
            fps=max(args.fps, 1),
            seconds=max(args.seconds, 0.001),
            force_warp=force_warp,
            glow=bool(args.glow),
            bands=bool(args.bands),
            reconfigure_cycles=max(args.reconfigure_cycles, 0),
            animation=args.animation,
            ruby=bool(args.ruby),
            signals=bool(args.signals),
            signal_style=args.signal_style,
            vertical=bool(args.vertical),
        )
        all_rows.extend(rows)
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))

    if args.output_csv is not None:
        args.output_csv.parent.mkdir(parents=True, exist_ok=True)
        with args.output_csv.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(all_rows[0]))
            writer.writeheader()
            writer.writerows(all_rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
