from __future__ import annotations

import logging
from pathlib import Path

from krok_helper import logging_config


def test_get_log_dir_honors_explicit_environment(monkeypatch, tmp_path: Path) -> None:
    target = tmp_path / "diagnostics"
    monkeypatch.setenv(logging_config.LOG_DIR_ENV, str(target))

    assert logging_config.get_log_dir() == target
    assert logging_config.get_log_path() == target / logging_config.LOG_FILE_NAME


def test_configure_application_logging_writes_rotating_redacted_log(tmp_path: Path) -> None:
    root_logger = logging.getLogger()
    old_level = root_logger.level
    log_path = logging_config.configure_application_logging(
        tmp_path,
        enable_native_crash_log=False,
        install_exception_hooks=False,
        install_qt_handler=False,
    )
    handler = next(
        item
        for item in root_logger.handlers
        if getattr(item, logging_config._HANDLER_MARKER, False)
    )
    try:
        assert isinstance(handler, logging.handlers.RotatingFileHandler)
        assert handler.maxBytes == logging_config.MAX_LOG_BYTES
        assert handler.backupCount == logging_config.LOG_BACKUP_COUNT

        logging.getLogger("tests.logging").error(
            "request=https://alice:secret@example.test/file?access_token=abc123 Cookie: session-secret"
        )
        handler.flush()
        contents = log_path.read_text(encoding="utf-8")
        assert "tests.logging" in contents
        assert "alice" not in contents
        assert "secret" not in contents
        assert "abc123" not in contents
        assert "session-secret" not in contents
        assert "https://***:***@example.test/file?access_token=***" in contents
    finally:
        root_logger.removeHandler(handler)
        handler.close()
        root_logger.setLevel(old_level)
        logging_config._configured_log_path = None
