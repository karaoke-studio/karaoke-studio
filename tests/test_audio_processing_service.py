from __future__ import annotations

import sys

from krok_helper.audio_processing.separation import service


class _Process:
    def __init__(self) -> None:
        self.returncode = None
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


class _Client:
    def __init__(self, url, api_key="") -> None:
        self.url = url
        self.api_key = api_key
        self.waited = False

    def wait_until_healthy(self, timeout, *, cancelled=None) -> dict:
        del timeout, cancelled
        self.waited = True
        return {"status": "ok"}


def test_build_server_command_places_model_dir_on_serve_subcommand(tmp_path) -> None:
    command = service.build_server_command(
        tmp_path / "python.exe",
        model_dir=tmp_path / "models",
        host="127.0.0.1",
        port=8765,
        api_key="secret",
        source="modelscope",
        device="auto",
    )
    assert command[:3] == [str(tmp_path / "python.exe"), "-m", "pymss.cli"]
    assert command.index("serve") < command.index("--model-dir")
    assert command[command.index("--host") + 1] == "127.0.0.1"
    assert command[command.index("--max-queue-size") + 1] == "1"


def test_managed_service_sets_private_registry_and_stops_owned_process(tmp_path, monkeypatch) -> None:
    executable = tmp_path / "pymss" / "runtime" / "python.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"fake")
    captured = {}
    process = _Process()

    def popen(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return process

    monkeypatch.setattr(service, "PyMSSClient", _Client)
    handle = service.ManagedServiceProcess.start(
        tmp_path / "pymss",
        popen_factory=popen,
        startup_timeout=0.1,
    )

    assert handle.running
    assert handle.client.waited
    assert captured["env"]["PYMSS_MODEL_DIR"].endswith("pymss\\models")
    assert captured["env"]["PYMSS_USER_MODELS"].endswith(
        "pymss\\manifests\\external-models.json"
    )
    assert captured["env"]["KARAOKE_STUDIO_PYMSS_PROGRESS"].endswith(
        "pymss\\logs\\separation-progress.json"
    )
    bridge = tmp_path / "pymss" / "manifests" / "karaoke_studio_server.py"
    assert captured["command"][1] == str(bridge)
    bridge_source = bridge.read_text(encoding="utf-8")
    assert "progress_callback" in bridge_source
    compile(bridge_source, str(bridge), "exec")
    assert captured["env"]["PYTHONNOUSERSITE"] == "1"
    assert captured["stdin"] is not None
    if sys.platform == "win32":
        assert captured["creationflags"]
        assert captured["startupinfo"].dwFlags
    assert handle.stop(timeout_seconds=0.1)
    assert process.terminated


def test_existing_environment_can_reuse_its_model_cache_and_user_registry(
    tmp_path, monkeypatch
) -> None:
    executable = tmp_path / "python.exe"
    executable.write_bytes(b"fake")
    model_dir = tmp_path / "existing-models"
    user_models = tmp_path / "existing-user-models.json"
    captured = {}

    def popen(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return _Process()

    monkeypatch.setattr(service, "PyMSSClient", _Client)
    handle = service.ManagedServiceProcess.start(
        tmp_path / "service-work",
        executable=executable,
        model_dir=model_dir,
        user_models_path=user_models,
        popen_factory=popen,
        startup_timeout=0.1,
    )
    try:
        assert captured["env"]["PYMSS_MODEL_DIR"] == str(model_dir)
        assert captured["env"]["PYMSS_USER_MODELS"] == str(user_models)
        # Do not change the package-resolution policy of a user-selected
        # external Python environment.
        assert "PYTHONNOUSERSITE" not in captured["env"] or captured["env"][
            "PYTHONNOUSERSITE"
        ] == __import__("os").environ.get("PYTHONNOUSERSITE")
        model_index = captured["command"].index("--model-dir")
        assert captured["command"][model_index + 1] == str(model_dir)
    finally:
        handle.stop()


def test_api_key_starting_with_dash_is_not_parsed_as_a_flag(tmp_path) -> None:
    """回归：随机 api key 以 '-' 开头时，分成两个参数会被 argparse 当成选项。

    实机表现为服务间歇性起不来（约 1.5% 的启动），日志里是
    ``pymss serve: error: argument --api-key: expected one argument``。
    """
    import argparse

    key = "-DjLPjfe6VNhCsQabS3oQw"
    command = service.build_server_command(
        tmp_path / "python.exe",
        model_dir=tmp_path / "models",
        host="127.0.0.1",
        port=8765,
        api_key=key,
        source="modelscope",
        device="auto",
    )
    assert f"--api-key={key}" in command
    assert "--api-key" not in command, "不能再用会被误解析的两段式写法"

    # 用与 pymss 同构的解析器验证：两段式会失败，= 形式能正确取到值。
    parser = argparse.ArgumentParser(prog="pymss serve")
    parser.add_argument("--model-dir")
    parser.add_argument("--host")
    parser.add_argument("--port")
    parser.add_argument("--api-key")
    parser.add_argument("--source")
    parser.add_argument("--device")
    parser.add_argument("--max-audio-seconds")
    parser.add_argument("--max-queue-size")
    parsed = parser.parse_args(command[command.index("serve") + 1 :])
    assert parsed.api_key == key


def test_generated_api_keys_survive_argument_parsing(tmp_path) -> None:
    """随机生成的 key 无论首字符是什么都必须能被正确解析。"""
    import argparse
    import secrets

    parser = argparse.ArgumentParser(prog="pymss serve")
    parser.add_argument("--api-key")
    for _ in range(200):
        key = secrets.token_urlsafe(32)
        command = service.build_server_command(
            tmp_path / "python.exe",
            model_dir=tmp_path / "models",
            host="127.0.0.1",
            port=8765,
            api_key=key,
            source="modelscope",
            device="auto",
        )
        known, _unknown = parser.parse_known_args(command)
        assert known.api_key == key


class _DeadProcess(_Process):
    """启动后立刻退出的进程（模拟参数解析失败）。"""

    def __init__(self, returncode: int = 2) -> None:
        super().__init__()
        self.returncode = returncode


class _UnreachableClient(_Client):
    def wait_until_healthy(self, timeout, *, cancelled=None):
        del timeout, cancelled
        raise ConnectionError(
            "HTTPConnectionPool(host='127.0.0.1', port=1728): Max retries exceeded "
            "with url: /health (Caused by NewConnectionError(...))"
        )


def _start_failing(tmp_path, monkeypatch, process, log_text: str = ""):
    executable = tmp_path / "pymss" / "runtime" / "python.exe"
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_bytes(b"fake")
    logs = tmp_path / "pymss" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    if log_text:
        (logs / "server.log").write_text(log_text, encoding="utf-8")

    monkeypatch.setattr(service, "PyMSSClient", _UnreachableClient)
    return service.ManagedServiceProcess.start(
        tmp_path / "pymss",
        popen_factory=lambda command, **kwargs: process,
        startup_timeout=0.05,
    )


def test_process_that_exits_reports_the_reason_from_the_log(tmp_path, monkeypatch) -> None:
    """进程已退出时，连接被拒只是症状；真正原因在日志里，必须带出来。"""
    import pytest

    log = (
        "INFO:     Started server process [1]\n"
        "usage: pymss serve [-h] [--model-dir MODEL_DIR]\n"
        "pymss serve: error: argument --api-key: expected one argument\n"
    )
    with pytest.raises(RuntimeError) as excinfo:
        _start_failing(tmp_path, monkeypatch, _DeadProcess(2), log)

    message = str(excinfo.value)
    assert "立即退出" in message
    assert "退出码 2" in message
    assert "expected one argument" in message, "应把日志里的真实原因带给用户"
    assert "HTTPConnectionPool" not in message, "不应把 HTTP 连接栈丢给用户"
    assert "server.log" in message


def test_running_process_that_never_becomes_healthy_is_reported_plainly(
    tmp_path, monkeypatch
) -> None:
    import pytest

    with pytest.raises(RuntimeError) as excinfo:
        _start_failing(tmp_path, monkeypatch, _Process())

    message = str(excinfo.value)
    assert "没有就绪" in message
    assert "HTTPConnectionPool" not in message
    assert "server.log" in message


def test_cancellation_is_not_turned_into_a_startup_error(tmp_path, monkeypatch) -> None:
    """用户主动取消不能被包装成启动失败。"""
    import pytest

    class _CancelledClient(_Client):
        def wait_until_healthy(self, timeout, *, cancelled=None):
            raise InterruptedError("等待 PyMSS 服务启动已取消。")

    executable = tmp_path / "pymss" / "runtime" / "python.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"fake")
    monkeypatch.setattr(service, "PyMSSClient", _CancelledClient)
    with pytest.raises(InterruptedError):
        service.ManagedServiceProcess.start(
            tmp_path / "pymss",
            popen_factory=lambda command, **kwargs: _Process(),
            startup_timeout=0.05,
        )
