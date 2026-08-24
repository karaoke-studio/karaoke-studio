"""Pure recent-project path policy for the subtitle-render application."""

from __future__ import annotations

import os
from pathlib import Path


class RecentProjectPolicy:
    """Normalize, prune, and order native project paths without UI or storage."""

    def __init__(self, *, project_suffix: str, limit: int) -> None:
        self._project_suffix = project_suffix.lower()
        self._limit = max(int(limit), 0)

    @staticmethod
    def path_key(path: Path | str) -> str:
        """Return the platform-normalized key used to deduplicate paths."""
        return os.path.normcase(os.path.abspath(os.fspath(path)))

    def normalize(self, stored: object) -> list[str]:
        """Return existing native projects in stable, deduplicated order."""
        if self._limit == 0:
            return []
        raw_paths = stored if isinstance(stored, list) else []
        paths: list[str] = []
        seen: set[str] = set()
        for value in raw_paths:
            if not isinstance(value, str) or not value.strip():
                continue
            path = Path(value).expanduser().absolute()
            key = self.path_key(path)
            if (
                key in seen
                or path.suffix.lower() != self._project_suffix
                or not path.is_file()
            ):
                continue
            seen.add(key)
            paths.append(str(path))
            if len(paths) >= self._limit:
                break
        return paths

    def record(self, existing: list[str], path: Path | str) -> list[str]:
        """Move one successfully opened project to the front."""
        resolved = str(Path(path).expanduser().absolute())
        key = self.path_key(resolved)
        paths = [
            value for value in existing if self.path_key(value) != key
        ]
        paths.insert(0, resolved)
        return paths[: self._limit]
