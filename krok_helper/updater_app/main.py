from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime

from krok_helper import ensure_sug_root_path
from krok_helper.updater_app import diagnostics, lock_diag

ensure_sug_root_path()
from updater_app import main as updater_main


# 工作台口径的文件锁重试间隔：等差、3s（SUG 默认 1.5s）。占用方多为常驻句柄，
# 单纯加次数帮助有限，拉长单次等待更稳妥。
FILE_LOCK_RETRY_INTERVAL = 3.0
_LOG_HISTORY_DIR_NAME = "log-history"
_LOG_HISTORY_KEEP_COUNT = 10

# 更新器只能结束明确属于本产品的进程。未知进程、Explorer、系统进程和安全软件
# 一律只展示；黑名单无法穷举，白名单才不会误伤用户的其他程序。
_TERMINABLE_PROCESS_IMAGES = frozenset({
    "lin-k lyrics.exe",
    "karaoke studio.exe",
    "krok_subtitle_renderer.exe",
})
_MANAGED_DESCENDANT_PROCESS_IMAGES = _TERMINABLE_PROCESS_IMAGES | frozenset({
    "python.exe",
    "pythonw.exe",
    "ffmpeg.exe",
    "ffplay.exe",
    "ffprobe.exe",
    "yt-dlp.exe",
    "aria2c.exe",
})
_UPDATE_DESCENDANTS_ENV = "KROK_UPDATE_DESCENDANTS"


class PersistentFileLock(OSError):
    """重命名持续被拒绝，且 Restart Manager 返回了可能的占用进程。"""


@dataclass
class BlockedLockInfo:
    """一次持续占用失败的结构化信息，供 GUI 弹窗提供「结束进程并重试」。"""

    entries: list = field(default_factory=list)  # [(pid, 友好名)]
    detail: str = ""
    diagnostic_path: str = ""


# worker 线程写入（_retry_workbench 抛 PersistentFileLock 时），GUI 线程在
# on_finished 时读取——finished 信号保证 happens-after，无需额外加锁。
_blocked_lock: BlockedLockInfo | None = None
# GUI 模式下 run_gui 的 (args, run_func)，失败弹窗重试时重建 worker 用。
_retry_context: tuple | None = None


_original_run_incremental = updater_main.run_incremental
_original_apply_part = updater_main._apply_part
_original_wait_for_pid_exit = updater_main.wait_for_pid_exit
_original_download_one = updater_main.download_one
_original_verify_content_hash = updater_main.verify_content_hash


def _best_effort_diagnostic(func, *args, **kwargs):
    """Keep every observability hook outside the updater's decision boundary."""
    try:
        return func(*args, **kwargs)
    except Exception:
        return None


def _record_diagnostic_attempt(*args, **kwargs) -> None:
    _best_effort_diagnostic(diagnostics.record_attempt, *args, **kwargs)


def _persist_diagnostic_failure(*args, **kwargs):
    return _best_effort_diagnostic(diagnostics.persist_failure, *args, **kwargs)


