"""Managed PyMSS runtime package, validation, download, and repair primitives."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Callable

import requests

from krok_helper.network import github_url_attempts
from krok_helper.windows import hidden_subprocess_kwargs

from .integration import (
    PYMSS_PYTHON_VERSION,
    PYMSS_RUNTIME_VERSION,
    PYMSS_TORCH_VERSION,
    PYMSS_VERSION,
    TORCH_WHEELS,
)

_MANIFEST_SCHEMA = 1
_CHUNK_SIZE = 1024 * 1024


def _default_download_session() -> requests.Session:
    """未显式注入 session 时的默认会话：PyMSS 底座与 torch wheel 属于下载步骤，
    必须遵循工作台的代理设置（system / auto / manual / off），不能吃进程环境里
    恰好残留的代理变量。设置读取失败时退回普通会话，不阻断安装流程。"""
    try:
        from krok_helper.network import requests_session_for_current_settings

        session, proxies = requests_session_for_current_settings()
        if proxies:
            session.proxies.update(proxies)
        return session
    except Exception:
        return requests.Session()


class RuntimeStatus(str, Enum):
    READY = "ready"
    MISSING = "missing"
    DAMAGED = "damaged"
    INCOMPATIBLE = "incompatible"


@dataclass(frozen=True)
class RuntimeFile:
    path: str
    size: int
    sha256: str


@dataclass(frozen=True)
class RuntimeArchivePart:
    url: str
    size: int
    sha256: str


@dataclass(frozen=True)
class RuntimeDependencyWheel:
    name: str
    version: str
    url: str
    filename: str
    size: int
    sha256: str


@dataclass(frozen=True)
class RuntimePackage:
    runtime_version: str
    pymss_version: str
    python_version: str
    variant: str
    archive_size: int
    archive_sha256: str
    archive_parts: tuple[RuntimeArchivePart, ...]
    files: tuple[RuntimeFile, ...]
    torch_wheel: RuntimeDependencyWheel | None = None

    @property
    def download_size(self) -> int:
        return self.archive_size + (self.torch_wheel.size if self.torch_wheel else 0)

    @property
    def archive_url(self) -> str:
        """Compatibility accessor for a package containing one archive."""
        return self.archive_parts[0].url if len(self.archive_parts) == 1 else ""

    @classmethod
    def from_payload(cls, payload: dict) -> "RuntimePackage":
        if not isinstance(payload, dict) or payload.get("schema") != _MANIFEST_SCHEMA:
            raise ValueError("PyMSS Runtime 清单格式不受支持。")
        archive = payload.get("archive")
        raw_files = payload.get("files")
        if not isinstance(archive, dict) or not isinstance(raw_files, list):
            raise ValueError("PyMSS Runtime 清单缺少 archive 或 files。")
        files = tuple(
            RuntimeFile(
                path=_safe_relative_path(item.get("path", "")),
                size=_nonnegative_int(item.get("size"), "files[].size"),
                sha256=_sha256_text(item.get("sha256"), "files[].sha256"),
            )
            for item in raw_files
            if isinstance(item, dict)
        )
        if not files:
            raise ValueError("PyMSS Runtime 清单没有文件记录。")
        raw_parts = archive.get("parts")
        if raw_parts is None:
            raw_parts = [archive]
        if not isinstance(raw_parts, list) or not raw_parts:
            raise ValueError("PyMSS Runtime 清单没有下载分片。")
        parts = tuple(
            RuntimeArchivePart(
                url=str(item.get("url", "")).strip(),
                size=_positive_int(item.get("size"), "archive.parts[].size"),
                sha256=_sha256_text(item.get("sha256"), "archive.parts[].sha256"),
            )
            for item in raw_parts
            if isinstance(item, dict)
        )
        if not parts or any(not item.url for item in parts):
            raise ValueError("PyMSS Runtime 清单的下载分片地址无效。")
        archive_size = _positive_int(archive.get("size"), "archive.size")
        if sum(item.size for item in parts) != archive_size:
            raise ValueError("PyMSS Runtime 清单的分片总大小与压缩包大小不一致。")
        torch_payload = payload.get("torch")
        torch_wheel = None
        if torch_payload is not None:
            if not isinstance(torch_payload, dict) or not isinstance(
                torch_payload.get("wheel"), dict
            ):
                raise ValueError("PyMSS Runtime 清单的 torch 依赖格式无效。")
            wheel = torch_payload["wheel"]
            filename = str(wheel.get("filename", "")).strip()
            if not filename.lower().endswith(".whl") or Path(filename).name != filename:
                raise ValueError("PyMSS Runtime 清单的 torch wheel 文件名无效。")
            torch_wheel = RuntimeDependencyWheel(
                name="torch",
                version=str(torch_payload.get("version", "")).strip(),
                url=str(wheel.get("url", "")).strip(),
                filename=filename,
                size=_positive_int(wheel.get("size"), "torch.wheel.size"),
                sha256=_sha256_text(wheel.get("sha256"), "torch.wheel.sha256"),
            )
            if not torch_wheel.version or not torch_wheel.url:
                raise ValueError("PyMSS Runtime 清单的 torch wheel 信息不完整。")
        return cls(
            runtime_version=str(payload.get("runtime_version", "")).strip(),
            pymss_version=str(payload.get("pymss_version", "")).strip(),
            python_version=str(payload.get("python_version", "")).strip(),
            variant=str(payload.get("variant", "")).strip(),
            archive_size=archive_size,
            archive_sha256=_sha256_text(archive.get("sha256"), "archive.sha256"),
            archive_parts=parts,
            files=files,
            torch_wheel=torch_wheel,
        )

    def installed_payload(self, files: tuple[RuntimeFile, ...] | None = None) -> dict:
        payload = {
            "schema": _MANIFEST_SCHEMA,
            "complete": True,
            "runtime_version": self.runtime_version,
            "pymss_version": self.pymss_version,
            "python_version": self.python_version,
            "variant": self.variant,
            "files": [
                {"path": item.path, "size": item.size, "sha256": item.sha256}
                for item in (files or self.files)
            ],
        }
        if self.torch_wheel is not None:
            payload["torch"] = {
                "version": self.torch_wheel.version,
                "wheel": {
                    "url": "installed://torch",
                    "filename": self.torch_wheel.filename,
                    "size": self.torch_wheel.size,
                    "sha256": self.torch_wheel.sha256,
                },
            }
        return payload


@dataclass(frozen=True)
class RuntimeValidation:
    status: RuntimeStatus
    message: str
    missing: tuple[str, ...] = ()
    damaged: tuple[str, ...] = ()
    package: RuntimePackage | None = None


def _positive_int(value, field: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"PyMSS Runtime 清单字段 {field} 无效。") from exc
    if parsed <= 0:
        raise ValueError(f"PyMSS Runtime 清单字段 {field} 必须大于 0。")
    return parsed


def _nonnegative_int(value, field: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"PyMSS Runtime 清单字段 {field} 无效。") from exc
    if parsed < 0:
        raise ValueError(f"PyMSS Runtime 清单字段 {field} 不能小于 0。")
    return parsed


def _sha256_text(value, field: str) -> str:
    text = str(value or "").strip().lower()
    if len(text) != 64 or any(ch not in "0123456789abcdef" for ch in text):
        raise ValueError(f"PyMSS Runtime 清单字段 {field} 不是有效 SHA-256。")
    return text


def _safe_relative_path(value: str) -> str:
    text = str(value or "").replace("\\", "/").strip()
    path = PurePosixPath(text)
    if not text or path.is_absolute() or ".." in path.parts or path.parts[0] != "runtime":
        raise ValueError(f"PyMSS Runtime 清单包含不安全路径：{value!r}")
    return path.as_posix()


def portable_base_dir() -> Path:
    """托管 Runtime 的便携基准目录：frozen = exe 所在目录。

    与向导的默认安装位置（``default_install_root``）同口径——基准目录
    内的安装以相对路径持久化，整目录搬移 / 多副本共用同一份用户级
    设置时指针始终跟随当前 exe 解析。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path.cwd()


