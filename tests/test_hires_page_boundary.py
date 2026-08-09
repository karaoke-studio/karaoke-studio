"""Hi-Res 混流页与外壳之间的边界。

这一页**已经是独立对象**了，边界不再是"清单描述现状"，而是真的封闭：页面能碰到
的外部东西只有构造时注入的 ``_host``，而 ``_host`` 能提供什么由
:class:`~krok_helper.hires.page.HiResHost` 说了算。

所以这里查两件事：
* 页面除了 ``_host`` 不再摸任何外部成员（静态）；
* 它真的能脱离主窗口构造、把素材接进来（动态）—— 这正是对象化换来的东西。
"""

from __future__ import annotations

import ast
import pathlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from PyQt6.QtWidgets import QApplication

from krok_helper.hires.page import HiResHost, HiResPage
from krok_helper.settings import AppSettings

PAGE = pathlib.Path(__file__).resolve().parents[1] / "krok_helper" / "hires" / "page.py"


def _fake_host(calls: list) -> SimpleNamespace:
    return SimpleNamespace(
        settings=AppSettings(),
        track_background_task=lambda task: task,
        resolve_ffmpeg_dir=lambda: None,
        resolve_output_name_mode=lambda: "fixed",
        resolve_output_name_templates=lambda: ("{video_name}_on", "{video_name}_off"),
        notify_handoff=lambda title, content: calls.append(("toast", title)),
        open_settings_window=lambda context: calls.append(("settings", context)),
    )


@pytest.fixture
def page():
    QApplication.instance() or QApplication([])
    calls: list = []
    widget = HiResPage(host=_fake_host(calls))
    yield widget, calls
    widget.deleteLater()


def test_the_page_reaches_outside_only_through_the_host() -> None:
    """静态检查：``self.X`` 里除了自己建的成员和 ``_host``，不该再有别的。"""
    tree = ast.parse(PAGE.read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "HiResPage")
    own = {n.name for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}

    assigned: set[str] = set()
    read: set[str] = set()
    for node in ast.walk(cls):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "self":
            (assigned if isinstance(node.ctx, ast.Store) else read).add(node.attr)

    # QWidget 自己的方法不算外部依赖。
    inherited = {name for name in read if hasattr(HiResPage.__mro__[1], name)}
    outside = read - own - assigned - inherited

    assert not outside, "页面绕过 _host 摸了外部成员：" + "、".join(sorted(outside))


def test_a_fake_host_satisfies_the_contract() -> None:
    assert isinstance(_fake_host([]), HiResHost)


def test_the_page_builds_without_the_main_window(page) -> None:
    """对象化换来的核心能力：不用造主窗口就能有这一页。"""
    widget, _ = page

    assert widget.video_zone is not None
    assert widget.hires_log is not None
    assert not widget.is_busy()
    assert widget.running_tasks() == []


def test_material_handoff_lands_in_the_cards(page) -> None:
    widget, calls = page

    widget.set_video_path(Path("D:/tmp/成片.mp4"))
    widget.set_on_vocal_path(Path("D:/tmp/原唱.flac"))
    accepted = widget.accept_separated_accompaniment(
        [Path("D:/tmp/伴奏1.wav"), Path("D:/tmp/伴奏2.wav")]
    )

    assert widget.video_zone.path == Path("D:/tmp/成片.mp4")
    assert widget.on_vocal_zone.path == Path("D:/tmp/原唱.flac")
    assert len(accepted) == 2
    assert ("toast", "伴奏已交给下一步") in calls


def test_the_page_does_not_import_the_shell() -> None:
    tree = ast.parse(PAGE.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}

    assert not any(m == "krok_helper.gui_qt" or m.startswith("krok_helper.gui_qt.") for m in imported)


def test_the_shell_still_satisfies_the_workflow_contract() -> None:
    """外壳把转交入口转调给本页，对外契约不变。"""
    from krok_helper.gui_qt import KrokHelperQtApp
    from krok_helper.workflow_host import WorkflowHost

    assert issubclass(KrokHelperQtApp, WorkflowHost)
