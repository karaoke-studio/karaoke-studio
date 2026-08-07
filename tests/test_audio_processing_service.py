from __future__ import annotations

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
