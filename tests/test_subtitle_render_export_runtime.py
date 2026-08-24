"""Focused Qt-thread wiring contracts for subtitle export."""

from __future__ import annotations

from pathlib import Path

from krok_helper.subtitle_render.frontend.export_runtime import (
    ExportRuntimeCallbacks,
    ExportRuntimeController,
)


class _Signal:
    def __init__(self) -> None:
        self.callbacks: list[object] = []

    def connect(self, callback) -> None:
        self.callbacks.append(callback)


class _Thread:
    def __init__(self, parent) -> None:
        self.parent = parent
        self.started = _Signal()
        self.finished = _Signal()
        self.running = False
        self.quit_calls = 0

    def start(self) -> None:
        self.running = True

    def quit(self) -> None:
        self.quit_calls += 1

    def isRunning(self) -> bool:
        return self.running


class _Worker:
    def __init__(self, *args) -> None:
        self.args = args
        self.progressChanged = _Signal()
        self.logMessage = _Signal()
        self.finished = _Signal()
        self.cancelled = _Signal()
        self.failed = _Signal()
        self.thread = None
        self.cancel_calls = 0

    def moveToThread(self, thread) -> None:
        self.thread = thread

    def run(self) -> None:
        pass

    def deleteLater(self) -> None:
        pass

    def cancel(self) -> None:
        self.cancel_calls += 1


def _callback(name: str):
    def callback(*_args) -> None:
        return None

    callback.__name__ = name
    return callback


def test_export_runtime_prepares_worker_thread_and_all_signal_routes() -> None:
    controller = ExportRuntimeController(
        thread_factory=_Thread,
        worker_factory=_Worker,
    )
    callbacks = ExportRuntimeCallbacks(
        progress=_callback("progress"),
        log=_callback("log"),
        success=_callback("success"),
        cancelled=_callback("cancelled"),
        failed=_callback("failed"),
        thread_finished=_callback("thread_finished"),
    )
    parent = object()
    job = object()
    preview_path = Path("preview.jpg")

    handles = controller.prepare(
        parent=parent,
        job=job,
        ffmpeg_dir=Path("ffmpeg"),
        preview_image_path=preview_path,
        preview_width=640,
        callbacks=callbacks,
    )

    thread = handles.thread
    worker = handles.worker
    assert thread.parent is parent
    assert worker.args == (job, Path("ffmpeg"), preview_path, 640)
    assert worker.thread is thread
    assert worker.progressChanged.callbacks == [callbacks.progress]
    assert worker.logMessage.callbacks == [callbacks.log]
    assert worker.finished.callbacks == [callbacks.success, thread.quit]
    assert worker.cancelled.callbacks == [callbacks.cancelled, thread.quit]
    assert worker.failed.callbacks == [callbacks.failed, thread.quit]
    assert thread.finished.callbacks == [worker.deleteLater, callbacks.thread_finished]
    assert thread.started.callbacks == [worker.run]


def test_export_runtime_start_active_and_cancel_use_retained_handles() -> None:
    controller = ExportRuntimeController(
        thread_factory=_Thread,
        worker_factory=_Worker,
    )
    callback = _callback("callback")
    handles = controller.prepare(
        parent=object(),
        job=object(),
        ffmpeg_dir=None,
        preview_image_path=None,
        preview_width=320,
        callbacks=ExportRuntimeCallbacks(
            progress=callback,
            log=callback,
            success=callback,
            cancelled=callback,
            failed=callback,
            thread_finished=callback,
        ),
    )

    assert controller.is_active(None) is False
    assert controller.is_active(handles) is False
    controller.start(handles)
    assert controller.is_active(handles) is True
    controller.cancel(handles)
    assert handles.worker.cancel_calls == 1
