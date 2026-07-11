from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger(__name__)

UPDATER_EXE_NAME = "Updater.exe"
TMP_DIR_NAME = "KaraokeStudioUpdater"
DEFAULT_APP_EXE_NAME = "Karaoke Studio.exe"
LOCAL_MANIFEST_FILENAME = ".installed_manifest.json"


class UpdateCancelledError(Exception):
    """用户在更新准备阶段主动取消时抛出。"""


@dataclass
class LaunchPlan:
    app_dir: Path
    app_exe_name: str
    target_version: str
    target_tag: str
    asset_name: str
    download_urls: list[tuple[str, str]]
    proxy_url: str = ""
    internal_dir_name: str = "_internal"
    expected_sha256: str = ""
    launch_after_update: bool = True
    extras: list[str] = field(default_factory=list)

    def command_args(self, updater_exe: Path, current_pid: int) -> list[str]:
        args = [str(updater_exe)]
        args += ["--app-dir", str(self.app_dir)]
        args += ["--app-exe", self.app_exe_name]
        args += ["--target-version", self.target_version]
        args += ["--target-tag", self.target_tag]
        args += ["--asset-name", self.asset_name]
        args += ["--internal-name", self.internal_dir_name]
        args += ["--pid", str(current_pid)]
        if self.proxy_url:
            args += ["--proxy", self.proxy_url]
        if self.expected_sha256:
            args += ["--sha256", self.expected_sha256]
        if not self.launch_after_update:
            args += ["--no-launch"]
        for source_id, url in self.download_urls:
            args += ["--url", f"{source_id}|{url}"]
        args += self.extras
        return args


@dataclass
class LaunchResult:
    launched: bool
    updater_path: str = ""
    temp_copy_path: str = ""
    pid: int = 0
    reason: str = ""


def find_app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(sys.argv[0]).resolve().parent


def find_app_exe_name() -> str:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).name
    return DEFAULT_APP_EXE_NAME


def find_updater_exe(app_dir: Optional[Path] = None) -> Optional[Path]:
    root = app_dir or find_app_dir()
    for path in (
        root / UPDATER_EXE_NAME,
        root / "_internal" / "updater" / UPDATER_EXE_NAME,
    ):
        if path.exists():
            return path
    return None


def is_updater_available(app_dir: Optional[Path] = None) -> bool:
    return find_updater_exe(app_dir) is not None


def _copy_updater_to_temp(updater_exe: Path) -> Path:
    tmp_dir = Path(tempfile.gettempdir()) / TMP_DIR_NAME
    tmp_dir.mkdir(parents=True, exist_ok=True)
    dest = tmp_dir / UPDATER_EXE_NAME
    try:
        shutil.copy2(str(updater_exe), str(dest))
    except PermissionError:
        dest = tmp_dir / f"Updater-{int(time.time())}.exe"
        shutil.copy2(str(updater_exe), str(dest))
    return dest


def _read_pe_subsystem(exe_path: Path) -> int:
    """读取 PE 头的 Subsystem 字段：2=GUI, 3=Console。失败返回 0。"""
    try:
        with open(exe_path, "rb") as f:
            f.seek(0x3C)
            pe_offset = int.from_bytes(f.read(4), "little")
            f.seek(pe_offset + 0x5C)
            return int.from_bytes(f.read(2), "little")
    except Exception:  # noqa: BLE001
        return 0


# ───────────────────────── Updater.exe 自更新 ─────────────────────────
#
# 解决鸡生蛋：执行本次更新的是随旧版分发的 Updater.exe（滞后一代），若它有
# bug 恰好令更新失败，用户会死锁在旧版。主程序在启动 Updater 之前先从远端
# app part 提取新 Updater.exe 替换本地，把依赖顺序反过来。
#
# 与 SUG 的偏差（见 docs/工作台更新器完善计划.md 偏差 #2）：part 资产名不做
# 字符串派生，一律读 manifest ``parts.app.asset``；manifest 拉不到就直接降级
# 用旧 Updater（走它自己的全量兜底），不做派生名的 bootstrap 下载。


def _manifest_asset_name(asset_name: str) -> str:
    """与随包 Updater 的 try_fetch_manifest 派生规则保持一致。"""
    return asset_name.replace("StrangeUtaGame", "manifest", 1).replace(".zip", ".json")


def _content_hash_of_zip(zip_path: Path) -> str:
    """zip 内容哈希，与 updater_app / build_parts 同一算法。"""
    entries: list[tuple[str, str]] = []
    with zipfile.ZipFile(str(zip_path), "r") as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            content = zf.read(info.filename)
            entries.append((info.filename, hashlib.sha256(content).hexdigest()))
    entries.sort(key=lambda e: e[0])
    combined = "\n".join(f"{name}:{h}" for name, h in entries)
    return hashlib.sha256(combined.encode("ascii")).hexdigest().lower()


