from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from krok_helper.updater.macos_sparkle import SparkleCallbacks, SparkleSession


def callbacks(completed):
    return SparkleCallbacks(
        status=lambda _: None, cancellable=lambda _: None,
        can_install=lambda: True, prepare_relaunch=lambda: None,
        schedule=lambda action: action(), quit_application=lambda: None,
        completed=completed,
    )


def session(completed):
    return SparkleSession(
        Path("/unused/Test.app"), build_version="1.0.1000",
        feed_url="http://127.0.0.1/feed", archive_url="http://127.0.0.1/archive",
        callbacks=callbacks(completed),
    )


def test_reply_blocks_are_released_and_called_only_once():
    events = []
    native = session(lambda _: None)
    native.cancel_callback = lambda: events.append("cancel")
    native.cancel()
    native.cancel()
    native.install_handler = lambda: events.append("install")
    native.continue_install()
    native.continue_install()
    assert events == ["cancel", "install"]
    assert native.cancel_callback is None and native.install_handler is None


def test_cancelled_native_cycle_completes_once_without_error():
    errors = []
    native = session(errors.append)
    native.error_text = "download error"
    native.cancel()
    native.finish()
    native.finish()
    assert errors == [""]


def test_completed_cycle_retains_error_and_releases_driver_owners():
    errors = []
    native = session(errors.append)
    driver = SimpleNamespace(owner=native)
    delegate = SimpleNamespace(owner=native)
    native.driver, native.delegate = driver, delegate
    native.error_text = "signature failed"
    native.finish()
    assert errors == ["signature failed"]
    native.close()
    assert driver.owner is None and delegate.owner is None
    assert native.updater is None and native.driver is None and native.delegate is None


def test_native_adapter_import_has_no_qt_dependency():
    result = subprocess.run(
        [sys.executable, "-S", "-c", "import sys; import krok_helper.updater.macos_sparkle; assert 'PyQt6' not in sys.modules"],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
