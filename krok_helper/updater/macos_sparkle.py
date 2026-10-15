"""Native Sparkle 2 adapter; UI and application lifecycle are supplied as callbacks."""

from __future__ import annotations

import plistlib
import sys
from pathlib import Path
from dataclasses import dataclass
from typing import Callable

from krok_helper.updater.macos_metadata import validate_ed_key

_native_classes = None


@dataclass(frozen=True)
class SparkleCallbacks:
    status: Callable[[str], None]
    cancellable: Callable[[bool], None]
    can_install: Callable[[], bool]
    prepare_relaunch: Callable[[], None]
    schedule: Callable[[Callable[[], None]], None]
    quit_application: Callable[[], None]
    completed: Callable[[str], None]
    verify_recovery: Callable[[], None] = lambda: None


def find_host_bundle() -> Path:
    executable = Path(sys.executable).resolve()
    bundle = executable.parent.parent.parent
    if not getattr(sys, "frozen", False) or bundle.suffix != ".app" or executable.parent.name != "MacOS":
        raise ValueError("macOS 自动更新需要从已安装的 .app 中运行。")
    with (bundle / "Contents/Info.plist").open("rb") as stream:
        metadata = plistlib.load(stream)
    validate_ed_key(metadata.get("SUPublicEDKey", ""))
    if not (bundle / "Contents/Frameworks/Sparkle.framework").is_dir():
        raise ValueError("应用包缺少 Sparkle 更新框架。")
    return bundle


def load_native_classes(bundle: Path):
    global _native_classes
    if _native_classes is not None:
        return _native_classes
    import objc
    from Foundation import NSObject, NSURL

    objc.loadBundle("Sparkle", {}, bundle_path=str(bundle / "Contents/Frameworks/Sparkle.framework"))

    def block_metadata(cls, selector, argument, enum=False, object_reply=False):
        args = {0: {"type": b"^v"}}
        if enum:
            args[1] = {"type": b"q"}
        elif object_reply:
            args[1] = {"type": b"@"}
        objc.registerMetaDataForSelector(cls.encode(), selector.encode(), {
            "arguments": {argument: {"callable": {"retval": {"type": b"v"}, "arguments": args}}},
        })

    for selector, argument, enum in [
        ("showUserInitiatedUpdateCheckWithCancellation:", 2, False),
        ("showUpdateFoundWithAppcastItem:state:reply:", 4, True),
        ("showUpdateNotFoundWithError:acknowledgement:", 3, False),
        ("showUpdaterError:acknowledgement:", 3, False),
        ("showDownloadInitiatedWithCancellation:", 2, False),
        ("showReadyToInstallAndRelaunch:", 2, True),
        ("showInstallingUpdateWithApplicationTerminated:retryTerminatingApplication:", 3, False),
        ("showUpdateInstalledAndRelaunched:acknowledgement:", 3, False),
    ]:
        block_metadata("LKMacUserDriver", selector, argument, enum)
    block_metadata("LKMacUserDriver", "showUpdatePermissionRequest:reply:", 3, object_reply=True)
    block_metadata("LKMacUpdaterDelegate", "updater:shouldPostponeRelaunchForUpdate:untilInvokingBlock:", 4)
    objc.registerMetaDataForSelector(b"SPUUpdater", b"startUpdater:", {
        "arguments": {2: {"type_modifier": b"o"}},
    })

    class LKMacUserDriver(NSObject, protocols=[objc.protocolNamed("SPUUserDriver")]):
        def showUpdatePermissionRequest_reply_(self, request, reply):
            # Scheduling and permission remain owned by the existing Qt checker.
            response = objc.lookUpClass("SUUpdatePermissionResponse").alloc().initWithAutomaticUpdateChecks_automaticUpdateDownloading_sendSystemProfile_(False, False, False)
            reply(response)

        def showUserInitiatedUpdateCheckWithCancellation_(self, cancellation):
            self.owner.cancel_callback = cancellation

        def showUpdateFoundWithAppcastItem_state_reply_(self, item, state, reply):
            valid = str(item.versionString()) == self.owner.build_version
            reply(1 if valid and not self.owner.cancelled else 0)

        def showUpdateReleaseNotesWithDownloadData_(self, data):
            pass  # Chinese release notes have already been shown in Qt.

        def showUpdateReleaseNotesFailedToDownloadWithError_(self, error):
            pass

        def showUpdateNotFoundWithError_acknowledgement_(self, error, acknowledgement):
            self.owner.error_text = "本次更新不可用：\n" + str(error.localizedDescription())
            acknowledgement()

        def showUpdaterError_acknowledgement_(self, error, acknowledgement):
            self.owner.error_text = "macOS 更新失败：\n" + str(error.localizedDescription())
            acknowledgement()

        def showDownloadInitiatedWithCancellation_(self, cancellation):
            self.owner.cancel_callback = cancellation
            self.owner.callbacks.cancellable(True)
            self.owner.callbacks.status("正在将更新包交给 macOS 更新器…")

        def showDownloadDidReceiveExpectedContentLength_(self, length):
            pass

        def showDownloadDidReceiveDataOfLength_(self, length):
            pass

        def showDownloadDidStartExtractingUpdate(self):
            self.owner.cancel_callback = None
            self.owner.callbacks.cancellable(False)
            self.owner.callbacks.status("签名校验通过，正在应用增量更新…" if self.owner.downloading_delta else "签名校验通过，正在解压更新包…")

        def showExtractionReceivedProgress_(self, progress):
            action = "应用增量更新" if self.owner.downloading_delta else "解压更新包"
            self.owner.callbacks.status(f"正在{action}：{int(progress * 100)}%")

        def showReadyToInstallAndRelaunch_(self, reply):
            if self.owner.cancelled or not self.owner.callbacks.can_install():
                self.owner.retry_full = False
                self.owner.error_text = "当前有任务正在运行，本次更新已取消。"
                reply(0)
                return
            if not self.owner.verify_recovery():
                self.owner.retry_full = False
                reply(0)
                return
            self.owner.callbacks.status("正在准备安装并重新启动…")
            reply(1)

        def showInstallingUpdateWithApplicationTerminated_retryTerminatingApplication_(self, terminated, retry):
            self.owner.callbacks.status("正在安装更新，完成后会自动重新启动…")

        def showUpdateInstalledAndRelaunched_acknowledgement_(self, relaunched, acknowledgement):
            acknowledgement()

        def dismissUpdateInstallation(self):
            pass  # The cycle completion delegate owns cache/server cleanup.

    class LKMacUpdaterDelegate(NSObject, protocols=[objc.protocolNamed("SPUUpdaterDelegate")]):
        def feedURLStringForUpdater_(self, updater):
            return self.owner.feed_url

        def updater_willDownloadUpdate_withRequest_(self, updater, item, request):
            self.owner.downloading_delta = bool(item.isDeltaUpdate())
            request.setURL_(NSURL.URLWithString_(self.owner.resource_url(self.owner.downloading_delta)))

        def updater_shouldPostponeRelaunchForUpdate_untilInvokingBlock_(self, updater, item, install_handler):
            # Called after Sparkle's installer has staged and validated the app.
            # Do not call Qt.quit() or the Windows two-second hard-exit timer.
            self.owner.callbacks.prepare_relaunch()
            self.owner.installing = True
            self.owner.install_handler = install_handler
            self.owner.callbacks.schedule(self.owner.continue_install)
            return True

        def updaterWillRelaunchApplication_(self, updater):
            self.owner.installing = True
            # Qt's Cocoa delegate does not exit its event loop in response to
            # Sparkle's native termination request. Quit Qt once Sparkle has
            # committed the handoff. Sparkle 2's agent watches process exit.
            self.owner.callbacks.schedule(self.owner.callbacks.quit_application)

        def updater_didFinishUpdateCycleForUpdateCheck_error_(self, updater, check, error):
            if error and not self.owner.error_text and not self.owner.cancelled:
                self.owner.error_text = "macOS 更新失败：\n" + str(error.localizedDescription())
            self.owner.callbacks.schedule(self.owner.finish)

    _native_classes = (objc.lookUpClass("SPUUpdater"), LKMacUserDriver, LKMacUpdaterDelegate)
    return _native_classes


