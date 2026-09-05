from __future__ import annotations

import logging
from pathlib import Path

import pytest

from krok_helper.updater_app import lock_diag
from krok_helper.updater_app import main as workbench_updater


def _lock_error(src: Path, dst: Path) -> PermissionError:
    # OSError(errno, strerror, filename, winerror, filename2)：winerror=5 是
    # 「拒绝访问」，属于 _retry_workbench 的可重试集合。
    return PermissionError(5, "拒绝访问。", str(src), 5, str(dst))


def _rm_diagnosis(entries=None, *, status: str | None = None):
    entries = list(entries or [])
    return lock_diag.RestartManagerResult(
        status=status or ("found" if entries else "none"),
        entries=entries,
        stage="complete",
        coverage={"complete": True, "registered_file_count": 1},
    )


def test_configure_product_registers_lock_guard_patches() -> None:
    workbench_updater._configure_product()

    assert workbench_updater.updater_main._retry_on_permission_error is (
        workbench_updater._retry_workbench
    )
    assert workbench_updater.updater_main.wait_for_pid_exit is (
        workbench_updater._wait_for_pid_exit_workbench
    )
    assert workbench_updater.updater_main.run_incremental is (
        workbench_updater._run_incremental_workbench
    )
    assert workbench_updater.updater_main._apply_part is (
        workbench_updater._apply_part_workbench
    )
    assert workbench_updater.updater_main.launch_main_app is (
        workbench_updater._launch_main_app_workbench
    )


def test_wait_for_pid_exit_terminates_only_orphaned_host_descendants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        lock_diag.ProcessTreeEntry(100, 1, "Lin-K Lyrics.exe"),
        lock_diag.ProcessTreeEntry(110, 100, "Updater.exe"),
        lock_diag.ProcessTreeEntry(111, 110, "Updater.exe"),
        lock_diag.ProcessTreeEntry(130, 100, "explorer.exe"),
    ]
    killed: list[int] = []
    recorded: list[dict] = []
    monkeypatch.setattr(workbench_updater.os, "getpid", lambda: 111)
    monkeypatch.setenv(
        workbench_updater._UPDATE_DESCENDANTS_ENV,
        '[{"pid":120,"parent_pid":100,"image_name":"python.exe"},'
        '{"pid":121,"parent_pid":120,"image_name":"ffmpeg.exe"}]',
    )
    monkeypatch.setattr(workbench_updater.lock_diag, "snapshot_processes", lambda: rows)
    monkeypatch.setattr(workbench_updater, "_original_wait_for_pid_exit", lambda *_args: True)
    monkeypatch.setattr(
        workbench_updater.updater_main,
        "_is_pid_alive",
        lambda pid: pid in {120, 121, 130},
    )
    monkeypatch.setattr(
        workbench_updater.lock_diag,
        "process_image_name",
        lambda pid: {
            120: "python.exe",
            121: "ffmpeg.exe",
            130: "explorer.exe",
        }.get(pid, ""),
    )
    monkeypatch.setattr(
        workbench_updater.lock_diag,
        "kill_pid",
        lambda pid: killed.append(pid) is None or True,
    )
    monkeypatch.setattr(
        workbench_updater.diagnostics,
        "record_process_cleanup",
        recorded.append,
    )
    monkeypatch.setattr(workbench_updater.time, "sleep", lambda _seconds: None)

    assert workbench_updater._wait_for_pid_exit_workbench(
        100, logging.getLogger("sug.updater"), timeout=1
    )

    assert killed == [121, 120]
    assert 110 not in killed and 111 not in killed and 130 not in killed
    assert recorded[0]["parent_exited"] is True
    assert {item["outcome"] for item in recorded[0]["processes"]} == {
        "terminated",
        "unmanaged_process_skipped",
    }


