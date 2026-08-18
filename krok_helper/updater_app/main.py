from __future__ import annotations

import logging
import shutil
import sys

from krok_helper import ensure_sug_root_path

ensure_sug_root_path()
from updater_app import main as updater_main


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
