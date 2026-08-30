"""点名占用更新目录的进程（Windows Restart Manager）。

更新器在目录改名被 WinError 5 拒绝、重试耗尽后调用，把「拒绝访问」翻译成
可直接操作的进程清单。结构化接口区分发现占用、未发现占用与诊断失败；兼容
接口仍在失败时返回空描述。诊断失败绝不影响更新主流程。
"""

from __future__ import annotations

import ctypes
import os
import time
import uuid
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
_DIRECTORY_HANDLE_SCAN_LIMIT = 250000
_DIRECTORY_HANDLE_SCAN_SECONDS = 2.0
_SYSTEM_EXTENDED_HANDLE_INFORMATION = 64
_STATUS_INFO_LENGTH_MISMATCH = 0xC0000004


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


class _SYSTEM_HANDLE_TABLE_ENTRY_INFO_EX(ctypes.Structure):
    _fields_ = [
        ("Object", ctypes.c_void_p),
        ("UniqueProcessId", ctypes.c_size_t),
        ("HandleValue", ctypes.c_size_t),
        ("GrantedAccess", wintypes.ULONG),
        ("CreatorBackTraceIndex", wintypes.USHORT),
        ("ObjectTypeIndex", wintypes.USHORT),
        ("HandleAttributes", wintypes.ULONG),
        ("Reserved", wintypes.ULONG),
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


def process_image_path(pid: int) -> str:
    """Return a process executable path without requiring debug privileges."""

    if os.name != "nt" or pid <= 0:
        return ""
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.restype = wintypes.HANDLE
        handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return ""
        try:
            buffer = ctypes.create_unicode_buffer(32768)
            size = wintypes.DWORD(len(buffer))
            if kernel32.QueryFullProcessImageNameW(
                handle, 0, buffer, ctypes.byref(size)
            ):
                return buffer.value
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        pass
    return ""


def _process_created_ticks(pid: int) -> int | None:
    """Return the Windows process creation FILETIME, used to spot PID reuse."""

    if os.name != "nt" or pid <= 0:
        return None
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.restype = wintypes.HANDLE
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        try:
            created = _FILETIME()
            exited = _FILETIME()
            kernel = _FILETIME()
            user = _FILETIME()
            if not kernel32.GetProcessTimes(
                handle,
                ctypes.byref(created),
                ctypes.byref(exited),
                ctypes.byref(kernel),
                ctypes.byref(user),
            ):
                return None
            return (int(created.dwHighDateTime) << 32) | int(created.dwLowDateTime)
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return None


def process_snapshot_details(
    snapshot: Iterable[ProcessTreeEntry],
    *,
    include_pids: Iterable[int] = (),
    app_dir: object | None = None,
    image_names: Iterable[str] = (),
) -> list[dict[str, Any]]:
    """Enrich relevant process rows for diagnostics, including non-descendants."""

    explicit = {int(pid) for pid in include_pids if int(pid) > 0}
    wanted_names = {str(name).casefold() for name in image_names if name}
    normalized_app = ""
    if app_dir:
        normalized_app = os.path.normcase(os.path.abspath(os.fspath(app_dir)))
    result: list[dict[str, Any]] = []
    for entry in snapshot:
        image_path = process_image_path(entry.pid)
        normalized_image = (
            os.path.normcase(os.path.abspath(image_path)) if image_path else ""
        )
        under_app_dir = bool(
            normalized_app
            and normalized_image
            and (
                normalized_image == normalized_app
                or normalized_image.startswith(normalized_app + os.sep)
            )
        )
        if (
            entry.pid not in explicit
            and entry.image_name.casefold() not in wanted_names
            and not under_app_dir
        ):
            continue
        result.append(
            {
                "pid": entry.pid,
                "parent_pid": entry.parent_pid,
                "image_name": entry.image_name,
                "image_path": image_path,
                "under_app_dir": under_app_dir,
                "created_filetime": _process_created_ticks(entry.pid),
            }
        )
    return result


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
    # Missing destinations such as ``_internal.old`` are expected during a
    # rename and say nothing about how completely the existing source was
    # inspected.  Keep them visible without poisoning source coverage.
    source_scan_complete = (
        len(explicit_files) <= _SAMPLE_FILE_LIMIT
        and all(item.get("complete", False) for item in per_directory)
    )
    coverage = {
        "input_path_count": input_count,
        "explicit_file_count": len(explicit_files),
        "directory_count": len(directories),
        "missing_paths": missing,
        "registered_file_count": len(registered),
        "sample_limit": _SAMPLE_FILE_LIMIT,
        "source_scan_complete": source_scan_complete,
        "complete": source_scan_complete,
        "truncated": not source_scan_complete,
        "directories": per_directory,
        "directories_registered_directly": False,
        "directory_handles_covered": False,
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


def _normalize_final_handle_path(path: str) -> str:
    if path.startswith("\\\\?\\UNC\\"):
        path = "\\\\" + path[8:]
    elif path.startswith("\\\\?\\"):
        path = path[4:]
    return os.path.normcase(os.path.normpath(path))


def diagnose_directory_handles(
    paths: Iterable[object],
    *,
    handle_limit: int = _DIRECTORY_HANDLE_SCAN_LIMIT,
    time_limit: float = _DIRECTORY_HANDLE_SCAN_SECONDS,
) -> dict[str, Any]:
    """Best-effort scan for open directory handles missed by Restart Manager.

    Restart Manager accepts files but not directory resources.  This bounded
    native scan duplicates accessible disk handles and resolves their final
    paths.  Only handles matching the failed source tree are returned.
    """

    started = time.monotonic()
    targets = []
    for raw in paths:
        if raw and os.path.isdir(os.fspath(raw)):
            normalized = _normalize_final_handle_path(os.path.abspath(os.fspath(raw)))
            if normalized not in targets:
                targets.append(normalized)
    result: dict[str, Any] = {
        "status": "unsupported" if os.name != "nt" else "none",
        "target_directories": targets,
        "entries": [],
        "scanned_handle_count": 0,
        "duplicated_disk_handle_count": 0,
        "inaccessible_process_count": 0,
        "handle_limit": int(handle_limit),
        "time_limit_seconds": float(time_limit),
        "truncated": False,
        "complete": False,
    }
    if os.name != "nt" or not targets:
        result["reason"] = "non_windows" if os.name != "nt" else "no_existing_directory"
        result["duration_ms"] = round((time.monotonic() - started) * 1000, 3)
        return result

    process_handles: dict[int, int | None] = {}
    current_process = None
    buffer = None
    try:
        kernel32 = ctypes.windll.kernel32
        ntdll = ctypes.windll.ntdll
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.GetFileType.argtypes = [wintypes.HANDLE]
        kernel32.GetFileType.restype = wintypes.DWORD
        kernel32.DuplicateHandle.argtypes = [
            wintypes.HANDLE,
            wintypes.HANDLE,
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.HANDLE),
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        kernel32.DuplicateHandle.restype = wintypes.BOOL
        kernel32.GetFinalPathNameByHandleW.argtypes = [
            wintypes.HANDLE,
            wintypes.LPWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
        ]
        kernel32.GetFinalPathNameByHandleW.restype = wintypes.DWORD
        ntdll.NtQuerySystemInformation.argtypes = [
            wintypes.ULONG,
            ctypes.c_void_p,
            wintypes.ULONG,
            ctypes.POINTER(wintypes.ULONG),
        ]
        ntdll.NtQuerySystemInformation.restype = wintypes.LONG

        size = 1 << 20
        returned = wintypes.ULONG(0)
        status = _STATUS_INFO_LENGTH_MISMATCH
        while size <= 64 << 20:
            buffer = ctypes.create_string_buffer(size)
            status = int(
                ntdll.NtQuerySystemInformation(
                    _SYSTEM_EXTENDED_HANDLE_INFORMATION,
                    buffer,
                    size,
                    ctypes.byref(returned),
                )
            ) & 0xFFFFFFFF
            if status != _STATUS_INFO_LENGTH_MISMATCH:
                break
            size = max(size * 2, int(returned.value) + 65536)
        if status != 0 or buffer is None:
            result.update({"status": "failed", "stage": "query_handles", "ntstatus": status})
            return result

        count = int(ctypes.c_size_t.from_buffer(buffer, 0).value)
        entry_size = ctypes.sizeof(_SYSTEM_HANDLE_TABLE_ENTRY_INFO_EX)
        offset = ctypes.sizeof(ctypes.c_size_t) * 2
        available = max(0, (len(buffer) - offset) // entry_size)
        count = min(count, available)
        result["system_handle_count"] = count
        current_process = kernel32.GetCurrentProcess()
        inaccessible: set[int] = set()
        seen_matches: set[tuple[int, str]] = set()

        for index in range(count):
            if result["scanned_handle_count"] >= handle_limit:
                result["truncated"] = True
                result["truncation_reason"] = "handle_limit"
                break
            if time.monotonic() - started >= time_limit:
                result["truncated"] = True
                result["truncation_reason"] = "time_limit"
                break
            entry = _SYSTEM_HANDLE_TABLE_ENTRY_INFO_EX.from_buffer(
                buffer, offset + index * entry_size
            )
            result["scanned_handle_count"] += 1
            pid = int(entry.UniqueProcessId)
            if pid <= 4:
                continue
            if pid not in process_handles:
                handle = kernel32.OpenProcess(0x0040 | 0x1000, False, pid)
                process_handles[pid] = int(handle) if handle else None
                if not handle:
                    inaccessible.add(pid)
            process_handle = process_handles[pid]
            if not process_handle:
                continue

            duplicate = wintypes.HANDLE()
            if not kernel32.DuplicateHandle(
                wintypes.HANDLE(process_handle),
                wintypes.HANDLE(entry.HandleValue),
                current_process,
                ctypes.byref(duplicate),
                0,
                False,
                0x00000002,  # DUPLICATE_SAME_ACCESS
            ):
                continue
            try:
                if kernel32.GetFileType(duplicate) != 0x0001:  # FILE_TYPE_DISK
                    continue
                path_buffer = ctypes.create_unicode_buffer(32768)
                length = int(
                    kernel32.GetFinalPathNameByHandleW(
                        duplicate, path_buffer, len(path_buffer), 0
                    )
                )
                if length <= 0 or length >= len(path_buffer):
                    continue
                resolved = _normalize_final_handle_path(path_buffer.value)
                result["duplicated_disk_handle_count"] += 1
                if not any(
                    resolved == target or resolved.startswith(target + os.sep)
                    for target in targets
                ):
                    continue
                match_key = (pid, resolved)
                if match_key in seen_matches:
                    continue
                seen_matches.add(match_key)
                image_path = process_image_path(pid)
                result["entries"].append(
                    {
                        "pid": pid,
                        "process_name": os.path.basename(image_path) or process_image_name(pid),
                        "process_path": image_path,
                        "handle_path": path_buffer.value,
                        "is_directory": os.path.isdir(path_buffer.value),
                        "granted_access": f"0x{int(entry.GrantedAccess):08X}",
                    }
                )
            finally:
                kernel32.CloseHandle(duplicate)

        result["inaccessible_process_count"] = len(inaccessible)
        result["complete"] = not result["truncated"] and not inaccessible
        result["status"] = "found" if result["entries"] else "none"
        return result
    except Exception as exc:
        result.update(
            {
                "status": "failed",
                "stage": "exception",
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        return result
    finally:
        try:
            kernel32 = ctypes.windll.kernel32
            for handle in process_handles.values():
                if handle:
                    kernel32.CloseHandle(wintypes.HANDLE(handle))
        except Exception:
            pass
        result["duration_ms"] = round((time.monotonic() - started) * 1000, 3)


def _windows_identity() -> dict[str, Any]:
    result: dict[str, Any] = {
        "available": False,
        "sid": "",
        "elevated": None,
        "integrity_rid": None,
    }
    if os.name != "nt":
        result["reason"] = "non_windows"
        return result
    token = wintypes.HANDLE()
    sid_text = ctypes.c_wchar_p()
    try:
        kernel32 = ctypes.windll.kernel32
        advapi32 = ctypes.windll.advapi32
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        advapi32.OpenProcessToken.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.HANDLE),
        ]
        advapi32.OpenProcessToken.restype = wintypes.BOOL
        advapi32.GetTokenInformation.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        advapi32.GetTokenInformation.restype = wintypes.BOOL
        advapi32.ConvertSidToStringSidW.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_wchar_p),
        ]
        advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
        if not advapi32.OpenProcessToken(
            kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)
        ):
            result["win32_error"] = int(kernel32.GetLastError())
            return result

        needed = wintypes.DWORD(0)
        advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
        user_buffer = ctypes.create_string_buffer(max(1, int(needed.value)))
        if advapi32.GetTokenInformation(
            token, 1, user_buffer, len(user_buffer), ctypes.byref(needed)
        ):
            sid_pointer = ctypes.c_void_p.from_buffer(user_buffer).value
            if sid_pointer and advapi32.ConvertSidToStringSidW(
                ctypes.c_void_p(sid_pointer), ctypes.byref(sid_text)
            ):
                result["sid"] = sid_text.value or ""

        elevation = wintypes.DWORD(0)
        needed = wintypes.DWORD(0)
        if advapi32.GetTokenInformation(
            token,
            20,  # TokenElevation
            ctypes.byref(elevation),
            ctypes.sizeof(elevation),
            ctypes.byref(needed),
        ):
            result["elevated"] = bool(elevation.value)
        needed = wintypes.DWORD(0)
        advapi32.GetTokenInformation(token, 25, None, 0, ctypes.byref(needed))
        integrity_buffer = ctypes.create_string_buffer(max(1, int(needed.value)))
        if advapi32.GetTokenInformation(
            token,
            25,  # TokenIntegrityLevel
            integrity_buffer,
            len(integrity_buffer),
            ctypes.byref(needed),
        ):
            integrity_sid = ctypes.c_void_p.from_buffer(integrity_buffer).value
            if integrity_sid:
                advapi32.GetSidSubAuthorityCount.argtypes = [ctypes.c_void_p]
                advapi32.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
                advapi32.GetSidSubAuthority.argtypes = [ctypes.c_void_p, wintypes.DWORD]
                advapi32.GetSidSubAuthority.restype = ctypes.POINTER(wintypes.DWORD)
                count_pointer = advapi32.GetSidSubAuthorityCount(integrity_sid)
                if count_pointer and count_pointer.contents.value:
                    rid_pointer = advapi32.GetSidSubAuthority(
                        integrity_sid, count_pointer.contents.value - 1
                    )
                    if rid_pointer:
                        result["integrity_rid"] = int(rid_pointer.contents.value)
        result["available"] = bool(result["sid"])
        return result
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result
    finally:
        try:
            if sid_text:
                kernel32 = ctypes.windll.kernel32
                kernel32.LocalFree.argtypes = [ctypes.c_void_p]
                kernel32.LocalFree.restype = ctypes.c_void_p
                kernel32.LocalFree(ctypes.cast(sid_text, ctypes.c_void_p))
            if token:
                ctypes.windll.kernel32.CloseHandle(token)
        except Exception:
            pass


