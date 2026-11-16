"""FFmpeg diagnostics and package-manager installation, independent of Qt."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Callable
import weakref

from krok_helper.errors import MissingMediaToolError, ProcessingError
from krok_helper.ffmpeg import _build_subprocess_kwargs, find_tool
from krok_helper.macos_environment import macos_shell_path

TOOLS = ("ffmpeg", "ffprobe", "ffplay")
_reporter: weakref.WeakMethod | None = None


def register_missing_tool_reporter(callback: Callable[[str], None]) -> None:
    global _reporter
    _reporter = weakref.WeakMethod(callback)


def report_missing_tool(error: object) -> bool:
    """Publish an optional GUI notification without importing Qt in workers/CLI."""
    if not missing_tool(error):
        return False
    callback = _reporter() if _reporter is not None else None
    if callback is None:
        return False
    try:
        callback(str(error))
    except RuntimeError:
        return False  # The host QObject has already been destroyed.
    return True


@dataclass(frozen=True)
class ToolStatus:
    tool: str
    path: str = ""
    version: str = ""
    error: str = ""

    @property
    def available(self) -> bool:
        return bool(self.path and self.version and not self.error)


def missing_tool(error: object) -> str | None:
    """Adapt legacy worker strings and third-party errors at the UI boundary.

    Require the missing phrase next to the executable name; a corrupt input,
    missing output file, codec error or an FFmpeg nonzero exit is not missing
    software. New resolvers carry the tool explicitly in a typed exception.
    """
    if isinstance(error, MissingMediaToolError):
        return error.tool if error.tool in TOOLS else None
    text = str(error)
    patterns = (
        r"所选\s*(ffmpeg)\s*目录无效",
        r"(?:找不到|未找到|未检测到|无法找到)\s*(ffmpeg|ffprobe|ffplay)(?:\.exe)?(?=[\s，。,:：]|$)",
        r"\b(ffmpeg|ffprobe|ffplay)(?:\.exe)?\s+(?:and\s+ffprobe\s+)?(?:(?:was|were|is|are)\s+)?(?:not found|not installed)\b",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            # '找不到 ffmpeg 提取的音频文件' describes an output, not a tool.
            suffix = text[match.end():].lstrip()
            if suffix.startswith(("提取", "生成", "输出")):
                continue
            return match.group(1).lower()
    return None


def refreshed_search_path() -> str:
    """Include newly installed commands without rewriting the system PATH."""
    entries = [os.environ.get("PATH", "")]
    if sys.platform == "win32":
        import winreg

        for root, key in (
            (winreg.HKEY_CURRENT_USER, r"Environment"),
            (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
        ):
            try:
                with winreg.OpenKey(root, key) as handle:
                    entries.append(os.path.expandvars(winreg.QueryValueEx(handle, "Path")[0]))
            except OSError:
                pass
        for variable in ("LOCALAPPDATA", "PROGRAMFILES"):
            if os.environ.get(variable):
                entries.append(str(Path(os.environ[variable]) / "Microsoft" / "WinGet" / "Links"))
    elif sys.platform == "darwin":
        macos_shell_path.cache_clear()
        entries.extend([macos_shell_path() or "", "/opt/homebrew/bin", "/usr/local/bin"])
    return _unique_search_path(entries)


def _unique_search_path(entries: list[str]) -> str:
    directories = []
    seen = set()
    for entry in entries:
        for directory in entry.split(os.pathsep):
            directory = os.path.expandvars(directory.strip().strip('"'))
            if not directory:
                continue
            key = os.path.normcase(os.path.normpath(directory))
            if key not in seen:
                seen.add(key)
                directories.append(directory)
    return os.pathsep.join(directories)


def inspect_tools(
    directory: Path | None = None, *, refresh: bool = False, activate: bool = False,
) -> tuple[ToolStatus, ...]:
    search_path = refreshed_search_path() if refresh or activate else None
    statuses = []
    for tool in TOOLS:
        path = ""
        try:
            try:
                path = find_tool(tool, directory)
            except MissingMediaToolError:
                path = shutil.which(tool, path=search_path) if search_path else None
                if not path:
                    raise MissingMediaToolError(tool)
            result = subprocess.run(
                [path, "-version"], capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=5, check=False, **_build_subprocess_kwargs(),
            )
            first_line = (result.stdout or result.stderr).splitlines()
            if result.returncode or not first_line or not first_line[0].lower().startswith(f"{tool} version "):
                raise ProcessingError("程序无法正常运行，请检查所选目录或重新安装。")
            statuses.append(ToolStatus(tool, str(path), first_line[0].split(" Copyright", 1)[0]))
        except subprocess.TimeoutExpired:
            statuses.append(ToolStatus(tool, str(path or ""), error="检测超时，请检查程序是否被阻止运行，或重新安装。"))
        except (OSError, subprocess.SubprocessError, ProcessingError) as exc:
            statuses.append(ToolStatus(tool, str(path or ""), error=str(exc)))
    if activate and search_path and all(s.available for s in statuses if s.tool in ("ffmpeg", "ffprobe")):
        # A package manager updates the system PATH, but this already-running
        # process keeps its old environment. Child tools and a mixed existing
        # ffmpeg/newly installed ffprobe must see the same verified search path.
        os.environ["PATH"] = search_path
    return tuple(statuses)


def installation_command() -> list[str]:
    if sys.platform == "win32":
        winget = shutil.which("winget") or shutil.which("winget", path=refreshed_search_path())
        if not winget:
            raise ProcessingError("未检测到 WinGet，请先安装或更新「应用安装程序」，也可以手动下载 FFmpeg 后选择目录。")
        return [winget, "install", "--id", "Gyan.FFmpeg", "--exact", "--source", "winget",
                "--accept-package-agreements", "--accept-source-agreements", "--disable-interactivity"]
    if sys.platform == "darwin":
        brew = shutil.which("brew", path=refreshed_search_path())
        if not brew:
            raise ProcessingError("未检测到 Homebrew，请先安装 Homebrew，也可以手动安装 FFmpeg 后选择目录。")
        return [brew, "install", "ffmpeg"]
    raise ProcessingError("当前平台请通过系统包管理器安装 FFmpeg，再选择目录或重新检测。")


def install_ffmpeg(logger: Callable[[str], None]) -> tuple[ToolStatus, ...]:
    command = installation_command()
    logger("正在安装 FFmpeg 工具包…")
    process = subprocess.Popen(
        command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", **_build_subprocess_kwargs(),
    )
    try:
        assert process.stdout is not None
        for line in process.stdout:
            if line.strip():
                logger(line.strip())
        code = process.wait()
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait()
        if process.stdout is not None:
            process.stdout.close()
    if code != 0:
        raise ProcessingError(f"FFmpeg 安装未完成（退出码 {code}），请查看安装日志；可重新检测或手动选择目录。")
    statuses = inspect_tools(refresh=True, activate=True)
    if not all(item.available for item in statuses if item.tool != "ffplay"):
        raise ProcessingError("安装命令已结束，但尚未检测到可用的 FFmpeg 和 FFprobe。请重新检测或选择安装目录。")
    return statuses
