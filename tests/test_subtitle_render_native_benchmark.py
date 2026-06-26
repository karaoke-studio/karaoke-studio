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
from scripts.probe_native_preview_stats import (
    _format_stats_line,
    _playback_times,
)


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
        },
        previous={
            "cache_hits": 2,
            "cache_misses": 6,
            "future_frames_cached": 1,
            "stale_frames_dropped": 2,
            "generations_cancelled": 0,
        },
    )

    assert line == (
        "elapsed=1.20s t=3400ms "
        "hit=5(+3) miss=8(+2) future=3(+2) stale=2(+0) cancel=1(+1)"
    )
