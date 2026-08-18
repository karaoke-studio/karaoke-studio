from __future__ import annotations

import atexit
import faulthandler
import logging
import logging.handlers
import os
import platform
import re
import sys
import tempfile
import threading
from pathlib import Path
from typing import TextIO

from krok_helper.config import APP_NAME, APP_VERSION
from krok_helper.app_paths import (
    SETTINGS_APP_NAME_ENV,
    SETTINGS_DIR_ENV,
    consume_migration_notes,
)


LOG_DIR_ENV = "KARAOKE_STUDIO_LOG_DIR"
LOG_FILE_NAME = "lin-k-lyrics.log"
NATIVE_CRASH_FILE_NAME = "native-crash.log"
MAX_LOG_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 5

_HANDLER_MARKER = "_karaoke_studio_file_handler"
_configured_log_path: Path | None = None
_crash_stream: TextIO | None = None
_hooks_installed = False
_qt_handler_installed = False
_qt_message_handler_callback = None
_shutdown_registered = False


def get_log_dir() -> Path:
    explicit = os.getenv(LOG_DIR_ENV)
    if explicit:
        return Path(explicit).expanduser()

    settings_dir = os.getenv(SETTINGS_DIR_ENV)
    if settings_dir:
        return Path(settings_dir).expanduser() / "logs"

    app_dir_name = os.getenv(SETTINGS_APP_NAME_ENV, APP_NAME).strip() or APP_NAME
    appdata = os.getenv("APPDATA")
    if os.name == "nt" and appdata:
        return Path(appdata) / app_dir_name / "logs"

    state_home = os.getenv("XDG_STATE_HOME")
    if state_home:
        return Path(state_home) / app_dir_name.lower().replace(" ", "-") / "logs"
    return Path.home() / ".local" / "state" / app_dir_name.lower().replace(" ", "-") / "logs"


def get_log_path() -> Path:
    return get_log_dir() / LOG_FILE_NAME


def get_active_log_dir() -> Path:
    if _configured_log_path is not None:
        return _configured_log_path.parent
    return get_log_dir()


def _redact_sensitive_text(text: str) -> str:
    text = re.sub(
        r"(?i)(https?://)([^\s/@:]+):([^\s/@]+)@",
        r"\1***:***@",
        text,
    )
    text = re.sub(
        r"(?i)\b(authorization|proxy-authorization|cookie|set-cookie)\s*[:=]\s*([^\r\n]+)",
        r"\1: ***",
        text,
    )
    text = re.sub(
        r"(?i)([?&](?:access_token|token|api_key|apikey|key|auth|sessdata|bili_jct)=)[^&#\s]+",
        r"\1***",
        text,
    )
    return text


class _SafeFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return _redact_sensitive_text(super().format(record))


def _rotate_plain_file(path: Path, *, backup_count: int = 2) -> None:
    if not path.exists() or path.stat().st_size < MAX_LOG_BYTES:
        return
    oldest = path.with_name(f"{path.name}.{backup_count}")
    if oldest.exists():
        oldest.unlink()
    for index in range(backup_count - 1, 0, -1):
        source = path.with_name(f"{path.name}.{index}")
        if source.exists():
            source.replace(path.with_name(f"{path.name}.{index + 1}"))
    path.replace(path.with_name(f"{path.name}.1"))


def _enable_native_crash_log(log_dir: Path) -> None:
    global _crash_stream
    if _crash_stream is not None:
        return
    crash_path = log_dir / NATIVE_CRASH_FILE_NAME
    try:
        _rotate_plain_file(crash_path)
        _crash_stream = crash_path.open("a", encoding="utf-8", buffering=1)
        _crash_stream.write(
            f"\n===== session pid={os.getpid()} version={APP_VERSION} executable={sys.executable} =====\n"
        )
        _crash_stream.flush()
        faulthandler.enable(file=_crash_stream, all_threads=True)
    except Exception:
        logging.getLogger(__name__).exception("无法启用原生崩溃日志")


def _install_exception_hooks() -> None:
    global _hooks_installed
    if _hooks_installed:
        return

    previous_sys_hook = sys.excepthook
    previous_thread_hook = threading.excepthook
    previous_unraisable_hook = sys.unraisablehook

    def sys_hook(exc_type, exc_value, exc_tb) -> None:
        logging.getLogger("krok_helper.crash").critical(
            "主线程发生未捕获异常",
            exc_info=(exc_type, exc_value, exc_tb),
        )
        previous_sys_hook(exc_type, exc_value, exc_tb)

    setattr(sys_hook, "_karaoke_studio_logging_hook", True)

    def thread_hook(args: threading.ExceptHookArgs) -> None:
        thread_name = args.thread.name if args.thread is not None else "unknown"
        logging.getLogger("krok_helper.crash").critical(
            "后台线程发生未捕获异常: %s",
            thread_name,
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )
        previous_thread_hook(args)

    def unraisable_hook(args) -> None:
        logging.getLogger("krok_helper.crash").error(
            "对象清理阶段发生无法抛出的异常: %s",
            args.err_msg or type(args.object).__name__,
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )
        previous_unraisable_hook(args)

    sys.excepthook = sys_hook
    threading.excepthook = thread_hook
    sys.unraisablehook = unraisable_hook
    _hooks_installed = True


