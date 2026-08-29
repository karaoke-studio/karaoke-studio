"""点名占用更新目录的进程（Windows Restart Manager）。

更新器在目录改名被 WinError 5 拒绝、重试耗尽后调用，把「拒绝访问」翻译成
可直接操作的进程清单。结构化接口区分发现占用、未发现占用与诊断失败；兼容
接口仍在失败时返回空描述。诊断失败绝不影响更新主流程。
"""

from __future__ import annotations

import ctypes
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, List, Tuple

from ctypes import wintypes

_CCH_RM_SESSION_KEY = 32
_CCH_RM_MAX_APP_NAME = 255
_CCH_RM_MAX_SVC_NAME = 63
# RmGetList 需要两阶段调用：先探测所需数组大小（返回 ERROR_MORE_DATA），再取结果。
_ERROR_MORE_DATA = 234
# 目录树过大时只注册有限样本；exe/dll/pyd 优先（最常被长期占用的类型）。
_SAMPLE_FILE_LIMIT = 400
_SCAN_FILE_LIMIT = 4000
_SCAN_TIME_LIMIT_SECONDS = 1.0
_BINARY_SUFFIXES = {".exe", ".dll", ".pyd"}


@dataclass(frozen=True)
class ProcessTreeEntry:
    """One process-table row used to contain updater handoff descendants."""

    pid: int
    parent_pid: int
    image_name: str


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


def snapshot_processes() -> List[ProcessTreeEntry]:
    """Return a best-effort Windows process snapshot without extra dependencies."""

    if os.name != "nt":
        return []
    kernel32 = ctypes.windll.kernel32
    create_snapshot = kernel32.CreateToolhelp32Snapshot
    create_snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    create_snapshot.restype = wintypes.HANDLE
    process_first = kernel32.Process32FirstW
    process_first.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    process_first.restype = wintypes.BOOL
    process_next = kernel32.Process32NextW
    process_next.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    process_next.restype = wintypes.BOOL

    handle = create_snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
    invalid_handle = ctypes.c_void_p(-1).value
    if not handle or int(handle) == invalid_handle:
        return []
    rows: List[ProcessTreeEntry] = []
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        ok = bool(process_first(handle, ctypes.byref(entry)))
        while ok:
            rows.append(
                ProcessTreeEntry(
                    pid=int(entry.th32ProcessID),
                    parent_pid=int(entry.th32ParentProcessID),
                    image_name=str(entry.szExeFile),
                )
            )
            entry.dwSize = ctypes.sizeof(entry)
            ok = bool(process_next(handle, ctypes.byref(entry)))
    except Exception:
        return []
    finally:
        kernel32.CloseHandle(handle)
    return rows


def process_lineage(pid: int, snapshot: Iterable[ProcessTreeEntry]) -> set[int]:
    """Return ``pid`` and every discoverable ancestor in one snapshot."""

    by_pid = {entry.pid: entry for entry in snapshot}
    lineage: set[int] = set()
    current = int(pid)
    while current > 0 and current not in lineage:
        lineage.add(current)
        entry = by_pid.get(current)
        if entry is None:
            break
        current = entry.parent_pid
    return lineage


def descendant_processes(
    root_pid: int,
    snapshot: Iterable[ProcessTreeEntry],
    *,
    exclude_pids: Iterable[int] = (),
) -> List[ProcessTreeEntry]:
    """Return descendants in parent-first order, excluding updater lineage."""

    excluded = {int(pid) for pid in exclude_pids}
    children: dict[int, List[ProcessTreeEntry]] = {}
    for entry in snapshot:
        children.setdefault(entry.parent_pid, []).append(entry)
    found: List[ProcessTreeEntry] = []
    pending = [int(root_pid)]
    visited = {int(root_pid)}
    while pending:
        parent = pending.pop(0)
        for child in children.get(parent, []):
            if child.pid in visited:
                continue
            visited.add(child.pid)
            pending.append(child.pid)
            if child.pid not in excluded:
                found.append(child)
    return found


@dataclass
class RestartManagerResult:
    """One explicit Restart Manager outcome, including diagnostic coverage."""

    status: str  # found / none / failed
    entries: List[Tuple[int, str]] = field(default_factory=list)
    stage: str = ""
    win32_error: int | None = None
    registered_paths: List[str] = field(default_factory=list)
    coverage: dict[str, Any] = field(default_factory=dict)
    reboot_reasons: int = 0
    end_session_error: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "entries": [
                {"pid": pid, "application_name": name}
                for pid, name in self.entries
            ],
            "stage": self.stage,
            "win32_error": self.win32_error,
            "registered_paths": list(self.registered_paths),
            "coverage": dict(self.coverage),
            "reboot_reasons": self.reboot_reasons,
            "end_session_error": self.end_session_error,
        }


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


