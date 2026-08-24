"""Benchmark the configured G1 Direct2D subtitle path and optionally emit CSV."""

from __future__ import annotations

import argparse
import csv
from dataclasses import replace
from datetime import datetime
import json
import os
from pathlib import Path
import platform
import statistics
import subprocess
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
from krok_helper.subtitle_render.native.backend import (  # noqa: E402
    NativeRendererProcess,
    SharedFrameRingReader,
)


DEFAULT_PROJECT = Path(r"C:\Users\18007\Downloads\芽吹の唄 - 大原ゆい子.yurika")
DEFAULT_STROKE_SWEEP = (0.0, 5.0, 14.0, 30.0, 50.0)
FRAME_DIAGNOSTIC_FIELDS = (
    "brush_created",
    "geometry_created_stable",
    "geometry_created_dynamic",
    "realization_hit",
    "realization_miss",
    "stroke_draw",
    "stroke2_draw",
    "glow_source_area_px",
    "layer_push",
    "animation_layout_ms",
    "geometry_ms",
    "stroke_ms",
    "glow_ms",
    "gpu_wait_ms",
    "readback_copy_ms",
    "shm_copy_ms",
)


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * fraction))]


def _load_project(project_path: Path) -> tuple[TimingTrack, Style, int, int, int]:
    from krok_helper.subtitle_render.models import style_from_dict
    from krok_helper.subtitle_render.project.store import load_render_project
    from krok_helper.subtitle_render.sources.subtitles import load_nicokara_lrc

    data = load_render_project(project_path)
    subtitle_path = Path(str(data["subtitle_path"]))
    track = load_nicokara_lrc(subtitle_path)
    style = style_from_dict(data.get("style"))
    screen = data.get("screen", {})
    return (
        track,
        style,
        int(screen.get("width", 1920)),
        int(screen.get("height", 1080)),
        int(screen.get("fps", 60)),
    )


def _git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=_REPO_ROOT,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _gpu_driver_version(adapter_name: str) -> str:
    if platform.system() != "Windows":
        return "unknown"
    command = (
        "Get-CimInstance Win32_VideoController | "
        "Select-Object Name,DriverVersion | ConvertTo-Json -Compress"
    )
    try:
        output = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command", command],
            text=True,
            encoding="utf-8",
            errors="replace",
            stderr=subprocess.DEVNULL,
        ).strip()
        payload = json.loads(output)
        items = payload if isinstance(payload, list) else [payload]
        adapter_lower = adapter_name.lower()
        for item in items:
            name = str(item.get("Name", ""))
            if name and (name.lower() in adapter_lower or adapter_lower in name.lower()):
                return str(item.get("DriverVersion", "unknown"))
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError):
        pass
    return "unknown"


def _parse_csv_numbers(value: str) -> list[float]:
    return [float(item.strip()) for item in value.split(",") if item.strip()]


