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

    def generic_apply(*args):
        assert not (app_dir / "_internal.old").exists()
        assert not (app_dir / "Lin-K Lyrics.exe.old").exists()
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


def test_full_update_uses_neutral_rename_error_wording(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app_dir = tmp_path / "app"
    new_root = tmp_path / "new"
    (app_dir / "_internal").mkdir(parents=True)
    (app_dir / "Lin-K Lyrics.exe").write_bytes(b"old")
    (new_root / "_internal").mkdir(parents=True)
    (new_root / "Lin-K Lyrics.exe").write_bytes(b"new")
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