def _retry_workbench(op_desc, func, log, max_retries=None, interval=FILE_LOCK_RETRY_INTERVAL):
    """重试被系统拒绝的文件操作，并在诊断后做一次最终尝试。

    行为与原版一致（重试 PermissionError 与 WinError 5/32，其余 OSError 直接
    抛出）。常规次数耗尽后先运行 Restart Manager 诊断，再执行一次最终操作，
    覆盖“最后一次失败后条件已经解除、但旧实现只诊断不再尝试”的竞态窗口。
    """
    global _blocked_lock
    _blocked_lock = None
    if max_retries is None:
        max_retries = updater_main.FILE_LOCK_RETRY_COUNT
    last_exc: BaseException = OSError("no attempt made")
    for attempt in range(1, max_retries + 1):
        started_ns = time.monotonic_ns()
        try:
            result = func()
        except PermissionError as exc:
            last_exc = exc
            _record_diagnostic_attempt(
                op_desc,
                phase="regular",
                attempt=attempt,
                max_attempts=max_retries,
                started_ns=started_ns,
                outcome="failed",
                exc=exc,
            )
        except OSError as exc:
            # WinError 5 (拒绝访问) / 32 (文件被占用) 同样视为可重试
            if getattr(exc, "winerror", None) in (5, 32):
                last_exc = exc
                _record_diagnostic_attempt(
                    op_desc,
                    phase="regular",
                    attempt=attempt,
                    max_attempts=max_retries,
                    started_ns=started_ns,
                    outcome="failed",
                    exc=exc,
                )
            else:
                _record_diagnostic_attempt(
                    op_desc,
                    phase="regular",
                    attempt=attempt,
                    max_attempts=max_retries,
                    started_ns=started_ns,
                    outcome="failed_non_retryable",
                    exc=exc,
                )
                _flush_logger(log)
                _persist_diagnostic_failure(
                    f"{op_desc}: non_retryable_oserror",
                    exc=exc,
                )
                raise
        else:
            _record_diagnostic_attempt(
                op_desc,
                phase="regular",
                attempt=attempt,
                max_attempts=max_retries,
                started_ns=started_ns,
                outcome="succeeded",
            )
            return result
        if attempt < max_retries:
            log.warning(
                "%s 第 %d/%d 次失败：%s；%.1fs 后重试…",
                op_desc, attempt, max_retries, last_exc, interval,
            )
            time.sleep(interval)
        else:
            log.warning(
                "%s 第 %d/%d 次失败：%s；正在诊断后执行最终重试…",
                op_desc, attempt, max_retries, last_exc,
            )

    diagnosis_started_ns = time.monotonic_ns()
    try:
        diagnosis = lock_diag.diagnose_lockers_for_exception(last_exc)
    except Exception as exc:
        diagnosis = lock_diag.RestartManagerResult(
            status="failed",
            stage="unexpected_exception",
            coverage={"exception": f"{type(exc).__name__}: {exc}"},
        )
    _best_effort_diagnostic(
        diagnostics.record_restart_manager,
        op_desc,
        diagnosis,
        started_ns=diagnosis_started_ns,
    )
    entries = diagnosis.entries
    holders = lock_diag.format_lockers(entries) if entries else ""
    if holders:
        log.warning("%s 诊断发现可能使用目标路径的进程：%s", op_desc, holders)
    elif diagnosis.status == "none":
        log.warning(
            "%s Restart Manager 未发现占用进程（覆盖完整=%s，注册文件=%d）",
            op_desc,
            diagnosis.coverage.get("complete", False),
            diagnosis.coverage.get("registered_file_count", 0),
        )
    else:
        log.warning(
            "%s Restart Manager 诊断失败（阶段=%s，返回码=%s）",
            op_desc,
            diagnosis.stage,
            diagnosis.win32_error,
        )

    log.info("%s 诊断完成，执行最终重试", op_desc)
    final_started_ns = time.monotonic_ns()
    try:
        result = func()
    except PermissionError as exc:
        final_exc: BaseException = exc
    except OSError as exc:
        if getattr(exc, "winerror", None) not in (5, 32):
            _record_diagnostic_attempt(
                op_desc,
                phase="final_after_diagnostics",
                attempt=max_retries + 1,
                max_attempts=max_retries + 1,
                started_ns=final_started_ns,
                outcome="failed_non_retryable",
                exc=exc,
            )
            _flush_logger(log)
            _persist_diagnostic_failure(
                f"{op_desc}: final_non_retryable_oserror",
                exc=exc,
            )
            raise
        final_exc = exc
    else:
        _record_diagnostic_attempt(
            op_desc,
            phase="final_after_diagnostics",
            attempt=max_retries + 1,
            max_attempts=max_retries + 1,
            started_ns=final_started_ns,
            outcome="succeeded",
        )
        log.info("%s 最终重试成功", op_desc)
        return result

    _record_diagnostic_attempt(
        op_desc,
        phase="final_after_diagnostics",
        attempt=max_retries + 1,
        max_attempts=max_retries + 1,
        started_ns=final_started_ns,
        outcome="failed",
        exc=final_exc,
    )
    _flush_logger(log)
    bundle_path = _persist_diagnostic_failure(
        f"{op_desc}: final_retry_failed",
        exc=final_exc,
        details={
            "restart_manager_status": diagnosis.status,
            "restart_manager_stage": diagnosis.stage,
            "restart_manager_win32_error": diagnosis.win32_error,
        },
    )
    if bundle_path is not None:
        log.error("诊断资料已保存：%s", bundle_path)
    if not entries:
        raise final_exc
    log.error("%s 最终重试仍被系统拒绝；%s", op_desc, holders)
    _blocked_lock = BlockedLockInfo(
        entries=entries,
        detail=holders,
        diagnostic_path=str(bundle_path) if bundle_path is not None else "",
    )
    raise PersistentFileLock(f"{final_exc}（{holders}）") from final_exc


