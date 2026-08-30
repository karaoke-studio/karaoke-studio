from __future__ import annotations

import ctypes
import json
import logging
import os
import time
from ctypes import wintypes
from pathlib import Path
from types import SimpleNamespace

import pytest

from krok_helper.updater_app import diagnostics, lock_diag
from krok_helper.updater_app import main as workbench_updater


def _args(app_dir: Path) -> SimpleNamespace:
    return SimpleNamespace(
        pid=1234,
        target_version="4.2.6.9",
        target_tag="v4.2.6.9",
        app_dir=app_dir,
        app_exe="Lin-K Lyrics.exe",
        internal_name="_internal",
    )


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_restart_manager_never_registers_directory_and_reports_coverage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "_internal"
    target.mkdir()
    (target / "module.py").write_text("pass", encoding="utf-8")
    (target / "native.dll").write_bytes(b"dll")
    captured: list[str] = []

    def fake_rm(paths: list[str]):
        captured.extend(paths)
        return lock_diag.RestartManagerResult(
            status="none",
            stage="complete",
            registered_paths=list(paths),
        )

    monkeypatch.setattr(lock_diag, "_rm_lockers", fake_rm)

    result = lock_diag.diagnose_lockers([target])

    assert result.status == "none"
    assert captured
    assert all(Path(path).is_file() for path in captured)
    assert str(target) not in captured
    assert result.coverage["directories_registered_directly"] is False
    assert result.coverage["registered_file_count"] == 2
    assert result.coverage["complete"] is True


