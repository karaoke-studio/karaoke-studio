"""设置对话框与外壳之间的边界。

这一块**已经是独立对象**（`SettingsDialogs`，不是 QWidget —— 它不常驻界面，
只按需搭对话框）。能碰到的外部东西只有构造时注入的 ``_host``。

``SettingsHost`` 里那组「波形对齐页的设置片段」是**明摆着的欠账**：对话框现在
直接读写对齐页的状态。等对齐页也对象化，那一组应当收敛成"向对齐页要一段设置
界面"。这里把这笔账单独断言出来，免得它悄悄长大。
"""

from __future__ import annotations

import ast
import pathlib

import pytest
from PyQt6.QtWidgets import QApplication, QWidget

from krok_helper.global_settings.page import SettingsDialogs, SettingsHost
from krok_helper.settings import AppSettings

PAGE = pathlib.Path(__file__).resolve().parents[1] / "krok_helper" / "global_settings" / "page.py"

#: 对齐页的设置片段 —— 曾经是九项穿透（模板值、输出目录、连那张 drop card 都
#: 直接摸），现在收敛成"向对齐页要一块 QWidget"。这一组只能是它自己。
ALIGNMENT_REACH_THROUGH = {"build_alignment_settings_fragment"}


class _Host(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.settings = AppSettings()
        self.ffmpeg_dir_text = ""
        self.output_name_mode_value = "fixed"
        self.on_name_template_value = "{video_name}_on"
        self.off_name_template_value = "{video_name}_off"

    def set_ffmpeg_dir(self, path) -> None:
        self.ffmpeg_dir_text = str(path) if path is not None else ""

    def sync_ffmpeg_labels(self) -> None: ...

    def sync_lyrics_timing_host_paths(self) -> None: ...

    def reload_lyrics_timing_settings(self) -> bool:
        return True

    def install_single_click_combo_behavior(self, combo) -> None: ...

    def start_workbench_update_check(self, *, manual: bool) -> None: ...

    def build_alignment_settings_fragment(self, parent=None):
        return QWidget(parent)

    def collect_page_settings(self) -> None: ...


@pytest.fixture
def host():
    QApplication.instance() or QApplication([])
    widget = _Host()
    yield widget
    widget.close()
    widget.deleteLater()


def test_the_dialogs_reach_outside_only_through_the_host() -> None:
    tree = ast.parse(PAGE.read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "SettingsDialogs")
    own = {n.name for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}

    assigned: set[str] = set()
    read: set[str] = set()
    for node in ast.walk(cls):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "self":
            (assigned if isinstance(node.ctx, ast.Store) else read).add(node.attr)

    assert not (read - own - assigned), "对话框绕过 _host 摸了外部成员"


def test_a_minimal_host_satisfies_the_contract(host) -> None:
    assert isinstance(host, SettingsHost)


def test_the_dialogs_build_without_the_main_window(host, monkeypatch) -> None:
    """对象化换来的能力：不用造主窗口就能开设置对话框。"""
    from krok_helper.global_settings import page as settings_page
    from krok_helper.updater.settings import UpdaterSettings

    monkeypatch.setattr(settings_page, "ensure_updater_settings", lambda _s: UpdaterSettings())
    built: list = []
    monkeypatch.setattr(
        settings_page.ModelessDialog,
        "exec",
        lambda dialog: (built.append(dialog), 0)[1],
    )

    dialogs = SettingsDialogs(host=host, parent=host)
    dialogs.open_page_settings("align")
    dialogs.open_global_settings()

    assert len(built) == 2
    assert "波形对齐设置" in built[0].windowTitle()
    assert "全局设置" in built[1].windowTitle()


def test_the_alignment_reach_through_has_not_grown() -> None:
    """对齐页那笔欠账只该变小。多一条就说明又隔空改了对齐页的东西。"""
    import re

    source = PAGE.read_text(encoding="utf-8")
    # 取 self._host.<名字> 里的第一段标识符（后面还可能跟 .path 之类）。
    touched = set(re.findall(r"self\._host\.(\w+)", source))
    alignment_touched = {name for name in touched if "align" in name}

    assert alignment_touched <= ALIGNMENT_REACH_THROUGH, (
        "对齐页的设置片段又长出了新的一条：" + "、".join(sorted(alignment_touched - ALIGNMENT_REACH_THROUGH))
    )


def test_the_dialogs_do_not_import_the_shell() -> None:
    tree = ast.parse(PAGE.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}

    assert not any(m == "krok_helper.gui_qt" or m.startswith("krok_helper.gui_qt.") for m in imported)