def _rm_lockers(paths: List[str]) -> RestartManagerResult:
    """Query one Restart Manager session and preserve the exact API outcome."""
    if os.name != "nt" or not paths:
        return RestartManagerResult(
            status="failed",
            stage="platform" if os.name != "nt" else "no_resources",
            registered_paths=list(paths),
        )
    result = RestartManagerResult(
        status="failed",
        stage="start_session",
        registered_paths=list(paths),
    )
    try:
        rstrtmgr = ctypes.windll.rstrtmgr
        session = ctypes.c_uint32(0)
        session_key = ctypes.create_unicode_buffer(_CCH_RM_SESSION_KEY + 1)
        rc = int(rstrtmgr.RmStartSession(ctypes.byref(session), 0, session_key))
        if rc != 0:
            result.win32_error = rc
            return result
        try:
            path_array = (ctypes.c_wchar_p * len(paths))(*paths)
            result.stage = "register_resources"
            rc = int(
                rstrtmgr.RmRegisterResources(
                    session, len(paths), path_array, 0, None, 0, None
                )
            )
            if rc != 0:
                result.win32_error = rc
                return result
            needed = ctypes.c_uint32(0)
            count = ctypes.c_uint32(0)
            reboot = ctypes.c_uint32(0)
            result.stage = "get_list_probe"
            rc = int(
                rstrtmgr.RmGetList(
                    session,
                    ctypes.byref(needed),
                    ctypes.byref(count),
                    None,
                    ctypes.byref(reboot),
                )
            )
            result.reboot_reasons = int(reboot.value)
            if rc == 0:
                result.status = "none"
                result.stage = "complete"
                result.win32_error = None
                return result
            if rc != _ERROR_MORE_DATA:
                result.win32_error = rc
                return result
            infos = (_RM_PROCESS_INFO * needed.value)()
            count = ctypes.c_uint32(needed.value)
            result.stage = "get_list_data"
            rc = int(
                rstrtmgr.RmGetList(
                    session,
                    ctypes.byref(needed),
                    ctypes.byref(count),
                    infos,
                    ctypes.byref(reboot),
                )
            )
            result.reboot_reasons = int(reboot.value)
            if rc != 0:
                result.win32_error = rc
                return result
            result.entries = [
                (int(infos[index].Process.dwProcessId), str(infos[index].strAppName))
                for index in range(count.value)
            ]
            result.status = "found" if result.entries else "none"
            result.stage = "complete"
            result.win32_error = None
            return result
        finally:
            end_rc = int(rstrtmgr.RmEndSession(session))
            if end_rc != 0:
                result.end_session_error = end_rc
    except Exception as exc:
        result.stage = "exception"
        result.coverage["exception"] = f"{type(exc).__name__}: {exc}"
        return result


def _sample_files(root: Path, limit: int = _SAMPLE_FILE_LIMIT) -> List[Path]:
    """Compatibility wrapper returning only bounded file samples."""
    return _sample_files_with_coverage(root, limit)[0]


def _sample_files_with_coverage(
    root: Path,
    limit: int = _SAMPLE_FILE_LIMIT,
) -> tuple[List[Path], dict[str, Any]]:
    """Collect bounded real-file samples without registering a directory in RM."""
    binaries: List[Path] = []
    others: List[Path] = []
    discovered = 0
    complete = True
    errors: list[str] = []
    deadline = time.monotonic() + _SCAN_TIME_LIMIT_SECONDS
    try:
        for current, dirs, files in os.walk(root, followlinks=False):
            # Do not traverse junctions/reparse points while diagnosing a failure.
            safe_dirs = []
            for name in dirs:
                candidate = Path(current) / name
                try:
                    attrs = int(getattr(os.lstat(candidate), "st_file_attributes", 0))
                    if attrs & 0x0400:
                        continue
                    safe_dirs.append(name)
                except OSError as exc:
                    errors.append(f"{candidate}: {type(exc).__name__}: {exc}")
            dirs[:] = safe_dirs
            for name in files:
                path = Path(current) / name
                discovered += 1
                if path.suffix.lower() in _BINARY_SUFFIXES:
                    if len(binaries) < limit:
                        binaries.append(path)
                elif len(others) < limit:
                    others.append(path)
                if discovered >= _SCAN_FILE_LIMIT or time.monotonic() >= deadline:
                    complete = False
                    break
            if not complete:
                break
    except OSError as exc:
        complete = False
        errors.append(f"{root}: {type(exc).__name__}: {exc}")
    if len(binaries) < limit:
        binaries += others[: limit - len(binaries)]
    samples = binaries[:limit]
    if discovered > len(samples):
        complete = False
    return samples, {
        "root": str(root),
        "discovered_file_count": discovered,
        "registered_file_count": len(samples),
        "complete": complete,
        "truncated": not complete,
        "errors": errors,
    }