def _open_for_delete_probe(path: Path) -> dict[str, Any]:
    if os.name != "nt":
        return {"available": False, "reason": "non_windows"}
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateFileW.restype = wintypes.HANDLE
        handle = kernel32.CreateFileW(
            str(path),
            0x00010000 | 0x00000080,  # DELETE | FILE_READ_ATTRIBUTES
            0x00000001 | 0x00000002 | 0x00000004,  # share read/write/delete
            None,
            3,  # OPEN_EXISTING
            0x02000000,  # FILE_FLAG_BACKUP_SEMANTICS
            None,
        )
        invalid = ctypes.c_void_p(-1).value
        if not handle or int(handle) == invalid:
            error = int(kernel32.GetLastError())
            return {
                "available": True,
                "opened": False,
                "win32_error": error,
                "classification": (
                    "sharing_violation" if error in (32, 33) else
                    "access_denied" if error == 5 else "other_error"
                ),
            }
        kernel32.CloseHandle(handle)
        return {"available": True, "opened": True, "win32_error": 0}
    except Exception as exc:
        return {"available": False, "error": f"{type(exc).__name__}: {exc}"}


def _parent_rename_probe(parent: Path) -> dict[str, Any]:
    source = parent / f".krok-update-probe-{os.getpid()}-{uuid.uuid4().hex}"
    destination = source.with_name(source.name + ".renamed")
    stage = "create"
    try:
        source.mkdir()
        stage = "rename"
        os.replace(source, destination)
        stage = "delete"
        destination.rmdir()
        return {"succeeded": True}
    except OSError as exc:
        return {
            "succeeded": False,
            "stage": stage,
            "exception_type": type(exc).__name__,
            "errno": getattr(exc, "errno", None),
            "winerror": getattr(exc, "winerror", None),
            "message": str(exc),
        }
    finally:
        for candidate in (destination, source):
            try:
                candidate.rmdir()
            except OSError:
                pass


