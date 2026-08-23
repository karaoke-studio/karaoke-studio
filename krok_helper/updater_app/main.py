from __future__ import annotations

import logging
import os
import shutil
import sys
import time
from dataclasses import dataclass, field

from krok_helper import ensure_sug_root_path
from krok_helper.updater_app import lock_diag

ensure_sug_root_path()
from updater_app import main as updater_main


# 工作台口径的文件锁重试间隔：等差、3s（SUG 默认 1.5s）。占用方多为常驻句柄，
# 单纯加次数帮助有限，拉长单次等待更稳妥。
FILE_LOCK_RETRY_INTERVAL = 3.0

# 永不由更新器结束的进程镜像名（小写）。系统关键进程结束会导致系统失稳，
# 安全软件进程受自保护且不应由应用代管——占用名单里出现它们时只展示、不提供
# 结束入口，指引用户重启电脑后再试。
_PROTECTED_PROCESS_IMAGES = frozenset({
    # Windows 系统关键进程
    "system", "registry", "smss.exe", "csrss.exe", "wininit.exe", "winlogon.exe",
    "services.exe", "lsass.exe", "lsm.exe", "svchost.exe", "dwm.exe",
    "fontdrvhost.exe", "sihost.exe", "taskhostw.exe", "conhost.exe",
    # Windows Defender / Security Health
    "msmpeng.exe", "msmpengl.exe", "nissrv.exe",
    "securityhealthservice.exe", "securityhealthsystray.exe",
    # 常见第三方安全软件（名单不追求完备，宁漏勿错杀）
    "360tray.exe", "360safe.exe", "zhudongfangyu.exe", "360sd.exe",
    "qhsafetray.exe", "qmdl.exe", "qqpcrtp.exe", "qqpctray.exe",
    "kxetray.exe", "ksafe.exe", "ravmond.exe", "baidusd.exe",
    "baiduansvx.exe", "hipsdaemon.exe", "hipstray.exe",
})


class PersistentFileLock(OSError):
    """重试窗口耗尽后仍被占用（WinError 5/32），消息中携带占用进程清单。"""


class UpdateBlockedByLock(RuntimeError):
    """增量与全量共同依赖的目录被占用，已跳过注定失败的全量下载。"""


@dataclass
class BlockedLockInfo:
    """一次持续占用失败的结构化信息，供 GUI 弹窗提供「结束进程并重试」。"""

    entries: list = field(default_factory=list)  # [(pid, 友好名)]
    detail: str = ""


# worker 线程写入（_retry_workbench 抛 PersistentFileLock 时），GUI 线程在
# on_finished 时读取——finished 信号保证 happens-after，无需额外加锁。
_blocked_lock: BlockedLockInfo | None = None
# GUI 模式下 run_gui 的 (args, run_func)，失败弹窗重试时重建 worker 用。
_retry_context: tuple | None = None


_original_run_incremental = updater_main.run_incremental
_original_apply_part = updater_main._apply_part
_original_retry_on_permission_error = updater_main._retry_on_permission_error

# 一次 run_incremental 执行期间累计的「持续占用」描述；其包装层据此决定
# 是否跳过全量兜底。目录占时时全量路径要 rename 的 _internal 是增量目标的
# 超集，数学上不可能成功，只会白白多下载一次全量包。
_persistent_lock_detail: list[str] = []


def _retry_workbench(op_desc, func, log, max_retries=None, interval=FILE_LOCK_RETRY_INTERVAL):
    """SUG _retry_on_permission_error 的工作台版：3s 等差 + 耗尽时点名占用进程。

    行为与原版一致（重试 PermissionError 与 WinError 5/32，其余 OSError 直接
    抛出）；唯一差异是耗尽后若能通过 Restart Manager 找到占用方，改抛携带
    进程清单的 :class:`PersistentFileLock`，让上游错误消息直接可操作。
    """
    if max_retries is None:
        max_retries = updater_main.FILE_LOCK_RETRY_COUNT
    last_exc: BaseException = OSError("no attempt made")
    for attempt in range(1, max_retries + 1):
        try:
            return func()
        except PermissionError as exc:
            last_exc = exc
        except OSError as exc:
            # WinError 5 (拒绝访问) / 32 (文件被占用) 同样视为可重试
            if getattr(exc, "winerror", None) in (5, 32):
                last_exc = exc
            else:
                raise
        log.warning(
            "%s 第 %d/%d 次失败：%s；%.1fs 后重试…",
            op_desc, attempt, max_retries, last_exc, interval,
        )
        time.sleep(interval)
    entries = lock_diag.find_lockers_for_exception(last_exc)
    if not entries:
        raise last_exc
    holders = lock_diag.format_lockers(entries)
    log.error("%s 持续被占用：%s", op_desc, holders)
    _persistent_lock_detail.append(f"{op_desc}：{holders}")
    global _blocked_lock
    _blocked_lock = BlockedLockInfo(entries=entries, detail=holders)
    raise PersistentFileLock(f"{last_exc}（{holders}）") from last_exc


