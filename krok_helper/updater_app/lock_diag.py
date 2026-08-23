"""点名占用更新目录的进程（Windows Restart Manager）。

更新器在目录改名被 WinError 5 拒绝、重试耗尽后调用，把「拒绝访问」翻译成
可直接操作的进程清单。诊断路径上的任何失败都安静降级（返回空描述），
绝不影响更新主流程。
"""

from __future__ import annotations

import ctypes
import os
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

_CCH_RM_SESSION_KEY = 32
_CCH_RM_MAX_APP_NAME = 255
_CCH_RM_MAX_SVC_NAME = 63
# RmGetList 需要两阶段调用：先探测所需数组大小（返回 ERROR_MORE_DATA），再取结果。
_ERROR_MORE_DATA = 234
# 目录树过大时只注册有限样本；exe/dll/pyd 优先（最常被长期占用的类型）。
_SAMPLE_FILE_LIMIT = 400
_BINARY_SUFFIXES = {".exe", ".dll", ".pyd"}


class _FILETIME(ctypes.Structure):
    _fields_ = [
        ("dwLowDateTime", ctypes.c_uint32),
        ("dwHighDateTime", ctypes.c_uint32),
    ]


class _RM_UNIQUE_PROCESS(ctypes.Structure):
    _fields_ = [
        ("dwProcessId", ctypes.c_uint32),
        ("ProcessStartTime", _FILETIME),
    ]


class _RM_PROCESS_INFO(ctypes.Structure):
    _fields_ = [
        ("Process", _RM_UNIQUE_PROCESS),
        ("strAppName", ctypes.c_wchar * (_CCH_RM_MAX_APP_NAME + 1)),
        ("strServiceShortName", ctypes.c_wchar * (_CCH_RM_MAX_SVC_NAME + 1)),
        ("ApplicationType", ctypes.c_uint32),
        ("AppStatus", ctypes.c_uint32),
        ("TSSessionId", ctypes.c_uint32),
        ("bRestartable", ctypes.c_int),
    ]


def _rm_lockers(paths: List[str]) -> Optional[List[Tuple[int, str]]]:
    """一次 Restart Manager 会话查询。

    返回 ``(pid, 应用名)`` 列表；``[]`` 表示注册的资源当前无进程占用，
    ``None`` 表示 API 调用失败（调用方需换一种资源组合重试或放弃）。
    """
    if os.name != "nt" or not paths:
        return None
    try:
        rstrtmgr = ctypes.windll.rstrtmgr
        session = ctypes.c_uint32(0)
        session_key = ctypes.create_unicode_buffer(_CCH_RM_SESSION_KEY + 1)
        if rstrtmgr.RmStartSession(ctypes.byref(session), 0, session_key) != 0:
            return None
        try:
            path_array = (ctypes.c_wchar_p * len(paths))(*paths)
            if rstrtmgr.RmRegisterResources(
                session, len(paths), path_array, 0, None, 0, None
            ) != 0:
                return None
            needed = ctypes.c_uint32(0)
            count = ctypes.c_uint32(0)
            reboot = ctypes.c_uint32(0)
            rc = rstrtmgr.RmGetList(
                session,
                ctypes.byref(needed),
                ctypes.byref(count),
                None,
                ctypes.byref(reboot),
            )
            if rc == 0:
                return []
            if rc != _ERROR_MORE_DATA:
                return None
            infos = (_RM_PROCESS_INFO * needed.value)()
            count = ctypes.c_uint32(needed.value)
            rc = rstrtmgr.RmGetList(
                session,
                ctypes.byref(needed),
                ctypes.byref(count),
                infos,
                ctypes.byref(reboot),
            )
            if rc != 0:
                return None
            return [
                (int(infos[index].Process.dwProcessId), str(infos[index].strAppName))
                for index in range(count.value)
            ]
        finally:
            rstrtmgr.RmEndSession(session)
    except Exception:
        return None


