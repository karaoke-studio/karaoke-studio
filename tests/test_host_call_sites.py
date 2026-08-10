"""页面调宿主的每一处，都要能按契约声明的形状调通。

`test_host_protocol_signatures` 管的是**外壳那一侧**：契约声明了什么，外壳就得
按那个形状可调。这份管**页面那一侧**：页面写下的 `self._host.xxx(...)` 也得对得上。

两侧都得钉，因为 `isinstance(host, Protocol)` 只查方法存不存在。对齐页对象化后
`self._host.track_background_task("align_export_task", task)` 还留着搬迁前的两参形式
（旧的外壳方法是按属性名代管任务的），点「生成波形」当场 TypeError —— 边界测试
只看"碰了谁"，看不出"怎么调的"。

用 AST 静态扫描，不用真跑到那行代码。
"""

from __future__ import annotations

import ast
import inspect
import pathlib

import pytest

from krok_helper.alignment.page import AlignmentHost
from krok_helper.global_settings.page import SettingsHost
from krok_helper.hires.page import HiResHost
from krok_helper.lyrics_search.page import LyricsSearchHost

ROOT = pathlib.Path(__file__).resolve().parents[1] / "krok_helper"

PAGES = [
    (ROOT / "alignment" / "page.py", AlignmentHost),
    (ROOT / "lyrics_search" / "page.py", LyricsSearchHost),
    (ROOT / "hires" / "page.py", HiResHost),
    (ROOT / "global_settings" / "page.py", SettingsHost),
]


def _host_calls(tree: ast.AST):
    """所有 ``self._host.NAME(...)`` 调用点。"""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        owner = func.value
        if (
            isinstance(owner, ast.Attribute)
            and owner.attr == "_host"
            and isinstance(owner.value, ast.Name)
            and owner.value.id == "self"
        ):
            yield func.attr, node


@pytest.mark.parametrize("path,protocol", PAGES, ids=lambda v: getattr(v, "__name__", None) or v.name)
def test_every_host_call_matches_the_contract(path: pathlib.Path, protocol) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    problems: list[str] = []

    for name, call in _host_calls(tree):
        member = getattr(protocol, name, None)
        if member is None:
            problems.append(f"第 {call.lineno} 行：契约里没有 {name}")
            continue
        if not callable(member):
            problems.append(f"第 {call.lineno} 行：{name} 是数据成员，不能当方法调")
            continue
        if any(isinstance(a, ast.Starred) for a in call.args) or any(k.arg is None for k in call.keywords):
            continue  # 展开传参，静态看不出实参个数

        try:
            # 第一个占位实参顶掉 self。
            inspect.signature(member).bind(object(), *[None] * len(call.args), **{k.arg: None for k in call.keywords})
        except TypeError as exc:
            problems.append(f"第 {call.lineno} 行：{name}(...) 对不上契约 —— {exc}")

    assert not problems, f"{path.name} 调 {protocol.__name__} 的方式有问题：\n  " + "\n  ".join(problems)


@pytest.mark.parametrize("path,protocol", PAGES, ids=lambda v: getattr(v, "__name__", None) or v.name)
def test_the_contract_has_no_dead_members(path: pathlib.Path, protocol) -> None:
    """契约只该声明页面真在用的东西 —— 多余的一项就是外壳白背的一份包袱。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    used = {name for name, _ in _host_calls(tree)}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "_host"
            and isinstance(node.value.value, ast.Name)
            and node.value.value.id == "self"
        ):
            used.add(node.attr)

    declared = {n for n in vars(protocol) if not n.startswith("_")}
    declared |= {n for n in getattr(protocol, "__annotations__", {}) if not n.startswith("_")}

    assert not (declared - used), f"{protocol.__name__} 声明了但没人用：" + "、".join(sorted(declared - used))
