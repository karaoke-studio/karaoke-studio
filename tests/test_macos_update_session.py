from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from PyQt6.QtWidgets import QWidget

from krok_helper.updater import macos_controller
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


def test_authorization_cancellation_finishes_without_error_or_full_retry(monkeypatch):
    monkeypatch.setattr(macos_controller, "find_host_bundle", lambda: Path("/unused/Test.app"))
    parent = QWidget()
    controller = macos_controller.MacUpdateController(parent, can_install=lambda: True, prepare_relaunch=lambda: None)
    errors = []
    controller.finished.connect(errors.append)
    native = session(controller._native_finished)
    controller.native = native
    controller.prepared = SimpleNamespace(delta=object(), close=lambda: None)
    monkeypatch.setattr(controller, "_request_full", lambda: (_ for _ in ()).throw(AssertionError("No retry after cancellation")))
    error = SimpleNamespace(domain=lambda: 'SUSparkleErrorDomain', code=lambda: 4007,
                            localizedDescription=lambda: 'authorization cancelled')
    native.record_error(error)
    native.finish()
    assert native.cancelled and not native.retry_full and controller.completed
    assert errors == ['']


def test_matching_error_number_in_another_domain_is_not_a_user_cancellation():
    native = session(lambda _: None)
    native.record_error(SimpleNamespace(domain=lambda: 'OtherDomain', code=lambda: 4007,
                                        localizedDescription=lambda: 'real failure'))
    assert native.retry_full and not native.cancelled and 'real failure' in native.error_text


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


def test_delta_and_full_downloads_have_distinct_native_resources():
    native = session(lambda _: None)
    native.delta_url = "http://127.0.0.1/token/update.delta"
    assert native.resource_url(True) == native.delta_url
    assert native.resource_url(False) == native.archive_url
    native.delta_url = None
    import pytest
    with pytest.raises(ValueError):
        native.resource_url(True)


def test_recovery_failure_rejects_install_without_quitting():
    from dataclasses import replace

    native = session(lambda _: None)
    def failed_save():
        raise OSError("disk full")
    native.callbacks = replace(native.callbacks, verify_recovery=failed_save)
    assert not native.verify_recovery()
    assert "disk full" in native.error_text
    assert not native.installing and native.install_handler is None


def test_qt_cancellation_closes_cache_server_and_native_cycle_once(monkeypatch):
    monkeypatch.setattr(macos_controller, "find_host_bundle", lambda: Path("/unused/Test.app"))
    parent = QWidget()
    controller = macos_controller.MacUpdateController(parent, can_install=lambda: True, prepare_relaunch=lambda: None)
    events, errors = [], []
    controller.finished.connect(errors.append)
    controller.native = SimpleNamespace(cancel=lambda: events.append("cancel"), close=lambda: events.append("native"))
    controller.server = SimpleNamespace(close=lambda: events.append("server"))
    controller.prepared = SimpleNamespace(close=lambda: events.append("cache"))
    controller.cancel()
    controller.finish("late network error")
    controller.finish("duplicate callback")
    assert events == ["cancel", "native", "server", "cache"]
    assert errors == [""]


def test_native_delta_failure_retries_full_once_but_not_after_recovery_rejection(monkeypatch):
    monkeypatch.setattr(macos_controller, "find_host_bundle", lambda: Path("/unused/Test.app"))
    parent = QWidget()
    controller = macos_controller.MacUpdateController(parent, can_install=lambda: True, prepare_relaunch=lambda: None)
    events = []
    controller.prepared = SimpleNamespace(delta=object(), close=lambda: events.append("cache"))
    controller.native = SimpleNamespace(retry_full=True, close=lambda: events.append("native"))
    controller.server = SimpleNamespace(close=lambda: events.append("server"))
    def full():
        controller.full_attempted = True
        events.append("full")
    monkeypatch.setattr(controller, "_request_full", full)
    controller._native_finished("signature failed")
    assert events == ["full", "native", "server"]
    assert not controller.completed and controller.native is None and controller.server is None
    controller._native_finished("full download failed")
    assert events.count("full") == 1 and controller.completed

    second = macos_controller.MacUpdateController(parent, can_install=lambda: True, prepare_relaunch=lambda: None)
    second.prepared = SimpleNamespace(delta=object(), close=lambda: None)
    second.native = SimpleNamespace(retry_full=False, close=lambda: None)
    monkeypatch.setattr(second, "_request_full", lambda: events.append("unexpected full"))
    second._native_finished("recovery disk full")
    assert second.completed and "unexpected full" not in events