def install_qt_message_logging() -> None:
    global _qt_handler_installed, _qt_message_handler_callback
    if _qt_handler_installed:
        return
    try:
        from PyQt6.QtCore import QtMsgType, qInstallMessageHandler

        levels = {
            QtMsgType.QtDebugMsg: logging.DEBUG,
            QtMsgType.QtInfoMsg: logging.INFO,
            QtMsgType.QtWarningMsg: logging.WARNING,
            QtMsgType.QtCriticalMsg: logging.ERROR,
            QtMsgType.QtFatalMsg: logging.CRITICAL,
        }

        def qt_message_handler(message_type, context, message) -> None:
            location = ""
            if context is not None and getattr(context, "file", None):
                location = f" ({context.file}:{getattr(context, 'line', 0)})"
            logging.getLogger("qt").log(
                levels.get(message_type, logging.INFO),
                "%s%s",
                message,
                location,
            )

        _qt_message_handler_callback = qt_message_handler
        qInstallMessageHandler(_qt_message_handler_callback)
        _qt_handler_installed = True
    except Exception:
        logging.getLogger(__name__).exception("无法安装 Qt 消息日志处理器")


def _log_shutdown() -> None:
    logging.getLogger(__name__).info("应用进程结束 pid=%s", os.getpid())


def configure_application_logging(
    log_dir: Path | None = None,
    *,
    enable_native_crash_log: bool = True,
    install_exception_hooks: bool = True,
    install_qt_handler: bool = True,
) -> Path:
    global _configured_log_path, _shutdown_registered

    target_dir = Path(log_dir) if log_dir is not None else get_log_dir()
    fallback_reason: OSError | None = None
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        fallback_reason = exc
        target_dir = Path(tempfile.gettempdir()) / "LinKLyrics" / "logs"
        target_dir.mkdir(parents=True, exist_ok=True)
    log_path = target_dir / LOG_FILE_NAME

    root_logger = logging.getLogger()
    handler = next(
        (item for item in root_logger.handlers if getattr(item, _HANDLER_MARKER, False)),
        None,
    )
    if handler is None:
        try:
            handler = logging.handlers.RotatingFileHandler(
                log_path,
                maxBytes=MAX_LOG_BYTES,
                backupCount=LOG_BACKUP_COUNT,
                encoding="utf-8",
            )
        except OSError as exc:
            fallback_reason = fallback_reason or exc
            target_dir = Path(tempfile.gettempdir()) / "LinKLyrics" / "logs"
            target_dir.mkdir(parents=True, exist_ok=True)
            log_path = target_dir / LOG_FILE_NAME
            handler = logging.handlers.RotatingFileHandler(
                log_path,
                maxBytes=MAX_LOG_BYTES,
                backupCount=LOG_BACKUP_COUNT,
                encoding="utf-8",
            )
        setattr(handler, _HANDLER_MARKER, True)
        handler.setLevel(logging.INFO)
        handler.setFormatter(
            _SafeFormatter(
                "%(asctime)s.%(msecs)03d %(levelname)-8s "
                "[%(process)d:%(threadName)s] %(name)s: %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        root_logger.addHandler(handler)
        if root_logger.level == logging.NOTSET or root_logger.level > logging.INFO:
            root_logger.setLevel(logging.INFO)
        _configured_log_path = log_path

        logging.getLogger("urllib3").setLevel(logging.WARNING)
        logging.getLogger("PIL").setLevel(logging.WARNING)
        logging.getLogger(__name__).info("=" * 72)
        logging.getLogger(__name__).info(
            "应用启动 version=%s pid=%s frozen=%s python=%s platform=%s",
            APP_VERSION,
            os.getpid(),
            bool(getattr(sys, "frozen", False)),
            platform.python_version(),
            platform.platform(),
        )
        logging.getLogger(__name__).info(
            "运行路径 executable=%s cwd=%s log=%s",
            sys.executable,
            Path.cwd(),
            log_path,
        )
        if fallback_reason is not None:
            logging.getLogger(__name__).warning(
                "默认日志目录不可写，已回退到临时目录: %s",
                fallback_reason,
            )
        # migrate_app_data_dir 跑在日志就绪之前，它攒下的说明在这里补写。
        for note in consume_migration_notes():
            logging.getLogger(__name__).info("%s", note)
    elif _configured_log_path is None:
        _configured_log_path = Path(getattr(handler, "baseFilename", log_path))

    if enable_native_crash_log:
        _enable_native_crash_log(target_dir)
    if install_exception_hooks:
        _install_exception_hooks()
    if install_qt_handler:
        install_qt_message_logging()
    if not _shutdown_registered:
        atexit.register(_log_shutdown)
        _shutdown_registered = True
    return _configured_log_path or log_path
