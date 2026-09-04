"""Subtitle-source loading contract independent from the Qt frontend."""

from __future__ import annotations

from collections.abc import Collection
from pathlib import Path
from typing import Optional

from krok_helper.subtitle_render.sources.subtitles import load_nicokara_lrc
from krok_helper.subtitle_render.sources.sug import (
    SugAxisTrack,
    load_sug_axis_tracks,
    load_sug_timing_track,
    timing_track_from_sug_project,
)
from krok_helper.subtitle_render.domain.timing import TimingTrack


class SubtitleSourceLoader:
    """Route disk and in-memory subtitle sources through one explicit boundary."""

    @staticmethod
    def load_file(
        path: Path,
        *,
        software_compensation_ms: int = 0,
        singer_filter: Collection[str] | None = None,
    ) -> TimingTrack:
        """Load ``.sug`` with compensation and all other paths as Nicokara LRC."""
        path = Path(path)
        if path.suffix.lower() == ".sug":
            return load_sug_timing_track(
                path,
                software_compensation_ms=int(software_compensation_ms),
                singer_filter=singer_filter,
            )
        return load_nicokara_lrc(path)

    @staticmethod
    def load_lrc(path: Path) -> TimingTrack:
        return load_nicokara_lrc(Path(path))

    @staticmethod
    def load_sug(
        path: Path,
        *,
        software_compensation_ms: int = 0,
        singer_filter: Collection[str] | None = None,
    ) -> TimingTrack:
        return load_sug_timing_track(
            Path(path),
            software_compensation_ms=int(software_compensation_ms),
            singer_filter=singer_filter,
        )

    @staticmethod
    def load_sug_axes(
        path: Path,
        *,
        software_compensation_ms: int = 0,
    ) -> list[SugAxisTrack]:
        """Parse a ``.sug`` into one track per axis group (single axis if none)."""
        return load_sug_axis_tracks(
            Path(path),
            software_compensation_ms=int(software_compensation_ms),
        )

    @staticmethod
    def load_sug_project(
        project: object,
        *,
        nicokara_tags: Optional[dict] = None,
        software_compensation_ms: int = 0,
        base_dir: Optional[Path] = None,
    ) -> TimingTrack:
        return timing_track_from_sug_project(
            project,
            nicokara_tags=nicokara_tags,
            software_compensation_ms=int(software_compensation_ms),
            base_dir=base_dir,
        )