def _flush_logger(log) -> None:
    """Flush current handlers before copying updater.log into a diagnostic bundle."""
    try:
        for handler in log.handlers:
            try:
                handler.flush()
            except Exception:
                pass
    except Exception:
        pass


def _wait_for_pid_exit_workbench(pid, log, timeout=None) -> bool:
    """Wait for the host and reap descendants left behind by its hard-exit fallback."""

    before = lock_diag.snapshot_processes()
    updater_lineage = lock_diag.process_lineage(os.getpid(), before)
    candidates = lock_diag.descendant_processes(
        pid,
        before,
        exclude_pids=updater_lineage,
    )
    try:
        inherited = json.loads(os.environ.get(_UPDATE_DESCENDANTS_ENV, "[]"))
        for item in inherited if isinstance(inherited, list) else []:
            entry = lock_diag.ProcessTreeEntry(
                pid=int(item["pid"]),
                parent_pid=int(item.get("parent_pid", pid)),
                image_name=str(item.get("image_name", "")),
            )
            if entry.pid > 0 and entry.pid not in updater_lineage:
                candidates.append(entry)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        log.warning("主程序子进程交接快照无效，回退到 Updater 实时进程树")
    resolved_timeout = (
        updater_main.WAIT_PID_TIMEOUT if timeout is None else timeout
    )
    exited = _original_wait_for_pid_exit(pid, log, resolved_timeout)

    after = lock_diag.snapshot_processes()
    updater_lineage.update(lock_diag.process_lineage(os.getpid(), after))
    merged = {entry.pid: entry for entry in candidates}
    for entry in lock_diag.descendant_processes(
        pid,
        after,
        exclude_pids=updater_lineage,
    ):
        merged[entry.pid] = entry

    outcomes: list[dict[str, object]] = []
    if not exited:
        for entry in merged.values():
            outcomes.append(
                {
                    "pid": entry.pid,
                    "parent_pid": entry.parent_pid,
                    "image_name": entry.image_name,
                    "outcome": "parent_still_running",
                }
            )
    else:
        # Children first: a parent cannot immediately recreate a child after the
        # latter has been reaped, and the Updater's own bootloader lineage is excluded.
        for entry in reversed(list(merged.values())):
            if entry.pid in updater_lineage or entry.pid == os.getpid():
                continue
            if not updater_main._is_pid_alive(entry.pid):
                outcome = "already_exited"
            else:
                current_name = lock_diag.process_image_name(entry.pid)
                if not current_name:
                    outcome = "image_unverified_skipped"
                    log.warning(
                        "无法验证残留子进程身份，保留并写入诊断: PID=%d",
                        entry.pid,
                    )
                elif (
                    current_name
                    and entry.image_name
                    and current_name.casefold() != entry.image_name.casefold()
                ):
                    outcome = "pid_reused_skipped"
                elif current_name.casefold() not in (
                    _MANAGED_DESCENDANT_PROCESS_IMAGES
                ):
                    outcome = "unmanaged_process_skipped"
                    log.warning(
                        "主程序退出后仍有未识别子进程，保留并写入诊断: %s (PID=%d)",
                        current_name,
                        entry.pid,
                    )
                else:
                    shown_name = current_name
                    log.warning(
                        "主程序退出后仍有子进程存活，正在结束: %s (PID=%d)",
                        shown_name,
                        entry.pid,
                    )
                    outcome = (
                        "terminated" if lock_diag.kill_pid(entry.pid) else "terminate_failed"
                    )
            outcomes.append(
                {
                    "pid": entry.pid,
                    "parent_pid": entry.parent_pid,
                    "image_name": entry.image_name,
                    "outcome": outcome,
                }
            )

    if outcomes:
        diagnostics.record_process_cleanup(
            {
                "parent_pid": int(pid),
                "parent_exited": bool(exited),
                "updater_lineage": sorted(updater_lineage),
                "processes": outcomes,
            }
        )
        terminated = sum(item["outcome"] == "terminated" for item in outcomes)
        failed = sum(item["outcome"] == "terminate_failed" for item in outcomes)
        log.info(
            "主程序子进程清理完成：发现 %d，结束 %d，失败 %d",
            len(outcomes),
            terminated,
            failed,
        )
        if terminated:
            time.sleep(0.5)
    return exited