def _parse_time_points(value: str, duration_ms: int) -> list[tuple[str, int]]:
    defaults = {
        "T1": max(0, duration_ms // 20),
        "T2": max(0, duration_ms // 8),
        "T3": max(0, duration_ms // 2),
        "T4": max(0, duration_ms * 7 // 8),
    }
    points: list[tuple[str, int]] = []
    for index, raw in enumerate(item.strip() for item in value.split(",")):
        if not raw:
            continue
        if ":" in raw or "=" in raw:
            separator = ":" if ":" in raw else "="
            label, milliseconds = raw.split(separator, 1)
            points.append((label.strip() or f"T{index + 1}", int(milliseconds)))
        elif raw.upper() in defaults:
            label = raw.upper()
            points.append((label, defaults[label]))
        else:
            points.append((f"T{index + 1}", int(raw)))
    if not points:
        raise ValueError("--time-points 至少需要一个时间点")
    return points


def _with_main_stroke_width(style: Style, stroke_width: float) -> Style:
    """Apply a benchmark sweep width to global and inline-role main text."""
    width = max(int(round(stroke_width)), 0)

    def update_scheme(scheme):
        return replace(
            scheme,
            stroke_width_px=width,
            latin_stroke_width_px=width,
        )

    return replace(
        style,
        stroke_width_px=width,
        latin_stroke_width_px=width,
        singer_style_overrides={
            key: update_scheme(scheme)
            for key, scheme in style.singer_style_overrides.items()
        },
        custom_style_schemes={
            key: update_scheme(scheme)
            for key, scheme in style.custom_style_schemes.items()
        },
    )


def _with_fixed_style_metrics(
    style: Style,
    *,
    font_size: int | None,
    stroke2_width: int | None,
    glow_radius: int | None,
) -> Style:
    updates: dict[str, object] = {}
    scheme_updates: dict[str, object] = {}
    if font_size is not None:
        value = max(int(font_size), 1)
        updates.update(font_size_px=value, latin_font_size_px=value)
        scheme_updates.update(font_size_px=value, latin_font_size_px=value)
    if stroke2_width is not None:
        value = max(int(stroke2_width), 0)
        updates.update(
            stroke2_enabled=value > 0,
            stroke2_width_px=value,
            latin_stroke2_enabled=value > 0,
            latin_stroke2_width_px=value,
        )
        scheme_updates.update(
            stroke2_enabled=value > 0,
            stroke2_width_px=value,
            latin_stroke2_enabled=value > 0,
            latin_stroke2_width_px=value,
        )
    if glow_radius is not None:
        value = max(int(glow_radius), 0)
        updates.update(
            glow_radius_px=value,
            glow_before_radius_px=value,
            glow_after_radius_px=value,
        )
        scheme_updates.update(
            glow_radius_px=value,
            glow_before_radius_px=value,
            glow_after_radius_px=value,
        )
    if not updates:
        return style
    return replace(
        style,
        **updates,
        singer_style_overrides={
            key: replace(scheme, **scheme_updates)
            for key, scheme in style.singer_style_overrides.items()
        },
        custom_style_schemes={
            key: replace(scheme, **scheme_updates)
            for key, scheme in style.custom_style_schemes.items()
        },
    )


def _scene(
    duration_ms: int,
    *,
    glow: bool,
    animation: str = "none",
    ruby: bool = False,
    signals: bool = False,
    signal_style: str = "volume",
    vertical: bool = False,
    rtl: bool = False,
    viewport: bool = False,
    per_row: bool = False,
) -> tuple[TimingTrack, Style]:
    text = "縦書きGPU" if vertical else "Karaoke Studio GPU"
    chars = [
        TimingChar(char, index * duration_ms // len(text))
        for index, char in enumerate(text)
    ]
    track = TimingTrack(lines=[TimingLine(chars=chars, end_ms=duration_ms)])
    if per_row:
        track.lines.append(
            TimingLine(
                chars=[
                    TimingChar(char, index * duration_ms // len(text))
                    for index, char in enumerate(reversed(text))
                ],
                end_ms=duration_ms,
            )
        )
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
        line_horizontal_layout="per_row" if per_row else "center",
        line_alignments=["left", "right"],
        dual_line_layout=per_row,
        row1_align="left",
        row1_offset_x=72,
        row1_offset_y=-18,
        row2_align="right",
        row2_offset_x=-84,
        row2_offset_y=22,
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
        right_to_left=rtl,
        viewport_scale_pct=115 if viewport else 100,
        viewport_rotation_deg=12 if viewport else 0,
        viewport_offset_x=24 if viewport else 0,
        viewport_offset_y=-18 if viewport else 0,
        viewport_align="center",
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
    rtl: bool = False,
    viewport: bool = False,
    per_row: bool = False,
    project_path: Path | None = None,
    stroke_width: float | None = None,
    time_points: list[tuple[str, int]] | None = None,
    warmup_frames: int = 30,
    decoration_mode: str = "project",
    fixed_font_size: int | None = None,
    fixed_stroke2_width: int | None = None,
    fixed_glow_radius: int | None = None,
    realization_wait_s: float = 0.0,
) -> tuple[dict, list[dict]]:
    import psutil
    from PyQt6.QtWidgets import QApplication

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    duration_ms = max(1, int(round(seconds * 1000.0)))
    if project_path is not None:
        track, style, project_width, project_height, project_fps = _load_project(
            project_path
        )
        width = project_width if width <= 0 else width
        height = project_height if height <= 0 else height
        fps = project_fps if fps <= 0 else fps
        if animation != "project":
            style = replace(style, entry_anim=animation, exit_anim=animation)
        if glow:
            style = replace(style, decoration_kind="glow")
    else:
        synthetic_animation = "none" if animation == "project" else animation
        track, style = _scene(
            duration_ms,
            glow=glow,
            animation=synthetic_animation,
            ruby=ruby,
            signals=signals,
            signal_style=signal_style,
            vertical=vertical,
            rtl=rtl,
            viewport=viewport,
            per_row=per_row,
        )
    if stroke_width is not None:
        style = _with_main_stroke_width(style, stroke_width)
    style = _with_fixed_style_metrics(
        style,
        font_size=fixed_font_size,
        stroke2_width=fixed_stroke2_width,
        glow_radius=fixed_glow_radius,
    )
    if decoration_mode != "project":
        style = replace(
            style,
            decoration_kind=decoration_mode,
            ruby_decoration_kind=decoration_mode,
        )
    samples = time_points or [
        ("continuous", frame_index * 1000 // fps)
        for frame_index in range(max(1, int(round(seconds * fps))))
    ]
    frames = len(samples)
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
            prewarm_t_ms=samples[0][1],
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
                prewarm_t_ms=samples[0][1],
            )
        cache_hits_after_reconfigure = int(configured["cache_hits"])
        if realization_wait_s > 0.0:
            deadline = time.monotonic() + realization_wait_s
            while time.monotonic() < deadline:
                prewarm = renderer.gpu_diagnostics(force_warp=force_warp)
                if bool(prewarm.get("realization_prewarm_complete", True)):
                    break
                time.sleep(0.05)
        try:
            for warmup_index in range(max(int(warmup_frames), 0)):
                warm_event = renderer.render_gpu_frame(
                    samples[warmup_index % len(samples)][1],
                    force_warp=force_warp,
                    generation=0,
                    frame_index=warmup_index,
                    shm_key=shm_key,
                    readback_bands=bands,
                )
                if reader is None:
                    reader = SharedFrameRingReader.from_event(warm_event)
                    reader.attach()
                reader.read_qimage(warm_event)
            warm_diagnostics = renderer.gpu_diagnostics(force_warp=force_warp)
            warm_rss_bytes = sidecar_process.memory_info().rss
        except Exception:
            if reader is not None:
                reader.close()
            raise
        start = time.perf_counter()
        try:
            for frame_index, (time_point, t_ms) in enumerate(samples):
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
                qimage_start = time.perf_counter()
                image = reader.read_qimage(event)
                qimage_ms = (time.perf_counter() - qimage_start) * 1000.0
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
                        "time_point": time_point,
                        "stroke_width_px": float(style.stroke_width_px),
                        "render_ms": float(event["render_ms"]),
                        "readback_ms": float(event["readback_ms"]),
                        "qimage_ms": qimage_ms,
                        "roundtrip_ms": (time.perf_counter() - request_start) * 1000.0,
                        "checksum": str(event["checksum"]),
                        "payload_bytes": int(event["payload_bytes"]),
                        "readback_ratio": float(event.get("readback_ratio", 1.0)),
                        **{
                            field: event.get(field, 0)
                            for field in FRAME_DIAGNOSTIC_FIELDS
                        },
                    }
                )
        finally:
            if reader is not None:
                reader.close()
        if warm_diagnostics is None:
            warm_diagnostics = renderer.gpu_diagnostics(force_warp=force_warp)
            warm_rss_bytes = sidecar_process.memory_info().rss
        end_diagnostics = renderer.gpu_diagnostics(force_warp=force_warp)
        end_rss_bytes = sidecar_process.memory_info().rss
        elapsed = time.perf_counter() - start
    app.processEvents()

    render_times = [row["render_ms"] for row in rows]
    readback_times = [row["readback_ms"] for row in rows]
    roundtrip_times = [row["roundtrip_ms"] for row in rows]
    summary = {
        "adapter": configured["adapter"],
        "driver_version": _gpu_driver_version(str(configured["adapter"])),
        "commit": _git_commit(),
        "captured_at": datetime.now().astimezone().isoformat(),
        "backend": "warp" if force_warp else "hardware",
        "cached_chars": configured["cached_chars"],
        "cached_geometries": configured["cached_geometries"],
        "elapsed_ms": round(elapsed * 1000.0, 3),
        "estimated_cache_bytes": configured["estimated_cache_bytes"],
        "feature_level": configured["feature_level"],
        "fps": round(frames / elapsed, 3),
        "frames": frames,
        "project": str(project_path) if project_path is not None else "synthetic",
        "stroke_width_px": float(style.stroke_width_px),
        "font_size_px": int(style.font_size_px),
        "stroke2_width_px": int(style.stroke2_width_px),
        "glow_radius_px": int(style.glow_before_radius_px),
        "glow": glow,
        "decoration_mode": decoration_mode,
        "animation": animation,
        "ruby": ruby,
        "signals": signals,
        "signal_style": signal_style if signals else "none",
        "vertical": vertical,
        "rtl": rtl,
        "per_row": per_row,
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
        "qimage_mean_ms": round(
            statistics.fmean(float(row["qimage_ms"]) for row in rows), 4
        ),
        "width": width,
        "cache_hits_reconfigure_delta": (
            cache_hits_after_reconfigure - cache_hits_before_reconfigure
        ),
        "cache_bytes_warmup": int(warm_diagnostics["estimated_cache_bytes"]),
        "cache_bytes_end": int(end_diagnostics["estimated_cache_bytes"]),
        "realization_enabled": bool(end_diagnostics.get("realization_enabled", False)),
        "realization_supported": bool(
            end_diagnostics.get("realization_supported", False)
        ),
        "realization_prewarm_complete": bool(
            end_diagnostics.get("realization_prewarm_complete", True)
        ),
        "realization_count": int(end_diagnostics.get("realization_count", 0)),
        "realization_capacity": int(end_diagnostics.get("realization_capacity", 0)),
        "realization_prewarm_skipped": int(
            end_diagnostics.get("realization_prewarm_skipped", 0)
        ),
        "realization_prewarm_ms": round(
            float(end_diagnostics.get("realization_prewarm_ms", 0.0)), 4
        ),
        "glow_dirty_rect_enabled": bool(
            end_diagnostics.get("glow_dirty_rect_enabled", False)
        ),
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
    parser.add_argument(
        "--decoration",
        choices=("project", "none", "shadow", "glow"),
        default="project",
        help="覆盖工程正文与注音装饰；none 用于非目标场景门禁",
    )
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
    parser.add_argument("--rtl", action="store_true", help="lay out and wipe text right-to-left")
    parser.add_argument(
        "--viewport",
        action="store_true",
        help="apply scale, rotation, offset, and center-pivot viewport transform",
    )
    parser.add_argument(
        "--per-row",
        action="store_true",
        help="render two rows with independent alignment and offsets",
    )
    parser.add_argument(
        "--animation",
        choices=(
            "project", "none", "fade", "char_fade", "char_drip",
            "spin_flip", "utopia",
        ),
        default="project",
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
    parser.add_argument(
        "--project",
        type=Path,
        default=DEFAULT_PROJECT if DEFAULT_PROJECT.is_file() else None,
        help=".yurika 工程；本机存在固定宽描边工程时默认使用该工程",
    )
    parser.add_argument(
        "--stroke-sweep",
        default=",".join(str(value) for value in DEFAULT_STROKE_SWEEP),
        help="逗号分隔的主描边宽度，例如 0,5,14,30,50",
    )
    parser.add_argument("--font-size", type=int, help="固定正文有效字号")
    parser.add_argument("--stroke2-width", type=int, help="固定正文二重描边宽度")
    parser.add_argument("--glow-radius", type=int, help="固定正文 before/after 发光半径")
    parser.add_argument(
        "--time-points",
        default="T1,T2,T3,T4",
        help="逗号分隔的 T1..T4、毫秒值或 T1=毫秒",
    )
    parser.add_argument(
        "--samples-per-point",
        type=int,
        default=30,
        help="每个固定时间点重复采样帧数",
    )
    parser.add_argument(
        "--warmup-frames",
        type=int,
        default=30,
        help="记录前用于填充稳定资源的帧数",
    )
    parser.add_argument(
        "--realization",
        choices=("on", "off"),
        default="on",
        help="切换 Direct2D geometry realization，便于同进程参数做 A/B",
    )
    parser.add_argument(
        "--realization-wait-s",
        type=float,
        default=0.0,
        help="采样前最多等待 realization 后台预热完成的秒数",
    )
    parser.add_argument("--output-csv", type=Path)
    args = parser.parse_args()

    os.environ["KROK_GPU_REALIZATION"] = "1" if args.realization == "on" else "0"

    all_rows: list[dict] = []
    duration_ms = max(1, int(round(max(args.seconds, 0.001) * 1000.0)))
    points = _parse_time_points(args.time_points, duration_ms)
    sampled_points = [
        (label, milliseconds)
        for label, milliseconds in points
        for _ in range(max(args.samples_per_point, 1))
    ]
    stroke_sweep = _parse_csv_numbers(args.stroke_sweep)
    if not stroke_sweep:
        raise SystemExit("--stroke-sweep 至少需要一个描边宽度")
    for force_warp in ([False, True] if args.both else [bool(args.warp)]):
        for stroke_width in stroke_sweep:
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
                rtl=bool(args.rtl),
                viewport=bool(args.viewport),
                per_row=bool(args.per_row),
                project_path=args.project,
                stroke_width=stroke_width,
                time_points=sampled_points,
                warmup_frames=max(args.warmup_frames, 0),
                decoration_mode=args.decoration,
                fixed_font_size=args.font_size,
                fixed_stroke2_width=args.stroke2_width,
                fixed_glow_radius=args.glow_radius,
                realization_wait_s=max(args.realization_wait_s, 0.0),
            )
            all_rows.extend(rows)
            print(json.dumps(summary, ensure_ascii=False, sort_keys=True))

    output_csv = args.output_csv
    if output_csv is None:
        backend_label = "hardware-warp" if args.both else (
            "warp" if args.warp else "hardware"
        )
        output_csv = _REPO_ROOT / "build" / (
            f"gpu-widestroke-baseline-{backend_label}-"
            f"{datetime.now():%Y%m%d-%H%M%S}.csv"
        )
    if all_rows:
        output_csv.parent.mkdir(parents=True, exist_ok=True)
        with output_csv.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(all_rows[0]))
            writer.writeheader()
            writer.writerows(all_rows)
        print(json.dumps({"output_csv": str(output_csv)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