def _fetch_remote_manifest(plan: LaunchPlan, proxies: Optional[dict]) -> Optional[dict]:
    """按下载源顺序拉取增量 manifest JSON，全部失败返回 None。"""
    from krok_helper.updater import http_client

    manifest_name = _manifest_asset_name(plan.asset_name)
    for source_id, url in plan.download_urls:
        prefix = url.rsplit("/", 1)[0]
        result = http_client.get_json(f"{prefix}/{manifest_name}", proxies=proxies, timeout=(5, 15))
        if not result.ok or not isinstance(result.body, dict):
            log.debug("[self-update] manifest 拉取失败（%s）: %s", source_id, result.error)
            continue
        data = result.body
        if isinstance(data.get("parts"), dict) and data["parts"]:
            log.info("[self-update] manifest 获取成功（version=%s）", data.get("version"))
            return data
    return None


def _read_local_manifest(plan: LaunchPlan) -> Optional[dict]:
    p = plan.app_dir / plan.internal_dir_name / LOCAL_MANIFEST_FILENAME
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _update_updater_from_remote(
    plan: LaunchPlan,
    progress_cb: Optional[Callable[[str], None]] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> bool:
    """尝试从远端 app part zip 提取并替换本地 Updater.exe。

    流程：
    1. 拉远端 manifest（小 JSON），对比 ``parts.app.sha256`` 与本地
       ``.installed_manifest.json``；一致则 Updater 未变，不下载任何 zip。
    2. 不一致（或本地无清单）时下载 app part zip 到 Updater 的 parts 工作目录
       （``%TEMP%/KaraokeStudioUpdater/parts/``），校验内容哈希后提取新
       Updater.exe 替换本地。zip 保留在原地——Updater 随后增量更新时按内容
       哈希直接复用，不会二次下载。

    返回 True 表示 Updater 已是最新或已更新；False 表示失败（降级用旧
    Updater，不影响后续流程）。``cancel_check`` 返回 True 或 ``progress_cb``
    抛 :class:`UpdateCancelledError` 时中止并向上抛。
    """
    from krok_helper.updater import http_client

    def _notify(text: str) -> None:
        if cancel_check is not None and cancel_check():
            raise UpdateCancelledError()
        if progress_cb is not None:
            progress_cb(text)

    _notify("正在检查更新器版本…")
    proxies = {"http": plan.proxy_url, "https": plan.proxy_url} if plan.proxy_url else None
    remote = _fetch_remote_manifest(plan, proxies)
    if remote is None:
        log.info("[self-update] 无法获取远端 manifest，降级使用旧版 Updater")
        return False

    app_part = (remote.get("parts") or {}).get("app") or {}
    remote_sha = str(app_part.get("sha256") or "").lower()
    part_asset = str(app_part.get("asset") or "")
    if not remote_sha or not part_asset:
        log.info("[self-update] manifest 缺少 app part 信息，降级使用旧版 Updater")
        return False

    local = _read_local_manifest(plan)
    local_sha = ""
    if local:
        local_sha = str(((local.get("parts") or {}).get("app") or {}).get("sha256") or "").lower()
    if local_sha and local_sha == remote_sha:
        log.info("[self-update] app part sha256 一致（%s…），Updater 无需更新", remote_sha[:12])
        _notify("更新器已是最新，无需更新")
        return True

    # 下载 app part zip（落到 Updater 的 parts 目录，供其增量复用）
    parts_dir = Path(tempfile.gettempdir()) / TMP_DIR_NAME / "parts"
    parts_dir.mkdir(parents=True, exist_ok=True)
    part_zip = parts_dir / part_asset

    # 本地已有且哈希一致 → 零下载复用
    if part_zip.exists():
        try:
            if _content_hash_of_zip(part_zip) == remote_sha:
                log.info("[self-update] 本地已有 app part zip 且哈希一致，跳过下载")
            else:
                part_zip.unlink()
        except (OSError, zipfile.BadZipFile):
            try:
                part_zip.unlink()
            except OSError:
                pass

    if not part_zip.exists():
        total_mb = int(app_part.get("size") or 0) / 1024 / 1024
        _notify(f"正在下载更新器… (约 {total_mb:.1f} MB)" if total_mb else "正在下载更新器…")

        last_progress = [-1]

        def _dl_progress(done: int, total: int) -> None:
            if progress_cb is None:
                return
            if total > 0:
                pct = int(done * 100 / total)
                if pct != last_progress[0]:
                    last_progress[0] = pct
                    progress_cb(
                        f"正在下载更新器… {pct}%  ({done / 1024 / 1024:.1f} / {total / 1024 / 1024:.1f} MB)"
                    )

        downloaded = False
        last_error = ""
        for source_id, url in plan.download_urls:
            if cancel_check is not None and cancel_check():
                raise UpdateCancelledError()
            prefix = url.rsplit("/", 1)[0]
            part_url = f"{prefix}/{part_asset}"
            # 切换源前清掉残留，防止跨源续传拼接出损坏的 zip
            if part_zip.exists():
                try:
                    part_zip.unlink()
                except OSError:
                    pass
            result = http_client.download(
                part_url,
                str(part_zip),
                proxies=proxies,
                progress_cb=_dl_progress,
                cancel_check=cancel_check,
            )
            if result.ok:
                downloaded = True
                break
            last_error = result.error
            if result.error == "用户取消":
                raise UpdateCancelledError()
            log.warning("[self-update] app part 下载失败（%s）: %s", source_id, result.error)
        if not downloaded:
            log.warning("[self-update] 所有源下载失败（%s），降级使用旧版 Updater", last_error)
            return False

        _notify("正在校验文件完整性…")
        try:
            actual = _content_hash_of_zip(part_zip)
        except (OSError, zipfile.BadZipFile) as exc:
            log.warning("[self-update] app part zip 无法读取: %s", exc)
            try:
                part_zip.unlink()
            except OSError:
                pass
            return False
        if actual != remote_sha:
            log.warning("[self-update] app part 内容哈希不匹配，放弃自更新")
            try:
                part_zip.unlink()
            except OSError:
                pass
            return False

    _notify("正在提取更新器…")
    try:
        with zipfile.ZipFile(str(part_zip)) as zf:
            entry = next(
                (n for n in zf.namelist() if n.endswith(UPDATER_EXE_NAME) and not n.endswith("/")),
                None,
            )
            if entry is None:
                log.warning("[self-update] app part zip 中未找到 %s", UPDATER_EXE_NAME)
                return False
            new_bytes = zf.read(entry)
    except (OSError, zipfile.BadZipFile) as exc:
        log.warning("[self-update] 提取 Updater 失败: %s", exc)
        return False

    local_target = plan.app_dir / UPDATER_EXE_NAME
    if local_target.exists():
        try:
            if local_target.read_bytes() == new_bytes:
                log.info("[self-update] Updater.exe 字节一致，无需替换")
                _notify("更新器已是最新，无需更新")
                return True
        except OSError:
            pass

    for attempt in range(3):
        try:
            local_target.write_bytes(new_bytes)
            break
        except PermissionError:
            if attempt < 2:
                time.sleep(1.0)
            else:
                log.warning("[self-update] 写入新 Updater.exe 失败（句柄未释放），降级使用旧版")
                return False
    log.info("[self-update] 已更新 Updater.exe（%d bytes）；app part zip 保留供增量复用", len(new_bytes))
    _notify("更新器更新完毕")
    return True


def launch_updater(
    plan: LaunchPlan,
    progress_cb: Optional[Callable[[str], None]] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> LaunchResult:
    updater = find_updater_exe(plan.app_dir)
    if updater is None:
        return LaunchResult(
            launched=False,
            reason="未找到 Updater.exe。请重新下载完整安装包，或确保 Updater.exe 与主程序位于同一目录。",
        )

    # 先尝试自更新 Updater.exe（失败仅降级，不阻断更新流程）
    try:
        _update_updater_from_remote(plan, progress_cb=progress_cb, cancel_check=cancel_check)
        updater = find_updater_exe(plan.app_dir) or updater
    except UpdateCancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        log.warning("自更新 Updater.exe 失败（忽略，继续使用旧版）: %s", exc)

    if progress_cb is not None:
        progress_cb("正在启动更新器…")

    try:
        temp_copy = _copy_updater_to_temp(updater)
    except Exception as exc:  # noqa: BLE001
        return LaunchResult(launched=False, reason=f"Failed to copy Updater.exe: {exc}")

    args = plan.command_args(temp_copy, os.getpid())

    # 按 PE Subsystem 选择启动方式：GUI 不弹控制台，console 弹新控制台让用户看到输出
    flags = 0
    if sys.platform == "win32":
        subsystem = _read_pe_subsystem(temp_copy)
        if subsystem == 2:  # IMAGE_SUBSYSTEM_WINDOWS_GUI
            flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        else:
            flags = 0x00000010 | 0x00000200  # CREATE_NEW_CONSOLE | CREATE_NEW_PROCESS_GROUP

    try:
        proc = subprocess.Popen(
            args,
            cwd=str(plan.app_dir),
            close_fds=True,
            creationflags=flags,
            stdin=None,
            stdout=None,
            stderr=None,
        )
    except Exception as exc:  # noqa: BLE001
        return LaunchResult(launched=False, updater_path=str(updater), temp_copy_path=str(temp_copy), reason=str(exc))

    return LaunchResult(
        launched=True,
        updater_path=str(updater),
        temp_copy_path=str(temp_copy),
        pid=int(proc.pid),
    )

