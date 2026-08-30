"""Best-effort forensic bundles for updater replacement failures.

This module is deliberately isolated from update decisions.  Every public
function swallows diagnostic failures so a broken collector can never turn a
successful update into a failed one.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import platform
import re
import shutil
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


_BUNDLE_SCHEMA_VERSION = 2
_BUNDLE_KEEP_COUNT = 5
_BUNDLE_NAME_RE = re.compile(
    r"^\d{8}T\d{6}\.\d{3}[+-]\d{4}-pid\d+(?:-\d+)?$"
)
_SENSITIVE_QUERY_RE = re.compile(
    r"(?i)([?&](?:access_?token|auth|authorization|client_?secret|credential|"
    r"key|password|secret|signature|sig|token|x-amz-(?:credential|signature|"
    r"security-token)|x-goog-(?:credential|signature))=)[^&#\s]+"
)
_URL_CREDENTIAL_RE = re.compile(r"(?i)(https?://)[^/@\s]+@")
_FILE_ATTRIBUTE_NAMES = {
    0x0001: "readonly",
    0x0002: "hidden",
    0x0004: "system",
    0x0010: "directory",
    0x0020: "archive",
    0x0400: "reparse_point",
    0x0800: "compressed",
    0x1000: "offline",
    0x2000: "not_content_indexed",
    0x4000: "encrypted",
}


def _wall_time() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def _redaction_roots() -> list[tuple[str, str]]:
    values = [
        (os.environ.get("LOCALAPPDATA", ""), "<LOCALAPPDATA>"),
        (os.environ.get("USERPROFILE", ""), "<USERPROFILE>"),
        (tempfile.gettempdir(), "<TEMP>"),
    ]
    unique: dict[str, str] = {}
    for raw, replacement in values:
        if raw:
            unique[os.path.normpath(raw)] = replacement
    return sorted(unique.items(), key=lambda item: len(item[0]), reverse=True)


def redact_text(value: object) -> str:
    """Redact user-profile roots and common URL credentials/secrets."""
    text = str(value)
    for raw, replacement in _redaction_roots():
        text = re.sub(re.escape(raw), replacement, text, flags=re.IGNORECASE)
    text = _URL_CREDENTIAL_RE.sub(r"\1<redacted>@", text)
    text = _SENSITIVE_QUERY_RE.sub(r"\1<redacted>", text)
    return text


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {str(key): _redact_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_value(item) for item in value]
    return value


def exception_details(exc: BaseException | None) -> dict[str, Any] | None:
    if exc is None:
        return None
    result: dict[str, Any] = {
        "type": type(exc).__name__,
        "message": redact_text(exc),
    }
    for name in ("errno", "winerror", "filename", "filename2"):
        value = getattr(exc, name, None)
        if value is not None:
            result[name] = redact_text(value) if name.startswith("filename") else value
    return result


def _windows_file_attributes(path: Path) -> dict[str, Any]:
    if os.name != "nt":
        return {"available": False, "reason": "non_windows"}
    try:
        attrs = int(ctypes.windll.kernel32.GetFileAttributesW(str(path))) & 0xFFFFFFFF
        if attrs == 0xFFFFFFFF:
            return {
                "available": False,
                "win32_error": int(ctypes.windll.kernel32.GetLastError()),
            }
        return {
            "available": True,
            "value": attrs,
            "hex": f"0x{attrs:08X}",
            "flags": [name for bit, name in _FILE_ATTRIBUTE_NAMES.items() if attrs & bit],
        }
    except Exception as exc:
        return {"available": False, "error": exception_details(exc)}


def _windows_sddl(path: Path) -> dict[str, Any]:
    """Read owner/group/DACL as SDDL without requesting privileged SACL data."""
    if os.name != "nt":
        return {"available": False, "reason": "non_windows"}
    security_descriptor = ctypes.c_void_p()
    owner = ctypes.c_void_p()
    group = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
    string_descriptor = ctypes.c_wchar_p()
    try:
        advapi32 = ctypes.windll.advapi32
        kernel32 = ctypes.windll.kernel32
        # OWNER_SECURITY_INFORMATION | GROUP_SECURITY_INFORMATION |
        # DACL_SECURITY_INFORMATION
        security_info = 0x00000001 | 0x00000002 | 0x00000004
        rc = int(
            advapi32.GetNamedSecurityInfoW(
                str(path),
                1,  # SE_FILE_OBJECT
                security_info,
                ctypes.byref(owner),
                ctypes.byref(group),
                ctypes.byref(dacl),
                None,
                ctypes.byref(security_descriptor),
            )
        )
        if rc != 0:
            return {"available": False, "win32_error": rc}
        length = ctypes.c_uint32(0)
        ok = advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW(
            security_descriptor,
            1,
            security_info,
            ctypes.byref(string_descriptor),
            ctypes.byref(length),
        )
        if not ok:
            return {
                "available": False,
                "win32_error": int(kernel32.GetLastError()),
            }
        return {"available": True, "sddl": string_descriptor.value or ""}
    except Exception as exc:
        return {"available": False, "error": exception_details(exc)}
    finally:
        try:
            if string_descriptor:
                ctypes.windll.kernel32.LocalFree(string_descriptor)
            if security_descriptor:
                ctypes.windll.kernel32.LocalFree(security_descriptor)
        except Exception:
            pass


def snapshot_path(path: object, role: str) -> dict[str, Any]:
    raw = os.fspath(path)
    target = Path(raw)
    entry: dict[str, Any] = {
        "role": role,
        "path": redact_text(os.path.abspath(raw)),
        "lexists": os.path.lexists(raw),
    }
    try:
        stat_result = os.lstat(raw)
        attrs = int(getattr(stat_result, "st_file_attributes", 0))
        if os.path.islink(raw):
            kind = "symlink"
        elif target.is_dir():
            kind = "directory"
        elif target.is_file():
            kind = "file"
        else:
            kind = "other"
        entry.update(
            {
                "kind": kind,
                "is_reparse_point": bool(attrs & 0x0400),
                "mode": f"0o{stat_result.st_mode:o}",
                "size": stat_result.st_size,
                "created_ns": getattr(stat_result, "st_ctime_ns", None),
                "modified_ns": getattr(stat_result, "st_mtime_ns", None),
                "accessed_ns": getattr(stat_result, "st_atime_ns", None),
            }
        )
    except OSError as exc:
        entry["stat_error"] = exception_details(exc)
    entry["windows_attributes"] = _windows_file_attributes(target)
    entry["security_descriptor"] = _windows_sddl(target) if entry["lexists"] else {
        "available": False,
        "reason": "path_missing",
    }
    return entry


def snapshot_failure_paths(
    exc: BaseException | None,
    extra_paths: Iterable[object] = (),
) -> list[dict[str, Any]]:
    candidates: list[tuple[str, object]] = []
    if exc is not None:
        for name in ("filename", "filename2"):
            value = getattr(exc, name, None)
            if value:
                candidates.append((name, value))
    for index, path in enumerate(extra_paths):
        if path:
            candidates.append((f"extra_{index}", path))

    expanded: list[tuple[str, object]] = []
    seen: set[str] = set()
    for role, path in candidates:
        raw = os.path.abspath(os.fspath(path))
        normalized = os.path.normcase(os.path.normpath(raw))
        if normalized not in seen:
            seen.add(normalized)
            expanded.append((role, raw))
        parent = os.path.dirname(raw)
        parent_key = os.path.normcase(os.path.normpath(parent))
        if parent and parent_key not in seen:
            seen.add(parent_key)
            expanded.append((f"{role}_parent", parent))

    result: list[dict[str, Any]] = []
    for role, path in expanded:
        try:
            result.append(snapshot_path(path, role))
        except Exception as snapshot_exc:
            result.append(
                {
                    "role": role,
                    "path": redact_text(path),
                    "snapshot_error": exception_details(snapshot_exc),
                }
            )
    return result


@dataclass
class _Session:
    started_at: str
    started_monotonic_ns: int
    root: Path
    log_path: Path
    metadata: dict[str, Any]
    attempts: list[dict[str, Any]] = field(default_factory=list)
    restart_manager: list[dict[str, Any]] = field(default_factory=list)
    process_cleanup: list[dict[str, Any]] = field(default_factory=list)
    access_diagnostics: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    filesystem: list[dict[str, Any]] = field(default_factory=list)
    failures: list[dict[str, Any]] = field(default_factory=list)
    collection_errors: list[dict[str, Any]] = field(default_factory=list)
    bundle_dir: Path | None = None
    live_event_path: Path | None = None
    final_exit_code: int | None = None


_state_lock = threading.RLock()
_active: _Session | None = None
_latest_bundle_path: Path | None = None


def _default_root() -> Path:
    local = os.environ.get("LOCALAPPDATA", "").strip()
    if local:
        return Path(local) / "KaraokeStudioUpdater" / "diagnostics"
    return Path(tempfile.gettempdir()) / "KaraokeStudioUpdater" / "diagnostics"


def _file_fingerprint(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {"path": str(path), "exists": path.is_file()}
    if not result["exists"]:
        return result
    try:
        stat_result = path.stat()
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        result.update(
            {
                "size": stat_result.st_size,
                "modified_ns": stat_result.st_mtime_ns,
                "sha256": digest.hexdigest(),
            }
        )
    except OSError as exc:
        result["error"] = exception_details(exc)
    return result


def _filesystem_info(path: object) -> dict[str, Any]:
    result: dict[str, Any] = {"available": False}
    if os.name != "nt" or not path:
        result["reason"] = "non_windows" if os.name != "nt" else "missing_path"
        return result
    try:
        kernel32 = ctypes.windll.kernel32
        absolute = os.path.abspath(os.fspath(path))
        volume_path = ctypes.create_unicode_buffer(32768)
        if not kernel32.GetVolumePathNameW(absolute, volume_path, len(volume_path)):
            result["win32_error"] = int(kernel32.GetLastError())
            return result
        volume_name = ctypes.create_unicode_buffer(261)
        filesystem_name = ctypes.create_unicode_buffer(261)
        serial = ctypes.c_uint32(0)
        max_component = ctypes.c_uint32(0)
        flags = ctypes.c_uint32(0)
        if not kernel32.GetVolumeInformationW(
            volume_path.value,
            volume_name,
            len(volume_name),
            ctypes.byref(serial),
            ctypes.byref(max_component),
            ctypes.byref(flags),
            filesystem_name,
            len(filesystem_name),
        ):
            result["win32_error"] = int(kernel32.GetLastError())
            return result
        return {
            "available": True,
            "volume_path": volume_path.value,
            "filesystem": filesystem_name.value,
            "drive_type": int(kernel32.GetDriveTypeW(volume_path.value)),
            "volume_serial": f"{serial.value:08X}",
            "filesystem_flags": f"0x{flags.value:08X}",
            "max_component_length": int(max_component.value),
        }
    except Exception as exc:
        result["error"] = exception_details(exc)
        return result


def _append_event_locked(
    session: _Session,
    event: str,
    details: dict[str, Any] | None = None,
) -> None:
    payload = {
        "wall_time": _wall_time(),
        "monotonic_ms": _elapsed_ms(session),
        "event": event,
        "details": _redact_value(details or {}),
    }
    session.events.append(payload)
    if session.live_event_path is None:
        return
    try:
        session.live_event_path.parent.mkdir(parents=True, exist_ok=True)
        with session.live_event_path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(payload, ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        session.collection_errors.append(
            {"stage": "append_live_event", "error": exception_details(exc)}
        )
        session.live_event_path = None


def record_event(event: str, details: dict[str, Any] | None = None) -> None:
    """Append one crash-resilient structured event, best effort."""

    try:
        with _state_lock:
            if _active is not None:
                _append_event_locked(_active, event, details)
    except Exception:
        pass


def begin_session(
    args: object,
    *,
    root: Path | None = None,
    log_path: Path | None = None,
) -> None:
    """Start an in-memory collection session; no diagnostic files are created yet."""
    global _active, _latest_bundle_path
    try:
        work_dir = Path(tempfile.gettempdir()) / "KaraokeStudioUpdater"
        try:
            from krok_helper.config import APP_VERSION
        except Exception:
            APP_VERSION = "unknown"
        executable = Path(sys.executable)
        metadata = {
            "updater_pid": os.getpid(),
            "parent_pid": getattr(args, "pid", None),
            "source_version": os.environ.get("KROK_UPDATE_SOURCE_VERSION", ""),
            "target_version": getattr(args, "target_version", ""),
            "target_tag": getattr(args, "target_tag", ""),
            "app_dir": os.fspath(getattr(args, "app_dir", "")),
            "app_filesystem": _filesystem_info(getattr(args, "app_dir", "")),
            "app_exe": getattr(args, "app_exe", ""),
            "internal_name": getattr(args, "internal_name", ""),
            "updater_product_version": APP_VERSION,
            "updater_bootstrap_result": os.environ.get(
                "KROK_UPDATE_BOOTSTRAP_RESULT", "unknown"
            ),
            "updater_executable": _file_fingerprint(executable),
            "updater_cwd": os.getcwd(),
            "updater_argv": list(sys.argv),
            "platform": platform.platform(),
            "python": platform.python_version(),
            "frozen": bool(getattr(sys, "frozen", False)),
        }
        with _state_lock:
            previous_live = _active.live_event_path if _active is not None else None
            if previous_live is not None:
                try:
                    previous_live.unlink(missing_ok=True)
                except OSError:
                    pass
            session_id = f"pid{os.getpid()}-{uuid.uuid4().hex}"
            _active = _Session(
                started_at=_wall_time(),
                started_monotonic_ns=time.monotonic_ns(),
                root=Path(root) if root is not None else _default_root(),
                log_path=Path(log_path) if log_path is not None else work_dir / "updater.log",
                metadata=metadata,
                live_event_path=(
                    Path(tempfile.gettempdir())
                    / "KaraokeStudioUpdaterLive"
                    / f"{session_id}.jsonl"
                ),
            )
            _latest_bundle_path = None
            _append_event_locked(_active, "session_started", metadata)
    except Exception:
        # Diagnostics may never block the updater from starting.
        with _state_lock:
            _active = None
            _latest_bundle_path = None


def _elapsed_ms(session: _Session, monotonic_ns: int | None = None) -> float:
    now = time.monotonic_ns() if monotonic_ns is None else monotonic_ns
    return round((now - session.started_monotonic_ns) / 1_000_000, 3)


def record_attempt(
    op_desc: str,
    *,
    phase: str,
    attempt: int,
    max_attempts: int,
    started_ns: int,
    outcome: str,
    exc: BaseException | None = None,
) -> None:
    try:
        finished_ns = time.monotonic_ns()
        with _state_lock:
            if _active is None:
                return
            detail = {
                "wall_time": _wall_time(),
                "monotonic_ms": _elapsed_ms(_active, finished_ns),
                "duration_ms": round((finished_ns - started_ns) / 1_000_000, 3),
                "operation": op_desc,
                "phase": phase,
                "attempt": attempt,
                "max_attempts": max_attempts,
                "outcome": outcome,
                "exception": exception_details(exc),
            }
            _active.attempts.append(detail)
            _append_event_locked(_active, "file_operation_attempt", detail)
    except Exception:
        pass


def record_restart_manager(
    op_desc: str,
    result: object,
    *,
    started_ns: int,
) -> None:
    try:
        finished_ns = time.monotonic_ns()
        if hasattr(result, "as_dict"):
            detail = result.as_dict()
        elif isinstance(result, dict):
            detail = dict(result)
        else:
            detail = {"status": "failed", "stage": "invalid_result"}
        detail.update(
            {
                "wall_time": _wall_time(),
                "operation": op_desc,
                "duration_ms": round((finished_ns - started_ns) / 1_000_000, 3),
            }
        )
        with _state_lock:
            if _active is not None:
                detail["monotonic_ms"] = _elapsed_ms(_active, finished_ns)
                _active.restart_manager.append(detail)
                _append_event_locked(_active, "restart_manager_query", detail)
    except Exception:
        pass


def record_process_cleanup(detail: dict[str, Any]) -> None:
    """Record updater handoff descendants and their cleanup outcome."""

    try:
        with _state_lock:
            if _active is not None:
                payload = _redact_value(dict(detail))
                payload["wall_time"] = _wall_time()
                payload["monotonic_ms"] = _elapsed_ms(_active)
                _active.process_cleanup.append(payload)
                _append_event_locked(_active, "process_cleanup", payload)
    except Exception:
        pass


def record_access_diagnostic(op_desc: str, detail: dict[str, Any]) -> None:
    """Record directory-handle, process, identity and mutation probes."""

    try:
        with _state_lock:
            if _active is not None:
                payload = _redact_value(dict(detail))
                payload["operation"] = op_desc
                payload["wall_time"] = _wall_time()
                payload["monotonic_ms"] = _elapsed_ms(_active)
                _active.access_diagnostics.append(payload)
                _append_event_locked(_active, "access_diagnostic", payload)
    except Exception:
        pass


def _create_bundle_dir(session: _Session) -> Path:
    roots = [session.root]
    fallback = Path(tempfile.gettempdir()) / "KaraokeStudioUpdater" / "diagnostics"
    if os.path.normcase(str(fallback)) != os.path.normcase(str(session.root)):
        roots.append(fallback)
    last_exc: BaseException | None = None
    stamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S.%f")[:-3]
    offset = datetime.now().astimezone().strftime("%z")
    base_name = f"{stamp}{offset}-pid{os.getpid()}"
    for root in roots:
        try:
            root.mkdir(parents=True, exist_ok=True)
            for suffix in range(0, 100):
                name = base_name if suffix == 0 else f"{base_name}-{suffix}"
                candidate = root / name
                try:
                    candidate.mkdir(exist_ok=False)
                    session.root = root
                    return candidate
                except FileExistsError:
                    continue
        except OSError as exc:
            last_exc = exc
    raise OSError(f"无法创建诊断目录: {last_exc}")


def _atomic_write_text(path: Path, content: str) -> None:
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp_path.write_text(content, encoding="utf-8")
        os.replace(temp_path, path)
    finally:
        try:
            if temp_path.exists():
                temp_path.unlink()
        except OSError:
            pass


def _atomic_write_json(path: Path, value: Any) -> None:
    content = json.dumps(_redact_value(value), ensure_ascii=False, indent=2) + "\n"
    _atomic_write_text(path, content)


def _copy_redacted_log(session: _Session) -> None:
    text = ""
    destination = session.bundle_dir / "updater.log"
    try:
        if session.log_path.is_file():
            text = session.log_path.read_text(encoding="utf-8", errors="replace")
        elif destination.is_file():
            # Full fallback success cleans the TEMP work directory before the
            # session is finalized. Preserve the earlier failure-time copy.
            return
    except OSError as exc:
        session.collection_errors.append(
            {"stage": "read_updater_log", "error": exception_details(exc)}
        )
    _atomic_write_text(destination, redact_text(text))


def _report(session: _Session) -> dict[str, Any]:
    if session.final_exit_code is None:
        outcome = "captured_before_update_finished"
    elif session.final_exit_code == 0:
        outcome = "recovered"
    else:
        outcome = "failed"
    return {
        "schema_version": _BUNDLE_SCHEMA_VERSION,
        "created_at": session.started_at,
        "updated_at": _wall_time(),
        "outcome": outcome,
        "final_exit_code": session.final_exit_code,
        "session": session.metadata,
        "counts": {
            "attempts": len(session.attempts),
            "restart_manager_queries": len(session.restart_manager),
            "process_cleanup_events": len(session.process_cleanup),
            "access_diagnostics": len(session.access_diagnostics),
            "structured_events": len(session.events),
            "filesystem_captures": len(session.filesystem),
            "failures": len(session.failures),
        },
        "process_cleanup": session.process_cleanup,
        "access_diagnostics": session.access_diagnostics,
        "failures": session.failures,
        "collection_errors": session.collection_errors,
        "privacy": "不包含文件内容；用户目录、临时目录和常见 URL 密钥已脱敏。",
    }


def _is_reparse_directory(path: Path) -> bool:
    try:
        stat_result = os.lstat(path)
        return bool(int(getattr(stat_result, "st_file_attributes", 0)) & 0x0400)
    except OSError:
        return True


def _prune_old_bundles(root: Path, keep: int = _BUNDLE_KEEP_COUNT) -> None:
    try:
        candidates = sorted(
            (
                item
                for item in root.iterdir()
                if item.is_dir()
                and _BUNDLE_NAME_RE.fullmatch(item.name)
                and (item / "report.json").is_file()
            ),
            key=lambda item: item.name,
            reverse=True,
        )
        for stale in candidates[keep:]:
            # Never recurse through a link/junction planted inside the diagnostics root.
            if stale.is_symlink() or _is_reparse_directory(stale):
                continue
            shutil.rmtree(stale, ignore_errors=False)
    except OSError:
        pass


def _write_bundle(session: _Session) -> Path:
    global _latest_bundle_path
    if session.bundle_dir is None:
        session.bundle_dir = _create_bundle_dir(session)
    _atomic_write_json(session.bundle_dir / "attempts.json", session.attempts)
    _atomic_write_json(
        session.bundle_dir / "restart-manager.json", session.restart_manager
    )
    _atomic_write_json(
        session.bundle_dir / "access-diagnostics.json", session.access_diagnostics
    )
    _atomic_write_json(session.bundle_dir / "filesystem.json", session.filesystem)
    event_text = "".join(
        json.dumps(_redact_value(item), ensure_ascii=False) + "\n"
        for item in session.events
    )
    _atomic_write_text(session.bundle_dir / "events.jsonl", event_text)
    _copy_redacted_log(session)
    _atomic_write_json(session.bundle_dir / "report.json", _report(session))
    _latest_bundle_path = session.bundle_dir
    _prune_old_bundles(session.root)
    return session.bundle_dir


def persist_failure(
    reason: str,
    *,
    exc: BaseException | None = None,
    extra_paths: Iterable[object] = (),
    details: dict[str, Any] | None = None,
) -> Path | None:
    """Capture the current failure and persist/update the bundle, best effort."""
    try:
        snapshots = snapshot_failure_paths(exc, extra_paths)
        with _state_lock:
            if _active is None:
                return None
            failure = {
                "wall_time": _wall_time(),
                "monotonic_ms": _elapsed_ms(_active),
                "reason": reason,
                "exception": exception_details(exc),
                "details": details or {},
            }
            _active.failures.append(failure)
            _append_event_locked(_active, "failure", failure)
            _active.filesystem.append(
                {
                    "wall_time": failure["wall_time"],
                    "reason": reason,
                    "entries": snapshots,
                }
            )
            return _write_bundle(_active)
    except Exception as persist_exc:
        try:
            with _state_lock:
                if _active is not None:
                    _active.collection_errors.append(
                        {"stage": "persist_failure", "error": exception_details(persist_exc)}
                    )
        except Exception:
            pass
        return None


def finish_session(code: int, *, exc: BaseException | None = None) -> Path | None:
    """Finalize a failed/recovered session; successful quiet sessions write nothing."""
    global _active
    session: _Session | None = None
    try:
        with _state_lock:
            if _active is None:
                return _latest_bundle_path
            session = _active
            _active.final_exit_code = int(code)
            _append_event_locked(
                _active,
                "session_finished",
                {"exit_code": int(code), "exception": exception_details(exc)},
            )
            needs_bundle = code != 0 or _active.bundle_dir is not None
        if code != 0:
            persist_failure(
                "updater_exit",
                exc=exc,
                details={"exit_code": int(code)},
            )
        elif needs_bundle:
            with _state_lock:
                if _active is not None:
                    return _write_bundle(_active)
        return latest_bundle_path()
    except Exception:
        return latest_bundle_path()
    finally:
        if session is not None:
            with _state_lock:
                if _active is session:
                    _active = None
            try:
                if session.live_event_path is not None:
                    session.live_event_path.unlink(missing_ok=True)
                    try:
                        session.live_event_path.parent.rmdir()
                    except OSError:
                        pass
            except OSError:
                pass


def latest_bundle_path() -> Path | None:
    with _state_lock:
        return _latest_bundle_path
