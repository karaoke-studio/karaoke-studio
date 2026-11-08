"""Qt thread wiring for one subtitle export runtime."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QObject, QThread, QTimer

from krok_helper.subtitle_render.engine.export.render_job import RenderJob
from krok_helper.subtitle_render.frontend.workflow.background_tasks import _RenderWorker


@dataclass(frozen=True)
class ExportRuntimeCallbacks:
    """Host callbacks driven by an export worker and its thread."""

    progress: Any
    log: Any
    success: Any
    cancelled: Any
    failed: Any
    thread_finished: Any


@dataclass
class ExportRuntimeHandles:
    """Thread and worker handles retained by the host for lifecycle checks."""

    thread: Any
    worker: Any
    preparation_timer: Any = None
    preparing: bool = False


class ExportRuntimeController:
    """Prepare, start, and cancel the Qt runtime for one render job."""

    def __init__(self, *, thread_factory: Any = QThread, worker_factory: Any = _RenderWorker):
        self._thread_factory = thread_factory
        self._worker_factory = worker_factory

    def prepare(
        self,
        *,
        parent: Any,
        job: RenderJob,
        ffmpeg_dir: Path | None,
        preview_image_path: Path | None,
        preview_width: int,
        callbacks: ExportRuntimeCallbacks,
    ) -> ExportRuntimeHandles:
        from krok_helper.subtitle_render.engine.text.font_preparation import iter_prepare_fonts
        thread = self._thread_factory(parent)
        worker = self._worker_factory(
            job,
            ffmpeg_dir,
            preview_image_path,
            preview_width,
        )
        worker.moveToThread(thread)
        worker.progressChanged.connect(callbacks.progress)
        worker.logMessage.connect(callbacks.log)
        worker.finished.connect(callbacks.success)
        worker.cancelled.connect(callbacks.cancelled)
        worker.failed.connect(callbacks.failed)
        worker.finished.connect(thread.quit)
        worker.cancelled.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(callbacks.thread_finished)
        thread.started.connect(worker.run)
        handles = ExportRuntimeHandles(thread=thread, worker=worker)
        if isinstance(parent, QObject):
            steps = iter_prepare_fonts(job.track, job.style, list(job.extra_tracks))
            timer = QTimer(parent)
            timer.setInterval(1)
            handles.preparation_timer = timer

            def advance() -> None:
                try:
                    next(steps)
                except StopIteration:
                    timer.stop()
                    timer.deleteLater()
                    handles.preparation_timer = None
                    handles.preparing = False
                    thread.start()

            timer.timeout.connect(advance)
        else:
            for _ in iter_prepare_fonts(job.track, job.style, list(job.extra_tracks)):
                pass
        return handles

    @staticmethod
    def is_active(handles: ExportRuntimeHandles | None) -> bool:
        return bool(handles is not None and (handles.preparing or handles.thread.isRunning()))

    @staticmethod
    def start(handles: ExportRuntimeHandles) -> None:
        if handles.preparation_timer is not None:
            handles.preparing = True
            handles.preparation_timer.start()
        else:
            handles.thread.start()

    @staticmethod
    def cancel(handles: ExportRuntimeHandles) -> None:
        handles.worker.cancel()
        if handles.preparing and handles.preparation_timer is not None:
            handles.preparation_timer.stop()
            handles.preparation_timer.deleteLater()
            handles.preparation_timer = None
            handles.preparing = False
            # Run the already-cancelled worker to keep the established
            # cancelled -> thread.quit -> cleanup signal lifecycle intact.
            handles.thread.start()
