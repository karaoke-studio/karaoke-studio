from __future__ import annotations

import logging
import sys
import time
from dataclasses import dataclass, field
from typing import Any

from PyQt6.QtCore import QObject, QThread, pyqtSignal

from krok_helper.config import APP_VERSION
from krok_helper.network import requests_session_for_proxy
from krok_helper.updater import http_client
from krok_helper.updater.settings import UpdaterSettings
from krok_helper.updater.sources import (
    REPO_NAME,
    REPO_OWNER,
    SourceId,
    build_api_list_urls,
    build_api_urls,
    build_release_urls,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReleaseAsset:
    name: str
    size: int
    download_url: str


@dataclass(frozen=True)
class LatestRelease:
    tag: str
    version: str
    name: str
    body: str
    html_url: str
    prerelease: bool
    published_at: str
    assets: list[ReleaseAsset] = field(default_factory=list)

    def pick_primary_asset(self, preferred_name: str) -> ReleaseAsset | None:
        for asset in self.assets:
            if asset.name == preferred_name:
                return asset
        platform_key = "windows" if sys.platform == "win32" else "macos" if sys.platform == "darwin" else ""
        if platform_key:
            for asset in self.assets:
                if platform_key in asset.name.lower() and asset.name.lower().endswith(".zip"):
                    return asset
        for asset in self.assets:
            if asset.name.lower().endswith(".zip"):
                return asset
        return None


@dataclass
class CheckResult:
    ok: bool
    has_update: bool = False
    release: LatestRelease | None = None
    primary_url: str = ""
    primary_source: str = ""
    primary_asset_name: str = ""
    download_candidates: list[tuple[SourceId, str]] = field(default_factory=list)
    attempts: list[tuple[SourceId, str, str]] = field(default_factory=list)
    error: str = ""
    skipped_due_to_cooldown: bool = False
    # 从当前版本到最新版之间所有中间版本的 release（含最新版，从新到旧），
    # 用于跨版本更新时在弹窗中聚合展示全部版本的更新日志。
    all_releases: list[LatestRelease] = field(default_factory=list)


def _strip_tag_prefix(tag: str) -> str:
    value = tag.strip()
    if value.lower().startswith("v"):
        return value[1:]
    return value


def _version_key(value: str) -> tuple[int, ...]:
    parts: list[int] = []
    for chunk in _strip_tag_prefix(value).replace("-", ".").split("."):
        digits = ""
        for char in chunk:
            if char.isdigit():
                digits += char
            else:
                break
        parts.append(int(digits or 0))
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


def is_newer_version(remote: str, local: str = APP_VERSION) -> bool:
    return _version_key(remote) > _version_key(local)


def current_asset_name() -> str:
    if sys.platform == "darwin":
        return "KaraokeStudio-macos.zip"
    return "KaraokeStudio-windows.zip"


def _parse_release(payload: dict[str, Any]) -> LatestRelease:
    assets: list[ReleaseAsset] = []
    for raw_asset in payload.get("assets") or []:
        if not isinstance(raw_asset, dict):
            continue
        name = str(raw_asset.get("name") or "")
        if not name:
            continue
        assets.append(
            ReleaseAsset(
                name=name,
                size=int(raw_asset.get("size") or 0),
                download_url=str(raw_asset.get("browser_download_url") or ""),
            )
        )
    tag = str(payload.get("tag_name") or "")
    return LatestRelease(
        tag=tag,
        version=_strip_tag_prefix(tag),
        name=str(payload.get("name") or "") or tag,
        body=str(payload.get("body") or ""),
        html_url=str(payload.get("html_url") or ""),
        prerelease=bool(payload.get("prerelease") or False),
        published_at=str(payload.get("published_at") or ""),
        assets=assets,
    )


def _proxies_for(settings: UpdaterSettings) -> dict[str, str] | None:
    _session, proxies = requests_session_for_proxy(settings.proxy_mode, settings.proxy_manual_url)
    return proxies


def _build_check_error(attempts: list[tuple[SourceId, str, str]]) -> str:
    """根据各源失败原因给出更有指导性的错误文案。

    最常见的失败是 ``api.github.com`` 的 403 限流（代理出口 IP 共享，未认证
    额度 60 次/小时被打满），这与"网络不通"完全是两回事，需要单独提示。
    """
    joined = " ".join(err for _sid, _url, err in attempts if err)
    if "403" in joined:
        return (
            "GitHub 访问频率超限：代理出口 IP 的未认证 API 配额（60 次/小时）已用尽。"
            "这通常是机场共享节点导致，请稍后重试或更换代理节点后再试。"
        )
    return "无法访问任何更新源（请检查网络/代理）"


def fetch_latest_release(
    settings: UpdaterSettings,
) -> tuple[LatestRelease | None, list[tuple[SourceId, str, str]]]:
    attempts: list[tuple[SourceId, str, str]] = []
    session, proxies = requests_session_for_proxy(settings.proxy_mode, settings.proxy_manual_url)
    for source, url in build_api_urls(settings.source_order):
        result = http_client.get_json(url, session=session, proxies=proxies, timeout=(5, 20))
        if not result.ok:
            attempts.append((source, url, result.error or "未知错误"))
            continue
        payload = result.body
        if not isinstance(payload, dict):
            attempts.append((source, url, "响应不是 JSON 对象"))
            continue
        release = _parse_release(payload)
        if not release.tag:
            attempts.append((source, url, "缺少 tag_name"))
            continue
        if release.prerelease:
            attempts.append((source, url, "命中预发布版本，已跳过"))
            continue
        attempts.append((source, url, ""))
        return release, attempts
    return None, attempts


def fetch_latest_release_via_redirect(
    settings: UpdaterSettings,
) -> tuple[LatestRelease | None, list[tuple[SourceId, str, str]]]:
    """轻量兜底：用 github.com 网页端 ``releases/latest`` 的 302 跳转探测最新 tag。

    当 :func:`fetch_latest_release` 的所有 API 源都失败时调用——最典型的场景是
    代理出口 IP（机场共享节点）触发 ``api.github.com`` 的"未认证 60 次/小时"
    限流而返回 403。该网页端点不计入该限流，只要代理能访问 github.com 即可拿到
    版本号。

    代价：只能拿到 ``tag`` / ``version``，拿不到 changelog 正文与资产清单。调用方
    需按发布命名约定自行合成下载链接，并据 ``version`` 判断是否有更新。
    """
    url = f"https://github.com/{REPO_OWNER}/{REPO_NAME}/releases/latest"
    attempts: list[tuple[SourceId, str, str]] = []
    session, proxies = requests_session_for_proxy(settings.proxy_mode, settings.proxy_manual_url)
    result = http_client.get_redirect_location(url, session=session, proxies=proxies)
    if not result.ok or not isinstance(result.body, str):
        attempts.append(("github", url, result.error or "未知错误"))
        return None, attempts

    location = result.body
    # Location 形如 ``https://github.com/<owner>/<repo>/releases/tag/v3.1.7.4``
    tag = location.rstrip("/").rsplit("/tag/", 1)[-1] if "/tag/" in location else ""
    if not tag:
        attempts.append(("github", url, f"无法从跳转地址解析 tag: {location}"))
        return None, attempts

    attempts.append(("github", url, ""))
    release = LatestRelease(
        tag=tag,
        version=_strip_tag_prefix(tag),
        name=tag,
        body="",
        html_url=location,
        prerelease=False,
        published_at="",
        assets=[],
    )
    return release, attempts


def fetch_releases_since(
    current_version: str,
    settings: UpdaterSettings,
) -> tuple[list[LatestRelease], list[tuple[SourceId, str, str]]]:
    """获取所有比 ``current_version`` 更新的 release（不含当前版本本身）。

    按源顺序尝试，第一个成功的源返回结果，按版本从新到旧排列；全部失败返回空。
    用途：跨版本更新（如 3.1.5 → 3.1.7.4）时聚合中间所有版本的更新日志。
    """
    attempts: list[tuple[SourceId, str, str]] = []
    session, proxies = requests_session_for_proxy(settings.proxy_mode, settings.proxy_manual_url)
    for source, url in build_api_list_urls(settings.source_order):
        result = http_client.get_json(url, session=session, proxies=proxies, timeout=(5, 20))
        if not result.ok or not isinstance(result.body, list):
            attempts.append((source, url, result.error or "响应不是 JSON 数组"))
            continue
        releases: list[LatestRelease] = []
        for item in result.body:
            if not isinstance(item, dict):
                continue
            try:
                release = _parse_release(item)
            except Exception:  # noqa: BLE001
                continue
            if not release.tag or release.prerelease:
                continue
            if is_newer_version(release.version, current_version):
                releases.append(release)
        attempts.append((source, url, ""))
        releases.sort(key=lambda r: _version_key(r.version), reverse=True)
        return releases, attempts
    return [], attempts


class _CheckRunnable(QObject):
    finished = pyqtSignal(object)

    def __init__(self, settings: UpdaterSettings, manual: bool = False):
        super().__init__()
        self._settings = settings
        self._manual = manual

    def run(self) -> None:
        try:
            result = self._do_check()
        except Exception as exc:  # noqa: BLE001
            log.exception("更新检查异常")
            result = CheckResult(ok=False, error=f"检查异常: {exc}")
        self.finished.emit(result)

    def _do_check(self) -> CheckResult:
        if not self._settings.enabled:
            return CheckResult(ok=False, error="更新功能已禁用")
        if not self._manual and not self._settings.check_on_startup:
            return CheckResult(ok=False, skipped_due_to_cooldown=True)
        if not self._manual and self._settings.is_within_check_cooldown():
            return CheckResult(ok=True, skipped_due_to_cooldown=True)

        release, attempts = fetch_latest_release(self._settings)
        # API 全部失败时的兜底：用 github.com 网页端 302 跳转探测版本。
        # 典型触发场景是代理出口 IP 触发 api.github.com 的 403 限流——此时代理
        # "联通"但官方与各镜像 API 都拿不到数据。网页端跳转不计入该限流。
        redirect_fallback = False
        if release is None:
            fb_release, fb_attempts = fetch_latest_release_via_redirect(self._settings)
            attempts = attempts + fb_attempts
            if fb_release is None:
                return CheckResult(ok=False, error=_build_check_error(attempts), attempts=attempts)
            release = fb_release
            redirect_fallback = True
            log.info("API 源全部失败，已用 github.com 跳转兜底探测到版本: %s", release.tag)

        self._settings.last_check_at = int(time.time())
        self._settings.last_seen_version = release.version
        preferred_asset = current_asset_name()
        if redirect_fallback:
            # 跳转兜底拿不到资产清单：按发布命名约定信任资产存在，仅以版本号判断。
            asset = None
            asset_name = preferred_asset
            has_update = is_newer_version(release.version, APP_VERSION)
        else:
            asset = release.pick_primary_asset(preferred_asset)
            asset_name = asset.name if asset is not None else preferred_asset
            has_update = is_newer_version(release.version, APP_VERSION) and asset is not None

        candidates: list[tuple[SourceId, str]] = []
        primary_source = ""
        primary_url = ""
        if asset is not None or redirect_fallback:
            candidates = build_release_urls(self._settings.source_order, release.tag, asset_name)
            if candidates:
                primary_source, primary_url = candidates[0]

        if has_update and not self._manual and self._settings.skipped_version == release.version:
            has_update = False

        # 有更新时聚合中间版本的 changelog（跨版本更新日志）。
        # 跳转兜底模式下 API 必然仍是失败的，跳过聚合以免徒劳的 403 请求。
        all_releases: list[LatestRelease] = []
        if has_update and not redirect_fallback:
            try:
                all_releases, _list_attempts = fetch_releases_since(APP_VERSION, self._settings)
            except Exception:  # noqa: BLE001
                log.debug("获取全量 releases 列表失败（仅展示最新版 changelog）", exc_info=True)
            if not all_releases:
                all_releases = [release]

        return CheckResult(
            ok=True,
            has_update=has_update,
            release=release,
            primary_url=primary_url,
            primary_source=primary_source,
            primary_asset_name=asset_name,
            download_candidates=candidates,
            attempts=attempts,
            all_releases=all_releases,
        )


class LaunchUpdaterWorker(QThread):
    """后台线程执行 Updater 预热（自更新）+ 启动，避免主线程卡死。

    ``installer.launch_updater`` 内部的自更新会下载 app part zip（数秒到数十秒
    的网络 IO）。``progress`` 信号供进度窗实时刷新；:meth:`request_cancel`
    由主线程调用请求取消。
    """

    done = pyqtSignal(object)  # LaunchResult
    progress = pyqtSignal(str)

    def __init__(self, plan, parent: QObject | None = None):
        super().__init__(parent)
        self._plan = plan
        self._cancelled = False

    def request_cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        from krok_helper.updater import installer

        try:
            result = installer.launch_updater(
                self._plan,
                progress_cb=self.progress.emit,
                cancel_check=lambda: self._cancelled,
            )
        except installer.UpdateCancelledError:
            result = installer.LaunchResult(launched=False, reason="用户取消更新")
        except Exception as exc:  # noqa: BLE001
            log.exception("启动 Updater 异常")
            result = installer.LaunchResult(launched=False, reason=str(exc))
        self.done.emit(result)


class UpdateChecker(QObject):
    finished = pyqtSignal(object)

    def __init__(self, settings: UpdaterSettings, manual: bool = False, parent: QObject | None = None):
        super().__init__(parent)
        self._settings = settings
        self._manual = manual
        self._thread: QThread | None = None
        self._worker: _CheckRunnable | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.isRunning():
            return
        self._thread = QThread()
        self._worker = _CheckRunnable(self._settings, self._manual)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.finished.connect(self._on_finished)
        self._worker.finished.connect(self._thread.quit)
        self._thread.finished.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._cleanup)
        self._thread.start()

    def _on_finished(self, result: object) -> None:
        self.finished.emit(result)

    def _cleanup(self) -> None:
        if self._thread is not None:
            self._thread.deleteLater()
        self._thread = None
        self._worker = None