def _run_incremental_workbench(args, manifest, work_dir, log):
    """SUG run_incremental 的工作台版：目录被占用时中止而非回退全量。"""
    _persistent_lock_detail.clear()
    detail = ""
    try:
        rc = _original_run_incremental(args, manifest, work_dir, log)
        detail = "；".join(_persistent_lock_detail)
    finally:
        _persistent_lock_detail.clear()
    if rc == 33 and detail:
        log.error(
            "安装目录被其他程序占用，增量更新失败；全量路径需要重命名同一目录，"
            "已跳过注定失败的全量下载。"
        )
        raise UpdateBlockedByLock(
            f"更新被占用阻止：{detail}。请关闭相关程序或重启电脑后重新尝试更新。"
        ) from None
    return rc


def _apply_part_workbench(part_zip, targets, app_dir, work_dir, part_id, log):
    """SUG _apply_part 的工作台版：备份清理失败不再静默。"""
    ok, err = _original_apply_part(part_zip, targets, app_dir, work_dir, part_id, log)
    if ok:
        leftovers = [
            rel for rel in targets
            if isinstance(rel, str) and (app_dir / (rel + ".bak")).exists()
        ]
        if leftovers:
            log.error(
                "[%s] 备份清理失败，残留：%s（下次更新会先尝试清理残留，不影响本次更新结果）",
                part_id, ", ".join(leftovers),
            )
    return ok, err


# ───────────────────────── 占用进程恢复弹窗 ─────────────────────────


def _classify_lock_entries(entries):
    """把 RM 占用名单分为「可由更新器结束」与「只展示不结束」两组。

    匹配用真实镜像名（RM 的友好名是本地化文案，不可靠）；镜像名查不到时退回
    友好名。本程序家族（主程序双名 / 渲染 sidecar / 更新器残留副本）视为可结束，
    但弹窗文案会提示其中可能有用户正在使用的实例。
    """
    killable = []
    blocked = []
    own_pid = os.getpid()
    for pid, name in entries:
        if pid == own_pid:
            continue
        image = lock_diag.process_image_name(pid)
        image = os.path.basename(image).lower() if image else ""
        if not image:
            # 镜像名查不到（进程已退出等）时退回友好名兜底匹配
            image = (name or "").strip().lower()
        if not image:
            continue
        if image in _PROTECTED_PROCESS_IMAGES:
            blocked.append((pid, name or image, image))
        else:
            killable.append((pid, name or image, image))
    return killable, blocked


def _restart_update_worker(win) -> bool:
    """用 run_gui 时记录的 (args, run_func) 重建 worker 并重新执行更新。"""
    if _retry_context is None:
        return False
    args, run_func = _retry_context
    from PyQt6.QtCore import Qt

    from updater_app import gui as updater_gui

    # 与 run_gui 内部一致的日志桥接（格式串为 gui.py 的镜像）
    bridge = updater_gui._SignalBridge(win)
    handler = updater_gui._SignalLogHandler(bridge)
    handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s %(message)s", "%H:%M:%S"))
    bridge.log_signal.connect(win.append_log, Qt.ConnectionType.QueuedConnection)

    worker = updater_gui._UpdaterWorker(args, run_func, handler)
    worker.finished.connect(win.on_finished, Qt.ConnectionType.QueuedConnection)
    # 钉住引用防止 Qt 对象被 GC
    win._workbench_retry_artifacts = (bridge, handler, worker)

    win._running = True
    try:
        win._btn.setText(updater_gui._tr("取消更新"))
    except Exception:
        pass
    worker.start()
    return True


