"""识别并校验用户已有的 MSST-WebUI 安装。

MSST 是 PyMSS 的前身，很多用户手上已经有一套能跑的环境（自带 ``workenv`` 里就有
torch）。让工作台直接驱动它，用户就不必再下一份 2.5 GB 的 PyMSS 托管 Runtime。

驱动方式不是启动它的 WebUI，而是用它自带的 Python 解释器执行工作台自己的桥接脚本
（见 :mod:`msst_service`）。因此这里要确认的是**能不能以库的方式用起来**：解释器、
推理模块、模型映射三者齐全。

工作台不修改用户的 MSST 目录（§4.4）：这里只做只读探测。
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

#: MSST 自带的 Python 解释器相对路径（官方整合包固定为 workenv）。
_PYTHON_CANDIDATES = (
    "workenv/python.exe",
    "workenv/Scripts/python.exe",
    "workenv/bin/python3",
    "workenv/bin/python",
)

#: 以库方式驱动所必需的文件。
_REQUIRED_FILES = (
    ("inference/msst_infer.py", "推理模块"),
    ("utils/constant.py", "模型类型表"),
)

#: 模型映射（任一存在即可）。
_MODEL_MAPS = (
    "data/msst_model_map.json",
    "data_backup/msst_model_map.json",
)


@dataclass(frozen=True)
class MsstEnvironment:
    """一套可用的 MSST 安装。"""

    root: Path
    python: Path

    @property
    def scripts_dir(self) -> Path:
        return self.root / "scripts"

    @property
    def pretrain_dir(self) -> Path:
        return self.root / "pretrain"


def find_python(root: str | os.PathLike) -> Path | None:
    """MSST 自带的解释器；找不到返回 None。"""
    base = Path(root)
    return next(
        (base / relative for relative in _PYTHON_CANDIDATES if (base / relative).is_file()),
        None,
    )


def locate_root(path: str | os.PathLike) -> Path | None:
    """把用户选中的目录换算成 MSST 根目录。

    允许用户选中根目录本身，也允许选中 ``pretrain`` 之类的子目录——按名字猜太脆弱，
    这里逐级向上找「有解释器且有推理模块」的那一层。
    """
    current = Path(path).resolve()
    for _ in range(4):
        if find_python(current) is not None and (current / "inference" / "msst_infer.py").is_file():
            return current
        if current.parent == current:
            break
        current = current.parent
    return None


def check_environment(path: str | os.PathLike) -> list[tuple[str, bool, str]]:
    """静态检查（不执行任何用户代码），返回 (项目, 通过, 说明) 列表。"""
    root = locate_root(path)
    if root is None:
        return [
            (
                "MSST 安装目录",
                False,
                "所选目录里没有找到 MSST（需要 workenv 解释器与 inference/msst_infer.py）。",
            )
        ]

    checks: list[tuple[str, bool, str]] = [("MSST 安装目录", True, str(root))]
    python = find_python(root)
    checks.append(
        ("Python 运行环境", python is not None, str(python) if python else "未找到 workenv 解释器")
    )
    for relative, label in _REQUIRED_FILES:
        exists = (root / relative).is_file()
        checks.append((label, exists, relative if exists else f"缺少 {relative}"))
    model_map = next((rel for rel in _MODEL_MAPS if (root / rel).is_file()), "")
    checks.append(
        ("模型映射", bool(model_map), model_map or "缺少 msst_model_map.json，无法识别已有模型")
    )
    return checks


def probe_runtime(
    root: str | os.PathLike,
    *,
    timeout_seconds: float = 120.0,
) -> tuple[bool, str]:
    """真实执行一次导入探测：确认 torch 与推理模块能在该环境里加载。

    静态文件检查过不了版本与依赖问题（比如 torch 装坏了），必须真跑一次。
    """
    base = locate_root(root)
    python = find_python(base) if base is not None else None
    if base is None or python is None:
        return False, "所选目录不是可用的 MSST 安装。"

    code = (
        "import json, sys;"
        "sys.path.insert(0, sys.argv[1]);"
        "import torch;"
        "from inference.msst_infer import MSSeparator;"
        "from utils.constant import MODEL_TYPE;"
        "print(json.dumps({'torch': torch.__version__,"
        " 'cuda': bool(torch.cuda.is_available()),"
        " 'types': len(MODEL_TYPE)}))"
    )
    try:
        completed = subprocess.run(
            [str(python), "-c", code, str(base)],
            cwd=str(base),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0,
        )
    except subprocess.TimeoutExpired:
        return False, "探测超时：MSST 环境加载时间过长。"
    except OSError as exc:
        return False, f"无法启动 MSST 的 Python 解释器：{exc}"

    if completed.returncode != 0:
        tail = (completed.stderr or completed.stdout or "").strip().splitlines()
        reason = tail[-1] if tail else f"退出码 {completed.returncode}"
        return False, f"MSST 环境无法加载推理模块：{reason}"

    line = (completed.stdout or "").strip().splitlines()
    try:
        import json

        info = json.loads(line[-1]) if line else {}
    except (ValueError, IndexError):
        return True, "推理模块可加载。"
    device = "CUDA 可用" if info.get("cuda") else "仅 CPU"
    return True, f"torch {info.get('torch', '未知')}，{device}，支持 {info.get('types', 0)} 种模型架构。"


__all__ = [
    "MsstEnvironment",
    "check_environment",
    "find_python",
    "locate_root",
    "probe_runtime",
]
