"""从任意文件夹导入一个模型。

自动导入（:mod:`local_models`）只认「按 catalog 结构摆进 ``models/``」的模型；
MSST 扫描（:mod:`msst`）依赖 MSST-WebUI 自己的 map 文件。两者都覆盖不到「我手上
就有一个 .ckpt，放在任意目录」的情况，本模块补上这条路径。

候选构造复用 :func:`msst.build_candidate`——状态判定、大小与哈希两条路径必须一致，
否则同一个文件经不同入口导入会得到不同结论。
"""

from __future__ import annotations

from pathlib import Path

from .backend import ExternalModelCandidate
from .msst import _SUPPORTED_TYPES, _config_instruments, build_candidate
from .states import TaskType

#: 权重文件的常见后缀（与 MSST 扫描一致）。
WEIGHT_SUFFIXES = (".ckpt", ".chpt", ".th", ".pth", ".pt")

#: 不需要 YAML 配置的架构；其余架构缺配置就无法加载。
_CONFIG_FREE_TYPES = {"vr", "demucs", "tasnet", "legacy_demucs", "legacy_tasnet"}


def supported_model_types() -> tuple[str, ...]:
    """PyMSS 支持的架构列表，供界面做下拉选择（架构无法从配置推断）。"""
    return tuple(sorted(_SUPPORTED_TYPES))


def guess_config_path(weight_path: str | Path) -> Path | None:
    """猜同目录下的配置文件：优先同名 ``.yaml``，其次同名 ``.yml``。

    找不到就返回 None，由界面让用户手动指定——不做模糊匹配，避免把不相干的
    配置套到模型上。
    """
    weight = Path(weight_path)
    for suffix in (".yaml", ".yml"):
        candidate = weight.with_suffix(suffix)
        if candidate.is_file():
            return candidate
    return None


def read_stems(config_path: str | Path | None) -> tuple[str, ...]:
    """读配置里声明的真实输出轨；读不出返回空（界面据此提示，不猜）。"""
    if not config_path:
        return ()
    return _config_instruments(Path(config_path))


def requires_config(model_type: str) -> bool:
    return str(model_type or "").strip().lower() not in _CONFIG_FREE_TYPES


def build_local_candidate(
    *,
    weight_path: str | Path,
    config_path: str | Path | None,
    model_type: str,
    task: TaskType,
    display_name: str = "",
    cancelled=None,
) -> ExternalModelCandidate:
    """由用户选定的文件构造一个可绑定候选。

    Raises:
        FileNotFoundError: 权重文件不存在。
        ValueError: 架构不在 PyMSS 支持范围内。
    """
    weight = Path(weight_path)
    if not weight.is_file():
        raise FileNotFoundError("所选权重文件不存在。")
    normalized = str(model_type or "").strip().lower()
    if normalized not in _SUPPORTED_TYPES:
        raise ValueError(f"PyMSS 不支持的模型架构：{model_type or '未选择'}。")

    config = Path(config_path) if config_path else None
    if config is not None and not config.is_file():
        config = None

    return build_candidate(
        name=display_name.strip() or weight.stem,
        category="本地导入",
        model_type=normalized,
        model_path=weight,
        config_path=config,
        task=task,
        instruments=read_stems(config),
        cancelled=cancelled,
        source="local",
    )


__all__ = [
    "WEIGHT_SUFFIXES",
    "build_local_candidate",
    "guess_config_path",
    "read_stems",
    "requires_config",
    "supported_model_types",
]
