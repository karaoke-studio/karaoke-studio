"""增量更新分包：app / runtime part zip + manifest + 全量 zip（Windows）。

在 ``scripts/build_windows.bat`` 完成主程序构建与内容校验后调用。产物全部落在
``dist/windows/``：

* ``KaraokeStudio-windows-app.zip``（+ ``.sha256``）——
  EXE + Updater.exe + GPU sidecar + ``_internal/krok_helper`` +
  ``_internal/strange_uta_game``，
  每次发版都变化，日常 bugfix 用户只需下载这个（约 65 MB）。
* ``KaraokeStudio-windows-runtime.zip``（+ ``.sha256``）——
  ``_internal/`` 其余全部内容（约 150 MB），依赖不变时跨版本复用同一份 zip，
  保证内容哈希稳定、老用户不重复下载。
* ``KaraokeStudio-windows.json`` —— 对外发布的增量清单（schema=1）。
  文件名必须是这个：存量客户端的 Updater 用
  ``asset_name.replace("StrangeUtaGame","manifest",1).replace(".zip",".json")``
  派生 manifest 名，对 ``KaraokeStudio-windows.zip`` 的派生结果就是它。
* ``KaraokeStudio-windows.zip``（+ ``.sha256``）—— 全量包（含出厂
  ``_internal/.installed_manifest.json``），旧客户端全量兜底路径继续使用。

runtime 复用（跨 CI 构建的确定性）：
    PyInstaller 重打包会引入字节级差异（pyc 时间戳等），干净环境重打 runtime
    必然算出新哈希，令所有老用户被迫重下 150 MB。因此依赖未变时必须复用上一版
    release 的 runtime zip 原文件。状态不走 git 缓存，而是随 release 资产走：
    manifest 里额外记录 ``build.dist_packages``（_internal 里的 .dist-info 扫描）
    与 ``build.freeze_hash``（构建环境 pip freeze 过滤后哈希），CI 在构建前把
    上一版的 manifest 与 runtime zip 下载到 ``dist/windows/prev/``，本脚本比对
    两个指纹，全部一致才复用。（Updater 只读 schema/parts/full，额外字段无害。）

与 ``updater_app/main.py``（SUG，随包分发给所有存量客户端）的契约：
    manifest schema=1；``parts.<id> = {asset, sha256, size, targets}``；
    part 的 ``sha256`` 是 **zip 内容哈希**（路径+内容，见 _content_hash_of_zip），
    不是 zip 文件本身的 sha256；``targets`` 是相对 app 根目录的路径列表；
    ``.sha256`` 旁车文件才是 zip 原文件哈希（coreutils 格式）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple


def _force_utf8_stdio() -> None:
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


_force_utf8_stdio()

ROOT = Path(__file__).resolve().parents[1]
DIST_ROOT = ROOT / "dist" / "windows"
APP_DIR_NAME = "Karaoke Studio"
APP_EXE_NAME = "Karaoke Studio.exe"
UPDATER_EXE_NAME = "Updater.exe"
NATIVE_RENDERER_EXE_NAME = "krok_subtitle_renderer.exe"
ASSET_BASE = "KaraokeStudio-windows"
MANIFEST_SCHEMA = 1
LOCAL_MANIFEST_FILENAME = ".installed_manifest.json"
# 发布端 runtime 收集策略版本。改变 PyInstaller 的模块/插件收集规则不一定会
# 改变包版本或 pip freeze；显式 profile 用来阻止错误复用缺少新组件的旧 runtime。
# Updater 不读取 build 字段，因此该值不影响存量客户端协议。
RUNTIME_PROFILE = "qt-multimedia-v1"

# _internal 下不属于 runtime 的顶层条目（属于 app part 或本地状态）。
INTERNAL_NON_RUNTIME_NAMES = {
    "krok_helper",
    "strange_uta_game",
    LOCAL_MANIFEST_FILENAME,
}

APP_TARGETS = [
    APP_EXE_NAME,
    UPDATER_EXE_NAME,
    NATIVE_RENDERER_EXE_NAME,
    "_internal/krok_helper",
    "_internal/strange_uta_game",
]

# pip freeze 指纹的排除清单：仅排除确定不进包的构建工具。
# 宁可少排（多触发一次 runtime 重打）也不多排（漏掉真实依赖变化）。
FREEZE_EXCLUDES = {
    "pip",
    "setuptools",
    "wheel",
    "pyinstaller",
    "pyinstaller-hooks-contrib",
    "altgraph",
    "pefile",
}


def read_app_version() -> str:
    text = (ROOT / "krok_helper" / "config.py").read_text(encoding="utf-8")
    m = re.search(r'APP_VERSION\s*=\s*"([^"]+)"', text)
    if not m:
        raise SystemExit("无法从 krok_helper/config.py 解析 APP_VERSION")
    return m.group(1)


# ───────────────────────── 哈希 ─────────────────────────


def sha256_of_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(64 * 1024), b""):
            h.update(chunk)
    return h.hexdigest().lower()


def content_hash_of_zip(zip_path: Path) -> str:
    """zip 内容哈希（路径+内容，确定性），必须与 updater_app/main.py 完全一致。"""
    entries: List[Tuple[str, str]] = []
    with zipfile.ZipFile(str(zip_path), "r") as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            content = zf.read(info.filename)
            entries.append((info.filename, hashlib.sha256(content).hexdigest()))
    entries.sort(key=lambda e: e[0])
    combined = "\n".join(f"{name}:{h}" for name, h in entries)
    return hashlib.sha256(combined.encode("ascii")).hexdigest().lower()


def write_sha256_sidecar(target: Path) -> Path:
    digest = sha256_of_file(target)
    sidecar = target.with_name(target.name + ".sha256")
    sidecar.write_text(f"{digest}  {target.name}\n", encoding="ascii")
    print(f"  + {sidecar.name}  (sha256={digest})")
    return sidecar


# ───────────────────────── 构建指纹 ─────────────────────────


def scan_dist_packages(app_dir: Path) -> Dict[str, str]:
    """扫描 ``_internal/*.dist-info``，返回 ``{规范名: 版本}``。"""
    internal = app_dir / "_internal"
    result: Dict[str, str] = {}
    if not internal.is_dir():
        return result
    for dist_info in sorted(internal.glob("*.dist-info")):
        metadata = dist_info / "METADATA"
        if not metadata.exists():
            metadata = dist_info / "PKG-INFO"
        if not metadata.exists():
            continue
        name = version = ""
        try:
            for line in metadata.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("Name:"):
                    name = line.split(":", 1)[1].strip().lower().replace("_", "-")
                elif line.startswith("Version:"):
                    version = line.split(":", 1)[1].strip()
                if name and version:
                    break
        except OSError:
            continue
        if name and version:
            result[name] = version
    return result


def freeze_fingerprint() -> Tuple[str, List[str]]:
    """构建环境 ``pip freeze`` 过滤后的行 + 哈希。

    KS 的构建依赖不锁版本（bat 里 ensure_pkg 装 pip 当时解析的版本），dist-info
    扫描只覆盖随包携带元数据的包；freeze 指纹补上其余依赖的版本漂移检测。
    """
    try:
        output = subprocess.run(
            [sys.executable, "-m", "pip", "freeze"],
            capture_output=True,
            text=True,
            timeout=120,
            check=True,
        ).stdout
    except Exception as exc:  # noqa: BLE001
        print(f"  ! pip freeze 失败（freeze 指纹置空，将总是重打 runtime）: {exc}")
        return "", []
    lines: List[str] = []
    for raw in output.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("-e "):
            continue
        pkg = line.split("==")[0].split(" @ ")[0].strip().lower().replace("_", "-")
        if pkg in FREEZE_EXCLUDES:
            continue
        lines.append(line)
    lines.sort()
    digest = hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest().lower()
    return digest, lines


# ───────────────────────── 打包 ─────────────────────────


def compute_runtime_targets(app_dir: Path) -> List[str]:
    internal = app_dir / "_internal"
    if not internal.is_dir():
        raise SystemExit(f"找不到 {internal}")
    out: List[str] = []
    for child in sorted(internal.iterdir(), key=lambda p: p.name.lower()):
        if child.name in INTERNAL_NON_RUNTIME_NAMES:
            continue
        out.append(f"_internal/{child.name}")
    return out


def pack_part_zip(zip_path: Path, app_dir: Path, targets: List[str]) -> None:
    """把 app 根目录下 targets 列出的内容打成一个 zip（arcname = 相对 app 根路径）。"""
    missing = [target for target in targets if not (app_dir / target).exists()]
    if missing:
        raise SystemExit(
            "app part 缺少必需 target：" + "、".join(missing)
        )
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(str(zip_path), "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for target in targets:
            src = app_dir / target
            if src.is_file():
                zf.write(src, arcname=target)
            else:
                for f in sorted(src.rglob("*")):
                    if f.is_file():
                        rel = f.relative_to(app_dir)
                        zf.write(f, arcname=str(rel).replace("\\", "/"))


def pack_full_zip(zip_path: Path, app_dir: Path) -> None:
    """全量 zip，保持既有布局：zip 内带 ``Karaoke Studio/`` 单一顶层目录。"""
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(str(zip_path), "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for f in sorted(app_dir.rglob("*")):
            if f.is_file():
                rel = f.relative_to(app_dir.parent)
                zf.write(f, arcname=str(rel).replace("\\", "/"))


# ───────────────────────── runtime 复用 ─────────────────────────


def load_prev_manifest(prev_dir: Path) -> Optional[dict]:
    p = prev_dir / f"{ASSET_BASE}.json"
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"  ! 上一版 manifest 解析失败（忽略复用）: {exc}")
        return None
    return data if isinstance(data, dict) else None


def try_reuse_runtime(
    prev_dir: Path,
    runtime_zip: Path,
    dist_packages: Dict[str, str],
    freeze_hash: str,
    require_reuse: bool,
) -> Optional[str]:
    """依赖指纹一致时复用上一版 runtime zip。

    成功返回其内容哈希（直接取上一版 manifest 记录，避免重算 150 MB zip），
    不能复用返回 ``None``。
    """
    prev_manifest = load_prev_manifest(prev_dir)
    if prev_manifest is None:
        print("  未找到上一版 manifest（首次分包发布），重新打包 runtime")
        return None

    prev_build = prev_manifest.get("build") or {}
    prev_pkgs = prev_build.get("dist_packages") or {}
    prev_freeze = prev_build.get("freeze_hash") or ""
    prev_profile = prev_build.get("runtime_profile") or ""
    prev_runtime = (prev_manifest.get("parts") or {}).get("runtime") or {}
    prev_sha = prev_runtime.get("sha256") or ""

    if not prev_pkgs or not prev_sha:
        print("  上一版 manifest 缺少构建指纹或 runtime sha256，重新打包 runtime")
        return None
    if prev_profile != RUNTIME_PROFILE:
        print(
            "  runtime 收集策略已变化"
            f"（{prev_profile or '旧版未记录'} -> {RUNTIME_PROFILE}），重新打包 runtime"
        )
        return None
    if prev_pkgs != dist_packages:
        added = sorted(set(dist_packages) - set(prev_pkgs))
        removed = sorted(set(prev_pkgs) - set(dist_packages))
        changed = sorted(
            k for k in set(prev_pkgs) & set(dist_packages) if prev_pkgs[k] != dist_packages[k]
        )
        print("  dist-info 包版本已变化，重新打包 runtime")
        for label, names in (("新增", added), ("移除", removed), ("变更", changed)):
            if names:
                print(f"    {label}: {', '.join(names)}")
        return None
    if not freeze_hash or not prev_freeze or prev_freeze != freeze_hash:
        print("  构建环境 pip freeze 指纹不一致（或缺失），重新打包 runtime")
        return None

    prev_zip = prev_dir / f"{ASSET_BASE}-runtime.zip"
    if not prev_zip.exists():
        message = (
            "依赖指纹与上一版完全一致，本应复用上一版 runtime zip，"
            f"但 {prev_zip} 不存在。CI 应在构建前把上一版 runtime zip 下载到该位置；"
            "重打 runtime 会改变内容哈希，令所有老用户被迫重下 runtime。"
        )
        if require_reuse:
            raise SystemExit(f"x --require-runtime-reuse: {message}")
        print(f"  ! {message}")
        return None

    print("  校验上一版 runtime zip 内容哈希...")
    actual = content_hash_of_zip(prev_zip)
    if actual != prev_sha.lower():
        message = f"上一版 runtime zip 内容哈希不匹配（期望 {prev_sha[:12]}…，实际 {actual[:12]}…）"
        if require_reuse:
            raise SystemExit(f"x --require-runtime-reuse: {message}")
        print(f"  ! {message}，重新打包 runtime")
        return None

    if runtime_zip.exists():
        runtime_zip.unlink()
    import shutil

    shutil.copy2(str(prev_zip), str(runtime_zip))
    print(f"  = 复用上一版 runtime zip → {runtime_zip.name}（用户不会重新下载）")
    return actual


# ───────────────────────── 清单 ─────────────────────────


def part_payload(zip_path: Path, targets: List[str], content_hash: str) -> dict:
    return {
        "asset": zip_path.name,
        "sha256": content_hash,
        "size": zip_path.stat().st_size,
        "targets": list(targets),
    }


def write_installed_manifest(app_dir: Path, version: str, parts: dict) -> Path:
    payload = {
        "version": version,
        "schema": MANIFEST_SCHEMA,
        "parts": {
            pid: {
                "sha256": info["sha256"],
                "asset": info["asset"],
                "targets": list(info["targets"]),
            }
            for pid, info in parts.items()
        },
        "installed_at": int(time.time()),
    }
    p = app_dir / "_internal" / LOCAL_MANIFEST_FILENAME
    p.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"  + 出厂本地清单: {p.relative_to(app_dir.parent)}")
    return p


def write_release_manifest(
    out_dir: Path,
    version: str,
    parts: dict,
    full_zip: Path,
    dist_packages: Dict[str, str],
    freeze_hash: str,
) -> Path:
    manifest = {
        "version": version,
        "schema": MANIFEST_SCHEMA,
        "parts": parts,
        "full": {
            "asset": full_zip.name,
            "sha256": content_hash_of_zip(full_zip),
            "size": full_zip.stat().st_size,
        },
        # Updater 只读 schema/parts/full；build 是发布端自用的复用指纹。
        "build": {
            "dist_packages": dist_packages,
            "freeze_hash": freeze_hash,
            "runtime_profile": RUNTIME_PROFILE,
        },
    }
    p = out_dir / f"{ASSET_BASE}.json"
    p.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"  + {p.name}")
    return p


# ───────────────────────── 主流程 ─────────────────────────


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--dist-root",
        type=Path,
        default=DIST_ROOT,
        help="构建产物根目录（默认 dist/windows）",
    )
    parser.add_argument(
        "--prev-dir",
        type=Path,
        default=None,
        help="上一版 release 资产目录（manifest + runtime zip，默认 <dist-root>/prev）",
    )
    parser.add_argument(
        "--require-runtime-reuse",
        action="store_true",
        help="依赖未变却无法复用上一版 runtime zip 时报错退出（CI 安全闸）",
    )
    args = parser.parse_args(argv)

    dist_root: Path = args.dist_root
    prev_dir: Path = args.prev_dir or (dist_root / "prev")
    app_dir = dist_root / APP_DIR_NAME
    if not (app_dir / APP_EXE_NAME).exists():
        raise SystemExit(f"找不到 {app_dir / APP_EXE_NAME}，请先运行 build_windows.bat")
    if not (app_dir / UPDATER_EXE_NAME).exists():
        raise SystemExit(f"找不到 {app_dir / UPDATER_EXE_NAME}")

    version = read_app_version()
    print(f"== 增量分包 v{version} ==")

    # 清理上次构建可能残留的出厂清单，确保 part zip 与内容哈希不受污染
    stale = app_dir / "_internal" / LOCAL_MANIFEST_FILENAME
    if stale.exists():
        stale.unlink()

    runtime_targets = compute_runtime_targets(app_dir)
    print(f"[1/5] 打包 app part（{len(APP_TARGETS)} targets）...")
    app_zip = dist_root / f"{ASSET_BASE}-app.zip"
    pack_part_zip(app_zip, app_dir, APP_TARGETS)
    app_hash = content_hash_of_zip(app_zip)
    print(f"  + {app_zip.name}  ({app_zip.stat().st_size / 1024 / 1024:.1f} MB)")
    write_sha256_sidecar(app_zip)

    print(f"[2/5] 打包 runtime part（{len(runtime_targets)} targets）...")
    dist_packages = scan_dist_packages(app_dir)
    freeze_hash, _freeze_lines = freeze_fingerprint()
    print(f"  dist-info: {len(dist_packages)} 个包; freeze 指纹: {freeze_hash[:12] or '（空）'}…")
    runtime_zip = dist_root / f"{ASSET_BASE}-runtime.zip"
    runtime_hash = try_reuse_runtime(
        prev_dir, runtime_zip, dist_packages, freeze_hash, args.require_runtime_reuse
    )
    if runtime_hash is None:
        pack_part_zip(runtime_zip, app_dir, runtime_targets)
        runtime_hash = content_hash_of_zip(runtime_zip)
    print(f"  + {runtime_zip.name}  ({runtime_zip.stat().st_size / 1024 / 1024:.1f} MB)")
    write_sha256_sidecar(runtime_zip)

    parts = {
        "app": part_payload(app_zip, APP_TARGETS, app_hash),
        "runtime": part_payload(runtime_zip, runtime_targets, runtime_hash),
    }

    print("[3/5] 写出厂本地清单...")
    write_installed_manifest(app_dir, version, parts)

    print("[4/5] 打包全量 zip（含出厂清单）...")
    full_zip = dist_root / f"{ASSET_BASE}.zip"
    pack_full_zip(full_zip, app_dir)
    print(f"  + {full_zip.name}  ({full_zip.stat().st_size / 1024 / 1024:.1f} MB)")
    write_sha256_sidecar(full_zip)

    print("[5/5] 写发布 manifest...")
    write_release_manifest(dist_root, version, parts, full_zip, dist_packages, freeze_hash)

    print()
    print("发布资产（全部上传到 GitHub Release）：")
    for name in (
        f"{ASSET_BASE}.zip",
        f"{ASSET_BASE}.zip.sha256",
        f"{ASSET_BASE}.json",
        f"{ASSET_BASE}-app.zip",
        f"{ASSET_BASE}-app.zip.sha256",
        f"{ASSET_BASE}-runtime.zip",
        f"{ASSET_BASE}-runtime.zip.sha256",
    ):
        print(f"  - dist/windows/{name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
