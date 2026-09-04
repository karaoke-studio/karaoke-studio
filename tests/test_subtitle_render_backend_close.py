"""NativeRendererProcess.close() 的管道收尾回归。

close() 此前从不显式关闭 sidecar 的 stdin/stdout/stderr，留给 GC 的
TextIOWrapper finalizer 对死管道 flush，Windows 上以「Exception ignored in:
<_io.TextIOWrapper ...>」+ OSError 22 刷未捕获栈。此处锁定两个行为：
显式关闭三根管道；管道句柄已失效（Errno 22）时吞掉异常不影响收尸流程。
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from krok_helper.subtitle_render.native.backend import (  # noqa: E402
    NativeRendererProcess,
)


def _write_min_sidecar(tmp_path: Path) -> Path:
    script = tmp_path / "min_sidecar.py"
    script.write_text(
        textwrap.dedent(
            """
            import json
            import sys

            print(json.dumps({"ok": True, "event": "ready", "schema": 1}), flush=True)
            for raw in sys.stdin:
                request = json.loads(raw)
                if request.get("cmd") == "shutdown":
                    print(json.dumps({"ok": True, "event": "shutdown"}), flush=True)
                    break
            """
        ),
        encoding="utf-8",
    )

    # CreateProcess 无法直接执行 .py：Windows 用 .cmd 启动器包一层（与
    # test_subtitle_render_native_protocol 的 fake sidecar 同一做法）。
    if os.name == "nt":
        launcher = tmp_path / "min_sidecar.cmd"
        launcher.write_text(
            f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n',
            encoding="utf-8",
        )
        return launcher

    launcher = tmp_path / "min_sidecar"
    launcher.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n',
        encoding="utf-8",
    )
    launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR)
    return launcher


def test_close_explicitly_closes_sidecar_pipes(tmp_path, monkeypatch):
    sidecar = _write_min_sidecar(tmp_path)
    created = []
    real_popen = subprocess.Popen

    def recording_popen(*args, **kwargs):
        proc = real_popen(*args, **kwargs)
        created.append(proc)
        return proc

    monkeypatch.setattr(subprocess, "Popen", recording_popen)
    renderer = NativeRendererProcess(
        sidecar, response_timeout_s=2.0, close_timeout_s=1.0
    )
    try:
        renderer.start()
        assert created
        streams = (created[-1].stdin, created[-1].stdout, created[-1].stderr)
        assert all(stream is not None for stream in streams)
    finally:
        renderer.close()

    assert renderer.is_running is False
    # close() 之后管道必须已经显式关闭，而不是留给 GC 的 finalizer。
    assert all(stream.closed for stream in streams)


def test_close_swallows_dead_pipe_handle_errors(tmp_path):
    sidecar = _write_min_sidecar(tmp_path)
    renderer = NativeRendererProcess(
        sidecar, response_timeout_s=2.0, close_timeout_s=1.0
    )
    renderer.start()
    process = renderer._process  # noqa: SLF001 — 替换管道以模拟句柄失效

    class _DeadPipe:
        """模拟进程已死后的管道：写 / 冲刷 / 关闭全部报 Errno 22。"""

        def write(self, data):
            raise OSError(22, "Invalid argument")

        def flush(self):
            raise OSError(22, "Invalid argument")

        def close(self):
            raise OSError(22, "Invalid argument")

    assert process is not None
    process.stdin = _DeadPipe()

    # 收尸流程不能被死句柄异常打断。
    renderer.close()
    assert renderer.is_running is False
