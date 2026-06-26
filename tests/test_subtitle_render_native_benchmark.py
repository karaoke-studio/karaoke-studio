from __future__ import annotations

import os

from scripts.bench_native_renderer import (
    TimingSample,
    _cache_modes,
    _format_counts,
    _int_map,
    _map_delta,
    _native_glow_cache_mode,
    _sample_timestamps,
    _sample_rows,
    _summarize_samples,
)
from scripts.compare_preview_backends import (
    PreviewBackendSample,
    _detail_rows,
    _image_diff_summary,
    _summarize_backend_samples,
)
from scripts.compare_export_backends import (
    ExportRunResult,
    _build_parser as _build_export_parser,
    _frame_count as _export_frame_count,
    _result_row as _export_result_row,
    _run_export as _run_export_backend,
    _sample_times as _export_sample_times,
)
from scripts.probe_native_preview_stats import (
    _build_parser,
    _churned_size,
    _churned_style,
    _format_stats_line,
    _format_summary_line,
    _playback_times,
    _summary_row,
)
from krok_helper.subtitle_render.models import Style
from pathlib import Path
from PyQt6.QtGui import QColor, QImage


def test_sample_timestamps_covers_window_endpoints() -> None:
    assert _sample_timestamps(start_ms=1000, frames=4, fps=2) == [1000, 1500, 2000, 2500]


def test_summarize_samples_reports_python_native_and_speedup() -> None:
    rows = _summarize_samples(
        [
            TimingSample(
                t_ms=0,
                python_ms=10.0,
                native_ms=5.0,
                native_cache_hits=0,
                native_cache_misses=1,
                native_render_ms=4.0,
                cache_hit_delta=0,
                cache_miss_delta=1,
                cache_shape_miss_delta=1,
                native_cache_shape_misses=1,
                cache_scope_miss_delta="main=1",
                native_cache_misses_by_scope="main=1",
            ),
            TimingSample(
                t_ms=16,
                python_ms=20.0,
                native_ms=5.0,
                native_cache_hits=1,
                native_cache_misses=1,
                native_render_ms=6.0,
                cache_hit_delta=1,
                cache_miss_delta=0,
                cache_content_variant_miss_delta=2,
                cache_evicted_key_miss_delta=3,
                native_cache_content_variant_misses=2,
                native_cache_evicted_key_misses=3,
                cache_scope_miss_delta="ruby=5",
                native_cache_misses_by_scope="main=1;ruby=5",
            ),
        ],
        scenario="fixture",
        width=640,
        height=360,
        cache_mode="on",
        native_mode="stats",
    )

    assert rows["scenario"] == "fixture"
    assert rows["cache_mode"] == "on"
    assert rows["native_mode"] == "stats"
    assert rows["frames"] == 2
    assert rows["python_mean_ms"] == "15.0000"
    assert rows["native_mean_ms"] == "5.0000"
    assert rows["native_render_mean_ms"] == "5.0000"
    assert rows["speedup"] == "3.00"
    assert rows["render_speedup"] == "3.00"
    assert rows["native_cache_hits"] == 1
    assert rows["native_cache_misses"] == 1
    assert rows["native_cache_hit_delta"] == 1
    assert rows["native_cache_miss_delta"] == 1
    assert rows["native_cache_shape_misses"] == 1
    assert rows["native_cache_content_variant_misses"] == 2
    assert rows["native_cache_evicted_key_misses"] == 3
    assert rows["native_cache_shape_miss_delta"] == 1
    assert rows["native_cache_content_variant_miss_delta"] == 2
    assert rows["native_cache_evicted_key_miss_delta"] == 3
    assert rows["native_cache_misses_by_scope"] == "main=1;ruby=5"


