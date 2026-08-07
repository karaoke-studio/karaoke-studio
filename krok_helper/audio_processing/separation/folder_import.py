"""一键导入：扫描一个文件夹，自动为三个任务各匹配一个模型。

覆盖两种常见来源，都不要求用户逐个指定架构：

* **MSST-WebUI 的 ``pretrain``**：权重旁边没有配置，架构与配置路径来自 MSST 自己的
  ``data/msst_model_map.json``。因此从所选目录逐级向上找 MSST 根，找到就直接复用
  :func:`msst.scan_msst_models`，再筛出物理位于所选目录下的条目。
* **按 catalog 结构摆放的 ``models/``**：没有 MSST 映射，改用托管 Runtime 内置的
  ``model_catalog.json`` 按**文件名**反查架构与配置相对路径。

两条路径都拿不到架构的文件不会被猜测，只作为「无法识别」列出（§8.7 同一原则）。
"""

from __future__ import annotations

import os
from pathlib import Path

from .backend import ExternalModelCandidate
from .local_import import WEIGHT_SUFFIXES, guess_config_path
from .local_models import load_catalog
from .msst import _suggest_tasks, build_candidate, scan_msst_models
from .states import TaskType

#: 从所选目录最多向上找几层 MSST 根（pretrain/<类别> → 根，两层足够）。
_MSST_LOOKUP_DEPTH = 3


def find_msst_root(folder: str | Path) -> Path | None:
    """从所选目录逐级向上找 MSST-WebUI 根（带 ``msst_model_map.json`` 的那层）。"""
    current = Path(folder).resolve()
    for _ in range(_MSST_LOOKUP_DEPTH + 1):
        for relative in (
            "data/msst_model_map.json",
            "data_backup/msst_model_map.json",
        ):
            if (current / relative).is_file():
                return current
        if current.parent == current:
            break
        current = current.parent
    return None


def _is_under(path: Path, folder: Path) -> bool:
    try:
        path.resolve().relative_to(folder.resolve())
        return True
    except ValueError:
        return False


def _scan_via_catalog(
    folder: Path,
    install_dir: str | Path,
    *,
    cancelled=None,
) -> list[ExternalModelCandidate]:
    """按 catalog 文件名反查架构，处理没有 MSST 映射的目录。"""
    catalog = load_catalog(install_dir) if install_dir else {}
    if not catalog:
        return []

    results: list[ExternalModelCandidate] = []
    for weight in sorted(folder.rglob("*")):
        if cancelled is not None and cancelled.is_set():
            raise InterruptedError("模型文件夹扫描已取消。")
        if not weight.is_file() or weight.suffix.lower() not in WEIGHT_SUFFIXES:
            continue
        entry = catalog.get(weight.name) or catalog.get(weight.stem)
        if entry is None:
            continue  # 不在 catalog 里就无从得知架构，交给「无法识别」。
        model_type = getattr(entry, "model_type", "") or _catalog_model_type(
            install_dir, entry.name
        )
        if not model_type:
            continue
        config = guess_config_path(weight)
        if config is None and entry.config_relpath:
            candidate_config = Path(install_dir) / "models" / entry.config_relpath
            config = candidate_config if candidate_config.is_file() else None
        from .local_import import read_stems

        instruments = read_stems(config)
        for task in _suggest_tasks(weight.name, entry.relpath, instruments):
            results.append(
                build_candidate(
                    name=weight.name,
                    category="文件夹导入",
                    model_type=model_type,
                    model_path=weight,
                    config_path=config,
                    task=task,
                    instruments=instruments,
                    cancelled=cancelled,
                    source="local",
                )
            )
    return results


def _catalog_model_type(install_dir: str | Path, name: str) -> str:
    """catalog 条目里的 ``model_type``（local_models 只保留了落盘信息）。"""
    import json

    path = (
        Path(install_dir)
        / "runtime"
        / "Lib"
        / "site-packages"
        / "pymss"
        / "resources"
        / "model_catalog.json"
    )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    for row in payload.get("models") or []:
        if isinstance(row, dict) and row.get("name") == name:
            return str(row.get("model_type") or "")
    return ""


def scan_folder(
    folder: str | Path,
    *,
    install_dir: str | Path = "",
    cancelled=None,
) -> list[ExternalModelCandidate]:
    """扫描一个文件夹，返回其中可绑定的模型候选（同一模型可对应多个任务）。"""
    base = Path(folder)
    if not base.is_dir():
        raise FileNotFoundError("所选文件夹不存在。")

    root = find_msst_root(base)
    if root is not None:
        # MSST 映射是权威来源：架构与配置路径都由它给出。
        return [
            candidate
            for candidate in scan_msst_models(root, cancelled=cancelled)
            if candidate.model_path and _is_under(Path(candidate.model_path), base)
        ]
    return _scan_via_catalog(base, install_dir, cancelled=cancelled)


def match_tasks(
    candidates: list[ExternalModelCandidate],
    preferred: dict[TaskType, str] | None = None,
) -> dict[TaskType, ExternalModelCandidate]:
    """为每个任务挑一个最合适的候选。

    只考虑 ``bindable`` 的候选；与推荐预设同名的优先，其次按名字排序保证结果稳定。
    """
    preferred = preferred or {}
    matched: dict[TaskType, ExternalModelCandidate] = {}
    for task in TaskType:
        options = [
            item
            for item in candidates
            if item.task is task and item.bindable
        ]
        if not options:
            continue
        wanted = str(preferred.get(task, "") or "").lower()

        def rank(item: ExternalModelCandidate) -> tuple[int, str]:
            name = item.display_name.lower()
            stem = Path(item.display_name).stem.lower()
            exact = bool(wanted) and wanted in {name, stem}
            return (0 if exact else 1, item.display_name)

        matched[task] = sorted(options, key=rank)[0]
    return matched


__all__ = ["find_msst_root", "match_tasks", "scan_folder"]
