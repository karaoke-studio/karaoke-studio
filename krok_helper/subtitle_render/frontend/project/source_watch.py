"""Runtime ownership for external subtitle-source file watching.

The runtime is split into three layers so the GUI thread never touches the disk
for a watched source:

``read_track_source`` / ``read_sug_source``
    Worker-safe: stat, digest, parse and the "file is still being written" check.
``accept_readout``
    GUI thread: turn a :class:`SourceReadout` into a disposition plus merge plan
    and update the runtime's retry / missing bookkeeping.
``prepare_watch_sync`` / ``apply_watch_sync``
    Worker-safe path probing and the Qt-only ``QFileSystemWatcher`` commit.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from enum import Enum
import os
from pathlib import Path
from typing import Callable, Iterable, Mapping, Optional

from PyQt6.QtCore import QFileSystemWatcher, QObject, QTimer, pyqtSignal as Signal

from krok_helper.subtitle_render.domain.timing import TimingTrack
from krok_helper.subtitle_render.sources.reload import (
    PreparedTrackReload,
    plan_reloaded_tracks,
    source_file_digest,
)


def subtitle_source_key(path: Path) -> str:
    """Return the platform-normalized identity for a subtitle source.

    Uses only string normalization (``abspath`` + ``normcase``): the key is
    computed on the GUI thread for every watcher event, so it must never hit the
    disk (``Path.resolve`` would stat symlinks/reparse points and can block for
    minutes on a broken network share).
    """
    resolved = os.path.abspath(os.fspath(path))
    return os.path.normcase(resolved)


def normalized_source_path(path: Path) -> Path:
    """Return the identity path used for watch state (no disk access)."""
    return Path(os.path.abspath(os.fspath(path)))


def subtitle_source_digest(path: Path) -> str:
    """Return the content digest used to suppress duplicate file events."""
    return source_file_digest(path)


@dataclass
class WatchedSubtitleState:
    """Last imported state for one externally watched subtitle source."""

    path: Path
    baseline: TimingTrack
    seen_digest: str
    missing_notified: bool = False
    #: Digest read is still queued for the worker; a change event arriving first
    #: simply parses once (the digest then settles in ``accept_readout``).
    digest_pending: bool = False


class SourceReloadDisposition(str, Enum):
    """Runtime outcome before any project or presentation mutation."""

    MISSING = "missing"
    RETRYING = "retrying"
    FAILED = "failed"
    DUPLICATE = "duplicate"
    UNCHANGED = "unchanged"
    READY = "ready"


@dataclass(frozen=True)
class SourceReloadPreparation:
    """Explicit source-watch result consumed by the window coordinator."""

    key: str
    path: Path
    disposition: SourceReloadDisposition
    prepared: Optional[PreparedTrackReload] = None
    error: Optional[Exception] = None
    notify: bool = False


@dataclass(frozen=True)
class SourceReadout:
    """Result of one worker-side read of a watched source file."""

    path: Path
    digest: str = ""
    candidate: Optional[TimingTrack] = None
    payload: object = None
    #: Extra worker-parsed payload for the same file (``.sug`` whole-file track).
    extra: object = None
    error: Optional[BaseException] = None
    missing: bool = False


@dataclass(frozen=True)
class WatchSyncPlan:
    """Worker-side watch bookkeeping ready to be committed on the GUI thread."""

    files: tuple[str, ...] = ()
    directories: tuple[str, ...] = ()
    digests: Mapping[str, str] = field(default_factory=dict)


def _read_payload(
    path: Path,
    seen_digest: str,
    load_candidate: Callable[[Path], object],
    *,
    candidate_of: Callable[[object], Optional[TimingTrack]],
    force_parse: bool = False,
    load_extra: Optional[Callable[[Path], object]] = None,
) -> SourceReadout:
    """Worker-safe read: existence, digest, stable parse.

    Returns ``candidate=None`` when the content is byte-identical to
    ``seen_digest`` (the caller then skips parsing entirely).  ``force_parse``
    must be set when a change event is already queued: the digest may have been
    read *after* that change, so equality can no longer prove "unchanged".
    """

    source_path = Path(path)
    try:
        if not source_path.is_file():
            return SourceReadout(path=source_path, missing=True)
    except OSError:
        return SourceReadout(path=source_path, missing=True)
    try:
        before = source_path.stat()
        digest = source_file_digest(source_path)
        if digest == seen_digest and not force_parse:
            return SourceReadout(path=source_path, digest=digest)
        payload = load_candidate(source_path)
        extra = load_extra(source_path) if load_extra is not None else None
        after = source_path.stat()
        if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
            raise OSError("字幕文件仍在写入")
    except BaseException as exc:  # noqa: BLE001 — retried by the caller
        return SourceReadout(path=source_path, error=exc)
    return SourceReadout(
        path=source_path,
        digest=digest,
        candidate=candidate_of(payload),
        payload=payload,
        extra=extra,
    )


def read_track_source(
    path: Path,
    seen_digest: str,
    load_candidate: Callable[[Path], TimingTrack],
    *,
    force_parse: bool = False,
) -> SourceReadout:
    """Worker-safe read of one whole-file subtitle source (``.lrc`` / ``.sug``)."""
    return _read_payload(
        path,
        seen_digest,
        load_candidate,
        candidate_of=lambda payload: payload if isinstance(payload, TimingTrack) else None,
        force_parse=force_parse,
    )


def read_sug_source(
    path: Path,
    seen_digest: str,
    load_axes: Callable[[Path], list],
    *,
    load_whole: Optional[Callable[[Path], TimingTrack]] = None,
    force_parse: bool = False,
) -> SourceReadout:
    """Worker-safe read of a ``.sug`` including all axis groups.

    ``load_whole`` optionally parses the same file as one whole track, so the
    apply step never has to re-read/re-parse ``.sug`` on the GUI thread.
    """

    def candidate_of(payload: object) -> Optional[TimingTrack]:
        axes = payload if isinstance(payload, list) else []
        return axes[0].track if axes else None

    return _read_payload(
        path,
        seen_digest,
        load_axes,
        candidate_of=candidate_of,
        force_parse=force_parse,
        load_extra=load_whole,
    )


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
        self._closed = False
        self._states: dict[str, WatchedSubtitleState] = {}
        self._pending_keys: set[str] = set()
        self._retries: dict[str, int] = {}

        self._watcher = QFileSystemWatcher(self)
        self._watcher.fileChanged.connect(self._on_watcher_file_changed)
        self._watcher.directoryChanged.connect(self._on_watcher_directory_changed)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(int(debounce_ms))
        self._timer.timeout.connect(self.pendingReady.emit)

    def _on_watcher_file_changed(self, path: str) -> None:
        if not self._closed:
            self.fileChanged.emit(path)

    def _on_watcher_directory_changed(self, path: str) -> None:
        if not self._closed:
            self.directoryChanged.emit(path)

    def shutdown(self) -> None:
        """Detach the Qt watcher before the owner (or the runtime) goes away.

        A queued ``fileChanged`` delivered after the C++ object was deleted would
        otherwise crash PyQt during teardown; window close calls this explicitly.
        """
        if self._closed:
            return
        self._closed = True
        self._timer.stop()
        try:
            self._watcher.blockSignals(True)
        except RuntimeError:
            return
        try:
            self._watcher.fileChanged.disconnect(self._on_watcher_file_changed)
            self._watcher.directoryChanged.disconnect(self._on_watcher_directory_changed)
        except TypeError:
            pass
        self.clear_watches()
        self._states.clear()
        self._pending_keys.clear()
        self._retries.clear()

    def clear_watches(self) -> None:
        """Remove every path from the Qt watcher (keeps the state bookkeeping)."""
        try:
            files = self._watcher.files()
            directories = self._watcher.directories()
        except RuntimeError:
            return
        if files:
            self._watcher.removePaths(files)
        if directories:
            self._watcher.removePaths(directories)

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

    # -- GUI-thread state ownership ------------------------------------
    def register_baseline(self, path: Path, track: TimingTrack) -> str:
        """Register a freshly loaded source as the merge baseline (GUI thread).

        The content digest is filled by the next worker-side
        :meth:`prepare_watch_sync`; until then a change event simply parses once.
        """
        source_path = normalized_source_path(path)
        key = subtitle_source_key(source_path)
        self._states[key] = WatchedSubtitleState(
            path=source_path,
            baseline=deepcopy(track),
            seen_digest="",
            digest_pending=True,
        )
        return key

    def retain_states(self, keys: Iterable[str]) -> None:
        """Drop state for sources that are no longer referenced (GUI thread)."""
        referenced = set(keys)
        for key in list(self._states):
            if key not in referenced:
                self._states.pop(key, None)
                self._pending_keys.discard(key)
                self._retries.pop(key, None)

    def watch_entries(self) -> tuple[tuple[str, Path, bool], ...]:
        """Snapshot used to prepare the next watch reconciliation (GUI thread)."""
        return tuple(
            (key, state.path, state.digest_pending)
            for key, state in self._states.items()
        )

    @staticmethod
    def prepare_watch_sync(
        entries: Iterable[tuple[str, Path, bool]],
    ) -> WatchSyncPlan:
        """Worker-safe: probe watched paths and read pending digests."""
        files: list[str] = []
        directories: set[str] = set()
        digests: dict[str, str] = {}
        for key, path, pending_digest in entries:
            try:
                if path.is_file():
                    files.append(str(path))
            except OSError:
                pass
            try:
                if path.parent.is_dir():
                    directories.add(str(path.parent))
            except OSError:
                pass
            if pending_digest:
                try:
                    digests[key] = subtitle_source_digest(path)
                except OSError:
                    continue
        return WatchSyncPlan(
            files=tuple(sorted(files)),
            directories=tuple(sorted(directories)),
            digests=digests,
        )

    def apply_watch_sync(self, plan: WatchSyncPlan) -> None:
        """GUI thread: commit digests and re-arm the Qt watches.

        Digests for keys with a *queued* change event are deliberately not
        committed: the worker may have read the digest after that change, and a
        committed digest would make the pending reload look like a duplicate.
        Those keys keep ``digest_pending=True`` so the reload parses, then
        settles the digest via :meth:`accept_readout`.
        """
        for key, digest in plan.digests.items():
            state = self._states.get(key)
            if state is None:
                continue
            if key in self._pending_keys:
                state.digest_pending = True
                continue
            state.seen_digest = digest
            state.digest_pending = False
        self.clear_watches()
        if plan.files:
            self._watcher.addPaths(list(plan.files))
        if plan.directories:
            self._watcher.addPaths(list(plan.directories))

    # -- queue / retry --------------------------------------------------
    def queue(self, key: str) -> None:
        self._pending_keys.add(key)
        state = self._states.get(key)
        if state is not None:
            # 已排队 = 文件确实变了：摘要短路必须失效，否则「摘要计算期间发生的
            # 改动」会被当成内容相同（DUPLICATE）而静默丢弃。
            state.digest_pending = True
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

    # -- reload preparation --------------------------------------------
    def accept_readout(
        self,
        key: str,
        readout: SourceReadout,
        *,
        primary_track: Optional[TimingTrack] = None,
        extra_tracks: Iterable[tuple[int, TimingTrack]] = (),
        style: object = None,
    ) -> Optional[SourceReloadPreparation]:
        """GUI thread: decide the disposition and build the merge plan."""
        state = self.state(key)
        if state is None:
            return None
        path = readout.path
        if readout.missing:
            notify = not state.missing_notified
            state.missing_notified = True
            return SourceReloadPreparation(
                key=key,
                path=path,
                disposition=SourceReloadDisposition.MISSING,
                notify=notify,
            )
        if readout.error is not None:
            retrying = self.retry(key)
            return SourceReloadPreparation(
                key=key,
                path=path,
                disposition=(
                    SourceReloadDisposition.RETRYING
                    if retrying
                    else SourceReloadDisposition.FAILED
                ),
                error=readout.error,  # type: ignore[arg-type]
            )
        state.missing_notified = False
        self.acknowledge(key)
        candidate = readout.candidate
        if candidate is None:
            state.seen_digest = readout.digest
            state.digest_pending = False
            return SourceReloadPreparation(
                key=key,
                path=path,
                disposition=SourceReloadDisposition.DUPLICATE,
                prepared=PreparedTrackReload(
                    digest=readout.digest, candidate=None, plan=None
                ),
            )
        if state.baseline == candidate:
            state.seen_digest = readout.digest
            state.digest_pending = False
            return SourceReloadPreparation(
                key=key,
                path=path,
                disposition=SourceReloadDisposition.UNCHANGED,
                prepared=PreparedTrackReload(
                    digest=readout.digest, candidate=candidate, plan=None
                ),
            )
        plan = plan_reloaded_tracks(
            state.baseline,
            candidate,
            primary_track=primary_track,
            extra_tracks=extra_tracks,
            style=style,  # type: ignore[arg-type]
        )
        prepared = PreparedTrackReload(
            digest=readout.digest, candidate=candidate, plan=plan
        )
        return SourceReloadPreparation(
            key=key,
            path=path,
            disposition=SourceReloadDisposition.READY,
            prepared=prepared,
        )

    def ignore_prepared(self, preparation: SourceReloadPreparation) -> None:
        """Remember a user-rejected stable revision without replacing tracks."""

        state = self.state(preparation.key)
        prepared = preparation.prepared
        if state is not None and prepared is not None:
            state.seen_digest = prepared.digest
            state.digest_pending = False

    def accept_prepared(self, preparation: SourceReloadPreparation) -> None:
        """Adopt the successfully applied source snapshot as the new baseline."""

        state = self.state(preparation.key)
        prepared = preparation.prepared
        if state is None or prepared is None or prepared.candidate is None:
            return
        state.baseline = deepcopy(prepared.candidate)
        state.seen_digest = prepared.digest
        state.digest_pending = False

    def start_pending(self, delay_ms: int = 0) -> None:
        if self._pending_keys:
            self._timer.start(int(delay_ms))


__all__ = [
    "SourceReadout",
    "SourceReloadDisposition",
    "SourceReloadPreparation",
    "SubtitleSourceWatchRuntime",
    "WatchSyncPlan",
    "WatchedSubtitleState",
    "normalized_source_path",
    "read_sug_source",
    "read_track_source",
    "subtitle_source_digest",
    "subtitle_source_key",
]