def test_wait_for_pid_exit_records_zero_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        lock_diag.ProcessTreeEntry(100, 1, "Lin-K Lyrics.exe"),
        lock_diag.ProcessTreeEntry(111, 100, "Updater.exe"),
    ]
    recorded: list[dict] = []
    monkeypatch.setattr(workbench_updater.os, "getpid", lambda: 111)
    monkeypatch.setenv(workbench_updater._UPDATE_DESCENDANTS_ENV, "[]")
    monkeypatch.setattr(workbench_updater.lock_diag, "snapshot_processes", lambda: rows)
    monkeypatch.setattr(
        workbench_updater.lock_diag,
        "process_snapshot_details",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(workbench_updater, "_original_wait_for_pid_exit", lambda *_args: True)
    monkeypatch.setattr(
        workbench_updater.diagnostics,
        "record_process_cleanup",
        recorded.append,
    )

    assert workbench_updater._wait_for_pid_exit_workbench(
        100, logging.getLogger("sug.updater"), timeout=1
    )
    assert recorded[0]["candidate_count"] == 0
    assert recorded[0]["processes"] == []


def test_retry_uses_three_second_intervals_and_names_holders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(workbench_updater, "_blocked_lock", None)
    sleeps: list[float] = []
    calls = 0
    monkeypatch.setattr(workbench_updater.time, "sleep", sleeps.append)
    monkeypatch.setattr(
        workbench_updater.lock_diag,
        "diagnose_lockers_for_exception",
        lambda exc: _rm_diagnosis([(42, "demo.exe")]),
    )
    src = tmp_path / "strange_uta_game"
    dst = tmp_path / "strange_uta_game.bak"

    def always_locked() -> None:
        nonlocal calls
        calls += 1
        raise _lock_error(src, dst)

    with caplog.at_level(logging.INFO, logger="sug.updater"):
        log = logging.getLogger("sug.updater")
        with pytest.raises(workbench_updater.PersistentFileLock) as excinfo:
            workbench_updater._retry_workbench("备份 _internal/strange_uta_game", always_locked, log)

    assert calls == 7
    assert sleeps == [workbench_updater.FILE_LOCK_RETRY_INTERVAL] * 5
    assert "demo.exe(PID 42)" in str(excinfo.value)
    assert workbench_updater._blocked_lock is not None
    assert workbench_updater._blocked_lock.entries == [(42, "demo.exe")]
    assert any("最终重试仍被系统拒绝" in message for message in caplog.messages)


def test_retry_records_directory_handle_and_permission_diagnostics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "_internal"
    source.mkdir()
    destination = tmp_path / "_internal.old"
    recorded: list[tuple[str, dict]] = []
    monkeypatch.setattr(workbench_updater.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        workbench_updater.lock_diag,
        "diagnose_lockers_for_exception",
        lambda exc: _rm_diagnosis(),
    )
    monkeypatch.setattr(
        workbench_updater.lock_diag,
        "diagnose_directory_handles",
        lambda paths: {
            "status": "found",
            "complete": True,
            "entries": [
                {
                    "pid": 4321,
                    "process_name": "terminal.exe",
                    "is_directory": True,
                }
            ],
        },
    )
    monkeypatch.setattr(
        workbench_updater.lock_diag,
        "diagnose_path_access",
        lambda exc: {"classification": "source_handle_conflict"},
    )
    monkeypatch.setattr(workbench_updater.lock_diag, "snapshot_processes", lambda: [])
    monkeypatch.setattr(
        workbench_updater.diagnostics,
        "record_access_diagnostic",
        lambda operation, detail: recorded.append((operation, detail)),
    )

    def fail() -> None:
        raise _lock_error(source, destination)

    with pytest.raises(PermissionError):
        workbench_updater._retry_workbench(
            "备份 _internal",
            fail,
            logging.getLogger("sug.updater"),
            max_retries=1,
            interval=0,
        )

    assert recorded[0][0] == "备份 _internal"
    assert recorded[0][1]["classification"] == "directory_handle_found"
    assert recorded[0][1]["directory_handles"]["entries"][0]["pid"] == 4321


def test_retry_succeeds_on_final_attempt_after_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(workbench_updater, "_blocked_lock", None)
    sleeps: list[float] = []
    monkeypatch.setattr(workbench_updater.time, "sleep", sleeps.append)
    monkeypatch.setattr(
        workbench_updater.lock_diag,
        "diagnose_lockers_for_exception",
        lambda exc: _rm_diagnosis([(42, "demo.exe")]),
    )
    calls = 0

    def released_after_regular_retries() -> str:
        nonlocal calls
        calls += 1
        if calls <= 6:
            raise _lock_error(tmp_path / "a", tmp_path / "a.bak")
        return "ok"

    result = workbench_updater._retry_workbench(
        "备份 a",
        released_after_regular_retries,
        logging.getLogger("sug.updater"),
    )

    assert result == "ok"
    assert calls == 7
    assert sleeps == [workbench_updater.FILE_LOCK_RETRY_INTERVAL] * 5
    assert workbench_updater._blocked_lock is None


def test_retry_without_holders_reraises_original_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(workbench_updater, "_blocked_lock", None)
    monkeypatch.setattr(workbench_updater.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        workbench_updater.lock_diag,
        "diagnose_lockers_for_exception",
        lambda exc: _rm_diagnosis(),
    )

    calls = 0

    def always_locked() -> None:
        nonlocal calls
        calls += 1
        raise _lock_error(tmp_path / "a", tmp_path / "a.bak")

    with pytest.raises(PermissionError) as excinfo:
        workbench_updater._retry_workbench("备份 a", always_locked, logging.getLogger("sug.updater"))

    assert type(excinfo.value) is PermissionError
    assert calls == 7
    assert workbench_updater._blocked_lock is None


def test_retry_non_lock_oserror_raises_immediately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(workbench_updater.time, "sleep", sleeps.append)

    def broken() -> None:
        # winerror=3 会被 OSError 映射为 FileNotFoundError，且不属于
        # 可重试的锁类错误（5/32），必须立刻原样冒泡。
        raise OSError(3, "系统找不到指定的路径。", str(tmp_path / "missing"), 3)

    with pytest.raises(OSError) as excinfo:
        workbench_updater._retry_workbench("写入 a", broken, logging.getLogger("sug.updater"))

    assert not isinstance(excinfo.value, workbench_updater.PersistentFileLock)
    assert not sleeps


def test_apply_part_removes_stale_backup_before_generic_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app_dir = tmp_path / "app"
    leftover = app_dir / "_internal" / "strange_uta_game.bak"
    leftover.mkdir(parents=True)
    (leftover / "old.py").write_text("old", encoding="utf-8")
    called = False

    def generic_apply(*args):
        nonlocal called
        called = True
        assert not leftover.exists()
        return True, ""

    monkeypatch.setattr(workbench_updater, "_original_apply_part", generic_apply)
    ok, err = workbench_updater._apply_part_workbench(
        tmp_path / "part.zip",
        ["_internal/strange_uta_game"],
        app_dir,
        tmp_path,
        "app",
        logging.getLogger("sug.updater"),
    )

    assert ok is True and err == ""
    assert called is True


def test_apply_part_stops_when_stale_backup_cannot_be_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app_dir = tmp_path / "app"
    leftover = app_dir / "_internal" / "strange_uta_game.bak"
    leftover.mkdir(parents=True)
    monkeypatch.setattr(
        workbench_updater.shutil,
        "rmtree",
        lambda *args, **kwargs: (_ for _ in ()).throw(PermissionError(5, "拒绝访问")),
    )
    monkeypatch.setattr(workbench_updater.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        workbench_updater.lock_diag,
        "diagnose_lockers_for_exception",
        lambda exc: _rm_diagnosis(),
    )
    called = False

    def generic_apply(*args):
        nonlocal called
        called = True
        return True, ""

    monkeypatch.setattr(workbench_updater, "_original_apply_part", generic_apply)
    ok, err = workbench_updater._apply_part_workbench(
        tmp_path / "part.zip",
        ["_internal/strange_uta_game"],
        app_dir,
        tmp_path,
        "app",
        logging.getLogger("sug.updater"),
    )

    assert ok is False
    assert "清理旧备份目标" in err
    assert called is False


def test_incremental_failure_keeps_full_fallback_and_clears_stale_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(workbench_updater, "_blocked_lock", workbench_updater.BlockedLockInfo(
        entries=[(42, "demo.exe")], detail="stale"
    ))
    monkeypatch.setattr(workbench_updater, "_original_run_incremental", lambda *args: 33)

    rc = workbench_updater._run_incremental_workbench(
        None, {}, None, logging.getLogger("sug.updater")
    )

    assert rc == 33
    assert workbench_updater._blocked_lock is None


def test_lock_diag_formats_and_filters_lockers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    target = tmp_path / "bass.dll"
    target.write_bytes(b"dll")
    own_pid = os.getpid()
    monkeypatch.setattr(
        lock_diag,
        "_rm_lockers",
        lambda paths: [
            (own_pid, "self"),
            (1234, "Windows 资源管理器"),
            (1234, "Windows 资源管理器"),
            (0, ""),
        ],
    )

    described = lock_diag.describe_lockers([target])

    assert described == "占用进程：Windows 资源管理器(PID 1234)"


def test_lock_diag_returns_empty_when_rm_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "bass.dll"
    target.write_bytes(b"dll")
    monkeypatch.setattr(lock_diag, "_rm_lockers", lambda paths: None)

    assert lock_diag.describe_lockers([target]) == ""


def test_lock_diag_reads_paths_from_exception(tmp_path: Path) -> None:
    src = tmp_path / "strange_uta_game"
    src.mkdir()
    exc = OSError(5, "拒绝访问。", str(src), 5, str(src) + ".bak")

    # 真实 RM 在测试环境可能返回空；只验证路径提取不抛异常且结果类型正确。
    described = lock_diag.describe_lockers_for_exception(exc)

    assert isinstance(described, str)


def test_cleanup_workbench_temp_workdir_resets_blocked_lock(tmp_path: Path) -> None:
    workbench_updater._blocked_lock = workbench_updater.BlockedLockInfo(
        entries=[(42, "demo.exe")], detail="占用进程：demo.exe(PID 42)"
    )

    workbench_updater._cleanup_workbench_temp_workdir(tmp_path)

    assert workbench_updater._blocked_lock is None


def test_classify_lock_entries_uses_image_name_and_protects_critical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os

    fake_images = {
        111: r"C:\Windows\explorer.exe",
        222: r"C:\Windows\System32\svchost.exe",
        333: r"C:\Program Files\Safe\MsMpEng.exe",
        444: r"G:\nicokara\Karaoke Studio\Karaoke Studio.exe",
        os.getpid(): r"C:\Temp\Updater.exe",
    }
    monkeypatch.setattr(
        workbench_updater.lock_diag,
        "process_image_name",
        lambda pid: fake_images.get(pid, ""),
    )

    killable, blocked = workbench_updater._classify_lock_entries(
        [
            (111, "Windows 资源管理器"),
            (222, "Service"),
            (333, "MsMpEng"),
            (444, "Karaoke Studio"),
            (os.getpid(), "self"),
        ]
    )

    killable_pids = [pid for pid, _name, _image in killable]
    blocked_pids = [pid for pid, _name, _image in blocked]
    # 只有本程序家族可结束；Explorer、系统进程和安全软件都只展示。
    assert killable_pids == [444]
    assert blocked_pids == [111, 222, 333]


def test_classify_lock_entries_falls_back_to_friendly_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(workbench_updater.lock_diag, "process_image_name", lambda pid: "")

    killable, blocked = workbench_updater._classify_lock_entries(
        [(555, "Karaoke Studio.exe")]
    )

    assert not killable
    assert [pid for pid, _n, _i in blocked] == [555]


def test_full_update_stops_when_old_backup_cannot_be_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app_dir = tmp_path / "app"
    new_root = tmp_path / "new"
    (app_dir / "_internal").mkdir(parents=True)
    (app_dir / "_internal.old").mkdir()
    (app_dir / "Karaoke Studio.exe").write_bytes(b"old")
    (new_root / "_internal").mkdir(parents=True)
    (new_root / "Karaoke Studio.exe").write_bytes(b"new")
    (new_root / "Lin-K Lyrics.exe").write_bytes(b"new")
    (new_root / "krok_subtitle_renderer.exe").write_bytes(b"new")
    called = False

    def fail_rmtree(*args, **kwargs):
        raise PermissionError(5, "拒绝访问")

    def generic_apply(*args):
        nonlocal called
        called = True
        return True, ""

    monkeypatch.setattr(workbench_updater.shutil, "rmtree", fail_rmtree)
    monkeypatch.setattr(workbench_updater.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        workbench_updater.lock_diag,
        "diagnose_lockers_for_exception",
        lambda exc: _rm_diagnosis(),
    )
    monkeypatch.setattr(workbench_updater, "_original_apply_update", generic_apply)

    ok, err = workbench_updater._apply_workbench_update(
        app_dir,
        "Karaoke Studio.exe",
        "_internal",
        new_root,
        logging.getLogger("sug.updater"),
    )

    assert ok is False
    assert "_internal.old" in err
    assert called is False


def test_full_update_removes_stale_backups_before_generic_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app_dir = tmp_path / "app"
    new_root = tmp_path / "new"
    (app_dir / "_internal").mkdir(parents=True)
    (app_dir / "_internal.old").mkdir()
    (app_dir / "Lin-K Lyrics.exe").write_bytes(b"old")
    (app_dir / "Lin-K Lyrics.exe.old").write_bytes(b"older")
    (new_root / "_internal").mkdir(parents=True)
    (new_root / "Lin-K Lyrics.exe").write_bytes(b"new")
    (new_root / "Karaoke Studio.exe").write_bytes(b"new")
    (new_root / "krok_subtitle_renderer.exe").write_bytes(b"new")

    def generic_apply(app_dir, app_exe, _internal_name, new_root, _log):
        assert not (app_dir / "_internal.old").exists()
        assert not (app_dir / "Lin-K Lyrics.exe.old").exists()
        # 忠实模拟原版行为：回写 --app-exe 指定的那一个 EXE
        workbench_updater.shutil.copy2(
            str(new_root / app_exe), str(app_dir / app_exe)
        )
        return True, ""

    monkeypatch.setattr(workbench_updater, "_original_apply_update", generic_apply)

    ok, err = workbench_updater._apply_workbench_update(
        app_dir,
        "Lin-K Lyrics.exe",
        "_internal",
        new_root,
        logging.getLogger("sug.updater"),
    )

    assert ok is True and err == ""
    assert (app_dir / "Lin-K Lyrics.exe").read_bytes() == b"new"
    # 新名更新不回写旧名副本（清理由成功拉起后的启动挂钩统一做，详见 §8.1）。
    assert not (app_dir / "Karaoke Studio.exe").exists()
    # sidecar 属必备根目录负载，一并回写。
    assert (app_dir / "krok_subtitle_renderer.exe").read_bytes() == b"new"


def test_full_update_uses_neutral_rename_error_wording(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app_dir = tmp_path / "app"
    new_root = tmp_path / "new"
    (app_dir / "_internal").mkdir(parents=True)
    (app_dir / "Lin-K Lyrics.exe").write_bytes(b"old")
    (new_root / "_internal").mkdir(parents=True)
    (new_root / "Lin-K Lyrics.exe").write_bytes(b"new")
    (new_root / "Karaoke Studio.exe").write_bytes(b"new")
    (new_root / "krok_subtitle_renderer.exe").write_bytes(b"new")
    monkeypatch.setattr(
        workbench_updater,
        "_original_apply_update",
        lambda *args: (False, "备份失败（主程序可能仍未完全释放文件句柄）"),
    )

    ok, err = workbench_updater._apply_workbench_update(
        app_dir,
        "Lin-K Lyrics.exe",
        "_internal",
        new_root,
        logging.getLogger("sug.updater"),
    )

    assert ok is False
    assert "目录重命名被系统拒绝" in err
    assert "可能" not in err


def test_launch_main_app_resets_pyinstaller_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exe = tmp_path / "Lin-K Lyrics.exe"
    exe.write_bytes(b"exe")
    captured = {}

    class FakeProcess:
        pass

    def fake_popen(args, **kwargs):
        captured["args"] = args
        captured.update(kwargs)
        return FakeProcess()

    monkeypatch.setattr(workbench_updater.subprocess, "Popen", fake_popen)

    assert workbench_updater._launch_main_app_workbench(
        tmp_path,
        exe.name,
        logging.getLogger("sug.updater"),
    )
    assert captured["cwd"] == str(tmp_path)
    assert captured["env"]["PYINSTALLER_RESET_ENVIRONMENT"] == "1"


def test_kill_pid_terminates_disposable_child() -> None:
    import os
    import subprocess
    import sys
    import time

    if os.name != "nt":
        pytest.skip("仅 Windows")

    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 10
    while child.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert child.poll() is None  # 子进程在睡眠，确实存活

    assert workbench_updater.lock_diag.kill_pid(child.pid) is True
    assert child.poll() is not None

    # 自身与非法 PID 永不结束。
    assert workbench_updater.lock_diag.kill_pid(os.getpid()) is False
    assert workbench_updater.lock_diag.kill_pid(0) is False


def test_find_lockers_returns_structured_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    target = tmp_path / "bass.dll"
    target.write_bytes(b"dll")
    own_pid = os.getpid()
    monkeypatch.setattr(
        lock_diag,
        "_rm_lockers",
        lambda paths: [(own_pid, "self"), (1234, "Windows 资源管理器"), (1234, "dup"), (0, "")],
    )

    entries = lock_diag.find_lockers([target])

    assert entries == [(1234, "Windows 资源管理器")]
    assert lock_diag.format_lockers(entries) == "占用进程：Windows 资源管理器(PID 1234)"


def test_enable_gui_registers_lock_recovery_hooks() -> None:
    workbench_updater._enable_gui()
    from updater_app import gui as updater_gui

    assert getattr(updater_gui._UpdaterWindow, "_workbench_lock_recovery_patch", False)
    assert getattr(updater_gui.run_gui, "_workbench_retry_context_patch", False)


def test_lock_dialog_construction_and_retry_flag() -> None:
    from PyQt6.QtWidgets import QApplication, QWidget

    from krok_helper.updater_app import lock_dialog

    app = QApplication.instance() or QApplication([])
    assert app is not None
    parent = QWidget()

    dialog = lock_dialog.LockRecoveryDialog(
        "以下程序正在占用安装目录，导致无法替换文件：",
        "Python 演示进程 A（PID 1）\n以下进程不会被更新器结束：360安全卫士。",
        "结束占用进程并重试",
        parent=parent,
        informative_text="测试文案",
    )
    assert dialog.retry_requested is False
    assert dialog.yesButton.text() == "结束占用进程并重试"
    assert dialog.cancelButton.text() == "关闭"
    dialog._mark_retry()
    assert dialog.retry_requested is True

    dialog_no_kill = lock_dialog.LockRecoveryDialog(
        "以下程序正在占用安装目录，导致无法替换文件：",
        "360安全卫士（PID 1）",
        "重试",
        parent=parent,
        informative_text="请重启电脑后再次尝试更新。",
    )
    assert dialog_no_kill.retry_requested is False

    # parent 缺失时必须显式失败（MaskDialogBase 依赖父窗口铺蒙层）
    with pytest.raises(ValueError):
        lock_dialog.LockRecoveryDialog("正文", "明细", "重试", parent=None)


# ───────────────── 全量回退路径的根目录负载回写 ─────────────────


def test_full_update_replaces_sidecar_and_dual_named_exes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """全量回退必须回写 --app-exe 之外的根目录 EXE（回归：GPU sidecar 停留旧版）。

    2026-09 事故：全量路径只回写 --app-exe 指定的主程序 + _internal，包内
    ``krok_subtitle_renderer.exe`` 与另一份主程序名被跳过，用户得到「新 Python
    代码 + 旧 schema sidecar」的混合安装，GPU 全量回退 Painter。

    2026-09 改名迁移后按启动名分家：存量客户端按旧名更新时双名都回写（它的
    下一次更新仍按旧名校验包）；新版主程序固定传新名，更新成功后不再回写
    旧名副本、而是清理它（见 docs/auto_update.md §8.1）。
    """
    for launched_name in ("Lin-K Lyrics.exe", "Karaoke Studio.exe"):
        app_dir = tmp_path / f"app-{launched_name}"
        new_root = tmp_path / f"new-{launched_name}"
        (app_dir / "_internal").mkdir(parents=True)
        (app_dir / "Lin-K Lyrics.exe").write_bytes(b"old-primary")
        (app_dir / "Karaoke Studio.exe").write_bytes(b"old-legacy")
        (app_dir / "krok_subtitle_renderer.exe").write_bytes(b"old-sidecar")
        (new_root / "_internal").mkdir(parents=True)
        (new_root / "Lin-K Lyrics.exe").write_bytes(b"new-primary")
        (new_root / "Karaoke Studio.exe").write_bytes(b"new-legacy")
        (new_root / "krok_subtitle_renderer.exe").write_bytes(b"new-sidecar")
        def faithful_generic_apply(app_dir, app_exe, _internal_name, new_root, _log):
            # 忠实模拟原版行为：回写 --app-exe 指定的那一个 EXE
            workbench_updater.shutil.copy2(
                str(new_root / app_exe), str(app_dir / app_exe)
            )
            return True, ""

        monkeypatch.setattr(
            workbench_updater, "_original_apply_update", faithful_generic_apply
        )

        ok, err = workbench_updater._apply_workbench_update(
            app_dir,
            launched_name,
            "_internal",
            new_root,
            logging.getLogger("sug.updater"),
        )

        assert ok is True and err == ""
        assert (app_dir / "krok_subtitle_renderer.exe").read_bytes() == b"new-sidecar"
        assert (app_dir / "Lin-K Lyrics.exe").read_bytes() == b"new-primary"
        assert not (app_dir / "krok_subtitle_renderer.exe.old").exists()
        if launched_name == workbench_updater.PRIMARY_APP_EXE_NAME:
            # 新名更新：旧名副本不回写也不删除——清理统一由成功拉起后的启动
            # 挂钩做（先删后启动失败会让安装失去可用入口）。
            assert (app_dir / "Karaoke Studio.exe").read_bytes() == b"old-legacy"
        else:
            # 旧名更新（存量客户端）：双主程序名都回写，不许分家。
            assert (app_dir / "Karaoke Studio.exe").read_bytes() == b"new-legacy"


@pytest.mark.parametrize(
    "missing_name",
    ["Karaoke Studio.exe", "krok_subtitle_renderer.exe"],
)
def test_full_update_rejects_package_missing_required_root_exe(
    tmp_path: Path, missing_name: str
) -> None:
    """缺必备根目录 EXE（主程序双名之一 / GPU sidecar）的全量包按损坏包处理。

    sidecar 与主程序名同样走前置校验：缺失即拒绝，不允许「更新成功却保留
    旧 sidecar」——那正是 2026-09 混合安装事故的形态。
    """
    app_dir = tmp_path / "app"
    new_root = tmp_path / "new"
    (app_dir / "_internal").mkdir(parents=True)
    (app_dir / "Lin-K Lyrics.exe").write_bytes(b"old")
    (app_dir / "krok_subtitle_renderer.exe").write_bytes(b"old-sidecar")
    (new_root / "_internal").mkdir(parents=True)
    (new_root / "Lin-K Lyrics.exe").write_bytes(b"new")
    (new_root / "Karaoke Studio.exe").write_bytes(b"new")
    (new_root / "krok_subtitle_renderer.exe").write_bytes(b"new")
    (new_root / missing_name).unlink()

    ok, err = workbench_updater._apply_workbench_update(
        app_dir,
        "Lin-K Lyrics.exe",
        "_internal",
        new_root,
        logging.getLogger("sug.updater"),
    )

    assert ok is False
    assert f"更新包中找不到 {missing_name}" in err
    # 包校验失败必须发生在触碰任何文件之前
    assert (app_dir / "Lin-K Lyrics.exe").read_bytes() == b"old"
    assert (app_dir / "krok_subtitle_renderer.exe").read_bytes() == b"old-sidecar"


def test_full_update_sidecar_copy_blocked_reports_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """sidecar 回写被持续拒绝时更新必须显式失败，并回滚该文件的 rename。"""
    app_dir = tmp_path / "app"
    new_root = tmp_path / "new"
    (app_dir / "_internal").mkdir(parents=True)
    (app_dir / "Lin-K Lyrics.exe").write_bytes(b"old-primary")
    (app_dir / "krok_subtitle_renderer.exe").write_bytes(b"old-sidecar")
    (new_root / "_internal").mkdir(parents=True)
    (new_root / "Lin-K Lyrics.exe").write_bytes(b"new-primary")
    (new_root / "Karaoke Studio.exe").write_bytes(b"new-legacy")
    (new_root / "krok_subtitle_renderer.exe").write_bytes(b"new-sidecar")
    monkeypatch.setattr(
        workbench_updater, "_original_apply_update", lambda *args: (True, "")
    )
    monkeypatch.setattr(workbench_updater.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        workbench_updater.lock_diag,
        "diagnose_lockers_for_exception",
        lambda exc: _rm_diagnosis(),
    )

    real_copy2 = workbench_updater.shutil.copy2

    def blocked_copy2(src, dst, *args, **kwargs):
        if str(dst).endswith("krok_subtitle_renderer.exe"):
            raise PermissionError(5, "拒绝访问。", str(src), 5, str(dst))
        return real_copy2(src, dst, *args, **kwargs)

    monkeypatch.setattr(workbench_updater.shutil, "copy2", blocked_copy2)

    ok, err = workbench_updater._apply_workbench_update(
        app_dir,
        "Lin-K Lyrics.exe",
        "_internal",
        new_root,
        logging.getLogger("sug.updater"),
    )

    assert ok is False
    assert "写入 krok_subtitle_renderer.exe 失败" in err
    # rename 已回滚：旧 sidecar 原位保留，不留 .old
    assert (app_dir / "krok_subtitle_renderer.exe").read_bytes() == b"old-sidecar"
    assert not (app_dir / "krok_subtitle_renderer.exe.old").exists()


# ───────────────── 旧名主程序副本的迁移清理 ─────────────────


def _make_dual_name_install(app_dir: Path) -> None:
    (app_dir / "_internal").mkdir(parents=True, exist_ok=True)
    (app_dir / "Lin-K Lyrics.exe").write_bytes(b"primary")
    (app_dir / "Karaoke Studio.exe").write_bytes(b"legacy")
    (app_dir / "Karaoke Studio.exe.old").write_bytes(b"legacy-backup")


def _mock_shortcut_migration(monkeypatch: pytest.MonkeyPatch, result: bool) -> list[Path]:
    """拦下真实 PowerShell 迁移，记录调用并返回指定结果。"""
    calls: list[Path] = []

    def fake_migration(app_dir, log):
        calls.append(app_dir)
        return result

    monkeypatch.setattr(workbench_updater, "_migrate_legacy_shortcuts", fake_migration)
    return calls


def test_cleanup_legacy_exe_runs_only_for_new_name_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """新名会话清理旧名副本（含 .old 残留）；旧名会话（存量客户端）绝不动它。"""
    new_name_dir = tmp_path / "new-session"
    legacy_name_dir = tmp_path / "legacy-session"
    for app_dir in (new_name_dir, legacy_name_dir):
        _make_dual_name_install(app_dir)
    migration_calls = _mock_shortcut_migration(monkeypatch, True)
    log = logging.getLogger("sug.updater")

    workbench_updater._cleanup_legacy_main_exe(
        new_name_dir, workbench_updater.PRIMARY_APP_EXE_NAME, log
    )
    assert not (new_name_dir / "Karaoke Studio.exe").exists()
    assert not (new_name_dir / "Karaoke Studio.exe.old").exists()
    assert (new_name_dir / "Lin-K Lyrics.exe").read_bytes() == b"primary"

    workbench_updater._cleanup_legacy_main_exe(
        legacy_name_dir, workbench_updater.LEGACY_APP_EXE_NAME, log
    )
    assert (legacy_name_dir / "Karaoke Studio.exe").read_bytes() == b"legacy"
    assert (legacy_name_dir / "Karaoke Studio.exe.old").read_bytes() == b"legacy-backup"
    # 迁移只在删旧名本体的新名会话里触发过一次。
    assert migration_calls == [new_name_dir]


def test_cleanup_keeps_legacy_when_new_entry_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P1 回归：新名入口缺失时绝不清理——旧名可能是该安装唯一可运行入口。

    最小复现：仅有可运行的 Karaoke Studio.exe（如降级旧 Updater 的全量更新只
    回写了旧名）。清理先于存在性检查会删掉它再启动失败，两个入口全灭。
    """
    app_dir = tmp_path / "app"
    (app_dir / "_internal").mkdir(parents=True)
    (app_dir / "Karaoke Studio.exe").write_bytes(b"legacy-only")
    (app_dir / "Karaoke Studio.exe.old").write_bytes(b"legacy-backup")
    migration_calls = _mock_shortcut_migration(monkeypatch, True)
    log = logging.getLogger("sug.updater")

    workbench_updater._cleanup_legacy_main_exe(
        app_dir, workbench_updater.PRIMARY_APP_EXE_NAME, log
    )
    assert (app_dir / "Karaoke Studio.exe").read_bytes() == b"legacy-only"
    assert (app_dir / "Karaoke Studio.exe.old").read_bytes() == b"legacy-backup"
    assert migration_calls == []

    # 启动挂钩同样先失败退出（找不到新名 EXE），不会碰旧名。
    assert not workbench_updater._launch_main_app_workbench(
        app_dir, workbench_updater.PRIMARY_APP_EXE_NAME, log
    )
    assert (app_dir / "Karaoke Studio.exe").read_bytes() == b"legacy-only"


def test_cleanup_defers_when_shortcut_migration_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """快捷方式迁移工具没跑起来时本次整体不清理，下次更新重试。

    否则会留下指向已删除文件的死快捷方式——正是迁移要消除的破坏面。
    """
    app_dir = tmp_path / "app"
    _make_dual_name_install(app_dir)
    _mock_shortcut_migration(monkeypatch, False)

    workbench_updater._cleanup_legacy_main_exe(
        app_dir,
        workbench_updater.PRIMARY_APP_EXE_NAME,
        logging.getLogger("sug.updater"),
    )

    assert (app_dir / "Karaoke Studio.exe").read_bytes() == b"legacy"
    assert (app_dir / "Karaoke Studio.exe.old").read_bytes() == b"legacy-backup"


def test_incremental_success_defers_cleanup_to_relaunch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """增量成功本身不清理：统一等成功拉起后的启动挂钩，保证任何时刻有可用入口。"""
    from types import SimpleNamespace

    app_dir = tmp_path / "app"
    _make_dual_name_install(app_dir)
    monkeypatch.setattr(workbench_updater, "_original_run_incremental", lambda *args: 0)
    monkeypatch.setattr(
        workbench_updater,
        "_cleanup_legacy_main_exe",
        lambda *args: pytest.fail("清理必须由启动挂钩触发，不得在增量尾部执行"),
    )
    args = SimpleNamespace(
        app_dir=app_dir, app_exe=workbench_updater.PRIMARY_APP_EXE_NAME
    )

    rc = workbench_updater._run_incremental_workbench(
        args, {}, tmp_path, logging.getLogger("sug.updater")
    )

    assert rc == 0
    assert (app_dir / "Karaoke Studio.exe").read_bytes() == b"legacy"


def test_launch_hook_cleans_legacy_exe_after_relaunch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """清理必须发生在成功拉起之后：Popen 执行时旧名仍在，返回 True 时已清理。"""
    app_dir = tmp_path
    _make_dual_name_install(app_dir)
    legacy_at_popen: list[bool] = []
    migration_calls = _mock_shortcut_migration(monkeypatch, True)

    def fake_popen(args, **kwargs):
        legacy_at_popen.append((app_dir / "Karaoke Studio.exe").exists())
        return object()

    monkeypatch.setattr(workbench_updater.subprocess, "Popen", fake_popen)

    assert workbench_updater._launch_main_app_workbench(
        app_dir,
        workbench_updater.PRIMARY_APP_EXE_NAME,
        logging.getLogger("sug.updater"),
    )
    assert legacy_at_popen == [True]
    assert not (app_dir / "Karaoke Studio.exe").exists()
    assert not (app_dir / "Karaoke Studio.exe.old").exists()
    assert migration_calls == [app_dir]


def test_migrate_legacy_shortcuts_invokes_powershell_with_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """迁移用 PowerShell 原地改写 .lnk：路径经环境变量传入，退出码决定结果。"""
    import subprocess
    import sys

    if sys.platform != "win32":
        pytest.skip("仅 Windows")
    captured: dict = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured["env"] = kwargs["env"]
        return subprocess.CompletedProcess(args=args, returncode=0)

    monkeypatch.setattr(workbench_updater.subprocess, "run", fake_run)
    assert workbench_updater._migrate_legacy_shortcuts(
        tmp_path, logging.getLogger("sug.updater")
    )

    assert captured["args"][0] == "powershell"
    assert "WScript.Shell" in captured["args"][-1]
    assert captured["env"]["KROK_MIGRATE_LEGACY_EXE"].endswith("Karaoke Studio.exe")
    assert captured["env"]["KROK_MIGRATE_PRIMARY_EXE"].endswith("Lin-K Lyrics.exe")

    monkeypatch.setattr(
        workbench_updater.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(args=a, returncode=2),
    )
    assert not workbench_updater._migrate_legacy_shortcuts(
        tmp_path, logging.getLogger("sug.updater")
    )

    def broken_run(*args, **kwargs):
        raise OSError("powershell not found")

    monkeypatch.setattr(workbench_updater.subprocess, "run", broken_run)
    assert not workbench_updater._migrate_legacy_shortcuts(
        tmp_path, logging.getLogger("sug.updater")
    )


def test_cleanup_falls_back_to_rename_when_delete_is_denied(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """删除被持续拒绝（如旧名镜像仍被占用）时 rename 成 .old 交给下次清理，不影响更新。"""
    app_dir = tmp_path / "app"
    _make_dual_name_install(app_dir)
    _mock_shortcut_migration(monkeypatch, True)
    monkeypatch.setattr(workbench_updater.time, "sleep", lambda _seconds: None)
    real_unlink = Path.unlink

    def refusing_unlink(self, missing_ok=False):
        if self.name == "Karaoke Studio.exe":
            raise PermissionError(5, "拒绝访问。", str(self), 5, str(self))
        return real_unlink(self, missing_ok)

    monkeypatch.setattr(Path, "unlink", refusing_unlink)

    with caplog.at_level(logging.INFO, logger="sug.updater"):
        workbench_updater._cleanup_legacy_main_exe(
            app_dir,
            workbench_updater.PRIMARY_APP_EXE_NAME,
            logging.getLogger("sug.updater"),
        )

    # 本体被挪成 .old（原 .old 已先行删除），内容不丢，等下次更新回收。
    assert not (app_dir / "Karaoke Studio.exe").exists()
    assert (app_dir / "Karaoke Studio.exe.old").read_bytes() == b"legacy"
    assert any("已暂存为 .old" in message for message in caplog.messages)