class SparkleSession:
    """One confirmed update cycle; retain native drivers and reply blocks until completion."""

    def __init__(
        self, bundle: Path, *, build_version: str, feed_url: str,
        archive_url: str, callbacks: SparkleCallbacks,
        delta_url: str | None = None,
    ):
        self.bundle = bundle
        self.build_version = build_version
        self.feed_url, self.archive_url = feed_url, archive_url
        self.delta_url = delta_url
        self.downloading_delta = False
        self.retry_full = True
        self.callbacks = callbacks
        self.updater = self.driver = self.delegate = None
        self.cancel_callback = self.install_handler = None
        self.cancelled = self.installing = self.completed = False
        self.error_text = ""

    def resource_url(self, delta: bool) -> str:
        if delta and not self.delta_url:
            raise ValueError("本次更新没有已准备的增量包。")
        return self.delta_url if delta else self.archive_url

    def start(self) -> None:
        from Foundation import NSBundle

        core, driver_class, delegate_class = load_native_classes(self.bundle)
        self.driver = driver_class.alloc().init()
        self.delegate = delegate_class.alloc().init()
        self.driver.owner = self.delegate.owner = self
        host = NSBundle.bundleWithPath_(str(self.bundle))
        self.updater = core.alloc().initWithHostBundle_applicationBundle_userDriver_delegate_(
            host, host, self.driver, self.delegate,
        )
        # The existing checker owns scheduling and skip-version settings.
        self.updater.setAutomaticallyChecksForUpdates_(False)
        self.updater.setAutomaticallyDownloadsUpdates_(False)
        started, error = self.updater.startUpdater_(None)
        if not started:
            raise RuntimeError(str(error.localizedDescription()) if error else "Sparkle 无法启动")
        self.updater.checkForUpdates()

    def cancel(self) -> None:
        self.cancelled = True
        if self.cancel_callback:
            callback, self.cancel_callback = self.cancel_callback, None
            callback()

    def verify_recovery(self) -> bool:
        """Reject installation before handoff if the host cannot save current edits."""
        try:
            self.callbacks.verify_recovery()
        except Exception as exc:
            self.error_text = f"更新已取消，工作台保持打开：\n{exc}"
            return False
        return True

    def continue_install(self) -> None:
        handler, self.install_handler = self.install_handler, None
        if handler:
            handler()

    def finish(self) -> None:
        if self.completed:
            return
        self.completed = True
        self.callbacks.completed("" if self.cancelled else self.error_text)

    def close(self) -> None:
        """Release only after the native cycle has completed or startup failed."""
        self.cancel_callback = self.install_handler = None
        if self.driver:
            self.driver.owner = None
        if self.delegate:
            self.delegate.owner = None
        self.updater = self.driver = self.delegate = None
