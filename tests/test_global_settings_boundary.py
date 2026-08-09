"""设置对话框与宿主之间的边界。

和三个工作流页面同样的做法：清单写死，只许变短。

这一份比页面那三份长，因为设置对话框天然要横跨各域——它读写 ffmpeg 目录、
对齐导出命名、更新器偏好。真正做对象化时，这里多半不是"搬走"，而是让各页
自己提供"设置片段"，由对话框拼装。
"""

from __future__ import annotations

import ast
import pathlib

PAGE = pathlib.Path(__file__).resolve().parents[1] / "krok_helper" / "global_settings" / "page.py"

#: 宿主服务：设置的读写生命周期留在外壳，对话框调它们落盘 / 刷新界面。
HOST_SERVICES = {
    "settings",
    "set_ffmpeg_dir",
    "_sync_ffmpeg_labels",
    "_sync_lyrics_timing_host_paths",
    "_install_single_click_combo_behavior",
    "_start_workbench_update_check",
}

#: 波形对齐页的设置片段 —— 对话框直接读写了对齐页的状态与接缝。
#: 对象化时这一组应当换成"向对齐页要一段设置界面"，而不是隔空改它的属性。
ALIGNMENT_REACH_THROUGH = {
    "align_output_custom_dir_text",
    "align_output_dir_mode_value",
    "align_video_zone",
    "set_alignment_output_dir_settings",
    "_collect_alignment_settings",
    "_update_alignment_preferences_from_ui",
    "_validate_alignment_name_template",
}


def _foreign_members() -> set[str]:
    tree = ast.parse(PAGE.read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "GlobalSettingsMixin")
    own = {n.name for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}

    assigned: set[str] = set()
    read: set[str] = set()
    for node in ast.walk(cls):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "self":
            (assigned if isinstance(node.ctx, ast.Store) else read).add(node.attr)
    return read - own - assigned


def test_the_dialog_only_reaches_for_the_declared_host_surface() -> None:
    unexpected = _foreign_members() - HOST_SERVICES - ALIGNMENT_REACH_THROUGH

    assert not unexpected, (
        "设置对话框多摸了宿主成员：" + "、".join(sorted(unexpected)) + "。"
        "先判断它该跟着搬、还是该走显式接口，再决定要不要加进清单。"
    )


def test_the_declared_surface_has_not_gone_stale() -> None:
    stale = (HOST_SERVICES | ALIGNMENT_REACH_THROUGH) - _foreign_members()

    assert not stale, "清单里这些已经不再被引用，可以删了：" + "、".join(sorted(stale))


def test_the_dialog_does_not_import_the_shell() -> None:
    tree = ast.parse(PAGE.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}

    assert not any(m == "krok_helper.gui_qt" or m.startswith("krok_helper.gui_qt.") for m in imported)