def test_restart_manager_none_and_failed_are_distinct(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "module.py"
    target.write_text("pass", encoding="utf-8")

    monkeypatch.setattr(
        lock_diag,
        "_rm_lockers",
        lambda paths: lock_diag.RestartManagerResult(
            status="none",
            stage="complete",
            registered_paths=list(paths),
        ),
    )
    assert lock_diag.diagnose_lockers([target]).status == "none"

    monkeypatch.setattr(
        lock_diag,
        "_rm_lockers",
        lambda paths: lock_diag.RestartManagerResult(
            status="failed",
            stage="register_resources",
            win32_error=5,
            registered_paths=list(paths),
        ),
    )
    failed = lock_diag.diagnose_lockers([target])
    assert failed.status == "failed"
    assert failed.stage == "register_resources"
    assert failed.win32_error == 5


def test_restart_manager_coverage_marks_bounded_scan_as_truncated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "_internal"
    target.mkdir()
    (target / "one.dll").write_bytes(b"1")
    monkeypatch.setattr(lock_diag, "_SAMPLE_FILE_LIMIT", 1)
    monkeypatch.setattr(
        lock_diag,
        "_sample_files_with_coverage",
        lambda root, limit: (
            [target / "one.dll"],
            {
                "root": str(root),
                "discovered_file_count": 4000,
                "registered_file_count": 1,
                "complete": False,
                "truncated": True,
                "errors": ["scan limit reached"],
            },
        ),
    )
    monkeypatch.setattr(
        lock_diag,
        "_rm_lockers",
        lambda paths: lock_diag.RestartManagerResult(
            status="none", stage="complete", registered_paths=list(paths)
        ),
    )

    result = lock_diag.diagnose_lockers([target])

    assert result.coverage["complete"] is False
    assert result.coverage["truncated"] is True
    assert result.coverage["registered_file_count"] == 1


def test_process_tree_excludes_updater_lineage_and_keeps_nested_children() -> None:
    rows = [
        lock_diag.ProcessTreeEntry(100, 1, "Lin-K Lyrics.exe"),
        lock_diag.ProcessTreeEntry(110, 100, "Updater.exe"),
        lock_diag.ProcessTreeEntry(111, 110, "Updater.exe"),
        lock_diag.ProcessTreeEntry(120, 100, "python.exe"),
        lock_diag.ProcessTreeEntry(121, 120, "ffmpeg.exe"),
    ]

    lineage = lock_diag.process_lineage(111, rows)
    descendants = lock_diag.descendant_processes(100, rows, exclude_pids=lineage)

    assert lineage == {1, 100, 110, 111}
    assert [(item.pid, item.parent_pid) for item in descendants] == [
        (120, 100),
        (121, 120),
    ]


def test_failure_bundle_contains_all_artifacts_and_redacts_secrets(
    tmp_path: Path,
) -> None:
    app_dir = tmp_path / "install"
    source = app_dir / "_internal"
    destination = app_dir / "_internal.old"
    source.mkdir(parents=True)
    log_path = tmp_path / "updater.log"
    log_path.write_text(
        f"目录={source}\nhttps://user:pass@example.test/a?token=secret-value"
        "&X-Amz-Credential=temporary-credential\n",
        encoding="utf-8",
    )
    root = tmp_path / "diagnostics"
    diagnostics.begin_session(_args(app_dir), root=root, log_path=log_path)

    started_ns = time.monotonic_ns()
    exc = PermissionError(5, "拒绝访问", str(source), 5, str(destination))
    diagnostics.record_attempt(
        "备份 _internal",
        phase="regular",
        attempt=1,
        max_attempts=2,
        started_ns=started_ns,
        outcome="failed",
        exc=exc,
    )
    rm_result = lock_diag.RestartManagerResult(
        status="failed",
        stage="register_resources",
        win32_error=5,
        registered_paths=[str(source / "module.py")],
        coverage={"complete": False, "truncated": True},
    )
    diagnostics.record_restart_manager(
        "备份 _internal",
        rm_result,
        started_ns=time.monotonic_ns(),
    )
    diagnostics.record_process_cleanup(
        {
            "parent_pid": 1234,
            "parent_exited": True,
            "processes": [
                {
                    "pid": 5678,
                    "image_name": "python.exe",
                    "outcome": "terminated",
                }
            ],
        }
    )
    bundle = diagnostics.persist_failure("final_retry_failed", exc=exc)

    assert bundle is not None
    assert {item.name for item in bundle.iterdir()} == {
        "report.json",
        "updater.log",
        "attempts.json",
        "restart-manager.json",
        "access-diagnostics.json",
        "events.jsonl",
        "filesystem.json",
    }
    assert _load(bundle / "attempts.json")[0]["exception"]["winerror"] == 5
    rm_json = _load(bundle / "restart-manager.json")[0]
    assert rm_json["status"] == "failed"
    assert rm_json["stage"] == "register_resources"
    assert rm_json["win32_error"] == 5
    report = _load(bundle / "report.json")
    assert report["counts"]["process_cleanup_events"] == 1
    assert report["process_cleanup"][0]["processes"][0]["outcome"] == "terminated"
    filesystem = _load(bundle / "filesystem.json")[0]["entries"]
    assert {item["role"] for item in filesystem} >= {
        "filename",
        "filename2",
        "filename_parent",
    }
    assert all("security_descriptor" in item for item in filesystem)
    copied_log = (bundle / "updater.log").read_text(encoding="utf-8")
    assert "user:pass" not in copied_log
    assert "secret-value" not in copied_log
    assert "temporary-credential" not in copied_log
    assert "<redacted>" in copied_log
    assert str(tmp_path) not in copied_log
    event_lines = (bundle / "events.jsonl").read_text(encoding="utf-8").splitlines()
    events = [json.loads(line) for line in event_lines]
    assert {item["event"] for item in events} >= {
        "session_started",
        "file_operation_attempt",
        "restart_manager_query",
        "process_cleanup",
        "failure",
    }


def test_missing_rename_destination_does_not_reduce_source_coverage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "module.py").write_text("pass", encoding="utf-8")
    destination = tmp_path / "source.old"
    monkeypatch.setattr(
        lock_diag,
        "_rm_lockers",
        lambda paths: lock_diag.RestartManagerResult(
            status="none", stage="complete", registered_paths=list(paths)
        ),
    )

    result = lock_diag.diagnose_lockers([source, destination])

    assert result.coverage["source_scan_complete"] is True
    assert result.coverage["complete"] is True
    assert result.coverage["missing_paths"] == [str(destination)]
    assert result.coverage["directory_handles_covered"] is False