def _path_lexists(path) -> bool:
    """Like Path.exists(), but also true for broken links/reparse-point entries."""
    return os.path.lexists(os.fspath(path))


def _remove_stale_backup(path, log, label: str) -> tuple[bool, str]:
    """Remove one stale backup and verify that the destination is really free."""
    if not _path_lexists(path):
        return True, ""

    log.info("清理旧备份目标: %s", path)

    def remove_once() -> None:
        if not _path_lexists(path):
            return
        if path.is_symlink() or not path.is_dir():
            path.unlink()
        else:
            shutil.rmtree(str(path), ignore_errors=False)

    try:
        _retry_workbench(f"清理旧备份目标 {label}", remove_once, log)
    except OSError as exc:
        message = f"清理旧备份目标 {label} 失败: {exc}"
        log.error(message)
        return False, message

    if _path_lexists(path):
        message = f"清理旧备份目标 {label} 失败: 删除操作结束后目标仍然存在"
        log.error(message)
        _flush_logger(log)
        _persist_diagnostic_failure(
            f"清理旧备份目标 {label}: verification_failed",
            extra_paths=[path],
            details={"message": message},
        )
        return False, message
    return True, ""


def _run_incremental_workbench(args, manifest, work_dir, log):
    """Keep full fallback enabled and discard stale incremental lock state."""
    rc = _original_run_incremental(args, manifest, work_dir, log)
    if rc != 0:
        global _blocked_lock
        _blocked_lock = None
        _flush_logger(log)
        _persist_diagnostic_failure(
            "incremental_update_failed",
            extra_paths=[getattr(args, "app_dir", "")],
            details={"exit_code": rc},
        )
    return rc


def _discard_invalid_part(zip_path, log, exc: BaseException) -> None:
    """Remove an unreadable part archive so the updater can recover automatically."""
    log.warning("分包缓存不是有效 ZIP，将删除后重新获取：%s（%s）", zip_path.name, exc)
    try:
        zip_path.unlink()
    except FileNotFoundError:
        pass
    except OSError as remove_exc:
        log.warning("删除无效分包缓存失败：%s（%s）", zip_path, remove_exc)


def _verify_content_hash_workbench(zip_path, expected_hex, log) -> bool:
    """Treat malformed or unreadable part archives as cache misses, not fatal errors."""
    try:
        return _original_verify_content_hash(zip_path, expected_hex, log)
    except (
        zipfile.BadZipFile,
        EOFError,
        NotImplementedError,
        OSError,
        RuntimeError,
    ) as exc:
        _discard_invalid_part(zip_path, log, exc)
        return False


def _download_one_workbench(url, dest, proxies, log):
    """Download into a resumable .part file and publish it with an atomic rename."""
    partial = dest.with_name(dest.name + ".part")
    ok, error = _original_download_one(url, partial, proxies, log)
    if not ok:
        # The caller may switch mirrors. Never resume bytes from one source against
        # another source during the same attempt.
        try:
            partial.unlink()
        except FileNotFoundError:
            pass
        except OSError as cleanup_exc:
            log.warning("清理未完成下载失败：%s（%s）", partial, cleanup_exc)
        return False, error
    try:
        # HTTP 200 only proves that bytes arrived. Reject HTML error pages and
        # truncated archives before they can become the canonical cache file.
        with zipfile.ZipFile(str(partial), "r") as archive:
            archive.infolist()
    except (zipfile.BadZipFile, EOFError, OSError) as exc:
        log.warning("下载结果不是有效 ZIP：%s（%s）", partial, exc)
        try:
            partial.unlink()
        except OSError:
            pass
        return False, f"下载结果不是有效 ZIP: {exc}"
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.replace(str(partial), str(dest))
    except OSError as exc:
        log.error("提交下载文件失败：%s → %s（%s）", partial, dest, exc)
        return False, f"提交下载文件失败: {exc}"
    return True, ""