def _offer_lock_recovery(win) -> bool:
    """在 GUI 线程展示占用进程清单，可选结束可结束的进程后重试更新。

    返回 True 表示用户选择了重试且 worker 已重启（调用方跳过默认失败收尾）。
    恢复流程自身的任何异常都吞掉并退回默认失败展示——恢复手段不能变成新的
    故障源。
    """
    info = _blocked_lock
    if info is None or not info.entries:
        return False

    killable, blocked = _classify_lock_entries(info.entries)
    lines = "\n".join(f"{name}（PID {pid}）" for pid, name in info.entries)
    blocked_note = ""
    if blocked:
        shown = "、".join(name for _pid, name, _image in blocked)
        blocked_note = f"\n以下进程不会被更新器结束：{shown}。"

    if killable:
        retry_label = "结束占用进程并重试"
        informative_text = None  # 弹窗默认文案
    else:
        retry_label = "重试"
        informative_text = "占用方不适合由更新器结束。\n请重启电脑后再次尝试更新。"

    from krok_helper.updater_app import lock_dialog

    dialog = lock_dialog.LockRecoveryDialog(
        "以下程序正在占用安装目录，导致无法替换文件：",
        lines + blocked_note,
        retry_label,
        parent=win,
        informative_text=informative_text,
    )
    dialog.exec()

    if not dialog.retry_requested:
        return False
    for pid, name, _image in killable:
        win.append_log(f"正在结束占用进程：{name}（PID {pid}）")
        if not lock_diag.kill_pid(pid):
            win.append_log(f"结束 {name}（PID {pid}）失败，将直接重试更新")
    win.append_log("正在重试更新…")
    return _restart_update_worker(win)