#: 托管 Runtime 安装目录名（与 SUG AI 打轴的 ``ai_runtime`` 约定同名同构，
#: 内层都是 ``runtime/``——两边本质上都是同一份 PyMSS 发行包）。
RUNTIME_DIR_NAME = "ai_runtime"

#: 旧版目录名：仅用于认领/防嵌套兼容，不再作为新安装的默认名。
LEGACY_RUNTIME_DIR_NAME = "pymss"


def resolve_install_dir(raw: str) -> str:
    """读取 ``install_dir`` 设置时的规范化：相对路径按当前基准目录展开。

    旧版存的绝对路径原样通过（过渡兼容）；只有本版本写入的相对值
    才会命中展开分支。
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    path = Path(text).expanduser()
    if path.is_absolute():
        return str(path)
    return str(portable_base_dir() / path)


def relativize_install_dir(path: str) -> str:
    """写入 ``install_dir`` 设置时的规范化：frozen 且位于基准目录内时
    收为相对路径，其余（自定义位置 / 源码运行）原样保留绝对路径。

    实测问题：用户级设置被多副本共用——在下载目录解压试运行并安装
    后，主安装重启即「丢失」分离环境（记录的是旧副本的绝对路径）。
    """
    text = str(path or "").strip()
    if not text or not getattr(sys, "frozen", False):
        return text
    try:
        rel = Path(text).expanduser().resolve().relative_to(
            portable_base_dir().resolve()
        )
        return str(rel)
    except (ValueError, OSError):
        return text


def sha256_file(path: Path, *, cancelled=None) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            if cancelled is not None and cancelled.is_set():
                raise InterruptedError("操作已取消。")
            chunk = stream.read(_CHUNK_SIZE)
            if not chunk:
                return digest.hexdigest()
            digest.update(chunk)


def fetch_runtime_package(
    manifest_url: str,
    *,
    session: requests.Session | None = None,
    timeout: tuple[float, float] = (10.0, 30.0),
) -> RuntimePackage:
    client = session or _default_download_session()
    response = None
    last_error: Exception | None = None
    # GitHub 直链按「官方 → gh-proxy 各节点」接力；单节点失败换下一个，
    # 全部失败才抛最后一个异常（与 SUG updater 的下载链同语义）。
    for candidate in github_url_attempts(manifest_url):
        try:
            response = client.get(candidate, timeout=timeout)
            response.raise_for_status()
            break
        except requests.RequestException as exc:
            last_error = exc
            response = None
    if response is None:
        raise last_error if last_error is not None else RuntimeError(
            "Runtime 清单拉取失败。"
        )
    package = RuntimePackage.from_payload(response.json())
    if package.pymss_version != PYMSS_VERSION:
        raise ValueError(
            f"Runtime 清单要求 PyMSS {package.pymss_version}，工作台要求 {PYMSS_VERSION}。"
        )
    if package.runtime_version != PYMSS_RUNTIME_VERSION:
        raise ValueError(
            "Runtime 清单修订不匹配："
            f"得到 r{package.runtime_version}，工作台要求 r{PYMSS_RUNTIME_VERSION}。"
        )
    if package.python_version != PYMSS_PYTHON_VERSION:
        raise ValueError(
            f"Runtime 清单要求 Python {package.python_version}，"
            f"工作台要求 {PYMSS_PYTHON_VERSION}。"
        )
    if package.torch_wheel is None or package.torch_wheel.version != PYMSS_TORCH_VERSION:
        raise ValueError(
            f"Runtime 清单必须提供 torch {PYMSS_TORCH_VERSION} 官方 wheel。"
        )
    expected_wheel = TORCH_WHEELS.get(package.variant)
    if expected_wheel is None:
        raise ValueError(f"Runtime 清单包含不支持的类型：{package.variant}。")
    actual_wheel = package.torch_wheel
    expected_fields = {
        "url": str(expected_wheel["url"]),
        "filename": str(expected_wheel["filename"]),
        "size": int(expected_wheel["size"]),
        "sha256": str(expected_wheel["sha256"]).lower(),
    }
    actual_fields = {
        "url": actual_wheel.url,
        "filename": actual_wheel.filename,
        "size": actual_wheel.size,
        "sha256": actual_wheel.sha256,
    }
    if actual_fields != expected_fields:
        raise ValueError("Runtime 清单中的 torch wheel 与工作台固定的 PyTorch 官方文件不一致。")
    forbidden_roots = {"torch", "functorch", "torchgen"}
    for item in package.files:
        parts = PurePosixPath(item.path).parts
        if len(parts) < 4 or tuple(part.lower() for part in parts[:3]) != (
            "runtime",
            "lib",
            "site-packages",
        ):
            continue
        name = parts[3].lower()
        if name in forbidden_roots or name.startswith("torch-"):
            raise ValueError("PyMSS 底座清单不得包含 torch；torch 必须由客户端单独下载。")
    return package


def _installed_manifest_path(install_dir: Path) -> Path:
    return install_dir / "manifests" / "runtime-manifest.json"


def resync_installed_manifest(install_dir: str | os.PathLike) -> RuntimeValidation:
    """受信变更后的清单再登记：按磁盘现状重建 installed manifest。

    方案 B 增量安装（AI 打轴向托管解释器 pip 安装依赖）会改动清单
    登记在案的共用包（升级/降级/换 dist-info），下次启动
    ``validate_runtime`` 即报「文件缺失或损坏」。这里以磁盘为准重建
    ``files``：size 与原记录一致的条目沿用原 sha256（免全量哈希），
    新增/变化的文件重新计算；已删除的条目剔除。调用方有两种：宿主
    通知（``note_runtime_changed``，安装方主动上报）或功能仲裁
    （``RealSeparationBackend._arbitrate_damaged_runtime``，起真实
    桥接进程跑能力探测通过后）——后者允许在清单失配但运行时功能
    完好时自愈；裸调本函数仍会无条件合法化任意篡改，不要新增裸调
    调用点。

    Returns:
        重建后的校验结果（READY 即清单与磁盘重新对齐）。
    """
    root = Path(install_dir)
    package = load_installed_package(root)
    old = {item.path: item for item in package.files}
    base = root / "runtime"
    files: list[RuntimeFile] = []
    if base.is_dir():
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            rel = "runtime/" + path.relative_to(base).as_posix()
            try:
                size = path.stat().st_size
            except OSError:
                continue
            prev = old.get(rel)
            if prev is not None and prev.size == size:
                files.append(RuntimeFile(path=rel, size=size, sha256=prev.sha256))
            else:
                files.append(
                    RuntimeFile(path=rel, size=size, sha256=sha256_file(path))
                )
    _atomic_json_write(
        _installed_manifest_path(root), package.installed_payload(tuple(files))
    )
    return validate_runtime(root)


def load_installed_package(install_dir: Path) -> RuntimePackage:
    path = _installed_manifest_path(install_dir)
    with path.open("r", encoding="utf-8") as stream:
        payload = json.load(stream)
    if not payload.get("complete"):
        raise ValueError("PyMSS Runtime 安装尚未完成。")
    # Installed manifests intentionally omit the archive. Supply validated
    # placeholders so one parser defines all version/file invariants.
    parsed = dict(payload)
    parsed["archive"] = {
        "url": "installed://runtime",
        "size": 1,
        "sha256": "0" * 64,
    }
    return RuntimePackage.from_payload(parsed)


def validate_runtime(
    install_dir: str | os.PathLike,
    *,
    full: bool = False,
    expected_variant: str | None = None,
) -> RuntimeValidation:
    root = Path(install_dir)
    if not root.exists():
        return RuntimeValidation(RuntimeStatus.MISSING, "PyMSS 安装目录不存在。")
    manifest = _installed_manifest_path(root)
    if not manifest.is_file():
        return RuntimeValidation(RuntimeStatus.DAMAGED, "缺少 PyMSS Runtime 清单。")
    try:
        package = load_installed_package(root)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return RuntimeValidation(RuntimeStatus.DAMAGED, str(exc))
    if package.pymss_version != PYMSS_VERSION:
        return RuntimeValidation(
            RuntimeStatus.INCOMPATIBLE,
            f"已安装 PyMSS {package.pymss_version}，需要 {PYMSS_VERSION}。",
            package=package,
        )
    if package.runtime_version != PYMSS_RUNTIME_VERSION:
        return RuntimeValidation(
            RuntimeStatus.INCOMPATIBLE,
            f"已安装 Runtime 修订 r{package.runtime_version}，需要 r{PYMSS_RUNTIME_VERSION}。",
            package=package,
        )
    if package.python_version != PYMSS_PYTHON_VERSION:
        return RuntimeValidation(
            RuntimeStatus.INCOMPATIBLE,
            f"已安装 Python {package.python_version}，需要 {PYMSS_PYTHON_VERSION}。",
            package=package,
        )
    if expected_variant and package.variant != expected_variant:
        return RuntimeValidation(
            RuntimeStatus.INCOMPATIBLE,
            f"Runtime 类型为 {package.variant}，当前需要 {expected_variant}。",
            package=package,
        )
    missing: list[str] = []
    damaged: list[str] = []
    for item in package.files:
        path = root / Path(item.path)
        if not path.is_file():
            missing.append(item.path)
            continue
        try:
            if path.stat().st_size != item.size:
                damaged.append(item.path)
            elif full and sha256_file(path) != item.sha256:
                damaged.append(item.path)
        except OSError:
            damaged.append(item.path)
    if missing or damaged:
        return RuntimeValidation(
            RuntimeStatus.DAMAGED,
            "PyMSS Runtime 文件缺失或损坏。",
            tuple(missing),
            tuple(damaged),
            package,
        )
    return RuntimeValidation(RuntimeStatus.READY, "PyMSS Runtime 可用。", package=package)


def _path_is_within(path: Path, parent: Path) -> bool:
    """Case-normalized containment check suitable for Windows paths."""
    path_text = os.path.normcase(str(path.resolve()))
    parent_text = os.path.normcase(str(parent.resolve()))
    try:
        return os.path.commonpath((path_text, parent_text)) == parent_text
    except ValueError:
        return False


def preflight_install_destination(install_dir: str | os.PathLike) -> Path:
    """Validate and write-probe a managed-runtime destination.

    This runs before fetching any large runtime asset.  The probe exercises
    create, write, rename and delete because all four operations are required
    by the staged atomic installer.
    """
    raw = str(install_dir or "").strip()
    if not raw:
        raise ValueError("请选择 PyMSS 安装目录。")
    root = Path(raw).expanduser().resolve()
    if root.exists() and not root.is_dir():
        raise ValueError("PyMSS 安装位置指向了文件，请选择一个目录。")
    if any(part.lower() == "_internal" for part in root.parts):
        raise ValueError("不能把 PyMSS 安装到工作台的 _internal 目录中。")
    updater_temp = Path(tempfile.gettempdir()) / "KaraokeStudioUpdater"
    if _path_is_within(root, updater_temp):
        raise ValueError("不能把 PyMSS 安装到工作台更新器的临时目录中。")

    try:
        root.mkdir(parents=True, exist_ok=True)
        token = uuid.uuid4().hex
        probe = root / f".pymss-write-probe-{token}.tmp"
        renamed = root / f".pymss-write-probe-{token}.ok"
        try:
            with probe.open("xb") as stream:
                stream.write(b"Lin-K Lyrics PyMSS write probe\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(probe, renamed)
        finally:
            probe.unlink(missing_ok=True)
            renamed.unlink(missing_ok=True)
    except OSError as exc:
        raise OSError(f"PyMSS 安装目录不可写，请选择其他目录：{exc}") from exc
    return root


#: 网络抖动重试：首次 + 4 次重试，退避 3/6/12/24s（取消可立即打断睡眠）
_DOWNLOAD_RETRY_DELAYS = (3.0, 6.0, 12.0, 24.0)


def _sleep_cancelable(seconds: float, cancelled) -> bool:
    """分片睡眠；返回 True 表示等待期间被取消。"""
    deadline = time.monotonic() + seconds
    while True:
        if cancelled is not None and cancelled.is_set():
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.2, remaining))


class ManagedRuntimeInstaller:
    """Install a versioned archive without modifying models or user registries."""

    def __init__(self, session: requests.Session | None = None) -> None:
        self._session = session or _default_download_session()

    def install(
        self,
        package: RuntimePackage,
        install_dir: str | os.PathLike,
        *,
        progress: Callable[[int, int], None] | None = None,
        cancelled=None,
        post_install_check: Callable[[Path], None] | None = None,
    ) -> RuntimeValidation:
        root = Path(install_dir)
        staging = root / "staging"
        manifests = root / "manifests"
        staging.mkdir(parents=True, exist_ok=True)
        manifests.mkdir(parents=True, exist_ok=True)
        required_free = self._required_free_bytes(package, root / "runtime")
        available = shutil.disk_usage(root).free
        if available < required_free:
            raise OSError(
                "PyMSS 安装目录空间不足："
                f"至少还需要约 {required_free / 1024**3:.1f} GB，"
                f"当前可用 {available / 1024**3:.1f} GB。"
            )
        token = uuid.uuid4().hex
        archive = staging / f"runtime-{token}.zip.part"
        wheel_part = (
            staging / f"{package.torch_wheel.filename}.part"
            if package.torch_wheel is not None
            else staging / f"torch-{token}.whl.part"
        )
        cached_wheel = (
            staging / package.torch_wheel.filename
            if package.torch_wheel is not None
            else None
        )
        # A previous successful pip run may leave the verified wheel behind
        # when a scanner temporarily blocks cleanup.  Reuse it on repair
        # instead of downloading another multi-gigabyte copy and attempting
        # to replace the still-open destination.
        wheel = (
            cached_wheel
            if cached_wheel is not None and cached_wheel.is_file()
            else wheel_part
        )
        if package.torch_wheel is not None:
            for stale in staging.glob("torch-*.whl.part"):
                if stale not in {wheel, wheel_part}:
                    _unlink_with_retry(stale, tolerate_busy=True)
        # 清理历史失败安装遗留的底座分卷临时文件（每次安装用新 token）
        for stale in staging.glob("runtime-*.zip.part"):
            if stale != archive:
                _unlink_with_retry(stale, tolerate_busy=True)
        payload = staging / f"runtime-{token}"
        backup = staging / f"runtime-backup-{token}"
        payload.mkdir()
        try:
            self._download(package, archive, progress=progress, cancelled=cancelled)
            self._extract(archive, payload, cancelled=cancelled)
            extracted = self._validate_payload(package, payload, cancelled=cancelled)
            if extracted.status is not RuntimeStatus.READY:
                raise ValueError(extracted.message)
            torch_files: tuple[RuntimeFile, ...] = ()
            if package.torch_wheel is not None:
                self._download_wheel(
                    package,
                    wheel,
                    progress=progress,
                    cancelled=cancelled,
                )
                if wheel == cached_wheel and wheel_part.is_file():
                    _unlink_with_retry(wheel_part, tolerate_busy=True)
                torch_files = self._install_torch(
                    package,
                    wheel,
                    payload,
                    cancelled=cancelled,
                )
            installed_files = tuple(package.files) + torch_files
            runtime_dir = root / "runtime"
            manifest_path = _installed_manifest_path(root)
            previous_manifest = manifest_path.read_bytes() if manifest_path.is_file() else None
            if runtime_dir.exists():
                _replace_with_retry(runtime_dir, backup)
            try:
                _replace_with_retry(payload / "runtime", runtime_dir)
                _atomic_json_write(
                    manifest_path,
                    package.installed_payload(installed_files),
                )
                final_validation = validate_runtime(
                    root, full=True, expected_variant=package.variant
                )
                if final_validation.status is not RuntimeStatus.READY:
                    raise RuntimeError(
                        f"PyMSS 新 Runtime 切换后复检失败：{final_validation.message}"
                    )
                if post_install_check is not None:
                    post_install_check(root)
            except Exception:
                if runtime_dir.exists():
                    shutil.rmtree(runtime_dir, ignore_errors=True)
                if backup.exists():
                    _replace_with_retry(backup, runtime_dir)
                if previous_manifest is None:
                    manifest_path.unlink(missing_ok=True)
                else:
                    _atomic_bytes_write(manifest_path, previous_manifest)
                raise
            if backup.exists():
                shutil.rmtree(backup, ignore_errors=True)
            return final_validation
        finally:
            if archive.exists():
                archive.unlink(missing_ok=True)
            if payload.exists():
                shutil.rmtree(payload, ignore_errors=True)

    @staticmethod
    def _required_free_bytes(package: RuntimePackage, current_runtime: Path) -> int:
        base_unpacked = sum(item.size for item in package.files)
        torch_peak = int((package.torch_wheel.size if package.torch_wheel else 0) * 2.5)
        current_size = 0
        if current_runtime.is_dir():
            for path in current_runtime.rglob("*"):
                try:
                    if path.is_file():
                        current_size += path.stat().st_size
                except OSError:
                    continue
        return (
            package.archive_size
            + base_unpacked
            + torch_peak
            + current_size
            + 512 * 1024**2
        )

    def _download(self, package, destination, *, progress=None, cancelled=None) -> None:
        """下载底座分卷并整体校验；网络抖动按退避重试（每次重下整档）。

        底座约 150MB 且当前 release 为单分卷，重试整档重下成本可控，
        不做字节级续传；torch wheel（数 GB）在 _download_wheel 里走
        Range 断点续传。大小不符视为连接中断的截断，同样重试；SHA
        不匹配是内容问题，不重试直接失败。

        分卷 URL 为 GitHub 直链时，重试轮次按「官方 → gh-proxy 各节点」
        尝试链轮换（与 SUG updater 的下载链同语义）：第 N 轮重试用链中
        第 N 个候选，链耗尽后停留末尾节点；非 GitHub URL（测试镜像）链
        长为 1，行为与原版一致。
        """
        import requests as _requests

        chains = {
            part.url: github_url_attempts(part.url)
            for part in package.archive_parts
        }
        chain_length = max((len(chain) for chain in chains.values()), default=1)
        # 链内每个候选立即轮换一轮；链耗尽后再按退避表整体重试（clamp 到
        # 末候选），保留原版对网络抖动的恢复能力。
        attempts = chain_length + len(_DOWNLOAD_RETRY_DELAYS)
        last_error: Exception | None = None
        for attempt in range(attempts):
            if cancelled is not None and cancelled.is_set():
                raise InterruptedError("下载已取消。")
            try:
                self._download_once(
                    package,
                    destination,
                    progress=progress,
                    cancelled=cancelled,
                    url_chain=chains,
                    chain_index=attempt,
                )
                return
            except InterruptedError:
                raise
            except _requests.RequestException as exc:
                # 连接中断/超时在 iter_content 里都以 RequestException
                # 抛出；大小/校验不符是内容问题，立即失败不重试。
                # HTTP 4xx（如资产缺失 404）是确定性失败，重试只会
                # 白烧退避时间；5xx 视为瞬态可重试
                if isinstance(exc, _requests.HTTPError):
                    status = getattr(getattr(exc, "response", None), "status_code", 0)
                    if 400 <= status < 500:
                        raise
                last_error = exc
            if attempt < attempts - 1:
                # 链内换源（GitHub → 各镜像节点）不退避、立即尝试下一候选；
                # 链耗尽后的整体重试才按退避表等待（网络抖动恢复）。
                if attempt < chain_length - 1:
                    delay = 0.0
                elif attempt - (chain_length - 1) < len(_DOWNLOAD_RETRY_DELAYS):
                    delay = _DOWNLOAD_RETRY_DELAYS[
                        attempt - (chain_length - 1)
                    ]
                else:
                    delay = _DOWNLOAD_RETRY_DELAYS[-1]
                if delay <= 0:
                    if cancelled is not None and cancelled.is_set():
                        raise InterruptedError("下载已取消。")
                    continue
                if _sleep_cancelable(delay, cancelled):
                    raise InterruptedError("下载已取消。")
        raise ValueError(
            f"PyMSS Runtime 下载失败（已重试 {attempts - 1} 次）：{last_error}"
        )

    def _download_once(
        self,
        package,
        destination,
        *,
        progress=None,
        cancelled=None,
        url_chain: dict[str, tuple[str, ...]] | None = None,
        chain_index: int = 0,
    ) -> None:
        done = 0
        archive_digest = hashlib.sha256()
        with destination.open("wb") as stream:
            for index, part in enumerate(package.archive_parts, start=1):
                chain = (url_chain or {}).get(part.url) or (part.url,)
                url = chain[min(chain_index, len(chain) - 1)]
                part_done = 0
                part_digest = hashlib.sha256()
                with self._session.get(url, stream=True, timeout=(10.0, 60.0)) as response:
                    response.raise_for_status()
                    for chunk in response.iter_content(_CHUNK_SIZE):
                        if cancelled is not None and cancelled.is_set():
                            raise InterruptedError("下载已取消。")
                        if not chunk:
                            continue
                        stream.write(chunk)
                        archive_digest.update(chunk)
                        part_digest.update(chunk)
                        part_done += len(chunk)
                        done += len(chunk)
                        if progress is not None:
                            progress(done, package.download_size)
                if part_done != part.size:
                    raise ValueError(
                        f"PyMSS Runtime 第 {index} 个分片大小不符：得到 {part_done}，应为 {part.size}。"
                    )
                if part_digest.hexdigest() != part.sha256:
                    raise ValueError(f"PyMSS Runtime 第 {index} 个分片校验失败。")
            stream.flush()
            os.fsync(stream.fileno())
        if done != package.archive_size:
            raise ValueError(
                f"PyMSS Runtime 下载大小不符：得到 {done}，应为 {package.archive_size}。"
            )
        if archive_digest.hexdigest() != package.archive_sha256:
            raise ValueError("PyMSS Runtime 下载校验失败（SHA-256 不匹配）。")

    def _download_wheel(self, package, destination, *, progress=None, cancelled=None) -> None:
        dependency = package.torch_wheel
        if dependency is None:
            return
        done = 0
        digest = hashlib.sha256()
        if destination.is_file():
            current_size = destination.stat().st_size
            if current_size > dependency.size:
                destination.unlink()
            else:
                with destination.open("rb") as existing:
                    for chunk in iter(lambda: existing.read(_CHUNK_SIZE), b""):
                        if cancelled is not None and cancelled.is_set():
                            raise InterruptedError("torch 下载已取消。")
                        digest.update(chunk)
                        done += len(chunk)
                if done == dependency.size:
                    if digest.hexdigest() == dependency.sha256:
                        if progress is not None:
                            progress(package.download_size, package.download_size)
                        return
                    destination.unlink()
                    done = 0
                    digest = hashlib.sha256()
        # 网络抖动退避重试：每次重试从已落盘偏移 Range 续传（不重下
        # 已完成的数 GB 内容）；大小不符=截断可重试，校验失败不重试
        import requests as _requests

        attempts = len(_DOWNLOAD_RETRY_DELAYS) + 1
        last_error: Exception | None = None
        state = {"done": done, "digest": digest}
        for attempt in range(attempts):
            if cancelled is not None and cancelled.is_set():
                raise InterruptedError("torch 下载已取消。")
            try:
                self._download_wheel_segment(
                    package,
                    dependency,
                    destination,
                    state=state,
                    progress=progress,
                    cancelled=cancelled,
                )
                break
            except InterruptedError:
                raise
            except _requests.RequestException as exc:
                if isinstance(exc, _requests.HTTPError):
                    status = getattr(getattr(exc, "response", None), "status_code", 0)
                    if 400 <= status < 500:
                        raise
                last_error = exc
            if attempt < attempts - 1:
                if _sleep_cancelable(
                    _DOWNLOAD_RETRY_DELAYS[attempt], cancelled
                ):
                    raise InterruptedError("torch 下载已取消。")
        else:
            raise ValueError(
                f"torch wheel 下载失败（已重试 {attempts - 1} 次）：{last_error}"
            )
        if state["done"] != dependency.size:
            if state["done"] > dependency.size:
                destination.unlink(missing_ok=True)
            raise ValueError(
                f"torch wheel 下载大小不符：得到 {state['done']}，应为 {dependency.size}。"
            )
        if state["digest"].hexdigest() != dependency.sha256:
            destination.unlink(missing_ok=True)
            raise ValueError("torch wheel 校验失败（SHA-256 不匹配）。")

    def _download_wheel_segment(
        self,
        package,
        dependency,
        destination,
        *,
        state: dict,
        progress=None,
        cancelled=None,
    ) -> None:
        """从 state["done"] 偏移续传一段下载；state 原地更新（可重入）。"""
        done = state["done"]
        digest = state["digest"]
        headers = {"Range": f"bytes={done}-"} if done else None
        with self._session.get(
            dependency.url,
            stream=True,
            timeout=(10.0, 120.0),
            headers=headers,
        ) as response:
            response.raise_for_status()
            append = done > 0 and int(getattr(response, "status_code", 200)) == 206
            if done and not append:
                done = 0
                digest = hashlib.sha256()
            with destination.open("ab" if append else "wb") as stream_out:
                for chunk in response.iter_content(_CHUNK_SIZE):
                    if cancelled is not None and cancelled.is_set():
                        raise InterruptedError("torch 下载已取消。")
                    if not chunk:
                        continue
                    stream_out.write(chunk)
                    digest.update(chunk)
                    done += len(chunk)
                    state["done"] = done
                    state["digest"] = digest
                    if progress is not None:
                        progress(package.archive_size + done, package.download_size)
                stream_out.flush()
                os.fsync(stream_out.fileno())
        state["done"] = done
        state["digest"] = digest

    def _install_torch(
        self,
        package: RuntimePackage,
        wheel: Path,
        payload: Path,
        *,
        cancelled=None,
    ) -> tuple[RuntimeFile, ...]:
        dependency = package.torch_wheel
        if dependency is None:
            return ()
        runtime = payload / "runtime"
        python = runtime / "python.exe"
        if not python.is_file():
            raise ValueError("PyMSS 底座缺少嵌入式 python.exe。")
        target = runtime / "Lib" / "site-packages"
        target.mkdir(parents=True, exist_ok=True)
        before = {
            path.relative_to(payload).as_posix()
            for path in runtime.rglob("*")
            if path.is_file()
        }
        final_wheel = wheel.with_name(dependency.filename)
        if wheel != final_wheel:
            _replace_with_retry(wheel, final_wheel)
        command = [
            str(python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-index",
            "--no-deps",
            "--no-compile",
            "--target",
            str(target),
            str(final_wheel),
        ]
        environment = os.environ.copy()
        environment["PYTHONNOUSERSITE"] = "1"
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        process = subprocess.Popen(
            command,
            cwd=str(runtime),
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **hidden_subprocess_kwargs(),
        )
        try:
            while process.poll() is None:
                if cancelled is not None and cancelled.is_set():
                    process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
                    raise InterruptedError("torch 安装已取消。")
                time.sleep(0.05)
            if process.returncode != 0:
                raise RuntimeError(f"私有 pip 安装 torch 失败，退出码 {process.returncode}。")
        finally:
            # pip has already exited here, but Windows Defender and other file
            # scanners can briefly retain a handle to a multi-gigabyte wheel.
            # Cleanup must never turn an otherwise successful installation
            # into WinError 32.  A later repair can remove a rare leftover.
            _unlink_with_retry(final_wheel, tolerate_busy=True)

        smoke = subprocess.run(
            [
                str(python),
                "-c",
                (
                    "import torch; "
                    f"assert torch.__version__.split('+', 1)[0] == '{dependency.version}'"
                ),
            ],
            cwd=str(runtime),
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=60,
            **hidden_subprocess_kwargs(),
        )
        if smoke.returncode != 0:
            raise RuntimeError("torch 安装后导入冒烟失败。")
        added = sorted(
            (
                path
                for path in runtime.rglob("*")
                if path.is_file() and path.relative_to(payload).as_posix() not in before
            ),
            key=lambda path: path.relative_to(payload).as_posix().lower(),
        )
        if not added:
            raise RuntimeError("私有 pip 未安装任何 torch 文件。")
        return tuple(
            RuntimeFile(
                path=path.relative_to(payload).as_posix(),
                size=path.stat().st_size,
                sha256=sha256_file(path, cancelled=cancelled),
            )
            for path in added
        )

    def _extract(self, archive: Path, destination: Path, *, cancelled=None) -> None:
        with zipfile.ZipFile(archive) as bundle:
            for member in bundle.infolist():
                if cancelled is not None and cancelled.is_set():
                    raise InterruptedError("解压已取消。")
                rel = PurePosixPath(member.filename)
                mode = member.external_attr >> 16
                if rel.is_absolute() or ".." in rel.parts or stat.S_ISLNK(mode):
                    raise ValueError(f"PyMSS Runtime 压缩包包含不安全路径：{member.filename!r}")
                if not rel.parts or rel.parts[0] != "runtime":
                    raise ValueError("PyMSS Runtime 压缩包必须只包含 runtime/ 目录。")
                target = destination.joinpath(*rel.parts)
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(member) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output, _CHUNK_SIZE)

    def _validate_payload(self, package, payload, *, cancelled=None) -> RuntimeValidation:
        missing: list[str] = []
        damaged: list[str] = []
        for item in package.files:
            if cancelled is not None and cancelled.is_set():
                raise InterruptedError("校验已取消。")
            path = payload / Path(item.path)
            if not path.is_file():
                missing.append(item.path)
            elif path.stat().st_size != item.size or sha256_file(path, cancelled=cancelled) != item.sha256:
                damaged.append(item.path)
        if missing or damaged:
            return RuntimeValidation(
                RuntimeStatus.DAMAGED,
                "解压后的 PyMSS Runtime 文件不完整。",
                tuple(missing),
                tuple(damaged),
                package,
            )
        return RuntimeValidation(RuntimeStatus.READY, "PyMSS Runtime 包校验通过。", package=package)


def _atomic_json_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_bytes_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _replace_with_retry(source: Path, destination: Path) -> None:
    """Retry Windows directory swaps while scanners release executable files."""
    last_error: OSError | None = None
    for attempt in range(30):
        try:
            os.replace(source, destination)
            return
        except PermissionError as exc:
            last_error = exc
            time.sleep(min(0.05 * (attempt + 1), 0.5))
    if last_error is not None:
        raise last_error


def _unlink_with_retry(path: Path, *, tolerate_busy: bool = False) -> None:
    """Retry deletion while Windows scanners release a recently read file."""
    last_error: PermissionError | None = None
    for attempt in range(30):
        try:
            path.unlink(missing_ok=True)
            return
        except PermissionError as exc:
            last_error = exc
            time.sleep(min(0.05 * (attempt + 1), 0.5))
    if last_error is not None and not tolerate_busy:
        raise last_error


__all__ = [
    "ManagedRuntimeInstaller",
    "RuntimeFile",
    "RuntimeArchivePart",
    "RuntimePackage",
    "RuntimeStatus",
    "RuntimeValidation",
    "fetch_runtime_package",
    "load_installed_package",
    "preflight_install_destination",
    "sha256_file",
    "validate_runtime",
]
