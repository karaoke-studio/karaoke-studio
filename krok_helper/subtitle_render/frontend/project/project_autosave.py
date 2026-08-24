"""Qt thread lifecycle for subtitle project recovery snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QObject, QThread, QTimer, pyqtSignal as Signal

from krok_helper.subtitle_render.frontend.background_tasks import (
    _RecoverySaveWorker,
)


@dataclass(frozen=True)
class RecoverySaveRequest:
    """Immutable input required by one background recovery write."""

    path: Path
    payload: dict
    generation: int
    revision: int
    snapshot_id: int


class ProjectAutoSaveRuntime(QObject):
    """Own one recovery worker thread and coalesce requests while it is busy."""

    saved = Signal(Path, int, int, int, bool)
    failed = Signal(Path, int, int, int, str)
    rerunRequested = Signal()
    saveRequested = Signal()

    def __init__(
        self,
        parent: Optional[QObject] = None,
        *,
        debounce_ms: int = 2_000,
    ) -> None:
        super().__init__(parent)
        self._thread: Optional[QThread] = None
        self._worker: Optional[_RecoverySaveWorker] = None
        self._pending = False
        self._debounce_timer = QTimer(self)
        self._debounce_timer.setSingleShot(True)
        self._debounce_timer.setInterval(max(0, int(debounce_ms)))
        self._debounce_timer.timeout.connect(self.saveRequested.emit)
        self._periodic_timer = QTimer(self)
        self._periodic_timer.timeout.connect(self.saveRequested.emit)

    @property
    def thread(self) -> Optional[QThread]:
        return self._thread

    @property
    def worker(self) -> Optional[_RecoverySaveWorker]:
        return self._worker

    @property
    def pending(self) -> bool:
        return self._pending

    @pending.setter
    def pending(self, value: bool) -> None:
        self._pending = bool(value)

    @property
    def debounce_timer(self) -> QTimer:
        """Expose the timer for compatibility diagnostics and explicit flushes."""
        return self._debounce_timer

    @property
    def periodic_timer(self) -> QTimer:
        """Expose the periodic timer for compatibility diagnostics."""
        return self._periodic_timer

    def configure(self, *, enabled: bool, interval_ms: int) -> None:
        """Apply periodic scheduling without changing the save request contract."""
        self._periodic_timer.setInterval(max(0, int(interval_ms)))
        if enabled:
            self._periodic_timer.start()
        else:
            self._periodic_timer.stop()
            self._debounce_timer.stop()

    def schedule(self, *, enabled: bool) -> None:
        """Restart the single-shot debounce timer when saving is eligible."""
        if enabled:
            self._debounce_timer.start()

    def stop_scheduling(self) -> None:
        """Stop both timers and discard a coalesced follow-up request."""
        self._debounce_timer.stop()
        self._periodic_timer.stop()
        self._pending = False

    def start(self, request: RecoverySaveRequest) -> bool:
        """Start one request, or remember that the busy runtime needs a rerun."""
        if self._thread is not None:
            self._pending = True
            return False
        worker = _RecoverySaveWorker(
            request.path,
            request.payload,
            request.generation,
            request.revision,
            request.snapshot_id,
        )
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.saved.connect(self.saved)
        worker.failed.connect(self.failed)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(self._finish)
        self._worker = worker
        self._thread = thread
        thread.start()
        return True

    def wait(self, timeout_ms: int) -> bool:
        """Wait for the active worker, returning False on timeout."""
        thread = self._thread
        if thread is None or not thread.isRunning():
            return True
        return bool(thread.wait(max(0, int(timeout_ms))))

    def cancel_pending(self) -> None:
        """Discard only the coalesced follow-up request."""
        self._pending = False

    def _finish(self) -> None:
        self._thread = None
        self._worker = None
        if not self._pending:
            return
        self._pending = False
        self.rerunRequested.emit()
