"""Focused Qt-thread wiring contracts for subtitle export."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from krok_helper.subtitle_render.frontend.workflow.export_runtime import (
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
    job = SimpleNamespace(track=None, style=None, extra_tracks=())
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
        job=SimpleNamespace(track=None, style=None, extra_tracks=()),
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


def test_export_preparation_yields_and_can_cancel(qapp, qtbot, monkeypatch):
    from PyQt6.QtCore import QObject
    from krok_helper.subtitle_render.engine.text import font_preparation
    steps = []

    def prepare(*args):
        for i in range(100):
            steps.append(i)
            yield

    monkeypatch.setattr(font_preparation, "iter_prepare_fonts", prepare)
    controller = ExportRuntimeController(thread_factory=_Thread, worker_factory=_Worker)
    callback = _callback("callback")
    parent = QObject()
    handles = controller.prepare(parent=parent,
        job=SimpleNamespace(track=None, style=None, extra_tracks=()),
        ffmpeg_dir=None, preview_image_path=None, preview_width=320,
        callbacks=ExportRuntimeCallbacks(callback, callback, callback, callback, callback, callback))
    assert steps == []
    controller.start(handles)
    assert controller.is_active(handles)
    assert not handles.thread.isRunning()
    qtbot.waitUntil(lambda: len(steps) >= 2)
    assert len(steps) < 100
    controller.cancel(handles)
    count = len(steps)
    qtbot.wait(20)
    assert len(steps) == count
    assert handles.worker.cancel_calls == 1
    assert handles.thread.isRunning()


def test_window_retains_export_preparation_state():
    from krok_helper.subtitle_render.frontend.main_window import SubtitleRenderWindow
    from krok_helper.subtitle_render.frontend.workflow.export_runtime import ExportRuntimeHandles
    thread, worker = object(), object()
    runtime = ExportRuntimeHandles(thread, worker, preparing=True)
    host = SimpleNamespace(_render_thread=thread, _render_worker=worker, _render_runtime=runtime)
    assert SubtitleRenderWindow._current_export_runtime(host) is runtime


def test_export_worker_starts_only_after_last_preparation_step(qapp, qtbot, monkeypatch):
    from PyQt6.QtCore import QObject
    from krok_helper.subtitle_render.engine.text import font_preparation
    steps = []
    def prepare(*args):
        for i in range(3):
            steps.append(i)
            yield
    monkeypatch.setattr(font_preparation, "iter_prepare_fonts", prepare)
    controller = ExportRuntimeController(thread_factory=_Thread, worker_factory=_Worker)
    callback = _callback("callback")
    parent = QObject()
    handles = controller.prepare(parent=parent,
        job=SimpleNamespace(track=None, style=None, extra_tracks=()),
        ffmpeg_dir=None, preview_image_path=None, preview_width=320,
        callbacks=ExportRuntimeCallbacks(callback, callback, callback, callback, callback, callback))
    controller.start(handles)
    assert not handles.thread.isRunning()
    qtbot.waitUntil(handles.thread.isRunning)
    assert steps == [0, 1, 2]
    assert not handles.preparing
    assert handles.preparation_timer is None


def test_cancelled_preparation_worker_never_enters_renderer(qapp, monkeypatch):
    from krok_helper.subtitle_render.frontend.workflow import background_tasks
    def forbidden(*args, **kwargs):
        raise AssertionError("cancelled preparation must not render")
    monkeypatch.setattr(background_tasks, "render_subtitle_video", forbidden)
    worker = background_tasks._RenderWorker(SimpleNamespace(), None, None, 320)
    cancelled = []
    worker.cancelled.connect(cancelled.append)
    worker.cancel()
    worker.run()
    assert cancelled == ["导出已取消"]


def test_real_export_thread_cancelled_during_preparation(qapp, qtbot, monkeypatch):
    from PyQt6.QtCore import QObject
    from krok_helper.subtitle_render.engine.text import font_preparation
    from krok_helper.subtitle_render.frontend.workflow import background_tasks
    steps = []
    def prepare(*args):
        for index in range(1000):
            steps.append(index)
            yield
    monkeypatch.setattr(font_preparation, "iter_prepare_fonts", prepare)
    def forbidden(*args, **kwargs):
        raise AssertionError("Cancelled preparation entered renderer")
    monkeypatch.setattr(background_tasks, "render_subtitle_video", forbidden)
    events = []
    parent = QObject()
    controller = ExportRuntimeController()
    ignore = lambda *args: None
    handles = controller.prepare(parent=parent,
        job=SimpleNamespace(track=None, style=None, extra_tracks=()),
        ffmpeg_dir=None, preview_image_path=None, preview_width=320,
        callbacks=ExportRuntimeCallbacks(ignore, ignore,
            lambda *args: events.append("success"),
            lambda *args: events.append("cancelled"),
            lambda *args: events.append("failed"),
            lambda: events.append("finished")))
    controller.start(handles)
    qtbot.waitUntil(lambda: len(steps) > 0)
    controller.cancel(handles)
    qtbot.waitUntil(lambda: "finished" in events, timeout=5000)
    assert events == ["cancelled", "finished"]
    assert not handles.thread.isRunning()
    assert handles.preparation_timer is None
