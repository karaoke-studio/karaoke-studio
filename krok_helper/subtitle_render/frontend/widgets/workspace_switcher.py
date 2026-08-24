"""主工作区切换控件（预览 / 导出）—— 兼容转发。

控件本体已提升为工作台公共组件 :mod:`krok_helper.workspace_switcher`，供第 2 步
「波形对齐 / 音频分离」等同级 Tab 复用。此处保留原导入路径，避免改动既有调用点。
"""

from __future__ import annotations

from krok_helper.workspace_switcher import (  # noqa: F401  (re-export)
    WorkspaceSwitcher,
    _WorkspaceSwitchItem,
    palette,
)

__all__ = ["WorkspaceSwitcher"]