def _apply_part_workbench(part_zip, targets, app_dir, work_dir, part_id, log):
    """SUG _apply_part wrapper with verified stale-backup cleanup."""
    for rel in targets:
        if not isinstance(rel, str) or rel in (
            updater_main.UPDATER_EXE_NAME,
            updater_main.UPDATER_EX_NAME,
        ):
            continue
        backup = app_dir / (rel + ".bak")
        ok, error = _remove_stale_backup(backup, log, rel + ".bak")
        if not ok:
            return False, error

    ok, err = _original_apply_part(part_zip, targets, app_dir, work_dir, part_id, log)
    if not ok:
        _flush_logger(log)
        target_paths = [app_dir / rel for rel in targets if isinstance(rel, str)]
        target_paths.extend(
            app_dir / (rel + ".bak") for rel in targets if isinstance(rel, str)
        )
        _persist_diagnostic_failure(
            f"incremental_part_{part_id}_failed",
            extra_paths=target_paths,
            details={"part_id": part_id, "error": err},
        )
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

    只有能查到真实镜像名、且镜像名属于本程序白名单的进程可结束。RM 友好名
    可本地化、也不能证明进程身份，查询不到镜像路径时必须保守地只展示。
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
            blocked.append((pid, name or "未知进程", ""))
            continue
        if image in _TERMINABLE_PROCESS_IMAGES:
            killable.append((pid, name or image, image))
        else:
            blocked.append((pid, name or image, image))
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
    for name in ("_workbench_open_diagnostics", "_workbench_copy_diagnostics"):
        button = getattr(win, name, None)
        if button is not None:
            button.hide()
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
    if info.diagnostic_path:
        lines += f"\n诊断资料：{info.diagnostic_path}"
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
        "目录重命名被系统拒绝；检测到以下程序可能正在使用相关文件：",
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


def _open_latest_diagnostic_bundle() -> None:
    path = diagnostics.latest_bundle_path()
    if path is None:
        return
    try:
        from PyQt6.QtCore import QUrl
        from PyQt6.QtGui import QDesktopServices

        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
    except Exception:
        pass


def _copy_latest_diagnostic_path() -> None:
    path = diagnostics.latest_bundle_path()
    if path is None:
        return
    try:
        from PyQt6.QtWidgets import QApplication

        QApplication.clipboard().setText(str(path))
    except Exception:
        pass


def _show_diagnostic_actions(win) -> None:
    """Expose the persisted bundle without making GUI recovery failure fatal."""
    path = diagnostics.latest_bundle_path()
    if path is None:
        return
    try:
        from qfluentwidgets import PushButton

        open_button = getattr(win, "_workbench_open_diagnostics", None)
        copy_button = getattr(win, "_workbench_copy_diagnostics", None)
        if open_button is None or copy_button is None:
            root_layout = win.layout()
            bottom_layout = root_layout.itemAt(root_layout.count() - 1).layout()
            open_button = PushButton("打开诊断目录", win)
            copy_button = PushButton("复制诊断路径", win)
            open_button.setFixedWidth(120)
            copy_button.setFixedWidth(120)
            open_button.clicked.connect(_open_latest_diagnostic_bundle)
            copy_button.clicked.connect(_copy_latest_diagnostic_path)
            bottom_layout.insertWidget(0, open_button)
            bottom_layout.insertWidget(1, copy_button)
            win._workbench_open_diagnostics = open_button
            win._workbench_copy_diagnostics = copy_button
        open_button.setToolTip(str(path))
        copy_button.setToolTip(str(path))
        open_button.show()
        copy_button.show()
        win.append_log(f"诊断资料已保存：{path}")
    except Exception:
        pass


