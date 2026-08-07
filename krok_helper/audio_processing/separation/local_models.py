"""识别用户手动放进 ``models/`` 的模型（自动导入）。

用户可能从别处拷贝已有权重，只要按工作台下载时的目录结构摆放，就应当被直接认成
「已下载」，不需要再去设置里选一遍。

判定依据是托管 Runtime 自带的 ``model_catalog.json``：它给出每个模型的权重相对路径、
配置相对路径与**确切字节数**。因此本模块完全离线工作——服务没启动时也能判断，
任务卡不会在明明有模型的情况下显示「需下载」。

大小必须逐字节吻合：拷贝了一半或下载中断的文件不得被认成可用模型（§8.4）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CatalogEntry:
    """catalog 中一个模型的落盘信息。"""

    name: str
    relpath: str
    config_relpath: str
    size_bytes: int

    def weight_path(self, models_dir: Path) -> Path:
        return models_dir / self.relpath

    def config_path(self, models_dir: Path) -> Path:
        return models_dir / self.config_relpath


def catalog_file(install_dir: str | Path) -> Path:
    """托管 Runtime 内置的 catalog 文件路径。"""
    return (
        Path(install_dir)
        / "runtime"
        / "Lib"
        / "site-packages"
        / "pymss"
        / "resources"
        / "model_catalog.json"
    )


def load_catalog(install_dir: str | Path) -> dict[str, CatalogEntry]:
    """读取托管 Runtime 的模型 catalog；读不到时返回空表（不抛异常）。"""
    path = catalog_file(install_dir)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    models = payload.get("models")
    if not isinstance(models, list):
        return {}

    entries: dict[str, CatalogEntry] = {}
    for row in models:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()
        relpath = str(row.get("relpath") or "").strip()
        if not name or not relpath:
            continue
        entry = CatalogEntry(
            name=name,
            relpath=relpath,
            config_relpath=str(row.get("config_relpath") or "").strip(),
            size_bytes=int(row.get("size_bytes") or 0),
        )
        entries[name] = entry
        # catalog 里同一个模型有带 .ckpt 与不带后缀两种别名，预设可能用任意一种。
        for alias in row.get("aliases") or []:
            alias_name = str(alias or "").strip()
            if alias_name:
                entries.setdefault(alias_name, entry)
    return entries


def _is_complete(entry: CatalogEntry, models_dir: Path) -> bool:
    weight = entry.weight_path(models_dir)
    try:
        if not weight.is_file():
            return False
        # 大小必须与 catalog 完全一致：半拷贝/中断的文件不算数（§8.4）。
        if entry.size_bytes and weight.stat().st_size != entry.size_bytes:
            return False
    except OSError:
        return False
    if entry.config_relpath and not entry.config_path(models_dir).is_file():
        return False
    return True


def scan_local_models(
    install_dir: str | Path,
    wanted: set[str] | None = None,
) -> set[str]:
    """扫描 ``<install_dir>/models``，返回其中完整存在的 catalog 模型名。

    Args:
        install_dir: 托管安装根目录。
        wanted: 只关心这些模型名时传入，可少做无谓的 stat；``None`` 表示全扫。

    Returns:
        完整存在的模型名集合。名字用**调用方使用的写法**返回——catalog 的正式名带
        ``.ckpt`` 后缀而预设用不带后缀的别名，若只回正式名会对不上，功能等于失效。
    """
    models_dir = Path(install_dir) / "models"
    if not models_dir.is_dir():
        return set()

    catalog = load_catalog(install_dir)
    if not catalog:
        return set()

    keys = [name for name in wanted if name in catalog] if wanted is not None else list(catalog)
    complete: dict[str, bool] = {}
    found: set[str] = set()
    for key in keys:
        entry = catalog[key]
        state = complete.get(entry.name)
        if state is None:
            state = _is_complete(entry, models_dir)
            complete[entry.name] = state
        if state:
            found.add(key)
    return found


__all__ = ["CatalogEntry", "catalog_file", "load_catalog", "scan_local_models"]