def _sample_files(root: Path, limit: int = _SAMPLE_FILE_LIMIT) -> List[Path]:
    """收集目录树内的文件样本，exe/dll/pyd 优先，总量不超过 ``limit``。"""
    binaries: List[Path] = []
    others: List[Path] = []
    try:
        for current, _dirs, files in os.walk(root):
            for name in files:
                path = Path(current) / name
                if path.suffix.lower() in _BINARY_SUFFIXES:
                    if len(binaries) < limit:
                        binaries.append(path)
                elif len(others) < limit:
                    others.append(path)
            if len(binaries) >= limit and len(others) >= limit:
                break
    except OSError:
        pass
    if len(binaries) < limit:
        binaries += others[: limit - len(binaries)]
    return binaries


def find_lockers(paths: Iterable[object]) -> List[Tuple[int, str]]:
    """返回去重后的 ``(pid, 友好名)`` 占用进程列表；无占用/不可用时为空。

    目录与文件都接受：先直接注册目录本身（捕获停在目录里的资源管理器等
    目录句柄持有者），查不到再用树内文件样本查询（捕获打开着树内文件的进程）。
    """
    files: List[str] = []
    dirs: List[str] = []
    for raw in paths:
        if not raw:
            continue
        text = os.fspath(raw)
        if os.path.isdir(text):
            dirs.append(text)
        elif os.path.isfile(text):
            files.append(text)
    if not files and not dirs:
        return []

    lockers: Optional[List[Tuple[int, str]]] = None
    if dirs:
        lockers = _rm_lockers(dirs)
    if not lockers:
        samples = list(files)
        for directory in dirs:
            samples.extend(_sample_files(Path(directory)))
        if samples:
            lockers = _rm_lockers([str(path) for path in samples])
    if not lockers:
        return []

    own_pid = os.getpid()
    seen: set[int] = set()
    result: List[Tuple[int, str]] = []
    for pid, name in lockers:
        if pid == own_pid or pid in seen or not name:
            continue
        seen.add(pid)
        result.append((pid, name))
    return result


def format_lockers(entries: List[Tuple[int, str]]) -> str:
    """把 :func:`find_lockers` 的结果格式化为「占用进程：A(PID x)、B(PID y)」。"""
    if not entries:
        return ""
    return f"占用进程：{'、'.join(f'{name}(PID {pid})' for pid, name in entries)}"


def describe_lockers(paths: Iterable[object]) -> str:
    return format_lockers(find_lockers(paths))


def find_lockers_for_exception(exc: BaseException) -> List[Tuple[int, str]]:
    """从 OSError 的 filename / filename2 中提取路径并查询占用进程。"""
    candidates = [
        getattr(exc, "filename", None),
        getattr(exc, "filename2", None),
    ]
    return find_lockers([item for item in candidates if item])


def describe_lockers_for_exception(exc: BaseException) -> str:
    return format_lockers(find_lockers_for_exception(exc))


def process_image_name(pid: int) -> str:
    """查询进程的可执行文件名（basename）；失败或非 Windows 返回空串。

    Restart Manager 给出的是本地化友好名（如「Windows 资源管理器」），不能
    直接用于进程名单匹配，必须回查真实的镜像名。
    """
    if os.name != "nt" or pid <= 0:
        return ""
    try:
        kernel32 = ctypes.windll.kernel32
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return ""
        try:
            buffer = ctypes.create_unicode_buffer(1024)
            size = ctypes.c_uint32(1024)
            if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                return os.path.basename(buffer.value)
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        pass
    return ""


def kill_pid(pid: int, *, wait_timeout_ms: int = 5000) -> bool:
    """结束指定进程并等待其退出（更新失败恢复路径专用）。"""
    if os.name != "nt" or pid <= 0 or pid == os.getpid():
        return False
    try:
        kernel32 = ctypes.windll.kernel32
        PROCESS_TERMINATE = 0x0001
        SYNCHRONIZE = 0x00100000
        handle = kernel32.OpenProcess(PROCESS_TERMINATE | SYNCHRONIZE, False, pid)
        if not handle:
            return False
        try:
            if not kernel32.TerminateProcess(handle, 1):
                return False
            kernel32.WaitForSingleObject(handle, wait_timeout_ms)
            return True
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return False