def test_sample_rows_reports_per_frame_cache_deltas() -> None:
    rows = _sample_rows(
        [
            TimingSample(
                t_ms=1000,
                python_ms=10.12345,
                native_ms=5.5,
                native_cache_hits=4,
                native_cache_misses=2,
                native_render_ms=4.25,
                frame_index=3,
                cache_hit_delta=2,
                cache_miss_delta=1,
                cache_shape_miss_delta=0,
                cache_content_variant_miss_delta=1,
                cache_evicted_key_miss_delta=0,
                native_cache_shape_misses=1,
                native_cache_content_variant_misses=2,
                native_cache_evicted_key_misses=3,
                cache_scope_miss_delta="ruby=1",
                native_cache_misses_by_scope="main=1;ruby=1",
                cache_mode="off",
                native_mode="png",
            )
        ],
        scenario="fixture",
    )

    assert rows == [
        {
            "scenario": "fixture",
            "cache_mode": "off",
            "native_mode": "png",
            "frame_index": 3,
            "t_ms": 1000,
            "python_ms": "10.1235",
            "native_ms": "5.5000",
            "native_render_ms": "4.2500",
            "cache_hit_delta": 2,
            "cache_miss_delta": 1,
            "cache_shape_miss_delta": 0,
            "cache_content_variant_miss_delta": 1,
            "cache_evicted_key_miss_delta": 0,
            "cache_scope_miss_delta": "ruby=1",
            "native_cache_hits": 4,
            "native_cache_misses": 2,
            "native_cache_shape_misses": 1,
            "native_cache_content_variant_misses": 2,
            "native_cache_evicted_key_misses": 3,
            "native_cache_misses_by_scope": "main=1;ruby=1",
        }
    ]


def test_cache_modes_expands_both() -> None:
    assert _cache_modes("on") == ["on"]
    assert _cache_modes("off") == ["off"]
    assert _cache_modes("both") == ["on", "off"]


def test_count_map_helpers_ignore_bad_values_and_format_deltas() -> None:
    current = _int_map({"ruby": "3", "main": 2, 1: 9, "bad": object()})
    previous = {"ruby": 1, "old": 5}

    assert current == {"main": 2, "ruby": 3}
    assert _map_delta(current, previous) == {"main": 2, "ruby": 2}
    assert _format_counts({"ruby": 2, "main": 1}) == "main=1;ruby=2"


def test_native_glow_cache_mode_restores_environment(monkeypatch) -> None:
    monkeypatch.setenv("KROK_SUBTITLE_NATIVE_GLOW_CACHE", "custom")
    monkeypatch.delenv("KROK_SUBTITLE_GLOW_CACHE", raising=False)

    with _native_glow_cache_mode("on"):
        assert "KROK_SUBTITLE_NATIVE_GLOW_CACHE" not in os.environ
        assert "KROK_SUBTITLE_GLOW_CACHE" not in os.environ

    assert os.environ["KROK_SUBTITLE_NATIVE_GLOW_CACHE"] == "custom"
    assert "KROK_SUBTITLE_GLOW_CACHE" not in os.environ


def test_preview_probe_playback_times_cover_duration_at_fps() -> None:
    assert _playback_times(duration_ms=100, fps=25) == [0, 40, 80, 100]
    assert _playback_times(duration_ms=0, fps=60) == [0]


def test_preview_probe_formats_stats_line_with_deltas() -> None:
    line = _format_stats_line(
        elapsed_ms=1200,
        t_ms=3400,
        current={
            "cache_hits": 5,
            "cache_misses": 8,
            "future_frames_cached": 3,
            "stale_frames_dropped": 2,
            "generations_cancelled": 1,
            "native_generation_cancelled_events": 4,
            "range_done_events": 7,
            "native_renderer_failures": 2,
        },
        previous={
            "cache_hits": 2,
            "cache_misses": 6,
            "future_frames_cached": 1,
            "stale_frames_dropped": 2,
            "generations_cancelled": 0,
            "native_generation_cancelled_events": 1,
            "range_done_events": 5,
            "native_renderer_failures": 1,
        },
    )

    assert line == (
        "elapsed=1.20s t=3400ms "
        "hit=5(+3) miss=8(+2) future=3(+2) stale=2(+0) "
        "cancel=1(+1) native_cancel=4(+3) done=7(+2) fail=2(+1)"
    )


def test_preview_probe_parses_resize_and_style_churn_options() -> None:
    args = _build_parser().parse_args(
        [
            "--lrc",
            "song.lrc",
            "--video",
            "bg.mp4",
            "--resize-every-ms",
            "250",
            "--resize-scale",
            "0.5",
            "--style-every-ms",
            "500",
            "--style-delta-px",
            "6",
            "--out",
            "summary.csv",
        ]
    )

    assert args.resize_every_ms == 250
    assert args.resize_scale == 0.5
    assert args.style_every_ms == 500
    assert args.style_delta_px == 6
    assert args.out.name == "summary.csv"


