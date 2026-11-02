"""扫字线图片素材的路径解析（叶子模块）。

图片扫字线形态的素材按**绝对路径**存盘；渲染期路径失效时回退到工程
（.yurika）所在目录按文件名查找。这里只做「这一帧有没有可用素材」的
判定，不依赖任何渲染/取色模块——``horizontal.scanline`` 的
``suppress_strip`` 需要在**每字形每帧**查询它，而 ``scanline_image``
反过来要消费 ``horizontal.scanline`` 的配色 helper，两边直接互相 import
会成环，所以解析逻辑单独放在这一层。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# 工程目录回退：.yurika 所在目录。session 在工程打开/另存/身份采纳时登记。
_SEARCH_ROOTS: list[Path] = []

# 路径解析的短时记忆：高亮带闸门按「每字形每帧」查询（suppress_strip），
# 不能每次都真做 stat。0.5s 内同 (路径, 搜索根) 复用结果——素材被移动/
# 删除后的回退切换最多延迟半秒，画面不会闪。
_RESOLVE_MEMO_TTL_S = 0.5
_resolve_memo: dict[tuple, tuple[float, Optional[str]]] = {}


def _resolve_memo_key(stored: str) -> tuple:
    return (stored, tuple(str(root) for root in _SEARCH_ROOTS))


def set_scanline_image_search_roots(paths: list[Path] | None) -> None:
    """登记渲染期路径回退的搜索根（通常只有工程所在目录一个）。"""

    _SEARCH_ROOTS[:] = [Path(item) for item in (paths or []) if str(item)]
    _resolve_memo.clear()


def scanline_image_search_roots() -> list[Path]:
    return list(_SEARCH_ROOTS)


def clear_scanline_image_resolve_memo() -> None:
    """丢弃路径解析的短时记忆（素材缓存整体失效时一起清）。"""

    _resolve_memo.clear()


def resolve_scanline_image_path(stored: str) -> Optional[str]:
    """素材路径解析：绝对路径优先，失效时按工程目录 basename 回退。

    返回可用的绝对路径字符串；未设置或两级查找都失败返回 ``None``
    （调用方整体跳过图片层，扫字线回退普通高亮带）。结果按
    ``_RESOLVE_MEMO_TTL_S`` 短时记忆（见上）。
    """

    text = str(stored or "").strip()
    if not text:
        return None
    key = _resolve_memo_key(text)
    now = time.monotonic()
    cached = _resolve_memo.get(key)
    if cached is not None and now - cached[0] < _RESOLVE_MEMO_TTL_S:
        return cached[1]
    resolved = _resolve_scanline_image_path_uncached(text)
    if len(_resolve_memo) > 64:
        _resolve_memo.clear()
    _resolve_memo[key] = (now, resolved)
    return resolved


def _resolve_scanline_image_path_uncached(text: str) -> Optional[str]:
    try:
        direct = Path(text)
    except (OSError, ValueError):
        return None
    if direct.is_file():
        return str(direct)
    name = direct.name
    if not name:
        return None
    for root in _SEARCH_ROOTS:
        try:
            candidate = root / name
            if candidate.is_file():
                return str(candidate)
        except OSError:
            continue
    return None


def scanline_image_available(stored: str) -> bool:
    return resolve_scanline_image_path(stored) is not None
