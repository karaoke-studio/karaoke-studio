import os
from pathlib import Path

import pytest

from krok_helper import ffmpeg as ffmpeg_mod
from krok_helper.ffmpeg import find_tool


def test_find_tool_prefers_configured_directory(monkeypatch, tmp_path):
    configured_dir = tmp_path / "configured"
    path_dir = tmp_path / "path"
    configured_dir.mkdir()
    path_dir.mkdir()

    suffix = ".exe" if os.name == "nt" else ""
    configured_tool = configured_dir / f"ffmpeg{suffix}"
    path_tool = path_dir / f"ffmpeg{suffix}"
    configured_tool.write_text("", encoding="utf-8")
    path_tool.write_text("", encoding="utf-8")
    configured_tool.chmod(0o755)
    path_tool.chmod(0o755)
    monkeypatch.setenv("PATH", str(path_dir))

    assert find_tool("ffmpeg", configured_dir) == str(configured_tool)


def test_find_tool_accepts_windows_executable_name_in_posix_directory(monkeypatch, tmp_path):
    monkeypatch.setattr(ffmpeg_mod.os, "name", "posix")
    install_dir = tmp_path / "ffmpeg-install"
    tool = install_dir / "bin" / "ffmpeg"
    tool.parent.mkdir(parents=True)
    tool.write_text("", encoding="utf-8")
    tool.chmod(0o755)

    assert find_tool("ffmpeg.exe", install_dir) == str(tool)


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable permissions")
def test_find_tool_skips_a_non_executable_configured_file(monkeypatch, tmp_path):
    configured = tmp_path / "工具" / "bin"
    configured.mkdir(parents=True)
    blocked = configured / "ffmpeg"
    blocked.write_text("", encoding="utf-8")
    blocked.chmod(0o644)
    path_dir = tmp_path / "実行"
    path_dir.mkdir()
    executable = path_dir / "ffmpeg"
    executable.write_text("", encoding="utf-8")
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(path_dir))

    assert find_tool("ffmpeg.exe", configured.parent) == str(executable)


@pytest.mark.parametrize("inherited_windows_path", [False, True])
def test_find_tool_accepts_windows_executable_name_on_posix_path(monkeypatch, inherited_windows_path):
    monkeypatch.setattr(ffmpeg_mod.os, "name", "posix")
    tools = {"ffmpeg": "/usr/local/bin/ffmpeg"}
    if inherited_windows_path:
        tools["ffmpeg.exe"] = "/mnt/c/tools/ffmpeg.exe"
    monkeypatch.setattr(ffmpeg_mod.shutil, "which", tools.get)

    assert find_tool("ffmpeg.exe") == "/usr/local/bin/ffmpeg"


@pytest.mark.parametrize("tool_name", ["ffmpeg.exe", "ffprobe.exe", "ffplay.exe"])
def test_find_tool_resolves_every_module_tool_from_install_root(tmp_path, tool_name):
    install_dir = tmp_path / "ffmpeg-install"
    suffix = ".exe" if os.name == "nt" else ""
    tool = install_dir / "bin" / f"{Path(tool_name).stem}{suffix}"
    tool.parent.mkdir(parents=True, exist_ok=True)
    tool.write_text("", encoding="utf-8")
    tool.chmod(0o755)

    assert find_tool(tool_name, install_dir) == str(tool)


@pytest.mark.parametrize("tool_name", ["ffmpeg", "ffprobe", "ffplay"])
def test_find_tool_resolves_every_module_tool_from_system_path(monkeypatch, tmp_path, tool_name):
    path_dir = tmp_path / "path"
    path_dir.mkdir()
    suffix = ".exe" if os.name == "nt" else ""
    tool = path_dir / f"{tool_name}{suffix}"
    tool.write_text("", encoding="utf-8")
    tool.chmod(0o755)
    monkeypatch.setenv("PATH", str(path_dir))

    assert find_tool(f"{tool_name}.exe", None) == str(tool)
