"""Managed pymss server process lifecycle."""

from __future__ import annotations

import os
import secrets
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .client import PyMSSClient


_SERVER_BRIDGE_SOURCE = r'''from __future__ import annotations

import importlib
import json
import os
import sys
import time
from pathlib import Path


_progress_path = Path(os.environ["KARAOKE_STUDIO_PYMSS_PROGRESS"])
_server_app = importlib.import_module("pymss.server.app")


def _publish_progress(done, total, message, status="running"):
    payload = {
        "done": max(0, int(done or 0)),
        "total": max(1, int(total or 1)),
        "message": str(message or "Processing audio"),
        "status": status,
        "updated_at": time.time(),
    }
    try:
        _progress_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = _progress_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(temporary, _progress_path)
    except OSError:
        # Progress reporting must never fail the actual separation request.
        pass


def _run_separation_with_progress(loaded, mix, stems):
    separator = loaded.separator
    previous_callback = getattr(separator, "progress_callback", None)
    separator.progress_callback = _publish_progress
    try:
        if separator.model_type == "vr":
            result = separator.separate(mix, pbar=False)
        else:
            result = separator.separate(mix, pbar=False, stems=stems)
        return result
    except Exception:
        try:
            _publish_progress(0, 1, "Processing failed", "failed")
        except OSError:
            pass
        raise
    finally:
        separator.progress_callback = previous_callback


_server_app._run_separation_sync = _run_separation_with_progress

from pymss.cli import main

raise SystemExit(main())
'''


def _write_server_bridge(root: Path) -> Path:
    bridge = root / "manifests" / "karaoke_studio_server.py"
    bridge.parent.mkdir(parents=True, exist_ok=True)
    try:
        current = bridge.read_text(encoding="utf-8")
    except OSError:
        current = ""
    if current != _SERVER_BRIDGE_SOURCE:
        bridge.write_text(_SERVER_BRIDGE_SOURCE, encoding="utf-8")
    return bridge


def reserve_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def runtime_python(install_dir: str | os.PathLike) -> Path:
    root = Path(install_dir) / "runtime"
    candidates = (
        root / "python.exe",
        root / "python",
        root / "bin" / "python3",
        root / "bin" / "python",
    )
    return next((candidate for candidate in candidates if candidate.is_file()), candidates[0])


def build_server_command(
    executable: str | os.PathLike,
    *,
    model_dir: str | os.PathLike,
    host: str,
    port: int,
    api_key: str,
    source: str,
    device: str,
    bridge_path: str | os.PathLike | None = None,
) -> list[str]:
    exe = Path(executable)
    name = exe.name.lower()
    prefix = [str(exe)]
    if name.startswith("python"):
        prefix.extend(
            [str(bridge_path)] if bridge_path else ["-m", "pymss.cli"]
        )
    return [
        *prefix,
        "serve",
        "--model-dir",
        str(model_dir),
        "--host",
        host,
        "--port",
        str(port),
        "--api-key",
        api_key,
        "--source",
        source,
        "--device",
        device,
        "--max-audio-seconds",
        "600",
        "--max-queue-size",
        "1",
    ]


@dataclass
class ManagedServiceProcess:
    install_dir: Path
    process: subprocess.Popen
    client: PyMSSClient
    api_key: str
    port: int
    log_stream: object

    @property
    def running(self) -> bool:
        return self.process.poll() is None

    def stop(self, timeout_seconds: float = 5.0) -> bool:
        try:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=timeout_seconds)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=max(1.0, timeout_seconds))
            return self.process.poll() is not None
        finally:
            try:
                self.log_stream.close()
            except Exception:
                pass

    @classmethod
    def start(
        cls,
        install_dir: str | os.PathLike,
        *,
        executable: str | os.PathLike | None = None,
        model_dir: str | os.PathLike | None = None,
        user_models_path: str | os.PathLike | None = None,
        source: str = "modelscope",
        device: str = "auto",
        startup_timeout: float = 45.0,
        cancelled=None,
        popen_factory=subprocess.Popen,
    ) -> "ManagedServiceProcess":
        root = Path(install_dir)
        managed_runtime = executable is None
        exe = Path(executable) if executable else runtime_python(root)
        if not exe.is_file():
            raise FileNotFoundError(f"找不到 PyMSS Runtime 入口：{exe}")
        models = Path(model_dir) if model_dir else root / "models"
        manifests = root / "manifests"
        logs = root / "logs"
        user_models = (
            Path(user_models_path)
            if user_models_path
            else manifests / "external-models.json"
        )
        for directory in (models, manifests, logs, user_models.parent):
            directory.mkdir(parents=True, exist_ok=True)
        port = reserve_local_port()
        api_key = secrets.token_urlsafe(32)
        bridge_path = _write_server_bridge(root) if exe.name.lower().startswith("python") else None
        command = build_server_command(
            exe,
            model_dir=models,
            host="127.0.0.1",
            port=port,
            api_key=api_key,
            source=source,
            device=device,
            bridge_path=bridge_path,
        )
        env = os.environ.copy()
        env["PYMSS_MODEL_DIR"] = str(models)
        env["PYMSS_USER_MODELS"] = str(user_models)
        env["KARAOKE_STUDIO_PYMSS_PROGRESS"] = str(
            logs / "separation-progress.json"
        )
        env["PYTHONUTF8"] = "1"
        # The managed embedded runtime must be hermetic.  In particular, a
        # globally configured PYTHONUSERBASE must not make a different torch
        # or PyMSS installation leak into the service.  An explicitly selected
        # external environment remains under the user's control, so do not
        # alter its site-package policy.
        if managed_runtime:
            env["PYTHONNOUSERSITE"] = "1"
        log_path = logs / "server.log"
        log_stream = log_path.open("a", encoding="utf-8", errors="replace")
        creationflags = 0
        if sys.platform == "win32":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            process = popen_factory(
                command,
                cwd=str(root),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log_stream,
                stderr=subprocess.STDOUT,
                creationflags=creationflags,
            )
            client = PyMSSClient(f"http://127.0.0.1:{port}", api_key=api_key)
            handle = cls(root, process, client, api_key, port, log_stream)
            try:
                client.wait_until_healthy(startup_timeout, cancelled=cancelled)
            except Exception:
                handle.stop(timeout_seconds=2.0)
                raise
            return handle
        except Exception:
            if not log_stream.closed:
                log_stream.close()
            raise


__all__ = [
    "ManagedServiceProcess",
    "build_server_command",
    "reserve_local_port",
    "runtime_python",
]