def test_preview_probe_summary_row_reports_machine_readable_totals() -> None:
    args = _build_parser().parse_args(
        [
            "--lrc",
            "song.lrc",
            "--video",
            "bg.mp4",
            "--duration-ms",
            "5000",
            "--fps",
            "60",
        ]
    )

    row = _summary_row(
        args=args,
        requests=296,
        elapsed_ms=5032,
        last_t_ms=5000,
        stats={
            "cache_hits": 180,
            "cache_misses": 120,
            "future_frames_cached": 800,
            "stale_frames_dropped": 10,
            "generations_cancelled": 3,
            "native_generation_cancelled_events": 2,
            "range_done_events": 250,
            "native_renderer_failures": 0,
        },
    )

    assert row["requests"] == 296
    assert row["cache_hit_rate"] == "0.6000"
    assert row["native_renderer_failures"] == 0
    assert row["duration_ms"] == 5000
    assert _format_summary_line(row).startswith("summary requests=296 elapsed_ms=5032")


def test_preview_probe_churned_size_alternates_scaled_output() -> None:
    assert _churned_size(1920, 1080, churn_index=0, scale=0.75) == (1920, 1080)
    assert _churned_size(1920, 1080, churn_index=1, scale=0.75) == (1440, 810)


def test_preview_probe_churned_style_alternates_font_size() -> None:
    style = Style(font_size_px=100)

    assert _churned_style(style, churn_index=0, delta_px=6).font_size_px == 100
    assert _churned_style(style, churn_index=1, delta_px=6).font_size_px == 106


def test_compare_preview_backend_summary_reports_latency_and_fps() -> None:
    row = _summarize_backend_samples(
        "native",
        requested_count=4,
        duration_ms=1000,
        samples=[
            PreviewBackendSample(t_ms=0, latency_ms=10.0),
            PreviewBackendSample(t_ms=17, latency_ms=20.0),
        ],
        extra={"cache_hits": 3},
    )

    assert row["backend"] == "native"
    assert row["requested_frames"] == 4
    assert row["ready_frames"] == 2
    assert row["dropped_frames"] == 2
    assert row["ready_fps"] == "2.00"
    assert row["latency_mean_ms"] == "15.0000"
    assert row["latency_p95_ms"] == "20.0000"
    assert row["cache_hits"] == 3


def test_compare_preview_backend_summary_counts_duplicate_ready_events() -> None:
    row = _summarize_backend_samples(
        "native",
        requested_count=2,
        duration_ms=1000,
        samples=[
            PreviewBackendSample(t_ms=0, latency_ms=10.0),
            PreviewBackendSample(t_ms=0, latency_ms=12.0),
            PreviewBackendSample(t_ms=17, latency_ms=20.0),
        ],
    )

    assert row["ready_events"] == 3
    assert row["ready_frames"] == 2
    assert row["duplicate_ready_events"] == 1


def test_compare_preview_backend_summary_reports_missing_duplicate_and_settle_times() -> None:
    row = _summarize_backend_samples(
        "native",
        requested_t_ms=[0, 17, 33],
        duration_ms=1000,
        samples=[
            PreviewBackendSample(t_ms=0, latency_ms=10.0),
            PreviewBackendSample(t_ms=17, latency_ms=20.0, phase="settle"),
            PreviewBackendSample(t_ms=17, latency_ms=25.0, phase="settle"),
        ],
    )

    assert row["ready_frames"] == 2
    assert row["dropped_frames"] == 1
    assert row["leading_missing_frames"] == 0
    assert row["trailing_missing_frames"] == 1
    assert row["steady_dropped_frames"] == 0
    assert row["ready_in_settle_frames"] == 1
    assert row["missing_t_ms"] == "33"
    assert row["duplicate_t_ms"] == "17"
    assert row["settle_ready_t_ms"] == "17"


def test_compare_preview_backend_summary_separates_leading_from_steady_drops() -> None:
    row = _summarize_backend_samples(
        "native",
        requested_t_ms=[0, 17, 34, 51, 68],
        duration_ms=1000,
        samples=[
            PreviewBackendSample(t_ms=51, latency_ms=20.0),
            PreviewBackendSample(t_ms=68, latency_ms=10.0),
        ],
    )

    assert row["dropped_frames"] == 3
    assert row["leading_missing_frames"] == 3
    assert row["trailing_missing_frames"] == 0
    assert row["steady_requested_frames"] == 2
    assert row["steady_ready_frames"] == 2
    assert row["steady_dropped_frames"] == 0


