"""Qt orchestration: download, install handoff, cancellation and application shutdown."""

from __future__ import annotations

import threading
import multiprocessing as mp
import tempfile
import time
from dataclasses import asdict
from pathlib import Path
from typing import Callable

from PyQt6.QtCore import QObject, QThread, QTimer, Qt, pyqtSignal
from PyQt6.QtWidgets import QApplication, QWidget

from krok_helper.updater.macos_sparkle import SparkleCallbacks, SparkleSession, find_host_bundle
from krok_helper.updater.macos_feed import ASSET_NAME, MacDelta, MacFeed
from krok_helper.updater import macos_transport
from krok_helper.updater.macos_transport import LocalUpdateServer, PreparedMacUpdate, prepare_in_process
from krok_helper.updater.progress_window import UpdateProgressWindow
from krok_helper.updater.settings import UpdaterSettings
from krok_helper.updater.worker import CheckResult


class _PrepareWorker(QThread):
    progress = pyqtSignal(str)

    def __init__(self, result: CheckResult, settings: UpdaterSettings, parent: QObject, *, feed: MacFeed | None = None):
        super().__init__(parent)
        self.result, self.settings = result, settings
        self.feed = feed
        self.cancel_event = threading.Event()
        self.prepared = None
        self.error = ""
        self.process = None

    def run(self) -> None:
        temporary = receiver = sender = None
        try:
            if self.cancel_event.is_set():
                return
            temporary = tempfile.TemporaryDirectory(prefix="Lin-K-macos-update-")
            context = mp.get_context("spawn")
            receiver, sender = context.Pipe(duplex=False)
            release = self.result.release
            if release is None:
                raise ValueError("缺少更新版本信息。")
            spec = {
                "tag": release.tag, "version": release.version,
                "asset": self.result.primary_asset_name,
                "sha256": self.result.primary_sha256,
                "candidates": self.result.download_candidates,
                "sources": self.settings.source_order,
                "proxy_mode": self.settings.proxy_mode,
                "proxy_url": self.settings.proxy_manual_url,
                "feeds": macos_transport.build_release_urls(
                    self.settings.source_order, release.tag, macos_transport.FEED_NAME,
                ),
                "feed": asdict(self.feed) if self.feed else None,
            }
            self.process = context.Process(
                target=prepare_in_process, args=(spec, temporary.name, sender),
                daemon=True,
            )
            self.process.start()
            sender.close()
            latest_status, last_emit = "", 0.0
            done = False
            while not self.cancel_event.is_set() and not done:
                if receiver.poll(0.05):
                    # Collapse a fast download's queued progress before emitting
                    # to Qt; never let progress flood postpone cancellation.
                    for _ in range(256):
                        if self.cancel_event.is_set():
                            break
                        kind, value = receiver.recv()
                        if kind == "prepared":
                            self.prepared = PreparedMacUpdate(
                                temporary, Path(temporary.name) / ASSET_NAME, MacFeed.from_dict(value["feed"]),
                                MacDelta(**value["delta"]) if value["delta"] else None,
                                Path(temporary.name) / value["delta_archive"] if value["delta_archive"] else None,
                            )
                            done = True
                            break
                        if kind == "error":
                            self.error = value
                            done = True
                            break
                        latest_status = value
                        if not receiver.poll():
                            break
                if latest_status and time.monotonic() - last_emit >= 0.05:
                    self.progress.emit(latest_status)
                    latest_status, last_emit = "", time.monotonic()
                if not done and not self.process.is_alive() and not receiver.poll():
                    raise RuntimeError("下载进程提前结束，请重试。")
        except EOFError:
            if not self.cancel_event.is_set():
                self.error = "下载进程提前结束，请重试。"
        except Exception as exc:
            self.error = str(exc)
        finally:
            if self.process is not None:
                if self.process.pid is not None:
                    if self.process.is_alive():
                        self.process.terminate()
                    self.process.join(timeout=0.5)
                    if self.process.is_alive():
                        self.process.kill()
                        self.process.join()
                self.process.close()
            self.process = None
            if receiver is not None:
                receiver.close()
            if sender is not None:
                sender.close()
            if self.cancel_event.is_set() or self.prepared is None:
                if temporary is not None:
                    temporary.cleanup()
                self.prepared = None