def test_access_probe_classifies_source_handle_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    destination = tmp_path / "source.old"
    exc = PermissionError(5, "拒绝访问", str(source), 5, str(destination))
    monkeypatch.setattr(lock_diag, "_windows_identity", lambda: {"available": True})
    monkeypatch.setattr(
        lock_diag,
        "_open_for_delete_probe",
        lambda path: {
            "available": True,
            "opened": False,
            "win32_error": 32,
            "classification": "sharing_violation",
        },
    )
    monkeypatch.setattr(
        lock_diag,
        "_parent_rename_probe",
        lambda parent: {"succeeded": True},
    )

    result = lock_diag.diagnose_path_access(exc)

    assert result["classification"] == "source_handle_conflict"


@pytest.mark.skipif(os.name != "nt", reason="Windows handle table only")
def test_directory_handle_scan_finds_open_directory(tmp_path: Path) -> None:
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateFileW.restype = wintypes.HANDLE
    handle = kernel32.CreateFileW(
        str(tmp_path),
        0x00000080,  # FILE_READ_ATTRIBUTES
        0x00000001 | 0x00000002 | 0x00000004,
        None,
        3,
        0x02000000,  # FILE_FLAG_BACKUP_SEMANTICS
        None,
    )
    try:
        result = lock_diag.diagnose_directory_handles(
            [tmp_path], handle_limit=500_000, time_limit=5.0
        )
    finally:
        kernel32.CloseHandle(handle)

    assert result["status"] == "found"
    assert any(
        item["pid"] == os.getpid() and item["is_directory"]
        for item in result["entries"]
    )


def test_bundle_records_recovered_full_fallback(tmp_path: Path) -> None:
    root = tmp_path / "diagnostics"
    log_path = tmp_path / "updater.log"
    log_path.write_text("增量替换失败\n", encoding="utf-8")
    diagnostics.begin_session(
        _args(tmp_path / "app"),
        root=root,
        log_path=log_path,
    )
    first = diagnostics.persist_failure(
        "incremental_update_failed",
        details={"exit_code": 33},
    )
    log_path.unlink()  # mirrors successful full-path TEMP cleanup
    final = diagnostics.finish_session(0)

    assert final == first
    report = _load(final / "report.json")
    assert report["outcome"] == "recovered"
    assert report["final_exit_code"] == 0
    assert (final / "updater.log").read_text(encoding="utf-8") == "增量替换失败\n"