def test_compare_preview_backend_detail_rows_report_first_latency_and_missing() -> None:
    rows = _detail_rows(
        "native",
        requested_t_ms=[0, 17],
        samples=[
            PreviewBackendSample(t_ms=0, latency_ms=10.0),
            PreviewBackendSample(t_ms=0, latency_ms=15.0, phase="settle"),
        ],
    )

    assert rows == [
        {
            "backend": "native",
            "request_index": 0,
            "t_ms": 0,
            "ready_events": 2,
            "duplicate_ready_events": 1,
            "missing": 0,
            "first_latency_ms": "10.0000",
            "first_ready_phase": "request",
        },
        {
            "backend": "native",
            "request_index": 1,
            "t_ms": 17,
            "ready_events": 0,
            "duplicate_ready_events": 0,
            "missing": 1,
            "first_latency_ms": "",
            "first_ready_phase": "",
        },
    ]


def test_compare_preview_image_diff_summary_samples_rgba_pixels() -> None:
    first = QImage(4, 4, QImage.Format.Format_ARGB32_Premultiplied)
    second = QImage(4, 4, QImage.Format.Format_ARGB32_Premultiplied)
    first.fill(QColor("#000000"))
    second.fill(QColor("#000000"))
    second.setPixelColor(0, 0, QColor("#FFFFFF"))

    same = _image_diff_summary(first, first, max_samples=16)
    diff = _image_diff_summary(first, second, max_samples=16)

    assert same["sampled_pixels"] == 16
    assert same["changed_pixels"] == 0
    assert diff["changed_pixels"] == 1
    assert diff["max_channel_delta"] == 255


def test_compare_export_parser_accepts_native_export_options() -> None:
    args = _build_export_parser().parse_args(
        [
            "--lrc",
            "song.lrc",
            "--video",
            "bg.mp4",
            "--native-renderer",
            "renderer.exe",
            "--duration-ms",
            "5000",
            "--sample-frames",
            "3",
            "--disable-strip",
            "--out",
            "summary.csv",
        ]
    )

    assert args.lrc.name == "song.lrc"
    assert args.video.name == "bg.mp4"
    assert args.native_renderer.name == "renderer.exe"
    assert args.duration_ms == 5000
    assert args.sample_frames == 3
    assert args.disable_strip is True
    assert args.out.name == "summary.csv"


def test_compare_export_helpers_report_frames_samples_and_summary() -> None:
    assert _export_frame_count(1001, 60) == 61
    assert _export_sample_times(1000, 3) == [167, 500, 833]

    row = _export_result_row(
        ExportRunResult(
            backend="native",
            output_path=Path("native.mp4"),
            elapsed_ms=500.0,
            total_frames=60,
            progress_events=60,
            file_size=1234,
        )
    )

    assert row["backend"] == "native"
    assert row["frames"] == 60
    assert row["export_fps"] == "120.00"
    assert row["file_size"] == 1234


def test_compare_export_run_sets_backend_env(monkeypatch, tmp_path) -> None:
    calls = []

    def fake_render_subtitle_video(job, *, on_progress=None):
        calls.append(
            {
                "native_export": os.environ.get("KROK_SUBTITLE_NATIVE_EXPORT"),
                "native_renderer": os.environ.get("KROK_SUBTITLE_NATIVE_RENDERER"),
                "strip": os.environ.get("KROK_SUBTITLE_RENDER_STRIP"),
                "output": job.output_path.name,
            }
        )
        if on_progress is not None:
            on_progress(1, 1)
        job.output_path.write_bytes(b"mp4")

    import krok_helper.subtitle_render.engine.renderer as render_module

    monkeypatch.setattr(render_module, "render_subtitle_video", fake_render_subtitle_video)
    background = tmp_path / "bg.mp4"
    background.write_bytes(b"bg")
    native_renderer = tmp_path / "renderer.exe"

    python_result = _run_export_backend(
        "python",
        track=None,
        style=Style(),
        background_video=background,
        output_path=tmp_path / "python.mp4",
        width=2,
        height=2,
        fps=1,
        duration_ms=1000,
        include_audio=False,
        crf=18,
        preset="veryfast",
        encoder_mode="cpu",
        native_renderer=native_renderer,
        strip_enabled=False,
    )
    native_result = _run_export_backend(
        "native",
        track=None,
        style=Style(),
        background_video=background,
        output_path=tmp_path / "native.mp4",
        width=2,
        height=2,
        fps=1,
        duration_ms=1000,
        include_audio=False,
        crf=18,
        preset="veryfast",
        encoder_mode="cpu",
        native_renderer=native_renderer,
        strip_enabled=False,
    )

    assert calls[0]["native_export"] == "0"
    assert calls[0]["strip"] == "0"
    assert calls[1]["native_export"] == "1"
    assert calls[1]["native_renderer"] == str(native_renderer)
    assert calls[1]["strip"] == "0"
    assert python_result.progress_events == 1
    assert native_result.file_size == 3
