from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from krok_helper.subtitle_render.frontend.workflow import background_tasks


class _CurrentThread:
    def __init__(self) -> None:
        self.quit_count = 0

    def quit(self) -> None:
        self.quit_count += 1


def _capture_thread_quit(monkeypatch) -> _CurrentThread:
    thread = _CurrentThread()
    monkeypatch.setattr(
        background_tasks,
        "QThread",
        SimpleNamespace(currentThread=lambda: thread),
    )
    return thread


def test_recovery_worker_preserves_success_payload(monkeypatch, tmp_path: Path) -> None:
    thread = _capture_thread_quit(monkeypatch)
    path = tmp_path / "recovery.yurika"
    monkeypatch.setattr(background_tasks, "save_recovery_project", lambda *_args: True)
    emitted: list[tuple] = []
    worker = background_tasks._RecoverySaveWorker(path, {"value": 1}, 2, 3, 4)
    worker.saved.connect(lambda *args: emitted.append(args))

    worker.run()

    assert emitted == [(path, 2, 3, 4, True)]
    assert thread.quit_count == 1


def test_recovery_worker_preserves_failure_payload(monkeypatch, tmp_path: Path) -> None:
    thread = _capture_thread_quit(monkeypatch)
    path = tmp_path / "recovery.yurika"

    def fail(*_args) -> bool:
        raise OSError("disk unavailable")

    monkeypatch.setattr(background_tasks, "save_recovery_project", fail)
    emitted: list[tuple] = []
    worker = background_tasks._RecoverySaveWorker(path, {}, 5, 6, 7)
    worker.failed.connect(lambda *args: emitted.append(args))

    worker.run()

    assert emitted == [(path, 5, 6, 7, "disk unavailable")]
    assert thread.quit_count == 1


def test_media_probe_worker_preserves_success_and_fallback(monkeypatch) -> None:
    thread = _capture_thread_quit(monkeypatch)
    path = Path("D:/media.mp4")
    info = object()
    monkeypatch.setattr(background_tasks, "probe_media", lambda *_args: info)
    emitted: list[tuple] = []
    worker = background_tasks._MediaProbeWorker("ffprobe", path, True)
    worker.probed.connect(lambda *args: emitted.append(args))

    worker.run()

    assert emitted == [(worker, info)]
    assert thread.quit_count == 1

    monkeypatch.setattr(
        background_tasks,
        "probe_media",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("probe failed")),
    )
    emitted.clear()
    worker = background_tasks._MediaProbeWorker("ffprobe", path, False)
    worker.probed.connect(lambda *args: emitted.append(args))
    worker.run()

    assert emitted == [(worker, None)]
    assert thread.quit_count == 2


def test_render_worker_preserves_callbacks_and_result(monkeypatch, tmp_path: Path) -> None:
    output = tmp_path / "output.mp4"
    job = SimpleNamespace(output_path=output, width=1920, height=1080, fps=60)
    calls: list[dict] = []

    def render(_job, **kwargs):
        calls.append(kwargs)
        kwargs["on_progress"](2, 5)
        return output

    monkeypatch.setattr(background_tasks, "render_subtitle_video", render)
    progress: list[tuple[int, int]] = []
    finished: list[Path] = []
    worker = background_tasks._RenderWorker(job, Path("D:/ffmpeg"))
    worker.progressChanged.connect(lambda current, total: progress.append((current, total)))
    worker.finished.connect(finished.append)

    worker.run()

    assert progress == [(2, 5)]
    assert finished == [output]
    assert calls[0]["should_cancel"] is not None
    assert calls[0]["on_process_started"] is not None


def test_render_worker_cancel_terminates_active_process(monkeypatch) -> None:
    job = SimpleNamespace(output_path=Path("out.mp4"), width=1, height=1, fps=60)
    process = object()
    terminated: list[object] = []
    monkeypatch.setattr(background_tasks, "terminate_process", terminated.append)
    worker = background_tasks._RenderWorker(job, None)
    worker._set_process(process)

    worker.cancel()

    assert worker.should_cancel()
    assert terminated == [process]
