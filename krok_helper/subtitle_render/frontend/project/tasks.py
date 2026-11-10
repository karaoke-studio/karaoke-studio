"""Off-thread execution and cancellation bookkeeping for subtitle-render work.

Every heavy operation in the subtitle-render window (project reads, subtitle
parsing, ``ffprobe`` probes, source-watch digests, image decoding) runs here
instead of on the GUI thread.  Two consumption styles are supported:

``ProjectTaskRunner.submit``
    Fire-and-forget with a callback delivered on the GUI thread.  Preferred for
    work whose result is consumed later (watch synchronisation, deferred asset
    probing, background image decoding).

``ProjectTaskRunner.run_blocking``
    Run the work off-thread but resume the *caller* only once the result is
    available.  Used by the staged project assembly, which is an inherently
    sequential Qt-only pipeline: it keeps the GUI painting through a nested
    ``QEventLoop``.

User-visible progress stays inside the window's usual surfaces (the project
load ``StateToolTip`` and ``InfoBar`` notifications) — this module deliberately
never creates a progress window.  :class:`ProjectIoGuard` only tracks nested
operations and their cancellation scopes so window teardown can release every
waiter.
"""

from __future__ import annotations

import logging
import queue
import threading
from typing import Any, Callable, Optional

from PyQt6.QtCore import QEventLoop, QObject, QThread, QTimer, Qt, pyqtSignal as Signal
from PyQt6.QtWidgets import QApplication

log = logging.getLogger(__name__)


class ProjectTaskCanceled(BaseException):
    """Raised when the user cancels the operation a task belongs to.

    Derives from :class:`BaseException` on purpose: ``except Exception`` blocks
    around file parsing must not mistake a user cancel for a parse failure.
    """


class ProjectTaskTimeout(ProjectTaskCanceled):
    """Raised when a blocking task exceeds its configured deadline.

    A subclass of :class:`ProjectTaskCanceled` so every existing cancel path also
    handles a hung disk/subprocess safely.
    """


#: Default upper bound for one blocking read.  Generous enough for very large
#: ``.sug`` projects on slow disks, but bounded so a hung network share can never
#: freeze the event loop forever.
DEFAULT_BLOCKING_TIMEOUT_S = 300.0


class ProjectTaskScope:
    """Cancellation scope shared by every task of one user action."""

    __slots__ = ("_canceled",)

    def __init__(self) -> None:
        self._canceled = threading.Event()

    @property
    def canceled(self) -> bool:
        return self._canceled.is_set()

    def cancel(self) -> None:
        self._canceled.set()


class _TaskFailure:
    """Wrapper so a raised ``None``-like payload can never be mistaken for a result."""

    __slots__ = ("error",)

    def __init__(self, error: BaseException) -> None:
        self.error = error


class _TaskBridge(QObject):
    """Queued delivery point: emitted from worker threads, delivered on the GUI thread."""

    finished = Signal(int, object)


