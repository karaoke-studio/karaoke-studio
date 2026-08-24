"""Focused contracts for the recovery auto-save thread runtime."""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEventLoop, QTimer  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402
import pytest  # noqa: E402

from krok_helper.subtitle_render.frontend.project_autosave import (  # noqa: E402
    ProjectAutoSaveRuntime,
    RecoverySaveRequest,
)
from krok_helper.subtitle_render.project_store import load_render_project  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _request(path: Path, snapshot_id: int) -> RecoverySaveRequest:
    return RecoverySaveRequest(
        path=path,
        payload={
            "value": snapshot_id,
            "recovery": {
                "source_project_path": None,
                "created_at_unix": 1.0,
                "snapshot_id": snapshot_id,
            },
        },
        generation=2,
        revision=3,
        snapshot_id=snapshot_id,
    )


def _wait_for(signal, timeout_ms: int = 3_000) -> None:
    loop = QEventLoop()
    signal.connect(loop.quit)
    QTimer.singleShot(timeout_ms, loop.quit)
    loop.exec()


def test_project_auto_save_runtime_forwards_success_and_clears_worker(
    qapp,
    tmp_path: Path,
) -> None:
    path = tmp_path / "song.recovery"
    runtime = ProjectAutoSaveRuntime()
    saved: list[tuple] = []
    runtime.saved.connect(lambda *args: saved.append(args))

    assert runtime.start(_request(path, 10)) is True
    _wait_for(runtime.saved)
    assert runtime.wait(3_000) is True
    qapp.processEvents()

    assert load_render_project(path)["value"] == 10
    assert saved == [(path, 2, 3, 10, True)]
    assert runtime.thread is None
    assert runtime.worker is None


def test_project_auto_save_runtime_coalesces_a_busy_follow_up(
    qapp,
    tmp_path: Path,
) -> None:
    runtime = ProjectAutoSaveRuntime()
    reruns: list[None] = []
    runtime.rerunRequested.connect(lambda: reruns.append(None))

    assert runtime.start(_request(tmp_path / "first.recovery", 20)) is True
    assert runtime.start(_request(tmp_path / "second.recovery", 21)) is False
    assert runtime.pending is True
    _wait_for(runtime.rerunRequested)
    assert runtime.wait(3_000) is True
    qapp.processEvents()

    assert reruns == [None]
    assert runtime.pending is False
    assert runtime.thread is None
