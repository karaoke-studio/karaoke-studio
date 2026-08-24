"""Crash-recovery identity and snapshot policy for subtitle projects."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
from pathlib import Path
import time
from typing import Callable, Optional

from krok_helper.subtitle_render.project_store import (
    RecoveryCandidate,
    invalidate_recovery_project,
    load_render_project,
    scan_recovery_projects,
)


@dataclass(frozen=True)
class RecoverySnapshot:
    """One immutable recovery payload and the session identity it captures."""

    payload: dict
    snapshot_id: int
    generation: int
    revision: int


@dataclass(frozen=True)
class RecoveryScan:
    """Recovery entries that still require a user decision."""

    candidates: tuple[RecoveryCandidate, ...]
    invalid_paths: tuple[Path, ...]

    @property
    def requires_attention(self) -> bool:
        return bool(self.candidates or self.invalid_paths)


@dataclass(frozen=True)
class ProjectRecoveryPolicy:
    """Own stable recovery paths, metadata, invalidation, and stale cleanup."""

    root: Path
    snapshot_id_factory: Callable[[], int] = field(
        default=time.time_ns,
        repr=False,
        compare=False,
    )
    timestamp_factory: Callable[[], float] = field(
        default=time.time,
        repr=False,
        compare=False,
    )

    def path_for(self, project_path: Optional[Path]) -> Path:
        """Return the deterministic recovery path for one project identity."""
        root = Path(self.root)
        if project_path is None:
            return root / "untitled.yurika.recovery"
        project_path = Path(project_path)
        identity = str(project_path.resolve()).encode(
            "utf-8",
            errors="surrogatepass",
        )
        suffix = hashlib.sha256(identity).hexdigest()[:12]
        return root / f"{project_path.name}.{suffix}.recovery"

    def snapshot(
        self,
        project_data: dict,
        *,
        project_path: Optional[Path],
        generation: int,
        revision: int,
    ) -> RecoverySnapshot:
        """Build a detached snapshot carrying the current session revision."""
        snapshot_id = int(self.snapshot_id_factory())
        payload = deepcopy(project_data)
        payload["recovery"] = {
            "source_project_path": str(project_path) if project_path else None,
            "created_at_unix": float(self.timestamp_factory()),
            "snapshot_id": snapshot_id,
            "project_generation": int(generation),
            "project_revision": int(revision),
        }
        return RecoverySnapshot(
            payload=payload,
            snapshot_id=snapshot_id,
            generation=int(generation),
            revision=int(revision),
        )

    def scan(self) -> RecoveryScan:
        """Return actionable entries and silently discard obsolete snapshots."""
        candidates, invalid, stale = scan_recovery_projects(self.root)
        for path in stale:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        return RecoveryScan(tuple(candidates), tuple(invalid))

    @staticmethod
    def cleanup_snapshot(path: Path, snapshot_id: int) -> None:
        """Delete ``path`` only when it still contains the named snapshot."""
        try:
            data = load_render_project(path)
            recovery = data.get("recovery")
            current_id = (
                int(recovery.get("snapshot_id") or 0)
                if isinstance(recovery, dict)
                else 0
            )
            if current_id == int(snapshot_id):
                Path(path).unlink(missing_ok=True)
        except (OSError, TypeError, ValueError):
            pass

    @staticmethod
    def invalidate(path: Path, *, delete: bool = True) -> int:
        """Invalidate in-flight writers for one recovery destination."""
        return invalidate_recovery_project(Path(path), delete=delete)