class _DaemonTaskPool:
    """Small daemon-thread pool; a hung task can never block interpreter exit.

    Idle workers exit after ``IDLE_EXIT_S`` so a long-lived app does not keep
    threads around.  The exit path re-checks the queue under the lock: a task
    enqueued right at the idle boundary must never be stranded without a worker.
    """

    IDLE_EXIT_S = 30.0

    def __init__(self, max_workers: int = 3) -> None:
        self._max_workers = max(1, int(max_workers))
        self._queue: "queue.Queue[Optional[Callable[[], None]]]" = queue.Queue()
        self._workers = 0
        self._idle = 0
        self._lock = threading.Lock()
        self._closed = False

    def submit(self, task: Callable[[], None]) -> bool:
        with self._lock:
            if self._closed:
                return False
            self._queue.put(task)
            # ``_workers == 0`` means no thread can be about to pick the task up,
            # so always spawn one there (an idle worker may be exiting right now).
            if (self._idle <= 0 or self._workers == 0) and self._workers < self._max_workers:
                self._spawn_worker_locked()
            return True

    def _spawn_worker_locked(self) -> None:
        self._workers += 1
        threading.Thread(
            target=self._worker_loop,
            name="project-io",
            daemon=True,
        ).start()

    def _worker_loop(self) -> None:
        while True:
            with self._lock:
                self._idle += 1
            try:
                task = self._queue.get(timeout=self.IDLE_EXIT_S)
            except queue.Empty:
                with self._lock:
                    self._idle -= 1
                    # Race guard: a task may have been enqueued while this worker
                    # was timing out.  Never walk away from queued work.
                    if not self._closed and not self._queue.empty():
                        continue
                    self._workers -= 1
                return
            with self._lock:
                self._idle -= 1
            if task is None:
                with self._lock:
                    self._workers -= 1
                return
            try:
                task()
            except BaseException:  # noqa: BLE001 — never kill a pool worker
                log.exception("后台任务线程异常退出")

    def shutdown(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            workers = self._workers
        for _ in range(max(workers, 1)):
            self._queue.put(None)


class ProjectIoGuard(QObject):
    """Tracks nested project operations and owns their cancellation scopes."""

    def __init__(
        self,
        parent: Optional[QObject] = None,
        *,
        parent_window: Optional[Any] = None,
    ) -> None:
        super().__init__(parent)
        # Kept for call-site compatibility; no window is created by the guard.
        self.parent_window = parent_window
        self._depth = 0
        self._labels: list[str] = []
        self._scopes: list[tuple[ProjectTaskScope, Optional[QEventLoop]]] = []
        self._levels: list[Optional[tuple[ProjectTaskScope, Optional[QEventLoop]]]] = []
        # Event loops owned by ``_run_project_steps`` (no blocking task wait):
        # they must also be released by ``close_now`` so window teardown can
        # never leave the GUI stuck in a nested loop.
        self._stage_loops: list[QEventLoop] = []
        self._closed = False

    # -- lifecycle -----------------------------------------------------
    def begin(
        self,
        label: str = "",
        scope: Optional[ProjectTaskScope] = None,
        loop: Optional[QEventLoop] = None,
    ) -> None:
        """Register one nested operation (bookkeeping only; nothing is shown)."""
        self._depth += 1
        self._labels.append(str(label or ""))
        level = (scope, loop) if scope is not None else None
        self._levels.append(level)
        if level is not None:
            self._scopes.append(level)

    def end(self, loop: Optional[QEventLoop] = None) -> None:
        if self._depth > 0:
            self._depth -= 1
        if self._labels:
            self._labels.pop()
        level = self._levels.pop() if self._levels else None
        if level is not None:
            try:
                self._scopes.remove(level)
            except ValueError:
                pass

    def close_now(self) -> None:
        """Abort every nested operation and release its event loop (window teardown)."""
        if self._closed:
            return
        self._closed = True
        # Cancelling the scopes makes every waiter unwind through its cancel path
        # instead of treating a quit loop as "finished successfully".
        for scope, _loop in list(self._scopes):
            scope.cancel()
        self._depth = 0
        self._labels.clear()
        self._levels.clear()
        self._scopes.clear()
        for loop in self._stage_loops:
            try:
                loop.quit()
            except RuntimeError:
                pass
        self._stage_loops.clear()

    @property
    def closed(self) -> bool:
        return self._closed

    # -- stage loops ----------------------------------------------------
    def register_stage_loop(self, loop: QEventLoop) -> None:
        self._stage_loops.append(loop)

    def unregister_stage_loop(self, loop: QEventLoop) -> None:
        self._stage_loops = [item for item in self._stage_loops if item is not loop]

    # -- cancellation --------------------------------------------------
    def request_cancel(self) -> None:
        entries = list(self._scopes)
        if not entries:
            return
        for scope, loop in entries:
            scope.cancel()
            if loop is not None:
                loop.quit()


class ProjectTaskRunner(QObject):
    """Owns the worker pool and cancellation guard for one window."""

    def __init__(
        self,
        parent: Optional[QObject] = None,
        *,
        guard: Optional[ProjectIoGuard] = None,
        max_workers: int = 3,
    ) -> None:
        super().__init__(parent)
        self._bridge = _TaskBridge(self)
        self._bridge.finished.connect(
            self._deliver, Qt.ConnectionType.QueuedConnection
        )
        self._pool = _DaemonTaskPool(max_workers=max_workers)
        self._callbacks: dict[int, tuple[Optional[Callable], Optional[Callable]]] = {}
        self._timeouts: dict[int, QTimer] = {}
        self._waits: dict[int, tuple[QEventLoop, Optional[ProjectTaskScope]]] = {}
        self._lock = threading.Lock()
        self._next_id = 0
        self._closed = False
        self._guard = (
            guard if guard is not None else ProjectIoGuard(self)
        )

    # -- async ---------------------------------------------------------
    @property
    def guard(self) -> ProjectIoGuard:
        """The shared cancellation guard for this runner."""
        return self._guard

    @property
    def closed(self) -> bool:
        return self._closed

    def submit(
        self,
        work: Callable[[], Any],
        *,
        on_done: Optional[Callable[[Any], None]] = None,
        on_error: Optional[Callable[[BaseException], None]] = None,
        timeout_s: Optional[float] = None,
    ) -> Optional[int]:
        """Run ``work`` off-thread; deliver exactly one callback on the GUI thread.

        ``timeout_s`` guarantees the owner is notified (with
        :class:`ProjectTaskTimeout`) even when the worker never returns, so an
        in-flight flag can never stay set forever.
        """
        if self._closed:
            return None
        with self._lock:
            self._next_id += 1
            task_id = self._next_id
            self._callbacks[task_id] = (on_done, on_error)

        if timeout_s is not None and timeout_s > 0:
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.setInterval(int(timeout_s * 1000))
            timer.timeout.connect(lambda: self._on_task_timeout(task_id))
            timer.start()
            self._timeouts[task_id] = timer

        def run() -> None:
            try:
                result = work()
            except BaseException as exc:  # noqa: BLE001 — forwarded to the owner
                payload: object = _TaskFailure(exc)
            else:
                payload = result
            try:
                self._bridge.finished.emit(task_id, payload)
            except RuntimeError:
                # Owner窗口已销毁（桥对象随之删除）：结果无处可送，直接丢弃。
                return

        if not self._pool.submit(run):
            self._cancel_timeout(task_id)
            self._callbacks.pop(task_id, None)
            return None
        return task_id

    def _cancel_timeout(self, task_id: int) -> None:
        timer = self._timeouts.pop(task_id, None)
        if timer is not None:
            try:
                timer.stop()
                timer.deleteLater()
            except RuntimeError:
                pass

    def _on_task_timeout(self, task_id: int) -> None:
        with self._lock:
            entry = self._callbacks.pop(task_id, None)
        self._cancel_timeout(task_id)
        if entry is None:
            return
        _on_done, on_error = entry
        timeout = ProjectTaskTimeout("后台任务超时")
        if on_error is not None:
            try:
                on_error(timeout)
            except BaseException:  # noqa: BLE001 — never break the event loop
                log.exception("后台任务超时回调抛出异常")
        else:
            log.warning("后台任务超时且无人处理: task=%s", task_id)

    def discard(self, task_id: Optional[int]) -> None:
        """Forget a pending callback; the worker result is dropped when it lands."""
        if task_id is None:
            return
        with self._lock:
            self._callbacks.pop(task_id, None)
        self._cancel_timeout(task_id)

    def _deliver(self, task_id: int, payload: object) -> None:
        with self._lock:
            entry = self._callbacks.pop(task_id, None)
        self._cancel_timeout(task_id)
        if entry is None:
            return
        on_done, on_error = entry
        if isinstance(payload, _TaskFailure):
            if on_error is not None:
                try:
                    on_error(payload.error)
                except BaseException:  # noqa: BLE001 — never break the event loop
                    log.exception("后台任务错误回调抛出异常")
            else:
                log.error(
                    "后台任务失败且无人处理: %s",
                    payload.error,
                    exc_info=payload.error,
                )
            return
        if on_done is not None:
            try:
                on_done(payload)
            except BaseException:  # noqa: BLE001 — never break the event loop
                log.exception("后台任务完成回调抛出异常")

    # -- blocking ------------------------------------------------------
    @staticmethod
    def on_gui_thread() -> bool:
        app = QApplication.instance()
        return app is not None and QThread.currentThread() is app.thread()

    def run_blocking(
        self,
        work: Callable[[], Any],
        *,
        label: str = "",
        scope: Optional[ProjectTaskScope] = None,
        timeout_s: Optional[float] = DEFAULT_BLOCKING_TIMEOUT_S,
    ) -> Any:
        """Run ``work`` off-thread and return its result once published.

        The caller resumes after a nested event loop that keeps paint, timers and
        window updates alive — no progress window is created; the window's usual
        ``StateToolTip``/``InfoBar`` surfaces remain the only user-visible
        progress.  ``scope`` (a user-initiated action) allows a programmatic
        cancel: it raises :class:`ProjectTaskCanceled` and discards the late
        result.  ``timeout_s`` bounds the wait so a hung disk/subprocess can never
        freeze the app; ``None`` disables the bound.
        """
        if not self.on_gui_thread():
            return work()
        if self._closed:
            raise ProjectTaskCanceled()
        if scope is not None and scope.canceled:
            raise ProjectTaskCanceled()

        loop = QEventLoop()
        box: dict[str, Any] = {}

        def done(result: Any) -> None:
            box["result"] = result
            loop.quit()

        def failed(exc: BaseException) -> None:
            box["error"] = exc
            loop.quit()

        task_id = self.submit(work, on_done=done, on_error=failed)
        if task_id is None:
            # Runner shut down between the check above and the submit: never fall
            # back to running heavy work on the GUI thread.
            raise ProjectTaskCanceled()

        watchdog: Optional[QTimer] = None
        if timeout_s is not None and timeout_s > 0:
            watchdog = QTimer(self)
            watchdog.setSingleShot(True)
            watchdog.setInterval(int(timeout_s * 1000))

            def on_watchdog() -> None:
                box["timeout"] = True
                loop.quit()

            watchdog.timeout.connect(on_watchdog)
            watchdog.start()

        self._waits[task_id] = (loop, scope)
        self._guard.begin(label, scope, loop)
        try:
            loop.exec()
        finally:
            self._waits.pop(task_id, None)
            if watchdog is not None:
                watchdog.stop()
                watchdog.deleteLater()
            self._guard.end(loop)
        if self._closed:
            self.discard(task_id)
            raise ProjectTaskCanceled()
        if scope is not None and scope.canceled:
            self.discard(task_id)
            raise ProjectTaskCanceled()
        if box.get("timeout"):
            self.discard(task_id)
            raise ProjectTaskTimeout(
                f"后台任务超时（{timeout_s:.0f}s）" if timeout_s else "后台任务超时"
            )
        error = box.get("error")
        if error is not None:
            raise error
        if "result" not in box:
            # The loop was exited by something other than the worker (window
            # teardown); never resume with a half-unknown value.
            self.discard(task_id)
            raise ProjectTaskCanceled()
        return box["result"]

    # -- lifecycle -----------------------------------------------------
    def shutdown(self) -> None:
        """Release every waiter, then stop accepting work (window teardown)."""
        if self._closed:
            return
        self._closed = True
        with self._lock:
            waits = list(self._waits.values())
            self._waits.clear()
            task_ids = list(self._timeouts)
            self._callbacks.clear()
        # Wake the waiters *before* the callbacks are gone so ``loop.exec``
        # returns and the caller can unwind through its cancel path.
        for loop, scope in waits:
            if scope is not None:
                scope.cancel()
            try:
                loop.quit()
            except RuntimeError:
                continue
        for task_id in task_ids:
            self._cancel_timeout(task_id)
        self._pool.shutdown()
        self._guard.close_now()


__all__ = [
    "DEFAULT_BLOCKING_TIMEOUT_S",
    "ProjectIoGuard",
    "ProjectTaskCanceled",
    "ProjectTaskRunner",
    "ProjectTaskScope",
    "ProjectTaskTimeout",
]
