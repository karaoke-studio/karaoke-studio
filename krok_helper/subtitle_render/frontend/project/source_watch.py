"""Runtime ownership for external subtitle-source file watching."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
from pathlib import Path
import sys
from typing import Callable, Mapping, Optional

from PyQt6.QtCore import QFileSystemWatcher, QObject, QTimer, pyqtSignal as Signal

from krok_helper.subtitle_render.domain.timing import TimingTrack


def subtitle_source_key(path: Path) -> str:
    """Return the platform-normalized identity for a subtitle source."""
    resolved = str(Path(path).resolve(strict=False))
    return resolved.casefold() if sys.platform == "win32" else resolved


def subtitle_source_digest(path: Path) -> str:
    """Return the content digest used to suppress duplicate file events."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@dataclass
class WatchedSubtitleState:
    """Last imported state for one externally watched subtitle source."""

    path: Path
    baseline: TimingTrack
    seen_digest: str
    missing_notified: bool = False


class SubtitleSourceWatchRuntime(QObject):
    """Own QFileSystemWatcher, debounce, retry, and baseline bookkeeping."""

    fileChanged = Signal(str)
    directoryChanged = Signal(str)
    pendingReady = Signal()

    def __init__(
        self,
        parent: Optional[QObject] = None,
        *,
        reload_suspended: Optional[Callable[[], bool]] = None,
        debounce_ms: int = 450,
    ) -> None:
        super().__init__(parent)
        self._reload_suspended = reload_suspended or (lambda: False)
        self._states: dict[str, WatchedSubtitleState] = {}
        self._pending_keys: set[str] = set()
        self._retries: dict[str, int] = {}

        self._watcher = QFileSystemWatcher(self)
        self._watcher.fileChanged.connect(self.fileChanged.emit)
        self._watcher.directoryChanged.connect(self.directoryChanged.emit)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(int(debounce_ms))
        self._timer.timeout.connect(self.pendingReady.emit)

    @property
    def watcher(self) -> QFileSystemWatcher:
        return self._watcher

    @property
    def timer(self) -> QTimer:
        return self._timer

    @property
    def states(self) -> dict[str, WatchedSubtitleState]:
        return self._states

    @property
    def pending_keys(self) -> set[str]:
        return self._pending_keys

    @property
    def retries(self) -> dict[str, int]:
        return self._retries

    def state(self, key: str) -> Optional[WatchedSubtitleState]:
        return self._states.get(key)

    def set_baseline(self, path: Path, track: TimingTrack) -> None:
        source_path = Path(path).resolve(strict=False)
        try:
            digest = subtitle_source_digest(source_path)
        except OSError:
            digest = ""
        self._states[subtitle_source_key(source_path)] = WatchedSubtitleState(
            path=source_path,
            baseline=deepcopy(track),
            seen_digest=digest,
        )

    def sync(self, referenced: Mapping[str, tuple[Path, TimingTrack]]) -> None:
        """Reconcile runtime state and Qt watches with current project sources."""
        for key in list(self._states):
            if key not in referenced:
                self._states.pop(key, None)
                self._pending_keys.discard(key)
                self._retries.pop(key, None)
        for key, (path, track) in referenced.items():
            if key not in self._states:
                self.set_baseline(path, track)

        watched_files = self._watcher.files()
        watched_directories = self._watcher.directories()
        if watched_files:
            self._watcher.removePaths(watched_files)
        if watched_directories:
            self._watcher.removePaths(watched_directories)

        files = sorted(
            str(state.path)
            for state in self._states.values()
            if state.path.is_file()
        )
        directories = sorted(
            {
                str(state.path.parent)
                for state in self._states.values()
                if state.path.parent.is_dir()
            }
        )
        if files:
            self._watcher.addPaths(files)
        if directories:
            self._watcher.addPaths(directories)

    def queue(self, key: str) -> None:
        self._pending_keys.add(key)
        if not self._reload_suspended():
            self._timer.start()

    def take_pending(self) -> tuple[str, ...]:
        pending = tuple(self._pending_keys)
        self._pending_keys.clear()
        return pending

    def retry(
        self,
        key: str,
        *,
        max_attempts: int = 5,
        delay_ms: int = 400,
    ) -> bool:
        """Schedule another read and report whether the retry was accepted."""
        attempt = self._retries.get(key, 0) + 1
        if attempt <= max_attempts:
            self._retries[key] = attempt
            self._pending_keys.add(key)
            self._timer.start(int(delay_ms))
            return True
        self._retries.pop(key, None)
        return False

    def acknowledge(self, key: str) -> None:
        self._retries.pop(key, None)

    def start_pending(self, delay_ms: int = 0) -> None:
        if self._pending_keys:
            self._timer.start(int(delay_ms))


__all__ = [
    "SubtitleSourceWatchRuntime",
    "WatchedSubtitleState",
    "subtitle_source_digest",
    "subtitle_source_key",
]