class _WorkbenchProductFilter(logging.Filter):
    """Replace SUG's hard-coded updater brand in all workbench log outputs."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = record.msg.replace(
                "StrangeUtaGame Updater",
                "Lin-K Lyrics Updater",
            )
        return True


_original_cleanup_temp_workdir = updater_main._cleanup_temp_workdir
_original_apply_update = updater_main.apply_update

PRIMARY_APP_EXE_NAME = "Lin-K Lyrics.exe"


def _close_logger_handlers(logger: logging.Logger) -> None:
    """Detach and close handlers left by a previous updater attempt."""
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        try:
            handler.flush()
        except Exception:
            pass
        try:
            handler.close()
        except Exception:
            pass


def _archive_previous_log(log_path) -> bool:
    """Archive a non-empty updater.log before a new attempt starts."""
    try:
        if not log_path.is_file() or log_path.stat().st_size == 0:
            return True
        history_dir = log_path.parent / _LOG_HISTORY_DIR_NAME
        history_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        archived = history_dir / f"updater-{stamp}-pid{os.getpid()}.log"
        os.replace(str(log_path), str(archived))

        history = sorted(
            history_dir.glob("updater-*.log"),
            key=lambda item: (item.stat().st_mtime_ns, item.name),
            reverse=True,
        )
        for stale in history[_LOG_HISTORY_KEEP_COUNT:]:
            try:
                stale.unlink()
            except OSError:
                pass
        return True
    except OSError:
        return False


def _setup_workbench_logger(log_path):
    logger = logging.getLogger("sug.updater")
    _close_logger_handlers(logger)
    archived = _archive_previous_log(log_path)

    logger.setLevel(logging.INFO)
    formatter = logging.Formatter(updater_main.LOG_FORMAT, updater_main.DATE_FORMAT)

    console = logging.StreamHandler(stream=sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(formatter)
    logger.addHandler(console)

    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        # If archiving was blocked, append rather than destroy the only copy.
        file_handler = logging.FileHandler(
            str(log_path),
            mode="w" if archived else "a",
            encoding="utf-8",
        )
        file_handler.setLevel(logging.INFO)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except OSError:
        pass

    if not any(isinstance(item, _WorkbenchProductFilter) for item in logger.filters):
        logger.addFilter(_WorkbenchProductFilter())
    if not archived:
        logger.warning("无法归档上一份 updater.log，本次日志将追加写入以避免丢失")
    return logger


def _run_with_diagnostics(args, run_func, *run_args, **run_kwargs):
    """Run one updater attempt with a non-interfering diagnostic session."""
    _best_effort_diagnostic(diagnostics.begin_session, args)
    try:
        code = run_func(args, *run_args, **run_kwargs)
    except Exception as exc:
        log = logging.getLogger("sug.updater")
        log.exception("更新器发生未处理异常")
        _flush_logger(log)
        _best_effort_diagnostic(diagnostics.finish_session, 99, exc=exc)
        _flush_logger(log)
        return 99
    except BaseException as exc:
        _best_effort_diagnostic(diagnostics.finish_session, 99, exc=exc)
        raise
    _best_effort_diagnostic(diagnostics.finish_session, code)
    _flush_logger(logging.getLogger("sug.updater"))
    return code


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

    # Validate the package before touching any recovery backup.  The generic
    # implementation performs the same validation, but its cleanup silently
    # ignores failures and then continues into rename.
    if not (new_root / app_exe).is_file():
        error = f"更新包中找不到 {app_exe}"
        _persist_diagnostic_failure(
            "full_update_package_validation_failed",
            extra_paths=[new_root / app_exe],
            details={"error": error},
        )
        return False, error
    if not (new_root / internal_name).is_dir():
        error = f"更新包中找不到 {internal_name}/"
        _persist_diagnostic_failure(
            "full_update_package_validation_failed",
            extra_paths=[new_root / internal_name],
            details={"error": error},
        )
        return False, error

    backup_targets = (
        (app_dir / internal_name, app_dir / f"{internal_name}.old", f"{internal_name}.old"),
        (app_dir / app_exe, app_dir / f"{app_exe}.old", f"{app_exe}.old"),
    )
    for current, backup, label in backup_targets:
        if not _path_lexists(current):
            continue
        ok, error = _remove_stale_backup(backup, log, label)
        if not ok:
            return False, error

    ok, error = _original_apply_update(app_dir, app_exe, internal_name, new_root, log)
    if not ok:
        error = error.replace(
            "（主程序可能仍未完全释放文件句柄）",
            "（目录重命名被系统拒绝）",
        ).replace(
            "（主程序可能未完全退出）",
            "（文件重命名被系统拒绝）",
        )
        _flush_logger(log)
        _persist_diagnostic_failure(
            "full_update_apply_failed",
            extra_paths=[
                app_dir / internal_name,
                app_dir / f"{internal_name}.old",
                app_dir / app_exe,
                app_dir / f"{app_exe}.old",
            ],
            details={"error": error},
        )
    if not ok or app_exe == PRIMARY_APP_EXE_NAME:
        return ok, error

    primary_source = new_root / PRIMARY_APP_EXE_NAME
    if not primary_source.is_file():
        error = f"更新包中找不到 {PRIMARY_APP_EXE_NAME}"
        _persist_diagnostic_failure(
            "full_update_package_validation_failed",
            extra_paths=[primary_source],
            details={"error": error},
        )
        return False, error

    try:
        shutil.copy2(str(primary_source), str(app_dir / PRIMARY_APP_EXE_NAME))
    except OSError as exc:
        _flush_logger(log)
        _persist_diagnostic_failure(
            "primary_executable_copy_failed",
            exc=exc,
            extra_paths=[primary_source, app_dir / PRIMARY_APP_EXE_NAME],
        )
        return False, f"写入 {PRIMARY_APP_EXE_NAME} 失败: {exc}"
    log.info("已写入改名后的主程序 %s", PRIMARY_APP_EXE_NAME)
    return True, ""


def _launch_main_app_workbench(app_dir, app_exe, log) -> bool:
    """Launch the updated frozen app as a fresh PyInstaller instance."""
    exe_path = app_dir / app_exe
    if not exe_path.exists():
        log.error("找不到主程序 EXE: %s", exe_path)
        return False

    log.info("启动新版本: %s", exe_path)
    flags = 0
    if sys.platform == "win32":
        flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    env = os.environ.copy()
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    env.pop(_UPDATE_DESCENDANTS_ENV, None)
    try:
        subprocess.Popen(  # noqa: S603
            [str(exe_path)],
            cwd=str(app_dir),
            env=env,
            close_fds=True,
            creationflags=flags,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except OSError as exc:
        log.error("启动主程序失败: %s", exc)
        return False


def _configure_product() -> None:
    updater_main.TMP_DIR_NAME = "KaraokeStudioUpdater"
    updater_main.DEFAULT_USER_AGENT = "KaraokeStudio-Updater/standalone"
    updater_main.setup_logger = _setup_workbench_logger
    updater_main._cleanup_temp_workdir = _cleanup_workbench_temp_workdir
    updater_main.apply_update = _apply_workbench_update
    updater_main.launch_main_app = _launch_main_app_workbench
    # 文件锁相关工作台口径（详见各函数 docstring）：
    # - 3s 间隔重试 + 诊断后的最终尝试；
    # - 增量失败仍保留全量兜底；
    # - 备份目标清理失败不再静默。
    # run_incremental / _apply_part 的调用点都在 SUG 模块内按全局名解析，
    # 替换模块属性即可生效。
    updater_main._retry_on_permission_error = _retry_workbench
    updater_main.wait_for_pid_exit = _wait_for_pid_exit_workbench
    updater_main.download_one = _download_one_workbench
    updater_main.verify_content_hash = _verify_content_hash_workbench
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
            if code != 0:
                _show_diagnostic_actions(self)

        window_class.on_finished = _on_finished_with_lock_recovery
        window_class._workbench_lock_recovery_patch = True

    # 记录 run_gui 的 (args, run_func) 供恢复弹窗重建 worker；包一层而不是改
    # SUG 源码。SUG main() 在调用时才 ``from gui import run_gui``，取到的是
    # 这里替换后的模块属性。
    if not getattr(updater_gui.run_gui, "_workbench_retry_context_patch", False):
        _original_run_gui = updater_gui.run_gui

        def _run_gui_workbench(args, run_func):
            global _retry_context

            def diagnosed_run(run_args, *extra_args, **kwargs):
                return _run_with_diagnostics(
                    run_args,
                    run_func,
                    *extra_args,
                    **kwargs,
                )

            _retry_context = (args, diagnosed_run)
            return _original_run_gui(args, diagnosed_run)

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
        args = updater_main.parse_args(argv)
        return _run_with_diagnostics(args, updater_main.run)

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
