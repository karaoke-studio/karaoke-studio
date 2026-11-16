from __future__ import annotations

import io
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from krok_helper import media_tools
from krok_helper.errors import MissingMediaToolError, ProcessingError


@pytest.mark.parametrize("message,tool", [
    (MissingMediaToolError("ffprobe.exe"), "ffprobe"),
    ("素材分析失败：找不到 ffmpeg.exe。请先安装。", "ffmpeg"),
    ("未检测到 FFmpeg，请配置路径", "ffmpeg"),
    ("所选 ffmpeg 目录无效，请重新选择。", "ffmpeg"),
    ("ERROR: ffmpeg and ffprobe not found. Please install", "ffmpeg"),
    ("ffprobe was not found", "ffprobe"),
    ("ffmpeg is not installed", "ffmpeg"),
    ("找不到 ffmpeg 提取的音频文件", None),
    ("找不到 ffmpeg 提取的音频文件\n找不到 ffprobe。", "ffprobe"),
    ("ffmpeg 执行失败，退出码: 1", None),
    ("ffmpeg: input.mp4 not found", None),
    ("未找到模型文件", None),
])
def test_only_missing_tools_offer_installation(message, tool):
    assert media_tools.missing_tool(message) == tool


def test_inspection_distinguishes_missing_broken_and_usable_tools(monkeypatch):
    def find(tool, _directory):
        if tool == "ffprobe":
            raise MissingMediaToolError(tool)
        return f"D:/tools/{tool}.exe"

    def run(command, **kwargs):
        assert kwargs["timeout"] == 5
        if "ffplay" in command[0]:
            raise subprocess.TimeoutExpired(command, 5)
        return SimpleNamespace(returncode=0, stdout="ffmpeg version 9.0\n", stderr="")

    monkeypatch.setattr(media_tools, "find_tool", find)
    monkeypatch.setattr(media_tools.subprocess, "run", run)
    statuses = media_tools.inspect_tools(Path("D:/tools"))
    assert [s.available for s in statuses] == [True, False, False]
    assert statuses[0].path == "D:/tools/ffmpeg.exe"
    assert "ffprobe" in statuses[1].error
    assert statuses[2].error


def test_refresh_discovers_installed_tools_absent_from_process_path(monkeypatch):
    monkeypatch.setenv("PATH", "old-path")
    monkeypatch.setattr(media_tools, "refreshed_search_path", lambda: "fresh-path")
    monkeypatch.setattr(media_tools, "find_tool", lambda tool, _: (_ for _ in ()).throw(MissingMediaToolError(tool)))
    monkeypatch.setattr(media_tools.shutil, "which", lambda tool, *, path: f"/new/bin/{tool}" if path == "fresh-path" else None)
    monkeypatch.setattr(media_tools.subprocess, "run", lambda command, **_: SimpleNamespace(
        returncode=0, stdout=f"{Path(command[0]).name} version 9.0\n", stderr="",
    ))
    assert all(s.available for s in media_tools.inspect_tools(refresh=True))
    assert media_tools.os.environ["PATH"] == "old-path"
    assert all(s.available for s in media_tools.inspect_tools(refresh=True, activate=True))
    assert media_tools.os.environ["PATH"] == "fresh-path"


def test_repeated_path_refresh_does_not_accumulate_duplicates():
    sep = media_tools.os.pathsep
    first = media_tools._unique_search_path([f"/one{sep}/two", f"/two{sep}/three"])
    assert first == sep.join(("/one", "/two", "/three"))
    assert media_tools._unique_search_path([first, f"/two{sep}/three"]) == first


@pytest.mark.parametrize("platform", ["win32", "darwin"])
def test_installer_uses_exact_platform_package(monkeypatch, platform):
    monkeypatch.setattr(media_tools.sys, "platform", platform)
    monkeypatch.setattr(media_tools, "refreshed_search_path", lambda: "new-path")
    monkeypatch.setattr(media_tools.shutil, "which", lambda tool, **_: f"/tools/{tool}")
    command = media_tools.installation_command()
    if platform == "win32":
        assert command[1:7] == ["install", "--id", "Gyan.FFmpeg", "--exact", "--source", "winget"]
    else:
        assert command == ["/tools/brew", "install", "ffmpeg"]


@pytest.mark.parametrize("platform,hint", [("win32", "应用安装程序"), ("darwin", "Homebrew")])
def test_missing_package_manager_has_manual_recovery(monkeypatch, platform, hint):
    monkeypatch.setattr(media_tools.sys, "platform", platform)
    monkeypatch.setattr(media_tools, "refreshed_search_path", lambda: "")
    monkeypatch.setattr(media_tools.shutil, "which", lambda *_, **__: None)
    with pytest.raises(ProcessingError, match=hint):
        media_tools.installation_command()


@pytest.mark.parametrize("code,ready", [(0, True), (0, False), (1, True)])
def test_installation_requires_exit_success_and_working_tools(monkeypatch, code, ready):
    monkeypatch.setattr(media_tools, "installation_command", lambda: ["fake-installer"])
    class Process:
        stdout = io.StringIO("downloading\ninstalling\n")
        def wait(self): return code
        def poll(self): return code

    monkeypatch.setattr(media_tools.subprocess, "Popen", lambda *_, **__: Process())
    statuses = tuple(media_tools.ToolStatus(t, f"/tools/{t}", "9.0") if ready else media_tools.ToolStatus(t, error="missing")
                     for t in media_tools.TOOLS)
    monkeypatch.setattr(media_tools, "inspect_tools", lambda **_: statuses)
    logs = []
    if code == 0 and ready:
        assert media_tools.install_ffmpeg(logs.append) == statuses
    else:
        with pytest.raises(ProcessingError):
            media_tools.install_ffmpeg(logs.append)
    assert "downloading" in logs and "installing" in logs


def test_cli_missing_tool_remains_a_typed_error_without_a_gui(monkeypatch):
    from krok_helper import ffmpeg

    monkeypatch.setattr(ffmpeg.shutil, "which", lambda *_, **__: None)
    monkeypatch.setattr(ffmpeg, "macos_shell_path", lambda: None)
    with pytest.raises(MissingMediaToolError) as failure:
        ffmpeg.find_tool("ffprobe")
    assert failure.value.tool == "ffprobe"
    assert "全局设置 → 工具 → FFmpeg" in str(failure.value)
