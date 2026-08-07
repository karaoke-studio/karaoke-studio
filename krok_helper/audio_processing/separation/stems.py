"""解析模型配置里的真实输出轨（stem）名。

用户在设置里给任务挑模型时，输出轨必须从**该模型自己声明的名字**里选，不能手填：
同一个概念在不同模型里可能叫 ``vocals`` / ``other`` / ``karaoke`` / ``instrumental``，
连 catalog 的 ``target_stem`` 字段都不可靠（``inst_v1e`` 标的是 ``vocals/instrumental``，
实际是 ``other`` 与 ``vocals``）。

权威来源是模型 YAML 的 ``training.instruments`` —— PyMSS 自己也读这里
（``model_card`` 的 ``instruments_source`` 字段写明了）。该文件只有 1 KB 左右，
不必下载数百 MB 的权重就能拿到。

工作台没有 PyYAML 依赖（它只存在于 pymss 运行时中），因此这里做一个针对该字段的
定向解析。**解析不出来就返回空**，由调用方提示用户换一个模型——绝不猜测或回退到
catalog 字段，否则就回到了「填 vocal 还是 vocals」的老问题。
"""

from __future__ import annotations

import re

#: 单个 stem 名的合法形状；超出这个范围一律视为解析失败。
_STEM_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-.]*$")

#: 一个模型的输出轨不会太多，明显超量说明解析跑偏了。
_MAX_STEMS = 16

_SEQUENCE_ITEM_RE = re.compile(r"^(\s*)-\s+(.+?)\s*$")
_KEY_RE = re.compile(r"^(\s*)([A-Za-z0-9_\-]+)\s*:\s*(.*?)\s*$")


def _strip_comment(value: str) -> str:
    """去掉行尾注释；配置里的值不含 '#'，因此按最左 '#' 截断即可。"""
    head = value.split("#", 1)[0]
    return head.strip().strip("'\"")


def _clean(values: list[str]) -> tuple[str, ...]:
    """校验并归一化 stem 列表；任何一项不合法就整体判失败。"""
    cleaned: list[str] = []
    for raw in values:
        item = _strip_comment(raw)
        if not item or not _STEM_RE.match(item):
            return ()
        if item not in cleaned:
            cleaned.append(item)
    if not cleaned or len(cleaned) > _MAX_STEMS:
        return ()
    return tuple(cleaned)


def parse_model_stems(config_text: str) -> tuple[str, ...]:
    """从模型 YAML 文本里取 ``training.instruments``。

    支持两种写法::

        training:
          instruments:
          - other
          - vocals

        training:
          instruments: [karaoke, other]

    Returns:
        真实 stem 名元组；无法可靠解析时返回空元组。
    """
    lines = config_text.splitlines()

    # 1) 定位顶层 training: 块（缩进为 0）。
    start = -1
    for index, line in enumerate(lines):
        match = _KEY_RE.match(line)
        if match and not match.group(1) and match.group(2) == "training":
            start = index + 1
            break
    if start < 0:
        return ()

    # 2) training 块的范围：到下一个顶层键为止。
    end = len(lines)
    for index in range(start, len(lines)):
        line = lines[index]
        if not line.strip() or line.startswith(("#", " ", "\t")):
            continue
        end = index
        break

    # 3) 块内找 instruments:。
    for index in range(start, end):
        match = _KEY_RE.match(lines[index])
        if not match or match.group(2) != "instruments":
            continue
        indent, inline = len(match.group(1)), match.group(3)

        # 行内列表写法。
        if inline:
            text = _strip_comment(inline)
            if text.startswith("[") and text.endswith("]"):
                return _clean([part for part in text[1:-1].split(",") if part.strip()])
            return ()

        # 块序列写法：后续以 '-' 开头、缩进不浅于 instruments 的行。
        values: list[str] = []
        for follow in lines[index + 1 : end]:
            if not follow.strip():
                continue
            item = _SEQUENCE_ITEM_RE.match(follow)
            if item is None or len(item.group(1)) < indent:
                break
            values.append(item.group(2))
        return _clean(values)

    return ()


__all__ = ["parse_model_stems"]
