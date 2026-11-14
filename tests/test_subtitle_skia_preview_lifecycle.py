"""P03.7: real render loops, runtime DPR changes, invalidation and owner-thread release."""
from __future__ import annotations

from collections import Counter
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import pytest

from test_subtitle_skia_preview import qml_dir  # noqa: F401 - shared opt-in fixture
from test_subtitle_skia_preview import (
    assert_vulkan_validation_clean, desktop_environment, vulkan_validation_environment)


@pytest.mark.parametrize("loop", ["basic", "threaded"])
@pytest.mark.parametrize("host", ["view", "widget"])
def test_preview_lifecycle_on_actual_render_loop(qml_dir, tmp_path, loop, host):
    repo = Path(__file__).resolve().parents[1]
    env = desktop_environment() | {
        "PYTHONPATH": str(repo), "QSG_RENDER_LOOP": loop, "QT_FORCE_STDERR_LOGGING": "1",
        "QT_LOGGING_RULES": "krok.subtitle.skia.preview.debug=true;qt.scenegraph.general=true"}
    if sys.platform == "darwin" and os.environ.get("KROK_METAL_VALIDATION"):
        env |= {"MTL_DEBUG_LAYER": "1", "MTL_DEBUG_LAYER_ERROR_MODE": "assert"}
    validation_log = tmp_path / "validation.log"
    if sys.platform != "darwin" and os.environ.get("KROK_VULKAN_VALIDATION"):
        env |= vulkan_validation_environment(validation_log)
    stdout_path, stderr_path = (tmp_path / f"lifecycle.{stream}.log" for stream in ("stdout", "stderr"))
    # Write while the process runs so a timeout still leaves the last stage and native trace.
    with stdout_path.open("wb") as stdout_file, stderr_path.open("wb") as stderr_file:
        result = subprocess.run([sys.executable, str(repo / "scripts/skia_preview_lifecycle.py"),
                                 str(qml_dir), "--host", host,
                                 *(["--no-readback"] if os.environ.get("KROK_VULKAN_VALIDATION") else [])],
                                env=env, stdout=stdout_file,
                                stderr=stderr_file, timeout=120)
    stdout, stderr = (p.read_text(encoding="utf-8", errors="replace") for p in (stdout_path, stderr_path))
    assert result.returncode == 0, stdout + stderr
    report = json.loads(next(line.removeprefix("LIFECYCLE_RESULT ") for line in stdout.splitlines()
                             if line.startswith("LIFECYCLE_RESULT ")))
    assert report["elapsed_seconds"] >= 12
    if sys.platform == "linux" and env.get("QT_QPA_PLATFORM"):
        assert report["platform"] == env["QT_QPA_PLATFORM"]
    assert report["counters"]["destroyed"] == 2
    assert report["counters"]["initialized"] >= (2 if host == "view" and loop == "basic" else 8)
    assert report["counters"]["invalidated"] == report["counters"]["initialized"]
    events = re.findall(r"lifecycle (\w+) renderer=(\d+) thread=(\d+) gui=(true|false)", stderr)
    created = Counter((renderer, thread) for event, renderer, thread, _ in events if event == "created")
    released = Counter((renderer, thread) for event, renderer, thread, _ in events if event == "released")
    assert sum(created.values()) >= 8, stderr
    assert created == released, "Every renderer must release its GPU resources on its creating thread"
    assert sum(event == "rendered" for event, *_ in events) > 30, stderr
    # QQuickWidget disables the threaded loop. The plain QQuickView control must actually
    # use the requested loop, rather than merely accepting an environment variable.
    expected_gui = "true" if host == "widget" or loop == "basic" else "false"
    assert {gui for _, _, _, gui in events} == {expected_gui}, stderr
    assert "Skia preview frame failed" not in stderr
    assert "Skia could not share" not in stderr
    assert "outside its owner thread" not in stderr
    if sys.platform != "darwin" and os.environ.get("KROK_VULKAN_VALIDATION"):
        assert_vulkan_validation_clean(validation_log)
