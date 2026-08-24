"""Subtitle-source loading contract independent from the Qt frontend."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from krok_helper.subtitle_render.subtitle_sources import load_nicokara_lrc
from krok_helper.subtitle_render.sug_project import (
    load_sug_timing_track,
    timing_track_from_sug_project,
)
from krok_helper.subtitle_render.timing import TimingTrack


class SubtitleSourceLoader:
    """Route disk and in-memory subtitle sources through one explicit boundary."""

    @staticmethod
    def load_file(
        path: Path,
        *,
        software_compensation_ms: int = 0,
    ) -> TimingTrack:
        """Load ``.sug`` with compensation and all other paths as Nicokara LRC."""
        path = Path(path)
        if path.suffix.lower() == ".sug":
            return load_sug_timing_track(
                path,
                software_compensation_ms=int(software_compensation_ms),
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
    ) -> TimingTrack:
        return load_sug_timing_track(
            Path(path),
            software_compensation_ms=int(software_compensation_ms),
        )

    @staticmethod
    def load_sug_project(
        project: object,
        *,
        nicokara_tags: Optional[dict] = None,
        software_compensation_ms: int = 0,
    ) -> TimingTrack:
        return timing_track_from_sug_project(
            project,
            nicokara_tags=nicokara_tags,
            software_compensation_ms=int(software_compensation_ms),
        )