class _WorkbenchProductFilter(logging.Filter):
    """Replace SUG's hard-coded updater brand in all workbench log outputs."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = record.msg.replace(
                "StrangeUtaGame Updater",
                "Lin-K Lyrics Updater",
            )
        return True


_original_setup_logger = updater_main.setup_logger
_original_cleanup_temp_workdir = updater_main._cleanup_temp_workdir
_original_apply_update = updater_main.apply_update

PRIMARY_APP_EXE_NAME = "Lin-K Lyrics.exe"


def _setup_workbench_logger(log_path):
    logger = _original_setup_logger(log_path)
    if not any(isinstance(item, _WorkbenchProductFilter) for item in logger.filters):
        logger.addFilter(_WorkbenchProductFilter())
    return logger


def _cleanup_workbench_temp_workdir(work_dir) -> None:
    """Preserve parts handed off by the main app while cleaning other stale files."""
    global _blocked_lock
    _blocked_lock = None  # run() 开头会调用本函数，顺带复位上一次运行的占用记录

    parts_dir = work_dir / "parts"
    handoff_dir = work_dir / "parts.handoff"
    preserved = False

    if parts_dir.is_dir():
        if handoff_dir.exists():
            shutil.rmtree(str(handoff_dir), ignore_errors=True)
        try:
            parts_dir.rename(handoff_dir)
            preserved = True
        except OSError:
            # Keeping an unverified cache is safe: _download_part validates its
            # content hash before reuse and deletes mismatches.
            return

    try:
        _original_cleanup_temp_workdir(work_dir)
    finally:
        if preserved and handoff_dir.exists():
            try:
                handoff_dir.rename(parts_dir)
            except OSError:
                # Do not turn cleanup into a fatal updater startup failure.
                pass


def _apply_workbench_update(app_dir, app_exe, internal_name, new_root, log):
    """Keep the renamed primary EXE when an old client requests the legacy name.

    SUG's generic full-package updater copies only the EXE named by ``--app-exe``.
    Existing workbench installs pass ``Karaoke Studio.exe``, while renamed release
    packages intentionally contain both that compatibility copy and
    ``Lin-K Lyrics.exe``.  Preserve the generic updater's rollback behavior, then
    add the renamed entry point after the legacy target has updated successfully.
    """

    ok, error = _original_apply_update(app_dir, app_exe, internal_name, new_root, log)
    if not ok or app_exe == PRIMARY_APP_EXE_NAME:
        return ok, error

    primary_source = new_root / PRIMARY_APP_EXE_NAME
    if not primary_source.is_file():
        return False, f"更新包中找不到 {PRIMARY_APP_EXE_NAME}"

    try:
        shutil.copy2(str(primary_source), str(app_dir / PRIMARY_APP_EXE_NAME))
    except OSError as exc:
        return False, f"写入 {PRIMARY_APP_EXE_NAME} 失败: {exc}"
    log.info("已写入改名后的主程序 %s", PRIMARY_APP_EXE_NAME)
    return True, ""


def _configure_product() -> None:
    updater_main.TMP_DIR_NAME = "KaraokeStudioUpdater"
    updater_main.DEFAULT_USER_AGENT = "KaraokeStudio-Updater/standalone"
    updater_main.setup_logger = _setup_workbench_logger
    updater_main._cleanup_temp_workdir = _cleanup_workbench_temp_workdir
    updater_main.apply_update = _apply_workbench_update
    # 文件锁相关工作台口径（详见各函数 docstring）：
    # - 3s 等差重试 + 耗尽时点名占用进程；
    # - 目录被占用时跳过注定失败的全量兜底；
    # - 备份清理失败不再静默。
    # run_incremental / _apply_part 的调用点都在 SUG 模块内按全局名解析，
    # 替换模块属性即可生效。
    updater_main._retry_on_permission_error = _retry_workbench
    updater_main.run_incremental = _run_incremental_workbench
    updater_main._apply_part = _apply_part_workbench


def _enable_gui() -> None:
    """Expose SUG's package-local GUI under its legacy top-level import name."""

    from PyQt6.QtCore import QTimer
    from updater_app import gui as updater_gui

    window_class = updater_gui._UpdaterWindow
    if not getattr(window_class, "_workbench_foreground_patch", False):
        original_show_event = window_class.showEvent

        def _show_event(self, event):
            original_show_event(self, event)

            def _bring_to_front():
                try:
                    self.raise_()
                    self.activateWindow()
                except RuntimeError:
                    pass

            QTimer.singleShot(0, _bring_to_front)

        window_class.showEvent = _show_event
        window_class._workbench_foreground_patch = True

    # 失败收尾挂钩：更新因目录被占用失败时，在 GUI 线程弹出「结束占用进程并
    # 重试」恢复弹窗（on_finished 经 QueuedConnection 回到主线程，弹窗安全）。
    if not getattr(window_class, "_workbench_lock_recovery_patch", False):
        _original_on_finished = window_class.on_finished

        def _on_finished_with_lock_recovery(self, code: int) -> None:
            if code in (6, 99) and _blocked_lock is not None:
                try:
                    if _offer_lock_recovery(self):
                        return
                except Exception:
                    pass  # 恢复流程异常时退回默认失败展示
            _original_on_finished(self, code)

        window_class.on_finished = _on_finished_with_lock_recovery
        window_class._workbench_lock_recovery_patch = True

    # 记录 run_gui 的 (args, run_func) 供恢复弹窗重建 worker；包一层而不是改
    # SUG 源码。SUG main() 在调用时才 ``from gui import run_gui``，取到的是
    # 这里替换后的模块属性。
    if not getattr(updater_gui.run_gui, "_workbench_retry_context_patch", False):
        _original_run_gui = updater_gui.run_gui

        def _run_gui_workbench(args, run_func):
            global _retry_context
            _retry_context = (args, run_func)
            return _original_run_gui(args, run_func)

        _run_gui_workbench._workbench_retry_context_patch = True
        updater_gui.run_gui = _run_gui_workbench

    # SUG's standalone entry point lives beside gui.py and therefore imports it
    # as ``from gui import run_gui``.  The workbench entry point lives in a
    # different package, so provide the same import name without changing the
    # submodule source.
    sys.modules["gui"] = updater_gui


def main(argv: list[str] | None = None, *, use_gui: bool = True) -> int:
    _configure_product()
    if use_gui:
        _enable_gui()
    else:
        # Programmatic callers (notably integration tests) can exercise the
        # updater core without constructing a QApplication.
        return updater_main.run(updater_main.parse_args(argv))

    if argv is None:
        return updater_main.main()
    old_argv = sys.argv[:]
    try:
        sys.argv = [old_argv[0], *argv]
        return updater_main.main()
    finally:
        sys.argv = old_argv

if __name__ == "__main__":
    raise SystemExit(main())
