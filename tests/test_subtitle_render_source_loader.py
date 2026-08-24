"""Focused routing contracts for subtitle source loading."""

from __future__ import annotations

from pathlib import Path

from krok_helper.subtitle_render.sources import loader as loader_module
from krok_helper.subtitle_render.sources.loader import SubtitleSourceLoader
from krok_helper.subtitle_render.timing import TimingTrack


def test_subtitle_source_loader_routes_sug_with_compensation(
    monkeypatch,
    tmp_path: Path,
) -> None:
    expected = TimingTrack()
    calls: list[tuple] = []
    monkeypatch.setattr(
        loader_module,
        "load_sug_timing_track",
        lambda *args, **kwargs: (calls.append((args, kwargs)), expected)[1],
    )

    result = SubtitleSourceLoader.load_file(
        tmp_path / "song.SUG",
        software_compensation_ms=125,
    )

    assert result is expected
    assert calls == [
        ((tmp_path / "song.SUG",), {"software_compensation_ms": 125})
    ]


def test_subtitle_source_loader_routes_non_sug_as_lrc(
    monkeypatch,
    tmp_path: Path,
) -> None:
    expected = TimingTrack()
    calls: list[Path] = []
    monkeypatch.setattr(
        loader_module,
        "load_nicokara_lrc",
        lambda path: (calls.append(path), expected)[1],
    )

    result = SubtitleSourceLoader.load_file(tmp_path / "song.txt")

    assert result is expected
    assert calls == [tmp_path / "song.txt"]


def test_subtitle_source_loader_preserves_in_memory_sug_arguments(monkeypatch) -> None:
    expected = TimingTrack()
    project = object()
    tags = {"title": "song"}
    calls: list[tuple] = []
    monkeypatch.setattr(
        loader_module,
        "timing_track_from_sug_project",
        lambda *args, **kwargs: (calls.append((args, kwargs)), expected)[1],
    )

    result = SubtitleSourceLoader.load_sug_project(
        project,
        nicokara_tags=tags,
        software_compensation_ms=-30,
    )

    assert result is expected
    assert calls == [
        (
            (project,),
            {"nicokara_tags": tags, "software_compensation_ms": -30},
        )
    ]