class MacUpdateController(QObject):
    finished = pyqtSignal(str)
    full_requested = pyqtSignal()

    def __init__(
        self, parent: QWidget, *, can_install: Callable[[], bool],
        prepare_relaunch: Callable[[], None],
        verify_recovery: Callable[[], None] = lambda: None,
    ):
        bundle = find_host_bundle()
        super().__init__(parent)
        self.bundle = bundle
        self.can_install, self.prepare_relaunch = can_install, prepare_relaunch
        self.verify_recovery = verify_recovery
        self.progress = UpdateProgressWindow(parent)
        self.progress.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.progress.destroyed.connect(self.deleteLater)
        self.progress.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.progress.cancelled.connect(self.cancel)
        self.worker = None
        self.prepared = self.server = self.native = None
        self.fallback_prepared = None
        self.full_attempted = False
        self.result = self.settings = None
        self.full_requested.connect(self._request_full, Qt.ConnectionType.QueuedConnection)
        self.cancelled = self.installing = self.completed = False
        QApplication.instance().aboutToQuit.connect(self.on_quit)

    def start(self, result: CheckResult, settings: UpdaterSettings) -> None:
        self.result, self.settings = result, settings
        self.progress.set_status("正在获取 macOS 更新签名…")
        self.progress.show()
        self.worker = _PrepareWorker(result, settings, self)
        self.worker.progress.connect(self._update_status, Qt.ConnectionType.QueuedConnection)
        self.worker.finished.connect(self.download_finished)
        self.worker.start()

    def cancel(self) -> None:
        if self.completed or self.installing:
            return
        self.cancelled = True
        self.progress.set_status("正在取消更新…")
        self.progress.set_cancellable(False)
        if self.worker:
            self.worker.cancel_event.set()
        if self.native:
            self.native.cancel()

    def _update_status(self, text: str) -> None:
        if not self.completed and not self.cancelled:
            self.progress.set_status(text)

    def download_finished(self) -> None:
        if self.worker is None:
            return
        worker, self.worker = self.worker, None
        prepared, error = worker.prepared, worker.error
        worker.deleteLater()
        if self.cancelled or prepared is None:
            if prepared:
                prepared.close()
            self.finish(error)
            return
        if self.server is not None:
            self.fallback_prepared = prepared
            self.server.supply_full(prepared.archive)
            return
        if self.prepared is not None:
            self.prepared.close()
        self.prepared = prepared
        try:
            self.server = LocalUpdateServer(self.prepared, request_full=self.full_requested.emit)
            self.native = SparkleSession(
                self.bundle, build_version=self.prepared.feed.build_version,
                feed_url=self.server.feed_url, archive_url=self.server.archive_url,
                delta_url=self.server.delta_url,
                callbacks=SparkleCallbacks(
                    status=self.progress.set_status,
                    cancellable=self.progress.set_cancellable,
                    can_install=self.can_install,
                    prepare_relaunch=self._prepare_install,
                    schedule=lambda callback: QTimer.singleShot(0, callback),
                    quit_application=QApplication.instance().quit,
                    completed=self._native_finished,
                    verify_recovery=self.verify_recovery,
                ),
            )
            self.native.start()
        except Exception as exc:
            self.finish(str(exc))

    def _request_full(self) -> None:
        if self.completed or self.cancelled or self.full_attempted:
            return
        self.full_attempted = True
        self.progress.set_cancellable(True)
        self.progress.set_status("增量更新不可用，正在下载完整包…")
        self.worker = _PrepareWorker(self.result, self.settings, self, feed=self.prepared.feed)
        self.worker.progress.connect(self._update_status, Qt.ConnectionType.QueuedConnection)
        self.worker.finished.connect(self.download_finished)
        self.worker.start()

    def _native_finished(self, error: str = "") -> None:
        # Sparkle may end the cycle instead of requesting its full enclosure
        # (e.g. signature rejection). Retry once without advertising any delta.
        if (error and not self.cancelled and not self.installing and not self.full_attempted
                and self.native.retry_full
                and self.prepared is not None and self.prepared.delta is not None):
            self._request_full()
            self.native.close()
            self.native = None
            self.server.close()
            self.server = None
            return
        self.finish(error)

    def _prepare_install(self) -> None:
        self.prepare_relaunch()
        self.installing = True

    def on_quit(self) -> None:
        if self.installing:
            # Sparkle 2's agent watches process exit via kqueue. Reentering
            # NSApplication.terminate from Qt.aboutToQuit crashes Qt 6 teardown.
            return
        self.cancel()
        if self.worker:
            self.worker.wait()  # Supervisor kills the download process on cancel.
            if self.worker.prepared:
                self.worker.prepared.close()
        if not self.native:
            self.finish()

    def finish(self, error: str = "") -> None:
        if self.completed:
            return
        self.completed = True
        if self.worker:
            worker, self.worker = self.worker, None
            worker.cancel_event.set()
            worker.wait()
            if worker.prepared:
                worker.prepared.close()
            worker.deleteLater()
        if self.native:
            self.native.close()
            self.native = None
        if self.server:
            self.server.close()
            self.server = None
        if self.prepared:
            self.prepared.close()
            self.prepared = None
        if self.fallback_prepared:
            self.fallback_prepared.close()
            self.fallback_prepared = None
        QApplication.instance().aboutToQuit.disconnect(self.on_quit)
        self.progress.finish()
        self.finished.emit("" if self.cancelled else error)