def _normalize_rm_result(
    raw: object,
    registered_paths: List[str],
) -> RestartManagerResult:
    """Accept legacy private-test doubles while the public result stays structured."""
    if isinstance(raw, RestartManagerResult):
        return raw
    if raw is None:
        return RestartManagerResult(
            status="failed",
            stage="legacy_unavailable",
            registered_paths=registered_paths,
        )
    entries = list(raw)  # type: ignore[arg-type]
    return RestartManagerResult(
        status="found" if entries else "none",
        entries=entries,
        stage="complete",
        registered_paths=registered_paths,
    )


def diagnose_lockers(paths: Iterable[object]) -> RestartManagerResult:
    """Diagnose files represented by paths, never registering directories directly."""
    explicit_files: List[str] = []
    directories: List[Path] = []
    missing: List[str] = []
    input_count = 0
    for raw in paths:
        if not raw:
            continue
        input_count += 1
        text = os.fspath(raw)
        if os.path.isdir(text):
            directories.append(Path(text))
        elif os.path.isfile(text) or os.path.lexists(text):
            explicit_files.append(text)
        else:
            missing.append(text)

    registered = list(dict.fromkeys(explicit_files))
    per_directory: list[dict[str, Any]] = []
    for directory in directories:
        remaining = max(0, _SAMPLE_FILE_LIMIT - len(registered))
        if remaining == 0:
            per_directory.append(
                {
                    "root": str(directory),
                    "discovered_file_count": 0,
                    "registered_file_count": 0,
                    "complete": False,
                    "truncated": True,
                    "errors": ["global sample limit reached"],
                }
            )
            continue
        samples, coverage = _sample_files_with_coverage(directory, remaining)
        per_directory.append(coverage)
        for sample in samples:
            text = str(sample)
            if text not in registered:
                registered.append(text)

    if len(registered) > _SAMPLE_FILE_LIMIT:
        registered = registered[:_SAMPLE_FILE_LIMIT]
    complete = (
        len(explicit_files) <= _SAMPLE_FILE_LIMIT
        and not missing
        and all(item.get("complete", False) for item in per_directory)
    )
    coverage = {
        "input_path_count": input_count,
        "explicit_file_count": len(explicit_files),
        "directory_count": len(directories),
        "missing_paths": missing,
        "registered_file_count": len(registered),
        "sample_limit": _SAMPLE_FILE_LIMIT,
        "complete": complete,
        "truncated": not complete,
        "directories": per_directory,
        "directories_registered_directly": False,
    }

    if not registered:
        return RestartManagerResult(
            status="failed",
            stage="no_registerable_files",
            coverage=coverage,
        )

    result = _normalize_rm_result(_rm_lockers(registered), registered)
    result.coverage.update(coverage)

    own_pid = os.getpid()
    seen: set[int] = set()
    filtered: List[Tuple[int, str]] = []
    for pid, name in result.entries:
        if pid == own_pid or pid in seen or not name:
            continue
        seen.add(pid)
        filtered.append((pid, name))
    result.entries = filtered
    if result.status != "failed":
        result.status = "found" if filtered else "none"
    return result


def find_lockers(paths: Iterable[object]) -> List[Tuple[int, str]]:
    """Compatibility helper returning only the deduplicated process entries."""
    return diagnose_lockers(paths).entries


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


def diagnose_lockers_for_exception(exc: BaseException) -> RestartManagerResult:
    """Return a tri-state diagnosis for ``filename`` / ``filename2``."""
    candidates = [
        getattr(exc, "filename", None),
        getattr(exc, "filename2", None),
    ]
    return diagnose_lockers([item for item in candidates if item])


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
