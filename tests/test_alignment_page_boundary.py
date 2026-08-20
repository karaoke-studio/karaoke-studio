"""波形对齐页与外壳之间的边界。

五个页面里最后一个对象化的，也是最大的一个（81 方法 / 140+ 属性）。和前四个
一样，边界不再是"清单描述现状"，而是真的封闭：能碰到的外部东西只有构造时
注入的 ``_host``，能提供什么由 :class:`~krok_helper.alignment.page.AlignmentHost`
说了算。
"""

from __future__ import annotations

import ast
import pathlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from PyQt6.QtWidgets import QApplication, QWidget

from krok_helper.alignment.page import AlignmentHost, AlignmentPage
from krok_helper.settings import AppSettings

PAGE = pathlib.Path(__file__).resolve().parents[1] / "krok_helper" / "alignment" / "page.py"


def _fake_host(calls: list) -> SimpleNamespace:
    return SimpleNamespace(
        settings=AppSettings(),
        active_module="waveform_align",
        track_background_task=lambda task: task,
        resolve_ffmpeg_dir=lambda: None,
        build_media_info=lambda path, label: f"{label}: {path.name if path else '时长未知'}",
        set_panel_enabled=lambda panel, enabled: (
            calls.append(("panel", enabled)),
            [w.setEnabled(enabled) for w in panel.findChildren(QWidget)],
        ),
        focused_widget_is_text_input=lambda: False,
        notify_handoff=lambda title, content: calls.append(("toast", title)),
        open_settings_window=lambda context: calls.append(("settings", context)),
        set_on_vocal_path=lambda path: calls.append(("vocal", path)),
        set_subtitle_background_video=lambda path: (calls.append(("background", path)), True)[1],
    )


@pytest.fixture
def page():
    QApplication.instance() or QApplication([])
    calls: list = []
    widget = AlignmentPage(host=_fake_host(calls))
    yield widget, calls
    widget.deleteLater()


def test_the_page_reaches_outside_only_through_the_host() -> None:
    tree = ast.parse(PAGE.read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "AlignmentPage")
    own = {n.name for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    own |= {t.id for n in cls.body if isinstance(n, ast.Assign) for t in n.targets if isinstance(t, ast.Name)}

    assigned: set[str] = set()
    read: set[str] = set()
    for node in ast.walk(cls):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "self":
            (assigned if isinstance(node.ctx, ast.Store) else read).add(node.attr)
        # ``getattr(self, "xxx", None)`` 也是摸自己 —— 名字藏在字符串里，上面那条
        # 看不见。对齐页曾这样去拿主窗口的 ``subtitle_render_page``：搬出 gui_qt
        # 之后永远拿到 ``None``，转交静默失灵，扫描却是绿的。
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and node.args
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id == "self"
            and len(node.args) > 1
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
        ):
            read.add(node.args[1].value)

    inherited = {name for name in read if hasattr(QWidget, name)}
    outside = read - own - assigned - inherited

    assert not outside, "页面绕过 _host 摸了外部成员：" + "、".join(sorted(outside))


def test_a_fake_host_satisfies_the_contract() -> None:
    assert isinstance(_fake_host([]), AlignmentHost)


def test_the_page_builds_without_the_main_window(page) -> None:
    """五个页面里最后一个 —— 现在整个工作台没有一页需要主窗口才能立起来。"""
    widget, _ = page

    assert widget.align_video_zone is not None
    assert widget.waveform_view is not None
    assert widget.running_tasks() == []
    assert not widget.is_busy()


def test_settings_round_trip_through_the_page(page) -> None:
    widget, _ = page

    widget.load_settings()
    video_template, audio_template = widget.name_templates()

    assert video_template and audio_template
    mode, custom_dir = widget.output_dir_settings()
    assert mode
    assert isinstance(custom_dir, str)


def test_materials_land_in_the_cards(page) -> None:
    widget, _ = page

    widget.set_align_video_path(Path("D:/tmp/字幕视频.mkv"))
    widget.set_align_audio_path(Path("D:/tmp/原唱.flac"))

    assert widget.align_video_zone.path == Path("D:/tmp/字幕视频.mkv")
    assert widget.align_audio_zone.path == Path("D:/tmp/原唱.flac")


def test_shortcut_scope_follows_the_active_page(page) -> None:
    """三个快捷键挂在页面上，但作用域由外壳按当前步骤开关。"""
    widget, _ = page

    widget.sync_shortcut_scope(True)
    assert widget.shortcut_space.isEnabled()

    widget.sync_shortcut_scope(False)
    assert not widget.shortcut_space.isEnabled()


def test_the_page_does_not_import_the_shell() -> None:
    tree = ast.parse(PAGE.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}

    assert not any(m == "krok_helper.gui_qt" or m.startswith("krok_helper.gui_qt.") for m in imported)


def test_no_mixin_is_left_on_the_shell() -> None:
    """五个页面都对象化之后，主窗口就只剩 QMainWindow 一个基类了。"""
    from krok_helper.gui_qt import KrokHelperQtApp
    from PyQt6.QtWidgets import QMainWindow

    assert KrokHelperQtApp.__bases__ == (QMainWindow,)