def diagnose_path_access(exc: BaseException) -> dict[str, Any]:
    """Separate parent mutation permission from a lock on the source directory."""

    raw_source = getattr(exc, "filename", None)
    source = Path(raw_source) if raw_source else None
    result: dict[str, Any] = {
        "identity": _windows_identity(),
        "source": str(source) if source is not None else "",
        "source_delete_open": (
            _open_for_delete_probe(source)
            if source is not None and os.path.lexists(source)
            else {"available": False, "reason": "source_missing"}
        ),
        "parent_rename_probe": {"available": False, "reason": "source_missing"},
        "classification": "unresolved",
    }
    if source is not None and source.parent.is_dir():
        probe = _parent_rename_probe(source.parent)
        probe["available"] = True
        result["parent_rename_probe"] = probe
        delete_probe = result["source_delete_open"]
        if not probe.get("succeeded"):
            result["classification"] = "parent_mutation_denied"
        elif delete_probe.get("classification") == "sharing_violation":
            result["classification"] = "source_handle_conflict"
        elif delete_probe.get("classification") == "access_denied":
            result["classification"] = "source_delete_access_denied_or_directory_in_use"
        elif delete_probe.get("opened"):
            result["classification"] = "rename_denied_despite_delete_access"
    return result


def describe_lockers_for_exception(exc: BaseException) -> str:
    return format_lockers(find_lockers_for_exception(exc))


def process_image_name(pid: int) -> str:
    """查询进程的可执行文件名（basename）；失败或非 Windows 返回空串。

    Restart Manager 给出的是本地化友好名（如「Windows 资源管理器」），不能
    直接用于进程名单匹配，必须回查真实的镜像名。
    """
    return os.path.basename(process_image_path(pid))


def kill_pid(pid: int, *, wait_timeout_ms: int = 5000) -> bool:
    """结束指定进程并等待其退出（更新失败恢复路径专用）。"""
    if os.name != "nt" or pid <= 0 or pid == os.getpid():
        return False
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.restype = wintypes.HANDLE
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