def test_session_metadata_identifies_source_and_actual_updater(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KROK_UPDATE_SOURCE_VERSION", "4.2.7.2")
    monkeypatch.setenv("KROK_UPDATE_BOOTSTRAP_RESULT", "updated_or_current")
    diagnostics.begin_session(_args(tmp_path / "app"), root=tmp_path / "diagnostics")

    bundle = diagnostics.persist_failure("metadata_test")

    assert bundle is not None
    report = _load(bundle / "report.json")
    assert report["schema_version"] == 2
    assert report["session"]["source_version"] == "4.2.7.2"
    assert report["session"]["updater_product_version"]
    assert report["session"]["updater_bootstrap_result"] == "updated_or_current"
    assert len(report["session"]["updater_executable"]["sha256"]) == 64
    assert report["session"]["app_filesystem"]["available"] is True
    assert report["session"]["app_filesystem"]["filesystem"]


def test_bundle_falls_back_to_temp_when_local_root_is_unusable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unusable_root = tmp_path / "not-a-directory"
    unusable_root.write_text("occupied", encoding="utf-8")
    fallback_temp = tmp_path / "fallback-temp"
    fallback_temp.mkdir()
    monkeypatch.setattr(diagnostics.tempfile, "gettempdir", lambda: str(fallback_temp))
    diagnostics.begin_session(_args(tmp_path / "app"), root=unusable_root)

    bundle = diagnostics.persist_failure("fallback_test")

    assert bundle is not None
    assert bundle.parent == fallback_temp / "KaraokeStudioUpdater" / "diagnostics"


def test_bundle_retention_keeps_latest_five(tmp_path: Path) -> None:
    root = tmp_path / "diagnostics"
    for index in range(7):
        diagnostics.begin_session(_args(tmp_path / f"app-{index}"), root=root)
        assert diagnostics.persist_failure(f"failure-{index}") is not None

    bundles = [item for item in root.iterdir() if item.is_dir()]
    assert len(bundles) == 5


def test_successful_session_without_failures_writes_nothing(tmp_path: Path) -> None:
    root = tmp_path / "diagnostics"
    diagnostics.begin_session(_args(tmp_path / "app"), root=root)
    diagnostics.record_attempt(
        "写入文件",
        phase="regular",
        attempt=1,
        max_attempts=1,
        started_ns=time.monotonic_ns(),
        outcome="succeeded",
    )

    assert diagnostics.finish_session(0) is None
    assert not root.exists()


def test_diagnostic_hooks_cannot_change_update_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken_diagnostic(*args, **kwargs):
        raise RuntimeError("diagnostic failure")

    monkeypatch.setattr(diagnostics, "begin_session", broken_diagnostic)
    monkeypatch.setattr(diagnostics, "finish_session", broken_diagnostic)

    result = workbench_updater._run_with_diagnostics(
        _args(tmp_path),
        lambda args: 0,
    )

    assert result == 0


def test_broken_attempt_collectors_cannot_change_retry_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken_diagnostic(*args, **kwargs):
        raise RuntimeError("diagnostic failure")

    monkeypatch.setattr(diagnostics, "record_attempt", broken_diagnostic)
    monkeypatch.setattr(diagnostics, "persist_failure", broken_diagnostic)
    monkeypatch.setattr(
        lock_diag,
        "diagnose_lockers_for_exception",
        lambda exc: lock_diag.RestartManagerResult(
            status="none",
            stage="complete",
            coverage={"complete": True, "registered_file_count": 1},
        ),
    )
    calls = 0

    def fail() -> None:
        nonlocal calls
        calls += 1
        raise PermissionError(5, "拒绝访问", str(tmp_path / "a"), 5)

    with pytest.raises(PermissionError):
        workbench_updater._retry_workbench(
            "备份 a",
            fail,
            logging.getLogger("sug.updater"),
            max_retries=1,
            interval=0,
        )

    assert calls == 2


def test_final_retry_snapshot_is_written_before_caller_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app_dir = tmp_path / "app"
    source = app_dir / "_internal"
    destination = app_dir / "_internal.old"
    source.mkdir(parents=True)
    root = tmp_path / "diagnostics"
    diagnostics.begin_session(_args(app_dir), root=root)
    monkeypatch.setattr(workbench_updater.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(
        lock_diag,
        "diagnose_lockers_for_exception",
        lambda exc: lock_diag.RestartManagerResult(
            status="none",
            stage="complete",
            coverage={"complete": True, "registered_file_count": 1},
        ),
    )

    def fail() -> None:
        raise PermissionError(5, "拒绝访问", str(source), 5, str(destination))

    with pytest.raises(PermissionError):
        workbench_updater._retry_workbench(
            "备份 _internal",
            fail,
            logging.getLogger("sug.updater"),
            max_retries=1,
            interval=0,
        )

    bundle = diagnostics.latest_bundle_path()
    assert bundle is not None
    attempts = _load(bundle / "attempts.json")
    assert [item["phase"] for item in attempts] == [
        "regular",
        "final_after_diagnostics",
    ]
    filesystem = _load(bundle / "filesystem.json")[0]["entries"]
    source_state = next(item for item in filesystem if item["role"] == "filename")
    assert source_state["lexists"] is True
    assert source_state["kind"] == "directory"
