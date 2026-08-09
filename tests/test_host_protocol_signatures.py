"""外壳实现的宿主接口，签名必须和契约一致。

页面对象化时，外壳这边是一层薄转调（``def resolve_ffmpeg_dir: return
self._resolve_ffmpeg_dir()``）。手写这层最容易漏参数：
``start_workbench_update_check`` 就漏掉了三个关键字参数，结果「立即检查更新」
一点就抛 ``TypeError``，而 ``isinstance(host, Protocol)`` 只查方法**存在**、
不查签名，拦不住。

这条测试补上那一半：契约声明了什么形状，外壳就得能按那个形状被调用。
"""

from __future__ import annotations

import inspect
from typing import get_type_hints

import pytest

from krok_helper.global_settings.page import SettingsHost
from krok_helper.gui_qt import KrokHelperQtApp
from krok_helper.hires.page import HiResHost
from krok_helper.lyrics_search.page import LyricsSearchHost

PROTOCOLS = [HiResHost, LyricsSearchHost, SettingsHost]


def _protocol_methods(protocol) -> list[str]:
    names = []
    for name, member in vars(protocol).items():
        if name.startswith("_") or not callable(member):
            continue
        names.append(name)
    return sorted(names)


def _protocol_attributes(protocol) -> list[str]:
    try:
        hints = get_type_hints(protocol)
    except Exception:  # 前向引用解析不了时退回注解本身
        hints = getattr(protocol, "__annotations__", {})
    return sorted(name for name in hints if not name.startswith("_"))


@pytest.mark.parametrize("protocol", PROTOCOLS, ids=lambda p: p.__name__)
def test_the_shell_implements_every_protocol_method(protocol) -> None:
    missing = [name for name in _protocol_methods(protocol) if not hasattr(KrokHelperQtApp, name)]

    assert not missing, f"{protocol.__name__} 要求的方法外壳没实现：" + "、".join(missing)


@pytest.mark.parametrize("protocol", PROTOCOLS, ids=lambda p: p.__name__)
def test_the_shell_accepts_the_declared_call_shape(protocol) -> None:
    """逐个方法比对参数名与种类 —— 漏一个关键字参数就在这里红。"""
    problems: list[str] = []
    for name in _protocol_methods(protocol):
        declared = inspect.signature(getattr(protocol, name))
        actual = inspect.signature(getattr(KrokHelperQtApp, name))

        declared_params = [p for p in declared.parameters.values() if p.name != "self"]
        actual_params = {p.name: p for p in actual.parameters.values() if p.name != "self"}

        for param in declared_params:
            got = actual_params.get(param.name)
            if got is None:
                problems.append(f"{name}(): 缺少参数 {param.name}")
            elif got.kind != param.kind:
                problems.append(f"{name}(): 参数 {param.name} 的种类不符（{got.kind} != {param.kind}）")

        # 外壳多出来的必填参数同样调不通。
        for pname, param in actual_params.items():
            if param.default is inspect.Parameter.empty and pname not in {p.name for p in declared_params}:
                if param.kind not in {inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD}:
                    problems.append(f"{name}(): 外壳多要了一个必填参数 {pname}")

    assert not problems, f"{protocol.__name__} 与外壳实现对不上：\n  " + "\n  ".join(problems)


@pytest.mark.parametrize("protocol", PROTOCOLS, ids=lambda p: p.__name__)
def test_the_shell_carries_every_protocol_attribute(protocol) -> None:
    """契约里的数据成员由实例持有，类上查不到，所以只断言它们有声明来源。"""
    declared = _protocol_attributes(protocol)

    assert declared, f"{protocol.__name__} 没有声明任何数据成员？"
