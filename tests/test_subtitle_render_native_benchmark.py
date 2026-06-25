from __future__ import annotations

from scripts.bench_native_renderer import (
    TimingSample,
    _sample_timestamps,
    _summarize_samples,
)


def test_sample_timestamps_covers_window_endpoints() -> None:
    assert _sample_timestamps(start_ms=1000, frames=4, fps=2) == [1000, 1500, 2000, 2500]


def test_summarize_samples_reports_python_native_and_speedup() -> None:
    rows = _summarize_samples(
        [
            TimingSample(t_ms=0, python_ms=10.0, native_ms=5.0, native_cache_hits=0, native_cache_misses=1),
            TimingSample(t_ms=16, python_ms=20.0, native_ms=5.0, native_cache_hits=1, native_cache_misses=1),
        ],
        scenario="fixture",
        width=640,
        height=360,
    )

    assert rows["scenario"] == "fixture"
    assert rows["frames"] == 2
    assert rows["python_mean_ms"] == "15.0000"
    assert rows["native_mean_ms"] == "5.0000"
    assert rows["speedup"] == "3.00"
    assert rows["native_cache_hits"] == 1
    assert rows["native_cache_misses"] == 1
