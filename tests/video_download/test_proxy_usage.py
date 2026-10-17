from __future__ import annotations

import json
import subprocess

from krok_helper.network import proxy_cli_args_for_app_settings, subprocess_env_for_app_settings
from krok_helper.settings import AppSettings
from krok_helper.video_download.ytdlp_service import YtDlpService


def _settings_with_manual_proxy() -> AppSettings:
    return AppSettings(
        updater={
            "proxy": {
                "mode": "manual",
                "manual_url": "127.0.0.1:7890",
            }
        }
    )


def test_global_proxy_builds_cli_args_and_subprocess_env() -> None:
    settings = _settings_with_manual_proxy()

    assert proxy_cli_args_for_app_settings(settings) == ["--proxy", "http://127.0.0.1:7890"]
    env = subprocess_env_for_app_settings(settings)
    assert env["HTTP_PROXY"] == "http://127.0.0.1:7890"
    assert env["HTTPS_PROXY"] == "http://127.0.0.1:7890"


def test_ytdlp_cli_extract_uses_global_proxy(monkeypatch) -> None:
    captured: dict[str, object] = {}
    service = YtDlpService(app_settings=_settings_with_manual_proxy())
    monkeypatch.setattr(service, "_find_ytdlp_cli", lambda: "yt-dlp")

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["env"] = kwargs.get("env")
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps({"title": "ok", "duration": 1, "formats": []}),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    service._extract_info_with_cli("https://www.youtube.com/watch?v=abc")

    command = captured["command"]
    assert isinstance(command, list)
    assert command[1:3] == ["--proxy", "http://127.0.0.1:7890"]
    assert "-f" not in command
    assert "--js-runtimes" in command
    env = captured["env"]
    assert isinstance(env, dict)
    assert env["HTTPS_PROXY"] == "http://127.0.0.1:7890"


def test_ytdlp_cli_extract_does_not_force_best_for_bilibili(monkeypatch) -> None:
    captured: dict[str, object] = {}
    service = YtDlpService()
    monkeypatch.setattr(service, "_find_ytdlp_cli", lambda: "yt-dlp")

    def fake_run(command, **kwargs):
        del kwargs
        captured["command"] = command
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps({"title": "ok", "duration": 1, "formats": []}),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    service._extract_info_with_cli("https://www.bilibili.com/video/BV1abc")

    command = captured["command"]
    assert isinstance(command, list)
    assert "-f" not in command


def test_ytdlp_cli_extract_ignores_invalid_cookie_file(monkeypatch, tmp_path) -> None:
    captured: dict[str, object] = {}
    cookie_file = tmp_path / "youtube_cookies.txt"
    cookie_file.write_text('{"not": "netscape"}', encoding="utf-8")
    service = YtDlpService()
    monkeypatch.setattr(service, "_find_ytdlp_cli", lambda: "yt-dlp")

    def fake_run(command, **kwargs):
        captured["command"] = command
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps({"title": "ok", "duration": 1, "formats": []}),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    service._extract_info_with_cli(
        "https://www.youtube.com/watch?v=ARj1adoUoEU&list=RDARj1adoUoEU&start_radio=1",
        str(cookie_file),
    )

    command = captured["command"]
    assert isinstance(command, list)
    assert "--cookies" not in command



def test_bundled_javascript_runtime_precedes_path(monkeypatch, tmp_path):
    import os
    import sys
    runtime = tmp_path / "tools" / "deno" / ("deno.exe" if os.name == "nt" else "deno")
    runtime.parent.mkdir(parents=True)
    runtime.touch()
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    monkeypatch.setattr("shutil.which", lambda _: "other-runtime")
    service = YtDlpService()
    assert service._javascript_runtimes() == {"deno": {"path": str(runtime)}}
    assert service._javascript_cli_args("https://youtu.be/abc") == ["--js-runtimes", f"deno:{runtime}"]
    assert service._javascript_cli_args("https://www.bilibili.com/video/BV1abc") == []


def test_python_extraction_receives_javascript_runtime(monkeypatch):
    service = YtDlpService()
    expected = {"deno": {"path": "C:/runtime with spaces/deno.exe"}}
    monkeypatch.setattr(service, "_javascript_runtimes", lambda: expected)
    captured = {}
    class FakeYoutubeDL:
        def __init__(self, opts):
            captured.update(opts)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def extract_info(self, url, download):
            return {"title": "ok"}
    service._extract_info_with_python_api(FakeYoutubeDL, "https://youtu.be/abc", None)
    assert captured["js_runtimes"] == expected
