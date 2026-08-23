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


def test_configure_product_registers_lock_guard_patches() -> None:
    workbench_updater._configure_product()

    assert workbench_updater.updater_main._retry_on_permission_error is (
        workbench_updater._retry_workbench
    )
    assert workbench_updater.updater_main.run_incremental is (
        workbench_updater._run_incremental_workbench
    )
    assert workbench_updater.updater_main._apply_part is (
        workbench_updater._apply_part_workbench
    )


def test_retry_uses_three_second_intervals_and_names_holders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    workbench_updater._persistent_lock_detail.clear()
    monkeypatch.setattr(workbench_updater, "_blocked_lock", None)
    sleeps: list[float] = []
    monkeypatch.setattr(workbench_updater.time, "sleep", sleeps.append)
    monkeypatch.setattr(
        workbench_updater.lock_diag,
        "find_lockers_for_exception",
        lambda exc: [(42, "demo.exe")],
    )
    src = tmp_path / "strange_uta_game"
    dst = tmp_path / "strange_uta_game.bak"

    def always_locked() -> None:
        raise _lock_error(src, dst)

    with caplog.at_level(logging.INFO, logger="sug.updater"):
        log = logging.getLogger("sug.updater")
        with pytest.raises(workbench_updater.PersistentFileLock) as excinfo:
            workbench_updater._retry_workbench("备份 _internal/strange_uta_game", always_locked, log)

    assert sleeps == [workbench_updater.FILE_LOCK_RETRY_INTERVAL] * 6
    assert "demo.exe(PID 42)" in str(excinfo.value)
    assert workbench_updater._persistent_lock_detail
    assert workbench_updater._blocked_lock is not None
    assert workbench_updater._blocked_lock.entries == [(42, "demo.exe")]
    assert any("持续被占用" in message for message in caplog.messages)


def test_retry_without_holders_reraises_original_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(workbench_updater, "_blocked_lock", None)
    monkeypatch.setattr(workbench_updater.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        workbench_updater.lock_diag, "find_lockers_for_exception", lambda exc: []
    )

    def always_locked() -> None:
        raise _lock_error(tmp_path / "a", tmp_path / "a.bak")

    with pytest.raises(PermissionError) as excinfo:
        workbench_updater._retry_workbench("备份 a", always_locked, logging.getLogger("sug.updater"))

    assert type(excinfo.value) is PermissionError
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


def test_run_incremental_guard_aborts_before_full_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def locked_failure(args, manifest, work_dir, log):
        workbench_updater._persistent_lock_detail.append(
            "备份 _internal/strange_uta_game：占用进程：demo.exe(PID 42)"
        )
        return 33

    monkeypatch.setattr(workbench_updater, "_original_run_incremental", locked_failure)

    with pytest.raises(workbench_updater.UpdateBlockedByLock) as excinfo:
        workbench_updater._run_incremental_workbench(None, {}, None, logging.getLogger("sug.updater"))

    message = str(excinfo.value)
    assert "demo.exe(PID 42)" in message
    assert "重启电脑" in message
    assert "杀" not in message
    # 明细在入口被清空，guard 消费后不影响下一次运行。
    assert workbench_updater._persistent_lock_detail == []


def test_run_incremental_guard_clears_stale_detail(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(workbench_updater, "_original_run_incremental", lambda *args: 0)
    workbench_updater._persistent_lock_detail.append("stale")

    rc = workbench_updater._run_incremental_workbench(None, {}, None, logging.getLogger("sug.updater"))

    assert rc == 0
    assert workbench_updater._persistent_lock_detail == []


def test_run_incremental_guard_keeps_full_fallback_for_other_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(workbench_updater, "_original_run_incremental", lambda *args: 33)

    log = logging.getLogger("sug.updater")
    assert workbench_updater._run_incremental_workbench(None, {}, None, log) == 33

    monkeypatch.setattr(workbench_updater, "_original_run_incremental", lambda *args: 31)
    assert workbench_updater._run_incremental_workbench(None, {}, None, log) == 31


def test_apply_part_reports_leftover_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(
        workbench_updater, "_original_apply_part", lambda *args: (True, "")
    )
    app_dir = tmp_path / "app"
    leftover = app_dir / "_internal" / "strange_uta_game.bak"
    leftover.mkdir(parents=True)

    with caplog.at_level(logging.ERROR, logger="sug.updater"):
        ok, err = workbench_updater._apply_part_workbench(
            tmp_path / "part.zip",
            ["_internal/strange_uta_game"],
            app_dir,
            tmp_path,
            "app",
            logging.getLogger("sug.updater"),
        )

    assert ok is True and err == ""
    assert any("残留" in message for message in caplog.messages)


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
    # explorer 与主程序家族可结束；svchost / 安全软件只展示；自身排除。
    assert killable_pids == [111, 444]
    assert blocked_pids == [222, 333]


def test_classify_lock_entries_falls_back_to_friendly_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(workbench_updater.lock_diag, "process_image_name", lambda pid: "")

    killable, blocked = workbench_updater._classify_lock_entries([(555, "360Tray.exe")])

    assert not killable
    assert [pid for pid, _n, _i in blocked] == [555]


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
